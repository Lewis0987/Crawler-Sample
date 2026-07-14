# -*- coding: utf-8 -*-
"""
設備控制選單（device_control_menu.py）— 互動式選單，包裝既有腳本
================================================================
不重寫登入/API，改用 subprocess 呼叫既有腳本：
  - 控制：device_control_operator.py --action <action> --execute [--yes] [--power N]
  - 查詢：device_control_scraper.py（唯讀）

不修改 device_control_operator.py / device_control_scraper.py。
登入與遮罩由 api_client 統一負責；本檔不輸出任何機密（token / 密碼 / 密文 / 公鑰）。

使用方式：
    cd "D:\\Crawler Sample\\test"
    python device_control_menu.py
"""

import os
import sys
import json
import time
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

# 查詢目前設備狀態
QUERY_CHOICE = "1"
# 一般控制（直接 --execute）
MENU_ACTIONS = {
    "2": ("pcs_manual_on", "PCS 手動模式開"),
    "3": ("pcs_manual_off", "PCS 手動模式關"),
    "10": ("ac_on", "空調開"),
    "11": ("ac_off", "空調關"),
    "12": ("vent_on", "進排風開"),
    "13": ("vent_off", "進排風關"),
    "14": ("cooling_on", "冷卻循環開"),
    "15": ("cooling_off", "冷卻循環關"),
}
# 高風險控制（需 YES 二次確認，並帶 --yes）
HIGH_RISK_ACTIONS = {
    "8": ("battery_power_on", "電池上電"),
    "9": ("battery_power_off", "電池下電"),
}
# PCS 功率控制：3 種模式（4/5/6），選後進入第二層（充/放電）。
# 三種模式 payload/enum/方向皆經 DevTools 確認、可實送。
PCS_MODE_MENU = {
    "4": {"label": "交流有功", "param": "有功功率", "unit": "kW", "lo": 0, "hi": 150,
          "charge": "pcs_charge", "discharge": "pcs_discharge", "confirmed": True},
    "5": {"label": "直流恆流", "param": "直流電流", "unit": "A", "lo": 0, "hi": 150,
          "charge": "pcs_dc_current_charge", "discharge": "pcs_dc_current_discharge", "confirmed": True},
    "6": {"label": "直流恆功率", "param": "直流功率", "unit": "kW", "lo": 0, "hi": 150,
          "charge": "pcs_dc_power_charge", "discharge": "pcs_dc_power_discharge", "confirmed": True},
}
PCS_STOP_CHOICE = "7"  # PCS 停止充放電（安全停止，維持可執行）

# ---- 控制後等待驗證（設備約 0~60 秒才完成切換）----
VERIFY_TIMEOUT = 60      # 最大等待秒數
VERIFY_INTERVAL = 2      # 每幾秒 verify 一次
# action → (status_summary 區塊, 欄位, 期望值需包含的字串)
VERIFY_EXPECT = {
    "pcs_manual_on":  ("PCS", "PCS手動模式開關", "已啟用"),
    "pcs_manual_off": ("PCS", "PCS手動模式開關", "已停用"),
    "battery_power_on":  ("電池", "電池上下電狀態", "已上電"),
    "battery_power_off": ("電池", "電池上下電狀態", "已下電"),
    "ac_on":  ("空調", "空調開關", "開"),
    "ac_off": ("空調", "空調開關", "關"),
    "vent_on":  ("進排風", "進排風執行狀態", "開啟"),
    "vent_off": ("進排風", "進排風執行狀態", "停止"),
    "cooling_on":  ("冷卻循環", "冷卻循環水泵狀態", "開啟"),
    "cooling_off": ("冷卻循環", "冷卻循環水泵狀態", "關閉"),
    # pcs_charge/discharge/stop：只讀來源無單一可靠回讀欄位（verify partial）→ 不做輪詢
}

MENU_TEXT = """
==============================
設備控制選單
==============================

1. 查詢目前設備狀態

2. PCS 手動模式開
3. PCS 手動模式關

4. PCS 交流有功控制（充/放電）
5. PCS 直流恆流控制（充/放電）
6. PCS 直流恆功率控制（充/放電）
7. PCS 停止充放電

8. 電池上電
9. 電池下電

10. 空調開
11. 空調關

12. 進排風開
13. 進排風關

14. 冷卻循環開
15. 冷卻循環關

0. 離開
==============================
"""


# ---- 顏色（開/開啟/已啟用/已上電=藍；關/關閉/已停用/已下電=紅）----
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"

# 只有在 TTY 且成功啟用 ANSI 時才上色；非 TTY / 不支援時退化成純文字。
_USE_COLOR = False


def _enable_ansi():
    """依 stdout 是否為 TTY 決定是否上色；Windows 另啟用 VT 色碼處理。"""
    global _USE_COLOR
    if not sys.stdout.isatty():
        _USE_COLOR = False
        return
    _USE_COLOR = True
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


def colorize(v):
    """開/開啟/已啟用/已上電→藍；關/關閉/已停用/已下電→紅；非 TTY 或其餘→純文字。"""
    s = str(v)
    if not _USE_COLOR:
        return s
    if s in ("開", "ON", "on", "true", "1") or "開啟" in s or "啟用" in s or "已上電" in s:
        return BLUE + s + RESET
    if s in ("關", "OFF", "off", "false", "0") or "關閉" in s or "已停用" in s or "已下電" in s:
        return RED + s + RESET
    return s


# 各區塊「控制選單」參考清單（僅顯示、不自動執行）
_CONTROL_MENUS = {
    "PCS": ["交流有功控制", "直流恆流控制", "直流恆功率控制", "PCS停機"],
    "電池": ["電池上電", "電池下電"],
    "進排風": ["開啟進排風", "關閉進排風"],
    "空調": ["開啟空調", "關閉空調", "設定空調模式", "設定溫度"],
    "冷卻循環": ["開啟冷卻循環", "關閉冷卻循環"],
}
# 區塊顯示順序（依需求：PCS→電池→進排風→空調→冷卻循環）
_DASHBOARD_ORDER = ("PCS", "電池", "進排風", "空調", "冷卻循環")
# 區塊之間的分隔線（取代原本的空白行）
_BLOCK_SEP = "─" * 24
# 電池區塊反饋欄位（總覽不重複顯示，仍保留在 scraper console / JSON）
_FEEDBACK_LABELS = {"主正反饋", "主正", "主負反饋", "主負", "環流"}


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


def _prompt_value(param, unit, lo, hi):
    """通用參數輸入（數字 + 範圍驗證）；回傳 float 或 None（取消/不合法）。"""
    try:
        raw = input(f"請輸入{param}（{lo}~{hi} {unit}）：").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    try:
        v = float(raw)
    except ValueError:
        print(f"{param}格式錯誤（需數字），取消。")
        return None
    if not (lo <= v <= hi):
        print(f"{param}超出允許範圍 {lo}~{hi} {unit}，取消。")
        return None
    return v


def _pcs_mode_submenu(cfg):
    """
    PCS 控制第二層：選充/放電 → 輸入參數 → 二次 YES 確認。
    依 confirmed 分流：
      - confirmed=True（交流有功）：【正式控制模式】，YES 後呼叫 operator --execute 實送並 verify。
      - confirmed=False（安全網）：僅 dry-run（不帶 --execute，不送控制 API）。
    """
    label, param, unit = cfg["label"], cfg["param"], cfg["unit"]
    confirmed = bool(cfg.get("confirmed"))
    print(f"\n【{label}控制】")
    if not confirmed:
        # 安全網：若某模式未確認（confirmed=False），仍只 dry-run，不送控制 API。
        print("此模式尚未確認 payload，僅產生 dry-run，不會送出控制 API。")
    print("方向：")
    print("1. 充電")
    print("2. 放電")
    print("0. 返回")
    try:
        d = input("請選擇方向：").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if d not in ("1", "2"):
        print("已返回。")
        return
    direction = "charge" if d == "1" else "discharge"
    dir_label = "充電" if d == "1" else "放電"
    action = cfg[direction]

    v = _prompt_value(param, unit, cfg["lo"], cfg["hi"])
    if v is None:
        return
    v_str = str(int(v) if float(v).is_integer() else v)

    # 執行摘要
    print("\n即將執行：")
    print(f"控制類型：{label}")
    print(f"方向：{dir_label}")
    print(f"設定值：{v_str} {unit}")

    if confirmed:
        # 正式實送
        print("\n【正式控制模式】")
        print("此操作將實際送出 PCS 控制命令。")
        try:
            ans = input("確認執行？請輸入完整大寫 YES：").strip()
        except (EOFError, KeyboardInterrupt):
            print("未取得確認，取消。")
            return
        if ans != "YES":
            print("未輸入完整大寫 YES，已取消，未送出。")
            return
        rc = _run_script([OPERATOR, "--action", action, "--power", v_str, "--execute"],
                         f"{label}{dir_label}（{action}）execute")
        show_action_result()
        wait_and_verify(action)      # 送出後回讀驗證
    else:
        # 未確認模式（安全網）→ 僅 dry-run
        print("\n此模式未確認 payload，僅產生 dry-run 預覽（不會送出控制 API）。")
        try:
            ans = input("是否產生 dry-run 預覽？請輸入完整大寫 YES：").strip()
        except (EOFError, KeyboardInterrupt):
            print("未取得確認，取消。")
            return
        if ans != "YES":
            print("已取消。")
            return
        rc = _run_script([OPERATOR, "--action", action, "--power", v_str],
                         f"{label}{dir_label}（{action}）dry-run")
        show_action_result()
    if rc != 0:
        print("（注意：控制腳本回傳非 0，請檢視上方訊息）")


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
    pc = data.get("precheck")
    if pc:
        state = pc.get("battery_power_state") or pc.get("battery_operation_state")
        print(f"前置檢查        = 狀態={state} / "
              f"allowed={pc.get('allowed')} / reason={pc.get('reason')}")
    if data.get("blocked"):
        print("狀態            = BLOCKED（前置檢查未通過，未送出控制）")
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


def refresh_status_silent():
    """靜默執行唯讀 scraper 刷新狀態，回傳 status_summary（不印任何東西）。"""
    try:
        subprocess.run(
            [sys.executable, "-X", "utf8", SCRAPER],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception:
        return {}
    return (_load_json(READONLY_RESULT) or {}).get("status_summary") or {}


def wait_and_verify(action):
    """
    控制成功後等待設備完成（最多 VERIFY_TIMEOUT 秒），每 VERIFY_INTERVAL 秒 verify 一次；
    成功立即結束，逾時印 Timeout 與最後狀態。保留 operator 既有 verify 判斷。
    """
    data = _load_json(ACTION_RESULT) or {}
    if not data.get("control_success"):
        print("（控制未成功送出，略過等待驗證）")
        return
    exp = VERIFY_EXPECT.get(action)
    if not exp:
        print("（控制已送出；此動作無明確狀態回讀欄位，略過輪詢，請參考上方 verify 結果）")
        return
    block, field, expected = exp
    print("-" * 40)
    print("控制指令已送出")
    print("等待設備完成動作...")
    print("-" * 40)
    elapsed, last = 0, None
    while elapsed < VERIFY_TIMEOUT:
        time.sleep(VERIFY_INTERVAL)
        elapsed += VERIFY_INTERVAL
        status = refresh_status_silent()
        val = (status.get(block) or {}).get(field)
        last = val
        if val is not None and expected in str(val):
            print(f"[{elapsed:02d}/{VERIFY_TIMEOUT}] 驗證成功 ✓ （{field}={val}）")
            return
        print(f"[{elapsed:02d}/{VERIFY_TIMEOUT}] 驗證中...（目前 {field}={val}）")
    print(f"Timeout：{VERIFY_TIMEOUT}s 內未達預期（{field} 應含「{expected}」，最後={last}）")


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
        wait_and_verify(action)
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
        wait_and_verify(action)
        if rc != 0:
            print("（注意：控制腳本回傳非 0，請檢視上方訊息）")
        return True

    # PCS 功率控制（6 交流有功 / 7 直流恆流 / 8 直流恆功率）→ 進入第二層
    if choice in PCS_MODE_MENU:
        _pcs_mode_submenu(PCS_MODE_MENU[choice])
        return True

    # PCS 停止充放電（安全停止；維持可執行）
    if choice == PCS_STOP_CHOICE:
        rc = _run_script([OPERATOR, "--action", "pcs_stop_power", "--execute"], "PCS 停止充放電")
        show_action_result()
        if rc != 0:
            print("（注意：控制腳本回傳非 0，請檢視上方訊息）")
        return True

    # 查詢：scraper 本身已印一份完整設備狀態（含 JSON 寫入行），不再重複列印。
    if choice == QUERY_CHOICE:
        rc = _run_script([SCRAPER], "查詢目前設備狀態（唯讀）")
        if rc != 0:
            print("（注意：查詢腳本回傳非 0，請檢視上方訊息）")
        return True

    print("無效選項，請重新輸入。")
    return True


def refresh_status():
    """執行唯讀 scraper 刷新狀態，只印登入 4 行，回傳 status_summary。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", SCRAPER],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        print(f"[錯誤] 無法刷新狀態：{e}")
        return {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith(("嘗試登入", "[ENV] loaded", "HMI login")):
            print(line)
    if proc.returncode != 0:
        print("[錯誤] 狀態查詢失敗")
        for l in (proc.stderr or "").strip().splitlines()[-5:]:
            print("   ", l)
    data = _load_json(READONLY_RESULT) or {}
    return data.get("status_summary") or {}


def _core_switch_fields(block, fields):
    """只取各區塊「當前開關狀態」核心欄位，其餘詳細狀態不顯示。"""
    out = {}
    if block == "PCS":
        # PCS 五個固定狀態欄位（皆唯讀，永遠顯示，不因控制功能增減而移除）：
        # 當前狀態（API 實際運轉狀態）/ 控制模式 / 排程開關狀態 / 工作模式 / 功率控制模式。
        for key in ("PCS當前狀態", "PCS控制模式", "PCS排程開關狀態", "PCS工作模式", "PCS功率控制模式"):
            if key in fields:
                out[key] = fields[key]
    elif block == "電池":
        # 依「最終格式」規範：儀表板電池區塊只顯示核心開關「電池上下電狀態」。
        # （放電/充電/待機的「當前狀態」不在儀表板顯示，但仍用於 operator 的電池下電前置檢查。）
        out["電池上下電狀態"] = fields.get("電池上下電狀態", "暫無資料")
    elif block == "進排風":
        v = str(fields.get("進排風執行狀態", ""))
        out["進排風開關"] = ("開啟" if ("開啟" in v or "運轉" in v)
                            else ("關閉" if v else "暫無資料"))
    elif block == "空調":
        v = str(fields.get("空調開關", ""))
        out["空調開關"] = {"開": "開啟", "關": "關閉"}.get(v, v or "暫無資料")
    elif block == "冷卻循環":
        out["冷卻循環開關"] = fields.get("冷卻循環水泵狀態", "暫無資料")
    return out


def render_dashboard(status):
    """依區塊顯示：僅「當前開關狀態」（含顏色）+ 控制選單參考（僅顯示，不執行）。
    區塊之間以分隔線區隔（取代原本的空白行）。"""
    for i, block in enumerate(_DASHBOARD_ORDER):
        simp = _core_switch_fields(block, status.get(block) or {})
        if i > 0:                       # 區塊之間插入分隔線（第一個區塊前不加）
            print(f"\n{_BLOCK_SEP}")
        print(f"\n【{block}】狀態：")
        if simp:
            for label, value in simp.items():
                print(f"{label}：{colorize(value)}")
        else:
            print("  （暫無資料）")
        print("控制選單：")
        for j, item in enumerate(_CONTROL_MENUS.get(block, []), 1):
            print(f"{j}. {item}")


def _exec_menu():
    """實際執行控制的子選單（沿用既有 handle_choice 邏輯）。"""
    while True:
        print(MENU_TEXT)
        try:
            choice = input("請輸入選項：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        try:
            if not handle_choice(choice):
                break
        except Exception as e:
            print(f"[錯誤] 執行選項 {choice} 時發生例外：{e}")
        print()


def main():
    _enable_ansi()
    print("設備控制總覽（狀態為顯示用，不會自動執行控制）")
    while True:
        status = refresh_status()
        render_dashboard(status)
        try:
            choice = input("\n[r] 重新整理　[e] 進入控制執行選單　[0] 離開：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n結束程式。")
            break
        if choice == "0":
            print("結束程式。")
            break
        if choice == "e":
            _exec_menu()
        # 其他（含 r / 空白）→ 迴圈頂端重新刷新


if __name__ == "__main__":
    main()
