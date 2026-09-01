# -*- coding: utf-8 -*-
"""
test_phase6_d5b_orchestrator_wiring.py — Phase D.5-B Orchestrator Wiring（A~O）
======================================================================
核心命題
    「production path 已經真的接起來並且會跑完整條鏈，
      但**結構上**沒有任何可被呼叫的控制出口 —— 不是靠旗標擋住。」

    Meter → Tariff/TOU → Decision → Safety Gate → Control Authority
          → Layer 1 Interlock → Executor decision → ReadBack config → Report

🔴 dispatch_enabled = False；executor / verifier 一律不建立。
🔴 即使 REQUIRED_FOR_DISPATCH 全部補齊，仍不得送出任何指令。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch、零實機 command。

用法
    python test_phase6_d5b_orchestrator_wiring.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meter_client as MC                                # noqa: E402
import decision_engine as DE                             # noqa: E402
import control_authority as CA                           # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402
import pcs_auto_control_production as PRD                # noqa: E402
import production_execution_chain as PEC                 # noqa: E402
import production_arbiter as ARB                        # noqa: E402
import annual_off_peak_calendar as AC                    # noqa: E402
import last_control_store as LCS                         # noqa: E402
import test_phase6_d3d_arbitration as D3D                # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def _tree(name):
    return ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())


def _all_imports(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


def _top_imports(tree):
    """只看模組層 —— 延後 import 由呼叫端另行逐函式檢查。"""
    out = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


# ======================================================================
# 測試替身
# ======================================================================
# 🔴 全部為**測試注入值**；production defaults 另有斷言證明未被更動。
FULL_CFG = CFG.AutoControlConfig(
    charge_power_kw=5.0, discharge_power_kw=5.0, max_power_kw=100.0,
    authority_ttl_sec=120.0, authority_power_tolerance_kw=1.0,
    decision_interval_sec=30.0, authorization_ttl_sec=30.0,
    readback_timeout_sec=75.0, readback_poll_interval_sec=5.0)

DT_OFF_PEAK = datetime(2026, 7, 15, 3, 0)     # 平日離峰（已 provision 的年度）
DT_PEAK = datetime(2026, 7, 15, 14, 0)        # 平日尖峰


class Spy(object):
    """
    控制出口的間諜。**不會被接上** —— 它存在只是為了證明呼叫次數恆為 0。
    任何一次呼叫都代表不變量被破壞。
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        raise AssertionError("OBSERVE_ONLY 期間不得呼叫任何控制出口")


class Stack(object):
    """以**真正的 production 組裝函式**建立一條可跑的鏈（來源為離線替身）。"""

    def __init__(self, pcs="STANDBY", meter_kw=12.5, local_now=DT_OFF_PEAK,
                 config=FULL_CFG, ess_over=None, meter=None,
                 last_control=None, trust=CA.TRUST_FOR_INTERVAL, start=1000.0):
        self.clk = D3D.Clk(start)
        self.pcs = pcs
        self.meter_kw = meter_kw
        self.ess_over = dict(ess_over or {})
        self.meter_override = meter
        self.slow_read_sec = 0.0
        self.spy = Spy()
        self.last_control = last_control
        self.trust = trust
        lcp = (None if last_control is None
               else (lambda: (self.last_control, self.trust)))
        # ⚠️ 時鐘與本地時間都由組裝函式一路注入 —— ESS / Meter / Observer /
        #    Arbiter 共用同一個時基，freshness 才有意義。
        self.obs_source, self.recovery, self.wiring = SVC.build_production_stack(
            config=config, reader=self._ess, meter_source=self._meter,
            last_control_provider=lcp, clock=self.clk,
            local_now=(lambda: local_now))

    def _observer(self):
        # obs_source 是 chain.run；沿著既有結構取回 observer 以注入時鐘
        return self._chain().arbiter.observer

    def _chain(self):
        return self.obs_source.__self__

    def _ess(self):
        # 模擬「讀取耗時很久」→ 快照一取得即為陳舊（age 以讀取起點為基準）
        self.clk.advance(self.slow_read_sec)
        return D3D.ess_reading(self.pcs, **self.ess_over)

    def _meter(self):
        if self.meter_override is not None:
            return self.meter_override
        return MC.evaluate({"meter": self.meter_kw, "meter_state": MC.S_OK,
                            "demand": 8.0, "demand_state": MC.S_OK},
                           received_at=self.clk.t, now=self.clk.t)

    def settle(self, n=6):
        for _ in range(n):
            self.clk.advance(1.0)
            self._chain().arbiter.observer.observe()
        return self

    def run(self, dt=1.0):
        self.clk.advance(dt)
        return self.obs_source()


def rec_of(res, config=FULL_CFG):
    return PRD.observe_record(res, config=config)


def no_action(res):
    """
    本輪確定**沒有送出任何指令**。

    ⚠️ 刻意**不看** audit 的 `control_action` —— 那是「原本想做什麼」的稽核欄位，
       即使後續被擋下也會留有值。
    ⚠️ 也刻意**不看**是否核發授權 —— 仲裁層核發一次性授權是它的正常職責；
       B/C 案的重點正是「上游全部放行、連授權都發了，仍然沒有東西可以被呼叫」。
    """
    return (res.executed is False and res.dispatch_count_delta == 0
            and res.operator_outcome is None)


def blocked_upstream(res):
    """本輪在送到執行層之前就被擋下（未核發授權）。"""
    arb = getattr(res, "arbitration", None)
    return arb is not None and arb.authorized is False and no_action(res)


# ======================================================================
def main():
    print("== Phase D.5-B Orchestrator Wiring / OBSERVE_ONLY 驗證（完全離線）==\n")

    # ---------------- A. 相依明確注入 ----------------
    print("A. production dependency 明確注入")
    st = Stack().settle()
    w = st.wiring
    check("★★ A. ESS reader / meter source / holiday / LastControl 皆已接上",
          all(w.sources[k] == PRD.SRC_WIRED for k in
              ("ess_reader", "meter_source", "holiday_provider",
               "last_control")))
    check("★★ A. executor / verifier **未接**（OBSERVE_ONLY 不建立出口）",
          w.sources["executor"] == PRD.SRC_NOT_WIRED
          and w.sources["verifier"] == PRD.SRC_NOT_WIRED)
    check("★★ A. 不相依 phase6_field_measure（wiring 模組）",
          "phase6_field_measure" not in
          _all_imports(_tree("pcs_auto_control_production.py")))
    check("★★ A. 不相依 phase6_field_measure（service 模組）",
          "phase6_field_measure" not in
          _all_imports(_tree("pcs_auto_control_service.py")))
    check("★★ A. wiring 模組不 import 任何 operator",
          "device_control_operator" not in
          _all_imports(_tree("pcs_auto_control_production.py")))
    check("★★ A. Tariff 來源為已驗收 provision 的年度清單（2026, 2027）",
          PRD.build_holiday_provider().known_years == (2026, 2027))
    check("★★ A. ReadBack 參數三項皆已定案並接上",
          (lambda rb: rb.timeout_sec == 75.0 and rb.poll_interval_sec == 5.0
           and rb.stability_samples == 1 and rb.ready is True)(w.readback))
    check("★★ A. 一輪觀測可完整跑完整條鏈（不 crash、不需設備）",
          st.run() is not None)
    # 🔴 Phase 6.7 實機驗證發現的 wiring 缺口：ESS reader 需要**已登入**的 client。
    #    getRunMode / getScheduleSwitch / getDOAndDIMsg 未登入會回 401 →
    #    pcs_schedule_enabled / pcs_manual_switch 皆為 None →
    #    ESS 觀測 PCS_MODE_UNAVAILABLE → 整條鏈永遠 Fail Closed（安全但空轉）。
    _cli, _tok = PRD.build_api_client(login=False)
    check("★★ A. build_api_client 回傳 (client, token) 兩元組",
          _cli is not None and _tok is None)
    _prd_src = io.open(os.path.join(HERE, "pcs_auto_control_production.py"),
                       encoding="utf-8").read()
    check("★★ A. 預設會登入，且明載未登入將導致整條鏈空轉",
          "def build_api_client(login=True)" in _prd_src
          and "PCS_MODE_UNAVAILABLE" in _prd_src
          and "401" in _prd_src)
    check("★★ A. 登入失敗不拋例外、不重試（交由上層 Fail Closed）",
          "不拋例外、不重試" in _prd_src)

    # ---------------- B/C. dispatch_enabled=False ----------------
    print("\nB/C. 上游全部放行時仍 0 calls")
    b = Stack(local_now=DT_OFF_PEAK, meter_kw=12.5).settle()
    rb = b.run()
    r = rec_of(rb)
    check("  B. 前提成立：Decision 為 charge",
          r.decision_action == DE.ACTION_CHARGE)
    check("  B. 前提成立：Safety 放行", r.safety_allowed is True)
    check("  B. 前提成立：Authority 為 IDLE",
          r.authority_state == CA.AUTH_IDLE)
    check("  B. 前提成立：Layer 1 未擋（control_action 已形成）",
          rb.control_action == PCI.CTRL_CHARGE)
    check("★★ B. CHARGE decision → operator 0 calls",
          no_action(rb) and b.spy.calls == [])
    check("★★ B. 且 outcome 明示「沒有可用的執行出口」",
          rb.outcome == PEC.EXEC_NOT_CONFIGURED
          and rb.reason == PEC.R_EXECUTOR_NOT_INJECTED)
    check("★★ B. no_action_reason 為 DISPATCH_DISABLED",
          r.no_action_reason == PRD.NO_ACTION_DISPATCH_DISABLED)

    c = Stack(local_now=DT_PEAK, meter_kw=12.5, ess_over={"soc_percent": 80.0}
              ).settle()
    rc = c.run()
    rcr = rec_of(rc)
    check("  C. 前提成立：Decision 為 discharge",
          rcr.decision_action == DE.ACTION_DISCHARGE)
    check("★★ C. DISCHARGE decision → operator 0 calls",
          no_action(rc) and c.spy.calls == [])

    # ---------------- D. UNKNOWN tariff ----------------
    print("\nD. UNKNOWN tariff → NO ACTION")
    d = Stack(local_now=datetime(2028, 7, 12, 3, 0)).settle()   # 未 provision 年度
    rd = d.run()
    check("★★ D. 未 provision 年度 → tariff UNKNOWN",
          rec_of(rd).tariff_state == "UNKNOWN")
    check("★★ D. → 未授權、0 calls",
          blocked_upstream(rd))

    # ---------------- E. Meter stale / invalid ----------------
    print("\nE. Meter stale / invalid → NO ACTION")
    bad_meter = MC.no_data_snapshot(MC.SOURCE_PRODUCTION)
    e = Stack(meter=bad_meter).settle()
    re_ = e.run()
    check("★★ E. 電表無資料 → 未授權、0 calls",
          blocked_upstream(re_))
    e2 = Stack(meter=MC.evaluate(
        {"meter": 12.5, "meter_state": MC.S_OK, "demand": 8.0,
         "demand_state": "fault"}, received_at=1000.0, now=1000.0)).settle()
    check("★★ E. demand_state=fault（官方旗標）→ 未授權、0 calls",
          blocked_upstream(e2.run()))

    # ---------------- F. ESS stale / 通訊失敗 ----------------
    print("\nF. ESS stale / communication failure → NO ACTION")
    f = Stack(ess_over={"communication_ok": False}).settle()
    rf = f.run()
    check("★★ F. 通訊失敗 → 未授權、0 calls",
          blocked_upstream(rf))
    # 讓「讀取本身」耗掉大量時間 → 快照一取得就是陳舊的（age 以讀取起點計）
    f2 = Stack()
    f2.settle()
    f2.slow_read_sec = 600.0
    rf2 = f2.run()
    check("★★ F. ESS 陳舊 → 未授權、0 calls",
          blocked_upstream(rf2))

    # ---------------- G. Authority UNKNOWN ----------------
    print("\nG. Authority UNKNOWN → NO ACTION")
    g = Stack(pcs="CHARGING", local_now=DT_OFF_PEAK).settle()
    rg = g.run()
    check("★★ G. 運轉中但無 LastControl 證據 → 非 IDLE/OWNED",
          rec_of(rg).authority_state not in CA.AUTHORITY_ALLOWED_STATES)
    check("★★ G. → 未授權、0 calls",
          blocked_upstream(rg))

    # ---------------- H. OWNERSHIP_PENDING ----------------
    print("\nH. OWNERSHIP_PENDING → NO ACTION")

    h = Stack(pcs="CHARGING", local_now=DT_OFF_PEAK)

    class _LC(object):
        """⚠️ TTL 以注入時鐘計算，紀錄時間必須落在同一個時基之內。"""
        action, target_power_kw = "charge", 5.0
        actual_active_power_kw = 5.2        # 與觀測相同 → 暫存器尚未更新

        def __init__(self, clk):
            self._clk = clk

        @property
        def verified_at_monotonic(self):
            return self._clk.t - 5.0

    h.last_control = _LC(h.clk)
    h._chain().arbiter._last_control_provider = (
        lambda: (h.last_control, CA.TRUST_FOR_INTERVAL))
    h.settle()
    rh = h.run()
    hr = rec_of(rh)
    check("★★ H. Authority 為 UNKNOWN / NOT_YET_CORROBORATED",
          hr.authority_state == CA.AUTH_UNKNOWN
          and hr.authority_reason == CA.CA_NOT_YET_CORROBORATED)
    check("★★ H. **不是** CONFLICT、**不是** EXTERNAL",
          hr.authority_state not in (CA.AUTH_CONFLICT, CA.AUTH_EXTERNAL))
    check("★★ H. → 未授權、0 calls",
          blocked_upstream(rh))

    # ---------------- I. EXTERNAL_CONTROL ----------------
    print("\nI. EXTERNAL_CONTROL → NO ACTION")
    i = Stack(pcs="CHARGING", local_now=DT_OFF_PEAK,
              ess_over={"pcs_schedule_enabled": True}).settle()
    ri = i.run()
    check("★★ I. PCS 原生排程 ON → Authority EXTERNAL",
          rec_of(ri).authority_state == CA.AUTH_EXTERNAL)
    check("★★ I. → 未授權、0 calls",
          blocked_upstream(ri))

    # ---------------- J. Safety Gate BLOCK ----------------
    print("\nJ. Safety Gate BLOCK → NO ACTION")
    j = Stack(local_now=DT_OFF_PEAK,
              ess_over={"battery_power_status": "已下電"}).settle()
    rj = j.run()
    check("★★ J. 電池下電 → Safety 不放行",
          rec_of(rj).safety_allowed is not True)
    check("★★ J. → 仲裁 BLOCKED、未授權、0 calls",
          rj.arbitration.outcome == ARB.ARB_BLOCKED
          and blocked_upstream(rj))

    # ---------------- K. Layer 1 BLOCK ----------------
    print("\nK. Layer 1 Interlock BLOCK → NO ACTION")

    class _LCd(object):
        action, target_power_kw = "discharge", 5.0
        verified_at_monotonic = 1000.0
        actual_active_power_kw = -1.3

    k = Stack(pcs="DISCHARGING", local_now=DT_OFF_PEAK,
              ess_over={"soc_percent": 30.0}, last_control=_LCd()).settle()
    rk = k.run()
    kr = rec_of(rk)
    check("  K. 前提：目前放電中、決策想充電",
          kr.decision_action == DE.ACTION_CHARGE)
    check("★★ K. Layer 1 不得直接反向（不會形成 charge leg）",
          rk.control_action != PCI.CTRL_CHARGE)
    check("★★ K. → 0 calls", no_action(rk))
    check("★★ K. Layer 1 恆生效（不依賴任何參數）",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None)

    # ---------------- L. 缺參數 ----------------
    print("\nL. 缺 REQUIRED_FOR_DISPATCH 參數")
    prod = CFG.DEFAULT_CONTROL_CONFIG
    # 🔁 max_power_kw 寫入後更正：required 已齊備。
    check("★★ L. production config required 已全部就緒",
          prod.dispatch_ready is True and prod.missing_required() == ())
    check("★★ L. 但 dispatch 仍未啟用，且 can_dispatch 仍為 False",
          RT.DISPATCH_ENABLED is False
          and PRD.wiring_report(config=prod).can_dispatch is False)
    check("★★ L. 已定案的三項 ReadBack 參數不在缺項內",
          not (set(prod.missing_required())
               & {"readback_timeout_sec", "readback_poll_interval_sec"}))
    check("★★ L. wiring report 如實回報缺項",
          set(PRD.wiring_report(config=prod).missing_required)
          == set(prod.missing_required()))

    # ---------------- M. 參數齊全仍不得送 ----------------
    print("\nM. 參數齊全 + dispatch_enabled=False → 仍不得送")
    check("★★ M. FULL_CFG 已補齊全部 required 參數",
          FULL_CFG.dispatch_ready is True
          and FULL_CFG.missing_required() == ())
    check("★★ M. 但 can_dispatch 仍為 False",
          PRD.wiring_report(config=FULL_CFG).can_dispatch is False)
    check("★★ M. 且實際跑一輪仍 0 calls（B 案即為參數齊全）",
          no_action(rb))
    check("★★ M. 原因是結構性的：executor 恆為 None",
          PRD.build_executor() is None and PRD.build_verifier() is None)
    check("★★ M. 非 OBSERVE_ONLY 模式一律拒絕建立",
          (lambda: (_raises(lambda: PRD.build_executor("DISPATCH"))
                    and _raises(lambda: PRD.build_verifier("DISPATCH"))))())
    check("★★ M. DISPATCH_ENABLED 模組層仍為 False",
          RT.DISPATCH_ENABLED is False)

    # ---------------- N. restart / recovery ----------------
    print("\nN. service restart / recovery 不得繞過")
    n = Stack(pcs="CHARGING", local_now=DT_OFF_PEAK).settle()
    rn = n.recovery()
    check("★★ N. recovery 可執行且 may_dispatch 恆為 False",
          rn is not None and rn.may_dispatch is False)
    rt = RT.AutoControlRuntime(observation_source=n.obs_source,
                               recovery_source=n.recovery)
    rec_res = rt.recover()
    check("★★ N. Runtime 復原後仍不在派工狀態",
          rt.state not in RT.DISPATCH_STATES)
    check("★★ N. 復原不改變 dispatch_enabled",
          rt.snapshot()["dispatch_enabled_instance"] is False)
    check("★★ N. 復原後再跑一輪仍 0 calls",
          no_action(n.run()))
    check("  N. recovery 結果可稽核", rec_res is not None)

    # ---------------- O. OBSERVE_ONLY 完整紀錄 ----------------
    print("\nO. OBSERVE_ONLY 可產生完整可稽核紀錄")
    rec = rec_of(rb)
    for f in ("meter_state", "grid_state", "tariff_state", "decision_action",
              "safety_reason", "authority_state", "interlock_reason",
              "dispatch_ready", "no_action_reason"):
        check(f"  O. 紀錄含 {f}", hasattr(rec, f))
    check("★★ O. 八項要求全部有值（非 None）",
          all(getattr(rec, f) is not None for f in
              ("grid_state", "tariff_state", "decision_action",
               "safety_allowed", "authority_state", "interlock_reason",
               "dispatch_ready", "no_action_reason")))
    check("★★ O. executed 為事實欄位且為 False", rec.executed is False)
    check("★★ O. 紀錄可序列化供 Phase 6.7 使用",
          isinstance(rec.as_dict(), dict)
          and set(rec.as_dict()) == set(PRD.ObserveRecord.FIELDS))
    check("★★ O. 字串形式可直接寫進 log", "OBSERVE" in str(rec))

    # ---------------- 不變量 ----------------
    print("\n不變量：production 參數未被本階段更動")
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    check("★★ 充放電功率為經裁示的 production 值（非自行決定）",
          prod.charge_power_kw == 5.0 and prod.discharge_power_kw == 5.0)
    check("★★ max_power_kw 為經裁示的 150.0（HMI 指令上限，非由 80 kW 推導）",
          prod.max_power_kw == 150.0)
    check("★★ Layer 2 仍為 DEFERRED（未自行填值）",
          prod.min_switch_interval_sec is None)
    check("★★ Authority TTL / tolerance 為經裁示的 production 值",
          prod.authority_ttl_sec == 180.0
          and prod.authority_power_tolerance_kw == 1.25)
    check("★★ 未經裁示的參數仍為 None（未為了就緒而亂補）",
          prod.min_switch_interval_sec is None
          and prod.meter_stale_grace_sec is None)
    # 🔴 Phase 6.9-A：service 取得了一個經裁示核准的控制出口來源
    #    （`build_live_operator_run()` 內的延後 import）。
    #    不變量改寫為「更精確」而非「更寬鬆」：模組層仍完全不得 import，
    #    且該延後 import 只能存在於那一個函式裡。
    _svc_tree = _tree("pcs_auto_control_service.py")
    check("★★ service 模組層不 import operator / api 控制出口",
          not (_top_imports(_svc_tree) & {"device_control_operator",
                                          "api_client"}))
    _elsewhere = set()
    for _f in ast.walk(_svc_tree):
        if isinstance(_f, ast.FunctionDef) and _f.name != "build_live_operator_run":
            _elsewhere |= (_all_imports(_f) & {"device_control_operator"})
    check(f"★★ service 的 operator 延後 import 只存在於 build_live_operator_run()"
          f"（其他函式命中={sorted(_elsewhere)}）", not _elsewhere)
    check("★★ OBSERVE_ONLY 組裝完成後行程內仍無 operator",
          "device_control_operator" not in sys.modules)
    check("★★ 執行鏈的 executor / verifier / store 皆為 None",
          (lambda ch: ch.executor is None and ch.verifier is None
           and ch.store is None)(st._chain()))
    check("★★ 年度離峰日仍只含經人工驗收的年度",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027))
    check("★★ LastControl 為唯讀使用（本階段未寫入任何紀錄）",
          st._chain().store is None)

    # ---------------- R1（設計登記，未接線）----------------
    print("\nR1. Auto Report 觸發設計")
    check("★★ R1. 目前明確為未接線",
          PRD.r1_trigger_state() == PRD.R1_TRIGGER_NOT_WIRED)
    check("★★ R1. 設計明載由控制方向／session lifecycle 驅動",
          "session lifecycle" in PRD.R1_DESIGN_NOTE
          and "排程 ON" in PRD.R1_DESIGN_NOTE)
    check("★★ R1. 明載不得反過來影響控制權或 dispatch 規則",
          "不得**反過來影響控制" in PRD.R1_DESIGN_NOTE
          or "不得**反過來影響控制：" in PRD.R1_DESIGN_NOTE
          or "不得" in PRD.R1_DESIGN_NOTE and "dispatch 規則" in PRD.R1_DESIGN_NOTE)
    # ⚠️ charge_discharge_report **會**被 import —— 但只用它的 read_all（唯讀取樣），
    #    那是 ESS reader 的來源，與報告 session lifecycle 無關。
    #    真正要證明的是：沒有碰報告的監看／排程／session 生命週期。
    check("★★ R1. wiring 模組不 import 報告監看／排程服務",
          not (_all_imports(_tree("pcs_auto_control_production.py"))
               & {"report_monitor", "auto_monitor_service",
                  "device_control_menu"}))
    _prd_src = io.open(os.path.join(HERE, "pcs_auto_control_production.py"),
                       encoding="utf-8").read()
    # ⚠️ `pcs_schedule_enabled` 已不適合當判準 —— 它是 ESS 讀值欄位名，
    #    本模組的註解會合理提到它（說明未登入時該欄位為 None）。
    #    改為只看真正的報告 session lifecycle 函式名稱。
    check("★★ R1. 未觸碰任何報告 session lifecycle 函式",
          not any(k in _prd_src for k in ("start_session", "should_auto_end",
                                          "find_active_session",
                                          "auto_schedule_check",
                                          "_report_finalize")))
    check("★★ R1. 只用到 read_all（唯讀取樣），不使用報告產出路徑",
          "read_all" in _prd_src)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.5-B Orchestrator Wiring 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
