# -*- coding: utf-8 -*-
"""
PCS 功率控制 payload / 驗證 測試（test_pcs_control.py）— 離線、無網路、不送控制
====================================================================================
依 DevTools 實測 payload 驗證 3 模式的 payload 組裝、enum、方向與範圍驗證。
交流有功 / 直流恆流：方向 = 正負號（充=負、放=正）。
直流恆功率：payload/enum 已確認，但 DevTools 顯示充/放皆正值 → 方向機制待確認（confirmed=False）。

用法：python test_pcs_control.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import device_control_operator as op

_results = []


def check(name, cond, detail=""):
    _results.append(bool(cond))
    print(f"  {'✓' if cond else '✗'} {name}{('　' + detail) if detail else ''}")


def main():
    print("=" * 64)
    print("共同欄位（DevTools）：param=1 / gridInterconnectionMode=0 / offGridAcVoltRegulation=null")
    base = op._pcs_payload_base()
    check("payload base 含 gridInterconnectionMode=0", base.get("gridInterconnectionMode") == 0)
    check("payload base 含 offGridAcVoltRegulation=null", base.get("offGridAcVoltRegulation") is None)
    check("payload base param=1", base.get("param") == 1)

    print("=" * 64)
    print("一、交流有功（energyDispatchingMode=0, activePowerControlMode=0, 方向=sign）")
    ac_c = op._build_pcs_payload("ac_active", "charge", 10)
    ac_d = op._build_pcs_payload("ac_active", "discharge", 15)
    check("充電 10 → activePowerSetPoint=-10", ac_c["activePowerSetPoint"] == -10, str(ac_c))
    check("放電 15 → activePowerSetPoint=+15", ac_d["activePowerSetPoint"] == 15)
    check("energyDispatchingMode=0", ac_c["energyDispatchingMode"] == 0)
    check("activePowerControlMode=0", ac_c["activePowerControlMode"] == 0)
    check("dcControlMode/dcCurrentSetPoint/dcPowerSetPoint 皆 null",
          ac_c["dcControlMode"] is None and ac_c["dcCurrentSetPoint"] is None and ac_c["dcPowerSetPoint"] is None)

    print("=" * 64)
    print("二、直流恆流（energyDispatchingMode=1, dcControlMode=0, 方向=sign）")
    dcc_c = op._build_pcs_payload("dc_current", "charge", 4)
    dcc_d = op._build_pcs_payload("dc_current", "discharge", 5)
    check("充電 4 → dcCurrentSetPoint=-4", dcc_c["dcCurrentSetPoint"] == -4, str(dcc_c))
    check("放電 5 → dcCurrentSetPoint=+5", dcc_d["dcCurrentSetPoint"] == 5)
    check("energyDispatchingMode=1", dcc_c["energyDispatchingMode"] == 1)
    check("dcControlMode=0", dcc_c["dcControlMode"] == 0)
    check("activePowerControlMode/activePowerSetPoint/dcPowerSetPoint 皆 null",
          dcc_c["activePowerControlMode"] is None and dcc_c["activePowerSetPoint"] is None and dcc_c["dcPowerSetPoint"] is None)

    print("=" * 64)
    print("三、直流恆功率（energyDispatchingMode=1, dcControlMode=1, 方向=sign）")
    dcp_c = op._build_pcs_payload("dc_power", "charge", 5)
    dcp_d = op._build_pcs_payload("dc_power", "discharge", 6)
    check("充電 5 → dcPowerSetPoint=-5", dcp_c["dcPowerSetPoint"] == -5, str(dcp_c))
    check("放電 6 → dcPowerSetPoint=+6", dcp_d["dcPowerSetPoint"] == 6)
    check("energyDispatchingMode=1", dcp_c["energyDispatchingMode"] == 1)
    check("dcControlMode=1", dcp_c["dcControlMode"] == 1)
    check("activePowerControlMode/activePowerSetPoint/dcCurrentSetPoint 皆 null",
          dcp_c["activePowerControlMode"] is None and dcp_c["activePowerSetPoint"] is None and dcp_c["dcCurrentSetPoint"] is None)

    print("=" * 64)
    print("四、confirmed（三種皆 READY）與 direction（三種皆 sign）")
    for m in ("ac_active", "dc_current", "dc_power"):
        check(f"{op.PCS_CONTROL_MODES[m]['label']} confirmed=True（READY）", op.PCS_CONTROL_MODES[m]["confirmed"] is True)
        check(f"{op.PCS_CONTROL_MODES[m]['label']} direction=sign（充=負、放=正）", op.PCS_CONTROL_MODES[m]["direction"] == "sign")

    print("=" * 64)
    print("五、範圍驗證（0~150；超出應阻擋）")
    for mode, bad in [("ac_active", 200), ("dc_current", 151), ("dc_power", 999)]:
        try:
            op._build_pcs_payload(mode, "charge", bad)
            check(f"{mode} 值 {bad} 應被拒", False)
        except RuntimeError as e:
            check(f"{mode} 值 {bad} 被拒（範圍驗證）", "範圍" in str(e), str(e)[:36])
    check("交流有功 邊界 0 可組裝", op._build_pcs_payload("ac_active", "charge", 0)["activePowerSetPoint"] == 0)
    check("直流恆流 邊界 150 可組裝", op._build_pcs_payload("dc_current", "discharge", 150)["dcCurrentSetPoint"] == 150)

    print("=" * 64)
    print("六、action 與必填 power")
    for a in ["pcs_charge", "pcs_discharge", "pcs_dc_current_charge", "pcs_dc_current_discharge",
              "pcs_dc_power_charge", "pcs_dc_power_discharge"]:
        check(f"action 存在且需 power：{a}", a in op.ACTIONS and a in op.REQUIRES_POWER)
    check("所有充放電皆需電池已上電前置檢查", op.PRECHECK_BATTERY_ON == op.REQUIRES_POWER)

    print("=" * 64)
    print(f"結果：{sum(1 for r in _results if r)}/{len(_results)} PASS")
    return 0 if all(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
