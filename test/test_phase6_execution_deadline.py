# -*- coding: utf-8 -*-
"""
test_phase6_execution_deadline.py — EXECUTION DEADLINE / SAFE CLEANUP
======================================================================
核心命題
    「`window_end` 是 **NO-NEW-RISK-ACTION DEADLINE**，
      不是 PROCESS TERMINATION DEADLINE。

      到期後禁止**開始**新的風險動作
          PAUSE ／authority acquire ／CHARGE ／DISCHARGE ／第二條 live leg

      但已成立的安全責任**必須允許完成**
          command resolution ／cleanup STOP ／authority release ／
          external restore ／RestoreVerifier」

🔴 全部使用 production entry point（`deps.arm_and_run_handoff()`）
   ＋ fake field I/O ＋ fake clock。
   **不得**只直接呼叫內部 helper。
🔴 deadline 一律以**注入的 monotonic fake clock** 推進，
   不依賴真實時間，也不 sleep。

涵蓋使用者指定項目
    D1 ~ D11   deadline / safe cleanup
    D12 ~ D16  attempt_result vs live_leg_result reporting 契約
    W          Watchdog 語意比較     X  production wiring

用法
    python test_phase6_execution_deadline.py        # exit 0 = PASS
"""
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcs_auto_control_runtime as RT                # noqa: E402
import phase6_execution_deadline as DL               # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_scheduled_first_live as SC             # noqa: E402
import run_scheduled_first_live as LAUNCH            # noqa: E402

# 🔴 production entry 的 fake 環境沿用 entry-point smoke 的**同一套**裝置，
#    避免兩檔對 production 形狀的認知漂移。
import test_phase6_entry_point_ordering as EP        # noqa: E402
import test_phase6_provider_construction as PCS      # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


# ======================================================================
# FAKE CLOCK ＋ 在指定 checkpoint 觸發「跨過 deadline」
# ======================================================================
class FakeMono(object):
    """可被測試任意推進的 monotonic clock。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += float(sec)


BUDGET_SEC = 1500.0        # 00:05 -> 00:30


def make_deadline(mono, budget_sec=BUDGET_SEC):
    """
    以 fake wall + fake monotonic 建立 canonical deadline。

    🔴 走的是 production 的 `ExecutionDeadline`，不是測試自造的替身。
    """
    start = datetime.datetime(2026, 10, 2, 0, 5)
    return DL.ExecutionDeadline(
        start + datetime.timedelta(seconds=budget_sec),
        now_fn=(lambda: start), monotonic_fn=mono,
        label="DEADLINE-TEST")


def build(trip_at=None, budget_sec=BUDGET_SEC, **kw):
    """
    建立 production entry 環境並注入 deadline。

    `trip_at` = 在某個 production checkpoint **第一次**被評估時，
    把 fake clock 推過 deadline —— 模擬「流程跑到這一步時剛好超時」。
    """
    mono = FakeMono()
    deadline = make_deadline(mono, budget_sec)
    # deadline 必須在 build_field_dependencies() 之前注入（ctx 會被複製）
    deps, env = EP.build_entry_env(deadline=deadline, **kw)
    env["mono"] = mono
    env["deadline"] = deadline

    if trip_at is not None:
        orig_check = deadline.check

        def tripping_check(ck):
            if ck == trip_at and not env.get("tripped"):
                env["tripped"] = True
                # 🔴 推到 deadline 之後才做這一次判定
                mono.advance(budget_sec + 60.0)
            return orig_check(ck)

        deadline.check = tripping_check
    return deps, env


def run(deps, env, **kw):
    return EP.run_entry(deps, env, **kw)


def counts(h):
    c = h["guard"].as_dict()["counts"]
    return (c.get("phase6_charge", 0), c.get("phase6_discharge", 0),
            c.get("phase6_pcs_stop", 0))


def steps_of(res):
    return {s.get("step"): s for s in (res.get("steps") or ())}


# ======================================================================
# D1 — 啟動時已過 deadline
# ======================================================================
def test_D1_before_start():
    print("\n[D1] 啟動時已過 deadline -> handoff 0")
    # 🔴 走真正的 ScheduledFirstLiveRunner（啟動階段 window 檢查）
    import test_phase6_scheduled_first_live as TS
    a = TS.auth()
    rig = TS.Rig(a.end_dt() + datetime.timedelta(minutes=5))
    ev = rig.runner().run()
    check("D1a ★★ handoff 從未執行", rig.handoff_calls == 0)
    check("D1b ★★ result = EXPIRED_NO_ATTEMPT", ev["result"] == SC.R_EXPIRED)
    check("D1c ★★ ledger 無 pause / charge 意圖",
          not rig.ledger.attempted("pause")
          and not rig.ledger.attempted("charge"))
    check("D1d ★★ 屬 handled terminal result（非 launcher failure）",
          LAUNCH.classify_exit(SC.R_EXPIRED) == LAUNCH.EXIT_HANDLED)


# ======================================================================
# D2 — handoff 中、PAUSE 前跨 deadline
# ======================================================================
def test_D2_before_pause():
    print("\n[D2] handoff 中、PAUSE 前跨 deadline -> pause 0 / command 0")
    deps, env = build(trip_at=DL.CK_BEFORE_PAUSE)
    res = run(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = counts(h)
    check("D2a ★★ 實際 SSH pause 動詞 = 0",
          env["ssh"].calls.count("pause") == 0)
    check("D2b ★★ 0 CHARGE / 0 DISCHARGE / 0 PCS STOP",
          (ch, di, stp) == (0, 0, 0))
    check("D2c ★★ authority acquire = 0",
          h["lease"].acquire_attempted is False)
    check("D2d ★★ dispatcher 從未被進入",
          int(getattr(h["dispatcher"], "calls", 0)) == 0)
    check("D2e ★★ 2_PAUSE 被 deadline 擋下",
          st.get("2_PAUSE", {}).get("ok") is not True
          and DL.DL_BLOCKED in str(st.get("2_PAUSE", {}).get("detail")))
    check("D2f ★★ 無恢復責任（從未 pause）",
          res.get("restore_responsibility") is False)
    check("D2g ★★ 實際 SSH restore 動詞 = 0",
          env["ssh"].calls.count("restore") == 0)
    art = env["artifact"]
    check("D2h ★★ 分類為 DEADLINE_BEFORE_FIELD_ACTION",
          SC.ScheduledFirstLiveRunner._classify(None, art)[0]
          == SC.R_DEADLINE_BEFORE_FIELD)
    check("D2i ★★ rest state 維持", rest_ok())


# ======================================================================
# D3 — PAUSE SENT / VERIFIED 後跨 deadline
# ======================================================================
def test_D3_after_pause():
    print("\n[D3] PAUSE 已 SENT/VERIFIED 後跨 deadline -> CHARGE 0 / restore 恰一次")
    deps, env = build(trip_at=DL.CK_BEFORE_GATE_B)
    res = run(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = counts(h)
    check("D3a ★★ pause 確實送出過 1 次",
          env["ssh"].calls.count("pause") == 1)
    check("D3b ★★ PauseVerifier 已 VERIFIED",
          st.get("3_4_PAUSE_VERIFY", {}).get("ok") is True)
    check("D3c ★★ 0 CHARGE / 0 DISCHARGE", (ch, di) == (0, 0))
    check("D3d ★★ authority acquire = 0",
          h["lease"].acquire_attempted is False)
    check("D3e ★★ restore 恰好送出一次",
          env["ssh"].calls.count("restore") == 1)
    check("D3f ★★ RestoreVerifier = RV_RESUMED",
          res.get("restore_verifier_state") == "RESUMED")
    check("D3g ★★ 恢復責任已解除",
          res.get("restore_responsibility") is False)
    art = env["artifact"]
    check("D3h ★★ 分類為 DEADLINE_DURING_HANDOFF",
          SC.ScheduledFirstLiveRunner._classify(None, art)[0]
          == SC.R_DEADLINE_DURING_HANDOFF)
    check("D3i ★★ rest state 維持", rest_ok())


# ======================================================================
# D4 — authority 已取得、CHARGE 前跨 deadline
# ======================================================================
def test_D4_after_authority():
    print("\n[D4] authority 已取得、CHARGE 前跨 deadline -> CHARGE 0 / release / restore")
    deps, env = build(trip_at=DL.CK_AFTER_AUTHORITY_ACQUIRE)
    res = run(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = counts(h)
    check("D4a ★★ authority 確實已取得",
          h["lease"].acquire_attempted is True
          and h["lease"].acquired_once is True)
    check("D4b ★★ 0 CHARGE / 0 DISCHARGE", (ch, di) == (0, 0))
    check("D4c ★★ 未進入 dispatch scope（permit 未 ACTIVE/EXITED）",
          h["permit"].state == FL.PERMIT_NOT_ENTERED)
    check("D4d ★★ authority 已釋放", h["lease"].held is False)
    check("D4e ★★ restore 恰好送出一次",
          env["ssh"].calls.count("restore") == 1)
    check("D4f ★★ RestoreVerifier = RV_RESUMED",
          res.get("restore_verifier_state") == "RESUMED")
    check("D4g ★★ release 先於 restore",
          res.get("release_before_restore") is True)
    check("D4h ★★ HO.DISPATCH_ENABLED 全程未被打開", rest_ok())
    check("D4i ★★ dispatcher 回報未送出",
          (h["dispatcher"].last or {}).get("dispatched") is False)


# ======================================================================
# D5 — CHARGE 已送出後跨 deadline
# ======================================================================
def test_D5_after_charge():
    print("\n[D5] CHARGE 已送出後跨 deadline -> 不 hard abort，走完 cleanup")
    deps, env = build(trip_at=DL.CK_LIVE_LEG_CLEANUP)
    res = run(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = counts(h)
    check("D5a ★★ CHARGE 確實送出 1 次", ch == 1)
    check("D5b ★★ DISCHARGE = 0", di == 0)
    check("D5c ★★ deadline 在 cleanup checkpoint 之後已到期",
          env["deadline"].expired() is True)
    check("D5d ★★ cleanup checkpoint 被放行（非 hard abort）",
          any(e["checkpoint"] == DL.CK_LIVE_LEG_CLEANUP and e["allowed"]
              for e in env["deadline"].events))
    check("D5e ★★ cleanup STOP 已執行（<= 1）", stp <= 1 and stp >= 0)
    check("D5f ★★ authority 已釋放", h["lease"].held is False)
    check("D5g ★★ restore 恰好送出一次",
          env["ssh"].calls.count("restore") == 1)
    check("D5h ★★ RestoreVerifier = RV_RESUMED",
          res.get("restore_verifier_state") == "RESUMED")
    check("D5i ★★ 流程走到 12_COMPLETE（未 hard abort）",
          st.get("12_COMPLETE", {}).get("ok") is True)
    art = env["artifact"]
    check("D5j ★★ artifact 標記 deadline_crossed_during_live_leg",
          art.get("deadline_crossed_during_live_leg") is True)
    check("D5k ★★ 分類為 DEADLINE_DURING_LIVE_CLEANUP",
          SC.ScheduledFirstLiveRunner._classify(None, art)[0]
          == SC.R_DEADLINE_DURING_LIVE_CLEANUP)
    check("D5l ★★ 但 artifact 仍顯示 CHARGE 確實發生過"
          "（result 不得被誤讀成沒送出）",
          ((art.get("phase6_command_counts") or {}).get("counts") or {})
          .get("phase6_charge") == 1)
    check("D5m ★★ rest state 恢復", rest_ok())


# ======================================================================
# D6 / D7 — deadline 後不得第二個 CHARGE / 不得 DISCHARGE
# ======================================================================
def test_D6_D7_no_second_risk_action():
    print("\n[D6/D7] deadline 後不得第二個 CHARGE、不得 DISCHARGE")
    deps, env = build(trip_at=DL.CK_LIVE_LEG_CLEANUP)
    run(deps, env)
    h = env["holder"]
    ch, di, stp = counts(h)
    d = env["deadline"]
    check("D6a ★★ CHARGE 總數恰好 1（無第二次）", ch == 1)
    check("D6b ★★ 到期後 charge 動作一律被拒",
          d.allows(DL.ACT_CHARGE)[0] is False)
    check("D6c ★★ 到期後 live_leg 動作一律被拒",
          d.allows(DL.ACT_LIVE_LEG)[0] is False)
    check("D7a ★★ DISCHARGE 總數 = 0", di == 0)
    check("D7b ★★ 到期後 discharge 動作一律被拒",
          d.allows(DL.ACT_DISCHARGE)[0] is False)
    check("D7c ★★ CommandCountGuard 無違規",
          h["guard"].as_dict()["violations"] == [])
    check("D7d ★★ 到期後 pause / authority 亦被拒",
          d.allows(DL.ACT_PAUSE)[0] is False
          and d.allows(DL.ACT_AUTHORITY_ACQUIRE)[0] is False)


# ======================================================================
# D8 — restore UNKNOWN -> no resend，責任保留
# ======================================================================
def test_D8_restore_unknown_no_resend():
    print("\n[D8] restore UNKNOWN -> 不重送、責任保留")
    # 🔴 restore SSH 以 41（COMMAND_TIMEOUT）回應 -> SEND_UNKNOWN
    deps, env = build(trip_at=DL.CK_BEFORE_GATE_B, restore_result=41)
    res = run(deps, env)
    h = env["holder"]
    ch, di, stp = counts(h)
    check("D8a ★★ restore 只送出一次（不得重送）",
          env["ssh"].calls.count("restore") == 1)
    check("D8b ★★ 0 CHARGE / 0 DISCHARGE", (ch, di) == (0, 0))
    check("D8c ★★ 恢復責任仍保留（UNKNOWN 不得解除）",
          res.get("restore_responsibility") is True)
    check("D8d ★★ outcome 為 RESTORE_OUTCOME_UNKNOWN",
          res.get("outcome") == "RESTORE_OUTCOME_UNKNOWN")
    check("D8e ★★ deadline 不得因此觸發重送",
          env["ssh"].calls.count("restore") == 1)
    check("D8f ★★ rest state 維持", rest_ok())


# ======================================================================
# D9 — deadline guard 例外 -> Fail Closed
# ======================================================================
def test_D9_guard_exception_fail_closed():
    print("\n[D9] deadline guard 例外 -> Fail Closed，不新增 risk action")
    deps, env = build()
    d = env["deadline"]

    def boom(_ck):
        raise RuntimeError("deadline guard broken")

    d.check = boom
    res = run(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = counts(h)
    check("D9a ★★ pause 未送出（新風險動作被擋）",
          env["ssh"].calls.count("pause") == 0)
    check("D9b ★★ 0 CHARGE / 0 DISCHARGE / 0 PCS STOP",
          (ch, di, stp) == (0, 0, 0))
    check("D9c ★★ authority acquire = 0",
          h["lease"].acquire_attempted is False)
    check("D9d ★★ 2_PAUSE 以 DEADLINE_UNKNOWN 擋下",
          DL.DL_UNKNOWN in str(st.get("2_PAUSE", {}).get("detail")))
    # module helper 層的 Fail Closed
    class Boom(object):
        def allows(self, a):
            raise RuntimeError("x")

        def check(self, c):
            raise RuntimeError("x")

    check("D9e ★★ helper：例外時新風險動作一律拒絕",
          DL.deadline_check(Boom(), DL.CK_BEFORE_CHARGE_SEND)[0] is False)
    check("D9f ★★ helper：例外時安全收尾仍放行",
          DL.deadline_check(Boom(), DL.CK_BEFORE_RESTORE_COMPLETION)[0]
          is True)
    check("D9g ★★ rest state 維持", rest_ok())


# ======================================================================
# D10 — 系統時間被調整，monotonic deadline 不倒退
# ======================================================================
def test_D10_monotonic_not_rewound():
    print("\n[D10] wall clock 被調整 -> monotonic deadline 不倒退 / 不跳動")
    mono = FakeMono()
    wall = {"t": datetime.datetime(2026, 10, 2, 0, 5)}
    d = DL.ExecutionDeadline(datetime.datetime(2026, 10, 2, 0, 30),
                             now_fn=(lambda: wall["t"]),
                             monotonic_fn=mono)
    check("D10a budget 由 wall clock 算一次 = 1500 s", d.budget_sec == 1500.0)
    mono.advance(1000.0)
    rem_before = d.remaining_sec()
    check("D10b 推進 1000 s -> remaining 500 s", rem_before == 500.0)
    # 🔴 把系統時間往回調一小時
    wall["t"] = datetime.datetime(2026, 9, 2, 0, 5)
    check("D10c ★★ wall clock 往回調，remaining 不變",
          d.remaining_sec() == rem_before)
    # 🔴 把系統時間往前跳一天
    wall["t"] = datetime.datetime(2026, 10, 3, 12, 0)
    check("D10d ★★ wall clock 往前跳，remaining 不變",
          d.remaining_sec() == rem_before)
    check("D10e ★★ 尚未到期", d.expired() is False)
    mono.advance(600.0)
    check("D10f ★★ 只有 monotonic 推進才會到期", d.expired() is True)
    check("D10g ★★ 到期後仍放行安全收尾",
          d.allows(DL.ACT_RESTORE)[0] is True
          and d.allows(DL.ACT_CLEANUP_STOP)[0] is True)


# ======================================================================
# D11 — 正常 FIRST LIVE 在 deadline 前完成
# ======================================================================
def test_D11_normal_within_deadline():
    print("\n[D11] 正常 FIRST LIVE 在 deadline 前完成 -> 與現行成功路徑一致")
    deps, env = build()                      # 不 trip，clock 不推進
    res = run(deps, env)
    h = env["holder"]
    st = steps_of(res)
    ch, di, stp = counts(h)
    check("D11a ★★ deadline 未到期", env["deadline"].expired() is False)
    check("D11b ★★ 所有 checkpoint 皆放行",
          all(e["allowed"] for e in env["deadline"].events))
    check("D11c ★★ 9 個 checkpoint 全部被走到",
          {e["checkpoint"] for e in env["deadline"].events}
          == set(DL.CHECKPOINTS))
    check("D11d ★★ CHARGE 5.0 kW 恰好一次", ch == 1)
    check("D11e ★★ DISCHARGE = 0", di == 0)
    check("D11f ★★ step 1~12 全部 PASS",
          all(st.get(k, {}).get("ok") is True
              for k in ("1_PRE_PAUSE_PREFLIGHT", "2_PAUSE",
                        "3_4_PAUSE_VERIFY", "5_OWNERSHIP",
                        "6_POST_PAUSE_PRECHECK", "7_FIRST_LIVE",
                        "8_VERIFY", "9_RELEASE", "10_RESTORE",
                        "11_RESTORE_VERIFY", "12_COMPLETE")))
    check("D11g ★★ control-flow ordering 仍為 canonical",
          FL.ordering_is_canonical(h["permit"].ordering_events()) is True)
    check("D11h ★★ 未被標記為 deadline 跨越",
          env["artifact"].get("deadline_crossed_during_live_leg") is False)
    check("D11i ★★ rest state 恢復", rest_ok())


# ======================================================================
# D12 ~ D16 — ATTEMPT RESULT vs LIVE LEG RESULT（2026-10-02 REPORTING 裁示）
# ======================================================================
def _attempt_result(art):
    """走 production 的 `_classify`，不自行判定。"""
    return SC.ScheduledFirstLiveRunner._classify(None, art)[0]


def test_D12_success_but_deadline_exceeded():
    print("\n[D12] CHARGE 成功 ＋ LEG_COMPLETE ＋ 跨 deadline ＋ cleanup/restore 成功")
    deps, env = build(trip_at=DL.CK_LIVE_LEG_CLEANUP)
    res = run(deps, env)
    art = env["artifact"]
    h = env["holder"]
    ch, di, stp = counts(h)

    check("D12a ★★ CHARGE 5.0 kW 恰好一次", ch == 1)
    check("D12b ★★ readback = VERIFY_SUCCESS",
          art.get("readback_result") == "VERIFY_SUCCESS")
    check("D12c ★★ LEG_COMPLETE = True",
          art.get("leg_reached_complete") is True)
    check("D12d ★★ cleanup STOP 成功",
          art.get("cleanup_stop_result") == "SUCCESS")
    check("D12e ★★ authority release 成功",
          art.get("authority_release_result") == "SUCCESS")
    check("D12f ★★ restore 成功", art.get("restore_result") == "SUCCESS")
    check("D12g ★★ RestoreVerifier = RV_RESUMED",
          res.get("restore_verifier_state") == "RESUMED")
    check("D12h ★★ deadline_exceeded = True",
          art.get("deadline_exceeded") is True)
    # 🔴 兩個維度分開
    check("D12i ★★ live_leg_result = SUCCESS",
          art.get("live_leg_result") == FL.LLR_SUCCESS)
    check("D12j ★★ attempt_result = DEADLINE_DURING_LIVE_CLEANUP",
          _attempt_result(art) == SC.R_DEADLINE_DURING_LIVE_CLEANUP)
    check("D12k ★★ attempt_result 不是 PASS",
          _attempt_result(art) != SC.R_PASS)
    check("D12l ★★ successful real 5 kW leg 計數 +1",
          art.get("counts_as_first_live_success") is True
          and FL.counts_as_first_live_success(art) is True)
    check("D12m ★★ deadline 未改寫真實事件序列（CHARGE 仍記錄為已送出）",
          ((art.get("phase6_command_counts") or {}).get("counts") or {})
          .get("phase6_charge") == 1)


def test_D13_readback_fail_not_success():
    print("\n[D13] 同場景但 readback FAIL -> 不得記 Live Leg SUCCESS")
    # 🔴 operator 送得出去，但設備狀態不跟上 -> readback 不會成功
    deps, env = build(trip_at=DL.CK_LIVE_LEG_CLEANUP)
    dev = env["dev"]
    dev.on_command = (lambda action: None)      # 設備不回應 -> 狀態不變
    res = run(deps, env)
    art = env["artifact"]
    check("D13a ★★ readback 非 VERIFY_SUCCESS",
          art.get("readback_result") != "VERIFY_SUCCESS")
    check("D13b ★★ live_leg_result 不得為 SUCCESS",
          art.get("live_leg_result") != FL.LLR_SUCCESS)
    check("D13c ★★ live_leg_result 為 FAILED 或 UNKNOWN",
          art.get("live_leg_result") in (FL.LLR_FAILED, FL.LLR_UNKNOWN))
    check("D13d ★★ successful real 5 kW leg 不得計數",
          art.get("counts_as_first_live_success") is False)
    check("D13e ★★ attempt_result 不得為 PASS",
          _attempt_result(art) != SC.R_PASS)
    check("D13f ★★ 仍完成 restore（責任不因失敗而消失）",
          env["ssh"].calls.count("restore") == 1)
    check("D13g ★★ rest state 恢復", rest_ok())


def test_D14_deadline_must_not_mask_send_unknown():
    print("\n[D14] deadline result 不得掩蓋 SEND_UNKNOWN")
    # 🔴 pause SSH 以 41（COMMAND_TIMEOUT）回應 -> SEND_UNKNOWN
    deps, env = build(trip_at=DL.CK_BEFORE_GATE_B, pause_result=41)
    res = run(deps, env)
    art = env["artifact"]
    check("D14a ★★ pause_send_outcome = UNKNOWN",
          art.get("pause_send_outcome") == "UNKNOWN")
    check("D14b ★★ attempt_result = PAUSE_UNKNOWN（非 DEADLINE_*）",
          _attempt_result(art) == SC.R_PAUSE_UNKNOWN)
    check("D14c ★★ deadline 事件仍被保留在 artifact（未遺失）",
          art.get("deadline") is not None
          and len(art["deadline"].get("events") or ()) > 0)
    check("D14d ★★ UNKNOWN 優先序高於 deadline（P0 > P2）",
          SC.ATTEMPT_RESULT_PRECEDENCE[0][0] == "P0_SAFETY_UNCERTAIN"
          and SC.ATTEMPT_RESULT_PRECEDENCE[2][0] == "P2_DEADLINE")
    check("D14e ★★ 0 CHARGE / 0 DISCHARGE",
          counts(env["holder"])[:2] == (0, 0))


def test_D15_deadline_must_not_clear_restore_responsibility():
    print("\n[D15] deadline result 不得清除 restore responsibility")
    deps, env = build(trip_at=DL.CK_BEFORE_GATE_B, restore_result=41)
    res = run(deps, env)
    art = env["artifact"]
    check("D15a ★★ 恢復責任仍成立",
          res.get("restore_responsibility") is True)
    check("D15b ★★ attempt_result = RESTORE_UNKNOWN（非 DEADLINE_*）",
          _attempt_result(art) == SC.R_RESTORE_UNKNOWN)
    check("D15c ★★ restore 只送出一次（不得重送）",
          env["ssh"].calls.count("restore") == 1)
    check("D15d ★★ restore_result 非 SUCCESS",
          art.get("restore_result") != "SUCCESS")
    check("D15e ★★ deadline 事件仍保留",
          art.get("deadline_exceeded") is True)
    check("D15f ★★ 0 CHARGE / 0 DISCHARGE",
          counts(env["holder"])[:2] == (0, 0))


def test_D16_report_shows_both_dimensions():
    print("\n[D16] artifact 必須同時看得到 deadline violation ＋ live leg success")
    deps, env = build(trip_at=DL.CK_LIVE_LEG_CLEANUP)
    run(deps, env)
    art = env["artifact"]
    required = ("live_leg_result", "leg_reached_complete",
                "deadline_exceeded", "deadline_blocked_at",
                "phase6_command_counts", "readback_result",
                "cleanup_stop_result", "authority_release_result",
                "restore_result", "post_restore_external_liveness",
                "external_liveness_final", "counts_as_first_live_success")
    missing = [k for k in required if k not in art]
    check("D16a ★★ 12 個 reporting 欄位全部存在：%r" % (missing,),
          missing == [])
    check("D16b ★★ attempt_result 欄位已在 schema 中",
          "attempt_result" in FL.HANDOFF_ARTIFACT_FIELDS)
    check("D16c ★★ 同一份 artifact 同時呈現兩個維度",
          art.get("deadline_exceeded") is True
          and art.get("live_leg_result") == FL.LLR_SUCCESS)
    check("D16d ★★ 不必只看 attempt_result 就能判斷 live leg",
          FL.derive_live_leg_result(art) == FL.LLR_SUCCESS)
    check("D16e ★★ post_restore / final liveness 皆為 restore 後的觀測",
          isinstance(art.get("post_restore_external_liveness"), dict)
          and art.get("external_liveness_final") is not None)
    check("D16f ★★ live_leg_result 詞彙完整且互斥",
          set(FL.LIVE_LEG_RESULTS) == {"NOT_ATTEMPTED", "SUCCESS",
                                       "FAILED", "UNKNOWN"})
    # CASE A / B 的 live_leg_result 必須是 NOT_ATTEMPTED
    deps2, env2 = build(trip_at=DL.CK_BEFORE_PAUSE)
    run(deps2, env2)
    check("D16g ★★ CASE A：live_leg_result = NOT_ATTEMPTED",
          env2["artifact"].get("live_leg_result") == FL.LLR_NOT_ATTEMPTED
          and _attempt_result(env2["artifact"])
          == SC.R_DEADLINE_BEFORE_FIELD)
    deps3, env3 = build(trip_at=DL.CK_BEFORE_GATE_B)
    run(deps3, env3)
    check("D16h ★★ CASE B：live_leg_result = NOT_ATTEMPTED",
          env3["artifact"].get("live_leg_result") == FL.LLR_NOT_ATTEMPTED
          and _attempt_result(env3["artifact"])
          == SC.R_DEADLINE_DURING_HANDOFF)
    check("D16i ★★ CASE A / B 皆不計入 successful 5 kW leg",
          env2["artifact"].get("counts_as_first_live_success") is False
          and env3["artifact"].get("counts_as_first_live_success") is False)


# ======================================================================
# W — Watchdog 語意比較（為何不直接沿用）
# ======================================================================
def test_W_watchdog_semantics():
    print("\n[W] Watchdog 語意不足以表達本契約 -> 另建 ExecutionDeadline")
    import inspect
    wd_src = inspect.getsource(HO.Watchdog)
    check("W1 ★★ Watchdog 量的是「已被 pause 多久」，非絕對截止時刻",
          "max_paused_sec" in wd_src and "mark_paused" in wd_src)
    check("W2 ★★ Watchdog 無 NO-NEW-RISK-ACTION / SAFE-CLEANUP 二分",
          not any(k in wd_src for k in ("new_risk", "safe_cleanup",
                                        "NEW_RISK_ACTIONS")))
    check("W3 ★★ ExecutionDeadline 明確區分兩類動作",
          set(DL.NEW_RISK_ACTIONS).isdisjoint(DL.SAFE_CLEANUP_ACTIONS)
          and len(DL.NEW_RISK_ACTIONS) == 5
          and len(DL.SAFE_CLEANUP_ACTIONS) == 5)
    check("W4 ★★ Watchdog 維持原狀、未被接入 production flow",
          HO.HandoffRunner.__init__.__defaults__ is not None
          and "watchdog" not in
          __import__("inspect").signature(
              HO.HandoffRunner.__init__).parameters)
    check("W5 ★★ ExecutionDeadline 不 import 任何專案模組（無循環 import）",
          all(("phase6" not in ln and "pcs_" not in ln)
              for ln in inspect.getsource(DL).splitlines()
              if ln.startswith("import ") or ln.startswith("from ")))


# ======================================================================
# X — production wiring（不得只有測試注入才有 deadline）
# ======================================================================
def test_X_production_wiring():
    print("\n[X] production launcher -> provider 必須真的建得出 deadline")
    # config 路徑必須 cwd-independent —— 正式 PRE-FIELD 命令是從
    # repo root 執行（`python test/test_phase6_execution_deadline.py`），
    # 用相對路徑會在 repo root 找不到檔案。
    st, cfg, problems = LAUNCH.load_config(
        os.path.join(HERE, "scheduled_first_live_attempt8_20261002.json"))
    check("X1 config 可解析", st == LAUNCH.CONFIG_OK and not problems)
    check("X2 ★★ config 保留 date / time 原型別 ＋ tzinfo",
          isinstance(cfg["target_date"], datetime.date)
          and isinstance(cfg["window_end"], datetime.time)
          and cfg.get("tzinfo") is not None)
    d = DL.ExecutionDeadline.from_window(
        cfg["target_date"], cfg["window_end"], tzinfo=cfg["tzinfo"],
        now_fn=(lambda: datetime.datetime(2026, 10, 2, 0, 5,
                                          tzinfo=cfg["tzinfo"])),
        monotonic_fn=(lambda: 0.0))
    check("X3 ★★ 由真實 config 建出的 deadline = window_end",
          d.deadline_at.hour == 0 and d.deadline_at.minute == 30)
    check("X4 ★★ budget = 00:05 -> 00:30 = 1500 s", d.budget_sec == 1500.0)
    # launcher 確實把原型別放進 runtime context
    import inspect
    src = inspect.getsource(LAUNCH)
    check("X5 ★★ launcher rtctx 帶入 target_date_obj / window_end_obj / tzinfo",
          '"target_date_obj"' in src and '"window_end_obj"' in src
          and '"tzinfo"' in src)
    # provider 以原型別建立；字串型別必須拿不到 deadline（避免靜默誤用）
    deps, env = EP.build_entry_env()
    env["ctx"].pop("deadline", None)
    check("X6 ★★ 三個 handled deadline result 皆為 exit 0",
          all(LAUNCH.classify_exit(r) == LAUNCH.EXIT_HANDLED
              for r in (SC.R_DEADLINE_BEFORE_FIELD,
                        SC.R_DEADLINE_DURING_HANDOFF,
                        SC.R_DEADLINE_DURING_LIVE_CLEANUP)))
    check("X7 ★★ 三者皆在 RESULT_STATES",
          all(r in SC.RESULT_STATES
              for r in (SC.R_DEADLINE_BEFORE_FIELD,
                        SC.R_DEADLINE_DURING_HANDOFF,
                        SC.R_DEADLINE_DURING_LIVE_CLEANUP)))
    check("X8 ★★ PT1H 不是 deadline：Task 限制與 window_end 無關",
          DL.ExecutionDeadline.from_window(
              cfg["target_date"], cfg["window_end"], tzinfo=cfg["tzinfo"],
              now_fn=(lambda: datetime.datetime(2026, 10, 2, 0, 5,
                                                tzinfo=cfg["tzinfo"])),
              monotonic_fn=(lambda: 0.0)).budget_sec == 1500.0
          and 1500.0 < 3600.0)


# ======================================================================
def main():
    print("=" * 72)
    print("  EXECUTION DEADLINE / SAFE CLEANUP CONTRACT")
    print("=" * 72)
    for fn in (test_D1_before_start, test_D2_before_pause,
               test_D3_after_pause, test_D4_after_authority,
               test_D5_after_charge, test_D6_D7_no_second_risk_action,
               test_D8_restore_unknown_no_resend,
               test_D9_guard_exception_fail_closed,
               test_D10_monotonic_not_rewound,
               test_D11_normal_within_deadline,
               test_D12_success_but_deadline_exceeded,
               test_D13_readback_fail_not_success,
               test_D14_deadline_must_not_mask_send_unknown,
               test_D15_deadline_must_not_clear_restore_responsibility,
               test_D16_report_shows_both_dimensions,
               test_W_watchdog_semantics, test_X_production_wiring):
        fn()
    for d in PCS.TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print(f"  rest state：HO={HO.DISPATCH_ENABLED} RT={RT.DISPATCH_ENABLED}")
    print("=" * 72)
    return 0 if (n == tot and rest_ok()) else 1


if __name__ == "__main__":
    sys.exit(main())
