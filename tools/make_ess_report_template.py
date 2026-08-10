# -*- coding: utf-8 -*-
"""
make_ess_report_template.py
---------------------------
Generate ESS_Report_Template.xlsx  --  a *layout-only* template for
ESS charge / discharge meter-log reports.

The template fixes:
  * worksheet set + order
  * Summary KPI cell map
  * chart type / position / size / axis config
  * Raw Data column structure (Excel Table "RawData")

It ships with a small SAMPLE data set purely so the charts are real and
previewable. A downstream script (e.g. charge_discharge_report.py) is expected
to: clear Raw Data -> write new rows -> resize table + chart refs ->
fill Summary KPI -> save as report.xlsx.

Run:  python make_ess_report_template.py
"""

import math
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.chart import Reference, ScatterChart, Series
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.marker import Marker
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.text import RichText
from openpyxl.drawing.line import LineProperties
from openpyxl.drawing.text import (
    CharacterProperties,
    Paragraph,
    ParagraphProperties,
    RichTextProperties,
)
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.table import Table, TableStyleInfo

OUT_FILE = "ESS_Report_Template.xlsx"
PLACEHOLDER = "—"          # em dash shown when no data yet
SAMPLE_ROWS = 180               # sample rows written into Raw Data
RAW_SHEET = "Raw Data"

# --------------------------------------------------------------------------
# palette / styles
# --------------------------------------------------------------------------
NAVY_DARK = "1F3864"   # main title band
NAVY = "2F5597"        # section bands / table headers
BAND_LIGHT = "D9E2F3"  # column-header row
ZEBRA = "F2F5FA"       # light data area
GREY_TXT = "595959"
BORDER_C = "BFBFBF"

C_KW = "1F4E79"        # kW series
C_KWH_P = "ED7D31"     # kWh+ series (charge counter)
C_KWH_N = "70AD47"     # kWh- series (discharge counter)
C_BASE = "A6A6A6"      # 0 kW baseline

FONT_NAME = "Calibri"

F_TITLE = Font(name=FONT_NAME, size=16, bold=True, color="FFFFFF")
F_SUB = Font(name=FONT_NAME, size=9, color="D9E2F3")
F_BAND = Font(name=FONT_NAME, size=11, bold=True, color="FFFFFF")
F_HEAD = Font(name=FONT_NAME, size=10, bold=True, color=NAVY_DARK)
F_ITEM = Font(name=FONT_NAME, size=10, color="000000")
F_VALUE = Font(name=FONT_NAME, size=12, bold=True, color=NAVY_DARK)
F_UNIT = Font(name=FONT_NAME, size=10, color=GREY_TXT)
F_NOTE = Font(name=FONT_NAME, size=9, color=GREY_TXT)

FILL_TITLE = PatternFill("solid", fgColor=NAVY_DARK)
FILL_BAND = PatternFill("solid", fgColor=NAVY)
FILL_HEAD = PatternFill("solid", fgColor=BAND_LIGHT)
FILL_ZEBRA = PatternFill("solid", fgColor=ZEBRA)

_side = Side(style="thin", color=BORDER_C)
BOX = Border(left=_side, right=_side, top=_side, bottom=_side)

AL_L = Alignment(horizontal="left", vertical="center")
AL_R = Alignment(horizontal="right", vertical="center")
AL_C = Alignment(horizontal="center", vertical="center")

# number formats -- Python must re-apply these when writing new cells
NF_DATE = "yyyy-mm-dd"
NF_DT = "yyyy-mm-dd hh:mm:ss"
NF_DUR = "[h]:mm:ss"
NF_KW = "#,##0.00"
NF_KWH = "#,##0.00"
NF_COUNTER = "#,##0.0"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def band(ws, row, text, last_col=4, fill=FILL_BAND, font=F_BAND, height=20):
    """Coloured band across a row. No merged cells - text simply overflows."""
    for c in range(1, last_col + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = fill
    cell = ws.cell(row=row, column=1, value=text)
    cell.font = font
    cell.alignment = AL_L
    ws.row_dimensions[row].height = height


def axis_text(size=900, rot=None):
    body = RichTextProperties(vert="horz")
    if rot is not None:
        body.rot = rot
    # r=[] -> emit an empty <a:p> with no text run. openpyxl's reader turns a
    # self-closing <a:t/> into None and re-writes it as the literal string
    # "None" on the next load/save cycle, so leave the run out entirely.
    return RichText(
        bodyPr=body,
        p=[Paragraph(
            pPr=ParagraphProperties(
                defRPr=CharacterProperties(sz=size)),
            endParaRPr=CharacterProperties(sz=size),
            r=[])],
    )


# --------------------------------------------------------------------------
# Y-axis bounds rule  (shared with the report generator)
# --------------------------------------------------------------------------
# Excel's auto scale snaps a value axis to 0 once the data span falls below
# ~1/6 of the maximum. For cumulative kWh counters (~90,000 with an ~18,000
# span) that flattens both curves into straight lines. So every value axis in
# this template gets explicit min/max instead of auto scale.
#
# NOTE: axis min/max are literal numbers in the chart XML -- they cannot be
# formulas. The values baked into this template come from its SAMPLE data.
# A report generator MUST recompute them from the real data with the same
# function, otherwise the axis will not match the plotted values.
PAD_POWER = 0.10       # kW axes
PAD_ENERGY = 0.05      # kWh counter axes


def nice_floor(x):
    """Largest value of the form {1,2,5}x10^k that is <= x."""
    if x <= 0:
        return 1.0
    mag = 10.0 ** math.floor(math.log10(x))
    for m in (5.0, 2.0, 1.0):
        if m * mag <= x:
            return m * mag
    return mag / 10.0


def padded_bounds(lo, hi, frac):
    """Explicit axis bounds = data range padded by `frac`, snapped outward.

    Padding is a fraction of the SPAN, not of the absolute value: for a
    counter sitting at ~90,000, 5% of the value is 4,500 -- a quarter of the
    real span -- which re-flattens the curve the rule is meant to expose.

    The padded bounds are then snapped outward to a round step, because Excel
    labels a value axis upward from its minimum: an un-snapped minimum yields
    tick labels like 88,221 / 90,221 / 92,221. Snapping only ever ADDS
    padding, so the result always contains data +/- frac.

    Rules:
      * max == min  -> fall back to `frac` of |value| (min 2%), never 0 width
      * never forced to include 0; 0 appears only if the data reaches it
    """
    lo, hi = float(min(lo, hi)), float(max(lo, hi))
    span = hi - lo
    if span > 0:
        pad = span * frac
    else:
        pad = abs(hi) * max(frac, 0.02) or 1.0
    lo, hi = lo - pad, hi + pad
    step = nice_floor((hi - lo) / 20.0)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step


def series_bounds(rows, cols, frac):
    """padded_bounds over one or more columns of (ts, kW, kWh+, kWh-) rows."""
    vals = [r[c] for r in rows for c in cols]
    return padded_bounds(min(vals), max(vals), frac)


def set_y_bounds(axis, lo, hi):
    axis.scaling.min = lo
    axis.scaling.max = hi
    axis.scaling.orientation = "minMax"


def light_gridlines():
    gl = ChartLines()
    gl.spPr = GraphicalProperties()
    gl.spPr.line = LineProperties(solidFill="D9D9D9", w=9525)  # 0.75 pt
    return gl


def style_title(title_obj, size=1200, bold=True, colour=NAVY_DARK, rot=None):
    """Font / rotation for a chart or axis title.

    overlay=False is essential: openpyxl omits <c:overlay>, which makes Excel
    draw the title on top of the plot area / tick labels.
    """
    title_obj.overlay = False
    rich = title_obj.tx.rich
    if rot is not None:
        rich.bodyPr.rot = rot
        rich.bodyPr.vert = "horz"
    cp = CharacterProperties(sz=size, b=bold, solidFill=colour)
    for para in rich.p:
        para.pPr = ParagraphProperties(defRPr=cp)
        for run in (para.r or []):
            run.rPr = cp


def style_axes(chart, x_title, y_title, x_fmt="hh:mm", y_fmt="#,##0"):
    chart.x_axis.title = x_title
    chart.y_axis.title = y_title
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    chart.x_axis.majorTickMark = "out"
    chart.y_axis.majorTickMark = "out"
    chart.x_axis.minorTickMark = "none"
    chart.y_axis.minorTickMark = "none"
    chart.x_axis.number_format = x_fmt
    chart.y_axis.number_format = y_fmt
    # keep the time axis + its labels pinned to the bottom of the plot,
    # otherwise Excel draws them through the 0 kW crossing
    chart.x_axis.crosses = "min"
    chart.x_axis.tickLblPos = "low"
    chart.y_axis.crosses = "autoZero"
    chart.x_axis.majorGridlines = None          # vertical gridlines off
    chart.y_axis.majorGridlines = light_gridlines()   # horizontal gridlines on
    chart.x_axis.txPr = axis_text(900, rot=0)   # horizontal time labels
    chart.y_axis.txPr = axis_text(900)
    style_title(chart.x_axis.title, size=1000)
    style_title(chart.y_axis.title, size=1000, rot=-5400000)  # vertical


def style_chart_frame(chart, title_size=1250):
    """Title above the legend, compact legend, no overlap."""
    if chart.title is not None:
        chart.title.overlay = False
        style_title(chart.title, size=title_size)
    chart.legend.position = "t"
    chart.legend.overlay = False
    chart.legend.txPr = axis_text(900)


def line_series(y_ref, x_ref, colour, title=None, from_data=False,
                width=19050, dash=None):
    s = Series(y_ref, x_ref, title=title, title_from_data=from_data)
    s.marker = Marker(symbol="none")            # no marker per point
    s.smooth = False                            # straight lines
    s.graphicalProperties.line = LineProperties(solidFill=colour, w=width)
    if dash:
        s.graphicalProperties.line.dashStyle = dash
    return s


def chart_sheet_header(ws, title, note, last_col=16):
    band(ws, 1, title, last_col=last_col, fill=FILL_TITLE, font=F_TITLE,
         height=26)
    for c in range(1, last_col + 1):
        ws.cell(row=2, column=c).fill = FILL_TITLE
    n = ws.cell(row=2, column=1, value=note)
    n.font = F_SUB
    n.alignment = AL_L
    ws.row_dimensions[2].height = 14
    ws.row_dimensions[3].height = 6
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 85


def page_setup(ws, landscape=True, fit_w=1, fit_h=0, area=None,
               title_rows=None):
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = fit_w
    ws.page_setup.fitToHeight = fit_h
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = 0.5
    ws.page_margins.bottom = 0.5
    if area:
        ws.print_area = area
    if title_rows:
        ws.print_title_rows = title_rows
    ws.oddFooter.left.text = "ESS Charge / Discharge Report"
    ws.oddFooter.left.size = 8
    ws.oddFooter.right.text = "Page &P / &N"
    ws.oddFooter.right.size = 8


# --------------------------------------------------------------------------
# sample data (charge / discharge cycle, 1-minute steps)
# --------------------------------------------------------------------------
def build_sample():
    """Return list of (timestamp, kW, kWh_plus, kWh_minus).

    Sign convention: kW > 0 = charging, kW < 0 = discharging.
    """
    t0 = datetime(2026, 1, 1, 9, 0, 0)
    kwh_p, kwh_n = 105992.30, 89122.90
    rows = []
    for i in range(SAMPLE_ROWS):
        if i < 12:                       # ramp into discharge
            kw = -25.0 * i
        elif i < 62:                     # discharge plateau
            kw = -300.0 + 2.0 * math.sin(i / 3.0)
        elif i < 72:                     # ramp back to idle
            kw = -300.0 + 30.0 * (i - 62)
        elif i < 86:                     # idle
            kw = 0.4 * math.sin(i / 2.0)
        elif i < 98:                     # ramp into charge
            kw = 20.0 * (i - 86)
        elif i < 166:                    # charge plateau
            kw = 240.0 + 2.5 * math.sin(i / 4.0)
        else:                            # ramp to stop
            kw = max(0.0, 240.0 - 17.0 * (i - 166))
        step = kw / 60.0                 # kWh accumulated in one minute
        if step > 0:
            kwh_p += step
        else:
            kwh_n += -step
        rows.append((t0 + timedelta(minutes=i), round(kw, 2),
                     round(kwh_p, 1), round(kwh_n, 1)))
    return rows


# ==========================================================================
# 1. Summary
# ==========================================================================
SUMMARY_MAP = [
    # (row, kind, label, unit, remark, number_format, defined_name)
    (5,  "band", "1. Test Period / 測試區間", "", "", None, None),
    (6,  "kpi", "Report Date / 報表日期", "",
     "Log date", NF_DATE, "ESS_ReportDate"),
    (7,  "kpi", "Start Time / 開始時間", "",
     "First timestamp in Raw Data", NF_DT, "ESS_StartTime"),
    (8,  "kpi", "End Time / 結束時間", "",
     "Last timestamp in Raw Data", NF_DT, "ESS_EndTime"),
    (9,  "kpi", "Duration / 持續時間", "hh:mm:ss",
     "End Time - Start Time", NF_DUR, "ESS_Duration"),

    (10, "band", "2. Power / 功率", "", "", None, None),
    (11, "kpi", "Max Charge Power / 最大充電功率", "kW",
     "MAX(kW), kW > 0", NF_KW, "ESS_MaxChargePower"),
    (12, "kpi",
     "Max Discharge Power / 最大放電功率", "kW",
     "MIN(kW), kW < 0", NF_KW, "ESS_MaxDischargePower"),
    (13, "kpi", "Average Power / 平均功率", "kW",
     "AVERAGE(kW) over full period", NF_KW, "ESS_AvgPower"),

    (14, "band", "3. Session Energy / 本次電量", "", "",
     None, None),
    (15, "kpi", "Charge Energy / 本次充電量", "kWh",
     "End kWh+  -  Start kWh+", NF_KWH, "ESS_ChargeEnergy"),
    (16, "kpi", "Discharge Energy / 本次放電量", "kWh",
     "End kWh-  -  Start kWh-", NF_KWH, "ESS_DischargeEnergy"),
    (17, "kpi", "Net Energy / 本次淨電量", "kWh",
     "Charge Energy  -  Discharge Energy", NF_KWH, "ESS_NetEnergy"),

    (18, "band", "4. Meter Counter / 電表累計值", "", "",
     None, None),
    (19, "kpi", "Start kWh+ / 起始 kWh+", "kWh",
     "First kWh+ value", NF_COUNTER, "ESS_StartKWhPlus"),
    (20, "kpi", "End kWh+ / 結束 kWh+", "kWh",
     "Last kWh+ value", NF_COUNTER, "ESS_EndKWhPlus"),
    (21, "kpi", "Start kWh- / 起始 kWh-", "kWh",
     "First kWh- value", NF_COUNTER, "ESS_StartKWhMinus"),
    (22, "kpi", "End kWh- / 結束 kWh-", "kWh",
     "Last kWh- value", NF_COUNTER, "ESS_EndKWhMinus"),
]

SUMMARY_NOTES = [
    "Sign convention  /  符號定義："
    "kW > 0 = Charging (充電)，kW < 0 = Discharging (放電)",
    "Session energy is derived from meter counters, not from integrating kW.",
    "All charts reference the 'Raw Data' worksheet only "
    "(no external CSV links).",
    "Values shown as “—” are placeholders; "
    "they are overwritten by the report script.",
]


def build_summary(wb):
    ws = wb.create_sheet("Summary")
    ws.sheet_properties.tabColor = NAVY_DARK
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 110

    # B must fit "yyyy-mm-dd hh:mm:ss" at 12 pt bold, otherwise Excel
    # renders the datetime KPIs as ######
    for col, w in zip("ABCD", (38, 26, 10, 44)):
        ws.column_dimensions[col].width = w

    # title block -------------------------------------------------------
    band(ws, 1, "ESS Charge / Discharge Report", fill=FILL_TITLE,
         font=F_TITLE, height=30)
    for c in range(1, 5):
        ws.cell(row=2, column=c).fill = FILL_TITLE
    sub = ws.cell(row=2, column=1,
                  value="Energy Storage System  ·  Meter Log Analysis  "
                        "·  Template v1.0")
    sub.font = F_SUB
    ws.row_dimensions[2].height = 15
    ws.row_dimensions[3].height = 8

    # column header -----------------------------------------------------
    for c, txt in enumerate(("Item", "Value", "Unit", "Remark"), start=1):
        cell = ws.cell(row=4, column=c, value=txt)
        cell.fill = FILL_HEAD
        cell.font = F_HEAD
        cell.border = BOX
        cell.alignment = AL_C if c != 1 else AL_L
    ws.row_dimensions[4].height = 18

    # KPI rows ----------------------------------------------------------
    for row, kind, label, unit, remark, nf, dname in SUMMARY_MAP:
        if kind == "band":
            band(ws, row, label, height=18)
            continue
        ws.row_dimensions[row].height = 19

        a = ws.cell(row=row, column=1, value=label)
        a.font = F_ITEM
        a.alignment = AL_L
        a.border = BOX
        a.fill = FILL_ZEBRA

        b = ws.cell(row=row, column=2, value=PLACEHOLDER)
        b.font = F_VALUE
        b.alignment = AL_R
        b.border = BOX
        b.number_format = nf

        c = ws.cell(row=row, column=3, value=unit)
        c.font = F_UNIT
        c.alignment = AL_C
        c.border = BOX
        c.fill = FILL_ZEBRA

        d = ws.cell(row=row, column=4, value=remark)
        d.font = F_NOTE
        d.alignment = AL_L
        d.border = BOX
        d.fill = FILL_ZEBRA

        if dname:
            wb.defined_names[dname] = DefinedName(
                dname, attr_text="Summary!$B$%d" % row)

    # notes -------------------------------------------------------------
    ws.row_dimensions[23].height = 8
    band(ws, 24, "Notes / 說明", height=18)
    for i, txt in enumerate(SUMMARY_NOTES):
        cell = ws.cell(row=25 + i, column=1, value="•  " + txt)
        cell.font = F_NOTE
        cell.alignment = AL_L
        ws.row_dimensions[25 + i].height = 14

    ws.freeze_panes = "A5"
    page_setup(ws, landscape=False, area="A1:D%d" % (24 + len(SUMMARY_NOTES)))
    return ws


# ==========================================================================
# 5. Raw Data
# ==========================================================================
def build_raw(wb, sample):
    ws = wb.create_sheet(RAW_SHEET)
    ws.sheet_properties.tabColor = "7F7F7F"
    ws.sheet_view.zoomScale = 100

    headers = ("Timestamp", "kW", "kWh+", "kWh-")
    for c, txt in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=txt)
        cell.fill = FILL_TITLE
        cell.font = Font(name=FONT_NAME, size=10, bold=True, color="FFFFFF")
        cell.alignment = AL_C
    ws.row_dimensions[1].height = 20

    for i, (ts, kw, kp, kn) in enumerate(sample, start=2):
        ws.cell(row=i, column=1, value=ts).number_format = NF_DT
        ws.cell(row=i, column=2, value=kw).number_format = NF_KW
        ws.cell(row=i, column=3, value=kp).number_format = NF_COUNTER
        ws.cell(row=i, column=4, value=kn).number_format = NF_COUNTER

    last = len(sample) + 1

    # Excel Table -> filter buttons + dynamic expansion
    table = Table(displayName="RawData", ref="A1:D%d" % last)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleLight9", showFirstColumn=False, showLastColumn=False,
        showRowStripes=True, showColumnStripes=False)
    ws.add_table(table)

    for col, w in zip("ABCDEFG", (21, 12, 14, 14, 3, 30, 14)):
        ws.column_dimensions[col].width = w
    ws.column_dimensions["A"].number_format = NF_DT
    ws.column_dimensions["B"].number_format = NF_KW
    ws.column_dimensions["C"].number_format = NF_COUNTER
    ws.column_dimensions["D"].number_format = NF_COUNTER

    # ---- chart helper block (0 kW baseline, 2 points only) ------------
    h = ws.cell(row=1, column=6,
                value="Chart Helper — 0 kW baseline (do not delete)")
    h.font = Font(name=FONT_NAME, size=9, bold=True, color=NAVY_DARK)
    for r, txt in ((2, "X (Timestamp)"), ):
        ws.cell(row=r, column=6, value=txt).font = F_NOTE
    ws.cell(row=2, column=7, value="Y (kW)").font = F_NOTE
    ws.cell(row=3, column=6, value=sample[0][0]).number_format = NF_DT
    ws.cell(row=4, column=6, value=sample[-1][0]).number_format = NF_DT
    ws.cell(row=3, column=7, value=0).number_format = NF_KW
    ws.cell(row=4, column=7, value=0).number_format = NF_KW

    notes = [
        "",
        "Template notes for the report script:",
        "  • Table name       : RawData   (ref must be resized "
        "to A1:D<last_row>)",
        "  • Number formats   : A = " + NF_DT + " ,  B = " + NF_KW +
        " ,  C/D = " + NF_COUNTER,
        "  • Sign convention  : kW > 0 charging, kW < 0 discharging",
        "  • F3/F4 = min/max Timestamp -> feeds the 0 kW baseline "
        "series on 'Power Trend'",
        "  • Charts on Power Trend / Energy Counter / Power + Energy "
        "all point at this sheet",
        "  • Y axes: auto scale is OFF on every value axis "
        "(kW = span +/-10%, kWh counters = span +/-5%, never 0-based)",
        "  • Axis min/max are literal numbers, not formulas -- the report "
        "script must recompute them from the real",
        "    data using the same padded_bounds() rule, or the axis will not "
        "match the plotted values",
    ]
    for i, txt in enumerate(notes, start=6):
        ws.cell(row=i, column=6, value=txt).font = F_NOTE

    ws.freeze_panes = "A2"
    page_setup(ws, landscape=True, fit_w=1, fit_h=0, title_rows="1:1")
    return ws, last


# ==========================================================================
# 2. Power Trend
# ==========================================================================
def build_power_trend(wb, raw, last, sample):
    ws = wb.create_sheet("Power Trend")
    ws.sheet_properties.tabColor = C_KW
    chart_sheet_header(
        ws, "Power Trend (kW)",
        "Source: '%s'!A:B   ·   X = Timestamp   ·   "
        "Y = Power (kW)   ·   kW > 0 charging / kW < 0 discharging"
        % RAW_SHEET)

    ch = ScatterChart()
    ch.scatterStyle = "lineMarker"
    ch.title = "Power Trend (kW)"
    ch.height = 12.5
    ch.width = 31.0
    style_axes(ch, "Timestamp", "Power (kW)")
    style_chart_frame(ch)

    x = Reference(raw, min_col=1, min_row=2, max_row=last)
    y = Reference(raw, min_col=2, min_row=1, max_row=last)
    ch.series.append(line_series(y, x, C_KW, from_data=True, width=19050))

    bx = Reference(raw, min_col=6, min_row=3, max_row=4)
    by = Reference(raw, min_col=7, min_row=3, max_row=4)
    ch.series.append(line_series(by, bx, C_BASE, title="0 kW Baseline",
                                 width=12700, dash="dash"))

    # no auto scale: kW range +/- 10 % of span
    lo, hi = series_bounds(sample, (1,), PAD_POWER)
    set_y_bounds(ch.y_axis, lo, hi)

    ws.add_chart(ch, "A4")
    page_setup(ws, landscape=True, fit_w=1, fit_h=1, area="A1:R32")
    return ws


# ==========================================================================
# 3. Energy Counter
# ==========================================================================
def build_energy_counter(wb, raw, last, sample):
    ws = wb.create_sheet("Energy Counter")
    ws.sheet_properties.tabColor = C_KWH_P
    chart_sheet_header(
        ws, "Energy Counter (kWh)",
        "Source: '%s'!A:A , C:D   ·   X = Timestamp   ·   "
        "Y = Energy (kWh)   ·   cumulative meter counters" % RAW_SHEET)

    ch = ScatterChart()
    ch.scatterStyle = "lineMarker"
    ch.title = "Energy Counter (kWh)"
    ch.height = 12.0
    ch.width = 29.0
    style_axes(ch, "Timestamp", "Energy (kWh)")
    style_chart_frame(ch)

    x = Reference(raw, min_col=1, min_row=2, max_row=last)
    for col, colour in ((3, C_KWH_P), (4, C_KWH_N)):
        y = Reference(raw, min_col=col, min_row=1, max_row=last)
        ch.series.append(line_series(y, x, colour, from_data=True))

    # no auto scale: counters never start at 0, +/- 5 % of span
    lo, hi = series_bounds(sample, (2, 3), PAD_ENERGY)
    set_y_bounds(ch.y_axis, lo, hi)

    ws.add_chart(ch, "A4")
    page_setup(ws, landscape=True, fit_w=1, fit_h=1, area="A1:R32")
    return ws


# ==========================================================================
# 4. Power + Energy  (dual Y axis)
# ==========================================================================
def build_combo(wb, raw, last, sample):
    ws = wb.create_sheet("Power + Energy")
    ws.sheet_properties.tabColor = "7030A0"
    chart_sheet_header(
        ws, "Power and Energy Overview",
        "Source: '%s'!A:D   ·   left Y = Power (kW)   ·   "
        "right Y = Energy (kWh)   ·   overall trend reference"
        % RAW_SHEET)

    x = Reference(raw, min_col=1, min_row=2, max_row=last)

    # primary: kW on left axis
    c1 = ScatterChart()
    c1.scatterStyle = "lineMarker"
    c1.title = "Power and Energy Overview"
    c1.height = 13.0
    c1.width = 31.0
    style_axes(c1, "Timestamp", "Power (kW)")
    style_chart_frame(c1)
    c1.x_axis.axId = 10
    c1.y_axis.axId = 100
    c1.x_axis.crossAx = 100
    c1.y_axis.crossAx = 10
    c1.y_axis.crosses = "autoZero"
    y_kw = Reference(raw, min_col=2, min_row=1, max_row=last)
    c1.series.append(line_series(y_kw, x, C_KW, from_data=True, width=19050))

    # secondary: kWh+/kWh- on right axis
    c2 = ScatterChart()
    c2.scatterStyle = "lineMarker"
    c2.y_axis.title = "Energy (kWh)"
    c2.y_axis.axId = 200
    c2.x_axis.axId = 300
    c2.y_axis.crossAx = 300
    c2.x_axis.crossAx = 200
    c2.y_axis.crosses = "max"          # -> plotted on the right
    c2.y_axis.delete = False
    c2.x_axis.delete = True            # hidden secondary X axis
    c2.x_axis.majorGridlines = None    # no vertical gridlines from it
    c2.x_axis.tickLblPos = "none"
    c2.x_axis.majorTickMark = "none"
    c2.x_axis.minorTickMark = "none"
    c2.y_axis.majorTickMark = "out"
    c2.y_axis.majorGridlines = None    # keep only one gridline set
    c2.y_axis.number_format = "#,##0"
    c2.y_axis.txPr = axis_text(900)
    style_title(c2.y_axis.title, size=1000, rot=-5400000)
    for col, colour in ((3, C_KWH_P), (4, C_KWH_N)):
        y = Reference(raw, min_col=col, min_row=1, max_row=last)
        c2.series.append(line_series(y, x, colour, from_data=True,
                                     width=15875))
    # no auto scale on either axis
    set_y_bounds(c1.y_axis, *series_bounds(sample, (1,), PAD_POWER))
    set_y_bounds(c2.y_axis, *series_bounds(sample, (2, 3), PAD_ENERGY))

    c1 += c2

    ws.add_chart(c1, "A4")
    page_setup(ws, landscape=True, fit_w=1, fit_h=1, area="A1:R34")
    return ws


# ==========================================================================
def main():
    sample = build_sample()

    wb = Workbook()
    wb.remove(wb.active)

    build_summary(wb)
    raw, last = build_raw(wb, sample)          # created first, moved later
    build_power_trend(wb, raw, last, sample)
    build_energy_counter(wb, raw, last, sample)
    build_combo(wb, raw, last, sample)

    # required sheet order
    order = ["Summary", "Power Trend", "Energy Counter", "Power + Energy",
             RAW_SHEET]
    wb._sheets = [wb[n] for n in order]
    wb.active = 0

    # handy named ranges for the report script
    wb.defined_names["ESS_RawFirstRow"] = DefinedName(
        "ESS_RawFirstRow", attr_text="'%s'!$A$2" % RAW_SHEET)
    wb.defined_names["ESS_BaselineX"] = DefinedName(
        "ESS_BaselineX", attr_text="'%s'!$F$3:$F$4" % RAW_SHEET)

    wb.properties.title = "ESS Charge / Discharge Report Template"
    wb.properties.creator = "ESS Report Toolkit"

    wb.save(OUT_FILE)
    print("written: %s  (sample rows = %d, table ref = A1:D%d)"
          % (OUT_FILE, len(sample), last))
    print("sheets :", ", ".join(order))
    print("summary KPI cells: B6:B9, B11:B13, B15:B17, B19:B22")


if __name__ == "__main__":
    main()
