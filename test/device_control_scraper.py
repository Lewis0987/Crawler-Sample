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
    # 排程清單（每個排程一筆，含 enableFlag=1 啟用 / 2 停用）；對應前端 pcs/schedulePlan.vue 之排程模板清單。
    "getScheduleTemplateList": "/schedule/template/list",
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
# PCS「控制模式」顯示規則（唯讀顯示層；依 getScheduleSwitch 兩個開關的實際組合判斷，不預設 fallback）：
#   schedulePlanSwitch==1                    → 智慧模式
#   schedulePlanSwitch==0 且 manualModeSwitch==1 → 手動模式
#   schedulePlanSwitch==0 且 manualModeSwitch==0 → 未開啟模式（兩者皆關，非手動）
#   任一欄位缺失/None/解析失敗                 → 未知（狀態無法確認）
# ⚠️ 不再以 getRunMode 或「排程關=手動」作為 fallback。
_RUNMODE_SMART = {"auto", "schedule", "smart", "intelligent", "智慧模式", "智慧"}
_RUNMODE_MANUAL = {"manual", "手動模式", "手動"}
# 機器語意 → 顯示中文
_CONTROL_MODE_DISPLAY = {"smart": "智慧模式", "manual": "手動模式",
                         "none": "未開啟模式", "unknown": "未知（狀態無法確認）"}
_SCHEDULE_DISPLAY = {True: "開", False: "關", None: "暫無資料"}
# PCS 電網模式（唯讀狀態顯示；非控制）：併網/離網 ← guest PCS 執行狀態
_GRID_MODE_DISPLAY = {"grid_connected": "併網", "off_grid": "離網", "unknown": "未知"}
# 電池「充放電狀態」判斷閾值（|功率| ≤ 此值視為待機，kW）
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
    PCS 功率控制模式（共用解析）— 依 API「當下實際欄位」判斷，**不套任何預設值**：
      交流有功 / 直流恆流 / 直流恆功率 / 離網交流電壓 / 未知

    來源欄位（guest PCS `/hmiGuest/unauthorizedAccess/envCon/pcs` 實際回傳的 mark）：
      - **離網**（systemOffGridStatus=true / systemGridTiedStatus=offGrid，見 parse_pcs_grid_mode）
        → 「離網交流電壓」（離網時 PCS 為交流電壓源，非併網的有功/直流控制）
      - 併網時：
        - energyDispatchingMode：type.attr.desc.ac → 交流有功；type.attr.desc.dc → 直流
        - dcControlMode（僅直流時看）：含 current → 直流恆流；含 power/watt → 直流恆功率
    欄位查不到或無法判斷 → 「未知」（不猜、不套預設）。

    註：僅補「狀態顯示」，不含任何離網控制；離網判斷取自 API 電網狀態欄位（非文字推論）。
    """
    raw = raw or {}
    # 離網：功率控制模式為「離網交流電壓」（依 API 電網狀態，避免誤顯示併網的直流恆流/恆功率）
    if parse_pcs_grid_mode(raw) == "off_grid":
        return "離網交流電壓"
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


# PCS「當前狀態」badge 設定 —— 100% 複製前端 pcsMode_US.vue 的 He 陣列（本機為 US 品牌 shengHong）。
# 固定順序；systemOnOrOffStatus 永遠顯示，其餘 5 個僅 oldValue=="1" 才顯示；顯示文字用該 mark 的中文 value。
_PCS_STATUS_CONFIG = [
    ("systemOnOrOffStatus", True),      # 永遠顯示（執行/停止…）
    ("systemFaultStatus", False),       # 故障（oldValue=="1"）
    ("systemGridTiedStatus", False),    # 併網（oldValue=="1"）
    ("systemOffGridStatus", False),     # 離網（oldValue=="1"）
    ("systemChargingStatus", False),    # 充電（oldValue=="1"）
    ("systemDischargingStatus", False), # 放電（oldValue=="1"）
]

# guest PCS 端點（狀態 badge 來源），取中文 value 需帶 Accept-Language: zh-TW
GUEST_PCS_PATH = READONLY_ENDPOINTS["guest_pcs"]
PCS_STATUS_LANG_HEADER = {"Accept-Language": "zh-TW"}


def parse_pcs_current_status(metrics):
    """
    PCS 當前狀態 —— **100% 複製前端 pcsMode_US.vue 的渲染邏輯，不讀 systemStatus**。

    前端（US 品牌）以 He 設定陣列，依固定順序檢查 guest PCS 的 metricsDataVoList：
      - systemOnOrOffStatus：alwaysDisplay，永遠顯示其 value
      - systemFaultStatus / systemGridTiedStatus / systemOffGridStatus /
        systemChargingStatus / systemDischargingStatus：oldValue=="1" 才顯示其 value
    最後依上述順序用「 / 」串接。value 為中文（API 帶 Accept-Language: zh-TW）。
    不補「正常」、不補「未知」、不固定段數、不改順序。

    參數 metrics：guest PCS 的 metricsDataVoList（list[dict]，含 mark/value/oldValue）。
    """
    marks = {
        item.get("mark"): item
        for item in (metrics or [])
        if isinstance(item, dict) and item.get("mark")
    }

    statuses = []
    for mark, always_display in _PCS_STATUS_CONFIG:
        item = marks.get(mark)
        if not item:
            continue

        value = str(item.get("value") or "").strip()
        old_value = str(item.get("oldValue") or "").strip()

        if not value:
            continue

        if always_display or old_value == "1":
            statuses.append(value)

    return " / ".join(statuses)


def _pcs_metrics_list(pcs_data):
    """從 guest PCS 回傳（list[dev]）取第一台的 metricsDataVoList（list[dict]）。"""
    if isinstance(pcs_data, list) and pcs_data and isinstance(pcs_data[0], dict):
        return pcs_data[0].get("metricsDataVoList", []) or []
    return []


def fetch_pcs_status_data(client):
    """以 Accept-Language: zh-TW 取 guest PCS（value 為中文），供 PCS當前狀態 badge 使用。回傳原始 list。"""
    return client.get(GUEST_PCS_PATH, headers=PCS_STATUS_LANG_HEADER)


def get_pcs_current_status(client):
    """**唯一共用來源**：抓 zh-TW guest PCS → 以 parse_pcs_current_status 組出 PCS當前狀態字串。"""
    return parse_pcs_current_status(_pcs_metrics_list(fetch_pcs_status_data(client)))


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


def parse_schedule_plans(template_list):
    """
    排程清單（/schedule/template/list）→ (是否取得清單, 總數, 啟用數)。
    每筆排程的 enableFlag==1 視為「啟用」（前端 Switch checkedValue:1 / unCheckedValue:2）；
    其餘（2 停用 / 0 / 缺）視為停用。
    """
    have = isinstance(template_list, list)
    plans = template_list if have else []
    total = len(plans)
    enabled = sum(1 for p in plans if isinstance(p, dict) and p.get("enableFlag") in (1, "1"))
    return have, total, enabled


def _switch01(v):
    """開關值 → 1 / 0 / None（缺失/非 0-1 → None，供 fail-safe「未知」）。"""
    if v in (1, "1"):
        return 1
    if v in (0, "0"):
        return 0
    return None


def parse_pcs_modes(runmode, schedule, pcs_raw=None, template_list=None):
    """
    分開解析 PCS 各狀態，回傳機器語意 dict：
      - control_mode      : smart / manual / none / unknown
            ← 依 schedulePlanSwitch + manualModeSwitch 實際組合（不預設 fallback、不用 getRunMode）：
              schedulePlanSwitch==1 → smart；==0 & manualModeSwitch==1 → manual；
              ==0 & manualModeSwitch==0 → none（未開啟模式）；任一 None/缺失 → unknown。
      - schedule_enabled  : True / False / None              ← getScheduleSwitch.schedulePlanSwitch（1/0）
      - schedule_have_list/total_plans/enabled_plans         ← /schedule/template/list（enableFlag==1 啟用）
      - schedule_idle     : True → 排程開關開但所有排程清單皆停用（排程實際不會執行）
      - grid_mode         : grid_connected / off_grid / unknown  ← guest PCS 執行狀態（唯讀狀態顯示）
      - power_control_mode: 交流有功 / 直流恆流 / 直流恆功率 / 離網交流電壓 / 未知
    """
    # 1) 兩個開關（1/0/None）
    ps = ms = None
    if isinstance(schedule, dict) and not schedule.get("_error"):
        ps = _switch01(schedule.get("schedulePlanSwitch"))
        ms = _switch01(schedule.get("manualModeSwitch"))
    schedule_enabled = None if ps is None else (ps == 1)

    # 2) 排程清單啟用統計
    have_list, total_plans, enabled_plans = parse_schedule_plans(template_list)

    # 3) control_mode ← 兩開關實際組合（不 fallback 手動、不使用 getRunMode）
    if ps is None:
        control_mode = "unknown"
    elif ps == 1:
        control_mode = "smart"
    elif ms is None:
        control_mode = "unknown"
    elif ms == 1:
        control_mode = "manual"
    else:                       # ps==0 且 ms==0
        control_mode = "none"

    # 4) 排程開關開、但（已取得清單且）所有排程清單皆停用 → 排程實際不會執行
    schedule_idle = (ps == 1) and have_list and enabled_plans == 0

    # 5) grid_mode / power_control_mode（唯讀狀態顯示；非控制）
    grid_mode = parse_pcs_grid_mode(pcs_raw)
    power_control_mode = parse_pcs_power_control_mode(pcs_raw)

    return {
        "control_mode": control_mode,
        "schedule_enabled": schedule_enabled,
        "schedule_have_list": have_list,
        "schedule_total_plans": total_plans,
        "schedule_enabled_plans": enabled_plans,
        "schedule_idle": schedule_idle,
        "grid_mode": grid_mode,
        "power_control_mode": power_control_mode,
    }


def parse_pcs(pcs_data, schedule, runmode, pcs_status_data=None, template_list=None):
    """
    HMI 左側 [PCS] 區塊：狀態 / 控制模式 / 排程 / 電網模式 / 功率控制模式（各自獨立解析）。
    pcs_status_data：以 zh-TW 取得的 guest PCS 原始 list（供 PCS當前狀態 badge 用中文 value）；
                     未提供時退回 pcs_data（值可能為 enum，非中文）。
    template_list：/schedule/template/list（供控制模式判斷與「排程未啟用」警告）。
    """
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

    # PCS當前狀態：100% 複製前端 pcsMode_US.vue，依 _PCS_STATUS_CONFIG 順序組 badge（中文 value）
    out["PCS當前狀態"] = parse_pcs_current_status(_pcs_metrics_list(pcs_status_data or pcs_data))

    # 各狀態解析；電網模式與功率控制模式為唯讀狀態，永遠顯示
    modes = parse_pcs_modes(runmode, schedule, raw, template_list)
    mode_label = _CONTROL_MODE_DISPLAY[modes["control_mode"]]                   # 智慧模式/手動模式/未知 ← 排程開關
    if modes["control_mode"] == "smart" and modes["schedule_idle"]:
        mode_label = "智慧模式（排程未啟用）"                                    # 排程開關開但清單全停用
    out["PCS控制模式"] = mode_label
    out["PCS排程開關狀態"] = _SCHEDULE_DISPLAY[modes["schedule_enabled"]]        # 開/關 ← schedulePlanSwitch
    # 排程清單摘要（唯讀；供除錯/佐證）＋ Console 警告（排程開關開但全部停用）
    if modes["schedule_have_list"]:
        out["PCS排程清單"] = f"共 {modes['schedule_total_plans']} 筆，啟用 {modes['schedule_enabled_plans']} 筆"
    out["_排程警告"] = ("排程開關已啟用，但所有排程清單皆為關閉，排程目前不會執行。"
                        if modes["schedule_idle"] else "")
    # 顯示 label 用「PCS工作模式」；底層機器語意仍為 grid_mode（systemGridTiedStatus/systemOffGridStatus）
    out["PCS工作模式"] = _GRID_MODE_DISPLAY[modes["grid_mode"]]                 # 併網/離網/未知（唯讀狀態，label=工作模式）
    out["PCS功率控制模式"] = modes["power_control_mode"]                         # 交流有功/直流恆流/直流恆功率/離網交流電壓/未知（依 API 實際）

    # 手動模式開關 ← getScheduleSwitch.manualModeSwitch（保留，供控制驗證用）
    if isinstance(schedule, dict) and "manualModeSwitch" in schedule:
        out["PCS手動模式開關"] = "已啟用" if schedule.get("manualModeSwitch") in (1, "1") else "已停用"

    # 機器語意（供除錯/程式判斷；"_" 開頭 console 不印，但存於 JSON）
    out["_pcs_modes"] = modes
    return out


def _battery_current_state_like_hmi(main, bcu):
    """
    電池「當前狀態」——**100% 複製 HMI /pcsControl `battery.vue` 的 _() 判斷**
    （前端已確認為 getBcuState 的唯一消費者；來源見 charge_discharge_report_DESIGN.md）：
      cur = mainControlCollectsInformation.rackElectricCurrent（原始電流，非 V×A）
        cur > 0  → 充電
        cur < 0  → 放電
        cur == 0 → getBcuState ? 故障 : 待機
        cur 無效 / 無資料 / 例外 → 故障（比照前端 try/catch 與 null 分支）
    bcu（/can/v1/getBcuState）取得失敗（非 bool）時，比照前端預設 true → 故障側。
    """
    u = bcu if isinstance(bcu, bool) else True        # 前端 catch 預設 U.value=true
    try:
        cur = float(main["rackElectricCurrent"])
    except (TypeError, ValueError, KeyError):
        return "故障"                                  # null / 無資料 / 例外 → 故障
    if cur > 0:
        return "充電"
    if cur < 0:
        return "放電"
    if cur == 0:
        return "故障" if u else "待機"
    return "故障"                                      # NaN 等 → 故障


def parse_battery(main, manual_power, dodi, bcu=None):
    """
    HMI 右上 [電池] 區塊，固定順序：
      電池上下電狀態 → 電池當前狀態 → 電池充放電狀態 → SOC/電壓/電流/功率 → 主正反饋/主正/主負反饋/主負/環流。
    電池當前狀態 100% 複製 HMI battery.vue（current 符號 + getBcuState）；電池充放電狀態為 V×A 能量流向。
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
        # 電池充放電狀態：battery_flow_state（V×A 能量流向：<0 放電 / >0 充電 / ≈0 待機）。
        # ⚠ 這只代表「能量流向」，不代表設備健康/故障 → 不再指定給「電池當前狀態」。
        flow_state, power_kw = battery_flow_state(main)
        out["電池充放電狀態"] = flow_state
        if soc is not None:
            out["當前SOC"] = f"{soc} {main.get('rackSocUnit', '%')}"
        if v is not None:
            out["當前電壓"] = f"{v} {main.get('rackTotalBatteryVoltageUnit', 'V')}"
        if a is not None:
            out["當前電流"] = f"{a} {main.get('rackElectricCurrentUnit', 'A')}"
        if power_kw is not None:
            out["當前功率"] = f"{power_kw:.3f} kW"
    else:
        out["電池充放電狀態"] = "暫無資料"
    # 電池當前狀態：100% 複製 HMI /pcsControl battery.vue（唯一 getBcuState 消費者，已全 bundle 驗證）。
    out["電池當前狀態"] = _battery_current_state_like_hmi(main, bcu)
    # 3) 反饋/接觸器（主正反饋/主正/主負反饋/主負/環流）← getDOAndDIMsg
    out.update(parse_feedback(dodi))
    # 4) 暫時 Debug（唯讀）
    out["battery_power_field"] = "getDOAndDIMsg: main.positive/negative.contactor.feedback"
    out["battery_power_raw"] = power_raw
    out["battery_power_mapped"] = power_state
    out["current_power"] = f"{power_kw:.3f} kW" if power_kw is not None else "unknown"
    out["current_flow_state"] = out.get("電池充放電狀態")   # 能量流向（原 current_state 改名，避免誤解為「當前狀態」）
    out["current_state"] = out.get("電池當前狀態")           # 電池當前狀態（目前為未知，待來源確認）
    return out


def parse_air(air_data):
    flat = _flatten_metrics(air_data) if air_data else {}
    if not flat:
        return {"空調狀態": "暫無資料"}
    out = {}

    # 空調開關：唯一以 indoorFanStatus.oldValue 判定（0→關 / 非0→開），對齊前端 airMode.vue 開關 d.value。
    # 實測確認：關/開時 indoorFanStatus 會確實翻轉（OFF≈5s、ON≈9s）；equipmentWorkingStatus/compressorStatus
    # 在開關當下不變動，故不可用（此為先前「關機後仍判定開」誤判的根因）。
    fan_old = (flat.get("indoorFanStatus") or {}).get("oldValue")
    if fan_old is not None:
        out["空調開關"] = "關" if str(fan_old) == "0" else "開"

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
        "PCS": parse_pcs(rd.get("guest_pcs"), rd.get("getScheduleSwitch"), rd.get("getRunMode"),
                         rd.get("guest_pcs_tw"), rd.get("getScheduleTemplateList")),
        "電池": parse_battery(rd.get("overview_mainControl"), rd.get("getManuallyPowerState"),
                             rd.get("getDOAndDIMsg"), rd.get("getBcuState")),
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


# ---- UI 顯示層（formatter）：console 只印 HMI UI 有的欄位；parser / JSON / CSV 不受影響 ----
# 每區塊只保留 HMI 實際顯示欄位；raw/debug/parser 欄位（如 battery_power_* / current_*）不印，JSON 仍完整。
_UI_FIELDS = {
    "PCS": ["PCS當前狀態", "PCS控制模式", "PCS排程開關狀態", "PCS排程清單", "PCS工作模式",
            "PCS功率控制模式", "PCS手動模式開關"],
    "電池": ["電池上下電狀態", "電池當前狀態", "電池充放電狀態", "當前SOC", "當前電壓", "當前電流", "當前功率",
             "主正反饋", "主正", "主負反饋", "主負", "環流"],
    "空調": ["空調開關", "空調目前狀態", "空調製冷設定", "空調製熱設定", "空調濕度設定",
             "空調櫃內溫度", "空調櫃內濕度", "空調故障"],
    # 進排風 / 冷卻循環：HMI 只顯示「開關」；由 parser 欄位導出（不印執行狀態/水泵/故障）
}


def _ui_view(section, fields):
    """UI 顯示層：回傳該區塊 console 要印的欄位（依 HMI）；parser/JSON 原欄位不變。"""
    fields = fields or {}
    if section in _UI_FIELDS:
        return {k: fields[k] for k in _UI_FIELDS[section] if k in fields}
    if section == "進排風":                      # HMI 只有「進排風開關」（由執行狀態導出）
        v = str(fields.get("進排風執行狀態", ""))
        sw = "開啟" if ("開啟" in v or "運轉" in v) else ("關閉" if v else "暫無資料")
        return {"進排風開關": sw}
    if section == "冷卻循環":                     # HMI 只有「冷卻循環開關」
        return {"冷卻循環開關": fields.get("冷卻循環水泵狀態")
                or fields.get("冷卻循環執行狀態") or "暫無資料"}
    return {k: v for k, v in fields.items() if not str(k).startswith("_")}


def print_summary(logged_in, status):
    """console 只印 [PCS][電池][空調][進排風][冷卻循環] 的「HMI UI 欄位」（經 _ui_view formatter）；
    raw/debug 與【其他】(DO/DI) 與【控制驗證端點】(403) 僅存於 output JSON，不印。"""
    print("=" * 56)
    print(f"設備控制（只讀）｜登入：{'成功' if logged_in else '失敗/未登入'}")
    for section in ("PCS", "電池", "空調", "進排風", "冷卻循環"):
        print(f"\n[{section}]")
        for label, value in _ui_view(section, status.get(section, {})).items():
            print(f"  {label}：{value}")
        # PCS：排程開關開但所有排程清單皆停用 → 額外警告（唯讀提示，非控制）
        if section == "PCS":
            warn = status.get("PCS", {}).get("_排程警告")
            if warn:
                print(f"  ⚠ {warn}")


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
    # PCS當前狀態 badge 需中文 value → 另以 Accept-Language: zh-TW 再取一次 guest PCS
    rd["guest_pcs_tw"] = fetch_pcs_status_data(client)
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
