# -*- coding: utf-8 -*-
"""
Phase 4.6-C — Mutex 名稱 canonicalization（A~J）
======================================================================
要修的缺陷（2026-08-14 實機發現）
    Service 以 `D:\\Crawler Sample\\...` 啟動；使用者以
    `python "d:/Crawler Sample/test/device_control_menu.py"` 啟動 Dashboard。
    同一個 output root，`_mutex_name()` 卻算出兩顆不同的 Global Mutex：

        D:\\...\\charge_discharge_reports → ...0c305bef986a0531   （Service）
        d:\\...\\charge_discharge_reports → ...fc4cd03c3cd675eb   （Dashboard）

    原因：Windows 檔案系統不分大小寫，但 os.path.abspath() **不會**正規化
    磁碟機代號大小寫，雜湊自然不同。

    後果不是顯示問題：兩個行程各自 acquire_ownership() 都成功 →
    is_owner() 都是 True → Writer Gate 三層全部放行 →
    **同一個 Session 資料夾出現兩個寫入者**，正是 Phase 4.4 要防止的核心風險。
    當次實機另有衍生症狀：Dashboard 結束時 release_ownership() 依「pid 相符才刪」
    把它先前覆寫過的 monitor_owner.json 刪掉，連帶抹掉 Service 的診斷資訊。

修法（一行）
    os.path.abspath(root)  →  os.path.normcase(os.path.abspath(root))

    abspath 吸收相對路徑、`.`／`..`、尾端分隔符；
    normcase 在 Windows 上轉小寫並統一分隔符。
    刻意**不**用 realpath —— 部署路徑無 symlink/junction、NTFS 無 8.3 短檔名
    （皆已實測），realpath 只多做 I/O 不帶來額外收斂。

是否需要設備
    **不需要**。零網路、零 ApiClient、零 ReportSession。

安全性
    只操作「本測試自建的臨時 output root 所導出的 Mutex 名稱」——
    正式部署的那一顆不會被碰到（F 節提供證據）。
    不送控制命令、不建立/修改/刪除 Session、不修改 config、不執行 Git。

用法
    python test_phase4_mutex_canon.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import json
import time
import shutil
import hashlib
import tempfile
import threading
import subprocess
import contextlib

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


_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

_BAK_ROOT = RM._report_output_root
_BAK_CLIENT = CDR.ApiClient


class _ForbiddenClient:
    def __init__(self, *_a, **_k):
        raise AssertionError("本測試不得建立真實 ApiClient（會連設備）")


CDR.ApiClient = _ForbiddenClient


def name_for(root):
    """以指定 root 算出 Mutex 名稱（暫時覆寫 _report_output_root）。"""
    RM._report_output_root = lambda: root
    try:
        return RM._mutex_name()
    finally:
        RM._report_output_root = _BAK_ROOT


print("=" * 74)
print("Phase 4.6-C  Mutex 名稱 canonicalization")
print("=" * 74)

# ======================================================================
print("\n[實機案例定錨] 2026-08-14 雙 Owner 事故的兩個路徑")
# ======================================================================
PROD_UPPER = r"D:\Crawler Sample\output\charge_discharge_reports"
PROD_LOWER = r"d:\Crawler Sample\output\charge_discharge_reports"
CANON = "Global\\ESS_AutoMonitor_Owner_v1_ad9a00aa7db0cf8f"
OLD_UPPER = "Global\\ESS_AutoMonitor_Owner_v1_0c305bef986a0531"
OLD_LOWER = "Global\\ESS_AutoMonitor_Owner_v1_fc4cd03c3cd675eb"

_nu, _nl = name_for(PROD_UPPER), name_for(PROD_LOWER)
print(f"    D:\\... → {_nu}")
print(f"    d:\\... → {_nl}")
check("實機案例：大小寫磁碟機導出**同一顆** Mutex", _nu == _nl)
check(f"實機案例：兩者皆為 canonical 名稱 {CANON}",
      _nu == CANON and _nl == CANON)
check("實機案例：不再產生事故當時的 Service 舊名 ...0c305bef986a0531",
      _nu != OLD_UPPER and _nl != OLD_UPPER)
check("實機案例：不再產生事故當時的 Dashboard 舊名 ...fc4cd03c3cd675eb",
      _nu != OLD_LOWER and _nl != OLD_LOWER)

# ======================================================================
print("\n[A~E] 同一實體 root 的各種表示法 → 必須同名")
# ======================================================================
BASE = tempfile.mkdtemp(prefix="p46c_canon_")
_drive, _rest = os.path.splitdrive(BASE)
_sub = os.path.join(BASE, "Charge_Discharge_Reports")
os.makedirs(_sub, exist_ok=True)

VARIANTS = [
    ("A  磁碟機大寫", _drive.upper() + _rest + os.sep + "Charge_Discharge_Reports"),
    ("A  磁碟機小寫", _drive.lower() + _rest + os.sep + "Charge_Discharge_Reports"),
    ("B  正斜線", _sub.replace("\\", "/")),
    ("B  反斜線", _sub.replace("/", "\\")),
    ("C  尾端有 separator", _sub + os.sep),
    ("C  尾端無 separator", _sub.rstrip("\\/")),
    ("D  含 .", os.path.join(BASE, ".", "Charge_Discharge_Reports")),
    ("D  含 ..", os.path.join(BASE, "Charge_Discharge_Reports", "..",
                              "Charge_Discharge_Reports")),
    ("E  目錄名全小寫", os.path.join(BASE, "charge_discharge_reports")),
    ("E  目錄名全大寫", os.path.join(BASE, "CHARGE_DISCHARGE_REPORTS")),
]
_ref = name_for(VARIANTS[0][1])
print(f"    基準 = {_ref}")
for label, v in VARIANTS:
    check(f"{label:<22} → 同名", name_for(v) == _ref)

# ======================================================================
print("\n[F] 不同 output root → 必須不同名（不得過度收斂）")
# ======================================================================
_other = os.path.join(BASE, "Charge_Discharge_Reports_2")
check("F  相鄰但不同的資料夾 → 不同 Mutex", name_for(_other) != _ref)
_sibling = tempfile.mkdtemp(prefix="p46c_canon_other_")
check("F  另一個臨時 root → 不同 Mutex", name_for(_sibling) != _ref)
check("F  本測試用的名稱與正式部署不同 → 不會干擾 ESSAutoMonitor",
      _ref != CANON and _ref != OLD_UPPER and _ref != OLD_LOWER)

# ======================================================================
print("\n[G] 前綴與命名空間不變")
# ======================================================================
check("G  仍使用 config 的 MONITOR_MUTEX_PREFIX（唯一定義處）",
      _ref.startswith(CFG.MONITOR_MUTEX_PREFIX))
check("G  仍位於 Global\\ 命名空間（跨 session 前提不變）",
      _ref.startswith("Global\\") and CFG.MONITOR_MUTEX_PREFIX.startswith("Global\\"))
check("G  名稱長度合法（< 260）", len(_ref) < 260)
check("G  雜湊仍為 16 位十六進位（格式未變）",
      re.fullmatch(r"[0-9a-f]{16}", _ref[len(CFG.MONITOR_MUTEX_PREFIX):]) is not None)

# ======================================================================
print("\n[H] 跨行程實際互斥：d: 取得後，D: 必須 held_by_other")
# ======================================================================
# ⚠️ 這一項才是真正證明缺陷消失的關鍵 —— 名稱相同只是必要條件，
#    互斥是否成立必須以**兩個真實行程**驗證（同行程 Wait 只會遞迴取得）。
_root_lower = _drive.lower() + _rest + os.sep + "Charge_Discharge_Reports"
_root_upper = _drive.upper() + _rest + os.sep + "Charge_Discharge_Reports"
print(f"    子行程 root（小寫）: {_root_lower}")
print(f"    本行程 root（大寫）: {_root_upper}")


def _spawn(args):
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


_p, _pl = _spawn(["hold", _root_lower, "25"])
try:
    check(f"H  子行程以**小寫** root 取得 Ownership（pid {_pl and _pl.get('pid')}）",
          _pl is not None and _pl.get("ok") is True)
    RM._report_output_root = lambda: _root_upper
    _ok, _why = RM.acquire_ownership(role="canon-upper")
    print(f"    本行程以大寫 root acquire → ({_ok}, {_why!r})")
    check("H  本行程以**大寫** root → held_by_other（互斥成立）",
          _ok is False and _why == "held_by_other")
    check("H  **不是**兩邊都成功（修正前會是 acquired → 雙 Owner）",
          _ok is not True)
    check("H  兩造算出的 Mutex 名稱相同",
          name_for(_root_lower) == name_for(_root_upper))
    if _ok:
        RM.release_ownership()
finally:
    RM._report_output_root = _BAK_ROOT
    _kill(_p)

time.sleep(0.3)
RM._report_output_root = lambda: _root_upper
_ok2, _why2 = RM.acquire_ownership(role="canon-after")
check(f"H  持有者結束後可正常取得（{_why2}）", _ok2 is True)
check("H  release 正常", RM.release_ownership() is True)
RM._report_output_root = _BAK_ROOT

# ======================================================================
print("\n[I/J] 修法範圍守門")
# ======================================================================
_src = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
_fn = _code_only(_src[_src.index("def _mutex_name("):
                      _src.index("def _build_mutex_security(")])
check("I  _mutex_name() 使用 normcase", "normcase" in _fn)
check("I  _mutex_name() **未**使用 realpath（不為不存在的情境擴大修法）",
      "realpath" not in _fn)
check("I  仍以 abspath 吸收相對路徑 / . / .. / 尾端分隔符", "abspath" in _fn)
check("I  例外退路（OUTPUT_DIR）同樣經過 normcase（兩條路徑一致）",
      _fn.count("normcase") >= 2)
check("I  端點雜湊仍為 md5[:16]（格式未變，僅輸入被正規化）",
      "md5" in _fn and "[:16]" in _fn)

_dacl = _code_only(_src[_src.index("def _build_mutex_security("):
                        _src.index("def is_owner(")])
check("J  _build_mutex_security() 未被本次修改觸及（SDDL 仍為唯一來源）",
      "MUTEX_SDDL" in _dacl and "ConvertStringSecurityDescriptor" in _dacl)
check("J  MUTEX_SDDL 內容未變", RM.MUTEX_SDDL ==
      "D:(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)(A;;0x100001;;;AU)")
check("J  MUTEX_MIN_ACCESS 未變（0x00100001）",
      RM.MUTEX_MIN_ACCESS == 0x00100001)
_acq = _code_only(_src[_src.index("def acquire_ownership("):
                       _src.index("def release_ownership(")])
check("J  acquire_ownership 仍以 CreateMutexExW + MUTEX_MIN_ACCESS 開啟",
      "CreateMutexExW(" in _acq and "MUTEX_MIN_ACCESS" in _acq
      and "MUTEX_ALL_ACCESS" not in _acq)

# ======================================================================
print("\n[環境還原]")
# ======================================================================
CDR.ApiClient = _BAK_CLIENT
RM._report_output_root = _BAK_ROOT
check("已還原 CDR.ApiClient / _report_output_root",
      CDR.ApiClient is _BAK_CLIENT and RM._report_output_root is _BAK_ROOT)
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("測試結束未殘留 Session", RM._report["session"] is None)
for _d in (BASE, _sibling):
    shutil.rmtree(_d, ignore_errors=True)

ok = all(RESULTS)
print("\n" + "=" * 74)
print(f"Phase 4.6-C canonicalization {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）")
print("=" * 74)
sys.exit(0 if ok else 1)
