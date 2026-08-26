# -*- coding: utf-8 -*-
"""
test_phase6_d4c_materialization.py — Phase D.4-C 年度日期具體化（1~34）
======================================================================
核心命題
    「規則可以自動展開，但**只展開能由規則唯一決定的項目**；
      農曆與逐年變動項目必須人工提供，且草稿永遠不可能自己變成已核准。」

不可違反的界線
    1. Materializer **永遠** 產生 verified=False；只有人工核准才可能 AVAILABLE。
    2. 農曆日期不自行換算、不新增第三方相依。
    3. 民族掃墓節「4/4 或 4/5」不得自行固定。
    4. 未確認的節日（未在官方清單中）不參與任何 production 產出。
    5. 有任何 unresolved → 不得升級。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d4c_materialization.py        # exit 0 = PASS
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


def mat(year=2026, supplied=None):
    return MZ.materialize(year, "draft-v1", date(year, 1, 1),
                          supplied=supplied, materialized_at="2026-08-25")


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
    print("== Phase D.4-C 年度日期具體化 驗證（完全離線）==\n")

    # ---------------- 1. 官方規則表 ----------------
    print("1. 官方規則表")
    # ⚠️ D.4-D 查證後加入「週日」（官方離峰日定義的第一項）→ 共 10 項
    # 🔁 D.4-E.1 更正：10 → 13（教師節／光復／行憲由官方年度日曆表證實後移入）
    check("★★ 1. 規則表官方確認項目數 = 清單長度（週日 + 12 節日項目）",
          len(RULES.OFFICIAL_RULES) == 13
          and all(r.confirmed for r in RULES.OFFICIAL_RULES))
    check("★★ 1. 五種規則分類齊備（含每週重複）",
          {r.kind for r in RULES.OFFICIAL_RULES}
          == {RULES.RULE_FIXED_GREGORIAN, RULES.RULE_LUNAR_SINGLE_DAY,
              RULES.RULE_LUNAR_RANGE, RULES.RULE_VARIABLE_GREGORIAN,
              RULES.RULE_WEEKLY_RECURRING})
    check("★★ 1. 規則表**不含任何特定年度的國曆日期**",
          not any(isinstance(v, date)
                  for r in RULES.ALL_RULES for v in r.spec.values()))
    # 🔁 D.4-E.1 更正：官方年度日曆表已取得並解析，改為斷言「來源分層已明載」
    check("★★ 1. 來源與其限定皆已記錄（P1 年度日曆已取得；P2 逐字條文未取得）",
          "台灣電力公司" in RULES.RULE_SOURCE
          and "年度時間電價日曆表" in RULES.RULE_SOURCE_NOTE
          and "逐字條文未另行擷取" in RULES.RULE_SOURCE_NOTE)
    # 🔁 D.4-E.1 更正：3 → 0（三項排除已由 P1 直接證據撤銷），並保留撤銷紀錄
    check("★★ 1. 官方清單中不存在的項目已無任何一項，且撤銷紀錄完整",
          len(RULES.EXCLUDED_BY_OFFICIAL_SOURCE) == 0
          and {k for k, _ in RULES.RETRACTED_EXCLUSIONS}
          == {"teachers_day", "retrocession_day", "constitution_day"})
    # ⚠️ OffPeakRule 內含 dict（spec）→ 不可雜湊，改以 key 比對
    check("★★ 1. confirmed_rules() 只回官方項目（未確認項目不參與 production）",
          {r.key for r in RULES.confirmed_rules()}
          == {r.key for r in RULES.OFFICIAL_RULES})

    # ---------------- 2~4. 各分類規則 ----------------
    print("\n2~4. 各分類規則")
    fixed = [r for r in RULES.OFFICIAL_RULES
             if r.kind == RULES.RULE_FIXED_GREGORIAN]
    # 🔁 D.4-E.1 更正：5 → 8（新增 9/28、10/25、12/25）
    check("★★ 2. 固定國曆日共 8 項且月日齊備",
          len(fixed) == 8
          and all({"month", "day"} <= set(r.spec) for r in fixed))
    lunar1 = [r for r in RULES.OFFICIAL_RULES
              if r.kind == RULES.RULE_LUNAR_SINGLE_DAY]
    check("★★ 3. 農曆單日共 2 項（端午 5/5、中秋 8/15）",
          len(lunar1) == 2
          and {(r.spec["lunar_month"], r.spec["lunar_day"]) for r in lunar1}
          == {(5, 5), (8, 15)})
    lunar_r = [r for r in RULES.OFFICIAL_RULES
               if r.kind == RULES.RULE_LUNAR_RANGE]
    check("★★ 4. 農曆區間共 1 項（春節：除夕～正月初五）",
          len(lunar_r) == 1 and lunar_r[0].key == "lunar_new_year"
          and "除夕" in lunar_r[0].spec["from"]
          and "初五" in lunar_r[0].spec["to"])
    check("★★ 5. 春節本身即為多日區間（已明文記錄）",
          "多個國曆日" in lunar_r[0].note)
    var = [r for r in RULES.OFFICIAL_RULES
           if r.kind == RULES.RULE_VARIABLE_GREGORIAN]
    check("★★ 6. 民族掃墓節記為「4/4 或 4/5」的候選，未固定",
          len(var) == 1
          and [tuple(c) for c in var[0].spec["candidates"]] == [(4, 4), (4, 5)])
    check("★★ 6. 且明文記載「不得自行固定」",
          "不得自行固定" in var[0].note)
    check("★★ 需人工提供年度日期者共 4 項（春節／掃墓節／端午／中秋）",
          {r.key for r in RULES.rules_needing_annual_input()}
          == {"lunar_new_year", "tomb_sweeping_day",
              "dragon_boat_festival", "mid_autumn_festival"})

    # ---------------- 7~9. 草稿生命週期 ----------------
    print("\n7~9. 草稿生命週期")
    r26 = mat(2026)
    check("★★ 7. 產出草稿的 verified 恆為 False",
          r26.draft is not None and r26.draft.verified is False)
    check("★★ 7. Materializer 不接受 verified 參數（結構上無法產生已核准資料）",
          "verified" not in MZ.materialize.__code__.co_varnames[
              :MZ.materialize.__code__.co_argcount])
    check("★★ 8. 草稿放進 Provider → 就緒狀態為 UNVERIFIED，**不是** AVAILABLE",
          AC.AnnualOffPeakDayProvider([r26.draft]).readiness(2026)
          == AC.AR_UNVERIFIED)
    check("★★ 8. 因此草稿完全無法用於判定（一律 None）",
          AC.AnnualOffPeakDayProvider([r26.draft]).is_off_peak_day(
              date(2026, 1, 1)) is None)
    promoted = AC.AnnualOffPeakDayList(
        **{**{k: v for k, v in vars(r26.draft).items()}, "verified": True})
    check("★★ 9. 只有人工核准（verified=True）後才成為 AVAILABLE",
          AC.AnnualOffPeakDayProvider([promoted]).readiness(2026)
          == AC.AR_AVAILABLE
          and AC.AnnualOffPeakDayProvider([promoted]).is_off_peak_day(
              date(2026, 1, 1)) is True)
    check("★★ 9. 有 unresolved 時 can_promote 為 False（不完整草稿不得升級）",
          r26.unresolved and r26.can_promote is False
          and r26.outcome == MZ.MAT_INCOMPLETE)
    full = mat(2026, supplied={
        "lunar_new_year": [date(2026, 3, 1), date(2026, 3, 2)],
        "tomb_sweeping_day": date(2026, 4, 5),
        "dragon_boat_festival": date(2026, 6, 1),
        "mid_autumn_festival": date(2026, 9, 1)})
    check("★★ 9. 全部提供後 → MATERIALIZED_COMPLETE 且 can_promote=True",
          full.outcome == MZ.MAT_COMPLETE and not full.unresolved
          and full.can_promote is True)
    check("★★ 9. 但 can_promote=True 仍不等於已核准（draft 依舊 verified=False）",
          full.draft.verified is False)

    # ---------------- 10/11. 2026 / 2027 草稿 ----------------
    print("\n10/11. 2026 / 2027 草稿")
    r27 = mat(2027)
    for tag, r, y in (("10. 2026", r26, 2026), ("11. 2027", r27, 2027)):
        # 🔁 D.4-E.1 更正：固定國曆日由 5 個增為 8 個
        check(f"★★ {tag} 草稿只含 8 個固定國曆日",
              [d.isoformat() for d in r.draft.off_peak_dates]
              == [f"{y}-01-01", f"{y}-02-28", f"{y}-04-04",
                  f"{y}-05-01", f"{y}-09-28", f"{y}-10-10",
                  f"{y}-10-25", f"{y}-12-25"])
        check(f"★★ {tag} 有 4 項待人工提供",
              len(r.unresolved) == 4
              and {u[0] for u in r.unresolved}
              == {"lunar_new_year", "tomb_sweeping_day",
                  "dragon_boat_festival", "mid_autumn_festival"})
        # 🔁 D.4-E.1 更正：這三項已由官方年度日曆表證實，改為必須**納入**
        check(f"  {tag} 已納入官方證實的三項（9/28、10/25、12/25）",
              {(d.month, d.day) for d in r.draft.off_peak_dates}
              >= {(9, 28), (10, 25), (12, 25)})
        doc = MZ.draft_document(r)
        check(f"  {tag} 交付文件含 rule_source / materialized_at / method",
              doc["verified"] is False and doc["rule_source"]
              and doc["materialized_at"] and doc["materialization_method"]
              and doc["unresolved"])

    # ---------------- 6. 掃墓節不得猜 ----------------
    print("\n掃墓節：不得猜測、只接受候選日")
    check("★★ 6. 未提供 → unresolved（絕不自行固定成 4/4）",
          ("tomb_sweeping_day", RULES.RULE_VARIABLE_GREGORIAN,
           MZ.UR_VARIABLE_NOT_SUPPLIED) in r26.unresolved)
    ok44 = mat(2026, supplied={"tomb_sweeping_day": date(2026, 4, 4)})
    ok45 = mat(2026, supplied={"tomb_sweeping_day": date(2026, 4, 5)})
    check("★★ 6. 提供 4/4 或 4/5 皆被接受",
          date(2026, 4, 4) in ok44.draft.off_peak_dates
          and date(2026, 4, 5) in ok45.draft.off_peak_dates)
    bad = mat(2026, supplied={"tomb_sweeping_day": date(2026, 4, 6)})
    check("★★ 6. 提供非候選日（4/6）→ 拒絕並列為 unresolved",
          ("tomb_sweeping_day", RULES.RULE_VARIABLE_GREGORIAN,
           MZ.UR_NOT_A_CANDIDATE) in bad.unresolved
          and date(2026, 4, 6) not in bad.draft.off_peak_dates)

    # ---------------- 12~14. 供應資料驗證 ----------------
    print("\n12~14. 人工提供資料的驗證")
    wrong = mat(2026, supplied={"dragon_boat_festival": date(2025, 6, 1)})
    check("★★ 12. 年份不符 → unresolved（不採用）",
          ("dragon_boat_festival", RULES.RULE_LUNAR_SINGLE_DAY,
           MZ.UR_SUPPLIED_WRONG_YEAR) in wrong.unresolved)
    for bad_val, tag in (("2026-13-01", "日期格式錯誤"), (20260101, "非日期型別"),
                         (True, "bool")):
        rr = mat(2026, supplied={"mid_autumn_festival": bad_val})
        check(f"★★ 14. {tag} → unresolved",
              ("mid_autumn_festival", RULES.RULE_LUNAR_SINGLE_DAY,
               MZ.UR_SUPPLIED_INVALID) in rr.unresolved)
    dup = mat(2026, supplied={"dragon_boat_festival": date(2026, 1, 1)})
    check("★★ 13. 與固定日重複 → 去重後仍為合法草稿（不重複計入）",
          dup.draft is not None
          and list(dup.draft.off_peak_dates).count(date(2026, 1, 1)) == 1)
    check("  ISO 字串亦可接受（人工檔案格式一致）",
          date(2026, 6, 1) in mat(
              2026, supplied={"dragon_boat_festival": "2026-06-01"}
          ).draft.off_peak_dates)
    rng = mat(2026, supplied={"lunar_new_year":
                              [date(2026, 3, 1), date(2026, 3, 2),
                               date(2026, 3, 3)]})
    check("★★ 5. 春節多日區間可展開為多個國曆日",
          all(d in rng.draft.off_peak_dates
              for d in (date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3))))

    # ---------------- 15~18. 與時段判定串接 ----------------
    print("\n15~18. 與時段判定串接（以人工核准後的資料）")
    lst, _ = AC.build_annual_list(
        2026, list(promoted.off_peak_dates), "測試（非正式）", "v1",
        date(2026, 1, 1), verified=True)
    p = TP.TariffProvider(holiday_provider=AC.AnnualOffPeakDayProvider([lst]))
    check("★★ 16. 週日 → OFF_PEAK", p.observe(at(2026, 7, 19, 14)).state
          == TP.TARIFF_OFF_PEAK)
    check("★★ 17. 週六（非離峰日）→ HALF_PEAK",
          p.observe(at(2026, 7, 18, 14)).state == TP.TARIFF_HALF_PEAK)
    check("★★ 18. 平日（非離峰日）→ PEAK",
          p.observe(at(2026, 7, 15, 14)).state == TP.TARIFF_PEAK)
    check("★★ 離峰日（2/28 為週六）→ 覆蓋 HALF_PEAK 成 OFF_PEAK",
          date(2026, 2, 28).weekday() == 5
          and p.observe(at(2026, 2, 28, 14)).state == TP.TARIFF_OFF_PEAK)
    check("★★ 離峰日（1/1 為週四）→ 覆蓋 PEAK 成 OFF_PEAK",
          date(2026, 1, 1).weekday() == 3
          and p.observe(at(2026, 1, 1, 14)).state == TP.TARIFF_OFF_PEAK)
    check("★★ 15. 年度切換：2027 未核准 → 平日 UNKNOWN，不沿用 2026",
          p.observe(at(2027, 1, 4, 14)).state == TP.TARIFF_UNKNOWN)
    check("★★ 19. 時區仍為 Asia/Taipei",
          p.observe(at(2026, 7, 15, 14)).timezone_name == "Asia/Taipei")

    # ---------------- 20/21. 零相依、零控制 ----------------
    print("\n20/21. 零網路相依、零控制能力")
    NET = {"requests", "urllib", "urllib3", "http", "socket", "socketio",
           "httpx", "aiohttp"}
    LUNAR_PKG = {"lunardate", "chinese_calendar", "zhdate", "borax",
                 "cnlunar", "sxtwl", "holidays", "workalendar"}
    PCS = {"pcs_control_executor", "device_control_operator", "api_client",
           "device_control_scraper", "charge_discharge_report",
           "production_execution_chain", "last_control_store"}
    for mod in ("taipower_offpeak_rules.py", "annual_calendar_materializer.py"):
        imps = _all_imports(_tree(mod))
        check(f"★★ 20. {mod} 零網路相依", not (imps & NET))
        check(f"★★ 20. {mod} 未引入任何農曆／假日第三方套件",
              not (imps & LUNAR_PKG))
        check(f"★★ 21. {mod} 零控制路徑", not (imps & PCS))
    src = io.open(os.path.join(HERE, "annual_calendar_materializer.py"),
                  encoding="utf-8").read()
    check("★★ 20. Materializer 不做農曆換算（無任何農曆演算法）",
          "不做農曆換算" in src
          and not any(k in src for k in ("lunar_to_solar", "solar_to_lunar",
                                         "jieqi", "朔日")))
    check("★★ 21. dispatch 仍為 False",
          RT.DISPATCH_ENABLED is False
          and RT.AutoControlRuntime().dispatch_enabled is False)

    # ---------------- Production 狀態 ----------------
    print("\nProduction 狀態")
    # 🔁 D.4-E.2 更正：production 已 provision 經人工驗收的年度；
    #    「規則展開的草稿不得自行進 production」這個契約改用結構性斷言表達。
    check("★★ production 清單只含經人工驗收的官方年度資料（規則草稿未納入）",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027)
          and all("官方年度時間電價日曆表" in l.source
                  and "人工驗收" in l.source_version
                  for l in AC.PRODUCTION_ANNUAL_LISTS))
    check("★★ 規則展開的草稿仍為 verified=False，放進 Provider 只會是 UNVERIFIED",
          all(MZ.materialize(y, "v1", date(y, 1, 1),
                             materialized_at="t").draft.verified is False
              and AC.AnnualOffPeakDayProvider(
                  [MZ.materialize(y, "v1", date(y, 1, 1),
                                  materialized_at="t").draft]).readiness(y)
              == AC.AR_UNVERIFIED for y in (2026, 2027)))
    prod = TP.TariffProvider(holiday_provider=AC.PRODUCTION_PROVIDER)
    empty = TP.TariffProvider(holiday_provider=AC.AnnualOffPeakDayProvider(()))
    check("★★ 未 provision 的年度仍 UNKNOWN、週日仍可保守判定",
          empty.observe(at(2026, 7, 15, 14)).state == TP.TARIFF_UNKNOWN
          and empty.observe(at(2026, 7, 19, 14)).state == TP.TARIFF_OFF_PEAK
          and prod.observe(at(2028, 7, 12, 14)).state == TP.TARIFF_UNKNOWN)
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ dispatch 仍未啟用，且各模組預設仍未配置",
          RT.DISPATCH_ENABLED is False
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None)
    check("★★ 既有時段模組未被修改（規則與介面沿用）",
          issubclass(AC.AnnualOffPeakDayProvider, TC.HolidayProvider)
          and "G_RULES = " not in io.open(
              os.path.join(HERE, "tariff_provider.py"),
              encoding="utf-8").read())

    ok_all = all(RESULTS)
    print(f"\n== Phase D.4-C 年度日期具體化 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
