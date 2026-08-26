# -*- coding: utf-8 -*-
"""
test_phase6_d3d_arbitration.py — Phase D.3-D 驗證（A~AN）
======================================================================
核心命題
    「系統可以**授權**一個 control leg，但仍然沒有能力**送出**它。」

不可違反的界線
    1. 授權票 action-bound / one-shot / expiring；ttl=None 不得等同永不過期。
    2. Decision 時 fresh ≠ Authorization 時 fresh —— 必須重新觀測、重新比對。
    3. Authority / Direction Interlock / Safety Gate 一律重用既有模組，
       且 Safety 的原始 reason 不得被換成 generic BLOCKED。
    4. dispatch_count 永遠 = 0；VERIFYING / OWNED_* 永遠不可達。

是否需要設備
    **不需要**。全部 fixture / fake reader / dependency injection。

用法
    python test_phase6_d3d_arbitration.py        # exit 0 = PASS
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
import control_authority as CA                           # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import ess_snapshot_adapter as EA                        # noqa: E402
import meter_observation_adapter as MA                   # noqa: E402
import decision_observer as DO                           # noqa: E402
import production_control_authorization as PAZ           # noqa: E402
import production_arbiter as ARB                         # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ----------------------------------------------------------------------
class Clk:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


# 🔴 全部為測試注入值 —— production defaults 另有斷言證明仍為 None。
TEST_CFG = CFG.AutoControlConfig(
    charge_power_kw=5.0, discharge_power_kw=5.0, max_power_kw=100.0,
    authority_ttl_sec=120.0, authority_power_tolerance_kw=1.0,
    decision_interval_sec=30.0, authorization_ttl_sec=30.0,
    readback_timeout_sec=75.0, readback_poll_interval_sec=5.0)
TEST_POLICY = DP.PolicyConfig(charge_power_kw=5.0, discharge_power_kw=5.0)
TEST_HOLIDAY = TC.StaticOffPeakDayProvider(dates=[], known_years=[2026])
DT_PEAK = datetime(2026, 7, 15, 14, 0)
DT_OFF_PEAK = datetime(2026, 7, 15, 3, 0)

FLAGS = {
    "STANDBY": dict(pcs_standby_flag=True, pcs_running_flag=True),
    "STOPPED": dict(pcs_standby_flag=False, pcs_running_flag=False),
    "CHARGING": dict(pcs_charging_flag=True, pcs_standby_flag=False,
                     pcs_running_flag=True, actual_active_power_kw=5.2),
    "DISCHARGING": dict(pcs_discharging_flag=True, pcs_standby_flag=False,
                        pcs_running_flag=True, actual_active_power_kw=-5.2),
}


def ess_reading(pcs="STANDBY", **over):
    r = {"communication_ok": True, "soc_percent": 50.0,
         "actual_active_power_kw": -1.3, "pcs_fault_flag": False,
         "pcs_charging_flag": False, "pcs_discharging_flag": False,
         "pcs_standby_flag": True, "pcs_running_flag": True,
         "battery_power_status": SG.BATT_ON, "pcs_control_mode_code": "manual",
         "pcs_schedule_enabled": False, "pcs_manual_switch": 1,
         "alarm_rows": [], "alarm_total": 0, "alarm_total_raw": 0, "_fail": []}
    r.update(FLAGS[pcs])
    for k, v in over.items():
        r.pop(k, None) if v is ... else r.__setitem__(k, v)
    return r


class LCRec:
    """last_control_store.LastControlRecord 的極簡替身。"""

    def __init__(self, action="charge", power=5.0, at=990.0,
                 power_at_verify=-1.3):
    # 🔁 Blocker 13 補齊：真實 LastControlRecord 必有 actual_active_power_kw
    #    （read-back 當下的觀測值）。實機證據顯示該值通常仍是**指令前的閒置值**，
    #    因為 AC 暫存器比旗標慢 0~1 個 refresh cycle。Authority 用它判斷
    #    暫存器是否已更新，因此替身必須提供，且預設為閒置值。
        self.action = action
        self.target_power_kw = power
        self.verified_at_monotonic = at
        self.actual_active_power_kw = power_at_verify


class Rig:
    """完整的觀測 → 仲裁鏈。所有時鐘與資料由測試明確控制。"""

    def __init__(self, pcs="STANDBY", ess_over=None, meter_kw=12.5,
                 local_now=DT_OFF_PEAK, soc=30.0, config=TEST_CFG,
                 last_control=None, trust=CA.TRUST_FOR_INTERVAL,
                 dec_offset=0.0, start=1000.0, mutate=None):
        self.clk = Clk(start)
        self.dec_clk = Clk(start + dec_offset)
        self.pcs = pcs
        self.ess_over = dict(ess_over or {})
        self.ess_over.setdefault("soc_percent", soc)
        self.meter_kw = meter_kw
        self.local_now = local_now
        self.mutate = mutate          # 在兩次觀測之間改變世界
        self._reads = 0
        self.last_control = last_control
        self.trust = trust

        ea = EA.EssSnapshotAdapter(reader=self._ess, clock=self.clk)
        ma = MA.MeterObservationAdapter(source=self._meter, clock=self.clk)
        self.policy = DP.TouArbitragePolicy(config=TEST_POLICY)
        self.observer = DO.DecisionObserver(
            ess_adapter=ea, meter_adapter=ma, classifier=PC.PowerClassifier(),
            engine=DE.DecisionEngine(policy=self.policy), policy=self.policy,
            holiday_provider=TEST_HOLIDAY, clock=self.dec_clk,
            local_now=lambda: self.local_now)
        self.arbiter = ARB.ProductionArbiter(
            observer=self.observer, config=config, clock=self.dec_clk,
            last_control_provider=(None if last_control is None
                                   else (lambda: (self.last_control, self.trust))))

    def _ess(self):
        self._reads += 1
        if self.mutate is not None:
            self.mutate(self, self._reads)
        return ess_reading(self.pcs, **self.ess_over)

    def _meter(self):
        return MC.evaluate({"meter": self.meter_kw, "meter_state": MC.S_OK,
                            "demand": 8.0, "demand_state": MC.S_OK},
                           received_at=self.clk.t, now=self.clk.t)

    def tick(self, dt=1.0):
        self.clk.advance(dt)
        self.dec_clk.advance(dt)
        return self.observer.observe()

    def settle(self, n=8):
        for _ in range(n):
            self.tick()
        return self

    def arbitrate(self, dt=1.0):
        self.clk.advance(dt)
        self.dec_clk.advance(dt)
        return self.arbiter.evaluate()


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
    print("== Phase D.3-D Arbitration + Authorization 驗證（完全離線）==\n")

    # ---------------- A/B. 授權成功 ----------------
    print("A/B. 授權成功（但仍未、也不能送出）")
    r = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0).settle().arbitrate()
    check("★★ A. WOULD_CHARGE + Authority IDLE + Safety PASS → AUTHORIZED",
          r.outcome == ARB.ARB_AUTHORIZED and r.authorized is True
          and r.control_action == PCI.CTRL_CHARGE and r.dispatched is False)
    check("  A. 授權票已建立且處於 ISSUED", r.authorization.state == PAZ.AUTHZ_ISSUED
          and r.authorization_id is not None)
    check("  A. Authority 為 IDLE、Safety allowed、14 項全評估",
          r.authority_state == CA.AUTH_IDLE and r.safety_allowed is True
          and r.safety_check_count == 14)
    rd = Rig(pcs="STANDBY", local_now=DT_PEAK, soc=80.0).settle().arbitrate()
    check("★★ B. WOULD_DISCHARGE → AUTHORIZED_WOULD_DISCHARGE / zero dispatch",
          rd.outcome == ARB.ARB_AUTHORIZED
          and rd.control_action == PCI.CTRL_DISCHARGE and rd.dispatched is False)

    # ---------------- C/D. IDLE 不建立多餘 STOP ----------------
    print("\nC/D. Decision IDLE + 已閒置 → 不建立 STOP leg")
    for pcs in ("STANDBY", "STOPPED"):
        ri = Rig(pcs=pcs, local_now=DT_PEAK, meter_kw=-9.0, soc=50.0).settle().arbitrate()
        check(f"★★ C/D. Decision IDLE + {pcs} → 無 control leg",
              ri.outcome == ARB.ARB_NO_CONTROL
              and ri.control_action == PCI.CTRL_NONE
              and ri.authorization is None)
    check("  C/D. reason 明示為「不需要 leg」，不是被擋下",
          Rig(pcs="STANDBY", local_now=DT_PEAK, meter_kw=-9.0,
              soc=50.0).settle().arbitrate().reason == ARB.R_IDLE_NO_LEG)

    # ---------------- E/F. 反向 → 只能 STOP candidate ----------------
    print("\nE/F. 反向切換只能得到 STOP candidate")
    rev = [("DISCHARGING", DT_OFF_PEAK, 30.0, "E. desired CHARGE"),
           ("CHARGING", DT_PEAK, 80.0, "F. desired DISCHARGE")]
    for pcs, dt, soc, tag in rev:
        lc = LCRec(action=("discharge" if pcs == "DISCHARGING" else "charge"),
                   power=5.0, at=1000.0)
        rr = Rig(pcs=pcs, local_now=dt, soc=soc, last_control=lc).settle().arbitrate()
        check(f"★★ {tag} + 目前 {pcs} → 只授權 STOP",
              rr.outcome == ARB.ARB_AUTHORIZED
              and rr.control_action == PCI.CTRL_STOP
              and rr.direction_reason == ARB.R_STOP_REQUIRED_FOR_REVERSAL)
        check(f"  {tag} 的授權票 action 是 stop（不是反方向）",
              rr.authorization.action == PCI.CTRL_STOP)
        check(f"  {tag} STOP leg 未完成前不得出現反方向授權",
              rr.authorization.action != (PCI.CTRL_CHARGE
                                          if pcs == "DISCHARGING"
                                          else PCI.CTRL_DISCHARGE))

    # ---------------- G. Schedule takeover ----------------
    print("\nG. Schedule takeover")

    def _sched_on(rig, n):
        if n > 2:                       # 第 2 次觀測（fresh）之後開啟排程
            rig.ess_over["pcs_schedule_enabled"] = True

    rg = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0).settle()
    rg.ess_over["pcs_schedule_enabled"] = True
    r = rg.arbitrate()
    check("★★ G. 授權前排程主開關轉為開啟 → BLOCK",
          r.outcome == ARB.ARB_BLOCKED and r.authorized is False
          and r.authorization is None)
    check("★★ G. reason 可追溯到排程接管",
          r.reason in (ARB.R_AUTHORITY_BLOCKED, ARB.R_SAFETY_BLOCKED)
          and ("SCHEDULE" in (r.authority_reason or "")
               or "SCHEDULE" in (r.safety_reason or "")
               or r.authority_state == CA.AUTH_EXTERNAL))
    check("  G. 未自行關閉排程開關、也未送 STOP",
          r.control_action in (PCI.CTRL_NONE, None))

    # ---------------- H. Manual / external takeover ----------------
    print("\nH. Manual / external takeover")
    rh = Rig(pcs="CHARGING", local_now=DT_OFF_PEAK, soc=30.0,
             last_control=None).settle().arbitrate()
    check("★★ H. 設備運轉中但無可信 Phase 6 ownership → EXTERNAL → BLOCK",
          rh.outcome == ARB.ARB_BLOCKED and rh.authority_state == CA.AUTH_EXTERNAL
          and rh.authorized is False)
    check("  H. 不搶回、不覆寫、不自動 STOP",
          rh.control_action in (PCI.CTRL_NONE, None) and rh.authorization is None)
    rh2 = Rig(pcs="CHARGING", local_now=DT_OFF_PEAK, soc=30.0,
              last_control=LCRec("discharge", 5.0, 1000.0)).settle().arbitrate()
    check("★★ H. LastControl 與實際狀態不符 → CONFLICT → BLOCK",
          rh2.authority_state == CA.AUTH_CONFLICT and rh2.authorized is False)

    # ---------------- I/J. Stale at authorization ----------------
    print("\nI/J. 授權當下的新鮮度")
    ri = Rig(pcs="STANDBY", soc=30.0, dec_offset=DE.ESS_STALE_AFTER_SEC + 5.0)
    ri.settle()
    r = ri.arbiter.evaluate()
    check("★★ I/J. 授權當下資料已過舊 → BLOCK / STALE，且不核發授權",
          r.outcome == ARB.ARB_BLOCKED
          and r.reason == ARB.R_STALE_AT_AUTHORIZATION
          and r.stale_blocked is True and r.authorization is None)
    check("  I. ESS stale 時不得授權 CHARGE / DISCHARGE / STOP",
          r.control_action in (None, PCI.CTRL_NONE))
    rj = Rig(pcs="STANDBY", soc=30.0, dec_offset=MC.STALE_AFTER_SEC + 0.5)
    rj.settle()
    check("★★ J. 電表在授權當下過舊 → BLOCK（方向政策仍未放行 STOP）",
          rj.arbiter.evaluate().reason == ARB.R_STALE_AT_AUTHORIZATION)

    # ---------------- K. Decision changed ----------------
    print("\nK. 兩次觀測之間決策改變")

    def _flip_soc(rig, n):
        if n >= 2:                      # fresh 觀測時 SOC 已跨越停止門檻
            rig.ess_over["soc_percent"] = 95.0

    rk = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0)
    rk.settle()
    rk.mutate = _flip_soc
    rk._reads = 0
    r = rk.arbiter.evaluate()
    check("★★ K. original 與 fresh 決策不一致 → BLOCK / DECISION_CHANGED",
          r.outcome == ARB.ARB_BLOCKED and r.reason == ARB.R_DECISION_CHANGED
          and r.authorization is None)
    check("  K. audit 同時保留 original 與 fresh 的決策",
          r.original_decision_action != r.fresh_decision_action)

    # ---------------- L. Safety failure ----------------
    print("\nL. Safety Gate 失敗必須保留原因")
    rl = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0,
             ess_over={"pcs_fault_flag": True}).settle().arbitrate()
    check("★★ L. Safety BLOCK → outcome BLOCKED / reason SAFETY_BLOCKED",
          rl.outcome == ARB.ARB_BLOCKED and rl.reason == ARB.R_SAFETY_BLOCKED
          and rl.authorized is False)
    check("★★ L. **原始** Safety reason 完整保留（未被換成 generic）",
          rl.safety_reason is not None and rl.safety_reason != ARB.R_SAFETY_BLOCKED
          and rl.safety_allowed is False and rl.safety_check_count == 14)

    # ---------------- M. Alarm provenance ----------------
    print("\nM. Alarm provenance")
    rm = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0,
             ess_over={"alarm_total_raw": None}).settle().arbitrate()
    check("★★ M. 告警來源不完整 → 連觀測都無效 → 不可能授權",
          rm.authorized is False and rm.authorization is None)
    check("  M. 空清單不得被當成「沒有告警」",
          rm.outcome == ARB.ARB_BLOCKED)

    # ---------------- N/O/P/Q/R. 授權票性質 ----------------
    print("\nN~R. 授權票 action-bound / one-shot / expiring")
    b = PAZ.AuthorizationBinding(authority_state="IDLE", pcs_state="STANDBY",
                                 decision_action="charge", grid_state="IMPORT",
                                 tou_state="OFF_PEAK", alarm_source_complete=True,
                                 schedule_switch=0, manual_switch=1)
    az, _ = PAZ.issue(PCI.CTRL_CHARGE, 5.0, 100.0, 30.0, b, 1)
    check("★★ N. CHARGE 授權票不能拿來送 DISCHARGE",
          az.check(PCI.CTRL_DISCHARGE, 105.0)[1] == PAZ.AZ_ACTION_MISMATCH)
    az2, _ = PAZ.issue(PCI.CTRL_DISCHARGE, 5.0, 100.0, 30.0, b, 2)
    check("★★ O. DISCHARGE 授權票不能拿來送 CHARGE",
          az2.check(PCI.CTRL_CHARGE, 105.0)[1] == PAZ.AZ_ACTION_MISMATCH)
    az3, _ = PAZ.issue(PCI.CTRL_STOP, None, 100.0, 30.0, b, 3)
    check("★★ P. STOP 授權票獨立，不接受 charge / discharge",
          az3.check(PCI.CTRL_CHARGE, 105.0)[1] == PAZ.AZ_ACTION_MISMATCH
          and az3.check(PCI.CTRL_DISCHARGE, 105.0)[1] == PAZ.AZ_ACTION_MISMATCH
          and az3.check(PCI.CTRL_STOP, 105.0)[0] is True)
    check("★★ Q. one-shot：消費一次後永久失效",
          az.consume(PCI.CTRL_CHARGE, 105.0)[0] is True
          and az.state == PAZ.AUTHZ_CONSUMED
          and az.consume(PCI.CTRL_CHARGE, 106.0) == (False, PAZ.AZ_ALREADY_CONSUMED))
    az4, _ = PAZ.issue(PCI.CTRL_CHARGE, 5.0, 100.0, 10.0, b, 4)
    check("★★ R. 過期授權 → BLOCK，且就地標記 EXPIRED（不可能復活）",
          az4.consume(PCI.CTRL_CHARGE, 200.0) == (False, PAZ.AZ_EXPIRED)
          and az4.state == PAZ.AUTHZ_EXPIRED
          and az4.consume(PCI.CTRL_CHARGE, 100.5) == (False, PAZ.AZ_EXPIRED))
    check("  綁定脈絡改變 → 不得消費",
          PAZ.issue(PCI.CTRL_CHARGE, 5.0, 100.0, 30.0, b, 5)[0].consume(
              PCI.CTRL_CHARGE, 105.0,
              PAZ.AuthorizationBinding(**{**b.as_dict(), "pcs_state": "CHARGING"}),
              5.0)[1] == PAZ.AZ_SNAPSHOT_CHANGED)
    check("  target 不符 → 不得消費",
          PAZ.issue(PCI.CTRL_CHARGE, 5.0, 100.0, 30.0, b, 6)[0].consume(
              PCI.CTRL_CHARGE, 105.0, None, 9.0)[1] == PAZ.AZ_TARGET_MISMATCH)

    # ---------------- S/T/U. None 參數語意 ----------------
    print("\nS/T/U. production None 參數的語意")
    for miss, tag in (("authorization_ttl_sec", "S. 授權 TTL"),
                      ("authority_ttl_sec", "S. Authority TTL"),
                      ("authority_power_tolerance_kw", "T. power tolerance"),
                      ("max_power_kw", "T. max_power_kw")):
        cfg = CFG.AutoControlConfig(**{**TEST_CFG.as_dict(), miss: None})
        rr = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0,
                 config=cfg).settle().arbitrate()
        check(f"★★ {tag} = None → 不核發授權",
              rr.authorized is False and rr.authorization is None)
    check("★★ S. ttl=None 絕不等同「永不過期」（issue 直接拒絕）",
          PAZ.issue(PCI.CTRL_CHARGE, 5.0, 100.0, None, b, 9)
          == (None, PAZ.AZ_TTL_NOT_CONFIGURED))
    check("★★ S. ttl=None 的授權票 is_expired 恆為 True（Fail Closed）",
          PAZ.ProductionControlAuthorization("X", PCI.CTRL_CHARGE, 5.0, 100.0,
                                             None, b).is_expired(100.0) is True)
    check("★★ U. min_switch_interval=None 語意為「未配置」而非「無限制」",
          SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None
          and "min_switch_interval_sec" not in ARB.AUTHORIZATION_REQUIRED_PARAMS)
    _sr = SG.SafetyGate().check(SG.SafetyRequest(
        requested_action=SG.REQ_CHARGE, target_power_kw=5.0,
        ess=DE.ess_snapshot_from_reading(ess_reading(), 1.0, 1.1, now=1.2),
        alarm_rows=(), alarm_source_complete=True,
        pcs_mode_state={"schedule_switch": 0, "manual_switch": 1}), now=2.0)
    _sw = [c for c in _sr.checks if c.name == SG.C_SWITCH][0]
    check("  U. C_SWITCH 在未配置時為 skip 且明示「規則未啟用」",
          "未配置" in _sw.detail and "未啟用" in _sw.detail)

    # ---------------- V/W. Duplicate suppression ----------------
    print("\nV/W. Duplicate suppression（desired state ≠ command edge）")
    for pcs, act, dt, soc, tag in (("CHARGING", "charge", DT_OFF_PEAK, 30.0, "V. CHARGE"),
                                   ("DISCHARGING", "discharge", DT_PEAK, 80.0, "W. DISCHARGE")):
        rv = Rig(pcs=pcs, local_now=dt, soc=soc,
                 last_control=LCRec(act, 5.0, 1000.0)).settle().arbitrate()
        check(f"★★ {tag}：已在目標狀態且目標未變 → 抑制，不建立新 leg",
              rv.outcome == ARB.ARB_SUPPRESSED
              and rv.reason == ARB.R_ALREADY_AT_DESIRED_STATE
              and rv.duplicate_suppressed is True and rv.authorization is None)
    # 🔴 要真正走到「目標變更」判定，設備必須正以**舊目標**運轉
    #    （否則 Control Authority 會先以功率不符判為 CONFLICT）。
    rt_chg = Rig(pcs="CHARGING", local_now=DT_OFF_PEAK, soc=30.0,
                 ess_over={"actual_active_power_kw": 9.2},
                 last_control=LCRec("charge", 9.0, 1000.0)).settle().arbitrate()
    check("  前提：設備正以舊目標運轉 → Authority 為 OWNED",
          rt_chg.authority_state == CA.AUTH_OWNED)
    check("★★ 同向但目標功率改變 → Fail Closed（OPEN DESIGN ITEM，不猜測）",
          rt_chg.outcome == ARB.ARB_BLOCKED
          and rt_chg.reason == ARB.R_TARGET_CHANGE_UNSUPPORTED
          and rt_chg.authorization is None)
    check("  目標不符時 Control Authority 本身也會先以功率不符擋下（雙重保護）",
          Rig(pcs="CHARGING", local_now=DT_OFF_PEAK, soc=30.0,
              last_control=LCRec("charge", 9.0, 1000.0)).settle().arbitrate()
          .authority_state == CA.AUTH_CONFLICT)

    # ---------------- X/Y/Z. Single-flight ----------------
    print("\nX/Y/Z. Single-flight 與授權生命週期")
    rig = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0).settle()
    first = rig.arbitrate()
    check("  前提：第一輪已授權", first.authorized is True)
    second = rig.arbitrate()
    check("★★ X. 已有未消費授權 → 第二張不得建立",
          second.outcome == ARB.ARB_PENDING
          and second.reason == ARB.R_PENDING_AUTHORIZATION
          and second.authorized is False)
    check("  X. PENDING 回報的是同一張票",
          second.authorization_id == first.authorization_id)
    rig.arbiter.cancel_pending("test")
    third = rig.arbitrate()
    check("★★ Y. 取消後可由 fresh 資料重新評估並重新授權",
          third.authorized is True and third.authorization_id != first.authorization_id)
    check("  Y. 被取消的票永久失效",
          first.authorization.state == PAZ.AUTHZ_CANCELLED
          and first.authorization.consume(PCI.CTRL_CHARGE, rig.dec_clk.t)[1]
          == PAZ.AZ_CANCELLED)
    rig.dec_clk.advance(TEST_CFG.authorization_ttl_sec + 1.0)
    rig.clk.advance(TEST_CFG.authorization_ttl_sec + 1.0)
    fourth = rig.arbitrate()
    check("★★ Z. 授權過期後，下一輪必須以 fresh 資料重新評估",
          third.authorization.state == PAZ.AUTHZ_EXPIRED
          and fourth.outcome in (ARB.ARB_AUTHORIZED, ARB.ARB_BLOCKED,
                                 ARB.ARB_NO_CONTROL)
          and fourth.reason != ARB.R_PENDING_AUTHORIZATION)

    # ---------------- AA~AD. Runtime 狀態 ----------------
    print("\nAA~AD. Runtime 狀態")
    rig2 = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0).settle()
    rt2 = RT.AutoControlRuntime(config=TEST_CFG,
                                observation_source=rig2.arbiter.evaluate)
    res = rt2.tick(RT.CycleInputs(is_owner=True))
    check("★★ AA. 已授權 → Runtime 可進 COMMAND_PENDING",
          res.state == RT.ST_COMMAND_PENDING
          and res.audit_event == RT.AUDIT_AUTHORIZED_WOULD_CHARGE
          and res.authorized is True and res.authorization_id is not None)
    check("★★ AI. 但 dispatched 仍為 False、dispatch_count 仍為 0",
          res.dispatched is False and rt2.dispatch_count == 0)
    check("  AA. COMMAND_PENDING 的語意是「等待未來的 Executor」",
          res.reason == RT.R_AUTHORIZED)
    for st in sorted(RT.EXECUTION_STATES):
        try:
            rt2._make(st, "x", "x")
            blocked = False
        except AssertionError:
            blocked = True
        check(f"★★ AB/AC/AD. 直接進入 {st} 仍被 guard 擋下", blocked)
    try:
        rt2._make(RT.ST_COMMAND_PENDING, "x", "x")
        pend_guard = False
    except AssertionError:
        pend_guard = True
    check("★★ COMMAND_PENDING 沒有授權票時同樣被擋下", pend_guard)

    seen = set()
    rig3 = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0).settle()
    rt3 = RT.AutoControlRuntime(config=TEST_CFG,
                                observation_source=rig3.arbiter.evaluate)
    for owner in (True, True, False, True):
        seen.add(rt3.tick(RT.CycleInputs(is_owner=owner)).state)
    seen.add(rt3.shutdown().state)
    check("★★ AB/AC/AD. 全程未出現 VERIFYING / OWNED_CHARGE / OWNED_DISCHARGE",
          not (seen & RT.EXECUTION_STATES))
    check("★★ 實際狀態全部落在 D.3-D 允許集合，且 dispatch_count=0",
          seen <= RT.D3D_REACHABLE_STATES and rt3.dispatch_count == 0)

    # ---------------- AE~AI. 零控制能力 ----------------
    print("\nAE~AI. 零控制能力（AST 靜態驗證）")
    PCS_PATH = {"device_control_operator", "device_control_menu",
                "device_control_scraper", "pcs_control_executor", "api_client",
                "charge_discharge_report", "report_monitor",
                "phase6_field_measure"}
    for mod in (ARB, PAZ):
        base = os.path.basename(mod.__file__)
        hit = _all_imports(_tree(mod)) & PCS_PATH
        check(f"★★ AE/AG. {base} 未 import 任何 PCS 控制路徑（命中={sorted(hit)}）",
              not hit)
    names = ({n.id for n in ast.walk(_tree(ARB)) if isinstance(n, ast.Name)}
             | {n.attr for n in ast.walk(_tree(ARB)) if isinstance(n, ast.Attribute)})
    check("★★ AF. 仲裁層未建立任何 executor 實例",
          not (names & {"PcsControlExecutor", "MockExecutor", "DryRunPipeline",
                        "operator_run", "RealPcsExecutor"}))
    check("★★ AH. 仲裁層未寫入 LastControl（未 import store、未呼叫 save）",
          "last_control_store" not in _all_imports(_tree(ARB))
          and not (names & {"LastControlStore", "save", "record_verified"}))
    check("★★ AG. 未出現 ReadBackVerifier / POST / requests",
          not (names & {"ReadBackVerifier", "post", "session"})
          and "requests" not in _all_imports(_tree(ARB)))
    lits = {n.value for n in ast.walk(_tree(ARB))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("★★ 仲裁層無任何 operator action 字面值",
          not (lits & {"pcs_charge", "pcs_discharge", "pcs_stop_power"}))
    check("★★ 未 import / reuse Field 的人工授權型別",
          "phase6_field_measure" not in _all_imports(_tree(ARB))
          and "phase6_field_measure" not in _all_imports(_tree(PAZ))
          and "ControlLegAuthorization" not in names)
    check("  仲裁層重用既有 Authority / Interlock / Safety（未建第二套）",
          {"control_authority", "safety_gate", "pcs_control_integration"}
          <= _all_imports(_tree(ARB)))

    # ---------------- audit ----------------
    print("\n稽核完整性")
    r = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0).settle().arbitrate()
    for f in ("original_decision_action", "fresh_decision_action",
              "original_ess_age_sec", "fresh_ess_age_sec",
              "original_meter_age_sec", "fresh_meter_age_sec",
              "pcs_state", "authority_state", "authority_reason",
              "direction_reason", "safety_allowed", "safety_reason",
              "safety_check_count", "alarm_source_complete",
              "authorization_id", "authorization_issued_at",
              "authorization_expires_at", "authorization_state",
              "grid_state", "tou_state", "soc_percent",
              "schedule_switch", "manual_switch", "arbitrated_at"):
        check(f"  audit 欄位存在且非 None：{f}", getattr(r, f) is not None)
    check("★★ audit 明確記錄 duplicate_suppressed 與 dispatched=False",
          r.duplicate_suppressed is False and r.dispatched is False)
    check("  as_dict 可序列化（含授權票）",
          isinstance(r.as_dict(), dict) and r.as_dict()["authorization"] is not None)

    # ---------------- production defaults ----------------
    print("\nProduction defaults")
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    # 🔁 max_power_kw 已依裁示寫入；未經裁示者仍為 None，dispatch 仍未啟用。
    check("★★ 測試注入未污染 production：Layer 2 仍未配置、dispatch 仍未啟用",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None
          and CFG.DEFAULT_CONTROL_CONFIG.max_power_kw == 150.0
          and __import__("pcs_auto_control_runtime").DISPATCH_ENABLED is False)
    check("★★ Safety / Authority / Policy 的 production 預設仍為 None",
          SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and CA.DEFAULT_AUTHORITY_POLICY.authority_ttl_sec is None
          and DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None)
    # 🔁 max_power_kw 寫入後更正：production 參數已齊備，仲裁層**可以**核發授權。
    #    這正是它的職責。真正的不變量是：仲裁層本身**結構上送不出任何指令**。
    _prod_arb = Rig(pcs="STANDBY", local_now=DT_OFF_PEAK, soc=30.0,
                    config=CFG.DEFAULT_CONTROL_CONFIG).settle().arbitrate()
    check("★★ 以 production 預設仲裁 → 仲裁層恆不 dispatch（dispatched 永遠 False）",
          _prod_arb.dispatched is False)
    check("★★ 且 runtime 的 dispatch 仍未啟用（授權 ≠ 送出）",
          __import__("pcs_auto_control_runtime").DISPATCH_ENABLED is False)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-D Arbitration + Authorization 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
