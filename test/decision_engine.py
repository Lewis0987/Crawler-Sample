# -*- coding: utf-8 -*-
"""
Phase 6.3-A Decision Engine Framework（自動充放電決策 — 骨架）— 唯讀、零 I/O
======================================================================
把 Phase 6.1/6.2/6.2b 的三個維度整合成單一決策輸入，並在任一維度不可信時
Fail Closed。**本階段不含任何充放電營運策略。**

⚠️ 零 I/O：不連 HMI / 6160 / PCS / BMS，不發任何 HTTP 請求，不讀寫檔案。
⚠️ 只產生「建議」，永遠不送控制。實際控制屬 Phase 6.5。
⚠️ 安全條件檢查（SOC 保護極限、PCS/BMS Fault、告警、控制模式…）屬 Phase 6.4 Safety Gate，
   本模組**不引用** SOC_MAX_PERCENT / SOC_MIN_PERCENT 等保護極限。
⚠️ 不使用 249 / 229（業務意義未確認）。

Phase 6.3-A 的範圍
    ✅ 資料模型（EssSnapshot / DecisionInput / DecisionResult）
    ✅ 三維度整合
    ✅ ESS 資料新鮮度（received_at / age_sec / stale / valid / reason）
    ✅ Fail Closed
    ❌ **沒有** policy —— 三維度全部有效時一律 no_action / NO_POLICY_CONFIGURED

Action 值域
    charge      策略判定應充電          （6.3-A 不會輸出）
    discharge   策略判定應放電          （6.3-A 不會輸出）
    idle        輸入有效、policy 有效，但目前不需充放電
    no_action   因 Fail Closed / 資料無效 / UNKNOWN / stale / policy 未設定 → 禁止產生控制建議
    刻意**不新增 HOLD** —— 避免與 device_control_operator 既有的 [HOLD] dry-run 標記語意衝突。

責任邊界（runtime 不 import 任何其他 Phase 模組）
    grid / tou 以 duck typing 消費（只讀 .state / .valid），並對照本模組自訂的字彙常數；
    非預期字彙一律 Fail Closed。這讓本模組不必相依 socketio，且可用簡單 stub 測試。
    字彙 drift 由 test_phase6_decision_engine.py 交叉比對 power_classifier / tou_calendar 鎖住。

EssSnapshot 的來源與新鮮度
    from_reading() 是**純轉接器**：吃既有 charge_discharge_report.read_all(client) 的回傳 dict，
    但**不 import 也不修改**該模組 —— I/O 一律由呼叫端負責。
    direction 由呼叫端以既有 resolve_direction() 算好後傳入（本模組不重新實作方向判定）；
    在 6.3-A 中僅為診斷欄位，不參與任何判定。

    🔴 age 一律以 **read_started_at** 起算，不是讀取完成時刻。
       原因：api_client 單支 GET 最壞 20s（connect 5 + read 15），而 read_all() 有 9 支序列 GET，
       最壞總耗時約 180s —— 同一份 reading 內的欄位可能相差數分鐘。
       以「開始」起算會保守高估年齡，不會低估。
       另以 read_duration_sec 判定內部一致性：超過門檻即 valid=False（READ_TOO_SLOW）。

    ⚠️ 後續整合（Phase 6.5/6.6）應提供**輕量 ESS Reader**，只取 Decision / Safety 真正需要的
       欄位，不讓決策每次都等完整 9 支 GET。該 adapter 不屬 6.3-A。

門檻（皆為工程暫定值，待 Phase 6.7 實機校正）
    ESS_STALE_AFTER_SEC     = 15.0   age > 15.0 → stale → valid=False
    ESS_READ_DURATION_MAX_SEC = 10.0 read_duration > 10.0 → valid=False（READ_TOO_SLOW）

用法（完全離線）
    python decision_engine.py --demo
"""

import sys
import math
import time
import argparse
from dataclasses import dataclass, asdict, field


# ======================================================================
# 設定區
# ======================================================================
# ---- Action 值域 ----
ACTION_CHARGE = "charge"
ACTION_DISCHARGE = "discharge"
ACTION_IDLE = "idle"
ACTION_NO_ACTION = "no_action"

VALID_ACTIONS = frozenset({ACTION_CHARGE, ACTION_DISCHARGE, ACTION_IDLE, ACTION_NO_ACTION})
CONTROL_ACTIONS = frozenset({ACTION_CHARGE, ACTION_DISCHARGE})   # 需要實際控制的動作

# 刻意不得成為 action 的字彙（由測試斷言）
FORBIDDEN_ACTIONS = frozenset({"HOLD", "STOP", "NONE", "PEAK", "OFF_PEAK", "HALF_PEAK",
                               "IMPORT", "EXPORT", "NEAR_ZERO", "UNKNOWN"})

# ---- 上游字彙（duck typing 的驗證依據；drift 由測試鎖住）----
GRID_STATE_VOCAB = frozenset({"IMPORT", "EXPORT", "NEAR_ZERO", "UNKNOWN"})
GRID_STATE_UNUSABLE = frozenset({"UNKNOWN"})

TOU_STATE_VOCAB = frozenset({"PEAK", "HALF_PEAK", "OFF_PEAK", "UNKNOWN"})
TOU_STATE_UNUSABLE = frozenset({"UNKNOWN"})

# ---- ESS 新鮮度門檻（暫定，待 Phase 6.7 校正）----
ESS_STALE_AFTER_SEC = 15.0
ESS_READ_DURATION_MAX_SEC = 10.0

# ---- Decision 原因 ----
R_OK = "OK"                                        # 保留給未來 policy 的正常路徑
R_NO_POLICY_CONFIGURED = "NO_POLICY_CONFIGURED"    # 6.3-A 的定義性結果
R_MISSING_INPUT = "MISSING_INPUT"
R_INVALID_GRID_STATE = "INVALID_GRID_STATE"
R_INVALID_TOU_STATE = "INVALID_TOU_STATE"
R_INVALID_ESS_SNAPSHOT = "INVALID_ESS_SNAPSHOT"
R_ESS_STALE = "ESS_STALE"
R_UNKNOWN_STATE_VOCAB = "UNKNOWN_STATE_VOCAB"
R_INVALID_POLICY_RESULT = "INVALID_POLICY_RESULT"

# ---- EssSnapshot 原因 ----
E_OK = "OK"
E_NO_DATA = "NO_DATA"
E_INVALID_READING = "INVALID_READING"
E_COMM_FAILED = "COMM_FAILED"
E_MISSING_FIELD = "MISSING_FIELD"
E_INVALID_TYPE = "INVALID_TYPE"
E_READ_TOO_SLOW = "READ_TOO_SLOW"
E_STALE = "STALE"

# read_all() 的 reading dict 中，EssSnapshot 視為必要且必須是有限數值的欄位
ESS_REQUIRED_NUMERIC = ("soc_percent",)
# 以下欄位允許缺失或 None（三態旗標本來就可能是 None），只做原樣搬運
ESS_OPTIONAL_FIELDS = (
    "battery_voltage_v", "battery_current_a", "actual_active_power_kw",
    "pcs_fault_flag", "pcs_running_flag", "pcs_standby_flag",
    "pcs_charging_flag", "pcs_discharging_flag",
    "battery_power_status", "pcs_control_mode_code",
    "alarm_total", "raw_source_time",
)


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class EssConfig:
    """
    ESS 新鮮度門檻。

    ⚠️ 刻意**不**檢核 stale_after > poll_interval + read_duration_max：
       目前 stale=15 / poll=10 / read_max=10 的組合並不滿足該不等式，但這是刻意的取捨 ——
       EssSnapshot 除 SOC 外還帶 PCS fault / running / standby / charging / discharging 等
       控制相關狀態，不應為了遷就 read_all() 的最壞耗時而把可接受資料年齡放寬到 25 秒。
       正確解法是後續提供輕量 ESS Reader（只抓必要欄位），而不是放寬 stale。
       新鮮度一律以實際 age_sec / read_duration_sec 判定。
    """
    stale_after_sec: float = ESS_STALE_AFTER_SEC
    read_duration_max_sec: float = ESS_READ_DURATION_MAX_SEC

    def __post_init__(self):
        for name, v in (("stale_after_sec", self.stale_after_sec),
                        ("read_duration_max_sec", self.read_duration_max_sec)):
            if not _is_number(v) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{name} 需為正的有限數值：{v!r}")


DEFAULT_ESS_CONFIG = EssConfig()


# ======================================================================
# EssSnapshot
# ======================================================================
@dataclass(frozen=True)
class EssSnapshot:
    """
    ESS（HMI 側）狀態快照 + 資料新鮮度。

    read_started_at   本機 monotonic，讀取**開始**時刻 —— age 的唯一基準（保守高估）
    read_completed_at 讀取結束時刻
    valid             False 時不得用於任何決策
    direction         由呼叫端以 resolve_direction() 算好傳入；診斷用，不參與判定
    """
    soc_percent: float
    battery_voltage_v: float
    battery_current_a: float
    actual_active_power_kw: float
    pcs_fault_flag: bool
    pcs_running_flag: bool
    pcs_standby_flag: bool
    pcs_charging_flag: bool
    pcs_discharging_flag: bool
    battery_power_status: str
    pcs_control_mode_code: str
    direction: str
    alarm_total: int
    communication_ok: bool
    raw_source_time: str
    read_started_at: float
    read_completed_at: float
    read_duration_sec: float
    age_sec: float
    stale: bool
    valid: bool
    reason: str
    detail: str = ""

    def as_dict(self):
        return asdict(self)

    def as_json_dict(self):
        """標準 JSON 沒有 Infinity / NaN（沿用 Phase 6.1 的處理原則）。"""
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        age = "n/a" if not math.isfinite(self.age_sec) else f"{self.age_sec:.2f}s"
        if not self.valid:
            extra = f" {self.detail}" if self.detail else ""
            return f"[ESS] INVALID reason={self.reason}{extra} age={age}"
        soc = "n/a" if self.soc_percent is None else f"{self.soc_percent:.1f}%"
        return (f"[ESS] VALID SOC={soc} mode={self.pcs_control_mode_code} "
                f"batt={self.battery_power_status} age={age} "
                f"read={self.read_duration_sec:.2f}s")


def _ess_invalid(reason, detail, read_started_at=None, read_completed_at=None,
                 age=float("inf"), duration=float("nan"), stale=False, comm=None):
    """建立一個不可用的 EssSnapshot —— 不外洩任何量測數值。"""
    return EssSnapshot(
        soc_percent=None, battery_voltage_v=None, battery_current_a=None,
        actual_active_power_kw=None, pcs_fault_flag=None, pcs_running_flag=None,
        pcs_standby_flag=None, pcs_charging_flag=None, pcs_discharging_flag=None,
        battery_power_status=None, pcs_control_mode_code=None, direction=None,
        alarm_total=None, communication_ok=comm, raw_source_time=None,
        read_started_at=read_started_at, read_completed_at=read_completed_at,
        read_duration_sec=duration, age_sec=age, stale=stale,
        valid=False, reason=reason, detail=detail,
    )


def ess_no_data(config=DEFAULT_ESS_CONFIG):
    """尚未取得任何 ESS reading。"""
    return _ess_invalid(E_NO_DATA, "尚未取得任何 ESS reading")


def ess_snapshot_from_reading(reading, read_started_at, read_completed_at,
                              now=None, direction=None, config=DEFAULT_ESS_CONFIG):
    """
    把 charge_discharge_report.read_all() 的 reading dict 轉成 EssSnapshot。

    純函式、零 I/O。判定順序（先結構、再通訊、再欄位、再耗時、最後新鮮度）：
        reading is None            → NO_DATA
        reading 非 dict            → INVALID_READING
        communication_ok 非 True   → COMM_FAILED
        必要欄位缺失                → MISSING_FIELD
        必要欄位型別/非有限值        → INVALID_TYPE
        read_duration > 門檻        → READ_TOO_SLOW
        age > 門檻                  → STALE
        否則                        → OK
    """
    now = time.monotonic() if now is None else now

    if reading is None:
        return ess_no_data(config)
    if not isinstance(reading, dict):
        return _ess_invalid(E_INVALID_READING, f"reading 型別為 {type(reading).__name__}")

    if not _is_number(read_started_at) or not _is_number(read_completed_at):
        return _ess_invalid(E_INVALID_READING, "read_started_at / read_completed_at 需為數值")

    duration = read_completed_at - read_started_at
    age = max(0.0, now - read_started_at)

    if reading.get("communication_ok") is not True:
        return _ess_invalid(E_COMM_FAILED,
                            f"communication_ok={reading.get('communication_ok')!r}"
                            f" fail={reading.get('_fail')}",
                            read_started_at, read_completed_at, age, duration,
                            stale=age > config.stale_after_sec, comm=False)

    missing = [k for k in ESS_REQUIRED_NUMERIC if k not in reading]
    if missing:
        return _ess_invalid(E_MISSING_FIELD, ",".join(sorted(missing)),
                            read_started_at, read_completed_at, age, duration,
                            stale=age > config.stale_after_sec, comm=True)

    bad = [k for k in ESS_REQUIRED_NUMERIC
           if not _is_number(reading[k]) or not math.isfinite(float(reading[k]))]
    if bad:
        return _ess_invalid(E_INVALID_TYPE, ",".join(sorted(bad)),
                            read_started_at, read_completed_at, age, duration,
                            stale=age > config.stale_after_sec, comm=True)

    if duration > config.read_duration_max_sec:
        return _ess_invalid(E_READ_TOO_SLOW,
                            f"read_duration={duration:.2f}s > {config.read_duration_max_sec}s"
                            "（reading 內部時間不一致）",
                            read_started_at, read_completed_at, age, duration,
                            stale=age > config.stale_after_sec, comm=True)

    stale = age > config.stale_after_sec
    if stale:
        return _ess_invalid(E_STALE, f"age={age:.2f}s > {config.stale_after_sec}s",
                            read_started_at, read_completed_at, age, duration,
                            stale=True, comm=True)

    return EssSnapshot(
        soc_percent=float(reading["soc_percent"]),
        battery_voltage_v=reading.get("battery_voltage_v"),
        battery_current_a=reading.get("battery_current_a"),
        actual_active_power_kw=reading.get("actual_active_power_kw"),
        pcs_fault_flag=reading.get("pcs_fault_flag"),
        pcs_running_flag=reading.get("pcs_running_flag"),
        pcs_standby_flag=reading.get("pcs_standby_flag"),
        pcs_charging_flag=reading.get("pcs_charging_flag"),
        pcs_discharging_flag=reading.get("pcs_discharging_flag"),
        battery_power_status=reading.get("battery_power_status"),
        pcs_control_mode_code=reading.get("pcs_control_mode_code"),
        direction=direction,
        alarm_total=reading.get("alarm_total"),
        communication_ok=True,
        raw_source_time=reading.get("raw_source_time"),
        read_started_at=read_started_at, read_completed_at=read_completed_at,
        read_duration_sec=duration, age_sec=age, stale=False,
        valid=True, reason=E_OK, detail="",
    )


# ======================================================================
# Decision 資料模型
# ======================================================================
@dataclass(frozen=True)
class DecisionInput:
    """三個維度的整合輸入。grid / tou 以 duck typing 消費（只讀 .state / .valid）。"""
    grid: object = None
    tou: object = None
    ess: object = None


@dataclass(frozen=True)
class DecisionResult:
    """
    決策**建議**。is_suggestion 恆為 True —— 本結果不得直接送控制，
    必須先通過 Phase 6.4 Safety Gate，再由 Phase 6.5 執行。
    """
    action: str
    reason: str
    inputs_valid: bool
    grid_state: str
    tou_state: str
    ess_valid: bool
    ess_stale: bool
    unmet: tuple
    target_power_kw: float          # 6.3-A 恆為 None
    policy_id: str                  # 6.3-A 恆為 None
    is_suggestion: bool             # 恆為 True
    detail: str = ""

    def as_dict(self):
        return asdict(self)

    def as_json_dict(self):
        d = asdict(self)
        d["unmet"] = list(d["unmet"])
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        um = f" unmet={list(self.unmet)}" if self.unmet else ""
        return (f"[DECISION] {self.action:<10} reason={self.reason:<22} "
                f"grid={self.grid_state} tou={self.tou_state} "
                f"ess_valid={self.ess_valid}{um}")


@dataclass(frozen=True)
class PolicyOutcome:
    """
    policy 的回傳型別（Phase 6.3-B 起）。

    policy 也可以只回傳 action 字串（向後相容，reason 視為 OK）；但若要表達
    policy 自己的原因（例如 POLICY_POWER_NOT_CONFIGURED / HELD_BY_MIN_HOLD）
    或帶出 target_power_kw，就必須回傳本型別。
    """
    action: str
    reason: str = R_OK
    detail: str = ""
    target_power_kw: float = None


def _result(action, reason, grid_state=None, tou_state=None, ess_valid=None,
            ess_stale=None, unmet=(), inputs_valid=False, detail="",
            target_power_kw=None, policy_id=None):
    return DecisionResult(
        action=action, reason=reason, inputs_valid=inputs_valid,
        grid_state=grid_state, tou_state=tou_state,
        ess_valid=ess_valid, ess_stale=ess_stale, unmet=tuple(unmet),
        target_power_kw=target_power_kw, policy_id=policy_id,
        is_suggestion=True, detail=detail,
    )


# ======================================================================
# Decision Engine
# ======================================================================
class DecisionEngine:
    """
    三維度整合 + Fail Closed。**Phase 6.3-A 不含任何營運策略。**

    policy 為未來（Phase 6.3-B）注入策略用的接縫；6.3-A 一律不提供，
    因此三維度全部有效時的結果固定為 no_action / NO_POLICY_CONFIGURED。
    """

    def __init__(self, policy=None, ess_config=DEFAULT_ESS_CONFIG):
        self.policy = policy
        self.ess_config = ess_config

    # ---- 內部：上游狀態擷取與驗證 ----
    @staticmethod
    def _read_state(obj):
        """回傳 (state, valid)；欄位缺失以 None 表示，交由呼叫端 Fail Closed。"""
        return getattr(obj, "state", None), getattr(obj, "valid", None)

    def _notify_fail_closed(self):
        """
        通知 policy「本輪為 Fail Closed」。

        policy 可能持有狀態（例如 Phase 6.3-B 的 min_hold），而 Fail Closed 時
        engine 會在呼叫 policy **之前**就短路返回 —— 若不通知，policy 的 hold
        會在輸入中斷期間繼續計時，恢復後可能誤用中斷前的舊建議。
        policy 未提供 on_fail_closed() 時自動略過；其例外一律吞掉，
        不得讓 policy 的瑕疵影響 Fail Closed 本身。
        """
        hook = getattr(self.policy, "on_fail_closed", None)
        if hook is None:
            return
        try:
            hook()
        except Exception:
            pass

    def decide(self, dinput):
        """
        由 DecisionInput 產生 DecisionResult。零 I/O、不送任何控制。

        Fail Closed 順序：缺輸入 → grid → tou → ess(stale 優先於 valid) → policy。
        任何 no_action 結果都會通知 policy（見 _notify_fail_closed）。
        """
        res = self._decide(dinput)
        if res.action == ACTION_NO_ACTION and self.policy is not None:
            self._notify_fail_closed()
        return res

    def _decide(self, dinput):
        """實際判定；Fail Closed 通知由 decide() 統一處理。"""
        if dinput is None:
            return _result(ACTION_NO_ACTION, R_MISSING_INPUT,
                           unmet=("dinput",), detail="DecisionInput 為 None")

        grid = getattr(dinput, "grid", None)
        tou = getattr(dinput, "tou", None)
        ess = getattr(dinput, "ess", None)

        unmet = [n for n, v in (("grid", grid), ("tou", tou), ("ess", ess)) if v is None]
        if unmet:
            return _result(ACTION_NO_ACTION, R_MISSING_INPUT, unmet=unmet,
                           detail="缺少必要輸入：" + ",".join(unmet))

        g_state, g_valid = self._read_state(grid)
        t_state, t_valid = self._read_state(tou)
        e_valid = getattr(ess, "valid", None)
        e_stale = getattr(ess, "stale", None)

        # ---- 字彙驗證（上游改字彙時立刻察覺，而不是靜默誤判）----
        if g_state not in GRID_STATE_VOCAB:
            return _result(ACTION_NO_ACTION, R_UNKNOWN_STATE_VOCAB, g_state, t_state,
                           e_valid, e_stale, unmet=("grid",),
                           detail=f"未知的 GridPowerState：{g_state!r}")
        if t_state not in TOU_STATE_VOCAB:
            return _result(ACTION_NO_ACTION, R_UNKNOWN_STATE_VOCAB, g_state, t_state,
                           e_valid, e_stale, unmet=("tou",),
                           detail=f"未知的 TOU State：{t_state!r}")

        # ---- Grid ----
        if g_valid is not True or g_state in GRID_STATE_UNUSABLE:
            return _result(ACTION_NO_ACTION, R_INVALID_GRID_STATE, g_state, t_state,
                           e_valid, e_stale, unmet=("grid",),
                           detail=f"grid state={g_state} valid={g_valid}")

        # ---- TOU ----
        if t_valid is not True or t_state in TOU_STATE_UNUSABLE:
            return _result(ACTION_NO_ACTION, R_INVALID_TOU_STATE, g_state, t_state,
                           e_valid, e_stale, unmet=("tou",),
                           detail=f"tou state={t_state} valid={t_valid}")

        # ---- ESS（stale 優先，讓維運看得出是「過期」而不是籠統的無效）----
        if e_stale is True:
            return _result(ACTION_NO_ACTION, R_ESS_STALE, g_state, t_state,
                           e_valid, e_stale, unmet=("ess",),
                           detail=f"ESS 資料過期：{getattr(ess, 'detail', '')}")
        if e_valid is not True:
            return _result(ACTION_NO_ACTION, R_INVALID_ESS_SNAPSHOT, g_state, t_state,
                           e_valid, e_stale, unmet=("ess",),
                           detail=f"ESS 不可信：reason={getattr(ess, 'reason', None)}")

        # ---- 三維度皆有效 ----
        if self.policy is None:
            return _result(ACTION_NO_ACTION, R_NO_POLICY_CONFIGURED, g_state, t_state,
                           e_valid, e_stale, inputs_valid=True,
                           detail="Phase 6.3-A 尚未設定任何充放電營運策略")

        # ---- policy 接縫（Phase 6.3-B 由 decision_policy 注入）----
        pid = getattr(self.policy, "policy_id", None)
        try:
            out = self.policy(dinput)
        except Exception as e:
            return _result(ACTION_NO_ACTION, R_INVALID_POLICY_RESULT, g_state, t_state,
                           e_valid, e_stale, inputs_valid=True, unmet=("policy",),
                           detail=f"policy 例外：{type(e).__name__}", policy_id=pid)

        # 相容兩種回傳：純 action 字串，或帶 reason/power 的 PolicyOutcome
        if isinstance(out, PolicyOutcome):
            action, reason, detail, power = out.action, out.reason, out.detail, out.target_power_kw
        else:
            action, reason, detail, power = out, R_OK, "", None

        if action not in VALID_ACTIONS:
            return _result(ACTION_NO_ACTION, R_INVALID_POLICY_RESULT, g_state, t_state,
                           e_valid, e_stale, inputs_valid=True, unmet=("policy",),
                           detail=f"policy 回傳非法 action：{action!r}", policy_id=pid)
        if not isinstance(reason, str) or not reason:
            return _result(ACTION_NO_ACTION, R_INVALID_POLICY_RESULT, g_state, t_state,
                           e_valid, e_stale, inputs_valid=True, unmet=("policy",),
                           detail=f"policy 回傳非法 reason：{reason!r}", policy_id=pid)
        # 只有實際要控制的動作才帶功率；idle / no_action 一律不得帶
        if action not in CONTROL_ACTIONS:
            power = None
        return _result(action, reason, g_state, t_state, e_valid, e_stale,
                       inputs_valid=True, detail=detail,
                       target_power_kw=power, policy_id=pid)


# ======================================================================
# CLI（--demo：完全合成資料，不連任何設備）
# ======================================================================
class _Stub:
    """CLI demo 用的極簡上游替身。"""

    def __init__(self, state, valid):
        self.state = state
        self.valid = valid


def _demo_reading(**over):
    r = {"communication_ok": True, "soc_percent": 55.0, "battery_voltage_v": 780.0,
         "battery_current_a": -3.2, "actual_active_power_kw": -2.5,
         "pcs_fault_flag": False, "pcs_running_flag": True, "pcs_standby_flag": False,
         "pcs_charging_flag": False, "pcs_discharging_flag": True,
         "battery_power_status": "已上電", "pcs_control_mode_code": "manual",
         "alarm_total": 0, "raw_source_time": "2026-08-20 09:00:00"}
    r.update(over)
    return r


def main():
    p = argparse.ArgumentParser(description="Phase 6.3-A Decision Engine Framework（唯讀、零 I/O）")
    p.add_argument("--demo", action="store_true", help="以合成資料展示 Fail Closed 各分支")
    args = p.parse_args()

    print("== Phase 6.3-A Decision Engine Framework（唯讀、零 I/O）==")
    print(f"  Action 值域 : {sorted(VALID_ACTIONS)}")
    print(f"  ESS 門檻    : stale>{ESS_STALE_AFTER_SEC}s、read_duration>{ESS_READ_DURATION_MAX_SEC}s"
          "（暫定，待 Phase 6.7 校正）")
    print("  ⚠ 本階段無任何充放電營運策略；三維度全部有效時亦僅輸出 no_action")
    print("  ⚠ 結果僅為建議，必須先過 Phase 6.4 Safety Gate 才可由 6.5 執行\n")

    if not args.demo:
        print("  （加上 --demo 可展示各 Fail Closed 分支）")
        return 0

    eng = DecisionEngine()
    ok_ess = ess_snapshot_from_reading(_demo_reading(), 100.0, 100.5, now=101.0)

    cases = [
        ("三維度皆有效", DecisionInput(_Stub("IMPORT", True), _Stub("PEAK", True), ok_ess)),
        ("Grid UNKNOWN", DecisionInput(_Stub("UNKNOWN", False), _Stub("PEAK", True), ok_ess)),
        ("TOU UNKNOWN", DecisionInput(_Stub("IMPORT", True), _Stub("UNKNOWN", False), ok_ess)),
        ("Grid 字彙未知", DecisionInput(_Stub("CHARGE", True), _Stub("PEAK", True), ok_ess)),
        ("ESS 過期(age 20s)", DecisionInput(
            _Stub("IMPORT", True), _Stub("PEAK", True),
            ess_snapshot_from_reading(_demo_reading(), 100.0, 100.5, now=120.0))),
        ("ESS 通訊失敗", DecisionInput(
            _Stub("IMPORT", True), _Stub("PEAK", True),
            ess_snapshot_from_reading(_demo_reading(communication_ok=False), 100.0, 100.5, now=101.0))),
        ("ESS 讀取過慢(12s)", DecisionInput(
            _Stub("IMPORT", True), _Stub("PEAK", True),
            ess_snapshot_from_reading(_demo_reading(), 100.0, 112.0, now=112.5))),
        ("ESS 無資料", DecisionInput(_Stub("IMPORT", True), _Stub("PEAK", True), ess_no_data())),
        ("缺少 tou", DecisionInput(_Stub("IMPORT", True), None, ok_ess)),
    ]
    for label, di in cases:
        print(f"  {label:<20} {eng.decide(di)}")

    acts = {eng.decide(di).action for _, di in cases}
    print(f"\n  本次 demo 出現的 action：{sorted(acts)}")
    print(f"  是否出現 charge/discharge：{'是（異常）' if acts & CONTROL_ACTIONS else '否（符合 6.3-A 預期）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
