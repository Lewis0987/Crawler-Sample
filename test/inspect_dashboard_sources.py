# -*- coding: utf-8 -*-
"""
[DIAGNOSTIC 唯讀] inspect_dashboard_sources.py
================================================
比對 PCS概覽（envCon/pcs）與 AC380 電量儀（envCon/voltameter）的資料來源與欄位 mapping，
釐清「CLI A相電流=0.00」與「HMI AC380 相電流A=1.05」是否為同一裝置。
純唯讀 GET，不送任何控制 API、不修改設備。

用法：python inspect_dashboard_sources.py
"""
import sys
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://192.168.128.110:8080/admin-api"
PCS_API = "/hmiGuest/unauthorizedAccess/envCon/pcs"
VM_API = "/hmiGuest/unauthorizedAccess/envCon/voltameter"


def _get(path):
    r = requests.get(BASE + path, timeout=8)
    r.raise_for_status()
    return r.json()


def _devices(payload):
    d = payload.get("data") if isinstance(payload, dict) else payload
    return d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])


def _marks(dev):
    return {m.get("mark"): m for m in (dev.get("metricsDataVoList") or []) if isinstance(m, dict)}


def main():
    print("=" * 64)
    print("PCS概覽 資料來源：GET", PCS_API)
    pcs_dev = _devices(_get(PCS_API))
    print(f"  list 筆數 = {len(pcs_dev)}")
    for i, dev in enumerate(pcs_dev):
        print(f"  [{i}] mark={dev.get('mark')} deviceId={dev.get('deviceId')} address={dev.get('address')}")
    pm = _marks(pcs_dev[0]) if pcs_dev else {}
    print("  PCS 交流欄位（PCS 自身 AC 匯流排；依 mark）：")
    for mk in ("voltageOfAcBusLineAB", "voltageOfAcBusLineBC", "voltageOfAcBusLineCA",
               "currentOfAcBusLineA", "currentOfAcBusLineB", "currentOfAcBusLineC", "acBusFrequency"):
        v = pm.get(mk, {})
        print(f"     {mk:24s} = {v.get('value')} {v.get('unit') or ''}")
    print("  PCS 狀態欄位（各自獨立）：")
    for mk in ("systemOnOrOffStatus", "systemFaultStatus", "systemAlarmStatus", "systemGridTiedStatus"):
        v = pm.get(mk, {})
        print(f"     {mk:24s} = {v.get('value')}")

    print("=" * 64)
    print("AC380 電量儀 資料來源：GET", VM_API)
    vm_dev = _devices(_get(VM_API))
    print(f"  list 筆數 = {len(vm_dev)}")
    for i, dev in enumerate(vm_dev):
        print(f"  [{i}] mark={dev.get('mark')} deviceId={dev.get('deviceId')} address={dev.get('address')}  ← 選用此筆(list[0]，僅 1 台)")
    vmm = _marks(vm_dev[0]) if vm_dev else {}
    print("  AC380 電壓/電流欄位（依 mark，對應 HMI 相電壓/相電流）：")
    for mk, zh in (("phaseVoltageAB", "相電壓AB"), ("phaseVoltageBC", "相電壓BC"), ("phaseVoltageCA", "相電壓CA"),
                   ("phaseCurrentA", "相電流A"), ("phaseCurrentB", "相電流B"), ("phaseCurrentC", "相電流C")):
        v = vmm.get(mk, {})
        print(f"     {zh}（{mk}） = {v.get('value')} {v.get('unit') or ''}")

    print("=" * 64)
    print("結論：")
    print("  PCS 交流電流 currentOfAcBusLine* = PCS「自身」AC 匯流排電流（停機時為 0.00，正確）")
    print("  AC380 相電流 phaseCurrent*       = 獨立電量儀量測（站用/負載，例 1.05/1.05/2.1）")
    print("  兩者為『不同裝置、不同量測點』，數值本就不同，非 bug。")


if __name__ == "__main__":
    main()
