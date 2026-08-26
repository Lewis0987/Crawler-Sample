# -*- coding: utf-8 -*-
"""
taipower_offpeak_rules.py — 台電離峰日規則表（Phase D.4-C）
======================================================================
只回答一件事：**「哪些節日屬於離峰日，以及它們的日期規則是什麼」**。

    Tariff Rule（本檔）        「春節 = 農曆除夕～正月初五」
            ↓
    Annual Materializer        「2026 年的春節實際落在哪幾天」
            ↓
    人工核准
            ↓
    Production Annual List

🔴 **Rule 與 Annual Date 嚴格分離**
    本檔**只有規則，沒有任何特定年度的國曆日期**。
    規則永遠不變；年度日期每年重新產生並重新核准。

🔴 **政府行事曆 ≠ 台電離峰日曆**
    政府補假／彈性放假／補班日**不得**自動視為離峰日。
    除非台電正式文件或正式契約另有規定，一律不擴張。

🔴 **不自行增加或推導節日**
    本檔只收錄**有官方直接證據**的項目（confirmed=True）。

🔴 **來源優先序（Source Authority）**
    P1 台電官方年度時間電價日曆表 → 年度實際 day type／實際國曆日期
    P2 台電正式詳細電價表         → tariff rule／語意定義
    P3 其他台電官方公告           → 補充說明
    P4 搜尋摘要／第三方資料       → 只能 research，**不得**直接形成 Production Rule
    ⚠️ P1 與低優先來源衝突時，**一律以 P1 為準**，低優先來源不得覆蓋。
    （機制實作見 official_annual_calendar.SOURCE_AUTHORITY）

🔴 **Phase D.4-D 的三項 domain 結論已正式撤銷**
    D.4-D 僅依 P4（搜尋摘要）建立認知，已由 D.4-E 的 P1 直接證據推翻：
    ① 教師節／臺灣光復紀念日／行憲紀念日被錯誤排除 → 實為官方離峰日
    ② 春節起算日少一天                          → 實為「除夕前一日」起
    ③ 「12 個節日項目」被判為無法佐證          → 官方年度日曆還原結果支持
    這三項舊結論 **RETRACTED / SUPERSEDED BY PRIMARY OFFICIAL CALENDAR EVIDENCE**，
    不得再以任何形式作為有效的 Production 認知。

規則分類
    FIXED_GREGORIAN      固定國曆日（例：1/1）
    LUNAR_SINGLE_DAY     農曆單日（例：農曆 5/5）
    LUNAR_RANGE          農曆區間（例：除夕～正月初五）
    VARIABLE_GREGORIAN   國曆但逐年變動（例：4/4 或 4/5）

用法（完全離線）
    python taipower_offpeak_rules.py --show
"""

import argparse
from dataclasses import dataclass, asdict

# ======================================================================
# 規則分類
# ======================================================================
RULE_FIXED_GREGORIAN = "FIXED_GREGORIAN"
RULE_LUNAR_SINGLE_DAY = "LUNAR_SINGLE_DAY"
RULE_LUNAR_RANGE = "LUNAR_RANGE"
RULE_VARIABLE_GREGORIAN = "VARIABLE_GREGORIAN"
# 每週固定重複（週日）—— 已由既有日型規則涵蓋，不需要年度資料
RULE_WEEKLY_RECURRING = "WEEKLY_RECURRING"

RULE_KINDS = frozenset({RULE_FIXED_GREGORIAN, RULE_LUNAR_SINGLE_DAY,
                        RULE_LUNAR_RANGE, RULE_VARIABLE_GREGORIAN,
                        RULE_WEEKLY_RECURRING})

# 已由既有日型規則涵蓋、**不需**年度資料、也**不進入**年度清單的分類
COVERED_BY_DAYTYPE_RULE = frozenset({RULE_WEEKLY_RECURRING})

# 需要人工提供年度實際日期的分類（無法由規則自動算出國曆日）
NEEDS_ANNUAL_INPUT = frozenset({RULE_LUNAR_SINGLE_DAY, RULE_LUNAR_RANGE,
                                RULE_VARIABLE_GREGORIAN})

# 規則來源（供事後追溯）
RULE_SOURCE = "台灣電力公司 電價表（詳細電價表）"

# ---- 版本與生效資訊（Phase D.4-D 查證）----
RULE_APPROVED_DATE = "民國 114 年 9 月 26 日（經濟部核定單價）"
RULE_EFFECTIVE_FROM = "民國 114 年 10 月 1 日"
RULE_EFFECTIVE_TO = None                 # 目前無終止日；未發現後續改版
RULE_FILING_REF = "民國 114 年 11 月 17 日 經濟部經授能字第 11400334600 號函同意備查"
RULE_VERSION = "1140926-核定／1141001-生效"

RULE_SOURCE_NOTE = (
    "Phase D.4-E 已取得並完整解析**台電官方年度時間電價日曆表**"
    "（115／116 兩年度，另有獨立第二份官方檔案互證），"
    "並以實際年度日型逐項對帳本規則表。"
    "本表的節日項目因此有 P1 直接證據，不再僅依搜尋摘要。"
    "⚠️ 仍需說明：取得的是**年度日曆表**，"
    "詳細電價表（P2）的逐字條文未另行擷取；"
    "因此「語意描述」依 P2、「實際日期」依 P1。")

RULE_CALENDAR_CROSSCHECK_NOTE = (
    "本規則表已與台電官方 115（西元 2026）及 116（西元 2027）年度"
    "時間電價日曆表交叉對帳。"
    "⚠️ 已知限制：節日剛好落在週日時，日曆表的紅色來自「週日」本身，"
    "**無法單獨證明該節日規則**（例：2026-10-25 為週日）——"
    "這種年度以另一個年度的非週日證據補齊，不以推測補齊。")


@dataclass(frozen=True)
class OffPeakRule:
    """
    單一離峰日的規則。**不含任何特定年度的國曆日期。**

    spec 依 kind 而異：
        FIXED_GREGORIAN    {"month": 1, "day": 1}
        LUNAR_SINGLE_DAY   {"lunar_month": 5, "lunar_day": 5}
        LUNAR_RANGE        {"from": "農曆除夕", "to": "農曆正月初五"}
        VARIABLE_GREGORIAN {"candidates": [(4, 4), (4, 5)]}
    """
    key: str
    name: str
    kind: str
    spec: dict
    confirmed: bool = False
    note: str = ""

    @property
    def needs_annual_input(self):
        return self.kind in NEEDS_ANNUAL_INPUT

    def as_dict(self):
        return asdict(self)

    def __str__(self):
        mark = "官方確認" if self.confirmed else "**未確認**"
        return f"[{self.kind:<19}] {self.name:<10} {mark}  {self.spec}"


# ======================================================================
# 官方規則（使用者已確認出自台電官方文件）
# ======================================================================
OFFICIAL_RULES = (
    OffPeakRule("sunday", "週日", RULE_WEEKLY_RECURRING, {"weekday": 6},
                confirmed=True,
                note="官方離峰日定義的第一項即為週日；"
                     "**已由既有日型規則涵蓋**，不需要年度資料、不進入年度清單"),
    OffPeakRule("founding_day", "中華民國開國紀念日", RULE_FIXED_GREGORIAN,
                {"month": 1, "day": 1}, confirmed=True),
    OffPeakRule("lunar_new_year", "春節", RULE_LUNAR_RANGE,
                {"from": "農曆除夕前一日", "to": "農曆正月初五"}, confirmed=True,
                note="台電正式定義為區間，**本身即涵蓋多個國曆日**。"
                     "起算日為**除夕前一日**（非除夕）—— 此為 Phase D.4-E 以"
                     "台電官方年度時間電價日曆表與農曆日期**交叉對帳確認**，"
                     "不是推測：116 年日曆表的連續離峰區間為 2/4~2/10，"
                     "而該年除夕為 2/5、正月初五為 2/10，起點恰早除夕一日。"
                     "115 年日曆表結果一致，但該年除夕前一日恰為週日，"
                     "紅色來自週日本身，**無法單獨佐證起算日**（已知限制）"),
    OffPeakRule("peace_memorial_day", "和平紀念日", RULE_FIXED_GREGORIAN,
                {"month": 2, "day": 28}, confirmed=True),
    OffPeakRule("childrens_day", "兒童節", RULE_FIXED_GREGORIAN,
                {"month": 4, "day": 4}, confirmed=True),
    OffPeakRule("tomb_sweeping_day", "民族掃墓節", RULE_VARIABLE_GREGORIAN,
                {"candidates": [(4, 4), (4, 5)]}, confirmed=True,
                note="官方記載為「4 月 4 日或 4 月 5 日」；"
                     "**逐年由哪一天生效需人工提供**，不得自行固定"),
    OffPeakRule("labor_day", "勞動節", RULE_FIXED_GREGORIAN,
                {"month": 5, "day": 1}, confirmed=True),
    OffPeakRule("dragon_boat_festival", "端午節", RULE_LUNAR_SINGLE_DAY,
                {"lunar_month": 5, "lunar_day": 5}, confirmed=True),
    OffPeakRule("mid_autumn_festival", "中秋節", RULE_LUNAR_SINGLE_DAY,
                {"lunar_month": 8, "lunar_day": 15}, confirmed=True),
    OffPeakRule("teachers_day", "教師節", RULE_FIXED_GREGORIAN,
                {"month": 9, "day": 28}, confirmed=True,
                note="Phase D.4-E 由官方年度日曆表直接證實：2026-09-28（週一）與"
                     "2027-09-28（週二）皆標示為離峰日，**兩年皆為非週日**，"
                     "不受週日遮蔽。D.4-D 的排除結論已撤銷"),
    OffPeakRule("national_day", "國慶日", RULE_FIXED_GREGORIAN,
                {"month": 10, "day": 10}, confirmed=True),
    OffPeakRule("retrocession_day", "臺灣光復紀念日", RULE_FIXED_GREGORIAN,
                {"month": 10, "day": 25}, confirmed=True,
                note="Phase D.4-E 由官方年度日曆表直接證實：2027-10-25（週一）"
                     "標示為離峰日，已排除週日干擾。"
                     "（2026-10-25 恰為週日，該年無法單獨佐證）"
                     "D.4-D 的排除結論已撤銷"),
    OffPeakRule("constitution_day", "行憲紀念日", RULE_FIXED_GREGORIAN,
                {"month": 12, "day": 25}, confirmed=True,
                note="Phase D.4-E 由官方年度日曆表直接證實：2026-12-25（週五）"
                     "標示為離峰日，已排除週日干擾。D.4-D 的排除結論已撤銷"),
)

# ======================================================================
# 官方離峰日清單中**不存在**的項目
# ======================================================================
# 🔴 Phase D.4-E 後**已無任何項目**。
#    D.4-D 曾把教師節／臺灣光復紀念日／行憲紀念日列於此處，
#    依據只有 P4（搜尋摘要）；官方年度時間電價日曆表（P1）已直接證實
#    這三項確為離峰日，該排除結論正式撤銷，三項均已移入 OFFICIAL_RULES。
#    ⚠️ 這個空 tuple 是**刻意保留**的：日後若真的出現「規則表有、但官方
#       日曆表沒有」的項目，必須放在這裡並附上 P1 證據，不得默默刪掉。
EXCLUDED_BY_OFFICIAL_SOURCE = ()

# 向後相容的舊名稱
UNCONFIRMED_RULES = EXCLUDED_BY_OFFICIAL_SOURCE

# 撤銷紀錄（供事後追溯：曾經被錯誤排除的項目與撤銷依據）
RETRACTED_EXCLUSIONS = (
    ("teachers_day", "D.4-D 依 P4 搜尋摘要排除；"
                     "D.4-E 由 P1 官方年度日曆表證實 2026-09-28、2027-09-28 皆為離峰日"),
    ("retrocession_day", "D.4-D 依 P4 搜尋摘要排除；"
                         "D.4-E 由 P1 官方年度日曆表證實 2027-10-25（週一）為離峰日"),
    ("constitution_day", "D.4-D 依 P4 搜尋摘要排除；"
                         "D.4-E 由 P1 官方年度日曆表證實 2026-12-25（週五）為離峰日"),
)

ALL_RULES = OFFICIAL_RULES + EXCLUDED_BY_OFFICIAL_SOURCE

# ======================================================================
# 政府假日調整 —— 官方明確排除
# ======================================================================
# 🔴 Phase D.4-D 查證：官方明確指出
#    「應以電價表表列之離峰日為主，**彈性放假日、補假日、颱風假等**
#      離峰日則**不適用**離峰日電價」。
#    因此政府行事曆與台電離峰日曆**確定不等價**，不得擴張。
HOLIDAY_ITEM_COUNT_NOTE = (
    "官方年度日曆表還原結果支持「12 個節日項目 ＋ 每週日」的說法："
    "開國紀念日、春節、和平紀念日、兒童節、民族掃墓節、勞動節、端午節、"
    "中秋節、教師節、國慶日、臺灣光復紀念日、行憲紀念日。"
    "🔴 這個數字只是 documentation／validation information，"
    "**不得**成為 runtime business logic：程式中不存在任何數量常數，"
    "項目數一律由清單長度決定。"
    "⚠️ 「節日項目數」≠「年度日曆日數」—— 春節本身即為跨多日區間，"
    "且節日落在週日時不另計入年度清單。")

GOVERNMENT_ADJUSTMENTS_EXCLUDED = ("彈性放假日", "補假日", "颱風假")
GOVERNMENT_ADJUSTMENT_NOTE = (
    "官方明確排除：僅電價表表列之離峰日適用離峰電價；"
    "彈性放假日、補假日、颱風假**不適用**。")


def confirmed_rules(rules=None):
    """只回傳已由官方文件確認的規則 —— production 產出的唯一來源。"""
    return tuple(r for r in (rules if rules is not None else ALL_RULES)
                 if r.confirmed)


def rules_needing_annual_input(rules=None):
    """需要人工提供年度實際日期的規則（農曆與逐年變動）。"""
    return tuple(r for r in confirmed_rules(rules) if r.needs_annual_input)


def rules_requiring_annual_dates(rules=None):
    """
    需要進入年度清單的規則（排除已由日型規則涵蓋者，例如週日）。
    """
    return tuple(r for r in confirmed_rules(rules)
                 if r.kind not in COVERED_BY_DAYTYPE_RULE)


def main(argv=None):
    ap = argparse.ArgumentParser(description="台電離峰日規則表（唯讀）")
    ap.add_argument("--show", action="store_true")
    ap.parse_args(argv)

    print("== 台電離峰日規則表（D.4-E.1，完全離線）==\n")
    print(f"  來源：{RULE_SOURCE}")
    print(f"  註記：{RULE_SOURCE_NOTE}\n")
    print(f"  交叉對帳：{RULE_CALENDAR_CROSSCHECK_NOTE}\n")
    print(f"  已由官方確認（{len(OFFICIAL_RULES)} 項）：")
    for r in OFFICIAL_RULES:
        print(f"    {r}")
    print(f"\n  官方清單中不存在的項目（{len(EXCLUDED_BY_OFFICIAL_SOURCE)} 項）")
    for r in EXCLUDED_BY_OFFICIAL_SOURCE:
        print(f"    {r}")
    print(f"\n  已撤銷的舊排除（{len(RETRACTED_EXCLUSIONS)} 項）：")
    for k, why in RETRACTED_EXCLUSIONS:
        print(f"    {k:<20} {why}")
    need = rules_needing_annual_input()
    print(f"\n  需人工提供年度日期（{len(need)} 項）：")
    for r in need:
        print(f"    {r.name}（{r.kind}）")
    print(f"\n  節日項目數說明：{HOLIDAY_ITEM_COUNT_NOTE}")
    print("\n  ⚠ 本檔不含任何特定年度的國曆日期。")
    print("  ⚠ 政府補假／彈性放假／補班日**不得**自動視為離峰日。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
