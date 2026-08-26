# -*- coding: utf-8 -*-
"""
test_phase6_d4d_rule_reconciliation.py — Phase D.4-D 官方規則對帳（1~28）
======================================================================
核心命題
    「Production 規則只收錄**官方清單中確實存在**的項目；
      repo 測試 fixture 與官方清單不一致時，**修正的是認知，不是規則**。」

D.4-D 的查證結論（**部分已於 D.4-E 撤銷**）
    · 彈性放假日／補假日／颱風假 **不適用**離峰電價（官方明確排除）→ 仍然有效
    · ⛔ 官方離峰日 = 週日 + 9 個具名節日 → **RETRACTED**（實為週日 + 12 個節日項目）
    · ⛔ 教師節／臺灣光復紀念日／行憲紀念日不在官方清單 → **RETRACTED**
    · ⛔ 「12 個離峰日」無法由官方來源佐證 → **RETRACTED**
    這三項舊結論由 Phase D.4-E 的官方年度時間電價日曆表（P1 直接證據）推翻。
    本檔保留 D.4-D 的**架構與方法**，但斷言已改為對齊 P1 證據。

是否需要設備
    **不需要**。零網路（runtime）、零登入、零 dispatch。

用法
    python test_phase6_d4d_rule_reconciliation.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tou_calendar as TC                                # noqa: E402
import safety_gate as SG                                 # noqa: E402
import decision_policy as DP                             # noqa: E402
import tariff_provider as TP                             # noqa: E402
import annual_off_peak_calendar as AC                    # noqa: E402
import taipower_offpeak_rules as RULES                   # noqa: E402
import annual_calendar_materializer as MZ                # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


TZ, _ = TP.resolve_timezone(TP.PRODUCTION_TIMEZONE_NAME)


def at(y, mo, d, h=0):
    return datetime(y, mo, d, h, tzinfo=TZ)


def _tree(name):
    return ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())


def _all_imports(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


# ======================================================================
def main():
    print("== Phase D.4-D 官方規則對帳 驗證（完全離線）==\n")

    # ---------------- 1. 來源 metadata ----------------
    print("1. 官方來源 metadata")
    check("★★ 1. 來源名稱已記錄", "台灣電力公司" in RULES.RULE_SOURCE)
    check("★★ 1. 核定日／生效日／核備文號皆已記錄",
          "114" in RULES.RULE_APPROVED_DATE
          and "114" in RULES.RULE_EFFECTIVE_FROM
          and "11400334600" in RULES.RULE_FILING_REF)
    check("★★ 1. 版本欄位存在且可追溯", bool(RULES.RULE_VERSION))
    check("★★ 1. 目前無終止日（未發現後續改版）",
          RULES.RULE_EFFECTIVE_TO is None)
    # 🔁 D.4-E.1 更正：年度日曆表已成功解析，但 P2 逐字條文仍未取得 ——
    #    註記必須**同時**說清楚「取得了什麼」與「還沒取得什麼」。
    check("★★ 1. 明載已取得 P1 年度日曆，且明載 P2 逐字條文仍未取得",
          "年度時間電價日曆表" in RULES.RULE_SOURCE_NOTE
          and "逐字條文未另行擷取" in RULES.RULE_SOURCE_NOTE)

    # ---------------- 2. 不 hard-code 12 ----------------
    print("\n2. 規則數不 hard-code 12")
    src = io.open(os.path.join(HERE, "taipower_offpeak_rules.py"),
                  encoding="utf-8").read()
    check("★★ 2. 規則模組不含「12 個離峰日」的斷言",
          "12 個離峰日" not in src)
    # ⚠️ 只看**模組層常數賦值** —— 12 也會出現在「12 月 25 日」的月份欄位，
    #    那是日期資料不是數量常數，不可一律禁止。
    _tp = _tree("taipower_offpeak_rules.py")
    mod_consts = [n.value.value for n in _tp.body
                  if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                  and isinstance(n.value.value, int)
                  and not isinstance(n.value.value, bool)]
    check("★★ 2. 規則模組沒有以 12 作為任何模組層數量常數",
          12 not in mod_consts)
    # 🔁 D.4-E.1 更正：9 → 12 節日項目（官方年度日曆表還原結果）
    check("★★ 2. 官方確認項目數由清單長度決定（週日 + 12 節日項目）",
          len(RULES.OFFICIAL_RULES) == 13
          and len([r for r in RULES.OFFICIAL_RULES
                   if r.kind != RULES.RULE_WEEKLY_RECURRING]) == 12)
    check("★★ 2. 「12」只是說明文字，程式不得有任何數量常數",
          "12 個節日項目" in RULES.HOLIDAY_ITEM_COUNT_NOTE
          and "不得" in RULES.HOLIDAY_ITEM_COUNT_NOTE)

    # ---------------- 3/4. 未確認項目與 fixture 分離 ----------------
    print("\n3/4. 官方清單外的項目不得進 Production")
    excluded = {r.key for r in RULES.EXCLUDED_BY_OFFICIAL_SOURCE}
    three = {"teachers_day", "retrocession_day", "constitution_day"}
    # 🔁 D.4-E.1 更正：三項不再被排除，且必須留下可追溯的撤銷紀錄
    check("★★ 3. 三項不再被排除，撤銷紀錄完整且指名 D.4-E 證據",
          excluded == set()
          and {k for k, _ in RULES.RETRACTED_EXCLUSIONS} == three
          and all("D.4-E" in why for _, why in RULES.RETRACTED_EXCLUSIONS))
    check("★★ 3. confirmed_rules() 已包含這三項",
          three <= {r.key for r in RULES.confirmed_rules()})
    for y in (2026, 2027):
        r = MZ.materialize(y, "v1", date(y, 1, 1), materialized_at="t")
        # 🔁 D.4-E.1 更正：由「不得含」改為「必須含」
        check(f"★★ 3. {y} 草稿含 9/28、10/25、12/25",
              {(d.month, d.day) for d in r.draft.off_peak_dates}
              >= {(9, 28), (10, 25), (12, 25)})
    fx = io.open(os.path.join(HERE, "test_phase6_tou_calendar.py"),
                 encoding="utf-8").read()
    # 🔁 D.4-E.1 更正：fixture 內容經 P1 核對為**正確但不完整**，註記語意隨之更正
    check("★★ 4. 既有 fixture 已標註其與官方年度日曆的關係",
          "官方年度時間電價日曆" in fx and "不是** production 來源" in fx)
    check("★★ 4. 且明載「刻意不修改 fixture 內容」的理由",
          "刻意**不**修改本 fixture" in fx)
    check("★★ 4. Production 規則不從 fixture 取得（模組互不 import）",
          "test_phase6_tou_calendar" not in
          _all_imports(_tree("taipower_offpeak_rules.py")))

    # ---------------- 5/6. 春節 ----------------
    print("\n5/6. 春節區間語意")
    ny = [r for r in RULES.OFFICIAL_RULES if r.key == "lunar_new_year"][0]
    check("★★ 5. 春節為 LUNAR_RANGE（區間，非單日）",
          ny.kind == RULES.RULE_LUNAR_RANGE)
    # 🔁 D.4-E.1 更正：起算日「農曆除夕」→「農曆除夕前一日」
    check("★★ 5. 區間起訖記為「農曆除夕前一日」～「農曆正月初五」",
          ny.spec["from"] == "農曆除夕前一日"
          and ny.spec["to"] == "農曆正月初五")
    check("★★ 5. 起算日的依據明載為與官方年度日曆交叉對帳（非推測）",
          "交叉對帳確認" in ny.note and "不是推測" in ny.note
          and "無法單獨佐證起算日" in ny.note)
    check("★★ 6. 明載本身即涵蓋多個國曆日", "多個國曆日" in ny.note)
    check("★★ 6. 需人工提供且可展開為多日（inclusive 由提供者決定）",
          ny.needs_annual_input is True
          and len(MZ.materialize(
              2026, "v1", date(2026, 1, 1),
              supplied={"lunar_new_year": [date(2026, 3, 1), date(2026, 3, 2),
                                           date(2026, 3, 3), date(2026, 3, 4),
                                           date(2026, 3, 5), date(2026, 3, 6)]},
              materialized_at="t").resolved) == 8 + 6)   # 🔁 固定日 5 → 8

    # ---------------- 7~9. 各分類 ----------------
    print("\n7~9. 各分類規則")
    fixed = [r for r in RULES.OFFICIAL_RULES
             if r.kind == RULES.RULE_FIXED_GREGORIAN]
    # 🔁 D.4-E.1 更正：新增 9/28、10/25、12/25
    check("★★ 7. 固定國曆日共 8 項（1/1、2/28、4/4、5/1、9/28、10/10、10/25、12/25）",
          {(r.spec["month"], r.spec["day"]) for r in fixed}
          == {(1, 1), (2, 28), (4, 4), (5, 1), (9, 28), (10, 10),
              (10, 25), (12, 25)})
    lun = [r for r in RULES.OFFICIAL_RULES
           if r.kind == RULES.RULE_LUNAR_SINGLE_DAY]
    check("★★ 8. 農曆單日共 2 項（端午 5/5、中秋 8/15）",
          {(r.spec["lunar_month"], r.spec["lunar_day"]) for r in lun}
          == {(5, 5), (8, 15)})
    var = [r for r in RULES.OFFICIAL_RULES
           if r.kind == RULES.RULE_VARIABLE_GREGORIAN][0]
    check("★★ 9. 民族掃墓節維持「4/4 或 4/5」候選，未固定",
          [tuple(c) for c in var.spec["candidates"]] == [(4, 4), (4, 5)]
          and var.needs_annual_input is True)
    sun = [r for r in RULES.OFFICIAL_RULES if r.key == "sunday"][0]
    check("★★ 週日已納入規則表，但由既有日型規則涵蓋（不需年度資料）",
          sun.kind == RULES.RULE_WEEKLY_RECURRING
          and sun.needs_annual_input is False
          and sun.kind in RULES.COVERED_BY_DAYTYPE_RULE)
    check("★★ 週日不進入年度清單（Materializer 跳過）",
          "sunday" not in {k for k, _ in MZ.materialize(
              2026, "v1", date(2026, 1, 1), materialized_at="t").resolved}
          and "sunday" not in {u[0] for u in MZ.materialize(
              2026, "v1", date(2026, 1, 1), materialized_at="t").unresolved})

    # ---------------- 10. 政府假日分離 ----------------
    print("\n10. 政府假日調整 —— 官方明確排除")
    check("★★ 10. 已記錄官方排除的三類（彈性放假／補假／颱風假）",
          RULES.GOVERNMENT_ADJUSTMENTS_EXCLUDED
          == ("彈性放假日", "補假日", "颱風假"))
    check("★★ 10. 明載「不適用離峰電價」",
          "不適用" in RULES.GOVERNMENT_ADJUSTMENT_NOTE)
    check("★★ 10. 規則表中沒有任何補假／補班相關的離峰日項目",
          not any(k in r.name for r in RULES.ALL_RULES
                  for k in ("補假", "補班", "彈性放假", "颱風")))
    check("★★ 10. 政府行事曆不得自動視為離峰日（模組明載）",
          "政府行事曆" in src and "不得" in src)

    # ---------------- 11. 未知規則 Fail Closed ----------------
    print("\n11. 未知／未確認一律 Fail Closed")
    r26 = MZ.materialize(2026, "v1", date(2026, 1, 1), materialized_at="t")
    check("★★ 11. 農曆與變動項目未提供 → unresolved，不猜測",
          {u[0] for u in r26.unresolved}
          == {"lunar_new_year", "tomb_sweeping_day",
              "dragon_boat_festival", "mid_autumn_festival"})
    check("★★ 11. 因此草稿不完整、不得升級",
          r26.outcome == MZ.MAT_INCOMPLETE and r26.can_promote is False)
    check("★★ 11. 草稿 verified 恆為 False", r26.draft.verified is False)
    check("★★ 11. 放進 Provider 只會是 UNVERIFIED",
          AC.AnnualOffPeakDayProvider([r26.draft]).readiness(2026)
          == AC.AR_UNVERIFIED)

    # ---------------- 12. Production 未變 ----------------
    print("\n12. Production 狀態未變")
    # 🔁 D.4-E.2 更正：production 已 provision（2026／2027）
    check("★★ 12. production 年度清單只含經人工驗收的年度",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027)
          and all(l.verified is True for l in AC.PRODUCTION_ANNUAL_LISTS))
    prod = TP.TariffProvider(holiday_provider=AC.PRODUCTION_PROVIDER)
    empty = TP.TariffProvider(holiday_provider=AC.AnnualOffPeakDayProvider(()))
    check("★★ 12. 未 provision 的年度平日仍 UNKNOWN（未 fallback）",
          empty.observe(at(2026, 7, 15, 14)).state == TP.TARIFF_UNKNOWN
          and prod.observe(at(2028, 7, 12, 14)).state == TP.TARIFF_UNKNOWN)
    check("★★ 12. production 週日仍可由保守判定得到 OFF_PEAK",
          empty.observe(at(2026, 7, 19, 14)).state == TP.TARIFF_OFF_PEAK)
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ 12. dispatch 仍未啟用，模組預設仍未配置",
          RT.DISPATCH_ENABLED is False
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None)

    # ---------------- 13/14. 零 runtime 網路、零 dispatch ----------------
    print("\n13/14. 零 runtime 網路相依、零控制")
    NET = {"requests", "urllib", "urllib3", "http", "socket", "socketio",
           "httpx", "aiohttp"}
    LUNAR = {"lunardate", "chinese_calendar", "zhdate", "borax", "cnlunar",
             "sxtwl", "holidays", "workalendar"}
    PCS = {"pcs_control_executor", "device_control_operator", "api_client",
           "device_control_scraper", "charge_discharge_report",
           "production_execution_chain", "last_control_store"}
    for mod in ("taipower_offpeak_rules.py", "annual_calendar_materializer.py",
                "annual_off_peak_calendar.py", "tariff_provider.py"):
        imps = _all_imports(_tree(mod))
        check(f"★★ 13. {mod} 零 runtime 網路相依", not (imps & NET))
        check(f"★★ 13. {mod} 未引入農曆／假日第三方套件", not (imps & LUNAR))
        check(f"★★ 14. {mod} 零控制路徑", not (imps & PCS))
    check("★★ 14. dispatch 仍為 False",
          RT.DISPATCH_ENABLED is False
          and RT.AutoControlRuntime().dispatch_enabled is False)
    check("★★ 查證只在開發期進行 —— runtime 不做任何外部查詢",
          "WebSearch" not in src and "WebFetch" not in src
          and "http" not in src.lower().replace("https://powerlex", ""))

    # ---------------- 既有規則未被扭曲 ----------------
    print("\n既有時段規則未被扭曲")
    known, _ = AC.build_annual_list(2026, [], "測試（非正式）", "v1",
                                    date(2026, 1, 1), verified=True)
    p = TP.TariffProvider(holiday_provider=AC.AnnualOffPeakDayProvider([known]))
    for tag, dt, want in (("週日", at(2026, 7, 19, 14), TP.TARIFF_OFF_PEAK),
                          ("週六", at(2026, 7, 18, 14), TP.TARIFF_HALF_PEAK),
                          ("平日", at(2026, 7, 15, 14), TP.TARIFF_PEAK)):
        check(f"  {tag} → {want}（既有規則未改）", p.observe(dt).state == want)
    check("★★ 既有時段模組一行未改（規則與介面沿用）",
          issubclass(AC.AnnualOffPeakDayProvider, TC.HolidayProvider)
          and "G_RULES = " not in src)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.4-D 官方規則對帳 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
