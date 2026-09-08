# -*- coding: utf-8 -*-
"""
test_phase6_610c_service.py — Phase 6.10-C Unattended Service（1~20）
======================================================================
核心命題
    「無人值守 service 可以完整演練整個 lifecycle，但在
      NETWORK_IDENTITY_STABILITY 未驗證、guard 仍是 B1、
      DISPATCH_ENABLED=False 的現況下，live handoff **一律 REFUSED**；
      而只要 external 曾被 pause，重啟、crash、shutdown 都不得讓恢復責任消失。」

是否需要設備
    **不需要**。sample / probe / sender / dispatcher / clock / journal /
    liveness 全部注入。零 SSH、零實機 command。

用法
    python test_phase6_610c_service.py        # exit 0 = PASS
"""
import io
import os
import sys
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO         # noqa: E402
import phase6_remote_adapter as RA               # noqa: E402
import phase6_unattended_service as SVC          # noqa: E402

RESULTS = []
_TMP = []
SOC_MAX = 85.0


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p610c_")
    _TMP.append(d)
    return d


def journal(d=None):
    return HO.Journal(path=os.path.join(d or tmpdir(), "j.jsonl"))


def probe(**over):
    p = {"screen_alive": True, "process_alive": True,
         "process_identity_ok": True, "controller_running": True}
    p.update(over)
    return p


def smp(**over):
    s = {"pcs_state": "STANDBY", "ac_kw": -1.3, "authority": "IDLE",
         "comm_ok": True, "fault": False, "critical_alarms": 0, "fresh": True,
         "soc": 5.0, "soc_fresh": True, "meter_fresh": True, "ess_fresh": True,
         "tou": "OFF_PEAK", "decision": "charge",
         "external_behaviour_known": True}
    s.update(over)
    return s


class Senders(object):
    def __init__(self, pause_ok=True, restore_ok=True):
        self.pause_calls = self.restore_calls = 0
        self.pause_ok, self.restore_ok = pause_ok, restore_ok

    def pause(self):
        self.pause_calls += 1
        return self.pause_ok

    def restore(self):
        self.restore_calls += 1
        return self.restore_ok


def guard(d, pid=1111, boot="B1", cmd="phase6", alive=None, mutex=None):
    return SVC.SingleInstanceGuard(
        os.path.join(d, "svc.lock"), pid=pid, boot_id=boot, cmdline=cmd,
        is_alive=(alive or (lambda p: False)), mutex_acquire=mutex)


def service(d=None, mode=HO.MODE_DRY_RUN, samples=None, probes=None,
            senders=None, dispatcher=None, j=None, g=None, marks=None):
    d = d or tmpdir()
    sd = senders if senders is not None else Senders()
    sq, pq = list(samples or [smp()] * 60), list(probes or [probe()] * 60)
    mq = list(marks or ["2026-09-03 03:00:%02d" % i for i in range(60)])

    def nxt(q, dflt):
        return q.pop(0) if q else dflt

    return SVC.UnattendedService(
        journal=j or journal(d), instance_guard=g or guard(d),
        sample_source=(lambda: nxt(sq, smp())),
        probe_source=(lambda: nxt(pq, probe())),
        loop_mark_source=(lambda: nxt(mq, "2026-09-03 04:00:00")),
        senders=sd, dispatcher=dispatcher, mode=mode,
        soc_max_pct=SOC_MAX), sd


# ======================================================================
# 1/2. DRY_RUN / OBSERVE 長迴圈：sender 恆為 0
# ======================================================================
def test_1_2():
    print("\n[1/2] DRY_RUN / OBSERVE —— 長迴圈 sender 恆為 0")
    hits = []
    for mode in (HO.MODE_DRY_RUN, HO.MODE_OBSERVE):
        s, sd = service(mode=mode,
                        dispatcher=(lambda x: hits.append(x) or {}))
        v, why = s.boot()
        check(f"  {mode}：boot = {v}", v == SVC.BOOT_OK)
        s.run(max_ticks=12)
        check(f"★★ {mode}：12 輪後 pause sender = 0", sd.pause_calls == 0)
        check(f"★★ {mode}：12 輪後 restore sender = 0", sd.restore_calls == 0)
        check(f"  {mode}：全部輪次皆 NO_HANDOFF",
              all(r["decision"] == "NO_HANDOFF" for r in s.run(max_ticks=3)))
    check("★★ dispatcher 全程 0 次", hits == [])


# ======================================================================
# 3/4/5. Live gate：三種現況各自擋下
# ======================================================================
def test_3_4_5():
    print("\n[3/4/5] Live gate —— DISPATCH_ENABLED / network / guard 變體")
    allowed, ch = SVC.live_handoff_allowed(
        HO.MODE_ARMED, HO.R_CLEAN, True, probe())
    check("★★ 即使 MODE=ARMED 且其餘寬鬆，live handoff 仍 REFUSED", not allowed)
    check("★★ dispatch_enabled 未過（DISPATCH_ENABLED=False）",
          ch["dispatch_enabled"] is False)
    # B1.7 裁示：ACCEPTED 也放行，因此 gate 更名並改為三態判定
    check("★★ network_identity_acceptable 已放行（ACCEPTED）",
          ch["network_identity_acceptable"] is True)
    check("★★ 但 NETWORK_IDENTITY_STABILITY **不是** PASS",
          SVC.NETWORK_IDENTITY_STABILITY == SVC.NET_ACCEPTED
          and SVC.NETWORK_IDENTITY_STABILITY != SVC.NET_PASS)
    check("★★ 舊的 network_identity_pass 名稱已不存在（避免語意誤讀）",
          "network_identity_pass" not in ch
          and "network_identity_pass" not in SVC.LIVE_GATE_ITEMS)
    check("★★ remote_guard_b2 未過（現場部署為 B1）",
          ch["remote_guard_b2"] is False)
    check("★★ pause_capable / restore_capable 皆 False（依實際部署判定）",
          ch["pause_capable"] is False and ch["restore_capable"] is False)
    check("★★ first_live_prerequisite 未過", ch["first_live_prerequisite"] is False)
    check("  DRY_RUN 下 mode_armed 亦不成立",
          SVC.live_handoff_allowed(HO.MODE_DRY_RUN, HO.R_CLEAN, True,
                                   probe())[1]["mode_armed"] is False)
    # C1 裁示：service_health_healthy 升為第 11 個 gate item
    check("  gate 共 11 項", len(SVC.LIVE_GATE_ITEMS) == 11
          and len(ch) == 11)
    check("★★ service_health_healthy 未帶 health → FAIL（Fail Closed）",
          ch["service_health_healthy"] is False)
    # 事實登記不得被誤寫
    check("★★ NETWORK_IDENTITY_STABILITY 常數為 ACCEPTED（不是 PASS）",
          SVC.NETWORK_IDENTITY_STABILITY == SVC.NET_ACCEPTED
          and SVC.NETWORK_IDENTITY_STABILITY != SVC.NET_PASS)
    check("★★ DHCP Reservation 仍為 NOT_VERIFIED（未被 ACCEPTED 蓋掉）",
          SVC.DHCP_RESERVATION_VERIFIED is False
          and SVC.DHCP_RESERVATION_STATUS == "NOT_VERIFIED")
    check("★★ DEPLOYED_GUARD_VARIANT 為 B2（現場已部署並唯讀驗證）",
          RA.DEPLOYED_GUARD_VARIANT == "B2")
    check("★★ 但 FIRST LIVE 仍 HOLD：first_live_prerequisite 未過",
          SVC.FIRST_LIVE_PREREQUISITE_SATISFIED is False)
    src = io.open(os.path.join(HERE, "phase6_unattended_service.py"),
                  encoding="utf-8").read()
    check("★★ 原始碼中不存在把 network identity 寫成 PASS 的指派",
          "NETWORK_IDENTITY_STABILITY = NET_PASS" not in src)


# ======================================================================
# 6/7. recovery UNKNOWN / PENDING_RESTORE
# ======================================================================
def test_6_7():
    print("\n[6/7] Startup recovery —— UNKNOWN / PENDING_RESTORE 皆拒絕新 handoff")
    d = tmpdir()
    j = journal(d)
    j.append("STATE", state=HO.S_EXTERNAL_PAUSED)
    s, sd = service(d=d, j=j, probes=[probe(controller_running=None)] * 10)
    v, why = s.boot()
    check("★★ 遠端狀態不可知 → boot REFUSED", v == SVC.BOOT_REFUSED)
    check(f"  原因為 RECOVERY_UNKNOWN（{why}）", "UNKNOWN" in why)
    check("  critical = RECOVERY_UNKNOWN",
          s.critical == SVC.CRIT_RECOVERY_UNKNOWN)

    d2 = tmpdir()
    j2 = journal(d2)
    j2.append("STATE", state=HO.S_EXTERNAL_PAUSED)
    s2, sd2 = service(d=d2, j=j2,
                      probes=[probe(controller_running=False)] * 10)
    v2, why2 = s2.boot()
    check("★★ external 仍暫停 → boot REFUSED", v2 == SVC.BOOT_REFUSED)
    check("★★ 重啟後**承接**恢復責任（未被重設為 IDLE）",
          s2.restore_responsibility is True)
    check("  不得進入正常 loop", "PENDING_RESTORE" in why2)

    d3 = tmpdir()
    j3 = journal(d3)
    j3.append("STATE", state=HO.S_RESTORE_FAILED)
    s3, _ = service(d=d3, j=j3)
    check("★★ 上次 CRITICAL 收場 → 拒絕啟動",
          s3.boot()[0] == SVC.BOOT_REFUSED)


# ======================================================================
# 8. Single instance
# ======================================================================
def test_8():
    print("\n[8] Single instance —— 不只靠 PID file")
    d = tmpdir()
    g1 = guard(d, pid=1111, alive=lambda p: True)
    check("第一個 instance 取得", g1.acquire()[0])
    g2 = guard(d, pid=2222, alive=lambda p: True)
    ok2, why2 = g2.acquire()
    check("★★ 第二個 instance 被拒", not ok2 and why2 == "ANOTHER_INSTANCE_RUNNING")

    # stale：前一個行程已死
    g3 = guard(d, pid=3333, alive=lambda p: False)
    check("★★ 前行程已死（stale lock）→ 可接手", g3.acquire()[0])

    # PID 被回收：活著但不是我們的程式
    d4 = tmpdir()
    guard(d4, pid=1111, cmd="phase6", alive=lambda p: True).acquire()
    g5 = SVC.SingleInstanceGuard(os.path.join(d4, "svc.lock"), pid=4444,
                                 boot_id="B1", cmdline="phase6",
                                 is_alive=lambda p: True)
    check("  同 boot 同 cmdline 且存活 → 拒絕", not g5.acquire()[0])
    g6 = SVC.SingleInstanceGuard(os.path.join(d4, "svc.lock"), pid=4444,
                                 boot_id="B1", cmdline="phase6-other",
                                 is_alive=lambda p: True)
    check("★★ PID 存活但 cmdline 不符（PID 被回收）→ 視為 stale，可接手",
          g6.acquire()[0])

    # 死活不可知 → Fail Closed
    d7 = tmpdir()
    guard(d7, pid=1111, alive=lambda p: True).acquire()
    g8 = guard(d7, pid=5555, alive=lambda p: None)
    ok8, why8 = g8.acquire()
    check("★★ 對方死活不可知 → 拒絕啟動（不假設已死）",
          not ok8 and why8 == "PEER_LIVENESS_UNKNOWN")

    # lock 檔損毀 → Fail Closed
    d9 = tmpdir()
    io.open(os.path.join(d9, "svc.lock"), "w").write("{broken")
    ok9, why9 = guard(d9, alive=lambda p: False).acquire()
    check("★★ lock 檔損毀 → 拒絕啟動", not ok9 and why9 == "LOCK_FILE_CORRUPT")

    # Named Mutex 注入（production 應採此路徑）
    check("  Named Mutex 被他人持有 → 拒絕",
          not guard(tmpdir(), mutex=lambda: False).acquire()[0])
    check("  Named Mutex 取得 → 允許",
          guard(tmpdir(), mutex=lambda: True).acquire()[0])

    # 第二 instance 不得寫 SERVICE_START
    d10 = tmpdir()
    guard(d10, pid=1111, alive=lambda p: True).acquire()
    j10 = journal(d10)
    s10, _ = service(d=d10, j=j10,
                     g=guard(d10, pid=2222, alive=lambda p: True))
    v10, _ = s10.boot()
    check("★★ 第二 instance boot REFUSED", v10 == SVC.BOOT_REFUSED)
    check("★★ 且未寫入 SERVICE_START",
          not any(r["kind"] == SVC.EV_SERVICE_START for r in j10.read_all()))


# ======================================================================
# 9/10. Graceful shutdown
# ======================================================================
def test_9_10():
    print("\n[9/10] Graceful shutdown")
    s, sd = service()
    s.boot()
    v, why = s.shutdown()
    check("無恢復責任 → CLEAN shutdown", v == SVC.SHUTDOWN_CLEAN)
    check("  已寫入 SERVICE_SHUTDOWN",
          any(r["kind"] == SVC.EV_SERVICE_SHUTDOWN for r in s.journal.read_all()))

    # 有恢復責任 → 必須先歸還
    s2, sd2 = service(mode=HO.MODE_ARMED)
    s2.boot()
    s2.restore_responsibility = True
    v2, why2 = s2.shutdown()
    check("★★ 有恢復責任 → shutdown 先送 restore", sd2.restore_calls == 1)
    check("  歸還並確認 loop 恢復 → CLEAN", v2 == SVC.SHUTDOWN_CLEAN)
    check("  責任已解除", s2.restore_responsibility is False)

    # 歸還失敗 → 不得記成 clean
    s3, sd3 = service(mode=HO.MODE_ARMED, senders=Senders(restore_ok=False))
    s3.boot()
    s3.restore_responsibility = True
    v3, _ = s3.shutdown()
    check("★★ 歸還失敗 → RESTORE_FAILED_CRITICAL（不得記成 clean）",
          v3 == SVC.SHUTDOWN_CRITICAL)
    check("  critical 已記錄", s3.critical == SVC.CRIT_RESTORE_FAILED)
    check("  journal 有 CRITICAL_FAILURE",
          any(r["kind"] == SVC.EV_CRITICAL for r in s3.journal.read_all()))

    # 歸還送出但 loop 未恢復
    s4, sd4 = service(mode=HO.MODE_ARMED,
                      marks=["2026-09-03 03:00:00"] * 10)
    s4.boot()
    s4.restore_responsibility = True
    check("★★ 歸還後 loop 未恢復 → CRITICAL",
          s4.shutdown()[0] == SVC.SHUTDOWN_CRITICAL)

    # DRY_RUN 下有責任卻無法送出 → 同樣 CRITICAL，不得假裝乾淨
    s5, sd5 = service(mode=HO.MODE_DRY_RUN)
    s5.boot()
    s5.restore_responsibility = True
    check("★★ DRY_RUN 有責任但送不出 → CRITICAL（不得假裝 clean）",
          s5.shutdown()[0] == SVC.SHUTDOWN_CRITICAL)
    check("  且 sender 未被呼叫", sd5.restore_calls == 0)


# ======================================================================
# 11~14 / 20. Crash recovery 八個切點
# ======================================================================
def test_11_14_20():
    print("\n[11~14/20] Crash recovery —— 八個切點皆須先 recover")
    points = [
        ("pause INTENT 後", [("INTENT", None, "PAUSE")]),
        ("pause success 後", [("INTENT", None, "PAUSE"),
                              ("OUTCOME", None, "PAUSE"),
                              ("STATE", HO.S_PAUSE_REQUESTED, None)]),
        ("SETTLING", [("STATE", HO.S_PAUSE_VERIFYING, None)]),
        ("STABLE_VERIFY", [("STATE", HO.S_PAUSE_VERIFYING, None)]),
        ("PHASE6_ACTIVE", [("STATE", HO.S_PHASE6_ACTIVE, None)]),
        ("PHASE6_RELEASED", [("STATE", HO.S_PHASE6_RELEASED, None)]),
        ("restore INTENT 後", [("STATE", HO.S_RESTORE_REQUESTED, None),
                               ("INTENT", None, "RESTORE")]),
        ("restore outcome 未落盤", [("STATE", HO.S_RESTORE_VERIFYING, None)]),
    ]
    for tag, evs in points:
        d = tmpdir()
        j = journal(d)
        for kind, st, step in evs:
            j.append(kind, state=st, step=step)
        s, sd = service(d=d, j=j, mode=HO.MODE_ARMED,
                        probes=[probe(controller_running=False)] * 10)
        v, why = s.boot()
        check(f"★★ crash 於「{tag}」→ boot REFUSED 並承接責任",
              v == SVC.BOOT_REFUSED and s.restore_responsibility is True)
        check(f"  「{tag}」未盲目重送 pause / restore",
              sd.pause_calls == 0 and sd.restore_calls == 0)

    # 20. service restart 不清除責任（journal 跨重啟）
    d = tmpdir()
    j = journal(d)
    j.append("STATE", state=HO.S_EXTERNAL_PAUSED)
    for i in range(3):
        j2 = HO.Journal(path=j.path)          # 模擬重啟後重新開啟同一份 journal
        s, sd = service(d=d, j=j2, probes=[probe(controller_running=False)] * 5)
        v, _ = s.boot()
        check(f"★★ 第 {i+1} 次重啟仍承接恢復責任",
              v == SVC.BOOT_REFUSED and s.restore_responsibility is True)


# ======================================================================
# 15. ownership BOTH → CRITICAL
# ======================================================================
def test_15():
    print("\n[15] ownership BOTH → CRITICAL")
    check("BOTH 被分類為不變量破壞",
          HO.classify_ownership(True, True) == HO.OWN_BOTH)
    check("★★ BOTH 不在任何狀態的可接受集合",
          not any(HO.OWN_BOTH in v for v in HO.ACCEPTABLE_OWNERSHIP.values()))
    check("★★ CRITICAL_CONDITIONS 涵蓋四種",
          set(SVC.CRITICAL_CONDITIONS) == {
              HO.S_OWNERSHIP_CONFLICT, HO.S_RESTORE_FAILED,
              SVC.CRIT_RECOVERY_UNKNOWN, SVC.CRIT_REMOTE_IDENTITY_MISMATCH})
    s, _ = service()
    s.boot()
    s.critical = SVC.CRIT_OWNERSHIP_CONFLICT
    check("★★ critical 期間迴圈不再前進", s.run(max_ticks=5) == [])


# ======================================================================
# 16/17/18. 資料不 fresh / TOU / SOC 不符 → 不 handoff
# ======================================================================
def test_16_17_18():
    print("\n[16/17/18] eligibility —— stale / TOU / SOC")
    cases = {
        "meter 不 fresh": smp(meter_fresh=False),
        "ESS 不 fresh": smp(ess_fresh=False),
        "樣本 stale": smp(fresh=False),
        "TOU=PEAK": smp(tou="PEAK"),
        "SOC 超過充電帶": smp(soc=90.0),
        "Decision 非 charge": smp(decision="idle"),
        "通訊異常": smp(comm_ok=False),
        "有故障": smp(fault=True),
        "有嚴重告警": smp(critical_alarms=1),
    }
    for tag, bad in cases.items():
        ok, ch = SVC.evaluate_eligibility(bad, probe(), SOC_MAX)
        check(f"  {tag} → 不 eligible", not ok)
    check("★★ remote probe 不健康 → 不 eligible",
          not SVC.evaluate_eligibility(smp(), probe(process_alive=False),
                                       SOC_MAX)[0])
    check("★★ soc_max_pct 未提供 → Fail Closed",
          not SVC.evaluate_eligibility(smp(), probe(), None)[0])
    check("乾淨樣本 → eligible",
          SVC.evaluate_eligibility(smp(), probe(), SOC_MAX)[0])
    # 但 eligible 不等於 allowed
    s, sd = service(mode=HO.MODE_ARMED)
    s.boot()
    r = s.tick()
    check("★★ eligible 但 live gate 未過 → 仍 NO_HANDOFF",
          r["eligible"] is True and r["live_allowed"] is False
          and r["decision"] == "NO_HANDOFF")
    check("  且 sender 未被呼叫", sd.pause_calls == 0)


# ======================================================================
# 19. 無硬編碼時刻
# ======================================================================
def test_19():
    print("\n[19] 無硬編碼 00:05 / 00:15 / 00:30")
    import ast
    import re
    for fn in ("phase6_unattended_service.py",
               "phase6_handoff_orchestrator.py"):
        src = io.open(os.path.join(HERE, fn), encoding="utf-8").read()
        tree = ast.parse(src)
        docs = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                dd = ast.get_docstring(n, clean=False)
                if dd:
                    docs.add(dd)
        strs = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value not in docs}
        clocks = sorted(v for v in strs
                        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v))
        check(f"★★ {fn} 程式碼中無硬編碼時刻（命中={clocks}）", not clocks)
    check("★★ decision_interval_sec 沿用既有 production config",
          SVC.ServiceTiming().decision_interval_sec == 30.0)
    t = SVC.ServiceTiming()
    # C1 裁示（2026-09-03）：兩項已核准為 5.0 / 300.0
    check("★★ retry_backoff_sec = 5.0（C1 核准值）", t.retry_backoff_sec == 5.0)
    check("★★ service_health_timeout_sec = 300.0（C1 核准值）",
          t.service_health_timeout_sec == 300.0)
    check("  兩項皆已定案 → missing() 為空", t.missing() == [])
    check("★★ 仍可注入 None 演練未設定路徑（Fail Closed 未被拿掉）",
          SVC.ServiceTiming(retry_backoff_sec=None,
                            service_health_timeout_sec=None).missing()
          == list(SVC.REQUIRED_TIMING_FIELDS))
    check("★★ 參數定案不等於 live 啟用：DISPATCH_ENABLED 仍為 False",
          HO.DISPATCH_ENABLED is False)


# ======================================================================
# 14(E2E). DRY_RUN 端到端
# ======================================================================
def test_e2e():
    print("\n[E2E] DRY_RUN 端到端 —— 狀態機／稽核／recovery 完整演練")
    d = tmpdir()
    j = journal(d)
    s, sd = service(d=d, j=j, mode=HO.MODE_DRY_RUN)
    v, _ = s.boot()
    check("boot READY", v == SVC.BOOT_OK)
    s.run(max_ticks=5)
    sv, _ = s.shutdown()
    check("shutdown CLEAN", sv == SVC.SHUTDOWN_CLEAN)
    kinds = {r["kind"] for r in j.read_all()}
    for ev in (SVC.EV_SERVICE_START, SVC.EV_RECOVERY, SVC.EV_ELIGIBILITY,
               SVC.EV_SERVICE_SHUTDOWN):
        check(f"  稽核含 {ev}", ev in kinds)
    check("★★ 全程 pause sender = 0", sd.pause_calls == 0)
    check("★★ 全程 restore sender = 0", sd.restore_calls == 0)
    recs = j.read_all()
    seqs = [r["seq"] for r in recs]
    check("★★ journal seq 單調遞增且不重複",
          seqs == sorted(seqs) and len(set(seqs)) == len(seqs))
    check("★★ journal 跨重啟不清空",
          len(HO.Journal(path=j.path).read_all()) == len(recs))
    check("  必要事件詞彙已定義 8 種",
          len(SVC.REQUIRED_JOURNAL_EVENTS) == 8)


def main():
    print("=" * 72)
    print("  Phase 6.10-C Unattended Service（完全離線，零實機 command）")
    print("=" * 72)
    try:
        for fn in (test_1_2, test_3_4_5, test_6_7, test_8, test_9_10,
                   test_11_14_20, test_15, test_16_17_18, test_19, test_e2e):
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
