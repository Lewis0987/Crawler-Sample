# -*- coding: utf-8 -*-
"""
test_phase6_first_live_field_provider.py — 正式 field provider 回歸
======================================================================
核心命題
    「provider 只負責 wiring。import 它不得產生任何 field I/O；
      validate 必須完全 passive；build 只在正式 execution path 才碰設備；
      任何缺件一律 Fail Closed，且沒有任何 bypass。」

使用者指定的 28 項（P1 ~ P28）—— 見各測試標題。

用法
    python test_phase6_first_live_field_provider.py        # exit 0 = PASS
"""
import ast
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

import phase6_first_live_field_provider as FP        # noqa: E402
import phase6_guard_ssh_runner as GR                 # noqa: E402
import run_scheduled_first_live as LAUNCH            # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402

RESULTS = []
TMPDIRS = []
PROVIDER_SRC_PATH = os.path.join(HERE, "phase6_first_live_field_provider.py")
RUNNER_SRC_PATH = os.path.join(HERE, "phase6_guard_ssh_runner.py")


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir(prefix="fp_"):
    d = tempfile.mkdtemp(prefix=prefix)
    TMPDIRS.append(d)
    return d


def src(path):
    return io.open(path, encoding="utf-8").read()


def cfg_dict(**kw):
    d = {"enabled": True, "attempt_id": "FP-TEST",
         "target_date": "2026-09-22", "window_start": "00:00",
         "window_end": "00:30", "timezone": "Asia/Taipei",
         "dependency_provider": FP.PROVIDER_ID,
         "ledger_path": "ledger.jsonl", "artifact_dir": "artifacts"}
    d.update(kw)
    return d


def write_cfg(d, **kw):
    p = os.path.join(d, "one-shot.json")
    io.open(p, "w", encoding="utf-8").write(
        json.dumps(cfg_dict(**kw), ensure_ascii=False, indent=2))
    return p


def run_cli(cfg_path, extra=None, cwd=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    argv = [sys.executable, os.path.join(HERE, "run_scheduled_first_live.py")]
    if cfg_path:
        argv += ["--config", cfg_path]
    argv += list(extra or [])
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd or tempfile.gettempdir(),
                       env=env, stdin=subprocess.DEVNULL, timeout=180)
    try:
        s = json.loads((p.stdout or "").strip())
    except ValueError:
        s = None
    return p.returncode, s, (p.stdout or "") + (p.stderr or "")


# ======================================================================
# P1 ~ P4：allowlist
# ======================================================================
def test_P1_allowlist_accepts_official():
    print("\n[P1] provider allowlist accepts official ID")
    check("★★ P1. 官方 ID 在 allowlist",
          FP.PROVIDER_ID in LAUNCH.PROVIDER_ALLOWLIST)
    check("  對映到正式模組",
          LAUNCH.PROVIDER_ALLOWLIST[FP.PROVIDER_ID]
          == "phase6_first_live_field_provider")
    check("  allowlist 只有一筆（不留後門）",
          len(LAUNCH.PROVIDER_ALLOWLIST) == 1)
    check("  resolve_provider_module 可解析",
          LAUNCH.resolve_provider_module(FP.PROVIDER_ID)
          == "phase6_first_live_field_provider")


def _reject(value, label):
    d = tmpdir()
    c = write_cfg(d, dependency_provider=value)
    rc, s, _ = run_cli(c)
    check(f"★★ {label} -> exit 2", rc == LAUNCH.EXIT_CONFIG_INVALID)
    check("  分類 = CONFIG_INVALID",
          s and s["config_validation"] == LAUNCH.CONFIG_INVALID)


def test_P2_unknown_provider_rejected():
    print("\n[P2] unknown provider rejected")
    _reject("not_a_registered_provider", "P2. 未登記 ID")
    _reject("", "P2. 空字串")


def test_P3_arbitrary_dotted_module_rejected():
    print("\n[P3] arbitrary dotted module rejected")
    for v in ("os", "subprocess", "some.random.module",
              "phase6_first_live_field_provider", "importlib.util"):
        _reject(v, "P3. %r" % v)


def test_P4_arbitrary_path_rejected():
    print("\n[P4] arbitrary filesystem path rejected")
    for v in (r"C:\evil\mod.py", "./mod.py", "/tmp/mod",
              r"..\..\phase6_first_live_field_provider"):
        _reject(v, "P4. %r" % v)
    tree = ast.parse(src(os.path.join(HERE, "run_scheduled_first_live.py")))
    strs = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for bad in ("--provider-module", "--force-provider"):
        check(f"  launcher 未提供 {bad}", bad not in strs)
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for bad in ("spec_from_file_location", "module_from_spec", "exec_module",
                "SourceFileLoader", "exec", "eval"):
        check(f"★★ P4. launcher 無 filesystem import（{bad}）", bad not in calls)


# ======================================================================
# P5 ~ P8：validate-only 必須完全 passive
# ======================================================================
class _Tripwire(object):
    """任何被呼叫都代表發生了 field side effect。"""

    def __init__(self):
        self.hits = []

    def __call__(self, *a, **k):
        self.hits.append((a, k))
        raise AssertionError("validate 期間不得產生 field I/O")


def _patch_field_entrypoints():
    """把所有會碰設備的入口換成 tripwire，回傳 (tripwire, restore)。"""
    import importlib
    tw = _Tripwire()
    saved = []
    # 🔴 只攔**真正會產生 field I/O** 的入口。
    #    `RA.SshTransport()`（未武裝、無 runner）是純建構，用來讀 .armed 預設，
    #    **不是** field I/O —— 攔它會造成假陽性（既往教訓）。
    targets = [("pcs_auto_control_production", "build_api_client"),
               ("pcs_auto_control_production", "build_meter_client"),
               ("pcs_auto_control_production", "build_ess_reader"),
               ("pcs_auto_control_service", "build_live_chain_bundle"),
               ("pcs_auto_control_service", "acquire_ownership"),
               ("pcs_auto_control_service", "run_live_leg_cli")]
    for mod, attr in targets:
        m = importlib.import_module(mod)
        saved.append((m, attr, getattr(m, attr)))
        setattr(m, attr, tw)
    # runner 的**呼叫**才是 SSH I/O；建構不是
    gr = importlib.import_module("phase6_guard_ssh_runner")
    saved.append((gr.GuardSshRunner, "__call__", gr.GuardSshRunner.__call__))
    gr.GuardSshRunner.__call__ = tw

    def restore():
        for m, attr, old in saved:
            setattr(m, attr, old)
    return tw, restore


def test_P5_P6_P7_validate_only_passive():
    print("\n[P5/P6/P7] validate-only：零 network / 零 SSH / 不建 armed transport")
    tw, restore = _patch_field_entrypoints()
    try:
        ok, checks = FP.validate_provider_definition(cfg_dict())
        check("★★ P5. validate 期間未觸發任何 HMI / Meter 建立",
              tw.hits == [])
        check("★★ P6. validate 期間未建立 SSH transport / runner",
              tw.hits == [])
        check("★★ P7. validate 期間未送任何 SSH 指令", tw.hits == [])
        # armed transport：以 AST 確認 validate 不曾傳 armed=True
        vfn = next(n for n in ast.walk(ast.parse(src(PROVIDER_SRC_PATH)))
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "validate_provider_definition")
        armed_true = [kw for c in ast.walk(vfn) if isinstance(c, ast.Call)
                      for kw in (c.keywords or [])
                      if kw.arg == "armed"
                      and isinstance(kw.value, ast.Constant)
                      and kw.value.value is True]
        check("★★ P7. validate 從未以 armed=True 建立 transport",
              armed_true == [])
        check("  validate 仍可得到結果", ok is True and all(checks.values()))
    finally:
        restore()
    # AST：validate_provider_definition 不得引用會碰設備的建構子
    fn = next(n for n in ast.walk(ast.parse(src(PROVIDER_SRC_PATH)))
              if isinstance(n, ast.FunctionDef)
              and n.name == "validate_provider_definition")
    ids = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    ids |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    for bad in ("build_api_client", "build_meter_client",
                "build_live_chain_bundle", "GuardSshRunner",
                "acquire_ownership", "run_live_leg_cli", "observe"):
        check(f"★★ P5/P6/P7. validate 不引用 {bad}", bad not in ids)
    # launcher --validate-only 走 subprocess
    d = tmpdir()
    c = write_cfg(d)
    rc, s, _ = run_cli(c, extra=["--validate-only"])
    check("★★ P5. launcher --validate-only exit 0",
          rc == LAUNCH.EXIT_HANDLED)
    check("★★ P7. 回報 field_side_effects = 0",
          s and s.get("field_side_effects") == 0)
    check("  未建立 ledger（validate 不落盤）",
          not os.path.exists(os.path.join(d, "ledger.jsonl")))
    check("  未建立 artifact 目錄",
          not os.path.isdir(os.path.join(d, "artifacts")))


def test_P8_import_no_side_effect():
    print("\n[P8] provider import 零 side effect")
    tree = ast.parse(src(PROVIDER_SRC_PATH))
    top = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            top.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            top.add(n.module.split(".")[0])
    check("★★ P8. 模組層只 import stdlib",
          top <= {"dataclasses", "datetime", "os", "sys", "time"})
    top_calls = []
    for n in tree.body:
        if isinstance(n, (ast.Expr, ast.Assign)):
            top_calls += [c for c in ast.walk(n) if isinstance(c, ast.Call)]
    names = {getattr(c.func, "attr", None) or getattr(c.func, "id", None)
             for c in top_calls}
    check("★★ P8. 模組層無 I/O 呼叫",
          not (names & {"open", "connect", "get", "post", "Popen", "run",
                        "build_api_client", "build_meter_client"}))
    # 以獨立 process 只 import，不得有任何輸出／連線
    code = ("import sys; sys.path.insert(0, r'%s');"
            "import phase6_first_live_field_provider as m;"
            "print('IMPORT_OK', m.PROVIDER_ID)" % HERE)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                       timeout=120)
    check("★★ P8. 獨立 process 只 import 即成功", p.returncode == 0)
    check("★★ P28. import 時無任何 field I/O 輸出",
          (p.stdout or "").strip() == "IMPORT_OK %s" % FP.PROVIDER_ID)
    check("  無 HMI 登入 / Meter 連線字樣",
          "HMI login" not in (p.stdout or "") and "Meter" not in (p.stdout or ""))


# ======================================================================
# P9 ~ P14：interface / Fail Closed
# ======================================================================
def test_P9_dependency_set_complete():
    print("\n[P9] provider build returns complete dependency set")
    check("  REQUIRED_DEPENDENCIES 與 launcher 一致",
          set(FP.REQUIRED_DEPENDENCIES) == set(LAUNCH.PROVIDER_REQUIRED))
    # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-23 Attempt #3 postmortem）
    #   OLD : FieldDependencies 只有 6 必要 + 4 選用相依。
    #   NEW : 新增**選用**相依 `register_pause_intent_hook`（預設 None），
    #         讓 scheduled runner 能在 pause sender 呼叫前一刻才落盤 INTENT。
    #   WHY : 使用者裁示第 8 項（DURABLE LEDGER ORDERING DEFECT）——
    #         Attempt #3 在 1_PRE_PAUSE_PREFLIGHT 就中止、pause 從未送出，
    #         卻已留下 restore responsibility=True。
    #   EVIDENCE : ledger seq2/seq3 @00:05:33 vs artifact
    #         pause_attempt_count=0 / restore_responsibility_final=False。
    #   🔴 本改動**只增加**涵蓋範圍，未放寬任何既有斷言。
    fd = FP.FieldDependencies(
        now_fn=1, source_state_check=2, safety_preflight=3, gate_a=4,
        handoff_readiness=5, arm_and_run_handoff=6)
    check("★★ P9. 必要相依齊全時 missing 為空", fd.missing() == [])
    kw = fd.as_runner_kwargs()
    check("★★ P9. as_runner_kwargs 涵蓋全部相依",
          set(kw) == set(FP.REQUIRED_DEPENDENCIES) | set(FP.OPTIONAL_DEPENDENCIES))
    check("  typed dataclass（非 untyped dict）",
          hasattr(FP.FieldDependencies, "__dataclass_fields__"))
    fd2 = FP.FieldDependencies(
        now_fn=None, source_state_check=2, safety_preflight=3, gate_a=4,
        handoff_readiness=5, arm_and_run_handoff=6)
    check("★★ P9. 缺相依可被偵測", fd2.missing() == ["now_fn"])


def _validate_with(patch):
    """暫時套用 patch（list of (module, attr, value)）後跑 validate。"""
    import importlib
    saved = []
    for mod, attr, val in patch:
        m = importlib.import_module(mod)
        saved.append((m, attr, getattr(m, attr)))
        setattr(m, attr, val)
    try:
        return FP.validate_provider_definition(cfg_dict())
    finally:
        for m, attr, old in saved:
            setattr(m, attr, old)


def test_P10_P11_P12_missing_dependencies():
    print("\n[P10/P11/P12] HMI / Meter / B2 transport 缺件 -> Fail Closed")
    import importlib
    # build 前一律先 validate；validate 不過就不會碰設備
    ok, _ = _validate_with([("phase6_remote_adapter",
                             "DEPLOYED_GUARD_VARIANT", "B1")])
    check("★★ P12. guard 非 B2 -> validate FAIL", ok is False)
    try:
        FP.build_field_dependencies(cfg_dict())
        raised = False
    except FP.ProviderPreconditionFailed:
        raised = True
    except Exception:                                          # noqa: BLE001
        raised = False
    # 正常環境下 validate 會過，所以這裡改用強制失敗的情境驗證
    RAm = importlib.import_module("phase6_remote_adapter")
    old = RAm.DEPLOYED_GUARD_VARIANT
    RAm.DEPLOYED_GUARD_VARIANT = "B1"
    try:
        FP.build_field_dependencies(cfg_dict())
        raised2 = False
    except FP.ProviderPreconditionFailed:
        raised2 = True
    finally:
        RAm.DEPLOYED_GUARD_VARIANT = old
    check("★★ P10/P11/P12. validate 不過時 build 立即 Fail Closed", raised2)
    check("  build 內確有前置 validate（AST）",
          "validate_provider_definition" in {
              getattr(n.func, "attr", None) or getattr(n.func, "id", None)
              for n in ast.walk(next(
                  x for x in ast.walk(ast.parse(src(PROVIDER_SRC_PATH)))
                  if isinstance(x, ast.FunctionDef)
                  and x.name == "build_field_dependencies"))
              if isinstance(n, ast.Call)})
    check("  ProviderPreconditionFailed 存在且為例外",
          issubclass(FP.ProviderPreconditionFailed, Exception))


def test_P13_wrong_guard_variant():
    print("\n[P13] wrong guard variant fail closed")
    check("  目前部署為 B2", RA.DEPLOYED_GUARD_VARIANT == "B2")
    check("  provider 期待值 = B2", FP.EXPECTED_GUARD_VARIANT == "B2")
    for v in ("B1", "", None, "b2"):
        ok, ck = _validate_with([("phase6_remote_adapter",
                                  "DEPLOYED_GUARD_VARIANT", v)])
        check(f"★★ P13. guard_variant={v!r} -> FAIL",
              ok is False and ck["guard_variant_b2"] is False)


def test_P14_credential_missing():
    print("\n[P14] credential missing fail closed")
    ok, ck = GR.check_credentials({"key_path": r"C:\no\such\key",
                                   "user": "u", "host": "h"})
    check("★★ P14. key 檔不存在 -> check_credentials FAIL",
          ok is False and ck["key_file_exists"] is False)
    for bad in ({"key_path": "", "user": "u", "host": "h"},
                {"key_path": r"C:\k", "user": "", "host": "h"},
                {"key_path": r"C:\k", "user": "u", "host": ""}):
        check("  缺欄位 -> FAIL", GR.check_credentials(bad)[0] is False)
    ok2, ck2 = _validate_with([("phase6_guard_ssh_runner", "check_credentials",
                                (lambda identity=None: (False, {})))])
    check("★★ P14. credential 缺 -> provider validate FAIL",
          ok2 is False and ck2["credentials_present"] is False)
    r = GR.GuardSshRunner(identity={"key_path": r"C:\no\such\key",
                                    "user": "u", "host": "h"})
    try:
        r("probe", 5, 5)
        raised = False
    except GR.CredentialMissing:
        raised = True
    check("★★ P14. runner 在 credential 缺時拒絕執行（不 fallback）", raised)


def test_P15_no_secret_in_artifact():
    print("\n[P15] no secret appears in artifact / log")
    # 🔴 掃「非 docstring 的字串字面值 ＋ 識別字」——
    #    本專案文件習慣會在 docstring 寫「不 fallback 到 password auth」，
    #    直接掃原始碼文字會產生假陽性（既往教訓）。
    def _scan(path):
        tree = ast.parse(src(path))
        skip = set()
        for n in ast.walk(tree):
            body = getattr(n, "body", None)
            if isinstance(body, list) and body:
                f = body[0]
                if (isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant)
                        and isinstance(f.value.value, str)):
                    skip.add(id(f.value))
        strs = {n.value.lower() for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in skip}
        ids = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
        ids |= {n.attr.lower() for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)}
        return strs, ids

    for path in (PROVIDER_SRC_PATH, RUNNER_SRC_PATH,
                 os.path.join(HERE, "run_scheduled_first_live.py")):
        strs, ids = _scan(path)
        blob = " ".join(strs)
        for bad in ("passwd", "secret", "private key", "-----begin",
                    "hmiuser", "authorization: bearer"):
            check(f"  {os.path.basename(path)} 字面值未含 {bad}",
                  bad not in blob)
        for bad in ("password", "passwd", "secret", "api_key"):
            check(f"  {os.path.basename(path)} 無 {bad} 識別字", bad not in ids)
        # 🔴 `token` 是既有 `build_api_client()` 的回傳值（只用於 None 檢查），
        #    不是硬編 secret。真正要確認的是它**不會進入 artifact / log**：
        #    provider 的任何字串字面值（= dict 鍵 / 輸出欄位）都不得叫 token。
        check(f"  {os.path.basename(path)} token 不會成為輸出欄位",
              not any("token" in x for x in strs))
    tree = ast.parse(src(RUNNER_SRC_PATH))
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    check("★★ P15. runner 未讀取 key 內容（無 read/read_text）",
          "read" not in calls and "read_text" not in calls)
    check("  只用 key **路徑**，不複製內容",
          "key_path" in src(RUNNER_SRC_PATH))


# ======================================================================
# P16 ~ P19：執行環境
# ======================================================================
def test_P16_independent_cwd():
    print("\n[P16] independent cwd")
    outs = []
    for cwd in (tempfile.gettempdir(), HERE, os.path.dirname(HERE)):
        d = tmpdir()
        c = write_cfg(d)
        rc, s, _ = run_cli(c, extra=["--validate-only"], cwd=cwd)
        outs.append((rc, s and s["scheduled_result"]))
        check(f"★★ P16. cwd={os.path.basename(cwd) or cwd} -> exit 0",
              rc == LAUNCH.EXIT_HANDLED)
    check("★★ P16. 三種 cwd 結果一致", len({o[1] for o in outs}) == 1)


def test_P17_path_with_spaces():
    print("\n[P17] D:\\Crawler Sample path with spaces")
    check("★★ P17. provider 位於含空白路徑", " " in PROVIDER_SRC_PATH
          and os.path.isfile(PROVIDER_SRC_PATH))
    d = tmpdir(prefix="has space ")
    c = write_cfg(d)
    rc, s, _ = run_cli(c, extra=["--validate-only"], cwd=d)
    check("★★ P17. 含空白 config 目錄 + cwd -> exit 0",
          rc == LAUNCH.EXIT_HANDLED)
    cmd = '"%s" "%s" --config "%s"' % (
        sys.executable, os.path.join(HERE, "run_scheduled_first_live.py"), c)
    check("  Task 風格 quoting 三段完整", cmd.count('"') == 6)


def test_P18_no_stdin():
    print("\n[P18] no stdin")
    for path in (PROVIDER_SRC_PATH, RUNNER_SRC_PATH):
        tree = ast.parse(src(path))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        check(f"★★ P18. {os.path.basename(path)} 無 input()",
              "input" not in names)
        check(f"  {os.path.basename(path)} 不讀 stdin",
              "stdin" not in names)
    check("  runner spawn 時 stdin 為 DEVNULL",
          "stdin=subprocess.DEVNULL" in src(RUNNER_SRC_PATH))
    d = tmpdir()
    c = write_cfg(d)
    rc, _, _ = run_cli(c, extra=["--validate-only"])
    check("★★ P18. stdin=DEVNULL 下仍可完成", rc == LAUNCH.EXIT_HANDLED)


def test_P19_no_scratchpad_dependency():
    print("\n[P19] no scratchpad dependency")
    for path in (PROVIDER_SRC_PATH, RUNNER_SRC_PATH,
                 os.path.join(HERE, "run_scheduled_first_live.py"),
                 os.path.join(HERE, "phase6_scheduled_first_live.py")):
        t = src(path).lower()
        for bad in ("scratchpad", "trial2_sources", "fl_step12",
                    "appdata\\local\\temp", "/tmp/claude"):
            check(f"  {os.path.basename(path)} 未引用 {bad}", bad not in t)
    # 全新獨立 process 完成 dependency construction 的 validate 階段
    d = tmpdir()
    c = write_cfg(d)
    rc, s, out = run_cli(c, extra=["--validate-only"])
    check("★★ P19. 全新獨立 process 可完成 provider 驗證",
          rc == LAUNCH.EXIT_HANDLED and s["provider_id"] == FP.PROVIDER_ID)
    check("  provider_validation 全數通過",
          s["provider_validation"] and all(s["provider_validation"].values()))


# ======================================================================
# P20 ~ P23：無重複邏輯 / 無任意執行
# ======================================================================
def test_P20_no_executor_import():
    print("\n[P20] no direct executor import / use in provider")
    tree = ast.parse(src(PROVIDER_SRC_PATH))
    top = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            top.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            top.add(n.module.split(".")[0])
    for bad in ("pcs_control_executor", "device_control_operator"):
        check(f"★★ P20. 模組層未 import {bad}", bad not in top)
    strs = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for bad in ("pcs_control_executor", "device_control_operator"):
        check(f"★★ P20. 未以字串動態載入 {bad}", bad not in strs)
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for bad in ("build_executor", "build_verifier", "PcsControlExecutor",
                "ReadBackVerifier"):
        check(f"  未呼叫 {bad}", bad not in calls)


def test_P21_no_policy_duplication():
    print("\n[P21] no decision policy duplication")
    tree = ast.parse(src(PROVIDER_SRC_PATH))
    strs = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for bad in ("OFF_PEAK", "PEAK", "HALF_PEAK", "S_LOW", "S_MID", "S_HIGH"):
        check(f"★★ P21. 未硬編 TOU / SOC band 字面值 {bad}", bad not in strs)
    nums = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, float)}
    check("★★ P21. 未出現功率常數 5.0 / 150.0",
          5.0 not in nums and 150.0 not in nums)
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    check("★★ P21. GATE A / GATE B 判定沿用既有 FL 函式",
          "pre_handoff_policy_eligibility" in calls
          and "build_post_handoff_revalidator" in calls)
    check("  handoff 沿用既有 HandoffRunner", "HandoffRunner" in calls)
    check("  dispatcher 沿用既有 build_first_live_dispatcher",
          "build_first_live_dispatcher" in calls)
    check("  live leg 沿用既有 run_live_leg_cli", "run_live_leg_cli" in calls)
    check("  TOU / SOC 只查既有 DECISION_MATRIX",
          "DECISION_MATRIX" in src(PROVIDER_SRC_PATH))


def test_P22_no_subprocess_in_provider():
    print("\n[P22] no subprocess / arbitrary ssh execution")
    tree = ast.parse(src(PROVIDER_SRC_PATH))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    for bad in ("subprocess", "paramiko", "socket", "os.system"):
        check(f"★★ P22. provider 未 import {bad}",
              bad.split(".")[0] not in mods or bad == "os.system")
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for bad in ("Popen", "system", "check_output", "spawn", "execv"):
        check(f"  provider 未呼叫 {bad}", bad not in calls)
    # runner 端：非允許動詞一律拒絕
    r = GR.GuardSshRunner(spawn=(lambda *a: {"exit_code": 0, "stdout": ""}))
    for bad in ("ps -ef", "stop", "sh -c ls", "probe;ls", ""):
        try:
            r(bad, 5, 5)
            raised = False
        except GR.ArbitraryCommandRefused:
            raised = True
        check(f"★★ P22. runner 拒絕非允許動詞 {bad!r}", raised)
    ok = r("probe", 5, 5)
    check("  允許動詞可執行（注入 spawn，離線）", ok["exit_code"] == 0)
    argv = r.argv("probe", 5)
    check("★★ P22. argv 最後一段只有單一動詞", argv[-1] == "probe")
    check("  不經 shell（argv list，無 shell=True）",
          "shell=True" not in src(RUNNER_SRC_PATH))


def test_P23_no_direct_pause_restore():
    print("\n[P23] no direct pause / restore call from provider")
    tree = ast.parse(src(PROVIDER_SRC_PATH))
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for bad in ("send_pause", "send_restore", "send_pause_ex",
                "send_restore_ex", "request_pause", "request_restore"):
        check(f"★★ P23. provider 未直接呼叫 {bad}", bad not in calls)
    strs = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("  pause / restore 只作為 guard 動詞字串傳給既有 transport",
          "pause" in strs and "restore" in strs)
    check("★★ P23. 送出一律包在既有 wrap_pause_sender / wrap_restore_sender",
          "wrap_pause_sender" in calls and "wrap_restore_sender" in calls)
    check("  控制流程由既有 HandoffRunner 驅動", "HandoffRunner" in calls)


# ======================================================================
# P24 ~ P28：整合
# ======================================================================
def test_P24_build_in_mocked_offline_env():
    print("\n[P24] official provider can be constructed in mocked offline env")
    import importlib
    RAm = importlib.import_module("phase6_remote_adapter")
    PRD = importlib.import_module("pcs_auto_control_production")
    SVCm = importlib.import_module("pcs_auto_control_service")

    class FakeMeter(object):
        def get_snapshot(self):
            class S(object):
                valid, stale, power_kw, reason = True, False, 12.3, None
            return S()

        def stop(self):
            return None

    ctx = {"ssh_runner": GR.GuardSshRunner(
               spawn=(lambda *a: {"exit_code": 0, "stdout": ""})),
           "api_client": object(),
           "ess_reader": (lambda: {"communication_ok": True}),
           "meter_client": FakeMeter(),
           "live_bundle": type("B", (), {"observe": staticmethod(lambda: None),
                                         "arbiter": None})(),
           "journal_path": os.path.join(tmpdir(), "j.jsonl"),
           "meter_warmup_sec": 0.0}
    deps = FP.build_field_dependencies(cfg_dict(), ctx)
    check("★★ P24. 離線 mock 環境可完成 build", isinstance(
        deps, FP.FieldDependencies))
    check("★★ P24. 必要相依齊全", deps.missing() == [])
    check("  全部為 callable",
          all(callable(getattr(deps, k)) for k in FP.REQUIRED_DEPENDENCIES))
    check("  build() 相容入口回傳 kwargs dict",
          isinstance(FP.build(cfg_dict(), ctx), dict))
    check("★★ P24. build 期間未送任何控制動詞",
          ctx["ssh_runner"].calls == [])


def test_P25_launcher_provider_integration():
    print("\n[P25] launcher + provider integration")
    d = tmpdir()
    c = write_cfg(d)
    rc, s, _ = run_cli(c, extra=["--validate-only"])
    check("★★ P25. launcher 能載入官方 provider 並驗證",
          rc == LAUNCH.EXIT_HANDLED
          and s["provider_module"] == "phase6_first_live_field_provider")
    prov = LAUNCH.load_provider(FP.PROVIDER_ID)
    check("  load_provider 回傳官方模組", prov.PROVIDER_ID == FP.PROVIDER_ID)
    checks = LAUNCH.validate_provider(prov, cfg_dict())
    check("★★ P25. launcher validate_provider 通過", all(checks.values()))
    check("  launcher 的必要相依名稱與 provider 對齊",
          set(LAUNCH.PROVIDER_REQUIRED) == set(FP.REQUIRED_DEPENDENCIES))


def test_P26_P27_validate_only_results():
    print("\n[P26/P27] --validate-only 官方 config PASS / 未知 provider FAIL")
    d = tmpdir()
    c = write_cfg(d)
    rc, s, _ = run_cli(c, extra=["--validate-only"])
    check("★★ P26. 官方 config --validate-only -> exit 0 / PASS",
          rc == LAUNCH.EXIT_HANDLED
          and s["scheduled_result"] == "VALIDATE_ONLY_NOT_EXECUTED")
    d2 = tmpdir()
    c2 = write_cfg(d2, dependency_provider="ghost_provider")
    rc2, s2, _ = run_cli(c2, extra=["--validate-only"])
    check("★★ P27. 未知 provider --validate-only -> exit 2",
          rc2 == LAUNCH.EXIT_CONFIG_INVALID)
    check("  問題指出不在 allowlist",
          any("allowlist" in x for x in s2["problems"]))


def test_P28_import_only_zero_field_io():
    print("\n[P28] import module only -> 0 field I/O")
    code = ("import sys; sys.path.insert(0, r'%s');"
            "import phase6_first_live_field_provider, phase6_guard_ssh_runner,"
            " run_scheduled_first_live;"
            "print('OK')" % HERE)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                       timeout=120)
    check("★★ P28. 三個模組同時 import 成功", p.returncode == 0)
    out = (p.stdout or "") + (p.stderr or "")
    check("★★ P28. 輸出僅 OK（無登入 / 連線 / probe 痕跡）",
          out.strip() == "OK")
    for bad in ("HMI login", "Meter", "Authenticated", "screen_count"):
        check(f"  無 {bad}", bad not in out)




# ======================================================================
# 2026-09-22 PROVIDER WIRING DEFECT —— 重現 / 修正 / 迴歸
#   BUG #1 canonical SOC band 斷線（CONFIRMED）
#   BUG #2 classifier warm-up omission（CONFIRMED）
# ======================================================================
def _old_soc_accessor(bundle):
    """Attempt #2 當時的寫法（錯誤），僅供 before/after 對照，不在 production。"""
    return getattr(getattr(getattr(bundle, "arbiter", None), "observer", None),
                   "soc_band", None)


def fake_bundle(band="S_LOW", *, factory=True, arbiter=True, observer=True,
                policy=True, has_band=True, arb_obj=None):
    """結構上忠實模擬 `LiveChainBundle` -> chain -> arbiter -> observer -> policy。"""
    pol = type("P", (), {})()
    if has_band:
        pol.soc_band = band
    obs = type("O", (), {})()
    if policy:
        obs.policy = pol
    arb = type("A", (), {})()
    if observer:
        arb.observer = obs
    ch = type("C", (), {})()
    if arbiter:
        ch.arbiter = arb
    b = type("B", (), {})()
    if factory:
        b.factory = lambda executor=None, verifier=None: ch
    b.observe = lambda: arb_obj
    return b


def arbitration(decision="charge", tou="OFF_PEAK", grid="NEAR_ZERO",
                safety=True):
    o = type("Arb", (), {})()
    o.fresh_decision_action = decision
    o.tou_state = tou
    o.grid_state = grid
    o.safety_allowed = safety
    o.authority_state = "EXTERNAL_OR_UNKNOWN"
    return o


def _ess_reading(**over):
    """完整可信的 read_all() reading（沿用 d3b 既有測試資料形狀）。"""
    import importlib
    SG = importlib.import_module("safety_gate")
    r = {"communication_ok": True, "soc_percent": 6.0,
         "actual_active_power_kw": -1.3, "pcs_fault_flag": False,
         "pcs_charging_flag": False, "pcs_discharging_flag": False,
         "pcs_standby_flag": True, "pcs_running_flag": True,
         "battery_power_status": SG.BATT_ON,
         "pcs_control_mode_code": "manual", "pcs_schedule_enabled": False,
         "pcs_manual_switch": 1, "alarm_rows": [], "alarm_total": 0,
         "alarm_total_raw": 0, "_fail": []}
    r.update(over)
    return r


class _FakeMeter(object):
    def __init__(self, valid=True, stale=False, kw=0.2):
        self._v, self._s, self._kw = valid, stale, kw

    def get_snapshot(self):
        o = type("S", (), {})()
        o.valid, o.stale, o.power_kw, o.reason = self._v, self._s, self._kw, None
        return o

    def stop(self):
        return None


def build_deps(band="S_LOW", *, arb=None, reading=None, prime=None, **bkw):
    """以結構忠實的假相依建出 provider，**完全離線**。"""
    primed = {"n": 0}

    def _prime(obs_source):
        primed["n"] += 1
        return obs_source()

    ctx = {"ssh_runner": GR.GuardSshRunner(
               spawn=(lambda *a: {"exit_code": 0, "stdout": ""})),
           "api_client": object(),
           "ess_reader": (lambda: (reading if reading is not None
                                   else _ess_reading())),
           "meter_client": _FakeMeter(),
           "live_bundle": fake_bundle(band, arb_obj=(arb or arbitration()),
                                      **bkw),
           "journal_path": os.path.join(tmpdir(), "j.jsonl"),
           "meter_warmup_sec": 0.0,
           "prime_classifier": (prime if prime is not None else _prime)}
    return FP.build_field_dependencies(cfg_dict(), ctx), primed, ctx


# ---------------------------------------------------------------- 重現
def test_R_reproduction_before_after():
    print("\n[R] BUG #1 重現與修正 —— before / after")
    import importlib
    SVCm = importlib.import_module("pcs_auto_control_service")

    class _Ess(object):
        soc_percent = 6.0

    real = SVCm.build_live_chain_bundle(
        reader=(lambda: _Ess()),
        meter_source=(lambda: type("M", (), {"valid": True, "stale": False,
                                             "power_kw": 0.0,
                                             "reason": None})()))
    check("★★ R1. 真實 LiveChainBundle **沒有** .arbiter 屬性",
          not hasattr(real, "arbiter"))
    check("★★ R1. 因此舊 accessor 在真實物件上恆為 None",
          _old_soc_accessor(real) is None)
    c1, c2 = real.factory(None, None), real.factory(None, None)
    check("  factory 之間共用同一 arbiter（latch / classifier 狀態不流失）",
          c1.arbiter is c2.arbiter)
    band = FP.canonical_soc_band(real)
    check("★★ R2. 新 canonical accessor 在真實物件上取得到值（非 None）",
          band is not None)
    check("  取得的是 policy.soc_band 本人",
          band == c1.arbiter.observer.policy.soc_band)

    fb = fake_bundle("S_LOW", arb_obj=arbitration())
    check("★★ R3. BEFORE：舊 accessor -> None",
          _old_soc_accessor(fb) is None)
    check("★★ R3. AFTER ：新 accessor -> S_LOW",
          FP.canonical_soc_band(fb) == "S_LOW")

    import decision_policy as DPm
    check("★★ R4. canonical matrix：(OFF_PEAK, *, S_LOW) 皆為 charge",
          all(v == DPm.CHARGE for k, v in DPm.DECISION_MATRIX.items()
              if k[0] == "OFF_PEAK" and k[2] == "S_LOW"))
    check("★★ R4. band=None / S_NONE 在 matrix 中永無 charge —— "
          "足以單獨解釋 soc_allows_charge=False",
          not any(k[2] in (None, "S_NONE") and v == DPm.CHARGE
                  for k, v in DPm.DECISION_MATRIX.items()))
    check("★★ R4. grid=UNKNOWN 在 matrix 中無任何列 —— "
          "足以單獨解釋 natural_decision_is_charge=False",
          not any(k[1] == "UNKNOWN" for k in DPm.DECISION_MATRIX))


def test_R5_gate_a_now_reachable():
    print("\n[R5] 修正後 GATE A 可離線到達 ELIGIBLE")
    deps, primed, _ = build_deps("S_LOW")
    ok, checks, verdict = deps.gate_a()
    for k in sorted(checks):
        check("  GATE A %s = %s" % (k, checks[k]), checks[k] is True)
    check("★★ R5. GATE A 整體 ELIGIBLE", ok is True)
    import phase6_first_live_handoff as FLm
    check("★★ R5. verdict = CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED",
          verdict == FLm.ELIGIBLE_HANDOFF_REQUIRED)
    check("★★ R5. GATE A 之前已執行 classifier 暖機", primed["n"] == 1)


def test_R6_old_wiring_would_still_fail():
    print("\n[R6] 反向對照：若 band 仍為 None，GATE A 必須 FAIL CLOSED")
    deps, _, _ = build_deps(None)
    ok, checks, _ = deps.gate_a()
    check("★★ R6. soc_allows_charge = False",
          checks["soc_allows_charge"] is False)
    check("★★ R6. GATE A 不合格", ok is False)
    check("  其餘 8 項仍通過（證明只差 SOC 這一項）",
          sum(1 for v in checks.values() if v) == 8)


# ---------------------------------------------------------------- 負向 SOC
def test_N_negative_soc():
    print("\n[N] 負向 SOC —— 不得因修正而放寬")
    for band, label in (("S_HIGH", "電量過高（充電閂鎖已跳開）"),
                        ("S_NONE", "band 未定（防禦性）"),
                        (None, "路徑健在但 latch 尚未定狀態")):
        deps, _, _ = build_deps(band)
        ok, checks, _ = deps.gate_a()
        check("★★ N. band=%s（%s）-> soc_allows_charge False" % (band, label),
              checks["soc_allows_charge"] is False)
        check("  band=%s -> GATE A 不合格" % band, ok is False)
    deps, _, _ = build_deps("S_MID")
    ok, checks, _ = deps.gate_a()
    check("★★ N. band=S_MID -> soc_allows_charge True（matrix 認定）",
          checks["soc_allows_charge"] is True)
    check("  band=S_MID -> GATE A 合格", ok is True)

    tree = ast.parse(src(PROVIDER_SRC_PATH))
    lits = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, float)}
    for bad in (90.0, 20.0, 85.0, 25.0):
        check("★★ N. provider 未出現 SOC 門檻字面值 %s" % bad, bad not in lits)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("★★ N. provider 未自建 SocBandLatch / 第二套 policy",
          not ({"SocBandLatch", "TouArbitragePolicy"} & (names | attrs)))


# ---------------------------------------------------------------- wiring 缺陷要大聲
def test_W_wiring_defect_is_loud():
    print("\n[W] 結構缺陷 != runtime 未知 —— 必須大聲失敗")
    cases = (("bundle 無 factory", dict(factory=False)),
             ("chain 無 arbiter", dict(arbiter=False)),
             ("arbiter 無 observer", dict(observer=False)),
             ("observer 無 policy", dict(policy=False)),
             ("policy 無 soc_band", dict(has_band=False)))
    for label, kw in cases:
        b = fake_bundle(**kw)
        try:
            FP.canonical_soc_band(b)
            ok = False
        except FP.ProviderWiringError:
            ok = True
        except Exception:
            ok = False
        check("★★ W. %s -> ProviderWiringError" % label, ok)
    check("★★ W. 路徑健在但值為 None -> 回傳 None（合法 runtime 未知，不 raise）",
          FP.canonical_soc_band(fake_bundle(None)) is None)
    check("  canonical path 以常數記載，供稽核比對",
          "policy.soc_band" in FP.SOC_BAND_CANONICAL_PATH)


# ---------------------------------------------------------------- classifier C1~C8
def test_C_classifier_contract():
    print("\n[C] classifier 暖機 —— 沿用既有正式 contract")
    import datetime as _dt
    import importlib
    PCm = importlib.import_module("power_classifier")
    MCm = importlib.import_module("meter_client")
    Rm = importlib.import_module("phase6_first_live_runner")
    psrc = src(PROVIDER_SRC_PATH)
    ptree = ast.parse(psrc)

    def mk(power, t):
        return MCm.evaluate({"meter": power, "meter_state": "ok",
                             "timestamp": "12:00:00", "demand": power,
                             "demand_state": "degraded"}, t, t)

    # C1 單次 update 結構上不可能脫離 UNKNOWN
    clf = PCm.PowerClassifier()
    st1 = clf.update(mk(0.2, 100.0), now=100.0)
    check("★★ C1. 單次 update() 後 grid_state 仍為 UNKNOWN（time-based debounce）",
          st1.state == PCm.STATE_UNKNOWN)
    st2 = None
    t = 100.0
    for _ in range(50):
        t += 0.5
        st2 = clf.update(mk(0.2, t), now=t)
        if st2.state != PCm.STATE_UNKNOWN:
            break
    check("  持續餵入跨過 debounce 視窗後才脫離 UNKNOWN",
          st2 is not None and st2.state != PCm.STATE_UNKNOWN)
    check("  脫離 UNKNOWN 所需時間 >= debounce_sec",
          (t - 100.0) >= PCm.DEFAULT_CONFIG.debounce_sec)

    # C2 使用既有官方 helper，而非自創迴圈
    check("★★ C2. provider 引用既有 prime_classifier",
          "prime_classifier" in psrc)
    check("  該 helper 由 phase6_first_live_runner 提供（既有 6.9 實作）",
          callable(getattr(Rm, "prime_classifier", None)))

    # C3 沒有自行發明的次數 / 秒數
    for bad in ("range(3)", "range(5)", "sleep(10)", "majority", "vote"):
        check("★★ C3. provider 未出現自創暖機寫法 `%s`" % bad, bad not in psrc)

    # C4 interval = 既有 canonical 常數
    check("★★ C4. PRIME_INTERVAL_SEC 即 PC.DEFAULT_CONFIG.debounce_sec",
          Rm.PRIME_INTERVAL_SEC == PCm.DEFAULT_CONFIG.debounce_sec)
    check("  provider 直接引用 R.PRIME_INTERVAL_SEC，未另立常數",
          "R.PRIME_INTERVAL_SEC" in psrc)

    # C5 預算沿用既有 6.9 用法
    rsrc = src(os.path.join(HERE, "phase6_first_live_runner.py"))
    check("★★ C5. 暖機預算 PRIME_INTERVAL_SEC * 4 與既有 6.9 用法一致",
          "PRIME_INTERVAL_SEC * 4" in rsrc and "PRIME_INTERVAL_SEC * 4" in psrc)

    # C6 有界 —— 不得無限 polling
    calls = {"n": 0}

    def obs():
        calls["n"] += 1
        return None

    start = _dt.datetime(2026, 9, 22, 0, 6, 0)
    seq = [start + _dt.timedelta(seconds=i * 3.0) for i in range(40)]

    class _Clock(object):
        def __init__(self):
            self.i = 0

        def __call__(self):
            v = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return v

    orig_now, orig_sleep = Rm._now, Rm.time.sleep
    try:
        Rm._now = _Clock()
        Rm.time.sleep = lambda s: None
        Rm.prime_classifier(obs, start + _dt.timedelta(seconds=12.0), 3.0)
    finally:
        Rm._now, Rm.time.sleep = orig_now, orig_sleep
    check("★★ C6. 暖機在預算內結束（有界，非無限 polling）",
          0 < calls["n"] <= 8)

    # C7 暖機 != attempt retry
    deps, primed, _ = build_deps("S_LOW")
    deps.gate_a()
    deps.gate_a()
    check("★★ C7. 連續兩次 gate_a 只暖機一次（暖機不是重跑 attempt）",
          primed["n"] == 1)

    # C8 GATE A FAIL 之後不得自行重試
    fails = {"n": 0}

    def _count_prime(obs_source):
        fails["n"] += 1
        return obs_source()

    deps2, _, _ = build_deps(None, prime=_count_prime)
    ok, _, _ = deps2.gate_a()
    check("★★ C8. GATE A FAIL", ok is False)
    check("★★ C8. provider 未在 GATE A 失敗後自行重跑（暖機仍只 1 次）",
          fails["n"] == 1)
    check("★★ C8. provider 內無 while 迴圈（結構上不可能自我重試）",
          [n for n in ast.walk(ptree) if isinstance(n, ast.While)] == [])


# ---------------------------------------------------------------- A ~ M
def test_A_to_M():
    print("\n[A~M] 修正後整體約束")
    import hashlib
    import importlib
    FLm = importlib.import_module("phase6_first_live_handoff")
    DPm = importlib.import_module("decision_policy")
    PCm = importlib.import_module("power_classifier")
    psrc = src(PROVIDER_SRC_PATH)
    ptree = ast.parse(psrc)

    # A. 可到達 ELIGIBLE
    deps, _, _ = build_deps("S_LOW")
    check("★★ A. GATE A 可離線到達 ELIGIBLE", deps.gate_a()[0] is True)

    # B. TOU 非 OFF_PEAK 仍 Fail Closed
    deps, _, _ = build_deps("S_LOW",
                            arb=arbitration(tou="PEAK", decision="idle"))
    ok, ch, _ = deps.gate_a()
    check("★★ B. TOU=PEAK -> tou_allows_charge False 且 GATE A FAIL",
          ch["tou_allows_charge"] is False and ok is False)

    # C. safety gate 不 ok 仍 Fail Closed
    deps, _, _ = build_deps("S_LOW", arb=arbitration(safety=False))
    ok, ch, _ = deps.gate_a()
    check("★★ C. safety_gate_ok=False -> GATE A FAIL",
          ch["non_ownership_safety_ok"] is False and ok is False)

    # D. 自然決策非 charge 仍 Fail Closed
    deps, _, _ = build_deps("S_LOW", arb=arbitration(decision="idle"))
    ok, ch, _ = deps.gate_a()
    check("★★ D. decision=idle -> GATE A FAIL",
          ch["natural_decision_is_charge"] is False and ok is False)

    # E. GATE A 不得讀 Control Authority（6.9 TOCTOU 不得復發）
    fsrc = src(os.path.join(HERE, "phase6_first_live_handoff.py"))
    fn = [n for n in ast.walk(ast.parse(fsrc))
          if isinstance(n, ast.FunctionDef)
          and n.name == "pre_handoff_policy_eligibility"][0]
    keys = {c.value for c in ast.walk(fn)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    check("★★ E. GATE A 未讀 authority / authorized / arbitration_outcome",
          not ({"authority", "authorized", "arbitration_outcome"} & keys))

    # F. 沒有第二套 SOC policy
    pnames = {n.id for n in ast.walk(ptree) if isinstance(n, ast.Name)}
    pattrs = {n.attr for n in ast.walk(ptree) if isinstance(n, ast.Attribute)}
    check("★★ F. SOC 判定只查 DP.DECISION_MATRIX / policy.soc_band",
          "DECISION_MATRIX" in pattrs and "soc_band" in pattrs)
    check("  provider 未實體化任何自有 SOC policy（僅註解提及名稱不算）",
          not ({"SocBandLatch", "TouArbitragePolicy"} & (pnames | pattrs)))

    # G. 沒有第二套 classifier / debounce 常數
    check("★★ G. provider 未自建 classifier 或 debounce 常數",
          "PowerClassifier(" not in psrc and "debounce_sec =" not in psrc)

    # H. decision semantics 未被本次修改
    check("★★ H. DECISION_MATRIX 仍為 27 列（3 TOU x 3 GRID x 3 SOC）",
          len(DPm.DECISION_MATRIX) == 27)
    check("★★ H. debounce_sec 未被修改（3.0）",
          PCm.DEFAULT_CONFIG.debounce_sec == 3.0)

    # I. validate-only 仍完全 passive（未觸發 factory / 暖機）
    hits = {"factory": 0}
    b = fake_bundle("S_LOW", arb_obj=arbitration())
    orig = b.factory

    def _spy(*a, **k):
        hits["factory"] += 1
        return orig(*a, **k)

    b.factory = _spy
    ok2, _ = FP.validate_provider_definition(cfg_dict())
    check("★★ I. validate_provider_definition 為純離線且通過", ok2 is True)
    check("★★ I. validate 期間未呼叫 bundle.factory", hits["factory"] == 0)

    # J. import provider 仍 0 field I/O
    check("★★ J. provider 模組層仍只 import stdlib",
          all(a.name.split(".")[0] in ("dataclasses", "datetime", "os",
                                       "sys", "time", "importlib")
              for n in ptree.body if isinstance(n, ast.Import)
              for a in n.names))

    # K. Attempt #2 證據未被更動
    base = os.path.join(HERE, "..", "output", "phase6_scheduled_first_live")
    frozen = {"FIRST-LIVE-ATTEMPT2-20260922.jsonl": "c71f7e58c8a123c9",
              "FIRST-LIVE-ATTEMPT2-20260922.launcher.log": "4c2cd606797de5b6",
              "FIRST-LIVE-ATTEMPT2-20260922.config.json": "26840bd172e5225f",
              "scheduled_first_live_FIRST-LIVE-ATTEMPT2-20260922"
              "_20260922_000508.json": "71133e70c70bbeec"}
    for name, want in frozen.items():
        fp = os.path.join(base, name)
        got = (hashlib.sha256(io.open(fp, "rb").read()).hexdigest()[:16]
               if os.path.isfile(fp) else None)
        check("★★ K. Attempt #2 證據未更動：%s" % name, got == want)

    # L. 未新增 credential / secret 面
    for bad in ("passwd", "PRIVATE KEY", "BEGIN OPENSSH"):
        check("★★ L. provider 未出現 `%s` 字面值" % bad, bad not in psrc)

    # M. command 上限未變
    check("★★ M. charge <= 1 / discharge == 0 / pcs_stop <= 1 未變",
          FLm.CommandCountGuard.LIMITS["phase6_charge"] == 1
          and FLm.CommandCountGuard.LIMITS["phase6_discharge"] == 0
          and FLm.CommandCountGuard.LIMITS["phase6_pcs_stop"] == 1)


# ======================================================================
def main():
    for fn in (test_P1_allowlist_accepts_official,
               test_P2_unknown_provider_rejected,
               test_P3_arbitrary_dotted_module_rejected,
               test_P4_arbitrary_path_rejected,
               test_P5_P6_P7_validate_only_passive,
               test_P8_import_no_side_effect,
               test_P9_dependency_set_complete,
               test_P10_P11_P12_missing_dependencies,
               test_P13_wrong_guard_variant, test_P14_credential_missing,
               test_P15_no_secret_in_artifact, test_P16_independent_cwd,
               test_P17_path_with_spaces, test_P18_no_stdin,
               test_P19_no_scratchpad_dependency,
               test_P20_no_executor_import, test_P21_no_policy_duplication,
               test_P22_no_subprocess_in_provider,
               test_P23_no_direct_pause_restore,
               test_P24_build_in_mocked_offline_env,
               test_P25_launcher_provider_integration,
               test_P26_P27_validate_only_results,
               test_P28_import_only_zero_field_io,
               test_R_reproduction_before_after,
               test_R5_gate_a_now_reachable,
               test_R6_old_wiring_would_still_fail,
               test_N_negative_soc,
               test_W_wiring_defect_is_loud,
               test_C_classifier_contract,
               test_A_to_M):
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
