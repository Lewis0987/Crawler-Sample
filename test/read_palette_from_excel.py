# -*- coding: utf-8 -*-
"""
讀取「手動上色的 Excel 範例檔」→ 原封不動取出各等級填滿色，輸出可貼進
charge_discharge_report_config.CELL_COLOR_PALETTE 的字典。**不近似、不轉換、不自行挑色。**

用法：
  # 1) 產生待填色範本（每一等級一列，B 欄請在 Excel 手動填色後存檔）
  python read_palette_from_excel.py --template [輸出路徑.xlsx]

  # 2) 讀取你填好的範例檔，印出每格 fill 診斷 + 正規化色碼 + 可貼上的 CELL_COLOR_PALETTE
  python read_palette_from_excel.py <你的範例檔.xlsx> [工作表名稱]

約定版面（範本即照此產生；讀取時亦依此對應）：
  A 欄 = 等級鍵（英文，程式用；請勿更動）
  B 欄 = 供你手動填色的儲存格（讀取的就是這格的底色）
  C 欄 = 中文說明（僅輔助辨識）
"""
import sys
import zipfile
import colorsys
from xml.etree import ElementTree as ET
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill, Font, Alignment

# --- 佈景主題色解析（Theme + Tint → Excel 實際顯示 RGB）---
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
# color 的 theme 索引 → clrScheme 名稱（注意 dk1/lt1 對調：0=lt1 背景1、1=dk1 文字1）
_THEME_INDEX = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2",
                "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]


def load_theme_colors(xlsx_path):
    """讀 xl/theme/theme1.xml 的 clrScheme → {name: 'RRGGBB'}。"""
    with zipfile.ZipFile(xlsx_path) as z:
        names = [n for n in z.namelist() if n.startswith("xl/theme/theme") and n.endswith(".xml")]
        if not names:
            return {}
        root = ET.fromstring(z.read(sorted(names)[0]))
    scheme = root.find(f".//{_A}clrScheme")
    out = {}
    if scheme is None:
        return out
    for child in scheme:
        name = child.tag.split("}")[-1]
        srgb = child.find(f"{_A}srgbClr")
        sysc = child.find(f"{_A}sysClr")
        if srgb is not None and srgb.get("val"):
            out[name] = srgb.get("val").upper()
        elif sysc is not None:
            out[name] = (sysc.get("lastClr") or "000000").upper()
    return out


def apply_tint(rgb6, tint):
    """套用 OOXML tint（於 HSL 亮度）：tint<0 變暗、tint>0 變亮；回 'RRGGBB'。"""
    r = int(rgb6[0:2], 16) / 255.0
    g = int(rgb6[2:4], 16) / 255.0
    b = int(rgb6[4:6], 16) / 255.0
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if tint < 0:
        l = l * (1.0 + tint)
    else:
        l = l * (1.0 - tint) + tint
    l = max(0.0, min(1.0, l))
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return f"{round(r2 * 255):02X}{round(g2 * 255):02X}{round(b2 * 255):02X}"


def resolve_theme_rgb(theme_colors, theme_idx, tint):
    """theme 索引 + tint → 'RRGGBB'（Excel 實際顯示色）；無法解析回 None。"""
    if theme_idx is None or theme_idx < 0 or theme_idx >= len(_THEME_INDEX):
        return None
    base = theme_colors.get(_THEME_INDEX[theme_idx])
    if not base:
        return None
    return apply_tint(base, tint or 0.0)

# 等級鍵（由高到低，含 no_data）；名稱沿用你提供的範例。
LEVELS = [
    ("abnormal_high", "異常高值"),
    ("high",          "偏高"),
    ("medium_high",   "中高"),
    ("normal",        "正常"),
    ("medium_low",    "中低"),
    ("low",           "偏低"),
    ("abnormal_low",  "異常低值"),
    ("no_data",       "無資料 / N/A"),
]


def make_template(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Palette"
    ws["A1"] = "level_key（勿改）"
    ws["B1"] = "在此格手動填色 →"
    ws["C1"] = "說明"
    for c in ("A1", "B1", "C1"):
        ws[c].font = Font(bold=True)
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 20
    for i, (key, zh) in enumerate(LEVELS, start=2):
        ws.cell(row=i, column=1, value=key)
        b = ws.cell(row=i, column=2, value="")          # ← 請對這格填色
        b.alignment = Alignment(horizontal="center")
        ws.cell(row=i, column=3, value=zh)
    wb.save(path)
    print(f"[範本已建立] {path}")
    print("請在 Excel 中對每一列的 B 欄手動填入你要的顏色（填滿色），存檔後再執行：")
    print(f"  python read_palette_from_excel.py {path}")


def _norm(color):
    """回傳 (normalized_rgb 或 None, 診斷字串)。RGB/ARGB → 取後 6 位、強制 FF 前綴（不透明）。"""
    if color is None:
        return None, "fgColor=None"
    ctype = getattr(color, "type", None)
    rgb = color.rgb if isinstance(getattr(color, "rgb", None), str) else None
    theme = color.theme if isinstance(getattr(color, "theme", None), int) else None
    indexed = color.indexed if isinstance(getattr(color, "indexed", None), int) else None
    tint = getattr(color, "tint", 0.0)
    diag = f"type={ctype} rgb={rgb} theme={theme} indexed={indexed} tint={tint}"
    if ctype == "rgb" and rgb:
        return "FF" + rgb[-6:].upper(), diag        # 統一 8 位 ARGB、FF 不透明
    return None, diag                                # theme/indexed：只回報，不擅自轉換


def read_palette(path, sheet=None, resolve_theme=False):
    """回傳 (palette dict, missing list)。resolve_theme=True 時（方案 B）將 theme+tint 解析為實際 RGB。"""
    wb = load_workbook(path)
    ws = wb[sheet] if sheet else wb.active
    theme_colors = load_theme_colors(path)
    print(f"[讀取] {path}  工作表={ws.title}  resolve_theme={resolve_theme}")
    if resolve_theme:
        print(f"       佈景主題色盤：{theme_colors}\n")
    rows = {}
    for r in range(2, ws.max_row + 1):
        k = ws.cell(row=r, column=1).value
        if k:
            rows[str(k).strip()] = r
    palette = {}
    missing = []
    for key, zh in LEVELS:
        r = rows.get(key)
        if not r:
            print(f"  {key:<14}({zh})  → ⚠ 範例檔找不到此等級")
            missing.append(key)
            continue
        fill = ws.cell(row=r, column=2).fill
        fill_type = fill.patternType if fill else None
        if not fill_type:                             # 未填色 → 缺，勿誤讀 00000000
            print(f"  {key:<14}({zh})  fill_type=None  → ⚠ 尚未填色")
            missing.append(key)
            continue
        fg = fill.fgColor
        norm, diag = _norm(fg)
        print(f"  {key:<14}({zh})  fill_type={fill_type}  {diag}", end="")
        if norm:
            palette[key] = norm
            print(f"   → RGB {norm}")
        elif resolve_theme and getattr(fg, "type", None) == "theme":
            resolved = resolve_theme_rgb(theme_colors,
                                         fg.theme if isinstance(fg.theme, int) else None,
                                         getattr(fg, "tint", 0.0))
            if resolved:
                argb = "FF" + resolved
                palette[key] = argb
                base = _THEME_INDEX[fg.theme] if isinstance(fg.theme, int) and fg.theme < len(_THEME_INDEX) else "?"
                print(f"   → 解析 theme={fg.theme}({base}) tint={round(fg.tint,6)} = RGB {argb}")
            else:
                print("   → ⚠ 主題色無法解析")
                missing.append(key)
        else:
            print("   → ⚠ 非 RGB（theme/indexed）未解析")
            missing.append(key)
    print("\n# ---- 讀取結果（可貼進 charge_discharge_report_config.CELL_COLOR_PALETTE）----")
    print("CELL_COLOR_PALETTE = {")
    for key, _zh in LEVELS:
        if key in palette:
            print(f'    "{key}": "{palette[key]}",')
    print("}")
    if missing:
        print(f"\n⚠ 缺少/未解析等級：{missing} → 依規則停止套用（不可用舊色補值）")
    return palette, missing


def main():
    args = [a for a in sys.argv[1:] if a != "--resolve-theme"]
    resolve = "--resolve-theme" in sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "--template":
        out = args[1] if len(args) > 1 else "cell_palette_template.xlsx"
        make_template(out)
        return
    read_palette(args[0], args[1] if len(args) > 1 else None, resolve_theme=resolve)


if __name__ == "__main__":
    main()
