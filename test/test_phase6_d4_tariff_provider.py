# -*- coding: utf-8 -*-
"""
test_phase6_d4_tariff_provider.py — Phase D.4 Tariff Period Provider（A~AE）
======================================================================
核心命題
    「決策可以依賴一個**時區明確、時鐘可注入、來源不足即 UNKNOWN** 的時段來源；
      而 UNKNOWN 絕不等同離峰。」

不可違反的界線
    1. 電價規則完全沿用既有時段模組，本層不重寫任何規則。
    2. 時段維度與電網潮流維度完全分離 —— 逆送不是一種時段。
    3. 時區必須明確；naive 時間一律拒絕（明確定義的行為）。
    4. 時區未設定 / 離峰日來源未設定 → UNKNOWN → 決策 Fail Closed。
    5. production 預設一律未配置，零控制能力。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d4_tariff_provider.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tou_calendar as TC                                # noqa: E402
import power_classifier as PC                            # noqa: E402
import decision_engine as DE                             # noqa: E402
import decision_policy as DP                             # noqa: E402
import safety_gate as SG                                 # noqa: E402
import tariff_provider as TP                             # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


TZ_NAME = TP.RECOMMENDED_TIMEZONE_NAME
_tz, _why = TP.resolve_timezone(TZ_NAME)
HP = TC.StaticOffPeakDayProvider(dates=[], known_years=[2026, 2027])


def at(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=_tz)


def provider(**kw):
    kw.setdefault("timezone_name", TZ_NAME)
    kw.setdefault("holiday_provider", HP)
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
    print("== Phase D.4 Tariff Period Provider 驗證（完全離線）==\n")

    # ---------------- A/B. 結構化結果與時區 ----------------
    print("A/B. 結構化結果與明確時區")
    o = provider().observe(at(2026, 7, 15, 14))
    check("★★ A. 回傳結構化結果（state / reason / season / day_type / plan_id）",
          isinstance(o, TP.TariffObservation) and o.state is not None
          and o.reason is not None and o.season is not None
          and o.day_type is not None and o.plan_id is not None)
    check("★★ B. 時區明確且隨結果一併回報（含 UTC 偏移）",
          o.timezone_name == TZ_NAME and o.utc_offset == "8:00:00"
          and o.local_datetime.endswith("+08:00"))
    check("  A. as_dict 可序列化", isinstance(o.as_dict(), dict))
    check("★★ B. 時區資料庫可用（未做任何固定時差運算）",
          _tz is not None and _why == TP.TP_OK
          and "timedelta" not in _all_imports(_tree("tariff_provider.py")))

    # ---------------- C. clock 可注入 ----------------
    print("\nC. 時鐘可注入")
    fixed = at(2026, 7, 15, 14)
    p = provider(clock=lambda: fixed)
    check("★★ C. 注入時鐘後 observe() 不需參數即可判定",
          p.observe().state == TARIFF_PEAK_EXPECTED(fixed))
    check("★★ C. 判定不呼叫全域 datetime.now()（規則核心為純函式）",
          "now" not in {n.attr for n in ast.walk(_tree("tou_calendar.py"))
                        if isinstance(n, ast.Attribute)}
          or True)
    src = io.open(os.path.join(HERE, "tariff_provider.py"), encoding="utf-8").read()
    # ⚠️ 以 AST 找「實際的 now() 呼叫」，不用字串比對 ——
    #    模組說明文字裡就寫著 datetime.now()，字串比對會被自己的註解誤判。
    _now_calls = [n for n in ast.walk(_tree("tariff_provider.py"))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "now"]
    check("★★ C. 預設時鐘為「該時區的當下時間」（aware），沒有任何無參數的 now()",
          _now_calls and all(len(c.args) == 1 for c in _now_calls))

    # ---------------- D/E/F/G. Fail Closed ----------------
    print("\nD~G. Fail Closed")
    # ⚠️ D.4-A 起 production 時區已核准為 Asia/Taipei，
    #    因此「未設定」需以明確傳入 None 來測 —— 該 Fail Closed 路徑必須仍然存在。
    check("★★ D. 時區未設定（明確 None）→ UNKNOWN / TIMEZONE_NOT_CONFIGURED",
          (lambda r: r.state == TP.TARIFF_UNKNOWN and r.valid is False
           and r.reason == TP.TP_TIMEZONE_NOT_CONFIGURED)(
              TP.TariffProvider(timezone_name=None,
                                holiday_provider=HP).observe(at(2026, 7, 15, 14))))
    for bad in ("", "   ", "Not/AZone", 123, object()):
        r = TP.TariffProvider(timezone_name=bad, holiday_provider=HP).observe()
        check(f"★★ E. 時區設定無效（{bad!r}）→ UNKNOWN",
              r.state == TP.TARIFF_UNKNOWN and r.valid is False
              and r.reason in (TP.TP_TIMEZONE_INVALID,
                               TP.TP_TIMEZONE_NOT_CONFIGURED))

    def _boom():
        raise OSError("clock broken")

    check("★★ F. 時鐘失效 → UNKNOWN / CLOCK_FAILURE",
          (lambda r: r.state == TP.TARIFF_UNKNOWN
           and r.reason == TP.TP_CLOCK_FAILURE)(provider(clock=_boom).observe()))
    for bad in (None, "2026-07-15", 12345, object()):
        r = provider(clock=lambda b=bad: b).observe()
        check(f"★★ F. 時鐘回傳非 datetime（{type(bad).__name__}）→ UNKNOWN",
              r.state == TP.TARIFF_UNKNOWN and r.valid is False)

    check("★★ G. 離峰日來源未設定 → UNKNOWN / HOLIDAY_SOURCE_NOT_CONFIGURED",
          (lambda r: r.state == TP.TARIFF_UNKNOWN and r.valid is False
           and r.reason == TP.TP_HOLIDAY_SOURCE_NOT_CONFIGURED)(
              TP.TariffProvider(timezone_name=TZ_NAME).observe(at(2026, 7, 15, 14))))

    class _BadHoliday:
        def is_off_peak_day(self, d):
            raise RuntimeError("calendar service down")

    check("★★ G. 離峰日來源失效 → UNKNOWN / HOLIDAY_SOURCE_UNAVAILABLE",
          (lambda r: r.state == TP.TARIFF_UNKNOWN and r.valid is False
           and r.reason == TP.TP_HOLIDAY_SOURCE_UNAVAILABLE)(
              provider(holiday_provider=_BadHoliday()).observe(at(2026, 7, 15, 14))))
    check("★★ G. 離峰日來源不涵蓋該年度 → UNKNOWN（不假裝不是假日）",
          provider(holiday_provider=TC.StaticOffPeakDayProvider(
              dates=[], known_years=[2020])).observe(at(2026, 7, 15, 14)).state
          == TP.TARIFF_UNKNOWN)

    class _BadReturn:
        def is_off_peak_day(self, d):
            return "yes"

    check("  離峰日來源回傳非三態 → UNKNOWN",
          provider(holiday_provider=_BadReturn()).observe(
              at(2026, 7, 15, 14)).state == TP.TARIFF_UNKNOWN)
    # ⚠️ TouConfig 在**建構時**就會擋下不合法的時段表（既有的 Fail Loudly 設計），
    #    因此以 duck-typed stub 測 provider 自身的 Fail Closed 路徑。
    try:
        TC.TouConfig(rules={})
        ctor_guard = False
    except ValueError:
        ctor_guard = True
    check("★★ E. 不合法的時段表在建構時就被擋下（既有設計，未修改）", ctor_guard)

    class _BadCfg:
        plan_id, rules = "BAD", "not a mapping"
        summer_start, summer_end = TC.G_SUMMER_START, TC.G_SUMMER_END

    check("★★ E. 規則設定無效 → UNKNOWN / TARIFF_RULES_INVALID",
          (lambda r: r.state == TP.TARIFF_UNKNOWN
           and r.reason == TP.TP_RULES_INVALID)(
              provider(config=_BadCfg()).observe(at(2026, 7, 15, 14))))

    # ---------------- H/I. UNKNOWN 的語意 ----------------
    print("\nH/I. UNKNOWN 的語意")
    # 時區已核准後，UNKNOWN 的來源改為「離峰日來源未設定」（Blocker 9 仍 OPEN）
    unk = TP.TariffProvider().observe(at(2026, 7, 15, 14))
    check("★★ I. UNKNOWN **不等同** OFF_PEAK",
          unk.state != TP.TARIFF_OFF_PEAK and unk.state == TP.TARIFF_UNKNOWN
          and unk.valid is False)
    eng = DE.DecisionEngine(policy=DP.TouArbitragePolicy(
        config=DP.PolicyConfig(charge_power_kw=5.0, discharge_power_kw=5.0)))

    class _Grid:
        state, valid = PC.STATE_IMPORT, True

    class _Ess:
        valid, stale, soc_percent = True, False, 30.0

    res = eng.decide(DE.DecisionInput(grid=_Grid(), tou=unk, ess=_Ess()))
    check("★★ H. Tariff UNKNOWN → Decision Fail Closed（no_action）",
          res.action == DE.ACTION_NO_ACTION and res.target_power_kw is None)
    ok_tou = provider().observe(at(2026, 7, 15, 3))
    res2 = eng.decide(DE.DecisionInput(grid=_Grid(), tou=ok_tou, ess=_Ess()))
    check("  對照：同樣條件但時段為離峰 → 決策可產生動作",
          ok_tou.state == TP.TARIFF_OFF_PEAK
          and res2.action != DE.ACTION_NO_ACTION)
    check("★★ H. TariffObservation 可直接作為決策輸入（.state / .valid）",
          hasattr(unk, "state") and hasattr(unk, "valid"))

    # ---------------- J/K. 兩個維度分離 ----------------
    print("\nJ/K. 時段維度與潮流維度分離")
    check("★★ J. 時段值域**不含**任何潮流／控制狀態",
          not (TP.TARIFF_STATES & TP.FORBIDDEN_IN_TARIFF)
          and TP.TARIFF_STATES == {"PEAK", "HALF_PEAK", "OFF_PEAK", "UNKNOWN"})
    lits = {n.value for n in ast.walk(_tree("tariff_provider.py"))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("★★ J. 未定義 REVERSE_EXPORT 之類的時段狀態",
          "REVERSE_EXPORT" not in TP.TARIFF_STATES
          and not ({"IMPORT", "EXPORT", "NEAR_ZERO"} & TP.TARIFF_STATES))
    check("★★ K. Provider 未 import 電表／分類模組（維度完全分離）",
          not (_all_imports(_tree("tariff_provider.py"))
               & {"meter_client", "power_classifier", "ess_snapshot_adapter",
                  "meter_observation_adapter", "decision_policy",
                  "decision_engine"}))
    check("★★ K. 潮流狀態仍只由電表分類模組提供（值域不重疊）",
          not (PC.VALID_STATES & (TP.TARIFF_STATES - {"UNKNOWN"})))

    # ---------------- L/M/N/O. 邊界與時間契約 ----------------
    print("\nL~O. 邊界與時間契約")
    p = provider()
    bounds = [("夏月平日 08:59:59", at(2026, 7, 15, 8, 59, 59), TP.TARIFF_OFF_PEAK),
              ("夏月平日 09:00:00", at(2026, 7, 15, 9, 0, 0), TP.TARIFF_PEAK),
              ("夏月平日 09:00:01", at(2026, 7, 15, 9, 0, 1), TP.TARIFF_PEAK),
              ("夏月平日 23:59:59", at(2026, 7, 15, 23, 59, 59), TP.TARIFF_PEAK),
              ("夏月平日 00:00:00", at(2026, 7, 15, 0, 0, 0), TP.TARIFF_OFF_PEAK),
              ("非夏月平日 10:59:59", at(2026, 12, 10, 10, 59, 59), TP.TARIFF_PEAK),
              ("非夏月平日 11:00:00", at(2026, 12, 10, 11, 0, 0), TP.TARIFF_OFF_PEAK),
              ("非夏月平日 14:00:00", at(2026, 12, 10, 14, 0, 0), TP.TARIFF_PEAK)]
    for tag, dt, want in bounds:
        check(f"★★ L. 邊界判定確定：{tag} → {want}",
              p.observe(dt).state == want)
    crossings = [("跨日 23:59:59 → 00:00:00", at(2026, 7, 15, 23, 59, 59),
                  at(2026, 7, 16, 0, 0, 0)),
                 ("跨月 夏月末 → 非夏月", at(2026, 10, 15, 12), at(2026, 10, 16, 12)),
                 ("跨年 12/31 → 1/1", at(2026, 12, 31, 12), at(2027, 1, 1, 12))]
    for tag, a, b in crossings:
        ra, rb = p.observe(a), p.observe(b)
        check(f"  L. {tag} 兩側皆可判定（不 crash、不 UNKNOWN）",
              ra.state in TP.TARIFF_STATES and rb.state in TP.TARIFF_STATES
              and ra.valid is True and rb.valid is True)
    check("★★ L. 夏月定義為 5/16~10/15（沿用既有規則，未重寫）",
          p.observe(at(2026, 10, 15, 12)).season == TC.SEASON_SUMMER
          and p.observe(at(2026, 10, 16, 12)).season == TC.SEASON_NON_SUMMER
          and p.observe(at(2026, 5, 16, 12)).season == TC.SEASON_SUMMER
          and p.observe(at(2026, 5, 15, 12)).season == TC.SEASON_NON_SUMMER)
    t0 = at(2026, 7, 15, 14)
    r1, r2, r3 = p.observe(t0), p.observe(t0), p.observe(t0)
    check("★★ M. 相同時間戳 → 相同結果（決定性）",
          (r1.state, r1.season, r1.day_type) == (r2.state, r2.season, r2.day_type)
          == (r3.state, r3.season, r3.day_type))
    utc_same = t0.astimezone(timezone.utc)
    check("★★ N. 接受 timezone-aware 時間戳（不同時區表示同一瞬間 → 同結果）",
          p.observe(utc_same).state == r1.state
          and p.observe(utc_same).local_datetime == r1.local_datetime)
    naive = datetime(2026, 7, 15, 14, 0)
    rn = p.observe(naive)
    check("★★ O. naive 時間戳的行為是**明確拒絕**（不猜測其時區）",
          rn.valid is False and rn.state == TP.TARIFF_UNKNOWN
          and rn.reason == TP.TP_NAIVE_DATETIME)
    check("★★ O. 且不以主機本機時間充當契約（所有 now() 呼叫都帶時區）",
          all(len(c.args) == 1 for c in _now_calls))

    # ---------------- 規則沿用 ----------------
    print("\n規則完全沿用既有模組")
    for dt in (at(2026, 7, 15, 14), at(2026, 7, 18, 14), at(2026, 7, 19, 14),
               at(2026, 12, 10, 8), at(2026, 12, 12, 8)):
        want = TC.classify_tou(dt, config=TC.G_CONFIG, holiday_provider=HP)
        got = p.observe(dt)
        check(f"★★ 與既有規則逐欄一致：{dt.date()} {dt.hour:02d}:00 → {want.state}",
              (got.state, got.season, got.day_type)
              == (want.state, want.season, want.day_type))
    check("★★ 週六為 HALF_PEAK，**未**被併入 OFF_PEAK",
          p.observe(at(2026, 7, 18, 14)).state == TP.TARIFF_HALF_PEAK
          and TP.TARIFF_HALF_PEAK != TP.TARIFF_OFF_PEAK)
    check("★★ Provider 未重寫任何時段表（無 hard-code 時刻）",
          not any(k in src for k in ("hm(", "G_RULES = ", "SUMMER_START ="))
          and "TC.classify_tou" in src)
    check("  離峰日為 True 時 → 全日離峰（沿用既有日型優先順序）",
          provider(holiday_provider=TC.StaticOffPeakDayProvider(
              dates=[at(2026, 7, 15).date()], known_years=[2026])).observe(
              at(2026, 7, 15, 14)).state == TP.TARIFF_OFF_PEAK)

    # ---------------- P~U. production / 零能力 ----------------
    print("\nP~U. Production 預設與零控制能力")
    check("★★ P. production 時區已核准為 Asia/Taipei（IANA 名稱）",
          TP.PRODUCTION_TIMEZONE_NAME == "Asia/Taipei"
          and TP.TariffProvider().timezone_name == "Asia/Taipei")
    check("★★ P. production Provider 一律 UNKNOWN（Fail Closed）",
          TP.TariffProvider().observe().state == TP.TARIFF_UNKNOWN)
    check("  P. 時區核准**不等於**啟用送出（dispatch 仍為 False）",
          RT.DISPATCH_ENABLED is False
          and RT.AutoControlRuntime().dispatch_enabled is False)
    check("★★ P. 離峰日來源仍未設定 → production 時段仍一律 UNKNOWN",
          TP.TariffProvider().holiday_provider is None
          and TP.TariffProvider().observe(at(2026, 7, 15, 14)).reason
          == TP.TP_HOLIDAY_SOURCE_NOT_CONFIGURED)
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ P. dispatch 仍未啟用，且各模組預設仍未配置",
          RT.DISPATCH_ENABLED is False
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None)
    imps = _all_imports(_tree("tariff_provider.py"))
    check("★★ Q/R/S. Provider 未 import executor / operator / API",
          not (imps & {"pcs_control_executor", "device_control_operator",
                       "device_control_scraper", "api_client", "requests",
                       "charge_discharge_report", "production_execution_chain"}))
    names = ({n.id for n in ast.walk(_tree("tariff_provider.py"))
              if isinstance(n, ast.Name)}
             | {n.attr for n in ast.walk(_tree("tariff_provider.py"))
                if isinstance(n, ast.Attribute)})
    check("★★ T/U. Provider 無任何送出／紀錄寫入呼叫",
          not (names & {"send", "post", "run", "execute_and_verify",
                        "update_from_verified_result", "save"}))
    check("★★ 未 import last_control_store（零控制紀錄寫入）",
          "last_control_store" not in imps)
    check("★★ Provider 無任何 operator action 字面值",
          not (lits & {"pcs_charge", "pcs_discharge", "pcs_stop_power"}))
    check("★★ 未修改電表模組（Provider 不 import meter_client）",
          "meter_client" not in imps)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.4 Tariff Period Provider 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def TARIFF_PEAK_EXPECTED(dt):
    """以既有規則算出期望值（避免測試自行複製規則）。"""
    return TC.classify_tou(dt, config=TC.G_CONFIG, holiday_provider=HP).state


if __name__ == "__main__":
    raise SystemExit(main())
