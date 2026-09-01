# -*- coding: utf-8 -*-
"""
phase6_live_precheck.py — 第一次 CONTROLLED SINGLE-LEG CHARGE LIVE 的
**全新即時 READ-ONLY PRECHECK**（Phase 6.9 / LIVE Step 1）
======================================================================
🔴 本檔**結構上不可能送出任何控制指令**
    · 不 import pcs_control_executor 以外的控制出口，也不建立 executor
    · 不 import operator（pcs_control_operator）
    · build_executor / build_verifier 一律不呼叫
    · mode 固定 OBSERVE_ONLY
    唯一的對外行為是唯讀 GET 與電表唯讀訂閱。

🔴 **不得沿用任何舊快照**
    每次執行都重新登入、重新 GET、重新取電表快照、重新跑一輪仲裁。

🔴 本檔**不做人工確認、不啟動 leg**
    只回報 20 項 precheck 與自然 Decision，然後結束。
    是否進入 LIVE 由人依本報告另行決定。

用法（唯讀）
    python phase6_live_precheck.py
    python phase6_live_precheck.py --meter-wait 20
"""
import os
import sys
import json
import time
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pcs_auto_control_config as CFG           # noqa: E402
import pcs_auto_control_production as PRD       # noqa: E402
import pcs_auto_control_service as SVC          # noqa: E402
import pcs_auto_control_runtime as RT           # noqa: E402
import safety_gate as SG                        # noqa: E402
import pcs_control_integration as PCI           # noqa: E402

EVIDENCE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "phase6_live_charge")

# 本次 LIVE 授權的方向（由裁示固定為 charge；本檔不接受 CLI 覆寫）
LIVE_ACTION = "charge"


def _now_wall():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt(v, spec=""):
    if v is None:
        return "None"
    if spec and isinstance(v, (int, float)):
        return format(v, spec)
    return str(v)


def collect(meter_wait=15.0, cycles=6, cycle_interval=10.0):
    """一次完整的即時唯讀取樣。回傳 dict（全部為觀測事實，不含任何判定放寬）。"""
    out = {"collected_at": _now_wall(), "errors": []}

    # ---- 1. 登入（只取唯讀權限）----
    client, token = PRD.build_api_client(login=True)
    out["login_ok"] = token is not None          # 🔴 只記布林，不輸出 token

    # ---- 2. 電表訂閱（唯讀）----
    mc = PRD.build_meter_client(connect=True)
    deadline = time.monotonic() + float(meter_wait)
    snap = None
    while time.monotonic() < deadline:
        snap = mc.get_snapshot()
        if snap is not None and getattr(snap, "valid", False):
            break
        time.sleep(1.0)

    try:
        # ---- 3. 原始 ESS 讀值（唯讀 GET）----
        import charge_discharge_report as CDR
        reading = CDR.read_all(client)
        out["reading"] = {
            k: reading.get(k) for k in (
                "communication_ok", "soc_percent", "actual_active_power_kw",
                "dc_power_kw", "pcs_charging_flag", "pcs_discharging_flag",
                "pcs_standby_flag", "pcs_running_flag", "pcs_fault_flag",
                "pcs_schedule_enabled", "pcs_manual_switch", "raw_source_time")
        }
        out["reading"]["_fail"] = list(reading.get("_fail") or ())
        rows = reading.get("alarm_rows")
        out["alarm_row_count"] = None if rows is None else len(rows)
        out["alarm_total_raw"] = reading.get("alarm_total_raw")
        # 🔴 「active alarm = 0」採 **Safety Gate 本身的語意**（_chk_alarm）：
        #    只有「level ∈ critical_alarm_levels」且「無法證明已恢復」才算作用中。
        #    非嚴重等級的既有告警不構成阻擋條件 —— 不得自行改嚴或改鬆。
        crit = SG.SafetyConfig().critical_alarm_levels
        n_crit, n_unparsable = 0, 0
        for row in (rows or ()):
            if not isinstance(row, dict):
                n_unparsable += 1
                continue
            lv = SG.CFG.as_int(row.get("level"))   # 與 _chk_alarm 同一個解析器
            if lv is None:
                n_unparsable += 1
                continue
            if lv in crit and SG._alarm_active(row.get("alarmStatus")) is not False:
                n_crit += 1
        out["critical_active_alarms"] = n_crit
        out["alarm_unparsable_rows"] = n_unparsable
        try:
            out["alarm_source_complete_raw"] = bool(
                SG.alarm_source_complete_from_reading(reading))
        except Exception as e:                    # noqa: BLE001
            out["alarm_source_complete_raw"] = None
            out["errors"].append(f"alarm_source_complete: {type(e).__name__}")
        try:
            out["pcs_state_raw"] = PCI.classify_pcs_state(
                reading.get("pcs_charging_flag"),
                reading.get("pcs_discharging_flag"),
                reading.get("pcs_standby_flag"),
                reading.get("pcs_running_flag"))
        except Exception as e:                    # noqa: BLE001
            out["pcs_state_raw"] = None
            out["errors"].append(f"classify_pcs_state: {type(e).__name__}")

        # ---- 4. 電表快照 ----
        snap = mc.get_snapshot()
        out["meter"] = None if snap is None else {
            k: getattr(snap, k, None) for k in (
                "power_kw", "direction", "meter_state", "demand_kw",
                "age_sec", "stale", "valid", "reason", "server_datetime")
        }

        # ---- 5. 完整一輪仲裁（OBSERVE_ONLY，executor/verifier = None）----
        obs_source, recovery, wiring = SVC.build_production_stack(
            client=client, meter_client=mc, mode=PRD.MODE_OBSERVE_ONLY)
        out["wiring"] = {"mode": wiring.mode,
                         "can_dispatch": wiring.can_dispatch,
                         "executor": wiring.sources.get("executor"),
                         "verifier": wiring.sources.get("verifier")}
        # 🔴 **必須連續觀測**，不能只取一筆。
        #    PowerClassifier 的 stable 狀態採 time-based debounce
        #    （candidate 需連續維持 debounce_sec 才正式生效），
        #    因此單次 update() 結構上不可能脫離 UNKNOWN ——
        #    單筆取樣的 grid_state=UNKNOWN 是**取樣方式的產物**，
        #    不是設備或電網異常，也不足以判定自然 Decision。
        #    這裡以 service 迴圈節奏連續餵入，直到分類器穩定為止。
        trace = []
        res = None
        for i in range(int(cycles)):
            if i:
                time.sleep(float(cycle_interval))
            res = obs_source()
            a = getattr(res, "arbitration", None) or res
            trace.append({
                "i": i, "at": _now_wall(),
                "grid_state": getattr(a, "grid_state", None),
                "tou_state": getattr(a, "tou_state", None),
                "decision": getattr(a, "fresh_decision_action", None),
                "target_kw": getattr(a, "fresh_decision_target_kw", None),
                "pcs_state": getattr(a, "pcs_state", None),
                "soc": getattr(a, "soc_percent", None),
                "meter_age": getattr(a, "fresh_meter_age_sec", None),
                "outcome": getattr(a, "outcome", None),
                "control_action": getattr(res, "control_action", None),
                "executed": bool(getattr(res, "executed", False))})
            if getattr(a, "grid_state", None) not in (None, "UNKNOWN"):
                break
        out["trace"] = trace
        # 重新取一筆與最終仲裁同時期的電表快照，作為 precheck 的新鮮度依據
        snap = mc.get_snapshot()
        out["meter"] = None if snap is None else {
            k: getattr(snap, k, None) for k in (
                "power_kw", "direction", "meter_state", "demand_kw",
                "age_sec", "stale", "valid", "reason", "server_datetime")}
        arb = getattr(res, "arbitration", None) or res
        out["result"] = {
            "executed": bool(getattr(res, "executed", False)),
            "control_action": getattr(res, "control_action", None),
            "outcome": getattr(res, "outcome", None),
            "reason": getattr(res, "reason", None),
        }
        out["arb"] = {k: getattr(arb, k, None) for k in (
            "outcome", "reason", "valid", "pcs_state", "would_action",
            "fresh_decision_action", "fresh_decision_target_kw",
            "fresh_meter_age_sec", "fresh_ess_age_sec",
            "safety_allowed", "safety_reason", "safety_check_count",
            "authority_state", "authority_reason", "direction_reason",
            "alarm_source_complete", "schedule_switch", "manual_switch",
            "grid_state", "tou_state", "soc_percent", "authorized",
            "dispatched", "authorization_state", "missing_config")}

        # ---- 6. fresh precheck（service 正式實作，非本檔重寫）----
        ok, items = SVC.live_leg_precheck(res, meter_snapshot=snap,
                                          report_session=None,
                                          config=CFG.DEFAULT_CONTROL_CONFIG)
        out["precheck_ok"] = bool(ok)
        out["precheck"] = items

        # ---- 7. 設定完整性 ----
        c = CFG.DEFAULT_CONTROL_CONFIG
        out["config"] = {"dispatch_ready": c.dispatch_ready,
                         "missing_required": list(c.missing_required()),
                         "charge_power_kw": c.charge_power_kw,
                         "discharge_power_kw": c.discharge_power_kw,
                         "max_power_kw": c.max_power_kw,
                         "dispatch_enabled": RT.DISPATCH_ENABLED}
        out["calendar_years"] = list(PRD.build_holiday_provider().known_years)

        # ---- 8. 「為何自然 Decision 不是 CHARGE」的可稽核推導 ----
        # 🔴 純查既有 27 格 Decision Matrix 與既有 SOC 門檻，
        #    不重新判定、不放寬、不製造 candidate。
        import decision_policy as DP
        pcfg = DP.PolicyConfig()
        soc = getattr(arb, "soc_percent", None)
        band = None
        if isinstance(soc, (int, float)):
            if soc >= pcfg.soc_charge_stop_pct:
                band = DP.S_HIGH
            elif soc <= pcfg.soc_discharge_stop_pct:
                band = DP.S_LOW
            else:
                band = DP.S_MID
        tou = getattr(arb, "tou_state", None)
        grid = getattr(arb, "grid_state", None)
        out["why"] = {
            "tou_state": tou, "grid_state": grid,
            "soc_percent": soc, "soc_band_if_fresh_latch": band,
            "soc_charge_stop_pct": pcfg.soc_charge_stop_pct,
            "soc_charge_resume_pct": pcfg.soc_charge_resume_pct,
            "soc_discharge_stop_pct": pcfg.soc_discharge_stop_pct,
            "soc_discharge_resume_pct": pcfg.soc_discharge_resume_pct,
            "matrix_now": DP.DECISION_MATRIX.get((tou, grid, band)),
            "matrix_if_offpeak": DP.DECISION_MATRIX.get(
                (DP.TOU_OFF_PEAK, grid, band)),
            "matrix_if_offpeak_soc_mid": DP.DECISION_MATRIX.get(
                (DP.TOU_OFF_PEAK, grid, DP.S_MID)),
        }
    finally:
        try:
            mc.stop()
        except Exception:                          # noqa: BLE001
            pass
    return out


def render(out):
    c = CFG.DEFAULT_CONTROL_CONFIG
    arb = out.get("arb") or {}
    m = out.get("meter") or {}
    rd = out.get("reading") or {}
    pc = out.get("precheck") or {}
    w = out.get("why") or {}
    print("=" * 72)
    print("  第一次 CONTROLLED SINGLE-LEG CHARGE LIVE —— FRESH READ-ONLY PRECHECK")
    print(f"  取樣時間 : {out['collected_at']}    登入 : "
          f"{'OK' if out.get('login_ok') else 'FAILED'}")
    print("=" * 72)

    print("\n-- Precheck（裁示第八節逐項）--")
    labels = [
        ("1  Meter connected", m.get("meter_state") is not None),
        ("2  Meter valid", pc.get("meter_valid")),
        ("3  Meter fresh", pc.get("meter_fresh")),
        ("4  ESS communication_ok", rd.get("communication_ok") is True),
        ("5  PCS idle (STANDBY/STOPPED)", pc.get("pcs_idle")),
        ("6  SOC 合理值（0-100）", pc.get("soc_sane")),
        # 裁示明列的條件：SOC 必須落在充電允許帶（充電閂已開啟）。
        # 這與 soc_sane 是**兩件事** —— 98% 是合理值，但充電閂閂閉。
        ("6b SOC 在充電允許帶（充電閂已開）",
         isinstance(w.get("soc_percent"), (int, float))
         and isinstance(w.get("soc_charge_resume_pct"), (int, float))
         and float(w["soc_percent"]) <= float(w["soc_charge_resume_pct"])),
        ("7  pcs_fault_flag=False", rd.get("pcs_fault_flag") is False),
        ("8  active critical alarm = 0",
         out.get("critical_active_alarms") == 0
         and out.get("alarm_unparsable_rows") == 0),
        ("9  alarm_source_complete", out.get("alarm_source_complete_raw") is True),
        ("10 schedule_switch=OFF", pc.get("schedule_off")),
        ("11 manual_switch 正確", pc.get("manual_switch_ok")),
        ("12 Control Authority=IDLE", pc.get("authority_idle")),
        ("13 無其他人正在控制", pc.get("no_other_operator")),
        ("14 report 無 active session", pc.get("no_report_session")),
        ("15 dispatch_ready=True", pc.get("dispatch_ready")),
        ("16 Production config 完整",
         not list(c.missing_required())),
        ("17 Calendar ready", pc.get("calendar_ready")),
        ("18 TOU valid", pc.get("tou_valid")),
        ("19 自然 Decision = CHARGE",
         arb.get("fresh_decision_action") == LIVE_ACTION),
        ("20 LiveLegAuthorization = CHARGE", LIVE_ACTION == "charge"),
    ]
    for name, ok in labels:
        print(f"   [{'PASS' if ok else 'FAIL'}] {name}")
    all_ok = all(ok for _, ok in labels)

    print("\n-- 觀測事實 --")
    print(f"   SOC                  : {_fmt(rd.get('soc_percent'), '.1f')} %")
    print(f"   PCS state            : {out.get('pcs_state_raw')}"
          f"   (flags C/D/S/R/F = {rd.get('pcs_charging_flag')}/"
          f"{rd.get('pcs_discharging_flag')}/{rd.get('pcs_standby_flag')}/"
          f"{rd.get('pcs_running_flag')}/{rd.get('pcs_fault_flag')})")
    print(f"   actual_active_power  : {_fmt(rd.get('actual_active_power_kw'), '.2f')} kW")
    print(f"   DC power             : {_fmt(rd.get('dc_power_kw'), '.2f')} kW")
    print(f"   schedule / manual    : {rd.get('pcs_schedule_enabled')} / "
          f"{rd.get('pcs_manual_switch')}")
    print(f"   Alarm                : rows={out.get('alarm_row_count')} "
          f"total_raw={out.get('alarm_total_raw')} "
          f"critical_active={out.get('critical_active_alarms')} "
          f"unparsable={out.get('alarm_unparsable_rows')} "
          f"source_complete={out.get('alarm_source_complete_raw')}")
    print(f"   Meter power          : {_fmt(m.get('power_kw'), '.2f')} kW "
          f"({m.get('direction')})  state={m.get('meter_state')}")
    print(f"   Meter freshness      : age={_fmt(m.get('age_sec'), '.1f')}s "
          f"stale={m.get('stale')} valid={m.get('valid')}")
    print(f"   TOU                  : {arb.get('tou_state')}")
    print(f"   Grid state           : {arb.get('grid_state')}")
    print(f"   Decision             : {arb.get('fresh_decision_action')} "
          f"target={_fmt(arb.get('fresh_decision_target_kw'))}")
    print(f"   Safety               : allowed={arb.get('safety_allowed')} "
          f"reason={arb.get('safety_reason')} checks={arb.get('safety_check_count')}")
    print(f"   Authority            : {arb.get('authority_state')} "
          f"/ {arb.get('authority_reason')}")
    print(f"   Interlock            : {arb.get('direction_reason')}")
    print(f"   Arbitration          : {arb.get('outcome')} / {arb.get('reason')}")
    print(f"   would_action         : {arb.get('would_action')}")
    print(f"   executed             : {out['result']['executed']}  "
          f"control_action={out['result']['control_action']}")
    print(f"   Wiring               : mode={out['wiring']['mode']} "
          f"can_dispatch={out['wiring']['can_dispatch']} "
          f"executor={out['wiring']['executor']} "
          f"verifier={out['wiring']['verifier']}")
    print(f"   config               : dispatch_ready={out['config']['dispatch_ready']} "
          f"missing={out['config']['missing_required']} "
          f"charge={out['config']['charge_power_kw']} kW "
          f"max={out['config']['max_power_kw']} kW "
          f"DISPATCH_ENABLED={out['config']['dispatch_enabled']}")
    print(f"   Calendar             : {out.get('calendar_years')}")
    if rd.get("_fail"):
        print(f"   ⚠ GET 失敗端點       : {rd['_fail']}")
    if out.get("errors"):
        print(f"   ⚠ 取樣例外           : {out['errors']}")

    if w:
        print("\n-- 為何自然 Decision 不是 CHARGE（查既有 27 格矩陣，未做任何放寬）--")
        print(f"   (TOU={w.get('tou_state')}, GRID={w.get('grid_state')}, "
              f"SOC band={w.get('soc_band_if_fresh_latch')}) → {w.get('matrix_now')}")
        print(f"   充電閂閉門檻          : stop>={w.get('soc_charge_stop_pct')}% / "
              f"resume<={w.get('soc_charge_resume_pct')}%"
              f"（目前 SOC {_fmt(w.get('soc_percent'), '.1f')}%）")
        print(f"   同 SOC 但 OFF_PEAK    : {w.get('matrix_if_offpeak')}")
        print(f"   OFF_PEAK 且 SOC 中段  : {w.get('matrix_if_offpeak_soc_mid')}")

    print("\n-- 觀測序列（分類器 debounce 需跨時間才會脫離 UNKNOWN）--")
    for t in (out.get("trace") or ()):
        print(f"   #{t['i']} {t['at']}  grid={t['grid_state']} tou={t['tou_state']} "
              f"decision={t['decision']} target={_fmt(t['target_kw'])} "
              f"pcs={t['pcs_state']} soc={_fmt(t['soc'], '.1f')} "
              f"outcome={t['outcome']} executed={t['executed']}")

    print("\n" + "=" * 72)
    if all_ok:
        print("  PRECHECK: ALL PASS —— 具備進入人工確認的條件。")
        print("  ⚠ 本檔不做人工確認、不建立 executor、不送任何指令。")
    else:
        print("  PRECHECK: **NOT ALL PASS** → NOT EXECUTED。")
        print("  🔴 不得建立 executor、不得送出任何 CHARGE。")
    print("=" * 72)
    return all_ok


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="第一次 CHARGE LIVE 的即時唯讀 precheck（不送任何指令）")
    ap.add_argument("--meter-wait", type=float, default=15.0,
                    help="等待第一筆有效電表快照的最長秒數")
    ap.add_argument("--cycles", type=int, default=6,
                    help="最多觀測幾輪（分類器穩定即提前結束）")
    ap.add_argument("--cycle-interval", type=float, default=10.0,
                    help="觀測輪間隔秒數（沿用 service skeleton 節奏）")
    ap.add_argument("--evidence-dir", default=EVIDENCE_DIR)
    args = ap.parse_args(argv)

    out = collect(meter_wait=args.meter_wait, cycles=args.cycles,
                  cycle_interval=args.cycle_interval)
    all_ok = render(out)

    os.makedirs(args.evidence_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.evidence_dir, f"precheck_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  evidence: {path}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
