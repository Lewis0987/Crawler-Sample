# -*- coding: utf-8 -*-
"""
Session State Atomic Persistence Bugfix —— 離線定向測試（不連設備、不寫正式 output）
================================================================================
驗證 session_state.json 原子寫入（A-1／A-2）、Session 資料提交關口與狀態轉移（M-1～M-3）、
舊背景執行緒防護與逾時處理（M-4）、補產 R2（M-5：原 --regen 與 report_monitor.py --regen 受保護入口、
跨程序與多程序補產互斥）、最後一筆與收尾第①階段有限等待（M-6：結束狀態未取得不捏造）、
收尾失敗後合法重試的事件一致性、讀取失敗語意（A-4）、收尾證據（F-1）。
‧命令列入口以子程序執行：HTTP／登入／子程序／背景執行緒一律攔截並計數（零網路、不操作設備）。

‧所有資料寫入 tempfile 暫存目錄；report_monitor 的 output root 改指暫存目錄（錄製權限 Mutex 名稱由 root 推導，
  不會與正式服務共用）；read_all／Cell 快照抓取／告警中文對照一律替換為離線替身（零網路）。
‧競態以 Event／Barrier／受控替身確定重現；兩項 join 逾時測試依正式設定實際等待
  SAMPLE_INTERVAL_SEC＋10（目前 20 秒），不修改正式程式 timeout。
執行：python test_session_state_atomic.py
"""

import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import contextlib
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import charge_discharge_report as CDR          # noqa: E402
import charge_discharge_report_config as CFG   # noqa: E402
import report_monitor as RM                    # noqa: E402

TMP = tempfile.mkdtemp(prefix="ssa_")
_results = []


def check(tid, label, ok, detail=""):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tid} {label}" + (f" —— {detail}" if detail and not ok else ""))


def sha(p):
    try:
        return hashlib.sha256(open(p, "rb").read()).hexdigest()
    except OSError:
        return None


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def ev_types(path):
    """events.csv 的 event_type 欄位清單（不以子字串比對：說明欄可能含 reason=session_end）。"""
    import csv as _csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r.get("event_type") for r in _csv.DictReader(f)]


def tmp_leftovers(d):
    return glob.glob(os.path.join(d, ".*.tmp"))


def winerr(code, msg="injected"):
    return OSError(None, msg, None, code)          # Windows：依 winerror 對應子類別（32 → PermissionError）


# ----------------------------------------------------------------------------- 離線替身
CLK = {"dt": datetime(2026, 11, 26, 9, 0, 0)}


def now_fn():
    return CLK["dt"]


def reading(power=5.0, soc=60, direction="charge"):
    v = 890.0
    i = round(power * 1000.0 / v, 2) if power else 0.0
    return {
        "communication_ok": True, "raw_source_time": "",
        "pcs_charging_flag": direction == "charge", "pcs_discharging_flag": direction == "discharge",
        "soc_percent": float(soc), "battery_voltage_v": v, "battery_current_a": i,
        "rack_max_temperature_c": 31.0, "rack_min_temperature_c": 26.0,
        "battery_status": "充電" if power > 0 else ("放電" if power < 0 else "待機"),
        "actual_active_power_kw": float(power), "actual_reactive_power_kvar": 0.0,
        "calculated_power_kw": round(v * i / 1000, 3),
        "pcs_status": "執行", "pcs_control_mode": "智慧模式", "pcs_work_mode": "併網",
        "pcs_power_control_mode": "交流有功", "pcs_manual_switch": 0, "pcs_schedule_enabled": True,
        "pcs_fault_flag": False, "pcs_running_flag": True, "pcs_standby_flag": direction == "idle",
        "battery_power_status": "已上電", "device_daily_charge_kwh": 4.6, "device_daily_discharge_kwh": 3.7,
        "alarm_rows": [], "alarm_total": 0,
    }


READ = {"block_threads": set(), "gate": threading.Event(), "entered": threading.Event()}


def fake_read_all(_client):
    CLK["dt"] += timedelta(seconds=5)
    if threading.current_thread() in READ["block_threads"]:
        READ["entered"].set()
        READ["gate"].wait(120)                     # 模擬 HMI 讀取卡住（受控放行）
    return reading()


CDR.read_all = fake_read_all
CDR._fetch_cell_packs = lambda _c: (None, "offline-test")
CDR._fetch_alarm_zh_map = lambda: {}
RM._report_client = lambda quiet=False: object()
RM._cert_active_provider = None
OWN_ROOT = os.path.join(TMP, "owner_root")
os.makedirs(OWN_ROOT)
RM._report_output_root = lambda: OWN_ROOT


def new_session(root, start=True):
    os.makedirs(root, exist_ok=True)
    s = CDR.ReportSession("auto", 10.0, "交流有功", object(), root, now_fn=now_fn)
    if start:
        s.start()
    return s


def samples(sess, n):
    for _ in range(n):
        sess.sample_once()


def reset_report():
    RM._report.update(session=None, thread=None, stop=None, finalized=False, pending_end_reason=None,
                      stopping=False, last_join_timeout=False)
    RM._auto["state"] = RM.AUTO_IDLE


def write_events(folder, types, incomplete_tail=False):
    lines = [",".join(CFG.EVENT_FIELDS)]
    for i, t in enumerate(types):
        lines.append(f"2026-11-26 10:00:{i:02d},{i},{t},info,x")
    data = "\r\n".join(lines) + ("" if incomplete_tail else "\r\n")
    with open(os.path.join(folder, CFG.FILE_EVENTS), "w", encoding="utf-8-sig", newline="") as f:
        f.write(data)


def mk_folder(root, name, state=None, events=None, samples_csv=True, raw_state=None, incomplete=False):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    if raw_state is not None:
        with open(os.path.join(d, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as f:
            f.write(raw_state)
    elif state is not None:
        with open(os.path.join(d, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    if events is not None:
        write_events(d, events, incomplete_tail=incomplete)
    if samples_csv:
        with open(os.path.join(d, CFG.FILE_SAMPLES), "w", encoding="utf-8") as f:
            f.write(",".join(CFG.SAMPLE_FIELDS) + "\n")
    return d


def capture(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


ST = lambda status, **kw: dict({"session_id": "S1", "status": status}, **kw)    # noqa: E731

try:
    # ===================================================================== A-1／A-2 原子寫入
    print("\n== A-1／A-2 原子寫入 ==")
    d = os.path.join(TMP, "atomic")
    os.makedirs(d)
    p = os.path.join(d, CFG.FILE_SESSION_STATE)
    obj = {"session_id": "中文-1", "status": "recording", "sample_count": 3, "nested": {"a": [1, 2]}}
    ok, err = CDR.write_json_atomic(p, obj)
    ref = os.path.join(d, "ref.json")
    with open(ref, "w", encoding="utf-8") as f:             # 舊 _dump_json 的寫法（格式比對基準）
        json.dump(obj, f, ensure_ascii=False, indent=2)
    check("T01", "正常原子寫入：成功、內容與舊寫法逐位元組相同（欄位／縮排／編碼）、無殘留暫存檔",
          ok and err is None and open(p, "rb").read() == open(ref, "rb").read() and not tmp_leftovers(d), err)

    # T02／T03：替換前讀到舊完整檔；替換後讀到新完整檔
    real_replace = CDR._state_replace
    ev_tmp, ev_go = threading.Event(), threading.Event()

    def blocking_replace(src, dst):
        ev_tmp.set()
        ev_go.wait(10)
        return real_replace(src, dst)
    CDR._state_replace = blocking_replace
    old_bytes = open(p, "rb").read()
    res = {}
    th = threading.Thread(target=lambda: res.update(r=CDR.write_json_atomic(p, dict(obj, sample_count=99))))
    th.start()
    ev_tmp.wait(10)
    during = jload(p)
    tmps = tmp_leftovers(d)
    tmp_full = bool(tmps) and json.load(open(tmps[0], encoding="utf-8")).get("sample_count") == 99
    ev_go.set()
    th.join(10)
    CDR._state_replace = real_replace
    check("T02", "暫存檔寫入完成、替換前：正式檔仍為舊的完整 JSON；暫存檔為新的完整 JSON",
          during == obj and open(p, "rb").read() != b"" and tmp_full, str(during))
    check("T03", "替換後：正式檔為新的完整 JSON；暫存檔已消失", res.get("r", (False,))[0]
          and jload(p).get("sample_count") == 99 and not tmp_leftovers(d))

    # T03b：連續讀寫（讀取端不得讀到不完整 JSON）
    stop_rd, parse_err, io_err, reads = threading.Event(), [0], [0], [0]

    def reader():
        while not stop_rd.is_set():
            try:
                with open(p, encoding="utf-8") as f:
                    json.load(f)
                reads[0] += 1
            except ValueError:
                parse_err[0] += 1
            except OSError:
                io_err[0] += 1
    rt = threading.Thread(target=reader)
    rt.start()
    wres = [CDR.write_json_atomic(p, dict(obj, sample_count=k)) for k in range(300)]
    stop_rd.set()
    rt.join(10)
    fails = [w for w in wres if not w[0]]
    check("T03b", f"300 次原子寫入同時連續讀取：JSON 解析錯誤 0 次（讀取 {reads[0]} 次、讀取端 IO 錯誤 {io_err[0]}、"
                  f"寫入失敗 {len(fails)}，失敗皆有錯誤訊息）",
          parse_err[0] == 0 and all(w[1] for w in fails) and json.load(open(p, encoding="utf-8")) is not None
          and not tmp_leftovers(d), str(fails[:2]))

    # T04 暫時失敗後成功
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise winerr(32, "sharing violation (injected)")
        return real_replace(src, dst)
    CDR._state_replace = flaky_replace
    m = {}
    ok, err = CDR.write_json_atomic(p, dict(obj, sample_count=4), metrics=m)
    CDR._state_replace = real_replace
    check("T04", f"替換暫時失敗 2 次後成功：回傳成功、總嘗試 3 次、重試等待 {m.get('retry_sleep_ms', 0):.0f} ms、無殘留暫存檔",
          ok and m.get("attempts") == 3 and 140 <= m.get("retry_sleep_ms", 0) <= 200 and jload(p)["sample_count"] == 4
          and not tmp_leftovers(d), str(m))

    # T05／T06 持續失敗：原檔完整保留
    before = sha(p)

    def always_32(src, dst):
        raise winerr(32, "sharing violation (injected)")
    CDR._state_replace = always_32
    m = {}
    t0 = time.perf_counter()
    ok, err = CDR.write_json_atomic(p, dict(obj, sample_count=5), metrics=m)
    el = time.perf_counter() - t0
    CDR._state_replace = real_replace
    check("T05", f"替換持續失敗：回傳失敗（{err}）、總嘗試 {m.get('attempts')} 次（上限 {CDR.STATE_WRITE_MAX_ATTEMPTS}）、"
                 f"耗時 {el:.2f}s 有上限、只清除本次暫存檔",
          (not ok) and "PermissionError" in (err or "") and m.get("attempts") == CDR.STATE_WRITE_MAX_ATTEMPTS
          and el < 2.0 and not tmp_leftovers(d))
    check("T06", "持續失敗後原本完整的狀態檔 SHA 不變（不退回清空後直接寫入）", sha(p) == before)

    # T23 磁碟空間不足（非暫時性）：不重試；存取被拒（winerror 5）：有限重試後失敗；原檔皆保留
    def no_space(src, dst):
        raise OSError(28, "No space left on device (injected)")
    CDR._state_replace = no_space
    m = {}
    ok, err = CDR.write_json_atomic(p, dict(obj, sample_count=6), metrics=m)
    CDR._state_replace = real_replace
    ok23a = (not ok) and m.get("attempts") == 1 and sha(p) == before and not tmp_leftovers(d)

    def denied(src, dst):
        raise winerr(5, "access denied (injected)")
    CDR._state_replace = denied
    m2 = {}
    ok2, err2 = CDR.write_json_atomic(p, dict(obj, sample_count=7), metrics=m2)
    CDR._state_replace = real_replace
    check("T23", f"磁碟空間不足：不重試（嘗試 {m.get('attempts')} 次）；存取被拒：有限重試（{m2.get('attempts')} 次）後失敗；"
                 "兩者原檔 SHA 不變、無殘留暫存檔",
          ok23a and (not ok2) and m2.get("attempts") == CDR.STATE_WRITE_MAX_ATTEMPTS and sha(p) == before
          and not tmp_leftovers(d), f"{err} / {err2}")

    # T24 fsync 失敗：不替換、原檔保留
    real_fsync = CDR._state_fsync

    def bad_fsync(fd):
        raise OSError(5, "fsync I/O error (injected)")
    CDR._state_fsync = bad_fsync
    m = {}
    ok, err = CDR.write_json_atomic(p, dict(obj, sample_count=8), fsync=True, metrics=m)
    CDR._state_fsync = real_fsync
    check("T24", "fsync 失敗：回傳失敗、未執行替換（嘗試 0 次）、原檔 SHA 不變、暫存檔已清除",
          (not ok) and m.get("attempts") == 0 and sha(p) == before and not tmp_leftovers(d), err)

    # T12 序列化例外：原檔保留、無暫存檔
    ok, err = CDR.write_json_atomic(p, {"bad": object()})
    check("T12", "JSON 序列化例外：回傳失敗（TypeError）、原檔 SHA 不變、無殘留暫存檔",
          (not ok) and "TypeError" in (err or "") and sha(p) == before and not tmp_leftovers(d), err)

    # ===================================================================== M-1～M-3 Session 內
    print("\n== M-1～M-3 Session 資料提交關口／狀態轉移／狀態檔原子寫入 ==")
    R1 = os.path.join(TMP, "r1")
    s1 = new_session(R1)
    samples(s1, 3)
    st1 = jload(os.path.join(s1.folder, CFG.FILE_SESSION_STATE))
    check("T38a", "正常流程：開始＋3 筆取樣 → 狀態檔 recording、sample_count＝3、累積電量＞0、無遲到捨棄",
          st1["status"] == "recording" and st1["sample_count"] == 3 and st1["cumulative_charge_energy_kwh"] > 0
          and s1.late_samples_discarded == 0)

    # T07 多寫入者：兩執行緒同時寫同一 Session 的狀態檔
    bar = threading.Barrier(3)
    errs7 = []

    def hammer_persist():
        try:
            bar.wait(5)
            for _ in range(60):
                s1._persist_state()
        except Exception as e:                         # noqa: BLE001
            errs7.append(e)
    ths = [threading.Thread(target=hammer_persist) for _ in range(2)]
    for t in ths:
        t.start()
    bar.wait(5)
    for _ in range(30):
        s1.sample_once()                               # 同時有取樣提交（狀態內容持續改變）
    for t in ths:
        t.join(30)
    st7 = jload(os.path.join(s1.folder, CFG.FILE_SESSION_STATE))
    check("T07", f"兩個執行緒＋取樣同時寫狀態檔：無例外、最終檔案完整且＝最後狀態（sample_count {st7['sample_count']}＝"
                 f"{s1.sample_index}）、無殘留暫存檔",
          not errs7 and st7["sample_count"] == s1.sample_index and not tmp_leftovers(s1.folder), str(errs7[:1]))

    # T09b：取樣提交在私有鎖內停住 → pause 必須等待 → 放行後 pause 完成，最終 paused 且包含該筆
    R9 = os.path.join(TMP, "r9")
    s9 = new_session(R9)
    samples(s9, 2)
    in_lock, go = threading.Event(), threading.Event()
    orig_commit = s9._commit_sample

    def slow_commit(r, el):
        in_lock.set()
        go.wait(15)
        return orig_commit(r, el)
    s9._commit_sample = slow_commit
    ta = threading.Thread(target=s9.sample_once)
    ta.start()
    in_lock.wait(10)
    pause_res = {}
    tb = threading.Thread(target=lambda: pause_res.update(r=s9.pause()))
    tb.start()
    time.sleep(0.6)
    waiting = tb.is_alive() and jload(os.path.join(s9.folder, CFG.FILE_SESSION_STATE))["status"] == "recording"
    go.set()
    ta.join(30)
    tb.join(60)
    st9 = jload(os.path.join(s9.folder, CFG.FILE_SESSION_STATE))
    check("T09b", "提交在鎖內停住時 pause 確實等待（測試端有時限等待，無死結）；放行後該筆先提交、"
                  "pause 才完成 → 最終 paused、sample_count＝3",
          waiting and not ta.is_alive() and not tb.is_alive() and pause_res.get("r") is not None
          and st9["status"] == "paused" and st9["sample_count"] == 3, str(st9.get("status")))

    # T09a／T15：讀取返回前已 pause → 遲到取樣捨棄（不增序號、不寫 samples／events／狀態檔）
    s9b = new_session(os.path.join(TMP, "r9b"))
    samples(s9b, 2)
    READ["gate"].clear()
    READ["entered"].clear()
    late = threading.Thread(target=s9b.sample_once)
    READ["block_threads"].add(late)
    late.start()
    READ["entered"].wait(10)
    s9b.pause()
    f_s, f_e, f_st = (os.path.join(s9b.folder, n) for n in (CFG.FILE_SAMPLES, CFG.FILE_EVENTS, CFG.FILE_SESSION_STATE))
    h_before = (sha(f_s), sha(f_e), sha(f_st), s9b.sample_index, s9b.energy.charged_kwh)
    READ["gate"].set()
    late.join(10)
    READ["block_threads"].discard(late)
    check("T09a", "背景讀取返回前已 pause：遲到取樣被捨棄（late_samples_discarded＝1），samples／events／狀態檔 SHA、"
                  "序號、累積電量全部不變，狀態維持 paused",
          s9b.late_samples_discarded == 1 and (sha(f_s), sha(f_e), sha(f_st), s9b.sample_index, s9b.energy.charged_kwh)
          == h_before and jload(f_st)["status"] == "paused")

    # T25：同一 Session 不得兩條收尾路徑；T27：合法 paused Session 收尾
    r27, out27 = capture(s9b.finalize, "manual_stop")
    ev27 = open(f_e, encoding="utf-8-sig").read()
    check("T27", "合法 paused Session 收尾：完成（completed）、events 有 session_end 與 finalize_ok/partial",
          r27 is not None and jload(f_st)["status"] == "completed" and ev_types(f_e).count("session_end") == 1
          and ("finalize_ok" in ev_types(f_e) or "finalize_partial" in ev_types(f_e)))
    r25, out25 = capture(s9b.finalize, "manual_stop")
    check("T25", "同一 Session 第二次收尾被拒絕（回傳 None、記錄原因）、session_end 只出現一次、仍為 completed",
          r25 is None and "finalize 拒絕" in out25 and ev_types(f_e).count("session_end") == 1
          and jload(f_st)["status"] == "completed")
    # T25c：兩條收尾路徑同時發生 → 只有一條執行
    s25c = new_session(os.path.join(TMP, "r25c"))
    samples(s25c, 2)
    bar25 = threading.Barrier(2)
    res25c = []

    def fin25():
        bar25.wait(5)
        with contextlib.redirect_stdout(io.StringIO()):
            res25c.append(s25c.finalize("manual_stop"))
    t25 = [threading.Thread(target=fin25) for _ in range(2)]
    for t in t25:
        t.start()
    for t in t25:
        t.join(120)
    e25c = ev_types(os.path.join(s25c.folder, CFG.FILE_EVENTS))
    check("T25c", "兩條收尾路徑同時呼叫 finalize：只有一條執行（一個回傳結果、一個回傳 None）、session_end／finalize_* 各一筆、completed",
          sorted(r is None for r in res25c) == [False, True] and e25c.count("session_end") == 1
          and (e25c.count("finalize_ok") + e25c.count("finalize_partial")) == 1
          and jload(os.path.join(s25c.folder, CFG.FILE_SESSION_STATE))["status"] == "completed", str(res25c))
    # T25d：首次收尾失敗 → 不得誤判完成；解除收尾中 → 合法重試（Menu 19）可完成
    s25d = new_session(os.path.join(TMP, "r25d"))
    samples(s25d, 2)
    orig_wo = s25d._write_outputs

    def boom():
        raise RuntimeError("injected output failure")
    s25d._write_outputs = boom
    try:
        capture(s25d.finalize, "manual_stop")
        raised25d = False
    except RuntimeError:
        raised25d = True
    st25d1 = jload(os.path.join(s25d.folder, CFG.FILE_SESSION_STATE))["status"]
    k25d = CDR.classify_session_folder(s25d.folder)
    s25d._write_outputs = orig_wo
    r25d2, _o = capture(s25d.finalize, "manual_stop")
    check("T25d", f"首次收尾失敗：拋出例外、狀態檔仍為 {st25d1}（未誤判完成）、下次啟動分類＝{k25d[0]}（收尾中斷 → 人工確認）；"
                  "解除收尾中後合法重試完成（completed）",
          raised25d and st25d1 == "recording" and k25d[0] == "unknown" and "finalize_interrupted" in k25d[1]
          and r25d2 is not None and jload(os.path.join(s25d.folder, CFG.FILE_SESSION_STATE))["status"] == "completed")

    r25b, out25b = capture(s9b.pause)
    check("T25b", "已完成的 Session 不得再暫停（回傳 None）、狀態維持 completed",
          r25b is None and "pause 拒絕" in out25b and jload(f_st)["status"] == "completed")

    # ===================================================================== A-4 讀取分類／resume
    print("\n== A-4 讀取失敗語意／resume ==")
    RA = os.path.join(TMP, "ra")
    os.makedirs(RA)
    pa = os.path.join(RA, "x.json")
    k_missing = CDR.read_session_state(pa)[0]
    with open(pa, "w", encoding="utf-8") as f:
        f.write('{"session_id": "S1", "status": "recor')
    t0 = time.perf_counter()
    k_parse = CDR.read_session_state(pa)[0]
    t_parse = time.perf_counter() - t0
    with open(pa, "w", encoding="utf-8") as f:
        json.dump({"session_id": "", "status": "recording"}, f)
    k_inv1 = CDR.read_session_state(pa)[0]
    with open(pa, "w", encoding="utf-8") as f:
        json.dump({"session_id": "S1", "status": "weird"}, f)
    k_inv2 = CDR.read_session_state(pa)[0]
    with open(pa, "w", encoding="utf-8") as f:
        json.dump(["not", "dict"], f)
    k_inv3 = CDR.read_session_state(pa)[0]
    check("A4-1", f"分類：不存在＝MISSING、解析失敗＝PARSE_ERROR（含短暫重試 {t_parse:.2f}s）、空 session_id／未知 status／"
                  "非 dict＝INVALID",
          k_missing == "MISSING" and k_parse == "PARSE_ERROR" and t_parse >= 0.25
          and (k_inv1, k_inv2, k_inv3) == ("INVALID",) * 3)
    legacy = {"session_id": "L", "status": "completed", "cumulative_charge_energy_kwh": 1.2, "output_status": {}}
    with open(pa, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    k_leg = CDR.read_session_state(pa)
    legacy_rec = mk_folder(RA, "legacy_rec", state={"session_id": "LR", "status": "recording"},
                           events=["session_start", "charge_start"])
    s_leg = CDR.ReportSession.resume(legacy_rec, object(), now_fn=now_fn)
    check("T20", "舊版 Session 相容：只有 4 欄的 completed 狀態檔＝OK；只有 session_id／status 的 recording 可正常 resume",
          k_leg[0] == "OK" and s_leg.session_id == "LR" and s_leg.status == "recording")

    # T10 resume 保留累積電量；讀不到 → 拒絕（不以空內容續接、不新建）
    R10 = os.path.join(TMP, "r10")
    s10 = new_session(R10)
    samples(s10, 4)
    s10.pause()
    _st10 = jload(os.path.join(s10.folder, CFG.FILE_SESSION_STATE))   # 狀態檔累積電量依既有格式取 4 位小數
    e_before = (_st10["cumulative_charge_energy_kwh"], _st10["sample_count"])
    s10r = CDR.ReportSession.resume(s10.folder, object(), now_fn=now_fn)
    check("T10a", f"正常續接：累積充電量與筆數＝狀態檔保存值（{e_before}；即時值 {round(s10.energy.charged_kwh, 4)}）",
          (round(s10r.energy.charged_kwh, 4), s10r.sample_index) == e_before
          and e_before == (round(s10.energy.charged_kwh, 4), s10.sample_index))
    with open(os.path.join(s10.folder, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as f:
        f.write('{"session_id": "x", "stat')
    n_dirs = len(os.listdir(R10))
    try:
        CDR.ReportSession.resume(s10.folder, object(), now_fn=now_fn)
        raised = False
    except CDR.SessionStateError:
        raised = True
    check("T10b", "狀態檔損壞時 resume 拋 SessionStateError（不以空內容續接）、未新建資料夾",
          raised and len(os.listdir(R10)) == n_dirs)
    try:
        CDR.ReportSession.resume(s9b.folder, object(), now_fn=now_fn)
        raised_c = False
    except CDR.SessionStateError:
        raised_c = True
    check("T10c", "completed Session 不得續接（拋 SessionStateError）；補產用 allow_completed 才可載入",
          raised_c and CDR.ReportSession.resume(s9b.folder, object(), allow_completed=True).status == "recording")

    # T22 初次建立狀態檔失敗 → start 拋例外、不啟動；資料夾保留；之後分類為 unknown
    R22 = os.path.join(TMP, "r22")
    CDR._state_replace = always_32
    s22 = new_session(R22, start=False)
    try:
        capture(s22.start)
        raised22 = False
    except CDR.SessionStateError:
        raised22 = True
    CDR._state_replace = real_replace
    k22 = CDR.classify_session_folder(s22.folder)
    check("T22", f"初次建立狀態檔失敗：start 拋 SessionStateError；資料夾保留、無狀態檔；分類＝{k22}",
          raised22 and os.path.isdir(s22.folder) and not os.path.exists(os.path.join(s22.folder, CFG.FILE_SESSION_STATE))
          and k22[0] == "unknown" and k22[1] == "missing_state_with_data")

    # ===================================================================== F-1 收尾證據
    print("\n== F-1 收尾證據／A-4 資料夾判定 ==")
    RF = os.path.join(TMP, "rf")
    os.makedirs(RF)
    cases = {
        "ok": (ST("recording"), ["session_start", "session_end", "finalize_ok"], False, "finalized_ok"),
        "partial": (ST("paused"), ["session_start", "session_end", "finalize_partial"], False, "finalized_partial"),
        "interrupted": (ST("recording"), ["session_start", "charge_start", "session_end"], False, "unknown"),
        "incomplete_tail": (ST("recording"), ["session_start", "session_end", "finalize_ok"], True, "unknown"),
        "fin_without_end": (ST("recording"), ["session_start", "finalize_ok"], False, "unknown"),
        "resumed_ok": (ST("paused"), ["session_start", "recording_pause", "recording_resume", "charge_start"], False, "resumable"),
        "end_then_resume": (ST("recording"), ["session_start", "session_end", "finalize_ok", "recording_resume"], False, "resumable"),
        "completed": (ST("completed"), ["session_start", "session_end", "finalize_ok"], False, "completed"),
    }
    got = {}
    for name, (st, evs, inc, exp) in cases.items():
        fd = mk_folder(os.path.join(RF, name), "S", state=st, events=evs, incomplete=inc)
        got[name] = CDR.classify_session_folder(fd)
    check("T26", "收尾證據判定：session_end＋finalize_ok→finalized_ok；＋finalize_partial→finalized_partial；"
                 "只有 session_end→unknown(收尾中斷)；最後一行不完整→unknown；finalize 先於 session_end→unknown；"
                 "最後一次續接之後無收尾→resumable；completed→completed",
          all(got[n][0] == cases[n][3] for n in cases) and "finalize_interrupted" in got["interrupted"][1],
          str({n: got[n] for n in cases if got[n][0] != cases[n][3]}))
    fd_noev = mk_folder(os.path.join(RF, "noev"), "S", state=ST("recording"), events=None)
    check("T26b", "events.csv 不存在（狀態 recording）→ unknown（不視為可續接或已完成）",
          CDR.classify_session_folder(fd_noev)[0] == "unknown")

    # T33：狀態檔損壞且很舊，但仍有未完成證據 → unknown（不因老舊放行）
    fd33 = mk_folder(os.path.join(RF, "old_corrupt"), "S", raw_state='{"session_id": "S1", "sta',
                     events=["session_start", "charge_start"])
    old = time.time() - 86400
    os.utime(os.path.join(fd33, CFG.FILE_SESSION_STATE), (old, old))
    fd33b = mk_folder(os.path.join(RF, "old_corrupt_finalized"), "S", raw_state="{bad",
                      events=["session_start", "session_end", "finalize_ok"])
    check("T33", "狀態檔損壞超過 20 秒（1 天前）且無收尾證據 → unknown；損壞但有可靠收尾證據 → finalized_ok（警告略過、不修改）",
          CDR.classify_session_folder(fd33)[0] == "unknown"
          and CDR.classify_session_folder(fd33b)[0] == "finalized_ok"
          and open(os.path.join(fd33b, CFG.FILE_SESSION_STATE), encoding="utf-8").read() == "{bad")

    # T34：缺狀態檔但有 samples.csv → unknown；完全沒有資料 → none
    fd34 = mk_folder(os.path.join(RF, "nostate"), "S", state=None, events=None, samples_csv=True)
    fd34b = os.path.join(RF, "empty", "S")
    os.makedirs(fd34b)
    check("T34", "缺少狀態檔但有 samples.csv → unknown（不得視為不存在）；空資料夾 → none",
          CDR.classify_session_folder(fd34)[0] == "unknown" and CDR.classify_session_folder(fd34b)[0] == "none")

    # T19／T35：同時存在可續接與無法確認 → checked 回報兩者；report_start 拒絕新建與續接
    R35 = os.path.join(TMP, "r35")
    mk_folder(R35, "A_resumable", state=ST("paused"), events=["session_start", "recording_pause"])
    mk_folder(R35, "B_unknown", raw_state="{", events=["session_start"])
    mk_folder(R35, "C_done", state=ST("completed"), events=["session_start", "session_end", "finalize_ok"])
    mk_folder(R35, "D_finalized_unpersisted", state=ST("recording"),
              events=["session_start", "session_end", "finalize_partial"])
    act, unk, wrn = CDR.find_active_session(R35, checked=True)
    check("T35", "checked 掃描：可續接 A、無法確認 B、已收尾未標記 D（警告）、completed C 忽略",
          act and act.endswith("A_resumable") and [os.path.basename(u[0]) for u in unk] == ["B_unknown"]
          and [os.path.basename(w[0]) for w in wrn] == ["D_finalized_unpersisted"])
    try:
        CDR.find_active_session(None)
        te = False
    except TypeError:
        te = True
    check("T35b", "未傳 checked 的舊呼叫行為不變（回傳單一資料夾字串；None root 仍拋 TypeError）",
          CDR.find_active_session(R35).endswith(("A_resumable", "D_finalized_unpersisted")) and te)
    RM._report_output_root = lambda: R35
    acq, why = RM.acquire_ownership(role="ssa-test")
    reset_report()
    n35 = sorted(os.listdir(R35))
    _r, out35 = capture(RM.report_start)
    check("T19", "同時有可續接與無法確認的 Session：report_start 拒絕新建與續接、列出待人工確認路徑、未新增資料夾、"
                 "未啟動背景執行緒、未改寫任何狀態檔",
          acq and "請人工確認" in out35 and "B_unknown" in out35 and sorted(os.listdir(R35)) == n35
          and RM._report["session"] is None and open(os.path.join(R35, "B_unknown", CFG.FILE_SESSION_STATE)).read() == "{",
          out35[-300:])
    R36 = os.path.join(TMP, "r36")
    mk_folder(R36, "I", state=ST("recording"), events=["session_start", "session_end"])
    RM._report_output_root = lambda: R36
    _r, out36 = capture(RM.report_resume_on_launch)
    check("T36", "已寫 session_end 但收尾未完成（收尾中斷）：啟動時自動續接被拒絕、列出路徑、不新建",
          "finalize_interrupted" in out36 and RM._report["session"] is None and os.listdir(R36) == ["I"], out36[-300:])

    # ===================================================================== M-4／M-6 背景執行緒
    print("\n== M-4／M-6 背景執行緒逾時與新舊 Session（實際等待 20 秒 ×2）==")
    R8 = os.path.join(TMP, "r8")
    os.makedirs(R8)
    RM._report_output_root = lambda: R8
    reset_report()
    s8 = new_session(R8)
    READ["gate"].clear()
    READ["entered"].clear()
    RM._report_bg_start(s8)
    old_th, old_stop, old_gen = RM._report["thread"], RM._report["stop"], RM._report["gen"]
    READ["block_threads"].add(old_th)
    # 第一筆背景取樣即卡住（持有 _CLIENT_LOCK）
    READ["entered"].wait(30)
    f8 = {n: os.path.join(s8.folder, n) for n in (CFG.FILE_SAMPLES, CFG.FILE_EVENTS, CFG.FILE_SESSION_STATE)}
    t0 = time.perf_counter()
    _r, out8 = capture(RM.report_pause)
    el8 = time.perf_counter() - t0
    st8 = jload(f8[CFG.FILE_SESSION_STATE])
    ev8 = open(f8[CFG.FILE_EVENTS], encoding="utf-8-sig").read()
    exp_wait = CFG.SAMPLE_INTERVAL_SEC + 10
    check("T08", f"report_pause 等待逾時：實際等待 {el8:.1f}s（設定 {exp_wait}s）、主控台與 events 記錄 "
                 "bg_thread_join_timeout、last_join_timeout＝True、Session 轉為 paused（背景仍卡住）",
          exp_wait <= el8 < exp_wait + 15 and "bg_thread_join_timeout" in ev8 and "未在" in out8
          and RM._report["last_join_timeout"] is True and st8["status"] == "paused" and old_th.is_alive(), out8[-300:])
    h8 = {n: sha(p_) for n, p_ in f8.items()}

    # T28／T29：舊執行緒延遲中，新 Session 已啟動
    R8n = os.path.join(TMP, "r8n")
    os.makedirs(R8n)
    s8n = new_session(R8n)
    RM._report_bg_start(s8n)
    new_th, new_gen, new_stop = RM._report["thread"], RM._report["gen"], RM._report["stop"]
    r29, out29 = capture(RM._auto_finalize_from_loop, s8, "auto_stop", gen=old_gen, stop=old_stop)
    ok29a = (r29 is True and RM._report["session"] is s8n and RM._report["gen"] == new_gen
             and RM._report["stopping"] is False and RM._report["finalized"] is False and s8n.status == "recording")
    fake_stop = threading.Event()                          # 即使舊執行緒的 stop 未設定，世代不符仍拒絕
    r29b, _o = capture(RM._auto_finalize_from_loop, s8, "auto_stop", gen=old_gen, stop=fake_stop)
    r29c, _o = capture(RM._report_finalize, "auto_stop", expect=s8)
    READ["gate"].set()                                    # 放行舊執行緒（遲到返回）
    old_th.join(15)
    READ["block_threads"].discard(old_th)
    check("T28", "舊執行緒在新 Session 啟動後才返回：遲到取樣被捨棄、舊 Session 的 samples／events／狀態檔 SHA 全部不變、"
                 "舊執行緒已結束",
          (not old_th.is_alive()) and s8.late_samples_discarded >= 1 and {n: sha(p_) for n, p_ in f8.items()} == h8)
    check("T29", "舊執行緒不得收尾或清除新 Session：自動收尾放棄、_report 仍指向新 Session、世代／停止旗標／finalized／"
                 "冷卻狀態未被改變、錄製權限仍由本程序持有",
          ok29a and r29b is True and r29c == (None, None) and RM._report["session"] is s8n
          and RM._report["gen"] == new_gen and RM._report["stopping"] is False and RM.is_owner() is True
          and RM._auto["state"] != RM.AUTO_COOLDOWN and s8n.status == "recording", out29[-200:])

    # T16／T17：_stop_and_finalize 等待逾時 → 略過最後一筆、完成收尾、報告後不再有資料寫入
    READ["gate"].clear()
    READ["entered"].clear()
    READ["block_threads"].add(new_th)
    READ["entered"].wait(30)
    f8n = {n: os.path.join(s8n.folder, n) for n in (CFG.FILE_SAMPLES, CFG.FILE_EVENTS, CFG.FILE_SESSION_STATE)}
    res16, buf16 = {}, io.StringIO()

    def run16():
        with contextlib.redirect_stdout(buf16):
            res16["t0"] = time.perf_counter()
            res16["r"] = RM._stop_and_finalize("manual_stop")
            res16["t1"] = time.perf_counter()
    t16 = threading.Thread(target=run16)
    t16.start()
    deadline = time.time() + exp_wait + 20
    while time.time() < deadline and "final_sample_skipped" not in open(f8n[CFG.FILE_EVENTS], encoding="utf-8-sig").read():
        time.sleep(0.2)
    time.sleep(1.0)
    # 收尾第①階段需要 _CLIENT_LOCK（卡住的背景讀取仍持有）→ 此時收尾必定在等待、尚未產生報告
    waiting16 = t16.is_alive() and s8n._finalizing and jload(f8n[CFG.FILE_SESSION_STATE])["status"] == "recording"
    h_samples = sha(f8n[CFG.FILE_SAMPLES])
    READ["gate"].set()                                     # 背景讀取返回 → 遲到取樣捨棄 → 釋放 _CLIENT_LOCK
    t16.join(120)
    new_th.join(15)
    READ["block_threads"].discard(new_th)
    out16 = buf16.getvalue()
    el16 = res16.get("t1", 0) - res16.get("t0", 0)
    ev16 = open(f8n[CFG.FILE_EVENTS], encoding="utf-8-sig").read()
    st16 = jload(f8n[CFG.FILE_SESSION_STATE])
    stats16 = (res16.get("r") or (None, None))[1]
    check("T16", f"_stop_and_finalize 等待逾時：記錄 bg_thread_join_timeout 與 final_sample_skipped（未等 _CLIENT_LOCK 取最後一筆）、"
                 f"收尾完成（completed，共 {el16:.1f}s）",
          el16 >= exp_wait and "bg_thread_join_timeout" in ev16 and "final_sample_skipped" in ev16
          and st16["status"] == "completed" and stats16 is not None, out16[-300:])
    check("T17", "收尾開始後背景讀取才返回：收尾在等待期間尚未產生報告；返回的遲到取樣被捨棄、samples.csv SHA 不變、"
                 "未觸發任何狀態轉移；背景執行緒已結束",
          waiting16 and s8n.late_samples_discarded >= 1 and sha(f8n[CFG.FILE_SAMPLES]) == h_samples
          and not new_th.is_alive() and ev_types(f8n[CFG.FILE_EVENTS]).count("session_end") == 1)
    h16 = {n: sha(p_) for n, p_ in f8n.items()}
    time.sleep(1.0)
    check("T30", "收尾完成後原始資料零異動（samples／events／狀態檔 SHA 不再改變）",
          {n: sha(p_) for n, p_ in f8n.items()} == h16)

    # T32：join 正常、但 _CLIENT_LOCK 被其他持有者占住 → 最多等 10 秒後略過最後一筆
    R32 = os.path.join(TMP, "r32")
    os.makedirs(R32)
    RM._report_output_root = lambda: R32
    reset_report()
    s32 = new_session(R32)
    RM._report_bg_start(s32)
    time.sleep(0.5)
    hold_go = threading.Event()

    def holder():
        with RM._CLIENT_LOCK:
            hold_go.wait(30)
    hth = threading.Thread(target=holder)
    hth.start()
    time.sleep(0.3)
    threading.Timer(CFG.SAMPLE_INTERVAL_SEC + 2, hold_go.set).start()   # 略過最後一筆後才釋放（讓 finalize 繼續）
    t0 = time.perf_counter()
    (s32r, st32), out32 = capture(RM._stop_and_finalize, "manual_stop")
    el32 = time.perf_counter() - t0
    hth.join(30)
    ev32 = open(os.path.join(s32.folder, CFG.FILE_EVENTS), encoding="utf-8-sig").read()
    check("T32", f"_CLIENT_LOCK 持續無法取得：最多等 {CFG.SAMPLE_INTERVAL_SEC}s 後略過最後一筆（記錄 final_sample_skipped）、"
                 f"不無限阻塞（共 {el32:.1f}s）、收尾完成",
          "final_sample_skipped" in ev32 and "無法取得共用 client 鎖" in ev32 and CFG.SAMPLE_INTERVAL_SEC <= el32
          and st32 is not None and jload(os.path.join(s32.folder, CFG.FILE_SESSION_STATE))["status"] == "completed",
          out32[-300:])

    # T18：狀態寫入失敗後 Owner／Session 一致性
    R18 = os.path.join(TMP, "r18")
    os.makedirs(R18)
    RM._report_output_root = lambda: R18
    reset_report()
    s18 = new_session(R18)
    st18_before = sha(os.path.join(s18.folder, CFG.FILE_SESSION_STATE))
    CDR._state_replace = always_32
    n_rows_before = len(open(os.path.join(s18.folder, CFG.FILE_SAMPLES), encoding="utf-8").read().splitlines())
    _r, o18 = capture(samples, s18, 2)
    n_rows_after = len(open(os.path.join(s18.folder, CFG.FILE_SAMPLES), encoding="utf-8").read().splitlines())
    RM._report.update(session=s18, thread=None, stop=threading.Event(), finalized=False)
    _r, o18p = capture(RM.report_pause)
    CDR._state_replace = real_replace
    st18 = jload(os.path.join(s18.folder, CFG.FILE_SESSION_STATE))
    k18 = CDR.classify_session_folder(s18.folder)
    check("T18", "狀態寫入持續失敗：取樣資料照常寫入 samples.csv、state_persist_ok＝False、狀態檔保留上一份完整內容；"
                 "暫停時明確顯示「暫停狀態未寫入」（不印已暫停）；錄製權限仍有效；下次啟動分類為可續接（resumable）",
          n_rows_after == n_rows_before + 2 and s18.state_persist_ok is False and sha(os.path.join(s18.folder,
          CFG.FILE_SESSION_STATE)) == st18_before and "暫停狀態未寫入" in o18p and "已暫停（下次可續接）" not in o18p
          and RM.is_owner() is True and st18["status"] == "recording" and k18[0] == "resumable", o18p[-300:])

    # T37：finalize_partial 後 completed 寫入失敗 → 下次啟動不續接（finalized_partial）
    R37 = os.path.join(TMP, "r37")
    s37 = new_session(R37)
    samples(s37, 2)
    s37._write_xlsx = lambda stats: (s37._record_output("xlsx", CFG.OUTPUT_FAILED), False)[1]
    orig_persist = CDR.write_json_atomic

    def fail_completed(path, obj, **kw):
        if obj.get("status") == "completed":
            return False, "PermissionError: injected"
        return orig_persist(path, obj, **kw)
    CDR.write_json_atomic = fail_completed
    _r, o37 = capture(s37.finalize, "manual_stop")
    CDR.write_json_atomic = orig_persist
    k37 = CDR.classify_session_folder(s37.folder)
    check("T37", f"finalize_partial 且 completed 寫入失敗：明確警告（未標記 completed）、狀態檔仍為 recording、"
                 f"分類＝{k37[0]}（不續接、不宣稱完整）",
          "未能標記 completed" in o37 and jload(os.path.join(s37.folder, CFG.FILE_SESSION_STATE))["status"] == "recording"
          and k37[0] == "finalized_partial", o37[-300:])

    # ===================================================================== M-5 補產（R2）
    print("\n== M-5 --regen：R2 跨程序互斥 ==")
    RG = os.path.join(TMP, "rg")
    os.makedirs(RG)
    RM._report_output_root = lambda: RG
    RM.release_ownership()
    reset_report()
    # completed：維持既有補產
    sg = new_session(RG)
    samples(sg, 3)
    sg.finalize("manual_stop")
    _r, og = capture(CDR.regenerate_report, sg.folder)
    check("T11", "completed Session 補產照舊允許、output_status 同步成功、status 維持 completed",
          _r is True and jload(os.path.join(sg.folder, CFG.FILE_SESSION_STATE))["status"] == "completed", og[-200:])
    # recording：拒絕
    sr = new_session(RG)
    samples(sr, 2)
    hr = sha(os.path.join(sr.folder, CFG.FILE_SESSION_STATE))
    _r, orr = capture(CDR.regenerate_report, sr.folder)
    check("T21", "recording Session 禁止補產（回傳 False、狀態檔 SHA 不變）",
          _r is False and "錄製中" in orr and sha(os.path.join(sr.folder, CFG.FILE_SESSION_STATE)) == hr)
    # unknown：拒絕
    su = mk_folder(RG, "zz_unknown", raw_state="{", events=["session_start"])
    _r, ou = capture(CDR.regenerate_report, su)
    check("T21b", "狀態無法確認 → 拒絕補產", _r is False and "無法確認" in ou)
    # 原指令（直接函式呼叫、無錄製權限）對 paused → 拒絕並提示改用受保護入口
    sr.pause()
    st_path = os.path.join(sr.folder, CFG.FILE_SESSION_STATE)
    h42 = {n: sha(os.path.join(sr.folder, n)) for n in os.listdir(sr.folder)}
    _r, o42 = capture(CDR.regenerate_report, sr.folder)
    _r42b, o42b = capture(CDR.regenerate_report, sr.folder, owner_check=lambda: False)
    check("T42", "paused Session：原補產函式（無錄製權限）拒絕並提示 python test/report_monitor.py --regen；"
                 "呼叫端注入的權限查核為否亦拒絕；資料夾內所有檔案 SHA 不變、無殘留鎖檔",
          _r is False and "report_monitor.py --regen" in o42 and _r42b is False and "未持有" in o42b
          and {n: sha(os.path.join(sr.folder, n)) for n in os.listdir(sr.folder)} == h42, o42[-200:])
    # paused、本程序可取得權限（受保護入口）：補產期間持有、結束後釋放
    seen_owner = []
    orig_wx = CDR.ReportSession._write_xlsx

    def spy_wx(self, stats):
        seen_owner.append(RM.is_owner())
        return orig_wx(self, stats)
    CDR.ReportSession._write_xlsx = spy_wx
    _r, op = capture(RM.regenerate_report_guarded, sr.folder)
    CDR.ReportSession._write_xlsx = orig_wx
    check("T31a", "paused Session：受保護入口取得錄製權限後補產（補產期間持有權限）、完成後釋放、狀態維持 paused",
          _r is True and seen_owner == [True] and RM.is_owner() is False and jload(st_path)["status"] == "paused"
          and not os.path.exists(os.path.join(sr.folder, CDR.REGEN_LOCK_NAME)), op[-200:])
    # paused、其他程序持有權限 → 拒絕
    child_code = (
        "import sys,time;sys.path.insert(0,sys.argv[1]);import report_monitor as RM;"
        "RM._report_output_root=lambda:sys.argv[2];ok,w=RM.acquire_ownership(role='child');"
        "print('READY' if ok else 'FAIL:'+str(w),flush=True);sys.stdin.readline();RM.release_ownership()")
    cp = subprocess.Popen([sys.executable, "-c", child_code, HERE, RG], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True)
    line = ""
    for _ in range(50):                                   # 子程序 import 時會先印模組標頭 → 讀到 READY／FAIL 為止
        line = cp.stdout.readline().strip()
        if line in ("READY",) or line.startswith("FAIL:") or not line and cp.poll() is not None:
            break
    h31b = sha(st_path)
    _r, oc = capture(RM.regenerate_report_guarded, sr.folder)
    cp.stdin.write("\n")
    cp.stdin.flush()
    cp.wait(30)
    check("T31b", f"paused Session、另一程序持有錄製權限（{line}）→ 受保護入口拒絕補產（跨程序互斥）、狀態檔不變",
          line == "READY" and _r is False and "無法取得錄製權限" in oc and sha(st_path) == h31b
          and RM.is_owner() is False, oc[-200:])

    # 第二層比對：補產期間狀態檔被改變 → 不同步 output_status
    def mutate_wx(self, stats):
        s_ = jload(st_path)
        s_["sample_count"] = 999
        CDR.write_json_atomic(st_path, s_)
        return orig_wx(self, stats)
    CDR.ReportSession._write_xlsx = mutate_wx
    _r, om = capture(RM.regenerate_report_guarded, sr.folder)
    CDR.ReportSession._write_xlsx = orig_wx
    check("T31c", "補產期間狀態檔被其他寫入者改變 → 第二層比對偵測、不同步 output_status（不覆蓋新狀態）",
          "已改變" in om and jload(st_path)["sample_count"] == 999)
    # _sync_output_status 寫入失敗 → False、原檔保留
    hs = sha(st_path)
    CDR._state_replace = always_32
    r11b, _o = capture(CDR._sync_output_status, sr.folder, {"xlsx": "ok"})
    CDR._state_replace = real_replace
    check("T11b", "_sync_output_status 寫入失敗 → 回傳 False、原狀態檔 SHA 不變", r11b is False and sha(st_path) == hs)

    # T48：補產失敗（例外／回傳失敗）後正確釋放本次取得的權限與補產鎖
    def boom_wx(self, stats):
        raise RuntimeError("injected regen failure")
    CDR.ReportSession._write_xlsx = boom_wx
    try:
        capture(RM.regenerate_report_guarded, sr.folder)
        raised48 = False
    except RuntimeError:
        raised48 = True
    CDR.ReportSession._write_xlsx = orig_wx
    own48a = RM.is_owner()
    lock48a = os.path.exists(os.path.join(sr.folder, CDR.REGEN_LOCK_NAME))
    sm_path = os.path.join(sr.folder, CFG.FILE_SUMMARY)
    os.rename(sm_path, sm_path + ".bak")
    _r48, o48 = capture(RM.regenerate_report_guarded, sr.folder)
    os.rename(sm_path + ".bak", sm_path)
    got48, why48 = RM.acquire_ownership(role="t48")
    RM.release_ownership()
    check("T48", "補產失敗（例外中斷、回傳失敗）後：錄製權限已釋放（本程序非 Owner、可再取得）、補產鎖已釋放",
          raised48 and own48a is False and not lock48a and _r48 is False and RM.is_owner() is False
          and got48 is True and why48 == "acquired")
    # T49：受保護入口對 recording／狀態無法確認／不在保護範圍的資料夾一律拒絕，不修改任何資料
    sr49 = new_session(RG)
    samples(sr49, 1)
    h49 = {n: sha(os.path.join(sr49.folder, n)) for n in os.listdir(sr49.folder)}
    hu49 = {n: sha(os.path.join(su, n)) for n in os.listdir(su)}
    _a, oa = capture(RM.regenerate_report_guarded, sr49.folder)
    _b, ob = capture(RM.regenerate_report_guarded, su)
    out_root = os.path.join(TMP, "elsewhere")
    os.makedirs(out_root)
    _c, oc49 = capture(RM.regenerate_report_guarded, os.path.join(out_root, "x"))
    check("T49", "受保護入口：recording 拒絕、狀態無法確認拒絕、不在錄製權限保護目錄下拒絕；資料零異動、權限已釋放",
          _a is False and "錄製中" in oa and _b is False and "無法確認" in ob and _c is False and "不在錄製權限" in oc49
          and {n: sha(os.path.join(sr49.folder, n)) for n in os.listdir(sr49.folder)} == h49
          and {n: sha(os.path.join(su, n)) for n in os.listdir(su)} == hu49 and RM.is_owner() is False)

    # ---- 命令列入口（子程序；HTTP／登入／子程序／背景執行緒一律攔截並計數，零網路）----
    CLI_WRAP = (
        "import sys,os,json,threading,subprocess,runpy\n"
        "here,script=sys.argv[1],sys.argv[2];args=sys.argv[3:];sys.path.insert(0,here)\n"
        "rec={'threads':0,'popen':0,'popen_cmds':[],'login':0,'http':[]}\n"
        "_ts=threading.Thread.start\n"
        "def _start(self,*a,**k):\n    rec['threads']+=1;return _ts(self,*a,**k)\n"
        "threading.Thread.start=_start\n"
        "def _pi(self,args,*a,**k):\n"
        "    rec['popen']+=1\n"
        "    rec['popen_cmds'].append([str(x) for x in args][:3] if isinstance(args,(list,tuple)) else [str(args)])\n"
        "    raise RuntimeError('blocked')\n"
        "subprocess.Popen.__init__=_pi\n"
        "import requests,api_client\n"
        "def _req(self,method,url,*a,**k):\n    rec['http'].append(str(url));raise RuntimeError('offline-test')\n"
        "requests.Session.request=_req\n"
        "def _login(self,*a,**k):\n    rec['login']+=1;return None\n"
        "api_client.ApiClient.login_hmi=_login\n"
        "code=0;sys.argv=[script]+args\n"
        "try:\n    runpy.run_path(os.path.join(here,script),run_name='__main__')\n"
        "except SystemExit as e:\n    code=e.code if isinstance(e.code,int) else (0 if e.code is None else 1)\n"
        "rec['mods']=sorted(m for m in ('device_control_menu','auto_monitor_service','device_control_operator','cert_service','pcs_auto_control_service','phase6_unattended_service') if m in sys.modules)\n"
        "rec['code']=code\n"
        "print('CLIREC '+json.dumps(rec),flush=True)\n")

    def cli_result(pr):
        out, _e = pr.communicate(timeout=180)
        rec = None
        for ln in out.splitlines():
            if ln.startswith("CLIREC "):
                rec = json.loads(ln[7:])
        return rec or {"code": None}, out

    def run_cli(script, *args, wait=True):
        pr = subprocess.Popen([sys.executable, "-c", CLI_WRAP, HERE, script] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace")
        return cli_result(pr) if wait else pr

    def no_side_effects(rec):
        """背景執行緒／登入皆 0、未載入選單或服務模組；子程序只允許既有版本橫幅的 git 版本查詢（已被攔截）；
        HTTP 只允許既有補產的告警中文補譯（guest、免登入，已被攔截）。"""
        return (rec.get("threads") == 0 and rec.get("login") == 0
                and all(c[:1] == ["git"] for c in rec.get("popen_cmds", ["?"]))
                and rec.get("mods") == [] and all(CDR.EP_ALARM in u for u in rec.get("http", [])))

    # T43／T50：新 CLI 正常補產 paused（子程序自行取得並釋放權限），且不啟動監控或設備控制
    st43 = jload(st_path)
    st43.pop("output_status", None)
    rec43, out43 = run_cli("report_monitor.py", "--regen", sr.folder)
    st43b = jload(st_path)
    st43b.pop("output_status", None)
    got43, _w = RM.acquire_ownership(role="t43")
    RM.release_ownership()
    check("T43", "新 CLI（report_monitor.py --regen）補產 paused：結束碼 0、report.xlsx 已重產、狀態欄位（除 output_status）"
                 "不變仍為 paused、結束後權限已釋放、無殘留鎖檔",
          rec43.get("code") == 0 and "已用最新 _write_xlsx 重新產生" in out43 and st43b == st43
          and st43b["status"] == "paused" and got43 is True
          and not os.path.exists(os.path.join(sr.folder, CDR.REGEN_LOCK_NAME)), out43[-300:])
    check("T50", f"新 CLI 不啟動背景監控或設備控制：背景執行緒 {rec43.get('threads')}、登入 {rec43.get('login')}、"
                 f"子程序僅 git 版本查詢 {rec43.get('popen_cmds')}（已攔截）、未載入選單／服務／控制模組、"
                 f"HTTP 僅既有補產的告警中文補譯（已攔截）",
          no_side_effects(rec43), json.dumps(rec43, ensure_ascii=False))
    # T44：本程序（模擬服務）持有權限 → 新 CLI 拒絕（跨程序）
    RM.acquire_ownership(role="svc-sim")
    h44 = {n: sha(os.path.join(sr.folder, n)) for n in os.listdir(sr.folder)}
    rec44, out44 = run_cli("report_monitor.py", "--regen", sr.folder)
    h44b = {n: sha(os.path.join(sr.folder, n)) for n in os.listdir(sr.folder)}
    still44 = RM.is_owner()
    RM.release_ownership()
    check("T44", "服務（其他程序）持有錄製權限時，新 CLI 拒絕跨程序補產：結束碼 1、資料夾檔案零異動、服務權限不受影響",
          rec44.get("code") == 1 and "無法取得錄製權限" in out44 and h44b == h44 and still44 is True
          and no_side_effects(rec44), out44[-300:])
    # T46：新 CLI 對 recording 拒絕
    rec46, out46 = run_cli("report_monitor.py", "--regen", sr49.folder)
    check("T46", "新 CLI 對錄製中（recording）Session 拒絕補產：結束碼 1、資料零異動",
          rec46.get("code") == 1 and "錄製中" in out46
          and {n: sha(os.path.join(sr49.folder, n)) for n in os.listdir(sr49.folder)} == h49, out46[-300:])
    # T45：原 CLI（charge_discharge_report.py --regen）相容性：completed 照舊補產；paused 拒絕並提示
    rec45a, out45a = run_cli("charge_discharge_report.py", "--regen", sg.folder)
    h45p = {n: sha(os.path.join(sr.folder, n)) for n in os.listdir(sr.folder)}
    rec45b, out45b = run_cli("charge_discharge_report.py", "--regen", sr.folder)
    check("T45", "原 CLI：completed 照舊補產成功（status 維持 completed）；paused 拒絕並提示 "
                 "python test/report_monitor.py --regen \"<Session資料夾>\"、資料零異動；皆無登入／控制",
          rec45a.get("code") == 0 and "已用最新 _write_xlsx 重新產生" in out45a
          and jload(os.path.join(sg.folder, CFG.FILE_SESSION_STATE))["status"] == "completed"
          and "python test/report_monitor.py --regen \"<Session資料夾>\"" in out45b and "已用最新" not in out45b
          and {n: sha(os.path.join(sr.folder, n)) for n in os.listdir(sr.folder)} == h45p
          and no_side_effects(rec45a) and no_side_effects(rec45b), (out45a[-200:] + out45b[-200:]))
    # T47：多程序補產一致性（completed 亦不得假設沒有其他 Writer）
    lf47, _w = CDR._regen_folder_lock(sg.folder)
    rec47a, out47a = run_cli("charge_discharge_report.py", "--regen", sg.folder)
    CDR._regen_folder_unlock(lf47, sg.folder)
    procs = [run_cli("charge_discharge_report.py", "--regen", sg.folder, wait=False) for _ in range(2)]
    res47 = [cli_result(p_) for p_ in procs]
    ok47 = [("已用最新 _write_xlsx 重新產生" in o) for _r_, o in res47]
    busy47 = [("其他補產程序正在處理同一 Session" in o) for _r_, o in res47]
    try:
        import openpyxl as _ox
        _ox.load_workbook(os.path.join(sg.folder, CFG.FILE_XLSX), read_only=True).close()
        xl_ok47 = True
    except Exception:
        xl_ok47 = False
    k47 = CDR.read_session_state(os.path.join(sg.folder, CFG.FILE_SESSION_STATE))
    check("T47", f"多程序同時補產同一 completed Session：補產鎖持有中 → 拒絕；兩程序同時執行 → 成功 {sum(ok47)}、"
                 f"互斥拒絕 {sum(busy47)}（每個程序非成功即互斥拒絕）；report.xlsx 可開啟、狀態檔完整且為 completed、"
                 "無暫存檔或鎖檔殘留",
          "其他補產程序正在處理同一 Session" in out47a and "已用最新" not in out47a and sum(ok47) >= 1
          and all(a or b for a, b in zip(ok47, busy47)) and xl_ok47 and k47[0] == "OK"
          and k47[1]["status"] == "completed" and not tmp_leftovers(sg.folder)
          and not os.path.exists(os.path.join(sg.folder, CDR.REGEN_LOCK_NAME)),
          str([o[-150:] for _r_, o in res47]))

    # ===================================================================== M-6 收尾第①階段有限等待
    print("\n== M-6 收尾第①階段 _CLIENT_LOCK 有限等待／結束狀態未取得 ==")
    R40 = os.path.join(TMP, "r40")
    os.makedirs(R40)
    RM._report_output_root = lambda: R40
    reset_report()
    s40 = new_session(R40)
    samples(s40, 3)
    RM._report.update(session=s40, thread=None, stop=threading.Event(), finalized=False)
    f40 = {n: os.path.join(s40.folder, n) for n in (CFG.FILE_SAMPLES, CFG.FILE_EVENTS, CFG.FILE_SESSION_STATE,
                                                    CFG.FILE_SUMMARY)}
    h40s = sha(f40[CFG.FILE_SAMPLES])
    n40 = (s40.sample_index, len(s40.samples), round(s40.energy.charged_kwh, 6))
    snap_calls = []
    orig_snap = CDR.ReportSession._capture_cell_snapshot

    def spy_snap(self, *a, **k):
        snap_calls.append(a[-1] if a else k)
        return orig_snap(self, *a, **k)
    CDR.ReportSession._capture_cell_snapshot = spy_snap
    rd40 = {"n": 0}
    prev_read = CDR.read_all

    def count_read(c):
        rd40["n"] += 1
        return prev_read(c)
    CDR.read_all = count_read
    rel40 = threading.Event()
    held40 = threading.Event()

    def holder40():                                        # 模擬卡住且不返回的 HMI 讀取：持續占用 _CLIENT_LOCK
        with RM._CLIENT_LOCK:
            held40.set()
            rel40.wait(300)
    th40 = threading.Thread(target=holder40, daemon=True)
    th40.start()
    held40.wait(5)
    t0 = time.perf_counter()
    (s40r, st40), o40 = capture(RM._stop_and_finalize, "manual_stop")
    el40 = time.perf_counter() - t0
    still_held40 = th40.is_alive()
    rel40.set()
    th40.join(10)
    CDR.read_all = prev_read
    CDR.ReportSession._capture_cell_snapshot = orig_snap
    e40 = ev_types(f40[CFG.FILE_EVENTS])
    sm40 = jload(f40[CFG.FILE_SUMMARY])
    k40 = CDR.read_session_state(f40[CFG.FILE_SESSION_STATE])
    end_rows = [ln for ln in open(f40[CFG.FILE_EVENTS], encoding="utf-8-sig").read().splitlines() if ",session_end," in ln]
    FIN_LOCK_SPEC_SEC = 10                                 # 裁定值：收尾第①階段等待 10 秒（以固定值判定，不隨常數放寬）
    check("T40e", f"收尾第①階段等鎖上限＝裁定值 {FIN_LOCK_SPEC_SEC}s（實際 {CDR.FINALIZE_CLIENT_LOCK_TIMEOUT_SEC}）",
          CDR.FINALIZE_CLIENT_LOCK_TIMEOUT_SEC == FIN_LOCK_SPEC_SEC)
    check("T40", f"收尾第①階段 _CLIENT_LOCK 被占住（未釋放）：最後一筆等 {CFG.SAMPLE_INTERVAL_SEC}s 略過、"
                 f"第①階段等 {FIN_LOCK_SPEC_SEC}s 後不再等待（共 {el40:.1f}s，持鎖者仍未返回）、收尾完成",
          still_held40 and el40 < CFG.SAMPLE_INTERVAL_SEC + FIN_LOCK_SPEC_SEC + 15
          and el40 >= CFG.SAMPLE_INTERVAL_SEC + FIN_LOCK_SPEC_SEC - 0.5
          and st40 is not None and k40[0] == "OK" and k40[1]["status"] == "completed", o40[-300:])
    check("T40b", "結束狀態未取得時不捏造：未做結束 HMI 讀取與 session_end Cell 快照、end_state＝null、"
                  "統計結束 SOC＝None（不以最後一筆取樣冒充）、事件記 final_state_unavailable 與單一 session_end"
                  "（end_state=unavailable）",
          rd40["n"] == 0 and "session_end" not in snap_calls and sm40.get("end_state") is None
          and (sm40.get("statistics") or {}).get("end_soc_percent") is None
          and e40.count("final_state_unavailable") == 1 and e40.count("session_end") == 1
          and "end_state=unavailable" in (end_rows[0] if end_rows else ""), str(e40))
    check("T40c", "未取得結束狀態不回報為成功：finalize_partial（無 finalize_ok）、summary data_complete＝False 且 "
                  "warnings 標示 end_state_unavailable、主控台明確提示；output_status 僅反映檔案輸出",
          e40.count("finalize_partial") == 1 and "finalize_ok" not in e40
          and (sm40.get("validation") or {}).get("data_complete") is False
          and any("end_state_unavailable" in w for w in (sm40.get("validation") or {}).get("warnings", []))
          and "結束狀態未取得" in o40 and s40.end_state_unavailable
          and all(v in (CFG.OUTPUT_OK, CFG.OUTPUT_SKIPPED) for v in (k40[1].get("output_status") or {}).values()))
    check("T40d", "有效原始資料完整保留：samples.csv SHA、取樣序號、筆數、累積電量於收尾前後不變",
          sha(f40[CFG.FILE_SAMPLES]) == h40s
          and (s40.sample_index, len(s40.samples), round(s40.energy.charged_kwh, 6)) == n40)

    # ===================================================================== 收尾失敗 → 合法重試的事件一致性
    print("\n== 收尾失敗後合法重試的事件一致性 ==")
    RR = os.path.join(TMP, "rr")
    os.makedirs(RR)

    def fin_counts(sess):
        e = ev_types(os.path.join(sess.folder, CFG.FILE_EVENTS))
        return e.count("session_end"), e.count("finalize_ok") + e.count("finalize_partial")

    def energy_of(sess):
        return (sess.sample_index, len(sess.samples), round(sess.energy.charged_kwh, 6),
                round(sess.energy.discharged_kwh, 6))

    def st_of(sess):
        return jload(os.path.join(sess.folder, CFG.FILE_SESSION_STATE))["status"]

    # T41a：首次失敗於 session_end 寫入之前（結束 HMI 讀取例外）
    s41a = new_session(os.path.join(RR, "a"))
    samples(s41a, 3)
    en41a = energy_of(s41a)

    def raise_read(_c):
        raise RuntimeError("injected HMI failure")
    CDR.read_all = raise_read
    try:
        capture(s41a.finalize, "manual_stop")
        r41a1 = "returned"
    except RuntimeError:
        r41a1 = "raised"
    CDR.read_all = fake_read_all
    c41a1, st41a1, k41a = fin_counts(s41a), st_of(s41a), CDR.classify_session_folder(s41a.folder)[0]
    r41a2, _o = capture(s41a.finalize, "manual_stop")
    check("T41a", f"首次失敗於 session_end 之前：拋出例外、無 session_end／finalize_*、狀態 {st41a1}、分類 {k41a}（可續接）；"
                  "合法重試完成：session_end 與 finalize_* 各一筆、累積電量與序號不變、completed",
          r41a1 == "raised" and c41a1 == (0, 0) and st41a1 == "recording" and k41a == "resumable"
          and r41a2 is not None and fin_counts(s41a) == (1, 1) and energy_of(s41a) == en41a
          and st_of(s41a) == "completed")
    # T41b：首次失敗於 session_end 寫入之後（輸出階段例外）
    s41b = new_session(os.path.join(RR, "b"))
    samples(s41b, 3)
    en41b = energy_of(s41b)
    orig_wo = s41b._write_outputs

    def boom2():
        raise RuntimeError("injected output failure")
    s41b._write_outputs = boom2
    try:
        capture(s41b.finalize, "manual_stop")
        r41b1 = "returned"
    except RuntimeError:
        r41b1 = "raised"
    s41b._write_outputs = orig_wo
    c41b1, st41b1 = fin_counts(s41b), st_of(s41b)
    k41b = CDR.classify_session_folder(s41b.folder)
    hst41b = sha(os.path.join(s41b.folder, CFG.FILE_SESSION_STATE))
    act41b, unk41b, _w = CDR.find_active_session(os.path.join(RR, "b"), checked=True)
    scan_ok41b = sha(os.path.join(s41b.folder, CFG.FILE_SESSION_STATE)) == hst41b
    rd41b = {"n": 0}

    def count_read2(c):
        rd41b["n"] += 1
        return fake_read_all(c)
    CDR.read_all = count_read2
    r41b2, _o = capture(s41b.finalize, "manual_stop")
    CDR.read_all = fake_read_all
    check("T41b", f"首次失敗於 session_end 之後：狀態 {st41b1}、分類 {k41b[0]}（收尾中斷 → 人工確認，掃描不續接、不改寫）；"
                  "合法重試不重讀 HMI、不重寫 session_end：session_end 與 finalize_* 各一筆、累積電量不變、completed",
          r41b1 == "raised" and c41b1 == (1, 0) and st41b1 == "recording" and k41b[0] == "unknown"
          and act41b is None and len(unk41b) == 1 and scan_ok41b
          and r41b2 is not None and rd41b["n"] == 0 and fin_counts(s41b) == (1, 1)
          and energy_of(s41b) == en41b and st_of(s41b) == "completed")
    # T41c：首次失敗於 finalize_* 寫入之後（標記 completed 時例外）
    s41c = new_session(os.path.join(RR, "c"))
    samples(s41c, 3)
    en41c = energy_of(s41c)
    orig_ps = s41c._persist_state
    n_ps = {"n": 0}

    def ps_raise_once(*a, **k):
        if s41c.status == "completed" and n_ps["n"] == 0:
            n_ps["n"] += 1
            raise RuntimeError("injected persist exception")
        return orig_ps(*a, **k)
    s41c._persist_state = ps_raise_once
    try:
        capture(s41c.finalize, "manual_stop")
        r41c1 = "returned"
    except RuntimeError:
        r41c1 = "raised"
    c41c1, st41c1 = fin_counts(s41c), st_of(s41c)
    k41c = CDR.classify_session_folder(s41c.folder)[0]
    hx41c = sha(os.path.join(s41c.folder, CFG.FILE_XLSX))
    r41c2, _o = capture(s41c.finalize, "manual_stop")
    r41c3, o41c3 = capture(s41c.finalize, "manual_stop")
    s41c._persist_state = orig_ps
    check("T41c", f"首次失敗於 finalize_* 之後：狀態檔 {st41c1}、分類 {k41c}（已收尾、不續接）；合法重試只補標記 completed："
                  "不重產輸出（report.xlsx SHA 不變）、session_end 與 finalize_* 各一筆、累積電量不變；完成後再收尾被拒絕",
          r41c1 == "raised" and c41c1 == (1, 1) and st41c1 == "recording" and k41c in ("finalized_ok", "finalized_partial")
          and r41c2 is not None and sha(os.path.join(s41c.folder, CFG.FILE_XLSX)) == hx41c
          and fin_counts(s41c) == (1, 1) and energy_of(s41c) == en41c and st_of(s41c) == "completed"
          and r41c3 is None and "finalize 拒絕" in o41c3)
    # T41d：completed 狀態寫入失敗（原子寫入回報失敗）
    s41d = new_session(os.path.join(RR, "d"))
    samples(s41d, 3)
    en41d = energy_of(s41d)
    orig_wj = CDR.write_json_atomic

    def fail_completed2(path, obj, **kw):
        if obj.get("status") == "completed":
            return False, "PermissionError: injected"
        return orig_wj(path, obj, **kw)
    CDR.write_json_atomic = fail_completed2
    r41d1, o41d1 = capture(s41d.finalize, "manual_stop")
    CDR.write_json_atomic = orig_wj
    c41d1, st41d1 = fin_counts(s41d), st_of(s41d)
    k41d = CDR.classify_session_folder(s41d.folder)[0]
    r41d2, _o = capture(s41d.finalize, "manual_stop")
    r41d3, o41d3 = capture(s41d.finalize, "manual_stop")
    check("T41d", f"completed 寫入失敗：明確警告未標記 completed、狀態檔 {st41d1}、分類 {k41d}（不續接、不誤判完成）；"
                  "合法重試只補寫 completed：無重複 session_end／finalize_*、累積電量不變；寫入成功後再收尾被拒絕",
          r41d1 is not None and "未能標記 completed" in o41d1 and c41d1 == (1, 1) and st41d1 == "recording"
          and k41d in ("finalized_ok", "finalized_partial") and r41d2 is not None and fin_counts(s41d) == (1, 1)
          and energy_of(s41d) == en41d and st_of(s41d) == "completed" and r41d3 is None and "finalize 拒絕" in o41d3)

    # ===================================================================== 正常流程（RM 整合）
    print("\n== T38 正常開始／取樣／暫停／續接／完成（report_monitor 整合）==")
    RN = os.path.join(TMP, "rn")
    os.makedirs(RN)
    RM._report_output_root = lambda: RN
    RM.acquire_ownership(role="ssa-test")
    reset_report()
    _r, o1 = capture(RM.report_start)
    sn = RM._report["session"]
    time.sleep(0.5)
    _r, o2 = capture(RM.report_pause)
    st_p = jload(os.path.join(sn.folder, CFG.FILE_SESSION_STATE))
    _r, o3 = capture(RM.report_start)                     # 續接同一資料夾
    sn2 = RM._report["session"]
    time.sleep(0.5)
    (s_f, stats_f), o4 = capture(RM._stop_and_finalize, "manual_stop")
    st_f = jload(os.path.join(sn.folder, CFG.FILE_SESSION_STATE))
    evn = open(os.path.join(sn.folder, CFG.FILE_EVENTS), encoding="utf-8-sig").read()
    check("T38", "正常流程不受影響：開始→暫停（paused、印已暫停）→續接同一資料夾（recording_resume）→收尾 completed；"
                 "無逾時、無略過最後一筆、只有一個資料夾",
          sn is not None and st_p["status"] == "paused" and "已暫停（下次可續接）" in o2 and sn2.folder == sn.folder
          and "recording_resume" in evn and st_f["status"] == "completed" and stats_f is not None
          and "bg_thread_join_timeout" not in evn and "final_sample_skipped" not in evn
          and len([x for x in os.listdir(RN) if os.path.isdir(os.path.join(RN, x))]) == 1,
          (o1 + o2 + o3 + o4)[-400:])
    RM.release_ownership()
finally:
    try:
        READ["gate"].set()
        RM.release_ownership()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)

n_pass = sum(_results)
print(f"\n== test_session_state_atomic：{n_pass}/{len(_results)} PASS ==")
print(f"結果：{n_pass} / {len(_results)} PASS（實際檢查 {len(_results)} 項；回歸工具以此行計數）")
sys.exit(0 if n_pass == len(_results) else 1)
