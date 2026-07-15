# -*- coding: utf-8 -*-
"""
環控【環控數據】頁 scraper（只讀 GET）
=========================================
100% 對齊前端 environmental-control/index.vue（chunk: index-c634af2b.js）的實際渲染。

前端資料來源（皆 guest、GET，且帶 Accept-Language: zh-TW 讓 value 中文化）：
  ttyS0        /hmiGuest/unauthorizedAccess/envCon/ttyS0         （急停/閉門/消防/液位/水泵/SPD… 數位訊號，回傳「多台」裝置）
  water        /hmiGuest/unauthorizedAccess/envCon/water         （水浸 alarm）
  air          /hmiGuest/unauthorizedAccess/envCon/air           （空調；envicoolEdition 特例）
  ups          /hmiGuest/unauthorizedAccess/envCon/ups           （UPS）
  voltameter   /hmiGuest/unauthorizedAccess/envCon/voltameter    （AC 380 電量儀）
  multifunction/hmiGuest/unauthorizedAccess/envCon/multifunction （多功能傳感器）
  pumpState    /hmiGuest/unauthorizedAccess/envCon/waterPumpStateRead  （冷卻循環：系統狀態，回傳 bool）
  pumpSwitch   /hmiGuest/unauthorizedAccess/envCon/waterPumpRead       （冷卻循環：系統開關，回傳 bool）

前端渲染規則（見 index-c634af2b.js）：
  - ttyS0：t[mark] = (oldValue=="1" && !alertFlag)；airFail 特例 = (oldValue=="0")。
  - water：t.alarm = (oldValue=="0")。
  - air/ups/voltameter/multifunction：a[mark] = value(+unit)（用 value，非 oldValue）。
  - 通訊狀態：抓取成功→正常（voltameter 為 alertFlag===false）。
  - 顯示文字：狀態→正常/異常；液位→未觸發/觸發；水泵/系統開關→開啟/關閉；value 含 "FFFF"→超時。
  - 空調 envicoolEdition=="103"：equipmentWorkingStatus 改用 workingMode 值，且移除「出風溫度探頭/內風機」。

輸出：
  env_data.json   ：完整原始 API 資料（全部端點，含 pump）
  env_curated.json：UI 對齊摘要（只含 UI 實際顯示欄位）
  env_curated.csv ：UI 對齊欄位（單列）
  console          ：UI 對齊摘要
執行：python env_data_scraper.py
"""

import os
import csv
import json
from datetime import datetime

from api_client import ApiClient

# ---- 端點 ----
LANG_HEADER = {"Accept-Language": "zh-TW"}   # 讓 air/ups… 的 value 回中文（與前端一致）
GUEST = "/hmiGuest/unauthorizedAccess/envCon"
ENDPOINTS = {
    "ttyS0":         f"{GUEST}/ttyS0",
    "water":         f"{GUEST}/water",
    "air":           f"{GUEST}/air",
    "ups":           f"{GUEST}/ups",
    "voltameter":    f"{GUEST}/voltameter",
    "multifunction": f"{GUEST}/multifunction",
    "pumpState":     f"{GUEST}/waterPumpStateRead",   # 冷卻循環 系統狀態（bool）
    "pumpSwitch":    f"{GUEST}/waterPumpRead",        # 冷卻循環 系統開關（bool）
}

# ---- 狀態文字（取自前端 zh_TW locale，與 UI 逐字一致）----
TXT = {
    "normal": "正常", "abnormal": "異常",
    "open": "開啟", "close": "關閉",
    "not_triggered": "未觸發", "trigger": "觸發",
    "timeout": "超時",
}

# ---- 輸出路徑 ----
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
JSON_PATH = os.path.join(_OUTPUT_DIR, "env_data.json")
CURATED_JSON_PATH = os.path.join(_OUTPUT_DIR, "env_curated.json")
CURATED_CSV_PATH = os.path.join(_OUTPUT_DIR, "env_curated.csv")


# ================= 抓取 =================
def fetch_env_data(client):
    """逐一 GET（帶 zh-TW），回傳 {名稱: 原始資料或 None}。"""
    return {name: client.get(path, headers=LANG_HEADER) for name, path in ENDPOINTS.items()}


def _devlist(payload):
    d = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return [d]
    return []


def _flatten_bool(payload):
    """
    ttyS0：跨「多台」裝置攤平 → {mark: bool}。
    規則（前端）：t[mark] = (oldValue=="1" && !alertFlag)；airFail 特例 = (oldValue=="0")。
    回傳 (dict, ok)；ok=False 表示整包無資料（前端此時所有旗標視為 False/異常）。
    """
    devs = _devlist(payload)
    if not devs:
        return {}, False
    out = {}
    for dev in devs:
        if not isinstance(dev, dict):
            continue
        alert = dev.get("alertFlag") is True
        for m in dev.get("metricsDataVoList", []) or []:
            if not isinstance(m, dict) or not m.get("mark"):
                continue
            mk = m["mark"]
            ov = str(m.get("oldValue"))
            out[mk] = (ov == "0") if mk == "airFail" else (ov == "1" and not alert)
    return out, True


def _water_bool(payload):
    """water：{mark: bool}，前端規則 t[mark] = (oldValue=="0")（如 alarm oldValue==0 → 正常）。"""
    devs = _devlist(payload)
    out = {}
    for dev in devs:
        if not isinstance(dev, dict):
            continue
        for m in dev.get("metricsDataVoList", []) or []:
            if isinstance(m, dict) and m.get("mark"):
                out[m["mark"]] = str(m.get("oldValue")) == "0"
    return out


def _flatten_val(payload):
    """air/ups/voltameter/multifunction：{mark: 'value unit'}（用 value；無值→None）。"""
    devs = _devlist(payload)
    if not devs or not isinstance(devs[0], dict):
        return {}
    out = {}
    for m in devs[0].get("metricsDataVoList", []) or []:
        if not isinstance(m, dict) or not m.get("mark"):
            continue
        v = m.get("value")
        if v is None:
            out[m["mark"]] = None
        else:
            unit = m.get("unit")
            out[m["mark"]] = f"{v} {unit}" if unit else str(v)
    return out


def _comm_ok(payload, use_alert_flag=False):
    """通訊狀態：前端 air/ups/multifunction=抓取成功；voltameter=alertFlag===false。"""
    devs = _devlist(payload)
    if not devs:
        return False
    if use_alert_flag:
        return devs[0].get("alertFlag") is False
    return True


# ================= UI 對齊組裝 =================
def _val(val_map, mark):
    """value 欄：None→'-'；含 'FFFF'→超時；其餘原樣（已含單位/中文）。"""
    s = val_map.get(mark)
    if s is None or s is False:
        return "-"
    if "FFFF" in str(s):
        return TXT["timeout"]
    return s


def _norm(b):
    return TXT["normal"] if b else TXT["abnormal"]


def _trig(b):
    return TXT["not_triggered"] if b else TXT["trigger"]


def _oc(b):
    return TXT["open"] if b else TXT["close"]


def summarize_env_curated(data):
    """
    依前端 index.vue 的 N 設定與渲染規則，組出與 UI 完全一致的區塊/欄位/狀態/單位/順序。
    回傳 OrderedDict{區塊名: {欄位: 顯示值}}（區塊無資料 → None）。
    """
    tty, tty_ok = _flatten_bool(data.get("ttyS0"))
    water = _water_bool(data.get("water"))   # 前端 water 規則：t.alarm = (oldValue=="0")
    air = _flatten_val(data.get("air"))
    ups = _flatten_val(data.get("ups"))
    vm = _flatten_val(data.get("voltameter"))
    mf = _flatten_val(data.get("multifunction"))

    comm = {
        "air": _comm_ok(data.get("air")),
        "ups": _comm_ok(data.get("ups")),
        "multifunction": _comm_ok(data.get("multifunction")),
        "voltameter": _comm_ok(data.get("voltameter"), use_alert_flag=True),
    }

    # pump：data 直接是 bool（系統狀態 True→異常；系統開關 True→開啟）
    def _pump(name):
        p = data.get(name)
        return p.get("data") if isinstance(p, dict) else p

    pump_state = bool(_pump("pumpState"))
    pump_switch = bool(_pump("pumpSwitch"))

    # ttyS0 旗標取值：整包失敗→False(異常)；成功但缺該 mark→沿用前端初值 True(正常)
    def tf(mark):
        if not tty_ok:
            return False
        return tty.get(mark, True)

    # 空調 edition 特例
    ed103 = str(air.get("envicoolEdition") or "").split()[0] == "103"
    air_state_mark = "workingMode" if ed103 else "equipmentWorkingStatus"
    air_fields = [
        ("通訊狀態", _norm(comm["air"])),
        ("當前狀態", _val(air, air_state_mark)),
        ("溫度", _val(air, "cabinetTemperature")),
        ("濕度", _val(air, "cabinetHumidity")),
        ("壓縮機", _val(air, "compressorStatus")),
        ("融霜探頭", _val(air, "frostProbeMalfunction")),
        ("冷凝溫度探頭", _val(air, "condensationTemperatureProbeFault")),
        ("櫃內溫度探頭", _val(air, "cabinetWithinTemperatureProbeFault")),
    ]
    if not ed103:
        air_fields.append(("出風溫度探頭", _val(air, "airOutTemperatureProbefault")))
    air_fields.append(("溼度探頭", _val(air, "humidityProbeFault")))
    if not ed103:
        air_fields.append(("內風機", _val(air, "internalFanFault")))

    curated = {
        "急停按鈕": [("狀態", _norm(tf("emergencyButton")))],
        "閉門器": [("狀態", _norm(tf("accessControl")))],
        "消防": [("消防報警", _norm(tf("fireAlarm"))),
                 ("消防故障", _norm(tf("fireMalfunction")))],
        "多功能傳感器": [("通訊狀態", _norm(comm["multifunction"])),
                        ("溫度", _val(mf, "temperature")),
                        ("濕度", _val(mf, "humidity")),
                        ("甲烷濃度", _val(mf, "ch4")),
                        ("氫氣濃度", _val(mf, "h2"))],
        "冷卻循環系統": [("系統狀態", _norm(not pump_state)),   # 前端：d.value?異常:正常
                        ("系統開關", _oc(pump_switch)),         # 前端：y.value?開啟:關閉
                        ("液位低", _trig(tf("liquidLow"))),
                        ("液位高", _trig(tf("liquidHigh"))),
                        ("水泵", _oc(tf("waterPumpOn")))],
        "水浸": [("狀態", _norm(water.get("alarm", False)))],
        "SPD防雷電涌保護": [("狀態", _norm(tf("SPD")))],
        "UPS": [("通訊狀態", _norm(comm["ups"])),
                ("工作模式", _val(ups, "workingMode")),
                ("電池容量", _val(ups, "batteryCapacity")),
                ("電池電壓", _val(ups, "batteryVoltage")),
                ("輸出電壓", _val(ups, "outputPhaseVoltage")),
                ("輸出電流", _val(ups, "outputPhaseCurrent")),
                ("故障資訊", _val(ups, "emergencyStopStatus"))],
        "AC 380 電量儀": [("通訊狀態", _norm(comm["voltameter"])),
                          ("相電壓AB", _val(vm, "phaseVoltageAB")),
                          ("相電壓BC", _val(vm, "phaseVoltageBC")),
                          ("相電壓CA", _val(vm, "phaseVoltageCA")),
                          ("相電流A", _val(vm, "phaseCurrentA")),
                          ("相電流B", _val(vm, "phaseCurrentB")),
                          ("相電流C", _val(vm, "phaseCurrentC")),
                          ("總視在功率", _val(vm, "totalApparentPower")),
                          ("功率因數", _val(vm, "totalPowerFactor"))],
        "空調": air_fields,
    }
    # 轉為 {區塊: {欄位: 值}}（保留插入順序）
    return {name: dict(fields) for name, fields in curated.items()}


# ================= 輸出 =================
def print_curated(curated):
    print("=" * 72)
    print('\033[33m⫸環控數據清單⫷ \033[0m')
    for block_name, fields in curated.items():
        print()
        print(f"【{block_name}】")
        if not fields:
            print("  (無資料)")
            continue
        # 每個區塊依最長欄位名稱自動決定寬度（不用固定值）
        width = max(len(str(label)) for label in fields)
        for label, val in fields.items():
            print(f"  {label:<{width}} : {val}")


def build_env_curated_row(curated):
    """CSV：攤平成單列（欄位＝區塊_欄位）。"""
    row = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for block_name, fields in curated.items():
        for label, val in fields.items():
            row[f"{block_name}_{label}"] = val
    return row


def save_curated(curated, json_path=CURATED_JSON_PATH, csv_path=CURATED_CSV_PATH):
    record = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "curated": curated}
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"\ncurated 摘要已寫入：{json_path}")
    except OSError as e:
        print(f"  [警告] 寫入 curated JSON 失敗：{e}")

    row = build_env_curated_row(curated)
    try:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            writer.writeheader()
            writer.writerow(row)
        print(f"curated CSV 已寫入：{csv_path}（{len(row) - 1} 個欄位）")
    except OSError as e:
        print(f"  [警告] 寫入 curated CSV 失敗：{e}")


def save_json(record, path=JSON_PATH):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"完整原始資料已寫入：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 JSON 失敗：{e}")


def main():
    client = ApiClient()
    data = fetch_env_data(client)

    # 完整原始 API 資料（全部端點，含 pump）保留在 env_data.json
    save_json({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "endpoints": ENDPOINTS,
        "data": data,
    })

    # UI 對齊摘要（只含 UI 實際顯示欄位）→ curated JSON / CSV / console
    curated = summarize_env_curated(data)
    save_curated(curated)
    print_curated(curated)


if __name__ == "__main__":
    main()
