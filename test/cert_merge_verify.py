# -*- coding: utf-8 -*-
"""
跨 Session 整合認證報告 —— 資料值驗證（docs/Cert_Merge_Report_Design.md §10；M11、P-23、Q-B、X6）
===================================================================================================
比較「重產明細」與快照中的「原始 Menu 22／25 報告」位元組複本（openpyxl 只讀載入，不存檔）。
名稱固定「資料值驗證」：只涵蓋儲存格值、公式文字、工作表結構、表格與指定中繼資料；圖表、圖片、注入圖形與格式不在範圍。
原始參數可追溯性另欄記錄（第一版一律 UNVERIFIABLE），不影響資料值驗證結果。
"""
import dataclasses
import json
import os
import re
from datetime import datetime, timedelta

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

import cert_supplement as SUP
from cert_report_config import NA
from cert_report_xlsx import (LOG_KEYS, RAW1_HEADERS, _raw1_note, _sheet_name, _v, field_value, is_report_valid,
                              pcs_display, raw_measurement_valid, stage_count_notes)
from cert_report_xlsx import TITLE_FILL, _slot_title, _title_cols, screenshot_slots   # r8 FMT：H2 標題底色規則（唯讀使用）
from cert_stage_engine import decisions_digest, stage_names
from cert_report_xlsx import STOPPED_FORMATS, _stage_key, exclude_stopped_raw_rows, split_stopped   # 中止階段排除格式相容（r8／v2）

RAW_VALUE_KEYS = LOG_KEYS + ["power_kw"]          # 與 write_raw_seconds 相同順序：SOC～功率 kW（RAW1 第 3～12 欄）

VERIFIED, UNVERIFIABLE, FAILED, NOT_APPLICABLE = "VERIFIED", "UNVERIFIABLE", "FAILED", "不適用"
VERIFY_SCOPE = {
    "included": ["共有工作表已使用範圍聯集每一格的值與公式文字", "工作表名稱與順序", "每個工作表的已使用範圍大小",
                 "Raw Data 列數", "表格名稱與範圍", "活頁簿描述（以行比對，只允許 X3、X4、X5）", "活頁簿標題（只允許 X5）"],
    "excluded": ["圖表", "圖片", "注入圖形（箭頭、文字框、截圖時間標籤）", "字型、填色、框線、數值格式、欄寬列高、合併儲存格、"
                 "凍結窗格、條件式格式、資料驗證、註解、超連結", "列印設定", "未指定的中繼資料（建立者、建立／修改時間等，X9）",
                 "檔案位元組與壓縮結構"],
}
X3_PREFIXES = ("Raw Data（1 秒格點認證）", "Raw Data：本認證的格點")
X4_PREFIX = "HMI 資料來源："
VALUE_SHEETS_O = ("數據概覽", "即時熱度")
RAW_PREFIX = "Raw Data"
DETAIL_TITLE = "重產明細（非原始認證紀錄）"
ORIGINAL_TITLE = "充／放電認證測試報告"


def _t(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:19], fmt)
        except ValueError:
            pass
    return None


# =============================================================================
# 原始報告清單與比較基準（§10.3）
# =============================================================================
def report_list(folder, cid, state, sha256_file):
    pat = re.compile(re.escape(f"認證測試報告_{cid}_") + r"(\d+)(_未完成|_中途)?\.xlsx$")
    rep_dir = os.path.join(folder, "reports")
    files = sorted(os.listdir(rep_dir)) if os.path.isdir(rep_dir) else []
    rec_state = {os.path.basename(str(r.get("file", "")).replace("\\", "/")): r.get("at")
                 for r in (state or {}).get("reports") or []}
    rec_supp = {}
    for r in SUP.load_report_log(folder):
        if r.get("type") == "report" and r.get("file"):
            rec_supp[os.path.basename(str(r["file"]).replace("\\", "/"))] = r.get("at")
    out = []
    for fn in files:
        m = pat.search(fn)
        if not m:
            continue
        p = os.path.join(rep_dir, fn)
        notes = []
        if fn not in rec_state and fn not in rec_supp:
            notes.append("檔案存在但沒有產報紀錄")
        out.append({"file": fn, "path": p, "no": int(m.group(1)), "suffix": m.group(2) or "",
                    "sha256": sha256_file(p), "size": os.path.getsize(p),
                    "recorded_at": rec_state.get(fn) or rec_supp.get(fn), "note": "；".join(notes), "is_baseline": False})
    for fn in sorted(set(rec_state) | set(rec_supp)):
        if fn not in {o["file"] for o in out}:
            out.append({"file": fn, "path": None, "no": None, "suffix": "", "sha256": None, "size": None,
                        "recorded_at": rec_state.get(fn) or rec_supp.get(fn), "note": "有產報紀錄但檔案不存在",
                        "is_baseline": False})
    cands = [o for o in out if o["path"] is not None]
    base = max(cands, key=lambda o: o["no"]) if cands else None
    if base:
        base["is_baseline"] = True
    return out, base


def report_format(path):
    """原始報告格式識別（活頁簿屬性 category）：""＝舊格式（完整階段）；STOPPED_FORMATS（v1／v2）＝中止階段排除
    （v2 另含整體曲線有效範圍）；其他值原樣回傳（不支援）。"""
    wb = load_workbook(path, read_only=True)
    try:
        return wb.properties.category or ""
    finally:
        wb.close()


# =============================================================================
# r8：決策快照（dec_v1／NK；舊版時間路徑）— 三種保證分開
# =============================================================================
KW_RE = re.compile(r"^dec_v=(\d+);dec_n=(\d+);dec_sha256=([0-9a-f]{64})$")
WINDOW_SEC = 120
SNAP_CONFIRMED, SNAP_PRESUMED = "CONFIRMED", "PRESUMED"           # 決策快照一致性
TRACE_CONFIRMED, TRACE_PRESUMED = "已確認", "推定（未確認）"       # 原始決策集合可追溯性
_TZ_RE = re.compile(r"(Z|[+-]\d\d:?\d\d)$")
NAME_ONLY_TYPES = ("stage_label", "stage_count")


def scan_decisions(path):
    """與 SRC.read_jsonl／cert_runtime._read_jsonl 同一解析規則（UTF-8、去行尾空白、空白行略過、無法解析行略過），
    另回傳每筆解析成功紀錄的實體行號與每個無法解析行的位置（之前已有幾筆解析成功）。只讀。"""
    out = {"exists": os.path.exists(path), "records": [], "lines": [], "invalid_lines": [], "sha256": None,
           "physical_lines": 0}
    if not out["exists"]:
        return out
    with open(path, "rb") as f:
        raw = f.read()
    import hashlib
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8").splitlines()
    out["physical_lines"] = len(text)
    for i, line in enumerate(text, 1):
        s = line.strip()
        if not s:
            continue
        try:
            out["records"].append(json.loads(s))
            out["lines"].append(i)
        except ValueError:
            out["invalid_lines"].append({"line": i, "valid_before": len(out["records"])})
    return out


def report_keywords(path):
    """原始報告 docProps/core.xml 的 keywords（只讀；沒有 → None）。"""
    import zipfile
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        try:
            root = ET.fromstring(z.read("docProps/core.xml"))
        except KeyError:
            return None
    for e in root:
        if e.tag.endswith("}keywords"):
            return e.text
    return None


def _has_tz(s):
    return isinstance(s, str) and bool(_TZ_RE.search(s.strip()))


def _is_end_report(folder, file_name):
    """基準報告是否為「結束認證時產生」的報告：events.jsonl 中最後一筆 end 事件之後的第一筆 report 事件指向該檔。"""
    evp = os.path.join(folder, "events.jsonl")
    if not os.path.exists(evp):
        return False
    ev = []
    with open(evp, "rb") as f:
        for line in f.read().decode("utf-8").splitlines():
            try:
                ev.append(json.loads(line))
            except ValueError:
                continue
    ends = [i for i, e in enumerate(ev) if isinstance(e, dict) and e.get("type") == "end"]
    if not ends:
        return False
    nxt = next((e for e in ev[ends[-1] + 1:] if isinstance(e, dict) and e.get("type") == "report"), None)
    return bool(nxt) and file_name in str(nxt.get("detail") or "")


def baseline_decisions(sa, base, base_keywords, src_meta):
    """
    決定原始報告產生時使用的決策集合（基準集合）。回傳 dict：
      path：new（有 keywords）／old（無 keywords）；snapshot：CONFIRMED／PRESUMED／UNVERIFIABLE；
      base／added：基準集合與其後新增的決策（無法確定 → None）；trace；reason；evidence。
    新版 keywords 格式錯誤、版本不支援 → UNVERIFIABLE（不得降級為舊版時間路徑）。
    """
    folder = sa["data"]["folder"]
    sc = scan_decisions(os.path.join(folder, "decisions.jsonl"))
    recs = sc["records"]
    ev = {"decisions_file": {"exists": sc["exists"], "sha256": sc["sha256"], "physical_lines": sc["physical_lines"],
                             "valid": len(recs), "invalid_lines": sc["invalid_lines"], "record_lines": sc["lines"]}}

    def res(path, snap, reason, base_set=None, trace=UNVERIFIABLE, **extra):
        added = None if base_set is None else recs[len(base_set):]
        ev.update(extra)
        return {"path": path, "snapshot": snap, "reason": reason, "base": base_set, "added": added, "trace": trace,
                "added_lines": None if base_set is None else sc["lines"][len(base_set):], "evidence": ev,
                "all": recs}
    kw = (base_keywords or "").strip()
    if kw:
        m = KW_RE.match(kw)
        if not m:
            return res("new", UNVERIFIABLE, f"keywords 格式錯誤（NK-04）：{kw[:80]!r}", keywords=kw)
        v, n, sha = int(m.group(1)), int(m.group(2)), m.group(3)
        ev.update(keywords=kw, dec_v=v, dec_n=n, dec_sha256=sha)
        if v != 1:
            return res("new", UNVERIFIABLE, f"不支援的 dec_v＝{v}（NK-04）")
        if len(recs) < n:
            return res("new", UNVERIFIABLE, f"解析成功的決策 {len(recs)} 筆少於 dec_n＝{n}（NK-04）")
        bad = [b for b in sc["invalid_lines"] if b["valid_before"] < n]
        if bad:
            return res("new", UNVERIFIABLE, f"前 {n} 筆範圍內有無法解析的行（第 {bad[0]['line']} 行；NK-04）")
        dn, dsha = decisions_digest(recs[:n])
        ev.update(recomputed_sha256=dsha)
        if dsha != sha:
            return res("new", UNVERIFIABLE, "前 N 筆決策的 dec_v1 雜湊與原始報告 keywords 不符（NK-03／MR-08）")
        return res("new", SNAP_CONFIRMED, "", recs[:n], TRACE_CONFIRMED)
    # ---- 舊版（無 keywords）：時間路徑（§8，使用者 2026-11-24 補充裁定） ----
    st = sa["state"] or {}
    fn = base["file"]
    st_at = {os.path.basename(str(r.get("file", "")).replace("\\", "/")): r.get("at") for r in st.get("reports") or []}.get(fn)
    supp_at = {os.path.basename(str(r["file"]).replace("\\", "/")): r.get("at")
               for r in SUP.load_report_log(folder) if r.get("type") == "report" and r.get("file")}.get(fn)
    times = [x for x in (st_at, supp_at) if x]
    ev.update(generated_at_sources={"cert_state.reports": st_at, "supplements/reports.jsonl": supp_at})
    if not times:
        return res("old", UNVERIFIABLE, "原始報告產生時間缺失")
    if len(set(times)) > 1:
        return res("old", UNVERIFIABLE, "原始報告產生時間來源不一致")
    tz_items = [x for x in [times[0], st.get("ended_at")] + [d.get("at") for d in recs] if _has_tz(x)]
    if tz_items:
        return res("old", UNVERIFIABLE, f"時區無法確認（{tz_items[0]!r}）")
    gen = _t(times[0])
    if gen is None:
        return res("old", UNVERIFIABLE, "原始報告產生時間無法解析")
    ats = [_t(d.get("at")) for d in recs]
    if any(a is None for a in ats):
        return res("old", UNVERIFIABLE, "有決策時間無法解析（MR-07）", generated_at=times[0])
    if not any(a > gen for a in ats):
        return res("old", SNAP_PRESUMED, "", list(recs), TRACE_PRESUMED, generated_at=times[0],
                   note="原始報告產生後沒有新增決策（依時間）")
    # 有新增決策 → 以下全部成立才可做時間路徑基準重放
    if any(b < a for a, b in zip(ats, ats[1:])):
        return res("old", UNVERIFIABLE, "決策時間與檔案行序不一致（MR-07）", generated_at=times[0])
    if not _is_end_report(folder, fn):
        return res("old", UNVERIFIABLE, "原始報告不是結束認證時產生的報告：報告請求建立時間不在快照內，無法可靠對應",
                   generated_at=times[0])
    end = _t(st.get("ended_at"))
    if end is None:
        return res("old", UNVERIFIABLE, "認證結束時間缺失或無法解析", generated_at=times[0])
    lo, hi = end - timedelta(seconds=WINDOW_SEC), gen + timedelta(seconds=WINDOW_SEC)
    ev.update(generated_at=times[0], window=[lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")])
    inwin = [i for i, a in enumerate(ats) if lo <= a <= hi]
    if inwin:
        return res("old", UNVERIFIABLE, f"模糊窗口內有決策（第 {sc['lines'][inwin[0]]} 行；MR-07／MR-10）")
    meta = src_meta or {}
    rm = (meta.get(f"reports/{fn}") or {}).get("mtime")
    dm = (meta.get("decisions.jsonl") or {}).get("mtime")
    rmt, dmt = _t(rm), _t(dm)
    ev.update(report_mtime=rm, decisions_mtime=dm)
    if rmt is None or abs((rmt - gen).total_seconds()) > WINDOW_SEC:
        return res("old", UNVERIFIABLE, "輔助檢查不通過：報告檔修改時間與產生時間相差超過 120 秒或無法取得")
    if dmt is None or dmt < max(ats):
        return res("old", UNVERIFIABLE, "輔助檢查不通過：decisions.jsonl 修改時間早於最後一筆決策時間或無法取得")
    k = next(i for i, a in enumerate(ats) if a > gen)
    if any(a <= end for a in ats[k:]):
        return res("old", UNVERIFIABLE, "新增決策不都晚於認證結束時間")
    return res("old", SNAP_PRESUMED, "", list(recs[:k]), TRACE_PRESUMED)


# =============================================================================
# 驗證主程序
# =============================================================================
def verify_session(sa, detail_path, rs, sha256_file, *, bd=None, base_target=None):
    """
    sa：cert_merge_analysis.analyze_session 的結果（全部決策）；detail_path：重產明細 R_all（.partial.xlsx）；
    rs：raw_second_rows（R_all 的同一份分析）。
    r8：bd＝baseline_decisions() 的結果；base_target＝(sa_base, R_base 路徑, rs_base)（有新增決策且基準已確定時）。
      第一階段：原始報告 vs R_base（無新增決策時 R_base＝R_all）— F3、V-S、V-O、V-R、V-D、V-T 規則不變、不套用 X10；
      第二階段：R_base vs R_all — 差異歸因（DECISION_DIFF_OUT_OF_SCOPE、X10）。
      基準無法確定 → 沿用 r7 比對（原始報告 vs R_all，X7），並將決策快照列為 UNVERIFIABLE（不放寬）。
    回傳 dict（result、reason_codes、items、report_list、decision_snapshot、reproducibility、traceability…）。
    """
    folder = sa["data"]["folder"]
    st = sa["state"] or {}
    reps, base = report_list(folder, sa["cert_id"], st, sha256_file)
    out = {"s_no": sa["s_no"], "cert_id": sa["cert_id"], "items": [], "report_list": reps,
           "baseline": base["file"] if base else None, "param_traceability": UNVERIFIABLE,
           "param_traceability_note": "原始報告未記錄產生時的服務參數",
           "decision_snapshot": UNVERIFIABLE, "reproducibility": None, "traceability": UNVERIFIABLE,
           "decision_verification": None, "change_list": []}
    if base is None:
        return _final(out, UNVERIFIABLE, "沒有原始報告（無比較基準）")
    if base["suffix"] == "_中途":
        return _final(out, UNVERIFIABLE, "比較基準為 _中途 報告（原始報告之後仍有資料）")
    if not sa["data"]["has_slots"]:
        return _final(out, UNVERIFIABLE, "來源版本過舊或格式不支援（沒有 slots.jsonl）")
    wo = load_workbook(base["path"])                 # 只讀使用，不存檔
    wn = load_workbook(detail_path)
    wb_ = None
    try:
        if RAW_PREFIX not in wo.sheetnames or \
                [c.value for c in next(wo[RAW_PREFIX].iter_rows(min_row=1, max_row=1))] != RAW1_HEADERS:
            return _final(out, UNVERIFIABLE, "來源版本過舊或格式不支援（Raw Data 不是 13 欄格式）")
        fmt = wo.properties.category or ""
        out["report_format"] = fmt
        if fmt not in ("",) + STOPPED_FORMATS:                                       # 未知格式識別：不猜測
            return _final(out, UNVERIFIABLE, f"原始報告格式識別不支援（category＝{fmt!r}）")
        new_fmt = fmt in STOPPED_FORMATS             # v1／v2 共用中止階段排除驗證規則（重產端依格式選旗標）
        ok_base = bd is not None and bd["snapshot"] in (SNAP_CONFIRMED, SNAP_PRESUMED)
        out["decision_verification"] = {k: bd[k] for k in ("path", "snapshot", "reason", "trace", "evidence",
                                                            "added_lines")} if bd else None
        if ok_base and bd["added"]:
            sa_t, path_t, rs_t = base_target
            wb_ = load_workbook(path_t)
            wt = wb_
        else:
            sa_t, wt, rs_t = sa, wn, rs
        x8 = _after(SUP.load(folder), _t(base["recorded_at"]), ("at", "captured_at", "requested_at", "trigger_time"))
        if ok_base:
            # 新版確認：差異 → FAILED；舊版推定且有新增決策：差異 → UNVERIFIABLE（X7，基準可能配錯）；舊版無新增：r7 相同
            newer = (bd["path"] == "old" and bool(bd["added"]))
        else:
            newer = _after(sa["data"]["decisions"], _t(base["recorded_at"]), ("at",))      # r7 X7
        out["items"].append({"item": "D-S 決策快照一致性", "result": VERIFIED if ok_base else UNVERIFIABLE,
                             "reason_code": None if ok_base else "DECISION_SNAPSHOT",
                             "detail": {"path": bd and bd["path"], "snapshot": bd and bd["snapshot"],
                                        "reason": bd and bd["reason"]}})
        if wo.sheetnames != wt.sheetnames:                                           # F3（原始報告 vs R_base）
            out["items"].append({"item": "F3 工作表清單", "result": FAILED, "reason_code": "STRUCTURE",
                                 "detail": {"original": wo.sheetnames, "regenerated": wt.sheetnames,
                                            "compared_with": "R_base" if wt is not wn else "R_all"}})
            return _guarantees(_final(out), bd, ok_base, sa)
        for name in wo.sheetnames:
            if name.startswith(RAW_PREFIX):
                continue
            kind = "V-O" if name in VALUE_SHEETS_O else "V-S"
            out["items"].append(_cmp_sheet(kind, name, wo[name], wt[name], newer if kind == "V-S" else (newer or x8)))
        if new_fmt:                                  # 新格式：Raw Data 與 build_workbook 同一排除規則（共用函式）
            _k, _x, _sp = split_stopped(sa_t["res"], True)
            rs_t = exclude_stopped_raw_rows(rs_t, _x, _sp)
        out["items"].append(_cmp_raw(wo, wt, rs_t, sa_t))
        out["items"].append(_cmp_desc(wo.properties.description or "", wt.properties.description or ""))
        out["items"].append(_cmp_title(wo.properties.title, wt.properties.title))
        out["items"].append({"item": "V-B 位元組、建立時間（X9）", "result": NOT_APPLICABLE, "reason_code": None,
                             "detail": "不比較（必然不同）"})
        step1 = [i["result"] for i in out["items"] if i["item"] != "D-S 決策快照一致性"]
        out["reproducibility"] = FAILED if FAILED in step1 else (UNVERIFIABLE if UNVERIFIABLE in step1 else VERIFIED)
        if ok_base and bd["added"]:                                                  # 第二階段：差異歸因
            item, changes = attribute(sa_t, wt, sa, wn, bd, new_fmt=new_fmt)
            out["items"].append(item)
            out["change_list"] = changes
    finally:
        wo.close()
        wn.close()
        if wb_ is not None:
            wb_.close()
    return _guarantees(_final(out), bd, ok_base, sa)


def _guarantees(out, bd, ok_base, sa):
    """三種保證分開記錄（不合併成單一通過）；request_id 重複等決策異常 → 可追溯性不得判為已確認。"""
    out["decision_snapshot"] = bd["snapshot"] if bd else UNVERIFIABLE
    if out.get("reproducibility") is None:
        out["reproducibility"] = out["result"] if out["result"] != VERIFIED else VERIFIED
    anomalies = list(sa.get("decision_anomalies") or [])
    if not ok_base:
        out["traceability"] = UNVERIFIABLE
    elif anomalies:
        out["traceability"] = UNVERIFIABLE
        out["traceability_note"] = "決策紀錄異常：" + "；".join(f"{a['kind']} {a['request_id']}（第 {a['lines']} 筆）"
                                                            for a in anomalies)
    else:
        out["traceability"] = bd["trace"]
    if out["result"] == VERIFIED and out["traceability"] != TRACE_CONFIRMED:
        out["display"] = f"VERIFIED（決策集合可追溯性：{out['traceability']}）"
    return out


# =============================================================================
# r8：第二階段 R_base vs R_all 差異歸因（DECISION_DIFF_OUT_OF_SCOPE、X10）
# =============================================================================
def _desc_split(desc, notes):
    """描述行拆成（非計次部分, 計次部分）：save_report 把 stage_count_notes 附在最後；不符 → (None, None)。"""
    lines = desc.split("\n") if desc else []
    n = len(notes)
    if n == 0:
        return lines, []
    if lines[-n:] != notes:
        return None, None
    return lines[:-n], lines[-n:]


def _hist_key(h):
    return (h.get("at"), h.get("counted"), h.get("reason"), h.get("by"), h.get("request_id"))


TITLE_FILL_SHEETS = ("數據概覽", "即時熱度")        # H2 _write_overview／_write_heat：唯二呼叫 _title_row 的頁


def _style(c):
    from copy import copy
    return (copy(c.font), copy(c.fill), copy(c.border), copy(c.alignment), c.number_format, copy(c.protection))


_REF = []


def _ref_styles():
    """H2 底色格（只設 fill＝TITLE_FILL）與空白格的預期格式（以屬性比較，不依 style_id）。"""
    if _REF:
        return _REF[0]
    from openpyxl import Workbook
    ws = Workbook().active
    ws["B1"].fill = TITLE_FILL
    _REF.append((_style(ws["B1"]), _style(ws["A1"])))
    return _REF[0]


def _cell_state(ws, pos):
    """None＝不存在或與不存在等價（無值、預設格式、無超連結／註解）；否則 (值, 資料型別, 格式, 超連結, 註解)。"""
    c = ws._cells.get(pos)
    if c is None:
        return None
    st = (c.value, c.data_type, _style(c), c.hyperlink.target if c.hyperlink else None, c.comment.text if c.comment else None)
    if st[0] is None and st[2] == _ref_styles()[1] and st[3] is None and st[4] is None:
        return None
    return st


def _state_text(st):
    if st is None:
        return "不存在／空白預設格式"
    v, _t, sty, hl, cm = st
    fill = sty[1]
    return (f"值={v!r}；底色={getattr(fill, 'fill_type', None)}/{getattr(getattr(fill, 'fgColor', None), 'rgb', None)}；"
            f"字型={sty[0].name},{sty[0].sz},粗體={sty[0].b}；超連結={hl!r}；註解={cm!r}")


def _title_positions(ws, titles):
    """標題文字（H2 _slot_title）在頁上的位置：每個標題必須恰好出現一次、依序，且為 H2 標題格（14pt 粗體＋TITLE_FILL）。"""
    want = _ref_styles()[0][1]
    pos = []
    for t in titles:
        hits = [k for k, c in ws._cells.items() if c.value == t]
        if len(hits) != 1:
            return None
        c = ws._cells[hits[0]]
        if not (c.font.b and c.font.sz == 14 and _style(c)[1] == want):
            return None
        pos.append(hits[0])
    return pos if pos == sorted(pos) else None


def _fill_span(ws, r, c0):
    """實際底色涵蓋欄數：自標題格起連續為 TITLE_FILL 的格數。"""
    want = _ref_styles()[0][1]
    n = 0
    while (c := ws._cells.get((r, c0 + n))) is not None and _style(c)[1] == want:
        n += 1
    return n


def title_fill_rule(name, ws_b, ws_a, rb, ra, nb, na):
    """
    r8 FMT（STOP3 裁定，只用於第二階段 R_base vs R_all）：依 H2 規則推導標題底色範圍的合法變化。
    H2：tcols＝_title_cols(該頁全部區塊標題)（最寬標題決定、全頁共用）；每個標題列 (r, c0..c0+tcols-1) 只設 fill。
    回傳 (合法格位置 → 資訊, 規則資訊, 問題)。無法推導 → 問題（不豁免）。
    """
    info, problems = {"sheet": name}, []
    for sf in (True, False):
        tb = [_slot_title(nb, st) for st in screenshot_slots(rb.stages, sf)]
        ta = [_slot_title(na, st) for st in screenshot_slots(ra.stages, sf)]
        pb, pa = _title_positions(ws_b, tb), _title_positions(ws_a, ta)
        if pb is not None and pa is not None:
            break
    else:
        return {}, info, [f"{name}：無法由 H2 規則找到標題列（不套用底色規則）"]
    if pb != pa:
        return {}, info, [f"{name}：標題列位置不同"]
    cb, ca = _title_cols(tb), _title_cols(ta)
    info.update(show_final=sf, titles_base=tb, titles_all=ta, tcols_base=cb, tcols_all=ca,
                px_base=max(sum(19 if ord(ch) > 0x2E80 else 10 for ch in t) for t in tb),
                px_all=max(sum(19 if ord(ch) > 0x2E80 else 10 for ch in t) for t in ta),
                title_cells=[f"{get_column_letter(c)}{r}" for r, c in pb])
    for (r, c0) in pb:                                  # 實際底色必須與規則一致（證明推導正確）
        if _fill_span(ws_b, r, c0) != cb or _fill_span(ws_a, r, c0) != ca:
            problems.append(f"{name}：第 {r} 列標題底色欄數與 H2 規則不符（R_base {_fill_span(ws_b, r, c0)}／{cb}，"
                            f"R_all {_fill_span(ws_a, r, c0)}／{ca}）")
    allowed = {}
    if cb != ca and not problems:
        lo, hi = min(cb, ca), max(cb, ca)
        for k, (r, c0) in enumerate(pb):
            for j in range(lo, hi):
                allowed[(r, c0 + j)] = {"kind": "增加" if ca > cb else "減少", "title_base": tb[k], "title_all": ta[k],
                                        "title_cell": f"{get_column_letter(c0)}{r}"}
    return allowed, info, problems


def _compare_title_sheet(name, A, B, allowed, renamed_ok):
    """標題底色頁逐格比對（值、型別、格式、超連結、註解）；只有 allowed 內、符合 H2 底色格條件的差異合法。"""
    fill_ref, _blank = _ref_styles()
    problems, cells = [], []
    for pos in sorted(set(A._cells) | set(B._cells) | set(allowed)):
        u, v = _cell_state(A, pos), _cell_state(B, pos)
        coord = f"{get_column_letter(pos[1])}{pos[0]}"
        if pos in allowed:
            al = allowed[pos]
            big, small = (v, u) if al["kind"] == "增加" else (u, v)
            ok = (big is not None and big[0] is None and big[1] != "f" and big[2] == fill_ref and big[3] is None
                  and big[4] is None and small is None)
            if not ok:
                problems.append(f"{name}!{coord}：位於 H2 底色變化範圍，但不是純底色格（{_state_text(u)} → {_state_text(v)}）")
                continue
            cells.append(dict(al, cell=coord, before=_state_text(u), after=_state_text(v)))
            continue
        if u == v:
            continue
        if u is not None and v is not None and u[1:] == v[1:] and isinstance(u[0], str) and isinstance(v[0], str) \
                and renamed_ok(u[0], v[0]):
            continue
        problems.append(f"{name}!{coord}：{_state_text(u)} → {_state_text(v)}")
    if sorted(map(str, A.merged_cells.ranges)) != sorted(map(str, B.merged_cells.ranges)):
        problems.append(f"{name}：合併儲存格不同")
    if _tables(A) != _tables(B):
        problems.append(f"{name}：表格不同")
    cw = lambda ws: {k: d.width for k, d in ws.column_dimensions.items()}            # noqa: E731
    rh = lambda ws: {k: d.height for k, d in ws.row_dimensions.items() if d.height is not None}   # noqa: E731
    if cw(A) != cw(B) or rh(A) != rh(B):
        problems.append(f"{name}：欄寬或列高不同")
    im = lambda ws: [(type(i).__name__, str(getattr(getattr(i.anchor, "_from", None), "row", i.anchor)),     # noqa: E731
                      str(getattr(getattr(i.anchor, "_from", None), "col", ""))) for i in ws._images]
    if im(A) != im(B) or len(A._charts) != len(B._charts):
        problems.append(f"{name}：圖片或圖表不同")
    return problems, cells


def attribute(sa_b, wb_b, sa_a, wb_a, bd, new_fmt=False):
    """
    新增決策只有 stage_label／stage_count：差異只能是由 H2 規則推導的名稱（頁籤、標題、各頁名稱字串）與
    X10 計次描述；任何數值、列數、結構、非計次描述差異 → FAILED（DECISION_DIFF_OUT_OF_SCOPE）。
    含其他類型（continuity 等）→ 結構性人工決策：完整列出，仍要求 Raw Data 時間與數值欄不變（原始資料完整性）。
    """
    item = {"item": "D-A 人工決策差異歸因（R_base vs R_all）", "result": VERIFIED, "reason_code": None, "detail": {}}
    added = bd["added"]
    changes = [{"line": ln, "type": d.get("type"), "stage_start": d.get("stage_start"), "label": d.get("label"),
                "counted": d.get("counted"), "reason": d.get("reason"), "item_id": d.get("item_id"),
                "choice": d.get("choice"), "at": d.get("at"), "by": d.get("by"), "request_id": d.get("request_id")}
               for ln, d in zip(bd["added_lines"], added)]
    structural = any(d.get("type", "continuity") not in NAME_ONLY_TYPES for d in added)
    rb, ra = sa_b["res"], sa_a["res"]
    rb_full, ra_full = rb, ra                       # X10（計次描述、歷史、孤兒）依完整 Result（與 save_report 相同）
    if new_fmt:                                     # 新格式：名稱／頁籤／FMT 依排除後的有效階段推導（共用 split_stopped）
        kb, xb, _sb = split_stopped(rb, True)
        ka, xa, _sa = split_stopped(ra, True)
        if [_stage_key(s) for s in xb] != [_stage_key(s) for s in xa]:
            item.update(result=UNVERIFIABLE, reason_code="STOPPED_STAGE_SET_CHANGED",
                        detail={"stopped_base": [_stage_key(s) for s in xb], "stopped_all": [_stage_key(s) for s in xa],
                                "note": "新增決策改變中止階段集合 → 結構差異無法依 r8 名稱規則歸因（不放寬）"})
            return item, changes
        rb, ra = dataclasses.replace(rb, stages=kb), dataclasses.replace(ra, stages=ka)
    problems, x10 = [], {}
    nb, na = stage_names(rb.stages), stage_names(ra.stages)
    # 頁籤／名稱推導（H2 _sheet_name／_stage_title；名稱對照依 Stage.no，原始 Stage.no 不變）
    if not structural:
        if [s.no for s in rb.stages] != [s.no for s in ra.stages] or \
                [(s.start.t, s.A.t, s.dir) for s in rb.stages] != [(s.start.t, s.A.t, s.dir) for s in ra.stages]:
            problems.append("階段切分／起訖不同（stage_label／stage_count 不得改變階段）")
        exp_b = [_sheet_name(i, nb, s) for i, s in enumerate(rb.stages, 1)]
        exp_a = [_sheet_name(i, na, s) for i, s in enumerate(ra.stages, 1)]
        sb, sa_ = wb_b.sheetnames, wb_a.sheetnames

        def shape(names, exp):
            return [("階段頁", exp.index(x)) if x in exp else ("其他", x) for x in names]
        if len(sb) != len(sa_):
            problems.append(f"工作表數量不同：{len(sb)} → {len(sa_)}")
        elif [x for x in sb if x in exp_b] != exp_b or [x for x in sa_ if x in exp_a] != exp_a:
            problems.append("階段工作表名稱不是由 H2 命名規則（_sheet_name）推導")
        elif shape(sb, exp_b) != shape(sa_, exp_a):
            problems.append("工作表順序或非階段工作表名稱改變")
        tb = {s.no: f"{nb[s.no]}（{s.label}）" if s.label and getattr(s, "counted", True) else nb[s.no] for s in rb.stages}
        ta = {s.no: f"{na[s.no]}（{s.label}）" if s.label and getattr(s, "counted", True) else na[s.no] for s in ra.stages}
        ren, origin = {}, {}                        # 舊字串 → 可能的新字串（不計次階段的標題與名稱相同，
        for no in tb:                               # 計次後卻分成「第N次…（標記）」與「第N次…」兩種 → 多個候選）
            for o, n in ((tb[no], ta.get(no)), (nb[no], na.get(no))):
                if n is not None and o != n:
                    ren.setdefault(o, set()).add(n)
                    origin.setdefault(o, set()).add(no)
        for i, (x, y) in enumerate(zip(sb, sa_)):
            if x != y:
                ren.setdefault(x, set()).add(y)
                origin.setdefault(x, set()).add(rb.stages[exp_b.index(x)].no if x in exp_b else None)
        bad_origin = sorted(o for o, src in origin.items() if len(src) != 1 or None in src)
        if bad_origin:                              # 多候選只允許來自同一原始階段（Stage.no）的 H2 推導結果
            problems.append(f"名稱對照來源不唯一或不是階段（不可接受）：{bad_origin[:5]}")
        rename = sorted(((o, sorted(n)) for o, n in ren.items()), key=lambda kv: -len(kv[0]))
        item["detail"]["rename_map"] = rename
        ren_re = re.compile("|".join(re.escape(o) for o, _n in rename)) if rename else None

        def renamed_ok(u, v):                       # 單次同時替換（避免「第3次→第2次→中止」連鎖替換）；每處取一個候選
            if not ren_re:
                return u == v
            pat, pos = [], 0
            for m in ren_re.finditer(u):
                pat.append(re.escape(u[pos:m.start()]))
                pat.append("(?:" + "|".join(re.escape(n) for n in sorted(ren[m.group(0)], key=len, reverse=True)) + ")")
                pos = m.end()
            pat.append(re.escape(u[pos:]))
            return re.fullmatch("".join(pat), v) is not None
        if not problems:
            fmt_cells, fmt_rules = [], []
            dec_lines = [ln for ln, dd in zip(bd["added_lines"], added) if dd.get("type") in NAME_ONLY_TYPES]
            for x, y in zip(sb, sa_):
                A, B = wb_b[x], wb_a[y]
                if x == y and x in TITLE_FILL_SHEETS:   # r8 FMT：H2 標題底色規則推導的合法空白底色格（只限第二階段）
                    allowed, rinfo, rp = title_fill_rule(x, A, B, rb, ra, nb, na)
                    fmt_rules.append(rinfo)
                    problems += rp
                    if not rp:
                        p2, cells = _compare_title_sheet(x, A, B, allowed, renamed_ok)
                        problems += p2
                        for cc in cells:
                            cc.update(session=sa_a.get("cert_id"), sheet=x, tcols_base=rinfo["tcols_base"],
                                      tcols_all=rinfo["tcols_all"], px_base=rinfo["px_base"], px_all=rinfo["px_all"],
                                      decision_lines=dec_lines,
                                      reason="H2 _title_cols：全頁最寬標題決定底色欄數；合法決策改變標題寬度 → 空白底色格依規則增減")
                        fmt_cells += cells
                    continue
                if _dims(A) != _dims(B) or _tables(A) != _tables(B):
                    problems.append(f"{y}：已使用範圍或表格不同（結構差異）")
                    continue
                for r1, r2 in zip(A.iter_rows(), B.iter_rows()):
                    for c1, c2 in zip(r1, r2):
                        u, v = _norm(c1.value), _norm(c2.value)
                        if u == v:
                            continue
                        if isinstance(u, str) and isinstance(v, str) and renamed_ok(u, v):
                            continue
                        problems.append(f"{y}!{c2.coordinate}：{u!r} → {v!r}")
                        if len(problems) > 20:
                            break
            item["detail"]["format_rule"] = fmt_rules
            item["detail"]["format_cells"] = fmt_cells
        if wb_b.properties.title != wb_a.properties.title:
            problems.append("活頁簿標題不同")
        # X10：計次描述（各自依 R_base／R_all 的有效決策集合重算，逐字一致；非計次描述不得變動）
        db, da = wb_b.properties.description or "", wb_a.properties.description or ""
        cb, ca = stage_count_notes(rb_full), stage_count_notes(ra_full)
        x10 = {"expected_base": cb, "expected_all": ca, "truncated_3000": len(db) >= 3000 or len(da) >= 3000}
        if x10["truncated_3000"]:
            item.update(result=UNVERIFIABLE, reason_code="X10_TRUNCATED")
        else:
            restb, _x = _desc_split(db, cb)
            resta, _y = _desc_split(da, ca)
            if restb is None or resta is None:
                problems.append("計次描述與依決策集合重算的內容不一致（X10）")
            elif restb != resta:
                problems.append("非計次描述行改變（X10 不吸收；含實際分析參數說明）")
            hb = {s.no: [_hist_key(h) for h in getattr(s, "count_history", None) or []] for s in rb_full.stages}
            ha = {s.no: [_hist_key(h) for h in getattr(s, "count_history", None) or []] for s in ra_full.stages}
            for no, h in hb.items():
                if ha.get(no, [])[:len(h)] != h:
                    problems.append(f"階段 {no} 原有計次決策歷史遺失或改變")
            ob = [(o.get("stage_start"), o.get("n")) for o in getattr(rb, "count_orphans", None) or []]
            oa = {o.get("stage_start"): o.get("n") for o in getattr(ra, "count_orphans", None) or []}
            for k, n in ob:
                if oa.get(k, 0) < n:
                    problems.append(f"原有無法對應的計次決策 {k!r} 遺失")
        x10["result"] = "UNVERIFIABLE" if x10["truncated_3000"] else ("FAILED" if problems else "VERIFIED")
    else:
        # 結構性人工決策：完整列出；Raw Data 時間與數值欄必須不變（不得作為通用豁免）
        rb_rows, ra_rows = _raw_rows(wb_b), _raw_rows(wb_a)
        nv = len(RAW1_HEADERS)
        if len(rb_rows) != len(ra_rows):
            problems.append(f"Raw Data 列數不同：{len(rb_rows)} → {len(ra_rows)}")
        else:
            for i, (p, q) in enumerate(zip(rb_rows, ra_rows)):
                if p[0] != q[0] or list(p[2:nv - 1]) != list(q[2:nv - 1]):
                    problems.append(f"Raw Data 第 {i + 2} 列時間或數值欄改變")
                    break
        item["detail"]["structural"] = "新增決策含 stage_label／stage_count 以外的類型（結構性人工決策）：差異完整列出，不套用名稱限制"
    item["detail"].update(x10=x10, problems=problems[:30], n_problems=len(problems),
                          added_types=sorted({str(d.get("type", "continuity")) for d in added}))
    if problems:
        item.update(result=FAILED, reason_code="DECISION_DIFF_OUT_OF_SCOPE")
    return item, changes


def _after(records, base_at, keys):
    """基準報告時間之後是否有紀錄；基準時間未知且有紀錄 → None（無法判定）。"""
    if not records:
        return False
    if base_at is None:
        return None
    for r in records:
        for k in keys:
            t = _t(r.get(k))
            if t is not None and t > base_at:
                return True
    return False


def _norm(v):
    return None if v in (None, "") else v


def _dims(ws):
    return ws.max_row, ws.max_column


def _tables(ws):
    """表格名稱 → 範圍。openpyxl 的 ws.tables.items() 回傳 (名稱, 範圍字串)；也接受表格物件（取 .ref）。
    名稱與範圍都納入比較（Q-B）；範圍無法取得 → 以 None 記錄（與另一邊不同即 FAILED，不略過）。"""
    out = {}
    for n, t in ws.tables.items():
        out[n] = t if isinstance(t, str) else getattr(t, "ref", None)
    return out


def _cmp_sheet(kind, name, a, b, newer):
    item = {"item": f"{kind} {name}", "result": VERIFIED, "reason_code": None, "detail": None}
    if _dims(a) != _dims(b):
        item.update(result=FAILED, reason_code="STRUCTURE", detail={"original": _dims(a), "regenerated": _dims(b)})
        return item
    if _tables(a) != _tables(b):
        item.update(result=FAILED, reason_code="TABLE_MISMATCH", detail={"original": _tables(a), "regenerated": _tables(b)})
        return item
    diffs, formula = [], False
    for ra, rb in zip(a.iter_rows(), b.iter_rows()):
        for ca, cb in zip(ra, rb):
            x, y = _norm(ca.value), _norm(cb.value)
            if x != y:
                if (isinstance(x, str) and x.startswith("=")) or (isinstance(y, str) and y.startswith("=")):
                    formula = True
                diffs.append(f"{ca.coordinate}: {x!r} → {y!r}")
    if not diffs:
        return item
    if newer:
        item.update(result=UNVERIFIABLE, reason_code="X7" if kind == "V-S" else "X7/X8",
                    detail={"reason": "基準報告之後有新的操作人員決策或事後補拍", "diffs": diffs[:20], "n": len(diffs)})
        return item
    note = "可能原因包含 X6（參數差異），工具無法區分" + ("；基準時間未知，無法判定 X7／X8" if newer is None else "")
    item.update(result=FAILED, reason_code="FORMULA_DIFF" if formula else "UNEXPLAINED_VALUE_DIFF",
                detail={"diffs": diffs[:20], "n": len(diffs), "note": note})
    return item


def _raw_rows(wb):
    rows = []
    for name in wb.sheetnames:
        if name.startswith(RAW_PREFIX):
            ws = wb[name]
            for i, r in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                rows.append(tuple(_norm(v) for v in r))
    return rows


def _cmp_raw(wo, wn, rs, sa):
    """V-R：列數相同；不同的列必須全部符合 X1（含 X2）。"""
    item = {"item": "V-R Raw Data", "result": VERIFIED, "reason_code": None, "detail": None}
    ro, rn = _raw_rows(wo), _raw_rows(wn)
    if len(ro) != len(rn):
        item.update(result=FAILED, reason_code="ROWCOUNT", detail={"original": len(ro), "regenerated": len(rn)})
        return item
    regen = (rs or {}).get("rows") or []
    if len(regen) != len(rn):
        item.update(result=FAILED, reason_code="ROWCOUNT",
                    detail={"regenerated_sheet": len(rn), "regenerated_rows_in_memory": len(regen)})
        return item
    p, nf = sa["params"], sa["names_full"]
    bad, x1 = [], 0
    for i, (a, b) in enumerate(zip(ro, rn)):
        if a == b:
            continue
        failed = x1_conditions(ro, i, b, regen[i], p, nf)
        if failed:
            bad.append({"row": i + 2, "time": str(a[0]), "failed": failed})
        else:
            x1 += 1
    if bad:
        item.update(result=FAILED, reason_code="RAW_ROW_NOT_X1", detail={"rows": bad[:20], "n": len(bad), "x1_rows": x1})
    else:
        item["detail"] = {"x1_rows": x1, "rows": len(ro)}
    return item


def x1_conditions(ro, i, b, rrow, p, nf):
    """
    REV-1 V-R 條件 (a)～(f)。ro＝原始列（已正規化）、i＝列序、b＝重產列（已正規化）、rrow＝重產每秒列（含來源觀測 obs）。
    回傳不成立的條件清單（空＝屬於 X1）。任何條件無法驗證（缺來源觀測、缺旗標欄位、無前一列）都視為不成立。
    數值欄＝第 3～12 欄（SOC～功率 kW），說明欄＝最後一欄。
    """
    a = ro[i]
    nv = len(RAW1_HEADERS)
    vals = slice(2, nv - 1)
    failed = []
    if a[0] != b[0]:
        failed.append("(a) 時間欄不同")
    if i == 0:
        failed.append("(b) 無前一列，無法證明整列沿用")
    else:
        prev = ro[i - 1]
        if list(a[vals]) != list(prev[vals]):
            failed.append("(b) 原始列數值欄不等於前一列")
        if a[nv - 1] != prev[nv - 1]:
            failed.append("(b) 原始列說明欄不等於前一列")
    o = (rrow or {}).get("obs")
    if o is None:
        failed.append("(c) 重產列沒有來源觀測（無法驗證）")
        failed.append("(e) 無來源觀測，數值欄無法追溯")
        failed.append("(f) 無來源觀測，說明欄無法追溯")
    else:
        flags = o.flags or {}
        if "pcs_charging_flag" not in flags and "pcs_discharging_flag" not in flags:
            failed.append("(c) 來源觀測缺少 PCS 充放電旗標欄位（無法證明旗標延遲）")
        elif not (flags.get("pcs_charging_flag") is True or flags.get("pcs_discharging_flag") is True):
            failed.append("(c) 來源觀測的 PCS 充放電旗標皆非 True（不是旗標延遲）")
        if o.direction != "I":
            failed.append(f"(c) 來源觀測方向為 {o.direction!r}，不是待機 I")
        if not raw_measurement_valid(o, p):
            failed.append("(c) 來源觀測 raw_measurement_valid 為假")
        if is_report_valid(o, p, nf):
            failed.append("(c) 來源觀測 is_report_valid 為真（狀態可判定，不屬旗標延遲）")
        for k, cell in zip(RAW_VALUE_KEYS, b[vals]):
            exp = pcs_display(o, p)[0] if k == "pcs_module_c" else _v(field_value(o, k, p))
            if _norm(cell) != _norm(exp):
                failed.append(f"(e) 重產 {k} 欄 {cell!r} ≠ 來源觀測讀值 {exp!r}")
        note = _norm(_raw1_note(rrow, p) or None)
        if b[nv - 1] != note:
            failed.append("(f) 重產說明欄與依來源觀測重新計算的內容不同")
    if b[1] != NA:
        failed.append(f"(d) 重產列狀態為 {b[1]!r}，不是 N/A")
    return failed


def _cmp_desc(a, b):
    item = {"item": "V-D 活頁簿描述", "result": VERIFIED, "reason_code": None, "detail": None}
    la, lb = set(a.split("\n")), set(b.split("\n"))
    extra = sorted((la - lb) | (lb - la))

    def allowed(line):
        return line.startswith(X3_PREFIXES) or line.startswith(X4_PREFIX) or line.startswith("本檔由跨 Session 整合工具")
    unknown = [x for x in extra if not allowed(x)]
    trunc = len(a) >= 3000 or len(b) >= 3000
    if unknown:
        item.update(result=FAILED, reason_code="DESC_UNKNOWN_LINE",
                    detail={"lines": [x[:120] for x in unknown], "truncated_3000": trunc})
    else:
        item["detail"] = {"allowed_diff_lines": [x[:60] for x in extra], "truncated_3000": trunc}
    return item


def _cmp_title(a, b):
    item = {"item": "V-T 活頁簿標題", "result": VERIFIED, "reason_code": None, "detail": {"original": a, "regenerated": b}}
    if a == b or (b == DETAIL_TITLE and a == ORIGINAL_TITLE):
        return item
    item.update(result=FAILED, reason_code="TITLE_UNKNOWN")
    return item


def _final(out, forced=None, reason=None):
    if forced:
        out.update(result=forced, reason=reason, reason_codes=[])
    else:
        rs = [i["result"] for i in out["items"]]
        codes = sorted({i["reason_code"] for i in out["items"] if i["result"] == FAILED and i["reason_code"]})
        if FAILED in rs:
            out.update(result=FAILED, reason_codes=codes, reason="有無法以 X1～X9 解釋的差異")
        elif UNVERIFIABLE in rs:
            out.update(result=UNVERIFIABLE, reason_codes=[],
                       reason="；".join(f"{i['item']}：{i['reason_code']}" for i in out["items"] if i["result"] == UNVERIFIABLE))
        else:
            out.update(result=VERIFIED, reason_codes=[], reason="")
    if out["result"] == VERIFIED:
        out["display"] = "VERIFIED"
    elif out["result"] == UNVERIFIABLE:
        out["display"] = f"UNVERIFIABLE（{out['reason']}）"
    else:
        out["display"] = f"FAILED（{'、'.join(out['reason_codes'])}）"
    return out
