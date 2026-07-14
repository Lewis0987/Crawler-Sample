# -*- coding: utf-8 -*-
"""
[DIAGNOSTIC 一次性診斷工具]（run_all.py 不會呼叫本檔）
唯讀：dump 設備控制相關端點的真實結構（mark / value / unit），
供 device_control_scraper.py 建立狀態解析用。不送任何控制。

用法：python inspect_device_status.py
"""
import sys
import json

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from api_client import ApiClient

# envCon 類（回 list，含 metricsDataVoList）
ENVCON = {
    "guest PCS": "/hmiGuest/unauthorizedAccess/envCon/pcs",
    "guest AIR": "/hmiGuest/unauthorizedAccess/envCon/air",
    "guest WATER": "/hmiGuest/unauthorizedAccess/envCon/water",
    "guest TTYS0": "/hmiGuest/unauthorizedAccess/envCon/ttyS0",
    "guest MULTIFUNCTION": "/hmiGuest/unauthorizedAccess/envCon/multifunction",
}
# overview / battery（找電池區塊 SOC/電壓/電流/功率/狀態來源）
OVERVIEW = {
    "overview mainControl": "/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation",
    "overview rackCapacity": "/hmiGuest/unauthorizedAccess/overview/rackCapacityInformation",
    "overview rackExtreme": "/hmiGuest/unauthorizedAccess/overview/rackExtremeValueInformation",
}

# 其他（bool / dict / 字串）
OTHERS = {
    "getRunMode": "/client/dynamic/dataOrControl/pcs/getRunMode",
    "getScheduleSwitch": "/schedule/config/getScheduleSwitch",
    "getBcuState": "/can/v1/getBcuState",
    "getDOAndDIMsg": "/can/v1/getDOAndDIMsg",
    "getManuallyPowerState": "/sys/control/getManuallyPowerState",
    "getBrand": "/hmiGuest/unauthorizedAccess/envCon/pcs/getBrand",
}


def dump_envcon(client, name, path):
    print(f"\n===== {name}  ({path}) =====")
    d = client.get(path)
    if not (isinstance(d, list) and d):
        print("  (非 list 或空)", json.dumps(d, ensure_ascii=False)[:200])
        return
    for idx, item in enumerate(d):
        if not isinstance(item, dict):
            continue
        print(f"  -- item[{idx}] mark={item.get('mark')} --")
        for m in item.get("metricsDataVoList", []):
            print(f"    {m.get('mark'):32} = {str(m.get('value')):20} unit={m.get('unit')}")


def dump_other(client, name, path):
    d = client.get(path)
    print(f"\n===== {name}  ({path}) =====")
    print("  type:", type(d).__name__)
    print("  " + json.dumps(d, ensure_ascii=False)[:600])


def main():
    client = ApiClient()
    client.login_hmi(log=True)
    for name, path in OVERVIEW.items():
        dump_envcon(client, name, path)   # 多為 envCon 樣式 list；非 list 會回退
    for name, path in OTHERS.items():
        dump_other(client, name, path)


if __name__ == "__main__":
    main()
