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

# 這些狀態代表「我方已暫停 external，尚未恢復」→ 背負 restore 責任
PENDING_RESTORE_STATES = frozenset({
    S_PAUSE_REQUESTED, S_PAUSE_VERIFYING, S_EXTERNAL_PAUSED,
    S_PHASE6_ACTIVE, S_PHASE6_RELEASING, S_PHASE6_RELEASED,
    S_RESTORE_REQUESTED, S_RESTORE_VERIFYING,
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
                        S_RESTORE_REQUESTED},
    S_PAUSE_VERIFYING: {S_EXTERNAL_PAUSED, S_RESTORE_REQUESTED,
                        S_ABORTED_PAUSE_FAILED, S_OWNERSHIP_CONFLICT},
    S_EXTERNAL_PAUSED: {S_PHASE6_ACTIVE, S_RESTORE_REQUESTED,
                        S_OWNERSHIP_CONFLICT},
    S_PHASE6_ACTIVE: {S_PHASE6_RELEASING, S_OWNERSHIP_CONFLICT},
    S_PHASE6_RELEASING: {S_PHASE6_RELEASED, S_OWNERSHIP_CONFLICT},
    S_PHASE6_RELEASED: {S_RESTORE_REQUESTED},
    S_RESTORE_REQUESTED: {S_RESTORE_VERIFYING, S_RESTORE_FAILED},
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

# 每個狀態下「應該」由誰持有 —— 用於偵測不變量破壞
EXPECTED_OWNERSHIP = {
    S_IDLE: OWN_EXTERNAL,
    S_PREFLIGHT: OWN_EXTERNAL,
    S_PAUSE_REQUESTED: OWN_UNKNOWN,       # 轉移中，暫時不可知
    S_PAUSE_VERIFYING: OWN_UNKNOWN,
    S_EXTERNAL_PAUSED: OWN_NEITHER,
    S_PHASE6_ACTIVE: OWN_PHASE6,
    S_PHASE6_RELEASING: OWN_PHASE6,
    S_PHASE6_RELEASED: OWN_NEITHER,
    S_RESTORE_REQUESTED: OWN_NEITHER,
    S_RESTORE_VERIFYING: OWN_UNKNOWN,
    S_RESTORED: OWN_EXTERNAL,
    S_COMPLETE: OWN_EXTERNAL,
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


# ======================================================================
# G. Audit Log（append-only JSONL）
# ======================================================================
class Journal(object):
    """
    追加式稽核日誌 ＋ durable handoff state。

    🔴 **意圖先寫、結果後寫**
       任何有副作用的動作（pause / restore / dispatch）都先寫 intent，
       再執行，再寫 outcome。crash 落在中間時，我們知道「可能已發生」，
       因此**必須以觀測驗證**，而不是盲目重送。

    🔴 每筆都有單調遞增 seq；檔案只追加不改寫，crash 最多損失最後一行。
    """

    def __init__(self, path=DEFAULT_JOURNAL, clock=None):
        self.path = path
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


def recover(journal, remote_probe_result=None):
    """
    啟動時的責任重建。回傳 (verdict, last_state, action)。

    🔴 **責任不因重開機而消失**：只要 journal 的最後狀態落在
       PENDING_RESTORE_STATES，就代表「我方暫停過 external 且未確認恢復」，
       必須先把 external 恢復，而不是直接開始新的交接。
    🔴 遠端實際狀態優先於 journal 推測 —— journal 說可能停了，
       但遠端 probe 顯示 running=True，那就是已經恢復（或根本沒停成功）。
    """
    last = journal.last_state()
    if last is None:
        return R_CLEAN, None, "無既往交接紀錄 → 可正常啟動"
    if last in CRITICAL_STATES:
        return R_CRITICAL, last, "上次以 CRITICAL 收場 → 需人工確認後才可再啟動"
    if last in PENDING_RESTORE_STATES:
        running = (remote_probe_result or {}).get("running")
        if running is True:
            return (R_CLEAN, last,
                    "journal 顯示交接未完成，但遠端 probe 顯示 external 仍在運行 "
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
        if journal is not None:
            for rec in journal.read_all():
                step = rec.get("step")
                if not step:
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
        expected = EXPECTED_OWNERSHIP.get(self.state)
        if expected in (None, OWN_UNKNOWN):
            return own, True
        return own, (own == expected or own == OWN_UNKNOWN)

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
        ok = self.remote.send_pause()
        if self.journal is not None:
            self.journal.append("OUTCOME", state=self.state, step=step,
                                detail=f"send_pause -> {ok}", ok=ok)
        if ok:
            self.guard.mark_confirmed(step)
            if self.watchdog is not None:
                self.watchdog.mark_paused()
        return ok, None

    def verify_pause(self, probe, observation):
        """
        pause 驗證 —— **看行為，不看宣稱**。回傳 (ok, checks)。
        """
        ac = observation.get("ac_kw")
        checks = {
            "process_alive": probe.get("pid_alive") is True,
            "screen_alive": probe.get("screen_alive") is True,
            "controller_not_running": probe.get("running") is False,
            "power_converged": power_is_idle(ac, self.idle_baseline_kw,
                                             self.power_tolerance_kw),
            "pcs_legal_idle": observation.get("pcs_state") in ("STANDBY", "STOPPED"),
            "no_external_authority": observation.get("authority") != "EXTERNAL_OR_UNKNOWN",
            "comm_ok": observation.get("comm_ok") is True,
            "fault_clear": observation.get("fault") is False,
            "alarm_clear": observation.get("critical_alarms") == 0,
        }
        return all(checks.values()), checks

    # ---- 步驟：restore ----
    def request_restore(self):
        step = "RESTORE"
        if self.mode != MODE_ARMED:
            return False, f"MODE_{self.mode}_WILL_NOT_SEND"
        if self.journal is not None:
            self.journal.append("INTENT", state=self.state, step=step,
                                detail=f"即將送出 external {RESTORE_COMMAND}")
        self.guard.mark_attempt(step)
        ok = self.remote.send_restore()
        if self.journal is not None:
            self.journal.append("OUTCOME", state=self.state, step=step,
                                detail=f"send_restore -> {ok}", ok=ok)
        if ok:
            self.guard.mark_confirmed(step)
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
