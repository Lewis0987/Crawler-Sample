# -*- coding: utf-8 -*-
"""
phase6_execution_deadline.py — EXECUTION DEADLINE / SAFE CLEANUP CONTRACT
======================================================================
2026-10-02 Attempt #8 後續正式裁示。

核心語意
    `window_end` = **NO-NEW-RISK-ACTION DEADLINE**
    `window_end` ≠ PROCESS TERMINATION DEADLINE

到達 deadline 之後：

    禁止再**開始**新的風險動作
        PAUSE ／authority acquire ／CHARGE ／DISCHARGE ／第二條 live leg

    但**已經產生的安全責任必須允許完成**
        command resolution ／cleanup STOP ／authority release ／
        external restore ／RestoreVerifier

🔴 cleanup / restore path **不得**被 deadline 自己阻擋 ——
   deadline 的作用是「不要再開始新風險」，不是「立刻停手」。

🔴 為什麼不直接沿用 `phase6_handoff_orchestrator.Watchdog`
    Watchdog 量的是「external 已被 pause 多久」（`max_paused_sec`，
    由 `mark_paused()` 起算），語意是**暫停時長上限**；
    它沒有「絕對 wall-clock 截止時刻」的概念，
    也沒有 NO-NEW-RISK-ACTION 與 SAFE-CLEANUP 的二分。
    其 `verdict()` 回傳的是 REQUIRE_RESTORE / REQUIRE_PHASE6_RELEASE_FIRST，
    屬於「逾時後該做什麼」，不是「這個動作現在准不准開始」。
    兩者語意不同，因此**另建**本模組，不為了「已有 class」而硬接。
    Watchdog 維持原狀、未被本模組取代，也仍未接入 production flow。

🔴 時鐘策略
    建立時用 **wall clock** 把 `target_date + window_end + timezone`
    換算成剩餘秒數；進入執行流程後改以 **monotonic clock** 追蹤，
    避免系統時間被調整造成 deadline 倒退或跳動。

🔴 本模組**不 import 任何專案模組**，避免循環 import；
   所有使用端以具名參數注入。
"""
import datetime

# ======================================================================
# 動作分類
# ======================================================================
# 🔴 NEW RISK ACTION：deadline 後**不得開始**
ACT_PAUSE = "pause"
ACT_AUTHORITY_ACQUIRE = "authority_acquire"
ACT_CHARGE = "charge"
ACT_DISCHARGE = "discharge"
ACT_LIVE_LEG = "live_leg"

NEW_RISK_ACTIONS = (ACT_PAUSE, ACT_AUTHORITY_ACQUIRE, ACT_CHARGE,
                    ACT_DISCHARGE, ACT_LIVE_LEG)

# 🔴 SAFE CLEANUP ACTION：**任何時候**都必須允許完成
ACT_COMMAND_RESOLUTION = "command_resolution"
ACT_CLEANUP_STOP = "cleanup_stop"
ACT_AUTHORITY_RELEASE = "authority_release"
ACT_RESTORE = "restore"
ACT_RESTORE_VERIFY = "restore_verify"

SAFE_CLEANUP_ACTIONS = (ACT_COMMAND_RESOLUTION, ACT_CLEANUP_STOP,
                        ACT_AUTHORITY_RELEASE, ACT_RESTORE,
                        ACT_RESTORE_VERIFY)

# ======================================================================
# 正式 checkpoint（必須是真正的 production 檢查點）
# ======================================================================
CK_BEFORE_PAUSE = "1_BEFORE_PAUSE"
CK_AFTER_PAUSE_RESPONSIBILITY = "2_AFTER_PAUSE_RESPONSIBILITY"
CK_BEFORE_GATE_B = "3_BEFORE_GATE_B"
CK_BEFORE_AUTHORITY_ACQUIRE = "4_BEFORE_AUTHORITY_ACQUIRE"
CK_AFTER_AUTHORITY_ACQUIRE = "5_AFTER_AUTHORITY_ACQUIRE"
CK_BEFORE_FULL_PERMIT = "6_BEFORE_FULL_PERMIT"
CK_BEFORE_CHARGE_SEND = "7_BEFORE_CHARGE_SEND"
CK_LIVE_LEG_CLEANUP = "8_LIVE_LEG_CLEANUP"
CK_BEFORE_RESTORE_COMPLETION = "9_BEFORE_RESTORE_COMPLETION"

CHECKPOINTS = (CK_BEFORE_PAUSE, CK_AFTER_PAUSE_RESPONSIBILITY,
               CK_BEFORE_GATE_B, CK_BEFORE_AUTHORITY_ACQUIRE,
               CK_AFTER_AUTHORITY_ACQUIRE, CK_BEFORE_FULL_PERMIT,
               CK_BEFORE_CHARGE_SEND, CK_LIVE_LEG_CLEANUP,
               CK_BEFORE_RESTORE_COMPLETION)

# 每個 checkpoint 即將開始的動作；SAFE CLEANUP 的兩個永遠放行
CHECKPOINT_ACTION = {
    CK_BEFORE_PAUSE: ACT_PAUSE,
    CK_AFTER_PAUSE_RESPONSIBILITY: ACT_LIVE_LEG,
    CK_BEFORE_GATE_B: ACT_LIVE_LEG,
    CK_BEFORE_AUTHORITY_ACQUIRE: ACT_AUTHORITY_ACQUIRE,
    CK_AFTER_AUTHORITY_ACQUIRE: ACT_LIVE_LEG,
    CK_BEFORE_FULL_PERMIT: ACT_LIVE_LEG,
    CK_BEFORE_CHARGE_SEND: ACT_CHARGE,
    CK_LIVE_LEG_CLEANUP: ACT_CLEANUP_STOP,
    CK_BEFORE_RESTORE_COMPLETION: ACT_RESTORE,
}

# ======================================================================
# 裁決詞彙
# ======================================================================
DL_OK = "DEADLINE_OK"                 # 尚未到期
DL_BLOCKED = "DEADLINE_BLOCKED"       # 已到期 -> 不得開始新風險動作
DL_CLEANUP_ALLOWED = "DEADLINE_CLEANUP_ALLOWED"   # 已到期但屬安全收尾
DL_UNKNOWN = "DEADLINE_UNKNOWN"       # 無法判定 -> Fail Closed


class ExecutionDeadline(object):
    """
    單一 canonical 的執行截止判定。

    🔴 **不得**讓各 module 自行重新解析 `target_date` / `window_end`；
       一律由本物件回答。
    🔴 建立之後 deadline 即固定；`now_fn`（wall）只在建立時使用一次，
       之後全部以 `monotonic_fn` 推進 -> 系統時間調整不會讓 deadline 倒退。
    🔴 任何判定例外一律 Fail Closed（視為已到期，阻擋新風險動作），
       但**仍然允許** SAFE CLEANUP。
    """

    def __init__(self, deadline_at, now_fn=None, monotonic_fn=None,
                 label=None):
        if deadline_at is None:
            raise ValueError("deadline_at 必填（不得預設為無限）")
        import time as _time
        self._now = now_fn or (lambda: datetime.datetime.now(
            deadline_at.tzinfo) if deadline_at.tzinfo
            else datetime.datetime.now())
        self._mono = monotonic_fn or _time.monotonic
        self.deadline_at = deadline_at
        self.label = label
        self.built_at_wall = self._now()
        self.built_at_mono = self._mono()
        # 🔴 budget 只用 wall clock 算一次
        self.budget_sec = (deadline_at - self.built_at_wall).total_seconds()
        self.events = []

    # ------------------------------------------------------------------
    @classmethod
    def from_window(cls, target_date, window_end, tzinfo=None, now_fn=None,
                    monotonic_fn=None, label=None):
        """
        由 canonical 授權欄位建立。

        🔴 `target_date` / `window_end` 必須是已解析的
           `datetime.date` / `datetime.time`（與 `OneShotAuthorization`
           同型別），本模組**不**自行解析字串，避免成為第二套解析實作。
        """
        if not isinstance(target_date, datetime.date):
            raise ValueError("target_date 必須是 datetime.date")
        if not isinstance(window_end, datetime.time):
            raise ValueError("window_end 必須是 datetime.time")
        dt = datetime.datetime.combine(target_date, window_end)
        if tzinfo is not None:
            dt = dt.replace(tzinfo=tzinfo)
        return cls(dt, now_fn=now_fn, monotonic_fn=monotonic_fn, label=label)

    # ------------------------------------------------------------------
    def elapsed_sec(self):
        return self._mono() - self.built_at_mono

    def remaining_sec(self):
        """剩餘秒數（可為負）。以 monotonic 推進。"""
        return self.budget_sec - self.elapsed_sec()

    def expired(self):
        """
        是否已到期。

        🔴 判定失敗（clock 例外等）一律視為**已到期**（Fail Closed）——
           寧可不開始新風險動作。
        """
        try:
            return self.remaining_sec() <= 0.0
        except Exception:                                      # noqa: BLE001
            return True

    # ------------------------------------------------------------------
    def allows(self, action):
        """
        這個動作現在准不准**開始**。回傳 (allowed, verdict, detail)。

        🔴 SAFE CLEANUP 一律放行 —— 包含已到期、包含判定例外。
        🔴 未知動作一律視為新風險動作（Fail Closed）。
        """
        if action in SAFE_CLEANUP_ACTIONS:
            if self.expired():
                return True, DL_CLEANUP_ALLOWED, "安全收尾不受 deadline 阻擋"
            return True, DL_OK, None
        try:
            rem = self.remaining_sec()
        except Exception as e:                                 # noqa: BLE001
            return (False, DL_UNKNOWN,
                    "deadline 判定例外：%s -> Fail Closed" % type(e).__name__)
        if action not in NEW_RISK_ACTIONS:
            # 未登錄的動作：當成新風險動作處理
            if rem <= 0.0:
                return (False, DL_BLOCKED,
                        "未登錄動作 %r 視為新風險動作，deadline 已過 %.1f s"
                        % (action, -rem))
            return True, DL_OK, None
        if rem <= 0.0:
            return (False, DL_BLOCKED,
                    "deadline 已過 %.1f s -> 不得開始 %s" % (-rem, action))
        return True, DL_OK, None

    # ------------------------------------------------------------------
    def check(self, checkpoint):
        """
        在正式 checkpoint 做判定並留痕。回傳 (allowed, verdict, detail)。

        🔴 未知 checkpoint -> 當成新風險動作（Fail Closed）。
        """
        action = CHECKPOINT_ACTION.get(checkpoint, "unknown_action")
        allowed, verdict, detail = self.allows(action)
        try:
            rem = self.remaining_sec()
        except Exception:                                      # noqa: BLE001
            rem = None
        self.events.append({"checkpoint": checkpoint, "action": action,
                            "allowed": bool(allowed), "verdict": verdict,
                            "detail": detail, "remaining_sec": rem})
        return allowed, verdict, detail

    # ------------------------------------------------------------------
    def as_dict(self):
        try:
            rem = self.remaining_sec()
        except Exception:                                      # noqa: BLE001
            rem = None
        return {"label": self.label,
                "deadline_at": (self.deadline_at.isoformat()
                                if self.deadline_at else None),
                "built_at": (self.built_at_wall.isoformat()
                             if self.built_at_wall else None),
                "budget_sec": self.budget_sec,
                "remaining_sec": rem,
                "expired": self.expired(),
                "checkpoints": list(CHECKPOINTS),
                "new_risk_actions": list(NEW_RISK_ACTIONS),
                "safe_cleanup_actions": list(SAFE_CLEANUP_ACTIONS),
                "events": list(self.events)}

    # ------------------------------------------------------------------
    def first_block(self):
        """第一個被阻擋的 checkpoint 事件（沒有就回 None）。"""
        for e in self.events:
            if not e["allowed"]:
                return e
        return None


def deadline_allows(deadline, action):
    """
    模組層 helper：`deadline is None` 時維持舊行為（不阻擋）。

    🔴 這是**向後相容**出口，不是 bypass ——
       production provider 一律注入 deadline；
       未注入時行為與導入本契約之前完全相同。
    """
    if deadline is None:
        return True, DL_OK, None
    fn = getattr(deadline, "allows", None)
    if not callable(fn):
        return True, DL_OK, None
    try:
        return fn(action)
    except Exception as e:                                     # noqa: BLE001
        if action in SAFE_CLEANUP_ACTIONS:
            return True, DL_CLEANUP_ALLOWED, "例外但屬安全收尾"
        return (False, DL_UNKNOWN,
                "deadline helper 例外：%s" % type(e).__name__)


def deadline_check(deadline, checkpoint):
    """同 `deadline_allows`，但走 checkpoint 並留痕。"""
    if deadline is None:
        return True, DL_OK, None
    fn = getattr(deadline, "check", None)
    if not callable(fn):
        return True, DL_OK, None
    try:
        return fn(checkpoint)
    except Exception as e:                                     # noqa: BLE001
        action = CHECKPOINT_ACTION.get(checkpoint, "unknown_action")
        if action in SAFE_CLEANUP_ACTIONS:
            return True, DL_CLEANUP_ALLOWED, "例外但屬安全收尾"
        return (False, DL_UNKNOWN,
                "deadline helper 例外：%s" % type(e).__name__)


if __name__ == "__main__":
    print("=" * 68)
    print("  EXECUTION DEADLINE / SAFE CLEANUP CONTRACT")
    print("=" * 68)
    print("  checkpoints          : %d" % len(CHECKPOINTS))
    print("  new risk actions     : %r" % (NEW_RISK_ACTIONS,))
    print("  safe cleanup actions : %r" % (SAFE_CLEANUP_ACTIONS,))
    print("  window_end 語意       : NO-NEW-RISK-ACTION DEADLINE")
    print("  window_end 不是       : PROCESS TERMINATION DEADLINE")
    print("=" * 68)
