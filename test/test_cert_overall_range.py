# -*- coding: utf-8 -*-
"""
整體溫度曲線 v2（stopped_stages_excluded=v2）—— 離線驗證 OR-01～OR-21 ＋ N Cycle 動態情境 NC-01～NC-20
==========================================================================================
完全離線：只用合成時間序列呼叫 cert_stage_engine／cert_report_xlsx（以及 cert_merge_analysis.pair_session、
cert_merge_xlsx.detail_meta 的函式層交叉驗證）；報告只寫入 tempfile 暫存目錄，不讀寫任何 Session、output\\、登入設定，
不連設備。參數使用 cert_report_config.params()。
執行：python test_cert_overall_range.py

預期值盡量不由受測函式推導：
  - 有效範圍 t0／t1：由合成排程（每個階段的開始／最後運行秒數）推算；
  - 遮蔽區段：由觀測的 stage_no（該階段首筆／末筆觀測時間）推算（同 EX-02）；
  - 有效完整循環：逐案手寫預期的 Stage.no 配對；
  - 每分鐘 Rack 最大／最小：由「裁切後觀測」獨立重算；
  - 圖表：直接解析 xlsx 內的 chart XML（Reference 公式、標題、catAx／c:auto、dispBlanksAs），不是只看圖表物件是否存在；
  - 中止全面排除（方案 B）：預期列配置由 t0／t1／遮蔽區段逐分鐘獨立推導（exp_layout），時間跳越次數另以 Stage 層級
    「前後都有有效階段的連續中止段數」交叉推導（interior_runs）；不得有分隔列與任何中止說明（含活頁簿描述）。
循環數不設上限：NC 情境涵蓋 1／2／3／4／5／10 個循環（僅為測試覆蓋，非程式上限）。
"""

import os
import posixpath
import re
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from openpyxl.utils import get_column_letter

import cert_report_config as CFG
import cert_report_xlsx as RX
from cert_stage_engine import Obs, analyze

P = CFG.params()
BASE = datetime(2026, 1, 5, 10, 0, 0)
META = {"conditions": ["冷氣溫度：測試", "硬體概述：測試", "測試條件：測試"],
        "source_notes": ["合成資料"], "generated_at": "2026-01-05 12:00:00",
        "cabinet_spec": "250KW", "cycles": 2}
V1 = dict(META, **{RX.EXCLUDE_STOPPED_KEY: True})
V2 = dict(META, **{RX.EXCLUDE_STOPPED_KEY: True, RX.OVERALL_RANGE_KEY: True})
MIN = timedelta(minutes=1)
OPP = {"C": "D", "D": "C"}
LABEL_OK = {"D": "滿放", "C": "滿充"}
REASON = "測試：此階段標記為中止，不納入充放電計次。"
_results = []


def check(label, ok, detail=""):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail and not ok else ""))


# ----------------------------------------------------------------------------- 合成資料
CUR = {"D": -140.0, "C": 135.0, "I": 0.0}
PWR = {"D": -165.0, "C": 160.0, "I": 0.0}


def ob(t, k, base=BASE, spike=False):
    v = {"soc": 50, "current_a": CUR[k], "rack_max_c": 40 + (t // 10) % 7, "rack_min_c": 30 + (t // 10) % 3,
         "cell_vmax_v": 3.4, "cell_vmin_v": 3.3, "voltage_v": 1200.0 + (t // 10) % 5,
         "sensor_c": 25.0, "pcs_module_c": None, "power_kw": PWR[k]}
    if spike:                                     # 邊界外待機觀測：明顯的極值（不得進入 v2 整體曲線）
        v.update(rack_max_c=99.0, rack_min_c=1.0)
    return Obs(t=base + timedelta(seconds=t), values=v, src=f"syn:{t}")


def build(segs, base=BASE, t0=0):
    """segs＝[(方向, 秒數)]，每 10 秒一筆；回傳 (觀測, 各非待機段的 (方向, 開始秒, 最後一筆秒))。"""
    out, t, sched = [], t0, []
    for k, dur in segs:
        first = t
        for _ in range(0, dur, 10):
            out.append(ob(t, k, base))
            t += 10
        if k != "I":
            sched.append((k, first, t - 10))
    return out, sched


STAGE_S, REST_S, LEAD_S = 600, 300, 60


def cycle_segs(n, first, short=None, stage_s=STAGE_S):
    """n 個循環（第一方向 first）；short＝{Stage.no: 秒數} 讓指定階段變短（短階段情境）；stage_s＝每階段秒數。"""
    segs, no = [("I", LEAD_S)], 0
    for _ in range(n):
        for d in (first, OPP[first]):
            no += 1
            segs += [(d, (short or {}).get(no, stage_s)), ("I", REST_S)]
    return segs


def key(st):
    return st.start.t.strftime("%Y-%m-%dT%H:%M:%S")


def lab(st, label):
    return {"type": "stage_label", "stage_start": key(st), "label": label, "note": "", "at": "2026-01-05 23:00:00",
            "by": "menu", "request_id": "REQ_L_" + label + key(st)}


def cnt(st, counted):
    return {"type": "stage_count", "stage_start": key(st), "counted": counted, "reason": REASON, "note": "",
            "at": "2026-01-05 23:10:00", "by": "offline_patch", "request_id": "OFFLINE-OR-" + key(st)}


def labels(stages, stopped):
    return [lab(st, "中止" if st.no in stopped else LABEL_OK[st.dir]) for st in stages]


def state_of(res):
    return ([(st.no, st.dir, st.start.t, st.A.t, st.label, st.counted, st.seq0, st.seq1) for st in res.stages],
            [(id(o), o.seq, o.t, o.stage_no, o.state, o.direction) for o in res.obs])


def runs_of(nos):
    """依 Stage.no 相鄰合併：{2,3,5} → [[2,3],[5]]。"""
    out = []
    for n in sorted(nos):
        if out and out[-1][-1] == n - 1:
            out[-1].append(n)
        else:
            out.append([n])
    return out


def exp_title(n):
    """Legacy／v1 標題（overall_title；維持「電櫃」）。"""
    return "【250KW】電櫃充放" + (f"{n}個循環" if n else "") + "溫度曲線"


EXP_IDLE = "【250KW】獨立櫃溫度曲線"


def exp_title_v2(pairs, kept_nos):
    """v2 標題（第二次人工驗收規格；由手寫預期配對＋有效 Stage.no 獨立推導）：全部有效階段都在配對內且 ≥1 個循環 →
    獨立櫃充放N個循環；否則 → 獨立櫃充放電。"""
    paired = [n for pr in pairs for n in pr]
    if pairs and sorted(paired) == sorted(kept_nos):
        return f"【250KW】獨立櫃充放{len(pairs)}個循環溫度曲線"
    return "【250KW】獨立櫃充放電溫度曲線"


def exp_skip(n_rows, n_pairs, n_unpaired, l_max=40, l_min=12):
    """tickLblSkip 預期值（測試端依裁定公式獨立實作，不呼叫受測函式）。"""
    if n_rows <= 0:
        return None
    eq = n_pairs + n_unpaired / 2
    base = 3 if eq <= 1 else 6 if eq <= 2 else 5 * (-(-eq // 1) - 1)
    lo = max(1, -(-n_rows // l_max))
    hi = max(lo, n_rows // l_min)
    tgt = min(max(int(base), lo), hi)
    steps = [1, 2, 3, 5, 6, 10, 15, 20, 25, 30, 45, 60]
    v = 90
    while steps[-1] < 10 * max(tgt, hi):
        steps += [v, v * 4 // 3]                  # 90,120,180,240,360,480…
        v *= 2
    up = [s for s in steps if s >= tgt]
    if up and up[0] <= hi:
        return up[0]
    down = [s for s in steps if lo <= s < tgt]
    return down[-1] if down else tgt


def floor_m(t):
    return t.replace(second=0, microsecond=0)


# ----------------------------------------------------------------------------- 工作表／圖表讀取
HELPER = [RX.HELPER_COL0 + k for k in range(len(RX.CHART_SRC))]
VAL_COLS = list(range(2, RX.REMARK_COL + 1))           # B～備註（LOG 欄位＋備註）


def sheet_rows(ws):
    """整體測試頁：[(時間, [B～備註], [輔助欄×3])]。"""
    out = []
    for r in range(2, ws.max_row + 1):
        t = ws.cell(r, 1).value
        if t is None:
            continue
        out.append((t, [ws.cell(r, c).value for c in VAL_COLS], [ws.cell(r, c).value for c in HELPER]))
    return out


def all_rows(ws):
    """整體測試頁全部資料列（含分隔列）：[(時間或 None, [B～備註], [輔助欄×3])]。"""
    return [(ws.cell(r, 1).value, [ws.cell(r, c).value for c in VAL_COLS], [ws.cell(r, c).value for c in HELPER])
            for r in range(2, ws.max_row + 1)]


def exp_layout(t0, t1, exp_masks):
    """
    v2 預期列配置（方案 B：中止全面排除；由合成排程推得的 t0／t1 與由觀測 stage_no 推得的遮蔽區段獨立推導，不呼叫受測函式）：
    floor(t0)～floor(t1) 逐分鐘中、未與任何遮蔽區段重疊的分鐘（不保留遮蔽分鐘、沒有分隔列）。
    （歷史：第一次人工驗收版本在每段中間遮蔽保留 1 列分隔列 → 使用者最終裁定方案 B 移除。）
    """
    out = []
    M = floor_m(t0)
    while M <= floor_m(t1):
        if not any(M <= b and M + MIN > a for a, b, _run in exp_masks):
            out.append(M)
        M += MIN
    return out


def masked_m(M, exp_masks):
    return any(M <= b and M + MIN > a for a, b, _run in exp_masks)


def interior_runs(stopped, kept_nos):
    """Stage 層級獨立推導：前後都有有效階段（依原始 Stage.no）的連續中止段數＝預期時間跳越次數（不經分鐘運算）。"""
    if not kept_nos:
        return 0
    return sum(1 for run in runs_of(stopped) if min(kept_nos) < run[0] and run[-1] < max(kept_nos))


def sheet_values(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
      "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
      "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def _rels(z, part):
    d, b = posixpath.split(part)
    rp = posixpath.join(d, "_rels", b + ".rels")
    if rp not in z.namelist():
        return {}
    root = ET.fromstring(z.read(rp))
    out = {}
    for e in root.findall("pr:Relationship", NS):
        tg = e.get("Target")
        out[e.get("Id")] = tg.lstrip("/") if tg.startswith("/") else posixpath.normpath(posixpath.join(d, tg))
    return out


def chart_map(path):
    """xlsx → {工作表名: [{"title": 文字, "series": [{"tx", "cat", "val"}]}]}（直接解析 chart XML）。"""
    out = {}
    with zipfile.ZipFile(path) as z:
        wb_rels = _rels(z, "xl/workbook.xml")
        for sh in ET.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
            part = wb_rels[sh.get("{%s}id" % NS["r"])]
            charts = []
            dr = ET.fromstring(z.read(part)).find("m:drawing", NS)
            if dr is not None:
                dpart = _rels(z, part)[dr.get("{%s}id" % NS["r"])]
                drels = _rels(z, dpart)
                for cref in ET.fromstring(z.read(dpart)).iter("{%s}chart" % NS["c"]):
                    cx = ET.fromstring(z.read(drels[cref.get("{%s}id" % NS["r"])]))
                    t = cx.find(".//c:chart/c:title", NS)
                    title = "".join(x.text or "" for x in t.iter("{%s}t" % NS["a"])) if t is not None else None
                    ser = []
                    for s in cx.iter("{%s}ser" % NS["c"]):
                        f = lambda q: (s.find(q, NS).text if s.find(q, NS) is not None else None)   # noqa: E731
                        ser.append({"tx": f("c:tx/c:strRef/c:f"), "cat": f("c:cat/c:numRef/c:f") or f("c:cat/c:strRef/c:f"),
                                    "val": f("c:val/c:numRef/c:f")})
                    cat_ax = cx.find(".//c:plotArea/c:catAx", NS)
                    auto = cat_ax.find("c:auto", NS) if cat_ax is not None else None
                    db = cx.find(".//c:chart/c:dispBlanksAs", NS)
                    sk = cat_ax.find("c:tickLblSkip", NS) if cat_ax is not None else None
                    bp = cat_ax.find("c:txPr/a:bodyPr", NS) if cat_ax is not None else None
                    dr = cat_ax.find("c:txPr/a:p/a:pPr/a:defRPr", NS) if cat_ax is not None else None
                    nf = cat_ax.find("c:numFmt", NS) if cat_ax is not None else None
                    charts.append({"skip": sk.get("val") if sk is not None else None,
                                   "rot": bp.get("rot") if bp is not None else None,
                                   "sz": dr.get("sz") if dr is not None else None,
                                   "numfmt": nf.get("formatCode") if nf is not None else None,
                                   "title": title, "series": ser,
                                   "axis": "catAx" if cat_ax is not None else
                                           ("dateAx" if cx.find(".//c:plotArea/c:dateAx", NS) is not None else None),
                                   "auto": auto.get("val") if auto is not None else None,
                                   "blanks": db.get("val") if db is not None else None})
            out[sh.get("name")] = charts
    return out


def overall_charts(cm):
    """引用「整體測試」的圖表：[(所在工作表, 圖表)]。"""
    return [(sh, c) for sh, cs in cm.items() for c in cs if c["series"] and all("整體測試" in (s["val"] or "") for s in c["series"])]


def expected_refs(n):
    col = [get_column_letter(h) for h in HELPER]
    return {"cat": f"'整體測試'!$A$2:$A${n + 1}", "val": [f"'整體測試'!${c}$2:${c}${n + 1}" for c in col]}


# ----------------------------------------------------------------------------- 單一情境的完整驗證
tmp = tempfile.mkdtemp(prefix="cert_overall_range_")


def make(segs_or_obs, stopped, extra_decs=(), sched=None):
    if sched is None:
        obs, sched = build(segs_or_obs)
    else:
        obs = segs_or_obs
    r0 = analyze(obs, P, [], [])
    res = analyze(obs, P, labels(r0.stages, stopped) + list(extra_decs), [])
    return obs, sched, res


def verify_scenario(tag, desc, segs, stopped, exp_pairs, save=True, sched_check=True):
    """N Cycle 通用驗證（依適用項目）。回傳 (res, wb_v2, info)。"""
    print(f"\n== {tag} {desc} ==")
    obs, sched, res = make(segs, stopped)
    S = res.stages
    pre = (len(S) == len(sched) and [st.dir for st in S] == [d for d, _a, _b in sched]
           and all(st.start.t == BASE + timedelta(seconds=a) and st.A.t == BASE + timedelta(seconds=b)
                   for st, (_d, a, b) in zip(S, sched)))
    check(f"{tag} 前提：引擎切出 {len(sched)} 個階段，方向／開始／最後運行時間與合成排程一致", pre or not sched_check,
          str([(st.no, st.dir, st.start.t.time(), st.A.t.time()) for st in S]))
    check(f"{tag} 原始 Stage.no＝1～{len(S)}（不重新編號）", [st.no for st in S] == list(range(1, len(S) + 1)))
    before = state_of(res)
    wb1 = RX.build_workbook(res, V1)
    wb2 = RX.build_workbook(res, V2)
    check(f"{tag} 產報不修改 Result／Stage／觀測（Stage.no、標記、seq、觀測物件不變）", state_of(res) == before)
    info = wb2._cert_overall_v2
    n = len(exp_pairs)
    check(f"{tag} 有效完整循環＝{n}，實際配對 Stage.no＝{exp_pairs}", info["pairs"] == exp_pairs, str(info["pairs"]))
    kept = [st for st in S if st.no not in stopped]
    ws = wb2["整體測試"]
    rows = sheet_rows(ws)
    if not kept:
        check(f"{tag} 全部中止：沒有整體曲線（t0／t1＝None、無分鐘列、整體頁與 Summary 無整體圖）",
              info["t0"] is None and info["t1"] is None and not rows and not ws._charts
              and len(wb2["Summary"]._charts) == 0)
        if save:
            p2 = RX.save_report(res, V2, os.path.join(tmp, f"{tag}_v2.xlsx"))
            cm = chart_map(p2)
            check(f"{tag} 全部中止：存檔成功、category＝v2、xlsx 內沒有引用整體測試的圖表",
                  os.path.exists(p2) and not overall_charts(cm) and _category(p2) == RX.STOPPED_FORMAT_V2)
        return res, wb2, info
    t0 = BASE + timedelta(seconds=sched[kept[0].no - 1][1])
    t1 = BASE + timedelta(seconds=max(sched[st.no - 1][2] for st in kept))
    check(f"{tag} 有效範圍：起點＝第一個有效階段開始 {t0.time()}、終點＝有效階段最後運行最大值 {t1.time()}",
          info["t0"] == t0 and info["t1"] == t1, f"{info['t0']}～{info['t1']}")
    exp_masks = []
    for run in runs_of(stopped):
        a = min(o.t for o in res.obs if o.stage_no == run[0])
        b = max(o.t for o in res.obs if o.stage_no == run[-1])
        exp_masks.append((a, b, run))
    check(f"{tag} 遮蔽區段＝相鄰中止階段合併（含其間休息）：{[m[2] for m in exp_masks]}", info["masks"] == exp_masks,
          str(info["masks"]))
    # ---- 中止全面排除（方案 B；原期待值「每段中間遮蔽 1 列分隔列」→ 新期待值「0 列分隔列、前後有效分鐘直接相鄰」）
    arows = all_rows(ws)
    lay = exp_layout(t0, t1, exp_masks)
    got_lay = [r[0] for r in arows]
    n_full = int((floor_m(t1) - floor_m(t0)) / MIN) + 1
    n_masked = sum(1 for k in range(n_full)
                   if any(floor_m(t0) + k * MIN <= b and floor_m(t0) + k * MIN + MIN > a for a, b, _r in exp_masks))
    n_sep = sum(1 for x in got_lay if x is None)
    exp_n = len(lay)
    check(f"{tag} 中止全面排除：floor(t0)～floor(t1) {n_full} 分鐘 − 遮蔽 {n_masked} 分鐘 ＝ {exp_n} 列"
          f"（有效分鐘依原始時間遞增、原始整分時間戳記、表外無資料列、沒有分隔列）",
          got_lay == lay and ws.max_row == exp_n + 1 and exp_n == n_full - n_masked, f"{len(got_lay)} 列：{got_lay[:4]}…")
    exp_jump = interior_runs(stopped, [st.no for st in kept])
    n_jump = sum(1 for x, y in zip(got_lay, got_lay[1:]) if x is not None and y is not None and y - x > MIN)
    check(f"{tag} 分隔列＝0（時間空白列 0、info separators＝0）；時間跳越次數＝前後都有有效階段的連續中止段數 {exp_jump}"
          f"（Stage 層級獨立推導；頭尾中止段只裁切、不產生跳越）",
          n_sep == 0 and info.get("separators") == 0 and n_jump == exp_jump, f"sep={n_sep} info={info.get('separators')} jumps={n_jump}")
    kept_nos = [st.no for st in kept]
    exp_unp = sorted(set(kept_nos) - {x for pr in exp_pairs for x in pr})
    etitle = exp_title_v2(exp_pairs, kept_nos)
    check(f"{tag} 圖表標題＝{etitle}（有效 {kept_nos}、配對 {exp_pairs}、未配對 {exp_unp}）",
          info["title"] == etitle and info.get("unpaired") == exp_unp, f"{info['title']} unpaired={info.get('unpaired')}")
    eskip = exp_skip(exp_n, n, len(exp_unp))
    check(f"{tag} X 軸標籤間隔：{exp_n} 列、{n} 個循環＋{len(exp_unp)} 個未配對 → tickLblSkip＝{eskip}"
          f"（可見標籤約 {-(-exp_n // eskip) if eskip else 0} 個 ≤ 40）",
          info.get("label_skip") == eskip and (eskip is None or -(-exp_n // eskip) <= 40), str(info.get("label_skip")))

    def masked(M):
        return any(M <= b and M + MIN > a for a, b, _r in exp_masks)
    check(f"{tag} 不保留任何與遮蔽區段重疊的分鐘列", not [r[0] for r in rows if masked(r[0])])
    stop_txt = [(r[0], v) for r in arows for v in r[1] if isinstance(v, str) and "中止" in v]
    check(f"{tag} 整體測試頁沒有任何中止相關文字（無「已排除中止階段」、無中止時間範圍、無跳越說明）、沒有空白時間列",
          not stop_txt and all(r[0] is not None for r in arows), str(stop_txt[:2]))
    # 直接銜接（原期待值「跨遮蔽區段的有效列之間一定隔著分隔列（曲線斷開）」→ 新期待值「直接相鄰（曲線銜接）」）：
    # 相鄰兩列相差 1 分鐘，或相差 >1 分鐘時其間每一分鐘都與遮蔽區段重疊（跳越只發生在中止處），且跳越兩端皆有溫度值
    bad_join = []
    for x, y in zip(arows, arows[1:]):
        if x[0] is None or y[0] is None:                  # 空白時間列（分隔列）→ 不是直接銜接
            bad_join.append((x[0], y[0]))
            continue
        if y[0] - x[0] == MIN:
            continue
        between = [x[0] + k * MIN for k in range(1, int((y[0] - x[0]) / MIN))]
        if not (between and all(masked_m(M, exp_masks) for M in between) and x[2][0] is not None and y[2][0] is not None):
            bad_join.append((x[0].time(), y[0].time()))
    check(f"{tag} 前後有效曲線直接銜接：時間跳越只發生在中止處（其間分鐘皆為中止遮蔽）、跳越兩端皆有溫度值；"
          f"時間戳記為原始整分（不產生虛構時間、不插值）", not bad_join, str(bad_join[:3]))
    clip = [o for o in res.obs if t0 <= o.t <= t1 and not any(a <= o.t <= b for a, b, _r in exp_masks)]
    bad_val = []
    for t, _vals, hv in rows:
        if masked(t):
            continue
        mo = [o for o in clip if t <= o.t < t + MIN]
        exp_max = max((o.values["rack_max_c"] for o in mo), default=None)
        exp_min = min((o.values["rack_min_c"] for o in mo), default=None)
        if hv[0] != exp_max or hv[1] != exp_min:
            bad_val.append((t.time(), hv[:2], exp_max, exp_min))
    check(f"{tag} 未遮蔽分鐘的 Rack 最大／最小＝裁切後實際觀測（有效範圍內、不在遮蔽區段）的分鐘極值（獨立重算）",
          not bad_val, str(bad_val[:3]))
    rest_ok = True
    for x, y in zip(S, S[1:]):
        if x.no in stopped or y.no in stopped:
            continue
        a_ = BASE + timedelta(seconds=sched[x.no - 1][2])
        b_ = BASE + timedelta(seconds=sched[y.no - 1][1])
        mid = [r for r in rows if a_ + MIN <= r[0] and r[0] + MIN <= b_]
        rest_ok = rest_ok and bool(mid) and all(r[2][0] is not None for r in mid)
    check(f"{tag} 相鄰有效階段之間的休息資料保留（範圍內照常顯示）", rest_ok)
    keep = [s for s in wb2.sheetnames if s != "整體測試"]
    check(f"{tag} v1／v2 除整體測試頁外逐格一致（各階段頁、統計、Summary 儲存格、數據概覽、即時熱度、Raw Data）",
          wb1.sheetnames == wb2.sheetnames and all(sheet_values(wb1[s]) == sheet_values(wb2[s]) for s in keep))
    if save:
        p2 = RX.save_report(res, V2, os.path.join(tmp, f"{tag}_v2.xlsx"))
        cm = chart_map(p2)
        oc = overall_charts(cm)
        er = expected_refs(exp_n)
        ok = (sorted(sh for sh, _c in oc) == ["Summary", "整體測試"]
              and all(c["title"] == etitle and len(c["series"]) == 3
                      and [s["val"] for s in c["series"]] == er["val"] and all(s["cat"] == er["cat"] for s in c["series"])
                      for _sh, c in oc))
        check(f"{tag} xlsx 圖表引用：Summary 與整體測試各 1 張、標題相同、三條曲線（Rack 最大／最小／傳感器）"
              f"引用同一截斷範圍 2～{exp_n + 1} 列", ok, str([(sh, c["title"], [s["val"] for s in c["series"]]) for sh, c in oc]))
        others = [c for _sh, cs in cm.items() for c in cs if not any(c is c2 for _s2, c2 in oc)]
        check(f"{tag} xlsx X 軸：兩張 v2 整體圖＝catAx＋<c:auto val=\"0\"/>（文字類別軸）、dispBlanksAs＝gap；"
              f"其餘 {len(others)} 張階段圖不含 c:auto（不受影響）",
              all(c["axis"] == "catAx" and c["auto"] == "0" and c["blanks"] == "gap" for _sh, c in oc)
              and all(c["auto"] is None for c in others),
              str([(c["axis"], c["auto"], c["blanks"]) for _sh, c in oc]))
        check(f"{tag} xlsx 時間標籤：兩張 v2 整體圖 tickLblSkip＝{eskip}（兩張相同）、文字直式（rot＝-5400000）、10pt（sz＝1000）、"
              f"格式 h:mm；階段圖維持 tickLblSkip＝1",
              len(oc) == 2 and all(c["skip"] == str(eskip) and c["rot"] == "-5400000" and c["sz"] == "1000"
                                   and c["numfmt"] == "h:mm" for _sh, c in oc)
              and all(c["skip"] == "1" for c in others),
              str([(c["skip"], c["rot"], c["sz"], c["numfmt"]) for _sh, c in oc]))
        check(f"{tag} category＝{RX.STOPPED_FORMAT_V2}", _category(p2) == RX.STOPPED_FORMAT_V2)
        check(f"{tag} xlsx 全份掃描（工作表儲存格、圖表文字、活頁簿描述）沒有中止相關文字", not stop_text_in(p2), str(stop_text_in(p2)[:2]))
    return res, wb2, info


def stop_text_in(path):
    """掃描 xlsx：所有工作表儲存格（含隱藏欄）、圖表 XML 文字、docProps（描述等）中含「中止」的片段。"""
    from openpyxl import load_workbook
    hits = []
    wb = load_workbook(path, read_only=True)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                hits += [(ws.title, v[:40]) for v in row if isinstance(v, str) and "中止" in v]
        if "中止" in (wb.properties.description or ""):
            hits.append(("description", (wb.properties.description or "")[:60]))
    finally:
        wb.close()
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if (n.startswith("xl/charts/") or n.startswith("docProps/")) and "中止".encode("utf-8") in z.read(n):
                hits.append((n, ""))
    return hits


def _category(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    try:
        return wb.properties.category or ""
    finally:
        wb.close()


try:
    # ========================================================================= NC：N Cycle 動態情境（循環數不設上限）
    print("\n######## NC：N Cycle 動態情境（1／2／3／4／5／10 個循環、任意位置中止）########")
    NC = [
        ("NC-01", "1 個循環全部有效（先放後充）", 1, "D", set(), [(1, 2)]),
        ("NC-02", "2 個循環全部有效", 2, "D", set(), [(1, 2), (3, 4)]),
        ("NC-03", "3 個循環全部有效", 3, "D", set(), [(1, 2), (3, 4), (5, 6)]),
        ("NC-04", "4 個循環全部有效", 4, "D", set(), [(1, 2), (3, 4), (5, 6), (7, 8)]),
        ("NC-05", "5 個循環全部有效", 5, "D", set(), [(i, i + 1) for i in range(1, 10, 2)]),
        ("NC-06", "10 個循環全部有效", 10, "D", set(), [(i, i + 1) for i in range(1, 20, 2)]),
        ("NC-07", "4 個循環，第一循環中止（裁切前段）", 4, "D", {1, 2}, [(3, 4), (5, 6), (7, 8)]),
        ("NC-08", "4 個循環，最後循環中止（裁切後段）", 4, "D", {7, 8}, [(1, 2), (3, 4), (5, 6)]),
        ("NC-09", "4 個循環，第二循環中止（中間移除、直接銜接）", 4, "D", {3, 4}, [(1, 2), (5, 6), (7, 8)]),
        ("NC-10", "4 個循環，第一及最後循環中止", 4, "D", {1, 2, 7, 8}, [(3, 4), (5, 6)]),
        ("NC-11", "5 個循環，第二及第四循環中止（兩個不連續遮蔽區段 → 2 次時間跳越、0 列分隔列）", 5, "D", {3, 4, 7, 8}, [(1, 2), (5, 6), (9, 10)]),
        ("NC-12", "5 個循環，第二～三循環連續中止（單一遮蔽區段 → 1 次時間跳越、0 列分隔列）", 5, "D", {3, 4, 5, 6}, [(1, 2), (7, 8), (9, 10)]),
        ("NC-13", "4 個循環，個別充電階段中止（第 4 階段）", 4, "D", {4}, [(1, 2), (5, 6), (7, 8)]),
        ("NC-14", "4 個循環，個別放電階段中止（第 5 階段）", 4, "D", {5}, [(1, 2), (3, 4), (7, 8)]),
        ("NC-15", "3 個循環，第一個階段中止（配對方向不偏移）", 3, "D", {1}, [(3, 4), (5, 6)]),
        ("NC-16", "2 個循環，中間兩階段中止：有有效階段但沒有完整循環", 2, "D", {2, 3}, []),
        ("NC-17", "3 個循環全部中止", 3, "D", {1, 2, 3, 4, 5, 6}, []),
        ("NC-19", "3 個循環，充電優先方向", 3, "C", set(), [(1, 2), (3, 4), (5, 6)]),
        ("NC-20", "4 個循環，放電優先方向＋第三循環中止", 4, "D", {5, 6}, [(1, 2), (3, 4), (7, 8)]),
    ]
    for tag, desc, n, first, stopped, pairs in NC:
        verify_scenario(tag, desc, cycle_segs(n, first), stopped, pairs)

    # NC-18 短階段（083020 同型：放 → 充 → 充（中止、180 秒）→ 放 → 充）
    SEG_SHORT = [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("C", 180), ("I", 300),
                 ("D", 600), ("I", 300), ("C", 600), ("I", 300)]
    verify_scenario("NC-18", "短階段（083020 同型：第 3 階段 180 秒充電中止）", SEG_SHORT, {3}, [(1, 2), (4, 5)])

    # NC-15 補強：若錯用「第一個有效階段方向」會配成 (2,3)(4,5)；甲-1 必須是 (3,4)(5,6)
    _o, _s, r15 = make(cycle_segs(3, "D"), {1})
    ex15 = [st for st in r15.stages if st.no == 1]
    check("NC-15b 第一階段中止時固定方向仍為原始第 1 階段的方向（放電）：配對 (3,4)(5,6)，不是 (2,3)(4,5)",
          RX.valid_cycle_pairs(r15.stages, ex15) == [(3, 4), (5, 6)])
    check("NC-15c valid_cycle_pairs 不依輸入順序（倒序輸入結果相同）、不修改 Stage",
          RX.valid_cycle_pairs(list(reversed(r15.stages)), ex15) == [(3, 4), (5, 6)]
          and [st.no for st in r15.stages] == list(range(1, 7)))

    # ========================================================================= OR-01～OR-07 基本情境（2 個循環 083020 型）
    print("\n######## OR-01～OR-07 基本情境 ########")
    SEG2 = cycle_segs(2, "D")
    verify_scenario("OR-01", "兩次循環皆有效", SEG2, set(), [(1, 2), (3, 4)])
    r02, wb02, info02 = verify_scenario("OR-02", "第 2 次充放電皆中止", SEG2, {3, 4}, [(1, 2)])
    r03, wb03, _i03 = verify_scenario("OR-03", "第 1 次充放電皆中止", SEG2, {1, 2}, [(3, 4)])
    check("OR-03b 顯示次數沿用重新排序（原第 2 次 → 第1次放電／第1次充電），原始 Stage.no 不變（3、4）",
          [s for s in wb03.sheetnames if s[:1].isdigit()] == ["1.第1次放電(滿放)", "2.第1次充電(滿充)"]
          and [st.no for st in r03.stages if st.no not in (1, 2)] == [3, 4])
    verify_scenario("OR-04", "單一充電階段中止（第 2 階段）", SEG2, {2}, [(3, 4)])
    verify_scenario("OR-05", "單一放電階段中止（第 3 階段；第 4 階段有效但未配對，仍在範圍內）", SEG2, {3}, [(1, 2)])
    r06, wb06, info06 = verify_scenario("OR-06", "中間循環中止（3 個循環，第 2 循環中止）", cycle_segs(3, "D"), {3, 4},
                                        [(1, 2), (5, 6)])
    verify_scenario("OR-07", "所有階段皆中止", SEG2, {1, 2, 3, 4}, [])

    # ========================================================================= OR-08 分鐘邊界與遮蔽（通則已在每個情境驗證）
    print("\n== OR-08 分鐘邊界及中止區間遮蔽 ==")
    m06 = info06["masks"][0]
    rows06 = sheet_rows(wb06["整體測試"])
    arows06 = all_rows(wb06["整體測試"])
    # 原期待值：遮蔽區段內每一分鐘皆 N/A 列（保留真實時間間隔）。新期待值（人工驗收修正）：遮蔽分鐘不保留、原位置一列分隔列，
    # 分隔列前後為原始時間戳記的有效數值列（時間直接跳越遮蔽區段，不重算時間）。
    before = floor_m(m06[0]) - MIN          # 遮蔽前最後一個未重疊分鐘（M＋1 分 ≤ 遮蔽起點）
    after = floor_m(m06[1]) + MIN           # 遮蔽後第一個未重疊分鐘（M ＞ 遮蔽終點）
    # 方案 B（原期待值：原位置恰好一列分隔列）：區段內分鐘全部不保留、沒有分隔列，遮蔽前最後未重疊分鐘與遮蔽後第一個
    # 未重疊分鐘直接相鄰（圖表直接銜接）
    ib = [i for i, r in enumerate(arows06) if r[0] == before]
    check("OR-08 中間遮蔽區段：區段內分鐘全部不保留、沒有分隔列；遮蔽前最後未重疊分鐘 "
          f"{before.time()} 的下一列直接是遮蔽後第一個未重疊分鐘 {after.time()}，兩側皆有有效數值（原始時間戳記、直接銜接）",
          ib and not [r for r in arows06 if r[0] is None] and not [r for r in rows06 if r[0] <= m06[1] and r[0] + MIN > m06[0]]
          and arows06[ib[0] + 1][0] == after and arows06[ib[0]][2][0] is not None and arows06[ib[0] + 1][2][0] is not None,
          str([(r[0], r[2][0]) for r in arows06[ib[0]:ib[0] + 2]] if ib else "找不到遮蔽前分鐘"))
    # 非中止原因的 N/A 分鐘（有效範圍內缺少取樣）照舊保留、不與中止分鐘一併移除
    g0, g1 = BASE + timedelta(minutes=12), BASE + timedelta(minutes=13, seconds=59)   # 第 1 階段（10:01～10:10:50）後休息中
    obs_gap = [ob(int((o.t - BASE).total_seconds()), "I" if o.values["current_a"] == 0 else
                  ("D" if o.values["current_a"] < 0 else "C")) for o in r06.obs if not (g0 <= o.t <= g1)]
    r0g = analyze(obs_gap, P, [], [])
    rg = analyze(obs_gap, P, labels(r0g.stages, {3, 4}), [])
    wg = RX.build_workbook(rg, V2)
    ag = all_rows(wg["整體測試"])
    gap_t = [g0, g0 + MIN]                                                            # 10:12、10:13 完全沒有取樣
    gap_rows = [r for r in ag if r[0] in gap_t]
    check("OR-08b 有效範圍內缺少取樣的分鐘（非中止原因）照舊保留為 N/A 列（時間保留、備註「N/A：該分鐘內無取樣」），"
          "不被移除（圖表輔助欄空值 → gap 斷點保留）；前後列時間相鄰；全表沒有分隔列",
          [st.no for st in rg.stages] == [1, 2, 3, 4, 5, 6] and len(gap_rows) == 2
          and all(r[2] == [None, None, None] and str(r[1][-1]) == "N/A：該分鐘內無取樣" for r in gap_rows)
          and all(ag[ag.index(r) - 1][0] == r[0] - MIN and ag[ag.index(r) + 1][0] == r[0] + MIN for r in gap_rows)
          and sum(1 for r in ag if r[0] is None) == 0,
          str([(r[0], r[1][-1]) for r in gap_rows]))

    # ========================================================================= OR-09 只用實際觀測
    print("\n== OR-09 LIVE／CARRIED 不影響整體實際觀測資料 ==")
    _orig_om = RX.overall_minutes
    seen = []

    def _spy(r, **kw):
        seen.append(list(r.obs))
        return _orig_om(r, **kw)
    RX.overall_minutes = _spy
    try:
        RX.build_workbook(r06, V2)
    finally:
        RX.overall_minutes = _orig_om
    ids = {id(o) for o in r06.obs}
    a06, b06, _ = info06["masks"][0]
    check("OR-09 交給 overall_minutes 的觀測全部是 Result.obs 的實際觀測物件（身分相同；沒有沿用／補值產生的列），"
          "且都在有效範圍內、不在遮蔽區段（LIVE／CARRIED 只屬 Raw Data 每秒列，不進入整體曲線）",
          len(seen) == 1 and seen[0] and all(id(o) in ids for o in seen[0])
          and all(info06["t0"] <= o.t <= info06["t1"] and not (a06 <= o.t <= b06) for o in seen[0]))

    # ========================================================================= OR-10／OR-20／OR-21 圖表
    print("\n== OR-10／OR-20／OR-21 Summary 與整體測試圖表、截斷 ==")
    p1 = RX.save_report(r02, V1, os.path.join(tmp, "OR21_v1.xlsx"))
    p2 = RX.save_report(r02, V2, os.path.join(tmp, "OR21_v2.xlsx"))
    oc1, oc2 = overall_charts(chart_map(p1)), overall_charts(chart_map(p2))
    n1 = len(sheet_rows(RX.build_workbook(r02, V1)["整體測試"]))
    n2 = len(all_rows(wb02["整體測試"]))
    check("OR-10 Summary 與整體測試的整體圖：同一標題、同一資料來源（三條曲線與 X 軸公式完全相同）",
          len(oc2) == 2 and oc2[0][1] == oc2[1][1])
    def _rows_of(f):
        return tuple(re.findall(r"\$(\d+)", f.split("!")[1]))
    check("OR-20 三條溫度曲線（Rack 最大／最小／傳感器）的資料範圍與 X 軸範圍列號完全相同",
          all(len({_rows_of(s["val"]) for s in c["series"]} | {_rows_of(s["cat"]) for s in c["series"]}) == 1
              and len(c["series"]) == 3 for _sh, c in oc2))
    check(f"OR-21 圖表實際引用範圍已截斷：v1 {n1} 列 → v2 {n2} 列（引用公式到第 {n2 + 1} 列；不是隱藏曲線或只改 X 軸標籤）",
          n2 < n1 and all(c["series"][0]["val"].endswith(f"${n2 + 1}") for _sh, c in oc2)
          and all(c["series"][0]["val"].endswith(f"${n1 + 1}") for _sh, c in oc1)
          and floor_m(info02["t1"]) == sheet_rows(wb02["整體測試"])[-1][0])
    check("OR-21b v1 標題沿用 meta['cycles']（電櫃充放2個循環，Legacy／v1 不變）；v2 依有效完整循環且無未配對（獨立櫃充放1個循環）",
          oc1[0][1]["title"] == exp_title(2) and oc2[0][1]["title"] == exp_title_v2([(1, 2)], [1, 2]))
    pl21 = RX.save_report(r02, META, os.path.join(tmp, "OR21_legacy.xlsx"))
    cm1, cml, cm2 = chart_map(p1), chart_map(pl21), chart_map(p2)
    st_ch = lambda cm: [c for sh, cs in cm.items() if sh not in ("Summary", "整體測試") for c in cs]     # noqa: E731
    check("OR-21c Legacy／v1 整體圖與全部階段圖 X 軸維持原樣（catAx、無 c:auto、dispBlanksAs＝gap）；"
          "v1／v2 各階段頁圖表的標題、資料參照、X 軸設定完全相同",
          all(c["axis"] == "catAx" and c["auto"] is None and c["blanks"] == "gap"
              for cm in (cm1, cml) for cs in cm.values() for c in cs)
          and st_ch(cm1) == st_ch(cm2) and st_ch(cm2))

    # ========================================================================= OR-11 完整在範圍內且未遮蔽的分鐘與 v1 一致
    print("\n== OR-11 完整有效且未遮蔽分鐘的數值與 v1 一致 ==")
    for tag_, res_, info_ in (("OR-06", r06, info06), ("OR-02", r02, info02)):
        w1 = {r[0]: r for r in sheet_rows(RX.build_workbook(res_, V1)["整體測試"])}
        w2 = sheet_rows(RX.build_workbook(res_, V2)["整體測試"])
        full = [r for r in w2 if r[0] >= info_["t0"] and r[0] + MIN - timedelta(seconds=1) <= info_["t1"]
                and not any(r[0] <= b and r[0] + MIN > a for a, b, _run in info_["masks"])]
        check(f"OR-11 {tag_}：{len(full)} 個完整在範圍內且未遮蔽的分鐘，v1／v2 數值、備註、輔助欄逐格相同",
              full and all(w1[r[0]][1:] == r[1:] for r in full))

    # ========================================================================= OR-12 與 P-4 pair_session 交叉
    print("\n== OR-12 循環計數與 pair_session() 交叉驗證 ==")
    import cert_merge_analysis as MA

    def p4_pairs(res, stopped, first):
        sa = {"cert_id": "CERT_SYN", "s_no": 1, "state": {}, "params": P, "res": res, "orphans": [],
              "stages": [{"id": f"S{st.no}", "idx": i, "no": st.no, "dir": st.dir, "stage": st, "stage_start": key(st),
                          "effective_label": "中止" if st.no in stopped else LABEL_OK[st.dir], "label_reasons": [],
                          "pending_pauses": 0} for i, st in enumerate(res.stages)]}
        cy = MA.pair_session(sa, (first, OPP[first]))
        return [(c["a"]["no"], c["b"]["no"]) for c in cy]
    for tag_, n_, first_, stp in (("全部有效 1 循環", 1, "D", set()), ("全部有效 3 循環", 3, "D", set()),
                                  ("全部有效 5 循環（充電優先）", 5, "C", set()), ("全部有效 10 循環", 10, "D", set()),
                                  ("第 2 循環中止", 3, "D", {3, 4}), ("第一階段中止", 3, "D", {1}),
                                  ("個別放電中止", 4, "D", {5})):
        obs_, sched_ = build(cycle_segs(n_, first_))
        r0_ = analyze(obs_, P, [], [])
        dec = labels(r0_.stages, stp) + [cnt(st, False) for st in r0_.stages if st.no in stp]
        res_ = analyze(obs_, P, dec, [])
        ex_ = [st for st in res_.stages if st.no in stp]
        mine, p4 = RX.valid_cycle_pairs(res_.stages, ex_), p4_pairs(res_, stp, first_)
        check(f"OR-12 {tag_}：v2 配對與 P-4 pair_session 相同 {mine}", mine == p4, f"v2={mine} P-4={p4}")
    # 刻意差異：P-4 會在排除不計次階段後的剩餘序列重新配對（可跨越中止階段）；甲-1 不跨越
    SEG_X = [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("C", 600), ("I", 300), ("D", 600), ("I", 300)]
    obs_x, _sx = build(SEG_X)
    r0x = analyze(obs_x, P, [], [])
    resx = analyze(obs_x, P, labels(r0x.stages, {2}) + [cnt(r0x.stages[1], False)], [])
    mine_x = RX.valid_cycle_pairs(resx.stages, [resx.stages[1]])
    p4_x = p4_pairs(resx, {2}, "D")
    check("OR-12b 已知且刻意的差異（放 → 充（中止）→ 充 → 放）：P-4 跨越中止階段配成 (1,3)；甲-1 不跨越 → 0 個循環",
          p4_x == [(1, 3)] and mine_x == [], f"P-4={p4_x} v2={mine_x}")

    # ========================================================================= OR-13／OR-19 格式相容
    print("\n== OR-13 Legacy／v1／v2／未知格式相容性 ==")
    check("OR-13 report_category：無旗標＝Legacy（空白）；只排除＝v1；排除＋整體範圍＝v2；只有整體範圍（無排除）＝Legacy",
          RX.report_category(META) == "" and RX.report_category(V1) == RX.STOPPED_FORMAT
          and RX.report_category(V2) == RX.STOPPED_FORMAT_V2
          and RX.report_category(dict(META, **{RX.OVERALL_RANGE_KEY: True})) == "")
    check("OR-13b format_flags：Legacy／v1／v2 對應旗標；未知格式 → None（不猜測）",
          RX.format_flags("") == {RX.EXCLUDE_STOPPED_KEY: False, RX.OVERALL_RANGE_KEY: False}
          and RX.format_flags(RX.STOPPED_FORMAT) == {RX.EXCLUDE_STOPPED_KEY: True, RX.OVERALL_RANGE_KEY: False}
          and RX.format_flags(RX.STOPPED_FORMAT_V2) == {RX.EXCLUDE_STOPPED_KEY: True, RX.OVERALL_RANGE_KEY: True}
          and RX.format_flags("stopped_stages_excluded=v9") is None)
    pl = RX.save_report(r02, META, os.path.join(tmp, "OR13_legacy.xlsx"))
    wl = RX.build_workbook(r02, META)
    check("OR-13c Legacy（直接呼叫 save_report、無旗標）：category 空白、中止階段頁保留、整體頁完整時間軸（不走 v2）",
          _category(pl) == "" and len([s for s in wl.sheetnames if s[:1].isdigit()]) == 4
          and not hasattr(wl, "_cert_overall_v2"))
    check("OR-13d v1：category＝v1、整體頁維持完整原始時間軸（pad_excluded_minutes，首筆～末筆觀測所在分鐘）",
          _category(p1) == RX.STOPPED_FORMAT
          and n1 == int((floor_m(r02.obs[-1].t) - floor_m(r02.obs[0].t)) / MIN) + 1)
    import cert_merge_verify as VF
    check("OR-13e cert_merge_verify：支援格式＝Legacy／v1／v2（共用中止排除驗證）；未知格式不在支援清單（→ UNVERIFIABLE）",
          VF.STOPPED_FORMATS == (RX.STOPPED_FORMAT, RX.STOPPED_FORMAT_V2)
          and VF.report_format(p2) == RX.STOPPED_FORMAT_V2 and VF.report_format(p1) == RX.STOPPED_FORMAT
          and VF.report_format(pl) == "" and "stopped_stages_excluded=v9" not in ("",) + VF.STOPPED_FORMATS)

    print("\n== OR-19 Legacy／v1／v2 混合合併：各 Session 依自己的原始報告格式選擇重產規則 ==")
    import cert_merge_xlsx as MX
    _orig_bf = MX._baseline_format
    got = {}
    try:
        for fmt in ("", RX.STOPPED_FORMAT, RX.STOPPED_FORMAT_V2, "stopped_stages_excluded=v9"):
            d = tempfile.mkdtemp(prefix="or19_", dir=tmp)
            MX._baseline_format = lambda folder, st, _f=fmt: _f
            sa = {"data": {"folder": d, "records": [], "commands": []}, "state": {"cert_id": "CERT_SYN", "test_info": {}},
                  "params": P}
            meta, _ss = MX.detail_meta(sa, "重產聲明（測試）")
            got[fmt] = (meta[RX.EXCLUDE_STOPPED_KEY], meta[RX.OVERALL_RANGE_KEY], RX.report_category(meta))
    finally:
        MX._baseline_format = _orig_bf
    check("OR-19 detail_meta：Legacy→(否,否,空白)、v1→(是,否,v1)、v2→(是,是,v2)、未知→照舊以 Legacy 規則重產（驗證端判 UNVERIFIABLE）",
          got == {"": (False, False, ""), RX.STOPPED_FORMAT: (True, False, RX.STOPPED_FORMAT),
                  RX.STOPPED_FORMAT_V2: (True, True, RX.STOPPED_FORMAT_V2),
                  "stopped_stages_excluded=v9": (False, False, "")}, str(got))

    # ========================================================================= OR-14／OR-15 首尾分鐘邊界
    print("\n== OR-14／OR-15 首分鐘／末分鐘的範圍外待機觀測不影響溫度極值 ==")
    obs_b = [ob(t, "I", spike=(t == 85)) for t in range(5, 95, 10)]               # 10:00:05～10:01:25；10:01:25 為極值
    obs_b += [ob(t, "D") for t in range(95, 695, 10)]                              # 放電 10:01:35～10:11:25
    obs_b += [ob(t, "I") for t in range(695, 995, 10)]
    obs_b += [ob(t, "C") for t in range(995, 1595, 10)]                            # 充電 10:16:35～10:26:25
    obs_b += [ob(t, "I", spike=(t == 1595)) for t in range(1595, 1895, 10)]       # 10:26:35 為極值（同一分鐘）
    sched_b = [("D", 95, 685), ("C", 995, 1585)]
    r0b = analyze(obs_b, P, [], [])
    resb = analyze(obs_b, P, labels(r0b.stages, set()), [])
    check("OR-14 前提：t0＝10:01:35（非整分）、t1＝10:26:25（非整分），首／末分鐘各有一筆範圍外待機極值觀測",
          [(st.start.t, st.A.t) for st in resb.stages] == [(BASE + timedelta(seconds=a), BASE + timedelta(seconds=b))
                                                           for _d, a, b in sched_b])
    rb1 = sheet_rows(RX.build_workbook(resb, V1)["整體測試"])
    wbb2 = RX.build_workbook(resb, V2)
    rb2 = sheet_rows(wbb2["整體測試"])
    f1 = next(r for r in rb1 if r[0] == BASE + timedelta(minutes=1))
    check("OR-14 首分鐘 10:01：v2 不納入 10:01:35 之前的待機觀測（Rack 最大≠99、最小≠1）；v1 會納入（證明邊界篩選生效）",
          rb2[0][0] == BASE + timedelta(minutes=1) and rb2[0][2][0] != 99.0 and rb2[0][2][1] != 1.0
          and f1[2][0] == 99.0 and f1[2][1] == 1.0, f"v2={rb2[0][2]} v1={f1[2]}")
    l1 = next(r for r in rb1 if r[0] == BASE + timedelta(minutes=26))
    check("OR-15 末分鐘 10:26：v2 不納入 10:26:25 之後的待機觀測；v1 會納入；v2 最後一列＝10:26（之後無資料列）",
          rb2[-1][0] == BASE + timedelta(minutes=26) and rb2[-1][2][0] != 99.0 and rb2[-1][2][1] != 1.0
          and l1[2][0] == 99.0 and l1[2][1] == 1.0, f"v2={rb2[-1][2]} v1={l1[2]}")
    check("OR-14b 邊界分鐘以外，v1／v2 數值相同（差異只在邊界分鐘）",
          all(r1[1:] == r2[1:] for r2 in rb2[1:-1] for r1 in rb1 if r1[0] == r2[0]))

    # ========================================================================= OR-16 中止與有效階段共用同一分鐘
    print("\n== OR-16 中止階段與有效階段共用同一分鐘 ==")
    SEG16 = [("I", 90), ("D", 600), ("I", 300), ("C", 600), ("D", 600), ("I", 300), ("C", 600), ("I", 300)]
    obs16, sched16 = build(SEG16)
    r016 = analyze(obs16, P, [], [])
    S16 = r016.stages
    pre16 = [st.dir for st in S16] == ["D", "C", "D", "C"] and floor_m(S16[1].A.t) == floor_m(S16[2].start.t)
    check("OR-16 前提：放 → 充（中止）→ 放（直接換向）→ 充；中止充電最後一筆與下一個放電首筆在同一分鐘",
          pre16, str([(st.no, st.dir, st.start.t.time(), st.A.t.time(), st.end_by) for st in S16]))
    if pre16:
        res16 = analyze(obs16, P, labels(S16, {2}), [])
        w161, w162 = RX.build_workbook(res16, V1), RX.build_workbook(res16, V2)
        Mshare = floor_m(res16.stages[2].start.t)
        a16 = all_rows(w162["整體測試"])
        nxt = [i for i, r in enumerate(a16) if r[0] == Mshare + MIN]
        # 原期待值：共用分鐘保留為 N/A 列 → 併入分隔列；方案 B：共用分鐘與遮蔽重疊 → 不保留、沒有分隔列，
        # 遮蔽前最後未重疊分鐘（中止充電開始前一分鐘）的下一列直接是共用分鐘的下一分鐘（直接銜接）
        Mprev = floor_m(res16.stages[1].start.t) - MIN
        check("OR-16 共用分鐘：與遮蔽區段重疊 → 不保留（不寫成有效值）、沒有分隔列與中止備註；"
              f"{Mprev.time()} 的下一列直接是 {(Mshare + MIN).time()}，配對＝(3,4)",
              not [r for r in a16 if r[0] == Mshare] and nxt and a16[nxt[0] - 1][0] == Mprev
              and not [r for r in a16 if r[0] is None]
              and not [v for r in a16 for v in r[1] if isinstance(v, str) and "中止" in v]
              and w162._cert_overall_v2["pairs"] == [(3, 4)])
        stage_sheet = [s for s in w162.sheetnames if s.startswith("2.")][0]
        first_row = sheet_values(w162[stage_sheet])[1]
        check("OR-16b 獨立階段頁不受整體遮蔽影響：第 3 階段（顯示第2次放電）頁與 v1 逐格相同，共用分鐘仍有數值",
              sheet_values(w161[stage_sheet]) == sheet_values(w162[stage_sheet]) and first_row[0] == Mshare
              and first_row[2] is not None)

    # ========================================================================= OR-17 連續中止階段之間的待機觀測
    print("\n== OR-17 連續中止階段之間存在待機實際觀測，不出現零星曲線 ==")
    a17, b17, _r17 = info06["masks"][0]
    gap = [r for r in rows06 if r[0] >= a17 and r[0] + MIN <= b17]
    v1_06 = {r[0]: r for r in sheet_rows(RX.build_workbook(r06, V1)["整體測試"])}
    rest_mid_t = []
    M = floor_m(r06.stages[2].A.t) + 2 * MIN
    while M < floor_m(r06.stages[3].start.t) - MIN:
        rest_mid_t.append(M)
        M += MIN
    # 原期待值：v2 這些分鐘全部 N/A 列 → 第一次驗收版本整段只剩一列分隔列；方案 B：完全不保留（0 列）
    check("OR-17 第 3、4 階段（連續中止）之間的休息：v1 有待機數值（零星曲線來源），v2 這些分鐘全部不保留（不出現零星曲線）",
          rest_mid_t and all(v1_06[M][2][0] is not None for M in rest_mid_t)
          and not [r for r in rows06 if r[0] in rest_mid_t] and not gap)

    # ========================================================================= OR-22 分鐘層級相鄰／重疊的多段遮蔽 → 全部移除（方案 B）
    print("\n== OR-22 多段遮蔽在分鐘層級相鄰或重疊時：重疊分鐘全部移除、不產生分隔列 ==")
    # 以 r06（3 循環，第 3、4 階段中止）的實際觀測為輸入，人為提供兩段在分鐘層級重疊（同一分鐘內一段結束、另一段開始）
    # 及相鄰（前段結束分鐘＋1＝後段開始分鐘）的遮蔽；預期以分鐘重疊關係獨立推得。
    S6 = r06.stages
    t0_22, t1_22 = S6[0].start.t, S6[5].A.t
    mA = (S6[2].start.t, S6[2].start.t + timedelta(seconds=150), [S6[2]])            # 一段
    mB = (mA[1] + timedelta(seconds=5), S6[3].A.t, [S6[3]])                           # 同一分鐘或下一分鐘開始
    mC = (S6[3].A.t + timedelta(seconds=10), S6[3].A.t + timedelta(seconds=70), [S6[3]])
    for tag_, masks_ in (("重疊／相鄰兩段", [mA, mB]), ("三段（含同一階段重複出現）", [mA, mB, mC])):
        rows22 = RX.overall_v2_rows(RX.without_stopped(r06, [S6[2], S6[3]]), t0_22, t1_22, masks_,
                                    lambda r: RX.overall_minutes(r, ext_scope="minute"))
        exp22 = exp_layout(t0_22, t1_22, [(a, b, [s.no for s in run]) for a, b, run in masks_])
        sep22 = [mr for mr in rows22 if mr.T is None]
        # 原期待值：只產生 1 列分隔列且列出第 3、4 階段；方案 B：0 列分隔列、沒有任何中止說明，列配置與獨立推導相同
        check(f"OR-22 {tag_}：重疊／相鄰遮蔽的分鐘全部移除、0 列分隔列、沒有中止說明，列配置與獨立推導相同",
              [mr.T for mr in rows22] == exp22 and not sep22
              and not [mr for mr in rows22 if "中止" in (mr.na_reason or "")], f"{len(rows22)} vs {len(exp22)}")

    # ========================================================================= OR-23 頭尾中止階段與有效範圍共用首／末分鐘
    print("\n== OR-23 頭尾中止段與有效範圍共用首／末分鐘：不加分隔列 ==")
    SEG23 = [("I", 90), ("C", 600), ("D", 600), ("I", 300), ("C", 600), ("D", 600), ("I", 300)]
    _o23, _s23 = build(SEG23)
    r023 = analyze(_o23, P, [], [])
    S23 = r023.stages
    pre23 = ([st.dir for st in S23] == ["C", "D", "C", "D"] and floor_m(S23[0].A.t) == floor_m(S23[1].start.t)
             and floor_m(S23[2].A.t) == floor_m(S23[3].start.t))
    check("OR-23 前提：充（中止）→ 放（直接換向）→ 充 → 放（中止、直接換向）；頭段末筆與 t0、t1 與尾段首筆各在同一分鐘",
          pre23, str([(st.no, st.dir, st.start.t.time(), st.A.t.time()) for st in S23]))
    if pre23:
        _r23, w23, i23 = verify_scenario("OR-23", "頭尾中止段與有效範圍共用首／末分鐘", SEG23, {1, 4}, [])
        a23 = all_rows(w23["整體測試"])
        check("OR-23b 首分鐘 floor(t0) 與頭段遮蔽重疊 → 不保留，第一列＝floor(t0)＋1 分；末分鐘 floor(t1) 與尾段遮蔽重疊 → 不保留，"
              "最後一列＝floor(t1)−1 分；全表沒有分隔列",
              a23[0][0] == floor_m(i23["t0"]) + MIN and a23[-1][0] == floor_m(i23["t1"]) - MIN
              and not [r for r in a23 if r[0] is None] and i23["separators"] == 0)

    # ========================================================================= OR-25 活頁簿描述（方案 B＋選項 1）
    print("\n== OR-25 v2 活頁簿描述不含中止說明；v1 描述維持原樣 ==")
    _o25, _s25 = build(SEG2)
    r025 = analyze(_o25, P, [], [])
    S25 = r025.stages
    dec25 = labels(S25, {2}) + [cnt(S25[1], False),
                                {"type": "stage_count", "stage_start": key(S25[2]), "counted": True, "reason": "測試：確認計次",
                                 "note": "", "at": "2026-01-05 23:20:00", "by": "offline_patch", "request_id": "OFFLINE-OR25-K"}]
    res25 = analyze(_o25, P, dec25, [])
    p25_1 = RX.save_report(res25, V1, os.path.join(tmp, "OR25_v1.xlsx"))
    p25_2 = RX.save_report(res25, V2, os.path.join(tmp, "OR25_v2.xlsx"))
    from openpyxl import load_workbook as _lw25
    w25_1, w25_2 = _lw25(p25_1, read_only=True), _lw25(p25_2, read_only=True)
    d1, d2 = w25_1.properties.description or "", w25_2.properties.description or ""
    k1, k2 = w25_1.properties.keywords, w25_2.properties.keywords
    w25_1.close(); w25_2.close()
    st2 = S25[1].start.t.strftime("%m/%d %H:%M:%S")
    check("OR-25 前提：第 2 階段標記中止且 stage_count 不計次；第 3 階段（有效）有 stage_count 計次決策",
          not res25.stages[1].counted and res25.stages[1].label == "中止"
          and any(h.get("counted") is True for h in (res25.stages[2].count_history or [])))
    check("OR-25a v1 描述維持原樣：含「已排除中止階段：充電，開始 …」及第 2 階段的「階段計次 … 不計次」說明",
          f"已排除中止階段：充電，開始 {st2}" in d1 and any(l.startswith("階段計次：") and "不計次" in l for l in d1.split("\n")))
    check("OR-25b v2 描述不含任何中止說明（無已排除中止階段、無中止階段開始／結束時間與筆數、無中止階段的不計次說明）",
          "中止" not in d2 and st2 not in d2 and "不計次" not in d2, d2[:200])
    check("OR-25c v2 其他正常描述保留：來源說明「合成資料」、有效第 3 階段的計次說明；category＝v2；dec_v1 keywords 與 v1 相同",
          "合成資料" in d2 and any(l.startswith("階段計次：") and "計次" in l for l in d2.split("\n"))
          and _category(p25_2) == RX.STOPPED_FORMAT_V2 and RX.format_flags(_category(p25_2))[RX.OVERALL_RANGE_KEY]
          and k1 == k2 and k2)
    check("OR-25d v2 xlsx 全份掃描（工作表、圖表、描述）沒有中止相關文字", not stop_text_in(p25_2), str(stop_text_in(p25_2)[:3]))
    import report_display as RD25
    rs25 = {"stats": {"counts": {}, "max_age": 0}, "excluded": {"rows_dropped": 3, "rows_blanked": 1},
            "rows": [{"time": BASE + timedelta(seconds=i), "state": RD25.RAW_LIVE, "obs": None, "age": 0} for i in range(5)]}
    n_on, n_off = RX.raw_seconds_note(rs25), RX.raw_seconds_note(rs25, stopped_text=False)
    check("OR-25e Raw Data 摘要：v1（預設）仍寫「已排除中止階段 3 列…沿用中止階段讀值改為 N/A」；v2 不寫中止列數，其餘內容相同",
          "已排除中止階段 3 列" in n_on and "中止" not in n_off and "秒數不重複；" in n_off
          and n_off == n_on.replace("已排除中止階段 3 列（排除時段不列出）、1 列沿用中止階段讀值改為 N/A；", ""), n_off[:120])

    # ========================================================================= TL：第二次人工驗收（標題＋X 軸標籤間隔）
    print("\n######## TL：v2 標題（獨立櫃／未配對 → 充放電）與 X 軸標籤動態間隔 ########")
    TL = [
        ("TL-01", "只有充電", [("I", 60), ("C", 600), ("I", 300)], set(), []),
        ("TL-02", "只有放電", [("I", 60), ("D", 600), ("I", 300)], set(), []),
        ("TL-03", "完整 1 Cycle", cycle_segs(1, "D"), set(), [(1, 2)]),
        ("TL-04", "1 Cycle＋充電（放→充→充：第 3 階段未配對）",
         [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("C", 600), ("I", 300)], set(), [(1, 2)]),
        ("TL-05", "1 Cycle＋放電（放→充→放：第 3 階段未配對）",
         [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("D", 600), ("I", 300)], set(), [(1, 2)]),
        ("TL-06", "完整 2 Cycle", cycle_segs(2, "D"), set(), [(1, 2), (3, 4)]),
        ("TL-07", "2 Cycle＋未配對放電（放充放充放）", cycle_segs(2, "D") + [("D", 600), ("I", 300)], set(), [(1, 2), (3, 4)]),
        ("TL-08", "偶數個有效階段但無法全部配對（放→充→充→放：甲-1 只配 (1,2)，第 3、4 階段未配對）",
         [("I", 60), ("D", 600), ("I", 300), ("C", 600), ("I", 300), ("C", 600), ("I", 300), ("D", 600), ("I", 300)],
         set(), [(1, 2)]),
        ("TL-09", "完整 6 Cycle", cycle_segs(6, "D"), set(), [(i, i + 1) for i in range(1, 12, 2)]),
        ("TL-10", "完整 12 Cycle（超過 10 個循環）", cycle_segs(12, "C"), set(), [(i, i + 1) for i in range(1, 24, 2)]),
        ("TL-11", "5 Cycle 中止第 2、4 循環（剩餘完整配對 → 3 個循環）", cycle_segs(5, "D"), {3, 4, 7, 8},
         [(1, 2), (5, 6), (9, 10)]),
        ("TL-12", "4 Cycle 中止第 2 循環的充電（第 3 階段放電未配對 → 充放電）", cycle_segs(4, "D"), {4},
         [(1, 2), (5, 6), (7, 8)]),
    ]
    tl = {}
    for tag_, desc_, segs_, stp_, prs_ in TL:
        tl[tag_] = verify_scenario(tag_, desc_, segs_, stp_, prs_)
    # 只有充電／只有放電：配對為空 → 充放電（不寫循環數）
    check("TL-01b／TL-02b 只有單一方向有效階段：標題「獨立櫃充放電」、未配對＝[1]",
          all(tl[t][2]["title"] == "【250KW】獨立櫃充放電溫度曲線" and tl[t][2]["unpaired"] == [1] for t in ("TL-01", "TL-02")))
    check("TL-08b 偶數個（4 個）有效階段仍顯示「充放電」：未配對 [3, 4]，循環數（pairs）維持 1",
          tl["TL-08"][2]["title"] == "【250KW】獨立櫃充放電溫度曲線" and tl["TL-08"][2]["unpaired"] == [3, 4]
          and tl["TL-08"][2]["pairs"] == [(1, 2)])
    check("TL-10b 12 個循環全部配對 →「獨立櫃充放12個循環溫度曲線」（循環數不設上限）",
          tl["TL-10"][2]["title"] == "【250KW】獨立櫃充放12個循環溫度曲線")
    # 相同循環數、不同測試時間（每階段 600 秒 vs 3600 秒）→ 間隔依實際列數調整
    _r13a, w13a, i13a = verify_scenario("TL-13a", "3 Cycle，每階段 600 秒", cycle_segs(3, "D"), set(),
                                        [(1, 2), (3, 4), (5, 6)], save=False)
    _r13b, w13b, i13b = verify_scenario("TL-13b", "3 Cycle，每階段 3600 秒", cycle_segs(3, "D", stage_s=3600), set(),
                                        [(1, 2), (3, 4), (5, 6)], save=False)
    n13a, n13b = len(all_rows(w13a["整體測試"])), len(all_rows(w13b["整體測試"]))
    la = -(-n13a // i13a["label_skip"]) if i13a.get("label_skip") else 0
    lb = -(-n13b // i13b["label_skip"]) if i13b.get("label_skip") else 0
    check(f"TL-13 相同 3 個循環（基礎 10）、不同測試時間：{n13a} 列 → k＝{i13a['label_skip']}（約 {la} 個標籤）；"
          f"{n13b} 列 → k＝{i13b['label_skip']}（約 {lb} 個標籤）；間隔隨列數調整、皆在 12～40 個之間",
          i13a["label_base"] == i13b["label_base"] == 10 and (i13a["label_skip"] or 0) < (i13b["label_skip"] or 0)
          and 12 <= la <= 40 and 12 <= lb <= 40)

    # 公式邊界（期待值依裁定公式手算，不呼叫受測函式產生）
    print("\n== TL-14 動態間隔公式邊界（手算期待值）==")
    CASES_K = [
        ("資料列 0 → 沒有圖表", (0, 3, 0), None),
        ("資料列 5（少於 12）、1 個循環 → 1（上下限皆 1，不強制 12 個標籤）", (5, 1, 0), 1),
        ("資料列 11、3 個循環 → 1", (11, 3, 0), 1),
        ("Idle 60 列、0 個循環 → 基礎 3", (60, 0, 0), 3),
        ("780 列、5 個循環 → 基礎 20＝下限 20 → 20", (780, 5, 0), 20),
        ("502 列、3 個循環 → 下限 13 → 常用值 15", (502, 3, 0), 15),
        ("621 列、2 個循環 → 下限 16 → 常用值 20", (621, 2, 0), 20),
        ("494 列、1 個循環＋1 未配對（N＝1.5 → 基礎 6）→ 下限 13 → 15", (494, 1, 1), 15),
        ("380 列、2 個循環＋1 未配對（N＝2.5 → 基礎 10）→ 10", (380, 2, 1), 10),
        ("200 列、2 個循環＋1 未配對（N＝2.5 → 基礎 10；下限 5）→ 10（未配對 0.5 影響間隔）", (200, 2, 1), 10),
        ("200 列、2 個循環、無未配對（N＝2 → 基礎 6）→ 6（對照）", (200, 2, 0), 6),
        ("10,000 列、1 個循環 → 下限 250 → 常用值 360（向上延伸，無固定上限）", (10000, 1, 0), 360),
        ("2,000 列、12 個循環（基礎 55）→ 常用值 60", (2000, 12, 0), 60),
        ("100,000 列、20 個循環 → 下限 2500 → 常用值 2880", (100000, 20, 0), 2880),
    ]
    for lab_, args_, exp_ in CASES_K:
        got_ = RX.overall_label_skip(*args_)
        check(f"TL-14 {lab_}", got_ == exp_ and exp_skip(*args_) == exp_, f"got={got_} test_formula={exp_skip(*args_)}")
    got_n = RX.overall_label_skip(100, 3, 0, l_max=12, l_min=12)
    check("TL-14 上下限之間沒有常用值（100 列、上限 12、下限 12 → [9, 9]）→ 用符合密度限制的整數 9（⌈100／9⌉＝12 ≤ 12）",
          got_n == 9 and -(-100 // got_n) <= 12, str(got_n))
    got_w = RX.overall_label_skip(780, 5, 0, width_cm=8.5)
    check("TL-14 圖寬 8.5 cm（上限依圖寬等比例 → 20 個）：780 列 → 下限 39 → 常用值 45", got_w == 45, str(got_w))
    check("TL-14 基礎間隔表（N＝0／1／1.5／2／2.5／3／4／5／6／7／8／11）＝3／3／6／6／10／10／15／20／25／30／35／50",
          [RX.label_base(n_, 0) for n_ in (0, 1)] + [RX.label_base(1, 1), RX.label_base(2, 0), RX.label_base(2, 1)]
          + [RX.label_base(n_, 0) for n_ in (3, 4, 5, 6, 7, 8, 11)] == [3, 3, 6, 6, 10, 10, 15, 20, 25, 30, 35, 50])
    # 標籤位置：Excel 依類別序號顯示（第 0、k、2k… 個類別）；方案 B 沒有分隔列 → 每個標籤都是 A 欄的原始有效時間
    a11 = all_rows(tl["TL-11"][1]["整體測試"])
    k11 = tl["TL-11"][2]["label_skip"] or 0
    lab11 = [a11[i][0] for i in range(0, len(a11), k11)] if k11 else []
    check(f"TL-15 標籤位置（第 0、{k11}、{2 * k11}… 類別）全部是 A 欄原始有效時間（{len(lab11)} 個標籤，空白 0 個），"
          f"不產生不存在的時間",
          k11 > 0 and all(x is not None and x in {r[0] for r in a11} for x in lab11) and len(lab11) <= 40)

    # ========================================================================= OR-18 方向／不完整循環
    print("\n== OR-18 不同有效階段方向及不完整循環的配對 ==")
    SEG_CC = [("I", 60), ("C", 600), ("I", 300), ("C", 600), ("I", 300), ("D", 600), ("I", 300)]
    verify_scenario("OR-18a", "連續相同方向的有效階段（充 → 充 → 放）：只有 (2,3)", SEG_CC, set(), [(2, 3)], save=False)
    SEG_ONE = [("I", 60), ("D", 600), ("I", 300)]
    verify_scenario("OR-18b", "只有單一有效階段：0 個完整循環（標題省略循環數）", SEG_ONE, set(), [], save=False)
    verify_scenario("OR-18c", "先充後放 2 循環，第 1 階段中止：方向仍為充電優先 → (3,4)", cycle_segs(2, "C"), {1}, [(3, 4)],
                    save=False)

    # ========================================================================= IO：Idle-only（原始 Stage 清單為空）≠ 全部中止
    print("\n######## IO：Idle-only（原始 Stage 清單為空）與全部中止分流 ########")

    def ob_flag(t, bp=None, fault=False, ok=True):
        o = ob(t, "I")
        o.ok = ok
        if not ok:
            o.values = {}
            o.fail_reason = "讀取失敗（測試）"
        o.flags = {"battery_power_status": bp, "pcs_fault_flag": fault or None}
        return o

    def overall_cells(wb):
        return [r for r in sheet_values(wb["整體測試"])[1:] if r and r[0] is not None]

    def idle_case(tag, desc, obs):
        print(f"\n== {tag} {desc} ==")
        res = analyze(obs, P, [], [])
        check(f"{tag} 前提：原始 Stage 清單為空（{len(obs)} 筆觀測）", res.stages == [])
        wl, w1, w2 = RX.build_workbook(res, META), RX.build_workbook(res, V1), RX.build_workbook(res, V2)
        info = w2._cert_overall_v2
        cl, c1, c2 = overall_cells(wl), overall_cells(w1), overall_cells(w2)
        if obs:
            exp_n = int((floor_m(obs[-1].t) - floor_m(obs[0].t)) / MIN) + 1
            times = [r[0] for r in c2]
            check(f"{tag} v2 Idle-only 分支（mode＝idle_only，不是 all_stopped）：保留完整觀測時間範圍 "
                  f"{floor_m(obs[0].t).time()}～{floor_m(obs[-1].t).time()}（{exp_n} 列，逐分鐘連續）",
                  info["mode"] == "idle_only" and times == [floor_m(obs[0].t) + k * MIN for k in range(exp_n)])
            check(f"{tag} v2 整體頁逐格＝Legacy＝v1（沿用既有分鐘聚合規則：數值、備註、輔助欄完全相同）", c2 == cl == c1)
            check(f"{tag} Idle-only 標題「{EXP_IDLE}」（沒有充放電階段：不寫充放電與循環數；v1 仍沿用 meta['cycles']）",
                  info["title"] == EXP_IDLE and info["pairs"] == [] and info.get("unpaired") == [])
        else:
            check(f"{tag} 沒有觀測：不產生虛構分鐘或空白曲線（0 列、整體頁與 Summary 皆無圖表）",
                  info["mode"] == "idle_only" and not c2 and not w2["整體測試"]._charts and not w2["Summary"]._charts)
        p2 = RX.save_report(res, V2, os.path.join(tmp, f"{tag}_v2.xlsx"))
        pl = RX.save_report(res, META, os.path.join(tmp, f"{tag}_legacy.xlsx"))
        p1_ = RX.save_report(res, V1, os.path.join(tmp, f"{tag}_v1.xlsx"))
        check(f"{tag} 格式相容：category Legacy＝空白／v1＝v1／v2＝v2；format_flags(v2) 還原同一組旗標",
              (_category(pl), _category(p1_), _category(p2)) == ("", RX.STOPPED_FORMAT, RX.STOPPED_FORMAT_V2)
              and RX.format_flags(_category(p2)) == {RX.EXCLUDE_STOPPED_KEY: True, RX.OVERALL_RANGE_KEY: True})
        regen = RX.build_workbook(res, dict(META, **RX.format_flags(_category(p2))))
        check(f"{tag} 依 v2 category 選擇規則重產 → 整體頁與原 v2 逐格相同（重產規則可重現）",
              overall_cells(regen) == c2 and regen._cert_overall_v2["mode"] == "idle_only")
        if obs:
            oc = overall_charts(chart_map(p2))
            er = expected_refs(len(c2))
            check(f"{tag} xlsx 圖表：Summary 與整體測試各 1 張、標題「{EXP_IDLE}」、三條曲線引用同一完整範圍 2～{len(c2) + 1} 列",
                  sorted(sh for sh, _c in oc) == ["Summary", "整體測試"]
                  and all(c["title"] == EXP_IDLE and [s["val"] for s in c["series"]] == er["val"]
                          and all(s["cat"] == er["cat"] for s in c["series"]) for _sh, c in oc))
            check(f"{tag} v2 Idle-only 整體圖同為 v2 文字類別軸（c:auto val=0、gap）；資料列逐分鐘連續、無分隔列，"
                  "故類別間距與日期軸相同（Legacy／v1 不含 c:auto）",
                  all(c["auto"] == "0" and c["blanks"] == "gap" for _sh, c in oc)
                  and all(c["auto"] is None for _sh, c in overall_charts(chart_map(pl)) + overall_charts(chart_map(p1_)))
                  and not [r for r in all_rows(w2["整體測試"]) if r[0] is None])
        return res, w2

    idle_obs = [ob_flag(t, bp="已上電") for t in range(5, 605, 10)]                  # 10:00:05～10:10:05（11 個分鐘）
    _r, w_io1 = idle_case("IO-01", "原始 Stage 清單為空、只有待機觀測（已上電）", idle_obs)
    check("IO-01b 待機分鐘保留實際溫度（輔助欄有值）、備註以「待機」開頭（其後可附 PCS 模組溫度說明）",
          all(r[2][0] is not None and str(r[1][-1]).startswith("待機") for r in sheet_rows(w_io1["整體測試"])))
    mixed = ([ob_flag(t, bp="已上電") for t in range(5, 185, 10)] + [ob_flag(t, bp="已下電") for t in range(185, 365, 10)]
             + [ob_flag(t, bp="已上電", fault=True) for t in range(365, 545, 10)] + [ob_flag(t, ok=False) for t in range(545, 665, 10)])
    _r, w_io2 = idle_case("IO-02", "原始 Stage 清單為空、含停機（已下電）／故障（PCS 故障旗標）／讀取失敗觀測", mixed)
    rem2 = {r[0].strftime("%H:%M"): r[1][-1] for r in sheet_rows(w_io2["整體測試"])}
    check("IO-02b 停機／故障／讀取失敗分鐘照實保留（不因沒有充放電階段而排除）：10:04＝停機、10:07＝故障、10:10＝N/A",
          str(rem2.get("10:04")).startswith("停機") and str(rem2.get("10:07")).startswith("故障")
          and str(rem2.get("10:10", "")).startswith("N/A"),
          str(rem2))
    idle_case("IO-03", "原始 Stage 清單為空、完全沒有觀測", [])

    print("\n== IO-04／IO-05 與 Idle-only 分流 ==")
    _o, _s, r_all = make(SEG2, {1, 2, 3, 4})
    w_all = RX.build_workbook(r_all, V2)
    check("IO-04 原始 Stage 清單非空但全部中止 → mode＝all_stopped（不是 idle_only）：不產生整體曲線、Summary 無整體圖",
          r_all.stages and w_all._cert_overall_v2["mode"] == "all_stopped" and not overall_cells(w_all)
          and not w_all["整體測試"]._charts and not w_all["Summary"]._charts)
    _o, _s, r_nc = make(cycle_segs(2, "D"), {2, 3})
    w_nc = RX.build_workbook(r_nc, V2)
    check("IO-05 有有效階段但無完整循環 → mode＝range（依有效範圍截斷，不走 Idle-only），0 個循環、標題「獨立櫃充放電」",
          w_nc._cert_overall_v2["mode"] == "range" and w_nc._cert_overall_v2["pairs"] == []
          and w_nc._cert_overall_v2["title"] == "【250KW】獨立櫃充放電溫度曲線"
          and overall_cells(w_nc)[0][0] == floor_m(r_nc.stages[0].start.t))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

n_pass = sum(_results)
print(f"\n== test_cert_overall_range：{n_pass}/{len(_results)} PASS ==")
print(f"結果：{n_pass} / {len(_results)} PASS（實際檢查 {len(_results)} 項；回歸工具以此行計數）")
sys.exit(0 if n_pass == len(_results) else 1)
