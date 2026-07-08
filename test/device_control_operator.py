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
_V_BCU = "/can/v1/getBcuState"                               # 電池/BCU 狀態（bool）
_V_MANUAL_POWER = "/sys/control/getManuallyPowerState"       # 手動上電狀態（bool）
_VERIFY_READ_WHITELIST = {_V_SCHEDULE, _V_RUNMODE, _V_PCS, _V_AIR, _V_BCU, _V_MANUAL_POWER}

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


def _pcs_active_payload(signed_power):
    """PCS 充放電 payload；activePowerSetPoint 帶正負號（充+、放-）。"""
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
    """依 action 決定實際 payload：充放電由 power 動態組出，其餘用靜態 payload。"""
    if action in ("pcs_charge", "pcs_discharge"):
        if power is None:
            raise RuntimeError(f"{action} 需要 --power（0~150）")
        p = int(power) if float(power).is_integer() else float(power)
        p = abs(p)
        return _pcs_active_payload(p if action == "pcs_charge" else -p)
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
    """電池上下電：對 getBcuState 輪詢，最多 60 秒、每 ~4 秒一次。控制 API 不重送。"""
    attempts = []
    start = time.time()
    ok = False
    n = 0
    while True:
        n += 1
        val = _read(client, _V_BCU)
        elapsed = round(time.time() - start, 1)
        attempts.append({"attempt": n, "elapsed_sec": elapsed, "getBcuState": val})
        print(f"  [poll #{n}] elapsed={elapsed}s getBcuState={val} (expect={expect_bool})")
        if isinstance(val, bool) and val == expect_bool:
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
        verify["expected"] = {"getBcuState": expect_bool}
        verify["verify_timeout_sec"] = _POLL_TIMEOUT_SEC
        verify["verify_interval_sec"] = _POLL_INTERVAL_SEC
        # 豐富欄位無對應 API → unknown（不因此失敗）
        base_vals = {"battery_status": "unknown", "current_soc": "unknown",
                     "current_voltage": "unknown", "current_current": "unknown",
                     "current_power": "unknown", "main_positive_feedback": "unknown",
                     "main_negative_feedback": "unknown", "ring_current_feedback": "unknown"}
        if executed:
            ok, attempts = _poll_battery(client, expect_bool)
            verify["verify_attempts"] = attempts
            last = attempts[-1]["getBcuState"] if attempts else None
            base_vals["battery_status"] = last
            verify["values"] = base_vals
            verify["matched"] = ok
            verify["final_verify_status"] = "matched" if ok else "timeout"
            verify["raw"] = {"getManuallyPowerState": _read(client, _V_MANUAL_POWER)}
            warnings.append("電池狀態以 getBcuState 推斷（極性請自行確認）；其餘欄位無 API 對應顯示 unknown")
            return verify, (True if ok else "timeout"), warnings
        else:
            cur = _read(client, _V_BCU)
            base_vals["battery_status"] = cur
            verify["values"] = base_vals
            verify["final_verify_status"] = "single-read"
            verify["raw"] = {"getBcuState": cur, "getManuallyPowerState": _read(client, _V_MANUAL_POWER)}
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

    if not logged_in:
        print("登入失敗，略過控制與驗證。")
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

    # 整體 success：dry-run / 未送出 → None
    if not do_send:
        success = None
    else:
        success = _compute_success(control_success, verify_success)

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "logged_in": logged_in,
        "action": action,
        "dry_run": (not execute),
        "power": power,
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
