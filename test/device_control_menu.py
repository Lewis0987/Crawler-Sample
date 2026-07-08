# -*- coding: utf-8 -*-
"""
設備控制選單（device_control_menu.py）— 互動式選單，包裝既有腳本
================================================================
不重寫登入/API，改用 subprocess 呼叫既有腳本：
  - 控制：device_control_operator.py --action <action> --execute [--yes] [--power N]
  - 查詢：device_control_scraper.py（唯讀）

不修改 device_control_operator.py / device_control_scraper.py。
token 與 LOGIN_PASSWORD_PAYLOAD 不完整輸出（僅遮罩，由被呼叫腳本負責）。

使用方式：
    cd "D:\\Crawler Sample\\test"
    python device_control_menu.py
"""

import os
import sys
import json
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(os.path.dirname(HERE), "output")
ACTION_RESULT = os.path.join(OUTPUT_DIR, "device_control_action_result.json")
READONLY_RESULT = os.path.join(OUTPUT_DIR, "device_control_readonly.json")

OPERATOR = os.path.join(HERE, "device_control_operator.py")
SCRAPER = os.path.join(HERE, "device_control_scraper.py")

# 一般控制（直接 --execute）
MENU_ACTIONS = {
    "1": ("pcs_manual_on", "PCS 手動模式開"),
    "2": ("pcs_manual_off", "PCS 手動模式關"),
    "3": ("ac_on", "空調開"),
    "4": ("ac_off", "空調關"),
    "11": ("vent_on", "進排風開"),
    "12": ("vent_off", "進排風關"),
    "13": ("cooling_on", "冷卻循環開"),
    "14": ("cooling_off", "冷卻循環關"),
}
# 高風險控制（需 YES 二次確認，並帶 --yes）
HIGH_RISK_ACTIONS = {
    "9": ("battery_power_on", "電池上電"),
    "10": ("battery_power_off", "電池下電"),
}
# PCS 功率控制（6/7 需功率；8 不需）
PCS_POWER_ACTIONS = {
    "6": ("pcs_charge", "PCS 充電", True),
    "7": ("pcs_discharge", "PCS 放電", True),
    "8": ("pcs_stop_power", "PCS 停止充放電", False),
}

MENU_TEXT = """
==============================
設備控制選單
1. PCS 手動模式開
2. PCS 手動模式關
3. 空調開
4. 空調關
5. 查詢目前設備狀態
6. PCS 充電
7. PCS 放電
8. PCS 停止充放電
9. 電池上電
10. 電池下電
11. 進排風開
12. 進排風關
13. 冷卻循環開
14. 冷卻循環關
0. 離開
==============================
"""


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _run_script(args, title):
    print(f"\n>>> 執行：{title}")
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", *args],
            cwd=HERE, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        print(f"[錯誤] 無法啟動腳本：{e}")
        return 1
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.returncode != 0:
        print(f"[錯誤] 腳本回傳非 0（returncode={proc.returncode}）")
        for line in (proc.stderr or "").strip().splitlines()[-8:]:
            print(f"    {line}")
    return proc.returncode


def _fmt_value(v):
    if v is None:
        return "無資料/需登入"
    if isinstance(v, dict) and v.get("_error"):
        return f"錯誤 code={v.get('_error')} msg={v.get('msg')}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _prompt_power():
    """PCS 充/放電功率輸入（0~150）；回傳 float 或 None（取消）。"""
    try:
        raw = input("請輸入功率 kW（0~150）：").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    try:
        p = float(raw)
    except ValueError:
        print("功率格式錯誤，取消。")
        return None
    if not (0 <= p <= 150):
        print("功率超出 0~150，取消。")
        return None
    return p


def show_action_result():
    data = _load_json(ACTION_RESULT)
    if data is None:
        print("尚未產生結果檔（device_control_action_result.json 不存在或無法讀取）")
        return
    cr = data.get("control_response") or {}
    verify = data.get("verify") or {}
    vals = verify.get("values") or {}

    print("\n---------- 控制結果 ----------")
    print(f"action          = {data.get('action')}")
    print(f"dry_run         = {data.get('dry_run')}")
    if isinstance(cr, dict):
        if cr.get("error"):
            print(f"control_response= 例外 {cr.get('error')}")
        else:
            print(f"control_response= http={cr.get('http_status')} code={cr.get('code')} msg={cr.get('msg')}")
    else:
        print("control_response= (無)")
    print(f"control_success = {data.get('control_success')}")
    print(f"verify_success  = {data.get('verify_success')}")
    print(f"success         = {data.get('success')}")
    if vals:
        print("[verify]")
        for k, v in vals.items():
            print(f"  {k} = {v}")
    if verify.get("final_verify_status") is not None:
        print(f"  final_verify_status = {verify.get('final_verify_status')}")
    if verify.get("verify_attempts"):
        print(f"  verify_attempts = {len(verify['verify_attempts'])}")
    for w in (data.get("warnings") or []):
        print(f"  [warn] {w}")


def show_readonly_summary():
    data = _load_json(READONLY_RESULT)
    if data is None:
        print("尚未產生結果檔（device_control_readonly.json 不存在或無法讀取）")
        return
    rd = data.get("readonly_data") or {}
    print("\n---------- 目前設備狀態 ----------")
    print(f"logged_in     = {data.get('logged_in')}")
    print(f"空調狀態       = {_fmt_value(rd.get('空調狀態'))}")
    print(f"PCS狀態        = {_fmt_value(rd.get('PCS狀態'))}")
    print(f"PCS運行模式    = {_fmt_value(rd.get('PCS運行模式'))}")
    sw = rd.get("排程開關狀態")
    if isinstance(sw, dict) and not sw.get("_error"):
        print("手動模式開關狀態 = "
              f"schedulePlanSwitch={sw.get('schedulePlanSwitch')} "
              f"manualModeSwitch={sw.get('manualModeSwitch')}")
    else:
        print(f"手動模式開關狀態 = {_fmt_value(sw)}")


def handle_choice(choice):
    """回傳 True 繼續、False 離開。"""
    if choice == "0":
        print("結束程式。")
        return False

    # 一般控制
    if choice in MENU_ACTIONS:
        action, label = MENU_ACTIONS[choice]
        rc = _run_script([OPERATOR, "--action", action, "--execute"], f"{label}（{action}）")
        show_action_result()
        if rc != 0:
            print("（注意：控制腳本回傳非 0，請檢視上方訊息）")
        return True

    # 高風險控制（電池上下電）：先 YES 二次確認，再帶 --yes
    if choice in HIGH_RISK_ACTIONS:
        action, label = HIGH_RISK_ACTIONS[choice]
        print(f"\n⚠️⚠️ 高風險控制：{label}（{action}）將實際送出。")
        print("此為電池上/下電，請確認現場安全。輸入 YES 才會送出（其他輸入取消）：")
        try:
            ans = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("未取得確認，取消。")
            return True
        if ans != "YES":
            print("已取消，未送出。")
            return True
        rc = _run_script([OPERATOR, "--action", action, "--execute", "--yes"], f"{label}（{action}）")
        show_action_result()
        if rc != 0:
            print("（注意：控制腳本回傳非 0，請檢視上方訊息）")
        return True

    # PCS 功率控制（6 充電 / 7 放電 需功率；8 停止不需）
    if choice in PCS_POWER_ACTIONS:
        action, label, needs_power = PCS_POWER_ACTIONS[choice]
        args = [OPERATOR, "--action", action, "--execute"]
        if needs_power:
            p = _prompt_power()
            if p is None:
                print("已取消。")
                return True
            args += ["--power", str(p)]
        rc = _run_script(args, f"{label}（{action}）")
        show_action_result()
        if rc != 0:
            print("（注意：控制腳本回傳非 0，請檢視上方訊息）")
        return True

    # 查詢
    if choice == "5":
        rc = _run_script([SCRAPER], "查詢目前設備狀態（唯讀）")
        show_readonly_summary()
        return True

    print("無效選項，請重新輸入。")
    return True


def main():
    print("設備控制選單啟動（控制動作會實際送出；查詢為唯讀；電池上下電需 YES 確認）")
    while True:
        print(MENU_TEXT)
        try:
            choice = input("請輸入選項：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n偵測到中斷，結束程式。")
            break
        try:
            if not handle_choice(choice):
                break
        except Exception as e:
            print(f"[錯誤] 執行選項 {choice} 時發生例外：{e}")
        print()


if __name__ == "__main__":
    main()
