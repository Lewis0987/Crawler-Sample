# -*- coding: utf-8 -*-
"""
test_phase6_scheduled_first_live.py — Scheduled FIRST LIVE（ONE-SHOT）回歸
======================================================================
核心命題
    「一個 scheduled task = 最多一次 field attempt。
      條件不成立就結束，不等、不重試、不隔天再跑；
      crash 之後一律不重送 pause / charge / restore。」

使用者指定的 20 項
    S1  window 前啟動 -> 等到 window -> 只執行一次
    S2  window 過期   -> EXPIRED -> 0 commands
    S3  GATE A fail   -> no pause -> 0 commands
    S4  GATE A PASS   -> handoff 恰好一次
    S5  pause UNKNOWN -> no resend -> responsibility 保留
    S6  pause verified -> GATE B 使用 fresh sample
    S7  GATE B stale sample -> Fail Closed
    S8  GATE B decision 變 DISCHARGE -> no charge -> restore
    S9  GATE B TOU 離開 OFF_PEAK     -> no charge -> restore
    S10 authority acquire fail       -> no charge -> restore
    S11 successful leg -> charge=1 / discharge=0 / pcs_stop<=1
    S12 dispatcher UNKNOWN -> no retry
    S13 restore UNKNOWN    -> no retry -> responsibility remains
    S14 crash after pause  -> recovery 不 resend pause
    S15 crash after charge -> 不 resend charge
    S16 crash before restore -> responsibility 可恢復
    S17 task process restart -> one-shot 不重新從頭執行
    S18 PASS 後再次啟動 -> CONSUMED / refuse
    S19 ABORT 後再次啟動 -> CONSUMED / refuse
    S20 exception -> safe reset / durable evidence

額外結構鎖
    X1  不得複製 decision / Safety Gate / executor / live-leg / handoff
    X2  日期 / 時段不得寫死在 production code
    X3  定位常數：非 Unattended LIVE、非 recurring、非 auto retry
    X4  host runtime 判定 Fail Closed 且不修改任何電源設定
    X5  production safe defaults 未變

用法
    python test_phase6_scheduled_first_live.py        # exit 0 = PASS
"""
import ast
import datetime
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
import phase6_scheduled_first_live as SC            # noqa: E402

RESULTS = []
TMPDIRS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="sfl_")
    TMPDIRS.append(d)
    return d


# ======================================================================
# 替身
# ======================================================================
DAY = datetime.date(2026, 9, 22)
W0 = datetime.time(0, 0)
W1 = datetime.time(0, 30)


def auth(**kw):
    d = dict(attempt_id="A2-20260922", enabled=True, target_date=DAY,
             window_start=W0, window_end=W1, timezone_name="Asia/Taipei",
             created_at=datetime.datetime(2026, 9, 21, 15, 0, 0))
    d.update(kw)
    return SC.OneShotAuthorization(**d)


class Clock(object):
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def wait(self, sec):
        self.t = self.t + datetime.timedelta(seconds=float(sec or 0))


def artifact(**kw):
    """以**既有** HandoffExecutionArtifact 產生 schema-complete 的 artifact。

    🔴 不自建 artifact 形狀 —— 直接用 production 的 schema，
       這樣 `validate_handoff_artifact()` 的檢查才有意義。
    """
    base = FL.HandoffExecutionArtifact()
    d = {"pause_send_outcome": RA.SEND_SENT,
         "post_handoff_revalidation_ok": True,
         "post_handoff_verdict": FL.REVALIDATION_OK,
         "phase6_authority_acquired": True,
         "phase6_authority_released": True,
         "dispatcher_attempted": True,
         "dispatcher_outcome": FL.DISPATCH_SUCCEEDED,
         "dispatcher_reason": None,
         "restore_attempt_count": 1,
         "restore_send_outcome": RA.SEND_SENT,
         "restore_verifier_state": RA.RV_RESUMED,
         "restore_resumed": True,
         "post_check": {"ownership_not_both": True, "fault_clear": True,
                        "alarm_clear": True, "runtime_healthy": True,
                        "responsibility_cleared": True},
         "phase6_command_counts": {"counts": {"phase6_charge": 1,
                                              "phase6_pcs_stop": 1,
                                              "live_leg_internal_stop": 1}},
         "pcs_stop_reason": "LIVE_LEG_CLEANUP",
         "pause_attempt_count": 1, "normal_loop_halted": True,
         "pause_verifier_state": RA.PV_VERIFIED,
         "discriminative_pause_verified": True,
         "release_before_restore": True, "final_ownership": "EXTERNAL",
         "fault": False, "critical_alarms": 0,
         "restore_responsibility_final": False,
         "handoff_outcome": "COMPLETE",
         "safety_preflight_ok": True, "handoff_readiness_ok": True,
         "pre_handoff_eligibility_ok": True,
         "pre_handoff_verdict": FL.ELIGIBLE_HANDOFF_REQUIRED,
         "live_leg_outcome": {"leg_state": "COMPLETE"}}
    d.update(kw)
    unknown = [k for k in d if k not in FL.HANDOFF_ARTIFACT_FIELDS]
    assert not unknown, "fixture 用到未登記欄位：%s" % unknown
    base.set(**d)
    return base.to_dict()


class Rig(object):
    def __init__(self, now, a=None, preflight_ok=True, gate_a_ok=True,
                 readiness_ok=True, art=None, handoff_exc=None,
                 host=None, ledger_path=None, source_ok=True,
                 gate_b_sample=None):
        self.clock = Clock(now)
        self.auth = a or auth()
        self.path = ledger_path or os.path.join(tmpdir(), "oneshot.jsonl")
        self.ledger = SC.OneShotLedger(self.path, now_fn=self.clock)
        self.preflight_ok = preflight_ok
        self.gate_a_ok = gate_a_ok
        self.readiness_ok = readiness_ok
        self.art = art if art is not None else artifact()
        self.handoff_exc = handoff_exc
        self.host = host
        self.source_ok = source_ok
        self.gate_b_sample = gate_b_sample
        self.handoff_calls = 0
        self.reset_calls = 0

    def source_state(self):
        return {"integration_module_ok": self.source_ok,
                "runner_direct_blocked": True,
                "service_cli_blocked": True,
                "safe_defaults_ok": self.source_ok}

    def preflight(self):
        return (self.preflight_ok,
                {"communication_healthy": self.preflight_ok,
                 "fault_clear": True})

    def gate_a(self):
        return (self.gate_a_ok,
                {"natural_decision_is_charge": self.gate_a_ok,
                 "tou_allows_charge": self.gate_a_ok},
                FL.ELIGIBLE_HANDOFF_REQUIRED if self.gate_a_ok
                else FL.NOT_ELIGIBLE)

    def readiness(self):
        return (self.readiness_ok, {"pause_capability_available":
                                    self.readiness_ok})

    def handoff(self):
        self.handoff_calls += 1
        if self.handoff_exc is not None:
            raise self.handoff_exc
        # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-23 Attempt #3 postmortem）
        #   OLD : runner 在進入 handoff **之前**就寫 pause INTENT，
        #         因此 Rig 不需要做任何事，ledger 就會有 pause 意圖。
        #   NEW : INTENT 改由 pause sender 呼叫前一刻的 hook 落盤，
        #         所以 Rig 必須在「真的會送 pause」的情境下呼叫 hook ——
        #         這正是 production provider `_send("pause")` 的行為。
        #   WHY : 使用者裁示第 8 / 9 項：pre-pause preflight 就失敗、
        #         sender 從未被呼叫時，**不得**留下 pause INTENT 與責任。
        #   EVIDENCE : Attempt #3 ledger seq2/seq3 與 artifact
        #         pause_attempt_count=0 互相矛盾。
        #   🔴 斷言本身未放寬：S4/S5 仍要求 INTENT 與責任成立，
        #      只是改由正確的時點產生。
        if (self.art or {}).get("pause_send_outcome") is not None:
            hook = getattr(self, "_pause_hook", None)
            if hook is not None:
                hook()
        return {"outcome": "COMPLETE"}

    def register_pause_hook(self, fn):
        self._pause_hook = fn

    def collect(self, res):
        return self.art

    def reset(self):
        self.reset_calls += 1
        return {"dispatch_enabled": False, "senders_armed": False,
                "pause_timeout": None, "restore_timeout": None,
                "first_live_prerequisite": False}

    def runner(self, **kw):
        d = dict(auth=self.auth, ledger=self.ledger, now_fn=self.clock,
                 source_state_check=self.source_state,
                 safety_preflight=self.preflight, gate_a=self.gate_a,
                 handoff_readiness=self.readiness,
                 arm_and_run_handoff=self.handoff,
                 register_pause_intent_hook=self.register_pause_hook,
                 collect_artifact=self.collect, safety_reset=self.reset,
                 host_evidence=(lambda: self.host) if self.host else None,
                 waiter=self.clock.wait)
        d.update(kw)
        return SC.ScheduledFirstLiveRunner(**d)


INSIDE = datetime.datetime(2026, 9, 22, 0, 10, 0)
BEFORE = datetime.datetime(2026, 9, 21, 23, 40, 0)
AFTER = datetime.datetime(2026, 9, 22, 1, 0, 0)


# ======================================================================
# S1 ~ S2：time window
# ======================================================================
def test_S1_wait_then_run_once():
    print("\n[S1] window 前啟動 -> 等到 window -> 只執行一次")
    rig = Rig(BEFORE)
    ev = rig.runner().run()
    check("★★ S1. 有等待且進入 window",
          ev["waited_sec"] > 0 and ev["window_check_result"] == SC.WIN_INSIDE)
    check("★★ S1. handoff 恰好執行一次", rig.handoff_calls == 1)
    check("★★ S1. result = PASS", ev["result"] == SC.R_PASS)
    check("  one_shot_consumed = True", ev["one_shot_consumed"] is True)
    ev2 = rig.runner().run()
    check("★★ S1. 第二次啟動被拒（不會再跑一次）",
          ev2["result"] == SC.R_ALREADY_CONSUMED and rig.handoff_calls == 1)
    # 超過允許等待時間
    rig2 = Rig(BEFORE)
    ev3 = rig2.runner(max_wait_sec=60).run()
    check("★★ S1. 等待超過上限 -> EXPIRED，不執行",
          ev3["result"] == SC.R_EXPIRED and rig2.handoff_calls == 0)


def test_S2_expired():
    print("\n[S2] window 過期 -> EXPIRED -> 0 commands")
    rig = Rig(AFTER)
    ev = rig.runner().run()
    check("★★ S2. result = EXPIRED_NO_ATTEMPT", ev["result"] == SC.R_EXPIRED)
    check("★★ S2. handoff 從未執行", rig.handoff_calls == 0)
    check("★★ S2. ledger 無 pause / charge 意圖",
          not rig.ledger.attempted("pause")
          and not rig.ledger.attempted("charge"))
    check("  仍執行 safety reset", rig.reset_calls == 1)
    rig2 = Rig(datetime.datetime(2026, 9, 23, 0, 10))
    check("★★ S2. 非授權日期 -> WRONG_TARGET_DATE",
          rig2.runner().run()["result"] == SC.R_WRONG_DATE)


# ======================================================================
# S3 ~ S4：GATE A
# ======================================================================
def test_S3_gate_a_fail():
    print("\n[S3] GATE A fail -> no pause -> 0 commands")
    rig = Rig(INSIDE, gate_a_ok=False)
    ev = rig.runner().run()
    check("★★ S3. result = ABORTED_CURRENT_CONDITIONS",
          ev["result"] == SC.R_ABORTED_CURRENT)
    check("★★ S3. handoff 從未執行（no arm / no pause）",
          rig.handoff_calls == 0)
    check("★★ S3. ledger 無 pause 意圖", not rig.ledger.attempted("pause"))
    check("★★ S3. ONE-SHOT 已消耗（不等下一筆 CHARGE）",
          rig.ledger.consumed is True)
    for bad, res in ((dict(preflight_ok=False), SC.R_ABORTED_PREFLIGHT),
                     (dict(readiness_ok=False), SC.R_ABORTED_READINESS),
                     (dict(source_ok=False), SC.R_ABORTED_PREFLIGHT)):
        r = Rig(INSIDE, **bad)
        check(f"  {list(bad)[0]} -> {res}", r.runner().run()["result"] == res
              and r.handoff_calls == 0)


def test_S4_gate_a_pass_handoff_once():
    print("\n[S4] GATE A PASS -> handoff 恰好一次")
    rig = Rig(INSIDE)
    ev = rig.runner().run()
    check("★★ S4. handoff 恰好一次", rig.handoff_calls == 1)
    check("  gate_a verdict = HANDOFF_REQUIRED",
          ev["gate_a"]["verdict"] == FL.ELIGIBLE_HANDOFF_REQUIRED)
    check("  pause 意圖先於執行落盤", rig.ledger.attempted("pause"))
    check("  lifecycle 曾進入責任狀態",
          any(r.get("lifecycle") == SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY
              for r in rig.ledger.records))


# ======================================================================
# S5 ~ S13：既有 handoff 結果的分類
# ======================================================================
def _res(art_kw):
    rig = Rig(INSIDE, art=artifact(**art_kw))
    return rig, rig.runner().run()


def test_S5_pause_unknown():
    print("\n[S5] pause UNKNOWN -> no resend -> responsibility 保留")
    rig, ev = _res(dict(pause_send_outcome=RA.SEND_UNKNOWN,
                        restore_resumed=False,
                        restore_verifier_state=RA.RV_UNKNOWN))
    check("★★ S5. result = PAUSE_UNKNOWN", ev["result"] == SC.R_PAUSE_UNKNOWN)
    check("★★ S5. handoff 只跑一次（無重送）", rig.handoff_calls == 1)
    check("★★ S5. ledger 責任仍成立",
          rig.ledger.restore_responsibility is True)
    rig2, ev2 = _res(dict(pause_send_outcome=RA.SEND_NOT_SENT))
    check("  pause NOT_SENT -> PAUSE_FAILED",
          ev2["result"] == SC.R_PAUSE_FAILED)


def test_S6_gate_b_fresh():
    print("\n[S6] pause verified -> GATE B 使用 fresh sample")
    smp = {"post_handoff_fresh": True, "decision": "charge",
           "tou_allows_charge": True, "soc_allows_charge": True,
           "fault": False, "critical_alarms": 0, "comm_ok": True,
           "pcs_state": "STANDBY", "ownership": "NEITHER",
           "authority": "IDLE"}
    ok, ck, verdict = FL.post_handoff_revalidation(smp)
    check("★★ S6. fresh sample -> GATE B PASS",
          ok is True and verdict == FL.REVALIDATION_OK)
    check("  fresh_reobservation 已確認", ck["fresh_reobservation"] is True)
    rig, ev = _res({})
    check("  artifact 記錄 GATE B 結果",
          ev["handoff_artifact"]["post_handoff_revalidation_ok"] is True)


def test_S7_gate_b_stale():
    print("\n[S7] GATE B stale sample -> Fail Closed")
    smp = {"decision": "charge", "tou_allows_charge": True,
           "soc_allows_charge": True, "fault": False, "critical_alarms": 0,
           "comm_ok": True, "pcs_state": "STANDBY", "ownership": "NEITHER",
           "authority": "IDLE"}          # 缺 post_handoff_fresh
    ok, ck, _ = FL.post_handoff_revalidation(smp)
    check("★★ S7. 未標記 fresh -> Fail Closed",
          ok is False and ck["fresh_reobservation"] is False)


def test_S8_gate_b_discharge():
    print("\n[S8] GATE B decision 變 DISCHARGE -> no charge -> restore")
    rig, ev = _res(dict(post_handoff_revalidation_ok=False,
                        post_handoff_verdict=FL.REVALIDATION_FAILED,
                        phase6_authority_acquired=False,
                        dispatcher_attempted=False,
                        dispatcher_outcome=FL.DISPATCH_NOT_ATTEMPTED,
                        phase6_command_counts={"counts": {}},
                        pcs_stop_reason=None))
    check("★★ S8. result = ABORTED_POST_HANDOFF_REVALIDATION",
          ev["result"] == SC.R_ABORTED_POST_HANDOFF)
    check("★★ S8. 無 charge 意圖", not rig.ledger.attempted("charge"))
    check("★★ S8. 仍有 restore", rig.ledger.attempted("restore"))


def test_S9_gate_b_tou_left_offpeak():
    print("\n[S9] GATE B TOU 已離開 OFF_PEAK -> no charge -> restore")
    smp = {"post_handoff_fresh": True, "decision": "charge",
           "tou_allows_charge": False, "soc_allows_charge": True,
           "fault": False, "critical_alarms": 0, "comm_ok": True,
           "pcs_state": "STANDBY", "ownership": "NEITHER",
           "authority": "IDLE"}
    ok, ck, _ = FL.post_handoff_revalidation(smp)
    check("★★ S9. TOU 不允許 -> GATE B FAIL",
          ok is False and ck["tou_allows_charge"] is False)
    rig, ev = _res(dict(post_handoff_revalidation_ok=False,
                        post_handoff_verdict=FL.REVALIDATION_FAILED,
                        phase6_authority_acquired=False,
                        dispatcher_attempted=False,
                        dispatcher_outcome=FL.DISPATCH_NOT_ATTEMPTED,
                        phase6_command_counts={"counts": {}},
                        pcs_stop_reason=None))
    check("★★ S9. runner 分類為 post-handoff abort 且仍 restore",
          ev["result"] == SC.R_ABORTED_POST_HANDOFF
          and rig.ledger.attempted("restore"))


def test_S10_authority_fail():
    print("\n[S10] authority acquire fail -> no charge -> restore")
    # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-23 Attempt #3 postmortem）
    #   OLD : 只要 `phase6_authority_acquired is False` 且 dispatcher
    #         NOT_ATTEMPTED，就分類成 AUTHORITY_ACQUIRE_FAILED。
    #   NEW : 另要求 `authority_acquire_attempted is True` ——
    #         只有 `Phase6AuthorityLease.acquire()` **真的被呼叫過**
    #         且回 FAIL，才允許這個終態。
    #   WHY : 使用者裁示第 7 項（RESULT CLASSIFICATION DEFECT）。
    #         Attempt #3 在 pre-pause preflight 就中止，acquire() 一次
    #         都沒被呼叫，卻被記成 AUTHORITY_ACQUIRE_FAILED。
    #   EVIDENCE : Attempt #3 artifact
    #         phase6_authority_detail.acquire_reason = None、
    #         handoff_abort_step = 1_PRE_PAUSE_PREFLIGHT。
    #   🔴 這是**收緊**（多一個必要條件），不是放寬。
    rig, ev = _res(dict(phase6_authority_acquired=False,
                        authority_acquire_attempted=True,
                        dispatcher_attempted=False,
                        dispatcher_outcome=FL.DISPATCH_NOT_ATTEMPTED,
                        dispatcher_reason="PHASE6_AUTHORITY_NOT_ACQUIRED:held",
                        phase6_command_counts={"counts": {}},
                        pcs_stop_reason=None))
    check("★★ S10. result = AUTHORITY_ACQUIRE_FAILED",
          ev["result"] == SC.R_AUTHORITY_FAILED)
    check("★★ S10. 無 charge 意圖", not rig.ledger.attempted("charge"))
    check("★★ S10. 仍 restore", rig.ledger.attempted("restore"))


def test_S11_successful_leg_counts():
    print("\n[S11] successful leg -> charge=1 / discharge=0 / pcs_stop<=1")
    rig, ev = _res({})
    check("★★ S11. result = PASS", ev["result"] == SC.R_PASS)
    c = ev["handoff_artifact"]["phase6_command_counts"]["counts"]
    check("★★ S11. charge = 1", c["phase6_charge"] == 1)
    check("★★ S11. discharge = 0", c.get("phase6_discharge", 0) == 0)
    check("★★ S11. pcs_stop = 1 <= 1", c["phase6_pcs_stop"] == 1)
    check("  cleanup STOP 計入 aggregate 且有 reason",
          c["live_leg_internal_stop"] == 1
          and ev["handoff_artifact"]["pcs_stop_reason"] == "LIVE_LEG_CLEANUP")
    check("  ledger lifecycle 收斂到 RESTORED",
          rig.ledger.lifecycle == SC.LC_RESTORED)


def test_S12_dispatch_unknown_no_retry():
    print("\n[S12] dispatcher UNKNOWN -> no retry")
    rig, ev = _res(dict(dispatcher_outcome=FL.DISPATCH_UNKNOWN))
    check("★★ S12. result = DISPATCH_UNKNOWN",
          ev["result"] == SC.R_DISPATCH_UNKNOWN)
    check("★★ S12. handoff 只跑一次", rig.handoff_calls == 1)
    n = sum(1 for r in rig.ledger.records
            if r.get("kind") == "INTENT" and r.get("verb") == "charge")
    check("★★ S12. charge 意圖只有一筆（無 retry）", n == 1)
    rig2, ev2 = _res(dict(dispatcher_outcome=FL.DISPATCH_FAILED))
    check("  dispatcher FAILED -> DISPATCH_FAILED",
          ev2["result"] == SC.R_DISPATCH_FAILED)


def test_S13_restore_unknown():
    print("\n[S13] restore UNKNOWN -> no retry -> responsibility remains")
    rig, ev = _res(dict(restore_send_outcome=RA.SEND_UNKNOWN,
                        restore_verifier_state=RA.RV_UNKNOWN,
                        restore_resumed=False))
    check("★★ S13. result = RESTORE_UNKNOWN",
          ev["result"] == SC.R_RESTORE_UNKNOWN)
    n = sum(1 for r in rig.ledger.records
            if r.get("kind") == "INTENT" and r.get("verb") == "restore")
    check("★★ S13. restore 意圖只有一筆", n == 1)
    check("★★ S13. 責任保留", rig.ledger.restore_responsibility is True)
    rig2, ev2 = _res(dict(restore_send_outcome=RA.SEND_NOT_SENT,
                          restore_verifier_state=RA.RV_FAILED,
                          restore_resumed=False))
    check("  restore NOT_SENT -> RESTORE_FAILED",
          ev2["result"] == SC.R_RESTORE_FAILED)


# ======================================================================
# S14 ~ S20：crash / restart / consumption
# ======================================================================
def test_S14_crash_after_pause():
    print("\n[S14] crash after pause -> recovery 不 resend pause")
    path = os.path.join(tmpdir(), "l.jsonl")
    led = SC.OneShotLedger(path, now_fn=Clock(INSIDE))
    led.mark(SC.LC_PREFLIGHT)
    led.mark_intent("pause")
    led.mark(SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY)
    # --- 模擬 process crash：重新載入 ledger ---
    led2 = SC.OneShotLedger(path, now_fn=Clock(INSIDE))
    v, act = SC.recover(led2)
    check("★★ S14. recovery = PENDING_RESTORE", v == SC.REC_PENDING_RESTORE)
    check("★★ S14. 責任由 durable evidence 恢復",
          led2.restore_responsibility is True)
    rig = Rig(INSIDE, ledger_path=path)
    ev = rig.runner().run()
    check("★★ S14. **不重跑流程、不重送 pause**", rig.handoff_calls == 0)
    check("★★ S14. result = RESTORE_UNKNOWN（Fail Closed）",
          ev["result"] == SC.R_RESTORE_UNKNOWN)
    n = sum(1 for r in rig.ledger.records
            if r.get("kind") == "INTENT" and r.get("verb") == "pause")
    check("  pause 意圖仍只有一筆", n == 1)


def test_S15_crash_after_charge():
    print("\n[S15] crash after charge -> 不 resend charge")
    path = os.path.join(tmpdir(), "l.jsonl")
    led = SC.OneShotLedger(path, now_fn=Clock(INSIDE))
    for f in (lambda: led.mark(SC.LC_PREFLIGHT),
              lambda: led.mark_intent("pause"),
              lambda: led.mark(SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY),
              lambda: led.mark(SC.LC_PHASE6_AUTHORITY),
              lambda: led.mark_intent("charge"),
              lambda: led.mark(SC.LC_LIVE_LEG)):
        f()
    led2 = SC.OneShotLedger(path, now_fn=Clock(INSIDE))
    v, _ = SC.recover(led2)
    check("★★ S15. recovery = PENDING_RESTORE", v == SC.REC_PENDING_RESTORE)
    rig = Rig(INSIDE, ledger_path=path)
    rig.runner().run()
    check("★★ S15. handoff 未重跑（charge 不重送）", rig.handoff_calls == 0)
    n = sum(1 for r in rig.ledger.records
            if r.get("kind") == "INTENT" and r.get("verb") == "charge")
    check("★★ S15. charge 意圖仍只有一筆", n == 1)


def test_S16_crash_before_restore():
    print("\n[S16] crash before restore -> responsibility 可恢復")
    path = os.path.join(tmpdir(), "l.jsonl")
    led = SC.OneShotLedger(path, now_fn=Clock(INSIDE))
    led.mark(SC.LC_PREFLIGHT)
    led.mark_intent("pause")
    led.mark(SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY)
    led.mark(SC.LC_PHASE6_RELEASED)
    led2 = SC.OneShotLedger(path, now_fn=Clock(INSIDE))
    check("★★ S16. 責任可從 durable evidence 恢復",
          led2.restore_responsibility is True)
    check("★★ S16. lifecycle 屬 PENDING_RESTORE 集合",
          led2.lifecycle in SC.PENDING_RESTORE_LIFECYCLE)
    v, _ = SC.recover(led2)
    check("  recovery = PENDING_RESTORE", v == SC.REC_PENDING_RESTORE)
    # 半截最後一行（crash 當下未寫完）不得破壞前面狀態
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "LIFEC')
    led3 = SC.OneShotLedger(path, now_fn=Clock(INSIDE))
    check("★★ S16. 半截尾行被忽略，前序狀態仍完整",
          led3.lifecycle == SC.LC_PHASE6_RELEASED
          and led3.restore_responsibility is True)


def test_S17_restart_does_not_rerun():
    print("\n[S17] task process restart -> one-shot 不重新從頭執行")
    rig = Rig(INSIDE)
    rig.runner().run()
    first = rig.handoff_calls
    led2 = SC.OneShotLedger(rig.path, now_fn=rig.clock)
    check("  重新載入的 ledger 標記已消耗", led2.consumed is True)
    rig2 = Rig(INSIDE, ledger_path=rig.path)
    ev = rig2.runner().run()
    check("★★ S17. 重啟後不重新執行",
          rig2.handoff_calls == 0 and first == 1)
    check("★★ S17. result = ALREADY_CONSUMED",
          ev["result"] == SC.R_ALREADY_CONSUMED)


def test_S18_pass_then_consumed():
    print("\n[S18] PASS 後再次啟動 -> CONSUMED / refuse")
    rig = Rig(INSIDE)
    ev = rig.runner().run()
    check("  第一次 PASS", ev["result"] == SC.R_PASS)
    ev2 = rig.runner().run()
    check("★★ S18. 第二次 ALREADY_CONSUMED",
          ev2["result"] == SC.R_ALREADY_CONSUMED)
    check("★★ S18. handoff 仍只跑過一次", rig.handoff_calls == 1)


def test_S19_abort_then_consumed():
    print("\n[S19] ABORT 後再次啟動 -> CONSUMED / refuse")
    for kw, res in ((dict(gate_a_ok=False), SC.R_ABORTED_CURRENT),
                    (dict(preflight_ok=False), SC.R_ABORTED_PREFLIGHT)):
        rig = Rig(INSIDE, **kw)
        ev = rig.runner().run()
        check(f"  第一次 {res}", ev["result"] == res)
        ev2 = rig.runner().run()
        check(f"★★ S19. {res} 後第二次 ALREADY_CONSUMED",
              ev2["result"] == SC.R_ALREADY_CONSUMED
              and rig.handoff_calls == 0)
    rig3 = Rig(AFTER)
    rig3.runner().run()
    check("★★ S19. EXPIRED 後同樣 CONSUMED",
          rig3.runner().run()["result"] == SC.R_ALREADY_CONSUMED)


def test_S20_exception_safe_reset():
    print("\n[S20] exception -> safe reset / durable evidence")
    rig = Rig(INSIDE, handoff_exc=RuntimeError("SSH 連線中斷"))
    ev = rig.runner().run()
    check("★★ S20. 例外不外漏，result = EXCEPTION_FAIL_CLOSED",
          ev["result"] == SC.R_EXCEPTION)
    check("★★ S20. safety reset 仍執行", rig.reset_calls == 1
          and ev["safety_reset"]["senders_armed"] is False)
    check("★★ S20. ONE-SHOT 已消耗", ev["one_shot_consumed"] is True
          and rig.ledger.consumed is True)
    # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-23 Attempt #3 postmortem）
    #   OLD : 「handoff 丟例外 -> ledger 必有 pause 意圖且責任成立」。
    #         那是舊 ordering 的產物 —— INTENT 在進入 handoff 前就寫死，
    #         所以**任何**例外都會留下責任，包含根本還沒送 pause 的例外。
    #   NEW : 依例外發生的位置分別斷言 ——
    #         (a) 例外發生在 pause sender **之前** -> 無 INTENT、無責任；
    #         (b) INTENT 已落盤之後才丟例外       -> 責任成立（不得遺漏）。
    #   WHY : 使用者裁示第 9 項（RESPONSIBILITY CONTRACT）：
    #         pre-pause 就失敗且 sender 從未呼叫 -> responsibility = False，
    #         不得留下 PENDING_RESTORE。
    #   EVIDENCE : Attempt #3 ledger seq2/seq3 標了責任，
    #         但 artifact pause_attempt_count=0、final_ownership=EXTERNAL，
    #         現場其實從未被暫停。
    #   🔴 這是**收緊**：新增 (a) 情境的嚴格斷言，並保留 (b) 的原有要求。
    check("★★ S20. 例外發生在 pause sender 之前 -> 無 pause 意圖",
          rig.ledger.attempted("pause") is False)
    check("★★ S20. 例外發生在 pause sender 之前 -> 責任 = False",
          rig.ledger.restore_responsibility is False)
    ev2 = rig.runner().run()
    check("  例外後再次啟動亦被拒", ev2["result"] == SC.R_ALREADY_CONSUMED)

    # (b) INTENT 已落盤之後才丟例外 -> 責任必須保留
    rigb = Rig(INSIDE)
    hold = {}

    def handoff_after_intent():
        rigb.handoff_calls += 1
        hold["hook"]()                       # sender 前一刻落盤
        raise RuntimeError("SSH 連線中斷（送出後失聯）")

    rb = rigb.runner(arm_and_run_handoff=handoff_after_intent,
                     register_pause_intent_hook=(
                         lambda fn: hold.__setitem__("hook", fn)))
    evb = rb.run()
    check("★★ S20b. INTENT 已落盤後才例外 -> result = EXCEPTION_FAIL_CLOSED",
          evb["result"] == SC.R_EXCEPTION)
    check("★★ S20b. INTENT 已落盤後才例外 -> 責任 = True（不得遺漏）",
          rigb.ledger.attempted("pause") is True
          and rigb.ledger.restore_responsibility is True)
    check("★★ S20b. 仍只有一筆 pause INTENT（不得重送）",
          sum(1 for x in rigb.ledger.records
              if x.get("kind") == "INTENT" and x.get("verb") == "pause") == 1)


# ======================================================================
# X 系列：結構鎖
# ======================================================================
def src_of(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


def test_X1_no_duplicate_logic():
    print("\n[X1] 不得複製 decision / Safety Gate / executor / live-leg / handoff")
    tree = ast.parse(src_of("phase6_scheduled_first_live.py"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    for bad in ("decision_engine", "decision_policy", "safety_gate",
                "pcs_control_executor", "device_control_operator",
                "pcs_auto_control_service", "pcs_control_integration",
                "control_authority", "tou_calendar", "subprocess", "requests"):
        check(f"★★ X1. 未 import {bad}", bad not in mods)
    check("  只相依 FIRST LIVE 整合層",
          mods & {"phase6_first_live_handoff", "phase6_handoff_orchestrator"}
          == {"phase6_first_live_handoff"})
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for bad in ("run_live_leg_cli", "live_leg_main", "build_executor",
                "evaluate", "classify_tou", "acquire_ownership"):
        check(f"  未呼叫 {bad}", bad not in calls)


def test_X2_no_hardcoded_schedule():
    print("\n[X2] 日期 / 時段不得寫死在 production code")
    for args in (dict(target_date=None), dict(window_start=None),
                 dict(window_end=None), dict(timezone_name=None),
                 dict(attempt_id="")):
        try:
            auth(**args)
            raised = False
        except ValueError:
            raised = True
        check(f"★★ X2. {list(args)[0]} 缺值 -> 拒絕", raised)
    try:
        auth(window_start=datetime.time(1, 0), window_end=datetime.time(0, 30))
        raised = False
    except ValueError:
        raised = True
    check("★★ X2. window_start 必須早於 window_end", raised)
    check("  enabled 非 True 一律視為未授權",
          auth(enabled=None).enabled is False)
    rig = Rig(INSIDE, a=auth(enabled=None))
    check("★★ X2. 未授權 -> NOT_ENABLED 且不執行",
          rig.runner().run()["result"] == SC.R_NOT_ENABLED
          and rig.handoff_calls == 0)
    tree = ast.parse(src_of("phase6_scheduled_first_live.py"))
    strs = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("★★ X2. 原始碼未出現寫死的時區 / 時段字面值",
          "Asia/Taipei" not in strs and "00:00" not in strs
          and "00:30" not in strs)


def test_X3_positioning_constants():
    print("\n[X3] 定位常數：非 Unattended LIVE、非 recurring、非 auto retry")
    check("★★ X3. mode = ONE_SHOT_COMMISSIONING",
          SC.SCHEDULED_MODE == "ONE_SHOT_COMMISSIONING")
    check("★★ X3. IS_UNATTENDED_LIVE = False", SC.IS_UNATTENDED_LIVE is False)
    check("★★ X3. IS_RECURRING = False", SC.IS_RECURRING is False)
    check("★★ X3. AUTO_RETRY_ALLOWED = False", SC.AUTO_RETRY_ALLOWED is False)
    d = auth().as_dict()
    check("  授權 evidence 帶上定位欄位",
          d["is_unattended_live"] is False and d["is_recurring"] is False
          and d["auto_retry_allowed"] is False)
    check("  result states 覆蓋使用者列出的 15 項",
          all(x in SC.RESULT_STATES for x in (
              SC.R_EXPIRED, SC.R_ABORTED_PREFLIGHT, SC.R_ABORTED_CURRENT,
              SC.R_ABORTED_READINESS, SC.R_PAUSE_FAILED, SC.R_PAUSE_UNKNOWN,
              SC.R_ABORTED_POST_HANDOFF, SC.R_AUTHORITY_FAILED,
              SC.R_DISPATCH_FAILED, SC.R_DISPATCH_UNKNOWN,
              SC.R_LIVE_LEG_VERIFY_FAILED, SC.R_RESTORE_FAILED,
              SC.R_RESTORE_UNKNOWN, SC.R_POSTCHECK_FAILED, SC.R_PASS)))


def test_X4_host_runtime_fail_closed():
    print("\n[X4] host runtime 判定 Fail Closed 且不修改任何電源設定")
    ok, ck = SC.evaluate_host_runtime(
        dict((k, True) for k in SC.HOST_ITEMS))
    check("  全部具備 -> PASS", ok is True and all(ck.values()))
    for k in SC.HOST_ITEMS:
        e = dict((x, True) for x in SC.HOST_ITEMS)
        e[k] = None
        check(f"★★ X4. {k} 未知 -> Fail Closed",
              SC.evaluate_host_runtime(e)[0] is False)
    check("★★ X4. 空 evidence -> Fail Closed",
          SC.evaluate_host_runtime(None)[0] is False)
    fn = next(n for n in ast.walk(ast.parse(
        src_of("phase6_scheduled_first_live.py")))
        if isinstance(n, ast.FunctionDef) and n.name == "evaluate_host_runtime")
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(fn) if isinstance(n, ast.Call)}
    for bad in ("run", "Popen", "system", "powercfg", "check_output"):
        check(f"  不執行系統呼叫 {bad}", bad not in calls)
    rig = Rig(INSIDE, host=dict((k, True) for k in SC.HOST_ITEMS))
    check("  host 齊備時流程照常", rig.runner().run()["result"] == SC.R_PASS)
    rig2 = Rig(INSIDE, host={"no_sleep": True})
    check("★★ X4. host 不齊 -> ABORTED_PREFLIGHT 且不 handoff",
          rig2.runner().run()["result"] == SC.R_ABORTED_PREFLIGHT
          and rig2.handoff_calls == 0)


def test_X5_production_defaults():
    print("\n[X5] production safe defaults 未變")
    import pcs_auto_control_runtime as RT
    import pcs_auto_control_config as C
    check("★★ X5. HO/RT DISPATCH_ENABLED = False",
          HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False)
    check("★★ X5. senders / transport 未武裝",
          RA.RemoteSenders().armed is False
          and RA.SshTransport().armed is False)
    check("★★ X5. pause / restore production default = None",
          RA.SshTimeouts().command_timeouts.get("pause") is None
          and RA.SshTimeouts().command_timeouts.get("restore") is None)
    check("★★ X5. 5 kW / 150 kW 未變",
          C.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0
          and C.DEFAULT_CONTROL_CONFIG.max_power_kw == 150.0)
    check("★★ X5. command limits 未變",
          FL.CommandCountGuard.LIMITS == {"phase6_charge": 1,
                                          "phase6_discharge": 0,
                                          "phase6_pcs_stop": 1,
                                          "pause": 1, "restore": 1})
    check("  HANDOFF_ALWAYS_REQUIRED 未變",
          FL.HANDOFF_ALWAYS_REQUIRED is True)


# ======================================================================
def main():
    for fn in (test_S1_wait_then_run_once, test_S2_expired,
               test_S3_gate_a_fail, test_S4_gate_a_pass_handoff_once,
               test_S5_pause_unknown, test_S6_gate_b_fresh,
               test_S7_gate_b_stale, test_S8_gate_b_discharge,
               test_S9_gate_b_tou_left_offpeak, test_S10_authority_fail,
               test_S11_successful_leg_counts,
               test_S12_dispatch_unknown_no_retry, test_S13_restore_unknown,
               test_S14_crash_after_pause, test_S15_crash_after_charge,
               test_S16_crash_before_restore, test_S17_restart_does_not_rerun,
               test_S18_pass_then_consumed, test_S19_abort_then_consumed,
               test_S20_exception_safe_reset,
               test_X1_no_duplicate_logic, test_X2_no_hardcoded_schedule,
               test_X3_positioning_constants,
               test_X4_host_runtime_fail_closed,
               test_X5_production_defaults):
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
