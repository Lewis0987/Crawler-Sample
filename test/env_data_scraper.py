# -*- coding: utf-8 -*-
"""
環控數據頁籤 scraper（只讀 GET）
==================================
對應前端：environmental-control/index.vue（import permission 的 i,j,k,l,m,n）
API（免登入 guest，皆 GET）：
  air          /hmiGuest/unauthorizedAccess/envCon/air
  water        /hmiGuest/unauthorizedAccess/envCon/water
  ups          /hmiGuest/unauthorizedAccess/envCon/ups
  ttyS0        /hmiGuest/unauthorizedAccess/envCon/ttyS0
  voltameter   /hmiGuest/unauthorizedAccess/envCon/voltameter   （AC380 電量儀）
  multifunction/hmiGuest/unauthorizedAccess/envCon/multifunction（多功能傳感器：溫濕度/氣體）

每支回傳 list，內含 metricsDataVoList（{mark,value,unit}）。
本 scraper 用共用的 _flatten_metrics/_fmt_metric/_zh 帶出每個 mark 的實際值（不硬猜欄位）。

輸出：env_data.json（完整原始 API 資料）＋ env_curated.json / env_curated.csv（UI 精簡欄位）＋ console 摘要。
     CSV 只輸出 curated/UI 欄位（env_curated.csv）；不再輸出 raw 全欄位 CSV。
執行：python env_data_scraper.py
"""

import os
import csv
import json
from datetime import datetime

from api_client import ApiClient
# 共用解析邏輯集中在 dashboard_scraper（單純 import 不會觸發其抓取流程）
from dashboard_scraper import _flatten_metrics, _fmt_metric

ENDPOINTS = {
    "空調":         "/hmiGuest/unauthorizedAccess/envCon/air",
    "水系統":       "/hmiGuest/unauthorizedAccess/envCon/water",
    "UPS":          "/hmiGuest/unauthorizedAccess/envCon/ups",
    "串口ttyS0":    "/hmiGuest/unauthorizedAccess/envCon/ttyS0",
    "AC380電量儀":  "/hmiGuest/unauthorizedAccess/envCon/voltameter",
    "多功能傳感器": "/hmiGuest/unauthorizedAccess/envCon/multifunction",
}

# 統一輸出目錄：專案根目錄下的 output/（不論從哪個資料夾執行都一致），不存在則自動建立
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
JSON_PATH = os.path.join(_OUTPUT_DIR, "env_data.json")
CSV_PATH = os.path.join(_OUTPUT_DIR, "env_data.csv")
CURATED_JSON_PATH = os.path.join(_OUTPUT_DIR, "env_curated.json")
CURATED_CSV_PATH = os.path.join(_OUTPUT_DIR, "env_curated.csv")

# ---- curated 重點欄位（中文欄位 → 實際 mark；只挑人會看的重點，非全部 raw mark）----
# 值透過 _flatten_metrics + _fmt_metric 帶出（狀態已中文化、數值已含單位）。
ENV_CURATED = {
    "多功能傳感器": {
        "溫度": "temperature",
        "濕度": "humidity",
        "甲烷CH4": "ch4",
        "氫氣H2": "h2",
    },
    "UPS": {
        "運行模式": "workingMode",
        "運行狀態": "OperationStatus",
        "負載率": "outputLoadLevel",
        "電池容量": "batteryCapacity",
        "電池電壓": "batteryVoltage",
        "備援時間": "batteryBackupTime",
        "電池電量警示": "batteryLowWarningStatus",
        "總告警": "unitGeneralAlarm",
        "急停狀態": "emergencyStopStatus",
        "過載狀態": "OverloadStatus",
    },
    "AC380電量儀": {
        "AB線電壓": "phaseVoltageAB",
        "BC線電壓": "phaseVoltageBC",
        "CA線電壓": "phaseVoltageCA",
        "A相電流": "phaseCurrentA",
        "B相電流": "phaseCurrentB",
        "C相電流": "phaseCurrentC",
        "總有功功率": "totalActivePower",
        "總無功功率": "totalReactivePower",
        "總視在功率": "totalApparentPower",
        "總功率因數": "totalPowerFactor",
    },
    "空調": {
        "運行狀態": "equipmentWorkingStatus",
        "工作模式": "workingMode",
        "設定溫度": "coolingSetTemperature",
        "櫃內溫度": "cabinetTemperature",
        "櫃內濕度": "cabinetHumidity",
        "高溫告警": "temperatureHigh",
        "高壓告警": "highVoltageAlarm",
    },
    "水系統": {
        "告警": "alarm",
    },
    # ttyS0 為 14 個數位訊號（值 0/1），只做中文欄位命名，值「刻意」保留原始 0/1：
    # HMI 本身即以 0/1 呈現，CSV 與 UI 一致，故不轉中文（非遺漏；請勿再自動轉換）。
    "串口ttyS0": {
        "紅燈": "lightRed",
        "黃燈": "lightYellow",
        "綠燈": "lightGreen",
        "水泵運轉": "waterPumpOn",
        "液位高": "liquidHigh",
        "液位低": "liquidLow",
        "緊急按鈕": "emergencyButton",
        "門禁": "accessControl",
        "火警": "fireAlarm",
        "消防故障": "fireMalfunction",
        "突波保護SPD": "SPD",
        "空調故障": "airFail",
        "排風": "airExhaust",
        "蜂鳴器": "buzzerOpne",   # 註：後端原始拼字為 buzzerOpne
    },
}


def fetch_env_data(client):
    """逐一 GET 六張環控卡，回傳 {名稱: 原始資料或 None}。"""
    return client.get_many(ENDPOINTS)


def summarize_env_data(data):
    """
    每個環控卡用通用攤平器帶出所有 mark → 顯示值（含翻譯與單位）。
    回傳 {卡名: {mark: 顯示值}}；某卡無資料則值為 None。
    """
    summary = {}
    for name in ENDPOINTS:
        block = data.get(name)
        if block is None or (isinstance(block, dict) and block.get("_error")):
            summary[name] = None
            continue
        metrics = _flatten_metrics(block)
        summary[name] = {mark: _fmt_metric(md) for mark, md in metrics.items()} or None
    return summary


def build_env_row(data):
    """CSV：把所有卡的所有 mark 攤平成單層一列，欄位帶卡名前綴。"""
    row = {}
    summ = summarize_env_data(data)
    for name, fields in summ.items():
        if fields:
            for mark, val in fields.items():
                row[f"{name}_{mark}"] = val
    return row


def summarize_env_curated(data):
    """
    依 ENV_CURATED 挑重點欄位，回傳 {卡名: {中文欄位: 顯示值}}。
    找不到的 mark 以 'N/A' 佔位；某卡無資料則值為 None。
    """
    curated = {}
    for block_name, fields in ENV_CURATED.items():
        raw = data.get(block_name)
        if raw is None or (isinstance(raw, dict) and raw.get("_error")):
            curated[block_name] = None
            continue
        metrics = _flatten_metrics(raw)
        curated[block_name] = {label: _fmt_metric(metrics.get(mark)) for label, mark in fields.items()}
    return curated


def build_env_curated_row(curated):
    """CSV：把 curated 攤平成單層一列，欄位帶卡名前綴（卡名_中文欄位）。"""
    row = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for block_name, fields in curated.items():
        if fields:
            for label, val in fields.items():
                row[f"{block_name}_{label}"] = val
    return row


def print_curated(curated):
    print("=" * 56)
    print("【環控 curated 重點摘要】")
    for block_name, fields in curated.items():
        print(f"  ● {block_name}")
        if not fields:
            print("      無資料")
            continue
        for label, val in fields.items():
            print(f"      {label}: {val}")


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


def print_summary(summary):
    print("=" * 56)
    for name, fields in summary.items():
        print(f"【{name}】")
        if not fields:
            print("  無資料")
            continue
        for mark, val in fields.items():
            print(f"  {mark}: {val}")


def save_json(record, path=JSON_PATH):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"\n完整原始資料已寫入：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 JSON 失敗：{e}")


def save_csv(row, path=CSV_PATH):
    if not row:
        return
    try:
        row = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **row}
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            writer.writeheader()
            writer.writerow(row)
        print(f"環控摘要已寫入：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 CSV 失敗：{e}")


def main():
    client = ApiClient()
    data = fetch_env_data(client)

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "endpoints": ENDPOINTS,
        "data": data,
    }
    save_json(record)   # 完整原始 API 資料（raw）全保留在 env_data.json

    # CSV 只輸出 curated / UI 欄位（env_curated.csv）；不再輸出 raw 全欄位 CSV，
    # 避免與 env_curated.csv 同時保留兩份大量重複資料。raw 仍在 env_data.json。
    curated = summarize_env_curated(data)
    save_curated(curated)

    # console 只印 curated 重點（raw 全欄位仍在 env_data.json）
    print_curated(curated)


if __name__ == "__main__":
    main()
