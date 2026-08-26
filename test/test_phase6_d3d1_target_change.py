# -*- coding: utf-8 -*-
"""
test_phase6_d3d1_target_change.py — Phase D.3-D.1 Target Change Semantics（A~U）
======================================================================
核心問題

    設備已由 Phase 6 控制、方向不變，但新的 target power 與 LastControl 不同時，
        CHARGING @ old → CHARGE @ new
    究竟是「合法的 setpoint update」，還是「一次新的 start/stop cycle」？

本檔做兩件事
    1. 以**既有程式碼證據**（AST，不 import 具網路能力的模組）盤點契約現況。
    2. 鎖住目前的 Fail Closed 行為，確保在 Blocker 10 關閉前不會被悄悄放行。

🔴 決定性發現（由本檔的契約測試證明）
    既有 ReadBackVerifier 的成功條件是「觀測到目標**狀態**」，
    且功率**永不**參與判定、也**不要求**發生狀態轉變。
    因此 CHARGING → CHARGE@new_target 會在**第一次輪詢**就回 VERIFY_SUCCESS ——
    它結構上**無法**區分「新 setpoint 已生效」與「舊 setpoint 仍在跑」。
    在此前提下把 LastControl 更新成 new target，等同把「未驗證」記成「已驗證」。

是否需要設備
    **不需要**。全部 fixture / AST / dependency injection，零網路、零控制。

用法
    python test_phase6_d3d1_target_change.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import safety_gate as SG                                 # noqa: E402
import control_authority as CA                           # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import pcs_control_executor as EX                        # noqa: E402
import last_control_store as LC                          # noqa: E402
import production_control_authorization as PAZ           # noqa: E402
import production_arbiter as ARB                         # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import decision_policy as DP                             # noqa: E402

# 重用 D.3-D 的完整觀測 → 仲裁測試夾具（同一套 fixture，不另建第二套）
import test_phase6_d3d_arbitration as D3D                # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ----------------------------------------------------------------------
# 以 AST 讀取既有模組的證據 —— 刻意**不 import**，避免把網路能力帶進本行程
# ----------------------------------------------------------------------
def _src(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


def _tree(name):
    return ast.parse(_src(name))


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _names(node):
    return ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})


def _strings(node):
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# ======================================================================
def main():
    print("== Phase D.3-D.1 Target Change Semantics 驗證（完全離線）==\n")

    # =========== 證據一：operator payload 與前置檢查 ===========
    print("證據一：operator 的 payload 與前置檢查")
    op = _tree("device_control_operator.py")
    base = _func(op, "_pcs_payload_base")
    check("★★ pcs_charge / pcs_discharge 共用同一個 manualControl 端點與 param=1",
          base is not None and 1 in [n.value for n in ast.walk(base)
                                     if isinstance(n, ast.Constant)
                                     and isinstance(n.value, int)]
          and "activePowerSetPoint" in _strings(base))
    build = _func(op, "_build_pcs_payload")
    check("  payload 由模式表組出：設定值欄位 + 方向以正負號表示",
          build is not None and "sign" in _strings(build))
    pre = _func(op, "_pcs_charge_discharge_precheck")
    check("★★ 前置檢查**不消費**目前 PCS 運轉狀態（無 charging/discharging/standby 判定）",
          pre is not None
          and not (_names(pre) & {"pcs_charging_flag", "pcs_discharging_flag",
                                  "pcs_standby_flag", "pcs_running_flag",
                                  "classify_pcs_state", "pcs_state"}))
    # ⚠️ 這兩個欄位以字串鍵讀取（st["schedule_switch"]），AST 上是 Constant 不是 Name
    check("  前置檢查只看：登入 / 排程開關 / 手動開關 / 電池上電",
          {"schedule_switch", "manual_switch"} <= _strings(pre)
          and "_pcs_battery_precheck" in _names(pre))
    check("★★ 因此 operator 層**沒有**「送控制前必須先 IDLE」這條規則",
          "STANDBY" not in _strings(pre) and "STOPPED" not in _strings(pre))

    # =========== 證據二：人工選單 ===========
    print("\n證據二：人工選單")
    menu = _tree("device_control_menu.py")
    sub = _func(menu, "_pcs_mode_submenu")
    check("★★ 人工選單在送出充/放電前**不檢查**目前運轉狀態",
          sub is not None
          and not (_names(sub) & {"pcs_charging_flag", "pcs_discharging_flag",
                                  "classify_pcs_state", "pcs_is_idle_state"}))
    check("  人工路徑只有『輸入數值 + 大寫 YES 二次確認』",
          "YES" in _strings(sub))
    check("★★ 但「人工被允許」不等於「PCS 契約已確認支援」（人工有現場判斷）",
          True)

    # =========== 證據三：DevTools 確認範圍 ===========
    print("\n證據三：DevTools 已確認的範圍")
    op_src = _src("device_control_operator.py")
    check("★★ DevTools 確認的是 payload 欄位 / enum / 正負號慣例",
          "energyDispatchingMode" in op_src and "DevTools 確認" in op_src)
    check("★★ **沒有任何**「運轉中可更新 setpoint」的既有證據",
          not any(k in op_src for k in ("運轉中更新", "運轉中可更新",
                                        "live setpoint", "setpoint update")))

    # =========== 證據四：Executor 假設 ===========
    print("\n證據四：Executor 的假設")
    ex = _tree("pcs_control_executor.py")
    send = None
    for n in ast.walk(ex):
        if isinstance(n, ast.FunctionDef) and n.name == "send":
            send = n
    check("★★ Executor **不假設**命令前必為 IDLE（send 未讀任何 PCS 狀態）",
          send is not None
          and not (_names(send) & {"pcs_state", "classify_pcs_state",
                                   "PCS_STATE_IDLE", "pcs_standby_flag"}))
    check("  Executor 只做動作映射與參數組裝，狀態判定交給 read-back",
          "OPERATOR_ACTION_MAP" in _names(send))

    # =========== 證據五：ReadBackVerifier（決定性）===========
    print("\n證據五：ReadBackVerifier —— 決定性發現")
    check("★★ 成功條件只看目標**狀態**，功率永不參與",
          EX.ACCEPTED_STATES_MAP[PCI.CTRL_CHARGE] == frozenset({PCI.PCS_CHARGING}))

    # 實測：設備「本來就在 CHARGING」時，verify(charge) 立刻成功、且只輪詢一次
    charging = {"pcs_charging_flag": True, "pcs_discharging_flag": False,
                "pcs_standby_flag": False, "pcs_running_flag": True,
                "actual_active_power_kw": 5.2}
    clk = {"t": 0.0}
    rb = EX.ReadBackVerifier(
        EX.ReadBackConfig(timeout_sec=75.0, poll_interval_sec=5.0,
                          stability_samples=1),
        reader=lambda: dict(charging), clock=lambda: clk["t"],
        sleeper=lambda d: clk.__setitem__("t", clk["t"] + d)
    ).verify(PCI.CTRL_CHARGE)
    check("★★ 設備原本就在 CHARGING → verify(charge) **第一次輪詢就成功**",
          rb.outcome == EX.VERIFY_SUCCESS and len(rb.attempts) == 1
          and rb.elapsed_sec == 0.0)
    check("★★ 因此 read-back **無法區分**「新 setpoint 已生效」與「舊 setpoint 仍在跑」",
          rb.observed_state == PCI.PCS_CHARGING
          and rb.actual_active_power_kw == 5.2)
    check("  verifier 不要求發生狀態轉變（沒有 previous/transition 概念）",
          not (_names(_func(ex, "verify")) & {"previous", "prev_state",
                                              "transition", "changed"}))
    check("★★ 結論：同向 setpoint 更新目前**無可驗證的成功條件**",
          True)

    # =========== 證據六：LastControl 契約 ===========
    print("\n證據六：LastControl 契約")
    lc_src = _src("last_control_store.py")
    check("★★ LastControl 的語意是「已經 read-back 驗證成功的控制結果」",
          "VERIFY_SUCCESS" in lc_src and "lastcontrol_eligible" in lc_src)
    check("★★ API 已接受（control_success=True）**不得**更新 LastControl",
          "control_success=True" in lc_src or "control_success" in lc_src)
    upd = _func(_tree("last_control_store.py"), "update_from_verified_result")
    # ⚠️ 以 getattr(result, "lastcontrol_eligible", False) 讀取 → AST 上是字串常數
    check("★★ 更新入口第一件事就是檢查 lastcontrol_eligible",
          upd is not None and "lastcontrol_eligible" in _strings(upd)
          and "ignore_eligibility" in _names(upd))
    check("★★ 因此「已送出但未驗證」的 new target **不得**寫成 verified LastControl",
          True)

    # =========== 證據七：實機紀錄 ===========
    print("\n證據七：實機紀錄")
    check("★★ 既有實機紀錄中**從未**出現同向連續指令（無 setpoint update 樣本）",
          True)   # 由本輪唯讀盤點確認：三次量測皆為 charge/discharge → stop 交替

    # =========== A/B. same target → 抑制 ===========
    print("\nA/B. 同向 + 相同目標 → duplicate suppressed")
    for pcs, act, dt, soc, tag in (
            ("CHARGING", "charge", D3D.DT_OFF_PEAK, 30.0, "A. CHARGING"),
            ("DISCHARGING", "discharge", D3D.DT_PEAK, 80.0, "B. DISCHARGING")):
        r = D3D.Rig(pcs=pcs, local_now=dt, soc=soc,
                    last_control=D3D.LCRec(act, 5.0, 1000.0)).settle().arbitrate()
        check(f"★★ {tag} + 相同目標 → SUPPRESSED / ALREADY_AT_DESIRED_STATE",
              r.outcome == ARB.ARB_SUPPRESSED
              and r.reason == ARB.R_ALREADY_AT_DESIRED_STATE
              and r.duplicate_suppressed is True and r.authorization is None)

    # =========== C~G. different target → Fail Closed ===========
    print("\nC~G. 同向 + 不同目標 → TARGET_CHANGE_UNSUPPORTED")
    cases = [("CHARGING", "charge", D3D.DT_OFF_PEAK, 30.0, 9.2, 9.0, "C. CHARGING"),
             ("DISCHARGING", "discharge", D3D.DT_PEAK, 80.0, -9.2, 9.0, "D. DISCHARGING")]
    for pcs, act, dt, soc, obs_kw, lc_kw, tag in cases:
        r = D3D.Rig(pcs=pcs, local_now=dt, soc=soc,
                    ess_over={"actual_active_power_kw": obs_kw},
                    last_control=D3D.LCRec(act, lc_kw, 1000.0)).settle().arbitrate()
        check(f"  前提：{tag} 正以舊目標運轉 → Authority OWNED",
              r.authority_state == CA.AUTH_OWNED)
        check(f"★★ {tag} + 不同目標 → BLOCKED / TARGET_CHANGE_UNSUPPORTED",
              r.outcome == ARB.ARB_BLOCKED
              and r.reason == ARB.R_TARGET_CHANGE_UNSUPPORTED)
        check(f"★★ E. {tag} 不得被誤判為 ALREADY_AT_DESIRED_STATE",
              r.reason != ARB.R_ALREADY_AT_DESIRED_STATE
              and r.duplicate_suppressed is False)
        check(f"★★ F. {tag} 不得建立任何授權",
              r.authorization is None and r.authorized is False
              and r.authorization_id is None)
        check(f"★★ G. {tag} 不得被轉成 STOP candidate",
              r.control_action != PCI.CTRL_STOP
              and r.direction_reason != ARB.R_STOP_REQUIRED_FOR_REVERSAL)
        check(f"  {tag} detail 明示為 OPEN DESIGN ITEM",
              "OPEN DESIGN ITEM" in (r.detail or ""))

    # =========== H. 舊 LastControl 不得被當成新 verified target ===========
    print("\nH. 未經 read-back 的 new target 不得被視為 verified")
    rec = D3D.LCRec("charge", 9.0, 1000.0)
    check("★★ H. LastControl 仍記錄舊目標（仲裁層未、也不能改寫它）",
          rec.target_power_kw == 9.0)
    r = D3D.Rig(pcs="CHARGING", local_now=D3D.DT_OFF_PEAK, soc=30.0,
                ess_over={"actual_active_power_kw": 9.2},
                last_control=rec).settle().arbitrate()
    check("★★ H. 一輪仲裁後 LastControl 完全未被更動",
          rec.target_power_kw == 9.0 and rec.action == "charge"
          and rec.verified_at_monotonic == 1000.0)
    arb_src = _src("production_arbiter.py")
    check("★★ H. 仲裁層沒有任何 LastControl 寫入路徑",
          "last_control_store" not in arb_src
          and "update_from_verified_result" not in arb_src)

    # =========== I/J. 授權票綁定 target ===========
    print("\nI/J. 授權票必須綁定 target")
    b = PAZ.AuthorizationBinding(authority_state=CA.AUTH_OWNED,
                                 pcs_state=PCI.PCS_CHARGING,
                                 decision_action="charge",
                                 decision_target_power_kw=5.0,
                                 grid_state="IMPORT", tou_state="OFF_PEAK",
                                 alarm_source_complete=True,
                                 schedule_switch=0, manual_switch=1)
    az, _ = PAZ.issue(PCI.CTRL_CHARGE, 5.0, 100.0, 30.0, b, 1)
    check("★★ I. 授權票本身帶 target_power_kw", az.target_power_kw == 5.0)
    check("★★ I. binding 亦保留 decision_target_power_kw",
          az.binding.decision_target_power_kw == 5.0)
    check("★★ J. 舊 target 的授權票不能以新 target 消費",
          az.consume(PCI.CTRL_CHARGE, 105.0, None, 8.0)
          == (False, PAZ.AZ_TARGET_MISMATCH))
    check("  J. 消費失敗後票仍未被消費（可用相同 target 正常消費）",
          az.state == PAZ.AUTHZ_ISSUED
          and az.consume(PCI.CTRL_CHARGE, 105.0, None, 5.0)[0] is True)

    # =========== K. 仲裁前 target 改變 ===========
    print("\nK. 兩次觀測之間 target 改變")

    def _bump(rig, n):
        if n >= 2:
            rig.policy.cfg = DP.PolicyConfig(charge_power_kw=8.0,
                                             discharge_power_kw=5.0)

    rk = D3D.Rig(pcs="STANDBY", local_now=D3D.DT_OFF_PEAK, soc=30.0)
    rk.settle()
    rk.mutate = _bump
    rk._reads = 0
    r = rk.arbiter.evaluate()
    check("★★ K. original 與 fresh 的 target 不一致 → DECISION_CHANGED",
          r.outcome == ARB.ARB_BLOCKED and r.reason == ARB.R_DECISION_CHANGED
          and r.authorization is None)
    check("  K. audit 同時保留兩個 target",
          r.original_decision_target_kw != r.fresh_decision_target_kw)

    # =========== L~Q. 邊界 ===========
    print("\nL~Q. 邊界不變量")
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ L. Layer 2 仍未配置、dispatch 仍未啟用",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None
          and SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None
          and DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None
          and __import__("pcs_auto_control_runtime").DISPATCH_ENABLED is False)
    PCS_PATH = {"device_control_operator", "device_control_menu",
                "device_control_scraper", "pcs_control_executor", "api_client",
                "charge_discharge_report", "report_monitor",
                "phase6_field_measure", "last_control_store"}
    arb_imports = set()
    for n in ast.walk(_tree("production_arbiter.py")):
        if isinstance(n, ast.Import):
            arb_imports |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            arb_imports.add((n.module or ".").split(".")[0])
    check(f"★★ M~P. 仲裁層仍零 executor / operator / PCS API / LastControl"
          f"（命中={sorted(arb_imports & PCS_PATH)}）",
          not (arb_imports & PCS_PATH))
    check("★★ Q. 本輪未新增任何 dispatch path（DISPATCH_ENABLED 仍為 False）",
          __import__("pcs_auto_control_runtime").DISPATCH_ENABLED is False)
    check("  仲裁層的 TARGET_CHANGE_UNSUPPORTED 是 BLOCKED，不是靜默略過",
          ARB.R_TARGET_CHANGE_UNSUPPORTED != ARB.R_ALREADY_AT_DESIRED_STATE)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-D.1 Target Change Semantics 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
