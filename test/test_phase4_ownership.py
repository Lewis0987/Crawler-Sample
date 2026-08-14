# -*- coding: utf-8 -*-
"""
Phase 4.4 — Monitor Ownership 驗證（A~P）
======================================================================
驗證跨行程互斥：同一時間只有一個行程能驅動自動報告生命週期並寫入 Session。

要防止的核心風險不是「建立兩份 Session」，而是**同一個 Session 資料夾同時
存在兩個寫入者** —— find_active_session() 只看 status，第二個行程啟動時會
resume 同一份 recording Session 並另起取樣執行緒。

是否需要設備
    **不需要**。所有子行程與本行程都走注入環境：假 client、暫存 output root、
    ApiClient 哨兵；全程零真設備 I/O。

為什麼需要子行程
    Windows Mutex 的互斥**無法在單一行程內驗證** —— 同行程再 Wait 只會累加
    遞迴計數而非阻擋。A~G 一律以 subprocess 起真實行程對打。

用法
    python test_phase4_ownership.py        # exit 0 = PASS
"""
import ast
import io
import os
import re
import sys
import json
import time
import shutil
import atexit
import signal
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


def _run_child(args, timeout=60):
    """執行子行程，回傳 (returncode, payload dict or None)。"""
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
    """非阻塞啟動子行程，等它吐出第一個 payload 後回傳 (proc, payload)。"""
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


# ======================================================================
# 靜態守門 + 本行程注入環境
# ======================================================================
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

ROOT = tempfile.mkdtemp(prefix="p44_own_")
_BAK = (CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
        RM._report_client, RM._monitor_client, RM._report_output_root)


class _ForbiddenClient:
    def __init__(self, *_a, **_k):
        raise AssertionError("測試不得建立真實 ApiClient（會連設備）")


class _FakeClient:
    def get(self, _p, **_k):
        return []


CDR.read_all = lambda _c: {"communication_ok": True, "pcs_charging_flag": False,
                           "pcs_discharging_flag": False, "pcs_running_flag": False,
                           "pcs_standby_flag": True, "pcs_fault_flag": False,
                           "pcs_control_mode_code": "manual", "pcs_schedule_enabled": False,
                           "actual_active_power_kw": 0.0, "soc_percent": 50.0,
                           "alarms": [], "_fail": []}
CDR._fetch_cell_packs = lambda _c: (None, "offline")
CDR.ApiClient = _ForbiddenClient
RM._report_client = lambda *a, **k: _FakeClient()
RM._monitor_client = lambda force=False: (_FakeClient(), False)
RM._report_output_root = lambda: ROOT

print("[Phase 4.4] I  import 不得自動取得 Ownership")
check("I  import report_monitor 後 is_owner() 為 False", RM.is_owner() is False)
check("I  import 後未建立 monitor_owner.json",
      not os.path.exists(os.path.join(ROOT, CFG.MONITOR_OWNER_FILE)))
check("I  ownership_state 初始為未持有",
      RM.ownership_state()["owned"] is False and RM.ownership_state()["handle"] is None
      if "handle" in RM.ownership_state() else RM.ownership_state()["owned"] is False)

print("\n[Phase 4.4] K  atexit LIFO：pause/guard 必須早於 release Mutex")
_mon_src = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
_i_rel = _mon_src.find("atexit.register(_release_ownership_atexit)")
_i_grd = _mon_src.find("atexit.register(_atexit_guard)")
check(f"K  release 先註冊、guard 後註冊（位置 {_i_rel} < {_i_grd}）",
      0 < _i_rel < _i_grd)
check("K  → LIFO 執行順序為 _atexit_guard() → _release_ownership_atexit()", 0 < _i_rel < _i_grd)
_reg = [getattr(c[0], "__name__", "") for c in getattr(atexit, "_exithandlers", [])] \
    if hasattr(atexit, "_exithandlers") else []
check("K  _atexit_guard 內含 Owner 判斷（非 Owner 直接返回）",
      "if not is_owner():" in _mon_src[_mon_src.index("def _atexit_guard("):
                                       _mon_src.index("atexit.register(_atexit_guard)")])

print("\n[Phase 4.4] N  _report_bg_start Writer Gate")
check("N  非 Owner 呼叫 _report_bg_start 回 False", RM._report_bg_start(object()) is False)
check("N  非 Owner 呼叫後未建立取樣 thread", RM._report["thread"] is None)
_bg = _mon_src[_mon_src.index("def _report_bg_start("):_mon_src.index("def _stop_and_finalize(")]
check("N  Gate 位於函式最前（在 threading.Event 之前）",
      _bg.index("if not is_owner():") < _bg.index("stop = threading.Event()"))

print("\n[Phase 4.4] O  四個 writer 入口在非 Owner 下全數被擋")
_entries = ("report_resume_on_launch", "_report_start_session",
            "_report_start_locked", "auto_schedule_check")
for _e in _entries:
    _fn = _mon_src[_mon_src.index(f"def {_e}("):]
    _fn = _fn[:_fn.index("\ndef ", 5)]
    check(f"O  {_e}() 的寫入路徑最終經過 _report_bg_start（受 Gate 保護）",
          "_report_bg_start(" in _fn or _e == "auto_schedule_check")
RM._report.update(session=None, thread=None)
with contextlib.redirect_stdout(io.StringIO()):
    RM.report_resume_on_launch()
check("O  非 Owner：report_resume_on_launch 未建立 session/thread",
      RM._report["session"] is None and RM._report["thread"] is None)
_res_fn = _mon_src[_mon_src.index("def report_resume_on_launch("):]
_res_fn = _res_fn[:_res_fn.index("\ndef ", 5)]
check("O  report_resume_on_launch 有獨立 Owner 守門（雙層防護）",
      "if not is_owner():" in _res_fn)

print("\n[Phase 4.4] M  acquire idempotent（不累加遞迴計數）")
_ok1, _w1 = RM.acquire_ownership(role="selftest")
check(f"M  第 1 次 acquire 成功（{_w1}）", _ok1 and _w1 in ("acquired", "abandoned_taken"))
_ok2, _w2 = RM.acquire_ownership(role="selftest")
_ok3, _w3 = RM.acquire_ownership(role="selftest")
check(f"M  第 2/3 次回 already_owner（{_w2} / {_w3}）",
      _ok2 and _ok3 and _w2 == "already_owner" and _w3 == "already_owner")
check("M  release 一次即完全釋放", RM.release_ownership() is True and RM.is_owner() is False)
_rc, _pl = _run_child(["acquire", ROOT])
check(f"M  釋放後其他行程可立即取得（child ok={_pl and _pl.get('ok')}）",
      _pl is not None and _pl.get("ok") is True)

print("\n[Phase 4.4] L  執行緒親和性：非取得執行緒不得 ReleaseMutex")
RM.acquire_ownership(role="selftest")
_res = {}


def _wrong_thread_release():
    _res["ret"] = RM.release_ownership()


_t = threading.Thread(target=_wrong_thread_release)
_t.start()
_t.join(10)
_rc2, _pl2 = _run_child(["acquire", ROOT])
check("L  由非取得執行緒呼叫 release → 回 False，未真正 ReleaseMutex",
      _res.get("ret") is False)
check(f"L  其他行程仍取不到（Mutex 未被誤釋放，"
      f"實際 {_pl2 and _pl2.get('why')}）",
      _pl2 is not None and _pl2.get("ok") is False and _pl2.get("why") == "held_by_other")
check("L  錯誤執行緒呼叫後所有權狀態完全未被破壞（仍持有、handle 仍在）",
      RM.is_owner() is True and RM._own["handle"] is not None
      and RM._own["thread_id"] == threading.get_ident())
check("L  由原執行緒 release 才真正釋放", RM.release_ownership() is True)

print("\n[Phase 4.4] A  Service A 取得 Owner → Service B 必須失敗")
_pA, _plA = _spawn_child(["hold", ROOT, "12"])
try:
    check(f"A  行程 A 取得 Ownership（{_plA and _plA.get('why')}）",
          _plA is not None and _plA.get("ok") is True)
    _rcB, _plB = _run_child(["acquire", ROOT])
    check(f"A  行程 B 取不到（{_plB and _plB.get('why')}）",
          _plB is not None and _plB.get("ok") is False
          and _plB.get("why") == "held_by_other")
    check("A  本行程（第三方）同樣取不到",
          RM.acquire_ownership(role="selftest")[1] == "held_by_other")

    print("\n[Phase 4.4] C  Dashboard Owner → Service 必須失敗（exit 3）")
    _rcS, _plS = _run_child(["service", ROOT])
    check(f"C  Service 以 exit {_rcS} 結束（須為 "
          f"{CFG.EXIT_OWNER_HELD_BY_OTHER}＝held_by_other，非程式故障）",
          _rcS == CFG.EXIT_OWNER_HELD_BY_OTHER)

    print("\n[Phase 4.4] B  Owner 持有 Session → 第二行程不得 resume / 不得寫入")
    # 由本測試以「假 Owner」身分建立一份 recording Session（模擬 Service 正在寫）
    _pA_hold = True
    _sdir = os.path.join(ROOT, "20260101_000000_auto")
    os.makedirs(_sdir, exist_ok=True)
    with open(os.path.join(_sdir, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as f:
        json.dump({"session_id": "20260101_000000_auto", "status": CFG.SESSION_RECORDING,
                   "action": "charge", "start_time": "2026-01-01 00:00:00",
                   "last_sample_time": "2026-01-01 00:00:05", "sample_count": 1}, f)
    _samples = os.path.join(_sdir, CFG.FILE_SAMPLES)
    with open(_samples, "w", encoding="utf-8-sig") as f:
        f.write("header\nrow1\n")
    _before_lines = open(_samples, encoding="utf-8-sig").read().count("\n")
    _rcR, _plR = _run_child(["resume", ROOT])
    check(f"B  第二行程取不到 Ownership（{_plR and _plR.get('why')}）",
          _plR is not None and _plR.get("ok") is False)
    check("B  第二行程未 resume（session 為 None）",
          _plR is not None and _plR.get("session") is False)
    check("B  第二行程未起取樣 thread", _plR is not None and _plR.get("thread") is False)
    _after_lines = open(_samples, encoding="utf-8-sig").read().count("\n")
    check(f"B  samples.csv 完全未增加（{_before_lines} → {_after_lines}）",
          _before_lines == _after_lines)
finally:
    _kill(_pA)

print("\n[Phase 4.4] D  Owner 正常 shutdown → 下一行程可立即接管")
_pD, _plD = _spawn_child(["hold", ROOT, "1"])
check(f"D  行程 D 取得 Ownership", _plD is not None and _plD.get("ok") is True)
_pD.wait(timeout=30)
_rcD2, _plD2 = _run_child(["acquire", ROOT])
check(f"D  D 正常結束後立即可接管（{_plD2 and _plD2.get('why')}）",
      _plD2 is not None and _plD2.get("ok") is True)
check("D  正常釋放 → 接管者不應標記 abandoned",
      _plD2 is not None and _plD2.get("abandoned") is False)

print("\n[Phase 4.4] E  Owner crash（taskkill /F）→ 下一行程以 abandoned 接管")
_pE, _plE = _spawn_child(["hold_kill", ROOT])
check(f"E  行程 E 取得 Ownership（pid {_plE and _plE.get('pid')}）",
      _plE is not None and _plE.get("ok") is True)
_rcE0, _plE0 = _run_child(["acquire", ROOT])
check("E  E 存活期間他人取不到",
      _plE0 is not None and _plE0.get("why") == "held_by_other")
# 真正觸發 WAIT_ABANDONED，需要第二個行程在 owner 死亡當下**已開著 handle 且正在 Wait**。
# 若無人持有 handle，owner 一死 Mutex 物件即消滅，下一個 CreateMutexW 會建出全新物件並
# 得到 WAIT_OBJECT_0 —— 結果同樣是「可接管」，但走的不是 abandoned 路徑。兩者都要驗。
_pW, _plW = _spawn_child(["wait_abandoned", ROOT, "20000"])
check("E  等待者已開啟 Mutex handle（owner 死亡後物件才不會消滅）",
      _plW is not None and _plW.get("stage") == "handle_open")
time.sleep(0.5)
_kill(_pE)                                              # 強制終止 owner，不給釋放機會
_outW = _pW.stdout.read().decode("utf-8", errors="replace")
try:
    _pW.wait(timeout=40)
except Exception:                                        # noqa: BLE001
    _kill(_pW)
_plW2 = None
for _ln in _outW.splitlines():
    if _ln.startswith("__P44__"):
        try:
            _plW2 = json.loads(_ln[len("__P44__"):])
        except ValueError:
            pass
check(f"E  owner 遭 taskkill /F 後，等待中的行程實際收到 WAIT_ABANDONED"
      f"（rc={_plW2 and _plW2.get('rc')}，WAIT_ABANDONED=128）",
      _plW2 is not None and _plW2.get("abandoned") is True)
time.sleep(0.3)
_rcE2, _plE2 = _run_child(["acquire", ROOT])
check(f"E  強制終止後新行程仍可正常取得所有權（{_plE2 and _plE2.get('why')}）",
      _plE2 is not None and _plE2.get("ok") is True)

print("\n[Phase 4.4] P  abandoned 接管後仍可正常 release（不留下第二個 abandoned）")
_rcP, _plP = _run_child(["acquire", ROOT])
check(f"P  前一位接管者結束後，下一位取得原因為 acquired（{_plP and _plP.get('why')}）",
      _plP is not None and _plP.get("ok") is True and _plP.get("why") == "acquired")
check("P  且未再標記 abandoned（abandoned 未被殘留傳遞）",
      _plP is not None and _plP.get("abandoned") is False)

print("\n[Phase 4.4] F  非 Owner 不得 release Owner 的 Mutex")
_pF, _plF = _spawn_child(["hold", ROOT, "10"])
try:
    check("F  行程 F 持有 Ownership", _plF is not None and _plF.get("ok") is True)
    check("F  本行程（非 Owner）呼叫 release_ownership() → no-op",
          RM.release_ownership() is False)
    _rcF, _plF2 = _run_child(["acquire", ROOT])
    check("F  F 仍持有（Mutex 未被非 Owner 釋放）",
          _plF2 is not None and _plF2.get("why") == "held_by_other")
finally:
    _kill(_pF)

print("\n[Phase 4.4] G  monitor_owner.json 損壞/不存在不得造成雙 Owner")
_pG, _plG = _spawn_child(["hold", ROOT, "12"])
_owner_file = os.path.join(ROOT, CFG.MONITOR_OWNER_FILE)
try:
    check("G  行程 G 持有 Ownership", _plG is not None and _plG.get("ok") is True)
    for _label, _mut in (
            ("刪除 monitor_owner.json", lambda: os.remove(_owner_file)),
            ("寫入損壞內容", lambda: open(_owner_file, "w", encoding="utf-8").write("{not json")),
            ("偽造成不存在的 pid", lambda: json.dump(
                {"pid": 999999, "role": "fake"},
                open(_owner_file, "w", encoding="utf-8")))):
        try:
            _mut()
        except OSError:
            pass
        _rcG, _plG2 = _run_child(["acquire", ROOT])
        check(f"G  {_label} → 仍取不到 Ownership（{_plG2 and _plG2.get('why')}）",
              _plG2 is not None and _plG2.get("ok") is False
              and _plG2.get("why") == "held_by_other")
finally:
    _kill(_pG)

print("\n[Phase 4.4] H  Mutex API failure → fail closed")
_orig_k32 = RM._k32
try:
    RM._k32 = lambda: None                               # 模擬非 Windows / 載入失敗
    _okH, _whyH = RM.acquire_ownership(role="selftest")
    check(f"H  _k32 不可用 → (False, no_win32)（實際 {_okH} / {_whyH}）",
          _okH is False and _whyH == "no_win32")
    check("H  且 is_owner() 仍為 False", RM.is_owner() is False)

    # ⚠️ 假物件的方法名必須跟產品碼一致（Phase 4.6-B 起為 CreateMutexExW）。
    #    名稱不一致時 acquire_ownership 只會拿到 AttributeError → 一律落到
    #    api_error_AttributeError，看似「fail closed 成功」，實際上 create_failed /
    #    wait_failed 這兩條路徑根本沒被測到。另需提供 LocalFree（SD 釋放）。
    class _NullMutex:
        def CreateMutexExW(self, *_a):
            return None

        def LocalFree(self, *_a):
            return None

    RM._k32 = lambda: _NullMutex()
    _okH2, _whyH2 = RM.acquire_ownership(role="selftest")
    check(f"H  CreateMutexExW 回 NULL → fail closed（{_whyH2}）",
          _okH2 is False and _whyH2.startswith("create_failed"))

    class _FailWait:
        def CreateMutexExW(self, *_a):
            return 12345

        def LocalFree(self, *_a):
            return None

        def WaitForSingleObject(self, *_a):
            return RM.WAIT_FAILED

        def CloseHandle(self, *_a):
            return True

    RM._k32 = lambda: _FailWait()
    _okH3, _whyH3 = RM.acquire_ownership(role="selftest")
    check(f"H  WAIT_FAILED → fail closed（{_whyH3}）",
          _okH3 is False and _whyH3.startswith("wait_failed"))

    class _Boom:
        def CreateMutexExW(self, *_a):
            raise RuntimeError("注入的 API 例外")

        def LocalFree(self, *_a):
            return None

    RM._k32 = lambda: _Boom()
    _okH4, _whyH4 = RM.acquire_ownership(role="selftest")
    check(f"H  任意例外 → fail closed（{_whyH4}）",
          _okH4 is False and _whyH4.startswith("api_error"))
    check("H  四種失敗情形下皆未取得 Ownership", RM.is_owner() is False)
finally:
    RM._k32 = _orig_k32

print("\n[Phase 4.4] 判定來源：只認 Mutex，不認 JSON")
_cfg_src = open(os.path.join(HERE, "charge_discharge_report_config.py"), encoding="utf-8").read()
check("MONITOR_OWNER_FILE / MUTEX_PREFIX 定義於 config（唯一定義處）",
      "MONITOR_OWNER_FILE" in _cfg_src and "MONITOR_MUTEX_PREFIX" in _cfg_src)


def _code_only(s):
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


_acq = _mon_src[_mon_src.index("def acquire_ownership("):
                _mon_src.index("def release_ownership(")]
check("acquire_ownership 未讀取 monitor_owner.json 作為判定依據",
      "_load_json" not in _code_only(_acq) and "MONITOR_OWNER_FILE" not in _code_only(_acq))
check("is_owner() 只讀 _own['owned']（不碰檔案）",
      "_load_json" not in _mon_src[_mon_src.index("def is_owner("):
                                   _mon_src.index("def ownership_state(")])
check("Global\\ 命名空間（跨 session；Local\\ 無法涵蓋 Service session 0）",
      CFG.MONITOR_MUTEX_PREFIX.startswith("Global\\"))

print("\n[Phase 4.4] J  Phase 4.3 行為不得退步")
_rcJ = subprocess.run([sys.executable, os.path.join(HERE, "test_phase4_service.py")],
                      cwd=HERE, capture_output=True, timeout=600)
_outJ = _rcJ.stdout.decode("utf-8", errors="replace")
_mJ = re.search(r"（(\d+)/(\d+) 檢查通過）", _outJ)
# ⚠️ 標籤內**不可**出現「（N/M 檢查通過）」原字串 —— run_phase3_regression.py 以該樣式
#    解析套件總數，嵌在檢查標籤裡會被誤抓，導致 runner 回報錯誤的檢查數。
#    故改以 "N of M" 呈現。
check(f"J  test_phase4_service.py 仍全數通過"
      f"［{_mJ.group(1) if _mJ else '?'} of {_mJ.group(2) if _mJ else '?'}"
      f"，exit {_rcJ.returncode}］",
      _rcJ.returncode == 0 and _mJ is not None and _mJ.group(1) == _mJ.group(2))

# ======================================================================
print("\n[Phase 4.4] 環境還原")
(CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
 RM._report_client, RM._monitor_client, RM._report_output_root) = _BAK
check("已還原 CDR.read_all / ApiClient", CDR.read_all is _BAK[0] and CDR.ApiClient is _BAK[2])
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("測試結束未殘留 Session", RM._report["session"] is None)
shutil.rmtree(ROOT, ignore_errors=True)

ok = all(RESULTS)
print(f"\n== Phase 4.4 Ownership 驗證 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
