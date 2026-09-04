# -*- coding: utf-8 -*-
"""
test_phase6_610_handoff.py — Phase 6.10 Unattended Ownership Handoff（A~J）
======================================================================
核心命題
    「Phase 6 與 external controller **永遠不得同時持有控制權**；
      只要曾經暫停過 external，就永遠背負恢復責任 ——
      重開機也不能讓這個責任消失；restore 失敗是 critical，不是一般錯誤。」

是否需要設備
    **不需要**。remote / observe / gates / dispatcher / clock / journal
    全部注入，零網路、零 SSH、零實機 command。

用法
    python test_phase6_610_handoff.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import json
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO          # noqa: E402

RESULTS = []
_TMP = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmp_journal():
    d = tempfile.mkdtemp(prefix="p610_")
    _TMP.append(d)
    return HO.Journal(path=os.path.join(d, "j.jsonl"))


# ======================================================================
# 測試替身
# ======================================================================
class FakeRemote(HO.RemoteController):
    """external controller 替身：狀態可控，且記錄實際被送過幾次指令。"""

    def __init__(self, running=True, alive=True, pause_ok=True,
                 restore_ok=True):
        self.running = running
        self.alive = alive
        self.pause_ok = pause_ok
        self.restore_ok = restore_ok
        self.pause_calls = 0
        self.restore_calls = 0
        HO.RemoteController.__init__(self)

    def probe(self):
        return {"screen_alive": self.alive, "pid_alive": self.alive,
                "running": self.running}

    def send_pause(self):
        self.pause_calls += 1
        if self.pause_ok:
            self.running = False
        return self.pause_ok

    def send_restore(self):
        self.restore_calls += 1
        if self.restore_ok:
            self.running = True
        return self.restore_ok


IDLE_BASE = -1.3          # 現場長期觀測的 idle baseline（測試常數，非 production）
TOL = 1.25                # 沿用 authority_power_tolerance_kw


def obs(**over):
    """Phase 6 側觀測替身，預設為「external 已停、一切乾淨」。"""
    o = {"authority": "IDLE", "pcs_state": "STANDBY", "ac_kw": -1.3,
         "soc": 5.0, "tou": "OFF_PEAK", "meter_valid": True,
         "meter_fresh": True, "comm_ok": True, "fault": False,
         "critical_alarms": 0, "decision": "charge"}
    o.update(over)
    return o


def orch(remote=None, mode=HO.MODE_DRY_RUN, journal=None, watchdog=None,
         observation=None):
    return HO.HandoffOrchestrator(
        remote=remote or FakeRemote(),
        observe=(lambda: observation or obs()),
        gates=(lambda o: (True, {})),
        journal=journal, watchdog=watchdog, mode=mode,
        idle_baseline_kw=IDLE_BASE, power_tolerance_kw=TOL)


# ======================================================================
# A. State Machine
# ======================================================================
def test_a():
    print("\n[A] State Machine —— 白名單轉移，非法轉移必須拋錯")
    check("初始狀態為 IDLE", orch().state == HO.S_IDLE)
    check("每個狀態都有明確的轉移集合",
          set(HO.TRANSITIONS) >= HO.TERMINAL_STATES | {HO.S_IDLE})
    check("終態沒有任何出口（OWNERSHIP_CONFLICT 除外，需歸還）",
          all(not HO.TRANSITIONS[s] for s in HO.TERMINAL_STATES
              if s != HO.S_OWNERSHIP_CONFLICT))
    o = orch()
    o._to(HO.S_PREFLIGHT)
    check("IDLE → PREFLIGHT 合法", o.state == HO.S_PREFLIGHT)
    for bad in (HO.S_PHASE6_ACTIVE, HO.S_RESTORED, HO.S_COMPLETE):
        try:
            orch()._to(bad)
            ok = False
        except RuntimeError:
            ok = True
        check(f"★★ IDLE → {bad} 為非法轉移，必須拋錯", ok)
    # 完整 happy path
    o = orch()
    for s in (HO.S_PREFLIGHT, HO.S_PAUSE_REQUESTED, HO.S_PAUSE_VERIFYING,
              HO.S_EXTERNAL_PAUSED, HO.S_PHASE6_ACTIVE, HO.S_PHASE6_RELEASING,
              HO.S_PHASE6_RELEASED, HO.S_RESTORE_REQUESTED,
              HO.S_RESTORE_VERIFYING, HO.S_RESTORED, HO.S_COMPLETE):
        o._to(s)
    check("★★ 完整 happy path 11 段轉移皆合法", o.state == HO.S_COMPLETE)
    check("不得從 EXTERNAL_PAUSED 直接跳到 COMPLETE（必須歸還）",
          HO.S_COMPLETE not in HO.TRANSITIONS[HO.S_EXTERNAL_PAUSED])


# ======================================================================
# B. Ownership Handoff State
# ======================================================================
def test_b():
    print("\n[B] Ownership —— 互斥不變量")
    cases = [((True, False), HO.OWN_EXTERNAL), ((False, True), HO.OWN_PHASE6),
             ((False, False), HO.OWN_NEITHER), ((True, True), HO.OWN_BOTH),
             ((None, False), HO.OWN_UNKNOWN), ((True, None), HO.OWN_UNKNOWN),
             ((None, None), HO.OWN_UNKNOWN)]
    for (ext, p6), want in cases:
        check(f"external={ext} phase6={p6} → {want}",
              HO.classify_ownership(ext, p6) == want)
    # 斷言更新（2026-09-02 語意修正）：
    #   OLD  EXPECTED_OWNERSHIP 對每個狀態指定「單一」應有 ownership，
    #        因此 RESTORED / COMPLETE 被硬性要求 == EXTERNAL。
    #   NEW  ACCEPTABLE_OWNERSHIP 改為「可接受集合」——
    #        external 恢復後可能依 policy 正確地 idle（ownership=NEITHER），
    #        那是恢復成功而非失敗。唯一絕對禁止的仍是 BOTH。
    #   證據 : 使用者裁示（restored external controller 可能正確選擇 idle）
    #   why  : 舊斷言會把「正確地不動作」誤判為交接失敗
    check("★★ BOTH 不出現在任何狀態的可接受集合中",
          not any(HO.OWN_BOTH in v for v in HO.ACCEPTABLE_OWNERSHIP.values()))
    check("★★ RESTORED / COMPLETE 接受 NEITHER（external 可能正確地 idle）",
          HO.OWN_NEITHER in HO.ACCEPTABLE_OWNERSHIP[HO.S_RESTORED]
          and HO.OWN_NEITHER in HO.ACCEPTABLE_OWNERSHIP[HO.S_COMPLETE])
    check("  但仍接受 EXTERNAL",
          HO.OWN_EXTERNAL in HO.ACCEPTABLE_OWNERSHIP[HO.S_COMPLETE])
    check("★★ EXTERNAL_PAUSED 只接受 NEITHER（此時誰都不該持有）",
          HO.ACCEPTABLE_OWNERSHIP[HO.S_EXTERNAL_PAUSED] == frozenset({HO.OWN_NEITHER}))
    check("★★ PHASE6_ACTIVE 只接受 PHASE6",
          HO.ACCEPTABLE_OWNERSHIP[HO.S_PHASE6_ACTIVE] == frozenset({HO.OWN_PHASE6}))
    # 偵測到 BOTH → 回報不合法
    o = orch(journal=tmp_journal())
    o._to(HO.S_PREFLIGHT)
    own, ok = o.check_ownership(external_running=True, phase6_holding=True)
    check("★★ 偵測到雙方同時持有 → ownership=BOTH 且判為不合法",
          own == HO.OWN_BOTH and ok is False)
    check("  不變量破壞已寫入 journal",
          any(r["kind"] == "INVARIANT_VIOLATION" for r in o.journal.read_all()))
    # 未知一律 Fail Closed，不猜
    o2 = orch()
    o2._to(HO.S_PREFLIGHT)
    check("任一側無法判定 → UNKNOWN（不猜測）",
          o2.check_ownership(None, False)[0] == HO.OWN_UNKNOWN)


# ======================================================================
# C. Pause / Restore mechanism
# ======================================================================
def test_c():
    print("\n[C] Pause / Restore —— 非互動、看行為不看宣稱")
    src = io.open(os.path.join(HERE, "phase6_handoff_orchestrator.py"),
                  encoding="utf-8").read()
    check("使用 screen -X stuff（非互動）", "screen -X stuff" in src)
    # ⚠️ 不能用 "attach" 這個字判斷 —— 模組自己的禁止性註解就含這個字。
    #    改查實際的 attach 指令形式（screen -r / -x / -RR）。
    check("★★ 不使用 screen attach 指令",
          not any(t in src for t in ("screen -r", "screen -x ", "screen -RR")))
    check("pause / restore 指令為 stop / start",
          HO.PAUSE_COMMAND == "stop" and HO.RESTORE_COMMAND == "start")

    # 功率收斂：不得用 == 0 判定（idle baseline 不是 0）
    check("★★ idle baseline −1.3 視為已收斂", HO.power_is_idle(-1.3, IDLE_BASE, TOL))
    check("★★ −1.4 仍在容差內（現場抖動）", HO.power_is_idle(-1.4, IDLE_BASE, TOL))
    # 🔴 |0.0 − (−1.3)| = 1.3 > 1.25 → **不**視為收斂。
    #    這是正確行為：容差是相對 idle baseline，不是相對 0。
    #    呼叫端必須讓 baseline 與 tolerance 相容；此處刻意不為了好看而放寬。
    check("★★ 0.0 相對 −1.3 baseline 差 1.3 > 容差 1.25 → 不視為收斂",
          not HO.power_is_idle(0.0, IDLE_BASE, TOL))
    check("  baseline 取 0 時，0.0 才算收斂（證明是相對關係）",
          HO.power_is_idle(0.0, 0.0, TOL))
    check("★★ 80 kW 明顯未收斂", not HO.power_is_idle(80.0, IDLE_BASE, TOL))
    check("−80 kW（充電中）未收斂", not HO.power_is_idle(-80.0, IDLE_BASE, TOL))
    for v in (None,):
        check("功率為 None → 不視為收斂（Fail Closed）",
              not HO.power_is_idle(v, IDLE_BASE, TOL))
    check("容差未提供 → Fail Closed", not HO.power_is_idle(-1.3, IDLE_BASE, None))

    # pause 驗證：九項全通過才算 PASS
    o = orch()
    good_probe = {"pid_alive": True, "screen_alive": True, "running": False}
    ok, checks = o.verify_pause(good_probe, obs())
    check(f"★★ 全部條件成立 → pause 驗證 PASS（{len(checks)} 項）", ok)
    broken = {
        "process_alive": ({"pid_alive": False, "screen_alive": True, "running": False}, obs()),
        "screen_alive": ({"pid_alive": True, "screen_alive": False, "running": False}, obs()),
        # 改名申報：controller_not_running → external_quiescent
        #   舊名宣稱「直接觀測到 running == False」，但那是行程內部變數，
        #   外部根本讀不到。新名只主張「行為上已靜止」，語意才成立。
        "external_quiescent": ({"pid_alive": True, "screen_alive": True, "running": True}, obs()),
        "power_converged": (good_probe, obs(ac_kw=-80.0)),
        "pcs_legal_idle": (good_probe, obs(pcs_state="CHARGING")),
        "no_external_authority": (good_probe, obs(authority="EXTERNAL_OR_UNKNOWN")),
        "comm_ok": (good_probe, obs(comm_ok=False)),
        "fault_clear": (good_probe, obs(fault=True)),
        "alarm_clear": (good_probe, obs(critical_alarms=2)),
    }
    for item, (p, ob) in broken.items():
        ok2, ch = o.verify_pause(p, ob)
        check(f"  {item} 不成立 → pause 驗證 FAIL", ok2 is False and ch[item] is False)
    check("★★ STOPPED 與 STANDBY 皆為 legal idle",
          o.verify_pause(good_probe, obs(pcs_state="STOPPED"))[0] is True)

    # restore 驗證
    ok3, _ = o.verify_restore({"pid_alive": True, "screen_alive": True, "running": True})
    check("restore 驗證：三項成立 → PASS", ok3)
    check("restore 後 running 仍為 False → FAIL",
          o.verify_restore({"pid_alive": True, "screen_alive": True,
                            "running": False})[0] is False)


# ======================================================================
# D. Crash Recovery
# ======================================================================
def test_d():
    print("\n[D] Crash Recovery —— 責任不因重開機而消失")
    j = tmp_journal()
    check("無紀錄 → CLEAN", HO.recover(j)[0] == HO.R_CLEAN)

    # 模擬：已 pause，尚未 restore，然後 crash
    j2 = tmp_journal()
    j2.append("STATE", state=HO.S_EXTERNAL_PAUSED)
    v, last, act = HO.recover(j2, {"running": False})
    check("★★ 已暫停未恢復 ＋ 遠端確實停著 → PENDING_RESTORE",
          v == HO.R_PENDING_RESTORE and last == HO.S_EXTERNAL_PAUSED)
    check("  行動指示為「先恢復，不得開始新交接」", "先恢復" in act)

    v2, _, act2 = HO.recover(j2, {"running": True})
    check("★★ 遠端 probe 顯示已在運行 → 責任解除（遠端事實優先於 journal 推測）",
          v2 == HO.R_CLEAN)

    v3, _, act3 = HO.recover(j2, {"running": None})
    check("★★ 遠端狀態不可知 → UNKNOWN，Fail Closed 不得開始新交接",
          v3 == HO.R_UNKNOWN and "不得開始新交接" in act3)

    j3 = tmp_journal()
    j3.append("STATE", state=HO.S_RESTORE_FAILED)
    check("★★ 上次 CRITICAL 收場 → 拒絕啟動，需人工",
          HO.recover(j3, {"running": True})[0] == HO.R_CRITICAL)

    j4 = tmp_journal()
    j4.append("STATE", state=HO.S_COMPLETE)
    check("上次已收斂 → CLEAN", HO.recover(j4, {"running": True})[0] == HO.R_CLEAN)

    # 每一個 PENDING_RESTORE_STATES 都必須被認出來
    bad = []
    for s in HO.PENDING_RESTORE_STATES:
        jx = tmp_journal()
        jx.append("STATE", state=s)
        if HO.recover(jx, {"running": False})[0] != HO.R_PENDING_RESTORE:
            bad.append(s)
    check(f"★★ 全部 {len(HO.PENDING_RESTORE_STATES)} 個待恢復狀態都被認出"
          f"（漏認 {bad}）", not bad)

    # journal 半行損毀不得讓整份失效
    j5 = tmp_journal()
    j5.append("STATE", state=HO.S_EXTERNAL_PAUSED)
    with io.open(j5.path, "a", encoding="utf-8") as f:
        f.write('{"seq": 99, "kind": "STA')      # crash 造成的半行
    check("★★ journal 半行損毀 → 略過該行，其餘仍可讀",
          HO.recover(j5, {"running": False})[0] == HO.R_PENDING_RESTORE)


# ======================================================================
# E. Watchdog
# ======================================================================
def test_e():
    print("\n[E] Watchdog —— 逾時不得直接 restore")
    t = {"v": 0.0}
    wd = HO.Watchdog(max_paused_sec=100.0, clock=lambda: t["v"])
    check("未開始計時 → elapsed 為 None", wd.elapsed() is None)
    wd.mark_paused()
    t["v"] = 50.0
    check("50s < 100s → 未逾時", not wd.expired())
    t["v"] = 150.0
    check("150s > 100s → 逾時", wd.expired())
    check("★★ Phase 6 仍在 dispatch → 要求先收斂，不得直接 restore",
          wd.verdict(HO.S_PHASE6_ACTIVE) == (True, "REQUIRE_PHASE6_RELEASE_FIRST"))
    check("★★ RELEASING 中同樣不得直接 restore",
          wd.verdict(HO.S_PHASE6_RELEASING)[1] == "REQUIRE_PHASE6_RELEASE_FIRST")
    check("Phase 6 已放手 → 才要求 restore",
          wd.verdict(HO.S_PHASE6_RELEASED) == (True, "REQUIRE_RESTORE"))
    wd.clear()
    check("clear 後不再逾時", not wd.expired())
    for bad in (None, 0, -5):
        try:
            HO.Watchdog(max_paused_sec=bad)
            ok = False
        except ValueError:
            ok = True
        check(f"★★ max_paused_sec={bad} 必須拒絕（不得預設為無限）", ok)


# ======================================================================
# F. Idempotency
# ======================================================================
def test_f():
    print("\n[F] Idempotency —— 不重送，改以觀測驗證")
    j = tmp_journal()
    r = FakeRemote()
    o = orch(remote=r, mode=HO.MODE_ARMED, journal=j)
    o._to(HO.S_PREFLIGHT)
    ok, _ = o.request_pause()
    check("第 1 次 pause 送出成功", ok and r.pause_calls == 1)
    ok2, why = o.request_pause()
    check("★★ 第 2 次 pause 被拒（不重送）", ok2 is False)
    check(f"  拒絕原因為「已嘗試，須改以驗證」（{why}）",
          "MUST_VERIFY_NOT_RESEND" in (why or ""))
    check("★★ operator 實際只被呼叫 1 次", r.pause_calls == 1)

    # crash 落在 intent 與 outcome 之間 → 必須驗證，不得重送
    j2 = tmp_journal()
    j2.append("INTENT", state=HO.S_PAUSE_REQUESTED, step="PAUSE")
    g = HO.OnceGuard(j2)
    check("★★ 只有 INTENT 沒有 OUTCOME → needs_verification=True",
          g.needs_verification("PAUSE"))
    check("★★ 且不允許再次嘗試", not g.may_attempt("PAUSE"))
    j2.append("OUTCOME", state=HO.S_PAUSE_REQUESTED, step="PAUSE", ok=True)
    g2 = HO.OnceGuard(j2)
    check("有 OUTCOME 後 → 不需再驗證", not g2.needs_verification("PAUSE"))


# ======================================================================
# G. Audit Log
# ======================================================================
def test_g():
    print("\n[G] Audit Log —— append-only、單調 seq、意圖先寫")
    j = tmp_journal()
    r = FakeRemote()
    o = orch(remote=r, mode=HO.MODE_ARMED, journal=j)
    o._to(HO.S_PREFLIGHT)
    o.request_pause()
    o._to(HO.S_PAUSE_REQUESTED)
    recs = j.read_all()
    check("每筆都有 seq / at / kind", all({"seq", "at", "kind"} <= set(x) for x in recs))
    seqs = [x["seq"] for x in recs]
    check("★★ seq 單調遞增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))
    kinds = [x["kind"] for x in recs]
    check("★★ INTENT 出現在 OUTCOME 之前",
          kinds.index("INTENT") < kinds.index("OUTCOME"))
    check("狀態轉移有留下 STATE 紀錄", "STATE" in kinds)
    check("每行皆為合法 JSON",
          all(isinstance(x, dict) for x in recs) and len(recs) >= 3)
    # append-only：既有行不得被改寫
    before = io.open(j.path, encoding="utf-8").read()
    j.append("STATE", state=HO.S_PAUSE_VERIFYING)
    after = io.open(j.path, encoding="utf-8").read()
    check("★★ 新增不改寫既有內容（append-only）", after.startswith(before))
    # 重新開啟後 seq 接續，不從 1 重來
    j2 = HO.Journal(path=j.path)
    j2.append("STATE", state=HO.S_EXTERNAL_PAUSED)
    check("★★ 重啟後 seq 接續（不重複）",
          j2.read_all()[-1]["seq"] == max(seqs) + 2)


# ======================================================================
# H. Service lifecycle
# ======================================================================
def test_h():
    print("\n[H] Service lifecycle")
    check("LIFECYCLE 已定義四種 recover 分支",
          all(k in HO.LIFECYCLE for k in ("CLEAN", "PENDING_RESTORE",
                                          "CRITICAL", "UNKNOWN")))
    check("★★ 關閉時仍持有責任 → 必須完成 restore，不得靜默結束",
          "不得靜默結束" in HO.LIFECYCLE)
    src = io.open(os.path.join(HERE, "phase6_handoff_orchestrator.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imports.add((n.module or ".").split(".")[0])
    check(f"★★ 模組層零控制相依（imports={sorted(imports)}）",
          not (imports & {"device_control_operator", "pcs_control_executor",
                          "paramiko", "subprocess", "requests"}))
    check("★★ 本階段不建立任何排程 / 服務",
          "schtasks" not in src and "Register-ScheduledTask" not in src)


# ======================================================================
# I. Dry-run test plan
# ======================================================================
def test_i():
    print("\n[I] Dry-run —— 預設模式結構上送不出任何指令")
    for mode in (HO.MODE_DRY_RUN, HO.MODE_OBSERVE):
        r = FakeRemote()
        o = orch(remote=r, mode=mode, journal=tmp_journal())
        o._to(HO.S_PREFLIGHT)
        ok, why = o.request_pause()
        check(f"★★ {mode}：pause 不送出（{why}）", ok is False and r.pause_calls == 0)
        ok2, why2 = o.request_restore()
        check(f"★★ {mode}：restore 不送出", ok2 is False and r.restore_calls == 0)
    check("預設模式為 DRY_RUN", orch().mode == HO.MODE_DRY_RUN)
    try:
        orch(mode="ANYTHING")
        ok = False
    except ValueError:
        ok = True
    check("未支援的模式一律拒絕", ok)

    # DRY_RUN 仍可完整演練狀態機（只是不送指令）
    o = orch(mode=HO.MODE_DRY_RUN, journal=tmp_journal())
    for s in (HO.S_PREFLIGHT, HO.S_PAUSE_REQUESTED, HO.S_PAUSE_VERIFYING,
              HO.S_EXTERNAL_PAUSED, HO.S_PHASE6_ACTIVE, HO.S_PHASE6_RELEASING,
              HO.S_PHASE6_RELEASED, HO.S_RESTORE_REQUESTED,
              HO.S_RESTORE_VERIFYING, HO.S_RESTORED, HO.S_COMPLETE):
        o._to(s)
    check("★★ DRY_RUN 可完整演練 happy path 而不觸及設備",
          o.state == HO.S_COMPLETE)

    # restore 失敗 → CRITICAL
    r2 = FakeRemote(restore_ok=False)
    o2 = orch(remote=r2, mode=HO.MODE_ARMED, journal=tmp_journal())
    for s in (HO.S_PREFLIGHT, HO.S_PAUSE_REQUESTED, HO.S_PAUSE_VERIFYING,
              HO.S_EXTERNAL_PAUSED, HO.S_PHASE6_ACTIVE, HO.S_PHASE6_RELEASING,
              HO.S_PHASE6_RELEASED, HO.S_RESTORE_REQUESTED):
        o2._to(s)
    ok3, _ = o2.request_restore()
    check("restore 送出失敗", ok3 is False)
    o2._to(HO.S_RESTORE_FAILED, detail="external 未能恢復")
    check("★★ → RESTORE_FAILED_CRITICAL", o2.state == HO.S_RESTORE_FAILED)
    check("  被列為 critical", o2.state in HO.CRITICAL_STATES)
    check("  失敗原因已記錄", o2.failure_reason == "external 未能恢復")


# ======================================================================
# J. FIRST LIVE 後如何切換 dispatch_enabled
# ======================================================================
def test_j():
    print("\n[J] dispatch_enabled 切換條件")
    check("★★ 目前 DISPATCH_ENABLED = False", HO.DISPATCH_ENABLED is False)
    check("  snapshot 一律揭露該旗標",
          orch().snapshot()["dispatch_enabled"] is False)
    src = io.open(os.path.join(HERE, "phase6_handoff_orchestrator.py"),
                  encoding="utf-8").read()
    check("★★ 原始碼中不存在 DISPATCH_ENABLED = True",
          "DISPATCH_ENABLED = True" not in src)
    # ⚠️ 不能整檔 grep —— 模組 docstring 的禁止敘述本身就含這兩個字串。
    #    改以 AST 取出「非 docstring」的字串常數，確認沒有 HH:MM 形式的時刻。
    import re as _re
    tree = ast.parse(src)
    docstrings = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            d = ast.get_docstring(n, clean=False)
            if d:
                docstrings.add(d)
    code_strs = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.value not in docstrings}
    clocks = sorted(v for v in code_strs if _re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", v))
    check(f"★★ 程式碼中無任何硬編碼時刻（命中={clocks}）", not clocks)
    nums = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)}
    check(f"★★ 模組內無功率 / 門檻魔術數字（{sorted(nums)}）",
          not (nums & {5, 5.0, 80, 80.0, 150, 150.0, 85, 85.0, 1.25}))
    check("★★ watchdog 上限必須由呼叫端提供（無預設值）",
          "max_paused_sec=None" not in src.replace(" ", ""))


# ======================================================================
# K. 已核准 production 參數（Phase 6.10-B1.5）
# ======================================================================
def test_k():
    print(chr(10) + "[K] 已核准參數 —— 值、span 一致性、安全裕度")
    C = HO.HandoffConfig
    check("idle band = [-3.0, +1.0]",
          C.idle_power_lower_kw == -3.0 and C.idle_power_upper_kw == 1.0)
    check("pause settling timeout = 120.0 s",
          C.pause_settling_timeout_sec == 120.0)
    check("stable verify = 4 samples @ 15.0 s",
          C.stable_verify_min_samples == 4
          and C.stable_verify_interval_sec == 15.0)
    check("restore loop = 3 obs @ 3.0 s",
          C.restore_loop_min_observations == 3
          and C.restore_loop_interval_sec == 3.0)

    check("★★ stable span = 45.0 s（t=0/15/30/45）",
          C.stable_verify_min_span_sec == 45.0)
    check("★★ restore span = 6.0 s（t=0/3/6）",
          C.restore_loop_min_span_sec == 6.0)
    check("★★ span == (samples-1) x interval —— 防 off-by-one",
          (C.stable_verify_min_samples - 1) * C.stable_verify_interval_sec
          == C.stable_verify_min_span_sec
          and (C.restore_loop_min_observations - 1) * C.restore_loop_interval_sec
          == C.restore_loop_min_span_sec)
    check("★★ validate() 無問題", HO.HandoffConfig.validate() == [])

    check("★★ idle band 絕對值 < 4.0（5 kW 充放電不可能被誤判 idle）",
          max(abs(C.idle_power_lower_kw), abs(C.idle_power_upper_kw)) < 4.0)
    check("  5.0 kW 落在 band 外",
          not (C.idle_power_lower_kw <= 5.0 <= C.idle_power_upper_kw))
    check("  -5.0 kW 落在 band 外",
          not (C.idle_power_lower_kw <= -5.0 <= C.idle_power_upper_kw))
    check("  現場 idle 觀測 -1.3 / -1.4 / -3.0 / 0.0 皆在 band 內",
          all(C.idle_power_lower_kw <= v <= C.idle_power_upper_kw
              for v in (-1.3, -1.4, -3.0, 0.0)))
    check("★★ stable span 45 s > 外部重新取得控制的最長觀測 32 s",
          C.stable_verify_min_span_sec > 32.0)

    # validate() 必須抓得到刻意的不一致
    class Bad(HO.HandoffConfig):
        stable_verify_min_span_sec = 60.0
    check("★★ span 與 samples/interval 不一致時 validate() 會抓到",
          any("span" in p for p in Bad.validate()))

    class Bad2(HO.HandoffConfig):
        idle_power_upper_kw = 6.0
    check("★★ band 過大時 validate() 會抓到",
          any("誤判" in p for p in Bad2.validate()))

    check("★★ 寫入參數不等於啟用控制：DISPATCH_ENABLED 仍為 False",
          HO.DISPATCH_ENABLED is False)


# ======================================================================
# L. COMPLETE 條件（不得強制 ownership == EXTERNAL）
# ======================================================================
def test_l():
    print(chr(10) + "[L] COMPLETE 條件 —— external 正確地 idle 仍算完成")
    ok, ch = HO.handoff_complete_ok(True, True, True, HO.OWN_EXTERNAL, False)
    check("external 有出力（ownership=EXTERNAL）→ COMPLETE", ok)
    ok2, _ = HO.handoff_complete_ok(True, True, True, HO.OWN_NEITHER, False)
    check("★★ external 依 policy 正確 idle（ownership=NEITHER）→ 仍 COMPLETE", ok2)
    ok3, ch3 = HO.handoff_complete_ok(True, True, True, HO.OWN_BOTH, False)
    check("★★ ownership=BOTH → 不得 COMPLETE",
          not ok3 and ch3["ownership_not_both"] is False)
    for tag, args in (
            ("Phase6 未放手", (False, True, True, HO.OWN_NEITHER, False)),
            ("external runtime 不健康", (True, False, True, HO.OWN_NEITHER, False)),
            ("external loop 未恢復", (True, True, False, HO.OWN_NEITHER, False)),
            ("存在 critical", (True, True, True, HO.OWN_NEITHER, True))):
        check(f"★★ {tag} → 不得 COMPLETE", not HO.handoff_complete_ok(*args)[0])


# ======================================================================
# M. FIRST LIVE handoff plan 語意
# ======================================================================
def test_m():
    print(chr(10) + "[M] FIRST LIVE handoff plan（2026-09-02 修正版）")
    plan = HO.FIRST_LIVE_HANDOFF_PLAN
    for step in ("PRE_PAUSE_PREFLIGHT", "PAUSE", "SETTLING", "STABLE_VERIFY",
                 "OWNERSHIP", "POST_PAUSE_FRESH_PRECHECK", "FIRST_LIVE",
                 "VERIFY", "RELEASE", "RESTORE", "RESTORE_VERIFY", "COMPLETE"):
        check(f"  含步驟 {step}", step in plan)
    check("★★ PRE-PAUSE 明載不要求 Authority=IDLE / PCS idle / no external",
          "不要求: Authority=IDLE、PCS idle、no external controller" in plan)
    check("★★ 只有 POST-PAUSE 才要求那三項",
          "只有此階段才要求 PCS idle / Authority IDLE" in plan)
    check("★★ POST-PAUSE 不得沿用 Step 1 資料", "不得沿用 Step 1" in plan)
    check("★★ COMPLETE 不要求 ownership == EXTERNAL",
          "ownership != BOTH" in plan and "ownership=NEITHER）仍算 PASS" in plan)
    check("★★ RESTORE_VERIFY 不要求 PCS 一定出力",
          "不要求 PCS 一定 CHARGE/DISCHARGE" in plan)
    check("★★ pause 後責任不可清除", "RESTORE RESPONSIBILITY" in plan)
    check("  引用已核准的 span", "span >= 45 s" in plan and "span >= 6 s" in plan)
    check("  引用已核准的 band 與 timeout",
          "[-3.0,+1.0]" in plan and "120 s" in plan)


def main():
    print("=" * 72)
    print("  Phase 6.10 Unattended Ownership Handoff（A~J，完全離線）")
    print("=" * 72)
    try:
        for fn in (test_a, test_b, test_c, test_d, test_e,
                   test_f, test_g, test_h, test_i, test_j,
                   test_k, test_l, test_m):
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
