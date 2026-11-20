# -*- coding: utf-8 -*-
"""
H2 stage_count（不計次）＋ dec_v1 決策指紋 —— 離線驗證 SC-01～SC-10、SC-14、SC-15、DV-01～DV-04
======================================================================
完全離線：只用合成時間序列呼叫 cert_stage_engine／cert_report_xlsx；報告只寫入 tempfile 暫存目錄，
不讀寫任何 Session、output\\、登入設定，不連設備。
參數使用 cert_report_config.params()（W=60、TCONF=15、G=30）。
執行：python test_cert_stage_count.py

範圍說明：
  · SC-05「方向不符」不適用：stage_count 決策沒有方向欄位（只以 stage_start 對應階段）。
  · SC-09 只驗證報告端（Summary、階段頁、Raw Data、整體測試、截圖頁）；status／選單屬 G6。
  · SC-11（分析快取）、SC-12（Menu 23）、SC-13（request_id 去重）屬 G6，不在 H2。
  · DV-03 驗證 dec_v1 對 BOM／無效行／空白行／重複鍵的既定行為；「前 N 筆內有無效行 → UNVERIFIABLE」是 r7 驗證端責任。
"""

import copy
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from openpyxl import load_workbook

import cert_report_config as CFG
import cert_report_xlsx as RX
from cert_stage_engine import Obs, analyze, decisions_digest, decisions_keywords, stage_names, strip_count_time

P = CFG.params()
BASE = datetime(2026, 1, 5, 10, 0, 0)
REASON = "誤操作：原定執行第二次放電，誤啟動充電，約 30 秒後停止；此階段標記為中止充電，不納入充放電計次。"
META = {"conditions": ["冷氣溫度：測試", "硬體概述：測試", "測試條件：測試"],
        "source_notes": ["合成資料"], "generated_at": "2026-01-05 12:00:00",
        "cabinet_spec": "250KW", "cycles": 2}
_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


CUR = {"D": -140.0, "C": 135.0, "I": 0.0}
PWR = {"D": -165.0, "C": 160.0, "I": 0.0}      # 與電流同號（充正放負）；classify 以電流優先，不影響方向判定


def ob(t, k, base):
    """RAW_REQUIRED 全部有效（含 power_kw）→ Raw Data／階段頁有實際數值；溫度隨時間變化，列錯位可被逐格比對抓到。"""
    v = {"soc": 50, "current_a": CUR[k], "rack_max_c": 40 + (t // 10) % 7, "rack_min_c": 30 + (t // 10) % 3,
         "cell_vmax_v": 3.4, "cell_vmin_v": 3.3, "voltage_v": 1200.0 + (t // 10) % 5,
         "sensor_c": 25.0, "pcs_module_c": None, "power_kw": PWR[k]}
    return Obs(t=base + timedelta(seconds=t), values=v, src=f"syn:{t}")


def build(segs, base=BASE):
    """segs：[(方向, 秒數)]，每 10 秒一筆、連續不中斷。"""
    out, t = [], 0
    for k, dur in segs:
        for _ in range(0, dur, 10):
            out.append(ob(t, k, base))
            t += 10
    return out


def key(st):
    return st.start.t.strftime("%Y-%m-%dT%H:%M:%S")


def lab(st, label, at="2026-01-05 13:00:00"):
    return {"type": "stage_label", "stage_start": key(st), "label": label, "note": "", "at": at,
            "by": "menu", "request_id": "REQ_L_" + label}


def cnt(st_or_key, counted, reason=REASON, at="2026-01-05 13:10:00", rid="OFFLINE-T-001"):
    """st_or_key：Stage → 取其開始時間；其他任何值（字串、None、數字…）→ 原樣寫入 stage_start（建立無效／孤兒決策）。"""
    k = key(st_or_key) if hasattr(st_or_key, "start") else st_or_key
    return {"type": "stage_count", "stage_start": k, "counted": counted, "reason": reason, "note": "",
            "at": at, "by": "offline_patch", "request_id": rid}


def run(obs, decs):
    return analyze(obs, P, decs, [])


def names_of(res):
    return [stage_names(res.stages)[st.no] for st in res.stages]


def skeleton(res):
    """切分／起訖／停頓／觀測標記（stage_count 不得改變）。"""
    return ([(st.no, st.dir, st.start.t, st.A.t if st.A else None, st.end_B.t if st.end_B else None,
              st.end_by, st.end_certain, st.label, len(st.pauses)) for st in res.stages],
            [(o.seq, o.state, o.stage_no, o.direction) for o in res.obs])


def stage_sheets(wb):
    return [s for s in wb.sheetnames if s[:1].isdigit()]


def cells(ws):
    return {c for row in ws.iter_rows(values_only=True) for c in row if isinstance(c, str)}


def xmls(path, prefix=""):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()
                if n.endswith(".xml") and n.startswith(prefix)}


# 情境 A：放 → 充（滿）→ 充（誤操作 180 秒）→ 放 → 充；階段之間待機 300 秒（> W）
SEG_A = [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("C", 180), ("I", 300),
         ("D", 600), ("I", 300), ("C", 600), ("I", 300)]
OBS_A = build(SEG_A)
tmp = tempfile.mkdtemp(prefix="cert_stage_count_")
try:
    r0 = run(OBS_A, [])
    check("情境 A：5 個階段 D C C D C，全部 idle_confirmed",
          [s.dir for s in r0.stages] == ["D", "C", "C", "D", "C"]
          and all(s.end_by == "idle_confirmed" for s in r0.stages))
    S = r0.stages
    LAB = [lab(S[0], "滿放"), lab(S[1], "滿充"), lab(S[2], "中止")]

    # =========================================================================
    print("\n== SC-01 沒有 stage_count：名稱、頁籤、描述與現行相同 ==")
    r1 = run(OBS_A, LAB)
    old = {}
    c_ = {"C": 0, "D": 0}
    for st in r1.stages:                                 # 現行規則：依方向各自計數，不看任何決策
        c_[st.dir] += 1
        old[st.no] = f"第{c_[st.dir]}次{st.dir_name}"
    check("SC-01 名稱＝現行規則（第1次放電、第1次充電、第2次充電、第2次放電、第3次充電）",
          stage_names(r1.stages) == old
          and names_of(r1) == ["第1次放電", "第1次充電", "第2次充電", "第2次放電", "第3次充電"])
    check("SC-01 所有階段 counted=True、沒有歷史、沒有說明；沒有孤兒",
          all(s.counted and not s.count_history and not s.count_note and not s.count_reason for s in r1.stages)
          and r1.count_orphans == [])
    p1 = RX.save_report(r1, META, os.path.join(tmp, "sc01.xlsx"))
    wb1 = load_workbook(p1)
    check("SC-01 頁籤＝現行（含標記）",
          stage_sheets(wb1) == ["1.第1次放電(滿放)", "2.第1次充電(滿充)", "3.第2次充電(中止)", "4.第2次放電", "5.第3次充電"])
    check("SC-01 描述＝來源說明原樣（沒有附加階段計次說明）", wb1.properties.description == "合成資料")
    check("SC-01 stage_count_notes 為空", RX.stage_count_notes(r1) == [])
    check("SC-01 keywords＝dec_v1（3 筆標記）",
          wb1.properties.keywords == decisions_keywords(*decisions_digest(LAB))
          and wb1.properties.keywords.startswith("dec_v=1;dec_n=3;dec_sha256="))

    # =========================================================================
    print("\n== SC-02 中止階段 counted=false ==")
    D2 = LAB + [cnt(S[2], False)]
    r2 = run(OBS_A, D2)
    check("SC-02 名稱：中止充電（不計次）；後續充電重新編號為第2次；放電不受影響",
          names_of(r2) == ["第1次放電", "第1次充電", "中止充電（不計次）", "第2次放電", "第2次充電"])
    check("SC-02 no=3 counted=False、原因＝核准文字、歷史 1 筆",
          (not r2.stages[2].counted) and r2.stages[2].count_reason == REASON and len(r2.stages[2].count_history) == 1)
    check("SC-02 切分、起訖、停頓、觀測標記與 SC-01 完全相同", skeleton(r2) == skeleton(r1))
    check("SC-02 Stage.no 不變（1～5）", [s.no for s in r2.stages] == [1, 2, 3, 4, 5])
    p2 = RX.save_report(r2, META, os.path.join(tmp, "sc02.xlsx"))
    wb2 = load_workbook(p2)
    check("SC-02 頁籤：3.中止充電（不計次）（不附標記）、5.第2次充電",
          stage_sheets(wb2) == ["1.第1次放電(滿放)", "2.第1次充電(滿充)", "3.中止充電（不計次）", "4.第2次放電", "5.第2次充電"])
    check("SC-02 工作表數量與順序（非階段頁）與 SC-01 相同",
          [s for s in wb2.sheetnames if not s[:1].isdigit()] == [s for s in wb1.sheetnames if not s[:1].isdigit()]
          and len(wb2.sheetnames) == len(wb1.sheetnames))
    d2 = wb2.properties.description or ""
    check("SC-02 描述：原有說明在前，附加不計次原因與歷史",
          d2.startswith("合成資料\n") and "階段計次：中止充電（不計次）" in d2 and REASON in d2 and "決策 1/1" in d2)

    # 逐格比對：除名稱替換外，所有儲存格與圖表內容都必須相同
    def remap(s):
        return (s.replace("第2次充電（中止）", "\0A").replace("3.第2次充電(中止)", "\0B")
                .replace("第3次充電", "第2次充電").replace("\0A", "中止充電（不計次）").replace("\0B", "3.中止充電（不計次）"))

    diff = []
    for ws_a, ws_b in zip(wb1.worksheets, wb2.worksheets):
        ra = list(ws_a.iter_rows(values_only=True))
        rb = list(ws_b.iter_rows(values_only=True))
        if len(ra) != len(rb):
            diff.append((ws_a.title, "rows", len(ra), len(rb)))
            continue
        for i, (x, y) in enumerate(zip(ra, rb)):
            xm = tuple(remap(v) if isinstance(v, str) else v for v in x)
            if xm != y:
                diff.append((ws_a.title, i + 1))
    raw1 = list(wb1["Raw Data"].iter_rows(values_only=True))
    raw2 = list(wb2["Raw Data"].iter_rows(values_only=True))
    hdr = list(raw1[0]) if raw1 else []
    num_cols = [i for i, h in enumerate(hdr) if h in ("SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓",
                                                      "Cell 最小電壓", "總電壓", "功率 kW")]
    check(f"SC-02 前提：Raw Data 有實際資料列（SC-01 {len(raw1) - 1} 列、SC-02 {len(raw2) - 1} 列，均 > 0）",
          len(raw1) > 1 and len(raw2) > 1)
    check("SC-02 前提：Raw Data 含 8 個量測欄位，且資料列的量測值都是數值（不是 N/A）",
          len(num_cols) == 8 and all(isinstance(r[i], (int, float)) for r in raw1[1:] for i in num_cols))
    st_rows = list(wb1[stage_sheets(wb1)[2]].iter_rows(min_row=2, values_only=True))
    check("SC-02 前提：不計次階段頁（no=3）有數值資料列",
          any(isinstance(v, (int, float)) for r in st_rows for v in r[1:]))
    check("SC-02 原始量測值不變：Raw Data 8 個量測欄位逐列完全相同",
          len(raw1) == len(raw2) and all(r1[i] == r2[i] for r1, r2 in zip(raw1[1:], raw2[1:]) for i in num_cols))
    check(f"SC-02 逐格比對（名稱替換後完全相同；差異 {len(diff)}）", not diff)
    ca, cb = xmls(p1, "xl/charts/"), xmls(p2, "xl/charts/")
    check("SC-02 圖表 XML（名稱／頁籤參照替換後）完全相同",
          sorted(ca) == sorted(cb) and all(remap(ca[n]) == cb[n] for n in ca))

    # =========================================================================
    print("\n== SC-09 報告各處名稱一致 ==")
    allx2 = "".join(xmls(p2).values())
    check("SC-09 數據概覽：中止充電（不計次）結束", "中止充電（不計次）結束" in cells(wb2["數據概覽"]))
    check("SC-09 即時熱度：中止充電（不計次）結束", "中止充電（不計次）結束" in cells(wb2["即時熱度"]))
    check("SC-09 Raw Data 狀態：中止充電（不計次）", "中止充電（不計次）" in cells(wb2["Raw Data"]))
    si = hdr.index("狀態") if "狀態" in hdr else None
    n_stage3 = sum(1 for o in r2.obs if o.stage_no == 3 and RX.is_report_valid(
        o, P, {st.no: RX._stage_title(stage_names(r2.stages), st) for st in r2.stages}))
    check(f"SC-09 Raw Data 中「中止充電（不計次）」列數＝no=3 有效觀測數（{n_stage3}，> 0）",
          si is not None and n_stage3 > 0 and sum(1 for r in raw2[1:] if r[si] == "中止充電（不計次）") == n_stage3)
    check("SC-09 Raw Data 狀態欄名稱集合＝階段名稱（含標記）＋非階段狀態，沒有舊名稱",
          si is not None and {r[si] for r in raw2[1:] if isinstance(r[si], str) and ("充電" in r[si] or "放電" in r[si])}
          <= {RX._stage_title(stage_names(r2.stages), st) for st in r2.stages}
          and not any(isinstance(r[si], str) and "第3次充電" in r[si] for r in raw2[1:]))
    check("SC-09 整體測試備註：中止充電（不計次）",
          any("中止充電（不計次）" in s for s in cells(wb2["整體測試"])))
    check("SC-09 階段頁與 Summary 圖表標題：250KW 中止充電（不計次）（至少 2 處）",
          allx2.count("250KW 中止充電（不計次）") >= 2)
    check("SC-09 全活頁簿不再出現舊名稱（第3次充電／第2次充電（中止）／不計次後附標記）",
          "第3次充電" not in allx2 and "第2次充電（中止）" not in allx2 and "（不計次）（中止）" not in allx2
          and "（不計次）(中止)" not in allx2)

    # =========================================================================
    print("\n== SC-10 循環數標題 ==")
    ot = RX.overall_title(META)
    check("SC-10 overall_title 含「2個循環」，SC-01 與 SC-02 都使用同一標題",
          "2個循環" in ot and ot in "".join(xmls(p1).values()) and ot in allx2)

    # =========================================================================
    print("\n== SC-03／SC-04 恢復計次與最後一筆生效 ==")
    r3 = run(OBS_A, LAB + [cnt(S[2], False, at="2026-01-05 13:10:00"), cnt(S[2], True, at="2026-01-05 13:20:00")])
    p3 = RX.save_report(r3, META, os.path.join(tmp, "sc03.xlsx"))
    wb3 = load_workbook(p3)
    check("SC-03 false→true：名稱與 SC-01 相同", names_of(r3) == names_of(r1))
    check("SC-03 頁籤與 SC-01 相同", stage_sheets(wb3) == stage_sheets(wb1))
    check("SC-03 count_note＝已恢復計次；歷史 2 筆", r3.stages[2].counted and r3.stages[2].count_note == "已恢復計次"
          and len(r3.stages[2].count_history) == 2)
    d3 = wb3.properties.description or ""
    check("SC-03 描述只多出歷史（原有說明在前）",
          d3.startswith("合成資料\n") and "已恢復計次" in d3 and "決策 1/2" in d3 and "決策 2/2" in d3)
    r4 = run(OBS_A, LAB + [cnt(S[2], False, at="2026-01-05 13:10:00"), cnt(S[2], True, at="2026-01-05 13:20:00"),
                           cnt(S[2], False, at="2026-01-05 13:30:00", rid="OFFLINE-T-003")])
    check("SC-04 false→true→false：採最後一筆（不計次），歷史 3 筆依行序",
          (not r4.stages[2].counted) and [h["at"] for h in r4.stages[2].count_history]
          == ["2026-01-05 13:10:00", "2026-01-05 13:20:00", "2026-01-05 13:30:00"])
    check("SC-04 名稱同 SC-02", names_of(r4) == names_of(r2))

    # =========================================================================
    print("\n== SC-05 無效決策（直接寫在 jsonl）：判無效、回報、不回退 ==")
    cases = [("原因空白", LAB + [cnt(S[2], False, reason="")], "原因空白"),
             ("原因只有空白字元", LAB + [cnt(S[2], False, reason="  　 ")], "原因空白"),
             ("原因不是字串", LAB + [cnt(S[2], False, reason=None)], "原因空白"),
             ("標記不是中止（滿充）", [lab(S[2], "滿充"), cnt(S[2], False)], "不是「中止」"),
             ("沒有標記", [cnt(S[2], False)], "不是「中止」"),
             ("counted 是字串 \"false\"", LAB + [cnt(S[2], "false")], "counted 不是布林值"),
             ("counted 是 0", LAB + [cnt(S[2], 0)], "counted 不是布林值"),
             ("counted 缺欄位", LAB + [{k: v for k, v in cnt(S[2], False).items() if k != "counted"}],
              "counted 不是布林值")]
    for nm, decs, why in cases:
        rr = run(OBS_A, decs)
        st = rr.stages[2]
        check(f"SC-05 {nm} → 視為計次、說明含「{why}」、名稱不變",
              st.counted and why in st.count_note and "不回退" in st.count_note
              and names_of(rr) == names_of(r1) and len(st.count_history) == 1)
    obs_ip = build(SEG_A[:-1])                             # 最後的充電沒有待機 → data_end（進行中）
    rip0 = run(obs_ip, [])
    last = rip0.stages[-1]
    rip = run(obs_ip, [lab(last, "中止"), cnt(last, False)])
    check("SC-05 階段進行中（data_end）→ 無效、視為計次",
          last.end_by == "data_end" and rip.stages[-1].counted and "階段尚未結束" in rip.stages[-1].count_note)

    # =========================================================================
    print("\n== SC-06 開始時間對不到 ==")
    D6 = LAB + [cnt("2026-01-05T09:00:00", False), cnt(None, False)]
    r6 = run(OBS_A, D6)
    check("SC-06 前提：兩筆無效決策確實進入產品端分析（stage_start 分別為字串與 None；dec_n=5）",
          D6[3]["stage_start"] == "2026-01-05T09:00:00" and D6[4]["stage_start"] is None
          and r6.decisions == D6 and r6.dec_n == 5)
    check("SC-06 不套用：名稱與 SC-01 相同", names_of(r6) == names_of(r1))
    check("SC-06 所有階段 counted=True 且沒有歷史（孤兒不影響計次）",
          all(s.counted and not s.count_history for s in r6.stages))
    check("SC-06 孤兒 2 筆（對不到任何階段／stage_start 格式無效）",
          [o["note"] for o in r6.count_orphans] == ["stage_start 格式無效", "對不到任何階段"]
          or [o["note"] for o in r6.count_orphans] == ["對不到任何階段", "stage_start 格式無效"])
    d6 = load_workbook(RX.save_report(r6, META, os.path.join(tmp, "sc06.xlsx"))).properties.description or ""
    check("SC-06 描述回報孤兒", "對不到任何階段" in d6 and "2026-01-05T09:00:00" in d6 and "未套用" in d6)

    # =========================================================================
    print("\n== SC-07 同方向兩個不計次階段 ==")
    SEG_B = [("I", 60), ("D", 600), ("I", 300), ("C", 180), ("I", 300), ("C", 180), ("I", 300),
             ("C", 600), ("I", 300), ("D", 600), ("I", 300)]
    OBS_B = build(SEG_B)
    rb0 = run(OBS_B, [])
    B = rb0.stages
    rb = run(OBS_B, [lab(B[1], "中止"), lab(B[2], "中止"), cnt(B[1], False), cnt(B[2], False)])
    t1, t2 = B[1].start.t.strftime("%H:%M:%S"), B[2].start.t.strftime("%H:%M:%S")
    check("SC-07 情境 B：D C C C D", [s.dir for s in rb0.stages] == ["D", "C", "C", "C", "D"])
    check(f"SC-07 名稱附開始時間：中止充電（不計次，{t1}）／（不計次，{t2}）；後續第1次充電、第2次放電",
          names_of(rb) == ["第1次放電", f"中止充電（不計次，{t1}）", f"中止充電（不計次，{t2}）", "第1次充電", "第2次放電"])
    wbb = load_workbook(RX.save_report(rb, META, os.path.join(tmp, "sc07.xlsx")))
    shb = stage_sheets(wbb)
    check("SC-07 頁籤 ≤31 字、唯一、冒號改底線",
          all(len(s) <= 31 for s in shb) and len(set(shb)) == len(shb)
          and shb[1] == "2.中止充電（不計次，" + t1.replace(":", "_") + "）")
    rb1 = run(OBS_B, [lab(B[1], "中止"), cnt(B[1], False)])
    check("SC-07 只有一個不計次 → 不附時間", names_of(rb1)[1] == "中止充電（不計次）")

    # =========================================================================
    print("\n== SC-08 標記後改成未滿 → 自動恢復計次 ==")
    r8 = run(OBS_A, LAB + [cnt(S[2], False), lab(S[2], "未滿", at="2026-01-05 14:00:00")])
    check("SC-08 counted=True、說明含「未滿」、名稱回到第2次充電（未滿）",
          r8.stages[2].counted and "未滿" in r8.stages[2].count_note and names_of(r8) == names_of(r1)
          and RX._stage_title(stage_names(r8.stages), r8.stages[2]) == "第2次充電（未滿）")

    # =========================================================================
    print("\n== SC-14 最後一筆無效、前一筆有效 ==")
    r14 = run(OBS_A, LAB + [cnt(S[2], False, at="2026-01-05 13:10:00"),
                            cnt(S[2], False, reason="", at="2026-01-05 13:20:00")])
    st14 = r14.stages[2]
    check("SC-14 視為計次（不回退前一筆）、說明原因、歷史 2 筆",
          st14.counted and "原因空白" in st14.count_note and len(st14.count_history) == 2
          and names_of(r14) == names_of(r1))

    # =========================================================================
    print("\n== SC-15 跨日、時分秒相同的兩個不計次階段 ==")
    SEG_C = [("I", 60), ("D", 600), ("I", 300), ("C", 180), ("I", 300)]
    OBS_C = build(SEG_C) + build(SEG_C, BASE + timedelta(days=1))
    rc0 = run(OBS_C, [])
    C = rc0.stages
    rc = run(OBS_C, [lab(C[1], "中止"), lab(C[3], "中止"), cnt(C[1], False), cnt(C[3], False)])
    n1 = C[1].start.t.strftime("%Y-%m-%d %H:%M:%S")
    n2 = C[3].start.t.strftime("%Y-%m-%d %H:%M:%S")
    check("SC-15 情境 C：D C D C，兩個充電時分秒相同、日期不同",
          [s.dir for s in C] == ["D", "C", "D", "C"]
          and C[1].start.t.strftime("%H:%M:%S") == C[3].start.t.strftime("%H:%M:%S") and n1 != n2)
    check("SC-15 名稱使用完整日期時間",
          names_of(rc) == ["第1次放電", f"中止充電（不計次，{n1}）", "第2次放電", f"中止充電（不計次，{n2}）"])
    wbc = load_workbook(RX.save_report(rc, META, os.path.join(tmp, "sc15.xlsx")))
    shc = stage_sheets(wbc)
    check("SC-15 頁籤 ≤31 字且唯一", all(len(s) <= 31 for s in shc) and len(set(shc)) == len(shc))
    nmc = stage_names(rc.stages)
    long_name = RX._sheet_name(10, nmc, rc.stages[1])
    check("SC-15 序號 10 時超過 31 字 → 去掉時間：10.中止充電（不計次）", long_name == "10.中止充電（不計次）")
    check("SC-15 strip_count_time 只處理不計次名稱",
          strip_count_time(f"中止充電（不計次，{n1}）") == "中止充電（不計次）"
          and strip_count_time("第1次充電") == "第1次充電")
    shown = [RX._sheet_name(i, nmc, st) for i, st in enumerate(rc.stages, 10)]
    check("SC-15 序號 10～13 全部頁籤 ≤31 字且唯一", all(len(s) <= 31 for s in shown) and len(set(shown)) == len(shown))

    # =========================================================================
    print("\n== DV-01 dec_v1 黃金測試值（完整 64 字元） ==")
    check("DV-01 空清單 → e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
          decisions_digest([]) == (0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"))
    check("DV-01 [{\"b\":1,\"a\":\"中止\"},{\"type\":\"x\",\"v\":true}] → 3771fb15…c5a5",
          decisions_digest([{"b": 1, "a": "中止"}, {"type": "x", "v": True}])
          == (2, "3771fb150709bab134744b9711584b767822760d127715fb4d3567a22d3ec5a5"))
    check("DV-01 keywords 格式", decisions_keywords(0, "e" * 64) == "dec_v=1;dec_n=0;dec_sha256=" + "e" * 64)

    # =========================================================================
    print("\n== DV-02 083020 決策（逐位元組重現的暫存檔，經 _read_jsonl） ==")
    from cert_runtime import _read_jsonl
    R083020 = [
        {"type": "stage_label", "stage_start": "2026-11-20T08:31:08", "label": "滿放", "note": "",
         "at": "2026-11-20 14:41:39", "by": "menu", "request_id": "REQ_20261120_144137_e144fdbe"},
        {"type": "stage_label", "stage_start": "2026-11-20T10:34:03", "label": "滿充", "note": "",
         "at": "2026-11-20 14:42:02", "by": "menu", "request_id": "REQ_20261120_144201_2c519c56"},
        {"type": "stage_label", "stage_start": "2026-11-20T14:35:36", "label": "中止", "note": "",
         "at": "2026-11-20 14:45:15", "by": "menu", "request_id": "REQ_20261120_144514_0b6beeee"}]
    fx = os.path.join(tmp, "decisions_083020.jsonl")
    with open(fx, "wb") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\r\n" for r in R083020).encode("utf-8"))
    import hashlib
    raw = open(fx, "rb").read()
    check("DV-02 暫存檔＝原始檔（549 bytes、SHA-256 c57f1500…6ab8f8）",
          len(raw) == 549 and hashlib.sha256(raw).hexdigest()
          == "c57f1500878c3b3ff1d4caf850cebf79e13bb2ad480c27e68ba27e6f069ab6f8")
    check("DV-02 dec_n=3、dec_sha256=e626176b…2784",
          decisions_digest(_read_jsonl(fx))
          == (3, "e626176b6547626dc11876668d537f9426a0b9f4ff0a1aefbee1edc9b40a2784"))
    fl = os.path.join(tmp, "decisions_lf.jsonl")
    with open(fl, "wb") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in R083020).encode("utf-8"))
    check("DV-02 LF 與 CRLF 結果相同", decisions_digest(_read_jsonl(fl)) == decisions_digest(_read_jsonl(fx)))

    # =========================================================================
    print("\n== DV-03 BOM／無效行／空白行／重複鍵 ==")
    rec1 = {"type": "stage_label", "stage_start": "2026-01-05T10:01:00", "label": "滿放"}
    rec2 = {"type": "stage_label", "stage_start": "2026-01-05T10:20:00", "label": "滿充"}
    fb = os.path.join(tmp, "decisions_bad.jsonl")
    with open(fb, "wb") as f:
        f.write(("﻿" + json.dumps(rec1, ensure_ascii=False) + "\n\n   \n{bad json\n"
                 + json.dumps(rec2, ensure_ascii=False) + "\n" + '{"type":"x","k":1,"k":2}' + "\n").encode("utf-8"))
    got = _read_jsonl(fb)
    check("DV-03 BOM 開頭那筆、空白行、無法解析的行都略過；dec_n 只計解析成功的 2 筆",
          got == [rec2, {"type": "x", "k": 2}] and decisions_digest(got)[0] == 2)
    check("DV-03 重複鍵取最後一個值；雜湊＝規格逐筆序列化結果",
          decisions_digest(got) == decisions_digest([rec2, {"k": 2, "type": "x"}]))
    check("DV-03 dict 鍵順序不影響（sort_keys）",
          decisions_digest([{"a": 1, "b": 2}]) == decisions_digest([{"b": 2, "a": 1}]))
    check("DV-03 不做 Unicode 正規化（NFC／NFD 不同 → 雜湊不同）",
          decisions_digest([{"s": "é"}]) != decisions_digest([{"s": "é"}]))

    # =========================================================================
    print("\n== DV-04 分析快照＝雜湊快照；不修改輸入 ==")
    decs = LAB + [cnt(S[2], False)]
    snap = copy.deepcopy(decs)
    ids = [id(d) for d in decs]
    rd = run(OBS_A, decs)
    check("DV-04 輸入清單與每筆紀錄內容都未被修改", decs == snap and [id(d) for d in decs] == ids and len(decs) == 4)
    check("DV-04 Result.dec_*＝同一份決策直接計算", (rd.dec_n, rd.dec_sha256) == decisions_digest(snap))
    check("DV-04 Result.decisions 即分析所用快照（內容相同、雜湊一致）",
          rd.decisions == snap and decisions_digest(rd.decisions) == (rd.dec_n, rd.dec_sha256))
    rd.stages[2].count_history[0]["reason"] = "改動"
    check("DV-04 歷史是摘要複本（改動不影響輸入）", decs[3]["reason"] == REASON)
    kw = load_workbook(RX.save_report(rd, META, os.path.join(tmp, "dv04.xlsx"))).properties.keywords
    check("DV-04 報告 keywords＝dec_v=1;dec_n=4;dec_sha256=<同一雜湊>",
          kw == f"dec_v=1;dec_n=4;dec_sha256={rd.dec_sha256}" and re.fullmatch(r"[0-9a-f]{64}", rd.dec_sha256))
    r_none = analyze(OBS_A, P)
    check("DV-04 decisions=None → dec_n=0、空清單雜湊",
          (r_none.dec_n, r_none.dec_sha256) == (0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

ok = all(_results)
print(f"\n== H2 stage_count／dec_v1 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
