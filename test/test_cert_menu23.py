# -*- coding: utf-8 -*-
"""
認證報告 —— 選單 23「階段標記」選擇流程（離線驗證）
==================================================
選認證 → 系統分析並列出階段（名稱、起訖、SOC、目前標記）→ 選編號 → 依方向列出可用標記 → 送出。
stage_start 由程式帶入，不需手動輸入。全部使用合成資料與 Field 資料的暫存複本；不連設備、不動原始資料。

執行：python test_cert_menu23.py
"""

import glob
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cert_menu                                                    # noqa: E402
import cert_requests as REQ                                         # noqa: E402
from cert_runtime import CertRuntime, _read_jsonl                   # noqa: E402

_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def feed(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


CUR = {"C": 135.0, "D": -140.0, "I": 0.0}


def rec(t, k, soc):
    vals = {"soc": soc, "current_a": CUR[k], "power_kw": None, "rack_max_c": 40.0, "rack_min_c": 30.0,
            "cell_vmax_v": 3.4, "cell_vmin_v": 3.3, "voltage_v": 900.0, "sensor_c": None, "pcs_module_c": "60"}
    return {"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "hmi_ok": True, "values": vals, "missing": [],
            "fail_reason": "", "notes": [], "flags": {}}


def write_cert(root, cid, status, plan, t0):
    """plan：[(方向, 秒起, 秒訖, 首筆 SOC, 末筆 SOC)]，每 10 秒一筆。"""
    folder = os.path.join(root, cid)
    os.makedirs(folder)
    with open(os.path.join(folder, "observations.jsonl"), "w", encoding="utf-8") as f:
        for k, s0, s1, soc0, soc1 in plan:
            secs = list(range(s0, s1 + 1, 10))
            for s in secs:
                soc = soc0 if s == secs[0] else (soc1 if s == secs[-1] else 50.0)
                f.write(json.dumps(rec(t0 + timedelta(seconds=s), k, soc), ensure_ascii=False) + "\n")
    REQ.atomic_write_json(os.path.join(folder, "cert_state.json"),
                          {"cert_id": cid, "status": status, "test_info": {"cabinet_spec": "250KW"}})
    return folder


def n_requests(root):
    return len(glob.glob(os.path.join(root, "requests", "*", "*.json")))


def run_ui(root, answers):
    outs = []
    ui = cert_menu.UI(root, inp=feed(answers), out=outs.append, wait_sec=0.1)
    ui.mark()
    return outs


TMP = tempfile.mkdtemp(prefix="cert_m23_")
try:
    T0 = datetime(2026, 1, 5, 9, 0, 0)
    root = os.path.join(TMP, "syn")
    # 已結束：充電（SOC 0 → 30）→ 待機 2 分 → 放電（首筆 SOC 缺值 → 20）→ 待機 2 分
    END = "CERT_20260105_090000"
    write_cert(root, END, "ended", [("C", 0, 300, 0.0, 30.0), ("I", 310, 430, 30.0, 30.0),
                                    ("D", 440, 740, None, 20.0), ("I", 750, 870, 20.0, 20.0)], T0)
    # 記錄中：放電仍在進行（最後一筆仍放電）
    REC = "CERT_20260106_090000"
    write_cert(root, REC, "recording", [("D", 0, 600, 100.0, 80.0)], T0 + timedelta(days=1))
    REQ.atomic_write_json(os.path.join(root, "active.json"), {"cert_id": REC})
    rt = CertRuntime(root, reader=None, enable_shots=False, log=lambda *a: None)

    print("\n== 階段清單（唯讀分析，不需手動輸入開始時間）==")
    ui = cert_menu.UI(root, out=lambda *_: None)
    rows = ui.stage_rows(END)
    check("已結束認證可列出階段（2 個），順序依時間，名稱依方向各自計數（第1次充電、第1次放電）",
          [(r["no"], r["name"]) for r in rows] == [(1, "第1次充電"), (2, "第1次放電")])
    check("起訖時間取自分析結果（首筆觀測運行 ～ 最後觀測運行）",
          [(r["t0"], r["t1"]) for r in rows] == [(T0, T0 + timedelta(seconds=300)),
                                                (T0 + timedelta(seconds=440), T0 + timedelta(seconds=740))])
    check("SOC 起訖取自首／末筆觀測；首筆 SOC 缺值 → N/A（不猜測）",
          [(r["soc0"], r["soc1"]) for r in rows] == [("0%", "30%"), ("N/A", "20%")])
    check("stage_start 為服務端比對用的 ISO 開始時間", rows[0]["start"] == "2026-01-05T09:00:00")
    rrows = ui.stage_rows(REC)
    check("記錄中認證可列出階段，資料結束時仍運行的最後階段標示進行中",
          len(rrows) == 1 and rrows[0]["active"] and rrows[0]["name"] == "第1次放電")
    check("已結束認證的階段不標示進行中", not any(r["active"] for r in rows))

    print("\n== 選擇與送出 ==")
    n0 = n_requests(root)
    outs = run_ui(root, ["2", "1", "0"])               # 選已結束認證 → 階段標記 → 0 返回
    check("階段清單顯示名稱、起訖、SOC、狀態，並有「0. 返回」",
          any("1. 第1次充電" in o for o in outs)
          and any("09:00:00 ～ 09:05:00｜SOC 0% → 30%｜停止後 SOC 30%｜狀態：未標記" in o for o in outs)
          and any("SOC N/A → 20%｜停止後 SOC 20%" in o for o in outs) and "  0. 返回" in outs)
    check("0 返回 → 不建立請求", n_requests(root) == n0)
    run_ui(root, ["2", "1", "9"])
    check("不存在的階段編號 → 不建立請求", n_requests(root) == n0)
    outs = run_ui(root, ["2", "1", "1", "0"])
    check("充電階段的標記選項：滿充／未滿／中止（不提供滿放）；選 0 返回 → 不建立請求",
          "  1. 滿充" in outs and "  2. 未滿" in outs and "  3. 中止" in outs
          and not any("滿放" in o for o in outs) and n_requests(root) == n0)
    outs = run_ui(root, ["2", "1", "2", "7"])
    check("放電階段的標記選項：滿放／未滿／中止（不提供滿充）；不存在的選項 → 不建立請求",
          "  1. 滿放" in outs and not any("滿充" in o for o in outs) and n_requests(root) == n0)

    run_ui(root, ["2", "1", "1", "1"])                 # 第1次充電 → 滿充
    rt.process_requests()
    dec = _read_jsonl(os.path.join(root, END, "decisions.jsonl"))
    check("選階段 1 → 程式自動帶入 stage_start（2026-01-05T09:00:00）、標記滿充，寫入 decisions.jsonl",
          len(dec) == 1 and dec[0]["stage_start"] == "2026-01-05T09:00:00" and dec[0]["label"] == "滿充")
    outs = run_ui(root, ["2", "1", "0"])
    check("已有標記 → 清單顯示「第1次充電（目前：滿充）」", any("1. 第1次充電（目前：滿充）" in o for o in outs))
    run_ui(root, ["2", "1", "1", "2"])                 # 重新標記：滿充 → 未滿
    rt.process_requests()
    dec = _read_jsonl(os.path.join(root, END, "decisions.jsonl"))
    check("同一階段重新標記（滿充 → 未滿）：舊紀錄保留（append-only），以最後一筆為準",
          [d["label"] for d in dec] == ["滿充", "未滿"] and ui.stage_rows(END)[0]["label"] == "未滿")

    rid = REQ.new_request(root, "stage_label", {"stage_start": rows[0]["start"], "label": "滿放"}, cert_id=END)
    rt.process_requests()
    check("服務端方向防呆仍有效（繞過選單直接送「充電階段標滿放」→ 拒絕、不寫入）",
          REQ.read_result(root, rid)["status"] == "rejected"
          and "此階段為充電階段，不可標記為滿放" in REQ.read_result(root, rid)["message"]
          and len(_read_jsonl(os.path.join(root, END, "decisions.jsonl"))) == 2)

    outs = run_ui(root, ["1", "1", "1", "1"])          # 記錄中認證 → 進行中的階段 → 只提供中止
    rt.process_requests()
    dr = _read_jsonl(os.path.join(root, REC, "decisions.jsonl"))
    check("進行中的階段只提供「中止」（不提供滿充／滿放／未滿），可送出並寫入",
          "  1. 中止" in outs and not any(o in outs for o in ("  1. 滿放", "  2. 未滿"))
          and len(dr) == 1 and dr[0]["label"] == "中止")

    print("\n== 服務端第二層防呆：進行中階段只允許中止＋方向防呆（繞過選單直接送請求）==")
    root_g = os.path.join(TMP, "guard")
    TG = T0 + timedelta(days=10)
    certs = {"ACT_C": ("CERT_20260115_090000", "recording", [("C", 0, 300, 0.0, 30.0)]),
             "ACT_D": ("CERT_20260116_090000", "recording", [("D", 0, 300, 100.0, 80.0)]),
             "END": ("CERT_20260117_090000", "ended", [("C", 0, 300, 0.0, 30.0), ("I", 310, 430, 30.0, 30.0),
                                                      ("D", 440, 740, 100.0, 20.0), ("I", 750, 870, 20.0, 20.0)]),
             "TRUNC": ("CERT_20260118_090000", "ended", [("D", 0, 300, 100.0, 80.0)])}   # 結束時仍運行（data_end）
    for i, (cid_, stt, plan) in enumerate(certs.values()):
        write_cert(root_g, cid_, stt, plan, TG + timedelta(days=i))
    rtg = CertRuntime(root_g, reader=None, enable_shots=False, log=lambda *a: None)
    ug = cert_menu.UI(root_g, out=lambda *_: None)

    def label(key, stage_idx, w):
        cid_ = certs[key][0]
        before = len(_read_jsonl(os.path.join(root_g, cid_, "decisions.jsonl")))
        rid_ = REQ.new_request(root_g, "stage_label", {"stage_start": ug.stage_rows(cid_)[stage_idx]["start"],
                                                       "label": w}, cert_id=cid_)
        rtg.process_requests()
        r_ = REQ.read_result(root_g, rid_)
        dec_ = _read_jsonl(os.path.join(root_g, cid_, "decisions.jsonl"))
        wrote = len(dec_) == before + 1 and dec_[-1]["label"] == w
        return r_["status"], r_["message"], wrote, len(dec_) == before

    IN_PROG = "此階段仍在進行中，滿充／滿放／未滿請待階段結束後再標記"
    matrix = [("ACT_C", 0, "滿充", "rejected", IN_PROG), ("ACT_C", 0, "未滿", "rejected", IN_PROG),
              ("ACT_C", 0, "滿放", "rejected", "此階段為充電階段，不可標記為滿放"), ("ACT_C", 0, "中止", "done", ""),
              ("ACT_D", 0, "滿放", "rejected", IN_PROG), ("ACT_D", 0, "未滿", "rejected", IN_PROG),
              ("ACT_D", 0, "滿充", "rejected", "此階段為放電階段，不可標記為滿充"), ("ACT_D", 0, "中止", "done", ""),
              ("END", 0, "滿充", "done", ""), ("END", 0, "未滿", "done", ""), ("END", 0, "中止", "done", ""),
              ("END", 0, "滿放", "rejected", "此階段為充電階段，不可標記為滿放"),
              ("END", 1, "滿放", "done", ""), ("END", 1, "未滿", "done", ""), ("END", 1, "中止", "done", ""),
              ("END", 1, "滿充", "rejected", "此階段為放電階段，不可標記為滿充"),
              ("TRUNC", 0, "未滿", "done", "")]
    name = {"ACT_C": "進行中充電", "ACT_D": "進行中放電", "END": "已結束", "TRUNC": "已結束認證（結束時仍運行）"}
    for key, idx, w, want, msg in matrix:
        stt, m_, wrote, unchanged = label(key, idx, w)
        dname = {"C": "充電", "D": "放電"}[ug.stage_rows(certs[key][0])[idx]["dir"]]
        ok_ = stt == want and (wrote if want == "done" else (unchanged and msg in m_))
        check(f"{name[key]}{dname if key in ('END', 'TRUNC') else ''}階段 ＋ {w} → "
              f"{'寫入' if want == 'done' else '拒絕（不寫 decisions.jsonl）'}" + (f"：{msg}" if msg else ""), ok_)
    check("選單與服務端「進行中」定義一致：進行中階段 ↔ 選單標示進行中；結束時仍運行的已結束認證不算進行中",
          ug.stage_rows(certs["ACT_C"][0])[0]["active"] and ug.stage_rows(certs["ACT_D"][0])[0]["active"]
          and not ug.stage_rows(certs["TRUNC"][0])[0]["active"]
          and not any(r["active"] for r in ug.stage_rows(certs["END"][0])))

    print("\n== 停止後 SOC（st.end_B 實際讀值，只供參考）==")
    root_p = os.path.join(TMP, "post")
    TP = T0 + timedelta(days=20)
    POST = "CERT_20260125_090000"
    write_cert(root_p, POST, "ended", [("D", 0, 300, 100.0, 1.0), ("I", 310, 430, 0.0, 0.0),
                                       ("C", 440, 740, 0.0, 99.0), ("I", 750, 870, 100.0, 100.0),
                                       ("C", 880, 1180, 0.0, 65.0), ("I", 1190, 1310, 65.0, 65.0),
                                       ("D", 1320, 1620, 100.0, 50.0), ("I", 1630, 1750, None, 40.0)], TP)
    up = cert_menu.UI(root_p, out=lambda *_: None)
    pr = up.stage_rows(POST)
    check("放電 100→1、停止後首筆 0 → 停止後 SOC 0%（運行範圍仍為 100% → 1%）",
          (pr[0]["soc0"], pr[0]["soc1"], pr[0]["soc_post"]) == ("100%", "1%", "0%"))
    check("充電 0→99、停止後首筆 100 → 停止後 SOC 100%", (pr[1]["soc1"], pr[1]["soc_post"]) == ("99%", "100%"))
    check("部分充電 0→65、停止後首筆 65 → 停止後 SOC 65%", (pr[2]["soc1"], pr[2]["soc_post"]) == ("65%", "65%"))
    check("停止後首筆 SOC 無效 → N/A（不往後找、不依方向推測 0%）",
          pr[3]["soc_post"] == "N/A" and pr[3]["soc1"] == "50%")
    tr = ug.stage_rows(certs["TRUNC"][0])[0]
    check("已結束認證＋結束時仍運行（沒有停止後首筆待機）→ 停止後 SOC N/A（不以最後運行值代替）",
          tr["soc_post"] == "N/A" and tr["soc1"] == "80%" and not tr["active"])
    outs = run_ui(root_g, [str(sorted(c[0] for c in certs.values())[::-1].index(certs["ACT_D"][0]) + 1), "1", "0"])
    check("進行中的階段：不顯示停止後 SOC，只顯示 SOC 起訖與「狀態：進行中」",
          any("SOC 100% → 80%｜狀態：進行中" in o for o in outs) and not any("停止後" in o for o in outs))
    from cert_report_xlsx import summary_titles
    rp_ = CertRuntime(root_p, reader=None, enable_shots=False, log=lambda *a: None)._offline(
        os.path.join(root_p, POST)).analysis()
    check("停止後 SOC 0%／100% 不會自動變成滿放／滿充（沒有人工標記 → 放電／充電）",
          list(summary_titles(rp_.stages, {"cabinet_spec": "250KW"}).values())
          == ["250KW 第一次放電", "250KW 第一次充電", "250KW 第二次充電", "250KW 第二次放電"])

    print("\n== 不確定項目確認（原流程不變）==")
    REQ.atomic_write_json(os.path.join(root, "status.json"),
                          {"cert_id": REC, "uncertain": [{"item_id": "U-TEST", "stage_no": 1, "A": "a", "R": "r"}]})
    n1 = n_requests(root)
    outs = run_ui(root, ["1", "2", "U-TEST", "1", "現場確認"])
    pend = [json.load(open(p_, encoding="utf-8")) for p_ in glob.glob(os.path.join(root, "requests", "pending", "*.json"))]
    check("選 2 → 列出不確定項目、輸入 ID 與 merge／split → 建立 confirm_item 請求（流程與原本相同）",
          any("U-TEST" in o for o in outs) and n_requests(root) == n1 + 1
          and any(q["type"] == "confirm_item" and q["payload"] == {"item_id": "U-TEST", "choice": "merge",
                                                                   "note": "現場確認"} for q in pend))

    print("\n== Field 資料 CERT_20261029_082805（暫存複本；不動原始資料）==")
    real = os.path.join(HERE, "..", "output", "cert_reports", "CERT_20261029_082805")
    if os.path.isdir(real):
        root_f = os.path.join(TMP, "field")
        shutil.copytree(real, os.path.join(root_f, "CERT_20261029_082805"), ignore=shutil.ignore_patterns("reports", "decisions.jsonl"))   # 從未標記狀態開始
        uf = cert_menu.UI(root_f, out=lambda *_: None)
        fr = uf.stage_rows("CERT_20261029_082805")
        check("082805：4 個階段 第1次放電／第1次充電／第2次放電／第2次充電，起始 SOC 100／0／100／0",
              [(r["name"], r["soc0"]) for r in fr] == [("第1次放電", "100%"), ("第1次充電", "0%"),
                                                       ("第2次放電", "100%"), ("第2次充電", "0%")])
        check("082805：結束 SOC 1／99／1／65", [r["soc1"] for r in fr] == ["1%", "99%", "1%", "65%"])
        outs = run_ui(root_f, ["1", "1", "0"])
        check("082805 清單：100%→1%｜停止後 0%、0%→99%｜停止後 100%、100%→1%｜停止後 0%、0%→65%｜停止後 65%",
              all(any(s in o for o in outs) for s in (
                  "08:34:30 ～ 10:37:28｜SOC 100% → 1%｜停止後 SOC 0%｜",
                  "10:39:05 ～ 12:42:57｜SOC 0% → 99%｜停止後 SOC 100%｜",
                  "14:39:01 ～ 16:45:17｜SOC 100% → 1%｜停止後 SOC 0%｜",
                  "16:49:07 ～ 18:05:07｜SOC 0% → 65%｜停止後 SOC 65%｜")))
        from cert_stage_engine import stage_minutes
        resf = CertRuntime(root_f, reader=None, enable_shots=False, log=lambda *a: None)._offline(
            os.path.join(root_f, "CERT_20261029_082805")).analysis()
        hm = lambda t: t.strftime("%H:%M:%S")
        check("082805 階段邊界不變：最後觀測運行 A／結束後首筆待機 end_B／end_by",
              [(hm(s.A.t), hm(s.end_B.t), s.end_by) for s in resf.stages]
              == [("10:37:28", "10:37:39", "idle_confirmed"), ("12:42:57", "12:43:07", "idle_confirmed"),
                  ("16:45:17", "16:45:28", "idle_confirmed"), ("18:05:07", "18:05:29", "idle_confirmed")])
        lm = stage_minutes(resf, resf.stages[0])[-1]
        check("082805 階段頁資料範圍不變：第1次放電最後一列 10:37（取 10:36:56、SOC 1），不延伸到待機",
              lm.T.strftime("%H:%M") == "10:37" and hm(lm.obs.t) == "10:36:56" and lm.obs.values["soc"] == 1.0)
        outs = run_ui(root_f, ["1", "1", "3", "1"])    # 選認證 → 階段標記 → 階段 3 → 滿放
        rtf = CertRuntime(root_f, reader=None, enable_shots=False, log=lambda *a: None)
        rtf.process_requests()
        df = _read_jsonl(os.path.join(root_f, "CERT_20261029_082805", "decisions.jsonl"))
        check("選階段 3 → 自動帶入 stage_start 2026-10-29T14:39:01、標記滿放（不需手動輸入時間）",
              len(df) == 1 and df[0]["stage_start"] == "2026-10-29T14:39:01" and df[0]["label"] == "滿放")
        ok_, _m, path_ = rtf.generate_report("CERT_20261029_082805")
        from openpyxl import load_workbook
        wb = load_workbook(path_)
        title = lambda ch: "".join(r_.t for p_ in ch.title.tx.rich.p for r_ in (p_.r or []))
        check("離線重產：Summary 標題 250KW 第一次放電／第一次充電／第二次滿放／第二次充電",
              ok_ and [title(c) for c in wb["Summary"]._charts][:4]
              == ["250KW 第一次放電", "250KW 第一次充電", "250KW 第二次滿放", "250KW 第二次充電"])
    else:
        print("  （略過：本機沒有 CERT_20261029_082805）")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== 認證報告 選單 23 階段標記 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
