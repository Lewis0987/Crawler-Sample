# -*- coding: utf-8 -*-
"""
Phase 6.4 Safety Gate 驗證 —— 完全離線
======================================================================
用途
    驗證「Decision 提出建議後、真正送 PCS 之前」的安全許可判定。
    重點：Fail Closed、方向性 SOC 保護、作用中嚴重告警、STOP 豁免、
          reason priority 的確定性。

是否需要設備
    **不需要**。完全離線：不連 HMI / 6160 / PCS / BMS，不開 socket，無檔案 I/O。
    所有外部狀態以 SafetyRequest 注入；時間以 now 參數注入。

涵蓋範圍
    A. 正常通過
    B. ESS 資料可信度   missing / comm / read_too_slow / stale / invalid
    C. PCS 故障         True / None（未知）
    D. 電池上下電       已下電 / 切換中 / 未知
    E. 嚴重告警         作用中 / 已恢復 / 舊但仍作用中 / 來源不完整 / 無法解析
    F. 控制模式         手動放行；智慧 / 未啟用 / 未知一律拒絕
    G. SOC 方向性保護   98.99 vs 99；1 vs 1.01；交叉不互擋
    H. 功率             None / 0 / 負 / NaN / inf / bool；額定上限框架
    I. min_switch_interval  未配置 / history 不可用 / 太快 / 已滿 / 同動作
    J. STOP 豁免
    K. checks 完整性與 reason priority
    L. 零 I/O 與相依邊界

用法
    python test_phase6_safety_gate.py          # exit 0 = PASS
"""
import os
import sys
import ast
import json
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safety_gate as SG                                # noqa: E402

# 於 import safety_gate 後立即快照 —— 用來證明它（含其相依）不具備任何網路能力
_MODULES_AFTER_SG = set(sys.modules)

import decision_engine as DE                            # noqa: E402
import charge_discharge_report_config as CFG            # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def ess(soc=50.0, now=100.5, started=100.0, completed=100.2, **over):
    r = {"communication_ok": True, "soc_percent": soc, "pcs_fault_flag": False,
         "battery_power_status": SG.BATT_ON}
    r.update(over)
    return DE.ess_snapshot_from_reading(r, started, completed, now=now)


def req(action=SG.REQ_CHARGE, power=30.0, **over):
    kw = dict(requested_action=action, target_power_kw=power, ess=ess(),
              alarm_rows=(), alarm_source_complete=True,
              pcs_mode_state={"schedule_switch": 0, "manual_switch": 1})
    kw.update(over)
    return SG.SafetyRequest(**kw)


GATE = SG.SafetyGate()
NOW = 1000.0


def run(r, gate=GATE, now=NOW):
    return gate.check(r, now=now)


def main():
    print("== Phase 6.4 Safety Gate 驗證（完全離線）==\n")
    c = SG.DEFAULT_SAFETY_CONFIG
    print(f"SOC 保護 {c.soc_min_percent:g} / {c.soc_max_percent:g}；"
          f"critical levels={sorted(c.critical_alarm_levels)}；"
          f"max_power_kw={c.max_power_kw}；min_switch_interval={c.min_switch_interval_sec}\n")

    # ---------------- A. 正常 ----------------
    print("A. 正常通過")
    r = run(req())
    check("全部正常（charge）→ allowed / SAFE_OK", r.allowed and r.reason == SG.SAFE_OK)
    check("  requested_action / target_power_kw 原樣保留",
          r.requested_action == SG.REQ_CHARGE and r.target_power_kw == 30.0)
    check("  checked_at 為注入的 now", r.checked_at == NOW)
    check("正常（discharge）→ allowed", run(req(action=SG.REQ_DISCHARGE)).allowed)
    check("非法 action → DENY / INVALID_REQUESTED_ACTION",
          run(req(action="idle")).reason == SG.R_INVALID_REQUESTED_ACTION)
    check("action=no_action → DENY", run(req(action="no_action")).reason
          == SG.R_INVALID_REQUESTED_ACTION)
    check("SafetyRequest 為 None → DENY", GATE.check(None, now=NOW).reason
          == SG.R_INVALID_REQUESTED_ACTION)

    # ---------------- B. ESS 資料可信度 ----------------
    print("\nB. ESS 資料可信度")
    check("ess=None → ESS_MISSING", run(req(ess=None)).reason == SG.R_ESS_MISSING)
    check("communication_ok=False → ESS_COMM_FAILED",
          run(req(ess=ess(communication_ok=False))).reason == SG.R_ESS_COMM_FAILED)
    slow = ess(now=112.0, started=100.0, completed=111.0)
    check("READ_TOO_SLOW → ESS_READ_TOO_SLOW",
          slow.reason == DE.E_READ_TOO_SLOW
          and run(req(ess=slow)).reason == SG.R_ESS_READ_TOO_SLOW)
    stale = ess(now=200.0)
    check("stale → ESS_STALE", stale.stale and run(req(ess=stale)).reason == SG.R_ESS_STALE)
    bad = ess(soc_percent="x")
    check("EssSnapshot invalid（型別錯）→ ESS_INVALID",
          (not bad.valid) and run(req(ess=bad)).reason == SG.R_ESS_INVALID)
    check("NO_DATA snapshot → DENY", not run(req(ess=DE.ess_no_data())).allowed)

    # ---------------- C. PCS 故障 ----------------
    print("\nC. PCS 故障")
    check("pcs_fault_flag=True → PCS_FAULT",
          run(req(ess=ess(pcs_fault_flag=True))).reason == SG.R_PCS_FAULT)
    check("pcs_fault_flag=None → PCS_STATE_UNKNOWN（不預設正常）",
          run(req(ess=ess(pcs_fault_flag=None))).reason == SG.R_PCS_STATE_UNKNOWN)
    check("SafetyRequest.pcs_fault=True 可覆寫 ess 的 False",
          run(req(pcs_fault=True)).reason == SG.R_PCS_FAULT)
    check("SafetyRequest.pcs_fault=False 且 ess 為 None → 通過（呼叫端已用 pcs_is_fault 判定）",
          run(req(pcs_fault=False, ess=ess(pcs_fault_flag=None))).allowed)

    # ---------------- D. 電池上下電 ----------------
    print("\nD. 電池上下電")
    check("已下電 → BATTERY_NOT_POWERED",
          run(req(ess=ess(battery_power_status=SG.BATT_OFF))).reason == SG.R_BATTERY_NOT_POWERED)
    for st in ("切換中", "未知", None, ""):
        check(f"battery_power_status={st!r} → BATTERY_STATE_UNKNOWN",
              run(req(ess=ess(battery_power_status=st))).reason == SG.R_BATTERY_STATE_UNKNOWN)
    check("已上電 → 通過", run(req(ess=ess(battery_power_status=SG.BATT_ON))).allowed)

    # ---------------- E. 嚴重告警 ----------------
    print("\nE. 嚴重告警（當下作用中，非 session 新增）")
    check("作用中 level 0 → CRITICAL_ALARM_ACTIVE",
          run(req(alarm_rows=({"level": 0, "alarmStatus": True},))).reason
          == SG.R_CRITICAL_ALARM_ACTIVE)
    check("level 0 已恢復 → 不阻擋",
          run(req(alarm_rows=({"level": 0, "alarmStatus": False},))).allowed)
    check("★ 舊的（session 前既存）但仍作用中的 level 0 → 必須阻擋",
          run(req(alarm_rows=({"level": 0, "alarmStatus": True, "id": 1,
                               "alarmTime": "2020-01-01 00:00:00"},))).reason
          == SG.R_CRITICAL_ALARM_ACTIVE)
    check("level 1 作用中 → 不阻擋（非 critical）",
          run(req(alarm_rows=({"level": 1, "alarmStatus": True},))).allowed)
    check("level 為字串 \"0\" 且作用中 → 仍阻擋（enum 正規化）",
          run(req(alarm_rows=({"level": "0", "alarmStatus": "true"},))).reason
          == SG.R_CRITICAL_ALARM_ACTIVE)
    check("level 0 但 alarmStatus 無法判定 → 保守視為作用中 → 阻擋",
          run(req(alarm_rows=({"level": 0, "alarmStatus": "???"},))).reason
          == SG.R_CRITICAL_ALARM_ACTIVE)
    check("多筆混合：只要有一筆作用中 level 0 就阻擋",
          run(req(alarm_rows=({"level": 1, "alarmStatus": True},
                              {"level": 0, "alarmStatus": False},
                              {"level": 0, "alarmStatus": True}))).reason
          == SG.R_CRITICAL_ALARM_ACTIVE)
    for v, label in ((None, "None"), (False, "False")):
        check(f"alarm_source_complete={label} → ALARM_SOURCE_UNAVAILABLE",
              run(req(alarm_source_complete=v)).reason == SG.R_ALARM_SOURCE_UNAVAILABLE)
    check("alarm_rows 非 list → ALARM_SOURCE_UNAVAILABLE",
          run(req(alarm_rows="oops")).reason == SG.R_ALARM_SOURCE_UNAVAILABLE)
    check("level 無法解析 → ALARM_SOURCE_UNAVAILABLE",
          run(req(alarm_rows=({"level": "abc", "alarmStatus": True},))).reason
          == SG.R_ALARM_SOURCE_UNAVAILABLE)
    check("★ 不以 len(rows)==100 武斷判定截斷（complete=True 即接受）",
          run(req(alarm_rows=tuple({"level": 3, "alarmStatus": True} for _ in range(100)),
                  alarm_source_complete=True)).allowed)

    # ---------------- F. 控制模式 ----------------
    print("\nF. 控制模式（依 read-back，不採信『上次控制成功』）")
    check("手動模式（0/1）→ 放行",
          run(req(pcs_mode_state={"schedule_switch": 0, "manual_switch": 1})).allowed)
    check("智慧模式（1/x）→ CONTROL_MODE_SMART",
          run(req(pcs_mode_state={"schedule_switch": 1, "manual_switch": 0})).reason
          == SG.R_CONTROL_MODE_SMART)
    check("未啟用模式（0/0）→ CONTROL_MODE_NONE",
          run(req(pcs_mode_state={"schedule_switch": 0, "manual_switch": 0})).reason
          == SG.R_CONTROL_MODE_NONE)
    for st, label in (({"schedule_switch": None, "manual_switch": 1}, "schedule=None"),
                      ({"schedule_switch": 0, "manual_switch": None}, "manual=None"),
                      ({"schedule_switch": 2, "manual_switch": 1}, "schedule=2"),
                      ({"schedule_switch": 0, "manual_switch": 9}, "manual=9"),
                      (None, "pcs_mode_state=None"),
                      ("x", "pcs_mode_state 非 dict")):
        check(f"{label} → CONTROL_MODE_UNKNOWN",
              run(req(pcs_mode_state=st)).reason == SG.R_CONTROL_MODE_UNKNOWN)

    # ---------------- G. SOC 方向性保護 ----------------
    print("\nG. SOC 方向性 Safety（99 / 1；與 6.3-B 的 90/85/20/25 不同層）")
    check("CHARGE SOC=98.99 → 允許", run(req(ess=ess(soc=98.99))).allowed)
    check("CHARGE SOC=99 → SOC_AT_MAX_LIMIT",
          run(req(ess=ess(soc=99.0))).reason == SG.R_SOC_AT_MAX_LIMIT)
    check("CHARGE SOC=99.5 → SOC_AT_MAX_LIMIT",
          run(req(ess=ess(soc=99.5))).reason == SG.R_SOC_AT_MAX_LIMIT)
    check("DISCHARGE SOC=1.01 → 允許",
          run(req(action=SG.REQ_DISCHARGE, ess=ess(soc=1.01))).allowed)
    check("DISCHARGE SOC=1 → SOC_AT_MIN_LIMIT",
          run(req(action=SG.REQ_DISCHARGE, ess=ess(soc=1.0))).reason == SG.R_SOC_AT_MIN_LIMIT)
    check("DISCHARGE SOC=0.5 → SOC_AT_MIN_LIMIT",
          run(req(action=SG.REQ_DISCHARGE, ess=ess(soc=0.5))).reason == SG.R_SOC_AT_MIN_LIMIT)
    check("★ CHARGE 不因 SOC<=1 被擋（SOC=0.5 仍允許充電）",
          run(req(action=SG.REQ_CHARGE, ess=ess(soc=0.5))).allowed)
    check("★ DISCHARGE 不因 SOC>=99 被擋（SOC=99.5 仍允許放電）",
          run(req(action=SG.REQ_DISCHARGE, ess=ess(soc=99.5))).allowed)
    check("SOC 門檻取自 charge_discharge_report_config（單一真相來源）",
          c.soc_max_percent == CFG.SOC_MAX_PERCENT and c.soc_min_percent == CFG.SOC_MIN_PERCENT)
    check("critical_alarm_levels 取自 CFG.ALARM_STOP_LEVELS",
          set(c.critical_alarm_levels) == set(CFG.ALARM_STOP_LEVELS))

    # ---------------- H. 功率 ----------------
    print("\nH. target_power_kw")
    check("None → POWER_NOT_SPECIFIED", run(req(power=None)).reason == SG.R_POWER_NOT_SPECIFIED)
    for v, label in ((0, "0"), (-1, "負值"), (float("nan"), "NaN"),
                     (float("inf"), "inf"), (True, "bool")):
        check(f"{label} → POWER_INVALID", run(req(power=v)).reason == SG.R_POWER_INVALID)
    check("正常正值 → 通過", run(req(power=0.1)).allowed)
    pr = [ch for ch in run(req(power=999999.0)).checks if ch.name == SG.C_POWER_RANGE][0]
    check("★ max_power_kw=None → 不做上限比較（999999 kW 也不擋，且標記 skipped）",
          pr.skipped and pr.passed and run(req(power=999999.0)).allowed)
    check("★ 未自行套用 150 kW", run(req(power=151.0)).allowed)
    g150 = SG.SafetyGate(SG.SafetyConfig(max_power_kw=100.0))
    check("配置 max_power_kw=100 後：120 kW → POWER_OUT_OF_RANGE",
          run(req(power=120.0), gate=g150).reason == SG.R_POWER_OUT_OF_RANGE)
    check("配置 max_power_kw=100 後：100 kW（=上限）→ 允許",
          run(req(power=100.0), gate=g150).allowed)

    # ---------------- I. min_switch_interval ----------------
    print("\nI. min_switch_interval（≠ 6.3-B 的 min_hold）")
    sw = [ch for ch in run(req()).checks if ch.name == SG.C_SWITCH][0]
    check("★ 未配置 → skipped，且不要求 last_control", sw.skipped and sw.passed)
    check("  未配置且無 last_control 仍可通過", run(req(last_control=None)).allowed)
    g30 = SG.SafetyGate(SG.SafetyConfig(min_switch_interval_sec=30.0))
    check("★ 已配置但無 last_control → CONTROL_HISTORY_UNAVAILABLE",
          run(req(), gate=g30).reason == SG.R_CONTROL_HISTORY_UNAVAILABLE)
    check("已配置但 last_control.success=False → CONTROL_HISTORY_UNAVAILABLE",
          run(req(last_control=SG.LastControl(SG.REQ_DISCHARGE, 990.0, success=False)),
              gate=g30).reason == SG.R_CONTROL_HISTORY_UNAVAILABLE)
    check("已配置但 last_control.at 非數值 → CONTROL_HISTORY_UNAVAILABLE",
          run(req(last_control=SG.LastControl(SG.REQ_DISCHARGE, "x")),
              gate=g30).reason == SG.R_CONTROL_HISTORY_UNAVAILABLE)
    check("★ 反向切換且距上次 10s < 30s → SWITCH_TOO_SOON",
          run(req(last_control=SG.LastControl(SG.REQ_DISCHARGE, 990.0)),
              gate=g30).reason == SG.R_SWITCH_TOO_SOON)
    check("★ 反向切換且距上次 30s（=門檻）→ 允許",
          run(req(last_control=SG.LastControl(SG.REQ_DISCHARGE, 970.0)), gate=g30).allowed)
    sw2 = [ch for ch in run(req(last_control=SG.LastControl(SG.REQ_CHARGE, 999.0)),
                            gate=g30).checks if ch.name == SG.C_SWITCH][0]
    check("與上次相同動作 → 非切換，skipped 且不擋", sw2.skipped and sw2.passed)
    check("  同動作即使距上次僅 1s 仍允許",
          run(req(last_control=SG.LastControl(SG.REQ_CHARGE, 999.0)), gate=g30).allowed)

    # ---------------- J. STOP 豁免 ----------------
    print("\nJ. STOP（安全停止）豁免")
    hard = req(action=SG.REQ_STOP, power=None,
               ess=ess(soc=99.5, battery_power_status=SG.BATT_OFF, pcs_fault_flag=True),
               alarm_rows=({"level": 0, "alarmStatus": True},),
               alarm_source_complete=None,
               pcs_mode_state={"schedule_switch": 1, "manual_switch": 0},
               last_control=None)
    rs = run(hard, gate=g30)
    check("★ STOP 不受 SOC / 電池 / 功率 / 告警 / 模式 / 切換間隔 阻擋 → allowed", rs.allowed)
    exempt = {ch.name for ch in rs.checks if ch.skipped}
    check(f"  被豁免的檢查項：{sorted(exempt)}",
          {SG.C_SOC, SG.C_BATTERY, SG.C_POWER, SG.C_MODE, SG.C_SWITCH,
           SG.C_ALARM, SG.C_PCS_FAULT} <= exempt)
    check("★ STOP 仍要求 ESS 通訊可用 → 通訊失敗時 DENY",
          run(req(action=SG.REQ_STOP, power=None, ess=ess(communication_ok=False))).reason
          == SG.R_ESS_COMM_FAILED)
    check("STOP 且 ess=None → ESS_MISSING",
          run(req(action=SG.REQ_STOP, power=None, ess=None)).reason == SG.R_ESS_MISSING)
    check("★ STOP 不因資料過期被擋（過期仍可停）",
          run(req(action=SG.REQ_STOP, power=None, ess=ess(now=200.0))).allowed)
    check("STOP 的 target_power_kw 為 None 也允許",
          run(req(action=SG.REQ_STOP, power=None)).allowed)

    # ---------------- K. checks 與 reason priority ----------------
    print("\nK. checks 完整性與 reason priority")
    r = run(req())
    check(f"checks 保留全部 {len(SG.ALL_CHECKS)} 項", len(r.checks) == len(SG.ALL_CHECKS))
    check("checks 名稱與 ALL_CHECKS 一致",
          tuple(ch.name for ch in r.checks) == SG.ALL_CHECKS)
    multi = req(power=None, ess=ess(soc=99.0, pcs_fault_flag=True,
                                    battery_power_status=SG.BATT_OFF),
                pcs_mode_state={"schedule_switch": 1, "manual_switch": 0},
                alarm_rows=({"level": 0, "alarmStatus": True},))
    rm = run(multi)
    check(f"多項同時失敗全部保留：{list(rm.failed_reasons)}", len(rm.failed) >= 5)
    check("★ reason 取 REASON_PRIORITY 最前者（PCS_FAULT）", rm.reason == SG.R_PCS_FAULT)
    check("failed 依 priority 排序",
          [SG._PRIORITY_INDEX[x] for x in rm.failed_reasons]
          == sorted(SG._PRIORITY_INDEX[x] for x in rm.failed_reasons))
    check("重複呼叫 reason 穩定（不依 dict 迭代順序）",
          len({run(multi).reason for _ in range(20)}) == 1)
    check("REASON_PRIORITY 無重複且涵蓋所有 R_ 常數",
          len(SG.REASON_PRIORITY) == len(set(SG.REASON_PRIORITY))
          and {v for k, v in vars(SG).items()
               if k.startswith("R_") and isinstance(v, str)} == set(SG.REASON_PRIORITY))
    check("allowed 時 reason=SAFE_OK 且無失敗項", r.reason == SG.SAFE_OK and not r.failed)
    txt = json.dumps(rm.as_json_dict(), ensure_ascii=False, allow_nan=False)
    check("SafetyResult 可嚴格 JSON 序列化且無 Infinity/NaN",
          "Infinity" not in txt and json.loads(txt)["reason"] == SG.R_PCS_FAULT)

    check("SafetyConfig 拒絕 soc_min >= soc_max",
          _raises(lambda: SG.SafetyConfig(soc_min_percent=99.0)))
    check("SafetyConfig 拒絕非正的 max_power_kw",
          _raises(lambda: SG.SafetyConfig(max_power_kw=0)))
    check("SafetyConfig 拒絕負的 min_switch_interval_sec",
          _raises(lambda: SG.SafetyConfig(min_switch_interval_sec=-1)))
    check("SafetyConfig 預設 max_power_kw / min_switch_interval_sec 皆為 None",
          c.max_power_kw is None and c.min_switch_interval_sec is None)

    # ---------------- L. 零 I/O 與相依邊界 ----------------
    print("\nL. 零 I/O 與相依邊界")
    src = open(SG.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    check(f"imports 僅標準庫 + decision_engine + 純設定檔：{sorted(imported)}",
          imported <= {"sys", "math", "time", "argparse", "dataclasses",
                       "decision_engine", "charge_discharge_report_config"})
    for m in ("requests", "socketio", "meter_client", "power_classifier",
              "tou_calendar", "charge_discharge_report", "device_control_operator",
              "device_control_scraper", "report_monitor", "api_client"):
        check(f"未 import {m}", m not in imported)
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    # 註：不把 get / post / put 列入黑名單 —— `dict.get()` 是一般字典存取，
    #     會造成誤判。網路能力改由「imports + sys.modules 傳遞性」證明（更強）。
    io_calls = (calls & {"open", "input", "eval", "exec", "compile", "__import__"}) | \
               (attrs & {"dump", "load", "urlopen", "connect", "emit", "request", "send"})
    check(f"無檔案／網路 I/O 呼叫（命中={sorted(io_calls)}）", not io_calls)
    net = {m for m in ("requests", "socketio", "engineio", "urllib", "http",
                       "socket", "websocket") if m in _MODULES_AFTER_SG}
    check(f"★ 傳遞性：import safety_gate 後未載入任何網路模組（命中={sorted(net)}）", not net)
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    check(f"未出現 249 / 229 / 150（命中={sorted(consts & {249, 229, 150})}）",
          not (consts & {249, 229, 150}))
    idents = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    idents |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("未引用 SafetyMonitor / AlarmTracker（Report 層 Safety）",
          not (idents & {"SafetyMonitor", "AlarmTracker"}))
    check("未引用 min_hold_sec / held_since / AUTO_COOLDOWN_SEC",
          not (idents & {"min_hold_sec", "held_since", "AUTO_COOLDOWN_SEC"}))

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.4 Safety Gate 驗證 {'PASS' if ok_all else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def _raises(fn):
    try:
        fn()
        return False
    except (ValueError, TypeError):
        return True


if __name__ == "__main__":
    sys.exit(main())
