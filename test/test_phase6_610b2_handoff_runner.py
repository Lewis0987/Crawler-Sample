# -*- coding: utf-8 -*-
"""
test_phase6_610b2_handoff_runner.py — Phase 6.10-B2（離線）
======================================================================
核心命題
    「12 步交接流程在 DRY_RUN 下可完整演練，但 remote sender 與 dispatcher
      **一次都不會被呼叫**；而只要 pause 曾經送出，之後任何失敗都必須走到
      歸還 —— 不存在『直接 return 而忘了 restore』的路徑。」

是否需要設備
    **不需要**。sample / probe / loop mark / dispatcher / sender 全部注入。
    guard allowlist 以本機 bash 執行離線腳本驗證。零 SSH、零實機 command。

用法
    python test_phase6_610b2_handoff_runner.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO            # noqa: E402
import phase6_remote_adapter as RA                  # noqa: E402

RESULTS = []
_TMP = []
SOC_MAX = 85.0


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def _bash():
    for c in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files\Git\usr\bin\bash.exe",
              "/usr/bin/bash", "/bin/bash"):
        if os.path.exists(c):
            return c
    return shutil.which("bash")


def tmp_journal():
    d = tempfile.mkdtemp(prefix="p610b2_")
    _TMP.append(d)
    return HO.Journal(path=os.path.join(d, "j.jsonl"))


def probe(**over):
    p = {"screen_alive": True, "process_alive": True,
         "process_identity_ok": True}
    p.update(over)
    return p


def smp(**over):
    """預設為「external 已靜止、Phase 6 可接手」的乾淨樣本。"""
    s = {"pcs_state": "STANDBY", "ac_kw": -1.3, "authority": "IDLE",
         "comm_ok": True, "fault": False, "critical_alarms": 0, "fresh": True,
         "soc": 5.0, "soc_fresh": True, "meter_fresh": True, "ess_fresh": True,
         "tou": "OFF_PEAK", "decision": "charge",
         "external_behaviour_known": True}
    s.update(over)
    return s


class Senders(object):
    """記錄 remote sender 實際被呼叫幾次。"""

    def __init__(self, pause_ok=True, restore_ok=True):
        self.pause_calls = 0
        self.restore_calls = 0
        self.pause_ok = pause_ok
        self.restore_ok = restore_ok

    def pause(self):
        self.pause_calls += 1
        return self.pause_ok

    def restore(self):
        self.restore_calls += 1
        return self.restore_ok


def runner(mode=HO.MODE_DRY_RUN, samples=None, probes=None, senders=None,
           dispatcher=None, marks=None, journal=None):
    sd = senders or Senders()
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd.pause,
                                   restore_sender=sd.restore),
        observe=(lambda: {}), gates=(lambda x: (True, {})),
        journal=journal or tmp_journal(), mode=mode,
        idle_baseline_kw=-1.3, power_tolerance_kw=1.25)
    sq = list(samples or [smp()] * 40)
    pq = list(probes or [probe()] * 40)
    mq = list(marks or ["2026-09-03 01:00:%02d" % i for i in range(40)])

    def nxt(q, default):
        return q.pop(0) if q else default

    r = HO.HandoffRunner(
        o, sample_source=(lambda: nxt(sq, smp())),
        probe_source=(lambda: nxt(pq, probe())),
        loop_mark_source=(lambda: nxt(mq, "2026-09-03 02:00:00")),
        dispatcher=dispatcher, soc_max_pct=SOC_MAX)
    return r, o, sd


# ======================================================================
# 1. DRY_RUN：sender / dispatcher 一次都不被呼叫
# ======================================================================
def test_1():
    print("\n[1] DRY_RUN —— 結構上送不出任何指令")
    hits = []
    for mode in (HO.MODE_DRY_RUN, HO.MODE_OBSERVE):
        r, o, sd = runner(mode=mode,
                          dispatcher=(lambda s: hits.append(s) or {}))
        res = r.run()
        check(f"★★ {mode}：pause sender 呼叫 0 次", sd.pause_calls == 0)
        check(f"★★ {mode}：restore sender 呼叫 0 次", sd.restore_calls == 0)
        check(f"  {mode}：outcome=NOT_SENT（預期，非失敗）",
              res["outcome"] == "NOT_SENT")
        check(f"  {mode}：未背負恢復責任",
              res["restore_responsibility"] is False)
    check("★★ dispatcher 全程未被呼叫", hits == [])
    check("★★ 預設模式為 DRY_RUN", HO.MODE_DRY_RUN == runner()[1].mode)


# ======================================================================
# 2. Step 1 PRE_PAUSE 不要求那三項
# ======================================================================
def test_2():
    print("\n[2] PRE_PAUSE_PREFLIGHT —— 不要求 Authority/PCS idle/no external")
    check("★★ 三項不在 PRE_PAUSE_ITEMS 中",
          not any(k in HO.PRE_PAUSE_ITEMS
                  for k in HO.PRE_PAUSE_MUST_NOT_REQUIRE))
    # external 正在充電 → pre-pause 仍應通過
    charging = smp(pcs_state="CHARGING", ac_kw=-80.0,
                   authority="EXTERNAL_OR_UNKNOWN")
    ok, ch = HO.pre_pause_gates(charging, probe(), SOC_MAX)
    check("★★ external 正在充電（PCS=CHARGING, Authority=EXTERNAL）→ 仍 PASS", ok)
    check("  檢查項不含 authority_idle / pcs_idle",
          "authority_idle" not in ch and "pcs_idle" not in ch)

    # 但 pre-pause 該擋的仍要擋
    for tag, bad in (("TOU 非離峰", smp(tou="PEAK")),
                     ("SOC 超過充電帶", smp(soc=90.0)),
                     ("Decision 非 charge", smp(decision="idle")),
                     ("通訊異常", smp(comm_ok=False)),
                     ("有故障", smp(fault=True)),
                     ("有嚴重告警", smp(critical_alarms=1)),
                     ("SOC 不 fresh", smp(soc_fresh=False)),
                     ("Meter 不 fresh", smp(meter_fresh=False)),
                     ("ESS 不 fresh", smp(ess_fresh=False)),
                     ("external 行為未知", smp(external_behaviour_known=False))):
        check(f"  {tag} → pre-pause FAIL",
              not HO.pre_pause_gates(bad, probe(), SOC_MAX)[0])
    check("  external runtime 不健康 → FAIL",
          not HO.pre_pause_gates(smp(), probe(process_alive=False), SOC_MAX)[0])
    check("★★ soc_max_pct 未提供 → Fail Closed",
          not HO.pre_pause_gates(smp(), probe(), None)[0])


# ======================================================================
# 3. Step 6 POST_PAUSE 才要求那三項
# ======================================================================
def test_3():
    print("\n[3] POST_PAUSE_FRESH_PRECHECK —— 這裡才要求三項")
    ok, ch = HO.post_pause_precheck(smp(), probe())
    check("乾淨樣本 → PASS", ok)
    for k in HO.PRE_PAUSE_MUST_NOT_REQUIRE:
        check(f"★★ 檢查項包含 {k}", k in ch)
    check("★★ PCS 仍在充電 → FAIL",
          not HO.post_pause_precheck(smp(pcs_state="CHARGING"), probe())[0])
    check("★★ Authority=EXTERNAL → FAIL",
          not HO.post_pause_precheck(smp(authority="EXTERNAL_OR_UNKNOWN"),
                                     probe())[0])
    check("★★ 樣本不 fresh → FAIL",
          not HO.post_pause_precheck(smp(fresh=False), probe())[0])


# ======================================================================
# 4. ARMED：pause 送出後即背負責任；DISPATCH_ENABLED=False 仍不得 dispatch
# ======================================================================
def test_4():
    print("\n[4] ARMED —— 責任成立，但 DISPATCH_ENABLED=False 擋下 dispatch")
    hits = []
    r, o, sd = runner(mode=HO.MODE_ARMED,
                      dispatcher=(lambda s: hits.append(s) or
                                  {"dispatched": True, "verified": True,
                                   "released": True}))
    res = r.run()
    check("pause sender 被呼叫 1 次", sd.pause_calls == 1)
    check("★★ dispatcher 仍未被呼叫（DISPATCH_ENABLED=False）", hits == [])
    steps = {s["step"]: s for s in res["steps"]}
    check("  Step 1 通過", steps["1_PRE_PAUSE_PREFLIGHT"]["ok"])
    check("  Step 2 通過", steps["2_PAUSE"]["ok"])
    check("  Step 3/4 通過", steps["3_4_PAUSE_VERIFY"]["ok"])
    check("  Step 5 通過", steps["5_OWNERSHIP"]["ok"])
    check("  Step 6 通過", steps["6_POST_PAUSE_PRECHECK"]["ok"])
    check("★★ Step 7 被 DISPATCH_ENABLED 擋下",
          not steps["7_FIRST_LIVE"]["ok"]
          and "DISPATCH_ENABLED" in steps["7_FIRST_LIVE"]["detail"])
    check("★★ 但仍走到歸還（restore 已送出）", sd.restore_calls == 1)
    check("★★ 歸還驗證通過 → 責任解除",
          res["restore_responsibility"] is False)
    check(f"  最終 outcome={res['outcome']}", res["outcome"] == "COMPLETE")
    check("  最終狀態 COMPLETE", o.state == HO.S_COMPLETE)


# ======================================================================
# 5. pause 後任一失敗都必須走到歸還
# ======================================================================
def test_5():
    print("\n[5] pause 後失敗 —— 一律走到歸還，不存在漏歸還路徑")
    # Step 6 失敗（暫停後 PCS 又被外部啟動）
    # sample_source 的消費順序：
    #   1 筆 Step 1 pre-flight
    # + 4 筆 PauseVerifier（stable_verify_min_samples=4）
    # → 第 6 筆（index 5）才是 Step 6 POST_PAUSE_PRECHECK 取到的樣本。
    samples = [smp()] * 5 + [smp(pcs_state="CHARGING",
                                 authority="EXTERNAL_OR_UNKNOWN")] + [smp()] * 20
    r, o, sd = runner(mode=HO.MODE_ARMED, samples=samples)
    res = r.run()
    check("pause 已送出", sd.pause_calls == 1)
    steps = {s["step"]: s for s in res["steps"]}
    check("★★ Step 6 失敗", "6_POST_PAUSE_PRECHECK" in steps
          and not steps["6_POST_PAUSE_PRECHECK"]["ok"])
    check("★★ 仍送出 restore（未漏歸還）", sd.restore_calls == 1)
    check("  責任已解除", res["restore_responsibility"] is False)

    # STABLE_VERIFY 期間出現 external evidence
    samples2 = [smp()] * 2 + [smp(authority="EXTERNAL_OR_UNKNOWN")] + [smp()] * 20
    r2, o2, sd2 = runner(mode=HO.MODE_ARMED, samples=samples2)
    res2 = r2.run()
    check("★★ STABLE_VERIFY 失敗後仍送出 restore", sd2.restore_calls == 1)

    # settling 期間 probe 顯示 process 死掉
    probes3 = [probe()] * 2 + [probe(process_alive=False)] * 30
    r3, o3, sd3 = runner(mode=HO.MODE_ARMED, probes=probes3)
    res3 = r3.run()
    check("★★ probe 異常後仍送出 restore", sd3.restore_calls == 1)


# ======================================================================
# 6. restore 失敗 → CRITICAL
# ======================================================================
def test_6():
    print("\n[6] restore 失敗 → RESTORE_FAILED_CRITICAL")
    sd = Senders(restore_ok=False)
    r, o, _ = runner(mode=HO.MODE_ARMED, senders=sd)
    res = r.run()
    check("★★ outcome = RESTORE_FAILED_CRITICAL",
          res["outcome"] == "RESTORE_FAILED_CRITICAL")
    check("★★ 狀態為 critical", o.state in HO.CRITICAL_STATES)
    check("★★ 恢復責任**未**解除（仍背負）",
          res["restore_responsibility"] is True)

    # restore 送出成功但 loop 沒恢復
    r2, o2, sd2 = runner(mode=HO.MODE_ARMED,
                         marks=["2026-09-03 01:00:00"] * 30)
    res2 = r2.run()
    check("★★ loop 時間戳未前進 → 仍判 CRITICAL",
          res2["outcome"] == "RESTORE_FAILED_CRITICAL")


# ======================================================================
# 7. B2 guard —— 離線 allowlist（尚未部署）
# ======================================================================
def test_7():
    print("\n[7] B2 guard —— 離線驗證，且明確標示尚未部署")
    check("B2 動詞含 pause / restore",
          RA.guard_b2_would_accept("pause")
          and RA.guard_b2_would_accept("restore"))
    check("★★ B1（實際部署中）仍拒絕 pause / restore",
          not RA.guard_b1_would_accept("pause")
          and not RA.guard_b1_would_accept("restore"))
    check("★★ DEPLOYED_GUARD_VARIANT = B2（現場已部署）",
          RA.DEPLOYED_GUARD_VARIANT == "B2")
    check("★★ 但 pause / restore 仍未 field tested"
          "（timeout 仍是 STRUCTURAL CANDIDATE）",
          RA.SshTimeouts().not_field_verified() == ["pause", "restore"])
    sh = RA.REMOTE_GUARD_B2_SH
    code = "\n".join(ln for ln in sh.splitlines()
                     if not ln.lstrip().startswith("#"))
    check("★★ pause / restore 分支皆先 require_single_screen",
          code.count("require_single_screen") >= 4)
    check("★★ pause / restore 分支皆先 require_identity",
          code.count("require_identity") >= 5)
    for t in ("eval", "sh -c"):
        check(f"★★ 不使用 {t}", t not in code)
    check("沿用 B1 強化", "set -euo pipefail" in code
          and "PATH=/usr/bin:/bin" in code and "MKTEMP" in code
          and "trap " in code and "SCREEN_NAME=auto" in code)
    check("★★ 不寫死 PID", "1128" not in code and "1140" not in code)

    d = tempfile.mkdtemp(prefix="p610b2g_")
    _TMP.append(d)
    path = os.path.join(d, "g.sh")
    body = re.sub(r"^(SCREEN|PGREP|GREP|HEAD|TAIL|READLINK|SLEEP|TIMEOUT"
                  r"|MKTEMP|RM|PS)=/usr/bin/\w+$",
                  lambda m: m.group(1) + "=/usr/bin/echo", sh, flags=re.M)
    io.open(path, "w", encoding="utf-8", newline="\n").write(body)
    for verb, want in (("hostname", RA.GUARD_EXIT_REFUSED),
                       ("kill 1140", RA.GUARD_EXIT_REFUSED),
                       ("pause; rm -rf /", RA.GUARD_EXIT_REFUSED),
                       ("PAUSE", RA.GUARD_EXIT_REFUSED),
                       (" pause", RA.GUARD_EXIT_REFUSED),
                       ("pause extra", RA.GUARD_EXIT_REFUSED),
                       ("restore;start", RA.GUARD_EXIT_REFUSED),
                       ("__MISSING__", RA.GUARD_EXIT_MISSING)):
        env = dict(os.environ)
        if verb == "__MISSING__":
            env.pop("SSH_ORIGINAL_COMMAND", None)
        else:
            env["SSH_ORIGINAL_COMMAND"] = verb
        r = subprocess.run([_bash(), path], env=env, capture_output=True,
                           text=True)
        lbl = "無 verb" if verb == "__MISSING__" else repr(verb)
        check(f"  {lbl} → exit {want}", r.returncode == want)


# ======================================================================
# 8. 稽核與旗標
# ======================================================================
def test_8():
    print("\n[8] 稽核紀錄與安全旗標")
    j = tmp_journal()
    r, o, sd = runner(mode=HO.MODE_ARMED, journal=j)
    res = r.run()
    recs = j.read_all()
    kinds = [x["kind"] for x in recs]
    check("★★ 每個步驟都留下 PLAN_STEP 稽核", kinds.count("PLAN_STEP") >= 8)
    check("  pause 有 INTENT 在 OUTCOME 之前",
          kinds.index("INTENT") < kinds.index("OUTCOME"))
    seqs = [x["seq"] for x in recs]
    check("  seq 單調遞增", seqs == sorted(seqs))
    check("★★ DISPATCH_ENABLED 仍為 False", HO.DISPATCH_ENABLED is False)
    check("★★ 結果一律揭露 mode 與 dispatch_enabled",
          res["mode"] == HO.MODE_ARMED and res["dispatch_enabled"] is False)
    src = io.open(os.path.join(HERE, "phase6_handoff_orchestrator.py"),
                  encoding="utf-8").read()
    check("★★ 原始碼中不存在 DISPATCH_ENABLED = True",
          "DISPATCH_ENABLED = True" not in src)


def main():
    print("=" * 72)
    print("  Phase 6.10-B2 Handoff Runner（完全離線，零實機 command）")
    print("=" * 72)
    try:
        for fn in (test_1, test_2, test_3, test_4, test_5, test_6,
                   test_7, test_8):
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
