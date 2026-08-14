# -*- coding: utf-8 -*-
"""
auto_monitor_service.py — Auto Monitor Service（Phase 4.3：可獨立執行的背景監看）
======================================================================
用途
    讓智慧排程自動報告**完全脫離 device_control_menu.py / Dashboard** 獨立運作。
    在此之前，Auto Start 的唯一觸發點是 Dashboard 前景刷新迴圈 —— Dashboard 沒開，
    排程時間到了也不會建立報告。本檔提供一個無 UI、無 stdin 的常駐迴圈來驅動它。

設計原則（本檔是「組裝層」，不含任何判定邏輯）
    所有監看／生命週期能力一律沿用 report_monitor.py 既有函式，**不複製**：
      · _report / _auto / _SESSION_LOCK / _CLIENT_LOCK  → 只有 report_monitor 那一份
      · 排程偵測、Start/Stop Debounce、Cooldown、finalize、resume、atexit → 全部沿用
    本檔只負責：訊號處理、迴圈節奏、關閉流程。

依賴方向（單向，不得反轉）
    auto_monitor_service.py ─┐
                             ├─► report_monitor.py ─► charge_discharge_report.py
    device_control_menu.py ──┘
    本檔**不得** import device_control_menu；report_monitor 亦不得 import 本檔。

執行流程
    啟動 → 恢復未完成 Session（RM.report_resume_on_launch）
         → 迴圈：登入/取樣/監看判定（RM.auto_schedule_check）
         → 條件成立時由 Monitor Core 建立 ReportSession 並起背景取樣執行緒
         → 排程結束、idle 達門檻 → Monitor Core 自動收尾並產生報告
         → 收到 SIGINT/SIGBREAK/SIGTERM 或 Ctrl+C → RM.report_pause()

關閉語意（**可續接**，不是結束報告）
    正常關閉一律走 RM.report_pause()：停止取樣、Session 標記為 paused。
    **不呼叫 report_stop()**，避免把「服務重啟」誤判成「排程結束」而提前 finalize。
    下次啟動由 RM.report_resume_on_launch() 續接同一個 Session。
    行程若異常退出，report_monitor 的 atexit 保底仍會標記 paused（Phase 3.6）。

執行緒
    本檔**不建立任何執行緒**。監看迴圈跑在主執行緒；唯一的背景執行緒是
    Monitor Core 的取樣執行緒（_report_bg_start），且全程最多一條。

尚未涵蓋（Phase 4.4 Ownership 才處理）
    多行程並存無互斥：同時啟動兩個 Service（或 Service + Dashboard）會各自持有
    一份 _report，可能建立兩份 Session。4.3 只建立「可獨立執行」的能力。

用法
    python auto_monitor_service.py                  # 常駐（預設 10s 一輪）
    python auto_monitor_service.py --interval 15    # 自訂輪詢間隔
    python auto_monitor_service.py --once           # 只跑一輪後結束（需連設備）
"""
import os
import sys
import signal
import argparse
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import report_monitor as RM                         # noqa: E402
import charge_discharge_report_config as CDR_CFG    # noqa: E402

SERVICE_NAME = "AutoMonitorService"
SOURCE = "service"

# ---- Log（Phase 4.6）-------------------------------------------------
# 正式部署由 NSSM 的 AppStdout / AppStderr 負責導向與 rotation；
# 本檔另提供 --log 與「無 console 時自動導向」作為第二層保障，
# 讓不經 NSSM 直接執行、或日後改用其他包裝方式時也不會因 stdout 為 None 而崩潰。
#
# ⚠️ 為什麼需要這層：Windows Service（session 0）沒有 console，
#    sys.stdout / sys.stderr 可能為 None，而本專案有 400 餘處 print()。
#    若不先導向，第一個 print() 就會 AttributeError 讓服務啟動失敗。
#    這裡刻意**不改動任何既有 print()** —— 只換掉它們寫入的目標。
DEFAULT_LOG_DIR = os.path.join(os.path.dirname(HERE), "output", "logs")
DEFAULT_LOG_NAME = "auto_monitor_service.log"
LOG_MAX_BYTES = 10 * 1024 * 1024        # 單檔上限 10MB
LOG_BACKUP_COUNT = 5                    # 保留 .1 ~ .5

# 預設輪詢間隔。刻意**不等於** AUTO_HOLD_START_SEC（15s）——
# 兩者相同時，Start Debounce 的達成會卡在「剛好差一輪」的邊界；
# 10s 可讓 15s 門檻穩定在第 2 輪達成。判定邏輯本身不受影響（一律用 monotonic）。
DEFAULT_INTERVAL_SEC = 10


def _rotate_log(path, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUP_COUNT):
    """
    開檔前的尺寸輪替：超過上限就 .5 刪除、.4→.5 … .1→.2、本體→.1。

    只在開檔時檢查一次（服務啟動時），不做執行中的即時輪替 ——
    正式部署由 NSSM 的 AppRotateBytes 負責，本層只避免單檔無限成長。
    任何失敗都不得中斷服務啟動，故全程吞例外。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return False
        oldest = f"{path}.{backups}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(backups - 1, 0, -1):
            src, dst = f"{path}.{i}", f"{path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
        return True
    except Exception:                                # noqa: BLE001
        return False


def setup_logging(log_path=None):
    """
    決定 stdout/stderr 的去向。回傳實際使用的 log 路徑，或 None（維持 console）。

    規則：
      · 有指定 --log            → 一律導向該檔
      · 未指定但 stdout 為 None → 無 console（Windows Service）→ 導向預設 log
      · 未指定且有 console      → 不動，維持互動輸出（NSSM 會自行接管）

    導向失敗時**不得讓服務起不來**：退回 os.devnull，服務照常運作
    （正式部署另有 NSSM 的 AppStdout 作為第一層，不會兩層同時失效）。
    """
    need = log_path is not None or sys.stdout is None or sys.stderr is None
    if not need:
        return None
    path = log_path or os.path.join(DEFAULT_LOG_DIR, DEFAULT_LOG_NAME)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _rotate_log(path)
        f = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
        sys.stdout = f
        sys.stderr = f
        return path
    except Exception:                                # noqa: BLE001
        try:
            f = open(os.devnull, "w", encoding="utf-8")
            sys.stdout = f
            sys.stderr = f
        except Exception:                            # noqa: BLE001
            pass
        return None


def _env_file_in_use():
    """診斷用：回報 api_client 實際會採用的 env 檔（找不到或無法查詢回 None）。"""
    try:
        import api_client
        cands = api_client.discover_env_candidates()
        return str(cands[0]) if cands else None
    except Exception:                                # noqa: BLE001
        return None


def print_startup_diagnostics(log_path, interval, installed_signals, owner_why):
    """
    啟動診斷：把「Service 環境與互動式 CMD 的差異」一次列清楚。

    Windows Service 在 session 0 執行，cwd 為 System32、不繼承使用者 PATH／proxy／
    對應磁碟。這些欄位是排查「在 CMD 可以跑、裝成服務就不行」的第一手資訊。
    """
    import platform
    print("=" * 66)
    print(f"{SERVICE_NAME} 啟動    {datetime_now()}")
    print("=" * 66)
    print(f"  Python          : {sys.executable}")
    print(f"  版本            : {platform.python_version()}    主機：{platform.node()}")
    print(f"  執行帳號        : {os.environ.get('USERNAME', '?')}"
          f"（USERDOMAIN={os.environ.get('USERDOMAIN', '?')}）")
    print(f"  cwd             : {os.getcwd()}")
    print(f"  程式目錄        : {HERE}")
    print(f"  env 檔          : {_env_file_in_use() or '（未找到）'}"
          f"    API_ENV_FILE={os.environ.get('API_ENV_FILE') or '未設定'}")
    print(f"  output root     : {RM._report_output_root()}")
    print(f"  log             : {log_path or '（console）'}")
    print(f"  NO_PROXY        : {os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or '未設定'}")
    print(f"  HTTP(S)_PROXY   : {os.environ.get('HTTP_PROXY') or '未設定'}"
          f" / {os.environ.get('HTTPS_PROXY') or '未設定'}")
    print(f"  Mutex           : {RM._mutex_name()}")
    print(f"  Monitor Owner   : 本行程（pid {os.getpid()}，{owner_why}）")
    print(f"  監看間隔        : {interval:g}s")
    print(f"  自動建立報告    : {getattr(CDR_CFG, 'AUTO_SCHEDULE_REPORT_ENABLED', None)}"
          f"（AUTO_SCHEDULE_REPORT_ENABLED）")
    print(f"  Start/Stop/Cool : {getattr(CDR_CFG, 'AUTO_HOLD_START_SEC', None)}s"
          f" / {getattr(CDR_CFG, 'AUTO_HOLD_STOP_SEC', None)}s"
          f" / {getattr(CDR_CFG, 'AUTO_COOLDOWN_SEC', None)}s")
    print(f"  訊號處理        : {', '.join(installed_signals) or '（無可用訊號）'}")
    print("  關閉語意        : report_pause()（paused，可續接；不 finalize）→ release Ownership")
    print("-" * 66)


def datetime_now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def tick(source=SOURCE):
    """
    單輪監看：取得 client → 唯讀取樣 → 交由 Monitor Core 判定。

    回傳 (action, reason)：action 為 auto_schedule_check 的結果
    （started / skipped / noop / ...），取不到 client 或取樣失敗時回 (None, 原因)。
    **本函式不做任何判定**，只負責把既有函式串起來。
    """
    client, _printed = RM._monitor_client()
    if client is None:
        return None, "no_client"                     # 登入失敗；RM 內部已負責節流重試
    reading = RM._read_device_state(client)
    if not isinstance(reading, dict):
        return None, "read_failed"
    try:
        RM._auto_note(RM._monitor_status_line(reading, source),
                      key="last_status", with_time=True)
    except Exception as e:                           # noqa: BLE001
        print(f"[{SERVICE_NAME}] 狀態行輸出失敗（不影響監看）：{type(e).__name__}: {e}")
    return RM.auto_schedule_check(reading, client=client, source=source)


def install_signal_handlers(stop_event):
    """
    安裝關閉訊號處理。回傳實際安裝成功的訊號名稱 list。

    ⚠️ SIGBREAK 僅存在於 Windows；SIGTERM 在部分環境不可用；
       signal.signal() 只能在主執行緒呼叫。三者皆以 hasattr / try 保護，
       任何一個不可用都不影響其餘訊號與服務啟動。
    """
    installed = []

    def _handler(_signum, _frame):
        stop_event.set()

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
            installed.append(name)
        except (ValueError, OSError, RuntimeError):
            # ValueError：非主執行緒；OSError/RuntimeError：平台不支援
            pass
    return installed


def run(interval=DEFAULT_INTERVAL_SEC, stop_event=None, source=SOURCE,
        max_ticks=None, resume=True):
    """
    監看主迴圈（跑在呼叫端的執行緒，**不另開 thread**）。回傳實際執行的輪數。

    interval  : 每輪間隔秒數
    stop_event: 外部可注入的 threading.Event（供訊號處理與測試控制）
    max_ticks : 測試用；達到輪數即返回（None = 無限）
    resume    : 啟動時是否嘗試恢復未完成 Session

    KeyboardInterrupt 於此處攔截並轉為正常結束，確保 finally 的關閉流程一定執行。
    單輪內的例外一律記錄後續行 —— 監看服務不得因單次取樣失敗而整個停擺。
    """
    stop_event = stop_event or threading.Event()
    if resume:
        try:
            RM.report_resume_on_launch()
        except Exception as e:                       # noqa: BLE001
            print(f"[{SERVICE_NAME}] 恢復未完成 Session 失敗（繼續啟動）："
                  f"{type(e).__name__}: {e}")
    ticks = 0
    try:
        while not stop_event.is_set():
            try:
                tick(source)
            except Exception as e:                   # noqa: BLE001
                print(f"[{SERVICE_NAME}] 本輪監看發生例外（略過，繼續下一輪）："
                      f"{type(e).__name__}: {e}")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            stop_event.wait(interval)
    except KeyboardInterrupt:
        print(f"\n[{SERVICE_NAME}] 收到 Ctrl+C，準備關閉…")
        stop_event.set()
    return ticks


def shutdown():
    """
    關閉流程：停止背景取樣並把 Session 標記為 paused（**可續接**）。

    刻意使用 report_pause() 而非 report_stop()：
    服務停止不代表排程結束，若在此 finalize 會產生一份不完整的報告，
    且下次啟動無法續接。paused 可由 report_resume_on_launch() 接回同一個 Session。
    """
    # 順序不可顛倒：先 pause（此時仍是 Owner，寫得進 session_state.json），
    # 再釋放 Mutex。先 release 會讓 pause 被 Owner 判斷擋下 → Session 停在 recording。
    try:
        RM.report_pause()
    except Exception as e:                           # noqa: BLE001
        print(f"[{SERVICE_NAME}] 關閉時暫停 Session 失敗"
              f"（atexit 保底仍會處理）：{type(e).__name__}: {e}")
    try:
        RM.release_ownership()
    except Exception as e:                           # noqa: BLE001
        print(f"[{SERVICE_NAME}] 釋放 Ownership 失敗"
              f"（atexit 保底仍會處理）：{type(e).__name__}: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Auto Monitor Service —— 無 UI 的智慧排程自動報告監看服務")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                    help=f"監看輪詢間隔秒數（預設 {DEFAULT_INTERVAL_SEC}）")
    ap.add_argument("--once", action="store_true",
                    help="只執行一輪後結束（需連設備；供人工煙霧測試）")
    ap.add_argument("--no-resume", action="store_true",
                    help="啟動時不恢復未完成 Session")
    ap.add_argument("--log", metavar="PATH", default=None,
                    help=f"把 stdout/stderr 導向指定檔案（預設：無 console 時自動導向 "
                         f"{os.path.join(DEFAULT_LOG_DIR, DEFAULT_LOG_NAME)}）")
    args = ap.parse_args(argv)

    # ⚠️ 必須在任何 print() 之前 —— Windows Service 的 sys.stdout 可能為 None。
    log_path = setup_logging(args.log)

    stop_event = threading.Event()
    installed = install_signal_handlers(stop_event)

    # Phase 4.4：取得 Monitor Ownership 才有資格驅動生命週期與寫入 Session。
    # 取不到就直接退出 —— Service 沒有 UI，留著只會空轉。
    owned, why = RM.acquire_ownership(role="service")
    if not owned:
        if why == "held_by_other":
            print(f"[{SERVICE_NAME}] 另一個行程已持有 Monitor Ownership → 本行程不啟動。")
            print("             （這是預期的競爭結果，非程式故障）")
            return getattr(CDR_CFG, "EXIT_OWNER_HELD_BY_OTHER", 3)
        print(f"[{SERVICE_NAME}] 無法取得 Monitor Ownership（{why}）→ fail closed，不啟動。")
        return getattr(CDR_CFG, "EXIT_OWNER_API_ERROR", 4)

    print_startup_diagnostics(log_path, args.interval, installed, why)

    try:
        n = run(interval=args.interval, stop_event=stop_event,
                max_ticks=1 if args.once else None, resume=not args.no_resume)
    finally:
        shutdown()
    print(f"[{SERVICE_NAME}] 已停止（共執行 {n} 輪）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
