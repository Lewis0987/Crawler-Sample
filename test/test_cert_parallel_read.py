# -*- coding: utf-8 -*-
"""
取樣 Cycle：HMI 與 6161 平行讀取、取樣時間＝HMI 讀取完成時間（離線）
====================================================================
時間軸縮放：真實 1 秒 ＝ 模擬 10 秒（clock 由 perf_counter 推算），讓 3 秒逾時、5 秒週期、60 秒停頓判斷都能在數秒內跑完。
假 HMI 讀取約 0.8 秒（模擬）、假 6161 正常約 0.1 秒、逾時 3 秒後丟 TimeoutError；記錄每次呼叫的開始／結束時間與同時呼叫數。
驗證：平行讀取、時間戳記、HMI／6161 各種成功失敗組合、長時間逾時的取樣間隔、API 次數不增加、不重疊、
     停止服務時沒有殘留執行緒、階段切換發生在逾時期間、跨整分鐘的分鐘歸屬。
執行：python test_cert_parallel_read.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import cert_report_config as CFG
import cert_requests as REQ
from cert_runtime import CertRuntime, _read_jsonl
from cert_stage_engine import stage_minutes

_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


SCALE = 10.0                      # 模擬秒／真實秒
T0 = datetime(2026, 11, 4, 19, 6, 40)


class SimClock:
    def __init__(self, t0=T0):
        self.p0 = time.perf_counter()
        self.t0 = t0

    def __call__(self):
        return self.t0 + timedelta(seconds=(time.perf_counter() - self.p0) * SCALE)


def sim_sleep(sec):
    time.sleep(sec / SCALE)


class Plant:
    """假設備：HMI／6161 讀取（模擬耗時）；記錄呼叫時間、同時呼叫數。direction()：依模擬時間決定充放電。"""

    def __init__(self, clock, hmi_ok=True, heat_mode="ok", direction=None):
        self.clock, self.hmi_ok, self.heat_mode = clock, hmi_ok, heat_mode
        self.direction = direction or (lambda t: "C")
        self.hmi_calls, self.heat_calls = [], []          # (開始, 結束)
        self.active = {"hmi": 0, "heat": 0}
        self.max_active = {"hmi": 0, "heat": 0}
        self._lk = threading.Lock()

    def _enter(self, k):
        with self._lk:
            self.active[k] += 1
            self.max_active[k] = max(self.max_active[k], self.active[k])

    def _leave(self, k):
        with self._lk:
            self.active[k] -= 1

    def read(self):
        self._enter("hmi")
        t0 = self.clock()
        try:
            sim_sleep(0.8)
            if not self.hmi_ok:
                return None
            d = self.direction(t0)
            cur = {"C": 140.0, "D": -140.0, "I": 0.0}[d]
            return {"soc_percent": 95, "battery_current_a": cur, "actual_active_power_kw": cur, "rack_max_temperature_c": 54,
                    "rack_min_temperature_c": 43, "battery_voltage_v": 956.9, "rack_cell_max_voltage_v": 3.423,
                    "rack_cell_min_voltage_v": 3.403, "_fail": [], "battery_power_status": "已上電", "pcs_fault_flag": False,
                    "pcs_charging_flag": d == "C", "pcs_discharging_flag": d == "D"}
        finally:
            self.hmi_calls.append((t0, self.clock()))
            self._leave("hmi")

    def heat(self):
        self._enter("heat")
        t0 = self.clock()
        try:
            if self.heat_mode == "timeout":
                sim_sleep(3.0)
                raise TimeoutError("timed out")
            sim_sleep(0.1)
            if self.heat_mode == "fail":
                raise OSError("模擬：連線拒絕")
            return {"st": "-", "pt": {"moduleTemperature": "75"}}
        finally:
            self.heat_calls.append((t0, self.clock()))
            self._leave("heat")


def runtime(root, plant, clock, **p):
    return CertRuntime(root, reader=plant.read, heat_reader=plant.heat, enable_shots=False, clock=clock,
                       log=lambda *a: None, params=CFG.params(**p))


def start(rt, root, clock):
    REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=clock())
    rt.process_requests()
    return rt.active_id()


def hmi_t(plant, i):
    return plant.hmi_calls[i][1]


def close(t, hmi_end):
    """observation t（既有格式只到秒，捨去小數）＝ HMI 讀取完成時間所在的那一秒。"""
    d = (hmi_end - t).total_seconds()
    return -0.05 <= d < 1.05


TMP = tempfile.mkdtemp(prefix="cert_parallel_")
loop0 = CFG.CERT_LOOP_SEC
try:
    print("== 單次 Cycle：各種成功／失敗組合 ==")
    for name, hmi_ok, mode in (("A HMI 正常、6161 正常", True, "ok"), ("B HMI 正常、6161 逾時 3 秒", True, "timeout"),
                               ("C HMI 失敗、6161 正常", False, "ok"), ("D HMI 與 6161 同時失敗", False, "fail")):
        clk = SimClock()
        pl = Plant(clk, hmi_ok, mode)
        root = os.path.join(TMP, name[0])
        rt = runtime(root, pl, clk)
        cid = start(rt, root, clk)
        c0 = clk()
        rec = rt.sample_once()
        c1 = clk()
        obs = _read_jsonl(os.path.join(root, cid, "observations.jsonl"))
        t = datetime.fromisoformat(rec["t"])
        hmi_end, heat_end = pl.hmi_calls[0][1], pl.heat_calls[0][1]
        par = pl.heat_calls[0][0] < pl.hmi_calls[0][1]               # 6161 在 HMI 結束前就已開始
        dur = (c1 - c0).total_seconds()
        if name[0] == "A":
            ok = rec["ok"] and rec["hmi_ok"] and rec["values"]["pcs_module_c"] == "75" and not rec["notes"]
        elif name[0] == "B":
            ok = (rec["ok"] and rec["hmi_ok"] and rec["values"]["soc"] == 95 and rec["values"]["current_a"] == 140.0
                  and rec["values"]["voltage_v"] == 956.9 and rec["values"]["rack_max_c"] == 54
                  and rec["values"]["cell_vmin_v"] == 3.403 and rec["values"]["pcs_module_c"] is None
                  and rec["notes"] == ["6161 讀取失敗：TimeoutError: timed out"])
        elif name[0] == "C":
            ok = rec["ok"] and not rec["hmi_ok"] and rec["values"]["pcs_module_c"] == "75" and rec["values"]["soc"] is None
        else:
            ok = (not rec["ok"] and "HMI 讀取失敗" in rec["fail_reason"] and "6161 讀取失敗：OSError" in rec["fail_reason"])
        print(f"    ▸ {name}：t − HMI 完成 {(t - hmi_end).total_seconds():+.2f}s；6161 完成 − HMI 完成 "
              f"{(heat_end - hmi_end).total_seconds():+.2f}s；Cycle {dur:.2f}s（模擬）")
        check(f"{name}：平行讀取（各 1 次）；取樣時間＝HMI 讀取完成時間；Cycle 等 6161 結束才寫入；紀錄內容正確",
              len(obs) == 1 and len(pl.hmi_calls) == 1 and len(pl.heat_calls) == 1 and par
              and close(t, hmi_end) and dur >= max((hmi_end - c0).total_seconds(), (heat_end - c0).total_seconds()) - 0.1
              and ok and not [x for x in threading.enumerate() if x.name == "cert-heat-read"])
        if name[0] == "B":
            check("B2 6161 逾時：時間戳記不再延後 3 秒（舊行為＝全部讀完才取時間 → 約 +2.2 秒）；Cycle 約 3 秒而非 3.8 秒",
                  close(t, hmi_end) and (heat_end - t).total_seconds() > 2.0 and dur < 3.5)

    print("\n== 長時間連續 6161 逾時（背景執行緒、取樣週期 5 秒、迴圈 1 秒，皆為模擬時間）＋階段切換＋跨整分鐘 ==")
    CFG.CERT_LOOP_SEC = 1.0 / SCALE
    clk = SimClock()
    sw = T0 + timedelta(seconds=75)                                    # 充電 → 待機（發生在 6161 逾時期間）
    pl = Plant(clk, True, "timeout", direction=lambda t: "C" if t < sw else "I")
    root = os.path.join(TMP, "long")
    rt = runtime(root, pl, clk)
    cid = start(rt, root, clk)
    stop = threading.Event()
    ths = rt.run(stop)
    time.sleep(170 / SCALE)                                             # 模擬約 170 秒
    stop.set()
    for th in ths:
        th.join(5)
    time.sleep(0.4)
    obs = _read_jsonl(os.path.join(root, cid, "observations.jsonl"))
    ts = [datetime.fromisoformat(o["t"]) for o in obs]
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    print(f"    ▸ {len(obs)} 筆；間隔（模擬秒）中位 {sorted(gaps)[len(gaps) // 2]:.1f}、最小 {min(gaps):.1f}、最大 {max(gaps):.1f}")
    check(f"L1 連續 6161 逾時：取樣間隔約 6 秒（5 秒＋HMI 0.8＋迴圈 ≤1 秒），不再是約 9 秒（中位 {sorted(gaps)[len(gaps) // 2]:.1f}）",
          len(obs) >= 20 and 5.0 <= sorted(gaps)[len(gaps) // 2] <= 7.2 and max(gaps) < 8.0)
    check(f"L2 API 次數不增加：HMI {len(pl.hmi_calls)} 次＝6161 {len(pl.heat_calls)} 次＝observations {len(obs)} 筆；"
          f"同時進行的 HMI 最多 {pl.max_active['hmi']}、6161 最多 {pl.max_active['heat']}（不重疊）",
          len(pl.hmi_calls) == len(pl.heat_calls) == len(obs)
          and pl.max_active == {"hmi": 1, "heat": 1}
          and all(b[0] >= a[1] for a, b in zip(pl.heat_calls, pl.heat_calls[1:])))
    check("L3 時間戳記逐筆＝該次 HMI 讀取完成時間，嚴格遞增（順序不亂）；HMI 數值全部保存、6161 全部記為讀取失敗",
          all(close(t, hmi_t(pl, i)) for i, t in enumerate(ts)) and all(b > a for a, b in zip(ts, ts[1:]))
          and all(o["hmi_ok"] and o["values"]["soc"] == 95 and "6161 讀取失敗" in o["notes"][0] for o in obs))
    res = rt.analysis()
    st = res.stages[0] if res.stages else None
    first_idle = next(i for i, t in enumerate(ts) if obs[i]["values"]["current_a"] == 0.0)
    check(f"L4 階段切換發生在 6161 逾時期間：首筆待機 B＝{st.end_B.t:%H:%M:%S}＝該次 HMI 完成時間"
          f"（不因 6161 逾時延後）；最後運行 A 為前一筆",
          st is not None and st.dir == "C" and st.end_B is not None and st.end_B.t == ts[first_idle]
          and close(st.end_B.t, hmi_t(pl, first_idle)) and st.A.t == ts[first_idle - 1]
          and (pl.heat_calls[first_idle][1] - st.end_B.t).total_seconds() > 2.0)
    mins = [m.T for m in stage_minutes(res, st, ext_scope="minute")]
    check("L5 分鐘列與取樣時間一致：每一筆階段內取樣都落在它自己時間戳記所在的分鐘列（不錯置）",
          all(ts[i].replace(second=0) in mins for i in range(len(ts)) if st.start.t <= ts[i] <= st.A.t))
    left =[x.name for x in threading.enumerate() if x.name in ("cert-heat-read", "cert-sampler", "cert-requests")]
    n_obs = len(obs)
    time.sleep(0.5)
    check(f"L6 服務停止：取樣中的讀取工作完成後即結束，沒有殘留背景執行緒（{left or '無'}）、停止後不再呼叫 API、不再寫入",
          not left and len(_read_jsonl(os.path.join(root, cid, "observations.jsonl"))) == n_obs
          and len(pl.hmi_calls) == len(pl.heat_calls) == n_obs)

    print("\n== 取樣跨越整分鐘（HMI 在 19:06:59 讀完、6161 逾時到 19:07:01 才結束）==")
    clk = SimClock(datetime(2026, 11, 4, 19, 6, 58, 300000))
    pl = Plant(clk, True, "timeout")
    root = os.path.join(TMP, "minute")
    rt = runtime(root, pl, clk)
    cid = start(rt, root, clk)
    rec = rt.sample_once()
    t = datetime.fromisoformat(rec["t"])
    print(f"    ▸ HMI 完成 {pl.hmi_calls[0][1]:%H:%M:%S.%f}；6161 結束 {pl.heat_calls[0][1]:%H:%M:%S.%f}；紀錄 t {rec['t'][11:]}")
    check("M1 HMI 在整分前讀完、6161 在整分後才結束 → 取樣時間 19:06:59，歸屬 19:06 那一分鐘（舊行為會記成 19:07:01、歸到 19:07）",
          pl.hmi_calls[0][1].minute == 6 and pl.heat_calls[0][1].minute == 7 and t == datetime(2026, 11, 4, 19, 6, 59)
          and rec["values"]["soc"] == 95)

    print("\n== 服務停止時 6161 讀取正在進行 ==")
    clk = SimClock()
    pl = Plant(clk, True, "timeout")
    root = os.path.join(TMP, "stop")
    rt = runtime(root, pl, clk)
    cid = start(rt, root, clk)
    stop = threading.Event()
    ths = rt.run(stop)
    while not pl.heat_calls and pl.active["heat"] == 0:
        time.sleep(0.01)
    stop.set()                                                          # 6161 逾時等待中停止
    for th in ths:
        th.join(5)
    alive = [x.name for x in threading.enumerate() if x.name.startswith("cert-")]
    obs = _read_jsonl(os.path.join(root, cid, "observations.jsonl"))
    check(f"S1 停止時讀取中：該 Cycle 等 6161 結束後完整寫入一筆（{len(obs)} 筆）再結束；"
          f"沒有殘留執行緒（{alive or '無'}）；API 各 {len(pl.hmi_calls)}／{len(pl.heat_calls)} 次",
          not alive and len(obs) == 1 and len(pl.hmi_calls) == len(pl.heat_calls) == 1
          and obs[0]["values"]["soc"] == 95 and pl.active == {"hmi": 0, "heat": 0})
finally:
    CFG.CERT_LOOP_SEC = loop0
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== HMI／6161 平行讀取與取樣時間 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
