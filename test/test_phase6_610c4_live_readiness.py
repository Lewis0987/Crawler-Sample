# -*- coding: utf-8 -*-
"""
test_phase6_610c4_live_readiness.py — Phase 6.10-C4 Live Readiness Package
======================================================================
核心命題
    「B2 guard 必須自己回報能力、Windows 端不得替它猜；舊 B1 沒有這些欄位
      要判成 NOT_REPORTED 而不是 false；capability=true 也不能跳過 identity
      驗證；而在目前的現場 checkpoint 下，live readiness 必定 BLOCKED。」

是否需要設備
    **不需要**，而且本檔不連線。B2 guard 的行為測試以本機 bash 執行
    **路徑改寫過的副本**＋stub 二進位檔，不碰任何遠端主機。
    真正部署中的腳本另以靜態比對驗證。

用法
    python test_phase6_610c4_live_readiness.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import ast
import stat
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO         # noqa: E402
import phase6_remote_adapter as RA               # noqa: E402
import phase6_unattended_service as SVC          # noqa: E402
import phase6_guard_deploy_plan as DEP           # noqa: E402

RESULTS = []
_TMP = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p610c4_")
    _TMP.append(d)
    return d


def _bash():
    """Git Bash 優先 —— 直接用 'bash' 會命中 WSL 的 bash.exe。"""
    for p in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files\Git\usr\bin\bash.exe", "/usr/bin/bash"):
        if os.path.exists(p):
            return p
    return shutil.which("bash") or "bash"


# ======================================================================
# B2 guard 的本機可執行副本（路徑改寫 + stub 二進位）
# ======================================================================
STUBS = {
    "screen": "#!/bin/sh\ncase \"$*\" in\n  *-ls*) echo '\t1140.auto\t(Detached)';;\n  *) echo \"screen $*\" >> \"$STUB_LOG\";;\nesac\nexit 0\n",
    "pgrep": "#!/bin/sh\necho 1140\n",
    "ps": "#!/bin/sh\nif [ \"${1:-}\" = '-o' ] && [ \"${2:-}\" = 'cmd=' ]; then echo \"$STUB_CMD\"; else echo 1129; fi\nexit 0\n",
    # 🔴 guard 呼叫的是 `readlink -f /proc/<pid>/cwd` → 要看**最後一個**參數
    "readlink": "#!/bin/sh\nfor a in \"$@\"; do L=\"$a\"; done\ncase \"$L\" in\n  */cwd) echo \"$STUB_CWD\";;\n  */exe) echo /usr/bin/python3.10;;\n  *) echo '';;\nesac\nexit 0\n",
    "timeout": "#!/bin/sh\nshift\nexec \"$@\"\n",
}


def build_guard(variant="B2", screens=1, cwd="/home/etica/ems",
                cmd="python3 auto_control.py"):
    """
    產生一份**可在本機跑**的 guard 副本：把 /usr/bin/<x> 改寫到 stub 目錄。

    🔴 這是測試 harness，不是部署品。真正的腳本仍以 `REMOTE_GUARD_B2_SH`
       為準，並由 test_5 做靜態驗證，確保 harness 的改寫沒有掩蓋問題。
    """
    d = tmpdir()
    bindir = os.path.join(d, "bin")
    os.makedirs(bindir)
    stubs = dict(STUBS)
    if screens != 1:
        # printf 會解讀 \n，因此輸出的是真正的多行（不是字面 backslash-n）
        body = "".join("\\t114%d.auto\\t(Detached)\\n" % i
                       for i in range(screens))
        stubs["screen"] = ("#!/bin/sh\ncase \"$*\" in\n  *-ls*) printf '"
                           + body + "';;\n  *) : ;;\nesac\nexit 0\n")
    for name, body in stubs.items():
        p = os.path.join(bindir, name)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP
                 | stat.S_IXOTH)

    src = (RA.REMOTE_GUARD_B2_SH if variant == "B2" else RA.REMOTE_GUARD_B1_SH)
    bp = bindir.replace("\\", "/")
    # 只改寫「工具路徑」，不動任何邏輯
    for tool in ("screen", "pgrep", "ps", "readlink", "timeout"):
        src = src.replace("/usr/bin/%s\n" % tool, "%s/%s\n" % (bp, tool))
    src = src.replace("PATH=/usr/bin:/bin", "PATH=%s:/usr/bin:/bin" % bp)
    gp = os.path.join(d, "guard.sh")
    with io.open(gp, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    return gp, {"STUB_LOG": os.path.join(d, "stub.log").replace("\\", "/"),
                "STUB_CWD": cwd, "STUB_CMD": cmd}


def run_guard(gp, env, verb):
    e = dict(os.environ)
    e.update(env)
    e["SSH_ORIGINAL_COMMAND"] = verb
    r = subprocess.run([_bash(), gp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=e, timeout=60)
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def stub_log(env):
    p = env["STUB_LOG"]
    return io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""


# ======================================================================
# 1. B2 capability fields 正確
# ======================================================================
def test_1_capability_fields():
    print("\n[1] B2 probe 回報 capability metadata")
    gp, env = build_guard("B2")
    rc, out, err = run_guard(gp, env, "probe")
    check(f"  probe exit=0（stderr={err.strip()[:40]}）", rc == 0)
    check("  輸出含 guard_variant=B2", "guard_variant=B2" in out)
    for v in RA.CAPABILITY_VERBS:
        check(f"  含 capability_{v}=true", f"capability_{v}=true" in out)
    cap = RA.parse_capability(out)
    check("★★ parse_capability → CAPABILITY_REPORTED",
          cap.status == RA.CAP_REPORTED)
    check("  variant=B2 且五項能力齊全",
          cap.variant == "B2" and len(cap.capabilities) == 5
          and all(cap.capabilities.values()))
    ok, ch = RA.capability_gate(cap)
    check("★★ capability_gate 全過", ok and all(ch.values()))

    # 🔴 metadata 必須來自 guard 自己的 allowlist，不是硬編字串
    src = RA.REMOTE_GUARD_B2_SH
    check("★★ capability 由 ALLOWED_VERBS 迴圈產生（非逐行硬寫 true）",
          "verb_allowed" in src and "for v in $KNOWN_VERBS" in src
          and "capability_$v=true" in src)
    check("★★ 同一份 ALLOWED_VERBS 同時決定 dispatch 與回報",
          src.count("ALLOWED_VERBS") >= 3
          and "if ! verb_allowed \"$VERB\"" in src)
    check("  probe 分支第一件事就是 emit_capabilities",
          re.search(r"probe\)\s*\n\s*emit_capabilities", src) is not None)


# ======================================================================
# 2. B1 無 capability fields → NOT_REPORTED
# ======================================================================
def test_2_b1_compat():
    print("\n[2] B1 相容：沒有 capability 欄位 → NOT_REPORTED")
    gp, env = build_guard("B1")
    rc, out, _ = run_guard(gp, env, "probe")
    check(f"  B1 probe exit=0", rc == 0)
    check("  B1 輸出**不含**任何 capability 欄位",
          "capability_" not in out and "guard_variant" not in out)
    cap = RA.parse_capability(out)
    check("★★ 判為 CAPABILITY_NOT_REPORTED", cap.status == RA.CAP_NOT_REPORTED)
    check("★★ **不是**自動假設 false 或 true（capabilities 為空）",
          cap.capabilities == {} and cap.variant is None)
    check("  can(pause) / can(restore) 皆 False（不放行）",
          not cap.can("pause") and not cap.can("restore"))
    check("★★ 但唯讀診斷仍允許（不因缺欄位就自斷觀測）",
          RA.readonly_diagnosis_allowed(cap) is True)
    ok, ch = RA.capability_gate(cap)
    check("★★ live capability gate FAIL", not ok
          and ch["capability_reported"] is False)
    # 真實現場 B1 的 probe 輸出（C3 實測格式）
    field = ("screen_count=1\npid=1140\ncwd=/home/etica/ems\n"
             "exe=/usr/bin/python3.10\ncmd=python3 auto_control.py\nppid=1129")
    check("  對 C3 實測到的 B1 輸出同樣判 NOT_REPORTED",
          RA.parse_capability(field).status == RA.CAP_NOT_REPORTED)


# ======================================================================
# 3. malformed capability → Fail Closed
# ======================================================================
def test_3_malformed():
    print("\n[3] malformed → Fail Closed")
    full = ("guard_variant=B2\ncapability_probe=true\ncapability_loopcheck=true"
            "\ncapability_status=true\ncapability_pause=true\n"
            "capability_restore=true\n")
    cases = {
        "值大小寫變化": full.replace("capability_pause=true",
                                     "capability_pause=TRUE"),
        "值非布林": full.replace("capability_pause=true",
                                 "capability_pause=yes"),
        "缺一項": full.replace("capability_restore=true\n", ""),
        "重複回報": full + "capability_pause=false\n",
        "variant 未知": full.replace("guard_variant=B2", "guard_variant=B3"),
        "variant 兩次": full + "guard_variant=B2\n",
        "欄位有多餘空白": full.replace("capability_pause=true",
                                       "capability_pause = true"),
        "尾隨字元": full.replace("capability_pause=true",
                                 "capability_pause=true;"),
        "無輸出": None,
    }
    for name, out in cases.items():
        cap = RA.parse_capability(out)
        ok, _ = RA.capability_gate(cap)
        check(f"★★ {name} → {cap.status}，gate FAIL",
              cap.status == RA.CAP_MALFORMED and not ok)
    check("★★ MALFORMED 連唯讀診斷都要存疑（輸出本身不可信）",
          RA.readonly_diagnosis_allowed(
              RA.parse_capability(cases["值非布林"])) is False)
    check("  完整合法輸出才 REPORTED",
          RA.parse_capability(full).status == RA.CAP_REPORTED)


# ======================================================================
# 4/5. capability_pause / capability_restore = false → Live refused
# ======================================================================
def test_4_5_live_refused():
    print("\n[4/5] capability false → LIVE REFUSED")
    base = ("guard_variant=B2\ncapability_probe=true\ncapability_loopcheck=true"
            "\ncapability_status=true\ncapability_pause=%s\n"
            "capability_restore=%s\n")
    for pv, rv in (("false", "true"), ("true", "false"), ("false", "false")):
        cap = RA.parse_capability(base % (pv, rv))
        check(f"  pause={pv} restore={rv} → 解析成功（不是 malformed）",
              cap.status == RA.CAP_REPORTED)
        ok, ch = RA.capability_gate(cap)
        check(f"★★ pause={pv} restore={rv} → capability gate FAIL", not ok)
        # 本機已是 B2，因此每個動詞各自依 guard 的回報判定（不再一律 False）
        check(f"  pause={pv} → remote_pause_capable = "
              f"{SVC.remote_pause_capable(cap)}",
              SVC.remote_pause_capable(cap) is (pv == "true"))
        check(f"  restore={rv} → remote_restore_capable = "
              f"{SVC.remote_restore_capable(cap)}",
              SVC.remote_restore_capable(cap) is (rv == "true"))
        allowed, g = SVC.live_handoff_allowed(
            HO.MODE_ARMED, HO.R_CLEAN, True,
            {"screen_alive": True, "process_alive": True,
             "process_identity_ok": True},
            health=SVC.HEALTH_OK, capability=cap)
        check(f"★★ live_handoff_allowed = False", allowed is False)

    # 2026-09-07：現場已部署 B2，因此「本機紀錄 + guard 自報」兩個條件同時成立
    good = RA.parse_capability(base % ("true", "true"))
    check("★★ 本機紀錄 B2 且 guard 自報 B2 全能力 → capability 條件成立",
          RA.DEPLOYED_GUARD_VARIANT == "B2"
          and SVC.remote_pause_capable(good) is True
          and SVC.remote_restore_capable(good) is True)
    check("★★ 但缺少 capability report 時仍 Fail Closed（雙重確認未被拿掉）",
          SVC.remote_pause_capable(None) is False
          and SVC.remote_restore_capable(None) is False)
    check("★★ guard 自報 pause=false 時，本機是 B2 也不放行",
          SVC.remote_pause_capable(
              RA.parse_capability(base % ("false", "true"))) is False)
    check("★★ live gate 的 remote_guard_b2 不只看本機常數",
          SVC.live_handoff_allowed(HO.MODE_ARMED, HO.R_CLEAN, True,
                                   {"screen_alive": True,
                                    "process_alive": True,
                                    "process_identity_ok": True},
                                   health=SVC.HEALTH_OK,
                                   capability=None)[1]["remote_guard_b2"]
          is False)
    check("  未提供 capability → 三項相關 gate 全 False（Fail Closed）",
          not any(SVC.live_handoff_allowed(
              HO.MODE_ARMED, HO.R_CLEAN, True,
              {"screen_alive": True, "process_alive": True,
               "process_identity_ok": True}, health=SVC.HEALTH_OK)[1][k]
              for k in ("remote_guard_b2", "pause_capable",
                        "restore_capable")))


# ======================================================================
# 6. capability=true 但 identity mismatch → REFUSED
# ======================================================================
def test_6_identity_gate():
    print("\n[6] identity gate —— capability=true 不得跳過")
    src = RA.REMOTE_GUARD_B2_SH
    for verb in ("pause", "restore", "status", "loopcheck"):
        m = re.search(r"\n  %s\)\n(.*?)\n    ;;" % verb, src, re.S)
        body = m.group(1) if m else ""
        i_single = body.find("require_single_screen")
        i_ident = body.find("require_identity")
        i_act = min([x for x in (body.find("stuff"), body.find("hardcopy"))
                     if x >= 0] or [10 ** 9])
        check(f"★★ {verb}：require_single_screen → require_identity → 動作",
              0 <= i_single < i_ident < i_act)
    check("★★ probe 不強制 identity（診斷用途，本來就要能看壞掉的狀態）",
          "require_identity" not in
          re.search(r"\n  probe\)\n(.*?)\n    ;;", src, re.S).group(1))
    check("★★ capability 回報**不在** identity 之後才產生 —— "
          "但也不代表能跳過 identity（兩者互不影響）",
          "emit_capabilities" in src
          and "require_identity" in src)

    # 行為驗證：四種 mismatch
    for name, kw, want_exit in (
            ("screen_count != 1", {"screens": 2}, 44),
            ("cwd mismatch", {"cwd": "/tmp"}, 45),
            ("cmdline mismatch", {"cmd": "python3 evil.py"}, 45)):
        gp, env = build_guard("B2", **kw)
        for verb in ("pause", "restore"):
            rc, out, err = run_guard(gp, env, verb)
            check(f"★★ {name} → {verb} REFUSED（exit {rc}）",
                  rc == want_exit and "injected" not in out)
            check(f"  {name} → {verb} 完全沒有 stuff 注入",
                  "stuff" not in stub_log(env))
    # identity 正常時才會走到注入
    gp, env = build_guard("B2")
    rc, out, _ = run_guard(gp, env, "pause")
    check("  identity 全部相符時 pause 才會注入（stub 環境）",
          rc == 0 and "pause_injected" in out)
    check("  且注入的是 controller 內建的 stop，不是 PCS STOP",
          "stop" in stub_log(env))


# ======================================================================
# 7. extra args / injection 全拒絕
# ======================================================================
def test_7_injection():
    print("\n[7] 精確比對 —— 任何變形一律 REFUSED")
    gp, env = build_guard("B2")
    accepted = ("probe", "status", "loopcheck", "pause", "restore")
    for v in accepted:
        rc, _, _ = run_guard(gp, env, v)
        check(f"  {v!r} 被認得（exit={rc} != 42）", rc != RA.GUARD_EXIT_REFUSED)

    rejected = [
        "probe extra", "probe -v", "probe --help",
        "Probe", "PROBE", "pRoBe",
        " probe", "probe ", "\tprobe", "probe\n",
        "probe;pause", "probe; pause", "probe && pause", "probe||pause",
        "probe|cat", "probe & pause",
        "$(pause)", "`pause`", "${pause}",
        "probe$(id)", "probe\npause",
        "cat /etc/passwd", "id", "sh", "bash -c id", "rm -rf /",
        "pause;", ";pause", "pause#", "../pause",
        "screen -X quit", "kill 1140", "",
    ]
    # 🔴 換一份乾淨的 guard/stub —— 上面的 accepted 迴圈本來就會注入，
    #    混用會讓「拒絕路徑沒有注入」這條斷言失去意義。
    gp, env = build_guard("B2")
    bad = []
    for v in rejected:
        rc, out, _ = run_guard(gp, env, v)
        # 空字串走 EX_MISSING(43)，其餘一律 EX_REFUSED(42)
        want = (RA.GUARD_EXIT_MISSING if v == "" else RA.GUARD_EXIT_REFUSED)
        if rc != want or "injected" in out:
            bad.append((v, rc))
    check(f"★★ {len(rejected)} 種變形／注入全部被拒（命中={bad}）", not bad)
    check("★★ 全程沒有任何指令被注入 screen", "stuff" not in stub_log(env))
    check("★★ guard 內不使用 eval", "eval" not in RA.REMOTE_GUARD_B2_SH
          .replace("# ", ""))
    check("  也不把原始 command 交給 sh -c",
          "sh -c" not in RA.REMOTE_GUARD_B2_SH)
    check("  Windows 端的 allowlist 判定同樣精確",
          RA.guard_would_accept("probe")
          and not RA.guard_would_accept("probe extra")
          and not RA.guard_would_accept("probe;pause"))


# ======================================================================
# 8. rollback artifact 回 B1
# ======================================================================
def test_8_rollback():
    print("\n[8] 部署 / 回滾套件")
    d = tmpdir()
    s1 = DEP.stage("B1", d)
    s2 = DEP.stage("B2", d)
    check(f"  B1 staged {s1['size']} bytes sha={s1['sha256'][:16]}…",
          os.path.exists(s1["path"]) and s1["size"] > 1000)
    check(f"  B2 staged {s2['size']} bytes sha={s2['sha256'][:16]}…",
          os.path.exists(s2["path"]) and s2["size"] > s1["size"])
    check("  兩者 sha256 不同", s1["sha256"] != s2["sha256"])
    check("★★ staged 檔為 LF 行尾（CRLF 會讓遠端 bash 直接壞掉）",
          b"\r\n" not in io.open(s2["path"], "rb").read())
    for v in ("B1", "B2"):
        p = os.path.join(d, "phase6_remote_guard_%s.sh" % v)
        r = subprocess.run([_bash(), "-n", p], capture_output=True, text=True)
        check(f"  {v} bash -n 語法檢查通過（{r.returncode}）", r.returncode == 0)
        check(f"  {v} sha256 與 guard_sha256() 一致",
              DEP.guard_sha256(v) == (s1 if v == "B1" else s2)["sha256"])

    cmds = dict(DEP.deployment_commands("B2"))
    check("  部署步驟含 backup / sha 驗證 / 語法檢查 / 原子替換",
          all(k in cmds for k in ("1_backup", "4_verify_sha", "5_syntax_check",
                                  "8_atomic_replace")))
    check("★★ 原子替換用 mv，不是直接覆寫 guard 檔",
          "mv -f" in cmds["8_atomic_replace"]
          and DEP.STAGE_PATH in cmds["8_atomic_replace"])
    check("★★ 部署流程不需要動 authorized_keys",
          DEP.AUTHORIZED_KEYS_CHANGE_REQUIRED is False
          and not any("authorized_keys" in c for c in cmds.values()))
    check(f"  權限要求 {DEP.REQUIRED_OWNER}:{DEP.REQUIRED_GROUP}"
          f" {DEP.REQUIRED_MODE}", DEP.REQUIRED_MODE == "0700")

    # 部署後驗證：第一個 capability test 不得是 pause / restore
    verbs = DEP.post_deploy_verification_verbs()
    check(f"★★ 部署後驗證只送唯讀動詞 {list(verbs)}",
          set(verbs) <= set(RA.READONLY_VERBS))
    check("★★ 第一個動作是 probe（唯讀 capability metadata）",
          verbs[0] == "probe")
    check("★★ 驗證階段明文禁止 pause / restore / stop / start",
          set(DEP.POST_DEPLOY_FORBIDDEN_VERBS)
          == {"pause", "restore", "stop", "start"}
          and not (set(verbs) & set(DEP.POST_DEPLOY_FORBIDDEN_VERBS)))

    # rollback
    rb = dict(DEP.rollback_commands("/tmp/backup.sh"))
    check("  回滾步驟含 sha 驗證 / 語法檢查 / 原子替換",
          all(k in rb for k in ("1_verify_backup_sha", "3_syntax_check",
                                "5_atomic_replace")))
    check(f"  回滾後重驗 {list(DEP.ROLLBACK_REVERIFICATION)}（皆唯讀）",
          set(DEP.ROLLBACK_REVERIFICATION) <= set(RA.READONLY_VERBS))
    check(f"  回滾觸發條件 {len(DEP.ROLLBACK_TRIGGERS)} 種",
          len(DEP.ROLLBACK_TRIGGERS) == 6
          and "capability_parse_fail" in DEP.ROLLBACK_TRIGGERS)

    # rollback 後 capability 必須回到不可用 —— 用真的 B1 guard 輸出驗
    gp, env = build_guard("B1")
    _, b1out, _ = run_guard(gp, env, "probe")
    ok, why = DEP.rollback_expected_capability(b1out)
    check(f"★★ 回滾後 B1 probe → 回滾成立（{why}）", ok)
    gp2, env2 = build_guard("B2")
    _, b2out, _ = run_guard(gp2, env2, "probe")
    ok2, why2 = DEP.rollback_expected_capability(b2out)
    check(f"★★ 若回滾後仍看到 B2 capability → 判回滾未生效（{why2}）", not ok2)
    check("  guard 自報 B1 也算回滾成立",
          DEP.rollback_expected_capability(
              "guard_variant=B1\ncapability_probe=true\n"
              "capability_loopcheck=true\ncapability_status=true\n"
              "capability_pause=false\ncapability_restore=false")[0] is True)

    # 🔴 本模組結構上不可能部署
    src = io.open(os.path.join(HERE, "phase6_guard_deploy_plan.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    check(f"★★ 部署模組未匯入 subprocess / paramiko / socket（{sorted(mods)}）",
          not (mods & {"subprocess", "paramiko", "socket", "fabric",
                       "asyncssh"}))
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("★★ 且不呼叫 run / Popen / connect —— 只回傳字串",
          not (calls & {"run", "Popen", "check_output", "connect"}))


# ======================================================================
# 9/10. readiness report
# ======================================================================
def test_9_10_readiness():
    print("\n[9/10] Live Readiness Report")
    v, rows, blocked = SVC.live_readiness_report()
    check(f"★★ 目前 field checkpoint 下 LIVE_READINESS = {v}",
          v == SVC.BLOCKED)
    # B1.7 裁示：network identity 拆成 狀態／佐證／DHCP／gate 四列（16 → 19）
    check(f"  共列 {len(rows)} 項", len(rows) == len(SVC.READINESS_ITEMS) == 19)
    check("  項目與 READINESS_ITEMS 完全對應",
          [r["item"] for r in rows] == list(SVC.READINESS_ITEMS))

    got = {r["item"]: r["value"] for r in rows}
    expect = {
        "NETWORK_IDENTITY_STABILITY": SVC.NET_ACCEPTED,
        "NETWORK_IDENTITY_EVIDENCE": SVC.NET_EVIDENCE_USER_CONFIRMED,
        "DHCP_RESERVATION": "NOT_VERIFIED",
        "NETWORK_IDENTITY_GATE": "ALLOWED",
        "REMOTE_GUARD_VARIANT": "B2",
        "REMOTE_CAPABILITY_REPORT": RA.CAP_NOT_REPORTED,
        "PAUSE_CAPABILITY": None,
        "RESTORE_CAPABILITY": None,
        "PAUSE_TIMEOUT_VERIFIED": False,
        "RESTORE_TIMEOUT_VERIFIED": False,
        "DISPATCH_ENABLED": False,
        "MODE": HO.MODE_DRY_RUN,
        "FIRST_LIVE_PREREQUISITE": False,
    }
    for k, want in expect.items():
        check(f"  {k} = {got[k]}", got[k] == want)
    check("  四個已核准 timeout 皆 OK（5.0 / 5.0 / 12.0 / 26.0）",
          all(r["ok"] for r in rows
              if r["item"] in ("SSH_CONNECT_TIMEOUT", "PROBE_TIMEOUT",
                               "STATUS_TIMEOUT", "LOOPCHECK_TIMEOUT")))
    check("  SERVICE_TIMING 已完成（C1 兩項已定案）",
          [r for r in rows if r["item"] == "SERVICE_TIMING"][0]["ok"] is True)
    for k in ("REMOTE_CAPABILITY_REPORT", "PAUSE_CAPABILITY",
              "RESTORE_CAPABILITY", "PAUSE_TIMEOUT_VERIFIED",
              "RESTORE_TIMEOUT_VERIFIED", "DISPATCH_ENABLED", "MODE",
              "FIRST_LIVE_PREREQUISITE"):
        check(f"  blocked 含 {k}", k in blocked)
    # B1.7 裁示：network identity 三列改為揭露用（non-blocking）
    check("★★ network identity 三列**不再**列入 blocked",
          not ({"NETWORK_IDENTITY_STABILITY", "NETWORK_IDENTITY_EVIDENCE",
                "DHCP_RESERVATION"} & set(blocked)))
    check("★★ 但仍如實列於 not-field-verified（未被隱藏）",
          SVC.readiness_unverified(rows)
          == ["NETWORK_IDENTITY_STABILITY", "NETWORK_IDENTITY_EVIDENCE",
              "DHCP_RESERVATION"])
    check("  REMOTE_GUARD_VARIANT 已通過（2026-09-07 B2 已部署）",
          "REMOTE_GUARD_VARIANT" not in blocked)
    check(f"★★ blocked 共 {len(blocked)} 項", len(blocked) == 9)
    txt = SVC.format_readiness(v, rows, blocked)
    check("  格式化輸出可讀且含 BLOCKED",
          "LIVE_READINESS = BLOCKED" in txt and "blocked reasons" in txt)

    # 純離線：evaluator 不得連線
    src = io.open(os.path.join(HERE, "phase6_unattended_service.py"),
                  encoding="utf-8").read()
    fn = re.search(r"def live_readiness_report\(.*?\n(?=\ndef |\nclass )",
                   src, re.S).group(0)
    check("★★ readiness evaluator 內不呼叫任何 run/probe/ssh",
          not re.search(r"\b(subprocess|Popen|ssh|scp)\b", fn))


# ======================================================================
# 11. pause/restore timeout candidate 不得被視為 verified
# ======================================================================
def test_11_candidate_not_verified():
    print("\n[11] 候選值 != 已驗證")
    t = RA.SshTimeouts()
    check("  pause / restore 在 production 預設下仍為 None",
          t.command_timeout_for("pause") is None
          and t.command_timeout_for("restore") is None)
    check("★★ status_of 仍為 STRUCTURAL_CANDIDATE",
          RA.SshTimeouts.status_of("pause")
          == RA.SshTimeouts.status_of("restore")
          == RA.TIMEOUT_STRUCTURAL_CANDIDATE)
    check("★★ C4 未把候選值升格（12.0 仍在 candidate 字典裡）",
          RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC
          == {"pause": 12.0, "restore": 12.0}
          and "pause" not in RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC)

    # 注入候選值也不能讓 readiness 的兩列變 OK
    injected = RA.SshTimeouts(
        command_timeouts=dict(RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC,
                              **RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC))
    check("  注入後 missing() 為空（值已填）", injected.missing() == [])
    _, rows, blocked = SVC.live_readiness_report(timeouts=injected)
    check("★★ 但 PAUSE_TIMEOUT_VERIFIED 仍 blocked",
          "PAUSE_TIMEOUT_VERIFIED" in blocked)
    check("★★ RESTORE_TIMEOUT_VERIFIED 亦然",
          "RESTORE_TIMEOUT_VERIFIED" in blocked)
    check("  not_field_verified() 恆列 pause / restore",
          injected.not_field_verified() == ["pause", "restore"])


# ======================================================================
# 12/13. DRY_RUN sender = 0、dispatcher = 0
# ======================================================================
def test_12_13_dry_run():
    print("\n[12/13] DRY_RUN / OBSERVE 全 0")
    good = ("guard_variant=B2\ncapability_probe=true\ncapability_loopcheck=true"
            "\ncapability_status=true\ncapability_pause=true\n"
            "capability_restore=true\n")

    class Senders(object):
        def __init__(self):
            self.pause_calls = self.restore_calls = 0

        def pause(self):
            self.pause_calls += 1
            return True

        def restore(self):
            self.restore_calls += 1
            return True

    for mode in (HO.MODE_DRY_RUN, HO.MODE_OBSERVE):
        d = tmpdir()
        sd = Senders()
        hits = []
        g = SVC.SingleInstanceGuard(os.path.join(d, "svc.lock"), pid=4444,
                                    boot_id="B", cmdline="p6",
                                    is_alive=(lambda p: False))
        s = SVC.UnattendedService(
            journal=HO.Journal(path=os.path.join(d, "j.jsonl")),
            instance_guard=g,
            # 🔴 即使 probe 回報 B2 全能力，DRY_RUN 仍不得送出任何東西
            sample_source=(lambda: {"pcs_state": "STANDBY", "ac_kw": -1.3,
                                    "authority": "IDLE", "comm_ok": True,
                                    "fault": False, "critical_alarms": 0,
                                    "fresh": True, "soc": 5.0,
                                    "soc_fresh": True, "meter_fresh": True,
                                    "ess_fresh": True, "tou": "OFF_PEAK",
                                    "decision": "charge",
                                    "external_behaviour_known": True}),
            probe_source=(lambda: {"screen_alive": True, "process_alive": True,
                                   "process_identity_ok": True,
                                   "controller_running": True,
                                   "raw_output": good}),
            loop_mark_source=(lambda: "2026-09-03 03:00:00"),
            senders=sd, dispatcher=(lambda x: hits.append(x) or {}),
            mode=mode, soc_max_pct=85.0)
        v, _ = s.boot()
        check(f"  {mode}: boot READY", v == SVC.BOOT_OK)
        res = s.run(max_ticks=8)
        check(f"★★ {mode}: capability 全 true 也全部 NO_HANDOFF",
              all(r["decision"] == "NO_HANDOFF" for r in res))
        # B2 已部署且 probe 回報全能力 → 這三個 gate 現在會放行；
        # 仍然 NO_HANDOFF，證明擋下來的是 mode / dispatch / first_live。
        check(f"  {mode}: capability 三個 gate 已放行（不是靠它們擋的）",
              res[0]["gates"]["remote_guard_b2"] is True
              and res[0]["gates"]["pause_capable"] is True
              and res[0]["gates"]["restore_capable"] is True)
        check(f"★★ {mode}: 真正擋下的是 mode / dispatch / first_live",
              res[0]["gates"]["mode_armed"] is False
              and res[0]["gates"]["dispatch_enabled"] is False
              and res[0]["gates"]["first_live_prerequisite"] is False)
        check(f"  {mode}: network identity gate 已放行（也不是靠它擋的）",
              res[0]["gates"]["network_identity_acceptable"] is True)
        check(f"★★ {mode}: pause / restore sender = 0",
              sd.pause_calls == 0 and sd.restore_calls == 0)
        check(f"★★ {mode}: dispatcher = 0", hits == [])
        check(f"  {mode}: shutdown CLEAN", s.shutdown()[0] == SVC.SHUTDOWN_CLEAN)

    check("★★ RemoteSenders 預設 armed = False", RA.RemoteSenders().armed is False)
    check("  DEPLOYED_GUARD_VARIANT = B2（2026-09-07 已部署）",
          RA.DEPLOYED_GUARD_VARIANT == "B2")
    check("  NETWORK_IDENTITY_STABILITY = ACCEPTED（不是 PASS）",
          SVC.NETWORK_IDENTITY_STABILITY == SVC.NET_ACCEPTED
          and SVC.NETWORK_IDENTITY_STABILITY != SVC.NET_PASS)


# ======================================================================
def main():
    for fn in (test_1_capability_fields, test_2_b1_compat, test_3_malformed,
               test_4_5_live_refused, test_6_identity_gate, test_7_injection,
               test_8_rollback, test_9_10_readiness,
               test_11_candidate_not_verified, test_12_13_dry_run):
        fn()
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
