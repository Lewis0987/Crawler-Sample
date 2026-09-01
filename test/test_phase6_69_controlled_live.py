# -*- coding: utf-8 -*-
"""
test_phase6_69_controlled_live.py — Phase 6.9 Controlled Single-Leg LIVE（A~T）
======================================================================
核心命題
    「`--live-leg` 只代表『本 process 最多允許做一次這件事』，
      **不代表立即動作**；缺任何一道守門一律 NO DISPATCH。
      一個 leg 做完就自動回 OBSERVE_ONLY，且授權永久失效。」

是否需要設備
    **不需要**。全程 Fake executor / Fake reader，零網路、零實機 command。

用法
    python test_phase6_69_controlled_live.py        # exit 0 = PASS
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
import pcs_control_executor as EXC                       # noqa: E402
import production_execution_chain as PEC                 # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402
import pcs_auto_control_production as PRD                # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def _tree(name):
    return ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())


# ======================================================================
# 測試替身
# ======================================================================
class Op(object):
    """Fake operator：只記錄呼叫，永遠不碰設備。"""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def __call__(self, action=None, execute=None, power=None, **kw):
        self.calls.append({"action": action, "execute": execute, "power": power})
        return {"ok": True, "action": action}

    @property
    def actions(self):
        return [c["action"] for c in self.calls]


class Res(object):
    """ExecutionResult 的最小替身。"""

    def __init__(self, action, executed=True, readback="VERIFY_SUCCESS",
                 lc=True, outcome="VERIFIED"):
        self.control_action = action
        self.executed = executed
        self.readback_outcome = readback
        self.lastcontrol_written = lc
        self.outcome = outcome


class Chain(object):
    """
    依序吐出各步結果的執行鏈替身。
    每次 run() 會呼叫一次 fake operator —— 讓「呼叫次數」是真的可數的。
    """

    def __init__(self, steps, op, leg=None):
        self.steps = list(steps)
        self.i = 0
        self.op = op
        # 🔴 fixture 前提修正（Phase 6.9-A）
        #    真實路徑上 executor 一定是 LegBoundExecutor，它在放行並成功送出後
        #    會 `leg.mark_dispatched(action)`。原本的替身沒有做這件事，等於在
        #    模擬一個「不是 leg-bound」的 executor —— 那不是 production 會出現
        #    的形態，也讓 run_leg 的事後檢查測不到真正的行為。
        self.leg = leg

    def run(self):
        r = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        if r.executed:
            self.op(action=r.control_action, execute=True,
                    power=CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw)
            if self.leg is not None:
                self.leg.mark_dispatched(r.control_action)
        return r


class Arb(object):
    def __init__(self, authority_state="OWNED_BY_PHASE6"):
        self.authority_state = authority_state


def leg_runner(action, steps, op, authority_after="OWNED_BY_PHASE6",
               config=None):
    """組出一個已 arm 的 ControlledLiveLeg。"""
    leg = PRD.LiveLegAuthorization(action, config=config)
    chain = Chain(steps, op, leg)
    runner = SVC.ControlledLiveLeg(
        leg, chain_factory=(lambda e, v: chain),
        observe_source=(lambda: Arb(authority_after)))
    runner.arm(executor=object(), verifier=object())
    return runner, leg


OK_CHARGE = [Res("charge"), Res("stop")]
OK_DISCHARGE = [Res("discharge"), Res("stop")]


# ======================================================================
def main():
    print("== Phase 6.9 Controlled Single-Leg LIVE 驗證（完全離線）==\n")

    # ---------------- A. 無 --live-leg ----------------
    print("A. 沒有 --live-leg → 永遠 OBSERVE_ONLY")
    obs, _rec, wiring = SVC.build_production_stack()
    check("★★ A. 模式仍為 OBSERVE_ONLY",
          wiring.mode == PRD.MODE_OBSERVE_ONLY)
    check("★★ A. executor / verifier 皆為 None",
          PRD.build_executor() is None and PRD.build_verifier() is None)
    check("★★ A. can_dispatch = False 且 DISPATCH_ENABLED = False",
          wiring.can_dispatch is False and RT.DISPATCH_ENABLED is False)
    check("★★ A. 跑一輪：未執行、無 operator 結果",
          (lambda r: r.executed is False and r.dispatch_count_delta == 0
           and r.operator_outcome is None)(obs()))
    check("★★ A. --live-leg 未搭配 --confirm → 直接中止（回傳 2）",
          SVC.main(["--live-leg", "charge"]) == 2)

    # ---------------- B/C. 方向授權 ----------------
    print("\nB/C. charge / discharge 授權各自只允許自己的方向")
    lc = PRD.LiveLegAuthorization("charge")
    ld = PRD.LiveLegAuthorization("discharge")
    check("★★ B. charge 授權：允許 charge、拒絕 discharge",
          lc.allows("charge") is True and lc.allows("discharge") is False)
    check("★★ C. discharge 授權：允許 discharge、拒絕 charge",
          ld.allows("discharge") is True and ld.allows("charge") is False)
    check("★★ B/C. 未送方向指令前不允許 STOP（STOP 是收尾，不是起手）",
          lc.allows("stop") is False and ld.allows("stop") is False)
    check("★★ B/C. 非法 action 一律拒絕建立授權",
          (lambda: _raises(lambda: PRD.LiveLegAuthorization("idle")))())

    # ---------------- D. action mismatch ----------------
    print("\nD. 決策方向與 leg 授權不符 → 0 calls")
    op = Op()
    r, leg = leg_runner("charge", [Res("discharge")], op)
    st = r.run_leg()
    check("★★ D. 授權 charge 但決策 discharge → ABORTED",
          st == SVC.LEG_ABORTED and "不符" in (r.abort_reason or ""))
    check("★★ D. 出口已銷毀",
          r.executor is None and r.verifier is None)
    # 🔴 真正的保證在 executor 層：leg 授權在**送出之前**就攔下，
    #    operator 完全不會被呼叫（若只在 chain.run() 之後才檢查，指令已經出去了）。
    op2 = Op()
    lg = PRD.LiveLegAuthorization("charge")
    ex = PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG, lg, op2)
    check("★★ D. executor 為 leg-bound 包裝",
          isinstance(ex, PRD.LegBoundExecutor))

    class _Ctrl(object):
        action, target_power_kw = "discharge", 5.0
    res = ex.send(_Ctrl())
    check("★★ D. 方向不符 → 未送出、operator 0 calls",
          res.sent is False and res.reason == PRD.R_LEG_NOT_AUTHORIZED
          and op2.calls == [])
    check("★★ D. 被拒絕的動作有留下稽核紀錄", ex.refusals == ["discharge"])

    # ---------------- E. one-shot ----------------
    print("\nE. one-shot：第二次 dispatch 0 calls")
    op = Op()
    r, leg = leg_runner("charge", OK_CHARGE, op)
    check("  E. 第一次完整跑完", r.run_leg() == SVC.LEG_OBSERVE_ONLY)
    check("★★ E. 授權已 consumed", leg.state == PRD.LEG_CONSUMED)
    before = len(op.calls)
    st2 = r.run_leg()
    check("★★ E. 第二次呼叫 → ABORTED 且 operator 沒有新增呼叫",
          st2 in SVC.LEG_TERMINAL and len(op.calls) == before)
    check("★★ E. consumed 後 allows 一律 False",
          leg.allows("charge") is False and leg.allows("stop") is False)

    # ---------------- F. 錯誤的確認字串 ----------------
    print("\nF. 確認字串錯誤 → 不建立 executor")
    leg = PRD.LiveLegAuthorization("charge")
    want = PRD.confirmation_phrase(leg)
    check("★★ F. 確認字串為明確語句（含動作與功率）",
          want == "CONFIRM CHARGE 5KW")
    for bad in ("YES", "y", "confirm charge 5kw", "CONFIRM CHARGE", "", "  "):
        check(f"★★ F. 輸入 {bad!r} → 確認失敗",
              SVC.confirm_live_leg(leg, prompt=lambda _p: bad) is False)
    check("★★ F. 輸入正確字串（含前後空白）→ 確認通過",
          SVC.confirm_live_leg(leg, prompt=lambda _p: f"  {want}  ") is True)
    check("★★ F. 確認失敗時不會建立任何出口",
          PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG, leg, None) is None)

    # ---------------- G. fresh precheck ----------------
    print("\nG. fresh precheck 失敗 → executor None / 0 calls")

    class PreArb(object):
        def __init__(self, **kw):
            self.valid = True
            self.pcs_state = "STANDBY"
            self.soc_percent = 50.0
            self.pcs_fault = False
            self.active_alarm_count = 0
            self.alarm_source_complete = True
            self.schedule_switch = 0
            self.manual_switch = 1
            self.authority_state = "IDLE"
            self.tou_state = "OFF_PEAK"
            for k, v in kw.items():
                setattr(self, k, v)

    class Snap(object):
        valid, stale, age_sec = True, False, 0.5

    ok, res = SVC.live_leg_precheck(PreArb(), Snap(), None)
    check("★★ G. 全部條件成立 → precheck PASS（16 項）",
          ok is True and len(res) == len(SVC.PRECHECK_ITEMS))
    for field, bad, tag in (("valid", False, "ESS 無效"),
                            ("pcs_state", "CHARGING", "PCS 非閒置"),
                            ("pcs_fault", True, "PCS 故障"),
                            ("active_alarm_count", 3, "有作用中告警"),
                            ("alarm_source_complete", None, "告警來源不完整"),
                            ("schedule_switch", 1, "排程 ON"),
                            ("manual_switch", 0, "手動開關不正確"),
                            ("authority_state", "UNKNOWN", "Authority 非 IDLE"),
                            ("tou_state", "UNKNOWN", "時段 UNKNOWN"),
                            ("soc_percent", None, "SOC 不合理")):
        bad_ok, _ = SVC.live_leg_precheck(PreArb(**{field: bad}), Snap(), None)
        check(f"★★ G. {tag} → precheck FAIL", bad_ok is False)
    check("★★ G. 電表無效 → FAIL",
          SVC.live_leg_precheck(PreArb(), type("S", (), {
              "valid": False, "stale": True, "age_sec": 99.0})(), None)[0] is False)
    check("★★ G. 已有 report session → FAIL",
          SVC.live_leg_precheck(PreArb(), Snap(), object())[0] is False)
    op = Op()
    leg = PRD.LiveLegAuthorization("charge")
    runner = SVC.ControlledLiveLeg(leg, lambda e, v: Chain(OK_CHARGE, op, leg),
                                   lambda: Arb())
    check("★★ G. precheck 未通過就不 arm → run_leg 直接 ABORTED、0 calls",
          runner.run_leg() == SVC.LEG_ABORTED and op.calls == [])

    # ---------------- H/I/J. 上游守門 ----------------
    print("\nH/I/J. Safety / Authority / Interlock 擋下 → 0 calls")
    for tag, step in (("H. Safety BLOCK", Res(None, executed=False,
                                              outcome="BLOCKED")),
                      ("I. Authority BLOCK", Res(None, executed=False,
                                                 outcome="AUTHORITY_BLOCKED")),
                      ("J. Interlock BLOCK", Res(None, executed=False,
                                                 outcome="DIRECTION_BLOCKED"))):
        op = Op()
        r, _ = leg_runner("charge", [step], op)
        st = r.run_leg()
        check(f"★★ {tag} → ABORTED 且 operator 0 calls",
              st == SVC.LEG_ABORTED and op.calls == [])

    # ---------------- K. ReadBack failure ----------------
    print("\nK. ReadBack 失敗 → 不重試第二次方向指令")
    op = Op()
    r, _ = leg_runner("charge", [Res("charge", readback="VERIFY_TIMEOUT"),
                                 Res("charge")], op)
    st = r.run_leg()
    check("★★ K. ABORTED 且方向指令只送一次",
          st == SVC.LEG_ABORTED and op.actions.count("charge") == 1)
    check("★★ K. 沒有送出任何 STOP（不自動補送）",
          "stop" not in op.actions)
    check("★★ K. LastControl 未寫入亦視為失敗",
          leg_runner("charge", [Res("charge", lc=False)], Op())[0].run_leg()
          == SVC.LEG_ABORTED)

    # ---------------- L. dispatch 後失去擁有權 ----------------
    print("\nL. dispatch 後 Authority 不再可信 → 不自動 STOP")
    for auth in ("UNKNOWN", "EXTERNAL_OR_UNKNOWN", "CONFLICT", None):
        op = Op()
        r, _ = leg_runner("charge", OK_CHARGE, op, authority_after=auth)
        st = r.run_leg()
        check(f"★★ L. authority={auth} → ABORTED_UNCERTAIN_OWNERSHIP",
              st == SVC.LEG_ABORTED_UNCERTAIN)
        check(f"★★ L. authority={auth} → **未送出任何 STOP**",
              "stop" not in op.actions and r.stop_count == 0)
    op = Op()
    r, _ = leg_runner("charge", OK_CHARGE, op, authority_after="IDLE")
    check("★★ L. 非 OWNED（IDLE）同樣不得送 STOP",
          r.run_leg() == SVC.LEG_ABORTED_UNCERTAIN and "stop" not in op.actions)

    # ---------------- M. 正常成功 ----------------
    print("\nM. 正常成功：恰好 1 個方向指令 + 1 個 STOP")
    for tag, action, steps in (("CHARGE", "charge", OK_CHARGE),
                               ("DISCHARGE", "discharge", OK_DISCHARGE)):
        op = Op()
        r, leg = leg_runner(action, steps, op)
        st = r.run_leg()
        check(f"★★ M. {tag} leg 完整走完並回 OBSERVE_ONLY",
              st == SVC.LEG_OBSERVE_ONLY)
        check(f"★★ M. {tag} 方向指令**恰好** 1 次",
              op.actions.count(action) == 1 and r.dispatch_count == 1)
        check(f"★★ M. {tag} STOP **恰好** 1 次",
              op.actions.count("stop") == 1 and r.stop_count == 1)
        check(f"★★ M. {tag} 總呼叫數為 2（無多餘指令）", len(op.calls) == 2)
        check(f"★★ M. {tag} 狀態機依序走過完整生命週期",
              r.history[:4] == [SVC.LEG_ARMED, SVC.LEG_PRECHECKED,
                                SVC.LEG_DISPATCHED, SVC.LEG_VERIFIED]
              and r.history[-3:] == [SVC.LEG_REPORT_FINALIZED,
                                     SVC.LEG_COMPLETE, SVC.LEG_OBSERVE_ONLY])
        check(f"★★ M. {tag} 送出的功率取自 ProductionConfig（5.0）",
              all(c["power"] == 5.0 for c in op.calls))

    # ---------------- N/O. 報告生命週期 ----------------
    print("\nN/O. Report session / finalize 各只一次")
    op = Op()
    r, _ = leg_runner("charge", OK_CHARGE, op)
    r.run_leg()
    check("★★ N. 狀態機只經過一次 REPORT_FINALIZED",
          r.history.count(SVC.LEG_REPORT_FINALIZED) == 1)
    check("★★ N. 只經過一次 IDLE_VERIFIED（不重複收尾）",
          r.history.count(SVC.LEG_IDLE_VERIFIED) == 1)
    svc_src = io.open(os.path.join(HERE, "pcs_auto_control_service.py"),
                      encoding="utf-8").read()
    # ⚠️ 不能用字串比對 —— 說明文字本身就會提到這些名稱。改看實際呼叫。
    _called = {n.func.attr for n in ast.walk(_tree("pcs_auto_control_service.py"))
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    _called |= {n.func.id for n in ast.walk(_tree("pcs_auto_control_service.py"))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check("★★ O. 本層不自行 finalize（沿用既有 idle debounce）",
          not (_called & {"_report_finalize", "should_auto_end",
                          "finalize", "_report_start_session"}))
    check("★★ O. 本層不 import 任何報告模組",
          not ({"report_monitor", "auto_monitor_service",
                "charge_discharge_report"}
               & _imports("pcs_auto_control_service.py")))

    # ---------------- P/Q. restart ----------------
    print("\nP/Q. process / service restart")
    check("★★ P. 授權不落盤（不寫任何檔案）",
          not any(k in io.open(os.path.join(HERE,
                                            "pcs_auto_control_production.py"),
                               encoding="utf-8").read()
                  for k in ("json.dump", "open(", "persist", "save(")))
    check("★★ P. 新 process 的模組層預設仍為 OBSERVE_ONLY",
          SVC.build_production_stack()[2].mode == PRD.MODE_OBSERVE_ONLY
          and RT.DISPATCH_ENABLED is False)
    check("★★ Q. 沒有任何環境變數 / config flag 可啟用 LIVE",
          "DISPATCH_ENABLED = False" in io.open(
              os.path.join(HERE, "pcs_auto_control_runtime.py"),
              encoding="utf-8").read()
          and "getenv" not in svc_src and "environ" not in svc_src)
    check("★★ Q. 啟用只能靠 CLI 參數（隨 process 生命週期）",
          '"--live-leg"' in svc_src and "choices=PRD.LEG_ACTIONS" in svc_src)
    check("★★ Q. recovery 不會設定 dispatch_enabled",
          RT.AutoControlRuntime().dispatch_enabled is False)

    # ---------------- R/S. 功率來源 ----------------
    print("\nR/S. LIVE 功率只能來自 ProductionConfig")
    check("★★ R. leg 功率為 5.0（不是 150）",
          PRD.LiveLegAuthorization("charge").power_kw == 5.0
          and PRD.LiveLegAuthorization("discharge").power_kw == 5.0)
    check("★★ R. 150 kW 仍只是 Safety Gate 上限，非操作目標",
          CFG.DEFAULT_CONTROL_CONFIG.max_power_kw == 150.0
          and CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0)
    check("★★ S. LiveLegAuthorization 不接受任何 power 參數",
          [a.arg for a in [n for n in _tree(
              "pcs_auto_control_production.py").body
              if isinstance(n, ast.ClassDef)
              and n.name == "LiveLegAuthorization"][0].body
              if isinstance(a, ast.FunctionDef) and a.name == "__init__"
           for a in a.args.args] == ["self", "action", "config"])
    check("★★ S. CLI 不提供 --power",
          '"--power"' not in svc_src and "'--power'" not in svc_src)
    check("★★ S. production 功率未設定時拒絕核發 leg 授權",
          _raises(lambda: PRD.LiveLegAuthorization(
              "charge", config=CFG.AutoControlConfig(charge_power_kw=None))))

    # ---------------- T. 自動回退 ----------------
    print("\nT. 完整 leg 後自動回 OBSERVE_ONLY")
    op = Op()
    r, leg = leg_runner("charge", OK_CHARGE, op)
    r.run_leg()
    snap = r.snapshot()
    check("★★ T. 最終狀態為 OBSERVE_ONLY", snap["state"] == SVC.LEG_OBSERVE_ONLY)
    check("★★ T. executor / verifier 已銷毀",
          snap["executor_present"] is False and snap["verifier_present"] is False)
    check("★★ T. leg 授權已 consumed", snap["leg"]["state"] == PRD.LEG_CONSUMED)
    check("★★ T. 模組層 dispatch_enabled 仍為 False",
          RT.DISPATCH_ENABLED is False)
    check("★★ T. 全新 stack 的 can_dispatch 仍為 False",
          SVC.build_production_stack()[2].can_dispatch is False)
    for st in (SVC.LEG_ABORTED, SVC.LEG_ABORTED_UNCERTAIN):
        op2 = Op()
        rr, _ = leg_runner("charge", [Res("discharge")], op2)
        rr.abort("test", uncertain=(st == SVC.LEG_ABORTED_UNCERTAIN))
        check(f"★★ T. {st} 亦會銷毀出口",
              rr.executor is None and rr.verifier is None)

    # ---------------- 不變量 ----------------
    print("\n不變量")
    check("★★ 只有 CONTROLLED_SINGLE_LEG 可能建立出口",
          PRD.DISPATCH_CAPABLE_MODES
          == frozenset({PRD.MODE_CONTROLLED_SINGLE_LEG}))
    check("★★ 未 arm 的 leg 不會拿到出口",
          PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG,
                             _consumed_leg(), lambda **k: None) is None)
    check("★★ 未支援的模式一律拒絕",
          _raises(lambda: PRD.build_executor("LIVE"), ValueError))
    check("★★ Safety / Authority / Interlock / Executor / config 未被本階段修改",
          all(k not in io.open(os.path.join(HERE, f), encoding="utf-8").read()
              for f in ("safety_gate.py", "control_authority.py",
                        "pcs_control_integration.py", "pcs_control_executor.py")
              for k in ("LiveLegAuthorization", "live_leg")))
    check("★★ Production 操作功率仍為 ±5 kW，未因 LIVE 而改變",
          CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0
          and CFG.DEFAULT_CONTROL_CONFIG.discharge_power_kw == 5.0)

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.9 Controlled Single-Leg LIVE 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def _imports(name):
    out = set()
    for n in ast.walk(_tree(name)):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


def _raises(fn, exc=ValueError):
    try:
        fn()
        return False
    except exc:
        return True


def _consumed_leg():
    lg = PRD.LiveLegAuthorization("charge")
    lg.consume()
    return lg


if __name__ == "__main__":
    raise SystemExit(main())
