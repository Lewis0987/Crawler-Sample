# -*- coding: utf-8 -*-
"""
test_phase6_610c3_timeout_values.py — Phase 6.10-C3 已核准 SSH timeout 值
======================================================================
核心命題
    「四個 read-only 動詞的 timeout 已由實測支撐並核准；pause / restore
      只有結構同構推導出的候選值，**不得**被寫成 production 預設、
      也不得從 missing / not_field_verified 清單裡消失。」

已核准（2026-09-03 裁示）
    ssh_connect_timeout_sec   =  5.0  FINAL
    probe   command timeout   =  5.0  FINAL
    status  command timeout   = 12.0  FINAL
    loopcheck command timeout = 26.0  FINAL

仍非 FINAL
    pause   command timeout   = 12.0  STRUCTURAL CANDIDATE
    restore command timeout   = 12.0  STRUCTURAL CANDIDATE

是否需要設備
    **不需要**。本檔不連線、不送任何動詞；只讀既有的 C3 evidence 檔
    並比對常數。

用法
    python test_phase6_610c3_timeout_values.py        # exit 0 = PASS
"""
import io
import os
import sys
import ast
import json
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA               # noqa: E402
import phase6_unattended_service as SVC          # noqa: E402
import phase6_handoff_orchestrator as HO         # noqa: E402

RESULTS = []

EVIDENCE = os.path.join(os.path.dirname(HERE), "output", "phase6_ssh_timing",
                        "ssh_timing_20260903_173334.jsonl")
EVIDENCE_SHA256 = ("beeab404917198c88121f71ee898f117cb38b38b99e07b012b2376"
                   "c26da50ce8")

# 實測摘要（來自 C3 量測，供回歸比對；不是可調參數）
MEASURED = {
    "connect": {"n": 115, "max": 0.5089, "p50": 0.2742},
    "probe": {"n": 60, "max": 0.4136, "p50": 0.3632},
    "status": {"n": 30, "max": 1.6450, "p50": 0.3407},
    "loopcheck": {"n": 25, "max": 5.0792, "p50": 3.3828},
}


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ======================================================================
# 1. 四項已核准值
# ======================================================================
def test_1_approved():
    print("\n[1] 四項 FINAL 值")
    t = RA.SshTimeouts()
    check("★★ ssh_connect_timeout_sec = 5.0",
          t.connect_timeout_sec == 5.0
          and RA.APPROVED_SSH_CONNECT_TIMEOUT_SEC == 5.0)
    check("★★ probe command timeout = 5.0",
          t.command_timeout_for("probe") == 5.0)
    check("★★ status command timeout = 12.0",
          t.command_timeout_for("status") == 12.0)
    check("★★ loopcheck command timeout = 26.0",
          t.command_timeout_for("loopcheck") == 26.0)
    for k in ("ssh_connect_timeout_sec", "probe", "status", "loopcheck"):
        check(f"  {k} 標記為 FINAL",
              RA.SshTimeouts.status_of(k) == RA.TIMEOUT_FINAL)
    check("  三個唯讀動詞在 production 預設下都 configured",
          all(t.configured_for(v) for v in RA.READONLY_VERBS))


# ======================================================================
# 2. pause / restore 維持 CANDIDATE
# ======================================================================
def test_2_candidates():
    print("\n[2] pause / restore = STRUCTURAL CANDIDATE，不是 FINAL")
    t = RA.SshTimeouts()
    check("★★ pause 在 production 預設下仍為 None",
          t.command_timeout_for("pause") is None)
    check("★★ restore 在 production 預設下仍為 None",
          t.command_timeout_for("restore") is None)
    check("★★ 兩者標記為 STRUCTURAL_CANDIDATE，不是 FINAL",
          RA.SshTimeouts.status_of("pause")
          == RA.SshTimeouts.status_of("restore")
          == RA.TIMEOUT_STRUCTURAL_CANDIDATE)
    check("  結構候選值本身有登記（12.0 / 12.0）",
          RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC
          == {"pause": 12.0, "restore": 12.0})
    check("★★ 候選值與 status 同值（結構同構推導）—— 但來源不同",
          RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC["pause"]
          == RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC["status"] == 12.0
          and "pause" not in RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC)
    check("★★ 候選值**沒有**被寫進已核准字典",
          set(RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC)
          & set(RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC) == set())
    check("★★ 兩者恆列於 missing()（值仍為 None）",
          t.missing() == ["ssh_command_timeout_sec[pause]",
                          "ssh_command_timeout_sec[restore]"])
    check("★★ 兩者恆列於 not_field_verified()",
          t.not_field_verified() == ["pause", "restore"])

    # 🔴 即使有人把候選值注入，status 仍是 CANDIDATE —— 不得因此當成已驗證
    injected = RA.SshTimeouts(
        command_timeouts=dict(RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC,
                              **RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC))
    check("  注入候選值後 missing() 變空（值已填）",
          injected.missing() == [])
    check("★★ 但 not_field_verified() 仍列出 pause / restore"
          "（填值 ≠ 已驗證）",
          injected.not_field_verified() == ["pause", "restore"])

    # transport 層：production 預設下控制動詞送不出去
    class R(object):
        def __init__(self):
            self.calls = []

        def __call__(self, verb, c, x):
            self.calls.append(verb)
            return {"exit_code": 0, "stdout": ""}

    for v in ("pause", "restore"):
        r = R()
        res = RA.SshTransport(timeouts=RA.SshTimeouts(), runner=r,
                              armed=True).run(v)
        check(f"★★ production 預設 + armed 下 {v} 仍 SSH_NOT_CONFIGURED",
              res.outcome == RA.SSH_NOT_CONFIGURED and r.calls == [])


# ======================================================================
# 3. 已核准值與實測 / 結構窗的關係
# ======================================================================
def test_3_traceable():
    print("\n[3] 已核准值可回溯到量測與結構窗")
    t = RA.SshTimeouts()

    c = MEASURED["connect"]
    check(f"  connect 5.0 ≈ {5.0 / c['max']:.1f} × 實測 max {c['max']}"
          f"（n={c['n']}）", 5.0 / c["max"] > 9.0)
    check("  connect 5.0 在核准區間 3.0 ~ 8.0 內", 3.0 <= 5.0 <= 8.0)

    p = MEASURED["probe"]
    check(f"  probe 5.0 ≈ {5.0 / p['max']:.1f} × 實測 max {p['max']}"
          f"（n={p['n']}）", 5.0 / p["max"] > 11.0)
    check("★★ probe 無 server-side 結構窗 → client timeout 是唯一上界",
          RA.guard_server_side_bound_sec("probe") is None)
    check("  probe 5.0 <= guard ACT_TIMEOUT 10.0"
          "（最便宜的動詞不該比 guard 更有耐心）",
          t.command_timeout_for("probe") <= RA.GUARD_ACT_TIMEOUT_SEC)

    st = MEASURED["status"]
    bound = RA.guard_server_side_bound_sec("status")
    check(f"★★ status 12.0 > 結構窗 {bound}（否則會搶在 guard exit 41 前逾時）",
          t.command_timeout_for("status") > bound)
    check(f"  12.0 = 結構窗 {bound} + 實測最大額外成本 {st['max']}"
          f" = {bound + st['max']:.3f} 向上取整",
          bound + st["max"] <= 12.0 < bound + st["max"] + 1.0)
    check("  12.0 <= 既有 read-timeout 契約 15.0", 12.0 <= 15.0)

    lc = MEASURED["loopcheck"]
    lb = RA.guard_server_side_bound_sec("loopcheck")
    extra = lc["max"] - RA.GUARD_LOOPCHECK_SLEEP_SEC
    check(f"★★ loopcheck 26.0 > 結構窗 {lb}",
          t.command_timeout_for("loopcheck") > lb)
    check(f"  26.0 = 結構窗 {lb} + 扣除固定 sleep 後的實測最大額外成本"
          f" {extra:.4f} = {lb + extra:.3f} 向上取整",
          lb + extra <= 26.0 < lb + extra + 1.0)
    check("  26.0 < 一個 decision interval 30.0", 26.0 < 30.0)
    check(f"★★ 正常實測只要 {lc['p50']} s —— 26 s 不是「需要這麼久」，"
          f"而是「不要先放棄」", lc["p50"] < 5.0)


# ======================================================================
# 4. Fail Closed 路徑未被拿掉
# ======================================================================
def test_4_fail_closed():
    print("\n[4] 未設定仍然 Fail Closed")
    u = RA.SshTimeouts.unconfigured()
    check("  unconfigured() 全為 None",
          u.connect_timeout_sec is None
          and all(u.command_timeout_for(v) is None
                  for v in RA.GUARD_ALLOWED_VERBS))
    check("  missing() 六項全列", len(u.missing()) == 6)

    class R(object):
        def __init__(self):
            self.calls = []

        def __call__(self, verb, c, x):
            self.calls.append(verb)
            return {"exit_code": 0, "stdout": ""}

    r = R()
    check("★★ 未設定 → NOT_CONFIGURED 且 runner 不被呼叫",
          RA.SshTransport(timeouts=u, runner=r,
                          armed=True).run("probe").outcome
          == RA.SSH_NOT_CONFIGURED and r.calls == [])
    check("  明確注入 None 覆蓋已核准值仍然有效",
          RA.SshTimeouts(connect_timeout_sec=None).connect_timeout_sec is None)
    check("  transport 的 timeouts 預設仍是 None（未注入即 Fail Closed）",
          RA.SshTransport(runner=R()).run("probe").outcome
          == RA.SSH_NOT_CONFIGURED)


# ======================================================================
# 5. 常數具名保存，非魔術數字
# ======================================================================
def test_5_named():
    print("\n[5] 具名常數")
    src = io.open(os.path.join(HERE, "phase6_remote_adapter.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    names = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
    for want in ("APPROVED_SSH_CONNECT_TIMEOUT_SEC",
                 "APPROVED_SSH_COMMAND_TIMEOUT_SEC",
                 "STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC",
                 "SSH_TIMEOUT_STATUS"):
        check(f"  {want} 為模組層具名常數", want in names)
    check("★★ 仍不存在單一 ssh_timeout_sec / SSH_TIMEOUT_SEC",
          "ssh_timeout_sec" not in names and "SSH_TIMEOUT_SEC" not in names)

    # 建構子簽章不得直接寫數字
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "__init__":
            a = n.args
            for arg, d in zip(a.args[len(a.args) - len(a.defaults):],
                              a.defaults):
                if arg.arg in ("connect_timeout_sec", "command_timeouts",
                               "timeouts"):
                    if isinstance(d, ast.Constant) and isinstance(
                            d.value, (int, float)) and not isinstance(
                            d.value, bool):
                        bad.append((arg.arg, n.lineno))
    check(f"★★ 簽章中沒有任何數字字面值 timeout 預設（命中={bad}）", not bad)

    # 未來 capability 改善只記錄，未部署
    check("  未來 guard capability 欄位已記錄（6 項）",
          len(RA.FUTURE_GUARD_CAPABILITY_FIELDS) == 6
          and "capability_pause" in RA.FUTURE_GUARD_CAPABILITY_FIELDS)
    check("★★ 但 deployed guard 未變更，仍是 B1",
          RA.DEPLOYED_GUARD_VARIANT == "B1")
    check("  B1 腳本內未出現 capability 回報欄位（確實沒改 guard）",
          "capability_pause" not in RA.REMOTE_GUARD_B1_SH)


# ======================================================================
# 6. C3 evidence 完整性
# ======================================================================
def test_6_evidence():
    print("\n[6] C3 evidence")
    check(f"  evidence 檔存在：{os.path.basename(EVIDENCE)}",
          os.path.exists(EVIDENCE))
    if not os.path.exists(EVIDENCE):
        return
    raw = io.open(EVIDENCE, "rb").read()
    check(f"★★ SHA256 相符（{len(raw)} bytes）",
          hashlib.sha256(raw).hexdigest() == EVIDENCE_SHA256)
    rows = [json.loads(x) for x in
            raw.decode("utf-8").splitlines() if x.strip()]
    meas = [r for r in rows if not r.get("warmup")]
    check(f"  量測樣本 {len(meas)} 筆（warm-up {len(rows) - len(meas)} 排除）",
          len(meas) == 115)
    check("★★ 115 筆全部 SSH_OK，0 失敗",
          all(r["classification"] == RA.SSH_OK for r in meas))
    counts = {v: sum(1 for r in meas if r["verb"] == v)
              for v in ("probe", "status", "loopcheck")}
    check(f"  取樣分布 {counts}",
          counts == {"probe": 60, "status": 30, "loopcheck": 25})
    check("★★ evidence 中沒有任何控制動詞",
          not [r for r in rows if r["verb"] in RA.CONTROL_VERBS])
    check("★★ evidence 中不含任何祕密欄位",
          not any(k in ("key", "password", "token", "authorization")
                  for r in rows for k in r))
    # 實測摘要與檔案一致
    for verb in ("probe", "status", "loopcheck"):
        vals = [r["command_sec"] for r in meas if r["verb"] == verb]
        check(f"  {verb} 實測 max {max(vals):.4f} 與回報一致",
              abs(max(vals) - MEASURED[verb]["max"]) < 1e-4)
    cv = [r["connect_sec"] for r in meas]
    check(f"  connect 實測 max {max(cv):.4f} 與回報一致",
          abs(max(cv) - MEASURED["connect"]["max"]) < 1e-4)


# ======================================================================
# 7. 其他狀態不因 C3 而改變
# ======================================================================
def test_7_unchanged():
    print("\n[7] C3 不改變其他任何狀態")
    check("★★ B1.7 相關：NETWORK_IDENTITY_STABILITY 仍 NOT_VERIFIED",
          SVC.NETWORK_IDENTITY_STABILITY == SVC.NET_NOT_VERIFIED)
    check("★★ DEPLOYED_GUARD_VARIANT 仍為 B1",
          RA.DEPLOYED_GUARD_VARIANT == "B1")
    check("★★ DISPATCH_ENABLED 仍為 False", HO.DISPATCH_ENABLED is False)
    check("★★ RemoteSenders 預設 armed = False",
          RA.RemoteSenders().armed is False)
    check("  FIRST_LIVE_PREREQUISITE 仍為 False",
          SVC.FIRST_LIVE_PREREQUISITE_SATISFIED is False)
    check("  live gate 仍 11 項、critical conditions 仍 4 項",
          len(SVC.LIVE_GATE_ITEMS) == 11
          and len(SVC.CRITICAL_CONDITIONS) == 4)
    check("  C1 兩項仍為 5.0 / 300.0",
          SVC.ServiceTiming().retry_backoff_sec == 5.0
          and SVC.ServiceTiming().service_health_timeout_sec == 300.0)
    check("★★ C2 taxonomy 未被 C3 修改（7 種，COMMAND_TIMEOUT 仍 uncertain）",
          len(RA.SSH_OUTCOMES) == 7
          and RA.SSH_COMMAND_TIMEOUT in RA.SSH_OUTCOME_UNCERTAIN
          and RA.SSH_COMMAND_TIMEOUT not in RA.SSH_DEFINITELY_NOT_EXECUTED)
    check("★★ 兩個 timeout 不確定狀態仍在 PENDING_RESTORE_STATES",
          HO.S_PAUSE_OUTCOME_UNKNOWN in HO.PENDING_RESTORE_STATES
          and HO.S_RESTORE_OUTCOME_UNKNOWN in HO.PENDING_RESTORE_STATES)


# ======================================================================
def main():
    for fn in (test_1_approved, test_2_candidates, test_3_traceable,
               test_4_fail_closed, test_5_named, test_6_evidence,
               test_7_unchanged):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
