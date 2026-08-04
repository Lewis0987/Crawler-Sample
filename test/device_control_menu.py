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

import io
import os
import sys
import json
import time
import contextlib
import threading
import subprocess
import unicodedata

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
    "12": ("ac_on", "空調開"),
    "13": ("ac_off", "空調關"),
    "14": ("vent_on", "進排風開"),
    "15": ("vent_off", "進排風關"),
    "16": ("cooling_on", "冷卻循環開"),
    "17": ("cooling_off", "冷卻循環關"),
}
# 高風險控制（需 YES 二次確認，並帶 --yes）
HIGH_RISK_ACTIONS = {
    "10": ("battery_power_on", "電池上電"),
    "11": ("battery_power_off", "電池下電"),
}
# PCS 排程：主開關（開/關）控制 = 8；排程配置（唯讀查看）= 9
SCHEDULE_SWITCH_CHOICE = "8"
SCHEDULE_VIEW_CHOICE = "9"
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

# 唯讀查詢子程序（device_control_scraper.py）的硬性逾時（秒）——父程序最後防線。
# 子程序內每支 API 亦各自逾時（見 api_client.REQUEST_TIMEOUT=(5,15)）；父程序不得無限等待。
SUBPROCESS_TIMEOUT_SEC = 60


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

# ======================================================================
# 設備控制選單（卡片式）——分類與項目定義（資料驅動；新增分類/功能沿用同樣式）
# ⚠️ 編號需與 handle_choice 的 dispatch 對應一致（1 查詢 / 2-9 PCS / 10-11 電池 /
#    12-13 空調 / 14-15 進排風 / 16-17 冷卻循環 / 18-20 報告 / 0 離開）。
# 卡片標題已標明分類，項目文字不重覆分類字（如 PCS 卡內不再冠「PCS」）。
# ======================================================================
MENU_SECTIONS = [
    ("系統", [("1", "查詢目前設備狀態"), ("0", "離開")]),
    ("PCS", [
        ("2", "手動模式開"),
        ("3", "手動模式關"),
        ("4", "交流有功控制（充/放電）"),
        ("5", "直流恆流控制（充/放電）"),
        ("6", "直流恆功率控制（充/放電）"),
        ("7", "停止充放電"),
        ("8", "排程主開關（開/關）"),
        ("9", "排程配置（查看）"),
    ]),
    ("電池", [("10", "電池上電"), ("11", "電池下電")]),
    ("空調", [("12", "空調開"), ("13", "空調關")]),
    ("進排風", [("14", "進排風開"), ("15", "進排風關")]),
    ("冷卻循環", [("16", "冷卻循環開"), ("17", "冷卻循環關")]),
    ("報告", [
        ("18", "手動開始合併充放電報告（備用）"),
        ("19", "手動結束合併充放電報告（備用）"),
        ("20", "查看最新報告"),
    ]),
]

# 卡片內容區顯示寬（不含左右各 1 空白與框線）；總寬 = CARD_INNER + 4 ≈ 50 字元。
CARD_INNER = 46


def _disp_width(s):
    """字串終端顯示寬度：東亞全形/寬字元(F/W)算 2，其餘(含框線 A/Na)算 1。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in str(s))


def _pad_display(s, width):
    """左對齊，右補空白到顯示寬度 width（中文對齊用）。"""
    return s + " " * max(0, width - _disp_width(s))


def _render_card(title, items, inner=CARD_INNER):
    """
    產生單一卡片（固定寬、置中標題、項目左對齊、左右各留一空白）。
    可重用：未來新增 EMS / BMS / 閥值管理 / 告警管理等分類，直接沿用本函式。
    """
    bar = "─" * (inner + 2)
    lines = [f"┌{bar}┐"]
    # 置中標題
    tw = _disp_width(title)
    left = max(0, (inner - tw) // 2)
    right = max(0, inner - tw - left)
    lines.append(f"│ {' ' * left}{title}{' ' * right} │")
    lines.append(f"├{bar}┤")
    for num, label in items:
        lines.append(f"│ {_pad_display(f'{num}. {label}', inner)} │")
    lines.append(f"└{bar}┘")
    return "\n".join(lines)


def _build_menu_text():
    """組出整份卡片式選單（各分類之間空一行，提高可讀性）。"""
    cards = [_render_card(title, items) for title, items in MENU_SECTIONS]
    return "\n" + "\n\n".join(cards) + "\n"


MENU_TEXT = _build_menu_text()


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
    """靜默執行唯讀 scraper 刷新狀態，回傳 status_summary（不印任何東西）。含硬性逾時，逾時即放棄。"""
    try:
        subprocess.run(
            [sys.executable, "-X", "utf8", SCRAPER],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {}                       # 逾時：subprocess.run 已終止子程序，直接放棄
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
           "pending_end_reason": None, "client": None,
           "stopping": False}      # 停止流程進行中（防重複 Stop；期間一律不得 Start）

# ---- Process-level 鎖（Phase 2）----
# _SESSION_LOCK：保護「Session 的建立與停止」。手動控制（Menu 4/5/6/7）與智慧排程監看層
#   共用同一份 _report 狀態，兩邊都必須經此鎖，確保同一時間只會有一份 Report Session。
#   為 RLock：建立流程內部可能再次進入需要同鎖的函式（如 _report_start_session → 記錄事件）。
# _CLIENT_LOCK：保護共用 ApiClient 的使用（requests.Session 非執行緒安全）。
#   背景取樣執行緒與前景監看/輪詢都會用同一個 client，所有 GET 一律包在此鎖內。
#   ⚠️ 控制命令（operator）是**獨立子程序、自己登入**，不共用本 client；且自動刷新僅在
#      Dashboard 前景迴圈執行（無背景 thread），進入控制選單期間不會刷新 → 控制與刷新不會並行。
_SESSION_LOCK = threading.RLock()
_CLIENT_LOCK = threading.RLock()

# 自動跟隨控制流程的輪詢設定（純 GET 唯讀）
AUTO_POLL_TIMEOUT_SEC = 90      # 送控制後最長等待實際狀態改變（畫面訊息一律動態引用此值）
AUTO_POLL_INTERVAL_SEC = 3


def _report_output_root():
    return os.path.join(OUTPUT_DIR, CDR_CFG.OUTPUT_SUBDIR)


def _report_client(quiet=False):
    """
    共用已登入的 ApiClient（**整個程式生命週期只登入一次**；輪詢、報告 Session、監看層共用）。
    登入成功後快取於 _report["client"]，之後每次呼叫直接回傳快取，不會重新登入。
    quiet=True：登入失敗時不印訊息（供監看層的靜默重試使用，避免每次重試都洗畫面）。
    失敗回 None。
    """
    if _report["client"] is None:
        c = CDR.ApiClient()
        tok = c.login_hmi(CDR.USERNAME)
        if not tok:
            if not quiet:
                print("[REPORT] 登入失敗，無法進行報告相關作業")
            return None
        _report["client"] = c
    return _report["client"]


def _read_device_state(client):
    """一次唯讀取樣（供輪詢方向/狀態）。錯誤印出、不靜默；回傳 reading dict 或 None。
    ⚠️ read_all 只做資料讀取；**不得**在此加入任何建立/停止報告的副作用（見 auto_schedule_check）。"""
    try:
        with _CLIENT_LOCK:                       # 共用 client：與背景取樣互斥
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
    # 故障判斷統一委派 CDR.pcs_is_fault()：優先讀 pcs_fault_flag（systemFaultStatus.oldValue，
    # 語言無關）；旗標缺失才退回中文 badge 比對。不在此檔另做字串判斷，避免兩份邏輯不一致。
    fault = CDR.pcs_is_fault(r)
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
        with _CLIENT_LOCK:                       # finalize 會做唯讀 GET（結束狀態/Cell 快照/告警）
            return sess, sess.finalize(end_reason)
    except Exception as e:
        print(f"[錯誤] 產生報告失敗：{e}")
        return sess, None


def report_start():
    """Menu 18：開始/續接充放電報告（背景記錄；方向由 PCS 旗標自動判定）。
    若磁碟上有未完成 Session（recording/paused）→ 續接同一資料夾累積；否則建立新 Session。"""
    if not _REPORT_AVAILABLE:
        print(f"[錯誤] 無法載入報告模組：{_REPORT_IMPORT_ERR}")
        return
    # 與智慧排程監看層共用同一把 _SESSION_LOCK 與同一份 _report 狀態 → Session 全程唯一
    with _SESSION_LOCK:
        _report_start_locked()


def _report_start_locked():
    """report_start() 的實際內容（呼叫端必須已持有 _SESSION_LOCK）。"""
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
            with _CLIENT_LOCK:                       # start() 會做唯讀 GET
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
            with _CLIENT_LOCK:                       # start() 會做唯讀 GET
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
                with _CLIENT_LOCK:               # 共用 client：與前景監看/輪詢互斥
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
    """
    停止背景執行緒 →（選擇性）寫最後一筆即時資料 → finalize（idempotent）。回傳 (sess, stats)。
    **鎖範圍最小化**（見檔頭鎖說明）：
      - _SESSION_LOCK 只包「檢查 Session／設 stopping 旗標／取出並清除參考」等瞬時狀態切換。
      - thread.join（最長 SAMPLE_INTERVAL+10 秒的等待）與 finalize（含 Excel 產出）
        **一律不持有 _SESSION_LOCK**，避免長時間鎖住 Dashboard。
      - stopping 旗標＋_report["session"] 仍非 None → 期間任何 Start 都會被擋掉（雙重保護）。
    """
    with _SESSION_LOCK:                          # ① 瞬時：檢查 + 旗標 + 取參考
        if _report["session"] is None or _report["stopping"]:
            return None, None
        _report["stopping"] = True
        sess = _report["session"]
        stop_ev, th = _report["stop"], _report["thread"]
        _auto["state"] = AUTO_STOP_PENDING
    try:                                         # ② 長時間：等待與產出，**不持鎖**
        if stop_ev is not None:
            stop_ev.set()
        if th is not None:
            th.join(timeout=CDR_CFG.SAMPLE_INTERVAL_SEC + 10)
        if write_final and sess is not None and not _report["finalized"]:
            try:
                with _CLIENT_LOCK:               # 只包一次取樣
                    sess.sample_once()           # 寫入最後一筆即時資料
            except Exception as e:
                print(f"[REPORT] 最後取樣失敗：{e}")
        res = _report_finalize(reason)
    finally:                                     # ③ 瞬時：清除參考與狀態
        with _SESSION_LOCK:
            _report.update(session=None, thread=None, stop=None,
                           pending_end_reason=None, stopping=False)
            _auto["state"] = AUTO_IDLE
            _auto_reset_tracking()
    return res


def report_pause():
    """
    離開程式時：停止背景取樣但保留 Session 為 paused（下次可續接），不標記 completed。
    鎖範圍同 _stop_and_finalize：_SESSION_LOCK 只包狀態切換，**join 等待不持鎖**。
    """
    with _SESSION_LOCK:                          # ① 瞬時
        sess = _report["session"]
        if sess is None or _report["stopping"]:
            return
        _report["stopping"] = True
        stop_ev, th = _report["stop"], _report["thread"]
        _auto["state"] = AUTO_STOP_PENDING
    try:                                         # ② 等待與寫檔，不持鎖
        if stop_ev is not None:
            stop_ev.set()
        if th is not None:
            th.join(timeout=CDR_CFG.SAMPLE_INTERVAL_SEC + 10)
        if not _report["finalized"]:             # 背景已 finalize（completed）→ 不覆蓋
            try:
                sess.pause()
                print(f"充放電報告已暫停（下次可續接）：{sess.session_id}")
            except Exception as e:
                print(f"[REPORT] 暫停報告失敗：{e}")
    finally:                                     # ③ 瞬時
        with _SESSION_LOCK:
            _report.update(session=None, thread=None, stop=None,
                           pending_end_reason=None, stopping=False)
            _auto.update(state=AUTO_IDLE, last_note=None, last_status=None)
            _auto_reset_tracking()


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


def _report_start_session(direction, mode_label, setpoint=None, origin="manual", extra_events=()):
    """
    **建立/沿用 Report Session 的唯一入口**（手動控制與智慧排程監看層共用，不存在第二套報告邏輯）。
    全程持有 _SESSION_LOCK，保證同一時間只會有一份 Session：
      - 已有 active Session → **不建新資料夾**，僅記錄 direction_change 事件後回傳該 Session。
      - 無 Session → 建立單一合併 Session（action='auto' → 資料夾 {timestamp}_auto）並啟動背景取樣。
    origin：manual（Menu 4/5/6）/ scheduler（智慧排程自動）—— 僅寫入事件，不影響流程。
    extra_events：[(event_type, severity, detail), ...] 建立成功後補記的事件（如 auto_charge_start）。
    回傳 (sess, created)：created=True 表示本次新建；False=沿用既有。失敗回 (None, False)。
    """
    zh = {"charge": "充電", "discharge": "放電"}.get(direction, direction)
    with _SESSION_LOCK:
        cur = _report["session"]
        if cur is not None:
            # 沿用現有合併 Session，僅記錄方向切換（不結束、不建新資料夾）
            try:
                cur.log_event("direction_change", "info",
                              f"方向切換為 {direction}（{zh}）｜來源 {origin}")
            except Exception as e:
                print(f"[REPORT] 記錄方向切換失敗：{e}")
            print(f"[REPORT] 沿用現有 Session，方向切換為 {direction}")
            return cur, False

        client = _report_client()
        if client is None:
            return None, False
        try:
            with _CLIENT_LOCK:                   # start() 會做唯讀 GET（起始狀態/Cell 快照）
                sess = CDR.ReportSession("auto", setpoint, mode_label, client,
                                         _report_output_root())
                sess.start()
        except Exception as e:
            print(f"[REPORT] 建立 Session 失敗（{origin}）：{e}")
            return None, False
        _report_bg_start(sess)                   # 設定 _report["session"] 並啟動背景取樣
        for ev in extra_events:
            try:
                sess.log_event(*ev)
            except Exception as e:
                print(f"[REPORT] 記錄事件失敗（{ev[0] if ev else '?'}）：{e}")
        print(f"[REPORT] 已建立充放電 Session（來源 {origin}）")
        print(f"  Session ID：{sess.session_id}｜初始方向：{direction}｜控制方式：{mode_label}")
        return sess, True


def report_on_charge_discharge_control(direction, mode_label, setpoint=None):
    """
    Menu 4/5/6 充/放電控制成功後：輪詢實際 PCS 狀態，確認進入該方向。
    合併 Session 模型：
      - 無 active Session → 建立單一「{timestamp}_auto」Session（不分充/放電資料夾）。
      - 已有 active Session（充↔放切換）→ **沿用同一 Session**，只記錄 direction_change 事件，
        不建新資料夾、不結束 Session；samples.csv 持續累加，方向依實際寫入。
      - 未偵測到實際充/放電 → 明確提示、不動作。
    ※ 實際建立/沿用一律委派 _report_start_session()（與智慧排程共用同一入口）。
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
    _report_start_session(direction, mode_label, setpoint, origin="manual")


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


# ======================================================================
# 智慧排程自動建立報告 —— 監看決策層（Phase 2：基本自動建立）
# ----------------------------------------------------------------------
# 三層嚴格分離，不得混用：
#   ① 資料讀取層  CDR.read_all(client)         只取狀態，**零副作用**
#   ② 監看決策層  auto_schedule_check(reading) 只判斷要不要開始/停止（本節）
#   ③ 報告執行層  _report_start_session(...)   沿用既有 ReportSession，不存在第二套邏輯
#
# ⚠️ 絕對不可把本節任何函式塞進 read_all()：selftest、_arm_for_charge_discharge()、
#    單純狀態查詢等都會呼叫 read_all()，一旦有副作用就會誤觸發自動報告。
#    呼叫端必須自己明確呼叫：reading = CDR.read_all(c) → auto_schedule_check(reading)
# ⚠️ 本節不建立任何背景 thread：偵測完全跑在 Dashboard 前景迴圈（離開迴圈即停止 GET）。
# ======================================================================
AUTO_REFRESH_SEC = 15            # Dashboard 前景自動刷新間隔（秒）
AUTO_LOGIN_RETRY_SEC = 60        # 登入失敗後的靜默重試間隔（秒）；按 r 可立即重試
# ↓ Phase 3 用的 debounce 門檻：資料結構先以「時間（monotonic 秒）」設計，Phase 2 只記錄不強制。
AUTO_HOLD_START_SEC = 30         # charge/discharge 需持續多久才建立報告（Phase 3 啟用）
AUTO_HOLD_STOP_SEC = 30          # idle 需持續多久才停止報告（Phase 3 啟用）

# 監看狀態機：IDLE → START_PENDING → RUNNING → STOP_PENDING → IDLE
# （Phase 2 只用到 IDLE / START_PENDING / RUNNING；STOP_PENDING 保留給 Phase 3 的停止 debounce）
AUTO_IDLE, AUTO_START_PENDING, AUTO_RUNNING, AUTO_STOP_PENDING = (
    "IDLE", "START_PENDING", "RUNNING", "STOP_PENDING")

_auto = {
    "state": AUTO_IDLE,
    "direction": None,          # 目前觀測到的方向（charge/discharge/idle/unknown）
    "since": None,              # **monotonic 秒**：該方向首次被觀測的時間點
    "held_sec": 0.0,            # 該方向已持續秒數（Phase 3 的觸發依據）
    "streak": 0,                # 連續同方向次數（僅輔助資訊，不作觸發依據）
    "template_status": "unknown",   # enabled / none / unknown（僅資訊，**不阻擋**建立）
    "last_note": None,          # 上次印出的判斷訊息 → 僅在改變時才印
    "last_status": None,        # 上次印出的狀態摘要 → 僅在改變時才印
    "disabled": False,          # 監看暫停（登入失敗）；**可恢復**，非永久停用
    "login_failed_at": None,    # monotonic：上次登入失敗時間（用於 60s 靜默重試節流）
    # ↓ 畫面輸出用的暫存旗標（非監看狀態；集中宣告於此，避免散落成動態新增的 key）
    "_pending_nl": False,       # 游標仍停在提示行末端 → 下一行輸出前需補一次換行
    "_no_tty_warned": False,    # 非互動 stdin 的提示只顯示一次
}
# 系統時間（datetime.now）可能被使用者/NTP 調整 → 一律用 time.monotonic 計算持續時間。
_MODE_LABEL_TO_CODE = {"智慧模式": "smart", "手動模式": "manual", "未開啟模式": "none"}


def _auto_reset_tracking():
    """清空方向追蹤（建立/停止 Session 後重新起算持續時間）。"""
    _auto.update(direction=None, since=None, held_sec=0.0, streak=0)


def _auto_pending_newline(on=True):
    """
    標記「游標目前停在提示行末端」。自動刷新逾時時不主動換行（否則每 15s 都會多一行空白），
    改由真正要輸出的第一行自己補換行 → 沒有輸出就完全不留痕跡。
    """
    _auto["_pending_nl"] = bool(on)


def _auto_print(text):
    """輸出一行；若游標仍停在提示行末端，先補一次換行（只補一次）。"""
    if _auto.get("_pending_nl"):
        print()
        _auto["_pending_nl"] = False
    print(text)


def _auto_note(text, key="last_note", with_time=False):
    """
    僅在訊息與上次不同時列印（避免每 15s 重複洗畫面）。回傳是否真的印出。
    with_time=True 時列印會加上 HH:MM:SS 前綴；比較仍以「不含時間」的內容為準，
    否則每輪時間都不同會導致每次都印。
    """
    if text == _auto[key]:
        return False
    _auto[key] = text
    _auto_print(f"[{time.strftime('%H:%M:%S')}] {text}" if with_time else text)
    return True


def _auto_track_direction(direction, now=None):
    """
    以 monotonic 時間累計「同一方向持續多久」。回傳 (held_sec, changed)。
    方向改變 → 重新起算（held=0）；相同 → held = now - since。
    不使用系統日期時間，避免電腦時間被調整後判斷錯誤。
    """
    now = time.monotonic() if now is None else now
    if direction != _auto["direction"]:
        _auto.update(direction=direction, since=now, held_sec=0.0, streak=1)
        return 0.0, True
    _auto["streak"] += 1
    held = max(0.0, now - _auto["since"]) if _auto["since"] is not None else 0.0
    _auto["held_sec"] = held
    return held, False


def _monitor_client(force=False):
    """
    取得監看用的共用 client，並處理「登入失敗 → 可恢復」：
      - 正常：直接回傳已快取的共用 client（**不會重新登入**）。
      - 首次失敗：印一次提示，暫停監看（disabled=True）並記錄失敗時間。
      - 暫停期間：每 AUTO_LOGIN_RETRY_SEC（60s）**靜默**重試一次（不洗畫面）；
                  force=True（使用者按 r）則立即重試，不受節流限制。
      - 恢復成功：印一次「自動監看已恢復」，回到正常 15 秒監看。
    回傳 (client 或 None, printed)。
    """
    if not _auto["disabled"]:
        client = _report_client()
        if client is not None:
            return client, False
        _auto.update(disabled=True, login_failed_at=time.monotonic())
        _auto_print(f"[AUTO] 登入失敗 → 暫停智慧排程自動偵測"
                    f"（每 {AUTO_LOGIN_RETRY_SEC}s 自動重試，或按 r 立即重試；"
                    f"設備控制選單不受影響）")
        return None, True

    # ---- 暫停中：節流重試（靜默；api_client 的登入訊息一併抑制，避免每 60s 洗畫面）----
    last = _auto["login_failed_at"]
    if not force and last is not None and (time.monotonic() - last) < AUTO_LOGIN_RETRY_SEC:
        return None, False
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            client = _report_client(quiet=True)
    except Exception:
        client = None
    if client is None:
        _auto["login_failed_at"] = time.monotonic()
        return None, False                       # 重試失敗 → 完全不輸出
    _auto.update(disabled=False, login_failed_at=None, last_note=None, last_status=None)
    _auto_print("[AUTO] 登入成功 → 自動監看已恢復"
                f"（每 {AUTO_REFRESH_SEC}s 更新 PCS／排程／充放電狀態）")
    return client, True


def _auto_control_mode_code(r):
    """PCS 控制模式機器語意：優先讀 pcs_control_mode_code；舊 reading 才退回中文標籤對照。"""
    code = r.get("pcs_control_mode_code")
    if code:
        return str(code)
    return _MODE_LABEL_TO_CODE.get(str(r.get("pcs_control_mode", "")), "unknown")


def _auto_template_status(client):
    """
    排程配置清單狀態（**僅供資訊，永不阻擋報告建立**）：
      enabled = 至少一筆 enableFlag==1 / none = 取得成功但無啟用 / unknown = API 取得失敗或格式非預期。
    """
    if client is None:
        return "unknown"
    try:
        with _CLIENT_LOCK:
            tpls = client.get(_SCHED_TPL_LIST)
    except Exception as e:
        _auto_print(f"[AUTO] 排程配置清單讀取失敗（不影響報告建立）：{e}")
        return "unknown"
    if not isinstance(tpls, list):
        return "unknown"
    return ("enabled" if any(_sched_template_enabled(t) for t in tpls if isinstance(t, dict))
            else "none")


def auto_schedule_check(r, client=None, source="auto"):
    """
    智慧排程自動建立報告（Phase 2 基本版）。由 Dashboard 刷新流程取得 reading 後**明確呼叫**。
    只做判斷與委派：不自行取樣、不自行登入、不複製報告邏輯。

    建立條件（須全部成立）：
      A. pcs_control_mode_code == "smart"      智慧模式（語言無關）
      B. pcs_schedule_enabled is True          排程主開關開
      C. 實際方向為 charge / discharge         ← systemCharging/DischargingStatus.oldValue
      D. _report["session"] is None            目前沒有任何 Report Session（手動建立的也算）
      E. PCS 非故障                            ← CDR.pcs_is_fault（pcs_fault_flag）
      F. 狀態機不在 START_PENDING / STOP_PENDING

    排程配置清單（template list）僅記錄 enabled/none/unknown，**取得失敗也照建報告**。
    Phase 2 不做 debounce（持續時間只記錄不判斷），也不自動停止報告（沿用既有 control_stop /
    fault_stop / communication_error），停止 debounce 留待 Phase 3。

    回傳 (action, reason)：action ∈ started / skipped / noop。
    """
    if not _REPORT_AVAILABLE:
        return "noop", "report_module_unavailable"
    if _auto["disabled"]:
        return "noop", "monitor_disabled"
    if not isinstance(r, dict):
        _auto_note("[AUTO] 本輪取樣失敗 → 略過自動判斷（不建立、不停止）")
        return "noop", "no_reading"

    direction, fault = _actual_direction(r)
    held, _changed = _auto_track_direction(direction)
    mode_code = _auto_control_mode_code(r)
    sched_on = r.get("pcs_schedule_enabled") is True

    with _SESSION_LOCK:
        # ---- 狀態機 ↔ 實際 Session 同步（手動 Menu 建立/停止的 Session 也要反映進來）----
        if _report["session"] is None:
            if _auto["state"] == AUTO_RUNNING:
                _auto["state"] = AUTO_IDLE          # 報告已由控制流程/背景自動結束
        elif _auto["state"] in (AUTO_IDLE, AUTO_START_PENDING):
            _auto["state"] = AUTO_RUNNING           # 手動流程建立的 Session：共用同一狀態

        st = _auto["state"]
        # F：Start/Stop 進行中 → 本輪不處理（防止同一輪、或手動＋自動雙來源重複觸發）
        if _report["stopping"]:
            return "skipped", "state=STOP_PENDING"
        if st in (AUTO_START_PENDING, AUTO_STOP_PENDING):
            return "skipped", f"state={st}"
        # D：已有 Session → 絕不建立第二份（方向切換由控制流程沿用同一 Session）
        if _report["session"] is not None:
            _auto_note(f"[AUTO] 已有進行中的報告 Session（{_report['session'].session_id}）"
                       f"→ 不建立新報告")
            return "skipped", "session_exists"
        # E：PCS 故障 → 不建立（故障停止沿用既有 SafetyMonitor / fault_stop）
        if fault:
            _auto_note("[AUTO] PCS 故障中（pcs_fault_flag）→ 不建立新報告")
            return "skipped", "pcs_fault"
        # A / B：非智慧模式或排程主開關未開 → 不自動建立
        if mode_code != "smart":
            _auto_note(f"[AUTO] 控制模式非智慧模式（{mode_code}）→ 不自動建立報告")
            return "skipped", f"control_mode={mode_code}"
        if not sched_on:
            _auto_note("[AUTO] 排程主開關未開啟 → 不自動建立報告")
            return "skipped", "schedule_off"
        # C：尚未實際充/放電（含 idle / unknown）→ 只等待，不建立
        if direction not in ("charge", "discharge"):
            _auto_note(f"[AUTO] 智慧模式＋排程已開，但設備方向為 {direction}"
                       f"（持續 {held:.0f}s）→ 等待實際充/放電")
            return "skipped", f"direction={direction}"

        # ---- A~F 全部成立 ----
        zh = "充電" if direction == "charge" else "放電"
        # 安全開關（config.AUTO_SCHEDULE_REPORT_ENABLED）：關閉時只判斷與記錄，不建立 Session。
        # **只擋自動建立**：手動報告（Menu 18/19）與手動控制後的自動跟隨完全不受影響。
        if not getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED", False):
            _auto_note(f"[AUTO] 條件全部成立（實際{zh}，持續 {held:.0f}s）"
                       f"但自動報告開關為關閉（AUTO_SCHEDULE_REPORT_ENABLED=False）"
                       f"→ 僅記錄，不建立報告")
            return "skipped", "auto_report_disabled"

        # 佔位（claim）：在鎖內把狀態切成 START_PENDING，之後任何呼叫都會在上面的 F 被擋掉。
        # 這一步就是唯一性的保證 → 因此接下來的網路等待可以安全地在鎖外執行。
        _auto["state"] = AUTO_START_PENDING

    # ---- 建立報告（**已離開 _SESSION_LOCK**：template GET 與 sess.start() 的網路等待不鎖住 Dashboard）
    sess = None
    try:
        _auto_print(f"[AUTO] 條件成立：智慧模式＋排程開啟＋實際{zh}"
                    f"（持續 {held:.0f}s／連續 {_auto['streak']} 次；來源 {source}）"
                    f"→ 建立充放電報告")
        _auto["template_status"] = _auto_template_status(client or _report["client"])
        print(f"[AUTO] 排程配置清單：{_auto['template_status']}（僅供資訊，不影響報告建立）")
        ev_type = "auto_charge_start" if direction == "charge" else "auto_discharge_start"
        sess, _created = _report_start_session(      # 內部自行取得 _SESSION_LOCK（RLock）
            direction,
            r.get("pcs_power_control_mode") or "交流有功",
            None,
            origin="scheduler",
            extra_events=[(ev_type, "info",
                           f"智慧排程自動開始{zh}｜方向持續 {held:.0f}s｜"
                           f"排程配置 {_auto['template_status']}｜刷新來源 {source}")],
        )
    finally:
        with _SESSION_LOCK:                          # 瞬時：離開 pending（成功或失敗都要）
            _auto["state"] = AUTO_RUNNING if _report["session"] is not None else AUTO_IDLE
    if sess is None:
        _auto_note("[AUTO] 建立報告失敗（詳見上方訊息）→ 下一輪重試")
        return "skipped", "start_failed"
    _auto_reset_tracking()
    _auto["last_note"] = None                        # 已建立 → 解除訊息抑制
    return "started", direction


def _monitor_status_line(r, source="auto"):
    """
    監看 Debug 一行（實機唯讀觀察階段用）。**不含時間戳**，才能做「僅在改變時列印」的比較。
    欄位固定：control_mode_code / schedule_enabled / charging_flag / discharging_flag /
              fault_flag / direction / auto_state / session / source / held_sec
    """
    if not isinstance(r, dict):
        return "[AUTO] 取樣失敗（本輪略過）"
    d, fault = _actual_direction(r)
    sess = _report["session"]
    return ("[AUTO] "
            f"mode={_auto_control_mode_code(r)}"
            f" sched={r.get('pcs_schedule_enabled')}"
            f" chg={r.get('pcs_charging_flag')}"
            f" dis={r.get('pcs_discharging_flag')}"
            f" fault={r.get('pcs_fault_flag')}({'是' if fault else '否'})"
            f" dir={d}"
            f" state={_auto['state']}"
            f" session={sess.session_id if sess is not None else 'None'}"
            f" src={source}"
            f" held={_auto['held_sec']:.0f}s"
            f"｜SOC {r.get('soc_percent')}% P {r.get('actual_active_power_kw')}kW")


def _monitor_footer():
    """
    Dashboard 底部說明：明確區分「每 15 秒自動更新」與「僅按 r 更新」的欄位，
    避免誤以為所有欄位都在自動更新。同時顯示自動報告安全開關的實際狀態。
    """
    armed = getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED", False) if CDR_CFG else False
    if _auto["disabled"]:
        state = f"暫停（登入失敗，每 {AUTO_LOGIN_RETRY_SEC}s 重試，或按 r 立即重試）"
    else:
        state = f"每 {AUTO_REFRESH_SEC} 秒更新"
    print(f"\n{_BLOCK_SEP}")
    print(f"自動監看：PCS／排程／充放電狀態 —— {state}")
    print(f"　　　　　自動建立報告：{'開啟' if armed else '關閉'}"
          f"（AUTO_SCHEDULE_REPORT_ENABLED={armed}）")
    print("完整設備狀態（空調／進排風／冷卻循環／電池上下電）：**僅在按 r 時更新**，"
          "目前顯示為最後一次手動刷新結果")


def dashboard_refresh(auto=False):
    """
    **Dashboard 唯一刷新入口**：手動按 r 與每 15s 自動刷新都走這裡（同一套流程、同一份判斷）。
      ① 取樣    ：CDR.read_all(共用已登入 client)     ← 不另外登入、不建立第二個 Client
      ② 更新畫面：auto=False 完整重畫（含唯讀 scraper 子程序：空調/進排風/冷卻循環等區塊）
                  auto=True  僅在狀態摘要改變時印一行（避免每 15s 洗畫面、避免重複登入子程序）
      ③ 判斷    ：auto_schedule_check(reading)        ← 監看決策層
    回傳 (reading, printed)：printed=True 表示本輪有輸出（呼叫端據此決定是否重印提示行）。
    """
    printed = False
    source = "auto" if auto else "manual"
    if not auto:
        status = refresh_status()                   # 既有唯讀子程序（顯示用；含逾時與清理）
        render_dashboard(status)                    # ← 空調/進排風/冷卻循環等只在這裡更新
        printed = True

    if not _REPORT_AVAILABLE:
        if not auto:
            _monitor_footer()
        return None, printed

    # 共用 client（整個程式只登入一次）；登入失敗可恢復：按 r 立即重試、自動每 60s 靜默重試
    client, login_printed = _monitor_client(force=not auto)
    printed = printed or login_printed
    if client is None:
        if not auto:
            _monitor_footer()
        return None, printed

    reading = _read_device_state(client)             # ← 純資料讀取（_CLIENT_LOCK 只包這一次取樣）
    debug_always = getattr(CDR_CFG, "AUTO_MONITOR_DEBUG", False)
    line = _monitor_status_line(reading, source)
    if debug_always:
        _auto_print(f"[{time.strftime('%H:%M:%S')}] {line}")     # 每輪都印（唯讀觀察階段）
        _auto["last_status"] = line
        printed = True
    elif _auto_note(line, key="last_status", with_time=True):
        printed = True                               # 僅在監看狀態改變時印

    act, _reason = auto_schedule_check(reading, client=client, source=source)
    if act == "started":
        printed = True
    if not auto:
        _monitor_footer()
    return reading, printed


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


# ---- PCS 排程（唯讀查看 + 主開關控制入口）----
_SCHED_MAIN = "/schedule/config/getScheduleSwitch"
_SCHED_TPL_LIST = "/schedule/template/list"
_SCHED_ITEM_LIST = "/schedule/list/{tid}"
_SCHED_CD = {1: "充電", 2: "放電", 3: "不充不放"}
_SCHED_PLAN = {1: "週", 2: "按日期"}
_SCHED_WEEK = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "日"}


def _sched_readonly_client():
    """建立已登入的 ApiClient（供排程唯讀查詢用）；失敗回 None。只做唯讀 GET。"""
    try:
        from api_client import ApiClient
    except Exception as e:
        print(f"[錯誤] 無法載入 ApiClient：{e}")
        return None
    c = ApiClient()
    if not c.login_hmi():
        print("[錯誤] 登入失敗，無法查詢排程。")
        return None
    return c


def _sched_switch_state(client):
    """讀取排程主開關狀態：回傳 (schedulePlanSwitch, manualModeSwitch) 各為 1/0/None。唯讀 GET。"""
    sw = client.get(_SCHED_MAIN)
    if not isinstance(sw, dict) or sw.get("_error"):
        return None, None, sw

    def _b(v):
        return 1 if v in (1, "1") else (0 if v in (0, "0") else None)
    return _b(sw.get("schedulePlanSwitch")), _b(sw.get("manualModeSwitch")), sw


def _menu_input(prompt):
    """子選單輸入；EOF/中斷視為返回（回 '0'）。"""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "0"


def _sched_template_enabled(t):
    """模板是否啟用（enableFlag==1）。"""
    return t.get("enableFlag") in (1, "1")


def _sched_week_text(execute_time):
    """planType=1 的 executeTime（如 '3,4,5,1,2'）→ '週一、週二…'。"""
    return "、".join("週" + _SCHED_WEEK.get(x, x) for x in str(execute_time or "").split(",") if x)


def _sched_enabled_item_count(client, tpls):
    """已啟用模板底下的排程項目總數（唯讀；供主開關警示判斷）。"""
    n = 0
    for t in tpls:
        if _sched_template_enabled(t):
            items = client.get(_SCHED_ITEM_LIST.format(tid=t.get("tempId")))
            n += len(items) if isinstance(items, list) else 0
    return n


def pcs_schedule_view():
    """
    Item 9：PCS 排程配置（唯讀）。先列所有排程模板，選單筆後進入模板子選單。
    只呼叫唯讀 GET（getScheduleSwitch / template/list / list/{tempId}），不送任何控制。
    ※ 模板開/關＝個別排程 enableFlag，與 Item 8 的排程主開關（schedulePlanSwitch）不同。
    """
    c = _sched_readonly_client()
    if c is None:
        return
    while True:
        ps, _ms, _raw = _sched_switch_state(c)
        tpls = c.get(_SCHED_TPL_LIST)
        tpls = tpls if isinstance(tpls, list) else []
        print("\nPCS 排程配置")
        print(f"（排程主開關：{'開' if ps == 1 else ('關' if ps == 0 else '無法確認')}）")
        if not tpls:
            print("  (無排程模板)")
        for i, t in enumerate(tpls, 1):
            en = _sched_template_enabled(t)
            print(f"{i}. {t.get('tempName')}   狀態：{'已啟用' if en else '已停用'}")
        print("0. 返回")
        # 主開關已開但無任何啟用項目 → 警示（智慧模式 pre-check 依據）
        if ps == 1 and _sched_enabled_item_count(c, tpls) == 0:
            print("⚠ 排程主開關已開啟，但排程配置沒有任何啟用項目。")
        ch = _menu_input("請選擇排程（號碼查看單筆，0 返回）：")
        if ch == "0":
            return
        if ch.isdigit() and 1 <= int(ch) <= len(tpls):
            _pcs_schedule_template_submenu(c, tpls[int(ch) - 1])
        else:
            print("無效選項，請重新輸入。")


def _pcs_schedule_template_submenu(client, tpl):
    """
    單筆排程模板子選單：查看明細 / 開啟此排程 / 關閉此排程（開關控制該模板 enableFlag）。
    ⚠️ enableFlag 寫入 API（前端經 PUT /schedule/template 整筆更新）之 payload 尚未由 DevTools 確認，
       故「開啟/關閉此排程」目前標示「尚未開放」，不送任何寫入（唯讀）。
    """
    tid = tpl.get("tempId")
    while True:
        en = _sched_template_enabled(tpl)
        pt = tpl.get("planType")
        items = client.get(_SCHED_ITEM_LIST.format(tid=tid))
        items = items if isinstance(items, list) else []
        print(f"\n排程名稱：{tpl.get('tempName')}")
        print(f"狀態：{'已啟用' if en else '已停用'}")
        print(f"週期：{'每週' if pt == 1 else '按日期'}")
        if pt == 1:
            print(f"星期：{_sched_week_text(tpl.get('executeTime'))}")
        else:
            print(f"日期：{tpl.get('executeTime')}")
        print(f"排程項目數：{len(items)}")
        print("1. 查看排程明細")
        print("2. 開啟此排程　（尚未開放）")
        print("3. 關閉此排程　（尚未開放）")
        print("0. 返回")
        ch = _menu_input("請輸入選項：")
        if ch == "0":
            return
        if ch == "1":
            if not items:
                print("  (無排程項目)")
            for i, it in enumerate(items, 1):
                cd = _SCHED_CD.get(it.get("chargeOrDischarge"), it.get("chargeOrDischarge"))
                print(f"  No.{i}  {it.get('startTime')}~{it.get('endTime')}  {cd}  "
                      f"{it.get('instantaneousPowerLimit')} kW  SOC {it.get('soc')}%  "
                      f"項目狀態：{'啟用' if en else '停用'}")
        elif ch in ("2", "3"):
            print("⚠ 目前僅唯讀，個別排程開/關控制功能尚未開放。")
            print("  （開/關＝更新該模板 enableFlag；前端經 PUT /schedule/template 整筆更新，"
                  "payload 尚未由 DevTools 確認，故暫不實作寫入。）")
        else:
            print("無效選項，請重新輸入。")


def _pcs_schedule_switch_menu():
    """
    Item 8：PCS 排程主開關（開/關）。控制整個智慧排程（schedulePlanSwitch），與 Item 9 個別模板不同。
    ⚠️ editScheduleSwitch 之實際 PUT payload 尚未由 DevTools 確認 → 開/關僅顯示、標示「尚未開放」，
       不猜測、不送任何寫入（唯讀）。待確認 payload 後才實作（寫入後會重新 GET 驗證，失敗不顯示成功）。
    """
    c = _sched_readonly_client()
    if c is None:
        return
    while True:
        ps, _ms, _raw = _sched_switch_state(c)
        cur = "開" if ps == 1 else ("關" if ps == 0 else "無法確認")
        print("\nPCS 排程主開關")
        print(f"目前狀態：{cur}")
        print("1. 開啟排程主開關　（尚未開放）")
        print("2. 關閉排程主開關　（尚未開放）")
        print("0. 返回")
        ch = _menu_input("請輸入選項：")
        if ch == "0":
            return
        if ch in ("1", "2"):
            print("⚠ 目前僅唯讀，排程主開關控制功能尚未開放。")
            print("  （開/關＝PUT /schedule/config/editScheduleSwitch；payload 尚未由 DevTools 確認，"
                  "為避免猜測 manualModeSwitch/多送欄位，暫不實作寫入。）")
        else:
            print("無效選項，請重新輸入。")


def handle_choice(choice):
    """回傳 True 繼續、False 離開。"""
    if choice == "0":
        print("結束程式。")
        return False

    # 充放電報告（Menu 18/19/20）：獨立唯讀報告模組，不送任何控制
    if choice == "18":
        report_start()
        return True
    if choice == "19":
        report_stop()
        return True
    if choice == "20":
        report_view_latest()
        return True

    # PCS 排程配置（唯讀查看）
    if choice == SCHEDULE_VIEW_CHOICE:
        pcs_schedule_view()
        return True

    # PCS 排程主開關（開/關）— 目前唯讀顯示（寫入待確認）
    if choice == SCHEDULE_SWITCH_CHOICE:
        _pcs_schedule_switch_menu()
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

    # PCS 功率控制（4 交流有功 / 5 直流恆流 / 6 直流恆功率）→ 進入第二層
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


def _terminate_child(proc):
    """
    安全清理子程序（Windows 亦適用）：terminate → 等待 → kill → communicate 回收 stdout/stderr。
    回傳 (stdout, stderr)。避免逾時/取消後殘留背景 Python 程序。
    """
    out = err = ""
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        out, err = proc.communicate(timeout=5)          # 等待自行結束並回收管線
    except subprocess.TimeoutExpired:
        try:
            proc.kill()                                 # 仍未結束 → 強制 kill
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            pass
    except Exception:
        pass
    return out or "", err or ""


def _run_readonly_with_countdown(args, wait_label, timeout=SUBPROCESS_TIMEOUT_SEC):
    """
    以 Popen + 每秒輪詢執行唯讀子程序，同一行顯示倒數（非阻塞式 subprocess.run）。
    回傳 (returncode, stdout, stderr, status)；status ∈ done / timeout / interrupted / launch_error。
    逾時或使用者 Ctrl+C 皆會清理子程序（terminate→kill→communicate），不殘留、不卡住。
    """
    try:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", *args],
            cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        print(f"✗ 無法啟動查詢子程序：{e}")
        return None, "", "", "launch_error"

    _clear = "\r" + " " * 44 + "\r"
    try:
        waited = 0
        while waited < timeout:
            rc = proc.poll()
            if rc is not None:                          # 子程序已結束
                out, err = proc.communicate()
                print(_clear, end="")
                return rc, out or "", err or "", "done"
            print(f"\r[{waited:02d}/{timeout}] {wait_label}", end="", flush=True)
            time.sleep(1)
            waited += 1
        # 逾時：清理子程序，不再等待
        print(_clear, end="")
        print(f"✗ 等待逾時（{timeout} 秒）")
        print("正在終止查詢程序...")
        out, err = _terminate_child(proc)
        print("✓ 查詢程序已終止")
        return None, out, err, "timeout"
    except (KeyboardInterrupt, EOFError):
        print(_clear, end="")
        print("使用者取消設備狀態查詢。")
        _terminate_child(proc)
        return None, "", "", "interrupted"


def refresh_status():
    """
    執行唯讀 scraper 刷新狀態（Popen + 倒數 + 硬性逾時 + 子程序清理 + Ctrl+C 友善處理）。
    回傳 status_summary；失敗/逾時/取消一律回 {}，絕不無限卡住。
    """
    rc, out, err, status = _run_readonly_with_countdown([SCRAPER], "正在取得設備狀態...")
    # 印登入 4 行（若有）
    for line in (out or "").splitlines():
        if line.startswith(("嘗試登入", "[ENV] loaded", "HMI login")):
            print(line)

    if status == "launch_error":
        return {}
    if status == "interrupted":
        return {}                                       # 已印「使用者取消」，不印 Traceback
    if status == "timeout":
        print(f"✗ 設備狀態在 {SUBPROCESS_TIMEOUT_SEC} 秒內沒有回應，查詢已取消。")
        print("  請確認 192.168.128.110:8080 是否可連線（IP / Port / 網路）。")
        return {}
    if rc != 0:
        print("✗ 設備狀態查詢失敗。")
        print(f"Exit Code：{rc}")
        for l in [x for x in (err or "").strip().splitlines() if x][-5:]:
            print("   ", l)
        return {}

    print("✓ 設備狀態取得成功")
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
        # （放電/充電/待機的「電池充放電狀態」不在儀表板顯示，但仍用於 operator 的電池下電前置檢查。）
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


def _prompt_with_timeout(prompt, timeout):
    """
    顯示 prompt 並等待一行輸入；**逾時且使用者尚未輸入任何字元 → 回傳 None**（觸發自動刷新）。
    prompt=None 表示不重印提示行（前一輪已印過且畫面未被其他訊息覆蓋）。
      - Windows：msvcrt 逐鍵讀取（不需背景 thread，離開函式即停止等待）
      - POSIX  ：select 監看 stdin
      - 非 tty / 兩者皆不可用：退回阻塞 input（無自動刷新，僅提示一次）
    使用者已開始輸入（尚未按 Enter）時**不逾時**，避免打斷輸入。
    Ctrl+C / Ctrl+D 比照原本 input() 行為，拋出 KeyboardInterrupt / EOFError。
    """
    if prompt:
        print(prompt, end="", flush=True)

    if not sys.stdin.isatty():                       # 管線/重導向輸入（如自動化測試）→ 不做逾時
        if not _auto.get("_no_tty_warned"):
            _auto["_no_tty_warned"] = True
            print("\n（stdin 非互動終端 → 停用自動刷新，請手動按 r 重新整理）")
        return input()

    try:
        import msvcrt                                # Windows
    except ImportError:
        msvcrt = None

    if msvcrt is not None:
        try:
            return _prompt_msvcrt(msvcrt, timeout)
        except (KeyboardInterrupt, EOFError):
            raise
        except OSError:
            # 無附接主控台（如 pythonw / 服務模式）→ 退回阻塞輸入，不讓選單崩潰
            print("\n（無法逐鍵讀取主控台 → 停用自動刷新，請手動按 r 重新整理）")
            return input()

    try:
        import select                                # POSIX
        rlist, _, _ = select.select([sys.stdin], [], [], timeout)
        if not rlist:
            print()
            return None
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")
    except (ImportError, OSError, ValueError):
        return input()                               # 最後退路：阻塞（無自動刷新）


def _prompt_msvcrt(msvcrt, timeout):
    """
    _prompt_with_timeout 的 Windows 實作：逐鍵讀取，不需背景 thread。
    逾時且尚未輸入任何字元 → 回 None；已開始輸入則持續等到 Enter（不打斷使用者）。
    """
    buf = ""
    deadline = time.monotonic() + timeout
    while True:
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                return buf
            if ch == "\x03":                         # Ctrl+C
                raise KeyboardInterrupt
            if ch == "\x04":                         # Ctrl+D
                raise EOFError
            if ch in ("\x00", "\xe0"):               # 功能鍵前置碼 → 連同下一碼丟棄
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if ch == "\b":                           # 退格
                if buf:
                    buf = buf[:-1]
                    print("\b \b", end="", flush=True)
                continue
            buf += ch
            print(ch, end="", flush=True)
        if not buf and time.monotonic() >= deadline:
            return None                              # **不換行**：由真正要輸出的第一行自己補
                                                     #（見 _auto_pending_newline；避免每 15s 留空白行）
        time.sleep(0.05)                             # 讓出 CPU（前景輪詢，非背景 thread）


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
            # 完整重畫（進入迴圈、按 r、或從控制選單返回）；同時做一次監看判斷
            dashboard_refresh(auto=False)
            sl = report_status_line() if _REPORT_AVAILABLE else None
            if sl:
                print(f"\n{sl}")

            # 等待輸入；逾時 → 自動刷新（前景、不新增 thread、離開迴圈即停止 GET）
            prompt = (f"\n[r] 重新整理　[e] 進入控制執行選單　[0] 離開"
                      f"（{AUTO_REFRESH_SEC}s 未輸入則自動刷新）：")
            choice = None
            show_prompt = True
            try:
                while choice is None:
                    choice = _prompt_with_timeout(prompt if show_prompt else None,
                                                  AUTO_REFRESH_SEC)
                    if choice is None:
                        # 自動刷新：不重畫整個 Dashboard，僅在狀態改變/建立報告時輸出。
                        # 游標仍停在提示行末端 → 交由第一行輸出自己補換行（無輸出即無痕跡）。
                        _auto_pending_newline(True)
                        _r, printed = dashboard_refresh(auto=True)
                        show_prompt = printed        # 有輸出才重印提示行，避免提示行被推走
                _auto_pending_newline(False)         # 使用者已按 Enter（自帶換行）
                choice = choice.strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n結束程式。")
                break
            if choice == "0":
                print("結束程式。")
                break
            if choice == "e":
                _exec_menu()
            elif choice in ("18", "19", "20"):
                # 便利：儀表板層直接輸入 18/19/20 也可操作充放電報告（等同進控制選單後選同項）
                try:
                    handle_choice(choice)
                except Exception as e:
                    print(f"[錯誤] 執行報告選項 {choice} 時發生例外：{e}")
            elif choice in ("r", ""):
                pass                       # 重新整理
            else:
                print(f"（'{choice}' 非本層選項；請按 e 進入控制執行選單，或直接輸入 18/19/20 操作報告）")
    finally:
        # 離開程式時，若仍有進行中的報告 → 暫停（paused，保留可續接），不強制 completed。
        if _REPORT_AVAILABLE and _report.get("session") is not None:
            print("\n偵測到進行中的充放電報告，離開前自動暫停（下次選 16 可續接）…")
            report_pause()


if __name__ == "__main__":
    main()
