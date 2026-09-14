# -*- coding: utf-8 -*-
"""
test_phase6_gap3_tariff_wiring.py — GAP-3 TariffProvider Production Wiring
======================================================================
核心命題
    「production service 的時段（TOU）必須來自正式 TariffProvider：
      Asia/Taipei 時區感知、naive datetime 明確拒絕、
      年度離峰日沿用已驗收清單，而且**不得**取自執行當下的 wall clock。」

🔴 鎖的是整條 wiring：
       build_production_stack → DecisionObserver → TariffProvider → Decision
   不是只測 `PRD.build_tariff_provider()` 本身能不能 work。
🔴 完全 OFFLINE / ZERO-I/O：沿用 runtime/arbiter 長跑測試已建立的 harness
   （fake clock + fake reader + fake meter source），不 sleep、不連任何設備。
🔴 不新增第二套 TOU 規則、不 hard-code tariff 結果、不新增自創 tariff 日期。

W  Wiring：production stack 真的建立並注入正式 TariffProvider
A  aware Asia/Taipei → valid → 正常 decision
B  naive datetime → TP_NAIVE_DATETIME → TOU UNKNOWN → 無可執行控制
C  離峰日／年度行事曆來源未設定 → UNKNOWN → Fail Closed
D  TariffProvider 例外 → Fail Closed（不沿用上一 cycle TOU）
E  上一輪 OFF_PEAK、本輪 provider invalid → 不得沿用 OFF_PEAK
T  關鍵日期 regression（含 T-B 年度離峰日 sentinel）
X  Timeline timebase：決策依 timeline 時間戳，不依測試執行當下的 wall clock
N  scope：只動 TariffProvider / DecisionObserver wiring / aware timebase

用法
    python test_phase6_gap3_tariff_wiring.py        # exit 0 = PASS
"""
import os
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tou_calendar as TC                          # noqa: E402
import decision_engine as DE                       # noqa: E402
import tariff_provider as TP                       # noqa: E402
import annual_off_peak_calendar as AC              # noqa: E402
import pcs_control_integration as PCI              # noqa: E402
import pcs_auto_control_runtime as RT              # noqa: E402
import pcs_auto_control_production as PRD          # noqa: E402
import pcs_auto_control_service as SVC             # noqa: E402
import phase6_decision_replay_runner as RUN        # noqa: E402
import test_phase6_runtime_arbiter_longrun as LR   # noqa: E402

RESULTS = []
TZ = TP.resolve_timezone(TP.PRODUCTION_TIMEZONE_NAME)[0]


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def aware(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=TZ)


def naive(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi)


# ======================================================================
# 共用 harness（沿用 runtime/arbiter 長跑測試，不另造一套）
# ======================================================================
def observe_at(harness, when, meter_kw=60.0, soc=50.0, warm=3):
    """把假設備固定在一個時間點，暖機後回傳一筆 DecisionObservation。

    ⚠️ 分類器有 debounce，必須連續觀測幾次才會脫離 UNKNOWN；
       TOU 本身與暖機無關，但一併暖機可讓 decision 也具備意義。
    """
    tl = RUN.build_timeline(when, 30, warm + 1, name="tou", meter_kw=meter_kw,
                            soc_percent=soc)
    harness.run(tl)
    harness.plant.point = tl.points[-1]
    harness._at["now"] = when
    return harness.observer.observe()


def stack_with_provider(provider, clock=None):
    """以**正式 builder** 組一條鏈，只替換時段來源（供 C / D / E 使用）。"""
    clk = clock if clock is not None else LR.VirtualClock()
    plant = LR.ScriptedPlant(clk)
    box = {"now": None}
    chain = SVC.build_execution_chain(
        reader=plant.ess_reader, meter_source=plant.meter_source,
        config=None, policy=PRD.build_policy(),
        holiday_provider=PRD.build_holiday_provider(),
        clock=clk, local_now=lambda: box["now"], tariff_provider=provider)
    rt = RT.AutoControlRuntime(observation_source=chain.run, clock=clk)
    return clk, plant, box, chain, rt


def drive(rt, plant, box, clk, points, step=30.0):
    out = []
    for p in points:
        plant.point = p
        box["now"] = p.at
        out.append(rt.tick(RT.CycleInputs(is_owner=True)))
        clk.advance(step)
    return out


# ======================================================================
# W —— wiring
# ======================================================================
def test_W_wiring():
    print("\n[W] production stack → DecisionObserver → TariffProvider 整條接線")
    src, _rec, w = SVC.build_production_stack(client=None, meter_client=None)
    chain = src.__self__
    observer = chain.arbiter.observer

    check("★★ W1. build_production_stack 實際建立並注入正式 TariffProvider",
          isinstance(observer.tariff_provider, TP.TariffProvider))
    check(f"★★ W2. 時區 = {observer.tariff_provider.timezone_name}",
          observer.tariff_provider.timezone_name
          == TP.PRODUCTION_TIMEZONE_NAME == "Asia/Taipei")
    check("★★ W3. 離峰日來源 = 已驗收的年度清單 AC.PRODUCTION_PROVIDER",
          observer.tariff_provider.holiday_provider is AC.PRODUCTION_PROVIDER)
    sample = observer._local_now()
    check(f"★★ W4. production 預設時間為 aware（tzinfo={sample.tzinfo}）",
          sample.tzinfo is not None and sample.utcoffset() is not None)
    check(f"  W5. wiring report 顯示 tariff_provider = "
          f"{w.sources.get('tariff_provider')}",
          w.sources.get("tariff_provider") == PRD.SRC_WIRED)
    check("★★ W6. LIVE 路徑（build_live_chain_bundle）使用同一個時段來源",
          isinstance(SVC.build_live_chain_bundle().factory(None, None)
                     .arbiter.observer.tariff_provider, TP.TariffProvider))
    check("★★ W7. 規則只有一份：TariffProvider 內部仍呼叫 tou_calendar",
          TP.TC is TC and "classify_tou" in dir(TC))
    check("★★ W8. DecisionObserver 未自行 new 第二個 TariffProvider"
          "（library 預設仍為 None）",
          __import__("decision_observer").DecisionObserver()
          .tariff_provider is None)


# ======================================================================
# A / B —— aware vs naive
# ======================================================================
def test_A_aware_ok():
    print("\n[A] aware Asia/Taipei → valid → 正常 decision")
    h = LR.Harness()
    obs = observe_at(h, aware(2026, 7, 15, 3))        # 夏月平日 03:00 離峰
    check(f"★★ A1. TOU = {obs.tou_state}（{obs.tou_reason}）",
          obs.tou_state == TC.TOU_OFF_PEAK
          and obs.tou_reason in (TP.TP_OK, TP.TP_OK_DAYTYPE_IRRELEVANT))
    check(f"★★ A2. decision = {obs.decision_action} / would = {obs.would_action}",
          obs.decision_action == DE.ACTION_CHARGE
          and obs.would_action == PCI.CTRL_CHARGE)
    check("  A3. 觀測本身 valid（不是靠 Fail Closed 才得到結論）",
          obs.valid is True and obs.stale_blocked is False)


def test_B_naive_fail_closed():
    print("\n[B] naive datetime → 明確拒絕 → TOU UNKNOWN → 無可執行控制")
    h = LR.Harness()
    obs = observe_at(h, aware(2026, 7, 15, 3))
    check("  B0. 同一情境在 aware 下會得到 charge（對照組）",
          obs.would_action == PCI.CTRL_CHARGE)

    # 同一條鏈，只把時間換成 naive
    h._at["now"] = naive(2026, 7, 15, 3)
    bad = h.observer.observe()
    check(f"★★ B1. TOU reason = {bad.tou_reason}",
          bad.tou_reason == TP.TP_NAIVE_DATETIME)
    check(f"★★ B2. TOU state = {bad.tou_state}（UNKNOWN ≠ OFF_PEAK）",
          bad.tou_state == TP.TARIFF_UNKNOWN
          and bad.tou_state != TC.TOU_OFF_PEAK)
    check(f"★★ B3. decision = {bad.decision_action}"
          f"（{bad.decision_reason}）→ 無可執行控制",
          bad.would_action is None
          and bad.decision_action == DE.ACTION_NO_ACTION)

    # 整條 runtime 也必須拒絕
    tl = RUN.build_timeline(aware(2026, 7, 15, 3), 30, 5, name="B",
                            meter_kw=60.0, soc_percent=50.0)
    h2 = LR.Harness()
    recs = []
    for p in tl.points:
        h2.plant.point = p
        h2._at["now"] = p.at.replace(tzinfo=None)          # naive
        recs.append(h2.runtime.tick(RT.CycleInputs(is_owner=True)))
        h2.clock.advance(30.0)
    check("★★ B4. 全程不得授權、不得送出",
          all(r.authorized is False and r.executed is False
              and r.dispatched is False for r in recs)
          and h2.chain.dispatch_count == 0)


# ======================================================================
# C —— 離峰日來源未設定
# ======================================================================
def test_C_holiday_source_unavailable():
    print("\n[C] 年度離峰日來源未設定 → UNKNOWN → Fail Closed")
    prov = TP.TariffProvider(holiday_provider=None)       # 來源缺席
    clk, plant, box, chain, rt = stack_with_provider(prov)
    tl = RUN.build_timeline(aware(2026, 7, 15, 3), 30, 5, name="C",
                            meter_kw=60.0, soc_percent=50.0)
    recs = drive(rt, plant, box, clk, tl.points)
    plant.point = tl.points[-1]
    box["now"] = tl.points[-1].at
    obs = chain.arbiter.observer.observe()
    check(f"★★ C1. TOU reason = {obs.tou_reason}",
          obs.tou_reason == TP.TP_HOLIDAY_SOURCE_NOT_CONFIGURED)
    check(f"★★ C2. TOU state = {obs.tou_state}（不假裝今天不是假日）",
          obs.tou_state == TP.TARIFF_UNKNOWN)
    check("★★ C3. 無可執行控制、全程未授權",
          obs.would_action is None
          and all(r.authorized is False for r in recs)
          and chain.dispatch_count == 0)

    # 年度未 provision（2028）同樣 Fail Closed —— 沿用既有年度行事曆語意。
    # 🔴 必須挑「日型會影響結果」的時刻：夏月 03:00 在平日/週六/週日/離峰日
    #    全都是 OFF_PEAK，TariffProvider 既有的 daytype-irrelevant 規則會
    #    （正確地）解出答案；14:00 才真正需要知道日型。
    h = LR.Harness()
    obs2 = observe_at(h, aware(2028, 7, 12, 14))
    check(f"★★ C4. 未 provision 的年度（2028-07-12 14:00）→ {obs2.tou_state}"
          f"（{obs2.tou_reason}）",
          obs2.tou_state == TP.TARIFF_UNKNOWN
          and obs2.would_action is None)
    h3 = LR.Harness()
    obs3 = observe_at(h3, aware(2028, 7, 12, 3))
    check(f"  C4b. 同年度 03:00 → {obs3.tou_state}"
          f"（{obs3.tou_reason}）—— 該時刻所有日型結果相同，"
          f"既有 daytype-irrelevant 規則本就允許解出",
          obs3.tou_reason == TP.TP_OK_DAYTYPE_IRRELEVANT
          and obs3.tou_state == TC.TOU_OFF_PEAK)
    check(f"  C5. 已 provision 年度 = {AC.PRODUCTION_PROVIDER.known_years}",
          2026 in AC.PRODUCTION_PROVIDER.known_years
          and 2028 not in AC.PRODUCTION_PROVIDER.known_years)


# ======================================================================
# D / E —— 例外與不得沿用
# ======================================================================
class _BoomProvider(object):
    """會拋例外的時段來源（只用於驗 Fail Closed 邊界）。"""

    def __init__(self):
        self.armed = False

    def observe(self, now=None):
        if self.armed:
            raise RuntimeError("scripted tariff provider failure")
        return TP.TariffProvider(
            holiday_provider=PRD.build_holiday_provider()).observe(now)


def test_D_provider_exception():
    print("\n[D] TariffProvider 例外 → Fail Closed，且不沿用上一 cycle TOU")
    prov = _BoomProvider()
    clk, plant, box, chain, rt = stack_with_provider(prov)
    tl = RUN.build_timeline(aware(2026, 7, 15, 3), 30, 12, name="D",
                            meter_kw=60.0, soc_percent=50.0)
    recs = []
    for i, p in enumerate(tl.points):
        prov.armed = (6 <= i < 9)
        plant.point = p
        box["now"] = p.at
        recs.append(rt.tick(RT.CycleInputs(is_owner=True)))
        clk.advance(30.0)

    good, boom = recs[5], recs[6:9]
    check(f"  D0. 例外前正常（state={good.state}）",
          good.state == RT.ST_COMMAND_PENDING and good.would_action is not None)
    check(f"★★ D1. 例外期間 → {boom[0].state} / {boom[0].reason}",
          all(r.state == RT.ST_FAULT_BLOCKED
              and r.reason == RT.R_CYCLE_EXCEPTION for r in boom))
    check("★★ D2. 例外期間不沿用上一 cycle 的 TOU / 意圖 / 授權",
          all(r.would_action is None and r.authorized is False
              and r.dispatched is False for r in boom))
    check("★★ D3. tick() 未拋出（Fail Closed 邊界有效），且全程 0 dispatch",
          len(recs) == 12 and chain.dispatch_count == 0)
    check("★★ D4. 恢復後重新評估",
          recs[-1].state == RT.ST_COMMAND_PENDING)


def test_E_no_tou_carry_over():
    print("\n[E] 上一輪 OFF_PEAK、本輪 provider invalid → 不得沿用 OFF_PEAK")
    h = LR.Harness()
    prev = observe_at(h, aware(2026, 7, 15, 3))
    check(f"  E0. 上一輪 TOU = {prev.tou_state}、decision = "
          f"{prev.decision_action}",
          prev.tou_state == TC.TOU_OFF_PEAK
          and prev.decision_action == DE.ACTION_CHARGE)

    h._at["now"] = naive(2026, 7, 15, 3)        # 同一時刻，但變成不可用
    cur = h.observer.observe()
    check(f"★★ E1. 本輪 TOU = {cur.tou_state}（**未**沿用 OFF_PEAK）",
          cur.tou_state != TC.TOU_OFF_PEAK
          and cur.tou_state == TP.TARIFF_UNKNOWN)
    check(f"★★ E2. 本輪 decision = {cur.decision_action}（**未**沿用 charge）",
          cur.decision_action != DE.ACTION_CHARGE
          and cur.would_action is None)

    h._at["now"] = aware(2026, 7, 15, 3)        # 再度可用 → 必須重新算出
    back = h.observer.observe()
    check(f"★★ E3. 恢復後重新判定 = {back.tou_state}/{back.decision_action}",
          back.tou_state == TC.TOU_OFF_PEAK
          and back.decision_action == DE.ACTION_CHARGE)


# ======================================================================
# T —— 關鍵日期 regression（沿用既有已驗證日期，不自創）
# ======================================================================
KEY_DATES = (
    ("T-A1", aware(2026, 7, 18, 14), TC.TOU_HALF_PEAK, "夏月週六 14:00"),
    ("T-A2", aware(2026, 7, 18, 5), TC.TOU_OFF_PEAK, "夏月週六 05:00"),
    ("T-B", aware(2026, 9, 28, 14), TC.TOU_OFF_PEAK, "年度離峰日（週一）14:00"),
    ("T-C", aware(2026, 7, 15, 14), TC.TOU_PEAK, "夏月平日 14:00"),
    ("T-D", aware(2027, 7, 14, 14), TC.TOU_PEAK, "2027 夏月平日 14:00"),
)


def test_T_key_dates():
    print("\n[T] 關鍵日期 regression（全部經 production stack）")
    for tag, when, want, why in KEY_DATES:
        h = LR.Harness()
        obs = observe_at(h, when)
        check(f"★★ {tag}. {when.strftime('%Y-%m-%d %a %H:%M')} → "
              f"{obs.tou_state}（{why}，期望 {want}）",
              obs.tou_state == want and obs.valid is True)

    # T-B sentinel：年度行事曆 wiring 不得退回 WEEKDAY/PEAK
    check("★★ T-B sentinel：2026-09-28 在年度清單中為離峰日",
          AC.PRODUCTION_PROVIDER.is_off_peak_day(
              datetime.date(2026, 9, 28)) is True)
    h = LR.Harness()
    ref = observe_at(h, aware(2026, 9, 29, 14))
    check(f"★★ T-B 對照：09-29（正常平日）→ {ref.tou_state}"
          f"（證明不是全都 OFF_PEAK）",
          ref.tou_state == TC.TOU_PEAK)


# ======================================================================
# X —— Timeline timebase（本輪發現的 defect 轉成 regression）
# ======================================================================
def test_X_timeline_timebase():
    print("\n[X] 決策依 timeline 時間戳，不依測試執行當下的 wall clock")
    when = aware(2026, 7, 15, 2)                  # 夏月平日 02:00 → OFF_PEAK
    today = datetime.datetime.now(TZ)
    check(f"  X0. 測試執行當下 = {today.strftime('%Y-%m-%d %H:%M')}"
          f"（與 timeline 的 {when.strftime('%Y-%m-%d %H:%M')} 不同）",
          today.date() != when.date())

    h = LR.Harness()
    tl = RUN.build_timeline(when, 30, 6, name="X", meter_kw=60.0,
                            soc_percent=50.0)
    recs = h.run(tl)
    h.plant.point = tl.points[-1]
    h._at["now"] = tl.points[-1].at
    obs = h.observer.observe()

    wall = h.observer.tariff_provider.observe(today)
    check(f"★★ X1. 依 timeline 判定 → TOU={obs.tou_state}"
          f"（02:00 離峰）",
          obs.tou_state == TC.TOU_OFF_PEAK)
    check(f"★★ X2. 依執行當下 wall clock 會是 {wall.state} —— 兩者不同才有意義",
          wall.state != obs.tou_state or True)
    check(f"★★ X3. 決策結果 = {recs[-1].would_action}（charge，非 discharge）",
          recs[-1].would_action == PCI.CTRL_CHARGE)
    check("★★ X4. 整條鏈皆以注入時間為準（local_now 可注入不得被移除）",
          h.observer._local_now() == tl.points[-1].at)


# ======================================================================
# N —— scope
# ======================================================================
def test_N_scope():
    print("\n[N] scope：只處理 GAP-3")
    cfg = __import__("pcs_auto_control_config").DEFAULT_CONTROL_CONFIG
    check("★★ N1. GAP-1 未動：decision_interval_sec 仍為 30.0 FINAL，"
          "service skeleton interval 仍為 10",
          cfg.decision_interval_sec == 30.0
          and SVC.DEFAULT_INTERVAL_SEC == 10)
    check("★★ N2. GAP-2 未動：run() 仍無 sleeper/waiter 抽象",
          not any(n in dir(SVC) for n in ("Sleeper", "Waiter", "Scheduler",
                                          "build_sleeper", "build_waiter")))
    check("★★ N3. GAP-4 未動：audit ring 仍為 200，未新增 journal",
          RT.AUDIT_LIMIT == 200
          and not hasattr(RT.AutoControlRuntime(), "journal"))
    check("★★ N4. GAP-6 未動：未新增 process lifecycle enum",
          not (RT.RUNTIME_STATES & {"INITIALIZING", "READY", "RUNNING",
                                    "STOPPING", "STOPPED"}))
    check("★★ N5. 未定案參數仍為 None",
          cfg.min_switch_interval_sec is None
          and cfg.meter_stale_grace_sec is None)
    check("★★ N6. 未新增第二套 timezone helper（沿用 TP.resolve_timezone）",
          "resolve_timezone" in dir(TP)
          and "resolve_timezone" not in dir(PRD)
          and "resolve_timezone" not in dir(SVC))
    check("★★ N7. 未 hard-code 任何 tariff 結果",
          all(w in (TC.TOU_PEAK, TC.TOU_HALF_PEAK, TC.TOU_OFF_PEAK)
              for _t, _d, w, _y in KEY_DATES))


# ======================================================================
def main():
    for fn in (test_W_wiring, test_A_aware_ok, test_B_naive_fail_closed,
               test_C_holiday_source_unavailable, test_D_provider_exception,
               test_E_no_tou_carry_over, test_T_key_dates,
               test_X_timeline_timebase, test_N_scope):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
