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

import os
import re
import zipfile
from datetime import datetime

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.chart.series import SeriesLabel
from openpyxl.chart.data_source import StrRef
from openpyxl.chart.shapes import GraphicalProperties
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
from cert_screenshot import caption as shot_caption, png_size, select_stage_shot, shot_flag
from cert_controls import DIRECTION_TEXT, describe as describe_action
from cert_stage_engine import (R_NO_HMI, _num, command_stage, fmt_t, field_value, has_hmi_values, overall_minutes,
                               rest_between, stage_minutes, stage_names)

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
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
DASH = Side(style="dashed", color="C00000")
PH_BOX = Border(left=DASH, right=DASH, top=DASH, bottom=DASH)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _v(x):
    return NA if x is None else x


def _stage_title(names, st):
    n = names[st.no]
    return f"{n}（{st.label}）" if st.label else n


def _sheet_name(i, names, st):
    s = f"{i}.{names[st.no]}" + (f"({st.label})" if st.label else "")
    return re.sub(r"[\[\]\*\?/\\:]", "_", s)[:31]


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
        vals = [field_value(mr.obs, k, p) if mr.obs is not None else None for k in LOG_KEYS]
        for c, (k, v) in enumerate(zip(LOG_KEYS, vals), 2):
            cell = ws.cell(r, c, _v(v))
            if k in LOG_FORMATS and v is not None:
                cell.number_format = LOG_FORMATS[k]
        ws.cell(r, REMARK_COL, remark_fn(mr))
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


def device_state(o):
    """非充放電期間的電池狀態：待機／停機／N/A。"""
    if o is None or not o.ok:
        return NA                                           # 讀取失敗／無取樣
    f = o.flags or {}
    if f.get("pcs_fault_flag") is True:
        return NA                                           # 明確故障（目前只有 PCS 故障旗標，不擴大解釋為 BMS 故障）
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
        for c, k in enumerate(LOG_KEYS, 2):
            v = field_value(o, k, p)
            cell = ws.cell(r, c, _v(v))
            if k in LOG_FORMATS and v is not None:
                cell.number_format = LOG_FORMATS[k]
        ws.cell(r, REMARK_COL, st.end_note)
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


def _shot_for(shots, st, target):
    """
    回傳 (圖片路徑或 None, 文字, 必要狀態)。shots=None → P1 模式（沒有截圖功能）。
    有圖片時「文字」不顯示於報告（類型、擷取時間、來源等只留在 screenshots.jsonl），只顯示必要狀態；
    沒有圖片時「文字」＝失敗／尚未截圖的原因，放在預留框內。
    """
    if shots is None:
        return None, None, ""
    job, res = select_stage_shot(shots["records"], _stage_key(st), target)
    text = shot_caption(job, res)
    if job is None:
        text = "【尚未截圖】本階段沒有截圖紀錄。不使用範本或其他時間的畫面代替。"
    if res and res["status"] in ("ok", "stale") and res.get("file"):
        path = os.path.join(shots["folder"], res["file"])
        if os.path.exists(path):
            return path, text, shot_flag(job, res)
        text = f"【圖片遺失】紀錄的檔案不存在：{res['file']}"
    return None, text, ""


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


def _write_overview(ws, stages, names, shots=None):
    """
    數據概覽：各階段由上而下，淺橘標題 → 電池概覽卡片（原尺寸 1:1）。
    截圖右側留白放一支細長「時間序」向下箭頭：從第 1 階段截圖頂端到第 2 階段截圖底端（不遮截圖與標題）。
    """
    ws.column_dimensions["A"].width = 9.1
    ws.sheet_view.topLeftCell = "A1"            # 切換到本頁時一律從頂端開始
    ws.sheet_view.selection[0].activeCell = ws.sheet_view.selection[0].sqref = "A1"
    r = 1
    spans, right = [], OV_IMG_PX           # 各區塊 (頂端, 底端)：(列 0 起算, 列內偏移 px)；right＝最寬圖片右緣
    tcols = _title_cols(f"{_stage_title(names, st)}結束" for st in stages)
    for st in stages:
        title = f"{_stage_title(names, st)}結束"
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
            spans.append(((r - 1, 0), (r - 1 + img.height // ROW_PX, img.height % ROW_PX)))
            right = max(right, img.width)
            r += -(-img.height // ROW_PX) + 2
        else:
            rows = 22 if shots is None else 11
            body = (f"【待截圖】HMI 數據概覽－電池概覽卡片\n{title}\n\n{PH_NOTE}" if shots is None
                    else f"HMI 數據概覽－電池概覽卡片\n{title}\n\n{text}")
            _placeholder(ws, r, 1, r + rows - 1, OV_COLS, body)
            spans.append(((r - 1, 0), (r - 1 + rows, 0)))
            r += rows + 2
    _shots_note(ws, r, 1, OV_COLS, shots)
    if spans:
        first, last = spans[0][0], spans[min(1, len(spans) - 1)][1]
        heights = {i: ws.row_dimensions[i].height for i in range(1, last[0] + 2) if ws.row_dimensions[i].height}
        ws._cert_order_arrow = (first, last, right + OV_ARROW_GAP_PX, heights, OV_ARROW_W_PX)   # 存檔後寫入 drawing


def _write_heat(ws, stages, names, shots=None):
    """即時熱度：各階段由左而右，淺橘標題 →「獨立櫃Pack 即時熱度圖」（置中）→ 獨立櫃 110 區塊（原尺寸 1:1）。"""
    paths = [_shot_for(shots, st, "heat") for st in stages]
    widths = [png_size(pth)[0] for pth, _, _ in paths if pth]
    # 區塊間距：以顯示縮放後較窄的 58px／欄計算，圖片右側至少留 2 欄
    pitch = max(12, -(-max(widths, default=0) // COL_PX_FIT) + 2)
    has_flag = any(f for _, _, f in paths)
    img_row = 3 + (1 if has_flag else 0)
    bottom = img_row + 38
    tcols = _title_cols(f"{_stage_title(names, st)}結束" for st in stages)
    for k, st in enumerate(stages):
        c = 1 + pitch * k
        title = f"{_stage_title(names, st)}結束"
        path, text, flag = paths[k]
        span = -(-png_size(path)[0] // COL_PX_FIT) if path else pitch - 2
        _title_row(ws, 1, c, tcols, title)
        ws.merge_cells(start_row=2, start_column=c, end_row=2, end_column=c + span - 1)
        head = ws.cell(2, c, HEAT_HEAD)
        head.font = F14B
        head.alignment = CENTER
        ws.row_dimensions[2].height = TITLE_ROW_PT
        if flag:
            _flag_cell(ws, 3, c, c + span - 1, flag)
        if shots is None:
            _placeholder(ws, img_row, c, img_row + 43, c + span - 1,
                         f"【待截圖】\n即時熱度－獨立櫃 110\n{title}\n\n{PH_NOTE}")
            bottom = max(bottom, img_row + 44)
            continue
        if path:
            img = PngImage(path)
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
            ws.add_chart(ch, _anchor(SUM_CHART_R if right else SUM_CHART_L, r0))
        else:
            c0 = 14 if right else 1
            _placeholder(ws, r0 + 1, c0, r0 + 4, c0 + 10,
                         f"{titles[st.no]}：不足一分鐘，無整分鐘 Log，不繪製圖表")
        if i + 1 < len(stages):
            text = arrow_text(res, st, stages[i + 1])
            n0, n_right = top(i + 1)
            if n0 == r0:
                arrows.append(("rightArrow" if n_right else "leftArrow", SUM_ARROW_H, r0, text))
            else:
                arrows.append(("downArrow", SUM_ARROW_DOWN_R if right else SUM_ARROW_DOWN_L, r0, text))
    orow = 4 + SUMMARY_PITCH * ((len(stages) + 1) // 2)
    if n_rows.get("overall"):
        ch = _make_chart(wb["整體測試"], n_rows["overall"], overall_title(meta))
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


def arrow_text(res, st, nxt):
    """
    Summary 箭頭文字：只顯示「N 分」（單行）。
      - 休息時間可確認（中間連續觀測）→ R − B
      - 否則 → 觀測間隔 R − A（下一階段首筆觀測運行 − 本階段最後觀測運行）：只是兩端觀測時間的差，
        不是已證實的待機時間；曲線不因此補值或連線。
    """
    sec, _why = rest_between(res, st, nxt)
    if sec is None:
        sec = (nxt.start.t - st.A.t).total_seconds()
    return [rest_minutes_text(sec)]


def rest_minutes_text(sec):
    """Summary 箭頭只顯示整分鐘，秒數無條件捨去（13 分 20 秒 → 「13 分」）；底層資料保留原始精度。"""
    return f"{int(sec // 60)} 分"


CN_DIGITS = "零一二三四五六七八九"


def _cn_num(n):
    """1～99 → 一、十、十一、二十三（圖表標題用）。"""
    if n < 10:
        return CN_DIGITS[n]
    t, u = divmod(n, 10)
    return ("" if t == 1 else CN_DIGITS[t]) + "十" + (CN_DIGITS[u] if u else "")


# 滿充／滿放（V1 固定規格）：方向相符 ＋ 階段首筆有效觀測 SOC 精確等於起始值 ＋ Menu 23 人工標記，三者同時成立。
# 完成與否以人工標記為準；起始 SOC 只是起始條件驗證。不以結束 SOC、Cell 電壓、上下電、故障、告警、idle 自動推論。
FULL_RULE = {"C": ("滿充", 0), "D": ("滿放", 100)}


def is_full_stage(st):
    word, soc0 = FULL_RULE[st.dir]
    return st.label == word and _num(st.start.values.get("soc")) == soc0


def summary_titles(stages, meta=None):
    """Summary 圖表標題：「250KW 第一次滿放」—— 依充／放電方向各自計數（不受標記影響）；
    符合 is_full_stage 才稱滿充／滿放，否則稱充電／放電。電櫃規格未輸入則不加前綴。"""
    cnt, out = {"C": 0, "D": 0}, {}
    for st in stages:
        cnt[st.dir] += 1
        word = FULL_RULE[st.dir][0] if is_full_stage(st) else st.dir_name
        out[st.no] = _with_spec(meta or {}, f"第{_cn_num(cnt[st.dir])}次{word}")
    return out


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
RAW_HEADERS = ["序號", "PC 取樣時間", "來源", "讀取結果", "狀態", "所屬階段", "對應報表分鐘",
               "SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓", "總電壓",
               "傳感器溫度", "PCS 模組溫度", "功率 kW", "說明"]
RAW_NOTE_COL = len(RAW_HEADERS)
RAW_NO_HMI = "無有效讀值"           # 讀取結果：ok=True 但 HMI 欄位全無值（只有 6161 溫度）


def _write_raw(ws, res, names, minute_maps, na_rows, p):
    for c, h in enumerate(RAW_HEADERS, 1):
        cell = ws.cell(1, c, h)
        cell.font = FB
        cell.fill = HDR_FILL
    items = [(o.t, 0, o) for o in res.obs] + [(T, 1, info) for T, info in na_rows.items()]
    items.sort(key=lambda x: (x[0], x[1]))
    r = 2
    for t, kind, x in items:
        if kind == 0:
            o = x
            ws.cell(r, 1, o.seq + 1)
            ws.cell(r, 2, o.t).number_format = "yyyy-mm-dd hh:mm:ss"
            ws.cell(r, 3, o.src)
            hmi = o.ok and has_hmi_values(o)
            ws.cell(r, 4, "成功" if hmi else ("失敗" if not o.ok else RAW_NO_HMI))
            ws.cell(r, 5, status_text(o, names))
            ws.cell(r, 6, names.get(o.stage_no, "") if o.stage_no else "")
            ws.cell(r, 7, "；".join(minute_maps.get(o.seq, [])))
            for c, k in enumerate(LOG_KEYS + ["power_kw"], 8):
                v = field_value(o, k, p)
                cell = ws.cell(r, c, _v(v))
                if k in LOG_FORMATS and v is not None:
                    cell.number_format = LOG_FORMATS[k]
            parts = [o.fail_reason] if not o.ok and o.fail_reason else []
            if o.ok and not hmi:
                # 舊資料 ok=True 也依數值判定；保留原始備註（例：HMI 讀取失敗：無讀值），6161 溫度照常顯示
                parts += [R_NO_HMI] + [n for n in o.notes if n]
                if any(_num(o.values.get(k)) is not None for k in ("sensor_c", "pcs_module_c")):
                    parts.append("僅 6161 溫度有效")
                parts.append(f"分析狀態：{o.state}")
            elif o.state not in ("充電", "放電"):
                # 追溯用：保留內部分析狀態與電池上下電原始值（狀態欄只顯示 待機／停機／N/A）
                parts.append(f"分析狀態：{o.state}")
                parts.append(f"電池上下電：{(o.flags or {}).get('battery_power_status') or '未保存'}")
                if (o.flags or {}).get("pcs_fault_flag") is True:
                    parts.append("PCS 故障")
            if o.sensor_ok is False:
                parts.append("傳感器關閉或異常")
            ws.cell(r, RAW_NOTE_COL, "；".join(parts))
        else:
            ws.cell(r, 2, t).number_format = "yyyy-mm-dd hh:mm:ss"
            ws.cell(r, 4, "（無取樣）")
            ws.cell(r, 6, x["stage"])
            ws.cell(r, 7, "；".join(x["labels"]))
            ws.cell(r, RAW_NOTE_COL, x["reason"])
            for c in range(1, RAW_NOTE_COL + 1):
                ws.cell(r, c).fill = WARN_FILL
        r += 1
    widths = [7, 20, 26, 9, 11, 13, 34, 7, 9, 13, 13, 13, 13, 9, 11, 13, 9, 30]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(RAW_NOTE_COL)}{r - 1}"


# =============================================================================
# 控制紀錄（Menu 4～7 指令；階段於產報時依指令時間與最終階段分析對應）
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
    names = stage_names(res.stages)
    names_full = {st.no: _stage_title(names, st) for st in res.stages}
    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"
    ws_ov = wb.create_sheet("數據概覽")
    ws_heat = wb.create_sheet("即時熱度")

    minute_maps = {}
    na_rows = {}

    def track(label, rows, stage_name):
        for mr in rows:
            if mr.obs is not None:
                d = int(round((mr.obs.t - mr.T).total_seconds()))
                minute_maps.setdefault(mr.obs.seq, []).append(
                    f"{label} {mr.T:%H:%M}（{d:+d} 秒）")
            else:
                e = na_rows.setdefault(mr.T, {"labels": [], "reason": mr.na_reason, "stage": ""})
                e["labels"].append(f"{label} {mr.T:%H:%M}（N/A）")
                if stage_name:
                    e["stage"] = stage_name

    sheet_of, n_rows = {}, {}
    for i, st in enumerate(res.stages, 1):
        ws = wb.create_sheet(_sheet_name(i, names, st))
        sheet_of[st.no] = ws.title
        rows = stage_minutes(res, st)
        n_rows[st.no] = len(rows)
        n_rows[("na", st.no)] = sum(1 for m in rows if m.obs is None)
        if rows:
            _write_log_sheet(ws, rows, _stage_remark, p, f"T_stage{i}")
            ws.add_chart(_make_chart(ws, len(rows), _with_spec(meta, names_full[st.no])), CHART_ANCHOR)
        else:
            _write_short_stage(ws, st, p)
        track(f"階段頁「{ws.title}」", rows, names_full[st.no])

    ws_all = wb.create_sheet("整體測試")
    orows = overall_minutes(res)
    n_rows["overall"] = len(orows)

    def overall_remark(mr):
        if mr.obs is None:
            return f"N/A：{mr.na_reason}"
        if not has_hmi_values(mr.obs):          # 容許範圍內只有僅 6161 溫度的觀測
            return f"N/A：{R_NO_HMI}，僅有 6161 溫度"
        return status_text(mr.obs, names_full)

    if orows:
        _write_log_sheet(ws_all, orows, overall_remark, p, "T_overall")
        ws_all.add_chart(_make_chart(ws_all, len(orows),
                                     overall_title(meta)), CHART_ANCHOR)
    track("整體", orows, "")

    shots = meta.get("screenshots")
    _write_overview(ws_ov, res.stages, names, shots)
    _write_heat(ws_heat, res.stages, names, shots)
    _write_summary(wb, ws_sum, res, names_full, sheet_of, n_rows, meta, p)
    _write_commands(wb.create_sheet("控制紀錄"), res, meta.get("commands"), names_full, p)
    _write_raw(wb.create_sheet("Raw Data"), res, names_full, minute_maps, na_rows, p)
    return wb


def save_report(res, meta, path):
    meta = dict(meta)
    meta.setdefault("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    wb = build_workbook(res, meta)
    # 來源說明（暫用參數、中斷、未完成等）寫入活頁簿屬性「描述」：不改動已確認的頁面版面
    notes = meta.get("source_notes") or []
    if notes:
        wb.properties.description = "\n".join(str(n) for n in notes)[:3000]
        wb.properties.title = "充／放電認證測試報告"
    wb.save(path)
    _force_plot_vis_off(path)
    _add_summary_arrows(path, getattr(wb, "_cert_summary_arrows", []))
    order = getattr(wb["數據概覽"], "_cert_order_arrow", None)
    if order:
        _inject_shapes(path, "數據概覽", _order_arrow_xml(*order))
    return path


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
