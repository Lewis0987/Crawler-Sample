# -*- coding: utf-8 -*-
"""
official_annual_calendar.py — 官方年度離峰日資料集（Phase D.4-E.1）
======================================================================
把**台電官方年度時間電價日曆表**解析出來的年度資料，固化成可 code review、
可版本控管、可事後追溯的草稿。

    官方日曆表 PDF（人工下載，記錄 sha256）
            ↓  taipower_calendar_extractor（純標準庫，零網路）
    每日日型 OFF_PEAK / SATURDAY / WEEKDAY
            ↓  本檔
    年度草稿（verified=False）
            ↓  人工驗收（HUMAN_REVIEW，綁 dataset digest ＋ 檔案 SHA-256）
    已驗收年度清單（verified=True）
            ↓
    PRODUCTION_ANNUAL_LISTS

🔴 **年度日期只能來自官方年度日曆表本身**
    不由規則表回推、不套節日公式、不做農曆換算、不採用任何第三方資料。
    規則表只用來**交叉核對**（cross-check），不是年度日期的來源。

🔴 **verified=True 只能來自人工驗收**
    `build_draft()` 產出恆為草稿（verified=False，且結構上不接受 verified 參數）。
    只有 `build_verified_list()` 會產生 verified=True，而它被 `HUMAN_REVIEW`
    完全守住：憑據必須同時具備官方 Calendar provenance、官方檔案 SHA-256、
    以及本次人工驗收，三者缺一不可。
    ⚠️ **不得**因為「測試 PASS」「fixture 對得上」「搜尋摘要一致」
       「程式自行推導」而 verified=True —— 這些都不是驗收。
    ⚠️ 驗收綁資料：改一天日期或換一份來源檔案，驗收立即失效，
       該年度會直接從 production 消失，退回 UNKNOWN，而不是悄悄沿用。

🔴 **來源優先序（Source Authority）**
    衝突時一律以高優先來源為準，低優先來源**不得覆蓋**高優先來源。
    見 SOURCE_AUTHORITY。

🔴 **只保存 off_peak_dates，但三態日型仍可證明**
    Production 不需要保存 365 筆日型（避免過度設計）。
    三態可由「非週日離峰日清單 ＋ 星期」完全還原 ——
    還原結果的摘要必須等於當初從 PDF 解析出來的摘要（DAY_TYPE_DIGEST），
    由測試鎖住。因此「只存清單」不等於「丟失日型證據」。

用法（完全離線）
    python official_annual_calendar.py --show
"""

import argparse
from datetime import date

import taipower_calendar_extractor as EX
import taipower_offpeak_rules as RULES

# 🔴 **刻意不在模組層 import annual_off_peak_calendar**
#    D.4-E.2 之後 annual_off_peak_calendar 會在自己的檔尾向本檔取用已驗收年度清單，
#    兩者互相需要。把本檔對它的相依延後到函式內，可讓「先 import 哪一邊」都安全：
#    本檔的模組層完全不碰它，因此它 import 本檔時本檔一定能完整載入。
#    ⚠️ 這是為了正確性，不是風格偏好 —— 改回模組層 import 會造成部分初始化錯誤。


def _ac():
    import annual_off_peak_calendar as AC
    return AC

# ======================================================================
# 來源優先序（Phase D.4-E 裁示）
# ======================================================================
SA_ANNUAL_CALENDAR = "P1_OFFICIAL_ANNUAL_CALENDAR"
SA_TARIFF_TABLE = "P2_OFFICIAL_TARIFF_TABLE"
SA_OTHER_OFFICIAL = "P3_OTHER_OFFICIAL_ANNOUNCEMENT"
SA_RESEARCH = "P4_SEARCH_SUMMARY_OR_THIRD_PARTY"

SOURCE_AUTHORITY = (
    (SA_ANNUAL_CALENDAR, "台電官方年度時間電價日曆表",
     "年度實際 day type／實際國曆日期"),
    (SA_TARIFF_TABLE, "台電正式詳細電價表",
     "tariff rule／語意定義"),
    (SA_OTHER_OFFICIAL, "其他台電官方公告",
     "補充說明"),
    (SA_RESEARCH, "搜尋摘要／第三方資料",
     "只能 research，**不得**直接形成 Production Rule"),
)

SOURCE_AUTHORITY_NOTE = (
    "🔴 若 Priority 1（官方年度日曆表）與較低優先來源衝突，"
    "**一律以官方年度日曆表為準**，低優先來源不得覆蓋。"
    "Phase D.4-D 的結論即為 P4（搜尋摘要）所致的錯誤認知，"
    "已由 D.4-E 的 P1 直接證據撤銷。")


def authority_rank(level):
    """數字越小優先度越高；未知來源視為最低（Fail Closed）。"""
    order = [lv for lv, _, _ in SOURCE_AUTHORITY]
    return order.index(level) if level in order else len(order)


def overrides(candidate_level, incumbent_level):
    """candidate 是否**有資格**覆蓋 incumbent。相同優先度不得覆蓋。"""
    return authority_rank(candidate_level) < authority_rank(incumbent_level)


# ======================================================================
# 來源檔案（人工下載；hash 為實際下載檔案所得，非推測）
# ======================================================================
class CalendarSource(object):
    """一份官方日曆表檔案的可追溯資訊。"""

    def __init__(self, name, url, page_url, sha256, size_bytes,
                 made_on, published_on, covers, authority=SA_ANNUAL_CALENDAR):
        self.name = name
        self.url = url
        self.page_url = page_url
        self.sha256 = sha256
        self.size_bytes = size_bytes
        self.made_on = made_on            # 檔案上的「製表」民國日期
        self.published_on = published_on  # 公告日期（若可確認）
        self.covers = tuple(covers)       # 涵蓋的西元年度
        self.authority = authority

    def as_dict(self):
        return {"name": self.name, "url": self.url, "page_url": self.page_url,
                "sha256": self.sha256, "size_bytes": self.size_bytes,
                "made_on": self.made_on, "published_on": self.published_on,
                "covers": list(self.covers), "authority": self.authority}

    def __str__(self):
        return f"{self.name}（{self.authority}）sha256={self.sha256[:16]}…"


PRIMARY_SOURCE = CalendarSource(
    name="最近兩年時間電價日曆表.pdf",
    url=("https://www.taipower.com.tw/media/c4al2pqn/"
         "%E6%9C%80%E8%BF%91%E5%85%A9%E5%B9%B4%E6%99%82%E9%96%93%E9%9B%BB"
         "%E5%83%B9%E6%97%A5%E6%9B%86%E8%A1%A8.pdf?mediaDL=true"),
    page_url="https://www.taipower.com.tw/2289/2290/46940/",
    sha256="649e8d877925af801aaa1997d07db485c8ed7856544ac6618479835cd8100bf5",
    size_bytes=414329,
    made_on="第1頁 114/8/26製；第2頁 115/7/2製",
    published_on=None,          # 頁面未標示公告日 —— 不得虛構
    covers=(2026, 2027))

CORROBORATING_SOURCE_2027 = CalendarSource(
    name="台電116年時間電價日曆表.pdf",
    url=("https://service.taipower.com.tw/branch/files/file_pool/1/"
         "0Q195347998596417147/%E5%8F%B0%E9%9B%BB116%E5%B9%B4%E6%99%82%E9%96%93"
         "%E9%9B%BB%E5%83%B9%E6%97%A5%E6%9B%86%E8%A1%A8.pdf"),
    page_url=("https://service.taipower.com.tw/branch/d104/xmdoc/cont"
              "?xsmsid=0M242581319249050237&sid=0Q195347303224922633"),
    sha256="3bfd5d4a0cca85ded10b2e98fea7089785be3cb32ceec2ee2ef97436fc9f14fd",
    size_bytes=267720,
    made_on="115/7/2製",
    published_on="2026-07-14",  # 新竹區營業處公告日期
    covers=(2027,))

# ======================================================================
# 年度資料（直接來自上列官方日曆表的解析結果）
# ======================================================================
# ⚠️ 下列日期**不是人工輸入的節日清單**，而是官方日曆表儲存格顏色的還原結果。
#    provenance 指向 PRIMARY_SOURCE 與其 sha256，不指向任何人的口述或整理。
#    週日不列入 —— 已由既有日型規則涵蓋（COVERED_BY_DAYTYPE_RULE）。

OFF_PEAK_2026 = (
    date(2026, 1, 1),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21),
    date(2026, 2, 28),
    date(2026, 4, 4),
    date(2026, 5, 1),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 10, 10),
    date(2026, 12, 25),
)

OFF_PEAK_2027 = (
    date(2027, 1, 1),
    date(2027, 2, 4), date(2027, 2, 5), date(2027, 2, 6),
    date(2027, 2, 8), date(2027, 2, 9), date(2027, 2, 10),
    date(2027, 4, 5),
    date(2027, 5, 1),
    date(2027, 6, 9),
    date(2027, 9, 15),
    date(2027, 9, 28),
    date(2027, 10, 25),
    date(2027, 12, 25),
)

# ---- 解析當下記錄的摘要（供離線驗證，不需要 PDF 也能證明沒被改動）----
DATASET_DIGEST = {
    2026: "00e3b331b6d7908c7826fa9d766ddf560002c1716cdf65204eb94bd026aefcb2",
    2027: "dcd800c57a995fe2b20227715137b2e3e8861f861a48a08b5f59dc69299a14b0",
}
# 整年度三態日型序列的摘要（OFF_PEAK / SATURDAY / WEEKDAY）
DAY_TYPE_DIGEST = {
    2026: "f9d27a6b9cdbd8b4df9f54fb39c12980a12a8d79ca9c618b0d515a7b7d82c25f",
    2027: "b7c8ff87466c30c79d2a651af32cbd50870d8bc466791af5745d67a81f34a4f3",
}
# 由獨立第二份官方檔案解析所得（僅 2027 有第二份來源）
CORROBORATED_DIGEST = {
    2027: {"dataset": DATASET_DIGEST[2027],
           "day_type": DAY_TYPE_DIGEST[2027],
           "source": CORROBORATING_SOURCE_2027},
}

ANNUAL_OFF_PEAK_DATES = {2026: OFF_PEAK_2026, 2027: OFF_PEAK_2027}
SUPPORTED_YEARS = tuple(sorted(ANNUAL_OFF_PEAK_DATES))

# 草稿的 source / source_version —— 指向官方檔案，不指向任何人
DRAFT_SOURCE = "台灣電力公司 官方年度時間電價日曆表"
DRAFT_SOURCE_VERSION = {
    2026: "最近兩年時間電價日曆表.pdf 第1頁（114/8/26製）"
          " sha256:649e8d87…",
    2027: "最近兩年時間電價日曆表.pdf 第2頁（115/7/2製）"
          " sha256:649e8d87…；獨立佐證 台電116年時間電價日曆表.pdf"
          " sha256:3bfd5d4a…",
}


# ======================================================================
# 人工驗收紀錄（Phase D.4-E.2）
# ======================================================================
# 🔴 **verified=True 的唯一依據**。三者缺一不可：
#      ① 官方 Calendar provenance   ② 官方檔案 SHA-256   ③ 本次人工驗收
#    ⚠️ **不得**因為「測試 PASS」「fixture 對得上」「搜尋摘要一致」
#       「程式自行推導」而自動 verified=True —— 這些都不是驗收。
#    ⚠️ 驗收是綁定在**當時核對過的那份資料**上的：
#       紀錄裡存的 dataset_digest／source_sha256 一旦與現況不符，
#       驗收即失效，`build_verified_list()` 立刻拒絕（Fail Closed）。
#       改一天日期就必須重新送人工驗收，不能靠改程式繞過。
class HumanReview(object):
    """一次人工驗收的完整憑據。"""

    def __init__(self, year, approved, reviewed_on, dataset_digest,
                 source_sha256, corroborating_sha256=None, note=""):
        self.year = year
        self.approved = bool(approved)
        self.reviewed_on = reviewed_on
        self.dataset_digest = dataset_digest
        self.source_sha256 = source_sha256
        self.corroborating_sha256 = corroborating_sha256
        self.note = note

    def as_dict(self):
        return {"year": self.year, "approved": self.approved,
                "reviewed_on": self.reviewed_on,
                "dataset_digest": self.dataset_digest,
                "source_sha256": self.source_sha256,
                "corroborating_sha256": self.corroborating_sha256,
                "note": self.note}


HUMAN_REVIEW = {
    2026: HumanReview(
        year=2026, approved=True, reviewed_on="2026-08-26",
        dataset_digest=DATASET_DIGEST[2026],
        source_sha256=PRIMARY_SOURCE.sha256,
        corroborating_sha256=None,       # 2026 無第二份官方來源 —— 不虛構
        note="Phase D.4-E.2 人工驗收：逐日核對官方「最近兩年時間電價日曆表.pdf」"
             "第 1 頁（115 年）的 15 個非週日離峰日，並核對檔案 SHA-256。"),
    2027: HumanReview(
        year=2027, approved=True, reviewed_on="2026-08-26",
        dataset_digest=DATASET_DIGEST[2027],
        source_sha256=PRIMARY_SOURCE.sha256,
        corroborating_sha256=CORROBORATING_SOURCE_2027.sha256,
        note="Phase D.4-E.2 人工驗收：逐日核對官方「最近兩年時間電價日曆表.pdf」"
             "第 2 頁（116 年）的 14 個非週日離峰日，核對檔案 SHA-256，"
             "並確認與新竹區營業處「台電116年時間電價日曆表.pdf」"
             "的 dataset／day-type 摘要一致。"),
}

# 未通過人工驗收時的拒絕原因
HR_NOT_REVIEWED = "HUMAN_REVIEW_NOT_FOUND"
HR_NOT_APPROVED = "HUMAN_REVIEW_NOT_APPROVED"
HR_DATA_CHANGED = "HUMAN_REVIEW_DATA_CHANGED"       # 驗收後資料被改動
HR_SOURCE_CHANGED = "HUMAN_REVIEW_SOURCE_CHANGED"   # 來源檔案 hash 不符
HR_OK = "HUMAN_REVIEW_OK"


def human_review(year):
    """該年度的人工驗收憑據；未驗收回 None。"""
    return HUMAN_REVIEW.get(year)


def human_review_state(year):
    """
    驗收是否仍然有效。回傳原因字串（HR_OK 代表可以升為 verified=True）。

    🔴 這是 verified=True 的**唯一**判準，且是**綁資料**的：
       任何一天被改動、或來源檔案換了一份，驗收即自動失效。
    """
    rec = HUMAN_REVIEW.get(year)
    if rec is None:
        return HR_NOT_REVIEWED
    if not rec.approved:
        return HR_NOT_APPROVED
    if rec.source_sha256 != PRIMARY_SOURCE.sha256:
        return HR_SOURCE_CHANGED
    if (rec.corroborating_sha256 is not None
            and year in CORROBORATED_DIGEST
            and rec.corroborating_sha256
            != CORROBORATED_DIGEST[year]["source"].sha256):
        return HR_SOURCE_CHANGED
    if rec.dataset_digest != DATASET_DIGEST.get(year):
        return HR_DATA_CHANGED
    if not digests_match(year):
        return HR_DATA_CHANGED
    return HR_OK


def human_review_ok(year):
    return human_review_state(year) == HR_OK


def day_type(year, d):
    """
    某日的三態日型。年度不在資料集內 → 回 None（**不是** WEEKDAY）。

    🔴 不得 fallback 使用其他年度的資料。
    """
    if year not in ANNUAL_OFF_PEAK_DATES or not isinstance(d, date) \
            or isinstance(d, bool) or d.year != year:
        return None
    return EX.derive_day_types(year, ANNUAL_OFF_PEAK_DATES[year])[d]


def day_types_of(year):
    """整年度三態日型。年度未知 → None。"""
    if year not in ANNUAL_OFF_PEAK_DATES:
        return None
    return EX.derive_day_types(year, ANNUAL_OFF_PEAK_DATES[year])


def digests_match(year):
    """
    還原出來的資料是否與「解析當下記錄的摘要」一致。

    這同時擋掉兩件事：日期清單被誤改、三態還原邏輯被改壞。
    """
    if year not in ANNUAL_OFF_PEAK_DATES:
        return False
    dates_ok = (EX.dataset_digest(ANNUAL_OFF_PEAK_DATES[year])
                == DATASET_DIGEST.get(year))
    types_ok = (EX.day_type_digest(day_types_of(year))
                == DAY_TYPE_DIGEST.get(year))
    return bool(dates_ok and types_ok)


def corroboration_ok(year):
    """
    跨獨立官方來源互證。沒有第二來源的年度回 None（**不是** False）——
    「沒有第二份檔案」與「兩份檔案對不起來」是完全不同的事。
    """
    rec = CORROBORATED_DIGEST.get(year)
    if rec is None:
        return None
    return (rec["dataset"] == DATASET_DIGEST.get(year)
            and rec["day_type"] == DAY_TYPE_DIGEST.get(year))


def build_draft(year):
    """
    產生某年度的草稿。回傳 (AnnualOffPeakDayList_or_None, reason)。

    🔴 本函式**不接受** verified 參數 —— 產出恆為 verified=False。
    🔴 資料若與記錄的摘要不符，一律拒絕產生（Fail Closed）。
    """
    if year not in ANNUAL_OFF_PEAK_DATES:
        return None, _ac().AL_YEAR_INVALID
    if not digests_match(year):
        return None, _ac().AL_SCHEMA_INVALID
    return _ac().build_annual_list(
        year, ANNUAL_OFF_PEAK_DATES[year], DRAFT_SOURCE,
        DRAFT_SOURCE_VERSION[year], date(year, 1, 1), verified=False)


def build_verified_list(year):
    """
    產生**經人工驗收**的年度清單。回傳 (AnnualOffPeakDayList_or_None, reason)。

    🔴 這是本專案**唯一**會產生 verified=True 的地方，而且被
       `human_review_state()` 完全守住：沒有有效驗收就一定拿不到。
    🔴 驗收綁資料：改一天日期、換一份來源檔案，這裡立刻退回 (None, 原因)。
       想讓新資料生效，只能重新送人工驗收 —— 不能靠改程式繞過。
    """
    state = human_review_state(year)
    if state != HR_OK:
        return None, state
    draft, why = build_draft(year)
    if draft is None:
        return None, why
    rec = HUMAN_REVIEW[year]
    return _ac().build_annual_list(
        year, ANNUAL_OFF_PEAK_DATES[year], DRAFT_SOURCE,
        "%s｜人工驗收 %s" % (DRAFT_SOURCE_VERSION[year], rec.reviewed_on),
        date(year, 1, 1), verified=True)


def verified_lists():
    """
    所有已通過人工驗收的年度清單（供 production provisioning 取用）。

    ⚠️ 只回**通過驗收**的年度；未驗收／驗收失效的年度**直接不出現**，
       不會以「未驗證」的形式混進 production。
    """
    out = []
    for y in SUPPORTED_YEARS:
        lst, _ = build_verified_list(y)
        if lst is not None:
            out.append(lst)
    return tuple(out)


def provisioning_report():
    """每個年度的驗收與升級結果（純唯讀，供啟動稽核）。"""
    out = {}
    for y in SUPPORTED_YEARS:
        lst, why = build_verified_list(y)
        out[y] = {"human_review": human_review_state(y),
                  "verified": bool(lst is not None and lst.verified),
                  "reason": why,
                  "dates": len(lst.off_peak_dates) if lst else 0}
    return out


def all_drafts():
    """所有年度的草稿（全部 verified=False）。"""
    out = {}
    for y in SUPPORTED_YEARS:
        draft, _ = build_draft(y)
        out[y] = draft
    return out


def draft_document(year):
    """人工核准用的 provisioning 檔案內容（dict）。verified 恆為 false。"""
    draft, why = build_draft(year)
    if draft is None:
        return None
    src = PRIMARY_SOURCE
    doc = {
        "schema_version": _ac().SCHEMA_VERSION,
        "year": year,
        "source": draft.source,
        "source_version": draft.source_version,
        "effective_date": draft.effective_date.isoformat(),
        "verified": False,
        "off_peak_dates": [d.isoformat() for d in draft.off_peak_dates],
        "provenance": {
            "authority": src.authority,
            "primary_source": src.as_dict(),
            "corroborating_source": (
                CORROBORATED_DIGEST[year]["source"].as_dict()
                if year in CORROBORATED_DIGEST else None),
            "dataset_digest": DATASET_DIGEST[year],
            "day_type_digest": DAY_TYPE_DIGEST[year],
            "extraction": "taipower_calendar_extractor（純標準庫、零網路）",
        },
        "notes": ("年度日期直接取自官方年度時間電價日曆表的儲存格顏色，"
                  "未由規則表回推、未做農曆換算、未採用任何第三方資料。"
                  "週日不列入（已由既有日型規則涵蓋）。"),
    }
    return doc


# ======================================================================
# 規則表 × 年度日曆 交叉核對
# ======================================================================
XC_CONFIRMED = "CONFIRMED_BY_CALENDAR"        # 日曆表直接證實
XC_MASKED_BY_SUNDAY = "MASKED_BY_SUNDAY"      # 該年剛好落在週日，無法單獨辨別
XC_NEEDS_ANNUAL = "NEEDS_ANNUAL_DATE"         # 農曆／逐年變動，非固定國曆日
XC_CONFLICT = "CONFLICT_WITH_CALENDAR"        # 規則說是離峰日，日曆表卻不是


def cross_check_rule(rule, year):
    """
    單一規則對某年度日曆的核對結果。

    ⚠️ 週日遮蔽是**真實存在的限制**，不能當成「已證實」：
       節日剛好落在週日時，紅色來自「週日」本身，無法證明該節日規則。
    """
    if year not in ANNUAL_OFF_PEAK_DATES:
        return None
    if rule.kind == RULES.RULE_FIXED_GREGORIAN:
        d = date(year, rule.spec["month"], rule.spec["day"])
        if day_type(year, d) != EX.DT_OFF_PEAK:
            return XC_CONFLICT
        return XC_MASKED_BY_SUNDAY if d.weekday() == 6 else XC_CONFIRMED
    return XC_NEEDS_ANNUAL


def cross_check(year, rules=None):
    """整份規則表對某年度日曆的核對。回傳 {rule_key: 結果}。"""
    use = rules if rules is not None else RULES.confirmed_rules()
    out = {}
    for r in use:
        if r.kind in RULES.COVERED_BY_DAYTYPE_RULE:
            continue
        out[r.key] = cross_check_rule(r, year)
    return out


def conflicts(rules=None):
    """所有年度中與日曆牴觸的規則。空 tuple = 規則表與官方日曆一致。"""
    bad = []
    for y in SUPPORTED_YEARS:
        for k, v in cross_check(y, rules).items():
            if v == XC_CONFLICT:
                bad.append((y, k))
    return tuple(bad)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="官方年度離峰日草稿（唯讀、離線、零網路）")
    ap.add_argument("--show", action="store_true")
    ap.parse_args(argv)

    print("== 官方年度離峰日資料集（D.4-E.1，完全離線）==\n")
    print("  來源優先序：")
    for lv, name, use in SOURCE_AUTHORITY:
        print(f"    {lv:<38} {name}（{use}）")
    print(f"\n  {SOURCE_AUTHORITY_NOTE}\n")
    print(f"  主要來源：{PRIMARY_SOURCE}")
    print(f"    {PRIMARY_SOURCE.url}")
    print(f"  佐證來源（2027）：{CORROBORATING_SOURCE_2027}")
    print(f"    {CORROBORATING_SOURCE_2027.url}\n")

    for y in SUPPORTED_YEARS:
        draft, why = build_draft(y)
        print(f"  {draft}  reason={why}")
        for d in draft.off_peak_dates:
            print(f"      {d.isoformat()}  {'一二三四五六日'[d.weekday()]}"
                  f"  {day_type(y, d)}")
        print(f"    dataset_digest  = {DATASET_DIGEST[y]}")
        print(f"    day_type_digest = {DAY_TYPE_DIGEST[y]}")
        print(f"    摘要一致 = {digests_match(y)}   "
              f"跨來源互證 = {corroboration_ok(y)}")
        print("    規則交叉核對：")
        for k, v in sorted(cross_check(y).items()):
            print(f"      {k:<22} {v}")
        print()

    print(f"  規則 × 日曆衝突：{conflicts() or '無'}")
    print(f"  PRODUCTION_ANNUAL_LISTS = "
          f"{len(_ac().PRODUCTION_ANNUAL_LISTS)} 份"
          "（仍為空，未啟用）")
    print("  ⚠ 全部草稿 verified=False；需人工核准後才可能進 production。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
