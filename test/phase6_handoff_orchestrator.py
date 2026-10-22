# -*- coding: utf-8 -*-
"""
phase6_handoff_orchestrator.py — Phase 6.10 Unattended Ownership Handoff
======================================================================
問題
    場站同時存在兩個會控制 PCS 的系統：

        external  /home/etica/ems/auto_control.py（screen 1128.auto，PID 1140）
        Phase 6   本專案的自動充放電

    兩者**永遠不得同時持有控制權**。要讓 Phase 6 無人介入運作，就必須有一套
    可驗證、可恢復、可稽核的交接程序：

        external 暫停 → 驗證真的停了 → Phase 6 接手 → Phase 6 收斂
        → 驗證真的放手 → external 恢復 → 驗證真的恢復

🔴 **最高不變量：Ownership 互斥**
    任何時刻只能是 EXTERNAL / PHASE6 / NEITHER 三者之一。
    `BOTH` 不是狀態，是**不變量被破壞** —— 一旦偵測到即進入 CRITICAL。

🔴 **restore 失敗是 critical failure，不是一般錯誤**
    我方暫停了場站原本的套利程式卻沒能恢復 → 場站直接損失。
    因此：
      · 只要曾經送出 pause，就永遠背負 restore 責任（durable journal 記錄）
      · Windows 重開機也不能讓這個責任消失
      · restore 失敗一律 RESTORE_FAILED_CRITICAL，需人工介入，不自動重試到天荒地老

🔴 **所有動作前必須 fresh read**
    不沿用任何先前快照。任一 Gate FAIL → Fail Closed → 不前進。

🔴 **不硬編碼任何時刻**
    沒有 00:05 / 00:30 這種常數。進場條件一律由
    TOU + fresh SOC + Authority + PCS state + meter + alarm/fault 決定。

🔴 **FIRST LIVE 完成前不得 dispatch**
    模組層 `DISPATCH_ENABLED = False`，且 `MODE_DRY_RUN` 為預設。
    真正送指令需要同時：FIRST LIVE 已完成 ＋ 明確授權 ＋ 非 dry-run 模式。

全部外部相依皆由呼叫端注入（remote / phase6 / clock / journal），
因此可完全離線測試，不需要設備、不需要 SSH。
"""
import io
import os
import json
import time
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JOURNAL = os.path.join(os.path.dirname(HERE), "output",
                               "phase6_live_charge", "handoff_journal.jsonl")

# ======================================================================
# 🔴 FIRST LIVE 尚未完成 —— 本模組結構上不得送出任何實機控制
# ======================================================================
DISPATCH_ENABLED = False

MODE_DRY_RUN = "DRY_RUN"          # 什麼都不送，只跑狀態機與 Gate（預設）
MODE_OBSERVE = "OBSERVE"          # 只做 fresh read 與 Gate 評估，不進交接
MODE_ARMED = "ARMED"              # 真正會送 pause / dispatch / restore
SUPPORTED_MODES = (MODE_DRY_RUN, MODE_OBSERVE, MODE_ARMED)


# ======================================================================
# Production Handoff 參數（Phase 6.10-B1.5，經 2026-09-02 裁示核准）
# ======================================================================
# 🔴 寫入參數**不等於**啟用控制：DISPATCH_ENABLED 仍為 False、
#    預設 MODE 仍為 DRY_RUN。這些值只是「若要執行時該用什麼」。
#
# 🔴 span 與 (samples-1) x interval 必須一致
#    4 samples @ 15 s -> t = 0 / 15 / 30 / 45 -> span 45 s
#    3 samples @  3 s -> t = 0 /  3 /  6      -> span  6 s
#    明確記錄 span，避免不同 caller 對 interval x samples 產生 off-by-one 誤解。
class HandoffConfig(object):
    """已核准參數。修改任一項都必須重新取得裁示。"""

    # --- idle power band ---
    #   證據：27,179 筆 steady-idle 樣本（已排除 AC 暫存器落後的假 idle）
    #         min -3.00 / p50 -1.30 / max 0.00；[-3.0, +1.0] 覆蓋 100.0000%
    #   上界安全性：最接近的真實充放電量值為 5.40 kW，分離裕度 2.4 kW，
    #   因此 Phase 6 自己的 5.0 kW 充電不可能被誤判為 idle。
    idle_power_lower_kw = -3.0
    idle_power_upper_kw = 1.0

    # --- pause settling ---
    #   證據：20 次「取樣連續」的 active->idle 收斂，observed max = 22 s
    #         credible upper bound ~= 42 s（22 + stop-loop 5 s + AC lag 15 s）
    #         production timeout 120 s ~= 2.86 x credible upper bound
    #   ⚠️ 先前算出的 571 s 是 log 取樣中斷造成的假值，非真實收斂時間。
    pause_settling_timeout_sec = 120.0

    # --- stable verify ---
    #   interval 取既有 ESS 新鮮度契約（15.0 s），保證每筆為獨立 fresh read。
    #   span 必須長於「外部 controller 重新取得控制」的最長觀測值：
    #   2026-09-01 實測 STOPPED 00:00:44 -> CHARGING 00:01:16 共 32 s < 45 s。
    stable_verify_min_samples = 4
    stable_verify_interval_sec = 15.0
    stable_verify_min_span_sec = 45.0

    # --- restore loop ---
    #   controller 每約 1 秒輸出一行時間戳；3 筆可抵抗單次 stale hardcopy。
    #   interval 沿用既有 3.0 s 契約（meter STALE_AFTER_SEC / classifier debounce）。
    restore_loop_min_observations = 3
    restore_loop_interval_sec = 3.0
    restore_loop_min_span_sec = 6.0

    @classmethod
    def validate(cls):
        """
        自我檢查。回傳 problems 清單（空 = 合格）。

        🔴 span 一致性是重點：span 必須等於 (samples - 1) x interval。
           不一致代表某處對窗口長度的理解錯了。
        """
        p = []
        if not cls.idle_power_lower_kw < cls.idle_power_upper_kw:
            p.append("idle band 上下界順序錯誤")
        if max(abs(cls.idle_power_lower_kw), abs(cls.idle_power_upper_kw)) >= 4.0:
            p.append("idle band 絕對值過大，可能把 5 kW 充放電誤判為 idle")
        want = (cls.stable_verify_min_samples - 1) * cls.stable_verify_interval_sec
        if abs(want - cls.stable_verify_min_span_sec) > 1e-9:
            p.append("stable_verify span 與 (samples-1)xinterval 不一致：%s vs %s"
                     % (cls.stable_verify_min_span_sec, want))
        want2 = (cls.restore_loop_min_observations - 1) * cls.restore_loop_interval_sec
        if abs(want2 - cls.restore_loop_min_span_sec) > 1e-9:
            p.append("restore_loop span 與 (obs-1)xinterval 不一致：%s vs %s"
                     % (cls.restore_loop_min_span_sec, want2))
        if cls.restore_loop_min_observations < 2:
            p.append("restore_loop 觀測數需 >= 2（單點無法證明前進）")
        if cls.pause_settling_timeout_sec <= 0:
            p.append("settling timeout 必須為正數")
        return p


# ======================================================================
# A. State Machine
# ======================================================================
S_IDLE = "IDLE"                             # 未持有任何責任
S_PREFLIGHT = "PREFLIGHT"                   # fresh read 兩側，評估 Gate
S_PAUSE_REQUESTED = "PAUSE_REQUESTED"       # 已送出 pause（責任已產生）
S_PAUSE_VERIFYING = "PAUSE_VERIFYING"
S_EXTERNAL_PAUSED = "EXTERNAL_PAUSED"       # external 確認停止，ownership=NEITHER
S_PHASE6_ACTIVE = "PHASE6_ACTIVE"           # Phase 6 持有控制權
S_PHASE6_RELEASING = "PHASE6_RELEASING"
S_PHASE6_RELEASED = "PHASE6_RELEASED"       # Phase 6 放手，ownership=NEITHER
S_RESTORE_REQUESTED = "RESTORE_REQUESTED"
S_RESTORE_VERIFYING = "RESTORE_VERIFYING"
S_RESTORED = "RESTORED"                     # external 恢復，ownership=EXTERNAL
S_COMPLETE = "COMPLETE"

# ---- 失敗 / 例外狀態 ----
S_ABORTED_PREFLIGHT = "ABORTED_PREFLIGHT"           # 尚未動任何東西，無 restore 責任
S_ABORTED_PAUSE_FAILED = "ABORTED_PAUSE_FAILED"     # pause 未生效；external 仍在跑
S_RESTORE_FAILED = "RESTORE_FAILED_CRITICAL"        # 🔴 場站失去套利，需人工
S_OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT_CRITICAL"  # 🔴 兩者同時持有

# ---- C2. SSH command timeout 造成的「結果不明」狀態 ----
# 🔴 SSH_COMMAND_TIMEOUT **不等於**「指令沒有執行」。連線已建立、指令已送出，
#    只是 client 在收到 outcome 前放棄等待 —— 遠端可能已經照做了。
#    因此不得落到 ABORTED_PAUSE_FAILED（那代表責任未成立），必須進入
#    專屬的不確定狀態，並保守地視為**已背負恢復責任**。
S_PAUSE_OUTCOME_UNKNOWN = "PAUSE_OUTCOME_UNKNOWN"
S_RESTORE_OUTCOME_UNKNOWN = "RESTORE_OUTCOME_UNKNOWN"

# 這些狀態代表「我方已暫停 external，尚未恢復」→ 背負 restore 責任
PENDING_RESTORE_STATES = frozenset({
    S_PAUSE_REQUESTED, S_PAUSE_VERIFYING, S_EXTERNAL_PAUSED,
    S_PHASE6_ACTIVE, S_PHASE6_RELEASING, S_PHASE6_RELEASED,
    S_RESTORE_REQUESTED, S_RESTORE_VERIFYING,
    # 🔴 兩個 timeout 不確定狀態一律算「背負責任」—— 寧可多還一次，
    #    也不能因為「可能沒暫停成功」就當作沒事。
    S_PAUSE_OUTCOME_UNKNOWN, S_RESTORE_OUTCOME_UNKNOWN,
})
TERMINAL_STATES = frozenset({
    S_COMPLETE, S_ABORTED_PREFLIGHT, S_ABORTED_PAUSE_FAILED,
    S_RESTORE_FAILED, S_OWNERSHIP_CONFLICT,
})
CRITICAL_STATES = frozenset({S_RESTORE_FAILED, S_OWNERSHIP_CONFLICT})

# 合法轉移（狀態機是白名單，不是「除了…以外都可以」）
TRANSITIONS = {
    S_IDLE: {S_PREFLIGHT},
    S_PREFLIGHT: {S_PAUSE_REQUESTED, S_ABORTED_PREFLIGHT},
    S_PAUSE_REQUESTED: {S_PAUSE_VERIFYING, S_ABORTED_PAUSE_FAILED,
                        S_RESTORE_REQUESTED, S_PAUSE_OUTCOME_UNKNOWN},
    # 只能先觀測，再決定是「其實暫停成功了」還是「要歸還」。
    # **沒有一條轉移可以回到 S_PAUSE_REQUESTED** —— 結構上禁止 blind resend。
    S_PAUSE_OUTCOME_UNKNOWN: {S_PAUSE_VERIFYING, S_RESTORE_REQUESTED},
    S_PAUSE_VERIFYING: {S_EXTERNAL_PAUSED, S_RESTORE_REQUESTED,
                        S_ABORTED_PAUSE_FAILED, S_OWNERSHIP_CONFLICT},
    S_EXTERNAL_PAUSED: {S_PHASE6_ACTIVE, S_RESTORE_REQUESTED,
                        S_OWNERSHIP_CONFLICT},
    S_PHASE6_ACTIVE: {S_PHASE6_RELEASING, S_OWNERSHIP_CONFLICT},
    S_PHASE6_RELEASING: {S_PHASE6_RELEASED, S_OWNERSHIP_CONFLICT},
    S_PHASE6_RELEASED: {S_RESTORE_REQUESTED},
    S_RESTORE_REQUESTED: {S_RESTORE_VERIFYING, S_RESTORE_FAILED,
                          S_RESTORE_OUTCOME_UNKNOWN},
    # 同理：restore 逾時也只能先觀測。不得直接重送 restore。
    S_RESTORE_OUTCOME_UNKNOWN: {S_RESTORE_VERIFYING, S_RESTORE_FAILED},
    S_RESTORE_VERIFYING: {S_RESTORED, S_RESTORE_FAILED},
    S_RESTORED: {S_COMPLETE},
    S_COMPLETE: set(),
    S_ABORTED_PREFLIGHT: set(),
    S_ABORTED_PAUSE_FAILED: set(),
    S_RESTORE_FAILED: set(),
    S_OWNERSHIP_CONFLICT: {S_RESTORE_REQUESTED},   # 人工處置後仍需歸還
}


# ======================================================================
# B. Ownership Handoff State
# ======================================================================
OWN_EXTERNAL = "EXTERNAL"
OWN_NEITHER = "NEITHER"
OWN_PHASE6 = "PHASE6"
OWN_UNKNOWN = "UNKNOWN"
OWN_BOTH = "BOTH"                 # 🔴 不變量破壞，不是正常狀態

# 每個狀態下**可接受**的 ownership（集合，不是單一值）。
# 🔴 語意修正（2026-09-02 裁示）：RESTORED / COMPLETE **不得**強制 EXTERNAL。
#    external controller 恢復後，可能依 SOC / TOU / demand policy **正確地**
#    選擇不動作 —— 此時 ownership = NEITHER，那是恢復成功，不是失敗。
#    唯一絕對禁止的是 BOTH。
ACCEPTABLE_OWNERSHIP = {
    S_IDLE: frozenset({OWN_EXTERNAL, OWN_NEITHER}),
    S_PREFLIGHT: frozenset({OWN_EXTERNAL, OWN_NEITHER}),
    S_PAUSE_REQUESTED: frozenset({OWN_EXTERNAL, OWN_NEITHER, OWN_UNKNOWN}),
    S_PAUSE_VERIFYING: frozenset({OWN_EXTERNAL, OWN_NEITHER, OWN_UNKNOWN}),
    S_EXTERNAL_PAUSED: frozenset({OWN_NEITHER}),
    S_PHASE6_ACTIVE: frozenset({OWN_PHASE6}),
    S_PHASE6_RELEASING: frozenset({OWN_PHASE6, OWN_NEITHER}),
    S_PHASE6_RELEASED: frozenset({OWN_NEITHER}),
    S_RESTORE_REQUESTED: frozenset({OWN_NEITHER}),
    S_RESTORE_VERIFYING: frozenset({OWN_NEITHER, OWN_EXTERNAL, OWN_UNKNOWN}),
    S_RESTORED: frozenset({OWN_EXTERNAL, OWN_NEITHER}),
    S_COMPLETE: frozenset({OWN_EXTERNAL, OWN_NEITHER}),
}


def classify_ownership(external_running, phase6_holding):
    """
    由兩側的**觀測事實**推導 ownership。

    external_running : external controller 是否仍在主動控制（None = 無法判定）
    phase6_holding   : Phase 6 是否持有 Control Authority（None = 無法判定）

    🔴 任一側無法判定 → UNKNOWN（Fail Closed），不猜。
    🔴 兩側皆為真 → BOTH → 不變量破壞。
    """
    if external_running is None or phase6_holding is None:
        return OWN_UNKNOWN
    if external_running and phase6_holding:
        return OWN_BOTH
    if external_running:
        return OWN_EXTERNAL
    if phase6_holding:
        return OWN_PHASE6
    return OWN_NEITHER


# ======================================================================
# C. Pause / Restore mechanism
# ======================================================================
# 🔴 **非互動**：不 attach screen。以 `screen -S auto -X stuff "stop\n"`
#    把指令注入既有 session；`screen -X` 是 client 指令，不需要 PTY，
#    因此可在 authorized_keys 的 `restrict`（no-pty）下運作。
#
# 🔴 **pause 的驗證一律看行為，不看宣稱**
#    不以「畫面出現『已停止運行』」為準（那只是輸出），而是看：
#      · auto_control.py 行程仍存活（stop 不該讓它死掉）
#      · screen session 仍存在
#      · PCS 功率收斂到約 0
#      · PCS 維持 legal idle（STANDBY / STOPPED 皆可）
#      · 觀測窗口內不再出現新的 setpoint 變化 / 自行 Power-On
#
# ⚠️ external 的 "stop" **不等於** PCS 的 STOP 指令；它停的是 automation loop
#    並把 active power 收斂到 0，PCS 可能停在 STANDBY 而非 STOPPED。
PAUSE_COMMAND = "stop"
RESTORE_COMMAND = "start"

# 收斂判準：功率是否已「約等於 0」。
# 🔴 沿用既有 idle baseline 語意 —— 設備待機時 AC 讀值本來就不是 0
#    （現場長期觀測為 −1.3 ~ −1.4 kW）。因此不能用 `== 0` 判定。
#    容差由呼叫端提供，預設取 Safety Gate 既有的 authority power tolerance，
#    不另外發明數字。
def power_is_idle(ac_kw, baseline_kw, tolerance_kw):
    """|ac - baseline| <= tolerance 視為已收斂。任一為 None → False（Fail Closed）。"""
    if ac_kw is None or baseline_kw is None or tolerance_kw is None:
        return False
    return abs(float(ac_kw) - float(baseline_kw)) <= float(tolerance_kw)


# 三值 send outcome 由 adapter 定義（唯一定義處），此處只引用。
from phase6_remote_adapter import (SEND_SENT, SEND_NOT_SENT,  # noqa: E402
                                   SEND_UNKNOWN)


class RemoteController(object):
    """
    external controller 的遠端操作介面。**全部由呼叫端注入實作。**

    本類別只定義契約並提供安全預設：未注入任何 sender 時，
    `send_pause` / `send_restore` 一律回 False（結構上送不出去）。
    """

    def __init__(self, probe=None, pause_sender=None, restore_sender=None):
        self._probe = probe                  # () -> dict：遠端唯讀狀態
        self._pause_sender = pause_sender    # () -> bool
        self._restore_sender = restore_sender

    def probe(self):
        """唯讀取得遠端狀態。回傳 dict，至少含 screen_alive / pid_alive / running。"""
        if self._probe is None:
            return {"screen_alive": None, "pid_alive": None, "running": None,
                    "reason": "PROBE_NOT_CONFIGURED"}
        return self._probe()

    def send_pause(self):
        if self._pause_sender is None:
            return False
        return bool(self._pause_sender())

    def send_restore(self):
        if self._restore_sender is None:
            return False
        return bool(self._restore_sender())

    def _send_ex(self, sender, bool_fn):
        """
        回傳 (send_outcome, detail)。

        🔴 bool 只能表達「成功／不成功」，無法表達第三種情形「送出了但不知道
           結果」。因此若 sender 具備 `last_send_outcome`（RemoteSenders 接上
           SshTransport 後就有），**以它為準**。
        🔴 一律透過 `bool_fn`（= self.send_pause / self.send_restore）呼叫，
           子類別覆寫 send_pause() 時仍然有效，且不會重複送出。
        """
        ok = bool(bool_fn())
        owner = getattr(sender, "__self__", None)
        outcome = getattr(owner, "last_send_outcome", None)
        if outcome in (SEND_SENT, SEND_NOT_SENT, SEND_UNKNOWN):
            res = getattr(owner, "last_result", None)
            return outcome, (getattr(res, "outcome", None) if res else None)
        return (SEND_SENT if ok else SEND_NOT_SENT), None

    def send_pause_ex(self):
        return self._send_ex(self._pause_sender, self.send_pause)

    def send_restore_ex(self):
        return self._send_ex(self._restore_sender, self.send_restore)


# ======================================================================
# G. Audit Log（append-only JSONL）
# ======================================================================
# 🔴 ATTEMPT-SCOPED JOURNAL（2026-10-01 Attempt #7 postmortem）
#    OnceGuard 的 one-shot 語意是「**同一 Attempt / 同一 handoff session**
#    內 pause / restore 各最多一次」，**不是**「整個 Phase 6 歷史期間只能一次」。
#    Attempt #7 因共用單一 `handoff_journal.jsonl`，讀到 Attempt #4 的
#    PAUSE INTENT 而永久拒送 -> `ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND`。
LEGACY_SHARED_JOURNAL_NAME = "handoff_journal.jsonl"
ATTEMPT_JOURNAL_PREFIX = "handoff_journal_"


def attempt_journal_path(base_dir, attempt_id):
    """
    由 `attempt_id` **決定性地**導出 journal 路徑。

    🔴 deterministic：同一 attempt_id -> 同一路徑（crash recovery 必須能
       在新 process 重新打開**同一個** journal）。
    🔴 attempt_id 缺失 / 空白 -> `ValueError`（Fail Closed）；
       **不得** fallback 回 legacy 共用 journal。
    """
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("attempt_id 必填且不得為空 —— 不得 fallback 回共用 journal")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_"
                   for c in attempt_id.strip())
    return os.path.join(base_dir, "%s%s.jsonl" % (ATTEMPT_JOURNAL_PREFIX, safe))


class Journal(object):
    """
    追加式稽核日誌 ＋ durable handoff state。

    🔴 **意圖先寫、結果後寫**
       任何有副作用的動作（pause / restore / dispatch）都先寫 intent，
       再執行，再寫 outcome。crash 落在中間時，我們知道「可能已發生」，
       因此**必須以觀測驗證**，而不是盲目重送。

    🔴 每筆都有單調遞增 seq；檔案只追加不改寫，crash 最多損失最後一行。

    🔴 **ATTEMPT-SCOPED**：正式 Attempt 一律以
       `attempt_journal_path(base_dir, attempt_id)` 取得專屬檔案，
       並把 `attempt_id` 寫進每一筆記錄（defense-in-depth）。
       舊的共用 `handoff_journal.jsonl` 保留為
       **LEGACY SHARED JOURNAL（含 Attempt #4 證據）**，immutable，不再寫入。
    """

    def __init__(self, path=DEFAULT_JOURNAL, clock=None, attempt_id=None):
        self.path = path
        self.attempt_id = attempt_id
        self._clock = clock or (lambda: datetime.datetime.now())
        self._seq = 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._seq = self._last_seq()

    def _last_seq(self):
        n = 0
        for rec in self.read_all():
            n = max(n, int(rec.get("seq") or 0))
        return n

    def append(self, kind, state=None, ownership=None, detail=None, **extra):
        self._seq += 1
        rec = {"seq": self._seq,
               "at": self._clock().strftime("%Y-%m-%d %H:%M:%S"),
               "kind": kind, "state": state, "ownership": ownership,
               "detail": detail}
        # 🔴 defense-in-depth：每筆都帶 attempt_id（主要隔離仍是 per-attempt 檔案）
        if self.attempt_id is not None:
            rec["attempt_id"] = self.attempt_id
        rec.update(extra)
        with io.open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return rec

    def read_all(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with io.open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    # crash 造成的半行：跳過，不讓整份 journal 失效
                    continue
        return out

    def last_state(self):
        """最後一次已知狀態（供 crash recovery）。"""
        st = None
        for rec in self.read_all():
            if rec.get("state"):
                st = rec["state"]
        return st


# ======================================================================
# D. Crash Recovery
# ======================================================================
R_CLEAN = "CLEAN"                       # 無未完成交接
R_PENDING_RESTORE = "PENDING_RESTORE"   # 我方暫停過 external，尚未恢復
R_CRITICAL = "CRITICAL"                 # 上次以 critical 收場
R_UNKNOWN = "UNKNOWN"                   # journal 損毀 / 無法判定


def _has_unresolved_pause(journal):
    """
    journal 中是否存在「已寫 PAUSE 意圖、但之後未完成歸還」的痕跡。

    🔴 判斷依據刻意寬鬆（寧可多疑）：只要出現過 PAUSE 的 INTENT，
       且其後沒有成功的 RESTORE OUTCOME，就視為可能仍背負責任。
    """
    paused = False
    for rec in journal.read_all():
        step, kind = rec.get("step"), rec.get("kind")
        if kind == "INTENT" and step == "PAUSE":
            paused = True
        elif kind == "OUTCOME" and step == "RESTORE" and rec.get("ok") is True:
            paused = False
    return paused


# 🔴 EXECUTION DEADLINE（2026-10-02 裁示）—— canonical 判定一律委派該模組，
#    本檔**不**自行解析 target_date / window_end。
import phase6_execution_deadline as _DL                        # noqa: E402


def _deadline_blocks(deadline, checkpoint):
    """
    回傳 (blocked, detail)。`deadline is None` -> 永不阻擋（向後相容）。

    🔴 SAFE CLEANUP 的 checkpoint（8 / 9）由 `_DL` 保證永遠放行，
       本函式不另加判斷，避免形成第二套規則。
    """
    allowed, verdict, detail = _DL.deadline_check(deadline, checkpoint)
    if allowed:
        return False, None
    return True, "%s @ %s：%s" % (verdict, checkpoint, detail)


def _permit_authorizes(permit):
    """
    permit 是否**授權**本次 FIRST LIVE dispatch。

    🔴 只問「有沒有授權」，不代表 dispatch 已被打開。
    🔴 任何無法確認的情況一律 False（Fail Closed）：
       None / 沒有 `authorizes()` / 呼叫拋例外 / 回傳非 True。
    """
    if permit is None:
        return False
    fn = getattr(permit, "authorizes", None)
    if not callable(fn):
        return False
    try:
        return fn() is True
    except Exception:                                          # noqa: BLE001
        return False


# 🔴 必須與 `phase6_first_live_handoff.EV_ENTRY_GATE_PASS` 完全一致。
#    本模組**不得** import phase6_first_live_handoff（會形成循環 import），
#    因此以字面值宣告，並由 regression 斷言兩者相等。
EV_ENTRY_GATE_PASS = "ENTRY_GATE_PASS"


def _entry_gate_authorizes(permit):
    """
    STAGE A：permit 是否允許**進入** dispatcher。回傳 (ok, why)。

    🔴 只呼叫 `permit.allows_dispatcher_entry()`（duck typing，避免循環 import）。
    🔴 Fail Closed：None / 沒有該方法 / 呼叫拋例外 / 回傳非 True 一律 False。
    🔴 **不得**改用 `authorizes()`（那是含 authority 的 STAGE B）。
    """
    if permit is None:
        return False, "未提供 permit"
    fn = getattr(permit, "allows_dispatcher_entry", None)
    if not callable(fn):
        return False, "permit 缺少 allows_dispatcher_entry()"
    try:
        ok = fn() is True
    except Exception as e:                                     # noqa: BLE001
        return False, "entry gate 評估例外：%s" % type(e).__name__
    if ok:
        return True, None
    return False, (getattr(permit, "entry_denied_reason", None)
                   or "entry 前置條件未全數成立")


def _note_permit(permit, event):
    """把 ordering 事件記到 permit（duck typing；permit 不支援時為 no-op）。"""
    if permit is None:
        return
    fn = getattr(permit, "note", None)
    if callable(fn):
        try:
            fn(event)
        except Exception:                                      # noqa: BLE001
            pass


def _resolve_external_running(remote_probe_result):
    """把 recovery evidence 正規化成 True / False / None。

    🔴 **不使用 `status`** —— 那是 QUERY_ONLY_WITH_STDIN_WRITE，
       recovery 證據只准用 PASSIVE_READ_ONLY 的 `probe` 與 `loopcheck`。
    🔴 兩種輸入皆接受，語意一致：
         legacy : {"running": True/False/None}        —— 呼叫端已自行判定
         新契約 : {"probe": RemoteProbe|dict,
                   "loop_marks": [...], "min_loop_observations": N}
       判定一律委派給 `phase6_remote_adapter.external_liveness`，
       **不在本模組另立第二套規則。**
    """
    if not isinstance(remote_probe_result, dict):
        return None
    running = remote_probe_result.get("running")
    if running is not None:
        return bool(running)
    if "probe" not in remote_probe_result:
        return None
    from phase6_remote_adapter import (external_liveness, EXT_RUNNING,
                                       EXT_NOT_RUNNING)
    verdict, _why = external_liveness(
        remote_probe_result.get("probe"),
        loop_active=remote_probe_result.get("loop_active"),
        loop_marks=remote_probe_result.get("loop_marks"),
        min_loop_observations=remote_probe_result.get(
            "min_loop_observations"))
    if verdict == EXT_RUNNING:
        return True
    if verdict == EXT_NOT_RUNNING:
        return False
    return None


def recover(journal, remote_probe_result=None):
    """
    啟動時的責任重建。回傳 (verdict, last_state, action)。

    🔴 **責任不因重開機而消失**：只要 journal 的最後狀態落在
       PENDING_RESTORE_STATES，就代表「我方暫停過 external 且未確認恢復」，
       必須先把 external 恢復，而不是直接開始新的交接。
    🔴 遠端實際狀態優先於 journal 推測 —— journal 說可能停了，
       但遠端證據顯示 external 仍在主動 loop，那就是已經恢復（或根本沒停成功）。
    🔴 `remote_probe_result` 的證據來源**只准** `probe` + `loopcheck`
       （兩者皆 PASSIVE_READ_ONLY）。判定委派給
       `phase6_remote_adapter.external_liveness`，語意與 RestoreVerifier 一致：
       runtime 健康 + loop 前進 = RUNNING；連不上 / loop 證據不足 = UNKNOWN。
    """
    last = journal.last_state()

    # 🔴 **不能只看 STATE 紀錄。**
    #    若 crash 恰好落在「pause INTENT 已寫入」與「任何 STATE 落盤」之間，
    #    journal 裡就只有一筆 INTENT —— 舊版 last_state() 會回 None，
    #    於是被判成 CLEAN，服務照常啟動，但 external 可能**已經被暫停**。
    #    因此：只要看到未完成的 PAUSE INTENT，就必須當成「可能已暫停」。
    #    （ABORTED_PAUSE_FAILED 例外 —— 那代表送出明確失敗，責任未成立。）
    unresolved_pause = _has_unresolved_pause(journal)
    if unresolved_pause and last not in (S_COMPLETE, S_RESTORED,
                                         S_ABORTED_PREFLIGHT,
                                         S_ABORTED_PAUSE_FAILED):
        running = _resolve_external_running(remote_probe_result)
        if running is True:
            return (R_CLEAN, last,
                    "有未完成的 pause 意圖，但遠端證據顯示 external 仍在主動 loop "
                    "→ 責任已解除，補記後可正常啟動")
        if running is False:
            return (R_PENDING_RESTORE, last,
                    "偵測到未完成的 pause 意圖且 external 已停 "
                    "→ 必須先恢復，不得開始新交接")
        return (R_UNKNOWN, last,
                "有未完成的 pause 意圖但無法取得遠端狀態 → Fail Closed，"
                "不得開始新交接")

    if last is None:
        return R_CLEAN, None, "無既往交接紀錄 → 可正常啟動"
    if last in CRITICAL_STATES:
        return R_CRITICAL, last, "上次以 CRITICAL 收場 → 需人工確認後才可再啟動"
    if last in PENDING_RESTORE_STATES:
        running = _resolve_external_running(remote_probe_result)
        if running is True:
            return (R_CLEAN, last,
                    "journal 顯示交接未完成，但遠端證據顯示 external 仍在主動 loop "
                    "→ 責任已解除，補記後可正常啟動")
        if running is False:
            return (R_PENDING_RESTORE, last,
                    "external 仍處於暫停 → 必須先恢復，不得開始新交接")
        return (R_UNKNOWN, last,
                "無法取得遠端狀態 → Fail Closed，不得開始新交接")
    return R_CLEAN, last, "上次交接已收斂"


# ======================================================================
# E. Watchdog
# ======================================================================
class Watchdog(object):
    """
    交接逾時保護。

    🔴 **不是「時間到就強制 restore」** —— 若 Phase 6 仍在 dispatch，
       此時恢復 external 會造成雙方同時控制（正是最高不變量禁止的事）。
       因此逾時只會：① 要求 Phase 6 先收斂 ② 收斂後才允許 restore。
    🔴 逾時上限由呼叫端提供，不硬編碼。
    """

    def __init__(self, max_paused_sec, clock=None):
        if max_paused_sec is None or float(max_paused_sec) <= 0:
            raise ValueError("max_paused_sec 必須為正數（不得預設為無限）")
        self.max_paused_sec = float(max_paused_sec)
        self._clock = clock or time.monotonic
        self._paused_at = None

    def mark_paused(self):
        self._paused_at = self._clock()

    def clear(self):
        self._paused_at = None

    def elapsed(self):
        return None if self._paused_at is None else self._clock() - self._paused_at

    def expired(self):
        e = self.elapsed()
        return e is not None and e > self.max_paused_sec

    def verdict(self, state):
        """回傳 (expired, required_action)。"""
        if not self.expired():
            return False, None
        if state in (S_PHASE6_ACTIVE, S_PHASE6_RELEASING):
            return True, "REQUIRE_PHASE6_RELEASE_FIRST"
        return True, "REQUIRE_RESTORE"


# ======================================================================
# F. Idempotency
# ======================================================================
class OnceGuard(object):
    """
    每個有副作用的步驟只允許執行一次。

    🔴 crash 後**不盲目重送**：`attempted` 會從 journal 重建，
       已嘗試過的步驟改走「以觀測驗證結果」而不是再送一次。
    """

    def __init__(self, journal=None):
        self.attempted = set()
        self.confirmed = set()
        self.scope_attempt_id = getattr(journal, "attempt_id", None)
        self.skipped_foreign = 0
        if journal is not None:
            for rec in journal.read_all():
                step = rec.get("step")
                if not step:
                    continue
                # 🔴 ATTEMPT SCOPE（2026-10-01 Attempt #7 postmortem）
                #    journal 已 per-attempt 分檔；此處再多一層防護：
                #    若本 journal 有 attempt_id，則只採用同一 attempt 的紀錄。
                #    **不得**把前次 Attempt 的 PAUSE/RESTORE 當成本次已嘗試。
                if self.scope_attempt_id is not None:
                    rid = rec.get("attempt_id")
                    if rid is not None and rid != self.scope_attempt_id:
                        self.skipped_foreign += 1
                        continue
                if rec.get("kind") == "INTENT":
                    self.attempted.add(step)
                elif rec.get("kind") == "OUTCOME":
                    self.confirmed.add(step)

    def may_attempt(self, step):
        return step not in self.attempted

    def mark_attempt(self, step):
        self.attempted.add(step)

    def mark_confirmed(self, step):
        self.confirmed.add(step)

    def needs_verification(self, step):
        """曾嘗試但無結果 → 必須以觀測確認，不得重送。"""
        return step in self.attempted and step not in self.confirmed


# ======================================================================
# Orchestrator
# ======================================================================
class HandoffOrchestrator(object):
    """
    Ownership 交接狀態機。**所有 I/O 由注入取得，可完全離線測試。**

    remote        : RemoteController
    observe       : () -> dict  Phase 6 側的 fresh 觀測
                    需含 authority / pcs_state / ac_kw / soc / tou /
                         meter_valid / meter_fresh / comm_ok / fault /
                         critical_alarms / decision
    gates         : (observation) -> (ok, dict)  進場條件評估（注入既有 precheck）
    dispatcher    : (observation) -> dict  真正的 FIRST LIVE / 充電流程
    """

    def __init__(self, remote, observe, gates, dispatcher=None,
                 journal=None, watchdog=None, mode=MODE_DRY_RUN,
                 idle_baseline_kw=None, power_tolerance_kw=None, clock=None):
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"未支援的模式：{mode!r}")
        self.remote = remote
        self.observe = observe
        self.gates = gates
        self.dispatcher = dispatcher
        self.journal = journal
        self.watchdog = watchdog
        self.mode = mode
        self.idle_baseline_kw = idle_baseline_kw
        self.power_tolerance_kw = power_tolerance_kw
        self._clock = clock or time.monotonic
        self.state = S_IDLE
        self.ownership = OWN_EXTERNAL
        self.history = [S_IDLE]
        self.guard = OnceGuard(journal)
        self.failure_reason = None

    # ---- 狀態轉移（白名單 ＋ 稽核）----
    def _to(self, new_state, detail=None, **extra):
        allowed = TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise RuntimeError(
                f"非法狀態轉移：{self.state} → {new_state}"
                f"（允許 {sorted(allowed)}）")
        self.state = new_state
        self.history.append(new_state)
        if new_state in CRITICAL_STATES and detail:
            self.failure_reason = detail
        if self.journal is not None:
            self.journal.append("STATE", state=new_state,
                                ownership=self.ownership, detail=detail, **extra)
        return new_state

    # ---- 不變量檢查 ----
    def check_ownership(self, external_running, phase6_holding):
        """
        以觀測事實檢查 ownership 不變量。
        BOTH → 立即 OWNERSHIP_CONFLICT（critical）。
        """
        own = classify_ownership(external_running, phase6_holding)
        self.ownership = own
        if own == OWN_BOTH:
            if self.journal is not None:
                self.journal.append("INVARIANT_VIOLATION", state=self.state,
                                    ownership=own,
                                    detail="external 與 Phase 6 同時持有控制權")
            return own, False
        acceptable = ACCEPTABLE_OWNERSHIP.get(self.state)
        if acceptable is None:
            return own, True
        return own, (own in acceptable)

    # ---- 步驟：pause ----
    def request_pause(self):
        """
        送出 external pause。**責任在送出前就以 journal 記下**。

        🔴 DRY_RUN / OBSERVE 一律不真的送 —— 回 (False, 原因)。
        """
        step = "PAUSE"
        if not self.guard.may_attempt(step):
            return False, "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"
        if self.mode != MODE_ARMED:
            return False, f"MODE_{self.mode}_WILL_NOT_SEND"
        if self.journal is not None:
            self.journal.append("INTENT", state=self.state, step=step,
                                detail=f"即將送出 external {PAUSE_COMMAND}")
        self.guard.mark_attempt(step)
        outcome, why2 = self.remote.send_pause_ex()
        ok = (outcome == SEND_SENT)
        if self.journal is not None:
            # 🔴 ok 三值：True / False / None(不確定)。不得把不確定寫成 False。
            self.journal.append(
                "OUTCOME", state=self.state, step=step,
                detail=f"send_pause -> {outcome}"
                       + (f" ({why2})" if why2 else ""),
                ok=(True if ok else (None if outcome == SEND_UNKNOWN else False)),
                send_outcome=outcome)
        if ok:
            self.guard.mark_confirmed(step)
            if self.watchdog is not None:
                self.watchdog.mark_paused()
        if outcome == SEND_UNKNOWN:
            # 保守：責任視為已成立，且必須先 probe / recover
            if self.watchdog is not None:
                self.watchdog.mark_paused()
            return False, "SEND_OUTCOME_UNKNOWN_MUST_PROBE"
        return ok, None

    def verify_pause(self, probe, observation):
        """
        pause 驗證 —— **看行為，不看宣稱**。回傳 (ok, checks)。
        """
        ac = observation.get("ac_kw")
        checks = {
            "process_alive": probe.get("pid_alive") is True,
            "screen_alive": probe.get("screen_alive") is True,
            # 🔴 **不是** 直接觀測到 running == False。
            #    auto_control.py 的 `running` 是行程內部變數，外部讀不到。
            #    因此改以外部可觀測事實證明它已不再控制 PCS —— 見
            #    phase6_remote_adapter.external_quiescent（多樣本）。
            #    這裡的單點版本只作為「明顯還在動」的快速否決。
            "external_quiescent": self._quiescent_hint(probe, observation),
            "power_converged": power_is_idle(ac, self.idle_baseline_kw,
                                             self.power_tolerance_kw),
            "pcs_legal_idle": observation.get("pcs_state") in ("STANDBY", "STOPPED"),
            "no_external_authority": observation.get("authority") != "EXTERNAL_OR_UNKNOWN",
            "comm_ok": observation.get("comm_ok") is True,
            "fault_clear": observation.get("fault") is False,
            "alarm_clear": observation.get("critical_alarms") == 0,
        }
        return all(checks.values()), checks

    def _quiescent_hint(self, probe, observation):
        """
        單點的「疑似靜止」判斷。**不足以作為 EXTERNAL_PAUSED 的依據** ——
        正式確認一律走 `external_quiescent(samples, ...)` 多樣本版本。

        probe 若提供了 `running`（例如離線測試的替身），採用之；
        真實環境讀不到內部變數時，退回以 PCS 行為判斷。
        """
        running = probe.get("running")
        if running is not None:
            return running is False
        return (observation.get("pcs_state") in ("STANDBY", "STOPPED")
                and observation.get("authority") != "EXTERNAL_OR_UNKNOWN")

    # ---- 步驟：restore ----
    def request_restore(self):
        """
        送出 external restore。**與 request_pause 對稱：ONE ATTEMPT ONLY。**

        🔴 `OnceGuard.attempted` 由 journal 的 INTENT 重建，因此 crash / 重啟
           之後也**不會盲目重送** —— 已嘗試過就只能以觀測驗證結果。
        🔴 `SEND_SENT` 與 `SEND_UNKNOWN` 皆**不得**自動重送；
           第二次呼叫一律擋下，由呼叫端走 RESTORE_FAILED_CRITICAL（責任保留）。
        """
        step = "RESTORE"
        if not self.guard.may_attempt(step):
            return False, "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"
        if self.mode != MODE_ARMED:
            return False, f"MODE_{self.mode}_WILL_NOT_SEND"
        if self.journal is not None:
            self.journal.append("INTENT", state=self.state, step=step,
                                detail=f"即將送出 external {RESTORE_COMMAND}")
        self.guard.mark_attempt(step)
        outcome, why2 = self.remote.send_restore_ex()
        ok = (outcome == SEND_SENT)
        if self.journal is not None:
            self.journal.append(
                "OUTCOME", state=self.state, step=step,
                detail=f"send_restore -> {outcome}"
                       + (f" ({why2})" if why2 else ""),
                ok=(True if ok else (None if outcome == SEND_UNKNOWN else False)),
                send_outcome=outcome)
        if ok:
            self.guard.mark_confirmed(step)
        if outcome == SEND_UNKNOWN:
            return False, "SEND_OUTCOME_UNKNOWN_MUST_PROBE"
        return ok, None

    def verify_restore(self, probe):
        """restore 驗證。回傳 (ok, checks)。"""
        checks = {
            "process_alive": probe.get("pid_alive") is True,
            "screen_alive": probe.get("screen_alive") is True,
            "controller_running": probe.get("running") is True,
        }
        return all(checks.values()), checks

    def snapshot(self):
        return {"state": self.state, "ownership": self.ownership,
                "mode": self.mode, "history": list(self.history),
                "failure_reason": self.failure_reason,
                "dispatch_enabled": DISPATCH_ENABLED,
                "watchdog_elapsed": (self.watchdog.elapsed()
                                     if self.watchdog else None)}


def handoff_complete_ok(phase6_released, external_runtime_healthy,
                        external_loop_resumed, ownership, critical):
    """
    交接是否算完成。回傳 (ok, checks)。

    🔴 **不要求 ownership == EXTERNAL**。恢復後的 external controller 可能
       依 policy 正確地選擇 idle，此時 ownership = NEITHER 仍屬完成。
       唯一絕對禁止的是 BOTH。
    """
    checks = {
        "phase6_ownership_released": phase6_released is True,
        "external_runtime_healthy": external_runtime_healthy is True,
        "external_loop_resumed": external_loop_resumed is True,
        "ownership_not_both": ownership != OWN_BOTH,
        "no_critical": not critical,
    }
    return all(checks.values()), checks


# ======================================================================
# FIRST LIVE Handoff Plan（2026-09-02 修正版）
# ======================================================================
# 🔴 最重要的修正：**PRE-PAUSE 階段不得要求 Authority=IDLE / PCS idle /
#    no external controller**。暫停之前，已知的 external controller 很可能
#    正在正常充電 —— 那是預期狀態，不是 failure。
#    這三項只有在 POST-PAUSE 的 fresh precheck 才要求。
FIRST_LIVE_HANDOFF_PLAN = """
 1 PRE_PAUSE_PREFLIGHT
     要求 : fresh SOC / Meter / PCS-BESS、TOU=OFF_PEAK、SOC<=85%、
            Natural Decision=CHARGE、comm healthy、fault/alarm clean、
            external controller screen/process/identity healthy、
            觀測到的 external ownership 與已知 auto_control.py 行為一致
     不要求: Authority=IDLE、PCS idle、no external controller
 2 PAUSE            external built-in "stop"
                    -> RESTORE RESPONSIBILITY = TRUE（自此不可清除）
 3 SETTLING         <= pause_settling_timeout_sec (120 s)
 4 STABLE_VERIFY    4 samples @ 15 s，span >= 45 s，全部成立：
                    PCS legal idle / power in [-3.0,+1.0] / Authority IDLE /
                    no external-control evidence / comm healthy /
                    fault-alarm clean / state stable
 5 OWNERSHIP        = NEITHER
 6 POST_PAUSE_FRESH_PRECHECK
                    重新取得全部 fresh data，**不得沿用 Step 1**。
                    只有此階段才要求 PCS idle / Authority IDLE /
                    no external controller
 7 FIRST_LIVE       CHARGE 5.0 kW
 8 VERIFY           Phase 6 authority / readback / LastControl
 9 RELEASE          Phase 6 STOP -> idle verified -> ownership = NEITHER
10 RESTORE          external built-in "start"
11 RESTORE_VERIFY   runtime health + loop resumed
                    3 observations @ 3 s，span >= 6 s
                    不要求 PCS 一定 CHARGE/DISCHARGE
12 COMPLETE         Phase6 ownership released AND external runtime healthy
                    AND external loop resumed AND ownership != BOTH
                    AND no critical
                    -> external policy 此刻 idle（ownership=NEITHER）仍算 PASS

任一步 FAIL -> Fail Closed。
Step 2 成功後，在 RESTORED 之前的任何失敗都必須保留 RESTORE RESPONSIBILITY。
"""


# ======================================================================
# Phase 6.10-B2：Pre-pause gates
# ======================================================================
# 🔴 **PRE-PAUSE 階段刻意不要求** Authority=IDLE / PCS idle / no external
#    controller。暫停之前，已知的 external controller 很可能正在正常充電 ——
#    那是預期狀態，不是 failure。把那三項放在這裡會讓交接永遠無法啟動。
#    它們只在 POST_PAUSE_FRESH_PRECHECK 才要求。
PRE_PAUSE_ITEMS = ("soc_fresh", "meter_fresh", "ess_fresh", "tou_off_peak",
                   "soc_in_charge_band", "decision_is_charge", "comm_ok",
                   "fault_clear", "alarm_clear", "external_runtime_healthy",
                   "external_behaviour_known")

# 🔴 這三項**不得**出現在 pre-pause：留成常數讓回歸可以直接驗。
PRE_PAUSE_MUST_NOT_REQUIRE = ("authority_idle", "pcs_idle",
                              "no_external_controller")


def pre_pause_gates(sample, probe, soc_max_pct):
    """
    Step 1 PRE_PAUSE_PREFLIGHT。回傳 (ok, checks)。

    soc_max_pct 必須由呼叫端提供（充電閂上限），不預設。
    """
    soc = sample.get("soc")
    checks = {
        "soc_fresh": sample.get("soc_fresh") is True,
        "meter_fresh": sample.get("meter_fresh") is True,
        "ess_fresh": sample.get("ess_fresh") is True,
        "tou_off_peak": sample.get("tou") == "OFF_PEAK",
        "soc_in_charge_band": (soc_max_pct is not None
                               and isinstance(soc, (int, float))
                               and float(soc) <= float(soc_max_pct)),
        "decision_is_charge": sample.get("decision") == "charge",
        "comm_ok": sample.get("comm_ok") is True,
        "fault_clear": sample.get("fault") is False,
        "alarm_clear": sample.get("critical_alarms") == 0,
        "external_runtime_healthy": (probe.get("screen_alive") is True
                                     and probe.get("process_alive") is True
                                     and probe.get("process_identity_ok") is True),
        "external_behaviour_known": sample.get("external_behaviour_known") is True,
    }
    return all(checks.values()), checks


def post_pause_precheck(sample, probe):
    """
    Step 6 POST_PAUSE_FRESH_PRECHECK。**只有這一階段**才要求那三項。

    🔴 必須使用暫停後重新取得的 fresh data，不得沿用 Step 1。
    """
    checks = {
        "fresh": sample.get("fresh") is not False,
        "pcs_idle": sample.get("pcs_state") in ("STANDBY", "STOPPED"),
        "authority_idle": sample.get("authority") == "IDLE",
        "no_external_controller": sample.get("authority") != "EXTERNAL_OR_UNKNOWN",
        "comm_ok": sample.get("comm_ok") is True,
        "fault_clear": sample.get("fault") is False,
        "alarm_clear": sample.get("critical_alarms") == 0,
        "external_runtime_healthy": (probe.get("screen_alive") is True
                                     and probe.get("process_alive") is True),
    }
    return all(checks.values()), checks


# ======================================================================
# Phase 6.10-B2：HandoffRunner —— 12 步驟驅動
# ======================================================================
class HandoffRunner(object):
    """
    依 FIRST_LIVE_HANDOFF_PLAN 驅動整個交接。**全部相依由注入取得。**

    🔴 `MODE_DRY_RUN`（預設）下 remote sender 與 dispatcher **一次都不會被呼叫**
       —— 不是靠旗標判斷，而是 orchestrator 的 request_pause / request_restore
       本身就在非 ARMED 模式直接回 False。
    🔴 dispatch 另需 `DISPATCH_ENABLED` 為 True；目前恆為 False，
       因此 Step 7 在任何模式下都不可能真的送出 CHARGE。
    🔴 Step 2 成功後，之後任一失敗都必須走 restore —— 由 `_bail()` 統一處理，
       不存在「直接 return 而忘了歸還」的路徑。
    """

    def __init__(self, orchestrator, sample_source, probe_source,
                 loop_mark_source=None, dispatcher=None, config=None,
                 soc_max_pct=None, sleeper=None, dispatch_permit=None,
                 deadline=None):
        self.o = orchestrator
        self.sample_source = sample_source      # () -> dict（每次都要 fresh）
        self.probe_source = probe_source        # () -> dict
        self.loop_mark_source = loop_mark_source  # () -> str|None
        self.dispatcher = dispatcher            # (sample) -> dict
        self.cfg = config or HandoffConfig
        self.soc_max_pct = soc_max_pct
        self._sleep = sleeper or (lambda s: None)
        self.steps = []
        self.restore_responsibility = False
        # 🔴 Attempt #4 postmortem：FIRST LIVE dispatch 授權憑證。
        #    None = 沒有任何已授權的 one-shot -> step 7 只看模組層
        #    `DISPATCH_ENABLED`（rest state False）-> 結構上不可能 dispatch。
        self.dispatch_permit = dispatch_permit
        # 🔴 EXECUTION DEADLINE（2026-10-02 裁示）
        #    `window_end` = NO-NEW-RISK-ACTION DEADLINE，不是 process kill。
        #    None = 未注入 -> 行為與導入本契約之前完全相同（向後相容）。
        self.deadline = deadline
        self.deadline_blocked_at = None
        # 🔴 EVIDENCE WIRING：verifier / ownership / post-check 的 canonical
        #    結果必須外露給 artifact，不得讓它們停在區域變數而被記成 None。
        self.pause_verifier_state = None
        self.normal_loop_halted = None
        self.pause_loop_marks = []
        self.restore_verifier_state = None
        self.release_before_restore = None
        self.post_check = None

    def _log(self, step, ok, detail=None, **extra):
        rec = {"step": step, "ok": bool(ok), "detail": detail}
        rec.update(extra)
        self.steps.append(rec)
        if self.o.journal is not None:
            self.o.journal.append("PLAN_STEP", state=self.o.state,
                                  ownership=self.o.ownership,
                                  detail=f"{step}: {'OK' if ok else 'FAIL'}"
                                         + (f" - {detail}" if detail else ""))
        return ok

    def _bail(self, step, detail):
        """
        失敗收斂。🔴 若已背負恢復責任，**一律**先嘗試歸還再結束。
        """
        self._log(step, False, detail)
        if not self.restore_responsibility:
            if self.o.state == S_PREFLIGHT:
                self.o._to(S_ABORTED_PREFLIGHT, detail=detail)
            return self._result("ABORTED", detail)
        return self._restore_phase(reason=f"因 {step} 失敗而歸還：{detail}")

    def _result(self, outcome, detail=None):
        return {"outcome": outcome, "detail": detail,
                "state": self.o.state, "ownership": self.o.ownership,
                "restore_responsibility": self.restore_responsibility,
                "steps": list(self.steps),
                "mode": self.o.mode, "dispatch_enabled": DISPATCH_ENABLED,
                # 🔴 canonical execution evidence（不重新推算，只是外露）
                "pause_verifier_state": self.pause_verifier_state,
                "normal_loop_halted": self.normal_loop_halted,
                "pause_loop_marks": list(self.pause_loop_marks),
                "restore_verifier_state": self.restore_verifier_state,
                "release_before_restore": self.release_before_restore,
                "post_check": self.post_check,
                # 🔴 EXECUTION DEADLINE evidence（只外露，不重新判定）
                "deadline_blocked_at": self.deadline_blocked_at,
                "deadline": (self.deadline.as_dict()
                             if self.deadline is not None else None)}

    # ---- 主流程 ----
    def run(self):
        from phase6_remote_adapter import PauseVerifier, RestoreVerifier,             PV_VERIFIED, RV_RESUMED

        # 1 PRE_PAUSE_PREFLIGHT
        self.o._to(S_PREFLIGHT)
        smp, prb = self.sample_source(), self.probe_source()
        ok, checks = pre_pause_gates(smp, prb, self.soc_max_pct)
        if not ok:
            bad = [k for k, v in checks.items() if not v]
            return self._bail("1_PRE_PAUSE_PREFLIGHT", f"未通過：{bad}")
        self._log("1_PRE_PAUSE_PREFLIGHT", True)

        # 🔴 DEADLINE CHECKPOINT 1 —— before PAUSE
        #    CASE A：external 尚未 pause。到期即直接結束，
        #    PAUSE / authority / CHARGE / DISCHARGE 全部 0。
        blocked, why_dl = _deadline_blocks(self.deadline,
                                           _DL.CK_BEFORE_PAUSE)
        if blocked:
            self.deadline_blocked_at = _DL.CK_BEFORE_PAUSE
            return self._bail("2_PAUSE", why_dl)

        # 2 PAUSE
        self.o._to(S_PAUSE_REQUESTED)
        sent, why = self.o.request_pause()
        if not sent and why == "SEND_OUTCOME_UNKNOWN_MUST_PROBE":
            # 🔴 SSH command timeout：external **可能已經被暫停**。
            #    不得判成 ABORTED_PAUSE_FAILED（那代表責任未成立），
            #    也不得重送 —— 一律進入不確定狀態並保留恢復責任，
            #    交給 recover() / probe 決定下一步。
            self.restore_responsibility = True
            self._log("2_PAUSE", False, why, uncertain=True)
            self.o._to(S_PAUSE_OUTCOME_UNKNOWN, detail=why)
            return self._result("PAUSE_OUTCOME_UNKNOWN", why)
        if not sent:
            # DRY_RUN / OBSERVE 走到這裡是**預期**行為，不是失敗
            self._log("2_PAUSE", False, why)
            self.o._to(S_ABORTED_PAUSE_FAILED, detail=why)
            return self._result("NOT_SENT", why)
        self.restore_responsibility = True          # 🔴 自此不可清除
        self._log("2_PAUSE", True, "已送出，恢復責任成立")

        # 🔴 DEADLINE CHECKPOINT 2 —— pause responsibility 已成立
        #    CASE B：不得再前進到 live leg；但**必須**走完
        #    restore responsibility handling（`_bail` 會轉 `_restore_phase`）。
        blocked, why_dl = _deadline_blocks(
            self.deadline, _DL.CK_AFTER_PAUSE_RESPONSIBILITY)
        if blocked:
            self.deadline_blocked_at = _DL.CK_AFTER_PAUSE_RESPONSIBILITY
            return self._bail("3_4_PAUSE_VERIFY", why_dl)

        # 3+4 SETTLING → STABLE_VERIFY
        self.o._to(S_PAUSE_VERIFYING)
        pv = PauseVerifier(self.cfg.idle_power_lower_kw,
                           self.cfg.idle_power_upper_kw,
                           self.cfg.stable_verify_min_samples,
                           self.cfg.pause_settling_timeout_sec)
        n = 0
        while pv.state not in (PV_VERIFIED,) and n < 200:
            n += 1
            st = pv.feed(self.sample_source(), self.probe_source())
            # 🔴 loop mark 與 PauseVerifier 取樣同步收集 —— halt 判定沿用
            #    canonical `loop_active_from_marks`，**不新增規則**。
            if self.loop_mark_source is not None:
                self.pause_loop_marks.append(self.loop_mark_source())
            self.pause_verifier_state = pv.state
            if st == PV_VERIFIED:
                break
            if st not in ("SETTLING", "STABLE_VERIFY"):
                return self._bail("3_4_PAUSE_VERIFY", f"{st}: {pv.reason}")
            self._sleep(self.cfg.stable_verify_interval_sec)
        self.pause_verifier_state = pv.state
        if self.loop_mark_source is not None:
            from phase6_remote_adapter import loop_active_from_marks
            act = loop_active_from_marks(
                self.pause_loop_marks, self.cfg.restore_loop_min_observations)
            # loop 仍在前進 -> 未 halt；未前進 -> halted；證據不足 -> None
            self.normal_loop_halted = (None if act is None else (not act))
        if pv.state != PV_VERIFIED:
            return self._bail("3_4_PAUSE_VERIFY", "未於允許次數內完成驗證")
        self._log("3_4_PAUSE_VERIFY", True, f"external_quiescent（{n} 筆）")

        # 5 OWNERSHIP = NEITHER
        self.o._to(S_EXTERNAL_PAUSED)
        own, valid = self.o.check_ownership(external_running=False,
                                            phase6_holding=False)
        if own != OWN_NEITHER or not valid:
            return self._bail("5_OWNERSHIP", f"ownership={own}")
        self._log("5_OWNERSHIP", True, own)

        # 🔴 DEADLINE CHECKPOINT 3 —— before GATE B / post-pause continuation
        blocked, why_dl = _deadline_blocks(self.deadline,
                                           _DL.CK_BEFORE_GATE_B)
        if blocked:
            self.deadline_blocked_at = _DL.CK_BEFORE_GATE_B
            return self._bail("6_POST_PAUSE_PRECHECK", why_dl)

        # 6 POST_PAUSE_FRESH_PRECHECK（不得沿用 Step 1）
        smp2, prb2 = self.sample_source(), self.probe_source()
        ok2, checks2 = post_pause_precheck(smp2, prb2)
        self.post_check = dict(checks2)
        if not ok2:
            bad2 = [k for k, v in checks2.items() if not v]
            return self._bail("6_POST_PAUSE_PRECHECK", f"未通過：{bad2}")
        self._log("6_POST_PAUSE_PRECHECK", True)

        # 7 FIRST LIVE
        # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-24 Attempt #4 postmortem）
        #   OLD : `if not DISPATCH_ENABLED: bail` —— 模組層旗標恆為 False，
        #         因此 5 kW live leg **結構上永遠不可能**被送出。
        #   NEW : 另接受一張**已授權的 one-shot FIRST LIVE permit**。
        #         permit 只代表「存在一個經使用者明確 field 授權的 attempt」，
        #         它**不會**打開 dispatch；真正的 `DISPATCH_ENABLED = True`
        #         只發生在 authority acquired 之後、緊貼 live leg 的
        #         `first_live_dispatch_scope()` 內，且 finally 無條件關閉。
        #   WHY : 使用者 2026-09-24 裁示 PART A —— DISPATCH_ENABLED 語意由
        #         「永久 False」改為「AT REST = False，僅在已授權 one-shot 的
        #         live-leg scope 內 temporarily True」。
        #   EVIDENCE : Attempt #4 於 7_FIRST_LIVE 中止，detail =
        #         "DISPATCH_ENABLED=False → 不得 dispatch"；其前的
        #         PRE_PAUSE 11/11、pause SENT、PauseVerifier PASS、
        #         ownership NEITHER、GATE B PASS 全部通過。
        #   🔴 未提供 permit 時行為與舊版**完全相同**（Fail Closed）。
        # 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-02 Attempt #8 postmortem）
        #   OLD : `if not DISPATCH_ENABLED and not _permit_authorizes(permit)`
        #         -> 兩個問題：
        #         (a) 要求**完整 16 項** permit（含 authority 兩項）才准進入
        #             dispatcher，但 authority 只能在 dispatcher 內取得
        #             -> CONTROL-FLOW CYCLE，production path 永遠過不了。
        #         (b) 把 `DISPATCH_ENABLED == True` 當成「可以放行」，
        #             但進入 dispatcher 前的正確狀態**本來就是 False**。
        #   NEW : 兩段式。
        #         ① REST INVARIANT：進入前 `DISPATCH_ENABLED` 必須是 False。
        #            已經是 True -> 代表有人在 scope 外打開過 -> Fail Closed。
        #         ② STAGE A ENTRY GATE：只問「是否允許進入 dispatcher」，
        #            使用的 evidence **不含** authority 兩項。
        #         完整 16 項 permit 仍在 dispatcher 內的
        #         `first_live_dispatch_scope()` 重新評估（STAGE B，未放寬）。
        #   WHY : 使用者 2026-10-02 裁示 —— STAGE A / STAGE B 必須分離，
        #         且「進入 dispatcher 前 DISPATCH_ENABLED=False」是
        #         **安全 invariant**，不是 failure condition。
        #   EVIDENCE : Attempt #8 artifact ——
        #         dispatch_permit.state = PERMIT_NOT_ENTERED，
        #         denied_reason = ["authority_acquire_attempted",
        #                          "authority_acquired"]，
        #         其餘 15 項全 true，handoff_abort_step = 7_FIRST_LIVE，
        #         authority_acquire_attempted = false，CHARGE = 0。
        if DISPATCH_ENABLED:
            return self._bail(
                "7_FIRST_LIVE",
                "進入 dispatcher 前 DISPATCH_ENABLED 已是 True "
                "-> 非預期 rest state，Fail Closed")
        entry_ok, entry_why = _entry_gate_authorizes(self.dispatch_permit)
        if not entry_ok:
            return self._bail("7_FIRST_LIVE",
                              "DISPATCHER ENTRY GATE DENIED：%s" % (entry_why,))
        _note_permit(self.dispatch_permit, EV_ENTRY_GATE_PASS)
        # 🔴 DEADLINE CHECKPOINT 4 —— before authority acquire
        #    CASE B / C：entry gate 即使 PASS，到期也不得 acquire authority。
        blocked, why_dl = _deadline_blocks(
            self.deadline, _DL.CK_BEFORE_AUTHORITY_ACQUIRE)
        if blocked:
            self.deadline_blocked_at = _DL.CK_BEFORE_AUTHORITY_ACQUIRE
            return self._bail("7_FIRST_LIVE", why_dl)
        if self.dispatcher is None:
            return self._bail("7_FIRST_LIVE", "未注入 dispatcher")
        self.o._to(S_PHASE6_ACTIVE)
        res = self.dispatcher(smp2)
        if not (res or {}).get("dispatched"):
            return self._bail("7_FIRST_LIVE", f"未送出：{(res or {}).get('reason')}")
        self._log("7_FIRST_LIVE", True, res)

        # 8 VERIFY
        if not (res or {}).get("verified"):
            return self._bail("8_VERIFY", "ReadBack / LastControl 未通過")
        self._log("8_VERIFY", True)

        # 9 RELEASE
        self.o._to(S_PHASE6_RELEASING)
        if not (res or {}).get("released"):
            return self._bail("9_RELEASE", "Phase 6 未收斂")
        self.o._to(S_PHASE6_RELEASED)
        self._log("9_RELEASE", True)
        # 🔴 Phase 6 release 已完成，且尚未進入 restore -> 順序成立
        self.release_before_restore = True

        # 10~12
        return self._restore_phase(reason="正常收尾")

    # ---- 10~12：歸還與驗證 ----
    def _restore_phase(self, reason):
        from phase6_remote_adapter import RestoreVerifier, RV_RESUMED, RV_UNKNOWN

        # DEADLINE CHECKPOINT 9 - before final restore completion path.
        # SAFE CLEANUP: must NEVER be blocked by the deadline. We only
        # record the observation so the artifact can prove cleanup was
        # allowed to finish even after window_end.
        _DL.deadline_check(self.deadline, _DL.CK_BEFORE_RESTORE_COMPLETION)

        # 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-02 EXECUTION DEADLINE 裁示）
        #   OLD : `release_before_restore` 只在**正常** step 9 被設為 True；
        #         走 `_bail()` 的路徑恆為 None，即使 authority 確實已釋放。
        #   NEW : 進入 restore 之前，若該欄位仍是 None 且 authority 曾被
        #         嘗試取得，就以**當下可觀測的 lease 狀態**補記。
        #   WHY : CASE C（authority 已取得、CHARGE 前到期）要求
        #         「release authority -> RESTORE external」，
        #         這個順序必須能從 artifact 證明，不能只靠推論。
        #   EVIDENCE : D4 —— dispatcher 的 `finally` 已 release，
        #         `_restore_phase` 此刻 `lease.held is False`，
        #         但舊欄位仍是 None。
        #   🔴 這是**補記觀測事實**，不是放寬：authority 從未被嘗試取得時
        #      維持 None（沒有順序可言），仍不得偽稱 True。
        if self.release_before_restore is None:
            _lease = getattr(self.dispatcher, "lease", None)
            if _lease is not None and getattr(
                    _lease, "acquire_attempted", False) is True:
                self.release_before_restore = (
                    getattr(_lease, "held", True) is False)

        if self.o.state not in (S_PHASE6_RELEASED, S_RESTORE_REQUESTED):
            # 從任何持有責任的狀態都要能走到歸還
            try:
                self.o._to(S_RESTORE_REQUESTED, detail=reason)
            except RuntimeError:
                self.o.state = S_RESTORE_REQUESTED
                self.o.history.append(S_RESTORE_REQUESTED)
        else:
            self.o._to(S_RESTORE_REQUESTED, detail=reason)

        sent, why = self.o.request_restore()
        if not sent and why == "SEND_OUTCOME_UNKNOWN_MUST_PROBE":
            # 🔴 restore 逾時同樣不得斷定失敗、更不得直接重送。
            #    恢復責任**保留**，狀態進入不確定，等 probe / recover。
            self._log("10_RESTORE", False, why, uncertain=True)
            self.o._to(S_RESTORE_OUTCOME_UNKNOWN, detail=why)
            return self._result("RESTORE_OUTCOME_UNKNOWN", why)
        if not sent:
            self._log("10_RESTORE", False, why)
            self.o._to(S_RESTORE_FAILED, detail=f"restore 未送出：{why}")
            return self._result("RESTORE_FAILED_CRITICAL", why)
        self._log("10_RESTORE", True)

        self.o._to(S_RESTORE_VERIFYING)
        rv = RestoreVerifier(self.cfg.restore_loop_min_observations,
                             self.cfg.pause_settling_timeout_sec)
        st = None
        for _ in range(self.cfg.restore_loop_min_observations + 3):
            prb = self.probe_source()
            from phase6_remote_adapter import RemoteProbe
            p = prb if isinstance(prb, RemoteProbe) else RemoteProbe(
                ssh_ok=True, screen_alive=prb.get("screen_alive"),
                process_alive=prb.get("process_alive"),
                process_identity_ok=prb.get("process_identity_ok"))
            mark = self.loop_mark_source() if self.loop_mark_source else None
            st = rv.feed(p, mark)
            self.restore_verifier_state = st
            if st in (RV_RESUMED, "FAILED", RV_UNKNOWN):
                break
            self._sleep(self.cfg.restore_loop_interval_sec)
        self.restore_verifier_state = st
        if st != RV_RESUMED:
            self._log("11_RESTORE_VERIFY", False, f"{st}: {rv.reason}")
            self.o._to(S_RESTORE_FAILED, detail=f"restore 驗證未通過：{st}")
            return self._result("RESTORE_FAILED_CRITICAL", st)
        self._log("11_RESTORE_VERIFY", True)
        self.restore_responsibility = False
        self.o._to(S_RESTORED)

        # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-24 Attempt #4 postmortem）
        #   OLD : `external_running=None` -> ownership 恆為 UNKNOWN，
        #         即使 RestoreVerifier 已判定 RV_RESUMED。
        #   NEW : 走到這一行的**前提**就是 `st == RV_RESUMED`
        #         （不是的話上面已 return），而 RV_RESUMED 的 canonical 語意
        #         就是「external runtime 健康且 loop 已恢復前進」= running。
        #   WHY : Attempt #4 restore 成功、現場實測已回到 EXTERNAL_RUNNING，
        #         artifact 卻記成 final_ownership = UNKNOWN，證據與事實不符。
        #   EVIDENCE : Attempt #4 artifact final_ownership='UNKNOWN' 對比
        #         handoff journal 11_RESTORE_VERIFY OK / STATE RESTORED，
        #         以及 2026-09-24 08:56 唯讀複驗 EXTERNAL_RUNNING。
        #   🔴 這是**收緊證據精確度**，不是放寬任何閘門。
        own, _ = self.o.check_ownership(external_running=True,
                                        phase6_holding=False)
        ok, checks = handoff_complete_ok(
            phase6_released=True, external_runtime_healthy=True,
            external_loop_resumed=True, ownership=self.o.ownership,
            critical=False)
        if not ok:
            self._log("12_COMPLETE", False, checks)
            return self._result("INCOMPLETE", checks)
        self.o._to(S_COMPLETE)
        self._log("12_COMPLETE", True, reason)
        return self._result("COMPLETE", reason)


# ======================================================================
# H. Service lifecycle（設計登記，本階段不啟動）
# ======================================================================
LIFECYCLE = """
啟動   → recover(journal, remote.probe())
         · CLEAN            → 可進入正常迴圈
         · PENDING_RESTORE  → 先恢復 external，恢復完成前不得開始新交接
         · CRITICAL         → 拒絕啟動，等待人工確認
         · UNKNOWN          → 拒絕啟動（Fail Closed）
迴圈   → fresh observe → gates 評估 → 條件成立才進入交接
關閉   → 若持有 restore 責任：先完成 restore 再結束；
         無法完成則寫入 RESTORE_FAILED_CRITICAL，**不得靜默結束**
"""


def main(argv=None):
    """離線自述：印出設計摘要與目前不變量，不做任何 I/O 以外的事。"""
    print("=" * 72)
    print("  Phase 6.10 Unattended Ownership Handoff —— 設計摘要（離線）")
    print("=" * 72)
    print(f"  DISPATCH_ENABLED : {DISPATCH_ENABLED}（FIRST LIVE 完成前恆為 False）")
    print(f"  預設模式          : {MODE_DRY_RUN}")
    print(f"  狀態數            : {len(TRANSITIONS)}   終態 {len(TERMINAL_STATES)}"
          f"   critical {len(CRITICAL_STATES)}")
    print(f"  背負 restore 責任的狀態 : {len(PENDING_RESTORE_STATES)} 個")
    print(f"  pause / restore   : {PAUSE_COMMAND!r} / {RESTORE_COMMAND!r}"
          f"（screen -X stuff，非互動、不 attach）")
    print(LIFECYCLE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
