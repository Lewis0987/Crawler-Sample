# -*- coding: utf-8 -*-
"""
Phase 4.4 跨行程測試用的**子行程** helper（不是測試本身）。

Mutex 互斥無法在單一行程內驗證 —— 同行程再 Wait 只會累加遞迴計數。
因此 A~G 必須真的起兩個行程，由本檔擔任被起的那一方。

以 stdout 最後一行輸出單行 JSON 結果，供父行程解析。
全程離線：不連設備、不建立真實 Session（除非父行程明確要求 hold-session 模式）。

用法（皆由測試呼叫，不需人工執行）：
    python _p44_owner_child.py acquire   <root>          取得後立即回報並釋放
    python _p44_owner_child.py hold      <root> <sec>    取得後持有 N 秒
    python _p44_owner_child.py hold_kill <root>          取得後永久持有（等父行程強制終止）
    python _p44_owner_child.py resume    <root>          以非 Owner 身分嘗試 resume
    python _p44_owner_child.py service   <root>          走 Service main() 的 ownership 判斷

Phase 4.7（Crash Recovery Verification）新增，僅供 test_phase4_recovery.py 使用：
    python _p44_owner_child.py crash_session_worker <root> <interval>
        取得 Ownership → 建立**真實** ReportSession（臨時 root）→ 啟動背景取樣 →
        回報 pid/session_id/folder → 之後永久等待，由父行程 taskkill /F 模擬崩潰。
        **刻意不註冊任何收尾**：被強殺時 atexit 不執行，Session 會停在 recording，
        這正是要驗證的 crash 狀態。
    python _p44_owner_child.py resume_worker <root> <hold_sec>
        取得 Ownership → report_resume_on_launch() 續接 → 取樣 hold_sec 秒 →
        回報續接結果 → 正常 report_pause() + release_ownership() 後結束。

⚠️ Phase 4.7 兩個模式的共同約束（避免污染正式環境）：
     · 一律使用父行程傳入的**臨時** root，不碰正式 output
     · client / read_all / _fetch_cell_packs 全部注入假物件，零網路 I/O
     · 不修改任何產品契約
"""
import io
import os
import sys
import json
import time
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _emit(obj):
    """結果一律印在最後一行，避免被模組載入訊息干擾。"""
    sys.stdout.write("\n__P44__" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    mode = sys.argv[1]
    root = sys.argv[2]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):            # 壓住模組載入訊息
        import report_monitor as RM
        import charge_discharge_report as CDR
        RM._report_output_root = lambda: root

    if mode == "acquire":
        ok, why = RM.acquire_ownership(role="child")
        st = RM.ownership_state()
        RM.release_ownership()
        _emit({"ok": ok, "why": why, "abandoned": st.get("abandoned")})
        return 0

    if mode in ("hold", "hold_kill"):
        ok, why = RM.acquire_ownership(role="child")
        st = RM.ownership_state()
        _emit({"ok": ok, "why": why, "abandoned": st.get("abandoned"),
               "pid": os.getpid()})
        if not ok:
            return 1
        if mode == "hold":
            time.sleep(float(sys.argv[3]))
            RM.release_ownership()
            return 0
        while True:                                   # 等父行程 taskkill /F
            time.sleep(0.2)

    if mode == "wait_abandoned":
        # 開啟 handle 但**不**取得所有權，然後以阻塞方式等待。
        # 父行程會在這段等待期間強制終止 owner —— 此時 Mutex 物件因本行程仍持有
        # handle 而不會消滅，OS 便會讓本次 Wait 回傳 WAIT_ABANDONED。
        # （若無人持有 handle，owner 一死物件即消滅，下一個 open-or-create 會建出
        #   全新物件並得到 WAIT_OBJECT_0 —— 那條路徑無法驗證 WAIT_ABANDONED。）
        #
        # ⚠️ 一律使用 CreateMutexExW + MUTEX_MIN_ACCESS，與產品碼同一條路徑：
        #    ① CreateMutexW 已不在 _k32() 的宣告清單中，未宣告 restype 會讓 64 位元
        #       HANDLE 被截成 32 位元 int（目前僥倖可用，不可依賴）。
        #    ② 這裡要驗的是「非 Owner 以最小權限開 handle 後仍能收到 WAIT_ABANDONED」，
        #       用 ALL_ACCESS 開就不是產品實際走的路徑。
        k = RM._k32()
        name = RM._mutex_name()
        h = k.CreateMutexExW(None, name, 0, RM.MUTEX_MIN_ACCESS)
        _emit({"stage": "handle_open", "handle": bool(h), "pid": os.getpid()})
        rc = k.WaitForSingleObject(h, int(sys.argv[3]))
        res = {"rc": int(rc),
               "abandoned": rc == RM.WAIT_ABANDONED,
               "acquired": rc == RM.WAIT_OBJECT_0,
               "timeout": rc == RM.WAIT_TIMEOUT}
        if rc in (RM.WAIT_OBJECT_0, RM.WAIT_ABANDONED):
            k.ReleaseMutex(h)                     # 取得後正常釋放，不留下第二個 abandoned
        k.CloseHandle(h)
        _emit(res)
        return 0

    # ------------------------------------------------------------------
    # Phase 4.7：Crash Recovery Verification 專用（離線、臨時 root）
    # ------------------------------------------------------------------
    if mode in ("crash_session_worker", "resume_worker"):
        interval = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3
        with contextlib.redirect_stdout(buf):
            import charge_discharge_report_config as CFG

            class _FakeClient:
                """假 client：零網路。login_hmi 供 _report_client() 使用。"""

                def login_hmi(self, *_a, **_k):
                    return "offline-token"

                def get(self, *_a, **_k):
                    return {}

            def _fake_read(_c):
                return {
                    "communication_ok": True, "raw_source_time": "",
                    "pcs_charging_flag": True, "pcs_discharging_flag": False,
                    "soc_percent": 50.0, "battery_voltage_v": 890.0,
                    "battery_current_a": 2.6, "rack_max_temperature_c": 30.0,
                    "rack_min_temperature_c": 25.0, "battery_status": "充電",
                    "actual_active_power_kw": 2.3, "actual_reactive_power_kvar": 0.0,
                    "calculated_power_kw": 2.3, "pcs_status": "執行 / 併網",
                    "pcs_control_mode": "智慧模式", "pcs_control_mode_code": "smart",
                    "pcs_work_mode": "併網", "pcs_power_control_mode": "交流有功",
                    "pcs_manual_switch": 0, "pcs_schedule_enabled": True,
                    "pcs_fault_flag": False, "pcs_running_flag": True,
                    "pcs_standby_flag": False, "battery_power_status": "已上電",
                    "device_daily_charge_kwh": 1.0, "device_daily_discharge_kwh": 0.5,
                    "alarm_rows": [], "alarm_total": 0,
                }

            CDR.read_all = _fake_read
            CDR._fetch_cell_packs = lambda _c: (CDR._fake_pack_data(), None)
            CDR.ApiClient = _FakeClient
            RM._report_client = lambda *a, **k: _FakeClient()
            CFG.SAMPLE_INTERVAL_SEC = interval

        ok, why = RM.acquire_ownership(role="service")
        st = RM.ownership_state()
        if not ok:
            _emit({"stage": "no_ownership", "ok": ok, "why": why,
                   "pid": os.getpid()})
            return 1

        if mode == "crash_session_worker":
            with contextlib.redirect_stdout(buf):
                sess = CDR.ReportSession("auto", None, "交流有功",
                                         _FakeClient(), root)
                sess.start()
                started = RM._report_bg_start(sess)
            # 等第一批取樣落地，父行程才能取到穩定的 sample_index
            deadline = time.time() + 20
            while len(sess.samples) < 3 and time.time() < deadline:
                time.sleep(0.1)
            _emit({"stage": "recording", "pid": os.getpid(), "ok": ok, "why": why,
                   "abandoned": st.get("abandoned"), "started": bool(started),
                   "session_id": sess.session_id, "folder": sess.folder,
                   "samples": len(sess.samples),
                   "sample_index": sess.sample_index,
                   "mutex": RM._mutex_name()})
            # **不做任何收尾**：等父行程 taskkill /F（模擬崩潰）
            while True:
                time.sleep(0.2)

        # resume_worker
        hold = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0
        with contextlib.redirect_stdout(buf):
            RM.report_resume_on_launch()
        sess = RM._report.get("session")
        if sess is None:
            _emit({"stage": "resume_failed", "pid": os.getpid(),
                   "ok": ok, "why": why, "abandoned": st.get("abandoned")})
            RM.release_ownership()
            return 1
        first = {"session_id": sess.session_id, "folder": sess.folder,
                 "sample_index": sess.sample_index,
                 "start_time": getattr(sess, "start_dt", None)
                 and sess.start_dt.strftime("%Y-%m-%d %H:%M:%S")}
        time.sleep(hold)
        payload = {
            "stage": "resumed", "pid": os.getpid(), "ok": ok, "why": why,
            "abandoned": st.get("abandoned"),
            "session_id": sess.session_id, "folder": sess.folder,
            "start_time": first["start_time"],
            "sample_index_at_resume": first["sample_index"],
            "sample_index_now": sess.sample_index,
            "samples_now": len(sess.samples),
            "thread_alive": RM._report["thread"] is not None
            and RM._report["thread"].is_alive(),
            "mutex": RM._mutex_name(),
        }
        with contextlib.redirect_stdout(buf):
            RM.report_pause()                 # 正常收尾：留下 paused，可再續接
        RM.release_ownership()
        payload["after_pause_owner"] = RM.is_owner()
        _emit(payload)
        return 0

    if mode == "resume":
        # 模擬第二個行程啟動：非 Owner 不得 resume、不得起取樣執行緒
        ok, why = RM.acquire_ownership(role="child-dashboard")
        with contextlib.redirect_stdout(buf):
            RM.report_resume_on_launch()
        _emit({"ok": ok, "why": why,
               "session": RM._report["session"] is not None,
               "thread": RM._report["thread"] is not None})
        RM.release_ownership()
        return 0

    if mode == "service":
        import auto_monitor_service as SVC
        with contextlib.redirect_stdout(buf):
            SVC.RM._report_output_root = lambda: root
        code = SVC.main(["--once", "--no-resume"])
        _emit({"exit": code})
        return code

    _emit({"error": f"unknown mode {mode}"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
