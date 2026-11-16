# -*- coding: utf-8 -*-
"""
test_cert_o1.py — M-S2 r2c O-1 離線測試：認證專用讀取 read_cert（5 GET）
==========================================================================================
全部離線：假 ApiClient（只記錄呼叫、回傳合成回應）；不連任何設備、不送任何控制。

  E-1 read_all 原行為等價：與修改前版本（git HEAD 的 charge_discharge_report.py）逐案比對
      回傳內容（含 _fail 順序）、請求順序／次數（9 次）／參數 —— 固定案例＋每端點單一故障＋隨機組合
  E-2 read_cert 欄位等價：5 次 GET（順序與參數）；認證使用的全部欄位＝read_all；_fail＝read_all 中這 5 個端點的條目；
      不讀的 4 個端點沒有對應鍵；build_record（數值／缺值／ok／hmi_ok／旗標）相同
  E-3 401／None／例外／部分失敗語意（逐一明確驗證）；格點分類只依實際讀取的端點
  E-4 _CLIENT_LOCK：範圍不擴大（一次讀取取鎖一次、讀取期間持鎖、讀完釋放；取鎖處數量與修改前相同）
  E-5 cert_state 讀取模式：新認證記錄 read_cert；舊 Session（無欄位）續接 → 記錄變更一次；測試 runtime 不記錄
  E-6 cert_timing 逐端點耗時：5 筆、名稱固定；每筆取樣 HMI 請求數＝5（不增加請求）
注意：本檔為 A 類（離線）證據；實機讀取時間改善屬 C 類，需實機驗證。
"""
import contextlib
import importlib.util
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                       # noqa: BLE001
    pass

import charge_discharge_report as CDR                   # noqa: E402
import cert_requests as REQ                             # noqa: E402
import cert_runtime as CR                               # noqa: E402
import report_quality as RQ                             # noqa: E402

RESULTS = []
TMP = tempfile.mkdtemp(prefix="cert_o1_")


def safe_detail(detail):
    """說明欄中的 [PASS]／[FAIL]（例：被測程式的原始輸出）改為全形 ［PASS］／［FAIL］→ 回歸工具的計數不受影響。"""
    return str(detail).replace("[PASS]", "［PASS］").replace("[FAIL]", "［FAIL］")


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"（{safe_detail(detail)}）" if detail else ""))


def guard(label, fn):
    try:
        fn()
    except Exception as e:                              # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{label}：執行例外 {type(e).__name__}: {str(e)[:160]}", False)


# ---------------------------------------------------------------------------------------- 修改前版本（基準）
def load_orig():
    """git HEAD 的 charge_discharge_report.py（＝正式版 99e2046，r2b 未修改此檔）→ 載入為獨立模組。"""
    src = subprocess.run(["git", "show", "HEAD:test/charge_discharge_report.py"], cwd=HERE, capture_output=True)
    if src.returncode != 0:
        raise RuntimeError("無法取得 HEAD 版本（git show 失敗）")
    p = os.path.join(TMP, "cdr_orig_head.py")
    open(p, "wb").write(src.stdout)
    spec = importlib.util.spec_from_file_location("cdr_orig_head", p)
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return m


ORIG = load_orig()
LANG = CDR._PCS_LANG_HEADER


# ---------------------------------------------------------------------------------------- 假回應
def pcs_list(zh, flags, status_text, power=-114.2):
    ms = [{"mark": "totalActivePowerOfAcBus", "value": power, "unit": "kW", "oldValue": power},
          {"mark": "totalReactivePowerOfAcBus", "value": 0.3, "unit": "kVar", "oldValue": 0.3},
          {"mark": "dcInputVoltage", "value": 951.0, "oldValue": 951.0},
          {"mark": "dcCurrent", "value": -120.0, "oldValue": -120.0},
          {"mark": "dcPower", "value": -114.0, "oldValue": -114.0},
          {"mark": "dailyChargedEnergyThroughAcPort", "value": 12.5, "oldValue": 12.5},
          {"mark": "dailyDischargedEnergyThroughAcPort", "value": 30.1, "oldValue": 30.1},
          {"mark": "gridTiedOrOffGrid", "value": "gridTied", "oldValue": "1"}]
    for mk, ov in flags.items():
        ms.append({"mark": mk, "value": (("運行" if mk == "systemOnOrOffStatus" else "是") if zh else "on"),
                   "oldValue": ov})
    if zh and status_text is not None:
        ms.append({"mark": "systemOnOrOffStatus", "value": status_text, "oldValue": "1"})
    return [{"deviceName": "PCS1", "metricsDataVoList": ms}]


def good_responses(rnd=None):
    rnd = rnd or random.Random(0)
    fl = {"systemChargingStatus": rnd.choice(["0", "1", 0, 1, None]),
          "systemDischargingStatus": rnd.choice(["0", "1", 0, 1, "x"]),
          "systemFaultStatus": rnd.choice(["0", "1"]), "systemOnOrOffStatus": rnd.choice(["0", "1"]),
          "systemStandbyStatus": rnd.choice(["0", "1", None])}
    fl = {k: v for k, v in fl.items() if v is not None or rnd.random() < 0.5}
    cur = rnd.choice([-140.1, 0.0, 139.9, "12.5", None])
    return {
        CDR.EP_MAINCTL: {"rackSoc": rnd.choice([50, "61.5", None]), "rackTotalBatteryVoltage": rnd.choice([952.3, 940.0]),
                         "rackElectricCurrent": cur, "createTime": "2026-11-13 09:00:00"},
        CDR.EP_RACKEXT: {"rackCellMaxTemperature": "33", "rackCellMinTemperature": rnd.choice(["28", None]),
                         "rackCellMaxVoltage": "3.4015", "rackCellMinVoltage": 3.385},
        CDR.EP_RACKCAP: {"accumulatedDailyChargingEnergy": "10.2", "accumulatedDailyDisChargingEnergy": 20.4},
        (CDR.EP_PCS, "en"): pcs_list(False, fl, None, power=rnd.choice([-114.2, 0.0, 113.8])),
        (CDR.EP_PCS, "zh"): pcs_list(True, fl, rnd.choice(["運行 / 放電", "待機", None])),
        CDR.EP_RUNMODE: rnd.choice(["smart", "manual", ""]),
        CDR.EP_SCHEDULE: {"schedulePlanSwitch": rnd.choice([0, 1]), "manualModeSwitch": rnd.choice([0, 1])},
        CDR.EP_DODI: [{"name": "main.positive.contactor.feedback", "value": rnd.choice([1, 0, "1"])},
                      {"name": "main.negative.contactor.feedback", "value": rnd.choice([1, 0])}],
        CDR.EP_ALARM: {"rows": [{"id": 1, "typeMark": "電池"}], "total": rnd.choice([1, None])},
    }


FAULTS = ("ok", "none", "exc", "err401", "err500", "badtype")


class FakeClient:
    """假 ApiClient：依 (path, 語言) 回傳；faults＝{key: 故障種類}。只記錄呼叫（path, kwargs）。"""
    def __init__(self, resp, faults=None):
        self.resp, self.faults, self.calls = resp, faults or {}, []

    def get(self, path, **kw):
        self.calls.append((path, json.dumps(kw, sort_keys=True, ensure_ascii=False)))
        key = (path, "zh" if kw.get("headers") == LANG else "en") if path == CDR.EP_PCS else path
        f = self.faults.get(key, "ok")
        if f == "none":
            return None
        if f == "exc":
            raise TimeoutError("模擬逾時")
        if f == "err401":
            return {"_error": 401, "msg": "unauthorized", "_raw": "x"}
        if f == "err500":
            return {"_error": 500, "msg": "server", "_raw": "y"}
        if f == "badtype":
            return "非預期型別"
        return json.loads(json.dumps(self.resp[key]))


KEYS9 = (CDR.EP_MAINCTL, CDR.EP_RACKEXT, CDR.EP_RACKCAP, (CDR.EP_PCS, "en"), (CDR.EP_PCS, "zh"), CDR.EP_RUNMODE,
         CDR.EP_SCHEDULE, CDR.EP_DODI, CDR.EP_ALARM)
CERT_EPS = (CDR.EP_MAINCTL, CDR.EP_RACKEXT, CDR.EP_PCS, CDR.EP_DODI)
DROPPED_KEYS = ("device_daily_charge_kwh", "device_daily_discharge_kwh", "pcs_control_mode", "pcs_control_mode_code",
                "pcs_work_mode", "pcs_power_control_mode", "pcs_schedule_enabled", "pcs_manual_switch",
                "alarm_rows", "alarm_total", "alarm_total_raw")
CERT_FIELDS = tuple(CR.FIELD_SRC.values()) + (
    "pcs_charging_flag", "pcs_discharging_flag", "pcs_fault_flag", "pcs_running_flag", "pcs_standby_flag",
    "pcs_status", "battery_power_status", "battery_status", "communication_ok", "raw_source_time",
    "actual_reactive_power_kvar", "dc_voltage_v", "dc_current_a", "dc_power_kw", "daily_charged_kwh",
    "daily_discharged_kwh", "calculated_power_kw")


def ep_of(entry):
    return str(entry).split(":", 1)[0]


class Raised(dict):
    """讀取函式丟出例外的結果（既有行為：例如電流為字串時 battery_flow_state 相乘失敗 → 修改前即會丟出）。"""


def run(fn, resp, faults):
    cli = FakeClient(resp, faults)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            out = fn(cli)
    except Exception as e:                              # noqa: BLE001
        out = Raised(_raised=type(e).__name__, _fail=[])
    return out, cli.calls


def scenarios():
    """固定案例＋每端點單一故障（9 端點×5 故障）＋多端點組合＋隨機 1,500 組。"""
    out = [("全部成功", good_responses(), {})]
    for k in KEYS9:
        for f in FAULTS[1:]:
            out.append((f"{k}={f}", good_responses(), {k: f}))
    out.append(("authed 全 401", good_responses(), {CDR.EP_RUNMODE: "err401", CDR.EP_SCHEDULE: "err401",
                                                   CDR.EP_DODI: "err401"}))
    out.append(("全部 None", good_responses(), {k: "none" for k in KEYS9}))
    out.append(("全部例外", good_responses(), {k: "exc" for k in KEYS9}))
    rnd = random.Random(20261113)
    for i in range(1500):
        fz = {k: rnd.choice(FAULTS) for k in KEYS9 if rnd.random() < 0.3}
        out.append((f"隨機#{i}", good_responses(rnd), fz))
    return out


SCEN = scenarios()


# =============================================================================================== E-1
def t_e1():
    print(f"\n== E-1 read_all 原行為等價（修改前 HEAD vs 修改後；{len(SCEN)} 案例）==")
    bad_ret, bad_calls, n9, n_exc = [], [], 0, 0
    for name, resp, fz in SCEN:
        a, ca = run(ORIG.read_all, resp, fz)
        b, cb = run(CDR.read_all, resp, fz)
        if json.dumps(a, sort_keys=True, ensure_ascii=False, default=str) != \
                json.dumps(b, sort_keys=True, ensure_ascii=False, default=str) or list(a) != list(b) \
                or type(a) is not type(b):
            bad_ret.append(name)
        if ca != cb:
            bad_calls.append(name)
        n_exc += isinstance(a, Raised)
        n9 += len(cb) == 9 or isinstance(b, Raised)
    check(f"回傳內容逐案相同（含鍵順序、_fail 順序與格式；其中 {n_exc} 案兩版皆以同型別例外中止＝既有行為）",
          not bad_ret, "；".join(bad_ret[:5]))
    check("請求順序、參數逐案相同", not bad_calls, "；".join(bad_calls[:5]))
    check("每次固定 9 次 GET，順序＝READ_ALL_ENDPOINTS", n9 == len(SCEN) and
          [p for p, _ in run(CDR.read_all, good_responses(), {})[1]] == list(CDR.READ_ALL_ENDPOINTS))


# =============================================================================================== E-2
def t_e2():
    print(f"\n== E-2 read_cert 欄位等價（{len(SCEN)} 案例）==")
    bad_calls, bad_fields, bad_fail, bad_keys, bad_rec, bad_cls = [], [], [], [], [], []
    n_exc = 0
    for name, resp, fz in SCEN:
        a, ca = run(CDR.read_all, resp, fz)
        c, cc = run(CDR.read_cert, resp, fz)
        if isinstance(a, Raised) or isinstance(c, Raised):  # 兩者須同樣中止（同型別；例外發生在共用的主資訊解析）
            n_exc += 1
            if not (isinstance(a, Raised) and isinstance(c, Raised) and a == c):
                bad_fields.append(f"{name}:例外不一致")
            continue
        want_calls = [x for x in ca if x[0] in CERT_EPS]
        if cc != want_calls or len(cc) != 5:
            bad_calls.append(name)
        diff = [k for k in CERT_FIELDS if json.dumps(a.get(k), default=str) != json.dumps(c.get(k), default=str)
                or (k in a) != (k in c)]
        if diff:
            bad_fields.append(f"{name}:{diff[:3]}")
        if c["_fail"] != [x for x in a["_fail"] if ep_of(x) in CERT_EPS]:
            bad_fail.append(name)
        if any(k in c for k in DROPPED_KEYS) or set(c) | set(DROPPED_KEYS) != set(a):
            bad_keys.append(name)
        t = datetime(2026, 11, 13, 9, 0, 0)
        ra, rc = CR.build_record(t, a, "", None, ""), CR.build_record(t, c, "", None, "")
        if any(ra[k] != rc[k] for k in ("values", "missing", "ok", "hmi_ok", "flags")):
            bad_rec.append(name)
        filt = dict(a, _fail=[x for x in a["_fail"] if ep_of(x) in CERT_EPS])
        rf = CR.build_record(t, filt, "", None, "")
        if (rc["notes"], rc["fail_reason"]) != (rf["notes"], rf["fail_reason"]) or \
                RQ.classify_cert_attempt(rc, "", c) != RQ.classify_cert_attempt(rf, "", filt):
            bad_cls.append(name)
    check("每次固定 5 次 GET，順序與參數＝read_all 中對應的請求（PCS zh-TW 標頭相同）",
          not bad_calls and [p for p, _ in run(CDR.read_cert, good_responses(), {})[1]] == list(CDR.READ_CERT_ENDPOINTS),
          "；".join(bad_calls[:5]))
    check(f"認證使用的 {len(CERT_FIELDS)} 個欄位逐案與 read_all 相同（8 數值、PCS 旗標×5、pcs_status、"
          "battery_power_status、communication_ok…）", not bad_fields, "；".join(bad_fields[:3]))
    check("_fail＝read_all 中這 5 個端點的條目（格式、順序相同）", not bad_fail, "；".join(bad_fail[:5]))
    check(f"不讀的 4 個端點沒有對應鍵（{len(DROPPED_KEYS)} 個），其餘鍵與 read_all 相同", not bad_keys,
          "；".join(bad_keys[:5]))
    check("build_record：數值、缺值、ok、hmi_ok、旗標逐案相同", not bad_rec, "；".join(bad_rec[:5]))
    check("說明／失敗原因與格點分類＝「只看實際讀取端點」的 read_all 結果", not bad_cls, "；".join(bad_cls[:5]))


# =============================================================================================== E-3
def t_e3():
    print("\n== E-3 401／None／例外／部分失敗語意 ==")
    t = datetime(2026, 11, 13, 9, 0, 0)
    resp = good_responses()

    def cert(fz):
        c, calls = run(CDR.read_cert, resp, fz)
        rec = CR.build_record(t, c, "", None, "")
        return c, rec, RQ.classify_cert_attempt(rec, "", c), calls

    c, rec, cls, calls = cert({CDR.EP_RUNMODE: "err401", CDR.EP_SCHEDULE: "err401", CDR.EP_DODI: "err401"})
    check("authed 全 401（實機 1 Cycle 型態）：_fail 只有 DODI:_error401、上下電未知、通訊正常、數值有效 → PARTIAL（與 read_all 相同）",
          c["_fail"] == [CDR.EP_DODI + ":_error401"] and c["battery_power_status"] == "未知"
          and c["communication_ok"] is True and rec["hmi_ok"] and cls == RQ.ATTEMPTED_PARTIAL, str(c["_fail"]))
    c, rec, cls, _ = cert({CDR.EP_RUNMODE: "err401"})
    a, _ = run(CDR.read_all, resp, {CDR.EP_RUNMODE: "err401"})
    ra = CR.build_record(t, a, "", None, "")
    check("只有 RunMode 401（認證不讀）：read_cert 無失敗 → OK；read_all 為 PARTIAL（格點分類只依實際讀取的端點；"
          "此差異為預期）", c["_fail"] == [] and cls == RQ.ATTEMPTED_OK
          and RQ.classify_cert_attempt(ra, "", a) == RQ.ATTEMPTED_PARTIAL)
    c, rec, cls, _ = cert({CDR.EP_DODI: "none"})
    check("DODI 回 None：_fail＝[DODI]、上下電未知 → PARTIAL", c["_fail"] == [CDR.EP_DODI]
          and c["battery_power_status"] == "未知" and cls == RQ.ATTEMPTED_PARTIAL)
    c, rec, cls, _ = cert({CDR.EP_MAINCTL: "exc"})
    check("主資訊例外：不往外丟；_fail 記「端點:例外型別」＋「端點」（與 read_all 相同）、通訊異常、SOC／電壓／電流缺值",
          c["_fail"] == [CDR.EP_MAINCTL + ":TimeoutError", CDR.EP_MAINCTL] and c["communication_ok"] is False
          and c["soc_percent"] is None and c["battery_current_a"] is None)
    c, rec, cls, _ = cert({(CDR.EP_PCS, "en"): "none"})
    check("PCS（英文）None：通訊異常、有功功率與旗標缺值", c["communication_ok"] is False
          and c["actual_active_power_kw"] is None and c["pcs_charging_flag"] is None)
    c, rec, cls, _ = cert({(CDR.EP_PCS, "zh"): "err500"})
    check("PCS（zh-TW）_error500：只影響 pcs_status（未知）；_fail 記錄 → PARTIAL",
          c["pcs_status"] == "未知" and c["_fail"] == [CDR.EP_PCS + ":_error500"] and cls == RQ.ATTEMPTED_PARTIAL)
    c, rec, cls, calls = cert({k: "exc" for k in KEYS9})
    check("全部例外：不往外丟、仍是 5 次 GET、所有量測缺值 → ok=False → FAIL",
          len(calls) == 5 and not rec["ok"] and cls == RQ.ATTEMPTED_FAIL)
    c, rec, cls, _ = cert({CDR.EP_RACKEXT: "badtype"})
    check("rackExtreme 非預期型別：溫度／Cell 電壓缺值、不丟例外、不記 _fail（與 read_all 相同）",
          c["rack_max_temperature_c"] is None and c["rack_cell_max_voltage_v"] is None and c["_fail"] == [])


# =============================================================================================== E-4
def t_e4():
    print("\n== E-4 _CLIENT_LOCK 使用範圍 ==")
    import report_monitor as RM
    import cert_service as CS

    class CountLock:
        def __init__(self, inner):
            self.inner, self.n, self.depth = inner, 0, 0

        def __enter__(self):
            self.inner.acquire()
            self.n += 1
            self.depth += 1
            return self

        def __exit__(self, *exc):
            self.depth -= 1
            self.inner.release()

        def acquire(self, *a, **k):
            return self.inner.acquire(*a, **k)

        def release(self):
            return self.inner.release()

    held = []
    cli = FakeClient(good_responses())
    orig_get = cli.get

    def get(path, **kw):
        held.append(lk.depth)
        return orig_get(path, **kw)
    cli.get = get
    bak_lock, bak_cli = RM._CLIENT_LOCK, RM._report.get("client")
    lk = CountLock(bak_lock)
    try:
        RM._CLIENT_LOCK = lk
        RM._report["client"] = cli
        with contextlib.redirect_stdout(io.StringIO()):
            r = CS._reader()
        tm = dict(CS._READ_TIMING)
    finally:
        RM._CLIENT_LOCK, RM._report["client"] = bak_lock, bak_cli
    check("一次取樣只取鎖一次；5 次 GET 全部在鎖內；讀完已釋放", lk.n == 1 and held == [1] * 5 and lk.depth == 0,
          f"取鎖 {lk.n}、GET 時持鎖 {held}")
    check("服務 reader 使用 read_cert（5 GET），回傳讀值", isinstance(r, dict) and len(cli.calls) == 5
          and "pcs_control_mode" not in r and CS.HMI_READER == "read_cert")
    head = subprocess.run(["git", "show", "HEAD:test/cert_service.py"], cwd=HERE, capture_output=True).stdout.decode("utf-8")
    now = open(os.path.join(HERE, "cert_service.py"), encoding="utf-8").read()
    check("cert_service 取 _CLIENT_LOCK 的位置數與修改前相同（reader＋起始環控各 1）",
          head.count("with RM._CLIENT_LOCK") == now.count("with RM._CLIENT_LOCK") == 2)
    check("計時：等鎖、讀取毫秒＋逐端點 5 筆（main／rack_ext／pcs／pcs_zh／dodi）",
          set(tm) == {"lock_wait_ms", "read_ms", "ep_ms"}
          and [x[0] for x in tm["ep_ms"]] == ["main", "rack_ext", "pcs", "pcs_zh", "dodi"]
          and all(isinstance(x[1], float) for x in tm["ep_ms"]), str(tm.get("ep_ms")))


# =============================================================================================== E-5／E-6
def t_e56():
    print("\n== E-5 cert_state 讀取模式／E-6 cert_timing 逐端點耗時 ==")
    import report_monitor as RM
    import cert_service as CS
    cli = FakeClient(good_responses())
    bak_cli = RM._report.get("client")
    RM._report["client"] = cli
    try:
        root = os.path.join(TMP, "e5")
        rt = CR.CertRuntime(root, reader=CS._reader, heat_reader=None, enable_shots=False, log=lambda *a: None,
                            read_timing=lambda: dict(CS._READ_TIMING), hmi_reader=CS.HMI_READER)
        REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=datetime.now())
        rt.process_requests()
        cid = rt.active_id()
        st = REQ.read_json(os.path.join(root, cid, "cert_state.json"))
        check("新認證：cert_state.hmi_reader＝read_cert", st.get("hmi_reader") == "read_cert")
        n0 = len(cli.calls)
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3):
                with rt._sample_lock:
                    rt._sample_cycle("schedule")
        tim = [json.loads(x) for x in open(os.path.join(root, cid, "cert_timing.jsonl"), encoding="utf-8") if x.strip()]
        eps = [t_["reader"].get("ep_ms") for t_ in tim]
        check(f"3 筆取樣共 {len(cli.calls) - n0} 次 HMI 請求（每筆 5 次，不增加請求）", len(cli.calls) - n0 == 15)
        check("cert_timing.jsonl 每筆都有 5 個端點耗時", len(eps) == 3 and all(e and len(e) == 5 for e in eps), str(eps[:1]))
        # 續接：舊 Session（沒有 hmi_reader 欄位）→ 記錄一次變更；再續接不重複
        st = REQ.read_json(os.path.join(root, cid, "cert_state.json"))
        st.pop("hmi_reader", None)
        json.dump(st, open(os.path.join(root, cid, "cert_state.json"), "w", encoding="utf-8"), ensure_ascii=False)
        rt2 = CR.CertRuntime(root, reader=CS._reader, enable_shots=False, log=lambda *a: None, hmi_reader="read_cert")
        rt2.startup()
        st2 = REQ.read_json(os.path.join(root, cid, "cert_state.json"))
        ch = st2.get("hmi_reader_changes") or []
        check("舊 Session 續接：hmi_reader_changes 記錄 read_all → read_cert（from_recorded=False）一次",
              st2.get("hmi_reader") == "read_cert" and len(ch) == 1 and ch[0]["from"] == "read_all"
              and ch[0]["to"] == "read_cert" and ch[0]["from_recorded"] is False, str(ch))
        rt3 = CR.CertRuntime(root, reader=CS._reader, enable_shots=False, log=lambda *a: None, hmi_reader="read_cert")
        rt3.startup()
        st3 = REQ.read_json(os.path.join(root, cid, "cert_state.json"))
        check("同模式再續接：不重複記錄", len(st3.get("hmi_reader_changes") or []) == 1)
        ev = [json.loads(x) for x in open(os.path.join(root, cid, "events.jsonl"), encoding="utf-8") if x.strip()]
        check("事件紀錄 hmi_reader 一筆", sum(1 for e in ev if e["type"] == "hmi_reader") == 1)
        # 測試／模擬 runtime（hmi_reader=None）不記錄
        root2 = os.path.join(TMP, "e5b")
        rt4 = CR.CertRuntime(root2, reader=lambda: None, enable_shots=False, log=lambda *a: None)
        REQ.new_request(root2, "start_cert", {"cabinet_spec": "250KW"}, now=datetime.now())
        rt4.process_requests()
        st4 = REQ.read_json(os.path.join(root2, rt4.active_id(), "cert_state.json"))
        check("hmi_reader=None（測試／模擬）：不寫此欄位（舊行為）", "hmi_reader" not in st4)
        # 結束與報告照常（含 hmi_reader 欄位的 Session）
        REQ.new_request(root, "end_cert", cert_id=cid, now=datetime.now())
        with contextlib.redirect_stdout(io.StringIO()):
            rt3.process_requests()
        reps = [x for x in os.listdir(os.path.join(root, cid, "reports")) if x.endswith(".xlsx")]
        check("含 hmi_reader 的 Session：Menu 22 結束與報告產生正常", len(reps) == 1, str(reps))
    finally:
        RM._report["client"] = bak_cli


# =============================================================================================== E-7／E-8
def report_text(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    txt = [wb.properties.description or ""]
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            txt.extend(str(v) for v in row if v is not None)
    return "\n".join(txt)


def t_e7():
    print("\n== E-7 報告資料來源說明（只依 cert_state 既有 metadata）==")
    check("無 metadata（舊 Session）→ 不加註", CR.hmi_reader_note({}) is None and CR.hmi_reader_note(None) is None)
    n1 = CR.hmi_reader_note({"hmi_reader": "read_cert"})
    check("read_cert：說明 5 支 GET、規則不變、401 減少不代表登入改善",
          n1 and "read_cert" in n1 and "5 支 GET" in n1 and "格點判定規則不變" in n1 and "不代表登入狀態改善" in n1, n1)
    n2 = CR.hmi_reader_note({"hmi_reader": "read_cert", "hmi_reader_changes": [
        {"at": "2026-11-13T10:00:00", "from": "read_all", "to": "read_cert", "from_recorded": False}]})
    check("續接切換：顯示開始時模式（未記錄→推定）與切換時間", n2 and "開始時為 完整讀取 read_all" in n2
          and "未記錄，依程式版本推定" in n2 and "2026-11-13T10:00:00 起改為 認證專用讀取 read_cert" in n2, n2)
    # 報告實際內容
    root = os.path.join(TMP, "e7")
    for name, mode in (("new", "read_cert"), ("old", None)):
        r = os.path.join(root, name)
        rt = CR.CertRuntime(r, reader=lambda: None, enable_shots=False, log=lambda *a: None, hmi_reader=mode)
        REQ.new_request(r, "start_cert", {"cabinet_spec": "250KW"}, now=datetime.now())
        rt.process_requests()
        cid = rt.active_id()
        REQ.new_request(r, "end_cert", cert_id=cid, now=datetime.now())
        with contextlib.redirect_stdout(io.StringIO()):
            rt.process_requests()
        rep = [x for x in os.listdir(os.path.join(r, cid, "reports")) if x.endswith(".xlsx")][0]
        txt = report_text(os.path.join(r, cid, "reports", rep))
        if mode:
            check("新 Session 報告含「HMI 資料來源」說明", "HMI 資料來源：認證專用讀取 read_cert" in txt)
        else:
            check("無讀取模式 metadata 的 Session 報告不含此說明（舊 Session 維持原樣）", "HMI 資料來源" not in txt)


def t_e8():
    print("\n== E-8 cert_timing_report：讀取模式、切換、401 統計範圍 ==")
    from datetime import timedelta
    folder = os.path.join(TMP, "e8", "CERT_20261113_100000")
    os.makedirs(folder)
    t0 = datetime(2026, 11, 13, 10, 0, 0)
    obs, tim = [], []
    ep5 = [["main", 60.0], ["rack_ext", 50.0], ["pcs", 70.0], ["pcs_zh", 70.0], ["dodi", 40.0]]
    for i in range(40):
        t = t0 + timedelta(seconds=i)
        new = i >= 20                                   # 前 20 筆舊版（無 ep_ms＝read_all 推定）、後 20 筆 read_cert
        f401 = ([CDR.EP_RUNMODE + ":_error401", CDR.EP_SCHEDULE + ":_error401", CDR.EP_DODI + ":_error401"]
                if i in (5, 6) else []) if not new else ([CDR.EP_DODI + ":_error401"] if i in (25, 26) else [])
        reader = {"lock_wait_ms": 1.0, "read_ms": 300.0 if new else 550.0}
        if new:
            reader["ep_ms"] = ep5
        obs.append({"t": t.isoformat(timespec="seconds"), "ok": True, "hmi_ok": True, "values": {"pcs_module_c": 40}})
        tim.append({"written": True, "obs_t": t.isoformat(timespec="seconds"), "interval_ms": 1000, "cycle_ms": 600,
                    "hmi_ms": 600, "heat_ms": 100, "rt_lock_wait_ms": 0, "obs_write_ms": 1, "state_write_ms": 2,
                    "wall_start": t.isoformat(timespec="milliseconds"),
                    "wall_end": (t + timedelta(milliseconds=600)).isoformat(timespec="milliseconds"),
                    "reader": reader, "hmi_fail": f401, "trigger": "schedule"})
    for name, rows in (("observations.jsonl", obs), ("cert_timing.jsonl", tim)):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    json.dump({"cert_id": "CERT_20261113_100000", "hmi_reader": "read_cert", "hmi_reader_changes": [
        {"at": "2026-11-13T10:00:20", "from": "read_all", "to": "read_cert", "from_recorded": False}]},
        open(os.path.join(folder, "cert_state.json"), "w", encoding="utf-8"))
    p = subprocess.run([sys.executable, "-B", os.path.join(HERE, "cert_timing_report.py"), folder, "--interval", "1"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = p.stdout
    ok1 = "└ read_all             :" not in out and "HMI 讀取 read_cert" in out and "端點 dodi" in out
    # 診斷：只在失敗時附上 timing 報告節錄（rc＋讀取耗時相關行；[PASS]／[FAIL] 由 check 轉為全形，不影響回歸計數）
    diag = "" if ok1 else (f"rc={p.returncode}；" + " ｜ ".join(
        x.strip() for x in out.splitlines() if "HMI 讀取" in x or "read_all" in x or "端點" in x)[:600]
        + (f"；stderr：{p.stderr[-300:]}" if p.stderr else ""))
    check("不再固定顯示「read_all」：依模式分列讀取耗時，並列出逐端點耗時", ok1, diag)
    check("同一 Session 兩段模式：正確顯示兩段（20＋20 筆）與切換警示",
          "read_all（推定：舊版計時無逐端點資料）：2026-11-13T10:00:00 ～ 2026-11-13T10:00:19（20 筆）" in out
          and "read_cert：2026-11-13T10:00:20 ～ 2026-11-13T10:00:39（20 筆）" in out and "切換 1 次" in out)
    check("cert_state 的切換紀錄（未記錄→推定）一併顯示",
          "cert_state.hmi_reader：read_cert；2026-11-13T10:00:20 read_all → read_cert（切換前模式未記錄" in out)
    check("401 依模式分列：read_all 6 次（3 支×2 Cycle）、read_cert 2 次（1 支×2 Cycle）；含 401 的 Cycle 皆 2（可跨模式比較）",
          "read_all：401 端點次數 6；含 401 的 Cycle 2／20" in out and "read_cert：401 端點次數 2；含 401 的 Cycle 2／20" in out)
    check("明確說明：端點減少不代表登入態改善；統計範圍", "不代表登入態改善" in out and "HMI 401 統計範圍" in out)


guard("E-1", t_e1)
guard("E-2", t_e2)
guard("E-3", t_e3)
guard("E-4", t_e4)
guard("E-5／E-6", t_e56)
guard("E-7", t_e7)
guard("E-8", t_e8)
shutil.rmtree(TMP, ignore_errors=True)
n = sum(RESULTS)
print(f"\n== test_cert_o1：{n}/{len(RESULTS)} PASS ==")
print(f"結果：{n} / {len(RESULTS)} PASS（實際檢查 {len(RESULTS)} 項；回歸工具以此行計數）")
sys.exit(0 if n == len(RESULTS) else 1)
