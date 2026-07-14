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
# 密碼不寫在程式碼：由 api_client 依 .env 的 HMI_PASSWORD + SM2_PUBLIC_KEY 即時 SM2 加密。

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
# PCS「控制模式」唯一來源 = getRunMode（智慧/手動），與排程互相獨立、不可互相推導。
# 值可能為 API 英文（auto/schedule/smart/manual）或中文（智慧模式/手動模式）。
_RUNMODE_SMART = {"auto", "schedule", "smart", "intelligent", "智慧模式", "智慧"}
_RUNMODE_MANUAL = {"manual", "手動模式", "手動"}
# 機器語意 → 顯示中文
_CONTROL_MODE_DISPLAY = {"smart": "智慧模式", "manual": "手動模式", "unknown": "未知"}
_SCHEDULE_DISPLAY = {True: "開", False: "關", None: "暫無資料"}
# PCS 電網模式（唯讀狀態顯示；非控制）：併網/離網 ← guest PCS 執行狀態
_GRID_MODE_DISPLAY = {"grid_connected": "併網", "off_grid": "離網", "unknown": "未知"}
# 電池「當前狀態」判斷閾值（|功率| ≤ 此值視為待機，kW）
_BATTERY_IDLE_KW = 0.1


def battery_power_state_from_dodi(dodi):
    """
    ⚠️ 電池上下電狀態的『唯一正解』：依主正/主負接觸器**反饋**（getDOAndDIMsg）判斷。
       實測：已上電時 main.positive/negative.contactor.feedback 皆=1（閉合）；
             已下電時皆=0（斷開）。getManuallyPowerState / getBcuState 皆非此狀態（上電時仍為 False）。
    回傳 (state, raw_dict)：state ∈ 已上電 / 已下電 / 切換中 / 未知。
    scraper 顯示與 operator verify 共用此函式，確保同一套 mapping。
    """
    pos = neg = None
    if isinstance(dodi, list):
        for it in dodi:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or it.get("mark") or ""
            if "main.positive.contactor.feedback" in name:
                pos = it.get("value")
            elif "main.negative.contactor.feedback" in name:
                neg = it.get("value")
    raw = {"main.positive.contactor.feedback": pos, "main.negative.contactor.feedback": neg}

    def _on(x):
        return x in (1, "1")

    def _off(x):
        return x in (0, "0")

    if pos is None and neg is None:
        return "未知", raw
    if _on(pos) and _on(neg):
        return "已上電", raw
    if _off(pos) and _off(neg):
        return "已下電", raw
    return "切換中", raw


def battery_flow_state(main):
    """
    電池充放電狀態（共用於顯示與 battery_power_off 前置檢查）：
    依功率方向回傳 (state, power_kw)：<0 放電 / >0 充電 / ≈0 待機 / 無資料 未知。
    main = overview/mainControlCollectsInformation dict。
    """
    if not isinstance(main, dict):
        return "未知", None
    v = main.get("rackTotalBatteryVoltage")
    a = main.get("rackElectricCurrent")
    if v is None or a is None:
        return "未知", None
    power_kw = v * a / 1000
    if power_kw <= -_BATTERY_IDLE_KW:
        return "放電", power_kw
    if power_kw >= _BATTERY_IDLE_KW:
        return "充電", power_kw
    return "待機", power_kw
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


def parse_pcs_power_control_mode(raw):
    """
    PCS 工作模式 / 功率控制模式（共用解析）— 依 API「當下實際欄位」判斷，**不套任何預設值**：
      交流有功 / 直流恆流 / 直流恆功率 / 未知

    來源欄位（guest PCS `/hmiGuest/unauthorizedAccess/envCon/pcs` 實際回傳的 mark）：
      - energyDispatchingMode：type.attr.desc.ac → 交流；type.attr.desc.dc → 直流
      - dcControlMode（僅直流時看）：含 current → 直流恆流；含 power/watt → 直流恆功率
    欄位查不到或無法判斷 → 「未知」（不猜、不套預設）。

    註：目前實測值 energyDispatchingMode=type.attr.desc.ac、dcControlMode=type.attr.desc.fixedPower。
    直流恆流的實際 enum 尚未於實機觀察到，故以子字串（current/power）判斷，判不到即回「未知」。
    """
    raw = raw or {}
    disp = str(raw.get("energyDispatchingMode", "")).lower()
    if disp.endswith("ac"):
        return "交流有功"
    if disp.endswith("dc"):
        dcm = str(raw.get("dcControlMode", "")).lower()
        if "current" in dcm:
            return "直流恆流"
        if "power" in dcm or "watt" in dcm:
            return "直流恆功率"
        return "未知"
    return "未知"


def parse_pcs_current_status(pcs_raw):
    """
    PCS 當前狀態：**直接對照 API 的 system*Status 狀態欄位值**（type.attr.desc.*），
    對齊 HMI「當前狀態」；**不由 控制模式 / 功率 / 電流 / 電壓 / 排程 / 手動 推論**。

    來源（guest PCS `/hmiGuest/unauthorizedAccess/envCon/pcs`；欄位值本身即狀態描述）：
      - systemFaultStatus / systemFailedStatus：非 normal/false → 故障
      - systemChargingStatus    = ...charging     → 充電
      - systemDischargingStatus = ...discharging  → 放電
      - systemBootingStatus     = ...booting/true → 啟動中
      - systemStandbyStatus     = ...standby      → 待機
      - systemOnOrOffStatus     = ...stopping → 停止中；...stop/off/running → 待機
        （對照 HMI：停機/運轉但無充放/故障/啟動時，HMI「當前狀態」顯示「待機」）
    優先序：故障 > 充電 > 放電 > 啟動中 > 停止中 > 待機；皆無對應 → 未知。
    （註：狀態欄位值為描述字（charging/standby…）而非 true/false，故直接依值對照。）
    """
    raw = pcs_raw or {}

    def suf(mark):
        return str(raw.get(mark, "")).lower().rsplit(".", 1)[-1]

    for mk in ("systemFaultStatus", "systemFailedStatus"):
        s = suf(mk)
        if s and s not in ("normal", "false"):
            return "故障"
    if suf("systemChargingStatus") == "charging":
        return "充電"
    if suf("systemDischargingStatus") == "discharging":
        return "放電"
    if suf("systemBootingStatus") in ("booting", "true"):
        return "啟動中"
    if suf("systemOnOrOffStatus") == "stopping":   # 明確「停止中」過渡態才顯示
        return "停止中"
    if suf("systemStandbyStatus") == "standby":
        return "待機"
    if suf("systemOnOrOffStatus") in ("stop", "off", "running", "run", "on"):
        return "待機"   # 對照 HMI：停機/運轉且無充放/故障/啟動 → 待機
    return "未知"


def parse_pcs_grid_mode(pcs_raw):
    """
    PCS 電網模式（**唯讀狀態顯示**，非控制）：grid_connected / off_grid / unknown。
    來源（guest PCS `/hmiGuest/unauthorizedAccess/envCon/pcs` 實際回傳）：
      - systemGridTiedStatus：...gridTied → 併網
      - systemOffGridStatus ：...true → 離網 / ...false → 併網
    判不到 → unknown。（此為狀態顯示，與已移除的離網「控制」無關。）
    """
    raw = pcs_raw or {}
    off = str(raw.get("systemOffGridStatus", "")).lower()
    gt = str(raw.get("systemGridTiedStatus", "")).lower()
    if off.endswith("true") or gt.endswith(("offgrid", "offgird")):
        return "off_grid"
    if off.endswith("false") or gt.endswith("gridtied"):
        return "grid_connected"
    return "unknown"


def parse_pcs_modes(runmode, schedule, pcs_raw=None):
    """
    分開解析 PCS 各狀態（互不推導），回傳機器語意 dict：
      - control_mode      : smart / manual / unknown         ← getRunMode（不可用排程/電網反推）
      - schedule_enabled  : True / False / None              ← getScheduleSwitch.schedulePlanSwitch（1/0）
      - grid_mode         : grid_connected / off_grid / unknown  ← guest PCS 執行狀態（唯讀狀態顯示）
      - power_control_mode: 交流有功 / 直流恆流 / 直流恆功率 / 未知
            ← parse_pcs_power_control_mode()：依 API 當下實際欄位，**不套預設值**（判不到→未知）
    schedulePlanSwitch=0 仍可能為智慧模式；各項不互相覆蓋、不互相推導。
    """
    # 1) control_mode ← getRunMode（唯一來源）
    rm = runmode.strip().lower() if isinstance(runmode, str) else ""
    if rm in _RUNMODE_SMART:
        control_mode = "smart"
    elif rm in _RUNMODE_MANUAL:
        control_mode = "manual"
    else:
        control_mode = "unknown"

    # 2) schedule_enabled ← schedulePlanSwitch（獨立；不影響 control_mode）
    schedule_enabled = None
    if isinstance(schedule, dict) and "schedulePlanSwitch" in schedule:
        schedule_enabled = schedule.get("schedulePlanSwitch") in (1, "1")

    # 3) grid_mode ← guest PCS 執行狀態（唯讀狀態顯示；非控制）
    grid_mode = parse_pcs_grid_mode(pcs_raw)

    # 4) power_control_mode ← 依 API 實際欄位（3 模式；不套預設，判不到→未知）
    power_control_mode = parse_pcs_power_control_mode(pcs_raw)

    return {
        "control_mode": control_mode,
        "schedule_enabled": schedule_enabled,
        "grid_mode": grid_mode,
        "power_control_mode": power_control_mode,
    }


def parse_pcs(pcs_data, schedule, runmode):
    """HMI 左側 [PCS] 區塊：狀態 / 控制模式 / 排程 / 電網模式 / 功率控制模式（各自獨立解析）。"""
    flat = _flatten_metrics(pcs_data) if pcs_data else {}
    raw = _pcs_raw(pcs_data)
    out = {}

    # PCS狀態：開關機 → 故障/告警 → 布林旗標
    parts = []
    if _v(flat, "systemOnOrOffStatus"):
        parts.append(str(_v(flat, "systemOnOrOffStatus")))
    for mk, label in _PCS_ALARM_FLAGS.items():
        val = _v(flat, mk)
        if val and val not in ("正常", "否"):
            parts.append(label)
    for mk, label in _PCS_BOOL_FLAGS.items():
        if _v(flat, mk) in _TRUE_SET:
            parts.append(label)
    out["PCS狀態"] = " / ".join(parts) if parts else "暫無資料"

    # PCS當前狀態：直接對照 API system*Status 狀態欄位（非由功率/模式推論）
    out["PCS當前狀態"] = parse_pcs_current_status(raw)

    # 各狀態獨立解析（互不推導）；電網模式與功率控制模式為唯讀狀態，永遠顯示
    modes = parse_pcs_modes(runmode, schedule, raw)
    out["PCS控制模式"] = _CONTROL_MODE_DISPLAY[modes["control_mode"]]           # 智慧模式/手動模式/未知 ← getRunMode
    out["PCS排程開關狀態"] = _SCHEDULE_DISPLAY[modes["schedule_enabled"]]        # 開/關 ← schedulePlanSwitch
    # 顯示 label 用「PCS工作模式」；底層機器語意仍為 grid_mode（systemGridTiedStatus/systemOffGridStatus）
    out["PCS工作模式"] = _GRID_MODE_DISPLAY[modes["grid_mode"]]                 # 併網/離網/未知（唯讀狀態，label=工作模式）
    out["PCS功率控制模式"] = modes["power_control_mode"]                         # 交流有功/直流恆流/直流恆功率/未知（依 API 實際）

    # 手動模式開關 ← getScheduleSwitch.manualModeSwitch（保留，供控制驗證用）
    if isinstance(schedule, dict) and "manualModeSwitch" in schedule:
        out["PCS手動模式開關"] = "已啟用" if schedule.get("manualModeSwitch") in (1, "1") else "已停用"

    # 機器語意（供除錯/程式判斷；"_" 開頭 console 不印，但存於 JSON）
    out["_pcs_modes"] = modes
    return out


def parse_battery(main, manual_power, dodi):
    """
    HMI 右上 [電池] 區塊，固定順序：
      電池上下電狀態 → 當前狀態/SOC/電壓/電流/功率 → 主正反饋/主正/主負反饋/主負/環流。
    """
    out = {}
    # 1) 電池上下電狀態 ← 主正/主負接觸器反饋（getDOAndDIMsg）；非 getManuallyPowerState
    power_state, power_raw = battery_power_state_from_dodi(dodi)
    out["電池上下電狀態"] = power_state
    # 2) 電池系統指標 ← overview/mainControlCollectsInformation
    power_kw = None
    if isinstance(main, dict):
        v = main.get("rackTotalBatteryVoltage")
        a = main.get("rackElectricCurrent")
        soc = main.get("rackSoc")
        # 當前狀態：共用 battery_flow_state（<0 放電 / >0 充電 / ≈0 待機），不用固定「運轉」
        flow_state, power_kw = battery_flow_state(main)
        out["當前狀態"] = flow_state
        if soc is not None:
            out["當前SOC"] = f"{soc} {main.get('rackSocUnit', '%')}"
        if v is not None:
            out["當前電壓"] = f"{v} {main.get('rackTotalBatteryVoltageUnit', 'V')}"
        if a is not None:
            out["當前電流"] = f"{a} {main.get('rackElectricCurrentUnit', 'A')}"
        if power_kw is not None:
            out["當前功率"] = f"{power_kw:.3f} kW"
    else:
        out["當前狀態"] = "暫無資料"
    # 3) 反饋/接觸器（主正反饋/主正/主負反饋/主負/環流）← getDOAndDIMsg
    out.update(parse_feedback(dodi))
    # 4) 暫時 Debug（確認 mapping 正確後可移除）
    out["battery_power_field"] = "getDOAndDIMsg: main.positive/negative.contactor.feedback"
    out["battery_power_raw"] = power_raw
    out["battery_power_mapped"] = power_state
    out["current_power"] = f"{power_kw:.3f} kW" if power_kw is not None else "unknown"
    out["current_state"] = out.get("當前狀態")
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


# console 不印出的欄位（僅影響顯示；JSON/解析完全不變）：
#   - PCS狀態：與 PCS當前狀態 重複，console 只留 PCS當前狀態。
#   - 電池內部 Debug 欄位（battery_power_* / current_power / current_state）：
#     為除錯用原始值，與正式欄位（電池上下電狀態 / 當前狀態 / 當前功率）重複，正式畫面不顯示。
_CONSOLE_HIDE_LABELS = {
    "PCS狀態",
    "battery_power_field", "battery_power_raw", "battery_power_mapped",
    "current_power", "current_state",
}


def print_summary(logged_in, status):
    """console 只印 [PCS][電池][空調][進排風][冷卻循環]；
    【其他】(DO/DI 原始) 與【控制驗證端點】(verify 403) 僅存於 output JSON，不印。
    另隱藏重複顯示欄位（_CONSOLE_HIDE_LABELS）；JSON 仍保留完整欄位。"""
    print("=" * 56)
    print(f"設備控制（只讀）｜登入：{'成功' if logged_in else '失敗/未登入'}")
    for section in ("PCS", "電池", "空調", "進排風", "冷卻循環"):
        print(f"\n[{section}]")
        for label, value in status.get(section, {}).items():
            if label.startswith("_") or label in _CONSOLE_HIDE_LABELS:
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
    # 統一登入：由 api_client.login_hmi 印出 4 行登入 log（嘗試登入/[ENV] loaded/
    # HMI login: user=/HMI login success），不含任何機密。失敗細節亦由其印出。
    token = client.login_hmi(USERNAME)

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
