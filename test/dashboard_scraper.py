# -*- coding: utf-8 -*-
"""
內網 Dashboard 抓取工具 — 數據概覽（Data Overview）
==================================================================
終端摘要「只印目前數據概覽畫面實際顯示的欄位」，欄位名稱、順序、狀態文字、單位
皆比照前端 Vue 渲染邏輯（batteryOverview / pcsOverview.data / env-detail /
controlOverview 等）逐欄對應，不沿用舊版摘要、不多印 UI 沒顯示的資料。

【資料來源與解析（前端 JS 靜態分析 + 實際回傳校正）】
  一、電池概覽
    - 基本資訊 / Rack容量 / Rack極端值：overview/mainControlCollectsInformation、
      overview/rackCapacityInformation、overview/rackExtremeValueInformation
      （扁平 dict：<key> + <key>Unit；極端值另含 <key>Index）。
      數值以「JS 顯示規則」呈現（整數浮點去掉 .0）；絕緣電阻 > 1000 比照前端加千分位。
  二、PCS概覽（envCon/pcs，list→metricsDataVoList）
    - 系統：啟停 systemOnOrOffStatus(value) / 併網離網(systemGridTiedStatus·
      systemOffGridStatus 的 oldValue) / 工作模式(energyDispatchingMode.oldValue 交/直
      + getRunMode 排程/手動) / 充放電(systemCharging·systemDischarging.oldValue)。
    - 電量統計：累計交流放/充電 = UI 綁定 LowByte 的 value（accumulatedAcDischargeLowByte /
      accumulatedAcChargingPowerLowByte，原樣顯示含小數位）；High×65536+Low 的合併值僅供
      [DEBUG]（SHOW_DEBUG_RAW=True 才印）。當天交流放/充電 dailyDischarged/ChargedEnergyThroughAcPort。
    - 直流：dcPower / dcCurrent / dcInputVoltage。
    - 交流：totalPfOfAcBus / totalActivePowerOfAcBus / totalApparentPowerOfAcBus。
  三、環控概覽
    - 急停/閉門器/SPD/消防報警/消防故障：envCon/ttyS0 數位訊號，
      正常 = oldValue=="1" 且 device.alertFlag 非 True（比照 env-detail.js）。
    - 水浸：envCon/water 的 alarm，正常 = oldValue=="0"。
    - 冷卻循環系統：envCon/waterPumpStateRead（比照 controlOverview：正常 = 非真值）。
    - 空調：狀態(alertFlag) / 執行狀態(envicoolEdition=="103"→workingMode 否則
      equipmentWorkingStatus) / 設定溫度(coolingSetTemperature)。
    - UPS：狀態(alertFlag) / 工作模式(workingMode) / 電池容量(batteryCapacity)。
    - 多功能傳感器：狀態(alertFlag) / 溫度 / 濕度 / 甲烷濃度(ch4,%LEL) / 氫氣濃度(h2,ppm)。
    - AC 380 電量儀：狀態(alertFlag) / 相電壓AB·BC·CA / 相電流A·B·C。

【輸出原則】終端只印上述 UI 欄位；告警等 UI 未顯示於概覽卡片者仍存於 JSON、不進終端。
【相容性】保留 _flatten_metrics / _fmt_metric 供 device_control_scraper 重用。
【限制】只用 GET；不做 POST/PUT/DELETE；不碰設備控制。

執行方式：
    pip install requests
    python dashboard_scraper.py --once     # 抓一次即結束
    python dashboard_scraper.py --loop      # 啟動背景循環（Ctrl+C 乾淨停止）
    python dashboard_scraper.py             # 依 ENABLE_AUTO_FETCH：True→背景循環 / False→單次
"""

import os
import csv
import json
import atexit
import argparse
import threading
from datetime import datetime

import requests

from api_client import ApiClient


# ======================================================================
# 設定區
# ======================================================================
BASE_URL = "http://192.168.128.110:8080/admin-api"   # 已驗證；如 8853 有代理可改成 :8853
TIMEOUT = 8                 # 單支 API 逾時秒數
INTERVAL_SECONDS = 60       # 背景循環每 N 秒抓一次（下限 1 秒，避免設 0 狂打 API）
# 背景自動抓取總開關：True→無參數執行時自動啟動背景循環；False→不自動啟動（--once 仍可單次抓取）
ENABLE_AUTO_FETCH = True

# 輸出設定
OUTPUT_TO_CONSOLE = True
OUTPUT_TO_JSON = True
OUTPUT_TO_CSV = True
# 累計交流放/充電：UI 綁定 LowByte 的 value（正式輸出照此顯示）。High×65536+Low 的
# 「原始合併值」僅供除錯：設為 True 才會在終端末尾多印 [DEBUG] 兩行；預設 False（正式輸出不顯示）。
SHOW_DEBUG_RAW = False
# 統一輸出目錄：專案根目錄下的 output/（不論從哪個資料夾執行都一致），不存在則自動建立
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
JSON_PATH = os.path.join(_OUTPUT_DIR, "dashboard_data.json")   # 完整原始資料（每輪覆寫最新快照）
CSV_PATH = os.path.join(_OUTPUT_DIR, "dashboard_data.csv")     # 每輪摘要（附加一列，累積時間序列）

# 告警本次取回筆數（僅存 JSON、不印終端；數據概覽卡片不顯示告警明細）
ALARM_PAGE_SIZE = 3

# ---- Data Overview 全部區塊 endpoint（method 皆為 GET）----
ENDPOINTS = {
    # 一、電池概覽
    "電池概覽":       "/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation",
    "Rack容量資訊":   "/hmiGuest/unauthorizedAccess/overview/rackCapacityInformation",
    "Rack極端值資訊": "/hmiGuest/unauthorizedAccess/overview/rackExtremeValueInformation",
    # 二、PCS 概覽
    "PCS概覽":        "/hmiGuest/unauthorizedAccess/envCon/pcs",
    # 三、環控概覽
    "環控_空調":      "/hmiGuest/unauthorizedAccess/envCon/air",
    "環控_水系統":    "/hmiGuest/unauthorizedAccess/envCon/water",       # 水浸 alarm
    "環控_UPS":       "/hmiGuest/unauthorizedAccess/envCon/ups",
    "環控_串口ttyS0": "/hmiGuest/unauthorizedAccess/envCon/ttyS0",       # 急停/閉門/消防/SPD 數位訊號
    "多功能傳感器":   "/hmiGuest/unauthorizedAccess/envCon/multifunction",
    "AC380電量儀":    "/hmiGuest/unauthorizedAccess/envCon/voltameter",
    "冷卻循環_泵狀態": "/hmiGuest/unauthorizedAccess/envCon/waterPumpStateRead",  # 冷卻循環系統狀態
    # PCS 工作模式所需（排程/手動）：getRunMode（需登入）
    "getRunMode":     "/client/dynamic/dataOrControl/pcs/getRunMode",
    # 告警（僅存 JSON、不印終端）
    "告警資訊":       ("/hmiGuest/unauthorizedAccess/alarm/list", {"pageNo": 1, "pageSize": ALARM_PAGE_SIZE}),
}

# ---- i18n 狀態值中文對照（value 為 type.attr.* 這類 key 時翻譯；查不到就保留原字串）----
# 供 _zh() 使用；device_control_scraper 透過 _flatten_metrics 共用同一份對照。
STATUS_MAP = {
    "type.attr.desc.normal":      "正常",
    "type.attr.desc.fault":       "故障",
    "type.attr.desc.failed":      "故障",
    "type.attr.desc.alarm":       "告警",
    "type.attr.desc.derating":    "降載",
    "type.attr.desc.booting":     "啟動中",
    "type.attr.desc.running":     "運行中",
    "type.attr.desc.stop":        "停機",
    "type.attr.desc.standby":     "待機",
    "type.attr.desc.charging":    "充電中",
    "type.attr.desc.discharging": "放電中",
    "type.attr.desc.gridTied":    "併網",
    "type.attr.desc.offGird":     "離網",   # 註：後端原字串即拼字為 offGird
    "type.attr.desc.true":        "是",
    "type.attr.desc.false":       "否",
    # 控制/調度模式（controlMode 等）
    "type.attr.desc.remote":      "遠端",
    "type.attr.desc.local":       "本地",
    "type.attr.desc.auto":        "自動",
    "type.attr.desc.ac":          "交流",
    "type.attr.desc.fixedWatt":   "定功率",
    "type.attr.desc.fixedPower":  "定功率",
    "type.attr.desc.powerFactor": "功率因數",
    "type.attr.desc.step":        "階梯",
    # 環控執行狀態（equipmentWorkingStatus）
    "type.attr.run":              "運轉中",
    "type.attr.stop":             "停止",
    # 環控工作模式（workingMode）
    "type.attr.refrigeration":    "製冷",
    "type.attr.dehumidification": "除濕",
    "type.attr.airSupply":        "送風",
    "type.attr.heating":          "制熱",
    "type.attr.ventilation":      "通風",
    "type.attr.auto":             "自動",
    # 環控/UPS/傳感器狀態（env 頁實測補上）
    "type.attr.normal":            "正常",
    "type.attr.beRunning":         "運行中",
    "type.attr.connected":         "已連接",
    "type.attr.mainsMode":         "市電模式",
    "type.attr.ordinaryMode":      "一般模式",
    "type.attr.nonOverload":       "未過載",
    "type.attr.loadProtection":    "負載保護",
    "type.attr.notInEmergencyStop":"未急停",
    "type.attr.unitNoGeneralAlarm":"無總告警",
    "type.attr.batteryPowerIsGood":"電池電量正常",
}

# PCS 啟停狀態顯示（數據概覽用；比照 UI「停止/運轉」，不動共用 STATUS_MAP 以免影響 device_control）
_PCS_ONOFF_TEXT = {
    "type.attr.desc.stop":  "停止",
    "type.attr.desc.off":   "停止",
    "type.attr.desc.run":   "運轉",
    "type.attr.desc.running": "運轉",
    "type.attr.desc.start": "運轉",
    "type.attr.desc.on":    "運轉",
}

# PCS getRunMode 值語意（排程/手動）
_RUNMODE_SMART = {"auto", "schedule", "smart", "intelligent", "智慧模式", "智慧"}
_RUNMODE_MANUAL = {"manual", "手動模式", "手動"}

# ---- 一、電池概覽：欄位定義（皆對應 overview 扁平 dict 的 <key> / <key>Unit）----
BATTERY_BASIC = [                       # 基本資訊（含 SOC/SOH，UI 以儀表顯示數值）
    ("電壓",       "rackTotalBatteryVoltage"),
    ("電流",       "rackElectricCurrent"),
    ("絕緣電阻R+", "rackInsulationResistanceRPositive"),
    ("絕緣電阻R-", "rackInsulationResistanceRPositiveNegative"),
    ("SOC",        "rackSoc"),
    ("SOH",        "rackSoh"),
]
# 絕緣電阻 > 1000 時比照前端 toLocaleString() 加千分位
_INSULATION_KEYS = {"rackInsulationResistanceRPositive", "rackInsulationResistanceRPositiveNegative"}

RACK_CAPACITY = [                       # Rack容量資訊（依前端 pcsOverview.data.js 順序）
    ("可充電電量", "rechargeableBatteryCapacity"),
    ("可放電電量", "dischargeableCapacity"),
    ("單次充電能量", "singleChargeEnergy"),
    ("單次放電能量", "singleDischargeEnergy"),
    ("累計充電能量", "accumulatedChargingEnergy"),
    ("累計放電能量", "accumulatedDisChargingEnergy"),
    ("當日充電能量", "accumulatedDailyChargingEnergy"),
    ("當日放電能量", "accumulatedDailyDisChargingEnergy"),
]

RACK_EXTREME = [                        # Rack極端值資訊（含 <key>Index 對應編號）
    ("最大電壓", "rackCellMaxVoltage"),
    ("最小電壓", "rackCellMinVoltage"),
    ("最大溫度", "rackCellMaxTemperature"),
    ("最小溫度", "rackCellMinTemperature"),
    ("最大SOC", "rackCellMaxSoc"),
    ("最小SOC", "rackCellMinSoc"),
    ("最大SOH", "rackCellMaxSoh"),
    ("最小SOH", "rackCellMinSoh"),
]

# ---- 二、PCS 直流/交流資訊：mark 定義（envCon/pcs metricsDataVoList）----
PCS_DC = [
    ("直流功率",     "dcPower"),
    ("直流電流",     "dcCurrent"),
    ("直流輸入電壓", "dcInputVoltage"),
]
PCS_AC = [
    ("交流母線總功率因數", "totalPfOfAcBus"),
    ("交流母線總有功功率", "totalActivePowerOfAcBus"),
    ("交流母線總視在功率", "totalApparentPowerOfAcBus"),
]

# ---- 三、多功能傳感器 / AC380 電量儀：mark 定義（envCon）----
MULTIFUNCTION = [
    ("溫度",     "temperature"),
    ("濕度",     "humidity"),
    ("甲烷濃度", "ch4"),
    ("氫氣濃度", "h2"),
]
AC380 = [
    ("相電壓AB", "phaseVoltageAB"),
    ("相電壓BC", "phaseVoltageBC"),
    ("相電壓CA", "phaseVoltageCA"),
    ("相電流A",  "phaseCurrentA"),
    ("相電流B",  "phaseCurrentB"),
    ("相電流C",  "phaseCurrentC"),
]
# ======================================================================


# ---------------- 共用工具 ----------------
def _zh(value):
    """把 i18n 狀態值（type.attr.*）翻成中文；查不到或非字串就原樣回傳。"""
    if isinstance(value, str) and value in STATUS_MAP:
        return STATUS_MAP[value]
    return value


def _js_num(v):
    """
    以「前端 JS 顯示規則」呈現數字：整數值的浮點去掉 .0（0.0→'0'、21.0→'21'），
    其餘保留最短往返表示（901.8→'901.8'、3.222→'3.222'）。非數字原樣轉字串。
    """
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else repr(v)
    return str(v)


def _fmt_overview(d, key, comma=False):
    """overview 扁平 dict：<key> 數值（JS 規則）＋ <key>Unit；comma 且 >1000 時加千分位。"""
    if not isinstance(d, dict):
        return "-"
    v = d.get(key)
    if v is None:
        return "-"
    unit = d.get(key + "Unit", "")
    if comma and isinstance(v, (int, float)) and abs(v) > 1000:
        s = f"{int(round(v)):,}"          # 比照前端 toLocaleString()
    else:
        s = _js_num(v)
    return f"{s} {unit}" if unit else s


def _fmt_extreme(d, key):
    """Rack極端值：<key> 數值＋單位，後接對應編號「  #<index>」。"""
    if not isinstance(d, dict):
        return "-"
    v = d.get(key)
    if v is None:
        return "-"
    unit = d.get(key + "Unit", "")
    idx = d.get(key + "Index")
    s = f"{_js_num(v)} {unit}" if unit else _js_num(v)
    if idx not in (None, ""):
        s += f"  #{idx}"
    return s


def _flatten_env(data):
    """
    envCon 回傳（list[device] 或單一 dict）→ {mark: {value, oldValue, unit, alertFlag}}。
    保留 device 層 alertFlag（數位訊號狀態判斷用）；value 不在此翻譯（顯示時再 _zh）。
    """
    flat = {}
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for item in items:
        if not isinstance(item, dict):
            continue
        aflag = item.get("alertFlag")
        for m in item.get("metricsDataVoList") or []:
            if isinstance(m, dict) and m.get("mark"):
                flat[m["mark"]] = {"value": m.get("value"), "oldValue": m.get("oldValue"),
                                   "unit": m.get("unit"), "alertFlag": aflag}
    return flat


def _fmt_env(flat, mark):
    """envCon 數值欄位：value（字串，backend 已格式化）＋ unit；無資料回 '-'。"""
    md = flat.get(mark)
    if not md or md.get("value") in (None, ""):
        return "-"
    v, unit = md["value"], md.get("unit")
    return f"{v} {unit}" if unit else f"{v}"


def _zh_env(flat, mark):
    """envCon 狀態欄位：value 經 _zh() 中文化（如 workingMode→製冷/市電模式）；無資料回 '-'。"""
    md = flat.get(mark)
    if not md or md.get("value") in (None, ""):
        return "-"
    return _zh(md["value"])


def _pcs_onoff(flat):
    """PCS 啟停狀態：systemOnOrOffStatus 的 value → 停止/運轉（比照 UI）；無資料回 '-'。"""
    md = flat.get("systemOnOrOffStatus")
    if not md or md.get("value") in (None, ""):
        return "-"
    raw = md["value"]
    return _PCS_ONOFF_TEXT.get(raw) or _zh(raw)


def _dev_status(data):
    """裝置通訊/總狀態：device.alertFlag 恰為 False → 正常，否則異常（比照 controlOverview）。"""
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    if not items or not isinstance(items[0], dict):
        return "異常"
    return "正常" if items[0].get("alertFlag") is False else "異常"


def _io_normal(flat, mark):
    """ttyS0 數位訊號：正常 = oldValue=='1' 且 device.alertFlag 非 True（env-detail.js 邏輯）。"""
    md = flat.get(mark)
    if not md:
        return "-"
    good = str(md.get("oldValue")) == "1" and md.get("alertFlag") is not True
    return "正常" if good else "異常"


def _water_normal(flat, mark="alarm"):
    """水浸（envCon/water alarm）：正常 = oldValue=='0'（env-detail.js 邏輯）。"""
    md = flat.get(mark)
    if not md:
        return "-"
    return "正常" if str(md.get("oldValue")) == "0" else "異常"


def _cooling_status(pump_data):
    """冷卻循環系統狀態（envCon/waterPumpStateRead）：比照 controlOverview 的 !k.value → 正常/異常。"""
    if pump_data is None:
        return "異常"
    if isinstance(pump_data, bool):
        return "正常" if not pump_data else "異常"
    return "正常" if not pump_data else "異常"


def _combine_hilo(flat, hi, lo):
    """累計交流電量 = HighByte×65536 + LowByte（value 已含 ×0.1 縮放），組「值 單位」。"""
    h, l = flat.get(hi), flat.get(lo)
    if not h or not l:
        return "-"
    try:
        val = round(float(h["value"]) * 65536 + float(l["value"]), 3)   # round 去除浮點雜訊
    except (TypeError, ValueError):
        return "-"
    unit = l.get("unit") or h.get("unit") or ""
    return f"{_js_num(val)} {unit}" if unit else _js_num(val)


# ---------------- 相容工具（device_control_scraper 依賴，行為與舊版一致）----------------
def _flatten_metrics(data):
    """
    把 envCon 回傳的 list 攤平成 {mark: {"value": <已中文化值>, "unit", "oldValue"}}。
    狀態值（type.attr.*）先經 _zh() 套中文；數值/單位保留原樣，交由 _fmt_metric 組字串。
    （device_control_scraper 直接 import 使用，請維持此行為。）
    """
    flat = {}
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        for m in item.get("metricsDataVoList") or []:
            if not isinstance(m, dict):
                continue
            mark = m.get("mark")
            if not mark:
                continue
            flat[mark] = {"value": _zh(m.get("value", "")), "unit": m.get("unit"),
                          "oldValue": m.get("oldValue")}
    return flat


def _fmt_metric(md, decimals=None):
    """把 {"value","unit"} 組成顯示字串「value unit」；decimals 不為 None 時數值套固定小數位。"""
    if not isinstance(md, dict):
        return "N/A"
    value, unit = md.get("value"), md.get("unit")
    if decimals is not None:
        try:
            value = f"{float(value):.{decimals}f}"
        except (TypeError, ValueError):
            pass
    return f"{value} {unit}" if unit else f"{value}"


# ---------------- 抓取（api_client / 只用 GET）----------------
USERNAME = "hmiUser"
_client = None
_login_ok = False   # 最近一次登入是否成功（需登入端點如 getRunMode 是否可用）

# 需登入才能取得的欄位，登入失敗時顯示此標記（與「空值 -」語意區分：- = API 回空值/未知）
LOGIN_FAILED_TAG = "登入失敗（需 HMI 權限）"

# 終端黃色警告（Windows 10+ 主控台支援 ANSI；載入時嘗試啟用 VT，無害）
_YELLOW, _RESET = "\033[33m", "\033[0m"
try:
    os.system("")
except Exception:
    pass


def _get_client():
    """建立並快取 ApiClient（輪詢時只登入一次；記錄登入結果供資料來源標示/需登入欄位判斷）。"""
    global _client, _login_ok
    if _client is None:
        _client = ApiClient()
        _login_ok = bool(_client.login_hmi(USERNAME))
    return _client


def is_login_ok():
    """最近一次登入是否成功。"""
    return _login_ok


def get_data_by_api():
    """逐一 GET 所有數據概覽區塊；client.get 已解封包（code 0/200 → data），單支失敗回 None。"""
    client = _get_client()
    result = {}
    for name, spec in ENDPOINTS.items():
        path, params = spec if isinstance(spec, tuple) else (spec, None)
        result[name] = client.get(path, params=params)
    return result


# ---------------- 各區塊摘要（回傳有序 dict，供終端與 CSV 共用）----------------
def summarize_battery(data):
    """一、電池概覽 → {基本資訊, Rack容量資訊, Rack極端值資訊}（Rack 兩區另取對應 endpoint）。"""
    basic = data.get("電池概覽")
    cap = data.get("Rack容量資訊")
    ext = data.get("Rack極端值資訊")
    return {
        "基本資訊": {label: _fmt_overview(basic, key, comma=(key in _INSULATION_KEYS))
                     for label, key in BATTERY_BASIC},
        "Rack容量資訊": {label: _fmt_overview(cap, key) for label, key in RACK_CAPACITY},
        "Rack極端值資訊": {label: _fmt_extreme(ext, key) for label, key in RACK_EXTREME},
    }


def summarize_pcs(data, login_ok=True):
    """二、PCS概覽 → {系統資訊, 電量統計資訊, 直流資訊, 交流資訊}。
    login_ok=False（HMI 登入失敗）時，需登入的「工作模式」（排程/手動來自 getRunMode）標為 <登入失敗>；
    其餘欄位（啟停/併網離網/充放電/電量/直流/交流）皆為免登入 guest 端點的即時 LIVE 值，照常顯示。"""
    flat = _flatten_env(data.get("PCS概覽"))
    runmode = data.get("getRunMode")

    def old(m):
        return str((flat.get(m) or {}).get("oldValue") or "")

    # 系統資訊
    on_off = _pcs_onoff(flat)                                   # 啟停狀態（停止/運轉）
    if old("systemOffGridStatus") == "1":                      # 併網/離網模式（以 oldValue 判斷）
        grid = "離網"
    elif old("systemGridTiedStatus") == "1":
        grid = "併網"
    else:
        grid = "-"
    # 工作模式：交/直（guest）＋ 排程/手動（來自需登入的 getRunMode）。登入失敗→整欄標 <登入失敗>
    if not login_ok:
        working = LOGIN_FAILED_TAG
    else:
        ed = old("energyDispatchingMode")
        acdc = "交流" if ed == "0" else "直流" if ed == "1" else ""
        rm = runmode.strip().lower() if isinstance(runmode, str) else ""
        suffix = "排程" if rm in _RUNMODE_SMART else "手動" if rm in _RUNMODE_MANUAL else ""
        working = "離網" if grid == "離網" else ((acdc + suffix) or "-")
    if old("systemChargingStatus") == "1":                     # 充/放電狀態
        chg = "充電"
    elif old("systemDischargingStatus") == "1":
        chg = "放電"
    else:
        chg = "-"

    system = {
        "啟停狀態": on_off,
        "併網/離網模式": grid,
        "工作模式": working,
        "充/放電狀態": chg,
    }
    energy = {
        # UI 綁定 LowByte 的 value（原樣顯示，含小數位）；High/Low 合併值僅供 [DEBUG]，不放正式輸出
        "累計交流放電電量": _fmt_env(flat, "accumulatedAcDischargeLowByte"),
        "累計交流充電電量": _fmt_env(flat, "accumulatedAcChargingPowerLowByte"),
        "當天交流放電電量": _fmt_env(flat, "dailyDischargedEnergyThroughAcPort"),
        "當天交流充電電量": _fmt_env(flat, "dailyChargedEnergyThroughAcPort"),
    }
    dc = {label: _fmt_env(flat, mark) for label, mark in PCS_DC}
    ac = {label: _fmt_env(flat, mark) for label, mark in PCS_AC}
    return {"系統資訊": system, "電量統計資訊": energy, "直流資訊": dc, "交流資訊": ac}


def summarize_env(data):
    """三、環控概覽 → 依 UI 順序的卡片 dict（每卡片為 {欄位: 值}）。"""
    ttys0 = _flatten_env(data.get("環控_串口ttyS0"))
    water = _flatten_env(data.get("環控_水系統"))
    air = _flatten_env(data.get("環控_空調"))
    ups = _flatten_env(data.get("環控_UPS"))
    multi = _flatten_env(data.get("多功能傳感器"))
    volt = _flatten_env(data.get("AC380電量儀"))

    # 空調執行狀態：envicoolEdition=="103" 用 workingMode，否則 equipmentWorkingStatus（controlOverview 邏輯）
    edition = str((air.get("envicoolEdition") or {}).get("value") or "")
    air_run = _zh_env(air, "workingMode") if edition == "103" else _zh_env(air, "equipmentWorkingStatus")

    cards = {
        "急停按鈕": {"狀態": _io_normal(ttys0, "emergencyButton")},
        "閉門器":   {"狀態": _io_normal(ttys0, "accessControl")},
        "水浸":     {"狀態": _water_normal(water)},
        "SPD":      {"狀態": _io_normal(ttys0, "SPD")},
        "消防": {
            "消防報警": _io_normal(ttys0, "fireAlarm"),
            "消防故障": _io_normal(ttys0, "fireMalfunction"),
        },
        "冷卻循環系統": {"狀態": _cooling_status(data.get("冷卻循環_泵狀態"))},
        "空調": {
            "狀態": _dev_status(data.get("環控_空調")),
            "執行狀態": air_run,
            "設定溫度": _fmt_env(air, "coolingSetTemperature"),
        },
        "UPS": {
            "狀態": _dev_status(data.get("環控_UPS")),
            "工作模式": _zh_env(ups, "workingMode"),
            "電池容量": _fmt_env(ups, "batteryCapacity"),
        },
        "多功能傳感器": dict(
            [("狀態", _dev_status(data.get("多功能傳感器")))]
            + [(label, _fmt_env(multi, mark)) for label, mark in MULTIFUNCTION]
        ),
        "AC 380 電量儀": dict(
            [("狀態", _dev_status(data.get("AC380電量儀")))]
            + [(label, _fmt_env(volt, mark)) for label, mark in AC380]
        ),
    }
    return cards


def build_overview(data, login_ok=True):
    """把一輪 data 整理成數據概覽三大區塊（有序）；終端與 CSV 皆以此為單一來源。"""
    return {
        "一、電池概覽": summarize_battery(data),
        "二、PCS概覽": summarize_pcs(data, login_ok),
        "三、環控概覽": summarize_env(data),
    }


# ---------------- 輸出：console 摘要 ----------------
def pcs_accum_debug(data):
    """[DEBUG] 累計交流放/充電的 High×65536+Low 原始合併值（僅除錯用，正式輸出不顯示）。"""
    flat = _flatten_env(data.get("PCS概覽"))
    return {
        "累計交流放電電量原始合併值": _combine_hilo(
            flat, "accumulatedAcDischargePowerHighByte", "accumulatedAcDischargeLowByte"),
        "累計交流充電電量原始合併值": _combine_hilo(
            flat, "accumulatedAcChargingPowerHighByte", "accumulatedAcChargingPowerLowByte"),
    }


def print_summary(record):
    """把一輪 record 印成數據概覽摘要（只印 UI 有顯示的欄位），並於開頭標示資料來源。"""
    source = record.get("source", "LIVE(API)")
    login_ok = record.get("login_ok", True)

    print("=" * 56)
    # 抓取全失敗且無快取 → 不印可能過期的設備資訊，直接顯示無資料
    if source is None:
        print(f"時間：{record['timestamp']}")
        print(f"{_YELLOW}登入失敗／無法連線，無法取得最新資料（且無可用快取）。{_RESET}")
        return

    # 資料來源標頭（LIVE=即時 API；CACHE=即時抓取失敗時改讀上一輪 JSON，可能過期）
    if source == "CACHE(JSON)":
        print(f"{_YELLOW}資料來源：CACHE(JSON) @ {record.get('cache_time', '?')}"
              f"（⚠ 可能過期：本次即時抓取失敗，顯示上一輪快照）{_RESET}")
    else:
        print(f"資料來源：LIVE(API) @ {record['timestamp']}")

    # 登入失敗警告：guest 資料仍為即時 LIVE，但需登入欄位無法取得
    if not login_ok:
        print(f"{_YELLOW}⚠ HMI 登入失敗：目前使用 Guest API，部分需登入欄位無法取得。{_RESET}")

    overview = build_overview(record["data"], login_ok)
    for section, groups in overview.items():
        print(f"\n【{section}】")
        for group, fields in groups.items():
            print(f"  ● {group}")
            for label, value in fields.items():
                print(f"      {label}：{value}")
    # 除錯用：High/Low 原始合併值（預設關閉，不影響正式與 UI 一致的輸出）
    if SHOW_DEBUG_RAW:
        for label, val in pcs_accum_debug(record["data"]).items():
            print(f"[DEBUG] {label}：{val}")


# ---------------- 輸出：JSON（完整原始快照）----------------
def save_to_json(record, path=JSON_PATH):
    """把一輪完整原始資料寫成 JSON（每輪覆寫，永遠是最新快照；含 UI 未顯示的原始資料）。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  [警告] 寫入 JSON 失敗：{e}")


# ---------------- 輸出：CSV（每輪摘要時間序列）----------------
def build_summary_row(record):
    """把數據概覽攤平成單層 dict（欄位帶「區塊_群組_欄位」前綴），供 CSV 時間序列用。"""
    overview = build_overview(record["data"], record.get("login_ok", True))
    row = {"timestamp": record["timestamp"]}
    for section, groups in overview.items():
        sec = section.split("、")[-1]                       # 去掉「一、」等序號前綴
        for group, fields in groups.items():
            for label, value in fields.items():
                row[f"{sec}_{group}_{label}"] = value
    return row


def save_to_csv(row, path=CSV_PATH):
    """
    逐輪把摘要附加一列（固定欄位＝本 row 的鍵、順序固定）。
    既有檔案表頭與目前欄位不符（含舊版欄位）時，改以目前欄位重建檔案，保留仍相容的舊列。
    使用 utf-8-sig 讓 Excel 正確辨識中文。
    """
    fields = list(row.keys())
    try:
        existing_header = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                existing_header = next(csv.reader(f), [])

        fresh = existing_header != fields
        old_rows = []
        if not fresh and os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                old_rows = list(csv.DictReader(f))

        mode = "w" if fresh else "a"
        with open(path, mode, encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", restval="")
            if fresh:
                writer.writeheader()
                for r in old_rows:
                    writer.writerow({k: r.get(k, "") for k in fields})
            writer.writerow({k: row.get(k, "") for k in fields})
    except OSError as e:
        print(f"  [警告] 寫入 CSV 失敗：{e}")


# ---------------- 主流程 / 背景循環控制 ----------------
LOOP_THREAD_NAME = "dashboard-fetch-loop"
_LOCK_PATH = os.path.join(_OUTPUT_DIR, ".dashboard_loop.lock")  # 跨程序鎖：避免多程序同時背景寫入

_loop_thread = None                 # 目前背景抓取 Thread（None = 未執行）
_loop_stop = threading.Event()      # 停止旗標；set() 即要求循環結束
_loop_guard = threading.Lock()      # 保護 start/stop 臨界區（避免競態、重複建立）


# 需登入才能取得的區塊（其餘皆為免登入 guest 端點，登入失敗仍為 LIVE）
_LOGIN_REQUIRED_KEYS = {"getRunMode"}


def _has_live(data):
    """是否至少有一個 guest 區塊取得有效即時資料（排除 None 與 {_error}）。"""
    for k, v in (data or {}).items():
        if k in _LOGIN_REQUIRED_KEYS or v is None:
            continue
        if isinstance(v, dict) and v.get("_error"):
            continue
        return True
    return False


def _load_cache(path=JSON_PATH):
    """讀取上一輪寫入的 dashboard_data.json（即時抓取全失敗時的 fallback）；無/損毀回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        if isinstance(rec, dict) and isinstance(rec.get("data"), dict):
            return rec
    except (OSError, ValueError):
        return None
    return None


def collect_once():
    """
    抓一輪並標示資料來源：
      - LIVE(API)：至少一個 guest 區塊取得即時資料（登入失敗仍算 LIVE，因 guest 免登入）。
      - CACHE(JSON)：guest 全部抓取失敗 → 改讀上一輪 dashboard_data.json（明確標記、可能過期）。
      - None：抓取全失敗且無快取 → print_summary 顯示「無法取得最新資料」。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = get_data_by_api()
    login_ok = is_login_ok()
    if _has_live(data):
        return {"timestamp": now, "data": data, "login_ok": login_ok, "source": "LIVE(API)"}
    cached = _load_cache()
    if cached:
        return {"timestamp": now, "data": cached.get("data", {}), "login_ok": login_ok,
                "source": "CACHE(JSON)", "cache_time": cached.get("timestamp")}
    return {"timestamp": now, "data": {}, "login_ok": login_ok, "source": None}


def fetch_dashboard():
    """抓一次並輸出到 console / JSON / CSV；回傳 record（單次與循環共用）。"""
    record = collect_once()
    if OUTPUT_TO_CONSOLE:
        print_summary(record)
    # 只有 LIVE 才覆寫 JSON/CSV：CACHE（讀舊檔）不回寫、無資料不寫，避免以舊蓋新或污染時間序列
    if record.get("source") == "LIVE(API)":
        if OUTPUT_TO_JSON:
            save_to_json(record)
        if OUTPUT_TO_CSV:
            save_to_csv(build_summary_row(record))
    return record


# ---- 跨程序 PID 鎖（Windows 以 tasklist 判斷存活；判斷不了則保守放行）----
def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=5)
        return str(pid) in (out.stdout or "")
    except Exception:
        return True   # 不確定就當作存活（保守，避免搶鎖造成雙寫）


def _read_lock_pid():
    try:
        with open(_LOCK_PATH, "r", encoding="utf-8") as f:
            return int((f.read().strip() or "0"))
    except (OSError, ValueError):
        return 0


def _acquire_process_lock():
    """取得跨程序鎖：其他存活程序持鎖→失敗；陳舊鎖自動接管。鎖檔操作失敗則降級放行。"""
    try:
        if os.path.exists(_LOCK_PATH):
            pid = _read_lock_pid()
            if pid and pid != os.getpid() and _pid_alive(pid):
                return False
        with open(_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return True


def _release_process_lock():
    try:
        if os.path.exists(_LOCK_PATH) and _read_lock_pid() in (0, os.getpid()):
            os.remove(_LOCK_PATH)
    except OSError:
        pass


def _loop_run():
    """背景循環本體：while not stop_event → fetch → stop_event.wait(interval)（不使用 time.sleep）。"""
    interval = max(1, INTERVAL_SECONDS)     # 下限 1 秒，避免 INTERVAL_SECONDS=0 狂打 API
    while not _loop_stop.is_set():
        try:
            fetch_dashboard()
        except Exception as e:              # 單輪例外不終止整個循環
            print(f"[Dashboard] 背景抓取例外：{e}")
        _loop_stop.wait(interval)           # 可被 stop_dashboard_loop() 立即喚醒


def start_dashboard_loop():
    """
    啟動背景抓取循環（單例）：
      - 已在執行 → 印「Dashboard 自動抓取已在執行」，不建立第二個 Thread。
      - 其他程序持鎖 → 略過（避免多程序同時寫 dashboard_data.json）。
      - 啟動前 stop_event.clear()，建立 daemon Thread（名稱 dashboard-fetch-loop）。
    回傳 True=本次成功啟動；False=未啟動。
    """
    global _loop_thread
    with _loop_guard:
        if _loop_thread is not None and _loop_thread.is_alive():
            print("Dashboard 自動抓取已在執行")
            return False
        if not _acquire_process_lock():
            print(f"Dashboard 自動抓取已由其他程序（PID {_read_lock_pid()}）執行中，略過啟動")
            return False
        _loop_stop.clear()
        _loop_thread = threading.Thread(target=_loop_run, name=LOOP_THREAD_NAME, daemon=True)
        _loop_thread.start()
    print(f"Dashboard 自動抓取已啟動（thread={LOOP_THREAD_NAME}，間隔 {max(1, INTERVAL_SECONDS)} 秒）")
    return True


def stop_dashboard_loop():
    """停止背景循環：stop_event.set() → 等待 Thread 結束 → 清除 Thread 與跨程序鎖。可安全重複呼叫。"""
    global _loop_thread
    with _loop_guard:
        th = _loop_thread
        if th is None or not th.is_alive():
            _loop_thread = None
            _release_process_lock()
            return False
        _loop_stop.set()
    th.join(timeout=max(1, INTERVAL_SECONDS) + TIMEOUT + 5)
    _loop_thread = None
    _release_process_lock()
    print("Dashboard 自動抓取已停止")
    return True


# 程式結束（正常/例外/atexit）時保證停止背景循環，不留背景 Thread 或鎖檔。
atexit.register(stop_dashboard_loop)


def main():
    """
    CLI 模式：
      --once  抓一次後結束（永遠可用，忽略 ENABLE_AUTO_FETCH）。
      --loop  啟動背景循環，直到 Ctrl+C 才 stop_dashboard_loop() 乾淨停止。
      無參數  依 ENABLE_AUTO_FETCH：True→自動背景循環；False→單次抓取。
    """
    parser = argparse.ArgumentParser(description="Dashboard 數據概覽抓取")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="抓一次後結束")
    group.add_argument("--loop", action="store_true", help="背景循環，每 INTERVAL_SECONDS 一次直到 Ctrl+C")
    args = parser.parse_args()

    loop_mode = args.loop or (not args.once and ENABLE_AUTO_FETCH)

    if not loop_mode:                       # --once，或無參數且 ENABLE_AUTO_FETCH=False
        fetch_dashboard()
        return

    # Ctrl+Break（Windows SIGBREAK）也比照 Ctrl+C 觸發 KeyboardInterrupt → 乾淨停止
    import signal
    try:
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    except (AttributeError, ValueError):
        pass                                 # 非 Windows 或非主執行緒：略過

    print(f"開始背景抓取，baseURL={BASE_URL}，間隔 {max(1, INTERVAL_SECONDS)} 秒（Ctrl+C 停止）")
    if not start_dashboard_loop():
        return                              # 已有其他實例在跑，不重複啟動
    try:
        while _loop_thread is not None and _loop_thread.is_alive():
            _loop_thread.join(timeout=0.5)   # 分段 join，讓 Ctrl+C 可即時中斷
    except KeyboardInterrupt:
        print("\n使用者中斷（Ctrl+C）…")
    finally:
        stop_dashboard_loop()


if __name__ == "__main__":
    main()
