# -*- coding: utf-8 -*-
"""
test_cert_r2c.py — M-S2 r2c 離線測試（FAIL-1：分析移出取樣關鍵路徑／增量轉換；FAIL-2：Raw Data 量測有效性）
==========================================================================================
全部離線：假 HMI reader、假截圖 worker（只記錄呼叫）、暫存資料夾；不連任何設備、不送任何控制。
可指定受測程式目錄（重現修正前的失敗）：python test_cert_r2c.py [--code <test 目錄>] [--quick]

  P-1 取樣關鍵路徑與資料量無關（1k vs 36k 筆）　P-2 狀態執行緒不讓取樣等鎖
  C-1 增量分析＝完整重算（逐筆判定、階段、停頓）　C-2 增量快取失效與重建（新認證／續接／Cache OFF）
  C-3 較舊的分析結果不覆蓋較新的（Cache／已套用版本）　C-4 認證切換：前一份的結果不套用到新認證
  C-5 Menu 22 結束與背景分析競爭：最後階段只處理一次、結束後不再套用　C-6 截圖觸發：背景模式＝同步模式、不重複
  C-7 壓力：取樣／背景分析／狀態／請求式分析同時運作（真實執行緒）
  F2-1～F2-6 階段結束旗標延遲：真實 0 A 列為 LIVE、狀態 N/A、PCS 狀態原文；讀取失敗／缺欄位不是 LIVE；不使用未來資料；
             階段頁規則不變；Excel 與內部格點品質一致
注意：本檔為 A／B 類（離線／合成）證據，不代表實機長時間驗證（C 類）。
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--code", default=os.path.dirname(os.path.abspath(__file__)))
ap.add_argument("--quick", action="store_true", help="縮短 C-7 壓力測試時間")
ARGS = ap.parse_args()
sys.path.insert(0, os.path.abspath(ARGS.code))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                       # noqa: BLE001
    pass

import cert_report_config as CFG                        # noqa: E402
import cert_report_xlsx as RX                           # noqa: E402
import cert_requests as REQ                             # noqa: E402
import cert_runtime as CR                               # noqa: E402
import cert_stage_engine as SE                          # noqa: E402
import report_display as RD                             # noqa: E402
import report_quality as RQ                             # noqa: E402
import sample_scheduler as SS                           # noqa: E402

print(f"受測程式：{os.path.abspath(ARGS.code)}")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"（{detail}）" if detail else ""))


def guard(label, fn):
    """執行一段測試；例外（例：受測版本沒有該介面）→ 記為 FAIL，繼續其他測試。"""
    try:
        fn()
    except Exception as e:                              # noqa: BLE001
        check(f"{label}：執行例外 {type(e).__name__}: {str(e)[:120]}", False)


TMP = tempfile.mkdtemp(prefix="cert_r2c_")
KEYS = {"soc": "soc_percent", "v": "battery_voltage_v", "i": "battery_current_a", "p": "actual_active_power_kw",
        "rmax": "rack_max_temperature_c", "rmin": "rack_min_temperature_c",
        "cmax": "rack_cell_max_voltage_v", "cmin": "rack_cell_min_voltage_v"}
GOOD = dict(soc=50.0, v=950.0, i=0.0, p=0.0, rmax=30.0, rmin=25.0, cmax=3.40, cmin=3.30)


def reading(flag=None, pcs_status=None, **kw):
    """假 HMI 讀值。flag：None＝依電流方向給旗標（|I|>1 A）；"D"／"C"＝強制該方向旗標（模擬 PCS 旗標延遲歸零）。"""
    vals = dict(GOOD)
    vals.update(kw)
    r = {KEYS[k]: v for k, v in vals.items()}
    cur = vals.get("i") if isinstance(vals.get("i"), (int, float)) else 0.0
    chg, dis = (cur > 1.0, cur < -1.0) if flag is None else (flag == "C", flag == "D")
    r.update({"_fail": [], "communication_ok": True, "raw_source_time": "", "pcs_charging_flag": chg,
              "pcs_discharging_flag": dis, "pcs_fault_flag": False, "battery_power_status": "已上電"})
    if pcs_status:
        r["pcs_status"] = pcs_status
    return r


def profile(i):
    """合成 1 Cycle（每秒一筆）：待機 60 → 放電 1800 → 待機 300 → 充電 1800 → 待機（重複）。"""
    k = i % 4000
    if 60 <= k < 1860:
        return reading(i=-140.0, p=-114.0, soc=max(0.0, 90 - (k - 60) / 20))
    if 2160 <= k < 3960:
        return reading(i=140.0, p=114.0, soc=min(100.0, (k - 2160) / 20))
    return reading()


def make_records(n, t_end):
    t0 = t_end - timedelta(seconds=n)
    return [CR.build_record(t0 + timedelta(seconds=j), profile(j), "", None, "") for j in range(n)]


class DummyWorker:
    """假截圖 worker：只記錄呼叫（不開瀏覽器、不擷取）。"""
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def submit(self, kind, stage_no, key, name, reason, **kw):
        with self.lock:
            jid = f"J{len(self.calls) + 1}"
            self.calls.append(("submit", kind, key, (kw.get("extra") or {}).get("b_time"), jid))
            return jid

    def resolve(self, jid, as_, note):
        with self.lock:
            self.calls.append(("resolve", as_, jid))

    def close(self, timeout=None):
        pass

    def mark_unfinished(self, msg):
        return 0


def new_runtime(name, reader=None, clock=datetime.now):
    root = os.path.join(TMP, name)
    rt = CR.CertRuntime(root, reader=reader or (lambda: reading()), heat_reader=None, enable_shots=False,
                        clock=clock, log=lambda *a: None)
    return rt, root


def start(rt, root, clock=datetime.now):
    REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=clock())
    rt.process_requests()
    rt.worker = DummyWorker()
    return rt.active_id()


def cycle_ms(rt, force_analysis=True):
    if force_analysis:
        rt._last_after_analysis = None                  # 舊版：強制本筆取樣後分析（模擬分析週期到期）
    t0 = time.perf_counter()
    with rt._sample_lock:
        rt._sample_cycle("schedule")
    return (time.perf_counter() - t0) * 1000


# =============================================================================================== P-1
def t_p1():
    print("\n== P-1 取樣關鍵路徑與資料量無關（正式模式：背景分析）==")
    res = {}
    for n in (1000, 36000):
        rt, root = new_runtime(f"p1_{n}")
        start(rt, root)
        rt._async_analysis = True                      # 正式模式（run() 會設定；舊版沒有此屬性 → 不影響）
        rt.records.extend(make_records(n, datetime.now()))
        cycle_ms(rt, False)                            # 暖機（6161 連線狀態首次重播全部紀錄）
        res[n] = [cycle_ms(rt) for _ in range(6)]
    a, b = max(res[1000]), max(res[36000])
    check(f"36k 筆時取樣 Cycle 最大 {b:.1f} ms（1k 筆 {a:.1f} ms）：不含完整分析、不隨資料量成長",
          b < 50 and b - a < 25)
    return res


# =============================================================================================== P-2
def t_p2():
    print("\n== P-2 狀態執行緒（write_status）不讓取樣等鎖 ==")
    rt, root = new_runtime("p2")
    start(rt, root)
    rt._async_analysis = True
    rt.records.extend(make_records(36000, datetime.now()))
    cycle_ms(rt, False)
    stop = threading.Event()
    errs = []

    def status_loop():
        while not stop.is_set():
            try:
                rt._last_status_analysis = None         # 舊版：強制重算（模擬快取逾時）
                rt.write_status()
            except Exception as e:                      # noqa: BLE001
                errs.append(e)
    th = threading.Thread(target=status_loop, daemon=True)
    th.start()
    time.sleep(0.3)
    ms = []
    for _ in range(25):
        ms.append(cycle_ms(rt, False))
        time.sleep(0.05)
    stop.set()
    th.join(10)
    p95 = sorted(ms)[int(len(ms) * 0.95) - 1]
    check(f"36k 筆、狀態執行緒連續寫入時，取樣 Cycle p95 {p95:.1f} ms、最大 {max(ms):.1f} ms（< 60 ms）",
          p95 < 60 and not errs, f"狀態例外 {len(errs)}")


# =============================================================================================== C-1／C-2
def sig(res):
    st = [(s.no, s.dir, s.start.t, s.A.t, getattr(s.end_B, "t", None), s.end_by) for s in res.stages]
    pz = [(p.item_id, p.certain, p.stage_no) for p in res.pauses]
    ob = [(o.t, o.seq, o.state, o.stage_no, o.direction) for o in res.obs]
    return st, pz, ob


def full_reference(rt):
    obs = [CR.record_to_obs(r, i + 1) for i, r in enumerate(rt.records)]
    return SE.analyze(obs, rt.p, list(rt.decisions), [])


def t_c1():
    print("\n== C-1 增量分析＝完整重算 ==")
    rt, root = new_runtime("c1")
    start(rt, root)
    allr = make_records(9000, datetime.now())
    ok, n_checked = True, 0
    for cut in (100, 1500, 1501, 1900, 4200, 8000, 9000):
        rt.records.extend(allr[len(rt.records):cut])
        a = rt.analysis()
        ok &= sig(a) == sig(full_reference(rt))
        n_checked += 1
    check(f"逐步增加筆數（{n_checked} 個時點）：階段、停頓、逐筆狀態／階段／方向與完整重算相同", ok)
    a1 = rt.analysis()
    a2 = rt.analysis()
    check("輸入不變 → 回傳同一結果（Cache 命中）", a1 is a2)


def t_c2():
    print("\n== C-2 增量快取失效與重建 ==")
    rt, root = new_runtime("c2")
    cid1 = start(rt, root)
    rt.records.extend(make_records(3000, datetime.now()))
    a = rt.analysis()
    REQ.new_request(root, "end_cert", cert_id=cid1, now=datetime.now())
    rt.process_requests()
    cid2 = start(rt, root)
    rt.records.extend(make_records(200, datetime.now()))
    b = rt.analysis()
    check("新認證：分析只含新認證的紀錄（舊認證的快取不沿用）", len(b.obs) == 200 and sig(b) == sig(full_reference(rt))
          and len(a.obs) == 3000 and cid1 != cid2)
    # 續接（startup → _load 從檔案重載）
    rt2 = CR.CertRuntime(root, reader=lambda: reading(), enable_shots=False, log=lambda *a: None)
    rt2.startup()
    c = rt2.analysis()
    check("續接重載：以檔案中的紀錄重建，結果與完整重算相同", sig(c) == sig(full_reference(rt2)))
    rt2.analysis_cache_enabled = False
    d1 = rt2.analysis()
    d2 = rt2.analysis()
    check("Cache OFF：每次重算、結果相同（不是同一物件）", d1 is not d2 and sig(d1) == sig(d2) == sig(c))
    # 同一份結果的 Obs 不會被之後的分析改寫（每次分析都是新物件）
    keep = [(o.state, o.stage_no) for o in c.obs]
    rt2.records.extend(make_records(50, datetime.now() + timedelta(seconds=3000)))
    rt2.analysis_cache_enabled = True
    rt2.analysis()
    check("之後的分析不改寫先前結果的 Obs 判定欄位", keep == [(o.state, o.stage_no) for o in c.obs])


# =============================================================================================== C-3
class Blocker:
    """把 SE.analyze 包起來：armed 時第一個呼叫會停在 gate，直到 release。"""
    def __init__(self):
        self.orig = CR.analyze
        self.armed = False
        self.entered = threading.Event()
        self.gate = threading.Event()

    def __call__(self, *a, **k):
        if self.armed:
            self.armed = False
            self.entered.set()
            self.gate.wait(20)
        return self.orig(*a, **k)

    def __enter__(self):
        CR.analyze = self
        return self

    def __exit__(self, *exc):
        CR.analyze = self.orig
        self.gate.set()


def run_bg(fn, out, key):
    def w():
        try:
            out[key] = fn()
        except Exception as e:                          # noqa: BLE001
            out[key] = e
    th = threading.Thread(target=w, daemon=True)
    th.start()
    return th


def t_c3():
    print("\n== C-3 較舊的分析結果不覆蓋較新的 ==")
    rt, root = new_runtime("c3")
    start(rt, root)
    rt.records.extend(make_records(2000, datetime.now()))
    out = {}
    with Blocker() as blk:
        blk.armed = True
        th_old = run_bg(rt.analysis, out, "old")       # 版本 2000：計算中被擋住
        check("（前提）較舊的分析確實進入計算", blk.entered.wait(5))
        rt.records.extend(make_records(10, datetime.now() + timedelta(seconds=10)))
        th_new = run_bg(rt.analysis, out, "new")       # 版本 2010
        th_new.join(3)
        check("較舊的分析計算中，較新的分析不需等待（計算不持 runtime 鎖）", not th_new.is_alive())
        blk.gate.set()
        th_old.join(10)
        th_new.join(10)
    c = rt._analysis_cache
    check("較舊結果完成後，Cache 仍是較新版本（2010 筆）", c is not None and c[0][1] == 2010
          and len(c[1].obs) == 2010)
    # 已套用版本：背景分析（舊快照）不得在較新的同步套用之後覆蓋
    out2 = {}
    rt.records.extend(make_records(3, datetime.now() + timedelta(seconds=20)))   # 讓背景分析的快照不命中 Cache
    with Blocker() as blk:
        blk.armed = True
        th = run_bg(rt.bg_analysis_once, out2, "bg")
        check("（前提）背景分析確實進入計算", blk.entered.wait(5))
        rt.records.extend(make_records(5, datetime.now() + timedelta(seconds=30)))
        with rt._lock:
            rt._after_sample(datetime.now(), force=True)   # 同步套用較新版本（如 Menu 22 最後一筆）
        blk.gate.set()
        th.join(10)
    check("背景分析的較舊結果在較新套用之後 → 不套用（older）", out2.get("bg") == "older", str(out2.get("bg")))


# =============================================================================================== C-4／C-5
def t_c45():
    print("\n== C-4／C-5 認證切換、Menu 22 結束與背景分析競爭 ==")
    t = [datetime.now()]
    clk = lambda: t[0]                                  # noqa: E731
    rt, root = new_runtime("c45", clock=clk)
    cid1 = start(rt, root, clk)
    w1 = rt.worker
    rt._async_analysis = True
    # 放電 → 待機（形成候選 B）：讓背景分析有截圖要觸發
    base = t[0]
    for j in range(400):
        r = reading(i=-140.0, p=-114.0) if j < 300 else reading()
        rt.records.append(CR.build_record(base + timedelta(seconds=j), r, "", None, ""))
    t[0] = base + timedelta(seconds=400)
    out = {}
    with Blocker() as blk:
        blk.armed = True
        th = run_bg(rt.bg_analysis_once, out, "bg")
        check("（前提）背景分析確實進入計算", blk.entered.wait(5))
        n_before = len(w1.calls)
        REQ.new_request(root, "end_cert", cert_id=cid1, now=clk())
        rt.process_requests()                           # Menu 22：同步最後分析＋結束＋產報
        ended_calls = list(w1.calls)
        cid2 = start(rt, root, clk)
        w2 = rt.worker
        blk.gate.set()
        th.join(10)
    check("背景分析在 Menu 22 結束／新認證開始之後完成 → 丟棄（stale／inactive），不套用", out.get("bg") in ("stale", "inactive"),
          str(out.get("bg")))
    check("前一份認證的結果沒有送到新認證的截圖 worker", w2.calls == [])
    check("結束後舊 worker 沒有再收到背景分析的呼叫", w1.calls == ended_calls)
    st_end = [c for c in ended_calls if c[0] == "resolve" and c[1] == "stage_end"] + \
             [c for c in ended_calls if c[0] == "submit" and c[1] in ("stage_end", "delayed")]
    cands = [c for c in ended_calls if c[0] == "submit" and c[1] == "candidate"]
    # 本情境背景分析被擋住、待機已超過截圖時效（100 秒 > 40 秒）→ 不補候選；Menu 22 同步分析處理階段結束（延遲補拍）一次
    check(f"最後階段在 Menu 22 結束時完成處理且只一次（候選 {len(cands)}、階段結束／延遲 {len(st_end)}）",
          len(cands) == 0 and len(st_end) == 1 and n_before == 0, str(ended_calls))
    st = json.load(open(os.path.join(root, cid1, "cert_state.json"), encoding="utf-8"))
    check("前一份認證已正常結束（ended）、新認證進行中", st["status"] == "ended" and rt.active_id() == cid2)


# =============================================================================================== C-6
def scenario():
    """放電 → 停頓 20 秒（同方向恢復）→ 放電 → 待機（確認）→ 充電 → 待機。"""
    seq = []
    for j in range(900):
        if j < 30:
            r = reading()
        elif j < 250:
            r = reading(i=-140.0, p=-114.0)
        elif j < 270:
            r = reading()                               # 停頓
        elif j < 450:
            r = reading(i=-140.0, p=-114.0)
        elif j < 600:
            r = reading()                               # 待機（階段結束）
        elif j < 800:
            r = reading(i=140.0, p=114.0)
        else:
            r = reading()
        seq.append(r)
    return seq


def t_c6():
    print("\n== C-6 截圖觸發：背景模式與同步模式結果相同、不重複 ==")
    seq = scenario()
    logs = {}
    for mode in ("sync", "async"):
        t = [datetime(2026, 11, 12, 9, 0, 0)]
        clk = lambda: t[0]                              # noqa: E731
        rt, root = new_runtime("c6_" + mode, clock=clk)
        cid = start(rt, root, clk)
        rt._async_analysis = mode == "async"
        base = t[0]
        for j, r in enumerate(seq):
            t[0] = base + timedelta(seconds=j)
            rt.records.append(CR.build_record(t[0], r, "", None, ""))
            with rt._lock:
                rt._after_sample(t[0])                  # 同步模式：每 5 秒分析；背景模式：此處不分析
            if mode == "async" and j % 5 == 4:
                rt.bg_analysis_once()
                if j % 50 == 4:
                    rt.bg_analysis_once()               # 重複執行（同一版本）也不得重複觸發
        w = rt.worker                                   # 結束後 _reset 會清掉 rt.worker → 先保留參照
        REQ.new_request(root, "end_cert", cert_id=cid, now=clk())
        rt.process_requests()
        logs[mode] = [(c[0], c[1], c[2], c[3]) if c[0] == "submit" else (c[0], c[1]) for c in w.calls]
    a, b = logs["sync"], logs["async"]
    subs = [c for c in b if c[0] == "submit"]
    check(f"背景模式的截圖觸發序列＝同步模式（{len(a)} 個呼叫）", a == b and len(a) > 0, f"sync {a} / async {b}")
    check("沒有重複的候選／階段結束截圖", len(subs) == len(set(subs)))


# =============================================================================================== C-7
def t_c7():
    dur = 20 if ARGS.quick else 60
    print(f"\n== C-7 壓力：取樣／背景分析／狀態／請求式分析同時運作（真實執行緒 {dur} 秒，預載 20k 筆）==")
    import random
    rnd = random.Random(7)

    def rd():
        time.sleep(rnd.uniform(0.2, 0.5))              # HMI 0.2～0.5 秒（遠低於 1 秒格點）
        return reading(i=-140.0, p=-114.0)
    rt, root = new_runtime("c7", reader=rd)
    start(rt, root)
    rt.records.extend(make_records(20000, datetime.now() - timedelta(seconds=5)))
    with rt._lock:
        rt._obs_lines = len(rt.records)
    logs = []
    rt.log = logs.append
    stop = threading.Event()
    ths = rt.run(stop)
    errs, vers = [], []

    def hammer():
        k = 0
        while not stop.is_set():
            try:
                k += 1
                if k % 50 == 0:
                    with rt._lock:                      # 少數持鎖呼叫（如結束收尾）：約每 10 秒一次
                        a = rt.analysis()
                else:
                    a = rt.analysis()                   # 請求式分析（Menu 標記／確認／立即截圖）：不持鎖呼叫
                vers.append(len(a.obs))
                rt.write_status()
            except Exception as e:                      # noqa: BLE001
                errs.append(e)
            time.sleep(0.2)
    hs = [threading.Thread(target=hammer, daemon=True) for _ in range(2)]
    for h in hs:
        h.start()
    time.sleep(dur)
    stop.set()
    for th in ths + hs:
        th.join(15)
    folder = rt.folder
    slots = [json.loads(l) for l in open(os.path.join(folder, "slots.jsonl"), encoding="utf-8") if l.strip()]
    st = [s.get("slot_status") for s in slots if s.get("rec") == "SLOT"]
    miss = sum(1 for s in st if s in (RQ.MISSED, "MISSED"))
    ms_idx = [s.get("slot_index") for s in slots if s.get("rec") == "SLOT" and s.get("slot_status") == RQ.MISSED]
    print(f"    MISSED 格點序號 {ms_idx[:10]}；原因 {sorted({s.get('missed_reason') for s in slots if s.get('slot_status') == RQ.MISSED})[:3]}")
    tim = [json.loads(l) for l in open(os.path.join(folder, "cert_timing.jsonl"), encoding="utf-8") if l.strip()]
    lw = sorted(x.get("rt_lock_wait_ms") or 0 for x in tim)
    bgn = sum(x.get("bg_analysis_n") or 0 for x in tim)
    steady = [i for i in ms_idx if i is not None and i >= 3]
    warm = [i for i in ms_idx if i is not None and i < 3]
    print(f"    （照實列出）啟動暖機（前 3 格；預載 20k 筆、多個分析執行緒同時第一次建立範本）MISSED {len(warm)} 格")
    check(f"{len(st)} 格；穩定期（第 3 格起）MISSED {len(steady)}（HMI 0.2～0.5 秒 → 應為 0）；"
          f"取樣等鎖 p95 {lw[int(len(lw) * .95) - 1]:.1f} ms", not steady and len(st) >= dur - 3)
    check(f"沒有例外（執行緒記錄 {len([x for x in logs if '例外' in str(x)])}、併發呼叫 {len(errs)}）",
          not errs and not any("例外" in str(x) for x in logs), str(logs[:3]) + str(errs[:3]))
    check(f"背景分析有執行（{bgn} 次；計時檔有記錄）", bgn >= dur // CFG.CERT_ANALYSIS_INTERVAL_SEC - 2)
    sj = json.load(open(os.path.join(root, "status.json"), encoding="utf-8"))
    check("status.json 可解析且含階段資訊", "stages" in sj)


# =============================================================================================== FAIL-2
class Sim:
    def __init__(self):
        self.t = 0.0


SIM = Sim()
WALL0 = datetime(2026, 11, 12, 13, 0, 0, tzinfo=timezone(timedelta(hours=8)))
T0 = WALL0.replace(tzinfo=None)


class SimStop:
    def __init__(self, rt, root, stop_at, end_at):
        self.rt, self.root, self.stop_at, self.end_at = rt, root, stop_at, end_at
        self.flag = self.ended = False

    def is_set(self):
        return self.flag or SIM.t >= self.stop_at

    def set(self):
        self.flag = True

    def wait(self, timeout=None):
        SIM.t += timeout if timeout else 1.0
        if not self.ended and SIM.t >= self.end_at:
            self.ended = True
            REQ.new_request(self.root, "end_cert", cert_id=self.rt.active_id(), now=T0 + timedelta(seconds=SIM.t))
            self.rt.process_requests()
            self.flag = True
        return self.is_set()


def f2_script(i):
    """0～29 待機；30～149 放電；150～161 電流 0 但放電旗標未歸零（12 秒）；162～ 待機。
    170～172 讀取失敗（None）；175 缺必要欄位（Cell 最大電壓）；耗時 0.5 秒。"""
    if 170 <= i <= 172:
        return 0.5, None
    if i < 30:
        return 0.5, reading()
    if i < 150:
        return 0.5, reading(i=-140.0, p=-114.0)
    if i < 162:
        return 0.5, reading(i=0.0, p=-113.7, flag="D", pcs_status="執行 / 併網 / 放電")
    if i == 175:
        return 0.5, reading(cmax=None)
    return 0.5, reading(pcs_status="停止 / 併網")


def t_f2():
    print("\n== FAIL-2 階段結束時 PCS 旗標延遲：Raw Data 呈現真實量測 ==")
    SIM.t = 0.0
    wall = lambda: WALL0 + timedelta(seconds=SIM.t)     # noqa: E731
    o_wall, o_ss, o_rq = SS.wall_now, SS.SlotScheduler.__init__.__defaults__, RQ.SlotSegment.__init__.__defaults__
    SS.wall_now = wall
    SS.SlotScheduler.__init__.__defaults__ = (time.monotonic, wall, None)
    RQ.SlotSegment.__init__.__defaults__ = ("start", time.monotonic, wall, RQ.SLOT_LOG_FILE)
    try:
        calls = []

        def rd():
            i = len(calls)
            calls.append(SIM.t)
            dur, out = f2_script(i)
            SIM.t += dur
            return out
        clk = lambda: T0 + timedelta(seconds=SIM.t)     # noqa: E731
        root = os.path.join(TMP, "f2")
        rt = CR.CertRuntime(root, reader=rd, heat_reader=None, enable_shots=False, clock=clk,
                            log=lambda *a: None, mono=lambda: SIM.t)
        REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=clk())
        rt.process_requests()
        cid = rt.active_id()
        rt._slot_sample_loop(SimStop(rt, root, stop_at=245, end_at=240))
    finally:
        SS.wall_now, SS.SlotScheduler.__init__.__defaults__, RQ.SlotSegment.__init__.__defaults__ = o_wall, o_ss, o_rq
    folder = os.path.join(root, cid)
    res = rt._offline(folder).analysis()
    ss = CR.build_sampling_slots(folder)
    rs = RX.raw_second_rows(res, ss["grid"])
    rows = rs["rows"]
    nm = RX.stage_names(res.stages)
    names = {s.no: RX._stage_title(nm, s) for s in res.stages}
    p = res.params
    lag = [r for r in rows if r["obs"] is not None and r["state"] == RD.RAW_LIVE
           and (r["obs"].flags or {}).get("pcs_status") == "執行 / 併網 / 放電"]
    lag_obs = [o for o in res.obs if (o.flags or {}).get("pcs_status") == "執行 / 併網 / 放電"]
    check(f"旗標延遲的 12 筆真實讀值（電流 0.0 A）在 Raw Data 都是 LIVE（{len(lag)}／{len(lag_obs)}）",
          len(lag_obs) == 12 and len(lag) == 12 and all(r["obs"].values.get("current_a") == 0.0 for r in lag))
    check("這些列的狀態欄 N/A（不由電流推論為待機）、說明欄附 PCS 狀態原文",
          all(RX.status_text(r["obs"], names) == RX.NA and "PCS 狀態：執行 / 併網 / 放電" in RX._raw1_note(r, p)
              for r in lag) and len(lag) == 12)
    bad = [r for r in rows if r["obs"] is not None and r["state"] == RD.RAW_CARRIED
           and r["obs"].values.get("current_a") == -140.0 and r["time"] >= lag_obs[0].t.replace(microsecond=0)] \
        if lag_obs else ["?"]
    check("放電結束後沒有任何一列沿用 −140 A", not bad, str([str(r["time"]) for r in bad][:5]))
    fail_obs = [o for o in res.obs if not o.ok]
    miss_field = [o for o in res.obs if o.ok and o.values.get("cell_vmax_v") is None]
    live_src = {id(r["obs"]) for r in rows if r["state"] == RD.RAW_LIVE}
    check(f"讀取失敗（{len(fail_obs)} 筆）與缺必要欄位（{len(miss_field)} 筆）不是 LIVE",
          len(fail_obs) == 3 and len(miss_field) == 1
          and not any(id(o) in live_src for o in fail_obs + miss_field))
    fut = [r for r in rows if r["src"] is not None and r["obs"] is not None and r["obs"].t > r["time"] + timedelta(seconds=2)]
    check("沒有使用未來資料（來源觀測時間不晚於該列＋讀取時間）", not fut)
    st1 = res.stages[0] if res.stages else None
    check("階段判定不變：第 1 階段放電、首筆觀測待機 B＝第一筆 0 A（旗標延遲那筆）",
          st1 is not None and st1.dir == "D" and st1.end_B is not None and st1.end_B.values.get("current_a") == 0.0
          and (st1.end_B.flags or {}).get("pcs_status") == "執行 / 併網 / 放電")
    check("階段頁選樣規則不變：旗標延遲的讀值仍不是報告有效觀測（is_report_valid）",
          all(not RX.is_report_valid(o, p, names) for o in lag_obs)
          and all(RX.raw_measurement_valid(o, p) for o in lag_obs))
    # 內部格點品質與 Excel 一致（雙向）：格點紀錄有讀取且該筆量測有效 ⇔ 該筆成為 LIVE 列的來源
    ref2obs, _miss = RX._obs_by_ref(res.obs, ss["grid"].get("raw") or [])
    att = [s for s in ss["grid"]["result"]["slots"] if s.get("raw_ref") is not None
           and s.get("status") in (RQ.ATTEMPTED_OK, RQ.ATTEMPTED_PARTIAL)]
    good_refs = {s["raw_ref"] for s in att if ref2obs.get(s["raw_ref"]) is not None
                 and RX.raw_measurement_valid(ref2obs[s["raw_ref"]], p)}
    live_refs = {r["src"] for r in rows if r["state"] == RD.RAW_LIVE}
    check(f"格點紀錄有讀取且量測有效的 {len(good_refs)} 筆 ⇔ Raw Data LIVE 來源 {len(live_refs)} 筆（Excel 與 JSONL 不矛盾）",
          good_refs == live_refs and len(good_refs) > 0)
    # Excel
    out = os.path.join(TMP, "f2.xlsx")
    ok, msg, path = rt.generate_report(cid)
    from openpyxl import load_workbook
    ws = load_workbook(path)["Raw Data"]
    hdr = [c.value for c in ws[1]]
    ex = {str(r[0])[11:19]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    lag_rows = [ex.get(f"{r['time']:%H:%M:%S}") for r in lag]
    check("Excel：13 欄；旗標延遲列電流 0、狀態 N/A、說明含 PCS 狀態",
          len(hdr) == 13 and ok and all(x is not None and x[3] == 0 and x[1] == RX.NA and "PCS 狀態" in (x[12] or "")
                                        for x in lag_rows))


# =============================================================================================== 執行
guard("P-1", t_p1)
guard("P-2", t_p2)
guard("C-1", t_c1)
guard("C-2", t_c2)
guard("C-3", t_c3)
guard("C-4／C-5", t_c45)
guard("C-6", t_c6)
guard("C-7", t_c7)
guard("FAIL-2", t_f2)
shutil.rmtree(TMP, ignore_errors=True)
n = sum(RESULTS)
print(f"\n== test_cert_r2c：{n}/{len(RESULTS)} PASS ==")
sys.exit(0 if n == len(RESULTS) else 1)
