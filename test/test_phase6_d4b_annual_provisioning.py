# -*- coding: utf-8 -*-
"""
test_phase6_d4b_annual_provisioning.py — Phase D.4-B 年度資料供應與保守判定（1~25）
======================================================================
核心命題
    「保守判定只解決『未知輸入不影響答案』；只要兩種假設結果不同，一律 UNKNOWN。
      年度資料未到位就是未到位，不得沿用其他年度、不得假設非假日。」

不可違反的界線
    1. 保守判定 **不是** best guess / default / fallback / heuristic。
    2. 週六與平日在年度資料未到位時**必須** UNKNOWN。
    3. 年度未提供／未核准／無效 → 一律視同不存在，**不得** fallback 其他年度。
    4. 年度切換（12/31 → 1/1）若新年度未供應 → 受年度資料影響的日期 Fail Closed。
    5. 資料就緒狀態只是回報，**不因此啟用任何控制**。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d4b_annual_provisioning.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import json
import shutil
import tempfile
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tou_calendar as TC                                # noqa: E402
import decision_engine as DE                             # noqa: E402
import decision_policy as DP                             # noqa: E402
import safety_gate as SG                                 # noqa: E402
import power_classifier as PC                            # noqa: E402
import tariff_provider as TP                             # noqa: E402
import annual_off_peak_calendar as AC                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402

RESULTS = []
_TMP = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


TZ, _ = TP.resolve_timezone(TP.PRODUCTION_TIMEZONE_NAME)
SRC = ("測試用來源（非正式）", "test-v1", date(2026, 1, 1))


def at(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


def annual(year=2026, dates=(), verified=True, src=SRC):
    return AC.build_annual_list(year, list(dates), *src, verified=verified)


def provider(lists=(), invalid=None, **kw):
    kw.setdefault("holiday_provider",
                  AC.AnnualOffPeakDayProvider(lists, invalid=invalid))
    return TP.TariffProvider(**kw)


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
    print("== Phase D.4-B 年度資料供應與保守判定 驗證（完全離線）==\n")
    # 🔁 D.4-E.2 更正：production 年度清單已 provision（2026／2027）。
    #    本檔驗證的是「**年度資料未到位**時的保守判定」，因此改用空 Provider ——
    #    契約一字未放寬，只是不能再拿 production provider 當作空清單。
    EMPTY = AC.AnnualOffPeakDayProvider(())
    prod = TP.TariffProvider(holiday_provider=EMPTY)

    # ---------------- 1~3. 保守判定 ----------------
    print("1~3. 保守判定（只在兩種假設一致時才回答）")
    r = prod.observe(at(2026, 7, 19, 14))
    check("★★ 1. 週日 + 年度資料未到位 → OFF_PEAK（唯一答案）",
          r.valid is True and r.state == TP.TARIFF_OFF_PEAK
          and r.reason == TP.TP_OK_DAYTYPE_IRRELEVANT)
    check("★★ 2. 週六 + 年度資料未到位 → UNKNOWN（兩種假設不同）",
          prod.observe(at(2026, 7, 18, 14)).state == TP.TARIFF_UNKNOWN)
    check("★★ 3. 平日 + 年度資料未到位 → UNKNOWN（兩種假設不同）",
          prod.observe(at(2026, 7, 15, 14)).state == TP.TARIFF_UNKNOWN)
    check("★★ 1. production 已啟用保守判定",
          TP.PRODUCTION_RESOLVE_DAYTYPE_IRRELEVANT is True
          and TP.TariffProvider().resolve_when_daytype_irrelevant is True)
    for tag, dt in (("週日 00:00", at(2026, 7, 19, 0)),
                    ("週日 23:59", at(2026, 7, 19, 23, 59)),
                    ("非夏月週日 08:00", at(2026, 12, 13, 8))):
        check(f"  1. {tag} 亦為 OFF_PEAK（全日皆一致）",
              prod.observe(dt).state == TP.TARIFF_OFF_PEAK)

    # ---------------- 6/7. 只在一致時解析 ----------------
    print("\n6/7. 一致才解析、不一致即 UNKNOWN")
    both = TC.classify_tou(at(2026, 7, 19, 14), TC.G_CONFIG,
                           TC.StaticOffPeakDayProvider([date(2026, 7, 19)],
                                                       known_years=[2026]))
    neither = TC.classify_tou(at(2026, 7, 19, 14), TC.G_CONFIG,
                              TC.AssumeNotHolidayProvider())
    check("★★ 6. 週日的兩種假設確實得到相同結果（解析的前提）",
          both.state == neither.state == TP.TARIFF_OFF_PEAK)
    for tag, dt in (("週六", at(2026, 7, 18, 14)), ("平日", at(2026, 7, 15, 14))):
        a = TC.classify_tou(dt, TC.G_CONFIG, TC.StaticOffPeakDayProvider(
            [dt.date()], known_years=[2026])).state
        b = TC.classify_tou(dt, TC.G_CONFIG, TC.AssumeNotHolidayProvider()).state
        check(f"★★ 7. {tag} 的兩種假設不同（{a} vs {b}）→ 必須 UNKNOWN",
              a != b and prod.observe(dt).state == TP.TARIFF_UNKNOWN)
    src = io.open(os.path.join(HERE, "tariff_provider.py"), encoding="utf-8").read()
    check("★★ 6/7. 未使用 assume-not-holiday 作為 fallback（只用於兩假設比對）",
          src.count("AssumeNotHolidayProvider") == 1
          and "_daytype_irrelevant_state" in src)
    check("★★ 保守判定不得成為 best guess / default（結果不同即不回答）",
          prod.observe(at(2026, 7, 15, 14)).valid is False)

    # ---------------- 4/5. 年度離峰日覆蓋 ----------------
    print("\n4/5. 年度離峰日覆蓋")
    sat, _ = annual(2026, [date(2026, 7, 18)])
    check("★★ 4. 年度離峰日落在週六 → 覆蓋 HALF_PEAK 成 OFF_PEAK",
          provider([sat]).observe(at(2026, 7, 18, 14)).state
          == TP.TARIFF_OFF_PEAK)
    wd, _ = annual(2026, [date(2026, 7, 15)])
    check("★★ 5. 年度離峰日落在平日 → 覆蓋 PEAK 成 OFF_PEAK",
          provider([wd]).observe(at(2026, 7, 15, 14)).state
          == TP.TARIFF_OFF_PEAK)
    known, _ = annual(2026, [])
    check("  年度已知且非離峰日 → 週六 HALF_PEAK、平日 PEAK（正常規則）",
          provider([known]).observe(at(2026, 7, 18, 14)).state
          == TP.TARIFF_HALF_PEAK
          and provider([known]).observe(at(2026, 7, 15, 14)).state
          == TP.TARIFF_PEAK)

    # ---------------- 8~10. 年度資料語意 ----------------
    print("\n8~10. 年度資料語意")
    # 🔁 D.4-E.2 更正：清單已 provision，改為驗證「只收經人工驗收的年度」
    check("★★ 8. production 年度清單只含經人工驗收的年度（2026, 2027）",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027)
          and all(l.verified is True for l in AC.PRODUCTION_ANNUAL_LISTS))
    check("★★ 8. 未經驗收的年度不會出現在 production（無 rejected 殘留）",
          AC.PRODUCTION_PROVIDER.rejected_years == ())
    unver, _ = annual(2026, [date(2026, 1, 1)], verified=False)
    pu = AC.AnnualOffPeakDayProvider([unver])
    check("★★ 9. verified=False → 該年度視同不存在 → UNKNOWN",
          pu.is_off_peak_day(date(2026, 1, 1)) is None
          and pu.readiness(2026) == AC.AR_UNVERIFIED
          and provider([unver]).observe(at(2026, 7, 15, 14)).state
          == TP.TARIFF_UNKNOWN)
    only27, _ = annual(2027, [date(2027, 1, 1)])
    p27 = AC.AnnualOffPeakDayProvider([only27])
    check("★★ 10. 只有 2027 資料 → 2026 仍 UNKNOWN（不得 fallback 其他年度）",
          p27.is_off_peak_day(date(2026, 7, 15)) is None
          and p27.readiness(2026) == AC.AR_MISSING
          and p27.readiness(2027) == AC.AR_AVAILABLE)
    check("★★ 10. 2026 的平日在只有 2027 資料時 → UNKNOWN",
          provider([only27]).observe(at(2026, 7, 15, 14)).state
          == TP.TARIFF_UNKNOWN)

    # ---------------- 11. 年度切換 ----------------
    print("\n11. 年度切換 12/31 → 1/1")
    only26, _ = annual(2026, [])
    p = provider([only26])
    e1231 = at(2026, 12, 31, 14)      # 週四
    e0101 = at(2027, 1, 1, 14)        # 週五
    check("  前提：12/31 與 1/1 皆為平日",
          e1231.weekday() < 5 and e0101.weekday() < 5)
    check("★★ 11. 12/31（2026 已供應）→ 可正常判定",
          p.observe(e1231).valid is True
          and p.observe(e1231).state in (TP.TARIFF_PEAK, TP.TARIFF_OFF_PEAK))
    check("★★ 11. 1/1（2027 未供應）→ UNKNOWN，**不得沿用上一年度**",
          p.observe(e0101).state == TP.TARIFF_UNKNOWN
          and p.observe(e0101).valid is False)
    sun0103 = at(2027, 1, 3, 14)
    check("  11. 但跨年後的週日仍可由保守判定得到 OFF_PEAK",
          sun0103.weekday() == 6
          and p.observe(sun0103).state == TP.TARIFF_OFF_PEAK)
    both_yrs = AC.AnnualOffPeakDayProvider([only26, annual(2027, [])[0]])
    check("★★ 11. 補上 2027 清單後 → 1/1 可正常判定",
          TP.TariffProvider(holiday_provider=both_yrs).observe(e0101).valid
          is True)

    # ---------------- 資料就緒狀態 ----------------
    print("\n年度資料就緒狀態（只回報，不啟用任何控制）")
    bad_year = {2025: AC.AL_DATE_WRONG_YEAR}
    pr = AC.AnnualOffPeakDayProvider([only26, unver_2027()], invalid=bad_year)
    rep = pr.readiness_report([2025, 2026, 2027, 2028])
    check("★★ 就緒狀態四態齊備",
          rep == {2025: AC.AR_INVALID, 2026: AC.AR_AVAILABLE,
                  2027: AC.AR_UNVERIFIED, 2028: AC.AR_MISSING})
    check("  只有 AVAILABLE 的年度能判定離峰日",
          pr.is_off_peak_day(date(2026, 1, 1)) is False
          and pr.is_off_peak_day(date(2027, 1, 1)) is None
          and pr.is_off_peak_day(date(2025, 1, 1)) is None
          and pr.is_off_peak_day(date(2028, 1, 1)) is None)
    # 🔁 更正：年度資料就緒與 production 參數就緒都不等於「可以送指令」。
    check("★★ 就緒狀態不影響任何控制能力（dispatch 仍未啟用）",
          RT.DISPATCH_ENABLED is False
          and RT.AutoControlRuntime().dispatch_enabled is False)
    check("  invalid_years / rejected_years 供稽核",
          pr.invalid_years == (2025,) and pr.rejected_years == (2027,))

    # ---------------- provisioning 格式 ----------------
    print("\n人工 provisioning 格式（可 review、可版控、零網路）")
    d = tempfile.mkdtemp(prefix="d4b_")
    _TMP.append(d)
    good = {"schema_version": AC.SCHEMA_VERSION, "year": 2026,
            "source": "測試來源", "source_version": "v1",
            "effective_date": "2026-01-01", "verified": True,
            "off_peak_dates": ["2026-01-01", "2026-02-28"],
            "notes": "測試用"}
    p_ok = os.path.join(d, "annual_off_peak_2026.json")
    io.open(p_ok, "w", encoding="utf-8").write(
        json.dumps(good, ensure_ascii=False, indent=2))
    lst, why = AC.load_annual_list_from_file(p_ok)
    check("★★ 由 JSON 檔載入成功且欄位正確",
          why == AC.AL_OK and lst.year == 2026 and lst.verified is True
          and lst.off_peak_dates == (date(2026, 1, 1), date(2026, 2, 28))
          and lst.source_version == "v1")
    bad_files = [
        ("schema 版本不符", {**good, "schema_version": 99},
         AC.AL_SCHEMA_UNSUPPORTED),
        ("日期格式錯誤", {**good, "off_peak_dates": ["2026-13-01"]},
         AC.AL_DATE_INVALID),
        ("日期非字串", {**good, "off_peak_dates": [20260101]},
         AC.AL_DATE_INVALID),
        ("年份不符", {**good, "off_peak_dates": ["2025-01-01"]},
         AC.AL_DATE_WRONG_YEAR),
        ("重複日期", {**good, "off_peak_dates": ["2026-01-01", "2026-01-01"]},
         AC.AL_DUPLICATE_DATE),
        ("來源缺失", {**good, "source": ""}, AC.AL_SOURCE_MISSING),
        ("生效日格式錯誤", {**good, "effective_date": "2026/01/01"},
         AC.AL_EFFECTIVE_DATE_INVALID),
        ("off_peak_dates 非陣列", {**good, "off_peak_dates": "x"},
         AC.AL_SCHEMA_INVALID),
    ]
    for tag, obj, want in bad_files:
        lst2, why2 = AC.load_annual_list_from_mapping(obj)
        check(f"★★ 載入驗證：{tag} → 拒絕（{want}）",
              lst2 is None and why2 == want)
    io.open(os.path.join(d, "broken.json"), "w", encoding="utf-8").write("{oops")
    check("★★ 檔案無法解析 → ANNUAL_LIST_FILE_UNREADABLE",
          AC.load_annual_list_from_file(
              os.path.join(d, "broken.json"))[1] == AC.AL_FILE_UNREADABLE)
    check("★★ 檔案不存在 → 同樣 Fail Closed（不視為空清單）",
          AC.load_annual_list_from_file(
              os.path.join(d, "nope.json"))[1] == AC.AL_FILE_UNREADABLE)
    draft, why3 = AC.load_annual_list_from_mapping({**good, "verified": False})
    check("★★ verified=false 的檔案可載入但不可用（草稿不得悄悄生效）",
          why3 == AC.AL_OK and draft.usable is False
          and AC.AnnualOffPeakDayProvider([draft]).readiness(2026)
          == AC.AR_UNVERIFIED)
    # 🔁 D.4-E.2 更正：改以 AST 證明「不掃描目錄」，比字串比對更強
    _ac_src = io.open(os.path.join(HERE, "annual_off_peak_calendar.py"),
                      encoding="utf-8").read()
    check("★★ production 不會自動掃描目錄（無 listdir／glob／walk／掃描式載入）",
          not any(k in _ac_src for k in ("listdir", "glob", "os.walk",
                                         "scandir", "iterdir"))
          and not (_all_imports(_tree("annual_off_peak_calendar.py"))
                   & {"glob", "pathlib", "os"}))
    check("★★ production 年度只能由程式碼明確納入（來源為已驗收清單，非目錄）",
          "verified_lists()" in _ac_src)

    # ---------------- 12/13. 時區與零控制 ----------------
    print("\n12/13. 時區與零控制")
    check("★★ 12. production 時區為 Asia/Taipei",
          TP.PRODUCTION_TIMEZONE_NAME == "Asia/Taipei"
          and prod.observe(at(2026, 7, 19, 14)).timezone_name == "Asia/Taipei")
    check("★★ 12. naive 時間仍一律拒絕（保守判定未放寬此規則）",
          TP.TariffProvider(holiday_provider=AC.PRODUCTION_PROVIDER).observe(
              datetime(2026, 7, 19, 14)).reason == TP.TP_NAIVE_DATETIME
          and TP.TariffProvider(holiday_provider=EMPTY).observe(
              datetime(2026, 7, 19, 14)).reason == TP.TP_NAIVE_DATETIME)
    NET = {"requests", "urllib", "urllib3", "http", "socket", "socketio",
           "httpx", "aiohttp"}
    PCS = {"pcs_control_executor", "device_control_operator", "api_client",
           "device_control_scraper", "charge_discharge_report",
           "production_execution_chain", "last_control_store"}
    for mod in ("tariff_provider.py", "annual_off_peak_calendar.py"):
        imps = _all_imports(_tree(mod))
        check(f"★★ 13. {mod} 零網路、零控制路徑",
              not (imps & NET) and not (imps & PCS))
    check("★★ 13. 未使用第三方 holiday 套件",
          not (_all_imports(_tree("annual_off_peak_calendar.py"))
               & {"holidays", "workalendar", "chinese_calendar", "lunardate"}))

    # ---------------- 決策 Fail Closed ----------------
    print("\n決策端 Fail Closed")
    eng = DE.DecisionEngine(policy=DP.TouArbitragePolicy(
        config=DP.PolicyConfig(charge_power_kw=5.0, discharge_power_kw=5.0)))

    class _Grid:
        state, valid = PC.STATE_IMPORT, True

    class _Ess:
        valid, stale, soc_percent = True, False, 30.0

    unk = prod.observe(at(2026, 7, 15, 14))
    check("★★ 平日 UNKNOWN → 決策 no_action",
          eng.decide(DE.DecisionInput(grid=_Grid(), tou=unk,
                                      ess=_Ess())).action
          == DE.ACTION_NO_ACTION)
    sun = prod.observe(at(2026, 7, 19, 3))
    check("★★ 週日 OFF_PEAK（保守判定）→ 決策可產生動作",
          sun.state == TP.TARIFF_OFF_PEAK
          and eng.decide(DE.DecisionInput(grid=_Grid(), tou=sun,
                                          ess=_Ess())).action
          != DE.ACTION_NO_ACTION)
    check("★★ 但仍不可能送出任何指令（dispatch 恆為 False）",
          RT.DISPATCH_ENABLED is False)

    # ---------------- 既有模組未被修改 ----------------
    print("\n既有模組未被修改")
    check("★★ tou_calendar 未被修改（規則與介面沿用）",
          issubclass(AC.AnnualOffPeakDayProvider, TC.HolidayProvider)
          and "G_RULES = " not in src and "TC.classify_tou" in src)
    for dt in (at(2026, 7, 15, 14), at(2026, 7, 18, 14), at(2026, 12, 10, 8)):
        hp = AC.AnnualOffPeakDayProvider([known])
        want = TC.classify_tou(dt, TC.G_CONFIG, hp)
        got = TP.TariffProvider(holiday_provider=hp).observe(dt)
        check(f"  與既有規則一致：{dt.date()} → {want.state}",
              (got.state, got.season, got.day_type)
              == (want.state, want.season, want.day_type))
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ 模組預設仍未配置，且 dispatch 未啟用",
          SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None
          and RT.DISPATCH_ENABLED is False)

    for x in _TMP:
        shutil.rmtree(x, ignore_errors=True)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.4-B 年度資料供應與保守判定 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def unver_2027():
    lst, _ = AC.build_annual_list(2027, [date(2027, 1, 1)], *SRC, verified=False)
    return lst


if __name__ == "__main__":
    raise SystemExit(main())
