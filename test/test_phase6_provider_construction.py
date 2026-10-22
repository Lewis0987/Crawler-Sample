# -*- coding: utf-8 -*-
"""
test_phase6_provider_construction.py — REAL PROVIDER CONSTRUCTION COVERAGE
======================================================================
核心命題
    「validate-only 只能證明 static / structural 有效；
      **不能**證明 production field dependency graph 建得起來。
      因此必須有一條走**真正的** `build_field_dependencies()`
      -> `build_first_live_dispatcher()` -> `HandoffRunner` 的離線測試，
      所有 field 依賴以 fake 注入，**0 field I/O**。」

🔴 本檔**不得**自行重拼 `build_first_live_dispatcher(...)` 的呼叫 ——
   必須讓 provider production code 自己呼叫工廠；
   未來 caller 多一個 kwarg 而 factory 沒跟上時，本檔必須直接 FAIL。

涵蓋使用者指定項目
    F1 ~ F8    factory contract
    PC1 ~      real provider construction（fake dependencies）
    A6R1 ~     Attempt #6 TypeError regression
    FM1 ~      full provider -> FIRST LIVE mock

用法
    python test_phase6_provider_construction.py        # exit 0 = PASS
"""
import ast
import datetime
import inspect
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
import phase6_first_live_field_provider as FP        # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402
import pcs_auto_control_runtime as RT                # noqa: E402
import decision_policy as DP                         # noqa: E402

RESULTS = []
TMPDIRS = []
CFGD = CFG.DEFAULT_CONTROL_CONFIG
CHARGE_KW = CFGD.charge_power_kw


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p6_pc_")
    TMPDIRS.append(d)
    return d


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


# ======================================================================
# FAKE FIELD DEPENDENCIES —— 唯一被替換的一層（0 network / 0 subprocess）
# ======================================================================
class FakeSshRunner(object):
    """取代 `GuardSshRunner`；**不 spawn 任何行程**。"""

    def __init__(self, marks=None):
        self.calls = []
        self._marks = list(marks or ["2026-10-01 00:05:10",
                                     "2026-10-01 00:05:19",
                                     "2026-10-01 00:05:28"])
        self._i = 0

    def __call__(self, verb, connect_timeout_sec, command_timeout_sec):
        self.calls.append(verb)
        if verb == "probe":
            out = ("pid=99001\n"
                   "cwd=%s\ncmd=%s\n"
                   "screen_count=1\nguard_variant=B2\n"
                   # 🔴 五個 capability 動詞缺一即 CAPABILITY_MALFORMED
                   "capability_probe=true\ncapability_loopcheck=true\n"
                   "capability_status=true\n"
                   "capability_pause=true\ncapability_restore=true\n"
                   % (RA.EXPECTED_CWD, RA.EXPECTED_CMD))
        elif verb == "loopcheck":
            m = self._marks[min(self._i, len(self._marks) - 1)]
            self._i += 1
            out = "mark1=%s\nmark2=%s\n" % (m, m)
        else:
            out = ""
        # 🔴 回傳 shape 沿用既有 `GuardSshRunner._run_real()` 契約
        return {"exit_code": 0, "stdout": out.strip(),
                "connect_sec": 0.1, "total_sec": 0.2}


class FakeClient(object):
    logged_in = True

    def login_hmi(self, user=None):
        return "fake-token"


class FakeMeter(object):
    class _Snap(object):
        valid = True
        stale = False
        power_kw = 0.4
        age_sec = 0.5

    def get_snapshot(self):
        return self._Snap()

    def stop(self):
        return None


class FakeStore(object):
    class _Cur(object):
        record = None
        trust = None

    def current(self):
        return self._Cur()

    def update_from_verified_result(self, vr):
        class U(object):
            outcome = "UPDATED"
            reason = None
        return U()


def fake_reading(pcs="STANDBY", soc=2.0, power=-4.8):
    """
    🔴 欄位沿用既有 `ess_snapshot_adapter._demo_reading()` 的 production 形狀，
       不自創第二套 reading schema。
    """
    running = pcs in ("CHARGING", "DISCHARGING", "STANDBY")
    return {"communication_ok": True,
            "soc_percent": soc,
            "actual_active_power_kw": power,
            "pcs_fault_flag": False,
            "pcs_charging_flag": pcs == "CHARGING",
            "pcs_discharging_flag": pcs == "DISCHARGING",
            "pcs_standby_flag": pcs == "STANDBY",
            "pcs_running_flag": running,
            "pcs_stopped_flag": pcs == "STOPPED",
            "battery_power_status": "已上電",
            "pcs_control_mode_code": "manual",
            "pcs_schedule_enabled": False,
            "pcs_manual_switch": 1,
            "alarm_rows": [], "alarm_total": 0, "alarm_total_raw": 0,
            "_fail": []}


class FakeBundle(object):
    """
    最小 live chain bundle：提供 canonical soc_band / soc_charge_stop_pct 路徑。

    🔴 policy 使用**既有** `DP.TouArbitragePolicy`（不自建第二套），
       因此 `canonical_soc_band` / `canonical_soc_max_pct` 走的是正式路徑。
    """

    def __init__(self, state=None, reader=None):
        self.state = state if state is not None else {}
        self.reader = reader or (lambda: fake_reading())
        self._policy = DP.TouArbitragePolicy()
        self._chain = self._mk_chain()

    def _mk_chain(self):
        pol = self._policy

        class Observer(object):
            policy = pol

        class Arbiter(object):
            observer = Observer()

        class Chain(object):
            arbiter = Arbiter()

            def run(self_inner):
                return None

        return Chain()

    def factory(self, executor=None, verifier=None):
        return self._chain

    def observe(self):
        pol = self._policy
        st = self.state

        class Arb(object):
            fresh_decision_action = st.get("decision", "charge")
            grid_state = "NEAR_ZERO"
            tou_state = st.get("tou", "OFF_PEAK")
            safety_allowed = True
            authority_state = st.get("authority", "IDLE")
            direction_reason = "DIRECTION_OK"
            soc_percent = st.get("soc", 2.0)
            # 🔴 live_leg_precheck 讀的是 arbitration.pcs_state（非 result）
            pcs_state = st.get("pcs", "STANDBY")
            valid = True
            pcs_fault = False
            active_alarm_count = 0
            alarm_source_complete = True
            schedule_switch = 0
            manual_switch = 1

        class Res(object):
            arbitration = Arb()
            pcs_state = st.get("pcs", "STANDBY")

        # 🔴 以既有 `TouArbitragePolicy.__call__(dinput)` 驅動 SOC latch ——
        #    canonical path，不直接碰 `_latch`、不自建第二套 band 判定。
        class _TOU(object):
            state = Arb.tou_state

        class _GRID(object):
            state = Arb.grid_state

        class _ESS(object):
            soc_percent = Arb.soc_percent

        class _DIn(object):
            tou = _TOU()
            grid = _GRID()
            ess = _ESS()

        pol(_DIn())
        return Res()


def build_ctx(state=None, marks=None, reader=None):
    """provider `runtime_context`：**全部 fake**，0 field I/O。"""
    st = state if state is not None else {}
    bundle = FakeBundle(st, reader=reader)
    return {
        "ssh_runner": FakeSshRunner(marks),
        "api_client": FakeClient(),
        "ess_reader": reader or (lambda: fake_reading(soc=st.get("soc", 2.0))),
        "relogin": (lambda: True),
        "meter_client": FakeMeter(),
        "live_bundle": bundle,
        "last_control_store": FakeStore(),
        # 🔴 既有契約：prime_classifier(observe_source) -> last observation
        "prime_classifier": (lambda observe_source: observe_source()),
        # 🔴 只注入 base dir；journal path 仍由 provider 依 attempt_id 導出，
        #    這樣 construction smoke 才驗得到 production 的 scoping 行為。
        "journal_dir": tmpdir(),
        "attempt_id": "FIRST-LIVE-ATTEMPT-SMOKE",
        "authorization": {"attempt_id": "FIRST-LIVE-ATTEMPT-SMOKE"},
        "field_authorized": True,
        "one_shot_consumed": False,
        "inside_window": True,
        "target_date": "2026-10-01",
        "window_start": "00:05:00",
        "window_end": "00:30:00",
    }, bundle


def provider_config():
    return {"dependency_provider": FP.PROVIDER_ID}


# ======================================================================
# F — FACTORY CONTRACT
# ======================================================================
def test_F_factory_contract():
    print("\n[F] FACTORY CONTRACT")
    sig = inspect.signature(FL.build_first_live_dispatcher)
    names = list(sig.parameters)
    check("F0 factory 具備 dispatch_permit 參數", "dispatch_permit" in names)
    check("F0b factory 具備 permit_event 參數", "permit_event" in names)
    check("F0c 兩者預設為 None（既有呼叫端不受影響）",
          sig.parameters["dispatch_permit"].default is None
          and sig.parameters["permit_event"].default is None)
    check("F5 ★★ factory **未**使用 **kwargs（signature 必須可驗證）",
          not any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()))

    ev = dict((k, True) for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS)
    permit = FL.FirstLiveDispatchPermit("F-TEST", lambda: ev)
    seen = []

    def on_event(kind, aid):
        seen.append((kind, aid))

    d, lease, guard = FL.build_first_live_dispatcher(
        live_leg_runner=(lambda: None),
        acquire_authority=(lambda: (True, "ok")),
        release_authority=(lambda: True),
        dispatch_permit=permit, permit_event=on_event)
    check("F2 ★★ dispatch_permit identity 保留（is 同一物件）",
          d.permit is permit)
    check("F3 ★★ permit_event identity 保留（is 同一 callable）",
          d._permit_event is on_event)
    check("F6 ★★ FirstLiveDispatcher 確實收到 permit",
          getattr(d, "permit", None) is not None)

    d2, _l2, _g2 = FL.build_first_live_dispatcher(
        live_leg_runner=(lambda: None),
        acquire_authority=(lambda: (True, "ok")),
        release_authority=(lambda: True))
    check("F4 ★★ 省略 permit 時既有語意不變（permit = None）",
          d2.permit is None and d2._permit_event is None)
    check("F8 ★★ 無 permit -> HandoffRunner step 7 不授權",
          HO._permit_authorizes(d2.permit) is False)
    check("F7 ★★ 有 permit -> 仍受 permit 條件控制",
          HO._permit_authorizes(permit) is True
          and HO._permit_authorizes(
              FL.FirstLiveDispatchPermit(
                  "X", lambda: dict(ev, gate_b_ok=False))) is False)

    try:
        FL.build_first_live_dispatcher(
            live_leg_runner=(lambda: None),
            acquire_authority=(lambda: (True, "ok")),
            release_authority=(lambda: True),
            definitely_not_a_real_kwarg=1)
        raised = False
    except TypeError:
        raised = True
    check("F5b ★★ 未知 kwarg -> TypeError（不得被吞掉）", raised)

    # factory 不得自行建立 permit / event
    src = io.open(os.path.join(HERE, "phase6_first_live_handoff.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
          and f.name == "build_first_live_dispatcher"][0]
    calls = {getattr(n.func, "id", getattr(n.func, "attr", ""))
             for n in ast.walk(fn) if isinstance(n, ast.Call)}
    check("F9 ★★ factory 內未建立第二份 FirstLiveDispatchPermit",
          "FirstLiveDispatchPermit" not in calls)
    check("F9b factory 內未建立 ScheduledLiveLegAuthorization",
          "ScheduledLiveLegAuthorization" not in calls
          and "build_scheduled_live_leg_authorization" not in calls)


# ======================================================================
# PC — REAL PROVIDER CONSTRUCTION（走 production build_field_dependencies）
# ======================================================================
def test_PC_real_construction():
    print("\n[PC] REAL PROVIDER CONSTRUCTION（fake dependencies，0 field I/O）")
    ctx, bundle = build_ctx()
    deps = FP.build_field_dependencies(provider_config(), ctx)
    check("PC1 ★★ build_field_dependencies() 成功（production path）",
          deps is not None)
    kw = deps.as_runner_kwargs()
    for k in FP.REQUIRED_DEPENDENCIES:
        check("PC2 必要相依 %s 已提供" % k, kw.get(k) is not None)
    check("PC2b register_pause_intent_hook 已提供",
          kw.get("register_pause_intent_hook") is not None)
    check("PC2c collect_artifact / safety_reset 已提供",
          kw.get("collect_artifact") is not None
          and kw.get("safety_reset") is not None)

    check("PC3 ★★ 建構期間未觸發任何 armed transport", rest_ok())
    check("PC3b RemoteSenders 預設未武裝", RA.RemoteSenders().armed is False)

    # 三個 gate 走 production 判定（fake 觀測）
    ok0, ck0 = deps.safety_preflight()
    check("PC4 Fresh Safety Preflight 可執行（production 判定）",
          isinstance(ck0, dict) and len(ck0) >= 8)
    okA, ckA, verdict = deps.gate_a()
    check("PC5 GATE A 可執行，verdict = %r" % verdict, verdict is not None)
    okR, ckR = deps.handoff_readiness()
    check("PC6 Handoff readiness 可執行", isinstance(ckR, dict))

    # 🔴 核心：真正呼叫 provider 的 arm_and_run_handoff **建構鏈**。
    #    Attempt #6 的 TypeError 發生在建構期，因此只要建構完成即可證明修好。
    #    construction smoke 不得執行 pause —— 以 stub 取代 `HandoffRunner.run`，
    #    讓所有物件照常建構，但不進入交接流程。
    captured = {}
    _orig_run = HO.HandoffRunner.run

    def _stub(self):
        captured["runner"] = self
        return {"outcome": "ABORTED", "state": "ABORTED_PREFLIGHT",
                "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT", "ok": False,
                           "detail": "construction smoke: 不執行交接"}]}

    HO.HandoffRunner.run = _stub
    try:
        res = deps.arm_and_run_handoff()
    finally:
        HO.HandoffRunner.run = _orig_run
    r = captured.get("runner")
    check("PC6b ★★ HandoffRunner 確實被建立（含 dispatcher / permit）",
          r is not None and r.dispatcher is not None
          and r.dispatch_permit is not None)
    check("PC6c ★★ soc_max_pct 由 canonical 路徑注入（非 None）",
          isinstance(r.soc_max_pct, float))

    # 🔴 2026-10-01 Attempt #7 postmortem：journal 必須 attempt-scoped
    jp = r.o.journal.path if getattr(r.o, "journal", None) else None
    aid = ctx["attempt_id"]
    check("PC6d ★★ journal_path_is_attempt_scoped（含 attempt_id）",
          jp is not None and aid.replace("-", "-") in os.path.basename(jp))
    check("PC6e ★★ journal path 不等於 legacy 共用 journal",
          jp is not None
          and os.path.basename(jp) != HO.LEGACY_SHARED_JOURNAL_NAME)
    check("PC6f ★★ journal 綁定 attempt_id（defense-in-depth）",
          getattr(r.o.journal, "attempt_id", None) == aid)
    check("PC7 ★★ arm_and_run_handoff() 未拋 TypeError（建構鏈完整）",
          isinstance(res, dict))
    check("PC7b 結果為既有 HandoffRunner 結果形狀",
          "outcome" in res and "steps" in res)
    check("PC8 ★★ 建構後 dispatch flags 仍為 rest state", rest_ok())
    ssh = ctx["ssh_runner"].calls
    check("PC9 ★★ 全程只用唯讀 SSH 動詞（無 pause / restore）",
          set(ssh) <= {"probe", "loopcheck"})
    art = deps.collect_artifact(res)
    check("PC10 collect_artifact 可執行且含 permit 證據",
          isinstance(art, dict) and "dispatch_permit" in art)
    check("PC10b scheduled_live_leg_authorization 證據存在",
          "scheduled_live_leg_authorization" in art)
    check("PC11 command counts 全 0",
          not art["phase6_command_counts"]["counts"])
    reset = deps.safety_reset()
    check("PC12 safety_reset 回到 rest state",
          reset["dispatch_enabled"] is False
          and reset["rt_dispatch_enabled"] is False
          and reset["senders_armed"] is False)


# ======================================================================
# A6R — ATTEMPT #6 TypeError REGRESSION
# ======================================================================
def test_A6R_regression():
    print("\n[A6R] ATTEMPT #6 TypeError REGRESSION")
    src = io.open(os.path.join(HERE, "phase6_first_live_field_provider.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    call = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "build_first_live_dispatcher"):
            call = n
            break
    check("A6R1 provider 確實呼叫 build_first_live_dispatcher", call is not None)
    kwargs = sorted(k.arg for k in (call.keywords or []) if k.arg)
    print("      provider 實際傳入的 kwargs = %r" % (kwargs,))
    sig = set(inspect.signature(FL.build_first_live_dispatcher).parameters)
    missing = [k for k in kwargs if k not in sig]
    check("A6R2 ★★ provider 的每個 kwarg 都在 factory signature 內"
          "（Attempt #6 的 TypeError 無法再重現）", not missing)
    if missing:
        print("      缺少：%r" % (missing,))
    check("A6R3 ★★ dispatch_permit / permit_event 確實在 provider 呼叫中",
          "dispatch_permit" in kwargs and "permit_event" in kwargs)

    # 端到端：Attempt #6 當時條件 -> 建構必須成功
    ctx, _b = build_ctx()
    deps = FP.build_field_dependencies(provider_config(), ctx)
    ok0, _c0 = deps.safety_preflight()
    okA, _cA, _v = deps.gate_a()
    okR, _cR = deps.handoff_readiness()
    try:
        deps.arm_and_run_handoff()
        te = None
    except TypeError as e:                                     # noqa: BLE001
        te = e
    check("A6R4 ★★ Attempt #6 條件重建 -> 無 TypeError", te is None)
    check("A6R5 三個 gate 皆可執行（與 Attempt #6 當時一致）",
          isinstance(ok0, bool) and isinstance(okA, bool)
          and isinstance(okR, bool))


# ======================================================================
# FM — FULL PROVIDER -> FIRST LIVE MOCK
# ======================================================================
class _ChainResult(object):
    def __init__(self, action, sent, rb, vr_ok):
        self.control_action = action
        self.executed = bool(sent)
        self.readback_outcome = getattr(rb, "outcome", None)
        self.readback_reason = getattr(rb, "reason", None)
        self.readback_observed_state = getattr(rb, "observed_state", None)
        self.readback_elapsed_sec = getattr(rb, "elapsed_sec", None)
        self.readback_attempts = getattr(rb, "attempts", None)
        self.lastcontrol_written = bool(vr_ok)
        self.outcome = vr_ok


class _Ctrl(object):
    def __init__(self, action, kw):
        self.action = action
        self.target_power_kw = kw


class FakeClock(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


class FakeDevice(object):
    """唯一被 mock 的最底層設備回讀。"""

    def __init__(self, plan, soc=2.0, power=-4.8):
        self.plan = list(plan)
        self.i = 0
        self.soc = soc
        self.power = power

    def read(self):
        st = self.plan[min(self.i, len(self.plan) - 1)]
        self.i += 1
        return fake_reading(st, soc=self.soc, power=self.power)


def test_FM_full_mock():
    print("\n[FM] FULL PROVIDER -> FIRST LIVE MOCK")
    clock = FakeClock()
    dev = FakeDevice(["STANDBY", "CHARGING", "CHARGING", "CHARGING",
                      "STANDBY", "STANDBY"])
    sent = []
    chain_state = {"dispatched": 0, "actions": [], "readbacks": []}

    def operator_run(**kwargs):
        sent.append(kwargs)
        return {"control_success": True, "blocked": False}

    ctx, bundle = build_ctx(state={"soc": 2.0}, reader=dev.read)

    # 🔴 讓 bundle.factory 回傳會驅動**真正** execute_and_verify 的 chain
    class _Chain(object):
        def __init__(self, executor, verifier):
            self.executor = executor
            self.verifier = verifier
            self.arbiter = bundle._chain.arbiter

        def run(self):
            action = "charge" if chain_state["dispatched"] == 0 else "stop"
            kw = CHARGE_KW if action == "charge" else 0.0
            vr = EXC.execute_and_verify(_Ctrl(action, kw), self.executor,
                                        self.verifier)
            op, rb = vr.operator_result, vr.readback_result
            ok = bool(getattr(op, "sent", False))
            if ok:
                chain_state["dispatched"] += 1
                chain_state["actions"].append(action)
            chain_state["readbacks"].append(getattr(rb, "outcome", None))
            return _ChainResult(action, ok, rb, vr.outcome)

    def factory(executor=None, verifier=None):
        if executor is None and verifier is None:
            return bundle._chain
        return _Chain(executor, verifier)

    bundle.factory = factory

    def observe():
        if chain_state["dispatched"] > 0:
            bundle.state["authority"] = "OWNED_BY_PHASE6"
        return FakeBundle.observe(bundle)

    bundle.observe = observe
    bundle.reader = dev.read

    deps = FP.build_field_dependencies(provider_config(), ctx)
    check("FM1 ★★ 由 production provider 建構成功（非從 dispatcher 中間開始）",
          deps is not None)

    # 🔴 先以 provider **自己的** gate 函式產生 permit 所需的前置事實
    #    （不自行捏造 state flags）
    deps.safety_preflight()
    deps.gate_a()
    deps.handoff_readiness()

    holder = FL_HOLDER(deps)
    runner = holder["runner"]
    # 🔴 交接前段（pause / PauseVerifier / ownership / GATE B）由
    #    test_phase6_live_leg_readback.py 與 test_phase6_attempt4_postmortem.py
    #    負責；此處以 Attempt #4 實測到的**已驗證階段**填入同一個
    #    production HandoffRunner 實例，讓 permit 依既有規則評估。
    runner.steps = [
        {"step": "1_PRE_PAUSE_PREFLIGHT", "ok": True, "detail": None},
        {"step": "2_PAUSE", "ok": True, "detail": "已送出，恢復責任成立"},
        {"step": "3_4_PAUSE_VERIFY", "ok": True, "detail": "external_quiescent"},
        {"step": "5_OWNERSHIP", "ok": True, "detail": "NEITHER"},
        {"step": "6_POST_PAUSE_PRECHECK", "ok": True, "detail": None},
    ]
    runner.pause_verifier_state = RA.PV_VERIFIED
    permit = holder["permit"]
    disp = holder["dispatcher"]
    lease = holder["lease"]
    guard = holder["guard"]
    auth = holder["scheduled_auth"]
    check("FM2 ★★ dispatcher 由 production factory 產生且持有同一 permit",
          disp.permit is permit)
    check("FM2b scheduled authorization 綁定同一 permit",
          auth.permit is permit)

    # 走真正的 live leg（只注入 device / operator / clock）
    SVC._live_leg_guard.update(used=False, action=None, final_state=None)
    inside = {}
    orig_build_verifier = PRD.build_verifier

    def patched(mode, leg_, reader, config=None):
        v = orig_build_verifier(mode, leg_, reader, config)
        if v is not None:
            v._clock = clock
            v._sleep = clock.sleep
        return v

    def leg():
        inside["dispatch_enabled"] = HO.DISPATCH_ENABLED
        PRD.build_verifier = patched
        try:
            return SVC.run_live_leg_cli(
                FL.FIRST_LIVE_ACTION, bundle, prompt=auth,
                operator_run=operator_run,
                meter_snapshot_source=(lambda: FakeMeter._Snap()),
                report_session_source=(lambda: None), verbose=False)
        finally:
            PRD.build_verifier = orig_build_verifier

    disp._run_leg = leg
    out = disp()

    print("      actions   = %r" % (chain_state["actions"],))
    print("      readbacks = %r" % (chain_state["readbacks"],))
    print("      outcome   = %r" % (out.get("outcome"),))
    print("      reason    = %r" % (out.get("reason"),))
    _lo = disp.last_live_outcome
    print("      live_leg  = reason=%r confirmed=%r leg_state=%r"
          % (getattr(_lo, "reason", None), getattr(_lo, "confirmed", None),
             getattr(_lo, "leg_state", None)))
    _g = getattr(_lo, "gates", None) or {}
    print("      gates FAIL= %r" % ([k for k, v in _g.items()
                                     if k[0] != "_" and not v],))
    _pc = getattr(_lo, "precheck", None) or {}
    print("      precheck FAIL= %r" % ([k for k, v in _pc.items() if not v],))
    check("FM3 ★★ CHARGE 5.0 kW 恰好一次",
          chain_state["actions"].count("charge") == 1
          and all(k.get("power") == CHARGE_KW for k in sent
                  if k["action"] == "pcs_charge"))
    check("FM4 ★★ DISCHARGE = 0", "discharge" not in chain_state["actions"])
    check("FM5 ★★ readback PASS",
          chain_state["readbacks"][0] == EXC.VERIFY_SUCCESS)
    check("FM6 ★★ LEG_COMPLETE",
          FL.leg_reached_complete(disp.last_live_outcome) is True)
    check("FM7 ★★ dispatcher outcome = DISPATCH_SUCCEEDED",
          out.get("outcome") == FL.DISPATCH_SUCCEEDED)
    check("FM8 ★★ live leg 期間 DISPATCH_ENABLED = True",
          inside.get("dispatch_enabled") is True)
    check("FM9 ★★ 結束後 dispatch flags reset", rest_ok())
    check("FM10 ★★ permit consumed", permit.state == FL.PERMIT_EXITED)
    check("FM11 ★★ scheduled authorization consumed", auth.used is True)
    check("FM12 ★★ authority released",
          out.get("released") is True and lease.held is False)
    c = guard.as_dict()
    check("FM13 ★★ charge 1 / discharge 0 / 無違規",
          c["counts"].get("phase6_charge") == 1
          and c["counts"].get("phase6_discharge", 0) == 0
          and c["violations"] == [])
    check("FM14 ★★ PCS STOP <= 1",
          c["counts"].get("phase6_pcs_stop", 0) <= 1)


def FL_HOLDER(deps):
    """
    取出 provider 內部 holder（permit / dispatcher / lease / guard / auth）。

    🔴 透過 provider **自己**的 `arm_and_run_handoff` 建構流程取得 ——
       不自行重建任何一個物件。
    """
    captured = {}
    orig = HO.HandoffRunner.run

    def stub(self):
        captured["runner"] = self
        return {"outcome": "ABORTED", "state": "ABORTED_PREFLIGHT",
                "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT", "ok": False,
                           "detail": "construction smoke: 不執行"}]}

    HO.HandoffRunner.run = stub
    try:
        deps.arm_and_run_handoff()
    finally:
        HO.HandoffRunner.run = orig
    r = captured["runner"]
    return {"runner": r, "permit": r.dispatch_permit,
            "dispatcher": r.dispatcher,
            "lease": r.dispatcher.lease, "guard": r.dispatcher.guard,
            "scheduled_auth": _find_auth(r.dispatcher)}


def _find_auth(dispatcher):
    """由 dispatcher 的 live_leg_runner closure 取回 provider 的 auth 物件。"""
    fn = dispatcher._run_leg
    for cell in (fn.__closure__ or ()):
        v = cell.cell_contents
        if isinstance(v, FL.ScheduledLiveLegAuthorization):
            return v
    # production 的 lambda 巢狀較深，逐層找
    for cell in (fn.__closure__ or ()):
        v = cell.cell_contents
        if callable(v) and getattr(v, "__closure__", None):
            for c2 in v.__closure__:
                if isinstance(c2.cell_contents, FL.ScheduledLiveLegAuthorization):
                    return c2.cell_contents
    raise AssertionError("找不到 ScheduledLiveLegAuthorization")


# ======================================================================
def main():
    print("=" * 72)
    print("  REAL PROVIDER CONSTRUCTION COVERAGE")
    print("=" * 72)
    for fn in (test_F_factory_contract, test_PC_real_construction,
               test_A6R_regression, test_FM_full_mock):
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
