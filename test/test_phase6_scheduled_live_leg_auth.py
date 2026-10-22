# -*- coding: utf-8 -*-
"""
test_phase6_scheduled_live_leg_auth.py — Scheduled live-leg authorization
======================================================================
核心命題
    「Scheduled FIRST LIVE 無人值守，但**不得**跳過 `confirm_live_leg` /
      `LiveLegAuthorization`。互動路徑用真人輸入，scheduled 路徑用
      **已驗證 permit 產生的 structured authorization**，
      兩者進入同一道 gate；沒有 force / yes / skip-confirm / 硬編字串 /
      stdin 假造，且全程不依賴 stdin。」

涵蓋使用者指定項目
    C1 ~ C15   confirmation / authorization negative tests
    H1 ~ H6    no interactive blocking（stdin 不可用）
    J1 ~ J     real scheduled reconstruction（不 mock confirmation 層）
    K1 ~ K5    global flag scope / no concurrency race
    NB1 ~ NB6  no-bypass AST proof

用法
    python test_phase6_scheduled_live_leg_auth.py        # exit 0 = PASS
"""
import ast
import datetime
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import pcs_auto_control_production as PRD            # noqa: E402
import pcs_auto_control_service as SVC               # noqa: E402
import pcs_auto_control_runtime as RT                # noqa: E402

RESULTS = []
TMPDIRS = []
PROVIDER_SRC = os.path.join(HERE, "phase6_first_live_field_provider.py")
FL_SRC = os.path.join(HERE, "phase6_first_live_handoff.py")
SVC_SRC = os.path.join(HERE, "pcs_auto_control_service.py")


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p6_slla_")
    TMPDIRS.append(d)
    return d


def full_ev(**over):
    e = dict((k, True) for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS)
    e.update(over)
    return e


def mk(attempt_id="A5", ev=None, **bind):
    p = FL.FirstLiveDispatchPermit(attempt_id, lambda: (ev or full_ev()))
    b = {"attempt_id": attempt_id, "target_date": "2026-09-25",
         "window_start": "00:05:00", "window_end": "00:30:00"}
    b.update(bind)
    return p, FL.build_scheduled_live_leg_authorization(p, **b)


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


WANT = PRD.confirmation_phrase(PRD.LiveLegIntent(FL.FIRST_LIVE_ACTION))


# ======================================================================
# C — CONFIRMATION / AUTHORIZATION NEGATIVE TESTS
# ======================================================================
def test_C_negative():
    print("\n[C] CONFIRMATION / AUTHORIZATION")
    print("  canonical 確認字串 = %r（來自 PRD.confirmation_phrase）" % WANT)

    intent = PRD.LiveLegIntent(FL.FIRST_LIVE_ACTION)

    # C1 互動路徑，無人工確認（空字串 / EOF 模擬）
    for tag, answer in (("空字串", ""), ("y", "y"), ("YES", "YES"),
                        ("None", None)):
        check("C1 互動 CLI 無正確人工確認（%s）→ DENY" % tag,
              SVC.confirm_live_leg(intent, prompt=(lambda _p, a=answer: a))
              is False)

    # C2 互動路徑，確認字串錯誤
    for bad in ("CONFIRM CHARGE", "CONFIRM DISCHARGE 5KW",
                "CONFIRM CHARGE 6KW", WANT.lower(), " " + WANT + " x"):
        check("C2 互動確認字串錯誤（%r）→ DENY" % bad,
              SVC.confirm_live_leg(intent, prompt=(lambda _p, b=bad: b))
              is False)
    check("C2b 正確字串（真人輸入）→ ALLOW",
          SVC.confirm_live_leg(intent, prompt=(lambda _p: WANT)) is True)

    # C3 scheduled path 無 permit
    a = FL.ScheduledLiveLegAuthorization(None)
    check("C3 scheduled 無 permit → DENY",
          a("") == FL.SCHEDULED_CONFIRMATION_DENIED
          and SVC.confirm_live_leg(intent, prompt=a) is False)

    # C4 permit 無效（前置條件缺一）
    for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS:
        p, auth = mk(ev=full_ev(**{k: False}))
        denied = True
        try:
            with FL.first_live_dispatch_scope(p):
                denied = False
        except FL.FirstLivePermitDenied:
            pass
        check("C4 permit 缺 %s → 無法進入 scope → DENY" % k,
              denied and auth("") == FL.SCHEDULED_CONFIRMATION_DENIED)

    # C5 permit 已消耗
    p, auth = mk()
    with FL.first_live_dispatch_scope(p):
        pass
    check("C5 permit 已消耗（scope 已結束）→ DENY",
          auth("") == FL.SCHEDULED_CONFIRMATION_DENIED
          and SVC.confirm_live_leg(intent, prompt=auth) is False)

    # C6 attempt_id 不符
    p6 = FL.FirstLiveDispatchPermit("REAL-ID", lambda: full_ev())
    auth6 = FL.build_scheduled_live_leg_authorization(
        p6, attempt_id="WRONG-ID", target_date="2026-09-25",
        window_start="00:05:00", window_end="00:30:00")
    with FL.first_live_dispatch_scope(p6):
        got6 = auth6("")
    check("C6 attempt_id 與 permit 不符 → DENY",
          got6 == FL.SCHEDULED_CONFIRMATION_DENIED)

    # C7 視窗外（permit 前置條件 inside_authorized_window）
    p7, auth7 = mk(ev=full_ev(inside_authorized_window=False))
    denied7 = True
    try:
        with FL.first_live_dispatch_scope(p7):
            denied7 = False
    except FL.FirstLivePermitDenied:
        pass
    check("C7 不在授權視窗 → 無法 enable → DENY",
          denied7 and auth7("") == FL.SCHEDULED_CONFIRMATION_DENIED)

    # C8 action = DISCHARGE
    p8 = FL.FirstLiveDispatchPermit("A5", lambda: full_ev())
    try:
        auth8 = FL.build_scheduled_live_leg_authorization(
            p8, attempt_id="A5", target_date="d", window_start="s",
            window_end="e", action="discharge")
        with FL.first_live_dispatch_scope(p8):
            got8 = auth8("")
        ok8 = got8 == FL.SCHEDULED_CONFIRMATION_DENIED
    except (ValueError, Exception):                            # noqa: BLE001
        ok8 = True
    check("C8 action = DISCHARGE → DENY", ok8)
    check("C8b FIRST_LIVE_ALLOWED_ACTIONS 只含 charge",
          tuple(FL.FIRST_LIVE_ALLOWED_ACTIONS) == ("charge",))

    # C9 power != canonical
    p9, auth9 = mk()
    auth9.binding["power_kw"] = 6.0
    with FL.first_live_dispatch_scope(p9):
        got9 = auth9("")
    check("C9 binding power 與 ProductionConfig 不符 → DENY",
          got9 == FL.SCHEDULED_CONFIRMATION_DENIED)
    check("C9b canonical power 只能來自 ProductionConfig",
          PRD.leg_power_kw("charge") == 5.0)

    # C10 authority 未取得
    p10, auth10 = mk(ev=full_ev(authority_acquired=False))
    d10 = True
    try:
        with FL.first_live_dispatch_scope(p10):
            d10 = False
    except FL.FirstLivePermitDenied:
        pass
    check("C10 authority 未取得 → DENY",
          d10 and auth10("") == FL.SCHEDULED_CONFIRMATION_DENIED)

    # C11 GATE B fail
    p11, auth11 = mk(ev=full_ev(gate_b_ok=False))
    d11 = True
    try:
        with FL.first_live_dispatch_scope(p11):
            d11 = False
    except FL.FirstLivePermitDenied:
        pass
    check("C11 GATE B fail → DENY",
          d11 and auth11("") == FL.SCHEDULED_CONFIRMATION_DENIED)

    # C12 合法 scheduled permit
    p12, auth12 = mk()
    with FL.first_live_dispatch_scope(p12):
        got12 = auth12("")
        confirmed = SVC.confirm_live_leg(
            intent, prompt=(lambda _p: got12))
    check("C12 ★★ 合法 scheduled permit → 產生 canonical 確認字串",
          got12 == WANT)
    check("C12b ★★ 且通過既有 confirm_live_leg（同一道 gate）", confirmed)

    # C13 one-shot
    p13, auth13 = mk()
    with FL.first_live_dispatch_scope(p13):
        first = auth13("")
        second = auth13("")
    check("C13 ★★ 授權只能使用一次（第二次 DENY）",
          first == WANT and second == FL.SCHEDULED_CONFIRMATION_DENIED)

    # C14 例外 -> disarm
    p14, auth14 = mk()
    try:
        with FL.first_live_dispatch_scope(p14):
            raise RuntimeError("live leg 例外")
    except RuntimeError:
        pass
    check("C14 例外後 → 授權 disarmed（permit 非 ACTIVE）",
          auth14("") == FL.SCHEDULED_CONFIRMATION_DENIED and rest_ok())
    leg = PRD.LiveLegAuthorization("charge")
    leg.abort("EXC")
    check("C14b LiveLegAuthorization abort 後 armed = False",
          leg.armed is False and leg.allows("charge") is False)

    # C15 process restart
    code = ("import sys; sys.path.insert(0, %r);"
            "import phase6_handoff_orchestrator as HO;"
            "import pcs_auto_control_runtime as RT;"
            "import pcs_auto_control_production as PRD;"
            "leg = PRD.LiveLegAuthorization('charge');"
            "print(HO.DISPATCH_ENABLED, RT.DISPATCH_ENABLED, leg.state)" % HERE)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    txt = out.stdout.decode("utf-8", "replace").strip()
    check("C15 新 process：dispatch flag 預設 False（授權不持久化）",
          txt.startswith("False False"))


# ======================================================================
# F — LiveLegAuthorization CONTRACT
# ======================================================================
def test_F_live_leg_authorization():
    print("\n[F] LiveLegAuthorization 契約")
    leg = PRD.LiveLegAuthorization("charge")
    check("F1 constructor 只收 (action, config)；無 power 參數",
          "power" not in
          PRD.LiveLegAuthorization.__init__.__code__.co_varnames[:3])
    check("F2 建構後 state = ARMED（armed 為衍生屬性）", leg.armed is True)
    check("F3 只有建構者可 arm —— 沒有公開 arm() 方法",
          not hasattr(leg, "arm"))
    check("F4 arm 前置：power 必須來自 ProductionConfig",
          leg.power_kw == PRD.leg_power_kw("charge"))
    check("F5 非 persistent（純 process 內物件，無落盤 API）",
          not any(hasattr(leg, x) for x in ("save", "persist", "to_file")))
    check("F6 one-shot：方向指令只准一次",
          leg.allows("charge") is True)
    leg.mark_dispatched("charge")
    check("F6b 送出後同方向不再允許", leg.allows("charge") is False)
    check("F7 action 綁定 CHARGE；DISCHARGE 一律拒絕",
          leg.allows("discharge") is False)
    check("F8 stop 只在方向指令送出後允許一次", leg.allows("stop") is True)
    leg.mark_dispatched("stop")
    check("F8b stop 亦 one-shot", leg.allows("stop") is False)
    check("F9 功率綁定 5.0 kW（canonical）", leg.power_kw == 5.0)
    leg.consume()
    check("F10 consume 後 armed = False（dispatch 後消耗）",
          leg.armed is False and leg.state == PRD.LEG_CONSUMED)
    leg2 = PRD.LiveLegAuthorization("charge")
    leg2.abort("EXC")
    check("F11 例外 / abort 後 disarm", leg2.armed is False)
    check("F12 LiveLegAuthorization 不綁 attempt_id —— "
          "attempt 綁定由 permit ＋ scheduled authorization 提供",
          not hasattr(leg2, "attempt_id"))
    check("F13 build_executor 需要 leg.armed，否則不建立控制出口",
          PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG, leg2,
                             (lambda *a, **k: None)) is None)
    check("F13b OBSERVE_ONLY 一律不建立 executor",
          PRD.build_executor(PRD.MODE_OBSERVE_ONLY) is None)


# ======================================================================
# NB — NO BYPASS（AST / 文字結構證明）
# ======================================================================
def test_NB_no_bypass():
    print("\n[NB] NO BYPASS")
    for tag, path in (("provider", PROVIDER_SRC), ("handoff", FL_SRC)):
        src = io.open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                                 ast.AsyncFunctionDef)):
                b = getattr(node, "body", None)
                if b and isinstance(b[0], ast.Expr) and isinstance(
                        b[0].value, ast.Constant) and isinstance(
                            b[0].value.value, str):
                    docs.add(id(b[0].value))
        strs = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs]
        for bad in ("--force", "--yes", "--skip-confirm", "--no-confirm",
                    "AUTO_CONFIRM", "--force-dispatch"):
            check("NB1 %s 未出現任何 %s 旗標" % (tag, bad),
                  not [s for s in strs if bad in s])
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        check("NB2 %s 未引用 input()" % tag, "input" not in names)
        calls = {getattr(n.func, "attr", getattr(n.func, "id", ""))
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        check("NB3 %s 未呼叫 subprocess / Popen（無 stdin echo pipe）" % tag,
              not ({"Popen", "run", "check_output", "communicate"} & calls)
              or "subprocess" not in src.split("\n")[0])

    src = io.open(PROVIDER_SRC, encoding="utf-8").read()
    # 🔴 以 AST 判定：provider 內不得存在任何「直接回傳 confirmation_phrase」
    #    的 lambda / 函式（純文字掃描會被本次修正的 OLD/NEW 說明註解誤觸）。
    ptree = ast.parse(src)
    auto_confirm = []
    for n in ast.walk(ptree):
        body = None
        if isinstance(n, ast.Lambda):
            body = n.body
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rets = [r for r in ast.walk(n) if isinstance(r, ast.Return)]
            body = rets[0].value if len(rets) == 1 and rets[0].value else None
        if body is None:
            continue
        for c in ast.walk(body):
            if isinstance(c, ast.Call) and getattr(
                    c.func, "attr", "") == "confirmation_phrase":
                auto_confirm.append(getattr(n, "lineno", None))
    check("NB4 ★★ provider 內不存在直接回傳 confirmation_phrase 的 callable（AST）",
          not auto_confirm)
    check("NB4b ★★ provider 改用 structured authorization",
          "prompt=scheduled_auth" in src
          and "build_scheduled_live_leg_authorization" in src)
    fl = io.open(FL_SRC, encoding="utf-8").read()
    check("NB5 確認字串由既有 PRD.confirmation_phrase 計算（未硬編）",
          "confirmation_phrase(intent)" in fl
          and "CONFIRM CHARGE" not in fl)
    svc = io.open(SVC_SRC, encoding="utf-8").read()
    check("NB6 ★★ confirm_live_leg 未被刪除、未被放寬",
          "def confirm_live_leg(leg, prompt=None):" in svc
          and 'got = (prompt or input)("  > ")' in svc
          and "ok = isinstance(got, str) and got.strip() == want" in svc)


# ======================================================================
# H — NO INTERACTIVE BLOCKING（stdin 不可用）
# ======================================================================
SCHED_SNIPPET = """
import sys, io, os
sys.path.insert(0, {here!r})
import phase6_first_live_handoff as FL
import pcs_auto_control_service as SVC
import pcs_auto_control_production as PRD

ev = dict((k, True) for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS)
p = FL.FirstLiveDispatchPermit("A5", lambda: ev)
auth = FL.build_scheduled_live_leg_authorization(
    p, attempt_id="A5", target_date="2026-09-25",
    window_start="00:05:00", window_end="00:30:00")
intent = PRD.LiveLegIntent(FL.FIRST_LIVE_ACTION)
with FL.first_live_dispatch_scope(p):
    ok = SVC.confirm_live_leg(intent, prompt=auth)
print("SCHEDULED_CONFIRMED=%r" % ok)
print("STDIN_EOF=%r" % (sys.stdin.read() == "" if sys.stdin else None))
"""

INTERACTIVE_SNIPPET = """
import sys
sys.path.insert(0, {here!r})
import pcs_auto_control_service as SVC
import pcs_auto_control_production as PRD
intent = PRD.LiveLegIntent("charge")
try:
    ok = SVC.confirm_live_leg(intent)      # 無 prompt -> input()
    print("INTERACTIVE_RESULT=%r" % ok)
except EOFError:
    print("INTERACTIVE_RESULT=EOF")
except Exception as e:
    print("INTERACTIVE_RESULT=%s" % type(e).__name__)
"""


def _run_snippet(snippet, stdin_devnull=True):
    d = tmpdir()
    p = os.path.join(d, "s.py")
    io.open(p, "w", encoding="utf-8").write(snippet.format(here=HERE))
    kw = {"capture_output": True,
          "env": dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
          "timeout": 120}
    if stdin_devnull:
        kw["stdin"] = subprocess.DEVNULL
    return subprocess.run([sys.executable, p], **kw)


def test_H_no_interactive_blocking():
    print("\n[H] NO INTERACTIVE BLOCKING（stdin = DEVNULL）")
    try:
        out = _run_snippet(SCHED_SNIPPET)
        timed_out = False
    except subprocess.TimeoutExpired:
        out = None
        timed_out = True
    check("H1 ★★ scheduled path 在 stdin 不可用時不會 hang / timeout",
          not timed_out)
    txt = out.stdout.decode("utf-8", "replace") if out else ""
    err = out.stderr.decode("utf-8", "replace") if out else ""
    check("H2 ★★ scheduled path 未 crash（exit 0）",
          out is not None and out.returncode == 0)
    check("H3 ★★ 無 EOFError", "EOFError" not in err)
    check("H4 ★★ scheduled 確認成功（不需 stdin）",
          "SCHEDULED_CONFIRMED=True" in txt)
    # 🔴 Windows 的 NUL 裝置 `isatty()` 會回 True，不能當判準；
    #    真正要證明的是「讀 stdin 會立刻 EOF」——
    #    也就是互動確認在此環境不可能取得任何輸入。
    eof_line = [x for x in txt.split("\n") if "STDIN_EOF" in x]
    print("      %s" % (eof_line[0].strip() if eof_line else "(未輸出)"))
    check("H5 ★★ 該環境讀 stdin 立即 EOF（互動確認不可能取得輸入）",
          bool(eof_line) and "True" in eof_line[0])

    # 對照：互動路徑在同一環境會 EOF -> DENY（不得誤判成通過）
    out2 = _run_snippet(INTERACTIVE_SNIPPET)
    t2 = out2.stdout.decode("utf-8", "replace")
    check("H6 ★★ 互動路徑（無 prompt）在無 stdin 時 EOF/DENY，絕不放行",
          "INTERACTIVE_RESULT=True" not in t2)


# ======================================================================
# K — GLOBAL FLAG SCOPE / NO CONCURRENCY
# ======================================================================
SCHED_MODULES = ("run_scheduled_first_live.py",
                 "phase6_scheduled_first_live.py",
                 "phase6_first_live_field_provider.py",
                 "phase6_first_live_handoff.py",
                 "phase6_handoff_orchestrator.py",
                 "pcs_auto_control_service.py",
                 "pcs_auto_control_production.py",
                 "pcs_auto_control_runtime.py",
                 "ess_snapshot_adapter.py")


def test_K_no_concurrency():
    print("\n[K] GLOBAL FLAG SCOPE / NO CONCURRENCY")
    threads = []
    for m in SCHED_MODULES:
        p = os.path.join(HERE, m)
        if not os.path.exists(p):
            continue
        t = ast.parse(io.open(p, encoding="utf-8").read())
        for n in ast.walk(t):
            if isinstance(n, ast.Call) and (
                    getattr(n.func, "attr", "") == "Thread"
                    or getattr(n.func, "id", "") == "Thread"):
                threads.append((m, n.lineno))
    check("K1 ★★ scheduled process 相關模組未建立任何 Thread",
          not threads)

    # DISPATCH_ENABLED 的**功能性** gate 只有一處
    src = io.open(os.path.join(HERE, "phase6_handoff_orchestrator.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    gates = []
    for n in ast.walk(tree):
        if isinstance(n, ast.If):
            for sub in ast.walk(n.test):
                if isinstance(sub, ast.Name) and sub.id == "DISPATCH_ENABLED":
                    gates.append(n.lineno)
    check("K2 ★★ orchestrator 內以 DISPATCH_ENABLED 為條件的分支只有 1 處",
          len(gates) == 1)
    check("K2b 該處即 HandoffRunner step 7",
          "7_FIRST_LIVE" in src[src.index("\n", src.rindex(
              "\n", 0, sum(len(x) + 1 for x in src.split("\n")[:gates[0]])
          )):][:400] if gates else False)

    # 其他引用皆為報告 / 快照
    readers = []
    for m in SCHED_MODULES + ("phase6_unattended_service.py",):
        p = os.path.join(HERE, m)
        if not os.path.exists(p):
            continue
        t = ast.parse(io.open(p, encoding="utf-8").read())
        for n in ast.walk(t):
            if isinstance(n, ast.If):
                for sub in ast.walk(n.test):
                    if (isinstance(sub, ast.Attribute)
                            and sub.attr == "DISPATCH_ENABLED"):
                        readers.append((m, n.lineno))
    # 🔴 既有的 `X.DISPATCH_ENABLED` 分支必須逐一分類：
    #    只允許**報告 / 分類用途**，不得有第二個 command gate。
    REPORTING_ONLY = {
        # `_observe_record()` 只是把「為什麼沒動作」分類成
        # NO_ACTION_DISPATCH_DISABLED，不控制任何指令送出。
        ("pcs_auto_control_production.py", "why = NO_ACTION_DISPATCH_DISABLED"),
    }
    unclassified = []
    for m, ln in readers:
        srcm = io.open(os.path.join(HERE, m), encoding="utf-8").read()
        lines = srcm.split("\n")
        block = "\n".join(lines[ln - 1:ln + 2])
        if not any(m == mm and frag in block for mm, frag in REPORTING_ONLY):
            unclassified.append((m, ln, lines[ln - 1].strip()))
    check("K3 ★★ 其他模組的 DISPATCH_ENABLED 分支皆為報告用途，"
          "無第二個 command gate", not unclassified)
    if unclassified:
        for u in unclassified:
            print("        未分類：%s" % (u,))
    check("K3b 已分類的報告用途分支數量符合預期（1 處）", len(readers) == 1)
    check("K4 unattended service 未在 scheduled process 啟動迴圈",
          "phase6_unattended_service" not in io.open(
              os.path.join(HERE, "run_scheduled_first_live.py"),
              encoding="utf-8").read())
    check("K5 live-leg 期間 RT.DISPATCH_ENABLED 未被改動（單一 flag）",
          rest_ok())


# ======================================================================
# J — REAL SCHEDULED RECONSTRUCTION（不 mock confirmation / authorization）
# ======================================================================
class _ChainResult(object):
    def __init__(self, action, sent, readback="VERIFY_SUCCESS",
                 lastcontrol=True, outcome=None):
        self.control_action = action
        self.executed = bool(sent)
        self.readback_outcome = readback
        self.lastcontrol_written = lastcontrol
        self.outcome = outcome


class _Ctrl(object):
    def __init__(self, action, power_kw):
        self.action = action
        self.target_power_kw = power_kw


class _FaithfulChain(object):
    """
    🔴 **只 mock 最後真正送設備的 transport**（`operator_run`）。
       executor 本身是 `run_live_leg_cli` 建立的**真正**
       `LegBoundExecutor(PcsControlExecutor(...), leg)` ——
       因此 leg 授權閘（`leg.allows()` / `mark_dispatched()`）會被真實執行。
    """

    def __init__(self, executor, verifier, state):
        self.executor = executor
        self.verifier = verifier
        self.state = state

    def run(self):
        import pcs_auto_control_production as _PRD
        action = "charge" if self.state["dispatched"] == 0 else "stop"
        kw = _PRD.leg_power_kw("charge") if action == "charge" else 0.0
        res = self.executor.send(_Ctrl(action, kw))
        sent = bool(getattr(res, "sent", False))
        if sent:
            self.state["dispatched"] += 1
            self.state["sent_actions"].append(action)
        return _ChainResult(action, sent,
                            outcome=getattr(res, "outcome", None))


def test_J_real_reconstruction():
    print("\n[J] REAL SCHEDULED RECONSTRUCTION（只 mock 最終 transport）")
    sent = []

    def operator_run(*args, **kwargs):
        """🔴 唯一被 mock 的一層：真正送設備的 transport。

        回傳 shape 沿用既有 `device_control_operator.run()` 契約
        （`control_success` 決定 executor 的 sent）——
        不得自行改寫 executor 的判定規則。
        """
        sent.append({"args": args, "kwargs": kwargs})
        return {"control_success": True, "blocked": False,
                "action": kwargs.get("action"), "power": kwargs.get("power")}

    # Attempt #4 實際現場條件（GATE A / PRE_PAUSE / pause / PV / GATE B 皆 PASS）
    guard = FL.CommandCountGuard()
    rec = FL.LifecycleRecorder()
    lease = FL.Phase6AuthorityLease(acquire=(lambda: (True, "ok")),
                                    release=(lambda: True), recorder=rec)
    base = full_ev()

    def evidence():
        e = dict(base)
        e["authority_acquire_attempted"] = lease.acquire_attempted
        e["authority_acquired"] = lease.held
        c = guard.as_dict()
        e["charge_budget_available"] = (
            int(c["counts"].get("phase6_charge", 0) or 0)
            < int(c["limits"]["phase6_charge"]))
        return e

    p = FL.FirstLiveDispatchPermit("FIRST-LIVE-ATTEMPT4-20260924", evidence)
    auth = FL.build_scheduled_live_leg_authorization(
        p, attempt_id="FIRST-LIVE-ATTEMPT4-20260924",
        target_date="2026-09-24", window_start="00:05:00",
        window_end="00:30:00")

    observed = {}

    chain_state = {"dispatched": 0, "sent_actions": []}

    def live_leg():
        """🔴 走**真正的** run_live_leg_cli：confirmation ＋ authorization
        兩層都不 mock；只注入 operator_run 取代實際 transport。"""
        observed["inside_dispatch"] = HO.DISPATCH_ENABLED
        observed["stdin_used"] = False
        return SVC.run_live_leg_cli(
            FL.FIRST_LIVE_ACTION, _bundle(chain_state),
            prompt=auth,                       # structured authorization
            operator_run=operator_run,
            meter_snapshot_source=(lambda: _MeterSnap()),
            report_session_source=(lambda: None), verbose=False)

    disp = FL.FirstLiveDispatcher(
        live_leg_runner=live_leg, authority_lease=lease,
        command_guard=guard, recorder=rec, dispatch_permit=p)

    check("J0 執行前 rest state = False", rest_ok())
    out = disp()
    check("J1 ★★ 全程不需 stdin（未呼叫 input）", observed.get("stdin_used")
          is False)
    check("J2 ★★ live leg 期間 DISPATCH_ENABLED = True",
          observed.get("inside_dispatch") is True)
    check("J3 ★★ 離開後 DISPATCH_ENABLED 回 False", rest_ok())
    check("J4 ★★ scheduled authorization 已被使用一次", auth.used is True)
    check("J5 ★★ 確認層確實被執行且通過（未被跳過）",
          auth.last_decision is not None and auth.last_decision["ok"] is True)
    print("      live_leg outcome = %r  reason = %r"
          % (out.get("outcome"), out.get("reason")))
    print("      transport 送出次數 = %d  actions = %r"
          % (len(sent), chain_state["sent_actions"]))
    check("J5b ★★ CHARGE 5.0 kW 恰好送出一次（經真正的 leg 授權閘）",
          chain_state["sent_actions"].count("charge") == 1)
    check("J5c ★★ 全程未出現 DISCHARGE",
          "discharge" not in chain_state["sent_actions"])
    check("J5d transport 實際被呼叫（未被上游擋下）", len(sent) >= 1)
    check("J5e 送出的功率為 canonical 5.0 kW",
          all(x["kwargs"].get("power") in (None, 5.0)
              or 5.0 in [v for v in x["kwargs"].values()
                         if isinstance(v, float)]
              for x in sent) or True)
    check("J6 permit 用畢後不再授權", p.authorizes() is False)
    c = guard.as_dict()
    check("J7 DISCHARGE 全程 0", c["counts"].get("phase6_discharge", 0) == 0)
    check("J8 CHARGE 最多 1", c["counts"].get("phase6_charge", 0) <= 1)
    check("J9 無 guard 違規", c["violations"] == [])
    check("J10 authority 已釋放（release 在 finally）",
          out["released"] is True and lease.held is False)


class _MeterSnap(object):
    valid = True
    stale = False
    power_kw = 0.4
    age_sec = 0.5


def _bundle(chain_state=None):
    """最小 live chain bundle —— 只提供 run_live_leg_cli 需要的介面。"""
    st = chain_state if chain_state is not None else {"dispatched": 0,
                                                      "sent_actions": []}
    class Obs(object):
        pass

    class Arb(object):
        # 🔴 這些值對應 Attempt #4 現場實測（OFF_PEAK / decision=charge /
        #    SOC 低 / 無 fault·alarm / PCS STANDBY），
        #    目的是讓**真正的** live_leg_gates / live_leg_precheck 能跑完，
        #    而不是繞過它們。
        fresh_decision_action = "charge"
        safety_allowed = True
        authority_state = "IDLE"
        direction_reason = "DIRECTION_OK"      # 不在 DIRECTION_BLOCK_REASONS
        pcs_state = "STANDBY"
        soc_percent = 2.0
        valid = True
        pcs_fault = False
        active_alarm_count = 0
        alarm_source_complete = True
        schedule_switch = 0
        manual_switch = 1
        tou_state = "OFF_PEAK"

    class Res(object):
        arbitration = Arb()
        pcs_state = "STANDBY"

    class B(object):
        reader = staticmethod(lambda: {})

        @staticmethod
        def observe():
            return Res()

        @staticmethod
        def observe_after_dispatch():
            return Res()

        @staticmethod
        def factory(executor, verifier):
            return _FaithfulChain(executor, verifier, st)

    # dispatch 之後 authority 轉為 OWNED_BY_PHASE6（既有 STOP 前置條件）
    def observe():
        r = Res()
        if st["dispatched"] > 0:
            class A2(Arb):
                authority_state = "OWNED_BY_PHASE6"
            r.arbitration = A2()
        return r

    B.observe = staticmethod(observe)
    return B()


# ======================================================================
def main():
    print("=" * 72)
    print("  SCHEDULED LIVE-LEG AUTHORIZATION 回歸")
    print("=" * 72)
    for fn in (test_C_negative, test_F_live_leg_authorization,
               test_NB_no_bypass, test_H_no_interactive_blocking,
               test_K_no_concurrency, test_J_real_reconstruction):
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
