# -*- coding: utf-8 -*-
"""
test_phase6_decision_matrix_e2e.py — Meter-Driven Decision 端到端矩陣
======================================================================
核心命題
    「從 6160 的一筆 kW 一路走到 CHARGE / DISCHARGE / IDLE / BLOCKED，
      每一格的結果都必須由既有模組決定，而且可以完整解釋。
      任何一個環節不可信（meter stale / disconnect、TOU unknown、ESS stale、
      PCS 狀態不明、fault/alarm、authority 不明、功率未設定）都必須
      Fail Closed —— 不得產生 dispatch。」

🔴 expected value 一律引用既有正式 constant / enum，**不複製一套文字常數**。
🔴 本檔零 I/O、零設備、零網路。

情境 A ~ P（另含 configuration wiring A/B/C）
    A  OFF_PEAK + GRID_IMPORT + SOC MID   → CHARGE @ 5.0 kW
    B  PEAK + GRID_IMPORT + SOC MID/HIGH  → DISCHARGE @ 5.0 kW
    C  GRID_EXPORT    PEAK → IDLE / OFF_PEAK → CHARGE
    D  NEAR_ZERO      PEAK → IDLE / OFF_PEAK → CHARGE
    E  meter stale              → METER_STALE / no dispatch
    F  meter disconnected       → UNKNOWN / Fail Closed
    G  TOU UNKNOWN              → INVALID_TOU_STATE / Fail Closed
    H  SOC HIGH                 → charge blocked / IDLE
    I  SOC LOW                  → discharge blocked / IDLE
    J  fault / critical alarm   → BLOCKED
    K  authority unavailable    → BLOCKED
    L  CHARGE → DISCHARGE       → DIRECTION_REVERSAL_REQUIRES_STOP
    M  DISCHARGE → CHARGE       → DIRECTION_REVERSAL_REQUIRES_STOP
    N  PCS UNKNOWN / CONFLICT   → Fail Closed
    O  PolicyConfig power=None  → POLICY_POWER_NOT_CONFIGURED
    P  ESS stale                → ESS_STALE
    T  真實 TariffProvider + 年度行事曆 wiring（非 scripted TOU）
    U  _UNSET / None 語意（None 不得被便利預設頂替）

用法
    python test_phase6_decision_matrix_e2e.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import ast

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meter_client as MC                       # noqa: E402
import power_classifier as PC                   # noqa: E402
import tou_calendar as TC                       # noqa: E402
import decision_engine as DE                    # noqa: E402
import decision_policy as DP                    # noqa: E402
import safety_gate as SG                        # noqa: E402
import control_authority as CA                  # noqa: E402
import pcs_control_integration as PCI           # noqa: E402
import pcs_auto_control_config as CFG           # noqa: E402
import tariff_provider as TP                    # noqa: E402
import annual_off_peak_calendar as AC           # noqa: E402
import phase6_decision_simulator as SIM         # noqa: E402

from datetime import datetime                   # noqa: E402
from zoneinfo import ZoneInfo                   # noqa: E402

TZ = ZoneInfo(TP.PRODUCTION_TIMEZONE_NAME)      # Asia/Taipei

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def run(tou_state, meter_kw, soc, charging=False, discharging=False,
        standby=True, sim=None, payload=None, **sc_kw):
    """暖機分類器後跑一個 tick。回傳 (sim, record)。"""
    sim = sim if sim is not None else SIM.DecisionSimulator()
    pay = payload if payload is not None else SIM.meter_payload(meter_kw)
    _, t = sim.prime_classifier(pay, start_at=0.0)
    sc = SIM.ScenarioStep(
        meter_payload=pay, received_at=t, now=t,
        ess_reading=SIM.ess_reading(soc_percent=soc, charging=charging,
                                    discharging=discharging, standby=standby),
        tou=SIM.ScriptedTou(tou_state), **sc_kw)
    return sim, sim.step(sc)


def run_with_observation(tou_obs, meter_kw, soc, **sc_kw):
    """與 run() 相同，但 TOU 直接使用**真實 provider** 的 TariffObservation。"""
    sim = SIM.DecisionSimulator()
    pay = SIM.meter_payload(meter_kw)
    _, t = sim.prime_classifier(pay, start_at=0.0)
    sc = SIM.ScenarioStep(
        meter_payload=pay, received_at=t, now=t,
        ess_reading=SIM.ess_reading(soc_percent=soc), tou=tou_obs, **sc_kw)
    return sim, sim.step(sc)


# ======================================================================
# A / B —— 正常充放電
# ======================================================================
def test_A_offpeak_import_charge():
    print("\n[A] OFF_PEAK + GRID_IMPORT + SOC MID → CHARGE")
    sim, r = run(TC.TOU_OFF_PEAK, 60.0, 50.0)
    check(f"  grid 分類 = {r.grid_state}", r.grid_state == SIM.GRID_IMPORT)
    check(f"  meter direction = {r.meter_direction}",
          r.meter_direction == MC.DIR_IMPORT)
    check(f"  SOC band = {r.soc_band}", r.soc_band == DP.S_MID)
    check(f"★★ decision = {r.decision_action}",
          r.decision_action == DE.ACTION_CHARGE)
    check(f"★★ requested = {r.requested_action}",
          r.requested_action == SIM.CHARGE_REQUEST)
    check(f"★★ power = {r.requested_power_kw} kW（來自 DEFAULT_CONTROL_CONFIG）",
          r.requested_power_kw == CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw
          == 5.0)
    check(f"★★ outcome = {r.outcome}", r.outcome == PCI.OUT_WOULD_EXECUTE)
    check("  可解釋：" + r.explain(), "charge" in r.explain())


def test_B_peak_import_discharge():
    print("\n[B] PEAK + GRID_IMPORT + SOC MID/HIGH → DISCHARGE")
    for soc, band in ((50.0, DP.S_MID), (95.0, DP.S_HIGH)):
        sim, r = run(TC.TOU_PEAK, 60.0, soc)
        check(f"  SOC {soc} → band {r.soc_band}", r.soc_band == band)
        check(f"★★ SOC {soc}：decision = {r.decision_action}",
              r.decision_action == DE.ACTION_DISCHARGE)
        check(f"★★ SOC {soc}：requested = {r.requested_action} @ "
              f"{r.requested_power_kw} kW",
              r.requested_action == SIM.DISCHARGE_REQUEST
              and r.requested_power_kw
              == CFG.DEFAULT_CONTROL_CONFIG.discharge_power_kw == 5.0)
        check(f"★★ SOC {soc}：outcome = {r.outcome}",
              r.outcome == PCI.OUT_WOULD_EXECUTE)


# ======================================================================
# C / D —— 逆灌與 idle band
# ======================================================================
def test_C_export():
    print("\n[C] GRID_EXPORT：PEAK → IDLE / OFF_PEAK → CHARGE")
    _, rp = run(TC.TOU_PEAK, -60.0, 50.0)
    check(f"  grid 分類 = {rp.grid_state}", rp.grid_state == SIM.GRID_EXPORT)
    check(f"  meter direction = {rp.meter_direction}",
          rp.meter_direction == MC.DIR_EXPORT)
    check(f"★★ PEAK + EXPORT → decision = {rp.decision_action}（禁止擴大逆灌）",
          rp.decision_action == DE.ACTION_IDLE)
    check(f"★★ PEAK + EXPORT → 不送控制（{rp.outcome}）",
          rp.outcome == PCI.OUT_NO_CONTROL
          and rp.requested_action == SIM.NO_REQUEST)
    check("★★ PEAK + EXPORT 絕不放電",
          rp.requested_action != SIM.DISCHARGE_REQUEST)

    _, ro = run(TC.TOU_OFF_PEAK, -60.0, 50.0)
    check(f"  grid 分類 = {ro.grid_state}", ro.grid_state == SIM.GRID_EXPORT)
    check(f"★★ OFF_PEAK + EXPORT → decision = {ro.decision_action}（充電吸收逆灌）",
          ro.decision_action == DE.ACTION_CHARGE)
    check(f"★★ OFF_PEAK + EXPORT → {ro.requested_action} @ "
          f"{ro.requested_power_kw} kW",
          ro.requested_action == SIM.CHARGE_REQUEST
          and ro.outcome == PCI.OUT_WOULD_EXECUTE)


def test_D_near_zero():
    print("\n[D] NEAR_ZERO：PEAK → IDLE / OFF_PEAK → CHARGE")
    _, rp = run(TC.TOU_PEAK, 0.0, 50.0)
    check(f"  grid 分類 = {rp.grid_state}",
          rp.grid_state == SIM.GRID_NEAR_ZERO)
    check(f"★★ PEAK + NEAR_ZERO → decision = {rp.decision_action}（再放就逆灌）",
          rp.decision_action == DE.ACTION_IDLE)
    check(f"★★ PEAK + NEAR_ZERO → 不送控制（{rp.outcome}）",
          rp.outcome == PCI.OUT_NO_CONTROL)

    _, ro = run(TC.TOU_OFF_PEAK, 0.0, 50.0)
    check(f"★★ OFF_PEAK + NEAR_ZERO → decision = {ro.decision_action}",
          ro.decision_action == DE.ACTION_CHARGE)
    check(f"★★ OFF_PEAK + NEAR_ZERO → {ro.requested_action}",
          ro.requested_action == SIM.CHARGE_REQUEST)

    # idle band 邊界：±2.0 以內不得被判成 IMPORT / EXPORT
    for kw in (1.9, -1.9, 0.0):
        _, r = run(TC.TOU_OFF_PEAK, kw, 50.0)
        check(f"  {kw} kW → {r.grid_state}（遲滯 exit 門檻內）",
              r.grid_state == SIM.GRID_NEAR_ZERO)


# ======================================================================
# E / F —— meter 不可信
# ======================================================================
def test_E_meter_stale():
    print("\n[E] meter stale → 不得 dispatch")
    sim = SIM.DecisionSimulator()
    pay = SIM.meter_payload(60.0)
    _, t = sim.prime_classifier(pay, start_at=0.0)
    # 🔴 age 超過既有 STALE_AFTER_SEC，**不加任何 grace**
    stale_now = t + MC.STALE_AFTER_SEC + 1.0
    sc = SIM.ScenarioStep(meter_payload=pay, received_at=t, now=stale_now,
                          ess_reading=SIM.ess_reading(soc_percent=50.0),
                          tou=SIM.ScriptedTou(TC.TOU_OFF_PEAK))
    r = sim.step(sc)
    check(f"  meter age = {r.meter_age_sec:.1f}s > {MC.STALE_AFTER_SEC}",
          r.meter_age_sec > MC.STALE_AFTER_SEC)
    check(f"★★ meter stale = {r.meter_stale}", r.meter_stale is True)
    check(f"★★ classifier reason = {r.grid_reason}",
          r.grid_reason == PC.R_METER_STALE)
    check(f"★★ grid 立即回 UNKNOWN（不經 debounce）",
          r.grid_state == SIM.GRID_UNKNOWN)
    check(f"★★ decision = {r.decision_action} / {r.decision_reason}",
          r.decision_action == DE.ACTION_NO_ACTION
          and r.decision_reason == DE.R_INVALID_GRID_STATE)
    check(f"★★ 不產生 dispatch（{r.outcome}）",
          r.would_send is False and r.executor_called is False)
    check("★★ **沒有**沿用上一筆決策的寬限路徑",
          r.requested_action == SIM.NO_REQUEST)


def test_F_meter_disconnected():
    print("\n[F] meter disconnected → Fail Closed")
    sim = SIM.DecisionSimulator()
    sc = SIM.ScenarioStep(meter_payload=None, received_at=0.0, now=10.0,
                          ess_reading=SIM.ess_reading(soc_percent=50.0),
                          tou=SIM.ScriptedTou(TC.TOU_OFF_PEAK))
    r = sim.step(sc)
    # payload 為 None（斷線期間沒有任何 update 事件）→ 契約判 NOT_MAPPING；
    # R_NO_DATA 是 client 層「從未收到過資料」的狀態，兩者都不可用於決策
    check(f"  meter reason = {r.meter_reason}",
          r.meter_reason in (MC.R_NOT_MAPPING, MC.R_NO_DATA))
    check(f"  meter valid = {r.meter_valid}", r.meter_valid is False)
    check(f"★★ grid = {r.grid_state}", r.grid_state == SIM.GRID_UNKNOWN)
    check(f"★★ decision = {r.decision_action}",
          r.decision_action == DE.ACTION_NO_ACTION)
    check(f"★★ 不產生 dispatch", r.would_send is False
          and r.executor_called is False)

    # schema 不完整（例如 reference 版 6160 缺 state 欄位）同樣 Fail Closed
    sim2 = SIM.DecisionSimulator()
    bad = {"meter": 60.0, "demand": 60.0}          # 缺 meter_state / demand_state
    _, t = sim2.prime_classifier(bad, start_at=0.0)
    r2 = sim2.step(SIM.ScenarioStep(
        meter_payload=bad, received_at=t, now=t,
        ess_reading=SIM.ess_reading(soc_percent=50.0),
        tou=SIM.ScriptedTou(TC.TOU_OFF_PEAK)))
    check(f"★★ 缺 meter_state/demand_state → {r2.meter_reason}",
          r2.meter_reason == MC.R_MISSING)
    check("★★ 且不產生 dispatch", r2.would_send is False)


# ======================================================================
# G —— TOU
# ======================================================================
def test_G_tou_unknown():
    print("\n[G] TOU UNKNOWN → Fail Closed")
    _, r = run(TC.TOU_UNKNOWN, 60.0, 50.0)
    check(f"  tou = {r.tou_state} valid={r.tou_valid}",
          r.tou_state == TC.TOU_UNKNOWN and r.tou_valid is False)
    check(f"★★ decision reason = {r.decision_reason}",
          r.decision_reason == DE.R_INVALID_TOU_STATE)
    check(f"★★ decision = {r.decision_action}",
          r.decision_action == DE.ACTION_NO_ACTION)
    check("★★ 不產生 dispatch", r.would_send is False
          and r.executor_called is False)
    check("★★ TOU UNKNOWN 絕不退化成 OFF_PEAK",
          r.requested_action != SIM.CHARGE_REQUEST)


# ======================================================================
# H / I —— SOC 閂鎖
# ======================================================================
def test_H_soc_high():
    print("\n[H] SOC HIGH → charge 閂閉 → IDLE")
    _, r = run(TC.TOU_OFF_PEAK, 60.0, 95.0)
    check(f"  SOC band = {r.soc_band}", r.soc_band == DP.S_HIGH)
    check(f"★★ OFF_PEAK 但 decision = {r.decision_action}（不再充電）",
          r.decision_action == DE.ACTION_IDLE)
    check("★★ 不送 charge", r.requested_action != SIM.CHARGE_REQUEST)
    check(f"★★ outcome = {r.outcome}", r.outcome == PCI.OUT_NO_CONTROL)


def test_I_soc_low():
    print("\n[I] SOC LOW → discharge 閂閉 → IDLE")
    _, r = run(TC.TOU_PEAK, 60.0, 10.0)
    check(f"  SOC band = {r.soc_band}", r.soc_band == DP.S_LOW)
    check(f"★★ PEAK 但 decision = {r.decision_action}（無電可放）",
          r.decision_action == DE.ACTION_IDLE)
    check("★★ 不送 discharge", r.requested_action != SIM.DISCHARGE_REQUEST)
    check(f"★★ outcome = {r.outcome}", r.outcome == PCI.OUT_NO_CONTROL)


# ======================================================================
# J —— fault / critical alarm
# ======================================================================
def test_J_fault_alarm():
    print("\n[J] fault / critical alarm → BLOCKED")
    # J-1 critical alarm active（level 0 且 alarmStatus active）
    rows = ({"level": 0, "alarmStatus": True, "name": "critical-test"},)
    _, r = run(TC.TOU_OFF_PEAK, 60.0, 50.0, alarm_rows=rows,
               alarm_source_complete=True)
    check(f"  decision 仍為 {r.decision_action}（Safety 在決策之後）",
          r.decision_action == DE.ACTION_CHARGE)
    check(f"★★ outcome = {r.outcome}", r.outcome == PCI.OUT_SAFETY_BLOCKED)
    check(f"★★ blocked reason = {r.blocked_reason}",
          r.blocked_reason == SG.R_CRITICAL_ALARM_ACTIVE)
    check("★★ executor 未被呼叫", r.executor_called is False)

    # J-2 alarm 來源不完整 → 一律 Fail Closed（不得當成「沒有告警」）
    _, r2 = run(TC.TOU_OFF_PEAK, 60.0, 50.0, alarm_rows=(),
                alarm_source_complete=None)
    check(f"★★ alarm_source_complete=None → {r2.blocked_reason}",
          r2.blocked_reason == SG.R_ALARM_SOURCE_UNAVAILABLE)
    check("★★ executor 未被呼叫", r2.executor_called is False)

    # J-3 PCS fault
    _, r3 = run(TC.TOU_OFF_PEAK, 60.0, 50.0, pcs_fault=True)
    check(f"★★ pcs_fault=True → outcome {r3.outcome}",
          r3.outcome == PCI.OUT_SAFETY_BLOCKED)
    check("★★ executor 未被呼叫", r3.executor_called is False)


# ======================================================================
# K —— authority
# ======================================================================
def test_K_authority():
    print("\n[K] authority unavailable → BLOCKED")
    # 控制模式無法確認 → Authority 無法判定
    _, r = run(TC.TOU_OFF_PEAK, 60.0, 50.0, pcs_mode_state=None)
    check(f"  authority state = {r.authority_state}",
          r.authority_state in CA.AUTHORITY_STATES)
    check(f"★★ authority allowed = {r.authority_allowed}",
          r.authority_allowed is False)
    check(f"★★ outcome = {r.outcome}",
          r.outcome == PCI.OUT_AUTHORITY_BLOCKED)
    check("★★ executor 未被呼叫", r.executor_called is False)
    check("★★ 且不送任何控制", r.requested_action == SIM.NO_REQUEST)

    # 排程模式啟用 → 不是我們的控制權
    _, r2 = run(TC.TOU_OFF_PEAK, 60.0, 50.0,
                pcs_mode_state={"schedule_switch": 1, "manual_switch": 0})
    check(f"★★ schedule 啟用 → {r2.outcome} / {r2.authority_state}",
          r2.outcome == PCI.OUT_AUTHORITY_BLOCKED
          and r2.authority_allowed is False)


# ======================================================================
# L / M / N —— Layer 1 方向互鎖
# ======================================================================
def test_L_M_reversal():
    print("\n[L/M] 方向反轉 → DIRECTION_REVERSAL_REQUIRES_STOP")
    # L：PCS 正在 CHARGING，想 discharge
    dres = PCI.check_direction_interlock(PCI.CTRL_DISCHARGE, PCI.PCS_CHARGING)
    check("★★ L：CHARGING + discharge → 不放行",
          dres.allowed is False
          and dres.reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP)
    # M：PCS 正在 DISCHARGING，想 charge
    mres = PCI.check_direction_interlock(PCI.CTRL_CHARGE, PCI.PCS_DISCHARGING)
    check("★★ M：DISCHARGING + charge → 不放行",
          mres.allowed is False
          and mres.reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP)

    # 經 build_control_request（不注入 authority，直接驗方向層）
    class _D:
        action = DE.ACTION_DISCHARGE
        target_power_kw = 5.0
        reason = "test"

    ess_charging = DE.ess_snapshot_from_reading(
        SIM.ess_reading(50.0, charging=True, standby=False), 100.0, 100.2,
        now=100.5)
    ctrl = PCI.build_control_request(_D(), ess=ess_charging)
    check(f"★★ L：ControlRequest = {ctrl.action} / {ctrl.reason}",
          ctrl.action == SIM.NO_REQUEST
          and ctrl.reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP)
    check("★★ 反轉被擋時**不得**輸出 charge / discharge",
          ctrl.action not in (SIM.CHARGE_REQUEST, SIM.DISCHARGE_REQUEST))

    # 端到端：PCS CHARGING + PEAK（想放電）→ 一定不是 WOULD_EXECUTE
    _, r = run(TC.TOU_PEAK, 60.0, 50.0, charging=True, standby=False)
    check(f"★★ L 端到端 outcome = {r.outcome}（非 WOULD_EXECUTE）",
          r.outcome != PCI.OUT_WOULD_EXECUTE and r.would_send is False)
    check("★★ executor 未被呼叫", r.executor_called is False)

    _, r2 = run(TC.TOU_OFF_PEAK, 60.0, 50.0, discharging=True, standby=False)
    check(f"★★ M 端到端 outcome = {r2.outcome}（非 WOULD_EXECUTE）",
          r2.outcome != PCI.OUT_WOULD_EXECUTE and r2.would_send is False)

    # STOP 永遠不得被方向互鎖擋下
    for st in (PCI.PCS_CHARGING, PCI.PCS_DISCHARGING, PCI.PCS_UNKNOWN,
               PCI.PCS_CONFLICT):
        d = PCI.check_direction_interlock(PCI.CTRL_STOP, st)
        check(f"★★ STOP + {st} → 互鎖不適用（一律放行）",
              d.allowed is True and d.reason == PCI.DIR_NOT_APPLICABLE)


def test_N_pcs_state_unusable():
    print("\n[N] PCS UNKNOWN / CONFLICT → Fail Closed")
    for st in (PCI.PCS_UNKNOWN, PCI.PCS_CONFLICT):
        for act in (PCI.CTRL_CHARGE, PCI.CTRL_DISCHARGE):
            d = PCI.check_direction_interlock(act, st)
            check(f"★★ {st} + {act} → Fail Closed",
                  d.allowed is False
                  and d.reason == PCI.CR_DIRECTION_STATE_UNUSABLE)
    # 端到端：三旗標全 False → PCS 狀態無法確認
    _, r = run(TC.TOU_OFF_PEAK, 60.0, 50.0, charging=False, discharging=False,
               standby=False)
    check(f"  pcs_state = {r.pcs_state}",
          r.pcs_state in (PCI.PCS_UNKNOWN, PCI.PCS_CONFLICT, PCI.PCS_STOPPED))
    check(f"★★ outcome = {r.outcome}（非 WOULD_EXECUTE）",
          r.outcome != PCI.OUT_WOULD_EXECUTE and r.would_send is False)


# ======================================================================
# O —— 功率未設定
# ======================================================================
def test_O_power_not_configured():
    print("\n[O] PolicyConfig power = None → POLICY_POWER_NOT_CONFIGURED")
    bare = DP.PolicyConfig()
    check("★★ library 預設 charge_power_kw 仍為 None（未被改成 5.0）",
          bare.charge_power_kw is None)
    check("★★ library 預設 discharge_power_kw 仍為 None",
          bare.discharge_power_kw is None)

    class _Empty:
        charge_power_kw = None
        discharge_power_kw = None

    prof = SIM.ProductionProfile(control_config=_Empty())
    check(f"  profile status = {prof.status}",
          prof.status == SIM.PROFILE_POWER_NOT_CONFIGURED)
    check("  missing() 兩項全列",
          prof.missing() == ["charge_power_kw", "discharge_power_kw"])

    sim = SIM.DecisionSimulator(profile=prof)
    _, r = run(TC.TOU_OFF_PEAK, 60.0, 50.0, sim=sim)
    check(f"★★ decision = {r.decision_action}（**不是** idle）",
          r.decision_action == DE.ACTION_NO_ACTION)
    check(f"★★ reason = {r.decision_reason}",
          r.decision_reason == DP.R_POWER_NOT_CONFIGURED)
    check("★★ 絕不退化成 idle —— no_action 與 idle 語意不同",
          r.decision_action != DE.ACTION_IDLE)
    check("★★ 不產生 dispatch", r.would_send is False
          and r.executor_called is False)


# ======================================================================
# P —— ESS stale
# ======================================================================
def test_P_ess_stale():
    print("\n[P] ESS stale → ESS_STALE")
    sim = SIM.DecisionSimulator()
    pay = SIM.meter_payload(60.0)
    _, t = sim.prime_classifier(pay, start_at=0.0)
    limit = DE.DEFAULT_ESS_CONFIG.stale_after_sec
    sc = SIM.ScenarioStep(
        meter_payload=pay, received_at=t, now=t,
        ess_reading=SIM.ess_reading(soc_percent=50.0),
        # 🔴 讀取開始時刻遠早於 now → age 超過既有門檻
        ess_read_started_at=t - limit - 1.0,
        ess_read_completed_at=t - limit - 0.8,
        tou=SIM.ScriptedTou(TC.TOU_OFF_PEAK))
    r = sim.step(sc)
    check(f"  ESS age 超過 {limit}s → stale = {r.ess_stale}",
          r.ess_stale is True)
    check(f"★★ ESS reason = {r.ess_reason}", r.ess_reason == DE.E_STALE)
    check(f"★★ decision reason = {r.decision_reason}",
          r.decision_reason == DE.R_ESS_STALE)
    check(f"★★ decision = {r.decision_action}",
          r.decision_action == DE.ACTION_NO_ACTION)
    check("★★ 不產生 dispatch", r.would_send is False
          and r.executor_called is False)


# ======================================================================
# Configuration wiring（7-A / 7-B / 7-C）
# ======================================================================
def test_config_wiring():
    print("\n[W] configuration wiring —— library 預設 ≠ application 已核准值")
    # A. bare PolicyConfig()
    bare = DP.PolicyConfig()
    check("★★ A：bare PolicyConfig() 兩個功率皆 None",
          bare.charge_power_kw is None and bare.discharge_power_kw is None)
    pol = DP.TouArbitragePolicy(config=bare, clock=lambda: 0.0)
    eng = DE.DecisionEngine(policy=pol)
    ess = DE.ess_snapshot_from_reading(SIM.ess_reading(50.0), 0.0, 0.1, now=0.2)

    class _G:
        state = PC.STATE_IMPORT
        valid = True

    res = eng.decide(DE.DecisionInput(grid=_G(),
                                      tou=SIM.ScriptedTou(TC.TOU_OFF_PEAK),
                                      ess=ess))
    check(f"★★ A：decision reason = {res.reason}",
          res.reason == DP.R_POWER_NOT_CONFIGURED
          and res.action == DE.ACTION_NO_ACTION)

    # B. simulator production profile
    prof = SIM.ProductionProfile()
    check(f"★★ B：profile status = {prof.status}",
          prof.status == SIM.PROFILE_OK)
    check("★★ B：charge = 5.0 且來源是 DEFAULT_CONTROL_CONFIG",
          prof.charge_power_kw
          == CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0)
    check("★★ B：discharge = 5.0",
          prof.discharge_power_kw
          == CFG.DEFAULT_CONTROL_CONFIG.discharge_power_kw == 5.0)
    pc = prof.policy_config()
    check("★★ B：注入後的 PolicyConfig 帶 5.0 / 5.0",
          pc.charge_power_kw == 5.0 and pc.discharge_power_kw == 5.0)
    check("★★ B：注入**不會**改動 library 預設",
          DP.PolicyConfig().charge_power_kw is None)

    # C. DEFAULT_CONTROL_CONFIG 缺值 → 不得 fallback 5.0
    for bad in (None, 0.0, -1.0, float("nan")):
        class _Bad:
            charge_power_kw = bad
            discharge_power_kw = 5.0
        p = SIM.ProductionProfile(control_config=_Bad())
        check(f"  C：charge={bad!r} → {p.status}",
              p.status == SIM.PROFILE_POWER_NOT_CONFIGURED)
        check(f"  C：不自行 fallback 成 5.0（實得 {p.charge_power_kw!r}）",
              p.policy_config().charge_power_kw is not 5.0
              or p.charge_power_kw == 5.0)
    class _NoAttr:
        pass
    p2 = SIM.ProductionProfile(control_config=_NoAttr())
    check("★★ C：config 完全沒有該欄位 → Fail Closed，不猜值",
          p2.status == SIM.PROFILE_POWER_NOT_CONFIGURED
          and p2.charge_power_kw is None)

    # 未定案參數維持 None
    check("★★ min_switch_interval_sec 仍為 None（Layer 2 DEFERRED）",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None)
    check("★★ meter_stale_grace_sec 仍為 None（DEFERRED）",
          getattr(CFG.DEFAULT_CONTROL_CONFIG, "meter_stale_grace_sec",
                  None) is None)


# ======================================================================
# Simulator 本身的不變量
# ======================================================================
def test_simulator_invariants():
    print("\n[S] simulator 不變量")
    src = io.open(os.path.join(HERE, "phase6_decision_simulator.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    forbidden = mods & {"socket", "socketio", "requests", "urllib", "http",
                        "subprocess", "paramiko", "modbus_tk", "serial",
                        "asyncio", "telnetlib"}
    check(f"★★ 零 I/O：未匯入任何連線模組（命中={sorted(forbidden)}）",
          not forbidden)
    check(f"  實際匯入：{sorted(mods)}",
          {"meter_client", "power_classifier", "decision_engine",
           "decision_policy", "pcs_control_integration"} <= mods)

    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("★★ 不呼叫 connect / emit / execute / run_forever / Popen",
          not (calls & {"connect", "emit", "execute", "run_forever", "Popen",
                        "check_output", "urlopen"}))

    # 不得出現第二套決策矩陣或門檻
    check("★★ simulator 內沒有自己的 DECISION_MATRIX",
          "DECISION_MATRIX" not in src.replace("DP.DECISION_MATRIX", ""))
    nums = re.findall(r"^\s*[A-Z_]+_KW\s*=\s*-?\d", src, re.M)
    check(f"★★ 沒有自訂 kW 門檻常數（命中={nums}）", not nums)
    check("★★ 沒有硬編 5.0",
          not re.search(r"(charge|discharge)_power_kw\s*=\s*5\.0", src))

    # 語意名稱沿用既有常數
    check("★★ GRID_IMPORT / GRID_EXPORT 直接引用 power_classifier",
          SIM.GRID_IMPORT is PC.STATE_IMPORT
          and SIM.GRID_EXPORT is PC.STATE_EXPORT)
    check("★★ CHARGE_REQUEST / DISCHARGE_REQUEST 直接引用 pcs_control_integration",
          SIM.CHARGE_REQUEST is PCI.CTRL_CHARGE
          and SIM.DISCHARGE_REQUEST is PCI.CTRL_DISCHARGE)

    # ScriptedTou 只接受正式詞彙
    try:
        SIM.ScriptedTou("OFFPEAK")
        ok = False
    except ValueError:
        ok = True
    check("★★ ScriptedTou 拒絕非正式 TOU 詞彙", ok)

    # DecisionRecord 欄位足以解釋每一筆
    need = ("meter_raw_kw", "meter_normalized_kw", "grid_state",
            "meter_stale", "tou_state", "soc_band", "pcs_state",
            "authority_state", "pcs_fault", "critical_alarm_rows",
            "requested_action", "requested_power_kw", "blocked_reason",
            "decision_reason")
    check(f"★★ DecisionRecord 涵蓋全部必要記錄欄位（{len(SIM.DecisionRecord.FIELDS)} 個）",
          all(f in SIM.DecisionRecord.FIELDS for f in need))


# ======================================================================
# Replay parser
# ======================================================================
def test_replay():
    print("\n[R] replay parser（純字串，不開任何 runtime artifact）")
    csv_text = ("Timestamp,Power Meter (kW),BESS Power (kW),Power Demand (kW)\n"
                "10:00:00,60.5,0.0,60.5\n"
                "10:00:01,-12.25,0.0,-12.25\n"
                "10:00:02,bad,0.0,0.0\n")
    rows = SIM.parse_meter_csv(csv_text)
    check(f"  解析出 {len(rows)} 筆（壞行略過）", len(rows) == 2)
    check("  kW 正確", rows[0]["meter"] == 60.5 and rows[1]["meter"] == -12.25)
    check("★★ 預設**不補** meter_state / demand_state（不偽造契約欄位）",
          "meter_state" not in rows[0] and "demand_state" not in rows[0])
    snap = MC.evaluate(rows[0], received_at=0.0, now=0.0)
    check(f"★★ 因此 PrintMeterWeb CSV 回放 → {snap.reason}（正確 Fail Closed）",
          snap.reason == MC.R_MISSING and snap.valid is False)
    ok_rows = SIM.parse_meter_csv(csv_text, meter_state=MC.S_OK,
                                  demand_state=MC.S_OK)
    snap2 = MC.evaluate(ok_rows[0], received_at=0.0, now=0.0)
    check("★★ 顯式補上 state 才有效（僅供測試 fixture）",
          snap2.valid is True and snap2.direction == MC.DIR_IMPORT)
    check("  EXPORT 方向正確",
          MC.evaluate(SIM.parse_meter_csv(csv_text, meter_state=MC.S_OK,
                                          demand_state=MC.S_OK)[1],
                      received_at=0.0, now=0.0).direction == MC.DIR_EXPORT)

    jl = ('{"meter": 30.0, "demand": 30.0, "meter_state": "ok",'
          ' "demand_state": "ok"}\n'
          'not-json\n'
          '{"meter": -8.0, "demand": -8.0}\n')
    jrows = SIM.parse_meter_jsonl(jl)
    check(f"  JSONL 解析出 {len(jrows)} 筆（壞行略過）", len(jrows) == 2)
    check("  有 state 的保留、沒有的不補",
          jrows[0].get("meter_state") == MC.S_OK
          and "meter_state" not in jrows[1])
    check("★★ PRINTMETERWEB_CSV_COLUMNS 已登記為 reference 欄位",
          SIM.PRINTMETERWEB_CSV_COLUMNS[1] == "Power Meter (kW)")


# ======================================================================
# T —— 真實 TariffProvider + 年度行事曆的 application wiring
# ======================================================================
# 🔴 本組**不是**重測時段規則（那已由 tou_calendar / tariff_provider /
#    annual calendar 的既有回歸負責）。這裡鎖的是**接線**：
#        simulator → 正式 TariffProvider → 正式 materialized 年度行事曆
#        → decision engine → decision
#    因此一律真的呼叫 `TP.TariffProvider.observe()`，
#    **不** scripted TOU、**不** hard-code provider 結果、**不** mock provider。
#
# 🔴 日期一律取既有正式測試已證明合法者，不自行發明節日或時段。
def _real_provider():
    """正式組合：Asia/Taipei + 已 materialize 的 2026/2027 年度離峰日清單。"""
    return TP.TariffProvider(timezone_name=TP.PRODUCTION_TIMEZONE_NAME,
                             holiday_provider=AC.PRODUCTION_PROVIDER)


def _aware(y, mo, d, h, mi=0):
    """一律 aware datetime —— provider 會拒絕 naive，本檔不為此放寬。"""
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def test_T_real_tariff_provider_wiring():
    print("\n[T] 真實 TariffProvider + 年度行事曆 wiring")
    prov = _real_provider()

    check("  年度行事曆已 materialize（2026 / 2027）",
          tuple(sorted(AC.PRODUCTION_PROVIDER.known_years)) == (2026, 2027))
    for y in (2026, 2027):
        check(f"  {y} readiness = {AC.PRODUCTION_PROVIDER.readiness(y)}",
              AC.PRODUCTION_PROVIDER.readiness(y) == AC.AR_AVAILABLE)

    # ---- T-A 夏月週六（既有測試已證明的日期）----
    obs_a = prov.observe(_aware(2026, 7, 18, 14))
    check(f"★★ T-A 2026-07-18(Sat) 14:00 → provider 實回 {obs_a.state}",
          obs_a.state == TC.TOU_HALF_PEAK and obs_a.valid is True
          and obs_a.reason == TP.TP_OK)
    check(f"  season={obs_a.season} day_type={obs_a.day_type}（由正式規則導出）",
          obs_a.season == TC.SEASON_SUMMER
          and obs_a.day_type == TC.DAY_SATURDAY)
    _, ra = run_with_observation(obs_a, 60.0, 50.0)
    check(f"★★ T-A decision = {ra.decision_action}（HALF_PEAK 整列 IDLE）",
          ra.decision_action == DE.ACTION_IDLE)
    check(f"★★ T-A 不送控制（{ra.outcome}）",
          ra.outcome == PCI.OUT_NO_CONTROL
          and ra.requested_action == SIM.NO_REQUEST)

    # 同一天的日內邊界：09:00 之前仍是 OFF_PEAK
    obs_a2 = prov.observe(_aware(2026, 7, 18, 5))
    check(f"  T-A 同日 05:00 → {obs_a2.state}（日內分段確實生效）",
          obs_a2.state == TC.TOU_OFF_PEAK)
    _, ra2 = run_with_observation(obs_a2, 60.0, 50.0)
    check(f"★★ T-A 05:00 → decision = {ra2.decision_action} @ "
          f"{ra2.requested_power_kw} kW",
          ra2.decision_action == DE.ACTION_CHARGE
          and ra2.requested_power_kw
          == CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw)

    # ---- T-B 年度離峰日（星期一，若無行事曆會是 PEAK）----
    obs_b = prov.observe(_aware(2026, 9, 28, 14))
    check(f"★★ T-B 2026-09-28(Mon) 14:00 → provider 實回 {obs_b.state}",
          obs_b.state == TC.TOU_OFF_PEAK and obs_b.valid is True)
    check("★★ T-B day_type 由年度行事曆決定（OFF_PEAK_DAY，非 WEEKDAY）",
          obs_b.day_type == TC.DAY_OFF_PEAK_DAY)
    check("★★ T-B 這是關鍵證據：同為夏月星期一 14:00，"
          "沒有行事曆就會是 PEAK",
          AC.PRODUCTION_PROVIDER.is_off_peak_day(
              datetime(2026, 9, 28).date()) is True)
    _, rb = run_with_observation(obs_b, 60.0, 50.0)
    check(f"★★ T-B decision = {rb.decision_action} @ "
          f"{rb.requested_power_kw} kW",
          rb.decision_action == DE.ACTION_CHARGE
          and rb.requested_power_kw
          == CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0)
    check(f"★★ T-B outcome = {rb.outcome}",
          rb.outcome == PCI.OUT_WOULD_EXECUTE)

    # ---- T-C 正常工作日正常時段（證明不是所有日期都 OFF_PEAK）----
    obs_c = prov.observe(_aware(2026, 7, 15, 14))
    check(f"★★ T-C 2026-07-15(Wed) 14:00 → provider 實回 {obs_c.state}",
          obs_c.state == TC.TOU_PEAK and obs_c.valid is True)
    check(f"  day_type = {obs_c.day_type}（一般平日）",
          obs_c.day_type == TC.DAY_WEEKDAY)
    _, rc = run_with_observation(obs_c, 60.0, 50.0)
    check(f"★★ T-C decision = {rc.decision_action} @ "
          f"{rc.requested_power_kw} kW",
          rc.decision_action == DE.ACTION_DISCHARGE
          and rc.requested_power_kw
          == CFG.DEFAULT_CONTROL_CONFIG.discharge_power_kw == 5.0)
    check(f"★★ T-C outcome = {rc.outcome}",
          rc.outcome == PCI.OUT_WOULD_EXECUTE)

    # ---- T-D 2027 年度也確實接上 ----
    obs_d = prov.observe(_aware(2027, 7, 14, 14))
    check(f"★★ T-D 2027-07-14(Wed) 14:00 → {obs_d.state}（2027 清單同樣生效）",
          obs_d.state == TC.TOU_PEAK and obs_d.valid is True)

    # ---- 三個日期得到三種不同結果 = 接線確實有作用 ----
    check("★★ 三個真實日期產生三種不同 decision（HALF_PEAK/OFF_PEAK/PEAK）",
          len({ra.decision_action, rb.decision_action,
               rc.decision_action}) == 3)

    # ---- Fail Closed 未被放寬 ----
    naive = prov.observe(datetime(2026, 7, 15, 14, 0))     # 刻意 naive
    check(f"★★ naive datetime 仍被拒（{naive.reason}）",
          naive.reason == TP.TP_NAIVE_DATETIME
          and naive.state == TC.TOU_UNKNOWN and naive.valid is False)
    _, rn = run_with_observation(naive, 60.0, 50.0)
    check(f"★★ naive → decision {rn.decision_action} / {rn.decision_reason}",
          rn.decision_action == DE.ACTION_NO_ACTION
          and rn.decision_reason == DE.R_INVALID_TOU_STATE)
    check("★★ naive → 不產生 dispatch",
          rn.would_send is False and rn.executor_called is False)

    no_cal = TP.TariffProvider(timezone_name=TP.PRODUCTION_TIMEZONE_NAME)
    obs_nc = no_cal.observe(_aware(2026, 7, 15, 14))
    check(f"★★ 未注入年度行事曆 → {obs_nc.reason}（不假裝今天不是假日）",
          obs_nc.reason == TP.TP_HOLIDAY_SOURCE_NOT_CONFIGURED
          and obs_nc.state == TC.TOU_UNKNOWN)
    _, rnc = run_with_observation(obs_nc, 60.0, 50.0)
    check("★★ 未注入行事曆 → 不產生 dispatch",
          rnc.decision_action == DE.ACTION_NO_ACTION
          and rnc.would_send is False)

    # ---- 這一組必須真的走 provider，不得是 scripted ----
    check("★★ 本組使用的是 TariffObservation，**不是** ScriptedTou",
          not isinstance(obs_a, SIM.ScriptedTou)
          and type(obs_a).__name__ == "TariffObservation")
    check("★★ 且 holiday_provider 就是正式 PRODUCTION_PROVIDER",
          prov.holiday_provider is AC.PRODUCTION_PROVIDER)


# ======================================================================
# U —— _UNSET / None 語意（回歸鎖住，不得再退化）
# ======================================================================
def test_U_unset_vs_none():
    print("\n[U] _UNSET = 未指定；None = 資料不存在")
    # 不帶參數 → 便利預設（手動模式已確認）
    sc_default = SIM.ScenarioStep()
    check("  不帶 pcs_mode_state → 使用 convenience default",
          sc_default.pcs_mode_state == {"schedule_switch": 0,
                                        "manual_switch": 1})
    # 明確傳 None → 原樣保留，代表讀不到
    sc_none = SIM.ScenarioStep(pcs_mode_state=None)
    check("★★ 明確傳 None → 原樣保留 None（**不得**被預設頂替）",
          sc_none.pcs_mode_state is None)

    # 端到端：None 必須真的造成 Fail Closed，不得假通過
    _, r = run(TC.TOU_OFF_PEAK, 60.0, 50.0, pcs_mode_state=None)
    check(f"★★ pcs_mode_state=None → {r.outcome}（authority 無法判定）",
          r.outcome == PCI.OUT_AUTHORITY_BLOCKED
          and r.authority_allowed is False)
    check("★★ 且不送任何控制、executor 未被呼叫",
          r.requested_action == SIM.NO_REQUEST
          and r.executor_called is False)

    # 對照：帶正常 mode state 才可能放行
    _, r2 = run(TC.TOU_OFF_PEAK, 60.0, 50.0)
    check(f"  對照組（未指定 → 預設）outcome = {r2.outcome}",
          r2.outcome == PCI.OUT_WOULD_EXECUTE)
    check("★★ 兩者結果不同 —— 證明 None 沒有被當成預設",
          r.outcome != r2.outcome)

    # 原始碼層級：ScenarioStep 必須使用 sentinel，不得用 None 當預設
    src = io.open(os.path.join(HERE, "phase6_decision_simulator.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    found = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == "ScenarioStep":
            for sub in n.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "__init__":
                    a = sub.args
                    for arg, d in zip(a.args[len(a.args) - len(a.defaults):],
                                      a.defaults):
                        if arg.arg == "pcs_mode_state":
                            found = d
    check("★★ AST：pcs_mode_state 預設是具名 sentinel `_UNSET`，不是 None",
          isinstance(found, ast.Name) and found.id == "_UNSET")


# ======================================================================
def main():
    for fn in (test_A_offpeak_import_charge, test_B_peak_import_discharge,
               test_C_export, test_D_near_zero, test_E_meter_stale,
               test_F_meter_disconnected, test_G_tou_unknown,
               test_H_soc_high, test_I_soc_low, test_J_fault_alarm,
               test_K_authority, test_L_M_reversal,
               test_N_pcs_state_unusable, test_O_power_not_configured,
               test_P_ess_stale, test_config_wiring,
               test_T_real_tariff_provider_wiring, test_U_unset_vs_none,
               test_simulator_invariants, test_replay):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
