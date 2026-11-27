# -*- coding: utf-8 -*-
"""
跨 Session 整合認證報告 —— Excel 產生（docs/Cert_Merge_Report_Design.md §9）
=========================================================================
  * 重產明細：以**未修改的** cert_report_xlsx.save_report 產生；報告設定（meta）依 cert_runtime.generate_report
    在此重建（N-3；不 import cert_runtime）；存檔後只以 zip 層級改寫 docProps/core.xml 的標題（N-1）。
  * 整合活頁簿：9 頁、不含任何圖表；第一屏只寫「產生本檔時的狀態」（§9.5）。
只讀快照；所有寫入路徑由呼叫端的寫入守門檢查。
"""
import json
import os
import re
import zipfile
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

import cert_heat_link as HL
import cert_report_config as CFG
import cert_supplement as SUP
import report_quality as RQ
from cert_report_xlsx import EXCLUDE_STOPPED_KEY, OVERALL_RANGE_KEY, format_flags, save_report
from cert_screenshot import load_manifest

DETAIL_TITLE = "重產明細（非原始認證紀錄）"
ORIGINAL_TITLE = "充／放電認證測試報告"
DISCLAIMER_PREFIX = "本檔由跨 Session 整合工具"
SHEETS = ["整合Summary", "Session總覽", "階段清單", "循環判定", "不確定停頓與標記紀錄",
          "資料品質", "參數與條件", "版本資訊", "來源追溯"]
LABEL_RULE_NOTE = ("整合報告的有效標記規則比 Menu 22 嚴格：最後一筆標記方向無效時不採用、不回退；"
                   "重產明細與原始 Menu 22 報告沿用原規則。")
VERIFY_SCOPE_NOTE = ("資料值驗證只涵蓋儲存格值、公式文字、工作表結構、表格與指定中繼資料；"
                     "圖表、圖片、注入圖形與格式未驗證。")


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        try:
            return json.loads(f.read().decode("utf-8"))
        except ValueError:
            return None


# =============================================================================
# 對照 cert_runtime（N-3；文字與邏輯逐字沿用，供重產明細與正式流程一致）
# =============================================================================
HMI_READER_DESC = {
    "read_cert": "認證專用讀取 read_cert（每次 5 支 GET：主控資訊、Rack 極值、PCS 英文／中文、電池上下電）",
    "read_all": "完整讀取 read_all（每次 9 支 GET，另含當日電量、PCS 模式、排程開關、告警）",
}


def params_note(params):
    cur = CFG.SAMPLE_INTERVAL_SEC
    n = (params or {}).get("SAMPLE_INTERVAL_SEC")
    iv = (f"取樣週期 {cur} 秒為正式設定" if n in (None, cur)
          else f"取樣週期 {n} 秒（本次認證開始時的設定；目前正式設定 {cur} 秒）")
    return f"{iv}；其他參數（G、確認門檻、待機電流門檻、每分鐘容許範圍、截圖等待／重試）為暫用測試值，非現場正式值"


def hmi_reader_note(st):
    st = st or {}
    cur, chg = st.get("hmi_reader"), st.get("hmi_reader_changes") or []
    if not cur and not chg:
        return None
    desc = lambda m: HMI_READER_DESC.get(m, str(m))       # noqa: E731
    if chg:
        parts = [f"開始時為 {desc(chg[0].get('from'))}"
                 + ("（未記錄，依程式版本推定）" if not chg[0].get("from_recorded", True) else "")]
        parts += [f"{c.get('at')} 起改為 {desc(c.get('to'))}" for c in chg]
        head = "HMI 資料來源：" + "；".join(parts) + "。"
    else:
        head = f"HMI 資料來源：{desc(cur)}。"
    return head + ("格點判定規則不變；「部分端點失敗」只計實際讀取的端點；"
                   "read_cert 不讀 PCS 模式／排程／告警（認證不使用），因此 401 次數減少是端點數減少，不代表登入狀態改善。")


def _pct(a, b):
    return None if not b else round(100.0 * a / b, 2)


class ENV:
    """cert_env 的逐字重建（cert_env → env_data_scraper → api_client；規格禁止 import api_client，故不 import cert_env）。"""
    SNAPSHOT_FILE = "env_start_snapshot.json"
    NORMAL, ABNORMAL, READ_FAIL = "正常", "異常", "讀取失敗"

    @staticmethod
    def _temp(x):
        try:
            v = float(str((x or {}).get("value")).strip())
        except (TypeError, ValueError):
            return None
        return f"{int(v) if v.is_integer() else v}°C"

    @staticmethod
    def summary_lines(snap, test_info=None):
        """
        回傳 (冷氣溫度文字, 硬體概述文字)（不含「冷氣溫度：」標籤）。
        有快照 → 依快照（認證開始時）；沒有快照（舊認證）→ 沿用人工輸入並標示（人工輸入），沒有則 N/A。不連 HMI。
        """
        if isinstance(snap, dict) and snap.get("schema"):
            ac = snap.get("aircon") or {}
            c, h = ENV._temp(ac.get("cooling")), ENV._temp(ac.get("heating"))
            aircon = "N/A" if c is None and h is None else f"製冷 {c or 'N/A'}／製熱 {h or 'N/A'}"
            bad = [d["name"] for d in snap.get("devices") or [] if d.get("status") == ENV.ABNORMAL]
            hardware = "；".join(f"{n}：{ENV.ABNORMAL}" for n in bad) if bad else "N/A"
            return aircon, hardware
        ti = test_info or {}
        aircon = f"{ti['aircon']}（人工輸入）" if str(ti.get("aircon") or "").strip() else "N/A"
        hardware = f"{ti['hardware']}（人工輸入）" if str(ti.get("hardware") or "").strip() else "N/A"
        return aircon, hardware

    @staticmethod
    def failure_note(snap):
        """報告來源說明：起始環控快照有端點讀取失敗時如實註明（讀取失敗不列為設備異常）。"""
        if not isinstance(snap, dict) or not snap.get("schema"):
            return None
        failed = [n for n, e in (snap.get("endpoints") or {}).items() if not e.get("ok")]
        if not failed:
            return None
        return (f"認證開始環控快照（{snap.get('captured_at')}）：{ '、'.join(failed)} 讀取失敗"
                "（相關設備狀態無法取得，不列為異常）")


# >>> BUILD_SAMPLING_SLOTS（由 cert_runtime.build_sampling_slots 逐字複製；只把 REQ.read_json 換成 _read_json）
def build_sampling_slots(folder):
    """
    M-S2：格點品質指標＋Raw Data 每秒列的輸入。沒有 slots.jsonl（歷史 5 秒認證等）→ None（Raw Data 維持原格式）。
    來源只有**已保存**的 observations.jsonl、slots.jsonl 與 cert_state.json（started_at、interruptions）：
      - 格點：report_quality.reconstruct（依段落起點與已知格點重建；SLOT 遺失 → ATTEMPTED_SLOT_LOST／INFERRED_MISSED）
      - 品質指標：report_display.quality_metrics（覆蓋率、M-1、完整即時資料率等；寫入報告描述）
    回傳 {"metrics", "grid"}；grid 交給 cert_report_xlsx 產生 Raw Data 每秒列（report_display.build_raw_seconds）。
    """
    import report_display as RD
    path = os.path.join(folder, RQ.SLOT_LOG_FILE)
    if not os.path.exists(path):
        return None
    recs, san = RQ.read_slot_log(path)
    raw, ran = RQ.load_cert_raw(os.path.join(folder, "observations.jsonl"))
    result = RQ.reconstruct(recs, raw, san, ran)
    if not result.get("has_slot_log"):
        return None
    disp = RD.build_display(result, raw, "cert")
    qm = RD.quality_metrics(result, raw, "cert", display=disp)
    slots = result.get("slots", [])
    expected = len(slots)
    cnt = {}
    for e in slots:
        cnt[e["status"]] = cnt.get(e["status"], 0) + 1
    logged = sum(1 for e in slots if e.get("source") == "logged")
    obs_fail = sum(1 for e in slots if e.get("obs_write_failed"))
    hmi_ok = (qm.get("hmi") or {}).get("hmi_valid") or 0
    an = dict(qm.get("anomaly_counts") or {})
    unterminated = qm.get("unterminated_segments") or 0
    raw_bad = qm.get("incomplete_raw_records") or 0
    slot_bad = an.get(RQ.AN_TAIL_INCOMPLETE, 0) + an.get(RQ.AN_LINE_UNPARSEABLE, 0)
    certain = (unterminated == 0 and raw_bad == 0 and slot_bad == 0 and cnt.get(RQ.INFERRED_MISSED, 0) == 0
               and cnt.get(RQ.ATTEMPTED_SLOT_LOST, 0) == 0 and cnt.get(RQ.ATTEMPTED_CONFLICT, 0) == 0)
    cov = _pct(logged, expected)
    if cov is None:
        cov_text = "N/A（沒有格點）"
    elif unterminated:
        cov_text = f"不確定（{cov:.2f}%；有 {unterminated} 個段落未正常結束，尾端格點未知）"
    elif certain and logged == expected:
        cov_text = "100%"
    else:
        cov_text = f"{cov:.2f}%（有紀錄遺失或異常，見下方）" if not certain else f"{cov:.2f}%"
    # 段落：SEGMENT_END 的 end_wall（服務停止／重啟統計的起點）
    seg_end_wall = {r.get("seg_id"): r.get("end_wall") for r in (recs or []) if r.get("rec") == RQ.REC_SEGMENT_END}
    seg_info = {s["seg_id"]: s for s in result.get("segments", [])}
    seg_order = []
    for e in slots:
        if not seg_order or seg_order[-1] != e["seg_id"]:
            seg_order.append(e["seg_id"])
    last_evidence = {}
    for e in slots:
        t = e.get("read_done") or e.get("scheduled_time")
        if t:
            last_evidence[e["seg_id"]] = t
    down_total, down_n = 0.0, 0
    prev_sid = None
    for sid in seg_order:
        if prev_sid is not None:
            t_from = seg_end_wall.get(prev_sid) or last_evidence.get(prev_sid)
            t_to = (seg_info.get(sid) or {}).get("anchor_wall")
            sec = None
            a, b = RD.parse_time(t_from), RD.parse_time(t_to)
            if a is not None and b is not None:
                sec = round((b - a).total_seconds(), 1)
                if sec >= 0:
                    down_total += sec
            down_n += 1
        prev_sid = sid
    fq_all = qm.get("field_quality") or {}
    m = {"expected_slots": expected, "logged_slots": logged, "coverage_pct": cov, "coverage_text": cov_text,
         "coverage_certain": certain and not unterminated,
         "attempted_slots": qm.get("attempted_slots"), "attempt_pct": _pct(qm.get("attempted_slots") or 0, expected),
         "hmi_success_slots": hmi_ok, "m1_pct": _pct(hmi_ok, expected),
         "complete_live_slots": qm.get("complete_live_slots"),
         "complete_live_pct": _pct(qm.get("complete_live_slots") or 0, expected),
         "status_counts": cnt, "missed": cnt.get(RQ.MISSED, 0), "inferred_missed": cnt.get(RQ.INFERRED_MISSED, 0),
         "slot_lost": cnt.get(RQ.ATTEMPTED_SLOT_LOST, 0), "read_fail": cnt.get(RQ.ATTEMPTED_FAIL, 0),
         "obs_write_failed": obs_fail, "segments": len(seg_order), "unterminated_segments": unterminated,
         "service_down_count": down_n, "service_down_sec": round(down_total, 1),
         "raw_unparseable": raw_bad, "slot_log_unparseable": slot_bad, "anomaly_counts": an,
         "field_quality": {f: {"name": d.get("name"), "counts": d.get("counts"),
                               "max_carried_age_sec": d.get("max_carried_age_sec")} for f, d in fq_all.items()}}
    st = _read_json(os.path.join(folder, "cert_state.json")) or {}
    grid = {"result": result, "raw": raw, "started_at": st.get("started_at"),
            "interruptions": list(st.get("interruptions") or [])}
    return {"metrics": m, "grid": grid}
# <<< BUILD_SAMPLING_SLOTS


def _baseline_format(folder, st):
    """原始比較基準報告的格式識別（活頁簿 category）；沒有基準 → ""（舊規則）。未知值原樣回傳（驗證端判 UNVERIFIABLE）。"""
    import cert_merge_source as SRC
    import cert_merge_verify as VF
    _reps, base = VF.report_list(folder, st.get("cert_id"), st, SRC.sha256_file)
    return VF.report_format(base["path"]) if base else ""


def detail_meta(sa, disclaimer):
    """對照 cert_runtime.generate_report 的 meta 組裝（rt＝離線 runtime；extra_notes＝[重產聲明]；shot_note＝""）。"""
    folder = sa["data"]["folder"]
    st = sa["state"] or {}
    cid = st.get("cert_id")
    records = sa["data"]["records"]
    p = sa["params"]
    extra_notes = [disclaimer]
    ti = st.get("test_info") or {}
    notes = [f"認證測試 {cid}：背景服務自 {st.get('started_at')} 起持續取樣（含休息），"
             f"至 {st.get('ended_at') or '（記錄中）'}；共 {len(records)} 筆。",
             "原始讀值照存：不沿用上一筆、不補 0、不內插；讀取失敗或欄位無效 → 該分鐘 N/A、曲線斷線。",
             params_note(st.get("params")) + "。",
             "傳感器溫度／PCS 模組溫度取自 6161 /api/temperature（st／pt.moduleTemperature），對應關係待實機確認。",
             "停止指令時間（Tc）取自控制結果檔的快照（寫檔時間；該檔只保留最近一次，非完整歷史）。"]
    rd_note = hmi_reader_note(st)
    if rd_note:
        notes.append(rd_note)
    for it in st.get("interruptions") or []:
        notes.append(f"服務中斷：{it.get('from')}～{it.get('to')}（約 {it.get('seconds') or 0:.0f} 秒）"
                     f"{it.get('reason')} → 期間無資料")
    notes = list(extra_notes) + notes
    if st.get("incomplete"):
        notes.insert(0, f"⚠ 未完成：{st['incomplete']}")
    if st.get("status") == "recording":
        notes.insert(0, "⚠ 本報告產生時認證仍在記錄中（中途報告）")
    meta_cmds = list(sa["data"]["commands"])
    env_snap = _read_json(os.path.join(folder, ENV.SNAPSHOT_FILE))
    aircon_txt, hardware_txt = ENV.summary_lines(env_snap, ti)
    env_note = ENV.failure_note(env_snap)
    if env_note:
        notes.append(env_note)
    meta = {"conditions": [f"冷氣溫度：{aircon_txt}",
                           f"硬體概述：{hardware_txt}",
                           f"測試條件：{ti.get('conditions') or '（未輸入）'}"],
            "source_notes": notes, "cabinet_spec": ti.get("cabinet_spec"), "cycles": ti.get("cycles"),
            "commands": meta_cmds}
    # 依原始報告格式選擇重產規則（r8 補充；v2 補充）：Legacy＝兩旗標皆無、v1＝只排除、v2＝排除＋整體有效範圍；
    # 未知格式 → 照舊以 Legacy 規則重產（驗證端判 UNVERIFIABLE，不猜測）
    flags = format_flags(_baseline_format(folder, st)) or format_flags("")
    meta[EXCLUDE_STOPPED_KEY] = flags[EXCLUDE_STOPPED_KEY]
    meta[OVERALL_RANGE_KEY] = flags[OVERALL_RANGE_KEY]
    supp = SUP.has_supplements(folder)
    if os.path.exists(os.path.join(folder, "screenshots.jsonl")) or os.path.isdir(os.path.join(folder, "screenshots")):
        hl, _evs = HL.replay(records, p["GAP_G_SEC"], CFG.CERT_HEAT_LINK_ALERT_SEC)
        meta["screenshots"] = {"folder": folder, "records": load_manifest(folder), "note": "",
                               "heat_link": {"runs": hl.all_runs(), "gap_sec": p["GAP_G_SEC"]}}
        if supp:
            meta["screenshots"]["supplements"] = {"folder": SUP.folder_of(folder), "records": SUP.load(folder)}
    ss = build_sampling_slots(folder)
    if ss is not None:
        meta["sampling_slots"] = ss
        mm = ss["metrics"]
        pct = lambda v: "N/A" if v is None else f"{v}%"      # noqa: E731
        q_note = (f"格點品質：預定格點 {mm['expected_slots']}；格點紀錄覆蓋率 {mm['coverage_text']}；"
                  f"實際執行讀取率 {pct(mm['attempt_pct'])}；"
                  f"HMI 實際成功率（M-1，分母＝預定格點）{pct(mm['m1_pct'])}；"
                  f"完整即時資料率 {pct(mm['complete_live_pct'])}；讀取失敗 {mm['read_fail']} 格；"
                  f"未執行讀取（MISSED）{mm['missed']} 格；推定缺格 {mm['inferred_missed']} 格；"
                  f"格點紀錄遺失 {mm['slot_lost']} 格；原始紀錄寫入失敗 {mm['obs_write_failed']} 格；"
                  f"段落 {mm['segments']}（未正常結束 {mm['unterminated_segments']}）；"
                  f"服務停止／重啟 {mm['service_down_count']} 次共 {mm['service_down_sec']} 秒；"
                  f"無法解析的紀錄行 {mm['raw_unparseable']}／{mm['slot_log_unparseable']}。")
        notes.insert(min(len(notes), 3 + len(extra_notes) + (1 if st.get("incomplete") else 0)
                         + (1 if st.get("status") == "recording" else 0)), q_note)
    return meta, ss


# =============================================================================
# 重產明細（M9）與標題改寫（N-1）
# =============================================================================
_TITLE_RE = re.compile(rb"<dc:title>(.*?)</dc:title>", re.S)


def write_detail(sa, path, disclaimer, guard):
    guard.check(path)
    meta, ss = detail_meta(sa, disclaimer)
    save_report(sa["res"], meta, path)
    set_title(path, DETAIL_TITLE, guard)
    return ss


def set_title(path, title, guard):
    """
    只改寫 docProps/core.xml 的 <dc:title>（不以 openpyxl 重新開啟另存，避免遺失注入的圖形）。
    改寫後檢查：除 core.xml 以外每個壓縮項目（名稱與解壓內容）逐項相同；core.xml 只有標題不同。不符 → 例外。
    """
    guard.check(path)
    tmp = guard.check(path + ".title_tmp")
    with zipfile.ZipFile(path) as zi:
        infos = zi.infolist()
        before = {i.filename: zi.read(i.filename) for i in infos}
    core = before.get("docProps/core.xml")
    if core is None or not _TITLE_RE.search(core):
        raise RuntimeError("重產明細沒有可改寫的標題（docProps/core.xml）")
    new_core = _TITLE_RE.sub(b"<dc:title>" + escape(title).encode("utf-8") + b"</dc:title>", core, count=1)
    with zipfile.ZipFile(tmp, "x", zipfile.ZIP_DEFLATED) as zo:
        for i in infos:
            data = new_core if i.filename == "docProps/core.xml" else before[i.filename]
            zo.writestr(i, data)
    with zipfile.ZipFile(tmp) as zc:
        after = {i.filename: zc.read(i.filename) for i in zc.infolist()}
    if list(after) != list(before):
        raise RuntimeError("標題改寫後壓縮項目清單不同")
    for k in before:
        if k != "docProps/core.xml" and after[k] != before[k]:
            raise RuntimeError(f"標題改寫意外改動 {k}")
    if _TITLE_RE.sub(b"", after["docProps/core.xml"]) != _TITLE_RE.sub(b"", core):
        raise RuntimeError("標題改寫改動了 core.xml 標題以外的內容")
    os.replace(tmp, path)


# =============================================================================
# 整合活頁簿（M8）
# =============================================================================
FB = Font(bold=True)
HDR = PatternFill("solid", fgColor="DDEBF7")
WRAP = Alignment(wrap_text=True, vertical="top")


def _table(ws, r, headers, rows):
    for c, h in enumerate(headers, 1):
        cell = ws.cell(r, c, h)
        cell.font, cell.fill = FB, HDR
    for row in rows:
        r += 1
        for c, v in enumerate(row, 1):
            ws.cell(r, c, _cellv(v)).alignment = WRAP
    return r + 2


def _cellv(v):
    if v is None:
        return "N/A"
    if isinstance(v, (list, tuple)):
        return "；".join(str(x) for x in v) if v else ""
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


def _hms(sec):
    if sec is None:
        return None
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def write_summary(path, model, guard):
    """model：cert_merge_report 組好的整合資料（只讀記憶體內容，不讀快照）。"""
    guard.check(path)
    wb = Workbook()
    ws = wb.active
    ws.title = SHEETS[0]
    for name in SHEETS[1:]:
        wb.create_sheet(name)
    _sheet_summary(wb[SHEETS[0]], model)
    _sheet_sessions(wb[SHEETS[1]], model)
    _sheet_stages(wb[SHEETS[2]], model)
    _sheet_cycles(wb[SHEETS[3]], model)
    _sheet_pauses(wb[SHEETS[4]], model)
    _sheet_quality(wb[SHEETS[5]], model)
    _sheet_params(wb[SHEETS[6]], model)
    _sheet_version(wb[SHEETS[7]], model)
    _sheet_trace(wb[SHEETS[8]], model)
    for w in wb.worksheets:
        for col in "ABCDEFGHIJKLMNOPQRS":
            w.column_dimensions[col].width = 22
    wb.properties.title = "跨 Session 整合認證報告"
    wb.save(path)


def first_screen_lines(model):
    lines = [f"產生本檔時的狀態（{model['written_at']}）：",
             "  快照建立驗證（H1～H3）：一致",
             f"  計數不變式 I1～I12：{'成立' if all(model['invariants'].values()) else '不成立'}",
             "  資料值驗證：" + "、".join(f"S{v['s_no']} {v['display']}" for v in model["verify"]),
             "  原始參數：UNVERIFIABLE（原始報告未記錄）",
             f"  預定檔名：{model['planned_summary_name']}"]
    if model.get("unverified_sessions"):
        for u in model["unverified_sessions"]:
            lines.append(f"  未驗證 Session：S{u['s_no']} {u['cert_id']}；原因：{u['reason']}；影響範圍：{u['scope']}")
    lines += ["最終快照驗證（H5、H4）與正式發布：於本檔產生後執行，結果以同資料夾 STATUS.json 與 merge_manifest.json 為準。",
              "本檔只有在 STATUS.json 的 publish.state＝PUBLISHED、且 manifest 雜湊與各檔 SHA-256 相符時，"
              "才是正式發布的報告；檔名為 .partial 時一律無效。",
              LABEL_RULE_NOTE, VERIFY_SCOPE_NOTE]
    return lines


def _sheet_summary(ws, m):
    r = 1
    for ln in first_screen_lines(m):
        ws.cell(r, 1, ln).font = FB if r == 1 else Font()
        r += 1
    r += 1
    ws.cell(r, 1, m["title"]).font = Font(bold=True, size=14)
    r += 1
    c = m["counts"]
    s = c["status"]
    ws.cell(r, 1, f"Session {c['sessions']}｜原始階段：放電 {c['stages_D']}、充電 {c['stages_C']}"
                  f"｜有效階段：放電 {c['valid_D']}、充電 {c['valid_C']}｜中止排除 {c['stopped']}（其中不計次 {c['uncounted']}）"
                  f"｜已配對循環 {c['cycles']}"
                  f"｜配對確定性：確定 {c['certain']}、循環配對待確認 {c['pair_pending']}"
                  f"｜確認狀態：確認完成 {s['確認完成']}、未滿 {s['未滿']}、中止 {s['中止']}、待確認 {s['待確認']}、"
                  f"未完成 {s['未完成']}｜有效未配對階段 {c['unpaired']}")
    r += 2
    ws.cell(r, 1, "跨 Session 比較（所有已配對循環；只並列，不計算差值；不可比指標不得加總或比較）").font = FB
    rows = []
    for cy in m["cycles"]:
        a, b = cy["a"], cy["b"]
        rows.append([cy["global_id"], cy["id"], f"S{cy['s_no']}", cy["certainty"], cy["status"],
                     _span_txt(a), _span_txt(b), _rest_txt(cy), _hms(cy["observed_gap_sec"]) + "（觀測間隔，不是休息時間）",
                     cy["time_comparable"], f"{a['soc_start']}→{a['soc_end']}", f"{b['soc_start']}→{b['soc_end']}",
                     a["extremes"]["rack_max_c"], a["extremes"]["rack_min_c"], a["extremes"]["cell_vmax_v"],
                     a["extremes"]["cell_vmin_v"], b["extremes"]["rack_max_c"], b["extremes"]["rack_min_c"],
                     b["extremes"]["cell_vmax_v"], b["extremes"]["cell_vmin_v"], cy["ext_comparable"]])
    _table(ws, r + 1, ["循環", "循環 ID", "Session", "配對確定性", "確認狀態", "前半觀測運行跨度", "後半觀測運行跨度",
                       "休息時間", "觀測間隔", "時間量可比性", "前半 SOC（只顯示）", "後半 SOC（只顯示）",
                       "前半 Rack 最高溫", "前半 Rack 最低溫", "前半 Cell 最高電壓", "前半 Cell 最低電壓",
                       "後半 Rack 最高溫", "後半 Rack 最低溫", "後半 Cell 最高電壓", "後半 Cell 最低電壓", "極值可比性"], rows)


def _span_txt(s):
    if s["span_sec"] is not None:
        return f"{_hms(s['span_sec'])}（1 Hz 取樣，邊界誤差約 ±1 秒）"
    return f"N/A（邊界不確定）；觀測跨度（下限）{_hms(s['span_lower_sec'])}，實際可能更長"


def _rest_txt(cy):
    return _hms(cy["rest_sec"]) if cy["rest_sec"] is not None else f"N/A（{cy['rest_note']}）"


def _sheet_sessions(ws, m):
    rows = []
    for x in m["sessions"]:
        rows.append([f"S{x['s_no']}", x["cert_id"], x["started_at"], x["ended_at"], x["status"], x["sample_count"],
                     x["stages_D"], x["stages_C"], x["cycles"], x["status_counts"], x["version"]["declared_version"],
                     x["version"]["version_source"], x["version"]["version_verification"],
                     x["version"]["features"]["summary"], x["baseline_report"], x["detail_name"],
                     x["verify_display"], "UNVERIFIABLE（原始報告未記錄）"])
    r = _table(ws, 1, ["Session", "cert_id", "開始", "結束", "status", "sample_count", "放電階段", "充電階段", "已配對循環",
                       "確認狀態計數", "宣告版本", "宣告來源", "驗證狀態", "資料特徵摘要", "原始報告（比較基準）",
                       "重產明細（預定檔名）", "資料值驗證", "原始參數可追溯性"], rows)
    # r8：三種保證分開呈現（不合併成單一通過）
    ws.cell(r, 1, "決策與報告的三種保證（分開判定）").font = FB
    # 第 2 欄不放 cert_id（Session 總覽第 2 欄只供每 Session 一列的 cert_id 計數；Session 以 S 編號對照上表）
    g = [[f"S{v['s_no']}", v.get("decision_snapshot"), (v.get("decision_verification") or {}).get("path"),
          (v.get("decision_verification") or {}).get("reason"), v.get("reproducibility"), v.get("traceability"),
          v.get("traceability_note"), len(v.get("change_list") or [])] for v in m.get("verify") or []]
    _table(ws, r + 1, ["Session", "① 決策快照一致性", "判定路徑（new＝keywords／old＝時間）", "原因",
                       "② 報告內容重現性", "③ 原始決策集合可追溯性", "可追溯性備註", "人工決策變更清單筆數"], g)


def _sheet_stages(ws, m):
    rows = []
    for s in m["stages"]:
        st = s["stage"]
        rows.append([s["id"], f"S{s['s_no']}", s["no"], s["name"], s["dir"], s["stage_start"],
                     st.A.t.strftime("%Y-%m-%dT%H:%M:%S"), st.start_certain, st.end_certain, st.end_by,
                     s.get("rest_from_prev_same"), s["label_count"], s["engine_label"], s["effective_label"],
                     s["label_reasons"], s["pairing"], s["cycle_id"], f"{s['soc_start']}→{s['soc_end']}",
                     _span_txt(s)])
    _table(ws, 1, ["階段 ID", "Session", "序號", "名稱", "方向", "開始（stage_start）", "最後觀測運行", "start_certain",
                   "end_certain", "end_by", "與前一同向階段間隔（秒）", "標記歷史筆數", "分析程式套用的標記",
                   "整合判定採用的有效標記", "標記備註", "配對結果", "所屬循環", "SOC（只顯示）", "觀測運行跨度"], rows)
    ws.cell(len(rows) + 4, 1, "階段 ID、循環 ID 只在本報告內有效，不是永久識別碼；跨報告比對請用 cert_id＋stage_start。")


def _sheet_cycles(ws, m):
    rows = []
    for cy in m["cycles"]:
        rows.append([cy["global_id"], cy["id"], f"S{cy['s_no']}", cy["a"]["id"], cy["b"]["id"], _rest_txt(cy),
                     _hms(cy["observed_gap_sec"]), cy["certainty"], cy["certainty_triggers"], cy["status"],
                     cy["reasons"], [f"{u['bound']}：{u['note']}" for u in cy["uncertain_bounds"]]])
    r = _table(ws, 1, ["循環", "循環 ID", "Session", "前半階段", "後半階段", "休息時間", "觀測間隔", "配對確定性",
                       "配對待確認觸發", "確認狀態", "全部理由", "不確定邊界"], rows)
    stopped = "中止（不參與配對）"                       # = cert_merge_analysis.STOPPED_EXCLUDED（v2-align）
    un = [[s["id"], f"S{s['s_no']}", s["dir"], s["pairing"], s["effective_label"]]
          for s in m["stages"] if s["cycle_id"] is None and s.get("pairing") != stopped]
    ws.cell(r, 1, "有效未配對階段（不含中止排除）").font = FB
    r = _table(ws, r + 1, ["階段 ID", "Session", "方向", "配對結果", "有效標記"], un)
    # v2-align：中止階段（含不計次）不參與配對；原始階段、量測、階段頁、Raw Data、統計與決策紀錄全部保留
    unc = []
    for s in m["stages"]:
        if s.get("pairing") == stopped:
            h = (s.get("count_history") or [{}])[-1]
            unc.append([s["id"], f"S{s['s_no']}", s["stage_start"], s["dir"], "否" if s.get("count_counted", True) else "是",
                        s.get("count_reason"), h.get("at"), h.get("request_id"), s.get("count_note")])
    ws.cell(r, 1, "中止階段（不參與配對；原始資料完整保留）").font = FB
    r = _table(ws, r + 1, ["階段 ID", "Session", "stage_start", "方向", "不計次", "不計次原因", "決策時間", "request_id",
                           "說明"], unc)
    # 第 2 欄不放循環 ID（循環判定第 2 欄只供每循環一列的計數；以「循環」欄 CYn 對照上表）
    notes = [[cy["global_id"], n] for cy in m["cycles"] for n in cy.get("uncounted_notes") or []]
    ws.cell(r, 1, "循環加註（相鄰中止階段）").font = FB
    _table(ws, r + 1, ["循環", "加註"], notes)


def _sheet_pauses(ws, m):
    rows = [[f"S{z['s_no']}", z["item_id"], z["stage_id"], z["A"], z["R"], z["A_to_R_sec"], z["system_judgment"],
             z["decision"] or "待確認"] for z in m["pauses"]]
    r = _table(ws, 1, ["Session", "item_id", "所屬階段", "A（最後運行）", "R（恢復）", "A→R 秒", "系統判定（原文）",
                       "操作人員決策"], rows)
    ws.cell(r - 1, 1, "不推論不確定停頓的原因。")
    ws.cell(r, 1, "標記歷史（依檔案行序）").font = FB
    hist = [[f"S{h['s_no']}", h["line"], h["at"], h["request_id"], h["stage_start"], h["label"], h["validity"], h["note"]]
            for h in m["label_history"]]
    r = _table(ws, r + 1, ["Session", "行", "at", "request_id", "stage_start", "label", "有效性", "note"], hist)
    ws.cell(r, 1, "無法對應的標記／停頓決策／無法辨識的紀錄").font = FB
    orph = [[f"S{o['s_no']}", o["line"], o["kind"], o["at"], o["request_id"],
             json.dumps(o["content"], ensure_ascii=False, default=str)] for o in m["orphans"]]
    r = _table(ws, r + 1, ["Session", "行", "類別", "at", "request_id", "原始內容"], orph)
    # r8：計次決策歷史（H2 分析結果）、決策紀錄異常、人工決策變更清單（只呈現，不參與判定）
    ws.cell(r, 1, "計次決策歷史（stage_count；依檔案行序）").font = FB
    cr = [[f"S{c['s_no']}", c["line"], c["stage_start"], c["counted"], c["reason"], c["at"], c["by"], c["request_id"]]
          for c in m.get("count_records") or []]
    r = _table(ws, r + 1, ["Session", "行", "stage_start", "counted", "原因", "at", "by", "request_id"], cr)
    ws.cell(r, 1, "決策紀錄異常（可追溯性不得判為已確認）").font = FB
    an = [[f"S{a['s_no']}", a["kind"], a["request_id"], a["lines"]] for a in m.get("decision_anomalies") or []]
    r = _table(ws, r + 1, ["Session", "類別", "request_id", "筆序"], an)
    ws.cell(r, 1, "人工決策變更清單（原始報告產生後新增；只呈現，不參與判定）").font = FB
    ch = [[f"S{v['s_no']}", c["line"], c["type"], c["stage_start"] or c.get("item_id"), c.get("label"), c.get("counted"),
           c.get("reason"), c["at"], c["by"], c["request_id"]] for v in m.get("verify") or [] for c in v.get("change_list") or []]
    _table(ws, r + 1, ["Session", "實體行", "type", "stage_start／item_id", "label", "counted", "原因", "at", "by",
                       "request_id"], ch)


def _sheet_quality(ws, m):
    rows = []
    for q in m["quality"]:
        mm = q["metrics"] or {}
        rows.append([f"S{q['s_no']}", q["cert_id"], mm.get("expected_slots"), mm.get("coverage_text"),
                     mm.get("attempt_pct"), mm.get("m1_pct"), mm.get("complete_live_pct"), mm.get("read_fail"),
                     mm.get("missed"), mm.get("inferred_missed"), mm.get("slot_lost"),
                     f"{mm.get('service_down_count')} 次／{mm.get('service_down_sec')} 秒" if mm else None,
                     q["partial_slots"], q["slots_401"], q["read_mode"] or "未記錄", q["declared_version"]])
    r = _table(ws, 1, ["Session", "cert_id", "預定格點", "覆蓋率", "實際執行讀取率 %", "M-1 %", "完整即時資料率 %",
                       "讀取失敗", "MISSED", "推定缺格", "格點紀錄遺失", "服務中斷", "部分端點失敗（格）", "含 401 的格",
                       "讀取模式（資料記錄）", "宣告版本"], rows)
    ws.cell(r, 1, "品質指標按 Session 分開呈現，不合併計算 M-1；不同讀取模式與版本可能影響 M-1。")
    ws.cell(r + 1, 1, f"端點失敗類指標（部分端點失敗、401）跨 Session 可比性：{m['endpoint_comparability']}；"
                      "不可比時不得加總、計算差值或比較。")


def _sheet_params(ws, m):
    keys = sorted({k for x in m["sessions"] for k in x["params"]})
    rows = []
    for k in keys:
        vals = [x["params"].get(k) for x in m["sessions"]]
        rows.append([k] + vals + ["不同" if len({json.dumps(v, default=str) for v in vals}) > 1 else ""])
    r = _table(ws, 1, ["參數"] + [f"S{x['s_no']}" for x in m["sessions"]] + ["差異"], rows)
    rows2 = []
    for x in m["sessions"]:
        ti = x["test_info"] or {}
        rows2.append([f"S{x['s_no']}", ti.get("cabinet_spec"), ti.get("conditions"), ti.get("cycles"),
                      x["params_note"], x["params_info"]])
    _table(ws, r, ["Session", "電櫃規格", "測試條件", "循環數（設定）", "params_note", "參數缺漏／與預設差異"], rows2)


def _sheet_version(ws, m):
    rows = []
    for x in m["sessions"]:
        v = x["version"]
        f = v["features"]
        rows.append([f"S{x['s_no']}", x["cert_id"], v["declared_version"], v["version_source"],
                     v["version_verification"], f["hmi_reader"] or "未記錄", f["ep_ms_count_distribution"] or "未記錄",
                     f["summary"], v["consistency"], m["tool_note"]])
    r = _table(ws, 1, ["Session", "cert_id", "A 宣告版本", "B 宣告來源", "C 驗證狀態", "D 讀取模式紀錄",
                       "E 端點計時分布", "F 資料特徵摘要", "G 一致性", "H 重產明細產生程式"], rows)
    ws.cell(r, 1, "A～C 為使用者宣告；D～F 只是資料特徵觀察，不代表版本已驗證。")
    ws.cell(r + 1, 1, LABEL_RULE_NOTE)


def _sheet_trace(ws, m):
    rows = []
    for f in m["files"]:
        rows.append([f"S{f['s_no']}", f["cert_id"], f["src"], f["dst"], f["rel"], f["size"], f["mtime"], f["h1"], f["h2"],
                     f["h3"], f["use"]])
    r = _table(ws, 1, ["Session", "cert_id", "來源完整路徑", "快照完整路徑", "相對路徑", "大小", "mtime", "H1", "H2", "H3",
                       "用途"], rows)
    ws.cell(r, 1, "H4、H5 於本檔產生後執行，結果記錄在 STATUS.json 與 merge_manifest.json。").font = FB
    r += 2
    ws.cell(r, 1, "原始報告清單").font = FB
    rep = [[f"S{x['s_no']}", o["file"], o["sha256"], o["size"], o["recorded_at"], o["note"], o["is_baseline"]]
           for x in m["sessions"] for o in x["report_list"]]
    r = _table(ws, r + 1, ["Session", "檔名", "SHA-256", "大小", "記錄時間", "備註", "比較基準"], rep)
    ws.cell(r, 1, "資料值驗證").font = FB
    vr = []
    for v in m["verify"]:
        for it in v["items"]:
            vr.append([f"S{v['s_no']}", it["item"], it["result"], it.get("reason_code"), it.get("detail")])
        vr.append([f"S{v['s_no']}", "（Session 結果）", v["result"], v.get("reason_codes"), v.get("reason")])
        vr.append([f"S{v['s_no']}", "原始參數可追溯性", "UNVERIFIABLE", None, "原始報告未記錄"])
    r = _table(ws, r + 1, ["Session", "項目", "結果", "原因代碼", "說明"], vr)
    ws.cell(r, 1, VERIFY_SCOPE_NOTE)
    ws.cell(r + 1, 1, "工具版本：" + json.dumps(m["tool"], ensure_ascii=False))
