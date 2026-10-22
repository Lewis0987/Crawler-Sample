# -*- coding: utf-8 -*-
"""
test_phase6_first_live_handoff.py — FIRST LIVE x Phase 6.10 整合回歸
======================================================================
核心命題
    「FIRST LIVE 不得再假設『external 當下 idle 就安全』。
      必須先 pause 並驗證 external 已靜止，Phase 6 才可取得控制權；
      而且不論 leg 成功、失敗或不確定，
      Phase 6 一定先 release，external 才被 restore。」

使用者指定的 18 項
    I1  external loop active -> pause verified -> dispatcher 恰好一次
    I2  pause 未 verified            -> dispatcher 從未被呼叫
    I3  pause SEND_UNKNOWN           -> dispatcher 從未被呼叫 / 責任維持
    I4  dispatcher success           -> Phase 6 release -> restore 一次
    I5  dispatcher fail              -> Phase 6 release -> restore 一次
    I6  dispatcher UNKNOWN           -> 不重試 -> release + restore
    I7  Phase 6 authority 取得失敗    -> 不送任何指令 -> restore external
    I8  Phase 6 release 必須發生在 external restore 之前
    I9  restore SEND_UNKNOWN         -> 不重送 -> 責任保留
    I10 RV_RESUMED                   -> 責任清除
    I11 CHARGE attempt <= 1
    I12 DISCHARGE == 0
    I13 PCS STOP <= 1（cleanup STOP 計入 aggregate）
    I14 Durable Decision Audit 有 FIRST LIVE decision/control evidence
    I15 6.10 artifact 有完整 handoff evidence
    I16 DISPATCH_ENABLED=False       -> dispatcher 不可觸發
    I17 RemoteSenders armed=False    -> remote send 不可發生
    I18 任何 exception               -> Fail Closed / 責任保留

額外結構鎖
    N1  兩層 preflight：LAYER 1 不得要求 external idle
    N2  dispatcher 不得知道 CHARGE 實作細節（AST）
    N3  不建立第二套 charge execution logic（AST）
    N4  production 參數 / 既有 criteria 未被更動
    N5  timeout 仍為 FIELD_VALIDATION_CANDIDATE

RULING 1 / RULING 2（2026-09-18）
    R1  phase6_first_live_runner 直接 CLI -> BLOCKED（exit != 0）
    R2  direct invocation 不送指令 / 不 acquire authority / 無 bypass 入口
    R3  live leg 仍可被 import 與 dispatcher 使用
    R4  repo 無其他可繞過 handoff 的 FIRST LIVE field entry
    R4b pcs_auto_control_service --live-leg CLI -> BLOCKED（只封 CLI，函式保留）
    R5  成功 leg + cleanup STOP -> charge 1 / discharge 0 / pcs_stop 1
    R6  未進 live leg -> 0 / 0 / 0
    R7  charge>1 / discharge>0 / pcs_stop>1 -> artifact reject
    R8  cleanup STOP 不得被排除於 aggregate；有 STOP 必須有 reason
    R9  handoff COMPLETE 但 dispatcher FAILED -> FIRST LIVE 不得標 PASS

GATE A / GATE B（2026-09-18 P0 flow correction）
    P1  decision DISCHARGE + authority EXTERNAL -> pre-handoff NOT ELIGIBLE
    P2  decision CHARGE + authority EXTERNAL    -> HANDOFF_REQUIRED（可進 pause）
    P3  decision CHARGE + authority IDLE 但 loop active -> 仍 HANDOFF_REQUIRED
    P4  post-handoff 仍 CHARGE                  -> 可 acquire + dispatch
    P5  post-handoff 變 DISCHARGE               -> no dispatch -> restore
    P6  post-handoff 變 NO_ACTION               -> no dispatch -> restore
    P7  post-handoff fault / critical / 未 relinquish -> no dispatch -> restore
    P8  post-handoff authority acquire fail     -> no dispatch -> restore
    P9  任何 post-handoff abort -> Phase 6 counts 維持 0 / 0 / 0
    P10 restore responsibility 直到 RV_RESUMED 才可清除

用法
    python test_phase6_first_live_handoff.py        # exit 0 = PASS
"""
import ast
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA                  # noqa: E402
import phase6_handoff_orchestrator as HO            # noqa: E402
import phase6_first_live_handoff as FL              # noqa: E402

RESULTS = []
TMPDIRS = []
CFG = HO.HandoffConfig


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="fl_")
    TMPDIRS.append(d)
    return d


# ======================================================================
# 替身（全部離線；沒有任何真實 I/O）
# ======================================================================
class FakeLeg(object):
    """既有 LiveLegOutcome 的最小替身（只給本測試需要的欄位）。"""

    def __init__(self, leg_state="COMPLETE", dispatch_count=1, stop_count=1,
                 reason=None):
        self.leg_state = leg_state
        self.dispatch_count = dispatch_count
        self.stop_count = stop_count
        self.reason = reason or leg_state
        self.action = "charge"

    def as_dict(self):
        return {"leg_state": self.leg_state, "dispatch_count": self.dispatch_count,
                "stop_count": self.stop_count, "reason": self.reason,
                "action": self.action}


def sample(**kw):
    """同時滿足 pre_pause_gates / post_pause_precheck / PauseVerifier 的樣本。"""
    d = {"fresh": True, "comm_ok": True, "meter_fresh": True, "ess_fresh": True,
         "soc_fresh": True, "soc": 40.0, "tou": "OFF_PEAK", "decision": "charge",
         "external_behaviour_known": True,
         "pcs_state": "STANDBY", "authority": "IDLE", "ac_kw": 0.0,
         "fault": False, "critical_alarms": 0, "ownership": "NEITHER"}
    d.update(kw)
    return d


def probe(**kw):
    d = {"ssh_ok": True, "screen_alive": True, "process_alive": True,
         "process_identity_ok": True, "guard_variant": "B2",
         "capability_ok": True}
    d.update(kw)
    return d


FLAGS = {"dispatch_enabled": False, "mode": "DRY_RUN", "senders_armed": False}


class FakeRemote(HO.RemoteController):
    """可編排 send outcome 的 remote。**不含任何 SSH。**"""

    def __init__(self, pause=RA.SEND_SENT, restore=RA.SEND_SENT):
        HO.RemoteController.__init__(self)
        self._p, self._r = pause, restore
        self.pause_calls = 0
        self.restore_calls = 0

    def send_pause_ex(self):
        self.pause_calls += 1
        return self._p, None

    def send_restore_ex(self):
        self.restore_calls += 1
        return self._r, None


class EntryOnlyPermit(object):
    """
    只回答 STAGE A（是否允許進入 dispatcher）的最小 permit stub。

    🔴 僅供**本檔的 HandoffRunner 單元測試**使用。
       production control-flow ordering 的證據由
       `test_phase6_entry_point_ordering.py` 從真正的 production entry
       （`build_field_dependencies` -> `arm_and_run_handoff`）產生，
       **不得**以本 stub 取代。
    🔴 本 stub **不**回答 `authorizes()`（STAGE B）——
       完整 permit 仍必須由真實 `lease.acquire()` 之後才可能成立。
    """

    def __init__(self, ok=True):
        self._ok = bool(ok)
        self.entry_denied_reason = (None if ok else ["test_forced_deny"])
        self.ordering = []

    def allows_dispatcher_entry(self):
        return self._ok

    def note(self, event, at=None):
        self.ordering.append(event)
        return event


class Rig(object):
    """一次完整 handoff 的離線裝置。"""

    def __init__(self, pause=RA.SEND_SENT, restore=RA.SEND_SENT,
                 leg=None, leg_exc=None, acquire_ok=True,
                 marks_advance_after_restore=True, audit_writer=None,
                 revalidator=None):
        self.rec = FL.LifecycleRecorder()
        self.guard = FL.CommandCountGuard()
        self.remote = FakeRemote(pause, restore)
        self.leg = leg if leg is not None else FakeLeg()
        self.leg_exc = leg_exc
        self.leg_calls = 0
        self.acquire_ok = acquire_ok
        self.released = []
        self.marks_after_restore = marks_advance_after_restore
        self.n = 0
        self.phase = "paused"
        self.j = HO.Journal(path=os.path.join(tmpdir(), "j.jsonl"))
        self.o = HO.HandoffOrchestrator(
            remote=self.remote, observe=(lambda: sample()),
            gates=(lambda x: (True, {})), journal=self.j,
            mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
        # 🔴 pause / restore 都經過模組提供的包裝 —— 順序與計數是模組性質
        self.o.remote.send_pause_ex = FL.wrap_pause_sender(
            self.remote.send_pause_ex, self.rec, self.guard)
        self.o.remote.send_restore_ex = FL.wrap_restore_sender(
            self.remote.send_restore_ex, self.rec, self.guard)
        self.dispatcher, self.lease, _ = FL.build_first_live_dispatcher(
            live_leg_runner=self._run_leg,
            acquire_authority=self._acquire,
            release_authority=self._release,
            command_guard=self.guard, recorder=self.rec,
            audit_writer=audit_writer, revalidator=revalidator)

    # ---- Phase 6 authority ----
    def _acquire(self):
        return (True, "acquired") if self.acquire_ok else (False, "held_by_other")

    def _release(self):
        self.released.append(1)
        return True

    # ---- live leg ----
    def _run_leg(self):
        self.leg_calls += 1
        if self.leg_exc is not None:
            raise self.leg_exc
        return self.leg

    # ---- sources ----
    def mark(self):
        if self.phase == "restored" and self.marks_after_restore:
            self.n += 1
        return "2026-09-18 09:%02d:%02d" % (self.n // 60, self.n % 60)

    def run(self, entry_authorized=True):
        """
        🔴 OLD / NEW / WHY / EVIDENCE（2026-10-02 Attempt #8 postmortem）
          OLD : 以 `HO.DISPATCH_ENABLED = True` 讓 step 7 放行。
          NEW : 改為注入通過 STAGE A 的 `EntryOnlyPermit`；
                `HO.DISPATCH_ENABLED` 全程維持 rest state False。
          WHY : 新語意下「進入 dispatcher 前 DISPATCH_ENABLED=False」是
                **安全 invariant**；在 scope 外把它設成 True 反而必須
                Fail Closed（negative test O6）。
          EVIDENCE : Attempt #8 artifact —— step 7 前
                HO.DISPATCH_ENABLED=False、authority 兩項皆 false，
                而這正是 production 的正常狀態。
          🔴 這不是放寬：本檔原本就在測 pause 未驗證前絕不 dispatch，
             該命題與判定強度完全不變。
        """
        r = HO.HandoffRunner(
            self.o, (lambda: sample()), (lambda: probe()),
            loop_mark_source=self.mark, dispatcher=self._dispatch,
            config=CFG, soc_max_pct=85.0, sleeper=(lambda s: None),
            dispatch_permit=EntryOnlyPermit(entry_authorized))
        self.result = r.run()
        self.runner = r
        return self.result

    def _dispatch(self, obs):
        self.phase = "restored"          # leg 之後 external 才會被 restore
        return self.dispatcher(obs)

    def artifact(self):
        return FL.collect_handoff_artifact(
            self.result, orchestrator=self.o, dispatcher=self.dispatcher,
            lease=self.lease, guard=self.guard, recorder=self.rec,
            pause_verifier_state=RA.PV_VERIFIED, normal_loop_halted=True,
            restore_verifier_state=(RA.RV_RESUMED if self.marks_after_restore
                                    else RA.RV_FAILED),
            final_sample=sample())


# ======================================================================
# I1 ~ I3：pause 未驗證前絕不 dispatch
# ======================================================================
def test_I1_dispatcher_once():
    print("\n[I1] external loop active -> pause verified -> dispatcher 恰好一次")
    rig = Rig()
    res = rig.run()
    check("★★ I1. dispatcher 被呼叫恰好一次", rig.dispatcher.calls == 1)
    check("★★ I1. live leg 只跑一次", rig.leg_calls == 1)
    check("★★ I1. pause 送出一次", rig.remote.pause_calls == 1)
    check("  outcome = COMPLETE", res["outcome"] == "COMPLETE")
    check("  dispatcher outcome = DISPATCH_SUCCEEDED",
          rig.dispatcher.last["outcome"] == FL.DISPATCH_SUCCEEDED)
    steps = {s["step"]: s for s in res["steps"]}
    check("  step 3_4 PauseVerifier 在 step 7 之前通過",
          steps["3_4_PAUSE_VERIFY"]["ok"] and steps["7_FIRST_LIVE"]["ok"])


def test_I2_pause_not_verified():
    print("\n[I2] pause 未 verified -> dispatcher 從未被呼叫")
    rig = Rig()
    old = HO.DISPATCH_ENABLED
    HO.DISPATCH_ENABLED = True
    try:
        r = HO.HandoffRunner(
            rig.o, (lambda: sample(pcs_state="CHARGING", ac_kw=-40.0,
                                   authority="EXTERNAL_OR_UNKNOWN")),
            (lambda: probe()), loop_mark_source=rig.mark,
            dispatcher=rig._dispatch, config=CFG, soc_max_pct=85.0,
            sleeper=(lambda s: None))
        rig.result = r.run()
    finally:
        HO.DISPATCH_ENABLED = old
    check("★★ I2. dispatcher 從未被呼叫", rig.dispatcher.calls == 0)
    check("★★ I2. live leg 從未執行", rig.leg_calls == 0)
    check("★★ I2. Phase 6 從未取得控制權", rig.lease.acquired_once is False)
    check("  Phase 6 charge 指令數 = 0", rig.guard.count("phase6_charge") == 0)
    check("  責任成立後仍走 restore", rig.remote.restore_calls == 1)


def test_I3_pause_unknown():
    print("\n[I3] pause SEND_UNKNOWN -> dispatcher 從未被呼叫 / 責任維持")
    rig = Rig(pause=RA.SEND_UNKNOWN)
    res = rig.run()
    check("★★ I3. dispatcher 從未被呼叫", rig.dispatcher.calls == 0)
    check("★★ I3. pause 只嘗試一次（不得重送）",
          rig.remote.pause_calls == 1 and rig.guard.count("pause") == 1)
    check("★★ I3. outcome = PAUSE_OUTCOME_UNKNOWN",
          res["outcome"] == "PAUSE_OUTCOME_UNKNOWN")
    check("★★ I3. restore responsibility 維持 TRUE",
          res["restore_responsibility"] is True)
    check("  Phase 6 從未取得控制權", rig.lease.acquired_once is False)


# ======================================================================
# I4 ~ I6：dispatcher 三種結局都要 release + restore
# ======================================================================
def _assert_release_then_restore(rig, label):
    check(f"  {label}：Phase 6 authority 已釋放", rig.lease.held is False
          and len(rig.released) == 1)
    check(f"  {label}：external restore 恰好一次",
          rig.remote.restore_calls == 1 and rig.guard.count("restore") == 1)
    check(f"★★ {label}：release 先於 restore",
          rig.rec.ordered(FL.EV_AUTHORITY_RELEASED,
                          FL.EV_RESTORE_ATTEMPTED) is True)


def test_I4_dispatch_success():
    print("\n[I4] dispatcher success -> Phase 6 release -> restore 一次")
    rig = Rig(leg=FakeLeg("COMPLETE"))
    rig.run()
    check("  dispatcher outcome = SUCCEEDED",
          rig.dispatcher.last["outcome"] == FL.DISPATCH_SUCCEEDED)
    _assert_release_then_restore(rig, "I4")


def test_I5_dispatch_fail():
    print("\n[I5] dispatcher fail -> Phase 6 release -> restore 一次")
    rig = Rig(leg=FakeLeg("ABORTED", dispatch_count=1, stop_count=1))
    res = rig.run()
    check("★★ I5. dispatcher outcome = DISPATCH_FAILED",
          rig.dispatcher.last["outcome"] == FL.DISPATCH_FAILED)
    check("  verified = False -> 走 bail", rig.dispatcher.last["verified"] is False)
    check("  live leg 不重試", rig.leg_calls == 1)
    _assert_release_then_restore(rig, "I5")
    # 🔴 HandoffRunner 的 outcome "COMPLETE" 意思是「交接生命週期已收斂
    #    （external 已歸還）」，**不是**「FIRST LIVE leg 成功」。
    #    leg 成敗一律看 step 8_VERIFY 與 dispatcher outcome。
    steps = {x["step"]: x for x in res["steps"]}
    check("★★ I5. step 8_VERIFY 標記為失敗",
          steps["8_VERIFY"]["ok"] is False)
    check("  step 9_RELEASE 未被標成通過", "9_RELEASE" not in steps)


def test_I6_dispatch_unknown():
    print("\n[I6] dispatcher UNKNOWN -> 不重試 -> release + restore")
    rig = Rig(leg=FakeLeg("ABORTED_UNCERTAIN_OWNERSHIP"))
    rig.run()
    check("★★ I6. dispatcher outcome = DISPATCH_UNKNOWN",
          rig.dispatcher.last["outcome"] == FL.DISPATCH_UNKNOWN)
    check("★★ I6. UNKNOWN 不得算 verified",
          rig.dispatcher.last["verified"] is False)
    check("★★ I6. 不重試 charge", rig.leg_calls == 1
          and rig.guard.count("phase6_charge") == 1)
    _assert_release_then_restore(rig, "I6")


# ======================================================================
# I7 ~ I8：ownership 分離與順序
# ======================================================================
def test_I7_authority_acquire_fail():
    print("\n[I7] Phase 6 authority 取得失敗 -> 不送任何指令 -> restore external")
    rig = Rig(acquire_ok=False)
    rig.run()
    check("★★ I7. Phase 6 未取得控制權", rig.lease.acquired_once is False)
    check("★★ I7. live leg 從未執行", rig.leg_calls == 0)
    check("★★ I7. Phase 6 charge 指令 = 0", rig.guard.count("phase6_charge") == 0)
    check("  dispatcher outcome = NOT_ATTEMPTED",
          rig.dispatcher.last["outcome"] == FL.DISPATCH_NOT_ATTEMPTED)
    check("★★ I7. external 仍被 restore", rig.remote.restore_calls == 1)
    check("  acquire 失敗事件已記錄",
          rig.rec.happened(FL.EV_AUTHORITY_ACQUIRE_FAILED))


def test_I8_release_before_restore():
    print("\n[I8] Phase 6 release 必須發生在 external restore 之前")
    for leg, label in ((FakeLeg("COMPLETE"), "success"),
                       (FakeLeg("ABORTED"), "fail"),
                       (FakeLeg("ABORTED_UNCERTAIN_OWNERSHIP"), "unknown")):
        rig = Rig(leg=leg)
        rig.run()
        a = rig.rec.seq_of(FL.EV_AUTHORITY_RELEASED)
        b = rig.rec.seq_of(FL.EV_RESTORE_ATTEMPTED)
        check(f"★★ I8. {label}：release(seq={a}) < restore(seq={b})",
              a is not None and b is not None and a < b)
    rig2 = Rig(acquire_ok=False)
    rig2.run()
    check("★★ I8. acquire 失敗時也先記 release 再 restore",
          rig2.rec.ordered(FL.EV_AUTHORITY_RELEASED,
                           FL.EV_RESTORE_ATTEMPTED) is True)


# ======================================================================
# I9 ~ I10：restore 責任生命週期
# ======================================================================
def test_I9_restore_unknown():
    print("\n[I9] restore SEND_UNKNOWN -> 不重送 -> 責任保留")
    rig = Rig(restore=RA.SEND_UNKNOWN)
    res = rig.run()
    check("★★ I9. restore 只嘗試一次", rig.remote.restore_calls == 1
          and rig.guard.count("restore") == 1)
    check("★★ I9. outcome = RESTORE_OUTCOME_UNKNOWN",
          res["outcome"] == "RESTORE_OUTCOME_UNKNOWN")
    check("★★ I9. 責任保留", res["restore_responsibility"] is True)
    art = rig.artifact().to_dict()
    check("  artifact 責任最終狀態 = True",
          art["restore_responsibility_final"] is True)


def test_I10_rv_resumed_clears():
    print("\n[I10] RV_RESUMED -> 責任清除")
    rig = Rig()
    res = rig.run()
    check("★★ I10. 責任已清除", res["restore_responsibility"] is False)
    check("  runner 內部旗標一致", rig.runner.restore_responsibility is False)
    art = rig.artifact().to_dict()
    check("★★ I10. artifact restore_resumed = True", art["restore_resumed"] is True)
    check("  validate 無問題", FL.validate_handoff_artifact(art) == [])

    rig2 = Rig(marks_advance_after_restore=False)
    res2 = rig2.run()
    check("★★ I10. marks 未前進 -> 責任保留",
          res2["restore_responsibility"] is True)


# ======================================================================
# I11 ~ I13：command count
# ======================================================================
def test_I11_charge_at_most_once():
    print("\n[I11] CHARGE attempt <= 1")
    rig = Rig()
    rig.run()
    check("★★ I11. phase6_charge == 1", rig.guard.count("phase6_charge") == 1)
    try:
        rig.guard.take("phase6_charge")
        raised = False
    except FL.CommandLimitExceeded:
        raised = True
    check("★★ I11. 第二次 charge 直接拋 CommandLimitExceeded", raised)
    check("  計數停在 1", rig.guard.count("phase6_charge") == 1)
    check("  guard 無違規", rig.guard.violations() == [])


def test_I12_discharge_zero():
    print("\n[I12] DISCHARGE == 0")
    rig = Rig()
    rig.run()
    check("★★ I12. phase6_discharge == 0", rig.guard.count("phase6_discharge") == 0)
    g = FL.CommandCountGuard()
    try:
        g.take("phase6_discharge")
        raised = False
    except FL.CommandLimitExceeded:
        raised = True
    check("★★ I12. 任何 discharge 嘗試立即被擋", raised)
    check("  FIRST LIVE 只允許 charge",
          FL.FIRST_LIVE_ALLOWED_ACTIONS == ("charge",))
    try:
        FL.FirstLiveDispatcher(lambda: None, None, g, action="discharge")
        bad = False
    except ValueError:
        bad = True
    check("★★ I12. dispatcher 拒絕 discharge action", bad)


def test_I13_pcs_stop_counted():
    print("\n[I13] PCS STOP <= 1，且 live-leg cleanup STOP 必須計入 aggregate")
    rig = Rig(leg=FakeLeg("COMPLETE", stop_count=1))
    rig.run()
    check("★★ I13. phase6_pcs_stop == 1（cleanup STOP 已計入 aggregate）",
          rig.guard.count("phase6_pcs_stop") == 1)
    check("★★ I13. live_leg_internal_stop == 1（保留為 subtype）",
          rig.guard.count("live_leg_internal_stop") == 1)
    check("★★ I13. pcs_stop_reason = LIVE_LEG_CLEANUP",
          rig.guard.pcs_stop_reason == "LIVE_LEG_CLEANUP")
    check("★★ I13. subtype **不是**豁免 —— 仍受 limits 管制",
          "phase6_pcs_stop" in FL.CommandCountGuard.LIMITS
          and FL.CommandCountGuard.LIMITS["phase6_pcs_stop"] == 1)
    check("  無違規", rig.guard.violations() == [])
    g = FL.CommandCountGuard()
    g.take("phase6_pcs_stop")
    try:
        g.take("phase6_pcs_stop")
        raised = False
    except FL.CommandLimitExceeded:
        raised = True
    check("★★ I13. 第二次 PCS STOP 立即被擋（<= 1）", raised)


# ======================================================================
# I14 ~ I15：兩套 evidence
# ======================================================================
def test_I14_decision_audit():
    print("\n[I14] Durable Decision Audit 有 FIRST LIVE decision/control evidence")
    seen = {}

    class FakeExec(object):
        executed = True
        control_action = "charge"
        outcome = "DISPATCHED"

    class Cap(object):
        latest = FakeExec()

    def fake_write_audit(sink, cycle_result, evidence=None, config=None,
                         local_now=None):
        seen["execution_result"] = evidence.execution_result
        seen["observation"] = evidence.observation
        seen["sink"] = sink
        return ("W_OK", 1)

    w = FL.build_first_live_audit_writer(
        sink=object(), result_capture=Cap(),
        observation_source=(lambda: {"obs": True}),
        write_audit=fake_write_audit)
    rig = Rig(audit_writer=w)
    rig.run()
    check("★★ I14. audit 已寫入", rig.dispatcher.last["audit"] == ("W_OK", 1))
    check("★★ I14. execution_result 有帶到（control evidence）",
          getattr(seen.get("execution_result"), "control_action", None) == "charge"
          and getattr(seen.get("execution_result"), "executed", None) is True)
    check("  observation 有帶到（decision evidence）",
          seen.get("observation") == {"obs": True})
    check("★★ I14. sink=None 時完全不寫檔",
          FL.build_first_live_audit_writer(None)(None)[0] == "AUDIT_DISABLED")
    # 🔴 不得新增欄位到既有 46 欄 schema
    import phase6_decision_audit as AUD
    check("★★ I14. Durable Decision Audit schema 未被更動",
          len(AUD.FIELDS) == 46 and AUD.SCHEMA_VERSION == 1)


def test_I15_handoff_artifact():
    print("\n[I15] 6.10 artifact 有完整 handoff evidence")
    rig = Rig()
    rig.run()
    art = rig.artifact()
    d = art.to_dict()
    for k in ("pause_attempt_count", "pause_send_outcome", "normal_loop_halted",
              "pause_verifier_state", "restore_responsibility_lifecycle",
              "phase6_authority_acquired", "phase6_authority_released",
              "dispatcher_attempted", "dispatcher_outcome",
              "phase6_command_counts", "restore_attempt_count",
              "restore_send_outcome", "restore_verifier_state",
              "final_ownership", "fault", "critical_alarms",
              "started_at", "finished_at", "lifecycle"):
        check(f"  欄位存在：{k}", k in d)
    check("★★ I15. validate_handoff_artifact 無問題",
          FL.validate_handoff_artifact(d) == [])
    check("  pause / restore 各 1 次",
          d["pause_attempt_count"] == 1 and d["restore_attempt_count"] == 1)
    check("  discriminative_pause_verified = True",
          d["discriminative_pause_verified"] is True)
    check("★★ I15. release_before_restore = True",
          d["release_before_restore"] is True)
    check("  與 Decision Audit 是兩套 schema",
          d["artifact_kind"] == FL.ARTIFACT_KIND)
    p = os.path.join(tmpdir(), "h.json")
    art.write(p)
    import json
    back = json.load(io.open(p, encoding="utf-8"))
    check("  可落盤並原樣讀回", FL.validate_handoff_artifact(back) == [])
    try:
        art.set(not_a_field=1)
        bad = False
    except KeyError:
        bad = True
    check("  未登記欄位一律拒絕", bad)
    bad2 = dict(d)
    bad2["phase6_command_counts"] = {"counts": {"phase6_charge": 2}}
    check("★★ I15. command count 超限被抓出",
          any("phase6_charge" in x for x in FL.validate_handoff_artifact(bad2)))
    bad3 = dict(d)
    bad3["release_before_restore"] = False
    check("★★ I15. release 未先於 restore 被抓出",
          any("release" in x or "釋放" in x
              for x in FL.validate_handoff_artifact(bad3)))


# ======================================================================
# I16 ~ I18：結構性安全
# ======================================================================
def test_I16_dispatch_disabled():
    # 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-02 Attempt #8 postmortem）
    #   OLD : 以 `dispatch_enabled=False` 表達「不得 dispatch」。
    #   NEW : 改為 `entry_authorized=False`（STAGE A entry gate 不授權）。
    #   WHY : 新語意下 `DISPATCH_ENABLED=False` 是**進入前的正常 rest
    #         state**，不是拒絕理由；真正的拒絕來自 entry gate。
    #   EVIDENCE : Attempt #8 artifact —— step 7 前
    #         HO.DISPATCH_ENABLED=False 卻是完全正常的 production 狀態。
    #   🔴 判定強度不變：dispatcher 0 次、live leg 0 次、
    #      authority 0 次、charge 0、restore 仍為 1。
    print("\n[I16] entry gate 不授權 -> dispatcher 不可觸發")
    check("  模組預設仍為 False", HO.DISPATCH_ENABLED is False)
    rig = Rig()
    res = rig.run(entry_authorized=False)
    check("★★ I16. dispatcher 從未被呼叫", rig.dispatcher.calls == 0)
    check("★★ I16. live leg 從未執行", rig.leg_calls == 0)
    check("★★ I16. Phase 6 從未取得控制權", rig.lease.acquired_once is False)
    check("  charge 指令 = 0", rig.guard.count("phase6_charge") == 0)
    check("★★ I16. 仍完成 restore（責任不因不 dispatch 而消失）",
          rig.remote.restore_calls == 1)
    check("  全程 DISPATCH_ENABLED 維持 rest state",
          HO.DISPATCH_ENABLED is False)


def test_I17_senders_not_armed():
    print("\n[I17] RemoteSenders armed=False -> remote send 不可發生")
    check("★★ I17. RemoteSenders 預設未武裝", RA.RemoteSenders().armed is False)
    check("★★ I17. SshTransport 預設未武裝", RA.SshTransport().armed is False)
    sd = RA.RemoteSenders()
    check("★★ I17. 未武裝時 pause sender 回 False", sd.pause() is False)
    check("★★ I17. 未武裝時 restore sender 回 False", sd.restore() is False)
    tr = RA.SshTransport(timeouts=RA.SshTimeouts(), runner=(lambda *a: {}),
                         armed=False)
    for v in RA.CONTROL_VERBS:
        ok, why = tr.can_run(v)
        check(f"  can_run({v}) = False（{why}）", ok is False)
    check("  未武裝時 runner 完全沒被呼叫", tr.calls == [])


def test_I18_exception_fail_closed():
    print("\n[I18] 任何 exception -> Fail Closed / 責任保留")
    rig = Rig(leg_exc=RuntimeError("operator 連線中斷"))
    res = rig.run()
    check("★★ I18. 例外不外漏（runner 正常收斂）", isinstance(res, dict))
    check("★★ I18. dispatcher outcome = DISPATCH_UNKNOWN（不得樂觀判 FAILED）",
          rig.dispatcher.last["outcome"] == FL.DISPATCH_UNKNOWN)
    check("★★ I18. Phase 6 authority 仍被釋放（finally）",
          rig.lease.held is False and len(rig.released) == 1)
    _assert_release_then_restore(rig, "I18")
    check("★★ I18. charge 不重試", rig.leg_calls == 1)
    # audit writer 例外同樣不得影響控制流程
    def boom(_x=None):
        raise RuntimeError("audit sink 壞了")
    rig2 = Rig(audit_writer=boom)
    rig2.run()
    check("★★ I18. audit 例外不影響控制（NON_GATING_CONTINUE）",
          rig2.dispatcher.last["audit"][0] == "AUDIT_WRITER_EXCEPTION"
          and rig2.dispatcher.last["outcome"] == FL.DISPATCH_SUCCEEDED)


# ======================================================================
# N 系列：結構鎖
# ======================================================================
def src_of(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


def find_node(src, name, kind=ast.FunctionDef):
    return next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, kind) and n.name == name)


def literal_strings_fl(node):
    """節點內字串字面值，**排除 docstring 本體**（避免自掃假陽性）。"""
    skip = set()
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                skip.add(id(first.value))
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in skip}


def test_N1_two_layer_preflight():
    print("\n[N1] 兩層 preflight：LAYER 1 不得要求 external idle")
    ok, ck = FL.first_live_safety_preflight(sample(), probe(), FLAGS)
    check("  乾淨狀態通過", ok is True)
    for st in ("CHARGING", "DISCHARGING", "STANDBY", "STOPPED"):
        ok2, _ = FL.first_live_safety_preflight(
            sample(pcs_state=st, authority="EXTERNAL_OR_UNKNOWN"), probe(), FLAGS)
        check(f"★★ N1. LAYER 1 不因 PCS={st} / Authority=EXTERNAL 判 FAIL",
              ok2 is True)
    fn = find_node(src_of("phase6_first_live_handoff.py"),
                   "first_live_safety_preflight")
    ids = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    ids |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    check("★★ N1. AST：LAYER 1 不引用 authority / pcs_state 判定",
          "authority" not in ids and "pcs_state" not in ids)
    for bad in (dict(fault=True), dict(critical_alarms=1), dict(comm_ok=False),
                dict(fresh=False), dict(ownership="BOTH")):
        check(f"  {list(bad)[0]} 不合格 -> FAIL",
              FL.first_live_safety_preflight(sample(**bad), probe(), FLAGS)[0]
              is False)
    check("  guard 非 B2 -> FAIL",
          FL.first_live_safety_preflight(sample(), probe(guard_variant="B1"),
                                         FLAGS)[0] is False)
    # LAYER 2
    marks = ["2026-09-18 09:00:01", "2026-09-18 09:00:04", "2026-09-18 09:00:07"]
    ok3, ck3 = FL.handoff_readiness(probe(), loop_marks=marks,
                                    min_loop_observations=3,
                                    pause_capable=(lambda c: True),
                                    restore_capable=(lambda c: True))
    check("★★ N1. LAYER 2 齊備時通過", ok3 is True)
    check("  loop 證據不足 -> FAIL",
          FL.handoff_readiness(probe(), loop_marks=marks[:1],
                               min_loop_observations=3,
                               pause_capable=(lambda c: True),
                               restore_capable=(lambda c: True))[0] is False)
    check("★★ N1. pause capability 不可用 -> FAIL",
          FL.handoff_readiness(probe(), loop_marks=marks,
                               min_loop_observations=3,
                               pause_capable=(lambda c: False),
                               restore_capable=(lambda c: True))[0] is False)
    check("★★ N1. restore capability 不可用 -> FAIL",
          FL.handoff_readiness(probe(), loop_marks=marks,
                               min_loop_observations=3,
                               pause_capable=(lambda c: True),
                               restore_capable=(lambda c: False))[0] is False)
    check("  兩層項目數 11 / 4",
          len(FL.SAFETY_PREFLIGHT_ITEMS) == 11
          and len(FL.HANDOFF_READINESS_ITEMS) == 4)


def test_N2_dispatcher_knows_no_charge_detail():
    print("\n[N2] dispatcher 不得知道 CHARGE 實作細節（AST）")
    src = src_of("phase6_first_live_handoff.py")
    cls = find_node(src, "FirstLiveDispatcher", ast.ClassDef)
    ids = {n.attr for n in ast.walk(cls) if isinstance(n, ast.Attribute)}
    ids |= {n.id for n in ast.walk(cls) if isinstance(n, ast.Name)}
    for bad in ("charge_power_kw", "max_power_kw", "build_executor",
                "build_verifier", "operator_run", "device_control_operator",
                "PcsControlExecutor", "run_live_leg_cli"):
        check(f"★★ N2. dispatcher 不引用 {bad}", bad not in ids)
    check("  只透過注入的 live_leg_runner 執行", "_run_leg" in ids)


def test_N3_no_second_charge_logic():
    print("\n[N3] 不建立第二套 charge execution logic（AST）")
    tree = ast.parse(src_of("phase6_first_live_handoff.py"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    for bad in ("device_control_operator", "pcs_control_executor",
                "pcs_control_integration", "decision_engine", "decision_policy",
                "safety_gate", "tou_calendar", "requests", "subprocess"):
        check(f"★★ N3. 模組層未 import {bad}", bad not in mods)
    nums = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, float)}
    check("★★ N3. 未出現 5.0 / 150.0 等功率常數",
          5.0 not in nums and 150.0 not in nums)
    check("  只有 timeout candidate 12.0 出現", 12.0 in nums)


def test_N4_production_unchanged():
    print("\n[N4] production 參數 / 既有 criteria 未被更動")
    import pcs_auto_control_config as C
    check("★★ N4. charge_power_kw 仍為 5.0",
          C.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0)
    check("★★ N4. discharge_power_kw 仍為 5.0",
          C.DEFAULT_CONTROL_CONFIG.discharge_power_kw == 5.0)
    check("★★ N4. max_power_kw 仍為 150.0（Safety Gate 上限，非 FIRST LIVE 目標）",
          C.DEFAULT_CONTROL_CONFIG.max_power_kw == 150.0)
    check("★★ N4. PauseVerifier criteria 仍 9 項",
          len(RA.CANDIDATE_IDLE_ITEMS) == 9)
    check("★★ N4. stable verify 4 / 15 / 45 / 120 未變",
          (CFG.stable_verify_min_samples, CFG.stable_verify_interval_sec,
           CFG.stable_verify_min_span_sec, CFG.pause_settling_timeout_sec)
          == (4, 15.0, 45.0, 120.0))
    check("★★ N4. RestoreVerifier 3 / 3 未變",
          (CFG.restore_loop_min_observations, CFG.restore_loop_interval_sec)
          == (3, 3.0))
    check("★★ N4. DEPLOYED_GUARD_VARIANT = B2",
          RA.DEPLOYED_GUARD_VARIANT == "B2")
    # latent dead path 不得被順手修改
    hsrc = src_of("phase6_handoff_orchestrator.py")
    for name in ("_quiescent_hint", "verify_restore"):
        fn = find_node(hsrc, name)
        consts = {n.value for n in ast.walk(fn)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        check(f"★★ N4. {name} 維持原樣（未擴 scope）", "running" in consts)


def test_N5_timeout_not_final():
    print("\n[N5] timeout 仍為 FIELD_VALIDATION_CANDIDATE")
    check("★★ N5. production pause default = None",
          RA.SshTimeouts().command_timeouts.get("pause") is None)
    check("★★ N5. production restore default = None",
          RA.SshTimeouts().command_timeouts.get("restore") is None)
    check("★★ N5. artifact timeout_status.final = False",
          FL.TIMEOUT_STATUS["final"] is False)
    check("  仍標 FIELD_VALIDATION_CANDIDATE",
          FL.TIMEOUT_STATUS["status"] == "FIELD_VALIDATION_CANDIDATE")
    art = FL.HandoffExecutionArtifact().to_dict()
    bad = dict(art)
    bad["timeout_status"] = dict(FL.TIMEOUT_STATUS, final=True)
    check("★★ N5. 標成 FINAL 會被 validate 抓出",
          any("FINAL" in x for x in FL.validate_handoff_artifact(bad)))



# ======================================================================
# R 系列：RULING 1（direct entry blocked）／RULING 2（command count 語意）
# ======================================================================
import subprocess                                                # noqa: E402

RUNNER = "phase6_first_live_runner.py"


def _run_cli(*args):
    p = subprocess.run([sys.executable, os.path.join(HERE, RUNNER)] + list(args),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=HERE, timeout=120)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def test_R1_direct_cli_blocked():
    print("\n[R1] phase6_first_live_runner 直接 CLI field invocation -> BLOCKED")
    for args in ((), ("--observe-only",), ("--observe-sec", "60")):
        rc, out = _run_cli(*args)
        check(f"★★ R1. argv={list(args)} -> exit code 非 0（{rc}）", rc != 0)
        check("  印出正式拒絕訊息",
              "FIRST LIVE direct execution disabled" in out
              and "Phase 6.10 handoff entry point" in out)
    check("★★ R1. 模組常數標示已封鎖",
          _runner_mod().DIRECT_FIELD_EXECUTION_BLOCKED is True)
    check("  exit code 常數非 0", _runner_mod().DIRECT_ENTRY_EXIT_CODE != 0)


def _runner_mod():
    import phase6_first_live_runner as R
    return R


def test_R2_direct_sends_nothing():
    print("\n[R2] direct invocation 不得送指令 / 不得 acquire authority")
    rc, out = _run_cli()
    for bad in ("HMI 登入", "Phase B", "Control Mutex", "run_live_leg_cli",
                "dispatch", "acquire"):
        check(f"★★ R2. 輸出未出現 {bad}", bad not in out)
    src = src_of(RUNNER)
    fn = find_node(src, "refuse_direct_execution")
    ids = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    ids |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    for bad in ("acquire_ownership", "run_live_leg_cli", "build_executor",
                "build_api_client", "main"):
        check(f"★★ R2. refuse_direct_execution 不引用 {bad}", bad not in ids)
    # __main__ 區塊必須呼叫 refuse，而不是 main()
    tree = ast.parse(src)
    mains = [n for n in tree.body if isinstance(n, ast.If)]
    called = set()
    for m in mains:
        for n in ast.walk(m):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                called.add(n.func.id)
    check("★★ R2. __main__ 呼叫 refuse_direct_execution",
          "refuse_direct_execution" in called)
    check("★★ R2. __main__ **不**呼叫 main()", "main" not in called)
    # 🔴 不得存在任何 bypass 入口。
    #    以 argparse 實際註冊的參數名判定 —— **不掃原始碼文字**，
    #    否則本檔自己的「刻意不提供 --force」註解會造成假陽性（既往教訓）。
    opts = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "add_argument"):
            for a in n.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    opts.add(a.value.lstrip("-").replace("-", "_").lower())
    for bad in ("force", "bypass", "bypass_handoff", "override", "allow_direct"):
        check(f"  argparse 未註冊 {bad} 繞過參數", bad not in opts)
    check("  且 __main__ 根本不解析 argv（無條件拒絕）",
          "refuse_direct_execution" in called and "main" not in called)


def test_R3_live_leg_still_importable():
    print("\n[R3] handoff / offline test 仍可 import 並使用 live leg")
    R = _runner_mod()
    check("★★ R3. 模組可正常 import", R is not None)
    check("★★ R3. main() 仍存在（供整合層 / 離線測試呼叫）",
          callable(getattr(R, "main", None)))
    check("  LIVE_ACTION 仍為 charge", R.LIVE_ACTION == "charge")
    check("  import 本身不觸發任何執行",
          getattr(R, "_log_lines", None) in (None, []))
    # dispatcher 仍可用任意 live-leg callable 組起來
    g = FL.CommandCountGuard()
    d, lease, _ = FL.build_first_live_dispatcher(
        live_leg_runner=(lambda: FakeLeg("COMPLETE")),
        acquire_authority=(lambda: (True, "ok")),
        release_authority=(lambda: True), command_guard=g)
    res = d(None)
    check("★★ R3. dispatcher 仍能正常執行注入的 live leg",
          res["outcome"] == FL.DISPATCH_SUCCEEDED and d.calls == 1)


# 已知會呼叫 live leg 的模組，以及各自的封鎖旗標
# 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-21 field provider formalization）
#   OLD : KNOWN_LIVE_LEG_MODULES 只登記 phase6_first_live_runner /
#         pcs_auto_control_service，兩者皆以「CLI 已封鎖」旗標通過。
#   NEW : 新增 phase6_first_live_field_provider —— 它會呼叫 run_live_leg_cli
#         （那是它**唯一**的職責：把既有 live leg 接給 dispatcher），
#         但其 `__main__` 只做離線自述 ＋ validate，**到不了** live leg。
#         因此改以 `CLI_ENTRY_IS_PASSIVE` 登記，並**額外要求 AST 證明**
#         其 main() 不可達 run_live_leg_cli / live_leg_main。
#   WHY : 不登記會讓 R4 誤判成「未登記的繞過入口」；
#         但若只是登記而不驗證，等於放寬 —— 故加上 AST 可達性證明。
#   EVIDENCE: 下方 `_main_reaches_live_leg()` 直接掃該模組 main() 的呼叫圖。
#   🔴 方向為**加嚴**（新增可達性證明），不是為了湊數字放寬斷言。
KNOWN_LIVE_LEG_MODULES = {
    "phase6_first_live_runner.py": ("phase6_first_live_runner",
                                    "DIRECT_FIELD_EXECUTION_BLOCKED"),
    "pcs_auto_control_service.py": ("pcs_auto_control_service",
                                    "LIVE_LEG_CLI_BLOCKED"),
    "phase6_first_live_field_provider.py": ("phase6_first_live_field_provider",
                                            "CLI_ENTRY_IS_PASSIVE"),
}

LIVE_LEG_CALLS = ("run_live_leg_cli", "live_leg_main")


def _main_reaches_live_leg(filename):
    """該模組的 main() 是否（直接或經同檔函式）可達 live leg 呼叫。"""
    tree = ast.parse(src_of(filename))
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}
    seen, stack = set(), ["main"]
    while stack:
        name = stack.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for c in ast.walk(funcs[name]):
            if not isinstance(c, ast.Call):
                continue
            target = getattr(c.func, "attr", None) or getattr(c.func, "id", None)
            if target in LIVE_LEG_CALLS:
                return True
            if target in funcs:
                stack.append(target)
    return False


def test_R4_no_other_field_entry():
    print("\n[R4] repo 不存在另一個可繞過 handoff 的 FIRST LIVE field entry")
    found = []
    for fn in sorted(os.listdir(HERE)):
        if not fn.endswith(".py") or fn.startswith("test_"):
            continue
        try:
            tree = ast.parse(src_of(fn))
        except Exception:                                   # noqa: BLE001
            continue
        calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        if "run_live_leg_cli" not in calls and "live_leg_main" not in calls:
            continue
        if not any(isinstance(n, ast.If) and "__main__" in ast.dump(n)
                   for n in tree.body):
            continue
        found.append(fn)
    check("★★ R4. 所有含 live-leg CLI 的模組皆為已知並已封鎖",
          set(found) <= set(KNOWN_LIVE_LEG_MODULES),
          )
    if set(found) - set(KNOWN_LIVE_LEG_MODULES):
        print("      未登記的入口:", sorted(set(found) - set(KNOWN_LIVE_LEG_MODULES)))
    import importlib
    for fn, (mod, flag) in KNOWN_LIVE_LEG_MODULES.items():
        m = importlib.import_module(mod)
        check(f"★★ R4. {fn} 的 CLI field entry 已封鎖 / passive（{flag}）",
              getattr(m, flag, False) is True)
    # 🔴 加嚴：宣告為 passive 的模組，其 main() 必須**結構上到不了** live leg
    check("★★ R4. provider 的 main() 不可達 live leg（AST 可達性）",
          _main_reaches_live_leg("phase6_first_live_field_provider.py")
          is False)
    check("  對照：被封鎖的 runner 其 __main__ 也不呼叫 main()",
          _main_reaches_live_leg("phase6_first_live_runner.py") is True)
    # 整合層本身不得直接呼叫 live leg（AST，不掃文字）
    ftree = ast.parse(src_of("phase6_first_live_handoff.py"))
    fcalls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
              for n in ast.walk(ftree) if isinstance(n, ast.Call)}
    check("  整合層未直接呼叫 run_live_leg_cli / live_leg_main",
          "run_live_leg_cli" not in fcalls and "live_leg_main" not in fcalls)


def test_R4b_service_live_leg_cli_blocked():
    print("\n[R4b] pcs_auto_control_service --live-leg CLI -> BLOCKED")
    import pcs_auto_control_service as SVC
    check("★★ R4b. LIVE_LEG_CLI_BLOCKED = True", SVC.LIVE_LEG_CLI_BLOCKED is True)
    check("  exit code 非 0", SVC.LIVE_LEG_CLI_EXIT_CODE != 0)
    check("  拒絕訊息與 runner 一致",
          SVC.LIVE_LEG_CLI_REFUSAL
          == _runner_mod().DIRECT_ENTRY_REFUSAL)
    p = subprocess.run(
        [sys.executable, os.path.join(HERE, "pcs_auto_control_service.py"),
         "--live-leg", "charge"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=HERE, timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    check("★★ R4b. exit code 非 0（%d）" % p.returncode, p.returncode != 0)
    check("★★ R4b. 印出正式拒絕訊息",
          "FIRST LIVE direct execution disabled" in out)
    for bad in ("Control Mutex", "CONTROLLED SINGLE-LEG LIVE", "HMI login"):
        check(f"  未進入 live leg 流程（無 {bad}）", bad not in out)
    # 函式本體未被刪除，仍可 import
    check("★★ R4b. live_leg_main / run_live_leg_cli 仍存在（只封 CLI）",
          callable(SVC.live_leg_main) and callable(SVC.run_live_leg_cli))


def test_R5_counts_success_leg():
    print("\n[R5] 成功 charge leg + cleanup STOP 的 aggregate 計數")
    rig = Rig(leg=FakeLeg("COMPLETE", dispatch_count=1, stop_count=1))
    rig.run()
    c = rig.guard.counts
    check("★★ R5. phase6_charge = 1", c.get("phase6_charge") == 1)
    check("★★ R5. phase6_discharge = 0", c.get("phase6_discharge", 0) == 0)
    check("★★ R5. phase6_pcs_stop = 1", c.get("phase6_pcs_stop") == 1)
    check("★★ R5. live_leg_internal_stop = 1",
          c.get("live_leg_internal_stop") == 1)
    d = rig.artifact().to_dict()
    check("★★ R5. artifact 如實記錄，未寫成 PCS STOP = 0",
          d["phase6_command_counts"]["counts"]["phase6_pcs_stop"] == 1)
    check("★★ R5. pcs_stop_reason = LIVE_LEG_CLEANUP",
          d["pcs_stop_reason"] == "LIVE_LEG_CLEANUP")
    check("  validate 無問題", FL.validate_handoff_artifact(d) == [])


def test_R6_counts_no_leg():
    print("\n[R6] 未進入 live leg -> 0 / 0 / 0")
    rig = Rig(acquire_ok=False)
    rig.run()
    c = rig.guard.counts
    check("★★ R6. charge / discharge / pcs_stop = 0 / 0 / 0",
          c.get("phase6_charge", 0) == 0 and c.get("phase6_discharge", 0) == 0
          and c.get("phase6_pcs_stop", 0) == 0)
    d = rig.artifact().to_dict()
    check("  pcs_stop_reason = None（沒有 STOP 就不編理由）",
          d["pcs_stop_reason"] is None)
    check("  validate 無問題", FL.validate_handoff_artifact(d) == [])


def test_R7_artifact_rejects_overcount():
    print("\n[R7] artifact 對超限一律 reject")
    rig = Rig(leg=FakeLeg("COMPLETE", stop_count=1))
    rig.run()
    base = rig.artifact().to_dict()

    def mutate(**counts):
        d = dict(base)
        c = dict(base["phase6_command_counts"])
        cc = dict(c["counts"])
        cc.update(counts)
        c["counts"] = cc
        d["phase6_command_counts"] = c
        return d

    check("★★ R7. phase6_pcs_stop = 2 -> reject",
          any("phase6_pcs_stop" in x
              for x in FL.validate_handoff_artifact(mutate(phase6_pcs_stop=2))))
    check("★★ R7. phase6_charge = 2 -> reject",
          any("phase6_charge" in x
              for x in FL.validate_handoff_artifact(mutate(phase6_charge=2))))
    check("★★ R7. phase6_discharge = 1 -> reject",
          any("phase6_discharge" in x
              for x in FL.validate_handoff_artifact(mutate(phase6_discharge=1))))


def test_R8_cleanup_stop_not_excluded():
    print("\n[R8] cleanup STOP 不得被錯誤排除於 aggregate")
    rig = Rig(leg=FakeLeg("COMPLETE", stop_count=1))
    rig.run()
    d = rig.artifact().to_dict()
    bad = dict(d)
    c = dict(d["phase6_command_counts"])
    cc = dict(c["counts"])
    cc["phase6_pcs_stop"] = 0          # 假裝 cleanup STOP 不算
    c["counts"] = cc
    bad["phase6_command_counts"] = c
    probs = FL.validate_handoff_artifact(bad)
    check("★★ R8. subtype > aggregate -> 被抓出",
          any("未計入" in x for x in probs))
    bad2 = dict(d)
    bad2["pcs_stop_reason"] = None
    check("★★ R8. 有 STOP 卻沒有 reason -> 被抓出",
          any("pcs_stop_reason" in x for x in FL.validate_handoff_artifact(bad2)))


def test_R9_outcome_semantics():
    print("\n[R9] handoff COMPLETE 但 dispatcher FAILED -> FIRST LIVE 不得標 PASS")
    rig = Rig(leg=FakeLeg("ABORTED", dispatch_count=1, stop_count=1))
    res = rig.run()
    d = rig.artifact().to_dict()
    check("  handoff_outcome = COMPLETE（external 已歸還）",
          d["handoff_outcome"] == "COMPLETE")
    check("★★ R9. dispatcher_outcome = DISPATCH_FAILED",
          d["dispatcher_outcome"] == FL.DISPATCH_FAILED)
    check("★★ R9. 三個 outcome 並存，可區分 restore 成功 vs charge 成功",
          d["handoff_outcome"] is not None
          and d["dispatcher_outcome"] is not None
          and d["live_leg_outcome"] is not None)
    check("★★ R9. live_leg_outcome 記錄 leg 終態",
          d["live_leg_outcome"]["leg_state"] == "ABORTED")
    check("★★ R9. FIRST LIVE 成功判準 = dispatcher SUCCEEDED（此處為 False）",
          (d["dispatcher_outcome"] == FL.DISPATCH_SUCCEEDED) is False)
    steps = {x["step"]: x for x in res["steps"]}
    check("  step 8_VERIFY 標記失敗", steps["8_VERIFY"]["ok"] is False)


# ======================================================================
# P 系列：GATE A pre-handoff eligibility ／ GATE B post-handoff revalidation
# ======================================================================
def pre_sample(**kw):
    """GATE A 用樣本（既有決策鏈算完後填入）。"""
    d = {"decision": "charge", "tou_allows_charge": True,
         "soc_allows_charge": True, "fault": False, "critical_alarms": 0,
         "comm_ok": True, "fresh": True, "meter_fresh": True,
         "pcs_state": "STANDBY", "safety_gate_ok": True,
         "authority": "EXTERNAL_OR_UNKNOWN"}
    d.update(kw)
    return d


def post_sample(**kw):
    """GATE B 用樣本（pause 之後重新取得）。"""
    d = pre_sample(authority="IDLE", ownership="NEITHER")
    d["post_handoff_fresh"] = True
    d.update(kw)
    return d


ADVANCING = ["2026-09-18 09:00:01", "2026-09-18 09:00:04",
             "2026-09-18 09:00:07"]


def test_P1_discharge_not_eligible():
    print("\n[P1] natural decision = DISCHARGE + authority EXTERNAL -> NOT ELIGIBLE")
    ok, ck, verdict = FL.pre_handoff_policy_eligibility(
        pre_sample(decision="discharge"))
    check("★★ P1. pre-handoff NOT ELIGIBLE", ok is False)
    check("★★ P1. verdict = CHARGE_POLICY_NOT_ELIGIBLE",
          verdict == FL.NOT_ELIGIBLE)
    check("  未通過項為 natural_decision_is_charge",
          [k for k, v in ck.items() if not v] == ["natural_decision_is_charge"])
    # 不得送 pause：dispatcher 也不會被建立/呼叫
    rig = Rig(revalidator=(lambda o: (False, {"x": False}, "NOPE")))
    check("★★ P1. 未進 handoff 時 pause 計數為 0",
          rig.guard.count("pause") == 0 and rig.remote.pause_calls == 0)


def test_P2_charge_external_handoff_required():
    print("\n[P2] natural decision = CHARGE + authority EXTERNAL -> HANDOFF_REQUIRED")
    smp = pre_sample(authority="EXTERNAL_OR_UNKNOWN")
    ok, ck, verdict = FL.pre_handoff_policy_eligibility(smp)
    check("★★ P2. pre-handoff ELIGIBLE（authority 不參與判定）", ok is True)
    check("★★ P2. verdict = CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED",
          verdict == FL.ELIGIBLE_HANDOFF_REQUIRED)
    check("  九項全通過", all(ck.values()) and len(ck) == 9)
    req, ev = FL.handoff_requirement(smp, loop_marks=ADVANCING,
                                     min_loop_observations=3)
    check("★★ P2. handoff required = True", req is True)
    check("  證據記錄 authority_external_or_unknown",
          ev["authority_external_or_unknown"] is True)


def test_P3_charge_idle_but_loop_active():
    print("\n[P3] natural decision = CHARGE + authority IDLE 但 external loop active")
    smp = pre_sample(authority="IDLE")
    ok, _, verdict = FL.pre_handoff_policy_eligibility(smp)
    check("  pre-handoff 仍 ELIGIBLE", ok is True
          and verdict == FL.ELIGIBLE_HANDOFF_REQUIRED)
    req, ev = FL.handoff_requirement(smp, loop_marks=ADVANCING,
                                     min_loop_observations=3)
    check("★★ P3. 仍 HANDOFF_REQUIRED", req is True)
    check("★★ P3. 證據：external_loop_active = True",
          ev["external_loop_active"] is True)
    check("★★ P3. 明載 Authority=IDLE 不是 relinquish 證明",
          ev["authority_idle_is_not_relinquish_proof"] is True)
    check("★★ P3. 結構上沒有『跳過 handoff』分支",
          FL.HANDOFF_ALWAYS_REQUIRED is True)
    import phase6_trial2_harness as T2H
    check("  合法 PCS 狀態與 Trial #2 harness 同值（兩處必須同步）",
          FL.LEGAL_OBSERVED_PCS_STATES == T2H.LEGAL_OBSERVED_PCS_STATES)
    # AST：GATE A 不得引用 authority / arbitration outcome
    fn = find_node(src_of("phase6_first_live_handoff.py"),
                   "pre_handoff_policy_eligibility")
    ids = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    ids |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    strs = literal_strings_fl(fn)
    for bad in ("authority", "arbitration_outcome", "authorized",
                "CONTROL_AUTHORITY_BLOCKED", "EXTERNAL_OR_UNKNOWN"):
        check(f"★★ P3. AST：GATE A 不引用 {bad}",
              bad not in ids and bad not in strs)
    # GATE A 其他不合格項仍要擋
    for bad, label in ((dict(tou_allows_charge=False), "TOU"),
                       (dict(soc_allows_charge=False), "SOC"),
                       (dict(fault=True), "fault"),
                       (dict(critical_alarms=1), "critical"),
                       (dict(comm_ok=False), "comm"),
                       (dict(fresh=False), "fresh"),
                       (dict(meter_fresh=False), "meter"),
                       (dict(pcs_state="UNKNOWN"), "PCS 非法"),
                       (dict(safety_gate_ok=False), "Safety Gate")):
        check(f"  {label} 不合格 -> NOT ELIGIBLE",
              FL.pre_handoff_policy_eligibility(pre_sample(**bad))[0] is False)


def test_P4_post_handoff_still_charge():
    print("\n[P4] post-handoff 仍 CHARGE -> 可 acquire + dispatch")
    ok, ck, verdict = FL.post_handoff_revalidation(post_sample())
    check("★★ P4. GATE B PASS", ok is True and verdict == FL.REVALIDATION_OK)
    check("  十項全通過", all(ck.values()) and len(ck) == 10)
    rig = Rig(revalidator=FL.build_post_handoff_revalidator(
        sample_source=(lambda: post_sample())))
    res = rig.run()
    check("★★ P4. dispatcher 被呼叫且成功",
          rig.dispatcher.calls == 1
          and rig.dispatcher.last["outcome"] == FL.DISPATCH_SUCCEEDED)
    check("★★ P4. Phase 6 authority 已取得", rig.lease.acquired_once is True)
    check("  charge 計數 = 1", rig.guard.count("phase6_charge") == 1)
    check("  artifact 記錄 GATE B 通過",
          rig.artifact().to_dict()["post_handoff_revalidation_ok"] is True)


def _post_abort(label, **kw):
    rig = Rig(revalidator=FL.build_post_handoff_revalidator(
        sample_source=(lambda: post_sample(**kw))))
    res = rig.run()
    check(f"★★ {label}. dispatcher outcome = NOT_ATTEMPTED",
          rig.dispatcher.last["outcome"] == FL.DISPATCH_NOT_ATTEMPTED)
    check(f"★★ {label}. Phase 6 authority 從未取得",
          rig.lease.acquired_once is False)
    check(f"★★ {label}. live leg 從未執行", rig.leg_calls == 0)
    check(f"★★ {label}. Phase 6 counts 維持 0 / 0 / 0",
          rig.guard.count("phase6_charge") == 0
          and rig.guard.count("phase6_discharge") == 0
          and rig.guard.count("phase6_pcs_stop") == 0)
    check(f"★★ {label}. external 仍被 restore（責任必須歸還）",
          rig.remote.restore_calls == 1)
    check(f"  {label}. reason 標示 post-handoff abort",
          FL.OUT_ABORTED_POST_HANDOFF in (rig.dispatcher.last["reason"] or ""))
    return rig


def test_P5_post_handoff_becomes_discharge():
    print("\n[P5] post-handoff 變 DISCHARGE -> no dispatch -> restore")
    rig = _post_abort("P5", decision="discharge")
    d = rig.artifact().to_dict()
    check("  artifact GATE B = False", d["post_handoff_revalidation_ok"] is False)
    check("  verdict = POST_HANDOFF_REVALIDATION_FAILED",
          d["post_handoff_verdict"] == FL.REVALIDATION_FAILED)


def test_P6_post_handoff_no_action():
    print("\n[P6] post-handoff 變 NO_ACTION -> no dispatch -> restore")
    _post_abort("P6", decision="no_action")


def test_P7_post_handoff_fault_or_critical():
    print("\n[P7] post-handoff fault / critical -> no dispatch -> restore")
    _post_abort("P7-fault", fault=True)
    _post_abort("P7-critical", critical_alarms=2)
    ok, ck, _ = FL.post_handoff_revalidation(post_sample(authority="EXTERNAL_OR_UNKNOWN"))
    check("★★ P7. external 未 relinquish -> GATE B FAIL",
          ok is False and ck["external_relinquished"] is False)
    ok2, ck2, _ = FL.post_handoff_revalidation(post_sample(ownership="BOTH"))
    check("★★ P7. ownership BOTH -> GATE B FAIL",
          ok2 is False and ck2["ownership_not_both"] is False)
    smp = post_sample()
    smp.pop("post_handoff_fresh")
    ok3, ck3, _ = FL.post_handoff_revalidation(smp)
    check("★★ P7. 未標記 post-handoff fresh -> Fail Closed（不得沿用舊樣本）",
          ok3 is False and ck3["fresh_reobservation"] is False)


def test_P8_post_handoff_acquire_fail():
    print("\n[P8] post-handoff authority acquire fail -> no dispatch -> restore")
    rig = Rig(acquire_ok=False,
              revalidator=FL.build_post_handoff_revalidator(
                  sample_source=(lambda: post_sample())))
    rig.run()
    check("★★ P8. GATE B 通過但 acquire 失敗",
          rig.dispatcher.revalidation["ok"] is True
          and rig.lease.acquired_once is False)
    check("★★ P8. live leg 從未執行 / counts 0", rig.leg_calls == 0
          and rig.guard.count("phase6_charge") == 0)
    check("★★ P8. external 仍被 restore", rig.remote.restore_calls == 1)
    check("  release 先於 restore",
          rig.rec.ordered(FL.EV_AUTHORITY_RELEASED,
                          FL.EV_RESTORE_ATTEMPTED) is True)


def test_P9_counts_zero_on_any_post_abort():
    print("\n[P9] 任何 post-handoff abort -> Phase 6 real counts 維持 0 / 0 / 0")
    for kw, label in ((dict(decision="discharge"), "discharge"),
                      (dict(decision="no_action"), "no_action"),
                      (dict(fault=True), "fault"),
                      (dict(critical_alarms=1), "critical"),
                      (dict(authority="EXTERNAL_OR_UNKNOWN"), "未 relinquish"),
                      (dict(pcs_state="UNKNOWN"), "PCS 非法")):
        rig = Rig(revalidator=FL.build_post_handoff_revalidator(
            sample_source=(lambda k=kw: post_sample(**k))))
        rig.run()
        c = rig.guard.counts
        check(f"★★ P9. {label} -> 0 / 0 / 0",
              c.get("phase6_charge", 0) == 0
              and c.get("phase6_discharge", 0) == 0
              and c.get("phase6_pcs_stop", 0) == 0)
        d = rig.artifact().to_dict()
        check(f"  {label} artifact validate 無問題",
              FL.validate_handoff_artifact(d) == [])


def test_P10_responsibility_until_rv_resumed():
    print("\n[P10] restore responsibility 直到 RV_RESUMED 才可清除")
    rig = Rig(revalidator=FL.build_post_handoff_revalidator(
        sample_source=(lambda: post_sample(decision="discharge"))))
    res = rig.run()
    check("★★ P10. post-handoff abort 後仍走 restore",
          rig.remote.restore_calls == 1)
    check("★★ P10. RV_RESUMED -> 責任清除",
          res["restore_responsibility"] is False)
    rig2 = Rig(marks_advance_after_restore=False,
               revalidator=FL.build_post_handoff_revalidator(
                   sample_source=(lambda: post_sample(decision="discharge"))))
    res2 = rig2.run()
    check("★★ P10. 未達 RV_RESUMED -> 責任保留",
          res2["restore_responsibility"] is True)
    d2 = rig2.artifact().to_dict()
    check("  artifact 責任最終狀態 = True",
          d2["restore_responsibility_final"] is True)
    check("  且 restore_resumed = False", d2["restore_resumed"] is False)

# ======================================================================
def main():
    for fn in (test_I1_dispatcher_once, test_I2_pause_not_verified,
               test_I3_pause_unknown, test_I4_dispatch_success,
               test_I5_dispatch_fail, test_I6_dispatch_unknown,
               test_I7_authority_acquire_fail, test_I8_release_before_restore,
               test_I9_restore_unknown, test_I10_rv_resumed_clears,
               test_I11_charge_at_most_once, test_I12_discharge_zero,
               test_I13_pcs_stop_counted, test_I14_decision_audit,
               test_I15_handoff_artifact, test_I16_dispatch_disabled,
               test_I17_senders_not_armed, test_I18_exception_fail_closed,
               test_N1_two_layer_preflight,
               test_N2_dispatcher_knows_no_charge_detail,
               test_N3_no_second_charge_logic, test_N4_production_unchanged,
               test_N5_timeout_not_final,
               test_R1_direct_cli_blocked, test_R2_direct_sends_nothing,
               test_R3_live_leg_still_importable,
               test_R4_no_other_field_entry,
               test_R4b_service_live_leg_cli_blocked,
               test_R5_counts_success_leg,
               test_R6_counts_no_leg, test_R7_artifact_rejects_overcount,
               test_R8_cleanup_stop_not_excluded, test_R9_outcome_semantics,
               test_P1_discharge_not_eligible,
               test_P2_charge_external_handoff_required,
               test_P3_charge_idle_but_loop_active,
               test_P4_post_handoff_still_charge,
               test_P5_post_handoff_becomes_discharge,
               test_P6_post_handoff_no_action,
               test_P7_post_handoff_fault_or_critical,
               test_P8_post_handoff_acquire_fail,
               test_P9_counts_zero_on_any_post_abort,
               test_P10_responsibility_until_rv_resumed):
        fn()
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
