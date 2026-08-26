# -*- coding: utf-8 -*-
"""
Phase 6.5-A/B/F PCS Control Integration（Adapter + Safety 串接 + Dry-run）— 純邏輯、零 I/O
======================================================================
把 DecisionResult 轉成 ControlRequest、串接 Phase 6.4 Safety Gate，
並提供完全離線的 Dry-run pipeline。

⚠️ 本輪範圍僅 6.5-A / 6.5-B / 6.5-F。**完全離線、不送任何 PCS/BMS 指令。**
⚠️ 零 I/O：不登入 HMI、不發 HTTP/Socket、不讀寫任何檔案、不控制設備。
⚠️ **不 import device_control_operator**，不呼叫 run()，不修改 build_verify()。

尚未實作（後續子階段）
    6.5-C  真實 PCS Executor（薄 adapter 包裝既有 run(..., no_verify=True)）
    6.5-D  Read-back Verification（自建輪詢，用 read_all() 已解析的 PCS 旗標）
    6.5-E  LastControl 建立與持久化
    6.5-G  Field Control Validation

🔴 為什麼不能用既有 verify 當作「控制成功」
    build_verify() 的 pcs_power 分支回 final_verify_status="partial"
    （note: "verify source does not expose PCS power status yet"），
    而 _compute_success() 會把 partial / unknown **視為 success**。
    若採信 run() 的 success，等於「API 回 200 就算控制成功」→ 產生假的 LastControl。
    因此 LastControl 必須等 6.5-D 的 read-back 確認後才能建立。

PCS 實際狀態使用四個旗標（6.5-G 實機修正）
    pcs_charging_flag / pcs_discharging_flag / pcs_standby_flag / pcs_running_flag
    ⚠️ **禁止**只看 actual_active_power_kw 的正負號推測充放電。
       6.5-G 實機證實該欄位比旗標晚約一個 refresh cycle 更新，
       且待機時本身就是 -1.4 kW（負值不代表放電）。它永遠只作觀測。
    🔴 running flag 是 STOPPED 與 UNKNOWN 的唯一分辨依據：
       三旗標全 False + running=False → STOPPED（已停機，已知狀態）
       三旗標全 False + running=True/None → UNKNOWN（無法解釋 → Fail Closed）

Phase D.1：Direction-Reversal State Interlock（Layer 1 —— 狀態，不是時間）
    🔴 禁止「運轉中直接反向」：CHARGING → discharge / DISCHARGING → charge 一律 BLOCK，
       reason = DIRECTION_REVERSAL_REQUIRES_STOP。
       上層必須自己先產生一個明確的 STOP leg，等 read-back 確認進入
       PCS_STATE_IDLE({STANDBY, STOPPED}) 後，**下一個** decision cycle 才能決定反向。
       ⚠️ 本模組**不會**自動把一個 request 展開成 STOP + opposite 兩個 command ——
          那會把一個 operator/scheduler 決策偷偷變成兩個 PCS 指令。
    🔴 方向判定的唯一依據是 **fresh PCS flags**（本次 ess 快照），
       **不得**改用 LastControl.action —— 它可能過期、跨 boot、或與外部控制不同步。
    🔴 STOP 永不受本互鎖阻擋（Safety Gate 的 STOP_CHECKS 豁免亦維持不變）。
    🔴 與 Phase 6.4 的 min_switch_interval（Layer 2 時間互鎖）是**兩件事**，
       不得混為同一個檢查。Layer 2 目前 min_switch_interval_sec = None / UNVALIDATED，未啟用。

Decision IDLE ≠ 一定送 STOP
    IDLE + PCS CHARGING/DISCHARGING → STOP
    IDLE + PCS STANDBY              → NONE（運轉中但未充放電，不需送控制）
    IDLE + PCS STOPPED              → NONE（已停機，不需送控制）
    IDLE + PCS UNKNOWN / CONFLICT   → Fail Closed，不猜 STOP 也不宣稱已停

用法（完全離線）
    python pcs_control_integration.py --demo
"""

import sys
import math
import time
import argparse
from dataclasses import dataclass, asdict, field

import decision_engine as DE
import safety_gate as SG
import control_authority as CA


# ======================================================================
# 設定區
# ======================================================================
# ---- PCS 實際狀態（封閉值域）----
PCS_CHARGING = "CHARGING"
PCS_DISCHARGING = "DISCHARGING"
PCS_STANDBY = "STANDBY"
# 已停機（systemOnOrOffStatus.oldValue==0，且三個充放/待機旗標皆為 False）。
# Phase 6.5-G 實機證實：送出 pcs_stop_power 後設備進入此狀態並持續停留，
# **不會**回到 STANDBY。STOPPED 是「已知且有效」的狀態，與 UNKNOWN 語意不同。
PCS_STOPPED = "STOPPED"
PCS_UNKNOWN = "UNKNOWN"
PCS_CONFLICT = "CONFLICT"

PCS_STATES = frozenset({PCS_CHARGING, PCS_DISCHARGING, PCS_STANDBY,
                        PCS_STOPPED, PCS_UNKNOWN, PCS_CONFLICT})
# ⚠️ STOPPED 不屬於 UNUSABLE —— 它是可信的狀態證據，不是「無法判斷」。
PCS_STATE_UNUSABLE = frozenset({PCS_UNKNOWN, PCS_CONFLICT})
# ⚠️ STOPPED 不屬於 RUNNING —— 它已離開充放電，不需要再送 stop。
PCS_STATE_RUNNING = frozenset({PCS_CHARGING, PCS_DISCHARGING})
# 「已離開充放電」的狀態集合（STOP 的可接受結果）。
PCS_STATE_IDLE = frozenset({PCS_STANDBY, PCS_STOPPED})

# ---- ControlRequest 動作（控制層值域；沿用 Safety Gate 的字串以免轉換出錯）----
CTRL_CHARGE = SG.REQ_CHARGE           # "charge"
CTRL_DISCHARGE = SG.REQ_DISCHARGE     # "discharge"
CTRL_STOP = SG.REQ_STOP               # "stop"
CTRL_NONE = "none"                    # 不需要送控制（與 STOP 語意不同）

CONTROL_ACTIONS = frozenset({CTRL_CHARGE, CTRL_DISCHARGE, CTRL_STOP})   # 需經 Safety Gate
ALL_CONTROL_REQUESTS = CONTROL_ACTIONS | {CTRL_NONE}

# ---- ControlRequest 原因 ----
CR_DECISION_CHARGE = "DECISION_CHARGE"
CR_DECISION_DISCHARGE = "DECISION_DISCHARGE"
CR_DECISION_NO_ACTION = "DECISION_NO_ACTION"
CR_DECISION_MISSING = "DECISION_MISSING"
CR_DECISION_INVALID_ACTION = "DECISION_INVALID_ACTION"
CR_IDLE_STOP_REQUIRED = "IDLE_STOP_REQUIRED"        # IDLE 但 PCS 仍在運轉 → 必須停
CR_IDLE_ALREADY_STANDBY = "IDLE_ALREADY_STANDBY"    # IDLE 且 PCS 已待機 → 不需控制
CR_IDLE_ALREADY_STOPPED = "IDLE_ALREADY_STOPPED"    # IDLE 且 PCS 已停機 → 不需控制
CR_AUTHORITY_BLOCKED = "CONTROL_AUTHORITY_BLOCKED"  # Authority 未放行（fallback）
CR_AUTHORITY_MISSING = "CONTROL_AUTHORITY_MISSING"  # pipeline 未取得判定 → Fail Closed
CR_PCS_STATE_UNKNOWN = "PCS_STATE_UNKNOWN"          # Fail Closed
CR_PCS_STATE_CONFLICT = "PCS_STATE_CONFLICT"        # Fail Closed

# ---- Phase D.1：Direction-Reversal State Interlock ----
# 🔴 以下 reason 只描述「狀態互鎖」，與時間門檻（min_switch_interval）無關。
CR_DIRECTION_REVERSAL_REQUIRES_STOP = "DIRECTION_REVERSAL_REQUIRES_STOP"
CR_DIRECTION_STATE_UNUSABLE = "DIRECTION_STATE_UNUSABLE"      # UNKNOWN/CONFLICT → Fail Closed
DIR_NOT_APPLICABLE = "DIRECTION_NOT_APPLICABLE"               # stop / none：互鎖不適用
DIR_SAME_DIRECTION = "DIRECTION_SAME"                         # 同向，非反向切換
DIR_FROM_IDLE = "DIRECTION_FROM_IDLE"                         # 由 IDLE 起始，方向已隔離

# 反向對照：要送 charge 時，「相反的運轉方向」是 DISCHARGING，反之亦然。
OPPOSITE_RUNNING_STATE = {CTRL_CHARGE: PCS_DISCHARGING,
                          CTRL_DISCHARGE: PCS_CHARGING}
# 完成方向隔離所需的狀態 —— 沿用既有 PCS_STATE_IDLE，**不新增「必須 STANDBY」的限制**。
# 6.5-G 實機已證實 STOPPED → DISCHARGE 可直接成功。
DIRECTION_ISOLATED_STATES = PCS_STATE_IDLE
DIRECTION_BLOCK_REASONS = frozenset({CR_DIRECTION_REVERSAL_REQUIRES_STOP,
                                     CR_DIRECTION_STATE_UNUSABLE})

# ---- Dry-run 結果 ----
# 🔴 AUTHORITY_BLOCKED 與 NO_CONTROL 語意不同，不得合併：
#    NO_CONTROL        = 不需要送控制（例如 PCS 本來就停著）
#    AUTHORITY_BLOCKED = 需要送，但 Phase 6 目前沒有控制權
OUT_AUTHORITY_BLOCKED = "AUTHORITY_BLOCKED"
OUT_DIRECTION_BLOCKED = "DIRECTION_BLOCKED"
OUT_NO_CONTROL = "NO_CONTROL"
OUT_SAFETY_BLOCKED = "SAFETY_BLOCKED"
OUT_WOULD_EXECUTE = "WOULD_EXECUTE"
# 保留給 6.5-C：Safety 放行但 operator 自帶的第二層 precheck 擋下 —— 那是縱深防禦的
# 合法結果，不是 bug。本階段不會產生（無真實 executor）。
OUT_SAFETY_ALLOW_OPERATOR_BLOCKED = "SAFETY_ALLOW_OPERATOR_BLOCKED"

DRYRUN_OUTCOMES = frozenset({OUT_NO_CONTROL, OUT_AUTHORITY_BLOCKED, OUT_SAFETY_BLOCKED,
                             OUT_WOULD_EXECUTE, OUT_SAFETY_ALLOW_OPERATOR_BLOCKED})


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ======================================================================
# 6.5-A：PCS 實際狀態
# ======================================================================
def classify_pcs_state(charging, discharging, standby, running=None):
    """
    由 PCS 旗標判定實際狀態（封閉值域）。

    只有 `is True` / `is False` 才算證據；None 與其他型別一律視為「無證據」。

    判定順序
        1. C/D/S 有 >= 2 個 True                → CONFLICT（互斥狀態同時成立，與 running 無關）
        2. running is False（設備已停機）
             - C/D/S **三者皆為明確 False**     → STOPPED
             - 仍有一個 C/D/S 為 True           → CONFLICT（旗標證據互相矛盾）
             - 任一為 None（欄位缺失）          → UNKNOWN（證據不足，Fail Closed）
               🔴 None 不得當成 False。「沒有證據」≠「明確為 False」——
                  只有 running 一項證據時，無法確認「確實沒在充放電」。
        3. running is True
             - 恰好 1 個 C/D/S 為 True          → 對應狀態
             - C/D/S 全 False                   → UNKNOWN（運轉中卻三態皆否 → 無法解釋）
        4. running is None（未提供／欄位缺失）
             - 維持既有 3 旗標行為：1 個 True → 對應狀態；全 False → UNKNOWN

    🔴 STOPPED / STANDBY / UNKNOWN 三者語意嚴格分離
        STOPPED  = 已停機，**已知且有效**的狀態（實機 STOP 後的歸宿）
        STANDBY  = 運轉中但未充放電
        UNKNOWN  = 證據不足／無法解釋 → Fail Closed

    ⚠️ running 預設 None，因此既有的 3 參數呼叫結果**逐一不變**（backward compatible）。
    """
    flags = ((PCS_CHARGING, charging is True),
             (PCS_DISCHARGING, discharging is True),
             (PCS_STANDBY, standby is True))
    on = [name for name, v in flags if v]
    if len(on) >= 2:
        return PCS_CONFLICT
    if running is False:
        if on:                                  # 仍有一個 C/D/S 為 True → 證據矛盾
            return PCS_CONFLICT
        # ⚠️ 這裡**必須**逐一檢查 is False，不能用 `not on` ——
        #    `not on` 只代表「沒有 True」，None（欄位缺失）也會通過。
        if charging is False and discharging is False and standby is False:
            return PCS_STOPPED
        return PCS_UNKNOWN                      # 任一為 None → 證據不足
    if len(on) == 1:
        return on[0]
    return PCS_UNKNOWN


def pcs_state_from_ess(ess):
    """由 EssSnapshot 的四個旗標（含 running）判定 PCS 狀態；ess 缺失一律 UNKNOWN。"""
    if ess is None:
        return PCS_UNKNOWN
    return classify_pcs_state(getattr(ess, "pcs_charging_flag", None),
                              getattr(ess, "pcs_discharging_flag", None),
                              getattr(ess, "pcs_standby_flag", None),
                              getattr(ess, "pcs_running_flag", None))


# ======================================================================
# 6.5-A：ControlRequest
# ======================================================================
@dataclass(frozen=True)
class ControlRequest:
    """
    要送給 PCS 的控制請求。

    action ∈ charge / discharge / stop / none
        none  = 不需要送控制（PCS 已在目標狀態，或 Fail Closed 決定不動作）
        stop  = **必須**送停止控制
        兩者語意不得混淆。
    """
    action: str
    target_power_kw: float = None
    reason: str = ""
    decision_action: str = None
    pcs_actual_state: str = None
    authority_state: str = None
    detail: str = ""

    @property
    def needs_control(self):
        return self.action in CONTROL_ACTIONS

    def as_dict(self):
        return asdict(self)

    def as_json_dict(self):
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        pw = "" if self.target_power_kw is None else f" power={self.target_power_kw:g}kW"
        return (f"[CTRL] {self.action:<9}{pw}  reason={self.reason:<24} "
                f"decision={self.decision_action} pcs={self.pcs_actual_state}")


@dataclass(frozen=True)
class DirectionResult:
    """
    Layer 1 方向狀態互鎖的判定結果。純資料，無 I/O。

    allowed=False 代表「這個方向現在不可直接送出」，**不代表**設備有問題。
    """
    allowed: bool
    reason: str
    control_action: str = None
    pcs_state: str = None
    detail: str = ""

    def as_dict(self):
        return asdict(self)


def check_direction_interlock(control_action, pcs_state):
    """
    Phase D.1 Layer 1：方向狀態互鎖。純函式、零 I/O、不含任何時間門檻。

    判定順序
        1. control_action 非 charge/discharge（stop / none）→ 不適用，一律放行。
           🔴 STOP 永遠不得被本互鎖阻擋。
        2. pcs_state ∈ {UNKNOWN, CONFLICT} → Fail Closed。
           無法證明目前方向，就無法證明這不是一次反向切換 → 不放行。
        3. pcs_state == 相反的運轉方向 → BLOCK（DIRECTION_REVERSAL_REQUIRES_STOP）。
        4. pcs_state == 同方向的運轉狀態 → 放行（非反向切換，交由既有邏輯處理）。
        5. pcs_state ∈ {STANDBY, STOPPED} → 放行（方向已隔離）。

    ⚠️ 唯一的方向資料來源是傳入的 fresh pcs_state（由本次 ess 旗標導出）。
       **不得**改讀 LastControl.action —— 過期 / 跨 boot / 外部控制都會讓它說謊。
    ⚠️ 本函式不會產生 STOP，也不會建議 STOP 之外的任何動作；
       它只回答「現在可不可以送這個方向」。把一個 request 展開成 STOP + opposite
       兩個 command 是上層的責任，而 Phase D.1 刻意**不**替上層做這件事。
    """
    if control_action not in OPPOSITE_RUNNING_STATE:
        return DirectionResult(True, DIR_NOT_APPLICABLE, control_action, pcs_state,
                               f"{control_action!r} 非充放電動作 → 方向互鎖不適用")
    if pcs_state in PCS_STATE_UNUSABLE:
        return DirectionResult(False, CR_DIRECTION_STATE_UNUSABLE, control_action, pcs_state,
                               f"PCS 狀態 {pcs_state} 無法確認目前方向 → Fail Closed，"
                               f"不得送出 {control_action}")
    opposite = OPPOSITE_RUNNING_STATE[control_action]
    if pcs_state == opposite:
        return DirectionResult(False, CR_DIRECTION_REVERSAL_REQUIRES_STOP,
                               control_action, pcs_state,
                               f"PCS 正在 {pcs_state}，不得直接送 {control_action}："
                               f"上層必須先送 STOP，read-back 確認進入 "
                               f"{sorted(DIRECTION_ISOLATED_STATES)} 之後，"
                               f"由新的 decision cycle 重新決定方向")
    if pcs_state in PCS_STATE_RUNNING:
        return DirectionResult(True, DIR_SAME_DIRECTION, control_action, pcs_state,
                               f"PCS 已在 {pcs_state}，與 {control_action} 同向 → 非反向切換")
    return DirectionResult(True, DIR_FROM_IDLE, control_action, pcs_state,
                           f"PCS 為 {pcs_state}（方向已隔離）→ 可送 {control_action}")


def _cr(action, reason, decision_action, pcs_state, power=None, detail="",
        authority_state=None):
    return ControlRequest(action=action, target_power_kw=power, reason=reason,
                          decision_action=decision_action,
                          pcs_actual_state=pcs_state,
                          authority_state=authority_state, detail=detail)


def build_control_request(decision, ess=None, pcs_state=None, authority=None):
    """
    DecisionResult + PCS 實際狀態 → ControlRequest。純函式、零 I/O。

    對照表
        decision charge     → charge     （帶 target_power_kw）
        decision discharge  → discharge  （帶 target_power_kw）
        decision no_action  → none       （Decision 已 Fail Closed）
        decision idle       → 視 PCS 實際狀態：
                                CHARGING / DISCHARGING → stop
                                STANDBY                → none
                                UNKNOWN / CONFLICT     → none（Fail Closed，明確 reason）

    ⚠️ 對 idle + UNKNOWN/CONFLICT 之所以輸出 none：在無法確認 PCS 狀態時送出任何控制
       都是憑猜測行動。reason 會明確標示為 Fail Closed，**不是**「已確認待機」。
       其代價是「狀態不明時不會自動停機」—— 已列入 6.5-D/G 待確認事項。

    ⚠️ 本函式**不做功率驗證** —— charge/discharge 一律原樣帶出 target_power_kw，
       由 Phase 6.4 Safety Gate 統一判定（POWER_NOT_SPECIFIED / POWER_INVALID），
       避免功率規則出現第二份實作。
    """
    st = pcs_state if pcs_state is not None else pcs_state_from_ess(ess)
    if st not in PCS_STATES:
        st = PCS_UNKNOWN

    if decision is None:
        return _cr(CTRL_NONE, CR_DECISION_MISSING, None, st, detail="未提供 DecisionResult")

    da = getattr(decision, "action", None)
    power = getattr(decision, "target_power_kw", None)

    # ---- Phase 6.5-H：Control Authority 閘門（在任何動作分派之前）----
    # 🔴 Authority 未放行 → 一律 CTRL_NONE，包含 STOP。
    #    外部作業進行中時，Phase 6 不得中止他人的 operation。
    # ⚠️ Authority 放行**不代表**可以跳過 Safety Gate —— 兩者必須都通過。
    if authority is not None:
        ast = getattr(authority, "state", None)
        if not getattr(authority, "allowed", False):
            return _cr(CTRL_NONE, getattr(authority, "reason", None) or CR_AUTHORITY_BLOCKED,
                       da, st, authority_state=ast,
                       detail=(f"Control Authority={ast}："
                               f"{getattr(authority, 'detail', '')}"))

    ast = getattr(authority, "state", None) if authority is not None else None

    # ---- Phase D.1：Direction-Reversal State Interlock（Layer 1）----
    # 🔴 位置刻意在 Authority 閘門**之後**、動作分派**之前**：
    #    Authority 放行不代表方向可以反轉；方向可放行也不代表握有控制權。兩者互不取代。
    # 🔴 只作用於 charge / discharge。STOP 與 none 完全不經過這裡。
    if da in (DE.ACTION_CHARGE, DE.ACTION_DISCHARGE):
        want = CTRL_CHARGE if da == DE.ACTION_CHARGE else CTRL_DISCHARGE
        dres = check_direction_interlock(want, st)
        if not dres.allowed:
            return _cr(CTRL_NONE, dres.reason, da, st, authority_state=ast,
                       detail=dres.detail)
        reason = CR_DECISION_CHARGE if want == CTRL_CHARGE else CR_DECISION_DISCHARGE
        return _cr(want, reason, da, st, power, authority_state=ast)
    if da == DE.ACTION_NO_ACTION:
        return _cr(CTRL_NONE, CR_DECISION_NO_ACTION, da, st, authority_state=ast,
                   detail=f"Decision 已 Fail Closed：{getattr(decision, 'reason', None)}")
    if da == DE.ACTION_IDLE:
        if st in PCS_STATE_RUNNING:
            return _cr(CTRL_STOP, CR_IDLE_STOP_REQUIRED, da, st, authority_state=ast,
                       detail=f"Decision 判定不需充放電，但 PCS 仍在 {st} → 必須停止")
        if st == PCS_STANDBY:
            return _cr(CTRL_NONE, CR_IDLE_ALREADY_STANDBY, da, st, authority_state=ast,
                       detail="PCS 已待機，不需送控制")
        if st == PCS_STOPPED:
            # STOPPED 是已知且有效的狀態，不是「無法判斷」—— 不得落入 fail-closed reason
            return _cr(CTRL_NONE, CR_IDLE_ALREADY_STOPPED, da, st, authority_state=ast,
                       detail="PCS 已停機，不需送控制")
        reason = CR_PCS_STATE_CONFLICT if st == PCS_CONFLICT else CR_PCS_STATE_UNKNOWN
        return _cr(CTRL_NONE, reason, da, st, authority_state=ast,
                   detail="PCS 實際狀態無法確認 → Fail Closed，不猜測 stop 或 standby")
    return _cr(CTRL_NONE, CR_DECISION_INVALID_ACTION, da, st, authority_state=ast,
               detail=f"未知的 decision action：{da!r}")


# ======================================================================
# 6.5-B：Safety Gate 串接
# ======================================================================
@dataclass(frozen=True)
class PipelineInputs:
    """
    Dry-run pipeline 的完整輸入。所有外部狀態由呼叫端注入（本模組零 I/O）。

    ess 同時提供兩件事：PCS 實際狀態（三旗標）與 Safety Gate 需要的 ESS 快照。
    """
    decision: object = None
    ess: object = None
    pcs_fault: bool = None
    alarm_rows: tuple = None
    alarm_source_complete: bool = None
    pcs_mode_state: dict = None
    last_control: object = None
    # ---- Phase 6.5-H：Control Authority 所需（與上面的 last_control 是不同的東西）----
    # last_control        → safety_gate.LastControl {action, at, success}，供 C_SWITCH
    # last_control_record → last_control_store.LastControlRecord，供 Authority 比對
    last_control_record: object = None
    last_control_trust: str = None


def build_authority_request(inputs):
    """
    PipelineInputs → AuthorityRequest。純轉換，零 I/O。

    ess_valid 一次表達「快照存在、有效且新鮮」—— 任一不成立即 False，
    Authority 會據此回 UNKNOWN 並 fail closed。
    """
    ess = inputs.ess
    valid = bool(ess is not None and getattr(ess, "valid", False)
                 and not getattr(ess, "stale", True))
    return CA.AuthorityRequest(
        pcs_state=pcs_state_from_ess(ess),
        actual_active_power_kw=getattr(ess, "actual_active_power_kw", None),
        last_control=inputs.last_control_record,
        last_control_trust=inputs.last_control_trust,
        pcs_mode_state=inputs.pcs_mode_state,
        ess_valid=valid,
        # 🔴 Blocker 13：把「這筆觀測何時讀到的」一併帶過去。
        #    沿用既有的 read_started_at（與 freshness 同一個基準），
        #    不新增任何額外的讀取或欄位收集。
        observed_at_monotonic=getattr(ess, "read_started_at", None),
    )


def build_safety_request(ctrl, inputs):
    """
    ControlRequest → SafetyRequest。

    ⚠️ 只有 needs_control（charge / discharge / stop）才會建立；
       action=none 一律回 None —— 不需要控制就不該進 Safety Gate 的控制許可路徑。
    """
    if ctrl is None or not ctrl.needs_control:
        return None
    return SG.SafetyRequest(
        requested_action=ctrl.action,
        target_power_kw=ctrl.target_power_kw,
        ess=inputs.ess,
        pcs_fault=inputs.pcs_fault,
        alarm_rows=inputs.alarm_rows,
        alarm_source_complete=inputs.alarm_source_complete,
        pcs_mode_state=inputs.pcs_mode_state,
        last_control=inputs.last_control,
    )


# ======================================================================
# 6.5-F：Mock Executor 與 Dry-run pipeline
# ======================================================================
class MockExecutor:
    """
    離線替身：只記錄「會送出什麼」，**絕不呼叫任何真實控制路徑**。

    Phase 6.5-C 才會有真實 executor；在那之前，pipeline 的 executor 一律是本類別。
    """

    name = "mock"

    def __init__(self):
        self.calls = []

    @property
    def call_count(self):
        return len(self.calls)

    def reset(self):
        self.calls = []

    def __call__(self, ctrl, safety):
        self.calls.append({"action": ctrl.action,
                           "target_power_kw": ctrl.target_power_kw,
                           "reason": ctrl.reason,
                           "safety_reason": getattr(safety, "reason", None)})
        return {"executed": False, "mock": True, "action": ctrl.action}


@dataclass(frozen=True)
class DryRunResult:
    """
    outcome ∈ AUTHORITY_BLOCKED / DIRECTION_BLOCKED / NO_CONTROL
              / SAFETY_BLOCKED / WOULD_EXECUTE
              （SAFETY_ALLOW_OPERATOR_BLOCKED 保留給 6.5-C）

    DIRECTION_BLOCKED 與 NO_CONTROL 語意不同，不得合併：
        DIRECTION_BLOCKED  想動作，但方向互鎖不允許（上層必須先送 STOP）
        NO_CONTROL         本來就不需要送控制

    executor_called 為稽核用：SAFETY_BLOCKED 時必須為 False。
    """
    outcome: str
    reason: str
    control_request: object
    safety_result: object = None
    authority_result: object = None
    executor_called: bool = False
    executor_response: object = None
    detail: str = ""

    @property
    def would_send(self):
        return self.outcome == OUT_WOULD_EXECUTE

    def as_dict(self):
        d = asdict(self)
        d["control_request"] = self.control_request.as_dict() if self.control_request else None
        d["safety_result"] = self.safety_result.as_dict() if self.safety_result else None
        d["authority_result"] = (self.authority_result.as_dict()
                                 if self.authority_result else None)
        return d

    def __str__(self):
        act = self.control_request.action if self.control_request else "?"
        return f"[DRYRUN] {self.outcome:<16} {act:<9} reason={self.reason}"


class DryRunPipeline:
    """
    Decision → ControlRequest → SafetyGate → MockExecutor 的完整離線串接。

    不變量（由測試鎖住）：
      - ControlRequest.none      → executor 呼叫次數 0
      - Safety BLOCK             → executor 呼叫次數 0
      - Safety ALLOW             → executor 恰呼叫 1 次（本階段僅 MockExecutor）
    """

    def __init__(self, gate=None, executor=None, authority_policy=None):
        self.gate = gate if gate is not None else SG.SafetyGate()
        self.executor = executor if executor is not None else MockExecutor()
        # 🔴 預設政策的 ttl / tolerance 皆為 None → 運轉中的 PCS 一律 BLOCK
        self.authority_policy = (authority_policy if authority_policy is not None
                                 else CA.DEFAULT_AUTHORITY_POLICY)

    def run(self, inputs, now=None):
        """
        正式順序：Decision → Control Authority → Safety Gate → Executor。

        🔴 Authority 一律先評估，且**永遠**傳給 build_control_request ——
           pipeline 路徑不存在「未判定 Authority 就送控制」的可能。
        🔴 Authority 放行不代表跳過 Safety Gate；兩者都必須通過才會呼叫 executor。
        """
        now = time.monotonic() if now is None else now

        auth = CA.evaluate(build_authority_request(inputs),
                           policy=self.authority_policy, now=now)
        ctrl = build_control_request(inputs.decision, ess=inputs.ess, authority=auth)

        if not auth.allowed:
            # 需要控制但無權，或根本不需要控制 —— 兩者都不得呼叫 executor
            return DryRunResult(OUT_AUTHORITY_BLOCKED, auth.reason, ctrl,
                                authority_result=auth, executor_called=False,
                                detail=auth.detail)

        # 🔴 方向互鎖擋下來的是 BLOCK，不是「不需要控制」—— outcome 必須可分辨。
        if ctrl.reason in DIRECTION_BLOCK_REASONS:
            return DryRunResult(OUT_DIRECTION_BLOCKED, ctrl.reason, ctrl,
                                authority_result=auth, executor_called=False,
                                detail=ctrl.detail)

        if not ctrl.needs_control:
            return DryRunResult(OUT_NO_CONTROL, ctrl.reason, ctrl,
                                authority_result=auth, detail=ctrl.detail)

        sreq = build_safety_request(ctrl, inputs)
        sres = self.gate.check(sreq, now=now)
        if not sres.allowed:
            # 絕對不得呼叫 executor
            return DryRunResult(OUT_SAFETY_BLOCKED, sres.reason, ctrl, sres,
                                authority_result=auth, executor_called=False,
                                detail=sres.detail)

        resp = self.executor(ctrl, sres)
        return DryRunResult(OUT_WOULD_EXECUTE, SG.SAFE_OK, ctrl, sres,
                            authority_result=auth, executor_called=True,
                            executor_response=resp,
                            detail=f"executor={getattr(self.executor, 'name', '?')}")


# ======================================================================
# CLI（完全離線）
# ======================================================================
def _ess(soc=50.0, charging=False, discharging=False, standby=True, **over):
    r = {"communication_ok": True, "soc_percent": soc, "pcs_fault_flag": False,
         "battery_power_status": SG.BATT_ON,
         "pcs_charging_flag": charging, "pcs_discharging_flag": discharging,
         "pcs_standby_flag": standby}
    r.update(over)
    return DE.ess_snapshot_from_reading(r, 100.0, 100.2, now=100.5)


class _Dec:
    """DecisionResult 的極簡替身（CLI 演示用）。"""

    def __init__(self, action, power=None, reason="OK"):
        self.action = action
        self.target_power_kw = power
        self.reason = reason


def _inputs(decision, ess, **over):
    kw = dict(decision=decision, ess=ess, alarm_rows=(), alarm_source_complete=True,
              pcs_mode_state={"schedule_switch": 0, "manual_switch": 1})
    kw.update(over)
    return PipelineInputs(**kw)


def main():
    p = argparse.ArgumentParser(
        description="Phase 6.5-A/B/F PCS Control Integration（純邏輯、零 I/O）")
    p.add_argument("--demo", action="store_true", help="離線演示各路徑")
    args = p.parse_args()

    print("== Phase 6.5-A/B/F PCS Control Integration（純邏輯、零 I/O）==")
    print(f"  ControlRequest 值域 : {sorted(ALL_CONTROL_REQUESTS)}")
    print(f"  PCS 狀態值域        : {sorted(PCS_STATES)}")
    print("  ⚠ 本輪不含 6.5-C 真實 Executor / 6.5-D Read-back / 6.5-E LastControl / 6.5-G 實機")
    print("  ⚠ 完全離線，不送任何 PCS/BMS 指令\n")

    if not args.demo:
        print("  （加上 --demo 展示各路徑）")
        return 0

    ex = MockExecutor()
    pipe = DryRunPipeline(executor=ex)
    cases = [
        ("IDLE + PCS 待機", _Dec(DE.ACTION_IDLE), _ess(standby=True)),
        ("IDLE + PCS 充電中", _Dec(DE.ACTION_IDLE), _ess(charging=True, standby=False)),
        ("IDLE + PCS 放電中", _Dec(DE.ACTION_IDLE), _ess(discharging=True, standby=False)),
        ("IDLE + PCS 狀態未知", _Dec(DE.ACTION_IDLE), _ess(standby=False)),
        ("IDLE + PCS 狀態衝突", _Dec(DE.ACTION_IDLE), _ess(charging=True, standby=True)),
        ("NO_ACTION（功率未設定）", _Dec(DE.ACTION_NO_ACTION, reason="POLICY_POWER_NOT_CONFIGURED"),
         _ess(standby=True)),
        ("CHARGE（mock 功率 30）", _Dec(DE.ACTION_CHARGE, 30.0), _ess(standby=True)),
        ("DISCHARGE（mock 功率 40）", _Dec(DE.ACTION_DISCHARGE, 40.0), _ess(standby=True)),
        ("CHARGE 但 SOC=99（Safety 擋）", _Dec(DE.ACTION_CHARGE, 30.0), _ess(soc=99.0)),
        ("CHARGE 但功率 None（Safety 擋）", _Dec(DE.ACTION_CHARGE, None), _ess(standby=True)),
        ("STOP 但電池下電（Safety 放行）", _Dec(DE.ACTION_IDLE),
         _ess(charging=True, standby=False, battery_power_status=SG.BATT_OFF)),
    ]
    for label, dec, e in cases:
        before = ex.call_count
        r = pipe.run(_inputs(dec, e), now=1000.0)
        delta = ex.call_count - before
        print(f"  {label:<30} {r}  executor+{delta}")

    print(f"\n  MockExecutor 總呼叫次數：{ex.call_count}")
    print(f"  送出內容：{[c['action'] for c in ex.calls]}")
    print("  ⚠ 全部為 mock，未接觸任何真實 PCS 控制路徑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
