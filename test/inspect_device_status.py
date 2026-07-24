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


def dump_battery_state_diagnostic(client):
    """
    [唯讀診斷] 電池「當前狀態 vs 充放電狀態」來源比對 —— 供釐清 UI「電池：故障」對應哪個原始欄位。
    **只印診斷，不寫任何 CSV、不改任何狀態**。下次 UI 顯示「電池：故障」時同時間點跑本工具比對。
    """
    from device_control_scraper import battery_flow_state
    print("\n===== [DIAG] 電池當前狀態來源比對（唯讀）=====")
    main = client.get("/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation")
    v = main.get("rackTotalBatteryVoltage") if isinstance(main, dict) else None
    a = main.get("rackElectricCurrent") if isinstance(main, dict) else None
    flow, pkw = battery_flow_state(main)
    print(f"  raw_voltage            = {v}")
    print(f"  raw_current            = {a}")
    print(f"  calculated_power_kw    = {pkw}")
    print(f"  battery_flow_state     = {flow}   (V×A 能量流向，非健康/故障)")
    # 電池「當前狀態」的原始欄位：mainControl 無此欄位 → missing
    print(f"  raw_battery_state      = missing  (mainControlCollectsInformation 無 status/fault 欄位)")
    print(f"  state_source           = unavailable")
    print(f"  normalized_battery_state = unavailable")
    print(f"  final_battery_state    = 未知（來源待確認）")
    # PCS 側 fault flags（**標明屬 PCS，非電池；不得直接當電池故障**），僅列出非 normal 者
    pcs = client.get("/hmiGuest/unauthorizedAccess/envCon/pcs")
    pcs_faults = []
    if isinstance(pcs, list) and pcs:
        for m in pcs[0].get("metricsDataVoList", []):
            mk = str(m.get("mark", ""))
            val = m.get("value")
            ov = m.get("oldValue")
            # 只列出「狀態旗標」型（value 為 type.attr.desc.* 描述碼）且非 normal；
            # 排除數值型門檻設定（如 insulationResistanceAlarmThreshold=100）以免誤判為故障。
            if (any(s in mk.lower() for s in ("fault", "fail", "abnormal"))
                    and isinstance(val, str) and val.startswith("type.attr.desc.")
                    and val != "type.attr.desc.normal" and ov not in (0, "0")):
                pcs_faults.append(f"{mk}={val}(old={ov})")
    print(f"  raw_fault_fields(PCS)  = {pcs_faults if pcs_faults else '全部 normal（PCS，僅供參考、非電池故障）'}")
    # active battery/BCU alarms（alarmStatus=True 且屬 BCU/BMS/電池類）
    al = client.get("/hmiGuest/unauthorizedAccess/alarm/list", params={"pageNo": 1, "pageSize": 100})
    rows = (al.get("rows") or al.get("list") or al.get("records") or []) if isinstance(al, dict) else []
    def _active(x): return x in (True, 1, "1", "true", "True")
    batt_active = [f"{r.get('typeMark')}/{r.get('targetMark')}/{r.get('val')}(lvl={r.get('level')})"
                   for r in rows if _active(r.get("alarmStatus"))
                   and any(k in str(r.get("typeMark", "")).lower() for k in ("bcu", "bms", "battery", "rack"))]
    print(f"  active_battery_alarms  = {batt_active if batt_active else '無（目前無 active 電池/BCU 告警）'}")
    print("  → 結論：電池「當前狀態」目前無已確認的原始來源；請於 UI 顯示『電池：故障』的同一時間點重跑本工具比對。")


def main():
    client = ApiClient()
    client.login_hmi(log=True)
    for name, path in OVERVIEW.items():
        dump_envcon(client, name, path)   # 多為 envCon 樣式 list；非 list 會回退
    for name, path in OTHERS.items():
        dump_other(client, name, path)
    dump_battery_state_diagnostic(client)   # 電池當前狀態來源比對（唯讀）


if __name__ == "__main__":
    main()
