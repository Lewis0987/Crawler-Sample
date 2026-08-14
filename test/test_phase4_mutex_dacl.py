# -*- coding: utf-8 -*-
"""
Phase 4.6-B — Mutex DACL / 最小權限 open-or-create 專項回歸
======================================================================
要修的缺陷（Phase 4.6-B I 實測發現）
    ESSAutoMonitor 以 LocalSystem 執行並先建立 Global\\ Mutex；
    使用者 session 的 Dashboard 呼叫 acquire_ownership() 得到的是
    **create_failed_5（ERROR_ACCESS_DENIED）**，而不是 held_by_other。

    原因有兩層，缺一不可：
      ① LocalSystem 建立的核心物件，預設 DACL 不授權一般使用者
         → 必須在建立時附帶明確 DACL（MUTEX_SDDL）。
      ② CreateMutexW 對「已存在的物件」**隱含要求 MUTEX_ALL_ACCESS**
         → 即使 DACL 已授權最小權限，舊寫法仍會被拒
         → 必須改用 CreateMutexExW 並只要求 SYNCHRONIZE|MUTEX_MODIFY_STATE。

    後果不是「多一行錯誤訊息」：held_by_other 是**正常互斥**（Dashboard 轉
    Observer、顯示背景服務為 Owner），create_failed_5 卻是 fail-closed 的
    API 失敗（顯示「自動報告已停用」），使用者會誤判系統故障。

本檔驗什麼
    A 產品碼契約（靜態；已剝除註解與 docstring）
    B Security Descriptor 建立與 fail-closed
    C 情境一：物件不存在 → 一般使用者建立，DACL 與 SDDL 逐字相符
    D 情境二：物件已存在且他人持有 → held_by_other，且**絕不**是 create_failed_5
    E 舊寫法的缺陷可離線重現（證明修法針對的是真正病因）
    F 跨帳號前提與測試隔離

是否需要設備
    **不需要**。零網路、零 ApiClient、零 ReportSession。

安全性
    只操作「本測試自建的臨時 output root 所導出的 Mutex 名稱」——
    _mutex_name() 取 output root 絕對路徑雜湊，臨時目錄必然與正式部署不同名，
    因此**不可能**碰到 ESSAutoMonitor 正在持有的那一顆（F 節提供證據）。
    不送控制命令、不建立/修改/刪除任何 Session、不修改 config、不執行 Git。

執行結果與 ESSAutoMonitor 是否 Running 無關。

用法
    python test_phase4_mutex_dacl.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import ast
import json
import time
import ctypes
import shutil
import tempfile
import threading
import subprocess
import contextlib
from ctypes import wintypes

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CHILD = os.path.join(HERE, "_p44_owner_child.py")
RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def _code_only(s):
    """剝除 docstring 與註解 —— 否則守門會比對到自己的說明文字（本專案累犯項）。"""
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


def _run_child(args, timeout=60):
    p = subprocess.run([sys.executable, CHILD] + args, cwd=HERE, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", errors="replace")
    payload = None
    for line in out.splitlines():
        if line.startswith("__P44__"):
            try:
                payload = json.loads(line[len("__P44__"):])
            except ValueError:
                pass
    return p.returncode, payload


def _spawn_child(args):
    p = subprocess.Popen([sys.executable, CHILD] + args, cwd=HERE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    payload, deadline = None, time.time() + 60
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        s = line.decode("utf-8", errors="replace")
        if s.startswith("__P44__"):
            try:
                payload = json.loads(s[len("__P44__"):])
            except ValueError:
                pass
            break
    return p, payload


def _kill(p):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       capture_output=True, timeout=20)
    except Exception:                                    # noqa: BLE001
        p.kill()
    try:
        p.wait(timeout=20)
    except Exception:                                    # noqa: BLE001
        pass


_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

ROOT = tempfile.mkdtemp(prefix="p46b_dacl_")
_BAK_ROOT = RM._report_output_root
_BAK_CLIENT = CDR.ApiClient


class _ForbiddenClient:
    def __init__(self, *_a, **_k):
        raise AssertionError("本測試不得建立真實 ApiClient（會連設備）")


CDR.ApiClient = _ForbiddenClient
RM._report_output_root = lambda: ROOT

PROD_MUTEX = None
with contextlib.redirect_stdout(io.StringIO()):
    RM._report_output_root = _BAK_ROOT
    PROD_MUTEX = RM._mutex_name()               # 正式部署的名稱（僅用於比對隔離）
    RM._report_output_root = lambda: ROOT
NAME = RM._mutex_name()

# ---- 本檔自用的 Win32 宣告（獨立宣告，不依賴產品碼的 _k32，才驗得出契約差異）----
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateMutexW.restype = wintypes.HANDLE
k32.CreateMutexExW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR,
                               wintypes.DWORD, wintypes.DWORD]
k32.CreateMutexExW.restype = wintypes.HANDLE
k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.OpenMutexW.restype = wintypes.HANDLE
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
k32.ReleaseMutex.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
k32.LocalFree.argtypes = [wintypes.LPVOID]
k32.LocalFree.restype = wintypes.LPVOID
adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
    ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
adv.GetSecurityInfo.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.LPVOID,
                                wintypes.LPVOID, wintypes.LPVOID,
                                ctypes.POINTER(wintypes.LPVOID)]
adv.GetSecurityInfo.restype = wintypes.DWORD

READ_CONTROL = 0x00020000
SE_KERNEL_OBJECT = 6
DACL_SECURITY_INFORMATION = 0x4
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183

_MON_SRC = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
_MON_CODE = _code_only(_MON_SRC)


def _sddl_of_handle(h):
    """回讀核心物件實際生效的 DACL（需要 handle 具備 READ_CONTROL）。"""
    psd = wintypes.LPVOID()
    rc = adv.GetSecurityInfo(h, SE_KERNEL_OBJECT, DACL_SECURITY_INFORMATION,
                             None, None, None, None, ctypes.byref(psd))
    if rc != 0 or not psd:
        return None, rc
    out = None
    s = wintypes.LPWSTR()
    ln = wintypes.DWORD()
    if adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            psd, RM.SDDL_REVISION_1, DACL_SECURITY_INFORMATION,
            ctypes.byref(s), ctypes.byref(ln)):
        out = s.value
        k32.LocalFree(s)
    k32.LocalFree(psd)
    return out, rc


def _wait_on_other_thread(handle, ms=0):
    """
    在**另一條執行緒**上 Wait。

    ⚠️ Mutex 所有權是執行緒親和且可遞迴的 —— 同一條執行緒重複 Wait 只會累加
       遞迴計數並回 WAIT_OBJECT_0，測不出 WAIT_TIMEOUT。
    ⚠️ 取得後必須在**同一條**執行緒立刻釋放，否則執行緒結束會讓 Mutex 進入
       abandoned 狀態，污染後續檢查（本專案曾因此誤判）。
    """
    box = {}

    def _run():
        rc = k32.WaitForSingleObject(handle, ms)
        box["rc"] = int(rc)
        if rc in (RM.WAIT_OBJECT_0, RM.WAIT_ABANDONED):
            k32.ReleaseMutex(handle)
            box["released"] = True

    t = threading.Thread(target=_run)
    t.start()
    t.join(30)
    return box.get("rc"), box.get("released", False)


try:
    IS_ADMIN = bool(ctypes.windll.shell32.IsUserAnAdmin())
except Exception:                                        # noqa: BLE001
    IS_ADMIN = None

print("=" * 70)
print("Phase 4.6-B  Mutex DACL / 最小權限 open-or-create 專項回歸")
print("=" * 70)
print(f"  測試用 output root : {ROOT}")
print(f"  測試用 Mutex       : {NAME}")
print(f"  正式部署 Mutex     : {PROD_MUTEX}")
print(f"  本行程是否 Admin   : {IS_ADMIN}（一般使用者情境即為 False）")

# ======================================================================
print("\n[A] 產品碼契約（靜態；已剝除註解與 docstring）")
# ======================================================================
check(f"MUTEX_MIN_ACCESS = 0x{RM.MUTEX_MIN_ACCESS:08x}"
      f"（SYNCHRONIZE|MUTEX_MODIFY_STATE）",
      RM.MUTEX_MIN_ACCESS == 0x00100001
      and RM.MUTEX_MIN_ACCESS == (RM.SYNCHRONIZE | RM.MUTEX_MODIFY_STATE))
check("MUTEX_MIN_ACCESS 嚴格小於 MUTEX_ALL_ACCESS（最小權限原則）",
      RM.MUTEX_MIN_ACCESS != RM.MUTEX_ALL_ACCESS
      and (RM.MUTEX_MIN_ACCESS & RM.MUTEX_ALL_ACCESS) == RM.MUTEX_MIN_ACCESS)
check("MUTEX_SDDL 逐字符合設計（SY/BA 全權、AU 僅 0x100001）",
      RM.MUTEX_SDDL == "D:(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)(A;;0x100001;;;AU)")
check("AU 未取得 FULL CONTROL，且未使用 Everyone(WD)",
      "0x1f0001;;;AU" not in RM.MUTEX_SDDL and ";;;WD)" not in RM.MUTEX_SDDL
      and "0x100001;;;AU" in RM.MUTEX_SDDL)
check("AU 的遮罩恰好等於 MUTEX_MIN_ACCESS（授權與要求一致，不多不少）",
      f"0x{RM.MUTEX_MIN_ACCESS:x};;;AU" in RM.MUTEX_SDDL.replace("0x00", "0x"))
check("SDDL_REVISION_1 == 1", RM.SDDL_REVISION_1 == 1)

_k = RM._k32()
check("_k32() 可用（Windows 平台）", _k is not None)
check("_k32() 宣告 CreateMutexExW 且 restype 為 HANDLE"
      "（未宣告會讓 64 位元 handle 被截成 32 位元）",
      hasattr(_k, "CreateMutexExW") and _k.CreateMutexExW.restype is wintypes.HANDLE)
check("_k32() 宣告 LocalFree（Security Descriptor 需釋放）",
      hasattr(_k, "LocalFree") and _k.LocalFree.restype is wintypes.LPVOID)

_k32_body = _MON_CODE[_MON_CODE.index("def _k32("):_MON_CODE.index("SYNCHRONIZE =")]
check("_k32() 內未宣告 CreateMutexW（避免誤用隱含 ALL_ACCESS 的舊 API）",
      "CreateMutexW" not in _k32_body)
check("report_monitor.py 全檔無 CreateMutexW 呼叫（僅註解可提及）",
      "CreateMutexW(" not in _MON_CODE)

_acq = _MON_CODE[_MON_CODE.index("def acquire_ownership("):
                 _MON_CODE.index("def release_ownership(")]
check("acquire_ownership 以 MUTEX_MIN_ACCESS 呼叫 CreateMutexExW",
      "CreateMutexExW(" in _acq and "MUTEX_MIN_ACCESS" in _acq)
check("acquire_ownership 未向 CreateMutexExW 要求 MUTEX_ALL_ACCESS",
      "MUTEX_ALL_ACCESS" not in _acq)
check("acquire_ownership 在 finally 中 LocalFree（不留 unmanaged memory）",
      "finally" in _acq and "LocalFree" in _acq)
check("acquire_ownership 只有一個建立入口（open-or-create，無 TOCTOU）",
      _acq.count("CreateMutexExW(") == 1 and "OpenMutexW" not in _acq)

# SD 失敗必須 fail closed —— 用 AST 確認「sa is None 就直接 return」出現在建立之前
_tree = ast.parse(_MON_SRC)
_acq_node = next(n for n in ast.walk(_tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "acquire_ownership")
_lines_sd = [n.lineno for n in ast.walk(_acq_node)
             if isinstance(n, ast.Compare) and getattr(n.left, "id", "") == "sa"]
_lines_create = [n.lineno for n in ast.walk(_acq_node)
                 if isinstance(n, ast.Attribute) and n.attr == "CreateMutexExW"]
check("sa is None 的 fail-closed 判斷在 CreateMutexExW 之前",
      bool(_lines_sd) and bool(_lines_create) and min(_lines_sd) < min(_lines_create))
check("SD 失敗時不存在任何「無 DACL 建立」的退路（原始碼無 CreateMutexExW(None",
      "CreateMutexExW(None" not in _acq.replace(" ", ""))

# ======================================================================
print("\n[B] Security Descriptor 建立與 fail-closed")
# ======================================================================
_sa, _sd, _err = RM._build_mutex_security()
check(f"_build_mutex_security() 成功（err={_err}）", _sa is not None and _err == 0)
check("sa.nLength 正確（等於結構大小）",
      _sa is not None and _sa.nLength == ctypes.sizeof(type(_sa)))
check("sa.lpSecurityDescriptor 非 NULL 且指向回傳的 sd",
      _sa is not None and bool(_sa.lpSecurityDescriptor)
      and _sa.lpSecurityDescriptor == getattr(_sd, "value", _sd))
check("sa.bInheritHandle 為 False（handle 不繼承給子行程）",
      _sa is not None and not _sa.bInheritHandle)
_readback = None
if _sd:
    _s = wintypes.LPWSTR()
    _ln = wintypes.DWORD()
    if adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            _sd, RM.SDDL_REVISION_1, DACL_SECURITY_INFORMATION,
            ctypes.byref(_s), ctypes.byref(_ln)):
        _readback = _s.value
        k32.LocalFree(_s)
    k32.LocalFree(_sd)
check(f"回讀建出的 SD 與 MUTEX_SDDL 逐字相符（{_readback}）",
      _readback == RM.MUTEX_SDDL)

# fail closed：advapi32 不可用 → 不得建立任何 Mutex
_orig_adv = RM._advapi32
try:
    RM._advapi32 = lambda: None
    _okB, _whyB = RM.acquire_ownership(role="dacl-selftest")
    check(f"advapi32 不可用 → (False, sd_failed_no_advapi32)（實際 {_okB} / {_whyB}）",
          _okB is False and _whyB == "sd_failed_no_advapi32")
    check("且 is_owner() 仍為 False", RM.is_owner() is False)
    _hB = k32.OpenMutexW(RM.MUTEX_MIN_ACCESS, False, NAME)
    _eB = ctypes.get_last_error()
    check(f"SD 失敗時**完全未建立** Mutex 物件（OpenMutexW err={_eB}，2=不存在）",
          not _hB and _eB == ERROR_FILE_NOT_FOUND)
    if _hB:
        k32.CloseHandle(_hB)
finally:
    RM._advapi32 = _orig_adv
check("已還原 _advapi32", RM._advapi32 is _orig_adv)

# ======================================================================
print("\n[C] 情境一：物件不存在 → 一般使用者建立")
# ======================================================================
_h0 = k32.OpenMutexW(RM.MUTEX_MIN_ACCESS, False, NAME)
_e0 = ctypes.get_last_error()
check(f"前置條件：Mutex 物件尚不存在（err={_e0}，2=ERROR_FILE_NOT_FOUND）",
      not _h0 and _e0 == ERROR_FILE_NOT_FOUND)
if _h0:
    k32.CloseHandle(_h0)

_okC, _whyC = RM.acquire_ownership(role="dacl-owner")
check(f"一般使用者可建立並取得所有權（{_okC} / {_whyC}）",
      _okC is True and _whyC == "acquired")
check("ownership_state() 一致（owned / reason / 未標記 abandoned）",
      RM.is_owner() is True
      and RM.ownership_state().get("reason") == "acquired"
      and RM.ownership_state().get("abandoned") is False)

# 建立時的 requested access 不受新 DACL 檢查（建立者本次即取得所請求的權限），
# 故一般使用者也能另開一個帶 READ_CONTROL 的 handle 回讀實際 DACL。
_hRC = k32.CreateMutexExW(None, NAME, 0, RM.MUTEX_MIN_ACCESS | READ_CONTROL)
_eRC = ctypes.get_last_error()
check(f"可另開帶 READ_CONTROL 的 handle 以回讀實際 DACL（err={_eRC}，183=已存在）",
      bool(_hRC))
_actual, _rcSD = (None, None)
if _hRC:
    _actual, _rcSD = _sddl_of_handle(_hRC)
check(f"核心物件**實際生效**的 DACL 與 MUTEX_SDDL 逐字相符（{_actual}）",
      _actual == RM.MUTEX_SDDL)
check("→ 因此 LocalSystem 建立的物件，一般使用者日後開得起來（AU 已被授權）",
      _actual is not None and f"0x{RM.MUTEX_MIN_ACCESS:x};;;AU".replace("0x00", "0x")
      in _actual.replace("0x00", "0x"))

# 最小權限 handle 的功能完整性：Wait 被擋（互斥有效）
_hMin = k32.CreateMutexExW(None, NAME, 0, RM.MUTEX_MIN_ACCESS)
_eMin = ctypes.get_last_error()
check(f"以最小權限開啟既有物件成功（err={_eMin}，183=ERROR_ALREADY_EXISTS）",
      bool(_hMin) and _eMin == ERROR_ALREADY_EXISTS)
_rc1, _rel1 = _wait_on_other_thread(_hMin, 0)
check(f"他執行緒以最小權限 Wait → WAIT_TIMEOUT（rc=0x{(_rc1 or 0):x}）"
      "，互斥有效",
      _rc1 == RM.WAIT_TIMEOUT and _rel1 is False)

check("release_ownership() 成功釋放", RM.release_ownership() is True)
_rc2, _rel2 = _wait_on_other_thread(_hMin, 0)
check(f"釋放後最小權限 handle 可真正取得所有權（rc=0x{(_rc2 or 0):x}）",
      _rc2 == RM.WAIT_OBJECT_0)
check("且以最小權限取得的 handle 足以 ReleaseMutex"
      "（→ 無需 MUTEX_ALL_ACCESS）", _rel2 is True)

# ======================================================================
print("\n[D] 情境二：物件已存在且他人持有 → held_by_other，不得是 create_failed_5")
# ======================================================================
_pD, _plD = _spawn_child(["hold", ROOT, "25"])
try:
    check(f"子行程取得 Ownership（pid {_plD and _plD.get('pid')}）",
          _plD is not None and _plD.get("ok") is True)
    _okD, _whyD = RM.acquire_ownership(role="dacl-dashboard")
    check(f"本行程（非 Owner）得到 held_by_other（實際 {_okD} / {_whyD}）",
          _okD is False and _whyD == "held_by_other")
    check(f"reason **不是** create_failed_5，也不是任何 create_failed / sd_failed"
          f"（{_whyD}）",
          _whyD != "create_failed_5"
          and not _whyD.startswith("create_failed")
          and not _whyD.startswith("sd_failed"))
    check("→ Dashboard 因此顯示為 Observer，而非「自動報告已停用（API 失敗）」",
          RM.ownership_state().get("reason") == "held_by_other"
          and RM.is_owner() is False)
    _hD = k32.CreateMutexExW(None, NAME, 0, RM.MUTEX_MIN_ACCESS)
    _eD = ctypes.get_last_error()
    check(f"物件被他人持有時，仍能以最小權限取得 handle（err={_eD}，183=已存在）",
          bool(_hD) and _eD == ERROR_ALREADY_EXISTS)
    _rcD, _relD = _wait_on_other_thread(_hD, 0)
    check(f"該 handle 的 Wait 為 WAIT_TIMEOUT（rc=0x{(_rcD or 0):x}）—— 未搶到所有權",
          _rcD == RM.WAIT_TIMEOUT and _relD is False)

    # ------------------------------------------------------------------
    print("\n[E] 舊寫法的缺陷可離線重現（證明修法針對真正病因）")
    # ------------------------------------------------------------------
    _hOld = k32.OpenMutexW(RM.MUTEX_ALL_ACCESS, False, NAME)
    _eOld = ctypes.get_last_error()
    check(f"對已存在物件要求 MUTEX_ALL_ACCESS → ACCESS_DENIED"
          f"（handle={'非NULL' if _hOld else 'NULL'}，err={_eOld}）",
          not _hOld and _eOld == ERROR_ACCESS_DENIED)
    if _hOld:
        k32.CloseHandle(_hOld)
    check("→ 這正是 create_failed_5 的來源：CreateMutexW 對既有物件隱含要求 ALL_ACCESS",
          _eOld == ERROR_ACCESS_DENIED)
    _hNew = k32.CreateMutexExW(None, NAME, 0, RM.MUTEX_MIN_ACCESS)
    _eNew = ctypes.get_last_error()
    check(f"同一顆物件、同一個 token，改要求最小權限即成功（err={_eNew}）",
          bool(_hNew))
    if _hNew:
        k32.CloseHandle(_hNew)
    check("→ 差異只在 dwDesiredAccess，證明修法（CreateMutexExW + MIN_ACCESS）對症",
          (not _hOld) and bool(_hNew))
    if _hD:
        k32.CloseHandle(_hD)
finally:
    _kill(_pD)

time.sleep(0.3)
_okD2, _whyD2 = RM.acquire_ownership(role="dacl-after")
check(f"持有者結束後本行程可正常取得（{_whyD2}）", _okD2 is True)
check("release 成功（不留下持有狀態）", RM.release_ownership() is True)

# ======================================================================
print("\n[F] 跨帳號前提與測試隔離")
# ======================================================================
check("Mutex 名稱位於 Global\\ 命名空間（Service 在 session 0，Local\\ 涵蓋不到）",
      NAME.startswith("Global\\")
      and CFG.MONITOR_MUTEX_PREFIX.startswith("Global\\"))
check("名稱由 output root 絕對路徑雜湊導出（同機不同部署不互搶）",
      "_report_output_root" in _MON_CODE[_MON_CODE.index("def _mutex_name("):
                                         _MON_CODE.index("def _build_mutex_security(")])
check(f"測試用 Mutex 與正式部署不同名 → 不可能干擾 ESSAutoMonitor 正持有的那一顆",
      NAME != PROD_MUTEX)
check("全程未建立真實 ApiClient（哨兵未被觸發）", CDR.ApiClient is _ForbiddenClient)

# ======================================================================
print("\n[環境還原]")
# ======================================================================
for _h in (_hRC, _hMin):
    if _h:
        k32.CloseHandle(_h)
CDR.ApiClient = _BAK_CLIENT
RM._report_output_root = _BAK_ROOT
check("已還原 CDR.ApiClient / _report_output_root",
      CDR.ApiClient is _BAK_CLIENT and RM._report_output_root is _BAK_ROOT)
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("測試結束未殘留 Session", RM._report["session"] is None)
shutil.rmtree(ROOT, ignore_errors=True)

ok = all(RESULTS)
print("\n" + "=" * 70)
print(f"Phase 4.6-B Mutex DACL 專項回歸 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）")
print("=" * 70)
sys.exit(0 if ok else 1)
