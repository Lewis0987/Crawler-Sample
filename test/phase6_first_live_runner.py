# -*- coding: utf-8 -*-
"""
phase6_first_live_runner.py — Phase 6.9 FIRST LIVE 一次性執行器
======================================================================
使用者裁示（2026-08-31）：FIRST LIVE = CONDITIONALLY_AUTHORIZED
    全部條件 PASS  → 直接執行 FIRST LIVE CHARGE 5.0 kW，不再等第二次核准
    任何一項 FAIL → NO DISPATCH + STOP AND REPORT

流程
    Phase A  00:00~00:10  READ-ONLY ownership observation
             （覆蓋 8/28 約 00:05 曾出現外部約 80 kW 充電的時段）
    Phase B  Fresh Precheck → Natural Decision → Safety → Authority
             → Layer 1 → Final Revalidation → （全 PASS）FIRST LIVE
    Phase C  收尾狀態快照 + 完整 log / evidence

🔴 **控制路徑一律是既有 Production path**
    本檔**不自行組任何 command payload**、不呼叫 operator、不碰 API。
    真正的送出鏈完全由既有模組負責：
        SVC.run_live_leg_cli
          → PRD.LiveLegAuthorization      （一次性、綁方向、不落盤）
          → PRD.build_executor            （→ LegBoundExecutor 授權閘）
          → EXC.PcsControlExecutor        （薄 adapter）
          → device_control_operator.run   （唯一持有控制 POST 的模組）
    本檔只負責「觀測、判定要不要走、把結果記下來」。

🔴 **功率只能是 ProductionConfig 的 5.0 kW**
    本檔沒有 power 參數，也不接受任何 override。
    max_power_kw = 150.0 仍只是 Safety Gate 的指令上限，不是操作目標。

🔴 **人工確認語句**
    既有 confirm_live_leg() 仍會被呼叫並逐字驗證確認語句；
    本檔依上述書面 CONDITIONAL AUTHORIZATION 提供該語句，
    並在 log 中明確記錄「由書面授權滿足」，不是繞過該閘門。

🔴 **一次性**：本檔不建立、不續期、不重排任何排程；失敗不重試。

用法
    python phase6_first_live_runner.py                 # 正式（會送實機指令）
    python phase6_first_live_runner.py --observe-only  # 只做 Phase A，永不 dispatch
    python phase6_first_live_runner.py --observe-sec 60 --observe-interval 20
"""
import os
import io
import sys
import json
import time
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcs_auto_control_config as CFG            # noqa: E402
import pcs_auto_control_production as PRD        # noqa: E402
import pcs_auto_control_service as SVC           # noqa: E402
import pcs_auto_control_runtime as RT            # noqa: E402
import safety_gate as SG                         # noqa: E402
import control_authority as CA                   # noqa: E402
import decision_engine as DE                     # noqa: E402  ESS 新鮮度契約
import meter_client as MC                        # noqa: E402  電表新鮮度契約
import power_classifier as PC                    # noqa: E402  分類器 debounce 契約

EVIDENCE_DIR = os.path.join(os.path.dirname(HERE), "output", "phase6_live_charge")
LIVE_ACTION = "charge"

# Phase A：觀測窗口（裁示指定 00:00~00:10）
OBSERVE_SEC = 600.0
OBSERVE_INTERVAL_SEC = 20.0

# 觀測期間只要出現任何一項，即 ABORT，不進入 Phase B
A_PCS_ACTIVE = "PCS_SELF_ACTIVATED"          # 自行進入 CHARGING / DISCHARGING
A_AUTHORITY = "AUTHORITY_NOT_IDLE"
A_EXTERNAL = "EXTERNAL_CONTROL_EVIDENCE"
A_FAULT = "PCS_FAULT"
A_ALARM = "CRITICAL_ALARM_ACTIVE"
A_ALARM_SRC = "ALARM_SOURCE_INCOMPLETE"
A_SCHEDULE = "SCHEDULE_NOT_OFF"
A_MODE = "MANUAL_MODE_INCORRECT"
A_COMM = "ESS_COMMUNICATION_ABNORMAL"
A_NO_FRESH_METER = "NO_FRESH_METER_SAMPLE"
A_LASTCONTROL = "NON_PHASE6_LASTCONTROL"
A_COMM_UNHEALTHY = "COMMUNICATION_UNHEALTHY"
A_WINDOW_OVERRUN = "OBSERVATION_WINDOW_OVERRUN"
A_WINDOW_MISSED = "MISSED_OBSERVATION_WINDOW"

# 🔴 通訊健康的判準**完全沿用既有 production 契約**，不自訂新數字、不放寬：
#      ESS   —— decision_engine.EssConfig.stale_after_sec
#      Meter —— meter_client.STALE_AFTER_SEC
#    單次取樣若耗時超過 ESS 新鮮度窗口，該筆資料在回到手上時就**已經過期**，
#    不能當成即時 snapshot 使用 —— 即使 API 最後成功返回也一樣。
ESS_FRESH_SEC = DE.DEFAULT_ESS_CONFIG.stale_after_sec
METER_FRESH_SEC = MC.STALE_AFTER_SEC
# 暖機餵入間隔 —— 直接採分類器自己的 debounce 契約，不另訂數字。
PRIME_INTERVAL_SEC = PC.DEFAULT_CONFIG.debounce_sec

_log_lines = []


def say(msg=""):
    _log_lines.append(str(msg))
    try:
        print(msg)
    except Exception:                              # noqa: BLE001 —— console 編碼問題不得中斷流程
        pass


def _now():
    return datetime.datetime.now()


def _ts():
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _f(v, spec=".2f"):
    return "None" if v is None else (format(v, spec)
                                     if isinstance(v, (int, float)) else str(v))


# ======================================================================
# Phase A：READ-ONLY ownership observation
# ======================================================================
def _critical_alarms(reading):
    """與 Safety Gate `_chk_alarm` 同語意：level ∈ critical 且無法證明已恢復。"""
    rows = reading.get("alarm_rows") or ()
    crit = SG.SafetyConfig().critical_alarm_levels
    n, bad = 0, 0
    for row in rows:
        if not isinstance(row, dict):
            bad += 1
            continue
        lv = SG.CFG.as_int(row.get("level"))
        if lv is None:
            bad += 1
            continue
        if lv in crit and SG._alarm_active(row.get("alarmStatus")) is not False:
            n += 1
    return n, bad


# ======================================================================
# 絕對觀測窗口的時間數學（純函式、零 I/O、clock 由呼叫端注入）
# ======================================================================
# 🔴 刻意抽成純函式：時間數學是這支 runner 最容易出錯、也最難用實機驗證的
#    部分（600 vs 660 秒只差一個「sleep 後有沒有重新取時間」）。
#    抽出來才能在沒有設備的情況下用 mock clock 逐項驗證。
def plan_window(window_start, observe_sec, now):
    """
    bootstrap 階段：算出絕對窗口與需等待的秒數。

    回傳 (w0, w1, wait_sec, missed)。missed 非 None 即代表已錯過起點。
    """
    w0 = datetime.datetime.strptime(window_start, "%Y-%m-%d %H:%M:%S")
    w1 = w0 + datetime.timedelta(seconds=float(observe_sec))
    if now > w0:
        return w0, w1, 0.0, A_WINDOW_MISSED
    return w0, w1, (w0 - now).total_seconds(), None


def window_remaining(w1, now_after_wait):
    """
    等待結束**之後**：以當下牆鐘時間換算 Phase A 還剩多少秒。

    🔴 必須傳入等待後重新取得的時間 —— 用 bootstrap 當時的舊時間會讓
       deadline 多算掉整段等待時間（23:59 bootstrap → deadline 變成 00:11）。
    🔴 remaining <= 0 代表窗口在等待期間就已結束（時鐘跳動 / sleep 超時 /
       系統暫停），一律視為錯過窗口，不得以任何方式續跑。
    """
    remaining = (w1 - now_after_wait).total_seconds()
    if remaining <= 0:
        return remaining, A_WINDOW_MISSED
    return remaining, None


def prime_classifier(obs_source, until_wall, interval, sink=None):
    """
    Bootstrap 期間的分類器暖機。回傳最後一次觀測結果（或 None）。

    🔴 為什麼必須暖機
        PowerClassifier 的 stable 狀態採 time-based debounce —— 單次 update()
        結構上不可能脫離 UNKNOWN。而 grid=UNKNOWN 時仲裁會提早收斂，
        `authority_state` 根本不會被評估，回傳 None。
        若不暖機，Phase A 的**第一筆必然** authority=None，
        會被誤判成「擁有權異常」而 abort —— 窗口永遠通不過。

    🔴 這是暖機，不是放寬
        暖機在**窗口開啟之前**完成；窗口一開，每一筆樣本仍必須拿到
        真正的 authority 判定且為 IDLE。呼叫端會在進入 Phase A 前
        驗證暖機結果，未暖機成功一律 ABORT。

    ⚠️ 只讀：obs_source 來自 executor/verifier 皆為 None 的執行鏈。
    """
    last = None
    while _now() < until_wall:
        last = obs_source()
        arb = getattr(last, "arbitration", None) or last
        if sink is not None:
            sink.append({"at": _ts(),
                         "grid": getattr(arb, "grid_state", None),
                         "authority": getattr(arb, "authority_state", None)})
        remain = (until_wall - _now()).total_seconds()
        if remain <= 0:
            break
        time.sleep(min(float(interval), remain))
    return last


def _has_401(reading):
    """read_all 的 _fail 中是否出現需登入端點的 401。"""
    return any(isinstance(f, str) and "_error401" in f
               for f in (reading.get("_fail") or ()))


def observe_window(client, meter_client, obs_source, deadline, interval):
    """
    Phase A。回傳 (ok, anomalies, samples, relogins)。

    deadline : time.monotonic() 座標的**絕對**窗口結束點
               —— 由呼叫端依牆鐘時間換算，不是 runner_start + N。

    🔴 只讀。obs_source 來自 executor/verifier 皆為 None 的執行鏈，
       結構上不可能送出任何指令。
    """
    import charge_discharge_report as CDR
    samples, anomalies = [], []
    i = 0
    fresh_meter_seen = False
    relogins = []
    while time.monotonic() < deadline:      # 🔴 已過窗口就**不再開始**新樣本
        i += 1
        t_read0 = time.monotonic()
        reading = CDR.read_all(client)
        # 🔴 觀測長達數分鐘，期間 HMI session 可能失效（401）。
        #    Phase 6.7 已確認這幾支需登入，未登入會讓
        #    schedule / manual / mode 全變 None → 被誤判成「異常」。
        #    因此偵測到 401 就重新登入一次並重讀；**只重試有限次**，
        #    且登入只取唯讀權限，不改變任何控制判定。
        if _has_401(reading) and len(relogins) < 2:
            relogins.append({"sample": i, "at": _ts()})
            say(f"   [重新登入] 第 {i} 筆偵測到 401 → 重新登入後重讀")
            try:
                client, _tk = PRD.build_api_client(login=True)
                reading = CDR.read_all(client)
            except Exception as e:                 # noqa: BLE001
                say(f"   [重新登入失敗] {type(e).__name__}")
        ess_read_sec = time.monotonic() - t_read0
        t_obs0 = time.monotonic()
        res = obs_source()
        observe_sec = time.monotonic() - t_obs0
        arb = getattr(res, "arbitration", None) or res
        snap = meter_client.get_snapshot()
        n_crit, n_bad = _critical_alarms(reading)
        s = {
            "i": i, "at": _ts(),
            "authority": getattr(arb, "authority_state", None),
            "pcs_state": getattr(arb, "pcs_state", None),
            "pcs_flags": {k: reading.get(k) for k in (
                "pcs_charging_flag", "pcs_discharging_flag",
                "pcs_standby_flag", "pcs_running_flag", "pcs_fault_flag")},
            "lastcontrol_action": getattr(arb, "lastcontrol_action", None),
            "lastcontrol_target_kw": getattr(arb, "lastcontrol_target_kw", None),
            "schedule_switch": reading.get("pcs_schedule_enabled"),
            "manual_switch": reading.get("pcs_manual_switch"),
            "meter_kw": getattr(snap, "power_kw", None),
            "meter_age": getattr(snap, "age_sec", None),
            "meter_valid": getattr(snap, "valid", None),
            "meter_stale": getattr(snap, "stale", None),
            "ac_kw": reading.get("actual_active_power_kw"),
            "dc_kw": reading.get("dc_power_kw"),
            "soc": reading.get("soc_percent"),
            "comm_ok": reading.get("communication_ok"),
            "fault": reading.get("pcs_fault_flag"),
            "critical_alarms": n_crit, "alarm_unparsable": n_bad,
            "alarm_rows": len(reading.get("alarm_rows") or ()),
            "ess_read_sec": round(ess_read_sec, 1),
            "observe_sec": round(observe_sec, 1),
            "get_fail": list(reading.get("_fail") or ()),
            "auth_401": _has_401(reading),
            "tou": getattr(arb, "tou_state", None),
            "grid": getattr(arb, "grid_state", None),
            "decision": getattr(arb, "fresh_decision_action", None),
        }
        samples.append(s)

        # ---- 逐樣本異常判定（任一成立即記錄；只要出現過就 ABORT）----
        def hit(code, detail):
            anomalies.append({"code": code, "sample": i, "at": s["at"],
                              "detail": detail})

        if s["pcs_state"] in ("CHARGING", "DISCHARGING"):
            hit(A_PCS_ACTIVE, f"PCS 自行進入 {s['pcs_state']}"
                              f"（AC {_f(s['ac_kw'])} kW / DC {_f(s['dc_kw'])} kW）")
        if s["authority"] != CA.AUTH_IDLE:
            hit(A_AUTHORITY, f"authority={s['authority']}")
        if s["authority"] in (CA.AUTH_EXTERNAL, CA.AUTH_CONFLICT):
            hit(A_EXTERNAL, f"authority={s['authority']}")
        if s["fault"] is not False:
            hit(A_FAULT, f"pcs_fault_flag={s['fault']}")
        if n_crit:
            hit(A_ALARM, f"作用中嚴重告警 {n_crit} 筆")
        if n_bad:
            hit(A_ALARM_SRC, f"{n_bad} 列告警無法解析 level")
        if s["schedule_switch"] not in (0, "0", False):
            hit(A_SCHEDULE, f"schedule_switch={s['schedule_switch']}")
        if s["manual_switch"] not in (1, "1", True):
            hit(A_MODE, f"manual_switch={s['manual_switch']}")
        if s["comm_ok"] is not True:
            hit(A_COMM, f"communication_ok={s['comm_ok']}")
        if s["lastcontrol_action"] not in (None, "", "charge", "discharge", "stop"):
            hit(A_LASTCONTROL, f"lastcontrol_action={s['lastcontrol_action']!r}")
        # ---- 通訊健康（依既有契約，非新門檻）----
        if ess_read_sec > ESS_FRESH_SEC:
            hit(A_COMM_UNHEALTHY,
                f"ESS 取樣耗時 {ess_read_sec:.1f}s > 既有新鮮度契約 "
                f"{ESS_FRESH_SEC}s → 取得時已過期，不得視為即時 snapshot")
        if observe_sec > ESS_FRESH_SEC:
            hit(A_COMM_UNHEALTHY,
                f"仲裁觀測耗時 {observe_sec:.1f}s > {ESS_FRESH_SEC}s")
        if s["get_fail"]:
            hit(A_COMM_UNHEALTHY, f"端點失敗 {s['get_fail']}")
        if time.monotonic() > deadline:
            hit(A_WINDOW_OVERRUN,
                f"本筆樣本跨越觀測窗口結束時間（耗時 "
                f"{ess_read_sec + observe_sec:.1f}s）→ 不得據以繼續")
        if s["meter_valid"] is True and s["meter_stale"] is False:
            fresh_meter_seen = True

        say(f"   #{i:02d} {s['at']}  auth={s['authority']} pcs={s['pcs_state']} "
            f"AC={_f(s['ac_kw'])} DC={_f(s['dc_kw'])} soc={_f(s['soc'],'.1f')} "
            f"meter={_f(s['meter_kw'])}kW age={_f(s['meter_age'],'.1f')} "
            f"tou={s['tou']} grid={s['grid']} dec={s['decision']} "
            f"fault={s['fault']} crit={n_crit} "
            f"[read {ess_read_sec:.1f}s obs {observe_sec:.1f}s]")

        # 🔴 任一異常即代表 FIRST LIVE 已 BLOCKED —— 提前結束，
        #    不必再耗掉整個窗口（結論不會因為多取幾筆而改變）。
        if anomalies:
            say(f"   [提前結束] 第 {i} 筆已偵測到異常 → 停止觀測，不進入 dispatch 階段")
            break
        time.sleep(min(float(interval), max(0.0, deadline - time.monotonic())))

    if not fresh_meter_seen:
        anomalies.append({"code": A_NO_FRESH_METER, "sample": None, "at": _ts(),
                          "detail": "整段觀測期間沒有任何一筆 valid 且 fresh 的電表快照"})
    return (not anomalies), anomalies, samples, relogins


# ======================================================================
# 主流程
# ======================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase 6.9 FIRST LIVE（條件式授權，一次性）")
    ap.add_argument("--observe-sec", type=float, default=OBSERVE_SEC,
                    help="無 --window-start 時使用的相對窗口長度（乾跑用）")
    ap.add_argument("--observe-interval", type=float, default=OBSERVE_INTERVAL_SEC)
    # 🔴 絕對觀測窗口 —— 這是裁示的核心要求：
    #    要覆蓋的是「8/28 約 00:05 發生外部控制」的**絕對時間**，
    #    不是「runner 啟動後 10 分鐘」。晚啟動不得以順延補足。
    ap.add_argument("--window-start", default=None,
                    metavar="'YYYY-MM-DD HH:MM:SS'",
                    help="絕對觀測窗口起點；runner 會在此之前完成 bootstrap 並等待")
    ap.add_argument("--meter-warmup", type=float, default=10.0)
    ap.add_argument("--observe-only", action="store_true",
                    help="只做 Phase A；結構上不會進入 dispatch 階段")
    args = ap.parse_args(argv)

    started = _now()
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    stamp = started.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(EVIDENCE_DIR, f"first_live_{stamp}.log")
    json_path = os.path.join(EVIDENCE_DIR, f"first_live_{stamp}.json")
    out = {"started_at": _ts(), "observe_only": bool(args.observe_only)}

    say("=" * 74)
    say("  Phase 6.9 FIRST LIVE —— CONDITIONALLY AUTHORIZED（一次性）")
    say(f"  開始時間 : {out['started_at']}")
    say("  授權範圍 : CHARGE 5.0 kW × 1（＋既有 lifecycle 的 STOP），僅此一次")
    say("  控制路徑 : 既有 Production path，本檔不自行組任何 command")
    say("=" * 74)

    client = meter_client = None
    owned = False
    outcome = None
    try:
        # ---------- 連線（唯讀）----------
        client, token = PRD.build_api_client(login=True)
        out["login_ok"] = token is not None
        say(f"\n[連線] HMI 登入 : {'OK' if out['login_ok'] else 'FAILED'}")
        if not out["login_ok"]:
            out["result"] = "ABORTED_LOGIN_FAILED"
            say("[中止] 登入失敗 → Fail Closed，不進行任何後續動作。")
            return _finish(out, log_path, json_path, started)

        meter_client = PRD.build_meter_client(connect=True)
        time.sleep(float(args.meter_warmup))

        bundle = SVC.build_live_chain_bundle(client=client,
                                             meter_client=meter_client)

        # ---------- Bootstrap → 絕對觀測窗口 ----------
        # 🔴 觀測窗口是**絕對牆鐘時間**。bootstrap（Python 啟動、模組匯入、
        #    登入、電表訂閱）必須在窗口起點**之前**完成；若 bootstrap 結束時
        #    已經晚於起點，代表無法完整覆蓋該窗口 → 一律 ABORT，
        #    **不得**以「起點順延 10 分鐘」的方式補足。
        if args.window_start:
            now = _now()
            w0, w1, wait, missed = plan_window(args.window_start,
                                               args.observe_sec, now)
            out["window"] = {"mode": "ABSOLUTE",
                             "start": w0.strftime("%Y-%m-%d %H:%M:%S"),
                             "end": w1.strftime("%Y-%m-%d %H:%M:%S"),
                             "bootstrap_done": now.strftime("%Y-%m-%d %H:%M:%S")}
            say(f"\n[Bootstrap] 絕對觀測窗口 : {out['window']['start']}"
                f" ~ {out['window']['end']}")
            say(f"            bootstrap 完成 : {now:%Y-%m-%d %H:%M:%S}")
            if missed:
                late = (now - w0).total_seconds()
                out["window"]["late_sec"] = round(late, 1)
                out["result"] = "ABORTED_" + A_WINDOW_MISSED
                say(f"\n[中止] {A_WINDOW_MISSED}：bootstrap 結束時已晚於窗口起點 "
                    f"{late:.0f} s，無法完整覆蓋 "
                    f"{out['window']['start']} ~ {out['window']['end']}。")
                say("       依裁示不得以順延方式補足觀測窗口 → NO DISPATCH。")
                return _finish(out, log_path, json_path, started)
            say(f"            等待 {wait:.0f} s 至窗口起點，期間持續暖機分類器"
                f"（唯讀觀測，結構上不可能 dispatch）")
            prime = []
            prime_last = prime_classifier(bundle.observe, w0,
                                          PRIME_INTERVAL_SEC, sink=prime)
            out["window"]["prime_samples"] = prime
            parb = getattr(prime_last, "arbitration", None) or prime_last
            pg = getattr(parb, "grid_state", None)
            pa = getattr(parb, "authority_state", None)
            say(f"            暖機 {len(prime)} 次 → grid={pg} authority={pa}")
            # 🔴 窗口一開就必須拿得到真正的判定。暖機沒成功代表通訊或
            #    分類器尚未就緒 —— 那不是「稍後就會好」，是不具備觀測條件。
            if pg in (None, "UNKNOWN") or pa is None:
                out["window"]["prime_failed"] = {"grid": pg, "authority": pa}
                out["result"] = "ABORTED_" + A_COMM_UNHEALTHY
                say(f"\n[中止] 分類器／仲裁在窗口開啟前未就緒"
                    f"（grid={pg} authority={pa}）→ 無法取得有效觀測 → NO DISPATCH。")
                return _finish(out, log_path, json_path, started)
            # 🔴 **等待後重新取得牆鐘時間**再換算 deadline。
            #    用 bootstrap 當時的舊時間會把整段等待也算進窗口
            #    （23:59 bootstrap → 600 s 變成 660 s → deadline 變 00:11）。
            now2 = _now()
            remaining, missed2 = window_remaining(w1, now2)
            out["window"]["window_open_at"] = now2.strftime("%Y-%m-%d %H:%M:%S")
            out["window"]["effective_sec"] = round(remaining, 1)
            say(f"            窗口開啟於 : {now2:%Y-%m-%d %H:%M:%S}"
                f"  → Phase A 實際可用 {remaining:.1f} s"
                f"（絕對結束時間 {out['window']['end']}）")
            if missed2:
                out["result"] = "ABORTED_" + A_WINDOW_MISSED
                say(f"\n[中止] {A_WINDOW_MISSED}：等待結束時窗口已關閉"
                    f"（remaining {remaining:.1f} s ≤ 0）→ NO DISPATCH。")
                return _finish(out, log_path, json_path, started)
            deadline = time.monotonic() + remaining
        else:
            out["window"] = {"mode": "RELATIVE", "length_sec": args.observe_sec}
            # 乾跑同樣需要暖機，否則第一筆 authority 必為 None。
            prime = []
            until = _now() + datetime.timedelta(seconds=PRIME_INTERVAL_SEC * 4)
            prime_last = prime_classifier(bundle.observe, until,
                                          PRIME_INTERVAL_SEC, sink=prime)
            parb = getattr(prime_last, "arbitration", None) or prime_last
            out["window"]["prime_samples"] = prime
            say(f"[Bootstrap] 暖機 {len(prime)} 次 → "
                f"grid={getattr(parb, 'grid_state', None)} "
                f"authority={getattr(parb, 'authority_state', None)}")
            deadline = time.monotonic() + float(args.observe_sec)

        # ---------- Phase A ----------
        say(f"\n[Phase A] READ-ONLY ownership observation "
            f"（{out['window']['mode']} 窗口 / 每 {args.observe_interval:.0f} s 一筆）")
        say("          覆蓋 8/28 約 00:05 曾出現外部約 80 kW 充電的時段")
        ok_a, anomalies, samples, relogins = observe_window(
            client, meter_client, bundle.observe,
            deadline, args.observe_interval)
        out["observation"] = {"ok": ok_a, "samples": samples,
                              "anomalies": anomalies, "relogins": relogins,
                              "from": samples[0]["at"] if samples else None,
                              "to": samples[-1]["at"] if samples else None}
        say(f"\n[Phase A] 結果 : {'PASS' if ok_a else 'FAIL'}"
            f"（樣本 {len(samples)} 筆，異常 {len(anomalies)} 項）")
        for a in anomalies:
            say(f"          ⚠ {a['code']}  #{a['sample']} {a['at']}  {a['detail']}")
        if not ok_a:
            out["result"] = "ABORTED_OWNERSHIP_OBSERVATION"
            say("\n[中止] 觀測期間偵測到異常／外部控制 → FIRST LIVE BLOCKED，不送任何指令。")
            return _finish(out, log_path, json_path, started)

        if args.observe_only:
            out["result"] = "OBSERVE_ONLY_COMPLETED"
            say("\n[結束] --observe-only：不進入 dispatch 階段。")
            return _finish(out, log_path, json_path, started)

        # ---------- Phase B ----------
        say("\n[Phase B] 取得 Control Ownership → Fresh Precheck → …")
        owned, why = SVC.acquire_ownership(role="first-live")
        out["ownership_mutex"] = {"acquired": owned, "reason": why}
        say(f"          Control Mutex : {'已取得' if owned else '未取得'}（{why}）")
        if not owned:
            out["result"] = "ABORTED_NO_CONTROL_OWNERSHIP"
            say("[中止] 未取得 PCS Control Ownership → Fail Closed。")
            return _finish(out, log_path, json_path, started)

        intent = PRD.LiveLegIntent(LIVE_ACTION)
        phrase = PRD.confirmation_phrase(intent)
        say(f"          確認語句 : {phrase}")
        say("          🔴 由使用者 2026-08-31 書面 CONDITIONAL AUTHORIZATION 滿足；"
            "既有 confirm_live_leg() 仍逐字驗證，未被繞過。")
        out["confirmation"] = {"phrase": phrase,
                               "satisfied_by": "WRITTEN_CONDITIONAL_AUTHORIZATION_20260831"}

        outcome = SVC.run_live_leg_cli(
            LIVE_ACTION, bundle,
            prompt=(lambda _p: phrase),
            operator_run=None,                     # → 既有 build_live_operator_run()
            meter_snapshot_source=meter_client.get_snapshot,
            report_session_source=(lambda: None),
            verbose=True)
        out["outcome"] = outcome.as_dict()
        say(f"\n[Phase B] {outcome}")

        # ---------- Phase C ----------
        say("\n[Phase C] 收尾狀態快照")
        import charge_discharge_report as CDR
        time.sleep(3.0)
        post = CDR.read_all(client)
        res = bundle.observe()
        arb = getattr(res, "arbitration", None) or res
        n_crit, _ = _critical_alarms(post)
        out["final"] = {
            "pcs_state": getattr(arb, "pcs_state", None),
            "authority": getattr(arb, "authority_state", None),
            "ac_kw": post.get("actual_active_power_kw"),
            "dc_kw": post.get("dc_power_kw"),
            "soc": post.get("soc_percent"),
            "fault": post.get("pcs_fault_flag"),
            "critical_alarms": n_crit,
            "alarm_rows": len(post.get("alarm_rows") or ()),
        }
        for k, v in out["final"].items():
            say(f"          {k:<16}: {v}")
        out["result"] = ("FIRST_LIVE_COMPLETED"
                         if outcome.dispatch_count else "NO_DISPATCH")
    except Exception as e:                          # noqa: BLE001 —— 記錄，不重試
        out["result"] = "ABORTED_EXCEPTION"
        out["exception"] = f"{type(e).__name__}: {e}"
        say(f"\n[例外] {type(e).__name__}: {e} —— 只記錄，不重試、不補送任何指令。")
    finally:
        if meter_client is not None:
            try:
                meter_client.stop()
            except Exception:                       # noqa: BLE001
                pass
        if owned:
            SVC.release_ownership()

    return _finish(out, log_path, json_path, started, outcome)


def _finish(out, log_path, json_path, started, outcome=None):
    out["ended_at"] = _ts()
    out["elapsed_sec"] = round((_now() - started).total_seconds(), 1)
    out["dispatch_count"] = getattr(outcome, "dispatch_count", 0)
    out["stop_count"] = getattr(outcome, "stop_count", 0)
    out["dispatch_enabled"] = RT.DISPATCH_ENABLED
    out["mode_after"] = PRD.MODE_OBSERVE_ONLY
    say("\n" + "=" * 74)
    say(f"  結果 : {out.get('result')}")
    say(f"  實機 CHARGE / STOP : {out['dispatch_count']} / {out['stop_count']}")
    say(f"  DISPATCH_ENABLED   : {out['dispatch_enabled']}（模組層，未變）")
    say(f"  耗時 : {out['elapsed_sec']} s")
    say("=" * 74)
    io.open(log_path, "w", encoding="utf-8").write("\n".join(_log_lines) + "\n")
    io.open(json_path, "w", encoding="utf-8").write(
        json.dumps(out, ensure_ascii=False, indent=2, default=str))
    try:
        print(f"\nlog      : {log_path}\nevidence : {json_path}")
    except Exception:                               # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                               # noqa: BLE001
        pass
    raise SystemExit(main())
