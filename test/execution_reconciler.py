# -*- coding: utf-8 -*-
"""
execution_reconciler.py — Crash / Restart Execution Reconciliation（Phase D.3-F）
======================================================================
只回答一個問題：

    「行程在指令生命週期中途死亡，重新啟動後 ——
      現在這台設備到底是不是 Phase 6 控制的？」

🔴 五條不可違反的原則
    1. UNKNOWN **不得**自動變成 OWNED。
    2. 沒有 durable 的**已驗證**證據，**不得**宣稱 Phase 6 擁有權。
    3. reconciliation **不得**自動重送上一筆指令。
    4. reconciliation **不得**為了恢復擁有權而自行送出停止指令。
    5. LastControl 仍只代表 VERIFIED RESULT，契約不得修改。

🔴 **本模組不做判定，只做分類**
    擁有權的唯一判定者仍是 control_authority。本模組把它的結論翻譯成
    「重啟後應該回到哪個 runtime 狀態」，**不覆寫、不補強、不放寬**任何 Authority 結論。
    刻意不建立第二套 ownership classifier。

🔴 **零 dispatch 能力**
    不 import pcs_control_executor / device_control_operator / api_client /
    production_execution_chain。reconciliation 是「判定擁有權」，
    **不是**「修復設備狀態」。

durable evidence 只有兩樣（重啟後記憶體全部消失）
    ① LastControlStore 的檔案（且必須通過 boot / trust 判定）
    ② 設備自己的 fresh 狀態
    「程序記憶體裡曾經驗證成功過」在重啟後**不存在**，不得作為證據。

用法（完全離線）
    python execution_reconciler.py --demo
"""

import argparse
from dataclasses import dataclass, asdict

import control_authority as CA
import pcs_control_integration as PCI
import last_control_store as LCS

# ======================================================================
# outcome
# ======================================================================
REC_OWNED = "RECOVER_OWNED"              # durable 證據充分 → 可恢復擁有權
REC_IDLE = "RECOVER_IDLE"                # 設備閒置 → 可重新開始（不需要擁有權）
REC_EXTERNAL = "RECOVERY_EXTERNAL"       # 運轉中但無法證明是我們 → 不介入
REC_CONFLICT = "RECOVERY_CONFLICT"       # 紀錄與現況矛盾 → 不介入
REC_UNKNOWN = "RECOVERY_UNKNOWN"         # 資料不足以判斷 → Fail Closed
REC_BLOCKED = "RECOVERY_BLOCKED"         # 輸入不可用
# 🔴 Blocker 13：有 durable 證據指向「這是我們的作業」，但 AC 功率暫存器
#    尚未更新到指令之後的值，因此**還不能**完成佐證。
#    這既不是 REC_OWNED（不得宣稱擁有權），也不是 REC_EXTERNAL
#    （不得把自家作業標成別人的）。
REC_PENDING = "RECOVERY_PENDING_CORROBORATION"

# 只有這一個 outcome 允許宣稱擁有權
OWNED_OUTCOMES = frozenset({REC_OWNED})

# ---- reason ----
R_NO_OBSERVATION = "NO_OBSERVATION"
R_OBSERVATION_INVALID = "OBSERVATION_INVALID"
R_DURABLE_EVIDENCE_OK = "DURABLE_VERIFIED_EVIDENCE"
R_DEVICE_IDLE = "DEVICE_IDLE_NO_OWNERSHIP_NEEDED"
R_STORE_UNREADABLE = "LASTCONTROL_STORE_UNREADABLE"
R_PENDING_CORROBORATION = "OWNERSHIP_PENDING_CORROBORATION"

# 🔴 「找不到紀錄」與「讀不出紀錄」是**不同**的事：
#    前者代表「本來就沒有」，設備閒置時可安全重新開始；
#    後者代表「可能有、但我們讀不到」—— 無法排除曾經擁有，一律 Fail Closed。
#    既有儲存層已提供此區分（load outcome），本模組**不修改**其契約。
STORE_FAILURE_REASONS = frozenset({LCS.LOAD_INVALID_JSON, LCS.LOAD_INVALID_SCHEMA,
                                   LCS.LOAD_UNSUPPORTED_SCHEMA,
                                   LCS.LOAD_INVALID_FIELD, LCS.LOAD_IO_ERROR})

# Authority 結論 → recovery outcome（一對一翻譯，不加工）
# ⚠️ Blocker 13 起有**唯一一項**例外：AUTH_UNKNOWN 需再看 reason ——
#    「尚未能佐證」與「資料不足」兩者的擁有權語意不同，見 _outcome_of()。
_OUTCOME_OF = {CA.AUTH_OWNED: REC_OWNED,
               CA.AUTH_IDLE: REC_IDLE,
               CA.AUTH_EXTERNAL: REC_EXTERNAL,
               CA.AUTH_CONFLICT: REC_CONFLICT,
               CA.AUTH_UNKNOWN: REC_UNKNOWN}


def _outcome_of(auth):
    """
    Authority 結論 → recovery outcome。

    🔴 Blocker 13：`AUTH_UNKNOWN + NOT_YET_CORROBORATED` 必須翻成 REC_PENDING。
       若沿用 REC_UNKNOWN，下游會 fallback 成 EXTERNAL_CONTROL ——
       那等於在服務重啟落在 AC 更新窗內時，把 Phase 6 自己剛送出的作業
       標記成外部控制，從此不再認領。這是**認知錯誤**，不是保守。
    """
    if (auth.state == CA.AUTH_UNKNOWN
            and auth.reason in CA.PENDING_CORROBORATION_REASONS):
        return REC_PENDING
    return _OUTCOME_OF.get(auth.state, REC_UNKNOWN)


# recovery outcome → 建議的 runtime 狀態
_RUNTIME_STATE_OF = {
    (REC_OWNED, PCI.PCS_CHARGING): "OWNED_CHARGE",
    (REC_OWNED, PCI.PCS_DISCHARGING): "OWNED_DISCHARGE",
}
# 🔴 REC_PENDING 不落在 EXTERNAL_CONTROL，也不落在任何 OWNED_* ——
#    它是獨立的「擁有權待佐證」狀態：不宣稱、不放棄、不 dispatch。
_RUNTIME_FALLBACK = {REC_IDLE: "IDLE",
                     REC_EXTERNAL: "EXTERNAL_CONTROL",
                     REC_CONFLICT: "EXTERNAL_CONTROL",
                     REC_UNKNOWN: "EXTERNAL_CONTROL",
                     REC_PENDING: "OWNERSHIP_PENDING",
                     REC_BLOCKED: "EXTERNAL_CONTROL"}


@dataclass(frozen=True)
class ReconciliationResult:
    """
    一次重啟後的擁有權判定。

    🔴 `may_dispatch` 恆為 False —— 本模組不可能送出任何指令。
    🔴 `runtime_state` 是**建議**，套用時仍受 runtime 自己的 guard 限制。
    """
    outcome: str
    reason: str
    authority_state: str = None
    authority_reason: str = None
    pcs_state: str = None
    lastcontrol_present: bool = False
    lastcontrol_action: str = None
    lastcontrol_target_kw: float = None
    lastcontrol_trust: str = None
    store_reason: str = None
    schedule_switch: object = None
    manual_switch: object = None
    runtime_state: str = None
    may_dispatch: bool = False           # 永遠 False
    detail: str = ""

    @property
    def owned(self):
        return self.outcome in OWNED_OUTCOMES

    def as_dict(self):
        return asdict(self)

    def audit_line(self):
        return (f"[RECONCILE] {self.outcome:<20} {self.reason:<34} "
                f"pcs={self.pcs_state or '-':<12} "
                f"authority={self.authority_state or '-':<18} "
                f"lc={self.lastcontrol_action or '-'}/{self.lastcontrol_trust or '-'} "
                f"→ {self.runtime_state}")

    def __str__(self):
        return self.audit_line()


class ExecutionReconciler:
    """
    重啟後的擁有權分類器。純邏輯、零 I/O、零 dispatch。

    authority_policy
        直接沿用 production 的 Control Authority 政策。
        ⚠️ 政策參數未配置時 Authority 會回 UNKNOWN —— 本模組**不得**因此放寬。
    """

    def __init__(self, authority_policy=None):
        self.authority_policy = (authority_policy if authority_policy is not None
                                 else CA.DEFAULT_AUTHORITY_POLICY)

    def reconcile(self, observation=None, last_control=None, trust=None,
                  store_reason=None, now=None):
        """
        以 fresh 觀測 + durable LastControl 判定擁有權。

        observation
            ess_snapshot_adapter.ControlObservation（或等價 duck type）。
            🔴 必須是**重啟後重新讀取**的觀測 —— 記憶體中的舊狀態不算證據。
        last_control / trust
            LastControlStore.current() 的結果。缺任一即無 durable 證據。
        store_reason
            LastControlStore.current().reason —— 用來區分「找不到紀錄」與
            「讀不出紀錄」。後者一律 Fail Closed，即使設備看起來是閒置的。
        """
        if observation is None:
            return ReconciliationResult(REC_BLOCKED, R_NO_OBSERVATION,
                                        runtime_state=_RUNTIME_FALLBACK[REC_BLOCKED],
                                        detail="未提供 fresh 觀測")
        base = dict(
            pcs_state=getattr(observation, "pcs_state", None),
            lastcontrol_present=last_control is not None,
            lastcontrol_action=getattr(last_control, "action", None),
            lastcontrol_target_kw=getattr(last_control, "target_power_kw", None),
            lastcontrol_trust=trust)
        mode = getattr(observation, "pcs_mode_state", None) or {}
        base.update(schedule_switch=mode.get("schedule_switch"),
                    manual_switch=mode.get("manual_switch"))

        base["store_reason"] = store_reason
        # 🔴 儲存層讀取失敗 → 無法排除「其實有一筆有效紀錄」→ Fail Closed。
        #    不得因為「讀不到就當作沒有」而走上 RECOVER_IDLE。
        if store_reason in STORE_FAILURE_REASONS:
            return ReconciliationResult(
                REC_BLOCKED, R_STORE_UNREADABLE, **base,
                runtime_state=_RUNTIME_FALLBACK[REC_BLOCKED],
                detail=f"控制紀錄無法讀取（{store_reason}）→ 不得判定擁有權")

        if getattr(observation, "valid", False) is not True:
            return ReconciliationResult(
                REC_UNKNOWN, R_OBSERVATION_INVALID, **base,
                runtime_state=_RUNTIME_FALLBACK[REC_UNKNOWN],
                detail=f"觀測無效：{getattr(observation, 'reason', None)}")

        ess = getattr(observation, "ess", None)
        req = CA.AuthorityRequest(
            pcs_state=base["pcs_state"],
            actual_active_power_kw=getattr(ess, "actual_active_power_kw", None),
            last_control=last_control, last_control_trust=trust,
            pcs_mode_state=mode,
            ess_valid=bool(ess is not None and getattr(ess, "valid", False)
                           and not getattr(ess, "stale", True)),
            # Blocker 13：重啟後的觀測時戳同樣要帶進去
            observed_at_monotonic=getattr(observation, "read_started_at", None))
        auth = CA.evaluate(req, policy=self.authority_policy, now=now)
        outcome = _outcome_of(auth)
        rstate = _RUNTIME_STATE_OF.get((outcome, base["pcs_state"]))
        if rstate is None:
            rstate = _RUNTIME_FALLBACK.get(outcome, "EXTERNAL_CONTROL")
            if outcome == REC_OWNED:
                # 🔴 OWNED 但狀態無法對應到運轉方向 → 不得宣稱擁有
                outcome, rstate = REC_UNKNOWN, _RUNTIME_FALLBACK[REC_UNKNOWN]

        if outcome == REC_OWNED:
            reason = R_DURABLE_EVIDENCE_OK
            detail = (f"durable LastControl（{base['lastcontrol_action']}"
                      f"@{base['lastcontrol_target_kw']}）與現況一致且信任度足夠")
        elif outcome == REC_IDLE:
            reason = R_DEVICE_IDLE
            detail = f"PCS {base['pcs_state']} → 不需要擁有權即可重新開始"
        elif outcome == REC_PENDING:
            reason = R_PENDING_CORROBORATION
            detail = (f"durable 證據指向本方作業（{base['lastcontrol_action']}"
                      f"@{base['lastcontrol_target_kw']}），但 AC 功率尚未更新到"
                      f"指令之後的值 → 擁有權待佐證，不宣稱也不放棄；"
                      f"在佐證完成前不得送出任何指令。{auth.detail}")
        else:
            reason = auth.reason
            detail = auth.detail
        return ReconciliationResult(outcome, reason,
                                    authority_state=auth.state,
                                    authority_reason=auth.reason, **base,
                                    runtime_state=rstate, detail=detail)


def main(argv=None):
    ap = argparse.ArgumentParser(description="重啟後擁有權判定（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Execution Reconciler（D.3-F，完全離線）==\n")
    r = ExecutionReconciler()
    print(f"  未提供觀測：{r.reconcile()}")
    print(f"\n  durable evidence 只有兩樣：LastControl 檔案（需通過 boot/trust）"
          f"與設備 fresh 狀態。")
    print(f"  記憶體中的『曾經驗證成功』在重啟後不存在，不得作為證據。")
    print(f"  可宣稱擁有的 outcome：{sorted(OWNED_OUTCOMES)}")
    print(f"  LastControl 可記錄的 action：{sorted(LCS.VALID_ACTIONS)}")
    print("\n  ⚠ 本模組不 import executor / operator / API，"
          "不可能送出任何指令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
