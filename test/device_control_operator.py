# -*- coding: utf-8 -*-
"""
設備控制操作程式（device_control_operator.py）— 單一控制 + 狀態回讀驗證
========================================================================
與唯讀版 device_control_scraper.py 分離；本檔不修改唯讀版。

⚠️ 安全規則（硬性）：
  1. 每次只執行「一個」action（argparse choices 白名單）。
  2. 預設 dry-run：只印 endpoint / method / payload，不真的送出。
  3. 僅在 --execute 時才真正送出控制 API。
  4. 不重試、不批量。
  5. 控制端點採 exact-match 白名單；不含離網 / 故障復位 / sys/control 手動上下電。
  6. token 與 LOGIN_PASSWORD_PAYLOAD 只遮罩，不完整輸出。
  7. --power 僅接受 0~150（超出直接拒絕，不送 API）。
  8. 高風險 action（電池上/下電）--execute 後需二次確認輸入 YES（或帶 --yes）。
  9. 高風險 action 控制 API 只送一次，但可對狀態做輪詢驗證（最多 60 秒）。

登入：沿用 api_client.py，密文由 test/.env 的 LOGIN_PASSWORD_PAYLOAD 提供。
輸出：output/device_control_action_result.json
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from api_client import ApiClient, mask_secret
# 共用電池狀態解析（顯示與 verify / 前置檢查同一套）
from device_control_scraper import battery_power_state_from_dodi, battery_flow_state

USERNAME = "hmiUser"

_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
RESULT_PATH = os.path.join(_OUTPUT_DIR, "device_control_action_result.json")

# ---- 控制端點 ----
_AC_EP = "/client/dynamic/airConditioningControl"
_MANUAL_EP = "/schedule/config/editManualSwitch"
_VENT_EP = "/client/dynamic/dataOrControl/switchingQuantityControl"
_COOL_ON_EP = "/client/dynamic/dataOrControl/waterPumpControl/1"
_COOL_OFF_EP = "/client/dynamic/dataOrControl/waterPumpControl/0"
_BATT_EP = "/can/v1/manuallyPowerOnAndPowerOff"
_PCS_POWER_EP = "/client/dynamic/sinexcel/dataOrControl/manualControl"

# ---- 允許的控制端點（唯一 exact-match 白名單）----
_ALLOWED_CONTROL_PATHS = {
    _AC_EP, _MANUAL_EP, _VENT_EP, _COOL_ON_EP, _COOL_OFF_EP, _BATT_EP, _PCS_POWER_EP,
}
# ---- 絕不允許（雙保險）----
_FORBIDDEN_EXACT = {
    "/sys/control/manuallyPowerOn",
    "/sys/control/manuallyPowerOff",
    "/can/v1/faultRecovery",
}

# ---- verify（回讀）端點 ----
_V_SCHEDULE = "/schedule/config/getScheduleSwitch"           # schedulePlanSwitch / manualModeSwitch
_V_RUNMODE = "/client/dynamic/dataOrControl/pcs/getRunMode"  # runMode 字串
_V_PCS = "/client/dynamic/dataOrControl/pcs"                 # PCS 狀態（本帳號可能 403）
_V_AIR = "/client/dynamic/dataOrControl/air"                 # 空調狀態（本帳號 403）
_V_BCU = "/can/v1/getBcuState"                               # 電池/BCU 狀態（bool；非電池上下電）
_V_MANUAL_POWER = "/sys/control/getManuallyPowerState"       # 手動上電操作狀態（bool；非電池上下電）
_V_DODI = "/can/v1/getDOAndDIMsg"                            # 主正/主負接觸器反饋（電池上下電真正來源）
_V_MAINCTL = "/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation"  # 電池電壓/電流（充放電判斷）
_VERIFY_READ_WHITELIST = {_V_SCHEDULE, _V_RUNMODE, _V_PCS, _V_AIR, _V_BCU,
                          _V_MANUAL_POWER, _V_DODI, _V_MAINCTL}

# battery 輪詢設定
_POLL_TIMEOUT_SEC = 60
_POLL_INTERVAL_SEC = 4

# ---- action 定義（payload 皆由前端 DevTools 實抓；未列出的一律不支援）----
ACTIONS = {
    "ac_on":  {"method": "POST", "endpoint": _AC_EP,
               "payload": {"command": 1, "commandValue": 0}, "kind": "ac"},
    "ac_off": {"method": "POST", "endpoint": _AC_EP,
               "payload": {"command": 1, "commandValue": 1}, "kind": "ac"},

    "pcs_manual_on":  {"method": "PUT", "endpoint": _MANUAL_EP,
                       "payload": {"schedulePlanSwitchId": 1, "schedulePlanSwitch": 0, "manualModeSwitch": 1},
                       "kind": "pcs_manual", "expect": {"manualModeSwitch": 1}},
    "pcs_manual_off": {"method": "PUT", "endpoint": _MANUAL_EP,
                       "payload": {"schedulePlanSwitchId": 1, "schedulePlanSwitch": 1, "manualModeSwitch": 0},
                       "kind": "pcs_manual", "expect": {"manualModeSwitch": 0}},

    "vent_on":  {"method": "POST", "endpoint": _VENT_EP,
                 "payload": {"outputIndex": 5, "status": 1}, "kind": "vent"},
    "vent_off": {"method": "POST", "endpoint": _VENT_EP,
                 "payload": {"outputIndex": 5, "status": 3}, "kind": "vent"},

    "cooling_on":  {"method": "POST", "endpoint": _COOL_ON_EP, "payload": {}, "kind": "cooling"},
    "cooling_off": {"method": "POST", "endpoint": _COOL_OFF_EP, "payload": {}, "kind": "cooling"},

    "battery_power_on":  {"method": "POST", "endpoint": _BATT_EP,
                          "payload": {"command": "1"}, "kind": "battery",
                          "high_risk": True, "poll_expect": True},
    "battery_power_off": {"method": "POST", "endpoint": _BATT_EP,
                          "payload": {"command": "2"}, "kind": "battery",
                          "high_risk": True, "poll_expect": False},

    # PCS 功率控制（充/放電需 --power；payload 由 power 動態組出，activePowerSetPoint 帶正負號）
    "pcs_charge":     {"method": "POST", "endpoint": _PCS_POWER_EP,
                       "payload": None, "kind": "pcs_power"},
    "pcs_discharge":  {"method": "POST", "endpoint": _PCS_POWER_EP,
                       "payload": None, "kind": "pcs_power"},
    "pcs_stop_power": {"method": "POST", "endpoint": _PCS_POWER_EP,
                       "payload": {"param": 2}, "kind": "pcs_power"},
}

# 必填 --power 的 action
REQUIRES_POWER = {"pcs_charge", "pcs_discharge"}

# 送出前需先確認「電池已上電」的 action（否則一律阻擋）
# 註：pcs_stop_power（停止充放電）屬安全停止指令，即使電池已下電/未知/切換中也應允許 → 不納入前置檢查。
PRECHECK_BATTERY_ON = {"pcs_charge", "pcs_discharge"}
_PCS_LABEL = {"pcs_charge": "PCS 充電", "pcs_discharge": "PCS 放電",
              "pcs_stop_power": "PCS 停止充放電"}


def _pcs_active_payload(signed_power):
    """PCS 充放電 payload；activePowerSetPoint 帶正負號（本站：充電=負、放電=正）。"""
    return {
        "param": 1,
        "gridInterconnectionMode": 0,
        "energyDispatchingMode": 0,
        "activePowerControlMode": 0,
        "activePowerSetPoint": signed_power,
        "dcControlMode": None,
        "dcCurrentSetPoint": None,
        "dcPowerSetPoint": None,
        "offGridAcVoltRegulation": None,
    }


def _resolve_payload(action, spec, power):
    """
    依 action 決定實際 payload：充放電由 power 動態組出，其餘用靜態 payload。
    ⚠️ 本站實測方向：activePowerSetPoint 為「正」= 放電、「負」= 充電。
       故 pcs_charge → -abs(power)、pcs_discharge → +abs(power)（action 名稱＝實際行為）。
    """
    if action in ("pcs_charge", "pcs_discharge"):
        if power is None:
            raise RuntimeError(f"{action} 需要 --power（0~150）")
        p = int(power) if float(power).is_integer() else float(power)
        p = abs(p)
        return _pcs_active_payload(-p if action == "pcs_charge" else p)
    return spec["payload"]


def _assert_control_allowed(endpoint):
    """控制端點防呆：在絕不允許清單、或不在白名單，一律拒絕。"""
    if endpoint in _FORBIDDEN_EXACT:
        raise RuntimeError(f"[安全阻擋] 該端點屬禁止控制，拒絕：{endpoint}")
    if endpoint not in _ALLOWED_CONTROL_PATHS:
        raise RuntimeError(f"[安全阻擋] 控制端點不在允許白名單，拒絕：{endpoint}")


def _envelope(resp):
    try:
        body = resp.json()
    except ValueError:
        body = {"_non_json": resp.text[:300]}
    code = body.get("code") if isinstance(body, dict) else None
    msg = body.get("msg") if isinstance(body, dict) else None
    return code, msg, body


def _read(client, path):
    """唯讀回讀（白名單防呆）。"""
    if path not in _VERIFY_READ_WHITELIST:
        return {"_blocked": f"verify 端點不在允許清單：{path}"}
    return client.get(path)


def _flatten_air(data):
    """空調 guest/air 回傳 list → {mark: value(+unit)}。"""
    out = {}
    if isinstance(data, list) and data:
        for it in data[0].get("metricsDataVoList", []):
            mk = it.get("mark")
            if mk:
                v, u = it.get("value"), it.get("unit")
                out[mk] = f"{v} {u}".strip() if v is not None else None
    return out


def _poll_battery(client, expect_bool):
    """
    電池上下電：對『主正/主負接觸器反饋』(getDOAndDIMsg) 輪詢，最多 60 秒、每 ~4 秒一次。
    expect_bool=True → 期望「已上電」；False → 期望「已下電」。控制 API 不重送。
    """
    expected = "已上電" if expect_bool else "已下電"
    attempts = []
    start = time.time()
    ok = False
    n = 0
    while True:
        n += 1
        dodi = _read(client, _V_DODI)
        state, raw = battery_power_state_from_dodi(dodi)
        elapsed = round(time.time() - start, 1)
        attempts.append({"attempt": n, "elapsed_sec": elapsed, "state": state, "feedback": raw})
        print(f"  [poll #{n}] elapsed={elapsed}s 電池上下電狀態={state} (expect={expected})")
        if state == expected:
            ok = True
            break
        if time.time() - start >= _POLL_TIMEOUT_SEC:
            break
        time.sleep(_POLL_INTERVAL_SEC)
    return ok, attempts


def _compute_success(control_success, verify_success):
    """整體 success 判斷。"""
    if control_success is None:
        return None                      # dry-run / 未執行 / 未確認
    if not control_success:
        return False
    if verify_success is True:
        return True
    if verify_success in ("partial", "unknown"):
        return True                      # 控制成功、verify 只能部分/無法確認 → 視為成功（附 warning）
    if verify_success == "timeout":
        return False                     # 逾時未達預期
    if verify_success is False:
        return False
    return True                          # 無 verify 需求 → 以控制成功為準


def build_verify(client, action, spec, executed, power=None):
    """依 action 類型回讀狀態，回傳 (verify_dict, verify_success, warnings)。不因欄位不足而失敗。"""
    kind = spec["kind"]
    warnings = []
    verify = {"kind": kind, "values": {}, "expected": None, "matched": None,
              "verify_attempts": [], "verify_timeout_sec": None, "verify_interval_sec": None,
              "final_verify_status": None, "raw": {}}

    if kind == "pcs_manual":
        sw = _read(client, _V_SCHEDULE)
        rm = _read(client, _V_RUNMODE)
        pcs = _read(client, _V_PCS)
        verify["raw"] = {"getScheduleSwitch": sw, "getRunMode": rm, "pcs": pcs}
        vals = {"schedulePlanSwitch": None, "manualModeSwitch": None, "runMode": None,
                "pcs_status": "unknown"}
        if isinstance(sw, dict) and not sw.get("_error"):
            vals["schedulePlanSwitch"] = sw.get("schedulePlanSwitch")
            vals["manualModeSwitch"] = sw.get("manualModeSwitch")
        vals["runMode"] = rm if isinstance(rm, str) else (rm.get("runMode") if isinstance(rm, dict) else None)
        if isinstance(pcs, dict) and pcs.get("_error"):
            warnings.append(f"PCS狀態({_V_PCS}) 回 {pcs.get('_error')}，pcs_status=unknown")
        elif pcs is not None:
            vals["pcs_status"] = pcs
        verify["values"] = vals
        exp = spec.get("expect") or {}
        verify["expected"] = exp
        if exp:
            f, ev = next(iter(exp.items()))
            actual = vals.get(f)
            matched = (actual == ev) if actual is not None else None
            verify["matched"] = matched
            verify["final_verify_status"] = "matched" if matched else ("mismatch" if matched is False else "unknown")
            verify_success = matched  # True/False/None
        else:
            verify_success = None
        return verify, verify_success, warnings

    if kind == "ac":
        air = _read(client, _V_AIR)
        verify["raw"] = {"air": air}
        if isinstance(air, dict) and air.get("_error"):
            verify["values"] = {"equipmentWorkingStatus": "unknown", "workingMode": "unknown",
                                "coolingSetTemperature": "unknown", "heatingSetTemperature": "unknown",
                                "setHumidity": "unknown"}
            verify["final_verify_status"] = "partial"
            warnings.append(f"空調 verify({_V_AIR}) 回 {air.get('_error')} {air.get('msg')}，無法確認實際狀態")
            return verify, "partial", warnings
        flat = _flatten_air(air)
        verify["values"] = {k: flat.get(k, "unknown") for k in
                            ("equipmentWorkingStatus", "workingMode", "coolingSetTemperature",
                             "heatingSetTemperature", "setHumidity", "cabinetTemperature", "cabinetHumidity")}
        verify["final_verify_status"] = "readable"
        return verify, "partial", warnings  # 可讀但未主張 on/off，視為 partial

    if kind == "vent":
        # 目前無明確 verify API，狀態顯示 unknown/partial，不讓程式失敗
        verify["values"] = {"vent_status": "unknown", "switchingQuantity_outputIndex_5": "unknown"}
        verify["final_verify_status"] = "partial"
        warnings.append("進排風目前無明確 verify API，狀態顯示 unknown（partial）")
        return verify, "partial", warnings

    if kind == "cooling":
        verify["values"] = {"cooling_status": "unknown", "water_pump_status": "unknown"}
        verify["final_verify_status"] = "partial"
        warnings.append("冷卻循環目前無明確 verify API，狀態顯示 unknown（partial）")
        return verify, "partial", warnings

    if kind == "battery":
        expect_bool = spec.get("poll_expect")
        expected_state = "已上電" if expect_bool else "已下電"
        verify["expected"] = {"電池上下電狀態": expected_state}
        verify["verify_timeout_sec"] = _POLL_TIMEOUT_SEC
        verify["verify_interval_sec"] = _POLL_INTERVAL_SEC
        if executed:
            ok, attempts = _poll_battery(client, expect_bool)
            state = attempts[-1]["state"] if attempts else "未知"
            verify["verify_attempts"] = attempts
            verify["values"] = {"電池上下電狀態": state,
                                "feedback": attempts[-1]["feedback"] if attempts else None}
            verify["matched"] = ok
            verify["final_verify_status"] = "matched" if ok else "timeout"
            verify["raw"] = {"getDOAndDIMsg_state": state}
            return verify, (True if ok else "timeout"), warnings
        else:
            dodi = _read(client, _V_DODI)
            state, raw = battery_power_state_from_dodi(dodi)
            verify["values"] = {"電池上下電狀態": state, "feedback": raw}
            verify["final_verify_status"] = "single-read"
            verify["raw"] = {"getDOAndDIMsg_state": state}
            warnings.append("dry-run：僅單次讀取目前狀態，未輪詢")
            return verify, "partial", warnings

    if kind == "pcs_power":
        sw = _read(client, _V_SCHEDULE)
        rm = _read(client, _V_RUNMODE)
        pcs = _read(client, _V_PCS)
        verify["raw"] = {"getScheduleSwitch": sw, "getRunMode": rm, "pcs": pcs}
        vals = {"pcs_status": "unknown", "charge_discharge_mode": "unknown", "active_power": "unknown",
                "schedulePlanSwitch": None, "manualModeSwitch": None, "runMode": None}
        if isinstance(sw, dict) and not sw.get("_error"):
            vals["schedulePlanSwitch"] = sw.get("schedulePlanSwitch")
            vals["manualModeSwitch"] = sw.get("manualModeSwitch")
        vals["runMode"] = rm if isinstance(rm, str) else (rm.get("runMode") if isinstance(rm, dict) else None)
        if isinstance(pcs, dict) and pcs.get("_error"):
            warnings.append(f"PCS狀態({_V_PCS}) 回 {pcs.get('_error')}，active_power/charge_discharge_mode=unknown")
        elif isinstance(pcs, dict):
            vals["pcs_status"] = pcs
            vals["active_power"] = pcs.get("activePower", "unknown")
            vals["charge_discharge_mode"] = pcs.get("chargeDischargeMode", pcs.get("workMode", "unknown"))
        verify["values"] = vals
        mode = {"pcs_charge": "charge", "pcs_discharge": "discharge",
                "pcs_stop_power": "stop"}.get(action)
        verify["expected_mode"] = mode
        verify["expected_power"] = power if action in ("pcs_charge", "pcs_discharge") else None
        verify["actual"] = vals["active_power"]
        verify["matched"] = None
        verify["note"] = "verify source does not expose PCS power status yet"
        verify["final_verify_status"] = "partial"
        warnings.append("PCS 功率 verify：只讀來源未提供 active power / charge-discharge 狀態，部分欄位 unknown（partial）")
        return verify, "partial", warnings

    return verify, None, warnings


def _confirm_high_risk(action, assume_yes):
    """高風險 action --execute 前的二次確認。回傳 True 表示可送出。"""
    if assume_yes:
        print("已帶 --yes，略過互動確認（呼叫端已確認）。")
        return True
    print(f"⚠️ 高風險控制：{action} 將實際送出。請輸入 YES 確認（其他任何輸入皆取消）：")
    try:
        ans = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("未取得確認輸入（非互動環境），取消送出。")
        return False
    return ans == "YES"


def _pcs_battery_precheck(client, logged_in, action):
    """
    PCS 充放電前置檢查：電池必須為「已上電」。
    使用共用解析（getDOAndDIMsg 主正/主負接觸器反饋），非畫面字串。
    回傳 {battery_power_state: on/off/unknown, allowed: bool, reason: str}，並印出檢查結果。
    """
    label = _PCS_LABEL.get(action, action)
    if not logged_in:
        print("[ERROR] 無法確認電池上下電狀態（未登入），已取消 PCS 控制。")
        print("請先查詢設備狀態或檢查 API 回應。")
        return {"battery_power_state": "unknown", "allowed": False, "reason": "not_logged_in"}

    state, raw = battery_power_state_from_dodi(_read(client, _V_DODI))
    print("前置檢查：")
    print(f"電池上下電狀態：{state}")
    if state == "已上電":
        print(f"允許執行 {label}")
        return {"battery_power_state": "on", "allowed": True, "reason": "battery_powered_on"}
    if state == "已下電":
        print(f"已取消 {label}")
        print("[ERROR] 電池目前為已下電狀態，無法執行 PCS 充電／放電。")
        print("請先執行「電池上電」，確認上電完成後再操作。")
        return {"battery_power_state": "off", "allowed": False, "reason": "battery_not_powered_on"}
    # 切換中 / 未知 / 讀取失敗
    print(f"已取消 {label}")
    print("[ERROR] 無法確認電池上下電狀態，已取消 PCS 控制。")
    print("請先查詢設備狀態或檢查 API 回應。")
    return {"battery_power_state": "unknown", "allowed": False, "reason": "battery_state_unknown"}


def _battery_off_precheck(client, logged_in, action):
    """
    電池下電前置檢查：目前充放電狀態必須為「待機」才允許下電。
    使用共用 battery_flow_state（overview/mainControl 功率方向），非畫面字串。
    回傳 {battery_operation_state: charging/discharging/standby/unknown, allowed, reason}。
    """
    main = _read(client, _V_MAINCTL)
    state, power = battery_flow_state(main)
    print("前置檢查：")
    print(f"目前充放電狀態：{state}")
    if state == "待機":
        print("允許執行電池下電")
        return {"battery_operation_state": "standby", "allowed": True, "reason": "battery_standby"}
    if state == "充電":
        print("已取消電池下電")
        print("[ERROR] 目前仍在充電，無法執行電池下電。")
        print("請先執行「PCS 停止充放電」，確認狀態變成待機後再下電。")
        return {"battery_operation_state": "charging", "allowed": False, "reason": "battery_charging"}
    if state == "放電":
        print("已取消電池下電")
        print("[ERROR] 目前仍在放電，無法執行電池下電。")
        print("請先執行「PCS 停止充放電」，確認狀態變成待機後再下電。")
        return {"battery_operation_state": "discharging", "allowed": False, "reason": "battery_discharging"}
    # 未知 / 403 / 欄位缺
    print("已取消電池下電")
    print("[ERROR] 無法確認目前充放電狀態，已取消電池下電。")
    print("請先查詢設備狀態，確認為待機後再操作。")
    return {"battery_operation_state": "unknown", "allowed": False, "reason": "battery_state_unknown"}


def run(action, execute, power=None, assume_yes=False):
    spec = ACTIONS[action]
    endpoint, method, kind = spec["endpoint"], spec["method"], spec["kind"]
    payload = _resolve_payload(action, spec, power)
    _assert_control_allowed(endpoint)

    warnings = []

    print("=" * 60)
    print(f"action      : {action}  (kind={kind})")
    print(f"mode        : {'EXECUTE（真的送出）' if execute else 'DRY-RUN（不送出）'}")
    print(f"control_req : {method} {endpoint}")
    print(f"payload     : {json.dumps(payload, ensure_ascii=False)}")
    if power is not None:
        print(f"power       : {power} kW")

    client = ApiClient()
    token = client.login_hmi(USERNAME)
    logged_in = bool(token)

    control_response = None
    control_success = None
    do_send = False

    # 前置檢查（CLI 直呼也會經過此關卡）：
    #   PCS 充/放電 → 電池必須「已上電」；電池下電 → 目前必須「待機」（非充放電中）
    precheck = None
    if action in PRECHECK_BATTERY_ON:
        precheck = _pcs_battery_precheck(client, logged_in, action)
    elif action == "battery_power_off":
        precheck = _battery_off_precheck(client, logged_in, action)
    blocked = bool(precheck and not precheck["allowed"])

    if not logged_in:
        print("登入失敗，略過控制與驗證。")
    elif blocked:
        print("前置檢查未通過，已取消 PCS 控制（未送出任何控制 API）。")
    elif not execute:
        print("DRY-RUN：未送出控制 API。加上 --execute 才會實際送出。")
    else:
        # 高風險二次確認
        if spec.get("high_risk"):
            if not _confirm_high_risk(action, assume_yes):
                warnings.append("高風險 action 未取得 YES 確認，未送出控制 API。")
                print("已取消：未送出控制 API。")
            else:
                do_send = True
        else:
            do_send = True

    if do_send:
        url = client.base_url + endpoint
        try:
            if method == "POST":
                resp = client.session.post(url, json=payload, timeout=120)
            elif method == "PUT":
                resp = client.session.put(url, json=payload, timeout=120)
            else:
                raise RuntimeError(f"不支援的 method：{method}")
            code, msg, raw = _envelope(resp)
            control_response = {"http_status": resp.status_code, "code": code, "msg": msg, "raw": raw}
            control_success = (resp.status_code == 200) and (code in (0, 200))
            print(f"control_resp: http={resp.status_code} code={code} msg={msg} "
                  f"-> {'成功' if control_success else '失敗'}")
        except Exception as e:  # 不重試
            control_response = {"error": str(e)}
            control_success = False
            print(f"control_resp: 例外 {e}（不重試）")

    # 回讀驗證（executed=是否真的送出控制）
    if logged_in:
        verify, verify_success, vwarn = build_verify(
            client, action, spec, executed=do_send and control_success is True, power=power)
        warnings.extend(vwarn)
    else:
        verify, verify_success = {"kind": kind, "values": {}, "note": "not logged in"}, None

    # 整體 success：dry-run / 未送出 → None；前置檢查阻擋 → control_success/success 皆 False
    if blocked:
        control_success = False
        success = False
        warnings.append(f"前置檢查阻擋：{precheck.get('reason')}（未送出控制、未進入 verify 等待）")
    elif not do_send:
        success = None
    else:
        success = _compute_success(control_success, verify_success)

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "logged_in": logged_in,
        "action": action,
        "dry_run": (not execute),
        "power": power,
        "precheck": precheck,
        "blocked": blocked,
        "control_request": {"endpoint": endpoint, "method": method, "payload": payload},
        "control_response": control_response,
        "verify": verify,
        "control_success": control_success,
        "verify_success": verify_success,
        "success": success,
        "warnings": warnings,
        "auth": {"token_masked": mask_secret(token) if token else None},
    }

    try:
        with open(RESULT_PATH, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"\n結果已寫入：{RESULT_PATH}")
    except OSError as e:
        print(f"[警告] 寫入結果失敗：{e}")

    # 摘要
    print("-" * 60)
    print("[verify]")
    for k, v in (verify.get("values") or {}).items():
        print(f"  {k} = {v}")
    if verify.get("expected") is not None:
        print(f"  expected={verify.get('expected')} matched={verify.get('matched')} "
              f"status={verify.get('final_verify_status')}")
    if verify.get("verify_attempts"):
        print(f"  verify_attempts={len(verify['verify_attempts'])} "
              f"timeout={verify.get('verify_timeout_sec')}s interval={verify.get('verify_interval_sec')}s")
    print(f"control_success={control_success} | verify_success={verify_success} | success={success}")
    for w in warnings:
        print(f"  [warn] {w}")
    return record


def _valid_power(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"power 必須是數字：{v}")
    if not (0 <= f <= 150):
        raise argparse.ArgumentTypeError(f"power 超出允許範圍 0~150：{f}")
    return f


def main():
    ap = argparse.ArgumentParser(
        description="設備控制操作（低風險為主、單一 action、預設 dry-run；--execute 才送出）")
    ap.add_argument("--action", required=True, choices=list(ACTIONS.keys()),
                    help="要執行的單一 action：" + " / ".join(ACTIONS.keys()))
    ap.add_argument("--execute", action="store_true",
                    help="真的送出控制 API（不加則為 dry-run，只印不送）")
    ap.add_argument("--power", type=_valid_power, default=None,
                    help="功率 kW（0~150），供需要功率的 action 使用")
    ap.add_argument("--yes", action="store_true",
                    help="高風險 action 略過互動確認（呼叫端已確認 YES）")
    args = ap.parse_args()
    if args.action in REQUIRES_POWER and args.power is None:
        ap.error(f"--power 為 {args.action} 的必填參數（0~150）")
    run(args.action, args.execute, power=args.power, assume_yes=args.yes)


if __name__ == "__main__":
    main()
