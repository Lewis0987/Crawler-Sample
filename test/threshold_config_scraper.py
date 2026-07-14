# -*- coding: utf-8 -*-
"""
閥值管理頁籤 scraper — 唯讀
================================================================
讀取「告警抑制」開關 + 閥值列表（含 ECM/PCS/BMS 聯動控制），輸出摘要與 JSON/CSV。
⚠️ 只讀：不修改閥值、不保存、不切換開關、不做任何控制。

API（皆 GET，需登入；沿用 api_client 的 SM2 登入）：
  - /client/dynamic/threshold/config/config → {configId, suppressionFlagMainSwitch}
  - /client/dynamic/threshold/list          → {total, rows:[...]}

輸出：
  output/threshold_config.json          （原始 config + list）
  output/threshold_config_summary.json  （解析後可讀摘要）
  output/threshold_config_summary.csv   （解析後表格）
執行：python threshold_config_scraper.py
"""

import os
import csv
import json
from datetime import datetime

from api_client import ApiClient

USERNAME = "hmiUser"

_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
RAW_PATH = os.path.join(_OUTPUT_DIR, "threshold_config.json")
SUMMARY_PATH = os.path.join(_OUTPUT_DIR, "threshold_config_summary.json")
CSV_PATH = os.path.join(_OUTPUT_DIR, "threshold_config_summary.csv")

CONFIG_API = "/client/dynamic/threshold/config/config"
# 需帶 pageSize 才會回全部（無參數預設只回第一頁 10 筆）
LIST_API = "/client/dynamic/threshold/list"
LIST_PARAMS = {"pageNo": 1, "pageSize": 1000}

# 終端明細預設只印前 N 筆（JSON/CSV 仍保留完整）
DETAIL_PRINT_LIMIT = 10

# ---- 對照表（來源：前端 alarm-threshold 渲染邏輯 + 實抓資料集合；原始值另存 JSON）----
_TYPE_MAP = {"device.type.temperature": "多功能溫溼度", "device.type.voltameter": "電量儀"}
_TARGET_MAP = {
    "temperature": "溫度", "humidity": "濕度", "ch4": "甲烷", "h2": "氫氣",
    "attr.connect.status": "連線狀態",
    "type.attr.phaseVoltageAB": "相電壓AB", "type.attr.phaseVoltageBC": "相電壓BC",
    "type.attr.phaseVoltageCA": "相電壓CA",
}
_LEVEL_MAP = {0: "嚴重", 1: "一般"}
# ⚠️ 開關值對照（依前端閥值頁：value 0 = 綠色/checked = 啟用；1 = 灰色 = 停用）
_SWITCH_MAP = {0: "已啟用", 1: "已停用"}
_COND_MAP = {"time": "時間持續"}
# 聯動控制動作代碼 → 中文
_ECM_MAP = {0: "黃燈恆亮", 1: "紅燈恆亮"}               # ipcOperate（0→yellowLightOn / 1→redLightIsOn）
_PCS_MAP = {17: "無動作", 2: "PCS停機"}                # pcsOperate
_BCU_MAP = {17: "無動作", 10: "普通下電"}              # bcuOperate（BMS）

CSV_FIELDS = ["感測器型別", "觸發閥值", "觸發條件", "告警級別",
              "ECM", "PCS", "BMS", "聯動控制開關", "告警開關"]


def parse_threshold_main_switch(value):
    """告警抑制主開關（suppressionFlagMainSwitch）：0→已啟用、1→已停用。"""
    return _SWITCH_MAP.get(value, f"原始值 {value}")


def parse_threshold_toggle(value):
    """每列 聯動控制開關 / 告警開關（linkageControlSwitch / enableFlag）：0→已啟用、1→已停用。"""
    return _SWITCH_MAP.get(value, f"原始值 {value}")


def parse_row(r):
    lv = r.get("linkageControlVo") or {}
    target = _TARGET_MAP.get(r.get("target"), r.get("targetName") or r.get("target"))
    unit = r.get("targetUnit") or ""
    thr = f"{target} {r.get('operatorStr', '')} {r.get('targetValue')}".strip()
    if unit:
        thr += f" {unit}"
    cond = r.get("condition")
    cond_str = (f"{_COND_MAP.get(cond, cond)} {r.get('conditionValue')} 秒"
                if cond else "-")
    return {
        "感測器型別": _TYPE_MAP.get(r.get("typeName"), r.get("typeName")),
        "觸發閥值": thr,
        "觸發條件": cond_str,
        "告警級別": _LEVEL_MAP.get(r.get("level"), r.get("level")),
        "ECM": _ECM_MAP.get(lv.get("ipcOperate"), f"代碼{lv.get('ipcOperate')}"),
        "PCS": _PCS_MAP.get(lv.get("pcsOperate"), f"代碼{lv.get('pcsOperate')}"),
        "BMS": _BCU_MAP.get(lv.get("bcuOperate"), f"代碼{lv.get('bcuOperate')}"),
        "聯動控制開關": parse_threshold_toggle(r.get("linkageControlSwitch")),
        "告警開關": parse_threshold_toggle(r.get("enableFlag")),
    }


def _err_note(block):
    """區分讀取 API 403/錯誤 vs 無資料。"""
    if isinstance(block, dict) and block.get("_error"):
        return f"讀取 API 錯誤 code={block.get('_error')} msg={block.get('msg')}"
    if block is None:
        return "讀取失敗/無回應"
    return None


def save_json(path, obj, label):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        print(f"  ✓ {label}：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 {label} 失敗：{e}")


def save_csv(path, rows):
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"  ✓ CSV：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 CSV 失敗：{e}")


def print_summary(suppression, total, parsed):
    print("=" * 56)
    print("【閥值管理】")
    print(f"告警抑制：{suppression}")
    print(f"總筆數：{total}")
    shown = min(len(parsed), DETAIL_PRINT_LIMIT)
    print(f"顯示筆數：{shown} / {total}")
    for i, row in enumerate(parsed[:DETAIL_PRINT_LIMIT], 1):
        print(f"\n[{i}]")
        for k in CSV_FIELDS:
            print(f"{k}：{row[k]}")
    remaining = len(parsed) - DETAIL_PRINT_LIMIT
    if remaining > 0:
        print(f"\n…其餘 {remaining} 筆略過（完整資料請看 "
              f"output/threshold_config.json 或 threshold_config_summary.csv）")


def main():
    client = ApiClient()
    # 統一登入：由 api_client.login_hmi 印出 4 行登入 log（不含機密），失敗細節亦由其印出。
    token = client.login_hmi(USERNAME)

    config = client.get(CONFIG_API)
    lst = client.get(LIST_API, params=LIST_PARAMS)   # 帶 pageSize 抓齊全部

    cfg_err = _err_note(config)
    lst_err = _err_note(lst)

    suppression = "讀取失敗"
    if isinstance(config, dict) and not config.get("_error"):
        suppression = parse_threshold_main_switch(config.get("suppressionFlagMainSwitch"))
    elif cfg_err:
        suppression = cfg_err

    rows = lst.get("rows", []) if isinstance(lst, dict) and not lst.get("_error") else []
    total = lst.get("total", len(rows)) if isinstance(lst, dict) else 0
    if lst_err:
        total = lst_err
    parsed = [parse_row(r) for r in rows]

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "logged_in": bool(token),
        "config_api": CONFIG_API,
        "list_api": LIST_API,
        "raw_config": config,
        "raw_list": lst,
    }
    summary = {
        "timestamp": record["timestamp"],
        "告警抑制": suppression,
        "總筆數": total,
        "閥值列表": parsed,
    }

    print("\n寫入輸出：")
    save_json(RAW_PATH, record, "原始 JSON")
    save_json(SUMMARY_PATH, summary, "摘要 JSON")
    save_csv(CSV_PATH, parsed)

    print_summary(suppression, total, parsed)


if __name__ == "__main__":
    main()
