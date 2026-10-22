# -*- coding: utf-8 -*-
"""
test_phase6_attempt4_postmortem.py — Attempt #4 postmortem 回歸
======================================================================
核心命題
    「FIRST LIVE dispatch 能力是 **scoped authorization**，不是 production
      dispatch enabled：AT REST 一律 False，只有在已授權 one-shot 的
      authority-acquired live-leg scope 內 temporarily True，finally 無條件關閉。
      終態必須反映真實 stage；artifact 必須帶出既有 canonical evidence；
      durable pause INTENT 必須在**真實 launcher 建構路徑**上也成立。」

涵蓋使用者指定項目
    D1 ~ D15   dispatch scope
    A1 ~ A7    artifact / ledger evidence 一致性
    LH1 ~ LH4  external liveness 歷程
    RL1 ~ RL5  real-launcher pause intent ＋ recovery
    CL1 ~ CL6  terminal classification precedence
    R4-1 ~     Attempt #4 reconstruction

用法
    python test_phase6_attempt4_postmortem.py        # exit 0 = PASS
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

import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402
import phase6_scheduled_first_live as SC             # noqa: E402
import run_scheduled_first_live as LAUNCH            # noqa: E402
import pcs_auto_control_runtime as RT                # noqa: E402

RESULTS = []
TMPDIRS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p6_attempt4_")
    TMPDIRS.append(d)
    return d


def full_evidence(**over):
    """16 項前置條件全數成立。"""
    e = dict((k, True) for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS)
    e.update(over)
    return e


def permit(ev=None, attempt_id="FIRST-LIVE-ATTEMPT5"):
    d = full_evidence() if ev is None else ev
    return FL.FirstLiveDispatchPermit(attempt_id, lambda: d)


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


# ======================================================================
# D — DISPATCH SCOPE
# ======================================================================
def test_D_dispatch_scope():
    print("\n[D] FIRST LIVE DISPATCH SCOPE")

    check("D1 import / 靜止狀態 → HO 與 RT dispatch 皆 False", rest_ok())
    okr, flags = FL.dispatch_flags_at_rest()
    check("D1b dispatch_flags_at_rest() 回報 rest state",
          okr and flags == {"ho_dispatch_enabled": False,
                            "rt_dispatch_enabled": False})

    # D2 authority 之前
    p = permit(full_evidence(authority_acquired=False,
                             authority_acquire_attempted=False))
    check("D2 authority 之前 → permit 不授權", p.authorizes() is False)
    try:
        with FL.first_live_dispatch_scope(p):
            ok = False
    except FL.FirstLivePermitDenied:
        ok = True
    check("D2b authority 之前 → 無法進入 scope", ok and rest_ok())

    # D3 GATE B fail
    p = permit(full_evidence(gate_b_ok=False))
    try:
        with FL.first_live_dispatch_scope(p):
            ok = False
    except FL.FirstLivePermitDenied:
        ok = True
    check("D3 GATE B fail → 無法 enable", ok and rest_ok())

    # D4 authority acquire fail
    p = permit(full_evidence(authority_acquired=False))
    try:
        with FL.first_live_dispatch_scope(p):
            ok = False
    except FL.FirstLivePermitDenied:
        ok = True
    check("D4 authority acquire fail → 無法 enable", ok and rest_ok())

    # D5 全部成立
    p = permit()
    inside = {}
    with FL.first_live_dispatch_scope(p):
        inside["ho"] = HO.DISPATCH_ENABLED
        inside["rt"] = RT.DISPATCH_ENABLED
    check("D5 authority acquired ＋ 完整授權 → scoped enable 允許",
          inside["ho"] is True)

    # D6 只有必要 flag
    check("D6 scope 內只有 HO.DISPATCH_ENABLED 為 True", inside["ho"] is True)
    check("D6b RT.DISPATCH_ENABLED 全程 False（live-leg 鏈不讀它）",
          inside["rt"] is False and RT.DISPATCH_ENABLED is False)
    check("D6c RemoteSenders / SshTransport 預設 armed 未被本 scope 改動",
          RA.RemoteSenders().armed is False)

    # D7 PASS 後全部 False
    check("D7 scope 正常結束 → 全部回 False", rest_ok())
    check("D7b permit 狀態 = PERMIT_EXITED", p.state == FL.PERMIT_EXITED)

    # D8 例外
    p8 = permit()
    try:
        with FL.first_live_dispatch_scope(p8):
            raise RuntimeError("live leg 例外")
    except RuntimeError:
        pass
    check("D8 live-leg 例外 → 仍然全部回 False", rest_ok())

    # D9 dispatch UNKNOWN
    p9 = permit()
    calls = []
    with FL.first_live_dispatch_scope(p9):
        calls.append(FL.DISPATCH_UNKNOWN)
    check("D9 dispatch UNKNOWN → 全部回 False 且無重送",
          rest_ok() and len(calls) == 1)
    check("D9b UNKNOWN 後 permit 不再授權（不得重取）",
          p9.authorizes() is False)

    # D10 crash / 新 process
    code = ("import sys; sys.path.insert(0, %r);"
            "import phase6_handoff_orchestrator as HO;"
            "import pcs_auto_control_runtime as RT;"
            "print(HO.DISPATCH_ENABLED, RT.DISPATCH_ENABLED)" % HERE)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    check("D10 新 process import → dispatch flag 預設回 False",
          out.stdout.decode("utf-8", "replace").strip() == "False False")

    # D11 同一 attempt 第二次進入
    p11 = permit()
    with FL.first_live_dispatch_scope(p11):
        pass
    try:
        with FL.first_live_dispatch_scope(p11):
            ok = False
    except FL.FirstLivePermitDenied:
        ok = True
    check("D11 同一 permit 第二次進入 → 拒絕", ok and rest_ok())

    # D12 已消耗的 attempt
    p12 = permit(full_evidence(one_shot_not_consumed=False))
    check("D12 one-shot 已消耗 → 拒絕", p12.authorizes() is False)

    # D13 validate-only
    p13 = permit(full_evidence(attempt_field_authorized=False,
                               inside_authorized_window=False))
    check("D13 未 field 授權 / 不在視窗（validate-only）→ 拒絕",
          p13.authorizes() is False)

    # D14 Unattended LIVE 不受影響
    check("D14 Unattended LIVE 未共用本機制（RT.DISPATCH_ENABLED 恆 False）",
          RT.DISPATCH_ENABLED is False)

    # D15 指令額度
    g = FL.CommandCountGuard()
    check("D15 CHARGE 上限 1 / DISCHARGE 0 / PCS STOP 1",
          g.LIMITS["phase6_charge"] == 1
          and g.LIMITS["phase6_discharge"] == 0
          and g.LIMITS["phase6_pcs_stop"] == 1)
    p15 = permit(full_evidence(charge_budget_available=False))
    check("D15b CHARGE 額度已用盡 → permit 拒絕", p15.authorizes() is False)

    # 逐項 Fail Closed
    for k in FL.FIRST_LIVE_PERMIT_PRECONDITIONS:
        pk = permit(full_evidence(**{k: False}))
        check("D16 缺 %s → 拒絕" % k, pk.authorizes() is False)

    # evidence 取不到
    bad = FL.FirstLiveDispatchPermit("X", lambda: (_ for _ in ()).throw(
        RuntimeError("boom")))
    check("D17 evidence_source 例外 → Fail Closed", bad.authorizes() is False)
    check("D18 permit=None → scope 拒絕", _denied(None))
    check("D19 rest state 已非 False → scope 拒絕（不得疊加）",
          _denied_when_not_rest())
    check("D19b 上述拒絕後仍回到 rest state", rest_ok())


def _denied(p):
    try:
        with FL.first_live_dispatch_scope(p):
            return False
    except FL.FirstLivePermitDenied:
        return True


def _denied_when_not_rest():
    HO.DISPATCH_ENABLED = True
    try:
        return _denied(permit())
    finally:
        HO.DISPATCH_ENABLED = False


# ======================================================================
# HandoffRunner step 7 permit gate
# ======================================================================
def test_step7_gate():
    print("\n[D-7] HandoffRunner step 7 permit gate")
    check("D7-1 無 permit → _permit_authorizes False",
          HO._permit_authorizes(None) is False)

    class NoApi(object):
        pass

    check("D7-2 permit 沒有 authorizes() → False",
          HO._permit_authorizes(NoApi()) is False)

    class Boom(object):
        def authorizes(self):
            raise RuntimeError("x")

    check("D7-3 authorizes() 拋例外 → False",
          HO._permit_authorizes(Boom()) is False)

    class Truthy(object):
        def authorizes(self):
            return 1            # 非 True

    check("D7-4 authorizes() 回非 True → False",
          HO._permit_authorizes(Truthy()) is False)
    check("D7-5 完整 permit → True", HO._permit_authorizes(permit()) is True)

    src = io.open(os.path.join(HERE, "phase6_handoff_orchestrator.py"),
                  encoding="utf-8").read()
    check("D7-6 step 7 gate 仍保留 DISPATCH_ENABLED 判斷（未被移除）",
          "if not DISPATCH_ENABLED and not _permit_authorizes(" in src)
    check("D7-7 模組層 rest state 仍為 False",
          "\nDISPATCH_ENABLED = False\n" in src)
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    check("D7-8 orchestrator 未新增 force / bypass 入口",
          not ({"FORCE_DISPATCH", "BYPASS_DISPATCH"} & names))


# ======================================================================
# A — ARTIFACT EVIDENCE 完整性
# ======================================================================
def a4_result(**over):
    """Attempt #4 真實 handoff result 形狀。"""
    r = {"outcome": "COMPLETE", "state": "COMPLETE", "ownership": "EXTERNAL",
         "restore_responsibility": False,
         "pause_verifier_state": RA.PV_VERIFIED,
         "normal_loop_halted": True,
         "pause_loop_marks": ["t1", "t1", "t1"],
         "restore_verifier_state": RA.RV_RESUMED,
         "release_before_restore": True,
         "post_check": {"ownership_not_both": True, "fault_clear": True,
                        "alarm_clear": True, "runtime_healthy": True,
                        "responsibility_cleared": True},
         "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT", "ok": True},
                   {"step": "2_PAUSE", "ok": True, "detail": "已送出"},
                   {"step": "3_4_PAUSE_VERIFY", "ok": True},
                   {"step": "5_OWNERSHIP", "ok": True},
                   {"step": "6_POST_PAUSE_PRECHECK", "ok": True},
                   {"step": "7_FIRST_LIVE", "ok": False,
                    "detail": "DISPATCH_ENABLED=False → 不得 dispatch"},
                   {"step": "10_RESTORE", "ok": True},
                   {"step": "11_RESTORE_VERIFY", "ok": True},
                   {"step": "12_COMPLETE", "ok": True}]}
    r.update(over)
    return r


def test_A_artifact_evidence():
    print("\n[A] ARTIFACT EVIDENCE 完整性")
    d = FL.collect_handoff_artifact(a4_result()).to_dict()
    check("A1 PauseVerifier PASS → artifact state VERIFIED",
          d["pause_verifier_state"] == RA.PV_VERIFIED)
    check("A2 loop halted → normal_loop_halted True",
          d["normal_loop_halted"] is True)
    check("A2b discriminative_pause_verified True",
          d["discriminative_pause_verified"] is True)
    check("A3 RestoreVerifier PASS → canonical RESUMED",
          d["restore_verifier_state"] == RA.RV_RESUMED)
    check("A4 restore complete → restore_resumed True",
          d["restore_resumed"] is True)
    check("A5 release before restore → True",
          d["release_before_restore"] is True)
    check("A6 final external restored → final_ownership EXTERNAL",
          d["final_ownership"] == "EXTERNAL")
    check("A6b post_check 完整帶出",
          isinstance(d["post_check"], dict) and all(d["post_check"].values()))
    check("A7 terminal semantics：abort step = 7_FIRST_LIVE",
          d["handoff_abort_step"] == "7_FIRST_LIVE"
          and "DISPATCH_ENABLED" in d["handoff_abort_detail"])

    # 未提供 evidence 時仍 Fail Closed（不得用 default 掩蓋，也不得造假）
    bare = FL.collect_handoff_artifact(
        {"outcome": "ABORTED", "state": "ABORTED_PREFLIGHT", "steps": []}
    ).to_dict()
    check("A8 沒有 verifier evidence → 維持 None / False（不造假）",
          bare["pause_verifier_state"] is None
          and bare["restore_verifier_state"] is None
          and bare["restore_resumed"] is False
          and bare["discriminative_pause_verified"] is False)
    check("A9 呼叫端顯式參數優先於 runner evidence",
          FL.collect_handoff_artifact(
              a4_result(), pause_verifier_state="OVERRIDE"
          ).to_dict()["pause_verifier_state"] == "OVERRIDE")


# ======================================================================
# LH — EXTERNAL LIVENESS 歷程
# ======================================================================
def test_LH_liveness_history():
    print("\n[LH] EXTERNAL LIVENESS 歷程")
    fields = set(FL.HANDOFF_ARTIFACT_FIELDS)
    for k in ("pre_pause_external_liveness", "post_pause_external_liveness",
              "post_restore_external_liveness", "external_liveness_final"):
        check("LH1 artifact schema 具備 %s" % k, k in fields)
    check("LH2 舊欄位 external_liveness_final 保留（backward compatible）",
          "external_liveness_final" in fields)

    src = io.open(os.path.join(HERE, "phase6_first_live_field_provider.py"),
                  encoding="utf-8").read()
    check("LH3 provider 以 additive history 收集（不覆蓋）",
          'state.setdefault("liveness_history", []).append(rec)' in src)
    check("LH4 階段由實際送出的動詞推進（不自行判斷現場狀態）",
          'state["liveness_phase"] = "post_pause"' in src
          and 'state["liveness_phase"] = "post_restore"' in src)


# ======================================================================
# CL — TERMINAL CLASSIFICATION PRECEDENCE
# ======================================================================
def _runner(ledger_path, art=None, register=None, handoff=None):
    auth = SC.OneShotAuthorization(
        attempt_id="T", enabled=True, target_date=datetime.date(2026, 9, 25),
        window_start=datetime.time(0, 5), window_end=datetime.time(0, 30),
        timezone_name="Asia/Taipei", created_at=None)
    return SC.ScheduledFirstLiveRunner(
        auth=auth, ledger=SC.OneShotLedger(path=ledger_path),
        now_fn=(lambda: datetime.datetime(2026, 9, 25, 0, 6)),
        source_state_check=(lambda: {}),
        safety_preflight=(lambda: (True, {})),
        gate_a=(lambda: (True, {}, "V")),
        handoff_readiness=(lambda: (True, {})),
        arm_and_run_handoff=handoff or (lambda: a4_result()),
        collect_artifact=(lambda res: (art if art is not None
                                       else FL.collect_handoff_artifact(
                                           res).to_dict())),
        safety_reset=(lambda: {}), register_pause_intent_hook=register)


def test_CL_classification():
    print("\n[CL] TERMINAL CLASSIFICATION PRECEDENCE")
    r = _runner(os.path.join(tmpdir(), "cl.jsonl"))
    a = FL.collect_handoff_artifact(a4_result()).to_dict()
    res, detail = r._classify(a)
    check("CL1 ★★ Attempt #4 artifact → ABORTED_FIRST_LIVE",
          res == SC.R_ABORTED_FIRST_LIVE)
    check("CL1b 不再被歸成 ABORTED_POST_HANDOFF_REVALIDATION",
          res != SC.R_ABORTED_POST_HANDOFF)
    check("CL1c detail 保留真實原因", "DISPATCH_ENABLED" in str(detail))
    check("CL2 pause 已 SENT 不得讓 step 被忽略",
          a["pause_send_outcome"] == RA.SEND_SENT
          and r._classify(a)[0] == SC.R_ABORTED_FIRST_LIVE)

    a6 = FL.collect_handoff_artifact(a4_result(steps=[
        {"step": "2_PAUSE", "ok": True},
        {"step": "6_POST_PAUSE_PRECHECK", "ok": False,
         "detail": "未通過：['ownership_not_both']"}])).to_dict()
    check("CL3 6_POST_PAUSE_PRECHECK → ABORTED_POST_HANDOFF_REVALIDATION",
          r._classify(a6)[0] == SC.R_ABORTED_POST_HANDOFF)

    a7 = dict(a, handoff_abort_step=None,
              authority_acquire_attempted=True,
              phase6_authority_acquired=False)
    check("CL4 acquire 真被呼叫且失敗 → AUTHORITY_ACQUIRE_FAILED",
          r._classify(a7)[0] == SC.R_AUTHORITY_FAILED)
    a8 = dict(a7, authority_acquire_attempted=False)
    check("CL5 acquire 未被呼叫 → 不得分類為 AUTHORITY_ACQUIRE_FAILED",
          r._classify(a8)[0] != SC.R_AUTHORITY_FAILED)

    check("CL6 precedence 常數已登記",
          SC.RESULT_PRECEDENCE[0] == "real_terminal_step")
    check("CL6b ABORTED_FIRST_LIVE 已登記為正式終態",
          SC.R_ABORTED_FIRST_LIVE == "ABORTED_FIRST_LIVE"
          and SC.STEP_RESULT_MAP["7_FIRST_LIVE"] == SC.R_ABORTED_FIRST_LIVE)


# ======================================================================
# RL — REAL LAUNCHER pause intent ＋ recovery
# ======================================================================
class _FakeProvider(object):
    """走 **真實 launcher 建構路徑**（build_dependencies -> runner kwargs）。"""

    PROVIDER_ID = "phase6_first_live_field"

    def __init__(self, pre_pause_ok=True, crash_after_intent=False):
        self.pre_pause_ok = pre_pause_ok
        self.crash = crash_after_intent
        self.hook = None
        self.pause_sent = 0

    # --- provider 介面 ---
    def validate_provider_definition(self, cfg=None):
        return True, {k: True for k in ("provider_id_match",
                                        "core_modules_importable",
                                        "guard_variant_b2",
                                        "credentials_present",
                                        "safe_defaults_intact",
                                        "interface_complete",
                                        "no_field_io_on_import")}

    def build(self, cfg, runtime_context=None):
        def register(fn):
            self.hook = fn

        def handoff():
            if not self.pre_pause_ok:
                return {"outcome": "ABORTED", "state": "ABORTED_PREFLIGHT",
                        "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT",
                                   "ok": False, "detail": "未通過"}]}
            # 🔴 與 production `_send("pause")` 相同順序：hook -> 實際送出
            if self.hook is not None:
                self.hook()
            if self.crash:
                raise RuntimeError("crash before outcome")
            self.pause_sent += 1
            return a4_result()

        return {"now_fn": (lambda: datetime.datetime(2026, 9, 25, 0, 6)),
                "source_state_check": (lambda: {}),
                "safety_preflight": (lambda: (True, {})),
                "gate_a": (lambda: (True, {}, "V")),
                "handoff_readiness": (lambda: (True, {})),
                "arm_and_run_handoff": handoff,
                "register_pause_intent_hook": register,
                "collect_artifact": (lambda res:
                                     FL.collect_handoff_artifact(res).to_dict()),
                "safety_reset": (lambda: {}),
                "host_evidence": None, "waiter": (lambda s: None)}


def _launch(provider, d, attempt="RL", date_="2026-09-25"):
    cfg_path = os.path.join(d, "cfg.json")
    json.dump({"enabled": True, "attempt_id": attempt,
               "target_date": date_, "window_start": "00:00",
               "window_end": "23:59", "timezone": "Asia/Taipei",
               "dependency_provider": "phase6_first_live_field",
               "ledger_path": os.path.join(d, "l.jsonl"),
               "artifact_dir": os.path.join(d, "art")},
              io.open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False)
    st, cfg, _p = LAUNCH.load_config(cfg_path)
    assert st == LAUNCH.CONFIG_OK
    auth = LAUNCH.build_authorization(cfg)
    rtctx = {"attempt_id": cfg["attempt_id"],
             "authorization": {"attempt_id": auth.attempt_id},
             "field_authorized": True, "one_shot_consumed": False,
             "inside_window": True}
    deps = LAUNCH.build_dependencies(provider, cfg, rtctx)
    ledger = SC.OneShotLedger(cfg["ledger_path"], now_fn=deps["now_fn"])
    runner = SC.ScheduledFirstLiveRunner(
        auth=auth, ledger=ledger, now_fn=deps["now_fn"],
        source_state_check=deps["source_state_check"],
        safety_preflight=deps["safety_preflight"],
        gate_a=deps["gate_a"], handoff_readiness=deps["handoff_readiness"],
        arm_and_run_handoff=deps["arm_and_run_handoff"],
        register_pause_intent_hook=deps.get("register_pause_intent_hook"),
        collect_artifact=deps.get("collect_artifact"),
        safety_reset=deps.get("safety_reset"),
        host_evidence=deps.get("host_evidence"),
        waiter=deps.get("waiter"))
    return runner, ledger, cfg


def test_RL_real_launcher():
    print("\n[RL] REAL LAUNCHER pause intent ＋ recovery")
    src = io.open(os.path.join(HERE, "run_scheduled_first_live.py"),
                  encoding="utf-8").read()
    check("RL0 ★★ launcher 顯式傳入 register_pause_intent_hook",
          'register_pause_intent_hook=deps.get("register_pause_intent_hook")'
          in src)
    tree = ast.parse(src)
    n = sum(1 for kw in ast.walk(tree) if isinstance(kw, ast.keyword)
            and kw.arg == "register_pause_intent_hook")
    check("RL0b 只有一個 intent callback（未重複建立第二套）", n == 1)

    # RL1 PRE_PAUSE PASS -> INTENT -> pause -> OUTCOME
    d = tmpdir()
    prov = _FakeProvider()
    runner, ledger, _c = _launch(prov, d)
    ev = runner.run()
    recs = ledger.records
    kinds = [(r.get("kind"), r.get("verb")) for r in recs]
    i_intent = next((i for i, k in enumerate(kinds)
                     if k == ("INTENT", "pause")), None)
    i_out = next((i for i, k in enumerate(kinds)
                  if k == ("OUTCOME", "pause")), None)
    check("RL1 ★★ 真實 launcher 路徑：scheduled ledger 有 pause INTENT",
          i_intent is not None)
    check("RL1b pause OUTCOME 也存在", i_out is not None)
    check("RL1c ★★ INTENT seq < OUTCOME seq",
          i_intent is not None and i_out is not None and i_intent < i_out)
    check("RL1d lifecycle 曾進入 EXTERNAL_PAUSE_RESPONSIBILITY",
          any(r.get("lifecycle") == SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY
              for r in recs))
    check("RL1e ledger.pause_attempted = True", ledger.attempted("pause"))

    # RL2 pre-pause FAIL -> 無 INTENT
    d2 = tmpdir()
    prov2 = _FakeProvider(pre_pause_ok=False)
    runner2, ledger2, _c2 = _launch(prov2, d2, attempt="RL2")
    ev2 = runner2.run()
    check("RL2 ★★ pre-pause FAIL → scheduled ledger 無 pause INTENT",
          ledger2.attempted("pause") is False)
    check("RL2b 責任未成立", ledger2.restore_responsibility is False)
    check("RL2c 終態 = ABORTED_PRE_PAUSE_PREFLIGHT",
          ev2.get("result") == SC.R_ABORTED_PRE_PAUSE)

    # RL3 crash before outcome -> recovery 必須認定責任存在
    d3 = tmpdir()
    prov3 = _FakeProvider(crash_after_intent=True)
    runner3, ledger3, cfg3 = _launch(prov3, d3, attempt="RL3")
    runner3.run()
    reread = SC.OneShotLedger(cfg3["ledger_path"])
    check("RL3 INTENT 已 durable（crash 前落盤）", reread.attempted("pause"))
    check("RL3b ★★ restore responsibility 成立",
          reread.restore_responsibility is True)
    verdict, action = SC.recover(reread)
    check("RL3c ★★ SC.recover() 不得回 R_CLEAN",
          verdict != SC.REC_CLEAN)
    check("RL3d verdict 指向需要 restore / 人工處理",
          verdict in (SC.REC_PENDING_RESTORE, SC.REC_CONSUMED,
                      SC.REC_CRITICAL))

    # RL4 舊缺陷重現：不傳 hook -> 缺 INTENT
    d4 = tmpdir()
    prov4 = _FakeProvider()
    runner4, ledger4, cfg4 = _launch(prov4, d4, attempt="RL4")
    runner4._register_pause_hook = None          # 模擬 Attempt #4 的漏傳
    runner4.run()
    check("RL4 ★★ 未傳 hook → 重現 Attempt #4 缺陷（OUTCOME 有、INTENT 無）",
          ledger4.attempted("pause") is False
          and ledger4.outcome_of("pause") == RA.SEND_SENT)
    check("RL4b 該狀態下 recover 會誤判 —— 證明修正的必要性",
          SC.recover(SC.OneShotLedger(cfg4["ledger_path"]))[0]
          != SC.REC_PENDING_RESTORE)

    # RL5 build_dependencies 仍要求必要相依
    class Missing(_FakeProvider):
        def build(self, cfg, runtime_context=None):
            d = super().build(cfg, runtime_context)
            d["gate_a"] = None
            return d

    try:
        _launch(Missing(), tmpdir(), attempt="RL5")
        ok = False
    except LAUNCH.ConfigError:
        ok = True
    check("RL5 必要相依缺漏 → ConfigError（Fail Closed）", ok)


# ======================================================================
# R4 — ATTEMPT #4 RECONSTRUCTION
# ======================================================================
def test_R4_reconstruction():
    print("\n[R4] ATTEMPT #4 RECONSTRUCTION（offline，mock live leg）")
    guard = FL.CommandCountGuard()
    rec = FL.LifecycleRecorder()
    lease = FL.Phase6AuthorityLease(acquire=(lambda: (True, "ok")),
                                    release=(lambda: True), recorder=rec)
    observed = {"inside": None, "charge_kw": None}

    class _Leg(object):
        """mock LiveLegOutcome —— 形狀沿用既有 `classify_dispatch_outcome` 契約。"""
        stop_count = 0
        dispatch_count = 1
        leg_state = FL.LEG_COMPLETE
        reason = FL.LEG_COMPLETE

        def as_dict(self):
            return {"action": "charge", "target_kw": 5.0,
                    "leg_state": FL.LEG_COMPLETE, "dispatch_count": 1}

    def mock_live_leg():
        observed["inside"] = HO.DISPATCH_ENABLED
        observed["charge_kw"] = 5.0
        return _Leg()

    ev = full_evidence()

    def evidence():
        # 依實際 lease / guard 狀態回報（不是硬填 True）
        e = dict(ev)
        e["authority_acquire_attempted"] = lease.acquire_attempted
        e["authority_acquired"] = lease.held
        c = guard.as_dict()
        e["charge_budget_available"] = (
            int(c["counts"].get("phase6_charge", 0) or 0)
            < int(c["limits"]["phase6_charge"]))
        return e

    p = FL.FirstLiveDispatchPermit("FIRST-LIVE-ATTEMPT4-20260924", evidence)
    events = []
    disp = FL.FirstLiveDispatcher(
        live_leg_runner=mock_live_leg, authority_lease=lease,
        command_guard=guard, recorder=rec, dispatch_permit=p,
        permit_event=(lambda k, a: events.append(k)))

    check("R4-1 dispatch 之前 rest state = False", rest_ok())
    check("R4-2 acquire 之前 permit 不授權（authority_acquired False）",
          p.authorizes() is False)

    out = disp()
    check("R4-3 ★★ Attempt #4 條件 → dispatcher 成功執行 live leg",
          out["dispatched"] is True)
    check("R4-4 ★★ live leg 執行當下 DISPATCH_ENABLED = True",
          observed["inside"] is True)
    check("R4-5 ★★ CHARGE 5.0 kW ×1", observed["charge_kw"] == 5.0)
    check("R4-6 ★★ 離開後 DISPATCH_ENABLED 回 False", rest_ok())
    check("R4-7 permit 事件完整（ENTERED -> EXITED）",
          events == ["PERMIT_ENTERED", "PERMIT_EXITED"])
    check("R4-8 permit 狀態 = PERMIT_EXITED", p.state == FL.PERMIT_EXITED)
    c = guard.as_dict()
    check("R4-9 CHARGE 計數 = 1，無違規",
          c["counts"].get("phase6_charge") == 1 and c["violations"] == [])
    check("R4-10 DISCHARGE 未被使用",
          c["counts"].get("phase6_discharge", 0) == 0)
    check("R4-11 authority 已釋放（release 在 finally）",
          out["released"] is True and lease.held is False)
    check("R4-12 outcome = DISPATCH_SUCCEEDED",
          out["outcome"] == FL.DISPATCH_SUCCEEDED)
    check("R4-13 permit 用畢後不得再授權", p.authorizes() is False)

    # 對照組：無 permit -> 不建立 scope（沿用舊行為）
    guard2 = FL.CommandCountGuard()
    lease2 = FL.Phase6AuthorityLease(acquire=(lambda: (True, "ok")),
                                     release=(lambda: True))
    seen = {}

    def leg2():
        seen["inside"] = HO.DISPATCH_ENABLED
        return _Leg()

    FL.FirstLiveDispatcher(live_leg_runner=leg2, authority_lease=lease2,
                           command_guard=guard2)()
    check("R4-14 無 permit → live leg 期間 DISPATCH_ENABLED 仍為 False",
          seen["inside"] is False and rest_ok())


# ======================================================================
def main():
    print("=" * 72)
    print("  Attempt #4 POSTMORTEM 回歸")
    print("=" * 72)
    for fn in (test_D_dispatch_scope, test_step7_gate, test_A_artifact_evidence,
               test_LH_liveness_history, test_CL_classification,
               test_RL_real_launcher, test_R4_reconstruction):
        fn()
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print(f"  rest state 最終確認：HO={HO.DISPATCH_ENABLED} "
          f"RT={RT.DISPATCH_ENABLED}")
    print("=" * 72)
    return 0 if (n == tot and rest_ok()) else 1


if __name__ == "__main__":
    sys.exit(main())
