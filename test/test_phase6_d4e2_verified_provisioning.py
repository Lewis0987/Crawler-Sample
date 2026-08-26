# -*- coding: utf-8 -*-
"""
test_phase6_d4e2_verified_provisioning.py — Phase D.4-E.2 已驗收年度資料上線
======================================================================
核心命題
    「verified=true 的**唯一**依據是：官方 Calendar provenance ＋ 官方檔案
      SHA-256 ＋ 本次人工驗收。三者缺一不可，且**綁資料**ïƒ 改一天就失效。」

本階段允許的事只有一件：把**已經人工驗收**的 2026／2027 年度清單
升為 verified=true 並 provision 到 PRODUCTION_ANNUAL_LISTS。

明令禁止（每一項都有對應斷言）
    · runtime 自動下載 Calendar     · 任何 runtime 網路相依
    · 自動掃描目錄加入年度          · 未來年度自動 fallback
    · 2027 資料沿用到 2028          · 自行產生 2028 日期
    · 第三方 Calendar fallback
    · 因「測試 PASS／fixture／搜尋摘要／程式自行推導」而 verified=true

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d4e2_verified_provisioning.py        # exit 0 = PASS
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
import official_annual_calendar as OC                    # noqa: E402
import taipower_calendar_extractor as EX                 # noqa: E402
import decision_engine as DE                             # noqa: E402
import decision_policy as DP                             # noqa: E402
import power_classifier as PC                            # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


TZ, _ = TP.resolve_timezone(TP.PRODUCTION_TIMEZONE_NAME)


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


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


PROD = TP.TariffProvider(holiday_provider=AC.PRODUCTION_PROVIDER)


# ======================================================================
def main():
    print("== Phase D.4-E.2 已驗收年度資料上線 驗證（完全離線）==\n")

    # ---------------- 1. verified 狀態 ----------------
    print("1. verified 狀態")
    lists = {l.year: l for l in AC.PRODUCTION_ANNUAL_LISTS}
    for y, cnt in ((2026, 15), (2027, 14)):
        check(f"★★ 1. {y} 已升為 verified=True",
              y in lists and lists[y].verified is True)
        check(f"★★ 1. {y} usable=True（可被 Provider 採用）",
              lists[y].usable is True)
        check(f"★★ 1. {y} 日期筆數為 {cnt}（與人工驗收清單一致）",
              len(lists[y].off_peak_dates) == cnt)
        check(f"  {y} 日期內容與官方資料集完全相同",
              tuple(lists[y].off_peak_dates) == OC.ANNUAL_OFF_PEAK_DATES[y])

    # ---------------- 2. verified evidence ----------------
    print("\n2. verified 的依據（provenance + SHA-256 + 人工驗收）")
    for y in OC.SUPPORTED_YEARS:
        rec = OC.human_review(y)
        check(f"★★ 2. {y} 有人工驗收紀錄且 approved=True",
              rec is not None and rec.approved is True)
        check(f"★★ 2. {y} 驗收綁定官方檔案 SHA-256",
              rec.source_sha256 == OC.PRIMARY_SOURCE.sha256
              and len(rec.source_sha256) == 64)
        check(f"★★ 2. {y} 驗收綁定 dataset digest",
              rec.dataset_digest == OC.DATASET_DIGEST[y])
        check(f"★★ 2. {y} human_review_state 為 OK",
              OC.human_review_state(y) == OC.HR_OK)
        check(f"  {y} 清單 source_version 同時載明官方檔案與人工驗收日",
              "官方年度時間電價日曆表" in lists[y].source
              and "人工驗收" in lists[y].source_version
              and rec.reviewed_on in lists[y].source_version)
    check("★★ 2. 2027 驗收另綁第二份官方檔案的 SHA-256",
          OC.human_review(2027).corroborating_sha256
          == OC.CORROBORATING_SOURCE_2027.sha256)
    check("★★ 2. 2026 無第二來源 → corroborating_sha256 為 None（不虛構）",
          OC.human_review(2026).corroborating_sha256 is None)

    # ---------------- 3. verified 不得自動取得 ----------------
    print("\n3. verified 不得因測試／fixture／推導而自動成立")
    check("★★ 3. 未驗收年度取不到 verified 清單",
          OC.build_verified_list(2028) == (None, OC.HR_NOT_REVIEWED))
    approved_only = [f for f, _ in [
        (fn.name, kw)
        for fn in ast.walk(_tree("official_annual_calendar.py"))
        if isinstance(fn, ast.FunctionDef)
        for c in ast.walk(fn) if isinstance(c, ast.Call)
        for kw in c.keywords
        if kw.arg == "verified" and isinstance(kw.value, ast.Constant)
        and kw.value.value is True]]
    check("★★ 3. 全模組只有 build_verified_list 會傳 verified=True",
          approved_only == ["build_verified_list"])
    fn = [n for n in _tree("official_annual_calendar.py").body
          if isinstance(n, ast.FunctionDef) and n.name == "build_verified_list"][0]
    check("★★ 3. build_verified_list 參數只有 year（外部無法要求跳過驗收）",
          [a.arg for a in fn.args.args] == ["year"]
          and fn.args.kwonlyargs == [])
    check("★★ 3. build_draft 仍恆為草稿（不因上線而改變）",
          all(OC.build_draft(y)[0].verified is False
              for y in OC.SUPPORTED_YEARS))

    # ---- 驗收綁資料：資料一變，驗收立即失效（Fail Closed）----
    print("\n3b. 驗收綁資料 —— 改一天即失效")
    _orig_dates = OC.ANNUAL_OFF_PEAK_DATES[2026]
    _orig_digest = OC.DATASET_DIGEST[2026]
    _orig_sha = OC.PRIMARY_SOURCE.sha256
    try:
        OC.ANNUAL_OFF_PEAK_DATES[2026] = _orig_dates[:-1]
        check("★★ 3b. 少一天 → human_review_state = DATA_CHANGED",
              OC.human_review_state(2026) == OC.HR_DATA_CHANGED)
        check("★★ 3b. 且拿不到 verified 清單（Fail Closed）",
              OC.build_verified_list(2026)[0] is None)
        check("★★ 3b. verified_lists() 直接不含該年度（不會以未驗證形式混入）",
              2026 not in {l.year for l in OC.verified_lists()})
    finally:
        OC.ANNUAL_OFF_PEAK_DATES[2026] = _orig_dates
    check("  還原後驗收恢復有效", OC.human_review_state(2026) == OC.HR_OK)
    try:
        OC.PRIMARY_SOURCE.sha256 = "0" * 64
        check("★★ 3b. 來源檔案 hash 不符 → SOURCE_CHANGED",
              OC.human_review_state(2026) == OC.HR_SOURCE_CHANGED
              and OC.build_verified_list(2026)[0] is None)
    finally:
        OC.PRIMARY_SOURCE.sha256 = _orig_sha
    check("  還原後驗收恢復有效", OC.human_review_state(2026) == OC.HR_OK)
    try:
        OC.DATASET_DIGEST[2026] = "f" * 64
        check("★★ 3b. digest 與實際資料不符 → 拒絕升級",
              OC.build_verified_list(2026)[0] is None)
    finally:
        OC.DATASET_DIGEST[2026] = _orig_digest
    check("  還原後驗收恢復有效",
          OC.human_review_state(2026) == OC.HR_OK
          and OC.build_verified_list(2026)[0].verified is True)

    # ---------------- 4/5. PRODUCTION 清單 ----------------
    print("\n4/5. PRODUCTION_ANNUAL_LISTS 與 known_years")
    check("★★ 4. PRODUCTION_ANNUAL_LISTS 恰含兩份清單",
          len(AC.PRODUCTION_ANNUAL_LISTS) == 2)
    check("★★ 4. 兩份皆 verified=True 且皆有有效人工驗收",
          all(l.verified and OC.human_review_ok(l.year)
              for l in AC.PRODUCTION_ANNUAL_LISTS))
    check("★★ 5. PRODUCTION_PROVIDER.known_years 精確為 (2026, 2027)",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027))
    check("★★ 5. 沒有任何被拒絕／無效的年度殘留",
          AC.PRODUCTION_PROVIDER.rejected_years == ()
          and AC.PRODUCTION_PROVIDER.invalid_years == ())
    check("★★ 5. provisioning_report 兩年度皆 verified",
          all(v["verified"] is True and v["human_review"] == OC.HR_OK
              for v in OC.provisioning_report().values()))

    # ---------------- 6~9. readiness ----------------
    print("\n6~9. 年度就緒狀態")
    for y, want, tag in ((2025, AC.AR_MISSING, "6"), (2026, AC.AR_AVAILABLE, "7"),
                         (2027, AC.AR_AVAILABLE, "8"),
                         (2028, AC.AR_MISSING, "9")):
        check(f"★★ {tag}. {y} readiness = {want}",
              AC.PRODUCTION_PROVIDER.readiness(y) == want)
    check("  readiness_report 一次檢視四個年度",
          AC.PRODUCTION_PROVIDER.readiness_report([2025, 2026, 2027, 2028])
          == {2025: AC.AR_MISSING, 2026: AC.AR_AVAILABLE,
              2027: AC.AR_AVAILABLE, 2028: AC.AR_MISSING})

    # ---------------- 10. 2026 代表性判定 ----------------
    print("\n10. 2026 代表性時段判定")
    for tag, dt in (("01/01", at(2026, 1, 1, 14)),
                    ("09/28", at(2026, 9, 28, 14)),
                    ("12/25", at(2026, 12, 25, 14))):
        r = PROD.observe(dt)
        check(f"★★ 10. 2026-{tag} → OFF_PEAK",
              r.state == TP.TARIFF_OFF_PEAK and r.valid is True)
    check("  2026 春節整段 02/16~02/21 皆 OFF_PEAK",
          all(PROD.observe(at(2026, 2, d, 14)).state == TP.TARIFF_OFF_PEAK
              for d in range(16, 22)))
    check("  2026 端午 06/19、中秋 09/25 皆 OFF_PEAK",
          PROD.observe(at(2026, 6, 19, 14)).state == TP.TARIFF_OFF_PEAK
          and PROD.observe(at(2026, 9, 25, 14)).state == TP.TARIFF_OFF_PEAK)

    # ---------------- 11. 2027 代表性判定 ----------------
    print("\n11. 2027 代表性時段判定")
    for tag, dt in (("02/04", at(2027, 2, 4, 14)),
                    ("02/10", at(2027, 2, 10, 14)),
                    ("04/05", at(2027, 4, 5, 14)),
                    ("09/28", at(2027, 9, 28, 14)),
                    ("10/25", at(2027, 10, 25, 14)),
                    ("12/25", at(2027, 12, 25, 14))):
        r = PROD.observe(dt)
        check(f"★★ 11. 2027-{tag} → OFF_PEAK",
              r.state == TP.TARIFF_OFF_PEAK and r.valid is True)
    check("  2027-02-03（春節前一日）與 02-11（初六）**不是** OFF_PEAK",
          PROD.observe(at(2027, 2, 3, 14)).state != TP.TARIFF_OFF_PEAK
          and PROD.observe(at(2027, 2, 11, 14)).state != TP.TARIFF_OFF_PEAK)

    # ---------------- 12~14. 一般日型 ----------------
    print("\n12~14. 一般週日／週六／平日")
    check("★★ 12. Sunday → OFF_PEAK（2026-07-19）",
          PROD.observe(at(2026, 7, 19, 14)).state == TP.TARIFF_OFF_PEAK)
    check("★★ 12. Sunday → OFF_PEAK（2027-07-18）",
          PROD.observe(at(2027, 7, 18, 14)).state == TP.TARIFF_OFF_PEAK)
    check("★★ 13. normal Saturday → HALF_PEAK（2026-07-18）",
          PROD.observe(at(2026, 7, 18, 14)).state == TP.TARIFF_HALF_PEAK)
    check("★★ 13. normal Saturday → HALF_PEAK（2027-07-17）",
          PROD.observe(at(2027, 7, 17, 14)).state == TP.TARIFF_HALF_PEAK)
    check("★★ 14. normal weekday → PEAK（2026-07-15 夏月 14:00）",
          PROD.observe(at(2026, 7, 15, 14)).state == TP.TARIFF_PEAK)
    check("★★ 14. normal weekday → PEAK（2027-07-14 夏月 14:00）",
          PROD.observe(at(2027, 7, 14, 14)).state == TP.TARIFF_PEAK)
    check("  離峰日蓋過週六（2026-02-28 週六 → OFF_PEAK，非 HALF_PEAK）",
          date(2026, 2, 28).weekday() == 5
          and PROD.observe(at(2026, 2, 28, 14)).state == TP.TARIFF_OFF_PEAK)
    check("  平日離峰時段仍為 OFF_PEAK（2026-07-15 03:00）",
          PROD.observe(at(2026, 7, 15, 3)).state == TP.TARIFF_OFF_PEAK)

    # ---------------- 15/16. 時區 ----------------
    print("\n15/16. 時區與 naive datetime")
    check("★★ 15. production 時區為 Asia/Taipei（IANA 名稱，非固定時差）",
          TP.PRODUCTION_TIMEZONE_NAME == "Asia/Taipei"
          and PROD.observe(at(2026, 9, 28, 14)).timezone_name == "Asia/Taipei")
    check("★★ 15. aware datetime 正常判定",
          PROD.observe(at(2026, 9, 28, 14)).valid is True)
    check("★★ 16. naive datetime 仍一律 Fail Closed",
          (lambda r: r.valid is False and r.state == TP.TARIFF_UNKNOWN
           and r.reason == TP.TP_NAIVE_DATETIME)(
              PROD.observe(datetime(2026, 9, 28, 14))))
    check("★★ 16. 已 provision 的離峰日用 naive 時間一樣被拒絕（無例外）",
          PROD.observe(datetime(2027, 10, 25, 14)).reason
          == TP.TP_NAIVE_DATETIME)
    check("  跨時區的 aware 時間換算到台北後判定（不看原始時區數字）",
          PROD.observe(datetime(
              2026, 9, 28, 6, tzinfo=TP.resolve_timezone("UTC")[0]
          )).state == TP.TARIFF_OFF_PEAK)

    # ---------------- 17. 年度邊界 ----------------
    print("\n17. 年度邊界（不得因相鄰年度而 fallback）")
    check("★★ 17. 2027-12-31 依 2027 Dataset 正常判定（平日 → PEAK）",
          date(2027, 12, 31).weekday() == 4
          and PROD.observe(at(2027, 12, 31, 14)).state == TP.TARIFF_PEAK
          and PROD.observe(at(2027, 12, 31, 14)).valid is True)
    check("★★ 17. 2028-01-01 → UNKNOWN（年度未 provision）",
          (lambda r: r.state == TP.TARIFF_UNKNOWN and r.valid is False)(
              PROD.observe(at(2028, 1, 1, 14))))
    check("★★ 17. 2025-12-31 → UNKNOWN（年度未 provision）",
          PROD.observe(at(2025, 12, 31, 14)).state == TP.TARIFF_UNKNOWN)
    check("★★ 17. 2026-01-01 → OFF_PEAK（第一個已 provision 的日子）",
          PROD.observe(at(2026, 1, 1, 0, 0)).state == TP.TARIFF_OFF_PEAK)
    check("★★ 17. 2025-12-31 23:59 → UNKNOWN、2026-01-01 00:00 → OFF_PEAK",
          PROD.observe(at(2025, 12, 31, 23, 59)).state == TP.TARIFF_UNKNOWN
          and PROD.observe(at(2026, 1, 1, 0, 0)).state == TP.TARIFF_OFF_PEAK)
    check("★★ 17. 2028 的任何一天都拿不到 True/False（一律 None）",
          all(AC.PRODUCTION_PROVIDER.is_off_peak_day(date(2028, m, 1)) is None
              for m in range(1, 13)))

    # ---------------- 18~20. 明令禁止項 ----------------
    print("\n18~20. 明令禁止項")
    ac_src = io.open(os.path.join(HERE, "annual_off_peak_calendar.py"),
                     encoding="utf-8").read()
    oc_src = io.open(os.path.join(HERE, "official_annual_calendar.py"),
                     encoding="utf-8").read()
    NET = {"requests", "urllib", "urllib3", "http", "httpx", "aiohttp",
           "socket", "ssl", "ftplib", "webbrowser", "telnetlib"}
    for mod in ("annual_off_peak_calendar.py", "official_annual_calendar.py",
                "taipower_calendar_extractor.py", "tariff_provider.py",
                "tou_calendar.py"):
        check(f"★★ 18. {mod} 無任何網路相依",
              not (_all_imports(_tree(mod)) & NET))
    check("★★ 18. 不存在任何自動下載 Calendar 的程式碼",
          not any(k in ac_src + oc_src
                  for k in ("urlopen", "download", "requests.get", "curl",
                            "http://", "wget")))
    check("  官方 URL 只作為 provenance 字串記錄，無任何取用程式碼",
          "https://" in oc_src
          and not any(k in oc_src for k in ("urlopen", "requests", "urllib")))
    check("★★ 19. 不會自動掃描目錄加入年度",
          not any(k in ac_src for k in ("listdir", "glob", "os.walk",
                                        "scandir", "iterdir"))
          and not (_all_imports(_tree("annual_off_peak_calendar.py"))
                   & {"glob", "pathlib", "os"}))
    check("★★ 19. 年度只能由已驗收清單納入（來源為 verified_lists，非目錄）",
          "verified_lists()" in ac_src)
    check("★★ 20. 未來年度不得 fallback：2028 一律 None",
          AC.PRODUCTION_PROVIDER.is_off_peak_day(date(2028, 9, 28)) is None
          and OC.day_type(2028, date(2028, 9, 28)) is None)
    check("★★ 20. 2027 資料不得沿用到 2028（同月同日結果不同）",
          AC.PRODUCTION_PROVIDER.is_off_peak_day(date(2027, 9, 28)) is True
          and AC.PRODUCTION_PROVIDER.is_off_peak_day(date(2028, 9, 28)) is None)
    check("★★ 20. 不得自行產生 2028 日期",
          2028 not in OC.ANNUAL_OFF_PEAK_DATES
          and 2028 not in OC.SUPPORTED_YEARS
          and OC.day_types_of(2028) is None)
    check("★★ 20. 無任何第三方 Calendar fallback",
          not (_all_imports(_tree("annual_off_peak_calendar.py"))
               | _all_imports(_tree("official_annual_calendar.py"))
               ) & {"holidays", "workalendar", "chinese_calendar",
                    "lunardate", "pandas", "dateutil"})
    check("  年度資料的日期由 official_annual_calendar 提供，"
          "annual_off_peak_calendar 不自產",
          all(tuple(l.off_peak_dates) == OC.ANNUAL_OFF_PEAK_DATES[l.year]
              for l in AC.PRODUCTION_ANNUAL_LISTS))

    # ---------------- 三態日型仍可證明 ----------------
    print("\n三態日型可追溯能力（裁示 7）")
    for y in OC.SUPPORTED_YEARS:
        check(f"★★ {y} 由 production 清單還原的三態日型與 PDF 解析摘要相同",
              EX.day_type_digest(
                  EX.derive_day_types(
                      y, [d for d in lists[y].off_peak_dates]))
              == OC.DAY_TYPE_DIGEST[y])
    check("  每個日期都能辨識 OFF_PEAK / SATURDAY / WEEKDAY",
          {OC.day_type(2026, d) for d in
           (date(2026, 9, 28), date(2026, 7, 18), date(2026, 7, 15))}
          == {EX.DT_OFF_PEAK, EX.DT_SATURDAY, EX.DT_WEEKDAY})

    # ---------------- 決策鏈未被扭曲 ----------------
    print("\n決策鏈與控制能力")
    eng = DE.DecisionEngine(policy=DP.TouArbitragePolicy(
        config=DP.PolicyConfig(charge_power_kw=5.0, discharge_power_kw=5.0)))

    class _Grid:
        state, valid = PC.STATE_IMPORT, True

    class _Ess:
        valid, stale, soc_percent = True, False, 30.0

    check("★★ 2028 時段 UNKNOWN → 決策仍 no_action（Fail Closed 未退步）",
          eng.decide(DE.DecisionInput(grid=_Grid(), tou=PROD.observe(
              at(2028, 1, 1, 14)), ess=_Ess())).action == DE.ACTION_NO_ACTION)
    check("  已 provision 的離峰日可產生動作（資料確實生效）",
          eng.decide(DE.DecisionInput(grid=_Grid(), tou=PROD.observe(
              at(2026, 9, 28, 14)), ess=_Ess())).action
          != DE.ACTION_NO_ACTION)
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ Layer 2 仍未配置，且 dispatch 未啟用",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None
          and __import__("pcs_auto_control_runtime").DISPATCH_ENABLED is False)
    check("★★ 年度資料模組不 import 任何控制模組",
          not ((_all_imports(_tree("annual_off_peak_calendar.py"))
                | _all_imports(_tree("official_annual_calendar.py")))
               & {"pcs_control_executor", "device_control_operator",
                  "pcs_control_integration", "production_execution_chain",
                  "pcs_auto_control_service", "report_monitor",
                  "auto_monitor_service", "safety_gate"}))
    check("★★ 既有時段規則未被修改（夏月定義沿用）",
          TC.G_CONFIG.summer_start == (5, 16)
          and TC.G_CONFIG.summer_end == (10, 15)
          and issubclass(AC.AnnualOffPeakDayProvider, TC.HolidayProvider))

    ok_all = all(RESULTS)
    print(f"\n== Phase D.4-E.2 已驗收年度資料上線 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
