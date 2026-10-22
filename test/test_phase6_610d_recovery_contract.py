# -*- coding: utf-8 -*-
"""
test_phase6_610d_recovery_contract.py — Recovery Evidence Contract（BUG FIX）
======================================================================
核心命題
    「pause 送出後若得到 SEND_UNKNOWN，責任已成立且**不得重送**，
      之後只能靠 recover() 判斷 external 是否已恢復。
      若 recover() 依賴一個 guard 根本不輸出的欄位，
      它會永遠回 R_UNKNOWN —— Field Validation 會被自己卡死。」

🔴 BUG（本檔鎖住的修正）
       OLD: recover() 讀 `probe.get("running")`；B2 guard 的 probe
            只輸出 guard_variant / capability_* / screen_count / pid /
            cwd / exe / cmd / ppid —— **沒有 running**。
            `UnattendedService.boot()` 取 `probe.get("controller_running")`
            同樣永遠 None → R_UNKNOWN → CRIT_RECOVERY_UNKNOWN → BOOT_REFUSED。
       NEW: 改用與 RestoreVerifier **同一組**外部可觀測證據：
            A 層 runtime health（ssh/screen/process/identity）
            B 層 loop activity（loopcheck 時間戳前進）
            由 `phase6_remote_adapter.external_liveness` 統一正規化成三態。

🔴 `running` 的語意是 **external controller active loop confirmed**，
   不是 process exists、不是 screen exists。
🔴 recovery 證據**只准** probe + loopcheck（皆 PASSIVE_READ_ONLY）；
   **不得**使用 status（QUERY_ONLY_WITH_STDIN_WRITE）。
🔴 全部 zero-I/O：fake probe / fake loop mark / fake transport，不碰 real SSH。

R1  probe healthy + marks 前進        → R_CLEAN
R2  probe healthy + marks 未前進      → R_PENDING_RESTORE
R3  probe healthy + loopcheck 取不到  → R_UNKNOWN
R4  probe identity mismatch           → Fail Closed
R5  screen 數量異常                    → Fail Closed
R6  SEND_UNKNOWN 之後不得 resend pause
R7  restore responsibility 只有確認後才可清除
R8  recover 不得使用 status stdin query
R9  zero-I/O：全程 fake，未接觸真實 SSH

用法
    python test_phase6_610d_recovery_contract.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA                  # noqa: E402
import phase6_handoff_orchestrator as HO            # noqa: E402
import phase6_unattended_service as US              # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ======================================================================
# 假件（zero-I/O）
# ======================================================================
class FakeJournal(object):
    """只保留 recover() 需要的行為，不落盤。"""

    def __init__(self, records=None):
        self.records = list(records or [])

    def append(self, kind, state=None, ownership=None, detail=None, **extra):
        rec = {"kind": kind, "state": state, "detail": detail}
        rec.update(extra)
        self.records.append(rec)
        return rec

    def read_all(self):
        return list(self.records)

    def last_state(self):
        st = None
        for r in self.records:
            if r.get("state"):
                st = r["state"]
        return st


def pending_journal():
    """製造一筆「pause 已送出但尚未確認恢復」的 journal。"""
    j = FakeJournal()
    j.append("INTENT", state=HO.S_PAUSE_REQUESTED, step="PAUSE",
             detail="即將送出 external stop")
    j.append("STATE", state=HO.S_PAUSE_REQUESTED)
    return j


def probe(ssh=True, screen=True, proc=True, ident=True):
    return RA.RemoteProbe(ssh_ok=ssh, screen_alive=screen,
                          process_alive=proc, process_identity_ok=ident)


MARKS_ADVANCING = ["2026-09-17 11:12:59", "2026-09-17 11:13:02",
                   "2026-09-17 11:13:07"]
MARKS_FLAT = ["2026-09-17 11:12:59"] * 3
MIN_OBS = HO.HandoffConfig().restore_loop_min_observations


def evidence(p, marks, min_obs=MIN_OBS):
    return {"probe": p, "loop_marks": marks, "min_loop_observations": min_obs}


# ======================================================================
# R1 / R2 / R3
# ======================================================================
def test_R1_running_confirmed():
    print("\n[R1] probe healthy + loop marks 前進 → 已恢復")
    v, why = RA.external_liveness(probe(), loop_marks=MARKS_ADVANCING,
                                  min_loop_observations=MIN_OBS)
    check(f"★★ R1a. external_liveness = {v}", v == RA.EXT_RUNNING)
    verdict, last, action = HO.recover(pending_journal(),
                                       evidence(probe(), MARKS_ADVANCING))
    check(f"★★ R1b. recover = {verdict}（責任解除，可正常啟動）",
          verdict == HO.R_CLEAN)
    check("★★ R1c. 舊契約下同一情境會卡死（probe 無 running → R_UNKNOWN）",
          HO.recover(pending_journal(),
                     {"running": probe().as_dict().get("running")})[0]
          == HO.R_UNKNOWN)
    check(f"  R1d. 理由可解釋：{why}", bool(why) and bool(action))


def test_R2_not_running():
    print("\n[R2] probe healthy + loop marks 未前進 → 必須先恢復")
    v, why = RA.external_liveness(probe(), loop_marks=MARKS_FLAT,
                                  min_loop_observations=MIN_OBS)
    check(f"★★ R2a. external_liveness = {v}", v == RA.EXT_NOT_RUNNING)
    verdict, _l, action = HO.recover(pending_journal(),
                                     evidence(probe(), MARKS_FLAT))
    check(f"★★ R2b. recover = {verdict}（不得開始新交接）",
          verdict == HO.R_PENDING_RESTORE)
    check(f"  R2c. 理由：{why}", "未前進" in why)
    check("★★ R2d. process 活著 ≠ 正在 loop（本例 runtime 全健康仍判 NOT_RUNNING）",
          probe().healthy is True and v == RA.EXT_NOT_RUNNING)


def test_R3_loop_unavailable():
    print("\n[R3] probe healthy 但 loopcheck 取不到 / 樣本不足 → Fail Closed")
    for name, marks in (("完全取不到", []),
                        ("全為 None", [None, None, None]),
                        ("樣本不足", MARKS_ADVANCING[:1]),
                        ("marks=None", None)):
        v, _why = RA.external_liveness(probe(), loop_marks=marks,
                                       min_loop_observations=MIN_OBS)
        verdict, _l, _a = HO.recover(pending_journal(),
                                     evidence(probe(), marks))
        check(f"★★ R3. {name} → {v} / recover={verdict}",
              v == RA.EXT_UNKNOWN and verdict == HO.R_UNKNOWN)
    check("★★ R3e. min_loop_observations 未提供 → None（不替呼叫端決定觀測長度）",
          RA.loop_active_from_marks(MARKS_ADVANCING, None) is None
          and RA.loop_active_from_marks(MARKS_ADVANCING, 1) is None)


# ======================================================================
# R4 / R5 —— Fail Closed
# ======================================================================
def test_R4_identity_mismatch():
    print("\n[R4] process identity 不符 → 依既有 taxonomy Fail Closed")
    v, why = RA.external_liveness(probe(ident=False),
                                  loop_marks=MARKS_ADVANCING,
                                  min_loop_observations=MIN_OBS)
    check(f"★★ R4a. identity 不符 → {v}（即使 loop marks 前進）",
          v == RA.EXT_NOT_RUNNING and "process_identity_ok" in why)
    verdict, _l, _a = HO.recover(pending_journal(),
                                 evidence(probe(ident=False),
                                          MARKS_ADVANCING))
    check(f"★★ R4b. recover = {verdict}（不得開始新交接）",
          verdict == HO.R_PENDING_RESTORE)

    # 連不上 / 事實取不到 → UNKNOWN（**不得**謊稱已停止而誤觸恢復動作）
    v2, why2 = RA.external_liveness(
        RA.RemoteProbe(ssh_ok=None, screen_alive=None,
                       process_alive=None, process_identity_ok=None),
        loop_marks=MARKS_ADVANCING, min_loop_observations=MIN_OBS)
    check(f"★★ R4c. probe 事實取不到 → {v2}（UNKNOWN，非 NOT_RUNNING）",
          v2 == RA.EXT_UNKNOWN and "不完整" in why2)
    check("★★ R4d. 未提供 probe → UNKNOWN",
          RA.external_liveness(None)[0] == RA.EXT_UNKNOWN
          and RA.external_liveness("不是 probe")[0] == RA.EXT_UNKNOWN)
    check(f"  R4e. 三態集合 = {RA.EXTERNAL_LIVENESS_VERDICTS}",
          len(RA.EXTERNAL_LIVENESS_VERDICTS) == 3)


def test_R5_screen_ambiguous():
    print("\n[R5] screen 數量異常 → Fail Closed（沿用既有 guard taxonomy）")
    v, why = RA.external_liveness(probe(screen=False),
                                  loop_marks=MARKS_ADVANCING,
                                  min_loop_observations=MIN_OBS)
    check(f"★★ R5a. screen 不存在 → {v}", v == RA.EXT_NOT_RUNNING
          and "screen_alive" in why)
    check("★★ R5b. screen 未知（None）→ UNKNOWN，不臆測",
          RA.external_liveness(probe(screen=None),
                               loop_marks=MARKS_ADVANCING,
                               min_loop_observations=MIN_OBS)[0]
          == RA.EXT_UNKNOWN)
    check("★★ R5c. guard 端 screen 數 ≠ 1 仍走既有 exit 44 → SEND_NOT_SENT",
          RA.GUARD_EXIT_AMBIGUOUS == 44
          and RA.SSH_GUARD_AMBIGUOUS in RA.SSH_DEFINITELY_NOT_EXECUTED)


# ======================================================================
# R6 / R7 —— 責任語意（既有行為，本次不得被改壞）
# ======================================================================
class _Remote(object):
    """只回固定 send 結果的假 remote。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.pause_calls = 0

    def send_pause_ex(self):
        self.pause_calls += 1
        return self.outcome, None

    def send_restore_ex(self):
        return self.outcome, None

    def probe(self):
        return {}


def test_R6_no_resend_after_unknown():
    print("\n[R6] SEND_UNKNOWN 之後不得 resend pause")
    j = FakeJournal()
    o = HO.HandoffOrchestrator(remote=_Remote(RA.SEND_UNKNOWN),
                               observe=lambda: {}, gates=lambda x: (True, {}),
                               journal=j, mode=HO.MODE_ARMED)
    o._to(HO.S_PREFLIGHT)                       # 依既有合法轉移：IDLE → PREFLIGHT
    o._to(HO.S_PAUSE_REQUESTED)
    sent, why = o.request_pause()
    check(f"★★ R6a. 第一次送出 → sent={sent} why={why}",
          sent is False and why == "SEND_OUTCOME_UNKNOWN_MUST_PROBE")
    check("  R6b. 底層確實只被呼叫一次", o.remote.pause_calls == 1)
    sent2, why2 = o.request_pause()
    check(f"★★ R6c. 再次嘗試 → {why2}（不得重送）",
          sent2 is False
          and why2 == "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"
          and o.remote.pause_calls == 1)
    oks = [r.get("ok") for r in j.records if r.get("kind") == "OUTCOME"]
    check(f"★★ R6d. journal 的 ok 為三值，不確定寫成 None：{oks}",
          oks and oks[0] is None)
    check("★★ R6e. taxonomy 未變：exit 41 是唯一 uncertain",
          RA.SSH_OUTCOME_UNCERTAIN == (RA.SSH_COMMAND_TIMEOUT,)
          and RA.GUARD_EXIT_TIMEOUT == 41)


def test_R7_restore_responsibility():
    print("\n[R7] restore responsibility 只有確認恢復後才可清除")
    rv = RA.RestoreVerifier(MIN_OBS, 120.0)
    st = None
    for m in MARKS_ADVANCING:
        st = rv.feed(probe(), m)
    check(f"★★ R7a. runtime 健康 + marks 前進 → {st}", st == RA.RV_RESUMED)

    rv2 = RA.RestoreVerifier(MIN_OBS, 120.0)
    for m in MARKS_FLAT:
        st2 = rv2.feed(probe(), m)
    check(f"★★ R7b. marks 未前進 → {st2}（不得清除責任）",
          st2 == RA.RV_FAILED)

    rv3 = RA.RestoreVerifier(MIN_OBS, 120.0)
    for _ in range(MIN_OBS):
        st3 = rv3.feed(probe(), None)
    check(f"★★ R7c. loop 證據取不到 → {st3}（不得 PASS）",
          st3 not in (RA.RV_RESUMED,))
    check("★★ R7d. RestoreVerifier 與 recover 共用同一個 marks 判定",
          "loop_active_from_marks" in
          io.open(RA.__file__, encoding="utf-8").read()
          .split("class RestoreVerifier")[1])
    check("★★ R7e. min_loop_observations < 2 直接拒絕（單點不能證明前進）",
          _raises(lambda: RA.RestoreVerifier(1, 120.0)))


def _raises(fn):
    try:
        fn()
    except (ValueError, TypeError):
        return True
    return False


# ======================================================================
# R8 —— 不得使用 status
# ======================================================================
def test_R8_no_status_in_recovery():
    print("\n[R8] recovery 證據不得使用 status（stdin write）")
    def _body_tokens(path, fn_name):
        """函式本體用到的識別字與字串常數（**排除註解與 docstring**）。

        🔴 純文字比對會把「不得使用 status」這種說明算成命中（假陽性），
           因此改掃 AST。
        """
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        target = [n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name][0]
        doc = ast.get_docstring(target)
        out = set()
        for x in ast.walk(target):
            if (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)
                    and x.value.value == doc):
                continue
            if isinstance(x, ast.Name):
                out.add(x.id)
            elif isinstance(x, ast.Attribute):
                out.add(x.attr)
            elif isinstance(x, ast.keyword) and x.arg:
                out.add(x.arg)
            elif isinstance(x, ast.Constant) and isinstance(x.value, str):
                if x.value != doc:
                    out.add(x.value)
        return out

    forbidden = {"status", "send_status", "status_injected"}
    t1 = _body_tokens(HO.__file__, "_resolve_external_running")
    check(f"★★ R8a. _resolve_external_running 本體無 status："
          f"命中={sorted(t1 & forbidden)}", not (t1 & forbidden))

    t2 = _body_tokens(US.__file__, "boot") | _body_tokens(
        US.__file__, "_collect_loop_marks")
    check(f"★★ R8b. boot() / _collect_loop_marks() 無 status："
          f"命中={sorted(t2 & forbidden)}", not (t2 & forbidden))

    us_src = io.open(US.__file__, encoding="utf-8").read()
    boot = us_src.split("    def boot(self):")[1].split("\n    def ")[0]
    collect = us_src.split("    def _collect_loop_marks(self):")[1] \
                    .split("\n    def ")[0]
    check("★★ R8c. 證據來源只有 probe 與 loop_mark_source",
          "probe" in boot and "loop_marks" in boot
          and "loop_mark_source" in collect)

    ra_src = io.open(RA.__file__, encoding="utf-8").read()
    liveness = ra_src.split("def external_liveness(")[1].split("\n\ndef ")[0]
    check("★★ R8d. external_liveness 不觸碰任何 transport / 動詞",
          not any(t in liveness for t in ("SshTransport", "run(", "status",
                                          "subprocess", "ssh")))
    check("★★ R8e. status 仍在 guard allowlist（本輪未改 B2 allowlist）",
          "status" in RA.GUARD_ALLOWED_VERBS
          and RA.CONTROL_VERBS == ("pause", "restore"))


# ======================================================================
# R9 —— zero-I/O
# ======================================================================
def test_R9_zero_io():
    print("\n[R9] zero-I/O：全程 fake，未接觸真實 SSH")
    ra_tree = ast.parse(io.open(RA.__file__, encoding="utf-8").read())
    imports = set()
    for n in ast.walk(ra_tree):
        if isinstance(n, ast.Import):
            imports |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imports.add(n.module.split(".")[0])
    check(f"★★ R9a. remote adapter 未 import subprocess/paramiko/requests："
          f"{sorted(imports & {'subprocess', 'paramiko', 'requests', 'socket'})}",
          not (imports & {"subprocess", "paramiko", "requests", "socket"}))
    check("★★ R9b. 本檔未 import 任何 SSH / 網路模組",
          not ({n.names[0].name for n in ast.walk(
              ast.parse(io.open(__file__, encoding="utf-8").read()))
              if isinstance(n, ast.Import)}
              & {"subprocess", "socket", "paramiko", "requests",
                 "phase6_ssh_timing_probe"}))
    check("★★ R9c. production transport 仍未武裝（armed=False、runner 未注入）",
          RA.SshTransport().can_run("pause")[0] is False
          and RA.SshTransport().armed is False)
    t = RA.SshTimeouts()
    check("★★ R9d. pause / restore timeout 仍為 None / STRUCTURAL_CANDIDATE",
          t.command_timeouts["pause"] is None
          and t.command_timeouts["restore"] is None
          and RA.SSH_TIMEOUT_STATUS["pause"]
          == RA.TIMEOUT_STRUCTURAL_CANDIDATE)
    check(f"  R9e. 結構候選值仍為 {RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC}"
          f"（FIELD_VALIDATION_CANDIDATE，非 FINAL）",
          RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC
          == {"pause": 12.0, "restore": 12.0})


# ======================================================================
# B —— 向下相容：legacy {"running": ...} 語意不得改變
# ======================================================================
def test_B_backward_compatible():
    print("\n[B] legacy {'running': ...} 契約語意不變")
    for running, want in ((True, HO.R_CLEAN),
                          (False, HO.R_PENDING_RESTORE),
                          (None, HO.R_UNKNOWN)):
        got = HO.recover(pending_journal(), {"running": running})[0]
        check(f"★★ B. running={running!r} → {got}", got == want)
    check("★★ B4. 無 evidence（None）→ R_UNKNOWN",
          HO.recover(pending_journal(), None)[0] == HO.R_UNKNOWN)
    check("★★ B5. 無既往紀錄仍為 R_CLEAN",
          HO.recover(FakeJournal())[0] == HO.R_CLEAN)
    check("★★ B6. legacy 明確值優先於新證據（呼叫端已自行判定時沿用）",
          HO.recover(pending_journal(),
                     dict(evidence(probe(), MARKS_FLAT), running=True))[0]
          == HO.R_CLEAN)


# ======================================================================
def main():
    for fn in (test_R1_running_confirmed, test_R2_not_running,
               test_R3_loop_unavailable, test_R4_identity_mismatch,
               test_R5_screen_ambiguous, test_R6_no_resend_after_unknown,
               test_R7_restore_responsibility, test_R8_no_status_in_recovery,
               test_R9_zero_io, test_B_backward_compatible):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
