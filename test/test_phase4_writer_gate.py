# -*- coding: utf-8 -*-
"""
Phase 4.6-B — Writer Gate 三層防護驗證（含 filesystem side-effect）
======================================================================
背景：Phase 4.4 原本只把 Owner Gate 放在 _report_bg_start()，但
      _report_start_session() 會先 ReportSession(...).start() 落盤，
      非 Owner 因此仍會留下 status=recording 的孤兒資料夾。
      Phase 4.6-B 把 authoritative Gate 前移到 _report_start_session() 最前端。

本檔的核心斷言（原始碼 grep 無法取代）：
      **非 Owner 呼叫任何寫入路徑後，filesystem 必須完全沒有新增 Session artifact。**

⚠️ 刻意在 fault=False、智慧模式、排程開啟、方向 charge、Session 條件全部成立
   的情況下測試 —— 否則會被實機的 fault=True 遮蔽而得到假 PASS。

是否需要設備
    **不需要**。注入假 client、暫存 output root、ApiClient 哨兵；零真設備 I/O。

用法
    python test_phase4_writer_gate.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import glob
import json
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


_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

ARTIFACTS = (CFG.FILE_SESSION_STATE, CFG.FILE_SAMPLES,
             CFG.FILE_EVENTS, CFG.FILE_CELL_SNAPSHOTS)


class _ForbiddenClient:
    def __init__(self, *_a, **_k):
        raise AssertionError("測試不得建立真實 ApiClient（會連設備）")


class _FakeClient:
    def get(self, _p, **_k):
        return []


def _reading_all_conditions_met():
    """
    ⚠️ 刻意讓**所有**建立條件成立：
        fault=False / 智慧模式 / 排程開啟 / 方向 charge / 通訊正常
    這樣才能證明擋下 Session 的是 Owner Gate，而不是被 fault 或其他條件遮蔽。
    """
    return {"communication_ok": True,
            "pcs_charging_flag": True, "pcs_discharging_flag": False,
            "pcs_running_flag": True, "pcs_standby_flag": False,
            "pcs_fault_flag": False,                     # ★ 非故障
            "pcs_control_mode_code": "smart",            # ★ 智慧模式
            "pcs_control_mode": "智慧模式",
            "pcs_schedule_enabled": True,                # ★ 排程開啟
            "pcs_power_control_mode": "交流有功",
            "actual_active_power_kw": 20.0, "calculated_power_kw": 20.0,
            "battery_voltage_v": 800.0, "battery_current_a": 25.0,
            "soc_percent": 55.0, "battery_status": "充電",
            "rack_max_temp_c": 28.0, "rack_min_temp_c": 25.0,
            "alarms": [], "_fail": []}


def _snapshot(root):
    """回傳 (資料夾清單, 所有 Session artifact 檔案清單)。"""
    dirs = sorted(d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d))
    files = sorted(glob.glob(os.path.join(root, "**", "*"), recursive=True))
    arts = [f for f in files if os.path.basename(f) in ARTIFACTS]
    return dirs, arts


ROOT = tempfile.mkdtemp(prefix="p46_wgate_")
_BAK = (CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
        RM._report_client, RM._monitor_client, RM._report_output_root,
        CFG.SAMPLE_INTERVAL_SEC, CFG.AUTO_SCHEDULE_REPORT_ENABLED)
CDR.read_all = lambda _c: _reading_all_conditions_met()
CDR._fetch_cell_packs = lambda _c: (None, "offline")
CDR.ApiClient = _ForbiddenClient
RM._report_client = lambda *a, **k: _FakeClient()
RM._monitor_client = lambda force=False: (_FakeClient(), False)
RM._report_output_root = lambda: ROOT
CFG.SAMPLE_INTERVAL_SEC = 0.05
CFG.AUTO_SCHEDULE_REPORT_ENABLED = True


def _reset():
    RM._report.update(session=None, thread=None, stop=None, finalized=False,
                      pending_end_reason=None, stopping=False)
    RM._auto.update(state=RM.AUTO_IDLE, direction=None, since=None, held_sec=0.0,
                    streak=0, cooldown_until=None, sched_ctx=None, sched_ctx_at=None,
                    disabled=False, login_failed_at=None)
    RM._auto_clear_notes()


_ok_all = False
try:
    print("[Phase 4.6-B] 前提：本行程非 Owner，且所有建立條件皆成立（fault=False）")
    _reset()
    check("前提  is_owner() 為 False", RM.is_owner() is False)
    _r = _reading_all_conditions_met()
    check("前提  reading 的 fault=False、smart、排程開啟、方向 charge",
          _r["pcs_fault_flag"] is False and _r["pcs_control_mode_code"] == "smart"
          and _r["pcs_schedule_enabled"] is True
          and CDR.resolve_direction(_r["pcs_charging_flag"], _r["pcs_discharging_flag"],
                                    _r["actual_active_power_kw"])[0] == "charge")

    # ==================================================================
    # Layer 2（authoritative）：_report_start_session 不得落盤
    # ==================================================================
    print("\n[Phase 4.6-B] Layer 2  非 Owner 呼叫 _report_start_session → 零 filesystem 副作用")
    _d0, _a0 = _snapshot(ROOT)
    _t0 = threading.active_count()
    _o = io.StringIO()
    with contextlib.redirect_stdout(_o):
        _sess, _created = RM._report_start_session("charge", "交流有功", None,
                                                   origin="non-owner-test")
    _d1, _a1 = _snapshot(ROOT)
    check(f"回傳 (None, False)（實際 ({'物件' if _sess else None}, {_created})）",
          _sess is None and _created is False)
    check("_report['session'] 仍為 None", RM._report["session"] is None)
    check("_report['thread'] 仍為 None", RM._report["thread"] is None)
    check(f"thread 數未增加（{_t0} → {threading.active_count()}）",
          threading.active_count() == _t0)
    check(f"★ 新增資料夾數 == 0（實際 {len(_d1) - len(_d0)}）", len(_d1) == len(_d0))
    check(f"★ 新增 Session artifact == 0（實際 {len(_a1) - len(_a0)}）", len(_a1) == len(_a0))
    for _name in ARTIFACTS:
        check(f"★ 未出現任何 {_name}",
              not glob.glob(os.path.join(ROOT, "**", _name), recursive=True))
    check("有明確拒絕訊息（非靜默）", "非 Monitor Owner" in _o.getvalue())
    check("Gate 早於 ReportSession 建立（輸出不含 REPORT SESSION CREATE）",
          "REPORT SESSION CREATE" not in _o.getvalue())

    # ==================================================================
    # Layer 1：auto_schedule_check 提早跳過，且不呼叫 Layer 2
    # ==================================================================
    print("\n[Phase 4.6-B] Layer 1  非 Owner 的 auto_schedule_check → skipped/not_owner")
    _reset()
    _called = {"n": 0}
    _orig_start = RM._report_start_session
    RM._report_start_session = lambda *a, **k: (_called.__setitem__("n", _called["n"] + 1)
                                                or (None, False))
    try:
        _d0, _a0 = _snapshot(ROOT)
        with contextlib.redirect_stdout(io.StringIO()):
            _act, _why = RM.auto_schedule_check(_reading_all_conditions_met(),
                                                client=_FakeClient(), source="observer")
        _d1, _a1 = _snapshot(ROOT)
    finally:
        RM._report_start_session = _orig_start
    check(f"回傳 ('skipped', 'not_owner')（實際 ({_act!r}, {_why!r})）",
          _act == "skipped" and _why == "not_owner")
    check(f"**未**呼叫 _report_start_session（實際 {_called['n']} 次）", _called["n"] == 0)
    check("未新增任何資料夾", len(_d1) == len(_d0))
    check("未新增任何 Session artifact", len(_a1) == len(_a0))
    _src = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
    _asc = _src[_src.index("def auto_schedule_check("):]
    _asc = _asc[:_asc.index("\ndef ", 5)]
    check("Layer 1 的 Gate 位於 _actual_direction / 排程查詢之前（不做無用工作）",
          _asc.index('return "skipped", "not_owner"') < _asc.index("_actual_direction(r)"))

    # ==================================================================
    # Layer 3：_report_bg_start 的既有 Gate 未被移除
    # ==================================================================
    print("\n[Phase 4.6-B] Layer 3  _report_bg_start 的既有 Gate 保留")
    _reset()
    with contextlib.redirect_stdout(io.StringIO()):
        _bg = RM._report_bg_start(object())
    check("非 Owner 呼叫 _report_bg_start 仍回 False", _bg is False)
    check("未起取樣 thread", RM._report["thread"] is None)
    _bgsrc = _src[_src.index("def _report_bg_start("):_src.index("def _stop_and_finalize(")]
    check("Layer 3 Gate 仍在函式最前（未因新增前兩層而移除）",
          "if not is_owner():" in _bgsrc
          and _bgsrc.index("if not is_owner():") < _bgsrc.index("stop = threading.Event()"))

    # ==================================================================
    # report_resume_on_launch 的非 Owner Gate 仍在
    # ==================================================================
    print("\n[Phase 4.6-B] report_resume_on_launch 非 Owner Gate 保持")
    # 先以 Owner 身分造一份 paused Session 供 resume 嘗試
    RM.acquire_ownership(role="wgate-setup")
    with contextlib.redirect_stdout(io.StringIO()):
        RM._report_start_session("charge", "交流有功", None, origin="setup")
        RM.report_pause()
    RM.release_ownership()
    _paused = [d for d in glob.glob(os.path.join(ROOT, "*")) if os.path.isdir(d)]
    check(f"前置：已由 Owner 造出 1 份可續接 Session（{len(_paused)}）", len(_paused) == 1)
    _st = json.load(open(os.path.join(_paused[0], CFG.FILE_SESSION_STATE), encoding="utf-8"))
    check(f"前置：該 Session 為 paused（{_st.get('status')}）",
          _st.get("status") == CFG.SESSION_PAUSED)
    _reset()
    _d0, _a0 = _snapshot(ROOT)
    _t0 = threading.active_count()
    with contextlib.redirect_stdout(io.StringIO()):
        RM.report_resume_on_launch()
    check("非 Owner 未 resume（session 仍 None）", RM._report["session"] is None)
    check("非 Owner 未起 thread", RM._report["thread"] is None
          and threading.active_count() == _t0)
    _d1, _a1 = _snapshot(ROOT)
    check("非 Owner resume 未新增資料夾", len(_d1) == len(_d0))
    _st2 = json.load(open(os.path.join(_paused[0], CFG.FILE_SESSION_STATE), encoding="utf-8"))
    check(f"非 Owner 未改動該 Session 狀態（仍 {_st2.get('status')}）",
          _st2.get("status") == CFG.SESSION_PAUSED)

    # ==================================================================
    # Owner 的正常流程不得被破壞
    # ==================================================================
    print("\n[Phase 4.6-B] Owner 正常流程未被破壞")
    _reset()
    _ok, _why2 = RM.acquire_ownership(role="wgate-owner")
    check(f"取得 Ownership（{_why2}）", _ok is True)
    _d0, _a0 = _snapshot(ROOT)
    with contextlib.redirect_stdout(io.StringIO()):
        _sess2, _created2 = RM._report_start_session("charge", "直流恆流", 15.0,
                                                     origin="owner-test")
    check(f"Owner 可正常建立 Session（created={_created2}）",
          _sess2 is not None and _created2 is True)
    check("Owner 的取樣 thread 已啟動",
          RM._report["thread"] is not None and RM._report["thread"].is_alive())
    _d1, _a1 = _snapshot(ROOT)
    check(f"Owner 確實新增 1 個資料夾（{len(_d1) - len(_d0)}）", len(_d1) - len(_d0) == 1)
    check("Owner 建立的 Session 參數正確帶入",
          _sess2.control_mode_label == "直流恆流" and _sess2.setpoint_value == 15.0)
    with contextlib.redirect_stdout(io.StringIO()):
        _act2, _why3 = RM.auto_schedule_check(_reading_all_conditions_met(),
                                              client=_FakeClient(), source="owner")
    check(f"Owner 的 auto_schedule_check 未被 not_owner 擋下（{_act2} / {_why3}）",
          _why3 != "not_owner")
    with contextlib.redirect_stdout(io.StringIO()):
        RM.report_pause()
    RM.release_ownership()
    check("Owner 流程收尾正常（session 已清空、已釋放 Ownership）",
          RM._report["session"] is None and RM.is_owner() is False)

    # ==================================================================
    # Dashboard 唯讀路徑不受 Layer 1 early return 影響
    # ==================================================================
    print("\n[Phase 4.6-B] Observer 的唯讀設備狀態路徑不受影響")
    _menu_src = open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
    _dash = _menu_src[_menu_src.index("def dashboard_refresh("):
                      _menu_src.index("def report_status_line(")]
    check("dashboard_refresh 在呼叫 auto_schedule_check **之前**完成 _read_device_state",
          _dash.index("RM._read_device_state(") < _dash.index("RM.auto_schedule_check("))
    check("dashboard_refresh 在呼叫 auto_schedule_check **之前**完成狀態行輸出",
          _dash.index("RM._monitor_status_line(") < _dash.index("RM.auto_schedule_check("))
    check("dashboard_refresh 未以 auto_schedule_check 的回傳值決定是否顯示狀態",
          "act, _reason = RM.auto_schedule_check(" in _dash)
    _reset()
    _line = RM._monitor_status_line(_reading_all_conditions_met(), "observer")
    check("非 Owner 仍可產生完整狀態行（唯讀顯示不受 Gate 影響）",
          "mode=smart" in _line and "SOC 55.0%" in _line and " owner=" in _line)
    _ok_all = True
except Exception as _e:                                  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check(f"測試發生未預期例外：{type(_e).__name__}: {_e}", False)
finally:
    (CDR.read_all, CDR._fetch_cell_packs, CDR.ApiClient,
     RM._report_client, RM._monitor_client, RM._report_output_root,
     CFG.SAMPLE_INTERVAL_SEC, CFG.AUTO_SCHEDULE_REPORT_ENABLED) = _BAK
    _reset()
    RM.release_ownership()
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n[Phase 4.6-B] 環境還原")
check("已還原 CDR.read_all / ApiClient", CDR.read_all is _BAK[0] and CDR.ApiClient is _BAK[2])
check("已還原 output root / config", RM._report_output_root is _BAK[5]
      and CFG.SAMPLE_INTERVAL_SEC == _BAK[6])
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("測試主體完整執行", _ok_all is True)

ok = all(RESULTS)
print(f"\n== Phase 4.6-B Writer Gate 驗證 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
