# -*- coding: utf-8 -*-
"""
內網 Dashboard 抓取 — 最小可行版（requests / 只用 GET）
=========================================================
只抓四大區塊：電池概覽、PCS 概覽、環控概覽、告警資訊。
所有結論來自前端 JS 靜態分析：
  - baseURL = http://192.168.128.110:8080/admin-api （API 在同主機 8080）
  - 免登入、免 tenant-id，全部 GET。

執行方式：
    pip install requests
    python dashboard_min.py

行為：
  - 只抓「一次」（不輪詢、不 sleep）
  - console 只印「摘要」（不印整包 JSON）
  - 完整原始資料仍寫入 dashboard_min.json
"""

import os
import json
from datetime import datetime

import requests

from api_client import ApiClient
# 與 dashboard_scraper 共用同一組 formatter / parser（避免兩支 dashboard 顯示不同）：
#   - PCS 概覽（4 個 UI 模式欄位）：dashboard_scraper.pcs_mode_view
#   - AC380 電量儀：dashboard_scraper.summarize_voltameter + ui_filter + AC380_UI_FIELDS
from dashboard_scraper import summarize_voltameter, ui_filter, AC380_UI_FIELDS, pcs_mode_view


# ============ 設定區（要改就改這裡） ============
BASE_URL = "http://192.168.128.110:8080/admin-api"   # 如 8853 有代理可改成 :8853
TIMEOUT = 8                  # 單支逾時秒數
USERNAME = "hmiUser"
# 註：PCS控制模式（手動/智慧）來自 getRunMode（需登入的 /client/dynamic 端點），
#     故本版改用 api_client 登入後抓取；其餘 guest 端點仍可讀。
# 統一輸出目錄：專案根目錄下的 output/（不論從哪個資料夾執行都一致），不存在則自動建立
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
JSON_PATH = os.path.join(_OUTPUT_DIR, "dashboard_min.json")  # 把 API 回傳資料存成完整 JSON

# 打 API 抓資料 => 已確認 method=GET 的四大區塊 endpoint
ENDPOINTS = {
    "電池概覽": "/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation",
    "PCS概覽":  "/hmiGuest/unauthorizedAccess/envCon/pcs",
    "AC380電量儀": "/hmiGuest/unauthorizedAccess/envCon/voltameter",   # AC380 電量儀（相電壓/相電流/功率）
    "環控概覽": "/hmiGuest/unauthorizedAccess/envCon/air",   # TODO: 環控可能還含 water/ups/ttyS0，待確認哪個對應畫面主區塊
    # 以下為 PCS 控制模式（手動/智慧）與排程狀態，屬需登入的 /client、/schedule 端點
    "getRunMode": "/client/dynamic/dataOrControl/pcs/getRunMode",
    "getScheduleSwitch": "/schedule/config/getScheduleSwitch",
    # 告警先只抓 3 筆。TODO: 分頁參數名稱尚未 100% 確認，先用 yudao 常見的 pageNo/pageSize
    "告警資訊": ("/hmiGuest/unauthorizedAccess/alarm/list", {"pageNo": 1, "pageSize": 3}),
}

# 電池概覽欄位（皆依 dashboard_min.json 實際 key 確認）
BATTERY_FIELDS = {
    "電壓": ["rackTotalBatteryVoltage"],                                     # 已確認（單位 V）
    "電流": ["rackElectricCurrent"],                                         # 已確認（單位 A）
    "R+":  ["rackInsulationResistanceRPositive"],                            # 已確認
    "R-":  ["rackInsulationResistanceRPositiveNegative"],                    # 已確認
    "SOC": ["rackSoc"],                                                      # 已確認
    "SOH": ["rackSoh"],                                                      # 已確認
}
# 環控概覽欄位（envCon/air 回傳 list，內含 metricsDataVoList，以 mark 對應）
ENV_FIELDS = {
    "執行狀態": "equipmentWorkingStatus",
    "工作模式": "workingMode",
    "設定溫度": "coolingSetTemperature",
    "櫃內溫度": "cabinetTemperature",
    "櫃內濕度": "cabinetHumidity",
}
# PCS 其餘量測欄位（envCon/pcs 的 metricsDataVoList，以 mark 對應）。
# PCS 概覽改用共用 dashboard_scraper.pcs_mode_view（只印 4 個 HMI 模式欄位）；
# 故障狀態/告警/功率/頻率/溫度等不在 HMI PCS 卡片 → console 不印（JSON 仍完整）。

# i18n 狀態值中文對照（value 為 type.attr.* 這類 key 時翻譯；查不到就保留原字串）
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
    # PCS 控制模式（controlMode）
    "type.attr.desc.remote":      "遠端",
    "type.attr.desc.local":       "本地",
    "type.attr.desc.auto":        "自動",
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
}
# ================================================


def _unwrap(payload):
    """
    解開 API 封包：
    - code == 0 或 200 都視為成功
    - 成功時優先回傳 data；若沒有 data 就回整包 payload
    - 失敗時保留 code / msg 方便除錯
    """
    if isinstance(payload, dict) and "code" in payload:
        code = payload.get("code")
        if code in (0, 200):
            return payload.get("data", payload)
        return {
            "_error": code,
            "msg": payload.get("msg"),
            "_raw": payload
        }
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
    """把 i18n 狀態值（type.attr.* 這類 key）翻成中文；查不到或非字串就原樣回傳。"""
    if isinstance(value, str) and value in STATUS_MAP:
        return STATUS_MAP[value]
    return value


def get_data_by_api():
    """登入後逐一 GET 各區塊（含需登入的 getRunMode/getScheduleSwitch），單支失敗不中斷。
    共用 api_client 的登入與封包解封（code 0/200 → data）。"""
    client = ApiClient()
    client.login_hmi(USERNAME)   # getRunMode/getScheduleSwitch 屬 authed 端點，需登入
    result = {}
    for name, spec in ENDPOINTS.items():
        path, params = spec if isinstance(spec, tuple) else (spec, None)
        result[name] = client.get(path, params=params)   # client.get 已解封包、失敗回 None
    return result


# ---------------- 摘要整理函式 ----------------
def summarize_battery(data):
    """整理電池概覽重要欄位，並組上對應單位（<key>Unit）；無資料回傳 None。"""
    if not isinstance(data, dict) or data.get("_error"):
        return None
    result = {}
    for label, keys in BATTERY_FIELDS.items():
        val = _pick(data, keys)
        if val != "N/A":
            # 找出實際命中的 key，取其對應的 <key>Unit 組成「900.6 V」
            for k in keys:
                if k in data and data[k] is not None:
                    unit = data.get(k + "Unit")
                    if unit:
                        val = f"{val} {unit}"
                    break
        result[label] = val
    return result


def _flatten_metrics(data):
    """
    把 envCon 回傳的 list 攤平成 {mark: {"value": <已翻譯值>, "unit": <單位或 None>}}。
    - data 為 [{ "mark": "air", "metricsDataVoList": [{mark,value,unit}, ...] }, ...]
    - 狀態值（type.attr.*）會先經 _zh() 套中文；數值/單位保留原樣，交由 _fmt_metric 組字串。
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
    - decimals 不為 None 時，數值型 value 會格式化為固定小數位（例：-1.20、60.00）。
    - 值已含單位或無單位時不重複補；非數字值套小數位失敗則保留原值。
    """
    if not isinstance(md, dict):
        return "N/A"
    value, unit = md.get("value"), md.get("unit")
    if decimals is not None:
        try:
            value = f"{float(value):.{decimals}f}"
        except (TypeError, ValueError):
            pass  # 非數字（例如狀態字串）就保留原值
    return f"{value} {unit}" if unit else f"{value}"


def summarize_env(data):
    """整理環控概覽重要欄位；無資料回傳 None。"""
    if data is None or (isinstance(data, dict) and data.get("_error")):
        return None
    metrics = _flatten_metrics(data)
    if not metrics:
        return None
    return {label: _fmt_metric(metrics.get(mark)) for label, mark in ENV_FIELDS.items()}


# PCS 概覽改用共用 dashboard_scraper.pcs_mode_view（見 print_summary），此處不再自訂 summarize_pcs。


def summarize_alarm(data):
    """
    整理告警資訊。回傳 (total, active_in_batch, brief)：

    - total：alarm/list API 回傳的「總筆數」欄位（本 API 為 rows 結構中的 total，實測=38）。
             這是資料庫端的告警總數，「並非程式自行計算」，也「不受 pageSize 影響」。
             pageSize=3 只是本次取回 3 筆做摘要，total 仍是全部筆數。
    - active_in_batch：本次「取回這批資料」中 alarmStatus 為真的筆數。
             ⚠️ 注意：目前 pageSize 只取回少數幾筆（即 rows），
             因此這個數字僅代表「本次取回資料中的啟用告警數」，
             不是全部 total（38）筆中的真實啟用數。若要真實啟用總數，
             需另外對 API 加條件查詢或把 pageSize 調大。
    - brief：本次取回資料的前 3 筆精簡欄位。

    相容結構：{"total": N, "rows": [...]}（本 API）或 {"list"/"records": [...]} 或直接是 list。
    無資料時回傳 (None, 0, [])。
    """
    if isinstance(data, dict) and not data.get("_error"):
        items = data.get("rows") or data.get("list") or data.get("records") or []
        total = data.get("total", len(items))   # total 直接取自 API 回傳欄位，非程式計算
    elif isinstance(data, list):
        items, total = data, len(data)
    else:
        return None, 0, []

    # 統計「本次取回資料」中啟用中的告警（alarmStatus 為真；相容 bool / 1 / "true"）
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


def print_summary(record):
    """把一輪 record 印成人看的摘要（不印整包 JSON）。"""
    data = record["data"]
    print("=" * 50)
    print(f"時間：{record['timestamp']}")

    # 電池概覽
    print("\n【電池概覽】")
    batt = summarize_battery(data.get("電池概覽"))
    if batt:
        for k, v in batt.items():
            print(f"  {k:<4}: {v}")
    else:
        print("  無資料")

    # PCS 概覽（控制/工作/功率控制模式用共用 parse_pcs_modes）
    print("\n【PCS概覽】")
    pcs = pcs_mode_view(data.get("PCS概覽"), data.get("getRunMode"), data.get("getScheduleSwitch"))
    if pcs:
        for k, v in pcs.items():
            print(f"    {k}: {v}")
    else:
        print("  無資料")

    # AC380 電量儀（共用 summarize_voltameter；UI formatter 只印相電壓/相電流）
    print("\n【AC380電量儀】")
    vm = ui_filter(summarize_voltameter(data.get("AC380電量儀")), AC380_UI_FIELDS)
    if vm:
        for k, v in vm.items():
            print(f"    {k}: {v}")
    else:
        print("  無資料")

    # 環控概覽
    print("\n【環控概覽】")
    env = summarize_env(data.get("環控概覽"))
    if env:
        print("  有資料")
        for k, v in env.items():
            print(f"    {k}: {v}")
    else:
        print("  無資料")

    # 告警資訊
    print("\n【告警資訊】")
    total, active_in_batch, brief = summarize_alarm(data.get("告警資訊"))
    if total is None:
        print("  無資料")
    else:
        print(f"  告警總數（API total）：{total}")
        # 註：此啟用數僅為「本次取回資料」中的統計，非全部 total 筆的真實啟用數
        print(f"  目前啟用中的告警數（alarmStatus=True，僅本次取回資料）：{active_in_batch}")
        print(f"  本次顯示前 {len(brief)} 筆：")
        if not brief:
            print("    （無告警項目）")
        for i, a in enumerate(brief, 1):
            print(f"    [{i}] typeMark={a['typeMark']} targetMark={a['targetMark']} "
                  f"level={a['level']} alarmStatus={a['alarmStatus']} val={a['val']}")


def main():
    print(f"開始抓取（單次），baseURL={BASE_URL}")
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": get_data_by_api(),
    }

    # console 只印摘要
    print_summary(record)

    # JSON 檔仍保留完整原始資料
    try:
        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"\n完整原始資料已寫入：{JSON_PATH}")
    except OSError as e:
        print(f"  [警告] 寫入 JSON 失敗：{e}")


if __name__ == "__main__":
    main()
