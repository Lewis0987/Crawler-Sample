# -*- coding: utf-8 -*-
"""
production_execution_chain.py — Production Execution Chain（Phase D.3-E）
======================================================================
把「已授權」推進到「已驗證」。

    Arbitration → Authorization → COMMAND_PENDING
            ↓ consume（fresh revalidation 之後）
    Executor  ── 重用 pcs_control_executor.execute_and_verify()
            ↓
    ReadBackVerifier
            ↓ 只有 VERIFY_SUCCESS
    LastControl commit ── 重用 last_control_store.update_from_verified_result()
            ↓
    OWNED_CHARGE / OWNED_DISCHARGE（stop → IDLE）

🔴 **零實機能力**
    executor / verifier / store 一律由呼叫端注入。三者任一未注入即不執行。
    本模組不 import device_control_operator / api_client / requests，
    也不自行建立任何連線 —— 離線測試注入 Fake，production 目前**不注入**。

🔴 LastControl 契約（不得放寬）
    只有 read-back 的 `VERIFY_SUCCESS`（`lastcontrol_eligible=True`）才寫入。
    Authorization issued / COMMAND_PENDING / Executor called / POST accepted /
    control_success=True / VERIFYING / ReadBack timeout / failed / exception
    —— **一律不得寫入**。

🔴 「沒有驗證成功」不等於「指令沒有執行」
    POST 已被接受但驗證未成功時，設備**可能其實已經照做了**。
    因此一律回報為 `COMMAND_OUTCOME_UNKNOWN`，Fail Closed：
    不宣稱擁有、不更新紀錄、不自動重送、不自動 STOP。

🔴 Authorization one-shot
    consume 之後即永久失效。POST 失敗 / ReadBack 失敗 / 例外 / 逾時
    都**不得**重用同一張票；重試必須重走 Observation → Decision → Arbitration。

🔴 Duplicate suppression 是安全條件，不是效能最佳化
    既有 ReadBackVerifier 在「設備原本就在目標狀態」時會**第一次輪詢就成功**，
    無法區分「新指令已生效」與「舊狀態仍在」。因此 same-action + same-target
    必須在仲裁層就被抑制，絕不能進到 Executor（D.3-D.1 已證實）。

用法（完全離線）
    python production_execution_chain.py --demo
"""

import time
import argparse
from dataclasses import dataclass, asdict

import pcs_control_executor as EX
import pcs_control_integration as PCI
import last_control_store as LCS
import production_control_authorization as PAZ
import production_arbiter as ARB

# ======================================================================
# outcome
# ======================================================================
EXEC_NOT_ATTEMPTED = "NOT_ATTEMPTED"                 # 仲裁未授權 → 根本沒有要執行的東西
EXEC_NOT_CONFIGURED = "EXECUTION_NOT_CONFIGURED"     # executor / verifier / store 未注入
EXEC_AUTHORIZATION_INVALID = "AUTHORIZATION_INVALID"
EXEC_REVALIDATION_FAILED = "REVALIDATION_FAILED"
EXEC_COMMAND_NOT_SENT = "COMMAND_NOT_SENT"           # **可證明**未送出
EXEC_OUTCOME_UNKNOWN = "COMMAND_OUTCOME_UNKNOWN"     # 已送出但無法證明結果
EXEC_VERIFY_MISMATCH = "VERIFY_STATE_MISMATCH"
EXEC_OWNERSHIP_UNRECORDED = "OWNERSHIP_UNRECORDED"   # 已驗證成功但紀錄寫入失敗
EXEC_VERIFIED = "VERIFIED"

# 只有這個 outcome 代表「我們確實、且能證明地控制了設備」
OWNED_OUTCOMES = frozenset({EXEC_VERIFIED})

# ---- 生命週期階段（供稽核與 runtime 狀態轉移）----
PH_AUTHORIZED = "AUTHORIZED"
PH_REVALIDATED = "REVALIDATED"
PH_CONSUMED = "CONSUMED"
PH_DISPATCHED = "DISPATCHED"
PH_VERIFYING = "VERIFYING"
PH_VERIFIED = "VERIFIED"
PH_COMMITTED = "COMMITTED"

# ---- reason ----
R_NO_AUTHORIZATION = "NO_AUTHORIZATION"
R_EXECUTOR_NOT_INJECTED = "EXECUTOR_NOT_INJECTED"
R_VERIFIER_NOT_INJECTED = "VERIFIER_NOT_INJECTED"
R_STORE_NOT_INJECTED = "LASTCONTROL_STORE_NOT_INJECTED"
R_OBSERVATION_CHANGED = "OBSERVATION_CHANGED_BEFORE_DISPATCH"
R_STALE_BEFORE_DISPATCH = "STALE_BEFORE_DISPATCH"
R_AUTHORITY_CHANGED = "AUTHORITY_CHANGED_BEFORE_DISPATCH"
R_VERIFIED = "CONTROL_VERIFIED"
R_LASTCONTROL_WRITE_FAILED = "LASTCONTROL_WRITE_FAILED"

# 🔴 可**證明**未送出的 operator reason —— 其餘一律視為「結果不明」。
#    例外與格式錯誤都不能證明請求沒有抵達設備。
PROVABLY_NOT_SENT = frozenset({EX.R_OPERATOR_NOT_CONFIGURED,
                               EX.R_OPERATOR_DRY_RUN,
                               EX.R_OPERATOR_PRECHECK_BLOCKED})


@dataclass(frozen=True)
class ExecutionResult:
    """
    一輪執行的完整稽核紀錄。

    duck typing：valid / reason / pcs_state / would_action / stale_blocked /
    authorized / control_action / authorization —— 讓 Runtime 不需認識本型別。
    """
    outcome: str
    reason: str
    arbitration: object = None
    control_action: str = None
    target_power_kw: float = None

    # ---- 執行 ----
    executed: bool = False                 # executor 是否**真的被呼叫**
    dispatch_count_delta: int = 0
    operator_outcome: str = None
    operator_reason: str = None
    readback_outcome: str = None
    readback_reason: str = None
    readback_observed_state: str = None
    readback_elapsed_sec: float = None
    lastcontrol_outcome: str = None
    lastcontrol_reason: str = None
    lastcontrol_written: bool = False

    # ---- 授權 ----
    authorization_id: str = None
    authorization_state: str = None
    consume_reason: str = None

    phases: tuple = ()
    finished_at: float = None
    detail: str = ""

    # ---- duck typing（轉給 Runtime）----
    @property
    def valid(self):
        return getattr(self.arbitration, "valid", False)

    @property
    def pcs_state(self):
        return getattr(self.arbitration, "pcs_state", None)

    @property
    def would_action(self):
        return getattr(self.arbitration, "would_action", None)

    @property
    def stale_blocked(self):
        return bool(getattr(self.arbitration, "stale_blocked", False))

    @property
    def authorized(self):
        return bool(getattr(self.arbitration, "authorized", False))

    @property
    def authorization(self):
        return getattr(self.arbitration, "authorization", None)

    def as_dict(self):
        d = asdict(self)
        d["arbitration"] = (self.arbitration.as_dict()
                            if self.arbitration is not None else None)
        d.update(valid=self.valid, pcs_state=self.pcs_state,
                 would_action=self.would_action, stale_blocked=self.stale_blocked,
                 authorized=self.authorized)
        return d

    def audit_line(self):
        return (f"[EXEC] {self.outcome:<24} {self.reason:<34} "
                f"action={self.control_action or '-':<9} "
                f"executed={self.executed} written={self.lastcontrol_written} "
                f"phases={'>'.join(self.phases) or '-'}")

    def __str__(self):
        return self.audit_line()


class ProductionExecutionChain:
    """
    仲裁 → 授權 → 執行 → 驗證 → 紀錄。

    executor / verifier / store
        全部注入。**任一未注入即不執行**（EXECUTION_NOT_CONFIGURED）——
        production 目前刻意不注入，離線測試注入 Fake。
    revalidate
        可注入的 () -> (ok, reason)；dispatch 前的最後一道 fresh 檢查。
        未注入時使用內建：重新取一次觀測並比對授權票的 binding 指紋。
    phase_hook
        每進入一個生命週期階段時回呼（Runtime 用來讓 VERIFYING 真正可觀測）。
    """

    def __init__(self, arbiter=None, executor=None, verifier=None, store=None,
                 revalidate=None, clock=None, phase_hook=None):
        self.arbiter = arbiter
        self.executor = executor
        self.verifier = verifier
        self.store = store
        self._revalidate = revalidate
        self._clock = clock if clock is not None else time.monotonic
        self._phase_hook = phase_hook
        self.dispatch_count = 0
        self.lastcontrol_write_count = 0

    # ------------------------------------------------------------------
    def _phase(self, phases, name):
        phases.append(name)
        if self._phase_hook is not None:
            self._phase_hook(name)
        return phases

    def _res(self, outcome, reason, arb, phases=(), **kw):
        kw.setdefault("finished_at", self._clock())
        return ExecutionResult(outcome=outcome, reason=reason, arbitration=arb,
                               phases=tuple(phases), **kw)

    def _missing(self):
        for obj, why in ((self.executor, R_EXECUTOR_NOT_INJECTED),
                         (self.verifier, R_VERIFIER_NOT_INJECTED),
                         (self.store, R_STORE_NOT_INJECTED)):
            if obj is None:
                return why
        return None

    # ------------------------------------------------------------------
    def run(self):
        """
        執行一輪。**不吞例外** —— 交給 Runtime 的 Fail Closed 邊界。

        🔴 唯一會呼叫 executor 的地方，且必須先通過：
           授權存在 → 元件齊備 → fresh revalidation → 授權消費成功。
        """
        arb = self.arbiter.evaluate() if self.arbiter is not None else None
        if arb is None:
            return self._res(EXEC_NOT_ATTEMPTED, R_NO_AUTHORIZATION, None,
                             detail="未注入 arbiter")
        if not getattr(arb, "authorized", False):
            # 仲裁未授權 —— 沒有任何東西需要執行（也不可能執行）
            return self._res(EXEC_NOT_ATTEMPTED, arb.reason, arb,
                             control_action=arb.control_action,
                             detail=arb.detail)

        az = arb.authorization
        phases = self._phase([], PH_AUTHORIZED)
        action = az.action
        target = az.target_power_kw

        miss = self._missing()
        if miss is not None:
            az.cancel(miss)                      # 票不得留著等下一輪
            return self._res(EXEC_NOT_CONFIGURED, miss, arb, phases,
                             control_action=action, target_power_kw=target,
                             authorization_id=az.authorization_id,
                             authorization_state=az.state,
                             detail="執行元件未注入 → 不可能送出任何指令")

        # ---- 1. dispatch 前的 fresh revalidation（TTL 不能取代 freshness）----
        ok, why = self._do_revalidate(az)
        if not ok:
            az.cancel(why)
            return self._res(EXEC_REVALIDATION_FAILED, why, arb, phases,
                             control_action=action, target_power_kw=target,
                             authorization_id=az.authorization_id,
                             authorization_state=az.state,
                             detail="授權後、送出前世界已改變 → 作廢，不送出")
        phases = self._phase(phases, PH_REVALIDATED)

        # ---- 2. 消費授權（one-shot；此後無論成敗都不可重用）----
        consumed, creason = az.consume(action, self._clock(),
                                       target_power_kw=target)
        if not consumed:
            return self._res(EXEC_AUTHORIZATION_INVALID, creason, arb, phases,
                             control_action=action, target_power_kw=target,
                             authorization_id=az.authorization_id,
                             authorization_state=az.state, consume_reason=creason,
                             detail="授權票不可用 → 不送出")
        phases = self._phase(phases, PH_CONSUMED)

        # ---- 3. 送出 + 回讀（重用既有 execute_and_verify）----
        ctrl = PCI.ControlRequest(action=action, target_power_kw=target,
                                  reason=arb.reason,
                                  pcs_actual_state=arb.pcs_state,
                                  authority_state=arb.authority_state,
                                  detail=f"authorization={az.authorization_id}")
        phases = self._phase(phases, PH_DISPATCHED)
        self.dispatch_count += 1
        phases = self._phase(phases, PH_VERIFYING)
        vr = EX.execute_and_verify(ctrl, self.executor, self.verifier)

        op = vr.operator_result
        rb = vr.readback_result
        common = dict(control_action=action, target_power_kw=target,
                      executed=True, dispatch_count_delta=1,
                      operator_outcome=getattr(op, "outcome", None),
                      operator_reason=getattr(op, "reason", None),
                      readback_outcome=getattr(rb, "outcome", None),
                      readback_reason=getattr(rb, "reason", None),
                      readback_observed_state=getattr(rb, "observed_state", None),
                      readback_elapsed_sec=getattr(rb, "elapsed_sec", None),
                      authorization_id=az.authorization_id,
                      authorization_state=az.state, consume_reason=creason)

        # ---- 4. 分類結果 ----
        if vr.outcome != EX.VERIFY_SUCCESS:
            op_reason = getattr(op, "reason", None)
            if (getattr(op, "outcome", None) != EX.COMMAND_ACCEPTED
                    and op_reason in PROVABLY_NOT_SENT):
                # 可證明未送出（未注入 operator / dry-run / 前置檢查擋下）
                return self._res(EXEC_COMMAND_NOT_SENT, op_reason, arb, phases,
                                 **common,
                                 detail="可證明指令未送出；設備狀態未被本次動作改變")
            if vr.outcome == EX.VERIFY_FAILED:
                return self._res(EXEC_VERIFY_MISMATCH, vr.reason, arb, phases,
                                 **common,
                                 detail="回讀觀測到互斥/非預期狀態 → 不宣稱擁有、不更新紀錄")
            # 🔴 其餘一律「結果不明」：POST 可能已被設備接受。
            #    不得假設沒執行、不得自動重送、不得自動 STOP。
            return self._res(EXEC_OUTCOME_UNKNOWN, vr.reason, arb, phases,
                             **common,
                             detail="指令可能已被設備接受但無法證明 → Fail Closed，"
                                    "不宣稱擁有、不自動重送、不自動停止")

        phases = self._phase(phases, PH_VERIFIED)

        # ---- 5. 只有 VERIFY_SUCCESS 才寫 LastControl ----
        upd = self.store.update_from_verified_result(vr)
        common.update(lastcontrol_outcome=getattr(upd, "outcome", None),
                      lastcontrol_reason=getattr(upd, "reason", None))
        if getattr(upd, "outcome", None) != LCS.UPD_UPDATED:
            # 🔴 設備已被我們控制，但持久化擁有權紀錄建立失敗。
            #    既不能當成正常擁有（下一輪讀不到紀錄 → 會判成外部控制），
            #    也不能當成指令失敗（設備確實已改變）。
            return self._res(EXEC_OWNERSHIP_UNRECORDED, R_LASTCONTROL_WRITE_FAILED,
                             arb, phases, **common,
                             detail=f"回讀已驗證成功，但紀錄寫入失敗"
                                    f"（{getattr(upd, 'outcome', None)}/"
                                    f"{getattr(upd, 'reason', None)}）→ Fail Closed")
        self.lastcontrol_write_count += 1
        phases = self._phase(phases, PH_COMMITTED)
        return self._res(EXEC_VERIFIED, R_VERIFIED, arb, phases, **common,
                         lastcontrol_written=True,
                         detail=f"{action} 已驗證並記錄")

    # ------------------------------------------------------------------
    def _do_revalidate(self, az):
        """
        dispatch 前的最後一道檢查。

        🔴 TTL 未過期**不等於**資料仍然新鮮 —— 兩者必須分別檢查。
        注入 revalidate 時完全交給呼叫端；未注入時以「重新觀測 + binding 指紋比對」
        作為內建實作（仍然需要 arbiter 的 observer）。
        """
        if self._revalidate is not None:
            return self._revalidate(az)
        obs = getattr(self.arbiter, "observer", None)
        if obs is None:
            return False, R_OBSERVATION_CHANGED
        fresh = obs.observe()
        if getattr(fresh, "stale_blocked", False):
            return False, R_STALE_BEFORE_DISPATCH
        if not getattr(fresh, "valid", False):
            return False, R_OBSERVATION_CHANGED
        b = az.binding
        if (fresh.pcs_state != b.pcs_state
                or fresh.decision_action != b.decision_action):
            return False, R_OBSERVATION_CHANGED
        eo = getattr(fresh, "ess_observation", None)
        mode = getattr(eo, "pcs_mode_state", None) or {}
        if (mode.get("schedule_switch") != b.schedule_switch
                or mode.get("manual_switch") != b.manual_switch):
            return False, R_AUTHORITY_CHANGED
        if getattr(eo, "alarm_source_complete", None) is not True:
            return False, R_OBSERVATION_CHANGED
        return True, "OK"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Production 執行鏈（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Production Execution Chain（D.3-E，完全離線）==\n")
    ch = ProductionExecutionChain(arbiter=ARB.ProductionArbiter())
    print(f"  未注入 arbiter observer：{ch.run()}")
    print(f"\n  dispatch 次數        ：{ch.dispatch_count}")
    print(f"  LastControl 寫入次數 ：{ch.lastcontrol_write_count}")
    print("\n  ⚠ executor / verifier / store 未注入時，結構上不可能送出任何指令。")
    print("  ⚠ 只有 VERIFY_SUCCESS 才寫 LastControl；"
          "『未驗證成功』≠『指令沒有執行』。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
