# -*- coding: utf-8 -*-
"""
test_phase6_attempt3_postmortem.py — Attempt #3 postmortem 回歸
======================================================================
核心命題
    「GATE A 通過**不等於** handoff pre-pause preflight 通過。
      兩者之間必須有一層**只做 mapping** 的 handoff observation adapter；
      任何對不上 canonical 來源的欄位一律 Fail Closed，
      終態必須反映真實 stage，durable pause INTENT 只能緊接在 send 之前。」

涵蓋使用者指定項目
    H1 ~ H12   handoff adapter
    L1 ~ L8    pause intent / responsibility ordering ＋ crash recovery
    E1 ~ E6    encoding
    C1 ~ C7    result classification
    N1 ~ N4    no duplicated policy（AST）

用法
    python test_phase6_attempt3_postmortem.py        # exit 0 = PASS
"""
import ast
import datetime
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_first_live_field_provider as FP        # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_scheduled_first_live as SC             # noqa: E402
import decision_policy as DP                         # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402

RESULTS = []
TMPDIRS = []
PROVIDER_SRC = os.path.join(HERE, "phase6_first_live_field_provider.py")


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p6_attempt3_")
    TMPDIRS.append(d)
    return d


# ======================================================================
# 共用：Attempt #3 現場條件重建
# ======================================================================
# 🔴 來源＝ Attempt #3 artifact 實際記錄：
#      gate_a.checks 全 True、verdict = CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED
#      fault = False、critical_alarms = 0、communication_healthy = True
#      fresh_preflight 11 項全 True（screen/process/identity alive）
#    SOC 原始值未寫入 artifact，故以 canonical 充電閂上限為界做邊界涵蓋，
#    **不猜**單一數值。
CANONICAL_SOC_MAX = DP.PolicyConfig().soc_charge_stop_pct


def decision_sample(**over):
    """`_decision_sample()` 形狀的輸入（canonical 詞彙）。"""
    s = {"fresh": True, "comm_ok": True,
         "ess_valid": True, "ess_stale": False,
         "ess_age_sec": 1.0, "ess_reason": None,
         "meter_fresh": True, "meter_kw": 0.4,
         "pcs_state": "STANDBY", "authority": "EXTERNAL_OR_UNKNOWN",
         "ac_kw": -1.3, "fault": False, "critical_alarms": 0,
         "ownership": "EXTERNAL", "soc": 2.0,
         "decision": "charge", "grid_state": "NEAR_ZERO",
         "classifier_primed": True,
         "tou_allows_charge": True, "soc_allows_charge": True,
         "safety_gate_ok": True, "tou_state": "OFF_PEAK",
         "soc_band": "S_LOW"}
    s.update(over)
    return s


def raw_sample(**over):
    """provider `sample()` 的實際形狀 —— Attempt #3 就是把這個直接餵給
    orchestrator。刻意**不含** decision / tou / soc_fresh / ess_fresh /
    external_behaviour_known。"""
    s = {"fresh": True, "comm_ok": True,
         "ess_valid": True, "ess_stale": False,
         "ess_age_sec": 1.0, "ess_reason": None,
         "meter_fresh": True, "meter_kw": 0.4,
         "pcs_state": "STANDBY", "authority": "EXTERNAL_OR_UNKNOWN",
         "ac_kw": -1.3, "fault": False, "critical_alarms": 0,
         "ownership": "EXTERNAL", "soc": 2.0}
    s.update(over)
    return s


def probe(**over):
    p = {"screen_alive": True, "process_alive": True,
         "process_identity_ok": True}
    p.update(over)
    return p


def obs(verdict=RA.EXT_RUNNING, **over):
    """經 adapter 轉出的 handoff observation（canonical 三態輸入）。"""
    return FP.build_handoff_observation(
        decision_sample(**over), external_liveness_verdict=verdict)


def gates(o, p=None, soc_max=None):
    return HO.pre_pause_gates(
        o, p if p is not None else probe(),
        CANONICAL_SOC_MAX if soc_max is None else soc_max)


# ======================================================================
# H — HANDOFF OBSERVATION ADAPTER
# ======================================================================
def test_H_adapter():
    print("\n[H] HANDOFF OBSERVATION ADAPTER")

    ok, ck = gates(obs())
    check("H1 完整 canonical observation → pre_pause_gates PASS", ok)
    check("H1b 11 項逐項皆 True",
          all(ck.values()) and set(ck) == set(HO.PRE_PAUSE_ITEMS))

    # --- 逐項 Fail Closed ---
    o = obs(ess_valid=False)
    ok2, ck2 = gates(o)
    check("H2 soc_fresh 缺（ESS 無效）→ FAIL CLOSED",
          not ok2 and ck2["soc_fresh"] is False)
    check("H3 ess_fresh 缺（ESS 無效）→ FAIL CLOSED", ck2["ess_fresh"] is False)

    o = obs(soc=None)
    ok3, ck3 = gates(o)
    check("H2b SOC 值缺失 → soc_fresh FAIL（ess_fresh 仍可 True）",
          not ok3 and ck3["soc_fresh"] is False and ck3["ess_fresh"] is True)

    o = obs(ess_stale=True)
    check("H3b ESS stale → ess_fresh / soc_fresh 皆 FAIL",
          gates(o)[1]["ess_fresh"] is False
          and gates(o)[1]["soc_fresh"] is False)

    ok4, ck4 = gates(obs(tou_state="PEAK"))
    check("H4 TOU 非 OFF_PEAK → FAIL", not ok4 and ck4["tou_off_peak"] is False)
    ok4b, ck4b = gates(obs(tou_state=None))
    check("H4b TOU 未知（None）→ FAIL CLOSED",
          not ok4b and ck4b["tou_off_peak"] is False)

    ok5, ck5 = gates(obs(decision="idle"))
    check("H5 decision != charge → FAIL",
          not ok5 and ck5["decision_is_charge"] is False)
    check("H5b decision 為 None → FAIL CLOSED",
          gates(obs(decision=None))[1]["decision_is_charge"] is False)

    ok6, ck6 = gates(obs(verdict=RA.EXT_UNKNOWN))
    check("H6 canonical liveness = EXT_UNKNOWN → FAIL",
          not ok6 and ck6["external_behaviour_known"] is False)
    check("H6b canonical verdict 取不到（None）→ FAIL CLOSED",
          gates(obs(verdict=None))[1]["external_behaviour_known"] is False)

    over_max = float(CANONICAL_SOC_MAX) + 0.1
    ok7, ck7 = gates(obs(soc=over_max))
    check("H7 SOC 超過 canonical 充電閂上限 → soc_in_charge_band FAIL",
          not ok7 and ck7["soc_in_charge_band"] is False)
    check("H7b SOC 恰等於上限 → 仍在 band 內（<= 契約，不改既有比較）",
          gates(obs(soc=float(CANONICAL_SOC_MAX)))[1][
              "soc_in_charge_band"] is True)
    check("H7c soc_max_pct 未提供 → Fail Closed（既有契約不變）",
          not HO.pre_pause_gates(obs(), probe(), None)[0])

    ok8, ck8 = gates(obs(meter_fresh=False))
    check("H8 meter stale → FAIL", not ok8 and ck8["meter_fresh"] is False)

    ok9, ck9 = gates(obs(comm_ok=False))
    check("H9 comm_ok False → FAIL", not ok9 and ck9["comm_ok"] is False)

    ok10, ck10 = gates(obs(fault=True))
    check("H10 fault → FAIL", not ok10 and ck10["fault_clear"] is False)

    ok11, ck11 = gates(obs(critical_alarms=1))
    check("H11 critical alarm → FAIL", not ok11 and ck11["alarm_clear"] is False)

    check("H11b external runtime 不健康 → FAIL",
          not gates(obs(), probe(process_alive=False))[0])

    # --- H12 Attempt #3 條件重建 ---
    print("\n  -- H12 Attempt #3 實際現場條件重建 --")
    for soc in (2.0, 50.0, float(CANONICAL_SOC_MAX)):
        o12 = obs(soc=soc)
        ok12, ck12 = gates(o12)
        check("H12 重建（OFF_PEAK / decision=charge / SOC %.1f / external "
              "running / fresh meter+HMI / no fault·alarm）→ PRE_PAUSE PASS"
              % soc, ok12)
    # 同一組條件也必須讓 GATE A 通過（證明兩個 gate 對同一現場一致）
    okA, ckA, verdict = FL.pre_handoff_policy_eligibility(obs(soc=2.0))
    check("H12b 同一組條件 → GATE A 亦 PASS", okA)
    check("H12c GATE A verdict = CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED",
          verdict == "CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED")

    # --- H12d Attempt #3 的原始失敗必須可重現，且只差 adapter ---
    raw = raw_sample()                 # 未經 adapter（= Attempt #3 的錯誤接法）
    okRaw, ckRaw = HO.pre_pause_gates(raw, probe(), None)
    bad = sorted(k for k, v in ckRaw.items() if not v)
    check("H12d 未經 adapter + soc_max_pct=None → 重現 Attempt #3 的 6 項失敗",
          not okRaw and bad == sorted(["soc_fresh", "ess_fresh", "tou_off_peak",
                                       "soc_in_charge_band",
                                       "decision_is_charge",
                                       "external_behaviour_known"]))
    check("H12e 經 adapter + canonical soc_max_pct → 同一現場改為 PASS",
          gates(obs())[0])

    # --- adapter 契約 ---
    check("H13 adapter 輸入非 dict → 骨架且 behaviour_known Fail Closed",
          FP.build_handoff_observation(None)[
              "external_behaviour_known"] is False)
    check("H13b adapter 覆蓋所有 PRE_PAUSE 需要的 sample 欄位",
          set(HO.PRE_PAUSE_ITEMS) == set(
              FP.HANDOFF_OBSERVATION_CANONICAL_SOURCES))
    check("H13c external_behaviour_known 已 CONFIGURED 且指向 canonical resolver",
          FP.EXTERNAL_BEHAVIOUR_KNOWN_STATUS == "CONFIGURED"
          and "external_liveness" in
          FP.EXTERNAL_BEHAVIOUR_KNOWN_CANONICAL_SOURCE
          and FP.HANDOFF_OBSERVATION_CANONICAL_SOURCES[
              "external_behaviour_known"]
          == FP.EXTERNAL_BEHAVIOUR_KNOWN_CANONICAL_SOURCE)
    check("H13d adapter 不改動既有 canonical 值（tou 只改名不改值）",
          obs()["tou"] == decision_sample()["tou_state"])
    check("H13e adapter 不呼叫任何 I/O（純函式，重複呼叫結果相同）",
          FP.build_handoff_observation(decision_sample(), RA.EXT_RUNNING)
          == FP.build_handoff_observation(decision_sample(), RA.EXT_RUNNING))


# ======================================================================
# soc_max_pct canonical wiring
# ======================================================================
class _FakeCfg(object):
    soc_charge_stop_pct = 77.0


class _FakePolicy(object):
    cfg = _FakeCfg()


class _FakeObserver(object):
    policy = _FakePolicy()


class _FakeArbiter(object):
    observer = _FakeObserver()


class _FakeChain(object):
    arbiter = _FakeArbiter()


class _FakeBundle(object):
    def factory(self, a, b):
        return _FakeChain()


def test_soc_max():
    print("\n[S] soc_max_pct CANONICAL WIRING")
    check("S1 canonical path 常數指向 policy.cfg.soc_charge_stop_pct",
          FP.SOC_MAX_PCT_CANONICAL_PATH.endswith(
              "arbiter.observer.policy.cfg.soc_charge_stop_pct"))
    check("S2 由 live chain 上實際 policy 取值（非硬編）",
          FP.canonical_soc_max_pct(_FakeBundle()) == 77.0)

    class NoFactory(object):
        pass

    class BrokenChain(object):
        arbiter = None

    class BrokenBundle(object):
        def factory(self, a, b):
            return BrokenChain()

    for tag, b in (("bundle 無 factory", NoFactory()),
                   ("chain 無 arbiter", BrokenBundle())):
        try:
            FP.canonical_soc_max_pct(b)
            ok = False
        except FP.ProviderWiringError:
            ok = True
        check("S3 %s → ProviderWiringError（大聲失敗，不降級成 None）" % tag, ok)

    class BadCfg(object):
        soc_charge_stop_pct = "90"

    class BadPolicy(object):
        cfg = BadCfg()

    class BadObs(object):
        policy = BadPolicy()

    class BadArb(object):
        observer = BadObs()

    class BadChain(object):
        arbiter = BadArb()

    class BadBundle(object):
        def factory(self, a, b):
            return BadChain()

    try:
        FP.canonical_soc_max_pct(BadBundle())
        ok = False
    except FP.ProviderWiringError:
        ok = True
    check("S4 門檻非數值 → ProviderWiringError", ok)
    check("S5 production 預設值與 canonical 一致（%.1f）" % CANONICAL_SOC_MAX,
          FP.canonical_soc_max_pct(
              _mk_bundle(CANONICAL_SOC_MAX)) == float(CANONICAL_SOC_MAX))


def _mk_bundle(v):
    class C(object):
        soc_charge_stop_pct = v

    class P(object):
        cfg = C()

    class O(object):
        policy = P()

    class A(object):
        observer = O()

    class Ch(object):
        arbiter = A()

    class B(object):
        def factory(self, a, b):
            return Ch()

    return B()


# ======================================================================
# C — RESULT CLASSIFICATION
# ======================================================================
def _runner(ledger_path, handoff=None, collect=None, register=None):
    auth = SC.OneShotAuthorization(
        attempt_id="T", enabled=True,
        target_date=datetime.date(2026, 9, 23),
        window_start=datetime.time(0, 5), window_end=datetime.time(0, 30),
        timezone_name="Asia/Taipei", created_at=None)
    led = SC.OneShotLedger(path=ledger_path)
    return SC.ScheduledFirstLiveRunner(
        auth=auth, ledger=led,
        now_fn=(lambda: datetime.datetime(2026, 9, 23, 0, 6)),
        source_state_check=(lambda: {}),
        safety_preflight=(lambda: (True, {})),
        gate_a=(lambda: (True, {}, "V")),
        handoff_readiness=(lambda: (True, {})),
        arm_and_run_handoff=handoff or (lambda: {}),
        collect_artifact=collect, safety_reset=(lambda: {}),
        register_pause_intent_hook=register)


def test_C_classification():
    print("\n[C] RESULT CLASSIFICATION")
    r = _runner(os.path.join(tmpdir(), "l.jsonl"))

    a3 = {"pause_send_outcome": None, "pause_attempt_count": 0,
          "handoff_abort_step": "1_PRE_PAUSE_PREFLIGHT",
          "handoff_abort_detail": "未通過：['soc_fresh']",
          "handoff_state": "ABORTED_PREFLIGHT",
          "authority_acquire_attempted": False,
          "phase6_authority_acquired": False,
          "dispatcher_attempted": False,
          "dispatcher_outcome": FL.DISPATCH_NOT_ATTEMPTED}
    res, detail = r._classify(a3)
    check("C1 Attempt #3 artifact → ABORTED_PRE_PAUSE_PREFLIGHT",
          res == SC.R_ABORTED_PRE_PAUSE)
    check("C1b 不再被歸成 AUTHORITY_ACQUIRE_FAILED",
          res != SC.R_AUTHORITY_FAILED)
    check("C1c detail 保留實際失敗項目", "soc_fresh" in str(detail))

    a = dict(a3, authority_acquire_attempted=True,
             handoff_abort_step=None, handoff_state="POST_PAUSE")
    check("C2 只有 acquire() 真的被呼叫且失敗 → AUTHORITY_ACQUIRE_FAILED",
          r._classify(a)[0] == SC.R_AUTHORITY_FAILED)

    a = dict(a3, handoff_abort_step="6_POST_PAUSE_PRECHECK")
    check("C3 6_POST_PAUSE_PRECHECK → ABORTED_POST_HANDOFF_REVALIDATION",
          r._classify(a)[0] == SC.R_ABORTED_POST_HANDOFF)

    a = dict(a3, handoff_abort_step="8_VERIFY")
    check("C4 未登記的步驟 → ABORTED_HANDOFF_STEP（誠實標示，不猜）",
          r._classify(a)[0] == SC.R_ABORTED_HANDOFF_STEP)

    a = dict(a3, pause_send_outcome="NOT_SENT")
    check("C5 pause NOT_SENT 優先於 stage → PAUSE_FAILED",
          r._classify(a)[0] == SC.R_PAUSE_FAILED)
    a = dict(a3, pause_send_outcome="UNKNOWN")
    check("C6 pause UNKNOWN → PAUSE_UNKNOWN（不得重送）",
          r._classify(a)[0] == SC.R_PAUSE_UNKNOWN)

    check("C7 ABORTED_PRE_PAUSE_PREFLIGHT 為正式登記終態",
          SC.R_ABORTED_PRE_PAUSE == "ABORTED_PRE_PAUSE_PREFLIGHT")

    # artifact 必須真的帶出 stage
    art = FL.collect_handoff_artifact(
        {"outcome": "ABORTED", "state": "ABORTED_PREFLIGHT",
         "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT", "ok": False,
                    "detail": "未通過：['soc_fresh']"}]})
    d = art.to_dict()
    check("C8 collect_handoff_artifact 帶出 handoff_abort_step",
          d["handoff_abort_step"] == "1_PRE_PAUSE_PREFLIGHT")
    check("C8b 帶出 handoff_state", d["handoff_state"] == "ABORTED_PREFLIGHT")
    check("C8c 帶出 handoff_abort_detail", "soc_fresh" in d["handoff_abort_detail"])
    check("C8d 全成功時 abort_step = None",
          FL.collect_handoff_artifact(
              {"outcome": "PASS", "steps": [{"step": "1_X", "ok": True}]}
          ).to_dict()["handoff_abort_step"] is None)

    lease = FL.Phase6AuthorityLease(acquire=(lambda: (False, "busy")),
                                    release=(lambda: True))
    check("C9 lease 未呼叫 acquire → acquire_attempted False",
          lease.acquire_attempted is False)
    lease.acquire()
    check("C9b 呼叫後 acquire_attempted True", lease.acquire_attempted is True)


# ======================================================================
# L — PAUSE INTENT / RESPONSIBILITY ORDERING ＋ CRASH RECOVERY
# ======================================================================
def test_L_ordering():
    print("\n[L] PAUSE INTENT / RESPONSIBILITY ORDERING")

    # L1 crash before pre-pause preflight
    p = os.path.join(tmpdir(), "L1.jsonl")
    led = SC.OneShotLedger(path=p)
    led.mark(SC.LC_PREFLIGHT, "fresh start")
    check("L1 preflight 前崩潰 → 無責任",
          led.restore_responsibility is False
          and led.attempted("pause") is False)

    # L2 pre-pause preflight FAIL -> no intent, no responsibility
    p2 = os.path.join(tmpdir(), "L2.jsonl")
    calls = []

    def handoff_fail():
        return {"outcome": "ABORTED", "state": "ABORTED_PREFLIGHT",
                "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT", "ok": False,
                           "detail": "未通過：['soc_fresh']"}]}

    r = _runner(p2, handoff=handoff_fail,
                collect=(lambda res: FL.collect_handoff_artifact(res).to_dict()),
                register=lambda fn: calls.append(fn))
    out = r.run()
    check("L2 pre-pause FAIL → pause INTENT 未落盤",
          r.ledger.attempted("pause") is False)
    check("L2b pre-pause FAIL → restore responsibility = False",
          r.ledger.restore_responsibility is False)
    check("L2c 終態 = ABORTED_PRE_PAUSE_PREFLIGHT",
          out.get("result") == SC.R_ABORTED_PRE_PAUSE)
    check("L2d hook 有被註冊但未被呼叫（sender 從未執行）", len(calls) == 1)

    # L3 preflight PASS -> intent -> crash before sender return
    p3 = os.path.join(tmpdir(), "L3.jsonl")
    hooks = []

    def handoff_crash():
        hooks[0]()                       # sender 前一刻落盤
        raise RuntimeError("crash before sender return")

    r3 = _runner(p3, handoff=handoff_crash,
                 register=lambda fn: hooks.append(fn))
    try:
        r3.run()
    except Exception:                                          # noqa: BLE001
        pass
    led3 = SC.OneShotLedger(path=p3)
    led3.recover() if hasattr(led3, "recover") else None
    check("L3 INTENT 落盤後崩潰 → 責任 = True",
          led3.attempted("pause") is True
          and led3.restore_responsibility is True)

    # L4 pause SENT
    p4 = os.path.join(tmpdir(), "L4.jsonl")
    led4 = SC.OneShotLedger(path=p4)
    led4.mark_intent("pause", "即將送出")
    led4.mark(SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY)
    led4.mark_outcome("pause", "SENT")
    check("L4 pause SENT → 責任 = True", led4.restore_responsibility is True)

    # L5 pause UNKNOWN -> responsibility, no resend
    p5 = os.path.join(tmpdir(), "L5.jsonl")
    led5 = SC.OneShotLedger(path=p5)
    led5.mark_intent("pause")
    led5.mark_outcome("pause", "UNKNOWN")
    check("L5 pause UNKNOWN → 責任 = True", led5.restore_responsibility is True)
    check("L5b UNKNOWN 不得產生第二筆 pause INTENT",
          sum(1 for x in led5.records
              if x.get("kind") == "INTENT" and x.get("verb") == "pause") == 1)

    # L6 SEND_NOT_SENT：INTENT 已落盤 -> 依既有契約仍保留責任
    p6 = os.path.join(tmpdir(), "L6.jsonl")
    led6 = SC.OneShotLedger(path=p6)
    led6.mark_intent("pause")
    led6.mark_outcome("pause", "NOT_SENT")
    check("L6 pause NOT_SENT（INTENT 已落盤）→ 依既有契約仍保留責任",
          led6.restore_responsibility is True)

    # L7 restore verified -> cleared
    p7 = os.path.join(tmpdir(), "L7.jsonl")
    led7 = SC.OneShotLedger(path=p7)
    led7.mark_intent("pause")
    led7.mark_outcome("pause", "SENT")
    led7.mark_intent("restore")
    led7.mark_outcome("restore_verified", "RV_RESUMED")
    check("L7 restore RV_RESUMED → 責任解除",
          led7.restore_responsibility is False)

    # L8 recovery 不得把 preflight abort 誤判為 pending restore
    p8 = os.path.join(tmpdir(), "L8.jsonl")
    led8 = SC.OneShotLedger(path=p8)
    led8.mark(SC.LC_PREFLIGHT, "fresh start")
    led8.consume(SC.R_ABORTED_PRE_PAUSE, "未通過：['soc_fresh']")
    led8b = SC.OneShotLedger(path=p8)
    verdict, action = SC.recover(led8b)
    check("L8 preflight abort 後 recover 不得回 pending restore",
          verdict != SC.REC_PENDING_RESTORE)
    check("L8a1 verdict = 已消耗（不得再執行）", verdict == SC.REC_CONSUMED)

    # L8b2：只停在 preflight、pause 從未落盤 -> CLEAN，不得 pending restore
    p8b = os.path.join(tmpdir(), "L8b.jsonl")
    l8b = SC.OneShotLedger(path=p8b)
    l8b.mark(SC.LC_PREFLIGHT, "fresh start")
    v2, _a2 = SC.recover(SC.OneShotLedger(path=p8b))
    check("L8a2 preflight + 無 pause INTENT → REC_CLEAN（非 pending restore）",
          v2 == SC.REC_CLEAN)

    # L8b3：舊寫法（intent 過早）才會被誤判成 pending restore —— 鎖住差異
    p8c = os.path.join(tmpdir(), "L8c.jsonl")
    l8c = SC.OneShotLedger(path=p8c)
    l8c.mark(SC.LC_PREFLIGHT, "fresh start")
    l8c.mark_intent("pause", "OLD：進入 handoff 前就落盤")
    v3, _a3 = SC.recover(SC.OneShotLedger(path=p8c))
    check("L8a3 舊 ordering（preflight 即有 pause INTENT）確實會回 "
          "PENDING_RESTORE → 證明 ordering 修正的必要性",
          v3 == SC.REC_PENDING_RESTORE)
    check("L8b 且 restore_responsibility = False",
          led8b.restore_responsibility is False)
    check("L8c one-shot 已 consumed", led8b.consumed is True)


# ======================================================================
# 11. LEDGER / ARTIFACT FINAL STATE CONSISTENCY
# ======================================================================
def test_consistency():
    print("\n[K] LEDGER / ARTIFACT 終態一致性")
    p = os.path.join(tmpdir(), "K.jsonl")

    def handoff_fail():
        return {"outcome": "ABORTED", "state": "ABORTED_PREFLIGHT",
                "restore_responsibility": False,
                "steps": [{"step": "1_PRE_PAUSE_PREFLIGHT", "ok": False,
                           "detail": "未通過：['external_behaviour_known']"}]}

    r = _runner(p, handoff=handoff_fail,
                collect=(lambda res: FL.collect_handoff_artifact(res).to_dict()),
                register=lambda fn: None)
    ev = r.run()
    art = ev["handoff_artifact"]
    led = ev["ledger"]
    check("K1 terminal result 一致",
          ev["result"] == SC.R_ABORTED_PRE_PAUSE)
    check("K2 restore responsibility 一致（artifact False / ledger False）",
          art["restore_responsibility_final"] is False
          and led["restore_responsibility"] is False)
    check("K3 pause 計數一致（artifact 0 / ledger 未 attempted）",
          art["pause_attempt_count"] == 0
          and led["pause_attempted"] is False)
    check("K4 stage 一致",
          art["handoff_abort_step"] == "1_PRE_PAUSE_PREFLIGHT")
    check("K5 one-shot consumed", ev["one_shot_consumed"] is True)


# ======================================================================
# E — ENCODING
# ======================================================================
def test_E_encoding():
    print("\n[E] ENCODING")
    d = tmpdir()
    zh = "未通過：['soc_fresh', 'ess_fresh']　尚未開始 -> 可在 window 內執行一次"

    p = os.path.join(d, "a.json")
    with io.open(p, "w", encoding="utf-8") as fh:
        json.dump({"detail": zh, "ascii": "OFF_PEAK"}, fh,
                  ensure_ascii=False, indent=2)
    back = json.load(io.open(p, encoding="utf-8"))
    check("E1 中文 detail round-trip UTF-8 完全相同", back["detail"] == zh)
    check("E2 ASCII 不變", back["ascii"] == "OFF_PEAK")

    raw = io.open(p, "rb").read()
    check("E3 檔案位元組為合法 UTF-8（非 cp950）",
          raw.decode("utf-8") is not None)
    try:
        raw.decode("cp950")
        cp_ok = False
    except UnicodeDecodeError:
        cp_ok = True
    check("E3b 以 cp950 解讀會失敗 → 證明寫出的是 UTF-8", cp_ok)

    # E4 cp950 console 環境仍要產生合法 UTF-8 artifact
    lp = os.path.join(d, "L.jsonl")
    led = SC.OneShotLedger(path=lp)
    led.mark_intent("pause", zh)
    led.consume(SC.R_ABORTED_PRE_PAUSE, zh)
    lines = io.open(lp, encoding="utf-8").read().strip().split("\n")
    recs = [json.loads(x) for x in lines]
    check("E4 ledger 在任何 console 編碼下都寫成 UTF-8（檔案層獨立於 stdout）",
          recs[0]["detail"] == zh)
    check("E5 ledger JSONL 中文可正常 parse/read", recs[1]["result"]
          == SC.R_ABORTED_PRE_PAUSE and recs[1]["detail"] == zh)

    # E6 artifact 與 ledger 同一語意字串必須一致
    ap = os.path.join(d, "art.json")
    with io.open(ap, "w", encoding="utf-8") as fh:
        json.dump({"handoff_abort_detail": zh}, fh, ensure_ascii=False)
    check("E6 artifact / ledger same semantic string 完全一致",
          json.load(io.open(ap, encoding="utf-8"))["handoff_abort_detail"]
          == recs[0]["detail"])

    # E3c 子行程 stdout 以 UTF-8 產出 evidence
    import subprocess
    sp = os.path.join(d, "emit.py")
    io.open(sp, "w", encoding="utf-8").write(
        "\n".join(["# -*- coding: utf-8 -*-",
                   "import json, sys",
                   "sys.stdout.reconfigure(encoding='utf-8')",
                   "print(json.dumps({'detail': %r}, ensure_ascii=False))"
                   % zh]))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, sp], capture_output=True, env=env)
    check("E3c subprocess stdout 為合法 UTF-8 且中文完整",
          json.loads(out.stdout.decode("utf-8"))["detail"] == zh)

    # 不得用 errors="ignore" 掩蓋
    src = io.open(os.path.join(HERE, "run_scheduled_first_live.py"),
                  encoding="utf-8").read()
    src2 = io.open(os.path.join(HERE, "phase6_scheduled_first_live.py"),
                   encoding="utf-8").read()
    check("E7 evidence 寫出路徑未使用 errors='ignore' 掩蓋編碼問題",
          'errors="ignore"' not in src and 'errors="ignore"' not in src2)
    check("E8 evidence 寫出一律顯式 encoding='utf-8'",
          src.count('encoding="utf-8"') >= 1
          and 'open(path, "w", encoding="utf-8")' in src)


# ======================================================================
# N — NO DUPLICATED POLICY（AST）
# ======================================================================
def _provider_tree():
    return ast.parse(io.open(PROVIDER_SRC, encoding="utf-8").read())


def _docstring_ids(tree):
    keep = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(
                        body[0].value.value, str):
                keep.add(id(body[0].value))
    return keep


def test_N_no_duplicated_policy():
    print("\n[N] NO DUPLICATED POLICY（AST）")
    tree = _provider_tree()
    docs = _docstring_ids(tree)

    nums = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    banned = {90.0, 20.0, 80.0, 95.0, 5.0}
    hit = sorted(set(nums) & banned - {5.0})
    check("N1 provider 未硬編 SOC 門檻常數（90.0 / 20.0 …）", not hit)

    strs = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]
    # OFF_PEAK 只允許出現在 canonical 來源說明字串中，不得作為判定字面值
    cmp_literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for c in node.comparators:
                if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                        and c.value in ("OFF_PEAK", "PEAK", "HALF_PEAK",
                                        "charge", "discharge",
                                        "S_LOW", "S_MID", "S_HIGH"):
                    cmp_literals.append(c.value)
    check("N2 provider 未以 TOU / decision / SOC band 字面值做比較判定",
          not cmp_literals)

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
    check("N3 provider 未直接引用 DECISION_MATRIX（SOC 判定一律走既有 FL 函式）",
          "DECISION_MATRIX" not in names)
    assigns = [t.id for node in ast.walk(tree)
               if isinstance(node, ast.Assign)
               for t in node.targets if isinstance(t, ast.Name)]
    check("N3b provider 未定義自己的 DECISION_MATRIX / POLICY_CONFIG",
          "DECISION_MATRIX" not in assigns
          and "PolicyConfig" not in assigns
          and "SOC_CHARGE_STOP_PCT" not in assigns)
    check("N4 canonical 來源表每一項都有 production 路徑或明確 UNCONFIGURED",
          all(v is None or isinstance(v, str)
              for v in FP.HANDOFF_OBSERVATION_CANONICAL_SOURCES.values()))
    check("N4b build_handoff_observation 內無任何數值常數（純 mapping）",
          not [n for fn in ast.walk(tree)
               if isinstance(fn, ast.FunctionDef)
               and fn.name == "build_handoff_observation"
               for n in ast.walk(fn)
               if isinstance(n, ast.Constant)
               and isinstance(n.value, (int, float))
               and not isinstance(n.value, bool)])




# ======================================================================
# EB — EXTERNAL_BEHAVIOUR_KNOWN（canonical liveness mapping）
# ======================================================================
def _probe_dict(**over):
    p = {"ssh_ok": True, "screen_alive": True, "process_alive": True,
         "process_identity_ok": True, "pid": "57483"}
    p.update(over)
    return p


MIN_OBS = HO.HandoffConfig.restore_loop_min_observations


def _liveness(probe_over=None, marks=None):
    """一律走 canonical resolver，測試自己**不**判定。"""
    return RA.external_liveness(
        _probe_dict(**(probe_over or {})), loop_marks=marks,
        min_loop_observations=MIN_OBS)


def test_EB_external_behaviour_known():
    print("\n[EB] EXTERNAL_BEHAVIOUR_KNOWN")
    check("EB0 canonical resolver 為既有 symbol（未新增第二套）",
          callable(RA.external_liveness)
          and RA.EXTERNAL_LIVENESS_VERDICTS
          == (RA.EXT_RUNNING, RA.EXT_NOT_RUNNING, RA.EXT_UNKNOWN))
    check("EB0b min_loop_observations 沿用既有 HandoffConfig（未新增常數）",
          FP.EXTERNAL_LIVENESS_MIN_OBS_SOURCE.endswith(
              "restore_loop_min_observations") and MIN_OBS >= 2)

    adv = ["2026-09-23 00:05:20", "2026-09-23 00:05:23",
           "2026-09-23 00:05:26"]
    still = ["2026-09-23 00:05:20"] * MIN_OBS

    v, why = _liveness(marks=adv)
    check("EB1 loop marks 前進 → canonical RUNNING", v == RA.EXT_RUNNING)
    check("EB1b RUNNING → known = True",
          FP.external_behaviour_known_from(v) is True)

    v2, _ = _liveness(marks=still)
    check("EB2 runtime 健康但 loop 未前進 → canonical NOT_RUNNING",
          v2 == RA.EXT_NOT_RUNNING)
    check("EB2b NOT_RUNNING → known = True",
          FP.external_behaviour_known_from(v2) is True)

    check("EB3 EXT_UNKNOWN → known = False",
          FP.external_behaviour_known_from(RA.EXT_UNKNOWN) is False)

    v4, _ = _liveness(marks=adv[:1])
    check("EB4 loop evidence 不足 → canonical UNKNOWN", v4 == RA.EXT_UNKNOWN)
    check("EB4b → known = False",
          FP.external_behaviour_known_from(v4) is False)
    v4b, _ = _liveness(marks=None)
    check("EB4c 完全沒有 loop evidence → UNKNOWN → known False",
          v4b == RA.EXT_UNKNOWN
          and FP.external_behaviour_known_from(v4b) is False)

    v5, _ = _liveness(probe_over={"process_identity_ok": None}, marks=adv)
    check("EB5 probe 必要事實不完整 → canonical UNKNOWN", v5 == RA.EXT_UNKNOWN)
    check("EB5b → known = False",
          FP.external_behaviour_known_from(v5) is False)
    check("EB5c probe 完全取不到（None）→ UNKNOWN → known False",
          RA.external_liveness(None, loop_marks=adv,
                               min_loop_observations=MIN_OBS)[0]
          == RA.EXT_UNKNOWN
          and FP.external_behaviour_known_from(
              RA.external_liveness(None)[0]) is False)

    # EB6 runtime 不健康 -> behaviour 可 KNOWN，但整體仍 FAIL
    v6, _ = _liveness(probe_over={"process_alive": False}, marks=adv)
    check("EB6 runtime 不健康 → canonical NOT_RUNNING（實證的否定）",
          v6 == RA.EXT_NOT_RUNNING)
    check("EB6b 此時 behaviour_known 仍為 True（KNOWN != HEALTHY）",
          FP.external_behaviour_known_from(v6) is True)
    ok6, ck6 = HO.pre_pause_gates(obs(verdict=v6),
                                  probe(process_alive=False),
                                  CANONICAL_SOC_MAX)
    check("EB6c ★★ external_runtime_healthy = False → 整體 pre-pause FAIL",
          not ok6 and ck6["external_runtime_healthy"] is False
          and ck6["external_behaviour_known"] is True)
    check("EB6d 兩者為獨立欄位，未合併",
          "external_runtime_healthy" in HO.PRE_PAUSE_ITEMS
          and "external_behaviour_known" in HO.PRE_PAUSE_ITEMS)

    # EB7 Trial #2 Active Gate 不參與 mapping
    src = io.open(
        os.path.join(HERE, "phase6_first_live_field_provider.py"),
        encoding="utf-8").read()
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("EB7 provider 未引用 active_gate / ActiveBaselineObserver",
          not ({"active_gate", "ActiveBaselineObserver"} & (names | attrs)))
    check("EB7b provider 未 import phase6_trial2_harness",
          "phase6_trial2_harness" not in src)

    # EB8 PCS CHARGING 但 canonical UNKNOWN -> known False
    v8, _ = _liveness(marks=None)
    ok8, ck8 = HO.pre_pause_gates(
        obs(verdict=v8, pcs_state="CHARGING", ac_kw=-80.0),
        probe(), CANONICAL_SOC_MAX)
    check("EB8 PCS CHARGING 但 canonical UNKNOWN → known False（不以 PCS 代理）",
          ck8["external_behaviour_known"] is False and not ok8)

    # EB9 PCS STOPPED 但 canonical RUNNING -> known True
    v9, _ = _liveness(marks=adv)
    ok9, ck9 = HO.pre_pause_gates(
        obs(verdict=v9, pcs_state="STOPPED", ac_kw=0.0),
        probe(), CANONICAL_SOC_MAX)
    check("EB9 PCS STOPPED 但 canonical RUNNING → known True",
          ck9["external_behaviour_known"] is True and ok9)

    # EB10 Attempt #3 現場證據重建
    print("\n  -- EB10 Attempt #3 reconstructed --")
    vA, whyA = _liveness(marks=adv)
    check("EB10 Attempt #3 現場（process healthy / identity OK / "
          "loop marks advancing）→ canonical RUNNING", vA == RA.EXT_RUNNING)
    okA, ckA = HO.pre_pause_gates(obs(verdict=vA), probe(), CANONICAL_SOC_MAX)
    check("EB10b external_behaviour_known = True",
          ckA["external_behaviour_known"] is True)
    check("★★ EB10c Attempt #3 reconstructed → PRE_PAUSE_PREFLIGHT 11 / 11 PASS",
          okA and sum(1 for v in ckA.values() if v) == 11
          and len(ckA) == 11)
    for k in HO.PRE_PAUSE_ITEMS:
        check("       %s = True" % k, ckA[k] is True)


# ======================================================================
# EBN — NO NEW POLICY IN ADAPTER（AST）
# ======================================================================
def test_EBN_no_new_policy():
    print("\n[EBN] ADAPTER 不得新增 policy")
    tree = ast.parse(io.open(
        os.path.join(HERE, "phase6_first_live_field_provider.py"),
        encoding="utf-8").read())
    fns = {f.name: f for f in ast.walk(tree)
           if isinstance(f, ast.FunctionDef)}

    f = fns["external_behaviour_known_from"]
    consts = [n.value for n in ast.walk(f)
              if isinstance(n, ast.Constant)
              and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)]
    check("EBN1 mapping 函式內無任何數值常數（無 threshold / timeout / count）",
          not consts)
    attrs = {n.attr for n in ast.walk(f) if isinstance(n, ast.Attribute)}
    check("EBN2 mapping 只引用 canonical enum（EXT_RUNNING / EXT_NOT_RUNNING）",
          attrs == {"EXT_RUNNING", "EXT_NOT_RUNNING"})
    strs = [n.value for n in ast.walk(f)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    body_strs = [x for x in strs if "\n" not in x]
    check("EBN3 mapping 未硬編 PCS / Authority / power 字面值",
          not [x for x in body_strs
               if x in ("CHARGING", "DISCHARGING", "STANDBY", "STOPPED",
                        "IDLE", "EXTERNAL_OR_UNKNOWN")])

    g = fns["_external_liveness"] if "_external_liveness" in fns else None
    check("EBN4 provider 具備 _external_liveness（委派既有 resolver）",
          g is not None)
    if g is not None:
        calls = {n.func.attr for n in ast.walk(g)
                 if isinstance(n, ast.Call) and isinstance(n.func,
                                                           ast.Attribute)}
        check("EBN5 _external_liveness 呼叫 RA.external_liveness（不自行判定）",
              "external_liveness" in calls)
        nums = [n.value for n in ast.walk(g)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, (int, float))
                and not isinstance(n.value, bool)]
        check("EBN6 _external_liveness 內無數值常數"
              "（mark age / count / threshold 一律沿用 canonical）", not nums)
        attrs2 = {n.attr for n in ast.walk(g) if isinstance(n, ast.Attribute)}
        check("EBN7 觀測數取自既有 HandoffConfig.restore_loop_min_observations",
              "restore_loop_min_observations" in attrs2)


# ======================================================================
def main():
    print("=" * 72)
    print("  Attempt #3 POSTMORTEM 回歸")
    print("=" * 72)
    for fn in (test_H_adapter, test_soc_max, test_EB_external_behaviour_known,
               test_EBN_no_new_policy, test_C_classification,
               test_L_ordering, test_consistency, test_E_encoding,
               test_N_no_duplicated_policy):
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
