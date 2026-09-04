# -*- coding: utf-8 -*-
"""
test_phase6_610a_remote_adapter.py — Phase 6.10-A Remote Adapter（1~12）
======================================================================
核心命題
    「external controller 的內部 `running` 變數讀不到，因此
      **不得宣稱** running==False；只能以外部可觀測事實、且**跨多筆樣本**
      證明它已經不再控制 PCS。restore 也一樣 —— 證不出來就標 UNKNOWN，
      不得假裝 PASS。」

是否需要設備
    **不需要**。probe 解析、quiescent 判定、restore 判定、guard allowlist
    全為純函式；sender 一律未武裝。零 SSH、零實機 command。

用法
    python test_phase6_610a_remote_adapter.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA                 # noqa: E402
import phase6_handoff_orchestrator as HO           # noqa: E402

RESULTS = []
_TMP = []
IDLE_BASE, TOL = -1.3, 1.25


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def sample(**over):
    s = {"ac_kw": -1.3, "pcs_state": "STANDBY", "authority": "IDLE",
         "comm_ok": True, "fault": False, "critical_alarms": 0}
    s.update(over)
    return s


def tmp_journal():
    d = tempfile.mkdtemp(prefix="p610a_")
    _TMP.append(d)
    return HO.Journal(path=os.path.join(d, "j.jsonl"))


# ======================================================================
# 1. status injection 的 source-safe 證明
# ======================================================================
def test_1():
    print("\n[1] status injection —— source-safe 與非互動")
    cmd = RA.build_stuff_command(RA.CMD_STATUS)
    check(f"注入指令為 screen -X stuff（{cmd}）",
          "-X stuff" in cmd and "screen -S auto" in cmd)
    check("★★ 不含 attach 形式的指令",
          not any(t in cmd for t in ("screen -r", "screen -x ", "screen -RR")))
    check("★★ 換行為 ANSI-C 引號的真換行（否則該行不會被提交）",
          r"$'status\n'" in cmd)
    for verb in ("stop", "start"):
        c = RA.build_stuff_command(verb)
        check(f"  {verb} 亦使用同一機制", "-X stuff" in c and verb in c)
    # 三個動詞彼此不可混淆
    check("★★ status 不會被誤判為 stop（精確字串）",
          RA.CMD_STATUS != RA.CMD_PAUSE and RA.CMD_STATUS not in (RA.CMD_PAUSE,))
    src = io.open(os.path.join(HERE, "phase6_remote_adapter.py"),
                  encoding="utf-8").read()
    check("★★ 模組記錄了『指令迴圈無 else 分支 → 殘缺注入不觸發動作』的依據",
          "else 分支" in src)


# ======================================================================
# 2. probe：資料缺漏 → UNKNOWN，不臆測
# ======================================================================
def test_2():
    print("\n[2] probe —— UNKNOWN 優先於猜測")
    p = RA.parse_probe("1", "1140 python3 auto_control.py",
                       cwd_line="/home/etica/ems")
    check("完整輸入 → known", p.known)
    check("★★ 且 healthy", p.healthy)
    check(f"  pid 解析正確（{p.pid}）", p.pid == 1140)
    check("  身分比對通過", p.process_identity_ok is True)

    p2 = RA.parse_probe(None, "1140 python3 auto_control.py")
    check("★★ screen 資訊缺漏 → known=False（UNKNOWN）", not p2.known)
    check("★★ 且 healthy=False（不得當成通過）", not p2.healthy)

    p3 = RA.parse_probe("1", None)
    check("★★ 行程資訊缺漏 → known=False", not p3.known)

    p4 = RA.parse_probe("0", "")
    check("screen 數 0 → screen_alive=False", p4.screen_alive is False)
    check("pgrep 空 → process_alive=False", p4.process_alive is False)
    check("  兩者皆已知 → known=True 但 healthy=False",
          p4.known and not p4.healthy)

    p5 = RA.parse_probe("1", "1140 python3 something_else.py")
    check("★★ 行程名稱不符 → identity 不通過（不因 PID 存在就放行）",
          p5.process_identity_ok is False and not p5.healthy)

    p6 = RA.parse_probe("1", "1140 python3 auto_control.py",
                        cwd_line="/tmp/evil")
    check("★★ cwd 不符 → identity 不通過", p6.process_identity_ok is False)

    p7 = RA.parse_probe("not-a-number", "1140 python3 auto_control.py")
    check("★★ screen 數無法解析 → None（UNKNOWN），不猜 0 也不猜 1",
          p7.screen_alive is None and not p7.known)


# ======================================================================
# 3. external_quiescent —— 多樣本
# ======================================================================
def test_3():
    print("\n[3] external_quiescent —— 必須多樣本，不是單點")
    ok, ch, d = RA.external_quiescent([sample()] * 5, IDLE_BASE, TOL,
                                      min_samples=5)
    check(f"★★ 5 筆皆靜止 → PASS（{len(ch)} 項）", ok)
    ok2, _, d2 = RA.external_quiescent([sample()] * 2, IDLE_BASE, TOL,
                                       min_samples=5)
    check(f"★★ 樣本數不足 → FAIL（{d2}）", not ok2)
    ok3, _, d3 = RA.external_quiescent([sample()] * 5, IDLE_BASE, TOL,
                                       min_samples=None)
    check(f"★★ min_samples 未提供 → Fail Closed（{d3}）", not ok3)
    ok4, _, _ = RA.external_quiescent([], IDLE_BASE, TOL, min_samples=1)
    check("空樣本 → FAIL", not ok4)
    check("檢查項共 7 項", len(RA.QUIESCENT_ITEMS) == 7)


# ======================================================================
# 4. 單一瞬時非 idle 樣本 → 整體 FAIL
# ======================================================================
def test_4():
    print("\n[4] 單一瞬時異常樣本即否決（不因多數正常而放行）")
    cases = {
        "power_converged": sample(ac_kw=-80.0),
        "pcs_legal_idle": sample(pcs_state="CHARGING"),
        "no_external_authority": sample(authority="EXTERNAL_OR_UNKNOWN"),
        "comm_ok": sample(comm_ok=False),
        "fault_clear": sample(fault=True),
        "alarm_clear": sample(critical_alarms=1),
    }
    for item, bad in cases.items():
        samples = [sample()] * 4 + [bad] + [sample()] * 4   # 9 筆中僅 1 筆異常
        ok, ch, _ = RA.external_quiescent(samples, IDLE_BASE, TOL, min_samples=5)
        check(f"★★ 9 筆中 1 筆 {item} 異常 → 整體 FAIL",
              not ok and ch[item] is False)
    # 狀態在窗口內變動 = 仍有人下指令
    mixed = [sample(pcs_state="STANDBY")] * 3 + [sample(pcs_state="STOPPED")] * 3
    ok2, ch2, _ = RA.external_quiescent(mixed, IDLE_BASE, TOL, min_samples=5)
    check("★★ 觀測窗口內 PCS 狀態變動 → state_stable FAIL",
          not ok2 and ch2["state_stable"] is False)


# ======================================================================
# 5. restore：runtime 健康但 policy 判定 idle → 仍可驗證 loop resumed
# ======================================================================
def test_5():
    print("\n[5] restore —— 不強迫 PCS 出力")
    p = RA.parse_probe("1", "1140 python3 auto_control.py",
                       cwd_line="/home/etica/ems")
    v, ch = RA.verify_restore(p, loop_active=True)
    check("★★ runtime 健康 ＋ loop 活動 → RESUMED（未要求 PCS 出力）",
          v == RA.RESTORE_RESUMED)
    check("  檢查項含 loop_active", ch.get("loop_active") is True)
    # loop 活動的證據來源：兩次 hardcopy 的時間戳前進
    a = "2026-09-01 13:08:44 meter 92.7\n2026-09-01 13:08:45 meter 92.8"
    b = "2026-09-01 13:08:46 meter 92.6\n2026-09-01 13:08:47 meter 92.9"
    check("★★ 時間戳前進 → loop_active=True",
          RA.loop_active_from_hardcopies(a, b) is True)
    check("★★ 時間戳未前進 → loop_active=False",
          RA.loop_active_from_hardcopies(b, b) is False)


# ======================================================================
# 6. restore 證據不足 → UNKNOWN，不得假裝 PASS
# ======================================================================
def test_6():
    print("\n[6] restore 證據不足 → NEEDS_ADDITIONAL_PROBE")
    p = RA.parse_probe("1", "1140 python3 auto_control.py",
                       cwd_line="/home/etica/ems")
    v, _ = RA.verify_restore(p, loop_active=None)
    check("★★ loop 無法判定 → NEEDS_ADDITIONAL_PROBE（不是 PASS）",
          v == RA.RESTORE_UNKNOWN)
    check("  且不等於 RESUMED", v != RA.RESTORE_RESUMED)
    v2, _ = RA.verify_restore(p, loop_active=False)
    check("loop 確定沒動 → FAILED", v2 == RA.RESTORE_FAILED)
    dead = RA.parse_probe("0", "")
    v3, _ = RA.verify_restore(dead, loop_active=True)
    check("★★ runtime 不健康 → FAILED（不看 B 層）", v3 == RA.RESTORE_FAILED)
    for txt in ((None, "x"), ("x", None), ("no ts", "no ts")):
        check(f"  hardcopy 缺漏/無時間戳 → None（UNKNOWN）",
              RA.loop_active_from_hardcopies(*txt) is None)


# ======================================================================
# 7. Remote guard allowlist
# ======================================================================
def test_7():
    print("\n[7] Remote guard —— 精確白名單，拒絕任意指令")
    for v in RA.GUARD_ALLOWED_VERBS:
        check(f"允許 {v!r}", RA.guard_would_accept(v))
    for bad in ("rm -rf /", "pause; rm -rf /", "bash", "ls", "kill 1140",
                "screen -X quit", "probe extra", "PAUSE", " pause",
                "pause\n", "restore;start", "", "cat /etc/shadow"):
        check(f"★★ 拒絕 {bad!r}", not RA.guard_would_accept(bad))
    sh = RA.REMOTE_GUARD_SH
    check("wrapper 使用 SSH_ORIGINAL_COMMAND", "SSH_ORIGINAL_COMMAND" in sh)
    # 斷言更新（Phase 6.10-A1）：guard 強化後 exit code 改用具名變數，
    #   `exit 42` 字面值不再出現。改為驗證「常數已定義 ＋ 分支確實使用它」，
    #   比原本的字面值比對更精確，不是放寬。
    check("★★ wrapper 有 default 拒絕分支並以非 0 結束",
          "REFUSED" in sh
          and f"EX_REFUSED={RA.GUARD_EXIT_REFUSED}" in sh
          and "exit $EX_REFUSED" in sh
          and RA.GUARD_EXIT_REFUSED != 0)
    check("★★ wrapper 不含 kill / rm", "kill" not in sh and "rm " not in sh)
    check("★★ wrapper 不含 screen -X quit", "quit" not in sh)
    check("設計為 forced command（authorized_keys command=…）",
          'command="/home/etica/ems/phase6_remote_guard.sh"' in sh)


# ======================================================================
# 8/9/10. sender 呼叫數
# ======================================================================
def test_8_9_10():
    print("\n[8/9/10] sender —— 未武裝時呼叫數恆為 0")
    hits = []
    s = RA.RemoteSenders(runner=(lambda c: hits.append(c) or True), armed=False)
    check("預設未武裝", s.armed is False)
    check("★★ 未武裝：pause 回 False", s.pause() is False)
    check("★★ 未武裝：restore 回 False", s.restore() is False)
    check("★★ runner 完全未被呼叫", hits == [])

    # DRY_RUN / OBSERVE：orchestrator 層再擋一次
    for mode in (HO.MODE_DRY_RUN, HO.MODE_OBSERVE):
        hits2 = []
        s2 = RA.RemoteSenders(runner=(lambda c: hits2.append(c) or True),
                              armed=True)
        o = HO.HandoffOrchestrator(
            remote=HO.RemoteController(pause_sender=s2.pause,
                                       restore_sender=s2.restore),
            observe=(lambda: {}), gates=(lambda x: (True, {})),
            journal=tmp_journal(), mode=mode,
            idle_baseline_kw=IDLE_BASE, power_tolerance_kw=TOL)
        o._to(HO.S_PREFLIGHT)
        o.request_pause()
        o.request_restore()
        check(f"★★ {mode}：即使 sender 已武裝，orchestrator 仍不送（呼叫 0）",
              hits2 == [])

    # ARMED 但 DISPATCH_ENABLED=False → 仍不得進入實機 dispatch
    check("★★ DISPATCH_ENABLED 仍為 False", HO.DISPATCH_ENABLED is False)
    check("  未武裝的 sender 無法被 ARMED 模式繞過",
          RA.RemoteSenders(runner=(lambda c: True), armed=False).pause() is False)
    check("  runner 未注入 → 即使武裝也送不出",
          RA.RemoteSenders(runner=None, armed=True).pause() is False)


# ======================================================================
# 11. crash pending restore
# ======================================================================
def test_11():
    print("\n[11] crash → pending restore 責任不滅")
    j = tmp_journal()
    j.append("STATE", state=HO.S_EXTERNAL_PAUSED)
    v, last, act = HO.recover(j, {"running": False})
    check("★★ 重啟後仍認得恢復責任", v == HO.R_PENDING_RESTORE)
    check("  且指示先恢復", "先恢復" in act)
    v2, _, _ = HO.recover(j, {"running": None})
    check("★★ 遠端不可知 → UNKNOWN，不得開始新交接", v2 == HO.R_UNKNOWN)


# ======================================================================
# 12. BOTH → CRITICAL
# ======================================================================
def test_12():
    print("\n[12] 雙方同時持有 → CRITICAL")
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(), observe=(lambda: {}),
        gates=(lambda x: (True, {})), journal=tmp_journal(),
        idle_baseline_kw=IDLE_BASE, power_tolerance_kw=TOL)
    o._to(HO.S_PREFLIGHT)
    own, ok = o.check_ownership(external_running=True, phase6_holding=True)
    check("★★ ownership=BOTH 且判為不合法", own == HO.OWN_BOTH and ok is False)
    o._to(HO.S_PAUSE_REQUESTED)
    o._to(HO.S_PAUSE_VERIFYING)
    o._to(HO.S_OWNERSHIP_CONFLICT, detail="雙方同時持有")
    check("  可轉入 OWNERSHIP_CONFLICT_CRITICAL",
          o.state == HO.S_OWNERSHIP_CONFLICT)
    check("★★ 屬 critical", o.state in HO.CRITICAL_STATES)
    check("★★ 仍必須歸還（唯一出口是 RESTORE_REQUESTED）",
          HO.TRANSITIONS[HO.S_OWNERSHIP_CONFLICT] == {HO.S_RESTORE_REQUESTED})
    # verify_pause 已改名為 external_quiescent
    ok2, ch = o.verify_pause({"pid_alive": True, "screen_alive": True,
                              "running": False},
                             sample())
    check("★★ verify_pause 使用 external_quiescent（非 controller_not_running）",
          "external_quiescent" in ch and "controller_not_running" not in ch)


def main():
    print("=" * 72)
    print("  Phase 6.10-A Remote Adapter / Probe Wiring（完全離線）")
    print("=" * 72)
    try:
        for fn in (test_1, test_2, test_3, test_4, test_5, test_6,
                   test_7, test_8_9_10, test_11, test_12):
            fn()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    total, bad = len(RESULTS), RESULTS.count(False)
    print("\n" + "=" * 72)
    print(f"  結果：{total - bad}/{total} PASS" + ("" if not bad else f"   ❌ {bad} FAIL"))
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
