# -*- coding: utf-8 -*-
"""
test_cert_o2.py — M-S2 r2c O-2 離線測試：認證結束／切換後釋放分析記憶體（增量範本＋分析快取）
==========================================================================================
全部離線：假 HMI reader、假截圖 worker、暫存資料夾；不連任何設備、不送任何控制。
可指定受測程式目錄（重現修正前的保留）：python test_cert_o2.py [--code <test 目錄>]

  O2-1 Menu 22 結束後：範本、快取、舊 records 全部可回收（weakref 驗證）
  O2-2 Menu 22 報告內容與事後 Menu 25 重產一致（釋放不影響結束當下的報告）
  O2-3 Menu 25（已結束認證）走離線重建：結果正確，且不把舊資料重新掛回主 runtime
  O2-4 Menu 25（進行中認證）：沿用快取，不清除（進行中不受影響）
  O2-5 競爭：背景分析在 Menu 22 結束「轉換中」／「轉換前」→ 結果丟棄、範本不重新掛回舊 list
  O2-6 認證切換：新認證分析＝完整重算；舊認證物件已回收
  O2-7 續接重載（_load）：舊 list 的範本與快取釋放
  O2-8 壓力：真實三執行緒（取樣／背景分析／請求＋狀態）運作中連續 3 次開始／結束認證
注意：本檔為 A／B 類（離線／合成）證據，不代表實機長時間驗證（C 類）。
"""
import argparse
import gc
import os
import shutil
import sys
import tempfile
import threading
import time
import weakref
from datetime import datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument("--code", default=os.path.dirname(os.path.abspath(__file__)))
ARGS = ap.parse_args()
sys.path.insert(0, os.path.abspath(ARGS.code))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                       # noqa: BLE001
    pass

import cert_report_config as CFG                        # noqa: E402
import cert_requests as REQ                             # noqa: E402
import cert_runtime as CR                               # noqa: E402

print(f"受測程式：{os.path.abspath(ARGS.code)}")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"（{detail}）" if detail else ""))


def guard(label, fn):
    try:
        fn()
    except Exception as e:                              # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{label}：執行例外 {type(e).__name__}: {str(e)[:160]}", False)


TMP = tempfile.mkdtemp(prefix="cert_o2_")
KEYS = {"soc": "soc_percent", "v": "battery_voltage_v", "i": "battery_current_a", "p": "actual_active_power_kw",
        "rmax": "rack_max_temperature_c", "rmin": "rack_min_temperature_c",
        "cmax": "rack_cell_max_voltage_v", "cmin": "rack_cell_min_voltage_v"}
GOOD = dict(soc=50.0, v=950.0, i=0.0, p=0.0, rmax=30.0, rmin=25.0, cmax=3.40, cmin=3.30)


def reading(**kw):
    vals = dict(GOOD)
    vals.update(kw)
    r = {KEYS[k]: v for k, v in vals.items()}
    cur = vals["i"]
    r.update({"_fail": [], "communication_ok": True, "raw_source_time": "", "pcs_charging_flag": cur > 1.0,
              "pcs_discharging_flag": cur < -1.0, "pcs_fault_flag": False, "battery_power_status": "已上電"})
    return r


def profile(i):
    k = i % 4000
    if 60 <= k < 1860:
        return reading(i=-140.0, p=-114.0, soc=max(0.0, 90 - (k - 60) / 20))
    if 2160 <= k < 3960:
        return reading(i=140.0, p=114.0, soc=min(100.0, (k - 2160) / 20))
    return reading()


class WRec(dict):
    """可 weakref 的原始紀錄（dict 子類別；內容與 dict 相同）→ 用來驗證舊認證的紀錄是否已無任何參照。"""
    __slots__ = ("__weakref__",)


def make_records(n, t_end):
    t0 = t_end - timedelta(seconds=n)
    return [WRec(CR.build_record(t0 + timedelta(seconds=j), profile(j), "", None, "")) for j in range(n)]


class DummyWorker:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def submit(self, kind, stage_no, key, name, reason, **kw):
        with self.lock:
            jid = f"J{len(self.calls) + 1}"
            self.calls.append(("submit", kind, key, jid))
            return jid

    def resolve(self, jid, as_, note):
        with self.lock:
            self.calls.append(("resolve", as_, jid))

    def close(self, timeout=None):
        pass

    def mark_unfinished(self, msg):
        return 0


def new_runtime(name, reader=None, clock=datetime.now, log=None):
    root = os.path.join(TMP, name)
    rt = CR.CertRuntime(root, reader=reader or (lambda: reading()), heat_reader=None, enable_shots=False,
                        clock=clock, log=log or (lambda *a: None))
    return rt, root


def start(rt, root, clock=datetime.now):
    REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=clock())
    rt.process_requests()
    rt.worker = DummyWorker()
    return rt.active_id()


def end(rt, root, cid, clock=datetime.now):
    REQ.new_request(root, "end_cert", cert_id=cid, now=clock())
    rt.process_requests()


def regen(rt, root, cid, clock=datetime.now):
    REQ.new_request(root, "regenerate", cert_id=cid, now=clock())
    rt.process_requests()


def preload(rt, n):
    """寫入檔案並放入記憶體（報告／離線重建要讀檔）。回傳 weakref 樣本。"""
    recs = make_records(n, datetime.now())
    with open(os.path.join(rt.folder, "observations.jsonl"), "a", encoding="utf-8") as f:
        for r in recs:
            f.write(CR.json.dumps(r, ensure_ascii=False, default=str) + "\n")
    rt.records.extend(recs)
    rt._obs_lines += n
    return [weakref.ref(recs[k]) for k in (0, n // 2, n - 1)]


def alive(refs):
    gc.collect()
    return sum(1 for r in refs if r() is not None)


def retained(rt):
    return {"tpl": len(rt._tpl), "tpl_src": rt._tpl_src is not None and len(rt._tpl_src),
            "cache": rt._analysis_cache is not None and rt._analysis_cache[0][1]}


def sig(res):
    return ([(o.state, o.stage_no, o.direction) for o in res.obs],
            [(s.no, s.dir, s.end_by, CR._iso(s.start.t)) for s in res.stages],
            [(p.item_id, p.certain) for p in res.pauses])


def full_reference(rt):
    obs = [CR.record_to_obs(r, i + 1) for i, r in enumerate(rt.records)]
    return CR.analyze(obs, rt.p, list(rt.decisions), [])


def find_type(rt, n):
    gc.collect()
    return sum(1 for o in gc.get_objects() if type(o) is WRec) - n


# =============================================================================================== O2-1／O2-2
def t_o21():
    print("\n== O2-1／O2-2 Menu 22 結束後釋放；Menu 22 報告＝Menu 25 重產 ==")
    rt, root = new_runtime("o21")
    cid = start(rt, root)
    refs = preload(rt, 6000)
    rt.analysis()
    before = retained(rt)
    check("（前提）結束前範本與快取存在", before["tpl"] == 6000 and before["cache"] == 6000, str(before))
    end(rt, root, cid)
    after = retained(rt)
    check("Menu 22 結束後：範本已清空、快取已清除", after["tpl"] == 0 and after["tpl_src"] is False
          and after["cache"] is False, str(after))
    n_alive = alive(refs)
    check("Menu 22 結束後：舊認證的原始紀錄已無任何參照（可回收）", n_alive == 0, f"仍存活 {n_alive}/3")
    st = REQ.read_json(os.path.join(root, cid, "cert_state.json"))
    reps = sorted(os.listdir(os.path.join(root, cid, "reports")))
    check("Menu 22 正常結束並產生報告", st["status"] == "ended" and any(x.endswith(".xlsx") for x in reps), str(reps))
    regen(rt, root, cid)
    reps2 = sorted(x for x in os.listdir(os.path.join(root, cid, "reports")) if x.endswith(".xlsx")
                   and ".partial" not in x)
    from openpyxl import load_workbook
    a, b = (load_workbook(os.path.join(root, cid, "reports", x), read_only=True) for x in reps2[:2])
    same_names = a.sheetnames == b.sheetnames
    diff = []
    for name in a.sheetnames:
        ra = list(a[name].iter_rows(values_only=True))
        rb = list(b[name].iter_rows(values_only=True))
        if len(ra) != len(rb):
            diff.append(f"{name}:列數 {len(ra)}≠{len(rb)}")
            continue
        for i, (x, y) in enumerate(zip(ra, rb)):
            if x != y:
                txt = " ".join(str(v) for v in x + y if v is not None)
                if "_01" in txt or "_02" in txt or "產生" in txt or "報告編號" in txt:
                    continue                            # 報告編號／產生時間本來就不同
                diff.append(f"{name}!{i + 1}")
                break
    check(f"Menu 22 報告與 Menu 25 重產內容一致（{len(reps2)} 份；工作表 {len(a.sheetnames)}）",
          len(reps2) == 2 and same_names and not diff, "；".join(diff[:5]))
    after2 = retained(rt)
    check("Menu 25（已結束）走離線重建 → 不把舊資料重新掛回主 runtime", after2["tpl"] == 0 and after2["cache"] is False
          and alive(refs) == 0, str(after2))


# =============================================================================================== O2-4
def t_o24():
    print("\n== O2-4 Menu 25（進行中認證）沿用快取，不清除 ==")
    rt, root = new_runtime("o24")
    cid = start(rt, root)
    preload(rt, 3000)
    res = rt.analysis()
    regen(rt, root, cid)
    r = retained(rt)
    reps = [x for x in os.listdir(os.path.join(root, cid, "reports")) if x.endswith(".xlsx")]
    check("進行中 Menu 25：產生 _中途 報告", len(reps) == 1 and "_中途" in reps[0], str(reps))
    check("進行中 Menu 25 後：範本與快取仍在、仍是同一份結果（不重算）",
          r["tpl"] == 3000 and r["cache"] == 3000 and rt.analysis() is res, str(r))


# =============================================================================================== O2-5
class Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.go = threading.Event()


def t_o25():
    print("\n== O2-5 競爭：背景分析與 Menu 22 結束 ==")
    # (a) 背景分析停在「增量轉換中」（持範本鎖）→ Menu 22 結束（另一執行緒）→ 放行
    rt, root = new_runtime("o25a")
    cid = start(rt, root)
    rt._async_analysis = True
    refs = preload(rt, 3000)
    rt.analysis()
    rt.records.extend(make_records(5, datetime.now() + timedelta(seconds=10)))
    g = Gate()
    orig = CR.record_to_obs

    def slow_r2o(rec, n):
        if not g.entered.is_set():
            g.entered.set()
            g.go.wait(20)
        return orig(rec, n)
    out = {}
    CR.record_to_obs = slow_r2o
    try:
        tb = threading.Thread(target=lambda: out.__setitem__("bg", rt.bg_analysis_once()), daemon=True)
        tb.start()
        check("（前提）背景分析停在增量轉換中", g.entered.wait(5))
        te = threading.Thread(target=lambda: end(rt, root, cid), daemon=True)
        te.start()
        time.sleep(0.5)
        g.go.set()
        tb.join(30)
        te.join(60)
    finally:
        CR.record_to_obs = orig
        g.go.set()
    st = REQ.read_json(os.path.join(root, cid, "cert_state.json"))
    r = retained(rt)
    # 放行時點不同 → 丟棄原因可能是世代已變（stale／inactive）或 Menu 22 最後一筆已套用較新版本（older）；皆為「不套用」
    check("(a) 背景結果丟棄（stale／inactive／older；不套用），認證正常結束",
          out.get("bg") in ("stale", "inactive", "older") and st["status"] == "ended" and not te.is_alive(),
          str(out.get("bg")))
    check("(a) 結束後範本／快取為空、舊紀錄可回收", r["tpl"] == 0 and r["cache"] is False and alive(refs) == 0,
          f"{r}；存活 {alive(refs)}")

    # (b) 背景分析已取快照、尚未開始轉換 → Menu 22 結束 → 放行：不得把舊 list 重新掛回範本
    rt, root = new_runtime("o25b")
    cid = start(rt, root)
    rt._async_analysis = True
    refs = preload(rt, 3000)
    g = Gate()
    orig_ol = rt._obs_list

    def gated(*a, **k):
        if not g.entered.is_set():
            g.entered.set()
            g.go.wait(20)
        return orig_ol(*a, **k)
    rt._obs_list = gated
    out = {}
    tb = threading.Thread(target=lambda: out.__setitem__("bg", rt.bg_analysis_once()), daemon=True)
    tb.start()
    check("（前提）背景分析已取快照、尚未轉換", g.entered.wait(5))
    end(rt, root, cid)                                  # 結束當下的分析不經 gated（gate 已用掉）→ 不阻塞
    g.go.set()
    tb.join(30)
    del rt._obs_list
    r = retained(rt)
    check("(b) 背景結果丟棄", out.get("bg") in ("stale", "inactive"), str(out.get("bg")))
    check("(b) 舊快照的轉換沒有把舊 list 重新掛回範本，舊紀錄可回收",
          r["tpl"] == 0 and r["tpl_src"] is False and r["cache"] is False and alive(refs) == 0,
          f"{r}；存活 {alive(refs)}")


# =============================================================================================== O2-6／O2-7
def t_o267():
    print("\n== O2-6／O2-7 認證切換、續接重載 ==")
    rt, root = new_runtime("o26")
    cid1 = start(rt, root)
    refs1 = preload(rt, 4000)
    rt.analysis()
    end(rt, root, cid1)
    cid2 = start(rt, root)
    preload(rt, 1500)
    res = rt.analysis()
    check("新認證：分析＝完整重算、只含新認證紀錄", len(res.obs) == 1500 and sig(res) == sig(full_reference(rt))
          and cid1 != cid2)
    check("新認證開始後：舊認證紀錄已回收、範本只對應新 list", alive(refs1) == 0 and rt._tpl_src is rt.records
          and len(rt._tpl) == 1500)
    # 續接重載
    with rt._lock:
        old_list = weakref.ref(rt.records[0])
        rt._load(cid2)
    r = retained(rt)
    check("續接重載（_load）：舊 list 的範本與快取已釋放", r["tpl"] == 0 and r["cache"] is False and alive([old_list]) == 0,
          str(r))
    res2 = rt.analysis()
    check("續接重載後分析＝完整重算", sig(res2) == sig(full_reference(rt)) and len(res2.obs) == 1500)


# =============================================================================================== O2-8
def t_o28():
    print("\n== O2-8 壓力：三執行緒運作中連續開始／結束認證 ==")
    errs = []
    rt, root = new_runtime("o28", log=lambda m: errs.append(m) if "例外" in m else None)
    stop = threading.Event()
    ths = rt.run(stop)
    dead = []
    reports = []
    try:
        for k in range(3):
            REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=datetime.now())
            t0 = time.time()
            while not rt.is_active() and time.time() - t0 < 10:
                time.sleep(0.05)
            cid = rt.active_id()
            with rt._lock:
                rt.worker = DummyWorker()
                refs = preload(rt, 2500)
            time.sleep(7)                               # 背景分析至少跑一輪、狀態執行緒持續輸出
            check(f"第 {k + 1} 份：背景分析已建立範本（{len(rt._tpl)} 筆）", len(rt._tpl) >= 2500)
            REQ.new_request(root, "end_cert", cert_id=cid, now=datetime.now())
            t0 = time.time()
            while rt.is_active() and time.time() - t0 < 60:
                time.sleep(0.05)
            time.sleep(0.5)
            reports.append([x for x in os.listdir(os.path.join(root, cid, "reports")) if x.endswith(".xlsx")])
            dead.append(alive(refs))
    finally:
        stop.set()
        for th in ths:
            th.join(15)
    r = retained(rt)
    check("3 份認證皆產生報告", all(len(x) == 1 for x in reports), str(reports))
    check("每份結束後舊紀錄皆可回收", dead == [0, 0, 0], str(dead))
    check("最後狀態：範本／快取為空", r["tpl"] == 0 and r["cache"] is False, str(r))
    check("執行緒沒有例外、全部已停止", not errs and not any(th.is_alive() for th in ths), "；".join(errs[:3]))


guard("O2-1／O2-2", t_o21)
guard("O2-4", t_o24)
guard("O2-5", t_o25)
guard("O2-6／O2-7", t_o267)
guard("O2-8", t_o28)
shutil.rmtree(TMP, ignore_errors=True)
n = sum(RESULTS)
print(f"\n== test_cert_o2：{n}/{len(RESULTS)} PASS ==")
sys.exit(0 if n == len(RESULTS) else 1)
