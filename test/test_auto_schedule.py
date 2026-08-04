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

import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import device_control_menu as M          # noqa: E402
import charge_discharge_report as CDR    # noqa: E402
import charge_discharge_report_config as CFG   # noqa: E402

_results = []
# 保留使用者在設定檔中的實際值，測試結束後還原（測試不得改變專案設定）
_CFG_ORIG = (CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG)


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
    M._report.update(session=None, thread=None, stop=None, finalized=False,
                     pending_end_reason=None, stopping=False)
    M._auto.update(state=M.AUTO_IDLE, direction=None, since=None, held_sec=0.0,
                   streak=0, template_status="unknown", last_note=None,
                   last_status=None, disabled=False, login_failed_at=None,
                   _pending_nl=False, _no_tty_warned=False)
    set_switches(auto_report=auto_report, debug=debug)


def run_check(r, client=None, source="auto"):
    """呼叫監看層並攔截建立行為；回傳 (action, reason, started_args)。"""
    calls = []

    def fake_start(direction, mode_label, setpoint=None, origin="manual", extra_events=()):
        sess = FakeSession()
        for ev in extra_events:
            sess.log_event(*ev)
        calls.append({"direction": direction, "mode_label": mode_label,
                      "setpoint": setpoint, "origin": origin, "session": sess})
        M._report["session"] = sess          # 模擬建立成功後的狀態
        return sess, True

    orig = M._report_start_session
    M._report_start_session = fake_start
    try:
        action, reason = M.auto_schedule_check(r, client=client, source=source)
    finally:
        M._report_start_session = orig
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
check("Template 狀態記為 enabled", M._auto["template_status"] == "enabled")
check(f"狀態機回到 RUNNING（{M._auto['state']}）", M._auto["state"] == M.AUTO_RUNNING)

print("\n[情境 5] 智慧模式＋排程開啟＋實際放電 → 建立一份報告")
reset_monitor(auto_report=True)
cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
act, why, calls = run_check(reading(direction="discharge"), client=cl)
check(f"action=started（{act} / {why}）", act == "started" and why == "discharge")
ev_types = [e[0] for e in calls[0]["session"].events] if calls else []
check(f"記錄 auto_discharge_start 事件（{ev_types}）", "auto_discharge_start" in ev_types)

print("\n[情境 6] 已有 ReportSession，再次刷新 → 不建立第二份")
reset_monitor()
M._report["session"] = FakeSession("EXISTING_SESSION")
act, why, calls = run_check(reading(direction="charge"))
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為已有 Session", why == "session_exists")
check("既有 Session 未被替換", M._report["session"].session_id == "EXISTING_SESSION")

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
M._auto["state"] = M.AUTO_START_PENDING
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
check("Template 狀態記為 unknown", M._auto["template_status"] == "unknown")
check("仍建立 1 份 Session", len(calls) == 1)

print("\n[情境 8b] Template List 取得成功但無啟用模板 → 仍建立，Template 記為 none")
reset_monitor(auto_report=True)
cl_none = FakeClient(templates=[{"tempId": "1", "enableFlag": 2}])
act, why, calls = run_check(reading(direction="charge"), client=cl_none)
check(f"action=started（{act}）→ 無啟用模板不得阻擋", act == "started")
check("Template 狀態記為 none", M._auto["template_status"] == "none")

print("\n[情境 8c] Template List 回傳非 list（格式非預期）→ unknown，仍建立")
reset_monitor(auto_report=True)
cl_bad = FakeClient(templates={"unexpected": True})
act, why, calls = run_check(reading(direction="charge"), client=cl_bad)
check(f"action=started（{act}）", act == "started")
check("Template 狀態記為 unknown", M._auto["template_status"] == "unknown")

print("\n[情境 9] read_all() 不得含任何自動報告副作用")
src_path = os.path.join(HERE, "charge_discharge_report.py")
with open(src_path, encoding="utf-8") as f:
    src = f.read()
check("charge_discharge_report.py 完全未提及 auto_schedule_check",
      "auto_schedule_check" not in src)
check("報告模組未匯出 auto_schedule_check（僅存在於 menu 監看層）",
      not hasattr(CDR, "auto_schedule_check"))
check("報告模組未匯入 device_control_menu（無反向依賴）",
      "import device_control_menu" not in src)
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
with open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8") as f:
    _menu_src = f.read()
_mon = _menu_src[_menu_src.index("def auto_schedule_check("):
                 _menu_src.index("def report_status_line(")]
check("auto_schedule_check ~ dashboard_refresh 區段內無 threading.Thread(",
      "threading.Thread(" not in _mon)
# Phase 2 未新增任何 thread：整份 menu 仍只有兩處既有 thread —
#   ① wait_and_verify._poller（控制後驗證進度列，既有）
#   ② _report_bg_start._loop（報告背景取樣，既有）
_thread_sites = _menu_src.count("threading.Thread(")
check(f"menu 內建立 thread 的位置仍為 2 處既有（實際 {_thread_sites}）", _thread_sites == 2)
check("兩處 thread 分別為 _poller（控制驗證）與 _loop（報告背景取樣）",
      "threading.Thread(target=_poller" in _menu_src
      and "threading.Thread(target=_loop" in _menu_src)

print("\n[情境 12] 安全開關 AUTO_SCHEDULE_REPORT_ENABLED=False → 條件成立也不建立")
reset_monitor(auto_report=False)
cl = FakeClient(templates=[{"tempId": "1", "enableFlag": 1}])
act, why, calls = run_check(reading(direction="charge"), client=cl)
check(f"action=skipped（{act} / {why}）", act == "skipped" and not calls)
check("原因為 auto_report_disabled", why == "auto_report_disabled")
check("開關關閉時完全不建立 Session", M._report["session"] is None)
check(f"狀態機留在 IDLE（{M._auto['state']}）", M._auto["state"] == M.AUTO_IDLE)
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
_start_fn = _ms[_ms.index("def _report_start_locked("):_ms.index("def _report_bg_start(")]
check("手動報告入口（Menu 18）未讀取 AUTO_SCHEDULE_REPORT_ENABLED",
      "AUTO_SCHEDULE_REPORT_ENABLED" not in _start_fn)
# 實際「讀取」開關的只有 2 處：auto_schedule_check 的判斷 + _monitor_footer 的狀態顯示
_reads = _ms.count('getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED"')
check(f"整份 menu 只有 2 處讀取此開關（判斷＋footer 顯示；實際 {_reads}）", _reads == 2)
_footer_fn = _ms[_ms.index("def _monitor_footer("):_ms.index("def dashboard_refresh(")]
_check_fn = _ms[_ms.index("def auto_schedule_check("):_ms.index("def _monitor_status_line(")]
check("兩處分別位於 auto_schedule_check（判斷）與 _monitor_footer（顯示）",
      'getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED"' in _check_fn
      and 'getattr(CDR_CFG, "AUTO_SCHEDULE_REPORT_ENABLED"' in _footer_fn)

print("\n[情境 13] 登入失敗可恢復：60s 靜默重試 + 按 r 立即重試")
reset_monitor()
_orig_rc = M._report_client
_login = {"ok": False, "calls": 0}


def _fake_report_client(quiet=False):
    _login["calls"] += 1
    return FakeClient() if _login["ok"] else None


M._report_client = _fake_report_client
try:
    c, p = M._monitor_client()
    check("首次登入失敗 → 回 None 並提示一次", c is None and p is True)
    check("已標記暫停（可恢復，非永久停用）", M._auto["disabled"] is True)
    n_before = _login["calls"]
    c, p = M._monitor_client()                       # 節流期間（<60s）
    check("60s 內自動重試被節流 → 不重試、不輸出",
          c is None and p is False and _login["calls"] == n_before)
    c, p = M._monitor_client(force=True)             # 使用者按 r → 立即重試
    check("force=True（按 r）→ 立即重試", _login["calls"] == n_before + 1)
    check("重試仍失敗 → 不輸出（不洗畫面）", p is False)
    # 模擬節流時間到（把失敗時間往前推 60s）
    M._auto["login_failed_at"] = M.time.monotonic() - M.AUTO_LOGIN_RETRY_SEC - 1
    _login["ok"] = True
    c, p = M._monitor_client()
    check("節流時間到且登入成功 → 恢復並提示一次", c is not None and p is True)
    check("恢復後 disabled=False（回到 15s 正常監看）", M._auto["disabled"] is False)
    c, p = M._monitor_client()
    check("恢復後不再重複提示", p is False)
finally:
    M._report_client = _orig_rc

print("\n[情境 11] dashboard_refresh：手動與自動走同一函式，自動刷新不重複登入子程序")
reset_monitor()
_calls = {"refresh_status": 0, "render": 0, "read": 0}
_o = (M.refresh_status, M.render_dashboard, M._report_client, M._read_device_state,
      M._report_start_session)


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
M._report_client = lambda quiet=False: _fake_cl
M._read_device_state = _fake_read
M._report_start_session = lambda *a, **k: (FakeSession(), True)
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
    M._report_client = lambda quiet=False: None
    _r5, p5 = M.dashboard_refresh(auto=True)
    check("登入失敗 → 暫停自動偵測並提示一次", M._auto["disabled"] is True and p5 is True)
    _r6, p6 = M.dashboard_refresh(auto=True)
    check("登入失敗後 60s 內不再重複提示、不再取樣", p6 is False)
finally:
    (M.refresh_status, M.render_dashboard, M._report_client, M._read_device_state,
     M._report_start_session) = _o

print("\n[附加] Debounce 資料結構為時間型（monotonic），Phase 3 可直接沿用")
reset_monitor()
M._auto_track_direction("charge", now=1000.0)
check("方向首次觀測 → held=0、since 記錄 monotonic 值",
      M._auto["held_sec"] == 0.0 and M._auto["since"] == 1000.0)
held, changed = M._auto_track_direction("charge", now=1032.5)
check(f"同方向 32.5s 後 held={held}（時間差，非次數）", abs(held - 32.5) < 1e-9 and not changed)
check("streak 仍記錄次數作為輔助資訊", M._auto["streak"] == 2)
held, changed = M._auto_track_direction("idle", now=1040.0)
check(f"方向改變 → 重新起算（held={held}, changed={changed})",
      held == 0.0 and changed is True)
check(f"門檻常數已定義：start={M.AUTO_HOLD_START_SEC}s / stop={M.AUTO_HOLD_STOP_SEC}s",
      M.AUTO_HOLD_START_SEC == 30 and M.AUTO_HOLD_STOP_SEC == 30)
check(f"自動刷新間隔 = {M.AUTO_REFRESH_SEC}s", M.AUTO_REFRESH_SEC == 15)

print("\n[附加] 訊息抑制：相同判斷結果不重複列印")
reset_monitor()
printed_1 = M._auto_note("測試訊息 A")
printed_2 = M._auto_note("測試訊息 A")
printed_3 = M._auto_note("測試訊息 B")
check("相同訊息第二次不列印", printed_1 is True and printed_2 is False and printed_3 is True)

print("\n[附加] 報告模組不可用時 → noop，不拋例外")
reset_monitor()
_orig_avail = M._REPORT_AVAILABLE
M._REPORT_AVAILABLE = False
try:
    act, why = M.auto_schedule_check(reading(direction="charge"))
    check(f"action=noop（{act} / {why}）", act == "noop")
finally:
    M._REPORT_AVAILABLE = _orig_avail

print("\n[附加] reading=None（取樣失敗）→ noop，不建立也不停止")
reset_monitor()
act, why, calls = run_check(None)
check(f"action=noop（{act} / {why}）", act == "noop" and not calls)

print("\n[附加] 鎖範圍：不得在等待/顯示期間持有鎖")
_src_menu = open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
_prompt_fn = _src_menu[_src_menu.index("def _prompt_with_timeout("):
                       _src_menu.index("def _exec_menu(")]
check("_prompt_with_timeout / _prompt_msvcrt（含 15s 等待與 sleep）完全不持有任何鎖",
      "_SESSION_LOCK" not in _prompt_fn and "_CLIENT_LOCK" not in _prompt_fn)
_dash_fn = _src_menu[_src_menu.index("def dashboard_refresh("):
                     _src_menu.index("def report_status_line(")]
check("dashboard_refresh 本體不直接持鎖（鎖只在被呼叫的取樣/建立函式內）",
      "with _SESSION_LOCK" not in _dash_fn and "with _CLIENT_LOCK" not in _dash_fn)
_stop_fn = _src_menu[_src_menu.index("def _stop_and_finalize("):
                     _src_menu.index("def report_pause(")]
_locked_head = _stop_fn[_stop_fn.index("with _SESSION_LOCK"):_stop_fn.index("try:")]
check("_stop_and_finalize：join 等待不在 _SESSION_LOCK 內", "join(" not in _locked_head)
check("_stop_and_finalize：finalize 不在 _SESSION_LOCK 內",
      "_report_finalize(" not in _locked_head)
_pause_fn = _src_menu[_src_menu.index("def report_pause("):
                      _src_menu.index("def report_stop(")]
_pause_head = _pause_fn[_pause_fn.index("with _SESSION_LOCK"):_pause_fn.index("try:")]
check("report_pause：join 等待不在 _SESSION_LOCK 內", "join(" not in _pause_head)
_chk_fn = _src_menu[_src_menu.index("def auto_schedule_check("):
                    _src_menu.index("def _monitor_status_line(")]
_chk_locked = _chk_fn[_chk_fn.index("with _SESSION_LOCK"):_chk_fn.index("# ---- 建立報告")]
check("auto_schedule_check：template GET 與 sess.start() 不在 _SESSION_LOCK 內",
      "_auto_template_status(" not in _chk_locked
      and "_report_start_session(" not in _chk_locked)

# 還原使用者原本的設定值（測試不得留下副作用）
CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG = _CFG_ORIG
check(f"測試結束已還原設定：AUTO_SCHEDULE_REPORT_ENABLED={_CFG_ORIG[0]}"
      f" / AUTO_MONITOR_DEBUG={_CFG_ORIG[1]}",
      (CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG) == _CFG_ORIG)

ok = all(_results)
print(f"\n== Phase 2 驗證 {'PASS' if ok else 'FAIL'}"
      f"（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
