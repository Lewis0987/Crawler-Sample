# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— 控制指令對照（集中定義；唯一一份）
==========================================================
把既有控制 action（device_control_operator 的 ACTIONS 名稱，**不改名**）對應到：
方向（charge／discharge／stop）、控制模式、設定值單位。

cert_runtime（寫 commands.jsonl）與 cert_report_xlsx（控制紀錄頁）都只查這裡，不另外硬編碼。
本模組只是對照表：不 import 控制模組、不送任何指令。
與 device_control_operator.ACTIONS 的一致性由 test_cert_p3_integration 交叉檢查。
"""

# 控制模式 → (顯示名稱, 設定值單位)；stop 沒有設定值
CONTROL_MODES = {
    "ac_active": ("交流有功功率", "kW"),
    "dc_current": ("直流恆流", "A"),
    "dc_power": ("直流恆功率", "kW"),
    "stop": ("停止", None),
}

# 既有 action → (方向, 控制模式)
ACTION_INFO = {
    "pcs_charge": ("charge", "ac_active"),                  # Menu 4 交流有功
    "pcs_discharge": ("discharge", "ac_active"),
    "pcs_dc_current_charge": ("charge", "dc_current"),      # Menu 5 直流恆流
    "pcs_dc_current_discharge": ("discharge", "dc_current"),
    "pcs_dc_power_charge": ("charge", "dc_power"),          # Menu 6 直流恆功率
    "pcs_dc_power_discharge": ("discharge", "dc_power"),
    "pcs_stop_power": ("stop", "stop"),                     # Menu 7 停止充放電
}

DIRECTION_TEXT = {"charge": "充電", "discharge": "放電", "stop": "停止"}


def describe(action):
    """
    action → {"direction", "control_mode", "control_mode_text", "unit"}。
    不在對照表的 action（例：空調、排程開關等非充放電控制）→ 全部 None（呈現為 N/A），不推測。
    """
    if action not in ACTION_INFO:
        return {"direction": None, "control_mode": None, "control_mode_text": None, "unit": None}
    direction, mode = ACTION_INFO[action]
    text, unit = CONTROL_MODES[mode]
    return {"direction": direction, "control_mode": mode, "control_mode_text": text, "unit": unit}
