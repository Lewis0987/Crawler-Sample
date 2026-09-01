# -*- coding: utf-8 -*-
"""
test_phase6_69a_cli_entry.py — Phase 6.9-A CLI Entry Wiring（A~P）
======================================================================
核心命題
    「`--live-leg` 真的能走進 ControlledLiveLeg，而且**只有**在
      人工確認 → fresh read → fresh 自然 Decision → fresh precheck /
      Safety / Authority / Interlock 全數通過之後才走得進去。
      任何一步失敗 → 不建立控制出口、operator 呼叫數 0。」

與既有 94 項的關係
    既有 test_phase6_69_controlled_live.py 驗的是**機制本身**擋不擋得住；
    本檔驗的是 **entry point 有沒有真的接上**，以及順序是否正確。

是否需要設備
    **不需要**。Fake operator / Fake reader / Fake meter，零網路、零實機 command。
    控制請求會真的穿過 LegBoundExecutor → PcsControlExecutor → Fake operator，
    因此「呼叫次數」是實際可數的，不是宣稱的。

用法
    python test_phase6_69a_cli_entry.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import control_authority as CA                          # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402
import pcs_auto_control_production as PRD                # noqa: E402

RESULTS = []
CONF = PRD.confirmation_phrase(PRD.LiveLegIntent("charge"))      # CONFIRM CHARGE 5KW


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def _src(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


# ======================================================================
# 測試替身
# ======================================================================
class Op(object):
    """Fake operator：只記錄呼叫，永遠不碰設備。"""

    def __init__(self):
        self.calls = []

    def __call__(self, action=None, execute=None, power=None, **kw):
        self.calls.append({"action": action, "execute": execute, "power": power})
        return {"control_success": True, "action": action}

    @property
    def actions(self):
        return [c["action"] for c in self.calls]

    @property
    def powers(self):
        return [c["power"] for c in self.calls]


class Arb(object):
    """
    仲裁結果替身 —— 預設**全部通過**，各測試只覆寫要打壞的那一項。
    欄位名稱與 ArbitrationResult 一致（gates / precheck 都由此讀取）。
    """

    def __init__(self, **over):
        self.fresh_decision_action = "charge"
        self.fresh_decision_target_kw = 5.0
        self.safety_allowed = True
        self.safety_reason = None
        self.authority_state = CA.AUTH_IDLE
        self.direction_reason = PCI.DIR_FROM_IDLE
        self.pcs_state = "STANDBY"
        self.soc_percent = 40.0
        self.valid = True
        self.pcs_fault = False
        self.active_alarm_count = 0
        self.alarm_source_complete = True
        self.schedule_switch = 0
        self.manual_switch = 1
        self.tou_state = "OFF_PEAK"
        for k, v in over.items():
            setattr(self, k, v)


class ObsResult(object):
    """ExecutionResult 替身（觀測用；executor 未接，結構上送不出指令）。"""

    def __init__(self, arb=None):
        self.arbitration = arb if arb is not None else Arb()
        self.executed = False
        self.control_action = "none"


class Snap(object):
    """MeterSnapshot 替身。"""

    def __init__(self, valid=True, stale=False, age_sec=1.0):
        self.valid, self.stale, self.age_sec = valid, stale, age_sec
        self.meter_state = "ok"


class Ctrl(object):
    """ControlRequest 替身（只帶 executor 真正會用到的兩個欄位）。"""

    def __init__(self, action, power):
        self.action = action
        self.target_power_kw = power


class LegRes(object):
    """ControlledLiveLeg 消費的執行結果。"""

    def __init__(self, action, executed, readback, lc):
        self.control_action = action
        self.executed = executed
        self.readback_outcome = readback
        self.lastcontrol_written = lc
        self.outcome = "VERIFIED" if executed else "NOT_SENT"
        self.reason = None


class WiredChain(object):
    """
    執行鏈替身，但**真的**把控制請求送進注入的 executor。

    🔴 因此 operator 呼叫數不是替身自己宣稱的，而是實際穿過
       LegBoundExecutor → PcsControlExecutor → Fake operator 的結果。
    """

    def __init__(self, actions, executor, readback="VERIFY_SUCCESS", lc=True):
        self.actions = list(actions)
        self.i = 0
        self.executor = executor
        self.readback = readback
        self.lc = lc

    def run(self):
        act = self.actions[min(self.i, len(self.actions) - 1)]
        self.i += 1
        power = (None if act == "stop"
                 else CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw)
        if self.executor is None:
            return LegRes(act, False, None, False)
        r = self.executor.send(Ctrl(act, power))
        sent = bool(getattr(r, "sent", False))
        return LegRes(act, sent, self.readback if sent else None,
                      self.lc if sent else False)


def make_bundle(op, arb=None, chain_actions=("charge", "stop"),
                readback="VERIFY_SUCCESS", lc=True,
                authority_after=CA.AUTH_OWNED):
    """
    組出一個 LiveChainBundle：
      · observe()          → 無出口的觀測（fresh 閘門判定用）
      · factory(e, v)      → 真的把請求送進 e 的鏈
    dispatch 之後的 observe 需回報 OWNED，才可能進入 STOP 收尾。
    """
    state = {"dispatched": False}

    def observe():
        if state["dispatched"]:
            return ObsResult(Arb(authority_state=authority_after))
        return ObsResult(arb if arb is not None else Arb())

    def factory(executor=None, verifier=None):
        if executor is not None:
            state["dispatched"] = True
        return WiredChain(chain_actions, executor, readback=readback, lc=lc)

    return SVC.LiveChainBundle(factory, observe, reader=object(), store=None)


def run_cli(action="charge", answer=CONF, op=None, arb=None, snap=None,
            session=None, **kw):
    """跑一次 CLI LIVE 路徑（每次先清掉 process 級 one-shot 記錄）。"""
    SVC.reset_live_leg_guard()
    op = op if op is not None else Op()
    bundle = make_bundle(op, arb=arb, **kw)
    out = SVC.run_live_leg_cli(
        action, bundle, prompt=(lambda _p: answer), operator_run=op,
        meter_snapshot_source=(lambda: snap if snap is not None else Snap()),
        report_session_source=(lambda: session), verbose=False)
    return out, op


# ======================================================================
# A. 無 --live-leg → OBSERVE_ONLY
# ======================================================================
def test_a():
    print("\n[A] 無 --live-leg → OBSERVE_ONLY，出口不存在")
    _o, _r, wiring = SVC.build_production_stack(client=None, meter_client=None)
    check("mode = OBSERVE_ONLY", wiring.mode == PRD.MODE_OBSERVE_ONLY)
    check("can_dispatch = False", wiring.can_dispatch is False)
    check("executor 未接", wiring.sources.get("executor") == "NOT_WIRED")
    check("verifier 未接", wiring.sources.get("verifier") == "NOT_WIRED")
    check("build_executor(OBSERVE_ONLY) → None",
          PRD.build_executor(PRD.MODE_OBSERVE_ONLY) is None)
    check("build_verifier(OBSERVE_ONLY) → None",
          PRD.build_verifier(PRD.MODE_OBSERVE_ONLY) is None)
    # main() 沒有 --live-leg 時不得走進 LIVE 分支
    tree = ast.parse(_src("pcs_auto_control_service.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [c.func.id for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    check("main() 只在一處呼叫 live_leg_main",
          calls.count("live_leg_main") == 1)
    check("main() 不直接建立控制出口",
          "build_executor" not in calls and "build_verifier" not in calls)


# ======================================================================
# B. --live-leg 無 --confirm → return 2、不建立出口
# ======================================================================
def test_b():
    print("\n[B] --live-leg 無 --confirm → return 2")
    SVC.reset_live_leg_guard()
    for act in ("charge", "discharge"):
        check(f"--live-leg {act}（無 --confirm）→ 2",
              SVC.main(["--live-leg", act]) == 2)
    check("未核發任何 leg 授權", SVC.live_leg_guard_state()["used"] is False)


# ======================================================================
# C. 錯誤 confirmation → 不建立出口
# ======================================================================
def test_c():
    print("\n[C] confirmation 字串錯誤 → 不建立出口、0 calls")
    for bad in ("", "y", "YES", "confirm charge 5kw", "CONFIRM CHARGE",
                "CONFIRM CHARGE 5KW ！", "CONFIRM DISCHARGE 5KW"):
        out, op = run_cli(answer=bad)
        ok = (out.reason == SVC.LIVE_R_CONFIRM_FAILED
              and out.executor_built is False and not op.calls)
        check(f"確認字串 {bad!r} → 拒絕且 0 calls", ok)
    out, op = run_cli(answer=f"  {CONF}  ")
    check("完整字串（含前後空白）→ 接受", out.confirmed is True)


# ======================================================================
# D. 確認正確但 Decision=IDLE → 0 operator calls
# ======================================================================
def test_d():
    print("\n[D] Decision=IDLE → NO DISPATCH")
    out, op = run_cli(arb=Arb(fresh_decision_action="idle"))
    check("reason = DECISION_NOT_LEG_ACTION",
          out.reason == SVC.LIVE_R_DECISION_NOT_LEG)
    check("operator 呼叫 0 次", len(op.calls) == 0)
    check("未建立 executor", out.executor_built is False)
    check("人工確認確實已通過（不是被確認擋掉的）", out.confirmed is True)


# ======================================================================
# E. 確認正確但 Decision=DISCHARGE（leg=charge）→ 0 calls
# ======================================================================
def test_e():
    print("\n[E] leg=charge 但 Decision=discharge → NO DISPATCH")
    out, op = run_cli(arb=Arb(fresh_decision_action="discharge"))
    check("reason = DECISION_NOT_LEG_ACTION",
          out.reason == SVC.LIVE_R_DECISION_NOT_LEG)
    check("operator 呼叫 0 次", len(op.calls) == 0)
    check("未建立 executor", out.executor_built is False)
    # 反向亦然
    out2, op2 = run_cli(action="discharge", answer="CONFIRM DISCHARGE 5KW",
                        arb=Arb(fresh_decision_action="charge"))
    check("leg=discharge 但 Decision=charge → 0 calls",
          out2.reason == SVC.LIVE_R_DECISION_NOT_LEG and not op2.calls)
    check("no_action 也不算 charge",
          run_cli(arb=Arb(fresh_decision_action="no_action"))[1].calls == [])
    check("None 也不算 charge",
          run_cli(arb=Arb(fresh_decision_action=None))[1].calls == [])


# ======================================================================
# F. Decision=CHARGE 但 fresh precheck FAIL → 0 calls
# ======================================================================
def test_f():
    print("\n[F] Decision=CHARGE 但 precheck FAIL → 0 calls")
    broken = {
        "ess_comm_ok": {"valid": False},
        "pcs_idle": {"pcs_state": "CHARGING"},
        "soc_sane": {"soc_percent": None},
        "no_pcs_fault": {"pcs_fault": True},
        "no_active_alarm": {"active_alarm_count": 3},
        "alarm_source_complete": {"alarm_source_complete": False},
        "schedule_off": {"schedule_switch": 1},
        "manual_switch_ok": {"manual_switch": 0},
        "tou_valid": {"tou_state": "UNKNOWN"},
    }
    for item, over in broken.items():
        out, op = run_cli(arb=Arb(**over))
        ok = (out.reason == SVC.LIVE_R_PRECHECK_FAILED
              and not op.calls and out.executor_built is False)
        check(f"precheck {item} FAIL → 0 calls", ok)
    # 電表面向
    for tag, snap in (("meter invalid", Snap(valid=False)),
                      ("meter stale", Snap(stale=True))):
        out, op = run_cli(snap=snap)
        check(f"{tag} → 0 calls",
              out.reason == SVC.LIVE_R_PRECHECK_FAILED and not op.calls)
    out, op = run_cli(session=object())
    check("已有 report session → 0 calls",
          out.reason == SVC.LIVE_R_PRECHECK_FAILED and not op.calls)


# ======================================================================
# G. Safety BLOCK → 0 calls
# ======================================================================
def test_g():
    print("\n[G] Safety BLOCK → 0 calls")
    for tag, val in (("BLOCK", False), ("未評估(None)", None)):
        out, op = run_cli(arb=Arb(safety_allowed=val,
                                  safety_reason="SOC_OUT_OF_RANGE"))
        check(f"safety_allowed={tag} → 不 dispatch",
              out.reason == SVC.LIVE_R_SAFETY_BLOCKED and not op.calls)
        check(f"safety_allowed={tag} → 未建立出口", out.executor_built is False)


# ======================================================================
# H. Authority BLOCK → 0 calls
# ======================================================================
def test_h():
    print("\n[H] Authority 非 IDLE → 0 calls")
    for st in (CA.AUTH_OWNED, CA.AUTH_EXTERNAL, CA.AUTH_CONFLICT,
               CA.AUTH_UNKNOWN, None, "OWNERSHIP_PENDING"):
        out, op = run_cli(arb=Arb(authority_state=st))
        check(f"authority={st} → 不 dispatch",
              out.reason == SVC.LIVE_R_AUTHORITY_BLOCKED and not op.calls)
    check("IDLE 是唯一可接受的起點",
          SVC.LIVE_AUTHORITY_OK == (CA.AUTH_IDLE,))


# ======================================================================
# I. Interlock BLOCK → 0 calls
# ======================================================================
def test_i():
    print("\n[I] 方向互鎖 BLOCK → 0 calls")
    for r in sorted(PCI.DIRECTION_BLOCK_REASONS):
        out, op = run_cli(arb=Arb(direction_reason=r))
        check(f"interlock={r} → 不 dispatch",
              out.reason == SVC.LIVE_R_INTERLOCK_BLOCKED and not op.calls)
    out, op = run_cli(arb=Arb(direction_reason=None))
    check("interlock 未評估(None) → Fail Closed",
          out.reason == SVC.LIVE_R_INTERLOCK_BLOCKED and not op.calls)


# ======================================================================
# J. 全部 PASS → CLI 確實進入 ControlledLiveLeg
# ======================================================================
def test_j():
    print("\n[J] 全數通過 → 真的進入 ControlledLiveLeg")
    out, op = run_cli()
    check("runner 已建立", isinstance(out.runner, SVC.ControlledLiveLeg))
    check("executor / verifier 皆已建立",
          out.executor_built is True and out.verifier_built is True)
    check("走完狀態機到 OBSERVE_ONLY", out.leg_state == SVC.LEG_OBSERVE_ONLY)
    hist = out.runner.history
    check("狀態歷程含 DISPATCHED", SVC.LEG_DISPATCHED in hist)
    check("狀態歷程含 OWNED", SVC.LEG_OWNED in hist)
    check("狀態歷程含 IDLE_VERIFIED", SVC.LEG_IDLE_VERIFIED in hist)
    check("狀態歷程含 REPORT_FINALIZED", SVC.LEG_REPORT_FINALIZED in hist)
    check("狀態歷程含 COMPLETE", SVC.LEG_COMPLETE in hist)


# ======================================================================
# K. 正常 fake CHARGE leg → 恰好 1 CHARGE + 1 STOP
# ======================================================================
def test_k():
    print("\n[K] 正常 leg → 恰好 1 方向指令 + 1 STOP")
    out, op = run_cli()
    check("operator 共 2 次呼叫", len(op.calls) == 2)
    check("第 1 次是充電", op.actions[0] == "pcs_charge")
    check("第 2 次是停止", op.actions[1] == "pcs_stop_power")
    check("CHARGE 恰好 1 次", op.actions.count("pcs_charge") == 1)
    check("STOP 恰好 1 次", op.actions.count("pcs_stop_power") == 1)
    check("dispatch_count = 1", out.dispatch_count == 1)
    check("stop_count = 1", out.stop_count == 1)
    check("execute 皆為 True", all(c["execute"] is True for c in op.calls))
    # 放電方向對稱
    out2, op2 = run_cli(action="discharge", answer="CONFIRM DISCHARGE 5KW",
                        arb=Arb(fresh_decision_action="discharge"),
                        chain_actions=("discharge", "stop"))
    check("discharge leg 亦為 1+1",
          op2.actions == ["pcs_discharge", "pcs_stop_power"])


# ======================================================================
# L. leg 完成 → 自動回 OBSERVE_ONLY、出口銷毀
# ======================================================================
def test_l():
    print("\n[L] leg 完成 → 自動回 OBSERVE_ONLY")
    out, op = run_cli()
    r = out.runner
    check("最終狀態 OBSERVE_ONLY", r.state == SVC.LEG_OBSERVE_ONLY)
    check("executor 已銷毀", r.executor is None)
    check("verifier 已銷毀", r.verifier is None)
    check("leg 授權已 consumed", out.runner.leg.state == PRD.LEG_CONSUMED)
    check("DISPATCH_ENABLED 仍為 False", SVC.RT.DISPATCH_ENABLED is False)
    _o, _rc, wiring = SVC.build_production_stack(client=None, meter_client=None)
    check("後續 stack 仍為 OBSERVE_ONLY / can_dispatch=False",
          wiring.mode == PRD.MODE_OBSERVE_ONLY and wiring.can_dispatch is False)


# ======================================================================
# M. restart → 不恢復 LIVE
# ======================================================================
def test_m():
    print("\n[M] restart → 不恢復 LIVE")
    src = _src("pcs_auto_control_service.py")
    tree = ast.parse(src)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("不讀環境變數（getenv/environ 皆未出現）",
          "getenv" not in names and "environ" not in names)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_live_leg_cli")
    fnames = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    check("LIVE 路徑不落盤（無 dump/write/save）",
          not ({"dump", "write", "save", "writelines"} & fnames))
    check("授權不持久化：guard 只是記憶體 dict",
          isinstance(SVC.live_leg_guard_state(), dict))
    # 新 process = 全新 guard；以 reset 模擬重啟後的初始狀態
    SVC.reset_live_leg_guard()
    check("重啟後 guard 回到未使用",
          SVC.live_leg_guard_state()["used"] is False)
    _o, _rc, wiring = SVC.build_production_stack(client=None, meter_client=None)
    check("重啟預設 OBSERVE_ONLY", wiring.mode == PRD.MODE_OBSERVE_ONLY)
    # production 路徑不得呼叫測試專用的 reset
    for name in ("main", "live_leg_main", "run_live_leg_cli"):
        f = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name)
        calls = {c.func.id for c in ast.walk(f)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        check(f"{name}() 未呼叫 reset_live_leg_guard",
              "reset_live_leg_guard" not in calls)


# ======================================================================
# 增補：one-shot（裁示第八節）
# ======================================================================
def test_one_shot():
    print("\n[one-shot] 同一 process 不得再次核發 leg 授權")
    op = Op()
    SVC.reset_live_leg_guard()
    bundle = make_bundle(op)
    first = SVC.run_live_leg_cli("charge", bundle, prompt=lambda _p: CONF,
                                 operator_run=op,
                                 meter_snapshot_source=lambda: Snap(),
                                 verbose=False)
    check("第 1 次成功", first.dispatch_count == 1 and first.stop_count == 1)
    n_after_first = len(op.calls)

    bundle2 = make_bundle(op)
    second = SVC.run_live_leg_cli("charge", bundle2, prompt=lambda _p: CONF,
                                  operator_run=op,
                                  meter_snapshot_source=lambda: Snap(),
                                  verbose=False)
    check("第 2 次 CHARGE 被拒", second.reason == SVC.LIVE_R_ALREADY_USED)
    check("第 2 次未再呼叫 operator", len(op.calls) == n_after_first)
    check("第 2 次未進入確認流程", second.confirmed is False)

    third = SVC.run_live_leg_cli("discharge", bundle2,
                                 prompt=lambda _p: "CONFIRM DISCHARGE 5KW",
                                 operator_run=op,
                                 meter_snapshot_source=lambda: Snap(),
                                 verbose=False)
    check("改送 DISCHARGE 也被拒", third.reason == SVC.LIVE_R_ALREADY_USED)
    check("仍未再呼叫 operator", len(op.calls) == n_after_first)


# ======================================================================
# 增補：確認順序（裁示第六節）
# ======================================================================
def test_order():
    print("\n[order] 人工確認 → fresh read → 閘門 → 授權 → 出口")
    seq = []
    op = Op()
    SVC.reset_live_leg_guard()
    inner = make_bundle(op)

    def observe():
        seq.append("fresh_read")
        return inner.observe()

    def factory(executor=None, verifier=None):
        if executor is not None:
            seq.append("chain_with_exit")
        return inner.factory(executor, verifier)

    def prompt(_p):
        seq.append("confirm")
        return CONF

    bundle = SVC.LiveChainBundle(factory, observe, reader=object(), store=None)
    orig_auth, orig_exec = PRD.LiveLegAuthorization, PRD.build_executor

    class TracedAuth(orig_auth):
        def __init__(self, *a, **k):
            seq.append("authorization")
            orig_auth.__init__(self, *a, **k)

    def traced_exec(*a, **k):
        seq.append("build_executor")
        return orig_exec(*a, **k)

    PRD.LiveLegAuthorization, PRD.build_executor = TracedAuth, traced_exec
    try:
        SVC.run_live_leg_cli("charge", bundle, prompt=prompt, operator_run=op,
                             meter_snapshot_source=lambda: Snap(), verbose=False)
    finally:
        PRD.LiveLegAuthorization, PRD.build_executor = orig_auth, orig_exec

    check(f"順序 = {seq}",
          seq[:5] == ["confirm", "fresh_read", "authorization",
                      "build_executor", "chain_with_exit"])
    check("確認發生在 fresh read 之前", seq.index("confirm") < seq.index("fresh_read"))
    check("fresh read 發生在核發授權之前",
          seq.index("fresh_read") < seq.index("authorization"))
    check("授權發生在建立出口之前",
          seq.index("authorization") < seq.index("build_executor"))
    check("出口發生在帶出口的執行鏈之前",
          seq.index("build_executor") < seq.index("chain_with_exit"))

    # 確認失敗 → 完全不 fresh read、不核發、不建立出口
    seq2 = []
    SVC.reset_live_leg_guard()
    b2 = SVC.LiveChainBundle(
        (lambda e=None, v=None: (seq2.append("chain"), inner.factory(e, v))[1]),
        (lambda: (seq2.append("fresh_read"), inner.observe())[1]),
        reader=object(), store=None)
    SVC.run_live_leg_cli("charge", b2, prompt=lambda _p: "YES",
                         operator_run=op, meter_snapshot_source=lambda: Snap(),
                         verbose=False)
    check("確認失敗 → 完全沒有 fresh read", "fresh_read" not in seq2)


# ======================================================================
# N. CLI 沒有 --power
# ======================================================================
def test_n():
    print("\n[N] CLI 沒有 --power")
    tree = ast.parse(_src("pcs_auto_control_service.py"))
    opts = set()
    for c in ast.walk(tree):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "add_argument"):
            for a in c.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    opts.add(a.value)
    check(f"CLI 選項 = {sorted(opts)}", "--power" not in opts)
    check("--live-leg 存在", "--live-leg" in opts)
    check("--confirm 存在", "--confirm" in opts)
    check("argparse 直接拒絕 --power", _rejects(["--live-leg", "charge",
                                                 "--confirm", "--power", "150"]))
    check("argparse 直接拒絕 --live-leg stop",
          _rejects(["--live-leg", "stop", "--confirm"]))


def _rejects(argv):
    try:
        SVC.main(argv)
    except SystemExit as e:
        return e.code != 0
    return False


# ======================================================================
# O. 150 kW 無法成為 live target
# ======================================================================
def test_o():
    print("\n[O] 150 kW 不可能成為 live target")
    op = Op()
    out, op = run_cli(op=op)
    check("送出功率皆為 5.0 kW（stop 不帶功率）",
          [c["power"] for c in op.calls] == [5.0, None])
    check("沒有任何一次送出 150", 150 not in op.powers and 150.0 not in op.powers)
    check("max_power_kw 只是 Safety 上限，非操作目標",
          CFG.DEFAULT_CONTROL_CONFIG.max_power_kw == 150.0
          and CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0)
    # 授權與意向都不接受功率參數
    for cls in (PRD.LiveLegAuthorization, PRD.LiveLegIntent):
        try:
            cls("charge", 150.0)
            ok = False
        except (TypeError, ValueError, AttributeError):
            ok = True
        except Exception:
            ok = True
        check(f"{cls.__name__} 不接受功率覆寫", ok)
    tree = ast.parse(_src("pcs_auto_control_production.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "leg_power_kw")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    check("功率唯一來源是 config 的兩個欄位",
          attrs >= {"charge_power_kw", "discharge_power_kw"})
    nums = {n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))}
    check(f"leg_power_kw 內無任何功率字面值（{sorted(nums)}）", not nums)


# ======================================================================
# P. Production power 固定 5.0 kW
# ======================================================================
def test_p():
    print("\n[P] Production power 固定 5.0 kW")
    check("charge 意向 = 5.0", PRD.LiveLegIntent("charge").power_kw == 5.0)
    check("discharge 意向 = 5.0", PRD.LiveLegIntent("discharge").power_kw == 5.0)
    check("charge 授權 = 5.0", PRD.LiveLegAuthorization("charge").power_kw == 5.0)
    check("確認語句 = CONFIRM CHARGE 5KW", CONF == "CONFIRM CHARGE 5KW")
    check("放電確認語句 = CONFIRM DISCHARGE 5KW",
          PRD.confirmation_phrase(PRD.LiveLegIntent("discharge"))
          == "CONFIRM DISCHARGE 5KW")
    # 意向與授權共用同一個解析點 → 不可能顯示 A 送出 B
    check("意向與授權功率一致",
          PRD.LiveLegIntent("charge").power_kw
          == PRD.LiveLegAuthorization("charge").power_kw)
    check("意向 armed 恆為 False", PRD.LiveLegIntent("charge").armed is False)
    check("意向誤傳進 build_executor → None（Fail Closed）",
          PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG,
                             PRD.LiveLegIntent("charge"), lambda **k: None) is None)
    check("意向誤傳進 build_verifier → None",
          PRD.build_verifier(PRD.MODE_CONTROLLED_SINGLE_LEG,
                             PRD.LiveLegIntent("charge"), object()) is None)


# ======================================================================
# 增補：operator 未注入 / 出口建立失敗
# ======================================================================
def test_no_operator():
    print("\n[extra] operator 未注入 → 不建立出口、不 dispatch")
    SVC.reset_live_leg_guard()
    op = Op()
    # ⚠️ 刻意**不**呼叫 run_live_leg_cli(operator_run=None)：那會走到
    #    build_live_operator_run() 而真的 import device_control_operator。
    #    這裡直接驗 builder 契約 —— 同樣證明「沒有 operator 就沒有出口」。
    leg = PRD.LiveLegAuthorization("charge")
    check("operator_run=None → executor 為 None",
          PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG, leg, None) is None)
    check("reader=None → verifier 為 None",
          PRD.build_verifier(PRD.MODE_CONTROLLED_SINGLE_LEG, leg, None) is None)
    check("已 abort 的 leg → executor 為 None",
          PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG,
                             _aborted_leg(), lambda **k: None) is None)
    check("operator 全程 0 次呼叫", len(op.calls) == 0)


def _aborted_leg():
    leg = PRD.LiveLegAuthorization("charge")
    leg.abort("test")
    return leg


# ======================================================================
# 增補：模組層永不 import operator
# ======================================================================
def test_no_module_level_operator():
    print("\n[extra] 模組層永不 import operator")
    tree = ast.parse(_src("pcs_auto_control_service.py"))
    top = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            top |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            top.add(n.module or "")
    check(f"模組層 import 不含 operator（{sorted(top)}）",
          not any("operator" in m for m in top))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build_live_operator_run")
    inner = {a.name for c in ast.walk(fn) if isinstance(c, ast.Import)
             for a in c.names}
    check("operator 只在 build_live_operator_run 內部 import",
          any("operator" in m for m in inner))
    prod = ast.parse(_src("pcs_auto_control_production.py"))
    ptop = set()
    for n in prod.body:
        if isinstance(n, ast.Import):
            ptop |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            ptop.add(n.module or "")
    check("production 模組層亦不 import operator",
          not any("operator" in m for m in ptop))


def main():
    print("=" * 70)
    print("  Phase 6.9-A CLI Entry Wiring —— A~P（完全離線，零實機 command）")
    print("=" * 70)
    for fn in (test_a, test_b, test_c, test_d, test_e, test_f, test_g,
               test_h, test_i, test_j, test_k, test_l, test_m,
               test_one_shot, test_order, test_n, test_o, test_p,
               test_no_operator, test_no_module_level_operator):
        fn()
    SVC.reset_live_leg_guard()
    total, bad = len(RESULTS), RESULTS.count(False)
    print("\n" + "=" * 70)
    print(f"  結果：{total - bad}/{total} PASS"
          + ("" if not bad else f"   ❌ {bad} FAIL"))
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
