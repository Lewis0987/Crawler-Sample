# -*- coding: utf-8 -*-
"""
test_phase6_entry_point_ordering.py — PRODUCTION ENTRY-POINT SMOKE
======================================================================
核心命題
    「construction smoke 只能證明 **建構 / wiring** 正確；
      **不能**證明 production **control-flow ordering** 正確。
      因此必須有一條從真正 production entry 走完整條鏈的離線測試：

        build_field_dependencies()
          -> production provider
          -> HandoffRunner.run()
          -> Step 1 ... Step 6
          -> Step 7 STAGE A ENTRY GATE
          -> FirstLiveDispatcher.dispatch()
          -> authority acquire
          -> STAGE B FULL PERMIT
          -> first_live_dispatch_scope
          -> live leg
          -> cleanup
          -> Step 10/11 restore
      所有 field 依賴以 fake 注入，**0 real field I/O**。」

🔴 本檔**不得**直接 `disp()`，也**不得**自行 `FirstLiveDispatcher(...)`。
   唯一允許的進入點是 `deps.arm_and_run_handoff()`。
🔴 本檔**不得**使用
      dict((k, True) for k in FIRST_LIVE_PERMIT_PRECONDITIONS)
   這類全 True 合成 evidence 來讓測試通過。
   authority evidence 必須由**真實的** `lease.acquire()` 在正確時點產生。

涵蓋使用者指定項目
    EP1 ~      production entry-point smoke（完整成功路徑）
    A8R1 ~     Attempt #8 exact regression
    O1 ~ O8    negative ordering tests

用法
    python test_phase6_entry_point_ordering.py        # exit 0 = PASS
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcs_auto_control_config as CFG                # noqa: E402
import pcs_auto_control_production as PRD            # noqa: E402
import pcs_auto_control_runtime as RT                # noqa: E402
import pcs_auto_control_service as SVC               # noqa: E402
import pcs_control_executor as EXC                   # noqa: E402
import phase6_first_live_field_provider as FP        # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402

# 🔴 fake 設備 / bundle / ctx 沿用 construction smoke 的**同一套** fixtures，
#    避免兩檔對 production 形狀的認知漂移。
import test_phase6_provider_construction as PCS      # noqa: E402

RESULTS = []
CHARGE_KW = CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


# ======================================================================
# FAKE SSH：loop marks 依「實際送出的動詞」改變行為
# ======================================================================
class EntrySshRunner(object):
    """
    pause 之前 mark 前進、pause 之後凍結、restore 之後再度前進。

    🔴 這是讓 PauseVerifier / RestoreVerifier 走**真實判定邏輯**所需的
       最小行為模型 —— 不是繞過它們。
    """

    def __init__(self, pause_result=0, restore_result=0):
        self.calls = []
        self.paused = False
        self.restored = False
        self._t = 0
        self._frozen = None
        self._pause_result = pause_result
        self._restore_result = restore_result

    def _mark(self):
        if self.paused and not self.restored:
            if self._frozen is None:
                self._frozen = "2026-10-02 00:05:%02d" % (self._t % 60)
            return self._frozen
        self._t += 9
        return "2026-10-02 00:%02d:%02d" % (5 + self._t // 60, self._t % 60)

    def __call__(self, verb, connect_timeout_sec, command_timeout_sec):
        self.calls.append(verb)
        code = 0
        if verb == "probe":
            out = ("pid=99001\n"
                   "cwd=%s\ncmd=%s\n"
                   "screen_count=1\nguard_variant=B2\n"
                   "capability_probe=true\ncapability_loopcheck=true\n"
                   "capability_status=true\n"
                   "capability_pause=true\ncapability_restore=true\n"
                   % (RA.EXPECTED_CWD, RA.EXPECTED_CMD))
        elif verb == "loopcheck":
            m = self._mark()
            out = "mark1=%s\nmark2=%s\n" % (m, m)
        elif verb == "pause":
            code = self._pause_result
            if code == 0:
                self.paused = True
            out = ""
        elif verb == "restore":
            code = self._restore_result
            if code == 0:
                self.restored = True
            out = ""
        else:
            out = ""
        return {"exit_code": code, "stdout": out.strip(),
                "connect_sec": 0.1, "total_sec": 0.2}


# ======================================================================
# 完整 production entry 環境（0 real field I/O）
# ======================================================================
class ResponsiveDevice(object):
    """
    會**回應指令**的設備模型。

    🔴 不能用固定 plan：完整 `HandoffRunner.run()` 會讀取設備數百次
       （pause settling / stable verify / restore verify），
       固定序列會在 pause 驗證期間就漂到 CHARGING。
    🔴 idle 期間功率必須落在 canonical idle band
       `[HandoffConfig.idle_power_lower_kw, idle_power_upper_kw]`
       = [-3.0, 1.0]，否則 `candidate_idle()` 永遠不成立。
    """

    def __init__(self, soc=2.0, idle_power=-0.5):
        self.state = "STANDBY"
        self.soc = soc
        self.power = idle_power
        self.reads = 0
        self.commands = []

    def read(self):
        self.reads += 1
        return PCS.fake_reading(self.state, soc=self.soc, power=self.power)

    def on_command(self, action):
        self.commands.append(action)
        if action == "pcs_charge":
            self.state = "CHARGING"
        elif action in ("pcs_stop_power", "pcs_stop"):
            self.state = "STANDBY"


def build_entry_env(state=None, pause_result=0, restore_result=0,
                    operator_ok=True, authorization=None, deadline=None):
    """
    回傳 (deps, env)。env 內含驗收所需的觀測點。

    🔴 `SVC._live_leg_guard` 是 **PROCESS_SCOPED** 一次性狀態
       （Attempt #7 shared-state inventory 已分類）。
       本檔在同一個 process 內會跑多次完整 live leg，
       因此每次建環境都必須把它重置 —— 否則第二次之後的 leg 會被擋下，
       測出來的會是「測試自己的殘留狀態」而不是 production 行為。
    🔴 `authorization` 必須在 `build_field_dependencies()` **之前**覆寫：
       provider 會 `ctx = dict(runtime_context or {})` 複製一份，
       事後改 `env["ctx"]` 不會生效。
    """
    SVC._live_leg_guard.update(used=False, action=None, final_state=None)
    clock = PCS.FakeClock()
    dev = ResponsiveDevice()
    sent = []
    chain_state = {"dispatched": 0, "actions": [], "readbacks": []}
    inside = {}

    def operator_run(**kwargs):
        sent.append(kwargs)
        ok = bool(operator_ok)
        if ok:
            dev.on_command(kwargs.get("action"))
        return {"control_success": ok, "blocked": False}

    ssh = EntrySshRunner(pause_result=pause_result,
                         restore_result=restore_result)
    ctx, bundle = PCS.build_ctx(state=dict(state or {"soc": 2.0}),
                                reader=dev.read)
    ctx["ssh_runner"] = ssh
    # 🔴 no-op sleeper：只避免真實等待，不改變任何判定邏輯
    ctx["sleeper"] = (lambda s: None)
    if authorization is not None:
        ctx["authorization"] = dict(authorization)
    if deadline is not None:
        # EXECUTION DEADLINE must be injected BEFORE
        # build_field_dependencies(), which copies ctx.
        ctx["deadline"] = deadline

    class _Chain(object):
        def __init__(self, executor, verifier):
            self.executor = executor
            self.verifier = verifier
            self.arbiter = bundle._chain.arbiter

        def run(self):
            action = "charge" if chain_state["dispatched"] == 0 else "stop"
            kw = CHARGE_KW if action == "charge" else 0.0
            vr = EXC.execute_and_verify(PCS._Ctrl(action, kw),
                                        self.executor, self.verifier)
            op, rb = vr.operator_result, vr.readback_result
            ok = bool(getattr(op, "sent", False))
            if ok:
                chain_state["dispatched"] += 1
                chain_state["actions"].append(action)
            chain_state["readbacks"].append(getattr(rb, "outcome", None))
            return PCS._ChainResult(action, ok, rb, vr.outcome)

    def factory(executor=None, verifier=None):
        if executor is None and verifier is None:
            return bundle._chain
        return _Chain(executor, verifier)

    bundle.factory = factory

    def observe():
        if chain_state["dispatched"] > 0:
            bundle.state["authority"] = "OWNED_BY_PHASE6"
        return PCS.FakeBundle.observe(bundle)

    bundle.observe = observe
    bundle.reader = dev.read

    deps = FP.build_field_dependencies(PCS.provider_config(), ctx)
    # 🔴 permit 的前置事實由 provider **自己的** gate 函式產生
    deps.safety_preflight()
    deps.gate_a()
    deps.handoff_readiness()

    env = {"ssh": ssh, "sent": sent, "chain": chain_state, "clock": clock,
           "bundle": bundle, "ctx": ctx, "inside": inside, "dev": dev,
           "operator_run": operator_run}
    return deps, env


def run_entry(deps, env, before_run=None, gate_spy=None):
    """
    唯一允許的進入點：`deps.arm_and_run_handoff()`。

    🔴 只替換最底層的 operator / verifier clock —— 不碰 HandoffRunner 的
       判定邏輯、不碰 FirstLiveDispatcher、不碰 step 7 的任何條件。

    🔴 `before_run(holder, runner)` 在 provider 建構完成、`run()` 開始**之前**
       呼叫 —— 這是取得 provider 內部物件（lease / permit）的唯一時點，
       因為 holder 是在 `arm_and_run_handoff()` 內才被填滿的。
    🔴 `gate_spy(permit)` 在 step 7 的 entry gate 被評估**當下**呼叫，
       用來捕捉「真實 Step 7 前狀態」。兩者都**不改變**任何判定結果。
    """
    inside = env["inside"]
    clock = env["clock"]
    orig_build_verifier = PRD.build_verifier
    orig_run_live = SVC.run_live_leg_cli

    def patched_verifier(mode, leg_, reader, config=None):
        v = orig_build_verifier(mode, leg_, reader, config)
        if v is not None:
            v._clock = clock
            v._sleep = clock.sleep
        return v

    def patched_live(action, bundle, **kw):
        # 🔴 只注入 operator_run ＋ 記錄「live leg 執行當下」的旗標；
        #    其餘一律沿用 production 的 run_live_leg_cli。
        inside["dispatch_enabled"] = HO.DISPATCH_ENABLED
        inside["rt_dispatch_enabled"] = RT.DISPATCH_ENABLED
        kw["operator_run"] = env["operator_run"]
        kw["verbose"] = False
        PRD.build_verifier = patched_verifier
        try:
            return orig_run_live(action, bundle, **kw)
        finally:
            PRD.build_verifier = orig_build_verifier

    orig_runner_run = HO.HandoffRunner.run
    orig_gate = HO._entry_gate_authorizes

    def spy_run(self_):
        env["runner"] = self_
        env["holder"] = holder_of(deps)
        if before_run is not None:
            before_run(env["holder"], self_)
        return orig_runner_run(self_)

    def spy_gate(permit):
        if gate_spy is not None:
            gate_spy(permit)
        return orig_gate(permit)

    SVC.run_live_leg_cli = patched_live
    HO.HandoffRunner.run = spy_run
    HO._entry_gate_authorizes = spy_gate
    try:
        res = deps.arm_and_run_handoff()
    finally:
        SVC.run_live_leg_cli = orig_run_live
        HO.HandoffRunner.run = orig_runner_run
        HO._entry_gate_authorizes = orig_gate
    env["result"] = res
    env["artifact"] = deps.collect_artifact(res)
    return res


def holder_of(deps):
    """
    provider 內部 holder —— 由 deps 的 closure 取得，不自行重建任何物件。

    🔴 只有在 `arm_and_run_handoff()` 已經走到建構完成之後才有內容。
    """
    for cell in (deps.arm_and_run_handoff.__closure__ or ()):
        v = cell.cell_contents
        if isinstance(v, dict) and "handoff_runner" in v:
            return v
    for cell in (deps.arm_and_run_handoff.__closure__ or ()):
        v = cell.cell_contents
        if isinstance(v, dict) and "permit" in v:
            return v
    raise AssertionError("找不到 provider holder")


def steps_of(res):
    return {s.get("step"): s for s in (res.get("steps") or ())}


# ======================================================================
# EP — PRODUCTION ENTRY-POINT SMOKE（完整成功路徑）
# ======================================================================
def test_EP_entry_point_smoke():
    print("\n[EP] PRODUCTION ENTRY-POINT SMOKE（完整 production control flow）")
    deps, env = build_entry_env()
    res = run_entry(deps, env)
    h = env["holder"]
    permit, lease, guard = h["permit"], h["lease"], h["guard"]
    st = steps_of(res)

    for k in ("1_PRE_PAUSE_PREFLIGHT", "2_PAUSE", "3_4_PAUSE_VERIFY",
              "5_OWNERSHIP", "6_POST_PAUSE_PRECHECK"):
        check("EP0 %s 通過" % k, st.get(k, {}).get("ok") is True)

    check("EP1 ★★ Step 7 通過（不再因 authority 未取得而被拒）",
          st.get("7_FIRST_LIVE", {}).get("ok") is True)
    check("EP2 ★★ dispatcher 真的被進入",
          int(getattr(h["dispatcher"], "calls", 0)) > 0)
    check("EP3 ★★ authority 由真實 acquire 產生",
          lease.acquire_attempted is True and lease.acquired_once is True)
    check("EP4 ★★ FULL PERMIT 通過（含 authority 兩項）",
          permit.last_checks is not None
          and permit.last_checks.get("authority_acquired") is True
          and all(permit.last_checks.values()))
    check("EP5 ★★ live leg 期間 HO.DISPATCH_ENABLED = True",
          env["inside"].get("dispatch_enabled") is True)
    check("EP6 ★★ live leg 期間 RT.DISPATCH_ENABLED 全程 False",
          env["inside"].get("rt_dispatch_enabled") is False)
    check("EP7 ★★ CHARGE 5.0 kW 恰好一次",
          env["chain"]["actions"].count("charge") == 1
          and all(k.get("power") == CHARGE_KW for k in env["sent"]
                  if k.get("action") == "pcs_charge"))
    check("EP8 ★★ DISCHARGE = 0",
          "discharge" not in env["chain"]["actions"])
    check("EP9 ★★ readback PASS",
          env["chain"]["readbacks"][0] == EXC.VERIFY_SUCCESS)
    check("EP10 ★★ restore 送出且驗證 RESUMED",
          st.get("10_RESTORE", {}).get("ok") is True
          and st.get("11_RESTORE_VERIFY", {}).get("ok") is True)
    check("EP11 ★★ 結束後 rest state 恢復", rest_ok())
    check("EP12 ★★ authority 已釋放", lease.held is False)
    c = guard.as_dict()
    check("EP13 ★★ charge 1 / discharge 0 / 無違規",
          c["counts"].get("phase6_charge") == 1
          and c["counts"].get("phase6_discharge", 0) == 0
          and c["violations"] == [])
    check("EP14 ★★ 實際 SSH 動詞含 pause 1 / restore 1",
          env["ssh"].calls.count("pause") == 1
          and env["ssh"].calls.count("restore") == 1)
    art = env["artifact"]
    check("EP15 ★★ artifact entry_gate_ok = True",
          art.get("entry_gate_ok") is True)
    check("EP16 ★★ artifact dispatcher_entered = True",
          art.get("dispatcher_entered") is True)
    check("EP17 ★★ post_restore_external_liveness 已填（不再是 None）",
          isinstance(art.get("post_restore_external_liveness"), dict))
    check("EP18 ★★ external_liveness_final 指向 restore 之後的觀測",
          art.get("external_liveness_final") is not None
          and art["external_liveness_final"].get("at")
          == art["post_restore_external_liveness"].get("at"))
    env["deps"] = deps
    return deps, env, permit


# ======================================================================
# A8R — ATTEMPT #8 EXACT REGRESSION
# ======================================================================
def test_A8R_exact_regression():
    print("\n[A8R] ATTEMPT #8 EXACT REGRESSION（step 7 不得因 authority 未取得而拒）")
    deps, env = build_entry_env()
    snap = {}

    def at_entry_gate(permit):
        """🔴 在 step 7 的 entry gate 被評估**當下**取樣 —— 這就是
           Attempt #8 失敗的那個時點。本函式不改變任何判定。"""
        runner = env["runner"]
        lease_ = runner.dispatcher.lease
        snap["dispatch_enabled"] = HO.DISPATCH_ENABLED
        snap["acquire_attempted"] = lease_.acquire_attempted
        snap["acquired_once"] = lease_.acquired_once
        snap["held"] = lease_.held
        # 完整 permit 在此刻必然不成立 —— 這正是 Attempt #8 的死結
        snap["full_ok"], snap["full_checks"] = permit.evaluate()

    res = run_entry(deps, env, gate_spy=at_entry_gate)
    h = env["holder"]
    permit, lease = h["permit"], h["lease"]
    st = steps_of(res)

    # 🔴 真實 Step 7 之前的狀態（與 Attempt #8 artifact 完全一致）
    check("A8R1 ★★ step 7 當下 HO.DISPATCH_ENABLED = False",
          snap.get("dispatch_enabled") is False)
    check("A8R2 ★★ step 7 當下 authority_acquire_attempted = False",
          snap.get("acquire_attempted") is False)
    check("A8R3 ★★ step 7 當下 authority_acquired = False",
          snap.get("held") is False and snap.get("acquired_once") is False)
    check("A8R3b ★★ 此刻完整 permit 必為 False，且被拒原因正是 authority 兩項",
          snap.get("full_ok") is False
          and sorted(k for k, v in (snap.get("full_checks") or {}).items()
                     if not v) == sorted(FL.FIRST_LIVE_ENTRY_EXCLUDED))

    check("A8R4 ★★ 前段與 Attempt #8 相同：PRE_PAUSE / PAUSE / VERIFY / "
          "OWNERSHIP / GATE B 全 PASS",
          all(st.get(k, {}).get("ok") is True
              for k in ("1_PRE_PAUSE_PREFLIGHT", "2_PAUSE",
                        "3_4_PAUSE_VERIFY", "5_OWNERSHIP",
                        "6_POST_PAUSE_PRECHECK")))
    d7 = st.get("7_FIRST_LIVE", {})
    check("A8R5 ★★ Step 7 不再出現 Attempt #8 的拒絕訊息",
          "DISPATCH_ENABLED=False" not in (d7.get("detail") or ""))
    check("A8R6 ★★ ENTRY_GATE = PASS", d7.get("ok") is True)
    check("A8R7 ★★ dispatcher entered = True",
          int(getattr(h["dispatcher"], "calls", 0)) > 0)
    check("A8R8 ★★ authority_acquire_attempted 變為 True",
          lease.acquire_attempted is True)
    check("A8R9 ★★ authority_acquired 變為 True", lease.acquired_once is True)
    check("A8R10 ★★ FULL PERMIT = PASS",
          permit.state == FL.PERMIT_EXITED
          and all((permit.last_checks or {"x": False}).values()))
    check("A8R11 ★★ HO.DISPATCH_ENABLED 只在 scope 內為 True",
          env["inside"].get("dispatch_enabled") is True and rest_ok())
    check("A8R12 ★★ CHARGE 5.0 kW 恰好一次",
          env["chain"]["actions"].count("charge") == 1)
    check("A8R13 ★★ entry gate 的前置條件**不含** authority 兩項",
          all(k not in FL.FIRST_LIVE_ENTRY_PRECONDITIONS
              for k in FL.FIRST_LIVE_ENTRY_EXCLUDED))
    check("A8R14 ★★ FULL PERMIT 仍保留 authority 兩項（未降低 safety）",
          all(k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS
              for k in FL.FIRST_LIVE_ENTRY_EXCLUDED))


# ======================================================================
# O — NEGATIVE ORDERING TESTS
# ======================================================================
def _counts(h):
    c = h["guard"].as_dict()["counts"]
    return (c.get("phase6_charge", 0), c.get("phase6_discharge", 0),
            c.get("phase6_pcs_stop", 0))


def test_O1_authority_fail():
    print("\n[O1] ENTRY GATE PASS 但 authority acquire FAIL")
    deps, env = build_entry_env()

    def fail_acquire(h_, runner):
        h_["lease"]._acquire = (lambda: (False, "AUTHORITY_BUSY"))

    res = run_entry(deps, env, before_run=fail_acquire)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = _counts(h)
    check("O1a ★★ entry gate 已 PASS（dispatcher 有被進入）",
          int(getattr(h["dispatcher"], "calls", 0)) > 0)
    check("O1b ★★ dispatch = False",
          (h["dispatcher"].last or {}).get("dispatched") is False)
    check("O1c ★★ 0 CHARGE / 0 DISCHARGE / 0 PCS STOP",
          (ch, di, stp) == (0, 0, 0))
    check("O1d ★★ 未進入 scope（permit 未 ACTIVE / EXITED）",
          h["permit"].state == FL.PERMIT_NOT_ENTERED)
    check("O1e ★★ HO.DISPATCH_ENABLED 全程 False", rest_ok())
    check("O1f ★★ step 7 判為失敗", st.get("7_FIRST_LIVE", {}).get("ok")
          is not True)
    check("O1g ★★ 仍走到 restore 並解除責任",
          st.get("10_RESTORE", {}).get("ok") is True
          and res.get("restore_responsibility") is False)


def test_O2_full_permit_fail_after_authority():
    print("\n[O2] authority 已取得，但 FULL PERMIT FAIL")
    deps, env = build_entry_env()

    def deny_full_permit(h_, runner):
        permit_ = h_["permit"]
        orig_eval = permit_.evaluate

        def deny_full():
            # 🔴 只讓 STAGE B 的其中一項條件不成立；
            #    STAGE A entry gate 完全不受影響。
            ok, checks = orig_eval()
            checks = dict(checks, charge_budget_available=False)
            permit_.last_checks = checks
            permit_.denied_reason = ["charge_budget_available"]
            return False, checks

        permit_.evaluate = deny_full

    res = run_entry(deps, env, before_run=deny_full_permit)
    h = env["holder"]
    permit = h["permit"]
    st = steps_of(res)
    ch, di, stp = _counts(h)
    check("O2a ★★ authority 確實已取得",
          h["lease"].acquire_attempted is True
          and h["lease"].acquired_once is True)
    check("O2b ★★ 0 CHARGE / 0 DISCHARGE", (ch, di) == (0, 0))
    check("O2c ★★ 未進入 dispatch scope", permit.state == FL.PERMIT_DENIED)
    check("O2d ★★ authority 已釋放", h["lease"].held is False)
    check("O2e ★★ dispatch = False",
          (h["dispatcher"].last or {}).get("dispatched") is False)
    check("O2f ★★ outcome 為 NOT_ATTEMPTED（確定未送出，非 UNKNOWN）",
          (h["dispatcher"].last or {}).get("outcome")
          == FL.DISPATCH_NOT_ATTEMPTED)
    check("O2g ★★ HO.DISPATCH_ENABLED 恢復 False", rest_ok())
    check("O2h ★★ 仍安全歸還 external",
          st.get("10_RESTORE", {}).get("ok") is True
          and res.get("restore_responsibility") is False)


def test_O3_scheduled_auth_invalid():
    print("\n[O3] ScheduledLiveLegAuthorization invalid")
    # 🔴 以既有契約讓 authorization 失效：attempt_id 綁定不符
    #    -> provider 的 `_permit_evidence()` 會把 `attempt_id_matches`
    #       判為 False（canonical path，未另造判定）。
    deps, env = build_entry_env(
        authorization={"attempt_id": "WRONG-ATTEMPT"})
    res = run_entry(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = _counts(h)
    check("O3a ★★ dispatcher 不得被進入",
          int(getattr(h["dispatcher"], "calls", 0)) == 0)
    check("O3b ★★ authority acquire = 0",
          h["lease"].acquire_attempted is False)
    check("O3c ★★ 0 CHARGE / 0 DISCHARGE / 0 PCS STOP",
          (ch, di, stp) == (0, 0, 0))
    check("O3d ★★ step 7 被 entry gate 擋下",
          st.get("7_FIRST_LIVE", {}).get("ok") is not True
          and "ENTRY GATE DENIED" in (st.get("7_FIRST_LIVE", {})
                                      .get("detail") or ""))
    check("O3e ★★ rest state 維持", rest_ok())


def test_O4_gate_b_fail():
    print("\n[O4] GATE B（6_POST_PAUSE_PRECHECK）FAIL")
    deps, env = build_entry_env(state={"soc": 2.0, "tou": "PEAK"})
    res = run_entry(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = _counts(h)
    check("O4a ★★ GATE B 未通過",
          st.get("6_POST_PAUSE_PRECHECK", {}).get("ok") is not True)
    check("O4b ★★ dispatcher 不得被進入",
          int(getattr(h["dispatcher"], "calls", 0)) == 0)
    check("O4c ★★ authority acquire = 0",
          h["lease"].acquire_attempted is False)
    check("O4d ★★ 0 CHARGE / 0 DISCHARGE / 0 PCS STOP",
          (ch, di, stp) == (0, 0, 0))
    check("O4e ★★ rest state 維持", rest_ok())


def test_O5_ownership_both():
    print("\n[O5] ownership = BOTH")
    deps, env = build_entry_env(state={"soc": 2.0,
                                       "authority": "OWNED_BY_PHASE6"})
    res = run_entry(deps, env)
    h = env["holder"]
    ch, di, stp = _counts(h)
    check("O5a ★★ dispatcher 不得被進入",
          int(getattr(h["dispatcher"], "calls", 0)) == 0)
    check("O5b ★★ authority acquire = 0",
          h["lease"].acquire_attempted is False)
    check("O5c ★★ 0 CHARGE / 0 DISCHARGE / 0 PCS STOP",
          (ch, di, stp) == (0, 0, 0))
    check("O5d ★★ rest state 維持", rest_ok())


def test_O6_dispatch_enabled_already_true():
    print("\n[O6] step 7 前 HO.DISPATCH_ENABLED 意外為 True -> Fail Closed")
    deps, env = build_entry_env()

    def poison(h_, runner):
        # 🔴 模擬「有人在 scope 外把 dispatch 打開」——
        #    進入 dispatcher 前的正確狀態本來就是 False。
        HO.DISPATCH_ENABLED = True

    try:
        res = run_entry(deps, env, before_run=poison)
    finally:
        # 🔴 測試自身必須負責把模組常數還原到 rest state
        HO.DISPATCH_ENABLED = False
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = _counts(h)
    d = st.get("7_FIRST_LIVE", {})
    check("O6a ★★ step 7 被擋下", d.get("ok") is not True)
    check("O6b ★★ 理由明示 rest state 異常",
          "DISPATCH_ENABLED 已是 True" in (d.get("detail") or ""))
    check("O6c ★★ dispatcher 不得被進入",
          int(getattr(h["dispatcher"], "calls", 0)) == 0)
    check("O6d ★★ authority acquire = 0",
          h["lease"].acquire_attempted is False)
    check("O6e ★★ 0 CHARGE / 0 DISCHARGE / 0 PCS STOP",
          (ch, di, stp) == (0, 0, 0))
    check("O6f ★★ 結束後 rest state 恢復", rest_ok())


def test_O7_full_permit_not_forgeable_before_authority():
    print("\n[O7] FULL PERMIT 不得在 authority acquire 前被偽造為 PASS")
    deps, env = build_entry_env()
    pre = {}

    def snapshot(pm):
        # 🔴 取樣時點必須是 **step 7 的 entry gate 當下**：
        #    step 1~6 已全部 PASS，但 authority 尚未取得。
        #    （若在 run() 之前取樣，step 1~6 的 evidence 都還是 False，
        #      就測不到「只差 authority 兩項」這件事。）
        #    使用 production 的 evidence source 評估，不手填任何 check。
        ls = env["runner"].dispatcher.lease
        pre["full"] = pm.evaluate()
        pre["entry"] = pm.evaluate_entry()
        pre["acquire_attempted"] = ls.acquire_attempted

    run_entry(deps, env, gate_spy=snapshot)
    h = env["holder"]
    permit, lease = h["permit"], h["lease"]
    full_ok, full_checks = pre["full"]
    entry_ok, entry_checks = pre["entry"]
    check("O7a ★★ authority 未取得時 FULL PERMIT 必為 False", full_ok is False)
    check("O7b ★★ 被拒原因正是 authority 兩項",
          sorted(k for k, v in full_checks.items() if not v)
          == sorted(FL.FIRST_LIVE_ENTRY_EXCLUDED))
    check("O7c ★★ 但 ENTRY GATE 可以 PASS（這正是解死結的關鍵）",
          entry_ok is True)
    check("O7d ★★ entry checks 結構上不含 authority 兩項",
          all(k not in entry_checks for k in FL.FIRST_LIVE_ENTRY_EXCLUDED))
    check("O7e ★★ 評估本身不得有副作用（當下 lease 未被呼叫）",
          pre["acquire_attempted"] is False)
    check("O7f ★★ authority 取得後 FULL PERMIT 才通過",
          lease.acquired_once is True
          and all((permit.last_checks or {"x": False}).values()))


def test_O8_canonical_ordering():
    print("\n[O8] 成功路徑的 control-flow ordering 必須可觀察且不得改序")
    deps, env = build_entry_env()
    run_entry(deps, env)
    h = env["holder"]
    permit = h["permit"]
    evs = permit.ordering_events()
    print("      ordering = %r" % (list(evs),))
    check("O8a ★★ ordering 恰好等於 canonical 序列",
          FL.ordering_is_canonical(evs) is True)
    check("O8b ★★ canonical 序列即裁示所列六步",
          FL.CANONICAL_DISPATCH_ORDERING
          == ("ENTRY_GATE_PASS", "AUTHORITY_ACQUIRE_ATTEMPT",
              "AUTHORITY_ACQUIRED", "FULL_PERMIT_PASS",
              "DISPATCH_SCOPE_ENTER", "CHARGE_SEND"))
    idx = {e: i for i, e in enumerate(evs)}
    check("O8c ★★ ENTRY_GATE_PASS < AUTHORITY_ACQUIRE_ATTEMPT",
          idx["ENTRY_GATE_PASS"] < idx["AUTHORITY_ACQUIRE_ATTEMPT"])
    check("O8d ★★ AUTHORITY_ACQUIRED < FULL_PERMIT_PASS",
          idx["AUTHORITY_ACQUIRED"] < idx["FULL_PERMIT_PASS"])
    check("O8e ★★ FULL_PERMIT_PASS < DISPATCH_SCOPE_ENTER",
          idx["FULL_PERMIT_PASS"] < idx["DISPATCH_SCOPE_ENTER"])
    check("O8f ★★ DISPATCH_SCOPE_ENTER < CHARGE_SEND",
          idx["DISPATCH_SCOPE_ENTER"] < idx["CHARGE_SEND"])
    check("O8g ★★ 亂序 / 缺項一律 False",
          FL.ordering_is_canonical(tuple(reversed(evs))) is False
          and FL.ordering_is_canonical(evs[:-1]) is False)
    check("O8h ★★ artifact 帶出 ordering 且判定為 canonical",
          env["artifact"].get("dispatch_ordering_canonical") is True
          and list(env["artifact"].get("dispatch_ordering_events") or ())
          == list(evs))


# ======================================================================
# NB — 禁止全 True 合成 evidence 作為 control-flow 證據
# ======================================================================
def test_NB_no_synthetic_blind_spot():
    print("\n[NB] 本檔不得以全 True 合成 evidence 當作 control-flow 證據")
    # 🔴 一律以 AST 判定。既有教訓：純文字掃描會掃到**本檔自己的**
    #    註解與字串字面值，產生假陽性。
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    tree = ast.parse(src)

    synth, direct_build, direct_call, entry_calls = [], [], [], 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.DictComp, ast.GeneratorExp, ast.ListComp)):
            seg = ast.get_source_segment(src, node) or ""
            if ("FIRST_LIVE_PERMIT_PRECONDITIONS" in seg
                    and "True" in seg):
                synth.append(seg)
        if isinstance(node, ast.Call):
            f = node.func
            name = (f.attr if isinstance(f, ast.Attribute)
                    else (f.id if isinstance(f, ast.Name) else None))
            if name == "FirstLiveDispatcher":
                direct_build.append(ast.dump(f))
            if name == "arm_and_run_handoff":
                entry_calls += 1
            # 直接把 dispatcher 物件當 callable 呼叫
            if isinstance(f, ast.Name) and f.id in ("disp", "dispatcher"):
                direct_call.append(f.id)
            if isinstance(f, ast.Subscript):
                pass

    check("NB1 ★★ 無全 True 合成的 FIRST_LIVE_PERMIT_PRECONDITIONS evidence",
          synth == [])
    check("NB2 ★★ 未直接建構 FirstLiveDispatcher", direct_build == [])
    check("NB3 ★★ 未繞過 HandoffRunner 直接呼叫 dispatcher",
          direct_call == [])
    check("NB4 ★★ 唯一進入點為 arm_and_run_handoff", entry_calls >= 1)
    check("NB5 ★★ HO 與 FL 的 ENTRY_GATE_PASS 字面值一致",
          HO.EV_ENTRY_GATE_PASS == FL.EV_ENTRY_GATE_PASS)


# ======================================================================
def main():
    print("=" * 72)
    print("  PRODUCTION ENTRY-POINT / CONTROL-FLOW ORDERING SMOKE")
    print("=" * 72)
    for fn in (test_EP_entry_point_smoke, test_A8R_exact_regression,
               test_O1_authority_fail,
               test_O2_full_permit_fail_after_authority,
               test_O3_scheduled_auth_invalid, test_O4_gate_b_fail,
               test_O5_ownership_both, test_O6_dispatch_enabled_already_true,
               test_O7_full_permit_not_forgeable_before_authority,
               test_O8_canonical_ordering, test_NB_no_synthetic_blind_spot):
        fn()
    for d in PCS.TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print(f"  rest state：HO={HO.DISPATCH_ENABLED} RT={RT.DISPATCH_ENABLED}")
    print("=" * 72)
    return 0 if (n == tot and rest_ok()) else 1


if __name__ == "__main__":
    sys.exit(main())
