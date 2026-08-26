# -*- coding: utf-8 -*-
"""
test_phase6_d4a_annual_calendar.py — Phase D.4-A 年度離峰日與時區接線（1~30）
======================================================================
核心命題
    「離峰日的答案必須來自**經核准、可追溯、版本控管**的年度清單；
      年度未知就是未知，絕不推論成『不是假日』。」

不可違反的界線
    1. 年度未知 / 未核准 / 資料無效 → None → 時段 UNKNOWN → 決策 Fail Closed。
    2. 不使用任何外部服務，不自行產生 production 日期。
    3. 時區一律以 IANA 名稱交由時區資料庫解析，naive 時間拒絕。
    4. 電價規則完全沿用既有模組，未修改。
    5. 零控制能力：不送出、不寫紀錄、dispatch 恆為 0。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d4a_annual_calendar.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
from datetime import datetime, date, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tou_calendar as TC                                # noqa: E402
import power_classifier as PC                            # noqa: E402
import decision_engine as DE                             # noqa: E402
import decision_policy as DP                             # noqa: E402
import safety_gate as SG                                 # noqa: E402
import tariff_provider as TP                             # noqa: E402
import annual_off_peak_calendar as AC                    # noqa: E402
import official_annual_calendar as OC                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


TZ, _WHY = TP.resolve_timezone(TP.PRODUCTION_TIMEZONE_NAME)
SRC = ("測試用來源（非正式）", "test-v1", date(2026, 1, 1))


def at(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


def annual(year=2026, dates=(), verified=True):
    lst, why = AC.build_annual_list(year, list(dates), *SRC, verified=verified)
    return lst, why


def provider(lists=(), **kw):
    kw.setdefault("timezone_name", TP.PRODUCTION_TIMEZONE_NAME)
    kw.setdefault("holiday_provider", AC.AnnualOffPeakDayProvider(lists))
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
    print("== Phase D.4-A 年度離峰日與時區接線 驗證（完全離線）==\n")

    # ---------------- 1. Asia/Taipei production wiring ----------------
    print("1. Asia/Taipei production 接線")
    check("★★ 1. production 時區已正式設為 Asia/Taipei",
          TP.PRODUCTION_TIMEZONE_NAME == "Asia/Taipei"
          and TP.TariffProvider().timezone_name == "Asia/Taipei")
    check("★★ 1. 時區以資料庫解析成功（未做固定時差運算）",
          TZ is not None and _WHY == TP.TP_OK
          and "timedelta" not in _all_imports(_tree("tariff_provider.py")))
    lst, _ = annual(2026, [date(2026, 1, 1)])
    o = provider([lst]).observe(at(2026, 7, 15, 14))
    check("★★ 1. 判定結果帶出明確時區與偏移",
          o.timezone_name == "Asia/Taipei" and o.utc_offset == "8:00:00"
          and o.local_datetime.endswith("+08:00"))
    check("★★ 1. 接線後 dispatch 仍為 False（時區核准 ≠ 啟用送出）",
          RT.DISPATCH_ENABLED is False
          and RT.AutoControlRuntime().dispatch_enabled is False)

    # ---------------- 2/3. 年度清單語意 ----------------
    print("\n2/3. 年度清單語意")
    lst26, why = annual(2026, [date(2026, 1, 1), date(2026, 2, 28)])
    check("★★ 2. 已核准年度 → 明確回答 True / False",
          why == AC.AL_OK
          and AC.AnnualOffPeakDayProvider([lst26]).is_off_peak_day(
              date(2026, 1, 1)) is True
          and AC.AnnualOffPeakDayProvider([lst26]).is_off_peak_day(
              date(2026, 3, 1)) is False)
    p26 = AC.AnnualOffPeakDayProvider([lst26])
    check("★★ 3. 年度未知 → None（**不是** False）",
          p26.is_off_peak_day(date(2027, 1, 1)) is None
          and p26.known_years == (2026,))
    unver, _ = annual(2027, [date(2027, 1, 1)], verified=False)
    pun = AC.AnnualOffPeakDayProvider([lst26, unver])
    check("★★ 3. 未核准的清單視同不存在（年度仍為未知）",
          pun.is_off_peak_day(date(2027, 1, 1)) is None
          and pun.known_years == (2026,) and pun.rejected_years == (2027,))
    check("  非日期輸入 → None（不猜測）",
          p26.is_off_peak_day("2026-01-01") is None
          and p26.is_off_peak_day(None) is None
          and p26.is_off_peak_day(True) is None)

    # ---------------- 4~8. 資料完整性 ----------------
    print("\n4~8. 年度資料完整性（任何問題一律拒絕建立）")
    bad_cases = [
        ("4. 年份非整數", dict(year="2026", dates=[]), AC.AL_YEAR_INVALID),
        ("4. 年份超出範圍", dict(year=1800, dates=[]), AC.AL_YEAR_INVALID),
        ("5. 重複日期",
         dict(year=2026, dates=[date(2026, 1, 1), date(2026, 1, 1)]),
         AC.AL_DUPLICATE_DATE),
        ("6. 非 date 物件", dict(year=2026, dates=["2026-01-01"]),
         AC.AL_DATE_INVALID),
        ("6. bool 混入", dict(year=2026, dates=[True]), AC.AL_DATE_INVALID),
        ("7. 年份不一致", dict(year=2026, dates=[date(2025, 12, 31)]),
         AC.AL_DATE_WRONG_YEAR),
    ]
    for tag, kw, want in bad_cases:
        lst, why = AC.build_annual_list(kw["year"], kw["dates"], *SRC,
                                        verified=True)
        check(f"★★ {tag} → 拒絕建立（{want}）", lst is None and why == want)
    for tag, args in (("來源缺失", ("", "v1", date(2026, 1, 1))),
                      ("版本缺失", ("s", "  ", date(2026, 1, 1))),
                      ("生效日非 date", ("s", "v1", "2026-01-01"))):
        lst, why = AC.build_annual_list(2026, [], *args, verified=True)
        check(f"★★ 8. {tag} → 拒絕建立",
              lst is None and why in (AC.AL_SOURCE_MISSING,
                                      AC.AL_EFFECTIVE_DATE_INVALID))
    leap, why = AC.build_annual_list(2028, [date(2028, 2, 29)], *SRC,
                                     verified=True)
    check("★★ 8. 閏年 2/29 為合法日期",
          leap is not None and why == AC.AL_OK
          and AC.AnnualOffPeakDayProvider([leap]).is_off_peak_day(
              date(2028, 2, 29)) is True)
    try:
        date(2026, 2, 29)
        leap_bad = False
    except ValueError:
        leap_bad = True
    check("  非閏年 2/29 無法構成 date（型別層即擋下）", leap_bad)
    empty, why = annual(2026, [])
    check("★★ 8. 空清單合法但明確標記（必須是刻意核准的宣告）",
          empty is not None and why == AC.AL_OK and empty.is_empty is True
          and AC.AnnualOffPeakDayProvider([empty]).is_off_peak_day(
              date(2026, 1, 1)) is False)
    shuffled, _ = AC.build_annual_list(
        2026, [date(2026, 5, 1), date(2026, 1, 1), date(2026, 3, 1)], *SRC,
        verified=True)
    check("★★ 8. 排序具決定性（輸入順序不影響結果）",
          shuffled.off_peak_dates == (date(2026, 1, 1), date(2026, 3, 1),
                                      date(2026, 5, 1)))
    check("★★ 8. source metadata 完整保留且可序列化",
          shuffled.source == SRC[0] and shuffled.source_version == SRC[1]
          and shuffled.effective_date == SRC[2]
          and isinstance(shuffled.as_dict(), dict))

    # ---------------- 9~11. 日型行為 ----------------
    print("\n9~11. 日型行為")
    known, _ = annual(2026, [])                       # 已核准且無離峰日
    p = provider([known])
    check("★★ 9. 週日（年度已知）→ OFF_PEAK",
          p.observe(at(2026, 7, 19, 14)).state == TP.TARIFF_OFF_PEAK)
    check("★★ 10. 週六（年度已知）→ HALF_PEAK（未併入 OFF_PEAK）",
          p.observe(at(2026, 7, 18, 14)).state == TP.TARIFF_HALF_PEAK
          and TP.TARIFF_HALF_PEAK != TP.TARIFF_OFF_PEAK)
    sat_opd, _ = annual(2026, [date(2026, 7, 18)])    # 離峰日剛好落在週六
    check("★★ 11. 年度離峰日落在週六 → 覆蓋 HALF_PEAK 成 OFF_PEAK",
          provider([sat_opd]).observe(at(2026, 7, 18, 14)).state
          == TP.TARIFF_OFF_PEAK)
    wd_opd, _ = annual(2026, [date(2026, 7, 15)])
    check("★★ 11. 年度離峰日落在平日 → 全日 OFF_PEAK（優先於星期）",
          provider([wd_opd]).observe(at(2026, 7, 15, 14)).state
          == TP.TARIFF_OFF_PEAK)
    check("★★ 12. 平日（非離峰日）→ 依時段表（夏月 14:00 為 PEAK）",
          p.observe(at(2026, 7, 15, 14)).state == TP.TARIFF_PEAK)

    # ---------------- 13/14. 季節邊界 ----------------
    print("\n13/14. 夏月／非夏月邊界")
    for tag, dt, want_season in (("13. 夏月末 10/15", at(2026, 10, 15, 12),
                                  TC.SEASON_SUMMER),
                                 ("14. 非夏月首日 10/16", at(2026, 10, 16, 12),
                                  TC.SEASON_NON_SUMMER),
                                 ("13. 夏月首日 5/16", at(2026, 5, 16, 12),
                                  TC.SEASON_SUMMER),
                                 ("14. 非夏月末 5/15", at(2026, 5, 15, 12),
                                  TC.SEASON_NON_SUMMER)):
        check(f"★★ {tag} → season={want_season}",
              p.observe(dt).season == want_season)
    check("★★ 14. 非夏月平日 12:00 為 OFF_PEAK（11-14 離峰帶）",
          p.observe(at(2026, 12, 10, 12)).state == TP.TARIFF_OFF_PEAK)

    # ---------------- 15/16. 時間契約 ----------------
    print("\n15/16. 時區與時間契約")
    t0 = at(2026, 7, 15, 14)
    check("★★ 15. UTC 表示的同一瞬間 → 相同結果",
          p.observe(t0.astimezone(timezone.utc)).state == p.observe(t0).state
          and p.observe(t0.astimezone(timezone.utc)).local_datetime
          == p.observe(t0).local_datetime)
    check("★★ 16. naive 時間 → Fail Closed",
          (lambda r: r.valid is False and r.state == TP.TARIFF_UNKNOWN
           and r.reason == TP.TP_NAIVE_DATETIME)(
              p.observe(datetime(2026, 7, 15, 14))))
    for bad in ("Not/AZone", "", 123):
        r = TP.TariffProvider(timezone_name=bad,
                              holiday_provider=AC.AnnualOffPeakDayProvider(
                                  [known])).observe()
        check(f"★★ 16. 時區無效（{bad!r}）→ Fail Closed",
              r.valid is False and r.state == TP.TARIFF_UNKNOWN)
    check("★★ 16. 時區資料庫不可用時亦有明確 Fail Closed 路徑",
          TP.TP_TIMEZONE_UNAVAILABLE in io.open(
              os.path.join(HERE, "tariff_provider.py"), encoding="utf-8").read())

    # ---------------- 17. UNKNOWN Fail Closed ----------------
    print("\n17. 年度未知 → 全鏈 Fail Closed")
    # 🔁 D.4-E.2 更正：production 年度清單已 provision（2026／2027），
    #    「年度資料未到位」的行為改用**空的** Provider 驗證 ——
    #    契約本身完全沒有放寬，只是不能再用 production provider 當作空清單。
    EMPTY = AC.AnnualOffPeakDayProvider(())
    prod = TP.TariffProvider(holiday_provider=EMPTY)
    # ⚠️ D.4-B 起 production 已核准啟用保守判定：
    #    週日的兩種假設結果相同（皆為 OFF_PEAK）→ 可判定；
    #    週六與平日兩種假設結果不同 → 仍必須 UNKNOWN。
    for tag, dt in (("週六", at(2026, 7, 18, 14)), ("平日", at(2026, 7, 15, 14))):
        r = prod.observe(dt)
        check(f"★★ 17. production（無年度清單）{tag} → UNKNOWN",
              r.state == TP.TARIFF_UNKNOWN and r.valid is False
              and r.state != TP.TARIFF_OFF_PEAK)
    check("★★ 17. 週日則因兩種假設一致而可判定（非 fallback、非猜測）",
          prod.observe(at(2026, 7, 19, 14)).reason
          == TP.TP_OK_DAYTYPE_IRRELEVANT)
    unk = prod.observe(at(2026, 7, 15, 14))
    eng = DE.DecisionEngine(policy=DP.TouArbitragePolicy(
        config=DP.PolicyConfig(charge_power_kw=5.0, discharge_power_kw=5.0)))

    class _Grid:
        state, valid = PC.STATE_IMPORT, True

    class _Ess:
        valid, stale, soc_percent = True, False, 30.0

    check("★★ 17. 時段 UNKNOWN → 決策 no_action（Fail Closed）",
          eng.decide(DE.DecisionInput(grid=_Grid(), tou=unk,
                                      ess=_Ess())).action
          == DE.ACTION_NO_ACTION)
    ok = provider([known]).observe(at(2026, 7, 15, 3))
    check("  對照：年度已知且為離峰 → 決策可產生動作",
          ok.state == TP.TARIFF_OFF_PEAK
          and eng.decide(DE.DecisionInput(grid=_Grid(), tou=ok,
                                          ess=_Ess())).action
          != DE.ACTION_NO_ACTION)
    # 🔁 D.4-E.2 更正：2026 已 provision，改用**未 provision 的年度**驗證同一契約
    check("★★ 17. 禁止『不是週末所以當平日』的推論（未核准年度一律 None）",
          EMPTY.is_off_peak_day(date(2026, 7, 15)) is None
          and AC.PRODUCTION_PROVIDER.is_off_peak_day(date(2028, 7, 12)) is None)

    # ---------------- 週日可判定性（預設關閉的選項）----------------
    print("\n週日可判定性（D.4-B 已核准於 production 啟用）")
    check("★★ production 已核准啟用保守判定（D.4-B）",
          TP.PRODUCTION_RESOLVE_DAYTYPE_IRRELEVANT is True
          and TP.TariffProvider(holiday_provider=AC.PRODUCTION_PROVIDER
                                ).resolve_when_daytype_irrelevant is True)
    # 🔁 D.4-E.2 更正：改用空 Provider（年度資料未到位）驗證保守判定的開關語意
    check("★★ 明確關閉時 → 回到純 Fail Closed（週日亦為 UNKNOWN）",
          TP.TariffProvider(holiday_provider=EMPTY,
                            resolve_when_daytype_irrelevant=False
                            ).observe(at(2026, 7, 19, 14)).state
          == TP.TARIFF_UNKNOWN)
    opt = TP.TariffProvider(holiday_provider=EMPTY,
                            resolve_when_daytype_irrelevant=True)
    check("★★ 週日可判定為 OFF_PEAK（兩種假設結果相同）",
          (lambda r: r.valid is True and r.state == TP.TARIFF_OFF_PEAK
           and r.reason == TP.TP_OK_DAYTYPE_IRRELEVANT)(
              opt.observe(at(2026, 7, 19, 14))))
    for tag, dt in (("週六", at(2026, 7, 18, 14)), ("平日", at(2026, 7, 15, 14))):
        check(f"★★ {tag} 仍為 UNKNOWN（兩種假設結果不同，不得回答）",
              opt.observe(dt).state == TP.TARIFF_UNKNOWN)

    # ---------------- 18. 維度分離 ----------------
    print("\n18. Tariff / Grid 維度分離")
    check("★★ 18. 時段值域不含任何潮流／控制狀態",
          not (TP.TARIFF_STATES & TP.FORBIDDEN_IN_TARIFF)
          and TP.TARIFF_STATES == {"PEAK", "HALF_PEAK", "OFF_PEAK", "UNKNOWN"})
    for mod in ("tariff_provider.py", "annual_off_peak_calendar.py"):
        check(f"★★ 18. {mod} 未 import 電表／分類模組",
              not (_all_imports(_tree(mod))
                   & {"meter_client", "power_classifier",
                      "meter_observation_adapter", "ess_snapshot_adapter"}))

    # ---------------- 19. 零控制能力 ----------------
    print("\n19. 零控制能力與無外部服務")
    PCS_PATH = {"pcs_control_executor", "device_control_operator",
                "device_control_scraper", "api_client", "requests",
                "charge_discharge_report", "production_execution_chain",
                "last_control_store"}
    NET = {"requests", "urllib", "urllib3", "http", "socket", "socketio",
           "httpx", "aiohttp"}
    for mod in ("tariff_provider.py", "annual_off_peak_calendar.py"):
        imps = _all_imports(_tree(mod))
        check(f"★★ 19. {mod} 零控制路徑（命中={sorted(imps & PCS_PATH)}）",
              not (imps & PCS_PATH))
        check(f"★★ 19. {mod} 零網路相依（命中={sorted(imps & NET)}）",
              not (imps & NET))
        names = ({n.id for n in ast.walk(_tree(mod)) if isinstance(n, ast.Name)}
                 | {n.attr for n in ast.walk(_tree(mod))
                    if isinstance(n, ast.Attribute)})
        # ⚠️ 不可把 `get` 列入 —— 那是 dict.get() 的映射查詢，不是 HTTP GET。
        #    只比對真正代表「送出」或「寫入」的名稱。
        check(f"  {mod} 無送出／寫入呼叫",
              not (names & {"send", "post", "put", "request", "urlopen",
                            "update_from_verified_result", "save",
                            "_atomic_write", "execute_and_verify"}))
    check("★★ 19. 未使用任何第三方 holiday 套件",
          not (_all_imports(_tree("annual_off_peak_calendar.py"))
               & {"holidays", "workalendar", "chinese_calendar", "lunardate"}))
    # 🔁 D.4-E.2 更正：清單已 provision；契約改為「本檔不自行產生任何日期」
    check("★★ 19. production 清單的日期完全來自官方年度日曆資料集（非本檔自產）",
          all(tuple(l.off_peak_dates) == OC.ANNUAL_OFF_PEAK_DATES[l.year]
              for l in AC.PRODUCTION_ANNUAL_LISTS))
    check("★★ 19. 每份 production 清單都指向官方日曆表並載明人工驗收",
          all("官方年度時間電價日曆表" in l.source
              and "人工驗收" in l.source_version
              for l in AC.PRODUCTION_ANNUAL_LISTS))
    check("★★ 19. production 已 provision 的年度精確為 (2026, 2027)",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027))

    # ---------------- 既有模組未被修改 ----------------
    print("\n既有模組未被修改")
    for dt in (at(2026, 7, 15, 14), at(2026, 7, 18, 14), at(2026, 7, 19, 14),
               at(2026, 12, 10, 8)):
        want = TC.classify_tou(dt, config=TC.G_CONFIG,
                               holiday_provider=AC.AnnualOffPeakDayProvider(
                                   [known]))
        got = provider([known]).observe(dt)
        check(f"★★ 與既有規則逐欄一致：{dt.date()} {dt.hour:02d}:00 → {want.state}",
              (got.state, got.season, got.day_type)
              == (want.state, want.season, want.day_type))
    src = io.open(os.path.join(HERE, "tariff_provider.py"), encoding="utf-8").read()
    check("★★ Provider 未重寫任何時段表",
          "G_RULES = " not in src and "SUMMER_START =" not in src
          and "TC.classify_tou" in src)
    check("★★ 年度清單模組實作既有 HolidayProvider 介面（未另建第二套）",
          issubclass(AC.AnnualOffPeakDayProvider, TC.HolidayProvider))
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ dispatch 仍未啟用，且各模組預設仍未配置",
          RT.DISPATCH_ENABLED is False
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.4-A 年度離峰日與時區接線 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
