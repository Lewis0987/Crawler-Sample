# -*- coding: utf-8 -*-
"""
annual_off_peak_calendar.py — 年度離峰日清單（Phase D.4-A）
======================================================================
提供「某一天是不是台電電價表所稱的**離峰日**」的**可追溯、可版本控管**答案。

    經核准的年度清單（隨程式版本控管）
            ↓
    AnnualOffPeakDayProvider（實作既有 HolidayProvider 介面）
            ↓
    既有時段規則 → 時段判定

🔴 **不使用任何外部服務**
    不連政府 API、不用第三方 holiday 套件、不做任何 runtime 網路查詢。
    自動控制不能因為外部服務掛掉就突然不知道今天能不能充放電。

🔴 **不自行產生 production 日期**
    本檔**不包含任何實際的離峰日**，一個日期字面值都沒有。
    `PRODUCTION_ANNUAL_LISTS` 的內容來自 official_annual_calendar 中
    **經人工驗收**的年度清單（Phase D.4-E.2），其日期來自台電官方年度
    時間電價日曆表的解析結果。未經驗收的年度一律「未知」→ 時段 UNKNOWN。

🔴 **三態語意（沿用既有 HolidayProvider 契約，未修改）**
    True   該日確定是離峰日
    False  該日確定不是離峰日
    None   **無法判定** —— 年度清單缺失／未驗證／資料無效
    ⚠️ None **絕不**等同 False。「查不到假日」不得推論成「不是假日」。

🔴 **未驗證的清單一律不可用**
    `verified=False` 的清單視同不存在 —— 避免草稿資料悄悄成為 production 依據。

資料來源要求（每份清單都必須完整填寫）
    source          出處（例如：正式電價表／客戶提供之年度行事曆）
    source_version  版本或發布日期，供事後追溯
    effective_date  生效日
    verified        是否已經人工核准

用法（完全離線）
    python annual_off_peak_calendar.py --show
"""

import io
import sys as _sys
import json
import argparse
from datetime import date
from dataclasses import dataclass, field, asdict

import tou_calendar as TC

# ======================================================================
# 驗證結果
# ======================================================================
AL_OK = "ANNUAL_LIST_OK"
AL_YEAR_INVALID = "ANNUAL_LIST_YEAR_INVALID"
AL_DATE_INVALID = "ANNUAL_LIST_DATE_INVALID"
AL_DATE_WRONG_YEAR = "ANNUAL_LIST_DATE_WRONG_YEAR"
AL_DUPLICATE_DATE = "ANNUAL_LIST_DUPLICATE_DATE"
AL_SOURCE_MISSING = "ANNUAL_LIST_SOURCE_MISSING"
AL_EFFECTIVE_DATE_INVALID = "ANNUAL_LIST_EFFECTIVE_DATE_INVALID"
AL_NOT_VERIFIED = "ANNUAL_LIST_NOT_VERIFIED"
AL_SCHEMA_INVALID = "ANNUAL_LIST_SCHEMA_INVALID"
AL_SCHEMA_UNSUPPORTED = "ANNUAL_LIST_SCHEMA_UNSUPPORTED"
AL_FILE_UNREADABLE = "ANNUAL_LIST_FILE_UNREADABLE"

# ---- 年度資料就緒狀態（供啟動檢查；**不因此啟用任何控制**）----
AR_AVAILABLE = "AVAILABLE"      # 已核准且通過驗證
AR_MISSING = "MISSING"          # 該年度根本沒有資料
AR_UNVERIFIED = "UNVERIFIED"    # 有資料但未經人工核准 → 視同不存在
AR_INVALID = "INVALID"          # 有資料但驗證失敗 → 視同不存在
READINESS_STATES = frozenset({AR_AVAILABLE, AR_MISSING, AR_UNVERIFIED, AR_INVALID})

# 供人工 provisioning 的檔案格式版本
SCHEMA_VERSION = 1

# 年度合理範圍（純結構性防呆，不是政策）
MIN_YEAR, MAX_YEAR = 2000, 2100


@dataclass(frozen=True)
class AnnualOffPeakDayList:
    """
    單一年度的離峰日清單。

    ⚠️ `off_peak_dates` 為**已排序且去重**的 tuple —— 確保序列化與比對具決定性。
    ⚠️ 空清單是**合法但特殊**的：它明確宣告「該年度沒有任何離峰日」。
       這在台灣幾乎不可能成立，因此必須是人工核准（verified）的刻意宣告，
       不得由「資料還沒填」演變而來。
    """
    year: int
    off_peak_dates: tuple
    source: str
    source_version: str
    effective_date: date
    verified: bool = False

    @property
    def is_empty(self):
        return len(self.off_peak_dates) == 0

    @property
    def usable(self):
        """未經核准的清單一律不可用（視同不存在）。"""
        return bool(self.verified)

    def contains(self, d):
        return d in self.off_peak_dates

    def as_dict(self):
        d = asdict(self)
        d["off_peak_dates"] = [x.isoformat() for x in self.off_peak_dates]
        d["effective_date"] = (self.effective_date.isoformat()
                               if self.effective_date else None)
        d["is_empty"] = self.is_empty
        d["usable"] = self.usable
        return d

    def __str__(self):
        return (f"[ANNUAL {self.year}] {len(self.off_peak_dates)} 日 "
                f"source={self.source}@{self.source_version} "
                f"verified={self.verified}")


def build_annual_list(year, dates, source, source_version, effective_date,
                      verified=False):
    """
    建立並驗證一份年度清單。回傳 (list_or_None, reason)。

    🔴 任何資料問題一律拒絕建立（Fail Closed），**不做任何自動修正**：
       不排除重複、不忽略錯年份、不猜測格式。
    """
    if not isinstance(year, int) or isinstance(year, bool) \
            or not (MIN_YEAR <= year <= MAX_YEAR):
        return None, AL_YEAR_INVALID
    if not isinstance(source, str) or not source.strip():
        return None, AL_SOURCE_MISSING
    if not isinstance(source_version, str) or not source_version.strip():
        return None, AL_SOURCE_MISSING
    if not isinstance(effective_date, date):
        return None, AL_EFFECTIVE_DATE_INVALID

    seen = []
    for d in (dates or ()):
        if not isinstance(d, date) or isinstance(d, bool):
            return None, AL_DATE_INVALID
        if d.year != year:
            return None, AL_DATE_WRONG_YEAR
        if d in seen:
            return None, AL_DUPLICATE_DATE
        seen.append(d)

    return AnnualOffPeakDayList(
        year=year, off_peak_dates=tuple(sorted(seen)), source=source.strip(),
        source_version=source_version.strip(), effective_date=effective_date,
        verified=bool(verified)), AL_OK


def load_annual_list_from_mapping(obj):
    """
    由已解析的 mapping 建立年度清單。回傳 (list_or_None, reason)。

    人工 provisioning 檔案格式（可 code review、可版本控管、**不需要任何網路**）：

        {
          "schema_version": 1,
          "year": 2026,
          "source": "<正式出處，例如台電電價表或客戶提供之年度行事曆>",
          "source_version": "<版本或發布日，供事後追溯>",
          "effective_date": "2026-01-01",
          "verified": false,
          "off_peak_dates": ["2026-01-01", "2026-02-28"],
          "notes": "<選填：核准者、核對方式>"
        }

    🔴 `verified` 預設應為 false，經人工核對後才改為 true。
    🔴 本函式**只解析與驗證**，不讀網路、不猜測、不自動修正。
    """
    if not isinstance(obj, dict):
        return None, AL_SCHEMA_INVALID
    if obj.get("schema_version") != SCHEMA_VERSION:
        return None, AL_SCHEMA_UNSUPPORTED
    raw = obj.get("off_peak_dates")
    if not isinstance(raw, list):
        return None, AL_SCHEMA_INVALID
    dates = []
    for s in raw:
        if not isinstance(s, str):
            return None, AL_DATE_INVALID
        try:
            dates.append(date.fromisoformat(s))
        except ValueError:
            return None, AL_DATE_INVALID
    eff = obj.get("effective_date")
    if not isinstance(eff, str):
        return None, AL_EFFECTIVE_DATE_INVALID
    try:
        eff_date = date.fromisoformat(eff)
    except ValueError:
        return None, AL_EFFECTIVE_DATE_INVALID
    return build_annual_list(obj.get("year"), dates, obj.get("source"),
                             obj.get("source_version"), eff_date,
                             verified=bool(obj.get("verified", False)))


def load_annual_list_from_file(path):
    """
    由本機 JSON 檔載入。回傳 (list_or_None, reason)。**零網路**。

    ⚠️ production 不會自動掃描目錄 —— 要納入必須明確加入 PRODUCTION_ANNUAL_LISTS，
       這一步刻意保持人工，讓年度資料的採用可被 code review。
    """
    try:
        obj = json.loads(io.open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return None, AL_FILE_UNREADABLE
    return load_annual_list_from_mapping(obj)


class AnnualOffPeakDayProvider(TC.HolidayProvider):
    """
    以年度清單回答「該日是否為離峰日」。實作既有 HolidayProvider 介面。

    🔴 只有 `verified=True` 的清單會被採用。
    🔴 年度不在已核准清單內 → 回 None（**不是** False）。
    🔴 本類別零 I/O：清單由呼叫端注入，不自行讀檔、不連網路。
    """

    def __init__(self, lists=(), invalid=None):
        usable = {}
        rejected = {}
        for lst in (lists or ()):
            if not isinstance(lst, AnnualOffPeakDayList):
                continue
            (usable if lst.usable else rejected)[lst.year] = lst
        self._lists = usable
        self._rejected = rejected
        # {year: reason} —— 有提供資料但驗證失敗的年度（同樣視同不存在）
        self._invalid = dict(invalid or {})

    @property
    def known_years(self):
        return tuple(sorted(self._lists))

    @property
    def rejected_years(self):
        """存在但未通過核准的年度（供稽核，仍視同不存在）。"""
        return tuple(sorted(self._rejected))

    @property
    def invalid_years(self):
        return tuple(sorted(self._invalid))

    def list_for(self, year):
        return self._lists.get(year)

    def readiness(self, year):
        """
        該年度的資料就緒狀態。**只回報，不影響任何控制能力。**

        🔴 只有 AVAILABLE 能讓該年度的離峰日被判定；
           MISSING / UNVERIFIED / INVALID 一律視同不存在 → is_off_peak_day 回 None。
        🔴 **不得** fallback 使用其他年度的清單。
        """
        if year in self._lists:
            return AR_AVAILABLE
        if year in self._rejected:
            return AR_UNVERIFIED
        if year in self._invalid:
            return AR_INVALID
        return AR_MISSING

    def readiness_report(self, years):
        """供啟動時檢查用的就緒摘要（純唯讀，不啟用任何控制）。"""
        return {int(y): self.readiness(int(y)) for y in years}

    def is_off_peak_day(self, d):
        """
        三態回答。**任何不確定一律回 None。**
        """
        if not isinstance(d, date) or isinstance(d, bool):
            return None
        lst = self._lists.get(d.year)
        if lst is None:
            return None                       # 年度未知／未核准 → 不得推論
        return lst.contains(d)


# ======================================================================
# Production 清單（Phase D.4-E.2 正式 provision）
# ======================================================================
# 🔴 **只收經人工驗收的年度**。來源是 official_annual_calendar 的
#    `verified_lists()` —— 那裡的 verified=True 被人工驗收憑據
#    （官方 Calendar provenance ＋ 檔案 SHA-256 ＋ 本次驗收）完全守住。
#    任何一天被改動、或來源檔案換了一份，該年度會**直接消失**在這個清單裡，
#    退回「年度未知 → UNKNOWN → Fail Closed」，而不是悄悄沿用舊資料。
#
# 🔴 這裡**不做**下列任何一件事（皆為明令禁止）：
#      · runtime 自動下載日曆表          · 任何 runtime 網路連線
#      · 自動掃描目錄把年度加進來        · 未涵蓋年度自動 fallback
#      · 把某年度資料沿用到相鄰年度      · 自行產生任何年度的日期
#      · 採用第三方 Calendar 作為後備
#    要新增年度，只能：人工下載官方檔案 → 解析 → 人工驗收 → 明確寫進程式碼。
#
# ⚠️ 這裡刻意用**延後 import**：official_annual_calendar 需要本檔的
#    build_annual_list，本檔又需要它的已驗收清單。把 import 放在函式內，
#    先 import 哪一邊都不會拿到部分初始化的模組。
def _provision_verified_lists():
    """取回所有已通過人工驗收的年度清單。零網路、零檔案掃描。"""
    import official_annual_calendar as _OC
    return _OC.verified_lists()


# 🔴 直接以 `python annual_off_peak_calendar.py` 執行時，本檔的模組名是 __main__，
#    official_annual_calendar 回頭 import 時會**再載入一份**本檔 ——
#    兩份的 AnnualOffPeakDayList 是不同的類別，isinstance 會失敗，
#    Provider 於是**靜默地**收下 0 份清單，看起來像「還沒 provision」。
#    先把自己登記進 sys.modules，確保兩邊拿到的是同一份模組。
#    ⚠️ 這個別名只在「本檔被當成腳本執行」時生效；正常 import 時 setdefault 不動作。
_sys.modules.setdefault("annual_off_peak_calendar", _sys.modules[__name__])

PRODUCTION_ANNUAL_LISTS = _provision_verified_lists()

PRODUCTION_PROVIDER = AnnualOffPeakDayProvider(PRODUCTION_ANNUAL_LISTS)


def main(argv=None):
    ap = argparse.ArgumentParser(description="年度離峰日清單（唯讀）")
    ap.add_argument("--show", action="store_true")
    ap.parse_args(argv)

    print("== 年度離峰日清單（D.4-A，完全離線）==\n")
    p = PRODUCTION_PROVIDER
    print(f"  production 已核准年度 : {list(p.known_years) or '（無）'}")
    print(f"  未核准（視同不存在）  : {list(p.rejected_years) or '（無）'}")
    for tag, d in (("已核准年度的離峰日", date(2026, 9, 28)),
                   ("已核准年度的平日  ", date(2026, 9, 29)),
                   ("未核准年度        ", date(2028, 9, 28))):
        print(f"    {tag} {d} → {p.is_off_peak_day(d)}")
    # ⚠️ 週日不在年度清單內（由既有日型規則涵蓋），因此這裡回 False 是正確的：
    #    「不是清單上的離峰日」不等於「不是離峰時段」。
    print(f"    週日（由日型規則涵蓋）2026-07-19 → "
          f"{p.is_off_peak_day(date(2026, 7, 19))}")
    print("    None = 無法判定（**絕不**等同 False）\n")

    lst, why = build_annual_list(2026, [date(2026, 1, 1)], "範例來源",
                                 "v0-demo", date(2026, 1, 1), verified=True)
    print(f"  範例（僅示範 schema）: {lst}")
    demo = AnnualOffPeakDayProvider([lst])
    print(f"    2026-01-01 → {demo.is_off_peak_day(date(2026, 1, 1))}")
    print(f"    2026-01-02 → {demo.is_off_peak_day(date(2026, 1, 2))}")
    print(f"    2027-01-01 → {demo.is_off_peak_day(date(2027, 1, 1))}（年度未知）")

    print("\n  ⚠ 本檔不含任何 production 離峰日；不連任何外部服務。")
    print("  ⚠ None 絕不等同 False —— 查不到假日不得推論成不是假日。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
