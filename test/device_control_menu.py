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
import threading
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass    # 舊環境無 reconfigure 或 stdout 已包裝：沿用現有編碼（非報告錯誤）

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(os.path.dirname(HERE), "output")
ACTION_RESULT = os.path.join(OUTPUT_DIR, "device_control_action_result.json")
READONLY_RESULT = os.path.join(OUTPUT_DIR, "device_control_readonly.json")

OPERATOR = os.path.join(HERE, "device_control_operator.py")
SCRAPER = os.path.join(HERE, "device_control_scraper.py")

# 充放電報告（獨立唯讀模組，以 import 呼叫；本檔不修改該模組、不送任何控制 API）
try:
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CDR_CFG
    _REPORT_AVAILABLE = True
    _REPORT_IMPORT_ERR = None
except Exception as _e:            # 匯入失敗不影響既有控制選單
    CDR = None
    CDR_CFG = None
    _REPORT_AVAILABLE = False
    _REPORT_IMPORT_ERR = str(_e)

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

# ---- 控制後等待驗證 ----
# 每種設備的翻轉時間不同（實測：空調風機停/啟受壓縮機保護影響，可達 ~120s），故 timeout 各自獨立設定。
VERIFY_TIMEOUT = {
    "battery": 60,
    "pcs": 60,
    "fan": 60,       # 進排風
    "water": 60,     # 冷卻循環
    "ac_on": 180,    # 空調開（風機翻轉可能 >60s）
    "ac_off": 180,   # 空調關
}
VERIFY_TIMEOUT_DEFAULT = 60
VERIFY_INTERVAL = 2      # 每幾秒 verify 一次


def _verify_timeout(action):
    """依 action 取對應設備的 verify timeout（秒）。"""
    if action in ("ac_on", "ac_off"):
        return VERIFY_TIMEOUT.get(action, VERIFY_TIMEOUT_DEFAULT)
    key = ("battery" if action.startswith("battery")
           else "pcs" if action.startswith("pcs")
           else "fan" if action.startswith("vent")
           else "water" if action.startswith("cooling")
           else None)
    return VERIFY_TIMEOUT.get(key, VERIFY_TIMEOUT_DEFAULT)
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

16. 手動開始合併充放電報告（備用）
17. 手動結束合併充放電報告（備用）
18. 查看最近報告

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
            _USE_COLOR = False        # 無法啟用 VT 色碼 → 退化為純文字（不靜默忽略）


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
        # 控制成功 → 自動跟隨：輪詢實際方向並自動建立/切換報告 Session（唯讀，不送任何控制）
        if _REPORT_AVAILABLE and _control_succeeded():
            report_on_charge_discharge_control(direction, label, v)
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


# ---- 動態進度行（同一行覆寫，不逐行新增）----
_PROGRESS_LINE_ACTIVE = False
_PROGRESS_LINE_LENGTH = 0


def print_progress_line(message):
    """在同一行覆寫等待進度，不新增換行。"""
    global _PROGRESS_LINE_ACTIVE, _PROGRESS_LINE_LENGTH
    text = str(message)
    padded = text.ljust(_PROGRESS_LINE_LENGTH)   # 新內容較短時補空白清掉舊字元
    print("\r" + padded, end="", flush=True)
    _PROGRESS_LINE_LENGTH = len(text)
    _PROGRESS_LINE_ACTIVE = True


def finish_progress_line(message=None):
    """結束動態行，必要時先覆寫最終文字，再換行。"""
    global _PROGRESS_LINE_ACTIVE, _PROGRESS_LINE_LENGTH
    if message is not None:
        text = str(message)
        padded = text.ljust(_PROGRESS_LINE_LENGTH)
        print("\r" + padded, end="", flush=True)
    if _PROGRESS_LINE_ACTIVE or message is not None:
        print(flush=True)
    _PROGRESS_LINE_ACTIVE = False
    _PROGRESS_LINE_LENGTH = 0


def wait_and_verify(action, start=None):
    """
    控制送出後即時輪詢驗證（最多 VERIFY_TIMEOUT 秒）。
    秒數每秒在「同一行」覆寫更新；狀態改變 / 驗證成功 / 逾時才結束該行、換行顯示下一行。
    背景執行緒負責向設備輪詢（子行程耗時數秒），主迴圈每秒更新秒數，兩者解耦。
    start：連續計時起點；由 _execute_live 傳入時涵蓋「送出階段」（[NN/60] 連續）。
           為 None（獨立呼叫，如 PCS 模式）時自建起點並先做 control_success 檢查與表頭。
    """
    to = _verify_timeout(action)      # 每種設備獨立 timeout
    live = start is not None
    if not live:
        data = _load_json(ACTION_RESULT) or {}
        if not data.get("control_success"):
            print("（控制未成功送出，略過等待驗證）", flush=True)
            return None
    exp = VERIFY_EXPECT.get(action)
    if not exp:
        el = 0 if start is None else int(time.monotonic() - start)
        print(f"[{el:02d}/{to}] 此動作無明確狀態回讀欄位，略過輪詢（請參考控制結果）", flush=True)
        return None
    block, field, expected = exp
    if not live:
        print("-" * 90, flush=True)
        print("控制指令已送出", flush=True)
        print("等待設備完成動作...", flush=True)
        print("-" * 90, flush=True)
        start = time.monotonic()

    # 背景執行緒輪詢設備狀態（子行程耗時數秒），主迴圈每秒在同一行更新秒數。
    latest = {"val": None}
    stop = threading.Event()

    def _poller():
        while not stop.is_set():
            status = refresh_status_silent()
            latest["val"] = (status.get(block) or {}).get(field)
            if stop.wait(VERIFY_INTERVAL):
                break

    th = threading.Thread(target=_poller, daemon=True)
    th.start()

    result, line_val = False, None
    try:
        while True:
            elapsed = int(time.monotonic() - start)
            val = latest["val"]
            # 設備完成（verify_success）：唯一以 verify 欄位翻轉判定（如空調＝indoorFanStatus）
            if val is not None and expected in str(val):
                finish_progress_line()
                print(f"[{elapsed:02d}/{to}] {field}：{val}", flush=True)
                print(f"[{elapsed:02d}/{to}] 驗證成功 ✓（設備完成：{field}={val}）", flush=True)
                result = True
                break
            # 逾時：結束目前動態行，換行顯示 Timeout
            if elapsed >= to:
                finish_progress_line()
                print(f"[{to:02d}/{to}] Timeout：{to}s 內未達預期"
                      f"（{field} 應含「{expected}」，最後={val}；控制已送出成功，設備可能仍在動作中）", flush=True)
                result = False
                break
            # 狀態改變：先結束（保留）目前動態行，於新行重新開始動態更新
            if line_val is not None and val != line_val:
                finish_progress_line()
            if val is None:
                print_progress_line(f"[{elapsed:02d}/{to}] 等待設備完成... 目前 {field}=（讀取中）")
            else:
                print_progress_line(f"[{elapsed:02d}/{to}] 等待設備完成... 目前 {field}：{val}")
            line_val = val
            time.sleep(1)
    finally:
        stop.set()
    return result


def _execute_live(operator_args, action, label):
    """
    送出控制並「即時」顯示等待計時（單一連續計時、逐行 flush）：
      YES → >>> 執行 → [00/60] 正在送出控制指令... → 送出（operator 帶 --no-verify，送完即返回）
          → [NN/60] 控制指令已送出，等待設備完成動作... → 即時輪詢驗證。
    operator 以 --no-verify 送出（不做內部輪詢），故按下 YES 後立刻開始計時，不必等子程式跑完。
    """
    print(f"\n>>> 執行：{label}（{action}）", flush=True)
    to = _verify_timeout(action)      # 每種設備獨立 timeout
    start = time.monotonic()

    # 送出階段：operator 以子行程非阻塞執行，主迴圈每秒在同一行覆寫秒數（不逐行新增）。
    print_progress_line(f"[{0:02d}/{to}] 正在送出控制指令...")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", *operator_args, "--no-verify"],
            cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        finish_progress_line()
        print(f"[錯誤] 無法啟動控制腳本：{e}", flush=True)
        return 1
    while proc.poll() is None:
        print_progress_line(f"[{int(time.monotonic() - start):02d}/{to}] 正在送出控制指令...")
        time.sleep(1)
    rc = proc.returncode

    data = _load_json(ACTION_RESULT) or {}
    sent = max(1, int(time.monotonic() - start))
    if data.get("blocked"):
        finish_progress_line(f"[{sent:02d}/{to}] 前置檢查未通過，未送出控制")
        _print_control_detail(data, None)
        return rc
    if not data.get("control_success"):
        finish_progress_line(f"[{sent:02d}/{to}] 控制指令送出失敗")
        _print_control_detail(data, None)
        if rc != 0:
            print("（注意：控制腳本回傳非 0，請檢視上方訊息）", flush=True)
        return rc

    # 第一階段「控制已接受」：POST http200（control_success）→ 設備已接受命令
    finish_progress_line(f"[{sent:02d}/{to}] 控制已送出 ✓（設備已接受命令）")
    print(f"[{sent:02d}/{to}] 等待設備完成動作...", flush=True)
    # 第二階段「設備完成」：等待 verify 欄位（如 indoorFanStatus）真正翻轉 → 驗證成功 ✓
    verify_result = wait_and_verify(action, start=start)     # 連續計時、即時輪詢（同一行覆寫）
    # 詳細資訊（action/mode/payload/response/verify/JSON）一律延後到計時與驗證之後才顯示
    _print_control_detail(data, verify_result)
    if rc != 0:
        print("（注意：控制腳本回傳非 0，請檢視上方訊息）", flush=True)
    return rc


def _print_control_detail(data, verify_result):
    """控制詳細資訊（延後顯示於等待計時與驗證結果之下）。verify_result 為選單即時輪詢結果。"""
    req = data.get("control_request") or {}
    cr = data.get("control_response") or {}
    control_success = data.get("control_success")
    # verify_success 以「選單即時輪詢」結果為準（operator --no-verify 未做內部驗證）
    if verify_result is True:
        vs = True
    elif verify_result is False:
        vs = "timeout"
    else:
        vs = data.get("verify_success")          # None：無需/略過驗證
    if control_success is False:
        success = False
    elif vs in (True, None):
        success = bool(control_success)
    else:
        success = False

    print("\n================ 控制詳細資訊 ================", flush=True)
    print(f"action          : {data.get('action')}", flush=True)
    print(f"mode            : {'DRY-RUN（不送出）' if data.get('dry_run') else 'EXECUTE（真的送出）'}", flush=True)
    print(f"control_req     : {req.get('method')} {req.get('endpoint')}", flush=True)
    print(f"payload         : {json.dumps(req.get('payload'), ensure_ascii=False)}", flush=True)
    pc = data.get("precheck")
    if pc:
        state = pc.get("battery_power_state") or pc.get("battery_operation_state")
        print(f"precheck        : 狀態={state} / allowed={pc.get('allowed')} / reason={pc.get('reason')}", flush=True)
    if data.get("blocked"):
        print("control_resp    : BLOCKED（前置檢查未通過，未送出控制）", flush=True)
    elif isinstance(cr, dict) and cr:
        if cr.get("error"):
            print(f"control_resp    : 例外 {cr.get('error')}", flush=True)
        else:
            print(f"control_resp    : http={cr.get('http_status')} code={cr.get('code')} msg={cr.get('msg')}", flush=True)
    print(f"control_success : {control_success}", flush=True)
    print(f"verify_success  : {vs}", flush=True)
    print(f"success         : {success}", flush=True)
    for w in (data.get("warnings") or []):
        print(f"  [warn] {w}", flush=True)
    print(f"結果已寫入：{ACTION_RESULT}", flush=True)


# ======================================================================
# 充放電報告控制器（Menu 16/17/18）
# 以獨立模組 charge_discharge_report 的 ReportSession 在背景執行緒取樣；
# 本檔僅 orchestration：不修改報告模組、不送任何控制 API、全程唯讀 GET。
# ======================================================================
_report = {"session": None, "thread": None, "stop": None,
           "lock": threading.Lock(), "finalized": False,
           "pending_end_reason": None, "client": None}

# 自動跟隨控制流程的輪詢設定（純 GET 唯讀）
AUTO_POLL_TIMEOUT_SEC = 90      # 送控制後最長等待實際狀態改變（畫面訊息一律動態引用此值）
AUTO_POLL_INTERVAL_SEC = 3


def _report_output_root():
    return os.path.join(OUTPUT_DIR, CDR_CFG.OUTPUT_SUBDIR)


def _report_client():
    """共用已登入的 ApiClient（只登入一次；供輪詢與報告 Session 共用）。失敗回 None。"""
    if _report["client"] is None:
        c = CDR.ApiClient()
        tok = c.login_hmi(CDR.USERNAME)
        if not tok:
            print("[REPORT] 登入失敗，無法進行報告相關作業")
            return None
        _report["client"] = c
    return _report["client"]


def _read_device_state(client):
    """一次唯讀取樣（供輪詢方向/狀態）。錯誤印出、不靜默；回傳 reading dict 或 None。"""
    try:
        return CDR.read_all(client)
    except Exception as e:
        print(f"[REPORT] 讀取設備狀態失敗：{e}")
        return None


def _actual_direction(r):
    """回傳 (direction, fault)：direction ∈ charge/discharge/idle/unknown；fault=PCS 是否故障。"""
    if not isinstance(r, dict):
        return "unknown", False
    d, _src = CDR.resolve_direction(r.get("pcs_charging_flag"),
                                    r.get("pcs_discharging_flag"),
                                    r.get("actual_active_power_kw"))
    fault = "故障" in str(r.get("pcs_status", ""))
    return d, fault


def _poll_state(client, want):
    """輪詢設備直到 want(direction, fault) 為真或逾時。回傳 (成功?, 最後 reading)。"""
    start = time.monotonic()
    last = None
    while time.monotonic() - start < AUTO_POLL_TIMEOUT_SEC:
        last = _read_device_state(client)
        d, f = _actual_direction(last)
        if want(d, f):
            return True, last
        time.sleep(AUTO_POLL_INTERVAL_SEC)
    return False, last


def _report_finalize(end_reason):
    """
    idempotent finalize：整個 session 只會產生一次報告（避免背景自動結束與手動 17 併發重複產檔）。
    回傳 (sess, stats)；若已結束/無 session → (None, None)。finalize 僅讀取狀態與寫本地檔，不送任何控制。
    """
    with _report["lock"]:
        sess = _report["session"]
        if sess is None or _report["finalized"]:
            return None, None
        _report["finalized"] = True
    try:
        return sess, sess.finalize(end_reason)
    except Exception as e:
        print(f"[錯誤] 產生報告失敗：{e}")
        return sess, None


def report_start():
    """Menu 16：開始/續接充放電報告（背景記錄；方向由 PCS 旗標自動判定）。
    若磁碟上有未完成 Session（recording/paused）→ 續接同一資料夾累積；否則建立新 Session。"""
    if not _REPORT_AVAILABLE:
        print(f"[錯誤] 無法載入報告模組：{_REPORT_IMPORT_ERR}")
        return
    if _report["session"] is not None:
        print("已有進行中的充放電報告，請先選 17 停止並產生報告。")
        return

    active = CDR.find_active_session(_report_output_root())
    client = _report_client()
    if client is None:
        return

    if active:
        # 續接既有未完成 Session（不建新資料夾、從既有累積值續加）
        try:
            sess = CDR.ReportSession.resume(active, client)
            sess.start()
        except Exception as e:
            print(f"[REPORT] 續接既有 Session 失敗：{e}")
            return
        print("\n【充放電報告】續接既有 Session（recording_resume）")
    else:
        # 建立新 Session（僅新建時詢問控制方式/設定值）
        try:
            mode = input("控制方式（交流有功/直流恆流/直流恆功率，Enter=交流有功）：").strip() or "交流有功"
        except (EOFError, KeyboardInterrupt):
            mode = "交流有功"
        try:
            sp_raw = input("設定值（選填，Enter 略過）：").strip()
        except (EOFError, KeyboardInterrupt):
            sp_raw = ""
        setpoint = None
        if sp_raw:
            try:
                setpoint = float(sp_raw)
            except ValueError:
                print("設定值格式錯誤，改為不記錄設定值。")
        try:
            sess = CDR.ReportSession("auto", setpoint, mode, client, _report_output_root())
            sess.start()
        except Exception as e:
            print(f"[REPORT] 開始報告失敗：{e}")
            return
        print("\n【充放電報告】開始記錄（新 Session）")

    _report_bg_start(sess)
    print(f"  Session ID：{sess.session_id}")
    print(f"  資料夾：{sess.folder}")
    print(f"  取樣間隔：{CDR_CFG.SAMPLE_INTERVAL_SEC}s（背景記錄中）")


def _report_bg_start(sess):
    """
    啟動背景取樣執行緒：持續取樣（合併 Session）。
      - charge/idle/discharge 皆為同一 Session 內的有效狀態 → **idle 不結束 Session**
        （充↔放切換之間可能經過 idle，屬正常流程；結束一律由 Menu 7 或程式離開決定）。
      - 僅在「PCS 故障」或「連續通訊失敗」時才自動結束（安全）；正常停止由 Menu 7 觸發。
      - 方向於相鄰取樣間改變時，由 ReportSession.sample_once 記錄 charge/idle/discharge_start 事件。
    """
    stop = threading.Event()
    _report.update(session=sess, thread=None, stop=stop, finalized=False)

    def _loop():
        while not stop.is_set():
            r = None
            try:
                r, _critical = sess.sample_once()
            except Exception as e:
                print(f"[REPORT] 背景取樣例外：{e}")
            reason = None
            if r is not None and _actual_direction(r)[1]:      # PCS 故障
                reason = "fault_stop"
            if reason is None and "communication_error" in sess.stop_reasons:
                reason = "communication_error"
            if reason:
                s, stats = _report_finalize(reason)
                if s is not None:
                    print(f"\n[REPORT] 已自動結束並產生報告"
                          f"（{reason} / {CDR_CFG.END_REASON.get(reason, reason)}）")
                    if stats is not None:
                        _print_report_result(s, stats)
                _report.update(session=None, thread=None, stop=None, pending_end_reason=None)
                break
            stop.wait(CDR_CFG.SAMPLE_INTERVAL_SEC)

    th = threading.Thread(target=_loop, daemon=True)
    _report["thread"] = th
    th.start()


def _stop_and_finalize(reason, write_final=True):
    """停止背景執行緒 →（選擇性）寫最後一筆即時資料 → finalize（idempotent）。回傳 (sess, stats)。"""
    if _report["session"] is None:
        return None, None
    if _report["stop"] is not None:
        _report["stop"].set()
    if _report["thread"] is not None:
        _report["thread"].join(timeout=CDR_CFG.SAMPLE_INTERVAL_SEC + 10)
    sess = _report["session"]
    if write_final and sess is not None and not _report["finalized"]:
        try:
            sess.sample_once()               # 寫入最後一筆即時資料
        except Exception as e:
            print(f"[REPORT] 最後取樣失敗：{e}")
    res = _report_finalize(reason)
    _report.update(session=None, thread=None, stop=None, pending_end_reason=None)
    return res


def report_pause():
    """離開程式時：停止背景取樣但保留 Session 為 paused（下次可續接），不標記 completed。"""
    sess = _report["session"]
    if sess is None:
        return
    if _report["stop"] is not None:
        _report["stop"].set()
    if _report["thread"] is not None:
        _report["thread"].join(timeout=CDR_CFG.SAMPLE_INTERVAL_SEC + 10)
    if _report["finalized"]:                  # 背景已 finalize（completed）→ 不覆蓋
        _report.update(session=None, thread=None, stop=None, pending_end_reason=None)
        return
    try:
        sess.pause()
        print(f"充放電報告已暫停（下次可續接）：{sess.session_id}")
    except Exception as e:
        print(f"[REPORT] 暫停報告失敗：{e}")
    _report.update(session=None, thread=None, stop=None, pending_end_reason=None)


def report_stop(end_reason="user_stop"):
    """Menu 17（備用）：手動停止並 finalize（status=completed；end_reason 預設 user_stop）。"""
    if _report["session"] is None:
        print("目前沒有進行中的充放電報告（請先選 16 開始，或由控制流程自動建立）。")
        return
    print("正在停止並產生報告…")
    sess, stats = _stop_and_finalize(end_reason)
    if sess is None:
        print("報告已結束（可能已自動結束）。")
    elif stats is not None:
        print("[REPORT] 已手動結束並產生報告")
        _print_report_result(sess, stats)


# ======================================================================
# 自動跟隨控制流程（Menu 4/5/6/7 成功後自動開始/結束報告）
# ======================================================================
def _control_succeeded():
    """讀 device_control_action_result.json 判斷最近一次控制是否成功。"""
    data = _load_json(ACTION_RESULT) or {}
    return bool(data.get("control_success"))


def report_on_charge_discharge_control(direction, mode_label, setpoint=None):
    """
    Menu 4/5/6 充/放電控制成功後：輪詢實際 PCS 狀態，確認進入該方向。
    合併 Session 模型：
      - 無 active Session → 建立單一「{timestamp}_auto」Session（不分充/放電資料夾）。
      - 已有 active Session（充↔放切換）→ **沿用同一 Session**，只記錄 direction_change 事件，
        不建新資料夾、不結束 Session；samples.csv 持續累加，方向依實際寫入。
      - 未偵測到實際充/放電 → 明確提示、不動作。
    """
    if not _REPORT_AVAILABLE:
        print(f"[REPORT] 報告模組未載入：{_REPORT_IMPORT_ERR}")
        return
    client = _report_client()
    if client is None:
        return
    zh = "充電" if direction == "charge" else "放電"
    print(f"[REPORT] 偵測實際{zh}狀態中…（最長 {AUTO_POLL_TIMEOUT_SEC}s）")
    ok, _r = _poll_state(client, lambda d, f: d == direction and not f)
    if not ok:
        print("[REPORT] 尚未偵測到實際充/放電狀態")
        return
    cur = _report["session"]
    if cur is not None:
        # 沿用現有合併 Session，僅記錄方向切換（不結束、不建新資料夾）
        try:
            cur.log_event("direction_change", "info", f"方向切換為 {direction}（{zh}）")
        except Exception as e:
            print(f"[REPORT] 記錄方向切換失敗：{e}")
        print(f"[REPORT] 沿用現有 Session，方向切換為 {direction}")
        return
    # 建立單一合併 Session（action='auto' → 資料夾為 {timestamp}_auto）
    try:
        sess = CDR.ReportSession("auto", setpoint, mode_label, client, _report_output_root())
        sess.start()
    except Exception as e:
        print(f"[REPORT] 自動開始失敗：{e}")
        return
    _report_bg_start(sess)
    print("[REPORT] 已自動建立充放電 Session")
    print(f"  Session ID：{sess.session_id}｜初始方向：{direction}｜控制方式：{mode_label}")


def report_on_stop_control():
    """Menu 7 停止充放電成功後：輪詢回到停止/待機 → 寫最後一筆 → 自動結束（control_stop；故障則 fault_stop）。"""
    if not _REPORT_AVAILABLE:
        print(f"[REPORT] 報告模組未載入：{_REPORT_IMPORT_ERR}")
        return
    if _report["session"] is None:
        print("[REPORT] 無進行中的報告 Session（略過自動結束）")
        return
    _report["pending_end_reason"] = "control_stop"
    reason = "control_stop"
    client = _report_client()
    if client is not None:
        print(f"[REPORT] 確認 PCS 回到停止/待機中…（最長 {AUTO_POLL_TIMEOUT_SEC}s）")
        ok, r = _poll_state(client, lambda d, f: d == "idle" or f)
        if r is not None and _actual_direction(r)[1]:
            reason = "fault_stop"
    try:
        sess, stats = _stop_and_finalize(reason)
        if sess is not None and stats is not None:
            print("[REPORT] 已結束合併充放電報告")
            _print_report_result(sess, stats)
        elif sess is None:
            print("[REPORT] 報告已結束（可能已自動結束）")
    except Exception as e:
        print(f"[REPORT] 自動結束失敗：{e}")


def report_resume_on_launch():
    """程式啟動時：若磁碟有未完成 Session（recording/paused）→ 恢復並繼續背景取樣（不重複建立）。"""
    if not _REPORT_AVAILABLE:
        return
    active = CDR.find_active_session(_report_output_root())
    if not active:
        return
    client = _report_client()
    if client is None:
        return
    try:
        sess = CDR.ReportSession.resume(active, client)
        sess.start()
    except Exception as e:
        print(f"[REPORT] 恢復未完成 Session 失敗：{e}")
        return
    _report_bg_start(sess)
    print(f"[REPORT] 已恢復未完成 Session：{sess.session_id}（方向 {sess.action}，背景繼續記錄）")


def report_status_line():
    """主選單顯示用：進行中報告的一行摘要；無則回 None。"""
    sess = _report["session"]
    if sess is None:
        return None
    last = sess.samples[-1] if sess.samples else {}
    return ("【充放電報告｜記錄中】"
            f"{sess.session_id}｜經過 {sess._elapsed():.0f}s｜"
            f"方向 {last.get('charge_discharge_direction', '-')}｜"
            f"SOC {last.get('soc_percent', '-')}%｜"
            f"P {last.get('actual_active_power_kw', '-')}kW｜"
            f"累積 {last.get('cumulative_energy_kwh', '-')}kWh｜"
            f"告警 {last.get('alarm_count', '-')}")


def report_view_latest():
    """
    Menu 18：查看最近一次『已完成』報告。
    規則：優先 status=completed（最新）；若最新為 recording/paused → 提示尚未完成；
          皆無則明確顯示「尚無已完成報告」。任何情況都不會無聲 return。
    """
    print("[REPORT] 執行查看最近報告")
    if not _REPORT_AVAILABLE:
        print(f"[錯誤] 無法載入報告模組：{_REPORT_IMPORT_ERR}")
        return
    root = _report_output_root()            # 由程式檔位置建立的絕對路徑，不依賴目前工作目錄
    print(f"[REPORT] 搜尋路徑：{root}")
    try:
        if not os.path.isdir(root):
            print("尚無已完成報告（輸出資料夾尚未建立）。")
            return
        sessions = []
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            state = _load_json(os.path.join(folder, CDR_CFG.FILE_SESSION_STATE)) or {}
            sessions.append({
                "folder": folder,
                "mtime": os.path.getmtime(folder),
                "status": state.get("status"),
                "has_summary": os.path.exists(os.path.join(folder, CDR_CFG.FILE_SUMMARY)),
            })
        if not sessions:
            print("尚無任何充放電報告。")
            return
        sessions.sort(key=lambda s: s["mtime"], reverse=True)     # 由新到舊

        # 1) 優先：最近一個 status=completed 且有 summary.json
        completed = [s for s in sessions
                     if s["status"] == CDR_CFG.SESSION_COMPLETED and s["has_summary"]]
        if completed:
            _show_report_folder(completed[0]["folder"])
            return
        # 2) 無 completed：若最新為 recording/paused → 明確提示尚未完成
        if sessions[0]["status"] in (CDR_CFG.SESSION_RECORDING, CDR_CFG.SESSION_PAUSED):
            print("目前報告尚未完成，請先選 17 停止並產生報告。")
            return
        # 3) 舊版無 session_state 但有 summary → 顯示最近一個含 summary 的報告
        with_summary = [s for s in sessions if s["has_summary"]]
        if with_summary:
            print("（找不到 status=completed 的 Session，顯示最近一個含 summary 的報告）")
            _show_report_folder(with_summary[0]["folder"])
            return
        print("尚無已完成報告。")
    except Exception as e:
        print(f"[錯誤] 查看最近報告失敗：{e}")


def _show_report_folder(folder):
    data = _load_json(os.path.join(folder, CDR_CFG.FILE_SUMMARY))
    if not data:
        print(f"[錯誤] 無法讀取 summary.json：{folder}")
        return
    _print_summary_json(data, folder)


def _print_report_result(sess, stats):
    data = _load_json(os.path.join(sess.folder, CDR_CFG.FILE_SUMMARY))
    if data:
        _print_summary_json(data, sess.folder)
    else:
        print(f"報告已產生於：{sess.folder}（無法讀回 summary.json）")


def _print_summary_json(data, path):
    sess = data.get("session", {}) or {}
    st = data.get("statistics", {}) or {}
    val = data.get("validation", {}) or {}
    # 簡易結果指標（依既有 summary 欄位；正式 PASS/FAIL 自動判定屬 V2，本處不臆造）
    result = "PASS" if (val.get("data_complete") and not val.get("stop_recommended")) else "REVIEW"
    print("\n============ 最近充放電報告 ============")
    print(f"Session ID     : {sess.get('session_id')}")
    print(f"方向(Direction) : {sess.get('action')}")
    print(f"控制方式        : {sess.get('control_mode')}")
    print(f"開始時間       : {sess.get('start_time')}")
    print(f"結束時間       : {sess.get('end_time')}")
    print(f"總時長(秒)     : {sess.get('duration_seconds')}")
    print(f"SOC            : {st.get('start_soc_percent')} → {st.get('end_soc_percent')} "
          f"（最高 {st.get('max_soc_percent')} / 最低 {st.get('min_soc_percent')}）")
    print(f"最大充/放電功率 : {st.get('max_charge_power_kw')} / {st.get('max_discharge_power_kw')} kW")
    print(f"累積充/放電    : {st.get('charged_energy_kwh')} / {st.get('discharged_energy_kwh')} kWh")
    print(f"Net Energy     : {st.get('net_energy_kwh')} kWh")
    print(f"往返效率       : {st.get('round_trip_efficiency_percent')}")
    print(f"告警數(新增)   : {st.get('alarm_count')}（追蹤 {st.get('alarm_total_tracked')}）")
    print(f"結束原因       : {sess.get('end_reason')}（{sess.get('end_reason_text')}）")
    print(f"資料完整/建議停止: {val.get('data_complete')} / {val.get('stop_recommended')} "
          f"{val.get('stop_reasons') or ''}")
    print(f"結果           : {result}")
    print(f"資料夾         : {path}")


def handle_choice(choice):
    """回傳 True 繼續、False 離開。"""
    if choice == "0":
        print("結束程式。")
        return False

    # 充放電報告（Menu 16/17/18）：獨立唯讀報告模組，不送任何控制
    if choice == "16":
        report_start()
        return True
    if choice == "17":
        report_stop()
        return True
    if choice == "18":
        report_view_latest()
        return True

    # 一般控制
    if choice in MENU_ACTIONS:
        action, label = MENU_ACTIONS[choice]
        _execute_live([OPERATOR, "--action", action, "--execute"], action, label)
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
        _execute_live([OPERATOR, "--action", action, "--execute", "--yes"], action, label)
        return True

    # PCS 功率控制（6 交流有功 / 7 直流恆流 / 8 直流恆功率）→ 進入第二層
    if choice in PCS_MODE_MENU:
        _pcs_mode_submenu(PCS_MODE_MENU[choice])
        return True

    # PCS 停止充放電（安全停止；維持可執行）
    if choice == PCS_STOP_CHOICE:
        rc = _run_script([OPERATOR, "--action", "pcs_stop_power", "--execute"], "PCS 停止充放電")
        show_action_result()
        # 控制成功 → 自動跟隨：輪詢回停止/待機並自動結束報告（唯讀，不送任何控制）
        if _REPORT_AVAILABLE and _control_succeeded():
            report_on_stop_control()
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
        sl = report_status_line() if _REPORT_AVAILABLE else None
        if sl:
            print(sl)
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
    report_resume_on_launch()      # 啟動時恢復未完成的充放電報告 Session（若有）
    try:
        while True:
            status = refresh_status()
            render_dashboard(status)
            sl = report_status_line() if _REPORT_AVAILABLE else None
            if sl:
                print(f"\n{sl}")
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
            elif choice in ("16", "17", "18"):
                # 便利：儀表板層直接輸入 16/17/18 也可操作充放電報告（等同進控制選單後選同項）
                try:
                    handle_choice(choice)
                except Exception as e:
                    print(f"[錯誤] 執行報告選項 {choice} 時發生例外：{e}")
            elif choice in ("r", ""):
                pass                       # 重新整理
            else:
                print(f"（'{choice}' 非本層選項；請按 e 進入控制執行選單，或直接輸入 16/17/18 操作報告）")
    finally:
        # 離開程式時，若仍有進行中的報告 → 暫停（paused，保留可續接），不強制 completed。
        if _REPORT_AVAILABLE and _report.get("session") is not None:
            print("\n偵測到進行中的充放電報告，離開前自動暫停（下次選 16 可續接）…")
            report_pause()


if __name__ == "__main__":
    main()
