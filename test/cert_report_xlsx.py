# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— Excel 產生（P1 離線範例）
=================================================
依範本「340KW UL9540認證測試_ 251218 .xlsx」的頁籤與圖表形式產生報告：
  Summary → 數據概覽 → 即時熱度 → 各階段頁（數量依實際階段）→ 整體測試 → Raw Data

與範本的刻意差異（皆依已確認的設計）：
  - 階段頁／整體頁多 H「PCS 模組溫度」、I「備註」兩欄（同 TW_temp CSV 的欄位）
  - 缺值顯示 "N/A"；圖表讀取隱藏輔助欄（數值或空白），dispBlanksAs=gap → 缺值斷線、不畫成 0
  - Summary 只放測試條件與溫度曲線圖表（圖表下方不放表格與說明；判定紀錄保留在 Raw Data 與 .summary.json）
  - P1 不插入任何圖片：數據概覽／即時熱度只放「待截圖」標示
  - Summary 範本的箭號圖形（openpyxl 無法建立）以文字儲存格「→／←／↓ 間隔」代替（待確認做法）

本模組只寫入呼叫端指定的輸出路徑，不讀寫單次報告的任何檔案。
"""

import dataclasses
import os
import re
import zipfile
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.chart.series import SeriesLabel
from openpyxl.chart.data_source import AxDataSource, StrData, StrRef, StrVal
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import CharacterProperties, Paragraph, ParagraphProperties, RichTextProperties
from openpyxl.drawing.colors import ColorChoice, SchemeColor
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.line import LineProperties
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from xml.sax.saxutils import escape

import cert_report_config as CFG
from cert_screenshot import caption as shot_caption, png_size, select_final_shot, select_stage_shot, shot_flag
from cert_heat_link import failure_line, failure_text
import cert_supplement as SUP
from cert_controls import DIRECTION_TEXT, describe as describe_action
from cert_stage_engine import (R_NO_HMI, command_stage, decisions_keywords, fmt_t, field_value, has_hmi_values,
                               minute_value, overall_minutes, stage_minutes, stage_names, strip_count_time,
                               MinuteRow)

NA = CFG.NA
LOG_HEADERS = ["時間", "SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓",
               "總電壓", "傳感器溫度", "PCS 模組溫度", "備註"]
LOG_KEYS = ["soc", "current_a", "rack_max_c", "rack_min_c", "cell_vmax_v", "cell_vmin_v", "voltage_v",
            "sensor_c", "pcs_module_c"]
# Cell 最大／最小電壓：來源 rackExtremeValueInformation（目前 110 號櫃單一 Rack 全部 Cell 的極值；單位 V）。
# 與「總電壓」（rackTotalBatteryVoltage）不同；舊資料沒有此欄 → N/A，不以總電壓代替。
LOG_FORMATS = {"cell_vmax_v": "0.000", "cell_vmin_v": "0.000"}
REMARK_COL = len(LOG_HEADERS)                               # 備註欄（最後一欄）
CHART_ANCHOR = f"{get_column_letter(REMARK_COL + 2)}2"       # 階段頁／整體頁圖表：備註欄右側空一欄（隨欄數推導，不遮資料）
LOG_WIDTHS = {"A": 8.8, "B": 8.2, "C": 8.6, "D": 19.1, "E": 19.1, "F": 19.1, "G": 19.1, "H": 10.9,
              "I": 15.9, "J": 18.0, "K": 34.0}
# 圖表輔助欄（隱藏 AA～AC）：只放三條溫度（Rack 最大／最小、傳感器），內容為數值或空白。
# 依欄名推導來源欄，欄位插入／位移時不會誤畫成電壓。
HELPER_COL0 = 27                    # AA
CHART_KEYS = ["rack_max_c", "rack_min_c", "sensor_c"]
CHART_SRC = [(get_column_letter(2 + LOG_KEYS.index(k)), LOG_KEYS.index(k)) for k in CHART_KEYS]   # (標題欄, LOG_KEYS 索引)

SUMMARY_PITCH = 26                  # 範本 Summary 圖表的列距
CHART_W, CHART_H = 17.0, 12.7       # cm（階段頁／整體頁圖表）：同範本約 477×359pt；原 19×10.2cm 偏扁，Y 軸每 1°C 一格時標籤擠在一起
# Summary 版面依範本 drawing1.xml：(欄, 欄內偏移 EMU, 相對圖表首列的列數, 列內偏移 EMU)，欄列 0 起算
SUM_CHART_L = ((0, 0, 0, 0), (10, 561975, 20, 214313))
SUM_CHART_R = ((13, 0, 0, 0), (24, 85725, 20, 214313))
SUM_ARROW_H = ((11, 19050, 7, 114299), (12, 590550, 12, 19050))       # 同列左右圖之間
# 向下箭頭：以左右箭頭（約 123×106px）為基準縮小為約 92×104px，水平置中於該欄圖表、垂直置中於上下兩圖之間
# （圖表底 r0+20 列 +22.5px → 下一張圖 r0+26 列，間距約 116.7px，上下各留約 6px，不與圖表重疊）。
# 欄位換算：A 欄 83px、其他欄 63px（與圖表同為儲存格定位，顯示縮放時一起縮放，維持置中）。
SUM_ARROW_DOWN_R = ((17, 504825, 21, 53816), (19, 180975, 25, 160496))  # 右欄往下：中心約 1190px（右圖 839～1541）
SUM_ARROW_DOWN_L = ((4, 347663, 21, 53816), (6, 23813, 25, 160496))     # 左欄往下：中心約 354px（左圖 0～709）
# 範本箭頭為 accent6 亮度 60%（Office 2013+ 佈景主題 = 淡綠 A9D18E）；openpyxl 內建的是舊版佈景主題
# （accent6 為橘色），所以直接寫 RGB，不用佈景主題色。
ARROW_FILL, ARROW_LINE = "A9D18E", "548235"
# 箭頭文字「N 分」單行粗體 14pt（約 19px 高、「120 分」約 55px 寬）：放得進最窄的向下箭頭箭身（約 71px 寬），
# 不大於圖表標題（Excel 預設 14pt）
ARROW_TEXT_SZ = 1400

F14 = Font(size=14)
F14B = Font(size=14, bold=True)
FB = Font(bold=True)
HDR_FILL = PatternFill("solid", fgColor="DDEBF7")
PH_FILL = PatternFill("solid", fgColor="F2F2F2")
DASH = Side(style="dashed", color="C00000")
PH_BOX = Border(left=DASH, right=DASH, top=DASH, bottom=DASH)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _v(x):
    return NA if x is None else x


def _counted(st):
    return getattr(st, "counted", True)


def _stage_title(names, st):
    """不計次階段（H2 stage_count）：名稱本身已含「中止…（不計次）」→ 不再附加標記。"""
    n = names[st.no]
    return f"{n}（{st.label}）" if st.label and _counted(st) else n


def _sheet_name(i, names, st):
    """頁籤：序號 i 唯一；Excel 上限 31 字。不計次階段不附標記；超過 31 字先去掉開始時間再截斷。"""
    def clean(s):
        return re.sub(r"[\[\]\*\?/\\:]", "_", s)
    s = clean(f"{i}.{names[st.no]}" + (f"({st.label})" if st.label and _counted(st) else ""))
    if len(s) > 31 and not _counted(st):
        s = clean(f"{i}.{strip_count_time(names[st.no])}")
    return s[:31]


# =============================================================================
# 中止階段排除（stopped_stages_excluded=v1）：所有工作表共用同一份排除結果（唯一判斷處）
# =============================================================================
STOPPED_LABEL = "中止"
STOPPED_FORMAT = "stopped_stages_excluded=v1"   # 活頁簿屬性 category：新格式識別（空白＝舊格式，完整階段）
EXCLUDE_STOPPED_KEY = "exclude_stopped_stages"  # meta 旗標：只有 cert_runtime.generate_report 設定；直接呼叫 save_report 預設不排除
EXCL_SRC_NOTE = "沿用來源屬已排除的中止階段，不顯示數值"


def split_stopped(res, enabled):
    """
    有效 Flag 為「中止」（該階段 stage_label 最後一筆決策＝中止）的階段 → 排除（enabled 為真時）。
    回傳 (kept, excluded, spans)；spans＝[(第一筆觀測時間, 最後一筆觀測時間, Stage)]（seq0～seq1，含階段內停頓／讀取失敗）。
    不修改 res、Stage.no 或觀測；enabled 為假 → 不排除（舊格式）。
    """
    if not enabled:
        return list(res.stages), [], []
    kept, excluded, spans = [], [], []
    for st in res.stages:
        if st.label == STOPPED_LABEL:
            excluded.append(st)
            spans.append((res.obs[st.seq0].t, res.obs[st.seq1].t, st))
        else:
            kept.append(st)
    return kept, excluded, spans


def without_stopped(res, excluded):
    """報告用檢視：觀測池去除排除階段的觀測（淺複製；原始 Result 與觀測物件不變）。"""
    if not excluded:
        return res
    nos = {st.no for st in excluded}
    return dataclasses.replace(res, obs=[o for o in res.obs if o.stage_no not in nos])


def _in_spans(t, spans):
    return any(t0 <= t <= t1 for t0, t1, _st in spans)


def exclude_stopped_raw_rows(rs, excluded, spans):
    """
    1 秒格點 Raw Data 每秒列（raw_second_rows 結果）→ 排除中止階段（只作用於 Excel 輸出；不修改原列、觀測或檔案）：
      · LIVE 列：來源觀測屬於排除階段 → 排除；其他 LIVE（待機、下一個有效階段）保留。
      · 非 LIVE 列（沿用／未讀／無格點／服務停止）：格點秒在排除階段區間內（含頭尾）→ 排除；
        區間外但沿用來源屬於排除階段 → 保留時間列、數值顯示 N/A（說明 EXCL_SRC_NOTE）。
      · 來源無法對應（obs 為 None）：只依格點秒判斷（區間內排除，區間外維持原狀）。
    回傳新 dict（rows 為新清單；stats 為複本；excluded＝排除統計）。沒有排除階段 → 原物件。
    """
    if rs is None or not excluded:
        return rs
    nos = {st.no for st in excluded}
    rows, dropped, blanked = [], 0, 0
    for r in rs["rows"]:
        if "live" not in r:                         # 必須來自 raw_second_rows（live 由該函式標記）；缺少 → 不猜測
            raise ValueError("Raw Data 每秒列缺少 live 欄位（必須由 raw_second_rows 產生）")
        o = r.get("obs")
        src_x = o is not None and o.stage_no in nos
        if r["live"] and o is not None:
            if src_x:
                dropped += 1
                continue
            rows.append(r)
        elif _in_spans(r["time"], spans):
            dropped += 1
        elif src_x:
            rows.append(dict(r, obs=None, excluded_src=True))
            blanked += 1
        else:
            rows.append(r)
    return {"rows": rows, "stats": dict(rs["stats"]), "excluded": {"rows_dropped": dropped, "rows_blanked": blanked}}


def pad_excluded_minutes(rows, res, spans):
    """整體頁：保留完整原始時間軸（全部觀測的首筆～最後一筆所在分鐘）；排除後缺少的頭尾分鐘補 N/A（不補值）。"""
    if not spans or not res.obs:
        return rows
    have = {mr.T: mr for mr in rows}
    M = res.obs[0].t.replace(second=0, microsecond=0)
    end = res.obs[-1].t.replace(second=0, microsecond=0)
    out = []
    while M <= end:
        out.append(have.get(M) or MinuteRow(M, None, "", {}))
        M += timedelta(minutes=1)
    return out


def stopped_notes(kept, excluded, spans, rs=None):
    """排除說明（活頁簿描述；放在計次描述之前）。沒有排除階段 → []（描述與舊格式相同）。"""
    if not excluded:
        return []
    out = [f"已排除中止階段：{st.dir_name}，開始 {fmt_t(t0, True)}，結束 {fmt_t(t1, True)}，原始觀測 {st.seq1 - st.seq0 + 1} 筆"
           "（不列入階段頁、Summary、數據概覽、即時熱度、整體頁數值與 Raw Data；原始資料保留於 observations.jsonl）"
           for t0, t1, st in spans]
    ex = rs.get("excluded") if isinstance(rs, dict) else None
    if ex:
        out.append(f"已排除中止階段（Raw Data）：排除 {ex['rows_dropped']} 列；{ex['rows_blanked']} 列沿用中止階段讀值改為 N/A")
    if not kept:
        out.append("本報告沒有有效充放電階段（全部階段標記為中止）")
    return out


# =============================================================================
# 每分鐘 Log 頁（階段頁／整體頁）
# =============================================================================
def _write_log_sheet(ws, rows, remark_fn, p, table_name):
    for c, h in enumerate(LOG_HEADERS, 1):
        ws.cell(1, c, h)
    for k, (col, _i) in enumerate(CHART_SRC):
        hc = ws.cell(1, HELPER_COL0 + k, f"（圖表用）{ws[f'{col}1'].value}")
        hc.font = Font(italic=True, color="808080")
    for r, mr in enumerate(rows, 2):
        a = ws.cell(r, 1, mr.T)
        a.number_format = "h:mm"
        vals = [minute_value(mr, k, p) for k in LOG_KEYS]      # 極值欄位 = 分鐘 MAX／MIN；其餘 = 該分鐘最後一筆有效觀測
        pcs, pcs_note = pcs_display(mr.obs, p) if mr.obs is not None else (None, "")   # 只依選用的那一筆
        for c, (k, v) in enumerate(zip(LOG_KEYS, vals), 2):
            cell = ws.cell(r, c, pcs if k == "pcs_module_c" and mr.obs is not None else _v(v))
            if k in LOG_FORMATS and v is not None:
                cell.number_format = LOG_FORMATS[k]
        ws.cell(r, REMARK_COL, _join_notes(remark_fn(mr), pcs_note))
        for k, (_col, idx) in enumerate(CHART_SRC):
            ws.cell(r, HELPER_COL0 + k, vals[idx])     # None → 真正空白
    for col, w in LOG_WIDTHS.items():
        ws.column_dimensions[col].width = w
    for k in range(len(CHART_SRC)):
        ws.column_dimensions[get_column_letter(HELPER_COL0 + k)].hidden = True
    if rows:
        t = Table(displayName=table_name, ref=f"A1:{get_column_letter(REMARK_COL)}{len(rows) + 1}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium7", showFirstColumn=False,
                                          showLastColumn=False, showRowStripes=True,
                                          showColumnStripes=False)
        ws.add_table(t)
    ws.freeze_panes = "A2"


def _make_chart(ws, n, title):
    """依 ws 的 A 欄與隱藏輔助欄建立範本樣式折線圖（Rack 最大／最小、傳感器溫度）。"""
    ch = LineChart()
    ch.title = title
    ch.width, ch.height = CHART_W, CHART_H
    ch.legend.position = "b"
    ch.legend.overlay = False
    ch.display_blanks = "gap"
    ch.visible_cells_only = False           # 存檔後另以 _force_plot_vis_off 保證
    nums = []
    for k, (col, _i) in enumerate(CHART_SRC):
        hc = HELPER_COL0 + k
        ch.add_data(Reference(ws, min_col=hc, min_row=2, max_row=n + 1), titles_from_data=False)
        s = ch.series[-1]
        s.tx = SeriesLabel(strRef=StrRef(f"'{ws.title}'!${col}$1"))
        s.marker.symbol = "none"
        s.smooth = False
        for r in range(2, n + 2):
            v = ws.cell(r, hc).value
            if isinstance(v, (int, float)):
                nums.append(v)
    ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=n + 1))
    ch.x_axis.number_format = "h:mm"
    ch.x_axis.axPos = "b"
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    if nums:
        # 上下界對稱：下界 ceil(最小)−1、上界 ceil(最大)+1 —— 最小值剛好是整數時（例：傳感器 23.0）也留 1°C，
        # 曲線不會壓在 X 軸上；最小值非整數時結果與 floor(最小) 相同。只影響座標軸，不改資料。
        lo, hi = int(-(-min(nums) // 1)) - 1, int(-(-max(nums) // 1)) + 1
        ch.y_axis.scaling.min = lo
        ch.y_axis.scaling.max = hi
        ch.y_axis.majorUnit = 1
    # 水平格線：同範本，文字 1 亮度 85%（淡灰），細線
    ch.y_axis.majorGridlines.spPr = GraphicalProperties(ln=LineProperties(
        w=9525, solidFill=ColorChoice(schemeClr=SchemeColor(
            val="tx1", lumMod=15000, lumOff=85000))))
    return ch


# ---- 設備狀態（使用者看到的備註／狀態；內部階段判斷的 休息／停頓 不變）--------------------------
# 待機／停機只依 battery_power_status（電池接觸器回授）判定；不以電流 0、方向 idle、PCS 運行／待機旗標推論。
# 舊資料沒有保存 battery_power_status → N/A（不回填、不推測）。
BATTERY_POWER_ON, BATTERY_POWER_OFF = "已上電", "已下電"


FAULT = "故障"


def device_state(o):
    """非充放電期間的電池狀態：故障（PCS 故障旗標）／待機／停機／N/A（真正無法判定）。"""
    if o is None or not o.ok:
        return NA                                           # 讀取失敗／無取樣
    f = o.flags or {}
    if f.get("pcs_fault_flag") is True:
        return FAULT                                        # 設備明確回報 PCS 故障 → 已知狀態「故障」（不是 N/A；不擴大解釋為 BMS 故障）
    if f.get("pcs_charging_flag") is True or f.get("pcs_discharging_flag") is True or o.direction in ("C", "D"):
        return NA                                           # 仍有充放電（例：突波、未確認方向）→ 不是待機／停機
    bp = f.get("battery_power_status")
    if bp == BATTERY_POWER_OFF:
        return "停機"
    if bp == BATTERY_POWER_ON:
        return "待機"
    return NA                                               # 未知／切換中／None／舊資料沒有此欄


def status_text(o, names):
    """整體測試備註／Raw Data 狀態：充放電階段 → 第N次充電／放電；其餘 → 待機／停機／N/A。"""
    if o is not None and o.ok and o.stage_no and o.state in ("充電", "放電"):
        return names[o.stage_no]
    return device_state(o)


def _stage_remark(mr):
    if mr.obs is None:
        return f"N/A：{mr.na_reason}"
    o = mr.obs
    notes = []
    if o.state not in ("充電", "放電"):
        # 階段頁：保留階段內的分析標記（停頓／突波…），另附設備狀態，例「停頓（待機）」
        notes.append(f"{o.state}（{device_state(o)}）")
    if o.sensor_ok is False:
        notes.append("傳感器關閉或異常")
    return "；".join(notes)


def _write_short_stage(ws, st, p):
    ws["A1"] = "本階段沒有落在階段範圍內的整分鐘時點，無整分鐘 Log，不繪製圖表；頭尾觀測值如下（如實保留）。"
    ws["A1"].font = FB
    hdr = ["項目"] + LOG_HEADERS[1:]
    for c, h in enumerate(hdr, 1):
        ws.cell(3, c, h).font = FB
    for r, (lab, o) in enumerate([(f"階段開始（首筆觀測 {fmt_t(st.start.t, True)}）", st.start),
                                   (f"階段結束（最後觀測運行 {fmt_t(st.A.t, True)}）", st.A)], 4):
        ws.cell(r, 1, lab)
        pcs, pcs_note = pcs_display(o, p)
        for c, k in enumerate(LOG_KEYS, 2):
            v = field_value(o, k, p)
            cell = ws.cell(r, c, pcs if k == "pcs_module_c" else _v(v))
            if k in LOG_FORMATS and v is not None:
                cell.number_format = LOG_FORMATS[k]
        ws.cell(r, REMARK_COL, _join_notes(st.end_note, pcs_note))
    ws.column_dimensions["A"].width = 40
    for col, w in LOG_WIDTHS.items():
        if col != "A":
            ws.column_dimensions[col].width = w


# =============================================================================
# 截圖頁：P1 只放「待截圖」標示
# =============================================================================
def _placeholder(ws, r0, c0, r1, c1, text):
    ws.merge_cells(start_row=r0, start_column=c0, end_row=r1, end_column=c1)
    cell = ws.cell(r0, c0, text)
    cell.alignment = CENTER
    cell.fill = PH_FILL
    cell.font = Font(size=14, bold=True, color="C00000")
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            ws.cell(r, c).border = PH_BOX


PH_NOTE = ("P1 離線範例：尚未整合自動截圖，此處刻意留空。\n"
           "不使用範本圖片或其他時間的畫面代替本次測試截圖。")
ROW_PX, COL_PX = 20, 64             # 預設列高 15pt＝20px、欄寬 8.43＝64px（圖片版面計算用）
TITLE_FILL = PatternFill("solid", fgColor="F4B183")     # 範本階段標題：accent2(ED7D31) 淺 40%
TITLE_ROW_PT = 21
FLAG_FONT = Font(size=10, bold=True, color="C00000")
FLAG_ROW_PT = 16
NOTE_FONT = Font(size=10, bold=True, color="C00000")
HEAT_HEAD = "獨立櫃Pack 即時熱度圖"
# 欄寬以「字元」計，實際像素會隨顯示縮放改變（本機 125%：每欄約 58.4px，100% 時 64px）；圖片則是固定像素。
# → 標題底色／合併寬度以保守的 58px 計算欄數（確保涵蓋整張圖）；箭頭改用絕對位置，不依欄寬。
COL_PX_FIT = 58
OV_IMG_PX = 1088                    # 電池概覽卡片寬度（已確認成品）
OV_COLS = -(-OV_IMG_PX // COL_PX_FIT)   # 19 欄
# 數據概覽「時間序」向下箭頭：截圖右側留白，細長，從第 1 階段截圖頂端到第 2 階段截圖底端
OV_ARROW_W_PX = 72                               # 箭頭寬 72px（細長；箭身 75%＝54px，容得下 24pt 直排字）
OV_ARROW_GAP_PX = 24                             # 距截圖右緣 24px（絕對位置，不受欄寬隨顯示縮放影響）
TITLE_PAD_PX = 30                   # 階段標題底色：文字左右留白合計（左側縮排約 12px）
ORDER_ARROW_FILL, ORDER_ARROW_LINE = "8FAADC", "41719C"  # 範本 accent5(4472C4) 60%；外框同範本 accent1 shade 50%


class PngImage(XLImage):
    """openpyxl 的 Image 需要 Pillow 只為讀尺寸與位元組；PNG 直接讀檔頭即可（不新增依賴）。"""

    def __init__(self, path):
        self.ref = path
        self.width, self.height = png_size(path)
        self.format = "png"

    def _data(self):
        with open(self.ref, "rb") as f:
            return f.read()


def _stage_key(st):
    return st.start.t.strftime("%Y-%m-%dT%H:%M:%S")


FINAL = "cert_final"                    # 截圖頁的「認證結束（最後狀態）」區塊（不是階段）
FINAL_TITLE = "認證結束（最後狀態）"


def _slot_title(names, st):
    return FINAL_TITLE if st is FINAL else f"{_stage_title(names, st)}結束"


def _select_shot(shots, st, target):
    """階段 → 該階段結束截圖；FINAL → Menu 22 結束認證時的最後狀態截圖（兩者分開，互不代替）。"""
    if st is FINAL:
        return select_final_shot(shots["records"], target)
    return select_stage_shot(shots["records"], _stage_key(st), target)


def _shot_for(shots, st, target):
    """
    回傳 (圖片路徑或 None, 文字, 必要狀態)。shots=None → P1 模式（沒有截圖功能）。
    有圖片時「文字」不顯示於報告（類型、擷取時間、來源等只留在 screenshots.jsonl），只顯示必要狀態；
    沒有圖片時「文字」＝失敗／尚未截圖的原因，放在預留框內。
    """
    if shots is None:
        return None, None, ""
    job, res = _select_shot(shots, st, target)
    link = shots.get("heat_link")       # 6161 連續失敗區段（由取樣紀錄重建）；沒有 → 不加說明行（舊呼叫端）
    fail_lines = ()
    if link is not None and target == "heat" and res and res["status"] == "failed":
        fail_lines = (failure_line(link["runs"], job.get("trigger_time"), link["gap_sec"]),)
    text = shot_caption(job, res, fail_lines=fail_lines)
    if job is None:
        text = ("【無結束截圖】認證進行中，或結束認證時沒有截圖紀錄。不使用其他時間的畫面代替。" if st is FINAL
                else "【尚未截圖】本階段沒有截圖紀錄。不使用範本或其他時間的畫面代替。")
    if res and res["status"] in ("ok", "stale") and res.get("file"):
        path = os.path.join(shots["folder"], res["file"])
        if os.path.exists(path):
            return path, text, shot_flag(job, res)
        text = f"【圖片遺失】紀錄的檔案不存在：{res['file']}"
    return None, text, ""


def _shot_time_text(shots, st, target):
    """
    截圖右上角顯示的日期時間：該階段被選用截圖的 PC 實際擷取時間（screenshots.jsonl 的 captured_at）。
    不用報告產生／重產時間代替；紀錄沒有可解析的 captured_at → "N/A"。
    """
    _job, res = _select_shot(shots, st, target)
    try:
        return datetime.fromisoformat(res["captured_at"]).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, KeyError):
        return NA


def _title_cols(titles):
    """
    標題底色欄數：只涵蓋標題文字＋左右留白（同一頁籤各階段統一取最長者）。
    14pt：全形字約 19px、半形約 10px（100% 縮放）；欄寬以顯示縮放後較窄的 58px 保守計算，確保文字不超出底色。
    """
    px = max((sum(19 if ord(ch) > 0x2E80 else 10 for ch in t) for t in titles), default=0)
    return -(-(px + TITLE_PAD_PX) // COL_PX_FIT)


def _title_row(ws, r, c0, ncols, text):
    """階段標題：淺橘底（範本 accent2 淺 40%）＋粗體，底色只涵蓋標題文字與左右留白。"""
    cell = ws.cell(r, c0, text)
    cell.font = F14B
    cell.alignment = Alignment(indent=1, vertical="center")
    for c in range(c0, c0 + ncols):
        ws.cell(r, c).fill = TITLE_FILL
    ws.row_dimensions[r].height = TITLE_ROW_PT


def _flag_cell(ws, r, c0, c1, text):
    ws.merge_cells(start_row=r, start_column=c0, end_row=r, end_column=c1)
    cell = ws.cell(r, c0, text)
    cell.font = FLAG_FONT
    cell.alignment = Alignment(vertical="center", shrink_to_fit=True)
    ws.row_dimensions[r].height = FLAG_ROW_PT


def _shots_note(ws, r, c0, c1, shots):
    """版面驗證／P2 驗證等示範身分：獨立一列放在所有圖片之後（不放在標題與圖片之間）。"""
    if shots and shots.get("note"):
        ws.merge_cells(start_row=r, start_column=c0, end_row=r, end_column=c1)
        cell = ws.cell(r, c0, f"【註記】{shots['note']}")
        cell.font = NOTE_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 30


def _write_overview(ws, stages, names, shots=None, show_final=True):
    """
    數據概覽：各階段由上而下，淺橘標題 → 電池概覽卡片（原尺寸 1:1）；最後一個區塊為「認證結束（最後狀態）」
    （Menu 22 結束認證時擷取；沒有階段時只有這一塊，不會整頁空白；循環測試不顯示，見 screenshot_slots）。
    截圖右側留白放一支細長「時間序」向下箭頭：從第 1 階段截圖頂端到第 2 階段截圖底端（不遮截圖與標題；只算階段區塊）。
    """
    ws.column_dimensions["A"].width = 9.1
    ws.sheet_view.topLeftCell = "A1"            # 切換到本頁時一律從頂端開始
    ws.sheet_view.selection[0].activeCell = ws.sheet_view.selection[0].sqref = "A1"
    r = 1
    spans, right = [], OV_IMG_PX           # 各區塊 (頂端, 底端)：(列 0 起算, 列內偏移 px)；right＝最寬圖片右緣
    times = []                             # 各截圖右上角擷取時間：(圖片頂端列 0 起算, 圖片寬 px, 文字)
    slots = screenshot_slots(stages, show_final)
    tcols = _title_cols(_slot_title(names, st) for st in slots)
    for st in slots:
        title = _slot_title(names, st)
        _title_row(ws, r, 1, tcols, title)
        path, text, flag = _shot_for(shots, st, "overview")
        r += 1
        if flag:
            _flag_cell(ws, r, 1, OV_COLS, flag)
            r += 1
        if path:
            img = PngImage(path)
            img.anchor = f"A{r}"
            ws.add_image(img)
            times.append((r - 1, img.width, _shot_time_text(shots, st, "overview")))
            if st is not FINAL:
                spans.append(((r - 1, 0), (r - 1 + img.height // ROW_PX, img.height % ROW_PX)))
            right = max(right, img.width)
            r += -(-img.height // ROW_PX) + 2
        else:
            rows = 22 if shots is None else 11
            body = (f"【待截圖】HMI 數據概覽－電池概覽卡片\n{title}\n\n{PH_NOTE}" if shots is None
                    else f"HMI 數據概覽－電池概覽卡片\n{title}\n\n{text}")
            _placeholder(ws, r, 1, r + rows - 1, OV_COLS, body)
            if st is not FINAL:
                spans.append(((r - 1, 0), (r - 1 + rows, 0)))
            r += rows + 2
    _shots_note(ws, r, 1, OV_COLS, shots)
    if spans:
        first, last = spans[0][0], spans[min(1, len(spans) - 1)][1]
        heights = {i: ws.row_dimensions[i].height for i in range(1, last[0] + 2) if ws.row_dimensions[i].height}
        ws._cert_order_arrow = (first, last, right + OV_ARROW_GAP_PX, heights, OV_ARROW_W_PX)   # 存檔後寫入 drawing
    if times:
        heights = {i: ws.row_dimensions[i].height for i in range(1, max(t[0] for t in times) + 2)
                   if ws.row_dimensions[i].height}
        ws._cert_shot_times = [(row0, w, text, heights) for row0, w, text in times]           # 存檔後寫入 drawing


def _supplement_for(shots, st):
    """
    階段的事後補拍圖片（supplements；選圖規則見 cert_supplement.select）→ 圖片路徑；沒有可用圖片 → None。
    只在原始階段結束截圖沒有圖片時使用；不屬於任何階段的「認證結束（最後狀態）」不補拍。
    報告只放圖片（補拍時間、類型、原始階段結束時間等完整資訊保存在 supplements/screenshots.jsonl）；
    被採用的補拍檔案遺失 → 不放圖、不改用其他補拍。
    """
    sp = (shots or {}).get("supplements")
    if not sp or st is FINAL:
        return None
    _job, res, _n = SUP.select(sp["records"], _stage_key(st), "heat")
    if res and res["status"] in ("ok", "stale") and res.get("file"):
        path = os.path.join(sp["folder"], res["file"])
        if os.path.exists(path):
            return path, res.get("captured_at")
    return None


def supplement_label(captured_at):
    """補拍圖片正上方的一行標示；時間取自補拍紀錄的實際擷取時間（captured_at），不是報告產生時間。"""
    try:
        t = datetime.fromisoformat(captured_at).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        t = NA
    return f"事後補拍：{t}（非階段結束畫面）"


def original_failure_line(shots, st, target="heat"):
    """有事後補拍時，補拍圖片上方的原始失敗原因（取自該階段原始截圖紀錄，不用補拍時的錯誤）。"""
    job, res = _select_shot(shots, st, target)
    if res is not None:
        reason = res.get("reason") or "未記錄原因"
    else:
        reason = "截圖尚未完成" if job is not None else "沒有階段結束截圖紀錄"
    return f"原始截圖失敗原因：{reason}"


def is_cycle_test(meta):
    """循環測試＝Menu 21 有填循環數（正整數；設定條件，不代表實際完成的循環數，不檢查實際階段）。"""
    v = (meta or {}).get("cycles")
    try:
        return not isinstance(v, bool) and int(v) > 0
    except (TypeError, ValueError):
        return False


def screenshot_slots(stages, show_final=True):
    """截圖頁（數據概覽／即時熱度）的區塊：各階段＋「認證結束（最後狀態）」。
    循環測試（show_final=False）不顯示最後狀態；但沒有任何階段時仍保留最後狀態一塊（不會整頁空白）。
    只影響 Excel 呈現：最後狀態的截圖紀錄與 PNG 照常保存。"""
    stages = list(stages)
    return stages + [FINAL] if show_final or not stages else stages


def _write_heat(ws, stages, names, shots=None, show_final=True):
    """即時熱度：各階段由左而右，淺橘標題 →「獨立櫃Pack 即時熱度圖」（置中）→ 獨立櫃 110 區塊（原尺寸 1:1）；
    最右邊為「認證結束（最後狀態）」（沒有階段時只有這一塊；循環測試不顯示，見 screenshot_slots）。
    原始截圖沒有圖片、但有成功的事後補拍圖片 → 該區域只放兩行紅字（事後補拍時間；原始截圖失敗原因）＋補拍圖片，
    不顯示原本整段失敗紀錄（完整紀錄仍保存在 screenshots.jsonl）。沒有可用補拍 → 照舊顯示原始失敗紀錄。
    提示列（第 3 列起）列數取各區塊最多者 → 各階段圖片頂端對齊。"""
    stages = screenshot_slots(stages, show_final)
    paths = [_shot_for(shots, st, "heat") for st in stages]
    supps = [None if paths[k][0] else _supplement_for(shots, st) for k, st in enumerate(stages)]
    widths = [png_size(pth)[0] for pth, _, _ in paths if pth] + [png_size(sp[0])[0] for sp in supps if sp]
    # 區塊間距：以顯示縮放後較窄的 58px／欄計算，圖片右側至少留 2 欄
    pitch = max(12, -(-max(widths, default=0) // COL_PX_FIT) + 2)
    flags = []
    for k, st in enumerate(stages):
        if supps[k]:                              # 成功的事後補拍：補拍時間（supplements 紀錄）＋原始失敗原因（原始截圖紀錄）
            flags.append([supplement_label(supps[k][1]), original_failure_line(shots, st)])
        else:
            flags.append([paths[k][2]] if paths[k][2] else [])
    img_row = 3 + max((len(f) for f in flags), default=0)
    bottom = img_row + 38
    tcols = _title_cols(_slot_title(names, st) for st in stages)
    for k, st in enumerate(stages):
        c = 1 + pitch * k
        title = _slot_title(names, st)
        path, text, _flag = paths[k]
        supp = supps[k]
        shown = path or (supp[0] if supp else None)
        span = -(-png_size(shown)[0] // COL_PX_FIT) if shown else pitch - 2
        _title_row(ws, 1, c, tcols, title)
        ws.merge_cells(start_row=2, start_column=c, end_row=2, end_column=c + span - 1)
        head = ws.cell(2, c, HEAT_HEAD)
        head.font = F14B
        head.alignment = CENTER
        ws.row_dimensions[2].height = TITLE_ROW_PT
        for i, line in enumerate(flags[k]):
            _flag_cell(ws, 3 + i, c, c + span - 1, line)
        if shots is None:
            _placeholder(ws, img_row, c, img_row + 43, c + span - 1,
                         f"【待截圖】\n即時熱度－獨立櫃 110\n{title}\n\n{PH_NOTE}")
            bottom = max(bottom, img_row + 44)
            continue
        if shown:
            img = PngImage(shown)
            img.anchor = f"{get_column_letter(c)}{img_row}"
            ws.add_image(img)
            bottom = max(bottom, img_row + -(-img.height // ROW_PX))
        else:
            _placeholder(ws, img_row, c, img_row + 37, c + span - 1, f"即時熱度－獨立櫃 110\n{title}\n\n{text}")
    _shots_note(ws, bottom + 1, 1, max(OV_COLS, pitch * len(stages) - 2), shots)


# =============================================================================
# Summary
# =============================================================================
def _write_summary(wb, ws, res, names, sheet_of, n_rows, meta, p):
    for i, line in enumerate(meta.get("conditions", [])[:3], 1):
        ws.cell(i, 1, _condition_text(line)).font = F14
    ws.cell(4, 1, "溫度曲線：").font = F14B
    ws.column_dimensions["A"].width = 11.9
    ws.sheet_format.defaultColWidth = 9             # 同範本，圖表與箭頭依儲存格定位
    ws.sheet_format.defaultRowHeight = 17.4
    ws.sheet_format.customHeight = True
    for r in range(1, 5):
        ws.row_dimensions[r].height = 20
    stages = res.stages
    titles = summary_titles(stages, meta)
    arrows = []

    def top(i):
        """蛇形：第 k 列偶數列左→右、奇數列右→左。回傳 (圖表首列 0 起算, 是否右欄)。"""
        r, slot = divmod(i, 2)
        if r % 2 == 1:
            slot = 1 - slot
        return 4 + SUMMARY_PITCH * r, slot == 1

    for i, st in enumerate(stages):
        r0, right = top(i)
        if n_rows[st.no]:
            ch = _make_chart(wb[sheet_of[st.no]], n_rows[st.no], titles[st.no])
            _summary_axis_labels(ch, wb[sheet_of[st.no]], n_rows[st.no])
            ws.add_chart(ch, _anchor(SUM_CHART_R if right else SUM_CHART_L, r0))
        else:
            c0 = 14 if right else 1
            _placeholder(ws, r0 + 1, c0, r0 + 4, c0 + 10,
                         f"{titles[st.no]}：不足一分鐘，無整分鐘 Log，不繪製圖表")
        if i + 1 < len(stages):
            text = arrow_text(chart_span(wb, sheet_of, n_rows, st),
                              chart_span(wb, sheet_of, n_rows, stages[i + 1]))
            n0, n_right = top(i + 1)
            if n0 == r0:
                arrows.append(("rightArrow" if n_right else "leftArrow", SUM_ARROW_H, r0, text))
            else:
                arrows.append(("downArrow", SUM_ARROW_DOWN_R if right else SUM_ARROW_DOWN_L, r0, text))
    orow = 4 + SUMMARY_PITCH * ((len(stages) + 1) // 2)
    if n_rows.get("overall"):
        ch = _make_chart(wb["整體測試"], n_rows["overall"], overall_title(meta))   # X 軸文字方向／間隔交給 Excel（直式）
        ws.add_chart(ch, _anchor(SUM_CHART_L, orow))
    if ws._charts:
        wb._cert_summary_arrows = arrows            # 存檔後寫入 Summary 的 drawing（真正的箭頭圖形）
    else:                                           # 沒有任何圖表 → 沒有 drawing 可放圖形，改用文字
        for prst, spec, r0, text in arrows:
            (c, _co, dr, _ro), _to = spec
            ws.cell(r0 + dr + 1, c + 1, {"rightArrow": "→", "leftArrow": "←"}.get(prst, "↓")
                    + " " + " ".join(text)).font = FB


def _condition_text(line):
    """「冷氣溫度：…」→ 標籤（含「：」）粗體、內容一般字，同範本 A1～A3。"""
    label, sep, rest = line.partition("：")
    if not sep:
        return line
    return CellRichText(TextBlock(InlineFont(b=True, sz=14), label + sep),
                        TextBlock(InlineFont(sz=14), rest))


# ---- 測試資訊（每次測試輸入；不寫死、不推算）----
def cabinet_spec(meta):
    """電櫃規格（例 340KW、250KW）；未輸入回 None。不可用測試設定功率代替。"""
    v = str(meta.get("cabinet_spec") or "").strip()
    return v or None


def overall_title(meta):
    """整體溫度曲線標題：【340KW】電櫃充放2個循環溫度曲線。Summary 與「整體測試」頁共用。
    未輸入電櫃規格 → 【電櫃規格未設定】；未輸入循環數 → 不寫循環數（不自動推算）。"""
    cyc = meta.get("cycles")
    return (f"【{cabinet_spec(meta) or '電櫃規格未設定'}】電櫃充放"
            + (f"{int(cyc)}個循環" if cyc else "") + "溫度曲線")


def _with_spec(meta, title):
    """階段頁圖表標題前綴電櫃規格（範本「340KW 第1次滿放」）；未輸入則不加。"""
    spec = cabinet_spec(meta)
    return f"{spec} {title}" if spec else title


def chart_span(wb, sheet_of, n_rows, st):
    """Summary 圖表實際使用的 Category 範圍（＝階段頁 A 欄第 2～n+1 列的整分時間，與 _make_chart 同一來源）。
    回傳 (第一個 Category, 最後一個 Category)；沒有圖表（不足一分鐘）→ None。"""
    n = n_rows.get(st.no)
    if not n:
        return None
    ws = wb[sheet_of[st.no]]
    return ws.cell(2, 1).value, ws.cell(n + 1, 1).value


SUM_LABEL_MAX = 32          # 階段圖寬度下直式 10pt 標籤約容納數；= Excel 在階段頁同尺寸圖表自動省略的結果（123→每 4、76→每 3）
SUM_LABEL_ROT = -5400000    # Summary 階段圖 X 軸標籤固定直式（-90°，同階段頁 Excel 自動呈現）；1/60000 度


def summary_axis_labels(times):
    """
    階段圖（Summary＋階段頁）X 軸標籤（與 chart_span／箭頭同一組 Category 時間，不增減資料點）：
    模擬階段頁 Excel 自動省略的密度 —— 自第一個 Category 起每 k 個顯示一個，k = ceil(n / SUM_LABEL_MAX)；
    另外最後一個 Category 一定顯示（Excel 自動間隔做不到），離最後一個不到 k 的中間標籤留空避免重疊。
    """
    n = len(times)
    k = max(1, -(-n // SUM_LABEL_MAX))
    return [f"{t:%H:%M}" if i == n - 1 or (i % k == 0 and n - 1 - i >= k) else "" for i, t in enumerate(times)]


def _summary_axis_labels(ch, ws, n):
    """Summary 階段圖與階段頁圖表共用：類別改為上列標籤文字（同一組階段頁 A 欄時間），每個標籤都顯示（tickLblSkip=1）。
    溫度系列（數值參照）不變；整體圖（Summary 與「整體測試」頁）不套用。"""
    labels = summary_axis_labels([ws.cell(r, 1).value for r in range(2, n + 2)])
    for s in ch.series:
        s.cat = AxDataSource(strLit=StrData(ptCount=n, pt=[StrVal(idx=i, v=v) for i, v in enumerate(labels)]))
    ch.x_axis.tickLblSkip = 1
    _summary_axis_style(ch)


def _summary_axis_style(ch):
    """階段圖（Summary＋階段頁）同一 X 軸文字樣式：固定直式、緊鄰座標軸、10pt（同 Excel 預設字級），
    避免 Excel 依標籤數各自改成橫／斜／直式。只改文字樣式，不動類別資料與標籤間隔。整體圖不套用（維持 Excel 自動）。"""
    ch.x_axis.tickLblPos = "nextTo"
    ch.x_axis.txPr = RichText(bodyPr=RichTextProperties(rot=SUM_LABEL_ROT, vert="horz"),
                              p=[Paragraph(pPr=ParagraphProperties(defRPr=CharacterProperties(sz=1000)),
                                           endParaRPr=CharacterProperties())])


def arrow_text(prev_span, next_span):
    """
    Summary 箭頭文字：只顯示「N 分」（單行）。
    = 下一張圖第一個 Category − 上一張圖最後一個 Category（與圖表同一資料時間基準，非 Excel 自動省略後的可見標籤，
      也不用 Stage Engine 的 end_B／A）；任一張沒有圖表 → N/A。
    """
    if prev_span is None or next_span is None:
        return [rest_minutes_text(None)]
    return [rest_minutes_text((next_span[0] - prev_span[1]).total_seconds())]


def rest_minutes_text(sec):
    """秒數 → 「N 分」（Category 為整分時間，差值本來就是整分；保留捨去以防萬一）。
    無法計算或為負（時間異常）→ 「N/A」，不猜時間。"""
    if sec is None or sec < 0:
        return "N/A"
    return f"{int(sec // 60)} 分"


def summary_titles(stages, meta=None):
    """Summary 圖表標題＝階段頁圖表標題（同一命名邏輯）：電櫃規格＋「第N次充電／放電」＋操作人員標記（Menu 23）。
    例「250KW 第1次放電（滿放）」；未標記 →「250KW 第1次放電」（不自行推論滿充／滿放）；電櫃規格未輸入則不加前綴。"""
    names = stage_names(stages)
    return {st.no: _with_spec(meta or {}, _stage_title(names, st)) for st in stages}


def _anchor(spec, r0):
    (c0, co0, r_0, ro0), (c1, co1, r_1, ro1) = spec
    return TwoCellAnchor(_from=AnchorMarker(col=c0, colOff=co0, row=r0 + r_0, rowOff=ro0),
                         to=AnchorMarker(col=c1, colOff=co1, row=r0 + r_1, rowOff=ro1))


def _arrow_xml(k, prst, spec, r0, text):
    """範本樣式箭頭（淡綠填色），文字放在箭頭內。"""
    (c0, co0, a, ro0), (c1, co1, b, ro1) = spec
    runs = "".join(f'<a:p><a:pPr algn="ctr"/><a:r><a:rPr lang="zh-TW" altLang="en-US" sz="{ARROW_TEXT_SZ}" b="1">'
                   f'<a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:rPr>'
                   f'<a:t>{escape(t)}</a:t></a:r></a:p>' for t in text)
    adj = ('<a:gd name="adj1" fmla="val 70000"/><a:gd name="adj2" fmla="val 40000"/>')
    return (
        f'<xdr:twoCellAnchor><xdr:from><xdr:col>{c0}</xdr:col><xdr:colOff>{co0}</xdr:colOff>'
        f'<xdr:row>{r0 + a}</xdr:row><xdr:rowOff>{ro0}</xdr:rowOff></xdr:from>'
        f'<xdr:to><xdr:col>{c1}</xdr:col><xdr:colOff>{co1}</xdr:colOff>'
        f'<xdr:row>{r0 + b}</xdr:row><xdr:rowOff>{ro1}</xdr:rowOff></xdr:to>'
        f'<xdr:sp macro="" textlink=""><xdr:nvSpPr><xdr:cNvPr id="{1000 + k}" name="休息箭頭 {k}"/>'
        f'<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></a:xfrm>'
        f'<a:prstGeom prst="{prst}"><a:avLst>{adj}</a:avLst></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{ARROW_FILL}"/></a:solidFill>'
        f'<a:ln w="12700"><a:solidFill><a:srgbClr val="{ARROW_LINE}"/></a:solidFill></a:ln></xdr:spPr>'
        f'<xdr:txBody><a:bodyPr vertOverflow="overflow" horzOverflow="overflow" wrap="none" '
        f'lIns="0" tIns="0" rIns="0" bIns="0" rtlCol="0" anchor="ctr"/><a:lstStyle/>{runs}</xdr:txBody>'
        f'</xdr:sp><xdr:clientData/></xdr:twoCellAnchor>')


# =============================================================================
# Raw Data
# =============================================================================
RAW_HEADERS = ["序號", "時間", "狀態",
               "SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓", "總電壓",
               "傳感器溫度", "PCS 模組溫度", "功率 kW", "說明"]
RAW_NOTE_COL = len(RAW_HEADERS)
RAW_COL = {h: i for i, h in enumerate(RAW_HEADERS, 1)}     # 欄名 → 欄號（不寫死欄號）
# 不輸出「所屬階段」：階段已由「狀態」欄呈現（第N次放電（滿放）…），內部 stage_no／階段分析不受影響。
# 來源（observations.jsonl:N）不輸出到報告；Obs.src／seq 仍保留於程式內供追溯／重播
# 「時間」＝PC 實際取樣時間（Obs.t，含秒、不取整）。只列取樣明細：不輸出與階段頁／整體頁的分鐘對照（含 N/A 分鐘列），
# 各分鐘的選用結果與 N/A 原因見階段頁／整體頁的備註欄。
# 讀取結果不輸出（Raw Data 只列有效觀測，原本全為「成功」）；成功／失敗仍保留在 observations.jsonl（ok／hmi_ok）。
RAW_WIDTHS = {"序號": 7, "時間": 20, "狀態": 11, "說明": 30}


# Raw Data 必要量測欄位（全部為 HMI 欄位）：任一缺值 → 整筆不輸出。
# 6161 欄位為選用（傳感器溫度、PCS 模組溫度）：6161 讀取失敗或多功能傳感器關閉 → 該欄 N/A，HMI 數據照常輸出。
RAW_REQUIRED = ["soc", "current_a", "rack_max_c", "rack_min_c", "cell_vmax_v", "cell_vmin_v", "voltage_v", "power_kw"]


PCS_FAIL = "FAIL"      # PCS 模組溫度「資料取得失敗」（該筆取樣記錄 6161 讀取失敗）；不代表 PCS 設備故障


def pcs_display(o, p):
    """
    PCS 模組溫度的呈現（Raw Data、階段頁、整體頁、短階段頁共用）→ (儲存格值, 備註)；只依「這一筆取樣」本身的紀錄：
      有效數值 → (數值, "")；
      該筆記錄 6161 讀取失敗 → ("FAIL", 實際失敗原因)；
      其他缺值（6161 有回應但沒有數值、或原因無法確認）→ ("N/A", 說明)。不推論為通訊故障或設備故障。
    """
    v = field_value(o, "pcs_module_c", p)
    if v is not None:
        return v, ""
    fail = failure_text(o.notes, o.fail_reason)
    if fail:
        return PCS_FAIL, f"PCS 模組溫度 FAIL（資料取得失敗）：{fail}"
    if any(str(x).startswith("6161 無資料") for x in o.notes or []):
        return NA, "PCS 模組溫度 N/A：6161 回傳空內容"
    raw = (o.values or {}).get("pcs_module_c")
    if raw not in (None, ""):
        return NA, f"PCS 模組溫度 N/A：6161 回傳非數值（{raw}）"
    return NA, "PCS 模組溫度 N/A：無有效資料（原因無法確認）"


def _join_notes(*parts):
    return "；".join(x for x in parts if x)


def has_pcs_temp(o, p):
    """每分鐘選樣的優先條件（階段頁與整體頁共用）：該筆有 PCS 模組溫度（6161）。
    同一分鐘有這種有效取樣就優先選用其最後一筆；沒有才選 HMI 有效的最後一筆（PCS 顯示 N/A）。Raw Data 不套用。"""
    return field_value(o, "pcs_module_c", p) is not None


def raw_rows_obs(res, p, names):
    """
    Raw Data 只輸出有效報告資料：HMI 有效（ok 且 has_hmi_values；與 Runtime、每分鐘選樣同一定義）
    且 RAW_REQUIRED 全部有有效數值（field_value；不補值、不沿用鄰近值）
    且「狀態」可依現行 Authority 判定（status_text 不是 N/A：階段內充放電 → 第N次…；其餘只依電池上下電 → 待機／停機；
    PCS 故障 → 故障（設備明確回報，照常輸出）；充放電旗標未歸零、上下電未知 → 無法判定）。不以 SOC／電流／功率推論狀態。
    讀取失敗、HMI 全無值（含僅 6161 溫度有效）、必要欄位缺值、狀態無法判定 → 整筆不輸出；observations.jsonl 原始紀錄不受影響。
    序號維持原觀測 seq（中間跳號 = 該筆未輸出）；序號＝observations.jsonl 列號，可回查原始紀錄。
    """
    return [o for o in res.obs if is_report_valid(o, p, names)]


def overall_phase(o, names):
    """整體頁改選時的語意範圍：所屬階段、方向、分析狀態（運行／停頓／休息…）、設備狀態文字（第N次… / 待機 / 停機 / N/A）。
    只用既有欄位，不另做狀態推論；改選的觀測必須與原選用觀測完全相同，避免休息分鐘改選成階段運行、故障／下電被正常資料取代。"""
    return o.stage_no, o.direction, o.state, status_text(o, names)


def raw_measurement_valid(o, p):
    """
    r2c：量測有效性（只看本筆讀值本身）：HMI 讀取成功（ok 且 has_hmi_values）且 RAW_REQUIRED 全部有有效數值。
    不看「狀態能否判定」。1 秒格點 Raw Data 每秒列以此判定本秒是否有真正的新量測（LIVE）；
    讀取失敗、HMI 全無值、必要欄位缺值 → 不是新量測（不會成為 LIVE）。
    例：階段結束後電流已歸零、PCS 充放電旗標尚未歸零 → 狀態無法判定（N/A），但量測本身有效 → Raw Data 照列真實讀值。
    """
    return (o.ok and has_hmi_values(o)
            and all(field_value(o, k, p) is not None for k in RAW_REQUIRED))


def is_report_valid(o, p, names):
    """正式報表有效觀測（4 張階段頁／整體頁每分鐘選樣；5 秒歷史認證的 Raw Data 也用此定義；條件見 raw_rows_obs）
    ＝量測有效（raw_measurement_valid）且狀態可判定。r2c：1 秒格點 Raw Data 每秒列只用量測有效性（見 raw_second_rows）
    → 該頁可能多列出「狀態 N/A」的真實量測；階段頁／整體頁／判定的選樣規則不變。"""
    return raw_measurement_valid(o, p) and status_text(o, names) != NA


def _write_raw(ws, res, names, p):
    heads, col, note_col = RAW_HEADERS, RAW_COL, RAW_NOTE_COL
    for c, h in enumerate(heads, 1):
        cell = ws.cell(1, c, h)
        cell.font = FB
        cell.fill = HDR_FILL
    r = 2
    for o in raw_rows_obs(res, p, names):
        ws.cell(r, col["序號"], o.seq + 1)
        ws.cell(r, col["時間"], o.t).number_format = "yyyy-mm-dd hh:mm:ss"
        ws.cell(r, col["狀態"], status_text(o, names))
        pcs, pcs_note = pcs_display(o, p)
        for c, k in enumerate(LOG_KEYS + ["power_kw"], col["SOC"]):
            v = field_value(o, k, p)
            cell = ws.cell(r, c, pcs if k == "pcs_module_c" else _v(v))
            if k in LOG_FORMATS and v is not None:
                cell.number_format = LOG_FORMATS[k]
        parts = []
        if o.state not in ("充電", "放電"):
            # 追溯用：保留內部分析狀態與電池上下電原始值（狀態欄只顯示 待機／停機；無法判定者不輸出）
            parts.append(f"分析狀態：{o.state}")
            parts.append(f"電池上下電：{(o.flags or {}).get('battery_power_status') or '未保存'}")
        if o.sensor_ok is False:
            parts.append("傳感器關閉或異常")
        ws.cell(r, note_col, _join_notes(*parts, pcs_note))
        r += 1
    num_w = [7, 9, 13, 13, 13, 13, 9, 11, 13, 9]                  # SOC～功率（與原寬度相同）
    widths = dict(RAW_WIDTHS, **dict(zip(heads[col["SOC"] - 1:note_col - 1], num_w)))
    for h, i in col.items():
        ws.column_dimensions[get_column_letter(i)].width = widths[h]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(note_col)}{r - 1}"


# =============================================================================
# 控制紀錄（Menu 4～7 指令；階段於產報時依指令時間與最終階段分析對應）
# 註：報告已不建立「控制紀錄」頁（build_workbook 不呼叫 _write_commands）；保留函式供測試／日後需要
# =============================================================================
CMD_HEADERS = ["時間", "階段", "方向", "控制模式", "設定值", "單位", "Action", "控制結果", "驗證結果", "整體結果", "備註"]
CMD_WIDTHS = [20, 22, 7, 14, 9, 6, 26, 11, 11, 11, 48]


def _ok_text(v):
    return {True: "成功", False: "失敗"}.get(v, NA)


def _verify_text(v):
    """
    驗證結果：operator 的 verify_success 原值照實區分（不改判定、不改原始資料）。
      True → 成功；False → 失敗；"partial" → 部分驗證（控制已送出成功，回讀只確認部分欄位）；
      "timeout" → 驗證逾時（不視為成功或失敗）；None／欄位不存在 → N/A；
      其他未知值 → N/A（原始值另寫入備註，見 _verify_note）。
    """
    if v is True or v is False:
        return _ok_text(v)
    return VERIFY_TEXT.get(v, NA) if isinstance(v, str) else NA


VERIFY_TEXT = {"partial": "部分驗證", "timeout": "驗證逾時"}


def _verify_note(v):
    """驗證結果需要補充說明時的備註文字；不需要時回 None。"""
    if v == "partial":
        return "驗證結果 partial：控制已送出成功，回讀僅部分確認"
    if v == "timeout":
        return "驗證結果 timeout：控制已送出，逾時仍未回讀到預期狀態"
    if v is None or v is True or v is False:
        return None
    return f"驗證結果原始值：{v!r}（未定義的值，顯示 N/A）"


def _write_commands(ws, res, commands, names, p):
    """
    commands=None → 來源沒有指令紀錄（例：歷史 TW CSV／單次報告回放）→「無控制紀錄」，不推定歷史控制資訊。
    舊認證資料缺新欄位 → 方向／控制模式／單位依 action 對照表（cert_controls）；設定值、驗證結果 → N/A。
    """
    for c, h in enumerate(CMD_HEADERS, 1):
        cell = ws.cell(1, c, h)
        cell.font = FB
        cell.fill = HDR_FILL
    for i, w in enumerate(CMD_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    if commands is None:
        ws.cell(2, 1, "無控制紀錄：來源資料沒有選單控制指令紀錄（不推定歷史控制資訊）")
        return
    rows = sorted((c for c in commands if c.get("t")), key=lambda c: c["t"])
    if not rows:
        ws.cell(2, 1, "無控制紀錄：認證期間沒有偵測到選單控制指令（HMI 直接操作不會被記錄）")
        return
    for r, c in enumerate(rows, 2):
        info = describe_action(c.get("action"))
        direction = c.get("direction", info["direction"])
        mode_text = c.get("control_mode_text") or info["control_mode_text"]
        unit = c.get("unit") if "unit" in c else info["unit"]
        t = datetime.strptime(c["t"], "%Y-%m-%dT%H:%M:%S")
        st = command_stage(res, t, direction, p) if not c.get("dry_run") else None
        notes = []
        if c.get("dry_run"):
            notes.append("dry-run（未實際送出）")
        if c.get("blocked"):
            notes.append("前置檢查阻擋（未送出）")
        if direction is None:
            notes.append("非充放電控制")
        elif st is None and not c.get("dry_run"):
            notes.append("無法可靠對應階段")
        if "setpoint" not in c:
            notes.append("舊資料未保存設定值")
        vn = _verify_note(c.get("verify_success"))
        if vn:
            notes.append(vn)
        notes.append("Tc＝控制結果寫檔時間（非送出瞬間）")
        ws.cell(r, 1, t).number_format = "yyyy-mm-dd hh:mm:ss"
        ws.cell(r, 2, names.get(st.no) if st is not None else NA)
        ws.cell(r, 3, DIRECTION_TEXT.get(direction, NA))
        ws.cell(r, 4, mode_text or NA)
        sp = c.get("setpoint")
        ws.cell(r, 5, sp if isinstance(sp, (int, float)) and not isinstance(sp, bool) else NA)
        ws.cell(r, 6, unit or NA)
        ws.cell(r, 7, c.get("action") or NA)
        ws.cell(r, 8, _ok_text(c.get("control_success")))
        ws.cell(r, 9, _verify_text(c.get("verify_success")))
        ws.cell(r, 10, _ok_text(c.get("success")))
        ws.cell(r, 11, "；".join(notes))


# =============================================================================
# 入口
# =============================================================================
def build_workbook(res, meta):
    p = res.params
    kept, excluded, spans = split_stopped(res, meta.get(EXCLUDE_STOPPED_KEY))   # 唯一中止判斷；各頁共用同一份結果
    res_v = without_stopped(res, excluded)          # 選樣／極值／Raw 觀測池：去除排除階段觀測（原始資料不變）
    names = stage_names(kept)                       # 有效階段重新計算第N次（原始 Stage.no 不變）
    names_full = {st.no: _stage_title(names, st) for st in kept}
    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"
    ws_ov = wb.create_sheet("數據概覽")
    ws_heat = wb.create_sheet("即時熱度")

    sheet_of, n_rows = {}, {}
    for i, st in enumerate(kept, 1):
        ws = wb.create_sheet(_sheet_name(i, names, st))
        sheet_of[st.no] = ws.title
        rows = stage_minutes(res_v, st, valid=lambda o: is_report_valid(o, p, names_full),   # 與 Raw Data 同一有效判定
                             ext_scope="minute",             # 四個極值＝階段所涵蓋分鐘的完整日曆分鐘（排除明確反方向）
                             prefer=lambda o: has_pcs_temp(o, p))                  # 優先含 PCS 溫度的取樣（與整體頁同一條件）
        n_rows[st.no] = len(rows)
        n_rows[("na", st.no)] = sum(1 for m in rows if m.obs is None)
        if rows:
            _write_log_sheet(ws, rows, _stage_remark, p, f"T_stage{i}")
            ch = _make_chart(ws, len(rows), _with_spec(meta, names_full[st.no]))
            _summary_axis_labels(ch, ws, len(rows))          # 與 Summary 階段圖同一 X 軸規則（首尾時間一定顯示）
            ws.add_chart(ch, CHART_ANCHOR)
        else:
            _write_short_stage(ws, st, p)

    ws_all = wb.create_sheet("整體測試")
    orows = overall_minutes(res_v, valid=lambda o: is_report_valid(o, p, names_full),          # 有效觀測優先（同一判定）
                            prefer=lambda o: has_pcs_temp(o, p),                              # 與階段頁同一選樣優先條件
                            phase=lambda o: overall_phase(o, names_full),                     # 不跨語意；沒有 → 原規則
                            ext_scope="minute")              # 四個極值＝完整日曆分鐘（一般欄位仍為最後語意範圍）
    orows = pad_excluded_minutes(orows, res, spans)    # 中止階段排除：保留完整原始時間軸（頭尾補 N/A）
    n_rows["overall"] = len(orows)

    def overall_remark(mr):
        if mr.obs is None:
            hit = [st for t0, t1, st in spans if mr.T <= t1 and mr.T + timedelta(minutes=1) > t0]
            if hit:
                return "N/A：已排除中止階段（" + "、".join(f"{st.dir_name}，開始 {fmt_t(st.start.t, True)}" for st in hit) + "）"
            return f"N/A：{mr.na_reason}"
        if not has_hmi_values(mr.obs):          # 該分鐘內只有僅 6161 溫度的觀測
            return f"N/A：{R_NO_HMI}，僅有 6161 溫度"
        return status_text(mr.obs, names_full)

    if orows:
        _write_log_sheet(ws_all, orows, overall_remark, p, "T_overall")
        ws_all.add_chart(_make_chart(ws_all, len(orows),
                                     overall_title(meta)), CHART_ANCHOR)

    shots = meta.get("screenshots")
    show_final = not is_cycle_test(meta)           # 循環測試：兩個截圖頁都不顯示「認證結束（最後狀態）」
    _write_overview(ws_ov, kept, names, shots, show_final)
    _write_heat(ws_heat, kept, names, shots, show_final)
    _write_summary(wb, ws_sum, dataclasses.replace(res_v, stages=kept), names_full, sheet_of, n_rows, meta, p)
    # 「控制紀錄」頁已自報告移除；commands.jsonl 仍由 cert_runtime 照常記錄（不影響控制流程／稽核）
    grid = (meta.get("sampling_slots") or {}).get("grid")
    rs = exclude_stopped_raw_rows(raw_second_rows(res, grid, names_full), excluded, spans) if grid else None
    if rs is None:
        _write_raw(wb.create_sheet("Raw Data"), res_v, names_full, p)
        if grid:
            wb._cert_raw_note = ("Raw Data：本認證的格點不是全部 1 秒間隔（或沒有任何格點）→ 維持只列實際有效觀測的格式"
                                 "（不補秒、不沿用）。")
    else:
        write_raw_seconds(wb, rs, p, names_full)            # M-S2 r2：每秒一列（整筆沿用）；取代 Sampling Slots 工作表
        wb._cert_raw_note = raw_seconds_note(rs)
    wb._cert_stopped_notes = stopped_notes(kept, excluded, spans, rs)
    return wb


# =============================================================================
# M-S2 r2：Raw Data 每秒一列（1 秒格點認證；有 slots.jsonl 且全部段落間隔 1 秒時才使用）
#   整筆沿用：沒有新的有效觀測 → 沿用最近一筆有效觀測的整列（不同欄位不混用不同時間）；只供 Raw Data 顯示，
#   階段頁／整體頁／圖表／Summary／判定仍只使用實際觀測（不讀本頁）。歷史認證 → 上方 _write_raw（原格式）。
# =============================================================================
RAW_SHEET = "Raw Data"
RAW_MAX_ROWS = 1048576              # Excel 單一工作表列數上限（含標題）；超過 → 「Raw Data (2)」…（測試可調小）
RAW_VALUE_HEADERS = RAW_HEADERS[RAW_COL["SOC"] - 1:RAW_NOTE_COL - 1]     # SOC～功率 kW（與原 Raw Data 相同順序）
# r2b：Excel 只顯示時間／狀態／數值／說明（13 欄）；內部資料狀態、來源、資料年齡與品質統計不變（描述與原始紀錄保留）
RAW1_HEADERS = ["時間", "狀態"] + RAW_VALUE_HEADERS + ["說明"]
RAW1_COL = {h: i for i, h in enumerate(RAW1_HEADERS, 1)}
RAW1_WIDTHS = {"時間": 20, "狀態": 11, "說明": 46}
RAW1_FONT = Font(color="FF000000")              # r2b：所有資料列一律黑色 #000000（ARGB 不透明；不依資料狀態區分顏色）
RAW1_HEAD_FONT = Font(bold=True, color="FF000000")


def _obs_by_ref(obs, raw_items):
    """
    原始紀錄（raw_ref＝observations.jsonl 實體行號）→ 分析用 Obs。兩者來自同一份 observations.jsonl，但分析端會略過
    無法解析的行（索引錯位），且觀測順序可能依時間重排（時鐘被調整時）→ 不用索引或順序，改以「時間＋數值＋旗標」
    內容對應（完全相同的紀錄依出現順序配對）。找不到 → 不對應（該格不算 LIVE、不作為沿用來源）。
    回傳 (dict, 無法對應筆數)。
    """
    def key(t, vals, flags):
        return (t, repr(sorted(vals.items())), repr(sorted(flags.items())))      # repr：NaN 也能比對
    pool = {}
    for o in obs:
        pool.setdefault(key(o.t, o.values or {}, o.flags or {}), []).append(o)
    out, miss = {}, 0
    for it in raw_items:
        rec = it.get("record") or {}
        try:
            t = datetime.strptime(rec.get("t") or "", "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            miss += 1
            continue
        cand = pool.get(key(t, rec.get("values") or {}, rec.get("flags") or {}))
        if cand:
            out[it.get("raw_ref")] = cand.pop(0)
        else:
            miss += 1
    return out, miss


def raw_second_rows(res, grid, names=None):
    """
    Raw Data 每秒列（純資料，不寫檔）。不適用（沒有格點、或格點間隔不是 1 秒）→ None。
    每列另附 obs（來源觀測；N/A → None）。
    r2c：本秒是否有新量測＝量測有效性（raw_measurement_valid），不含狀態可判定性 → 階段結束後旗標延遲期間的
    真實讀值（例：電流 0.0 A）照常列出（狀態欄 N/A），不再整筆沿用前一階段的讀值。階段頁仍用 is_report_valid。
    """
    import report_display as RD     # 補值模組只在本函式使用（階段頁／整體頁／判定不使用）
    if not grid:
        return None
    if names is None:
        nm = stage_names(res.stages)
        names = {st.no: _stage_title(nm, st) for st in res.stages}
    p = res.params
    ref2obs, miss = _obs_by_ref(res.obs, grid.get("raw") or [])
    cache = {}

    def valid(ref):
        if ref not in cache:
            o = ref2obs.get(ref)
            cache[ref] = o is not None and raw_measurement_valid(o, p)
        return cache[ref]
    out = RD.build_raw_seconds(grid.get("result"), grid.get("raw") or [], valid,
                               started_at=grid.get("started_at"), interruptions=grid.get("interruptions"))
    if out is None:
        return None
    for r in out["rows"]:
        r["obs"] = ref2obs.get(r["src"]) if r["src"] is not None else None
        r["live"] = r["state"] == RD.RAW_LIVE          # 內部判斷用（中止階段排除）；不輸出到 Excel
    out["stats"]["unmatched_raw"] = miss
    check_raw_continuity(out["rows"])
    return out


def check_raw_continuity(rows):
    """每秒一列、時間連續且不重複；不成立 → 例外（報告產生失敗、原始資料不動，可重新產生），不輸出錯誤的 Raw Data。"""
    one = timedelta(seconds=1)
    for a, b in zip(rows, rows[1:]):
        if a["time"] is None or b["time"] is None or b["time"] - a["time"] != one:
            raise RuntimeError(f"Raw Data 每秒列不連續：{a['time']} → {b['time']}")


def _raw1_note(r, p):
    """說明欄（r2b）：只放來源觀測本身的設備資訊（分析狀態、電池上下電、傳感器、PCS 模組溫度說明）；
    沿用列＝來源列的說明（整筆沿用）；沒有來源（N/A）→ 空白。不顯示取樣品質／沿用等技術文字（統計在報告描述）。"""
    if r.get("excluded_src"):                       # 中止階段排除：沿用來源屬排除階段 → 不顯示來源資訊
        return EXCL_SRC_NOTE
    o = r.get("obs")
    parts = []
    if o is not None:
        if o.state not in ("充電", "放電"):
            parts.append(f"分析狀態：{o.state}")
            parts.append(f"電池上下電：{(o.flags or {}).get('battery_power_status') or '未保存'}")
            # r2c：狀態無法判定（N/A）時附上設備回報的 PCS 狀態原文（例：電流已歸零但 PCS 仍回報放電）；不另做推論
            if device_state(o) == NA and (o.flags or {}).get("pcs_status"):
                parts.append(f"PCS 狀態：{o.flags['pcs_status']}")
        if o.sensor_ok is False:
            parts.append("傳感器關閉或異常")
        parts.append(pcs_display(o, p)[1])
    return _join_notes(*parts)


def write_raw_seconds(wb, rs, p, names, max_rows=None):
    """M-S2 r2：Raw Data 每秒一列（超過列數上限 → 「Raw Data (2)」…）。回傳工作表名稱清單。"""
    max_rows = max_rows or RAW_MAX_ROWS
    col = RAW1_COL
    vcol = col["SOC"]
    ncol = len(RAW1_HEADERS)
    keys = LOG_KEYS + ["power_kw"]
    sheets = []
    pg = {"ws": None, "row": 0}

    def new_page():
        title = RAW_SHEET if not sheets else f"{RAW_SHEET} ({len(sheets) + 1})"
        ws = wb.create_sheet(title)
        sheets.append(ws)
        for c, h in enumerate(RAW1_HEADERS, 1):
            cell = ws.cell(1, c, h)
            cell.font = RAW1_HEAD_FONT
            cell.fill = HDR_FILL
        pg["ws"], pg["row"] = ws, 1

    def finish(ws, last_row):
        widths = dict(RAW1_WIDTHS, **dict(zip(RAW_VALUE_HEADERS, [7, 9, 13, 13, 13, 13, 9, 11, 13, 9])))
        for h, i in col.items():
            ws.column_dimensions[get_column_letter(i)].width = widths[h]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(ncol)}{max(last_row, 1)}"

    new_page()
    for r in rs["rows"]:
        if pg["row"] >= max_rows:
            finish(pg["ws"], pg["row"])
            new_page()
        ws = pg["ws"]
        rr = pg["row"] + 1
        pg["row"] = rr
        o = r.get("obs")
        cells = [ws.cell(rr, col["時間"], r["time"])]
        cells[0].number_format = "yyyy-mm-dd hh:mm:ss"
        if o is not None:
            cells.append(ws.cell(rr, col["狀態"], status_text(o, names)))
            pcs, _note = pcs_display(o, p)
            for c, k in enumerate(keys, vcol):
                v = field_value(o, k, p)
                cell = ws.cell(rr, c, pcs if k == "pcs_module_c" else _v(v))
                if k in LOG_FORMATS and v is not None:
                    cell.number_format = LOG_FORMATS[k]
                cells.append(cell)
        else:
            cells.append(ws.cell(rr, col["狀態"], NA))
            cells.extend(ws.cell(rr, c) for c in range(vcol, vcol + len(keys)))     # 數值格空白（不虛構），字型仍統一黑色
        cells.append(ws.cell(rr, col["說明"], _raw1_note(r, p) or None))
        for cell in cells:
            cell.font = RAW1_FONT
    finish(pg["ws"], pg["row"])
    return [ws.title for ws in sheets]


def raw_seconds_note(rs):
    """報告描述（活頁簿屬性）用的一行摘要。"""
    import report_display as RD
    s, rows = rs["stats"], rs["rows"]
    c = s["counts"]
    ex = rs.get("excluded")
    if ex:                                          # 中止階段排除：依實際輸出列重算各狀態秒數與資料年齡
        c = {k: 0 for k in RD.RAW_STATES}
        ages, unk = [], 0
        for r in rows:
            c[RD.RAW_NA if r.get("excluded_src") else r["state"]] += 1
            if r.get("obs") is not None or r["state"] == RD.RAW_LIVE:
                if r.get("age") is None:
                    unk += 1
                else:
                    ages.append(r["age"])
        s = dict(s, max_age=max(ages) if ages else None, age_unknown=unk)
    cont = ("秒數連續不重複；" if not ex else
            f"秒數不重複；已排除中止階段 {ex['rows_dropped']} 列（排除時段不列出）、"
            f"{ex['rows_blanked']} 列沿用中止階段讀值改為 N/A；")
    span = f"{rows[0]['time']:%Y-%m-%d %H:%M:%S}～{rows[-1]['time']:%Y-%m-%d %H:%M:%S}" if rows else "—"
    txt = (f"Raw Data（1 秒格點認證）：每秒一列，共 {len(rows)} 列（{span}），{cont}"
           "沒有新的有效量測時整筆沿用最近一筆有效量測（沿用值不是新量測；Excel 不另外標示，各狀態秒數如下；"
           "讀值有效但狀態無法判定者照列真實讀值、狀態顯示 N/A）；"
           + "／".join(f"{k} {c.get(k, 0)}" for k in RD.RAW_STATES)
           + f"；最長資料年齡 {'—' if s['max_age'] is None else str(s['max_age']) + ' 秒'}")
    if s.get("age_unknown"):
        txt += f"；資料年齡無法計算（時鐘異常）{s['age_unknown']} 列"
    if s.get("overlap_slots"):
        txt += (f"；時鐘回撥：{s['overlap_slots']} 個格點與前段時間重疊（{s['overlap_from']:%H:%M:%S}～"
                f"{s['overlap_to']:%H:%M:%S}），不產生列、其觀測不採用（原始紀錄保留）")
    if s.get("unmatched_raw"):
        txt += f"；{s['unmatched_raw']} 筆原始紀錄無法對應分析觀測（不採用）"
    return txt + ("。資料年齡＝本列秒－來源列秒（同一段落內以單調時鐘推算；跨服務停止以電腦時鐘計算，停止期間的時鐘調整"
                  "若未造成時間重疊則無法偵測）。階段頁、整體頁、圖表與判定只使用實際觀測，不使用本頁沿用值。")


def stage_count_notes(res):
    """
    H2 stage_count 說明（活頁簿屬性「描述」）：不計次原因、無效／恢復說明、完整歷史、對不到階段的決策。
    沒有任何 stage_count 決策 → []（報告描述與現行完全相同）。
    """
    out = []
    stages = [st for st in res.stages if getattr(st, "count_history", None)]
    if stages:
        nm = stage_names(res.stages)
    for st in stages:
        title = _stage_title(nm, st)
        head = f"階段計次：{title}（開始 {fmt_t(st.start.t, True)}）→ "
        if not _counted(st):
            out.append(head + f"不計次；原因：{st.count_reason}")
        else:
            out.append(head + "計次" + (f"；{st.count_note}" if st.count_note else ""))
        n = len(st.count_history)
        for k, h in enumerate(st.count_history, 1):
            out.append(f"　決策 {k}/{n}：{h.get('at') or '（無時間）'}｜counted={h.get('counted')!r}｜"
                       f"原因：{h.get('reason') or '（空白）'}｜by={h.get('by') or ''}｜request_id={h.get('request_id') or ''}")
    for o in getattr(res, "count_orphans", None) or []:
        out.append(f"階段計次：stage_start={o.get('stage_start')!r} {o.get('note')}（{o.get('n')} 筆）→ 未套用")
    return out


def save_report(res, meta, path):
    meta = dict(meta)
    meta.setdefault("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb = build_workbook(res, meta)
    # 來源說明（暫用參數、中斷、未完成等）寫入活頁簿屬性「描述」：不改動已確認的頁面版面
    notes = list(meta.get("source_notes") or [])
    raw_note = getattr(wb, "_cert_raw_note", None)
    if raw_note:                                   # M-S2 r2：Raw Data 每秒列摘要放在格點品質之後（避免描述長度上限截掉）
        at = next((i + 1 for i, n in enumerate(notes) if str(n).startswith("格點品質：")), len(notes))
        notes.insert(at, raw_note)
    notes += getattr(wb, "_cert_stopped_notes", [])  # 中止階段排除說明（必須在計次描述之前：r8 X10 要求計次描述在最後）
    notes += stage_count_notes(res)                # H2：只有 stage_count 歷史／孤兒時才有內容（否則描述不變）
    if notes:
        wb.properties.description = "\n".join(str(n) for n in notes)[:3000]
        wb.properties.title = "充／放電認證測試報告"
    if meta.get(EXCLUDE_STOPPED_KEY):              # 新格式識別（category 目前無其他用途；dec_v1 keywords 不變）
        wb.properties.category = STOPPED_FORMAT
    dec_sha = getattr(res, "dec_sha256", "")
    if dec_sha:                                    # H2 dec_v1：分析所用決策快照的指紋
        wb.properties.keywords = decisions_keywords(res.dec_n, dec_sha)
    wb.save(path)
    _force_plot_vis_off(path)
    _add_summary_arrows(path, getattr(wb, "_cert_summary_arrows", []))
    order = getattr(wb["數據概覽"], "_cert_order_arrow", None)
    if order:
        _inject_shapes(path, "數據概覽", _order_arrow_xml(*order))
    times = getattr(wb["數據概覽"], "_cert_shot_times", None)
    if times:
        _inject_shapes(path, "數據概覽", "".join(_shot_time_xml(k, *t) for k, t in enumerate(times, 1)))
    return path


SHOT_TIME_RIGHT_PX = 100     # 文字框右緣距圖片右緣（卡片右上「● Normal」約佔最右 90px，留白不遮）
SHOT_TIME_TOP_PX = 18        # 與卡片標題「Battery Overview」同一列（SOC／SOH 框約從 70px 開始）
SHOT_TIME_W_PX, SHOT_TIME_H_PX = 220, 28


def _row_top_emu(row0, heights):
    return sum(round((heights.get(i) or 15) * 12700) for i in range(1, row0 + 1))


def _shot_time_xml(k, row0, img_w, text, heights):
    """
    數據概覽：截圖右上方空白處（「● Normal」左側）的擷取日期時間文字框（無底色、無框線、靠右）。
    只是 Excel 上的文字物件，不修改原始截圖檔；位置用絕對座標（同時間序箭頭）。
    """
    x0 = (img_w - SHOT_TIME_RIGHT_PX - SHOT_TIME_W_PX) * EMU_PX
    y0 = _row_top_emu(row0, heights) + SHOT_TIME_TOP_PX * EMU_PX
    cx, cy = SHOT_TIME_W_PX * EMU_PX, SHOT_TIME_H_PX * EMU_PX
    return (
        f'<xdr:absoluteAnchor><xdr:pos x="{x0}" y="{y0}"/><xdr:ext cx="{cx}" cy="{cy}"/>'
        f'<xdr:sp macro="" textlink=""><xdr:nvSpPr><xdr:cNvPr id="{2100 + k}" name="擷取時間{k}"/>'
        f'<xdr:cNvSpPr txBox="1"/></xdr:nvSpPr><xdr:spPr><a:xfrm><a:off x="{x0}" y="{y0}"/>'
        f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:noFill/><a:ln><a:noFill/></a:ln></xdr:spPr>'
        f'<xdr:txBody><a:bodyPr wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" rtlCol="0" anchor="ctr"/>'
        f'<a:lstStyle/><a:p><a:pPr algn="r"/><a:r><a:rPr lang="en-US" sz="1200" b="1">'
        f'<a:solidFill><a:srgbClr val="333333"/></a:solidFill></a:rPr><a:t>{escape(text)}</a:t></a:r></a:p>'
        f'</xdr:txBody></xdr:sp><xdr:clientData/></xdr:absoluteAnchor>')


def _order_arrow_xml(first, last, x_px, heights, w_px):
    """
    數據概覽右側細長「時間序」向下箭頭（範本：淡藍底 accent5 60%、白色粗體、文字逐字直排）。
    箭身寬 75%、箭頭長 50%（佔箭頭寬度）。
    用絕對位置（absoluteAnchor）：x 依圖片像素寬度，y 依列高（點）累加 —— 不受欄寬隨顯示縮放變動影響。
    """
    def y(row0, off_px):                    # 列（0 起算）頂端＋偏移 → EMU；未設定列高者為預設 15pt
        return sum(round((heights.get(i) or 15) * 12700) for i in range(1, row0 + 1)) + off_px * EMU_PX
    y0, y1 = y(*first), y(*last)
    x0, cx = x_px * EMU_PX, w_px * EMU_PX
    runs = "".join(f'<a:p><a:pPr algn="ctr"/><a:r><a:rPr lang="zh-TW" altLang="en-US" sz="2400" b="1">'
                   f'<a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:rPr><a:t>{ch}</a:t></a:r></a:p>'
                   for ch in "時間序")
    adj = '<a:gd name="adj1" fmla="val 75000"/><a:gd name="adj2" fmla="val 50000"/>'
    return (
        f'<xdr:absoluteAnchor><xdr:pos x="{x0}" y="{y0}"/>'
        f'<xdr:ext cx="{cx}" cy="{y1 - y0}"/>'
        f'<xdr:sp macro="" textlink=""><xdr:nvSpPr><xdr:cNvPr id="2001" name="時間序箭頭"/>'
        f'<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr><a:xfrm><a:off x="{x0}" y="{y0}"/>'
        f'<a:ext cx="{cx}" cy="{y1 - y0}"/></a:xfrm>'
        f'<a:prstGeom prst="downArrow"><a:avLst>{adj}</a:avLst></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{ORDER_ARROW_FILL}"/></a:solidFill>'
        f'<a:ln w="12700"><a:solidFill><a:srgbClr val="{ORDER_ARROW_LINE}"/></a:solidFill></a:ln></xdr:spPr>'
        f'<xdr:txBody><a:bodyPr vertOverflow="overflow" horzOverflow="overflow" wrap="none" '
        f'lIns="0" tIns="0" rIns="0" bIns="0" rtlCol="0" anchor="ctr"/>'
        f'<a:lstStyle/>{runs}</xdr:txBody></xdr:sp><xdr:clientData/></xdr:absoluteAnchor>')


XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


EMU_PX = 9525
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DRAWING_REL = R_NS + "/drawing"
DRAWING_CT = "application/vnd.openxmlformats-officedocument.drawing+xml"


def _add_summary_arrows(xlsx_path, arrows):
    """openpyxl 不能建立圖形 → 存檔後把箭頭寫進 Summary 的 drawing XML。"""
    if arrows:
        _inject_shapes(xlsx_path, "Summary", "".join(_arrow_xml(k, *a) for k, a in enumerate(arrows, 1)))


def _inject_shapes(xlsx_path, sheet_title, shapes):
    """
    存檔後把圖形（twoCellAnchor XML）加入指定工作表的 drawing。
    工作表已有 drawing（圖表／圖片）→ 附加；沒有（例：所有截圖都失敗）→ 新建 drawing 與關聯。
    """
    with zipfile.ZipFile(xlsx_path, "r") as zin:
        files = {i.filename: zin.read(i.filename) for i in zin.infolist()}
        infos = zin.infolist()
    wbx = files["xl/workbook.xml"].decode("utf-8")
    rid = re.search(rf'<sheet [^>]*name="{re.escape(escape(sheet_title))}"[^>]*r:id="([^"]+)"', wbx).group(1)
    wrels = files["xl/_rels/workbook.xml.rels"].decode("utf-8")
    sheet_target = re.search(rf'<Relationship [^>]*Id="{rid}"[^>]*>', wrels).group(0)
    sheet_path = "xl/" + re.search(r'Target="([^"]+)"', sheet_target).group(1).lstrip("/").replace("xl/", "", 1)
    rels_path = os.path.dirname(sheet_path) + "/_rels/" + os.path.basename(sheet_path) + ".rels"
    rels = files.get(rels_path, b"").decode("utf-8")
    m = re.search(r'<Relationship [^>]*Type="[^"]*/drawing"[^>]*>', rels)
    if m:
        target = re.search(r'Target="([^"]+)"', m.group(0)).group(1)
        dpath = os.path.normpath(os.path.join(os.path.dirname(sheet_path), target)).replace("\\", "/") \
            if not target.startswith("/") else target.lstrip("/")
        x = files[dpath].decode("utf-8")
        end = re.search(r"</(?:(\w+):)?wsDr>\s*$", x)
        if end.group(1) != "xdr" or 'xmlns:a="' not in x:
            shapes = re.sub(r"<xdr:(twoCellAnchor|absoluteAnchor)>",
                            lambda m: f'<xdr:{m.group(1)} xmlns:xdr="{XDR_NS}" xmlns:a="{A_NS}">', shapes)
        files[dpath] = (x[:end.start()] + shapes + x[end.start():]).encode("utf-8")
    else:
        n = 1
        while f"xl/drawings/drawing{n}.xml" in files:
            n += 1
        dpath = f"xl/drawings/drawing{n}.xml"
        files[dpath] = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                        f'<xdr:wsDr xmlns:xdr="{XDR_NS}" xmlns:a="{A_NS}">{shapes}</xdr:wsDr>').encode("utf-8")
        ids = [int(i) for i in re.findall(r'Id="rId(\d+)"', rels)]
        new_id = f"rId{max(ids, default=0) + 1}"
        rel = f'<Relationship Id="{new_id}" Type="{DRAWING_REL}" Target="../drawings/drawing{n}.xml"/>'
        if rels:
            rels = rels.replace("</Relationships>", rel + "</Relationships>")
        else:
            rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns='
                    f'"http://schemas.openxmlformats.org/package/2006/relationships">{rel}</Relationships>')
        files[rels_path] = rels.encode("utf-8")
        sx = files[sheet_path].decode("utf-8")
        tag = f'<drawing xmlns:r="{R_NS}" r:id="{new_id}"/>'
        nxt = re.search(r"<(?:legacyDrawing|legacyDrawingHF|picture|oleObjects|controls|webPublishItems|"
                        r"tableParts|extLst)\b", sx)
        pos = nxt.start() if nxt else sx.rindex("</worksheet>")
        files[sheet_path] = (sx[:pos] + tag + sx[pos:]).encode("utf-8")
        ct = files["[Content_Types].xml"].decode("utf-8")
        files["[Content_Types].xml"] = ct.replace(
            "</Types>", f'<Override PartName="/{dpath}" ContentType="{DRAWING_CT}"/></Types>').encode("utf-8")
    tmp = xlsx_path + ".tmp"
    names = [i.filename for i in infos]
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for i in infos:
            zout.writestr(i, files[i.filename])
        for fn in files:
            if fn not in names:
                zout.writestr(fn, files[fn])
    os.replace(tmp, xlsx_path)


def _force_plot_vis_off(xlsx_path):
    """圖表讀的是隱藏輔助欄 → plotVisOnly 必須為 0，否則 Excel 不畫隱藏欄資料。"""
    tmp = xlsx_path + ".tmp"
    pat = re.compile(r'(<(?:c:)?plotVisOnly val=")(?:1|true)(")')
    with zipfile.ZipFile(xlsx_path, "r") as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if re.match(r"xl/charts/chart\d+\.xml$", item.filename):
                data = pat.sub(r"\g<1>0\g<2>", data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
    os.replace(tmp, xlsx_path)
