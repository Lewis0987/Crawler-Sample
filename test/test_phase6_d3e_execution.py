# -*- coding: utf-8 -*-
"""
test_phase6_d3e_execution.py — Phase D.3-E Production Execution Chain（A~AW）
======================================================================
核心命題
    「完整生命週期可以跑完，但只有**可證明**的成功才會被記錄為擁有。」

不可違反的界線
    1. LastControl 只有 VERIFY_SUCCESS 才寫入；其餘一律不寫。
    2. 「未驗證成功」**不等於**「指令沒有執行」→ COMMAND_OUTCOME_UNKNOWN，Fail Closed。
    3. 授權票 one-shot：送出失敗 / 回讀失敗 / 例外 / 逾時都不得重用。
    4. Duplicate suppression 是**安全條件**：same-action + same-target 絕不可進 Executor。
    5. production 預設（全 None、executor/verifier/store 未注入）→ 零授權、零送出、零寫入。

是否需要設備
    **不需要**。executor 以 fake operator_run 注入、read-back 以 fake reader 注入、
    LastControl 寫入暫存目錄。零網路、零登入、零實機副作用。

用法
    python test_phase6_d3e_execution.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcs_control_executor as EX                        # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import last_control_store as LCS                         # noqa: E402
import control_authority as CA                           # noqa: E402
import safety_gate as SG                                 # noqa: E402
import decision_policy as DP                             # noqa: E402
import production_control_authorization as PAZ           # noqa: E402
import production_arbiter as ARB                         # noqa: E402
import production_execution_chain as PEC                 # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402

import test_phase6_d3d_arbitration as D3D                # noqa: E402

RESULTS = []
_TMP = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ----------------------------------------------------------------------
# Fakes —— 全部注入；沒有任何一個會碰到真實設備
# ----------------------------------------------------------------------
def op_record(control_success=True, **over):
    r = {"action": "pcs_charge", "control_success": control_success,
         "dry_run": False, "warnings": []}
    r.update(over)
    return r


def fake_operator(record=None, raise_exc=None, malformed=False):
    """假的 operator_run。**不 import、不呼叫** device_control_operator。"""
    calls = []

    def _run(**kwargs):
        calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        if malformed:
            return "not a dict"
        return dict(record if record is not None else op_record())

    _run.calls = calls
    return _run


def flags(state, power=None):
    m = {"CHARGING": dict(pcs_charging_flag=True, pcs_discharging_flag=False,
                          pcs_standby_flag=False, pcs_running_flag=True),
         "DISCHARGING": dict(pcs_charging_flag=False, pcs_discharging_flag=True,
                             pcs_standby_flag=False, pcs_running_flag=True),
         "STANDBY": dict(pcs_charging_flag=False, pcs_discharging_flag=False,
                         pcs_standby_flag=True, pcs_running_flag=True),
         "STOPPED": dict(pcs_charging_flag=False, pcs_discharging_flag=False,
                         pcs_standby_flag=False, pcs_running_flag=False),
         "CONFLICT": dict(pcs_charging_flag=True, pcs_discharging_flag=True,
                          pcs_standby_flag=False, pcs_running_flag=True),
         "UNKNOWN": dict(pcs_charging_flag=False, pcs_discharging_flag=False,
                         pcs_standby_flag=False, pcs_running_flag=True)}[state]
    m["actual_active_power_kw"] = power
    return m


def make_verifier(states, raise_exc=None):
    """以既有 ReadBackVerifier + 假 reader 建立（走真實驗證邏輯）。"""
    seq = list(states)
    clk = {"t": 0.0}

    def _read():
        if raise_exc is not None:
            raise raise_exc
        return dict(seq.pop(0) if len(seq) > 1 else seq[0])

    return EX.ReadBackVerifier(
        EX.ReadBackConfig(timeout_sec=20.0, poll_interval_sec=5.0,
                          stability_samples=1),
        reader=_read, clock=lambda: clk["t"],
        sleeper=lambda d: clk.__setitem__("t", clk["t"] + d))


def temp_store(broken=False):
    """真實 LastControlStore，寫入暫存目錄；broken=True 時讓寫入必定失敗。"""
    d = tempfile.mkdtemp(prefix="d3e_")
    _TMP.append(d)
    p = os.path.join(d, "last_control.json")
    if broken:
        os.makedirs(p)              # 路徑被目錄佔用 → 寫入必定 OSError
    return LCS.LastControlStore(path=p)


class Chain:
    """完整鏈：D.3-D 的觀測/仲裁 Rig + 注入的 executor / verifier / store。"""

    def __init__(self, pcs="STANDBY", soc=30.0, local_now=D3D.DT_OFF_PEAK,
                 last_control=None, ess_over=None, config=D3D.TEST_CFG,
                 operator=None, verify_states=None, store=None,
                 revalidate=None, phase_hook=None, execute=True):
        self.rig = D3D.Rig(pcs=pcs, soc=soc, local_now=local_now,
                           last_control=last_control, ess_over=ess_over,
                           config=config)
        self.rig.settle()
        self.operator = operator if operator is not None else fake_operator()
        self.store = store if store is not None else temp_store()
        self.phases = []
        self.chain = PEC.ProductionExecutionChain(
            arbiter=self.rig.arbiter,
            executor=EX.PcsControlExecutor(operator_run=self.operator,
                                           execute=execute),
            verifier=make_verifier(verify_states or [flags(pcs)]),
            store=self.store, revalidate=revalidate,
            clock=self.rig.dec_clk,
            phase_hook=(phase_hook if phase_hook is not None
                        else self.phases.append))

    def run(self, dt=1.0):
        self.rig.clk.advance(dt)
        self.rig.dec_clk.advance(dt)
        return self.chain.run()


def _tree(name):
    return ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())


def _all_imports(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


ALWAYS_OK = (lambda az: (True, "OK"))


# ======================================================================
def main():
    print("== Phase D.3-E Production Execution Chain 驗證（完全離線）==\n")

    # ---------------- A/B/C. happy path ----------------
    print("A/B/C. 完整生命週期（授權 → 消費 → 送出 → 驗證 → 記錄）")
    happy = [("A. CHARGE", "STANDBY", 30.0, D3D.DT_OFF_PEAK, None,
              PCI.CTRL_CHARGE, "CHARGING", RT.ST_OWNED_CHARGE),
             ("B. DISCHARGE", "STANDBY", 80.0, D3D.DT_PEAK, None,
              PCI.CTRL_DISCHARGE, "DISCHARGING", RT.ST_OWNED_DISCHARGE)]
    for tag, pcs, soc, dt, lc, act, done, want_state in happy:
        c = Chain(pcs=pcs, soc=soc, local_now=dt, last_control=lc,
                  verify_states=[flags(done, 5.1)], revalidate=ALWAYS_OK)
        r = c.run()
        check(f"★★ {tag} → VERIFIED / 已寫入 LastControl",
              r.outcome == PEC.EXEC_VERIFIED and r.control_action == act
              and r.executed is True and r.lastcontrol_written is True)
        check(f"  {tag} 生命週期階段完整",
              tuple(c.phases) == (PEC.PH_AUTHORIZED, PEC.PH_REVALIDATED,
                                  PEC.PH_CONSUMED, PEC.PH_DISPATCHED,
                                  PEC.PH_VERIFYING, PEC.PH_VERIFIED,
                                  PEC.PH_COMMITTED))
        check(f"  {tag} 授權票已消費且不可重用",
              r.authorization.state == PAZ.AUTHZ_CONSUMED
              and r.authorization.consume(act, 9999.0)[1]
              == PAZ.AZ_ALREADY_CONSUMED)
        rec = c.store.load().record
        check(f"  {tag} LastControl 記錄了正確的 action / target",
              rec.action == act and rec.target_power_kw == 5.0)
        check(f"  {tag} dispatch 恰好一次、寫入恰好一次",
              c.chain.dispatch_count == 1 and c.chain.lastcontrol_write_count == 1)
        rt = RT.AutoControlRuntime(config=D3D.TEST_CFG, dispatch_enabled=True,
                                   observation_source=lambda r=r: r)
        res = rt.tick(RT.CycleInputs(is_owner=True))
        check(f"★★ {tag} → Runtime 進入 {want_state}",
              res.state == want_state and res.audit_event
              == RT.AUDIT_EXECUTED_VERIFIED and rt.dispatch_count == 1)

    # C. STOP happy path（反向切換只會產生 STOP leg）
    c = Chain(pcs="CHARGING", soc=80.0, local_now=D3D.DT_PEAK,
              ess_over={"actual_active_power_kw": 5.2},
              last_control=D3D.LCRec("charge", 5.0, 1000.0),
              verify_states=[flags("STOPPED", -1.3)], revalidate=ALWAYS_OK)
    r = c.run()
    check("★★ C. STOP → VERIFIED（反向切換只形成 STOP leg）",
          r.outcome == PEC.EXEC_VERIFIED and r.control_action == PCI.CTRL_STOP
          and r.lastcontrol_written is True)
    check("★★ Z. STOPPED 被接受為合法的 idle 結果（不強迫 STANDBY）",
          r.readback_observed_state == PCI.PCS_STOPPED)
    rec = c.store.load().record
    check("★★ 9. STOP 的 LastControl：action=stop、target 一律 None（沿用既有契約）",
          rec.action == LCS.ACT_STOP and rec.target_power_kw is None)
    rt = RT.AutoControlRuntime(config=D3D.TEST_CFG, dispatch_enabled=True,
                               observation_source=lambda r=r: r)
    check("★★ STOP 驗證成功 → Runtime 回 IDLE（非 OWNED）",
          rt.tick(RT.CycleInputs(is_owner=True)).state == RT.ST_IDLE)

    # ---------------- D~I. 授權與重驗 ----------------
    print("\nD~I. 授權票與 dispatch 前重驗")
    c = Chain(pcs="CHARGING", soc=30.0, ess_over={"actual_active_power_kw": 5.2},
              last_control=D3D.LCRec("charge", 5.0, 1000.0), revalidate=ALWAYS_OK)
    r = c.run()
    check("★★ D. 仲裁未授權（本例為 duplicate 抑制）→ NOT_ATTEMPTED / 零送出",
          r.outcome == PEC.EXEC_NOT_ATTEMPTED and r.executed is False
          and c.chain.dispatch_count == 0
          and len(c.operator.calls) == 0)

    def _expire(az):
        az.state = PAZ.AUTHZ_EXPIRED
        return True, "OK"

    c = Chain(revalidate=_expire)
    r = c.run()
    check("★★ E. 授權票過期 → AUTHORIZATION_INVALID / 零送出",
          r.outcome == PEC.EXEC_AUTHORIZATION_INVALID
          and r.consume_reason == PAZ.AZ_EXPIRED
          and r.executed is False and len(c.operator.calls) == 0)

    def _consume_first(az):
        az.consume(az.action, 0.0, target_power_kw=az.target_power_kw)
        return True, "OK"

    c = Chain(revalidate=_consume_first)
    r = c.run()
    check("★★ F. 授權票已被消費 → AUTHORIZATION_INVALID / 零送出",
          r.outcome == PEC.EXEC_AUTHORIZATION_INVALID
          and r.consume_reason == PAZ.AZ_ALREADY_CONSUMED
          and r.executed is False and len(c.operator.calls) == 0)

    b = PAZ.AuthorizationBinding(authority_state=CA.AUTH_IDLE,
                                 pcs_state=PCI.PCS_STANDBY,
                                 decision_action="charge",
                                 alarm_source_complete=True,
                                 schedule_switch=0, manual_switch=1)
    az, _ = PAZ.issue(PCI.CTRL_CHARGE, 5.0, 100.0, 30.0, b, 1)
    check("★★ G. action 不符 → 不可消費",
          az.consume(PCI.CTRL_DISCHARGE, 105.0)[1] == PAZ.AZ_ACTION_MISMATCH)
    check("★★ H. target 不符 → 不可消費",
          az.consume(PCI.CTRL_CHARGE, 105.0, None, 8.0)[1]
          == PAZ.AZ_TARGET_MISMATCH)

    for why, tag in ((PEC.R_STALE_BEFORE_DISPATCH, "I. 送出前已 stale"),
                     (PEC.R_AUTHORITY_CHANGED, "J. 送出前模式改變"),
                     (PEC.R_OBSERVATION_CHANGED, "K. 送出前觀測改變")):
        c = Chain(revalidate=lambda az, w=why: (False, w))
        r = c.run()
        check(f"★★ {tag} → REVALIDATION_FAILED / 零送出 / 票作廢",
              r.outcome == PEC.EXEC_REVALIDATION_FAILED and r.reason == why
              and r.executed is False and len(c.operator.calls) == 0
              and r.authorization.state == PAZ.AUTHZ_CANCELLED)
    check("★★ 16. TTL 未過期**不能**取代 freshness（兩者分別檢查）",
          "R_STALE_BEFORE_DISPATCH" in io.open(
              os.path.join(HERE, "production_execution_chain.py"),
              encoding="utf-8").read())

    # ---------------- L. ESS stale ----------------
    print("\nL. ESS stale")
    stale = D3D.Rig(pcs="STANDBY", soc=30.0, dec_offset=20.0)
    stale.settle()
    ch = PEC.ProductionExecutionChain(
        arbiter=stale.arbiter,
        executor=EX.PcsControlExecutor(operator_run=fake_operator(), execute=True),
        verifier=make_verifier([flags("CHARGING", 5.1)]), store=temp_store(),
        clock=stale.dec_clk)
    r = ch.run()
    check("★★ L. ESS stale → 仲裁就擋下 → 零授權 / 零送出",
          r.outcome == PEC.EXEC_NOT_ATTEMPTED and r.executed is False
          and ch.dispatch_count == 0 and ch.lastcontrol_write_count == 0)

    # ---------------- M~Q. Executor / ReadBack 失敗 ----------------
    print("\nM~Q. 送出與回讀失敗（『未驗證』≠『沒執行』）")
    fails = [
        ("M. Executor 例外", fake_operator(raise_exc=RuntimeError("boom")),
         [flags("STANDBY")], PEC.EXEC_OUTCOME_UNKNOWN),
        ("N. Operator 拒絕（control_success=False）",
         fake_operator(op_record(control_success=False)),
         [flags("STANDBY")], PEC.EXEC_OUTCOME_UNKNOWN),
        ("N. Operator 前置檢查擋下", fake_operator(op_record(blocked=True)),
         [flags("STANDBY")], PEC.EXEC_COMMAND_NOT_SENT),
        ("N. Operator 回傳格式錯誤", fake_operator(malformed=True),
         [flags("STANDBY")], PEC.EXEC_OUTCOME_UNKNOWN),
        ("O. POST 接受 + 回讀逾時", fake_operator(),
         [flags("STANDBY")], PEC.EXEC_OUTCOME_UNKNOWN),
        ("P. POST 接受 + 回讀狀態互斥", fake_operator(),
         [flags("CONFLICT")], PEC.EXEC_VERIFY_MISMATCH),
        ("Q. POST 接受 + 回讀通訊失敗", fake_operator(),
         [flags("UNKNOWN")], PEC.EXEC_OUTCOME_UNKNOWN),
    ]
    for tag, op, vs, want in fails:
        c = Chain(operator=op, verify_states=vs, revalidate=ALWAYS_OK)
        r = c.run()
        check(f"★★ {tag} → {want}",
              r.outcome == want and r.lastcontrol_written is False)
        check(f"  {tag} → LastControl 完全未寫入",
              c.chain.lastcontrol_write_count == 0
              and c.store.load().outcome == LCS.LOAD_NOT_FOUND)
        check(f"  {tag} → 授權票不可重用（AA/AB/AC）",
              r.authorization.state in (PAZ.AUTHZ_CONSUMED, PAZ.AUTHZ_CANCELLED)
              and r.authorization.consume(r.control_action, 9999.0)[0] is False)
        rt = RT.AutoControlRuntime(config=D3D.TEST_CFG, dispatch_enabled=True,
                                   observation_source=lambda r=r: r)
        res = rt.tick(RT.CycleInputs(is_owner=True))
        check(f"  {tag} → Runtime FAULT_BLOCKED，不宣稱擁有",
              res.state == RT.ST_FAULT_BLOCKED
              and res.state not in (RT.ST_OWNED_CHARGE, RT.ST_OWNED_DISCHARGE))
    check("★★ 15. 『可證明未送出』與『結果不明』是不同 outcome",
          PEC.EXEC_COMMAND_NOT_SENT != PEC.EXEC_OUTCOME_UNKNOWN
          and PEC.PROVABLY_NOT_SENT == frozenset({
              EX.R_OPERATOR_NOT_CONFIGURED, EX.R_OPERATOR_DRY_RUN,
              EX.R_OPERATOR_PRECHECK_BLOCKED}))

    # ---------------- R/S. LastControl 寫入 ----------------
    print("\nR/S. LastControl 寫入")
    c = Chain(verify_states=[flags("CHARGING", 5.1)], revalidate=ALWAYS_OK)
    r = c.run()
    check("★★ R. 回讀成功 + 寫入成功 → VERIFIED",
          r.outcome == PEC.EXEC_VERIFIED
          and r.lastcontrol_outcome == LCS.UPD_UPDATED)
    c = Chain(verify_states=[flags("CHARGING", 5.1)], revalidate=ALWAYS_OK,
              store=temp_store(broken=True))
    r = c.run()
    check("★★ S. 回讀成功但寫入失敗 → OWNERSHIP_UNRECORDED（Fail Closed）",
          r.outcome == PEC.EXEC_OWNERSHIP_UNRECORDED
          and r.lastcontrol_outcome == LCS.UPD_WRITE_FAILED
          and r.lastcontrol_written is False)
    check("  S. 既不宣稱擁有、也不當成指令失敗（executed 仍為 True）",
          r.executed is True and c.chain.lastcontrol_write_count == 0)
    rt = RT.AutoControlRuntime(config=D3D.TEST_CFG, dispatch_enabled=True,
                               observation_source=lambda r=r: r)
    check("  S. Runtime → FAULT_BLOCKED，不進 OWNED_*",
          rt.tick(RT.CycleInputs(is_owner=True)).state == RT.ST_FAULT_BLOCKED)

    # ---------------- T/U. Duplicate suppression 安全不變量 ----------------
    print("\nT/U. Duplicate suppression 是安全條件（不是效能最佳化）")
    for pcs, act, dt, soc, kw, tag in (
            ("CHARGING", "charge", D3D.DT_OFF_PEAK, 30.0, 5.2, "T. CHARGE"),
            ("DISCHARGING", "discharge", D3D.DT_PEAK, 80.0, -5.2, "U. DISCHARGE")):
        c = Chain(pcs=pcs, soc=soc, local_now=dt,
                  ess_over={"actual_active_power_kw": kw},
                  last_control=D3D.LCRec(act, 5.0, 1000.0), revalidate=ALWAYS_OK)
        r = c.run()
        check(f"★★ {tag} 同目標 → SUPPRESSED；Executor 0 / ReadBack 0 / 寫入 0",
              r.outcome == PEC.EXEC_NOT_ATTEMPTED
              and r.arbitration.reason == ARB.R_ALREADY_AT_DESIRED_STATE
              and len(c.operator.calls) == 0
              and c.chain.dispatch_count == 0
              and c.chain.lastcontrol_write_count == 0
              and c.store.load().outcome == LCS.LOAD_NOT_FOUND)
    check("★★ AP. 抑制發生在仲裁層，執行鏈**結構上**無法繞過"
          "（未授權即 NOT_ATTEMPTED）",
          "if not getattr(arb, \"authorized\", False):" in io.open(
              os.path.join(HERE, "production_execution_chain.py"),
              encoding="utf-8").read())

    # ---------------- V/W. Target change ----------------
    print("\nV/W. 同向不同目標仍 Fail Closed")
    for pcs, act, dt, soc, kw, tag in (
            ("CHARGING", "charge", D3D.DT_OFF_PEAK, 30.0, 9.2, "V. CHARGE"),
            ("DISCHARGING", "discharge", D3D.DT_PEAK, 80.0, -9.2, "W. DISCHARGE")):
        c = Chain(pcs=pcs, soc=soc, local_now=dt,
                  ess_over={"actual_active_power_kw": kw},
                  last_control=D3D.LCRec(act, 9.0, 1000.0), revalidate=ALWAYS_OK)
        r = c.run()
        check(f"★★ {tag} 不同目標 → 不執行 / 不寫入（Blocker 10 維持 Fail Closed）",
              r.outcome == PEC.EXEC_NOT_ATTEMPTED
              and r.arbitration.reason == ARB.R_TARGET_CHANGE_UNSUPPORTED
              and len(c.operator.calls) == 0
              and c.chain.dispatch_count == 0
              and c.chain.lastcontrol_write_count == 0)

    # ---------------- X/Y. 反向切換 ----------------
    print("\nX/Y. 反向切換只產生 STOP leg")
    for pcs, act, dt, soc, kw, opp, tag in (
            ("CHARGING", "charge", D3D.DT_PEAK, 80.0, 5.2,
             PCI.CTRL_DISCHARGE, "X. CHARGE→DISCHARGE"),
            ("DISCHARGING", "discharge", D3D.DT_OFF_PEAK, 30.0, -5.2,
             PCI.CTRL_CHARGE, "Y. DISCHARGE→CHARGE")):
        c = Chain(pcs=pcs, soc=soc, local_now=dt,
                  ess_over={"actual_active_power_kw": kw},
                  last_control=D3D.LCRec(act, 5.0, 1000.0),
                  verify_states=[flags("STOPPED", -1.3)], revalidate=ALWAYS_OK)
        r = c.run()
        check(f"★★ {tag} → 只送出 STOP，不送反方向",
              r.control_action == PCI.CTRL_STOP and r.control_action != opp
              and c.chain.dispatch_count == 1)
        check(f"  {tag} 本輪不可能出現反方向的 leg",
              c.store.load().record.action == LCS.ACT_STOP)

    # ---------------- schedule / manual takeover ----------------
    print("\n授權後、送出前的接管")
    rig = D3D.Rig(pcs="STANDBY", soc=30.0)
    rig.settle()
    ch = PEC.ProductionExecutionChain(
        arbiter=rig.arbiter,
        executor=EX.PcsControlExecutor(operator_run=fake_operator(), execute=True),
        verifier=make_verifier([flags("CHARGING", 5.1)]), store=temp_store(),
        clock=rig.dec_clk)
    rig.ess_over["pcs_schedule_enabled"] = True     # 授權後排程被開啟
    r = ch.run()
    check("★★ 24. 排程接管 → 不送出（Executor 0）",
          r.executed is False and ch.dispatch_count == 0)
    rig2 = D3D.Rig(pcs="STANDBY", soc=30.0)
    rig2.settle()
    ch2 = PEC.ProductionExecutionChain(
        arbiter=rig2.arbiter,
        executor=EX.PcsControlExecutor(operator_run=fake_operator(), execute=True),
        verifier=make_verifier([flags("CHARGING", 5.1)]), store=temp_store(),
        clock=rig2.dec_clk)
    rig2.pcs = "CHARGING"                            # 授權後被人工改成充電中
    rig2.ess_over["actual_active_power_kw"] = 7.7
    r2 = ch2.run()
    check("★★ 25. 人工接管 → 不送出（Executor 0）",
          r2.executed is False and ch2.dispatch_count == 0)

    # ---------------- AD/AE. production 預設 ----------------
    print("\nAD/AE. production 預設 → 零執行")
    prod = SVC.build_execution_chain()
    pr = prod.run()
    check("★★ AD. production 預設 → 零授權 / 零送出 / 零寫入",
          pr.executed is False and prod.dispatch_count == 0
          and prod.lastcontrol_write_count == 0
          and prod.executor is None and prod.verifier is None
          and prod.store is None)
    prod_cfg = PEC.ProductionExecutionChain(
        arbiter=D3D.Rig(pcs="STANDBY", soc=30.0,
                        config=CFG.DEFAULT_CONTROL_CONFIG).settle().arbiter,
        executor=EX.PcsControlExecutor(operator_run=fake_operator(), execute=True),
        verifier=make_verifier([flags("CHARGING", 5.1)]), store=temp_store())
    pr2 = prod_cfg.run()
    check("★★ AD. 即使注入了元件，production config 全 None → 仍不授權、不送出",
          pr2.executed is False and prod_cfg.dispatch_count == 0
          and prod_cfg.lastcontrol_write_count == 0)
    rt_prod = RT.AutoControlRuntime()
    check("★★ AE. production Runtime 的 dispatch_enabled=False",
          rt_prod.dispatch_enabled is False and RT.DISPATCH_ENABLED is False)
    for st in sorted(RT.EXECUTION_STATES):
        try:
            rt_prod._make(st, "x", "x")
            blocked = False
        except AssertionError:
            blocked = True
        check(f"★★ AE. dispatch_enabled=False → {st} 不可達", blocked)
    rt_on = RT.AutoControlRuntime(dispatch_enabled=True)
    check("  對照：dispatch_enabled=True 時 VERIFYING 才可達",
          rt_on._make(RT.ST_VERIFYING, "x", "x").state == RT.ST_VERIFYING)

    # ---------------- AF/AG. LastControl 契約 ----------------
    print("\nAF/AG. LastControl 契約")
    st = temp_store()
    vr_accepted = EX.VerifiedControlResult(
        PCI.CTRL_CHARGE, 5.0, EX.COMMAND_ACCEPTED, EX.R_OK,
        lastcontrol_eligible=False)
    check("★★ AG. control_success=True（COMMAND_ACCEPTED）單獨**不能**寫入",
          st.update_from_verified_result(vr_accepted).outcome
          == LCS.UPD_NOT_UPDATED and st.load().outcome == LCS.LOAD_NOT_FOUND)
    for oc in (EX.VERIFY_PENDING, EX.VERIFY_TIMEOUT, EX.VERIFY_FAILED,
               EX.COMMAND_NOT_SENT, EX.COMMAND_BLOCKED, EX.COMMAND_SEND_FAILED):
        vr = EX.VerifiedControlResult(PCI.CTRL_CHARGE, 5.0, oc, "x",
                                      lastcontrol_eligible=False)
        check(f"★★ AF. {oc} → 不得寫入 LastControl",
              st.update_from_verified_result(vr).outcome == LCS.UPD_NOT_UPDATED)
    check("  AF. 執行鏈只在 VERIFY_SUCCESS 之後才呼叫 store",
          "if vr.outcome != EX.VERIFY_SUCCESS:" in io.open(
              os.path.join(HERE, "production_execution_chain.py"),
              encoding="utf-8").read())

    # ---------------- AH~AN. Runtime / 稽核 ----------------
    print("\nAH~AN. Runtime 與稽核")
    rt = RT.AutoControlRuntime(dispatch_enabled=True)
    try:
        rt._make(RT.ST_COMMAND_PENDING, "x", "x")
        pend = False
    except AssertionError:
        pend = True
    check("★★ AH. COMMAND_PENDING 仍需有效授權", pend)
    c = Chain(verify_states=[flags("CHARGING", 5.1)], revalidate=ALWAYS_OK)
    r = c.run()
    check("★★ AI. 重驗發生在授權核發之後、送出之前",
          c.phases.index(PEC.PH_REVALIDATED) > c.phases.index(PEC.PH_AUTHORIZED)
          and c.phases.index(PEC.PH_REVALIDATED)
          < c.phases.index(PEC.PH_DISPATCHED))
    check("★★ AJ. 稽核保留授權 id", r.authorization_id is not None)
    check("★★ AK. 稽核保留 executor 結果",
          r.operator_outcome == EX.COMMAND_ACCEPTED and r.operator_reason is not None)
    check("★★ AL. 稽核保留回讀結果",
          r.readback_outcome == EX.VERIFY_SUCCESS
          and r.readback_observed_state == PCI.PCS_CHARGING
          and r.readback_elapsed_sec is not None)
    check("★★ AM. 稽核保留 LastControl 寫入結果",
          r.lastcontrol_outcome == LCS.UPD_UPDATED and r.lastcontrol_written is True)
    check("★★ AN. dispatch_count 精確為 1",
          c.chain.dispatch_count == 1 and r.dispatch_count_delta == 1)
    check("★★ AO. LastControl 寫入次數精確為 1",
          c.chain.lastcontrol_write_count == 1)
    check("  稽核可序列化", isinstance(r.as_dict(), dict))

    # ---------------- 零實機能力 ----------------
    print("\n零實機能力")
    PCS_PATH = {"device_control_operator", "device_control_menu",
                "device_control_scraper", "api_client", "charge_discharge_report",
                "report_monitor", "phase6_field_measure"}
    hit = _all_imports(_tree("production_execution_chain.py")) & PCS_PATH
    check(f"★★ 執行鏈未 import 任何 PCS 控制路徑（命中={sorted(hit)}）", not hit)
    check("★★ 本測試全程未載入 device_control_operator / api_client",
          "device_control_operator" not in sys.modules
          and "api_client" not in sys.modules)
    check("★★ 未注入 operator_run 的 executor 結構上送不出去",
          EX.PcsControlExecutor().send(
              PCI.ControlRequest(action=PCI.CTRL_CHARGE, target_power_kw=5.0)
          ).reason == EX.R_OPERATOR_NOT_CONFIGURED)

    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-E Production Execution Chain 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
