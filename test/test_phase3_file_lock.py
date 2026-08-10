# -*- coding: utf-8 -*-
"""
Phase 3.7 輸出重試 —— Windows 真實檔案鎖驗證
======================================================================
用途
    驗證 finalize 的輸出重試在**作業系統層級真的會觸發並復原**，而不只是
    「注入假例外時 wrapper 的判斷邏輯正確」。charge_discharge_report.py 的
    --selftest 已用注入例外驗過 18 項判斷邏輯；本檔補的是那 18 項證明不了的部分：
    wb.save() / os.replace() 這兩個實際寫檔動作，在檔案真的被占用時會不會
    走進 _retry_file_op、以及占用解除後能不能自行復原。

是否需要設備
    **不需要**。完全離線：read_all / _fetch_cell_packs 皆以合成資料注入，
    輸出寫在系統暫存目錄，不連任何設備、不碰正式 output 資料夾。

製造真實失敗的兩種方式（皆為作業系統層級，非注入）
    A. 在 report.xlsx 的位置放一個**同名目錄** → openpyxl 的 wb.save() 開檔寫入
       → 真 PermissionError [Errno 13]
    B. 以 open(path,'rb') 持有 report.xlsx → _force_plot_visible_all() 的
       os.replace() 在 Windows 無法覆蓋開啟中的檔案 → 真 PermissionError [WinError 5]

安全性
    不含帳號/密碼/Token；不送任何控制 API；不修改 config（暫時調短重試等待，
    結束前還原並自我驗證）；不修改正式 output；不執行 Git。

用法
    python test_phase3_file_lock.py          # exit 0 = PASS
"""
import os
import sys
import json
import shutil
import tempfile
import contextlib
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import charge_discharge_report as CDR              # noqa: E402
import charge_discharge_report_config as CFG       # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# ---------- 合成 reading（完全離線，不連設備）----------
_clk = {"dt": datetime(2026, 1, 1, 10, 0, 0)}


def _fake_read_all(_client):
    return {
        "soc_percent": 55.0, "actual_active_power_kw": 10.0, "calculated_power_kw": 10.0,
        "battery_voltage_v": 800.0, "battery_current_a": 12.5,
        "pcs_charging_flag": True, "pcs_discharging_flag": False,
        "pcs_running_flag": True, "pcs_standby_flag": False, "pcs_fault_flag": False,
        "pcs_status": "運轉", "battery_status": "運轉", "communication_ok": True,
        "rack_max_temp_c": 28.0, "rack_min_temp_c": 25.0,
        "alarms": [], "_fail": [],
    }


def _make_session(root):
    """建立一份合成 Session（4 筆取樣）。建立訊息不需要出現在驗證輸出中，故抑制。"""
    with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
        sess = CDR.ReportSession("auto", None, "交流有功", object(), root,
                                 now_fn=lambda: _clk["dt"])
        sess.start()
        for _ in range(4):
            _clk["dt"] += timedelta(seconds=5)
            sess.sample_once()
    return sess


def _finalize(sess):
    _clk["dt"] += timedelta(seconds=5)
    return sess.finalize("auto_stop")


def _events(folder):
    import csv as _csv
    with open(os.path.join(folder, CFG.FILE_EVENTS), encoding="utf-8-sig") as f:
        return [r["event_type"] for r in _csv.DictReader(f)]


def _state(folder):
    with open(os.path.join(folder, CFG.FILE_SESSION_STATE), encoding="utf-8") as f:
        return json.load(f)


def run():
    root = tempfile.mkdtemp(prefix="p37_lock_")
    print(f"暫存輸出根目錄（結束時刪除）：{root}")

    # ================================================================
    # A-1  wb.save 真 PermissionError ×2 → 第 3 次前解除占用 → 應成功
    # ================================================================
    print("\n[A-1] wb.save 真 PermissionError ×2 → 第 3 次成功")
    sess = _make_session(root)
    xlsx = os.path.join(sess.folder, CFG.FILE_XLSX)
    os.makedirs(xlsx, exist_ok=True)              # 同名目錄擋在 report.xlsx 位置
    calls = {"n": 0}
    _orig_retry = CDR._retry_file_op

    def _counting_retry(label, fn, **kw):
        def _wrapped():
            if label == CFG.FILE_XLSX:
                calls["n"] += 1
                if calls["n"] == 3 and os.path.isdir(xlsx):
                    os.rmdir(xlsx)                # 第 3 次嘗試前解除占用
            return fn()
        return _orig_retry(label, _wrapped, **kw)

    CDR._retry_file_op = _counting_retry
    try:
        _finalize(sess)
    finally:
        CDR._retry_file_op = _orig_retry
    st = _state(sess.folder)
    check(f"wb.save 實際被呼叫 {calls['n']} 次（須為 3）", calls["n"] == 3)
    check(f"output_status.xlsx = ok（實際 {st.get('output_status', {}).get('xlsx')}）",
          st.get("output_status", {}).get("xlsx") == CFG.OUTPUT_OK)
    check("report.xlsx 實際產生且為檔案", os.path.isfile(xlsx) and os.path.getsize(xlsx) > 0)
    check("events 記 finalize_ok", "finalize_ok" in _events(sess.folder))
    check(f"status = completed（實際 {st.get('status')}）",
          st.get("status") == CFG.SESSION_COMPLETED)

    # ================================================================
    # A-2  持續占用 → 重試耗盡：completed + failed_locked + finalize_partial，不拋例外
    # ================================================================
    print("\n[A-2] wb.save 持續 PermissionError → 重試耗盡")
    sess2 = _make_session(root)
    xlsx2 = os.path.join(sess2.folder, CFG.FILE_XLSX)
    os.makedirs(xlsx2, exist_ok=True)
    raised = None
    try:
        _finalize(sess2)
    except Exception as e:                        # noqa: BLE001
        raised = e
    st2 = _state(sess2.folder)
    check("finalize() 未向外拋例外", raised is None)
    check(f"output_status.xlsx = failed_locked（實際 {st2.get('output_status', {}).get('xlsx')}）",
          st2.get("output_status", {}).get("xlsx") == CFG.OUTPUT_FAILED_LOCKED)
    check(f"status 仍 = completed（實際 {st2.get('status')}）",
          st2.get("status") == CFG.SESSION_COMPLETED)
    check("events 記 finalize_partial", "finalize_partial" in _events(sess2.folder))
    check("events 未記 finalize_ok", "finalize_ok" not in _events(sess2.folder))
    for f in (CFG.FILE_SAMPLES, CFG.FILE_EVENTS, CFG.FILE_ALARMS,
              CFG.FILE_SUMMARY, CFG.FILE_STATISTICS):
        p = os.path.join(sess2.folder, f)
        check(f"原始資料完整保留：{f}", os.path.isfile(p) and os.path.getsize(p) > 0)
    os.rmdir(xlsx2)

    # ================================================================
    # B  os.replace 真 PermissionError（Windows 不可覆蓋開啟中的檔案）
    #    → plot_vis 重試；第 3 次前關閉 handle → 應成功
    # ================================================================
    print("\n[B] _force_plot_visible_all 的 os.replace 真 PermissionError → 釋放後成功")
    sess3 = _make_session(root)
    xlsx3 = os.path.join(sess3.folder, CFG.FILE_XLSX)
    holder = {"fh": None, "n": 0}
    _orig_force = CDR._force_plot_visible_all

    def _counting_force(path):
        holder["n"] += 1
        if holder["n"] == 3 and holder["fh"] is not None:
            holder["fh"].close()                  # 第 3 次嘗試前釋放檔案
            holder["fh"] = None
        return _orig_force(path)

    try:
        with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):
            sess3._write_xlsx(sess3._compute_statistics())    # 先產生實體 report.xlsx
        holder["fh"] = open(xlsx3, "rb")                      # 佔住 → os.replace 失敗
        CDR._force_plot_visible_all = _counting_force
        _finalize(sess3)
    finally:
        CDR._force_plot_visible_all = _orig_force
        if holder["fh"] is not None:
            holder["fh"].close()
    st3 = _state(sess3.folder)
    pv = st3.get("output_status", {}).get("plot_vis")
    check(f"os.replace 真的走進重試（實際嘗試 {holder['n']} 次 > 1）", holder["n"] > 1)
    check(f"plot_vis 最終 = ok（實際 {pv}）", pv == CFG.OUTPUT_OK)
    check(f"xlsx = ok（實際 {st3.get('output_status', {}).get('xlsx')}）",
          st3.get("output_status", {}).get("xlsx") == CFG.OUTPUT_OK)

    # ================================================================
    # C  無電表 Log（write_meter_report 回傳 None）→ skipped、**零等待**
    #    若把 None 誤判為可重試，此處會白等 (MAX-1)×WAIT 秒。
    # ================================================================
    print("\n[C] 無電表 Log → skipped 且完全不浪費重試等待")
    waits = []
    _orig_sleep = CDR.time.sleep
    CDR.time.sleep = waits.append
    sess4 = _make_session(root)
    try:
        _finalize(sess4)
    finally:
        CDR.time.sleep = _orig_sleep
    ms = _state(sess4.folder).get("output_status", {}).get("meter_xlsx")
    check(f"meter_xlsx = skipped（實際 {ms}）", ms == CFG.OUTPUT_SKIPPED)
    check(f"整段 finalize 完全沒有重試等待（實際等待 {waits}）", waits == [])

    shutil.rmtree(root, ignore_errors=True)
    return root


def main():
    # 暫時調短重試等待（只影響本行程記憶體，不寫檔）；結束時還原並自我驗證。
    _bak = (CDR.read_all, CDR._fetch_cell_packs, CFG.FINALIZE_RETRY_WAIT_SEC)
    CDR.read_all = _fake_read_all
    CDR._fetch_cell_packs = lambda _c: (None, "offline")
    CFG.FINALIZE_RETRY_WAIT_SEC = 0.4
    root = None
    try:
        root = run()
    except Exception as e:                        # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"驗證過程發生未預期例外：{type(e).__name__}: {e}", False)
    finally:
        CDR.read_all, CDR._fetch_cell_packs, CFG.FINALIZE_RETRY_WAIT_SEC = _bak
        if root:
            shutil.rmtree(root, ignore_errors=True)

    # 不得留下副作用
    check("結束後已還原 CDR.read_all 注入", CDR.read_all is _bak[0])
    check(f"結束後已還原 FINALIZE_RETRY_WAIT_SEC（{CFG.FINALIZE_RETRY_WAIT_SEC}）",
          CFG.FINALIZE_RETRY_WAIT_SEC == _bak[2])

    ok = all(RESULTS)
    print(f"\n== 真實檔案鎖驗證 {'PASS' if ok else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
