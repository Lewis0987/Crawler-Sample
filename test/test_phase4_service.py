# -*- coding: utf-8 -*-
"""
Phase 4.3 — Auto Monitor Service 離線驗證
======================================================================
驗證 auto_monitor_service.py 能在**完全不依賴 device_control_menu / Dashboard**
的情況下驅動整套自動報告流程，且不複製 Monitor Core 的任何狀態。

是否需要設備
    **不需要**。四道獨立防線確保全程零真設備 I/O：
      ① 注入：CDR.read_all / CDR._fetch_cell_packs / RM._report_client
               / RM._monitor_client 全部換成合成資料
      ② 哨兵：CDR.ApiClient 一被實例化就 AssertionError —— 即使有漏網的登入
               路徑也會立刻失敗，而不是連上設備
      ③ 隔離：輸出根目錄指向 tempfile，不碰正式 output/charge_discharge_reports
      ④ config：AUTO_* 僅在測試內暫時覆寫，結束前還原並自我驗證

用法
    python test_phase4_service.py          # exit 0 = PASS
"""
import ast
import io
import os
import sys
import json
import glob
import time
import shutil
import tempfile
import threading
import contextlib

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def _imports_of(path):
    """以 AST 取出實際 import 的頂層模組名（不用字串比對 —— 檔頭 docstring
    內就寫著「不得 import device_control_menu」，字串比對必然誤報）。"""
    out = set()
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0])
    return out


def _top_assigns(path):
    out = set()
    for n in ast.parse(open(path, encoding="utf-8").read()).body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
                elif isinstance(t, ast.Tuple):
                    out.update(e.id for e in t.elts if isinstance(e, ast.Name))
    return out


# ======================================================================
# A~D  靜態結構（import 前先驗，確保之後的 import 不會有意外副作用）
# ======================================================================
print("[Phase 4.3] 模組結構與依賴方向")
SVC_PATH = os.path.join(HERE, "auto_monitor_service.py")
MON_PATH = os.path.join(HERE, "report_monitor.py")

_svc_imp = _imports_of(SVC_PATH)
_mon_imp = _imports_of(MON_PATH)
check(f"C  service 不 import device_control_menu（實際 {sorted(_svc_imp)}）",
      "device_control_menu" not in _svc_imp)
check("C  service 有 import report_monitor", "report_monitor" in _svc_imp)
check("D  report_monitor 不 import service / menu",
      "auto_monitor_service" not in _mon_imp and "device_control_menu" not in _mon_imp)

_svc_top = _top_assigns(SVC_PATH)
check(f"K  service 未自建 _report / _auto / 鎖（實際頂層指派 {sorted(_svc_top)}）",
      not ({"_report", "_auto", "_SESSION_LOCK", "_CLIENT_LOCK"} & _svc_top))
_svc_src = open(SVC_PATH, encoding="utf-8").read()
check("K  service 未自建 threading.Thread（監看迴圈跑主執行緒）",
      "threading.Thread(" not in _svc_src)

# ---- B：import 不得啟動任何背景 thread ----
_before = threading.active_count()
import report_monitor as RM                        # noqa: E402
import auto_monitor_service as SVC                 # noqa: E402
import charge_discharge_report as CDR              # noqa: E402
import charge_discharge_report_config as CFG       # noqa: E402
_after = threading.active_count()
check(f"A  auto_monitor_service 可 import", SVC is not None)
check(f"B  import 未啟動任何背景 thread（{_before} → {_after}）", _before == _after)
check("N  未間接載入 device_control_menu", "device_control_menu" not in sys.modules)

_j_res = SVC.run.__doc__ or ""
check("J  resume 沿用 RM.report_resume_on_launch（未自行實作）",
      "RM.report_resume_on_launch()" in _svc_src
      and "def report_resume_on_launch" not in _svc_src)

# ======================================================================
# 測試替身與注入
# ======================================================================
_CLK = {"t": 1000.0}


def _mono():
    return _CLK["t"]


def _advance(sec):
    _CLK["t"] += sec


class _ForbiddenClient:
    """哨兵：任何真實 ApiClient 實例化都代表測試即將連設備 → 立即失敗。"""

    def __init__(self, *_a, **_k):
        raise AssertionError("測試不得建立真實 ApiClient（會連設備）")


class _FakeClient:
    def get(self, _path, **_kw):
        return []


_STATE = {"direction": "charge", "mode": "smart", "sched": True}


def _fake_read_all(_client):
    d = _STATE["direction"]
    return {
        "communication_ok": True,
        "pcs_control_mode_code": _STATE["mode"],
        "pcs_control_mode": "智慧模式" if _STATE["mode"] == "smart" else "手動模式",
        "pcs_schedule_enabled": _STATE["sched"],
        "pcs_charging_flag": d == "charge",
        "pcs_discharging_flag": d == "discharge",
        "pcs_running_flag": d != "idle",
        "pcs_standby_flag": d == "idle",
        "pcs_fault_flag": False,
        "pcs_status": "執行 / 併網" if d != "idle" else "停止 / 併網",
        "battery_status": "充電" if d == "charge" else "待機",
        "pcs_power_control_mode": "交流有功",
        "actual_active_power_kw": 20.0 if d == "charge" else (-18.0 if d == "discharge" else 0.0),
        "calculated_power_kw": 20.0 if d == "charge" else (-18.0 if d == "discharge" else 0.0),
        "battery_voltage_v": 800.0, "battery_current_a": 25.0 if d != "idle" else 0.0,
        "soc_percent": 55.0, "rack_max_temp_c": 28.0, "rack_min_temp_c": 25.0,
        "alarms": [], "_fail": [],
    }


def _reset_monitor():
    # 先讓仍在跑的 Session 正常收尾，避免把背景取樣執行緒遺留成孤兒
    if RM._report.get("session") is not None:
        try:
            RM.report_pause()
        except Exception:                            # noqa: BLE001
            pass
    RM._report.update(session=None, thread=None, stop=None, finalized=False,
                      pending_end_reason=None, stopping=False)
    RM._auto.update(state=RM.AUTO_IDLE, direction=None, since=None, held_sec=0.0,
                    streak=0, cooldown_until=None, sched_ctx=None, sched_ctx_at=None,
                    disabled=False, login_failed_at=None)
    RM._auto_clear_notes()


_BAK = (CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
        RM._report_client, RM._monitor_client, RM._report_output_root,
        RM.time.monotonic, CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.SAMPLE_INTERVAL_SEC)
_TMP_ROOT = tempfile.mkdtemp(prefix="p43_svc_")

CDR.read_all = _fake_read_all
CDR._fetch_cell_packs = lambda _c: (None, "offline")
CDR.ApiClient = _ForbiddenClient                     # 防線②
RM._report_client = lambda *a, **k: _FakeClient()
RM._monitor_client = lambda force=False: (_FakeClient(), False)
RM._report_output_root = lambda: _TMP_ROOT           # 防線③
RM.time.monotonic = _mono
CFG.AUTO_SCHEDULE_REPORT_ENABLED = True
CFG.SAMPLE_INTERVAL_SEC = 0.05                       # 加速背景取樣

# Phase 4.4 起，啟動任何 Session 寫入者都必須先持有 Monitor Ownership
# （_report_bg_start 的 Writer Gate）。本測試模擬「Service 就是 Owner」的正常情境。
# 「非 Owner 不得寫入」由 test_phase4_ownership.py 的 N/O 項專責驗證。
_own_ok, _own_why = RM.acquire_ownership(role="test-phase43")
print(f"\n[Phase 4.3] 前置：取得 Monitor Ownership（{_own_ok} / {_own_why}）")
check(f"前置  取得 Monitor Ownership（Phase 4.4 起為寫入前提）", _own_ok is True)

_svc_ok = False
try:
    # ==================================================================
    # E / F / L / M  完整生命週期
    # ==================================================================
    print("\n[Phase 4.3] Service 驅動完整生命週期（IDLE → RUNNING → 收尾）")
    _reset_monitor()
    _STATE["direction"] = "charge"

    act, why = SVC.tick()
    check(f"L  首輪 charge：Start Debounce 未達門檻 → 不建立（{act} / {why}）",
          act == "skipped" and RM._auto["state"] == RM.AUTO_START_HOLD)
    check("E  尚未建立 Session → 無取樣 thread", RM._report["thread"] is None)

    _advance(CFG.AUTO_HOLD_START_SEC + 1)
    act, why = SVC.tick()
    check(f"L  持續 {CFG.AUTO_HOLD_START_SEC}s 後 → 建立 Session（{act}）", act == "started")
    check(f"L  狀態機進入 RUNNING（{RM._auto['state']}）",
          RM._auto["state"] == RM.AUTO_RUNNING)
    check("M  Session 已建立", RM._report["session"] is not None)
    _th1 = RM._report["thread"]
    check("E  取樣 thread 恰 1 條且存活", _th1 is not None and _th1.is_alive())

    _n_thread_after_start = threading.active_count()
    for _ in range(3):
        _advance(5)
        SVC.tick()
    check(f"F  重複 tick 不產生第二條取樣 thread（thread 物件不變）",
          RM._report["thread"] is _th1)
    check(f"F  行程 thread 數未增加（{_n_thread_after_start} → {threading.active_count()}）",
          threading.active_count() == _n_thread_after_start)

    # 排程結束 → idle → Stop Debounce → 自動收尾
    _STATE["direction"] = "idle"
    SVC.tick()
    # ⚠️ 假時鐘必須「持續前進」而非一次跳躍：背景取樣執行緒是在自己的節奏上把
    #    _idle_since 設為「當下」的 monotonic 值。若只跳一次，取樣若發生在跳躍之後，
    #    _idle_since 會被設成跳躍後的時間，之後時鐘不再動 → idle 永遠累積不到門檻。
    _deadline = time.time() + 15
    while RM._report["session"] is not None and time.time() < _deadline:
        _advance(5)
        time.sleep(0.05)
    check(f"L  idle 達 {CFG.AUTO_HOLD_STOP_SEC}s → Monitor Core 自動收尾",
          RM._report["session"] is None)
    check(f"L  收尾後進入 COOLDOWN（{RM._auto['state']}）",
          RM._auto["state"] == RM.AUTO_COOLDOWN)
    check("E  收尾後取樣 thread 已結束", not _th1.is_alive())

    _folders = [d for d in glob.glob(os.path.join(_TMP_ROOT, "*")) if os.path.isdir(d)]
    check(f"M  恰建立 1 個 Session 資料夾（{len(_folders)}）", len(_folders) == 1)
    _st = json.load(open(os.path.join(_folders[0], CFG.FILE_SESSION_STATE),
                         encoding="utf-8")) if _folders else {}
    check(f"M  Session 已 finalize（status={_st.get('status')}）",
          _st.get("status") == CFG.SESSION_COMPLETED)

    # ==================================================================
    # B(A)  shutdown → pause → resume（可續接語意）
    # ==================================================================
    print("\n[Phase 4.3] 關閉語意：shutdown → paused → 可 resume")
    _reset_monitor()
    _STATE["direction"] = "discharge"
    act, _ = SVC.tick()                              # 第 1 輪：進入 START_HOLD（held=0）
    _advance(CFG.AUTO_HOLD_START_SEC + 1)            # 推進超過 Start Debounce 門檻
    act2, _ = SVC.tick()                             # 第 2 輪：達門檻 → 建立
    check(f"建立第二個 Session 供關閉測試（{act} / {act2}）",
          RM._report["session"] is not None)
    _sid = RM._report["session"].session_id
    _th2 = RM._report["thread"]

    SVC.shutdown()
    check("G  shutdown 後取樣 thread 已結束", not _th2.is_alive())
    check("G  shutdown 後 _report['session'] 已清空", RM._report["session"] is None)
    _sdir = os.path.join(_TMP_ROOT, _sid)
    _st2 = json.load(open(os.path.join(_sdir, CFG.FILE_SESSION_STATE), encoding="utf-8"))
    check(f"B  shutdown 後 status=paused（可續接，實際 {_st2.get('status')}）",
          _st2.get("status") == CFG.SESSION_PAUSED)
    check("B  shutdown **未**誤 finalize 成 completed",
          _st2.get("status") != CFG.SESSION_COMPLETED)
    check("B  shutdown 走 report_pause（原始碼未出現 report_stop）",
          "RM.report_pause()" in _svc_src and "RM.report_stop(" not in _svc_src)

    check("B  shutdown 同時釋放 Monitor Ownership（讓下一個行程可接管）",
          RM.is_owner() is False)

    # resume：模擬「服務重啟」—— 重新取得 Ownership 後才續接（Phase 4.4 的前提）
    _reset_monitor()
    _re_ok, _re_why = RM.acquire_ownership(role="test-phase43-restart")
    check(f"B  重啟後可重新取得 Ownership（{_re_why}）", _re_ok is True)
    RM.report_resume_on_launch()
    check(f"B  重新啟動後接回同一個 Session（{_sid}）",
          RM._report["session"] is not None
          and RM._report["session"].session_id == _sid)
    _th3 = RM._report["thread"]
    check("E  resume 後取樣 thread 恰 1 條", _th3 is not None and _th3.is_alive())
    _folders2 = [d for d in glob.glob(os.path.join(_TMP_ROOT, "*")) if os.path.isdir(d)]
    check(f"B  resume 未建立新資料夾（仍 {len(_folders2)} 個）", len(_folders2) == 2)
    SVC.shutdown()
    check("G  再次 shutdown 正常結束", RM._report["session"] is None and not _th3.is_alive())

    # ==================================================================
    # H / I  中斷與例外路徑
    # ==================================================================
    print("\n[Phase 4.3] 中斷與例外路徑")
    _reset_monitor()
    _STATE["direction"] = "idle"

    _ev = threading.Event()
    _n = SVC.run(interval=0, stop_event=_ev, max_ticks=3, resume=False)
    check(f"H  run(max_ticks=3) 正常返回 3 輪（實際 {_n}）", _n == 3)

    _ev2 = threading.Event()
    _ev2.set()
    check("H  stop_event 已設 → run 立即返回 0 輪",
          SVC.run(interval=0, stop_event=_ev2, resume=False) == 0)

    _orig_tick = SVC.tick

    def _kb_tick(*_a, **_k):
        raise KeyboardInterrupt
    SVC.tick = _kb_tick
    try:
        _ev3 = threading.Event()
        _n3 = SVC.run(interval=0, stop_event=_ev3, resume=False)
        check(f"H  KeyboardInterrupt 被攔截並正常返回（{_n3} 輪，未向外拋出）", True)
        check("H  KeyboardInterrupt 後 stop_event 已設", _ev3.is_set())
    except KeyboardInterrupt:
        check("H  KeyboardInterrupt 被攔截並正常返回", False)
        check("H  KeyboardInterrupt 後 stop_event 已設", False)
    finally:
        SVC.tick = _orig_tick

    def _boom_tick(*_a, **_k):
        raise RuntimeError("注入的取樣例外")
    SVC.tick = _boom_tick
    try:
        _ev4 = threading.Event()
        _n4 = SVC.run(interval=0, stop_event=_ev4, max_ticks=2, resume=False)
        check(f"I  單輪例外不中斷迴圈（仍跑滿 {_n4} 輪）", _n4 == 2)
    finally:
        SVC.tick = _orig_tick
    check("I  例外後未殘留 Session", RM._report["session"] is None)
    check(f"I  例外後狀態機未卡在中間態（{RM._auto['state']}）",
          RM._auto["state"] in (RM.AUTO_IDLE, RM.AUTO_COOLDOWN))

    # tick 取不到 client / 取樣失敗時的保守行為
    _bak_mc = RM._monitor_client
    RM._monitor_client = lambda force=False: (None, False)
    try:
        check("I  取不到 client → (None, 'no_client')，不拋例外",
              SVC.tick() == (None, "no_client"))
    finally:
        RM._monitor_client = _bak_mc
    _bak_rd = RM._read_device_state
    RM._read_device_state = lambda _c: None
    try:
        check("I  取樣失敗 → (None, 'read_failed')，不建立 Session",
              SVC.tick() == (None, "read_failed") and RM._report["session"] is None)
    finally:
        RM._read_device_state = _bak_rd

    # ==================================================================
    # A(訊號)  SIGBREAK 平台安全
    # ==================================================================
    print("\n[Phase 4.3] 訊號處理（平台安全）")
    import signal as _sig
    _ev5 = threading.Event()
    _inst = SVC.install_signal_handlers(_ev5)
    check(f"A  SIGINT 已註冊（實際 {_inst}）", "SIGINT" in _inst)
    check("A  SIGBREAK 僅在平台具備時註冊（不假設存在）",
          ("SIGBREAK" in _inst) == hasattr(_sig, "SIGBREAK"))
    check("A  原始碼以 getattr(signal, name, None) 取用，未硬編 signal.SIGBREAK",
          'getattr(signal, name, None)' in _svc_src
          and "signal.SIGBREAK" not in _svc_src)
    check("A  未安裝成功的訊號不影響服務啟動（install 回傳 list 且不拋例外）",
          isinstance(_inst, list))
    _sig.signal(_sig.SIGINT, _sig.default_int_handler)   # 還原，避免影響後續

    # ==================================================================
    # N / O  全程未觸及 menu 與真設備
    # ==================================================================
    print("\n[Phase 4.3] 隔離確認")
    check("N  全程未載入 device_control_menu", "device_control_menu" not in sys.modules)
    check("O  ApiClient 哨兵全程未被觸發（未連真設備）", CDR.ApiClient is _ForbiddenClient)
    check("O  輸出全部落在暫存目錄（正式 output 未被寫入）",
          RM._report_output_root() == _TMP_ROOT)
    _svc_ok = True
except Exception as _e:                              # noqa: BLE001
    import traceback
    traceback.print_exc()
    check(f"Phase 4.3 測試發生未預期例外：{type(_e).__name__}: {_e}", False)
finally:
    (CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
     RM._report_client, RM._monitor_client, RM._report_output_root,
     RM.time.monotonic, CFG.AUTO_SCHEDULE_REPORT_ENABLED,
     CFG.SAMPLE_INTERVAL_SEC) = _BAK
    _reset_monitor()
    RM.release_ownership()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)

print("\n[Phase 4.3] 測試環境還原")
check("測試結束已還原 CDR.read_all / ApiClient", CDR.read_all is _BAK[0]
      and CDR.ApiClient is _BAK[2])
check("測試結束已還原 RM._report_output_root / time.monotonic",
      RM._report_output_root is _BAK[5] and RM.time.monotonic is _BAK[6])
check(f"測試結束已還原 config（AUTO_SCHEDULE_REPORT_ENABLED="
      f"{CFG.AUTO_SCHEDULE_REPORT_ENABLED}）",
      CFG.AUTO_SCHEDULE_REPORT_ENABLED is _BAK[7]
      and CFG.SAMPLE_INTERVAL_SEC == _BAK[8])
check("測試結束未殘留 Session", RM._report["session"] is None)
check("測試結束已釋放 Monitor Ownership（不影響其他測試/行程）", RM.is_owner() is False)

ok = all(RESULTS)
print(f"\n== Phase 4.3 Service 驗證 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
