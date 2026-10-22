# -*- coding: utf-8 -*-
"""
phase6_first_live_handoff.py — FIRST LIVE ↔ Phase 6.10 Handoff 整合層
======================================================================
使用者 2026-09-18 P3 裁示
    FIRST LIVE 正式路徑改走 **Phase 6.10 HANDOFF PATH**。
    不再以 Phase 6.9「external 當下 idle 就直接執行」作為 field execution path。

    理由（Trial #2 實測）：external 可能 `PCS=STANDBY` / `Authority=IDLE`
    但 normal loop marks 仍持續 advancing，之後自行轉入 `DISCHARGING`。
    因此「external 當下 idle」**不等於**「external 已 relinquish control」——
    6.9 原 Phase A 存在 TOCTOU / ownership race。

正式 lifecycle
    External Controller
      -> Fresh observation / safety preflight
      -> Phase 6.10 pause
      -> NORMAL LOOP HALTED + PauseVerifier VERIFIED
      -> external command silence established
      -> Phase 6 FIRST LIVE dispatcher
      -> 既有 5 kW charge-only live leg
      -> Phase 6 release
      -> Phase 6.10 restore
      -> RestoreVerifier RV_RESUMED
      -> post-check -> COMPLETE

🔴 **不建立第二套 charge execution logic**
    本模組**不組任何 command payload**、不呼叫 operator、不碰 API、
    不定義任何功率數字。真正的送出鏈仍是既有：
        SVC.run_live_leg_cli -> PRD.LiveLegAuthorization -> PRD.build_executor
        -> EXC.PcsControlExecutor -> device_control_operator.run
    本模組只負責「什麼時候可以叫它、叫完怎麼收尾、證據怎麼記」。

🔴 **HandoffRunner 不得知道 CHARGE 的實作細節**
    dispatcher 介面只負責：執行已驗證的 FIRST LIVE live leg ＋ 回傳明確 outcome。

🔴 **兩種 ownership 嚴格分離**
        EXTERNAL CONTROLLER OWNERSHIP —— 由 6.10 pause / restore responsibility 管
        PHASE 6 CONTROL AUTHORITY     —— 由 Phase6AuthorityLease 管
    禁止：external 尚未 verified pause 就 Phase 6 acquire / send
    禁止：Phase 6 authority 尚未 release 就 restore external

🔴 **不修改**：Phase 6 decision policy / TOU / SOC policy / Safety Gate semantics /
    5 kW target / max_power_kw / PauseVerifier criteria / RestoreVerifier criteria /
    deployed remote guard / external controller / Durable Decision Audit 既有 46 欄。

用法
    python phase6_first_live_handoff.py      # 離線自述；不做任何 I/O
"""
import datetime
import json
import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA                  # noqa: E402
import phase6_handoff_orchestrator as HO            # noqa: E402

# 🔴 FIRST LIVE 只允許這一個動作。本模組**不定義**功率，功率永遠來自
#    既有 ProductionConfig（charge_power_kw = 5.0），且不接受任何 override。
FIRST_LIVE_ACTION = "charge"
FIRST_LIVE_ALLOWED_ACTIONS = (FIRST_LIVE_ACTION,)


# ======================================================================
# Dispatcher outcome（P5 契約）
# ======================================================================
DISPATCH_NOT_ATTEMPTED = "DISPATCH_NOT_ATTEMPTED"
DISPATCH_SUCCEEDED = "DISPATCH_SUCCEEDED"
DISPATCH_FAILED = "DISPATCH_FAILED"
DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"
DISPATCH_OUTCOMES = (DISPATCH_NOT_ATTEMPTED, DISPATCH_SUCCEEDED,
                     DISPATCH_FAILED, DISPATCH_UNKNOWN)

# FIRST LIVE 專屬 abort 標籤（與 Trial #2 harness 的 OUT_* 分開，不混用）
OUT_ABORTED_PRE_HANDOFF = "ABORTED_PRE_HANDOFF_POLICY_INELIGIBLE"
OUT_ABORTED_POST_HANDOFF = "ABORTED_POST_HANDOFF_REVALIDATION"

# leg 終態 -> dispatcher outcome。**未列舉者一律 Fail Closed 成 UNKNOWN。**
LEG_COMPLETE = "COMPLETE"
LEG_ABORTED_UNCERTAIN = "ABORTED_UNCERTAIN_OWNERSHIP"


def leg_reached_complete(live_outcome):
    """
    live leg 是否**真的**走到 canonical 成功終點 `LEG_COMPLETE`。

    🔴 既有 `ControlledLiveLeg.run_leg()` 的成功路徑是
           LEG_IDLE_VERIFIED -> LEG_REPORT_FINALIZED -> leg.consume()
           -> LEG_COMPLETE -> _teardown() -> LEG_OBSERVE_ONLY -> return
       —— **回傳值是 `LEG_OBSERVE_ONLY`**，`LEG_COMPLETE` 只出現在
       `runner.history` 中。因此不能只比對 `leg_state`。
    🔴 本函式要求**正面證據**（history 真的出現 LEG_COMPLETE），
       比原本的「leg_state 相等」更嚴格：沒有 runner / 沒有 history /
       history 不含 LEG_COMPLETE 一律 False。
    """
    if live_outcome is None:
        return False
    if getattr(live_outcome, "leg_state", None) == LEG_COMPLETE:
        return True
    runner = getattr(live_outcome, "runner", None)
    hist = getattr(runner, "history", None)
    if not hist:
        return False
    try:
        return LEG_COMPLETE in tuple(hist)
    except TypeError:
        return False


def classify_dispatch_outcome(live_outcome, exception=None):
    """
    把既有 `LiveLegOutcome` 轉成 dispatcher outcome。

    🔴 `None` / 例外 / 未知終態一律 **UNKNOWN**（不得樂觀判成 FAILED）——
       「不知道有沒有送出去」與「確定沒送出去」責任完全不同。

    🔴 OLD / NEW / WHY / EVIDENCE（2026-09-24 readback closure）
      OLD : `if state == LEG_COMPLETE: return DISPATCH_SUCCEEDED`
            —— 但 `run_leg()` 在**完全成功**時回傳的是 `LEG_OBSERVE_ONLY`
            （`_teardown()` 之後刻意回到「結構上送不出指令」的狀態），
            於是**成功的** live leg 會被分類成 `DISPATCH_FAILED`。
      NEW : 改以 `leg_reached_complete()` 判定 —— 要求 `runner.history`
            中**真的出現** `LEG_COMPLETE`（正面證據），
            或 `leg_state` 本身就是 `LEG_COMPLETE`（既有契約保留）。
      WHY : 這是 production contract 缺口，不是測試 harness 問題：
            即使真實 Attempt #5 成功，也會被記成 DISPATCH_FAILED。
      EVIDENCE : Attempt #4 offline reconstruction 走完 charge + cleanup STOP
            後得到 `DISPATCH_FAILED / reason = OBSERVE_ONLY`。
      🔴 這是**收緊**：要求正面完成證據，而不是「不是 abort 就算過」。
    """
    # 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-02 Attempt #8 postmortem，O2）
    #   OLD : 任何例外 + `live_outcome is None` -> `DISPATCH_UNKNOWN`
    #         （= 可能已送出，責任成立）。
    #   NEW : `FirstLivePermitDenied` **專屬**例外 -> `DISPATCH_NOT_ATTEMPTED`。
    #   WHY : 該例外只會在 `first_live_dispatch_scope()` 的 `yield` **之前**
    #         拋出；此時 `guard.take()` 與 `_run_leg()` 都還沒執行，
    #         因此「確定沒送出」，不是「不知道有沒有送出」。
    #   EVIDENCE : scope 的 rest-state 檢查與 `permit.evaluate()` 都在
    #         `permit.state = PERMIT_ACTIVE` 之前；一旦拋出就不會進入 try，
    #         也就不存在 finally 之外的送出路徑。
    #   🔴 僅此一種例外型別可降級；其餘例外一律維持 UNKNOWN（Fail Closed）。
    if live_outcome is None and isinstance(exception, FirstLivePermitDenied):
        return DISPATCH_NOT_ATTEMPTED
    if live_outcome is None:
        # 完全沒拿到結果：只有在確定尚未進入送出流程時才算 NOT_ATTEMPTED
        return DISPATCH_UNKNOWN if exception is not None else DISPATCH_NOT_ATTEMPTED
    n = int(getattr(live_outcome, "dispatch_count", 0) or 0)
    state = getattr(live_outcome, "leg_state", None)
    if n <= 0:
        # 從未送出方向指令 —— 被上游閘門擋下，責任未成立
        return DISPATCH_UNKNOWN if exception is not None else DISPATCH_NOT_ATTEMPTED
    if exception is not None:
        return DISPATCH_UNKNOWN
    if state == LEG_ABORTED_UNCERTAIN:
        return DISPATCH_UNKNOWN
    if leg_reached_complete(live_outcome):
        return DISPATCH_SUCCEEDED
    if state is None:
        return DISPATCH_UNKNOWN
    return DISPATCH_FAILED


# ======================================================================
# Command count guard
# ======================================================================
class CommandLimitExceeded(RuntimeError):
    """Phase 6 / external 指令超出授權上限 —— 結構上禁止。"""


class CommandCountGuard(object):
    """
    FIRST LIVE 的指令上限。**超額即拋例外，不回 False。**

    limits（使用者 2026-09-18 RULING 2）
        phase6_charge    <= 1     ONE CONTROLLED LEG ONLY
        phase6_discharge == 0
        phase6_pcs_stop  <= 1     🔴 **包含** live-leg cleanup STOP
        pause            <= 1
        restore          <= 1

    🔴 RULING 2：既有 live leg 實際送出的 cleanup PCS STOP **必須**計入
       `phase6_pcs_stop` aggregate。**不得**因為它叫 `live_leg_internal_stop`
       就排除於總數之外。
    ⚠️ `live_leg_internal_stop` 仍保留，但改為 **subtype / reason**
       （`pcs_stop_reason = LIVE_LEG_CLEANUP`），不是豁免。
    ⚠️ 允許 `<= 1` **不代表**可以新增任何新的 STOP 路徑 ——
       只認既有 6.9 live-leg lifecycle 本來就存在的 cleanup STOP。
       本次修改只是 COUNTING / EVIDENCE SEMANTICS，未動 STOP policy /
       timing / trigger / PCS control semantics。
    """

    LIMITS = {"phase6_charge": 1, "phase6_discharge": 0, "phase6_pcs_stop": 1,
              "pause": 1, "restore": 1}
    SUBTYPE_COUNTERS = ("live_leg_internal_stop",)
    SEPARATE_COUNTERS = SUBTYPE_COUNTERS          # 向後相容別名
    PCS_STOP_REASON_LIVE_LEG_CLEANUP = "LIVE_LEG_CLEANUP"

    def __init__(self, limits=None):
        self.limits = dict(limits if limits is not None else self.LIMITS)
        self.counts = {}

    def count(self, kind):
        return self.counts.get(kind, 0)

    def may(self, kind):
        if kind not in self.limits:
            return True
        return self.count(kind) < self.limits[kind]

    def take(self, kind):
        if not self.may(kind):
            raise CommandLimitExceeded(
                "%s 已達上限 %d，禁止再送（已 %d 次）"
                % (kind, self.limits.get(kind, 0), self.count(kind)))
        self.counts[kind] = self.count(kind) + 1
        return self.counts[kind]

    def record(self, kind, n):
        """只記錄、不受 limits 管制（供 SEPARATE_COUNTERS 使用）。"""
        self.counts[kind] = int(n or 0)
        return self.counts[kind]

    def violations(self):
        bad = []
        for k, lim in self.limits.items():
            if self.count(k) > lim:
                bad.append("%s=%d > %d" % (k, self.count(k), lim))
        return bad

    @property
    def pcs_stop_reason(self):
        """`phase6_pcs_stop` 的 subtype。沒有 STOP -> None。"""
        if self.count("phase6_pcs_stop") <= 0:
            return None
        if self.count("live_leg_internal_stop") > 0:
            return self.PCS_STOP_REASON_LIVE_LEG_CLEANUP
        return "UNSPECIFIED"

    def as_dict(self):
        d = {"limits": dict(self.limits), "counts": dict(self.counts),
             "violations": self.violations(),
             "subtype_counters": list(self.SUBTYPE_COUNTERS),
             "separate_counters": list(self.SUBTYPE_COUNTERS),
             "pcs_stop_reason": self.pcs_stop_reason}
        return d


# ======================================================================
# Lifecycle recorder —— 用來證明「release 先於 restore」
# ======================================================================
EV_PAUSE_VERIFIED = "PAUSE_VERIFIED"
EV_AUTHORITY_ACQUIRE_ATTEMPTED = "PHASE6_AUTHORITY_ACQUIRE_ATTEMPTED"
EV_AUTHORITY_ACQUIRED = "PHASE6_AUTHORITY_ACQUIRED"
EV_AUTHORITY_ACQUIRE_FAILED = "PHASE6_AUTHORITY_ACQUIRE_FAILED"
EV_DISPATCH_ATTEMPTED = "FIRST_LIVE_DISPATCH_ATTEMPTED"
EV_DISPATCH_OUTCOME = "FIRST_LIVE_DISPATCH_OUTCOME"
EV_AUTHORITY_RELEASED = "PHASE6_AUTHORITY_RELEASED"
EV_RESTORE_ATTEMPTED = "EXTERNAL_RESTORE_ATTEMPTED"
EV_RESTORE_VERIFIED = "EXTERNAL_RESTORE_VERIFIED"


class LifecycleRecorder(object):
    """單調遞增的事件序列。**唯一**用來證明生命週期順序的來源。"""

    def __init__(self, now_fn=None):
        self._now = now_fn or (lambda: datetime.datetime.now())
        self.events = []

    def record(self, event, detail=None, **extra):
        rec = {"seq": len(self.events) + 1, "event": event, "detail": detail,
               "at": self._now().isoformat(timespec="seconds")}
        rec.update(extra)
        self.events.append(rec)
        return rec

    def seq_of(self, event):
        for e in self.events:
            if e["event"] == event:
                return e["seq"]
        return None

    def happened(self, event):
        return self.seq_of(event) is not None

    def ordered(self, first, second):
        """first 必須先於 second。任一未發生 -> None（未知，不得當成通過）。"""
        a, b = self.seq_of(first), self.seq_of(second)
        if a is None or b is None:
            return None
        return a < b

    def as_list(self):
        return list(self.events)


# ======================================================================
# PHASE 6 CONTROL AUTHORITY —— 與 external ownership 嚴格分離
# ======================================================================
# ======================================================================
# FIRST LIVE DISPATCH PERMIT / SCOPE（2026-09-24 裁示 PART A）
# ======================================================================
# 🔴 `DISPATCH_ENABLED` 的正式語意
#      AT REST                     : False（import / validate / 視窗外 / 例外 /
#                                    recovery / attempt 結束後一律 False）
#      AUTHORIZED SCOPED LIVE-LEG  : 僅在本 scope 內 temporarily True
#      FINALLY / POST ATTEMPT      : 無條件回到 False
#    這是 **scoped authorization**，不是 production dispatch enabled。
#    Unattended LIVE 仍為 HOLD，完全不共用本機制。
#
# 🔴 **不提供** force flag / bypass / --force-dispatch / 全域 production switch。
# 🔴 permit 本身**不會**打開 dispatch —— 它只回答「有沒有被授權」。
#    真正的 True 只在 `first_live_dispatch_scope()` 內，且由 finally 關閉。
# 🔴 permit **不持久化**。process 死亡後新 process 重新 import，
#    模組層一律回到 False；ledger 只記 permit entered / exited，
#    **不得**用 ledger recovery 自動重新 enable dispatch。
FIRST_LIVE_PERMIT_PRECONDITIONS = (
    "attempt_field_authorized",       # 1  使用者明確 field authorize
    "attempt_id_matches",             # 2  與正式 config 完全一致
    "one_shot_not_consumed",          # 3
    "inside_authorized_window",       # 4
    "fresh_safety_preflight_ok",      # 5
    "gate_a_ok",                      # 6
    "handoff_readiness_ok",           # 7
    "pre_pause_preflight_ok",         # 8  11/11
    "pause_sent",                     # 9
    "pause_verified",                 # 10 PauseVerifier = VERIFIED
    "ownership_neither",              # 11 external 已 relinquished
    "gate_b_ok",                      # 12 fresh GATE B
    "authority_acquire_attempted",    # 13 acquire() 真正被呼叫
    "authority_acquired",             # 14 acquire = SUCCESS
    "charge_budget_available",        # 15 CommandCountGuard 尚未用掉 CHARGE
    "about_to_run_live_leg",          # 16
)

# ======================================================================
# TWO-STAGE AUTHORIZATION（2026-10-02 Attempt #8 postmortem 正式裁示）
# ======================================================================
# 🔴 OLD / NEW / WHY / EVIDENCE
#   OLD : `HandoffRunner` step 7 在呼叫 dispatcher **之前**要求
#         `_permit_authorizes()` = True，也就是要求**完整 16 項** permit
#         成立 —— 其中含 `authority_acquire_attempted` / `authority_acquired`。
#   NEW : 拆成兩段。
#         STAGE A  DISPATCHER ENTRY GATE  只用「進入 dispatcher 前**確實
#                  已能成立**」的 evidence，**不得**含 authority 兩項。
#         STAGE B  FULL DISPATCH PERMIT   維持原本 16 項（不降低 safety），
#                  在 `lease.acquire()` 之後、進入 scope 之前重新評估。
#   WHY : authority 只能在 `FirstLiveDispatcher.dispatch()` 內的
#         `lease.acquire()` 取得，而 dispatch() 只有通過 step 7 才會被呼叫
#         -> CONTROL-FLOW CYCLE，production path 結構性無法前進。
#   EVIDENCE : Attempt #8 artifact ——
#         dispatch_permit.state = PERMIT_NOT_ENTERED，
#         denied_reason = ["authority_acquire_attempted","authority_acquired"]，
#         其餘 15 項 checks 全 true，handoff_abort_step = 7_FIRST_LIVE，
#         authority_acquire_attempted = false。
#   🔴 這**不是**放寬：完整 permit 的 authority 要求原封不動保留，
#      只是把「進入點判定」與「開啟 dispatch 判定」分離。
FIRST_LIVE_ENTRY_EXCLUDED = ("authority_acquire_attempted",
                             "authority_acquired")

# 🔴 EXECUTION DEADLINE（2026-10-02 裁示）
import phase6_execution_deadline as _DL                        # noqa: E402

OUT_DEADLINE_BLOCKED = "DEADLINE_BLOCKED_NO_NEW_RISK_ACTION"

# ======================================================================
# ATTEMPT RESULT vs LIVE LEG RESULT（2026-10-02 REPORTING CONTRACT 裁示）
# ======================================================================
# 🔴 兩個**不同維度**，不得互相覆蓋：
#      attempt_result   整體排程 / deadline / handoff / safety contract 結果
#      live_leg_result  實際 PCS 5 kW control leg 結果
#
# 🔴 deadline 只能表示「排程時間契約違反」，**不得改寫**：
#      command 是否送出 ／readback 是否成功 ／LEG_COMPLETE 是否達成 ／
#      STOP 是否完成 ／restore 是否完成
#
# 🔴 反之，`attempt_result = DEADLINE_DURING_LIVE_CLEANUP` 也**不得**把
#    真實已成功的 5 kW leg 回頭改判成失敗。
LLR_NOT_ATTEMPTED = "NOT_ATTEMPTED"
LLR_SUCCESS = "SUCCESS"
LLR_FAILED = "FAILED"
LLR_UNKNOWN = "UNKNOWN"

LIVE_LEG_RESULTS = (LLR_NOT_ATTEMPTED, LLR_SUCCESS, LLR_FAILED, LLR_UNKNOWN)

# ======================================================================
# POST-DISPATCH UNCERTAIN OWNERSHIP RECOVERY（2026-10-06 Attempt #9 裁示）
# ======================================================================
# 🔴 契約維持：ownership 不可信時**不得**自動送 PCS STOP ——
#    再送 STOP 可能覆蓋 external controller 或其他合法控制來源。
#    「UNKNOWN ownership」**不得**被推論成「Phase 6 仍持有 PCS」。
# 🔴 但也不得只是「不 STOP -> restore -> 結束」。CHARGE 已送出而
#    authority 轉 UNKNOWN 時，必須走完下列 reconciliation：
#      1 凍結所有新的 Phase 6 PCS 指令
#      2 不送第二次 CHARGE   3 不送 DISCHARGE   4 不送 PCS STOP
#      5 release Phase 6 authority
#      6 restore external controller 恰好一次
#      7 RestoreVerifier
#      8 RV_RESUMED 之後
#      9 執行 **read-only** post-restore reconciliation
EXTERNAL_TAKEOVER_VERIFIED = "EXTERNAL_TAKEOVER_VERIFIED"
EXTERNAL_TAKEOVER_UNCONFIRMED = "EXTERNAL_TAKEOVER_UNCONFIRMED"
EXTERNAL_TAKEOVER_NOT_APPLICABLE = "EXTERNAL_TAKEOVER_NOT_APPLICABLE"

TAKEOVER_RESULTS = (EXTERNAL_TAKEOVER_VERIFIED,
                    EXTERNAL_TAKEOVER_UNCONFIRMED,
                    EXTERNAL_TAKEOVER_NOT_APPLICABLE)

#: reconciliation 必須 fresh 取得的唯讀觀測項（取不到一律 None，不得省略）
RECONCILIATION_ITEMS = (
    "external_liveness", "external_loop_marks", "pcs_state",
    "pcs_charging_flag", "pcs_discharging_flag", "pcs_standby_flag",
    "pcs_fault_flag", "actual_active_power_kw", "pcs_control_mode_code",
    "soc_percent", "communication_ok")

#: 人工注意的正式語意
MA_NOT_REQUIRED = "MANUAL_ATTENTION_NOT_REQUIRED"
MA_REQUIRED = "MANUAL_ATTENTION_REQUIRED"
MA_OPERATOR_REVIEW = "OPERATOR_REVIEW_RECOMMENDED"

PCS_MAY_STILL_BE_UNDER_PHASE6_SETPOINT = (
    "PCS MAY STILL BE UNDER LAST PHASE6 SETPOINT")


def evaluate_external_takeover(restore_verifier_state=None,
                               external_liveness=None, reconciliation=None):
    """
    由**既有證據**判定 external 是否確實接手。回傳 (verdict, reasons)。

    🔴 三項同時成立才算 VERIFIED：
         RestoreVerifier == RV_RESUMED
         canonical external_liveness == EXTERNAL_RUNNING
         post-restore **唯讀** PCS observation 取得成功
    🔴 任一不成立 -> UNCONFIRMED（Fail Closed）。
    🔴 VERIFIED **不得**把 `live_leg_result` 改成 SUCCESS。
    """
    rec = reconciliation or {}
    checks = {
        "restore_resumed": restore_verifier_state == "RESUMED",
        "external_running": external_liveness == "EXTERNAL_RUNNING",
        "pcs_observation_available": rec.get("communication_ok") is True
        and rec.get("pcs_state") is not None,
    }
    bad = [k for k, v in checks.items() if not v]
    if not bad:
        return EXTERNAL_TAKEOVER_VERIFIED, checks
    return EXTERNAL_TAKEOVER_UNCONFIRMED, checks


def evaluate_manual_attention(live_leg_result=None, takeover=None,
                              charge_sent=False, stop_sent=False):
    """
    是否需要人工介入。回傳 (status, reasons)。

    🔴 判定原則：**Phase 6 已真的送出指令、卻沒有自己收尾**時，
       只有在 external 接手被完整驗證之後才可以降級。
    """
    reasons = []
    unresolved = bool(charge_sent) and not bool(stop_sent)
    if unresolved:
        reasons.append("Phase 6 已送出方向指令但未由 Phase 6 執行 cleanup STOP")
    if takeover == EXTERNAL_TAKEOVER_UNCONFIRMED:
        reasons.append("external takeover 未能確認")
    if live_leg_result == LLR_UNKNOWN:
        reasons.append("live_leg_result = UNKNOWN")
    if takeover == EXTERNAL_TAKEOVER_UNCONFIRMED:
        return MA_REQUIRED, reasons
    if unresolved:
        # external 已驗證接手 -> 降級為建議複查，但**不得**說成完全無事
        return MA_OPERATOR_REVIEW, reasons
    if live_leg_result in (LLR_FAILED, LLR_UNKNOWN):
        return MA_OPERATOR_REVIEW, reasons
    return MA_NOT_REQUIRED, reasons


def derive_live_leg_result(art):
    """
    由 artifact 既有證據導出 live leg 結果。**不新增任何判定規則。**

    🔴 SUCCESS 需要**三項正面證據同時成立**：
         dispatcher_outcome   == DISPATCH_SUCCEEDED
         leg_reached_complete is True
         readback_outcome     == VERIFY_SUCCESS
       少任一項都不得稱 SUCCESS。
    🔴 送出過但結果不確定 -> UNKNOWN（Fail Closed，不得樂觀判成 FAILED）。
    🔴 完全沒送出方向指令 -> NOT_ATTEMPTED。
    """
    a = art or {}
    counts = ((a.get("phase6_command_counts") or {}).get("counts") or {})
    n_charge = int(counts.get("phase6_charge", 0) or 0)
    n_discharge = int(counts.get("phase6_discharge", 0) or 0)
    if n_charge <= 0 and n_discharge <= 0:
        return LLR_NOT_ATTEMPTED
    do = a.get("dispatcher_outcome")
    if do == DISPATCH_UNKNOWN:
        return LLR_UNKNOWN
    if (do == DISPATCH_SUCCEEDED
            and a.get("leg_reached_complete") is True
            and a.get("readback_outcome") == "VERIFY_SUCCESS"):
        return LLR_SUCCESS
    return LLR_FAILED


def counts_as_first_live_success(art):
    """
    這次是否該讓「FIRST LIVE successful real 5 kW leg」+1。

    🔴 依**真實 live leg**是否完成計數，**不看** attempt_result ——
       即使 attempt_result = DEADLINE_DURING_LIVE_CLEANUP，
       真實成功的 leg 也不得被改回 0 次。
    """
    return derive_live_leg_result(art) == LLR_SUCCESS

FIRST_LIVE_ENTRY_PRECONDITIONS = tuple(
    k for k in FIRST_LIVE_PERMIT_PRECONDITIONS
    if k not in FIRST_LIVE_ENTRY_EXCLUDED)

# ----------------------------------------------------------------------
# CONTROL-FLOW ORDERING（O8：正常成功順序必須可觀察且不得改序）
# ----------------------------------------------------------------------
EV_ENTRY_GATE_PASS = "ENTRY_GATE_PASS"
EV_AUTHORITY_ACQUIRE_ATTEMPT = "AUTHORITY_ACQUIRE_ATTEMPT"
EV_AUTHORITY_ACQUIRED = "AUTHORITY_ACQUIRED"
EV_FULL_PERMIT_PASS = "FULL_PERMIT_PASS"
EV_DISPATCH_SCOPE_ENTER = "DISPATCH_SCOPE_ENTER"
EV_CHARGE_SEND = "CHARGE_SEND"

CANONICAL_DISPATCH_ORDERING = (
    EV_ENTRY_GATE_PASS,
    EV_AUTHORITY_ACQUIRE_ATTEMPT,
    EV_AUTHORITY_ACQUIRED,
    EV_FULL_PERMIT_PASS,
    EV_DISPATCH_SCOPE_ENTER,
    EV_CHARGE_SEND,
)


def ordering_is_canonical(events):
    """
    只保留 canonical 事件後，序列必須**恰好**等於
    `CANONICAL_DISPATCH_ORDERING`。

    🔴 少一個、多一個、順序不同一律 False（Fail Closed）。
    🔴 這是「成功路徑」的驗收條件；中止路徑本來就不會齊全。
    """
    seq = tuple(e for e in (events or ()) if e in CANONICAL_DISPATCH_ORDERING)
    return seq == CANONICAL_DISPATCH_ORDERING


PERMIT_NOT_ENTERED = "PERMIT_NOT_ENTERED"
PERMIT_ACTIVE = "PERMIT_ACTIVE"
PERMIT_EXITED = "PERMIT_EXITED"
PERMIT_DENIED = "PERMIT_DENIED"


class FirstLivePermitDenied(RuntimeError):
    """permit 前置條件未全數成立 -> 一律拒絕，不得 dispatch。"""


class FirstLiveDispatchPermit(object):
    """
    一次性 FIRST LIVE dispatch 授權憑證。

    🔴 `authorizes()` 只在**全部 16 項**前置條件成立、且尚未被用掉時回 True。
    🔴 同一 permit 只能進入 scope **一次**（第二次一律拒絕）。
    🔴 任何缺漏 / 非 True 的 evidence 一律 Fail Closed。
    """

    def __init__(self, attempt_id, evidence_source):
        if not attempt_id:
            raise ValueError("attempt_id 必填")
        if not callable(evidence_source):
            raise ValueError("evidence_source 必須可呼叫（每次重新取得 evidence）")
        self.attempt_id = attempt_id
        self._evidence = evidence_source
        self.state = PERMIT_NOT_ENTERED
        self.entered_at = None
        self.exited_at = None
        self.last_checks = None
        self.denied_reason = None
        # 🔴 STAGE A 的結果與 STAGE B 分開保存，不得互相覆蓋
        self.last_entry_checks = None
        self.entry_denied_reason = None
        # 🔴 control-flow ordering trace（O8）—— 只追加，不改寫
        self.ordering = []

    # ------------------------------------------------------------------
    def evaluate(self):
        """回傳 (ok, checks)。**純判定**，不改變任何旗標。"""
        try:
            ev = self._evidence() or {}
        except Exception as e:                                 # noqa: BLE001
            self.last_checks = {k: False
                                for k in FIRST_LIVE_PERMIT_PRECONDITIONS}
            self.denied_reason = "evidence_source 例外：%s" % type(e).__name__
            return False, dict(self.last_checks)
        checks = dict((k, ev.get(k) is True)
                      for k in FIRST_LIVE_PERMIT_PRECONDITIONS)
        # 🔴 已進入過 scope 的 permit 不得再次授權
        checks["permit_not_used"] = (self.state == PERMIT_NOT_ENTERED)
        self.last_checks = checks
        ok = all(checks.values())
        if not ok:
            self.denied_reason = [k for k, v in checks.items() if not v]
        return ok, dict(checks)

    def authorizes(self):
        """STAGE B：完整 16 項 permit（含 authority）是否授權。"""
        return self.evaluate()[0]

    # ------------------------------------------------------------------
    def evaluate_entry(self):
        """
        STAGE A：是否允許**進入** `FirstLiveDispatcher`。

        🔴 只使用 `FIRST_LIVE_ENTRY_PRECONDITIONS` —— 結構上**不含**
           `authority_acquire_attempted` / `authority_acquired`，
           因為那兩項只能在 dispatcher 內部才可能成立。
        🔴 其餘一律沿用 STAGE B 的 Fail Closed 規則：
           evidence 缺漏 / 非 True / 取得時例外 -> False。
        🔴 純判定，不改變任何旗標，也不改變 `self.state`。
        """
        try:
            ev = self._evidence() or {}
        except Exception as e:                                 # noqa: BLE001
            self.last_entry_checks = {
                k: False for k in FIRST_LIVE_ENTRY_PRECONDITIONS}
            self.entry_denied_reason = ("evidence_source 例外：%s"
                                        % type(e).__name__)
            return False, dict(self.last_entry_checks)
        checks = dict((k, ev.get(k) is True)
                      for k in FIRST_LIVE_ENTRY_PRECONDITIONS)
        # 🔴 已進入過 scope 的 permit 不得再次進入
        checks["permit_not_used"] = (self.state == PERMIT_NOT_ENTERED)
        self.last_entry_checks = checks
        ok = all(checks.values())
        self.entry_denied_reason = (
            None if ok else [k for k, v in checks.items() if not v])
        return ok, dict(checks)

    def allows_dispatcher_entry(self):
        """STAGE A 的布林入口（供 `HandoffRunner` step 7 以 duck typing 呼叫）。"""
        return self.evaluate_entry()[0]

    # ------------------------------------------------------------------
    def note(self, event, at=None):
        """記一筆 control-flow ordering 事件（只追加）。"""
        self.ordering.append(
            {"event": event, "seq": len(self.ordering) + 1,
             "at": (at or datetime.datetime.now()).isoformat(
                 timespec="seconds")})
        return event

    def ordering_events(self):
        return tuple(r["event"] for r in self.ordering)

    def as_dict(self):
        return {"attempt_id": self.attempt_id, "state": self.state,
                "entered_at": self.entered_at, "exited_at": self.exited_at,
                "checks": self.last_checks,
                "denied_reason": self.denied_reason,
                "preconditions": list(FIRST_LIVE_PERMIT_PRECONDITIONS),
                # --- STAGE A（2026-10-02 Attempt #8 postmortem）---
                "entry_checks": self.last_entry_checks,
                "entry_denied_reason": self.entry_denied_reason,
                "entry_preconditions": list(FIRST_LIVE_ENTRY_PRECONDITIONS),
                # --- control-flow ordering（O8）---
                "ordering": list(self.ordering),
                "ordering_events": list(self.ordering_events()),
                "ordering_canonical": ordering_is_canonical(
                    self.ordering_events())}


def dispatch_flags_at_rest():
    """rest state 事實檢查。回傳 (ok, flags)。"""
    import phase6_handoff_orchestrator as _HO
    import pcs_auto_control_runtime as _RT
    flags = {"ho_dispatch_enabled": _HO.DISPATCH_ENABLED,
             "rt_dispatch_enabled": _RT.DISPATCH_ENABLED}
    return (flags["ho_dispatch_enabled"] is False
            and flags["rt_dispatch_enabled"] is False), flags


@contextlib.contextmanager
def first_live_dispatch_scope(permit, now_fn=None, on_event=None):
    """
    **唯一**允許 `HO.DISPATCH_ENABLED` 為 True 的區間。

    🔴 `__enter__` 前：permit 必須 16 項全數成立，且 rest state 必須是 False。
       任一不成立 -> `FirstLivePermitDenied`，**不會**改動任何旗標。
    🔴 `finally`：**無條件**把 `HO.DISPATCH_ENABLED` 設回 False，
       不論 live leg PASS / FAIL / UNKNOWN / timeout / 例外 / cleanup 失敗。
    🔴 只動 `HO.DISPATCH_ENABLED` —— 既有 6.9 live-leg 呼叫鏈
       （run_live_leg_cli -> live_leg_gates -> LiveLegAuthorization ->
       build_executor -> ControlledLiveLeg）**完全不讀** `RT.DISPATCH_ENABLED`，
       因此不得為了方便而一併打開。
       external 的 pause / restore 走 SSH `armed` transport，與本 scope 無關。
    """
    import phase6_handoff_orchestrator as _HO
    now = now_fn or (lambda: datetime.datetime.now())
    if permit is None:
        raise FirstLivePermitDenied("未提供 permit -> 不得 dispatch")
    rest_ok, flags = dispatch_flags_at_rest()
    if not rest_ok:
        permit.state = PERMIT_DENIED
        raise FirstLivePermitDenied(
            "進入 scope 前 rest state 不是 False：%r" % (flags,))
    ok, checks = permit.evaluate()
    if not ok:
        permit.state = PERMIT_DENIED
        raise FirstLivePermitDenied(
            "permit 前置條件未全數成立：%s"
            % ([k for k, v in checks.items() if not v],))

    # 🔴 STAGE B 通過 —— 此時 authority 必定已取得（否則上面 evaluate 不會過）
    permit.note(EV_FULL_PERMIT_PASS)
    permit.state = PERMIT_ACTIVE
    permit.entered_at = now().isoformat(timespec="seconds")
    if on_event is not None:
        on_event("PERMIT_ENTERED", permit.attempt_id)
    _HO.DISPATCH_ENABLED = True
    permit.note(EV_DISPATCH_SCOPE_ENTER)
    try:
        yield permit
    finally:
        # 🔴 無條件恢復 rest state（不還原「進入前的值」，一律 False）
        _HO.DISPATCH_ENABLED = False
        permit.state = PERMIT_EXITED
        permit.exited_at = now().isoformat(timespec="seconds")
        if on_event is not None:
            on_event("PERMIT_EXITED", permit.attempt_id)


# ======================================================================
# SCHEDULED LIVE-LEG AUTHORIZATION ADAPTER（2026-09-24 裁示 PART E / G）
# ======================================================================
# 🔴 既有 `SVC.confirm_live_leg(leg, prompt=None)` 的語意
#      `got = (prompt or input)("  > ")`
#      未注入 prompt -> 真人以 stdin 輸入完整確認字串（互動 CLI safeguard）。
#      注入 prompt   -> 由呼叫端提供**確認證據**，同一道閘門照常比對。
#
# 🔴 Scheduled FIRST LIVE 由 Task Scheduler 無人值守執行，沒有真人可輸入。
#    但**不得**因此跳過這一層，也不得使用：
#      --force / --yes / --skip-confirm / AUTO_CONFIRM / 硬編確認字串 /
#      stdin echo pipe / subprocess input="YES"。
#    正式作法：以 **typed / structured authorization object** 取代鍵盤輸入，
#    且該物件**只能**由已驗證的 `FirstLiveDispatchPermit` 產生。
#
# 🔴 兩條路徑進入**同一個** live-leg authorization gate：
#      INTERACTIVE  : human confirmation      -> confirm_live_leg()
#      SCHEDULED    : validated permit        -> confirm_live_leg()
#    scheduled path **沒有**繞過整層的捷徑。
SCHEDULED_CONFIRMATION_DENIED = "SCHEDULED-AUTHORIZATION-DENIED"

SCHEDULED_AUTH_BINDING = ("attempt_id", "action", "power_kw",
                          "target_date", "window_start", "window_end")


class ScheduledLiveLegAuthorization(object):
    """
    Scheduled FIRST LIVE 的**非互動**確認證據。

    🔴 它不是「自動按 YES」—— 它是一個綁定到單一 attempt 的授權物件：
       只有在 permit **正處於已驗證的 dispatch scope 內**（`PERMIT_ACTIVE`）、
       動作為 canonical FIRST LIVE action、功率取自 canonical ProductionConfig、
       且本授權尚未使用過時，才會產生正確的確認字串。
       其餘一律回 `SCHEDULED_CONFIRMATION_DENIED` —— 與 `want` 不符，
       `confirm_live_leg()` 照既有規則回 False，不建立任何控制出口。

    🔴 確認字串一律由既有 `PRD.confirmation_phrase(PRD.LiveLegIntent(...))`
       計算，**不硬編**。功率只來自 `ProductionConfig`，無參數可覆寫；
       若實際 leg 的功率與 canonical 不符，`want` 自然不同 -> 結構性拒絕。

    🔴 **one-shot**：同一物件只能確認一次；第二次一律 DENIED。
    """

    def __init__(self, permit, action=None, config=None, binding=None):
        import pcs_auto_control_production as _PRD
        self._PRD = _PRD
        self.permit = permit
        self.action = action if action is not None else FIRST_LIVE_ACTION
        self._config = config
        self.binding = dict(binding or {})
        self.used = False
        self.last_decision = None
        self.denied_reason = None

    # ------------------------------------------------------------------
    def expected_phrase(self):
        """canonical 確認字串（來自既有 production 函式，不硬編）。"""
        intent = self._PRD.LiveLegIntent(self.action, self._config)
        return self._PRD.confirmation_phrase(intent)

    def evaluate(self):
        """回傳 (ok, checks)。**純判定**，不改變 used 狀態。"""
        p = self.permit
        checks = {
            "permit_present": p is not None,
            # 🔴 只有真的進入 `first_live_dispatch_scope()` 之後才是 ACTIVE；
            #    scope 進入前已把 16 項前置條件全數驗過。
            "permit_active_scope": getattr(p, "state", None) == PERMIT_ACTIVE,
            "action_is_first_live": self.action in FIRST_LIVE_ALLOWED_ACTIONS,
            "action_is_canonical": self.action == FIRST_LIVE_ACTION,
            "authorization_not_used": self.used is False,
            "binding_complete": all(
                self.binding.get(k) is not None
                for k in SCHEDULED_AUTH_BINDING),
            "binding_attempt_id_matches": (
                bool(self.binding.get("attempt_id"))
                and self.binding.get("attempt_id")
                == getattr(p, "attempt_id", None)),
            "binding_action_matches": self.binding.get("action") == self.action,
        }
        # 功率一律以 canonical 解析結果比對，binding 只是宣告
        try:
            canonical_kw = self._PRD.leg_power_kw(self.action, self._config)
            checks["power_from_production_config"] = isinstance(
                canonical_kw, float)
            checks["binding_power_matches"] = (
                self.binding.get("power_kw") == canonical_kw)
        except Exception:                                      # noqa: BLE001
            checks["power_from_production_config"] = False
            checks["binding_power_matches"] = False
        ok = all(checks.values())
        if not ok:
            self.denied_reason = [k for k, v in checks.items() if not v]
        return ok, checks

    # ------------------------------------------------------------------
    def __call__(self, _prompt=None):
        """注入 `confirm_live_leg(prompt=...)` 的 callable。"""
        ok, checks = self.evaluate()
        self.last_decision = {"ok": ok, "checks": checks}
        if not ok:
            return SCHEDULED_CONFIRMATION_DENIED
        # 🔴 one-shot：取用即消耗，之後一律 DENIED
        self.used = True
        return self.expected_phrase()

    def as_dict(self):
        return {"action": self.action, "used": self.used,
                "binding": dict(self.binding),
                "denied_reason": self.denied_reason,
                "last_decision": self.last_decision,
                "permit_state": getattr(self.permit, "state", None)}


def build_scheduled_live_leg_authorization(permit, attempt_id,
                                           target_date=None,
                                           window_start=None,
                                           window_end=None,
                                           action=None, config=None):
    """
    由**已建立的 permit** 產生 scheduled 授權物件。

    🔴 功率不接受呼叫端傳入 —— 一律由 `PRD.leg_power_kw()` 從
       ProductionConfig 解析後寫入 binding。
    """
    import pcs_auto_control_production as _PRD
    act = action if action is not None else FIRST_LIVE_ACTION
    binding = {"attempt_id": attempt_id, "action": act,
               "power_kw": _PRD.leg_power_kw(act, config),
               "target_date": target_date, "window_start": window_start,
               "window_end": window_end}
    return ScheduledLiveLegAuthorization(permit, action=act, config=config,
                                         binding=binding)


class Phase6AuthorityLease(object):
    """
    Phase 6 控制權租約。**只管 Phase 6 自己**，完全不碰 external 責任。

    🔴 `release()` 一律在 `finally` 呼叫 —— 不論 leg 成功、失敗或例外，
       Phase 6 都必須先放掉控制權，之後才輪到 6.10 restore external。
    """

    def __init__(self, acquire, release, recorder=None):
        if acquire is None or release is None:
            raise ValueError("acquire / release 必填（不得預設）")
        self._acquire = acquire
        self._release = release
        self.rec = recorder
        self.held = False
        self.acquire_reason = None
        self.release_ok = None
        self.acquired_once = False
        # 🔴 Attempt #3 postmortem：只有 acquire() **真的被呼叫過**，
        #    才允許把終態分類成 AUTHORITY_ACQUIRE_FAILED。
        self.acquire_attempted = False

    def acquire(self):
        self.acquire_attempted = True
        if self.rec is not None:
            self.rec.record(EV_AUTHORITY_ACQUIRE_ATTEMPTED)
        ok, why = self._acquire()
        self.acquire_reason = why
        self.held = bool(ok)
        if ok:
            self.acquired_once = True
            if self.rec is not None:
                self.rec.record(EV_AUTHORITY_ACQUIRED, why)
        elif self.rec is not None:
            self.rec.record(EV_AUTHORITY_ACQUIRE_FAILED, why)
        return bool(ok), why

    def release(self):
        """idempotent。未曾取得 -> 視為已釋放（no-op），但仍留下事件。"""
        if not self.held:
            self.release_ok = True if not self.acquired_once else self.release_ok
            if self.rec is not None and not self.rec.happened(EV_AUTHORITY_RELEASED):
                self.rec.record(EV_AUTHORITY_RELEASED, "not_held_noop")
            return True
        ok = bool(self._release())
        self.held = False
        self.release_ok = ok
        if self.rec is not None:
            self.rec.record(EV_AUTHORITY_RELEASED, "released=%r" % ok)
        return ok

    def as_dict(self):
        return {"acquired_once": self.acquired_once, "held": self.held,
                "acquire_reason": self.acquire_reason,
                "release_ok": self.release_ok}


# ======================================================================
# FIRST LIVE dispatcher（P5）
# ======================================================================
class FirstLiveDispatcher(object):
    """
    `HandoffRunner` 的 dispatcher。介面：`(observation) -> dict`。

    🔴 本類別**不知道** CHARGE 怎麼送 —— `live_leg_runner` 由呼叫端注入，
       production 即既有 `SVC.run_live_leg_cli`。
    🔴 只有在 `HandoffRunner` 走到 step 7 才會被呼叫，而 step 7 之前
       已經通過：PauseVerifier VERIFIED、ownership NEITHER、post-pause precheck。
    🔴 生命週期：acquire -> live leg -> release（release 在 finally）。
       因此「Phase 6 release 先於 external restore」是結構保證。
    """

    def __init__(self, live_leg_runner, authority_lease, command_guard,
                 recorder=None, audit_writer=None, action=FIRST_LIVE_ACTION,
                 revalidator=None, dispatch_permit=None, permit_event=None,
                 deadline=None):
        if action not in FIRST_LIVE_ALLOWED_ACTIONS:
            raise ValueError("FIRST LIVE 只允許 %s，收到 %r"
                             % (list(FIRST_LIVE_ALLOWED_ACTIONS), action))
        if live_leg_runner is None:
            raise ValueError("live_leg_runner 必填（未注入即不可能送出）")
        self._run_leg = live_leg_runner
        self.lease = authority_lease
        self.guard = command_guard
        self.rec = recorder
        self._audit = audit_writer
        self._revalidate = revalidator          # GATE B；None = 不做（離線測試）
        # 🔴 permit = 已授權的 one-shot；None -> 不建立 scope（沿用舊行為）
        self.permit = dispatch_permit
        self._permit_event = permit_event
        # 🔴 EXECUTION DEADLINE：None = 未注入 -> 行為與導入前完全相同。
        #    已到期時**不得開始**新風險動作，但 cleanup / release / restore
        #    一律放行（由 `_DL` 保證，本檔不另造規則）。
        self.deadline = deadline
        self.deadline_blocked_at = None
        self.action = action
        self.calls = 0
        self.last = None
        self.revalidation = None
        self.last_live_outcome = None

    def _note(self, event):
        """把 ordering 事件記在 permit 上（permit = None 時為 no-op）。"""
        if self.permit is not None:
            self.permit.note(event)
        return event

    def __call__(self, observation=None):
        self.calls += 1
        out = {"dispatched": False, "verified": False, "released": False,
               "outcome": DISPATCH_NOT_ATTEMPTED, "reason": None,
               "live_leg": None, "audit": None, "action": self.action,
               "revalidation": None}

        # ---- 0. GATE B：POST-HANDOFF REVALIDATION（在 acquire 之前）----
        # 🔴 pause 已 VERIFIED，但策略可能已變 —— 必須用 pause 之後的
        #    fresh 觀測重新確認，**不得**沿用 pause 前的 decision / SOC / TOU。
        #    未通過 -> 不 acquire、不送任何 Phase 6 command（counts 維持 0/0/0），
        #    由 HandoffRunner 走 restore responsibility path。
        if self._revalidate is not None:
            rv_ok, rv_checks, rv_verdict = self._revalidate(observation)
            self.revalidation = {"ok": rv_ok, "checks": rv_checks,
                                 "verdict": rv_verdict}
            out["revalidation"] = self.revalidation
            if not rv_ok:
                bad = [k for k, v in rv_checks.items() if not v]
                out["reason"] = "%s:%s" % (OUT_ABORTED_POST_HANDOFF, bad)
                out["released"] = not self.lease.held
                self.last = out
                return out

        # ---- 1. Phase 6 取得控制權（external 已 verified paused 才會走到這）----
        # 🔴 ordering（O8）：ENTRY_GATE_PASS 之後才會走到這一行；
        #    authority 事件必須由**真實的** acquire 呼叫產生，
        #    不得由測試手填。
        self._note(EV_AUTHORITY_ACQUIRE_ATTEMPT)
        ok, why = self.lease.acquire()
        if not ok:
            # 🔴 即使從未取得，也記一筆 release（no-op）——
            #    這樣「release 先於 restore」在**所有**路徑上都可被證明，
            #    而不是只在有取得控制權時才有序列可查。
            self.lease.release()
            # 🔴 O1：entry gate 已 PASS 不代表可以送指令。
            #    authority 取不到 -> 不進 scope、DISPATCH_ENABLED 維持 False、
            #    0 CHARGE / 0 DISCHARGE / 0 PCS STOP。
            out["reason"] = "PHASE6_AUTHORITY_NOT_ACQUIRED:%s" % (why,)
            out["released"] = not self.lease.held
            self.last = out
            return out

        self._note(EV_AUTHORITY_ACQUIRED)

        # 🔴 DEADLINE CHECKPOINT 5 —— immediately after authority acquire
        #    CASE C：authority 已取得但 CHARGE 尚未送；到期則
        #    CHARGE = 0，走 release -> restore（`finally` 已保證 release）。
        _ok5, _v5, _d5 = _DL.deadline_check(self.deadline,
                                            _DL.CK_AFTER_AUTHORITY_ACQUIRE)
        if not _ok5:
            self.deadline_blocked_at = _DL.CK_AFTER_AUTHORITY_ACQUIRE
            out["reason"] = "%s:%s@%s" % (OUT_DEADLINE_BLOCKED, _v5,
                                          _DL.CK_AFTER_AUTHORITY_ACQUIRE)
            self.lease.release()
            out["released"] = not self.lease.held
            out["outcome"] = DISPATCH_NOT_ATTEMPTED
            self.last = out
            return out

        live = None
        exc = None
        try:
            # ---- 2~3. 指令額度 ＋ 既有 5 kW charge-only live leg ----
            # 🔴 dispatch 能力只在此 scope 內存在；finally 無條件關閉。
            #    permit 為 None 時維持舊行為（由 HandoffRunner step 7 擋下）。
            # 🔴 scope 必須**涵蓋** `guard.take()` —— permit 的第 15 項前置條件
            #    是「CHARGE 額度尚未使用」，若先扣額度再評估 permit，
            #    該條件永遠不可能成立。
            # 🔴 DEADLINE CHECKPOINT 6 —— before FULL PERMIT / dispatch scope
            _ok6, _v6, _d6 = _DL.deadline_check(self.deadline,
                                                _DL.CK_BEFORE_FULL_PERMIT)
            if not _ok6:
                self.deadline_blocked_at = _DL.CK_BEFORE_FULL_PERMIT
                raise FirstLivePermitDenied(
                    "%s:%s@%s" % (OUT_DEADLINE_BLOCKED, _v6,
                                  _DL.CK_BEFORE_FULL_PERMIT))
            scope = (first_live_dispatch_scope(self.permit,
                                               on_event=self._permit_event)
                     if self.permit is not None else contextlib.nullcontext())
            with scope:
                # 🔴 DEADLINE CHECKPOINT 7 —— immediately before CHARGE send
                #    `guard.take()` 之前擋下 -> 指令額度不會被消耗。
                _ok7, _v7, _d7 = _DL.deadline_check(
                    self.deadline, _DL.CK_BEFORE_CHARGE_SEND)
                if not _ok7:
                    self.deadline_blocked_at = _DL.CK_BEFORE_CHARGE_SEND
                    raise FirstLivePermitDenied(
                        "%s:%s@%s" % (OUT_DEADLINE_BLOCKED, _v7,
                                      _DL.CK_BEFORE_CHARGE_SEND))
                self.guard.take("phase6_%s" % self.action)
                self._note(EV_CHARGE_SEND)
                if self.rec is not None:
                    self.rec.record(EV_DISPATCH_ATTEMPTED, self.action)
                live = self._run_leg()
        except Exception as e:                                  # noqa: BLE001
            exc = e
            out["reason"] = "%s: %s" % (type(e).__name__, e)
        finally:
            # 🔴 無論成敗，Phase 6 控制權一律先釋放
            self.lease.release()
            out["released"] = not self.lease.held

        # 🔴 DEADLINE CHECKPOINT 8 —— live-leg transition / cleanup
        #    CASE D：CHARGE 已送出之後才跨 deadline ——
        #    **不得** hard abort；cleanup 一律放行（`_DL` 保證回 True）。
        #    這裡只留痕，不改變任何流程。
        _DL.deadline_check(self.deadline, _DL.CK_LIVE_LEG_CLEANUP)

        self.last_live_outcome = live
        outcome = classify_dispatch_outcome(live, exception=exc)
        out["outcome"] = outcome
        out["live_leg"] = (live.as_dict() if hasattr(live, "as_dict") else None)
        if live is not None:
            n_stop = int(getattr(live, "stop_count", 0) or 0)
            # 🔴 RULING 2：live-leg cleanup STOP **必須**計入 aggregate，
            #    subtype 另存 —— 不得用命名把它排除在總數之外。
            self.guard.record("live_leg_internal_stop", n_stop)
            for _ in range(n_stop):
                self.guard.take("phase6_pcs_stop")
        if self.rec is not None:
            self.rec.record(EV_DISPATCH_OUTCOME, outcome)

        out["dispatched"] = outcome in (DISPATCH_SUCCEEDED, DISPATCH_FAILED,
                                        DISPATCH_UNKNOWN)
        # 🔴 只有明確成功才算 verified —— UNKNOWN 一律不得當成通過
        out["verified"] = outcome == DISPATCH_SUCCEEDED
        if out["reason"] is None:
            out["reason"] = getattr(live, "reason", None)

        # ---- 4. P6-A：接回既有 Durable Decision Audit（沿用現有 schema）----
        if self._audit is not None:
            try:
                out["audit"] = self._audit(live)
            except Exception as e:                              # noqa: BLE001
                # audit 永遠不得影響控制流程（NON_GATING_CONTINUE）
                out["audit"] = ("AUDIT_WRITER_EXCEPTION",
                                "%s: %s" % (type(e).__name__, e))
        self.last = out
        return out


# ======================================================================
# P6-A：Durable Decision Audit 接線（不新增任何欄位）
# ======================================================================
class _LiveLegEvidence(object):
    """`SVC.write_audit` 需要的最小 evidence 形狀。

    🔴 只提供 `execution_result` / `observation` / `local_now` 三個既有屬性，
       **不擴充** Durable Decision Audit 的 46 欄 schema。
       取不到就是 None -> 對應欄位 null，不猜、不補預設值。
    """

    def __init__(self, execution_result=None, observation=None, local_now=None):
        self.execution_result = execution_result
        self.observation = observation
        self.local_now = local_now


def build_first_live_audit_writer(sink, result_capture=None,
                                  observation_source=None, local_now=None,
                                  config=None, write_audit=None):
    """
    回傳 `(live_outcome) -> (audit_outcome, seq)`。

    🔴 P6-A：沿用既有 Durable Decision Audit，**不新增 handoff 欄位**。
       handoff / ownership 證據一律進 6.10 execution artifact（P6-B）。
    🔴 `sink` 未注入 -> 完全不寫檔。
    """
    if write_audit is None:
        import pcs_auto_control_service as SVC
        write_audit = SVC.write_audit

    def _write(_live_outcome=None):
        if sink is None:
            return ("AUDIT_DISABLED", None)
        exec_result = getattr(result_capture, "latest", None)
        obs = observation_source() if observation_source is not None else None
        ev = _LiveLegEvidence(exec_result, obs, local_now)
        return write_audit(sink, None, evidence=ev, config=config,
                           local_now=local_now)

    return _write


# ======================================================================
# 兩層 FIRST LIVE PRECHECK
# ======================================================================
# LAYER 1：只確認「現在動手是安全的」。
# 🔴 **不得**要求 external 本來 idle —— 那正是 6.9 的 TOCTOU 缺陷。
SAFETY_PREFLIGHT_ITEMS = (
    "communication_healthy", "source_available", "fault_clear",
    "critical_alarm_clear", "screen_alive", "process_alive",
    "process_identity_ok", "ownership_not_both", "guard_variant_b2",
    "mode_dry_run_baseline", "senders_not_armed_baseline",
)

# LAYER 2：external handoff 是否具備可執行條件。
HANDOFF_READINESS_ITEMS = (
    "external_runtime_confirmable", "loop_evidence_available",
    "pause_capability_available", "restore_capability_available",
)


def first_live_safety_preflight(sample, probe, safety_flags):
    """LAYER 1。回傳 (ok, checks)。**任一未知一律 False（Fail Closed）。**"""
    sample = sample or {}
    probe = probe or {}
    flags = safety_flags or {}
    checks = {
        "communication_healthy": sample.get("comm_ok") is True,
        "source_available": sample.get("fresh") is True,
        "fault_clear": sample.get("fault") is False,
        "critical_alarm_clear": sample.get("critical_alarms") == 0,
        "screen_alive": probe.get("screen_alive") is True,
        "process_alive": probe.get("process_alive") is True,
        "process_identity_ok": probe.get("process_identity_ok") is True,
        "ownership_not_both": sample.get("ownership") != "BOTH",
        "guard_variant_b2": probe.get("guard_variant") == "B2",
        "mode_dry_run_baseline": flags.get("mode") == "DRY_RUN",
        "senders_not_armed_baseline": flags.get("senders_armed") is False,
    }
    return all(checks.values()), checks


def handoff_readiness(probe, loop_marks=None, min_loop_observations=None,
                      capability=None, pause_capable=None,
                      restore_capable=None):
    """
    LAYER 2。回傳 (ok, checks)。

    🔴 `pause_capable` / `restore_capable` 未注入時委派給
       `phase6_unattended_service.remote_pause_capable / remote_restore_capable`
       —— **不在本模組另立第二套 capability 判準。**
    """
    probe = probe or {}
    if pause_capable is None or restore_capable is None:
        import phase6_unattended_service as US
        pause_capable = (US.remote_pause_capable if pause_capable is None
                         else pause_capable)
        restore_capable = (US.remote_restore_capable if restore_capable is None
                           else restore_capable)
    p = probe if isinstance(probe, RA.RemoteProbe) else RA.RemoteProbe(
        ssh_ok=probe.get("ssh_ok"), screen_alive=probe.get("screen_alive"),
        process_alive=probe.get("process_alive"),
        process_identity_ok=probe.get("process_identity_ok"))
    verdict, _why = RA.external_liveness(
        p, loop_marks=loop_marks, min_loop_observations=min_loop_observations)
    checks = {
        "external_runtime_confirmable": verdict in RA.EXTERNAL_LIVENESS_VERDICTS
                                        and p.known is True,
        "loop_evidence_available": RA.loop_active_from_marks(
            loop_marks, min_loop_observations) is not None,
        "pause_capability_available": bool(pause_capable(capability)),
        "restore_capability_available": bool(restore_capable(capability)),
    }
    return all(checks.values()), checks


# ======================================================================
# GATE A —— PRE-HANDOFF POLICY ELIGIBILITY
# ======================================================================
# 🔴 本 Gate 只回答一個問題：
#        「**如果** ownership 可以合法取得，
#          目前正式策略是否應該執行 CHARGE？」
#
# 🔴 **絕對不看 Control Authority。**
#    `Authority = EXTERNAL_OR_UNKNOWN` 在 external identity / runtime /
#    handoff capability 都正常時，代表的是 **HANDOFF REQUIRED**，
#    **不是** FIRST LIVE POLICY INELIGIBLE。
#    若在此處因 `CONTROL_AUTHORITY_BLOCKED` 就 abort，等於要求
#    「Authority 必須先 IDLE 才能進 handoff」——那正是舊 6.9 的 TOCTOU 問題，
#    也讓 Phase 6.10 handoff 失去存在意義（handoff 的前提本來就是
#    external 目前持有控制權）。
# 🔴 合法 PCS 觀測狀態。與 `phase6_trial2_harness.LEGAL_OBSERVED_PCS_STATES`
#    保持同值（由回歸測試鎖住兩者相等）。此處**不 import** trial harness ——
#    FIRST LIVE 整合層不應相依於 Trial #2 tooling。
LEGAL_OBSERVED_PCS_STATES = ("CHARGING", "DISCHARGING", "STANDBY", "STOPPED")


def pcs_state_legal(pcs_state):
    """PCS 觀測狀態是否合法。UNKNOWN / FAULT / None / 未知字串一律 False。"""
    return pcs_state in LEGAL_OBSERVED_PCS_STATES


PRE_HANDOFF_ELIGIBILITY_ITEMS = (
    "natural_decision_is_charge", "tou_allows_charge", "soc_allows_charge",
    "fault_clear", "critical_alarm_clear", "communication_healthy",
    "required_data_available", "pcs_state_legal", "non_ownership_safety_ok",
)

ELIGIBLE_HANDOFF_REQUIRED = "CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED"
NOT_ELIGIBLE = "CHARGE_POLICY_NOT_ELIGIBLE"

# 🔴 FIRST LIVE 一律經過 handoff（P3 裁示）。**刻意沒有** "不需 handoff" 的分支 ——
#    留一條可跳過 handoff 的路，就等於把 6.9 的 TOCTOU 漏洞再開一次。
HANDOFF_ALWAYS_REQUIRED = True


def pre_handoff_policy_eligibility(sample):
    """
    GATE A。回傳 (eligible, checks, verdict)。

    sample 需提供（皆由既有 production 決策鏈算出後填入，本函式不自行推導）：
        decision / tou_allows_charge / soc_allows_charge / fault /
        critical_alarms / comm_ok / fresh / meter_fresh / pcs_state /
        safety_gate_ok（非 ownership 類的既有 Safety Gate 結論；None = 未知）

    🔴 任一未知一律 False（Fail Closed）。
    🔴 **不讀** authority / arbitration_outcome / authorized —— 由 AST 測試鎖住。
    """
    sample = sample or {}
    checks = {
        "natural_decision_is_charge": sample.get("decision") == "charge",
        "tou_allows_charge": sample.get("tou_allows_charge") is True,
        "soc_allows_charge": sample.get("soc_allows_charge") is True,
        "fault_clear": sample.get("fault") is False,
        "critical_alarm_clear": sample.get("critical_alarms") == 0,
        "communication_healthy": sample.get("comm_ok") is True,
        "required_data_available": (sample.get("fresh") is True
                                    and sample.get("meter_fresh") is True),
        "pcs_state_legal": pcs_state_legal(sample.get("pcs_state")),
        "non_ownership_safety_ok": sample.get("safety_gate_ok") is not False,
    }
    ok = all(checks.values())
    return ok, checks, (ELIGIBLE_HANDOFF_REQUIRED if ok else NOT_ELIGIBLE)


def handoff_requirement(sample=None, loop_marks=None,
                        min_loop_observations=None):
    """
    說明「為什麼仍需 handoff」的**證據**，不是是否可跳過 —— 永遠 required。

    回傳 (required, evidence)。`required` 恆為 True（`HANDOFF_ALWAYS_REQUIRED`）。
    """
    sample = sample or {}
    loop_active = RA.loop_active_from_marks(loop_marks, min_loop_observations)
    evidence = {
        "authority_external_or_unknown":
            sample.get("authority") == "EXTERNAL_OR_UNKNOWN",
        "external_loop_active": loop_active,
        "handoff_always_required": HANDOFF_ALWAYS_REQUIRED,
        # 🔴 Trial #2 實測：external 可在 STANDBY/IDLE 期間 loop 持續 advancing，
        #    因此 Authority=IDLE **不足以**證明 external 已 relinquish。
        "authority_idle_is_not_relinquish_proof": True,
    }
    return True, evidence


# ======================================================================
# GATE B —— POST-HANDOFF DISPATCH REVALIDATION
# ======================================================================
# 🔴 pause 之後**必須用新的 fresh 觀測**重新確認策略仍成立。
#    不得沿用 pause 前的 decision / SOC / TOU / PCS state 直接 dispatch。
POST_HANDOFF_REVALIDATION_ITEMS = (
    "fresh_reobservation", "natural_decision_is_charge", "tou_allows_charge",
    "soc_allows_charge", "fault_clear", "critical_alarm_clear",
    "communication_healthy", "pcs_state_legal", "ownership_not_both",
    "external_relinquished",
)

REVALIDATION_OK = "POST_HANDOFF_REVALIDATION_OK"
REVALIDATION_FAILED = "POST_HANDOFF_REVALIDATION_FAILED"


def post_handoff_revalidation(sample):
    """
    GATE B。回傳 (ok, checks, verdict)。

    🔴 與 GATE A 不同：這裡**必須**要求 `authority == "IDLE"`
       （`external_relinquished`）—— 因為此時 pause 已 VERIFIED，
       external 理應已交出控制權；若仍非 IDLE，代表交接未真正完成。
    🔴 `fresh_reobservation` 由呼叫端標記為 True，代表本筆是 pause 之後
       重新取得的觀測。未標記 -> False（Fail Closed），不得沿用舊樣本。
    """
    sample = sample or {}
    checks = {
        "fresh_reobservation": sample.get("post_handoff_fresh") is True,
        "natural_decision_is_charge": sample.get("decision") == "charge",
        "tou_allows_charge": sample.get("tou_allows_charge") is True,
        "soc_allows_charge": sample.get("soc_allows_charge") is True,
        "fault_clear": sample.get("fault") is False,
        "critical_alarm_clear": sample.get("critical_alarms") == 0,
        "communication_healthy": sample.get("comm_ok") is True,
        "pcs_state_legal": pcs_state_legal(sample.get("pcs_state")),
        "ownership_not_both": sample.get("ownership") != "BOTH",
        "external_relinquished": sample.get("authority") == "IDLE",
    }
    ok = all(checks.values())
    return ok, checks, (REVALIDATION_OK if ok else REVALIDATION_FAILED)


def build_post_handoff_revalidator(sample_source=None):
    """
    組出可餵給 `FirstLiveDispatcher(revalidator=...)` 的 GATE B 檢查。

    `sample_source` 未注入時，直接使用 dispatcher 收到的 observation ——
    那是 `HandoffRunner` step 6 取得的 **post-pause fresh** 樣本。
    """
    def _revalidate(observation=None):
        smp = sample_source() if sample_source is not None else observation
        return post_handoff_revalidation(smp)
    return _revalidate


# ======================================================================
# P6-B：6.10 Handoff Execution Artifact（與 Decision Audit 分離）
# ======================================================================
HANDOFF_ARTIFACT_SCHEMA_VERSION = 1

HANDOFF_ARTIFACT_FIELDS = (
    "schema_version", "artifact_kind", "started_at", "finished_at",
    # --- preflight ---
    "safety_preflight_ok", "safety_preflight_checks",
    "handoff_readiness_ok", "handoff_readiness_checks",
    # --- GATE A / GATE B ---
    "pre_handoff_eligibility_ok", "pre_handoff_eligibility_checks",
    "pre_handoff_verdict", "handoff_requirement_evidence",
    "post_handoff_revalidation_ok", "post_handoff_revalidation_checks",
    "post_handoff_verdict",
    # --- external pause ---
    "pause_attempt_count", "pause_step_attempt_count",
    "pause_send_attempt_count", "pause_send_outcome",
    "pause_transitional_control_effect",
    "normal_loop_halted", "pause_verifier_state",
    "discriminative_pause_verified",
    # --- responsibility ---
    "restore_responsibility_lifecycle", "restore_responsibility_final",
    # --- Phase 6 側 ---
    "phase6_authority_acquired", "phase6_authority_released",
    "phase6_authority_detail",
    "dispatcher_attempted", "dispatcher_outcome", "dispatcher_reason",
    "live_leg_outcome", "phase6_command_counts", "pcs_stop_reason",
    # --- external restore ---
    "restore_attempt_count", "restore_send_outcome",
    "restore_verifier_state", "restore_resumed",
    # --- 收尾 ---
    "post_check", "final_ownership", "fault", "critical_alarms",
    "release_before_restore", "lifecycle", "handoff_outcome",
    "timeout_status",
    # --- Attempt #3 postmortem：終態分類必須依實際 stage ---
    "handoff_state", "handoff_abort_step", "handoff_abort_detail",
    "authority_acquire_attempted",
    # --- Attempt #4 postmortem：permit ＋ liveness 歷程 ---
    "dispatch_permit", "scheduled_live_leg_authorization",
    "leg_reached_complete", "leg_state_history", "readback_outcome",
    "readback_observed_state", "readback_attempts", "readback_elapsed_sec",
    "live_leg_authorization_consumed",
    "pre_pause_external_liveness",
    "post_pause_external_liveness", "post_restore_external_liveness",
    "external_liveness_final",
    # --- Attempt #8 postmortem：two-stage authorization ＋ ordering ---
    "entry_gate_ok", "entry_gate_checks", "entry_gate_denied_reason",
    "dispatcher_entered", "dispatch_ordering_events",
    "dispatch_ordering_canonical",
    # --- Attempt #8 postmortem：ledger snapshot 取樣時點 ---
    "ledger_snapshot_is_pre_consume",
    # --- EXECUTION DEADLINE / SAFE CLEANUP（2026-10-02 裁示）---
    "deadline", "deadline_blocked_at", "deadline_expired",
    "deadline_remaining_sec", "deadline_events",
    "deadline_crossed_during_live_leg",
    # --- REPORTING CONTRACT（2026-10-02 裁示）---
    #     attempt_result 由 scheduled runner 回填；其餘皆由既有證據導出
    "attempt_result", "live_leg_result", "counts_as_first_live_success",
    "deadline_exceeded", "readback_result", "cleanup_stop_result",
    "authority_release_result", "restore_result",
    # --- Attempt #9 postmortem：AUTHORITY EVIDENCE ---
    "authority_snapshots", "authority_result", "authority_reason",
    "authority_detail", "authority_at_abort",
    "authority_ttl_sec", "authority_power_tolerance_kw",
    # --- Attempt #9 postmortem：dispatch 與 leg 分離 ---
    "dispatch_result",
    # --- Attempt #9 postmortem：post-restore reconciliation ---
    "post_restore_reconciliation", "external_takeover",
    "external_takeover_checks", "external_takeover_verified",
    "manual_attention", "manual_attention_required",
    "manual_attention_reasons", "pcs_state_warning",
)

ARTIFACT_KIND = "PHASE6_10_HANDOFF_EXECUTION_ARTIFACT"

# 🔴 timeout 仍是 FIELD_VALIDATION_CANDIDATE —— 本整合**不**升格為 FINAL。
TIMEOUT_STATUS = {
    "pause_sec": 12.0, "restore_sec": 12.0,
    "status": "FIELD_VALIDATION_CANDIDATE", "final": False, "n": 2,
    "production_default_pause": None, "production_default_restore": None,
}


class HandoffExecutionArtifact(object):
    """6.10 ownership / handoff 證據。**與 Decision Audit 是兩套 schema，不揉合。**"""

    def __init__(self, now_fn=None):
        self._now = now_fn or (lambda: datetime.datetime.now())
        self.data = dict((k, None) for k in HANDOFF_ARTIFACT_FIELDS)
        self.data.update({
            "schema_version": HANDOFF_ARTIFACT_SCHEMA_VERSION,
            "artifact_kind": ARTIFACT_KIND,
            "started_at": self._now().isoformat(timespec="seconds"),
            "pause_transitional_control_effect": "ZERO-UNTIL-STOP-CONFIRMED",
            "pause_attempt_count": 0,
            "restore_attempt_count": 0,
            "dispatcher_attempted": False,
            "dispatcher_outcome": DISPATCH_NOT_ATTEMPTED,
            "phase6_authority_acquired": False,
            "phase6_authority_released": False,
            "restore_responsibility_lifecycle": [],
            "lifecycle": [],
            "timeout_status": dict(TIMEOUT_STATUS),
        })

    def set(self, **kw):
        for k, v in kw.items():
            if k not in HANDOFF_ARTIFACT_FIELDS:
                raise KeyError("未登記於 HANDOFF_ARTIFACT_FIELDS 的欄位：%s" % k)
            self.data[k] = v
        return self

    def note_responsibility(self, state, why=None):
        self.data["restore_responsibility_lifecycle"].append(
            {"state": bool(state), "why": why,
             "at": self._now().isoformat(timespec="seconds")})
        self.data["restore_responsibility_final"] = bool(state)
        return self

    def finish(self, outcome, recorder=None):
        self.data["handoff_outcome"] = outcome
        self.data["finished_at"] = self._now().isoformat(timespec="seconds")
        if recorder is not None:
            self.data["lifecycle"] = recorder.as_list()
            self.data["release_before_restore"] = recorder.ordered(
                EV_AUTHORITY_RELEASED, EV_RESTORE_ATTEMPTED)
        return self

    def to_dict(self):
        return dict(self.data)

    def write(self, path):
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2,
                      default=str)
        return path


def validate_handoff_artifact(record, limits=None):
    """回傳 problems 清單（空 = 合格）。供離線回歸鎖住 command-count 與順序。"""
    p = []
    if not isinstance(record, dict):
        return ["artifact 必須是 dict"]
    if record.get("schema_version") != HANDOFF_ARTIFACT_SCHEMA_VERSION:
        p.append("schema_version 不符")
    if record.get("artifact_kind") != ARTIFACT_KIND:
        p.append("artifact_kind 不符")
    for k in HANDOFF_ARTIFACT_FIELDS:
        if k not in record:
            p.append("缺少必要欄位：%s" % k)
    lim = dict(limits if limits is not None else CommandCountGuard.LIMITS)
    counts = record.get("phase6_command_counts") or {}
    got = counts.get("counts", {}) if isinstance(counts, dict) else {}
    for k, v in lim.items():
        n = int(got.get(k, 0) or 0)
        if n > v:
            p.append("command count 超限：%s=%d > %d" % (k, n, v))
    # 🔴 RULING 2：pcs_stop == 1 必須說得出 subtype；且 cleanup STOP 不得被
    #    排除於 aggregate 之外（subtype 數量不得大於 aggregate）。
    n_stop = int(got.get("phase6_pcs_stop", 0) or 0)
    n_sub = int(got.get("live_leg_internal_stop", 0) or 0)
    if n_stop >= 1 and not record.get("pcs_stop_reason"):
        p.append("phase6_pcs_stop=%d 但未說明 pcs_stop_reason" % n_stop)
    if n_sub > n_stop:
        p.append("live_leg_internal_stop=%d 未計入 phase6_pcs_stop=%d"
                 % (n_sub, n_stop))
    if record.get("dispatcher_outcome") not in DISPATCH_OUTCOMES:
        p.append("dispatcher_outcome 非法：%r" % (record.get("dispatcher_outcome"),))
    # 🔴 只要 Phase 6 曾取得控制權，release 就必須先於 restore
    if record.get("phase6_authority_acquired") and record.get("restore_attempt_count"):
        if record.get("release_before_restore") is not True:
            p.append("Phase 6 authority 未在 external restore 之前釋放")
    # 🔴 pause 可能成功之後，責任只能由 RV_RESUMED 清除
    if (record.get("pause_send_outcome") in (RA.SEND_SENT, RA.SEND_UNKNOWN)
            and record.get("restore_responsibility_final") is False
            and record.get("restore_verifier_state") != RA.RV_RESUMED):
        p.append("責任被清除但 RestoreVerifier 未達 RV_RESUMED")
    if record.get("timeout_status", {}).get("final") is not False:
        p.append("timeout 不得標記為 FINAL（仍為 FIELD_VALIDATION_CANDIDATE）")
    return p


# ======================================================================
# External sender 包裝 —— 讓「順序」與「指令上限」成為模組性質而非測試技巧
# ======================================================================
def wrap_pause_sender(sender, recorder=None, guard=None):
    """包裝 external pause sender：計數 + 事件。**不改變送出語意。**"""
    def _send():
        if guard is not None:
            guard.take("pause")
        return sender()
    return _send


def wrap_restore_sender(sender, recorder=None, guard=None):
    """
    包裝 external restore sender：計數 + 事件。

    🔴 `EV_RESTORE_ATTEMPTED` 在**呼叫 sender 之前**記錄 ——
       它與 `EV_AUTHORITY_RELEASED` 的先後，就是
       「Phase 6 release 必須先於 external restore」的證據。
    """
    def _send():
        if guard is not None:
            guard.take("restore")
        if recorder is not None:
            recorder.record(EV_RESTORE_ATTEMPTED)
        return sender()
    return _send


def note_restore_verified(recorder, restore_verifier_state):
    if recorder is not None and restore_verifier_state == RA.RV_RESUMED:
        recorder.record(EV_RESTORE_VERIFIED, restore_verifier_state)
    return restore_verifier_state


# ======================================================================
# 產線接線（P5）
# ======================================================================
def build_first_live_dispatcher(live_leg_runner, acquire_authority,
                                release_authority, command_guard=None,
                                recorder=None, audit_writer=None,
                                action=FIRST_LIVE_ACTION, revalidator=None,
                                dispatch_permit=None, permit_event=None,
                                deadline=None):
    """
    組出可直接餵給 `HandoffRunner(dispatcher=...)` 的 dispatcher。

    🔴 `live_leg_runner` 必須是**既有** live leg 的零參數包裝
       （production：`lambda: SVC.run_live_leg_cli(...)`）。
       本函式**不**建立 executor / verifier / operator，也不碰功率。

    🔴 OLD / NEW / WHY / EVIDENCE（2026-09-30 Attempt #6 postmortem）
      OLD signature :
          (live_leg_runner, acquire_authority, release_authority,
           command_guard=None, recorder=None, audit_writer=None,
           action=FIRST_LIVE_ACTION, revalidator=None)
      NEW signature :
          ... 同上 ... + dispatch_permit=None, permit_event=None
      OLD forwarding :
          FirstLiveDispatcher(live_leg_runner, lease, guard,
                              recorder=, audit_writer=, action=, revalidator=)
      NEW forwarding :
          ... 同上 ... + dispatch_permit=dispatch_permit,
                        permit_event=permit_event
      WHY : 2026-09-24 為 `FirstLiveDispatcher.__init__` 新增 permit 支援時，
            **漏改本工廠**；provider 以 kwargs 呼叫即 `TypeError`。
      EVIDENCE : Attempt #6（2026-09-30 00:05）
            `TypeError: build_first_live_dispatcher() got an unexpected
             keyword argument 'dispatch_permit'` -> EXCEPTION_FAIL_CLOSED。

    🔴 本工廠**只做 argument forwarding**：
       不在此建立 permit、不產生 permit_event、不以 `**kwargs` 吞掉未知參數
       （那會讓同類 wiring defect 再次被掩蓋）。
       caller -> factory -> dispatcher 必須維持 **same object identity**。
    """
    guard = command_guard if command_guard is not None else CommandCountGuard()
    lease = Phase6AuthorityLease(acquire_authority, release_authority,
                                 recorder=recorder)
    return FirstLiveDispatcher(live_leg_runner, lease, guard,
                               recorder=recorder, audit_writer=audit_writer,
                               action=action, revalidator=revalidator,
                               dispatch_permit=dispatch_permit,
                               permit_event=permit_event,
                               deadline=deadline
                               ), lease, guard


def _first_failed_step(res):
    """`HandoffRunner` 結果中第一個失敗的步驟名稱；全成功回 None。"""
    for s in ((res or {}).get("steps") or ()):
        if not s.get("ok"):
            return s.get("step")
    return None


def _first_failed_detail(res):
    for s in ((res or {}).get("steps") or ()):
        if not s.get("ok"):
            return s.get("detail")
    return None


def collect_handoff_artifact(runner_result, orchestrator=None, dispatcher=None,
                             lease=None, guard=None, recorder=None,
                             artifact=None, pause_verifier_state=None,
                             normal_loop_halted=None,
                             restore_verifier_state=None,
                             final_sample=None, now_fn=None,
                             post_check=None, permit=None, deadline=None,
                             reconciliation=None, external_liveness=None):
    """
    把 `HandoffRunner` 的結果 ＋ Phase 6 側證據彙整成 6.10 execution artifact。

    🔴 只讀既有物件，不重新觀測、不送任何指令。

    🔴 `permit` 為 `FirstLiveDispatchPermit`（2026-10-02 Attempt #8 postmortem
       新增，**顯式具名參數**，不以 `**kwargs` 吞掉）——
       用來分別記錄 STAGE A entry gate 與 control-flow ordering。
       省略時行為與舊版完全相同。

    🔴 `deadline` 為 `phase6_execution_deadline.ExecutionDeadline`
       （2026-10-02 裁示新增，**顯式具名參數**）——
       只外露既有判定留痕，不在此重新判定任何事。
    """
    art = artifact if artifact is not None else HandoffExecutionArtifact(now_fn)
    res = runner_result or {}
    steps = {s.get("step"): s for s in (res.get("steps") or [])}
    # 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-01 Attempt #7 postmortem）
    #   OLD : 只有 `pause_attempt_count`，語意是「2_PAUSE 這個**步驟**跑過」，
    #         很容易被讀成「真的送出過一次 pause」。
    #   NEW : 另外明示
    #           pause_step_attempt_count  步驟是否執行過（沿用舊值）
    #           pause_send_attempt_count  **實際送出**的 pause 次數
    #   WHY : Attempt #7 的 artifact 是 pause_attempt_count=1 但
    #         SSH pause verb = 0（被 OnceGuard 擋在送出之前）。
    #   EVIDENCE : Attempt #7 artifact ssh_verb_counts = {probe 4, loopcheck 6}
    #         —— 完全沒有 pause 動詞。
    #   🔴 `pause_attempt_count` 保留不變（backward compatible）。
    _pause_sent_n = (1 if steps.get("2_PAUSE", {}).get("ok")
                     or steps.get("2_PAUSE", {}).get("uncertain") else 0)
    art.set(
        pause_attempt_count=(1 if "2_PAUSE" in steps else 0),
        pause_step_attempt_count=(1 if "2_PAUSE" in steps else 0),
        pause_send_attempt_count=_pause_sent_n,
        pause_send_outcome=(RA.SEND_SENT if steps.get("2_PAUSE", {}).get("ok")
                            else (RA.SEND_UNKNOWN
                                  if steps.get("2_PAUSE", {}).get("uncertain")
                                  else (RA.SEND_NOT_SENT if "2_PAUSE" in steps
                                        else None))),
        # 🔴 Attempt #4 postmortem（EVIDENCE WIRING DEFECT）
        #   OLD : 只用呼叫端傳入的參數；provider 沒傳 -> 一律 None/False，
        #         即使 HandoffRunner 內的 PauseVerifier 實際已 VERIFIED。
        #   NEW : 呼叫端未提供時，改用 `HandoffRunner` 結果裡的 canonical
        #         evidence（`res` 已由 runner 外露），**不重新推算**。
        #   EVIDENCE : Attempt #4 artifact pause_verifier_state=None 對比
        #         handoff journal 3_4_PAUSE_VERIFY OK（external_quiescent 6 筆）。
        normal_loop_halted=(res.get("normal_loop_halted")
                            if normal_loop_halted is None
                            else normal_loop_halted),
        pause_verifier_state=(res.get("pause_verifier_state")
                              if pause_verifier_state is None
                              else pause_verifier_state),
        discriminative_pause_verified=(
            (res.get("normal_loop_halted") if normal_loop_halted is None
             else normal_loop_halted) is True
            and (res.get("pause_verifier_state")
                 if pause_verifier_state is None
                 else pause_verifier_state) == RA.PV_VERIFIED),
        restore_attempt_count=(1 if "10_RESTORE" in steps else 0),
        restore_send_outcome=(RA.SEND_SENT if steps.get("10_RESTORE", {}).get("ok")
                              else (RA.SEND_UNKNOWN
                                    if steps.get("10_RESTORE", {}).get("uncertain")
                                    else (RA.SEND_NOT_SENT if "10_RESTORE" in steps
                                          else None))),
        restore_verifier_state=(res.get("restore_verifier_state")
                                if restore_verifier_state is None
                                else restore_verifier_state),
        restore_resumed=((res.get("restore_verifier_state")
                          if restore_verifier_state is None
                          else restore_verifier_state) == RA.RV_RESUMED),
        release_before_restore=res.get("release_before_restore"),
        final_ownership=(res.get("ownership")
                         if orchestrator is None else orchestrator.ownership),
        handoff_outcome=res.get("outcome"),
        # 🔴 Attempt #3 postmortem：把「在哪一步中止」寫進 artifact，
        #    讓 scheduled runner 能依實際 stage 分類，而不是用
        #    「authority 沒拿到」反推成 AUTHORITY_ACQUIRE_FAILED。
        handoff_state=res.get("state"),
        handoff_abort_step=_first_failed_step(res),
        handoff_abort_detail=_first_failed_detail(res),
    )
    if permit is not None:
        # 🔴 STAGE A 與 STAGE B 分開記錄 —— 讀 artifact 的人必須能分辨
        #    「沒被允許進入 dispatcher」與「進去了但完整 permit 未通過」。
        art.set(entry_gate_ok=(permit.last_entry_checks is not None
                               and all(permit.last_entry_checks.values())),
                entry_gate_checks=permit.last_entry_checks,
                entry_gate_denied_reason=permit.entry_denied_reason,
                dispatch_ordering_events=list(permit.ordering_events()),
                dispatch_ordering_canonical=ordering_is_canonical(
                    permit.ordering_events()))
    if dispatcher is not None:
        art.set(dispatcher_entered=(int(getattr(dispatcher, "calls", 0) or 0)
                                    > 0))
        if getattr(dispatcher, "deadline_blocked_at", None) is not None:
            art.set(deadline_blocked_at=dispatcher.deadline_blocked_at)
    # 🔴 HandoffRunner 自己的 checkpoint 1~4 也會擋下 ——
    #    只讀 dispatcher 會漏掉「還沒進 dispatcher 就被 deadline 擋下」的情況。
    if res.get("deadline_blocked_at") is not None:
        art.set(deadline_blocked_at=res.get("deadline_blocked_at"))
    if deadline is not None:
        d = deadline.as_dict()
        art.set(deadline=d,
                deadline_expired=d.get("expired"),
                deadline_remaining_sec=d.get("remaining_sec"),
                deadline_events=d.get("events"))
        # 🔴 「CHARGE 已送出之後才跨 deadline」= CASE D，
        #    必須可從 artifact 直接看出，不靠事後推算。
        _charge_ev = [e for e in (d.get("events") or ())
                      if e.get("checkpoint") == _DL.CK_BEFORE_CHARGE_SEND
                      and e.get("allowed")]
        art.set(deadline_crossed_during_live_leg=bool(
            _charge_ev and d.get("expired")))
    if lease is not None:
        art.set(authority_acquire_attempted=getattr(
                    lease, "acquire_attempted", False),
                phase6_authority_acquired=lease.acquired_once,
                phase6_authority_released=(lease.release_ok is True
                                           or not lease.acquired_once),
                phase6_authority_detail=lease.as_dict())
    if dispatcher is not None:
        rv = dispatcher.revalidation or {}
        art.set(post_handoff_revalidation_ok=rv.get("ok"),
                post_handoff_revalidation_checks=rv.get("checks"),
                post_handoff_verdict=rv.get("verdict"))
        last = dispatcher.last or {}
        art.set(dispatcher_attempted=dispatcher.calls > 0,
                dispatcher_outcome=last.get("outcome", DISPATCH_NOT_ATTEMPTED),
                dispatcher_reason=last.get("reason"),
                # 🔴 三個 outcome 必須並存，避免把 restore 成功誤寫成 charge 成功
                live_leg_outcome=last.get("live_leg"))
        # 🔴 readback / leg 完成證據：只讀既有物件，不重新推算
        _lo = getattr(dispatcher, "last_live_outcome", None)
        if _lo is not None:
            _runner = getattr(_lo, "runner", None)
            _leg = getattr(_lo, "leg", None) or getattr(_runner, "leg", None)
            art.set(
                leg_reached_complete=leg_reached_complete(_lo),
                leg_state_history=list(getattr(_runner, "history", ()) or ()),
                live_leg_authorization_consumed=(
                    getattr(_leg, "state", None) == "CONSUMED"
                    if _leg is not None else None))
            # 🔴 readback 證據來自 `ControlledLiveLeg.dispatch_result`
            #    （既有 `ExecutionResult`，不重新推算、不另取觀測）
            _dr = getattr(_runner, "dispatch_result", None)
            if _dr is not None:
                art.set(
                    readback_outcome=getattr(_dr, "readback_outcome", None),
                    readback_observed_state=getattr(
                        _dr, "readback_observed_state", None),
                    readback_elapsed_sec=getattr(
                        _dr, "readback_elapsed_sec", None))
                _att = getattr(_dr, "readback_attempts", None)
                art.set(readback_attempts=(len(_att) if _att is not None
                                           else None))
    if guard is not None:
        art.set(phase6_command_counts=guard.as_dict(),
                pcs_stop_reason=guard.pcs_stop_reason)
    art.set(post_check=(res.get("post_check") if post_check is None
                        else post_check))
    if final_sample is not None:
        art.set(fault=final_sample.get("fault"),
                critical_alarms=final_sample.get("critical_alarms"))
    # 🔴 REPORTING CONTRACT：以**既有證據**導出可直接閱讀的欄位。
    #    讀 artifact 的人不必（也不得）只看 attempt_result 去推論 live leg。
    _d = art.to_dict()
    _counts = ((_d.get("phase6_command_counts") or {}).get("counts") or {})
    # 🔴 AUTHORITY EVIDENCE（Attempt #9 postmortem）——
    #    由 live leg 的既有快照帶出，本層不重新觀測、不重新判定。
    _snaps = None
    if dispatcher is not None:
        _lo = getattr(dispatcher, "last_live_outcome", None)
        # 🔴 快照掛在 `ControlledLiveLeg`（= outcome.runner）上，
        #    與 `leg_state_history` 走同一條既有取得路徑。
        _rn = getattr(_lo, "runner", None)
        _snaps = getattr(_rn, "authority_snapshots", None)
        if _snaps is None:
            _snaps = getattr(_lo, "authority_snapshots", None)
        if _snaps is None and isinstance(_lo, dict):
            _snaps = _lo.get("authority_snapshots")
    if _snaps:
        _abort = dict(_snaps).get("at_abort") or {}
        art.set(authority_snapshots={k: dict(v) for k, v in _snaps.items()},
                authority_at_abort=dict(_abort),
                authority_result=_abort.get("authority_result"),
                authority_reason=_abort.get("reason"),
                authority_detail=_abort.get("detail"),
                authority_ttl_sec=_abort.get("authority_ttl_sec"),
                authority_power_tolerance_kw=_abort.get(
                    "authority_power_tolerance_kw"))
    art.set(
        live_leg_result=derive_live_leg_result(_d),
        counts_as_first_live_success=counts_as_first_live_success(_d),
        deadline_exceeded=(_d.get("deadline_expired") is True),
        readback_result=_d.get("readback_outcome"),
        # cleanup STOP：有送出過才有結果可言
        cleanup_stop_result=(
            None if int(_counts.get("phase6_pcs_stop", 0) or 0) <= 0
            else ("SUCCESS" if _d.get("pcs_stop_reason") else "UNKNOWN")),
        authority_release_result=(
            None if _d.get("authority_acquire_attempted") is not True
            else ("SUCCESS" if _d.get("phase6_authority_released") is True
                  else "FAILED")),
        restore_result=(
            None if _d.get("restore_send_outcome") is None
            else ("SUCCESS" if _d.get("restore_resumed") is True
                  else _d.get("restore_send_outcome"))),
        # 🔴 dispatch 與 live leg 是兩件事：指令是否確實送出且回讀成功，
        #    與整條 leg 是否走完 cleanup，不得混為一談。
        dispatch_result=(
            None if int(_counts.get("phase6_charge", 0) or 0) <= 0
            else ("DISPATCH_SUCCEEDED"
                  if _d.get("readback_outcome") == "VERIFY_SUCCESS"
                  else _d.get("dispatcher_outcome"))),
    )
    # 🔴 POST-RESTORE RECONCILIATION ＋ MANUAL ATTENTION（Attempt #9 裁示）
    _d2 = art.to_dict()
    _c2 = ((_d2.get("phase6_command_counts") or {}).get("counts") or {})
    _charge_sent = int(_c2.get("phase6_charge", 0) or 0) > 0
    _stop_sent = int(_c2.get("phase6_pcs_stop", 0) or 0) > 0
    _recon = reconciliation if reconciliation is not None else None
    if _charge_sent and not _stop_sent:
        # 🔴 `external_liveness_final` 由 provider 在本函式**之後**才寫入
        #    artifact，因此這裡必須由呼叫端顯式傳入 canonical verdict；
        #    未傳入時才退回 artifact 既有欄位（兩者皆無 -> UNCONFIRMED）。
        _fin = _d2.get("external_liveness_final") or {}
        _lv = external_liveness
        if _lv is None:
            _lv = (_fin.get("verdict") if isinstance(_fin, dict) else None)
        if isinstance(_lv, dict):
            _lv = _lv.get("verdict")
        _tk, _tkc = evaluate_external_takeover(
            restore_verifier_state=_d2.get("restore_verifier_state"),
            external_liveness=_lv, reconciliation=_recon)
    else:
        _tk, _tkc = EXTERNAL_TAKEOVER_NOT_APPLICABLE, {}
    _ma, _mar = evaluate_manual_attention(
        live_leg_result=_d2.get("live_leg_result"), takeover=_tk,
        charge_sent=_charge_sent, stop_sent=_stop_sent)
    art.set(post_restore_reconciliation=_recon,
            external_takeover=_tk,
            external_takeover_checks=_tkc,
            external_takeover_verified=(_tk == EXTERNAL_TAKEOVER_VERIFIED),
            manual_attention=_ma,
            manual_attention_required=(_ma == MA_REQUIRED),
            manual_attention_reasons=_mar,
            # 🔴 指令已送出但 Phase 6 未自行收尾 -> artifact 必須明講，
            #    不得只寫 DISPATCH_UNKNOWN。
            pcs_state_warning=(PCS_MAY_STILL_BE_UNDER_PHASE6_SETPOINT
                               if (_charge_sent and not _stop_sent
                                   and _tk != EXTERNAL_TAKEOVER_VERIFIED)
                               else None))
    art.note_responsibility(bool(res.get("restore_responsibility")),
                            res.get("outcome"))
    art.finish(res.get("outcome"), recorder=recorder)
    return art


# ======================================================================
def main(argv=None):
    """離線自述。**不做任何 I/O，不接觸設備。**"""
    print("=" * 72)
    print("  Phase 6 FIRST LIVE  x  Phase 6.10 Handoff —— 整合層（離線）")
    print("=" * 72)
    print("  FIRST LIVE PATH   : Phase 6.10 HANDOFF PATH（P3 裁示 2026-09-18）")
    print("  FIRST LIVE ACTION : %s（ONE CONTROLLED LEG ONLY）" % FIRST_LIVE_ACTION)
    print("  功率來源          : 既有 ProductionConfig，本模組不定義、不 override")
    print()
    print("  dispatcher outcomes : %s" % (list(DISPATCH_OUTCOMES),))
    print("  command limits      : %r" % (CommandCountGuard.LIMITS,))
    print("  PCS STOP subtype    : %s（計入 aggregate，非豁免）"
          % (list(CommandCountGuard.SUBTYPE_COUNTERS),))
    print()
    print("  GATE A PRE-HANDOFF ELIGIBILITY : %d 項（**不看 Authority**）"
          % len(PRE_HANDOFF_ELIGIBILITY_ITEMS))
    print("    Authority=EXTERNAL_OR_UNKNOWN -> HANDOFF_REQUIRED，非 INELIGIBLE")
    print("  GATE B POST-HANDOFF REVALIDATION: %d 項（**必須** authority IDLE）"
          % len(POST_HANDOFF_REVALIDATION_ITEMS))
    print("  HANDOFF_ALWAYS_REQUIRED    : %r（無跳過分支）" % HANDOFF_ALWAYS_REQUIRED)
    print("  LAYER 1 SAFETY PREFLIGHT   : %d 項" % len(SAFETY_PREFLIGHT_ITEMS))
    print("    [!] 不得要求 external 本來 idle（6.9 TOCTOU 缺陷）")
    print("  LAYER 2 HANDOFF READINESS  : %d 項" % len(HANDOFF_READINESS_ITEMS))
    print()
    print("  ownership 分離 :")
    print("    EXTERNAL CONTROLLER OWNERSHIP -> 6.10 pause/restore responsibility")
    print("    PHASE 6 CONTROL AUTHORITY     -> Phase6AuthorityLease")
    print("  順序保證 : Phase 6 release（finally）先於 external restore")
    print()
    print("  artifact : %s v%d，%d 欄"
          % (ARTIFACT_KIND, HANDOFF_ARTIFACT_SCHEMA_VERSION,
             len(HANDOFF_ARTIFACT_FIELDS)))
    print("  timeout  : %s（NOT FINAL，n=%d）"
          % (TIMEOUT_STATUS["status"], TIMEOUT_STATUS["n"]))
    print("  DISPATCH_ENABLED : %r（仍為 False）" % HO.DISPATCH_ENABLED)
    print()
    print("  [!] 本模組不執行 FIRST LIVE，也不含任何 SSH / HTTP / subprocess。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
