# -*- coding: utf-8 -*-
"""
報告優化：有效 Flag 為「中止」的階段全面排除（stopped_stages_excluded=v1）—— 離線驗證 EX-01～EX-30
======================================================================
完全離線：只用合成時間序列呼叫 cert_stage_engine／cert_report_xlsx；報告只寫入 tempfile 暫存目錄，
不讀寫任何 Session、output\\、登入設定，不連設備。參數使用 cert_report_config.params()。
執行：python test_cert_exclude_stopped.py

範圍：單一報告的排除規則（split_stopped／build_workbook／save_report／exclude_stopped_raw_rows）。
1 秒格點 Raw Data 的端到端（slots.jsonl → raw_second_rows）由 test_cert_merge_report 3S 的 FX 案例與實際 Session 副本驗收涵蓋；
本檔以合成每秒列驗證排除規則本身（LIVE／非 LIVE／區間外沿用／來源缺失／邊界重疊）。
"""

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

from openpyxl import Workbook, load_workbook

import cert_report_config as CFG
import cert_report_xlsx as RX
import report_display as RD
from cert_stage_engine import Obs, analyze, stage_names

P = CFG.params()
BASE = datetime(2026, 1, 5, 10, 0, 0)
META = {"conditions": ["冷氣溫度：測試", "硬體概述：測試", "測試條件：測試"],
        "source_notes": ["合成資料"], "generated_at": "2026-01-05 12:00:00",
        "cabinet_spec": "250KW", "cycles": 2}
ON = dict(META, **{RX.EXCLUDE_STOPPED_KEY: True})
REASON = "誤操作：原定執行第二次放電，誤啟動充電，約 30 秒後停止；此階段標記為中止充電，不納入充放電計次。"
_results = []


def check(label, ok, detail=""):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail and not ok else ""))


CUR = {"D": -140.0, "C": 135.0, "I": 0.0}
PWR = {"D": -165.0, "C": 160.0, "I": 0.0}


def ob(t, k, base):
    v = {"soc": 50, "current_a": CUR[k], "rack_max_c": 40 + (t // 10) % 7, "rack_min_c": 30 + (t // 10) % 3,
         "cell_vmax_v": 3.4, "cell_vmin_v": 3.3, "voltage_v": 1200.0 + (t // 10) % 5,
         "sensor_c": 25.0, "pcs_module_c": None, "power_kw": PWR[k]}
    return Obs(t=base + timedelta(seconds=t), values=v, src=f"syn:{t}")


def build(segs, base=BASE):
    out, t = [], 0
    for k, dur in segs:
        for _ in range(0, dur, 10):
            out.append(ob(t, k, base))
            t += 10
    return out


def key(st):
    return st.start.t.strftime("%Y-%m-%dT%H:%M:%S")


def lab(st, label):
    return {"type": "stage_label", "stage_start": key(st), "label": label, "note": "", "at": "2026-01-05 13:00:00",
            "by": "menu", "request_id": "REQ_L_" + label + key(st)}


def cnt(st, counted):
    return {"type": "stage_count", "stage_start": key(st), "counted": counted, "reason": REASON, "note": "",
            "at": "2026-01-05 13:10:00", "by": "offline_patch", "request_id": "OFFLINE-T-001"}


def run(obs, decs):
    return analyze(obs, P, decs, [])


def stage_sheets(wb):
    return [s for s in wb.sheetnames if s[:1].isdigit()]


def all_strings(ws):
    return [c for row in ws.iter_rows(values_only=True) for c in row if isinstance(c, str)]


def raw_rows(ws):
    return [list(r) for r in ws.iter_rows(min_row=2, values_only=True)]


def state_of(res):
    return ([(st.no, st.dir, st.start.t, st.label, st.counted, st.seq0, st.seq1) for st in res.stages],
            [(id(o), o.seq, o.t, o.stage_no, o.state, o.direction) for o in res.obs])


# 情境 A：放 → 充 → 充（中止）→ 放 → 充（083020 同型：5 個階段、第 3 階段中止）
SEG_A = [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("C", 180), ("I", 300),
         ("D", 600), ("I", 300), ("C", 600), ("I", 300)]
OBS_A = build(SEG_A)
tmp = tempfile.mkdtemp(prefix="cert_exclude_stopped_")
try:
    r0 = run(OBS_A, [])
    S = r0.stages
    check("前提：情境 A 5 個階段 D C C D C", [s.dir for s in S] == ["D", "C", "C", "D", "C"])
    LAB = [lab(S[0], "滿放"), lab(S[1], "滿充"), lab(S[2], "中止"), lab(S[3], "滿放"), lab(S[4], "滿充")]
    r1 = run(OBS_A, LAB)

    print("\n== EX-01～03 旗標與有效階段 ==")
    p_off = RX.save_report(r1, META, os.path.join(tmp, "off.xlsx"))
    w_off = load_workbook(p_off)
    check("EX-01 旗標關閉（直接呼叫 save_report）→ 舊版：5 個階段頁含中止、category 空白、描述無排除說明",
          stage_sheets(w_off) == ["1.第1次放電(滿放)", "2.第1次充電(滿充)", "3.第2次充電(中止)", "4.第2次放電(滿放)", "5.第3次充電(滿充)"]
          and not (w_off.properties.category or "") and "已排除中止階段" not in (w_off.properties.description or ""),
          str(stage_sheets(w_off)))
    kept, exc, spans = RX.split_stopped(r1, False)
    kept2, exc2, spans2 = RX.split_stopped(r1, True)
    check("EX-02 split_stopped：關閉 → 不排除；開啟 → 只排除 label＝中止（第 3 階段），區間＝該階段首筆～末筆觀測",
          len(kept) == 5 and not exc and [s.no for s in exc2] == [S[2].no] and len(kept2) == 4
          and spans2[0][0] == min(o.t for o in r1.obs if o.stage_no == S[2].no)
          and spans2[0][1] == max(o.t for o in r1.obs if o.stage_no == S[2].no))
    before = state_of(r1)
    wb_on = RX.build_workbook(r1, ON)
    p_on = RX.save_report(r1, ON, os.path.join(tmp, "on.xlsx"))
    w_on = load_workbook(p_on)
    check("EX-03 旗標開啟 → 只保留 4 個有效階段頁且重新編號（第1次放電／第1次充電／第2次放電／第2次充電）",
          stage_sheets(w_on) == ["1.第1次放電(滿放)", "2.第1次充電(滿充)", "3.第2次放電(滿放)", "4.第2次充電(滿充)"],
          str(stage_sheets(w_on)))

    print("\n== EX-04～07 Summary／截圖頁 ==")
    ws_sum = wb_on["Summary"]
    arrows = getattr(wb_on, "_cert_summary_arrows", [])
    check("EX-04 Summary：4 張階段圖＋1 張整體圖；箭頭 3 個（沒有中止圖表或空位）",
          len(ws_sum._charts) == 5 and len(arrows) == 3, f"charts={len(ws_sum._charts)} arrows={len(arrows)}")

    def span_of(ws):
        ts = [ws.cell(r, 1).value for r in range(2, ws.max_row + 1) if isinstance(ws.cell(r, 1).value, datetime)]
        return ts[0], ts[-1]
    a1, b1 = span_of(wb_on["2.第1次充電(滿充)"])
    a3, b3 = span_of(wb_on["3.第2次放電(滿放)"])
    exp_txt = f"{int((a3 - b1).total_seconds() // 60)} 分"
    check("EX-05 綠色時間箭頭依實際時間重算：第1次充電 → 第2次放電 ＝ 下一張圖首分鐘 − 上一張圖末分鐘（跨過中止區間）",
          arrows[1][3] == [exp_txt] and (a3 - b1).total_seconds() > 600, f"{arrows[1][3]} vs {exp_txt}")
    sheets_vis = ["Summary", "數據概覽", "即時熱度"] + stage_sheets(w_on)
    leak = [n for n in sheets_vis if any("中止" in s for s in all_strings(w_on[n]))]
    check("EX-06 Summary、數據概覽、即時熱度、各階段頁都沒有任何「中止」字樣", not leak, str(leak))
    ov = " ".join(all_strings(w_on["數據概覽"]) + all_strings(w_on["即時熱度"]))
    check("EX-07 數據概覽／即時熱度：依有效階段重新編號（含第2次充電、無第3次充電）",
          "第2次充電" in ov and "第3次充電" not in ov and "第2次放電" in ov)

    print("\n== EX-08～10 Raw Data（舊 Session 逐筆觀測）==")
    col = RX.RAW_COL
    ro, rn = raw_rows(w_off["Raw Data"]), raw_rows(w_on["Raw Data"])
    so = {r[col["序號"] - 1]: r for r in ro}
    sn = {r[col["序號"] - 1]: r for r in rn}
    s2 = {o.seq + 1 for o in r1.obs if o.stage_no == S[2].no}
    check("EX-08 Raw Data：中止階段觀測全部排除（排除筆數＝舊版中屬於中止階段的列數）",
          not (set(sn) & s2) and set(so) - set(sn) == set(so) & s2 and len(set(so) & s2) > 0,
          f"old={len(so)} new={len(sn)} s2_in_old={len(set(so) & s2)}")
    diff = [k for k in sn if sn[k][:col["狀態"] - 1] + sn[k][col["狀態"]:] != so[k][:col["狀態"] - 1] + so[k][col["狀態"]:]]
    renum = sum(1 for k in sn if sn[k][col["狀態"] - 1] != so[k][col["狀態"] - 1])
    check("EX-09 Raw Data：保留列的序號、時間與量測值和舊版完全相同（只有狀態欄因重新編號改名）",
          not diff and set(sn) <= set(so), f"diff={diff[:5]} renamed_status={renum}")
    check("EX-10 Raw Data：沒有「中止」狀態；舊版的第2次放電／第3次充電依新編號改為第2次放電／第2次充電",
          all("中止" not in str(r[col["狀態"] - 1]) for r in rn) and any(str(r[col["狀態"] - 1]).startswith("第2次充電") for r in rn)
          and not any(str(r[col["狀態"] - 1]).startswith("第3次充電") for r in rn))

    print("\n== EX-11～14 整體測試（方案 A）==")
    wo_, wn_ = w_off["整體測試"], w_on["整體測試"]
    t0, t1 = spans2[0][0], spans2[0][1]
    f0, f1 = t0.replace(second=0), t1.replace(second=0)
    rows_o = [[wo_.cell(r, c).value for c in range(1, RX.REMARK_COL + 1)] for r in range(2, wo_.max_row + 1)]
    rows_n = [[wn_.cell(r, c).value for c in range(1, RX.REMARK_COL + 1)] for r in range(2, wn_.max_row + 1)]
    check("EX-11 整體測試保留完整原始時間軸（分鐘列數與時間和舊版相同）",
          len(rows_o) == len(rows_n) and [r[0] for r in rows_o] == [r[0] for r in rows_n], f"{len(rows_o)} vs {len(rows_n)}")
    inside = [i for i, r in enumerate(rows_n) if f0 < r[0] < f1]
    hc = [RX.HELPER_COL0 + k for k in range(len(RX.CHART_SRC))]
    ok_in = all(all(v == CFG.NA for v in rows_n[i][1:RX.REMARK_COL - 1]) and str(rows_n[i][-1]).startswith("N/A：已排除中止階段")
                and all(wn_.cell(i + 2, c).value is None for c in hc) for i in inside)
    check("EX-12 中止區間的分鐘：量測值 N/A、備註「已排除中止階段」、圖表輔助欄空白（曲線斷開）",
          len(inside) >= 1 and ok_in, f"inside={len(inside)}")
    outside = [i for i, r in enumerate(rows_n) if r[0] < f0 or r[0] > f1]
    check("EX-13 中止區間外的分鐘（含正常待機與其他有效階段）和舊版逐格相同（狀態文字依新編號）",
          all(rows_n[i][1:RX.REMARK_COL - 1] == rows_o[i][1:RX.REMARK_COL - 1] for i in outside) and len(outside) > 50)
    ch = [c for c in wn_._charts] if hasattr(wn_, "_charts") else []
    check("EX-14 整體圖表空白值設定為 gap（不跨中止區間連線）",
          all(c.display_blanks == "gap" for c in wb_on["整體測試"]._charts) and len(wb_on["整體測試"]._charts) == 1)

    print("\n== EX-15～17 描述、格式識別、原始資料 ==")
    d_on = w_on.properties.description or ""
    check("EX-15 描述：已排除中止階段（方向、開始、結束、筆數）；category＝stopped_stages_excluded=v1；dec_v1 keywords 與舊版相同",
          f"已排除中止階段：充電，開始 {t0:%m/%d %H:%M:%S}，結束 {t1:%m/%d %H:%M:%S}" in d_on and w_on.properties.category == RX.STOPPED_FORMAT
          and w_on.properties.keywords == w_off.properties.keywords and w_on.properties.keywords)
    check("EX-16 原始 Result、Stage 與觀測物件完全不變（建立報告前後逐項相同）", state_of(r1) == before)
    w_none = RX.save_report(run(OBS_A, [lab(S[0], "滿放"), lab(S[1], "滿充")]), ON, os.path.join(tmp, "on_none.xlsx"))
    w_none_off = RX.save_report(run(OBS_A, [lab(S[0], "滿放"), lab(S[1], "滿充")]), META, os.path.join(tmp, "off_none.xlsx"))
    a, b = load_workbook(w_none), load_workbook(w_none_off)
    same = a.sheetnames == b.sheetnames and all(
        [[c for c in r] for r in a[n].iter_rows(values_only=True)] == [[c for c in r] for r in b[n].iter_rows(values_only=True)]
        for n in a.sheetnames)
    check("EX-17 旗標開啟但沒有中止階段 → 各頁內容與描述和舊版相同（只有 category 不同）",
          same and a.properties.description == b.properties.description and a.properties.category == RX.STOPPED_FORMAT
          and not b.properties.category)

    print("\n== EX-18～23 其他情境 ==")
    rm = run(OBS_A, [lab(S[0], "滿放"), lab(S[1], "滿充"), lab(S[2], "中止"), lab(S[3], "滿放"), lab(S[4], "中止")])
    wm = RX.build_workbook(rm, ON)
    check("EX-18 多個中止＋最後一個階段為中止 → 保留 第1次放電／第1次充電／第2次放電",
          stage_sheets(wm) == ["1.第1次放電(滿放)", "2.第1次充電(滿充)", "3.第2次放電(滿放)"], str(stage_sheets(wm)))
    rc = run(OBS_A, [lab(S[1], "中止"), lab(S[2], "中止")])
    wc = RX.build_workbook(rc, ON)
    check("EX-19 同方向連續兩個中止（第 2、3 階段都是充電）→ 保留 第1次放電／第2次放電／第1次充電",
          stage_sheets(wc) == ["1.第1次放電", "2.第2次放電", "3.第1次充電"], str(stage_sheets(wc)))
    SEG_F = [("D", 600), ("I", 300), ("C", 600), ("I", 300), ("D", 600), ("I", 300)]
    rf0 = run(build(SEG_F), [])
    rf = run(build(SEG_F), [lab(rf0.stages[0], "中止")])
    wf, wf_off = RX.build_workbook(rf, ON), RX.build_workbook(rf, META)
    wsf, wsf_o = wf["整體測試"], wf_off["整體測試"]
    check("EX-20 第一個階段為中止（認證從該階段開始）→ 保留 第1次充電／第1次放電；整體頁開頭補 N/A、時間軸不縮短",
          stage_sheets(wf) == ["1.第1次充電", "2.第1次放電"] and wsf.max_row == wsf_o.max_row
          and wsf.cell(2, 1).value == wsf_o.cell(2, 1).value and str(wsf.cell(2, RX.REMARK_COL).value).startswith("N/A：已排除中止階段"),
          f"{stage_sheets(wf)} rows {wsf.max_row}/{wsf_o.max_row}")
    ra = run(OBS_A, [lab(s, "中止") for s in S])
    p_all = RX.save_report(ra, ON, os.path.join(tmp, "all.xlsx"))
    wa = load_workbook(p_all)
    wa_b = RX.build_workbook(ra, ON)
    check("EX-21 全部階段為中止 → 沒有階段頁、Summary 只有整體圖、截圖頁只有結束狀態；描述明確說明沒有有效充放電階段",
          stage_sheets(wa) == [] and len(wa_b["Summary"]._charts) == 1 and not getattr(wa_b, "_cert_summary_arrows", [])
          and "本報告沒有有效充放電階段" in (wa.properties.description or "")
          and RX.FINAL_TITLE in " ".join(all_strings(wa["數據概覽"])))
    rs_ = run(OBS_A, LAB + [cnt(S[2], False)])
    ps_ = RX.save_report(rs_, ON, os.path.join(tmp, "sc.xlsx"))
    ws_ = load_workbook(ps_)
    dl = (ws_.properties.description or "").split("\n")
    cn = RX.stage_count_notes(rs_)
    check("EX-22 stage_count 不計次＋中止 Flag → 同樣排除；計次描述保留且位於描述最後，排除說明在其之前（r8 X10 順序）",
          stage_sheets(ws_) == stage_sheets(w_on) and cn and dl[-len(cn):] == cn
          and max(i for i, x in enumerate(dl) if x.startswith("已排除中止階段")) < len(dl) - len(cn))
    ru = run(OBS_A, [lab(S[0], "滿放"), lab(S[1], "滿充"), lab(S[2], "未滿")])
    check("EX-23 標記為「未滿」或未標記的階段不排除（5 個階段頁）", len(stage_sheets(RX.build_workbook(ru, ON))) == 5)

    print("\n== EX-24～25 中途報告（記錄中）==")
    SEG_R = [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("C", 180)]
    rr0 = run(build(SEG_R), [])
    last = rr0.stages[-1]
    rr = run(build(SEG_R), [lab(last, "中止")])
    check("EX-24 中途報告：進行中階段的有效 Flag 明確為中止 → 排除（保留 第1次放電／第1次充電）",
          last.end_by == "data_end" and stage_sheets(RX.build_workbook(rr, ON)) == ["1.第1次放電", "2.第1次充電"])
    check("EX-25 中途報告：進行中但未標記的階段不排除（3 個階段頁）", len(stage_sheets(RX.build_workbook(rr0, ON))) == 3)

    print("\n== EX-26～30 1 秒格點 Raw Data 排除規則（合成每秒列）==")
    s1o = [o for o in r1.obs if o.stage_no == S[1].no]
    s2o = [o for o in r1.obs if o.stage_no == S[2].no]
    s3o = [o for o in r1.obs if o.stage_no == S[3].no]
    idle = [o for o in r1.obs if o.stage_no is None and s2o[-1].t < o.t < s3o[0].t]
    sec = timedelta(seconds=1)
    R = lambda t, st, o, age=0: {"time": t, "state": st, "obs": o, "src": None if o is None else o.seq + 1, "age": age,   # noqa: E731
                                 "live": st == RD.RAW_LIVE}
    rows = ([R(s1o[-1].t, RD.RAW_LIVE, s1o[-1])]
            + [R(o.t, RD.RAW_LIVE, o) for o in s2o]
            + [R(s2o[0].t + 5 * sec, RD.RAW_CARRIED, s2o[0], 5), R(s2o[1].t + 5 * sec, RD.RAW_NA, None, None),
               R(s2o[2].t + sec, RD.RAW_LIVE, s1o[-1]),                                   # 邊界重疊：時間落在中止區間但來源屬前一有效階段
               R(s2o[-1].t + 5 * sec, RD.RAW_MISSED, s2o[-1], 5),                        # 中止結束後仍沿用中止讀值
               R(idle[0].t, RD.RAW_LIVE, idle[0]), R(idle[0].t + 5 * sec, RD.RAW_CARRIED, idle[0], 5),
               R(idle[0].t + 6 * sec, RD.RAW_NA, None, None),                            # 區間外、來源缺失
               R(s3o[0].t, RD.RAW_LIVE, s3o[0])])
    rows.sort(key=lambda r: r["time"])
    rs = {"rows": rows, "stats": {"counts": {k: 0 for k in RD.RAW_STATES}, "max_age": None}}
    snap = [(id(r), dict(r)) for r in rows]
    out = RX.exclude_stopped_raw_rows(rs, exc2, spans2)
    kept_ids = {id(r) for r in out["rows"]}
    drop_exp = len(s2o) + 2
    check("EX-26 LIVE 中止來源與中止區間內的非 LIVE 列（含來源缺失）排除；排除列數＝中止 LIVE 筆數＋2",
          out["excluded"]["rows_dropped"] == drop_exp and len(out["rows"]) == len(rows) - drop_exp,
          str(out["excluded"]))
    bl = [r for r in out["rows"] if r.get("excluded_src")]
    check("EX-27 中止結束後仍沿用中止讀值的列：保留時間列、來源觀測清空（N/A）、只有這 1 列",
          out["excluded"]["rows_blanked"] == 1 and len(bl) == 1 and bl[0]["obs"] is None and bl[0]["time"] == s2o[-1].t + 5 * sec)
    keep_exp = [r for r in rows if r["obs"] is not None and r["obs"].stage_no != S[2].no] + [rows[[i for i, r in enumerate(rows)
                                                                                              if r["time"] == idle[0].t + 6 * sec][0]]]
    check("EX-28 正常待機、下一個有效階段第一筆、邊界重疊（來源屬有效階段）與區間外來源缺失列：原物件保留不變",
          all(id(r) in kept_ids for r in keep_exp))
    check("EX-29 原始每秒列與觀測物件不被修改（排除只產生新清單與複本）",
          [(id(r), dict(r)) for r in rows] == snap and out["rows"] is not rows and out["stats"] is not rs["stats"])
    names_full = {st.no: RX._stage_title(stage_names(kept2), st) for st in kept2}
    wbx = Workbook()
    RX.write_raw_seconds(wbx, out, P, names_full)
    wx = wbx[RX.RAW_SHEET]
    xr = [r for r in wx.iter_rows(min_row=2, values_only=True) if r[0] == s2o[-1].t + 5 * sec][0]
    note = RX.raw_seconds_note(out)
    check("EX-30 沿用中止讀值的列在 Excel：狀態 N/A、數值空白、說明為排除說明（無階段名稱）；摘要註明排除列數且不再宣稱秒數連續",
          xr[1] == CFG.NA and all(v is None for v in xr[2:-1]) and xr[-1] == RX.EXCL_SRC_NOTE
          and f"已排除中止階段 {drop_exp} 列" in note and "秒數連續不重複" not in note, str(xr))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

ok = all(_results)
print(f"\n== 中止階段排除 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
