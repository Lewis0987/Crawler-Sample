# -*- coding: utf-8 -*-
"""
Phase 6.5-C/D PCS Control Executor + Read-back Verification — 離線可測、可注入
======================================================================
6.5-C  把 ControlRequest 映射到既有 device_control_operator 的 action 並送出（薄 adapter）
6.5-D  送出後自行輪詢 read_all() 的 PCS 旗標，確認 PCS **實際**達成目標狀態

⚠️ 本模組**不 import device_control_operator，也不 import charge_discharge_report**。
   operator / reader / clock / sleeper 一律由外部注入 —— 未注入時不會做任何事。
   真實接線留到 Phase 6.5-G。
⚠️ 本輪不做 6.5-E：可產生 VerifiedControlResult，但**不寫任何檔案**、
   不建立 LastControl、不修改 device_control_action_result.json。
⚠️ 不設定 production timeout / poll interval；未設定即 CONFIG_NOT_READY，不 fallback 到 15/60/90。

🔴 為什麼 operator/API success ≠ PCS control success
    既有 build_verify() 的 pcs_power 分支回 final_verify_status="partial"
    （note: "verify source does not expose PCS power status yet"），
    而 _compute_success() 會把 partial / unknown **視為 success**。
    因此 record["success"] 可能在「完全沒確認 PCS 實際狀態」的情況下為 True。
    本模組**只採信 record["control_success"]**（代表 API 送出成功），
    並將其對應為 COMMAND_ACCEPTED —— 距離「控制成功」還差一個 read-back。
    只有 6.5-D 的 VERIFY_SUCCESS 才具備更新 LastControl 的資格。

狀態分層（不得混淆）
    COMMAND_NOT_SENT     未送出（dry-run / operator 未注入 / 非控制請求）
    COMMAND_BLOCKED      operator 自帶的第二層 precheck 擋下 → SAFETY_ALLOW_OPERATOR_BLOCKED
    COMMAND_SEND_FAILED  送出失敗（例外 / control_success=False / 回傳格式異常）
    COMMAND_ACCEPTED     API 已接受（**不代表 PCS 已達成目標狀態**）
    VERIFY_PENDING       read-back 尚未確認（單次觀測用）
    VERIFY_SUCCESS       read-back 確認 PCS 已達目標狀態  ← 唯一可更新 LastControl
    VERIFY_FAILED        read-back 明確失敗（目前僅 PCS 狀態衝突）
    VERIFY_TIMEOUT       逾時仍無法確認
    CONFIG_NOT_READY     timeout / poll interval / reader 未配置

用法（完全離線）
    python pcs_control_executor.py --demo
"""

import sys
import math
import time
import argparse
from dataclasses import dataclass, asdict, field

import pcs_control_integration as PCI
import safety_gate as SG


# ======================================================================
# 設定區
# ======================================================================
# ---- 結果狀態 ----
COMMAND_NOT_SENT = "COMMAND_NOT_SENT"
COMMAND_BLOCKED = "COMMAND_BLOCKED"
COMMAND_SEND_FAILED = "COMMAND_SEND_FAILED"
COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
VERIFY_PENDING = "VERIFY_PENDING"
VERIFY_SUCCESS = "VERIFY_SUCCESS"
VERIFY_FAILED = "VERIFY_FAILED"
VERIFY_TIMEOUT = "VERIFY_TIMEOUT"
CONFIG_NOT_READY = "CONFIG_NOT_READY"

COMMAND_OUTCOMES = frozenset({COMMAND_NOT_SENT, COMMAND_BLOCKED,
                              COMMAND_SEND_FAILED, COMMAND_ACCEPTED})
VERIFY_OUTCOMES = frozenset({VERIFY_PENDING, VERIFY_SUCCESS, VERIFY_FAILED,
                             VERIFY_TIMEOUT, CONFIG_NOT_READY})

# ---- 原因 ----
R_OK = "OK"
R_NOT_A_CONTROL_REQUEST = "NOT_A_CONTROL_REQUEST"     # action=none 不得進 Executor
R_OPERATOR_NOT_CONFIGURED = "OPERATOR_NOT_CONFIGURED"
R_OPERATOR_EXCEPTION = "OPERATOR_EXCEPTION"
R_OPERATOR_MALFORMED_RESULT = "OPERATOR_MALFORMED_RESULT"
R_OPERATOR_PRECHECK_BLOCKED = "OPERATOR_PRECHECK_BLOCKED"
R_OPERATOR_DRY_RUN = "OPERATOR_DRY_RUN"
R_CONTROL_SEND_FAILED = "CONTROL_SEND_FAILED"
R_UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"

R_READER_NOT_CONFIGURED = "READER_NOT_CONFIGURED"
R_TIMEOUT_NOT_CONFIGURED = "TIMEOUT_NOT_CONFIGURED"
R_POLL_INTERVAL_NOT_CONFIGURED = "POLL_INTERVAL_NOT_CONFIGURED"
R_PCS_STATE_CONFLICT = "PCS_STATE_CONFLICT"
R_TARGET_STATE_REACHED = "TARGET_STATE_REACHED"
R_TARGET_STATE_NOT_REACHED = "TARGET_STATE_NOT_REACHED"

# Safety 放行但 operator 自帶 precheck 擋下 —— 縱深防禦的合法結果，不是 bug
SAFETY_ALLOW_OPERATOR_BLOCKED = PCI.OUT_SAFETY_ALLOW_OPERATOR_BLOCKED

# ---- ControlRequest → 既有 operator action（名稱取自 device_control_operator.ACTIONS，非猜測）----
OPERATOR_ACTION_MAP = {
    PCI.CTRL_CHARGE: "pcs_charge",
    PCI.CTRL_DISCHARGE: "pcs_discharge",
    PCI.CTRL_STOP: "pcs_stop_power",
}
# 需要帶 --power 的 operator action（stop 不帶）
OPERATOR_ACTIONS_WITH_POWER = frozenset({"pcs_charge", "pcs_discharge"})

# ---- ControlRequest → read-back 目標 PCS 狀態 ----
TARGET_STATE_MAP = {
    PCI.CTRL_CHARGE: PCI.PCS_CHARGING,
    PCI.CTRL_DISCHARGE: PCI.PCS_DISCHARGING,
    PCI.CTRL_STOP: PCI.PCS_STOPPED,
}

# ---- read-back 可接受的狀態集合 ----
# 🔴 STOP 的語意是「確認已離開 CHARGING / DISCHARGING」，不是「必須進入某一個特定狀態」。
#    Phase 6.5-G 實機：本機 stop 後進入 STOPPED（三旗標全 False + running=False）並持續停留；
#    但 STANDBY（未充放電、仍運轉）同樣代表已離開充放電，一併接受。
#    ⚠️ STOPPED 與 STANDBY 仍是**兩個不同的 state**，此處只是驗證條件放在同一集合，
#       絕不可為了驗證方便把兩者合併成同一個 enum 值。
#    ⚠️ CHARGING / DISCHARGING / UNKNOWN / CONFLICT 一律不算 STOP 成功。
ACCEPTED_STATES_MAP = {
    PCI.CTRL_CHARGE: frozenset({PCI.PCS_CHARGING}),
    PCI.CTRL_DISCHARGE: frozenset({PCI.PCS_DISCHARGING}),
    PCI.CTRL_STOP: PCI.PCS_STATE_IDLE,          # {STANDBY, STOPPED}
}


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ======================================================================
# 6.5-C：Operator Adapter
# ======================================================================
@dataclass(frozen=True)
class OperatorResult:
    """
    送出結果。**outcome 最好也只到 COMMAND_ACCEPTED** —— 它不代表 PCS 已達成目標狀態。
    operator_record 原樣保留供稽核（含既有 partial verify 內容，但本模組不採信其 success）。
    """
    outcome: str
    control_action: str
    operator_action: str = None
    target_power_kw: float = None
    sent: bool = False
    reason: str = ""
    detail: str = ""
    blocked_reason: str = None
    operator_record: dict = None
    call_kwargs: dict = None

    @property
    def accepted(self):
        return self.outcome == COMMAND_ACCEPTED

    def as_dict(self):
        return asdict(self)

    def __str__(self):
        pw = "" if self.target_power_kw is None else f" power={self.target_power_kw:g}kW"
        return (f"[EXEC] {self.outcome:<20} {self.control_action:<9}"
                f"→{self.operator_action or '-':<16}{pw} reason={self.reason}")


class PcsControlExecutor:
    """
    薄 adapter：ControlRequest → 既有 device_control_operator.run()。

    operator_run 必須由外部注入（簽章同 `run(action, execute, power, assume_yes, no_verify)`）。
    **未注入時不做任何事** → COMMAND_NOT_SENT / OPERATOR_NOT_CONFIGURED，
    確保本模組在任何情況下都不可能自己接到真機。

    execute 預設 False（dry-run）。設為 True 只應發生在 Phase 6.5-G 的實機驗證，
    且必須同時注入真實的 device_control_operator.run。

    no_verify 固定 True：Phase 6.5 不採信既有 build_verify() 的 partial 結果，
    read-back 一律交給 6.5-D 的 ReadBackVerifier 自行輪詢。
    """

    def __init__(self, operator_run=None, execute=False):
        self.operator_run = operator_run
        self.execute = bool(execute)

    def send(self, ctrl, safety=None):
        ca = getattr(ctrl, "action", None)

        if ca not in PCI.CONTROL_ACTIONS:
            return OperatorResult(COMMAND_NOT_SENT, ca, reason=R_NOT_A_CONTROL_REQUEST,
                                  detail=f"action={ca!r} 不是控制請求（none 不得進 Executor）")
        op_action = OPERATOR_ACTION_MAP.get(ca)
        if op_action is None:
            return OperatorResult(COMMAND_NOT_SENT, ca, reason=R_UNSUPPORTED_ACTION,
                                  detail=f"無對應的 operator action：{ca!r}")

        power = getattr(ctrl, "target_power_kw", None)
        kwargs = {"action": op_action, "execute": self.execute, "no_verify": True}
        if op_action in OPERATOR_ACTIONS_WITH_POWER:
            kwargs["power"] = power          # stop 不帶 power
        else:
            power = None

        if self.operator_run is None:
            return OperatorResult(COMMAND_NOT_SENT, ca, op_action, power,
                                  reason=R_OPERATOR_NOT_CONFIGURED,
                                  detail="未注入 operator_run（本模組不會自行接上真機）",
                                  call_kwargs=kwargs)

        try:
            record = self.operator_run(**kwargs)
        except Exception as e:
            return OperatorResult(COMMAND_SEND_FAILED, ca, op_action, power,
                                  reason=R_OPERATOR_EXCEPTION,
                                  detail=f"{type(e).__name__}: {e}", call_kwargs=kwargs)

        if not isinstance(record, dict):
            return OperatorResult(COMMAND_SEND_FAILED, ca, op_action, power,
                                  reason=R_OPERATOR_MALFORMED_RESULT,
                                  detail=f"operator 回傳型別 {type(record).__name__}",
                                  call_kwargs=kwargs)

        # operator 自帶的第二層 precheck —— 縱深防禦，不是 bug
        if record.get("blocked") is True:
            pre = record.get("precheck") or {}
            return OperatorResult(COMMAND_BLOCKED, ca, op_action, power,
                                  reason=R_OPERATOR_PRECHECK_BLOCKED,
                                  detail=SAFETY_ALLOW_OPERATOR_BLOCKED,
                                  blocked_reason=pre.get("reason"),
                                  operator_record=record, call_kwargs=kwargs)

        cs = record.get("control_success")
        if cs is True:
            # ⚠️ 只到 ACCEPTED —— **刻意不看 record["success"]**（會被 partial verify 汙染）
            return OperatorResult(COMMAND_ACCEPTED, ca, op_action, power, sent=True,
                                  reason=R_OK,
                                  detail="API 已接受；PCS 是否達成目標狀態需由 read-back 確認",
                                  operator_record=record, call_kwargs=kwargs)
        if cs is False:
            return OperatorResult(COMMAND_SEND_FAILED, ca, op_action, power,
                                  reason=R_CONTROL_SEND_FAILED,
                                  detail=f"control_success=False；warnings={record.get('warnings')}",
                                  operator_record=record, call_kwargs=kwargs)
        # control_success is None → dry-run 或未執行
        return OperatorResult(COMMAND_NOT_SENT, ca, op_action, power,
                              reason=R_OPERATOR_DRY_RUN,
                              detail=f"control_success=None（dry_run={record.get('dry_run')}）",
                              operator_record=record, call_kwargs=kwargs)


# ======================================================================
# 6.5-D：Read-back Verification
# ======================================================================
@dataclass(frozen=True)
class ReadBackConfig:
    """
    ⚠️ timeout_sec / poll_interval_sec **預設 None＝尚未取得正式數值**。
       未配置時一律 CONFIG_NOT_READY，**絕不 fallback 到 15 / 60 / 90 秒或任何既有常數**。

    stability_samples
        需連續幾次觀測到目標狀態才算成功。**第一版固定 1（＝不做 debounce）** ——
        目前沒有任何證據顯示 HMI 的 PCS 旗標會抖動，不得自行加入穩定性要求。
        保留此欄位只是讓架構在未來取得證據後可直接調高，不需重寫。
    """
    timeout_sec: float = None
    poll_interval_sec: float = None
    stability_samples: int = 1

    def __post_init__(self):
        for name, v in (("timeout_sec", self.timeout_sec),
                        ("poll_interval_sec", self.poll_interval_sec)):
            if v is None:
                continue
            if not _is_number(v) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{name} 需為 None 或非負有限數值：{v!r}")
        if not isinstance(self.stability_samples, int) or isinstance(self.stability_samples, bool) \
                or self.stability_samples < 1:
            raise ValueError(f"stability_samples 需為 >=1 的整數：{self.stability_samples!r}")

    @property
    def ready(self):
        return self.timeout_sec is not None and self.poll_interval_sec is not None


DEFAULT_READBACK_CONFIG = ReadBackConfig()


@dataclass(frozen=True)
class ReadBackResult:
    """
    actual_active_power_kw 為**觀測值**，不參與成功判定（見模組 docstring 與 verify()）。
    attempts 保留每次輪詢的觀測，供 Phase 6.5-G 實機比對。
    """
    outcome: str
    reason: str
    control_action: str
    target_state: str = None
    observed_state: str = None
    actual_active_power_kw: float = None
    attempts: tuple = ()
    elapsed_sec: float = None
    detail: str = ""

    @property
    def verified(self):
        return self.outcome == VERIFY_SUCCESS

    def as_dict(self):
        return asdict(self)

    def __str__(self):
        pw = "n/a" if self.actual_active_power_kw is None else f"{self.actual_active_power_kw:g}kW"
        return (f"[VERIFY] {self.outcome:<16} {self.control_action:<9} "
                f"target={self.target_state} observed={self.observed_state} "
                f"power={pw}(obs) attempts={len(self.attempts)} reason={self.reason}")


class ReadBackVerifier:
    """
    送出控制後，自行輪詢 read_all() 的 PCS 旗標，確認 PCS 實際達成目標狀態。

    ⚠️ **不使用** device_control_operator 的 build_verify()（其 pcs_power 分支為 partial）。
    ⚠️ reader / clock / sleeper 一律注入；未注入 reader 即 CONFIG_NOT_READY，不會自行讀設備。

    判定（旗標為 authoritative，狀態互斥一致才算數）
        charge    → PCS 狀態恰為 CHARGING
        discharge → PCS 狀態恰為 DISCHARGING
        stop      → PCS 狀態恰為 STANDBY
        CONFLICT（兩個以上旗標同時 True）→ **立即** VERIFY_FAILED，不等 timeout
        UNKNOWN / 讀取例外 / 資料不全 / 缺旗標 → VERIFY_PENDING，繼續輪詢到 timeout
    """

    def __init__(self, config=DEFAULT_READBACK_CONFIG, reader=None,
                 clock=time.monotonic, sleeper=time.sleep):
        self.cfg = config
        self.reader = reader
        self._clock = clock
        self._sleep = sleeper

    def _observe(self):
        """單次觀測 → (state, power, note)。任何讀取問題一律回 UNKNOWN，不拋例外。"""
        try:
            reading = self.reader()
        except Exception as e:
            return PCI.PCS_UNKNOWN, None, f"reader 例外：{type(e).__name__}"
        if not isinstance(reading, dict):
            return PCI.PCS_UNKNOWN, None, f"reading 型別 {type(reading).__name__}"
        state = PCI.classify_pcs_state(reading.get("pcs_charging_flag"),
                                       reading.get("pcs_discharging_flag"),
                                       reading.get("pcs_standby_flag"),
                                       reading.get("pcs_running_flag"))
        power = reading.get("actual_active_power_kw")
        if not _is_number(power) or not math.isfinite(power):
            power = None                      # 觀測值缺失不影響判定
        return state, power, ""

    def verify(self, control_action):
        """輪詢直到成功／明確失敗／逾時。純由注入的 clock / sleeper 推進，不做真實等待。"""
        target = TARGET_STATE_MAP.get(control_action)
        accepted = ACCEPTED_STATES_MAP.get(control_action, frozenset({target}))
        if target is None:
            return ReadBackResult(CONFIG_NOT_READY, R_UNSUPPORTED_ACTION, control_action,
                                  detail=f"無對應目標狀態：{control_action!r}")
        if self.cfg.timeout_sec is None:
            return ReadBackResult(CONFIG_NOT_READY, R_TIMEOUT_NOT_CONFIGURED, control_action,
                                  target, detail="timeout_sec 未配置（不得 fallback 既有常數）")
        if self.cfg.poll_interval_sec is None:
            return ReadBackResult(CONFIG_NOT_READY, R_POLL_INTERVAL_NOT_CONFIGURED,
                                  control_action, target,
                                  detail="poll_interval_sec 未配置")
        if self.reader is None:
            return ReadBackResult(CONFIG_NOT_READY, R_READER_NOT_CONFIGURED, control_action,
                                  target, detail="未注入 reader（本模組不會自行讀設備）")

        start = self._clock()
        attempts = []
        streak = 0
        while True:
            t = self._clock()
            state, power, note = self._observe()
            attempts.append({"at": t, "elapsed": t - start, "state": state,
                             "actual_active_power_kw": power, "note": note})

            if state == PCI.PCS_CONFLICT:
                return ReadBackResult(VERIFY_FAILED, R_PCS_STATE_CONFLICT, control_action,
                                      target, state, power, tuple(attempts), t - start,
                                      "PCS 旗標互斥衝突 → 立即判失敗，不等 timeout")

            # ⚠️ 只以 PCS 旗標判定；actual_active_power_kw 純觀測，不參與成功條件。
            #    實機已證實 AC 功率會晚約 1 個 refresh cycle，等它回落會造成假 timeout。
            streak = streak + 1 if state in accepted else 0
            if streak >= self.cfg.stability_samples:
                return ReadBackResult(VERIFY_SUCCESS, R_TARGET_STATE_REACHED, control_action,
                                      target, state, power, tuple(attempts), t - start,
                                      f"連續 {streak} 次觀測到 {state}"
                                      f"（可接受狀態 {sorted(accepted)}）")

            if (self._clock() - start) >= self.cfg.timeout_sec:
                return ReadBackResult(VERIFY_TIMEOUT, R_TARGET_STATE_NOT_REACHED,
                                      control_action, target, state, power,
                                      tuple(attempts), self._clock() - start,
                                      f"逾時 {self.cfg.timeout_sec}s 仍未確認 {target}")
            self._sleep(self.cfg.poll_interval_sec)


# ======================================================================
# 6.5-C + 6.5-D 合併結果（供未來 6.5-E 使用；本輪不寫檔）
# ======================================================================
@dataclass(frozen=True)
class VerifiedControlResult:
    """
    ⚠️ lastcontrol_eligible **只有** read-back 為 VERIFY_SUCCESS 時才為 True。
       COMMAND_ACCEPTED / VERIFY_PENDING / VERIFY_FAILED / VERIFY_TIMEOUT /
       COMMAND_BLOCKED 一律 False。
    ⚠️ 本輪不建立、不持久化 LastControl —— 那是 Phase 6.5-E。
    """
    control_action: str
    target_power_kw: float
    outcome: str
    reason: str
    operator_result: object = None
    readback_result: object = None
    lastcontrol_eligible: bool = False
    detail: str = ""

    def as_dict(self):
        d = asdict(self)
        d["operator_result"] = (self.operator_result.as_dict()
                                if self.operator_result else None)
        d["readback_result"] = (self.readback_result.as_dict()
                                if self.readback_result else None)
        return d

    def __str__(self):
        return (f"[RESULT] {self.outcome:<20} {self.control_action:<9} "
                f"reason={self.reason:<28} lastcontrol_eligible={self.lastcontrol_eligible}")


def execute_and_verify(ctrl, executor, verifier, safety=None):
    """
    送出 → 只有 COMMAND_ACCEPTED 才進行 read-back。純協調，無 I/O。

    未送出 / 被擋 / 送出失敗時**不做 read-back** —— 沒送出的指令沒有什麼可驗證的。
    """
    op = executor.send(ctrl, safety)
    power = getattr(ctrl, "target_power_kw", None)
    ca = getattr(ctrl, "action", None)

    if op.outcome != COMMAND_ACCEPTED:
        eligible = False
        detail = (SAFETY_ALLOW_OPERATOR_BLOCKED
                  if op.outcome == COMMAND_BLOCKED else op.detail)
        return VerifiedControlResult(ca, op.target_power_kw, op.outcome, op.reason,
                                     op, None, eligible, detail)

    rb = verifier.verify(ca)
    return VerifiedControlResult(ca, op.target_power_kw, rb.outcome, rb.reason,
                                 op, rb, rb.outcome == VERIFY_SUCCESS, rb.detail)


# ======================================================================
# CLI（完全離線）
# ======================================================================
def _fake_operator(record):
    """回傳固定 record 的假 operator（示範用）。"""
    def _run(**kwargs):
        r = dict(record)
        r["_called_with"] = kwargs
        return r
    return _run


def _fake_reader(states):
    """依序回傳各次觀測的 reading；用盡後重複最後一筆。"""
    seq = list(states)

    def _read():
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(r, Exception):
            raise r
        return r
    return _read


def _reading(charging=False, discharging=False, standby=False, power=None):
    return {"pcs_charging_flag": charging, "pcs_discharging_flag": discharging,
            "pcs_standby_flag": standby, "actual_active_power_kw": power}


def main():
    p = argparse.ArgumentParser(
        description="Phase 6.5-C/D PCS Control Executor + Read-back（離線、可注入）")
    p.add_argument("--demo", action="store_true", help="離線演示")
    args = p.parse_args()

    print("== Phase 6.5-C/D PCS Control Executor + Read-back Verification ==")
    print(f"  operator action mapping : {OPERATOR_ACTION_MAP}")
    print(f"  read-back 目標狀態       : {TARGET_STATE_MAP}")
    print(f"  預設 ReadBackConfig      : timeout={DEFAULT_READBACK_CONFIG.timeout_sec} / "
          f"poll={DEFAULT_READBACK_CONFIG.poll_interval_sec} → CONFIG_NOT_READY")
    print("  ⚠ operator / reader / clock / sleeper 皆需注入；未注入即不動作")
    print("  ⚠ 本輪不實機送控制、不寫任何檔案、不建立 LastControl\n")

    if not args.demo:
        print("  （加上 --demo 展示各路徑）")
        return 0

    clk = {"t": 0.0}
    cfg = ReadBackConfig(timeout_sec=5.0, poll_interval_sec=1.0)   # 測試 fixture，非 production

    def clock():
        return clk["t"]

    def sleeper(s):
        clk["t"] += s

    ctrl_chg = PCI.build_control_request(
        type("D", (), {"action": "charge", "target_power_kw": 30.0, "reason": "OK"})(), None)
    ctrl_stop = PCI.ControlRequest(action=PCI.CTRL_STOP, reason="IDLE_STOP_REQUIRED")

    print("  ── 6.5-C Executor ──")
    print("   ", PcsControlExecutor().send(ctrl_chg))                      # 未注入 operator
    ok_rec = {"blocked": False, "control_success": True, "dry_run": False, "success": True}
    ex = PcsControlExecutor(operator_run=_fake_operator(ok_rec))
    r = ex.send(ctrl_chg)
    print("   ", r)
    print("     call_kwargs =", r.call_kwargs)
    blocked_rec = {"blocked": True, "precheck": {"reason": "smart_mode_blocked"},
                   "control_success": False, "success": False}
    print("   ", PcsControlExecutor(operator_run=_fake_operator(blocked_rec)).send(ctrl_chg))
    print("   ", PcsControlExecutor(operator_run=_fake_operator(ok_rec)).send(ctrl_stop))

    print("\n  ── 6.5-D Read-back ──")
    print("   ", ReadBackVerifier(DEFAULT_READBACK_CONFIG,
                                  reader=lambda: _reading(charging=True)).verify("charge"))
    clk["t"] = 0.0
    v = ReadBackVerifier(cfg, reader=_fake_reader(
        [_reading(standby=True), _reading(), _reading(charging=True, power=-29.8)]),
        clock=clock, sleeper=sleeper)
    print("   ", v.verify("charge"))
    clk["t"] = 0.0
    v = ReadBackVerifier(cfg, reader=_fake_reader([_reading(standby=True)]),
                         clock=clock, sleeper=sleeper)
    print("   ", v.verify("charge"))
    clk["t"] = 0.0
    v = ReadBackVerifier(cfg, reader=_fake_reader([_reading(charging=True, standby=True)]),
                         clock=clock, sleeper=sleeper)
    print("   ", v.verify("charge"))

    print("\n  ── 合併結果 ──")
    clk["t"] = 0.0
    good = ReadBackVerifier(cfg, reader=_fake_reader([_reading(charging=True, power=-30.1)]),
                            clock=clock, sleeper=sleeper)
    print("   ", execute_and_verify(ctrl_chg, ex, good))
    clk["t"] = 0.0
    bad = ReadBackVerifier(cfg, reader=_fake_reader([_reading(standby=True)]),
                           clock=clock, sleeper=sleeper)
    print("   ", execute_and_verify(ctrl_chg, ex, bad))
    print("   ", execute_and_verify(
        ctrl_chg, PcsControlExecutor(operator_run=_fake_operator(blocked_rec)), good))
    return 0


if __name__ == "__main__":
    sys.exit(main())
