# -*- coding: utf-8 -*-
"""
taipower_calendar_extractor.py — 台電年度時間電價日曆表解析器（Phase D.4-E.1）
======================================================================
把**台電官方年度時間電價日曆表 PDF**還原成「每一天屬於哪一種日型」。

    官方 PDF（人工下載）
            ↓  本檔（純標準庫、零網路）
    {日期: OFF_PEAK / SATURDAY / WEEKDAY} + 完整性自檢
            ↓
    official_annual_calendar.py（草稿，verified=false）
            ↓
    人工核准
            ↓
    Production Annual List

🔴 **零網路**
    本檔**不下載任何東西**，只接受呼叫端給的 bytes 或本機檔案路徑。
    官方檔案一律由人工下載並記錄 hash —— 自動控制不得在 runtime 抓網路資料。

🔴 **零第三方相依**
    本機沒有任何 PDF 函式庫，且**不得安裝**。因此這裡只用標準庫：
    `zlib` 解 FlateDecode、自寫 content-stream tokenizer、
    自解 `ToUnicode` CMap 還原 Identity-H 中文。

🔴 **日型不是文字，是儲存格顏色**
    官方日曆表的圖例原文：
        「表示週日或離峰日，全日 24 小時均為離峰時間；
          表示週六，其中有 15 小時適用週六半尖峰電價，其餘 9 小時適用離峰電價；
          表示平日……」
    因此必須讀矩形填色並與日期數字做幾何對位，**不能只讀文字**。
    ⚠️ 各年度檔案的紅色色值**不完全相同**（曾見 #FF0000 與 #EE0000），
       因此以色域判定，**不得**硬編碼單一色值。

🔴 **Fail Closed**
    任何一項對不上就整份拒絕，回 (None, 原因)：
    寧可「解析不出來」，也不要交出一份看起來合理但其實錯位的年度資料。

🔴 **不做農曆換算、不套任何節日公式**
    年度日期一律以日曆表本身的顏色為準（Source Authority Priority 1）。

用法（完全離線）
    python taipower_calendar_extractor.py <官方日曆表.pdf>
"""

import io
import re
import sys
import zlib
import hashlib
import argparse
from datetime import date
from dataclasses import dataclass

# ======================================================================
# 日型
# ======================================================================
DT_OFF_PEAK = "OFF_PEAK"   # 週日或離峰日：全日離峰
DT_SATURDAY = "SATURDAY"   # 週六：部分半尖峰、部分離峰
DT_WEEKDAY = "WEEKDAY"     # 平日
DAY_TYPES = (DT_OFF_PEAK, DT_SATURDAY, DT_WEEKDAY)

# ---- 解析結果 ----
CAL_OK = "CALENDAR_OK"
CAL_NOT_PDF = "CALENDAR_NOT_A_PDF"
CAL_NO_PAGE = "CALENDAR_PAGE_NOT_FOUND"
CAL_STREAM_UNREADABLE = "CALENDAR_STREAM_UNREADABLE"
CAL_TITLE_UNRECOGNISED = "CALENDAR_TITLE_UNRECOGNISED"
CAL_GRID_INCOMPLETE = "CALENDAR_GRID_INCOMPLETE"
CAL_CELL_AMBIGUOUS = "CALENDAR_COLOURED_CELL_AMBIGUOUS"
CAL_INCONSISTENT = "CALENDAR_DAYTYPE_INCONSISTENT"
CAL_FILE_UNREADABLE = "CALENDAR_FILE_UNREADABLE"

# ======================================================================
# 版面常數（官方日曆表為固定模板：3 欄 × 4 列共 12 個月）
# ======================================================================
MONTH_COL_X = (64.0, 226.0, 388.0)          # 三欄的左緣
MONTH_ROW_Y = (716.1, 574.6, 433.2, 291.7)  # 四列月份標題的基線
CELL_W_RANGE = (17.0, 22.0)                 # 儲存格寬
CELL_H_RANGE = (12.0, 18.0)                 # 儲存格高
# 日期數字相對於所屬儲存格左下角的位移（實測極穩定，用來做 1:1 對位）
NUM_DX_RANGE = (2.0, 9.0)
NUM_DY_RANGE = (3.0, 7.0)
GRID_MIN_Y = 150.0                           # 低於此為註解區，不是日曆格


def is_off_peak_colour(c):
    """紅系＝週日或離峰日。以色域判定，**不硬編碼**單一色值。"""
    return c[0] > 0.8 and c[1] < 0.25 and c[2] < 0.25


def is_saturday_colour(c):
    """黃系＝週六。"""
    return c[0] > 0.8 and c[1] > 0.8 and c[2] < 0.3


@dataclass(frozen=True)
class CalendarPage:
    """單一年度頁的解析結果。`day_types` 涵蓋該年度**每一天**。"""
    year: int
    title: str
    made_on: str                 # 檔案上的「製表」民國日期，例如 115/7/2
    day_types: dict
    legend: tuple                # 圖例原文（供人工核對語意）

    @property
    def off_peak_dates(self):
        """全部離峰日（含週日）。"""
        return tuple(d for d in sorted(self.day_types)
                     if self.day_types[d] == DT_OFF_PEAK)

    @property
    def non_sunday_off_peak_dates(self):
        """
        非週日的離峰日 —— 即年度清單真正需要的部分。

        週日已由既有日型規則涵蓋（見 taipower_offpeak_rules.COVERED_BY_DAYTYPE_RULE），
        不需要、也不應該重複寫進年度清單。
        """
        return tuple(d for d in self.off_peak_dates if d.weekday() != 6)

    def day_type_digest(self):
        """整年度日型序列的 SHA-256 —— 供離線比對與跨來源互證。"""
        return day_type_digest(self.day_types)

    def __str__(self):
        return (f"[CALENDAR {self.year}] {self.title} 製表={self.made_on} "
                f"離峰日={len(self.off_peak_dates)}（非週日 "
                f"{len(self.non_sunday_off_peak_dates)}）")


def day_type_digest(day_types):
    """日型序列摘要。順序固定，因此可跨機器、跨來源比對。"""
    s = ";".join("%s=%s" % (d.isoformat(), day_types[d])
                 for d in sorted(day_types))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def dataset_digest(dates):
    """日期集合摘要（已排序去重）。"""
    s = ";".join(d.isoformat() for d in sorted(set(dates)))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_of(data):
    return hashlib.sha256(data).hexdigest()


# ======================================================================
# 最小 PDF 讀取（只支援本用途所需的部分）
# ======================================================================
_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj\b", re.S)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def _objects(data):
    return {int(m.group(1)): m.group(3) for m in _OBJ.finditer(data)}


def _stream(objs, num):
    body = objs.get(num)
    if body is None:
        return None
    m = _STREAM.search(body)
    if not m:
        return None
    if b"/FlateDecode" in body:
        try:
            return zlib.decompress(m.group(1))
        except zlib.error:
            return None
    return m.group(1)


def _tounicode(objs, num):
    """解析 ToUnicode CMap → {CID: 字}。"""
    raw = _stream(objs, num)
    if raw is None:
        return {}
    text = raw.decode("latin-1")
    mp = {}
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            mp[int(src, 16)] = "".join(chr(int(dst[i:i + 4], 16))
                                       for i in range(0, len(dst), 4))
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            lo, hi, base = int(lo, 16), int(hi, 16), int(dst, 16)
            for i in range(hi - lo + 1):
                mp[lo + i] = chr(base + i)
    return mp


_TOKEN = re.compile(
    r"\((?:[^()\\]|\\.)*\)"          # 字串
    r"|<[0-9A-Fa-f\s]*>"             # 十六進位字串
    r"|\[|\]"
    r"|[-+]?[\d.]+"                  # 數字
    r"|/[^\s/\[\]<>()]+"             # 名稱
    r"|[A-Za-z'\"*]+")               # 運算子


def _unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 1
            if i >= len(s):
                break
            n = s[i]
            if n in "nrtbf":
                out.append({"n": "\n", "r": "\r", "t": "\t",
                            "b": "\b", "f": "\f"}[n])
            elif n.isdigit():
                o = n
                while len(o) < 3 and i + 1 < len(s) and s[i + 1].isdigit():
                    i += 1
                    o += s[i]
                out.append(chr(int(o, 8)))
            else:
                out.append(n)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _page_content(objs, page_num):
    """回傳 (content_text, {資源名: cmap})。"""
    page = objs[page_num].decode("latin-1")
    fonts = {}
    res = re.search(r"/Font\s*<<(.*?)>>", page, re.S)
    if res:
        for name, num in re.findall(r"/(\w+)\s+(\d+)\s+0\s+R", res.group(1)):
            fo = objs.get(int(num), b"").decode("latin-1")
            tu = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", fo)
            if tu:
                fonts[name] = _tounicode(objs, int(tu.group(1)))
    cm = re.search(r"/Contents\s+(\d+)", page)
    if not cm:
        return None, None
    raw = _stream(objs, int(cm.group(1)))
    if raw is None:
        return None, None
    return raw.decode("latin-1"), fonts


def _walk(content, fonts):
    """走一遍 content stream，收集文字片段與矩形。"""
    items, rects = [], []
    font, tm, fill = "", (0.0, 0.0), (0.0, 0.0, 0.0)
    stack = []

    def nums(k):
        v = [x for x in stack if re.fullmatch(r"[-+]?[\d.]+", x)]
        return [float(x) for x in v[-k:]] if len(v) >= k else None

    for tok in _TOKEN.finditer(content):
        t = tok.group(0)
        if (t.startswith("/") or t in ("[", "]") or t.startswith("(")
                or t.startswith("<") or re.fullmatch(r"[-+]?[\d.]+", t)):
            stack.append(t)
            continue
        op = t
        if op == "Tf":
            names = [x for x in stack if x.startswith("/")]
            if names:
                font = names[-1][1:]
        elif op == "Tm":
            v = nums(6)
            if v:
                tm = (v[4], v[5])
        elif op in ("Td", "TD"):
            v = nums(2)
            if v:
                tm = (tm[0] + v[0], tm[1] + v[1])
        elif op in ("rg", "sc", "scn"):
            v = nums(3)
            if v:
                fill = tuple(round(x, 3) for x in v)
        elif op == "g":
            v = nums(1)
            if v:
                fill = (round(v[0], 3),) * 3
        elif op == "k":
            v = nums(4)
            if v:
                c, m, y, kk = v
                fill = tuple(round((1 - x) * (1 - kk), 3) for x in (c, m, y))
        elif op == "re":
            v = nums(4)
            if v:
                rects.append((v[0], v[1], v[2], v[3], fill))
        elif op in ("Tj", "TJ", "'", '"'):
            parts = []
            for x in reversed(stack):
                if x == "[":
                    break
                if x.startswith("(") or x.startswith("<"):
                    parts.append(x)
            parts.reverse()
            buf = ""
            cmap = fonts.get(font)
            for s in parts:
                if s.startswith("("):
                    raw = _unescape(s[1:-1])
                    if cmap is not None:
                        bs = raw.encode("latin-1")
                        buf += "".join(cmap.get((bs[i] << 8) | bs[i + 1], "�")
                                       for i in range(0, len(bs) - 1, 2))
                    else:
                        buf += raw
                else:
                    hx = re.sub(r"\s", "", s[1:-1])
                    if cmap is not None:
                        buf += "".join(cmap.get(int(hx[i:i + 4], 16), "�")
                                       for i in range(0, len(hx) - 3, 4))
                    else:
                        buf += "".join(chr(int(hx[i:i + 2], 16))
                                       for i in range(0, len(hx) - 1, 2))
            if buf.strip():
                items.append((round(tm[0], 2), round(tm[1], 2), buf))
        stack = []
    return items, rects


# ======================================================================
# 幾何還原
# ======================================================================
def _cells(rects):
    """把儲存格大小的矩形群聚（同一格會被重複描繪）。"""
    picked = []
    for x, y, w, h, fill in rects:
        hh = abs(h)
        if not (CELL_W_RANGE[0] <= w <= CELL_W_RANGE[1]):
            continue
        if not (CELL_H_RANGE[0] <= hh <= CELL_H_RANGE[1]):
            continue
        y0 = y + h if h < 0 else y
        if y0 <= GRID_MIN_Y:
            continue
        picked.append((x, y0, w, hh, fill))
    clusters = []
    for x, y, w, h, fill in sorted(picked):
        for cl in clusters:
            if abs(cl[0] - x) < 4 and abs(cl[1] - y) < 4:
                cl[4].add(fill)
                break
        else:
            clusters.append([x, y, w, h, {fill}])
    return clusters


def _month_of(x, y):
    band = [i for i, ry in enumerate(MONTH_ROW_Y) if y < ry]
    cols = [i for i, cx in enumerate(MONTH_COL_X) if x >= cx - 6]
    if not band or not cols:
        return None
    return max(band) * 3 + max(cols) + 1


def _title_year(items):
    """由頁面標題取民國年（例：「116年時間電價日曆表」→ 2027）。"""
    top = "".join(t for _, y, t in sorted(items, key=lambda a: -a[1])
                  if y > 745)
    m = re.search(r"(\d{2,3})\s*年", top)
    if not m:
        return None, None
    return int(m.group(1)) + 1911, top


def _made_on(items):
    """
    取檔案上的「製表」民國日期（例：「115/7/2製」）。

    ⚠️ 這一行在 PDF 裡是逐字散開的，必須先依 x 由左而右接回來，
       只抓「含『製』的那一個片段」只會拿到單一個「製」字。
    """
    band = [(x, t) for x, y, t in items if 725 < y < 745]
    joined = "".join(t for _, t in sorted(band)).strip()
    return joined or None


def parse_page(objs, page_num):
    """解析單一頁 → (CalendarPage_or_None, reason)。任何異常一律 Fail Closed。"""
    content, fonts = _page_content(objs, page_num)
    if content is None:
        return None, CAL_STREAM_UNREADABLE
    items, rects = _walk(content, fonts)
    year, title = _title_year(items)
    if year is None:
        return None, CAL_TITLE_UNRECOGNISED

    nums = [(x, y, int(t)) for x, y, t in items
            if t.isdigit() and 1 <= int(t) <= 31 and GRID_MIN_Y < y < 700]

    # --- 日期數字必須恰好覆蓋整個年度 ---
    seen = {}
    for x, y, d in nums:
        m = _month_of(x, y)
        if m is None:
            return None, CAL_GRID_INCOMPLETE
        if (m, d) in seen:
            return None, CAL_GRID_INCOMPLETE
        seen[(m, d)] = (x, y)
    expected = set()
    cur = date(year, 1, 1)
    while cur.year == year:
        expected.add((cur.month, cur.day))
        cur = date.fromordinal(cur.toordinal() + 1)
    if set(seen) != expected:
        return None, CAL_GRID_INCOMPLETE

    # --- 有色儲存格與日期數字 1:1 對位 ---
    day_types = {}
    for x, y, w, h, fills in _cells(rects):
        hits = [(mm, dd) for (mm, dd), (nx, ny) in seen.items()
                if NUM_DX_RANGE[0] <= nx - x <= NUM_DX_RANGE[1]
                and NUM_DY_RANGE[0] <= ny - y <= NUM_DY_RANGE[1]]
        if len(hits) != 1:
            return None, CAL_CELL_AMBIGUOUS
        m, d = hits[0]
        if any(is_off_peak_colour(c) for c in fills):
            kind = DT_OFF_PEAK
        elif any(is_saturday_colour(c) for c in fills):
            kind = DT_SATURDAY
        else:
            return None, CAL_CELL_AMBIGUOUS
        key = date(year, m, d)
        if key in day_types and day_types[key] != kind:
            return None, CAL_CELL_AMBIGUOUS
        day_types[key] = kind
    for m, d in expected:
        day_types.setdefault(date(year, m, d), DT_WEEKDAY)

    ok, why = check_integrity(year, day_types)
    if not ok:
        return None, why

    # 圖例原文：同樣是逐字散開的，先依 y 分行、再依 x 接回來
    lines = {}
    for x, y, t in items:
        if y < GRID_MIN_Y:
            lines.setdefault(round(y), []).append((x, t))
    legend = tuple("".join(t for _, t in sorted(lines[y])).strip()
                   for y in sorted(lines, reverse=True))
    return CalendarPage(year=year, title=title, made_on=_made_on(items),
                        day_types=day_types, legend=legend), CAL_OK


def check_integrity(year, day_types):
    """
    日型自我一致性檢核。回傳 (bool, reason)。

    這些不是「額外的保險」，而是**解析正確性的證明**：
    幾何對位一旦錯位，星期分布必然崩掉，下列任何一項都會立刻失敗。
    """
    cur, n = date(year, 1, 1), 0
    while cur.year == year:
        n += 1
        k = day_types.get(cur)
        if k not in DAY_TYPES:
            return False, CAL_INCONSISTENT
        if cur.weekday() == 6 and k != DT_OFF_PEAK:
            # 官方圖例：週日一律為離峰日
            return False, CAL_INCONSISTENT
        if cur.weekday() == 5 and k == DT_WEEKDAY:
            # 週六一定有顏色（週六色或離峰色）
            return False, CAL_INCONSISTENT
        if cur.weekday() < 5 and k == DT_SATURDAY:
            # 平日不可能被標成週六
            return False, CAL_INCONSISTENT
        cur = date.fromordinal(cur.toordinal() + 1)
    if n != len(day_types):
        return False, CAL_INCONSISTENT
    return True, CAL_OK


def derive_day_types(year, non_sunday_off_peak_dates):
    """
    由「非週日離峰日清單」還原整年度日型。

    這是 official_annual_calendar 只保存 off_peak_dates、卻仍能證明
    OFF_PEAK / SATURDAY / WEEKDAY 三態的關鍵：還原結果的摘要必須與
    當初從 PDF 解析出來的摘要**完全相同**（由測試鎖住）。
    """
    extra = set(non_sunday_off_peak_dates)
    out, cur = {}, date(year, 1, 1)
    while cur.year == year:
        if cur.weekday() == 6 or cur in extra:
            out[cur] = DT_OFF_PEAK
        elif cur.weekday() == 5:
            out[cur] = DT_SATURDAY
        else:
            out[cur] = DT_WEEKDAY
        cur = date.fromordinal(cur.toordinal() + 1)
    return out


def parse_pdf(data):
    """
    解析整份官方日曆表 PDF → (pages_tuple, reason)。

    ⚠️ 只要**任何一頁**解析失敗就整份拒絕 —— 不交出半份資料。
    """
    if not data[:5].startswith(b"%PDF"):
        return None, CAL_NOT_PDF
    objs = _objects(data)
    page_nums = [k for k, v in sorted(objs.items())
                 if re.search(rb"/Type\s*/Page\b", v)
                 and not re.search(rb"/Type\s*/Pages\b", v)]
    if not page_nums:
        return None, CAL_NO_PAGE
    pages = []
    for num in page_nums:
        page, why = parse_page(objs, num)
        if page is None:
            return None, why
        pages.append(page)
    return tuple(sorted(pages, key=lambda p: p.year)), CAL_OK


def parse_file(path):
    """由本機檔案解析 → (pages, reason, sha256)。**零網路**。"""
    try:
        data = io.open(path, "rb").read()
    except OSError:
        return None, CAL_FILE_UNREADABLE, None
    pages, why = parse_pdf(data)
    return pages, why, sha256_of(data)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="台電年度時間電價日曆表解析（唯讀、離線、零網路）")
    ap.add_argument("pdf", help="人工下載的官方日曆表 PDF 路徑")
    args = ap.parse_args(argv)

    pages, why, digest = parse_file(args.pdf)
    print("== 台電年度時間電價日曆表解析（D.4-E.1，完全離線）==\n")
    print(f"  檔案 sha256 = {digest}")
    if pages is None:
        print(f"  ❌ 解析失敗：{why}")
        print("  （Fail Closed：不交出任何部分結果）")
        return 1
    for p in pages:
        print(f"\n  {p}")
        print(f"    非週日離峰日（{len(p.non_sunday_off_peak_dates)} 天）：")
        for d in p.non_sunday_off_peak_dates:
            print(f"      {d.isoformat()}  {'一二三四五六日'[d.weekday()]}")
        print(f"    日型摘要 = {p.day_type_digest()}")
        rebuilt = derive_day_types(p.year, p.non_sunday_off_peak_dates)
        same = day_type_digest(rebuilt) == p.day_type_digest()
        print(f"    由離峰日清單還原三態日型 == PDF 解析結果：{same}")
    print("\n  ⚠ 本檔不下載任何東西；官方檔案一律人工下載並記錄 hash。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
