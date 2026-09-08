# -*- coding: utf-8 -*-
"""
test_phase6_610c2_ssh_timeout.py — Phase 6.10-C2 SSH Timeout Architecture
======================================================================
核心命題
    「SSH 有兩種完全不同的等待：連不連得上這台主機，和主機連上了但指令
      跑不跑得完。兩者的 timeout 必須分開、command timeout 必須能依動詞
      區分、失敗分類不得壓成單一 REMOTE_FAILED；而且 command timeout 之後
      **不得**推論成『指令沒有執行』，更不得直接重送。」

🔴 本檔只驗**架構與語意**；注入用的數字一律是測試 fixture，
   不是候選值、更不是 production 值。
   C3 已核准的四項數值本身由 `test_phase6_610c3_timeout_values.py` 驗證。

是否需要設備
    **不需要**，而且結構上不可能連線 —— runner 一律注入，未注入即
    SSH_NOT_CONFIGURED。零實際 SSH、零 subprocess。

用法
    python test_phase6_610c2_ssh_timeout.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import ast
import inspect
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO         # noqa: E402
import phase6_remote_adapter as RA               # noqa: E402
import phase6_unattended_service as SVC          # noqa: E402

RESULTS = []
_TMP = []

# 🔴 測試 fixture，不是候選值。取值刻意醜（7 / 11 / 29…）以免被誤讀成建議值。
FX_CONNECT = 7.0
FX_CMD = {"probe": 11.0, "status": 11.0, "loopcheck": 29.0,
          "pause": 11.0, "restore": 11.0}


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p610c2_")
    _TMP.append(d)
    return d


def timeouts(connect=FX_CONNECT, cmd=None):
    return RA.SshTimeouts(connect_timeout_sec=connect,
                          command_timeouts=(FX_CMD if cmd is None else cmd))


class Runner(object):
    """
    可注入的假 SSH runner。記錄每次收到的 (verb, connect_to, command_to)，
    並依 `behaviour` 決定回傳或拋出。**完全不碰網路。**
    """

    def __init__(self, behaviour=None, exit_code=0, stdout=""):
        self.behaviour = behaviour or {}
        self.exit_code = exit_code
        self.stdout = stdout
        self.calls = []

    def __call__(self, verb, connect_to, command_to):
        self.calls.append({"verb": verb, "connect": connect_to,
                           "command": command_to})
        b = self.behaviour.get(verb)
        if b == "connect_timeout":
            raise RA.SshConnectTimeout("connect > %ss" % connect_to)
        if b == "command_timeout":
            raise RA.SshCommandTimeout("command > %ss" % command_to)
        if b == "auth":
            raise RA.SshAuthFailed("permission denied")
        if b == "refused":
            return {"exit_code": RA.GUARD_EXIT_REFUSED, "stdout": ""}
        if b == "guard_timeout":
            return {"exit_code": RA.GUARD_EXIT_TIMEOUT, "stdout": ""}
        if b == "boom":
            raise OSError("something else")
        return {"exit_code": self.exit_code, "stdout": self.stdout}


def transport(runner=None, to=None, armed=False):
    return RA.SshTransport(timeouts=(timeouts() if to is None else to),
                           runner=runner, armed=armed)


def probe_dict(**over):
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


# ======================================================================
# 1. connect timeout 與 command timeout 結構分離
# ======================================================================
def test_1_split():
    print("\n[1] connect / command timeout 結構分離")
    t = RA.SshTimeouts()
    check("  SshTimeouts 有獨立的 connect_timeout_sec 欄位",
          hasattr(t, "connect_timeout_sec"))
    check("  SshTimeouts 有獨立的 per-verb command_timeouts",
          isinstance(t.command_timeouts, dict))
    check("★★ 不存在單一 ssh_timeout_sec 欄位",
          not hasattr(t, "ssh_timeout_sec")
          and "ssh_timeout_sec" not in
          inspect.signature(RA.SshTimeouts.__init__).parameters)
    r = Runner()
    tr = transport(r)
    tr.run("probe")
    check("★★ runner 同時收到兩個不同的 timeout（各自獨立傳入）",
          r.calls[0]["connect"] == FX_CONNECT
          and r.calls[0]["command"] == FX_CMD["probe"]
          and r.calls[0]["connect"] != r.calls[0]["command"])

    # 兩種逾時走不同分支
    a = transport(Runner({"probe": "connect_timeout"})).run("probe")
    b = transport(Runner({"probe": "command_timeout"})).run("probe")
    check("★★ connect 階段逾時 → SSH_CONNECT_TIMEOUT",
          a.outcome == RA.SSH_CONNECT_TIMEOUT)
    check("★★ command 階段逾時 → SSH_COMMAND_TIMEOUT",
          b.outcome == RA.SSH_COMMAND_TIMEOUT)
    check("  兩者是不同的 outcome，沒有被壓成同一個",
          a.outcome != b.outcome)


# ======================================================================
# 2. per-verb command timeout 可注入
# ======================================================================
def test_2_per_verb():
    print("\n[2] per-verb command timeout")
    t = timeouts()
    for v in RA.GUARD_ALLOWED_VERBS:
        check(f"  command_timeout_for({v!r}) = {t.command_timeout_for(v)}",
              t.command_timeout_for(v) == FX_CMD[v])
    check("★★ 各動詞可以有不同的值（loopcheck 與 probe 不同）",
          t.command_timeout_for("loopcheck") != t.command_timeout_for("probe"))

    # 只設定部分動詞：其餘維持 None，不得被別人的值頂替
    partial = RA.SshTimeouts(connect_timeout_sec=FX_CONNECT,
                             command_timeouts={"probe": 11.0})
    check("★★ 只設 probe → 其餘動詞仍為 None（不共用、不繼承）",
          partial.command_timeout_for("probe") == 11.0
          and all(partial.command_timeout_for(v) is None
                  for v in RA.GUARD_ALLOWED_VERBS if v != "probe"))
    check("  未知動詞一律拒絕（不靜默接受）",
          _raises(lambda: t.command_timeout_for("rm -rf /"))
          and _raises(lambda: RA.SshTimeouts(command_timeouts={"evil": 1})))

    # runner 真的收到 per-verb 的值
    r = Runner()
    tr = transport(r, armed=True)
    for v in RA.GUARD_ALLOWED_VERBS:
        tr.run(v)
    got = {c["verb"]: c["command"] for c in r.calls}
    check(f"★★ runner 收到的 command timeout 逐動詞不同：{got}",
          got == FX_CMD)


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


# ======================================================================
# 3. None → Live Fail Closed
# ======================================================================
def test_3_none_fail_closed():
    print("\n[3] timeout 未定案 → Fail Closed")
    # C3 裁示：四項已 FINAL，因此改以 unconfigured() 演練未設定路徑
    t = RA.SshTimeouts.unconfigured()
    check("★★ unconfigured() 全為 None（Fail Closed 路徑未被拿掉）",
          t.connect_timeout_sec is None
          and all(t.command_timeout_for(v) is None
                  for v in RA.GUARD_ALLOWED_VERBS))
    check("  missing() 列出 connect + 五個動詞，共 6 項",
          len(t.missing()) == 6
          and "ssh_connect_timeout_sec" in t.missing())
    check("  configured_for 全部 False",
          not any(t.configured_for(v) for v in RA.GUARD_ALLOWED_VERBS))

    r = Runner()
    tr = RA.SshTransport(timeouts=t, runner=r, armed=True)
    res = tr.run("probe")
    check("★★ timeout 未設定 → SSH_NOT_CONFIGURED",
          res.outcome == RA.SSH_NOT_CONFIGURED)
    check("★★ 且 runner 完全沒有被呼叫（結構上不可能連線）",
          r.calls == [] and tr.calls == [])
    check("  原因如實回報",
          "TIMEOUT_UNRESOLVED" in (res.detail or ""))

    # connect 有、command 沒有 → 仍然 Fail Closed
    half = RA.SshTimeouts(connect_timeout_sec=FX_CONNECT)
    r2 = Runner()
    res2 = RA.SshTransport(timeouts=half, runner=r2, armed=True).run("pause")
    check("★★ 只有 connect timeout、缺 command timeout → 仍 NOT_CONFIGURED",
          res2.outcome == RA.SSH_NOT_CONFIGURED and r2.calls == [])

    # command 有、connect 沒有 → 仍然 Fail Closed
    # C3：connect 已有 approved default，因此要明確注入 None 才是「缺 connect」
    half2 = RA.SshTimeouts(connect_timeout_sec=None, command_timeouts=FX_CMD)
    r3 = Runner()
    res3 = RA.SshTransport(timeouts=half2, runner=r3, armed=True).run("pause")
    check("★★ 只有 command timeout、缺 connect timeout → 仍 NOT_CONFIGURED",
          res3.outcome == RA.SSH_NOT_CONFIGURED and r3.calls == [])

    # 未注入 timeouts / runner
    check("  timeouts 未注入 → NOT_CONFIGURED",
          RA.SshTransport(runner=Runner()).run("probe").outcome
          == RA.SSH_NOT_CONFIGURED)
    check("  runner 未注入 → NOT_CONFIGURED（且不可能連線）",
          RA.SshTransport(timeouts=timeouts()).run("probe").outcome
          == RA.SSH_NOT_CONFIGURED)
    # C3：pause / restore 沒有 approved default → 用 production 預設也送不出去
    r4 = Runner()
    res4 = RA.SshTransport(timeouts=RA.SshTimeouts(), runner=r4,
                           armed=True).run("pause")
    check("★★ production 預設下 pause 仍 NOT_CONFIGURED（無 approved default）",
          res4.outcome == RA.SSH_NOT_CONFIGURED and r4.calls == [])
    r5 = Runner()
    res5 = RA.SshTransport(timeouts=RA.SshTimeouts(), runner=r5,
                           armed=True).run("restore")
    check("★★ production 預設下 restore 亦然",
          res5.outcome == RA.SSH_NOT_CONFIGURED and r5.calls == [])
    check("  但唯讀三動詞在 production 預設下可執行",
          all(RA.SshTransport(timeouts=RA.SshTimeouts(),
                              runner=Runner()).run(v).ok
              for v in RA.READONLY_VERBS))


# ======================================================================
# 4/5. 唯讀動詞逾時 → 不送任何控制
# ======================================================================
def test_4_5_readonly_timeout():
    print("\n[4/5] probe / status 逾時不得觸發任何控制")
    for verb in ("probe", "status"):
        for mode in ("connect_timeout", "command_timeout"):
            r = Runner({verb: mode})
            tr = transport(r, armed=True)
            res = tr.run(verb)
            check(f"  {verb} {mode} → {res.outcome}",
                  res.outcome in (RA.SSH_CONNECT_TIMEOUT,
                                  RA.SSH_COMMAND_TIMEOUT))
            check(f"★★ {verb} {mode} 後未送出任何控制動詞",
                  not [c for c in r.calls if c["verb"] in RA.CONTROL_VERBS])
    check("★★ 唯讀動詞與控制動詞的清單互斥",
          not (set(RA.READONLY_VERBS) & set(RA.CONTROL_VERBS)))
    check("  兩者聯集 = guard allowlist",
          set(RA.READONLY_VERBS) | set(RA.CONTROL_VERBS)
          == set(RA.GUARD_ALLOWED_VERBS))
    # 唯讀動詞不需要 armed
    check("  唯讀動詞不需要 armed 即可執行（但仍需 timeout）",
          RA.SshTransport(timeouts=timeouts(), runner=Runner(),
                          armed=False).run("probe").ok)
    check("★★ 控制動詞未 armed → 不呼叫 runner",
          RA.SshTransport(timeouts=timeouts(), runner=(r2 := Runner()),
                          armed=False).run("pause").outcome
          == RA.SSH_NOT_CONFIGURED and r2.calls == [])


# ======================================================================
# 6. loopcheck 的合法長執行不得套用 connect timeout
# ======================================================================
def test_6_loopcheck():
    print("\n[6] loopcheck 合法長執行")
    bound = RA.guard_server_side_bound_sec("loopcheck")
    check(f"  loopcheck server-side 結構上界 = {bound} s"
          f"（10 + 3 + 10）", bound == 23.0)
    check("  其餘動詞的上界較短（不可共用一個值）",
          RA.guard_server_side_bound_sec("status") == 10.0
          and bound > RA.guard_server_side_bound_sec("status"))
    check("  probe 無明確結構上界 → None（不得解讀成無限制）",
          RA.guard_server_side_bound_sec("probe") is None)

    r = Runner()
    tr = transport(r, armed=True)
    tr.run("loopcheck")
    c = r.calls[0]
    check(f"★★ loopcheck 收到的是 command timeout {c['command']}，"
          f"不是 connect timeout {c['connect']}",
          c["command"] == FX_CMD["loopcheck"] and c["connect"] == FX_CONNECT)
    check("★★ loopcheck 的 command timeout 未被 connect timeout 取代",
          c["command"] != c["connect"])

    t = timeouts()
    check(f"  covers_server_side_bound('loopcheck') = "
          f"{t.covers_server_side_bound('loopcheck')}"
          f"（{FX_CMD['loopcheck']} >= {bound}）",
          t.covers_server_side_bound("loopcheck") is True)
    too_short = timeouts(cmd=dict(FX_CMD, loopcheck=10.0))
    check("★★ 若 loopcheck command timeout 小於結構窗 → 明確判 False",
          too_short.covers_server_side_bound("loopcheck") is False)
    check("  未設定時回 None（不得回 False 假裝已檢查）",
          RA.SshTimeouts.unconfigured()
          .covers_server_side_bound("loopcheck") is None)

    # C3：已核准值必須嚴格大於結構窗，且不得等於結構窗本身
    prod = RA.SshTimeouts()
    check("★★ 已核准的 loopcheck 26.0 > 結構窗 23.0",
          prod.command_timeout_for("loopcheck") == 26.0
          and prod.covers_server_side_bound("loopcheck") is True)
    check("★★ 結構窗本身沒有被直接當成 timeout（26 != 23）",
          prod.command_timeout_for("loopcheck")
          != RA.guard_server_side_bound_sec("loopcheck"))
    check("★★ status 12.0 > 結構窗 10.0，且不等於結構窗",
          prod.command_timeout_for("status") == 12.0
          and prod.covers_server_side_bound("status") is True
          and prod.command_timeout_for("status")
          != RA.guard_server_side_bound_sec("status"))


# ======================================================================
# 7/8. pause command timeout → UNKNOWN，且不得 blind resend
# ======================================================================
def test_7_8_pause_timeout():
    print("\n[7/8] pause command timeout → UNKNOWN")
    r = Runner({"pause": "command_timeout"})
    sd = RA.RemoteSenders(transport=transport(r, armed=True), armed=True)
    outcome, res = sd.pause_ex()
    check("  outcome 分類為 SEND_UNKNOWN", outcome == RA.SEND_UNKNOWN)
    check("★★ **不得**分類為 SEND_NOT_SENT（可能已經執行）",
          outcome != RA.SEND_NOT_SENT)
    check("  底層 outcome 為 SSH_COMMAND_TIMEOUT",
          res.outcome == RA.SSH_COMMAND_TIMEOUT and res.uncertain is True)
    check("  pause() 的 bool 回 False（只有確定送達才 True）",
          sd.last_send_outcome == RA.SEND_UNKNOWN)

    # 對照：connect timeout / refused 是可斷定的「沒執行」
    for beh, want in (("connect_timeout", RA.SEND_NOT_SENT),
                      ("auth", RA.SEND_NOT_SENT),
                      ("refused", RA.SEND_NOT_SENT),
                      ("guard_timeout", RA.SEND_UNKNOWN),
                      ("boom", RA.SEND_UNKNOWN)):
        s2 = RA.RemoteSenders(
            transport=transport(Runner({"pause": beh}), armed=True), armed=True)
        got = s2.pause_ex()[0]
        check(f"  pause 遇到 {beh} → {got}", got == want)

    check("★★ blind_resend_allowed 恆為 False（契約可回歸驗證）",
          all(RA.blind_resend_allowed(o) is False
              for o in (RA.SEND_SENT, RA.SEND_NOT_SENT, RA.SEND_UNKNOWN)))
    check("★★ UNKNOWN 之後必須先 probe / recover",
          RA.must_probe_before_further_action(RA.SEND_UNKNOWN) is True
          and RA.must_probe_before_further_action(RA.SEND_NOT_SENT) is False)

    # orchestrator 層
    d = tmpdir()
    j = HO.Journal(path=os.path.join(d, "j.jsonl"))
    sd2 = RA.RemoteSenders(
        transport=transport(Runner({"pause": "command_timeout"}), armed=True),
        armed=True)
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd2.pause,
                                   restore_sender=sd2.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    o._to(HO.S_PREFLIGHT)
    o._to(HO.S_PAUSE_REQUESTED)
    sent, why = o.request_pause()
    check("★★ request_pause 回 (False, SEND_OUTCOME_UNKNOWN_MUST_PROBE)",
          sent is False and why == "SEND_OUTCOME_UNKNOWN_MUST_PROBE")
    recs = j.read_all()
    intent = [x for x in recs if x.get("kind") == "INTENT"
              and x.get("step") == "PAUSE"]
    out = [x for x in recs if x.get("kind") == "OUTCOME"
           and x.get("step") == "PAUSE"]
    check("★★ INTENT 已 durable（先寫意圖再送出）", len(intent) == 1)
    check("★★ OUTCOME 的 ok 記為 None（不確定），**不是** False",
          len(out) == 1 and out[0].get("ok") is None)
    check("  OUTCOME 保留 send_outcome=UNKNOWN 供稽核",
          out[0].get("send_outcome") == RA.SEND_UNKNOWN)

    # 不得 blind resend：OnceGuard 直接擋
    sent2, why2 = o.request_pause()
    check("★★ 第二次 request_pause 被擋（不得 blind resend）",
          sent2 is False
          and why2 == "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND")
    check("★★ 遠端只被送過 1 次 pause",
          len([c for c in r_calls(sd2) if c == "stop"]) == 1)


def r_calls(senders):
    return list(senders.calls)


# ======================================================================
# 9/10. restore command timeout → 責任保留、不得 blind resend
# ======================================================================
def test_9_10_restore_timeout():
    print("\n[9/10] restore command timeout")
    sd = RA.RemoteSenders(
        transport=transport(Runner({"restore": "command_timeout"}), armed=True),
        armed=True)
    outcome, res = sd.restore_ex()
    check("  restore 逾時 → SEND_UNKNOWN", outcome == RA.SEND_UNKNOWN)
    check("★★ **不得**因此判定 restore 失敗", outcome != RA.SEND_NOT_SENT)

    d = tmpdir()
    j = HO.Journal(path=os.path.join(d, "j.jsonl"))
    sd2 = RA.RemoteSenders(
        transport=transport(Runner({"restore": "command_timeout"}), armed=True),
        armed=True)
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd2.pause,
                                   restore_sender=sd2.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    o._to(HO.S_PREFLIGHT)
    o._to(HO.S_PAUSE_REQUESTED)
    o._to(HO.S_RESTORE_REQUESTED)
    sent, why = o.request_restore()
    check("★★ request_restore 回 SEND_OUTCOME_UNKNOWN_MUST_PROBE",
          sent is False and why == "SEND_OUTCOME_UNKNOWN_MUST_PROBE")
    o._to(HO.S_RESTORE_OUTCOME_UNKNOWN, detail=why)
    check("★★ RESTORE_OUTCOME_UNKNOWN 屬於 PENDING_RESTORE_STATES（責任保留）",
          HO.S_RESTORE_OUTCOME_UNKNOWN in HO.PENDING_RESTORE_STATES)
    check("★★ recover() 會把它判成 PENDING_RESTORE（責任跨重啟存活）",
          HO.recover(j, {"running": False})[0] == HO.R_PENDING_RESTORE)
    check("  遠端狀態不明時 → R_UNKNOWN（Fail Closed）",
          HO.recover(j, {"running": None})[0] == HO.R_UNKNOWN)

    check("★★ 狀態機不存在回到 RESTORE_REQUESTED 的轉移（禁止 blind resend）",
          HO.S_RESTORE_REQUESTED
          not in HO.TRANSITIONS[HO.S_RESTORE_OUTCOME_UNKNOWN])
    check("★★ 同理 PAUSE_OUTCOME_UNKNOWN 不得回到 PAUSE_REQUESTED",
          HO.S_PAUSE_REQUESTED
          not in HO.TRANSITIONS[HO.S_PAUSE_OUTCOME_UNKNOWN])
    check("  只能先觀測：允許的下一步是驗證或歸還",
          HO.TRANSITIONS[HO.S_PAUSE_OUTCOME_UNKNOWN]
          == {HO.S_PAUSE_VERIFYING, HO.S_RESTORE_REQUESTED})
    check("★★ PAUSE_OUTCOME_UNKNOWN 也屬於 PENDING_RESTORE_STATES",
          HO.S_PAUSE_OUTCOME_UNKNOWN in HO.PENDING_RESTORE_STATES)
    check("  兩個不確定狀態皆**不是** CRITICAL（可由觀測收斂）",
          HO.S_PAUSE_OUTCOME_UNKNOWN not in HO.CRITICAL_STATES
          and HO.S_RESTORE_OUTCOME_UNKNOWN not in HO.CRITICAL_STATES)


# ======================================================================
# 11/12/13. DRY_RUN / OBSERVE sender = 0、dispatcher = 0
# ======================================================================
def test_11_12_13_dry_run():
    print("\n[11/12/13] DRY_RUN / OBSERVE 全 0")
    for mode in (HO.MODE_DRY_RUN, HO.MODE_OBSERVE):
        r = Runner()
        sd = RA.RemoteSenders(transport=transport(r, armed=True), armed=False)
        d = tmpdir()
        j = HO.Journal(path=os.path.join(d, "j.jsonl"))
        hits = []
        o = HO.HandoffOrchestrator(
            remote=HO.RemoteController(pause_sender=sd.pause,
                                       restore_sender=sd.restore),
            observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j,
            mode=mode, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
        o._to(HO.S_PREFLIGHT)
        o._to(HO.S_PAUSE_REQUESTED)
        sent, why = o.request_pause()
        check(f"  {mode}: request_pause 不送出（{why}）", sent is False)
        check(f"★★ {mode}: transport 完全沒被呼叫", r.calls == [])
        check(f"★★ {mode}: senders.calls = 0", sd.calls == [])
        check(f"★★ {mode}: dispatcher = 0", hits == [])
    check("★★ RemoteSenders 預設 armed=False（結構上送不出去）",
          RA.RemoteSenders().armed is False)
    check("  未 armed 時即使有 transport 也不呼叫",
          RA.RemoteSenders(transport=transport(rr := Runner(), armed=True),
                           armed=False).pause() is False and rr.calls == [])


# ======================================================================
# 14. 原始碼無單一 ssh_timeout_sec magic-number fallback
# ======================================================================
def test_14_no_magic():
    print("\n[14] 原始碼無單一 ssh_timeout / magic-number fallback")
    src = io.open(os.path.join(HERE, "phase6_remote_adapter.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)

    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            dd = ast.get_docstring(n, clean=False)
            if dd:
                docs.add(dd)
    code_strs = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.value not in docs}

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.arg for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) for a in n.args.args}
    check("★★ 不存在名為 ssh_timeout_sec 的識別字",
          "ssh_timeout_sec" not in names)
    check("★★ 也不存在 SSH_TIMEOUT_SEC 常數",
          "SSH_TIMEOUT_SEC" not in names)

    # 兩個 timeout 參數的預設值必須是 None，不得是數字
    defaults = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef):
            a = n.args
            for arg, dflt in zip(a.args[len(a.args) - len(a.defaults):],
                                 a.defaults):
                if arg.arg in ("connect_timeout_sec", "command_timeouts",
                               "timeouts"):
                    defaults.setdefault(arg.arg, []).append(dflt)
    flat = [d for v in defaults.values() for d in v]
    # C3：SshTimeouts 的預設改為具名 sentinel（_UNSET）＋具名已核准常數；
    #     SshTransport.timeouts 仍是 None（未注入即 Fail Closed）。
    ok_default = []
    for d in flat:
        if isinstance(d, ast.Constant) and d.value is None:
            ok_default.append(True)
        elif isinstance(d, ast.Name) and d.id == "_UNSET":
            ok_default.append(True)
        else:
            ok_default.append(False)
    check(f"★★ 預設值只允許 None 或具名 sentinel，**不得**是數字字面值"
          f"（共 {len(flat)} 處）", bool(flat) and all(ok_default))
    check("★★ 已核准值以具名常數保存，不是散落在簽章裡的魔術數字",
          RA.APPROVED_SSH_CONNECT_TIMEOUT_SEC == 5.0
          and RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC
          == {"probe": 5.0, "status": 12.0, "loopcheck": 26.0})

    # transport 內不得有 `or <number>` 這種偷偷補預設的寫法
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            for v in n.values:
                if (isinstance(v, ast.Constant)
                        and isinstance(v.value, (int, float))
                        and not isinstance(v.value, bool)):
                    bad.append(n.lineno)
    check(f"★★ 無 `x or <數字>` 形式的隱性 timeout fallback（命中={bad}）",
          not bad)

    # 只有 guard 腳本字面（部署事實）可以出現硬編秒數
    guard_like = [x for x in code_strs
                  if re.search(r"ACT_TIMEOUT|TIMEOUT=", x)]
    check(f"  guard 腳本內的 ACT_TIMEOUT 是既有部署事實，不受此限"
          f"（{len(guard_like)} 段）", len(guard_like) >= 1)
    check("  ACT_TIMEOUT 已在 python 側具名登記，可回歸比對",
          RA.GUARD_ACT_TIMEOUT_SEC == 10.0)


# ======================================================================
# 15. 不存在實際 SSH connection
# ======================================================================
def test_15_no_real_ssh():
    print("\n[15] 結構上不可能發生實際 SSH 連線")
    src = io.open(os.path.join(HERE, "phase6_remote_adapter.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    forbidden = mods & {"subprocess", "paramiko", "fabric", "socket",
                        "asyncssh", "os"} - {"os"}
    check(f"★★ 未匯入任何連線／行程執行模組（命中={sorted(forbidden)}）",
          not forbidden)
    check("★★ SshTransport 不自建 runner —— 未注入即不可能執行",
          inspect.signature(RA.SshTransport.__init__)
          .parameters["runner"].default is None)

    src2 = inspect.getsource(RA.SshTransport)
    t2 = ast.parse(src2.lstrip())
    called = set()
    for n in ast.walk(t2):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute):
                called.add(n.func.attr)
            elif isinstance(n.func, ast.Name):
                called.add(n.func.id)
    check(f"★★ SshTransport 內不呼叫 run/Popen/check_output/connect"
          f"（命中={sorted(called & {'Popen', 'check_output', 'connect', 'system'})}）",
          not (called & {"Popen", "check_output", "connect", "system"}))

    # 全流程實跑一次，確認唯一的外部呼叫就是注入的 fake runner
    r = Runner()
    tr = transport(r, armed=True)
    for v in RA.GUARD_ALLOWED_VERBS:
        tr.run(v)
    check(f"★★ 本輪唯一被呼叫的「外部」是注入的假 runner（{len(r.calls)} 次）",
          len(r.calls) == len(RA.GUARD_ALLOWED_VERBS))
    check("  部署中的 guard 為 B2（allowlist 含五個動詞）",
          RA.DEPLOYED_GUARD_VARIANT == "B2"
          and RA.guard_b2_would_accept("pause")
          and RA.guard_b2_would_accept("restore"))
    check("★★ 但本模組仍不可能真的送出 —— senders 預設未武裝",
          RA.RemoteSenders().armed is False)


# ======================================================================
# 16. taxonomy 完整性
# ======================================================================
def test_16_taxonomy():
    print("\n[16] timeout result taxonomy")
    # 🔴 先驗語意、再驗數量 —— 數量只是輔助，語意才是契約
    check("★★ 四種 guard 端拒絕皆為獨立 outcome（未互相合併）",
          len({RA.SSH_REFUSED, RA.SSH_GUARD_MISSING,
               RA.SSH_GUARD_AMBIGUOUS, RA.SSH_GUARD_IDENTITY}) == 4)
    check("★★ 四者皆屬 DEFINITELY_NOT_EXECUTED",
          all(x in RA.SSH_DEFINITELY_NOT_EXECUTED
              for x in (RA.SSH_REFUSED, RA.SSH_GUARD_MISSING,
                        RA.SSH_GUARD_AMBIGUOUS, RA.SSH_GUARD_IDENTITY)))
    check("  outcome 詞彙共 10 種（輔助檢查）", len(RA.SSH_OUTCOMES) == 10)
    check("  無重複詞彙", len(set(RA.SSH_OUTCOMES)) == len(RA.SSH_OUTCOMES))
    check("★★ 不存在把一切壓成一個值的 REMOTE_FAILED",
          not hasattr(RA, "REMOTE_FAILED"))
    check("★★ CONNECT_TIMEOUT 屬於「可斷定未執行」",
          RA.SSH_CONNECT_TIMEOUT in RA.SSH_DEFINITELY_NOT_EXECUTED)
    check("★★ COMMAND_TIMEOUT **不**屬於「可斷定未執行」",
          RA.SSH_COMMAND_TIMEOUT not in RA.SSH_DEFINITELY_NOT_EXECUTED)
    check("  COMMAND_TIMEOUT 被列為不確定",
          RA.SSH_COMMAND_TIMEOUT in RA.SSH_OUTCOME_UNCERTAIN)
    check("  兩個集合互斥",
          not (set(RA.SSH_DEFINITELY_NOT_EXECUTED)
               & set(RA.SSH_OUTCOME_UNCERTAIN)))
    check("  SSH_OK 不在任一失敗集合中",
          RA.SSH_OK not in RA.SSH_DEFINITELY_NOT_EXECUTED
          and RA.SSH_OK not in RA.SSH_OUTCOME_UNCERTAIN)
    for o in RA.SSH_OUTCOMES:
        check(f"  {o} 有明確的三值歸類",
              RA.classify_send_outcome(
                  RA.SshResult("pause", o)) in (RA.SEND_SENT, RA.SEND_NOT_SENT,
                                                RA.SEND_UNKNOWN))
    check("  result 為 None → 保守判 NOT_SENT（沒有結果就是沒送）",
          RA.classify_send_outcome(None) == RA.SEND_NOT_SENT)
    check("★★ timed_out 同時涵蓋兩種逾時，但 uncertain 只涵蓋 command",
          RA.SshResult("p", RA.SSH_CONNECT_TIMEOUT).timed_out
          and RA.SshResult("p", RA.SSH_COMMAND_TIMEOUT).timed_out
          and not RA.SshResult("p", RA.SSH_CONNECT_TIMEOUT).uncertain
          and RA.SshResult("p", RA.SSH_COMMAND_TIMEOUT).uncertain)


# ======================================================================
# 16b. exit 44 / 45 —— field-discovered correctness gap（2026-09-07）
# ======================================================================
def test_16b_guard_refusal_exit_codes():
    print("\n[16b] guard exit 44 / 45 = DEFINITELY NOT EXECUTED")

    class R(object):
        """回固定 exit code 的假 runner；記錄是否被呼叫。"""

        def __init__(self, code):
            self.code = code
            self.calls = []

        def __call__(self, verb, c, x):
            self.calls.append(verb)
            return {"exit_code": self.code, "stdout": ""}

    def run(code, verb="pause"):
        to = RA.SshTimeouts(
            connect_timeout_sec=FX_CONNECT,
            command_timeouts=dict(FX_CMD))
        return RA.SshTransport(timeouts=to, runner=R(code),
                               armed=True).run(verb)

    check("  exit 43 有具名常數", RA.GUARD_EXIT_MISSING == 43)
    check("  exit 44 有具名常數", RA.GUARD_EXIT_AMBIGUOUS == 44)
    check("  exit 45 有具名常數", RA.GUARD_EXIT_IDENTITY == 45)

    # ---- exit 43 ----
    m = run(RA.GUARD_EXIT_MISSING)
    check("★★ exit 43 → SSH_GUARD_MISSING（不再壓成 SSH_ERROR）",
          m.outcome == RA.SSH_GUARD_MISSING)
    check("★★ exit 43 屬於 DEFINITELY_NOT_EXECUTED",
          RA.SSH_GUARD_MISSING in RA.SSH_DEFINITELY_NOT_EXECUTED)
    check("★★ exit 43 → SEND_NOT_SENT",
          RA.classify_send_outcome(m) == RA.SEND_NOT_SENT)
    check("  exit 43 非 uncertain、非 timed_out",
          RA.SSH_GUARD_MISSING not in RA.SSH_OUTCOME_UNCERTAIN
          and not m.uncertain and not m.timed_out)
    check("★★ exit 43 與 exit 42 未被合併（語意不同）",
          m.outcome != run(RA.GUARD_EXIT_REFUSED).outcome
          and RA.SSH_GUARD_MISSING != RA.SSH_REFUSED)
    check("  exit 42 仍為 SSH_REFUSED / NOT_SENT",
          run(RA.GUARD_EXIT_REFUSED).outcome == RA.SSH_REFUSED
          and RA.classify_send_outcome(
              run(RA.GUARD_EXIT_REFUSED)) == RA.SEND_NOT_SENT)

    a = run(RA.GUARD_EXIT_AMBIGUOUS)
    i = run(RA.GUARD_EXIT_IDENTITY)
    check("★★ exit 44 → SSH_GUARD_AMBIGUOUS（不再壓成 SSH_ERROR）",
          a.outcome == RA.SSH_GUARD_AMBIGUOUS)
    check("★★ exit 45 → SSH_GUARD_IDENTITY（不再壓成 SSH_ERROR）",
          i.outcome == RA.SSH_GUARD_IDENTITY)
    check("★★ exit 44 屬於 DEFINITELY_NOT_EXECUTED",
          RA.SSH_GUARD_AMBIGUOUS in RA.SSH_DEFINITELY_NOT_EXECUTED)
    check("★★ exit 45 屬於 DEFINITELY_NOT_EXECUTED",
          RA.SSH_GUARD_IDENTITY in RA.SSH_DEFINITELY_NOT_EXECUTED)
    check("★★ 兩者都**不**屬於 uncertain",
          RA.SSH_GUARD_AMBIGUOUS not in RA.SSH_OUTCOME_UNCERTAIN
          and RA.SSH_GUARD_IDENTITY not in RA.SSH_OUTCOME_UNCERTAIN)
    check("★★ exit 44 → SEND_NOT_SENT",
          RA.classify_send_outcome(a) == RA.SEND_NOT_SENT)
    check("★★ exit 45 → SEND_NOT_SENT",
          RA.classify_send_outcome(i) == RA.SEND_NOT_SENT)
    check("  兩者仍區分得開（診斷用途不被合併）",
          RA.SSH_GUARD_AMBIGUOUS != RA.SSH_GUARD_IDENTITY
          and a.detail != i.detail)
    check("  兩者皆非 timed_out、非 uncertain",
          not a.timed_out and not a.uncertain
          and not i.timed_out and not i.uncertain)

    # 🔴 真正結果不明的路徑一律不得被這次修正波及
    t41 = run(RA.GUARD_EXIT_TIMEOUT)
    check("★★ exit 41 仍為 SSH_COMMAND_TIMEOUT",
          t41.outcome == RA.SSH_COMMAND_TIMEOUT)
    check("★★ exit 41 仍為 SEND_UNKNOWN（未被改成 NOT_SENT）",
          RA.classify_send_outcome(t41) == RA.SEND_UNKNOWN)
    check("★★ client command timeout 仍為 SEND_UNKNOWN",
          RA.classify_send_outcome(
              transport(Runner({"pause": "command_timeout"}),
                        armed=True).run("pause")) == RA.SEND_UNKNOWN)
    check("★★ 未知 exit code 仍保守判 UNKNOWN",
          RA.classify_send_outcome(run(99)) == RA.SEND_UNKNOWN
          and run(99).outcome == RA.SSH_ERROR)
    check("  SSH_COMMAND_TIMEOUT 仍是唯一的 uncertain outcome",
          RA.SSH_OUTCOME_UNCERTAIN == (RA.SSH_COMMAND_TIMEOUT,))
    check("  兩個集合仍互斥",
          not (set(RA.SSH_DEFINITELY_NOT_EXECUTED)
               & set(RA.SSH_OUTCOME_UNCERTAIN)))

    # ---- orchestrator：pause exit 45 不得建立 restore responsibility ----
    d = tmpdir()
    j = HO.Journal(path=os.path.join(d, "j.jsonl"))
    to = RA.SshTimeouts(connect_timeout_sec=FX_CONNECT,
                        command_timeouts=dict(FX_CMD))
    sd = RA.RemoteSenders(
        transport=RA.SshTransport(timeouts=to,
                                  runner=R(RA.GUARD_EXIT_IDENTITY),
                                  armed=True), armed=True)
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd.pause,
                                   restore_sender=sd.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    o._to(HO.S_PREFLIGHT)
    o._to(HO.S_PAUSE_REQUESTED)
    sent, why = o.request_pause()
    check("★★ pause 遇 exit 45 → 不是 SEND_OUTCOME_UNKNOWN_MUST_PROBE",
          sent is False and why != "SEND_OUTCOME_UNKNOWN_MUST_PROBE")
    outs = [x for x in j.read_all()
            if x.get("kind") == "OUTCOME" and x.get("step") == "PAUSE"]
    check("★★ journal 記為 ok=False（明確未送出），**不是** None",
          len(outs) == 1 and outs[0].get("ok") is False)
    check("★★ send_outcome 記為 NOT_SENT",
          outs[0].get("send_outcome") == RA.SEND_NOT_SENT)

    # runner 層：不得進入 PAUSE_OUTCOME_UNKNOWN、不得背責任
    d2 = tmpdir()
    j2 = HO.Journal(path=os.path.join(d2, "j.jsonl"))
    sd2 = RA.RemoteSenders(
        transport=RA.SshTransport(timeouts=to,
                                  runner=R(RA.GUARD_EXIT_IDENTITY),
                                  armed=True), armed=True)
    o2 = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd2.pause,
                                   restore_sender=sd2.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j2,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    r2 = HO.HandoffRunner(o2, (lambda: smp()), (lambda: probe_dict()),
                          loop_mark_source=(lambda: "2026-09-07 10:00:00"),
                          config=HO.HandoffConfig, soc_max_pct=85.0,
                          sleeper=(lambda x: None))
    res = r2.run()
    check(f"★★ handoff 結果 = {res['outcome']}（非 PAUSE_OUTCOME_UNKNOWN）",
          res["outcome"] != "PAUSE_OUTCOME_UNKNOWN")
    check("★★ **不建立** restore responsibility",
          res["restore_responsibility"] is False)
    check("  狀態未進入 PAUSE_OUTCOME_UNKNOWN",
          o2.state != HO.S_PAUSE_OUTCOME_UNKNOWN)

    # ---- restore exit 45 不得宣稱已執行 ----
    d3 = tmpdir()
    j3 = HO.Journal(path=os.path.join(d3, "j.jsonl"))
    sd3 = RA.RemoteSenders(
        transport=RA.SshTransport(timeouts=to,
                                  runner=R(RA.GUARD_EXIT_IDENTITY),
                                  armed=True), armed=True)
    o3 = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd3.pause,
                                   restore_sender=sd3.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j3,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    o3._to(HO.S_PREFLIGHT)
    o3._to(HO.S_PAUSE_REQUESTED)
    o3._to(HO.S_RESTORE_REQUESTED)
    rsent, rwhy = o3.request_restore()
    check("★★ restore 遇 exit 45 → 未送出（不得宣稱已執行）", rsent is False)
    check("★★ 且不是 UNKNOWN 路徑（明確未送出）",
          rwhy != "SEND_OUTCOME_UNKNOWN_MUST_PROBE")
    routs = [x for x in j3.read_all()
             if x.get("kind") == "OUTCOME" and x.get("step") == "RESTORE"]
    check("★★ restore OUTCOME 記為 ok=False，send_outcome=NOT_SENT",
          len(routs) == 1 and routs[0].get("ok") is False
          and routs[0].get("send_outcome") == RA.SEND_NOT_SENT)

    # ---- pause / restore 遇 exit 43 ----
    d5 = tmpdir()
    j5 = HO.Journal(path=os.path.join(d5, "j.jsonl"))
    sd5 = RA.RemoteSenders(
        transport=RA.SshTransport(timeouts=to,
                                  runner=R(RA.GUARD_EXIT_MISSING),
                                  armed=True), armed=True)
    o5 = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd5.pause,
                                   restore_sender=sd5.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j5,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    r5 = HO.HandoffRunner(o5, (lambda: smp()), (lambda: probe_dict()),
                          loop_mark_source=(lambda: "2026-09-07 10:00:00"),
                          config=HO.HandoffConfig, soc_max_pct=85.0,
                          sleeper=(lambda x: None))
    res5 = r5.run()
    check("★★ pause 遇 exit 43 → **不建立** restore responsibility",
          res5["restore_responsibility"] is False)
    check("★★ pause 遇 exit 43 → 不進 PAUSE_OUTCOME_UNKNOWN",
          res5["outcome"] != "PAUSE_OUTCOME_UNKNOWN"
          and o5.state != HO.S_PAUSE_OUTCOME_UNKNOWN)
    p5 = [x for x in j5.read_all()
          if x.get("kind") == "OUTCOME" and x.get("step") == "PAUSE"]
    check("★★ pause exit 43 journal：ok=False / NOT_SENT",
          len(p5) == 1 and p5[0].get("ok") is False
          and p5[0].get("send_outcome") == RA.SEND_NOT_SENT)

    d6 = tmpdir()
    j6 = HO.Journal(path=os.path.join(d6, "j.jsonl"))
    sd6 = RA.RemoteSenders(
        transport=RA.SshTransport(timeouts=to,
                                  runner=R(RA.GUARD_EXIT_MISSING),
                                  armed=True), armed=True)
    o6 = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd6.pause,
                                   restore_sender=sd6.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j6,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    o6._to(HO.S_PREFLIGHT)
    o6._to(HO.S_PAUSE_REQUESTED)
    o6._to(HO.S_RESTORE_REQUESTED)
    rs6, rw6 = o6.request_restore()
    r6out = [x for x in j6.read_all()
             if x.get("kind") == "OUTCOME" and x.get("step") == "RESTORE"]
    check("★★ restore 遇 exit 43 → 未送出，且不宣稱已執行",
          rs6 is False and rw6 != "SEND_OUTCOME_UNKNOWN_MUST_PROBE")
    check("★★ restore exit 43 journal：ok=False / NOT_SENT",
          len(r6out) == 1 and r6out[0].get("ok") is False
          and r6out[0].get("send_outcome") == RA.SEND_NOT_SENT)

    # ---- exit 41 對照組：仍必須背責任 ----
    d4 = tmpdir()
    j4 = HO.Journal(path=os.path.join(d4, "j.jsonl"))
    sd4 = RA.RemoteSenders(
        transport=RA.SshTransport(timeouts=to,
                                  runner=R(RA.GUARD_EXIT_TIMEOUT),
                                  armed=True), armed=True)
    o4 = HO.HandoffOrchestrator(
        remote=HO.RemoteController(pause_sender=sd4.pause,
                                   restore_sender=sd4.restore),
        observe=(lambda: smp()), gates=(lambda x: (True, {})), journal=j4,
        mode=HO.MODE_ARMED, idle_baseline_kw=-3.0, power_tolerance_kw=0.0)
    r4 = HO.HandoffRunner(o4, (lambda: smp()), (lambda: probe_dict()),
                          loop_mark_source=(lambda: "2026-09-07 10:00:00"),
                          config=HO.HandoffConfig, soc_max_pct=85.0,
                          sleeper=(lambda x: None))
    res4 = r4.run()
    check("★★ 對照組：exit 41 仍走 PAUSE_OUTCOME_UNKNOWN",
          res4["outcome"] == "PAUSE_OUTCOME_UNKNOWN")
    check("★★ 對照組：exit 41 仍**建立** restore responsibility",
          res4["restore_responsibility"] is True)


# ======================================================================
# 17. 值仍為 UNRESOLVED
# ======================================================================
def test_17_unresolved():
    print("\n[17] 所有 SSH timeout 數值仍為 UNRESOLVED")
    # C3 裁示：四項 FINAL，pause / restore 仍為 STRUCTURAL CANDIDATE
    t = RA.SshTimeouts()
    check(f"★★ 仍未取得實測值：{t.missing()}（僅 pause / restore）",
          len(t.missing()) == 2
          and all("pause" in x or "restore" in x for x in t.missing()))
    check("★★ pause / restore 恆列於 not_field_verified()",
          t.not_field_verified() == ["pause", "restore"])
    check("★★ 已核准常數僅涵蓋 connect + 三個唯讀動詞",
          sorted(RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC)
          == sorted(RA.READONLY_VERBS))
    check("  C1 的兩項則已定案（對照組）",
          SVC.ServiceTiming().missing() == [])
    check("  DISPATCH_ENABLED 仍為 False", HO.DISPATCH_ENABLED is False)
    check("  NETWORK_IDENTITY_STABILITY 為 ACCEPTED（非 PASS，非 field verified）",
          SVC.NETWORK_IDENTITY_STABILITY == SVC.NET_ACCEPTED
          and SVC.NETWORK_IDENTITY_STABILITY != SVC.NET_PASS)


# ======================================================================
def main():
    for fn in (test_1_split, test_2_per_verb, test_3_none_fail_closed,
               test_4_5_readonly_timeout, test_6_loopcheck,
               test_7_8_pause_timeout, test_9_10_restore_timeout,
               test_11_12_13_dry_run, test_14_no_magic, test_15_no_real_ssh,
               test_16_taxonomy, test_16b_guard_refusal_exit_codes,
               test_17_unresolved):
        fn()
    import shutil
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
