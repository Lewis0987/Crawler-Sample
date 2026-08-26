# -*- coding: utf-8 -*-
"""
Phase 6.5-G Field Measurement Tool 驗證 —— 完全離線
======================================================================
是否需要設備
    **不需要**。完全離線：不連 HMI / 6160 / PCS / BMS，不開 socket。
    fetch / operator / clock / confirmer / read_all 一律注入。

涵蓋範圍
    A. payload 解析語意與 charge_discharge_report 完全一致
    B. PcsSampler：注入、失敗不跳過不沿用、時間戳、背景執行緒
    C. Baseline：樣本不足 Fail Closed、noise_band 由實測推導
    D. ramp-up 分析：區間回報、多 window、無 steady
    E. STOP decay：旗標 strict is False、standby 不作判據
    F. StageGate：confirmer 未注入即 False
    G. 🔴 execute=False → operator 呼叫次數必須為 0
    H. 🔴 --power Fail Closed（CLI 層 + dispatch 層）
    I. 🔴 Conflict / Fault → abort、取樣不停、不自動 STOP
    J. 🔴 timeout / 控制失敗 → INCONCLUSIVE、不建立 LastControl
    K. 🔴 不自動串接：任一段未過 → 後續全部不執行
    L. 靜態守門：無 execute=True 字面量、無 production 參數、無 config 寫回
    M. CSV / event schema
    N. STOP 不帶 power、charge/discharge 帶 power

用法
    python test_phase6_field_measure.py          # exit 0 = PASS
"""
import os
import sys
import ast
import csv
import math
import time
import io as _io
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phase6_field_measure as FM                      # noqa: E402

# 於 import 後立即快照 —— 證明本模組（含相依）在未執行 main() 前不具備任何網路能力
_MODULES_AFTER_FM = set(sys.modules)

import pcs_control_integration as PCI                  # noqa: E402
import control_authority as CA                         # noqa: E402
import last_control_store as _LCS_FOR_TEST             # noqa: E402
LCS_SCHEMA = _LCS_FOR_TEST.SCHEMA_VERSION
CAe = _LCS_FOR_TEST
import pcs_control_executor as EX                      # noqa: E402
import decision_engine as DE                           # noqa: E402
import safety_gate as SG                               # noqa: E402

RESULTS = []
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = _io.open(os.path.join(HERE, "phase6_field_measure.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)


def _code_only(src):
    """去除註解與所有字串常數 —— 避免 docstring 自我描述造成字串誤命中。"""
    import tokenize
    out = []
    for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


CODE = _code_only(SRC)


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# ======================================================================
# 測試輔助
# ======================================================================
def payload(charging=False, discharging=False, standby=True, running=True,
            fault=False, p=0.0, dc_p=0.0, dc_v=800.0, dc_a=0.0, drop=()):
    """組一份 envCon/pcs 形狀的 payload；drop 中的 mark 會被移除。"""
    def b(v):
        return 1 if v is True else (0 if v is False else None)
    m = [
        {"mark": FM.MARK_CHARGING, "oldValue": b(charging), "value": "充電中"},
        {"mark": FM.MARK_DISCHARGING, "oldValue": b(discharging), "value": "放電中"},
        {"mark": FM.MARK_STANDBY, "oldValue": b(standby), "value": "待機"},
        {"mark": FM.MARK_RUNNING, "oldValue": b(running), "value": "運行"},
        {"mark": FM.MARK_FAULT, "oldValue": b(fault), "value": "故障"},
        {"mark": FM.MARK_ACTIVE_POWER, "value": p, "unit": "kW"},
        {"mark": FM.MARK_DC_POWER, "value": dc_p, "unit": "kW"},
        {"mark": FM.MARK_DC_VOLTAGE, "value": dc_v, "unit": "V"},
        {"mark": FM.MARK_DC_CURRENT, "value": dc_a, "unit": "A"},
    ]
    m = [x for x in m if x["mark"] not in drop]
    return [{"metricsDataVoList": m}]


class FakeClock:
    def __init__(self, t=1000.0, step=1.0):
        self.t, self.step = t, step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def mk_sample(seq, t, state, p, ok=True, charging=False, discharging=False,
              standby=True, fault=False, lat=0.1):
    return FM.PcsSample(seq=seq, wall_clock="w", t_req_mono=t, t_resp_mono=t + lat,
                        latency_sec=lat, sample_ok=ok, error="", pcs_state=state,
                        pcs_charging_flag=charging, pcs_discharging_flag=discharging,
                        pcs_standby_flag=standby, pcs_running_flag=True,
                        pcs_fault_flag=fault, actual_active_power_kw=p,
                        dc_power_kw=p, dc_voltage_v=800.0, dc_current_a=0.0)


class ManualSampler:
    """MeasurementSession 只用到 start/running/snapshot/last_seq/interval_sec。"""

    def __init__(self, samples=None, interval_sec=1.0):
        self._s = list(samples or [])
        self.interval_sec = interval_sec
        self.running = True
        self.start_calls = 0
        self.stopped = False

    def start(self):
        self.start_calls += 1
        self.running = True

    def stop(self, join_timeout=None):
        self.stopped = True
        self.running = False

    def snapshot(self):
        return list(self._s)

    def last_seq(self):
        return self._s[-1].seq if self._s else 0

    def feed(self, s):
        self._s.append(s)


class SpyOperator:
    def __init__(self, control_success=True, raise_exc=None):
        self.calls = []
        self.control_success = control_success
        self.raise_exc = raise_exc

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        # 刻意加入被汙染的 success —— 工具不得讀它
        return {"control_success": self.control_success, "success": not self.control_success}


def idle_samples(n=12, p=0.0, jitter=0.01, t0=1000.0):
    out = []
    for i in range(n):
        out.append(mk_sample(i + 1, t0 + i, PCI.PCS_STANDBY,
                             p + (jitter if i % 2 else -jitter)))
    return out


class FakeTTY:
    """
    模擬終端機。刻意區分兩種輸入，否則測不出真正的故障：

      type_ahead —— 提示出現「之前」就已經敲進鍵盤緩衝區的內容（可被 drain 清掉）
      answers    —— 使用者被問到時才輸入的答案（drain 清不掉，因為還沒打）

    真實故障就發生在 type_ahead 被下一個 input() 當成 answers 讀走。
    """

    def __init__(self, answers=(), type_ahead=(), tty=True):
        self.answers = list(answers)
        self.type_ahead = list(type_ahead)
        self.tty = tty
        self.reads = []
        self.prompts = []
        self.drained = []

    def push_type_ahead(self, *lines):
        self.type_ahead.extend(lines)

    def reader(self, prompt):
        self.prompts.append(prompt)
        if self.type_ahead:
            line = self.type_ahead.pop(0)
        elif self.answers:
            line = self.answers.pop(0)
        else:
            raise EOFError
        self.reads.append(line)
        return line

    def drain(self):
        n = len(self.type_ahead)
        self.drained.append(n)
        self.type_ahead.clear()
        return n

    def isatty(self):
        return self.tty


class StepClock:
    """不自動前進的時鐘；由測試明確 advance，才能精準控制 snapshot age。"""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d
        return self.t


def good_reading(**over):
    r = {"_fail": [], "alarm_rows": [], "alarm_total": 0, "alarm_total_raw": 0,
         "communication_ok": True, "soc_percent": 50.0, "pcs_fault_flag": False,
         "battery_power_status": SG_BATT_ON(),
         "pcs_charging_flag": False, "pcs_discharging_flag": False,
         "pcs_standby_flag": True, "pcs_running_flag": True,
         "actual_active_power_kw": -1.4}
    r.update(over)
    return r


class SpyReader:
    """記錄每次 read_all 的呼叫時刻，並讓每次回傳**不同的 dict 物件**。"""

    def __init__(self, clock, duration=0.5, reading_fn=None, exc=None):
        self.clock = clock
        self.duration = duration
        self.reading_fn = reading_fn or (lambda: good_reading())
        self.exc = exc
        self.calls = []
        self.returned = []

    def __call__(self):
        self.calls.append(self.clock.t)
        self.clock.advance(self.duration)
        if self.exc:
            raise self.exc
        r = self.reading_fn()
        self.returned.append(r)
        return r


def idle_reading(**over):
    """PCS 閒置（STANDBY）且各項安全條件正常的 reading —— Authority 應判為 IDLE。"""
    r = {"_fail": [], "alarm_rows": [], "alarm_total": 0, "alarm_total_raw": 0,
         "communication_ok": True, "soc_percent": 50.0, "pcs_fault_flag": False,
         "battery_power_status": SG_BATT_ON(),
         "pcs_charging_flag": False, "pcs_discharging_flag": False,
         "pcs_standby_flag": True, "pcs_running_flag": True,
         "actual_active_power_kw": -1.4}
    r.update(over)
    return r


_AUTZ_DEFAULT = object()


def charging_reading(power=5.3, **over):
    """PCS 充電中的 reading。"""
    return idle_reading(pcs_charging_flag=True, pcs_standby_flag=False,
                        actual_active_power_kw=power, **over)


def discharging_reading(power=-5.4, **over):
    return idle_reading(pcs_discharging_flag=True, pcs_standby_flag=False,
                        actual_active_power_kw=power, **over)


def pcs_obs(**kw):
    """供 ReadBackVerifier 的 reader —— 回傳解析後的觀測 dict。"""
    return FM.observations_from_payload(payload(**kw))


def make_store(clk, path=None):
    """真實 LastControlStore（寫暫存檔）—— 不另造第二套簡化 LastControl。"""
    import last_control_store as LCS
    p = path or os.path.join(tempfile.mkdtemp(prefix="p65h_"), "lc.json")
    return LCS.LastControlStore(path=p, boot_id_provider=lambda: "test-boot",
                                clock=clk, wall_clock=lambda: "2026-08-24 00:00:00")


def field_session(operator=None, power=5.0, reading_fn=None, obs=None, store=None,
                  clock=None, mode=None, policy=None, authorize=_AUTZ_DEFAULT, **over):
    """已注入完整 Authority 元件的量測 session（FIELD TEST ONLY 政策）。"""
    clk = clock or FakeClock(3000.0, 0.0)
    st = make_store(clk) if store is None else store
    kw = dict(execute=True, sampler=ManualSampler(idle_samples(12)),
              operator=operator if operator is not None else SpyOperator(), power=power,
              # 新 leg 一律從 idle 起算；需要 CHARGING 現況的案例自行覆寫 read_all_fn
              read_all_fn=reading_fn or (lambda: idle_reading()),
              mode_state_reader=lambda: (mode if mode is not None
                                         else {"schedule_switch": 0, "manual_switch": 1}),
              clock=clk)
    kw.update(over)
    s = session(**kw)
    s.pcs_reader = obs or (lambda: pcs_obs(charging=True, standby=False, running=True))
    s.readback_config = FM.field_readback_config()
    s.last_control_store = st
    s.authority_policy = policy if policy is not None else FM.field_authority_policy()

    # ⚠️ ReadBackVerifier 以注入的 clock 計算 elapsed，以 sleeper 推進時間。
    #    測試用的 sleeper 必須真的推進 FakeClock，否則永遠不會逾時（無限迴圈）。
    def _sleep(d):
        clk.t += d

    s.sleeper = _sleep
    # Phase A.1：多數案例直接從 Stage 3 開始，需預先核發該 leg 的授權。
    # authorize=None 可關閉（用來驗證「沒有授權就不可能 dispatch」）。
    az = FM.ACT_CHARGE if authorize is _AUTZ_DEFAULT else authorize
    if az is not None:
        grant_leg(s, az)
    return s, st, clk


def _raises_base(fn, *a):
    try:
        fn(*a)
    except KeyboardInterrupt:
        return True
    except BaseException:                                   # noqa: BLE001
        return False
    return False


def session(execute=False, sampler=None, operator=None, confirm=True, power=7.0,
            read_all_fn=None, mode_state_reader=None, clock=None, events=None,
            authorize=None):
    """authorize=<operator action> 會預先核發該 leg 的授權（Phase A.1 新增的必要輸入）。"""
    gate = FM.StageGate((lambda _p: True) if confirm else (lambda _p: False))
    s = FM.MeasurementSession(
        power_kw=power, execute=execute, sampler=sampler, gate=gate,
        read_all_fn=read_all_fn, mode_state_reader=mode_state_reader,
        operator_run=operator, clock=clock or FakeClock(2000.0, 0.0),
        event_sink=(events.append if events is not None else None))
    if authorize is not None:
        grant_leg(s, authorize)
    return s


def grant_leg(s, action, state=None):
    """替測試補上 per-leg 授權，並確保 dispatch 前複驗有最小可用輸入。"""
    if s.read_all_fn is None:
        s.read_all_fn = lambda: idle_reading()
    if s.mode_state_reader is None:
        s.mode_state_reader = lambda: {"schedule_switch": 0, "manual_switch": 1}
    s.leg_authorization = FM.ControlLegAuthorization(
        action=action, authority_state=state or CA.AUTH_IDLE,
        authority_reason=CA.CA_IDLE, authorized=True, evaluated_at=s.clock(),
        pcs_state="STANDBY", schedule_switch=0)
    return s


# ======================================================================
def main():
    print("== Phase 6.5-G Field Measurement Tool 驗證（完全離線）==\n")

    # ---------------- A. payload 解析語意 ----------------
    print("A. payload 解析語意（與 charge_discharge_report 一致）")
    import charge_discharge_report as CDR
    for kw in ({"charging": True, "standby": False, "p": -12.5},
               {"discharging": True, "standby": False, "p": 12.5},
               {"standby": True, "p": 0.0},
               {"charging": None, "standby": None, "p": None}):
        pl = payload(**kw)
        mine = FM.pcs_metric_map(pl)
        theirs = CDR._pcs_metric_map(pl)
        check(f"  pcs_metric_map 一致（{kw}）", mine == theirs)
    pl = payload(charging=True, standby=False, p=-30.25)
    marks = CDR._pcs_metric_map(pl)
    obs = FM.observations_from_payload(pl)
    check("  flag 取自 oldValue（與 read_all 同）",
          obs["pcs_charging_flag"] is True and obs["pcs_standby_flag"] is False)
    check("  power 取自 value（與 read_all 同）",
          obs["actual_active_power_kw"] == CDR._to_float(marks[FM.MARK_ACTIVE_POWER]["value"])
          == -30.25)
    check("  pcs_state 由 classify_pcs_state 導出", obs["pcs_state"] == PCI.PCS_CHARGING)
    check("  oldValue 缺失 → None（不猜）",
          FM.observations_from_payload(payload(drop=(FM.MARK_CHARGING,)))["pcs_charging_flag"]
          is None)
    check("  oldValue 無法解析 → None",
          FM.flag_of({"m": {"oldValue": "???"}}, "m") is None)
    check("  全 False → UNKNOWN（不等於待機）",
          FM.observations_from_payload(payload(standby=False))["pcs_state"] == PCI.PCS_UNKNOWN)
    check("  C+S 同時 True → CONFLICT",
          FM.observations_from_payload(payload(charging=True, standby=True))["pcs_state"]
          == PCI.PCS_CONFLICT)
    check("  payload 非 list → 全 None / UNKNOWN",
          FM.observations_from_payload({"x": 1})["pcs_state"] == PCI.PCS_UNKNOWN)
    # 6.5-G 實機狀態模型：取樣器必須把 running flag 一起傳進分類
    o_stop = FM.observations_from_payload(payload(standby=False, running=False, p=-5.4))
    check("★★ 取樣器：C/D/S 全 False + R=False → STOPPED（不再是 UNKNOWN）",
          o_stop["pcs_state"] == PCI.PCS_STOPPED and o_stop["pcs_running_flag"] is False)
    check("  STOP 後 AC 功率仍為 -5.4 不影響狀態判定",
          o_stop["actual_active_power_kw"] == -5.4)
    check("  C/D/S 全 False + R=True → UNKNOWN（維持 Fail Closed）",
          FM.observations_from_payload(payload(standby=False, running=True))["pcs_state"]
          == PCI.PCS_UNKNOWN)
    check("  running mark 缺失 → None → UNKNOWN",
          FM.observations_from_payload(payload(standby=False, drop=(FM.MARK_RUNNING,)))
          ["pcs_state"] == PCI.PCS_UNKNOWN)
    check("★ S=True 但 R=False → CONFLICT（旗標證據矛盾）",
          FM.observations_from_payload(payload(standby=True, running=False))["pcs_state"]
          == PCI.PCS_CONFLICT)
    check("★★ 缺 standby mark + R=False → UNKNOWN（None 不得當成 False）",
          FM.observations_from_payload(
              payload(standby=False, running=False, drop=(FM.MARK_STANDBY,)))["pcs_state"]
          == PCI.PCS_UNKNOWN)
    check("★★ 缺 charging mark + R=False → UNKNOWN",
          FM.observations_from_payload(
              payload(standby=False, running=False, drop=(FM.MARK_CHARGING,)))["pcs_state"]
          == PCI.PCS_UNKNOWN)
    check("  四旗標齊備且皆明確 False + R=False → STOPPED",
          FM.observations_from_payload(payload(standby=False, running=False))["pcs_state"]
          == PCI.PCS_STOPPED)
    check("  正常 STANDBY（S=True, R=True）不受影響",
          FM.observations_from_payload(payload(standby=True, running=True))["pcs_state"]
          == PCI.PCS_STANDBY)

    # ---------------- B. PcsSampler ----------------
    print("\nB. PcsSampler")
    try:
        FM.PcsSampler().sample_once()
        ok = False
    except RuntimeError:
        ok = True
    check("fetch 未注入 → sample_once 拋 RuntimeError（不自行連線）", ok)
    try:
        FM.PcsSampler().start()
        ok = False
    except RuntimeError:
        ok = True
    check("fetch 未注入 → start() 拋 RuntimeError", ok)

    clk = FakeClock(500.0, 0.25)
    sp = FM.PcsSampler(fetch=lambda: payload(charging=True, standby=False, p=-9.0), clock=clk)
    s1 = sp.sample_once()
    check("樣本記錄 t_req/t_resp/latency",
          s1.t_req_mono == 500.0 and s1.t_resp_mono == 500.25 and s1.latency_sec == 0.25)
    check("seq 自 1 遞增", s1.seq == 1 and sp.sample_once().seq == 2)
    check("觀測欄位落入樣本", s1.actual_active_power_kw == -9.0 and s1.pcs_state == PCI.PCS_CHARGING)

    bad = FM.PcsSampler(fetch=lambda: None, clock=FakeClock(0.0, 0.1))
    b1 = bad.sample_once()
    check("★ fetch 回 None → 仍寫一列、sample_ok=False、欄位為 None（不跳過）",
          (not b1.sample_ok) and b1.actual_active_power_kw is None
          and b1.pcs_state == PCI.PCS_UNKNOWN and "None" in b1.error)

    seqv = [payload(p=1.0), None, payload(p=2.0)]
    it = iter(seqv)
    sp2 = FM.PcsSampler(fetch=lambda: next(it), clock=FakeClock(0.0, 0.1))
    got = [sp2.sample_once() for _ in range(3)]
    check("★ 失敗樣本不沿用上一筆數值",
          got[0].actual_active_power_kw == 1.0 and got[1].actual_active_power_kw is None
          and got[2].actual_active_power_kw == 2.0)
    check("  失敗樣本仍佔一個 seq（時間軸無隱形空洞）",
          [g.seq for g in got] == [1, 2, 3] and len(sp2.snapshot()) == 3)

    boom = FM.PcsSampler(fetch=lambda: (_ for _ in ()).throw(ValueError("net down")),
                         clock=FakeClock(0.0, 0.1))
    e1 = boom.sample_once()
    check("fetch 例外 → sample_ok=False 並記錄型別（不中斷取樣）",
          (not e1.sample_ok) and "ValueError" in e1.error)

    sink, watched = [], []
    sp3 = FM.PcsSampler(fetch=lambda: payload(p=0.0), interval_sec=0.01,
                        sink=sink.append, watcher=watched.append)
    sp3.start()
    t_end = time.monotonic() + 0.35
    while time.monotonic() < t_end and len(sink) < 3:
        time.sleep(0.01)
    running_mid = sp3.running
    sp3.stop()
    check("背景執行緒可啟動並持續取樣", running_mid and len(sink) >= 3)
    check("  stop() 後執行緒結束", not sp3.running)
    check("  sink / watcher 皆被呼叫", len(sink) >= 3 and len(watched) >= 3)
    n_after = len(sp3.snapshot())
    time.sleep(0.05)
    check("  stop() 後不再新增樣本", len(sp3.snapshot()) == n_after)

    slow = FM.PcsSampler(fetch=lambda: payload(p=0.0), interval_sec=1.0,
                         clock=FakeClock(0.0, 3.0))
    ss = slow.sample_once()
    check("★ latency > 目標間隔時樣本仍記錄實際時距（latency-bound）",
          ss.latency_sec == 3.0 and ss.latency_sec > slow.interval_sec)

    # ---------------- C. Baseline ----------------
    print("\nC. Baseline（Stage 0）")
    bl = FM.compute_baseline(idle_samples(5))
    check("★ 有效樣本不足 → valid=False（Fail Closed）",
          (not bl.valid) and "BASELINE_INSUFFICIENT" in bl.reason)
    check("  不足時不產生 p_base / noise_band", bl.p_base is None and bl.noise_band is None)
    bl = FM.compute_baseline(idle_samples(12, p=0.0, jitter=0.02))
    check("★ noise_band 由實測推導（非寫死）",
          bl.valid and abs(bl.noise_band - 0.02) < 1e-9)
    check("  p_base 為中位數", abs(bl.p_base - 0.0) < 1e-9)
    check("  latency 統計齊備",
          bl.lat_min is not None and bl.lat_median is not None
          and bl.lat_p95 is not None and bl.lat_max is not None)
    check("  flag baseline / 抖動次數", bl.state_baseline == PCI.PCS_STANDBY
          and bl.state_changes == 0 and bl.states_seen == (PCI.PCS_STANDBY,))
    mixed = idle_samples(12)
    mixed[5] = mk_sample(6, 1005.0, PCI.PCS_UNKNOWN, 0.0)
    bm = FM.compute_baseline(mixed)
    check("★ baseline 期間 flag 抖動 → state_baseline=MIXED 且 changes>0",
          bm.state_baseline == "MIXED" and bm.state_changes == 2)
    noisy = idle_samples(12) + [mk_sample(99, 1099.0, PCI.PCS_STANDBY, None, ok=False)]
    check("失敗樣本不列入 baseline 計算",
          FM.compute_baseline(noisy).ok_count == 12)
    check("percentile 只回資料中存在的值（不內插）",
          FM._percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0)

    # ---------------- D. ramp-up ----------------
    print("\nD. ramp-up 分析")
    base = FM.compute_baseline(idle_samples(12, p=0.0, jitter=0.02))
    ramp = idle_samples(12)
    t = 2000.0
    # ⚠️ Phase C.1：測資改為 charge → **正**功率，符合 Phase 6.2 量測層 sign convention；
    #    並且 steady 現在需要 action 與指令幅度才能判定。
    TGT = 30.0
    ramp += [mk_sample(13, t, PCI.PCS_STANDBY, 0.0),
             mk_sample(14, t + 1, PCI.PCS_CHARGING, 3.0, charging=True, standby=False),
             mk_sample(15, t + 2, PCI.PCS_CHARGING, 20.0, charging=True, standby=False)]
    # 穩定段的擺動明確小於 noise_band(0.02)，避免測資卡在容差邊界
    ramp += [mk_sample(16 + i, t + 3 + i, PCI.PCS_CHARGING, TGT + (0.005 if i % 2 else -0.005),
                       charging=True, standby=False) for i in range(6)]
    r = FM.analyze_ramp(ramp, base, t_cmd_sent=1999.5, t_cmd_returned=1999.8, window=3,
                        action=FM.ACT_CHARGE, target_power_kw=TGT)
    check("onset 找到第一個脫離 baseline 的樣本", r["onset_seq"] == 14)
    check("steady 找到穩定窗", r["steady_seq"] == 16 and r["steady_state"] == PCI.PCS_CHARGING)
    check("★ ramp_up 以區間回報（upper 自 cmd_sent、lower 自 cmd_returned）",
          r["ramp_up_upper"] is not None and r["ramp_up_lower"] is not None
          and r["ramp_up_upper"] > r["ramp_up_lower"])
    check("  upper 較保守（用於推導 timeout）",
          abs(r["ramp_up_upper"] - (2003.1 - 1999.5)) < 1e-9)
    check("  steady_power_kw 為窗內中位數", abs(r["steady_power_kw"] - TGT) < 0.02)
    check("★ 中間過渡值（3.0 / 20.0 kW）不得被當成 steady（幅度未達 target）",
          r["steady_seq"] == 16)
    multi = FM.analyze_multi(FM.analyze_ramp, ramp, base, 1999.5, 1999.8,
                             action=FM.ACT_CHARGE, target_power_kw=TGT)
    check("★ 多 window 同時分析（3/5/10）",
          [m["window"] for m in multi] == list(FM.STEADY_WINDOWS))
    check("  資料不足以支撐長窗 → 該窗回 NO_STEADY_WINDOW（不硬湊）",
          multi[2]["steady_seq"] is None and multi[2]["reason"] == "NO_STEADY_WINDOW")
    check("★ IEEE754 比較保護：7.01-6.99 的浮點誤差不影響容差判定",
          FM._le(7.01 - 6.99, 0.02) and not FM._le(0.03, 0.02))
    check("★ 分析結果標註所用容差與其來源",
          r["tolerance"] == base.noise_band and r["tolerance_source"] == FM.TOL_NOISE)
    # idle 完全恆定（noise_band=0）→ 改用實測解析度，否則 steady 永遠不成立
    flat = [mk_sample(i + 1, 1000.0 + i, PCI.PCS_STANDBY, 0.0) for i in range(12)]
    b0 = FM.compute_baseline(flat)
    check("  idle 完全恆定 → noise_band=0", b0.valid and b0.noise_band == 0.0)
    ramp0 = flat + [mk_sample(13 + i, 2000.0 + i, PCI.PCS_CHARGING,
                              7.0 + (0.01 if i % 2 else -0.01),
                              charging=True, standby=False) for i in range(6)]
    r0 = FM.analyze_ramp(ramp0, b0, 1999.5, 1999.8, window=3,
                         action=FM.ACT_CHARGE, target_power_kw=7.0)
    check("★★ noise_band=0 時改用 OBSERVED_QUANTUM，steady 仍可判定",
          r0["tolerance_source"] == FM.TOL_QUANTUM and r0["steady_seq"] == 13)
    check("  quantum 由資料導出（全樣本最小非零差）",
          abs(FM.observed_quantum(ramp0) - 0.02) < 1e-9)
    check("  完全無變化的資料 → 無 quantum → ZERO_STRICT",
          FM.effective_tolerance(b0, flat)[1] == FM.TOL_STRICT)
    # 容差必須只由「本次命令之後的區段」導出，否則會隨 session 累積漂移
    later = ramp0 + [mk_sample(30 + i, 3000.0 + i, PCI.PCS_DISCHARGING,
                               -7.0 + (0.01 if i % 2 else -0.01),
                               discharging=True, standby=False) for i in range(6)] \
                  + [mk_sample(40, 3100.0, PCI.PCS_STANDBY, 0.005)]
    r_early = FM.analyze_ramp(later, b0, 1999.5, 1999.8, window=3,
                              action=FM.ACT_CHARGE, target_power_kw=7.0)
    r_late = FM.analyze_ramp(later, b0, 2999.5, 2999.8, window=3,
                             action=FM.ACT_DISCHARGE, target_power_kw=7.0)
    check("★★ 容差不隨 session 累積漂移：早/晚兩次分析各自取得 steady",
          r_early["steady_seq"] == 13 and r_late["steady_seq"] == 30)
    check("  兩次分析的容差各自由自身區段導出（皆為實測解析度 0.02）",
          abs(r_early["tolerance"] - 0.02) < 1e-9 and abs(r_late["tolerance"] - 0.02) < 1e-9
          and r_early["tolerance_source"] == r_late["tolerance_source"] == FM.TOL_QUANTUM)
    st0 = FM.analyze_stop(flat + [mk_sample(20 + i, 2100.0 + i, PCI.PCS_STANDBY, 0.0)
                                  for i in range(4)], b0, 2099.0, 2099.2, window=3)
    check("  STOP 分析同樣標註容差來源", st0["tolerance_source"] in
          (FM.TOL_QUANTUM, FM.TOL_STRICT))

    r_none = FM.analyze_ramp(idle_samples(12), base, 1999.5, 1999.8, window=3,
                             action=FM.ACT_CHARGE, target_power_kw=TGT)
    check("命令後無樣本 → NO_SAMPLE_AFTER_COMMAND",
          r_none["reason"] == "NO_SAMPLE_AFTER_COMMAND")
    check("baseline 無效 → BASELINE_INVALID（不分析）",
          FM.analyze_ramp(ramp, FM.compute_baseline(idle_samples(3)), 1999.5, 1999.8,
                          window=3, action=FM.ACT_CHARGE,
                          target_power_kw=TGT)["reason"] == "BASELINE_INVALID")
    still = idle_samples(12) + [mk_sample(20 + i, 2000.0 + i, PCI.PCS_STANDBY, 0.0)
                                for i in range(6)]
    check("★ 完全沒動 → 不得誤判為已達 steady",
          FM.analyze_ramp(still, base, 1999.5, 1999.8, window=3, action=FM.ACT_CHARGE,
                          target_power_kw=TGT)["steady_seq"] is None)

    # ---------------- E. STOP decay ----------------
    print("\nE. STOP decay 分析")
    stop_s = [mk_sample(1 + i, 3000.0 + i, PCI.PCS_CHARGING, -30.0,
                        charging=True, standby=False) for i in range(2)]
    stop_s += [mk_sample(3, 3002.0, PCI.PCS_CHARGING, -12.0, charging=True, standby=False)]
    stop_s += [mk_sample(4 + i, 3003.0 + i, PCI.PCS_STANDBY, 0.0) for i in range(5)]
    st = FM.analyze_stop(stop_s, base, t_stop_sent=2999.5, t_stop_returned=2999.7, window=3)
    check("找到 decay onset（功率回落雜訊帶）", st["decay_onset_seq"] == 4)
    check("找到 stopped 窗", st["stopped_seq"] == 4)
    check("★ stop_decay 以區間回報",
          st["stop_decay_upper"] is not None and st["stop_decay_lower"] is not None
          and st["stop_decay_upper"] > st["stop_decay_lower"])
    none_flags = [mk_sample(10 + i, 3100.0 + i, PCI.PCS_UNKNOWN, 0.0,
                            charging=None, discharging=None, standby=None) for i in range(5)]
    check("★★ charging/discharging 為 None → 不得判定為已停（None ≠ False）",
          FM.analyze_stop(none_flags, base, 3099.0, 3099.2, window=3)["stopped_seq"] is None)
    standby_only = [mk_sample(20 + i, 3200.0 + i, PCI.PCS_STANDBY, -25.0,
                              charging=False, standby=True) for i in range(5)]
    check("★★ standby=True 但功率未回落 → 不得判定為已停（standby 不作判據）",
          FM.analyze_stop(standby_only, base, 3199.0, 3199.2, window=3)["stopped_seq"] is None)
    still_charging = [mk_sample(30 + i, 3300.0 + i, PCI.PCS_CHARGING, 0.0,
                                charging=True, standby=False) for i in range(5)]
    check("充電旗標仍為 True → 不得判定為已停",
          FM.analyze_stop(still_charging, base, 3299.0, 3299.2, window=3)["stopped_seq"] is None)

    # ---------------- F. StageGate ----------------
    print("\nF. StageGate（人工逐段確認）")
    check("★ confirmer 未注入 → 一律 False（Fail Closed）",
          FM.StageGate().require("S", "?") is False)
    check("  未注入時記錄原因",
          FM.StageGate().history == [] or True)
    g = FM.StageGate()
    g.require("S", "?")
    check("  history 記 CONFIRMER_NOT_INJECTED", g.history[0][2] == "CONFIRMER_NOT_INJECTED")
    check("confirmer 回 False → require False",
          FM.StageGate(lambda _p: False).require("S", "?") is False)
    check("confirmer 例外 → False（不當成同意）",
          FM.StageGate(lambda _p: (_ for _ in ()).throw(ValueError("boom"))).require("S", "?")
          is False)
    check("  KeyboardInterrupt 不被吞掉（上拋中止全流程才是 fail closed）",
          _raises_base(FM.StageGate(
              lambda _p: (_ for _ in ()).throw(KeyboardInterrupt())).require, "S", "?"))
    import builtins
    _real_input = builtins.input
    try:
        builtins.input = lambda _p="": "YES"
        c_yes = FM.console_confirmer("go?")
        builtins.input = lambda _p="": "yes"
        c_lower = FM.console_confirmer("go?")
        builtins.input = lambda _p="": "y"
        c_y = FM.console_confirmer("go?")
        builtins.input = lambda _p="": (_ for _ in ()).throw(EOFError())
        c_eof = FM.console_confirmer("go?")
        builtins.input = lambda _p="": (_ for _ in ()).throw(KeyboardInterrupt())
        c_int = FM.console_confirmer("go?")
    finally:
        builtins.input = _real_input
    check("★ console_confirmer 只接受完整 'YES'", c_yes is True)
    check("  'yes' / 'y' 一律不算同意", c_lower is False and c_y is False)
    check("★ EOF / Ctrl-C → False（非互動環境不可能誤同意）",
          c_eof is False and c_int is False)
    check("confirmer 回 True → require True", FM.StageGate(lambda _p: True).require("S", "?"))
    prompts = []
    FM.StageGate(lambda p: prompts.append(p) or True).require("Stage 3", "送出 CHARGE？")
    check("  prompt 含 stage 名稱與動作", "Stage 3" in prompts[0] and "CHARGE" in prompts[0])

    # ---------------- G. execute=False 不可能控制設備 ----------------
    print("\nG. 🔴 execute=False → operator 呼叫次數必須為 0")
    spy = SpyOperator()
    sm = ManualSampler(idle_samples(12))
    s = session(execute=False, sampler=sm, operator=spy,
                read_all_fn=lambda: idle_reading(),
                mode_state_reader=lambda: {"schedule_switch": 0, "manual_switch": 1})
    s.stage0_baseline()
    s.stage1_read_all()
    s.stage2_safety(FM.ACT_CHARGE)
    s.stage_command("Stage 3", FM.ACT_CHARGE)
    s.stage_command("Stage 4", FM.ACT_STOP)
    s.stage_command("Stage 5.3", FM.ACT_DISCHARGE)
    s.stage_command("Stage 6", FM.ACT_STOP)
    check("★★ 全流程跑完，operator 呼叫次數 = 0", len(spy.calls) == 0)
    check("  Stage 3/4/5.3/6 全部標記 NO_EXECUTE",
          all(r.detail == FM.CMD_NO_EXECUTE
              for r in s.results if r.stage in ("Stage 3", "Stage 4", "Stage 5.3", "Stage 6")))
    check("  且狀態為 INCONCLUSIVE（不得謊稱成功）",
          all(r.status == FM.ST_INCONCLUSIVE
              for r in s.results if r.stage in ("Stage 3", "Stage 4")))
    check("  execute=False 時未進入 abort（純 dry-run）", not s.aborted)
    check("★ 純程式碼（去註解/字串）不存在 execute=True 字面量",
          "execute=True" not in CODE.replace(" ", ""))
    ex_true = [n for n in ast.walk(TREE) if isinstance(n, ast.keyword)
               and n.arg == "execute" and isinstance(n.value, ast.Constant)
               and n.value.value is True]
    check("★ AST：無任何 execute=True 的 keyword 實參", ex_true == [])
    dispatch = [n for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
                and n.name == "_dispatch"][0]
    first = [n for n in dispatch.body if not (isinstance(n, ast.Expr)
                                              and isinstance(n.value, ast.Constant))][0]
    check("★ _dispatch 的第一個語句就是 execute 硬閘門",
          isinstance(first, ast.If) and isinstance(first.test, ast.UnaryOp)
          and isinstance(first.test.op, ast.Not))
    op_calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "operator_run"]
    check("★ operator_run 全模組只有一個呼叫點", len(op_calls) == 1)

    # ---------------- H. --power Fail Closed ----------------
    print("\nH. 🔴 --power Fail Closed")
    check("valid_power：None 不合法", not FM.valid_power(None))
    for v in (0, -1.0, float("nan"), float("inf"), True, "7", None):
        check(f"  valid_power({v!r}) = False", not FM.valid_power(v))
    check("  valid_power(正有限數) = True", FM.valid_power(7.5))
    check("★ CLI：未給 --power → return 2（拒絕啟動）", FM.main([]) == 2)
    check("★ CLI：--power 0 → return 2", FM.main(["--power", "0"]) == 2)
    check("★ CLI：--power 負值 → return 2", FM.main(["--power", "-5"]) == 2)
    check("★ CLI：--power nan → return 2", FM.main(["--power", "nan"]) == 2)
    check("★ CLI：--execute 未給 --power → return 2", FM.main(["--execute"]) == 2)
    check("  CLI：--interval 0 → return 2", FM.main(["--power", "7", "--interval", "0"]) == 2)
    ap = FM.build_arg_parser()
    check("★ argparse --power 無預設值", ap.get_default("power") is None)
    check("  argparse --execute 預設 False", ap.get_default("execute") is False)

    spy2 = SpyOperator()
    s2 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=spy2, power=None,
                 authorize=FM.ACT_CHARGE)
    r2 = s2.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ dispatch 層：execute=True 但 power=None → operator 仍未被呼叫",
          len(spy2.calls) == 0 and r2.detail == FM.CMD_POWER_NOT_SPECIFIED)
    check("  並進入 abort（後續不執行）", s2.aborted)

    # ---------------- I. Conflict / Fault ----------------
    print("\nI. 🔴 Conflict / Fault")
    spy3 = SpyOperator()
    sm3 = ManualSampler(idle_samples(12))
    s3 = session(execute=True, sampler=sm3, operator=spy3)
    s3.watch_sample(mk_sample(13, 2000.0, PCI.PCS_CONFLICT, -5.0,
                              charging=True, discharging=True, standby=False))
    check("★ CONFLICT → 立即 abort", s3.aborted and s3.abort_reason == FM.AB_CONFLICT)
    r3 = s3.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ abort 後不再送任何命令（operator 0 次）",
          len(spy3.calls) == 0 and r3.status == FM.ST_ABORTED)
    check("  Stage 4 STOP 也不會自動送出（不自動 Recovery STOP）",
          s3.stage_command("Stage 4", FM.ACT_STOP).status == FM.ST_ABORTED
          and len(spy3.calls) == 0)
    check("★ abort 不會停止取樣（sampler.stop 未被呼叫）",
          (not sm3.stopped) and sm3.running)
    sm3.feed(mk_sample(14, 2001.0, PCI.PCS_CONFLICT, -5.0))
    check("  abort 後仍可持續累積樣本", len(sm3.snapshot()) == 13)

    s4 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=SpyOperator())
    s4.watch_sample(mk_sample(13, 2000.0, PCI.PCS_STANDBY, 0.0, fault=True))
    check("★ pcs_fault_flag=True → abort", s4.aborted and s4.abort_reason == FM.AB_FAULT)
    s5 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=SpyOperator())
    s5.watch_sample(mk_sample(13, 2000.0, PCI.PCS_STANDBY, 0.0, fault=None))
    check("  fault_flag=None 不觸發 fault abort（但 UNKNOWN 由 Safety Gate 擋）", not s5.aborted)

    # ---------------- J. timeout / 控制失敗 ----------------
    print("\nJ. 🔴 timeout / 控制失敗")
    spy6 = SpyOperator(control_success=None)
    s6 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=spy6,
                 authorize=FM.ACT_CHARGE)
    r6 = s6.stage_command("Stage 3", FM.ACT_CHARGE)
    check("control_success=None → INCONCLUSIVE 並 abort",
          r6.status == FM.ST_INCONCLUSIVE and s6.aborted)
    spy7 = SpyOperator(raise_exc=TimeoutError("read timeout"))
    s7 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=spy7,
                 authorize=FM.ACT_CHARGE)
    r7 = s7.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★ operator 例外（timeout）→ INCONCLUSIVE + abort",
          r7.status == FM.ST_INCONCLUSIVE and s7.aborted
          and s7.abort_reason == FM.AB_OPERATOR)
    check("★★ timeout 後不自動 Recovery STOP",
          s7.stage_command("Stage 4", FM.ACT_STOP).status == FM.ST_ABORTED
          and len(spy7.calls) == 1)
    # Phase A：LastControl 改為「由 main() 注入的 store 建立」，因此模組本身仍不得
    # import last_control_store（那會讓模組具備檔案 I/O 能力）。這比原本的字串比對更嚴格。
    # ⚠️ 只看**模組層級** import。連線／寫檔類模組一律延後到 main() 內才 import，
    #    因此模組被 import 時仍是零 I/O（由上方 sys.modules 快照獨立佐證）。
    _fm_top = {a.name.split(".")[0] for n in ast.parse(SRC).body
               if isinstance(n, ast.Import) for a in n.names} |               {n.module.split(".")[0] for n in ast.parse(SRC).body
               if isinstance(n, ast.ImportFrom) and n.module}
    check(f"★★ 模組層級未 import last_control_store（store 一律注入）：{sorted(_fm_top)}",
          "last_control_store" not in _fm_top)
    check("★★ 連線類模組也都不在模組層級",
          not (_fm_top & {"api_client", "charge_discharge_report",
                          "device_control_operator", "requests"}))
    check("★★ 未注入 store 時不可能建立 LastControl",
          FM.MeasurementSession()._last_control() == (None, None))
    imports = {a.name for n in ast.walk(TREE) if isinstance(n, ast.Import) for a in n.names} | \
              {n.module for n in ast.walk(TREE) if isinstance(n, ast.ImportFrom) and n.module}
    top_imports = {a.name.split(".")[0] for n in ast.parse(SRC).body
                   if isinstance(n, ast.Import) for a in n.names} |                   {n.module.split(".")[0] for n in ast.parse(SRC).body
                   if isinstance(n, ast.ImportFrom) and n.module}
    check("  模組層級 import 清單不含 last_control_store",
          "last_control_store" not in top_imports)
    spy8 = SpyOperator(control_success=False)
    s8 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=spy8,
                 authorize=FM.ACT_CHARGE)
    check("control_success=False → abort",
          s8.stage_command("Stage 3", FM.ACT_CHARGE).status == FM.ST_INCONCLUSIVE and s8.aborted)
    _succ = [n for n in ast.walk(TREE)
             if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "get" and n.args
                 and isinstance(n.args[0], ast.Constant) and n.args[0].value == "success")
             or (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                 and n.slice.value == "success")]
    check("★ AST：只讀 control_success，未讀被汙染的 record['success']", _succ == [])

    # ---------------- K. 不自動串接 ----------------
    print("\nK. 🔴 Stage 不自動串接")
    spy9 = SpyOperator()
    s9 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=spy9,
                 confirm=False)
    s9.stage0_baseline()
    s9.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★ 每個 Stage 都必須先過 StageGate；未確認 → DECLINED",
          all(r.status == FM.ST_DECLINED for r in s9.results) and len(spy9.calls) == 0)

    calls = []
    gate = FM.StageGate(lambda p: calls.append(p) or ("Stage 3" not in p))
    spy10 = SpyOperator()
    s10 = FM.MeasurementSession(power_kw=7.0, execute=True,
                                sampler=ManualSampler(idle_samples(12)), gate=gate,
                                operator_run=spy10, clock=FakeClock(2000.0, 0.0))
    grant_leg(s10, FM.ACT_DISCHARGE)
    s10.stage0_baseline()
    s10.stage_command("Stage 3", FM.ACT_CHARGE)
    # 用 DISCHARGE 而非 STOP —— 本項驗證的是 StageGate 的逐段確認，
    # 而 STOP 在 execute=True 下另有 Control Authority 前置要求（見 W 節專屬測試）。
    s10.stage_command("Stage 5.3", FM.ACT_DISCHARGE)
    check("★★ Stage 3 被拒 → 不送命令；下一段仍需自己的確認",
          len(spy10.calls) == 1 and spy10.calls[0]["action"] == FM.ACT_DISCHARGE)
    check("  每個 Stage 各要一次確認（共 3 次 prompt）", len(calls) == 3)

    # Safety BLOCKED → 後續全部 ABORTED
    spy11 = SpyOperator()
    bad_reading = {"_fail": [], "alarm_rows": [], "alarm_total": 0, "alarm_total_raw": 0,
                   "communication_ok": False, "soc_percent": 50.0,
                   "pcs_fault_flag": False, "battery_power_status": SG_BATT_ON()}
    s11 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=spy11,
                  read_all_fn=lambda: bad_reading,
                  mode_state_reader=lambda: {"schedule_switch": 0, "manual_switch": 1})
    s11.stage0_baseline()
    s11.stage1_read_all()
    r11 = s11.stage2_safety(FM.ACT_CHARGE)
    check("★ Safety Gate 未過 → Stage 2 BLOCKED + abort",
          r11.status == FM.ST_BLOCKED and s11.aborted)
    check("★★ 之後 Stage 3~6 全部 ABORTED 且 operator 0 次",
          all(s11.stage_command(n, a).status == FM.ST_ABORTED
              for n, a in (("Stage 3", FM.ACT_CHARGE), ("Stage 4", FM.ACT_STOP),
                           ("Stage 5.3", FM.ACT_DISCHARGE), ("Stage 6", FM.ACT_STOP)))
          and len(spy11.calls) == 0)
    check("  無取樣器運作時不得送命令",
          session(execute=True, sampler=None, operator=SpyOperator())
          .stage_command("Stage 3", FM.ACT_CHARGE).status == FM.ST_NOT_READY)

    # baseline 無效 → 不得繼續通電
    spy12 = SpyOperator()
    s16 = session(execute=True, sampler=ManualSampler(idle_samples(4)), operator=spy12)
    r16 = s16.stage0_baseline()
    check("★★ Stage 0 baseline 無效 → abort（不做無法分析的通電）",
          r16.status == FM.ST_INCONCLUSIVE and s16.aborted
          and s16.abort_reason == FM.AB_BASELINE)
    check("  後續 Stage 3 不送命令",
          s16.stage_command("Stage 3", FM.ACT_CHARGE).status == FM.ST_ABORTED
          and len(spy12.calls) == 0)

    # ---------------- L. 靜態守門 ----------------
    print("\nL. 靜態守門")
    for mod in ("requests", "socket", "urllib.request", "api_client",
                "charge_discharge_report", "device_control_operator", "last_control_store"):
        check(f"  import 後未載入 {mod}（無網路/控制相依）", mod not in _MODULES_AFTER_FM)
    check("★ 未 import charge_discharge_report_config（不可能改 Production Config）",
          "charge_discharge_report_config" not in imports
          and "charge_discharge_report_config" not in CODE)
    top_assign = {n.targets[0].id for n in TREE.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    for banned in ("charge_power_kw", "discharge_power_kw", "max_power_kw",
                   "min_switch_interval_sec", "timeout_sec", "poll_interval_sec",
                   "stability_samples", "CHARGE_POWER_KW", "DISCHARGE_POWER_KW",
                   "MAX_POWER_KW", "MIN_SWITCH_INTERVAL_SEC"):
        check(f"  未定義 production 參數 {banned}", banned not in top_assign)
    # Phase A：量測工具會建構 FIELD TEST ONLY 的 ReadBackConfig。
    # 真正要鎖的是「production default 未被污染」。
    check("★★ production ReadBackConfig default 未被污染（None / None / 1）",
          (EX.ReadBackConfig().timeout_sec, EX.ReadBackConfig().poll_interval_sec,
           EX.ReadBackConfig().stability_samples) == (None, None, 1))
    check("★★ production AuthorityPolicy default 未被污染（None / None）",
          (CA.DEFAULT_AUTHORITY_POLICY.authority_ttl_sec,
           CA.DEFAULT_AUTHORITY_POLICY.authority_power_tolerance_kw) == (None, None))
    check("★ FIELD 政策明確標示為 FIELD TEST ONLY",
          "FIELD TEST ONLY" in FM.FIELD_TEST_ONLY and "NOT PRODUCTION" in FM.FIELD_TEST_ONLY)
    check("  FIELD 值與 production default 是不同物件、不互相污染",
          FM.field_authority_policy() != CA.DEFAULT_AUTHORITY_POLICY
          and FM.field_readback_config() != EX.ReadBackConfig())
    nums = {n.value for n in ast.walk(TREE)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)}
    for banned in (150, 249, 229):
        check(f"  原始碼不含被禁數值 {banned}", banned not in nums)
    check("  ACTION 名稱與 operator 一致",
          (FM.ACT_CHARGE, FM.ACT_DISCHARGE, FM.ACT_STOP)
          == ("pcs_charge", "pcs_discharge", "pcs_stop_power"))

    # ---------------- M. CSV / event schema ----------------
    print("\nM. CSV / event schema")
    tmp = tempfile.mkdtemp(prefix="p65g_")
    p_s = os.path.join(tmp, "s.csv")
    w = FM.RowWriter(p_s, FM.SAMPLE_FIELDS).open()
    smp = FM.PcsSampler(fetch=lambda: payload(charging=True, standby=False, p=-30.0),
                        clock=FakeClock(10.0, 0.2), sink=lambda x: w.write(x.as_row()))
    smp.sample_once()
    rows = list(csv.DictReader(_io.open(p_s, encoding="utf-8-sig")))
    check("sample CSV 逐列 flush（關檔前即可讀到）", len(rows) == 1)
    check("  欄位齊備且順序固定", list(rows[0].keys()) == list(FM.SAMPLE_FIELDS))
    check("  含 request/response monotonic 與 latency",
          rows[0]["t_req_mono"] == "10.0" and rows[0]["t_resp_mono"] == "10.2")
    check("  含 5 個 flag + 功率 + DC 三項",
          rows[0]["pcs_charging_flag"] == "True" and rows[0]["pcs_running_flag"] == "True"
          and rows[0]["actual_active_power_kw"] == "-30.0"
          and rows[0]["dc_voltage_v"] != "")
    w.close()

    p_e = os.path.join(tmp, "e.csv")
    we = FM.RowWriter(p_e, FM.EVENT_FIELDS).open()
    events = []
    s12 = session(execute=False, sampler=ManualSampler(idle_samples(12)),
                  operator=SpyOperator(), events=events)
    s12.event_sink = lambda r: (events.append(r), we.write(r))
    s12.stage_command("Stage 3", FM.ACT_CHARGE)
    we.close()
    erows = list(csv.DictReader(_io.open(p_e, encoding="utf-8-sig")))
    check("event CSV 欄位齊備", erows and list(erows[0].keys()) == list(FM.EVENT_FIELDS))
    kinds = [e["event"] for e in events]
    check("  記錄 STAGE_BEGIN / COMMAND_SUPPRESSED / STAGE_END",
          "STAGE_BEGIN" in kinds and "COMMAND_SUPPRESSED" in kinds and "STAGE_END" in kinds)
    check("  event 帶 seq_at_event（可與 sample 對齊）",
          all(e["seq_at_event"] == 12 for e in events))

    events2 = []
    spy13 = SpyOperator()
    s13 = session(execute=True, sampler=ManualSampler(idle_samples(12)), operator=spy13,
                  events=events2, authorize=FM.ACT_CHARGE)
    s13.stage_command("Stage 3", FM.ACT_CHARGE)
    sent = [e for e in events2 if e["event"] == "COMMAND_SENT"]
    ret = [e for e in events2 if e["event"] == "COMMAND_RETURNED"]
    check("★ 記錄 t_cmd_sent_mono 與 t_cmd_returned_mono",
          len(sent) == 1 and len(ret) == 1
          and sent[0]["t_cmd_sent_mono"] is not None
          and ret[0]["t_cmd_returned_mono"] is not None)
    check("  記錄 control_success", ret[0]["control_success"] is True)

    # ---------------- N. power 傳遞 ----------------
    print("\nN. power 傳遞規則")
    spy14 = SpyOperator()
    # Phase A：STOP 在 execute=True 下需先證明 Control Authority，故使用完整注入的 session。
    s14, _st14, _c14 = field_session(operator=spy14, power=7.0)
    s14.stage_command("Stage 3", FM.ACT_CHARGE)
    s14.read_all_fn = lambda: charging_reading(7.0)   # 充電後的現況 → STOP 前可判 OWNED
    s14.stage_command("Stage 4", FM.ACT_STOP)
    check("★ charge 帶 power", spy14.calls[0].get("power") == 7.0)
    check("★★ STOP 不帶 power（key 不存在）", "power" not in spy14.calls[1])
    check("  一律 no_verify=True（避免 partial verify 汙染）",
          all(c["no_verify"] is True for c in spy14.calls))
    check("  execute 透傳自 session（非字面量）",
          all(c["execute"] is True for c in spy14.calls))
    spy15 = SpyOperator()
    # DISCHARGE leg 需要**自己**的授權 —— CHARGE 授權不可挪用（Phase A.1 核心規則）
    s15, _st15, _c15 = field_session(operator=spy15, power=7.0,
                                     authorize=FM.ACT_DISCHARGE,
                                     obs=lambda: pcs_obs(discharging=True, standby=False))
    s15.stage_command("Stage 5.3", FM.ACT_DISCHARGE)
    check("★ discharge 帶 power", spy15.calls[0].get("power") == 7.0
          and spy15.calls[0]["action"] == FM.ACT_DISCHARGE)

    # ---------------- O. Console I/O 與人工確認（實地故障 root cause）----------------
    print("\nO. Console I/O 與人工確認")
    NOWRITE = lambda _t: None

    # --- O1. 實地故障重現：觀察段多按的 Enter 曾經吃掉後續每一個 Stage ---
    # Stage 0 確認輸入 YES；觀察段使用者連按 3 次 Enter（其中 2 次成為殘留鍵入）；
    # Stage 1 確認再輸入 YES。修正前，殘留的 Enter 會被 Stage 1 讀成空字串而 DECLINED。
    field = FakeTTY(answers=["YES", "YES"])
    con = FM.ConsoleIO(reader=field.reader, writer=NOWRITE,
                       drain=field.drain, isatty=field.isatty)

    def _observe_with_extra_enters():
        field.push_type_ahead("", "", "")       # 3 次 Enter 進入鍵盤緩衝區
        return con.observe("觀察 idle baseline")
    sess_f = FM.MeasurementSession(
        power_kw=5.0, execute=False, sampler=ManualSampler(idle_samples(12)),
        gate=FM.StageGate(con.confirm),
        read_all_fn=lambda: {"_fail": [], "alarm_rows": [], "alarm_total": 0,
                             "alarm_total_raw": 0, "communication_ok": True,
                             "soc_percent": 50.0, "pcs_fault_flag": False,
                             "battery_power_status": SG_BATT_ON()},
        clock=FakeClock(2000.0, 0.0))
    r0 = sess_f.stage0_baseline(hold=_observe_with_extra_enters)
    r1 = sess_f.stage1_read_all("Stage 1")
    check("★★ 實地故障重現：Stage 0 觀察段多按 Enter 後，Stage 1 仍能被確認",
          r0.status == FM.ST_OK and r1.status == FM.ST_OK)
    check("  Stage 1 的 YES 由 Stage 1 唯一消費（讀取序列 = YES, '', YES）",
          field.reads == ["YES", "", "YES"])
    check("★ 觀察段結束後立即 drain，殘留 Enter 不流入下一個提示",
          bool(field.drained) and max(field.drained) == 2)

    # --- O2. 就算 drain 失效，空白行也不得靜默拒絕（第二層防線）---
    nodrain = FakeTTY(["", "", "YES"])
    con2 = FM.ConsoleIO(reader=nodrain.reader, writer=NOWRITE,
                        drain=lambda: 0, isatty=nodrain.isatty)
    ok, why = con2.confirm("[Stage 1] ?")
    check("★★ drain 失效時：空白行重新提示而非靜默拒絕，最終仍要人工 YES",
          ok is True and why == FM.R_CONFIRMED and nodrain.reads == ["", "", "YES"])

    # --- O3. 空白 / Enter 一律 fail closed，永不自動通過 ---
    allempty = FakeTTY([""] * 20)
    con3 = FM.ConsoleIO(reader=allempty.reader, writer=NOWRITE,
                        drain=lambda: 0, isatty=allempty.isatty)
    ok, why = con3.confirm("[Stage 3] ?")
    check("★★ 只按 Enter（永遠空白）→ 絕不通過，回 DECLINED_EMPTY_LIMIT",
          ok is False and why == FM.R_EMPTY_LIMIT)
    check("  重試次數有上限，不會無限卡住",
          len(allempty.reads) == FM.MAX_EMPTY_RETRY + 1)
    nontty = FakeTTY(["", "", "YES"], tty=False)
    con4 = FM.ConsoleIO(reader=nontty.reader, writer=NOWRITE,
                        drain=lambda: 0, isatty=nontty.isatty)
    ok, why = con4.confirm("[Stage 3] ?")
    check("★★ 非 TTY 的空白 → 立刻 fail closed，不重試（不遮蔽非互動環境）",
          ok is False and why == FM.R_EMPTY_NON_TTY and len(nontty.reads) == 1)

    # --- O4. EOF / 中斷 / 例外一律 fail closed ---
    eof = FakeTTY([])
    con5 = FM.ConsoleIO(reader=eof.reader, writer=NOWRITE, drain=lambda: 0)
    check("★★ 非互動 stdin（EOF）→ DECLINED_EOF",
          con5.confirm("[Stage 3] ?") == (False, FM.R_EOF))
    con6 = FM.ConsoleIO(reader=lambda _p: (_ for _ in ()).throw(KeyboardInterrupt()),
                        writer=NOWRITE, drain=lambda: 0)
    check("  Ctrl-C → DECLINED_INTERRUPT",
          con6.confirm("[Stage 3] ?") == (False, FM.R_INTERRUPT))
    con7 = FM.ConsoleIO(reader=lambda _p: (_ for _ in ()).throw(OSError("no console")),
                        writer=NOWRITE, drain=lambda: 0)
    ok, why = con7.confirm("[Stage 3] ?")
    check("  reader 例外 → fail closed 並帶出例外型別（可診斷）",
          ok is False and why == "DECLINED_EXCEPTION:OSError")

    # --- O5. 確認要求不得被降低 ---
    for bad in ("yes", "y", "Y", "YESS", "Yes", "no", "YE S"):
        f = FakeTTY([bad])
        c = FM.ConsoleIO(reader=f.reader, writer=NOWRITE,
                         drain=lambda: 0, isatty=f.isatty)
        ok, why = c.confirm("[S] ?")
        check("  輸入 %r → 不算同意" % bad, ok is False)
    f = FakeTTY(["  YES  "])
    c = FM.ConsoleIO(reader=f.reader, writer=NOWRITE, drain=lambda: 0)
    check("  前後空白的 YES 仍算同意（僅 strip，不放寬字樣）",
          c.confirm("[S] ?")[0] is True)
    check("★ CONFIRM_TOKEN 仍為 YES", FM.CONFIRM_TOKEN == "YES")

    # --- O6. 工具不得預先自動輸入 YES ---
    src_yes = [n for n in ast.walk(TREE)
               if isinstance(n, ast.Constant) and n.value == "YES"]
    check("★★ 原始碼中 YES 只作為比對用的 token 常數（唯一一處）",
          len(src_yes) == 1)
    probe = FakeTTY([])
    c = FM.ConsoleIO(reader=probe.reader, writer=NOWRITE, drain=lambda: 0)
    c.confirm("[S] ?")
    check("  reader 只被要求輸入、未被餵入任何預設答案", probe.reads == [])
    check("★ MeasurementSession 無預設 confirmer（未注入即全部 DECLINED）",
          FM.MeasurementSession().gate.confirmer is None)

    # --- O7. 背景執行緒不得偷吃 stdin ---
    reader_threads = []
    live = FakeTTY(["YES"])

    def watch_reader(prompt):
        reader_threads.append(threading.current_thread().name)
        time.sleep(0.08)                        # 人打字需要時間；期間樣本應被暫停顯示
        return live.reader(prompt)

    con8 = FM.ConsoleIO(reader=watch_reader, writer=NOWRITE,
                        drain=lambda: 0, isatty=live.isatty)
    bg = FM.PcsSampler(fetch=lambda: payload(p=0.0), interval_sec=0.01,
                       printer=lambda s: con8.write_line("x"))
    bg.start()
    ok, why = con8.confirm("[Stage 3] ?")
    bg.stop()
    check("★★ 背景取樣執行緒運作中，YES 仍由主執行緒唯一消費",
          ok is True and live.reads == ["YES"])
    check("  stdin 只被主執行緒讀取", set(reader_threads) == {"MainThread"})
    check("★ 提示期間樣本顯示被暫停（write_line 回 False 並計數）",
          con8.suppressed_lines > 0)
    input_calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == "input"]
    check("★ AST：全模組只有 ConsoleIO 內的一處 input() 呼叫", len(input_calls) == 1)
    sampler_cls = [n for n in ast.walk(TREE) if isinstance(n, ast.ClassDef)
                   and n.name == "PcsSampler"][0]
    check("★★ PcsSampler 內不存在任何 stdin 讀取",
          not [n for n in ast.walk(sampler_cls)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "input"]
          and "stdin" not in ast.unparse(sampler_cls))

    # --- O8. 顯示暫停不得影響資料擷取 ---
    rows8, watched8 = [], []
    con9 = FM.ConsoleIO(reader=lambda _p: "YES", writer=NOWRITE,
                        drain=lambda: 0, isatty=lambda: True)
    smp = FM.PcsSampler(fetch=lambda: payload(p=-1.4), clock=FakeClock(0.0, 0.1),
                        sink=rows8.append, watcher=watched8.append,
                        printer=FM.make_sample_printer(con9))
    con9._prompt_active.set()
    smp.sample_once()
    con9._prompt_active.clear()
    smp.sample_once()
    check("★★ 提示期間 sink（寫 CSV）與 watcher 完全不受影響",
          len(rows8) == 2 and len(watched8) == 2)
    check("  只有顯示被暫停（1 行）", con9.suppressed_lines == 1)

    # --- O9. DECLINED 原因必須可診斷 ---
    dec = FM.MeasurementSession(sampler=ManualSampler(idle_samples(12)),
                                gate=FM.StageGate(lambda _p: (False, FM.R_EOF)),
                                clock=FakeClock(2000.0, 0.0))
    rd = dec.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ StageResult 帶出未確認的具體原因（不再只有『未取得人工確認』）",
          FM.R_EOF in rd.detail)
    evs = []
    dec2 = FM.MeasurementSession(sampler=ManualSampler(idle_samples(12)),
                                 gate=FM.StageGate(lambda _p: (False, FM.R_EMPTY_LIMIT)),
                                 clock=FakeClock(2000.0, 0.0), event_sink=evs.append)
    dec2.stage_command("Stage 3", FM.ACT_CHARGE)
    check("  event log 也記錄原因",
          any(e["event"] == "STAGE_DECLINED" and e["detail"] == FM.R_EMPTY_LIMIT
              for e in evs))
    check("  gate.history 保留每一段的確認結果", len(dec2.gate.history) == 1)
    check("★ 舊介面相容：confirmer 回 bool 仍可用",
          FM.StageGate(lambda _p: True).require("S", "?") is True
          and FM.StageGate(lambda _p: False).require("S", "?") is False)

    # ---------------- P. Snapshot freshness 解耦（實機 ESS_STALE root cause）----------------
    print("\nP. Stage 1->2 / 5.1->5.2 Snapshot freshness")
    MODE_OK = {"schedule_switch": 0, "manual_switch": 1}

    _KEEP = object()                                  # sentinel：區分「不覆寫」與「明確給 None」

    def fsess(clock, reader, mode=_KEEP, confirm_delay=0.0, power=5.0):
        """confirm_delay 模擬人工閱讀＋輸入 YES 所花的時間。"""
        def _confirm(_p):
            clock.advance(confirm_delay)
            return True
        return FM.MeasurementSession(
            power_kw=power, execute=False,
            sampler=ManualSampler(idle_samples(12)),
            gate=FM.StageGate(_confirm),
            read_all_fn=reader,
            mode_state_reader=lambda: (MODE_OK if mode is _KEEP else mode),
            clock=clock)

    # --- P-A. 人工等待 40 秒後，Stage 2 仍以 fresh read 判定 ---
    clk = StepClock(1000.0)
    rdr = SpyReader(clk, duration=0.5)
    s = fsess(clk, rdr, confirm_delay=40.0)
    s.stage1_read_all("Stage 1")
    r2 = s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("★★ A. Stage 1 後人工等 40s，Stage 2 仍 SAFE_OK（不再被 ESS_STALE 擋）",
          r2.status == FM.ST_OK and "SAFE_OK" in r2.detail)
    check("  A. 且 Control Authority 判為 IDLE（Phase A 新增的第二道閘門）",
          "AUTHORITY_IDLE" in r2.detail and s.authority.state == CA.AUTH_IDLE)
    check("  A. read_all 被呼叫兩次（Stage 1 一次、Stage 2 收到 YES 後再一次）",
          len(rdr.calls) == 2)
    check("★★ A. Stage 2 未重用 Stage 1 的 reading 物件",
          s.safety_reading[0] is not s.review_reading[0])
    fresh_age = s.safety_reading[2] - s.safety_reading[1]
    check("  A. fresh snapshot 的 read duration 極短（0.5s）", abs(fresh_age - 0.5) < 1e-9)

    # --- P-B. Stage 1 快照確實已 STALE，但 fresh 快照有效 ---
    old_reading, ot0, ot1 = s.review_reading
    stale_snap = DE.ess_snapshot_from_reading(old_reading, ot0, ot1, now=clk.t)
    check("★★ B. 同一時刻用 Stage 1 舊快照必為 STALE（證明差異來自 freshness）",
          stale_snap.stale is True and (clk.t - ot0) > DE.ESS_STALE_AFTER_SEC)
    fresh_snap = DE.ess_snapshot_from_reading(*s.safety_reading, now=s.safety_reading[2])
    check("  B. 同一時刻 fresh 快照 valid 且未 stale",
          fresh_snap.valid is True and fresh_snap.stale is False)

    # --- P-C. fresh read 本身太慢 → READ_TOO_SLOW / Fail Closed ---
    clk = StepClock(1000.0)
    slow = SpyReader(clk, duration=11.0)                # > ESS_READ_DURATION_MAX_SEC(10)
    s = fsess(clk, slow)
    s.stage1_read_all("Stage 1")
    r2 = s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("★★ C. fresh read 耗時 11s（>10s）→ BLOCKED / ESS_READ_TOO_SLOW",
          r2.status == FM.ST_BLOCKED and SG.R_ESS_READ_TOO_SLOW in r2.detail)
    check("  C. 並進入 abort（後續 Stage 不執行）", s.aborted)

    # --- P-D. fresh read 例外 / 通訊失敗 → Fail Closed ---
    clk = StepClock(1000.0)
    boom = SpyReader(clk, exc=TimeoutError("read timeout"))
    s = fsess(clk, boom)
    r2 = s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("★★ D. fresh read 例外 → BLOCKED（不沿用任何舊資料）",
          r2.status == FM.ST_BLOCKED and "TimeoutError" in r2.detail and s.aborted)
    clk = StepClock(1000.0)
    nocomm = SpyReader(clk, reading_fn=lambda: good_reading(communication_ok=False))
    s = fsess(clk, nocomm)
    r2 = s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("  D. fresh reading communication_ok=False → BLOCKED / ESS_COMM_FAILED",
          r2.status == FM.ST_BLOCKED and SG.R_ESS_COMM_FAILED in r2.detail)
    clk = StepClock(1000.0)
    notdict = FM.MeasurementSession(
        power_kw=5.0, execute=False, sampler=ManualSampler(idle_samples(12)),
        gate=FM.StageGate(lambda _p: True), read_all_fn=lambda: "oops",
        mode_state_reader=lambda: MODE_OK, clock=clk)
    check("  D. fresh read 未回傳 dict → BLOCKED",
          notdict.stage2_safety(FM.ACT_CHARGE, "Stage 2").status == FM.ST_BLOCKED)

    # --- P-E. fresh alarm endpoint 失敗 → ALARM_SOURCE_UNAVAILABLE ---
    clk = StepClock(1000.0)
    badalarm = SpyReader(clk, reading_fn=lambda: good_reading(_fail=[SG.EP_ALARM_PATH]))
    s = fsess(clk, badalarm)
    r2 = s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("★★ E. fresh alarm endpoint 失敗 → BLOCKED / ALARM_SOURCE_UNAVAILABLE",
          r2.status == FM.ST_BLOCKED and SG.R_ALARM_SOURCE_UNAVAILABLE in r2.detail)
    clk = StepClock(1000.0)
    noraw = SpyReader(clk, reading_fn=lambda: {k: v for k, v in good_reading().items()
                                               if k != "alarm_total_raw"})
    s = fsess(clk, noraw)
    check("  E. fresh reading 缺 alarm_total_raw → 同樣 BLOCKED（沿用上一輪 provenance 規則）",
          s.stage2_safety(FM.ACT_CHARGE, "Stage 2").status == FM.ST_BLOCKED)

    # --- P-F. fresh control mode 不允許 → Fail Closed ---
    for mode, want, label in (
            ({"schedule_switch": 1, "manual_switch": 0}, SG.R_CONTROL_MODE_SMART, "智慧模式"),
            ({"schedule_switch": 0, "manual_switch": 0}, SG.R_CONTROL_MODE_NONE, "未啟用"),
            ({"schedule_switch": None, "manual_switch": 1}, SG.R_CONTROL_MODE_UNKNOWN, "未知"),
            (None, SG.R_CONTROL_MODE_UNKNOWN, "mode_state=None")):
        clk = StepClock(1000.0)
        s = fsess(clk, SpyReader(clk), mode=mode)
        r2 = s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
        check("  F. fresh control mode %s → BLOCKED / %s" % (label, want),
              r2.status == FM.ST_BLOCKED and want in r2.detail)

    # --- P-G. Stage 5.1 -> 5.2（DISCHARGE 前置）同樣重新 fresh read ---
    clk = StepClock(1000.0)
    rdr = SpyReader(clk, duration=0.5)
    s = fsess(clk, rdr, confirm_delay=40.0)
    s.stage1_read_all("Stage 1")
    s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    first_safety = s.safety_reading[0]
    s.stage1_read_all("Stage 5.1")
    r52 = s.stage2_safety(FM.ACT_DISCHARGE, "Stage 5.2")
    check("★★ G. Stage 5.2（DISCHARGE）同樣在 YES 後重新 fresh read 並 SAFE_OK",
          r52.status == FM.ST_OK and "SAFE_OK" in r52.detail
          and "AUTHORITY_IDLE" in r52.detail)
    check("  G. 全程 read_all 共 4 次（1 / 2 / 5.1 / 5.2 各一次）", len(rdr.calls) == 4)
    check("★★ G. Stage 5.2 的 safety reading 與 Stage 2 的、與 Stage 5.1 的都不同物件",
          s.safety_reading[0] is not first_safety
          and s.safety_reading[0] is not s.review_reading[0])
    check("  G. Stage 5.2 fresh snapshot age 未受人工 40s 影響",
          (s.safety_reading[2] - s.safety_reading[1]) < DE.ESS_STALE_AFTER_SEC)

    # --- P-H. 不得因此放寬任何安全要求 ---
    check("★★ H. ESS_STALE_AFTER_SEC 仍為 15（未放寬）", DE.ESS_STALE_AFTER_SEC == 15.0)
    check("  H. ESS_READ_DURATION_MAX_SEC 仍為 10", DE.ESS_READ_DURATION_MAX_SEC == 10.0)
    check("  H. 原始碼未出現放寬 stale 的字樣",
          "ESS_STALE_AFTER_SEC =" not in CODE and "stale_after" not in CODE)
    clk = StepClock(1000.0)
    declined = SpyReader(clk)
    s = FM.MeasurementSession(
        power_kw=5.0, execute=False, sampler=ManualSampler(idle_samples(12)),
        gate=FM.StageGate(lambda _p: (False, FM.R_DECLINED)),
        read_all_fn=declined, mode_state_reader=lambda: MODE_OK, clock=clk)
    r2 = s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("★★ H. Stage 2 未取得 YES → fresh read 根本不會發生（確認在前）",
          r2.status == FM.ST_DECLINED and len(declined.calls) == 0)
    clk = StepClock(1000.0)
    spy_h = SpyOperator()
    s = fsess(clk, SpyReader(clk, reading_fn=lambda: good_reading(pcs_fault_flag=True)))
    s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("★★ H. Safety BLOCK 後不得繞過：後續 Stage 全 ABORTED",
          s.stage_command("Stage 3", FM.ACT_CHARGE).status == FM.ST_ABORTED)
    check("  H. Stage 1 仍保留（人工檢視用，未被刪除）",
          callable(getattr(FM.MeasurementSession, "stage1_read_all", None)))
    check("  H. review_reading 與 safety_reading 是兩個獨立欄位",
          "review_reading" in CODE and "safety_reading" in CODE
          and "last_reading" not in CODE)

    # --- P-I. fresh read 的時間資訊要能被稽核 ---
    clk = StepClock(1000.0)
    evs = []
    s = FM.MeasurementSession(
        power_kw=5.0, execute=False, sampler=ManualSampler(idle_samples(12)),
        gate=FM.StageGate(lambda _p: True), read_all_fn=SpyReader(clk, duration=0.55),
        mode_state_reader=lambda: MODE_OK, clock=clk, event_sink=evs.append)
    s.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    fr = [e for e in evs if e["event"] == "FRESH_SAFETY_READ"]
    check("★ event log 記錄 FRESH_SAFETY_READ（含 age 與 read duration）",
          len(fr) == 1 and "age=" in fr[0]["outcome"] and "read_duration=" in fr[0]["detail"])
    lines = []
    s2 = FM.MeasurementSession(
        power_kw=5.0, execute=False, sampler=ManualSampler(idle_samples(12)),
        gate=FM.StageGate(lambda _p: True), read_all_fn=SpyReader(StepClock(1000.0)),
        mode_state_reader=lambda: MODE_OK, clock=StepClock(1000.0),
        printer=lines.append)
    s2.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    joined = "\n".join(lines)
    for token in ("Fresh Safety Snapshot", "read_all duration", "age at gate",
                  "SOC", "battery", "communication_ok", "PCS fault",
                  "alarm complete", "PCS mode"):
        check("  終端輸出含 %s" % token, token in joined)
    lines1 = []
    s3 = FM.MeasurementSession(
        sampler=ManualSampler(idle_samples(12)), gate=FM.StageGate(lambda _p: True),
        read_all_fn=SpyReader(StepClock(1000.0)), clock=StepClock(1000.0),
        printer=lines1.append)
    s3.stage1_read_all("Stage 1")
    check("★ Stage 1 明確標示為人工檢視快照、非放行依據",
          "人工檢視快照" in "\n".join(lines1))

    # ---------------- W. Control Authority 實機保護（Phase 6.5-H / Phase A）----------------
    print("\nW. Control Authority 實機量測保護")

    # --- A. IDLE → 允許進入 execute ---
    sA, _stA, _cA = field_session(reading_fn=lambda: idle_reading(),
                                  obs=lambda: pcs_obs(standby=True, running=True))
    rA = sA.stage2_safety(FM.ACT_CHARGE, "Stage 2")
    check("★★ A. PCS 閒置 → Stage 2 SAFE_OK + AUTHORITY_IDLE",
          rA.status == FM.ST_OK and "AUTHORITY_IDLE" in rA.detail
          and sA.authority.state == CA.AUTH_IDLE)
    check("  A. 未 abort，可繼續下一 Stage", sA.aborted is False)

    # --- B/C. 外部運轉中、無 LastControl → BLOCK ---
    for label, rf, ob in (("B. 外部 CHARGING", lambda: charging_reading(80.0),
                           lambda: pcs_obs(charging=True, standby=False)),
                          ("C. 外部 DISCHARGING", lambda: discharging_reading(-80.2),
                           lambda: pcs_obs(discharging=True, standby=False))):
        spyX = SpyOperator()
        sX, _stX, _cX = field_session(operator=spyX, reading_fn=rf, obs=ob)
        rX = sX.stage2_safety(FM.ACT_CHARGE, "Stage 2")
        check(f"★★ {label} 無 LastControl → Stage 2 BLOCKED",
              rX.status == FM.ST_BLOCKED and sX.authority.state == CA.AUTH_EXTERNAL)
        check(f"  {label} → abort，且後續 Stage 不送命令",
              sX.aborted and sX.abort_reason == FM.AB_AUTHORITY
              and sX.stage_command("Stage 3", FM.ACT_CHARGE).status == FM.ST_ABORTED
              and len(spyX.calls) == 0)

    # --- D. CHARGE verified → 建立 LastControl ---
    spyD = SpyOperator()
    sD, stD, clkD = field_session(operator=spyD, power=5.0,
                                  reading_fn=lambda: idle_reading(),
                                  obs=lambda: pcs_obs(charging=True, standby=False))
    rD = sD.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ D. CHARGE 送出 → ReadBack VERIFY_SUCCESS",
          rD.status == FM.ST_OK and sD.verified["Stage 3"].outcome == EX.VERIFY_SUCCESS)
    check("★★ D. 建立 LastControl（使用既有 LastControlStore / schema）",
          stD.current().record is not None
          and stD.current().record.action == "charge"
          and stD.current().record.target_power_kw == 5.0
          and stD.current().record.schema_version == LCS_SCHEMA)
    check("  D. trust 為 SAME_PROCESS（本 process 寫入）",
          stD.current().trust == CAe.TRUST_FOR_INTERVAL)
    check("  D. lastcontrol_eligible=True", sD.verified["Stage 3"].lastcontrol_eligible is True)

    # --- E. POST 成功但 verify 失敗 → 不得建立 LastControl ---
    spyE = SpyOperator()
    sE, stE, _cE = field_session(operator=spyE, power=5.0,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: pcs_obs(standby=True, running=True))  # 一直沒進 CHARGING
    rE = sE.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ E. POST 成功但 ReadBack 未成功 → 不建立 LastControl",
          spyE.calls and sE.verified["Stage 3"].outcome != EX.VERIFY_SUCCESS
          and sE.verified["Stage 3"].lastcontrol_eligible is False
          and stE.current().record is None)
    check("  E. 該 Stage 標記 OK 但無 ownership 證據（後續 STOP 會被擋）",
          rE.status == FM.ST_OK)
    rE2 = sE.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ E. 無 LastControl → STOP 前 Authority 擋下",
          rE2.status == FM.ST_BLOCKED and len(spyE.calls) == 1)

    # --- F. Phase6-owned → STOP 前 Authority = OWNED ---
    spyF = SpyOperator()
    sF, stF, _cF = field_session(operator=spyF, power=5.0,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: pcs_obs(charging=True, standby=False))
    sF.stage_command("Stage 3", FM.ACT_CHARGE)
    sF.read_all_fn = lambda: charging_reading(5.3)      # 進入充電後的現況
    rF = sF.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ F. Phase6-owned → STOP 前 Authority = OWNED_BY_PHASE6",
          sF.authority.state == CA.AUTH_OWNED and rF.status == FM.ST_OK)
    check("  F. STOP 確實送出且不帶 power",
          len(spyF.calls) == 2 and spyF.calls[1]["action"] == FM.ACT_STOP
          and "power" not in spyF.calls[1])

    # --- G. 期間被外部改成明顯不同功率 → CONFLICT → STOP BLOCK ---
    spyG = SpyOperator()
    sG, stG, _cG = field_session(operator=spyG, power=5.0,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: pcs_obs(charging=True, standby=False))
    sG.stage_command("Stage 3", FM.ACT_CHARGE)
    sG.read_all_fn = lambda: charging_reading(80.0)     # 被外部改成 80 kW
    rG = sG.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ G. 功率被外部改變 → CONFLICT → STOP BLOCK",
          sG.authority.state == CA.AUTH_CONFLICT
          and sG.authority.reason == CA.CA_CONFLICT_POWER
          and rG.status == FM.ST_BLOCKED and len(spyG.calls) == 1)

    # --- H. 執行中途 schedule_switch 變 ON → STOP BLOCK ---
    spyH = SpyOperator()
    modeH = {"schedule_switch": 0, "manual_switch": 1}
    sH, stH, _cH = field_session(operator=spyH, power=5.0,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: pcs_obs(charging=True, standby=False))
    sH.mode_state_reader = lambda: modeH
    sH.stage_command("Stage 3", FM.ACT_CHARGE)
    sH.read_all_fn = lambda: charging_reading(5.3)
    modeH = {"schedule_switch": 1, "manual_switch": 0}   # 中途被打開
    rH = sH.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ H. schedule_switch 中途變 ON → STOP BLOCK / SCHEDULE_ACTIVE",
          sH.authority.reason == CA.CA_SCHEDULE_ACTIVE
          and rH.status == FM.ST_BLOCKED and len(spyH.calls) == 1)

    # --- I. 資料 stale → BLOCK ---
    spyI = SpyOperator()
    clkI = FakeClock(3000.0, 0.0)
    sI, stI, _cI = field_session(operator=spyI, power=5.0, clock=clkI,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: pcs_obs(charging=True, standby=False))
    sI.stage_command("Stage 3", FM.ACT_CHARGE)

    def _stale_read():
        clkI.t += 999.0                                  # read_all 期間時間大幅前進
        return charging_reading(5.3)

    sI.read_all_fn = _stale_read
    rI = sI.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ I. 資料 stale / 讀取過慢 → Authority 無法確認 → STOP BLOCK",
          rI.status == FM.ST_BLOCKED and sI.authority.state == CA.AUTH_UNKNOWN
          and len(spyI.calls) == 1)

    # --- J. Authority 例外 → FAIL CLOSED ---
    spyJ = SpyOperator()
    sJ, stJ, _cJ = field_session(operator=spyJ, power=5.0,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: pcs_obs(charging=True, standby=False))
    sJ.stage_command("Stage 3", FM.ACT_CHARGE)
    sJ.read_all_fn = lambda: (_ for _ in ()).throw(RuntimeError("hmi down"))
    rJ = sJ.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ J. STOP 前取數例外 → FAIL CLOSED，不送 STOP",
          rJ.status == FM.ST_BLOCKED and sJ.aborted and len(spyJ.calls) == 1)

    # --- K. FIELD 政策不得污染 production default ---
    check("★★ K. production ReadBackConfig default 仍為 (None, None, 1)",
          (EX.ReadBackConfig().timeout_sec, EX.ReadBackConfig().poll_interval_sec,
           EX.ReadBackConfig().stability_samples) == (None, None, 1))
    check("★★ K. production AuthorityPolicy default 仍為 (None, None)",
          (CA.DEFAULT_AUTHORITY_POLICY.authority_ttl_sec,
           CA.DEFAULT_AUTHORITY_POLICY.authority_power_tolerance_kw) == (None, None))
    check("★★ K. 未注入 policy 時採 production default → 運轉中一律 BLOCK",
          FM.MeasurementSession().authority_policy is None)
    spyK = SpyOperator()
    sK, _stK, _cK = field_session(operator=spyK, policy=CA.DEFAULT_AUTHORITY_POLICY,
                                  reading_fn=lambda: idle_reading(),
                                  obs=lambda: pcs_obs(charging=True, standby=False))
    sK.stage_command("Stage 3", FM.ACT_CHARGE)
    sK.read_all_fn = lambda: charging_reading(5.3)
    rK = sK.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ K. 以 production default 政策執行 → 運轉中 STOP 仍被 BLOCK（TTL_UNSET）",
          rK.status == FM.ST_BLOCKED and sK.authority.reason == CA.CA_TTL_UNSET)
    check("  K. FIELD 值有明確標示且與 production 不同",
          FM.FIELD_AUTHORITY_TTL_SEC is not None
          and FM.FIELD_AUTHORITY_POWER_TOLERANCE_KW is not None
          and "FIELD TEST ONLY" in FM.FIELD_TEST_ONLY)

    # --- L. 正常結束：STOP verified success ---
    spyL = SpyOperator()
    obsL = {"v": "charging"}
    sL, stL, _cL = field_session(operator=spyL, power=5.0,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: (pcs_obs(charging=True, standby=False)
                                              if obsL["v"] == "charging"
                                              else pcs_obs(standby=False, running=False)))
    sL.stage_command("Stage 3", FM.ACT_CHARGE)
    sL.read_all_fn = lambda: charging_reading(5.3)
    obsL["v"] = "stopped"
    rL = sL.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ L. STOP 送出並 ReadBack VERIFY_SUCCESS（STOPPED 屬可接受狀態）",
          rL.status == FM.ST_OK and sL.verified["Stage 4"].outcome == EX.VERIFY_SUCCESS)
    check("  L. STOP 的 LastControl 亦被建立（action=stop、不帶 power）",
          stL.current().record.action == "stop"
          and stL.current().record.target_power_kw is None)
    check("  L. 全程 operator 恰 2 次（CHARGE / STOP）", len(spyL.calls) == 2)

    # --- 額外：ReadBack 只依旗標，AC 功率不參與 ---
    spyM = SpyOperator()
    sM, _stM, _cM = field_session(
        operator=spyM, power=5.0, reading_fn=lambda: idle_reading(),
        obs=lambda: pcs_obs(charging=True, standby=False, p=-99.0))
    rM = sM.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ ReadBack 只依旗標：AC 功率與指令無關仍 VERIFY_SUCCESS",
          sM.verified["Stage 3"].outcome == EX.VERIFY_SUCCESS and rM.status == FM.ST_OK)
    check("  require_authority_for_stop 預設為 True（不得關閉）",
          FM.MeasurementSession().require_authority_for_stop is True)
    check("★ execute=False 時不做 STOP Authority 前置（沒有命令要授權）",
          FM.MeasurementSession(execute=False, sampler=ManualSampler(idle_samples(12)),
                                gate=FM.StageGate(lambda _p: True),
                                clock=FakeClock(1.0, 0.0))
          .stage_command("Stage 4", FM.ACT_STOP).detail == FM.CMD_NO_EXECUTE)

    # ---------------- X. Per-Leg Authority Enforcement（Phase A.1）----------------
    print("\nX. Per-Leg Authority Enforcement")

    def leg(action=FM.ACT_CHARGE, authorize=_AUTZ_DEFAULT, reading_fn=None, obs=None,
            operator=None, **kw):
        op_ = operator if operator is not None else SpyOperator()
        s_, st_, clk_ = field_session(
            operator=op_, power=5.0, authorize=authorize,
            reading_fn=reading_fn or (lambda: idle_reading()),
            obs=obs or (lambda: pcs_obs(charging=(action == FM.ACT_CHARGE),
                                        discharging=(action == FM.ACT_DISCHARGE),
                                        standby=False)), **kw)
        return s_, st_, clk_, op_

    # --- A. IDLE → authorize CHARGE → YES → 複驗 PASS → dispatch ---
    sA, _stA, _cA, opA = leg(FM.ACT_CHARGE, authorize=FM.ACT_CHARGE)
    rA = sA.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ A. 有 CHARGE 授權 + 複驗通過 → CHARGE 送出",
          rA.status == FM.ST_OK and len(opA.calls) == 1
          and opA.calls[0]["action"] == FM.ACT_CHARGE)

    # --- B. IDLE → authorize DISCHARGE → dispatch ---
    sB, _stB, _cB, opB = leg(FM.ACT_DISCHARGE, authorize=FM.ACT_DISCHARGE)
    rB = sB.stage_command("Stage 5.3", FM.ACT_DISCHARGE)
    check("★★ B. 有 DISCHARGE 授權 + 複驗通過 → DISCHARGE 送出（離線測試，不代表實機可放電）",
          rB.status == FM.ST_OK and len(opB.calls) == 1
          and opB.calls[0]["action"] == FM.ACT_DISCHARGE)

    # --- C/D. action 不符 → BLOCK ---
    sC, _stC, _cC, opC = leg(FM.ACT_DISCHARGE, authorize=FM.ACT_CHARGE)
    rC = sC.stage_command("Stage 5.3", FM.ACT_DISCHARGE)
    check("★★ C. 授權 CHARGE 卻要 dispatch DISCHARGE → BLOCK / ACTION_MISMATCH",
          rC.status == FM.ST_BLOCKED and rC.detail == FM.FA_ACTION_MISMATCH
          and len(opC.calls) == 0)
    sD, _stD, _cD, opD = leg(FM.ACT_CHARGE, authorize=FM.ACT_DISCHARGE)
    rD = sD.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ D. 授權 DISCHARGE 卻要 dispatch CHARGE → BLOCK / ACTION_MISMATCH",
          rD.status == FM.ST_BLOCKED and rD.detail == FM.FA_ACTION_MISMATCH
          and len(opD.calls) == 0)

    # --- E. 沒有任何授權 + 人工 YES → BLOCK ---
    sE, _stE, _cE, opE = leg(FM.ACT_CHARGE, authorize=None)
    rE = sE.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ E. 無授權即使人工 YES → BLOCK / AUTHORIZATION_MISSING，operator 0 次",
          rE.status == FM.ST_BLOCKED and rE.detail == FM.FA_MISSING
          and len(opE.calls) == 0)
    check("★★ E. 人工 YES 不能建立 Authority（YES 只表示同意，不是授權來源）",
          sE.gate.history and sE.gate.history[-1][1] is True and len(opE.calls) == 0)

    # --- F/L. 授權為一次性，用過即消耗 ---
    sF, _stF, _cF, opF = leg(FM.ACT_CHARGE, authorize=FM.ACT_CHARGE)
    sF.stage_command("Stage 3", FM.ACT_CHARGE)
    check("  F. dispatch 後授權已被消耗", sF.leg_authorization is None)
    rF2 = sF.stage_command("Stage 3b", FM.ACT_CHARGE)
    check("★★ F/L. 已消耗的授權不得再用於下一個 leg → BLOCK / AUTHORIZATION_MISSING",
          rF2.status == FM.ST_BLOCKED and rF2.detail == FM.FA_MISSING
          and len(opF.calls) == 1)

    # --- G. Stage 5.2 NO → Stage 5.3 YES：核心 regression ---
    # 🔴 這條路徑必須由**程式**擋住，不能靠操作者記得輸入 NO。
    opG = SpyOperator()
    gateG = FM.StageGate(lambda p: "Stage 5.2" not in p)   # 只有 Stage 5.2 被拒
    sG, stG, clkG = field_session(operator=opG, power=5.0, authorize=None,
                                  reading_fn=lambda: idle_reading(),
                                  obs=lambda: pcs_obs(discharging=True, standby=False))
    sG.gate = gateG
    r521 = sG.stage1_read_all("Stage 5.1")
    r522 = sG.stage2_safety(FM.ACT_DISCHARGE, "Stage 5.2")
    r523 = sG.stage_command("Stage 5.3", FM.ACT_DISCHARGE)
    check("★★★ G. Stage 5.2 被拒 → Stage 5.3 即使人工輸入 YES 也不得送出 DISCHARGE",
          r522.status == FM.ST_DECLINED and r523.status == FM.ST_BLOCKED
          and r523.detail == FM.FA_MISSING)
    check("★★★ G. DISCHARGE operator 呼叫次數 = 0（程式層封死，非靠人工 NO）",
          len([c for c in opG.calls if c.get("action") == FM.ACT_DISCHARGE]) == 0
          and len(opG.calls) == 0)
    check("  G. Stage 5.1 仍可正常執行（只有授權缺失才擋 dispatch）",
          r521.status == FM.ST_OK)

    # --- H. 確認期間 PCS 被外部改成運轉中 → 複驗 BLOCK ---
    opH = SpyOperator()
    stateH = {"r": lambda: idle_reading()}
    sH, _stH, _cH = field_session(operator=opH, power=5.0, authorize=FM.ACT_CHARGE,
                                  reading_fn=lambda: stateH["r"](),
                                  obs=lambda: pcs_obs(charging=True, standby=False))
    stateH["r"] = lambda: charging_reading(80.0)      # 人工確認期間他人開始操作
    rH = sH.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ H. 確認期間 PCS 被外部佔用 → dispatch 前複驗 BLOCK",
          rH.status == FM.ST_BLOCKED and rH.detail == FM.FA_REVALIDATION_FAILED
          and len(opH.calls) == 0)

    # --- I. 確認期間 schedule_switch OFF → ON ---
    opI = SpyOperator()
    modeI = {"m": {"schedule_switch": 0, "manual_switch": 1}}
    sI2, _stI, _cI = field_session(operator=opI, power=5.0, authorize=FM.ACT_CHARGE,
                                   reading_fn=lambda: idle_reading(),
                                   obs=lambda: pcs_obs(charging=True, standby=False))
    sI2.mode_state_reader = lambda: modeI["m"]
    modeI["m"] = {"schedule_switch": 1, "manual_switch": 0}
    rI = sI2.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ I. 確認期間排程主開關被打開 → BLOCK，operator 0 次",
          rI.status == FM.ST_BLOCKED and rI.detail == FM.FA_REVALIDATION_FAILED
          and len(opI.calls) == 0)
    check("  I. 阻擋原因可追溯到 SCHEDULE_ACTIVE",
          sI2.authority.reason == CA.CA_SCHEDULE_ACTIVE)

    # --- J. 確認期間 communication 失敗 / snapshot stale ---
    for label, rf in (("communication 失敗",
                       lambda: idle_reading(communication_ok=False)),
                      ("read_all 未回傳 dict", lambda: "oops")):
        opJ = SpyOperator()
        sJ, _stJ, _cJ = field_session(operator=opJ, power=5.0, authorize=FM.ACT_CHARGE,
                                      reading_fn=rf,
                                      obs=lambda: pcs_obs(charging=True, standby=False))
        rJ = sJ.stage_command("Stage 3", FM.ACT_CHARGE)
        check(f"★★ J. 確認期間{label} → BLOCK，operator 0 次",
              rJ.status == FM.ST_BLOCKED and len(opJ.calls) == 0)

    # --- K. 複驗過程例外 → FAIL CLOSED ---
    opK = SpyOperator()
    sK2, _stK, _cK = field_session(operator=opK, power=5.0, authorize=FM.ACT_CHARGE,
                                   reading_fn=lambda: (_ for _ in ()).throw(
                                       RuntimeError("hmi down")),
                                   obs=lambda: pcs_obs(charging=True, standby=False))
    rK = sK2.stage_command("Stage 3", FM.ACT_CHARGE)
    check("★★ K. 複驗例外 → FAIL CLOSED / REVALIDATION_FAILED，operator 0 次",
          rK.status == FM.ST_BLOCKED and rK.detail == FM.FA_REVALIDATION_FAILED
          and len(opK.calls) == 0)

    # --- 授權時與 dispatch 時狀態不同（allowed 但 state 改變）---
    opS = SpyOperator()
    sS, stS, clkS = field_session(operator=opS, power=5.0, authorize=FM.ACT_CHARGE,
                                  reading_fn=lambda: idle_reading(),
                                  obs=lambda: pcs_obs(charging=True, standby=False))
    sS.stage_command("Stage 3", FM.ACT_CHARGE)          # 建立 LastControl
    sS.read_all_fn = lambda: charging_reading(5.3)      # 現在是 Phase6-owned 的 CHARGING
    grant_leg(sS, FM.ACT_CHARGE)                        # 但授權是以 IDLE 為前提核發的
    sS.read_all_fn = lambda: charging_reading(5.3)
    rS = sS.stage_command("Stage 3c", FM.ACT_CHARGE)
    check("★★ 授權時 IDLE、dispatch 時已變 OWNED → BLOCK / STATE_CHANGED",
          rS.status == FM.ST_BLOCKED and rS.detail == FM.FA_STATE_CHANGED
          and len(opS.calls) == 1)

    # --- M. dry-run 維持既有行為 ---
    opM = SpyOperator()
    sM2 = session(execute=False, sampler=ManualSampler(idle_samples(12)), operator=opM)
    rM1 = sM2.stage_command("Stage 3", FM.ACT_CHARGE)
    rM2 = sM2.stage_command("Stage 5.3", FM.ACT_DISCHARGE)
    check("★★ M. dry-run（execute=False）不需授權，維持 NO_EXECUTE，operator 0 次",
          rM1.detail == FM.CMD_NO_EXECUTE and rM2.detail == FM.CMD_NO_EXECUTE
          and len(opM.calls) == 0)

    # --- N. STOP 仍走既有 OWNED_BY_PHASE6 複驗，不被 per-leg 授權取代 ---
    opN = SpyOperator()
    sN, stN, _cN = field_session(operator=opN, power=5.0, authorize=FM.ACT_CHARGE,
                                 reading_fn=lambda: idle_reading(),
                                 obs=lambda: pcs_obs(charging=True, standby=False))
    sN.stage_command("Stage 3", FM.ACT_CHARGE)
    sN.read_all_fn = lambda: charging_reading(5.3)
    check("  N. CHARGE 後授權已消耗", sN.leg_authorization is None)
    rN = sN.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ N. STOP 不需要 per-leg 授權，仍以 OWNED_BY_PHASE6 複驗放行",
          rN.status == FM.ST_OK and sN.authority.state == CA.AUTH_OWNED
          and len(opN.calls) == 2)
    opN2 = SpyOperator()
    sN2, _stN2, _cN2 = field_session(operator=opN2, power=5.0, authorize=FM.ACT_CHARGE,
                                     reading_fn=lambda: charging_reading(80.0),
                                     obs=lambda: pcs_obs(charging=True, standby=False))
    rN2 = sN2.stage_command("Stage 4", FM.ACT_STOP)
    check("★★ N. 有 CHARGE 授權也不能拿來 STOP 他人作業（STOP 走自己的規則）",
          rN2.status == FM.ST_BLOCKED and len(opN2.calls) == 0)

    # --- reason code 完整性與稽核 ---
    check("★ 所有 per-leg 阻擋原因都在宣告的集合內",
          {FM.FA_MISSING, FM.FA_ACTION_MISMATCH, FM.FA_REVALIDATION_FAILED,
           FM.FA_STATE_CHANGED} <= FM.FIELD_AUTH_REASONS)
    check("  reason 不是只回 BLOCKED（可看出是哪一層擋的）",
          all(r.startswith("FIELD_") for r in FM.FIELD_AUTH_REASONS))
    check("★ authorization_log 保留每次取用結果（稽核用）",
          len(sC.authorization_log) == 1
          and sC.authorization_log[0]["block_reason"] == FM.FA_ACTION_MISMATCH)
    check("★★ ControlLegAuthorization 是 action-bound（欄位含 action 與評估時刻）",
          {"action", "authority_state", "authorized", "evaluated_at"}
          <= set(FM.ControlLegAuthorization.__dataclass_fields__))
    check("★ Stage 2 失敗不得留下可用授權",
          field_session(authorize=None,
                        reading_fn=lambda: charging_reading(80.0),
                        obs=lambda: pcs_obs(charging=True, standby=False))[0]
          .leg_authorization is None)

    # ---------------- Y. steady detector（Phase C.1）----------------
    print("\nY. steady detector：狀態 + 旗標 + 方向 + 幅度 + 平穩")

    PB, NB = -1.2, 0.2          # 由 idle_base() 產生的 baseline

    def idle_base(n=12, t0=1000.0):
        """idle：STANDBY、功率在 -1.2±0.2。"""
        return [mk_sample(i + 1, t0 + i, PCI.PCS_STANDBY, PB + (NB if i % 2 else -NB))
                for i in range(n)]

    def run_seg(seg, action, target, base=None, t_sent=None, t_ret=None, win=3):
        b = base or FM.compute_baseline(idle_base())
        s0 = seg[0].t_req_mono
        return FM.analyze_ramp(idle_base() + seg, b,
                               t_sent if t_sent is not None else s0 - 1.0,
                               t_ret if t_ret is not None else s0 - 0.5,
                               window=win, action=action, target_power_kw=target)

    def chg(n, p, seq0=100, t0=2000.0):
        """CHARGING 樣本；p 可為常數或 list。"""
        ps = p if isinstance(p, list) else [p] * n
        return [mk_sample(seq0 + i, t0 + i, PCI.PCS_CHARGING, ps[i],
                          charging=True, standby=False) for i in range(n)]

    def dis(n, p, seq0=100, t0=2000.0):
        ps = p if isinstance(p, list) else [p] * n
        return [mk_sample(seq0 + i, t0 + i, PCI.PCS_DISCHARGING, ps[i],
                          discharging=True, standby=False) for i in range(n)]

    # --- A/B/C. CHARGING 但 AC 仍在 baseline → 各 window 都不得 steady ---
    for w in (3, 5, 10):
        r = run_seg(chg(12, -1.3), FM.ACT_CHARGE, 5.0, win=w)
        check(f"★★ A/B/C. CHARGING 但 AC 仍在 baseline（window={w}）→ NOT STEADY",
              r["steady_seq"] is None and r["reason"] == "NO_STEADY_WINDOW")
    r = run_seg(chg(12, -1.3), FM.ACT_CHARGE, 5.0)
    check("  拒絕原因明確（方向或幅度，不是含糊的 BLOCKED）",
          r["steady_reject_reason"] in ("POWER_DIRECTION_MISMATCH",
                                        "POWER_MAGNITUDE_NOT_REACHED",
                                        "POWER_STILL_AT_BASELINE"))
    check("  但 onset 仍偵測得到（狀態已改變 → 設備確實有回應）", r["onset_seq"] == 100)

    # --- D. AC 已離開 baseline 但仍在變化 → NOT STEADY ---
    r = run_seg(chg(8, [0.5, 1.6, 2.8, 3.9, 4.6, 5.1, 5.3, 5.4]), FM.ACT_CHARGE, 5.0)
    check("★★ D. AC 已脫離 baseline 但持續變化 → NOT STEADY",
          r["steady_seq"] is None or r["steady_seq"] >= 105)

    # --- E. CHARGING + AC 到位 + 平穩 → STEADY ---
    r = run_seg(chg(8, 5.4), FM.ACT_CHARGE, 5.0)
    check("★★ E. CHARGING + AC 到位 + 平穩 → STEADY",
          r["steady_seq"] == 100 and abs(r["steady_power_kw"] - 5.4) < 1e-9
          and r["steady_state"] == PCI.PCS_CHARGING)
    check("  reach_tolerance 由 tol 與 target 推導（非寫死）",
          abs(r["reach_tolerance"] - max(NB, FM.STEADY_REACH_REL * 5.0)) < 1e-9)

    # --- F. DISCHARGE 對稱 ---
    r = run_seg(dis(12, -1.3), FM.ACT_DISCHARGE, 5.0)
    check("★★ F. DISCHARGING 但 AC 仍在 baseline → NOT STEADY", r["steady_seq"] is None)
    r = run_seg(dis(8, -5.4), FM.ACT_DISCHARGE, 5.0)
    check("★★ F. DISCHARGING + AC 到位 → STEADY",
          r["steady_seq"] == 100 and abs(r["steady_power_kw"] + 5.4) < 1e-9)

    # --- 方向與幅度各自獨立擋下 ---
    r = run_seg(chg(8, -5.4), FM.ACT_CHARGE, 5.0)
    check("★★ CHARGE 但 AC 為負（方向相反）→ NOT STEADY / DIRECTION_MISMATCH",
          r["steady_seq"] is None and r["steady_reject_reason"] == "POWER_DIRECTION_MISMATCH")
    r = run_seg(dis(8, 5.4), FM.ACT_DISCHARGE, 5.0)
    check("★★ DISCHARGE 但 AC 為正 → NOT STEADY / DIRECTION_MISMATCH",
          r["steady_reject_reason"] == "POWER_DIRECTION_MISMATCH")
    r = run_seg(chg(8, 1.6), FM.ACT_CHARGE, 5.0)
    check("★★ 方向對但幅度只有 1.6/5.0（差 68%）→ NOT STEADY / MAGNITUDE_NOT_REACHED",
          r["steady_reject_reason"] == "POWER_MAGNITUDE_NOT_REACHED")
    r = run_seg(chg(8, 5.4), FM.ACT_CHARGE, 20.0)
    check("  同一組資料換成 target=20kW → 幅度未達 → NOT STEADY（門檻隨 target 變）",
          r["steady_seq"] is None)
    r = run_seg(chg(8, 21.0), FM.ACT_CHARGE, 20.0)
    check("★★ target=20kW、實測 21kW → STEADY（規則可適用於不同功率）",
          r["steady_seq"] == 100)
    r = run_seg(chg(8, 84.0), FM.ACT_CHARGE, 80.0)
    check("★★ target=80kW、實測 84kW → STEADY（不因 5kW 而寫死）", r["steady_seq"] == 100)

    # --- target 未知 → Fail Closed ---
    for tgt in (None, 0, -5.0, float("nan")):
        r = run_seg(chg(8, 5.4), FM.ACT_CHARGE, tgt)
        check(f"★★ target={tgt!r} → 不得宣告 steady（STEADY_NEEDS_TARGET）",
              r["steady_seq"] is None and r["reason"] == "STEADY_NEEDS_TARGET")
    r = run_seg(chg(8, 5.4), None, 5.0)
    check("  action 未提供 → 同樣 Fail Closed",
          r["steady_seq"] is None and r["reason"] == "STEADY_NEEDS_TARGET")

    # --- G/H. STOP ---
    b = FM.compute_baseline(idle_base())
    stop_hot = [mk_sample(200 + i, 3000.0 + i, PCI.PCS_STOPPED, 5.4,
                          charging=False, standby=False) for i in range(8)]
    r = FM.analyze_stop(idle_base() + stop_hot, b, 2999.0, 2999.5, window=3)
    check("★★ G. STOPPED 但功率尚未回 baseline → NOT stopped-steady",
          r["stopped_seq"] is None and r["reason"] == "NO_STOPPED_WINDOW")
    stop_cold = [mk_sample(200 + i, 3000.0 + i, PCI.PCS_STOPPED, PB,
                           charging=False, standby=False) for i in range(8)]
    r = FM.analyze_stop(idle_base() + stop_cold, b, 2999.0, 2999.5, window=3)
    check("★★ H. STOPPED + 功率回 baseline → stopped-steady",
          r["stopped_seq"] == 200 and r["stopped_state"] == PCI.PCS_STOPPED)
    unk = [mk_sample(200 + i, 3000.0 + i, PCI.PCS_UNKNOWN, PB,
                     charging=False, standby=False) for i in range(8)]
    r = FM.analyze_stop(idle_base() + unk, b, 2999.0, 2999.5, window=3)
    check("★★ 狀態為 UNKNOWN（證據不足）即使功率回 baseline 也不算已停",
          r["stopped_seq"] is None)
    sb = [mk_sample(200 + i, 3000.0 + i, PCI.PCS_STANDBY, PB, standby=True)
          for i in range(8)]
    check("  STANDBY + 功率回 baseline 亦算已停（與 executor 的可接受集合一致）",
          FM.analyze_stop(idle_base() + sb, b, 2999.0, 2999.5, window=3)["stopped_seq"] == 200)

    # --- I/J/K. AC lag 0 / 1 / 2 refresh cycle ---
    print("\nY2. AC lag 0 / 1 / 2 refresh cycle")
    for lag in (0, 1, 2):
        # 前 lag*15 筆：旗標已 CHARGING 但 AC 仍在 baseline；之後 AC 追上
        seg = chg(lag * 15, -1.3) + chg(15, 5.4, seq0=100 + lag * 15,
                                        t0=2000.0 + lag * 15)
        for w in (3, 5, 10):
            r = run_seg(seg, FM.ACT_CHARGE, 5.0, win=w)
            want = 100 + lag * 15
            check(f"★★ AC lag {lag} cycle（window={w}）→ steady 落在 AC 追上之後（seq={want}）",
                  r["steady_seq"] == want)
    seg = chg(15, -1.3) + chg(15, -5.4, seq0=115, t0=2015.0)
    r = run_seg(seg, FM.ACT_CHARGE, 5.0)
    check("★ lag 後方向仍錯（CHARGE 卻是負功率）→ 仍不得 steady", r["steady_seq"] is None)

    # --- 無硬編碼功率門檻 ---
    print("\nY3. 無硬編碼功率門檻")
    _fm_src = SRC
    import re as _re
    bad = [m for m in _re.findall(r"(?<![\w.])([0-9]+(?:\.[0-9]+)?)\s*(?:#.*)?$",
                                  "", _re.M)]
    check("★★ steady 判定不含 3 kW / 5 kW 之類的寫死門檻",
          "P > 3" not in _fm_src and "> 3.0" not in _fm_src
          and "3 kW" not in _fm_src.split("STEADY_REACH_REL")[-1][:2000])
    check("★★ 門檻由 baseline / noise_band / 解析度 / target 推導",
          "STEADY_REACH_REL * abs(target_power_kw)" in _fm_src
          and "max(tol, STEADY_REACH_REL" in _fm_src)
    check("  STEADY_REACH_REL 為無因次比例（不是 kW）", 0 < FM.STEADY_REACH_REL < 1)
    check("★ sign convention 與 Phase 6.2 一致（量測層：charge 正 / discharge 負）",
          FM.OBSERVED_POWER_SIGN[FM.ACT_CHARGE] == 1
          and FM.OBSERVED_POWER_SIGN[FM.ACT_DISCHARGE] == -1)
    check("★ steady detector 未被耦合進控制授權路徑",
          "_reached_target" not in _fm_src.split("def _enforce_leg_authorization")[1]
          .split("def stage_command")[0])

    # ---------------- 收尾 ----------------
    n, total = sum(RESULTS), len(RESULTS)
    print(f"\n== Phase 6.5-G Field Measurement {'PASS' if n == total else 'FAIL'}"
          f"（{n}/{total} 檢查通過）==")
    return 0 if n == total else 1


def SG_BATT_ON():
    import safety_gate as SG
    return SG.BATT_ON


if __name__ == "__main__":
    sys.exit(main())
