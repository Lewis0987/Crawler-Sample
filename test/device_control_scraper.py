# -*- coding: utf-8 -*-
"""
設備控制頁籤 scraper —（登入後）只讀分析，嚴禁送出控制命令
==============================================================
對應前端：pcsControl（設備控制頁，需登入）
帳號：hmiUser / hmiUser123（僅供登入取得 token 做只讀查詢）

⚠️⚠️ 安全規則（硬性）：
  - 本程式只呼叫「只讀白名單」內的 GET。
  - 控制類 API（見 CONTROL_APIS）一律「只記錄、不呼叫」。
  - 即使 method 是 GET，只要用途是控制（manuallyPowerOn/manuallyPowerOff/faultRecovery），
    也絕不呼叫。程式內建 _assert_readonly() 防呆，命中控制字樣會直接拒絕。

輸出：device_control_readonly.json（登入狀態＋只讀查詢結果＋控制 API 清單，僅記錄）
執行：python device_control_scraper.py
"""

import os
import json
from datetime import datetime

from api_client import ApiClient

USERNAME = "hmiUser"
PASSWORD = "hmiUser123"

# 統一輸出目錄：專案根目錄下的 output/（不論從哪個資料夾執行都一致），不存在則自動建立
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
JSON_PATH = os.path.join(_OUTPUT_DIR, "device_control_readonly.json")

# ---- 只讀白名單（唯一允許呼叫的端點；皆為 GET 查詢）----
READONLY_ENDPOINTS = {
    "空調狀態":        "/client/dynamic/dataOrControl/air",
    "PCS狀態":         "/client/dynamic/dataOrControl/pcs",
    "PCS運行模式":     "/client/dynamic/dataOrControl/pcs/getRunMode",
    "PCS品牌(guest)":  "/hmiGuest/unauthorizedAccess/envCon/pcs/getBrand",
    "排程開關狀態":    "/schedule/config/getScheduleSwitch",
    "BCU狀態":         "/can/v1/getBcuState",
    "DO/DI訊號":       "/can/v1/getDOAndDIMsg",
    "手動上電狀態查詢": "/sys/control/getManuallyPowerState",
}

# ---- 控制類 API（僅記錄，程式一律不呼叫）----
# 註：manuallyPowerOn/Off、faultRecovery 雖是 GET，但屬控制動作，嚴禁呼叫。
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

# 防呆用集合：只讀白名單（唯一允許）與控制黑名單（永不呼叫）
_READONLY_ALLOWLIST = set(READONLY_ENDPOINTS.values())
_CONTROL_BLOCKLIST = {c["path"] for c in CONTROL_APIS}


def _assert_readonly(path):
    """
    防呆（exact-match，最保守）：
    - path 必須在只讀白名單內，否則拒絕。
    - path 若命中控制黑名單，直接拒絕（雙重保險）。
    這樣即使白名單路徑含 'Control' 字樣（dataOrControl）也不會誤判，
    且任何不在白名單的路徑（含 GET 型控制動作）都無法被呼叫。
    """
    if path in _CONTROL_BLOCKLIST:
        raise RuntimeError(f"[安全阻擋] 該路徑屬控制 API，拒絕呼叫：{path}")
    if path not in _READONLY_ALLOWLIST:
        raise RuntimeError(f"[安全阻擋] 路徑不在只讀白名單，拒絕呼叫：{path}")


def fetch_readonly(client):
    """依白名單逐一 GET，回傳 {名稱: 資料或 None}。每支都先過防呆。"""
    result = {}
    for name, path in READONLY_ENDPOINTS.items():
        try:
            _assert_readonly(path)
        except RuntimeError as e:
            print(f"  {e}")
            result[name] = {"_blocked": True, "path": path}
            continue
        result[name] = client.get(path)
    return result


def summarize_device_control_readonly(data):
    """簡單摘要：每支只讀查詢是否有資料。"""
    summary = {}
    for name in READONLY_ENDPOINTS:
        block = data.get(name)
        if block is None:
            summary[name] = "無資料/需登入"
        elif isinstance(block, dict) and block.get("_error"):
            summary[name] = f"錯誤 code={block.get('_error')}"
        elif isinstance(block, dict) and block.get("_blocked"):
            summary[name] = "已被安全阻擋"
        else:
            summary[name] = "有資料"
    return summary


def print_summary(logged_in, summary):
    print("=" * 56)
    print(f"設備控制（只讀）｜登入：{'成功' if logged_in else '失敗/未登入'}")
    for name, status in summary.items():
        print(f"  {name}: {status}")
    print("\n  ⚠️ 控制類 API（僅記錄，未呼叫）：")
    for c in CONTROL_APIS:
        print(f"    [{c['method']}] {c['path']} — {c['用途']}")


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
    token = client.login(USERNAME, PASSWORD)

    data = fetch_readonly(client)

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "logged_in": bool(token),
        "readonly_endpoints": READONLY_ENDPOINTS,
        "readonly_data": data,
        # 控制類 API 僅記錄，不呼叫
        "control_apis_documented_not_called": CONTROL_APIS,
    }
    save_json(record)

    print_summary(bool(token), summarize_device_control_readonly(data))


if __name__ == "__main__":
    main()
