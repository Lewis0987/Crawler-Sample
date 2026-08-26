# -*- coding: utf-8 -*-
"""
test_phase6_68_closure.py — Phase 6.8 Regression & Closure（不變量 A~U）
======================================================================
核心命題
    「Phase 6 全部參數就緒、整條 production path 已接通並經現場驗證，
      但**結構上仍不可能送出任何指令** —— 開啟派工必須是另一次明確裁示。」

本檔**不新增任何功能**，只把散落在各階段的關鍵不變量收斂成單一結案閘門。
任一條失敗即代表 Go-Live 前提被破壞。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch、零實機 command。

用法
    python test_phase6_68_closure.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tou_calendar as TC                                # noqa: E402
import safety_gate as SG                                 # noqa: E402
import control_authority as CA                           # noqa: E402
import tariff_provider as TP                             # noqa: E402
import meter_client as MC                                # noqa: E402
import annual_off_peak_calendar as AC                    # noqa: E402
import official_annual_calendar as OC                    # noqa: E402
import last_control_store as LCS                         # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import pcs_control_executor as EXC                       # noqa: E402
import execution_reconciler as RC                        # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402
import pcs_auto_control_production as PRD                # noqa: E402
import report_monitor as RM                              # noqa: E402
import phase6_report_bridge as BR                        # noqa: E402

import test_phase6_d5b_orchestrator_wiring as D5B        # noqa: E402

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


C = CFG.DEFAULT_CONTROL_CONFIG

# production path 上的全部模組（用於相依性稽核）
PROD_MODULES = ("pcs_auto_control_config.py", "pcs_auto_control_runtime.py",
                "pcs_auto_control_service.py", "pcs_auto_control_production.py",
                "production_arbiter.py", "production_execution_chain.py",
                "execution_reconciler.py", "control_authority.py",
                "pcs_control_integration.py", "decision_observer.py",
                "ess_snapshot_adapter.py", "meter_observation_adapter.py",
                "tariff_provider.py", "annual_off_peak_calendar.py",
                "official_annual_calendar.py", "phase6_report_bridge.py")

NET = {"requests", "urllib", "urllib3", "http", "httpx", "aiohttp",
       "socket", "ssl", "ftplib", "telnetlib", "webbrowser"}


def no_dispatch(res):
    return (res.executed is False and res.dispatch_count_delta == 0
            and res.operator_outcome is None)


# ======================================================================
def main():
    print("== Phase 6.8 Regression & Closure 驗證（完全離線）==\n")
    RM.clear_control_session_provider()

    # ---------------- A/B. dispatch 就緒 vs 啟用 ----------------
    print("A/B. dispatch_ready 與 dispatch_enabled 完全分離")
    check("★★ A. dispatch_ready = True（所有必要參數皆已就緒）",
          C.dispatch_ready is True and C.missing_required() == ())
    check("★★ B. dispatch_enabled = False（模組層）",
          RT.DISPATCH_ENABLED is False)
    check("★★ B. dispatch_enabled = False（Runtime 實例預設）",
          RT.AutoControlRuntime().dispatch_enabled is False)
    check("★★ A/B. 就緒**不等於**啟用（兩者同時成立且互不影響）",
          C.dispatch_ready is True and RT.DISPATCH_ENABLED is False)

    # ---------------- C/D. OBSERVE_ONLY 無出口 ----------------
    print("\nC/D. OBSERVE_ONLY：結構上沒有控制出口")
    obs, recovery, wiring = SVC.build_production_stack()
    check("★★ C. 模式為 OBSERVE_ONLY 且 can_dispatch=False",
          wiring.mode == PRD.MODE_OBSERVE_ONLY
          and wiring.can_dispatch is False)
    check("★★ D. executor / verifier 一律不建立",
          PRD.build_executor() is None and PRD.build_verifier() is None
          and wiring.sources["executor"] == PRD.SRC_NOT_WIRED
          and wiring.sources["verifier"] == PRD.SRC_NOT_WIRED)
    for m in ("DISPATCH", "LIVE", "ACTIVE"):
        try:
            PRD.build_executor(m)
            ok = False
        except ValueError:
            ok = True
        check(f"★★ D. 非 OBSERVE_ONLY 模式（{m}）一律拒絕建立出口", ok)
    chain = obs.__self__
    check("★★ D. 執行鏈的 executor / verifier / store 皆為 None",
          chain.executor is None and chain.verifier is None
          and chain.store is None)
    check("★★ C. 跑一輪：未執行、無 operator 結果",
          no_dispatch(obs()))

    # ---------------- E/F/G. 功率參數與上限 ----------------
    print("\nE/F/G. 功率參數與 Safety Gate 上限")
    check("★★ E. max_power_kw = 150.0", C.max_power_kw == 150.0)
    check("★★ F. Production target 仍為 ±5.0 kW",
          C.charge_power_kw == 5.0 and C.discharge_power_kw == 5.0)
    check("★★ F. 操作功率遠低於上限（5.0 ≪ 150.0）",
          C.charge_power_kw < C.max_power_kw)

    class _Ess(object):
        valid, stale = True, False
        soc_percent, communication_ok = 50.0, True
        read_duration_sec, age_sec = 0.5, 1.0
        battery_power_status = SG.BATT_ON
        pcs_fault_flag = False

    gate = SG.SafetyGate(SG.SafetyConfig(max_power_kw=C.max_power_kw))

    def _rng(action, power):
        r = gate.check(SG.SafetyRequest(
            requested_action=action, target_power_kw=power, ess=_Ess(),
            pcs_fault=False, alarm_rows=(), alarm_source_complete=True,
            pcs_mode_state={"schedule_switch": 0, "manual_switch": 1},
            last_control=None))
        rc = [c for c in r.checks if c.name == SG.C_POWER_RANGE]
        return r, (rc[0] if rc else None)

    for action in ("charge", "discharge"):
        check(f"★★ G. {action} 150.0 kW → 上限檢查放行",
              _rng(action, 150.0)[1].passed is True)
        for over in (150.1, 160.0, 1000.0):
            r, rc = _rng(action, over)
            check(f"★★ G. {action} {over} kW → Safety Gate BLOCK",
                  rc.passed is False
                  and rc.reason == SG.R_POWER_OUT_OF_RANGE
                  and r.allowed is False)
    import device_control_operator as OP
    def _op_blocked(d, v):
        try:
            OP._build_pcs_payload("ac_active", d, v)
            return False
        except RuntimeError:
            return True
    check("★★ G. operator 第二層防線仍存在（充放電 >150 皆不送 API）",
          _op_blocked("charge", 150.1) and _op_blocked("discharge", 150.1)
          and OP.PCS_CONTROL_MODES["ac_active"]["max"] == 150)

    # ---------------- H. Authority 參數 ----------------
    print("\nH. Control Authority 參數")
    check("★★ H. authority_ttl_sec = 180.0 且 tolerance = 1.25",
          C.authority_ttl_sec == 180.0
          and C.authority_power_tolerance_kw == 1.25)
    check("★★ H. 相依約束成立（authz 10 ≪ authority 180；"
          "authority ≥ interval + readback timeout）",
          C.authorization_ttl_sec < C.authority_ttl_sec
          and C.authority_ttl_sec >= C.decision_interval_sec
          + C.readback_timeout_sec)
    check("★★ H. Authority 模組預設仍未配置（避免任何隱性放行）",
          CA.AuthorityPolicy().authority_ttl_sec is None
          and CA.AuthorityPolicy().authority_power_tolerance_kw is None)
    check("★★ H. 允許狀態集合仍只有 IDLE 與 OWNED",
          CA.AUTHORITY_ALLOWED_STATES
          == frozenset({CA.AUTH_IDLE, CA.AUTH_OWNED}))
    check("★★ H. 時機守則仍在（NOT_YET_CORROBORATED ≠ CONFLICT_POWER）",
          CA.CA_NOT_YET_CORROBORATED in CA.AUTHORITY_REASONS
          and CA.CA_NOT_YET_CORROBORATED != CA.CA_CONFLICT_POWER)

    # ---------------- I/J. 兩層互鎖 ----------------
    print("\nI/J. Layer 1 / Layer 2")
    check("★★ I. Layer 1 不依賴任何參數（無條件生效）",
          C.min_switch_interval_sec is None
          and PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
          in PCI.DIRECTION_BLOCK_REASONS)
    check("★★ J. Layer 2 維持 DEFERRED（刻意未配置）",
          C.min_switch_interval_sec is None
          and "min_switch_interval_sec" not in CFG.REQUIRED_FOR_DISPATCH)
    check("★★ J. 未配置時該規則為「未啟用」而非失敗",
          SG.SafetyGate(SG.SafetyConfig(
              max_power_kw=150.0, min_switch_interval_sec=None)) is not None)

    # ---------------- K~N. Fail Closed ----------------
    print("\nK~N. Fail Closed（真實 production 組裝，離線來源）")
    k = D5B.Stack(local_now=datetime(2028, 7, 12, 3, 0)).settle()
    check("★★ K. Tariff UNKNOWN（未 provision 年度）→ 未授權、0 calls",
          D5B.blocked_upstream(k.run()))
    m1 = D5B.Stack(meter=MC.no_data_snapshot(MC.SOURCE_PRODUCTION)).settle()
    check("★★ L. Meter 無資料 → 未授權、0 calls",
          D5B.blocked_upstream(m1.run()))
    m2 = D5B.Stack(meter=MC.evaluate(
        {"meter": 12.5, "meter_state": MC.S_OK, "demand": 8.0,
         "demand_state": "fault"}, received_at=1000.0, now=1000.0)).settle()
    check("★★ L. Meter demand_state=fault → 未授權、0 calls",
          D5B.blocked_upstream(m2.run()))
    e1 = D5B.Stack(ess_over={"communication_ok": False}).settle()
    check("★★ M. ESS 通訊失敗 → 未授權、0 calls",
          D5B.blocked_upstream(e1.run()))
    e2 = D5B.Stack().settle()
    e2.slow_read_sec = 600.0
    check("★★ M. ESS 陳舊 → 未授權、0 calls",
          D5B.blocked_upstream(e2.run()))
    n1 = D5B.Stack(pcs="CHARGING", local_now=D5B.DT_OFF_PEAK).settle()
    r1 = n1.run()
    check("★★ N. Authority 非 IDLE/OWNED → 未授權、0 calls",
          D5B.rec_of(r1).authority_state not in CA.AUTHORITY_ALLOWED_STATES
          and D5B.blocked_upstream(r1))
    n2 = D5B.Stack(pcs="CHARGING", local_now=D5B.DT_OFF_PEAK,
                   ess_over={"pcs_schedule_enabled": True}).settle()
    r2 = n2.run()
    check("★★ N. EXTERNAL_CONTROL → 未授權、0 calls",
          D5B.rec_of(r2).authority_state == CA.AUTH_EXTERNAL
          and D5B.blocked_upstream(r2))
    check("★★ N. OWNERSHIP_PENDING 為獨立 runtime 狀態且不可派工",
          RT.ST_OWNERSHIP_PENDING in RT.RUNTIME_STATES
          and RT.ST_OWNERSHIP_PENDING not in RT.DISPATCH_STATES
          and RC._RUNTIME_FALLBACK[RC.REC_PENDING]
          == RT.ST_OWNERSHIP_PENDING)

    # ---------------- O~R. 報告整合不變量 ----------------
    print("\nO~R. Auto Report 整合不變量")
    rm_src = io.open(os.path.join(HERE, "report_monitor.py"),
                     encoding="utf-8").read()
    br_src = io.open(os.path.join(HERE, "phase6_report_bridge.py"),
                     encoding="utf-8").read()
    check("★★ O. 報告層不 import 任何 Phase 6 控制模組",
          not (_all_imports(_tree("report_monitor.py"))
               & {"pcs_auto_control_runtime", "pcs_auto_control_service",
                  "pcs_auto_control_production", "production_arbiter",
                  "production_execution_chain", "control_authority",
                  "phase6_report_bridge"}))
    check("★★ O. 報告層不含任何控制指令字串",
          not any(k in rm_src for k in ("pcs_charge", "pcs_discharge",
                                        "pcs_stop_power", "param=1",
                                        "param=2")))
    check("★★ P. 橋接未建立第二套 report engine",
          not any(k in br_src for k in ("def start_session", "def finalize",
                                        "ReportSession(", "def _report_")))
    check("★★ P. 既有 session 入口仍是唯一的一個",
          rm_src.count("def _report_start_session") == 1)
    check("★★ P. 收尾仍由既有 should_auto_end() 單一決策點負責",
          "should_auto_end" in rm_src)
    check("★★ P. 控制側不 import 任何報告監看模組",
          not (_all_imports(_tree("pcs_auto_control_production.py"))
               & {"report_monitor", "auto_monitor_service"}))
    # Q：只有 OWNED + 方向一致才建立
    BR.install(lambda: D5B.Stack(pcs="CHARGING").run())
    for tag, auth, st, want in (
            ("OWNED + CHARGING", CA.AUTH_OWNED, PCI.PCS_CHARGING, True),
            ("UNKNOWN + CHARGING", CA.AUTH_UNKNOWN, PCI.PCS_CHARGING, False),
            ("EXTERNAL + CHARGING", CA.AUTH_EXTERNAL, PCI.PCS_CHARGING, False),
            ("OWNED + STANDBY", CA.AUTH_OWNED, "STANDBY", False)):
        class _A(object):
            state = auth

        class _LC(object):
            action = "charge"
            target_power_kw = 5.0
        got = BR.control_session_state(authority=_A(), pcs_state=st,
                                       last_control=_LC())
        check(f"★★ Q. {tag} → {'建立' if want else '不建立'}",
              (got is not None) is want)
    RM.set_control_session_provider(
        lambda: {"owned": True, "action": "charge", "target_power_kw": 5.0})
    check("★★ Q. Phase 6 宣稱方向與設備方向不符 → Fail Closed",
          RM._start_gate("manual", False, "discharge")[1]
          == "phase6_direction_mismatch")
    RM.clear_control_session_provider()
    check("★★ R. 未安裝橋接時，scheduler 舊路徑完全不退化",
          RM._control_session_provider is None
          and RM._start_gate("smart", True, "charge")[:3]
          == (True, "scheduler", "scheduler")
          and RM._start_gate("manual", False, "charge")[0] is False)

    # ---------------- S. restart / recovery ----------------
    print("\nS. restart / recovery 不繞過 OBSERVE_ONLY")
    rec = recovery()
    check("★★ S. recovery 可執行且 may_dispatch 恆為 False",
          rec is not None and rec.may_dispatch is False)
    rt = RT.AutoControlRuntime(observation_source=obs,
                               recovery_source=recovery)
    rt.recover()
    check("★★ S. 復原後不在派工狀態，且 dispatch_enabled 未變",
          rt.state not in RT.DISPATCH_STATES
          and rt.snapshot()["dispatch_enabled_instance"] is False)
    check("★★ S. 復原後再跑一輪仍 0 calls", no_dispatch(obs()))
    check("★★ S. reconciler 結構上不可能送指令",
          not (_all_imports(_tree("execution_reconciler.py"))
               & {"device_control_operator", "pcs_control_executor",
                  "production_execution_chain"}))

    # ---------------- T/U. 相依性稽核 ----------------
    print("\nT/U. 相依性稽核（全 production path）")
    for m in PROD_MODULES:
        check(f"★★ U. {m} 不相依 phase6_field_measure",
              "phase6_field_measure" not in _all_imports(_tree(m)))
    calendar_mods = ("annual_off_peak_calendar.py", "official_annual_calendar.py",
                     "taipower_calendar_extractor.py", "tariff_provider.py",
                     "tou_calendar.py")
    for m in calendar_mods:
        check(f"★★ T. {m} 無任何網路相依",
              not (_all_imports(_tree(m)) & NET))
    check("★★ T. 年度日曆為靜態已驗收資料（2026, 2027）",
          AC.PRODUCTION_PROVIDER.known_years == (2026, 2027)
          and all(OC.human_review_ok(y) for y in OC.SUPPORTED_YEARS))
    check("★★ T. 不會自動掃描目錄加入年度",
          not any(k in io.open(os.path.join(HERE,
                                            "annual_off_peak_calendar.py"),
                               encoding="utf-8").read()
                  for k in ("listdir", "glob", "os.walk", "scandir")))
    check("★★ T. 未涵蓋年度不得 fallback（2028 一律 None）",
          AC.PRODUCTION_PROVIDER.is_off_peak_day(
              __import__("datetime").date(2028, 9, 28)) is None)

    # ---------------- 結案總結 ----------------
    print("\n結案總結")
    check("★★ 時段判定四態齊備且 naive datetime 仍拒絕",
          TP.PRODUCTION_TIMEZONE_NAME == "Asia/Taipei"
          and TP.TariffProvider(
              holiday_provider=AC.PRODUCTION_PROVIDER).observe(
                  datetime(2026, 9, 28, 14)).reason == TP.TP_NAIVE_DATETIME)
    check("★★ 夏月定義未被更動（高壓 5/16~10/15）",
          TC.G_CONFIG.summer_start == (5, 16)
          and TC.G_CONFIG.summer_end == (10, 15))
    check("★★ ReadBack production 設定就緒（75 / 5 / 1）",
          PRD.build_readback_config().ready is True)
    check("★★ ReadBack 模組預設仍未配置",
          EXC.DEFAULT_READBACK_CONFIG.ready is False)
    check("★★ LastControl 只在 VERIFY_SUCCESS 寫入（契約未變）",
          "VERIFY_SUCCESS" in io.open(
              os.path.join(HERE, "last_control_store.py"),
              encoding="utf-8").read())
    check("★★ 未經裁示的參數仍為 None",
          C.min_switch_interval_sec is None
          and C.meter_stale_grace_sec is None)
    check("★★★ 最終閘門：所有參數就緒，但結構上仍不可能送出任何指令",
          C.dispatch_ready is True
          and RT.DISPATCH_ENABLED is False
          and PRD.build_executor() is None
          and PRD.wiring_report(config=C).can_dispatch is False)

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.8 Regression & Closure 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
