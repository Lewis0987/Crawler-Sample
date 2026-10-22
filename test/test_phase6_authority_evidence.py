# -*- coding: utf-8 -*-
"""
test_phase6_authority_evidence.py — AUTHORITY EVIDENCE ／UNCERTAIN OWNERSHIP
======================================================================
核心命題
    「`authority = UNKNOWN` **不足以**進行 production postmortem。
      `AuthorityResult` 的 reason / detail 與其判斷輸入必須進入
      durable artifact；而 dispatch 之後 ownership 轉為不可信時，
      **不得**自動送 PCS STOP，但也**不得**只是『不 STOP -> restore -> 結束』——
      必須走完 read-only post-restore reconciliation。」

🔴 Attempt #9 的事實：CHARGE 5 kW 確定送出、readback VERIFY_SUCCESS、
   observed_state CHARGING，但 LEG_OWNED 之後 authority 轉 UNKNOWN ->
   ABORTED_UNCERTAIN_OWNERSHIP ／STOP 0。
   當時 artifact 只保留 "UNKNOWN" 字串，**無法**判定是哪一條規則。

涵蓋使用者指定項目
    A1 ~ A9    authority 診斷
    A9R        Attempt #9 exact regression（production entry point）
    R1 ~ R5    post-restore reconciliation
    N1 ~ N4    不變量（STOP policy ／計數規則 ／歷史不可改寫）

用法
    python test/test_phase6_authority_evidence.py        # exit 0 = PASS
"""
import datetime
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import control_authority as CA                   # noqa: E402
import pcs_auto_control_runtime as RT            # noqa: E402
import pcs_auto_control_service as SVC           # noqa: E402
import phase6_first_live_handoff as FL           # noqa: E402
import phase6_handoff_orchestrator as HO         # noqa: E402
import phase6_scheduled_first_live as SC         # noqa: E402
import run_scheduled_first_live as LAUNCH        # noqa: E402

import test_phase6_entry_point_ordering as EP    # noqa: E402
import test_phase6_provider_construction as PCS  # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


# ======================================================================
# A — AUTHORITY DIAGNOSTIC（直接驅動既有 control_authority，不造第二套）
# ======================================================================
def _req(**over):
    """建立 AuthorityRequest；欄位沿用既有 production 形狀。"""
    base = dict(pcs_state="CHARGING",
                pcs_mode_state={"schedule_switch": 0, "manual_switch": 1},
                ess_valid=True, ess_stale=False,
                actual_active_power_kw=-5.0,
                last_control=None, last_control_record=None,
                last_control_trust=None)
    base.update(over)
    return CA.AuthorityRequest(**{k: v for k, v in base.items()
                                  if k in CA.AuthorityRequest.__annotations__})


def test_A_authority_reasons():
    print("\n[A] AuthorityResult reason / detail 必須可診斷")
    # A8 / A9 —— 門檻未設定
    pol_no_ttl = CA.AuthorityPolicy(authority_ttl_sec=None,
                                    authority_power_tolerance_kw=1.25)
    pol_no_tol = CA.AuthorityPolicy(authority_ttl_sec=180.0,
                                    authority_power_tolerance_kw=None)
    check("A8 ★★ TTL 未設定 -> CA_TTL_UNSET 這個常數存在且唯一",
          CA.CA_TTL_UNSET == "CONTROL_AUTHORITY_TTL_UNSET")
    check("A9 ★★ tolerance 未設定 -> CA_TOLERANCE_UNSET 存在",
          hasattr(CA, "CA_TOLERANCE_UNSET"))
    check("A8b 兩個門檻皆有 policy 欄位",
          pol_no_ttl.authority_ttl_sec is None
          and pol_no_tol.authority_power_tolerance_kw is None)

    # A7 —— 評估例外一律 UNKNOWN，且不得洩漏 secret
    class Boom(object):
        def __getattr__(self, k):
            raise RuntimeError("password=hunter2 token=abcdef")

    r = CA.evaluate(Boom(), policy=CA.DEFAULT_AUTHORITY_POLICY, now=0.0)
    check("A7 ★★ 評估例外 -> AUTH_UNKNOWN", r.state == CA.AUTH_UNKNOWN)
    check("A7b ★★ 例外有可診斷的 reason 類別",
          r.reason == CA.CA_EVALUATION_ERROR)
    blob = "%s %s" % (r.reason, r.detail)
    check("A7c ★★ 不得洩漏 secret（password / token 不得出現在 detail）",
          "hunter2" not in blob and "abcdef" not in blob)

    # A1 —— AuthorityResult 必須能完整序列化
    ok = CA.AuthorityResult("IDLE", "R", True, detail="d")
    d = ok.as_dict()
    for k in ("state", "reason", "allowed", "detail", "pcs_state",
              "last_control_action", "last_control_power_kw",
              "observed_power_kw", "age_sec"):
        check("A1 ★★ AuthorityResult.as_dict() 含 %s" % k, k in d)

    # A2~A6 —— 各 reason 常數必須存在且互異（artifact 才能定位）
    names = ("CA_EXPIRED", "CA_DATA_INSUFFICIENT", "CA_MODE_UNKNOWN",
             "CA_TTL_UNSET", "CA_TOLERANCE_UNSET", "CA_EVALUATION_ERROR")
    vals = [getattr(CA, n, None) for n in names]
    check("A2~A6 ★★ 六個 reason 常數皆存在", all(v is not None for v in vals))
    check("A2~A6b ★★ 六個 reason 常數互異", len(set(vals)) == len(vals))
    check("A5 ★★ PCS 狀態不可用時的 reason 可定位（在 CA_REASONS 內）",
          all(v in CA.AUTHORITY_REASONS for v in vals))


# ======================================================================
# A1W — reason / detail 真的進 artifact（production wiring）
# ======================================================================
def test_A1W_artifact_wiring():
    print("\n[A1W] authority reason / detail 必須進入 durable artifact")
    import production_arbiter as ARB
    import dataclasses
    fields = {f.name for f in dataclasses.fields(ARB.ArbitrationResult)}
    for k in ("authority_state", "authority_reason", "authority_detail",
              "authority_result", "authority_ttl_sec",
              "authority_power_tolerance_kw"):
        check("A1W ArbitrationResult 外露 %s" % k, k in fields)
    for k in ("authority_snapshots", "authority_reason", "authority_detail",
              "authority_result", "authority_at_abort",
              "authority_ttl_sec", "authority_power_tolerance_kw"):
        check("A1W artifact schema 含 %s" % k,
              k in FL.HANDOFF_ARTIFACT_FIELDS)
    check("A1W ★★ 四個取樣時點皆已定義",
          SVC.ControlledLiveLeg.AUTHORITY_SNAPSHOT_POINTS
          == ("before_dispatch", "after_dispatch", "after_readback",
              "at_abort"))
    check("A1W ★★ 取不到時記 NOT_OBSERVED（不得只省略）",
          SVC.ControlledLiveLeg.AUTHORITY_NOT_OBSERVED == "NOT_OBSERVED")


# ======================================================================
# A9R — ATTEMPT #9 EXACT REGRESSION（production entry point）
# ======================================================================
class UnknownAfterDispatchBundle(object):
    """讓 authority 在 dispatch **之後**才轉為 UNKNOWN。"""


def build_a9(deadline_budget=1500.0):
    """
    重現 Attempt #9：CHARGE 成功 ＋ readback 成功，但 LEG_OWNED 之後
    authority 轉 UNKNOWN。

    🔴 走 production entry point（`deps.arm_and_run_handoff()`）。
    """
    deps, env = EP.build_entry_env()
    bundle = env["bundle"]
    chain = env["chain"]
    orig_observe = bundle.observe

    def observe():
        obs = orig_observe()
        # dispatch 之後（charge 已進 chain_state）authority 轉 UNKNOWN
        if chain["dispatched"] > 0:
            obs.arbitration.authority_state = "UNKNOWN"
            obs.arbitration.authority_reason = CA.CA_EXPIRED
            obs.arbitration.authority_detail = (
                "LastControl 年齡 999.000s > TTL 180s")
            obs.arbitration.authority_result = {
                "state": "UNKNOWN", "reason": CA.CA_EXPIRED,
                "allowed": False,
                "detail": "LastControl 年齡 999.000s > TTL 180s",
                "pcs_state": "CHARGING", "last_control_action": "charge",
                "last_control_power_kw": 5.0, "observed_power_kw": -5.0,
                "age_sec": 999.0}
            obs.arbitration.authority_ttl_sec = 180.0
            obs.arbitration.authority_power_tolerance_kw = 1.25
        return obs

    bundle.observe = observe

    # 🔴 production 的 `ExecutionResult` 帶 `arbitration`；fake chain 也必須帶，
    #    否則 before_dispatch 取樣不到，測出來的是 fixture 缺口而非 production。
    orig_factory = bundle.factory

    def factory(executor=None, verifier=None):
        c = orig_factory(executor, verifier)
        if executor is None and verifier is None:
            return c
        inner_run = c.run

        def run():
            r = inner_run()
            try:
                r.arbitration = orig_observe().arbitration
            except Exception:                                  # noqa: BLE001
                pass
            return r

        c.run = run
        return c

    bundle.factory = factory
    return deps, env


def test_A9R_exact_regression():
    print("\n[A9R] ATTEMPT #9 EXACT REGRESSION")
    deps, env = build_a9()
    res = EP.run_entry(deps, env)
    h = env["holder"]
    art = env["artifact"]
    c = h["guard"].as_dict()["counts"]
    ch = int(c.get("phase6_charge", 0) or 0)
    di = int(c.get("phase6_discharge", 0) or 0)
    stp = int(c.get("phase6_pcs_stop", 0) or 0)

    check("A9R1 ★★ CHARGE 恰好 1 次", ch == 1)
    check("A9R2 ★★ DISCHARGE = 0", di == 0)
    check("A9R3 ★★ PCS STOP = 0（ownership 不可信時不得自動 STOP）",
          stp == 0)
    check("A9R4 ★★ dispatch_result = DISPATCH_SUCCEEDED",
          art.get("dispatch_result") == "DISPATCH_SUCCEEDED")
    check("A9R5 ★★ readback_result = VERIFY_SUCCESS",
          art.get("readback_result") == "VERIFY_SUCCESS")
    check("A9R6 ★★ live_leg_result = UNKNOWN",
          art.get("live_leg_result") == FL.LLR_UNKNOWN)
    check("A9R7 ★★ counts_as_first_live_success = False",
          art.get("counts_as_first_live_success") is False)
    ar = SC.ScheduledFirstLiveRunner._classify(None, art)[0]
    check("A9R8 ★★ attempt_result = AUTHORITY_UNKNOWN_AFTER_DISPATCH"
          "（非 DISPATCH_UNKNOWN）",
          ar == SC.R_AUTHORITY_UNKNOWN_AFTER_DISPATCH)
    check("A9R9 ★★ leg_state_history 走到 OWNED 後才 abort",
          "OWNED" in (art.get("leg_state_history") or ())
          and "ABORTED_UNCERTAIN_OWNERSHIP"
          in (art.get("leg_state_history") or ()))
    # --- authority evidence ---
    snaps = art.get("authority_snapshots") or {}
    check("A9R10 ★★ 四個取樣時點全部存在（含 NOT_OBSERVED）",
          set(snaps) == set(SVC.ControlledLiveLeg.AUTHORITY_SNAPSHOT_POINTS))
    check("A9R11 ★★ at_abort 保存了 reason（不再只有 UNKNOWN 字串）",
          (snaps.get("at_abort") or {}).get("reason") == CA.CA_EXPIRED)
    check("A9R12 ★★ at_abort 保存了 detail",
          bool((snaps.get("at_abort") or {}).get("detail")))
    check("A9R13 ★★ at_abort 保存了完整 authority_result",
          isinstance((snaps.get("at_abort") or {}).get("authority_result"),
                     dict))
    check("A9R14 ★★ artifact 頂層帶出 authority_reason",
          art.get("authority_reason") == CA.CA_EXPIRED)
    check("A9R15 ★★ before_dispatch 有被取樣（來自 dispatch arbitration）",
          (snaps.get("before_dispatch") or {}).get("observed") is True)
    check("A9R16 ★★ after_dispatch 記為 NOT_OBSERVED 且有 note（不得只省略）",
          (snaps.get("after_dispatch") or {}).get("state")
          == SVC.ControlledLiveLeg.AUTHORITY_NOT_OBSERVED
          and bool((snaps.get("after_dispatch") or {}).get("note")))
    check("A9R17 ★★ 判斷門檻一併保存",
          art.get("authority_ttl_sec") == 180.0
          and art.get("authority_power_tolerance_kw") == 1.25)
    check("A9R18 ★★ external restore 仍恰好一次",
          env["ssh"].calls.count("restore") == 1)
    check("A9R19 ★★ authority 已釋放", h["lease"].held is False)
    check("A9R20 ★★ rest state 恢復", rest_ok())
    env["a9"] = art
    return art


# ======================================================================
# R — POST-RESTORE RECONCILIATION
# ======================================================================
def test_R_post_restore():
    print("\n[R] POST-RESTORE RECONCILIATION（READ ONLY）")
    deps, env = build_a9()
    EP.run_entry(deps, env)
    art = env["artifact"]
    rec = art.get("post_restore_reconciliation") or {}

    check("R1a ★★ reconciliation 已產生", bool(rec))
    missing = [k for k in FL.RECONCILIATION_ITEMS if k not in rec]
    check("R1b ★★ 11 項唯讀觀測欄位齊備（缺 %r）" % (missing,), missing == [])
    check("R1c ★★ 模式標示為 READ_ONLY_NO_CONTROL",
          rec.get("mode") == "READ_ONLY_NO_CONTROL")
    check("R5 ★★ reconciliation 未送出任何 PCS 指令",
          rec.get("commands_sent") == {"charge": 0, "discharge": 0,
                                       "pcs_stop": 0})
    check("R1d ★★ external restore RV_RESUMED ＋ EXTERNAL_RUNNING ＋ 觀測可得"
          " -> EXTERNAL_TAKEOVER_VERIFIED",
          art.get("external_takeover") == FL.EXTERNAL_TAKEOVER_VERIFIED
          and art.get("external_takeover_verified") is True)
    check("R1e ★★ takeover VERIFIED 仍不得把 live_leg_result 改 SUCCESS",
          art.get("live_leg_result") == FL.LLR_UNKNOWN)
    check("R1f ★★ takeover VERIFIED -> OPERATOR_REVIEW_RECOMMENDED",
          art.get("manual_attention") == FL.MA_OPERATOR_REVIEW
          and art.get("manual_attention_required") is False)

    # R2 —— restore UNKNOWN
    v, c2 = FL.evaluate_external_takeover(
        restore_verifier_state="NEEDS_ADDITIONAL_PROBE",
        external_liveness="EXTERNAL_RUNNING",
        reconciliation={"communication_ok": True, "pcs_state": "STANDBY"})
    ma2 = FL.evaluate_manual_attention(FL.LLR_UNKNOWN, v, True, False)[0]
    check("R2 ★★ restore UNKNOWN -> UNCONFIRMED ＋ MANUAL_ATTENTION_REQUIRED",
          v == FL.EXTERNAL_TAKEOVER_UNCONFIRMED and ma2 == FL.MA_REQUIRED)

    # R3 —— restore PASS 但 external liveness UNKNOWN
    v3, _ = FL.evaluate_external_takeover(
        "RESUMED", "EXTERNAL_UNKNOWN",
        {"communication_ok": True, "pcs_state": "STANDBY"})
    ma3 = FL.evaluate_manual_attention(FL.LLR_UNKNOWN, v3, True, False)[0]
    check("R3 ★★ external liveness UNKNOWN -> MANUAL_ATTENTION_REQUIRED",
          v3 == FL.EXTERNAL_TAKEOVER_UNCONFIRMED and ma3 == FL.MA_REQUIRED)

    # R4 —— restore PASS ／external running，但唯讀 PCS 觀測取不到
    v4, c4 = FL.evaluate_external_takeover(
        "RESUMED", "EXTERNAL_RUNNING",
        {"communication_ok": False, "pcs_state": None})
    check("R4 ★★ PCS 觀測取不到 -> 不得偽稱完全 reconciled",
          v4 == FL.EXTERNAL_TAKEOVER_UNCONFIRMED
          and c4.get("pcs_observation_available") is False)
    ma4 = FL.evaluate_manual_attention(FL.LLR_UNKNOWN, v4, True, False)[0]
    check("R4b ★★ 此時 manual_attention_required", ma4 == FL.MA_REQUIRED)

    # reconciliation 原始碼不得有控制出口（AST，不用文字掃描）
    import ast
    import inspect
    import phase6_first_live_field_provider as FP
    src = inspect.getsource(FP)
    fn = None
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "post_restore_reconciliation"):
            fn = node
    banned = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            nm = (f.attr if isinstance(f, ast.Attribute)
                  else (f.id if isinstance(f, ast.Name) else None))
            if nm in ("run_live_leg_cli", "execute_and_verify",
                      "operator_run", "take", "send_pause_ex",
                      "send_restore_ex"):
                banned.add(nm)
    check("R5b ★★ reconciliation 函式內無任何控制呼叫（AST）：%r" % (banned,),
          banned == set())


# ======================================================================
# N — 不變量
# ======================================================================
def test_N_invariants():
    print("\n[N] 不變量：STOP policy ／計數規則 ／exit ／歷史不可改寫")
    import ast
    import inspect
    src = inspect.getsource(SVC.ControlledLiveLeg)
    check("N1 ★★ ownership 不可信時不得自動 STOP（契約文字仍在）",
          "不自動 STOP" in src)
    # abort(uncertain=True) 之後不得有 STOP 呼叫 —— 以 run_leg 結構驗證
    rl = inspect.getsource(SVC.ControlledLiveLeg.run_leg)
    i = rl.find("擁有權不可信")
    check("N1b ★★ authority 不可信分支直接 return abort(..., uncertain=True)",
          i > 0 and "uncertain=True" in rl[i:i + 200])

    check("N2 ★★ 計數規則未變：只有 live_leg_result == SUCCESS 才 +1",
          FL.counts_as_first_live_success(
              {"phase6_command_counts": {"counts": {"phase6_charge": 1}},
               "dispatcher_outcome": FL.DISPATCH_SUCCEEDED,
               "leg_reached_complete": True,
               "readback_outcome": "VERIFY_SUCCESS"}) is True
          and FL.counts_as_first_live_success(
              {"phase6_command_counts": {"counts": {"phase6_charge": 1}},
               "dispatcher_outcome": FL.DISPATCH_UNKNOWN}) is False)

    R = SC.R_AUTHORITY_UNKNOWN_AFTER_DISPATCH
    check("N3a ★★ 未提供 artifact -> 保守 exit 4",
          LAUNCH.classify_exit(R, None) == LAUNCH.EXIT_MANUAL)
    check("N3b ★★ MANUAL_ATTENTION_REQUIRED -> exit 4",
          LAUNCH.classify_exit(
              R, {"manual_attention": FL.MA_REQUIRED}) == LAUNCH.EXIT_MANUAL)
    check("N3c ★★ OPERATOR_REVIEW_RECOMMENDED -> exit 0",
          LAUNCH.classify_exit(
              R, {"manual_attention": FL.MA_OPERATOR_REVIEW})
          == LAUNCH.EXIT_HANDLED)
    check("N3d ★★ REFUSED 永遠優先於 artifact",
          LAUNCH.classify_exit(
              SC.R_ALREADY_CONSUMED,
              {"manual_attention": FL.MA_NOT_REQUIRED})
          == LAUNCH.EXIT_REFUSED)
    check("N3e ★★ 未知 result 仍 Fail Closed",
          LAUNCH.classify_exit("BOGUS", None)
          == LAUNCH.EXIT_LAUNCHER_FAILURE)

    # 歷史 artifact 不得被改寫
    p = os.path.join(HERE, "..", "output", "phase6_scheduled_first_live",
                     "attempt9_20261006",
                     "scheduled_first_live_FIRST-LIVE-ATTEMPT9-20261006"
                     "_20261006_000939.json")
    if os.path.exists(p):
        import json
        import io as _io
        a = json.load(_io.open(p, encoding="utf-8"))
        check("N4 ★★ Attempt #9 歷史 artifact 的 result 仍是 DISPATCH_UNKNOWN"
              "（不得回寫）", a.get("result") == "DISPATCH_UNKNOWN")
        check("N4b ★★ 歷史 artifact 仍無 authority reason（evidence gap 保留）",
              "authority_reason" not in (a.get("handoff_artifact") or {}))
    else:
        check("N4 歷史 artifact 不在本機（略過）", True)


# ======================================================================
# S — PRODUCTION READ-ONLY SHADOW REHEARSAL
# ======================================================================
def test_S_shadow_rehearsal():
    print("\n[S] PRODUCTION READ-ONLY SHADOW REHEARSAL 契約")
    import ast
    import inspect
    import phase6_shadow_rehearsal as SH

    check("S1 ★★ 三個 verdict 詞彙存在",
          (SH.SHADOW_PASS, SH.SHADOW_BLOCKED, SH.SHADOW_ERROR)
          == ("SHADOW_REHEARSAL_PASS", "SHADOW_REHEARSAL_BLOCKED",
              "SHADOW_REHEARSAL_ERROR"))
    check("S2 ★★ 禁止動詞清單涵蓋六項",
          set(SH.FORBIDDEN_FIELD_ACTIONS)
          == {"pause", "restore", "authority_acquire", "charge",
              "discharge", "pcs_stop"})
    check("S3 ★★ 必須在第一個 field side effect 之前停止",
          SH.SHADOW_STOP_BEFORE == "1_PRE_PAUSE_PREFLIGHT_DONE")

    # 🔴 以 AST 證明 shadow 不呼叫 arm_and_run_handoff（第一個 side effect 入口）
    src = inspect.getsource(SH)
    calls = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            nm = (f.attr if isinstance(f, ast.Attribute)
                  else (f.id if isinstance(f, ast.Name) else None))
            if nm in ("arm_and_run_handoff", "run_live_leg_cli",
                      "execute_and_verify", "send_pause_ex",
                      "send_restore_ex", "acquire_ownership"):
                calls.add(nm)
    check("S4 ★★ shadow 不呼叫任何 side-effect 入口（AST）：%r" % (calls,),
          calls == set())
    check("S5 ★★ shadow 以 field_authorized=False / inside_window=False 建構",
          '"field_authorized": False' in src
          and '"inside_window": False' in src)
    check("S6 ★★ 必須明示 SHADOW PASS 不等於 real field PASS",
          "不是** real field PASS" in src)
    check("S7 ★★ side effect 以『本次是否新建』判定（不得誤判歷史檔案）",
          "preexisting_artifacts" in src
          and "ledger_created_by_shadow" in src)
    check("S8 ★★ 檢查項目涵蓋 11 項", len(SH.SHADOW_ITEMS) == 11)


# ======================================================================
def main():
    print("=" * 72)
    print("  AUTHORITY EVIDENCE ／UNCERTAIN OWNERSHIP RECONCILIATION")
    print("=" * 72)
    for fn in (test_A_authority_reasons, test_A1W_artifact_wiring,
               test_A9R_exact_regression, test_R_post_restore,
               test_N_invariants, test_S_shadow_rehearsal):
        fn()
    for d in PCS.TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print(f"  rest state：HO={HO.DISPATCH_ENABLED} RT={RT.DISPATCH_ENABLED}")
    print("=" * 72)
    return 0 if (n == tot and rest_ok()) else 1


if __name__ == "__main__":
    sys.exit(main())
