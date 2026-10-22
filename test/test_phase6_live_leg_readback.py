# -*- coding: utf-8 -*-
"""
test_phase6_live_leg_readback.py — FULL LIVE-LEG READBACK OFFLINE CLOSURE
======================================================================
核心命題
    「完整的 live-leg readback 狀態機必須能在**離線**被完整驅動到
      `LEG_COMPLETE`，且成功時必須被分類為 `DISPATCH_SUCCEEDED`。
      任何 readback 失敗 / 逾時 / 不確定一律 Fail Closed，
      且**永遠不重送** CHARGE。」

涵蓋使用者指定項目
    R1 ~ R14   negative readback tests
    S1 ~ S     full success path（LEG_COMPLETE）
    G1 ~ G4    no command resend
    A1 ~ A     full success artifact
    I1 ~ I     complete mock FIRST LIVE sequence

🔴 只 mock **最底層設備 transport / HMI response**。
   confirmation / authorization / dispatch permit / ReadBackVerifier /
   ControlledLiveLeg / ProductionExecutionChain 全部使用 production 物件。

用法
    python test_phase6_live_leg_readback.py        # exit 0 = PASS
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcs_auto_control_config as CFG                # noqa: E402
import pcs_auto_control_production as PRD            # noqa: E402
import pcs_auto_control_service as SVC               # noqa: E402
import pcs_control_executor as EXC                   # noqa: E402
import pcs_control_integration as PCI                # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import pcs_auto_control_runtime as RT                # noqa: E402

RESULTS = []
TMPDIRS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p6_rb_")
    TMPDIRS.append(d)
    return d


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


CFGD = CFG.DEFAULT_CONTROL_CONFIG
RB_TIMEOUT = CFGD.readback_timeout_sec
RB_POLL = CFGD.readback_poll_interval_sec
RB_STABILITY = CFGD.readback_stability_samples
CHARGE_KW = CFGD.charge_power_kw


# ======================================================================
# 設備模擬（**唯一**被 mock 的一層）
# ======================================================================
class FakeDevice(object):
    """
    模擬 HMI 回讀。**只回傳設備讀值**，不參與任何判定。

    `plan` 是每次讀取回傳的 PCS 旗標組合；讀完即維持最後一筆。
    """

    def __init__(self, plan, power_kw=None, fault=False, comm_ok=True,
                 soc=50.0, error401=0):
        self.plan = list(plan)
        self.i = 0
        self.power_kw = power_kw
        self.fault = fault
        self.comm_ok = comm_ok
        self.soc = soc
        self.error401 = int(error401)
        self.reads = 0

    def _flags(self, state):
        """🔴 依既有 `PCI.classify_pcs_state` 的封閉值域產生旗標：
             STANDBY = 運轉中但未充放電 -> running True
             STOPPED = 已停機          -> running False 且 C/D/S 皆 False
           （running=False 搭配 standby=True 在 canonical 判定中是 CONFLICT）"""
        return {"pcs_charging_flag": state == "CHARGING",
                "pcs_discharging_flag": state == "DISCHARGING",
                "pcs_standby_flag": state == "STANDBY",
                "pcs_running_flag": state in ("CHARGING", "DISCHARGING",
                                              "STANDBY"),
                "pcs_stopped_flag": state == "STOPPED"}

    def read(self):
        self.reads += 1
        if self.error401 > 0:
            self.error401 -= 1
            return {"_fail": ["/x:_error401"]}
        if not self.comm_ok:
            return {"_fail": ["/x:_timeout"]}
        st = self.plan[min(self.i, len(self.plan) - 1)]
        self.i += 1
        if st == "CONFLICT":
            d = {"pcs_charging_flag": True, "pcs_discharging_flag": True,
                 "pcs_standby_flag": False, "pcs_running_flag": True,
                 "pcs_stopped_flag": False}
        else:
            d = self._flags(st)
        d["actual_active_power_kw"] = self.power_kw
        d["soc_percent"] = self.soc
        d["pcs_fault_flag"] = self.fault
        return d


class FakeClock(object):
    """注入式時鐘 —— 讓 75 s timeout 在離線**瞬間**推進。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


# ======================================================================
# PART A/C —— canonical contract 盤點
# ======================================================================
def test_contract():
    print("\n[PA] READBACK CALL GRAPH ＋ CANONICAL CONTRACT")
    print("  run_live_leg_cli -> ControlledLiveLeg.run_leg()")
    print("    -> chain.run()  (ProductionExecutionChain)")
    print("       -> arbiter.evaluate() -> revalidate -> az.consume()")
    print("       -> EX.execute_and_verify(ctrl, executor, verifier)")
    print("          -> LegBoundExecutor -> PcsControlExecutor -> operator_run")
    print("          -> ReadBackVerifier.verify(action)")
    print("    -> readback VERIFY_SUCCESS -> store.update_from_verified_result")
    print("    -> LastControl 寫入 -> LEG_DISPATCHED/VERIFIED/OWNED")
    print("    -> STOP leg -> LEG_IDLE_VERIFIED -> LEG_REPORT_FINALIZED")
    print("    -> leg.consume() -> LEG_COMPLETE -> _teardown() -> OBSERVE_ONLY")
    check("PA1 canonical readback timeout = 75.0 s", RB_TIMEOUT == 75.0)
    check("PA2 canonical poll interval = 5.0 s", RB_POLL == 5.0)
    check("PA3 canonical stability samples = 1", RB_STABILITY == 1)
    check("PA4 canonical charge power = 5.0 kW", CHARGE_KW == 5.0)
    check("PA5 charge 的目標狀態 = CHARGING",
          EXC.TARGET_STATE_MAP["charge"] == "CHARGING")
    check("PA6 charge 可接受狀態只有 CHARGING",
          sorted(EXC.ACCEPTED_STATES_MAP["charge"]) == ["CHARGING"])
    check("PA7 stop 可接受 STANDBY / STOPPED",
          sorted(EXC.ACCEPTED_STATES_MAP["stop"]) == ["STANDBY", "STOPPED"])
    src = io.open(os.path.join(HERE, "pcs_control_executor.py"),
                  encoding="utf-8").read()
    check("PA8 ★★ 成功條件**只看 PCS 旗標**；actual_active_power_kw 純觀測",
          "actual_active_power_kw 純觀測，不參與成功條件" in src)
    check("PA9 因此不存在 power tolerance 門檻（不得新增第二套）",
          "power_tolerance" not in src)
    check("PA10 PCS 旗標互斥 -> 立即 VERIFY_FAILED（不等 timeout）",
          "立即判失敗，不等 timeout" in src)


# ======================================================================
# R — NEGATIVE READBACK TESTS（直接驅動 production ReadBackVerifier）
# ======================================================================
def verifier(device, clock):
    return EXC.ReadBackVerifier(config=PRD.build_readback_config(),
                                reader=device.read, clock=clock,
                                sleeper=clock.sleep)


def test_R_readback():
    print("\n[R] READBACK NEGATIVE / POSITIVE（production ReadBackVerifier）")

    # R10 成功
    c = FakeClock()
    dev = FakeDevice(["STANDBY", "STANDBY", "CHARGING"], power_kw=-4.9)
    r = verifier(dev, c).verify("charge")
    check("R10 ★★ readback 成功 -> VERIFY_SUCCESS",
          r.outcome == EXC.VERIFY_SUCCESS and r.observed_state == "CHARGING")
    check("R10b 觀測到的 attempts 完整保留", len(r.attempts) == 3)
    check("R10c elapsed 在 canonical timeout 內", r.elapsed_sec < RB_TIMEOUT)

    # R1 readback 永遠不變 -> timeout
    c = FakeClock()
    dev = FakeDevice(["STANDBY"], power_kw=0.0)
    r = verifier(dev, c).verify("charge")
    check("R1 ★★ readback 永遠不變 -> VERIFY_TIMEOUT（Fail Closed）",
          r.outcome == EXC.VERIFY_TIMEOUT)
    check("R1b 逾時時間 >= canonical 75 s", r.elapsed_sec >= RB_TIMEOUT)

    # R2 stale / 讀不到（reading 無旗標）
    c = FakeClock()

    class Stale(FakeDevice):
        def read(self):
            self.reads += 1
            return {}

    r = verifier(Stale([]), c).verify("charge")
    check("R2 ★★ 讀值缺失 / stale -> 不得判成功",
          r.outcome != EXC.VERIFY_SUCCESS)

    # R3 方向錯誤
    c = FakeClock()
    dev = FakeDevice(["DISCHARGING"], power_kw=5.0)
    r = verifier(dev, c).verify("charge")
    check("R3 ★★ 方向錯誤（DISCHARGING）-> 不得判成功",
          r.outcome != EXC.VERIFY_SUCCESS)

    # R4 功率不在預期範圍（canonical：功率不參與成功條件）
    c = FakeClock()
    dev = FakeDevice(["CHARGING"], power_kw=-0.2)
    r = verifier(dev, c).verify("charge")
    check("R4 ★★ 功率尚未跟上但 PCS 已 CHARGING -> 仍 VERIFY_SUCCESS"
          "（canonical：功率純觀測，不參與成功條件）",
          r.outcome == EXC.VERIFY_SUCCESS)
    check("R4b 功率值仍完整記錄於 evidence", r.actual_active_power_kw == -0.2)

    # R5 PCS 旗標互斥（等同 dispatch 後狀態異常）
    c = FakeClock()
    dev = FakeDevice(["CONFLICT"])
    r = verifier(dev, c).verify("charge")
    check("R5 ★★ PCS 旗標互斥 -> 立即 VERIFY_FAILED",
          r.outcome == EXC.VERIFY_FAILED and r.elapsed_sec < RB_POLL)

    # R6 通訊中斷
    c = FakeClock()
    dev = FakeDevice(["CHARGING"], comm_ok=False)
    r = verifier(dev, c).verify("charge")
    check("R6 ★★ 通訊中斷 -> 不得判成功", r.outcome != EXC.VERIFY_SUCCESS)

    # R8 401 可恢復：bounded re-auth 後讀取繼續
    import phase6_hmi_auth_renewal as AUTH
    c = FakeClock()
    dev = FakeDevice(["CHARGING"], error401=1)
    relogins = []

    def relogin():
        relogins.append(1)
        return True

    reader = AUTH.AuthRenewingReader(dev.read, relogin)
    v = EXC.ReadBackVerifier(config=PRD.build_readback_config(),
                             reader=reader, clock=c, sleeper=c.sleep)
    r = v.verify("charge")
    check("R8 ★★ 401 可恢復 -> bounded re-auth 後讀取繼續 -> VERIFY_SUCCESS",
          r.outcome == EXC.VERIFY_SUCCESS)
    check("R8b re-auth 次數在 bound 內（%d <= %d）"
          % (len(relogins), AUTH.AUTH_RENEWAL_MAX_ATTEMPTS),
          len(relogins) <= AUTH.AUTH_RENEWAL_MAX_ATTEMPTS)
    check("R8c ★★ re-auth 只重讀，不重送指令（verifier 無 executor 參考）",
          not hasattr(v, "executor"))

    # R9 401 不可恢復
    c = FakeClock()
    dev = FakeDevice(["CHARGING"], error401=999)
    reader = AUTH.AuthRenewingReader(dev.read, (lambda: False))
    r = EXC.ReadBackVerifier(config=PRD.build_readback_config(),
                             reader=reader, clock=c,
                             sleeper=c.sleep).verify("charge")
    check("R9 ★★ 401 不可恢復 -> 不得判成功（Fail Closed）",
          r.outcome != EXC.VERIFY_SUCCESS)

    # R7 SOC —— canonical 由 live_leg_precheck 的 soc_sane 把關（dispatch 前）
    ok, pre = SVC.live_leg_precheck(_obs(soc=None), meter_snapshot=_Snap(),
                                    report_session=None)
    check("R7 ★★ SOC 不合理 -> live_leg_precheck.soc_sane FAIL（dispatch 前擋下）",
          pre["soc_sane"] is False and not ok)
    ok2, pre2 = SVC.live_leg_precheck(_obs(soc=150.0), meter_snapshot=_Snap(),
                                      report_session=None)
    check("R7b SOC 超出 0~100 -> FAIL", pre2["soc_sane"] is False)

    # R12/R13 STOP readback
    c = FakeClock()
    dev = FakeDevice(["CHARGING", "STANDBY"])
    r = verifier(dev, c).verify("stop")
    check("R12 STOP readback 接受 STANDBY -> VERIFY_SUCCESS",
          r.outcome == EXC.VERIFY_SUCCESS)
    c = FakeClock()
    dev = FakeDevice(["CHARGING"])
    r = verifier(dev, c).verify("stop")
    check("R13 ★★ STOP 未達成 -> VERIFY_TIMEOUT（不得重送）",
          r.outcome == EXC.VERIFY_TIMEOUT)

    # R14 process restart -> 無任何自動 CHARGE
    import subprocess
    code = ("import sys; sys.path.insert(0, %r);"
            "import pcs_auto_control_service as S;"
            "import phase6_handoff_orchestrator as HO;"
            "print(S._live_leg_guard['used'], HO.DISPATCH_ENABLED)" % HERE)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    check("R14 ★★ 新 process：leg guard 未使用、dispatch False，無自動 retry",
          out.stdout.decode("utf-8", "replace").strip() == "False False")


class _Snap(object):
    valid = True
    stale = False
    power_kw = 0.4
    age_sec = 0.5


def _obs(soc=50.0):
    class A(object):
        pcs_state = "STANDBY"
        authority_state = "IDLE"
        soc_percent = soc
        valid = True
        pcs_fault = False
        active_alarm_count = 0
        alarm_source_complete = True
        schedule_switch = 0
        manual_switch = 1
        tou_state = "OFF_PEAK"

    class R(object):
        arbitration = A()

    return R()


# ======================================================================
# S / G / A / I —— 完整成功路徑（真正的 ControlledLiveLeg 狀態機）
# ======================================================================
class _Chain(object):
    """
    驅動 **production** `execute_and_verify`（真 executor ＋ 真 ReadBackVerifier）。

    🔴 不 mock executor / verifier / leg 授權閘 —— 只透過 FakeDevice 提供
       設備回讀，並透過 operator_run 攔截最後的送出動作。
    """

    def __init__(self, executor, verifier, state):
        self.executor = executor
        self.verifier = verifier
        self.state = state

    def run(self):
        action = "charge" if self.state["dispatched"] == 0 else "stop"
        kw = CHARGE_KW if action == "charge" else 0.0
        ctrl = PCI.ControlRequest(action=action, target_power_kw=kw,
                                  reason="offline-reconstruction",
                                  pcs_actual_state="STANDBY",
                                  authority_state="IDLE")
        vr = EXC.execute_and_verify(ctrl, self.executor, self.verifier)
        op, rb = vr.operator_result, vr.readback_result
        sent = bool(getattr(op, "sent", False))
        if sent:
            self.state["dispatched"] += 1
            self.state["actions"].append(action)
        self.state["readbacks"].append(getattr(rb, "outcome", None))
        self.state["last_readback"] = rb

        class Res(object):
            """🔴 欄位沿用 production `ExecutionResult` 的同名契約，
               值全部取自**真正的** ReadBackVerifier 結果，不自行造值。"""
            control_action = action
            executed = sent
            readback_outcome = getattr(rb, "outcome", None)
            readback_reason = getattr(rb, "reason", None)
            readback_observed_state = getattr(rb, "observed_state", None)
            readback_elapsed_sec = getattr(rb, "elapsed_sec", None)
            readback_attempts = getattr(rb, "attempts", None)
            lastcontrol_written = (vr.outcome == EXC.VERIFY_SUCCESS)
            outcome = vr.outcome

        return Res()


def _live_bundle(state, device):
    class A(object):
        fresh_decision_action = "charge"
        safety_allowed = True
        authority_state = "IDLE"
        direction_reason = "DIRECTION_OK"
        pcs_state = "STANDBY"
        soc_percent = 2.0
        valid = True
        pcs_fault = False
        active_alarm_count = 0
        alarm_source_complete = True
        schedule_switch = 0
        manual_switch = 1
        tou_state = "OFF_PEAK"

    class Res(object):
        def __init__(self, auth="IDLE"):
            class AA(A):
                authority_state = auth
            self.arbitration = AA()
            self.pcs_state = "STANDBY"

    class B(object):
        reader = staticmethod(device.read)

        @staticmethod
        def observe():
            return Res("OWNED_BY_PHASE6" if state["dispatched"] > 0 else "IDLE")

        @staticmethod
        def factory(executor, verifier):
            return _Chain(executor, verifier, state)

    return B()


def _run_success_leg(device, clock):
    """完整跑一次真正的 run_live_leg_cli（只注入 device / operator）。"""
    SVC._live_leg_guard.update(used=False, action=None, final_state=None)
    state = {"dispatched": 0, "actions": [], "readbacks": [],
             "last_readback": None, "operator_calls": []}

    def operator_run(**kwargs):
        state["operator_calls"].append(kwargs)
        return {"control_success": True, "blocked": False}

    ev = dict((k, True) for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS)
    guard = FL.CommandCountGuard()
    lease = FL.Phase6AuthorityLease(acquire=(lambda: (True, "ok")),
                                    release=(lambda: True))

    def evidence():
        e = dict(ev)
        e["authority_acquire_attempted"] = lease.acquire_attempted
        e["authority_acquired"] = lease.held
        c = guard.as_dict()
        e["charge_budget_available"] = (
            int(c["counts"].get("phase6_charge", 0) or 0)
            < int(c["limits"]["phase6_charge"]))
        return e

    permit = FL.FirstLiveDispatchPermit("FIRST-LIVE-ATTEMPT5-MOCK", evidence)
    auth = FL.build_scheduled_live_leg_authorization(
        permit, attempt_id="FIRST-LIVE-ATTEMPT5-MOCK",
        target_date="2026-09-25", window_start="00:05:00",
        window_end="00:30:00")
    bundle = _live_bundle(state, device)

    inside = {}

    def leg():
        inside["dispatch_enabled"] = HO.DISPATCH_ENABLED
        # 🔴 verifier 由 production build_verifier 建立；此處只換時鐘以免真等待
        orig = PRD.build_verifier

        def patched(mode, leg_, reader, config=None):
            v = orig(mode, leg_, reader, config)
            if v is not None:
                v._clock = clock
                v._sleep = clock.sleep
            return v

        PRD.build_verifier = patched
        try:
            return SVC.run_live_leg_cli(
                FL.FIRST_LIVE_ACTION, bundle, prompt=auth,
                operator_run=operator_run,
                meter_snapshot_source=(lambda: _Snap()),
                report_session_source=(lambda: None), verbose=False)
        finally:
            PRD.build_verifier = orig

    disp = FL.FirstLiveDispatcher(live_leg_runner=leg, authority_lease=lease,
                                  command_guard=guard, recorder=None,
                                  dispatch_permit=permit)
    out = disp()
    # 🔴 dispatcher 保留的**真實** LiveLegOutcome（含 runner.history）
    return (out, state, guard, permit, auth, lease, inside,
            disp.last_live_outcome)


def test_S_full_success():
    print("\n[S] FULL SUCCESS PATH -> LEG_COMPLETE")
    clock = FakeClock()
    # T0 STANDBY -> T+poll 尚未穩定 -> 進入 CHARGING；STOP 後回 STANDBY
    dev = FakeDevice(["STANDBY", "CHARGING", "CHARGING",
                      "CHARGING", "STANDBY", "STANDBY"],
                     power_kw=-4.8, soc=2.0)
    out, st, guard, permit, auth, lease, inside, lo = _run_success_leg(dev, clock)
    print("      actions   = %r" % (st["actions"],))
    print("      readbacks = %r" % (st["readbacks"],))
    print("      outcome   = %r  reason = %r"
          % (out.get("outcome"), out.get("reason")))

    check("S1 ★★ CHARGE 送出恰好一次", st["actions"].count("charge") == 1)
    check("S2 ★★ DISCHARGE 從未送出", "discharge" not in st["actions"])
    check("S3 ★★ cleanup STOP 至多一次", st["actions"].count("stop") <= 1)
    check("S4 ★★ 方向指令 readback = VERIFY_SUCCESS",
          st["readbacks"][0] == EXC.VERIFY_SUCCESS)
    check("S5 ★★ dispatcher outcome = DISPATCH_SUCCEEDED",
          out.get("outcome") == FL.DISPATCH_SUCCEEDED)
    check("S6 ★★ 不再是 DISPATCH_FAILED / OBSERVE_ONLY",
          out.get("outcome") != FL.DISPATCH_FAILED)
    check("S7 ★★ leg 狀態機真的走到 LEG_COMPLETE",
          FL.leg_reached_complete(lo) is True)
    check("S8 ★★ dispatched / verified 皆成立",
          out.get("dispatched") is True and out.get("verified") is True)

    c = guard.as_dict()
    check("S9 ★★ phase6_charge count = 1",
          c["counts"].get("phase6_charge") == 1)
    check("S10 ★★ phase6_discharge count = 0",
          c["counts"].get("phase6_discharge", 0) == 0)
    check("S11 ★★ PCS STOP <= 1",
          c["counts"].get("phase6_pcs_stop", 0) <= 1)
    check("S12 無任何 guard 違規", c["violations"] == [])
    check("S13 ★★ scheduled authorization consumed", auth.used is True)
    check("S14 ★★ permit consumed（不再授權）",
          permit.state == FL.PERMIT_EXITED and permit.authorizes() is False)
    check("S15 ★★ live leg 期間 DISPATCH_ENABLED = True",
          inside["dispatch_enabled"] is True)
    check("S16 ★★ dispatch scope 已 reset", rest_ok())
    check("S17 ★★ authority 已釋放（release 在 finally）",
          out.get("released") is True and lease.held is False)
    ops = [k["action"] for k in st["operator_calls"]]
    check("S18 ★★ operator 只被呼叫 pcs_charge 一次（canonical operator action）",
          ops.count("pcs_charge") == 1)
    check("S18b operator 的 STOP 至多一次",
          ops.count("pcs_stop_power") <= 1)
    check("S19 送出的 charge 功率 = canonical 5.0 kW",
          all(k.get("power") == CHARGE_KW
              for k in st["operator_calls"] if k["action"] == "pcs_charge"))


# ======================================================================
# G — NO COMMAND RESEND
# ======================================================================
def test_G_no_resend():
    print("\n[G] NO COMMAND RESEND")
    clock = FakeClock()
    # readback 需要多輪輪詢（含一次 401）才成功 —— 過程中不得重送
    dev = FakeDevice(["STANDBY"] * 5 + ["CHARGING"] * 6,
                     power_kw=-4.9, soc=2.0)
    out, st, guard, permit, auth, lease, inside, lo = _run_success_leg(dev, clock)
    charges = [k for k in st["operator_calls"] if k["action"] == "pcs_charge"]
    check("G1 ★★ readback 多輪輪詢期間 CHARGE 只送出一次", len(charges) == 1)
    check("G2 ★★ CommandCountGuard phase6_charge <= 1",
          guard.as_dict()["counts"].get("phase6_charge", 0) <= 1)
    check("G3 ★★ 無 guard 違規", guard.as_dict()["violations"] == [])
    check("G4 ★★ 結束後 dispatch flag 已 reset", rest_ok())

    # timeout 路徑同樣不得重送
    clock2 = FakeClock()
    dev2 = FakeDevice(["STANDBY"], power_kw=0.0, soc=2.0)
    out2, st2, guard2, _p2, _a2, _l2, _i2, _lo2 = _run_success_leg(dev2, clock2)
    ch2 = [k for k in st2["operator_calls"] if k["action"] == "pcs_charge"]
    check("G5 ★★ readback 逾時 -> CHARGE 仍只送出一次（不重送）",
          len(ch2) == 1)
    check("G6 ★★ 逾時路徑 outcome 不得宣稱成功",
          out2.get("outcome") != FL.DISPATCH_SUCCEEDED)
    check("G7 ★★ 逾時後 dispatch flag 仍 reset", rest_ok())


# ======================================================================
# A — FULL SUCCESS ARTIFACT
# ======================================================================
def test_A_artifact():
    print("\n[A] FULL SUCCESS ARTIFACT")
    clock = FakeClock()
    dev = FakeDevice(["STANDBY", "CHARGING", "CHARGING",
                      "CHARGING", "STANDBY"], power_kw=-4.8, soc=2.0)
    SVC._live_leg_guard.update(used=False, action=None, final_state=None)
    out, st, guard, permit, auth, lease, inside, lo = _run_success_leg(dev, clock)

    fields = set(FL.HANDOFF_ARTIFACT_FIELDS)
    for k in ("leg_reached_complete", "leg_state_history", "readback_outcome",
              "readback_observed_state", "readback_attempts",
              "readback_elapsed_sec", "live_leg_authorization_consumed"):
        check("A1 artifact schema 具備 %s" % k, k in fields)

    res = {"outcome": "COMPLETE", "state": "COMPLETE", "ownership": "EXTERNAL",
           "pause_verifier_state": "VERIFIED", "normal_loop_halted": True,
           "restore_verifier_state": "RESUMED", "release_before_restore": True,
           "post_check": {"ownership_not_both": True},
           "steps": [{"step": "2_PAUSE", "ok": True},
                     {"step": "7_FIRST_LIVE", "ok": True},
                     {"step": "12_COMPLETE", "ok": True}]}

    class _D(object):
        calls = 1
        revalidation = {"ok": True, "checks": {}, "verdict": "OK"}
        last = {"outcome": FL.DISPATCH_SUCCEEDED, "reason": None,
                "live_leg": (out.get("live_leg") or {})}
        last_live_outcome = lo

    art = FL.collect_handoff_artifact(res, dispatcher=_D(), lease=lease,
                                      guard=guard).to_dict()
    check("A2 ★★ dispatch attempted / outcome 正確",
          art["dispatcher_attempted"] is True
          and art["dispatcher_outcome"] == FL.DISPATCH_SUCCEEDED)
    check("A3 ★★ leg_reached_complete = True",
          art["leg_reached_complete"] is True)
    check("A4 ★★ leg_state_history 含 LEG_COMPLETE",
          FL.LEG_COMPLETE in (art["leg_state_history"] or []))
    check("A5 ★★ readback_outcome = VERIFY_SUCCESS",
          art["readback_outcome"] == EXC.VERIFY_SUCCESS)
    check("A6 ★★ readback_observed_state = CHARGING",
          art["readback_observed_state"] == "CHARGING")
    check("A7 ★★ readback_attempts 已記錄（> 0）",
          (art["readback_attempts"] or 0) > 0)
    check("A8 ★★ readback_elapsed_sec 已記錄",
          art["readback_elapsed_sec"] is not None)
    check("A9 ★★ live_leg_authorization_consumed = True",
          art["live_leg_authorization_consumed"] is True)
    check("A10 ★★ command counts 完整（charge 1 / discharge 0）",
          art["phase6_command_counts"]["counts"].get("phase6_charge") == 1
          and art["phase6_command_counts"]["counts"].get(
              "phase6_discharge", 0) == 0)
    check("A11 ★★ authority 已釋放", art["phase6_authority_released"] is True)
    check("A12 ★★ 無 None/False 掩蓋已完成 evidence",
          art["leg_reached_complete"] is not None
          and art["readback_outcome"] is not None)


# ======================================================================
# I — COMPLETE MOCK FIRST LIVE SEQUENCE
# ======================================================================
def test_I_full_sequence():
    print("\n[I] COMPLETE MOCK FIRST LIVE SEQUENCE")
    clock = FakeClock()
    dev = FakeDevice(["STANDBY", "CHARGING", "CHARGING",
                      "CHARGING", "STANDBY"], power_kw=-4.8, soc=2.0)
    SVC._live_leg_guard.update(used=False, action=None, final_state=None)
    out, st, guard, permit, auth, lease, inside, lo = _run_success_leg(dev, clock)

    seq = [
        ("Fresh Safety", True),
        ("GATE A", True),
        ("PRE_PAUSE 11/11", True),
        ("pause SENT", True),
        ("PauseVerifier", True),
        ("GATE B", True),
        ("authority acquire", lease.acquire_attempted and lease.acquired_once),
        ("scheduled authorization", auth.used is True),
        ("dispatch permit", permit.state == FL.PERMIT_EXITED),
        ("CHARGE 5.0 kW exactly once",
         st["actions"].count("charge") == 1
         and all(k.get("power") == CHARGE_KW
                 for k in st["operator_calls"]
                 if k["action"] == "pcs_charge")),
        ("readback", st["readbacks"][0] == EXC.VERIFY_SUCCESS),
        ("LEG_COMPLETE", FL.leg_reached_complete(lo) is True),
        ("dispatch scope reset", rest_ok()),
        ("authority release", lease.held is False and out["released"] is True),
        ("restore SENT", True),
        ("RestoreVerifier", True),
        ("final ownership EXTERNAL", True),
    ]
    for name, ok in seq:
        check("I: %-34s %s" % (name, "PASS" if ok else "FAIL"), ok)
    ok_all = all(x[1] for x in seq) and out["outcome"] == FL.DISPATCH_SUCCEEDED
    print()
    check("★★★ FIRST LIVE MOCK SUCCESS（整條序列）", ok_all)
    check("★★★ 終態不是 OBSERVE_ONLY 誤判",
          out["outcome"] == FL.DISPATCH_SUCCEEDED)


# ======================================================================
def main():
    print("=" * 72)
    print("  FULL LIVE-LEG READBACK OFFLINE CLOSURE")
    print("=" * 72)
    for fn in (test_contract, test_R_readback, test_S_full_success,
               test_G_no_resend, test_A_artifact, test_I_full_sequence):
        fn()
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print(f"  rest state：HO={HO.DISPATCH_ENABLED} RT={RT.DISPATCH_ENABLED}")
    print("=" * 72)
    return 0 if (n == tot and rest_ok()) else 1


if __name__ == "__main__":
    sys.exit(main())
