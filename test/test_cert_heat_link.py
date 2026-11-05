# -*- coding: utf-8 -*-
"""
6161 連線異常提醒（A）與即時熱度截圖失敗說明（B）測試（離線）
==========================================================
不連設備：假 6161 讀取器可切換成功／逾時；時間由測試時鐘推進（5 秒一筆）。
驗證：短暫逾時不提醒、連續失敗達 60 秒只提醒一次、恢復記錄中斷時長、再次失敗另起區段、
     服務重啟（取樣中斷 → 觀測區間不完整、不虛構起點、不重複事件）、選單狀態、
     Excel 截圖失敗說明（原因／次數／觸發時間／6161 起點或「無法確認」）、舊認證離線重建相容。
執行：python test_cert_heat_link.py
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from openpyxl import load_workbook

import cert_heat_link as HL
import cert_requests as REQ
from cert_menu import UI, heat_link_text, status_line
from cert_runtime import CertRuntime, _read_jsonl

_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


T0 = datetime(2026, 11, 4, 17, 0, 0)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Heat:
    """假 6161：down=True → 逾時例外；calls 記錄呼叫次數（驗證沒有額外請求）。"""

    def __init__(self):
        self.down = False
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.down:
            raise TimeoutError("timed out")
        return {"st": 22.0, "pt": {"moduleTemperature": 40}}


def reading():
    return {"soc_percent": 50, "battery_current_a": 0.0, "actual_active_power_kw": 0.0, "rack_max_temperature_c": 30,
            "rack_min_temperature_c": 25, "battery_voltage_v": 1250.0, "rack_cell_max_voltage_v": 3.4,
            "rack_cell_min_voltage_v": 3.38, "_fail": [], "battery_power_status": "已上電", "pcs_fault_flag": False,
            "pcs_charging_flag": False, "pcs_discharging_flag": False}


def hl_events(root, cid):
    return [e for e in _read_jsonl(os.path.join(root, cid, "events.jsonl")) if e["type"].startswith("heat_link")]


def steps(rt, clk, n, sec=5):
    for _ in range(n):
        clk.t += timedelta(seconds=sec)
        rt.tick()


TMP = tempfile.mkdtemp(prefix="cert_heat_link_")
try:
    print("== A. 6161 連線異常提醒 ==")
    root = os.path.join(TMP, "a")
    clk, heat, logs = Clock(), Heat(), []
    rt = CertRuntime(root, reader=reading, heat_reader=heat, enable_shots=False, clock=clk, log=logs.append)
    REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=clk())
    rt.tick()
    cid = rt.active_id()
    steps(rt, clk, 5)
    heat.down = True
    steps(rt, clk, 1)                                   # 單次逾時
    heat.down = False
    steps(rt, clk, 3)
    st = REQ.read_status(root)
    check("1 單次逾時後恢復：不提醒、不記事件，狀態正常",
          hl_events(root, cid) == [] and st["heat_link"] == {"state": "ok"})

    heat.down = True
    steps(rt, clk, 11)                                  # 第一筆失敗到最新一筆 50 秒
    st = REQ.read_status(root)
    check("2 連續失敗不足 60 秒（50 秒）：不提醒，狀態正常",
          hl_events(root, cid) == [] and st["heat_link"]["state"] == "ok")
    first_fail = (T0 + timedelta(seconds=5 * 10)).strftime("%Y-%m-%dT%H:%M:%S")
    steps(rt, clk, 2)                                   # 60 秒
    ev = hl_events(root, cid)
    st = REQ.read_status(root)
    check(f"3 連續失敗達 60 秒：記一次 heat_link_down（起點＝第一筆失敗 {first_fail[11:]}），狀態異常",
          len(ev) == 1 and ev[0]["type"] == "heat_link_down" and first_fail.replace("T", " ") in ev[0]["detail"]
          and st["heat_link"]["state"] == "down" and st["heat_link"]["start_known"]
          and st["heat_link"]["first_fail"] == first_fail and st["heat_link"]["seconds"] == 60
          and any("6161 連續讀取失敗" in str(x) for x in logs))
    steps(rt, clk, 24)                                  # 再失敗 2 分鐘
    st = REQ.read_status(root)
    check("4 持續失敗：不重複提醒（事件仍 1 筆），狀態秒數持續更新",
          len(hl_events(root, cid)) == 1 and st["heat_link"]["seconds"] == 180)

    # 7 選單狀態
    out = []
    UI(root, out=out.append).status()
    line = status_line(root)
    check("7 Menu 26 狀態與主選單摘要顯示 6161 連線異常（秒數＋起點）",
          any("6161 連線異常" in o and "180 秒" in o and first_fail.replace("T", " ") in o for o in out)
          and line is not None and "6161 連線異常" in line)

    heat.down = False
    steps(rt, clk, 1)
    ev = hl_events(root, cid)
    up_t = clk.t.strftime("%Y-%m-%d %H:%M:%S")
    st = REQ.read_status(root)
    check(f"5a 恢復讀取：記 heat_link_up（恢復 {up_t[11:]}、中斷時長 3 分 5 秒），狀態恢復正常、選單不再提示",
          [e["type"] for e in ev] == ["heat_link_down", "heat_link_up"] and up_t in ev[1]["detail"]
          and "中斷時長 3 分 5 秒" in ev[1]["detail"] and st["heat_link"] == {"state": "ok"}
          and heat_link_text(st) is None and "6161" not in (status_line(root) or ""))
    steps(rt, clk, 2)
    heat.down = True
    steps(rt, clk, 13)
    ev = hl_events(root, cid)
    check("5b 恢復後再次失敗達 60 秒：另起新區段、再提醒一次（不同 key）",
          [e["type"] for e in ev] == ["heat_link_down", "heat_link_up", "heat_link_down"]
          and len({e["key"] for e in ev}) == 3)
    n_obs = len(_read_jsonl(os.path.join(root, cid, "observations.jsonl")))
    check(f"不新增請求、不影響取樣：6161 讀取次數＝取樣筆數（{n_obs}），observation 欄位不變",
          heat.calls == n_obs and set(_read_jsonl(os.path.join(root, cid, "observations.jsonl"))[0]) ==
          {"t", "ok", "hmi_ok", "values", "missing", "fail_reason", "notes", "flags"})

    print("\n== 6 服務重新啟動 ==")
    # 6a 快速重啟（取樣間隔 ≤ G）：由 observations 重播，事件不重複、異常狀態延續
    rt_b = CertRuntime(root, reader=reading, heat_reader=heat, enable_shots=False, clock=clk, log=logs.append)
    clk.t += timedelta(seconds=10)
    rt_b.startup()
    steps(rt_b, clk, 2)
    ev = hl_events(root, cid)
    st = REQ.read_status(root)
    check("6a 重啟後 ≤30 秒恢復取樣：重播既有紀錄恢復異常狀態（起點不變），不重複記事件",
          len(ev) == 3 and st["heat_link"]["state"] == "down" and st["heat_link"]["start_known"]
          and st["heat_link"]["first_fail"] == ev[2]["key"].split("|")[1])
    # 6b 長時間中斷（> G）：舊區段「恢復時間無法確認」；新區段起點無法確認，不虛構
    rt_c = CertRuntime(root, reader=reading, heat_reader=heat, enable_shots=False, clock=clk, log=logs.append)
    clk.t += timedelta(minutes=10)
    rt_c.startup()
    rt_c.tick()
    ev = hl_events(root, cid)
    check("6b 重啟中斷 10 分鐘：記 heat_link_gap（觀測區間不完整、恢復時間無法確認）",
          ev[-1]["type"] == "heat_link_gap" and "觀測區間不完整" in ev[-1]["detail"]
          and "恢復時間無法確認" in ev[-1]["detail"])
    steps(rt_c, clk, 12)
    ev = hl_events(root, cid)
    st = REQ.read_status(root)
    out = []
    UI(root, out=out.append).status()
    check("6c 重啟後仍失敗達 60 秒：新提醒標示「起點無法確認」（取樣中斷後首筆即失敗），不沿用重啟前起點",
          ev[-1]["type"] == "heat_link_down" and "起點無法確認" in ev[-1]["detail"]
          and st["heat_link"]["state"] == "down" and st["heat_link"]["start_known"] is False
          and any("起點無法確認" in o for o in out))
    n_ev = len(ev)
    rt_d = CertRuntime(root, reader=reading, heat_reader=heat, enable_shots=False, clock=clk, log=logs.append)
    clk.t += timedelta(seconds=5)
    rt_d.startup()
    steps(rt_d, clk, 3)
    check("6d 再重啟一次：重播不產生重複事件", len(hl_events(root, cid)) == n_ev)

    print("\n== 9 沒有可靠起點 ==")
    recs = []
    t = T0
    for i in range(20):
        recs.append({"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "notes": ["6161 讀取失敗：TimeoutError: timed out"]})
        t += timedelta(seconds=5)
    tr, evs = HL.replay(recs, 30, 60)
    runs = tr.all_runs()
    check("9a 認證第一筆就失敗：提醒標示起點無法確認；截圖說明只顯示「無法確認」",
          evs[0][0] == "heat_link_down" and "起點無法確認" in evs[0][2]
          and HL.failure_line(runs, T0 + timedelta(seconds=60), 30) == "6161 連續讀取失敗起點：無法確認")
    check("9b 截圖觸發時 6161 並未處於連續失敗（觸發在失敗區段之後很久）→「無法確認」，不推測",
          HL.failure_line(runs, T0 + timedelta(hours=1), 30) == "6161 連續讀取失敗起點：無法確認")
    ok_then_fail = [{"t": (T0 + timedelta(seconds=5 * i)).strftime("%Y-%m-%dT%H:%M:%S"), "ok": True,
                     "notes": [] if i < 3 else ["6161 讀取失敗：URLError: timed out"]} for i in range(10)]
    _tr, _ = HL.replay(ok_then_fail, 30, 60)
    check("9c 前一筆成功且連續 → 起點可靠＝第一筆失敗時間",
          HL.failure_line(_tr.all_runs(), T0 + timedelta(seconds=40), 30)
          == f"6161 連續讀取失敗起點：{(T0 + timedelta(seconds=15)):%Y-%m-%d %H:%M:%S}（認證取樣紀錄）")
    check("「6161 無資料（API 回傳空內容）」有回應 → 不算連線失敗",
          not HL.heat_failed({"notes": ["6161 無資料（API 回傳空內容）"]})
          and HL.heat_failed({"ok": False, "fail_reason": "HMI 讀取失敗：x；6161 讀取失敗：y"}))

    print("\n== 8／10 Excel 截圖失敗說明、舊認證離線重建 ==")
    # 依 CERT_20261104_090053 的情境合成：17:16 起充電、19:07:09 起 6161 逾時、19:22:01 首筆待機、19:23:18 結束
    old = os.path.join(TMP, "old")
    cid2 = "CERT_20261104_170000"
    fd = os.path.join(old, cid2)
    os.makedirs(fd)
    obs = []
    t = datetime(2026, 11, 4, 17, 0, 4)                 # 5 秒格點剛好落在 19:07:09
    end = datetime(2026, 11, 4, 19, 23, 17)
    while t <= end:
        cur = 140.0 if datetime(2026, 11, 4, 17, 16) <= t < datetime(2026, 11, 4, 19, 22, 1) else 0.0
        down = t >= datetime(2026, 11, 4, 19, 7, 9)
        obs.append({"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "hmi_ok": True,
                    "values": {"soc": 50, "current_a": cur, "power_kw": cur, "rack_max_c": 30, "rack_min_c": 25,
                               "cell_vmax_v": 3.4, "cell_vmin_v": 3.38, "voltage_v": 1250.0,
                               "sensor_c": None if down else "-", "pcs_module_c": None if down else "40"},
                    "missing": [], "fail_reason": "",
                    "notes": ["6161 讀取失敗：URLError: <urlopen error timed out>"] if down else [], "flags": {}})
        t += timedelta(seconds=9 if down else 5)
    with open(os.path.join(fd, "observations.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in obs)
    with open(os.path.join(fd, "events.jsonl"), "w", encoding="utf-8") as f:     # 舊格式事件（沒有 key）
        f.write(json.dumps({"t": "2026-11-04T17:00:00", "type": "start", "detail": "開始認證"}, ensure_ascii=False) + "\n")
    REQ.atomic_write_json(os.path.join(fd, "cert_state.json"),
                          {"cert_id": cid2, "status": "ended", "started_at": "2026-11-04T17:00:00",
                           "ended_at": "2026-11-04T19:23:18", "test_info": {"cabinet_spec": "250KW"}, "reports": []})
    ro = CertRuntime(old, reader=None, enable_shots=False, log=lambda *a: None)
    st_key = ro._offline(fd).analysis().stages[0].start.t.strftime("%Y-%m-%dT%H:%M:%S")
    fail3 = [{"n": i, "started": "x", "ended": "x", "status": "failed", "reason": "開頁失敗：net::ERR_CONNECTION_TIMED_OUT"}
             for i in (1, 2, 3)]
    man = [{"type": "job", "job_id": "J1", "kind": "candidate", "stage_no": 1, "stage_start": st_key,
            "stage_name": "第1次充電", "reason": "首筆觀測待機 B（19:22:01）", "trigger_time": "2026-11-04T19:22:01",
            "requested_at": "2026-11-04T19:22:02", "targets": ["overview", "heat"], "ref_job": None,
            "extra": {"b_time": "2026-11-04T19:22:01"}},
           {"type": "result", "job_id": "J1", "target": "overview", "status": "failed",
            "reason": "開頁失敗：net::ERR_CONNECTION_TIMED_OUT", "attempts": fail3[:1], "file": None},
           {"type": "resolve", "job_id": "J1", "as": "stage_end", "reason": "階段結束", "at": "2026-11-04T19:23:05"},
           {"type": "job", "job_id": "J2", "kind": "cert_final", "stage_no": None, "stage_start": None,
            "stage_name": "認證結束（最後狀態）", "reason": "結束認證", "trigger_time": "2026-11-04T19:23:18",
            "requested_at": "2026-11-04T19:23:18", "targets": ["overview", "heat"], "ref_job": None, "extra": {}},
           {"type": "result", "job_id": "J1", "target": "heat", "status": "failed",
            "reason": "開頁失敗：net::ERR_CONNECTION_TIMED_OUT", "attempts": fail3, "file": None},
           {"type": "result", "job_id": "J2", "target": "heat", "status": "failed",
            "reason": "開頁失敗：net::ERR_CONNECTION_TIMED_OUT", "attempts": fail3, "file": None}]
    with open(os.path.join(fd, "screenshots.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in man)
    before = {n: open(os.path.join(fd, n), "rb").read() for n in os.listdir(fd) if n.endswith(".jsonl")}
    ok, msg, p = ro.generate_report(cid2)

    def cells(xlsx, sheet):
        return [str(c.value) for row in load_workbook(xlsx)[sheet].iter_rows() for c in row if c.value]
    heat_cells = [c for c in cells(p, "即時熱度") if "【截圖失敗】" in c] if ok else []
    stage_cell = next((c for c in heat_cells if "第1次充電" in c), "")
    final_cell = next((c for c in heat_cells if "認證結束（最後狀態）" in c and "第1次充電" not in c), "")
    print("    ▸ " + stage_cell.replace("\n", "｜"))
    check("8a 第1次充電（最後階段）即時熱度失敗說明：截圖失敗、原因、嘗試 3 次、觸發時間、6161 起點 19:07:09",
          all(k in stage_cell for k in ("【截圖失敗】", "嘗試 3 次", "ERR_CONNECTION_TIMED_OUT",
                                         "截圖觸發時間：2026-11-04 19:22:01",
                                         "6161 連續讀取失敗起點：2026-11-04 19:07:09", "不使用範本")))
    check("8b 認證結束（最後狀態）失敗說明：觸發 19:23:18，同一連續失敗起點；兩個位置各自獨立、不互相覆蓋",
          "截圖觸發時間：2026-11-04 19:23:18" in final_cell and "2026-11-04 19:07:09" in final_cell
          and len(heat_cells) == 2)
    ov = " ".join(cells(p, "數據概覽")) if ok else ""
    check("8c 數據概覽（HMI 頁面）失敗只加觸發時間，不加 6161 說明",
          "截圖觸發時間：2026-11-04 19:22:01" in ov and "6161 連續讀取失敗起點" not in ov)
    after = {n: open(os.path.join(fd, n), "rb").read() for n in os.listdir(fd) if n.endswith(".jsonl")}
    check("10 舊認證（事件沒有 key、沒有 heat_link 事件）可離線重建；取樣／截圖紀錄不被修改（events 只附加 report 事件）",
          ok and all(before[n] == after[n] for n in before if n != "events.jsonl")
          and after["events.jsonl"].startswith(before["events.jsonl"])
          and not any(e["type"].startswith("heat_link") for e in _read_jsonl(os.path.join(fd, "events.jsonl"))))
    from cert_report_xlsx import _shot_for
    from cert_screenshot import load_manifest
    legacy = {"folder": fd, "records": load_manifest(fd), "note": ""}             # 舊呼叫端：沒有 heat_link
    stg = ro._offline(fd).analysis().stages[0]
    _p, txt, _f = _shot_for(legacy, stg, "heat")
    check("10b 呼叫端未提供 6161 區段（舊介面）→ 失敗說明照舊，不顯示 6161 行",
          "【截圖失敗】" in txt and "6161" not in txt and "截圖觸發時間" in txt)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== 6161 連線提醒與截圖失敗說明 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
