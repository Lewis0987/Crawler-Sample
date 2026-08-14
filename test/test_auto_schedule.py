# -*- coding: utf-8 -*-
"""
智慧排程自動建立報告 —— Phase 2 離線驗證（不連設備、不建立任何真實報告）
======================================================================
驗證 device_control_menu 的「監看決策層」auto_schedule_check()：
  - 觸發條件 A~F 是否正確（智慧模式 / 排程主開關 / 實際方向 / 無既有 Session / 非故障 / 非 pending）
  - Session 唯一性（已有 Session 時不得再建立）
  - Template List 僅作資訊、取得失敗不得阻擋建立
  - read_all() 不得含任何自動報告副作用（selftest / arming 不可誤觸發）
  - 不得新增任何背景 thread（自動偵測純前景）

作法：完全以 monkeypatch 取代「報告執行層」（_report_start_session）與 client，
      因此本測試**不會**建立資料夾、不會啟動背景取樣、不會呼叫任何 API。
執行：python test_auto_schedule.py
"""

import csv
import io
import json
import os
import re
import sys
import shutil
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import device_control_menu as M          # noqa: E402
import report_monitor as RM    # noqa: E402  Phase 4.2：Monitor Core 已抽離
import charge_discharge_report as CDR    # noqa: E402
import charge_discharge_report_config as CFG   # noqa: E402

_results = []
# 保留使用者在設定檔中的實際值，測試結束後還原（測試不得改變專案設定）
_CFG_ORIG = (CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG)

# Phase 4.4 起，啟動 Session 寫入者（_report_bg_start）與 atexit 保底都需要
# 持有 Monitor Ownership；Phase 4.6-B 又把 Writer Gate 前推到 auto_schedule_check，
# 因此本測試必須真的是 Owner，否則整段 E2E 會被正確地擋掉（而非測試通過）。
# 本測試模擬「本行程即 Owner」的正常情境。
# 「非 Owner 不得寫入」由 test_phase4_ownership.py 的 N/O 項與
# test_phase4_writer_gate.py 專責驗證。
#
# ⚠️ 必須先把 output root 導向臨時目錄**再**取得 Ownership：
#    _mutex_name() 由 output root 絕對路徑雜湊導出 —— 不改的話會去搶
#    正式部署的那一顆 Global Mutex，而 ESSAutoMonitor 服務正持有它，
#    結果變成「服務沒跑就 PASS、服務在跑就 FAIL」的環境依賴（Phase 4.6-B 修正）。
#    改用臨時 root 後，本測試的 Mutex 必然是自己專屬的一顆，
#    且 monitor_owner.json 也只會寫進臨時目錄，不污染正式 output。
_MUTEX_ROOT = tempfile.mkdtemp(prefix="p2_auto_sched_")
_BAK_OUTPUT_ROOT = RM._report_output_root
RM._report_output_root = lambda: _MUTEX_ROOT
_P44_OWNED, _P44_WHY = RM.acquire_ownership(role="test-auto-schedule")


def set_switches(auto_report=None, debug=None):
    """調整監看層開關（測試用；M.CDR_CFG 與 CFG 為同一 module 物件）。"""
    if auto_report is not None:
        CFG.AUTO_SCHEDULE_REPORT_ENABLED = auto_report
    if debug is not None:
        CFG.AUTO_MONITOR_DEBUG = debug


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# ---------------------------------------------------------------- 測試替身
class FakeSession:
    """假 Session：只記錄事件，不寫檔、不取樣。"""

    def __init__(self, sid="FAKE_20260730_120000_auto"):
        self.session_id = sid
        self.events = []

    def log_event(self, event_type, severity, detail=""):
        self.events.append((event_type, severity, detail))


class FakeClient:
    """假 client：只回傳預先設定的排程模板清單；raise=True 模擬 API 失敗。"""

    def __init__(self, templates=None, raise_error=False):
        self.templates = templates
        self.raise_error = raise_error
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(path)
        if self.raise_error:
            raise RuntimeError("simulated API failure")
        return self.templates


def reading(direction="charge", mode_code="smart", schedule=True, fault=False,
            power=20.2, soc=55.0):
    """合成 reading（欄位與 CDR.read_all 輸出一致的子集）。"""
    return {
        "communication_ok": True,
        "pcs_charging_flag": direction == "charge",
        "pcs_discharging_flag": direction == "discharge",
        "actual_active_power_kw": (power if direction == "charge"
                                   else (-power if direction == "discharge" else 0.0)),
        "pcs_fault_flag": fault,
        "pcs_status": "執行 / 併網",
        "pcs_control_mode_code": mode_code,
        "pcs_control_mode": {"smart": "智慧模式", "manual": "手動模式",
                             "none": "未開啟模式"}.get(mode_code, "未知"),
        "pcs_schedule_enabled": schedule,
        "pcs_power_control_mode": "交流有功",
        "soc_percent": soc,
    }


def reset_monitor(auto_report=False, debug=False):
    """每個情境前重設監看狀態與 _report（不觸碰真實 Session）；開關預設全關，需要時明確開啟。"""
    RM._report.update(session=None, thread=None, stop=None, finalized=False,
                     pending_end_reason=None, stopping=False)
    RM._auto.update(state=RM.AUTO_IDLE, direction=None, since=None, held_sec=0.0,
                   streak=0, template_status="unknown", last_note=None,
                   last_status=None, disabled=False, login_failed_at=None,
                   _pending_nl=False, _no_tty_warned=False,
                   cooldown_until=None, last_start_note=None, last_cooldown_note=None,
                   sched_ctx=None, sched_ctx_at=None,
                   # Phase 3.8：快取單位改為 plans；另有 continuity 事件 latch
                   sched_plans=None, sched_plans_ok=False, sched_plans_at=None,
                   transition_wait_latch=False)
    set_switches(auto_report=auto_report, debug=debug)


def run_check(r, client=None, source="auto", satisfy_hold=True):
    """
    呼叫監看層並攔截建立行為；回傳 (action, reason, started_args)。
    satisfy_hold=True（預設）：先把方向持續時間設成遠超 AUTO_HOLD_START_SEC，
      讓 Phase 3.4 的 Start Debounce 不影響「條件 A~F」本身的測試。
      要測 Start Debounce 本身時請傳 satisfy_hold=False 並自行設定 _auto["since"]。
    """
    if satisfy_hold and isinstance(r, dict):
        _d = RM._actual_direction(r)[0]
        if _d in ("charge", "discharge"):
            RM._auto.update(direction=_d, since=M.time.monotonic() - 3600,
                           held_sec=3600.0, streak=1)
    calls = []

    def fake_start(direction, mode_label, setpoint=None, origin="manual", extra_events=()):
        sess = FakeSession()
        for ev in extra_events:
            sess.log_event(*ev)
        calls.append({"direction": direction, "mode_label": mode_label,
                      "setpoint": setpoint, "origin": origin, "session": sess})
        RM._report["session"] = sess          # 模擬建立成功後的狀態
        return sess, True

    orig = RM._report_start_session
    RM._report_start_session = fake_start
    try:
        action, reason = RM.auto_schedule_check(r, client=client, source=source)
    finally:
        RM._report_start_session = orig
    return action, reason, calls


# ---------------------------------------------------------------- 情境 1~10
print("== Phase 2 離線驗證：智慧排程自動建立報告 ==\n")
print("[情境 1] 手動模式＋實際充電 → 不建立")
reset_monitor()
act, why, calls = run_check(reading(direction="charge", mode_code="manual"))
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為控制模式非智慧模式", why == "control_mode=manual")

print("\n[情境 2] 智慧模式＋排程關閉＋實際充電 → 不建立")
reset_monitor()
act, why, calls = run_check(reading(direction="charge", schedule=False))
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為排程主開關未開", why == "schedule_off")

print("\n[情境 3] 智慧模式＋排程開啟＋idle → 不建立")
reset_monitor()
act, why, calls = run_check(reading(direction="idle"))
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為方向非充/放電", why == "direction=idle")

print("\n[情境 3b] 智慧模式＋排程開啟＋方向 unknown（旗標缺失且功率為 0）→ 不建立")
reset_monitor()
r_unknown = reading(direction="idle")
r_unknown.update(pcs_charging_flag=None, pcs_discharging_flag=None,
                 actual_active_power_kw=None)
act, why, calls = run_check(r_unknown)
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)

print("\n[情境 4] 智慧模式＋排程開啟＋實際充電 → 建立一份報告")
reset_monitor(auto_report=True)
cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
act, why, calls = run_check(reading(direction="charge"), client=cl)
check(f"action=started（{act}）", act == "started" and why == "charge")
check("僅建立 1 份 Session", len(calls) == 1)
check("origin=scheduler（與手動流程共用同一入口）",
      calls and calls[0]["origin"] == "scheduler")
check("控制方式標籤取自 pcs_power_control_mode（交流有功）",
      calls and calls[0]["mode_label"] == "交流有功")
ev_types = [e[0] for e in calls[0]["session"].events] if calls else []
check(f"記錄 auto_charge_start 事件（{ev_types}）", "auto_charge_start" in ev_types)
check("Template 狀態記為 enabled", RM._auto["template_status"] == "enabled")
check(f"狀態機回到 RUNNING（{RM._auto['state']}）", RM._auto["state"] == RM.AUTO_RUNNING)

print("\n[情境 5] 智慧模式＋排程開啟＋實際放電 → 建立一份報告")
reset_monitor(auto_report=True)
cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
act, why, calls = run_check(reading(direction="discharge"), client=cl)
check(f"action=started（{act} / {why}）", act == "started" and why == "discharge")
ev_types = [e[0] for e in calls[0]["session"].events] if calls else []
check(f"記錄 auto_discharge_start 事件（{ev_types}）", "auto_discharge_start" in ev_types)

print("\n[情境 6] 已有 ReportSession，再次刷新 → 不建立第二份")
reset_monitor()
RM._report["session"] = FakeSession("EXISTING_SESSION")
act, why, calls = run_check(reading(direction="charge"))
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為已有 Session", why == "session_exists")
check("既有 Session 未被替換", RM._report["session"].session_id == "EXISTING_SESSION")

print("\n[情境 6b] 連續 5 次刷新（模擬 15s×5 自動刷新）→ 全程只建立 1 份")
reset_monitor(auto_report=True)
cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
created = 0
for _i in range(5):
    act, _why, calls = run_check(reading(direction="charge"), client=cl)
    created += len(calls)
check(f"5 次刷新共建立 {created} 份（需為 1）", created == 1)

print("\n[情境 6c] START_PENDING 中再次呼叫 → 不重複建立")
reset_monitor()
RM._auto["state"] = RM.AUTO_START_PENDING
act, why, calls = run_check(reading(direction="charge"))
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為 state=START_PENDING", why == "state=START_PENDING")

print("\n[情境 7] pcs_fault_flag=True → 不建立新報告")
reset_monitor()
act, why, calls = run_check(reading(direction="charge", fault=True))
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為 PCS 故障", why == "pcs_fault")

print("\n[情境 7b] 故障判斷語言無關：英文 badge + pcs_fault_flag=True → 仍不建立")
reset_monitor()
r_en = reading(direction="charge", fault=True)
r_en["pcs_status"] = "running / gridTied / charging / fault"
act, why, calls = run_check(r_en)
check(f"action=skipped（{why}）", act == "skipped" and why == "pcs_fault")

print("\n[情境 8] Template List API 失敗＋實際充電 → 仍建立，Template 記為 unknown")
reset_monitor(auto_report=True)
cl_fail = FakeClient(raise_error=True)
act, why, calls = run_check(reading(direction="charge"), client=cl_fail)
check(f"action=started（{act}）→ Template 失敗不得阻擋", act == "started")
check("Template 狀態記為 unknown", RM._auto["template_status"] == "unknown")
check("仍建立 1 份 Session", len(calls) == 1)

print("\n[情境 8b] Template List 取得成功但無啟用模板 → 仍建立，Template 記為 none")
reset_monitor(auto_report=True)
cl_none = FakeClient(templates=[{"tempId": "1", "enableFlag": 2}])
act, why, calls = run_check(reading(direction="charge"), client=cl_none)
check(f"action=started（{act}）→ 無啟用模板不得阻擋", act == "started")
check("Template 狀態記為 none", RM._auto["template_status"] == "none")

print("\n[情境 8c] Template List 回傳非 list（格式非預期）→ unknown，仍建立")
reset_monitor(auto_report=True)
cl_bad = FakeClient(templates={"unexpected": True})
act, why, calls = run_check(reading(direction="charge"), client=cl_bad)
check(f"action=started（{act}）", act == "started")
check("Template 狀態記為 unknown", RM._auto["template_status"] == "unknown")

print("\n[情境 9] read_all() 不得含任何自動報告副作用")
src_path = os.path.join(HERE, "charge_discharge_report.py")
with open(src_path, encoding="utf-8") as f:
    src = f.read()
check("charge_discharge_report.py 完全未提及 auto_schedule_check",
      "auto_schedule_check" not in src)
check("報告模組未匯出 auto_schedule_check（僅存在於 menu 監看層）",
      not hasattr(CDR, "auto_schedule_check"))
check("報告模組未匯入監看層（device_control_menu / report_monitor 皆無反向依賴）",
      "import device_control_menu" not in src and "import report_monitor" not in src)
# read_all() 本體不得出現建立 Session / 背景取樣的字樣
_ra = src[src.index("def read_all("):src.index("class EnergyAccumulator")]
check("read_all() 內未出現 ReportSession / _report_bg_start / log_event",
      not any(k in _ra for k in ("ReportSession", "_report_bg_start", "log_event")))

print("\n[情境 10] 自動偵測不得新增背景 thread")
reset_monitor()
before = threading.active_count()
cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
for _i in range(3):
    run_check(reading(direction="charge"), client=cl)
    run_check(reading(direction="idle"), client=cl)
after = threading.active_count()
check(f"thread 數不變（{before} → {after}）", before == after)
# 原始碼層級守門：監看層與刷新流程都不得自行開 thread（背景取樣 thread 只由 _report_bg_start 建立）
# Phase 4.2：監看層已抽離至 report_monitor.py，故 auto_schedule_check 的區段改掃該檔；
#            區段結尾改用 _monitor_status_line（在 report_monitor 內緊接其後的函式）。
with open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8") as f:
    _menu_src = f.read()
with open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8") as f:
    _mon_src = f.read()
# 原本 auto_schedule_check 與 dashboard_refresh 在 menu 內相鄰，可用單一區段涵蓋；
# 抽離後分屬兩檔，故改為分別取段後合併判定 —— 覆蓋範圍與原檢查完全相同。
_mon = _mon_src[_mon_src.index("def auto_schedule_check("):
                _mon_src.index("def _monitor_status_line(")]
_dash_seg = _menu_src[_menu_src.index("def dashboard_refresh("):
                      _menu_src.index("def report_status_line(")]
check("auto_schedule_check ~ dashboard_refresh 區段內無 threading.Thread(",
      "threading.Thread(" not in _mon and "threading.Thread(" not in _dash_seg)
# Phase 2 未新增任何 thread；Phase 4.2 後兩處既有 thread 分屬兩個模組 —
#   ① wait_and_verify._poller（控制後驗證進度列）→ 留在 device_control_menu
#   ② _report_bg_start._loop（報告背景取樣）    → 已隨 Monitor Core 移至 report_monitor
_thread_sites = _menu_src.count("threading.Thread(") + _mon_src.count("threading.Thread(")
check(f"兩模組合計建立 thread 的位置仍為 2 處既有（實際 {_thread_sites}）",
      _thread_sites == 2)
check("_poller（控制驗證）留在 menu、_loop（報告背景取樣）在 report_monitor，且未互相混入",
      _menu_src.count("threading.Thread(target=_poller") == 1
      and _mon_src.count("threading.Thread(target=_loop") == 1
      and "threading.Thread(target=_loop" not in _menu_src
      and "threading.Thread(target=_poller" not in _mon_src)

print("\n[情境 12] 安全開關 AUTO_SCHEDULE_REPORT_ENABLED=False → 條件成立也不建立")
reset_monitor(auto_report=False)
cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
act, why, calls = run_check(reading(direction="charge"), client=cl)
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為 auto_report_disabled", why == "auto_report_disabled")
check("開關關閉時完全不建立 Session", RM._report["session"] is None)
check(f"狀態機留在 IDLE（{RM._auto['state']}）", RM._auto["state"] == RM.AUTO_IDLE)
check("開關關閉時不必查 Template（不做多餘 API 呼叫）", cl.calls == [])
# 同一份 reading，只把開關打開 → 立刻建立（證明差異僅來自開關）
set_switches(auto_report=True)
act2, why2, calls2 = run_check(reading(direction="charge"), client=cl)
check(f"開關改 True → action=started（{act2}）", act2 == "started" and len(calls2) == 1)
# 開關**不得**影響手動流程：手動入口不讀這個開關
_ms = _menu_src if "_menu_src" in dir() else open(
    os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
_manual = _ms[_ms.index("def report_on_charge_discharge_control("):
              _ms.index("def report_on_stop_control(")]
check("手動控制自動跟隨流程未讀取 AUTO_SCHEDULE_REPORT_ENABLED",
      "AUTO_SCHEDULE_REPORT_ENABLED" not in _manual)
# Phase 4.2：_report_start_locked / auto_schedule_check 已隨 Monitor Core 移至 report_monitor
_start_fn = _mon_src[_mon_src.index("def _report_start_locked("):
                     _mon_src.index("def _last_active_direction(")]
check("手動報告入口（Menu 18）未讀取 AUTO_SCHEDULE_REPORT_ENABLED",
      "AUTO_SCHEDULE_REPORT_ENABLED" not in _start_fn)
# 實際「讀取」開關的只有 2 處：auto_schedule_check 的判斷（monitor）
#                              + _monitor_footer 的狀態顯示（menu）
_reads = (_ms.count('getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED"')
          + _mon_src.count('getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED"'))
check(f"兩模組合計只有 2 處讀取此開關（判斷＋footer 顯示；實際 {_reads}）", _reads == 2)
_footer_fn = _ms[_ms.index("def _monitor_footer("):_ms.index("def dashboard_refresh(")]
_check_fn = _mon_src[_mon_src.index("def auto_schedule_check("):
                     _mon_src.index("def _monitor_status_line(")]
check("兩處分別位於 auto_schedule_check（判斷）與 _monitor_footer（顯示）",
      'getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED"' in _check_fn
      and 'getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED"' in _footer_fn)

print("\n[情境 13] 登入失敗可恢復：60s 靜默重試 + 按 r 立即重試")
reset_monitor()
_orig_rc = RM._report_client
_login = {"ok": False, "calls": 0}


def _fake_report_client(quiet=False):
    _login["calls"] += 1
    return FakeClient() if _login["ok"] else None


RM._report_client = _fake_report_client
try:
    c, p = RM._monitor_client()
    check("首次登入失敗 → 回 None 並提示一次", c is None and p is True)
    check("已標記暫停（可恢復，非永久停用）", RM._auto["disabled"] is True)
    n_before = _login["calls"]
    c, p = RM._monitor_client()                       # 節流期間（<60s）
    check("60s 內自動重試被節流 → 不重試、不輸出",
          c is None and p is False and _login["calls"] == n_before)
    c, p = RM._monitor_client(force=True)             # 使用者按 r → 立即重試
    check("force=True（按 r）→ 立即重試", _login["calls"] == n_before + 1)
    check("重試仍失敗 → 不輸出（不洗畫面）", p is False)
    # 模擬節流時間到（把失敗時間往前推 60s）
    RM._auto["login_failed_at"] = M.time.monotonic() - RM.AUTO_LOGIN_RETRY_SEC - 1
    _login["ok"] = True
    c, p = RM._monitor_client()
    check("節流時間到且登入成功 → 恢復並提示一次", c is not None and p is True)
    check("恢復後 disabled=False（回到 15s 正常監看）", RM._auto["disabled"] is False)
    c, p = RM._monitor_client()
    check("恢復後不再重複提示", p is False)
finally:
    RM._report_client = _orig_rc

print("\n[情境 11] dashboard_refresh：手動與自動走同一函式，自動刷新不重複登入子程序")
reset_monitor()
_calls = {"refresh_status": 0, "render": 0, "read": 0}
_o = (M.refresh_status, M.render_dashboard, RM._report_client, RM._read_device_state,
      RM._report_start_session)


def _fake_refresh_status():
    _calls["refresh_status"] += 1
    return {}


def _fake_render(_status):
    _calls["render"] += 1


_fake_cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
_state = {"r": reading(direction="idle", mode_code="manual")}


def _fake_read(_client):
    _calls["read"] += 1
    return _state["r"]


M.refresh_status = _fake_refresh_status
M.render_dashboard = _fake_render
RM._report_client = lambda quiet=False: _fake_cl
RM._read_device_state = _fake_read
RM._report_start_session = lambda *a, **k: (FakeSession(), True)
try:
    r1, p1 = M.dashboard_refresh(auto=False)
    check("手動刷新：呼叫唯讀 scraper 子程序 + 重畫 Dashboard",
          _calls["refresh_status"] == 1 and _calls["render"] == 1)
    check("手動刷新：同時取樣一次（read_all）", _calls["read"] == 1)
    check("手動刷新 printed=True（有完整輸出）", p1 is True)

    r2, p2 = M.dashboard_refresh(auto=True)
    check("自動刷新：**不**呼叫唯讀 scraper 子程序（不重複登入、不重畫）",
          _calls["refresh_status"] == 1 and _calls["render"] == 1)
    check("自動刷新：仍取樣一次（共用已登入 client）", _calls["read"] == 2)
    check("自動刷新首次 printed=True（狀態摘要首次輸出）", p2 is True)

    r3, p3 = M.dashboard_refresh(auto=True)
    check("自動刷新：狀態未變 → printed=False（不洗畫面）", p3 is False)

    _state["r"] = reading(direction="charge", mode_code="manual")   # 狀態改變
    r4, p4 = M.dashboard_refresh(auto=True)
    check("自動刷新：狀態改變 → 重新輸出一行", p4 is True)
    check(f"共取樣 {_calls['read']} 次、子程序僅 1 次", _calls["read"] == 4)

    # AUTO_MONITOR_DEBUG=True（實機唯讀觀察）→ 每輪都印，讓使用者確認確實在更新
    set_switches(debug=True)
    _r, pa = M.dashboard_refresh(auto=True)
    _r, pb = M.dashboard_refresh(auto=True)
    check("AUTO_MONITOR_DEBUG=True → 每輪都輸出 Debug 行", pa is True and pb is True)
    set_switches(debug=False)

    # 登入失敗 → 暫停監看且只提示一次，不影響控制選單
    reset_monitor()
    RM._report_client = lambda quiet=False: None
    _r5, p5 = M.dashboard_refresh(auto=True)
    check("登入失敗 → 暫停自動偵測並提示一次", RM._auto["disabled"] is True and p5 is True)
    _r6, p6 = M.dashboard_refresh(auto=True)
    check("登入失敗後 60s 內不再重複提示、不再取樣", p6 is False)
finally:
    (M.refresh_status, M.render_dashboard, RM._report_client, RM._read_device_state,
     RM._report_start_session) = _o

print("\n[附加] Debounce 資料結構為時間型（monotonic），Phase 3 可直接沿用")
reset_monitor()
RM._auto_track_direction("charge", now=1000.0)
check("方向首次觀測 → held=0、since 記錄 monotonic 值",
      RM._auto["held_sec"] == 0.0 and RM._auto["since"] == 1000.0)
held, changed = RM._auto_track_direction("charge", now=1032.5)
check(f"同方向 32.5s 後 held={held}（時間差，非次數）", abs(held - 32.5) < 1e-9 and not changed)
check("streak 仍記錄次數作為輔助資訊", RM._auto["streak"] == 2)
held, changed = RM._auto_track_direction("idle", now=1040.0)
check(f"方向改變 → 重新起算（held={held}, changed={changed})",
      held == 0.0 and changed is True)
check(f"門檻常數集中於 config：start={CFG.AUTO_HOLD_START_SEC}s / "
      f"stop={CFG.AUTO_HOLD_STOP_SEC}s / cooldown={CFG.AUTO_COOLDOWN_SEC}s",
      CFG.AUTO_HOLD_START_SEC == 15 and CFG.AUTO_HOLD_STOP_SEC == 60
      and CFG.AUTO_COOLDOWN_SEC == 60)
check(f"自動刷新間隔 = {RM.AUTO_REFRESH_SEC}s", RM.AUTO_REFRESH_SEC == 15)

print("\n[附加] 訊息抑制：相同判斷結果不重複列印")
reset_monitor()
printed_1 = RM._auto_note("測試訊息 A")
printed_2 = RM._auto_note("測試訊息 A")
printed_3 = RM._auto_note("測試訊息 B")
check("相同訊息第二次不列印", printed_1 is True and printed_2 is False and printed_3 is True)

print("\n[附加] 報告模組不可用時 → noop，不拋例外")
reset_monitor()
# Phase 4.2：兩模組各自持有一份 _REPORT_AVAILABLE（各自 try/except 匯入 CDR）。
# 受測的是 RM.auto_schedule_check，它讀的是 RM 的那一份 —— 必須 patch RM，patch M 無效。
_orig_avail = RM._REPORT_AVAILABLE
RM._REPORT_AVAILABLE = False
try:
    act, why = RM.auto_schedule_check(reading(direction="charge"))
    check(f"action=noop（{act} / {why}）", act == "noop")
finally:
    RM._REPORT_AVAILABLE = _orig_avail

print("\n[附加] reading=None（取樣失敗）→ noop，不建立也不停止")
reset_monitor()
act, why, calls = run_check(None)
check(f"action=noop（{act} / {why}）", act == "noop" and not calls)

print("\n[附加] 鎖範圍：不得在等待/顯示期間持有鎖")
# Phase 4.2：_prompt_with_timeout / dashboard_refresh 仍在 menu；
#            _stop_and_finalize / report_pause / auto_schedule_check 等已在 report_monitor。
_src_menu = open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
_src_mon = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
_prompt_fn = _src_menu[_src_menu.index("def _prompt_with_timeout("):
                       _src_menu.index("def _exec_menu(")]
check("_prompt_with_timeout / _prompt_msvcrt（含 15s 等待與 sleep）完全不持有任何鎖",
      "_SESSION_LOCK" not in _prompt_fn and "_CLIENT_LOCK" not in _prompt_fn)
_dash_fn = _src_menu[_src_menu.index("def dashboard_refresh("):
                     _src_menu.index("def report_status_line(")]
check("dashboard_refresh 本體不直接持鎖（鎖只在被呼叫的取樣/建立函式內）",
      "with _SESSION_LOCK" not in _dash_fn and "with _CLIENT_LOCK" not in _dash_fn)
_stop_fn = _src_mon[_src_mon.index("def _stop_and_finalize("):
                    _src_mon.index("def report_pause(")]
_locked_head = _stop_fn[_stop_fn.index("with _SESSION_LOCK"):_stop_fn.index("try:")]
check("_stop_and_finalize：join 等待不在 _SESSION_LOCK 內", "join(" not in _locked_head)
check("_stop_and_finalize：finalize 不在 _SESSION_LOCK 內",
      "_report_finalize(" not in _locked_head)
_pause_fn = _src_mon[_src_mon.index("def report_pause("):
                     _src_mon.index("def _atexit_guard(")]
_pause_head = _pause_fn[_pause_fn.index("with _SESSION_LOCK"):_pause_fn.index("try:")]
check("report_pause：join 等待不在 _SESSION_LOCK 內", "join(" not in _pause_head)
_chk_fn = _src_mon[_src_mon.index("def auto_schedule_check("):
                   _src_mon.index("def _monitor_status_line(")]
_chk_locked = _chk_fn[_chk_fn.index("with _SESSION_LOCK"):_chk_fn.index("# ---- 建立報告")]
check("auto_schedule_check：template GET 與 sess.start() 不在 _SESSION_LOCK 內",
      "_auto_template_status(" not in _chk_locked
      and "_report_start_session(" not in _chk_locked)

print("\n[Phase 3.3] 背景取樣迴圈：停止判定一律委派 should_auto_end()")
_bg_fn = _src_mon[_src_mon.index("def _report_bg_start("):
                  _src_mon.index("def _stop_and_finalize(")]
_bg_body = _bg_fn[_bg_fn.index("def _loop():"):]          # 只看 _loop() 實作（排除 docstring）
check("_loop() 呼叫 sess.should_auto_end()（Phase 3.5 起帶 schedule_ctx）",
      "sess.should_auto_end(" in _bg_body)
for _kw in ("fault_stop", "communication_error", "battery_off", "timeout"):
    check(f"_loop() 內不再自行判斷 {_kw}", f'"{_kw}"' not in _bg_body)
check("_loop() 不再呼叫 _actual_direction（原故障判斷已移除）",
      "_actual_direction" not in _bg_body)
check("_loop() 不呼叫 _stop_and_finalize（避免 join 自己造成死鎖）",
      "_stop_and_finalize" not in _bg_body)
check("收尾走既有唯一入口 _report_finalize()",
      "_report_finalize(" in _src_mon[_src_mon.index("def _auto_finalize_from_loop("):
                                      _src_mon.index("def _report_bg_start(")])
check("全專案僅 should_auto_end() 一處做停止判定（menu 不含 idle_held 判斷式）",
      "idle_held_seconds() >=" not in _src_menu)
check("_loop() 有最外層例外防護（背景執行緒不靜默死亡）",
      _bg_body.count("except Exception") >= 3)
check("should_auto_end() 例外時不收尾、繼續記錄",
      "停止判定例外" in _bg_body)
check("pending_end_reason 優先於 auto_stop（Menu 7 人工停止不被覆蓋）",
      'if _report["pending_end_reason"]' in _bg_body)
check("AUTO_STOP_ENABLED=False 時只記錄不收尾",
      "AUTO_STOP_ENABLED" in _bg_body)
check(f"AUTO_STOP_ENABLED 正式預設為 True（實際 {CFG.AUTO_STOP_ENABLED}）",
      CFG.AUTO_STOP_ENABLED is True)

print("\n[Phase 3.3] _report_finalize 例外安全：失敗時還原 finalized 供 Menu 19 重試")
_fin_fn = _src_mon[_src_mon.index("def _report_finalize("):
                   _src_mon.index("def report_start(")]
check("finalize 失敗時把 finalized 還原為 False",
      '_report["finalized"] = False' in _fin_fn)
check("並提示可用 Menu 19 重試", "Menu 19 重試" in _fin_fn)

print("\n[Phase 3.3] auto_charge_stop / auto_discharge_stop 事件命名")
_lastdir = RM._last_active_direction


class _FakeSess:
    def __init__(self, dirs):
        self.samples = [{"charge_discharge_direction": d} for d in dirs]


check("最後非 idle 方向為 charge → auto_charge_stop",
      _lastdir(_FakeSess(["charge", "charge", "idle", "idle"])) == "charge")
check("最後非 idle 方向為 discharge → auto_discharge_stop",
      _lastdir(_FakeSess(["charge", "idle", "discharge", "idle"])) == "discharge")
check("全程皆 idle → 無法判定方向（回 None，事件退回 auto_stop）",
      _lastdir(_FakeSess(["idle", "idle"])) is None)
check("samples 為空 → 回 None（不拋例外）", _lastdir(_FakeSess([])) is None)
check("EVENT_FIELDS 未被修改（仍 5 欄）", len(CFG.EVENT_FIELDS) == 5)
check("SAMPLE_FIELDS 未被修改（仍 30 欄）", len(CFG.SAMPLE_FIELDS) == 30)

print("\n[Phase 3.3] STOP_PENDING 同步（只反映狀態，不做判定）")
_sync_fn = _src_mon[_src_mon.index("def _sync_stop_pending("):
                    _src_mon.index("def _auto_finalize_from_loop(")]
check("_sync_stop_pending 不做 finalize", "_report_finalize" not in _sync_fn)
check("_sync_stop_pending 不呼叫 should_auto_end（docstring 提及不算）",
      ".should_auto_end(" not in _sync_fn)


class _HeldSess:
    """只提供 idle 倒數與 session_id 的 Session 替身（供狀態同步／顯示測試）。"""

    def __init__(self, held, sid="HELD_SESSION"):
        self._held = held
        self.session_id = sid

    def idle_held_seconds(self):
        return self._held


reset_monitor()
RM._report["session"] = FakeSession("S1")
RM._auto["state"] = RM.AUTO_RUNNING
RM._sync_stop_pending(_HeldSess(12.0))
check("idle 累積中 → state 轉為 STOP_PENDING", RM._auto["state"] == RM.AUTO_STOP_PENDING)
RM._sync_stop_pending(_HeldSess(0.0))
check("方向回充/放（held=0）→ state 回 RUNNING", RM._auto["state"] == RM.AUTO_RUNNING)
RM._report["stopping"] = True
RM._auto["state"] = RM.AUTO_RUNNING
RM._sync_stop_pending(_HeldSess(99.0))
check("收尾進行中（stopping=True）→ 不覆寫狀態", RM._auto["state"] == RM.AUTO_RUNNING)
RM._report["stopping"] = False

print("\n[Phase 3.3] Dashboard 顯示 idle 倒數")
reset_monitor()
RM._report["session"] = _HeldSess(42.0)
_line = RM._monitor_status_line(reading(direction="idle"), "auto")
check(f"狀態行含 idle=42s/{CFG.AUTO_HOLD_STOP_SEC}s（{_line[-40:]}）",
      f"idle=42s/{CFG.AUTO_HOLD_STOP_SEC}s" in _line)
RM._report["session"] = None
_line2 = RM._monitor_status_line(reading(direction="idle"), "auto")
check("無 Session 時不顯示 idle 倒數", "idle=" not in _line2)
reset_monitor()

print("\n[Phase 3.6 / D-1] enum 與 as_int 共用（config 為唯一定義）")
_msrc_d1 = open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
# Phase 4.2：D-1（enum/as_int 共用）與 atexit／resume 的程式碼已隨 Monitor Core
#            移至 report_monitor.py，故需一併掃描該檔；_SCHED_CD（TUI 顯示表）仍在 menu。
_dsrc_d1 = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
_vsrc_d1 = open(os.path.join(HERE, "phase2_real_trigger_validation.py"),
                encoding="utf-8").read()
check("config 定義 SCHED_CD_CODE", hasattr(CFG, "SCHED_CD_CODE"))
check("config 定義 as_int()", callable(getattr(CFG, "as_int", None)))
check("menu / report_monitor 皆不再自行宣告 SCHED_CD_CODE",
      not re.search(r"^SCHED_CD_CODE\s*=", _msrc_d1, re.M)
      and not re.search(r"^SCHED_CD_CODE\s*=", _dsrc_d1, re.M))
check("menu / report_monitor 皆不再自行定義 _as_int_code()",
      not re.search(r"^def _as_int_code", _msrc_d1, re.M)
      and not re.search(r"^def _as_int_code", _dsrc_d1, re.M))
check("驗證腳本不再自行宣告 SCHED_CD / 定義 _as_int()",
      not re.search(r"^SCHED_CD\s*=\s*\{", _vsrc_d1, re.M)
      and not re.search(r"^def _as_int\(", _vsrc_d1, re.M))
check("排程層（report_monitor）使用 CDR_CFG.SCHED_CD_CODE / CDR_CFG.as_int",
      "CDR_CFG.SCHED_CD_CODE" in _dsrc_d1 and "CDR_CFG.as_int" in _dsrc_d1)
check("menu 中文顯示用 _SCHED_CD 保留（TUI 用途）",
      re.search(r"^_SCHED_CD\s*=", _msrc_d1, re.M) is not None)
check("as_int：int / 字串數字 → int",
      (CFG.as_int(2), CFG.as_int("2"), CFG.as_int(" 3 ")) == (2, 2, 3))
check("as_int：None / 非數值 → None",
      CFG.as_int(None) is None and CFG.as_int("x") is None and CFG.as_int([]) is None)
check("as_int：bool 明確排除（True 不可變成 1＝充電）",
      CFG.as_int(True) is None and CFG.as_int(False) is None)
check("SCHED_CD_CODE 對照正確",
      CFG.SCHED_CD_CODE == {1: "charge", 2: "discharge", 3: "none"})
check("命名整理：_auto_enabled_plan_state()（三態，非 bool）",
      callable(getattr(RM, "_auto_enabled_plan_state", None))
      and not hasattr(RM, "_auto_template_status")
      and not hasattr(M, "_auto_template_status"))

print("\n[Phase 3.6] atexit 保底：行程結束時把 Session 標記為 paused")
check("Monitor Core 模組層已註冊 atexit，且 menu 不再重複註冊",
      "atexit.register(_atexit_guard)" in _dsrc_d1
      and "atexit.register(" not in _msrc_d1)
_guard_src = _dsrc_d1[_dsrc_d1.index("def _atexit_guard("):
                      _dsrc_d1.index("atexit.register(_atexit_guard)")]
check("atexit 用 mark_paused()（不重產 Excel），不用 pause()",
      "mark_paused" in _guard_src and ".pause()" not in _guard_src)
check("atexit 不做網路 I/O（不呼叫 finalize / sample_once / read_all）",
      not any(k in _guard_src for k in ("_report_finalize", "sample_once",
                                        "read_all", "_stop_and_finalize")))
check("atexit 的 join 有硬上限（ATEXIT_JOIN_TIMEOUT_SEC）",
      "ATEXIT_JOIN_TIMEOUT_SEC" in _guard_src)
check("atexit 全程 try/except（不得拋例外）", "except Exception" in _guard_src)
check(f"ATEXIT_JOIN_TIMEOUT_SEC = 2（實際 {CFG.ATEXIT_JOIN_TIMEOUT_SEC}）",
      CFG.ATEXIT_JOIN_TIMEOUT_SEC == 2)


class _MarkSess:
    """可記錄 mark_paused 是否被呼叫的替身。"""

    def __init__(self, sid="MARK"):
        self.session_id = sid
        self.marked = 0

    def mark_paused(self):
        self.marked += 1
        return "paused"


reset_monitor()
check("無 Session → atexit 不動作、不拋例外", RM._atexit_guard() is None)
_ms = _MarkSess()
RM._report.update(session=_ms, thread=None, stop=None, finalized=False)
RM._atexit_guard()
check(f"有 Session → 呼叫 mark_paused 一次（{_ms.marked}）", _ms.marked == 1)
RM._report.update(session=_ms, finalized=True)
RM._atexit_guard()
check("已 finalized → 不重複標記（idempotent）", _ms.marked == 1)
reset_monitor()
RM._report["session"] = FakeSession("NO_MARK")     # 測試替身沒有 mark_paused
check("Session 無 mark_paused 方法 → 不拋例外", RM._atexit_guard() is None)
reset_monitor()

print("\n[Phase 3.6] 孤兒分類：只記錄，不改變 Resume 流程")
import tempfile as _tf                                        # noqa: E402
from datetime import datetime as _DTO, timedelta as _TD       # noqa: E402


def _mk_state(last_offset_sec):
    d = _tf.mkdtemp(prefix="p36_")
    ts = (_DTO.now() - _TD(seconds=last_offset_sec)).strftime("%Y-%m-%d %H:%M:%S")
    with io.open(os.path.join(d, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as fh:
        json.dump({"status": "recording", "last_sample_time": ts}, fh)
    return d


check(f"ORPHAN_GAP_SEC = 300（實際 {CFG.ORPHAN_GAP_SEC}）", CFG.ORPHAN_GAP_SEC == 300)
_g = RM._orphan_gap_seconds(_mk_state(299))
check(f"斷線 299s → gap≈299（{_g:.0f}）且未逾門檻", 295 <= _g <= 305 and _g <= 300)
_g = RM._orphan_gap_seconds(_mk_state(600))
check(f"斷線 600s → gap≈600（{_g:.0f}）逾門檻 → 判定孤兒", _g > CFG.ORPHAN_GAP_SEC)
_d_no = _tf.mkdtemp(prefix="p36_")
with io.open(os.path.join(_d_no, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as fh:
    json.dump({"status": "recording"}, fh)
check("last_sample_time 缺失 → 回 None（保守，不誤判）",
      RM._orphan_gap_seconds(_d_no) is None)
_d_bad = _tf.mkdtemp(prefix="p36_")
with io.open(os.path.join(_d_bad, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as fh:
    json.dump({"status": "recording", "last_sample_time": "not-a-time"}, fh)
check("last_sample_time 格式非預期 → 回 None（不拋例外）",
      RM._orphan_gap_seconds(_d_bad) is None)
check("無 session_state.json → 回 None",
      RM._orphan_gap_seconds(_tf.mkdtemp(prefix="p36_")) is None)
# 流程未被改變：resume 仍照常續接，不因孤兒而 finalize
def _func_src(src, name):
    """擷取單一頂層函式的原始碼（def 行 + 其後所有縮排/空白行），避免掃到後續註解區塊。"""
    lines = src[src.index(f"def {name}("):].splitlines(keepends=True)
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        out.append(ln)
    return "".join(out)


_res_fn = _func_src(_dsrc_d1, "report_resume_on_launch")
check("Resume 流程未加入 finalize（Phase 3.6 只分類不處置）",
      "_report_finalize" not in _res_fn and "_stop_and_finalize" not in _res_fn)
check("孤兒時仍呼叫 _report_bg_start（照常續接）", "_report_bg_start(sess)" in _res_fn)
check("孤兒時記 orphan_detected 事件", 'log_event("orphan_detected"' in _res_fn)
check("未加入 ORPHAN_AUTO_FINALIZE（Phase 3.6 刻意不做）",
      not hasattr(CFG, "ORPHAN_AUTO_FINALIZE"))
check("report_pause() 未加入 idle→finalize 判斷",
      "_report_finalize" not in _func_src(_dsrc_d1, "report_pause"))

print("\n[Phase 3.5] Schedule Context —— 常數與預設值")
check(f"AUTO_NEXT_PLAN_GAP_SEC = 180（Phase 3.8 啟用；實際 {CFG.AUTO_NEXT_PLAN_GAP_SEC}）",
      CFG.AUTO_NEXT_PLAN_GAP_SEC == 180)
check(f"AUTO_SCHEDULE_CONTINUITY_GRACE_SEC = 180"
      f"（實際 {CFG.AUTO_SCHEDULE_CONTINUITY_GRACE_SEC}）",
      CFG.AUTO_SCHEDULE_CONTINUITY_GRACE_SEC == 180)
check("AUTO_HOLD_STOP_SEC 維持 60（continuity 不得靠放大全域 idle 門檻解決）",
      CFG.AUTO_HOLD_STOP_SEC == 60)
check(f"AUTO_WINDOW_GRACE_SEC = 120（實際 {CFG.AUTO_WINDOW_GRACE_SEC}）",
      CFG.AUTO_WINDOW_GRACE_SEC == 120)
check(f"AUTO_WINDOW_CACHE_SEC = 60（實際 {CFG.AUTO_WINDOW_CACHE_SEC}）",
      CFG.AUTO_WINDOW_CACHE_SEC == 60)

print("\n[Phase 3.5] compute_schedule_ctx() —— 純函式，窗口判定")
from datetime import datetime as _DT     # noqa: E402


def _plan(name, ptype, days_or_date, s, e, cd="charge"):
    return {"name": name, "plan_type": ptype,
            "days": [x for x in days_or_date.split(",") if x], "date": days_or_date,
            "start_min": RM._hhmm_to_min(s), "end_min": RM._hhmm_to_min(e),
            "start_txt": s, "end_txt": e, "cd": cd}


# 2026-08-07 為週五（isoweekday=5）；模板執行日含 5
_p_week = [_plan("Charge", 1, "3,4,5,1,2", "17:00", "17:50")]
for _t, _exp in (("16:00", False), ("17:00", True), ("17:30", True),
                 ("17:50", False), ("18:00", False)):
    _c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 7, int(_t[:2]), int(_t[3:])), grace_sec=0)
    check(f"每週排程 {_t} → in_window={_c['in_window']}（預期 {_exp}）",
          _c["in_window"] is _exp)
_c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 7, 17, 30), grace_sec=0)
check(f"命中時 current_plan 有值（{_c['current_plan']}）",
      _c["current_plan"] == "Charge 17:00~17:50 charge")
check("source 為 ok", _c["source"] == "ok")
# 執行日不含週六（8/8 isoweekday=6）
_c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 8, 17, 30), grace_sec=0)
check("執行日不符（週六）→ in_window=False", _c["in_window"] is False)
# 停用模板不會進入 plans（由 fetch_enabled_plans 過濾）→ 空清單
_c = RM.compute_schedule_ctx([], _DT(2026, 8, 7, 17, 30), grace_sec=0)
check("無啟用排程 → in_window=False、next=None",
      _c["in_window"] is False and _c["next_plan_in_sec"] is None)

print("\n[Phase 3.5] 跨午夜時段（22:00~02:00，僅週五啟用）")
_p_night = [_plan("Night", 1, "5", "22:00", "02:00")]
for _d, _t, _exp in ((7, "21:00", False), (7, "23:00", True),
                     (8, "01:00", True), (8, "03:00", False)):
    _c = RM.compute_schedule_ctx(_p_night, _DT(2026, 8, _d, int(_t[:2]), int(_t[3:])),
                                grace_sec=0)
    check(f"08-{_d:02d} {_t} → in_window={_c['in_window']}（預期 {_exp}）",
          _c["in_window"] is _exp)
check("→ 00:00~02:00 需回看『前一天』是否為執行日（已驗證）", True)

print("\n[Phase 3.5] 按日期排程（planType=2，MM-DD）")
_p_date = [_plan("ByDate", 2, "08-07", "10:00", "11:00")]
for _d, _exp in ((6, False), (7, True), (8, False)):
    _c = RM.compute_schedule_ctx(_p_date, _DT(2026, 8, _d, 10, 30), grace_sec=0)
    check(f"08-{_d:02d} 10:30 → in_window={_c['in_window']}（預期 {_exp}）",
          _c["in_window"] is _exp)

print("\n[Phase 3.5] 邊界與寬限（AUTO_WINDOW_GRACE_SEC）")
_c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 7, 16, 59), grace_sec=0)
check("無寬限：窗口前 1 分鐘 → out", _c["in_window"] is False)
_c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 7, 16, 59), grace_sec=120)
check("寬限 120s：窗口前 1 分鐘 → in（吸收設備/PC 時鐘落差）", _c["in_window"] is True)
_c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 7, 17, 51), grace_sec=120)
check("寬限 120s：窗口後 1 分鐘 → in", _c["in_window"] is True)
_c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 7, 17, 53), grace_sec=120)
check("寬限 120s：窗口後 3 分鐘 → out", _c["in_window"] is False)

print("\n[Phase 3.5] next_plan_in_sec")
_c = RM.compute_schedule_ctx(_p_week, _DT(2026, 8, 7, 16, 0), grace_sec=0)
check(f"16:00 距 17:00 開始 = 3600s（實際 {_c['next_plan_in_sec']}）",
      _c["next_plan_in_sec"] == 3600.0)
# 相鄰排程：Charge 17:00~17:50、Discharge 17:55~18:10
_p_adj = _p_week + [_plan("Discharge", 1, "3,4,5,1,2", "17:55", "18:10", "discharge")]
_c = RM.compute_schedule_ctx(_p_adj, _DT(2026, 8, 7, 17, 51), grace_sec=0)
check(f"17:51（Charge 剛結束）→ 距下段 Discharge {_c['next_plan_in_sec']}s（預期 240）",
      _c["next_plan_in_sec"] == 240.0)
check("此時 in_window=False（兩段之間的空檔）", _c["in_window"] is False)

print("\n[Phase 3.5] should_auto_end() 與 ctx 的關係（gap=0 → 行為同 Phase 3.4）")
_bak_gap = CFG.AUTO_NEXT_PLAN_GAP_SEC


def _sess_idle(sec):
    s = CDR.ReportSession("auto", None, "交流有功", object(),
                          os.path.join(os.path.dirname(HERE), "output", "test_output",
                                       "p35_tmp"))
    s._idle_since = M.time.monotonic() - sec
    return s


_ctx_soon = {"in_window": False, "current_plan": None,
             "current_plan_started_sec": None, "current_plan_cd": None,
             "next_plan_in_sec": 60.0, "next_plan_cd": "charge", "source": "ok"}
try:
    CFG.AUTO_NEXT_PLAN_GAP_SEC = 0                      # 停用 → Case A 完全跳過
    check("gap=0 + ctx 顯示下段 60s 後開始 → 仍照常收尾（行為同 3.4）",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_soon) == (True, "auto_stop"))
    check("gap=0 + ctx=None → 照常收尾",
          _sess_idle(70).should_auto_end(schedule_ctx=None) == (True, "auto_stop"))
    check("gap=0 + idle 未達門檻 → 不收尾",
          _sess_idle(30).should_auto_end(schedule_ctx=_ctx_soon) == (False, None))
    CFG.AUTO_NEXT_PLAN_GAP_SEC = 300                    # 啟用
    check("gap=300 + 下段 60s 後開始 → 阻擋收尾（維持同一份報告）",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_soon)
          == (False, "schedule_continuity_wait"))
    _ctx_far = dict(_ctx_soon, next_plan_in_sec=301.0)
    check("gap=300 + 下段 301s 後開始 → 正常收尾",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_far) == (True, "auto_stop"))
    _ctx_none = dict(_ctx_soon, next_plan_in_sec=None)
    check("gap=300 + 無下段排程 → 正常收尾",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_none) == (True, "auto_stop"))
    _ctx_unk = dict(_ctx_soon, source="unknown")
    check("gap=300 + source=unknown（API 失敗）→ 退回 3.4 行為，正常收尾",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_unk) == (True, "auto_stop"))
    check("gap=300 + ctx=None → 退回 3.4 行為，正常收尾",
          _sess_idle(70).should_auto_end(schedule_ctx=None) == (True, "auto_stop"))
    check("gap>0 不影響白名單 critical（fault 仍優先）",
          (lambda s: (s.stop_reasons.append("pcs_fault"),
                      s.should_auto_end(schedule_ctx=_ctx_soon))[1])(_sess_idle(70))
          == (True, "fault_stop"))
finally:
    CFG.AUTO_NEXT_PLAN_GAP_SEC = _bak_gap
check(f"測試後 AUTO_NEXT_PLAN_GAP_SEC 已還原（{CFG.AUTO_NEXT_PLAN_GAP_SEC}）",
      CFG.AUTO_NEXT_PLAN_GAP_SEC == _bak_gap)

# ======================================================================
# Phase 3.8：相鄰智慧排程銜接保護（continuity）
# ----------------------------------------------------------------------
# 實機案例（2026-08-10）：16:00~16:10 discharge、16:11~16:21 charge，
# PCS 切換延遲使 16:12:23 時 idle 已達 60s，但 charge 要到 16:13:37 才出現
# → Phase 3.7 之前會在此收尾，把一組連續測試拆成兩份報告。
# 關鍵：16:12:23 時 16:11 的 charge **已經開始**，因此它不是 next_plan
#       而是 current/in-window plan → 只看 next_plan_in_sec 永遠修不好。
# ======================================================================
print("\n[Phase 3.8] 相鄰排程銜接：compute_schedule_ctx() 結構化欄位")


def _plan(name, s_txt, e_txt, cd, days="1234567"):
    _s, _e = RM._hhmm_to_min(s_txt), RM._hhmm_to_min(e_txt)
    return {"name": name, "plan_type": 1, "days": list(days), "date": "",
            "start_min": _s, "end_min": _e, "start_txt": s_txt, "end_txt": e_txt, "cd": cd}


# 實機案例的排程組合
_P_CASE = [_plan("D", "16:00", "16:10", "discharge"), _plan("C", "16:11", "16:21", "charge")]
# 單筆排程（無下一段）—— 驗證上一筆尾端 grace 不得造成 continuity
_P_SOLO = [_plan("D", "16:00", "16:10", "discharge")]


def _ctx_at(plans, hh, mm, ss=0):
    return RM.compute_schedule_ctx(plans, _DT(2026, 8, 10, hh, mm, ss))


# ---- ③ grace window 重疊時取 nominal start 最新者（不是 API 第一筆）----
_c = _ctx_at(_P_CASE, 16, 11, 20)
check("重疊 grace（16:11:20 同時命中 16:00 尾端與 16:11 前段）→ 取 start 最新的 charge",
      _c["current_plan_cd"] == "charge" and abs(_c["current_plan_started_sec"] - 20.0) < 1)
_c_rev = RM.compute_schedule_ctx(list(reversed(_P_CASE)), _DT(2026, 8, 10, 16, 11, 20))
check("命中結果與 API 回傳順序無關（反轉 plans 仍取 charge）",
      _c_rev["current_plan_cd"] == "charge"
      and _c_rev["current_plan"] == _c["current_plan"])
# ---- 結構化欄位本身 ----
_c = _ctx_at(_P_CASE, 16, 12, 23)
check("16:12:23：current_plan_cd=charge、started≈83s（實機案例的判定依據）",
      _c["current_plan_cd"] == "charge" and abs(_c["current_plan_started_sec"] - 83.0) < 1)
check("16:12:23：16:11 charge 已開始 → 不再是 next_plan（證明只看 next 修不好）",
      _c["next_plan_in_sec"] > 3600)
_c = _ctx_at(_P_CASE, 16, 10, 40)
check("16:10:40：下段尚未開始 → next_plan_in_sec≈20、next_plan_cd=charge",
      abs(_c["next_plan_in_sec"] - 20.0) < 1 and _c["next_plan_cd"] == "charge")
check("尚未開始時 current_plan_started_sec 為負（落在前緣 grace 內）",
      _c["current_plan_started_sec"] < 0)
_c = RM.compute_schedule_ctx([], _DT(2026, 8, 10, 16, 12, 23))
check("無排程 → 結構化欄位皆 None、in_window=False",
      _c["current_plan_cd"] is None and _c["current_plan_started_sec"] is None
      and _c["next_plan_cd"] is None and _c["in_window"] is False)

print("\n[Phase 3.8] should_auto_end()：continuity Case A / Case B")
_bak_gap2 = CFG.AUTO_NEXT_PLAN_GAP_SEC
_bak_grace2 = CFG.AUTO_SCHEDULE_CONTINUITY_GRACE_SEC
_bak_to2 = CFG.AUTO_END_TIMEOUT_SEC
_WAIT = (False, "schedule_continuity_wait")
_STOP = (True, "auto_stop")
try:
    CFG.AUTO_NEXT_PLAN_GAP_SEC = 180
    CFG.AUTO_SCHEDULE_CONTINUITY_GRACE_SEC = 180

    # ① 實機案例：16:12:23 idle 已達 60s，但 16:11 charge 剛開始 83s
    check("① 實機案例 16:12:23（idle 70s）→ schedule_continuity_wait，不 finalize",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 12, 23)) == _WAIT)
    # ② 單筆排程結束：只剩上一筆尾端 grace → 不得 continuity
    _c2 = _ctx_at(_P_SOLO, 16, 11, 20)
    check("② 單筆排程 16:11:20：in_window 仍為 True（上一筆尾端 grace）",
          _c2["in_window"] is True)
    check("② 但 started≈680s > 180 → 正常 auto_stop（不因尾端 grace 延後收尾）",
          _sess_idle(70).should_auto_end(schedule_ctx=_c2) == _STOP)
    # ④ Case A：下一段尚未開始
    check("④ 16:10:40（下段 20s 後開始）→ Case A continuity wait",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 10, 40)) == _WAIT)
    # ⑤ continuity 逾時：nominal start 已過 181s 仍未充電
    check("⑤ 16:14:01（charge 已開始 181s > grace 180）→ continuity 逾時，正常收尾",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 14, 1)) == _STOP)
    check("⑤ 邊界：started=180 恰等於 grace → 仍 wait",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 14, 0)) == _WAIT)
    # ⑥ none（不充不放）不算連續排程
    _P_NONE = [_plan("D", "16:00", "16:10", "discharge"), _plan("N", "16:11", "16:21", "none")]
    check("⑥ 下一筆為 none（Case B）→ 不 continuity，正常收尾",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_NONE, 16, 12, 23)) == _STOP)
    check("⑥ 下一筆為 none（Case A，尚未開始）→ 不 continuity",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_NONE, 16, 10, 40)) == _STOP)
    # ⑦ 間隔過長
    _P_FAR = [_plan("D", "16:00", "16:10", "discharge"), _plan("C", "16:30", "16:40", "charge")]
    check("⑦ 下一筆 10 分鐘後才開始 → 不 continuity（兩份報告才是正確的）",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_FAR, 16, 11, 20)) == _STOP)
    # ⑨ fault 優先於 continuity
    _sf = _sess_idle(70)
    _sf.stop_reasons.append("pcs_fault")
    check("⑨ continuity 條件成立但發生 fault → fault_stop 優先，不得延後停止",
          _sf.should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 12, 23)) == (True, "fault_stop"))
    for _cond, _exp in (("communication_error", "communication_error"),
                        ("battery_off", "battery_off")):
        _sx = _sess_idle(70)
        _sx.stop_reasons.append(_cond)
        check(f"⑨ continuity 中 {_cond} 仍優先 → {_exp}",
              _sx.should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 12, 23)) == (True, _exp))
    # ⑩ 硬性 timeout 不得被 continuity 短路
    CFG.AUTO_END_TIMEOUT_SEC = 1
    _st = _sess_idle(70)
    _st.start_dt = _st.start_dt - _TD(seconds=999)       # _elapsed() 以 start_dt 計算
    check("⑩ AUTO_END_TIMEOUT_SEC 生效時，continuity 不得阻擋 timeout",
          _st.should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 12, 23)) == (True, "timeout"))
    CFG.AUTO_END_TIMEOUT_SEC = 0
    check("⑩ timeout=0（不限）→ 回到 continuity wait",
          _sess_idle(70).should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 12, 23)) == _WAIT)
    # idle 未達門檻時 continuity 完全不參與（reason 維持 None）
    check("idle 未達 60s → (False, None)，不是 continuity_wait",
          _sess_idle(30).should_auto_end(schedule_ctx=_ctx_at(_P_CASE, 16, 12, 23))
          == (False, None))
    # 決策點唯一性：continuity 判定是 should_auto_end 的私有 helper
    check("continuity 判定屬 should_auto_end（_schedule_continuity_wait 為其 helper）",
          hasattr(CDR.ReportSession, "_schedule_continuity_wait"))
    _src_cdr = open(os.path.join(HERE, "charge_discharge_report.py"),
                    encoding="utf-8").read()
    # _func_src() 只適用**頂層**函式（遇到未縮排的行才停）；這是 class 內的方法，
    # 需切到「下一個同縮排的 def」為止，否則會連整個 class 其餘方法一起掃進來。
    _m0 = _src_cdr.index("    def _schedule_continuity_wait(")
    _cw_fn = _src_cdr[_m0:_src_cdr.index("\n    def ", _m0 + 10)]
    check("_schedule_continuity_wait 為純判定（不寫檔／不呼叫 log_event）",
          "log_event" not in _cw_fn and "_dump_json" not in _cw_fn)
    check("_schedule_continuity_wait 不自行讀排程 API（排程讀取屬監看層）",
          "fetch_enabled_plans" not in _cw_fn and "client" not in _cw_fn)
    check("continuity 不 parse current_plan 顯示字串（只讀結構化欄位）",
          'get("current_plan")' not in _cw_fn)
finally:
    CFG.AUTO_NEXT_PLAN_GAP_SEC = _bak_gap2
    CFG.AUTO_SCHEDULE_CONTINUITY_GRACE_SEC = _bak_grace2
    CFG.AUTO_END_TIMEOUT_SEC = _bak_to2
check("測試後 continuity 常數已還原",
      CFG.AUTO_NEXT_PLAN_GAP_SEC == _bak_gap2
      and CFG.AUTO_SCHEDULE_CONTINUITY_GRACE_SEC == _bak_grace2
      and CFG.AUTO_END_TIMEOUT_SEC == _bak_to2)

print("\n[Phase 3.8] ⑧ 人工停止優先於 continuity")
# Menu 7 走 _stop_and_finalize()，**完全不經過 should_auto_end()** → continuity 不可能延後它。
_m7 = _func_src(_src_menu, "report_on_stop_control")
check("⑧ Menu 7 直接呼叫 _stop_and_finalize（不經 should_auto_end）",
      "_stop_and_finalize(" in _m7 and "should_auto_end" not in _m7)
check("⑧ Menu 7 先設 pending_end_reason=control_stop（結束原因不被 auto_stop 蓋掉）",
      'pending_end_reason"] = "control_stop"' in _m7)
check("⑧ device_control_menu 不含任何 continuity 判定（決策未外流）",
      "schedule_continuity_wait" not in _src_menu
      and "AUTO_SCHEDULE_CONTINUITY_GRACE_SEC" not in _src_menu)

print("\n[Phase 3.8] ⑪ ctx 倒數新鮮度：快取 plans，不快取時間計算結果")
reset_monitor()


class _CountingSchedCli:
    """最小排程 API 假物件（_SchedCli 定義於後方，此處自備）；只計算 API 呼叫次數。"""

    def __init__(self):
        self.calls = 0

    def get(self, path, **_k):
        self.calls += 1
        if path == RM._SCHED_TPL_LIST:
            return [{"tempId": "1", "tempName": "C", "planType": 1,
                     "executeTime": "1,2,3,4,5,6,7", "enableFlag": 1}]
        return [{"startTime": "16:11", "endTime": "16:21", "chargeOrDischarge": 1}]


_cli_f = _CountingSchedCli()
_x1 = RM._schedule_window_ctx(_cli_f, now=_DT(2026, 8, 10, 16, 0, 0))
_calls_after_first = _cli_f.calls
_x2 = RM._schedule_window_ctx(_cli_f, now=_DT(2026, 8, 10, 16, 0, 50))
check(f"⑪ 快取有效期內未重查 API（{_calls_after_first} → {_cli_f.calls}）",
      _cli_f.calls == _calls_after_first)
check(f"⑪ 但倒數已隨 now 更新（{_x1['next_plan_in_sec']:.0f}s → "
      f"{_x2['next_plan_in_sec']:.0f}s，差 50s）",
      abs((_x1["next_plan_in_sec"] - _x2["next_plan_in_sec"]) - 50.0) < 1)
_x3 = RM._schedule_window_ctx(_cli_f, now=_DT(2026, 8, 10, 16, 12, 23))
check("⑪ 進入窗口後 current_plan_started_sec 亦即時重算（≈83s，非沿用舊值）",
      abs(_x3["current_plan_started_sec"] - 83.0) < 1)
check("⑪ 全程只查一次 API（快取單位＝plans）", _cli_f.calls == _calls_after_first)
_src_swc = _func_src(_src_mon, "_schedule_window_ctx")
check("⑪ _schedule_window_ctx 不再以 sched_ctx 作為快取來源",
      'return _auto["sched_ctx"]' not in _src_swc)

print("\n[Phase 3.8] ⑫ schedule_transition_wait 事件 latch")
reset_monitor()


class _LatchSess:
    """只提供 latch 測試所需的最小介面（不建立任何真實 Session／不寫檔）。"""

    def __init__(self, idle):
        self.idle, self.events, self.samples = idle, [], []

    def idle_held_seconds(self):
        return self.idle

    def log_event(self, ev, _lvl, _msg):
        self.events.append(ev)


_ctx_w = _ctx_at(_P_CASE, 16, 12, 23)
_ls = _LatchSess(70.0)
for _ in range(12):                                   # 背景取樣器每 5s 一輪，模擬 1 分鐘
    RM._sync_transition_wait(_ls, "schedule_continuity_wait", _ctx_w)
check(f"⑫ 等待期間 12 輪只寫 1 筆 schedule_transition_wait（實際 {len(_ls.events)}）",
      _ls.events == ["schedule_transition_wait"])
_ls.idle = 0.0                                        # 設備真正進入 charge → idle 歸零
RM._sync_transition_wait(_ls, None, _ctx_w)
check("⑫ idle 歸零（已進入下一段充/放電）→ latch 解除",
      RM._auto["transition_wait_latch"] is False and len(_ls.events) == 1)
_ls.idle = 70.0                                       # 下一次 transition（charge → discharge）
RM._sync_transition_wait(_ls, "schedule_continuity_wait", _ctx_w)
check(f"⑫ 下一次 transition 可再記一筆（實際 {len(_ls.events)}）", len(_ls.events) == 2)
_ls2 = _LatchSess(70.0)
RM._auto["transition_wait_latch"] = False
RM._sync_transition_wait(_ls2, "auto_stop", _ctx_w)
check("⑫ 非 continuity_wait 的 reason 不寫事件", _ls2.events == [])
_src_stw = _func_src(_src_mon, "_sync_transition_wait")
check("⑫ _sync_transition_wait 只記事件、不做停止判定",
      "_report_finalize" not in _src_stw and ".should_auto_end(" not in _src_stw)
reset_monitor()

print("\n[Phase 3.8] ⑬⑭ 銜接後維持同一份 Session")
# 唯一的 finalize 路徑是背景迴圈的 `if end:` 分支；continuity 回傳 end=False，
# 因此 Session 不會被收尾，也就不可能建立第二份報告。
_bg2 = _src_mon[_src_mon.index("def _report_bg_start("):
                _src_mon.index("def _stop_and_finalize(")]
check("⑬ 背景迴圈只在 end=True 時 finalize（continuity 回 False → 不收尾）",
      "if end:" in _bg2 and "_auto_finalize_from_loop(sess, reason)" in _bg2)
check("⑬ 迴圈不因 continuity 而 break（Session 續存，samples 繼續累積）",
      _bg2.count("break") == 1)
check("⑬ 背景迴圈本身不做窗口判定（in_window 只出現在 ctx 計算層）",
      "in_window" not in _bg2)
for _dirs, _ctxp in (("discharge → charge", _P_CASE),
                     ("charge → discharge",
                      [_plan("C", "16:00", "16:10", "charge"),
                       _plan("D", "16:11", "16:21", "discharge")])):
    _bak_g3 = CFG.AUTO_NEXT_PLAN_GAP_SEC
    CFG.AUTO_NEXT_PLAN_GAP_SEC = 180
    try:
        check(f"⑬⑭ {_dirs}：16:12:23 → continuity wait（同一 Session，不拆兩份）",
              _sess_idle(70).should_auto_end(
                  schedule_ctx=_ctx_at(_ctxp, 16, 12, 23)) == _WAIT)
    finally:
        CFG.AUTO_NEXT_PLAN_GAP_SEC = _bak_g3
check("⑬⑭ 方向切換事件由 sample_once 記錄（未因 continuity 改動既有事件邏輯）",
      "discharge_start" in _src_cdr and "charge_start" in _src_cdr)

print("\n[Phase 3.5] fetch_enabled_plans() —— 只取啟用模板、格式防禦")


class _SchedCli:
    def __init__(self, tpls, items, fail=False):
        self.tpls, self.items, self.fail = tpls, items, fail
        self.calls = 0

    def get(self, path, **_k):
        self.calls += 1
        if self.fail:
            raise RuntimeError("API down")
        if path == RM._SCHED_TPL_LIST:
            return self.tpls
        for tid, v in self.items.items():
            if path == RM._SCHED_ITEM_LIST.format(tid=tid):
                return v
        return []


_T_EN = [{"tempId": "1", "tempName": "Charge", "planType": 1,
          "executeTime": "3,4,5,1,2", "enableFlag": 1}]
_I_OK = {"1": [{"startTime": "17:00", "endTime": "17:50", "chargeOrDischarge": 1}]}
_pl, _ok = RM.fetch_enabled_plans(_SchedCli(_T_EN, _I_OK), RM._SCHED_TPL_LIST,
                                 RM._SCHED_ITEM_LIST)
check(f"啟用模板 → 取得 {len(_pl)} 筆項目、ok={_ok}", _ok is True and len(_pl) == 1)
check("方向正規化為 charge", _pl[0]["cd"] == "charge")
_pl2, _ = RM.fetch_enabled_plans(
    _SchedCli(_T_EN, {"1": [{"startTime": "17:00", "endTime": "17:50",
                             "chargeOrDischarge": "2"}]}),
    RM._SCHED_TPL_LIST, RM._SCHED_ITEM_LIST)
check("chargeOrDischarge 為字串 \"2\" → 正規化為 discharge", _pl2[0]["cd"] == "discharge")
_T_DIS = [dict(_T_EN[0], enableFlag=2)]
_pl3, _ = RM.fetch_enabled_plans(_SchedCli(_T_DIS, _I_OK), RM._SCHED_TPL_LIST,
                                RM._SCHED_ITEM_LIST)
check("停用模板（enableFlag=2）不列入", _pl3 == [])
_pl4, _ok4 = RM.fetch_enabled_plans(_SchedCli(_T_EN, _I_OK, fail=True),
                                   RM._SCHED_TPL_LIST, RM._SCHED_ITEM_LIST)
check("API 失敗 → ok=False", _ok4 is False)
_pl5, _ok5 = RM.fetch_enabled_plans(_SchedCli({"bad": 1}, {}), RM._SCHED_TPL_LIST,
                                   RM._SCHED_ITEM_LIST)
check("模板清單格式非預期 → ok=False", _ok5 is False)
_pl6, _ = RM.fetch_enabled_plans(
    _SchedCli(_T_EN, {"1": [{"startTime": "bad", "endTime": "17:50"}]}),
    RM._SCHED_TPL_LIST, RM._SCHED_ITEM_LIST)
check("時間格式非預期的項目被略過（不拋例外）", _pl6 == [])
check("_hhmm_to_min 邊界：'00:00'→0、'23:59'→1439、'24:00'→None、''→None",
      RM._hhmm_to_min("00:00") == 0 and RM._hhmm_to_min("23:59") == 1439
      and RM._hhmm_to_min("24:00") is None and RM._hhmm_to_min("") is None)

print("\n[Phase 3.5] 快取：60 秒內只查一次；idle=0 時完全不查")
reset_monitor()
_cli = _SchedCli(_T_EN, _I_OK)
_ctx1 = RM._schedule_window_ctx(_cli)
_n1 = _cli.calls
_ctx2 = RM._schedule_window_ctx(_cli)
check(f"快取有效期內第二次呼叫不再查 API（{_n1} → {_cli.calls}）", _cli.calls == _n1)
# Phase 3.8：快取單位是 plans，不是 ctx —— ctx 每次都以「現在」重算（見下方倒數新鮮度測試）
check("快取的是 plans 而非 ctx（兩次為不同 ctx 物件）", _ctx1 is not _ctx2)
check("同一份 plans 被重用（未重新查 API）", RM._auto["sched_plans"] is not None)
RM._auto["sched_plans_at"] = M.time.monotonic() - CFG.AUTO_WINDOW_CACHE_SEC - 1
RM._schedule_window_ctx(_cli)
check(f"快取過期後重新查詢（{_n1} → {_cli.calls}）", _cli.calls > _n1)
reset_monitor()
check("client=None → source=unknown（不拋例外）",
      RM._schedule_window_ctx(None)["source"] == "unknown")
reset_monitor()
_bg = _src_mon[_src_mon.index("def _report_bg_start("):
               _src_mon.index("def _stop_and_finalize(")]
check("背景迴圈僅在 idle_held_seconds() > 0 時才查排程",
      "if sess.idle_held_seconds() > 0:" in _bg)
check("背景迴圈把 ctx 傳給 should_auto_end（決策仍在該函式）",
      "should_auto_end(schedule_ctx=sched_ctx)" in _bg)
check("背景迴圈本身不做窗口判定（不含 in_window 條件式）",
      "in_window" not in _bg)

print("\n[Phase 3.5] Debug 顯示 window=")
check("in_window → window=in(...)",
      "window=in(Charge 17:00~17:50 charge)" in RM._schedule_ctx_text(
          {"in_window": True, "current_plan": "Charge 17:00~17:50 charge",
           "next_plan_in_sec": None, "source": "ok"}))
check("窗口外有下段 → window=out next=240s",
      RM._schedule_ctx_text({"in_window": False, "current_plan": None,
                            "next_plan_in_sec": 240.0, "source": "ok"})
      == " window=out next=240s")
check("窗口外無下段 → window=out next=none",
      RM._schedule_ctx_text({"in_window": False, "current_plan": None,
                            "next_plan_in_sec": None, "source": "ok"})
      == " window=out next=none")
check("API 失敗 → window=unknown",
      RM._schedule_ctx_text({"source": "unknown"}) == " window=unknown")
check("非 dict → 空字串（不拋例外）", RM._schedule_ctx_text(None) == "")

print("\n[Phase 3.4] 常數集中於 config，menu 不得自行宣告")
_msrc = open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
# Phase 4.2：Debounce/Cooldown 的實作與門檻使用點已隨 Monitor Core 移至 report_monitor.py
_dsrc = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
for _c in ("AUTO_HOLD_START_SEC", "AUTO_HOLD_STOP_SEC", "AUTO_COOLDOWN_SEC"):
    check(f"menu / report_monitor 皆未自行宣告 {_c}（避免與 config 生效值矛盾）",
          not re.search(rf"^{_c}\s*=", _msrc, re.M)
          and not re.search(rf"^{_c}\s*=", _dsrc, re.M))
    check(f"config 有定義 {_c}", hasattr(CFG, _c))
check(f"AUTO_HOLD_START_SEC = 15（實際 {CFG.AUTO_HOLD_START_SEC}）",
      CFG.AUTO_HOLD_START_SEC == 15)
check(f"AUTO_HOLD_STOP_SEC  = 60（實際 {CFG.AUTO_HOLD_STOP_SEC}）",
      CFG.AUTO_HOLD_STOP_SEC == 60)
check(f"AUTO_COOLDOWN_SEC   = 60（實際 {CFG.AUTO_COOLDOWN_SEC}）",
      CFG.AUTO_COOLDOWN_SEC == 60)
check("狀態機含 START_HOLD / COOLDOWN",
      RM.AUTO_START_HOLD == "START_HOLD" and RM.AUTO_COOLDOWN == "COOLDOWN")

print("\n[Phase 3.4] Start Debounce：monotonic 計時，與呼叫頻率解耦")


def _hold_run(direction="charge", held=None, **over):
    """把方向持續時間直接設成指定秒數（模擬 monotonic 經過），再跑一次判斷。"""
    reset_monitor(auto_report=True)
    r = reading(direction=direction, **over)
    RM._auto_track_direction(direction)                    # 首次觀測 → held=0
    if held is not None:                                  # 以 monotonic 回推，非靠呼叫次數
        RM._auto["since"] = M.time.monotonic() - held
    return run_check(r, client=FakeClient(templates=[{"tempId": "1", "enableFlag": 1}]),
                     satisfy_hold=False)


_a, _w, _c = _hold_run(held=0.0)
check(f"charge 持續 0s（未達 15s）→ 不建立（{_w}）",
      _a == "skipped" and _w.startswith("start_hold=") and not _c)
check(f"狀態為 START_HOLD（{RM._auto['state']}）", RM._auto["state"] == RM.AUTO_START_HOLD)
_a, _w, _c = _hold_run(held=14.9)
check(f"charge 持續 14.9s（未達門檻）→ 仍不建立（{_w}）", _a == "skipped" and not _c)
_a, _w, _c = _hold_run(held=15.0)
check(f"charge 持續 15.0s（達門檻）→ 建立（{_a}）", _a == "started" and len(_c) == 1)
_a, _w, _c = _hold_run(direction="discharge", held=20.0)
check(f"discharge 持續 20s → 建立（{_a}）", _a == "started" and len(_c) == 1)
# 與呼叫次數無關：呼叫 10 次但實際只過 1 秒 → 仍不建立
reset_monitor(auto_report=True)
RM._auto_track_direction("charge")
RM._auto["since"] = M.time.monotonic() - 1.0
_created = 0
for _i in range(10):
    _a, _w, _c = run_check(reading(direction="charge"),
                           client=FakeClient(templates=[{"tempId": "1", "enableFlag": 1}]),
                           satisfy_hold=False)          # 自行控制 hold，不讓 helper 覆寫
    _created += len(_c)
check(f"連呼叫 10 次但實際僅過 1 秒 → 仍不建立（建立 {_created} 份）", _created == 0)
check("→ Debounce 依 monotonic 實際時間，不依呼叫/刷新次數", True)

print("\n[Phase 3.4] START_HOLD 期間任一守門條件失效 → 回 IDLE")
for _label, _kw, _expect in (
        ("方向消失（idle）", dict(direction="idle"), "direction=idle"),
        ("智慧模式關閉", dict(mode_code="manual"), "control_mode=manual"),
        ("排程主開關關閉", dict(schedule=False), "schedule_off"),
        ("PCS 故障", dict(fault=True), "pcs_fault")):
    reset_monitor(auto_report=True)
    RM._auto_track_direction("charge")
    RM._auto["since"] = M.time.monotonic() - 5.0
    RM._auto["state"] = RM.AUTO_START_HOLD                  # 先處於累積中
    _a, _w, _c = run_check(reading(**_kw),
                           client=FakeClient(templates=[{"tempId": "1", "enableFlag": 1}]))
    check(f"START_HOLD →{_label}→ 不建立（{_w}）",
          _a == "skipped" and _w == _expect and not _c)
    check(f"　　　　　　　→ 狀態回 IDLE（{RM._auto['state']}）",
          RM._auto["state"] == RM.AUTO_IDLE)
# 已存在 Session
reset_monitor(auto_report=True)
RM._auto["state"] = RM.AUTO_START_HOLD
RM._report["session"] = FakeSession("EXIST")
_a, _w, _c = run_check(reading(direction="charge"))
check(f"START_HOLD →已存在 Session→ 不建立（{_w}）",
      _a == "skipped" and _w == "session_exists" and not _c)
check(f"　　　　　　　→ 狀態轉 RUNNING（{RM._auto['state']}）",
      RM._auto["state"] == RM.AUTO_RUNNING)

print("\n[Phase 3.4] COOLDOWN：期間一律不得建立，不論條件如何變化")
reset_monitor(auto_report=True)
_cd_sec = RM._auto_start_cooldown()
check(f"_auto_start_cooldown() 設定 {_cd_sec}s 冷卻", _cd_sec == CFG.AUTO_COOLDOWN_SEC)
check(f"cooldown_until 為 monotonic 值（{type(RM._auto['cooldown_until']).__name__}）",
      isinstance(RM._auto["cooldown_until"], float))
check(f"剩餘秒數約 {CFG.AUTO_COOLDOWN_SEC}s",
      CFG.AUTO_COOLDOWN_SEC - 1 <= RM._auto_cooldown_remaining() <= CFG.AUTO_COOLDOWN_SEC)
for _label, _kw in (("charge", dict(direction="charge")),
                    ("discharge", dict(direction="discharge")),
                    ("智慧模式重開", dict(direction="charge", mode_code="smart")),
                    ("排程重開", dict(direction="charge", schedule=True))):
    RM._auto_track_direction(_kw.get("direction", "charge"))
    RM._auto["since"] = M.time.monotonic() - 999          # 即使 hold 早已達標
    _a, _w, _c = run_check(reading(**_kw),
                           client=FakeClient(templates=[{"tempId": "1", "enableFlag": 1}]))
    check(f"冷卻期間 {_label} → 不建立（{_w}）",
          _a == "skipped" and _w.startswith("cooldown=") and not _c)
check(f"冷卻期間狀態為 COOLDOWN（{RM._auto['state']}）", RM._auto["state"] == RM.AUTO_COOLDOWN)
# 冷卻期滿 → 立即可重新建立
RM._auto["cooldown_until"] = M.time.monotonic() - 0.01
RM._auto_track_direction("charge")
RM._auto["since"] = M.time.monotonic() - 30
_a, _w, _c = run_check(reading(direction="charge"),
                       client=FakeClient(templates=[{"tempId": "1", "enableFlag": 1}]))
check(f"冷卻期滿 → 立即可重新建立（{_a}）", _a == "started" and len(_c) == 1)
check("冷卻期滿後 cooldown_until 已清除", RM._auto["cooldown_until"] is None)
check("冷卻不影響 Auto Stop（should_auto_end 未被 cooldown 干預）",
      "cooldown" not in _dsrc[_dsrc.index("def _report_bg_start("):
                              _dsrc.index("def _stop_and_finalize(")])

print("\n[Phase 3.4] Auto Stop → Cooldown 可連續循環兩輪")
reset_monitor(auto_report=True)
_cycle_ok = True
for _round in (1, 2):
    RM._auto.update(cooldown_until=None, state=RM.AUTO_IDLE)
    RM._auto_track_direction("charge")
    RM._auto["since"] = M.time.monotonic() - 30
    _a, _w, _c = run_check(reading(direction="charge"),
                           client=FakeClient(templates=[{"tempId": "1", "enableFlag": 1}]))
    _cycle_ok = _cycle_ok and _a == "started" and len(_c) == 1
    RM._report["session"] = None                          # 模擬 auto_stop 收尾完成
    RM._auto_start_cooldown()                             # 收尾後進入冷卻
    _a2, _w2, _c2 = run_check(reading(direction="charge"),
                              client=FakeClient(templates=[{"tempId": "1", "enableFlag": 1}]))
    _cycle_ok = _cycle_ok and _a2 == "skipped" and _w2.startswith("cooldown=") and not _c2
    check(f"第 {_round} 輪：建立 → 收尾 → 冷卻阻擋（{_a} / {_w2}）", _cycle_ok)
check("兩輪循環後狀態仍為 COOLDOWN（未殘留錯誤狀態）",
      RM._auto["state"] == RM.AUTO_COOLDOWN)

print("\n[Phase 3.4] 顯示：state 名稱 + 對應倒數（三者擇一）")
reset_monitor()
RM._auto.update(state=RM.AUTO_START_HOLD, held_sec=8.0)
_l = RM._monitor_status_line(reading(direction="charge"), "auto")
check(f"START_HOLD 顯示 state 與 hold=8s/15s（{_l[-46:]}）",
      "state=START_HOLD" in _l and f"hold=8s/{CFG.AUTO_HOLD_START_SEC}s" in _l)
RM._auto.update(state=RM.AUTO_COOLDOWN, cooldown_until=M.time.monotonic() + 42)
_l = RM._monitor_status_line(reading(direction="idle"), "auto")
check(f"COOLDOWN 顯示 state 與 cooldown=42s/60s（{_l[-46:]}）",
      "state=COOLDOWN" in _l and f"cooldown=42s/{CFG.AUTO_COOLDOWN_SEC}s" in _l)
RM._auto.update(state=RM.AUTO_RUNNING, cooldown_until=None)
RM._report["session"] = _HeldSess(30.0)
_l = RM._monitor_status_line(reading(direction="idle"), "auto")
check(f"RUNNING 顯示 idle=30s/60s（不顯示 hold/cooldown）",
      f"idle=30s/{CFG.AUTO_HOLD_STOP_SEC}s" in _l
      and "hold=" not in _l and "cooldown=" not in _l)
reset_monitor()
check("_auto_clear_notes() 一併清除 start/cooldown 去重 key",
      all(k in _dsrc[_dsrc.index("def _auto_clear_notes("):
                     _dsrc.index("def _auto_cooldown_remaining(")]
          for k in ("last_start_note", "last_cooldown_note")))
check("cooldown_until 存於 _auto（Phase 4 可直接沿用，非 Dashboard 專用變數）",
      "cooldown_until" in RM._auto)

print("\n[Phase 3.3] Dashboard debug line 顯示 run / stby 旗標（顯示強化，不參與判定）")
reset_monitor()
_r_chg = reading(direction="charge")
_r_chg.update(pcs_running_flag=True, pcs_standby_flag=False)
_l_chg = RM._monitor_status_line(_r_chg, "auto")
check(f"充電中含 run=True stby=False（{_l_chg[:78]}…）",
      " run=True" in _l_chg and " stby=False" in _l_chg)
check("充電中 chg=True dir=charge", " chg=True" in _l_chg and " dir=charge" in _l_chg)
_r_stop = reading(direction="idle")
_r_stop.update(pcs_running_flag=False, pcs_standby_flag=False)
_l_stop = RM._monitor_status_line(_r_stop, "auto")
check(f"停止後含 run=False stby=False（{_l_stop[:78]}…）",
      " run=False" in _l_stop and " stby=False" in _l_stop)
_r_stby = reading(direction="idle")
_r_stby.update(pcs_running_flag=True, pcs_standby_flag=True)
check("待機時含 stby=True",
      " stby=True" in RM._monitor_status_line(_r_stby, "auto"))
_r_none = reading(direction="idle")
_r_none.update(pcs_running_flag=None, pcs_standby_flag=None)
_l_none = RM._monitor_status_line(_r_none, "auto")      # 欄位缺失不得拋例外
check(f"旗標為 None 時安全顯示 run=None stby=None（不拋例外）",
      " run=None" in _l_none and " stby=None" in _l_none)
_r_miss = {k: v for k, v in reading(direction="idle").items()
           if k not in ("pcs_running_flag", "pcs_standby_flag")}
check("reading 完全缺少該兩欄位時仍可產生狀態行（不拋例外）",
      " run=None" in RM._monitor_status_line(_r_miss, "auto"))
# Phase 4.2：_monitor_status_line 已移至 report_monitor；其後緊接 _print_report_result
_ml_fn = _src_mon[_src_mon.index("def _monitor_status_line("):
                  _src_mon.index("def _print_report_result(")]
# 只檢查「實際呼叫」形式（CDR.xxx / .xxx()）；註解中提及函式名不算
check("顯示強化未涉入判定（_monitor_status_line 不呼叫 should_auto_end / pcs_is_idle_state）",
      ".should_auto_end(" not in _ml_fn and "CDR.pcs_is_idle_state" not in _ml_fn)

print("\n[Phase 3.3] 停止提示去重：背景取樣器與 Dashboard 各用獨立 key，互不干擾")
reset_monitor()
_STOP_MSG = "[REPORT] 自動停止條件成立（auto_stop）但 AUTO_STOP_ENABLED=False → 僅記錄，不收尾"
_DASH_MSG = "[AUTO] 已有進行中的報告 Session（S1）→ 不建立新報告"
check("_auto 具備獨立的 last_stop_note key", "last_stop_note" in RM._auto)
# 模擬實際節奏：背景取樣器每輪（5s）印停止提示，Dashboard 每 3 輪（15s）印一般提示
_n_stop = _n_dash = 0
for _cy in range(1, 7):
    if RM._auto_note(_STOP_MSG, key="last_stop_note"):
        _n_stop += 1
    if _cy % 3 == 0 and RM._auto_note(_DASH_MSG):
        _n_dash += 1
check(f"6 輪背景刷新 → 停止提示只印 1 次（實際 {_n_stop}）", _n_stop == 1)
check(f"2 輪 Dashboard 刷新 → 一般提示只印 1 次（實際 {_n_dash}）", _n_dash == 1)
check("兩者互不影響：last_note 與 last_stop_note 各自保存",
      RM._auto["last_note"] == _DASH_MSG and RM._auto["last_stop_note"] == _STOP_MSG)
# 一般提示內容改變時仍應重印（去重不能過度抑制）
check("一般提示內容改變 → 仍會重印", RM._auto_note("[AUTO] 換一則訊息") is True)
check("停止提示不受一般提示影響（仍被抑制）",
      RM._auto_note(_STOP_MSG, key="last_stop_note") is False)

print("\n[Phase 3.3] _auto_clear_notes()：Session reset 時統一清除所有去重 key")
RM._auto.update(last_note="a", last_status="b", last_stop_note="c")
RM._auto_clear_notes()
check("三個 key 一次全部清除",
      RM._auto["last_note"] is None and RM._auto["last_status"] is None
      and RM._auto["last_stop_note"] is None)
# Phase 4.2：提示去重狀態（_auto）與其清除路徑已全部隨 Monitor Core 移至 report_monitor.py
_menu_src2 = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
check("清除邏輯集中：全檔只有 _auto_clear_notes() 內指派這些 key",
      _menu_src2.count("last_stop_note=None") == 1
      and _menu_src2.count("last_note=None") == 1
      and _menu_src2.count("last_status=None") == 1)
check("所有回到 AUTO_IDLE / Session 起訖的路徑都呼叫 _auto_clear_notes()（≥6 處）",
      _menu_src2.count("_auto_clear_notes()") >= 6)
for _fn_name, _next_fn in (("def _auto_finalize_from_loop(", "def _report_bg_start("),
                           ("def _stop_and_finalize(", "def report_pause("),
                           ("def report_pause(", "def report_stop("),
                           ("def _monitor_client(", "def _auto_control_mode_code("),
                           ("def _report_bg_start(", "def _stop_and_finalize(")):
    _seg = _menu_src2[_menu_src2.index(_fn_name):_menu_src2.index(_next_fn)]
    check(f"{_fn_name.replace('def ', '').replace('(', '')}() 內有呼叫 _auto_clear_notes()",
          "_auto_clear_notes()" in _seg)

print("\n[Phase 3.3] 新 Session 不沿用上一份 Session 的停止提示")


class _NoopSess:
    """最小 Session 替身：不取樣、不結束（僅供驗證 _report_bg_start 的初始化行為）。"""
    session_id = "NOOP_SESSION"
    samples = []

    def sample_once(self):
        return None, None

    def should_auto_end(self):
        return False, None

    def idle_held_seconds(self):
        return 0.0


reset_monitor()
RM._auto["last_stop_note"] = _STOP_MSG            # 上一份 Session 遺留
_noop = _NoopSess()
RM._report_bg_start(_noop)                        # ← 正式路徑：新 Session 起始
try:
    check("新 Session 起始後 last_stop_note 已清除（不沿用上一份）",
          RM._auto["last_stop_note"] is None)
    check("新 Session 可再次正常印出停止提示",
          RM._auto_note(_STOP_MSG, key="last_stop_note") is True)
finally:
    if RM._report["stop"] is not None:
        RM._report["stop"].set()
    if RM._report["thread"] is not None:
        RM._report["thread"].join(timeout=5)
    reset_monitor()
check("測試用背景執行緒已收尾", RM._report["thread"] is None)

print("\n[Phase 3.3] 正式流程 Regression（E2E）："
      "Charge → Idle → Auto Stop → Finalize → Summary → Excel")
# 走**完全真實**的正式路徑：真的 ReportSession + 真的 _report_bg_start 背景執行緒 +
# 真的 should_auto_end() + 真的 _report_finalize() → 真的產出 Summary / Excel。
# 只把「設備讀值」與「Cell 抓取」換成合成資料（不連設備），並把節奏調快以縮短測試時間。
_e2e_ok = False
_e2e_root = os.path.join(os.path.dirname(HERE), "output", "test_output", "phase33_e2e")
_bak = (CDR.read_all, CDR._fetch_cell_packs, CFG.SAMPLE_INTERVAL_SEC,
        CFG.AUTO_HOLD_STOP_SEC, CFG.AUTO_STOP_ENABLED)
try:
    import shutil
    import time as _t
    if os.path.isdir(_e2e_root):
        shutil.rmtree(_e2e_root)
    os.makedirs(_e2e_root, exist_ok=True)

    _phase = {"dir": "charge"}          # 由測試控制設備方向：先 charge，之後轉 idle

    def _e2e_reading(_client):
        d = _phase["dir"]
        p = 20.0 if d == "charge" else 0.0
        return {
            "communication_ok": True, "raw_source_time": "",
            "pcs_charging_flag": d == "charge", "pcs_discharging_flag": False,
            "soc_percent": 50.0, "battery_voltage_v": 890.0,
            "battery_current_a": round(p * 1000 / 890.0, 2),
            "rack_max_temperature_c": 30.0, "rack_min_temperature_c": 25.0,
            "battery_status": "充電" if d == "charge" else "待機",
            "actual_active_power_kw": p, "actual_reactive_power_kvar": 0.0,
            "calculated_power_kw": p,
            "pcs_status": "執行 / 併網" if d == "charge" else "停止 / 併網",
            "pcs_control_mode": "智慧模式", "pcs_control_mode_code": "smart",
            "pcs_work_mode": "併網", "pcs_power_control_mode": "交流有功",
            "pcs_manual_switch": 0, "pcs_schedule_enabled": True,
            "pcs_fault_flag": False,
            "pcs_running_flag": d == "charge",          # idle 階段：設備已停止
            "pcs_standby_flag": d != "charge",
            "battery_power_status": "已上電",
            "device_daily_charge_kwh": 1.0, "device_daily_discharge_kwh": 0.5,
            "alarm_rows": [], "alarm_total": 0,
        }

    CDR.read_all = _e2e_reading
    CDR._fetch_cell_packs = lambda _c: (CDR._fake_pack_data(), None)
    CFG.SAMPLE_INTERVAL_SEC = 0.2       # 加速：背景取樣 0.2s
    CFG.AUTO_HOLD_STOP_SEC = 1.0        # 加速：idle 持續 1s 即收尾
    CFG.AUTO_STOP_ENABLED = True

    reset_monitor()
    _sess = CDR.ReportSession("auto", None, "交流有功", object(), _e2e_root)
    _sess.start()
    _folder = _sess.folder
    RM._report_bg_start(_sess)                      # ← 正式背景取樣執行緒
    RM._auto["state"] = RM.AUTO_RUNNING
    _t.sleep(1.0)                                  # 充電階段
    _n_charge = len(_sess.samples)
    check(f"充電階段持續取樣（{_n_charge} 筆）且未收尾",
          _n_charge >= 2 and RM._report["session"] is not None)

    _phase["dir"] = "idle"                         # ← 排程結束，設備回 idle
    _deadline = _t.monotonic() + 15
    while RM._report["session"] is not None and _t.monotonic() < _deadline:
        _t.sleep(0.1)
    check("idle 持續達門檻 → 背景自動收尾（_report[\"session\"] 已清空）",
          RM._report["session"] is None)
    check(f"背景執行緒已結束（{RM._report['thread']}）", RM._report["thread"] is None)
    # Phase 3.4 起：auto_stop 收尾後進入 COOLDOWN（非直接 IDLE），冷卻期滿才回 IDLE
    check(f"狀態機進入 COOLDOWN（{RM._auto['state']}）", RM._auto["state"] == RM.AUTO_COOLDOWN)
    check(f"冷卻已啟動（剩餘 {RM._auto_cooldown_remaining():.0f}s）",
          RM._auto_cooldown_remaining() > 0)

    _st = json.load(io.open(os.path.join(_folder, CFG.FILE_SESSION_STATE), encoding="utf-8"))
    check(f"session_state.status == completed（{_st.get('status')}）",
          _st.get("status") == CFG.SESSION_COMPLETED)
    _sm = json.load(io.open(os.path.join(_folder, CFG.FILE_SUMMARY), encoding="utf-8"))
    _end_reason = (_sm.get("session") or {}).get("end_reason")
    check(f"summary.end_reason == auto_stop（{_end_reason}）", _end_reason == "auto_stop")
    for _f in (CFG.FILE_SUMMARY, CFG.FILE_STATISTICS, CFG.FILE_SAMPLES,
               CFG.FILE_EVENTS, CFG.FILE_XLSX, CFG.FILE_SESSION_STATE):
        check(f"輸出檔存在：{_f}", os.path.exists(os.path.join(_folder, _f)))
    with io.open(os.path.join(_folder, CFG.FILE_EVENTS), encoding="utf-8-sig") as _fh:
        _ev = list(csv.DictReader(_fh))
    _types = [e["event_type"] for e in _ev]
    check(f"events 含 auto_charge_stop 恰 1 次（{_types.count('auto_charge_stop')}）",
          _types.count("auto_charge_stop") == 1)
    check(f"events 含 session_end 恰 1 次（{_types.count('session_end')}）",
          _types.count("session_end") == 1)
    check(f"events 欄位仍為 {len(CFG.EVENT_FIELDS)} 欄",
          list(_ev[0].keys()) == CFG.EVENT_FIELDS)
    with io.open(os.path.join(_folder, CFG.FILE_SAMPLES), encoding="utf-8-sig") as _fh:
        _sp = list(csv.DictReader(_fh))
    check(f"samples 欄位仍為 {len(CFG.SAMPLE_FIELDS)} 欄（未改 Raw Data schema）",
          list(_sp[0].keys()) == CFG.SAMPLE_FIELDS)
    check("samples 末筆方向為 idle",
          _sp[-1]["charge_discharge_direction"] == "idle")
    check("samples 同時含 charge 與 idle（同一份 Session 未被切開）",
          {"charge", "idle"} <= {r["charge_discharge_direction"] for r in _sp})
    from openpyxl import load_workbook as _lwb
    _wb = _lwb(os.path.join(_folder, CFG.FILE_XLSX))
    check(f"report.xlsx 工作表齊全（{len(_wb.sheetnames)} 頁）",
          {"Summary", "Charge & Discharge", "Charge", "Raw Data",
           "Alarm"} <= set(_wb.sheetnames))
    check("收尾後只有 1 個 session 資料夾（未建立第二份）",
          len([n for n in os.listdir(_e2e_root)
               if os.path.isdir(os.path.join(_e2e_root, n))]) == 1)

    # ---- Phase 3.8 補洞：E2E 是唯一串起 3.3+3.4+3.7 的路徑，原本對 3.7 的最終輸出是盲的 ----
    # meter_xlsx=skipped 屬合法結果（E2E 無電表 Log），只有 failed/failed_locked 才算 FAIL。
    _ostat = _st.get("output_status") or {}
    _bad_out = {k: v for k, v in _ostat.items()
                if v in (CFG.OUTPUT_FAILED, CFG.OUTPUT_FAILED_LOCKED)}
    check(f"output_status 無任何 failed 項目（實際 {_ostat}）",
          isinstance(_st.get("output_status"), dict) and not _bad_out)
    check(f"events 含 finalize_ok（Phase 3.7 輸出全數成功）（{_types.count('finalize_ok')} 次）",
          _types.count("finalize_ok") == 1)
    check("finalize_ok 發生於 session_end 之後（③在①之後）",
          "session_end" in _types and "finalize_ok" in _types
          and _types.index("finalize_ok") > _types.index("session_end"))
    _e2e_ok = True
except Exception as _e:
    check(f"E2E 正式流程發生例外：{_e}", False)
finally:
    (CDR.read_all, CDR._fetch_cell_packs, CFG.SAMPLE_INTERVAL_SEC,
     CFG.AUTO_HOLD_STOP_SEC, CFG.AUTO_STOP_ENABLED) = _bak
    reset_monitor()
check("E2E 結束後已還原所有注入與 config", CDR.read_all is _bak[0]
      and CFG.SAMPLE_INTERVAL_SEC == _bak[2] and CFG.AUTO_HOLD_STOP_SEC == _bak[3])

# ======================================================================
# Phase 3.7  Finalize 拆分 / _CLIENT_LOCK 縮小
# ======================================================================
print("\n[Phase 3.7] _CLIENT_LOCK 縮小 —— 監看層只注入鎖，不自行包覆輸出")
_msrc_37 = open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
# Phase 4.2：_report_finalize 與整個監看層已移至 report_monitor.py
_dsrc_37 = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()


def _code_only(s):
    """剝除 docstring 與註解，只留執行程式碼 —— 否則說明文字會讓守門誤判。"""
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


_fin_fn = _code_only(_func_src(_dsrc_37, "_report_finalize"))
check("_report_finalize() 以 client_lock 參數注入（不自行 with 包覆 finalize）",
      "client_lock=_CLIENT_LOCK" in _fin_fn)
check("_report_finalize() 內已無 with _CLIENT_LOCK（輸出不再持鎖）",
      "with _CLIENT_LOCK" not in _fin_fn)
check("_report_finalize() 仍是唯一 finalize 入口（呼叫 sess.finalize）",
      ".finalize(" in _fin_fn)
check("Phase 3.3 例外還原邏輯保留（失敗可用 Menu 19 重試）",
      '_report["finalized"] = False' in _fin_fn)
_menu_code_37 = _code_only(_msrc_37) + "\n" + _code_only(_dsrc_37)
check("監看層未直接呼叫 finalize 的三個內部階段（維持單一公開入口）",
      "_prepare_finalize" not in _menu_code_37
      and "_write_outputs" not in _menu_code_37
      and "_finalize_cleanup" not in _menu_code_37)

print("\n[Phase 3.7] ReportSession.finalize() 契約")
import inspect as _insp     # noqa: E402
_sig = _insp.signature(CDR.ReportSession.finalize)
check(f"finalize(self, end_reason, client_lock=None)（實際 {_sig}）",
      list(_sig.parameters) == ["self", "end_reason", "client_lock"]
      and _sig.parameters["client_lock"].default is None)
for _m in ("_prepare_finalize", "_write_outputs", "_finalize_cleanup", "_record_output"):
    check(f"ReportSession 具備 {_m}()", callable(getattr(CDR.ReportSession, _m, None)))
check("模組層具備 _retry_file_op()", callable(getattr(CDR, "_retry_file_op", None)))

print("\n[Phase 3.7] 輸出重試常數與 output_status 值")
check(f"FINALIZE_RETRY_MAX = 3（實際 {CFG.FINALIZE_RETRY_MAX}）", CFG.FINALIZE_RETRY_MAX == 3)
check(f"FINALIZE_RETRY_WAIT_SEC = 2.0（實際 {CFG.FINALIZE_RETRY_WAIT_SEC}）",
      CFG.FINALIZE_RETRY_WAIT_SEC == 2.0)
check("output_status 四種值皆定義於 config（唯一定義處）",
      (CFG.OUTPUT_OK, CFG.OUTPUT_SKIPPED, CFG.OUTPUT_FAILED_LOCKED, CFG.OUTPUT_FAILED)
      == ("ok", "skipped", "failed_locked", "failed"))
check("Phase 3.7 未新增補產入口（Repair/Regenerate 屬後續 Phase）",
      not hasattr(CFG, "ORPHAN_AUTO_FINALIZE")
      and "def report_repair" not in _msrc_37 and "def report_regen" not in _msrc_37
      and "def report_repair" not in _dsrc_37 and "def report_regen" not in _dsrc_37)
_cdr_src_37 = open(os.path.join(HERE, "charge_discharge_report.py"), encoding="utf-8").read()
check("output_status 寫入 session_state.json",
      '"output_status": dict(' in _code_only(_func_src(_cdr_src_37.replace("\n    def ", "\ndef "),
                                                       "_persist_state")))
check("summary.json 產生路徑未加入 output_status（Summary schema 不變）",
      "output_status" not in _code_only(_func_src(
          _cdr_src_37.replace("\n    def ", "\ndef "), "_session_block")))
check("EVENT_FIELDS 仍為 5 欄（finalize_partial 只是新 event_type，非 schema 變更）",
      CFG.EVENT_FIELDS == ["timestamp", "elapsed_seconds", "event_type", "severity", "detail"])

# 還原使用者原本的設定值（測試不得留下副作用）
CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG = _CFG_ORIG
check(f"測試結束已還原設定：AUTO_SCHEDULE_REPORT_ENABLED={_CFG_ORIG[0]}"
      f" / AUTO_MONITOR_DEBUG={_CFG_ORIG[1]}",
      (CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG) == _CFG_ORIG)
check(f"Phase 4.4：測試期間持有 Monitor Ownership（{_P44_WHY}）", _P44_OWNED is True)
# Phase 4.6-B：確定性 —— 本測試用的是自己臨時 root 導出的 Mutex，
# 因此「ESSAutoMonitor 服務是否 Running」不影響本測試結果。
_PROD_MUTEX = None
try:
    RM._report_output_root = _BAK_OUTPUT_ROOT
    _PROD_MUTEX = RM._mutex_name()
finally:
    RM._report_output_root = lambda: _MUTEX_ROOT
check(f"Phase 4.6-B：使用專屬 Mutex（非正式部署那一顆）→ 不受服務狀態影響",
      RM.ownership_state().get("name") == RM._mutex_name()
      and RM._mutex_name() != _PROD_MUTEX)
RM.release_ownership()
check("Phase 4.4：測試結束已釋放 Monitor Ownership（不阻擋其他行程）",
      RM.is_owner() is False)
RM._report_output_root = _BAK_OUTPUT_ROOT
check("Phase 4.6-B：測試結束已還原 _report_output_root",
      RM._report_output_root is _BAK_OUTPUT_ROOT)
shutil.rmtree(_MUTEX_ROOT, ignore_errors=True)
check("Phase 4.6-B：臨時 Mutex root 已清除（未污染正式 output）",
      not os.path.isdir(_MUTEX_ROOT))

ok = all(_results)
print(f"\n== Phase 2 驗證 {'PASS' if ok else 'FAIL'}"
      f"（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
