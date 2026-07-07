# -*- coding: utf-8 -*-
"""
內網 Dashboard 抓取工具 — 正式版（第一階段：Data Overview 整頁）
==================================================================
核心解析邏輯整合自已驗證的 dashboard_min.py。本階段只做「數據概覽 Data Overview」
整頁，不碰其他 tab。

【已驗證結論（前端 JS 靜態分析 + 實際回傳）】
  - baseURL = http://192.168.128.110:8080/admin-api （API 在同主機 8080）
  - 免登入、免 tenant-id，全部 GET。
  - overview/* 回傳扁平 dict（key + <key>Unit）。
  - envCon/*   回傳 list，內含 metricsDataVoList（{mark,value,unit}）。
  - alarm/list 回傳 {total, rows, code, msg}。

【欄位信心度】
  - 電池主資訊、PCS 四組、空調、告警：已用實際 JSON 校正（curated）。
  - Rack 容量/極值、水系統、UPS、ttyS0、多功能傳感器、AC380 電量儀：
    尚無實際回傳，改用「通用攤平器」帶出 API 真實欄位（不硬猜），
    待拿到真資料後再做 curated 對應。

【限制】只用 requests GET；不做 POST/PUT/DELETE；不碰設備控制。

執行方式：
    pip install requests
    python dashboard_scraper.py
"""

import os
import csv
import json
from datetime import datetime

import requests


# ======================================================================
# 設定區
# ======================================================================
BASE_URL = "http://192.168.128.110:8080/admin-api"   # 已驗證；如 8853 有代理可改成 :8853
TIMEOUT = 8                 # 單支 API 逾時秒數
INTERVAL_SECONDS = 10       # 每 N 秒抓一次
MAX_LOOPS = 0               # 最多抓幾輪；0 = 無限（Ctrl+C 停止）

# 輸出設定
OUTPUT_TO_CONSOLE = True
OUTPUT_TO_JSON = True
OUTPUT_TO_CSV = True
JSON_PATH = "dashboard_data.json"   # 完整原始資料（每輪覆寫最新快照）
CSV_PATH = "dashboard_data.csv"     # 每輪摘要（附加一列，累積時間序列）

# 告警本次取回筆數（只影響顯示筆數，不影響 API 回傳的 total）
ALARM_PAGE_SIZE = 3

# ---- Data Overview 全部區塊 endpoint（method 皆為 GET）----
ENDPOINTS = {
    # 一、電池概覽
    "電池概覽":       "/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation",
    "Rack容量資訊":   "/hmiGuest/unauthorizedAccess/overview/rackCapacityInformation",
    "Rack極端值資訊": "/hmiGuest/unauthorizedAccess/overview/rackExtremeValueInformation",
    # 二、PCS 概覽
    "PCS概覽":        "/hmiGuest/unauthorizedAccess/envCon/pcs",
    # 三、環控概覽（6 張卡）
    "環控_空調":      "/hmiGuest/unauthorizedAccess/envCon/air",
    "環控_水系統":    "/hmiGuest/unauthorizedAccess/envCon/water",
    "環控_UPS":       "/hmiGuest/unauthorizedAccess/envCon/ups",
    "環控_串口ttyS0": "/hmiGuest/unauthorizedAccess/envCon/ttyS0",
    "多功能傳感器":   "/hmiGuest/unauthorizedAccess/envCon/multifunction",
    "AC380電量儀":    "/hmiGuest/unauthorizedAccess/envCon/voltameter",
    # 四、告警資訊
    "告警資訊":       ("/hmiGuest/unauthorizedAccess/alarm/list", {"pageNo": 1, "pageSize": ALARM_PAGE_SIZE}),
}

# 尚無實際回傳、改用通用攤平器（會帶出 API 真實欄位）的區塊
GENERIC_BLOCKS = {
    "Rack容量資訊", "Rack極端值資訊",
    "環控_水系統", "環控_UPS", "環控_串口ttyS0", "多功能傳感器", "AC380電量儀",
}

# ---- 電池概覽主資訊欄位（皆依實際 JSON key 確認）----
BATTERY_FIELDS = {
    "電壓": ["rackTotalBatteryVoltage"],                                     # V
    "電流": ["rackElectricCurrent"],                                         # A
    "R+":  ["rackInsulationResistanceRPositive"],                            # KΩ
    "R-":  ["rackInsulationResistanceRPositiveNegative"],                    # KΩ
    "SOC": ["rackSoc"],                                                      # %
    "SOH": ["rackSoh"],                                                      # %
}

# ---- 環控-空調欄位（envCon/air，以 mark 對應；已用實際 JSON 校正）----
ENV_FIELDS = {
    "執行狀態": "equipmentWorkingStatus",   # type.attr.run/stop
    "工作模式": "workingMode",              # type.attr.refrigeration/dehumidification/airSupply...
    "設定溫度": "coolingSetTemperature",
    "櫃內溫度": "cabinetTemperature",
    "櫃內濕度": "cabinetHumidity",
}

# ---- PCS 概覽（envCon/pcs）分組欄位；標題為顯示中文，值取自對應 mark（皆依實際 JSON 校正）----
PCS_GROUPS = {
    "系統資訊": {
        "啟停狀態":   "systemOnOrOffStatus",
        "併網/離網":  "systemGridTiedStatus",
        "充電狀態":   "systemChargingStatus",
        "放電狀態":   "systemDischargingStatus",
        "故障狀態":   "systemFaultStatus",
        "告警狀態":   "systemAlarmStatus",
        "控制模式":   "controlMode",
        "模組溫度":   "moduleTemperature",
        "環境溫度":   "ambientTemperature",
        "櫃內溫度":   "cabinetTemperature",
    },
    "電量統計資訊": {
        "交流總有功功率": "totalActivePowerOfAcBus",       # kW
        "交流總無功功率": "totalReactivePowerOfAcBus",     # kVar
        "交流總視在功率": "totalApparentPowerOfAcBus",     # kVA
        "可用有功容量":   "availableActivePowerCapacity",  # kW
        "當日充電電量":   "dailyChargedEnergyThroughAcPort",   # kWh
        "當日放電電量":   "dailyDischargedEnergyThroughAcPort", # kWh
    },
    "直流資訊": {
        "直流輸入電壓": "dcInputVoltage",   # V
        "直流電流":     "dcCurrent",        # A
        "直流功率":     "dcPower",          # kW
    },
    "交流資訊": {
        "AB線電壓":  "voltageOfAcBusLineAB",   # V
        "BC線電壓":  "voltageOfAcBusLineBC",   # V
        "CA線電壓":  "voltageOfAcBusLineCA",   # V
        "A相電流":   "currentOfAcBusLineA",    # A
        "B相電流":   "currentOfAcBusLineB",    # A
        "C相電流":   "currentOfAcBusLineC",    # A
        "母線頻率":  "acBusFrequency",         # Hz
    },
}

# ---- i18n 狀態值中文對照（value 為 type.attr.* 這類 key 時翻譯；查不到就保留原字串）----
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
# ======================================================================


# ---------------- 共用工具（沿用 dashboard_min.py）----------------
def _unwrap(payload):
    """
    解開 API 封包：code == 0/200 視為成功（優先回 data，無 data 回整包）；
    失敗保留 code/msg 方便除錯。
    """
    if isinstance(payload, dict) and "code" in payload:
        code = payload.get("code")
        if code in (0, 200):
            return payload.get("data", payload)
        return {"_error": code, "msg": payload.get("msg"), "_raw": payload}
    return payload


def _pick(data, keys, default="N/A"):
    """從 dict 依候選鍵順序取第一個存在的值；取不到回傳 default。"""
    if not isinstance(data, dict):
        return default
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return default


def _zh(value):
    """把 i18n 狀態值（type.attr.*）翻成中文；查不到或非字串就原樣回傳。"""
    if isinstance(value, str) and value in STATUS_MAP:
        return STATUS_MAP[value]
    return value


def _flatten_metrics(data):
    """
    把 envCon 回傳的 list 攤平成 {mark: {"value": <已翻譯值>, "unit": <單位或 None>}}。
    狀態值（type.attr.*）先經 _zh() 套中文；數值/單位保留原樣，交由 _fmt_metric 組字串。
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
            flat[mark] = {"value": _zh(m.get("value", "")), "unit": m.get("unit")}
    return flat


def _fmt_metric(md, decimals=None):
    """
    把 {"value","unit"} 組成顯示字串「value unit」。
    - decimals 不為 None 時，數值型 value 會格式化為固定小數位。
    - 無單位時不補；非數字值套小數位失敗則保留原值。
    """
    if not isinstance(md, dict):
        return "N/A"
    value, unit = md.get("value"), md.get("unit")
    if decimals is not None:
        try:
            value = f"{float(value):.{decimals}f}"
        except (TypeError, ValueError):
            pass
    return f"{value} {unit}" if unit else f"{value}"


# ---------------- 抓取（requests / 只用 GET）----------------
def get_data_by_api():
    """逐一 GET 所有 Data Overview 區塊，單支失敗不中斷，回傳 dict。"""
    session = requests.Session()
    session.headers.update({"Accept": "application/json, text/plain, */*"})

    result = {}
    for name, spec in ENDPOINTS.items():
        path, params = spec if isinstance(spec, tuple) else (spec, None)
        try:
            resp = session.get(BASE_URL + path, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            result[name] = _unwrap(resp.json())
        except requests.exceptions.RequestException as e:
            print(f"  [警告] 抓取「{name}」失敗：{e}")
            result[name] = None
        except ValueError:
            print(f"  [警告]「{name}」回傳非 JSON")
            result[name] = None
    return result


# ---------------- 摘要整理函式 ----------------
def summarize_battery(data):
    """整理電池概覽主資訊，並組上對應單位（<key>Unit）；無資料回傳 None。"""
    if not isinstance(data, dict) or data.get("_error"):
        return None
    result = {}
    for label, keys in BATTERY_FIELDS.items():
        val = _pick(data, keys)
        if val != "N/A":
            for k in keys:
                if k in data and data[k] is not None:
                    unit = data.get(k + "Unit")
                    if unit:
                        val = f"{val} {unit}"
                    break
        result[label] = val
    return result


def summarize_pcs(data):
    """
    整理 PCS 概覽，依 PCS_GROUPS 分成 系統/電量/直流/交流 四組。
    回傳 {群組: {欄位: 顯示值}}；無資料回傳 None。
    """
    if data is None or (isinstance(data, dict) and data.get("_error")):
        return None
    metrics = _flatten_metrics(data)
    if not metrics:
        return None
    grouped = {}
    for group, fields in PCS_GROUPS.items():
        grouped[group] = {label: _fmt_metric(metrics.get(mark)) for label, mark in fields.items()}
    return grouped


def summarize_env(data):
    """整理環控-空調重要欄位（curated）；無資料回傳 None。"""
    if data is None or (isinstance(data, dict) and data.get("_error")):
        return None
    metrics = _flatten_metrics(data)
    if not metrics:
        return None
    return {label: _fmt_metric(metrics.get(mark)) for label, mark in ENV_FIELDS.items()}


def summarize_generic(data):
    """
    通用攤平器（給尚未 curated 的區塊用）：帶出 API「實際回傳」的欄位，不硬猜。
    - envCon 型（list 或含 metricsDataVoList）：輸出 {mark: 顯示值}。
    - overview 型（扁平 dict）：key 配對 <key>Unit 輸出 {key: 顯示值}。
    無資料回傳 None。
    """
    if data is None or (isinstance(data, dict) and data.get("_error")):
        return None

    # envCon 型
    if isinstance(data, list) or (isinstance(data, dict) and "metricsDataVoList" in data):
        metrics = _flatten_metrics(data)
        return {mark: _fmt_metric(md) for mark, md in metrics.items()} or None

    # overview 扁平 dict 型
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if v is None or k.endswith("Unit") or k.startswith("_"):
                continue
            unit = data.get(k + "Unit")
            v = _zh(v)
            out[k] = f"{v} {unit}" if unit else v
        return out or None
    return None


def summarize_alarm(data):
    """
    整理告警資訊。回傳 (total, active_in_batch, brief)：
    - total：alarm/list API 回傳的「總筆數」欄位（rows 結構中的 total）。
             是資料庫端告警總數，非程式計算，也不受 pageSize 影響。
    - active_in_batch：本次「取回這批資料」中 alarmStatus 為真的筆數。
             ⚠️ 僅代表本次取回資料中的啟用數，不是全部 total 筆的真實啟用數。
    - brief：本次取回資料的前 3 筆精簡欄位。
    無資料時回傳 (None, 0, [])。
    """
    if isinstance(data, dict) and not data.get("_error"):
        items = data.get("rows") or data.get("list") or data.get("records") or []
        total = data.get("total", len(items))   # total 直接取自 API 回傳欄位，非程式計算
    elif isinstance(data, list):
        items, total = data, len(data)
    else:
        return None, 0, []

    active_in_batch = sum(
        1 for a in items
        if isinstance(a, dict) and a.get("alarmStatus") in (True, 1, "1", "true", "True")
    )

    brief = []
    for a in items[:3]:
        if not isinstance(a, dict):
            continue
        brief.append({
            "typeMark":    a.get("typeMark", "N/A"),
            "targetMark":  a.get("targetMark", "N/A"),
            "level":       a.get("level", "N/A"),
            "alarmStatus": a.get("alarmStatus", "N/A"),
            "val":         a.get("val", "N/A"),
        })
    return total, active_in_batch, brief


# ---------------- 輸出：console 摘要 ----------------
def _print_kv(mapping, indent="    "):
    """印出 {label: value}；mapping 為 None 時印「無資料」。"""
    if not mapping:
        print(f"{indent}無資料")
        return
    for k, v in mapping.items():
        print(f"{indent}{k}: {v}")


def print_summary(record):
    """把一輪 record 印成人看的摘要（不印整包 JSON）。"""
    data = record["data"]
    print("=" * 56)
    print(f"時間：{record['timestamp']}")

    # 一、電池概覽
    print("\n【一、電池概覽】")
    print("  ● 主資訊")
    _print_kv(summarize_battery(data.get("電池概覽")), indent="      ")
    print("  ● Rack 容量資訊")
    _print_kv(summarize_generic(data.get("Rack容量資訊")), indent="      ")
    print("  ● Rack 極端值資訊")
    _print_kv(summarize_generic(data.get("Rack極端值資訊")), indent="      ")

    # 二、PCS 概覽
    print("\n【二、PCS概覽】")
    pcs = summarize_pcs(data.get("PCS概覽"))
    if not pcs:
        print("    無資料")
    else:
        for group, fields in pcs.items():
            print(f"  ● {group}")
            _print_kv(fields, indent="      ")

    # 三、環控概覽
    print("\n【三、環控概覽】")
    print("  ● 空調")
    _print_kv(summarize_env(data.get("環控_空調")), indent="      ")
    for card, block in (("水冷/水系統", "環控_水系統"), ("UPS", "環控_UPS"),
                        ("串口 ttyS0", "環控_串口ttyS0"), ("多功能傳感器", "多功能傳感器"),
                        ("AC380 電量儀", "AC380電量儀")):
        print(f"  ● {card}")
        _print_kv(summarize_generic(data.get(block)), indent="      ")

    # 四、告警資訊
    print("\n【四、告警資訊】")
    total, active_in_batch, brief = summarize_alarm(data.get("告警資訊"))
    if total is None:
        print("    無資料")
    else:
        print(f"    告警總數（API total）：{total}")
        print(f"    目前啟用中的告警數（alarmStatus=True，僅本次取回資料）：{active_in_batch}")
        print(f"    本次顯示前 {len(brief)} 筆：")
        if not brief:
            print("      （無告警項目）")
        for i, a in enumerate(brief, 1):
            print(f"      [{i}] typeMark={a['typeMark']} targetMark={a['targetMark']} "
                  f"level={a['level']} alarmStatus={a['alarmStatus']} val={a['val']}")


# ---------------- 輸出：JSON（完整原始快照）----------------
def save_to_json(record, path=JSON_PATH):
    """把一輪完整原始資料寫成 JSON（每輪覆寫，永遠是最新快照）。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  [警告] 寫入 JSON 失敗：{e}")


# ---------------- 輸出：CSV（每輪摘要時間序列）----------------
def build_summary_row(record):
    """把一輪 record 攤平成單層摘要 dict（給 CSV 用），欄位帶區塊前綴以免碰撞。"""
    data = record["data"]
    row = {"timestamp": record["timestamp"]}

    batt = summarize_battery(data.get("電池概覽"))
    if batt:
        for k, v in batt.items():
            row[f"電池_{k}"] = v

    for block in ("Rack容量資訊", "Rack極端值資訊"):
        g = summarize_generic(data.get(block))
        if g:
            for k, v in g.items():
                row[f"{block}_{k}"] = v

    pcs = summarize_pcs(data.get("PCS概覽"))
    if pcs:
        for group, fields in pcs.items():
            for k, v in fields.items():
                row[f"PCS_{group}_{k}"] = v

    env = summarize_env(data.get("環控_空調"))
    if env:
        for k, v in env.items():
            row[f"環控空調_{k}"] = v

    for block in ("環控_水系統", "環控_UPS", "環控_串口ttyS0", "多功能傳感器", "AC380電量儀"):
        g = summarize_generic(data.get(block))
        if g:
            for k, v in g.items():
                row[f"{block}_{k}"] = v

    total, active_in_batch, _ = summarize_alarm(data.get("告警資訊"))
    row["告警_API總數"] = total
    row["告警_本批啟用數"] = active_in_batch
    return row


def save_to_csv(row, path=CSV_PATH):
    """
    逐輪把摘要附加一列到 CSV。欄位以聯集累積；新欄位出現時自動重寫表頭。
    使用 utf-8-sig 讓 Excel 正確辨識中文。
    """
    try:
        existing_header = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                existing_header = next(csv.reader(f), [])

        header = list(existing_header)
        for k in row:
            if k not in header:
                header.append(k)

        rewrite = header != existing_header
        old_rows = []
        if rewrite and os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                old_rows = list(csv.DictReader(f))

        mode = "w" if rewrite else "a"
        with open(path, mode, encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            if rewrite:
                writer.writeheader()
                for r in old_rows:
                    writer.writerow(r)
            writer.writerow({k: row.get(k, "") for k in header})
    except OSError as e:
        print(f"  [警告] 寫入 CSV 失敗：{e}")


# ---------------- 主流程 ----------------
def collect_once():
    """抓一輪，回傳 record。"""
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": get_data_by_api(),
    }


def main():
    """依設定每 N 秒抓一次，並輸出到 console / JSON / CSV。"""
    import time  # 僅主迴圈需要

    print(f"開始抓取，baseURL={BASE_URL}，間隔 {INTERVAL_SECONDS} 秒（Ctrl+C 停止）")
    loops = 0
    try:
        while True:
            record = collect_once()

            if OUTPUT_TO_CONSOLE:
                print_summary(record)
            if OUTPUT_TO_JSON:
                save_to_json(record)
            if OUTPUT_TO_CSV:
                save_to_csv(build_summary_row(record))

            loops += 1
            if MAX_LOOPS and loops >= MAX_LOOPS:
                print(f"\n已達設定輪數 {MAX_LOOPS}，結束。")
                break

            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n使用者中斷（Ctrl+C），程式結束。")


if __name__ == "__main__":
    main()
