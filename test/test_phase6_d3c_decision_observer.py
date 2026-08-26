# -*- coding: utf-8 -*-
"""
test_phase6_d3c_decision_observer.py — Phase D.3-C 驗證（A~AD）
======================================================================
核心命題
    「Production Runtime 已經可以看資料並做出決策，但仍然沒有能力執行決策。」

四條不可違反的界線
    1. 兩份資料來源（ESS / 電表）任一不可信 → 不產生任何充放電意圖。
    2. Adapter 當時 fresh **不等於** 決策當下 fresh —— 必須以決策時的 now 重算。
    3. 尖峰／離峰、逆送／deadband、充放電策略一律由正式 library 判定，
       本層不得出現第二套規則。
    4. would_action 只是稽核事實；Runtime 永遠 dispatched=False。

是否需要設備
    **不需要**。全部使用 fixture / fake reader / dependency injection，
    零網路連線、零實機讀取。

用法
    python test_phase6_d3c_decision_observer.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meter_client as MC                                # noqa: E402
import power_classifier as PC                            # noqa: E402
import tou_calendar as TC                                # noqa: E402
import decision_engine as DE                             # noqa: E402
import decision_policy as DP                             # noqa: E402
import safety_gate as SG                                 # noqa: E402
import ess_snapshot_adapter as EA                        # noqa: E402
import meter_observation_adapter as MA                   # noqa: E402
import decision_observer as DO                           # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
class Clk:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


def ess_reading(**over):
    r = {"communication_ok": True, "soc_percent": 50.0,
         "actual_active_power_kw": -1.3, "pcs_fault_flag": False,
         "pcs_charging_flag": False, "pcs_discharging_flag": False,
         "pcs_standby_flag": True, "pcs_running_flag": True,
         "battery_power_status": SG.BATT_ON, "pcs_control_mode_code": "manual",
         "pcs_schedule_enabled": False, "pcs_manual_switch": 1,
         "alarm_rows": [], "alarm_total": 0, "alarm_total_raw": 0, "_fail": []}
    for k, v in over.items():
        r.pop(k, None) if v is ... else r.__setitem__(k, v)
    return r


def meter_payload(**over):
    p = {"meter": 12.5, "meter_state": MC.S_OK,
         "demand": 8.0, "demand_state": MC.S_OK}
    p.update(over)
    return p


# 🔴 測試專用注入值 —— **不得**寫入 production defaults（另有專屬斷言）。
TEST_POLICY = DP.PolicyConfig(charge_power_kw=5.0, discharge_power_kw=5.0)
TEST_HOLIDAY = TC.StaticOffPeakDayProvider(dates=[], known_years=[2026])
DT_PEAK = datetime(2026, 7, 15, 14, 0)          # 夏月平日尖峰
DT_OFF_PEAK = datetime(2026, 7, 15, 3, 0)       # 夏月平日離峰


_UNSET = object()          # 與 None 區分：None 代表「明確不接來源」


class Rig:
    """
    一組完整的觀測鏈。所有時鐘與資料都由測試明確控制。

    ess_clk / meter_clk : Adapter 取樣用（決定 read_started_at / received_at）
    dec_clk             : 決策時鐘 —— 與 Adapter 分離，才能測 consumer-time freshness
    """

    def __init__(self, ess_over=None, meter_over=None, meter_kw=None,
                 meter_lag=0.0, dec_offset=0.0, local_now=DT_PEAK,
                 policy_cfg=TEST_POLICY, holiday=TEST_HOLIDAY,
                 ess_reader=_UNSET, meter_source=_UNSET, start=1000.0):
        self.clk = Clk(start)
        self.dec_clk = Clk(start + dec_offset)
        self.meter_lag = meter_lag
        self.meter_over = dict(meter_over or {})
        if meter_kw is not None:
            self.meter_over["meter"] = meter_kw
        self.ess_over = dict(ess_over or {})
        self.local_now = local_now

        ea = EA.EssSnapshotAdapter(
            reader=(self._ess if ess_reader is _UNSET else ess_reader),
            clock=self.clk)
        ma = MA.MeterObservationAdapter(
            source=(self._meter if meter_source is _UNSET else meter_source),
            clock=self.clk)
        self.policy = DP.TouArbitragePolicy(config=policy_cfg)
        self.observer = DO.DecisionObserver(
            ess_adapter=ea, meter_adapter=ma,
            classifier=PC.PowerClassifier(),
            engine=DE.DecisionEngine(policy=self.policy),
            policy=self.policy, holiday_provider=holiday,
            clock=self.dec_clk, local_now=lambda: self.local_now)

    def _ess(self):
        return ess_reading(**self.ess_over)

    def _meter(self):
        recv = self.clk.t - self.meter_lag
        return MC.evaluate(meter_payload(**self.meter_over),
                           received_at=recv, now=self.clk.t)

    def step(self, dt=1.0):
        """推進兩個時鐘並觀測一次（維持 dec_clk 與 clk 的相對位移）。"""
        self.clk.advance(dt)
        self.dec_clk.advance(dt)
        return self.observer.observe()

    def settle(self, n=8, dt=1.0):
        """跑到 classifier 的 debounce 完成，回傳最後一次觀測。"""
        r = None
        for _ in range(n):
            r = self.step(dt)
        return r


def _tree(mod):
    return ast.parse(io.open(mod.__file__, encoding="utf-8").read())


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
    print("== Phase D.3-C Decision Observer 驗證（OBSERVE-ONLY、完全離線）==\n")

    # ---------------- A / I. 兩者有效 ----------------
    print("A/I. ESS valid + Meter valid → Decision 可產生")
    r = Rig(meter_kw=12.5).settle()
    check("★★ A. 兩份觀測皆有效 → DecisionObservation valid",
          r.valid is True and r.reason == DO.DOBS_OK)
    check("★★ I. 兩者 fresh → 決策正常產生（有 decision_action 與 reason）",
          r.decision_action is not None and r.decision_reason is not None)
    check("  A. ESS 與 Meter 的判定結果都被如實記錄",
          r.ess_valid is True and r.meter_valid is True
          and r.pcs_state is not None and r.grid_power_kw == 12.5)
    check("  A. stale_blocked 為 False", r.stale_blocked is False)

    # ---------------- B/C/D. Meter Fail Closed ----------------
    print("\nB~D. 電表 Fail Closed")
    r = Rig(meter_source=None).settle()
    check("★★ B. 電表來源未接 → METER_OBSERVATION_INVALID / 無意圖",
          r.valid is False and r.reason == DO.DOBS_METER_INVALID
          and r.would_action is None)
    check("  B. 電表 reason 保留來源碼（可稽核）",
          r.meter_reason == MA.MOBS_SOURCE_NOT_CONFIGURED)

    def _boom_meter():
        raise ConnectionError("socket closed")

    r = Rig(meter_source=_boom_meter).settle()
    check("★★ B. 電表連線失敗 → Fail Closed / 無意圖",
          r.valid is False and r.meter_reason == MA.MOBS_SOURCE_EXCEPTION
          and r.would_action is None)

    for over, tag, want in (
            ({"meter": ...}, "台電電錶值缺失", MC.R_MISSING),
            ({"meter_state": "bogus"}, "meter_state 字彙錯誤", MC.R_STATE_VOCAB),
            ({"meter": "12.5"}, "型別錯誤", MC.R_TYPE)):
        pl = meter_payload()
        pl.pop("meter", None) if over.get("meter") is ... else pl.update(
            {k: v for k, v in over.items() if v is not ...})
        rr = Rig(meter_source=lambda pl=pl: MC.evaluate(pl, received_at=1000.0,
                                                        now=1000.0)).settle()
        check(f"★★ C. 電表 payload 非法（{tag}）→ Fail Closed / 無意圖",
              rr.valid is False and rr.reason == DO.DOBS_METER_INVALID
              and rr.would_action is None)

    for v, tag in ((float("nan"), "NaN"), (float("inf"), "Inf"),
                   (float("-inf"), "-Inf")):
        rr = Rig(meter_kw=v).settle()
        check(f"★★ D. 台電電錶值 {tag} → Fail Closed / 無意圖",
              rr.valid is False and rr.would_action is None)

    r = Rig(meter_source=lambda: {"meter": 1.0}).settle()
    check("  電表來源回傳非 MeterSnapshot → Fail Closed",
          r.valid is False and r.meter_reason == MA.MOBS_NOT_SNAPSHOT)
    r = Rig(meter_source=MC.no_data_snapshot).settle()
    check("  尚未收到任何電表資料 → Fail Closed（不猜測）",
          r.valid is False and r.meter_reason == MA.MOBS_SNAPSHOT_INVALID)

    # ---------------- E~H. Freshness ----------------
    print("\nE~H. Freshness（Adapter 時 fresh ≠ 決策時 fresh）")
    r = Rig(meter_lag=MC.STALE_AFTER_SEC + 1.0).settle()
    check("★★ E. 電表在 Adapter 就已過期 → Fail Closed",
          r.valid is False and r.meter_reason == MA.MOBS_SNAPSHOT_INVALID)

    # G：Adapter 取樣時 fresh，但決策時鐘已往前 → 決策當下過期
    rg = Rig(dec_offset=MC.STALE_AFTER_SEC + 1.0).settle()
    check("★★ G. 電表在 Adapter 時 fresh、決策時已過期 → STALE_BLOCKED",
          rg.valid is False and rg.reason == DO.DOBS_METER_STALE_AT_DECISION
          and rg.stale_blocked is True and rg.would_action is None)
    check("  G. 兩個 age 都被記錄，可看出「當時新、現在舊」",
          rg.meter_age_at_adapter_sec is not None
          and rg.meter_age_at_decision_sec > rg.meter_age_at_adapter_sec)

    # H：ESS 過期但電表仍新（電表 received_at 貼近決策時刻）
    ess_stale = DE.ESS_STALE_AFTER_SEC + 5.0
    rh = Rig(dec_offset=ess_stale, meter_lag=-ess_stale).settle()
    check("★★ H. ESS 在 Adapter 時 valid、決策時已過期 → STALE_BLOCKED",
          rh.valid is False and rh.reason == DO.DOBS_ESS_STALE_AT_DECISION
          and rh.stale_blocked is True and rh.would_action is None)
    check("★★ F. ESS consumer-time 過期即 BLOCK（不得沿用 Adapter 的結論）",
          rh.ess_valid is True and rh.ess_age_at_decision_sec > DE.ESS_STALE_AFTER_SEC)
    check("  H. 此情境的電表在決策當下仍是新的（證明兩者分別判定）",
          rh.meter_age_at_decision_sec <= MC.STALE_AFTER_SEC)
    check("★★ E/G/H. STALE_BLOCKED 一律不產生任何意圖",
          all(x.would_action is None for x in (rg, rh)))

    # ---------------- coherence ----------------
    print("\nSnapshot coherence（只記錄，不設門檻）")
    r = Rig(meter_lag=1.5).settle()
    check("★★ coherence_gap_sec 已計算並記錄",
          r.coherence_gap_sec is not None and r.coherence_gap_sec >= 0)
    check("★★ coherence 門檻維持 UNCONFIGURED（不猜秒數）",
          DO.COHERENCE_THRESHOLD_SEC is None
          and r.coherence_threshold_sec is None)
    check("  ESS 讀取起訖與電表抵達時刻皆已記錄",
          r.ess_read_started_at is not None and r.ess_read_completed_at is not None
          and r.meter_received_at is not None)
    check("  coherence 不影響 valid（只記錄）", r.valid is True)

    # ---------------- J/K. TOU ----------------
    print("\nJ/K. TOU 一律由正式 library 判定")
    for dt, tag in ((DT_PEAK, "J. 尖峰"), (DT_OFF_PEAK, "K. 離峰")):
        want = TC.classify_tou(dt, holiday_provider=TEST_HOLIDAY)
        got = Rig(local_now=dt).settle()
        check(f"★★ {tag} → 與 tou_calendar 判定完全一致（{want.state}）",
              got.tou_state == want.state and got.tou_season == want.season
              and got.tou_day_type == want.day_type)
    r_nohp = Rig(holiday=None).settle()
    check("★★ 未注入正式離峰日來源 → TOU UNKNOWN → Decision Fail Closed",
          r_nohp.tou_state == TC.TOU_UNKNOWN and r_nohp.would_action is None
          and r_nohp.decision_action == DE.ACTION_NO_ACTION)
    check("  觀測層未自行實作尖峰／離峰規則",
          not ({"PEAK", "HALF_PEAK", "OFF_PEAK"}
               & {n.value for n in ast.walk(_tree(DO))
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}))

    # ---------------- L/M/N. Grid classification ----------------
    print("\nL/M/N. Grid 分類一律由正式 library 判定")
    for kw, tag, want in ((12.5, "L. import", PC.STATE_IMPORT),
                          (-9.0, "M. export / 逆送", PC.STATE_EXPORT),
                          (0.0, "N. deadband", PC.STATE_NEAR_ZERO)):
        got = Rig(meter_kw=kw).settle()
        check(f"★★ {tag} → grid_state={want}",
              got.grid_state == want and got.grid_power_kw == kw)
    check("  觀測層未自行實作 deadband / 門檻 / 遲滯",
          not ({"IMPORT", "EXPORT", "NEAR_ZERO"}
               & {n.value for n in ast.walk(_tree(DO))
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}))
    # 斷線時 classifier 必須被推向 UNKNOWN，不得卡在斷線前的狀態
    rig = Rig(meter_kw=12.5)
    rig.settle()
    check("  前提：已進入穩定的 IMPORT",
          rig.observer.classifier.current().state == PC.STATE_IMPORT)
    rig.observer.meter_adapter = MA.MeterObservationAdapter(source=None)
    r_lost = rig.step()
    check("★★ 電表斷線 → grid 立即轉 UNKNOWN（不得卡在斷線前狀態）",
          r_lost.grid_state == PC.STATE_UNKNOWN and r_lost.valid is False)

    # ---------------- O/P/Q. would_action ----------------
    print("\nO/P/Q. would_action 只是稽核事實")
    seen_would = {}
    scenarios = [
        ("離峰 + IMPORT + SOC 低", dict(local_now=DT_OFF_PEAK, meter_kw=12.5,
                                    ess_over={"soc_percent": 30.0})),
        ("尖峰 + IMPORT + SOC 高", dict(local_now=DT_PEAK, meter_kw=12.5,
                                    ess_over={"soc_percent": 80.0})),
        ("尖峰 + EXPORT", dict(local_now=DT_PEAK, meter_kw=-9.0,
                             ess_over={"soc_percent": 80.0})),
        ("離峰 + EXPORT", dict(local_now=DT_OFF_PEAK, meter_kw=-9.0,
                             ess_over={"soc_percent": 30.0})),
    ]
    for tag, kw in scenarios:
        rr = Rig(**kw).settle()
        seen_would[tag] = rr.would_action
        check(f"  情境「{tag}」→ would={rr.would_action} "
              f"decision={rr.decision_action}",
              rr.valid is True and rr.decision_action is not None)
    check("★★ O/P/Q. 至少涵蓋 charge / discharge / idle 三種 would_action",
          {DO.WOULD_CHARGE, DO.WOULD_DISCHARGE, DO.WOULD_IDLE}
          <= set(seen_would.values()))
    check("★★ would_action 只可能是 charge / discharge / idle / None",
          all(v in DO.WOULD_ACTIONS or v is None for v in seen_would.values()))

    # ---------------- Z. Decision audit ----------------
    print("\nZ. Decision audit 必須能回答「為什麼」")
    r = Rig(local_now=DT_OFF_PEAK, meter_kw=12.5,
            ess_over={"soc_percent": 30.0}).settle()
    for f in ("ess_valid", "ess_reason", "ess_age_at_decision_sec", "pcs_state",
              "meter_valid", "meter_reason", "meter_age_at_decision_sec",
              "grid_power_kw", "grid_state", "tou_state", "decision_action",
              "decision_reason", "would_action", "coherence_gap_sec"):
        check(f"  audit 欄位存在且非 None：{f}", getattr(r, f) is not None)
    line = r.audit_line()
    check("★★ Z. 單行稽核可看出 grid / tou / soc / pcs / decision",
          all(k in line for k in ("grid=", "tou=", "soc=", "pcs=", "would="))
          and "因為" in line)
    check("  as_json_dict 可序列化", isinstance(r.as_json_dict(), dict))

    # ---------------- R/S. Production parameters ----------------
    print("\nR/S. Production 參數")
    prod = Rig(policy_cfg=DP.PolicyConfig()).settle()      # 充放電功率皆 None
    check("★★ R. 充放電功率未設定 → Decision Fail Closed（no_action）",
          prod.valid is True and prod.decision_action == DE.ACTION_NO_ACTION
          and prod.would_action is None)
    check("  R. reason 明示為功率未配置",
          "POWER" in (prod.decision_reason or ""))
    check("★★ S. 測試注入值未污染 production defaults",
          DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.discharge_power_kw is None
          and DP.PolicyConfig().charge_power_kw is None)
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ S. dispatch 仍未啟用，且 Safety Gate 模組預設仍未配置",
          RT.DISPATCH_ENABLED is False
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None)
    check("  S. Meter / ESS 新鮮度門檻未被修改",
          MC.STALE_AFTER_SEC == 3.0
          and DE.DEFAULT_ESS_CONFIG.stale_after_sec == DE.ESS_STALE_AFTER_SEC)

    # ---------------- T/U/V. Runtime 仍無控制能力 ----------------
    print("\nT/U/V. Runtime：看得到決策，做不到控制")
    full = CFG.AutoControlConfig(
        charge_power_kw=5.0, discharge_power_kw=5.0, max_power_kw=100.0,
        authority_ttl_sec=120.0, authority_power_tolerance_kw=1.0,
        decision_interval_sec=30.0, authorization_ttl_sec=30.0,
        readback_timeout_sec=75.0, readback_poll_interval_sec=5.0)

    rig = Rig(local_now=DT_OFF_PEAK, meter_kw=12.5, ess_over={"soc_percent": 30.0})
    rig.settle()
    rt = RT.AutoControlRuntime(config=full,
                               observation_source=lambda: rig.step(0.0))
    res = rt.tick(RT.CycleInputs(is_owner=True))
    check("★★ Runtime 取得 would_action 並記錄為 WOULD_* 稽核事件",
          res.audit_event in (RT.AUDIT_WOULD_CHARGE, RT.AUDIT_WOULD_DISCHARGE,
                              RT.AUDIT_WOULD_IDLE)
          and res.would_action is not None)
    check("★★ O/P/Q. dispatched 恆為 False，dispatch_count 恆為 0",
          res.dispatched is False and rt.dispatch_count == 0)
    check("★★ T/U/V. 狀態仍為 OBSERVE_ONLY（不得進 dispatch 狀態）",
          res.state == RT.ST_OBSERVE_ONLY
          and res.state not in RT.DISPATCH_STATES)

    rig_stale = Rig(dec_offset=MC.STALE_AFTER_SEC + 1.0)
    rt2 = RT.AutoControlRuntime(config=full,
                                observation_source=lambda: rig_stale.step(0.0))
    res2 = rt2.tick(RT.CycleInputs(is_owner=True))
    check("★★ E/F. consumer-time stale → Runtime 進 STALE_BLOCKED",
          res2.state == RT.ST_STALE_BLOCKED
          and res2.audit_event == RT.AUDIT_STALE_BLOCKED
          and res2.would_action is None and res2.dispatched is False)
    check("  STALE_BLOCKED 語意是「資料不足以判斷」，不是控制失敗",
          res2.reason == RT.R_OBSERVATION_STALE and rt2.dispatch_count == 0)

    seen = set()
    rig3 = Rig(local_now=DT_OFF_PEAK, meter_kw=12.5, ess_over={"soc_percent": 30.0})
    rt3 = RT.AutoControlRuntime(config=full,
                                observation_source=lambda: rig3.step(1.0))
    for owner in (True, False, True, True, False):
        seen.add(rt3.tick(RT.CycleInputs(is_owner=owner)).state)
    seen.add(rt3.shutdown().state)
    check("★★ T. 不得進 COMMAND_PENDING", RT.ST_COMMAND_PENDING not in seen)
    check("★★ U. 不得進 VERIFYING", RT.ST_VERIFYING not in seen)
    check("★★ V. 不得進 OWNED_CHARGE / OWNED_DISCHARGE",
          not (seen & {RT.ST_OWNED_CHARGE, RT.ST_OWNED_DISCHARGE}))
    check("★★ 實際出現的狀態全部落在 D.3-C 允許集合內",
          seen <= RT.D3C_REACHABLE_STATES and rt3.dispatch_count == 0)

    # ---------------- W/X/Y. 零控制能力 ----------------
    print("\nW/X/Y. 零控制能力（AST 靜態驗證）")
    PCS_PATH = {"device_control_operator", "device_control_menu",
                "device_control_scraper", "pcs_control_executor", "api_client",
                "charge_discharge_report", "report_monitor",
                "phase6_field_measure"}
    for mod in (DO, MA):
        base = os.path.basename(mod.__file__)
        hit = _all_imports(_tree(mod)) & PCS_PATH
        check(f"★★ W. {base} 未 import 任何 PCS 控制路徑（命中={sorted(hit)}）",
              not hit)
    names_do = ({n.id for n in ast.walk(_tree(DO)) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(_tree(DO)) if isinstance(n, ast.Attribute)})
    check("★★ X. 觀測層未建立任何 executor 實例",
          not (names_do & {"PcsControlExecutor", "MockExecutor", "send",
                           "operator_run", "RealPcsExecutor"}))
    check("★★ Y. 觀測層未寫入 LastControl，也未建立 Authorization",
          not (_all_imports(_tree(DO)) & {"last_control_store", "control_authority"})
          and not (names_do & {"LastControlStore", "ControlLegAuthorization",
                               "ProductionControlAuthorization", "save"}))
    check("★★ 觀測層未呼叫 Safety Gate 的 dispatch 路徑或建立 ControlRequest",
          not (names_do & {"SafetyGate", "SafetyRequest", "build_control_request",
                           "DryRunPipeline", "check_direction_interlock"})
          and "pcs_control_integration" not in _all_imports(_tree(DO)))
    lits = {n.value for n in ast.walk(_tree(DO))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("★★ 觀測層無任何 operator action 字面值",
          not (lits & {"pcs_charge", "pcs_discharge", "pcs_stop_power"}))
    check("★★ 觀測層未 import phase6_field_measure（Field 永遠只是 reference）",
          "phase6_field_measure" not in _all_imports(_tree(DO)))
    check("  Runtime core 仍未 import 任何 adapter / observer / 決策 library",
          not (_all_imports(_tree(RT))
               & {"decision_observer", "ess_snapshot_adapter",
                  "meter_observation_adapter", "decision_engine",
                  "decision_policy", "tou_calendar", "power_classifier",
                  "meter_client"}))

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-C Decision Observer 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
