# -*- coding: utf-8 -*-
"""
test_phase6_attempt10_safety.py — Attempt #10 第一階段安全強化（C1～C7，離線）
======================================================================
核心命題
    「FIRST_LIVE_HOLD 生效時，任何 FIRST LIVE 現場動作都在**設備 I/O 與授權消耗
      之前**被拒絕；restore 只有在全部條件成立時才送出，否則 WITHHELD 並保留責任；
      先前 attempt 的責任未解除時，下一次 attempt 一律拒絕；人工確認只解除該阻擋，
      不解除 HOLD、不送 restore、不略過任何檢查。」

組別
    H  C1 FIRST_LIVE_HOLD           X  C2 交接前 external RUNNING
    W  C3 RESTORE_PRECHECK / WITHHELD / 送出前重新確認 / 30 秒時效
    J  C4 跨 attempt 阻擋 ＋ C5 人工確認
    L  C6 normal_loop_halted 必要條件
    P  C7 identity / pid / exe / ppid（只是必要條件）

是否需要設備
    **不需要**。全部 fake / 注入；runner 另有網路阻擋器。
    只有標示「HOLD 解除」的案例才在本程序內暫時解除 HOLD，finally 改回。

用法
    python test_phase6_attempt10_safety.py        # exit 0 = PASS
"""
import ast
import datetime
import glob
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

import pcs_auto_control_runtime as RT                # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402
import phase6_restore_policy as RP                   # noqa: E402
import phase6_scheduled_first_live as SC             # noqa: E402
import run_scheduled_first_live as LAUNCH            # noqa: E402
import _phase6_hold_override as HOLD                 # noqa: E402

RESULTS = []
_TMP = []
SOC_MAX = 85.0
PID = "4242"
# 🔴 GAP-6：C3 時間基準 = Asia/Taipei aware（與 TOU 同一來源）
TZ8 = datetime.timezone(datetime.timedelta(hours=8))
NOW_OK = datetime.datetime(2026, 11, 7, 0, 10, 0, tzinfo=TZ8)


def tou_bracket(t):
    """GAP-6 fixture：TOU 判定前後的時間（與 C3 同一基準，Asia/Taipei aware）。"""
    return {"before": t, "after": t, "mono_before": 0.0, "mono_after": 0.0,
            "host_before": t.replace(tzinfo=None)}


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


def tmpdir(prefix="a10_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP.append(d)
    return d


def src(name):
    return io.open(os.path.join(HERE, name), encoding="utf-8").read()


# ======================================================================
# 共用：HandoffRunner 離線裝置（模擬 external controller）
# ======================================================================
def probe(**over):
    p = {"ssh_ok": True, "screen_alive": True, "process_alive": True,
         "process_identity_ok": True, "pid": PID}
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
    def __init__(self):
        self.pause_calls = 0
        self.restore_calls = 0

    def pause(self):
        self.pause_calls += 1
        return True

    def restore(self):
        self.restore_calls += 1
        return True


class EntryPermit(object):
    """只回答 STAGE A 的最小 stub（與既有 first_live_handoff 測試同一慣例）。"""
    entry_denied_reason = None

    def allows_dispatcher_entry(self):
        return True

    def note(self, event, at=None):
        return event


class FakeLease(object):
    def __init__(self, held):
        self.held = held
        self.acquire_attempted = True


class LeaseDispatcher(object):
    """帶 lease 的 dispatcher stub：dispatched / verified / released。"""

    def __init__(self, lease_held=False):
        self.lease = FakeLease(lease_held)
        self.calls = 0

    def __call__(self, obs):
        self.calls += 1
        return {"dispatched": True, "verified": True, "released": True}


class Harness(object):
    """
    external controller 模型：pause 之前 RUNNING（時間戳前進）、
    pause 之後 NOT_RUNNING（凍結）、restore 之後 RUNNING（前進）。
    `restore_over` / `recheck_over` / `restore_probe_over` 只在
    orchestrator 進入 RESTORE_REQUESTED 之後才套用（模擬收尾時的現場變化）。
    """

    def __init__(self, pre_over=None, restore_over=None, recheck_over=None,
                 restore_probe_over=None, recheck_probe_over=None,
                 now=NOW_OK, now_fn="default", marks=None, probes=None,
                 samples=None, dispatcher="default", permit=True,
                 controller_runs_after_pause=False, quick=True, clock=None,
                 no_marks=False, paused_probe_over=None, deadline=None):
        self.sd = Senders()
        self.j = HO.Journal(path=os.path.join(tmpdir(), "j.jsonl"))
        self.o = HO.HandoffOrchestrator(
            remote=HO.RemoteController(pause_sender=self.sd.pause,
                                       restore_sender=self.sd.restore),
            observe=(lambda: {}), gates=(lambda x: (True, {})),
            journal=self.j, mode=HO.MODE_ARMED, idle_baseline_kw=-1.3,
            power_tolerance_kw=1.25, clock=clock)
        self.pre_over = pre_over or {}
        self.restore_over = restore_over or {}
        self.recheck_over = recheck_over or {}
        self.restore_probe_over = restore_probe_over or {}
        self.recheck_probe_over = recheck_probe_over
        self.paused_probe_over = paused_probe_over or {}
        self.runs_after_pause = controller_runs_after_pause
        self.marks = list(marks) if marks is not None else None
        self.probes = list(probes) if probes is not None else None
        self.samples = list(samples) if samples is not None else None
        self.tick = 0
        self.disp = (LeaseDispatcher() if dispatcher == "default"
                     else dispatcher)
        # fixture：naive 時間一律視為 Asia/Taipei（GAP-6 的 naive 拒絕另有專門測試）
        if isinstance(now, datetime.datetime) and now.tzinfo is None:
            now = now.replace(tzinfo=TZ8)
        self.tou_at = now if (now_fn == "default"
                              and isinstance(now, datetime.datetime)) else NOW_OK
        if now_fn == "default":
            nf = (lambda: now)
        elif callable(now_fn):
            def nf(_f=now_fn):
                v = _f()
                if isinstance(v, datetime.datetime) and v.tzinfo is None:
                    v = v.replace(tzinfo=TZ8)
                return v
        else:
            nf = now_fn
        self.r = HO.HandoffRunner(
            self.o, self.sample, self.probe,
            loop_mark_source=(None if no_marks else self.mark),
            dispatcher=self.disp, soc_max_pct=SOC_MAX,
            dispatch_permit=(EntryPermit() if permit else None),
            now_fn=nf, quick_sample_source=(self.quick if quick else None),
            deadline=deadline)

    def paused(self):
        return self.sd.pause_calls > 0 and self.sd.restore_calls == 0

    def running(self):
        return not self.paused() or self.runs_after_pause

    def in_restore(self):
        return self.o.state == HO.S_RESTORE_REQUESTED

    def sample(self):
        s = smp(**self.pre_over) if self.sd.pause_calls == 0 else smp()
        if self.samples:
            s = self.samples.pop(0)
        s = dict(s)
        s.setdefault("external_liveness_verdict",
                     RA.EXT_RUNNING if self.running() else RA.EXT_NOT_RUNNING)
        s.setdefault("tou_sampled_between", tou_bracket(self.tou_at))
        if self.in_restore():
            s.update(self.restore_over)
        return s

    def quick(self):
        s = smp(tou_sampled_between=tou_bracket(self.tou_at))
        if self.in_restore():
            s.update(self.restore_over)
            s.update(self.recheck_over)
        self._in_quick = True
        return s

    def probe(self):
        if self.probes:
            return self.probes.pop(0)
        p = probe()
        if self.o.state == HO.S_EXTERNAL_PAUSED:
            p.update(self.paused_probe_over)
        if self.in_restore():
            if getattr(self, "_in_quick", False) and \
                    self.recheck_probe_over is not None:
                p.update(self.recheck_probe_over)
            else:
                p.update(self.restore_probe_over)
        return p

    def mark(self):
        if self.marks is not None:
            return self.marks.pop(0) if self.marks else None
        if self.running():
            self.tick += 1
        return "2026-11-07 00:%02d:%02d" % divmod(self.tick, 60)

    def run(self):
        self.res = self.r.run()
        return self.res


def released(fn):
    """在 HOLD 暫時解除（finally 改回）的情況下執行。"""
    with HOLD.hold_released_for_offline_test():
        return fn()


# ======================================================================
# H — C1 FIRST_LIVE_HOLD（**HOLD 生效**）
# ======================================================================
def test_H_hold():
    print("\n[H] C1 FIRST_LIVE_HOLD —— 生效中")
    check("H0 ★★ HO.FIRST_LIVE_HOLD is True", HO.FIRST_LIVE_HOLD is True)
    check("H0b ★★ first_live_hold_active() 為 True",
          HO.first_live_hold_active() is True)

    # --- H1 AST：唯一定義、字面值 True ---
    tree = ast.parse(src("phase6_handoff_orchestrator.py"))
    mod_assigns = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "FIRST_LIVE_HOLD"
                           for t in n.targets)]
    check("H1 ★★ 模組層只定義一次，且字面值為 True",
          len(mod_assigns) == 1 and isinstance(mod_assigns[0].value,
                                               ast.Constant)
          and mod_assigns[0].value.value is True)
    writers = []
    for p in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        b = os.path.basename(p)
        if b.startswith("test_") or b == "_phase6_hold_override.py":
            continue
        try:
            t = ast.parse(io.open(p, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(t):
            tg = []
            if isinstance(n, ast.Assign):
                tg = n.targets
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                tg = [n.target]
            for x in tg:
                if (isinstance(x, ast.Attribute) and x.attr == "FIRST_LIVE_HOLD") \
                        or (isinstance(x, ast.Name) and x.id == "FIRST_LIVE_HOLD"
                            and b != "phase6_handoff_orchestrator.py"):
                    writers.append(b)
        if b != "phase6_handoff_orchestrator.py" and "setattr(" in \
                io.open(p, encoding="utf-8").read() and \
                "FIRST_LIVE_HOLD" in io.open(p, encoding="utf-8").read():
            writers.append(b + "(setattr)")
    check("H1b ★★ 沒有任何正式模組改寫 FIRST_LIVE_HOLD：%r" % (writers,),
          writers == [])
    imp = []
    for p in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        b = os.path.basename(p)
        if b.startswith("test_") or b.startswith("_phase6_hold"):
            continue
        try:
            t = ast.parse(io.open(p, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(t):
            if isinstance(n, ast.Import) and any(
                    a.name == "_phase6_hold_override" for a in n.names):
                imp.append(b)
            if isinstance(n, ast.ImportFrom) and n.module == \
                    "_phase6_hold_override":
                imp.append(b)
    check("H1c ★★ 正式模組不 import 測試用 HOLD 覆寫：%r" % (imp,), imp == [])
    check("H1d ★★ 不提供環境變數 / 設定檔開關",
          "environ" not in "".join(
              ast.get_source_segment(src("phase6_handoff_orchestrator.py"), n)
              or "" for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "first_live_hold_active"))

    # --- H2 新開子程序 ---
    code = ("import sys; sys.path.insert(0, %r); "
            "import phase6_handoff_orchestrator as HO; "
            "print('HOLD=%%r' %% HO.FIRST_LIVE_HOLD)" % HERE)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    check("H2 ★★ 新開子程序 import 後 FIRST_LIVE_HOLD=True",
          "HOLD=True" in (p.stdout or ""))

    # --- H3 request_pause ---
    sd = Senders()
    j = HO.Journal(path=os.path.join(tmpdir(), "j.jsonl"))
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd.pause,
                                   restore_sender=sd.restore),
        observe=(lambda: {}), gates=(lambda x: (True, {})), journal=j,
        mode=HO.MODE_ARMED)
    sent, why = o.request_pause()
    check("H3 ★★ request_pause（ARMED）被 HOLD 拒絕",
          sent is False and why == HO.HOLD_REASON)
    check("H3b ★★ pause sender 0 次", sd.pause_calls == 0)
    check("H3c ★★ 未寫 PAUSE INTENT、未標記 attempted（無恢復責任）",
          not [r for r in j.read_all() if r.get("kind") == "INTENT"]
          and o.guard.may_attempt("PAUSE"))

    # --- H4 STAGE A ---
    ok, why = HO._entry_gate_authorizes(EntryPermit())
    check("H4 ★★ STAGE A：即使 permit 允許也拒絕（HOLD）",
          ok is False and why == HO.HOLD_REASON)

    # --- H5 STAGE B ---
    ev = dict((k, True) for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS)
    pm = FL.FirstLiveDispatchPermit("A10-HOLD", lambda: ev)
    raised = False
    try:
        with FL.first_live_dispatch_scope(pm):
            pass
    except FL.FirstLivePermitDenied as e:
        raised = HO.HOLD_REASON in str(e)
    check("H5 ★★ STAGE B：16 項全 True 仍被 HOLD 拒絕", raised)
    check("H5b ★★ DISPATCH_ENABLED 未被打開", rest_ok())
    check("H5c permit 狀態記為 DENIED", pm.state == FL.PERMIT_DENIED)

    # --- H6 HandoffRunner 全流程（ARMED）---
    h = Harness()
    h.run()
    check("H6 ★★ HandoffRunner（ARMED）：pause 0 / restore 0 / dispatcher 0",
          h.sd.pause_calls == 0 and h.sd.restore_calls == 0
          and h.disp.calls == 0)
    check("H6b ★★ 未背負恢復責任", h.res["restore_responsibility"] is False)

    # --- H7 restore 不受 HOLD 管制（安全收尾不得被擋）---
    sd7 = Senders()
    o7 = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd7.pause,
                                   restore_sender=sd7.restore),
        observe=(lambda: {}), gates=(lambda x: (True, {})),
        journal=HO.Journal(path=os.path.join(tmpdir(), "j.jsonl")),
        mode=HO.MODE_ARMED)
    sent7, _ = o7.request_restore(o7._issue_restore_clearance("test"))  # GAP-5：ARMED 送出需 C3 clearance（測試以正式核發入口取得）
    check("H7 ★★ request_restore 不受 HOLD 影響（由 C3 把關）",
          sent7 is True and sd7.restore_calls == 1)


def test_H_scheduled_and_launcher():
    print("\n[H] C1 —— scheduled runner / launcher / field_measure / shadow")
    import test_phase6_scheduled_first_live as TS
    rig = TS.Rig(TS.INSIDE)
    calls = {"src": 0}
    orig = rig.source_state

    def counting():
        calls["src"] += 1
        return orig()

    ev = rig.runner(source_state_check=counting).run()
    check("H8 ★★ runner：result = FIRST_LIVE_HOLD", ev["result"]
          == SC.R_FIRST_LIVE_HOLD)
    check("H8b ★★ runner：授權**未**消耗",
          rig.ledger.consumed is False and ev.get("one_shot_consumed") is False)
    check("H8c ★★ runner：source / preflight / handoff 皆 0 次",
          calls["src"] == 0 and rig.handoff_calls == 0)
    check("H8d runner：safety reset 仍執行", rig.reset_calls == 1)
    check("H8e ★★ FIRST_LIVE_HOLD -> EXIT_REFUSED(3)",
          LAUNCH.classify_exit(SC.R_FIRST_LIVE_HOLD) == LAUNCH.EXIT_REFUSED)

    # --- launcher：build_dependencies / ledger 之前 ---
    import test_phase6_scheduled_launcher as TL
    d = tmpdir("a10l_")
    pdir = tmpdir("a10p_")
    TL.write_provider(pdir)
    cfg = TL.write_cfg(d)
    built = {"n": 0}
    orig_build = LAUNCH.build_dependencies

    def counting_build(*a, **k):
        built["n"] += 1
        return orig_build(*a, **k)

    LAUNCH.build_dependencies = counting_build
    try:
        rc, summ, _out = TL.run_in_process(cfg, pdir)
    finally:
        LAUNCH.build_dependencies = orig_build
    check("H9 ★★ launcher：exit 3（REFUSED）",
          rc == LAUNCH.EXIT_REFUSED)
    check("H9b ★★ launcher：scheduled_result = FIRST_LIVE_HOLD",
          (summ or {}).get("scheduled_result") == SC.R_FIRST_LIVE_HOLD)
    check("H9c ★★ launcher：build_dependencies 0 次（無 HMI / 電表 / SSH）",
          built["n"] == 0)
    _lp = (summ or {}).get("ledger_path")
    check("H9d ★★ launcher：ledger 檔未建立（授權未消耗）",
          bool(_lp) and not os.path.exists(_lp)
          and not os.path.exists(os.path.join(d, "ledger.jsonl"))
          and (summ or {}).get("one_shot_consumed") is False)

    # validate-only 不受影響
    old = list(sys.argv)
    try:
        import contextlib
        buf = io.StringIO()
        old_map = dict(LAUNCH.PROVIDER_ALLOWLIST)
        sys.path.insert(0, pdir)
        LAUNCH.PROVIDER_ALLOWLIST[TL.OFFICIAL_ID] = "fixture_provider"
        with contextlib.redirect_stdout(buf):
            rcv = LAUNCH.main(["--config", cfg, "--validate-only"])
    finally:
        LAUNCH.PROVIDER_ALLOWLIST.clear()
        LAUNCH.PROVIDER_ALLOWLIST.update(old_map)
        sys.argv[:] = old
        if pdir in sys.path:
            sys.path.remove(pdir)
    sv = json.loads(buf.getvalue().strip() or "{}")
    check("H10 ★★ validate-only 不受 HOLD 影響（exit 0、未執行）",
          rcv == LAUNCH.EXIT_HANDLED
          and sv.get("scheduled_result") == "VALIDATE_ONLY_NOT_EXECUTED")

    # launcher 原始碼順序：validate-only < HOLD < build_dependencies < ledger
    ls = src("run_scheduled_first_live.py")
    i_val = ls.index("if args.validate_only:")
    i_hold = ls.index("SC.FL.first_live_hold_active()")
    i_build = ls.index("deps = build_dependencies(")
    i_ledger = ls.index("SC.OneShotLedger(")
    check("H10b ★★ 原始碼順序 validate-only < HOLD < build_dependencies < ledger",
          i_val < i_hold < i_build < i_ledger)

    # --- field_measure --execute ---
    import phase6_field_measure as FM
    sentinel = {"api": 0}

    class _Boom(object):
        def __init__(self, *a, **k):
            sentinel["api"] += 1
            raise AssertionError("不得建立 ApiClient")

    fake_mod = type(sys)("api_client")
    fake_mod.ApiClient = _Boom
    saved = sys.modules.get("api_client")
    sys.modules["api_client"] = fake_mod
    err = io.StringIO()
    try:
        import contextlib
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rcm = FM.main(["--execute", "--power", "5"])
    finally:
        if saved is None:
            sys.modules.pop("api_client", None)
        else:
            sys.modules["api_client"] = saved
    check("H11 ★★ field_measure --execute：HOLD 拒絕（return 2）",
          rcm == 2 and HO.HOLD_REASON in err.getvalue())
    check("H11b ★★ field_measure --execute：未建立任何連線（ApiClient 0 次）",
          sentinel["api"] == 0)
    fs = src("phase6_field_measure.py")
    m = fs.index("def main(argv=None):")
    check("H11c ★★ HOLD 檢查只在 --execute，且位於任何連線元件 import 之前",
          fs.index("if args.execute:\n        import phase6_handoff_orchestrator",
                   m) < fs.index("from api_client import ApiClient", m))

    # --- shadow：provider 建構不被 HOLD 擋；但 pause 送不出 ---
    import test_phase6_entry_point_ordering as EP
    deps, env = EP.build_entry_env()
    check("H12 ★★ shadow：provider 建構與三道 gate 不受 HOLD 影響",
          deps is not None)
    EP.run_entry(deps, env)
    check("H12b ★★ shadow：arm_and_run 在 HOLD 下 SSH pause 0 / restore 0",
          env["ssh"].calls.count("pause") == 0
          and env["ssh"].calls.count("restore") == 0)
    check("H12c ★★ shadow：Phase 6 command 0",
          env["sent"] == [] and rest_ok())


# ======================================================================
# X — C2 交接前 external RUNNING（HOLD 解除）
# ======================================================================
def test_X_running():
    print("\n[X] C2 交接前 external loop 必須 RUNNING（HOLD 解除）")
    check("X0 ★★ PRE_PAUSE_ITEMS 含 external_loop_running",
          "external_loop_running" in HO.PRE_PAUSE_ITEMS)
    check("X0b ★★ 字面值與 remote adapter 一致",
          HO.EXT_RUNNING_VERDICT == RA.EXT_RUNNING
          and HO.EXT_NOT_RUNNING_VERDICT == RA.EXT_NOT_RUNNING)
    for v, want in ((RA.EXT_RUNNING, True), (RA.EXT_NOT_RUNNING, False),
                    (RA.EXT_UNKNOWN, False), (None, False)):
        ok, ck = HO.pre_pause_gates(smp(external_liveness_verdict=v), probe(),
                                    SOC_MAX)
        check("X1 verdict=%s -> pre-pause %s" % (v, "PASS" if want else "FAIL"),
              ok is want and ck["external_loop_running"] is want)
    check("X1b ★★ external_behaviour_known 定義未改（NOT_RUNNING 仍為 KNOWN）",
          HO.pre_pause_gates(smp(external_liveness_verdict=RA.EXT_NOT_RUNNING),
                             probe(), SOC_MAX)[1]["external_behaviour_known"])

    def scen(v):
        h = Harness(pre_over={"external_liveness_verdict": v})
        h.run()
        return h
    for v in (RA.EXT_NOT_RUNNING, RA.EXT_UNKNOWN):
        h = released(lambda: scen(v))
        check("X2 ★★ %s（人工停止 / 無法判定）-> pause 0 / restore 0"
              % v, h.sd.pause_calls == 0 and h.sd.restore_calls == 0)
        check("X2b %s -> 1_PRE_PAUSE_PREFLIGHT 中止、無責任" % v,
              h.res["restore_responsibility"] is False
              and h.res["steps"][0]["step"] == "1_PRE_PAUSE_PREFLIGHT"
              and not h.res["steps"][0]["ok"])

    # production entry：controller 交接前就停止（時間戳不前進）
    import test_phase6_entry_point_ordering as EP

    class StoppedSsh(EP.EntrySshRunner):
        def _mark(self):
            return "2026-10-02 00:05:00"

    def ep_stopped():
        deps, env = EP.build_entry_env()
        env["ssh"].__class__ = StoppedSsh
        EP.run_entry(deps, env)
        return env
    env = released(ep_stopped)
    check("X3 ★★ production entry：人工停止的 controller 不會收到 pause / start",
          env["ssh"].calls.count("pause") == 0
          and env["ssh"].calls.count("restore") == 0)


# ======================================================================
# W — C3 RESTORE_PRECHECK（HOLD 解除）
# ======================================================================
def _withheld(h):
    return (h.sd.restore_calls == 0
            and h.res["outcome"] == "RESTORE_WITHHELD"
            and h.o.state == HO.S_RESTORE_WITHHELD
            and h.res["restore_responsibility"] is True)


def test_W_restore_precheck():
    print("\n[W] C3 RESTORE_PRECHECK / WITHHELD（HOLD 解除）")
    check("W0 ★★ restore 時段 = 00:00 <= t < 05:30",
          RP.RESTORE_WINDOW_START == datetime.time(0, 0)
          and RP.RESTORE_WINDOW_END == datetime.time(5, 30))
    check("W0b ★★ P-8：只允許 STANDBY / STOPPED（不含 CHARGING）",
          RP.RESTORE_ALLOWED_PCS_STATES == ("STANDBY", "STOPPED"))
    check("W0c 時效上限 30 秒", RP.RESTORE_PRECHECK_MAX_AGE_SEC == 30.0)
    check("W0d ★★ WITHHELD 同時屬於 TERMINAL 與 PENDING_RESTORE",
          HO.S_RESTORE_WITHHELD in HO.TERMINAL_STATES
          and HO.S_RESTORE_WITHHELD in HO.PENDING_RESTORE_STATES
          and HO.TRANSITIONS[HO.S_RESTORE_WITHHELD] == set())

    # --- 正常：全部成立 -> 送一次 ---
    h = released(lambda: (lambda x: (x.run(), x)[1])(Harness()))
    check("W1 ★★ 全部條件成立 -> restore 恰好 1 次、COMPLETE",
          h.sd.restore_calls == 1 and h.res["outcome"] == "COMPLETE"
          and h.res["restore_responsibility"] is False)
    check("W1b precheck / recheck 證據寫入結果",
          h.res["restore_precheck"]["ok"] is True
          and h.res["restore_recheck"]["ok"] is True
          and set(h.res["restore_precheck"]["checks"])
          == set(HO.RESTORE_PRECHECK_ITEMS)
          and set(h.res["restore_recheck"]["checks"])
          == set(HO.RESTORE_RECHECK_ITEMS))
    for t, want in ((datetime.datetime(2026, 11, 7, 0, 0, 0), 1),
                    (datetime.datetime(2026, 11, 7, 5, 29, 59), 1)):
        h = released(lambda: (lambda x: (x.run(), x)[1])(Harness(now=t)))
        check("W1c 邊界 %s -> 送出" % t.time(), h.sd.restore_calls == want)

    cases = [
        ("05:30:00（上界不含）", {"now": datetime.datetime(2026, 11, 7, 5, 30)}),
        ("05:31", {"now": datetime.datetime(2026, 11, 7, 5, 31)}),
        ("23:59", {"now": datetime.datetime(2026, 11, 6, 23, 59)}),
        ("now_fn 未注入", {"now_fn": None}),
        ("now_fn 回傳非 datetime", {"now_fn": (lambda: "00:10")}),
        ("TOU 非離峰", {"restore_over": {"tou": "PEAK"}}),
        ("PCS DISCHARGING", {"restore_over": {"pcs_state": "DISCHARGING"}}),
        ("PCS CHARGING（P-8）", {"restore_over": {"pcs_state": "CHARGING"}}),
        ("PCS 狀態 None", {"restore_over": {"pcs_state": None}}),
        ("fault", {"restore_over": {"fault": True}}),
        ("fault None", {"restore_over": {"fault": None}}),
        ("critical alarm", {"restore_over": {"critical_alarms": 1}}),
        ("通訊失敗", {"restore_over": {"comm_ok": False}}),
        ("資料不 fresh", {"restore_over": {"fresh": False}}),
        ("authority UNKNOWN", {"restore_over": {"authority": "UNKNOWN"}}),
        ("authority EXTERNAL", {"restore_over":
                                {"authority": "EXTERNAL_OR_UNKNOWN"}}),
        ("authority PHASE6", {"restore_over": {"authority": "OWNED_BY_PHASE6"}}),
        ("lease 仍持有", {"dispatcher": LeaseDispatcher(lease_held=True)}),
        ("probe ssh_ok None", {"restore_probe_over": {"ssh_ok": None}}),
        ("probe process_alive None", {"restore_probe_over":
                                      {"process_alive": None}}),
        ("identity False", {"restore_probe_over":
                            {"process_identity_ok": False}}),
        ("pid 改變", {"restore_probe_over": {"pid": "9999"}}),
        ("external 已 RUNNING（有人先恢復）", {"restore_over":
                                          {"external_liveness_verdict":
                                           RA.EXT_RUNNING}}),
        ("external UNKNOWN", {"restore_over": {"external_liveness_verdict":
                                               RA.EXT_UNKNOWN}}),
    ]
    for tag, kw in cases:
        h = released(lambda: (lambda x: (x.run(), x)[1])(Harness(**kw)))
        check("W2 ★★ %s -> restore 0、WITHHELD、責任保留" % tag, _withheld(h))

    # --- 送出前重新確認 ---
    for tag, kw in (
            ("recheck 出現故障", {"recheck_over": {"fault": True}}),
            ("recheck 出現告警", {"recheck_over": {"critical_alarms": 2}}),
            ("recheck PCS 轉 DISCHARGING",
             {"recheck_over": {"pcs_state": "DISCHARGING"}}),
            ("recheck pid 改變", {"recheck_probe_over": {"pid": "7777"}})):
        h = released(lambda: (lambda x: (x.run(), x)[1])(Harness(**kw)))
        check("W3 ★★ 完整 precheck 通過、%s -> restore 0" % tag,
              _withheld(h) and h.res["restore_precheck"]["ok"] is True
              and h.res["restore_recheck"]["ok"] is False)

    times = iter([datetime.datetime(2026, 11, 7, 5, 29, 50),
                  datetime.datetime(2026, 11, 7, 5, 30, 1)])
    h = released(lambda: (lambda x: (x.run(), x)[1])(
        Harness(now_fn=(lambda: next(times, datetime.datetime(2026, 11, 7,
                                                              5, 31))))))
    check("W3b ★★ precheck 05:29:50 通過、recheck 跨過 05:30 -> restore 0",
          _withheld(h) and h.res["restore_recheck"]["checks"][
              "restore_window"] is False)

    class StepClock(object):
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            self.t += 31.0
            return self.t
    h = released(lambda: (lambda x: (x.run(), x)[1])(
        Harness(clock=StepClock())))
    check("W4 ★★ precheck 結果超過 30 秒 -> restore 0（不重跑）",
          _withheld(h) and "逾時" in (h.res["restore_withheld_reason"] or ""))

    def boom():
        raise RuntimeError("sample 失敗")
    h = Harness()
    orig = h.sample

    def flaky():
        if h.in_restore():
            boom()
        return orig()
    h.r.sample_source = flaky
    released(h.run)
    check("W5 ★★ precheck 取樣例外 -> restore 0、WITHHELD", _withheld(h))

    # --- journal / recover / 分類 ---
    h = released(lambda: (lambda x: (x.run(), x)[1])(
        Harness(restore_over={"authority": "UNKNOWN"})))
    recs = h.j.read_all()
    st = [r for r in recs if r.get("state") == HO.S_RESTORE_WITHHELD]
    check("W6 ★★ journal 記錄 RESTORE_WITHHELD 與逐項檢查結果",
          st and isinstance(st[-1].get("restore_precheck_checks"), dict)
          and st[-1]["restore_precheck_checks"]["authority_idle"] is False)
    check("W6b ★★ journal 沒有 RESTORE INTENT（真的沒送）",
          not [r for r in recs if r.get("kind") == "INTENT"
               and r.get("step") == "RESTORE"])
    v, last, _a = HO.recover(h.j, {"running": False})
    check("W6c recover()：WITHHELD -> PENDING_RESTORE（責任跨重啟存活）",
          v == HO.R_PENDING_RESTORE and last == HO.S_RESTORE_WITHHELD)
    art = FL.collect_handoff_artifact(h.res, orchestrator=h.o)
    d = art.to_dict()
    check("W7 ★★ artifact：restore_withheld True、takeover WITHHELD、"
          "MA_REQUIRED",
          d["restore_withheld"] is True
          and d["external_takeover"] == FL.EXTERNAL_TAKEOVER_WITHHELD
          and d["manual_attention"] == FL.MA_REQUIRED)
    r, _ = SC.ScheduledFirstLiveRunner._classify(None, d)
    check("W7b ★★ scheduled 分類 = RESTORE_WITHHELD -> exit 4",
          r == SC.R_RESTORE_WITHHELD
          and LAUNCH.classify_exit(r, d) == LAUNCH.EXIT_MANUAL)
    check("W7c RESTORE_WITHHELD 列入 RESULT_STATES 與 P0",
          SC.R_RESTORE_WITHHELD in SC.RESULT_STATES
          and "RESTORE_WITHHELD" in SC.ATTEMPT_RESULT_PRECEDENCE[0][1])
    import phase6_execution_deadline as DL
    mono = {"t": 0.0}
    start = datetime.datetime(2026, 11, 7, 0, 5)
    dl = DL.ExecutionDeadline(start + datetime.timedelta(seconds=1500),
                              now_fn=(lambda: start),
                              monotonic_fn=(lambda: mono["t"]),
                              label="A10-W8")
    orig_ck = dl.check

    def trip(ck):
        if ck == DL.CK_BEFORE_GATE_B:
            mono["t"] = 99999.0
        return orig_ck(ck)
    dl.check = trip
    h = released(lambda: (lambda x: (x.run(), x)[1])(Harness(deadline=dl)))
    check("W8 ★★ 執行期限已過、precheck 通過 -> restore 照送一次"
          "（deadline 不擋安全收尾）",
          h.sd.restore_calls == 1 and h.res["deadline_blocked_at"]
          == DL.CK_BEFORE_GATE_B and h.res["outcome"] == "COMPLETE")
    check("W9 rest state 維持", rest_ok())


# ======================================================================
# J — C4 跨 attempt 阻擋 ＋ C5 人工確認
# ======================================================================
def _journal(d, aid, states, intent=True, restore_ok=False):
    j = HO.Journal(path=HO.attempt_journal_path(d, aid), attempt_id=aid)
    if intent:
        j.append("INTENT", state="PAUSE_REQUESTED", step="PAUSE")
        j.append("OUTCOME", state="PAUSE_REQUESTED", step="PAUSE", ok=True)
    # 與真實 journal 相同的順序：RESTORE OUTCOME 在 RESTORED / COMPLETE 之前
    if restore_ok:
        j.append("OUTCOME", state="RESTORE_REQUESTED", step="RESTORE", ok=True)
    for s in states:
        j.append("STATE", state=s)
    return j


def _ev(now=None, liveness=RA.EXT_NOT_RUNNING, age_sec=10, drop=None):
    now = now or datetime.datetime.now().astimezone()
    at = (now - datetime.timedelta(seconds=age_sec)).isoformat(
        timespec="seconds")
    pe = {"at": at, "liveness": liveness, "pid": PID, "screen_count": "1"}
    ce = {"at": at, "pcs_state": "STANDBY", "fault": False,
          "critical_alarms": 0, "comm_ok": True, "soc": 50.0, "power_kw": -1.3}
    if drop:
        ce.pop(drop, None)
    return pe, ce


def test_J_prior_and_ack():
    print("\n[J] C4 跨 attempt 阻擋 ＋ C5 人工確認")
    # 🔴 OLD / NEW / WHY（2026-11-06 GAP-4）
    #   OLD : 掃描目錄不存在 -> CLEAR。
    #   NEW : 掃描目錄不存在 -> UNRESOLVED（無法證明沒有歷史）。更嚴格。
    check("J0 ★★ 不存在的目錄 -> UNRESOLVED（GAP-4，原為 CLEAR）",
          HO.scan_prior_responsibility(os.path.join(tmpdir(), "none"))[0]
          == HO.PRIOR_UNRESOLVED)
    check("J0b 空目錄 -> CLEAR",
          HO.scan_prior_responsibility(tmpdir())[0] == HO.PRIOR_CLEAR)
    base = tmpdir()
    _journal(base, "A-OK", ["PREFLIGHT", "RESTORED", "COMPLETE"],
             restore_ok=True)
    check("J1 COMPLETE -> CLEAR",
          HO.scan_prior_responsibility(base)[0] == HO.PRIOR_CLEAR)
    for tag, states, intent in (
            ("RESTORE_WITHHELD", ["RESTORE_REQUESTED", "RESTORE_WITHHELD"], True),
            ("PENDING（EXTERNAL_PAUSED）", ["EXTERNAL_PAUSED"], True),
            ("CRITICAL", ["RESTORE_FAILED_CRITICAL"], True),
            ("只有 PAUSE INTENT", [], True),
            ("RESTORE_OUTCOME_UNKNOWN", ["RESTORE_OUTCOME_UNKNOWN"], True)):
        d = tmpdir()
        _journal(d, "A-X", states, intent=intent)
        v, det = HO.scan_prior_responsibility(d)
        check("J2 ★★ %s -> UNRESOLVED" % tag,
              v == HO.PRIOR_UNRESOLVED and det[0]["blocking"])
    d = tmpdir()
    io.open(HO.attempt_journal_path(d, "A-BAD"), "w", encoding="utf-8").write(
        '{"seq": 1, "kind": "STATE", "state": "COMPLETE"}\n{broken\n')
    check("J3 ★★ journal 無法解析 -> UNRESOLVED",
          HO.scan_prior_responsibility(d)[0] == HO.PRIOR_UNRESOLVED)
    # 🔴 OLD / NEW / WHY（2026-11-06 GAP-4）
    #   OLD : 實際歷史 journal（#8 / #9 / legacy）-> CLEAR（只看 journal）。
    #   NEW : 加入 ledger 交叉比對與 Git 歷史清單後，實際歷史**只**因
    #         #3 / #4 的歷史例外尚未核准（PENDING_USER_APPROVAL）而阻擋；
    #         其餘歷史檔完整、#8 / #9 / legacy 仍判定已收斂。更嚴格且更精確。
    real_dir = os.path.join(os.path.dirname(HERE), "output",
                            "phase6_scheduled_first_live")
    real_man = os.path.join(HERE, "phase6_attempt_history.json")
    v, det = HO.scan_prior_responsibility(real_dir, manifest_path=real_man)
    blk = sorted({x["attempt_id"] for x in det if x["blocking"]})
    check("J4 ★★ 實際歷史：只因 #3 / #4 例外未核准而阻擋（其餘已收斂、檔案完整）",
          v == HO.PRIOR_UNRESOLVED
          and blk == ["FIRST-LIVE-ATTEMPT3-20260923",
                      "FIRST-LIVE-ATTEMPT4-20260924"]
          and not [x for x in det if x["source"] == "MANIFEST"
                   and x["blocking"]])
    check("J4c 實際 #8 / #9 / legacy journal 判定已收斂",
          all(not x["blocking"] for x in det if x["source"] == "JOURNAL"))
    _nd = os.path.join(tmpdir(), "not-created")
    HO.scan_prior_responsibility(_nd)
    check("J4b scan 不建立目錄（純讀取）", not os.path.exists(_nd))

    # --- C5 ---
    d = tmpdir()
    aid = "A10-WH"
    _journal(d, aid, ["RESTORE_REQUESTED", "RESTORE_WITHHELD"])
    jp = HO.attempt_journal_path(d, aid)
    pe, ce = _ev()
    good = dict(journal_path=jp, attempt_id=aid, operator="field-op",
                reason="現場人員已確認 controller 由人工恢復並觀察正常",
                probe_evidence=pe, pcs_evidence=ce,
                confirm_phrase="RESOLVE %s" % aid, interactive=True)
    for tag, over in (("非互動", {"interactive": False}),
                      ("確認語句錯", {"confirm_phrase": "RESOLVE"}),
                      ("operator 空白", {"operator": "  "}),
                      ("reason 太短", {"reason": "ok"}),
                      ("liveness UNKNOWN",
                       {"probe_evidence": _ev(liveness=RA.EXT_UNKNOWN)[0]}),
                      ("probe 證據過舊（6 分鐘）",
                       {"probe_evidence": _ev(age_sec=360)[0]}),
                      ("pcs 證據缺 fault",
                       {"pcs_evidence": _ev(drop="fault")[1]}),
                      ("attempt_id 與路徑不符", {"attempt_id": "OTHER"})):
        kw = dict(good)
        kw.update(over)
        ok, why = HO.acknowledge_manual_resolution(**kw)
        check("J5 ★★ C5 拒絕：%s" % tag, ok is False)
    check("J5b 拒絕後仍 UNRESOLVED",
          HO.scan_prior_responsibility(d)[0] == HO.PRIOR_UNRESOLVED)
    sha_before = HO._sha256_file(jp)
    ok, rec = HO.acknowledge_manual_resolution(**good)
    check("J6 ★★ C5 有效確認 -> 寫入", ok is True)
    check("J6b ★★ 確認紀錄欄位齊全（operator / os_user / hostname / ack_at / "
          "attempt_id / seq / sha256 / reason / 兩份證據）",
          ok and all(rec.get(k) not in (None, "") for k in (
              "operator", "os_user", "hostname", "ack_at", "attempt_id",
              "journal_last_seq", "journal_sha256", "reason",
              "probe_evidence", "pcs_evidence")))
    check("J6c ★★ 確認不修改原 journal（sha256 不變、寫在獨立檔案）",
          HO._sha256_file(jp) == sha_before
          and os.path.exists(HO.manual_resolution_path(d, aid)))
    check("J6d ★★ 確認是責任紀錄：not_authentication / hold_unchanged / "
          "restore_sent=False",
          rec.get("not_authentication") is True
          and rec.get("hold_unchanged") is True
          and rec.get("restore_sent") is False)
    check("J6e ★★ 確認後 FIRST_LIVE_HOLD 仍為 True",
          HO.FIRST_LIVE_HOLD is True)
    v, det = HO.scan_prior_responsibility(d)
    check("J7 ★★ 有效確認 -> 該 journal 解除阻擋",
          v == HO.PRIOR_CLEAR and det[0]["resolved_by_ack"])
    HO.Journal(path=jp, attempt_id=aid).append("STATE", state="RESTORE_WITHHELD")
    check("J8 ★★ 確認後 journal 又有新增 -> 確認失效、再度阻擋",
          HO.scan_prior_responsibility(d)[0] == HO.PRIOR_UNRESOLVED)
    d2 = tmpdir()
    _journal(d2, "A-DONE", ["COMPLETE"], restore_ok=True)
    kw = dict(good, journal_path=HO.attempt_journal_path(d2, "A-DONE"),
              attempt_id="A-DONE", confirm_phrase="RESOLVE A-DONE")
    check("J9 沒有未解除責任的 journal -> 不接受確認",
          HO.acknowledge_manual_resolution(**kw)[0] is False)

    # --- AST：沒有正式呼叫點；不碰 HOLD / restore ---
    callers = []
    for p in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        b = os.path.basename(p)
        if b.startswith("test_") or b in ("phase6_manual_resolution.py",):
            continue
        try:
            t = ast.parse(io.open(p, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(t):
            if isinstance(n, ast.Call):
                f = n.func
                nm = f.attr if isinstance(f, ast.Attribute) else getattr(
                    f, "id", None)
                if nm == "acknowledge_manual_resolution":
                    callers.append(b)
    check("J10 ★★ 正式程式沒有任何 acknowledge_manual_resolution 呼叫點：%r"
          % (callers,), callers == [])
    tree = ast.parse(src("phase6_handoff_orchestrator.py"))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def names_in(fn):
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
        return out
    ack_names = names_in(fns["acknowledge_manual_resolution"])
    check("J10b ★★ C5 不讀也不寫 FIRST_LIVE_HOLD、不呼叫 restore",
          not ({"FIRST_LIVE_HOLD", "first_live_hold_active",
                "request_restore", "send_restore_ex"} & ack_names))
    scan_names = set()
    for nm in ("scan_prior_responsibility", "evaluate_journal_responsibility",
               "_valid_resolution", "_read_journal_strict"):
        scan_names |= names_in(fns[nm])
    check("J10c ★★ C4 沒有 restore / recover / probe 路徑",
          not ({"request_restore", "send_restore_ex", "recover", "probe",
                "makedirs"} & scan_names))

    # --- runner / launcher / unattended 的 C4（HOLD 解除）---
    import test_phase6_scheduled_first_live as TS
    blk = tmpdir()
    _journal(blk, "A-PREV", ["RESTORE_REQUESTED", "RESTORE_WITHHELD"])

    def run_rig():
        rig = TS.Rig(TS.INSIDE)
        return rig, rig.runner(prior_journal_dir=blk).run()
    rig, ev = released(run_rig)
    check("J11 ★★ runner：先前責任未解除 -> PRIOR_RESPONSIBILITY_UNRESOLVED",
          ev["result"] == SC.R_PRIOR_RESPONSIBILITY_UNRESOLVED)
    check("J11b ★★ runner：handoff 0 次、授權未消耗",
          rig.handoff_calls == 0 and rig.ledger.consumed is False)
    check("J11c -> exit 4（需人工）",
          LAUNCH.classify_exit(ev["result"]) == LAUNCH.EXIT_MANUAL)
    clr = tmpdir()
    _journal(clr, "A-PREV", ["COMPLETE"], restore_ok=True)
    rig2, ev2 = released(lambda: (lambda r: (r, r.runner(
        prior_journal_dir=clr).run()))(TS.Rig(TS.INSIDE)))
    check("J11d 先前責任已收斂 -> 照常執行（handoff 1 次）",
          rig2.handoff_calls == 1 and ev2["result"] != SC.R_PRIOR_RESPONSIBILITY_UNRESOLVED)

    import test_phase6_scheduled_launcher as TL
    d = tmpdir("a10l_")
    pdir = tmpdir("a10p_")
    TL.write_provider(pdir)
    cfg = TL.write_cfg(d)
    built = {"n": 0}
    ob = LAUNCH.build_dependencies
    od = HO.DEFAULT_SCHEDULED_JOURNAL_DIR

    def cb(*a, **k):
        built["n"] += 1
        return ob(*a, **k)
    # launcher 的掃描目錄固定為正式目錄；測試只在本程序內暫時改指向暫存目錄
    LAUNCH.build_dependencies = cb
    HO.DEFAULT_SCHEDULED_JOURNAL_DIR = blk
    try:
        rc, summ, _o = released(lambda: TL.run_in_process(cfg, pdir))
    finally:
        LAUNCH.build_dependencies = ob
        HO.DEFAULT_SCHEDULED_JOURNAL_DIR = od
    check("J12 ★★ launcher：先前責任未解除 -> exit 4、build 0 次",
          rc == LAUNCH.EXIT_MANUAL and built["n"] == 0
          and (summ or {}).get("scheduled_result")
          == SC.R_PRIOR_RESPONSIBILITY_UNRESOLVED)
    check("J12b launcher 原始碼：C4 掃描目錄不可由 config 指定",
          "scan_prior_responsibility(None)" in src("run_scheduled_first_live.py")
          and "journal_dir" not in src("run_scheduled_first_live.py"))

    import phase6_unattended_service as US
    probed = {"n": 0}

    class _G(object):
        def acquire(self):
            return True, None

    def _probe():
        probed["n"] += 1
        return {}
    svc = US.UnattendedService(
        journal=HO.Journal(path=os.path.join(tmpdir(), "svc.jsonl")),
        instance_guard=_G(), sample_source=(lambda: {}),
        probe_source=_probe, prior_journal_dir=blk)
    v, why = svc.boot()
    check("J13 ★★ 無人值守服務：先前責任未解除 -> BOOT_REFUSED，probe 0 次",
          v == US.BOOT_REFUSED and probed["n"] == 0)

    # --- 確認之後下一次 attempt 仍須通過全部檢查 ---
    d3 = tmpdir()
    aid3 = "A10-ACK"
    _journal(d3, aid3, ["RESTORE_REQUESTED", "RESTORE_WITHHELD"])
    ok3, _ = HO.acknowledge_manual_resolution(**dict(
        good, journal_path=HO.attempt_journal_path(d3, aid3),
        attempt_id=aid3, confirm_phrase="RESOLVE %s" % aid3))
    check("J14 確認後 C4 放行", ok3 and HO.scan_prior_responsibility(d3)[0]
          == HO.PRIOR_CLEAR)
    h = Harness(pre_over={"external_liveness_verdict": RA.EXT_NOT_RUNNING})
    released(h.run)
    check("J14b ★★ 確認後若 controller 仍暫停（NOT_RUNNING）-> C2 仍擋下，"
          "pause 0 / restore 0",
          h.sd.pause_calls == 0 and h.sd.restore_calls == 0)
    hh = Harness()
    hh.run()
    check("J14c ★★ 確認後 HOLD 仍拒絕（pause 0）", hh.sd.pause_calls == 0)

    # --- CLI：非 TTY 一律拒絕 ---
    import phase6_manual_resolution as MR

    class _NoTty(object):
        def isatty(self):
            return False
    pf = os.path.join(tmpdir(), "p.json")
    io.open(pf, "w", encoding="utf-8").write(json.dumps(pe))
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        rc = MR.main(["--attempt-id", aid, "--journal-dir", d,
                      "--operator", "x", "--reason", "y" * 20,
                      "--probe-evidence", pf, "--pcs-evidence", pf],
                     stdin=_NoTty(), prompt=(lambda _p: "RESOLVE %s" % aid))
    check("J15 ★★ CLI 非互動式終端機 -> 拒絕（return 2）", rc == 2)


# ======================================================================
# L — C6 normal_loop_halted（HOLD 解除）
# ======================================================================
def test_L_loop_halted():
    print("\n[L] C6 normal_loop_halted 是必要條件（HOLD 解除）")

    def scen(**kw):
        h = Harness(**kw)
        released(h.run)
        return h
    h = scen(controller_runs_after_pause=True)
    st = {s["step"]: s for s in h.res["steps"]}
    check("L1 ★★ pause 後時間戳仍前進 -> 3_4 中止、dispatcher 0",
          h.disp.calls == 0 and not st["3_4_PAUSE_VERIFY"]["ok"]
          and "C6" in st["3_4_PAUSE_VERIFY"]["detail"]
          and h.res["normal_loop_halted"] is False)
    check("L1b 此時 external 實為 RUNNING -> restore 0（WITHHELD）",
          _withheld(h))
    h = scen(marks=[None] * 40)
    check("L2 ★★ 時間戳取不到（None）-> 中止、dispatcher 0",
          h.disp.calls == 0 and h.res["normal_loop_halted"] is None)
    h = scen(no_marks=True)
    check("L3 ★★ 未注入 loop mark 來源 -> 中止、dispatcher 0",
          h.disp.calls == 0)
    h = scen()
    st = {s["step"]: s for s in h.res["steps"]}
    check("L4 ★★ 時間戳凍結 ＋ PauseVerifier 通過 -> 繼續（到 5_OWNERSHIP）",
          st.get("5_OWNERSHIP", {}).get("ok") is True
          and h.res["normal_loop_halted"] is True)
    charging = [smp()] + [smp(pcs_state="CHARGING", ac_kw=-40.0,
                              authority="EXTERNAL_OR_UNKNOWN")] * 400
    h = Harness(samples=charging)
    released(h.run)
    st = {s["step"]: s for s in h.res["steps"]}
    check("L5 ★★ 時間戳凍結但 PauseVerifier 未通過 -> 不繼續、dispatcher 0",
          h.disp.calls == 0 and not st["3_4_PAUSE_VERIFY"]["ok"]
          and "5_OWNERSHIP" not in st)
    fl = src("phase6_handoff_orchestrator.py")
    check("L6 文件註明「凍結 ≠ 控制權已釋放」", "凍結**不等於**控制權已釋放" in fl)


# ======================================================================
# P — C7 identity / pid / exe / ppid（HOLD 解除）
# ======================================================================
def test_P_identity():
    print("\n[P] C7 identity / pid / exe / ppid —— 只是必要條件")
    ok, ck = HO.post_pause_precheck(smp(), probe(process_identity_ok=False))
    check("P1 ★★ post-pause：identity False -> FAIL",
          not ok and ck["process_identity_ok"] is False)

    def scen(**kw):
        h = Harness(**kw)
        released(h.run)
        return h
    h = scen(probes=[probe(pid=None)] + [probe()] * 40)
    check("P2 ★★ 交接前取不到 pid -> 1_PRE_PAUSE 中止、pause 0",
          h.sd.pause_calls == 0 and h.res["steps"][0]["ok"] is False)
    h = scen(probes=[probe()] * 2 + [probe(pid="5555")] * 40)
    st = {s["step"]: s for s in h.res["steps"]}
    check("P3 ★★ PauseVerifier 期間 pid 改變 -> 3_4 中止、dispatcher 0",
          h.disp.calls == 0 and not st["3_4_PAUSE_VERIFY"]["ok"]
          and "pid" in st["3_4_PAUSE_VERIFY"]["detail"])
    check("P3b pid 改變後 restore 亦不送（WITHHELD）", _withheld(h))
    h = scen(paused_probe_over={"pid": "6666"})
    st = {s["step"]: s for s in h.res["steps"]}
    check("P4 ★★ 6_POST_PAUSE 時 pid 改變 -> 中止、dispatcher 0",
          h.disp.calls == 0 and not st["6_POST_PAUSE_PRECHECK"]["ok"])

    import phase6_first_live_field_provider as FP
    for exe, want in (("/usr/bin/python3.8", True), ("/usr/bin/python3", True),
                      ("/usr/bin/python2.7", False), ("/usr/bin/perl", False),
                      ("", False), (None, False)):
        check("P5 exe=%r -> %s" % (exe, want), FP.controller_exe_ok(exe) is want)
    for pp, want in (("1128", True), ("1", False), ("0", False), ("", False),
                     (None, False), ("abc", False)):
        check("P6 ppid=%r -> %s" % (pp, want),
              FP.controller_ppid_ok(pp) is want)
    fs = src("phase6_first_live_field_provider.py")
    check("P7 provider probe 的 identity 要求 exe 與 ppid",
          "controller_exe_ok(f.get(\"exe\"))" in fs
          and "controller_ppid_ok(f.get(\"ppid\"))" in fs)

    import test_phase6_entry_point_ordering as EP

    def ep_with(line_from, line_to):
        class Ssh(EP.EntrySshRunner):
            def __call__(self, verb, a, b):
                r = EP.EntrySshRunner.__call__(self, verb, a, b)
                if verb == "probe":
                    r["stdout"] = r["stdout"].replace(line_from, line_to)
                return r
        deps, env = EP.build_entry_env()
        env["ssh"].__class__ = Ssh
        EP.run_entry(deps, env)
        return env
    for tag, a, b in (("ppid=1（被 init 收養）", "ppid=99000", "ppid=1"),
                      ("exe 不是 python3", "exe=/usr/bin/python3.8",
                       "exe=/usr/bin/bash")):
        env = released(lambda: ep_with(a, b))
        check("P8 ★★ production entry：%s -> pause 0 / restore 0" % tag,
              env["ssh"].calls.count("pause") == 0
              and env["ssh"].calls.count("restore") == 0)

    h = scen()
    ip = h.res.get("identity_preconditions") or {}
    check("P9 ★★ 結果只外露必要條件欄位（pid0 / identity_preconditions_ok）",
          ip.get("pid0") == PID and ip.get("identity_preconditions_ok") is True)
    keys = set(FL.HANDOFF_ARTIFACT_FIELDS) | set(h.res) | set(ip)
    check("P9b ★★ 沒有 foreground_verified / sole_controller_verified 欄位",
          not ({"foreground_verified", "sole_controller_verified"} & keys))
    check("P9c ★★ 文字明載「只是必要條件」", "只是必要條件" in ip.get("note", ""))


def main():
    print("=" * 72)
    print("  Phase 6 Attempt #10 安全強化（C1～C7）—— 離線")
    print("=" * 72)
    try:
        # 🔴 GAP-4：C4 需要歷史清單與 runtime index -> 一律在暫存環境執行
        with HOLD.offline_c4_env():
            for fn in (test_H_hold, test_H_scheduled_and_launcher,
                       test_X_running, test_W_restore_precheck,
                       test_J_prior_and_ack, test_L_loop_halted,
                       test_P_identity):
                fn()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    hold_ok = HO.FIRST_LIVE_HOLD is True
    print("=" * 72)
    print("  結果：%d/%d %s" % (n, tot, "PASS" if n == tot else "FAIL"))
    print("  rest state：HO=%r RT=%r  FIRST_LIVE_HOLD=%r"
          % (HO.DISPATCH_ENABLED, RT.DISPATCH_ENABLED, HO.FIRST_LIVE_HOLD))
    print("=" * 72)
    return 0 if (n == tot and rest_ok() and hold_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
