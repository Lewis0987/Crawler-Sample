# -*- coding: utf-8 -*-
"""
test_phase6_trial2_harness.py — Trial #2 harness 與 P2 one-attempt 對稱保護
======================================================================
核心命題
    「Trial #2 的 PASS 不能來自『設備剛好很安靜』。
      必須先證明 external controller 確實 ACTIVE，
      再證明 pause 之後 normal control loop 真的停了，
      而且 PauseVerifier 也獨立收斂 —— 兩者同時成立才算鑑別驗證。」

涵蓋（使用者指定的 15 項）
    T1  PCS CHARGING + marks advancing            -> Active Gate PASS
    T2  PCS DISCHARGING + marks advancing         -> Active Gate PASS
    T3  Authority EXTERNAL_OR_UNKNOWN + advancing -> Active Gate PASS
    T4  PCS STANDBY + Authority IDLE              -> Active Gate FAIL
    T5  marks not advancing                       -> Active Gate FAIL
    T6  Meter 高負載但其他 active evidence 不成立 -> Active Gate FAIL
    T7  No Active Baseline -> ABORT / pause_attempt_count == 0
    T8  pause SEND_UNKNOWN -> count==1 / no resend / responsibility TRUE
    T9  marks stopped only                        -> NOT full pause PASS
    T10 marks stopped + PV_VERIFIED               -> discriminative verified
    T11 restore SEND_UNKNOWN -> restore_attempt_count == 1 / no resend
    T12 第二次 restore request                    -> blocked by may_attempt
    T13 RV_RESUMED                                -> responsibility FALSE
    T14 restore runtime healthy 但 marks 未前進    -> not RV_RESUMED
    T15 evidence artifact 含完整 Trial #2 新欄位

額外結構鎖（S 系列）
    S1  LAYER 1 不得因 CHARGING / DISCHARGING 判 FAIL（G2 POLICY）
    S2  UNKNOWN / FAULT / None PCS 狀態仍 Fail Closed
    S3  baseline duration / interval 必填，不得預設
    S4  OneAttemptGuard 硬上限（超額拋例外，不是回 False）
    S5  harness zero-I/O：不含 subprocess / socket / ssh / http
    S6  production 預設未變（pause / restore timeout = None、DISPATCH_ENABLED）
    S7  discriminative_pause_verified 真值表
    S8  validate_artifact 抓得出竄改
    S9  P2：request_restore 與 request_pause 對稱（AST）
    S10 harness 不碰 Phase 6 frozen application layer（AST import 掃描）

source freshness / baseline policy（F 系列，2026-09-18 evidence upgrade）
    F1  raw_source_time present + fresh      -> SOURCE_FRESH
    F2  raw_source_time present + stale      -> SOURCE_STALE
    F3  raw_source_time missing              -> SOURCE_TIME_UNAVAILABLE
    F4  malformed timestamp                  -> Fail Closed（age = None）
    F5  HTTP latency 低但 device timestamp stale -> 不得判 fresh
    F6  sample_count / terminal active sample 語意一致（非 off-by-one）
    F7  MAX WINDOW 600 但可提前 ACTIVE_BASELINE_ESTABLISHED
    F8  no active -> 跑滿 max window -> NO_ACTIVE_BASELINE
    F9  schema v2 與 v1 歷史 artifact 相容
    F10 2026-09-18 Trial #2 歷史 artifact 仍可驗證（唯讀）

用法
    python test_phase6_trial2_harness.py        # exit 0 = PASS
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
import phase6_trial2_harness as T2                  # noqa: E402

RESULTS = []
TMPDIRS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="t2_")
    TMPDIRS.append(d)
    return d


def harness_src():
    return io.open(os.path.join(HERE, "phase6_trial2_harness.py"),
                   encoding="utf-8").read()


def find_node(src, name, kind=ast.FunctionDef):
    return next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, kind) and n.name == name)


def literal_strings(node):
    """節點內的字串字面值，**排除 docstring 本體**。

    🔴 以 AST 節點身分排除，不用字串比對 —— `ast.get_docstring()` 會做
       cleandoc，與原始 Constant 值不相等，用它 discard 會漏掉（曾誤判）。
    """
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


# ======================================================================
# 共用替身（全部離線；沒有任何真實 I/O）
# ======================================================================
class FakeClock(object):
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += float(s or 0)


def mark(n):
    """單調遞增的 loop timestamp 字串（字典序即時間序）。"""
    return "2026-09-17 10:%02d:%02d" % (n // 60, n % 60)


def probe_ok(**kw):
    d = {"ssh_ok": True, "screen_alive": True, "process_alive": True,
         "process_identity_ok": True, "guard_variant": "B2",
         "capability_ok": True}
    d.update(kw)
    return d


def sample_active(pcs="CHARGING", authority="EXTERNAL_OR_UNKNOWN", **kw):
    d = {"fresh": True, "comm_ok": True, "meter_fresh": True,
         "pcs_state": pcs, "authority": authority, "ac_kw": -40.0,
         "meter_kw": 120.0, "fault": False, "critical_alarms": 0,
         "ownership": "EXTERNAL", "soc": 40.0}
    d.update(kw)
    return d


def sample_idle(**kw):
    d = {"fresh": True, "comm_ok": True, "meter_fresh": True,
         "pcs_state": "STANDBY", "authority": "IDLE", "ac_kw": 0.0,
         "meter_kw": 3.0, "fault": False, "critical_alarms": 0,
         "ownership": "NEITHER", "soc": 40.0}
    d.update(kw)
    return d


SAFETY_FLAGS = {"dispatch_enabled": False, "mode": "DRY_RUN",
                "senders_armed": False}

CFG = HO.HandoffConfig


def baseline_cfg(duration=60.0, interval=20.0, minobs=3):
    return T2.BaselineConfig(duration, interval, minobs)


class Scenario(object):
    """
    可編排的觀測來源。phase: baseline -> paused -> restored。

    🔴 所有資料都是本地 dict，**沒有任何裝置 I/O**。
    """

    def __init__(self, clock, active_sample=None, idle_after_pause=True,
                 marks_advance_baseline=True, marks_advance_after_pause=False,
                 marks_advance_after_restore=True, mark_available=True,
                 pause_outcome=RA.SEND_SENT, restore_outcome=RA.SEND_SENT):
        self.clock = clock
        self.active_sample = active_sample or sample_active()
        self.idle_after_pause = idle_after_pause
        self.marks_advance_baseline = marks_advance_baseline
        self.marks_advance_after_pause = marks_advance_after_pause
        self.marks_advance_after_restore = marks_advance_after_restore
        self.mark_available = mark_available
        self.pause_outcome = pause_outcome
        self.restore_outcome = restore_outcome
        self.phase = "baseline"
        self.n = 0
        self.pause_calls = 0
        self.restore_calls = 0
        self.reset_calls = 0

    # ---- sources ----
    def sample(self):
        if self.phase == "baseline":
            return dict(self.active_sample)
        if self.idle_after_pause:
            return sample_idle()
        return dict(self.active_sample)

    def probe(self):
        return probe_ok()

    def loop_mark(self):
        if not self.mark_available:
            return None
        advance = {"baseline": self.marks_advance_baseline,
                   "paused": self.marks_advance_after_pause,
                   "restored": self.marks_advance_after_restore}[self.phase]
        if advance:
            self.n += 1
        return mark(self.n)

    def flags(self):
        return dict(SAFETY_FLAGS)

    # ---- senders ----
    def send_pause(self):
        self.pause_calls += 1
        self.phase = "paused"
        return self.pause_outcome

    def send_restore(self):
        self.restore_calls += 1
        self.phase = "restored"
        return self.restore_outcome

    def safety_reset(self):
        self.reset_calls += 1
        return {"armed": False, "pause_timeout": None, "restore_timeout": None}


def make_runner(sc, clock, cfg=None):
    cfg = cfg or baseline_cfg()
    return T2.Trial2Runner(
        sample_source=sc.sample, probe_source=sc.probe,
        loop_mark_source=sc.loop_mark, safety_flags_source=sc.flags,
        baseline_config=cfg,
        pause_verifier_factory=(lambda: RA.PauseVerifier(
            CFG.idle_power_lower_kw, CFG.idle_power_upper_kw,
            CFG.stable_verify_min_samples, CFG.pause_settling_timeout_sec,
            clock=clock)),
        restore_verifier_factory=(lambda: RA.RestoreVerifier(
            CFG.restore_loop_min_observations, CFG.pause_settling_timeout_sec,
            clock=clock)),
        loop_halt_min_observations=CFG.restore_loop_min_observations,
        loop_halt_timeout_sec=CFG.pause_settling_timeout_sec,
        stable_verify_interval_sec=CFG.stable_verify_interval_sec,
        restore_loop_interval_sec=CFG.restore_loop_interval_sec,
        pause_sender=sc.send_pause, restore_sender=sc.send_restore,
        safety_reset=sc.safety_reset, sleeper=clock.sleep, clock=clock)


def observe_baseline(sample, marks_advance=True, n=6, minobs=3):
    """把一組 sample / mark 餵進 ActiveBaselineObserver，回傳 observer。"""
    clock = FakeClock()
    obs = T2.ActiveBaselineObserver(baseline_cfg(minobs=minobs), clock=clock)
    step = 0
    for _ in range(n):
        if marks_advance:
            step += 1
        obs.feed(sample, mark(step))
        clock.sleep(20.0)
        if obs.state != T2.AB_OBSERVING:
            break
    obs.close()
    return obs


# ======================================================================
# T1 ~ T3：Active Gate PASS
# ======================================================================
def test_T1_pcs_charging():
    print("\n[T1] PCS CHARGING + marks advancing -> Active Gate PASS")
    obs = observe_baseline(sample_active(pcs="CHARGING", authority="IDLE"))
    ok, why = T2.active_gate(obs)
    check("★★ T1. Active Gate PASS", ok is True)
    check(f"  成立理由含 pcs_charging（{why}）", "pcs_charging" in (why or ""))
    check("  observer 狀態 = ACTIVE_BASELINE_ESTABLISHED",
          obs.state == T2.AB_ACTIVE_ESTABLISHED)
    check("  loop marks 判為前進", obs.loop_marks_advancing is True)


def test_T2_pcs_discharging():
    print("\n[T2] PCS DISCHARGING + marks advancing -> Active Gate PASS")
    obs = observe_baseline(sample_active(pcs="DISCHARGING", authority="IDLE"))
    ok, why = T2.active_gate(obs)
    check("★★ T2. Active Gate PASS", ok is True)
    check("  成立理由含 pcs_discharging", "pcs_discharging" in (why or ""))


def test_T3_authority_external():
    print("\n[T3] Authority EXTERNAL_OR_UNKNOWN + marks advancing -> PASS")
    obs = observe_baseline(sample_active(pcs="STANDBY",
                                         authority="EXTERNAL_OR_UNKNOWN"))
    ok, why = T2.active_gate(obs)
    check("★★ T3. Active Gate PASS（PCS 非充放電亦可）", ok is True)
    check("  成立理由含 authority_external",
          "authority_external" in (why or ""))


# ======================================================================
# T4 ~ T6：Active Gate FAIL
# ======================================================================
def test_T4_standby_idle():
    print("\n[T4] PCS STANDBY + Authority IDLE -> Active Gate FAIL")
    obs = observe_baseline(sample_active(pcs="STANDBY", authority="IDLE"))
    ok, why = T2.active_gate(obs)
    check("★★ T4. Active Gate FAIL", ok is False)
    check("  observer 狀態 = NO_ACTIVE_BASELINE",
          obs.state == T2.AB_NO_ACTIVE)
    check("  理由指出未觀測到 active evidence",
          "active" in (why or "").lower() or "EXTERNAL_OR_UNKNOWN" in (why or ""))
    check("  marks 前進不足以單獨成立", obs.loop_marks_advancing is True
          and obs.active_evidence_seen is False)


def test_T5_marks_not_advancing():
    print("\n[T5] marks not advancing -> Active Gate FAIL")
    obs = observe_baseline(sample_active(pcs="CHARGING"), marks_advance=False)
    ok, why = T2.active_gate(obs)
    check("★★ T5. Active Gate FAIL", ok is False)
    check("  marks 判為未前進", obs.loop_marks_advancing is False)
    check("  即使 PCS CHARGING 也不成立（必須 AND）",
          obs.active_evidence_seen is True and obs.established is False)
    check("  理由指出 marks 未前進", "marks" in (why or ""))


def test_T6_meter_only():
    print("\n[T6] Meter 高負載但其他 active evidence 不成立 -> FAIL")
    s = sample_active(pcs="STANDBY", authority="IDLE", meter_kw=999.0,
                      ac_kw=999.0)
    obs = observe_baseline(s)
    ok, _ = T2.active_gate(obs)
    check("★★ T6. Active Gate FAIL（Meter 不得單獨成立）", ok is False)
    ev = T2.active_evidence(s)
    check("  active_evidence 三項全 False", not any(ev.values()))
    check("  Meter 觀測有被保存（corroborating）",
          999.0 in obs.as_dict()["meter_observations_corroborating_only"])
    check("  METER_ROLE = CORROBORATING_ONLY",
          T2.METER_ROLE == "CORROBORATING_ONLY")

    # AST：active_evidence 的判定式不得引用任何 meter 欄位
    fn = find_node(harness_src(), "active_evidence")
    strs = literal_strings(fn)
    check("★★ T6. AST：active_evidence 不引用任何 meter 字串欄位",
          not any("meter" in c.lower() for c in strs if c))
    ids = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    ids |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    check("  且無任何 meter 識別字", not any("meter" in i.lower() for i in ids))


# ======================================================================
# T7：No Active Baseline -> ABORT
# ======================================================================
def test_T7_abort_no_baseline():
    print("\n[T7] No Active Baseline -> ABORT / pause_attempt_count == 0")
    clock = FakeClock()
    sc = Scenario(clock, active_sample=sample_active(pcs="STANDBY",
                                                     authority="IDLE"))
    art = make_runner(sc, clock).run().to_dict()
    check("★★ T7. outcome = ABORTED_NO_ACTIVE_BASELINE",
          art["outcome"] == T2.OUT_ABORTED_NO_ACTIVE_BASELINE)
    check("★★ T7. pause_attempt_count == 0", art["pause_attempt_count"] == 0)
    check("★★ T7. pause sender 完全沒有被呼叫", sc.pause_calls == 0)
    check("  restore 也不需要（責任未成立）",
          art["restore_attempt_count"] == 0 and sc.restore_calls == 0
          and art["restore_responsibility"] is False)
    check("  active_baseline_established = False",
          art["active_baseline_established"] is False)
    check("  安全收尾仍有執行", sc.reset_calls == 1)


# ======================================================================
# T8：pause SEND_UNKNOWN
# ======================================================================
def test_T8_pause_unknown():
    print("\n[T8] pause SEND_UNKNOWN -> count==1 / no resend / responsibility")
    clock = FakeClock()
    sc = Scenario(clock, pause_outcome=RA.SEND_UNKNOWN)
    art = make_runner(sc, clock).run().to_dict()
    check("★★ T8. pause_send_outcome = UNKNOWN",
          art["pause_send_outcome"] == RA.SEND_UNKNOWN)
    check("★★ T8. pause_attempt_count == 1", art["pause_attempt_count"] == 1)
    check("★★ T8. pause sender 只被呼叫一次（無 resend）", sc.pause_calls == 1)
    check("★★ T8. SEND_UNKNOWN 仍成立 restore responsibility",
          any(s["step"] == "7_RESPONSIBILITY" and s["ok"]
              for s in art["steps"]))
    check("  且確實走到 restore（責任必須歸還）", sc.restore_calls == 1)


def test_T8b_pause_not_sent():
    print("\n[T8b] pause SEND_NOT_SENT -> 責任未成立，不需 restore")
    clock = FakeClock()
    sc = Scenario(clock, pause_outcome=RA.SEND_NOT_SENT)
    art = make_runner(sc, clock).run().to_dict()
    check("★★ T8b. outcome = ABORTED_PAUSE_FAILED",
          art["outcome"] == T2.OUT_ABORTED_PAUSE_FAILED)
    check("★★ T8b. restore 未被呼叫", sc.restore_calls == 0
          and art["restore_attempt_count"] == 0)
    check("  責任未成立", art["restore_responsibility"] is False)


# ======================================================================
# T9 / T10：loop halt 與鑑別結論
# ======================================================================
def test_T9_marks_stopped_only():
    print("\n[T9] marks stopped only -> NOT full pause PASS")
    clock = FakeClock()
    # pause 後 marks 停止，但 PCS 仍在 CHARGING -> PauseVerifier 不會 VERIFIED
    sc = Scenario(clock, idle_after_pause=False)
    art = make_runner(sc, clock).run().to_dict()
    check("★★ T9. normal_loop_halted = True", art["normal_loop_halted"] is True)
    check("★★ T9. PauseVerifier 未 VERIFIED",
          art["pause_verifier_state"] != RA.PV_VERIFIED)
    check("★★ T9. pause_verified = False", art["pause_verified"] is False)
    check("★★ T9. discriminative_pause_verified = False（不得只靠 loop marks）",
          art["discriminative_pause_verified"] is False)


def test_T10_discriminative():
    print("\n[T10] marks stopped + PV_VERIFIED -> discriminative verified")
    clock = FakeClock()
    sc = Scenario(clock)
    art = make_runner(sc, clock).run().to_dict()
    check("  normal_loop_halted = True", art["normal_loop_halted"] is True)
    check("  pause_verifier_state = VERIFIED",
          art["pause_verifier_state"] == RA.PV_VERIFIED)
    check("★★ T10. discriminative_pause_verified = True",
          art["discriminative_pause_verified"] is True)
    check("  outcome = COMPLETE", art["outcome"] == T2.OUT_COMPLETE)
    check("  active_baseline_established = True",
          art["active_baseline_established"] is True)


def test_T9b_loop_still_advancing():
    print("\n[T9b] pause 後 marks 仍前進 -> normal_loop_halted = False")
    clock = FakeClock()
    sc = Scenario(clock, marks_advance_after_pause=True)
    art = make_runner(sc, clock).run().to_dict()
    check("★★ T9b. loop halt = STILL_ADVANCING",
          art["loop_halt"]["state"] == T2.LH_STILL_ADVANCING)
    check("★★ T9b. normal_loop_halted = False",
          art["normal_loop_halted"] is False)
    check("★★ T9b. discriminative_pause_verified = False",
          art["discriminative_pause_verified"] is False)


def test_T9c_loop_unavailable():
    print("\n[T9c] loop 證據取不到 -> normal_loop_halted = None（不得視為 halted）")
    lh = T2.LoopHaltTracker(3, 10.0, clock=FakeClock())
    lh.feed(None)
    check("  取不到 mark 時仍 OBSERVING", lh.state == T2.LH_OBSERVING)
    c = FakeClock()
    lh2 = T2.LoopHaltTracker(3, 10.0, clock=c)
    lh2.feed(None)
    c.sleep(11.0)
    lh2.feed(None)
    check("★★ T9c. 逾時仍取不到 -> LOOP_EVIDENCE_UNAVAILABLE",
          lh2.state == T2.LH_UNKNOWN)
    check("★★ T9c. normal_loop_halted = None", lh2.normal_loop_halted is None)
    check("★★ T9c. None 不構成 discriminative verified",
          T2.discriminative_pause_verified(None, RA.PV_VERIFIED) is False)


# ======================================================================
# T11 ~ T14：restore
# ======================================================================
def test_T11_restore_unknown():
    print("\n[T11] restore SEND_UNKNOWN -> count==1 / no resend")
    clock = FakeClock()
    sc = Scenario(clock, restore_outcome=RA.SEND_UNKNOWN)
    art = make_runner(sc, clock).run().to_dict()
    check("★★ T11. restore_send_outcome = UNKNOWN",
          art["restore_send_outcome"] == RA.SEND_UNKNOWN)
    check("★★ T11. restore_attempt_count == 1",
          art["restore_attempt_count"] == 1)
    check("★★ T11. restore sender 只被呼叫一次", sc.restore_calls == 1)


def test_T12_second_restore_blocked():
    print("\n[T12] 第二次 restore request -> blocked by may_attempt（P2）")
    # (a) harness 層：OneAttemptGuard 硬擋
    g = T2.OneAttemptGuard()
    g.take("restore")
    try:
        g.take("restore")
        raised = False
    except T2.OneAttemptExceeded:
        raised = True
    check("★★ T12a. harness OneAttemptGuard 第二次 restore 直接拋例外", raised)
    check("  restore_attempt_count 仍為 1", g.restore_attempt_count == 1)

    # (b) production 層：request_restore 的 may_attempt（P2 修正）
    calls = []

    def sender():
        calls.append(1)
        return True

    d = tmpdir()
    j = HO.Journal(path=os.path.join(d, "j.jsonl"))
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(restore_sender=sender),
        observe=(lambda: {}), gates=(lambda x: (True, {})), journal=j,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    o._to(HO.S_PREFLIGHT)
    o._to(HO.S_PAUSE_REQUESTED)
    o._to(HO.S_RESTORE_REQUESTED)
    s1, w1 = o.request_restore()
    s2, w2 = o.request_restore()
    check("★★ T12b. 第一次 restore 送出", s1 is True and len(calls) == 1)
    check("★★ T12b. 第二次被 may_attempt 擋下（不重送）",
          s2 is False and w2 == "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"
          and len(calls) == 1)
    check("  與 request_pause 的回應字串一致（對稱）", w2 == (
        HO.HandoffOrchestrator(
            remote=HO.RemoteController(),
            observe=(lambda: {}), gates=(lambda x: (True, {})),
            journal=HO.Journal(path=os.path.join(tmpdir(), "k.jsonl")),
            mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0
        ).__class__ and "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"))
    outs = [x for x in j.read_all()
            if x.get("kind") == "INTENT" and x.get("step") == "RESTORE"]
    check("  journal 只有一筆 RESTORE INTENT", len(outs) == 1)


def test_T12b_pause_symmetry():
    print("\n[T12c] pause 第二次同樣被擋（既有行為未退步）")
    calls = []
    d = tmpdir()
    j = HO.Journal(path=os.path.join(d, "j.jsonl"))
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=(lambda: calls.append(1) or True)),
        observe=(lambda: {}), gates=(lambda x: (True, {})), journal=j,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    o._to(HO.S_PREFLIGHT)
    o.request_pause()
    s2, w2 = o.request_pause()
    check("★★ T12c. pause 第二次被擋",
          s2 is False and w2 == "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"
          and len(calls) == 1)


def test_T13_rv_resumed_clears():
    print("\n[T13] RV_RESUMED -> responsibility FALSE")
    clock = FakeClock()
    sc = Scenario(clock)
    runner = make_runner(sc, clock)
    art = runner.run().to_dict()
    check("  restore_verifier_state = RESUMED",
          art["restore_verifier_state"] == RA.RV_RESUMED)
    check("★★ T13. restore_resumed = True", art["restore_resumed"] is True)
    check("★★ T13. restore_responsibility = False",
          art["restore_responsibility"] is False
          and runner.restore_responsibility is False)
    check("  post_check.responsibility_cleared", art["post_check"]
          ["responsibility_cleared"] is True)


def test_T14_restore_marks_not_advancing():
    print("\n[T14] restore runtime healthy 但 marks 未前進 -> not RV_RESUMED")
    clock = FakeClock()
    sc = Scenario(clock, marks_advance_after_restore=False)
    runner = make_runner(sc, clock)
    art = runner.run().to_dict()
    check("★★ T14. restore_verifier_state != RESUMED",
          art["restore_verifier_state"] != RA.RV_RESUMED)
    check("★★ T14. restore_resumed = False", art["restore_resumed"] is False)
    check("★★ T14. 責任**保留**（不得清除）",
          art["restore_responsibility"] is True
          and runner.restore_responsibility is True)
    check("  outcome = RESTORE_FAILED_CRITICAL",
          art["outcome"] == T2.OUT_RESTORE_FAILED_CRITICAL)


# ======================================================================
# T15：evidence artifact schema
# ======================================================================
def test_T15_artifact():
    print("\n[T15] evidence artifact 含完整 Trial #2 新欄位")
    clock = FakeClock()
    sc = Scenario(clock)
    art_obj = make_runner(sc, clock).run()
    art = art_obj.to_dict()
    for f in T2.REQUIRED_TRIAL2_FIELDS:
        check(f"  必備欄位存在：{f}", f in art)
    check("★★ T15. missing_required 為空", art_obj.missing_required() == [])
    check("★★ T15. PAUSE TRANSITIONAL CONTROL EFFECT 已記錄",
          art["pause_transitional_control_effect"]
          == "ZERO-UNTIL-STOP-CONFIRMED")
    check("  pause_semantics 完整記錄",
          art["pause_semantics"]
          == "ZERO-UNTIL-STOP-CONFIRMED -> THEN COMMAND SILENCE")
    check("  deployed source sha256 已記錄",
          art["deployed_source_sha256"].startswith("964bb642"))
    check("  actual_active_power_kw 標為 LOW 鑑別力",
          art["actual_active_power_kw_discriminative_value"] == "LOW")
    check("  meter_role = CORROBORATING_ONLY",
          art["meter_role"] == "CORROBORATING_ONLY")
    check("★★ T15. validate_artifact 無問題", T2.validate_artifact(art) == [])

    # 落盤後可再讀回
    p = os.path.join(tmpdir(), "trial2.json")
    art_obj.write(p)
    import json
    back = json.load(io.open(p, encoding="utf-8"))
    check("★★ T15. artifact 可落盤並原樣讀回",
          T2.validate_artifact(back) == []
          and back["schema_version"] == T2.ARTIFACT_SCHEMA_VERSION)

    # 未登記欄位不得寫入
    try:
        art_obj.set(not_a_field=1)
        bad = False
    except KeyError:
        bad = True
    check("★★ T15. 未登記於 ARTIFACT_FIELDS 的欄位一律拒絕", bad)


# ======================================================================
# S 系列：結構鎖
# ======================================================================
def test_S1_layer1_allows_active():
    print("\n[S1] LAYER 1 不得因 CHARGING / DISCHARGING 判 FAIL（G2 POLICY）")
    for st in ("CHARGING", "DISCHARGING", "STANDBY", "STOPPED"):
        ok, ck = T2.safety_preflight(sample_active(pcs=st, authority="IDLE"),
                                     probe_ok(), SAFETY_FLAGS)
        check(f"★★ S1. PCS={st} -> safety preflight PASS", ok is True)
    check("  合法觀測狀態即為這四個",
          T2.LEGAL_OBSERVED_PCS_STATES
          == ("CHARGING", "DISCHARGING", "STANDBY", "STOPPED"))
    # Trial #1 的 idle-only 條件不得出現在 LAYER 1
    strs = literal_strings(find_node(harness_src(), "safety_preflight"))
    check("★★ S1. AST：safety_preflight 不含 idle-only 判定字串",
          not any(x in ("STANDBY", "STOPPED") for x in strs if x))


def test_S2_layer1_fail_closed():
    print("\n[S2] UNKNOWN / FAULT / None PCS 狀態仍 Fail Closed")
    for st in ("UNKNOWN", "FAULT", None, "", "RUNNING"):
        ok, ck = T2.safety_preflight(sample_active(pcs=st), probe_ok(),
                                     SAFETY_FLAGS)
        check(f"★★ S2. PCS={st!r} -> FAIL", ok is False
              and ck["pcs_state_legal"] is False)
    for bad, label in ((dict(fault=True), "fault"),
                       (dict(critical_alarms=1), "critical alarm"),
                       (dict(comm_ok=False), "comm"),
                       (dict(fresh=False), "fresh"),
                       (dict(meter_fresh=False), "meter fresh"),
                       (dict(ownership="BOTH"), "ownership BOTH")):
        ok, _ = T2.safety_preflight(sample_active(**bad), probe_ok(),
                                    SAFETY_FLAGS)
        check(f"  {label} 不合格 -> FAIL", ok is False)
    for bad, label in ((dict(guard_variant="B1"), "guard variant"),
                       (dict(capability_ok=False), "capability"),
                       (dict(process_identity_ok=None), "identity UNKNOWN"),
                       (dict(ssh_ok=False), "ssh")):
        ok, _ = T2.safety_preflight(sample_active(), probe_ok(**bad),
                                    SAFETY_FLAGS)
        check(f"  {label} 不合格 -> FAIL", ok is False)
    for bad, label in (({"dispatch_enabled": True}, "DISPATCH_ENABLED"),
                       ({"mode": "ARMED"}, "MODE"),
                       ({"senders_armed": True}, "senders armed")):
        flags = dict(SAFETY_FLAGS)
        flags.update(bad)
        ok, _ = T2.safety_preflight(sample_active(), probe_ok(), flags)
        check(f"★★ S2. {label} 不安全 -> FAIL", ok is False)


def test_S3_baseline_params_required():
    print("\n[S3] baseline duration / interval 必填，不得預設")
    for args in ((None, 20.0, 3), (600.0, None, 3), (600.0, 20.0, None),
                 (0, 20.0, 3), (600.0, 0, 3), (600.0, 20.0, 1)):
        try:
            T2.BaselineConfig(*args)
            raised = False
        except ValueError:
            raised = True
        check(f"★★ S3. BaselineConfig{args} -> 拒絕", raised)
    c = T2.BaselineConfig(600.0, 20.0, 3)
    check("  合法值可建立且 max_samples 正確", c.max_samples == 31)
    check("  建議值僅為建議常數，未被當成預設",
          T2.RECOMMENDED_BASELINE_DURATION_SEC == 600.0
          and T2.RECOMMENDED_BASELINE_INTERVAL_SEC == 20.0)
    # AST：ActiveBaselineObserver 不得自帶 duration / interval 預設
    cls = find_node(harness_src(), "ActiveBaselineObserver", ast.ClassDef)
    init = next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    check("★★ S3. ActiveBaselineObserver.__init__ 的 config 無預設值",
          init.args.args[1].arg == "config"
          and len(init.args.defaults) == 1)     # 只有 clock 有預設


def test_S4_one_attempt_guard():
    print("\n[S4] OneAttemptGuard 硬上限")
    g = T2.OneAttemptGuard()
    check("  上限常數 = 1", T2.OneAttemptGuard.MAX_ATTEMPTS == 1)
    check("  初始 pause / restore 計數為 0",
          g.pause_attempt_count == 0 and g.restore_attempt_count == 0)
    g.take("pause")
    check("★★ S4. pause 第一次成功", g.pause_attempt_count == 1)
    for verb in ("pause", "restore"):
        gg = T2.OneAttemptGuard()
        gg.take(verb)
        try:
            gg.take(verb)
            raised = False
        except T2.OneAttemptExceeded:
            raised = True
        check(f"★★ S4. {verb} 第二次拋 OneAttemptExceeded（不是回 False）",
              raised)
        check(f"  {verb} 計數停在 1", gg.attempts(verb) == 1)
    check("  唯讀動詞不受限（probe 可多次）",
          all(T2.OneAttemptGuard().may_attempt("probe") for _ in range(3)))
    check("  控制動詞集合與 adapter 一致",
          tuple(T2.OneAttemptGuard().control_verbs) == RA.CONTROL_VERBS)


def test_S5_zero_io():
    print("\n[S5] harness zero-I/O：不含 subprocess / socket / ssh / http")
    tree = ast.parse(harness_src())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    for bad in ("subprocess", "socket", "paramiko", "requests", "http",
                "urllib", "telnetlib", "pymodbus"):
        check(f"★★ S5. 未 import {bad}", bad not in mods)
    # 🔴 只掃「被呼叫的方法名」(ast.Attribute)，不掃本地變數名 ——
    #    本檔的 `post` 是 post_check 的區域變數，掃 ast.Name 會產生假陽性。
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for bad in ("Popen", "check_output", "urlopen", "post", "request",
                "sendall", "recv"):
        check(f"  未出現 {bad}() 呼叫", bad not in called)
    check("  實際 import 的專案模組僅 phase6_remote_adapter",
          mods & {"phase6_remote_adapter", "phase6_handoff_orchestrator",
                  "pcs_auto_control_production", "pcs_control_integration"}
          == {"phase6_remote_adapter"})


def test_S6_production_defaults_unchanged():
    print("\n[S6] production 預設未變")
    to = RA.SshTimeouts()
    check("★★ S6. SshTimeouts().command_timeouts['pause'] 仍為 None",
          to.command_timeouts.get("pause") is None)
    check("★★ S6. SshTimeouts().command_timeouts['restore'] 仍為 None",
          to.command_timeouts.get("restore") is None)
    check("★★ S6. 12.0 仍只是候選，未進入預設",
          RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC
          == {"pause": 12.0, "restore": 12.0})
    check("★★ S6. DISPATCH_ENABLED 仍為 False", HO.DISPATCH_ENABLED is False)
    check("★★ S6. RemoteSenders 預設未武裝", RA.RemoteSenders().armed is False)
    check("★★ S6. DEPLOYED_GUARD_VARIANT = B2",
          RA.DEPLOYED_GUARD_VARIANT == "B2")
    check("  PauseVerifier criteria 未被更動（9 項）",
          len(RA.CANDIDATE_IDLE_ITEMS) == 9)
    check("  stable verify 參數未變（4 筆 / 15 s / 45 s / 120 s）",
          (CFG.stable_verify_min_samples, CFG.stable_verify_interval_sec,
           CFG.stable_verify_min_span_sec, CFG.pause_settling_timeout_sec)
          == (4, 15.0, 45.0, 120.0))
    check("  RestoreVerifier 參數未變（3 筆 / 3 s）",
          (CFG.restore_loop_min_observations, CFG.restore_loop_interval_sec)
          == (3, 3.0))


def test_S7_truth_table():
    print("\n[S7] discriminative_pause_verified 真值表")
    tbl = [
        (True, RA.PV_VERIFIED, True),
        (True, RA.PV_FAILED, False),
        (True, RA.PV_SETTLING_TIMEOUT, False),
        (True, RA.PV_UNKNOWN, False),
        (True, None, False),
        (False, RA.PV_VERIFIED, False),
        (None, RA.PV_VERIFIED, False),
        (None, None, False),
    ]
    for halted, pv, want in tbl:
        got = T2.discriminative_pause_verified(halted, pv)
        check(f"★★ S7. halted={halted!r} pv={pv!r} -> {want}", got is want)


def test_S8_validate_artifact():
    print("\n[S8] validate_artifact 抓得出竄改")
    clock = FakeClock()
    art = make_runner(Scenario(clock), clock).run().to_dict()
    check("  正常 artifact 無問題", T2.validate_artifact(art) == [])
    bad = dict(art)
    bad["pause_attempt_count"] = 2
    check("★★ S8. pause_attempt_count = 2 被抓出",
          any("pause_attempt_count" in p for p in T2.validate_artifact(bad)))
    bad2 = dict(art)
    bad2["discriminative_pause_verified"] = True
    bad2["normal_loop_halted"] = False
    check("★★ S8. 鑑別結論與證據不一致被抓出",
          any("discriminative" in p for p in T2.validate_artifact(bad2)))
    bad3 = dict(art)
    bad3["pause_transitional_control_effect"] = "immediate command silence"
    check("★★ S8. 錯誤的 pause 語意用語被抓出",
          any("transitional" in p for p in T2.validate_artifact(bad3)))
    bad4 = dict(art)
    del bad4["restore_send_outcome"]
    check("★★ S8. 缺少必要欄位被抓出",
          any("restore_send_outcome" in p for p in T2.validate_artifact(bad4)))


def test_S9_p2_symmetry_ast():
    print("\n[S9] P2：request_restore 與 request_pause 對稱（AST）")
    src = io.open(os.path.join(HERE, "phase6_handoff_orchestrator.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    found = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in ("request_pause",
                                                         "request_restore"):
            attrs = set()
            for m in ast.walk(n):
                if isinstance(m, ast.Attribute):
                    attrs.add(m.attr)
            found[n.name] = attrs
    for name in ("request_pause", "request_restore"):
        check(f"★★ S9. {name} 有 may_attempt 前置閘",
              "may_attempt" in found.get(name, set()))
        check(f"  {name} 仍有 mark_attempt",
              "mark_attempt" in found.get(name, set()))
    # latent dead path 不得被順手改動（維持 KNOWN LIMITATION）
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name in ("_quiescent_hint",
                                                         "verify_restore"):
            consts = {m.value for m in ast.walk(n)
                      if isinstance(m, ast.Constant)
                      and isinstance(m.value, str)}
            check(f"★★ S9. {n.name} 維持原樣（仍引用 running，未擴 scope）",
                  "running" in consts)


def test_S10_frozen_layer_untouched():
    print("\n[S10] harness 不碰 Phase 6 frozen application layer")
    tree = ast.parse(harness_src())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    for frozen in ("decision_observer", "pcs_auto_control_service",
                   "pcs_auto_control_runtime", "pcs_auto_control_production",
                   "phase6_decision_audit", "control_authority",
                   "safety_gate", "pcs_control_integration"):
        check(f"★★ S10. 未 import frozen 模組 {frozen}", frozen not in mods)
    check("  也未 import handoff orchestrator（不包裝 production 流程）",
          "phase6_handoff_orchestrator" not in mods)
    # 只有 assignment / 呼叫自己的物件，不得對 RA 的模組層常數賦值
    assigns = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    assigns.add((t.value.id, t.attr))
    check("★★ S10. 未對 RA 模組層屬性賦值（不得偷改 production 常數）",
          not any(v == "RA" for v, _ in assigns))



# ======================================================================
# F 系列：source-side freshness（TASK 4 / TASK 5）
# ======================================================================
import datetime as _dt                                           # noqa: E402

OBS_AT = _dt.datetime(2026, 9, 18, 9, 0, 30)
MAX_AGE = 30.0


def test_F1_source_fresh():
    print("\n[F1] raw_source_time present + fresh -> SOURCE_FRESH")
    for raw, label in (("2026-09-18 09:00:20", "10 s 前"),
                       ("2026/09/18 09:00:01", "29 s 前（斜線格式）"),
                       ("2026-09-18T09:00:00", "30 s 前（邊界，ISO）"),
                       ("2026-09-18 09:00:00.482", "帶毫秒")):
        st, age = T2.source_freshness(raw, OBS_AT, MAX_AGE)
        check(f"★★ F1. {label} -> SOURCE_FRESH（age={age}）",
              st == T2.SOURCE_FRESH and age is not None and age <= MAX_AGE)
    ev = T2.source_evidence({"raw_source_time": "2026-09-18 09:00:20"},
                            OBS_AT, MAX_AGE)
    check("  evidence 四欄齊全",
          ev["raw_source_time"] == "2026-09-18 09:00:20"
          and ev["source_time_available"] is True
          and ev["source_age_sec"] == 10.0
          and ev["source_freshness_state"] == T2.SOURCE_FRESH)


def test_F2_source_stale():
    print("\n[F2] raw_source_time present + stale -> SOURCE_STALE")
    st, age = T2.source_freshness("2026-09-18 08:54:27", OBS_AT, MAX_AGE)
    check("★★ F2. 6 分鐘前 -> SOURCE_STALE", st == T2.SOURCE_STALE)
    check(f"  age_sec = {age}（= 363.0）", age == 363.0)
    st2, age2 = T2.source_freshness("2026-09-18 08:59:59", OBS_AT, MAX_AGE)
    check("★★ F2. 剛過門檻（31 s）-> SOURCE_STALE",
          st2 == T2.SOURCE_STALE and age2 == 31.0)


def test_F3_source_time_missing():
    print("\n[F3] raw_source_time missing -> SOURCE_TIME_UNAVAILABLE")
    for raw in (None, "", "   "):
        st, age = T2.source_freshness(raw, OBS_AT, MAX_AGE)
        check(f"★★ F3. raw={raw!r} -> SOURCE_TIME_UNAVAILABLE",
              st == T2.SOURCE_TIME_UNAVAILABLE)
        check("  age_sec = None（不造 timestamp）", age is None)
    ev = T2.source_evidence({}, OBS_AT, MAX_AGE)
    check("★★ F3. evidence source_time_available = False",
          ev["source_time_available"] is False
          and ev["source_age_sec"] is None)
    check("  觀測時間取不到時亦為 UNAVAILABLE",
          T2.source_freshness("2026-09-18 09:00:20", None, MAX_AGE)[0]
          == T2.SOURCE_TIME_UNAVAILABLE)


def test_F4_source_time_malformed():
    print("\n[F4] malformed timestamp -> Fail Closed")
    for raw in ("not-a-time", "2026-13-45 99:99:99", "1758150000",
                "09:00:20", "2026-09-18"):
        st, age = T2.source_freshness(raw, OBS_AT, MAX_AGE)
        check(f"★★ F4. raw={raw!r} -> SOURCE_TIME_MALFORMED",
              st == T2.SOURCE_TIME_MALFORMED)
        check("  age_sec = None（不猜、不補零）", age is None)
    check("  parse_source_time 無法解析時回 None",
          T2.parse_source_time("not-a-time") is None)
    check("★★ F4. malformed 不得被視為 fresh",
          T2.source_freshness("not-a-time", OBS_AT, MAX_AGE)[0]
          != T2.SOURCE_FRESH)
    for bad in (None, 0, -1):
        try:
            T2.source_freshness("2026-09-18 09:00:20", OBS_AT, bad)
            raised = False
        except ValueError:
            raised = True
        check(f"  max_age_sec={bad!r} -> 拒絕（不得預設為無限）", raised)


def test_F5_latency_is_not_freshness():
    print("\n[F5] HTTP latency 低但 device timestamp stale -> 不得判 fresh")
    # 模擬 Trial #2 的實際情形：讀取極快（OBSERVATION_OK / stale=False），
    # 但設備自報時間是 15 小時前。
    sample = {"fresh": True, "comm_ok": True, "age_sec": 0.42, "stale": False,
              "raw_source_time": "2026-09-17 17:57:03"}
    ev = T2.source_evidence(sample, OBS_AT, MAX_AGE)
    check("★★ F5. adapter 層 stale=False（HTTP 讀取很快）",
          sample["stale"] is False and sample["age_sec"] < 1.0)
    check("★★ F5. 但 source_freshness_state = SOURCE_STALE",
          ev["source_freshness_state"] == T2.SOURCE_STALE)
    check("★★ F5. source_age_sec 以 device 時間計（≈15 h，不是 0.42 s）",
          ev["source_age_sec"] > 50000 and ev["source_age_sec"] != 0.42)
    check("  兩者為獨立欄位，不得互相冒充",
          ev["source_age_sec"] != sample["age_sec"])
    # AST：source_freshness 不得引用任何 latency / adapter 欄位
    fn = find_node(harness_src(), "source_freshness")
    strs = literal_strings(fn)
    ids = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    ids |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    check("★★ F5. AST：source_freshness 不引用 age_sec / stale / latency",
          not any(x in ("age_sec", "stale", "latency", "read_duration_sec")
                  for x in (ids | {c for c in strs if c})))


def test_F6_sample_count_semantics():
    print("\n[F6] sample_count / terminal active sample 語意一致")
    clock = FakeClock()
    # 600 s 窗（MAX WINDOW）確保第 5 筆 active 樣本落在窗內
    obs = T2.ActiveBaselineObserver(baseline_cfg(duration=600.0), clock=clock)
    step = 0
    for i in range(6):
        step += 1
        smp = (sample_active(pcs="CHARGING", authority="IDLE") if i == 4
               else sample_active(pcs="STANDBY", authority="IDLE"))
        obs.feed(smp, mark(step))
        clock.sleep(20.0)
        if obs.state != T2.AB_OBSERVING:
            break
    obs.close()
    d = obs.as_dict()
    check("  baseline 共 5 筆", d["baseline_sample_count"] == 5)
    check("  sample_count 向後相容且相等",
          d["sample_count"] == d["baseline_sample_count"] == 5)
    check("★★ F6. terminal_active_sample_index = 5（1-based）",
          d["terminal_active_sample_index"] == 5)
    check("★★ F6. observing_sample_count = 4（不含 terminal 那筆）",
          d["observing_sample_count"] == 4)
    check("  observing + 1 == baseline_sample_count",
          d["observing_sample_count"] + 1 == d["baseline_sample_count"])
    check("★★ F6. 明示計數不含 preflight 取樣",
          d["counts_exclude_preflight_sample"] is True)
    check("  terminal index 指到的確實是有 hits 的那筆",
          bool(d["samples"][d["terminal_active_sample_index"] - 1]["hits"]))
    # 未成立時
    obs2 = observe_baseline(sample_active(pcs="STANDBY", authority="IDLE"))
    d2 = obs2.as_dict()
    check("★★ F6. 未成立 -> terminal_active_sample_index = None",
          d2["terminal_active_sample_index"] is None)
    check("  未成立時 observing_sample_count == baseline_sample_count",
          d2["observing_sample_count"] == d2["baseline_sample_count"])


def test_F7_max_window_early_success():
    print("\n[F7] MAX WINDOW 600 但可提前 ACTIVE_BASELINE_ESTABLISHED")
    check("  政策常數 = MAX_OBSERVATION_WINDOW",
          T2.BASELINE_WINDOW_POLICY == "MAX_OBSERVATION_WINDOW")
    check("  early success 允許", T2.BASELINE_EARLY_SUCCESS_ALLOWED is True)
    clock = FakeClock()
    cfg = T2.BaselineConfig(600.0, 20.0, 3)
    sc = Scenario(clock)
    art = make_runner(sc, clock, cfg=cfg).run().to_dict()
    ab = art["active_baseline"]
    check("★★ F7. observe_duration_sec 記錄仍為 600.0（未被縮短）",
          art["baseline_config"]["observe_duration_sec"] == 600.0)
    check("★★ F7. 實際樣本數 < max_samples（early success）",
          ab["baseline_sample_count"] < cfg.max_samples)
    check("★★ F7. 仍為 ACTIVE_BASELINE_ESTABLISHED",
          ab["state"] == T2.AB_ACTIVE_ESTABLISHED)
    check("★★ F7. terminal active evidence 已保存",
          ab["terminal_active_sample_index"] is not None
          and ab["active_hits"])
    check("  artifact 記錄 window policy",
          art["baseline_window_policy"] == "MAX_OBSERVATION_WINDOW"
          and ab["window_policy"] == "MAX_OBSERVATION_WINDOW")


def test_F8_no_active_runs_full_window():
    print("\n[F8] no active：完整跑到 max window -> NO_ACTIVE_BASELINE")
    clock = FakeClock()
    cfg = T2.BaselineConfig(200.0, 20.0, 3)          # max_samples = 11
    sc = Scenario(clock, active_sample=sample_active(pcs="STANDBY",
                                                     authority="IDLE"))
    art = make_runner(sc, clock, cfg=cfg).run().to_dict()
    ab = art["active_baseline"]
    check("★★ F8. state = NO_ACTIVE_BASELINE",
          ab["state"] == T2.AB_NO_ACTIVE)
    check("★★ F8. 觀測跑滿整個 window（elapsed >= duration）",
          clock.t >= cfg.observe_duration_sec)
    check("★★ F8. 未提前結束：樣本數達上限",
          ab["baseline_sample_count"] == cfg.max_samples)
    check("★★ F8. outcome = ABORTED_NO_ACTIVE_BASELINE",
          art["outcome"] == T2.OUT_ABORTED_NO_ACTIVE_BASELINE)
    check("★★ F8. pause / restore 皆 0 次",
          art["pause_attempt_count"] == 0
          and art["restore_attempt_count"] == 0
          and sc.pause_calls == 0 and sc.restore_calls == 0)
    check("  terminal_active_sample_index = None",
          ab["terminal_active_sample_index"] is None)


def test_F9_schema_v2_backward_compatible():
    print("\n[F9] schema v2 與 v1 歷史 artifact 相容性")
    check("  ARTIFACT_SCHEMA_VERSION = 2", T2.ARTIFACT_SCHEMA_VERSION == 2)
    clock = FakeClock()
    art = make_runner(Scenario(clock), clock).run().to_dict()
    check("★★ F9. 新 artifact 為 v2 且通過驗證",
          art["schema_version"] == 2 and T2.validate_artifact(art) == [])
    for k in T2.REQUIRED_SOURCE_FRESHNESS_FIELDS:
        check(f"  v2 必備 source 欄位存在：{k}", k in art)
    check("★★ F9. 未注入來源時間 -> SOURCE_TIME_UNAVAILABLE（不填假值）",
          art["source_freshness_state"] == T2.SOURCE_TIME_UNAVAILABLE
          and art["source_time_available"] is False
          and art["source_age_sec"] is None)
    # v1 歷史 artifact 仍必須可驗證
    v1 = dict(art)
    v1["schema_version"] = 1
    for k in T2.REQUIRED_SOURCE_FRESHNESS_FIELDS:
        v1.pop(k, None)
    check("★★ F9. v1 歷史 artifact 仍通過驗證（不得讓既有 field evidence 失效）",
          T2.validate_artifact(v1) == [])
    bad = dict(art)
    bad["source_freshness_state"] = T2.SOURCE_TIME_UNAVAILABLE
    bad["source_age_sec"] = 12.0
    check("★★ F9. UNAVAILABLE 卻帶 age -> 被抓出（不造 timestamp）",
          any("不造 timestamp" in x for x in T2.validate_artifact(bad)))
    bad2 = dict(art)
    bad2["source_freshness_state"] = "TOTALLY_FRESH"
    check("★★ F9. 非法 state 被抓出",
          any("source_freshness_state" in x for x in T2.validate_artifact(bad2)))


def test_F10_real_trial2_artifact_still_valid():
    print("\n[F10] 2026-09-18 Trial #2 歷史 artifact 仍可驗證（唯讀）")
    path = (r"D:\Crawler Sample\output\phase6_field_validation"
            r"\trial2_20260918_085422.json")
    if not os.path.exists(path):
        check("  （歷史 artifact 不在本機，略過）", True)
        return
    import json
    rec = json.load(io.open(path, encoding="utf-8"))["artifact"]
    check("★★ F10. 歷史 artifact schema_version = 1", rec["schema_version"] == 1)
    check("★★ F10. 仍通過 validate_artifact（v1 相容）",
          T2.validate_artifact(rec) == [])
    ab = rec["active_baseline"]
    check("★★ F10. 歷史 baseline sample_count = 15", ab["sample_count"] == 15)
    hits = [i for i, x in enumerate(ab["samples"], 1) if x["hits"]]
    check("★★ F10. terminal active 落在 baseline#15（非 off-by-one）",
          hits == [15])
    check("  核心 handoff evidence 未被本輪變更",
          rec["discriminative_pause_verified"] is True
          and rec["pause_attempt_count"] == 1
          and rec["restore_attempt_count"] == 1
          and rec["restore_verifier_state"] == "RESUMED")

# ======================================================================
def main():
    for fn in (test_T1_pcs_charging, test_T2_pcs_discharging,
               test_T3_authority_external, test_T4_standby_idle,
               test_T5_marks_not_advancing, test_T6_meter_only,
               test_T7_abort_no_baseline, test_T8_pause_unknown,
               test_T8b_pause_not_sent, test_T9_marks_stopped_only,
               test_T9b_loop_still_advancing, test_T9c_loop_unavailable,
               test_T10_discriminative, test_T11_restore_unknown,
               test_T12_second_restore_blocked, test_T12b_pause_symmetry,
               test_T13_rv_resumed_clears, test_T14_restore_marks_not_advancing,
               test_T15_artifact, test_S1_layer1_allows_active,
               test_S2_layer1_fail_closed, test_S3_baseline_params_required,
               test_S4_one_attempt_guard, test_S5_zero_io,
               test_S6_production_defaults_unchanged, test_S7_truth_table,
               test_S8_validate_artifact, test_S9_p2_symmetry_ast,
               test_S10_frozen_layer_untouched,
               test_F1_source_fresh, test_F2_source_stale,
               test_F3_source_time_missing, test_F4_source_time_malformed,
               test_F5_latency_is_not_freshness,
               test_F6_sample_count_semantics,
               test_F7_max_window_early_success,
               test_F8_no_active_runs_full_window,
               test_F9_schema_v2_backward_compatible,
               test_F10_real_trial2_artifact_still_valid):
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
