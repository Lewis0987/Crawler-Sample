# -*- coding: utf-8 -*-
"""
phase6_report_bridge.py — Phase 6 ↔ Auto Report 整合橋接（Phase 6.6 / R1）
======================================================================
把 Phase 6 的**控制擁有權狀態**單向餵給既有自動報告子系統，
讓報告改由「實際控制方向與 session lifecycle」驅動，
取代原本「PCS 排程主開關為 ON」這個與 Phase 6 互斥的條件。

    Phase 6 Orchestrator（Authority / LastControl）
            │  唯讀狀態，單向
            ▼
    report_monitor.set_control_session_provider(...)
            │
            ▼
    既有 charge_discharge_report / report_monitor / auto_monitor_service

🔴 **這是唯一的整合點**
    報告層**不** import 任何 Phase 6 模組；
    Phase 6 wiring 層（pcs_auto_control_production）**不** import 任何報告模組。
    兩邊只在本檔相遇 —— 要拆除整合，只需 `uninstall()`。

🔴 **不建立第二套報告系統**
    完全沿用既有 session / resume / finalize / ownership / Named Mutex /
    observer / debounce / cooldown 機制，一行都不重寫。

🔴 **控制與報告解耦**
    · 本檔**只讀** Phase 6 狀態，不呼叫任何控制函式、不產生任何控制決策。
    · 報告子系統失敗**不得**回頭改寫 Decision / Safety Gate / Control Authority。
      本檔提供的資料是單向的；報告層拿不到任何可以改變控制的把手。
    · 反之，Phase 6 也不因報告失敗而改變行為 —— 兩者沒有共用狀態。

🔴 **Ownership 不互相搶奪**
    報告子系統有自己的 Named Mutex（Report Monitor Ownership），
    Phase 6 有自己的 Control Mutex。本檔**不碰任何 Mutex**，
    也不讓任何一方去取得另一方的所有權。

⚠️ OBSERVE_ONLY 期間 Phase 6 不會產生任何 verified direction，
   因此 provider 恆回 None → 既有報告行為**完全不受影響**。

用法（完全離線）
    python phase6_report_bridge.py --show
"""

import argparse

import control_authority as CA
import pcs_control_integration as PCI

# ======================================================================
# 狀態來源
# ======================================================================
# 🔴 「Phase 6 擁有這個作業」的唯一判準：Control Authority 判為 OWNED_BY_PHASE6。
#    那代表 durable LastControl 存在、信任度足夠、狀態相符、TTL 內、
#    且已取得指令後的新功率觀測完成佐證（Blocker 13 時機守則）。
#    ⚠️ 只有 LastControl 存在**不算**；只有設備在運轉**更不算**。
OWNED_STATE = CA.AUTH_OWNED

# Authority 狀態 → 報告方向
_ACTION_OF_STATE = {PCI.PCS_CHARGING: "charge",
                    PCI.PCS_DISCHARGING: "discharge"}


def control_session_state(authority=None, pcs_state=None, last_control=None):
    """
    Phase 6 目前是否擁有一個進行中的充放電作業。

    回傳 `None`（沒有）或
    `{"owned": True, "action": "charge"|"discharge", "target_power_kw": float|None}`。

    🔴 純函式：不讀設備、不連網路、不碰 Mutex、不送任何指令。
    🔴 Fail Closed：任何一項不成立就回 None —— 寧可不建立報告，
       也不要在擁有權未確立時就把作業記到 Phase 6 名下。
    """
    if authority is None or getattr(authority, "state", None) != OWNED_STATE:
        return None
    action = _ACTION_OF_STATE.get(pcs_state)
    if action is None:
        return None
    # LastControl 的 action 必須與設備方向一致（Authority 已檢查過，這裡是第二道）
    lc_action = getattr(last_control, "action", None)
    if lc_action is not None and lc_action != action:
        return None
    return {"owned": True, "action": action,
            "target_power_kw": getattr(last_control, "target_power_kw", None)}


def make_provider(observe_source):
    """
    把一個「取得目前 Phase 6 觀測結果」的 callable，包成報告層要的 provider。

    observe_source : zero-arg callable → 具備 `arbitration`（或本身即為仲裁結果）
                     的物件，需含 authority_state / pcs_state 等稽核欄位。

    ⚠️ provider 每次被呼叫都重新取值 —— 不快取，避免報告層看到過期的擁有權。
    """
    def _provider():
        res = observe_source()
        if res is None:
            return None
        arb = getattr(res, "arbitration", None) or res

        class _Auth(object):
            state = getattr(arb, "authority_state", None)

        return control_session_state(
            authority=_Auth(),
            pcs_state=getattr(arb, "pcs_state", None),
            last_control=_LastControlView(arb))

    return _provider


class _LastControlView(object):
    """從仲裁稽核欄位還原 LastControl 的最小視圖（唯讀）。"""

    def __init__(self, arb):
        self.action = getattr(arb, "lastcontrol_action", None)
        self.target_power_kw = getattr(arb, "lastcontrol_target_kw", None)


# ======================================================================
# 安裝 / 拆除
# ======================================================================
def install(observe_source):
    """
    安裝橋接。回傳一個 zero-arg 的拆除函式。

    ⚠️ 這是**唯一**會讓兩個子系統產生關聯的地方。
    """
    import report_monitor as RM
    RM.set_control_session_provider(make_provider(observe_source))
    return uninstall


def uninstall():
    """拆除橋接 —— 報告行為立即完全還原成 Phase 6 導入前的樣子。"""
    import report_monitor as RM
    RM.clear_control_session_provider()


def installed():
    import report_monitor as RM
    return RM._control_session_provider is not None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase 6 ↔ Auto Report 橋接（唯讀、離線）")
    ap.add_argument("--show", action="store_true")
    ap.parse_args(argv)

    print("== Phase 6.6 Auto Report 橋接（R1，完全離線）==\n")
    print(f"  目前是否已安裝 : {installed()}")
    print(f"  擁有權判準     : Authority == {OWNED_STATE}")
    print(f"  方向對映       : {_ACTION_OF_STATE}")
    print()
    print("  Fail Closed 檢查：")
    for tag, auth, st in (("Authority 非 OWNED", "IDLE", PCI.PCS_CHARGING),
                          ("設備未在充放電", OWNED_STATE, "STANDBY"),
                          ("無 Authority", None, PCI.PCS_CHARGING)):
        class _A(object):
            state = auth
        r = control_session_state(authority=(None if auth is None else _A()),
                                  pcs_state=st)
        print(f"    {tag:<18} → {r}")

    class _A2(object):
        state = OWNED_STATE

    class _LC(object):
        action, target_power_kw = "charge", 5.0

    print(f"    {'OWNED + CHARGING':<18} → "
          f"{control_session_state(authority=_A2(), pcs_state=PCI.PCS_CHARGING, last_control=_LC())}")
    print("\n  ⚠ 本檔不呼叫任何控制函式、不碰任何 Mutex、不建立第二套報告系統。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
