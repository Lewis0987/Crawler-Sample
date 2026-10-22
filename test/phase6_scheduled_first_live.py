# -*- coding: utf-8 -*-
"""
phase6_scheduled_first_live.py — Scheduled FIRST LIVE Attempt（ONE-SHOT）
======================================================================
定位（使用者 2026-09-21 裁示）
    **一次性 commissioning runner**。

    不是 Unattended LIVE。
    不是 recurring schedule。
    不是每日尖離峰自動控制。
    不是背景永久排程服務。
    不是 auto retry。

    使用者白天先明確授權一次；runner 只允許在指定 field window 內
    自動做**一次** fresh eligibility 判斷。條件不成立即 ABORT，
    **NO CONTROL / NO RETRY**。

🔴 **不建立第二套 FIRST LIVE control logic**
    本模組只是包在既有流程外層的 **orchestration shell**：
        Scheduler / One-Shot Gate
          -> Fresh STEP 0 -> Fresh Safety Preflight -> GATE A
          -> Phase 6.10 Handoff（pause / PauseVerifier / GATE B /
             Phase6AuthorityLease / FIRST LIVE Dispatcher / 既有 6.9 live leg /
             cleanup STOP / release / restore / RestoreVerifier）
          -> post-check -> artifact -> SELF DISARM / EXIT
    decision logic / Safety Gate / executor / readback / live-leg / handoff
    **一律沿用既有實作**，本模組全部以注入取得，且不含任何 I/O
    （唯一例外是 durable ledger 的檔案讀寫 —— crash safety 必需）。

🔴 **ONE-SHOT CONSUMED**
    任何結果（PASS / ABORT / FAIL / UNKNOWN / EXCEPTION / TIMEOUT / EXPIRED）
    都會把授權標記為已消耗；同一個 task 不得再次執行。

🔴 **CRASH / RESTART**
    durable ledger 記錄生命週期；重啟後一律
    **NO RESEND pause / NO RESEND charge / NO RESEND restore**，
    UNKNOWN -> FAIL CLOSED。

用法
    python phase6_scheduled_first_live.py      # 離線自述；不做任何 I/O
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_first_live_handoff as FL              # noqa: E402

SCHEDULED_MODE = "ONE_SHOT_COMMISSIONING"
IS_UNATTENDED_LIVE = False
IS_RECURRING = False
AUTO_RETRY_ALLOWED = False


# ======================================================================
# Result states
# ======================================================================
R_EXPIRED = "EXPIRED_NO_ATTEMPT"
R_ABORTED_PREFLIGHT = "ABORTED_PREFLIGHT"
R_ABORTED_CURRENT = "ABORTED_CURRENT_CONDITIONS"
R_ABORTED_READINESS = "ABORTED_HANDOFF_READINESS"
# 🔴 Attempt #3 postmortem：pre-pause preflight 中止是**獨立終態**，
#    以前被錯誤歸成 AUTHORITY_ACQUIRE_FAILED。
R_ABORTED_PRE_PAUSE = "ABORTED_PRE_PAUSE_PREFLIGHT"
R_ABORTED_HANDOFF_STEP = "ABORTED_HANDOFF_STEP"
# 🔴 Attempt #4 postmortem：7_FIRST_LIVE 中止是**獨立終態**，
#    以前被錯誤歸成 ABORTED_POST_HANDOFF_REVALIDATION。
R_ABORTED_FIRST_LIVE = "ABORTED_FIRST_LIVE"

# EXECUTION DEADLINE / SAFE CLEANUP (2026-10-02 ruling)
#    window_end = NO-NEW-RISK-ACTION DEADLINE. All three are
#    handled terminal results, never UNEXPECTED_LAUNCHER_FAILURE.
# 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-06 Attempt #9 postmortem）
#   OLD : dispatch 後 authority 轉 UNKNOWN -> attempt_result 記成
#         `DISPATCH_UNKNOWN`。
#   NEW : 另立 canonical `AUTHORITY_UNKNOWN_AFTER_DISPATCH`。
#   WHY : Attempt #9 的 dispatch **確定成功**（control_resp success ＋
#         readback VERIFY_SUCCESS ＋ observed_state CHARGING），
#         `DISPATCH_UNKNOWN` 會誤導成「不知道有沒有送出去」。
#   EVIDENCE : Attempt #9 artifact —— dispatch_count 1、
#         readback_outcome VERIFY_SUCCESS、leg_state_history 含 OWNED。
#   🔴 全專案只用這一個 enum 表達此終態；`DISPATCH_UNKNOWN` 保留給
#      「真的不知道有沒有送出」的情況。
R_AUTHORITY_UNKNOWN_AFTER_DISPATCH = "AUTHORITY_UNKNOWN_AFTER_DISPATCH"

R_DEADLINE_BEFORE_FIELD = "DEADLINE_BEFORE_FIELD_ACTION"
R_DEADLINE_DURING_HANDOFF = "DEADLINE_DURING_HANDOFF"
R_DEADLINE_DURING_LIVE_CLEANUP = "DEADLINE_DURING_LIVE_CLEANUP"
R_PAUSE_FAILED = "PAUSE_FAILED"
R_PAUSE_UNKNOWN = "PAUSE_UNKNOWN"
R_ABORTED_POST_HANDOFF = "ABORTED_POST_HANDOFF_REVALIDATION"
R_AUTHORITY_FAILED = "AUTHORITY_ACQUIRE_FAILED"
R_DISPATCH_FAILED = "DISPATCH_FAILED"
R_DISPATCH_UNKNOWN = "DISPATCH_UNKNOWN"
R_LIVE_LEG_VERIFY_FAILED = "LIVE_LEG_VERIFY_FAILED"
R_RESTORE_FAILED = "RESTORE_FAILED"
R_RESTORE_UNKNOWN = "RESTORE_UNKNOWN"
R_POSTCHECK_FAILED = "POSTCHECK_FAILED"
R_PASS = "PASS"
# 額外：授權層面的拒絕（不是 field 結果，但必須可分類）
R_NOT_ENABLED = "NOT_ENABLED"
R_ALREADY_CONSUMED = "ALREADY_CONSUMED"
R_WRONG_DATE = "WRONG_TARGET_DATE"
R_EXCEPTION = "EXCEPTION_FAIL_CLOSED"

# 🔴 失敗階段（供 postmortem 精確分類；不取代 result）
STAGE_DISPATCHER_CONSTRUCTION = "DISPATCHER_CONSTRUCTION"
STAGE_HANDOFF_EXECUTION = "HANDOFF_EXECUTION"
STAGE_PREFLIGHT = "PREFLIGHT"
STAGE_GATE_A = "GATE_A"
STAGE_HANDOFF_READINESS = "HANDOFF_READINESS"

# 🔴 失敗階段（供 postmortem 精確分類；**不取代** result）
#    Attempt #6 的例外發生在 dispatcher 建構，而不是任何 gate / dispatch，
#    因此必須能與「handoff 執行期間的例外」區分開。
STAGE_DISPATCHER_CONSTRUCTION = "DISPATCHER_CONSTRUCTION"
STAGE_HANDOFF_EXECUTION = "HANDOFF_EXECUTION"
FAILURE_STAGES = (STAGE_DISPATCHER_CONSTRUCTION, STAGE_HANDOFF_EXECUTION)

# 🔴 HandoffRunner 步驟 -> scheduled 終態。**只做映射，不判 policy。**
#    未列出的步驟一律落到 R_ABORTED_HANDOFF_STEP（誠實標示，不猜）。
STEP_RESULT_MAP = {
    "1_PRE_PAUSE_PREFLIGHT": R_ABORTED_PRE_PAUSE,
    "6_POST_PAUSE_PRECHECK": R_ABORTED_POST_HANDOFF,
    "7_FIRST_LIVE": R_ABORTED_FIRST_LIVE,
}

# 🔴 終態 precedence（2026-09-24 裁示第 11 項）
#      real terminal step  >  derived booleans  >  generic fallback
#    `handoff_abort_step` 是 HandoffRunner 實際記錄的失敗步驟，
#    **不得**因為 pause 已 SENT 就忽略它，也不得用
#    `DISPATCH_NOT_ATTEMPTED` 反推「一定是 GATE B fail」。
RESULT_PRECEDENCE = ("real_terminal_step", "derived_booleans",
                     "generic_fallback")

# ======================================================================
# ATTEMPT RESULT PRECEDENCE TABLE（2026-10-02 REPORTING CONTRACT 裁示）
# ======================================================================
# 🔴 核心原則：**deadline 不得掩蓋任何「不確定」或「未完成的安全責任」**。
#    deadline 只表示「排程時間契約違反」；它的優先序必須**低於**
#    SEND_UNKNOWN、restore responsibility 未解除、未完成 cleanup、
#    以及實際 control failure。
#
#    P0  SAFETY UNCERTAIN      責任可能仍成立 / 不得重送
#        pause UNKNOWN              -> PAUSE_UNKNOWN
#        restore UNKNOWN / 未 RESUMED -> RESTORE_UNKNOWN
#        restore NOT_SENT           -> RESTORE_FAILED
#        dispatcher UNKNOWN         -> DISPATCH_UNKNOWN
#    P1  CONTROL FAILURE
#        dispatcher FAILED          -> DISPATCH_FAILED
#    P2  DEADLINE（排程時間契約違反）
#        三個 DEADLINE_* 之一
#    P3  pause 明確未送出           -> PAUSE_FAILED
#    P4  real terminal step         -> STEP_RESULT_MAP
#    P5  derived booleans
#    P6  generic fallback           -> PASS
#
# 🔴 P2 置於 P0 / P1 之後**不是**把 deadline 降級 ——
#    attempt_result 只有一個欄位，必須先說出「最需要人處理的事」。
#    deadline 是否被跨越一律另由 artifact 的 `deadline_exceeded` /
#    `deadline_blocked_at` / `deadline_events` 保留，不會遺失。
ATTEMPT_RESULT_PRECEDENCE = (
    ("P0_SAFETY_UNCERTAIN",
     ("PAUSE_UNKNOWN", "RESTORE_UNKNOWN", "RESTORE_FAILED",
      "DISPATCH_UNKNOWN")),
    ("P1_CONTROL_FAILURE", ("DISPATCH_FAILED",)),
    ("P2_DEADLINE", ("DEADLINE_BEFORE_FIELD_ACTION",
                     "DEADLINE_DURING_HANDOFF",
                     "DEADLINE_DURING_LIVE_CLEANUP")),
    ("P3_PAUSE_NOT_SENT", ("PAUSE_FAILED",)),
    ("P4_REAL_TERMINAL_STEP", ("<STEP_RESULT_MAP>",)),
    ("P5_DERIVED_BOOLEANS", ("<derived>",)),
    ("P6_GENERIC_FALLBACK", ("PASS",)),
)

# 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-02 Attempt #8 postmortem）
#   OLD : `R_ABORTED_PRE_PAUSE` / `R_ABORTED_HANDOFF_STEP` /
#         `R_ABORTED_FIRST_LIVE` 在前幾輪新增後**未**列入 `RESULT_STATES`，
#         於是 `run_scheduled_first_live.classify_exit()` 落到
#         `EXIT_LAUNCHER_FAILURE`（exit 5 / UNEXPECTED_LAUNCHER_FAILURE）。
#   NEW : 三者皆為**安全的 handled terminal result**，正式納入 RESULT_STATES
#         -> `HANDLED_TERMINAL_RESULT`（exit 0）。
#   WHY : exit classification 表達的是「流程有沒有被處理並安全結束」，
#         不是「Attempt 有沒有成功」。
#   EVIDENCE : Attempt #8 launcher log ——
#         scheduled_result = ABORTED_FIRST_LIVE（安全 fail-closed、CHARGE 0）
#         但 exit_code = 5 / exit_classification = UNEXPECTED_LAUNCHER_FAILURE。
#   🔴 **不改變** Attempt RESULT 本身：Attempt #8 仍是 ABORTED_FIRST_LIVE。
#   🔴 這不是放寬 —— exit 0 的語意是 handled，不是 PASS；
#      PASS 只有 `R_PASS` 一個。
RESULT_STATES = (
    R_EXPIRED, R_ABORTED_PREFLIGHT, R_ABORTED_CURRENT, R_ABORTED_READINESS,
    R_ABORTED_PRE_PAUSE, R_ABORTED_HANDOFF_STEP, R_ABORTED_FIRST_LIVE,
    R_DEADLINE_BEFORE_FIELD, R_DEADLINE_DURING_HANDOFF,
    R_DEADLINE_DURING_LIVE_CLEANUP, R_AUTHORITY_UNKNOWN_AFTER_DISPATCH,
    R_PAUSE_FAILED, R_PAUSE_UNKNOWN, R_ABORTED_POST_HANDOFF,
    R_AUTHORITY_FAILED, R_DISPATCH_FAILED, R_DISPATCH_UNKNOWN,
    R_LIVE_LEG_VERIFY_FAILED, R_RESTORE_FAILED, R_RESTORE_UNKNOWN,
    R_POSTCHECK_FAILED, R_PASS,
    R_NOT_ENABLED, R_ALREADY_CONSUMED, R_WRONG_DATE, R_EXCEPTION,
)


# ======================================================================
# One-shot lifecycle（durable）
# ======================================================================
LC_NOT_STARTED = "NOT_STARTED"
LC_PREFLIGHT = "PREFLIGHT"
LC_EXTERNAL_PAUSE_RESPONSIBILITY = "EXTERNAL_PAUSE_RESPONSIBILITY"
LC_PHASE6_AUTHORITY = "PHASE6_AUTHORITY"
LC_LIVE_LEG = "LIVE_LEG"
LC_PHASE6_RELEASED = "PHASE6_RELEASED"
LC_RESTORE_PENDING = "RESTORE_PENDING"
LC_RESTORED = "RESTORED"
LC_COMPLETE = "COMPLETE"

LIFECYCLE_STATES = (
    LC_NOT_STARTED, LC_PREFLIGHT, LC_EXTERNAL_PAUSE_RESPONSIBILITY,
    LC_PHASE6_AUTHORITY, LC_LIVE_LEG, LC_PHASE6_RELEASED,
    LC_RESTORE_PENDING, LC_RESTORED, LC_COMPLETE,
)

# 🔴 一旦進入這些狀態，external 就**可能已被暫停** —— 責任必須跨 crash 存活。
PENDING_RESTORE_LIFECYCLE = frozenset({
    LC_EXTERNAL_PAUSE_RESPONSIBILITY, LC_PHASE6_AUTHORITY, LC_LIVE_LEG,
    LC_PHASE6_RELEASED, LC_RESTORE_PENDING,
})

# recovery verdict
REC_CLEAN = "RECOVERY_CLEAN"
REC_CONSUMED = "RECOVERY_ALREADY_CONSUMED"
REC_PENDING_RESTORE = "RECOVERY_PENDING_RESTORE"
REC_CRITICAL = "RECOVERY_CRITICAL_MANUAL"


# ======================================================================
# One-shot authorization（全部必填，production code 不得寫死日期/時段）
# ======================================================================
class OneShotAuthorization(object):
    """
    一次性 field 授權。

    🔴 `target_date` / `window_start` / `window_end` / `timezone_name`
       **全部必填**，且由 one-shot config 注入 ——
       **不得**寫死在 production code。
    """

    def __init__(self, attempt_id, enabled, target_date, window_start,
                 window_end, timezone_name, created_at=None):
        if not attempt_id:
            raise ValueError("attempt_id 必填")
        if enabled is not True:
            # 🔴 只有明確 True 才算啟用；None / 缺值一律視為未授權
            self.enabled = False
        else:
            self.enabled = True
        for name, v in (("target_date", target_date),
                        ("window_start", window_start),
                        ("window_end", window_end),
                        ("timezone_name", timezone_name)):
            if v is None or v == "":
                raise ValueError("%s 必填（不得預設）" % name)
        if not isinstance(target_date, datetime.date):
            raise ValueError("target_date 必須是 datetime.date")
        if not (isinstance(window_start, datetime.time)
                and isinstance(window_end, datetime.time)):
            raise ValueError("window_start / window_end 必須是 datetime.time")
        if not window_start < window_end:
            raise ValueError("window_start 必須早於 window_end（不跨日）")
        self.attempt_id = str(attempt_id)
        self.target_date = target_date
        self.window_start = window_start
        self.window_end = window_end
        self.timezone_name = str(timezone_name)
        self.created_at = created_at

    def start_dt(self):
        return datetime.datetime.combine(self.target_date, self.window_start)

    def end_dt(self):
        return datetime.datetime.combine(self.target_date, self.window_end)

    def as_dict(self):
        return {"attempt_id": self.attempt_id, "enabled": self.enabled,
                "target_date": self.target_date.isoformat(),
                "window_start": self.window_start.isoformat(),
                "window_end": self.window_end.isoformat(),
                "timezone": self.timezone_name,
                "created_at": (self.created_at.isoformat(timespec="seconds")
                               if isinstance(self.created_at,
                                             datetime.datetime)
                               else self.created_at),
                "mode": SCHEDULED_MODE,
                "is_unattended_live": IS_UNATTENDED_LIVE,
                "is_recurring": IS_RECURRING,
                "auto_retry_allowed": AUTO_RETRY_ALLOWED}


WIN_BEFORE = "BEFORE_WINDOW"
WIN_INSIDE = "INSIDE_WINDOW"
WIN_EXPIRED = "WINDOW_EXPIRED"
WIN_WRONG_DATE = "WRONG_DATE"


def window_state(now, auth):
    """
    回傳 (state, seconds_to_start)。`now` 必須是與授權同時區的 naive/aware
    datetime（由呼叫端保證，本模組**不新造 timezone helper**）。
    """
    if now is None:
        raise ValueError("now 必填（不得預設為系統時間）")
    n = now.replace(tzinfo=None) if now.tzinfo is not None else now
    if n.date() < auth.target_date:
        return WIN_BEFORE, (auth.start_dt() - n).total_seconds()
    if n.date() > auth.target_date:
        return WIN_WRONG_DATE, None
    if n < auth.start_dt():
        return WIN_BEFORE, (auth.start_dt() - n).total_seconds()
    if n > auth.end_dt():
        return WIN_EXPIRED, None
    return WIN_INSIDE, 0.0


# ======================================================================
# Durable ledger（crash / restart safety）
# ======================================================================
class OneShotLedger(object):
    """
    一次性授權的持久狀態。**append-only JSONL ＋ 立即 fsync。**

    🔴 crash 後的唯一真相來源。重啟一律依此判定，
       **不得**重送 pause / charge / restore。
    """

    SCHEMA_VERSION = 1

    def __init__(self, path, now_fn=None):
        if not path:
            raise ValueError("ledger path 必填")
        self.path = path
        self._now = now_fn or (lambda: datetime.datetime.now())
        self.records = []
        self._load()

    # ---- 讀 ----
    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.records.append(json.loads(line))
                except ValueError:
                    # 🔴 半截的最後一行 = crash 當下未寫完 -> 忽略該行，
                    #    但**不得**因此認為前面的狀態不存在。
                    continue

    def append(self, kind, **fields):
        rec = {"schema_version": self.SCHEMA_VERSION, "kind": kind,
               "seq": len(self.records) + 1,
               "at": self._now().isoformat(timespec="seconds")}
        rec.update(fields)
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.records.append(rec)
        return rec

    def mark(self, lifecycle, detail=None):
        if lifecycle not in LIFECYCLE_STATES:
            raise ValueError("未登記的 lifecycle：%r" % (lifecycle,))
        return self.append("LIFECYCLE", lifecycle=lifecycle, detail=detail)

    def mark_intent(self, verb, detail=None):
        """🔴 有副作用的動作：**意圖先落盤**，再執行。"""
        return self.append("INTENT", verb=verb, detail=detail)

    def mark_outcome(self, verb, outcome, detail=None):
        return self.append("OUTCOME", verb=verb, outcome=outcome,
                           detail=detail)

    def consume(self, result, detail=None):
        return self.append("CONSUMED", result=result, detail=detail)

    # ---- 衍生狀態 ----
    @property
    def lifecycle(self):
        for r in reversed(self.records):
            if r.get("kind") == "LIFECYCLE":
                return r.get("lifecycle")
        return LC_NOT_STARTED

    @property
    def consumed(self):
        return any(r.get("kind") == "CONSUMED" for r in self.records)

    @property
    def result(self):
        for r in reversed(self.records):
            if r.get("kind") == "CONSUMED":
                return r.get("result")
        return None

    def attempted(self, verb):
        return any(r.get("kind") == "INTENT" and r.get("verb") == verb
                   for r in self.records)

    def outcome_of(self, verb):
        for r in reversed(self.records):
            if r.get("kind") == "OUTCOME" and r.get("verb") == verb:
                return r.get("outcome")
        return None

    @property
    def restore_responsibility(self):
        """
        🔴 只要 pause 的 **INTENT** 落過盤，責任就成立 ——
           即使 OUTCOME 沒寫進去（crash 落在中間）也一樣。
           只有 restore 明確 RV_RESUMED 才解除。
        """
        if not self.attempted("pause"):
            return False
        return self.outcome_of("restore_verified") != "RV_RESUMED"

    def as_dict(self):
        return {"path": self.path, "lifecycle": self.lifecycle,
                "consumed": self.consumed, "result": self.result,
                "pause_attempted": self.attempted("pause"),
                "charge_attempted": self.attempted("charge"),
                "restore_attempted": self.attempted("restore"),
                "restore_responsibility": self.restore_responsibility,
                "record_count": len(self.records),
                # 🔴 Attempt #8 postmortem：artifact 內嵌的 ledger snapshot
                #    是在**最後一筆 CONSUMED 寫入之前**取樣的，
                #    因此 `consumed` / `record_count` 會比 ledger 檔案少一筆。
                #    這不是 execution defect，但必須可自我說明。
                "ledger_snapshot_is_pre_consume": not self.consumed}


def recover(ledger):
    """
    重啟後的判定。回傳 (verdict, action)。

    🔴 **一律不重送**。UNKNOWN -> FAIL CLOSED。
    """
    if ledger.consumed:
        return REC_CONSUMED, ("授權已消耗（result=%s）-> 拒絕再次執行"
                              % ledger.result)
    lc = ledger.lifecycle
    if lc == LC_NOT_STARTED:
        return REC_CLEAN, "尚未開始 -> 可在 window 內執行一次"
    if lc in (LC_RESTORED, LC_COMPLETE):
        return REC_CLEAN, "已收斂但未標記消耗 -> 補記消耗後結束，不重跑"
    if lc == LC_PREFLIGHT:
        if ledger.attempted("pause"):
            return REC_PENDING_RESTORE, ("preflight 階段已有 pause 意圖 "
                                         "-> 只可 restore，不得重送 pause")
        return REC_CLEAN, "停在 preflight 且未送任何指令 -> 視為未發生"
    if lc in PENDING_RESTORE_LIFECYCLE:
        return REC_PENDING_RESTORE, (
            "lifecycle=%s -> external 可能仍被暫停；"
            "只可執行 restore（且 restore 若已嘗試過則不得重送）" % lc)
    return REC_CRITICAL, "無法判定的 lifecycle=%r -> 需人工確認" % (lc,)


# ======================================================================
# Windows host runtime precondition（純判定；evidence 由呼叫端唯讀取得）
# ======================================================================
HOST_ITEMS = ("no_sleep", "no_hibernate", "no_auto_reboot_pending",
              "survives_unattended", "network_stable")


def evaluate_host_runtime(evidence):
    """
    回傳 (ok, checks)。**任一未知一律 False（Fail Closed）。**

    evidence 由呼叫端以**唯讀**方式取得（例如 `powercfg /q`、
    pending-reboot registry 查詢）。本函式不執行任何系統呼叫，
    也**不修改**任何電源設定。
    """
    e = evidence or {}
    checks = dict((k, e.get(k) is True) for k in HOST_ITEMS)
    return all(checks.values()), checks


# ======================================================================
# Scheduled runner —— orchestration shell
# ======================================================================
class ScheduledFirstLiveRunner(object):
    """
    一次性 scheduled FIRST LIVE。**全部相依由注入取得。**

    source_state_check()  -> dict   STEP 0（正式 source / 封鎖 / safe defaults）
    safety_preflight()    -> (ok, checks)
    gate_a()              -> (ok, checks, verdict)
    handoff_readiness()   -> (ok, checks)
    arm_and_run_handoff() -> dict   🔴 **唯一**會真的送指令的地方，
                                    內部即既有 HandoffRunner + FIRST LIVE dispatcher
    collect_artifact(res) -> dict
    safety_reset()        -> dict
    host_evidence()       -> dict   （可選）

    🔴 本類別**不含** decision / Safety Gate / executor / readback /
       live-leg / handoff 的任何實作。
    """

    def __init__(self, auth, ledger, now_fn, source_state_check,
                 safety_preflight, gate_a, handoff_readiness,
                 arm_and_run_handoff, collect_artifact=None,
                 safety_reset=None, host_evidence=None, waiter=None,
                 max_wait_sec=None, register_pause_intent_hook=None):
        if auth is None or ledger is None:
            raise ValueError("auth / ledger 必填")
        if now_fn is None:
            raise ValueError("now_fn 必填（不得預設為系統時間）")
        self.auth = auth
        self.ledger = ledger
        self._now = now_fn
        self._source_state = source_state_check
        self._preflight = safety_preflight
        self._gate_a = gate_a
        self._readiness = handoff_readiness
        self._handoff = arm_and_run_handoff
        self._collect = collect_artifact
        self._reset = safety_reset
        self._host = host_evidence
        self._wait = waiter or (lambda s: None)
        # 🔴 Attempt #3 postmortem（durable ledger ordering）：
        #    pause INTENT 不得在進入 HandoffRunner 前就落盤。
        #    正式順序 = pre-pause preflight PASS -> 即將呼叫 pause sender
        #    -> durable INTENT -> 實際 send。provider 以此 hook 在
        #    sender 呼叫的前一刻回呼；未提供 hook 即**沒有** pause 能力。
        self._register_pause_hook = register_pause_intent_hook
        self.max_wait_sec = max_wait_sec
        self.evidence = {}

    # ------------------------------------------------------------------
    def _finish(self, result, detail=None):
        """🔴 任何結果都 SELF DISARM ＋ ONE-SHOT CONSUMED。"""
        reset = self._reset() if self._reset is not None else None
        self.evidence.update(
            result=result, detail=detail, safety_reset=reset,
            one_shot_consumed=True,
            finished_at=self._now().isoformat(timespec="seconds"),
            ledger=self.ledger.as_dict())
        if not self.ledger.consumed:
            self.ledger.consume(result, detail)
        return dict(self.evidence)

    # ------------------------------------------------------------------
    def run(self):
        a = self.auth
        self.evidence = {
            "scheduled_attempt_id": a.attempt_id,
            "authorization": a.as_dict(),
            "authorization_created_at": a.as_dict()["created_at"],
            "window_start": a.start_dt().isoformat(timespec="seconds"),
            "window_end": a.end_dt().isoformat(timespec="seconds"),
            "timezone": a.timezone_name,
            "mode": SCHEDULED_MODE,
            "actual_start_at": self._now().isoformat(timespec="seconds"),
        }
        try:
            return self._run_inner()
        except Exception as e:                                  # noqa: BLE001
            # 🔴 例外一律 Fail Closed；durable evidence 已在各步驟落盤
            # 🔴 Attempt #6 postmortem：標記**真實失敗階段**。
            #    handoff 從未產生任何結果 -> 例外發生在 dispatcher 建構期；
            #    已有 handoff_result -> 例外發生在 handoff 執行期。
            self.evidence["failure_stage"] = (
                STAGE_HANDOFF_EXECUTION
                if self.evidence.get("handoff_result") is not None
                else STAGE_DISPATCHER_CONSTRUCTION)
            return self._finish(R_EXCEPTION,
                                "%s: %s" % (type(e).__name__, e))

    def _run_inner(self):
        a = self.auth

        # ---- 0. 授權層 ----
        if self.ledger.consumed:
            self.evidence["window_check_result"] = "NOT_EVALUATED"
            return self._finish(R_ALREADY_CONSUMED,
                                "result=%s" % self.ledger.result)
        if not a.enabled:
            return self._finish(R_NOT_ENABLED, "authorization.enabled != True")

        verdict, action = recover(self.ledger)
        self.evidence["recovery"] = {"verdict": verdict, "action": action}
        if verdict == REC_CONSUMED:
            return self._finish(R_ALREADY_CONSUMED, action)
        if verdict in (REC_PENDING_RESTORE, REC_CRITICAL):
            # 🔴 重啟後發現 external 可能仍被暫停：**不重跑流程**，
            #    只回報，由既有 restore responsibility contract / 人工處理。
            return self._finish(R_RESTORE_UNKNOWN if verdict == REC_PENDING_RESTORE
                                else R_EXCEPTION, action)

        # ---- 1. TIME WINDOW ----
        st, wait_sec = window_state(self._now(), a)
        if st == WIN_BEFORE:
            if self.max_wait_sec is not None and wait_sec > float(self.max_wait_sec):
                self.evidence["window_check_result"] = "WAIT_EXCEEDS_LIMIT"
                return self._finish(R_EXPIRED,
                                    "距 window 起點 %.0f s，超過允許等待" % wait_sec)
            self.evidence["waited_sec"] = wait_sec
            self._wait(wait_sec)
            st, _ = window_state(self._now(), a)
        self.evidence["window_check_result"] = st
        if st == WIN_WRONG_DATE:
            return self._finish(R_WRONG_DATE, "now 不在授權日期")
        if st != WIN_INSIDE:
            # 🔴 已過期：一次都不執行
            return self._finish(R_EXPIRED, "啟動時已超過 window_end")

        # ---- 2. STEP 0 fresh source state ----
        self.ledger.mark(LC_PREFLIGHT, "fresh start")
        src = self._source_state() if self._source_state else {}
        self.evidence["source_state"] = src
        if src and not all(v is True for k, v in src.items()
                           if k.endswith("_ok") or k.endswith("_blocked")):
            return self._finish(R_ABORTED_PREFLIGHT, "source state 不符：%r" % src)

        # ---- 2b. host runtime（可選）----
        if self._host is not None:
            hok, hchecks = evaluate_host_runtime(self._host())
            self.evidence["host_runtime"] = {"ok": hok, "checks": hchecks}
            if not hok:
                return self._finish(R_ABORTED_PREFLIGHT,
                                    "host runtime 不符：%s"
                                    % [k for k, v in hchecks.items() if not v])

        # ---- 3. FRESH SAFETY PREFLIGHT ----
        ok1, ck1 = self._preflight()
        self.evidence["fresh_preflight"] = {"ok": ok1, "checks": ck1}
        if not ok1:
            return self._finish(R_ABORTED_PREFLIGHT,
                                [k for k, v in ck1.items() if not v])

        # ---- 4. GATE A ----
        okA, ckA, verdictA = self._gate_a()
        self.evidence["gate_a"] = {"ok": okA, "checks": ckA,
                                   "verdict": verdictA}
        if not okA:
            # 🔴 NO pause / NO arm / NO Phase 6 command；ONE-SHOT 結束
            return self._finish(R_ABORTED_CURRENT,
                                [k for k, v in ckA.items() if not v])

        # ---- 5. HANDOFF READINESS ----
        ok3, ck3 = self._readiness()
        self.evidence["handoff_readiness"] = {"ok": ok3, "checks": ck3}
        if not ok3:
            return self._finish(R_ABORTED_READINESS,
                                [k for k, v in ck3.items() if not v])

        # ---- 6. 既有 Handoff + FIRST LIVE（唯一送指令處）----
        # 🔴 OLD（Attempt #3 之前）：在此就寫 pause INTENT ＋ 責任成立。
        #    NEW：只註冊 hook；INTENT 由 provider 在 pre-pause preflight
        #    PASS、即將呼叫 pause sender 的前一刻才落盤。
        #    WHY：Attempt #3 在 1_PRE_PAUSE_PREFLIGHT 就中止，
        #         pause_attempt_count = 0，卻已留下 responsibility=True。
        if self._register_pause_hook is not None:
            self._register_pause_hook(self._on_pause_intent)
        res = self._handoff()
        self.evidence["handoff_result"] = res
        art = self._collect(res) if self._collect else {}
        self.evidence["handoff_artifact"] = art
        self._record_from_artifact(art)

        return self._finish(*self._classify(art))

    # ------------------------------------------------------------------
    def _on_pause_intent(self, detail=None):
        """pause sender **即將**被呼叫 -> durable INTENT ＋ 責任成立。"""
        self.ledger.mark_intent(
            "pause", detail or "pre-pause preflight PASS -> 即將送出 pause")
        self.ledger.mark(LC_EXTERNAL_PAUSE_RESPONSIBILITY,
                         "pause 意圖已落盤 -> 責任成立")

    def _record_from_artifact(self, art):
        """把既有 artifact 的關鍵事實落進 durable ledger。"""
        a = art or {}
        if a.get("pause_send_outcome") is not None:
            self.ledger.mark_outcome("pause", a.get("pause_send_outcome"))
        if a.get("phase6_authority_acquired"):
            self.ledger.mark(LC_PHASE6_AUTHORITY)
        if a.get("dispatcher_attempted"):
            self.ledger.mark_intent("charge")
            self.ledger.mark(LC_LIVE_LEG)
            self.ledger.mark_outcome("charge", a.get("dispatcher_outcome"))
        if a.get("phase6_authority_released"):
            self.ledger.mark(LC_PHASE6_RELEASED)
        if a.get("restore_attempt_count"):
            self.ledger.mark_intent("restore")
            self.ledger.mark(LC_RESTORE_PENDING)
            self.ledger.mark_outcome("restore", a.get("restore_send_outcome"))
        if a.get("restore_resumed"):
            self.ledger.mark_outcome("restore_verified", "RV_RESUMED")
            self.ledger.mark(LC_RESTORED)

    def _classify(self, art):
        """
        依既有 artifact 決定 result state。**不重新判定任何 policy。**

        🔴 Attempt #3 postmortem（RESULT CLASSIFICATION DEFECT）
          OLD : `dispatcher_outcome == DISPATCH_NOT_ATTEMPTED` 且
                `phase6_authority_acquired is False` -> AUTHORITY_ACQUIRE_FAILED。
                Attempt #3 在 1_PRE_PAUSE_PREFLIGHT 就中止、
                `Phase6AuthorityLease.acquire()` **一次都沒被呼叫**，
                卻被記成 AUTHORITY_ACQUIRE_FAILED。
          NEW : 依 artifact 的 `handoff_abort_step` 決定實際 stage；
                只有 `authority_acquire_attempted is True` 且未取得，
                才允許 AUTHORITY_ACQUIRE_FAILED。
          WHY : 終態必須反映真實 field stage，否則 postmortem 會被誤導。
        """
        a = art or {}
        po = a.get("pause_send_outcome")
        # ---------------- P0  SAFETY UNCERTAIN ----------------
        # 🔴 這一層一律**高於 deadline**：責任可能仍成立、或不得重送。
        if po == "UNKNOWN":
            return R_PAUSE_UNKNOWN, "pause 結果不確定 -> 不得重送"
        ro0 = a.get("restore_send_outcome")
        if ro0 is not None:
            # restore 曾被嘗試才有結果可言
            if ro0 == "NOT_SENT":
                return R_RESTORE_FAILED, "restore 明確未送出"
            if ro0 == "UNKNOWN" or a.get("restore_resumed") is not True:
                return (R_RESTORE_UNKNOWN,
                        "restore 未確認恢復（%s）-> 責任不得解除"
                        % a.get("restore_verifier_state"))
        do0 = a.get("dispatcher_outcome")
        if do0 == FL.DISPATCH_UNKNOWN:
            # 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-06 Attempt #9 postmortem）
            #   OLD : dispatcher_outcome == DISPATCH_UNKNOWN 一律記
            #         `DISPATCH_UNKNOWN`。
            #   NEW : 若 **dispatch 本身確定成功**（真的送出 ＋ readback
            #         VERIFY_SUCCESS），而中止原因是 dispatch 之後
            #         authority 轉為不可信，則記
            #         `AUTHORITY_UNKNOWN_AFTER_DISPATCH`。
            #   WHY : `DISPATCH_UNKNOWN` 的語意是「不知道有沒有送出去」，
            #         用在已知送出成功的情況會誤導 postmortem。
            #   EVIDENCE : Attempt #9 —— dispatch_result DISPATCH_SUCCEEDED、
            #         readback VERIFY_SUCCESS、observed_state CHARGING、
            #         abort reason ABORTED_UNCERTAIN_OWNERSHIP。
            #   🔴 這是**精確化**，不是放寬：兩者都不是 PASS，
            #      且 live_leg_result 仍為 UNKNOWN、仍不計數。
            if (a.get("dispatch_result") == "DISPATCH_SUCCEEDED"
                    and a.get("readback_result") == "VERIFY_SUCCESS"):
                return (R_AUTHORITY_UNKNOWN_AFTER_DISPATCH,
                        a.get("dispatcher_reason"))
            return R_DISPATCH_UNKNOWN, a.get("dispatcher_reason")
        # ---------------- P1  CONTROL FAILURE ----------------
        if do0 == FL.DISPATCH_FAILED:
            return R_DISPATCH_FAILED, a.get("dispatcher_reason")
        # 🔴 PRECEDENCE 1：real terminal step。
        #    OLD（Attempt #4 之前）：只有 `po is None` 時才看 step，
        #    於是 pause 已 SENT 的 7_FIRST_LIVE 中止被歸成
        #    ABORTED_POST_HANDOFF_REVALIDATION（GATE B 其實 PASS）。
        #    NEW：只要有 `handoff_abort_step` 就以它為準。
        # 🔴 EXECUTION DEADLINE PRECEDENCE（2026-10-02 裁示）
        #    置於 pause NOT_SENT / UNKNOWN 之**後** ——
        #    pause 結果不確定時仍必須是 R_PAUSE_UNKNOWN（NO RESEND 契約優先）。
        #    置於 generic step abort 之**前** —— 否則 deadline 造成的中止
        #    會被記成 ABORTED_HANDOFF_STEP，看不出真正原因。
        #    🔴 `DEADLINE_DURING_LIVE_CLEANUP` 只表示「跨過 deadline」，
        #       **不表示 CHARGE 未發生**；CHARGE 是否發生一律以 artifact 的
        #       `phase6_command_counts` / `leg_reached_complete` 為準。
        dl_at = a.get("deadline_blocked_at")
        if a.get("deadline_crossed_during_live_leg") is True:
            return (R_DEADLINE_DURING_LIVE_CLEANUP,
                    "CHARGE 已送出後跨過 deadline -> 依既有 cleanup 安全收尾")
        if dl_at is not None:
            # 🔴 以「pause 是否**真的送出**」區分兩種 deadline 終態。
            #    po 為 None 或 NOT_SENT -> 完全沒有 field 動作成立。
            if po != "SENT":
                return (R_DEADLINE_BEFORE_FIELD,
                        "deadline 於 %s 阻擋：尚未產生任何 field 動作" % dl_at)
            return (R_DEADLINE_DURING_HANDOFF,
                    "deadline 於 %s 阻擋：不得開始新風險動作，已安全歸還"
                    % dl_at)
        # ---------------- P3  pause 明確未送出（非 deadline 造成）----------
        if po == "NOT_SENT":
            return R_PAUSE_FAILED, "pause 明確未送出"
        step = a.get("handoff_abort_step")
        if step is not None:
            if step in STEP_RESULT_MAP:
                return STEP_RESULT_MAP[step], a.get("handoff_abort_detail")
            return R_ABORTED_HANDOFF_STEP, "%s：%s" % (
                step, a.get("handoff_abort_detail"))
        if a.get("post_handoff_revalidation_ok") is False:
            return R_ABORTED_POST_HANDOFF, a.get("post_handoff_verdict")
        if (a.get("authority_acquire_attempted") is True
                and not a.get("phase6_authority_acquired")):
            return R_AUTHORITY_FAILED, a.get("dispatcher_reason")
        do = a.get("dispatcher_outcome")
        if do == FL.DISPATCH_UNKNOWN:
            return R_DISPATCH_UNKNOWN, a.get("dispatcher_reason")
        if do == FL.DISPATCH_FAILED:
            return R_DISPATCH_FAILED, a.get("dispatcher_reason")
        if do == FL.DISPATCH_NOT_ATTEMPTED:
            return R_ABORTED_POST_HANDOFF, a.get("dispatcher_reason")
        # dispatcher SUCCEEDED 之後才看 restore / post-check
        ro = a.get("restore_send_outcome")
        if ro == "NOT_SENT":
            return R_RESTORE_FAILED, "restore 明確未送出"
        if ro == "UNKNOWN" or a.get("restore_resumed") is not True:
            return R_RESTORE_UNKNOWN, a.get("restore_verifier_state")
        post = a.get("post_check") or {}
        if post and not all(post.values()):
            return R_POSTCHECK_FAILED, [k for k, v in post.items() if not v]
        if FL.validate_handoff_artifact(a):
            return R_POSTCHECK_FAILED, FL.validate_handoff_artifact(a)
        return R_PASS, None


# ======================================================================
def main(argv=None):
    """離線自述。**不做任何 I/O，不接觸設備。**"""
    print("=" * 74)
    print("  Phase 6.10 — Scheduled FIRST LIVE Attempt（ONE-SHOT）")
    print("=" * 74)
    print("  mode                 : %s" % SCHEDULED_MODE)
    print("  is_unattended_live   : %r" % IS_UNATTENDED_LIVE)
    print("  is_recurring         : %r" % IS_RECURRING)
    print("  auto_retry_allowed   : %r" % AUTO_RETRY_ALLOWED)
    print()
    print("  window states        : %s" % [WIN_BEFORE, WIN_INSIDE,
                                           WIN_EXPIRED, WIN_WRONG_DATE])
    print("  lifecycle states     : %d" % len(LIFECYCLE_STATES))
    print("  pending-restore 狀態  : %d" % len(PENDING_RESTORE_LIFECYCLE))
    print("  result states        : %d" % len(RESULT_STATES))
    print("  host runtime items   : %s" % (list(HOST_ITEMS),))
    print()
    print("  [!] 一個 scheduled task = 最多一次 field attempt。")
    print("  [!] 任何結果都 ONE-SHOT CONSUMED；不自動重試、不隔天再跑。")
    print("  [!] crash 後一律 NO RESEND pause / charge / restore。")
    print("  [!] 本模組不含 decision / Safety Gate / executor / live-leg /")
    print("      handoff 的任何實作，全部沿用既有模組。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
