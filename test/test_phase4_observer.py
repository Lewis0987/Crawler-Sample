# -*- coding: utf-8 -*-
"""
Phase 4.5 — Dashboard Observer 模式驗證（A~S）
======================================================================
驗證兩件事：
  ① Monitor Core（report_monitor.py）內**不再有任何 input()**
     —— Core 會被 auto_monitor_service 使用，而 Windows Service（4.6）沒有 console，
     任何 input() 都會讓服務直接卡死。互動輸入一律留在 device_control_menu 的 UI 層。
  ② 非 Owner 的 Dashboard 進入 Observer：唯讀顯示與人工控制正常，
     但**絕不建立／續接／收尾任何 ReportSession**，且 samples.csv 完全不增加。

是否需要設備
    **不需要**。注入假 client、暫存 output root、ApiClient 哨兵 —— 零真設備 I/O。

用法
    python test_phase4_observer.py          # exit 0 = PASS
"""
import ast
import io
import os
import re
import sys
import json
import time
import shutil
import inspect
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


# ======================================================================
# A~C  Core 純參數化（靜態，先於 import）
# ======================================================================
print("[Phase 4.5] A~C  Monitor Core 不得含 input()，改為純參數 API")
_MON = os.path.join(HERE, "report_monitor.py")
_MENU = os.path.join(HERE, "device_control_menu.py")
_mon_src = open(_MON, encoding="utf-8").read()
_menu_src = open(_MENU, encoding="utf-8").read()

_mon_inputs = [n for n in ast.walk(ast.parse(_mon_src))
               if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "input"]
check(f"B  report_monitor.py 的 input() 呼叫數為 0（AST 判定，實際 {len(_mon_inputs)}）",
      len(_mon_inputs) == 0)
_menu_inputs = [n for n in ast.walk(ast.parse(_menu_src))
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "input"]
check(f"B  互動輸入已落在 UI 層（device_control_menu 的 input() 數 {len(_menu_inputs)} > 0）",
      len(_menu_inputs) > 0)

_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import device_control_menu as M
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

_sig = inspect.signature(RM.report_start)
check(f"C  report_start 簽章為 (mode_label='交流有功', setpoint=None)（實際 {_sig}）",
      list(_sig.parameters) == ["mode_label", "setpoint"]
      and _sig.parameters["mode_label"].default == "交流有功"
      and _sig.parameters["setpoint"].default is None)
_sig2 = inspect.signature(RM._report_start_locked)
check(f"C  _report_start_locked 亦為純參數（實際 {_sig2}）",
      list(_sig2.parameters) == ["mode_label", "setpoint"])
def _code_only(s):
    """剝除 docstring 與註解 —— 否則說明文字裡提到的 input() 會讓守門誤判。"""
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


check("A  report_start / _report_start_locked 的**執行碼**內已無 input(",
      "input(" not in _code_only(
          _mon_src[_mon_src.index("def report_start("):
                   _mon_src.index("def _last_active_direction(")]))
check("A  未新增重複的 lifecycle API（仍以 _report_start_session 為建立入口）",
      "_report_start_session(" in _mon_src and _mon_src.count("def _report_start_session(") == 1)
check("C  UI 互動入口 report_start_interactive() 存在於 menu",
      callable(getattr(M, "report_start_interactive", None)))

# ======================================================================
# 注入環境
# ======================================================================
ROOT = tempfile.mkdtemp(prefix="p45_obs_")
_BAK = (CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
        RM._report_client, RM._monitor_client, RM._report_output_root,
        CFG.SAMPLE_INTERVAL_SEC)


class _ForbiddenClient:
    def __init__(self, *_a, **_k):
        raise AssertionError("測試不得建立真實 ApiClient（會連設備）")


class _FakeClient:
    def get(self, _p, **_k):
        return []


def _reading(direction="charge"):
    return {"communication_ok": True,
            "pcs_charging_flag": direction == "charge",
            "pcs_discharging_flag": direction == "discharge",
            "pcs_running_flag": direction != "idle",
            "pcs_standby_flag": direction == "idle",
            "pcs_fault_flag": False, "pcs_control_mode_code": "smart",
            "pcs_control_mode": "智慧模式", "pcs_schedule_enabled": True,
            "pcs_power_control_mode": "交流有功",
            "actual_active_power_kw": 20.0 if direction == "charge" else 0.0,
            "calculated_power_kw": 20.0 if direction == "charge" else 0.0,
            "battery_voltage_v": 800.0, "battery_current_a": 25.0,
            "soc_percent": 55.0, "battery_status": "充電",
            "rack_max_temp_c": 28.0, "rack_min_temp_c": 25.0,
            "alarms": [], "_fail": []}


CDR.read_all = lambda _c: _reading("charge")
CDR._fetch_cell_packs = lambda _c: (None, "offline")
CDR.ApiClient = _ForbiddenClient
RM._report_client = lambda *a, **k: _FakeClient()
RM._monitor_client = lambda force=False: (_FakeClient(), False)
RM._report_output_root = lambda: ROOT
CFG.SAMPLE_INTERVAL_SEC = 0.05

_ok_all = False
try:
    # ==================================================================
    # E  Owner 下 report_start(參數) 正確寫入 Session
    # ==================================================================
    print("\n[Phase 4.5] E  Owner 下純參數 report_start 正確帶入設定")
    RM.acquire_ownership(role="test-observer")
    with contextlib.redirect_stdout(io.StringIO()):
        RM.report_start(mode_label="直流恆流", setpoint=20.0)
    _sess = RM._report["session"]
    check("E  Owner 下成功建立 Session", _sess is not None)
    check(f"E  control_mode_label 由參數帶入（{_sess and _sess.control_mode_label}）",
          _sess is not None and _sess.control_mode_label == "直流恆流")
    check(f"E  setpoint_value 由參數帶入（{_sess and _sess.setpoint_value}）",
          _sess is not None and _sess.setpoint_value == 20.0)
    _n_thread_owner = threading.active_count()
    with contextlib.redirect_stdout(io.StringIO()):
        RM.report_pause()
    RM.release_ownership()

    # ==================================================================
    # 建立一份「Owner 正在記錄」的 Session 供 Observer 唯讀顯示
    # ==================================================================
    _sdir = os.path.join(ROOT, "20260101_010101_auto")
    os.makedirs(_sdir, exist_ok=True)
    with open(os.path.join(_sdir, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as f:
        json.dump({"session_id": "20260101_010101_auto", "status": CFG.SESSION_RECORDING,
                   "action": "charge", "sample_count": 42,
                   "last_sample_time": "2026-01-01 01:05:00"}, f)
    _samples = os.path.join(_sdir, CFG.FILE_SAMPLES)
    with open(_samples, "w", encoding="utf-8-sig") as f:
        f.write("h\nr1\nr2\n")

    # 進入 Observer 語意（他人持有）—— 以真實子行程持鎖，最貼近實況
    _pOwner, _plOwner = _spawn_child(["hold", ROOT, "45"])
    check("前置  子行程取得 Ownership（本行程即成為 Observer）",
          _plOwner is not None and _plOwner.get("ok") is True)
    _okA, _whyA = RM.acquire_ownership(role="dashboard")
    check(f"前置  本行程取不到（{_whyA}）", _okA is False and _whyA == "held_by_other")

    _before = open(_samples, encoding="utf-8-sig").read().count("\n")
    _thr_before = threading.active_count()

    # ==================================================================
    # D / F / G  Observer 下報告操作
    # ==================================================================
    print("\n[Phase 4.5] D/F/G  Observer 下報告操作明確拒絕（不得 silent no-op）")
    _o = io.StringIO()
    with contextlib.redirect_stdout(_o):
        RM.report_start(mode_label="交流有功")
    check("D  非 Owner 呼叫 RM.report_start → 未建立 Session",
          RM._report["session"] is None)
    check("D  非 Owner 呼叫後未起取樣 thread", RM._report["thread"] is None)
    check("D  Core Writer Gate 有輸出明確原因（非靜默）",
          "非 Monitor Owner" in _o.getvalue())

    _o18 = io.StringIO()
    with contextlib.redirect_stdout(_o18):
        M.report_start_interactive()
    _t18 = _o18.getvalue()
    check("F  Menu 18 明確提示 OBSERVER（不得 silent no-op）",
          "[OBSERVER]" in _t18 and "無法開始報告" in _t18)
    check("F  Menu 18 未建立 Session", RM._report["session"] is None)
    check("F  Menu 18 未詢問任何 input（Owner 檢查先於 input）",
          "控制方式" not in _t18)

    _o19 = io.StringIO()
    with contextlib.redirect_stdout(_o19):
        M.handle_choice("19")
    _t19 = _o19.getvalue()
    check("G  Menu 19 明確提示 OBSERVER（原本為 silent no-op）",
          "[OBSERVER]" in _t19 and "無法結束報告" in _t19)

    # ==================================================================
    # H / I  Observer 下設備控制照常、但不碰報告
    # ==================================================================
    print("\n[Phase 4.5] H/I  設備控制不受限制，但不建立/收尾報告")
    _oH = io.StringIO()
    with contextlib.redirect_stdout(_oH):
        M.report_on_charge_discharge_control("charge", "交流有功", 20.0)
    _tH = _oH.getvalue()
    check("H  Menu 4/5/6 後明確提示「控制已送出／Observer／由 Owner 負責」",
          "設備控制已送出" in _tH and "Observer" in _tH and "Monitor Owner" in _tH)
    check("H  未建立 Session", RM._report["session"] is None)
    check("H  未進行 PCS 輪詢（Owner 檢查先於 _poll_state，不浪費 API）",
          "偵測實際" not in _tH)

    _oI = io.StringIO()
    with contextlib.redirect_stdout(_oI):
        M.report_on_stop_control()
    _tI = _oI.getvalue()
    check("I  Menu 7 後明確提示，且未執行報告收尾",
          "設備已停止" in _tI and "Observer" in _tI)
    check("I  Observer 未呼叫 _stop_and_finalize（pending_end_reason 未被設定）",
          RM._report["pending_end_reason"] is None)

    # ==================================================================
    # 核心不變量：Observer 全程零寫入、零 thread
    # ==================================================================
    print("\n[Phase 4.5] 核心不變量：Observer 零寫入、零 sampling thread")
    _after = open(_samples, encoding="utf-8-sig").read().count("\n")
    check(f"Observer 全程 samples.csv 完全未增加（{_before} → {_after}）", _before == _after)
    check(f"Observer 全程未新增 thread（{_thr_before} → {threading.active_count()}）",
          threading.active_count() == _thr_before)
    check("Observer 全程 _report['session'] 恆為 None", RM._report["session"] is None)
    check("Observer 全程 _report['thread'] 恆為 None", RM._report["thread"] is None)
    _st_now = json.load(open(os.path.join(_sdir, CFG.FILE_SESSION_STATE), encoding="utf-8"))
    check("Observer 未改動 Owner 的 session_state.json（sample_count 仍 42）",
          _st_now.get("sample_count") == 42
          and _st_now.get("status") == CFG.SESSION_RECORDING)

    # ==================================================================
    # J / K  Observer 下其餘 Menu 功能
    # ==================================================================
    print("\n[Phase 4.5] J/K  Observer 下唯讀與設備控制功能維持")
    _oJ = io.StringIO()
    with contextlib.redirect_stdout(_oJ):
        M.handle_choice("20")
    check("J  Menu 20 查看報告可用（未被 Ownership 阻擋）",
          "[OBSERVER]" not in _oJ.getvalue())
    _menu_code = re.sub(r"#.*", "", re.sub(r'"""[\s\S]*?"""', "", _menu_src))
    _hc = _menu_code[_menu_code.index("def handle_choice("):]
    _hc = _hc[:_hc.index("\ndef ", 5)]
    check("K  Ownership 守門只出現在報告類選項（18/19），未加在設備控制分支",
          _hc.count("_require_owner(") == 1)
    check("K  設備控制路徑（_execute_live）不含 Ownership 判斷",
          "is_owner" not in _menu_code[_menu_code.index("def _execute_live("):
                                       _menu_code.index("def _print_control_detail(")])

    # ==================================================================
    # L / M / N / O  顯示層與 JSON 降級
    # ==================================================================
    print("\n[Phase 4.5] L/M/N/O  Ownership 顯示與 JSON 降級")
    _owner_file = os.path.join(ROOT, CFG.MONITOR_OWNER_FILE)
    check("L  Observer 狀態行標示 Observer 且說明不寫報告",
          "Observer" in M._ownership_line() and "不建立" in M._ownership_line())
    _tmp_own = RM._own["reason"]
    RM._own.update(owned=True, role="dashboard")
    check("L  Owner 狀態行標示「本視窗負責 Auto Report」",
          "本視窗" in M._ownership_line() and "負責 Auto Report" in M._ownership_line())
    RM._own.update(owned=False, reason="no_win32")
    check("L  取不到時標示 fail closed / 自動報告停用",
          "fail closed" in M._ownership_line() and "停用" in M._ownership_line())
    RM._own.update(reason=_tmp_own)

    _bak_json = open(_owner_file, encoding="utf-8").read() if os.path.exists(_owner_file) else None
    if os.path.exists(_owner_file):
        os.remove(_owner_file)
    check("M  owner_info() 於檔案不存在時回 None", RM.owner_info() is None)
    check("M  UI 降級顯示「詳細資訊不可用」，不拋例外",
          "詳細資訊不可用" in M._ownership_line())
    with open(_owner_file, "w", encoding="utf-8") as f:
        f.write("{not json")
    check("N  owner_info() 於內容損壞時回 None", RM.owner_info() is None)
    _okN, _whyN = RM.acquire_ownership(role="dashboard")
    check(f"N  JSON 損壞**不影響** Mutex 判定（仍 held_by_other，實際 {_whyN}）",
          _okN is False and _whyN == "held_by_other")
    check("N  JSON 損壞後 is_owner() 仍為 False（未被推定成 Owner）",
          RM.is_owner() is False)

    with open(_owner_file, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "role": "dashboard",
                   "started_at": "00:00:00"}, f)
    check("O  json 的 pid 是自己但 is_owner()=False → 仍以 Mutex 為準（不得推定 Owner）",
          RM.is_owner() is False)
    check("O  UI 仍顯示 Observer（未因 pid 相符而誤判為本視窗）",
          "Observer" in M._ownership_line())
    check("O  owner_info() 為純唯讀：呼叫多次不修改也不修復檔案",
          RM.owner_info() is not None
          and json.load(open(_owner_file, encoding="utf-8")).get("pid") == os.getpid())

    # ==================================================================
    # P  Observer 的 report_status_line 唯讀顯示他方 Session
    # ==================================================================
    print("\n[Phase 4.5] P  Observer 唯讀顯示 Owner 的 Session")
    _thr_p = threading.active_count()
    _line = M.report_status_line()
    check(f"P  Observer 顯示 Owner 的 Session（{(_line or '')[:48]}…）",
          _line is not None and "Monitor Owner 記錄中" in _line
          and "20260101_010101_auto" in _line)
    check("P  顯示內容取自 session_state.json（status / 取樣數）",
          _line is not None and "status=recording" in _line and "42" in _line)
    check("P  產生該行未建立 ReportSession", RM._report["session"] is None)
    check("P  產生該行未啟動任何 thread", threading.active_count() == _thr_p)
    _after_p = open(_samples, encoding="utf-8-sig").read().count("\n")
    check(f"P  產生該行未寫入任何檔案（samples 仍 {_after_p}）", _after_p == _before)
    # 精確擷取**單一函式**（到下一個頂層 def 為止）並剝除說明文字 ——
    # 用「到某個具名函式為止」的區間會隨檔案內函式順序調整而納入無關程式碼。
    _i = _menu_src.index("def _observer_status_line(")
    _fn_src = _code_only(_menu_src[_i:_menu_src.index("\ndef ", _i + 10)])
    check("P  _observer_status_line 的執行碼不含 resume / bg_start / 寫檔",
          not any(k in _fn_src for k in ("ReportSession(", ".resume(", "_report_bg_start",
                                         '"w"', "_persist_state")))

    # ==================================================================
    # Q  Debug 行的 owner= 欄位
    # ==================================================================
    print("\n[Phase 4.5] Q  _monitor_status_line 的 owner= 欄位")
    check("Q  Observer 下 owner=other",
          " owner=other" in RM._monitor_status_line(_reading("charge"), "auto"))
    RM._own.update(owned=True)
    check("Q  Owner 下 owner=self",
          " owner=self" in RM._monitor_status_line(_reading("charge"), "auto"))
    RM._own.update(owned=False, reason="no_win32")
    check("Q  取不到時 owner=none",
          " owner=none" in RM._monitor_status_line(_reading("charge"), "auto"))
    RM._own.update(reason="held_by_other")
    check("Q  owner 標記由 is_owner() 推導，未讀 monitor_owner.json",
          "_load_json" not in _mon_src[_mon_src.index("def _owner_tag("):
                                       _mon_src.index("def _monitor_status_line(")])

    # ==================================================================
    # D（補）Writer Gate 仍是最後防線
    # ==================================================================
    print("\n[Phase 4.5] D  Core Writer Gate 仍為最後一道防線（UI 判斷不可取代）")
    check("D  Writer Gate 仍在 _report_bg_start 內（Phase 4.4 未被 UI 判斷取代）",
          "if not is_owner():" in _mon_src[_mon_src.index("def _report_bg_start("):
                                           _mon_src.index("def _stop_and_finalize(")])
    check("D  即使繞過 UI 直接呼叫 _report_bg_start，仍被擋下",
          RM._report_bg_start(object()) is False)
    _ok_all = True
finally:
    try:
        _kill(_pOwner)
    except Exception:                                    # noqa: BLE001
        pass
    (CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
     RM._report_client, RM._monitor_client, RM._report_output_root,
     CFG.SAMPLE_INTERVAL_SEC) = _BAK
    RM.release_ownership()
    RM._own.update(owned=False, reason=None, abandoned=False)
    RM._report.update(session=None, thread=None, stop=None, finalized=False,
                      pending_end_reason=None, stopping=False)
    shutil.rmtree(ROOT, ignore_errors=True)

# ======================================================================
# R / S  跨行程與不退步
# ======================================================================
print("\n[Phase 4.5] R  跨行程：Service Owner + Dashboard Observer")
_R_ROOT = tempfile.mkdtemp(prefix="p45_x_")
_pR, _plR = _spawn_child(["hold", _R_ROOT, "25"])
try:
    check("R  Service 端子行程取得 Ownership",
          _plR is not None and _plR.get("ok") is True)
    _rcR = subprocess.run([sys.executable, CHILD, "resume", _R_ROOT], cwd=HERE,
                          capture_output=True, timeout=90)
    _outR = _rcR.stdout.decode("utf-8", errors="replace")
    _plR2 = None
    for _ln in _outR.splitlines():
        if _ln.startswith("__P44__"):
            _plR2 = json.loads(_ln[len("__P44__"):])
    check(f"R  Dashboard 端行程取不到 Ownership（{_plR2 and _plR2.get('why')}）",
          _plR2 is not None and _plR2.get("ok") is False)
    check("R  Dashboard 端未 resume、未起 thread",
          _plR2 is not None and _plR2.get("session") is False
          and _plR2.get("thread") is False)
finally:
    _kill(_pR)
    shutil.rmtree(_R_ROOT, ignore_errors=True)

print("\n[Phase 4.5] S  Phase 4.3 / 4.4 不得退步")
for _name, _file, _want in (("Phase 4.3 service", "test_phase4_service.py", 56),
                            ("Phase 4.4 ownership", "test_phase4_ownership.py", 62)):
    _p = subprocess.run([sys.executable, os.path.join(HERE, _file)], cwd=HERE,
                        capture_output=True, timeout=1800)
    _o = _p.stdout.decode("utf-8", errors="replace")
    _m = re.findall(r"（(\d+)/(\d+) 檢查通過）", _o)
    _got = (int(_m[-1][0]), int(_m[-1][1])) if _m else (None, None)
    check(f"S  {_name} 仍全數通過［{_got[0]} of {_got[1]}，需 ≥{_want}，exit {_p.returncode}］",
          _p.returncode == 0 and _got[0] == _got[1] and (_got[1] or 0) >= _want)

print("\n[Phase 4.5] 環境還原")
check("已還原 CDR.read_all / ApiClient", CDR.read_all is _BAK[0] and CDR.ApiClient is _BAK[2])
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("測試結束未殘留 Session", RM._report["session"] is None)
check("測試主體完整執行（未因例外中斷）", _ok_all is True)

ok = all(RESULTS)
print(f"\n== Phase 4.5 Observer 驗證 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
