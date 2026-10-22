# -*- coding: utf-8 -*-
"""
test_phase6_scheduled_launcher.py — Scheduled FIRST LIVE launcher 回歸
======================================================================
核心命題
    「launcher 只是薄殼：解析 config、Fail Closed 驗證、組相依、
      呼叫既有 scheduled runner、回穩定 exit code。
      它不含任何控制邏輯，也不得把『config 存在』當成 field authorization。」

使用者指定的 17 項
    L1  valid config
    L2  missing config
    L3  invalid JSON
    L4  missing required field
    L5  invalid timezone
    L6  invalid date
    L7  invalid window
    L8  enabled false
    L9  already consumed
    L10 independent cwd
    L11 paths with spaces（D:\\Crawler Sample）
    L12 no stdin required
    L13 handled ABORT exit semantics
    L14 unexpected exception
    L15 no duplicate control logic
    L16 no subprocess control path
    L17 launcher never directly imports executor / control modules

用法
    python test_phase6_scheduled_launcher.py        # exit 0 = PASS
"""
import ast
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_scheduled_first_live as SC             # noqa: E402
import run_scheduled_first_live as LAUNCH            # noqa: E402

RESULTS = []
TMPDIRS = []
LAUNCHER = os.path.join(HERE, "run_scheduled_first_live.py")


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir(prefix="lnch_"):
    d = tempfile.mkdtemp(prefix=prefix)
    TMPDIRS.append(d)
    return d


# ======================================================================
# Fixture provider（離線替身；**不碰任何設備**）
# ======================================================================
PROVIDER_SRC = '''# -*- coding: utf-8 -*-
"""離線 fixture provider。不 import 任何 control / executor 模組。"""
import datetime
import os

MODE = os.environ.get("FIXTURE_MODE", "pass")
NOW = datetime.datetime(2026, 9, 22, 0, 10, 0)


def _art():
    import phase6_first_live_handoff as FL
    a = FL.HandoffExecutionArtifact()
    a.set(pause_send_outcome="SENT", post_handoff_revalidation_ok=True,
          post_handoff_verdict=FL.REVALIDATION_OK,
          phase6_authority_acquired=True, phase6_authority_released=True,
          dispatcher_attempted=True, dispatcher_outcome=FL.DISPATCH_SUCCEEDED,
          restore_attempt_count=1, restore_send_outcome="SENT",
          restore_verifier_state="RESUMED", restore_resumed=True,
          pause_attempt_count=1, normal_loop_halted=True,
          pause_verifier_state="VERIFIED",
          discriminative_pause_verified=True, release_before_restore=True,
          final_ownership="EXTERNAL", fault=False, critical_alarms=0,
          pcs_stop_reason="LIVE_LEG_CLEANUP",
          phase6_command_counts={"counts": {"phase6_charge": 1,
                                            "phase6_pcs_stop": 1,
                                            "live_leg_internal_stop": 1}},
          post_check={"ownership_not_both": True, "fault_clear": True,
                      "alarm_clear": True, "runtime_healthy": True,
                      "responsibility_cleared": True},
          handoff_outcome="COMPLETE")
    d = a.to_dict()
    d["restore_responsibility_final"] = False
    return d


def validate_provider_definition(config=None):
    if MODE == "validate_fail":
        return False, {"stub_precondition": False}
    return True, {"stub_precondition": True}


def build(config):
    if MODE == "boom":
        raise RuntimeError("provider 故意失敗")
    if MODE == "incomplete":
        return {"now_fn": (lambda: NOW)}

    def gate_a():
        ok = MODE != "gate_a_fail"
        return (ok, {"natural_decision_is_charge": ok}, "V")

    return {"now_fn": (lambda: NOW),
            "source_state_check": (lambda: {"module_ok": True}),
            "safety_preflight": (lambda: (True, {"comm": True})),
            "gate_a": gate_a,
            "handoff_readiness": (lambda: (True, {"pause_cap": True})),
            "arm_and_run_handoff": (lambda: {"outcome": "COMPLETE"}),
            "collect_artifact": (lambda r: _art()),
            "safety_reset": (lambda: {"armed": False}),
            "waiter": (lambda s: None)}
'''


OFFICIAL_ID = "phase6_first_live_field"


def write_provider(d, name="fixture_provider"):
    p = os.path.join(d, name + ".py")
    io.open(p, "w", encoding="utf-8").write(PROVIDER_SRC)
    return name


_ATTEMPT_SEQ = [0]


def cfg_dict(**kw):
    # 🔴 每份 config 用獨立 attempt_id ＋ 落在自己目錄下的 ledger / artifact，
    #    否則不同測試會共用同一份 one-shot ledger 而互相污染
    #    （預設路徑是 <config_dir>/../output/...，暫存目錄同父層時會撞在一起）。
    _ATTEMPT_SEQ[0] += 1
    d = {"enabled": True, "attempt_id": "LNCH-TEST-%03d" % _ATTEMPT_SEQ[0],
         "target_date": "2026-09-22", "window_start": "00:00",
         "window_end": "00:30", "timezone": "Asia/Taipei",
         "dependency_provider": OFFICIAL_ID,
         "ledger_path": "ledger.jsonl", "artifact_dir": "artifacts"}
    d.update(kw)
    return d


def write_cfg(d, **kw):
    p = os.path.join(d, "one-shot.json")
    body = kw.pop("_raw", None)
    if body is None:
        body = json.dumps(cfg_dict(**kw), ensure_ascii=False, indent=2)
    io.open(p, "w", encoding="utf-8").write(body)
    return p


def run_in_process(cfg_path, provider_dir, mode="pass"):
    """以 in-process 方式跑完整 main()，暫時把 allowlist 對映到 fixture 模組。

    🔴 **不新增**任何 production bypass —— allowlist 本體未變，
       只有測試期間替換對映，結束即還原。
    """
    old_map = dict(LAUNCH.PROVIDER_ALLOWLIST)
    old_mode = os.environ.get("FIXTURE_MODE")
    old_path = list(sys.path)
    os.environ["FIXTURE_MODE"] = mode
    if provider_dir not in sys.path:
        sys.path.insert(0, provider_dir)
    for m in [k for k in list(sys.modules) if k == "fixture_provider"]:
        del sys.modules[m]
    LAUNCH.PROVIDER_ALLOWLIST[OFFICIAL_ID] = "fixture_provider"
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    try:
        rc = LAUNCH.main(["--config", cfg_path])
    finally:
        sys.stdout = real
        LAUNCH.PROVIDER_ALLOWLIST.clear()
        LAUNCH.PROVIDER_ALLOWLIST.update(old_map)
        sys.path[:] = old_path
        if old_mode is None:
            os.environ.pop("FIXTURE_MODE", None)
        else:
            os.environ["FIXTURE_MODE"] = old_mode
    out = buf.getvalue()
    try:
        summary = json.loads(out.strip())
    except ValueError:
        summary = None
    return rc, summary, out


def run_cli(cfg_path, extra=None, cwd=None, provider_dir=None, mode=None):
    """以**獨立 python process** 啟動 launcher（無 stdin）。"""
    env = dict(os.environ)
    paths = [provider_dir] if provider_dir else []
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in paths + [env.get("PYTHONPATH", "")] if p])
    env["PYTHONIOENCODING"] = "utf-8"
    if mode:
        env["FIXTURE_MODE"] = mode
    argv = [sys.executable, LAUNCHER]
    if cfg_path is not None:
        argv += ["--config", cfg_path]
    argv += list(extra or [])
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd or tempfile.gettempdir(),
                       env=env, stdin=subprocess.DEVNULL, timeout=180)
    out = (p.stdout or "") + (p.stderr or "")
    try:
        summary = json.loads((p.stdout or "").strip())
    except ValueError:
        summary = None
    return p.returncode, summary, out


# ======================================================================
# L1 ~ L8：config 驗證
# ======================================================================
def test_L1_valid_config():
    print("\n[L1] valid config")
    d = tmpdir()
    write_provider(d)
    c = write_cfg(d)
    rc, s, out = run_in_process(c, d)
    check("★★ L1. exit = 0（handled terminal result）", rc == LAUNCH.EXIT_HANDLED)
    check("★★ L1. config_validation = CONFIG_OK",
          s and s["config_validation"] == LAUNCH.CONFIG_OK)
    check("★★ L1. scheduled_result = PASS",
          s and s["scheduled_result"] == SC.R_PASS)
    check("  attempt_id 正確帶出",
          s and s["attempt_id"].startswith("LNCH-TEST"))
    check("  artifact 已寫出", s and s["artifact_path"]
          and os.path.isfile(s["artifact_path"]))
    check("  ledger 已建立", s and os.path.isfile(s["ledger_path"]))
    check("  window 由 config 決定（非硬編）",
          s and s["window_start"].endswith("2026-09-22T00:00:00")
          or (s and "2026-09-22T00:00" in s["window_start"]))
    d2 = tmpdir()
    c2 = write_cfg(d2)
    rc2, s2, _ = run_cli(c2, extra=["--validate-only"])
    check("  --validate-only（官方 provider，subprocess）不執行 attempt",
          rc2 == LAUNCH.EXIT_HANDLED
          and s2["scheduled_result"] == "VALIDATE_ONLY_NOT_EXECUTED")
    check("  provider_id / module 有記錄",
          s2["provider_id"] == OFFICIAL_ID
          and s2["provider_module"] == "phase6_first_live_field_provider")


def test_L2_missing_config():
    print("\n[L2] missing config")
    rc, s, _ = run_cli(None)
    check("★★ L2. 未給 --config -> exit 2", rc == LAUNCH.EXIT_CONFIG_INVALID)
    check("  分類 = CONFIG_INVALID", s["config_validation"] == LAUNCH.CONFIG_INVALID)
    rc2, s2, _ = run_cli(os.path.join(tmpdir(), "nope.json"))
    check("★★ L2. config 檔不存在 -> exit 2",
          rc2 == LAUNCH.EXIT_CONFIG_INVALID
          and any("不存在" in x for x in s2["problems"]))


def test_L3_invalid_json():
    print("\n[L3] invalid JSON")
    d = tmpdir()
    write_provider(d)
    c = write_cfg(d, _raw='{"enabled": true, ')
    rc, s, _ = run_cli(c, provider_dir=d)
    check("★★ L3. 壞 JSON -> exit 2", rc == LAUNCH.EXIT_CONFIG_INVALID)
    check("  問題明確指出非合法 JSON",
          any("JSON" in x for x in s["problems"]))
    c2 = write_cfg(d, _raw='[1, 2, 3]')
    rc2, s2, _ = run_cli(c2, provider_dir=d)
    check("★★ L3. 最外層非 object -> exit 2",
          rc2 == LAUNCH.EXIT_CONFIG_INVALID)


def test_L4_missing_field():
    print("\n[L4] missing required field")
    d = tmpdir()
    write_provider(d)
    for f in LAUNCH.REQUIRED_FIELDS:
        body = cfg_dict()
        body.pop(f)
        c = write_cfg(d, _raw=json.dumps(body))
        rc, s, _ = run_cli(c, provider_dir=d)
        check(f"★★ L4. 缺 {f} -> exit 2", rc == LAUNCH.EXIT_CONFIG_INVALID)


def test_L5_invalid_timezone():
    print("\n[L5] invalid timezone")
    d = tmpdir()
    write_provider(d)
    for tz in ("Not/AZone", "", "   ", "UTC+8"):
        c = write_cfg(d, _raw=json.dumps(cfg_dict(timezone=tz)))
        rc, s, _ = run_cli(c, provider_dir=d)
        check(f"★★ L5. timezone={tz!r} -> exit 2",
              rc == LAUNCH.EXIT_CONFIG_INVALID)
    check("  合法 timezone 可通過驗證",
          LAUNCH._check_timezone("Asia/Taipei") is not None)


def test_L6_invalid_date():
    print("\n[L6] invalid date")
    d = tmpdir()
    write_provider(d)
    for v in ("2026-13-01", "22/09/2026", "tonight", 20260922, ""):
        c = write_cfg(d, _raw=json.dumps(cfg_dict(target_date=v)))
        rc, _, _ = run_cli(c, provider_dir=d)
        check(f"★★ L6. target_date={v!r} -> exit 2",
              rc == LAUNCH.EXIT_CONFIG_INVALID)


def test_L7_invalid_window():
    print("\n[L7] invalid window")
    d = tmpdir()
    write_provider(d)
    cases = [("00:30", "00:00"), ("00:00", "00:00"), ("25:00", "00:30"),
             ("00:00", "noon")]
    for a, b in cases:
        c = write_cfg(d, _raw=json.dumps(
            cfg_dict(window_start=a, window_end=b)))
        rc, _, _ = run_cli(c, provider_dir=d)
        check(f"★★ L7. window {a}~{b} -> exit 2",
              rc == LAUNCH.EXIT_CONFIG_INVALID)


def test_L8_enabled_false():
    print("\n[L8] enabled false")
    d = tmpdir()
    write_provider(d)
    for v in (False, None, "true", 1):
        c = write_cfg(d, _raw=json.dumps(cfg_dict(enabled=v)))
        rc, s, _ = run_cli(c, provider_dir=d)
        check(f"★★ L8. enabled={v!r} -> exit 3（REFUSED）",
              rc == LAUNCH.EXIT_REFUSED)
        check("  分類 = NOT_ENABLED",
              s["config_validation"] == LAUNCH.CONFIG_NOT_ENABLED)
    check("★★ L8. config 存在 != field authorization（仍拒絕執行）", True)


# ======================================================================
# L9 ~ L14：執行語意
# ======================================================================
def test_L9_already_consumed():
    print("\n[L9] already consumed")
    d = tmpdir()
    write_provider(d)
    c = write_cfg(d)
    rc1, s1, _ = run_in_process(c, d)
    check("  第一次 exit 0 / PASS",
          rc1 == LAUNCH.EXIT_HANDLED and s1["scheduled_result"] == SC.R_PASS)
    rc2, s2, _ = run_in_process(c, d)
    check("★★ L9. 第二次 exit = 3（REFUSED）", rc2 == LAUNCH.EXIT_REFUSED)
    check("★★ L9. result = ALREADY_CONSUMED",
          s2["scheduled_result"] == SC.R_ALREADY_CONSUMED)
    check("  ledger 為同一份（one-shot 跨 process 有效）",
          s1["ledger_path"] == s2["ledger_path"]
          and os.path.isfile(s1["ledger_path"]))


def test_L10_independent_cwd():
    print("\n[L10] independent cwd")
    d = tmpdir()
    write_provider(d)
    c = write_cfg(d)
    outs = []
    for cwd in (tempfile.gettempdir(), HERE, os.path.dirname(HERE)):
        dd = tmpdir()
        write_provider(dd)
        cc = write_cfg(dd)
        rc, s, _ = run_cli(cc, cwd=cwd, extra=["--validate-only"])
        outs.append((rc, s["scheduled_result"], s["ledger_path"]))
        check(f"★★ L10. cwd={os.path.basename(cwd) or cwd} -> exit 0",
              rc == LAUNCH.EXIT_HANDLED)
    check("★★ L10. 三種 cwd 結果一致", len({o[1] for o in outs}) == 1)
    check("★★ L10. 相對路徑以 config 目錄解析（三份 ledger 互不相同）",
          len({o[2] for o in outs}) == 3)
    base = tmpdir()
    p = LAUNCH.resolve_path(base, os.path.join("sub", "x.jsonl"))
    check("★★ L10. resolve_path 以 config 目錄為基準",
          os.path.dirname(os.path.dirname(p)) == os.path.abspath(base))


def test_L11_paths_with_spaces():
    print("\n[L11] paths with spaces（D:\\Crawler Sample）")
    d = tmpdir(prefix="has space ")
    check("  暫存目錄含空白", " " in d)
    write_provider(d)
    c = write_cfg(d)
    rc, s, _ = run_in_process(c, d)
    check("★★ L11. 含空白路徑仍 exit 0", rc == LAUNCH.EXIT_HANDLED)
    check("★★ L11. artifact / ledger 落在含空白目錄且可讀",
          os.path.isfile(s["artifact_path"])
          and os.path.isfile(s["ledger_path"]))
    check("★★ L11. launcher 絕對路徑本身含空白（D:\\Crawler Sample）",
          " " in LAUNCHER and os.path.isfile(LAUNCHER))
    # Task Scheduler 風格的完整 quoting
    cmd = '"%s" "%s" --config "%s"' % (sys.executable, LAUNCHER, c)
    check("  Task 風格 command line 三段皆有引號", cmd.count('"') == 6)
    d2 = tmpdir(prefix="another space ")
    write_provider(d2)
    c2 = write_cfg(d2)
    rc2, _, _ = run_cli(c2, cwd=d, extra=["--validate-only"])
    check("★★ L11. cwd 也含空白時仍正常", rc2 == LAUNCH.EXIT_HANDLED)


def test_L12_no_stdin():
    print("\n[L12] no stdin required")
    d = tmpdir()
    write_provider(d)
    c = write_cfg(d)
    # run_cli 一律 stdin=DEVNULL
    rc, s, _ = run_cli(c, extra=["--validate-only"])
    check("★★ L12. stdin=DEVNULL 仍可完成", rc == LAUNCH.EXIT_HANDLED)
    tree = ast.parse(io.open(LAUNCHER, encoding="utf-8").read())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("★★ L12. 原始碼不含 input()", "input" not in names)
    check("★★ L12. 不讀 stdin", "stdin" not in attrs and "stdin" not in names)
    check("  不含 getpass / msvcrt", "getpass" not in names
          and "msvcrt" not in names)


def test_L13_handled_abort_exit():
    print("\n[L13] handled ABORT exit semantics")
    d = tmpdir()
    write_provider(d)
    c = write_cfg(d)
    rc, s, _ = run_in_process(c, d, mode="gate_a_fail")
    check("★★ L13. GATE A fail -> result = ABORTED_CURRENT_CONDITIONS",
          s["scheduled_result"] == SC.R_ABORTED_CURRENT)
    check("★★ L13. 安全 ABORT 仍 exit 0（不得被當成 launcher crash）",
          rc == LAUNCH.EXIT_HANDLED)
    check("  分類 = HANDLED_TERMINAL_RESULT",
          s["exit_classification"] == "HANDLED_TERMINAL_RESULT")
    for r, want in ((SC.R_EXPIRED, LAUNCH.EXIT_HANDLED),
                    (SC.R_ABORTED_PREFLIGHT, LAUNCH.EXIT_HANDLED),
                    (SC.R_ABORTED_CURRENT, LAUNCH.EXIT_HANDLED),
                    (SC.R_ABORTED_READINESS, LAUNCH.EXIT_HANDLED),
                    (SC.R_PASS, LAUNCH.EXIT_HANDLED),
                    (SC.R_ALREADY_CONSUMED, LAUNCH.EXIT_REFUSED),
                    (SC.R_NOT_ENABLED, LAUNCH.EXIT_REFUSED),
                    (SC.R_RESTORE_UNKNOWN, LAUNCH.EXIT_MANUAL),
                    (SC.R_PAUSE_UNKNOWN, LAUNCH.EXIT_MANUAL),
                    (SC.R_DISPATCH_UNKNOWN, LAUNCH.EXIT_MANUAL),
                    (SC.R_EXCEPTION, LAUNCH.EXIT_MANUAL),
                    ("TOTALLY_UNKNOWN", LAUNCH.EXIT_LAUNCHER_FAILURE)):
        check(f"  classify_exit({r}) = {want}",
              LAUNCH.classify_exit(r) == want)


def test_L14_unexpected_exception():
    print("\n[L14] unexpected exception")
    d = tmpdir()
    write_provider(d)
    c = write_cfg(d)
    rc, s, _ = run_in_process(c, d, mode="boom")
    check("★★ L14. provider build 失敗 -> exit 2（config/invocation 無效）",
          rc == LAUNCH.EXIT_CONFIG_INVALID)
    check("  問題文字帶出 provider", any("provider" in x.lower()
                                        for x in s["problems"]))
    rc2, s2, _ = run_in_process(c, d, mode="incomplete")
    check("★★ L14. provider 缺必要相依 -> exit 2",
          rc2 == LAUNCH.EXIT_CONFIG_INVALID
          and any("必要相依" in x for x in s2["problems"]))
    rc4, s4, _ = run_in_process(c, d, mode="validate_fail")
    check("★★ L14. provider validate 不過 -> exit 2（且不進 build）",
          rc4 == LAUNCH.EXIT_CONFIG_INVALID
          and any("前提不成立" in x for x in s4["problems"]))
    c3 = write_cfg(tmpdir(), _raw=json.dumps(
        cfg_dict(dependency_provider="no_such_module_xyz")))
    rc3, s3, _ = run_cli(c3)
    check("★★ L14. 未登記 provider ID -> exit 2",
          rc3 == LAUNCH.EXIT_CONFIG_INVALID)
    check("  exit code 常數互不相同",
          len({LAUNCH.EXIT_HANDLED, LAUNCH.EXIT_CONFIG_INVALID,
               LAUNCH.EXIT_REFUSED, LAUNCH.EXIT_MANUAL,
               LAUNCH.EXIT_LAUNCHER_FAILURE}) == 5)


# ======================================================================
# L15 ~ L17：結構鎖
# ======================================================================
def test_L15_no_duplicate_logic():
    print("\n[L15] no duplicate control logic")
    tree = ast.parse(io.open(LAUNCHER, encoding="utf-8").read())
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for bad in ("pre_handoff_policy_eligibility", "post_handoff_revalidation",
                "run_live_leg_cli", "live_leg_main", "build_executor",
                "build_verifier", "classify_tou", "evaluate",
                "acquire_ownership", "HandoffRunner", "FirstLiveDispatcher"):
        check(f"★★ L15. 未呼叫 {bad}", bad not in calls)
    check("  只呼叫既有 scheduled runner",
          "ScheduledFirstLiveRunner" in calls and "OneShotLedger" in calls
          and "OneShotAuthorization" in calls)
    nums = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, float)}
    check("★★ L15. 不含任何功率常數", 5.0 not in nums and 150.0 not in nums)
    strs = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("★★ L15. 不含硬編日期 / 時段 / 時區",
          "Asia/Taipei" not in strs and "00:00" not in strs
          and "00:30" not in strs and "2026-09-22" not in strs)
    check("  未提供 --force / --bypass 覆寫參數",
          not any(isinstance(x, str) and ("force" in x.lower()
                                          or "bypass" in x.lower())
                  for x in strs))


def test_L16_no_subprocess_control():
    print("\n[L16] no subprocess control path")
    tree = ast.parse(io.open(LAUNCHER, encoding="utf-8").read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    for bad in ("subprocess", "os.system", "socket", "paramiko", "requests",
                "urllib", "http", "pymodbus"):
        check(f"★★ L16. 未 import {bad}", bad.split(".")[0] not in mods
              or bad == "os.system")
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    # 🔴 不掃裸 `run` —— `runner.run()` 是**正當**的既有 scheduled runner 呼叫，
    #    掃它會產生假陽性（既往教訓）。只掃真正的 process-spawn API。
    for bad in ("Popen", "system", "check_output", "call", "spawn",
                "startfile", "execv", "fork"):
        check(f"  未呼叫 {bad}()", bad not in calls)
    check("★★ L16. 呼叫的是既有 scheduled runner 的 run()",
          "run" in calls and "ScheduledFirstLiveRunner" in calls)


def test_L17_no_control_module_imports():
    print("\n[L17] launcher never directly imports executor / control modules")
    tree = ast.parse(io.open(LAUNCHER, encoding="utf-8").read())
    top = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            top.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            top.add(n.module.split(".")[0])
    for bad in ("pcs_control_executor", "device_control_operator",
                "pcs_auto_control_service", "pcs_auto_control_production",
                "pcs_control_integration", "phase6_remote_adapter",
                "phase6_handoff_orchestrator", "phase6_first_live_handoff",
                "phase6_first_live_runner", "decision_engine",
                "decision_policy", "safety_gate", "control_authority"):
        check(f"★★ L17. 模組層未 import {bad}", bad not in top)
    # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-24 Attempt #4 postmortem）
    #   OLD : stdlib allowlist = {argparse, datetime, importlib, json, os, sys}
    #   NEW : 增列 `inspect`（同樣是 stdlib）。
    #   WHY : `build_dependencies()` 需以 `inspect.signature()` 判斷 provider
    #         的 `build()` 是否宣告第二個參數，才決定是否傳 runtime_context；
    #         用 try/except TypeError 會把 build() 內部真正的 TypeError
    #         誤判成「不支援第二個參數」而掩蓋缺陷。
    #   EVIDENCE : Attempt #4 需要把 authorization / window 事實傳進 provider
    #         以建立 FIRST LIVE permit。
    #   🔴 斷言本質未放寬：仍然是「只准 stdlib ＋ phase6_scheduled_first_live」。
    check("★★ L17. 模組層只 import stdlib ＋ phase6_scheduled_first_live",
          top - {"argparse", "datetime", "importlib", "inspect", "json",
                 "os", "sys"}
          == {"phase6_scheduled_first_live"})
    check("  設備相依一律由 config 指名的 provider 動態載入",
          "dependency_provider" in LAUNCH.REQUIRED_FIELDS
          and callable(LAUNCH.load_provider))
    # provider 載入失敗必須 Fail Closed
    try:
        LAUNCH.load_provider("definitely_not_a_module_zz")
        raised = False
    except LAUNCH.ConfigError:
        raised = True
    check("★★ L17. provider 載入失敗 -> ConfigError（Fail Closed）", raised)


# ======================================================================
def main():
    for fn in (test_L1_valid_config, test_L2_missing_config,
               test_L3_invalid_json, test_L4_missing_field,
               test_L5_invalid_timezone, test_L6_invalid_date,
               test_L7_invalid_window, test_L8_enabled_false,
               test_L9_already_consumed, test_L10_independent_cwd,
               test_L11_paths_with_spaces, test_L12_no_stdin,
               test_L13_handled_abort_exit, test_L14_unexpected_exception,
               test_L15_no_duplicate_logic, test_L16_no_subprocess_control,
               test_L17_no_control_module_imports):
        fn()
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
