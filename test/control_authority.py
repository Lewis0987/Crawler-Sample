# -*- coding: utf-8 -*-
"""
Phase 6.5-H Control Authority / Command Arbitration —— 純邏輯、零 I/O
======================================================================
回答**唯一**一個問題：

    「Phase 6 現在是否有權對 PCS 發送一般控制命令？」

這與另外兩層嚴格分離：

    Decision Engine   「系統想做什麼？」
    Safety Gate       「設備安全條件是否允許？」（既有 14 項，本模組不碰）
    Control Authority 「Phase 6 現在是否有控制權？」   ← 本模組

🔴 為什麼需要這一層（Phase 6.5-G 實機證據）
    現場觀測到 PCS 正被外部以約 80 kW 放電時，對 Phase 6 的 DISCHARGE 5 kW
    做 Safety Gate 試算，14 項**全部 PASS**、allowed=True / SAFE_OK。
    原因是 Gate 不消費 pcs_state，而 `manual_switch=1` 只代表「允許手動控制」，
    不代表「目前沒有人在控制」。若自動控制此刻上線，會直接把 80 kW 覆寫成 5 kW。
    這不是設備安全失敗，因此不該由 Safety Gate 表達，需要獨立的控制權層。

⚠️ 命名：本專案 Phase 4~5 已有 `report_monitor.acquire_ownership()`，那是
   Windows 具名 Mutex 的**本機行程互斥**（誰有資格驅動 Auto Report），與本模組
   完全無關。本概念一律稱 **Control Authority**，不得稱 ownership。

零 I/O 保證
    不讀 API、不讀檔、不送控制、不 import device_control_operator、
    不 import pcs_control_integration（避免循環）、不修改 Safety Gate /
    ReadBackVerifier / LastControlStore。所有輸入由呼叫端注入。

🔴 Fail Closed
    只有 IDLE 與 OWNED_BY_PHASE6 允許送出一般控制命令。
    EXTERNAL_OR_UNKNOWN / CONFLICT / UNKNOWN 一律 BLOCK。
    判定過程任何例外 → UNKNOWN → BLOCK（絕不 `except: allow`）。

🔴 本階段（Fail-Closed Baseline）政策參數皆為 None
    authority_ttl_sec = None            尚無實測依據
    authority_power_tolerance_kw = None 尚無實測依據
    後果（**這是預期行為**）：
        PCS 閒置（STANDBY / STOPPED） → IDLE   → 可以開始新的控制
        PCS 運轉中（CHARGING/DISCHARGING）→ 即使有 LastControl 也無法證明
                                            仍在有效期內 → UNKNOWN → BLOCK
    也就是「可以從 IDLE 開始」，但「暫時不能自動接管一台已在運轉的 PCS」。

🔴 功率只作 Authority corroboration
    `actual_active_power_kw` 在此僅用於佐證「這個 operation 看起來還像不像是
    Phase 6 自己發動的」。**絕不**用於 ReadBackVerifier 的 command success 判定
    —— 實機證實 AC 功率比旗標晚約一個 backend refresh cycle，拿它判 success
    會造成假 timeout。兩者是不同的問題。
"""
import math
from dataclasses import dataclass, asdict


# ======================================================================
# 值域
# ======================================================================
AUTH_IDLE = "IDLE"
AUTH_OWNED = "OWNED_BY_PHASE6"
AUTH_EXTERNAL = "EXTERNAL_OR_UNKNOWN"
AUTH_CONFLICT = "CONFLICT"
AUTH_UNKNOWN = "UNKNOWN"

AUTHORITY_STATES = frozenset({AUTH_IDLE, AUTH_OWNED, AUTH_EXTERNAL,
                              AUTH_CONFLICT, AUTH_UNKNOWN})
# 🔴 只有這兩個允許送出一般控制命令
AUTHORITY_ALLOWED_STATES = frozenset({AUTH_IDLE, AUTH_OWNED})
AUTHORITY_BLOCKED_STATES = AUTHORITY_STATES - AUTHORITY_ALLOWED_STATES

# ---- reason code（必須能看出「為什麼沒有取得控制權」，不得只回 BLOCKED）----
CA_IDLE = "CONTROL_AUTHORITY_IDLE"
CA_PHASE6 = "CONTROL_AUTHORITY_PHASE6"
CA_EXTERNAL = "CONTROL_AUTHORITY_EXTERNAL"
CA_SCHEDULE_ACTIVE = "CONTROL_AUTHORITY_SCHEDULE_ACTIVE"
CA_TRUST_INSUFFICIENT = "CONTROL_AUTHORITY_TRUST_INSUFFICIENT"
CA_CONFLICT_STATE = "CONTROL_AUTHORITY_CONFLICT_STATE"
CA_CONFLICT_POWER = "CONTROL_AUTHORITY_CONFLICT_POWER"
CA_TTL_UNSET = "CONTROL_AUTHORITY_TTL_UNSET"
CA_EXPIRED = "CONTROL_AUTHORITY_EXPIRED"
CA_TOLERANCE_UNSET = "CONTROL_AUTHORITY_TOLERANCE_UNSET"
CA_PCS_STATE_UNUSABLE = "CONTROL_AUTHORITY_PCS_STATE_UNUSABLE"
CA_MODE_UNKNOWN = "CONTROL_AUTHORITY_MODE_UNKNOWN"
CA_DATA_INSUFFICIENT = "CONTROL_AUTHORITY_DATA_INSUFFICIENT"
CA_EVALUATION_ERROR = "CONTROL_AUTHORITY_EVALUATION_ERROR"
# 🔴 Blocker 13：指令已驗證，但觀測到的 AC 功率**還不是指令之後的新資料**。
#    這**不是**衝突，也**不是**外部控制 —— 是「還不能佐證」。
#    語意上必須與 CA_CONFLICT_POWER 嚴格分開：
#      CONFLICT_POWER      已取得指令後的新觀測，且與目標不符 → 真的有人在動它
#      NOT_YET_CORROBORATED 尚未取得指令後的新觀測         → 我們自己剛下的指令
#    兩者都 Fail Closed（都不允許再送指令），但**不得**混為一談：
#    把後者當成前者，會讓 Phase 6 把自己剛送出的作業誤認成外部控制。
CA_NOT_YET_CORROBORATED = "CONTROL_AUTHORITY_NOT_YET_CORROBORATED"

AUTHORITY_REASONS = frozenset({
    CA_IDLE, CA_PHASE6, CA_EXTERNAL, CA_SCHEDULE_ACTIVE, CA_TRUST_INSUFFICIENT,
    CA_CONFLICT_STATE, CA_CONFLICT_POWER, CA_TTL_UNSET, CA_EXPIRED,
    CA_TOLERANCE_UNSET, CA_PCS_STATE_UNUSABLE, CA_MODE_UNKNOWN,
    CA_DATA_INSUFFICIENT, CA_EVALUATION_ERROR, CA_NOT_YET_CORROBORATED,
})

# 「尚未能佐證擁有權」的 reason —— 下游（recovery / reconciler）必須據此
# 避免把自家作業誤標成 EXTERNAL_CONTROL。
PENDING_CORROBORATION_REASONS = frozenset({CA_NOT_YET_CORROBORATED})

# ---- PCS 狀態分類 ----
# ⚠️ 這裡刻意不 import pcs_control_integration（那會造成循環 import）。
#    值必須與 PCI.PCS_STATE_RUNNING / PCS_STATE_IDLE / PCS_STATE_UNUSABLE 相同，
#    有專屬測試比對兩邊，防止日後漂移。
PCS_RUNNING_STATES = frozenset({"CHARGING", "DISCHARGING"})
PCS_IDLE_STATES = frozenset({"STANDBY", "STOPPED"})
PCS_UNUSABLE_STATES = frozenset({"UNKNOWN", "CONFLICT"})
KNOWN_PCS_STATES = PCS_RUNNING_STATES | PCS_IDLE_STATES | PCS_UNUSABLE_STATES

# ---- 控制動作（與 pcs_control_integration.CTRL_* / last_control_store.ACT_* 相同）----
ACT_CHARGE = "charge"
ACT_DISCHARGE = "discharge"
ACT_STOP = "stop"
# LastControl.action → 該命令成功後設備**應該**呈現的狀態
ACTION_EXPECTED_STATE = {ACT_CHARGE: "CHARGING", ACT_DISCHARGE: "DISCHARGING"}
# LastControl.action → 指令生效後，**觀測到的 AC 有功**應有的正負號。
# 🔴 這是「觀測」慣例（充電為正、放電為負），實機四筆一致。
#    **不是** PCS 命令 setpoint 的正負號慣例 —— 兩者相反，絕不可混用。
ACTION_EXPECTED_SIGN = {ACT_CHARGE: 1, ACT_DISCHARGE: -1}

# ---- LastControl 信任層級（與 last_control_store.TRUST_* 相同）----
TRUST_FOR_INTERVAL = "TRUSTED_FOR_INTERVAL"
TRUST_HISTORY_ONLY = "HISTORY_ONLY"
TRUST_INVALID = "INVALID"
# 🔴 只有這個層級可以作為「認領一台運轉中設備」的證據
TRUST_USABLE_FOR_AUTHORITY = frozenset({TRUST_FOR_INTERVAL})


def _sign(v):
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _observation_is_post_command(lc, request):
    """
    這一筆 AC 功率觀測，是否**確定**已經是指令之後的新資料？
    回傳 (bool, 說明)。

    背景（實機證據，Blocker 13）
        PCS 的充放電旗標與 AC 功率暫存器**不保證同步**：實測到 0 個或
        1 個 backend refresh cycle 的落差（延遲案例約 15～16 秒）。
        旗標先翻、功率還停在指令前的閒置值，是完全正常的中間狀態。

    🔴 因此**不能**拿「還沒更新的功率」去比對目標值 —— 那必然超出任何
       有意義的容差，會把我們自己剛送出的作業誤判成 CONFLICT_POWER。

    判定方式（**不使用任何固定秒數**）
        ① 時序：觀測時間必須晚於指令驗證時間。
           兩者同為 monotonic 且同一 boot 才可比較；不可比較時本條不成立，
           改由 ② 單獨負責（不因此放行）。
        ② refresh 證據：觀測值必須**已經離開**指令驗證當下所記錄的功率。
           LastControl 已保存 read-back 當下的觀測值，直接拿它當基準：
           值仍完全相同 → 無法證明暫存器已更新 → 不得佐證。

    ⚠️ ② 是必要條件：即使時序成立，只要值沒變，就仍可能是同一筆舊資料。
    ⚠️ 「舊值剛好等於目標值」也一樣不放行 —— 巧合不是證據。
    """
    observed = request.actual_active_power_kw
    baseline = getattr(lc, "actual_active_power_kw", None)
    obs_at = request.observed_at_monotonic
    cmd_at = getattr(lc, "verified_at_monotonic", None)

    # ① 時序（可判定時必須成立）
    if _is_num(obs_at) and _is_num(cmd_at) and obs_at <= cmd_at:
        return False, (f"觀測時間 {obs_at:.3f} 不晚於指令驗證時間 {cmd_at:.3f}"
                       " → 這是指令**之前**的資料，不能用來佐證")

    # ② refresh 證據（必要條件）
    if not _is_num(baseline):
        return False, ("LastControl 未保存指令當下的觀測功率 → 無從證明"
                       "目前這筆是指令後的新資料")
    if observed == baseline:
        return False, (f"AC 功率仍為指令驗證當下的值 {baseline:+.2f} kW"
                       " → 暫存器尚未更新，無法佐證（**不是**功率衝突）")
    return True, ""


def _is_num(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ======================================================================
# 政策
# ======================================================================
@dataclass(frozen=True)
class AuthorityPolicy:
    """
    🔴 兩個門檻本階段一律 None —— 尚未取得實測依據，**不得**自行填入數字。

    authority_ttl_sec
        一筆 verified LastControl 可以維持多久的控制權。
        None → 無法證明紀錄仍在有效期內 → 運轉中設備一律 UNKNOWN → BLOCK。
        ⚠️ 與 LastControlStore 的 interval 判斷需求方向相反（interval 越舊越安全，
           authority 越舊越危險），故刻意放在本層而非改動已驗收的 6.5-E 模組。

    authority_power_tolerance_kw
        「實際功率」與「LastControl 記錄的 target」可容許的差距（絕對值比較，
        避免依賴充放電的正負號慣例）。
        None → 無法以功率佐證 → 運轉中設備一律 UNKNOWN → BLOCK。

    require_schedule_off
        Phase 6 要取得控制權，PCS 原生排程主開關必須為 OFF。
        不做兩套 scheduler 的 arbitration —— 單一控制源原則。
    """
    authority_ttl_sec: float = None
    authority_power_tolerance_kw: float = None
    require_schedule_off: bool = True


DEFAULT_AUTHORITY_POLICY = AuthorityPolicy()


# ======================================================================
# 輸入 / 輸出
# ======================================================================
@dataclass(frozen=True)
class AuthorityRequest:
    """
    由 pcs_control_integration 準備；本模組不自行取得任何一項。

    pcs_state              PCI.classify_pcs_state() 的結果（四旗標判定）
    actual_active_power_kw 目前 AC 有功（僅供 corroboration）
    last_control           LastControlRecord（需有 action / target_power_kw /
                           verified_at_monotonic）
    last_control_trust     LastControlStore.current().trust
    pcs_mode_state         {schedule_switch, manual_switch, ...}
    ess_valid              呼叫端已判定的 ESS 快照有效性（含 freshness）
    """
    pcs_state: str = None
    actual_active_power_kw: float = None
    last_control: object = None
    last_control_trust: str = None
    pcs_mode_state: dict = None
    ess_valid: bool = None
    # 🔴 Blocker 13：這一筆觀測是**何時讀到的**（monotonic，與 LastControl 同基準）。
    #    只用來證明「觀測發生在指令驗證之後」，不參與任何門檻比較。
    #    None ＝ 呼叫端未提供 → 無法用時序證明新舊，只能改用「值已改變」的證據。
    observed_at_monotonic: float = None


@dataclass(frozen=True)
class AuthorityResult:
    state: str
    reason: str
    allowed: bool
    detail: str = ""
    pcs_state: str = None
    last_control_action: str = None
    last_control_power_kw: float = None
    observed_power_kw: float = None
    age_sec: float = None

    def as_dict(self):
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        return (f"[AUTH] {self.state:<19} allowed={str(self.allowed):<5} "
                f"reason={self.reason:<40} pcs={self.pcs_state}")


def _res(state, reason, detail="", req=None, age=None):
    lc = getattr(req, "last_control", None) if req is not None else None
    return AuthorityResult(
        state=state, reason=reason, allowed=state in AUTHORITY_ALLOWED_STATES,
        detail=detail,
        pcs_state=getattr(req, "pcs_state", None) if req is not None else None,
        last_control_action=getattr(lc, "action", None),
        last_control_power_kw=getattr(lc, "target_power_kw", None),
        observed_power_kw=(getattr(req, "actual_active_power_kw", None)
                           if req is not None else None),
        age_sec=age,
    )


# ======================================================================
# 判定
# ======================================================================
def evaluate(request, policy=DEFAULT_AUTHORITY_POLICY, now=None):
    """
    判定 Phase 6 目前是否有權發送一般控制命令。

    判定順序（順序本身是安全語意的一部分）
        0. 例外 / 輸入不可用            → UNKNOWN
        1. 原生排程主開關 ON            → EXTERNAL_OR_UNKNOWN（排程已取得控制來源）
        2. ESS 快照無效 / 不新鮮        → UNKNOWN
        3. PCS 狀態 UNKNOWN / CONFLICT  → UNKNOWN
        4. PCS 閒置（STANDBY/STOPPED）  → IDLE（不需要 LastControl）
        5. PCS 運轉中：
             5a 無 LastControl                        → EXTERNAL_OR_UNKNOWN
             5b LastControl 信任度不足（非同一 boot）  → EXTERNAL_OR_UNKNOWN
             5c 上次命令是 stop，設備卻在運轉          → EXTERNAL_OR_UNKNOWN
             5d 上次命令的預期狀態與現況不符           → CONFLICT
             5e TTL 未設定                            → UNKNOWN
             5f 紀錄已過期 / 時間異常                  → UNKNOWN
             5g 功率容差未設定                        → UNKNOWN
             5h 功率與紀錄明顯不符                     → CONFLICT
             5i 全部通過                              → OWNED_BY_PHASE6

    ⚠️ 4 必須早於 5：設備已停，舊紀錄不構成占用。
    ⚠️ 5d 早於 5e：方向矛盾是**不需要任何門檻**就能斷定的事實，
       不應被「TTL 未設定」這種政策缺口掩蓋成 UNKNOWN。
    """
    try:
        return _evaluate(request, policy, now)
    except Exception as e:                                      # noqa: BLE001
        # 🔴 絕不 except: allow
        return AuthorityResult(AUTH_UNKNOWN, CA_EVALUATION_ERROR, False,
                               detail=f"{type(e).__name__}: {e}")


def _evaluate(request, policy, now):
    if request is None:
        return _res(AUTH_UNKNOWN, CA_DATA_INSUFFICIENT, "未提供 AuthorityRequest")
    if policy is None:
        return _res(AUTH_UNKNOWN, CA_DATA_INSUFFICIENT, "未提供 AuthorityPolicy", request)

    # ---- 1. 原生排程主開關 ----
    if getattr(policy, "require_schedule_off", True):
        mode = request.pcs_mode_state
        if not isinstance(mode, dict):
            return _res(AUTH_UNKNOWN, CA_MODE_UNKNOWN,
                        f"pcs_mode_state 型別 {type(mode).__name__} → 無法確認排程開關",
                        request)
        ss = mode.get("schedule_switch")
        if ss in (1, "1"):
            return _res(AUTH_EXTERNAL, CA_SCHEDULE_ACTIVE,
                        "PCS 原生排程主開關為 ON → 控制來源屬排程，Phase 6 不介入",
                        request)
        if ss not in (0, "0"):
            return _res(AUTH_UNKNOWN, CA_MODE_UNKNOWN,
                        f"schedule_switch={ss!r} 無法確認", request)

    # ---- 2. ESS 快照有效性 ----
    if request.ess_valid is not True:
        return _res(AUTH_UNKNOWN, CA_DATA_INSUFFICIENT,
                    f"ess_valid={request.ess_valid!r}（快照無效或不新鮮）", request)

    # ---- 3/4. PCS 狀態 ----
    st = request.pcs_state
    if st not in KNOWN_PCS_STATES:
        return _res(AUTH_UNKNOWN, CA_PCS_STATE_UNUSABLE,
                    f"pcs_state={st!r} 不在已知值域", request)
    if st in PCS_UNUSABLE_STATES:
        return _res(AUTH_UNKNOWN, CA_PCS_STATE_UNUSABLE,
                    f"pcs_state={st} → 無法判斷是否有作業進行中", request)
    if st in PCS_IDLE_STATES:
        # 設備已閒置：不需要 LastControl，舊紀錄也不構成占用
        return _res(AUTH_IDLE, CA_IDLE, f"PCS {st} → 無作業進行中，可開始新控制", request)

    # ---- 5. PCS 運轉中（CHARGING / DISCHARGING）----
    lc = request.last_control
    if lc is None:
        return _res(AUTH_EXTERNAL, CA_EXTERNAL,
                    f"PCS {st} 但無 Phase 6 LastControl → 無法證明此作業由 Phase 6 發動",
                    request)

    trust = request.last_control_trust
    if trust not in TRUST_USABLE_FOR_AUTHORITY:
        return _res(AUTH_EXTERNAL, CA_TRUST_INSUFFICIENT,
                    f"LastControl 信任度={trust!r} → 不足以認領運轉中的設備", request)

    action = getattr(lc, "action", None)
    if action == ACT_STOP:
        return _res(AUTH_EXTERNAL, CA_EXTERNAL,
                    f"上次 Phase 6 命令為 stop，設備卻在 {st} → 非 Phase 6 發動", request)
    expected = ACTION_EXPECTED_STATE.get(action)
    if expected is None:
        return _res(AUTH_EXTERNAL, CA_EXTERNAL,
                    f"LastControl.action={action!r} 無法對應任何運轉狀態", request)
    if expected != st:
        return _res(AUTH_CONFLICT, CA_CONFLICT_STATE,
                    f"LastControl.action={action}（預期 {expected}）但實際為 {st}", request)

    # ---- TTL ----
    ttl = getattr(policy, "authority_ttl_sec", None)
    if ttl is None:
        return _res(AUTH_UNKNOWN, CA_TTL_UNSET,
                    "authority_ttl_sec 未設定 → 無法證明 LastControl 仍在控制權有效期內",
                    request)
    if not _is_num(ttl) or ttl < 0:
        return _res(AUTH_UNKNOWN, CA_TTL_UNSET, f"authority_ttl_sec 不合法：{ttl!r}", request)
    at = getattr(lc, "verified_at_monotonic", None)
    if not _is_num(at) or not _is_num(now):
        return _res(AUTH_UNKNOWN, CA_DATA_INSUFFICIENT,
                    f"無法計算紀錄年齡：verified_at_monotonic={at!r} now={now!r}", request)
    age = now - at
    if age < 0:
        return _res(AUTH_UNKNOWN, CA_DATA_INSUFFICIENT,
                    f"紀錄時間在未來（age={age:.3f}s）→ 時間基準異常", request, age)
    if age > ttl:
        return _res(AUTH_UNKNOWN, CA_EXPIRED,
                    f"LastControl 年齡 {age:.3f}s > TTL {ttl:g}s", request, age)

    # ---- 功率 corroboration ----
    tol = getattr(policy, "authority_power_tolerance_kw", None)
    if tol is None:
        return _res(AUTH_UNKNOWN, CA_TOLERANCE_UNSET,
                    "authority_power_tolerance_kw 未設定 → 無法以功率佐證控制權",
                    request, age)
    if not _is_num(tol) or tol < 0:
        return _res(AUTH_UNKNOWN, CA_TOLERANCE_UNSET,
                    f"authority_power_tolerance_kw 不合法：{tol!r}", request, age)
    target = getattr(lc, "target_power_kw", None)
    observed = request.actual_active_power_kw
    if not _is_num(target) or not _is_num(observed):
        return _res(AUTH_UNKNOWN, CA_DATA_INSUFFICIENT,
                    f"功率資料不足：target={target!r} observed={observed!r}", request, age)

    # ---- Blocker 13：功率佐證的時機守則 ----
    # 🔴 只有**確定是指令之後的新觀測**才允許做門檻比較。
    #    否則一律 NOT_YET_CORROBORATED（UNKNOWN），**絕不**判成 CONFLICT_POWER。
    fresh_ok, why = _observation_is_post_command(lc, request)
    if not fresh_ok:
        return _res(AUTH_UNKNOWN, CA_NOT_YET_CORROBORATED, why, request, age)

    # ---- 方向 ----
    # 🔴 取得指令後的新觀測時，方向必須與 LastControl 的動作一致。
    #    觀測慣例（實機證據）：充電 AC 為正、放電 AC 為負。
    #    ⚠️ 這與 PCS **命令**的正負號慣例不同，不可混用。
    want_sign = ACTION_EXPECTED_SIGN.get(action)
    if want_sign is not None and observed != 0 and _sign(observed) != want_sign:
        return _res(AUTH_CONFLICT, CA_CONFLICT_POWER,
                    f"指令後的新觀測方向錯誤：{action} 預期 "
                    f"{'正' if want_sign > 0 else '負'}值，實測 {observed:+.2f} kW",
                    request, age)

    # 以絕對值比較幅度 —— 方向已於上一步單獨判定，這裡只看大小
    diff = abs(abs(observed) - abs(target))
    if diff > tol:
        return _res(AUTH_CONFLICT, CA_CONFLICT_POWER,
                    f"實際 {observed:+.2f} kW 與 LastControl target {target:g} kW "
                    f"相差 {diff:.2f} kW > 容差 {tol:g} kW", request, age)

    return _res(AUTH_OWNED, CA_PHASE6,
                f"PCS {st}；LastControl {action} {target:g}kW，年齡 {age:.3f}s，"
                f"功率差 {diff:.2f}kW ≤ 容差 {tol:g}kW", request, age)


# ======================================================================
# CLI（完全離線；只印判定表，不連任何設備）
# ======================================================================
class _LC:
    def __init__(self, action, power, at):
        self.action, self.target_power_kw, self.verified_at_monotonic = action, power, at


def main():
    print("== Phase 6.5-H Control Authority（純邏輯、零 I/O）==\n")
    p = DEFAULT_AUTHORITY_POLICY
    print(f"預設政策：ttl={p.authority_ttl_sec} tolerance={p.authority_power_tolerance_kw} "
          f"require_schedule_off={p.require_schedule_off}")
    print("⚠️ 兩個門檻皆為 None（尚無實測依據）→ 運轉中的 PCS 一律 BLOCK\n")
    mode_ok = {"schedule_switch": 0, "manual_switch": 1}
    cases = [
        ("PCS STANDBY，無 LastControl",
         AuthorityRequest("STANDBY", -1.4, None, None, mode_ok, True), p),
        ("PCS STOPPED，有舊 LastControl",
         AuthorityRequest("STOPPED", -1.3, _LC("discharge", 5.0, 0.0),
                          TRUST_FOR_INTERVAL, mode_ok, True), p),
        ("PCS DISCHARGING 80kW，無 LastControl（本次實機情境）",
         AuthorityRequest("DISCHARGING", -80.2, None, None, mode_ok, True), p),
        ("PCS DISCHARGING 5kW，有 LastControl，但 TTL 未設定",
         AuthorityRequest("DISCHARGING", -5.4, _LC("discharge", 5.0, 100.0),
                          TRUST_FOR_INTERVAL, mode_ok, True), p),
        ("同上，注入測試政策 ttl=120 tol=1.0",
         AuthorityRequest("DISCHARGING", -5.4, _LC("discharge", 5.0, 100.0),
                          TRUST_FOR_INTERVAL, mode_ok, True),
         AuthorityPolicy(120.0, 1.0)),
        ("DISCHARGING 80kW + LastControl discharge 5kW（注入政策）",
         AuthorityRequest("DISCHARGING", -80.2, _LC("discharge", 5.0, 100.0),
                          TRUST_FOR_INTERVAL, mode_ok, True),
         AuthorityPolicy(120.0, 1.0)),
        ("排程主開關 ON",
         AuthorityRequest("STANDBY", -1.4, None, None,
                          {"schedule_switch": 1, "manual_switch": 0}, True), p),
    ]
    for label, req, pol in cases:
        r = evaluate(req, pol, now=150.0)
        print(f"  {label}\n    {r}\n    {r.detail}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
