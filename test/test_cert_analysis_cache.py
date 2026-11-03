# -*- coding: utf-8 -*-
"""
認證 Runtime Analysis Cache 測試（離線）
=======================================
不連設備、不開網頁、不送任何控制、不碰正式服務：模擬時鐘＋模擬讀值＋假截圖（一律失敗，不需圖檔）。
驗證：
  - 等價：Cache ON 與 Cache OFF 兩份相同模擬，逐輪 status.json、逐欄 analysis 結果、報告內容完全相同
  - 命中：輸入沒變 → 只有第一次重算（record_to_obs／strptime／analyze 都不再執行）
  - 失效：新 observation／新決策（Menu 23/24）／新指令／參數變更／結束後開始新認證 → 立即重算
  - 多執行緒：取樣／status／選單請求／指令快照同時進行 → 無例外、無死結、無舊結果、無半成品
  - 重啟：新 Runtime 從檔案重建（不保存 Cache），結果與重啟前相同
  - 計時：cert_timing.jsonl 有 Cache 欄位；cert_timing_report.py 新舊 timing 都能分析
執行：python test_cert_analysis_cache.py
"""

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from openpyxl import load_workbook

import cert_requests as REQ
import cert_runtime as CR
from cert_runtime import CertRuntime, _read_jsonl
from cert_screenshot import ShotError, shot_params

_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


TMP = tempfile.mkdtemp(prefix="cert_cache_")
T0 = datetime(2026, 10, 27, 10, 0, 0)
SP = shot_params(SHOT_ATTEMPTS=1, SHOT_RETRY_DELAY_SEC=0, SHOT_ATTEMPT_HARD_TIMEOUT_SEC=10)
# 秒數區間 → 模式：放電（中間讀取失敗＝連續性不確定、短待機＝停頓）→ 長待機 → 充電 → 待機
PLAN = [(60, 400, "D"), (400, 430, "FAIL"), (430, 700, "D"), (700, 750, "I"), (750, 1000, "D"),
        (1400, 1900, "C")]


class Clock:
    def __init__(self, t0):
        self.t = t0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += timedelta(seconds=sec)


class Plant:
    def __init__(self, clock, plan):
        self.clock, self.plan, self.temp = clock, plan, 25.0

    def mode(self):
        s = (self.clock() - T0).total_seconds() % 2400          # 長時間模擬時循環同一時間表
        return next((m for a, b, m in self.plan if a <= s < b), "I")

    def read(self):
        m = self.mode()
        if m == "FAIL":
            return None
        self.temp += {"D": 0.012, "C": 0.010}.get(m, -0.004)
        cur = {"D": -140.0, "C": 135.0}.get(m, 0.0)
        return {"soc_percent": 50, "battery_current_a": cur, "actual_active_power_kw": cur * 1.2,
                "rack_max_temperature_c": round(self.temp, 1), "rack_min_temperature_c": round(self.temp - 8, 1),
                "battery_voltage_v": 1250.0, "_fail": [], "communication_ok": True,
                "rack_cell_max_voltage_v": 3.4015, "rack_cell_min_voltage_v": 3.3852,
                "battery_power_status": "已上電", "pcs_fault_flag": False, "pcs_running_flag": m in ("C", "D"),
                "pcs_standby_flag": False, "pcs_charging_flag": m == "C", "pcs_discharging_flag": m == "D"}

    def heat(self):
        if self.mode() == "FAIL":
            raise OSError("模擬：6161 無回應")
        return {"st": 22.5, "pt": {"moduleTemperature": 40}}


def no_shot(target, out_png, p, procs=None):
    raise ShotError("模擬：截圖一律失敗（本測試不驗截圖內容）")


def runtime(root, clock, plant, cmd_file, cache=True):
    rt = CertRuntime(root, reader=plant.read, heat_reader=plant.heat, cmd_file=cmd_file, shot_params=SP,
                     capturer=no_shot, enable_shots=True, clock=clock, log=lambda *a: None)
    rt.analysis_cache_enabled = cache
    return rt


def req(root, clock, rtype, payload=None, cert_id=None):
    return REQ.new_request(root, rtype, payload, cert_id=cert_id, now=clock())


def write_cmd(cmd_file, t, action="pcs_stop_power"):
    REQ.atomic_write_json(cmd_file, {"timestamp": t.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
                                     "power": 0, "control_success": True, "verify_success": True,
                                     "success": True, "dry_run": False, "blocked": False})


def fresh(rt):
    """同一份輸入、不經 Cache 重算（比對基準）。"""
    with rt._lock:
        keep = rt.analysis_cache_enabled, rt._analysis_cache
        rt.analysis_cache_enabled = False
        try:
            return rt.analysis()
        finally:
            rt.analysis_cache_enabled, rt._analysis_cache = keep


def _no_rid(x):
    """去掉請求 ID（每次建立請求隨機產生；兩份獨立模擬之間唯一合理的差異）。"""
    if isinstance(x, dict):
        return {k: _no_rid(v) for k, v in x.items() if k != "request_id"}
    if isinstance(x, (list, tuple)):
        return type(x)(_no_rid(v) for v in x)
    if isinstance(x, str):
        return re.sub(r"請求 [0-9A-Za-z_\-]+", "請求 <id>", x)
    return x


def diff_fields(a, b, ignore_rid=False):
    """逐欄比較兩個 Result（dataclass 深層展開）；回傳不同的欄位名。ignore_rid：兩份獨立模擬比對時忽略請求 ID。"""
    da, db = dataclasses.asdict(a), dataclasses.asdict(b)
    if ignore_rid:
        da, db = _no_rid(da), _no_rid(db)
    return [k for k in da if da[k] != db[k]]


def sheet_values(path, skip=("數據概覽", "即時熱度")):
    wb = load_workbook(path)
    return {ws.title: [tuple(c.value for c in row) for row in ws.iter_rows()]
            for ws in wb.worksheets if ws.title not in skip}


class Counter_:
    """包住 cert_runtime 內的函式，計算實際被呼叫次數（判斷是否真的重算）。"""

    def __init__(self, name):
        self.name, self.n, self.orig = name, 0, getattr(CR, name)

        def wrap(*a, **k):
            self.n += 1
            return self.orig(*a, **k)
        setattr(CR, name, wrap)

    def restore(self):
        setattr(CR, self.name, self.orig)


def simulate(name, cache):
    """完整模擬：開始 → 取樣（含決策、指令）→ Menu 22。回傳每輪 status、最終 analysis（逐欄）與報告。"""
    root = os.path.join(TMP, name)
    os.makedirs(root, exist_ok=True)
    clk = Clock(T0)
    cmd_file = os.path.join(root, "device_control_action_result.json")
    write_cmd(cmd_file, T0 - timedelta(hours=1))                # 開始前的舊指令（不應記錄）
    rt = runtime(root, clk, Plant(clk, PLAN), cmd_file, cache)
    req(root, clk, "start_cert", {"cabinet_spec": "250KW"})
    statuses = [rt.write_status()]
    rt.tick()
    cid = rt.active_id()
    while (clk() - T0).total_seconds() < 2300:
        clk.advance(10)
        s = (clk() - T0).total_seconds()
        if s == 1010:                                           # 放電停止指令（快照）
            write_cmd(cmd_file, clk() - timedelta(seconds=5))
        if s == 1450:                                           # 充電指令
            write_cmd(cmd_file, clk() - timedelta(seconds=40), "pcs_charge")
        if s == 1200:                                           # Menu 23：階段標記
            res = rt.analysis()
            req(root, clk, "stage_label", {"stage_start": CR._iso(res.stages[0].start.t), "label": "滿放"}, cert_id=cid)
        if s == 1210:                                           # Menu 24：不確定項目確認（拆分）；分開送出 → 處理順序固定
            unc = [pz for pz in rt.analysis().pauses if not pz.certain]
            if unc:
                req(root, clk, "confirm_item", {"item_id": unc[0].item_id, "choice": "split"}, cert_id=cid)
        rt.process_requests()
        rt.poll_commands()
        rt.maybe_sample()
        statuses.append(rt.write_status())
    final = fresh(rt) if not cache else rt.analysis()
    rq = req(root, clk, "end_cert", cert_id=cid)
    rt.tick()
    out = REQ.read_result(root, rq)
    return {"rt": rt, "root": root, "cid": cid, "statuses": statuses, "final": final, "end": out}


try:
    # =========================================================================
    print("\n== 等價：Cache ON 與 Cache OFF（同一份模擬）==")
    on = simulate("on", True)
    off = simulate("off", False)
    res_on, res_off = on["final"], off["final"]
    check(f"模擬內容足以涵蓋判定：{len(res_on.stages)} 個階段、停頓 {len(res_on.pauses)}（含不確定／拆分）、"
          f"指令 {len(res_on.commands or [])} 筆、決策 {len(res_on.decisions)} 筆、讀取失敗事件",
          len(res_on.stages) >= 3 and res_on.pauses and len(res_on.commands or []) == 2 and len(res_on.decisions) == 2
          and any(e[1] == "讀取失敗" for e in res_on.events))
    d = diff_fields(res_on, res_off, ignore_rid=True)
    check(f"analysis 結果逐欄相同（obs／stages／pauses／events／gaps／params／decisions／commands／warnings；"
          f"僅忽略每次隨機產生的請求 ID）{'；不同：' + str(d) if d else ''}", not d)
    check(f"逐輪 status.json 完全相同（{len(on['statuses'])} 輪；含 stages／uncertain／last_direction）",
          on["statuses"] == off["statuses"])
    check("Menu 22 結束與報告：兩份都成功，最後取樣都有寫入",
          on["end"]["status"] == off["end"]["status"] == "done" and on["end"]["data"]["report_ok"]
          and off["end"]["data"]["report_ok"]
          and len(_read_jsonl(os.path.join(on["root"], on["cid"], "observations.jsonl")))
          == len(_read_jsonl(os.path.join(off["root"], off["cid"], "observations.jsonl"))))
    sv_on, sv_off = sheet_values(on["end"]["data"]["report"]), sheet_values(off["end"]["data"]["report"])
    bad = [k for k in sv_on if sv_on[k] != sv_off.get(k)]
    check(f"報告各頁內容相同（{'、'.join(sv_on)}；截圖頁除外）{'；不同：' + str(bad) if bad else ''}",
          sv_on.keys() == sv_off.keys() and not bad)
    for f in ("observations.jsonl", "decisions.jsonl", "commands.jsonl", "events.jsonl"):
        a = _read_jsonl(os.path.join(on["root"], on["cid"], f))
        b = _read_jsonl(os.path.join(off["root"], off["cid"], f))
        a, b = _no_rid(a), _no_rid(b)
        if f == "events.jsonl":
            a = [x for x in a if not str(x.get("type")).startswith(("screenshot", "report"))]
            b = [x for x in b if not str(x.get("type")).startswith(("screenshot", "report"))]
        check(f"{f} 內容相同（{len(a)} 筆）", a == b)

    tim = _read_jsonl(os.path.join(on["root"], on["cid"], CR.TIMING_FILE))
    sh = sum(x.get("status_cache_hits") or 0 for x in tim)
    sm = sum(x.get("status_cache_misses") or 0 for x in tim)
    wr = [x for x in tim if x.get("written")]
    check(f"計時欄位：每筆有 analysis_cache_hit／status_cache_hits／status_cache_misses；status 命中 {sh}／重算 {sm}",
          all({"analysis_cache_hit", "status_cache_hits", "status_cache_misses"} <= set(x) for x in tim)
          and sh > 0 and sh / (sh + sm) >= 0.9)
    check("每次取樣（新 observation 之後）的 analysis 都是重算（analysis_cache_hit=False）",
          wr and all(x["analysis_cache_hit"] is False for x in wr))
    tim_off = _read_jsonl(os.path.join(off["root"], off["cid"], CR.TIMING_FILE))
    check("Cache OFF：status 全部重算（命中 0）",
          sum(x.get("status_cache_hits") or 0 for x in tim_off) == 0
          and sum(x.get("status_cache_misses") or 0 for x in tim_off) > 0)

    # =========================================================================
    print("\n== 命中與失效 ==")
    root = os.path.join(TMP, "inval")
    os.makedirs(root, exist_ok=True)
    clk = Clock(T0)
    cmd_file = os.path.join(root, "device_control_action_result.json")
    write_cmd(cmd_file, T0 - timedelta(hours=1))
    rt = runtime(root, clk, Plant(clk, PLAN), cmd_file)
    req(root, clk, "start_cert", {"cabinet_spec": "250KW"})
    rt.tick()
    cid = rt.active_id()
    while (clk() - T0).total_seconds() < 1100:
        clk.advance(10)
        rt.tick()

    cnt = {n: Counter_(n) for n in ("analyze", "record_to_obs", "_parse")}
    try:
        def calls():
            return {n: c.n for n, c in cnt.items()}

        def reset():
            for c in cnt.values():
                c.n = 0
        rt._analysis_cache = None
        r1 = rt.analysis()
        first = calls()
        reset()
        rs = [rt.analysis() for _ in range(5)]
        check(f"輸入沒變：第一次重算（analyze {first['analyze']}、record_to_obs {first['record_to_obs']}），"
              f"之後 5 次全部命中（analyze／record_to_obs／strptime 都 0 次）",
              first["analyze"] == 1 and first["record_to_obs"] == len(rt.records)
              and calls() == {"analyze": 0, "record_to_obs": 0, "_parse": 0}
              and all(r is r1 for r in rs) and rt._last_analysis_hit is True)
        for _ in range(3):
            rt.write_status()
        check("status.json 連續更新（無新資料）全部命中，不重算", calls()["analyze"] == 0)

        clk.advance(10)
        rec = rt.sample_once()
        r2 = rt.analysis()
        check("新 observation → 失效重算；結果包含新的一筆，且與不經 Cache 的重算逐欄相同",
              calls()["analyze"] >= 1 and r2 is not r1 and len(r2.obs) == len(rt.records)
              and r2.obs[-1].t == CR._parse(rec["t"]) and not diff_fields(r2, fresh(rt)))

        reset()
        key0 = CR._iso(r2.stages[0].start.t)
        req(root, clk, "stage_label", {"stage_start": key0, "label": "滿放"}, cert_id=cid)
        rt.process_requests()
        n_lbl = calls()["analyze"]
        r3 = rt.analysis()
        check(f"Menu 23 階段標記 → 失效：新結果的階段 1 標記＝滿放（重算 {calls()['analyze'] - n_lbl} 次）",
              r3 is not r2 and r3.stages[0].label == "滿放" and len(r3.decisions) == len(rt.decisions)
              and calls()["analyze"] - n_lbl == 1 and rt.write_status()["stages"][0]["label"] == "滿放")

        unc = [pz for pz in r3.pauses if not pz.certain]
        req(root, clk, "confirm_item", {"item_id": unc[0].item_id, "choice": "split"}, cert_id=cid)
        rt.process_requests()
        r4 = rt.analysis()
        check(f"Menu 24 不確定項目拆分 → 失效：階段數 {len(r3.stages)} → {len(r4.stages)}；status 的 uncertain 已記錄決定",
              r4 is not r3 and len(r4.stages) == len(r3.stages) + 1
              and any(u["decided"] == "split" for u in rt.write_status()["uncertain"])
              and not diff_fields(r4, fresh(rt)))

        clk.advance(10)
        write_cmd(cmd_file, clk() - timedelta(seconds=3))
        n_cmd = len(rt.commands)
        rt.poll_commands()
        r5 = rt.analysis()
        check(f"新 stop 指令 → 失效：指令 {n_cmd} → {len(r5.commands)} 筆，結果與重算相同",
              r5 is not r4 and len(r5.commands) == n_cmd + 1 and not diff_fields(r5, fresh(rt)))

        clk.advance(10)
        write_cmd(cmd_file, clk() - timedelta(seconds=3), "pcs_charge")
        rt.poll_commands()
        r5b = rt.analysis()
        check("新 charge 指令 → 失效", r5b is not r5 and len(r5b.commands) == n_cmd + 2)

        old_w = rt.p["W_PAUSE_SEC"]
        rt.p["W_PAUSE_SEC"] = 600                              # 參數原地修改（正式程式不會改；防呆）
        r6 = rt.analysis()
        rt.p["W_PAUSE_SEC"] = old_w
        r7 = rt.analysis()
        check("參數變更 → 失效；改回後再失效（結果回到與原參數重算相同）",
              r6 is not r5b and r7 is not r6 and not diff_fields(r7, fresh(rt)))

        reset()
        rt.write_status()
        rt.write_status()
        check("決策／指令之後 status 再度全部命中", calls()["analyze"] == 0)
    finally:
        for c in cnt.values():
            c.restore()

    # =========================================================================
    print("\n== 重啟（Cache 只存記憶體）==")
    before = rt.analysis()
    rtR = runtime(root, clk, Plant(clk, PLAN), cmd_file)
    check("新 Runtime 建立時沒有 Cache", rtR._analysis_cache is None)
    cid_r = rtR.startup()
    after = rtR.analysis()
    d = diff_fields(before, after)
    check(f"啟動續接後第一次分析重算，結果與重啟前逐欄相同{'；不同：' + str(d) if d else ''}",
          cid_r == cid and rtR._last_analysis_hit is False and not d)
    if rtR.worker:
        rtR.worker.close(5)
    if rt.worker:
        rt.worker.close(5)

    print("\n== 結束後開始新認證：不會沿用前一份的 Cache ==")
    rq = req(root, clk, "end_cert", cert_id=cid)
    rtR.tick()
    endr = REQ.read_result(root, rq)
    clk.advance(60)
    req(root, clk, "start_cert", {"cabinet_spec": "250KW"})
    rtR.tick()
    r_new = rtR.analysis()
    check("新認證：分析只含新認證的資料（不是上一份的結果）",
          endr["status"] == "done" and rtR.active_id() != cid and len(r_new.obs) == len(rtR.records) <= 1
          and not r_new.stages and not r_new.decisions)
    rq = req(root, clk, "end_cert", cert_id=rtR.active_id())
    rtR.tick()

    # =========================================================================
    print("\n== 多執行緒：取樣／status／選單／指令同時進行 ==")
    rootT = os.path.join(TMP, "threads")
    os.makedirs(rootT, exist_ok=True)
    clkT = Clock(T0)
    cmdT = os.path.join(rootT, "device_control_action_result.json")
    write_cmd(cmdT, T0 - timedelta(hours=1))
    rtT = runtime(rootT, clkT, Plant(clkT, PLAN), cmdT)
    req(rootT, clkT, "start_cert", {"cabinet_spec": "250KW"})
    rtT.tick()
    cidT = rtT.active_id()
    errs, stale = [], []
    stop = threading.Event()
    stats = {"samples": 0, "status": 0, "menu": 0, "cmds": 0}

    def guard(fn):
        def run():
            try:
                fn()
            except Exception as e:                         # noqa: BLE001
                errs.append(f"{fn.__name__}: {type(e).__name__}: {e}")
                stop.set()
        return run

    def sampler():
        for _ in range(300):
            if stop.is_set():
                return
            clkT.advance(10)
            rec = rtT.sample_once()
            res = rtT.analysis()                              # 剛寫入的一筆一定要在結果內（不可讀到舊 Cache）
            if rec is not None and CR._parse(rec["t"]) not in {o.t for o in res.obs}:
                stale.append(f"sample {rec['t']}")
            stats["samples"] += 1

    def status():
        while not stop.is_set():
            rtT.write_status()
            with rtT._lock:                                    # 同一把鎖內：結果必須對應當下的輸入
                res = rtT.analysis()
                if (len(res.obs) != len(rtT.records) or len(res.decisions) != len(rtT.decisions)
                        or len(res.commands or []) != sum(1 for c in rtT.commands
                                                          if c.get("cmd") and c.get("control_success") is True)):
                    stale.append(f"status n={len(rtT.records)}")
            stats["status"] += 1

    def menu():
        k = 0
        while not stop.is_set():
            res, keys = rtT._stage_keys(rtT.folder)          # 與正式 _h_stage_label 相同：不先持鎖
            if keys:
                k += 1
                req(rootT, clkT, "stage_label", {"stage_start": sorted(keys)[0], "label": "中止", "note": str(k)},
                    cert_id=cidT)
                rtT.process_requests()
                with rtT._lock:
                    if rtT.decisions and rtT.analysis().decisions[-1] is not rtT.decisions[-1]:
                        stale.append("decision")
            stats["menu"] += 1

    def commands():
        k = 0
        while not stop.is_set():
            k += 1
            write_cmd(cmdT, clkT() - timedelta(seconds=1), "pcs_stop_power" if k % 2 else "pcs_discharge")
            rtT.poll_commands()
            stats["cmds"] += 1

    ths = [threading.Thread(target=guard(f), name=f.__name__) for f in (status, menu, commands)]
    ts = threading.Thread(target=guard(sampler), name="sampler")
    for t in ths + [ts]:
        t.start()
    ts.join(120)
    stop.set()
    for t in ths:
        t.join(30)
    alive = [t.name for t in ths + [ts] if t.is_alive()]
    check(f"無例外、無死結（取樣 {stats['samples']}、status {stats['status']}、選單 {stats['menu']}、指令 {stats['cmds']} 輪）"
          + (f"；例外：{errs[:3]}" if errs else "") + (f"；未結束：{alive}" if alive else ""),
          not errs and not alive and stats["samples"] == 300 and min(stats.values()) > 0)
    check(f"無舊結果／半成品（{len(stale)} 次）{stale[:3] if stale else ''}", not stale)
    d = diff_fields(rtT.analysis(), fresh(rtT))
    check(f"併發結束後 Cache 結果與不經 Cache 的重算逐欄相同（決策 {len(rtT.decisions)}、指令 {len(rtT.commands)}）"
          f"{'；不同：' + str(d) if d else ''}", not d and rtT.decisions and rtT.commands)
    rq = req(rootT, clkT, "end_cert", cert_id=cidT)
    rtT.tick()
    check("併發後 Menu 22 正常結束並產報", REQ.read_result(rootT, rq)["status"] == "done")

    # =========================================================================
    print("\n== cert_timing_report.py：新舊 timing 都能分析 ==")
    folder = os.path.join(on["root"], on["cid"])
    p = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "cert_timing_report.py"), folder],
                       capture_output=True, text=True, encoding="utf-8")
    check("新 timing：顯示 status Cache 命中率與每次取樣 Hit／Miss，無 Traceback",
          "analysis Cache（status）: Hit" in p.stdout and "命中率" in p.stdout and "Traceback" not in p.stderr
          and "== 判定 ==" in p.stdout)
    legacy = os.path.join(TMP, "legacy", on["cid"])
    shutil.copytree(folder, legacy)
    keys = ("analysis_cache_hit", "status_cache_hits", "status_cache_misses")
    with open(os.path.join(legacy, CR.TIMING_FILE), "w", encoding="utf-8") as f:
        for x in tim:
            f.write(json.dumps({k: v for k, v in x.items() if k not in keys}, ensure_ascii=False) + "\n")
    p2 = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "cert_timing_report.py"), legacy],
                        capture_output=True, text=True, encoding="utf-8")
    check("舊 timing（無 Cache 欄位）：照常分析、標示舊版，其他判定行與新 timing 相同",
          "舊版 timing（無 Cache 欄位）" in p2.stdout and "Traceback" not in p2.stderr
          and p.stdout.split("== 判定 ==")[1] == p2.stdout.split("== 判定 ==")[1])
finally:
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== 認證 Analysis Cache {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
