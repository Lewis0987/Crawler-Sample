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
_GT = {"systemGridTiedStatus": "type.attr.desc.gridTied", "systemOffGridStatus": _F}  # 併網
_OFF = {"systemOffGridStatus": "type.attr.desc.true"}                                  # 離網


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

    # parse_pcs_current_status（100% 複製前端 pcsMode_US.vue 的 He 組合邏輯）
    #   固定順序：systemOnOrOffStatus[永遠] / systemFaultStatus / systemGridTiedStatus /
    #             systemOffGridStatus / systemChargingStatus / systemDischargingStatus[oldValue=="1"]
    #   顯示文字用該 mark 的中文 value，以「 / 」串接。
    print("=" * 60)
    print("parse_pcs_current_status（前端 pcsMode_US He 組合：value + oldValue）：")

    def M(mark, value, old="0"):
        return {"mark": mark, "value": value, "oldValue": old}

    cs_cases = [
        # 執行 + 併網 + 充電（gridTied/charging oldValue=1）
        ([M("systemOnOrOffStatus", "執行"), M("systemGridTiedStatus", "併網", "1"),
          M("systemChargingStatus", "充電", "1")], "執行 / 併網 / 充電"),
        # 停止 + 故障 + 離網
        ([M("systemOnOrOffStatus", "停止"), M("systemFaultStatus", "故障", "1"),
          M("systemOffGridStatus", "離網", "1")], "停止 / 故障 / 離網"),
        # 待機 + 離網（只有 onOff 與 offGrid）
        ([M("systemOnOrOffStatus", "待機"), M("systemOffGridStatus", "離網", "1")], "待機 / 離網"),
        # 目前實機：停止 + 併網（onOff always；grid oldValue=1；其餘 0）
        ([M("systemOnOrOffStatus", "停止"), M("systemFaultStatus", "正常", "0"),
          M("systemGridTiedStatus", "併網", "1"), M("systemOffGridStatus", "否", "0"),
          M("systemChargingStatus", "否", "0"), M("systemDischargingStatus", "否", "0")], "停止 / 併網"),
        # systemOnOrOffStatus 永遠顯示（即使 oldValue!=1）
        ([M("systemOnOrOffStatus", "執行", "0")], "執行"),
        # 其餘 5 欄 oldValue!=1 一律不顯示（不補「正常/未知」）
        ([M("systemOnOrOffStatus", "停止"), M("systemFaultStatus", "正常", "0"),
          M("systemGridTiedStatus", "併網", "0")], "停止"),
        # 順序固定：charging(第5) 在 discharging(第6) 前，且 gridTied(第3) 在 charging 前
        ([M("systemDischargingStatus", "放電", "1"), M("systemChargingStatus", "充電", "1"),
          M("systemGridTiedStatus", "併網", "1"), M("systemOnOrOffStatus", "執行")],
         "執行 / 併網 / 充電 / 放電"),
        # onOff 缺失則略過該段（不補預設），只顯示其餘符合者
        ([M("systemGridTiedStatus", "併網", "1")], "併網"),
        # value 空字串 → 該段略過
        ([M("systemOnOrOffStatus", "  ", "0"), M("systemGridTiedStatus", "併網", "1")], "併網"),
        # 完全無資料 → 空字串
        ([], ""),
    ]
    for metrics, exp in cs_cases:
        got = parse_pcs_current_status(metrics)
        mark = "✓" if got == exp else "✗"
        results.append(got == exp)
        print(f"  {mark} → {got!r}（expect {exp!r}）")

    print("=" * 60)
    print(f"結果：{sum(1 for r in results if r)}/{len(results)} PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
