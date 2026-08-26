# -*- coding: utf-8 -*-
"""
Phase 6.5-G Field Measurement Tool（實機量測工具）
======================================================================
目的
    取得 Phase 6.5-G 尚未定案參數的**實測依據**：
      - PCS 三個狀態 flag 的真實反應與是否抖動
      - CHARGE / DISCHARGE 時 actual_active_power_kw 的符號
      - ramp-up 時間（以區間回報）
      - STOP 後功率下降 / 完全停止時間（以區間回報）

    本工具**只產生量測資料**。它不做任何自動判定，也不會把分析結果寫回任何
    Production Config。timeout_sec / poll_interval_sec / stability_samples
    一律由人依本工具產出的 CSV 事後裁示。

🔴 本模組不含任何 Production 參數
    - 不定義 charge_power_kw / discharge_power_kw / max_power_kw
    - 不定義 min_switch_interval_sec
    - 不定義 ReadBackConfig.timeout_sec / poll_interval_sec / stability_samples
    - 不 import last_control_store（結構上不可能寫 LastControl）
    - 不 import charge_discharge_report_config（結構上不可能改 Production Config）
    - 量測功率一律由 CLI --power 傳入，**沒有預設值**

🔴 execute 閘門
    self.execute 為 False 時，operator **連呼叫都不會發生**（_dispatch 首行即 return）。
    原始碼中不存在 `execute=True` 字面量；execute 一律由 CLI --execute 決定後透傳。

🔴 人工逐段確認、不自動串接
    Stage 0~6 每一段執行前都必須通過 StageGate.require()。
    confirmer 未注入 → 一律 False（Fail Closed）。
    任一段未確認 / 失敗 / abort → 後續全部不執行。

取樣架構
    量測期間只讀 /hmiGuest/unauthorizedAccess/envCon/pcs（guest，不需登入）。
    該支 response 同時含五個 flag（oldValue）與功率（value），
    因此單次 GET 即得**時間一致**的快照，不需跨端點對齊。
    背景取樣執行緒必須先啟動，控制命令由另一路徑送出；
    控制 POST 阻塞（最長 120s）期間取樣不得中斷。

用法
    # Stage 0 only（唯讀，不需 --power）
    python phase6_field_measure.py --stage0-only

    # 全流程 dry-run（不送任何命令；operator 不會被呼叫）
    python phase6_field_measure.py --power <kW>

    # 實機量測（需人工逐段確認）
    python phase6_field_measure.py --power <kW> --execute
"""
import os
import sys
import csv
import math
import time
import argparse
import datetime
import threading
from dataclasses import dataclass, asdict, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pcs_control_integration as PCI          # noqa: E402  classify_pcs_state
import safety_gate as SG                       # noqa: E402
import decision_engine as DE                   # noqa: E402
import control_authority as CA                 # noqa: E402  純邏輯、零 I/O
import pcs_control_executor as EX              # noqa: E402  ReadBackVerifier（純邏輯）
# ⚠️ last_control_store 會寫檔，**不**在此 import；一律由 main() 注入（保持模組零 I/O）。


# ======================================================================
# 常數（皆為「量測分析用」，不是 Production 參數）
# ======================================================================
EP_PCS = "/hmiGuest/unauthorizedAccess/envCon/pcs"

MARK_CHARGING = "systemChargingStatus"
MARK_DISCHARGING = "systemDischargingStatus"
MARK_STANDBY = "systemStandbyStatus"
MARK_RUNNING = "systemOnOrOffStatus"
MARK_FAULT = "systemFaultStatus"
MARK_ACTIVE_POWER = "totalActivePowerOfAcBus"
MARK_DC_POWER = "dcPower"
MARK_DC_VOLTAGE = "dcInputVoltage"
MARK_DC_CURRENT = "dcCurrent"

# 量測取樣目標間隔（秒）。⚠️ 這是 measurement sample interval，
#   **不是** ReadBackConfig.poll_interval_sec，不得直接沿用為 Production 值。
DEFAULT_SAMPLE_INTERVAL_SEC = 1.0

# baseline 至少需要的有效樣本數（量測分析用門檻，非 Production 參數）
MIN_BASELINE_SAMPLES = 10

# steady 判定的候選尾窗長度（同時算多組，用來檢查結論是否穩健）
STEADY_WINDOWS = (3, 5, 10)

# ======================================================================
# Phase C.1：steady 判定參數（**量測分析用**，不是 production 參數）
# ======================================================================
# 🔴 為什麼需要「幅度」條件
#    實機證實 actual_active_power_kw（AC）可能比旗標／DC 晚 0~1 個 backend
#    refresh cycle 才更新。Phase B 的 CHARGE：
#        seq 135  state→CHARGING、dcP +4.60、dcA +5.40，但 AC 仍為 −1.30（= idle baseline）
#        seq 150（晚 1 cycle）AC 才跳到 +5.40
#    舊 detector 只要 `state != baseline.state_baseline` 就視為「已脫離 baseline」，
#    於是「CHARGING 但 AC 還停在 baseline 且平坦」滿足穩定窗 → false steady。
#
# 🔴 判定改為「狀態 + 旗標 + 方向 + 幅度 + 平穩」五者皆須成立，
#    且門檻一律由 baseline / noise_band / 實測解析度 / 指令目標推導，
#    **不得**寫死任何 kW 數值（3 kW、5 kW 都不行）。
#
# 已到達指令幅度的相對容差：|observed| 與 |target| 的差不得超過此比例。
# 依據：實機兩次量測的 AC 讀值相對指令的偏差約 2%~8%
#      （6.5-G：5kW→+5.30 / −5.40；Phase B：5kW→+5.40，DC 為 4.90）。
# 取 0.25 給約 3 倍餘裕，同時仍能排除中間過渡平台
#      （Phase 6.5-G DISCHARGE 的 −1.6 kW 平台相對 5kW 偏差 68%）。
STEADY_REACH_REL = 0.25


# ======================================================================
# 🔴 FIELD TEST ONLY —— 以下門檻**只**在本量測工具內使用
# ======================================================================
# 這些不是 production 參數，也不是 production 候選值。它們存在的唯一目的，是讓
# 「Authority → 送命令 → ReadBack → 建立 LastControl → STOP 前重新驗證 Authority」
# 這條流程能在實機上被走完並取得證據。
#
# 🔴 絕不寫入 production default。production 端一律維持：
#      ReadBackConfig(timeout_sec=None, poll_interval_sec=None, stability_samples=1)
#      AuthorityPolicy(authority_ttl_sec=None, authority_power_tolerance_kw=None)
#    （有專屬測試鎖住 production default 未被污染。）
FIELD_TEST_ONLY = "FIELD TEST ONLY — NOT PRODUCTION CONFIG"

# ReadBack：沿用 6.5-G 已分析的 PROVISIONAL timeout 與已 FINAL 的 poll / stability。
FIELD_READBACK_TIMEOUT_SEC = 75.0
FIELD_READBACK_POLL_INTERVAL_SEC = 5.0
FIELD_READBACK_STABILITY_SAMPLES = 1

# Authority TTL：本輪量測從 verified 到 STOP 預計數分鐘（穩定段需涵蓋數個 backend
# refresh cycle 再加人工觀察）。取 600s 讓流程能走完，**不代表** production TTL 候選值。
FIELD_AUTHORITY_TTL_SEC = 600.0

# Authority 功率容差：6.5-G 實測 5kW 指令的 AC 讀值為 +5.30 / −5.40（誤差 0.30~0.40kW）。
# 取 2.0kW —— 對實測誤差有約 5 倍餘裕（不會在正常運轉中誤判 CONFLICT），
# 同時對外部接管情境（實測 80.2 vs 5.0，差 75.2kW）仍有約 37 倍的偵測餘裕。
# 尚無穩定段的功率分布資料，因此這**只是讓流程可執行的保守值**，不是 tolerance 候選值。
FIELD_AUTHORITY_POWER_TOLERANCE_KW = 2.0


def field_readback_config():
    """FIELD TEST ONLY 的 ReadBackConfig。"""
    return EX.ReadBackConfig(timeout_sec=FIELD_READBACK_TIMEOUT_SEC,
                             poll_interval_sec=FIELD_READBACK_POLL_INTERVAL_SEC,
                             stability_samples=FIELD_READBACK_STABILITY_SAMPLES)


def field_authority_policy():
    """FIELD TEST ONLY 的 AuthorityPolicy。"""
    return CA.AuthorityPolicy(authority_ttl_sec=FIELD_AUTHORITY_TTL_SEC,
                              authority_power_tolerance_kw=FIELD_AUTHORITY_POWER_TOLERANCE_KW)


# action 名稱（對應 device_control_operator.ACTIONS）
ACT_CHARGE = "pcs_charge"
ACT_DISCHARGE = "pcs_discharge"
ACT_STOP = "pcs_stop_power"
_REQUIRES_POWER = (ACT_CHARGE, ACT_DISCHARGE)

# 🔴 operator action 名（pcs_charge…）與控制層 action 名（charge…）不同。
#    ReadBackVerifier 的 TARGET_STATE_MAP、LastControlStore 的 VALID_ACTIONS、
#    以及 control_authority 的 ACTION_EXPECTED_STATE 全部使用**控制層**名稱，
#    送 operator 時才用 operator 名稱。兩者混用會讓 read-back 直接回 CONFIG_NOT_READY。
CONTROL_ACTION_OF = {ACT_CHARGE: PCI.CTRL_CHARGE,
                     ACT_DISCHARGE: PCI.CTRL_DISCHARGE,
                     ACT_STOP: PCI.CTRL_STOP}
# ---- Phase 6.2 observed-power sign convention（量測層，與命令層相反）----
# 命令層：CHARGE → activePowerSetPoint 為負；DISCHARGE → 為正
# 量測層：CHARGE → actual_active_power_kw 為正；DISCHARGE → 為負
# 兩者是兩套獨立且相反的慣例，不得互推。已由 6.5-G 與 Phase B 兩次獨立實機確認。
OBSERVED_POWER_SIGN = {ACT_CHARGE: +1, ACT_DISCHARGE: -1}
EXPECTED_RUNNING_STATE = {ACT_CHARGE: "CHARGING", ACT_DISCHARGE: "DISCHARGING"}
# STOP 的可接受停止狀態（與 pcs_control_executor 的 ACCEPTED_STATES_MAP 同義）
STOPPED_STATES = ("STOPPED", "STANDBY")

# _dispatch 結果
CMD_NO_EXECUTE = "NO_EXECUTE"
CMD_POWER_NOT_SPECIFIED = "POWER_NOT_SPECIFIED"
CMD_OPERATOR_MISSING = "OPERATOR_NOT_INJECTED"
CMD_ABORTED = "ABORTED"
CMD_SENT = "SENT"
CMD_CONTROL_FAILED = "CONTROL_FAILED"
CMD_EXCEPTION = "OPERATOR_EXCEPTION"

# stage 結果
ST_OK = "OK"
ST_DECLINED = "DECLINED"
ST_ABORTED = "ABORTED"
ST_BLOCKED = "BLOCKED"
ST_INCONCLUSIVE = "INCONCLUSIVE"
ST_NOT_READY = "NOT_READY"

# abort 原因
AB_CONFLICT = "CONFLICT_ABORT"
AB_FAULT = "PCS_FAULT_ABORT"
AB_OPERATOR = "OPERATOR_EXCEPTION_ABORT"
AB_SAFETY = "SAFETY_BLOCKED_ABORT"
AB_MANUAL = "MANUAL_ABORT"
AB_BASELINE = "BASELINE_INVALID_ABORT"
AB_AUTHORITY = "CONTROL_AUTHORITY_ABORT"

# ---- Phase A.1：per-leg authorization 的阻擋原因（必須能看出是哪一層擋的）----
FA_MISSING = "FIELD_AUTHORIZATION_MISSING"
FA_NOT_GRANTED = "FIELD_AUTHORIZATION_NOT_GRANTED"
FA_ACTION_MISMATCH = "FIELD_AUTHORIZATION_ACTION_MISMATCH"
FA_CONSUMED = "FIELD_AUTHORIZATION_CONSUMED"
FA_REVALIDATION_FAILED = "FIELD_AUTHORITY_REVALIDATION_FAILED"
FA_STATE_CHANGED = "FIELD_AUTHORITY_STATE_CHANGED"
FA_STALE = "FIELD_AUTHORITY_STALE"

FIELD_AUTH_REASONS = frozenset({FA_MISSING, FA_NOT_GRANTED, FA_ACTION_MISMATCH,
                                FA_CONSUMED, FA_REVALIDATION_FAILED,
                                FA_STATE_CHANGED, FA_STALE})

SAMPLE_FIELDS = (
    "seq", "wall_clock", "t_req_mono", "t_resp_mono", "latency_sec",
    "sample_ok", "error", "pcs_state",
    "pcs_charging_flag", "pcs_discharging_flag", "pcs_standby_flag",
    "pcs_running_flag", "pcs_fault_flag",
    "actual_active_power_kw", "dc_power_kw", "dc_voltage_v", "dc_current_a",
)

EVENT_FIELDS = (
    "wall_clock", "t_mono", "seq_at_event", "stage", "event", "action",
    "power_requested_kw", "t_cmd_sent_mono", "t_cmd_returned_mono",
    "control_success", "outcome", "detail",
)


# ======================================================================
# PCS payload 解析（語意與 charge_discharge_report 完全一致）
#   flag ← oldValue（1/0）        power ← value（float）
# ======================================================================
def to_float(v):
    """把 API 值安全轉 float；None/非數字回 None。（同 charge_discharge_report._to_float）"""
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def pcs_metric_map(pcs_data):
    """envCon/pcs → {mark: {"value","unit","oldValue"}}。（同 _pcs_metric_map）"""
    out = {}
    if isinstance(pcs_data, list) and pcs_data and isinstance(pcs_data[0], dict):
        for m in pcs_data[0].get("metricsDataVoList", []) or []:
            if isinstance(m, dict) and m.get("mark"):
                out[m["mark"]] = {"value": m.get("value"), "unit": m.get("unit"),
                                  "oldValue": m.get("oldValue")}
    return out


def flag_of(marks, mark):
    """狀態旗標 → True/False/None（依 oldValue 1/0；缺失或無法解析回 None）。"""
    md = marks.get(mark)
    if not md:
        return None
    ov = md.get("oldValue")
    if ov in (1, "1"):
        return True
    if ov in (0, "0"):
        return False
    return None


def value_of(marks, mark):
    return to_float(marks.get(mark, {}).get("value"))


def observations_from_payload(pcs_data):
    """raw envCon/pcs payload → 觀測欄位 dict（不含時間戳）。"""
    marks = pcs_metric_map(pcs_data)
    charging = flag_of(marks, MARK_CHARGING)
    discharging = flag_of(marks, MARK_DISCHARGING)
    standby = flag_of(marks, MARK_STANDBY)
    running = flag_of(marks, MARK_RUNNING)
    return {
        "pcs_charging_flag": charging,
        "pcs_discharging_flag": discharging,
        "pcs_standby_flag": standby,
        "pcs_running_flag": running,
        "pcs_fault_flag": flag_of(marks, MARK_FAULT),
        # running 一併傳入 —— 否則 STOP 後的「已停機」會被誤判為 UNKNOWN
        "pcs_state": PCI.classify_pcs_state(charging, discharging, standby, running),
        "actual_active_power_kw": value_of(marks, MARK_ACTIVE_POWER),
        "dc_power_kw": value_of(marks, MARK_DC_POWER),
        "dc_voltage_v": value_of(marks, MARK_DC_VOLTAGE),
        "dc_current_a": value_of(marks, MARK_DC_CURRENT),
    }


_EMPTY_OBS = {
    "pcs_charging_flag": None, "pcs_discharging_flag": None, "pcs_standby_flag": None,
    "pcs_running_flag": None, "pcs_fault_flag": None, "pcs_state": PCI.PCS_UNKNOWN,
    "actual_active_power_kw": None, "dc_power_kw": None,
    "dc_voltage_v": None, "dc_current_a": None,
}


# ======================================================================
# 樣本
# ======================================================================
@dataclass(frozen=True)
class PcsSample:
    seq: int
    wall_clock: str
    t_req_mono: float
    t_resp_mono: float
    latency_sec: float
    sample_ok: bool
    error: str
    pcs_state: str
    pcs_charging_flag: object
    pcs_discharging_flag: object
    pcs_standby_flag: object
    pcs_running_flag: object
    pcs_fault_flag: object
    actual_active_power_kw: object
    dc_power_kw: object
    dc_voltage_v: object
    dc_current_a: object

    def as_row(self):
        d = asdict(self)
        return {k: d[k] for k in SAMPLE_FIELDS}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ======================================================================
# 取樣器（背景執行緒；fetch 一律注入）
# ======================================================================
class PcsSampler:
    """
    只讀 EP_PCS 的高頻取樣器。

    ⚠️ fetch 未注入 → 不會做任何事（Fail Closed，絕不自行連線）。
    ⚠️ 取樣失敗仍會寫入一列（sample_ok=False、欄位 None）——
       **絕不跳過、絕不沿用上一筆**，否則時間軸會出現看不見的洞。
    ⚠️ 節奏是 latency-bound：實際間隔一律以 t_req_mono / t_resp_mono 為準，
       分析時**不得**假設 seq × interval。
    """

    def __init__(self, fetch=None, interval_sec=DEFAULT_SAMPLE_INTERVAL_SEC,
                 clock=time.monotonic, wall=None, sink=None, watcher=None, printer=None):
        self.fetch = fetch
        self.interval_sec = interval_sec
        self.clock = clock
        self.wall = wall or (lambda: datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        self.sink = sink
        self.watcher = watcher
        self.printer = printer
        self.samples = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._seq = 0

    # ---- 單次取樣（測試可直接驅動，不需執行緒）----
    def sample_once(self):
        if self.fetch is None:
            raise RuntimeError("PcsSampler.fetch 未注入；本模組不會自行連線")
        with self._lock:
            self._seq += 1
            seq = self._seq
        wall = self.wall()
        t_req = self.clock()
        err = ""
        obs = dict(_EMPTY_OBS)
        ok = False
        try:
            payload = self.fetch()
            if payload is None:
                err = "fetch 回傳 None（GET 失敗或逾時）"
            else:
                obs = observations_from_payload(payload)
                ok = True
        except Exception as e:                                  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        t_resp = self.clock()
        s = PcsSample(seq=seq, wall_clock=wall, t_req_mono=t_req, t_resp_mono=t_resp,
                      latency_sec=t_resp - t_req, sample_ok=ok, error=err, **obs)
        with self._lock:
            self.samples.append(s)
        if self.sink is not None:
            self.sink(s)
        if self.printer is not None:
            self.printer(s)
        if self.watcher is not None:
            self.watcher(s)
        return s

    # ---- 背景執行緒 ----
    def _loop(self):
        while not self._stop.is_set():
            try:
                s = self.sample_once()
                remain = self.interval_sec - s.latency_sec
            except Exception:                                   # noqa: BLE001
                remain = self.interval_sec
            if remain > 0:
                self._stop.wait(remain)

    def start(self):
        if self.fetch is None:
            raise RuntimeError("PcsSampler.fetch 未注入；本模組不會自行連線")
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pcs-sampler", daemon=True)
        self._thread.start()

    def stop(self, join_timeout=5.0):
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None:
            t.join(join_timeout)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self):
        with self._lock:
            return list(self.samples)

    def last_seq(self):
        with self._lock:
            return self._seq


# ======================================================================
# Baseline（Stage 0）
# ======================================================================
@dataclass(frozen=True)
class Baseline:
    valid: bool
    reason: str
    total_count: int = 0
    ok_count: int = 0
    p_base: object = None
    noise_band: object = None
    p_min: object = None
    p_max: object = None
    lat_min: object = None
    lat_median: object = None
    lat_p95: object = None
    lat_max: object = None
    state_baseline: str = ""
    state_changes: int = 0
    states_seen: tuple = ()


def _median(xs):
    ys = sorted(xs)
    n = len(ys)
    if n == 0:
        return None
    m = n // 2
    return ys[m] if n % 2 else (ys[m - 1] + ys[m]) / 2.0


def _percentile(xs, q):
    """最近秩法（不內插）；量測用途足夠且不會造出資料裡沒有的值。"""
    ys = sorted(xs)
    if not ys:
        return None
    k = max(0, min(len(ys) - 1, int(math.ceil(q * len(ys))) - 1))
    return ys[k]


def compute_baseline(samples, min_samples=MIN_BASELINE_SAMPLES):
    """
    由 Stage 0 的 idle 樣本推導功率雜訊帶與 latency 分布。

    🔴 noise_band 一律由實測推導，**不得**預先寫死門檻。
    """
    total = len(samples)
    ok = [s for s in samples if s.sample_ok and _is_num(s.actual_active_power_kw)]
    if len(ok) < min_samples:
        return Baseline(False, f"BASELINE_INSUFFICIENT（有效樣本 {len(ok)} < {min_samples}）",
                        total_count=total, ok_count=len(ok))
    powers = [s.actual_active_power_kw for s in ok]
    lats = [s.latency_sec for s in ok]
    p_base = _median(powers)
    noise = max(abs(p - p_base) for p in powers)
    states = [s.pcs_state for s in ok]
    changes = sum(1 for a, b in zip(states, states[1:]) if a != b)
    uniq = tuple(sorted(set(states)))
    return Baseline(
        True, "OK", total_count=total, ok_count=len(ok),
        p_base=p_base, noise_band=noise, p_min=min(powers), p_max=max(powers),
        lat_min=min(lats), lat_median=_median(lats),
        lat_p95=_percentile(lats, 0.95), lat_max=max(lats),
        state_baseline=(states[0] if len(uniq) == 1 else "MIXED"),
        state_changes=changes, states_seen=uniq,
    )


# ======================================================================
# ramp-up / STOP decay 分析（一律以區間回報，不假裝單點）
# ======================================================================
def observed_quantum(samples):
    """
    全樣本中最小的非零功率差 —— 即後端回報值的實際解析度。
    完全由資料導出，不引入任何發明的門檻。
    """
    vals = sorted({s.actual_active_power_kw for s in samples
                   if s.sample_ok and _is_num(s.actual_active_power_kw)})
    diffs = [b - a for a, b in zip(vals, vals[1:]) if b - a > 0]
    return min(diffs) if diffs else None


# IEEE754 比較保護：容差比較是等號邊界（range <= tol），二進位浮點誤差
# （例如 7.01-6.99 = 0.020000000000000018）會讓判定不穩定。
# 這是浮點比較保護，**不是**物理門檻：1e-9 kW = 1e-6 W。
_FP_GUARD = 1e-9


def _le(a, b):
    """a <= b（含 IEEE754 比較保護）。"""
    return a <= b + abs(b) * _FP_GUARD + _FP_GUARD


TOL_NOISE = "IDLE_NOISE_BAND"
TOL_QUANTUM = "OBSERVED_QUANTUM"
TOL_STRICT = "ZERO_STRICT"


def effective_tolerance(baseline, segment):
    """
    steady / at-rest 判定用的容差。

    🔴 idle 期間功率若完全恆定（例如 HMI idle 一律回 0.00），noise_band 會是 0；
       此時若硬用 0 當容差，運轉中的正常擺動會讓 steady 永遠不成立。
       改以「實測解析度」（最小非零差）替代 —— 仍然 100% 由資料導出。

    ⚠️ segment 必須是**本次命令之後的區段**，不是全體樣本：
       用全體樣本會讓容差隨 session 累積而漂移，同一份資料在不同階段得到不同結論。
       相關的解析度是這次轉態自身的，不是別的階段的。
    回傳 (tolerance, source)，source 一律寫進分析結果供人檢視。
    """
    if baseline.noise_band and baseline.noise_band > 0:
        return baseline.noise_band, TOL_NOISE
    q = observed_quantum(segment)
    if q is not None and q > 0:
        return q, TOL_QUANTUM
    return 0.0, TOL_STRICT


def _moved(s, baseline, tol):
    """
    該樣本是否已脫離 baseline（狀態改變 或 功率超出容差）。

    ⚠️ 這是 **onset（開始反應）** 的判準，刻意保持敏感：狀態改變本身就是設備已回應
       的證據。**不得**用它判定 steady —— steady 另有幅度條件（見 _reached_target）。
    """
    if s.pcs_state != baseline.state_baseline:
        return True
    return _is_num(s.actual_active_power_kw) and \
        not _le(abs(s.actual_active_power_kw - baseline.p_base), tol)


def reach_tolerance(tol, target_power_kw):
    """
    「已到達指令幅度」的容差 —— 由實測量測誤差（tol）與指令目標共同推導。
    tol 本身已是 max(idle noise_band, 實測解析度)。
    """
    if not _is_num(target_power_kw) or target_power_kw <= 0:
        return None
    return max(tol, STEADY_REACH_REL * abs(target_power_kw))


def _reached_target(win, action, target_power_kw, tol, baseline):
    """
    窗內功率是否確實達到「指令要求的方向與幅度」。回傳 (ok, reason)。

    五個條件（缺一不可）：
      1. 窗內狀態一致且等於該 action 的預期運轉狀態
      2. 對應旗標為 True（charge → C；discharge → D）
      3. 方向符合 Phase 6.2 observed-power sign convention
      4. |median(P)| 與 |target| 的差在 reach_tolerance 內
      5. 已明確脫離 idle baseline（避免 target 極小時退化）
    """
    want_state = EXPECTED_RUNNING_STATE.get(action)
    if want_state is None:
        return False, "ACTION_UNKNOWN"
    if not _is_num(target_power_kw) or target_power_kw <= 0:
        # 🔴 不知道指令幅度就無法宣稱「已到達」 → Fail Closed
        return False, "TARGET_POWER_UNKNOWN"
    if any(x.pcs_state != want_state for x in win):
        return False, "STATE_NOT_EXPECTED"
    flag = "pcs_charging_flag" if action == ACT_CHARGE else "pcs_discharging_flag"
    if any(getattr(x, flag) is not True for x in win):
        return False, "FLAG_NOT_SET"
    ps = [x.actual_active_power_kw for x in win]
    med = _median(ps)
    sign = OBSERVED_POWER_SIGN[action]
    if med == 0 or (med > 0) != (sign > 0):
        return False, "POWER_DIRECTION_MISMATCH"
    rt = reach_tolerance(tol, target_power_kw)
    if abs(abs(med) - abs(target_power_kw)) > rt + _FP_GUARD:
        return False, "POWER_MAGNITUDE_NOT_REACHED"
    if _le(abs(med - baseline.p_base), tol):
        return False, "POWER_STILL_AT_BASELINE"
    return True, "OK"


def _at_baseline(win, baseline, tol):
    """窗內功率是否已回到 idle baseline 帶內（STOP 用）。"""
    return all(_is_num(x.actual_active_power_kw)
               and _le(abs(x.actual_active_power_kw - baseline.p_base), tol) for x in win)


def _window_stable(win, tol):
    """尾窗內：全部取樣成功、狀態恆定、功率極差 ≤ 容差。"""
    if any((not s.sample_ok) or (not _is_num(s.actual_active_power_kw)) for s in win):
        return False
    if len({s.pcs_state for s in win}) != 1:
        return False
    ps = [s.actual_active_power_kw for s in win]
    return _le(max(ps) - min(ps), tol)


def analyze_ramp(samples, baseline, t_cmd_sent, t_cmd_returned, window,
                 action=None, target_power_kw=None):
    """
    ramp-up 分析（單一 window 長度）。

    onset  = 第一個脫離 baseline 的樣本（狀態改變 或 功率超出容差；刻意敏感）
    steady = 最早一個尾窗，同時滿足：
               窗內平穩（極差 ≤ tol）
               **且** _reached_target()：狀態／旗標／方向／幅度／已離開 baseline

    🔴 Phase C.1：steady **不再**只因 state 改變就成立。
       實機證實 AC 讀值可能比旗標晚 0~1 個 refresh cycle，
       「CHARGING 但 AC 仍在 baseline 且平坦」曾被誤判為 steady。

    ⚠️ 未提供 action / target_power_kw → 無法判斷「是否已達指令幅度」→
       一律不宣告 steady（reason=STEADY_NEEDS_TARGET），Fail Closed。

    時間一律回報區間：
        upper = 樣本 t_resp_mono − t_cmd_sent_mono      （最保守；用於推導 timeout）
        lower = 樣本 t_req_mono  − t_cmd_returned_mono
    """
    out = {"window": window, "tolerance": None, "tolerance_source": "",
           "reach_tolerance": None, "action": action, "target_power_kw": target_power_kw,
           "onset_seq": None, "onset_upper": None, "onset_lower": None,
           "steady_seq": None, "steady_state": None, "steady_power_kw": None,
           "ramp_up_upper": None, "ramp_up_lower": None,
           "steady_reject_reason": "", "reason": ""}
    if not baseline.valid:
        out["reason"] = "BASELINE_INVALID"
        return out
    after = [s for s in samples if s.t_req_mono >= t_cmd_sent]
    if not after:
        out["reason"] = "NO_SAMPLE_AFTER_COMMAND"
        return out
    tol, tsrc = effective_tolerance(baseline, after)
    out["tolerance"], out["tolerance_source"] = tol, tsrc
    out["reach_tolerance"] = reach_tolerance(tol, target_power_kw)

    for s in after:
        if s.sample_ok and _moved(s, baseline, tol):
            out["onset_seq"] = s.seq
            out["onset_upper"] = s.t_resp_mono - t_cmd_sent
            out["onset_lower"] = s.t_req_mono - t_cmd_returned
            break

    if action not in EXPECTED_RUNNING_STATE or not _is_num(target_power_kw) \
            or target_power_kw <= 0:
        out["reason"] = "STEADY_NEEDS_TARGET"
        out["steady_reject_reason"] = ("ACTION_UNKNOWN"
                                       if action not in EXPECTED_RUNNING_STATE
                                       else "TARGET_POWER_UNKNOWN")
        return out

    last_reject = ""
    for i in range(len(after) - window + 1):
        win = after[i:i + window]
        if not _window_stable(win, tol):
            last_reject = last_reject or "WINDOW_NOT_FLAT"
            continue
        ok, why = _reached_target(win, action, target_power_kw, tol, baseline)
        if not ok:
            last_reject = why
            continue
        s0 = win[0]
        ps = [s.actual_active_power_kw for s in win]
        out["steady_seq"] = s0.seq
        out["steady_state"] = s0.pcs_state
        out["steady_power_kw"] = _median(ps)
        out["ramp_up_upper"] = s0.t_resp_mono - t_cmd_sent
        out["ramp_up_lower"] = s0.t_req_mono - t_cmd_returned
        break
    if out["steady_seq"] is None:
        out["reason"] = "NO_STEADY_WINDOW"
        out["steady_reject_reason"] = last_reject
    return out


def analyze_stop(samples, baseline, t_stop_sent, t_stop_returned, window):
    """
    STOP decay 分析。

    完全停止判據（同時成立，且維持一個尾窗）：
        pcs_charging_flag is False 且 pcs_discharging_flag is False
            （**不接受 None** —— None 代表無法判定，不是「已停」）
        |P − P_base| ≤ noise_band（回落至 idle 雜訊帶）
    ⚠️ pcs_standby_flag 只記錄，不作為停止判據。
    """
    out = {"window": window, "tolerance": None, "tolerance_source": "",
           "decay_onset_seq": None, "decay_onset_upper": None,
           "stopped_seq": None, "stopped_state": None,
           "stop_decay_upper": None, "stop_decay_lower": None, "reason": ""}
    if not baseline.valid:
        out["reason"] = "BASELINE_INVALID"
        return out
    after = [s for s in samples if s.t_req_mono >= t_stop_sent]
    if not after:
        out["reason"] = "NO_SAMPLE_AFTER_COMMAND"
        return out
    tol, tsrc = effective_tolerance(baseline, after)
    out["tolerance"], out["tolerance_source"] = tol, tsrc

    def _at_rest(s):
        # 🔴 Phase C.1：除旗標與功率外，另要求狀態確實是「已離開充放電」。
        #    只看旗標而不看狀態，會讓 UNKNOWN（證據不足）被當成已停。
        return (s.sample_ok
                and s.pcs_state in STOPPED_STATES
                and s.pcs_charging_flag is False and s.pcs_discharging_flag is False
                and _is_num(s.actual_active_power_kw)
                and _le(abs(s.actual_active_power_kw - baseline.p_base), tol))

    for s in after:
        if s.sample_ok and _is_num(s.actual_active_power_kw) and \
                _le(abs(s.actual_active_power_kw - baseline.p_base), tol):
            out["decay_onset_seq"] = s.seq
            out["decay_onset_upper"] = s.t_resp_mono - t_stop_sent
            break

    for i in range(len(after) - window + 1):
        win = after[i:i + window]
        if all(_at_rest(s) for s in win):
            s0 = win[0]
            out["stopped_seq"] = s0.seq
            out["stopped_state"] = s0.pcs_state
            out["stop_decay_upper"] = s0.t_resp_mono - t_stop_sent
            out["stop_decay_lower"] = s0.t_req_mono - t_stop_returned
            break
    if out["stopped_seq"] is None:
        out["reason"] = "NO_STOPPED_WINDOW"
    return out


def analyze_multi(fn, *args, windows=STEADY_WINDOWS, **kw):
    """同一份資料用多組尾窗各算一次；結果不穩健就代表資料不足，不挑好看的。"""
    return [fn(*args, window=w, **kw) for w in windows]


def count_state_changes(samples):
    ok = [s.pcs_state for s in samples if s.sample_ok]
    return sum(1 for a, b in zip(ok, ok[1:]) if a != b)


# ======================================================================
# Stage 閘門（人工逐段確認；confirmer 未注入 → 一律 False）
# ======================================================================
CONFIRM_TOKEN = "YES"
MAX_EMPTY_RETRY = 3

R_CONFIRMED = "CONFIRMED"
R_NOT_INJECTED = "CONFIRMER_NOT_INJECTED"
R_DECLINED = "DECLINED"
R_EOF = "DECLINED_EOF"
R_INTERRUPT = "DECLINED_INTERRUPT"
R_EMPTY_NON_TTY = "DECLINED_EMPTY_NON_TTY"
R_EMPTY_LIMIT = "DECLINED_EMPTY_LIMIT"


def drain_stdin(stream=None):
    """
    丟棄「提示出現之前」就已經在鍵盤緩衝區裡的殘留輸入。

    🔴 這是第一次實機量測 Stage 1~6 全部 DECLINED 的直接對策：
       觀察段以「按 Enter」結束，使用者在樣本刷屏時很自然會多按幾次 Enter；
       多出來的換行留在緩衝區，被**下一個** input() 讀成空字串，
       而空字串 != "YES" → 該 Stage 立刻 DECLINED，並依序吃掉後面每一個 Stage。

    只在 stdin 為 TTY 時動作；非互動環境不做任何事（也就不會遮蔽 EOF）。
    回傳丟棄的字元數；POSIX 路徑無法計數，回 -1。
    """
    st = stream or sys.stdin
    try:
        if not st.isatty():
            return 0
    except Exception:                                           # noqa: BLE001
        return 0
    try:
        import msvcrt                                           # noqa: PLC0415
    except ImportError:
        try:
            import termios                                      # noqa: PLC0415
            termios.tcflush(st, termios.TCIFLUSH)
            return -1
        except Exception:                                       # noqa: BLE001
            return 0
    n = 0
    try:
        while msvcrt.kbhit():
            msvcrt.getwch()
            n += 1
    except Exception:                                           # noqa: BLE001
        pass
    return n


class ConsoleIO:
    """
    stdin / stdout 的唯一入口。

    🔴 為什麼需要它
       背景取樣執行緒每秒往 console 印一行，人工確認又要在同一個 console 打字。
       共用 stdin/stdout 造成兩個真實故障：
         1. 提示被樣本捲走，看起來像沒反應 → 使用者多按 Enter
         2. 多按的 Enter 留在緩衝區，被下一個 input() 當成「答案」讀走

    🔴 三個對策（都**不降低**人工確認要求）
       1. 確認提示期間暫停**畫面輸出** —— 只停顯示，CSV 照常逐列寫入
       2. 每次確認提示前 drain stdin，殘留鍵入不可能被當成答案
       3. 空白行不再代表任何意思：一律重新提示。
          「同意」永遠只有完整輸入 YES 一種方式；EOF / 非空白的其他輸入一律拒絕。

    ⚠️ 全程只有主執行緒讀 stdin。背景取樣執行緒**不讀** stdin（有靜態測試守門）。
    """

    def __init__(self, reader=None, writer=None, drain=None,
                 isatty=None, max_empty_retry=MAX_EMPTY_RETRY):
        # 延後解析 builtins，讓測試可以 monkeypatch
        self._reader = reader or (lambda p: input(p))
        self._writer = writer or (lambda t: print(t))
        self._drain = drain or drain_stdin
        self._isatty = isatty or self._default_isatty
        self._max_empty_retry = max_empty_retry
        self._lock = threading.RLock()
        self._prompt_active = threading.Event()
        self.suppressed_lines = 0
        self.drained_total = 0

    @staticmethod
    def _default_isatty():
        try:
            return sys.stdin.isatty()
        except Exception:                                       # noqa: BLE001
            return False

    @property
    def prompt_active(self):
        return self._prompt_active.is_set()

    def write_line(self, text):
        """樣本／進度輸出。確認提示進行中一律不印（不蓋提示、不誘發亂按）。"""
        if self._prompt_active.is_set():
            self.suppressed_lines += 1
            return False
        with self._lock:
            self._writer(text)
        return True

    def ask(self, header, token=CONFIRM_TOKEN):
        """
        人工確認。回傳 (ok, reason)。

        只有完整輸入 token 才算同意。空白行不是答案（重新提示），
        EOF / 中斷 / 其他任何輸入一律 fail closed。
        """
        self._prompt_active.set()
        try:
            with self._lock:
                self._writer("")
                self._writer(header)
            dropped = self._drain()
            if dropped:
                self.drained_total += (dropped if dropped > 0 else 1)
                with self._lock:
                    self._writer(f"  （已清除提示前的殘留鍵入：{dropped}）")
            for _ in range(self._max_empty_retry + 1):
                try:
                    ans = self._reader(f"  完整輸入 {token} 繼續，其他任意內容中止 > ")
                except EOFError:
                    return False, R_EOF
                except KeyboardInterrupt:
                    return False, R_INTERRUPT
                except Exception as e:                          # noqa: BLE001
                    return False, f"DECLINED_EXCEPTION:{type(e).__name__}"
                a = (ans or "").strip()
                if a == token:
                    return True, R_CONFIRMED
                if a == "":
                    # 空白不是答案 —— 不得因此靜默拒絕，也不得因此自動通過
                    if not self._isatty():
                        return False, R_EMPTY_NON_TTY
                    with self._lock:
                        self._writer(f"  （空白不算答案：要繼續請完整輸入 {token}；"
                                     f"要中止請輸入其他任意內容）")
                    continue
                return False, f"{R_DECLINED}:{a[:20]!r}"
            return False, R_EMPTY_LIMIT
        finally:
            self._prompt_active.clear()

    def confirm(self, prompt):
        return self.ask(prompt)

    def observe(self, msg):
        """
        觀察段：畫面**保持輸出**（人要看樣本才能判斷何時穩定），按 Enter 結束。
        結束後立刻 drain —— 多按的 Enter 不會流進下一個確認提示。
        """
        with self._lock:
            self._writer(f"\n  >>> {msg}（取樣持續中）按 Enter 結束本段 ... ")
        try:
            self._reader("")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            dropped = self._drain()
            if dropped:
                self.drained_total += (dropped if dropped > 0 else 1)
        return True


class StageGate:
    def __init__(self, confirmer=None):
        self.confirmer = confirmer
        self.history = []
        self.last_reason = R_NOT_INJECTED

    def require(self, stage, prompt):
        """confirmer 可回 bool 或 (bool, reason)；未注入或任何例外一律 False。"""
        if self.confirmer is None:
            self.last_reason = R_NOT_INJECTED
            self.history.append((stage, False, R_NOT_INJECTED))
            return False
        try:
            res = self.confirmer(f"[{stage}] {prompt}")
        except Exception as e:                                  # noqa: BLE001
            self.last_reason = f"CONFIRMER_EXCEPTION:{type(e).__name__}"
            self.history.append((stage, False, self.last_reason))
            return False
        if isinstance(res, tuple) and len(res) == 2:
            ok, reason = bool(res[0]), str(res[1])
        else:
            ok = bool(res)
            reason = R_CONFIRMED if ok else R_DECLINED
        self.last_reason = reason
        self.history.append((stage, ok, reason))
        return ok


_DEFAULT_CONSOLE = ConsoleIO()


def console_confirmer(prompt):
    """互動確認：必須完整輸入 YES（大小寫相符）才算同意。"""
    ok, _reason = _DEFAULT_CONSOLE.confirm(prompt)
    return ok


# ======================================================================
# 結果型別
# ======================================================================
@dataclass(frozen=True)
class ControlLegAuthorization:
    """
    單一 control leg 的授權憑證。

    🔴 action-bound：授權 charge 就**只能**送 charge，不得拿去送 discharge。
    🔴 one-shot：dispatch 一經嘗試即消耗，不得沿用到下一個 leg。
    🔴 只是「系統已判定允許」，**不是**放行令 —— 真正 dispatch 前還會再做一次
       fresh revalidation（避免 TOCTOU）。
    """
    action: str                       # operator action（pcs_charge / pcs_discharge）
    authority_state: str
    authority_reason: str
    authorized: bool
    evaluated_at: float               # monotonic
    snapshot_age_sec: object = None
    pcs_state: str = None
    schedule_switch: object = None
    detail: str = ""

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CommandResult:
    action: str
    outcome: str
    sent: bool
    power_requested_kw: object = None
    t_cmd_sent_mono: object = None
    t_cmd_returned_mono: object = None
    control_success: object = None
    detail: str = ""


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    detail: str = ""
    data: dict = field(default_factory=dict)


def valid_power(v):
    """量測功率必須為有限正數；None / bool / NaN / inf / ≤0 一律不合法。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) and v > 0


# ======================================================================
# 量測 Session
# ======================================================================
class MeasurementSession:
    """
    Stage 0~6 的協調者。

    🔴 execute 閘門：self.execute 為 False 時 _dispatch 首行即 return，
       operator **不會被呼叫**。
    🔴 abort 閂鎖：CONFLICT / PCS fault / operator 例外 / Safety 擋下 → 之後不再送任何命令。
       但**取樣不停**（abort 當下的持續時間本身是關鍵證據）。
    🔴 本類別不會自動 Recovery STOP，也不會建立 LastControl。
    """

    def __init__(self, power_kw=None, execute=False, sampler=None, gate=None,
                 read_all_fn=None, mode_state_reader=None, operator_run=None,
                 clock=time.monotonic, event_sink=None, printer=None,
                 pcs_reader=None, authority_policy=None, readback_config=None,
                 last_control_store=None, sleeper=time.sleep):
        self.power_kw = power_kw
        self.execute = bool(execute)
        self.sampler = sampler
        self.gate = gate or StageGate()
        self.read_all_fn = read_all_fn
        self.mode_state_reader = mode_state_reader
        self.operator_run = operator_run
        self.clock = clock
        self.event_sink = event_sink
        self.printer = printer or (lambda *_a, **_k: None)

        # ---- Phase A：Control Authority 實機保護（全部注入，未注入即不動作）----
        self.pcs_reader = pcs_reader                  # () -> observations dict（供 ReadBack）
        self.require_authority_for_stop = True        # 🔴 不得關閉（測試鎖住預設值）
        self.authority_policy = authority_policy      # FIELD TEST ONLY
        self.readback_config = readback_config        # FIELD TEST ONLY
        self.last_control_store = last_control_store  # LastControlStore（由 main 注入）
        self.sleeper = sleeper
        self.authority = None                         # 最近一次 Authority 判定
        self.verified = {}                            # stage -> VerifiedControlResult
        self.lastcontrol_at = None                    # verified LastControl 建立時刻（monotonic）
        self.leg_authorization = None                 # 🔴 per-leg 一次性授權（action-bound）
        self.authorization_log = []                   # 稽核用：每次取用的結果

        self.aborted = False
        self.abort_reason = ""
        self.baseline = None
        self.review_reading = None          # Stage 1/5.1：人工檢視用，非放行依據
        self.safety_reading = None          # Stage 2/5.2：fresh read，Gate 的唯一依據
        self.results = []
        self.analyses = {}

    # ---------------- 內部 ----------------
    def _event(self, stage, event, **kw):
        rec = {"wall_clock": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
               "t_mono": self.clock(),
               "seq_at_event": self.sampler.last_seq() if self.sampler else None,
               "stage": stage, "event": event, "action": kw.get("action"),
               "power_requested_kw": kw.get("power_requested_kw"),
               "t_cmd_sent_mono": kw.get("t_cmd_sent_mono"),
               "t_cmd_returned_mono": kw.get("t_cmd_returned_mono"),
               "control_success": kw.get("control_success"),
               "outcome": kw.get("outcome", ""), "detail": kw.get("detail", "")}
        if self.event_sink is not None:
            self.event_sink(rec)
        return rec

    # ---------------- Control Authority ----------------
    def _last_control(self):
        """取得 LastControl 與其信任層級；store 未注入即 (None, None)。"""
        if self.last_control_store is None:
            return None, None
        try:
            t = self.last_control_store.current()
        except Exception as e:                                  # noqa: BLE001
            self.printer(f"    ⚠️ LastControlStore.current() 例外：{type(e).__name__}")
            return None, None
        return getattr(t, "record", None), getattr(t, "trust", None)

    def evaluate_authority(self, reading, t0, t1, now, mode_state, label=""):
        """
        由 fresh reading 判定 Control Authority。回傳 (AuthorityResult, EssSnapshot)。

        ⚠️ 使用的政策是 FIELD TEST ONLY 的注入值；未注入時採 production default
           （ttl/tolerance 皆 None），運轉中的 PCS 一律 UNKNOWN → BLOCK。
        """
        ess = DE.ess_snapshot_from_reading(reading, t0, t1, now=now)
        lc, trust = self._last_control()
        areq = CA.AuthorityRequest(
            pcs_state=PCI.pcs_state_from_ess(ess),
            actual_active_power_kw=getattr(ess, "actual_active_power_kw", None),
            last_control=lc, last_control_trust=trust,
            pcs_mode_state=mode_state,
            ess_valid=bool(getattr(ess, "valid", False) and not getattr(ess, "stale", True)),
        )
        pol = self.authority_policy if self.authority_policy is not None \
            else CA.DEFAULT_AUTHORITY_POLICY
        auth = CA.evaluate(areq, policy=pol, now=now)
        self.authority = auth
        self.printer(f"    【Control Authority】{auth.state}（{auth.reason}）")
        self.printer(f"      pcs_state = {auth.pcs_state}   observed = {auth.observed_power_kw} kW")
        self.printer(f"      LastControl = {auth.last_control_action} "
                     f"{auth.last_control_power_kw} kW   trust = {trust}   age = {auth.age_sec}")
        self.printer(f"      {auth.detail}")
        self._event(label or "-", "AUTHORITY_RESULT", outcome=f"{auth.state}/{auth.reason}",
                    detail=f"pcs={auth.pcs_state} lc={auth.last_control_action} "
                           f"trust={trust} age={auth.age_sec} obs={auth.observed_power_kw}")
        return auth, ess

    def abort(self, reason, detail=""):
        """設定 abort 閂鎖。⚠️ 不停取樣、不送 STOP、不做任何自動補償。"""
        if not self.aborted:
            self.aborted = True
            self.abort_reason = reason
            self._event("-", "ABORT", outcome=reason, detail=detail)
            self.printer(f"  🔴 ABORT：{reason} —— {detail}")
            self.printer("     取樣繼續；後續 Stage 不會執行；是否 STOP 由人工判斷。")

    def watch_sample(self, s):
        """掛在 sampler 上的守望者：CONFLICT / fault 立即中止後續流程。"""
        if s.pcs_state == PCI.PCS_CONFLICT:
            self.abort(AB_CONFLICT, f"seq={s.seq} 三旗標衝突："
                                    f"C={s.pcs_charging_flag} D={s.pcs_discharging_flag} "
                                    f"S={s.pcs_standby_flag}")
        elif s.pcs_fault_flag is True:
            self.abort(AB_FAULT, f"seq={s.seq} pcs_fault_flag=True")

    def _stage(self, stage, prompt, fn):
        """所有 Stage 的唯一入口：先 abort 檢查 → 再人工確認 → 才執行。"""
        if self.aborted:
            r = StageResult(stage, ST_ABORTED, f"已中止（{self.abort_reason}），不執行")
            self._event(stage, "STAGE_SKIPPED", outcome=ST_ABORTED, detail=self.abort_reason)
            self.results.append(r)
            return r
        if not self.gate.require(stage, prompt):
            # 一定要把「為什麼沒確認」帶出來 —— 否則 EOF / 空白 / 打錯 / 例外
            # 在報告上長得一模一樣，無法診斷（第一次實機量測就吃了這個虧）。
            why = self.gate.last_reason
            r = StageResult(stage, ST_DECLINED, f"未取得人工確認（{why}）")
            self._event(stage, "STAGE_DECLINED", outcome=ST_DECLINED, detail=why)
            self.results.append(r)
            return r
        self._event(stage, "STAGE_BEGIN")
        r = fn()
        self._event(stage, "STAGE_END", outcome=r.status, detail=r.detail)
        self.results.append(r)
        return r

    # ---------------- 控制命令派送 ----------------
    def _dispatch(self, stage, action, power_kw):
        # 🔴 硬閘門 #1：execute 為 False → operator 連呼叫都不會發生
        if not self.execute:
            res = CommandResult(action, CMD_NO_EXECUTE, False, power_kw,
                                detail="execute=False：未呼叫 operator，未送出任何命令")
            self._event(stage, "COMMAND_SUPPRESSED", action=action,
                        power_requested_kw=power_kw, outcome=CMD_NO_EXECUTE,
                        detail=res.detail)
            self.printer(f"  [dry-run] {action} 未送出（execute=False）")
            return res
        # 🔴 硬閘門 #2：abort 後不再送任何命令
        if self.aborted:
            res = CommandResult(action, CMD_ABORTED, False, power_kw, detail=self.abort_reason)
            self._event(stage, "COMMAND_SUPPRESSED", action=action, outcome=CMD_ABORTED,
                        detail=self.abort_reason)
            return res
        # 🔴 硬閘門 #3：需要功率的 action 必須有合法功率（無預設值）
        if action in _REQUIRES_POWER and not valid_power(power_kw):
            res = CommandResult(action, CMD_POWER_NOT_SPECIFIED, False, power_kw,
                                detail=f"--power 未提供或不合法：{power_kw!r}")
            self._event(stage, "COMMAND_SUPPRESSED", action=action, outcome=CMD_POWER_NOT_SPECIFIED,
                        detail=res.detail)
            self.abort(AB_MANUAL, res.detail)
            return res
        # 🔴 硬閘門 #4：operator 未注入 → 不做任何事
        if self.operator_run is None:
            res = CommandResult(action, CMD_OPERATOR_MISSING, False, power_kw,
                                detail="operator_run 未注入；本模組不會自行控制設備")
            self._event(stage, "COMMAND_SUPPRESSED", action=action, outcome=CMD_OPERATOR_MISSING,
                        detail=res.detail)
            return res

        kwargs = {"action": action, "execute": self.execute, "no_verify": True}
        if action in _REQUIRES_POWER:
            kwargs["power"] = power_kw            # STOP 不帶 power

        t_sent = self.clock()
        self._event(stage, "COMMAND_SENT", action=action, power_requested_kw=power_kw,
                    t_cmd_sent_mono=t_sent)
        try:
            rec = self.operator_run(**kwargs)
        except Exception as e:                                  # noqa: BLE001
            t_ret = self.clock()
            detail = f"{type(e).__name__}: {e}"
            self._event(stage, "COMMAND_EXCEPTION", action=action,
                        t_cmd_sent_mono=t_sent, t_cmd_returned_mono=t_ret,
                        outcome=CMD_EXCEPTION, detail=detail)
            self.abort(AB_OPERATOR, detail)
            return CommandResult(action, CMD_EXCEPTION, False, power_kw, t_sent, t_ret,
                                 detail=detail)
        t_ret = self.clock()
        # 只讀 control_success —— 絕不讀 record 的整體結果（會被 partial verify 汙染）
        cs = rec.get("control_success") if isinstance(rec, dict) else None
        outcome = CMD_SENT if cs is True else CMD_CONTROL_FAILED
        self._event(stage, "COMMAND_RETURNED", action=action, power_requested_kw=power_kw,
                    t_cmd_sent_mono=t_sent, t_cmd_returned_mono=t_ret,
                    control_success=cs, outcome=outcome)
        if outcome == CMD_CONTROL_FAILED:
            self.abort(AB_OPERATOR, f"{action} control_success={cs!r}")
        return CommandResult(action, outcome, True, power_kw, t_sent, t_ret, cs)

    # ---------------- Stage 0 ----------------
    def stage0_baseline(self, hold=None):
        def _run():
            if self.sampler is None:
                return StageResult("Stage 0", ST_NOT_READY, "sampler 未注入")
            if not self.sampler.running:
                self.sampler.start()
            self.printer("  Stage 0：唯讀取樣中 —— 建立 latency / idle power / noise band / flag baseline")
            if hold is not None:
                hold()                      # 人工結束（互動時為等待輸入）
            bl = compute_baseline(self.sampler.snapshot())
            self.baseline = bl
            if not bl.valid:
                # Fail Closed：沒有 baseline 就無法定義「開始反應」與「穩定」，
                # 送出去的 CHARGE/DISCHARGE 將無法分析 —— 不做無法分析的通電。
                self.abort(AB_BASELINE, bl.reason)
                return StageResult("Stage 0", ST_INCONCLUSIVE, bl.reason, {"baseline": asdict(bl)})
            self.printer(f"    P_base={bl.p_base:+.3f} kW  noise_band={bl.noise_band:.3f} kW  "
                         f"latency med={bl.lat_median:.3f}s p95={bl.lat_p95:.3f}s "
                         f"max={bl.lat_max:.3f}s")
            self.printer(f"    flag baseline={bl.state_baseline}  state_changes={bl.state_changes}  "
                         f"states_seen={list(bl.states_seen)}")
            if bl.lat_p95 is not None and bl.lat_p95 > self.sampler.interval_sec:
                self.printer("    ⚠️ p95 latency > 目標間隔 → 實際為 latency-bound 取樣，"
                             "分析請一律用實際時間戳")
            return StageResult("Stage 0", ST_OK, "baseline 建立完成", {"baseline": asdict(bl)})
        return self._stage("Stage 0", "開始唯讀 baseline 取樣（不送任何命令）？", _run)

    # ---------------- Stage 1 ----------------
    def stage1_read_all(self, label="Stage 1"):
        """
        🔵 人工檢視用的快照 —— **不是**控制放行依據。

        這份 reading 只給人看設備現況。從它讀出來到人工把 YES 打完，
        中間會經過任意長的閱讀／思考時間，snapshot 必然變舊。
        真正用來放行的資料一律由 Stage 2 在收到 YES 之後重新讀取。
        """
        def _run():
            if self.read_all_fn is None:
                return StageResult(label, ST_NOT_READY, "read_all_fn 未注入")
            t0 = self.clock()
            try:
                reading = self.read_all_fn()
            except Exception as e:                              # noqa: BLE001
                self.abort(AB_MANUAL, f"read_all 例外：{type(e).__name__}: {e}")
                return StageResult(label, ST_INCONCLUSIVE, f"read_all 例外：{type(e).__name__}")
            t1 = self.clock()
            if not isinstance(reading, dict):
                self.abort(AB_MANUAL, "read_all 未回傳 dict")
                return StageResult(label, ST_INCONCLUSIVE, "read_all 未回傳 dict")
            self.review_reading = (reading, t0, t1)
            alarm = SG.alarm_source_complete_from_reading(reading)
            self.printer(f"    【人工檢視快照】不作最終控制放行依據；"
                         f"Safety Gate 會在收到 YES 後重新讀取最新資料")
            self.printer(f"    read_all 耗時 {t1 - t0:.2f}s")
            self.printer(f"    SOC={reading.get('soc_percent')}%  "
                         f"電池={reading.get('battery_power_status')!r}  "
                         f"通訊={reading.get('communication_ok')}  "
                         f"PCS故障={reading.get('pcs_fault_flag')}")
            self.printer(f"    PCS flags  C={reading.get('pcs_charging_flag')} "
                         f"D={reading.get('pcs_discharging_flag')} "
                         f"S={reading.get('pcs_standby_flag')} "
                         f"R={reading.get('pcs_running_flag')}  "
                         f"P={reading.get('actual_active_power_kw')} kW")
            self.printer(f"    告警來源完整性：{alarm.complete}（{alarm.reason}）"
                         f" total={alarm.total} fetched={alarm.fetched}")
            self.printer(f"    _fail={reading.get('_fail')}")
            return StageResult(label, ST_OK, "read_all 完成",
                               {"read_duration_sec": t1 - t0,
                                "alarm_source": asdict(alarm)})
        return self._stage(label, "執行 read_all() 一次，確認完整 ESS 狀態？", _run)

    # ---------------- Stage 2 ----------------
    def stage2_safety(self, action, label="Stage 2"):
        """
        🔴 控制放行依據 —— 在**收到人工 YES 之後**才重新讀取。

        為什麼一定要重讀（實機量測踩過的坑）
            Stage 1 的 read_all 只花 0.52s，資料完全正常；但人工閱讀畫面、
            輸入 YES 花了 40 秒，等 Safety Gate 執行時 snapshot age 已達 41.7s
            > ESS_STALE_AFTER_SEC(15) → STALE → DENY。
            Gate 的判定是對的，錯的是「fresh read → 長時間等人 → 才 safety check」
            這個順序。

        正確順序：人工確認在前 → fresh read 在後 → Safety check 緊接 fresh read。
        ⚠️ 絕不重用 Stage 1 的 reading（有測試鎖死 reader 必須被再呼叫一次）。
        ⚠️ 本修正不放寬 stale 門檻、不 bypass Gate、不自動 YES。
        """
        req_action = SG.REQ_CHARGE if action == ACT_CHARGE else SG.REQ_DISCHARGE

        def _run():
            if self.read_all_fn is None:
                return StageResult(label, ST_NOT_READY, "read_all_fn 未注入")
            # ---- 收到 YES 後才做的 fresh read（_stage 已確保確認在前）----
            t0 = self.clock()
            try:
                reading = self.read_all_fn()
            except Exception as e:                              # noqa: BLE001
                detail = f"fresh read_all 例外：{type(e).__name__}: {e}"
                self._event(label, "SAFETY_RESULT", action=action,
                            outcome="FRESH_READ_FAILED", detail=detail)
                self.abort(AB_SAFETY, detail)
                return StageResult(label, ST_BLOCKED, detail)
            t1 = self.clock()
            if not isinstance(reading, dict):
                detail = "fresh read_all 未回傳 dict"
                self._event(label, "SAFETY_RESULT", action=action,
                            outcome="FRESH_READ_FAILED", detail=detail)
                self.abort(AB_SAFETY, detail)
                return StageResult(label, ST_BLOCKED, detail)
            self.safety_reading = (reading, t0, t1)

            now = self.clock()
            ess = DE.ess_snapshot_from_reading(reading, t0, t1, now=now)
            alarm = SG.alarm_source_complete_from_reading(reading)
            mode_state = None
            if self.mode_state_reader is not None:
                try:
                    mode_state = self.mode_state_reader()
                except Exception as e:                          # noqa: BLE001
                    self.printer(f"    ⚠️ mode_state_reader 例外：{type(e).__name__}")
            self.printer("    【Fresh Safety Snapshot】此份資料才是 Safety Gate 判定依據")
            self.printer(f"      read_all duration = {t1 - t0:.2f}s")
            self.printer(f"      age at gate       = {now - t0:.2f}s"
                         f"（門檻 {DE.ESS_STALE_AFTER_SEC:g}s）")
            self.printer(f"      SOC               = {reading.get('soc_percent')}%")
            self.printer(f"      battery           = {reading.get('battery_power_status')!r}")
            self.printer(f"      communication_ok  = {reading.get('communication_ok')}")
            self.printer(f"      PCS fault         = {reading.get('pcs_fault_flag')}")
            self.printer(f"      alarm complete    = {alarm.complete}（{alarm.reason}）")
            self.printer(f"      PCS mode          = {mode_state}")
            rows = reading.get("alarm_rows")
            req = SG.SafetyRequest(
                requested_action=req_action,
                target_power_kw=self.power_kw,
                ess=ess,
                alarm_rows=tuple(rows) if isinstance(rows, list) else rows,
                alarm_source_complete=alarm.complete,
                pcs_mode_state=mode_state,
            )
            res = SG.SafetyGate().check(req, now=now)
            for c in res.checks:
                self.printer(f"      {'✓' if c.passed else '✗'} {c.name}: {c.detail}")
            self._event(label, "FRESH_SAFETY_READ", action=action,
                        outcome=f"age={now - t0:.3f}s",
                        detail=f"read_duration={t1 - t0:.3f}s soc={reading.get('soc_percent')} "
                               f"batt={reading.get('battery_power_status')!r} "
                               f"comm={reading.get('communication_ok')} "
                               f"alarm_complete={alarm.complete}")
            ok = bool(res.allowed) and res.reason == SG.SAFE_OK
            self._event(label, "SAFETY_RESULT", action=action,
                        power_requested_kw=self.power_kw,
                        outcome=("SAFE_OK" if ok else res.reason), detail=res.detail)
            if not ok:
                self.abort(AB_SAFETY, f"{res.reason}：{res.detail}")
                return StageResult(label, ST_BLOCKED, f"{res.reason}：{res.detail}")
            self.printer("    ✅ Safety Gate: allowed=True / SAFE_OK")

            # ---- Phase A：Control Authority 閘門（實機控制開始前必須為 IDLE）----
            # 🔴 Safety PASS 不等於有控制權。設備若已被其他來源操作，Safety Gate 會放行
            #    （14 項全過），必須由 Authority 擋下。
            # 🔴 先作廢任何殘留授權 —— Stage 2 失敗不得留下可用的放行令
            self.leg_authorization = None
            auth, ess2 = self.evaluate_authority(reading, t0, t1, now, mode_state, label)
            if auth.state != CA.AUTH_IDLE:
                d = f"{auth.state}／{auth.reason}：{auth.detail}"
                self.abort(AB_AUTHORITY, d)
                return StageResult(label, ST_BLOCKED, f"Control Authority 非 IDLE → {d}")
            self.printer("    ✅ Control Authority: IDLE（可開始新的 Phase 6 控制）")
            self.grant_leg_authorization(action, auth, ess2, label)
            return StageResult(label, ST_OK, "SAFE_OK + AUTHORITY_IDLE + LEG_AUTHORIZED")
        return self._stage(label,
                           f"對 {action} 執行 Safety Gate？"
                           f"（收到 YES 後會**重新讀取**最新資料再判定）", _run)

    # ---------------- Stage 3 / 5：送 CHARGE / DISCHARGE ----------------
    # ---------------- Phase A：ReadBack 驗證與 LastControl ----------------
    def _readback(self, stage, action):
        """
        以既有 ReadBackVerifier 驗證命令是否真的生效（**只依旗標／狀態模型**）。

        ⚠️ actual_active_power_kw 不參與 success 判定 —— 實機證實它比旗標晚約一個
           backend refresh cycle，拿它判 success 會造成假 timeout。
        未注入 reader / config → CONFIG_NOT_READY（Fail Closed，不會謊稱成功）。
        """
        if self.pcs_reader is None or self.readback_config is None:
            self._event(stage, "READBACK_SKIPPED", action=action,
                        outcome=EX.CONFIG_NOT_READY,
                        detail="pcs_reader / readback_config 未注入")
            return None
        v = EX.ReadBackVerifier(self.readback_config, reader=self.pcs_reader,
                                clock=self.clock, sleeper=self.sleeper)
        rb = v.verify(CONTROL_ACTION_OF[action])          # 控制層名稱
        self.printer(f"    【ReadBack】{rb.outcome}（{rb.reason}）"
                     f" target={rb.target_state} observed={rb.observed_state}"
                     f" elapsed={rb.elapsed_sec}s attempts={len(rb.attempts)}")
        self._event(stage, "READBACK_RESULT", action=action,
                    outcome=f"{rb.outcome}/{rb.reason}",
                    detail=f"target={rb.target_state} observed={rb.observed_state} "
                           f"elapsed={rb.elapsed_sec} power={rb.actual_active_power_kw}")
        return rb

    def _record_last_control(self, stage, action, power, rb):
        """
        🔴 只有 ReadBack VERIFY_SUCCESS 才允許建立 LastControl。
           POST 回 200 但 read-back 未成功 → **不得**建立 ownership 證據。
        使用既有 LastControlStore / VerifiedControlResult / schema，不另造第二套。
        """
        eligible = rb is not None and rb.outcome == EX.VERIFY_SUCCESS
        vres = EX.VerifiedControlResult(
            control_action=CONTROL_ACTION_OF[action],     # 控制層名稱（LastControl 用）
            target_power_kw=power,
            outcome=(rb.outcome if rb is not None else EX.CONFIG_NOT_READY),
            reason=(rb.reason if rb is not None else "READBACK_NOT_CONFIGURED"),
            operator_result=None, readback_result=rb, lastcontrol_eligible=eligible)
        self.verified[stage] = vres
        if not eligible:
            self._event(stage, "LASTCONTROL_NOT_CREATED", action=action,
                        outcome="NOT_ELIGIBLE",
                        detail=f"outcome={vres.outcome}（僅 VERIFY_SUCCESS 可建立）")
            self.printer("    LastControl：未建立（read-back 未 VERIFY_SUCCESS）")
            return None
        if self.last_control_store is None:
            self._event(stage, "LASTCONTROL_NOT_CREATED", action=action,
                        outcome="STORE_NOT_INJECTED")
            self.printer("    LastControl：未建立（store 未注入）")
            return None
        upd = self.last_control_store.update_from_verified_result(vres)
        self.lastcontrol_at = self.clock()
        self.printer(f"    【LastControl】{upd.outcome}（{upd.reason}） path={upd.path}")
        self._event(stage, "LASTCONTROL_UPDATED", action=action, power_requested_kw=power,
                    outcome=upd.outcome, detail=f"{upd.reason} path={upd.path}")
        return upd

    def _fresh_authority(self, stage, label="複驗"):
        """
        取最新 reading → 重新判定 Control Authority。回傳 (auth, ess, err)。

        🔴 STOP 與 CHARGE/DISCHARGE dispatch 前共用這一條路徑 ——
           兩者都必須用「剛剛才讀到的」資料判定，不得沿用等待人工輸入之前的舊快照。
        """
        if self.read_all_fn is None:
            return None, None, "read_all_fn 未注入"
        t0 = self.clock()
        try:
            reading = self.read_all_fn()
        except Exception as e:                                  # noqa: BLE001
            return None, None, f"fresh read_all 例外：{type(e).__name__}: {e}"
        t1 = self.clock()
        if not isinstance(reading, dict):
            return None, None, "fresh read_all 未回傳 dict"
        now = self.clock()
        mode_state = None
        if self.mode_state_reader is not None:
            try:
                mode_state = self.mode_state_reader()
            except Exception as e:                              # noqa: BLE001
                return None, None, f"mode_state_reader 例外：{type(e).__name__}"
        self.printer(f"    【{label}】fresh read {t1 - t0:.2f}s  age {now - t0:.2f}s")
        auth, ess = self.evaluate_authority(reading, t0, t1, now, mode_state, stage)
        return auth, ess, ""

    def _authority_before_stop(self, stage):
        """
        🔴 STOP 之前必須重新證明「這個運轉中的 operation 確實是本輪 Phase 6 發動的」。
           必須 OWNED_BY_PHASE6 才允許 STOP。
           ⚠️ STOP **不**使用 per-leg authorization —— 它有自己的複驗規則，兩者不互相取代。
        """
        auth, _ess, err = self._fresh_authority(stage, "STOP 前複驗")
        return auth, err

    # ---------------- Phase A.1：per-leg authorization ----------------
    def grant_leg_authorization(self, action, auth, ess, stage=""):
        """Stage 2 判定通過後，發給**這一個 action** 的一次性授權。"""
        a = ControlLegAuthorization(
            action=action, authority_state=auth.state, authority_reason=auth.reason,
            authorized=True, evaluated_at=self.clock(),
            snapshot_age_sec=getattr(ess, "age_sec", None),
            pcs_state=auth.pcs_state, schedule_switch=self._schedule_switch(),
            detail=auth.detail)
        self.leg_authorization = a
        self._event(stage, "LEG_AUTHORIZATION_GRANTED", action=action,
                    outcome=f"{auth.state}/{auth.reason}",
                    detail=f"action-bound、one-shot；pcs={auth.pcs_state}")
        self.printer(f"    【Leg Authorization】已核發給 {action}（一次性、僅限此 action）")
        return a

    def _schedule_switch(self):
        if self.mode_state_reader is None:
            return None
        try:
            m = self.mode_state_reader()
        except Exception:                                       # noqa: BLE001
            return None
        return m.get("schedule_switch") if isinstance(m, dict) else None

    def _consume_leg_authorization(self, stage, action):
        """
        檢查並**消耗**授權。回傳 (authorization, block_reason)。

        🔴 一經取用即消耗 —— 即使後續複驗失敗也不保留，避免殘留可重用的放行令。
        """
        a = self.leg_authorization
        self.leg_authorization = None                # one-shot：先消耗再判定
        if a is None:
            return None, FA_MISSING
        if a.authorized is not True:
            return a, FA_NOT_GRANTED
        if a.action != action:
            return a, FA_ACTION_MISMATCH
        return a, None

    def _enforce_leg_authorization(self, stage, action):
        """
        送 CHARGE / DISCHARGE 前的強制授權檢查。通過回 None，否則回 StageResult。

        兩道：
          1. 取用並消耗本 leg 的 authorization（action-bound、one-shot）
          2. **人工 YES 之後**再做一次 fresh revalidation —— 這是 TOCTOU 的防線：
             判定與 dispatch 之間隔著任意長的人工輸入時間，期間他人可能開始操作。
        """
        a, why = self._consume_leg_authorization(stage, action)
        self.authorization_log.append({"stage": stage, "action": action,
                                       "authorization": a.as_dict() if a else None,
                                       "block_reason": why})
        if why is not None:
            self._event(stage, "LEG_AUTHORIZATION_BLOCKED", action=action, outcome=why,
                        detail=(f"授權 action={a.action!r} 但要求 dispatch {action!r}"
                                if a is not None else "本 leg 未取得任何授權"))
            self.abort(AB_AUTHORITY, why)
            self.printer(f"    🔴 未授權，不送出 {action}：{why}")
            return StageResult(stage, ST_BLOCKED, why,
                               {"authorization": a.as_dict() if a else None})

        # ---- 人工 YES 之後的 fresh revalidation ----
        auth, ess, err = self._fresh_authority(stage, "dispatch 前複驗")
        if auth is None:
            self._event(stage, "LEG_AUTHORIZATION_BLOCKED", action=action,
                        outcome=FA_REVALIDATION_FAILED, detail=err)
            self.abort(AB_AUTHORITY, f"{FA_REVALIDATION_FAILED}：{err}")
            self.printer(f"    🔴 dispatch 前複驗失敗，不送出 {action}：{err}")
            return StageResult(stage, ST_BLOCKED, FA_REVALIDATION_FAILED, {"detail": err})
        if getattr(ess, "stale", False):
            self._event(stage, "LEG_AUTHORIZATION_BLOCKED", action=action, outcome=FA_STALE)
            self.abort(AB_AUTHORITY, FA_STALE)
            return StageResult(stage, ST_BLOCKED, FA_STALE)
        if not auth.allowed:
            d = f"{auth.state}／{auth.reason}：{auth.detail}"
            self._event(stage, "LEG_AUTHORIZATION_BLOCKED", action=action,
                        outcome=FA_REVALIDATION_FAILED, detail=d)
            self.abort(AB_AUTHORITY, f"{FA_REVALIDATION_FAILED}：{d}")
            self.printer(f"    🔴 dispatch 前 Authority 已不允許，不送出 {action}：{d}")
            return StageResult(stage, ST_BLOCKED, FA_REVALIDATION_FAILED, {"detail": d})
        if auth.state != a.authority_state:
            d = f"授權時 {a.authority_state} → 現在 {auth.state}"
            self._event(stage, "LEG_AUTHORIZATION_BLOCKED", action=action,
                        outcome=FA_STATE_CHANGED, detail=d)
            self.abort(AB_AUTHORITY, f"{FA_STATE_CHANGED}：{d}")
            self.printer(f"    🔴 等待確認期間狀態已改變，不送出 {action}：{d}")
            return StageResult(stage, ST_BLOCKED, FA_STATE_CHANGED, {"detail": d})
        self._event(stage, "LEG_AUTHORIZATION_USED", action=action,
                    outcome=f"{auth.state}/{auth.reason}",
                    detail=f"授權於 {a.evaluated_at}，dispatch 前複驗通過")
        self.printer(f"    ✅ Leg Authorization 有效且複驗通過 → 允許 dispatch {action}")
        return None

    def stage_command(self, stage, action, hold=None, analyse=True):
        def _run():
            if self.sampler is None or not self.sampler.running:
                return StageResult(stage, ST_NOT_READY, "取樣器未啟動；不得在無取樣下送命令")

            # ---- Phase A：STOP 前重新驗證 Control Authority ----
            # ⚠️ 只在真的要送命令時才需要授權。execute=False 時 _dispatch 根本不會
            #    呼叫 operator，沒有任何命令需要被授權，因此不做此判定也不會降低安全性。
            if action == ACT_STOP and self.execute and self.require_authority_for_stop:
                auth, err = self._authority_before_stop(stage)
                if auth is None:
                    self.abort(AB_AUTHORITY, err)
                    return StageResult(stage, ST_BLOCKED, f"STOP 前 Authority 無法判定：{err}")
                if auth.state != CA.AUTH_OWNED:
                    d = f"{auth.state}／{auth.reason}：{auth.detail}"
                    self.abort(AB_AUTHORITY, d)
                    return StageResult(stage, ST_BLOCKED,
                                       f"STOP 前 Authority 非 OWNED_BY_PHASE6 → {d}")
                self.printer("    ✅ Control Authority: OWNED_BY_PHASE6（允許本輪 STOP）")

            # ---- Phase A.1：CHARGE / DISCHARGE 的 per-leg authorization enforcement ----
            # 🔴 人工 YES 只代表「同意執行一個系統已判定允許的命令」，
            #    它**不能**建立 Authority。沒有對應授權 → 結構上不可能 dispatch。
            # ⚠️ 只在真的要送命令時要求（execute=False 不會呼叫 operator，無命令可授權）。
            if action in _REQUIRES_POWER and self.execute:
                blocked = self._enforce_leg_authorization(stage, action)
                if blocked is not None:
                    return blocked

            power = self.power_kw if action in _REQUIRES_POWER else None
            cmd = self._dispatch(stage, action, power)

            # ---- Phase A：ReadBack 驗證 → 僅 VERIFY_SUCCESS 才建立 LastControl ----
            if cmd.sent and cmd.outcome == CMD_SENT:
                rb = self._readback(stage, action)
                self._record_last_control(stage, action, power, rb)

            if hold is not None:
                hold()
            data = {"command": asdict(cmd)}
            if analyse and cmd.sent and self.baseline is not None and self.baseline.valid:
                samples = self.sampler.snapshot()
                if action == ACT_STOP:
                    res = analyze_multi(analyze_stop, samples, self.baseline,
                                        cmd.t_cmd_sent_mono, cmd.t_cmd_returned_mono)
                else:
                    # Phase C.1：steady 需要知道 action 與指令幅度才能判定
                    res = analyze_multi(analyze_ramp, samples, self.baseline,
                                        cmd.t_cmd_sent_mono, cmd.t_cmd_returned_mono,
                                        action=action, target_power_kw=power)
                data["analysis"] = res
                self.analyses[stage] = res
                for r in res:
                    self.printer(f"    [window={r['window']}] " +
                                 "  ".join(f"{k}={v}" for k, v in r.items() if k != "window"))
            if self.authority is not None:
                data["authority"] = self.authority.as_dict()
            if stage in self.verified:
                v = self.verified[stage]
                data["readback_outcome"] = v.outcome
                data["lastcontrol_eligible"] = v.lastcontrol_eligible
            if not cmd.sent:
                return StageResult(stage, ST_INCONCLUSIVE, cmd.outcome, data)
            if cmd.outcome != CMD_SENT:
                return StageResult(stage, ST_INCONCLUSIVE, cmd.outcome, data)
            return StageResult(stage, ST_OK, cmd.outcome, data)
        return self._stage(stage, f"送出單次 {action}"
                                  f"{f'（power={self.power_kw} kW）' if action in _REQUIRES_POWER else ''}？",
                           _run)


# ======================================================================
# 輸出（CSV / event log）
# ======================================================================
class RowWriter:
    """逐列 flush 的 CSV writer —— 中途中斷也不會失去已取得的樣本。"""

    def __init__(self, path, fields):
        self.path = path
        self.fields = fields
        self._fh = None
        self._w = None
        self._lock = threading.Lock()

    def open(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._fh = open(self.path, "w", newline="", encoding="utf-8-sig")
        self._w = csv.DictWriter(self._fh, fieldnames=list(self.fields))
        self._w.writeheader()
        self._fh.flush()
        return self

    def write(self, row):
        with self._lock:
            if self._w is None:
                return
            self._w.writerow({k: row.get(k) for k in self.fields})
            self._fh.flush()

    def close(self):
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh, self._w = None, None


def format_sample(s):
    p = s.actual_active_power_kw
    ps = f"{p:+8.3f}" if _is_num(p) else "    n/a "
    return (f"  #{s.seq:<5d} {s.pcs_state:<11s} C={str(s.pcs_charging_flag):<5s} "
            f"D={str(s.pcs_discharging_flag):<5s} S={str(s.pcs_standby_flag):<5s} "
            f"P={ps} kW  lat={s.latency_sec:5.3f}s"
            + ("" if s.sample_ok else f"  ⚠ {s.error}"))


def make_sample_printer(console):
    """
    樣本顯示一律經 ConsoleIO —— 確認提示進行中會被暫停。
    ⚠️ 只暫停「顯示」；sink（寫 CSV）與 watcher 完全不受影響。
    """
    return lambda s: console.write_line(format_sample(s))


# ======================================================================
# CLI
# ======================================================================
def build_arg_parser():
    ap = argparse.ArgumentParser(
        description="Phase 6.5-G 實機量測工具（sampling / CSV / stage control；不自行控制設備）")
    # ⚠️ 沒有 default —— 未提供即 None，由 Fail Closed 檢查擋下
    ap.add_argument("--power", type=float, default=None,
                    help="量測用測試功率（kW）。無預設值；未提供且非 --stage0-only 即拒絕啟動。")
    ap.add_argument("--execute", action="store_true",
                    help="允許實際送出控制命令。未指定時 operator 完全不會被呼叫。")
    ap.add_argument("--stage0-only", action="store_true",
                    help="只做 Stage 0 唯讀取樣（不需 --power）。")
    ap.add_argument("--interval", type=float, default=DEFAULT_SAMPLE_INTERVAL_SEC,
                    help=f"量測取樣目標間隔秒（預設 {DEFAULT_SAMPLE_INTERVAL_SEC}）。"
                         "⚠️ 這不是 production poll_interval_sec。")
    ap.add_argument("--out-dir", default=None, help="輸出目錄（預設 output/phase6_5g_measure）")
    ap.add_argument("--quiet-samples", action="store_true", help="不逐筆列印樣本")
    return ap


def _default_out_dir():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "output", "phase6_5g_measure")


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    # ---- Fail Closed：--power ----
    if not args.stage0_only:
        if args.power is None:
            print("[拒絕] 未提供 --power。本工具沒有預設功率，"
                  "量測功率必須由操作者明確指定。", file=sys.stderr)
            return 2
        if not valid_power(args.power):
            print(f"[拒絕] --power 不合法：{args.power!r}（需為有限正數）", file=sys.stderr)
            return 2
    if args.execute and not valid_power(args.power):
        print("[拒絕] --execute 必須搭配合法的 --power。", file=sys.stderr)
        return 2
    if not (args.interval > 0 and math.isfinite(args.interval)):
        print(f"[拒絕] --interval 不合法：{args.interval!r}", file=sys.stderr)
        return 2

    out_dir = args.out_dir or _default_out_dir()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    sample_csv = os.path.join(out_dir, f"pcs_samples_{stamp}.csv")
    event_csv = os.path.join(out_dir, f"events_{stamp}.csv")

    print("=" * 70)
    print("Phase 6.5-G Field Measurement Tool")
    print("=" * 70)
    print(f"  execute        : {args.execute}"
          f"{'' if args.execute else '   ← operator 不會被呼叫，不可能控制設備'}")
    print(f"  power          : {args.power} kW"
          f"{'（僅本次量測用；不寫入任何 Production Config）' if args.power else ''}")
    print(f"  sample interval: {args.interval}s（量測用；非 production poll interval）")
    print(f"  samples        : {sample_csv}")
    print(f"  events         : {event_csv}")
    print("=" * 70)

    # ---- 連線元件（延後 import，確保未執行 main 時本模組零網路相依）----
    from api_client import ApiClient                            # noqa: PLC0415
    import charge_discharge_report as CDR                       # noqa: PLC0415
    import device_control_operator as DCO                       # noqa: PLC0415
    import last_control_store as LCS                            # noqa: PLC0415

    # 取樣專用 client：與控制路徑**分開**，避免背景執行緒共用 requests.Session
    sampler_client = ApiClient()

    def fetch_pcs():
        return sampler_client.get(EP_PCS)

    ctrl_client = ApiClient()
    ctrl_client.login_hmi(DCO.USERNAME)

    sw = RowWriter(sample_csv, SAMPLE_FIELDS).open()
    ew = RowWriter(event_csv, EVENT_FIELDS).open()

    # stdin / stdout 的唯一入口：確認提示期間暫停樣本顯示、提示前 drain stdin
    console = ConsoleIO()
    if not console._isatty():
        print("  ⚠️ stdin 不是互動終端機 —— 所有 Stage 都會 fail closed（DECLINED_EOF /"
              " DECLINED_EMPTY_NON_TTY）。請在真正的終端機視窗執行。")

    # ---- Phase A：Control Authority 元件（全部 FIELD TEST ONLY）----
    # 🔴 LastControl 寫在量測專用路徑，不污染 production 的 runtime 狀態檔。
    lc_path = os.path.join(out_dir, f"field_last_control_{stamp}.json")
    field_store = LCS.LastControlStore(path=lc_path)
    field_policy = field_authority_policy()
    field_rb = field_readback_config()
    print(f"  {FIELD_TEST_ONLY}")
    print(f"    authority ttl        : {field_policy.authority_ttl_sec}s")
    print(f"    authority tolerance  : {field_policy.authority_power_tolerance_kw} kW")
    print(f"    readback timeout     : {field_rb.timeout_sec}s")
    print(f"    readback poll        : {field_rb.poll_interval_sec}s")
    print(f"    readback stability   : {field_rb.stability_samples}")
    print(f"    field LastControl    : {lc_path}")
    print("    ⚠️ 以上皆不寫入 production config；production default 仍為 None。")
    print("=" * 70)

    sess = MeasurementSession(
        power_kw=args.power, execute=args.execute,
        gate=StageGate(console.confirm),
        read_all_fn=lambda: CDR.read_all(ctrl_client),
        mode_state_reader=lambda: DCO._read_pcs_mode_state(ctrl_client),
        operator_run=DCO.run,
        event_sink=ew.write, printer=console.write_line,
        pcs_reader=lambda: observations_from_payload(sampler_client.get(EP_PCS)),
        authority_policy=field_policy, readback_config=field_rb,
        last_control_store=field_store,
    )
    sampler = PcsSampler(fetch=fetch_pcs, interval_sec=args.interval,
                         sink=lambda s: sw.write(s.as_row()),
                         watcher=sess.watch_sample,
                         printer=None if args.quiet_samples
                         else make_sample_printer(console))
    sess.sampler = sampler

    def hold(msg):
        return lambda: console.observe(msg)

    rc = 0
    try:
        sess.stage0_baseline(hold=hold("觀察 idle baseline"))
        if args.stage0_only:
            print("\n--stage0-only：結束。")
        else:
            sess.stage1_read_all("Stage 1")
            sess.stage2_safety(ACT_CHARGE, "Stage 2")
            sess.stage_command("Stage 3", ACT_CHARGE, hold=hold("觀察 CHARGE ramp-up 至穩定"))
            sess.stage_command("Stage 4", ACT_STOP, hold=hold("觀察 STOP decay 至完全停止"))
            sess.stage1_read_all("Stage 5.1")
            sess.stage2_safety(ACT_DISCHARGE, "Stage 5.2")
            sess.stage_command("Stage 5.3", ACT_DISCHARGE, hold=hold("觀察 DISCHARGE ramp-up 至穩定"))
            sess.stage_command("Stage 6", ACT_STOP, hold=hold("觀察 STOP decay 至完全停止"))
    except KeyboardInterrupt:
        print("\n  [中斷] 使用者中止；取樣停止，資料已落檔。")
        rc = 130
    finally:
        sampler.stop()
        samples = sampler.snapshot()
        sw.close()
        ew.close()

    print("\n" + "=" * 70)
    print("Stage 摘要")
    for r in sess.results:
        print(f"  {r.stage:<10s} {r.status:<14s} {r.detail}")
    if sess.aborted:
        print(f"  🔴 ABORT：{sess.abort_reason}")
        print("     本輪標記 INCONCLUSIVE；未建立 LastControl；未自動 Recovery STOP。")
    declined = [(s, why) for s, ok, why in sess.gate.history if not ok]
    if declined:
        print("\n  未取得確認的 Stage 與原因（用於診斷，非自動放行依據）")
        for s, why in declined:
            print(f"    {s:<10s} {why}")
    print(f"\n  樣本數 {len(samples)}；state 變化次數 {count_state_changes(samples)}")
    print(f"  提示期間暫停顯示 {console.suppressed_lines} 行（CSV 未受影響）；"
          f"清除殘留鍵入 {console.drained_total} 次")
    print(f"  samples → {sample_csv}")
    print(f"  events  → {event_csv}")
    print("  ⚠️ 分析結果不會寫回任何 Production Config，請人工裁示 "
          "timeout_sec / poll_interval_sec / stability_samples。")
    print("=" * 70)
    return rc


if __name__ == "__main__":
    sys.exit(main())
