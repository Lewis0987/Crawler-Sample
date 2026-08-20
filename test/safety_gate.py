# -*- coding: utf-8 -*-
"""
Phase 6.4 Safety Gate（安全條件檢查）— 純邏輯、零 I/O
======================================================================
Decision Engine 提出控制建議之後、真正送到 PCS 之前，判斷「現在允不允許做」。

⚠️ 零 I/O：不登入 HMI、不發任何 HTTP、不呼叫 PCS/BMS API、不讀寫 JSON/CSV、
   不建立 Session。**所有外部狀態一律由 SafetyRequest 注入。**
⚠️ 本模組不送任何控制。實際送出屬 Phase 6.5。
⚠️ 不使用 249 / 229；不使用 150 kW 當額定功率。

責任邊界
    6.3-B  想做什麼          → DecisionResult（charge / discharge / idle / no_action）
    6.4    現在允不允許做      → 本模組（SafetyResult）
    6.5    真正送出控制        → 另行實作
    「Decision IDLE」≠「PCS STOP」：是否需要送 STOP，由 Phase 6.5 比對
    Decision 意圖與 PCS 實際狀態後決定，本模組只負責「STOP 這個 request 安不安全」。

兩層 SOC 不得混用
    6.3-B  90 / 85 / 20 / 25  營運策略 hysteresis
    6.4    99 / 1             最終 Safety Protection（本模組），且**依方向判定**：
           charge 只受上限約束、discharge 只受下限約束。

與既有 Report 層 Safety 的差異（刻意不重用）
    charge_discharge_report.SafetyMonitor 是**有狀態的 session 監測器**
    （連續通訊失敗計數、功率偏離計時），且綁 target_power_kw 建構 —— 適合報告，
    不適合當控制許可判定。
    AlarmTracker 的「新增告警」是 baseline+new 的 session 模型：session 開始前
    就存在、且**仍在告警中**的 level 0 告警不算新增、不觸發停止。
    🔴 對控制許可而言那是錯的 —— 本模組判斷的是「**當下是否存在作用中的嚴重告警**」，
       不論它在 session 之前或之後產生。

重用的既有純解析能力
    charge_discharge_report_config（**零 import 的純設定檔**）提供
    SOC_MAX_PERCENT / SOC_MIN_PERCENT / ALARM_STOP_LEVELS / as_int，維持單一真相來源。
    PCS 故障旗標與電池上下電狀態由呼叫端以既有 pcs_is_fault() /
    battery_power_state_from_dodi() 解析後注入（本模組不自行以中文 badge 判斷）。

用法（完全離線）
    python safety_gate.py --demo
"""

import sys
import math
import time
import argparse
from dataclasses import dataclass, asdict, field

import decision_engine as DE
import charge_discharge_report_config as CFG


# ======================================================================
# 設定區
# ======================================================================
# ---- 控制請求動作（**控制層**值域，與 Decision 的 action 值域不同）----
# Decision 的 idle / no_action 不會進入本 Gate（見模組 docstring 的責任邊界）。
REQ_CHARGE = DE.ACTION_CHARGE           # "charge"
REQ_DISCHARGE = DE.ACTION_DISCHARGE     # "discharge"
REQ_STOP = "stop"                       # 安全停止（對應既有 pcs_stop_power）

VALID_REQUESTS = frozenset({REQ_CHARGE, REQ_DISCHARGE, REQ_STOP})
POWERED_REQUESTS = frozenset({REQ_CHARGE, REQ_DISCHARGE})   # 需要 target_power_kw 的請求

# ---- 電池上下電狀態字串（來源：device_control_scraper.battery_power_state_from_dodi）----
BATT_ON = "已上電"
BATT_OFF = "已下電"

# ---- 告警 alarmStatus 語意（來源：alarm_records_scraper._ACTIVE_VALUES）----
_ALARM_ACTIVE_VALUES = (True, 1, "1", "true", "True")
_ALARM_RECOVERED_VALUES = (False, 0, "0", "false", "False")

# ======================================================================
# Reason 字彙
# ======================================================================
SAFE_OK = "SAFE_OK"

R_INVALID_REQUESTED_ACTION = "INVALID_REQUESTED_ACTION"

R_ESS_MISSING = "ESS_MISSING"
R_ESS_COMM_FAILED = "ESS_COMM_FAILED"
R_ESS_READ_TOO_SLOW = "ESS_READ_TOO_SLOW"
R_ESS_STALE = "ESS_STALE"
R_ESS_INVALID = "ESS_INVALID"

R_PCS_FAULT = "PCS_FAULT"
R_PCS_STATE_UNKNOWN = "PCS_STATE_UNKNOWN"

R_CRITICAL_ALARM_ACTIVE = "CRITICAL_ALARM_ACTIVE"
R_ALARM_SOURCE_UNAVAILABLE = "ALARM_SOURCE_UNAVAILABLE"

R_BATTERY_NOT_POWERED = "BATTERY_NOT_POWERED"
R_BATTERY_STATE_UNKNOWN = "BATTERY_STATE_UNKNOWN"

R_CONTROL_MODE_SMART = "CONTROL_MODE_SMART"
R_CONTROL_MODE_NONE = "CONTROL_MODE_NONE"
R_CONTROL_MODE_UNKNOWN = "CONTROL_MODE_UNKNOWN"

R_SOC_UNAVAILABLE = "SOC_UNAVAILABLE"
R_SOC_AT_MAX_LIMIT = "SOC_AT_MAX_LIMIT"
R_SOC_AT_MIN_LIMIT = "SOC_AT_MIN_LIMIT"

R_POWER_NOT_SPECIFIED = "POWER_NOT_SPECIFIED"
R_POWER_INVALID = "POWER_INVALID"
R_POWER_OUT_OF_RANGE = "POWER_OUT_OF_RANGE"

R_CONTROL_HISTORY_UNAVAILABLE = "CONTROL_HISTORY_UNAVAILABLE"
R_SWITCH_TOO_SOON = "SWITCH_TOO_SOON"

# ---- Deterministic reason priority ----
# 多項同時失敗時，reason 取本序列中最前面者。序列即優先序，**不依賴 dict 迭代順序**。
# 排序理由：請求本身不合法 → 資料不可信（不可信就無從判斷其餘） → 設備故障 →
#           作用中嚴重告警 → 電池未就緒 → 控制模式 → SOC 保護 → 功率 → 切換保護。
REASON_PRIORITY = (
    R_INVALID_REQUESTED_ACTION,
    R_ESS_MISSING,
    R_ESS_COMM_FAILED,
    R_ESS_READ_TOO_SLOW,
    R_ESS_STALE,
    R_ESS_INVALID,
    R_PCS_FAULT,
    R_PCS_STATE_UNKNOWN,
    R_CRITICAL_ALARM_ACTIVE,
    R_ALARM_SOURCE_UNAVAILABLE,
    R_BATTERY_NOT_POWERED,
    R_BATTERY_STATE_UNKNOWN,
    R_CONTROL_MODE_SMART,
    R_CONTROL_MODE_NONE,
    R_CONTROL_MODE_UNKNOWN,
    R_SOC_UNAVAILABLE,
    R_SOC_AT_MAX_LIMIT,
    R_SOC_AT_MIN_LIMIT,
    R_POWER_NOT_SPECIFIED,
    R_POWER_INVALID,
    R_POWER_OUT_OF_RANGE,
    R_CONTROL_HISTORY_UNAVAILABLE,
    R_SWITCH_TOO_SOON,
)
_PRIORITY_INDEX = {r: i for i, r in enumerate(REASON_PRIORITY)}

# ---- 檢查項名稱（穩定識別碼，供診斷）----
C_REQUEST = "requested_action"
C_ESS_PRESENT = "ess_present"
C_ESS_COMM = "ess_communication"
C_ESS_READ = "ess_read_duration"
C_ESS_FRESH = "ess_freshness"
C_ESS_VALID = "ess_validity"
C_PCS_FAULT = "pcs_fault"
C_ALARM = "critical_alarm"
C_BATTERY = "battery_power"
C_MODE = "control_mode"
C_SOC = "soc_limit"
C_POWER = "power_validity"
C_POWER_RANGE = "power_range"
C_SWITCH = "switch_interval"

ALL_CHECKS = (C_REQUEST, C_ESS_PRESENT, C_ESS_COMM, C_ESS_READ, C_ESS_FRESH,
              C_ESS_VALID, C_PCS_FAULT, C_ALARM, C_BATTERY, C_MODE, C_SOC,
              C_POWER, C_POWER_RANGE, C_SWITCH)

# STOP（安全停止）只做這些檢查；其餘一律豁免。
# 依據既有設計：device_control_operator 的 PRECHECK_BATTERY_ON = set(REQUIRES_POWER)，
# pcs_stop_power **不在其中**，亦即既有 run() 對 STOP 完全不做電池／控制模式前置檢查
#   「pcs_stop_power（停止充放電）屬安全停止指令，即使電池已下電/未知/切換中也應允許」
# 本模組沿用該原則：不得因一般充放電前置條件而讓「正在充放電卻停不下來」。
STOP_CHECKS = (C_REQUEST, C_ESS_PRESENT, C_ESS_COMM)


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ======================================================================
# 設定
# ======================================================================
@dataclass(frozen=True)
class SafetyConfig:
    """
    Safety Gate 門檻。

    soc_max/min 直接取自 charge_discharge_report_config，維持單一真相來源
    （避免安全極限出現第二份定義而 drift）。

    max_power_kw = None
        設備額定上限**尚未取得正式依據** —— `PCS_CONTROL_MODES[*]["max"]=150`
        的原始碼自註為「HMI 畫面範例」，不是額定。未配置時**不做上限比較**，
        也不因此單獨 DENY。
    min_switch_interval_sec = None
        控制切換間隔**尚未取得正式數值**。未配置＝此規則未啟用，
        不因缺少 last_control 而阻擋。
        ⚠️ 與 6.3-B 的 min_hold_sec（Decision 建議穩定性）是完全不同的東西。
    """
    soc_max_percent: float = CFG.SOC_MAX_PERCENT          # 99
    soc_min_percent: float = CFG.SOC_MIN_PERCENT          # 1
    critical_alarm_levels: frozenset = frozenset(CFG.ALARM_STOP_LEVELS)   # {0}
    max_power_kw: float = None
    min_switch_interval_sec: float = None

    def __post_init__(self):
        for name, v in (("soc_max_percent", self.soc_max_percent),
                        ("soc_min_percent", self.soc_min_percent)):
            if not _is_number(v) or not math.isfinite(v) or not (0 <= v <= 100):
                raise ValueError(f"{name} 需為 0~100 的有限數值：{v!r}")
        if not self.soc_min_percent < self.soc_max_percent:
            raise ValueError("需滿足 soc_min_percent < soc_max_percent")
        if self.max_power_kw is not None:
            if not _is_number(self.max_power_kw) or not math.isfinite(self.max_power_kw) \
                    or self.max_power_kw <= 0:
                raise ValueError(f"max_power_kw 需為 None 或正的有限數值：{self.max_power_kw!r}")
        if self.min_switch_interval_sec is not None:
            v = self.min_switch_interval_sec
            if not _is_number(v) or not math.isfinite(v) or v < 0:
                raise ValueError(f"min_switch_interval_sec 需為 None 或非負有限數值：{v!r}")
        if not self.critical_alarm_levels:
            raise ValueError("critical_alarm_levels 不可為空")


DEFAULT_SAFETY_CONFIG = SafetyConfig()


# ======================================================================
# 輸入模型
# ======================================================================
@dataclass(frozen=True)
class LastControl:
    """
    上一次**實際送出且成功**的 PCS 控制。

    ⚠️ 必須是真正送到 PCS 的紀錄，**不得**用 Decision 的 held_since 充當
    （那是建議的時間，不是控制的時間）。
    at 使用本機 monotonic 秒；由 Phase 6.5 建立與持久化，本模組只讀不寫。
    """
    action: str
    at: float
    success: bool = True


@dataclass(frozen=True)
class SafetyRequest:
    """
    Gate 的完整輸入。所有外部狀態由呼叫端注入，本模組不自行取得任何資料。

    ess                    Phase 6.3-A 的 EssSnapshot
    pcs_fault              由 charge_discharge_report.pcs_is_fault(reading) 算好傳入
                           （含中文 badge fallback）；未提供則退回 ess.pcs_fault_flag
    alarm_rows             告警原始列（每列需含 level / alarmStatus）
    alarm_source_complete  **是否可證明已取得完整的作用中告警集合**。
                           只有 True 才視為完整；False / None 一律 Fail Closed。
                           ⚠️ 不以 len(alarm_rows)==100 武斷推定截斷 —— 完整性由呼叫端判定。
    pcs_mode_state         _read_pcs_mode_state() 的回讀結果
                           {schedule_switch, manual_switch, ...}
    last_control           上一次成功控制（min_switch_interval 用）
    """
    requested_action: str
    target_power_kw: float = None
    ess: object = None
    pcs_fault: bool = None
    alarm_rows: tuple = None
    alarm_source_complete: bool = None
    pcs_mode_state: dict = None
    last_control: object = None


# ======================================================================
# 輸出模型
# ======================================================================
@dataclass(frozen=True)
class SafetyCheck:
    name: str
    passed: bool
    reason: str = None          # 未通過時的 reason；通過為 None
    detail: str = ""
    skipped: bool = False       # True＝該請求類型豁免或規則未配置

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SafetyResult:
    """
    allowed  任一必要條件失敗即 False
    reason   由 REASON_PRIORITY 決定的**主要**阻擋原因（deterministic）
    checks   全部檢查項的結果（含通過與豁免），供 Phase 6.7 實機診斷
    """
    allowed: bool
    reason: str
    detail: str
    checked_at: float
    requested_action: str
    target_power_kw: float
    checks: tuple = ()

    @property
    def failed(self):
        """所有未通過的檢查（依 REASON_PRIORITY 排序）。"""
        f = [c for c in self.checks if not c.passed]
        return tuple(sorted(f, key=lambda c: _PRIORITY_INDEX.get(c.reason, 10_000)))

    @property
    def failed_reasons(self):
        return tuple(c.reason for c in self.failed)

    def as_dict(self):
        d = asdict(self)
        d["checks"] = [c.as_dict() for c in self.checks]
        return d

    def as_json_dict(self):
        d = self.as_dict()
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        if self.allowed:
            pw = "" if self.target_power_kw is None else f" power={self.target_power_kw:g}kW"
            return f"[SAFETY] ALLOW  {self.requested_action}{pw}"
        return (f"[SAFETY] DENY   {self.requested_action}  reason={self.reason}"
                f"  failed={list(self.failed_reasons)}")


# ======================================================================
# 個別檢查（皆為純函式；回傳 SafetyCheck）
# ======================================================================
def _ok(name, detail=""):
    return SafetyCheck(name, True, None, detail)


def _skip(name, detail):
    return SafetyCheck(name, True, None, detail, skipped=True)


def _no(name, reason, detail=""):
    return SafetyCheck(name, False, reason, detail)


def _chk_request(action):
    if action in VALID_REQUESTS:
        return _ok(C_REQUEST, f"action={action}")
    return _no(C_REQUEST, R_INVALID_REQUESTED_ACTION,
               f"action={action!r} 不在 {sorted(VALID_REQUESTS)} 之內")


def _chk_ess_present(ess):
    if ess is None:
        return _no(C_ESS_PRESENT, R_ESS_MISSING, "未提供 EssSnapshot")
    return _ok(C_ESS_PRESENT)


def _chk_ess_comm(ess):
    v = getattr(ess, "communication_ok", None)
    if v is True:
        return _ok(C_ESS_COMM)
    return _no(C_ESS_COMM, R_ESS_COMM_FAILED, f"communication_ok={v!r}")


def _chk_ess_read(ess):
    if getattr(ess, "reason", None) == DE.E_READ_TOO_SLOW:
        return _no(C_ESS_READ, R_ESS_READ_TOO_SLOW,
                   f"read_duration={getattr(ess, 'read_duration_sec', None)}")
    return _ok(C_ESS_READ)


def _chk_ess_fresh(ess):
    if getattr(ess, "stale", None) is True:
        return _no(C_ESS_FRESH, R_ESS_STALE, f"age={getattr(ess, 'age_sec', None)}")
    return _ok(C_ESS_FRESH)


def _chk_ess_valid(ess):
    if getattr(ess, "valid", None) is not True:
        return _no(C_ESS_VALID, R_ESS_INVALID,
                   f"valid={getattr(ess, 'valid', None)} reason={getattr(ess, 'reason', None)}")
    return _ok(C_ESS_VALID)


def _chk_pcs_fault(req, ess):
    v = req.pcs_fault
    src = "SafetyRequest.pcs_fault"
    if v is None:
        v = getattr(ess, "pcs_fault_flag", None)
        src = "ess.pcs_fault_flag"
    if v is True:
        return _no(C_PCS_FAULT, R_PCS_FAULT, f"{src}=True")
    if v is False:
        return _ok(C_PCS_FAULT, src)
    return _no(C_PCS_FAULT, R_PCS_STATE_UNKNOWN, f"{src}={v!r}（無法確認故障狀態）")


def _alarm_active(status):
    """三態：True 作用中 / False 已恢復 / None 無法判定。"""
    if status in _ALARM_ACTIVE_VALUES:
        return True
    if status in _ALARM_RECOVERED_VALUES:
        return False
    return None


def _chk_alarm(req, cfg):
    if req.alarm_source_complete is not True:
        return _no(C_ALARM, R_ALARM_SOURCE_UNAVAILABLE,
                   f"alarm_source_complete={req.alarm_source_complete!r}"
                   "（無法證明取得完整的作用中告警集合）")
    rows = req.alarm_rows
    if not isinstance(rows, (list, tuple)):
        return _no(C_ALARM, R_ALARM_SOURCE_UNAVAILABLE,
                   f"alarm_rows 型別為 {type(rows).__name__}")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return _no(C_ALARM, R_ALARM_SOURCE_UNAVAILABLE, f"第 {i} 列不是 dict")
        lv = CFG.as_int(row.get("level"))
        if lv is None:
            return _no(C_ALARM, R_ALARM_SOURCE_UNAVAILABLE,
                       f"第 {i} 列 level 無法解析：{row.get('level')!r}")
        if lv not in cfg.critical_alarm_levels:
            continue
        act = _alarm_active(row.get("alarmStatus"))
        if act is False:
            continue                                    # 已恢復 → 不阻擋
        # 作用中，或無法證明已恢復 → 一律視為作用中（Fail Closed）
        return _no(C_ALARM, R_CRITICAL_ALARM_ACTIVE,
                   f"level={lv} alarmStatus={row.get('alarmStatus')!r} "
                   f"id={row.get('id')} {'（無法判定是否恢復，保守視為作用中）' if act is None else ''}")
    return _ok(C_ALARM, f"作用中嚴重告警 0 筆（共檢視 {len(rows)} 列）")


def _chk_battery(ess):
    st = getattr(ess, "battery_power_status", None)
    if st == BATT_ON:
        return _ok(C_BATTERY, st)
    if st == BATT_OFF:
        return _no(C_BATTERY, R_BATTERY_NOT_POWERED, st)
    return _no(C_BATTERY, R_BATTERY_STATE_UNKNOWN, f"battery_power_status={st!r}")


def _chk_mode(state):
    """
    沿用既有 _pcs_charge_discharge_precheck 的判定樹（已實機驗證）：
    只有 schedulePlanSwitch==0 且 manualModeSwitch==1（手動模式）才放行。
    ⚠️ 依 read-back 判定，**不採信任何「上次控制 API 成功」**。
    """
    if not isinstance(state, dict):
        return _no(C_MODE, R_CONTROL_MODE_UNKNOWN,
                   f"pcs_mode_state 型別為 {type(state).__name__}")
    ps, ms = state.get("schedule_switch"), state.get("manual_switch")
    if ps is None:
        return _no(C_MODE, R_CONTROL_MODE_UNKNOWN, "schedule_switch=None（排程開關無法確認）")
    if ps == 1:
        return _no(C_MODE, R_CONTROL_MODE_SMART, "智慧模式（排程主開關開啟）")
    if ps != 0:
        return _no(C_MODE, R_CONTROL_MODE_UNKNOWN, f"schedule_switch={ps!r}")
    if ms is None:
        return _no(C_MODE, R_CONTROL_MODE_UNKNOWN, "manual_switch=None（手動開關無法確認）")
    if ms == 0:
        return _no(C_MODE, R_CONTROL_MODE_NONE, "未啟用任何控制模式")
    if ms != 1:
        return _no(C_MODE, R_CONTROL_MODE_UNKNOWN, f"manual_switch={ms!r}")
    return _ok(C_MODE, "手動模式")


def _chk_soc(action, ess, cfg):
    """依方向判定：charge 只受上限、discharge 只受下限。"""
    soc = getattr(ess, "soc_percent", None)
    if not _is_number(soc) or not math.isfinite(soc):
        return _no(C_SOC, R_SOC_UNAVAILABLE, f"soc_percent={soc!r}")
    if action == REQ_CHARGE:
        if soc >= cfg.soc_max_percent:
            return _no(C_SOC, R_SOC_AT_MAX_LIMIT,
                       f"SOC {soc:g}% >= 上限 {cfg.soc_max_percent:g}%")
        return _ok(C_SOC, f"charge：SOC {soc:g}% < {cfg.soc_max_percent:g}%")
    if soc <= cfg.soc_min_percent:
        return _no(C_SOC, R_SOC_AT_MIN_LIMIT,
                   f"SOC {soc:g}% <= 下限 {cfg.soc_min_percent:g}%")
    return _ok(C_SOC, f"discharge：SOC {soc:g}% > {cfg.soc_min_percent:g}%")


def _chk_power(power):
    if power is None:
        return _no(C_POWER, R_POWER_NOT_SPECIFIED, "target_power_kw 未指定")
    if not _is_number(power) or not math.isfinite(power) or power <= 0:
        return _no(C_POWER, R_POWER_INVALID, f"target_power_kw={power!r}")
    return _ok(C_POWER, f"{power:g} kW")


def _chk_power_range(power, cfg):
    if cfg.max_power_kw is None:
        return _skip(C_POWER_RANGE, "max_power_kw 未配置（設備額定尚無正式依據）→ 不做上限比較")
    if not _is_number(power) or not math.isfinite(power):
        return _skip(C_POWER_RANGE, "功率本身已不合法，交由 power_validity 判定")
    if power > cfg.max_power_kw:
        return _no(C_POWER_RANGE, R_POWER_OUT_OF_RANGE,
                   f"{power:g} kW > 額定 {cfg.max_power_kw:g} kW")
    return _ok(C_POWER_RANGE, f"{power:g} kW <= {cfg.max_power_kw:g} kW")


def _chk_switch(action, last, cfg, now):
    """
    min_switch_interval：只在「實際控制切換」時適用（requested_action 與上次不同）。

    規則未配置（None）＝ 尚未取得正式數值 → 此規則未啟用，**不因缺少 last_control 而阻擋**。
    規則已配置但 last_control 不可用 → DENY（無法證明距上次切換已足夠）。
    """
    if cfg.min_switch_interval_sec is None:
        return _skip(C_SWITCH, "min_switch_interval_sec 未配置 → 規則未啟用")
    if last is None:
        return _no(C_SWITCH, R_CONTROL_HISTORY_UNAVAILABLE, "未提供 last_control")
    la, lat, lsucc = (getattr(last, "action", None), getattr(last, "at", None),
                      getattr(last, "success", None))
    if la not in VALID_REQUESTS or not _is_number(lat) or not math.isfinite(lat) \
            or lsucc is not True:
        return _no(C_SWITCH, R_CONTROL_HISTORY_UNAVAILABLE,
                   f"last_control 不可信：action={la!r} at={lat!r} success={lsucc!r}")
    if la == action:
        return _skip(C_SWITCH, f"與上次控制相同（{action}）→ 非切換，不適用")
    elapsed = now - lat
    if elapsed < cfg.min_switch_interval_sec:
        return _no(C_SWITCH, R_SWITCH_TOO_SOON,
                   f"{la}→{action} 距上次 {elapsed:.1f}s < {cfg.min_switch_interval_sec:g}s")
    return _ok(C_SWITCH, f"{la}→{action} 距上次 {elapsed:.1f}s")


# ======================================================================
# Safety Gate
# ======================================================================
class SafetyGate:
    """無狀態、純邏輯。所有外部資料由 SafetyRequest 注入。"""

    def __init__(self, config=DEFAULT_SAFETY_CONFIG):
        self.cfg = config

    def check(self, req, now=None):
        """執行全部檢查後回報（不 short-circuit），讓診斷能一次看到所有不合格項。"""
        now = time.monotonic() if now is None else now
        cfg = self.cfg

        if req is None:
            c = _no(C_REQUEST, R_INVALID_REQUESTED_ACTION, "SafetyRequest 為 None")
            return SafetyResult(False, R_INVALID_REQUESTED_ACTION, c.detail, now,
                                None, None, (c,))

        action = getattr(req, "requested_action", None)
        power = getattr(req, "target_power_kw", None)
        ess = getattr(req, "ess", None)

        req_chk = _chk_request(action)
        if not req_chk.passed:
            # 請求本身不合法時，其餘檢查無意義
            return self._build(False, (req_chk,), now, action, power)

        applicable = STOP_CHECKS if action == REQ_STOP else ALL_CHECKS
        exempt_note = "STOP（安全停止）豁免：不得因一般充放電前置條件而停不下來"

        checks = [req_chk]
        for name in ALL_CHECKS[1:]:                     # C_REQUEST 已處理
            if name not in applicable:
                checks.append(_skip(name, exempt_note))
                continue
            checks.append(self._run_one(name, req, ess, action, power, now))

        return self._build(all(c.passed for c in checks), tuple(checks), now, action, power)

    def _run_one(self, name, req, ess, action, power, now):
        cfg = self.cfg
        if name == C_ESS_PRESENT:
            return _chk_ess_present(ess)
        # 以下各項都需要 ess 物件；缺少時由 ess_present 負責回報，其餘標記豁免避免噪音
        if ess is None:
            return _skip(name, "無 EssSnapshot，交由 ess_present 判定")
        if name == C_ESS_COMM:
            return _chk_ess_comm(ess)
        if name == C_ESS_READ:
            return _chk_ess_read(ess)
        if name == C_ESS_FRESH:
            return _chk_ess_fresh(ess)
        if name == C_ESS_VALID:
            return _chk_ess_valid(ess)
        if name == C_PCS_FAULT:
            return _chk_pcs_fault(req, ess)
        if name == C_ALARM:
            return _chk_alarm(req, cfg)
        if name == C_BATTERY:
            return _chk_battery(ess)
        if name == C_MODE:
            return _chk_mode(req.pcs_mode_state)
        if name == C_SOC:
            return _chk_soc(action, ess, cfg)
        if name == C_POWER:
            return _chk_power(power)
        if name == C_POWER_RANGE:
            return _chk_power_range(power, cfg)
        if name == C_SWITCH:
            return _chk_switch(action, req.last_control, cfg, now)
        return _skip(name, "未知檢查項")

    @staticmethod
    def _build(allowed, checks, now, action, power):
        if allowed:
            return SafetyResult(True, SAFE_OK, "全部安全條件通過", now, action, power, checks)
        failed = [c for c in checks if not c.passed]
        primary = min(failed, key=lambda c: _PRIORITY_INDEX.get(c.reason, 10_000))
        return SafetyResult(False, primary.reason, primary.detail, now, action, power, checks)


# ======================================================================
# CLI（完全離線）
# ======================================================================
def _ess(soc=50.0, **over):
    r = {"communication_ok": True, "soc_percent": soc, "pcs_fault_flag": False,
         "battery_power_status": BATT_ON}
    r.update(over)
    return DE.ess_snapshot_from_reading(r, 100.0, 100.2, now=100.5)


def _base(action=REQ_CHARGE, power=30.0, **over):
    kw = dict(requested_action=action, target_power_kw=power, ess=_ess(),
              alarm_rows=(), alarm_source_complete=True,
              pcs_mode_state={"schedule_switch": 0, "manual_switch": 1})
    kw.update(over)
    return SafetyRequest(**kw)


def main():
    p = argparse.ArgumentParser(description="Phase 6.4 Safety Gate（純邏輯、零 I/O）")
    p.add_argument("--demo", action="store_true", help="以合成資料展示各阻擋分支")
    args = p.parse_args()

    cfg = DEFAULT_SAFETY_CONFIG
    print("== Phase 6.4 Safety Gate（純邏輯、零 I/O）==")
    print(f"  SOC 保護  : charge >= {cfg.soc_max_percent:g}% 拒絕、"
          f"discharge <= {cfg.soc_min_percent:g}% 拒絕（依方向判定）")
    print(f"  嚴重告警  : level ∈ {sorted(cfg.critical_alarm_levels)} 且作用中 → 拒絕")
    print(f"  額定上限  : max_power_kw={cfg.max_power_kw}（未配置 → 不做上限比較）")
    print(f"  切換間隔  : min_switch_interval_sec={cfg.min_switch_interval_sec}（未配置 → 規則未啟用）")
    print("  ⚠ 只判斷「允不允許」，不送任何控制\n")

    if not args.demo:
        print("  （加上 --demo 展示各分支）")
        return 0

    gate = SafetyGate(cfg)
    cases = [
        ("全部正常（charge）", _base()),
        ("PCS 故障", _base(ess=_ess(pcs_fault_flag=True))),
        ("PCS 故障旗標未知", _base(ess=_ess(pcs_fault_flag=None))),
        ("電池已下電", _base(ess=_ess(battery_power_status=BATT_OFF))),
        ("電池切換中", _base(ess=_ess(battery_power_status="切換中"))),
        ("作用中 level0 告警", _base(alarm_rows=({"level": 0, "alarmStatus": True, "id": 7},))),
        ("level0 已恢復（不阻擋）", _base(alarm_rows=({"level": 0, "alarmStatus": False},))),
        ("告警來源不完整", _base(alarm_source_complete=None)),
        ("智慧模式", _base(pcs_mode_state={"schedule_switch": 1, "manual_switch": 0})),
        ("未啟用任何模式", _base(pcs_mode_state={"schedule_switch": 0, "manual_switch": 0})),
        ("模式未知", _base(pcs_mode_state={"schedule_switch": None, "manual_switch": None})),
        ("charge SOC=99", _base(ess=_ess(soc=99.0))),
        ("discharge SOC=99（不受上限擋）", _base(action=REQ_DISCHARGE, ess=_ess(soc=99.0))),
        ("discharge SOC=1", _base(action=REQ_DISCHARGE, ess=_ess(soc=1.0))),
        ("功率未指定", _base(power=None)),
        ("功率為 0", _base(power=0)),
        ("ESS 過期", _base(ess=DE.ess_snapshot_from_reading(
            {"communication_ok": True, "soc_percent": 50.0}, 100.0, 100.2, now=200.0))),
        ("多項同時失敗", _base(power=None, ess=_ess(soc=99.0, pcs_fault_flag=True))),
        ("STOP（電池下電＋SOC 99＋無功率）", _base(
            action=REQ_STOP, power=None, ess=_ess(soc=99.0, battery_power_status=BATT_OFF))),
        ("STOP（通訊失敗）", _base(action=REQ_STOP, power=None,
                              ess=_ess(communication_ok=False))),
    ]
    for label, req in cases:
        print(f"  {label:<28} {gate.check(req, now=1000.0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
