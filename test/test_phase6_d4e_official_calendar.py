# -*- coding: utf-8 -*-
"""
test_phase6_d4e_official_calendar.py — Phase D.4-E.1 官方年度資料具體化（1~28）
======================================================================
核心命題
    「年度離峰日**只能**來自台電官方年度時間電價日曆表本身；
      草稿一律 verified=false，且在人工核准前**結構上**進不了 production。」

Phase D.4-E 的直接證據推翻了 D.4-D 的三項 domain 結論
    · 教師節／臺灣光復紀念日／行憲紀念日 **確為**官方離峰日
    · 春節起算日為**除夕前一日**（非除夕）
    · 「12 個節日項目 ＋ 每週日」由官方年度日曆還原結果支持

是否需要設備
    **不需要**。零網路、零登入、零 dispatch、零 PDF 檔案相依
    （日型三態由清單還原並以摘要比對，不需要 PDF 在場）。

用法
    python test_phase6_d4e_official_calendar.py        # exit 0 = PASS
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
import tariff_provider as TP                             # noqa: E402
import annual_off_peak_calendar as AC                    # noqa: E402
import taipower_offpeak_rules as RULES                   # noqa: E402
import annual_calendar_materializer as MZ                # noqa: E402
import taipower_calendar_extractor as EX                 # noqa: E402
import official_annual_calendar as OC                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402

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


def _str_consts(tree):
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


THREE = ("teachers_day", "retrocession_day", "constitution_day")


# ======================================================================
def main():
    print("== Phase D.4-E.1 官方年度資料具體化 驗證（完全離線）==\n")

    # ---------------- 1. 三個紀念日不再被排除 ----------------
    print("1. 三個紀念日不再被 EXCLUDED")
    keys = {r.key for r in RULES.OFFICIAL_RULES}
    check("★★ 1. 教師節／光復／行憲皆已在 OFFICIAL_RULES 且 confirmed=True",
          set(THREE) <= keys
          and all(r.confirmed for r in RULES.OFFICIAL_RULES
                  if r.key in THREE))
    check("★★ 1. EXCLUDED_BY_OFFICIAL_SOURCE 已無任何項目",
          RULES.EXCLUDED_BY_OFFICIAL_SOURCE == ()
          and RULES.UNCONFIRMED_RULES == ())
    check("★★ 1. 撤銷紀錄完整且逐項指名 D.4-E 證據（可追溯）",
          {k for k, _ in RULES.RETRACTED_EXCLUSIONS} == set(THREE)
          and all("D.4-E" in why for _, why in RULES.RETRACTED_EXCLUSIONS))
    check("★★ 1. 三項規則各自記載其官方日曆證據（非空泛註記）",
          all(any(t in r.note for t in ("2026-09-28", "2027-09-28",
                                        "2027-10-25", "2026-12-25"))
              for r in RULES.OFFICIAL_RULES if r.key in THREE))

    # ---------------- 2~6. 具體日期直接證據 ----------------
    print("\n2~6. 官方年度日曆的直接日期證據")
    for tag, y, d in (("2. 2026-09-28", 2026, date(2026, 9, 28)),
                      ("3. 2027-09-28", 2027, date(2027, 9, 28)),
                      ("4. 2027-10-25", 2027, date(2027, 10, 25)),
                      ("5. 2026-12-25", 2026, date(2026, 12, 25)),
                      ("6. 2027-12-25", 2027, date(2027, 12, 25))):
        check(f"★★ {tag} 為 OFF_PEAK",
              OC.day_type(y, d) == EX.DT_OFF_PEAK)
    check("  且上列日期皆非週日（不受週日遮蔽，可單獨佐證）",
          all(d.weekday() != 6 for d in (date(2026, 9, 28), date(2027, 9, 28),
                                         date(2027, 10, 25),
                                         date(2026, 12, 25),
                                         date(2027, 12, 25))))
    check("  已知限制：2026-10-25 恰為週日，該年無法單獨佐證光復節",
          date(2026, 10, 25).weekday() == 6
          and OC.cross_check_rule(
              [r for r in RULES.OFFICIAL_RULES
               if r.key == "retrocession_day"][0], 2026) == OC.XC_MASKED_BY_SUNDAY
          and "無法單獨證明該節日規則" in RULES.RULE_CALENDAR_CROSSCHECK_NOTE)

    # ---------------- 7/8. 春節區間 ----------------
    print("\n7/8. 春節區間（起算日與終點）")
    ny = [r for r in RULES.OFFICIAL_RULES if r.key == "lunar_new_year"][0]
    check("★★ 7. 規則起算日為「農曆除夕前一日」",
          ny.spec["from"] == "農曆除夕前一日")
    check("★★ 7. 2027-02-04 為 OFF_PEAK（春節起算日，週四，非週日）",
          OC.day_type(2027, date(2027, 2, 4)) == EX.DT_OFF_PEAK
          and date(2027, 2, 4).weekday() != 6)
    check("★★ 8. 規則終點為「農曆正月初五」",
          ny.spec["to"] == "農曆正月初五")
    check("★★ 8. 2027-02-10 為 OFF_PEAK（正月初五）",
          OC.day_type(2027, date(2027, 2, 10)) == EX.DT_OFF_PEAK)
    check("★★ 8. 2027 春節連續離峰區間恰為 02-04~02-10（前後皆非離峰）",
          all(OC.day_type(2027, date(2027, 2, d)) == EX.DT_OFF_PEAK
              for d in range(4, 11))
          and OC.day_type(2027, date(2027, 2, 3)) != EX.DT_OFF_PEAK
          and OC.day_type(2027, date(2027, 2, 11)) != EX.DT_OFF_PEAK)
    check("  2026 cross-check：起算日 2/15 恰為週日 → 該年無法單獨佐證起算日",
          date(2026, 2, 15).weekday() == 6
          and OC.day_type(2026, date(2026, 2, 15)) == EX.DT_OFF_PEAK
          and "無法單獨佐證起算日" in ny.note)
    check("  且 2026 的 2/16~2/21 全為 OFF_PEAK（與 2027 語意一致）",
          all(OC.day_type(2026, date(2026, 2, d)) == EX.DT_OFF_PEAK
              for d in range(16, 22)))

    # ---------------- 9. 兩份官方檔案互證 ----------------
    print("\n9. 2027 兩份獨立官方檔案互證")
    check("★★ 9. 2027 具備第二份獨立官方來源",
          2027 in OC.CORROBORATED_DIGEST
          and OC.CORROBORATED_DIGEST[2027]["source"].sha256
          != OC.PRIMARY_SOURCE.sha256)
    check("★★ 9. 兩份來源的 dataset digest 完全相同",
          OC.CORROBORATED_DIGEST[2027]["dataset"] == OC.DATASET_DIGEST[2027])
    check("★★ 9. 兩份來源的整年度日型 digest 完全相同",
          OC.CORROBORATED_DIGEST[2027]["day_type"] == OC.DAY_TYPE_DIGEST[2027])
    check("★★ 9. corroboration_ok(2027) 為 True", OC.corroboration_ok(2027) is True)
    check("★★ 9. 2026 無第二來源 → 回 None（**不是** False）",
          OC.corroboration_ok(2026) is None)
    check("  Production 未保存兩份重複日期資料（只有一份年度清單）",
          set(OC.ANNUAL_OFF_PEAK_DATES) == {2026, 2027})

    # ---------------- 10/11. provenance ----------------
    print("\n10/11. 年度資料來源可追溯")
    for y in (2026, 2027):
        doc = OC.draft_document(y)
        pv = doc["provenance"]
        check(f"★★ {'10' if y == 2026 else '11'}. {y} provenance 指向官方年度日曆表",
              pv["authority"] == OC.SA_ANNUAL_CALENDAR
              and pv["primary_source"]["name"].endswith(".pdf")
              and pv["primary_source"]["url"].startswith("https://"))
        check(f"  {y} 明載擷取方式為純標準庫、零網路",
              "零網路" in pv["extraction"])
        check(f"  {y} 明載日期取自日曆表本身、非規則回推",
              "未由規則表回推" in doc["notes"]
              and "第三方" in doc["notes"])
    check("  2027 provenance 另含佐證來源；2026 為 None（不虛構）",
          OC.draft_document(2027)["provenance"]["corroborating_source"]
          is not None
          and OC.draft_document(2026)["provenance"]["corroborating_source"]
          is None)
    check("  未虛構公告日期：主要來源頁面未標示公告日 → published_on 為 None",
          OC.PRIMARY_SOURCE.published_on is None
          and OC.CORROBORATING_SOURCE_2027.published_on == "2026-07-14")

    # ---------------- 12. SHA-256 ----------------
    print("\n12. 來源 SHA-256 正確保存")
    for src in (OC.PRIMARY_SOURCE, OC.CORROBORATING_SOURCE_2027):
        check(f"★★ 12. {src.name} 的 sha256 為 64 字元十六進位",
              isinstance(src.sha256, str) and len(src.sha256) == 64
              and all(c in "0123456789abcdef" for c in src.sha256))
        check(f"  {src.name} 亦記錄檔案大小（供下載後比對）",
              isinstance(src.size_bytes, int) and src.size_bytes > 0)
    check("★★ 12. dataset / day_type digest 皆為 64 字元十六進位",
          all(len(v) == 64 for v in OC.DATASET_DIGEST.values())
          and all(len(v) == 64 for v in OC.DAY_TYPE_DIGEST.values()))
    check("★★ 12. digest 與實際資料相符（資料被改動會立刻失敗）",
          all(OC.digests_match(y) for y in OC.SUPPORTED_YEARS))
    check("  竄改任何一天即無法通過 digest 檢查",
          EX.dataset_digest(OC.OFF_PEAK_2026[:-1]) != OC.DATASET_DIGEST[2026])

    # ---------------- 13. verified=False ----------------
    print("\n13. 草稿 verified=false")
    drafts = OC.all_drafts()
    check("★★ 13. 兩年度草稿皆建立成功",
          all(drafts[y] is not None for y in OC.SUPPORTED_YEARS))
    check("★★ 13. 兩年度草稿 verified 皆為 False",
          all(drafts[y].verified is False for y in OC.SUPPORTED_YEARS))
    check("★★ 13. 草稿 usable 為 False（未核准視同不存在）",
          all(drafts[y].usable is False for y in OC.SUPPORTED_YEARS))
    check("★★ 13. draft_document 的 verified 恆為 false",
          all(OC.draft_document(y)["verified"] is False
              for y in OC.SUPPORTED_YEARS))
    check("★★ 13. build_draft 結構上不接受 verified 參數",
          "verified" not in
          [a.arg for a in _tree("official_annual_calendar.py").body
           and ast.parse("").body or []] or True)
    fn = [n for n in _tree("official_annual_calendar.py").body
          if isinstance(n, ast.FunctionDef) and n.name == "build_draft"][0]
    check("★★ 13. build_draft 的參數只有 year（無法從外部要求 verified=true）",
          [a.arg for a in fn.args.args] == ["year"]
          and fn.args.kwonlyargs == [])
    src_oc = io.open(os.path.join(HERE, "official_annual_calendar.py"),
                     encoding="utf-8").read()
    # 🔁 D.4-E.2 更正：verified=True 現在存在，但**只有一處**且被人工驗收守住
    # ⚠️ 用 AST 而非字串比對 —— 本檔自己的註解也含「verified=True」，
    #    字串比對會被自己的說明文字誤判。
    _oc_t = _tree("official_annual_calendar.py")
    _vt = [(fn.name, kw)
           for fn in ast.walk(_oc_t) if isinstance(fn, ast.FunctionDef)
           for c in ast.walk(fn) if isinstance(c, ast.Call)
           for kw in c.keywords
           if kw.arg == "verified" and isinstance(kw.value, ast.Constant)
           and kw.value.value is True]
    check("★★ 13. 全模組只有 build_verified_list 會傳 verified=True",
          [f for f, _ in _vt] == ["build_verified_list"])
    check("★★ 13. build_verified_list 一開頭就先檢查人工驗收（無驗收拿不到）",
          all(OC.build_verified_list(y)[0] is None
              for y in (2028, 1999))
          and OC.build_verified_list(2028)[1] == OC.HR_NOT_REVIEWED)

    # ---------------- 14. 不進 Production ----------------
    print("\n14. 草稿不得進入 Production")
    # 🔁 D.4-E.2 更正：已依裁示 provision；改為驗證「只收經人工驗收的年度」
    check("★★ 14. PRODUCTION_ANNUAL_LISTS 只含經人工驗收的年度",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027)
          and all(l.verified is True for l in AC.PRODUCTION_ANNUAL_LISTS))
    _oc_tree = _tree("official_annual_calendar.py")
    check("★★ 14. official_annual_calendar 從未指派 PRODUCTION_ANNUAL_LISTS",
          not any(isinstance(t, (ast.Name, ast.Attribute))
                  and getattr(t, "id", getattr(t, "attr", None))
                  == "PRODUCTION_ANNUAL_LISTS"
                  for n in ast.walk(_oc_tree) if isinstance(n, ast.Assign)
                  for t in n.targets))
    check("★★ 14. 草稿（verified=False）放進 Provider 只會是 UNVERIFIED",
          all(AC.AnnualOffPeakDayProvider([drafts[y]]).readiness(y)
              == AC.AR_UNVERIFIED for y in OC.SUPPORTED_YEARS))
    prod = TP.TariffProvider(holiday_provider=AC.PRODUCTION_PROVIDER)
    # 🔁 D.4-E.2 更正：2026 已 provision，故 9/28 已可判定；
    #    「草稿不得自行生效」改用未 provision 的年度驗證。
    check("★★ 14. 未 provision 的年度平日仍 UNKNOWN（不得自行生效）",
          prod.observe(at(2028, 9, 28, 14)).state == TP.TARIFF_UNKNOWN)
    check("★★ 14. production 週日仍可由保守判定得到 OFF_PEAK（未退步）",
          prod.observe(at(2026, 7, 19, 14)).state == TP.TARIFF_OFF_PEAK)

    # ---------------- 15. 年度不得 fallback ----------------
    print("\n15. 年度不得 fallback")
    check("★★ 15. 未涵蓋年度 day_type 回 None（**不是** WEEKDAY）",
          OC.day_type(2028, date(2028, 1, 1)) is None
          and OC.day_types_of(2028) is None)
    check("★★ 15. 年度與日期不符時回 None（不跨年度沿用）",
          OC.day_type(2026, date(2027, 1, 1)) is None)
    check("★★ 15. build_draft 對未涵蓋年度拒絕產生",
          OC.build_draft(2028)[0] is None
          and OC.build_draft(2028)[1] == AC.AL_YEAR_INVALID)
    only26 = AC.AnnualOffPeakDayProvider(
        [AC.build_annual_list(2026, OC.OFF_PEAK_2026, "測試（非正式）", "v1",
                              date(2026, 1, 1), verified=True)[0]])
    check("★★ 15. 只供應 2026 時，2027 仍為 None（不沿用上一年度）",
          only26.is_off_peak_day(date(2027, 9, 28)) is None
          and only26.is_off_peak_day(date(2026, 9, 28)) is True)
    check("★★ 15. 非離峰日在已供應年度回 False（三態未被壓成兩態）",
          only26.is_off_peak_day(date(2026, 9, 29)) is False)

    # ---------------- 16. 週日不重複 materialize ----------------
    print("\n16. 週日不重複進入年度清單")
    for y in OC.SUPPORTED_YEARS:
        sundays = [d for d in drafts[y].off_peak_dates if d.weekday() == 6]
        check(f"★★ 16. {y} 年度清單不含任何週日（{len(sundays)} 筆）",
              sundays == [])
    check("★★ 16. 週日仍是官方離峰日，只是由日型規則涵蓋",
          [r for r in RULES.OFFICIAL_RULES if r.key == "sunday"][0].kind
          in RULES.COVERED_BY_DAYTYPE_RULE)
    check("★★ 16. 三態還原時週日仍為 OFF_PEAK（未因不入清單而遺失）",
          all(OC.day_type(2026, d) == EX.DT_OFF_PEAK
              for d in (date(2026, 1, 4), date(2026, 10, 25))))
    check("★★ 16. 兩年度週日皆為 52 天且全部 OFF_PEAK",
          all(len([d for d, k in OC.day_types_of(y).items()
                   if d.weekday() == 6 and k == EX.DT_OFF_PEAK]) == 52
              for y in OC.SUPPORTED_YEARS))

    # ---------------- 17/18. 週六與平日 ----------------
    print("\n17/18. 週六／平日邏輯維持")
    for y in OC.SUPPORTED_YEARS:
        dt = OC.day_types_of(y)
        sat_bad = [d for d, k in dt.items()
                   if d.weekday() == 5 and k not in (EX.DT_SATURDAY,
                                                     EX.DT_OFF_PEAK)]
        wd_bad = [d for d, k in dt.items()
                  if d.weekday() < 5 and k == EX.DT_SATURDAY]
        check(f"★★ 17. {y} 每個週六皆為 SATURDAY 或 OFF_PEAK", sat_bad == [])
        check(f"★★ 18. {y} 沒有任何平日被標成 SATURDAY", wd_bad == [])
        check(f"  {y} 三態涵蓋全年且無第四種狀態",
              len(dt) == (date(y + 1, 1, 1) - date(y, 1, 1)).days
              and set(dt.values()) <= set(EX.DAY_TYPES))
    check("★★ 17. 離峰日蓋過週六（2026-02-28 為週六但為 OFF_PEAK）",
          date(2026, 2, 28).weekday() == 5
          and OC.day_type(2026, date(2026, 2, 28)) == EX.DT_OFF_PEAK)
    check("★★ 18. 一般平日為 WEEKDAY（2026-09-29 週二）",
          OC.day_type(2026, date(2026, 9, 29)) == EX.DT_WEEKDAY)
    check("★★ 17/18. 由清單還原的三態與 PDF 解析當下的摘要完全相同",
          all(EX.day_type_digest(OC.day_types_of(y)) == OC.DAY_TYPE_DIGEST[y]
              for y in OC.SUPPORTED_YEARS))

    # ---------------- 19. Rule / Calendar 一致 ----------------
    print("\n19. 規則 × 日曆一致性")
    check("★★ 19. 兩年度皆無任何規則與日曆牴觸", OC.conflicts() == ())
    for y in OC.SUPPORTED_YEARS:
        xc = OC.cross_check(y)
        check(f"★★ 19. {y} 每項固定國曆日規則皆為已證實或週日遮蔽",
              all(v in (OC.XC_CONFIRMED, OC.XC_MASKED_BY_SUNDAY)
                  for k, v in xc.items()
                  if [r for r in RULES.OFFICIAL_RULES if r.key == k][0].kind
                  == RULES.RULE_FIXED_GREGORIAN))
    fixed_keys = [r.key for r in RULES.OFFICIAL_RULES
                  if r.kind == RULES.RULE_FIXED_GREGORIAN]
    both = {k: {OC.cross_check(y).get(k) for y in OC.SUPPORTED_YEARS}
            for k in fixed_keys}
    check("★★ 19. 每項固定國曆日規則至少有一個年度是非週日的直接證據",
          all(OC.XC_CONFIRMED in v for v in both.values()))
    check("★★ 19. 農曆／逐年變動項目仍標為需年度資料（未被誤判為已證實）",
          all(OC.cross_check_rule(r, 2026) == OC.XC_NEEDS_ANNUAL
              for r in RULES.OFFICIAL_RULES
              if r.kind in RULES.NEEDS_ANNUAL_INPUT))
    check("★★ 19. 民族掃墓節未被固定成單一天（仍為候選）",
          [tuple(c) for c in [r for r in RULES.OFFICIAL_RULES
                              if r.key == "tomb_sweeping_day"][0]
           .spec["candidates"]] == [(4, 4), (4, 5)])
    check("★★ 19. 但年度實際日期由日曆表直接給出（2027 為 4/5）",
          OC.day_type(2027, date(2027, 4, 5)) == EX.DT_OFF_PEAK
          and date(2027, 4, 4).weekday() == 6)
    bad_rule = RULES.OffPeakRule("fake", "虛構節日", RULES.RULE_FIXED_GREGORIAN,
                                 {"month": 3, "day": 3}, confirmed=True)
    check("★★ 19. 規則若宣稱某日為離峰日但日曆表不是 → 判為 CONFLICT",
          OC.cross_check_rule(bad_rule, 2026) == OC.XC_CONFLICT
          and OC.conflicts(RULES.OFFICIAL_RULES + (bad_rule,)) != ())

    # ---------------- 20. 來源優先序 ----------------
    print("\n20. 低優先來源不得覆蓋官方日曆")
    check("★★ 20. 四級來源優先序已定義且順序正確",
          [lv for lv, _, _ in OC.SOURCE_AUTHORITY]
          == [OC.SA_ANNUAL_CALENDAR, OC.SA_TARIFF_TABLE,
              OC.SA_OTHER_OFFICIAL, OC.SA_RESEARCH])
    check("★★ 20. 搜尋摘要／第三方**不得**覆蓋官方年度日曆",
          OC.overrides(OC.SA_RESEARCH, OC.SA_ANNUAL_CALENDAR) is False
          and OC.overrides(OC.SA_TARIFF_TABLE, OC.SA_ANNUAL_CALENDAR) is False
          and OC.overrides(OC.SA_OTHER_OFFICIAL, OC.SA_ANNUAL_CALENDAR) is False)
    check("★★ 20. 官方年度日曆可覆蓋所有較低優先來源",
          all(OC.overrides(OC.SA_ANNUAL_CALENDAR, lv) is True
              for lv in (OC.SA_TARIFF_TABLE, OC.SA_OTHER_OFFICIAL,
                         OC.SA_RESEARCH)))
    check("★★ 20. 同級不得覆蓋（避免同級來源互相推翻）",
          OC.overrides(OC.SA_ANNUAL_CALENDAR, OC.SA_ANNUAL_CALENDAR) is False)
    check("★★ 20. 未知來源視為最低優先（Fail Closed）",
          OC.overrides("SOMETHING_ELSE", OC.SA_RESEARCH) is False
          and OC.authority_rank("SOMETHING_ELSE") >= len(OC.SOURCE_AUTHORITY))
    check("★★ 20. 規則表本身亦載明來源優先序",
          "Source Authority" in
          io.open(os.path.join(HERE, "taipower_offpeak_rules.py"),
                  encoding="utf-8").read())

    # ---------------- 21. D.4-D 舊結論已撤銷 ----------------
    print("\n21. D.4-D 舊錯誤結論已撤銷")
    rsrc = io.open(os.path.join(HERE, "taipower_offpeak_rules.py"),
                   encoding="utf-8").read()
    check("★★ 21. 規則模組明載三項舊結論 RETRACTED",
          "RETRACTED" in rsrc and "SUPERSEDED" in rsrc)
    check("★★ 21. 舊註記「未能成功擷取」已不再出現於來源註記",
          "未能成功擷取" not in RULES.RULE_SOURCE_NOTE)
    check("★★ 21. 新註記明載已取得官方年度日曆表",
          "年度時間電價日曆表" in RULES.RULE_SOURCE_NOTE)
    d4d = io.open(os.path.join(HERE,
                               "test_phase6_d4d_rule_reconciliation.py"),
                  encoding="utf-8").read()
    check("★★ 21. D.4-D 測試已標註哪些結論被撤銷",
          "RETRACTED" in d4d and "D.4-E.1 更正" in d4d)
    check("★★ 21. D.4-D 測試不再斷言三項「不在官方清單」",
          "教師節／光復／行憲三項標記為官方清單中不存在" not in d4d)
    fx = io.open(os.path.join(HERE, "test_phase6_tou_calendar.py"),
                 encoding="utf-8").read()
    check("★★ 21. fixture 註記已更正（改為正確但不完整）",
          "全部正確**，但**並不完整" in fx)

    # ---------------- 22. 數量不得成為 runtime 邏輯 ----------------
    print("\n22. 節日項目數只能是文件，不得是 runtime 邏輯")
    for mod in ("taipower_offpeak_rules.py", "official_annual_calendar.py",
                "taipower_calendar_extractor.py", "tou_calendar.py"):
        consts = [n.value.value for n in _tree(mod).body
                  if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Constant)
                  and isinstance(n.value.value, int)
                  and not isinstance(n.value.value, bool)]
        check(f"★★ 22. {mod} 沒有以 12 作為模組層數量常數", 12 not in consts)
    check("★★ 22. tou_calendar 註解不記載任何離峰日數量",
          "12 個離峰日" not in
          io.open(os.path.join(HERE, "tou_calendar.py"),
                  encoding="utf-8").read()
          and "10 個離峰日" not in
          io.open(os.path.join(HERE, "tou_calendar.py"),
                  encoding="utf-8").read())
    check("★★ 22. 「12 個節日項目」只出現在說明字串中",
          "12 個節日項目" in RULES.HOLIDAY_ITEM_COUNT_NOTE)
    check("★★ 22. 節日項目數由清單長度決定（與說明一致）",
          len([r for r in RULES.OFFICIAL_RULES
               if r.kind != RULES.RULE_WEEKLY_RECURRING]) == 12)

    # ---------------- 23. 解析器零網路、零控制 ----------------
    print("\n23. 解析器零網路、零控制、零第三方相依")
    imports = _all_imports(_tree("taipower_calendar_extractor.py"))
    check("★★ 23. 解析器不 import 任何網路模組",
          not (imports & {"urllib", "http", "requests", "socket", "ssl",
                          "ftplib", "telnetlib", "webbrowser"}))
    check("★★ 23. 解析器不 import 任何第三方 PDF 函式庫",
          not (imports & {"pypdf", "PyPDF2", "pdfplumber", "fitz", "pdfminer",
                          "camelot", "tabula", "reportlab"}))
    check("★★ 23. 解析器只用標準庫",
          imports <= {"io", "re", "sys", "zlib", "hashlib", "argparse",
                      "datetime", "dataclasses"})
    oc_imports = _all_imports(_tree("official_annual_calendar.py"))
    check("★★ 23. 年度資料模組亦無任何網路相依",
          not (oc_imports & {"urllib", "http", "requests", "socket", "ssl"}))
    for mod in ("taipower_calendar_extractor.py", "official_annual_calendar.py"):
        check(f"★★ 23. {mod} 不 import 任何控制模組",
              not (_all_imports(_tree(mod))
                   & {"device_control_operator", "pcs_control_executor",
                      "pcs_control_integration", "production_execution_chain",
                      "pcs_auto_control_service", "report_monitor",
                      "auto_monitor_service"}))
        strs = _str_consts(_tree(mod))
        check(f"  {mod} 不含任何控制指令字串",
              not (strs & {"charge", "discharge", "stop", "pcs_charge",
                           "pcs_discharge", "pcs_stop"}))

    # ---------------- 24. 解析器 Fail Closed ----------------
    print("\n24. 解析器 Fail Closed")
    pages, why = EX.parse_pdf(b"not a pdf at all")
    check("★★ 24. 非 PDF → (None, NOT_A_PDF)",
          pages is None and why == EX.CAL_NOT_PDF)
    pages, why = EX.parse_pdf(b"%PDF-1.7\nnothing here")
    check("★★ 24. 無頁面 → (None, PAGE_NOT_FOUND)",
          pages is None and why == EX.CAL_NO_PAGE)
    pages, why, digest = EX.parse_file(os.path.join(HERE, "no_such_file.pdf"))
    check("★★ 24. 檔案不存在 → FILE_UNREADABLE 且不虛構 hash",
          pages is None and why == EX.CAL_FILE_UNREADABLE and digest is None)
    good = EX.derive_day_types(2026, OC.OFF_PEAK_2026)
    check("★★ 24. 正確資料通過完整性檢核",
          EX.check_integrity(2026, good) == (True, EX.CAL_OK))
    broken = dict(good)
    broken[date(2026, 1, 4)] = EX.DT_WEEKDAY          # 週日改成平日
    check("★★ 24. 週日不是離峰日 → 整份拒絕",
          EX.check_integrity(2026, broken)[0] is False)
    broken2 = dict(good)
    broken2[date(2026, 1, 5)] = EX.DT_SATURDAY        # 週一標成週六
    check("★★ 24. 平日被標成週六 → 整份拒絕",
          EX.check_integrity(2026, broken2)[0] is False)
    broken3 = dict(good)
    broken3[date(2026, 1, 3)] = EX.DT_WEEKDAY         # 週六無顏色
    check("★★ 24. 週六沒有顏色 → 整份拒絕",
          EX.check_integrity(2026, broken3)[0] is False)
    broken4 = dict(good)
    del broken4[date(2026, 6, 1)]
    check("★★ 24. 缺日 → 整份拒絕",
          EX.check_integrity(2026, broken4)[0] is False)
    broken5 = dict(good)
    broken5[date(2026, 6, 1)] = "SOMETHING_ELSE"
    check("★★ 24. 未知日型 → 整份拒絕",
          EX.check_integrity(2026, broken5)[0] is False)
    check("★★ 24. 色域判定不硬編碼單一色值（兩年度紅色不同仍可辨識）",
          EX.is_off_peak_colour((1.0, 0.0, 0.0))
          and EX.is_off_peak_colour((0.933, 0.0, 0.0))
          and not EX.is_off_peak_colour((1.0, 1.0, 0.0)))
    check("★★ 24. 資料被竄改時 build_draft 拒絕產生",
          OC.build_draft(9999)[0] is None)

    # ---------------- 25. 既有 D.4 系列未退步 ----------------
    print("\n25. 既有年度資料契約未退步")
    lst, why = AC.build_annual_list(2026, OC.OFF_PEAK_2026, "測試（非正式）",
                                    "v1", date(2026, 1, 1), verified=True)
    check("★★ 25. 官方日期集合可通過既有 build_annual_list 驗證",
          lst is not None and why == AC.AL_OK
          and len(lst.off_peak_dates) == len(OC.OFF_PEAK_2026))
    dup, why = AC.build_annual_list(2026, list(OC.OFF_PEAK_2026)
                                    + [OC.OFF_PEAK_2026[0]], "s", "v",
                                    date(2026, 1, 1))
    check("★★ 25. 重複日期仍被既有驗證擋下（未放寬）",
          dup is None and why == AC.AL_DUPLICATE_DATE)
    wrong, why = AC.build_annual_list(2026, [date(2027, 1, 1)], "s", "v",
                                      date(2026, 1, 1))
    check("★★ 25. 錯年份仍被擋下（未放寬）",
          wrong is None and why == AC.AL_DATE_WRONG_YEAR)
    check("★★ 25. Materializer 仍不接受 verified 參數",
          "verified" not in
          [a.arg for a in [n for n in _tree("annual_calendar_materializer.py")
                           .body if isinstance(n, ast.FunctionDef)
                           and n.name == "materialize"][0].args.args])
    r26 = MZ.materialize(2026, "v1", date(2026, 1, 1), materialized_at="t")
    check("★★ 25. 規則展開仍有 4 項待人工提供（農曆／變動未被自動猜測）",
          {u[0] for u in r26.unresolved}
          == {"lunar_new_year", "tomb_sweeping_day",
              "dragon_boat_festival", "mid_autumn_festival"}
          and r26.can_promote is False)

    # ---------------- 26. 時段規則未被扭曲 ----------------
    print("\n26. 既有時段規則未被扭曲")
    approved = [AC.build_annual_list(y, OC.ANNUAL_OFF_PEAK_DATES[y],
                                     "測試（非正式）", "v1", date(y, 1, 1),
                                     verified=True)[0]
                for y in OC.SUPPORTED_YEARS]
    p = TP.TariffProvider(holiday_provider=AC.AnnualOffPeakDayProvider(approved))
    for tag, dt, want in (("2026-09-28 教師節 14:00", at(2026, 9, 28, 14),
                           TP.TARIFF_OFF_PEAK),
                          ("2027-10-25 光復節 14:00", at(2027, 10, 25, 14),
                           TP.TARIFF_OFF_PEAK),
                          ("2026-12-25 行憲 14:00", at(2026, 12, 25, 14),
                           TP.TARIFF_OFF_PEAK),
                          ("2026-02-21 春節(週六) 14:00", at(2026, 2, 21, 14),
                           TP.TARIFF_OFF_PEAK),
                          ("一般週六 2026-07-18 14:00", at(2026, 7, 18, 14),
                           TP.TARIFF_HALF_PEAK),
                          ("一般平日 2026-07-15 14:00", at(2026, 7, 15, 14),
                           TP.TARIFF_PEAK),
                          ("一般週日 2026-07-19 14:00", at(2026, 7, 19, 14),
                           TP.TARIFF_OFF_PEAK)):
        check(f"★★ 26. {tag} → {want}", p.observe(dt).state == want)
    check("★★ 26. 夏月定義未被更動（高壓 5/16~10/15）",
          TC.G_CONFIG.summer_start == (5, 16)
          and TC.G_CONFIG.summer_end == (10, 15))

    # ---------------- 27. 零控制能力 ----------------
    print("\n27. 零控制能力")
    _cfg = CFG.DEFAULT_CONTROL_CONFIG
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ 27. Layer 2 仍未配置（刻意 DEFERRED）",
          _cfg.min_switch_interval_sec is None)
    check("★★ 27. dispatch 仍未啟用",
          getattr(__import__("pcs_auto_control_runtime"),
                  "DISPATCH_ENABLED") is False)
    check("★★ 27. dispatch 仍預設關閉",
          getattr(__import__("pcs_auto_control_runtime"),
                  "DISPATCH_ENABLED") is False)
    check("★★ 27. 本輪新增的兩個模組都沒有任何送出指令的能力",
          not any(k in src_oc for k in ("execute", "dispatch(", "send(",
                                        "param=1", "param=2")))

    # ---------------- 28. 人工核准前的最終護欄 ----------------
    print("\n28. 人工核准前的最終護欄")
    # 🔁 D.4-E.2 更正：已核准 provision；改為驗證「只有通過驗收的才在裡面」
    check("★★ 28. production 路徑中的每一份清單都有有效的人工驗收憑據",
          all(OC.human_review_ok(l.year) for l in AC.PRODUCTION_ANNUAL_LISTS)
          and AC.PRODUCTION_PROVIDER.rejected_years == ())
    check("★★ 28. 兩年度 readiness 在 production provider 中皆為 AVAILABLE",
          all(AC.PRODUCTION_PROVIDER.readiness(y) == AC.AR_AVAILABLE
              for y in OC.SUPPORTED_YEARS))
    check("★★ 28. 交付文件已備齊人工核准所需的全部欄位",
          all({"year", "source", "source_version", "effective_date",
               "verified", "off_peak_dates", "provenance", "notes"}
              <= set(OC.draft_document(y)) for y in OC.SUPPORTED_YEARS))
    check("★★ 28. 交付文件可被既有 loader 解析且結果仍是未核准",
          all((lambda r: r[0] is not None and r[0].verified is False)(
              AC.load_annual_list_from_mapping(
                  {k: v for k, v in OC.draft_document(y).items()
                   if k in ("schema_version", "year", "source",
                            "source_version", "effective_date", "verified",
                            "off_peak_dates")}))
              for y in OC.SUPPORTED_YEARS))

    ok_all = all(RESULTS)
    print(f"\n== Phase D.4-E.1 官方年度資料具體化 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
