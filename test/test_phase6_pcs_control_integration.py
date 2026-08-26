# -*- coding: utf-8 -*-
"""
Phase 6.5-A/B/F PCS Control Integration 驗證 —— 完全離線
======================================================================
用途
    驗證 DecisionResult → ControlRequest → SafetyGate → MockExecutor 的完整串接。
    最重要的斷言：**Safety BLOCK 或 ControlRequest none 時，Executor 呼叫次數必須為 0。**

是否需要設備
    **不需要**。完全離線：不登入 HMI、不開 socket、不發 HTTP、不寫檔案、
    不呼叫 device_control_operator、不控制 PCS/BMS。

涵蓋範圍
    A. PCS state parsing     8 種旗標組合
    B. Decision Adapter      8 條映射（含 IDLE 的 5 種分支）
    C. Power                 production power=None 不得被補值；mock 功率僅為 fixture
    D. Safety integration    BLOCK→0 calls、ALLOW→1 call、STOP 仍過 Gate、none 不進 Executor
    E. Dry-run result        NO_CONTROL / SAFETY_BLOCKED / WOULD_EXECUTE ×3
    F. Isolation             無真實控制路徑、無 socket、無 HMI、無檔案 I/O

用法
    python test_phase6_pcs_control_integration.py          # exit 0 = PASS
"""
import os
import sys
import ast

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pcs_control_integration as PCI
import control_authority as CA
import pcs_control_executor as EX                     # noqa: E402

# 於 import 受測模組後立即快照 —— 證明它（含相依）不具備控制／網路能力
_MODULES_AFTER_PCI = set(sys.modules)

import decision_engine as DE                              # noqa: E402
import decision_policy as DP                              # noqa: E402
import safety_gate as SG                                  # noqa: E402

import io as _io2
import os as _os2

SRC_PCI = _io2.open(_os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)),
                                   "pcs_control_integration.py"), encoding="utf-8").read()

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


class Dec:
    """DecisionResult 替身。"""

    def __init__(self, action, power=None, reason="OK"):
        self.action = action
        self.target_power_kw = power
        self.reason = reason


class Stub:
    def __init__(self, state, valid=True):
        self.state = state
        self.valid = valid


def ess(soc=50.0, charging=False, discharging=False, standby=True, **over):
    r = {"communication_ok": True, "soc_percent": soc, "pcs_fault_flag": False,
         "battery_power_status": SG.BATT_ON,
         "pcs_charging_flag": charging, "pcs_discharging_flag": discharging,
         "pcs_standby_flag": standby}
    r.update(over)
    # 🔁 Blocker 13：觀測時戳必須與 NOW／LastControl 同一個時間基準。
    #    原本固定在 100.x，與 NOW=1000 差了 900 秒，Authority 會（正確地）
    #    判定「這是指令之前的資料」。改為緊鄰 NOW 的讀取時刻。
    return DE.ess_snapshot_from_reading(r, 995.0, 995.2, now=995.5)


def inputs(decision, e=None, **over):
    kw = dict(decision=decision, ess=e if e is not None else ess(),
              alarm_rows=(), alarm_source_complete=True,
              pcs_mode_state={"schedule_switch": 0, "manual_switch": 1})
    kw.update(over)
    return PCI.PipelineInputs(**kw)


NOW = 1000.0
CHG, DIS, STOP, NONE = PCI.CTRL_CHARGE, PCI.CTRL_DISCHARGE, PCI.CTRL_STOP, PCI.CTRL_NONE


# ---- Phase 6.5-H：Control Authority 測試輔助 ----
# ⚠️ TTL / tolerance 僅為**測試注入值**，production default 仍為 None，
#    有專屬斷言鎖住（見 Section H）。
TEST_AUTHORITY_POLICY = CA.AuthorityPolicy(authority_ttl_sec=120.0,
                                           authority_power_tolerance_kw=1.0)


class LCRec:
    """last_control_store.LastControlRecord 的極簡替身（Authority 只取這三個欄位）。"""

    def __init__(self, action="charge", power=5.0, at=NOW - 10.0,
                 power_at_verify=-1.3):
    # 🔁 Blocker 13 補齊：真實 LastControlRecord 必有 actual_active_power_kw
    #    （read-back 當下的觀測值）。實機證據顯示該值通常仍是**指令前的閒置值**，
    #    因為 AC 暫存器比旗標慢 0~1 個 refresh cycle。Authority 用它判斷
    #    暫存器是否已更新，因此替身必須提供，且預設為閒置值。
        self.action = action
        self.target_power_kw = power
        self.verified_at_monotonic = at
        self.actual_active_power_kw = power_at_verify


def owned(decision, e=None, action="charge", power=5.0, at=NOW - 10.0, **over):
    """PipelineInputs + 可證明 Phase 6 擁有控制權的 LastControl 證據。"""
    kw = dict(last_control_record=LCRec(action, power, at),
              last_control_trust=CA.TRUST_FOR_INTERVAL)
    kw.update(over)
    return inputs(decision, e, **kw)


def main():
    print("== Phase 6.5-A/B/F PCS Control Integration 驗證（完全離線）==\n")

    # ---------------- A. PCS state parsing ----------------
    print("A. PCS 實際狀態判定（只用三個旗標）")
    cs = PCI.classify_pcs_state
    check("只有 charging=True → CHARGING", cs(True, False, False) == PCI.PCS_CHARGING)
    check("只有 discharging=True → DISCHARGING", cs(False, True, False) == PCI.PCS_DISCHARGING)
    check("只有 standby=True → STANDBY", cs(False, False, True) == PCI.PCS_STANDBY)
    check("全部 False → UNKNOWN（全 False 不等於待機）",
          cs(False, False, False) == PCI.PCS_UNKNOWN)
    check("全部 None → UNKNOWN", cs(None, None, None) == PCI.PCS_UNKNOWN)
    check("charging + discharging → CONFLICT", cs(True, True, False) == PCI.PCS_CONFLICT)
    check("charging + standby → CONFLICT", cs(True, False, True) == PCI.PCS_CONFLICT)
    check("discharging + standby → CONFLICT", cs(False, True, True) == PCI.PCS_CONFLICT)
    check("三個皆 True → CONFLICT", cs(True, True, True) == PCI.PCS_CONFLICT)
    check("非布林值不算 True（1 / \"true\" 皆不視為成立）",
          cs(1, "true", None) == PCI.PCS_UNKNOWN)
    check("ess=None → UNKNOWN", PCI.pcs_state_from_ess(None) == PCI.PCS_UNKNOWN)
    check("由 EssSnapshot 取旗標正確",
          PCI.pcs_state_from_ess(ess(charging=True, standby=False)) == PCI.PCS_CHARGING)
    check("狀態值域封閉", PCI.PCS_STATES == {"CHARGING", "DISCHARGING", "STANDBY",
                                        "STOPPED", "UNKNOWN", "CONFLICT"})

    # ---- 6.5-G 實機狀態模型：running flag 納入分類 ----
    print("\nA2. running flag 納入狀態分類（Phase 6.5-G 實機修正）")
    TRUTH = [
        (False, False, False, False, PCI.PCS_STOPPED,     "1. F,F,F,False -> STOPPED"),
        (False, False, False, True,  PCI.PCS_UNKNOWN,     "2. F,F,F,True  -> UNKNOWN"),
        (False, False, False, None,  PCI.PCS_UNKNOWN,     "3. F,F,F,None  -> UNKNOWN"),
        (False, False, True,  True,  PCI.PCS_STANDBY,     "4. F,F,T,True  -> STANDBY"),
        (False, False, True,  False, PCI.PCS_CONFLICT,    "5. F,F,T,False -> CONFLICT"),
        (True,  False, False, True,  PCI.PCS_CHARGING,    "6. T,F,F,True  -> CHARGING"),
        (True,  False, False, False, PCI.PCS_CONFLICT,    "7. T,F,F,False -> CONFLICT"),
        (False, True,  False, True,  PCI.PCS_DISCHARGING, "8. F,T,F,True  -> DISCHARGING"),
        (False, True,  False, False, PCI.PCS_CONFLICT,    "9. F,T,F,False -> CONFLICT"),
    ]
    for c, d, s, r, want, label in TRUTH:
        check(f"  {label}", cs(c, d, s, r) == want)
    for r in (True, False, None):
        for c, d, s in ((True, True, False), (True, False, True),
                        (False, True, True), (True, True, True)):
            check(f"  10. >=2 個 True（C={c} D={d} S={s}, running={r}）-> CONFLICT",
                  cs(c, d, s, r) == PCI.PCS_CONFLICT)

    print("\nA2b. running=False 時 None 不得被當成 False（Fail Closed 收緊）")
    STRICT = [
        ((False, False, False, False), PCI.PCS_STOPPED,  "1. F/F/F/F      -> STOPPED"),
        ((None,  None,  None,  False), PCI.PCS_UNKNOWN,  "2. None/None/None/F -> UNKNOWN"),
        ((False, None,  False, False), PCI.PCS_UNKNOWN,  "3. F/None/F/F   -> UNKNOWN"),
        ((None,  False, False, False), PCI.PCS_UNKNOWN,  "4. None/F/F/F   -> UNKNOWN"),
        ((False, False, None,  False), PCI.PCS_UNKNOWN,  "5. F/F/None/F   -> UNKNOWN"),
        ((True,  False, False, False), PCI.PCS_CONFLICT, "6. T/F/F/F      -> CONFLICT"),
        ((False, True,  False, False), PCI.PCS_CONFLICT, "7. F/T/F/F      -> CONFLICT"),
        ((False, False, True,  False), PCI.PCS_CONFLICT, "8. F/F/T/F      -> CONFLICT"),
    ]
    for flags, want, label in STRICT:
        check(f"  {label}", cs(*flags) == want)
    stopped_combos = [(c, d, s) for c in (True, False, None)
                      for d in (True, False, None) for s in (True, False, None)
                      if cs(c, d, s, False) == PCI.PCS_STOPPED]
    check("★★ running=False 時，唯一能得到 STOPPED 的組合是 (False, False, False)",
          stopped_combos == [(False, False, False)])
    check("★★ 三旗標任一為 None + running=False → 一律 UNKNOWN（不是 STOPPED）",
          all(cs(c, d, s, False) == PCI.PCS_UNKNOWN
              for c, d, s in ((None, False, False), (False, None, False),
                              (False, False, None), (None, None, False),
                              (None, False, None), (False, None, None),
                              (None, None, None))))
    check("  None 與 False 在 running=False 下語意不同（不可互換）",
          cs(False, False, False, False) != cs(None, False, False, False))
    check("  但『有 True』仍優先判 CONFLICT（即使其他為 None）",
          cs(True, None, None, False) == PCI.PCS_CONFLICT
          and cs(None, True, None, False) == PCI.PCS_CONFLICT
          and cs(None, None, True, False) == PCI.PCS_CONFLICT)
    check("★ 原始碼未以 `not on` 代表三旗標皆 False",
          "return PCS_STOPPED if not on else" not in SRC_PCI)
    check("★ STOPPED 判定明確逐一檢查 is False",
          "charging is False and discharging is False and standby is False" in SRC_PCI)

    print("\nA3. 三個狀態語意必須分離")
    check("★★ STOPPED != STANDBY", PCI.PCS_STOPPED != PCI.PCS_STANDBY)
    check("★★ STOPPED != UNKNOWN", PCI.PCS_STOPPED != PCI.PCS_UNKNOWN)
    check("★★ CONFLICT != UNKNOWN", PCI.PCS_CONFLICT != PCI.PCS_UNKNOWN)
    check("★ STOPPED 屬於封閉值域", PCI.PCS_STOPPED in PCI.PCS_STATES)
    check("★★ STOPPED 不屬於 RUNNING（已離開充放電，不需再送 stop）",
          PCI.PCS_STOPPED not in PCI.PCS_STATE_RUNNING)
    check("★★ STOPPED 不屬於 UNUSABLE（是可信證據，不是無法判斷）",
          PCI.PCS_STOPPED not in PCI.PCS_STATE_UNUSABLE)
    check("★ PCS_STATE_IDLE == {STANDBY, STOPPED}",
          PCI.PCS_STATE_IDLE == {PCI.PCS_STANDBY, PCI.PCS_STOPPED})
    check("  UNKNOWN / CONFLICT 仍在 UNUSABLE（Fail Closed 未被削弱）",
          PCI.PCS_STATE_UNUSABLE == {PCI.PCS_UNKNOWN, PCI.PCS_CONFLICT})

    print("\nA4. 3 參數呼叫的回溯相容（running 預設 None）")
    for c in (True, False, None):
        for d in (True, False, None):
            for s in (True, False, None):
                on = sum(1 for v in (c, d, s) if v is True)
                legacy = (PCI.PCS_CONFLICT if on >= 2 else
                          (PCI.PCS_CHARGING if c is True else
                           PCI.PCS_DISCHARGING if d is True else
                           PCI.PCS_STANDBY if s is True else PCI.PCS_UNKNOWN))
                if cs(c, d, s) != legacy:
                    check(f"  3 參數 ({c},{d},{s}) 行為改變", False)
    check("★★ 全部 27 種 3 參數組合結果與修改前逐一相同", True)
    check("  3 參數與 running=None 等價", all(
        cs(c, d, s) == cs(c, d, s, None)
        for c in (True, False, None) for d in (True, False, None)
        for s in (True, False, None)))

    print("\nA5. pcs_state_from_ess 使用 EssSnapshot 的 running flag")
    class _E:
        def __init__(self, c, d, s, r):
            self.pcs_charging_flag, self.pcs_discharging_flag = c, d
            self.pcs_standby_flag, self.pcs_running_flag = s, r
    check("★★ EssSnapshot(F,F,F,R=False) -> STOPPED",
          PCI.pcs_state_from_ess(_E(False, False, False, False)) == PCI.PCS_STOPPED)
    check("  EssSnapshot(F,F,T,R=True) -> STANDBY",
          PCI.pcs_state_from_ess(_E(False, False, True, True)) == PCI.PCS_STANDBY)
    check("  EssSnapshot(F,F,F,R=None) -> UNKNOWN（欄位缺失仍 Fail Closed）",
          PCI.pcs_state_from_ess(_E(False, False, False, None)) == PCI.PCS_UNKNOWN)
    check("  ess=None -> UNKNOWN", PCI.pcs_state_from_ess(None) == PCI.PCS_UNKNOWN)
    check("★ EssSnapshot 本來就有 pcs_running_flag（Decision Engine 零改動）",
          "pcs_running_flag" in DE.EssSnapshot.__dataclass_fields__)

    print("\nA6. 實機轉態 fixture（本次 5 kW Field Measurement 的四個轉態點）")
    FIELD = [
        ("#102 CHARGE",    (True,  False, False, True),  PCI.PCS_CHARGING),
        ("#132 STOP",      (False, False, False, False), PCI.PCS_STOPPED),
        ("#208 DISCHARGE", (False, True,  False, True),  PCI.PCS_DISCHARGING),
        ("#239 STOP",      (False, False, False, False), PCI.PCS_STOPPED),
    ]
    seq = []
    for label, flags, want in FIELD:
        got = cs(*flags)
        seq.append(got)
        check(f"  {label} C/D/S/R={flags} -> {want}", got == want)
    check("★★ 15. 實機轉態序列 = CHARGING -> STOPPED -> DISCHARGING -> STOPPED",
          seq == [PCI.PCS_CHARGING, PCI.PCS_STOPPED,
                  PCI.PCS_DISCHARGING, PCI.PCS_STOPPED])
    check("★ 修正前這四點會是 CHARGING -> UNKNOWN -> DISCHARGING -> UNKNOWN",
          [cs(*f) for _l, f, _w in FIELD if True] != [
              PCI.PCS_CHARGING, PCI.PCS_UNKNOWN,
              PCI.PCS_DISCHARGING, PCI.PCS_UNKNOWN])

    # ---------------- A7. IDLE + STOPPED ----------------
    print("\nA7. build_control_request：IDLE + STOPPED")

    class _D:
        def __init__(self, action, power=None, reason=""):
            self.action, self.target_power_kw, self.reason = action, power, reason

    stopped_ess = _E(False, False, False, False)
    r = PCI.build_control_request(_D(DE.ACTION_IDLE), ess=stopped_ess)
    check("★★ 12. IDLE + STOPPED -> CTRL_NONE / CR_IDLE_ALREADY_STOPPED",
          r.action == PCI.CTRL_NONE and r.reason == PCI.CR_IDLE_ALREADY_STOPPED)
    check("★★ 不得再落入 fail-closed 的 PCS_STATE_UNKNOWN",
          r.reason != PCI.CR_PCS_STATE_UNKNOWN)
    check("  pcs_actual_state 如實記錄 STOPPED（未被打回 UNKNOWN）",
          r.pcs_actual_state == PCI.PCS_STOPPED)
    check("  IDLE + STANDBY 行為不變（IDLE_ALREADY_STANDBY）",
          PCI.build_control_request(_D(DE.ACTION_IDLE),
                                    ess=_E(False, False, True, True)).reason
          == PCI.CR_IDLE_ALREADY_STANDBY)
    check("  IDLE + CHARGING 仍要求 STOP",
          PCI.build_control_request(_D(DE.ACTION_IDLE),
                                    ess=_E(True, False, False, True)).action == PCI.CTRL_STOP)
    check("  IDLE + UNKNOWN 仍 Fail Closed",
          PCI.build_control_request(_D(DE.ACTION_IDLE),
                                    ess=_E(False, False, False, None)).reason
          == PCI.CR_PCS_STATE_UNKNOWN)
    check("  IDLE + CONFLICT 仍 Fail Closed",
          PCI.build_control_request(_D(DE.ACTION_IDLE),
                                    ess=_E(True, False, True, True)).reason
          == PCI.CR_PCS_STATE_CONFLICT)
    rc_ = PCI.build_control_request(_D(DE.ACTION_CHARGE, 5.0), ess=stopped_ess)
    rd_ = PCI.build_control_request(_D(DE.ACTION_DISCHARGE, 5.0), ess=stopped_ess)
    check("★★ 13. STOPPED 下 ACTION_CHARGE 行為不變（CTRL_CHARGE + 功率原樣帶出）",
          rc_.action == PCI.CTRL_CHARGE and rc_.reason == PCI.CR_DECISION_CHARGE
          and rc_.target_power_kw == 5.0)
    check("★★ 13. STOPPED 下 ACTION_DISCHARGE 行為不變",
          rd_.action == PCI.CTRL_DISCHARGE and rd_.reason == PCI.CR_DECISION_DISCHARGE
          and rd_.target_power_kw == 5.0)
    check("  STOPPED 下 ACTION_NO_ACTION 行為不變",
          PCI.build_control_request(_D(DE.ACTION_NO_ACTION), ess=stopped_ess).action
          == PCI.CTRL_NONE)

    # ---------------- B. Decision Adapter ----------------
    print("\nB. Decision → ControlRequest")
    bcr = PCI.build_control_request
    r = bcr(Dec(DE.ACTION_IDLE), ess(standby=True))
    check("IDLE + STANDBY → none / IDLE_ALREADY_STANDBY",
          r.action == NONE and r.reason == PCI.CR_IDLE_ALREADY_STANDBY)
    r = bcr(Dec(DE.ACTION_IDLE), ess(charging=True, standby=False))
    check("★ IDLE + CHARGING → stop / IDLE_STOP_REQUIRED",
          r.action == STOP and r.reason == PCI.CR_IDLE_STOP_REQUIRED)
    r = bcr(Dec(DE.ACTION_IDLE), ess(discharging=True, standby=False))
    check("★ IDLE + DISCHARGING → stop / IDLE_STOP_REQUIRED",
          r.action == STOP and r.reason == PCI.CR_IDLE_STOP_REQUIRED)
    r = bcr(Dec(DE.ACTION_IDLE), ess(standby=False))
    check("★ IDLE + UNKNOWN → none / PCS_STATE_UNKNOWN（Fail Closed，不猜 stop）",
          r.action == NONE and r.reason == PCI.CR_PCS_STATE_UNKNOWN)
    r = bcr(Dec(DE.ACTION_IDLE), ess(charging=True, standby=True))
    check("★ IDLE + CONFLICT → none / PCS_STATE_CONFLICT（Fail Closed）",
          r.action == NONE and r.reason == PCI.CR_PCS_STATE_CONFLICT)
    r = bcr(Dec(DE.ACTION_NO_ACTION, reason="POLICY_POWER_NOT_CONFIGURED"), ess())
    check("NO_ACTION → none / DECISION_NO_ACTION",
          r.action == NONE and r.reason == PCI.CR_DECISION_NO_ACTION)
    r = bcr(Dec(DE.ACTION_CHARGE, 30.0), ess())
    check("CHARGE → charge，功率原樣帶出",
          r.action == CHG and r.target_power_kw == 30.0)
    r = bcr(Dec(DE.ACTION_DISCHARGE, 40.0), ess())
    check("DISCHARGE → discharge，功率原樣帶出",
          r.action == DIS and r.target_power_kw == 40.0)
    check("decision=None → none / DECISION_MISSING",
          bcr(None, ess()).reason == PCI.CR_DECISION_MISSING)
    check("未知 decision action → none / DECISION_INVALID_ACTION",
          bcr(Dec("teleport"), ess()).reason == PCI.CR_DECISION_INVALID_ACTION)
    check("ControlRequest 記錄 decision_action 與 pcs_actual_state 供回溯",
          (lambda x: x.decision_action == DE.ACTION_IDLE
           and x.pcs_actual_state == PCI.PCS_CHARGING)(
              bcr(Dec(DE.ACTION_IDLE), ess(charging=True, standby=False))))
    check("needs_control：charge/discharge/stop 為 True、none 為 False",
          bcr(Dec(DE.ACTION_CHARGE, 1.0), ess()).needs_control
          and not bcr(Dec(DE.ACTION_NO_ACTION), ess()).needs_control)
    check("★ none 與 stop 是不同語意（不得混用）",
          NONE != STOP and NONE not in PCI.CONTROL_ACTIONS and STOP in PCI.CONTROL_ACTIONS)
    check("actual_active_power_kw 不參與狀態判定",
          PCI.pcs_state_from_ess(ess(standby=False, actual_active_power_kw=-80.0))
          == PCI.PCS_UNKNOWN)

    # ---------------- C. Power ----------------
    print("\nC. 功率（production 未設定不得被補值）")
    eng = DE.DecisionEngine(policy=DP.TouArbitragePolicy(DP.DEFAULT_POLICY_CONFIG,
                                                         clock=lambda: NOW))
    dres = eng.decide(DE.DecisionInput(Stub("IMPORT"), Stub("PEAK"), ess()))
    check("★ 真實 policy（power=None）在 PEAK+IMPORT 下輸出 no_action",
          dres.action == DE.ACTION_NO_ACTION
          and dres.reason == DP.R_POWER_NOT_CONFIGURED)
    cr = PCI.build_control_request(dres, ess())
    check("★ Adapter 維持 none，未自行補功率",
          cr.action == NONE and cr.target_power_kw is None)
    check("DEFAULT_POLICY_CONFIG 的功率仍為 None",
          DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.discharge_power_kw is None)
    check("mock 功率可建立 charge request（僅測試 fixture）",
          PCI.build_control_request(Dec(DE.ACTION_CHARGE, 12.5), ess()).target_power_kw == 12.5)
    check("mock 功率可建立 discharge request（僅測試 fixture）",
          PCI.build_control_request(Dec(DE.ACTION_DISCHARGE, 7.5), ess()).target_power_kw == 7.5)
    check("Adapter 不做功率驗證（power=None 的 charge 仍原樣帶出，交由 Safety Gate）",
          PCI.build_control_request(Dec(DE.ACTION_CHARGE, None), ess()).action == CHG)
    check("SafetyGate 預設 max_power_kw / min_switch_interval_sec 仍為 None",
          SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None)

    # ---------------- D. Safety integration ----------------
    print("\nD. Safety Gate 串接")
    ex = PCI.MockExecutor()
    pipe = PCI.DryRunPipeline(executor=ex)

    ex.reset()
    res = pipe.run(inputs(Dec(DE.ACTION_IDLE), ess(standby=True)), now=NOW)
    check("★ ControlRequest none → executor 呼叫 0 次",
          res.outcome == PCI.OUT_NO_CONTROL and ex.call_count == 0
          and res.executor_called is False)
    check("  none 不建立 SafetyRequest",
          PCI.build_safety_request(res.control_request, inputs(None)) is None)
    check("  none 時不呼叫 Safety Gate（safety_result 為 None）", res.safety_result is None)

    ex.reset()
    res = pipe.run(inputs(Dec(DE.ACTION_CHARGE, 30.0), ess(soc=99.0)), now=NOW)
    check("★ Safety BLOCK → executor 呼叫 0 次",
          res.outcome == PCI.OUT_SAFETY_BLOCKED and ex.call_count == 0
          and res.executor_called is False)
    check("  BLOCK 保留 reason 與完整 checks",
          res.reason == SG.R_SOC_AT_MAX_LIMIT and len(res.safety_result.checks) == 14)

    ex.reset()
    res = pipe.run(inputs(Dec(DE.ACTION_CHARGE, 30.0), ess()), now=NOW)
    check("★ Safety ALLOW → executor 恰呼叫 1 次",
          res.outcome == PCI.OUT_WOULD_EXECUTE and ex.call_count == 1
          and res.executor_called is True)
    check("  executor 收到正確 action / power",
          ex.calls[0]["action"] == CHG and ex.calls[0]["target_power_kw"] == 30.0)

    # Phase 6.5-H：STOP 的前提是 Phase 6 確實擁有控制權，否則不得中止他人作業。
    # 原不變量（STOP 不得跳過 Safety Gate）完整保留，只是補上 Authority 證據。
    pipe_owned = PCI.DryRunPipeline(executor=ex, authority_policy=TEST_AUTHORITY_POLICY)
    owned_charging = ess(charging=True, standby=False, pcs_running_flag=True,
                         actual_active_power_kw=5.3)
    ex.reset()
    res = pipe_owned.run(owned(Dec(DE.ACTION_IDLE), owned_charging), now=NOW)
    check("★ STOP 仍必須經過 Safety Gate（safety_result 存在）",
          res.safety_result is not None and res.control_request.action == STOP)
    check("  STOP 通過 → executor 呼叫 1 次", ex.call_count == 1)
    check("★★ 且 Authority 為 OWNED_BY_PHASE6（不是被跳過）",
          res.authority_result.state == CA.AUTH_OWNED
          and res.control_request.authority_state == CA.AUTH_OWNED)
    check("★★ Authority ALLOW 不得 bypass Safety Gate（14 項仍全數評估）",
          len(res.safety_result.checks) == 14)

    # 🔴 新行為：外部作業進行中（無 LastControl）→ 不得送 STOP
    ex.reset()
    res = pipe.run(inputs(Dec(DE.ACTION_IDLE), ess(charging=True, standby=False)), now=NOW)
    check("★★ K. 外部控制中 + Decision STOP → AUTHORITY_BLOCKED，不得中止他人作業",
          res.outcome == PCI.OUT_AUTHORITY_BLOCKED
          and res.reason == CA.CA_EXTERNAL
          and res.control_request.action == NONE and ex.call_count == 0)
    check("  且未進入 Safety Gate（safety_result is None）", res.safety_result is None)
    check("  reason 明確指出是控制權問題，不是 SAFETY_FAIL",
          res.reason.startswith("CONTROL_AUTHORITY_"))

    # ESS 通訊失敗時，EssSnapshot 會清空所有欄位 → PCS 狀態不可知 →
    # Adapter 在建立 request 前就 Fail Closed，根本到不了 Safety Gate。
    # Adapter 層（不帶 authority）：原斷言逐字保留 —— 這是 Adapter 自身的 Fail Closed
    bad_ess = ess(charging=True, standby=False, communication_ok=False)
    cr_adapter = PCI.build_control_request(Dec(DE.ACTION_IDLE), ess=bad_ess)
    check("★ ESS 通訊失敗 → PCS 狀態不可知 → Adapter 先 Fail Closed（none / PCS_STATE_UNKNOWN）",
          cr_adapter.action == NONE and cr_adapter.reason == PCI.CR_PCS_STATE_UNKNOWN)
    # Pipeline 層：Authority 是更外層的閘門，會先以「資料不足」擋下（同樣 Fail Closed）
    ex.reset()
    res = pipe.run(inputs(Dec(DE.ACTION_IDLE), bad_ess), now=NOW)
    check("★★ Pipeline：ESS 無效 → Authority 先 Fail Closed（UNKNOWN / DATA_INSUFFICIENT）",
          res.outcome == PCI.OUT_AUTHORITY_BLOCKED
          and res.authority_result.state == CA.AUTH_UNKNOWN
          and res.reason == CA.CA_DATA_INSUFFICIENT)
    check("  executor 0 次，且未進入 Safety Gate",
          ex.call_count == 0 and res.safety_result is None)

    # 結構性性質：STOP 只在 PCS 確定為 CHARGING/DISCHARGING 時建立，
    # 而那需要一份 valid（communication_ok=True）的 EssSnapshot；
    # Safety Gate 對 STOP 只檢查 ess_present + ess_communication，兩者此時必然通過。
    # → 本 pipeline 中 STOP 永遠不會被 Safety Gate 擋下（安全停止優先的預期結果）。
    # Safety Gate 的 STOP 豁免不變 —— 在 Phase 6 確實擁有控制權時，
    # 各種惡劣條件（SOC 極值 / 電池下電 / PCS 故障 / 作用中嚴重告警 / 告警來源不完整）
    # 都不得妨礙停止。原不變量完整保留，只補上 Authority 證據並把排程主開關設為 OFF。
    ex.reset()
    stop_outcomes = set()
    # 🔁 Blocker 13 修正 fixture：放電時觀測到的 AC 有功是**負值**（實機四筆一致）。
    #    原本填 +5.4 與實機相反；舊版功率比對取絕對值，因此這個錯誤被掩蓋。
    for st_kw, act, pw in (({"charging": True, "standby": False}, "charge", 5.3),
                           ({"discharging": True, "standby": False}, "discharge", -5.4)):
        for extra in ({}, {"soc": 99.5}, {"soc": 0.5}, {"battery_power_status": SG.BATT_OFF},
                      {"pcs_fault_flag": True}):
            e2 = ess(**{**st_kw, **extra}, pcs_running_flag=True, actual_active_power_kw=pw)
            r2 = pipe_owned.run(
                owned(Dec(DE.ACTION_IDLE), e2, action=act, power=5.0,
                      alarm_rows=({"level": 0, "alarmStatus": True},),
                      alarm_source_complete=None,
                      pcs_mode_state={"schedule_switch": 0, "manual_switch": 1}),
                now=NOW)
            stop_outcomes.add((r2.control_request.action, r2.outcome))
    check(f"★ STOP 在各種惡劣條件下皆通過 Safety Gate（安全停止優先）：{sorted(stop_outcomes)}",
          stop_outcomes == {(STOP, PCI.OUT_WOULD_EXECUTE)})

    # 🔴 新行為：排程主開關 ON → 連 STOP 都不得送出（PCS 原生排程已取得控制來源）
    ex.reset()
    sched_outcomes = set()
    for st_kw, act, pw in (({"charging": True, "standby": False}, "charge", 5.3),
                           ({"discharging": True, "standby": False}, "discharge", 5.4)):
        e2 = ess(**st_kw, pcs_running_flag=True, actual_active_power_kw=pw)
        r2 = pipe_owned.run(
            owned(Dec(DE.ACTION_IDLE), e2, action=act, power=5.0,
                  pcs_mode_state={"schedule_switch": 1, "manual_switch": 0}), now=NOW)
        sched_outcomes.add((r2.control_request.action, r2.outcome, r2.reason))
    check("★★ I. 排程主開關 ON → 連 STOP 也 AUTHORITY_BLOCKED / SCHEDULE_ACTIVE",
          sched_outcomes == {(NONE, PCI.OUT_AUTHORITY_BLOCKED, CA.CA_SCHEDULE_ACTIVE)}
          and ex.call_count == 0)
    check("★★ Safety Gate 自身的 STOP 豁免未被修改（仍在更內層生效）",
          SG.STOP_CHECKS == (SG.C_REQUEST, SG.C_ESS_PRESENT, SG.C_ESS_COMM))

    ex.reset()
    for e2, label in ((ess(pcs_fault_flag=True), "PCS 故障"),
                      (ess(battery_power_status=SG.BATT_OFF), "電池下電"),
                      (ess(soc=50.0), "告警來源不完整")):
        kw = {} if label != "告警來源不完整" else {"alarm_source_complete": None}
        pipe.run(inputs(Dec(DE.ACTION_CHARGE, 30.0), e2, **kw), now=NOW)
    check("★ 三種 Safety 阻擋情境累計 executor 呼叫仍為 0", ex.call_count == 0)

    ex.reset()
    res = pipe.run(inputs(Dec(DE.ACTION_CHARGE, 30.0), ess(),
                          pcs_mode_state={"schedule_switch": 1, "manual_switch": 0}), now=NOW)
    # Phase 6.5-H：智慧模式現在由 Control Authority 在更外層擋下，reason 更精確。
    # 原本的 Safety Gate 規則**沒有被移除** —— 下一項直接對 Gate 斷言證明。
    check("★★ 智慧模式 → AUTHORITY_BLOCKED / SCHEDULE_ACTIVE，executor 0 次",
          res.outcome == PCI.OUT_AUTHORITY_BLOCKED
          and res.reason == CA.CA_SCHEDULE_ACTIVE and ex.call_count == 0)
    check("★★ Safety Gate 的智慧模式規則仍然存在（未因新增 Authority 而移除）",
          SG.SafetyGate().check(
              SG.SafetyRequest(requested_action=SG.REQ_CHARGE, target_power_kw=30.0,
                               ess=ess(), alarm_rows=(), alarm_source_complete=True,
                               pcs_mode_state={"schedule_switch": 1, "manual_switch": 0}),
              now=NOW).reason == SG.R_CONTROL_MODE_SMART)

    # ---------------- E. Dry-run result ----------------
    print("\nE. DryRunResult 分類")
    ex.reset()
    outcomes = {}
    for label, dec, e2 in (
            ("NO_CONTROL", Dec(DE.ACTION_IDLE), ess(standby=True)),
            ("SAFETY_BLOCKED", Dec(DE.ACTION_CHARGE, 30.0), ess(soc=99.0)),
            ("WOULD_EXECUTE charge", Dec(DE.ACTION_CHARGE, 30.0), ess()),
            ("WOULD_EXECUTE discharge", Dec(DE.ACTION_DISCHARGE, 40.0), ess()),
            ("WOULD_EXECUTE stop", Dec(DE.ACTION_IDLE),
             ess(discharging=True, standby=False, pcs_running_flag=True,
                 actual_active_power_kw=-5.4))):
        # stop 需要 Phase 6 確實擁有控制權，否則會（正確地）被 Authority 擋下
        if label.endswith("stop"):
            r = pipe_owned.run(owned(dec, e2, action="discharge", power=5.0), now=NOW)
        else:
            r = pipe.run(inputs(dec, e2), now=NOW)
        outcomes[label] = (r.outcome, r.control_request.action)
    r_ext = pipe.run(inputs(Dec(DE.ACTION_IDLE),
                            ess(discharging=True, standby=False)), now=NOW)
    outcomes["AUTHORITY_BLOCKED"] = (r_ext.outcome, r_ext.control_request.action)
    check(f"NO_CONTROL：{outcomes['NO_CONTROL']}",
          outcomes["NO_CONTROL"] == (PCI.OUT_NO_CONTROL, NONE))
    check(f"SAFETY_BLOCKED：{outcomes['SAFETY_BLOCKED']}",
          outcomes["SAFETY_BLOCKED"] == (PCI.OUT_SAFETY_BLOCKED, CHG))
    check(f"WOULD_EXECUTE charge：{outcomes['WOULD_EXECUTE charge']}",
          outcomes["WOULD_EXECUTE charge"] == (PCI.OUT_WOULD_EXECUTE, CHG))
    check(f"WOULD_EXECUTE discharge：{outcomes['WOULD_EXECUTE discharge']}",
          outcomes["WOULD_EXECUTE discharge"] == (PCI.OUT_WOULD_EXECUTE, DIS))
    check(f"WOULD_EXECUTE stop：{outcomes['WOULD_EXECUTE stop']}",
          outcomes["WOULD_EXECUTE stop"] == (PCI.OUT_WOULD_EXECUTE, STOP))
    check(f"★★ AUTHORITY_BLOCKED：{outcomes['AUTHORITY_BLOCKED']}",
          outcomes["AUTHORITY_BLOCKED"] == (PCI.OUT_AUTHORITY_BLOCKED, NONE))
    check("executor 只被 WOULD_EXECUTE 的 3 次呼叫", ex.call_count == 3)
    check("★★ 五種 outcome 皆可達成且互斥",
          len({v[0] for v in outcomes.values()}) == 4
          and PCI.OUT_AUTHORITY_BLOCKED in {v[0] for v in outcomes.values()})
    check("would_send 僅在 WOULD_EXECUTE 為 True",
          pipe.run(inputs(Dec(DE.ACTION_CHARGE, 30.0), ess()), now=NOW).would_send
          and not pipe.run(inputs(Dec(DE.ACTION_IDLE), ess(standby=True)), now=NOW).would_send)
    check("★ 本階段不會產生 SAFETY_ALLOW_OPERATOR_BLOCKED（保留給 6.5-C）",
          PCI.OUT_SAFETY_ALLOW_OPERATOR_BLOCKED not in
          {pipe.run(inputs(d, e2), now=NOW).outcome
           for d, e2 in ((Dec(DE.ACTION_CHARGE, 30.0), ess()),
                         (Dec(DE.ACTION_IDLE), ess(charging=True, standby=False)),
                         (Dec(DE.ACTION_NO_ACTION), ess()))})

    # 全矩陣：decision × PCS 狀態，斷言 executor 不會在不該被呼叫時被呼叫
    ex.reset()
    bad = []
    for da in (DE.ACTION_CHARGE, DE.ACTION_DISCHARGE, DE.ACTION_IDLE, DE.ACTION_NO_ACTION):
        for st_kw in ({"standby": True}, {"charging": True, "standby": False},
                      {"discharging": True, "standby": False}, {"standby": False},
                      {"charging": True, "standby": True}):
            before = ex.call_count
            r = pipe.run(inputs(Dec(da, 30.0), ess(**st_kw)), now=NOW)
            called = ex.call_count - before
            if r.outcome != PCI.OUT_WOULD_EXECUTE and called != 0:
                bad.append((da, st_kw, r.outcome, called))
            if r.outcome == PCI.OUT_WOULD_EXECUTE and called != 1:
                bad.append((da, st_kw, r.outcome, called))
    check(f"★ 20 組全矩陣：非 WOULD_EXECUTE 一律 0 次呼叫、WOULD_EXECUTE 恰 1 次（違規={len(bad)}）",
          not bad)

    # ---------------- F. Isolation ----------------
    print("\nF. Isolation（不得接觸真實控制路徑）")
    for m in ("device_control_operator", "device_control_menu", "api_client",
              "charge_discharge_report", "report_monitor", "requests", "socketio",
              "urllib", "socket", "meter_client", "power_classifier"):
        check(f"runtime 未載入 {m}", m not in _MODULES_AFTER_PCI)

    src = open(PCI.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    check(f"imports 僅標準庫 + decision_engine + safety_gate + control_authority："
          f"{sorted(imported)}",
          imported <= {"sys", "math", "time", "argparse", "dataclasses",
                       "decision_engine", "safety_gate", "control_authority"})
    ca_tree = ast.parse(_io2.open(_os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)),
                                  "control_authority.py"),
                                  encoding="utf-8").read())
    ca_imp = set()
    for n in ast.walk(ca_tree):
        if isinstance(n, ast.Import):
            ca_imp |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            ca_imp.add(n.module.split(".")[0])
    check(f"★★ control_authority 本身只依賴標準庫（零 I/O、無循環 import）：{sorted(ca_imp)}",
          ca_imp <= {"math", "dataclasses"})
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    io_calls = (calls & {"open", "input", "eval", "exec", "compile", "__import__"}) | \
               (attrs & {"dump", "load", "urlopen", "connect", "emit", "request",
                         "send", "login_hmi"})
    check(f"無檔案／網路／登入呼叫（命中={sorted(io_calls)}）", not io_calls)
    idents = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    idents |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    # 註：不把 `run` 列入黑名單 —— DryRunPipeline.run() 是本模組自己的方法名，會誤判。
    #     真實控制路徑的隔離改由 imports 與 sys.modules 檢查保證（更強）。
    banned = idents & {"ApiClient", "login_hmi", "build_verify", "ACTIONS",
                       "RESULT_PATH", "_compute_success", "_resolve_payload",
                       "_build_pcs_payload"}
    check(f"未引用既有控制執行元件（命中={sorted(banned)}）", not banned)
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    check(f"未出現 150 / 249 / 229（命中={sorted(consts & {150, 249, 229})}）",
          not (consts & {150, 249, 229}))
    check("未建立 production LastControl（模組未定義 LastControl）",
          not hasattr(PCI, "LastControl"))
    check("未設定 read-back timeout / poll interval",
          not (idents & {"timeout_sec", "poll_interval_sec", "verify_timeout_sec"}))
    check("Executor 預設為 MockExecutor",
          isinstance(PCI.DryRunPipeline().executor, PCI.MockExecutor))
    check("MockExecutor 明示不執行（回傳 executed=False）",
          PCI.MockExecutor()(PCI.build_control_request(Dec(DE.ACTION_CHARGE, 1.0), ess()),
                             None)["executed"] is False)

    # ---------------- S. STOPPED lifecycle replay regression ----------------
    # 🔴 目的不是重測 PCS，而是鎖住「STOPPED 是合法的可啟動閒置狀態」。
    #    若日後有人修改 pcs_state 分類 / Control Authority / Control Integration /
    #    Executor，把 STOPPED 變成「不可啟動」，本節會立即失敗。
    #
    # 證據來源（Phase 6.5-G 實機，2026-08-20 pcs_samples/events_20260820_170911）：
    #    seq 162~207  STOPPED (C=F D=F S=F R=F)  AC -1.4/-1.3  dcP 0.00
    #                 ↑ Stage 5.3 DISCHARGE COMMAND_SENT @ +168.59s（此時 state = STOPPED）
    #    seq 208      DISCHARGING (C=F D=T S=F R=T)  dcP -5.30
    #    → 全程沒有任何 START / ENABLE command；啟動語意內含於 manualControl 的 param=1。
    print("\nS. STOPPED lifecycle（實機證據固化）")

    # 取自上述實機 CSV 的旗標組合（inline fixture，不依賴 output/ 檔案是否留存）
    FIELD_STOPPED = {"pcs_charging_flag": False, "pcs_discharging_flag": False,
                     "pcs_standby_flag": False, "pcs_running_flag": False,
                     "actual_active_power_kw": -1.3}
    FIELD_DISCHARGING = {"pcs_charging_flag": False, "pcs_discharging_flag": True,
                         "pcs_standby_flag": False, "pcs_running_flag": True,
                         "actual_active_power_kw": -1.6}

    # --- A. 旗標 → STOPPED ---
    check("★★ A. 實機旗標 C=F D=F S=F R=F → pcs_state = STOPPED",
          PCI.classify_pcs_state(False, False, False, False) == PCI.PCS_STOPPED)
    stopped_ess = ess(**{k: v for k, v in FIELD_STOPPED.items()
                         if k != "actual_active_power_kw"},
                      actual_active_power_kw=-1.3, standby=False)
    check("  A. 由 EssSnapshot 導出亦為 STOPPED",
          PCI.pcs_state_from_ess(stopped_ess) == PCI.PCS_STOPPED)
    check("  A. STOPPED 屬於 PCS_STATE_IDLE，且不在 UNUSABLE",
          PCI.PCS_STOPPED in PCI.PCS_STATE_IDLE
          and PCI.PCS_STOPPED not in PCI.PCS_STATE_UNUSABLE)

    # --- B. Control Authority：STOPPED → IDLE / allowed ---
    a_stopped = CA.evaluate(PCI.build_authority_request(
        PCI.PipelineInputs(ess=stopped_ess,
                           pcs_mode_state={"schedule_switch": 0, "manual_switch": 1})),
        now=NOW)
    check("★★ B. Control Authority：STOPPED → IDLE / allowed=True",
          a_stopped.state == CA.AUTH_IDLE and a_stopped.allowed is True
          and a_stopped.reason == CA.CA_IDLE)

    # --- C. Decision=DISCHARGE 必須產生可執行的 CTRL_DISCHARGE ---
    cr_dis = PCI.build_control_request(Dec(DE.ACTION_DISCHARGE, 5.0), ess=stopped_ess,
                                       authority=a_stopped)
    check("★★ C. STOPPED + Decision DISCHARGE → CTRL_DISCHARGE（非 none / 非 BLOCKED）",
          cr_dis.action == PCI.CTRL_DISCHARGE and cr_dis.reason == PCI.CR_DECISION_DISCHARGE
          and cr_dis.target_power_kw == 5.0)
    check("  C. 且 authority_state 如實記為 STOPPED 下的 IDLE",
          cr_dis.authority_state == CA.AUTH_IDLE
          and cr_dis.pcs_actual_state == PCI.PCS_STOPPED)

    # --- D. Safety Gate 不因 pcs_state 本身阻擋 ---
    for act in (SG.REQ_CHARGE, SG.REQ_DISCHARGE):
        sres = SG.SafetyGate().check(SG.SafetyRequest(
            requested_action=act, target_power_kw=5.0, ess=stopped_ess,
            alarm_rows=(), alarm_source_complete=True,
            pcs_mode_state={"schedule_switch": 0, "manual_switch": 1}), now=NOW)
        check(f"★★ D. Safety Gate {act}：STOPPED 起始不被 pcs_state 阻擋（{sres.reason}）",
              sres.allowed is True and sres.reason == SG.SAFE_OK)
    check("  D. Safety Gate 根本不消費 pcs_state（14 項中無此檢查）",
          "pcs_state" not in {c.name for c in SG.SafetyGate().check(
              SG.SafetyRequest(requested_action=SG.REQ_DISCHARGE, target_power_kw=5.0,
                               ess=stopped_ess, alarm_rows=(), alarm_source_complete=True,
                               pcs_mode_state={"schedule_switch": 0, "manual_switch": 1}),
              now=NOW).checks})

    # --- E/F. Executor：起始 STOPPED，等待目標 DISCHARGING（實機轉態 replay）---
    seq = [dict(FIELD_STOPPED), dict(FIELD_STOPPED), dict(FIELD_DISCHARGING)]
    box = {"i": 0}

    def _replay_reader():
        r = seq[min(box["i"], len(seq) - 1)]
        box["i"] += 1
        return dict(r)

    clk = {"t": 0.0}
    rb = EX.ReadBackVerifier(
        EX.ReadBackConfig(timeout_sec=75.0, poll_interval_sec=5.0, stability_samples=1),
        reader=_replay_reader, clock=lambda: clk["t"],
        sleeper=lambda d: clk.__setitem__("t", clk["t"] + d)).verify(PCI.CTRL_DISCHARGE)
    check("★★ E/F. 起始 STOPPED → DISCHARGE read-back 成功轉態到 DISCHARGING",
          rb.outcome == EX.VERIFY_SUCCESS and rb.observed_state == PCI.PCS_DISCHARGING
          and rb.target_state == PCI.PCS_DISCHARGING)
    check("  E. 起始的 STOPPED 觀測不會被誤判為失敗（只是尚未達標，繼續輪詢）",
          len(rb.attempts) == 3 and rb.attempts[0]["state"] == PCI.PCS_STOPPED)
    check("★★ F. 全程未使用任何 START / ENABLE 動作（executor 只認得三個控制動作）",
          set(EX.TARGET_STATE_MAP) == {PCI.CTRL_CHARGE, PCI.CTRL_DISCHARGE, PCI.CTRL_STOP})
    check("★★ F. operator 亦無 START / ENABLE / RUN / STANDBY 動作",
          not {"pcs_start", "pcs_enable", "pcs_run", "pcs_standby"}
          & set(EX.OPERATOR_ACTION_MAP.values()))

    # --- G. STOPPED + 舊 LastControl 仍為 IDLE ---
    class _OldLC:
        action, target_power_kw, verified_at_monotonic = "discharge", 5.0, NOW - 99999.0
        actual_active_power_kw = -1.3          # 🔁 Blocker 13 補齊

    a_old = CA.evaluate(PCI.build_authority_request(
        PCI.PipelineInputs(ess=stopped_ess,
                           pcs_mode_state={"schedule_switch": 0, "manual_switch": 1},
                           last_control_record=_OldLC(),
                           last_control_trust=CA.TRUST_FOR_INTERVAL)), now=NOW)
    check("★★ G. STOPPED + 很舊的 LastControl → 仍為 IDLE（舊紀錄不構成設備占用）",
          a_old.state == CA.AUTH_IDLE and a_old.allowed is True)

    # --- H. STOPPED → CHARGE 亦可建立控制請求 ---
    cr_chg = PCI.build_control_request(Dec(DE.ACTION_CHARGE, 5.0), ess=stopped_ess,
                                       authority=a_stopped)
    check("★★ H. STOPPED + Decision CHARGE → CTRL_CHARGE（與 DISCHARGE 對稱）",
          cr_chg.action == PCI.CTRL_CHARGE and cr_chg.reason == PCI.CR_DECISION_CHARGE
          and cr_chg.target_power_kw == 5.0)
    check("  H. IDLE 決策在 STOPPED 下仍為「不需控制」而非 Fail Closed",
          PCI.build_control_request(Dec(DE.ACTION_IDLE), ess=stopped_ess,
                                    authority=a_stopped).reason
          == PCI.CR_IDLE_ALREADY_STOPPED)

    # --- 端到端：STOPPED 起始的完整 pipeline 必須可執行 ---
    ex.reset()
    res = pipe.run(inputs(Dec(DE.ACTION_DISCHARGE, 5.0), stopped_ess), now=NOW)
    check("★★ 端到端：STOPPED 起始 + Decision DISCHARGE → WOULD_EXECUTE",
          res.outcome == PCI.OUT_WOULD_EXECUTE and res.control_request.action == DIS
          and ex.call_count == 1)
    check("  端到端：Authority 為 IDLE，且仍完整評估 Safety Gate 14 項",
          res.authority_result.state == CA.AUTH_IDLE
          and len(res.safety_result.checks) == 14)

    # ---------------- T. Direction-Reversal State Interlock（Phase D.1）----------------
    # 🔴 Layer 1 = 狀態互鎖，與 Layer 2（min_switch_interval 時間互鎖）是**兩個檢查**。
    #    本節不得出現任何時間門檻；min_switch_interval_sec 必須維持 None。
    # 🔴 方向的唯一依據是 fresh PCS flags，不是 LastControl.action。
    print("\nT. Direction-Reversal State Interlock（Phase D.1）")

    charging_ess = ess(charging=True, standby=False, pcs_running_flag=True,
                       actual_active_power_kw=5.3)
    discharging_ess = ess(discharging=True, standby=False, pcs_running_flag=True,
                          actual_active_power_kw=-5.3)
    standby_ess = ess(standby=True)
    stopped_d1 = ess(charging=False, discharging=False, standby=False,
                     pcs_running_flag=False, actual_active_power_kw=-1.3)
    unknown_ess = ess(charging=False, discharging=False, standby=False)          # running=None
    conflict_ess = ess(charging=True, discharging=True, standby=False)

    class _AuthOK:
        """Authority 已放行的替身 —— 用來證明 Authority PASS 不能取代方向互鎖。"""
        state, allowed, reason, detail = CA.AUTH_OWNED, True, CA.CA_PHASE6, "測試注入"

    def bcr_d1(action, e, power=5.0):
        return PCI.build_control_request(Dec(action, power), ess=e, authority=_AuthOK())

    # --- A~D. IDLE 起始（STOPPED / STANDBY）→ 兩個方向都放行 ---
    for tag, e, st_name in (("A/B. STOPPED", stopped_d1, PCI.PCS_STOPPED),
                            ("C/D. STANDBY", standby_ess, PCI.PCS_STANDBY)):
        rc = bcr_d1(DE.ACTION_CHARGE, e)
        rd = bcr_d1(DE.ACTION_DISCHARGE, e)
        check(f"★★ {tag} + CHARGE → ALLOW（沿用既有路徑）",
              rc.action == CHG and rc.reason == PCI.CR_DECISION_CHARGE
              and rc.pcs_actual_state == st_name)
        check(f"★★ {tag} + DISCHARGE → ALLOW（沿用既有路徑）",
              rd.action == DIS and rd.reason == PCI.CR_DECISION_DISCHARGE
              and rd.pcs_actual_state == st_name)

    # --- E/F. 同方向 → 不得因方向互鎖被擋 ---
    re_ = bcr_d1(DE.ACTION_CHARGE, charging_ess)
    rf_ = bcr_d1(DE.ACTION_DISCHARGE, discharging_ess)
    check("★★ E. CHARGING + CHARGE → 不因 direction reversal 被 BLOCK",
          re_.action == CHG and re_.reason == PCI.CR_DECISION_CHARGE)
    check("★★ F. DISCHARGING + DISCHARGE → 不因 direction reversal 被 BLOCK",
          rf_.action == DIS and rf_.reason == PCI.CR_DECISION_DISCHARGE)
    check("  E/F. 同方向的互鎖判定為 DIRECTION_SAME（明示非反向切換）",
          PCI.check_direction_interlock(CHG, PCI.PCS_CHARGING).reason == PCI.DIR_SAME_DIRECTION
          and PCI.check_direction_interlock(DIS, PCI.PCS_DISCHARGING).reason
          == PCI.DIR_SAME_DIRECTION)

    # --- G/H. 反向 → BLOCK ---
    rg_ = bcr_d1(DE.ACTION_DISCHARGE, charging_ess)
    rh_ = bcr_d1(DE.ACTION_CHARGE, discharging_ess)
    check("★★ G. CHARGING + DISCHARGE → BLOCK / DIRECTION_REVERSAL_REQUIRES_STOP",
          rg_.action == NONE
          and rg_.reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
          and rg_.pcs_actual_state == PCI.PCS_CHARGING)
    check("★★ H. DISCHARGING + CHARGE → BLOCK / DIRECTION_REVERSAL_REQUIRES_STOP",
          rh_.action == NONE
          and rh_.reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
          and rh_.pcs_actual_state == PCI.PCS_DISCHARGING)
    check("★★ G/H. BLOCK 時**不得**自行改送 STOP（一個 request 不得變成兩個 command）",
          rg_.action != STOP and rh_.action != STOP)
    check("  G/H. detail 明示上層必須先送 STOP 並確認 idle",
          "STOP" in rg_.detail and "STOPPED" in rg_.detail and "STANDBY" in rg_.detail)

    # --- I/J. STOP 永遠可用 ---
    ri_ = PCI.build_control_request(Dec(DE.ACTION_IDLE), ess=charging_ess, authority=_AuthOK())
    rj_ = PCI.build_control_request(Dec(DE.ACTION_IDLE), ess=discharging_ess,
                                    authority=_AuthOK())
    check("★★ I. CHARGING + STOP → ALLOW（不受方向互鎖影響）",
          ri_.action == STOP and ri_.reason == PCI.CR_IDLE_STOP_REQUIRED)
    check("★★ J. DISCHARGING + STOP → ALLOW（不受方向互鎖影響）",
          rj_.action == STOP and rj_.reason == PCI.CR_IDLE_STOP_REQUIRED)
    check("★★ I/J. 互鎖對 stop / none 一律不適用（任何 PCS 狀態皆放行）",
          all(PCI.check_direction_interlock(a, s).allowed is True
              and PCI.check_direction_interlock(a, s).reason == PCI.DIR_NOT_APPLICABLE
              for a in (STOP, NONE) for s in sorted(PCI.PCS_STATES)))

    # --- K/L. STOPPED replay / offline path 維持 ---
    check("★★ K. STOPPED → DISCHARGE replay 路徑維持 PASS",
          PCI.check_direction_interlock(DIS, PCI.PCS_STOPPED).allowed is True
          and bcr_d1(DE.ACTION_DISCHARGE, stopped_d1).action == DIS)
    check("★★ L. STOPPED → CHARGE offline 路徑維持 PASS",
          PCI.check_direction_interlock(CHG, PCI.PCS_STOPPED).allowed is True
          and bcr_d1(DE.ACTION_CHARGE, stopped_d1).action == CHG)

    # --- M. Authority OWNED 仍不得繞過方向互鎖（pipeline 層）---
    ex.reset()
    res_m = pipe_owned.run(owned(Dec(DE.ACTION_DISCHARGE, 5.0), charging_ess,
                                 action="charge", power=5.0), now=NOW)
    check("★★ M. Authority OWNED + CHARGING + DISCHARGE → 仍 BLOCK",
          res_m.outcome == PCI.OUT_DIRECTION_BLOCKED
          and res_m.reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
          and res_m.authority_result.state == CA.AUTH_OWNED
          and ex.call_count == 0 and res_m.executor_called is False)
    check("  M. DIRECTION_BLOCKED 與 NO_CONTROL 是不同 outcome（不得混用）",
          PCI.OUT_DIRECTION_BLOCKED != PCI.OUT_NO_CONTROL
          and res_m.outcome != PCI.OUT_NO_CONTROL)
    check("  M. 方向互鎖擋下時不進入 Safety Gate（safety_result is None）",
          res_m.safety_result is None)

    # --- N/O. fresh state 優先於 LastControl.action ---
    ex.reset()
    res_n = pipe_owned.run(owned(Dec(DE.ACTION_DISCHARGE, 5.0), stopped_d1,
                                 action="charge", power=5.0), now=NOW)
    check("★★ N. LastControl=charge 但 fresh state=STOPPED + DISCHARGE → 不因舊 action 被擋",
          res_n.outcome == PCI.OUT_WOULD_EXECUTE
          and res_n.control_request.action == DIS and ex.call_count == 1)
    check("  N. 且此時 Authority 為 IDLE —— 舊紀錄不構成占用",
          res_n.authority_result.state == CA.AUTH_IDLE)
    ro_ = bcr_d1(DE.ACTION_DISCHARGE, charging_ess)
    check("★★ O. LastControl=discharge 但 fresh state=CHARGING + DISCHARGE → 依 fresh state BLOCK",
          ro_.reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP)
    # AST 靜態驗證：只看程式碼識別字，天然排除註解與字串，不會被自己的說明文字誤判
    _d1_tree = ast.parse(_io2.open(PCI.__file__, encoding="utf-8").read())
    _d1_fn = [n for n in ast.walk(_d1_tree)
              if isinstance(n, ast.FunctionDef) and n.name == "check_direction_interlock"][0]
    _d1_args = [a.arg for a in _d1_fn.args.args]
    _d1_names = ({n.id for n in ast.walk(_d1_fn) if isinstance(n, ast.Name)}
                 | {n.attr for n in ast.walk(_d1_fn) if isinstance(n, ast.Attribute)})
    check("★★ N/O. 互鎖函式簽章只吃 (control_action, pcs_state) —— 結構上拿不到 LastControl",
          _d1_args == ["control_action", "pcs_state"]
          and not _d1_fn.args.kwonlyargs and _d1_fn.args.vararg is None
          and _d1_fn.args.kwarg is None)
    check("★★ N/O. 互鎖實作未引用任何 LastControl 相關識別字（AST）",
          not any(k in nm.lower() for nm in _d1_names
                  for k in ("last_control", "lastcontrol", "verified_at")))
    check("★★ 互鎖是 State 不是 Time：實作內無 time / now / elapsed / monotonic / interval（AST）",
          not (_d1_names & {"time", "now", "elapsed", "monotonic",
                            "min_switch_interval_sec", "check_interval"}))

    # --- P. UNKNOWN / CONFLICT → Fail Closed ---
    for tag, e, st_name in (("UNKNOWN", unknown_ess, PCI.PCS_UNKNOWN),
                            ("CONFLICT", conflict_ess, PCI.PCS_CONFLICT)):
        rp_c = bcr_d1(DE.ACTION_CHARGE, e)
        rp_d = bcr_d1(DE.ACTION_DISCHARGE, e)
        check(f"★★ P. {tag} + CHARGE → Fail Closed / DIRECTION_STATE_UNUSABLE",
              rp_c.action == NONE and rp_c.reason == PCI.CR_DIRECTION_STATE_UNUSABLE
              and rp_c.pcs_actual_state == st_name)
        check(f"★★ P. {tag} + DISCHARGE → Fail Closed / DIRECTION_STATE_UNUSABLE",
              rp_d.action == NONE and rp_d.reason == PCI.CR_DIRECTION_STATE_UNUSABLE)
    check("  P. Fail Closed 的 reason 與『反向切換』分開（兩者不得混為同一碼）",
          PCI.CR_DIRECTION_STATE_UNUSABLE != PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
          and PCI.DIRECTION_BLOCK_REASONS == {PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP,
                                              PCI.CR_DIRECTION_STATE_UNUSABLE})

    # --- 邊界：idle 集合沿用，未新增「必須 STANDBY」---
    check("★★ 方向隔離狀態沿用 PCS_STATE_IDLE = {STANDBY, STOPPED}（未要求必須 STANDBY）",
          PCI.DIRECTION_ISOLATED_STATES == PCI.PCS_STATE_IDLE
          and PCI.DIRECTION_ISOLATED_STATES == {PCI.PCS_STANDBY, PCI.PCS_STOPPED})
    check("★★ Phase D.1 未設定任何時間門檻：min_switch_interval_sec 仍為 None",
          SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None
          and SG.SafetyConfig().min_switch_interval_sec is None)
    check("  互鎖只認得 charge / discharge 兩個方向（對照表不含 stop）",
          set(PCI.OPPOSITE_RUNNING_STATE) == {CHG, DIS}
          and PCI.OPPOSITE_RUNNING_STATE[CHG] == PCI.PCS_DISCHARGING
          and PCI.OPPOSITE_RUNNING_STATE[DIS] == PCI.PCS_CHARGING)

    # ---------------- T2. 完整 sequence regression（D.1 最重要的驗收條件）------------
    print("\nT2. 完整 sequence：運轉中 → BLOCK → STOP → IDLE → 反向 ALLOW")

    def run_sequence(start_ess, start_state, opposite_decision, opposite_ctrl,
                     own_action, label):
        """一次完整的方向反轉流程；回傳每一步的結果供斷言。"""
        out = {}
        # 步驟 1：運轉中直接要求反向 → 必須 BLOCK，且不得產生任何 command
        ex.reset()
        out["s1"] = pipe_owned.run(owned(Dec(opposite_decision, 5.0), start_ess,
                                         action=own_action, power=5.0), now=NOW)
        out["s1_calls"] = ex.call_count
        # 步驟 2：上層自己產生 STOP leg（一個明確的新 decision）→ 必須 ALLOW
        ex.reset()
        out["s2"] = pipe_owned.run(owned(Dec(DE.ACTION_IDLE), start_ess,
                                         action=own_action, power=5.0), now=NOW)
        out["s2_calls"] = ex.call_count
        # 步驟 3：STOP read-back（用真實 verifier replay 到 STOPPED）
        seq_r = [{"pcs_charging_flag": start_state == PCI.PCS_CHARGING,
                  "pcs_discharging_flag": start_state == PCI.PCS_DISCHARGING,
                  "pcs_standby_flag": False, "pcs_running_flag": True},
                 {"pcs_charging_flag": False, "pcs_discharging_flag": False,
                  "pcs_standby_flag": False, "pcs_running_flag": False}]
        bx = {"i": 0}

        def _rd():
            r = seq_r[min(bx["i"], len(seq_r) - 1)]
            bx["i"] += 1
            return dict(r)

        ck = {"t": 0.0}
        out["rb"] = EX.ReadBackVerifier(
            EX.ReadBackConfig(timeout_sec=75.0, poll_interval_sec=5.0,
                              stability_samples=1),
            reader=_rd, clock=lambda: ck["t"],
            sleeper=lambda d: ck.__setitem__("t", ck["t"] + d)).verify(STOP)
        # 步驟 4：**新的** decision cycle，fresh state 已是 STOPPED → 反向必須 ALLOW
        ex.reset()
        out["s4"] = pipe_owned.run(owned(Dec(opposite_decision, 5.0), stopped_d1,
                                         action=own_action, power=5.0), now=NOW)
        out["s4_calls"] = ex.call_count
        out["label"] = label
        return out

    for start_ess, start_state, dec_op, ctrl_op, own_a, lbl in (
            (charging_ess, PCI.PCS_CHARGING, DE.ACTION_DISCHARGE, DIS, "charge",
             "CHARGING → STOP → STOPPED → DISCHARGE"),
            (discharging_ess, PCI.PCS_DISCHARGING, DE.ACTION_CHARGE, CHG, "discharge",
             "DISCHARGING → STOP → STOPPED → CHARGE")):
        q = run_sequence(start_ess, start_state, dec_op, ctrl_op, own_a, lbl)
        check(f"★★ [{lbl}] 1. 運轉中要求反向 → DIRECTION_BLOCKED，executor 0 次",
              q["s1"].outcome == PCI.OUT_DIRECTION_BLOCKED
              and q["s1"].reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
              and q["s1_calls"] == 0)
        check(f"  [{lbl}] 1. 被擋的那一步**沒有**被偷偷改成 STOP",
              q["s1"].control_request.action == NONE)
        check(f"★★ [{lbl}] 2. 上層明確送出 STOP leg → ALLOW，executor 1 次",
              q["s2"].outcome == PCI.OUT_WOULD_EXECUTE
              and q["s2"].control_request.action == STOP and q["s2_calls"] == 1)
        check(f"  [{lbl}] 2. STOP 仍完整經過 Safety Gate（不得跳過）",
              q["s2"].safety_result is not None)
        check(f"★★ [{lbl}] 3. STOP read-back 成功且落在 idle 集合",
              q["rb"].outcome == EX.VERIFY_SUCCESS
              and q["rb"].observed_state in PCI.DIRECTION_ISOLATED_STATES)
        check(f"★★ [{lbl}] 4. 新 decision cycle：IDLE 起始 → 反向 ALLOW，executor 1 次",
              q["s4"].outcome == PCI.OUT_WOULD_EXECUTE
              and q["s4"].control_request.action == ctrl_op and q["s4_calls"] == 1)
        check(f"  [{lbl}] 4. 反向那一步的 fresh state 確實是 STOPPED",
              q["s4"].control_request.pcs_actual_state == PCI.PCS_STOPPED)
        check(f"★★ [{lbl}] 全程一個 request 只對應一個 command（總計 2 個 leg）",
              q["s1_calls"] + q["s2_calls"] + q["s4_calls"] == 2)

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.5-A/B/F PCS Control Integration 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
