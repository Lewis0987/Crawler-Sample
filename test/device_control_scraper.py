# -*- coding: utf-8 -*-
"""
設備控制頁籤 scraper —（登入後）只讀分析，嚴禁送出控制命令
==============================================================
狀態摘要盡量對齊 HMI 畫面；狀態值取自 guest/envCon 可讀來源，authed 403 端點僅作
「控制驗證」獨立顯示，不覆蓋狀態。

⚠️ 安全：只呼叫只讀白名單 GET；控制類 API 僅記錄不呼叫（_assert_readonly 防呆）。
輸出：device_control_readonly.json
執行：python device_control_scraper.py
"""

import os
import json
from datetime import datetime

from api_client import ApiClient
from dashboard_scraper import _flatten_metrics, _fmt_metric  # 重用 envCon 攤平 + i18n 中文化

USERNAME = "hmiUser"
PASSWORD = "hmiUser123"  # 相容用；實際密碼/密文由 api_client 依 .env 決定（SM2 或 payload）

_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
JSON_PATH = os.path.join(_OUTPUT_DIR, "device_control_readonly.json")

# ---- 只讀「狀態顯示」端點（皆可讀）----
READONLY_ENDPOINTS = {
    "guest_air":        "/hmiGuest/unauthorizedAccess/envCon/air",
    "guest_pcs":        "/hmiGuest/unauthorizedAccess/envCon/pcs",
    "guest_ttyS0":      "/hmiGuest/unauthorizedAccess/envCon/ttyS0",   # 進排風 / 水泵 數位訊號
    "guest_water":      "/hmiGuest/unauthorizedAccess/envCon/water",   # 冷卻循環/水系統告警
    "overview_mainControl": "/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation",  # 電池系統 SOC/電壓/電流
    "getRunMode":       "/client/dynamic/dataOrControl/pcs/getRunMode",
    "getScheduleSwitch":"/schedule/config/getScheduleSwitch",
    "getBcuState":      "/can/v1/getBcuState",
    "getDOAndDIMsg":    "/can/v1/getDOAndDIMsg",
    "getManuallyPowerState": "/sys/control/getManuallyPowerState",
}

# ---- 「控制驗證」端點（authed，需操作權限；本帳號 403）----
VERIFY_ENDPOINTS = {
    "空調狀態(authed驗證)": "/client/dynamic/dataOrControl/air",
    "PCS狀態(authed驗證)":  "/client/dynamic/dataOrControl/pcs",
}

# ---- 控制類 API（僅記錄，一律不呼叫）----
CONTROL_APIS = [
    {"path": "/client/dynamic/airConditioningControl",             "method": "POST", "用途": "空調控制"},
    {"path": "/client/dynamic/dataOrControl/manualControl",        "method": "POST", "用途": "手動控制"},
    {"path": "/client/dynamic/sinexcel/dataOrControl/manualControl","method": "POST", "用途": "手動控制(Sinexcel)"},
    {"path": "/schedule/config/editManualSwitch",                  "method": "PUT",  "用途": "修改手動開關"},
    {"path": "/schedule/config/editScheduleSwitch",                "method": "PUT",  "用途": "修改排程開關"},
    {"path": "/can/v1/manuallyPowerOnAndPowerOff",                 "method": "POST", "用途": "手動開/關機"},
    {"path": "/sys/control/manuallyPowerOn",   "method": "GET(控制)", "用途": "手動上電（GET 但屬控制，禁呼叫）"},
    {"path": "/sys/control/manuallyPowerOff",  "method": "GET(控制)", "用途": "手動斷電（GET 但屬控制，禁呼叫）"},
    {"path": "/can/v1/faultRecovery",          "method": "GET(控制)", "用途": "故障復歸（GET 但屬控制，禁呼叫）"},
]

_READONLY_ALLOWLIST = set(READONLY_ENDPOINTS.values()) | set(VERIFY_ENDPOINTS.values())
_CONTROL_BLOCKLIST = {c["path"] for c in CONTROL_APIS}

# ---- 對照表 ----
# PCS 控制模式（getRunMode）：智慧模式 / 手動（對應 HMI 控制模式黃框）
_CONTROL_MODE_MAP = {"manual": "手動", "schedule": "智慧模式", "auto": "智慧模式",
                     "smart": "智慧模式", "intelligent": "智慧模式"}
# 屬「智慧模式」的 getRunMode 值（此時才顯示排程開關狀態）
_SMART_RUNMODES = {"schedule", "auto", "smart", "intelligent"}


def _battery_power_state(v):
    """電池上下電狀態 mapping → 已上電 / 已下電 / 未知。"""
    if isinstance(v, bool):
        return "已上電" if v else "已下電"
    s = str(v).strip().lower()
    if s in ("開", "true", "1", "on", "poweron", "已上電"):
        return "已上電"
    if s in ("關", "false", "0", "off", "poweroff", "已下電"):
        return "已下電"
    return "未知"
# PCS 故障/告警類旗標（值非「正常」時列出）
_PCS_ALARM_FLAGS = {"systemFaultStatus": "故障", "systemFailedStatus": "故障", "systemAlarmStatus": "告警"}
# PCS 布林旗標（值為「是」時列出）
_PCS_BOOL_FLAGS = {"systemStandbyStatus": "待機", "systemChargingStatus": "充電中",
                   "systemDischargingStatus": "放電中", "systemBootingStatus": "啟動中"}
_TRUE_SET = {"是", True, "true", "True"}
# 空調告警/故障類 mark（值非「正常」即視為異常）
_AIR_FAULT_MARKS = ("cabinetWithinTemperatureHigh", "temperatureHigh", "coilAntifreeze",
                    "frostProbeMalfunction", "condensationTemperatureProbeFault",
                    "cabinetWithinTemperatureProbeFault", "humidityProbeFault", "highVoltageAlarm")
# DO/DI 反饋/接觸器 mark 關鍵字 → 中文（由實抓 getDOAndDIMsg 的 name 對照確認）
# 順序：feedback 需在 contactor 之前（feedback 名稱含 contactor 字串，先比對較specific者）
_FEEDBACK_MARKS = [
    ("main.positive.contactor.feedback", "主正反饋"),
    ("main.positive.contactor", "主正"),
    ("main.negative.contactor.feedback", "主負反饋"),
    ("main.negative.contactor", "主負"),
    ("circulation.contactor", "環流"),
]


def _assert_readonly(path):
    if path in _CONTROL_BLOCKLIST:
        raise RuntimeError(f"[安全阻擋] 該路徑屬控制 API，拒絕呼叫：{path}")
    if path not in _READONLY_ALLOWLIST:
        raise RuntimeError(f"[安全阻擋] 路徑不在只讀白名單，拒絕呼叫：{path}")


def _fetch(client, endpoints):
    result = {}
    for name, path in endpoints.items():
        try:
            _assert_readonly(path)
        except RuntimeError as e:
            print(f"  {e}")
            result[name] = {"_blocked": True, "path": path}
            continue
        result[name] = client.get(path)
    return result


def _v(flat, mark):
    """取攤平後的（已中文化）value。"""
    return flat.get(mark, {}).get("value")


def _mv(flat, mark):
    """取「value unit」字串；無此 mark 回 None。"""
    return _fmt_metric(flat[mark]) if mark in flat else None


def _onoff01(v, on="開啟", off="關閉"):
    """ttyS0 類 0/1 → 中文。"""
    if v in (1, "1"):
        return on
    if v in (0, "0"):
        return off
    return f"原始值 {v}"


# ---------------- 各區塊解析（值有就填，沒有省略 / 標暫無資料）----------------
def _pcs_raw(pcs_data):
    """取 guest PCS 的原始（未中文化）mark→value，供工作模式判斷。"""
    raw = {}
    if isinstance(pcs_data, list) and pcs_data:
        for m in pcs_data[0].get("metricsDataVoList", []):
            if isinstance(m, dict) and m.get("mark"):
                raw[m["mark"]] = m.get("value")
    return raw


def _pcs_work_mode(raw):
    """
    PCS 工作模式（只回目前選中的單一模式）：交流有功 / 直流恆流 / 直流恆功率 / 離網。
    來源：guest PCS 的 systemOffGridStatus / energyDispatchingMode / dcControlMode。
    """
    grid = str(raw.get("systemGridTiedStatus", ""))
    if str(raw.get("systemOffGridStatus", "")).endswith("true") or grid.endswith(("offGrid", "offGird")):
        return "離網"
    disp = str(raw.get("energyDispatchingMode", ""))
    if disp.endswith("ac"):
        return "交流有功"
    if disp.endswith("dc"):
        dcm = str(raw.get("dcControlMode", "")).lower()
        if "current" in dcm:
            return "直流恆流"
        if "power" in dcm or "watt" in dcm:
            return "直流恆功率"
        return "直流"
    return "暫無資料"


def parse_pcs(pcs_data, schedule, runmode):
    """HMI 左側 [PCS] 區塊：狀態 / 控制模式 / 工作模式 / 手動模式開關。"""
    flat = _flatten_metrics(pcs_data) if pcs_data else {}
    raw = _pcs_raw(pcs_data)
    out = {}

    # PCS狀態：開關機 → 故障/告警 → 併網/離網 → 布林旗標
    parts = []
    if _v(flat, "systemOnOrOffStatus"):
        parts.append(str(_v(flat, "systemOnOrOffStatus")))
    for mk, label in _PCS_ALARM_FLAGS.items():
        val = _v(flat, mk)
        if val and val not in ("正常", "否"):
            parts.append(label)
    if _v(flat, "systemGridTiedStatus"):
        parts.append(str(_v(flat, "systemGridTiedStatus")))
    for mk, label in _PCS_BOOL_FLAGS.items():
        if _v(flat, mk) in _TRUE_SET:
            parts.append(label)
    out["PCS狀態"] = " / ".join(parts) if parts else "暫無資料"

    # PCS控制模式（智慧 / 手動）← getRunMode
    if isinstance(runmode, str):
        out["PCS控制模式"] = _CONTROL_MODE_MAP.get(runmode, runmode)

    # PCS工作模式（單一選中）
    out["PCS工作模式"] = _pcs_work_mode(raw) if raw else "暫無資料"

    # PCS控制模式 已在上方處理；手動模式開關 ← getScheduleSwitch.manualModeSwitch
    if isinstance(schedule, dict) and "manualModeSwitch" in schedule:
        out["PCS手動模式開關"] = "已啟用" if schedule.get("manualModeSwitch") in (1, "1") else "已停用"

    # PCS離網模式 ← guest PCS systemOffGridStatus（目前是否離網運行）
    off = str(raw.get("systemOffGridStatus", ""))
    if off:
        out["PCS離網模式"] = "啟用（離網）" if off.endswith("true") else "關閉（非離網）"
    else:
        out["PCS離網模式"] = "尚未找到對應狀態欄位"

    # 排程開關狀態 ← getScheduleSwitch.schedulePlanSwitch；僅「智慧模式」才顯示
    is_smart = isinstance(runmode, str) and runmode.strip().lower() in _SMART_RUNMODES
    if is_smart and isinstance(schedule, dict) and "schedulePlanSwitch" in schedule:
        out["排程開關狀態"] = "開" if schedule.get("schedulePlanSwitch") in (1, "1") else "關"
    return out


def parse_battery(main, manual_power, dodi):
    """
    HMI 右上 [電池] 區塊，固定順序：
      電池上下電狀態 → 當前狀態/SOC/電壓/電流/功率 → 主正反饋/主正/主負反饋/主負/環流。
    """
    out = {}
    # 1) 電池上下電狀態 ← getManuallyPowerState（manuallyPowerOnAndPowerOff 的狀態回讀）
    out["電池上下電狀態"] = _battery_power_state(manual_power)
    # 2) 電池系統指標 ← overview/mainControlCollectsInformation
    if isinstance(main, dict):
        v = main.get("rackTotalBatteryVoltage")
        a = main.get("rackElectricCurrent")
        soc = main.get("rackSoc")
        out["當前狀態"] = "暫無資料" if a is None else ("待機" if abs(a) < 0.5 else "運轉")
        if soc is not None:
            out["當前SOC"] = f"{soc} {main.get('rackSocUnit', '%')}"
        if v is not None:
            out["當前電壓"] = f"{v} {main.get('rackTotalBatteryVoltageUnit', 'V')}"
        if a is not None:
            out["當前電流"] = f"{a} {main.get('rackElectricCurrentUnit', 'A')}"
        if v is not None and a is not None:
            out["當前功率"] = f"{v * a / 1000:.3f} kW"   # DC 功率 = 電壓×電流
    else:
        out["當前狀態"] = "暫無資料"
    # 3) 反饋/接觸器（主正反饋/主正/主負反饋/主負/環流）← getDOAndDIMsg
    out.update(parse_feedback(dodi))
    return out


def parse_air(air_data):
    flat = _flatten_metrics(air_data) if air_data else {}
    if not flat:
        return {"空調狀態": "暫無資料"}
    out = {}
    ews = _v(flat, "equipmentWorkingStatus")
    if ews is not None:
        out["空調開關"] = "開" if ews in ("運轉中", "type.attr.run", "run") else "關"
    if _v(flat, "workingMode") is not None:
        out["空調目前狀態"] = str(_v(flat, "workingMode"))
    for label, mk in (("空調製冷設定", "coolingSetTemperature"),
                      ("空調製熱設定", "heatingSetTemperature"),
                      ("空調濕度設定", "setHumidity"),
                      ("空調櫃內溫度", "cabinetTemperature"),
                      ("空調櫃內濕度", "cabinetHumidity")):
        val = _mv(flat, mk)
        if val is not None:
            out[label] = val
    # 空調故障：任一告警 mark 值非「正常」即列出
    faults = [mk for mk in _AIR_FAULT_MARKS if _v(flat, mk) not in (None, "正常")]
    if any(mk in flat for mk in _AIR_FAULT_MARKS):
        out["空調故障"] = "正常" if not faults else "異常：" + "、".join(faults)
    return out


def parse_vent(ttys0_data):
    flat = _flatten_metrics(ttys0_data) if ttys0_data else {}
    if "airExhaust" not in flat:
        return {"進排風執行狀態": "暫無資料"}
    out = {"進排風執行狀態": _onoff01(_v(flat, "airExhaust"), on="運轉/開啟", off="停止/關閉")}
    if "airFail" in flat:
        out["進排風故障"] = _onoff01(_v(flat, "airFail"), on="故障", off="正常")
    return out


def parse_cooling(ttys0_data, water_data=None):
    flat = _flatten_metrics(ttys0_data) if ttys0_data else {}
    out = {}
    if "waterPumpOn" in flat:
        pump = _v(flat, "waterPumpOn")
        out["冷卻循環執行狀態"] = _onoff01(pump, on="運轉", off="停止")
        out["冷卻循環水泵狀態"] = _onoff01(pump, on="開啟", off="關閉")
    else:
        out["冷卻循環水泵狀態"] = "暫無資料"
    # 冷卻循環故障 ← envCon/water 的 alarm（正常/異常）
    wf = _flatten_metrics(water_data) if water_data else {}
    if "alarm" in wf:
        out["冷卻循環故障"] = _v(wf, "alarm")
    return out


def parse_feedback(dodi):
    """從 getDOAndDIMsg 解析反饋/接觸器開關（主正反饋/主正/主負反饋/主負/環流）。0→斷開 1→閉合。"""
    if not isinstance(dodi, list):
        return {}
    used, out = set(), {}
    for kw, label in _FEEDBACK_MARKS:
        for i, it in enumerate(dodi):
            if i in used or not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("mark") or ""
            if kw in name:
                out[label] = _onoff01(it.get("value"), on="閉合", off="斷開")
                used.add(i)
                break
    return out


def parse_others(bcu, manual_power, dodi):
    out = {}
    if isinstance(bcu, bool):
        out["BCU狀態"] = f"{'在線' if bcu else '離線'}（原始:{str(bcu).lower()}）"
    if isinstance(manual_power, bool):
        out["手動上電狀態"] = f"{'已上電' if manual_power else '未上電/停機'}（原始:{str(manual_power).lower()}）"
    # DO/DI 原始訊號全列（0→斷開 / 1→閉合），供比對其餘未命名訊號
    if isinstance(dodi, list):
        sigs = [f"{(it.get('name') or it.get('mark') or '?')}={_onoff01(it.get('value'), on='閉合', off='斷開')}"
                for it in dodi if isinstance(it, dict)]
        out["DO/DI原始訊號"] = "；".join(sigs) if sigs else "暫無資料"
    return out


def summarize_status(rd):
    """整合各區塊為結構化狀態摘要。"""
    return {
        "PCS": parse_pcs(rd.get("guest_pcs"), rd.get("getScheduleSwitch"), rd.get("getRunMode")),
        "電池": parse_battery(rd.get("overview_mainControl"), rd.get("getManuallyPowerState"),
                             rd.get("getDOAndDIMsg")),
        "空調": parse_air(rd.get("guest_air")),
        "進排風": parse_vent(rd.get("guest_ttyS0")),
        "冷卻循環": parse_cooling(rd.get("guest_ttyS0"), rd.get("guest_water")),
        # 【其他】仍保留於 JSON（除錯用），但不在 console 摘要顯示
        "其他": parse_others(rd.get("getBcuState"), rd.get("getManuallyPowerState"),
                             rd.get("getDOAndDIMsg")),
    }


def summarize_verify(vd):
    out = {}
    for name in VERIFY_ENDPOINTS:
        block = vd.get(name)
        if isinstance(block, dict) and block.get("_error"):
            out[name] = f"verify API {block.get('_error')}（無法用該端點確認）"
        elif isinstance(block, dict) and block.get("_blocked"):
            out[name] = "已被安全阻擋"
        elif block is None:
            out[name] = "無回應"
        else:
            out[name] = "可用"
    return out


def print_summary(logged_in, status):
    """console 只印 [PCS][電池][空調][進排風][冷卻循環]；
    【其他】(DO/DI 原始) 與【控制驗證端點】(verify 403) 僅存於 output JSON，不印。"""
    print("=" * 56)
    print(f"設備控制（只讀）｜登入：{'成功' if logged_in else '失敗/未登入'}")
    for section in ("PCS", "電池", "空調", "進排風", "冷卻循環"):
        print(f"\n[{section}]")
        for label, value in status.get(section, {}).items():
            if label.startswith("_"):
                continue
            print(f"  {label}：{value}")


def save_json(record, path=JSON_PATH):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"\n只讀資料已寫入：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 JSON 失敗：{e}")


def main():
    client = ApiClient()
    print(f"嘗試登入（{USERNAME}）…")
    # log=False：抑制 api_client 內部的敏感登入輸出（密文 / accessToken / SM2 來源）
    # [ENV] loaded 仍會由 _load_dotenv 印出；以下只印不含機密的最小登入結果
    token, dbg = client.login_hmi(USERNAME, return_debug=True, log=False)
    print(f"HMI login: user={USERNAME}")
    print("HMI login success" if token
          else f"HMI login failed: code={dbg.get('code')} msg={dbg.get('msg')}")

    rd = _fetch(client, READONLY_ENDPOINTS)     # 狀態來源
    vd = _fetch(client, VERIFY_ENDPOINTS)       # 控制驗證端點（authed，可能 403）

    status = summarize_status(rd)
    verify_summary = summarize_verify(vd)

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "logged_in": bool(token),
        "status_summary": status,               # 對齊 HMI 的可讀狀態
        "readonly_data": rd,                    # 狀態來源原始資料（供比對）
        "verify_summary": verify_summary,
        "verify_data": vd,
        "control_apis_documented_not_called": CONTROL_APIS,
    }
    save_json(record)
    print_summary(bool(token), status)   # 只印 5 區塊；其他/verify 保留在 JSON


if __name__ == "__main__":
    main()
