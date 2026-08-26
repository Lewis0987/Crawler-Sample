# -*- coding: utf-8 -*-
"""
Phase 6.5-C/D PCS Control Executor + Read-back 驗證 —— 完全離線
======================================================================
用途
    驗證 ControlRequest → operator mapping → OperatorResult → read-back → VerifiedControlResult。
    最重要的斷言：**operator/API success 絕不等於 PCS control success**，
    只有 read-back VERIFY_SUCCESS 才具備更新 LastControl 的資格。

是否需要設備
    **不需要**。完全離線：不 import device_control_operator、不登入 HMI、不開 socket、
    不寫檔案、不做真實 sleep。operator / reader / clock / sleeper 全部注入 fake。

涵蓋範圍
    A. Executor mapping     charge/discharge/stop 對應、power 傳遞、no_verify、none 不可執行
    B. Operator result      accepted / blocked / exception / malformed / 不採信 API success
    C. Read-back CHARGE     success / pending / conflict / timeout
    D. Read-back DISCHARGE  同上
    E. Read-back STOP       同上
    F. Power observation    保留 float、不參與判定
    G. Config               timeout/poll/reader 未配置 → CONFIG_NOT_READY，無 fallback
    H. LastControl 資格     只有 VERIFY_SUCCESS 為 True
    I. Isolation            無真實控制路徑、無真實 sleep

用法
    python test_phase6_pcs_control_executor.py          # exit 0 = PASS
"""
import os
import sys
import ast
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pcs_control_executor as EX                        # noqa: E402

_MODULES_AFTER_EX = set(sys.modules)

import pcs_control_integration as PCI                    # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


CHG, DIS, STOP, NONE = PCI.CTRL_CHARGE, PCI.CTRL_DISCHARGE, PCI.CTRL_STOP, PCI.CTRL_NONE


def ctrl(action, power=None):
    return PCI.ControlRequest(action=action, target_power_kw=power, reason="test")


def reading(charging=None, discharging=None, standby=None, power=None, **over):
    r = {"pcs_charging_flag": charging, "pcs_discharging_flag": discharging,
         "pcs_standby_flag": standby, "actual_active_power_kw": power}
    r.update(over)
    return r


class FakeOperator:
    """記錄呼叫參數的假 operator；可設定回傳 record 或拋例外。"""

    def __init__(self, record=None, raise_exc=None):
        self.record = record
        self.raise_exc = raise_exc
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.record


class FakeClock:
    """可控時鐘 + 假 sleeper（只推進虛擬時間，不做真實等待）。"""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def seq_reader(items):
    """依序回傳 reading；用盡後重複最後一筆。Exception 實例會被拋出。"""
    box = list(items)

    def _r():
        v = box.pop(0) if len(box) > 1 else box[0]
        if isinstance(v, Exception):
            raise v
        return v
    return _r


ACCEPTED_REC = {"blocked": False, "control_success": True, "dry_run": False,
                "success": True, "verify_success": "partial"}
CFG = EX.ReadBackConfig(timeout_sec=5.0, poll_interval_sec=1.0)     # 測試 fixture


def verifier(items, cfg=CFG):
    c = FakeClock()
    return EX.ReadBackVerifier(cfg, reader=seq_reader(items),
                               clock=c.now, sleeper=c.sleep), c


def main():
    print("== Phase 6.5-C/D PCS Control Executor + Read-back 驗證（完全離線）==\n")

    # ---------------- A. Executor mapping ----------------
    print("A. Executor mapping（action 名稱取自既有 ACTIONS，非猜測）")
    check("charge → pcs_charge", EX.OPERATOR_ACTION_MAP[CHG] == "pcs_charge")
    check("discharge → pcs_discharge", EX.OPERATOR_ACTION_MAP[DIS] == "pcs_discharge")
    check("stop → pcs_stop_power", EX.OPERATOR_ACTION_MAP[STOP] == "pcs_stop_power")
    check("mapping 恰含三個控制動作", set(EX.OPERATOR_ACTION_MAP) == PCI.CONTROL_ACTIONS)

    op = FakeOperator(ACCEPTED_REC)
    r = EX.PcsControlExecutor(operator_run=op).send(ctrl(CHG, 30.0))
    check("charge 呼叫參數正確（action/power/no_verify/execute）",
          op.calls[-1] == {"action": "pcs_charge", "execute": False,
                           "no_verify": True, "power": 30.0})
    op2 = FakeOperator(ACCEPTED_REC)
    EX.PcsControlExecutor(operator_run=op2).send(ctrl(DIS, 40.0))
    check("discharge 帶 power=40", op2.calls[-1]["power"] == 40.0
          and op2.calls[-1]["action"] == "pcs_discharge")
    op3 = FakeOperator(ACCEPTED_REC)
    EX.PcsControlExecutor(operator_run=op3).send(ctrl(STOP))
    check("★ stop 不帶 power 參數", "power" not in op3.calls[-1]
          and op3.calls[-1]["action"] == "pcs_stop_power")
    check("★ no_verify 恆為 True（不採信既有 partial verify）",
          all(c["no_verify"] is True for c in op.calls + op2.calls + op3.calls))
    check("★ execute 預設 False（不實送）",
          all(c["execute"] is False for c in op.calls + op2.calls + op3.calls))

    op4 = FakeOperator(ACCEPTED_REC)
    rn = EX.PcsControlExecutor(operator_run=op4).send(ctrl(NONE))
    check("★ none 不可執行 → COMMAND_NOT_SENT / NOT_A_CONTROL_REQUEST，operator 0 次呼叫",
          rn.outcome == EX.COMMAND_NOT_SENT and rn.reason == EX.R_NOT_A_CONTROL_REQUEST
          and len(op4.calls) == 0)
    op5 = FakeOperator(ACCEPTED_REC)
    r5 = EX.PcsControlExecutor(operator_run=op5).send(ctrl("teleport"))
    check("未知 action → 不呼叫 operator", len(op5.calls) == 0
          and r5.outcome == EX.COMMAND_NOT_SENT)
    r6 = EX.PcsControlExecutor().send(ctrl(CHG, 30.0))
    check("★ 未注入 operator → COMMAND_NOT_SENT / OPERATOR_NOT_CONFIGURED（不會自行接真機）",
          r6.outcome == EX.COMMAND_NOT_SENT and r6.reason == EX.R_OPERATOR_NOT_CONFIGURED)
    check("  未注入時仍回報將使用的 call_kwargs 供稽核",
          r6.call_kwargs["action"] == "pcs_charge" and r6.call_kwargs["execute"] is False)

    # ---------------- B. Operator result ----------------
    print("\nB. OperatorResult 狀態")
    check("control_success=True → COMMAND_ACCEPTED（僅 API 已接受）",
          r.outcome == EX.COMMAND_ACCEPTED and r.accepted and r.sent is True)
    blocked = {"blocked": True, "precheck": {"reason": "smart_mode_blocked", "allowed": False},
               "control_success": False, "success": False}
    rb = EX.PcsControlExecutor(operator_run=FakeOperator(blocked)).send(ctrl(CHG, 30.0))
    check("★ operator precheck 擋下 → COMMAND_BLOCKED（縱深防禦，不是 bug）",
          rb.outcome == EX.COMMAND_BLOCKED and rb.reason == EX.R_OPERATOR_PRECHECK_BLOCKED)
    check("  保留 operator block reason", rb.blocked_reason == "smart_mode_blocked")
    check("  detail 標示 SAFETY_ALLOW_OPERATOR_BLOCKED",
          rb.detail == EX.SAFETY_ALLOW_OPERATOR_BLOCKED)
    check("  未送出（sent=False）", rb.sent is False)
    re_ = EX.PcsControlExecutor(
        operator_run=FakeOperator(raise_exc=RuntimeError("boom"))).send(ctrl(CHG, 30.0))
    check("operator 拋例外 → COMMAND_SEND_FAILED / OPERATOR_EXCEPTION",
          re_.outcome == EX.COMMAND_SEND_FAILED and re_.reason == EX.R_OPERATOR_EXCEPTION)
    rm = EX.PcsControlExecutor(operator_run=FakeOperator("not a dict")).send(ctrl(CHG, 30.0))
    check("operator 回傳格式異常 → COMMAND_SEND_FAILED / MALFORMED",
          rm.outcome == EX.COMMAND_SEND_FAILED
          and rm.reason == EX.R_OPERATOR_MALFORMED_RESULT)
    rf = EX.PcsControlExecutor(operator_run=FakeOperator(
        {"blocked": False, "control_success": False, "warnings": ["x"]})).send(ctrl(CHG, 30.0))
    check("control_success=False → COMMAND_SEND_FAILED",
          rf.outcome == EX.COMMAND_SEND_FAILED and rf.reason == EX.R_CONTROL_SEND_FAILED)
    rd = EX.PcsControlExecutor(operator_run=FakeOperator(
        {"blocked": False, "control_success": None, "dry_run": True})).send(ctrl(CHG, 30.0))
    check("control_success=None（dry-run）→ COMMAND_NOT_SENT",
          rd.outcome == EX.COMMAND_NOT_SENT and rd.reason == EX.R_OPERATOR_DRY_RUN)

    # 🔴 最重要的不變量
    poisoned = {"blocked": False, "control_success": True, "dry_run": False,
                "success": True, "verify_success": "partial"}
    rp = EX.PcsControlExecutor(operator_run=FakeOperator(poisoned)).send(ctrl(CHG, 30.0))
    check("★ record success=True 且 verify_success='partial' → 仍只到 COMMAND_ACCEPTED",
          rp.outcome == EX.COMMAND_ACCEPTED and rp.outcome != EX.VERIFY_SUCCESS)
    check("★ COMMAND_ACCEPTED 不具 LastControl 資格",
          EX.execute_and_verify(
              ctrl(CHG, 30.0), EX.PcsControlExecutor(operator_run=FakeOperator(poisoned)),
              EX.ReadBackVerifier()).lastcontrol_eligible is False)
    # 精確判定：找出對 "success" 的實際存取（.get("success") 或 ["success"]）。
    # 不能用字串比對 —— 模組 docstring 正是在說明「為何不採信 record["success"]」，會誤判。
    _tree = ast.parse(open(EX.__file__, encoding="utf-8").read())
    _hits = []
    for n in ast.walk(_tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == "success"):
            _hits.append(f'.get("success") @L{n.lineno}')
        if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                and n.slice.value == "success"):
            _hits.append(f'["success"] @L{n.lineno}')
    check(f"★ 程式碼未讀取 record[\"success\"]，只讀 control_success（命中={_hits}）", not _hits)
    _gets = {n.args[0].value for n in ast.walk(_tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "get" and n.args
             and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}
    check(f"  實際讀取的 record 欄位：{sorted(_gets & {'control_success', 'blocked', 'precheck', 'dry_run', 'warnings', 'success', 'verify_success'})}",
          "control_success" in _gets and "success" not in _gets
          and "verify_success" not in _gets)

    # ---------------- C. Read-back CHARGE ----------------
    print("\nC. Read-back CHARGE")
    v, _ = verifier([reading(charging=True)])
    res = v.verify(CHG)
    check("charging only → VERIFY_SUCCESS", res.outcome == EX.VERIFY_SUCCESS
          and res.target_state == PCI.PCS_CHARGING)
    for label, rd in (("standby", reading(standby=True)),
                      ("discharging", reading(discharging=True)),
                      ("all false", reading(False, False, False)),
                      ("missing flags", {}),
                      ("reader 例外", RuntimeError("io")),
                      ("reading 非 dict", None)):
        v, c = verifier([rd] if not isinstance(rd, Exception) else [rd])
        res = v.verify(CHG)
        check(f"{label} → 持續 pending 直到 VERIFY_TIMEOUT",
              res.outcome == EX.VERIFY_TIMEOUT and res.reason == EX.R_TARGET_STATE_NOT_REACHED)
    v, _ = verifier([reading(charging=True, standby=True)])
    res = v.verify(CHG)
    check("★ CONFLICT → 立即 VERIFY_FAILED（不等 timeout，attempts=1）",
          res.outcome == EX.VERIFY_FAILED and res.reason == EX.R_PCS_STATE_CONFLICT
          and len(res.attempts) == 1)
    v, c = verifier([reading(standby=True), reading(False, False, False),
                     reading(charging=True)])
    res = v.verify(CHG)
    check("先 pending 再成功 → VERIFY_SUCCESS，attempts 記錄完整過程",
          res.outcome == EX.VERIFY_SUCCESS and len(res.attempts) == 3)
    check("  attempts 保留每次 state 與 elapsed",
          [a["state"] for a in res.attempts]
          == [PCI.PCS_STANDBY, PCI.PCS_UNKNOWN, PCI.PCS_CHARGING])

    # ---------------- D. Read-back DISCHARGE ----------------
    print("\nD. Read-back DISCHARGE")
    v, _ = verifier([reading(discharging=True)])
    check("discharging only → VERIFY_SUCCESS", v.verify(DIS).outcome == EX.VERIFY_SUCCESS)
    v, _ = verifier([reading(charging=True)])
    check("charging → timeout", v.verify(DIS).outcome == EX.VERIFY_TIMEOUT)
    v, _ = verifier([reading(standby=True)])
    check("standby → timeout", v.verify(DIS).outcome == EX.VERIFY_TIMEOUT)
    v, _ = verifier([reading(discharging=True, standby=True)])
    check("CONFLICT → VERIFY_FAILED", v.verify(DIS).outcome == EX.VERIFY_FAILED)

    # ---------------- E. Read-back STOP ----------------
    print("\nE. Read-back STOP")
    v, _ = verifier([reading(standby=True)])
    check("standby only → VERIFY_SUCCESS", v.verify(STOP).outcome == EX.VERIFY_SUCCESS)
    v, _ = verifier([reading(charging=True)])
    check("charging → timeout", v.verify(STOP).outcome == EX.VERIFY_TIMEOUT)
    v, _ = verifier([reading(discharging=True)])
    check("discharging → timeout", v.verify(STOP).outcome == EX.VERIFY_TIMEOUT)
    v, _ = verifier([reading(charging=True, discharging=True)])
    check("CONFLICT → VERIFY_FAILED", v.verify(STOP).outcome == EX.VERIFY_FAILED)
    check("目標狀態對照完整", EX.TARGET_STATE_MAP
          == {CHG: PCI.PCS_CHARGING, DIS: PCI.PCS_DISCHARGING, STOP: PCI.PCS_STOPPED})
    check("★ 可接受狀態集合完整",
          EX.ACCEPTED_STATES_MAP == {CHG: {PCI.PCS_CHARGING}, DIS: {PCI.PCS_DISCHARGING},
                                     STOP: {PCI.PCS_STOPPED, PCI.PCS_STANDBY}})

    # ---- 6.5-G：STOP read-back 接受 STOPPED / STANDBY ----
    print("\nE2. STOP read-back（Phase 6.5-G 實機狀態模型）")
    _cfg = EX.ReadBackConfig(timeout_sec=5.0, poll_interval_sec=1.0, stability_samples=1)

    def _rb(charging=False, discharging=False, standby=False, running=None, power=None):
        return {"pcs_charging_flag": charging, "pcs_discharging_flag": discharging,
                "pcs_standby_flag": standby, "pcs_running_flag": running,
                "actual_active_power_kw": power}

    def _verify(reading, action=STOP):
        clk = [0.0]

        def _c():
            return clk[0]

        def _s(d):
            clk[0] += d
        return EX.ReadBackVerifier(_cfg, reader=lambda: dict(reading),
                                   clock=_c, sleeper=_s).verify(action)

    r = _verify(_rb(running=False))
    check("★★ 11. STOP + STOPPED(F,F,F,R=False) -> VERIFY_SUCCESS",
          r.outcome == EX.VERIFY_SUCCESS and r.observed_state == PCI.PCS_STOPPED)
    r = _verify(_rb(standby=True, running=True))
    check("★★ 11. STOP + STANDBY(F,F,T,R=True) -> VERIFY_SUCCESS",
          r.outcome == EX.VERIFY_SUCCESS and r.observed_state == PCI.PCS_STANDBY)
    r = _verify(_rb(charging=True, running=True))
    check("★★ 11. STOP + CHARGING -> 不成功",
          r.outcome != EX.VERIFY_SUCCESS)
    r = _verify(_rb(discharging=True, running=True))
    check("★★ 11. STOP + DISCHARGING -> 不成功", r.outcome != EX.VERIFY_SUCCESS)
    r = _verify(_rb(running=True))
    check("★★ 11. STOP + UNKNOWN(F,F,F,R=True) -> 不成功", r.outcome != EX.VERIFY_SUCCESS)
    r = _verify(_rb(charging=True, discharging=True, running=True))
    check("★★ 11. STOP + CONFLICT -> 不成功（且立即失敗，不等 timeout）",
          r.outcome == EX.VERIFY_FAILED and r.reason == EX.R_PCS_STATE_CONFLICT)
    r = _verify(_rb(running=False, power=-5.4))
    check("★★ 14. AC power 不參與判定：state=STOPPED 但 P=-5.4 仍 VERIFY_SUCCESS",
          r.outcome == EX.VERIFY_SUCCESS)
    check("  且該功率有被如實記錄（觀測用）", r.actual_active_power_kw == -5.4)
    r = _verify(_rb(running=False, power=5.4))
    check("  state=STOPPED 但 P=+5.4 同樣 VERIFY_SUCCESS", r.outcome == EX.VERIFY_SUCCESS)
    r = _verify(_rb(charging=True, running=True, power=-1.4), action=CHG)
    check("  CHARGE read-back 仍只接受 CHARGING", r.outcome == EX.VERIFY_SUCCESS)
    r = _verify(_rb(running=False), action=CHG)
    check("★ CHARGE + STOPPED -> 不成功（可接受集合未被放寬）",
          r.outcome != EX.VERIFY_SUCCESS)
    r = _verify(_rb(standby=True, running=True), action=DIS)
    check("★ DISCHARGE + STANDBY -> 不成功", r.outcome != EX.VERIFY_SUCCESS)
    check("★★ reading 缺 pcs_standby_flag 但 running=False -> UNKNOWN -> STOP 不成功",
          _verify({"pcs_charging_flag": False, "pcs_discharging_flag": False,
                   "pcs_running_flag": False}).outcome != EX.VERIFY_SUCCESS)
    check("★★ reading 缺 pcs_charging_flag 但 running=False -> UNKNOWN -> STOP 不成功",
          _verify({"pcs_discharging_flag": False, "pcs_standby_flag": False,
                   "pcs_running_flag": False}).outcome != EX.VERIFY_SUCCESS)
    check("  四個旗標齊備且皆為明確 False -> STOPPED -> STOP 成功",
          _verify(_rb(running=False)).outcome == EX.VERIFY_SUCCESS)
    check("★ reader 未提供 pcs_running_flag -> None -> UNKNOWN -> STOP 不成功",
          _verify({"pcs_charging_flag": False, "pcs_discharging_flag": False,
                   "pcs_standby_flag": False}).outcome != EX.VERIFY_SUCCESS)

    # ---------------- F. Power observation ----------------
    print("\nF. actual_active_power_kw 僅作觀測")
    v, _ = verifier([reading(charging=True, power=-29.8765)])
    res = v.verify(CHG)
    check("成功時保留完整 float 觀測值",
          res.actual_active_power_kw == -29.8765)
    outs = set()
    for pw in (-80.0, 0.0, 80.0, None, float("nan")):
        v, _ = verifier([reading(charging=True, power=pw)])
        outs.add(v.verify(CHG).outcome)
    check(f"★ 功率為 正/負/0/None/NaN 皆不改變 flag-based 判定：{sorted(outs)}",
          outs == {EX.VERIFY_SUCCESS})
    outs = set()
    for pw in (-80.0, 0.0, 80.0):
        v, _ = verifier([reading(standby=True, power=pw)])
        outs.add(v.verify(CHG).outcome)
    check("★ 功率看起來像在充電也不會讓 standby 變成 CHARGE 成功",
          outs == {EX.VERIFY_TIMEOUT})
    v, _ = verifier([reading(charging=True, power=float("inf"))])
    check("非有限功率 → 觀測值記為 None，不影響判定",
          v.verify(CHG).actual_active_power_kw is None)

    # ---------------- G. Config ----------------
    print("\nG. ReadBackConfig（未配置不得 fallback）")
    check("預設 timeout_sec / poll_interval_sec 皆為 None",
          EX.DEFAULT_READBACK_CONFIG.timeout_sec is None
          and EX.DEFAULT_READBACK_CONFIG.poll_interval_sec is None)
    check("預設 ready=False", EX.DEFAULT_READBACK_CONFIG.ready is False)
    res = EX.ReadBackVerifier(EX.DEFAULT_READBACK_CONFIG,
                              reader=lambda: reading(charging=True)).verify(CHG)
    check("★ timeout 未配置 → CONFIG_NOT_READY / TIMEOUT_NOT_CONFIGURED，且未輪詢",
          res.outcome == EX.CONFIG_NOT_READY
          and res.reason == EX.R_TIMEOUT_NOT_CONFIGURED and len(res.attempts) == 0)
    res = EX.ReadBackVerifier(EX.ReadBackConfig(timeout_sec=5.0),
                              reader=lambda: reading(charging=True)).verify(CHG)
    check("poll_interval 未配置 → CONFIG_NOT_READY / POLL_INTERVAL_NOT_CONFIGURED",
          res.reason == EX.R_POLL_INTERVAL_NOT_CONFIGURED)
    res = EX.ReadBackVerifier(CFG).verify(CHG)
    check("★ 未注入 reader → CONFIG_NOT_READY / READER_NOT_CONFIGURED（不自行讀設備）",
          res.reason == EX.R_READER_NOT_CONFIGURED)
    check("ReadBackConfig 拒絕負值",
          _raises(lambda: EX.ReadBackConfig(timeout_sec=-1)))
    check("stability_samples 預設 1（第一版不做 debounce）",
          EX.DEFAULT_READBACK_CONFIG.stability_samples == 1)
    check("stability_samples 拒絕 0 / 非整數",
          _raises(lambda: EX.ReadBackConfig(stability_samples=0))
          and _raises(lambda: EX.ReadBackConfig(stability_samples=1.5)))
    cfg2 = EX.ReadBackConfig(timeout_sec=5.0, poll_interval_sec=1.0, stability_samples=2)
    v, _ = verifier([reading(charging=True), reading(standby=True),
                     reading(charging=True), reading(charging=True)], cfg=cfg2)
    res = v.verify(CHG)
    check("★ 架構已可支援未來的 stability requirement（samples=2 需連續兩次）",
          res.outcome == EX.VERIFY_SUCCESS and len(res.attempts) == 4)
    v, _ = verifier([reading(charging=True)], cfg=cfg2)
    check("  samples=2 且持續成立 → 第 2 次即成功",
          len(v.verify(CHG).attempts) == 2)

    # ---------------- H. LastControl eligibility ----------------
    print("\nH. LastControl 資格（只有 VERIFY_SUCCESS）")
    ex_ok = EX.PcsControlExecutor(operator_run=FakeOperator(ACCEPTED_REC))
    v, _ = verifier([reading(charging=True)])
    res = EX.execute_and_verify(ctrl(CHG, 30.0), ex_ok, v)
    check("★ VERIFY_SUCCESS → lastcontrol_eligible=True",
          res.outcome == EX.VERIFY_SUCCESS and res.lastcontrol_eligible is True)
    cases = []
    v, _ = verifier([reading(standby=True)])
    cases.append(("VERIFY_TIMEOUT", EX.execute_and_verify(ctrl(CHG, 30.0), ex_ok, v)))
    v, _ = verifier([reading(charging=True, standby=True)])
    cases.append(("VERIFY_FAILED", EX.execute_and_verify(ctrl(CHG, 30.0), ex_ok, v)))
    v, _ = verifier([reading(charging=True)])
    cases.append(("COMMAND_BLOCKED", EX.execute_and_verify(
        ctrl(CHG, 30.0), EX.PcsControlExecutor(operator_run=FakeOperator(blocked)), v)))
    cases.append(("COMMAND_SEND_FAILED", EX.execute_and_verify(
        ctrl(CHG, 30.0),
        EX.PcsControlExecutor(operator_run=FakeOperator(raise_exc=RuntimeError("x"))), v)))
    cases.append(("COMMAND_NOT_SENT", EX.execute_and_verify(
        ctrl(CHG, 30.0), EX.PcsControlExecutor(), v)))
    cases.append(("CONFIG_NOT_READY", EX.execute_and_verify(
        ctrl(CHG, 30.0), ex_ok, EX.ReadBackVerifier())))
    for label, c in cases:
        check(f"{label} → lastcontrol_eligible=False", c.lastcontrol_eligible is False)
    check(f"僅 1 種結果具資格（實測 outcomes={sorted({c.outcome for _, c in cases})}）",
          not any(c.lastcontrol_eligible for _, c in cases))
    check("★ 未送出／被擋／送出失敗時不做 read-back",
          all(c.readback_result is None for lbl, c in cases
              if lbl in ("COMMAND_BLOCKED", "COMMAND_SEND_FAILED", "COMMAND_NOT_SENT")))
    check("模組未定義 LastControl（6.5-E 才做）", not hasattr(EX, "LastControl"))

    # ---------------- I. Isolation ----------------
    print("\nI. Isolation")
    for m in ("device_control_operator", "device_control_menu", "api_client",
              "charge_discharge_report", "report_monitor", "requests", "socketio",
              "urllib", "socket"):
        check(f"runtime 未載入 {m}", m not in _MODULES_AFTER_EX)
    src = open(EX.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    check(f"imports 僅標準庫 + 6.5-A/6.4 模組：{sorted(imported)}",
          imported <= {"sys", "math", "time", "argparse", "dataclasses",
                       "pcs_control_integration", "safety_gate"})
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    io_calls = (calls & {"open", "input", "eval", "exec", "compile", "__import__"}) | \
               (attrs & {"dump", "load", "urlopen", "connect", "emit", "login_hmi"})
    check(f"無檔案／網路／登入呼叫（命中={sorted(io_calls)}）", not io_calls)
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    check(f"未出現 15 / 60 / 90 / 150 / 249 / 229（命中={sorted(consts & {15, 60, 90, 150, 249, 229})}）",
          not (consts & {15, 60, 90, 150, 249, 229}))

    t0 = time.monotonic()
    for _ in range(20):
        v, _ = verifier([reading(standby=True)])
        v.verify(CHG)                      # 每次都跑到 timeout
    wall = time.monotonic() - t0
    check(f"★ 20 次 timeout 情境的真實耗時 {wall:.3f}s < 0.5s（未真實 sleep）", wall < 0.5)
    c = FakeClock()
    v = EX.ReadBackVerifier(CFG, reader=seq_reader([reading(standby=True)]),
                            clock=c.now, sleeper=c.sleep)
    v.verify(CHG)
    check(f"  虛擬時鐘確實推進到逾時（t={c.t}s，sleep 呼叫 {len(c.slept)} 次）",
          c.t >= CFG.timeout_sec and len(c.slept) >= 1)

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.5-C/D PCS Control Executor + Read-back 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def _raises(fn):
    try:
        fn()
        return False
    except (ValueError, TypeError):
        return True


if __name__ == "__main__":
    sys.exit(main())
