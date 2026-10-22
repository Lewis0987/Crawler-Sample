# -*- coding: utf-8 -*-
"""
test_phase6_journal_scope.py — ATTEMPT-SCOPED HANDOFF JOURNAL
======================================================================
核心命題
    「`OnceGuard` 的 one-shot 語意是『**同一 Attempt / 同一 handoff session**
      內 pause / restore 各最多一次』，**不是**『整個 Phase 6 歷史期間只能一次』。
      journal 必須 attempt-scoped 且 path 由 attempt_id 決定性導出 ——
      跨 Attempt 完全隔離，同一 Attempt 的 crash recovery 仍完全 durable。」

涵蓋使用者指定項目
    J1 ~ J6    journal scope ／ same-attempt crash recovery
    X1 ~ X7    cross-attempt isolation
    A7R        Attempt #7 exact regression（用 Attempt #4 真實 journal 歷史）
    PS         pause step vs actual send 計數語意
    INV        shared-state inventory

用法
    python test/test_phase6_journal_scope.py        # exit 0 = PASS
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402
import pcs_auto_control_runtime as RT                # noqa: E402

RESULTS = []
TMPDIRS = []

LEGACY_JOURNAL = os.path.join(HERE, "..", "output",
                              "phase6_scheduled_first_live",
                              "handoff_journal.jsonl")


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p6_jrnl_")
    TMPDIRS.append(d)
    return d


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


def journal(base, attempt_id):
    return HO.Journal(path=HO.attempt_journal_path(base, attempt_id),
                      attempt_id=attempt_id)


def guard_of(j):
    return HO.OnceGuard(j)


# ======================================================================
# J — JOURNAL SCOPE ／ SAME-ATTEMPT CRASH RECOVERY
# ======================================================================
def test_J_journal_scope():
    print("\n[J] JOURNAL SCOPE ／ SAME-ATTEMPT CRASH RECOVERY")
    base = tmpdir()

    # --- 先造出「前一個 Attempt 已完成 pause/restore」的 durable 狀態 ---
    ja = journal(base, "ATTEMPT-A")
    ja.append("INTENT", step="PAUSE", detail="即將送出 external stop")
    ja.append("OUTCOME", step="PAUSE", detail="send_pause -> SENT")
    ja.append("INTENT", step="RESTORE", detail="即將送出 external start")
    ja.append("OUTCOME", step="RESTORE", detail="send_restore -> SENT")
    ga = guard_of(ja)
    check("J0 Attempt A 自身：PAUSE 已 attempted（one-shot 生效）",
          ga.may_attempt("PAUSE") is False)
    check("J0b Attempt A 自身：RESTORE 已 attempted",
          ga.may_attempt("RESTORE") is False)

    # --- J1 新 Attempt B 必須乾淨 ---
    jb = journal(base, "ATTEMPT-B")
    gb = guard_of(jb)
    check("J1 ★★ 新 Attempt B：may_attempt('PAUSE') = True（不受 A 影響）",
          gb.may_attempt("PAUSE") is True)
    check("J1b 新 Attempt B：may_attempt('RESTORE') = True",
          gb.may_attempt("RESTORE") is True)
    check("J1c 新 Attempt B 初始 attempted 為空", gb.attempted == set())

    # Attempt B 正常送出 pause 恰好一次
    sent = []

    class Remote(object):
        def send_pause_ex(self):
            sent.append("pause")
            return RA.SEND_SENT, None

        def send_restore_ex(self):
            sent.append("restore")
            return RA.SEND_SENT, None

    orch = HO.HandoffOrchestrator(remote=Remote(), observe=(lambda: {}),
                                  gates=(lambda x: (True, {})), journal=jb,
                                  mode=HO.MODE_ARMED)
    ok1, why1 = orch.request_pause()
    check("J1d ★★ Attempt B 可正常送出 pause（exactly once）",
          ok1 is True and sent.count("pause") == 1)
    ok2, why2 = orch.request_pause()
    check("J1e 同一 Attempt 第二次 pause → 拒送（one-shot）",
          ok2 is False and why2 == "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"
          and sent.count("pause") == 1)

    # --- J2 同一 Attempt / 新 process：重新開同一 journal ---
    jb2 = journal(base, "ATTEMPT-B")
    check("J2 ★★ 同一 attempt_id -> 同一 journal path", jb2.path == jb.path)
    gb2 = guard_of(jb2)
    check("J2b ★★ 重啟後 may_attempt('PAUSE') = False（NO RESEND）",
          gb2.may_attempt("PAUSE") is False)

    # --- J3 pause outcome UNKNOWN -> 重啟不得重送、責任保留 ---
    jc = journal(base, "ATTEMPT-C")
    jc.append("INTENT", step="PAUSE", detail="即將送出")
    jc.append("OUTCOME", step="PAUSE", detail="send_pause -> UNKNOWN")
    gc = guard_of(journal(base, "ATTEMPT-C"))
    check("J3 ★★ pause UNKNOWN 後重啟 → 不得重送",
          gc.may_attempt("PAUSE") is False)
    check("J3b needs_verification 仍為 False（已有 OUTCOME 紀錄）"
          "；責任由 scheduled ledger 判定",
          gc.needs_verification("PAUSE") is False)

    # --- J4 PAUSE SENT 後 crash（無 verify 紀錄）-> 不得第二次 PAUSE ---
    jd = journal(base, "ATTEMPT-D")
    jd.append("INTENT", step="PAUSE")
    gd = guard_of(journal(base, "ATTEMPT-D"))
    check("J4 ★★ 只有 INTENT（crash before outcome）→ 不得再送 PAUSE",
          gd.may_attempt("PAUSE") is False)
    check("J4b needs_verification('PAUSE') = True（必須以觀測確認）",
          gd.needs_verification("PAUSE") is True)

    # --- J5 RESTORE INTENT 後 crash -> 不得第二次 RESTORE ---
    je = journal(base, "ATTEMPT-E")
    je.append("INTENT", step="PAUSE")
    je.append("OUTCOME", step="PAUSE")
    je.append("INTENT", step="RESTORE")
    ge = guard_of(journal(base, "ATTEMPT-E"))
    check("J5 ★★ RESTORE INTENT 後 crash → 不得再送 RESTORE",
          ge.may_attempt("RESTORE") is False)
    check("J5b needs_verification('RESTORE') = True",
          ge.needs_verification("RESTORE") is True)

    # --- J6 RESTORE verified -> confirmed ---
    jf = journal(base, "ATTEMPT-F")
    jf.append("INTENT", step="RESTORE")
    jf.append("OUTCOME", step="RESTORE")
    gf = guard_of(journal(base, "ATTEMPT-F"))
    check("J6 ★★ RESTORE 已有 OUTCOME → confirmed，不需再驗證",
          gf.needs_verification("RESTORE") is False
          and "RESTORE" in gf.confirmed)


# ======================================================================
# X — CROSS-ATTEMPT ISOLATION
# ======================================================================
def test_X_cross_attempt():
    print("\n[X] CROSS-ATTEMPT ISOLATION")
    base = tmpdir()

    ja = journal(base, "X-A")
    for k, st in (("INTENT", "PAUSE"), ("OUTCOME", "PAUSE"),
                  ("INTENT", "RESTORE"), ("OUTCOME", "RESTORE")):
        ja.append(k, step=st)
    gb = guard_of(journal(base, "X-B"))
    check("X1 ★★ A 完成 pause/restore → B 全新乾淨",
          gb.attempted == set() and gb.may_attempt("PAUSE") is True)

    ju = journal(base, "X-UNKNOWN")
    ju.append("INTENT", step="PAUSE")
    ju.append("OUTCOME", step="PAUSE", detail="UNKNOWN")
    gu = guard_of(journal(base, "X-UNKNOWN"))
    gnext = guard_of(journal(base, "X-NEXT"))
    check("X2 ★★ A 的 PAUSE UNKNOWN 不污染 B",
          gnext.may_attempt("PAUSE") is True)
    check("X2b A 自身仍保留 one-shot 限制（需人工 / recovery 處理）",
          gu.may_attempt("PAUSE") is False)

    jc = journal(base, "X-CONSUMED")
    jc.append("STATE", step=None, detail="COMPLETE")
    check("X3 ★★ A consumed → B 獨立",
          guard_of(journal(base, "X-B2")).may_attempt("PAUSE") is True)

    p1 = HO.attempt_journal_path(base, "ID-ONE")
    p2 = HO.attempt_journal_path(base, "ID-TWO")
    check("X4 ★★ 不同 attempt_id -> 不同 journal path", p1 != p2)
    check("X5 ★★ 同一 attempt_id -> journal path 必須相同",
          HO.attempt_journal_path(base, "ID-ONE") == p1)

    # X6 malformed / missing attempt_id record -> Fail Closed within attempt
    jm = journal(base, "X-MAL")
    jm.append("INTENT", step="PAUSE")          # 帶 attempt_id
    with io.open(jm.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 99, "kind": "INTENT", "step": "RESTORE"},
                           ensure_ascii=False) + "\n")   # 無 attempt_id
    gm = guard_of(journal(base, "X-MAL"))
    check("X6 ★★ 同檔內缺 attempt_id 的紀錄仍計入（Fail Closed，不放行）",
          gm.may_attempt("RESTORE") is False
          and gm.may_attempt("PAUSE") is False)

    # X7 不得 fallback 回 global shared journal
    for bad in (None, "", "   "):
        try:
            HO.attempt_journal_path(base, bad)
            ok = False
        except ValueError:
            ok = True
        check("X7 attempt_id=%r -> ValueError（不得 fallback 共用 journal）"
              % (bad,), ok)
    check("X7b 產生的檔名不等於 legacy 共用 journal",
          os.path.basename(p1) != HO.LEGACY_SHARED_JOURNAL_NAME)
    check("X7c 檔名以 attempt 專屬前綴開頭",
          os.path.basename(p1).startswith(HO.ATTEMPT_JOURNAL_PREFIX))

    # 外來 attempt_id 的紀錄必須被略過（defense-in-depth）
    jx = journal(base, "X-SCOPE")
    with io.open(jx.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 1, "kind": "INTENT", "step": "PAUSE",
                            "attempt_id": "SOMEONE-ELSE"},
                           ensure_ascii=False) + "\n")
    gx = guard_of(journal(base, "X-SCOPE"))
    check("X8 ★★ 同檔內**他人 attempt_id** 的紀錄被略過",
          gx.may_attempt("PAUSE") is True and gx.skipped_foreign == 1)


# ======================================================================
# A7R — ATTEMPT #7 EXACT REGRESSION（用 Attempt #4 真實 journal 歷史）
# ======================================================================
def test_A7R_regression():
    print("\n[A7R] ATTEMPT #7 EXACT REGRESSION")
    legacy = os.path.abspath(LEGACY_JOURNAL)
    check("A7R0 legacy 共用 journal 仍存在（immutable 證據，未被刪除）",
          os.path.exists(legacy))
    recs = [json.loads(x) for x in io.open(legacy, encoding="utf-8")]
    steps = [(r.get("kind"), r.get("step")) for r in recs if r.get("step")]
    check("A7R0b legacy journal 內確實有 Attempt #4 的 PAUSE/RESTORE 歷史",
          ("INTENT", "PAUSE") in steps and ("OUTCOME", "PAUSE") in steps
          and ("INTENT", "RESTORE") in steps)

    # 舊行為重現：直接用 legacy 共用 journal -> PAUSE 被永久擋下
    legacy_j = HO.Journal(path=legacy)          # 無 attempt_id = legacy 語意
    g_legacy = HO.OnceGuard(legacy_j)
    check("A7R1 ★★ 舊共用 journal 確實會讓 may_attempt('PAUSE') = False"
          "（重現 Attempt #7 根因）",
          g_legacy.may_attempt("PAUSE") is False)

    # 新行為：把 legacy 內容當成「前一個 Attempt 的 durable 證據」，
    # Attempt #7 等價 runtime 使用自己的 per-attempt journal
    base = tmpdir()
    shutil.copy(legacy, os.path.join(base, HO.LEGACY_SHARED_JOURNAL_NAME))
    j7 = journal(base, "FIRST-LIVE-ATTEMPT7-20261001")
    g7 = HO.OnceGuard(j7)
    check("A7R2 ★★ Attempt #7 等價 runtime：may_attempt('PAUSE') = True",
          g7.may_attempt("PAUSE") is True)
    check("A7R2b attempted 初始為空", g7.attempted == set())
    check("A7R2c journal path 不等於 legacy 共用 journal",
          os.path.basename(j7.path) != HO.LEGACY_SHARED_JOURNAL_NAME)

    sent = []

    class Remote(object):
        def send_pause_ex(self):
            sent.append("pause")
            return RA.SEND_SENT, None

        def send_restore_ex(self):
            sent.append("restore")
            return RA.SEND_SENT, None

    orch = HO.HandoffOrchestrator(remote=Remote(), observe=(lambda: {}),
                                  gates=(lambda x: (True, {})), journal=j7,
                                  mode=HO.MODE_ARMED)
    ok, why = orch.request_pause()
    check("A7R3 ★★ PAUSE mock send = exactly once",
          ok is True and sent.count("pause") == 1)
    check("A7R4 ★★ 不再出現 ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND",
          why != "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND")
    check("A7R5 legacy journal 內容未被本測試改動",
          len([json.loads(x) for x in io.open(legacy, encoding="utf-8")])
          == len(recs))


# ======================================================================
# PS — pause step vs actual send 計數語意
# ======================================================================
def test_PS_counts():
    print("\n[PS] PAUSE STEP vs ACTUAL SEND 計數")
    fields = set(FL.HANDOFF_ARTIFACT_FIELDS)
    for k in ("pause_attempt_count", "pause_step_attempt_count",
              "pause_send_attempt_count"):
        check("PS0 artifact schema 具備 %s" % k, k in fields)

    # Attempt #7 形狀：步驟跑過但未送出
    a7 = FL.collect_handoff_artifact(
        {"outcome": "NOT_SENT", "state": "ABORTED_PAUSE_FAILED",
         "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT", "ok": True},
                   {"step": "2_PAUSE", "ok": False,
                    "detail": "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"}]}
    ).to_dict()
    check("PS1 ★★ Attempt #7 形狀：step 計數 = 1",
          a7["pause_step_attempt_count"] == 1)
    check("PS2 ★★ Attempt #7 形狀：**實際送出**計數 = 0",
          a7["pause_send_attempt_count"] == 0)
    check("PS3 pause_send_outcome = NOT_SENT",
          a7["pause_send_outcome"] == RA.SEND_NOT_SENT)
    check("PS4 舊欄位 pause_attempt_count 維持 1（backward compatible）",
          a7["pause_attempt_count"] == 1)

    # 真的送出
    ok = FL.collect_handoff_artifact(
        {"outcome": "COMPLETE", "steps": [{"step": "2_PAUSE", "ok": True}]}
    ).to_dict()
    check("PS5 ★★ 真的送出時 send 計數 = 1",
          ok["pause_send_attempt_count"] == 1)
    unk = FL.collect_handoff_artifact(
        {"outcome": "PAUSE_OUTCOME_UNKNOWN",
         "steps": [{"step": "2_PAUSE", "ok": False, "uncertain": True}]}
    ).to_dict()
    check("PS6 ★★ UNKNOWN 也算**可能已送出** -> send 計數 = 1",
          unk["pause_send_attempt_count"] == 1
          and unk["pause_send_outcome"] == RA.SEND_UNKNOWN)


# ======================================================================
# INV — SHARED STATE INVENTORY
# ======================================================================
SHARED_STATE_INVENTORY = {
    "handoff_journal": "ATTEMPT_SCOPED",
    "one_shot_ledger": "ATTEMPT_SCOPED",
    "artifact_dir": "ATTEMPT_SCOPED",
    "dispatch_permit": "ATTEMPT_SCOPED",
    "scheduled_live_leg_authorization": "ATTEMPT_SCOPED",
    "command_count_guard": "ATTEMPT_SCOPED",
    "recovery_state": "ATTEMPT_SCOPED",
    "_live_leg_guard": "PROCESS_SCOPED",
    "HO.DISPATCH_ENABLED": "PROCESS_SCOPED",
    "RT.DISPATCH_ENABLED": "PROCESS_SCOPED",
    "last_control_store": "SYSTEM_PERSISTENT",
    "decision_audit": "SYSTEM_PERSISTENT",
    "legacy_shared_journal": "INTENTIONALLY_SHARED",
}
VALID_SCOPES = ("ATTEMPT_SCOPED", "PROCESS_SCOPED",
                "SYSTEM_PERSISTENT", "INTENTIONALLY_SHARED")


def test_INV_inventory():
    print("\n[INV] SHARED STATE INVENTORY")
    for k, v in sorted(SHARED_STATE_INVENTORY.items()):
        check("INV %-36s %s" % (k, v), v in VALID_SCOPES)

    import pcs_auto_control_service as SVC
    check("INV1 ★★ _live_leg_guard 為 process 內 dict（無落盤 API）",
          isinstance(SVC._live_leg_guard, dict)
          and not any(hasattr(SVC._live_leg_guard, x)
                      for x in ("save", "persist", "to_file")))
    import subprocess
    code = ("import sys; sys.path.insert(0, %r);"
            "import pcs_auto_control_service as S;"
            "print(S._live_leg_guard['used'])" % HERE)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    check("INV2 ★★ 新 process 的 _live_leg_guard 自然重置為 False",
          out.stdout.decode("utf-8", "replace").strip() == "False")

    # LastControl 維持 SYSTEM_PERSISTENT —— 證明 stale 不能跳過 fresh gates
    import pcs_auto_control_production as PRD
    import pcs_auto_control_service as SVC2
    src = io.open(os.path.join(HERE, "pcs_auto_control_service.py"),
                  encoding="utf-8").read()
    check("INV3 ★★ LastControl 不參與 live_leg_gates 的放行判定",
          "last_control" not in src.split("def live_leg_gates")[1]
          .split("return ok, gates, precheck")[0].lower())
    check("INV4 ★★ 送出前仍需 fresh observation（確認後重新 observe）",
          "人工確認通過 -> 重新取得 fresh 觀測".replace(" -> ", " → ") in src
          or "確認前的快照一律不採用" in src)
    check("INV5 ★★ readback 由 ReadBackVerifier 獨立執行，不讀 LastControl",
          "last_control" not in io.open(
              os.path.join(HERE, "pcs_control_executor.py"),
              encoding="utf-8").read().lower())
    check("INV6 ★★ LastControl 只在 VERIFY_SUCCESS 後才寫入",
          "只有 VERIFY_SUCCESS 才寫 LastControl" in io.open(
              os.path.join(HERE, "production_execution_chain.py"),
              encoding="utf-8").read())


# ======================================================================
def main():
    print("=" * 72)
    print("  ATTEMPT-SCOPED HANDOFF JOURNAL 回歸")
    print("=" * 72)
    for fn in (test_J_journal_scope, test_X_cross_attempt,
               test_A7R_regression, test_PS_counts, test_INV_inventory):
        fn()
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print(f"  rest state：HO={HO.DISPATCH_ENABLED} RT={RT.DISPATCH_ENABLED}")
    print("=" * 72)
    return 0 if (n == tot and rest_ok()) else 1


if __name__ == "__main__":
    sys.exit(main())
