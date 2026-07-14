# -*- coding: utf-8 -*-
"""
PCS 模式解析驗證（test_pcs_modes.py）— 離線、無網路
====================================================
驗證 device_control_scraper.parse_pcs_modes()：控制模式 / 排程 / 電網模式 各自「獨立解析、
互不推導」，以及功率控制模式（交流有功／直流恆流／直流恆功率／未知，依 API 實際欄位、不套預設）。
電網模式（併網／離網）為唯讀狀態顯示（非控制）。

用法：python test_pcs_modes.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from device_control_scraper import (
    parse_pcs_modes, parse_pcs_power_control_mode, parse_pcs_current_status,
    _CONTROL_MODE_DISPLAY, _SCHEDULE_DISPLAY, _GRID_MODE_DISPLAY,
)

_F = "type.attr.desc.false"
_NORMAL = "type.attr.desc.normal"


def display(modes):
    """機器語意 → 中文顯示（與 parse_pcs 相同對照）。"""
    return {
        "PCS控制模式": _CONTROL_MODE_DISPLAY[modes["control_mode"]],
        "PCS排程開關狀態": _SCHEDULE_DISPLAY[modes["schedule_enabled"]],
        "PCS工作模式": _GRID_MODE_DISPLAY[modes["grid_mode"]],
        "PCS功率控制模式": modes["power_control_mode"],
    }


def run_case(name, runmode, schedule, pcs_raw, expect):
    modes = parse_pcs_modes(runmode, schedule, pcs_raw)
    got = display(modes)
    ok = all(got.get(k) == v for k, v in expect.items())
    print("=" * 60)
    print(f"{name}")
    print(f"  輸入：getRunMode={runmode!r}  schedule={schedule}  pcs_raw={pcs_raw or '{}'}")
    for k, v in expect.items():
        mark = "✓" if got.get(k) == v else "✗"
        print(f"  {mark} {k}：got={got.get(k)!r}  expect={v!r}")
    print(f"  結果：{'PASS' if ok else 'FAIL'}")
    return ok


def main():
    results = []
    GRID = {"systemGridTiedStatus": "type.attr.desc.gridTied", "systemOffGridStatus": "type.attr.desc.false"}

    # 1) 併網 + 交流有功
    results.append(run_case(
        "1. 併網 + 交流有功（智慧，schedulePlanSwitch=0）",
        "智慧模式", {"schedulePlanSwitch": 0},
        {**GRID, "energyDispatchingMode": "type.attr.desc.ac"},
        {"PCS控制模式": "智慧模式", "PCS排程開關狀態": "關",
         "PCS工作模式": "併網", "PCS功率控制模式": "交流有功"}))

    # 2) 併網 + 直流恆流（排程開）
    results.append(run_case(
        "2. 併網 + 直流恆流（排程開）",
        "智慧模式", {"schedulePlanSwitch": 1},
        {**GRID, "energyDispatchingMode": "type.attr.desc.dc", "dcControlMode": "type.attr.desc.constantCurrent"},
        {"PCS控制模式": "智慧模式", "PCS排程開關狀態": "開",
         "PCS工作模式": "併網", "PCS功率控制模式": "直流恆流"}))

    # 3) 併網 + 直流恆功率（手動）
    results.append(run_case(
        "3. 併網 + 直流恆功率（手動）",
        "手動模式", {"schedulePlanSwitch": 0},
        {**GRID, "energyDispatchingMode": "type.attr.desc.dc", "dcControlMode": "type.attr.desc.fixedPower"},
        {"PCS控制模式": "手動模式", "PCS排程開關狀態": "關",
         "PCS工作模式": "併網", "PCS功率控制模式": "直流恆功率"}))

    # 4) 離網 + 離網交流電壓（唯讀狀態；systemOffGridStatus=true）
    #    離網時功率控制模式應顯示「離網交流電壓」，不可誤顯示併網的交流有功/直流恆流/直流恆功率
    results.append(run_case(
        "4. 離網 + 離網交流電壓（即使 energyDispatchingMode=ac/dc 也不顯示併網模式）",
        "智慧模式", {"schedulePlanSwitch": 0},
        {"systemOffGridStatus": "type.attr.desc.true",
         "energyDispatchingMode": "type.attr.desc.dc", "dcControlMode": "type.attr.desc.fixedPower"},
        {"PCS控制模式": "智慧模式", "PCS工作模式": "離網", "PCS功率控制模式": "離網交流電壓"}))

    # 5) 欄位缺失 → 功率控制模式=未知；電網模式=未知（不猜、不套預設值）
    results.append(run_case(
        "5. 欄位缺失 → 功率控制模式=未知、電網模式=未知",
        "智慧模式", {"schedulePlanSwitch": 0}, {},
        {"PCS控制模式": "智慧模式", "PCS排程開關狀態": "關",
         "PCS工作模式": "未知", "PCS功率控制模式": "未知"}))

    # 6) 獨立性：控制模式（getRunMode=auto）不被排程/電網反推
    results.append(run_case(
        "6. 獨立性：getRunMode=auto，排程關，併網",
        "auto", {"schedulePlanSwitch": 0, "manualModeSwitch": 0},
        {**GRID, "energyDispatchingMode": "type.attr.desc.ac"},
        {"PCS控制模式": "智慧模式", "PCS排程開關狀態": "關",
         "PCS工作模式": "併網", "PCS功率控制模式": "交流有功"}))

    # 直接測 parse_pcs_power_control_mode（欄位級）
    print("=" * 60)
    print("parse_pcs_power_control_mode 欄位級：")
    pc_cases = [
        ({"energyDispatchingMode": "type.attr.desc.ac"}, "交流有功"),
        ({"energyDispatchingMode": "type.attr.desc.dc", "dcControlMode": "type.attr.desc.constantCurrent"}, "直流恆流"),
        ({"energyDispatchingMode": "type.attr.desc.dc", "dcControlMode": "type.attr.desc.fixedPower"}, "直流恆功率"),
        # 離網：不論 energyDispatchingMode/dcControlMode 為何，一律顯示「離網交流電壓」
        ({"systemOffGridStatus": "type.attr.desc.true", "energyDispatchingMode": "type.attr.desc.dc",
          "dcControlMode": "type.attr.desc.fixedPower"}, "離網交流電壓"),
        ({"systemGridTiedStatus": "type.attr.desc.offGrid", "energyDispatchingMode": "type.attr.desc.ac"}, "離網交流電壓"),
        ({"energyDispatchingMode": "type.attr.desc.dc"}, "未知"),
        ({}, "未知"),
    ]
    for raw, exp in pc_cases:
        got = parse_pcs_power_control_mode(raw)
        mark = "✓" if got == exp else "✗"
        results.append(got == exp)
        print(f"  {mark} {str(raw):70s} → {got}（expect {exp}）")

    # parse_pcs_current_status（直接對照 API system*Status 欄位值，不推論）
    print("=" * 60)
    print("parse_pcs_current_status 欄位級（直接對照 API 狀態欄位值）：")
    _base = {"systemStandbyStatus": _F, "systemChargingStatus": _F, "systemDischargingStatus": _F,
             "systemBootingStatus": _F, "systemFaultStatus": _NORMAL, "systemFailedStatus": _NORMAL}
    cs_cases = [
        ({**_base, "systemStandbyStatus": "type.attr.desc.standby"}, "待機"),
        ({**_base, "systemChargingStatus": "type.attr.desc.charging"}, "充電"),
        ({**_base, "systemDischargingStatus": "type.attr.desc.discharging"}, "放電"),
        ({**_base, "systemBootingStatus": "type.attr.desc.booting"}, "啟動中"),
        ({**_base, "systemFaultStatus": "type.attr.desc.fault"}, "故障"),
        ({**_base, "systemOnOrOffStatus": "type.attr.desc.stopping"}, "停止中"),
        ({**_base, "systemOnOrOffStatus": "type.attr.desc.stop"}, "待機"),      # 停機/在網待命 → 對照 HMI 待機
        ({**_base, "systemOnOrOffStatus": "type.attr.desc.running"}, "待機"),
        # 實機實測：充電中（systemChargingStatus=charging、systemOnOrOffStatus=running）→ 充電（非停止中）
        ({**_base, "systemChargingStatus": "type.attr.desc.charging",
          "systemOnOrOffStatus": "type.attr.desc.running"}, "充電"),
        ({}, "未知"),
        # 關鍵：工作模式=併網、功率控制模式=交流有功，但狀態欄位為 standby → 待機（不推論成充電）
        ({**_base, "systemStandbyStatus": "type.attr.desc.standby",
          "systemGridTiedStatus": "type.attr.desc.gridTied",
          "energyDispatchingMode": "type.attr.desc.ac"}, "待機"),
    ]
    for raw, exp in cs_cases:
        got = parse_pcs_current_status(raw)
        mark = "✓" if got == exp else "✗"
        results.append(got == exp)
        active = ",".join(f"{k}={str(v).rsplit('.',1)[-1]}" for k, v in raw.items()
                          if str(v).rsplit('.', 1)[-1] not in ("false", "normal")) or "(全空/false)"
        print(f"  {mark} {active:66s} → {got}（expect {exp}）")

    print("=" * 60)
    print(f"結果：{sum(1 for r in results if r)}/{len(results)} PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
