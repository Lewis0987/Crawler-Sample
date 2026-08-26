# -*- coding: utf-8 -*-
"""
report_monitor.py — Auto Monitor Core（智慧排程自動報告的監看與生命週期核心）
======================================================================
Phase 4.2 由 device_control_menu.py 抽離而來。程式碼為**原樣搬移**，未重新實作
任何判定邏輯，因此行為與 Phase 3 完全一致。

本模組負責：
  D 裝置讀取    共用 ApiClient、_CLIENT_LOCK、唯讀取樣
  E 排程感知    排程窗口查詢與 schedule_ctx 計算（Phase 3.5）
  C 生命週期    ReportSession 的建立／背景取樣／停止／finalize（Phase 3.3、3.7）
  B 監看決策    auto_schedule_check() 與 _auto 狀態機（Phase 2、3.4）
  F 恢復        resume／孤兒分類／atexit 保底（Phase 3.6）

不負責（一律留在 device_control_menu.py）：
  A  UI 繪製、選單、Dashboard 刷新迴圈、人工控制與其驗證輪詢

依賴方向為單向：device_control_menu.py -> report_monitor.py。
本模組不得 import device_control_menu，否則造成循環 import。

全域狀態（_report / _auto / _SESSION_LOCK / _CLIENT_LOCK）在此為唯一一份；
呼叫端一律透過 `import report_monitor as RM` 以 RM.<name> 存取，不要 from-import
個別名稱 —— from-import 會在呼叫端綁定獨立參考，測試對 RM.<name> 的注入不會生效。

atexit 保底在本模組 import 時註冊一次；device_control_menu.py 不得再註冊。
"""

import atexit
import io
import os
import sys
import json
import time
import glob
import contextlib
import threading
from datetime import datetime, timedelta
from datetime import time as dtime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(os.path.dirname(HERE), "output")

# 充放電報告模組（唯讀使用；本檔不修改該模組、不送任何控制 API）
try:
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CDR_CFG
    _REPORT_AVAILABLE = True
    _REPORT_IMPORT_ERR = None
except Exception as _e:
    CDR = None
    CDR_CFG = None
    _REPORT_AVAILABLE = False
    _REPORT_IMPORT_ERR = str(_e)


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


# ======================================================================
# Monitor Ownership（Phase 4.4）—— 跨行程互斥
# ----------------------------------------------------------------------
# 要防止的不是「建立兩份 Session」，而是**同一個 Session 資料夾同時有兩個
# 寫入者**：find_active_session() 只看 status，任何第二個行程啟動時都會
# resume 同一份 Session 並另起取樣執行緒，導致 samples.csv 交錯、
# sample_index 重複、session_state.json 互相覆寫、累積電量錯亂、雙重 finalize。
#
# invariant：
#   只有取得 Monitor Ownership 的行程，才有資格驅動自動報告生命週期與寫入 Session。
#
# ⚠️ 互斥的**唯一依據**是 Windows 命名 Mutex。
#    monitor_owner.json 只是診斷資訊，**永遠不得**用來判定誰是 Owner
#    —— PID 會被回收、檔案會殘留，用它判定必然出錯。
#
# ⚠️ 執行緒親和性：Mutex 所有權綁定「執行 WaitForSingleObject 的那條執行緒」，
#    只有該執行緒能 ReleaseMutex。acquire/release 一律在主執行緒進行；
#    取樣執行緒絕不碰 ownership。
#
# ⚠️ 非 Windows 平台一律 fail closed（自動報告停用），**不得**退回無鎖或自製
#    lock file —— 那會讓「防雙寫」這件事失去保證。
# ======================================================================
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

_own = {
    "handle": None,        # Mutex HANDLE（僅 Owner 持有）
    "owned": False,        # **唯一**的所有權真值來源
    "abandoned": False,    # 是否由 WAIT_ABANDONED 接管（前任崩潰）
    "role": None,          # "service" / "dashboard" / 測試用字串
    "reason": None,        # 最近一次 acquire 的結果原因
    "thread_id": None,     # 取得所有權的執行緒（release 必須同一條）
    "name": None,          # 實際使用的 Mutex 名稱
}


def _k32():
    """取得 kernel32；非 Windows 或載入失敗回 None（呼叫端 fail closed）。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                                    # noqa: BLE001
        return None
    if not hasattr(ctypes, "WinDLL"):
        return None
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        # restype 必須明確宣告：64 位元下 HANDLE 預設會被截成 32 位元 int
        #
        # ⚠️ 一律使用 CreateMutexExW，**不得**改回 CreateMutexW：
        #    CreateMutexW 對「已存在的物件」隱含要求 MUTEX_ALL_ACCESS，
        #    因此 LocalSystem 服務先建立的 Mutex，一般使用者的 Dashboard 會拿到
        #    ERROR_ACCESS_DENIED(5) 而非 held_by_other —— 互斥語意會退化成「拿不到鎖」。
        #    CreateMutexExW 可指定 dwDesiredAccess，讓非 Owner 只要求最小必要權限。
        k.CreateMutexExW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR,
                                     wintypes.DWORD, wintypes.DWORD]
        k.CreateMutexExW.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k.ReleaseMutex.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.LocalFree.argtypes = [wintypes.LPVOID]
        k.LocalFree.restype = wintypes.LPVOID
        return k
    except Exception:                                    # noqa: BLE001
        return None


# Mutex 存取權：Wait 需要 SYNCHRONIZE、ReleaseMutex 需要 MUTEX_MODIFY_STATE。
# 兩者即為全部所需 —— 刻意**不**要求 MUTEX_ALL_ACCESS（最小權限原則）。
SYNCHRONIZE = 0x00100000
MUTEX_MODIFY_STATE = 0x0001
MUTEX_ALL_ACCESS = 0x001F0001
MUTEX_MIN_ACCESS = SYNCHRONIZE | MUTEX_MODIFY_STATE          # 0x00100001

# 建立 Mutex 時附帶的 DACL（SDDL）。
#   SY LocalSystem         → MUTEX_ALL_ACCESS   （Windows Service 以此帳號執行）
#   BA Administrators      → MUTEX_ALL_ACCESS
#   AU Authenticated Users → SYNCHRONIZE|MUTEX_MODIFY_STATE（僅同步所需）
# 刻意不使用 Everyone(WD)，也不給 AU FULL CONTROL。
# 沒有這個 DACL，LocalSystem 建立的物件預設不授權一般使用者 → Dashboard 會 ACCESS_DENIED。
MUTEX_SDDL = "D:(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)(A;;0x100001;;;AU)"
SDDL_REVISION_1 = 1


def _advapi32():
    """取得 advapi32（SDDL → Security Descriptor）；失敗回 None（呼叫端 fail closed）。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                                    # noqa: BLE001
        return None
    if not hasattr(ctypes, "WinDLL"):
        return None
    try:
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        return a
    except Exception:                                    # noqa: BLE001
        return None


def _mutex_name():
    """
    Mutex 名稱：前綴 + output_root **正規化後**絕對路徑的雜湊
    （同機不同部署不互相搶鎖）。

    ⚠️ 路徑必須先 normcase：Windows 檔案系統不分大小寫，同一個資料夾可以有
       多種字面表示，而 os.path.abspath() **不會**正規化磁碟機代號大小寫。
       2026-08-14 實機案例：Service 以 `D:\\Crawler Sample\\...` 啟動、
       Dashboard 以 `d:/Crawler Sample/test/device_control_menu.py` 啟動，
       同一個 output root 卻算出兩顆不同的 Mutex
       （0c305bef986a0531 / fc4cd03c3cd675eb）→ 兩個行程各自 acquire 成功
       → 雙 Owner → Writer Gate 三層全部放行，正是 Phase 4.4 要防的情況。
       normcase 會把整串轉小寫並統一分隔符，abspath 則吸收相對路徑、
       `.`／`..` 與尾端分隔符，兩者合起來即可收斂所有實際會遇到的表示法。

    ⚠️ 刻意**不**使用 realpath：本專案部署路徑上沒有 symlink / junction，
       NTFS 也未產生 8.3 短檔名（皆已實測），realpath 只會多做檔案系統 I/O
       而不帶來額外收斂。UNC / 網路磁碟目前不在部署範圍。
    """
    import hashlib
    try:
        root = os.path.normcase(os.path.abspath(_report_output_root()))
    except Exception:                                    # noqa: BLE001
        root = os.path.normcase(os.path.abspath(OUTPUT_DIR))
    prefix = getattr(CDR_CFG, "MONITOR_MUTEX_PREFIX", "Global\\ESS_AutoMonitor_Owner_v1_")
    return prefix + hashlib.md5(root.encode("utf-8")).hexdigest()[:16]


def _build_mutex_security():
    """
    由 MUTEX_SDDL 建立 SECURITY_ATTRIBUTES。回傳 (sa, sd_ptr, err)。

    失敗回 (None, None, err) —— 呼叫端必須 fail closed，
    **不得**退回「無 DACL 的 CreateMutexW」：那正是跨帳號互斥失效的原因。

    sd_ptr 由 LocalAlloc 配置，呼叫端須在 CreateMutexExW 完成後 LocalFree。
    sa 必須在 CreateMutexExW 呼叫期間保持存活（故一併回傳，避免被 GC）。
    """
    a = _advapi32()
    if a is None:
        return None, None, "no_advapi32"
    try:
        import ctypes
        from ctypes import wintypes

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD),
                        ("lpSecurityDescriptor", wintypes.LPVOID),
                        ("bInheritHandle", wintypes.BOOL)]

        sd = wintypes.LPVOID()
        size = wintypes.DWORD()
        ok = a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            MUTEX_SDDL, SDDL_REVISION_1, ctypes.byref(sd), ctypes.byref(size))
        if not ok or not sd:
            return None, None, str(ctypes.get_last_error())
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = sd
        sa.bInheritHandle = False
        return sa, sd, 0
    except Exception as e:                                # noqa: BLE001
        return None, None, type(e).__name__


def is_owner():
    """本行程是否持有 Monitor Ownership。**唯一**的判定入口。"""
    return bool(_own["owned"])


def ownership_state():
    """Ownership 快照（供顯示與測試；不含 handle）。"""
    return {k: v for k, v in _own.items() if k != "handle"}


def acquire_ownership(role="unknown"):
    """
    嘗試取得 Monitor Ownership。回傳 (is_owner, reason)。**idempotent**。

    reason：
        already_owner    本行程已是 Owner（不重複 Wait —— 重複 Wait 會累加遞迴
                         計數，需等量的 Release 才真正釋放）
        acquired         正常取得
        abandoned_taken  前任持有者未釋放即死亡，由本行程接管（**視為成功**）
        held_by_other    他人持有（WAIT_TIMEOUT）
        no_win32         非 Windows 平台 → fail closed
        sd_failed_X      DACL / Security Descriptor 建立失敗 → fail closed
                         （**不會**退回無 DACL 的建立方式）
        create_failed_N  CreateMutexExW 回 NULL（N = GetLastError）
        wait_failed_N    WaitForSingleObject 回 WAIT_FAILED
        api_error_X      其他例外 → fail closed
    """
    if _own["owned"]:
        return True, "already_owner"
    k = _k32()
    if k is None:
        _own.update(reason="no_win32", role=role)
        return False, "no_win32"
    try:
        import ctypes
        name = _mutex_name()
        # open-or-create（單一呼叫，無 TOCTOU）：
        #   物件不存在 → 以 MUTEX_SDDL 建立，讓其他帳號日後開得起來
        #   物件已存在 → 以最小權限開啟（不要求 ALL_ACCESS，否則跨帳號會 ACCESS_DENIED）
        # 取得所有權仍完全由後續 WaitForSingleObject 決定（與建立分離）。
        sa, sd, sd_err = _build_mutex_security()
        if sa is None:
            # DACL 初始化失敗 → fail closed。
            # **不得**退回 CreateMutexW / 無 DACL 建立 —— 那會讓跨帳號互斥再次失效。
            _own.update(reason=f"sd_failed_{sd_err}", role=role)
            return False, _own["reason"]
        try:
            h = k.CreateMutexExW(ctypes.byref(sa), name, 0, MUTEX_MIN_ACCESS)
            err = ctypes.get_last_error()
        finally:
            # Security Descriptor 由 LocalAlloc 配置，且只需在 CreateMutexExW
            # 呼叫期間有效 —— 呼叫一結束立即釋放，不留 unmanaged memory。
            if sd:
                k.LocalFree(sd)
        if not h:
            _own.update(reason=f"create_failed_{err}", role=role)
            return False, _own["reason"]
        rc = k.WaitForSingleObject(h, 0)                  # 非阻塞
        if rc in (WAIT_OBJECT_0, WAIT_ABANDONED):
            _own.update(handle=h, owned=True, role=role, name=name,
                        abandoned=(rc == WAIT_ABANDONED),
                        reason=("abandoned_taken" if rc == WAIT_ABANDONED else "acquired"),
                        thread_id=threading.get_ident())
            _write_owner_file()                           # 診斷用；失敗不影響所有權
            if rc == WAIT_ABANDONED:
                print("[OWNER] 前一個 Monitor Owner 未正常釋放即結束（abandoned）"
                      " → 由本行程接管")
            return True, _own["reason"]
        k.CloseHandle(h)
        if rc == WAIT_TIMEOUT:
            _own.update(reason="held_by_other", role=role)
            return False, "held_by_other"
        err = ctypes.get_last_error()
        _own.update(reason=f"wait_failed_{err}", role=role)
        return False, _own["reason"]                      # WAIT_FAILED → fail closed
    except Exception as e:                                # noqa: BLE001
        _own.update(reason=f"api_error_{type(e).__name__}", role=role)
        return False, _own["reason"]                      # 任何不確定 → fail closed


def release_ownership():
    """
    釋放 Monitor Ownership。回傳是否確實釋放。

    三層保護，確保非 Owner 不可能誤釋放他人的 Mutex：
      ① _own["owned"] 為 False → 直接 no-op
      ② 執行緒不符 → 只 CloseHandle，不 ReleaseMutex（Mutex 為執行緒親和）
      ③ Windows 本身：ReleaseMutex 對非持有執行緒回 FALSE + ERROR_NOT_OWNER
    """
    if not _own["owned"]:
        return False
    # ⚠️ 執行緒不符時**什麼都不能做就返回**：
    #    不可 ReleaseMutex（Windows 會以 ERROR_NOT_OWNER 失敗），
    #    更不可 CloseHandle —— 關掉最後一個 handle 會讓 Mutex 物件消滅、名稱釋出，
    #    等於變相放棄所有權，正是本守門要防的事。
    #    也不可清掉 _own：所有權仍在，必須讓原執行緒之後還能正常釋放。
    if _own["thread_id"] != threading.get_ident():
        print("[OWNER] ⚠ release 被非取得執行緒呼叫 → 完全不處理，所有權維持不變"
              "（Mutex 為執行緒親和，僅取得者可釋放）")
        return False
    k = _k32()
    h = _own["handle"]
    released = False
    try:
        if k is not None and h:
            released = bool(k.ReleaseMutex(h))
            k.CloseHandle(h)
    except Exception as e:                                # noqa: BLE001
        print(f"[OWNER] 釋放 Ownership 時發生例外：{type(e).__name__}: {e}")
    finally:
        _delete_owner_file()
        _own.update(handle=None, owned=False, abandoned=False,
                    thread_id=None, reason="released")
    return released


def _owner_file_path():
    return os.path.join(_report_output_root(),
                        getattr(CDR_CFG, "MONITOR_OWNER_FILE", "monitor_owner.json"))


def _write_owner_file():
    """寫入診斷資訊。**任何失敗都不得影響 ownership** —— 故全程吞例外。"""
    try:
        import platform
        root = _report_output_root()
        if not os.path.isdir(root):
            os.makedirs(root, exist_ok=True)
        with open(_owner_file_path(), "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "host": platform.node(),
                "role": _own.get("role"),
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mutex_name": _own.get("name"),
                "abandoned_takeover": bool(_own.get("abandoned")),
                "note": "僅供診斷；Ownership 一律以 Windows Mutex 為準",
            }, f, ensure_ascii=False, indent=2)
    except Exception:                                     # noqa: BLE001
        pass


def _delete_owner_file():
    """只刪除自己寫的那一份（pid 相符）；失敗不影響任何事。"""
    try:
        p = _owner_file_path()
        st = _load_json(p)
        if isinstance(st, dict) and st.get("pid") == os.getpid():
            os.remove(p)
    except Exception:                                     # noqa: BLE001
        pass


def owner_info():
    """
    讀取 monitor_owner.json，回傳 dict 或 None。**純唯讀顯示 helper**。

    鐵則（Phase 4.5）：
      - 只讀不寫：不建立、不修復、不刪除 owner file
      - 不觸碰 Mutex，不改變 _own 的任何欄位
      - 任何失敗（不存在／損壞／非 dict／權限）一律回 None → UI 降級顯示「資訊不可用」
      - **絕不可**用它的 pid 或存在與否推定 Ownership —— 是否 Owner 永遠只看
        is_owner()（Windows Mutex）。PID 會被回收、檔案會殘留。
    """
    try:
        st = _load_json(_owner_file_path())
        return st if isinstance(st, dict) else None
    except Exception:                                     # noqa: BLE001
        return None


def _release_ownership_atexit():
    try:
        release_ownership()
    except Exception:                                     # noqa: BLE001
        pass


# ⚠️ atexit 為 **LIFO**（後註冊者先執行）。這裡必須先註冊 release、
#    稍後才註冊 _atexit_guard，退出時才會是：
#        _atexit_guard()（標記 paused，此時仍是 Owner 才寫得進去）
#     →  _release_ownership_atexit()（釋放 Mutex）
#    順序寫反的話，guard 會因為已非 Owner 而被擋下 → 孤兒保底靜默失效。
atexit.register(_release_ownership_atexit)
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
def _report_output_root():
    return os.path.join(OUTPUT_DIR, CDR_CFG.OUTPUT_SUBDIR)
def _report_client(quiet=False):
    """
    共用已登入的 ApiClient（輪詢、報告 Session、監看層共用）。
    登入成功後快取於 _report["client"]，之後每次呼叫直接回傳快取，不會重新登入。
    quiet=True：登入失敗時不印訊息（供監看層的靜默重試使用，避免每次重試都洗畫面）。
    失敗回 None。

    ⚠️ Phase 4.6-B：快取**不再是「整個行程只登入一次」**。
       長期常駐的 Service 會遇到 authenticated session 逾期（實測 30 分鐘），
       此時 _check_auth_session() 會把快取清成 None，於是這裡自然重新登入。
       這是**唯一**建立 client 的地方，因此也在此把新 client 接回進行中的 Session。
    """
    if _report["client"] is None:
        c = CDR.ApiClient()
        tok = c.login_hmi(CDR.USERNAME)
        if not tok:
            if not quiet:
                print("[REPORT] 登入失敗，無法進行報告相關作業")
            return None
        _report["client"] = c
        _rebind_session_client(c)      # 進行中的 Session 也換到新 client（不中斷 Session）
    return _report["client"]
# ======================================================================
# authenticated session 失效偵測與復原（Phase 4.6-B）
# ======================================================================
# 問題：_report_client() 整個行程只登入一次。互動式 Dashboard 沒事，但
#       Windows Service 是長期常駐 —— 實測 Service 於 11:03:15 登入，
#       11:33:15（整整 30:00）起 getRunMode / getScheduleSwitch 開始回 None，
#       之後約 100 分鐘、598 筆監看紀錄完全沒有恢復。
#       同一時間 guest 端點（SOC / 功率 / 故障旗標）全部正常，另外新登入的
#       client 也讀得到 smart / True → 設備正常，是**這個長壽 client 的
#       登入態失效了，而且沒有任何人會去清掉它**。
#       後果不是顯示問題：auto_schedule_check 的條件 A 要求 mode_code == "smart"，
#       讀不到就永遠是 "unknown" → Service 再也不可能自動建立報告。
#
# 修法（Monitor 層，最小範圍）：偵測到「authenticated 端點全失敗、但 guest 端點
#       仍正常」連續達門檻 → 丟棄快取 client → 由**既有**的 _monitor_client()
#       重新登入恢復。不新增第二套登入狀態機。
#
# ⚠️ 刻意**不**用 `mode_code == "unknown"` 當判定依據 —— 那是衍生症狀，
#    真的把智慧模式關掉、或舊版 reading，都可能出現類似值。
#    一律以 read_all() 原始的失敗紀錄 reading["_fail"] 為準。

# 端點分類一律引用 CDR 的 EP_* 常數（唯一定義處），不另外抄字串。
#   authenticated：Monitor 判定智慧模式／排程主開關所依賴的兩支
#   guest        ：/hmiGuest/unauthorizedAccess/... 免登入，用來證明「網路與設備都還活著」
AUTH_ENDPOINTS = (CDR.EP_RUNMODE, CDR.EP_SCHEDULE)
GUEST_ENDPOINTS = (CDR.EP_MAINCTL, CDR.EP_PCS, CDR.EP_RACKEXT,
                   CDR.EP_RACKCAP, CDR.EP_ALARM)
# 去抖動：連續幾輪符合特徵才重新登入。避免單次網路瞬斷造成 login storm。
# Service 監看間隔 10s → 2 輪約 20 秒內恢復；相較「失效後永遠不恢復」安全得多。
AUTH_LOST_STREAK = 2


def _ep_failed(fails, path):
    """
    該端點是否出現在 read_all 的失敗清單中。

    _fail 的元素有兩種形式（見 CDR.read_all._get）：
      "<path>"                  取回 None
      "<path>:<ExceptionType>"  丟出例外（此時同一個 path 會被記兩次）
    """
    p = str(path)
    for item in fails or ():
        s = str(item)
        if s == p or s.startswith(p + ":"):
            return True
    return False


def _auth_session_lost(reading):
    """
    判斷是否「高度懷疑 authenticated session 已失效」。**純判斷，無副作用**。

    成立條件（兩者同時）：
      ① 兩支 authenticated 端點在**同一輪**全部失敗
      ② 至少一支 guest 端點在同一輪成功
    → API 可通、設備可通，但需要登入的端點讀不到 = 登入態問題。

    回傳 (bool, 診斷字串)。reading 不含 _fail（舊版）時一律回 False —— 不臆測。

    診斷字串（成立時）帶上**實際命中的 authenticated 失敗條目**，例如：
        authed_all_failed[/client/.../getRunMode:_error1002,
                          /schedule/.../getScheduleSwitch:_error1002], guest_ok=5/5

    ⚠️ 這純粹是 observability：實機觀察時若只印 "authed_all_failed"，就無法知道
       真正造成失敗的 application code，Step 4 取證會缺一塊。帶上 _fail 條目後，
       只讀 Service log 就能取得 code，不必再對設備做任何額外 probe。
    ⚠️ 只取 AUTH_ENDPOINTS 命中的條目，且每支端點最多一筆：
       _fail 的元素依構造只有「端點 + code」或「端點 + 例外型別」
       （見 CDR.read_all._get），因此**不可能**夾帶 msg / _raw / body /
       token / cookie / password / Authorization。
       guest 端點的失敗詳情一律不放進來（只回報成功支數）。
    ⚠️ **判定條件、門檻與流程完全未變** —— 本次只改診斷字串內容。
    """
    if not isinstance(reading, dict):
        return False, "no_reading"
    fails = reading.get("_fail")
    if fails is None:
        return False, "no_fail_field"
    auth_bad = [p for p in AUTH_ENDPOINTS if _ep_failed(fails, p)]
    guest_ok = [p for p in GUEST_ENDPOINTS if not _ep_failed(fails, p)]
    if len(auth_bad) < len(AUTH_ENDPOINTS):
        return False, f"authed_failed={len(auth_bad)}/{len(AUTH_ENDPOINTS)}"
    if not guest_ok:
        # guest 也一起掛 → 是網路／設備問題，不是登入態 → 不得誤判成 auth expiry
        return False, "guest_also_down"
    detail = ",".join(_auth_fail_entries(fails))
    return True, (f"authed_all_failed[{detail}], "
                  f"guest_ok={len(guest_ok)}/{len(GUEST_ENDPOINTS)}")


def _auth_fail_entries(fails):
    """
    取出 AUTH_ENDPOINTS 在 _fail 中實際命中的條目（每支端點最多一筆）。

    刻意**不**直接 join 整個 _fail —— 那會把 guest 端點的失敗詳情一起帶出來，
    也讓輸出長度不可控。此處只取 authenticated 端點，且保留 _fail 原本的
    metadata 形式（"<path>" / "<path>:<ExceptionType>" / "<path>:_error<code>"）。
    """
    out = []
    for ep in AUTH_ENDPOINTS:
        for item in fails or ():
            s = str(item)
            if s == ep or s.startswith(ep + ":"):
                out.append(s)
                break
    return out


def _invalidate_report_client(reason):
    """
    丟棄快取的已登入 client，讓下一輪 _monitor_client() 重新登入。

    ⚠️ **只**碰 client 快取：
       不碰 Mutex / Ownership、不建立或收尾 Session、不動 report folder、
       不重啟取樣執行緒、不釋放所有權。
       API session 復原與 ReportSession 生命週期是兩件**獨立**的事。

    ⚠️ 刻意不在這裡自己寫一套登入流程：清掉快取後，下一輪 _monitor_client()
       會走既有路徑 —— 成功即恢復；失敗則沿用既有的「暫停 + 每 60s 靜默重試 +
       恢復時印一行」機制（那條路徑早已有測試涵蓋）。

    進行中的 Session 另外處理：ReportSession 在建構時就把 client 存成 self.client，
    因此**只清 _report["client"] 不會讓取樣執行緒換到新 client**，它會繼續拿著
    失效的舊 client 取樣。故此處在 _CLIENT_LOCK 保護下一併把新 client 指過去
    （只換 client 參照，不碰 session 狀態、不碰執行緒、不重建任何東西）。
    """
    _report["client"] = None
    _auto["auth_fail_streak"] = 0
    print(f"[AUTO] authenticated API 連續失敗，判定登入態可能失效（{reason}）")
    print("[AUTO] 清除 cached client，下一輪重新登入")


def _rebind_session_client(new_client):
    """
    把進行中 Session 的 client 換成重新登入後的新 client。

    只改 sess.client 這一個參照 —— 不 finalize、不 pause、不建立新 Session、
    不建立資料夾、不重啟取樣執行緒、不碰 Ownership。
    在 _CLIENT_LOCK 內進行，確保不會與背景取樣的 sample_once() 交錯。
    回傳是否確實換過（無進行中 Session 或 client 未變 → False）。
    """
    if new_client is None:
        return False
    with _CLIENT_LOCK:
        sess = _report.get("session")
        if sess is None or getattr(sess, "client", None) is new_client:
            return False
        sess.client = new_client
    print("[AUTO] 進行中的報告 Session 已改用重新登入後的 client（Session 未中斷）")
    return True


def _check_auth_session(reading):
    """
    每輪監看取樣後呼叫：偵測登入態失效並在連續達門檻時丟棄 client。
    回傳本輪是否執行了 invalidate。
    """
    lost, why = _auth_session_lost(reading)
    if not lost:
        _auto["auth_fail_streak"] = 0
        return False
    _auto["auth_fail_streak"] += 1
    n = _auto["auth_fail_streak"]
    if n < AUTH_LOST_STREAK:
        _auto_note(f"[AUTO] authenticated API 讀取失敗（{n}/{AUTH_LOST_STREAK} 輪，"
                   f"{why}）→ 先觀察，未重新登入", key="last_auth_note")
        return False
    _invalidate_report_client(f"連續 {n} 輪；{why}")
    return True


def _read_device_state(client):
    """一次唯讀取樣（供輪詢方向/狀態）。錯誤印出、不靜默；回傳 reading dict 或 None。
    ⚠️ read_all 只做資料讀取；**不得**在此加入任何建立/停止報告的副作用（見 auto_schedule_check）。
    ⚠️ _check_auth_session 只影響「共用 client 的登入態」，不屬於報告副作用：
       它不會建立/續接/收尾任何 Session，也不碰 Ownership。"""
    try:
        with _CLIENT_LOCK:                       # 共用 client：與背景取樣互斥
            reading = CDR.read_all(client)
    except Exception as e:
        print(f"[REPORT] 讀取設備狀態失敗：{e}")
        return None
    _check_auth_session(reading)
    return reading
# ======================================================================
# Phase 6.6：Direction-driven Auto Report Trigger（R1）
# ======================================================================
# 🔴 **注入式，預設關閉** —— `_control_session_provider = None` 時，
#    本檔行為與 Phase 6 導入前**完全相同**（既有 A/B 條件原封不動生效）。
#
# 為什麼需要它：既有自動報告的建立條件是「智慧模式 ＋ 排程主開關開」，
# 而 Phase 6 自動控制的前提**恰好相反**（排程 ON 時 Control Authority 一律
# 判為 EXTERNAL，Phase 6 不介入）。兩者互斥 → Phase 6 在控時報告永遠不會開始。
# R1 讓報告改由「**實際控制方向與擁有權**」驅動，取代「排程開關」這個條件。
#
# 🔴 責任分離（不得違反）
#    · 本檔**不** import 任何 Phase 6 模組；只接受一個唯讀的狀態提供者。
#    · 提供者**只回報狀態**，不得、也無法讓報告層產生任何控制決策。
#    · 報告失敗不得回頭改寫 Decision / Safety Gate / Control Authority。
#
# 提供者契約：zero-arg callable → None 或
#    {"owned": bool, "action": "charge"|"discharge", "target_power_kw": float|None}
_control_session_provider = None

# 由 R1 觸發時的來源標記（與既有 origin=manual/scheduler 並列）
ORIGIN_PHASE6 = "phase6"


def set_control_session_provider(fn):
    """
    注入 Phase 6 控制狀態提供者。傳 None 即完全還原既有行為。

    ⚠️ 刻意做成注入而非 import —— 報告層不得相依控制層。
    """
    global _control_session_provider
    _control_session_provider = fn


def clear_control_session_provider():
    set_control_session_provider(None)


def _control_session_state():
    """
    取得 Phase 6 目前的控制擁有權狀態。**任何例外一律視為「沒有」**：
    報告層的問題不得影響控制層，控制層的問題也不得讓報告層當掉。
    """
    fn = _control_session_provider
    if fn is None:
        return None
    try:
        st = fn()
    except Exception as e:                    # noqa: BLE001 —— 邊界，刻意吞
        print(f"[AUTO] 控制狀態提供者例外（忽略，退回既有條件）：{e}")
        return None
    if not isinstance(st, dict) or st.get("owned") is not True:
        return None
    if st.get("action") not in ("charge", "discharge"):
        return None
    return st


def _start_gate(mode_code, sched_on, direction):
    """
    是否允許自動建立報告。回傳 (ok, reason, origin, setpoint)。

    兩條互斥的路徑，**任一成立即可**：
      既有路徑：智慧模式 ＋ 排程主開關開            → origin=scheduler
      R1 路徑 ：Phase 6 擁有該作業且方向一致        → origin=phase6

    🔴 R1 路徑額外要求「Phase 6 宣稱的方向」與「設備實際方向」一致 ——
       兩者不符代表擁有權有疑義，此時**不建立報告**（Fail Closed）。
    """
    st = _control_session_state()
    if st is not None:
        if st.get("action") != direction:
            _auto_note(f"[AUTO] Phase 6 宣稱方向為 {st.get('action')}，"
                       f"但設備實際方向為 {direction} → 不建立報告")
            return False, "phase6_direction_mismatch", None, None
        return True, "phase6_owned", ORIGIN_PHASE6, st.get("target_power_kw")
    if mode_code != "smart":
        _auto_note(f"[AUTO] 控制模式非智慧模式（{mode_code}）→ 不自動建立報告")
        return False, f"control_mode={mode_code}", None, None
    if not sched_on:
        _auto_note("[AUTO] 排程主開關未開啟 → 不自動建立報告")
        return False, "schedule_off", None, None
    return True, "scheduler", "scheduler", None


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
        # Phase 3.7：_CLIENT_LOCK 改為**注入**給 finalize()，由它只套用在網路階段
        # （_prepare_finalize：read_all + session_end Cell 快照，<1s）。
        # Excel / CSV / Chart / Meter 產出（數秒）不再持鎖 → 不會卡住前景 Dashboard 讀取。
        # 安全前提（已實查）：兩條 finalize 路徑都已先停止背景取樣 ——
        #   _stop_and_finalize() 先 set stop event 並 join thread 才呼叫本函式；
        #   _auto_finalize_from_loop() 本身就跑在背景取樣迴圈內。
        # 故輸出期間不會有人改動 sess.samples。
        return sess, sess.finalize(end_reason, client_lock=_CLIENT_LOCK)
    except Exception as e:
        # 例外安全：finalized 旗標在嘗試前就已設 True，若不還原，之後用 Menu 19 重試會被
        # idempotent 檢查擋掉 → 整份 Session 永遠無法補產報告（例如 Excel 被開啟鎖住時）。
        # 自動收尾是無人值守的，這種情況更需要能重試，故失敗時還原旗標。
        with _report["lock"]:
            _report["finalized"] = False
        print(f"[錯誤] 產生報告失敗：{e}")
        print("       Session 仍為 recording、資料完整保留，可用 Menu 19 重試產生報告。")
        return sess, None
def report_start(mode_label="交流有功", setpoint=None):
    """Menu 18：開始/續接充放電報告（背景記錄；方向由 PCS 旗標自動判定）。
    若磁碟上有未完成 Session（recording/paused）→ 續接同一資料夾累積；否則建立新 Session。

    mode_label / setpoint 由 UI 層收集後傳入（皆有預設值，維持無參數呼叫相容性）。
    Phase 4.5 起本函式與其實作皆不含 input()。"""
    if not _REPORT_AVAILABLE:
        print(f"[錯誤] 無法載入報告模組：{_REPORT_IMPORT_ERR}")
        return
    # 與智慧排程監看層共用同一把 _SESSION_LOCK 與同一份 _report 狀態 → Session 全程唯一
    with _SESSION_LOCK:
        _report_start_locked(mode_label, setpoint)
def _report_start_locked(mode_label="交流有功", setpoint=None):
    """
    report_start() 的實際內容（呼叫端必須已持有 _SESSION_LOCK）。

    ⚠️ Phase 4.5：控制方式與設定值一律由呼叫端以參數傳入。
       本模組（Monitor Core）**不得有任何 input()** —— Core 會被
       auto_monitor_service.py 使用，而 Windows Service（Phase 4.6）沒有 console，
       任何 input() 都會讓服務直接卡死。互動輸入一律留在 device_control_menu 的 UI 層。
    """
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
        # 建立新 Session（控制方式/設定值一律由呼叫端傳入）
        mode = mode_label or "交流有功"
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
def _last_active_direction(sess):
    """由 samples 反向找出最後一次非 idle 的方向（供 auto_charge_stop / auto_discharge_stop 命名）。"""
    for row in reversed(getattr(sess, "samples", None) or []):
        d = row.get("charge_discharge_direction")
        if d in ("charge", "discharge"):
            return d
    return None
def _sync_stop_pending(sess):
    """
    把 idle 累積狀態反映到 _auto（供 Dashboard 顯示）。
    **只反映狀態、不做任何停止判定**（判定一律在 should_auto_end()）。
    """
    held = sess.idle_held_seconds()
    with _SESSION_LOCK:                              # 瞬時：只寫一個 key
        if _report["stopping"]:
            return
        if held > 0 and _auto["state"] == AUTO_RUNNING:
            _auto["state"] = AUTO_STOP_PENDING       # idle 累積中 → 等待收尾
        elif held <= 0 and _auto["state"] == AUTO_STOP_PENDING:
            _auto["state"] = AUTO_RUNNING            # 方向回充/放 → 取消
def _sync_transition_wait(sess, reason, sched_ctx):
    """
    排程銜接等待（Phase 3.8）：把 should_auto_end() 的 "schedule_continuity_wait" 反映成
    **一次** schedule_transition_wait 事件。

    **只記錄事件、不做任何停止判定**（判定一律在 should_auto_end()）。
    latch：同一次 transition 只寫一筆 —— 背景取樣器每 5s 一輪，不設 latch 會在等待期間
           寫進數十筆重複事件，把 events.csv 洗掉。
    清除時機：idle 歸零（設備真正進入下一段 charge/discharge）→ 下一次 transition 可再記一筆。
    事件欄位沿用既有 schema（type/level/message），**不為此功能擴充 events 結構**。
    """
    if sess.idle_held_seconds() <= 0:                # 已回到充/放電 → 解除 latch
        _auto["transition_wait_latch"] = False
        return
    if reason != "schedule_continuity_wait" or _auto.get("transition_wait_latch"):
        return
    ctx = sched_ctx if isinstance(sched_ctx, dict) else {}
    prev_d = _last_active_direction(sess) or "unknown"
    # Case B（排程已開始）看 current_plan_cd；Case A（尚未開始）看 next_plan_cd
    next_d = ctx.get("current_plan_cd") if ctx.get("in_window") else None
    next_d = next_d or ctx.get("next_plan_cd") or "unknown"
    _auto["transition_wait_latch"] = True
    sess.log_event("schedule_transition_wait", "info",
                   f"origin=scheduler｜相鄰排程銜接中，暫緩收尾以維持同一份報告｜"
                   f"{prev_d} -> {next_d}｜plan={ctx.get('current_plan') or 'next'}｜"
                   f"idle 持續 {sess.idle_held_seconds():.0f}s")
def _auto_finalize_from_loop(sess, reason):
    """
    背景取樣執行緒內的自動收尾。回傳 True=可結束迴圈；False=收尾失敗（Session 保留供重試）。
    ⚠️ 這裡**不可**呼叫 _stop_and_finalize()：那會 join 自己所在的執行緒 → 死鎖。
       一律走既有唯一入口 _report_finalize(reason)，不新增第二套 finalize 邏輯。
    """
    with _SESSION_LOCK:
        _report["stopping"] = True                   # 收尾期間擋掉任何新 Session（條件 F）
        _auto["state"] = AUTO_STOP_PENDING
    ok = False
    try:
        # 正常結束（auto_stop）額外記錄 auto_charge_stop / auto_discharge_stop；
        # 故障/通訊異常沿用既有 session_end + stop_reasons，不另外加事件。
        if reason == "auto_stop":
            d = _last_active_direction(sess)
            ev = {"charge": "auto_charge_stop",
                  "discharge": "auto_discharge_stop"}.get(d, "auto_stop")
            try:
                sess.log_event(ev, "info",
                               f"origin=scheduler｜排程結束自動收尾｜"
                               f"idle 持續 {sess.idle_held_seconds():.0f}s｜"
                               f"最後方向 {d or 'unknown'}")
            except Exception as e:
                print(f"[REPORT] 記錄自動停止事件失敗：{e}")
        s, stats = _report_finalize(reason)
        if s is None:                                # 已由其他路徑收尾（idempotent）
            ok = True
        elif stats is None:
            print(f"[REPORT] 自動收尾失敗（{reason}）→ Session 仍為 recording，"
                  f"資料完整保留，可用 Menu 19 重試產生報告")
        else:
            print(f"\n[REPORT] 已自動結束並產生報告"
                  f"（{reason} / {CDR_CFG.END_REASON.get(reason, reason)}）")
            _print_report_result(s, stats)
            ok = True
    except Exception as e:
        print(f"[REPORT] 自動收尾例外（{reason}）：{e}；Session 保留，可用 Menu 19 重試")
    finally:
        with _SESSION_LOCK:
            if ok:                                   # 成功才清空；失敗保留供 Menu 19 重試
                _report.update(session=None, thread=None, stop=None,
                               pending_end_reason=None)
                _auto_clear_notes()              # 回到 IDLE → 統一清除所有去重狀態
                _auto_reset_tracking()
                # 啟動冷卻：期間一律不得建立新 Session（見 CFG.AUTO_COOLDOWN_SEC 說明）
                _cd_sec = _auto_start_cooldown()
                _auto["state"] = AUTO_COOLDOWN if _cd_sec > 0 else AUTO_IDLE
                if _cd_sec > 0:
                    print(f"[AUTO] 進入收尾後冷卻 {_cd_sec}s"
                          f"（期間不建立新報告，避免旗標抖動造成重複建立）")
            _report["stopping"] = False
    return True if ok else False
def _report_bg_start(sess):
    """
    啟動背景取樣執行緒：持續取樣（合併 Session）並負責 Session 的**生命週期結束**。
      - charge/idle/discharge 皆為同一 Session 內的有效狀態；充↔放切換之間的短暫 idle
        由 should_auto_end() 的 idle debounce（AUTO_HOLD_STOP_SEC）吸收，不會被切成兩份。
      - **停止判定一律委派 sess.should_auto_end()**（全專案唯一決策點）；本檔不得再自行
        判斷 fault / communication_error / battery_off / idle / timeout。
      - 停止交給背景取樣器（而非 Dashboard 監看層）的理由：週期較短（5s），且使用者進入
        控制選單期間仍持續運作 —— 否則排程結束時若正在控制選單就永遠不會收尾。
      - 方向於相鄰取樣間改變時，由 ReportSession.sample_once 記錄 charge/idle/discharge_start 事件。

    ⚠️ **Writer Gate（Phase 4.4）**：本函式是全專案唯一「啟動 Session 寫入者」的收斂點。
       四個入口 —— report_resume_on_launch / auto_schedule_check→_report_start_session /
       report_start→_report_start_locked / report_on_charge_discharge_control ——
       全部經過這裡。守住此處即涵蓋全部，日後新增入口也自動受保護。
       回傳 True=已啟動取樣；False=非 Owner，未啟動（呼叫端需容忍 False）。
    """
    if not is_owner():
        print("[REPORT] 非 Monitor Owner → 拒絕啟動取樣執行緒"
              "（防止同一 Session 被兩個行程同時寫入）")
        return False
    stop = threading.Event()
    _report.update(session=sess, thread=None, stop=stop, finalized=False)
    # 新 Session 起始（手動 / 排程 / resume 都會經過這裡）→ 清除上一份 Session 遺留的
    # 去重狀態，確保各類提示（含停止提示）在新 Session 會重新各印一次。
    _auto_clear_notes()

    def _loop():
        try:
            while not stop.is_set():
                try:
                    with _CLIENT_LOCK:           # 共用 client：與前景監看/輪詢互斥
                        sess.sample_once()
                except Exception as e:
                    print(f"[REPORT] 背景取樣例外：{e}")

                # ---- 排程感知（Phase 3.5）：只在 idle 開始累積時才查，並快取 60s ----
                #      本層只把 ctx 算出來，判定仍在 should_auto_end()。
                #      取得失敗 → ctx=None/source=unknown → 退回 Phase 3.4 行為。
                sched_ctx = None
                try:
                    if sess.idle_held_seconds() > 0:
                        sched_ctx = _schedule_window_ctx(_report["client"])
                except Exception as e:
                    print(f"[REPORT] 排程窗口查詢例外（退回純 idle 門檻）：{e}")

                # ---- 停止判定：唯一決策點；不依賴本輪取樣結果（讀累積狀態）----
                end, reason = False, None
                try:
                    end, reason = sess.should_auto_end(schedule_ctx=sched_ctx)
                except Exception as e:
                    print(f"[REPORT] 停止判定例外（本輪略過，繼續記錄）：{e}")
                try:
                    _sync_stop_pending(sess)     # 只反映 STOP_PENDING，不參與判定
                except Exception as e:
                    print(f"[REPORT] 狀態同步例外：{e}")
                try:
                    _sync_transition_wait(sess, reason, sched_ctx)   # 只記事件，不參與判定
                except Exception as e:
                    print(f"[REPORT] 排程銜接事件記錄例外：{e}")

                if end:
                    # 使用者已按 Menu 7 → 尊重其結束原因，不被 auto_stop 蓋掉
                    if _report["pending_end_reason"]:
                        reason = _report["pending_end_reason"]
                    if not getattr(CDR_CFG, "AUTO_STOP_ENABLED", True):
                        # 用**獨立** key：背景取樣器(5s) 與 Dashboard(15s) 是兩個節奏，
                        # 共用 last_note 會互相覆寫而讓去重失效 → 同一條件反覆洗版。
                        _auto_note(f"[REPORT] 自動停止條件成立（{reason}）但 "
                                   f"AUTO_STOP_ENABLED=False → 僅記錄，不收尾"
                                   f"（請以 Menu 19 人工結束）",
                                   key="last_stop_note")
                        stop.wait(CDR_CFG.SAMPLE_INTERVAL_SEC)
                        continue
                    _auto_finalize_from_loop(sess, reason)
                    break                        # 成功或失敗都結束迴圈（失敗時 Session 保留）
                stop.wait(CDR_CFG.SAMPLE_INTERVAL_SEC)
        except Exception as e:                   # 最外層防護：背景執行緒不得靜默死亡
            print(f"[REPORT] 背景執行緒異常結束：{e}")

    th = threading.Thread(target=_loop, daemon=True)
    _report["thread"] = th
    th.start()
    return True
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
            _auto_clear_notes()                  # 回到 IDLE → 統一清除所有去重狀態
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
            _auto["state"] = AUTO_IDLE
            _auto_clear_notes()                  # 回到 IDLE → 統一清除所有去重狀態
            _auto_reset_tracking()
def _atexit_guard():
    """
    行程結束時的**保底**：仍有進行中的 Session → 標記為 paused（可續接）。

    為什麼需要：`report_pause()` 只在 main() 的 finally 被呼叫。任何「import 了本模組、
    建立了 Session、但沒走 main()」的行程（驗證腳本、工具、未來的 Auto Monitor Service）
    正常退出時，背景 daemon thread 會被直接終止，session_state.json 永遠停在 recording
    → 產生孤兒。2026-08-04 兩次孤兒皆為此成因（都是**正常**退出，非當機）。

    ⚠️ 本函式的鐵則（atexit 期間不可拖慢或阻斷行程退出）：
        - **不做任何網路 I/O**（不取樣、不讀 Cell/告警、不呼叫 finalize）
        - **不重產報表** —— 故用 mark_paused() 而非 pause()：
          pause() 會 _regenerate_outputs()，重寫 summary/statistics/alarms 與整份 Excel，需數秒
        - join 背景執行緒有硬上限（CFG.ATEXIT_JOIN_TIMEOUT_SEC）
        - 全程 try/except，**任何情況都不得拋出例外**
        - idempotent：main() 的 finally 已收尾時 _report["session"] 為 None → 直接返回

    ⚠️ Phase 4.4：非 Owner 不得動 Session（它的 _report["session"] 本來就是 None，
       但明確擋下可避免日後有人在 Observer 路徑塞入 session 參考時誤觸發）。
       本 guard **必須在 release_ownership 之前執行** —— 見模組上方 atexit LIFO 說明。
    """
    try:
        if not is_owner():
            return
        sess = _report.get("session")
        if sess is None or _report.get("finalized"):
            return
        ev = _report.get("stop")
        if ev is not None:
            ev.set()                                     # 讓背景取樣停止
        th = _report.get("thread")
        if th is not None and th.is_alive():
            th.join(timeout=getattr(CDR_CFG, "ATEXIT_JOIN_TIMEOUT_SEC", 2))
        mark = getattr(sess, "mark_paused", None)        # 測試替身可能沒有此方法
        if callable(mark):
            mark()
            print(f"[REPORT] 行程結束保底：Session 已標記為 paused（下次啟動可續接）："
                  f"{getattr(sess, 'session_id', '?')}")
    except Exception:
        pass                                             # atexit 絕不拋例外
atexit.register(_atexit_guard)
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
    # ⚠️ **Authoritative Writer Gate（Phase 4.6-B）** —— 必須在任何落盤行為之前。
    #    ReportSession(...).start() 會建立資料夾並寫入 session_state.json(status=recording)、
    #    events.csv、samples.csv、cell_snapshots.json。若只靠 _report_bg_start() 的 Gate，
    #    非 Owner 已經先把整套 Session 結構落盤，留下無人擁有的 recording 孤兒
    #    —— 與「Observer 不建立 Session」及 Phase 3.6「不再產生孤兒」直接衝突。
    #    此處是唯一能保證「不落盤」的位置：早於 ReportSession()、早於 _SESSION_LOCK。
    if not is_owner():
        print("[REPORT] 非 Monitor Owner → 不建立 Session（防止產生無人擁有的資料夾）")
        return None, False

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
def _orphan_gap_seconds(folder):
    """
    由 session_state.json 的 last_sample_time 算出「斷線間隔」秒數。
    ⚠️ 用**系統時間**（非 monotonic）—— 這是跨行程的比較，monotonic 值跨行程無意義。
    無法判定（欄位缺失／格式非預期）回 None。
    """
    st = _load_json(os.path.join(folder, CDR_CFG.FILE_SESSION_STATE)) or {}
    last = st.get("last_sample_time")
    if not last:
        return None
    try:
        return (datetime.now()
                - datetime.strptime(str(last), "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (ValueError, TypeError):
        return None
def report_resume_on_launch():
    """
    程式啟動時：若磁碟有未完成 Session（recording/paused）→ 恢復並繼續背景取樣（不重複建立）。

    Phase 3.6 新增「孤兒分類」：以斷線間隔判斷是否為行程異常中斷所遺留，
    **只記錄事件與提示，不改變 Resume 流程**（超過門檻仍照常續接）。
    是否要對孤兒做更積極的處置（如直接 finalize），待累積更多實機案例後再決定。

    ⚠️ Phase 4.4：這是跨行程雙寫的**直接入口** —— find_active_session() 只看 status，
       第二個行程啟動時會 resume 同一份 recording Session 並另起取樣執行緒。
       故非 Owner 一律不得 resume。與 _report_bg_start() 的 Writer Gate 形成雙層防護。
    """
    if not _REPORT_AVAILABLE:
        return
    if not is_owner():
        print("[REPORT] 非 Monitor Owner → 不續接未完成 Session（避免與 Owner 雙寫）")
        return
    active = CDR.find_active_session(_report_output_root())
    if not active:
        return
    gap = _orphan_gap_seconds(active)            # 需在 resume 覆寫 state 前先讀
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
    # 孤兒分類：僅記錄，不改變流程
    _gap_limit = getattr(CDR_CFG, "ORPHAN_GAP_SEC", 0) or 0
    if gap is not None and _gap_limit > 0 and gap > _gap_limit:
        detail = (f"斷線間隔 {gap:.0f}s（門檻 {_gap_limit}s）｜"
                  f"上次取樣後行程未正常收尾即中斷｜本次照常續接，未改變流程")
        try:
            sess.log_event("orphan_detected", "warning", detail)
        except Exception as e:
            print(f"[REPORT] 記錄 orphan_detected 失敗：{e}")
        print(f"[REPORT] ⚠ 偵測到孤兒 Session：{detail}")
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
# ⚠️ Auto Start 目前依賴本刷新間隔：排程開始後最久要等這麼久才會建立 Session。
#    設太大會漏掉排程開頭的資料（曾誤設 60s）。維持 15s。
AUTO_REFRESH_SEC = 15            # Dashboard 前景自動刷新間隔（秒）
AUTO_LOGIN_RETRY_SEC = 60        # 登入失敗後的靜默重試間隔（秒）；按 r 可立即重試
# ======================================================================
# 監看狀態機（Phase 3.4 起固定）
# ----------------------------------------------------------------------
#   IDLE
#     ↓  條件 A~F 全部成立，且方向為 charge/discharge
#   START_HOLD        ← Start Debounce 累積中（held < CFG.AUTO_HOLD_START_SEC）
#     ↓  held ≥ CFG.AUTO_HOLD_START_SEC
#   START_PENDING     ← 鎖內瞬時佔位，外部觀察不到
#     ↓  _report_start_session() 成功
#   RUNNING           ← Session recording，背景取樣器每 5s 取樣
#     ↓  偵測到 idle（背景取樣器 _sync_stop_pending）
#   STOP_PENDING      ← Stop Debounce 累積中（idle < CFG.AUTO_HOLD_STOP_SEC）
#     ↓  should_auto_end() → (True, "auto_stop")
#   AUTO_STOP         ← _report_finalize() 產出 Summary / Excel
#     ↓
#   COOLDOWN          ← 冷卻中，**一律不得建立任何 Session**
#     ↓  冷卻期滿（CFG.AUTO_COOLDOWN_SEC）
#   IDLE
#
# ⚠️ START_HOLD 不是「進去就等」的狀態：每一輪 auto_schedule_check() 都會**重新
#    完整驗證所有守門條件**（F/D/E/A/B/C），任一失效即回 IDLE 並重新累積。
# ⚠️ 所有 debounce/cooldown 計時一律 time.monotonic()，與呼叫頻率無關。
# ======================================================================
(AUTO_IDLE, AUTO_START_HOLD, AUTO_START_PENDING, AUTO_RUNNING,
 AUTO_STOP_PENDING, AUTO_COOLDOWN) = (
    "IDLE", "START_HOLD", "START_PENDING", "RUNNING", "STOP_PENDING", "COOLDOWN")
_auto = {
    "state": AUTO_IDLE,
    "direction": None,          # 目前觀測到的方向（charge/discharge/idle/unknown）
    "since": None,              # **monotonic 秒**：該方向首次被觀測的時間點
    "held_sec": 0.0,            # 該方向已持續秒數（monotonic 實際時間，Start Debounce 依據）
    "streak": 0,                # 連續同方向次數（僅輔助資訊，不作觸發依據）
    # 冷卻截止時間（**monotonic 秒**；None = 無冷卻）。auto_stop 收尾成功後設定。
    # 冷卻期間一律不得建立 Session —— 不論方向如何變化、模式或排程是否重開。
    "cooldown_until": None,
    # 排程感知（Phase 3.5）：最近一次算出的 schedule_ctx 與其 monotonic 時間戳
    # ⚠️ sched_ctx **僅供 Dashboard 顯示**，不是快取來源（倒數值不可快取，見 _schedule_window_ctx）。
    "sched_ctx": None,
    "sched_ctx_at": None,
    # 真正的快取單位：排程清單本身（API 結果，很少變動）＋ 取得成功與否 ＋ monotonic 時間戳
    "sched_plans": None,
    "sched_plans_ok": False,
    "sched_plans_at": None,
    # 排程銜接等待（continuity）的 latch：同一次 transition 只記一次 schedule_transition_wait 事件。
    # 真正進入下一段 charge/discharge（idle 歸零）後清除 → 下一次 transition 才能再記一筆。
    "transition_wait_latch": False,
    "template_status": "unknown",   # enabled / none / unknown（僅資訊，**不阻擋**建立）
    # ---- 訊息去重用的「上次已印內容」；三者**必須各自獨立**，不可共用一個 key ----
    # 背景取樣器（5s）與 Dashboard 監看層（15s）是兩個不同節奏的輸出來源，
    # 若共用同一個 key，兩邊會交替覆寫而讓彼此的去重失效 → 同一條件反覆洗版。
    "last_note": None,          # 監看層的判斷訊息（auto_schedule_check）
    "last_start_note": None,    # Start Debounce 累積中的提示（START_HOLD）
    "last_cooldown_note": None,  # 冷卻中的提示（COOLDOWN）
    "last_status": None,        # 監看層的狀態摘要（_monitor_status_line）
    "last_stop_note": None,     # 背景取樣器的停止提示（AUTO_STOP_ENABLED=False 時）
    "last_auth_note": None,     # 登入態失效偵測的觀察中提示（_check_auth_session）
    "disabled": False,          # 監看暫停（登入失敗）；**可恢復**，非永久停用
    "login_failed_at": None,    # monotonic：上次登入失敗時間（用於 60s 靜默重試節流）
    "auth_fail_streak": 0,      # 連續判定「登入態可能失效」的輪數（去抖動；見 _check_auth_session）
    # ↓ 畫面輸出用的暫存旗標（非監看狀態；集中宣告於此，避免散落成動態新增的 key）
    "_pending_nl": False,       # 游標仍停在提示行末端 → 下一行輸出前需補一次換行
    "_no_tty_warned": False,    # 非互動 stdin 的提示只顯示一次
}
# 系統時間（datetime.now）可能被使用者/NTP 調整 → 一律用 time.monotonic 計算持續時間。
_MODE_LABEL_TO_CODE = {"智慧模式": "smart", "手動模式": "manual", "未開啟模式": "none"}
def _auto_reset_tracking():
    """清空方向追蹤（建立/停止 Session 後重新起算持續時間）。"""
    _auto.update(direction=None, since=None, held_sec=0.0, streak=0,
                 transition_wait_latch=False)
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
def _auto_clear_notes():
    """清除所有訊息去重狀態（Session 起訖、監看恢復時呼叫），確保新一輪會重新提示一次。"""
    _auto.update(last_note=None, last_status=None, last_stop_note=None,
                 last_start_note=None, last_cooldown_note=None, last_auth_note=None)
def _auto_cooldown_remaining():
    """
    冷卻剩餘秒數（monotonic）；0.0 = 無冷卻或已期滿。
    與呼叫頻率無關 —— 呼叫得再密集也不會讓冷卻提早結束。
    """
    until = _auto.get("cooldown_until")
    if until is None:
        return 0.0
    return max(0.0, until - time.monotonic())
def _auto_start_cooldown():
    """auto_stop 收尾成功後啟動冷卻（見 CFG.AUTO_COOLDOWN_SEC 的三項理由）。"""
    sec = getattr(CDR_CFG, "AUTO_COOLDOWN_SEC", 0) or 0
    _auto["cooldown_until"] = (time.monotonic() + sec) if sec > 0 else None
    return sec
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
    _auto.update(disabled=False, login_failed_at=None)
    _auto_clear_notes()                          # 監看恢復 → 讓各類提示重新各印一次
    _auto_print("[AUTO] 登入成功 → 自動監看已恢復"
                f"（每 {AUTO_REFRESH_SEC}s 更新 PCS／排程／充放電狀態）")
    return client, True
def _auto_control_mode_code(r):
    """PCS 控制模式機器語意：優先讀 pcs_control_mode_code；舊 reading 才退回中文標籤對照。"""
    code = r.get("pcs_control_mode_code")
    if code:
        return str(code)
    return _MODE_LABEL_TO_CODE.get(str(r.get("pcs_control_mode", "")), "unknown")
def _auto_enabled_plan_state(client):
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
# ======================================================================
# 排程感知（Schedule Context）—— Phase 3.5
# ----------------------------------------------------------------------
# 職責分工：本層只負責「把排程窗口狀態算出來」，**不做任何停止判定**；
#           決策一律留在 ReportSession.should_auto_end(schedule_ctx=...)。
# 時間來源：PC 時間 + CFG.AUTO_WINDOW_GRACE_SEC 寬限（不依賴設備 dataCollectTime）。
# 呼叫時機：僅在 idle 開始累積時查詢，並快取 CFG.AUTO_WINDOW_CACHE_SEC 秒。
# ======================================================================
def _hhmm_to_min(s):
    """'17:00' → 1020（當日分鐘數）；格式非預期回 None。"""
    try:
        h, m = str(s).strip().split(":")[:2]
        h, m = int(h), int(m)
    except (ValueError, AttributeError, IndexError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m
def _plan_applies_on(plan, day):
    """該排程模板是否適用於指定日期（datetime.date）。"""
    if plan["plan_type"] == 1:                       # 每週：executeTime 為 isoweekday 逗號字串
        return str(day.isoweekday()) in plan["days"]
    if plan["plan_type"] == 2:                       # 按日期：executeTime 為 'MM-DD'
        return plan["date"] == day.strftime("%m-%d")
    return False
def fetch_enabled_plans(client, tpl_path, item_path_fmt):
    """
    唯讀取得**已啟用**（enableFlag==1）排程模板底下的所有項目。
    回傳 (plans, ok)：plans 為 list[dict]；ok=False 表示 API 失敗或格式非預期。
    """
    try:
        with _CLIENT_LOCK:
            tpls = client.get(tpl_path)
    except Exception as e:
        print(f"[AUTO] 排程清單讀取失敗（窗口判定退回純 idle 門檻）：{e}")
        return [], False
    if not isinstance(tpls, list):
        return [], False
    plans = []
    for t in tpls:
        if not isinstance(t, dict) or t.get("enableFlag") not in (1, "1"):
            continue
        try:
            with _CLIENT_LOCK:
                items = client.get(item_path_fmt.format(tid=t.get("tempId")))
        except Exception as e:
            print(f"[AUTO] 排程項目讀取失敗（tempId={t.get('tempId')}）：{e}")
            return [], False
        if not isinstance(items, list):
            return [], False
        for it in items:
            if not isinstance(it, dict):
                continue
            s, e = _hhmm_to_min(it.get("startTime")), _hhmm_to_min(it.get("endTime"))
            if s is None or e is None:
                continue
            plans.append({
                "name": t.get("tempName"),
                "plan_type": t.get("plan_type", t.get("planType")),
                "days": [x for x in str(t.get("executeTime") or "").split(",") if x],
                "date": str(t.get("executeTime") or ""),
                "start_min": s, "end_min": e,
                "start_txt": str(it.get("startTime")), "end_txt": str(it.get("endTime")),
                # enum 對照與型別正規化一律用 config 的唯一定義（Phase 3.6 / D-1）
                "cd": CDR_CFG.SCHED_CD_CODE.get(
                    CDR_CFG.as_int(it.get("chargeOrDischarge")), "unknown"),
            })
    return plans, True
def compute_schedule_ctx(plans, now, grace_sec=None, lookahead_days=8):
    """
    **純函式**：由排程清單與現在時間算出 schedule_ctx（無 I/O，供離線測試直接驗證）。

    回傳 {"in_window", "current_plan", "current_plan_started_sec", "current_plan_cd",
          "next_plan_in_sec", "next_plan_cd", "source"}。
      in_window        ：現在是否落在任一啟用排程項目的時段內（含前後 grace 寬限）
      current_plan     ：命中的項目描述（如 "Charge 17:00~17:50 charge"），無則 None
                         ⚠️ **顯示用字串**；判定一律用下面兩個結構化欄位，不得 parse 此字串。
      current_plan_started_sec：now - 命中項目的 nominal start（秒）。
                         正值＝已開始多久；**負值＝尚未開始**（落在前緣 grace 內）；無命中則 None。
      current_plan_cd  ：命中項目的方向（charge / discharge / none / unknown），無命中則 None
      next_plan_in_sec ：距下一段排程開始的秒數；未來 lookahead_days 內查無則 None
      next_plan_cd     ：該下一段排程的方向；查無則 None
      source           ："ok"

    命中多筆時取「**nominal start 最新**」的一筆（不是 API 回傳的第一筆）：
      相鄰排程加上前後 grace 必然重疊 —— 16:00~16:10 的 grace 到 16:12、
      16:11~16:21 的 grace 自 16:09 起，16:09~16:12 之間兩筆同時命中。
      取第一筆會讓結果取決於 API 順序；取 start 最新者才會命中「剛開始的那一筆」，
      這正是 should_auto_end() 的 continuity Case B 所需要的語意。

    跨午夜（end <= start，如 22:00~02:00）處理：
      該時段橫跨兩天 —— 22:00~24:00 屬「當天」、00:00~02:00 屬「前一天」的排程，
      因此判斷 00:00~02:00 時要看**前一天**是否符合執行日；此時 nominal start 也屬前一天。
    """
    grace = (getattr(CDR_CFG, "AUTO_WINDOW_GRACE_SEC", 0) or 0) if grace_sec is None else grace_sec
    gmin = grace / 60.0
    today = now.date()
    yesterday = today - timedelta(days=1)
    now_min = now.hour * 60 + now.minute + now.second / 60.0

    def _start_dt(p, day):
        return datetime.combine(day, dtime()) + timedelta(minutes=p["start_min"])

    hits = []                                         # [(start_dt, plan)]
    for p in plans:
        s, e = p["start_min"], p["end_min"]
        if e > s:                                     # 一般時段
            if _plan_applies_on(p, today) and (s - gmin) <= now_min < (e + gmin):
                hits.append((_start_dt(p, today), p))
        elif e < s:                                   # 跨午夜
            if _plan_applies_on(p, today) and now_min >= (s - gmin):
                hits.append((_start_dt(p, today), p))      # 22:00~24:00 段：start 屬今天
            elif _plan_applies_on(p, yesterday) and now_min < (e + gmin):
                hits.append((_start_dt(p, yesterday), p))  # 00:00~02:00 段：start 屬昨天
        # e == s → 零長度時段，視為不成立

    in_window = bool(hits)
    current = cur_started = cur_cd = None
    if hits:
        st_dt, p = max(hits, key=lambda x: x[0])      # nominal start 最新的一筆
        current = f"{p['name']} {p['start_txt']}~{p['end_txt']} {p['cd']}"
        cur_started, cur_cd = (now - st_dt).total_seconds(), p["cd"]

    # 下一段排程開始時間：往後掃 lookahead_days 天，取最近且尚未開始者
    next_sec, next_cd = None, None
    for d in range(lookahead_days):
        day = today + timedelta(days=d)
        for p in plans:
            if not _plan_applies_on(p, day):
                continue
            delta = (_start_dt(p, day) - now).total_seconds()
            if delta > 0 and (next_sec is None or delta < next_sec):
                next_sec, next_cd = delta, p["cd"]
        if next_sec is not None and d >= 1:
            break                                     # 已找到且掃過隔天 → 不需再往後
    return {"in_window": in_window, "current_plan": current,
            "current_plan_started_sec": cur_started, "current_plan_cd": cur_cd,
            "next_plan_in_sec": next_sec, "next_plan_cd": next_cd, "source": "ok"}
def _unknown_schedule_ctx():
    """排程無法取得時的 ctx（source="unknown" → should_auto_end 完全退回 Phase 3.4 行為）。"""
    return {"in_window": False, "current_plan": None,
            "current_plan_started_sec": None, "current_plan_cd": None,
            "next_plan_in_sec": None, "next_plan_cd": None, "source": "unknown"}
def _schedule_window_ctx(client, now=None):
    """
    取得 schedule_ctx。失敗回 source="unknown" → should_auto_end 會退回 3.4 行為。

    ⚠️ 快取的是**排程清單（plans）**，不是算出來的 ctx ——
       ctx 內 next_plan_in_sec / current_plan_started_sec 都是「相對現在」的秒數，
       整份快取 60s 會讓倒數值最多慢 60s：門檻 180s 時，實際只剩 170s 的排程可能
       讀到 230s 的舊值而被判成「不連續」→ 提前 finalize。
       改為只快取 API 結果、每次呼叫都以 datetime.now() 重算 → API 呼叫頻率完全不變。
    _auto["sched_ctx"] 仍會寫入最新結果，但**只供 Dashboard 顯示**，不再作為快取來源。
    """
    # client 不可用 → 一律 unknown，**不吃快取**：連線都沒有時，寧可退回純 idle 門檻
    # 正常收尾，也不要靠舊排程資料把 Session 續留下去（失敗方向取保守側）。
    if client is None:
        ctx = _unknown_schedule_ctx()
        _auto.update(sched_ctx=ctx, sched_ctx_at=time.monotonic())
        return ctx
    ttl = getattr(CDR_CFG, "AUTO_WINDOW_CACHE_SEC", 0) or 0
    cached_at = _auto.get("sched_plans_at")
    if (cached_at is not None and _auto.get("sched_plans") is not None
            and (time.monotonic() - cached_at) < ttl):
        plans, ok = _auto["sched_plans"], _auto["sched_plans_ok"]
    else:
        plans, ok = fetch_enabled_plans(client, _SCHED_TPL_LIST, _SCHED_ITEM_LIST)
        _auto.update(sched_plans=plans, sched_plans_ok=ok, sched_plans_at=time.monotonic())
    # 時間計算一律用「現在」，不吃快取
    ctx = (compute_schedule_ctx(plans, now or datetime.now()) if ok
           else _unknown_schedule_ctx())
    _auto.update(sched_ctx=ctx, sched_ctx_at=time.monotonic())   # 僅供顯示
    return ctx
def _schedule_ctx_text(ctx):
    """schedule_ctx → Debug 顯示字串。"""
    if not isinstance(ctx, dict):
        return ""
    if ctx.get("source") != "ok":
        return " window=unknown"
    if ctx.get("in_window"):
        return f" window=in({ctx.get('current_plan')})"
    nxt = ctx.get("next_plan_in_sec")
    return f" window=out next={'none' if nxt is None else f'{nxt:.0f}s'}"
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
    # Defense in depth（Phase 4.6-B）：非 Owner 完全不進入自動排程的 writer 流程。
    # 省掉每輪的排程判定與 API 查詢，也讓 Observer 的意圖在最上層就明確。
    # ⚠️ 不影響 Dashboard 的唯讀設備狀態更新 —— dashboard_refresh 已在呼叫本函式**之前**
    #    完成 _read_device_state() 與 _monitor_status_line()。
    if not is_owner():
        return "skipped", "not_owner"
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
        # START_HOLD 是**每輪重新推導**的顯示狀態，先歸零再依本輪條件重新決定，
        # 確保任一守門條件失效時會確實回到 IDLE（不會卡在 START_HOLD）。
        if _report["session"] is None:
            if _auto["state"] in (AUTO_RUNNING, AUTO_START_HOLD):
                _auto["state"] = AUTO_IDLE          # 報告已結束／重新推導 START_HOLD
        elif _auto["state"] in (AUTO_IDLE, AUTO_START_HOLD, AUTO_START_PENDING):
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
        # COOLDOWN：auto_stop 收尾後的冷卻期，**一律不得建立任何 Session**。
        # 刻意置於 E/A/B/C/G 之前 → 期間即使方向改變（charge↔discharge）、
        # 智慧模式重開、排程重開，也全部等冷卻結束才重新開始判斷。
        _cd = _auto_cooldown_remaining()
        if _cd > 0:
            _auto["state"] = AUTO_COOLDOWN
            _auto_note(f"[AUTO] 自動收尾後冷卻中（剩餘 {_cd:.0f}s / "
                       f"{CDR_CFG.AUTO_COOLDOWN_SEC}s）→ 期間一律不建立新報告",
                       key="last_cooldown_note")
            return "skipped", f"cooldown={_cd:.0f}s"
        if _auto["state"] == AUTO_COOLDOWN:         # 冷卻剛期滿 → 回到正常判斷
            _auto.update(state=AUTO_IDLE, cooldown_until=None, last_cooldown_note=None)
        # E：PCS 故障 → 不建立（故障停止沿用既有 SafetyMonitor / fault_stop）
        if fault:
            _auto_note("[AUTO] PCS 故障中（pcs_fault_flag）→ 不建立新報告")
            return "skipped", "pcs_fault"
        # A / B：既有「智慧模式＋排程開」條件；R1 起另可由 Phase 6 擁有權驅動。
        # ⚠️ 其餘所有守門（C 方向、D 已有 session、E 故障、F pending、
        #    cooldown、G debounce、owner、主開關）**一項都沒有放寬**。
        _gate_ok, _gate_reason, _origin, _setpoint = _start_gate(
            mode_code, sched_on, direction)
        if not _gate_ok:
            return "skipped", _gate_reason
        # C：尚未實際充/放電（含 idle / unknown）→ 只等待，不建立
        if direction not in ("charge", "discharge"):
            _auto_note(f"[AUTO] 智慧模式＋排程已開，但設備方向為 {direction}"
                       f"（持續 {held:.0f}s）→ 等待實際充/放電")
            return "skipped", f"direction={direction}"

        # ---- A~F 全部成立 ----
        zh = "充電" if direction == "charge" else "放電"
        # G：Start Debounce —— 同方向需**持續**達門檻才建立，濾掉設備旗標瞬時抖動。
        #    held 為 monotonic 實際經過秒數（非取樣次數）→ 與呼叫頻率完全解耦；
        #    未來由 Auto Monitor Service 以更短週期呼叫時，此邏輯一行都不需要改。
        _hold_start = getattr(CDR_CFG, "AUTO_HOLD_START_SEC", 0) or 0
        if held < _hold_start:
            _auto["state"] = AUTO_START_HOLD
            _auto_note(f"[AUTO] 偵測到實際{zh}，去抖動中"
                       f"（持續 {held:.0f}s / {_hold_start}s）→ 尚未建立報告",
                       key="last_start_note")
            return "skipped", f"start_hold={held:.0f}s/{_hold_start}s"
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
        _auto["template_status"] = _auto_enabled_plan_state(client or _report["client"])
        print(f"[AUTO] 排程配置清單：{_auto['template_status']}（僅供資訊，不影響報告建立）")
        ev_type = "auto_charge_start" if direction == "charge" else "auto_discharge_start"
        _origin = _origin or "scheduler"
        # R1：由 Phase 6 驅動時，把控制 action 與 target_power_kw 一併記入
        # （setpoint 沿用既有參數，不擴充 CSV schema）。
        _extra = (f"｜控制來源 Phase 6｜action={direction}"
                  f"｜target_power_kw={_setpoint}") if _origin == ORIGIN_PHASE6 else ""
        sess, _created = _report_start_session(      # 內部自行取得 _SESSION_LOCK（RLock）
            direction,
            r.get("pcs_power_control_mode") or "交流有功",
            _setpoint,
            origin=_origin,
            # detail 開頭寫入 origin=scheduler：讓 events.csv 本身能自證「誰建立了這份報告」，
            # 不必回頭看當時的 console 輸出（origin 與 source 語意不同：
            #   origin=scheduler/manual → 誰建立；source=auto/manual → 哪種刷新觸發）。
            # 僅增加 detail 文字，不改 CSV schema、不增欄位。
            extra_events=[(ev_type, "info",
                           f"origin={_origin}｜自動開始{zh}｜方向持續 {held:.0f}s｜"
                           f"排程配置 {_auto['template_status']}｜刷新來源 {source}"
                           f"{_extra}")],
        )
    finally:
        with _SESSION_LOCK:                          # 瞬時：離開 pending（成功或失敗都要）
            _auto["state"] = AUTO_RUNNING if _report["session"] is not None else AUTO_IDLE
    if sess is None:
        _auto_note("[AUTO] 建立報告失敗（詳見上方訊息）→ 下一輪重試")
        return "skipped", "start_failed"
    _auto_reset_tracking()
    _auto_clear_notes()                              # 已建立 → 解除訊息抑制（含停止提示）
    return "started", direction
def _owner_tag():
    """
    Debug 行用的 owner 標記：self / other / none。

    只依 is_owner()（Mutex）與最近一次 acquire 的 reason 推導 —— 不讀 owner file。
      self  本行程持有
      other 他人持有（reason=held_by_other）
      none  無人／取不到（fail closed，如 no_win32）
    """
    if is_owner():
        return "self"
    return "other" if _own.get("reason") == "held_by_other" else "none"


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
    # 三個倒數擇一顯示，讓現場一眼看出目前卡在哪個階段（皆為顯示，不參與判定）：
    #   有 Session   → idle=XXs/60s      （Stop Debounce，距離自動收尾）
    #   冷卻中       → cooldown=XXs/60s  （收尾後冷卻，期間不建立新報告）
    #   其餘         → hold=XXs/15s      （Start Debounce，距離建立報告）
    idle_txt = ""
    try:
        if sess is not None:
            _h = getattr(CDR_CFG, "AUTO_HOLD_STOP_SEC", 0)
            idle_txt = f" idle={sess.idle_held_seconds():.0f}s/{_h}s"
            # 排程窗口（Phase 3.5）：僅在 idle 累積期間才會有 ctx，其餘時候不顯示
            if sess.idle_held_seconds() > 0:
                idle_txt += _schedule_ctx_text(_auto.get("sched_ctx"))
        else:
            _cd = _auto_cooldown_remaining()
            if _cd > 0:
                _c = getattr(CDR_CFG, "AUTO_COOLDOWN_SEC", 0)
                idle_txt = f" cooldown={_cd:.0f}s/{_c}s"
            else:
                _s = getattr(CDR_CFG, "AUTO_HOLD_START_SEC", 0)
                idle_txt = f" hold={_auto['held_sec']:.0f}s/{_s}s"
    except Exception:
        idle_txt = ""
    return ("[AUTO] "
            f"mode={_auto_control_mode_code(r)}"
            f" sched={r.get('pcs_schedule_enabled')}"
            f" chg={r.get('pcs_charging_flag')}"
            f" dis={r.get('pcs_discharging_flag')}"
            # run / stby 為**顯示強化**：讓現場直接看見排程結束時的旗標轉換
            # （systemOnOrOffStatus / systemStandbyStatus 的 oldValue）。
            # 判斷仍一律由 pcs_is_idle_state() 與 should_auto_end() 負責，此處不參與決策。
            f" run={r.get('pcs_running_flag')}"
            f" stby={r.get('pcs_standby_flag')}"
            f" fault={r.get('pcs_fault_flag')}({'是' if fault else '否'})"
            f" dir={d}"
            f" state={_auto['state']}"
            f" session={sess.session_id if sess is not None else 'None'}"
            # owner（Phase 4.5）：self=本行程持有 / other=他人持有 / none=無人（fail closed）
            # 一律由 is_owner() 推導，不讀 monitor_owner.json
            f" owner={_owner_tag()}"
            f" src={source}"
            f"{idle_txt}"
            f"｜SOC {r.get('soc_percent')}% P {r.get('actual_active_power_kw')}kW")
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
_SCHED_TPL_LIST = "/schedule/template/list"
_SCHED_ITEM_LIST = "/schedule/list/{tid}"
def _sched_template_enabled(t):
    """模板是否啟用（enableFlag==1）。"""
    return t.get("enableFlag") in (1, "1")
