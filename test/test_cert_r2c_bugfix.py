# -*- coding: utf-8 -*-
"""
test_cert_r2c_bugfix.py — 認證 BUG-1（W2b 方案 A 問題 2）／BUG-2（W-G1c）最小修正 離線測試（2026-11-16）
=====================================================================================================
全部離線：假 HMI reader、注入時鐘、暫存資料夾；不連任何設備、不送任何控制、不截圖（enable_shots=False）。

  BUG-1  T1-1 其他認證 cert_state 讀不到 → 不產報、不寫入　T1-2 內容無效（壞 JSON／list／null／cert_id 不符／狀態不明）
         T1-3 其他認證正常重產　T1-4 目前認證結束時讀磁碟失敗 → 用記憶體狀態　T1-5 產報例外分支
  BUG-2  T2-1 收尾失敗（分析／cert_state 寫入／end_begin 寫入）→ HOLD、停止取樣　T2-2 ended 落盤後才失敗 → 不 HOLD
         T2-3 HOLD 時開始／重產被拒　T2-4 同程序重新結束　T2-5 重試又失敗　T2-6 處理結果寫不出去 → 只補寫結果
         T2-7 active.json 清除失敗／指向其他認證
  重啟   R-1 failed 結果 → HOLD　R-2 pending／expired／done 矛盾／只歸檔／end_failed 事件／帶 hold 標記的 rejected
         R-3 讀不到／缺 cert_id／created_at 錯誤／started_at 錯誤 → HOLD　R-4 無證據 → 照常續接
         R-5 重啟前結束請求分類（M7b）　R-6 重啟後人工結束（M9）　R-7 反覆重啟不重複事件　R-8 誤判 HOLD 排除後續接
         R-9 資料截止時間取自 observations 完整行　R-10 observations 讀不到 → null
  相容   N-1 正常結束：cert_state／events（只多 end_begin）／報告逐格與修正前相同（--dump／--compare）

用法：
  python test_cert_r2c_bugfix.py [--code <受測 test 目錄>] [--dump <資料夾>] [--compare <資料夾>]
    --code     受測程式目錄（重現修正前的失敗）
    --dump     把 N-1 正常結束的 cert_state／events／報告儲存格寫到資料夾（以修正前版本執行，作為對照）
    --compare  與 --dump 的對照逐項比對（修正後版本執行）
注意：A／B 類（離線／合成）證據，不代表實機驗證。
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

ap = argparse.ArgumentParser()
ap.add_argument("--code", default=os.path.dirname(os.path.abspath(__file__)))
ap.add_argument("--dump")
ap.add_argument("--compare")
ARGS = ap.parse_args()
sys.path.insert(0, os.path.abspath(ARGS.code))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                       # noqa: BLE001
    pass

import cert_report_xlsx as RX                           # noqa: E402
import cert_requests as REQ                             # noqa: E402
import cert_runtime as CR                               # noqa: E402

print(f"受測程式：{os.path.abspath(ARGS.code)}")
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"（{detail}）" if detail else ""))


def guard(label, fn):
    try:
        fn()
    except Exception as e:                              # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{label}：執行例外 {type(e).__name__}: {str(e)[:160]}", False)


TMP = tempfile.mkdtemp(prefix="cert_bugfix_")
KEYS = {"soc": "soc_percent", "v": "battery_voltage_v", "i": "battery_current_a", "p": "actual_active_power_kw",
        "rmax": "rack_max_temperature_c", "rmin": "rack_min_temperature_c",
        "cmax": "rack_cell_max_voltage_v", "cmin": "rack_cell_min_voltage_v"}
GOOD = dict(soc=50.0, v=950.0, i=0.0, p=0.0, rmax=30.0, rmin=25.0, cmax=3.40, cmin=3.30)
T0 = datetime(2026, 11, 2, 10, 0, 0)


def reading(**kw):
    vals = dict(GOOD)
    vals.update(kw)
    r = {KEYS[k]: v for k, v in vals.items()}
    cur = vals.get("i") or 0.0
    r.update({"_fail": [], "communication_ok": True, "raw_source_time": "", "pcs_charging_flag": cur > 1.0,
              "pcs_discharging_flag": cur < -1.0, "pcs_fault_flag": False, "battery_power_status": "已上電"})
    return r


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


class Reader:
    """假 HMI：mode＝idle／charge／discharge／fail；calls＝被呼叫次數（驗證重新結束不讀 HMI）。"""
    def __init__(self):
        self.mode, self.calls = "idle", 0

    def __call__(self):
        self.calls += 1
        if self.mode == "fail":
            return None
        if self.mode == "charge":
            return reading(i=140.0, p=114.0)
        if self.mode == "discharge":
            return reading(i=-140.0, p=-114.0)
        return reading()


class Env:
    def __init__(self, name):
        self.root = os.path.join(TMP, name)
        os.makedirs(self.root)
        self.clk = Clock()
        self.rd = Reader()

    def runtime(self):
        return CR.CertRuntime(self.root, reader=self.rd, enable_shots=False, clock=self.clk, log=lambda *a: None)

    def start(self, rt):
        rid = REQ.new_request(self.root, "start_cert", {"cabinet_spec": "250KW", "cycles": 1, "conditions": "測試"},
                              now=self.clk())
        rt.process_requests()
        return REQ.read_result(self.root, rid)["cert_id"]

    def sample(self, rt, n):
        for _ in range(n):
            self.clk.advance(1)
            rt.maybe_sample()

    def request(self, rt, rtype, cid, payload=None, process=True):
        self.clk.advance(1)
        rid = REQ.new_request(self.root, rtype, payload, cert_id=cid, now=self.clk())
        if process:
            rt.process_requests()
        return rid, REQ.read_result(self.root, rid)

    def p(self, cid, *parts):
        return os.path.join(self.root, cid, *parts)

    def state(self, cid):
        return REQ.read_json(self.p(cid, "cert_state.json"))

    def events(self, cid):
        return CR._read_jsonl(self.p(cid, "events.jsonl"))

    def obs(self, cid):
        return CR._read_jsonl(self.p(cid, "observations.jsonl"))

    def reports(self, cid):
        d = self.p(cid, "reports")
        return sorted(f for f in os.listdir(d) if f.endswith(".xlsx")) if os.path.isdir(d) else []


def sha(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def halt_of(rt):
    return getattr(rt, "_halt", None)


class Patch:
    """暫時替換模組屬性（離開時還原）。"""
    def __init__(self, obj, name, fn):
        self.obj, self.name, self.fn = obj, name, fn

    def __enter__(self):
        self.orig = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.fn(self.orig))
        return self

    def __exit__(self, *a):
        setattr(self.obj, self.name, self.orig)


def fail_read_for(path):
    """模擬 cert_state 讀不到（共用衝突等 OSError → read_json 回 None）。"""
    norm = os.path.normcase(os.path.abspath(path))
    return Patch(REQ, "read_json",
                 lambda orig: (lambda p: None if os.path.normcase(os.path.abspath(p)) == norm else orig(p)))


def fail_state_write(times=1, only_ended=True):
    box = {"n": times}

    def mk(orig):
        def f(path, data):
            if (path.endswith("cert_state.json") and box["n"] > 0
                    and (not only_ended or (isinstance(data, dict) and data.get("status") == "ended"))):
                box["n"] -= 1
                raise PermissionError(13, "模擬：cert_state.json 寫入失敗")
            return orig(path, data)
        return f
    return Patch(REQ, "atomic_write_json", mk)


def fail_event(kind, times=1):
    box = {"n": times}

    def mk(orig):
        def f(path, rec):
            if path.endswith("events.jsonl") and isinstance(rec, dict) and rec.get("type") == kind and box["n"] > 0:
                box["n"] -= 1
                raise OSError(28, f"模擬：events.jsonl 寫入失敗（{kind}）")
            return orig(path, rec)
        return f
    return Patch(CR, "_append_jsonl", mk)


def ended_ok(env, cid, rid):
    """C 節五項一致性：cert_state ended＋end_request、active.json 不指向本認證、end 事件一筆、結果 done。"""
    st = env.state(cid) or {}
    ends = [e for e in env.events(cid) if e.get("type") == "end"]
    res = REQ.read_result(env.root, rid) or {}
    return (st.get("status") == "ended" and st.get("end_request") == rid and REQ.read_active(env.root) != cid
            and len(ends) == 1 and res.get("status") == "done"), \
        f"status={st.get('status')} end_request={st.get('end_request') == rid} active={REQ.read_active(env.root)} " \
        f"end事件={len(ends)} 結果={res.get('status')}"


def no_sampling(env, rt, cid, n=10):
    before = len(env.obs(cid))
    calls = env.rd.calls
    for _ in range(n):
        env.clk.advance(1)
        rt.tick()
    return len(env.obs(cid)) == before and env.rd.calls == calls, f"observations {before}→{len(env.obs(cid))}，HMI 呼叫 +{env.rd.calls - calls}"


def recording_cert(env, n=20, mode="idle"):
    rt = env.runtime()
    cid = env.start(rt)
    env.rd.mode = mode
    env.sample(rt, n)
    return rt, cid


# =============================================================================================== BUG-1
def t1():
    print("== BUG-1：cert_state.json 讀不到／內容無效時不得覆寫 ==")
    env = Env("t1")
    rt, a = recording_cert(env, 30)
    rid, res = env.request(rt, "end_cert", a)
    check("前置：認證 A 正常結束", (res or {}).get("status") == "done", str(res and res.get("message"))[:80])
    b = env.start(rt)
    env.sample(rt, 10)
    path_a = env.p(a, "cert_state.json")
    before, n_rep = sha(path_a), len(env.reports(a))
    with fail_read_for(path_a):
        rid, res = env.request(rt, "regenerate", a)
    check("T1-1 其他認證 cert_state 讀不到 → 重產失敗、cert_state 不變、無新報告、無 .partial",
          (res or {}).get("status") == "failed" and sha(path_a) == before and len(env.reports(a)) == n_rep
          and not [f for f in os.listdir(env.p(a, "reports")) if "partial" in f],
          f"結果 {res and res.get('status')}；cert_state 變更={sha(path_a) != before}；報告 {n_rep}→{len(env.reports(a))}")
    good = open(path_a, "rb").read()
    for label, content in (("壞 JSON", b"{not json"), ("list", b"[]"), ("null", b"null"),
                           ("cert_id 不符", json.dumps({**json.loads(good), "cert_id": "CERT_X"}).encode()),
                           ("狀態不明", json.dumps({**json.loads(good), "status": "weird"}).encode())):
        open(path_a, "wb").write(content)
        before, n_rep = sha(path_a), len(env.reports(a))
        rid, res = env.request(rt, "regenerate", a)
        check(f"T1-2 其他認證 cert_state {label} → 不產報、不寫入",
              (res or {}).get("status") != "done" and sha(path_a) == before and len(env.reports(a)) == n_rep,
              f"結果 {res and res.get('status')}；檔案變更={sha(path_a) != before}")
    open(path_a, "wb").write(good)
    keys = set(json.loads(good))
    rid, res = env.request(rt, "regenerate", a)
    st = env.state(a) or {}
    check("T1-3 其他認證正常 → 產報成功、原有欄位全部保留、reports 加一筆",
          (res or {}).get("status") == "done" and keys <= set(st) and len(st.get("reports") or []) == 2,
          f"結果 {res and res.get('status')}；缺欄位 {sorted(keys - set(st))}")
    # T1-4：目前認證 B 結束時讀磁碟失敗 → 用記憶體狀態（設備仍在充電 → 未完成後綴）
    env.rd.mode = "charge"
    env.sample(rt, 5)
    path_b = env.p(b, "cert_state.json")
    with fail_read_for(path_b):
        rid, res = env.request(rt, "end_cert", b)
    st = env.state(b) or {}
    reps = env.reports(b)
    check("T1-4 目前認證結束時 cert_state 讀不到 → 報告檔名與狀態取自記憶體（_未完成）、cert_state 完整",
          (res or {}).get("status") == "done" and reps and reps[-1].endswith("_未完成.xlsx")
          and st.get("started_at") and st.get("test_info") and st.get("params") and st.get("incomplete")
          and "未完成" in str((res or {}).get("message")),
          f"報告 {reps}；incomplete={st.get('incomplete')}；結果訊息含警告={'未完成' in str((res or {}).get('message'))}")
    # T1-5：產報例外分支
    c = env.start(rt)
    env.rd.mode = "idle"
    env.sample(rt, 5)
    before = sha(path_a)

    def boom(orig):
        return lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模擬：save_report 失敗"))
    with Patch(RX, "save_report", boom), fail_read_for(path_a):
        ok, msg, p = rt.generate_report(a)
    check("T1-5a 其他認證產報例外且 cert_state 讀不到 → 不寫入", not ok and sha(path_a) == before,
          f"ok={ok}；cert_state 變更={sha(path_a) != before}")
    keys_c = set(env.state(c) or {})
    with Patch(RX, "save_report", boom):
        ok, msg, p = rt.generate_report(c)
    st = env.state(c) or {}
    check("T1-5b 目前認證產報例外 → 只合併 last_report_error，其他欄位保留",
          not ok and keys_c <= set(st) and (st.get("last_report_error") or {}).get("error"),
          f"缺欄位 {sorted(keys_c - set(st))}")


# =============================================================================================== BUG-2 同程序
def t2():
    print("== BUG-2：結束認證中途失敗 → HOLD（同一程序）==")
    # T2-1a：分析例外
    env = Env("t2a")
    rt, cid = recording_cert(env, 20)
    orig = rt.analysis
    rt.analysis = lambda: (_ for _ in ()).throw(RuntimeError("模擬：分析例外"))
    rid, res = env.request(rt, "end_cert", cid)
    rt.analysis = orig
    ev = env.events(cid)
    check("T2-1a 收尾分析例外 → 結果 failed、磁碟仍 recording、HOLD、有 end_begin／end_failed",
          (res or {}).get("status") == "failed" and (env.state(cid) or {}).get("status") == "recording"
          and halt_of(rt) is not None and any(e.get("type") == "end_begin" for e in ev)
          and any(e.get("type") == "end_failed" for e in ev),
          f"結果 {res and res.get('status')}；HOLD={halt_of(rt) is not None}")
    ok, d = no_sampling(env, rt, cid)
    check("T2-1a HOLD 期間不取樣（10 秒）", ok, d)
    # T2-1b：cert_state 寫入失敗（＋真實執行緒 2.5 秒不取樣）
    env = Env("t2b")
    rt, cid = recording_cert(env, 20)
    with fail_state_write():
        rid1, res = env.request(rt, "end_cert", cid)
    check("T2-1b cert_state 寫入 ended 失敗 → failed、磁碟仍 recording、HOLD",
          (res or {}).get("status") == "failed" and (env.state(cid) or {}).get("status") == "recording"
          and halt_of(rt) is not None, f"結果 {res and res.get('status')}；HOLD={halt_of(rt) is not None}")
    ok, d = no_sampling(env, rt, cid)
    check("T2-1b HOLD 期間不取樣（tick 10 秒）", ok, d)
    n0, calls = len(env.obs(cid)), env.rd.calls
    stop = threading.Event()
    ths = rt.run(stop)
    time.sleep(2.5)
    stop.set()
    for th in ths:
        th.join(5)
    check("T2-1b HOLD 期間背景執行緒 2.5 秒不取樣、不讀 HMI",
          len(env.obs(cid)) == n0 and env.rd.calls == calls, f"observations {n0}→{len(env.obs(cid))}")
    st_json = REQ.read_json(os.path.join(env.root, "status.json")) or {}
    check("T2-3 status.json 顯示 halted；active＝False", st_json.get("active") is False and "halted" in st_json,
          str(sorted(st_json))[:120])
    rid_s, res_s = env.request(rt, "start_cert", None, {"cabinet_spec": "250KW"})
    n_rep = len(env.reports(cid))
    rid_g, res_g = env.request(rt, "regenerate", cid)
    check("T2-3 HOLD 時開始新認證被拒、重產被拒（不產報）",
          (res_s or {}).get("status") == "rejected" and (res_g or {}).get("status") != "done"
          and len(env.reports(cid)) == n_rep and (env.state(cid) or {}).get("status") == "recording",
          f"開始 {res_s and res_s.get('status')}；重產 {res_g and res_g.get('status')}")
    check("T2-3 磁碟仍為進行中（選單 21 擋下、單次報告擋下的依據）；記憶體 active_id 為 None",
          REQ.active_on_disk(env.root) == cid and rt.active_id() is None)
    # T2-5：重試又失敗
    with fail_state_write():
        rid2, res = env.request(rt, "end_cert", cid)
    h = halt_of(rt) or {}
    check("T2-5 重新結束又失敗 → 仍 HOLD、失敗次數 2、沒有 end 事件、沒有報告",
          (res or {}).get("status") == "failed" and h.get("attempts") == 2
          and not any(e.get("type") == "end" for e in env.events(cid)) and not env.reports(cid),
          f"attempts={h.get('attempts')}")
    # T2-4：同程序重新結束成功
    obs_before = env.obs(cid)
    calls = env.rd.calls
    env.clk.advance(30)
    rid3, res = env.request(rt, "end_cert", cid)
    ok, d = ended_ok(env, cid, rid3)
    check("T2-4 重新結束 → 五項一致", ok, d)
    st = env.state(cid) or {}
    er = st.get("end_recovery") or {}
    check("T2-4 重新結束不讀 HMI、不新增 observations、報告 1 份",
          env.rd.calls == calls and len(env.obs(cid)) == len(obs_before) and len(env.reports(cid)) == 1,
          f"HMI +{env.rd.calls - calls}；報告 {env.reports(cid)}")
    check("T2-4 end_recovery：in_process_retry、資料截止＝observations 最後一筆（≠ 重試時間）、sampling_stopped_at 有值",
          er.get("mode") == "in_process_retry" and er.get("last_recorded_sample_at") == obs_before[-1]["t"]
          and er.get("last_recorded_sample_at") != er.get("completed_at") and er.get("sampling_stopped_at")
          and er.get("first_end_request_id") == rid1 and er.get("failed_attempts") == 2
          and er.get("manual_end_request_id") == rid3 and er.get("final_sample") == "taken",
          json.dumps({k: er.get(k) for k in ("mode", "last_recorded_sample_at", "completed_at", "sampling_stopped_at",
                                            "failed_attempts", "final_sample")}, ensure_ascii=False))
    # T2-1c：end_begin 寫入失敗 → 不取最後一筆、HOLD
    env = Env("t2c")
    rt, cid = recording_cert(env, 20)
    n0, calls = len(env.obs(cid)), env.rd.calls
    with fail_event("end_begin"):
        rid, res = env.request(rt, "end_cert", cid)
    check("T2-1c end_begin 寫入失敗 → 不執行最後一筆取樣、不寫 ended、HOLD、結果 failed",
          (res or {}).get("status") == "failed" and len(env.obs(cid)) == n0 and env.rd.calls == calls
          and (env.state(cid) or {}).get("status") == "recording" and halt_of(rt) is not None,
          f"結果 {res and res.get('status')}；HMI +{env.rd.calls - calls}")
    # T2-2：ended 已落盤後 end 事件失敗 → 不 HOLD；重啟不續接；選單 25 可補產
    env = Env("t2d")
    rt, cid = recording_cert(env, 20)
    with fail_event("end"):
        rid, res = env.request(rt, "end_cert", cid)
    check("T2-2 ended 落盤後 end 事件失敗 → 磁碟 ended、沒有 HOLD",
          (env.state(cid) or {}).get("status") == "ended" and halt_of(rt) is None, f"結果 {res and res.get('status')}")
    rt2 = env.runtime()
    r = rt2.startup()
    rid, res = env.request(rt2, "regenerate", cid)
    check("T2-2 重啟不續接；選單 25 可補產報告",
          r is None and rt2.active_id() is None and (res or {}).get("status") == "done"
          and not any(e.get("type") == "resume" for e in env.events(cid)))
    # T2-6：處理結果寫不出去 → 同程序補寫；重啟後補寫
    for restart in (False, True):
        env = Env(f"t2e{int(restart)}")
        rt, cid = recording_cert(env, 20)
        box = {"n": 2}

        def mk(orig):
            def f(root, req, status, message, cert_id=None, data=None):
                if req.get("type") == "end_cert" and box["n"] > 0:
                    box["n"] -= 1
                    raise PermissionError(13, "模擬：results 寫入失敗")
                return orig(root, req, status, message, cert_id=cert_id, data=data)
            return f
        env.clk.advance(1)
        rid = REQ.new_request(env.root, "end_cert", None, cert_id=cid, now=env.clk())
        with Patch(REQ, "write_result", mk):
            try:
                rt.process_requests()
            except Exception:                           # noqa: BLE001
                pass
        n_rep, n_obs = len(env.reports(cid)), len(env.obs(cid))
        if restart:
            rt = env.runtime()
            rt.startup()
        rt.process_requests()
        res = REQ.read_result(env.root, rid) or {}
        ok, d = ended_ok(env, cid, rid)
        check(f"T2-6 處理結果寫不出去 → {'重啟後' if restart else '同程序'}補寫 done、報告／end 事件／observations 不重複",
              ok and (res.get("data") or {}).get("resent") is True and len(env.reports(cid)) == n_rep
              and len(env.obs(cid)) == n_obs, d + f"；resent={(res.get('data') or {}).get('resent')}")
    # T2-7：active.json 清除失敗；指向其他認證
    env = Env("t2f")
    rt, cid = recording_cert(env, 20)

    def mk_act(orig):
        def f(path, data):
            if path.endswith("active.json") and isinstance(data, dict) and data.get("cert_id") is None:
                raise PermissionError(13, "模擬：active.json 寫入失敗")
            return orig(path, data)
        return f
    with Patch(REQ, "atomic_write_json", mk_act):
        rid, res = env.request(rt, "end_cert", cid)
    rt2 = env.runtime()
    r = rt2.startup()
    rid2, res2 = env.request(rt2, "end_cert", cid)
    ends = [e for e in env.events(cid) if e.get("type") == "end"]
    check("T2-7a active.json 清除失敗 → 磁碟 ended、重啟不續接、再送結束不會產生第二筆 end 事件",
          (env.state(cid) or {}).get("status") == "ended" and r is None and len(ends) == 1
          and REQ.active_on_disk(env.root) is None, f"end 事件 {len(ends)}；第二次結果 {res2 and res2.get('status')}")
    env = Env("t2g")
    rt, cid = recording_cert(env, 20)
    REQ.atomic_write_json(os.path.join(env.root, "active.json"), {"cert_id": "CERT_OTHER", "updated_at": "x"})
    rid, res = env.request(rt, "end_cert", cid)
    check("T2-7b active.json 指向其他認證 → 不清除（記 active_mismatch）",
          REQ.read_active(env.root) == "CERT_OTHER" and (env.state(cid) or {}).get("status") == "ended"
          and any(e.get("type") == "active_mismatch" for e in env.events(cid)), f"active={REQ.read_active(env.root)}")


# =============================================================================================== 重啟
def disk_cert(name, n=20, mode="idle"):
    """建立一份「磁碟上仍 recording」的認證（模擬服務停止，不經結束）。"""
    env = Env(name)
    rt, cid = recording_cert(env, n, mode)
    del rt
    return env, cid


def put_req(env, cid, rtype="end_cert", where="pending", result=None, created=None, rid=None, cert_id="__same__", data=None):
    created = created or env.clk()
    rid = rid or f"REQ_{created:%Y%m%d_%H%M%S}_{os.urandom(4).hex()}"
    req = {"request_id": rid, "type": rtype, "cert_id": cid if cert_id == "__same__" else cert_id, "payload": {},
           "created_at": created.strftime("%Y-%m-%d %H:%M:%S"), "source": "test"}
    REQ.atomic_write_json(os.path.join(env.root, "requests", where, f"{rid}.json"), req)
    if result:
        REQ.atomic_write_json(os.path.join(env.root, "results", f"{rid}.json"),
                              {**req, "status": result, "message": "test", "data": data or {},
                               "processed_at": created.strftime("%Y-%m-%d %H:%M:%S")})
    return rid


def restart(env):
    env.clk.advance(60)
    rt = env.runtime()
    r = rt.startup()
    return rt, r


def held(env, rt, r, cid):
    resumes = [e for e in env.events(cid) if e.get("type") == "resume"]
    st = env.state(cid) or {}
    return (r is None and halt_of(rt) is not None and not rt.is_active() and not resumes
            and not st.get("interruptions")), f"startup={r}；HOLD={halt_of(rt) is not None}；resume 事件 {len(resumes)}"


def t_restart():
    print("== 重啟：結束證據 → HOLD；無證據 → 照常續接 ==")
    env = Env("r1")
    rt, cid = recording_cert(env, 20)
    with fail_state_write():
        rid, res = env.request(rt, "end_cert", cid)
    rt, r = restart(env)
    ok, d = held(env, rt, r, cid)
    check("R-1 failed 結果 → 重啟 HOLD（不續接、無 resume 事件、無中斷紀錄）", ok, d)
    ok, d = no_sampling(env, rt, cid)
    check("R-1 HOLD 不取樣", ok, d)
    cases = [("pending", dict(where="pending")),
             ("expired", dict(where="done", result="expired")),
             ("done 但仍 recording", dict(where="done", result="done")),
             ("只歸檔沒有結果", dict(where="done")),
             ("帶 hold 標記的 rejected", dict(where="done", result="rejected", data={"hold": {"class": "not_started"}}))]
    for i, (label, kw) in enumerate(cases):
        env, cid = disk_cert(f"r2_{i}")
        env.clk.advance(5)
        put_req(env, cid, **kw)
        rt, r = restart(env)
        ok, d = held(env, rt, r, cid)
        check(f"R-2 {label} → HOLD", ok, d)
    env, cid = disk_cert("r2_ev")
    with open(env.p(cid, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": "2026-11-02T10:00:30", "type": "end_failed", "detail": "x", "request_id": "REQ_X"},
                           ensure_ascii=False) + "\n")
    rt, r = restart(env)
    ok, d = held(env, rt, r, cid)
    check("R-2 只有 end_failed 事件（結果檔遺失）→ HOLD", ok, d)
    # R-3
    env, cid = disk_cert("r3a")
    env.clk.advance(5)
    rid = f"REQ_{env.clk():%Y%m%d_%H%M%S}_badjson0"
    os.makedirs(os.path.join(env.root, "results"), exist_ok=True)
    open(os.path.join(env.root, "results", f"{rid}.json"), "w", encoding="utf-8").write("{bad")
    rt, r = restart(env)
    ok, d = held(env, rt, r, cid)
    check("R-3 範圍內請求／結果檔讀不到 → HOLD", ok, d)
    env, cid = disk_cert("r3b")
    env.clk.advance(5)
    put_req(env, cid, cert_id=None)
    rt, r = restart(env)
    ok, d = held(env, rt, r, cid)
    check("R-3 end_cert 缺 cert_id → HOLD", ok, d)
    env, cid = disk_cert("r3c")
    env.clk.advance(5)
    rid = put_req(env, cid)
    p = os.path.join(env.root, "requests", "pending", f"{rid}.json")
    j = REQ.read_json(p)
    j["created_at"] = "bad"
    REQ.atomic_write_json(p, j)
    rt, r = restart(env)
    ok, d = held(env, rt, r, cid)
    check("R-3 created_at 無法判讀 → HOLD", ok, d)
    env, cid = disk_cert("r3d")
    st = env.state(cid)
    st["started_at"] = "2026/11/02 10:00"
    REQ.atomic_write_json(env.p(cid, "cert_state.json"), st)
    rt, r = restart(env)
    ok, d = held(env, rt, r, cid)
    check("R-3 started_at 格式錯誤 → HOLD", ok, d)
    # R-4：無證據 → 照常續接
    for i, (label, kw) in enumerate([("只有 rejected", dict(where="done", result="rejected")),
                                     ("其他認證的結束請求", dict(cert_id="CERT_OTHER")),
                                     ("開始前建立的請求", dict(created=T0 - timedelta(hours=1)))]):
        env, cid = disk_cert(f"r4_{i}")
        env.clk.advance(5)
        put_req(env, cid, **kw)
        rt, r = restart(env)
        st = env.state(cid) or {}
        check(f"R-4 {label} → 照常續接（resume 事件、中斷紀錄、可取樣）",
              r == cid and rt.is_active() and any(e.get("type") == "resume" for e in env.events(cid))
              and len(st.get("interruptions") or []) == 1, f"startup={r}")
    # R-5：重啟前結束請求分類（M7b）
    variants = [("沒有任何證據", "not_started", "rejected", None),
                ("有 end_begin 事件", "started_no_result", "failed", "end_begin"),
                ("只有 cert_end 計時紀錄", "started_no_result", "failed", "timing"),
                ("建立超過 300 秒", "not_started", "rejected", "old")]
    for i, (label, cls, status, ev) in enumerate(variants):
        env, cid = disk_cert(f"r5_{i}", n=(500 if ev == "old" else 20))   # 超過 300 秒的請求仍須晚於認證開始
        env.clk.advance(5)
        created = env.clk() - (timedelta(seconds=400) if ev == "old" else timedelta(0))
        rid = put_req(env, cid, created=created)
        if ev == "end_begin":
            with open(env.p(cid, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": env.clk().strftime("%Y-%m-%dT%H:%M:%S"), "type": "end_begin",
                                    "detail": "x", "request_id": rid}, ensure_ascii=False) + "\n")
        if ev == "timing":
            with open(env.p(cid, CR.TIMING_FILE), "a", encoding="utf-8") as f:
                f.write(json.dumps({"wall_start": (env.clk() + timedelta(seconds=1)).isoformat(timespec="milliseconds"),
                                    "trigger": "cert_end"}) + "\n")
        req_bytes = open(os.path.join(env.root, "requests", "pending", f"{rid}.json"), "rb").read()
        rt, r = restart(env)
        rt.process_requests()
        res = REQ.read_result(env.root, rid) or {}
        hold = (res.get("data") or {}).get("hold") or {}
        moved = os.path.join(env.root, "requests", "done", f"{rid}.json")
        check(f"R-5 重啟前結束請求（{label}）→ {status}＋hold.class={cls}、不執行、原始請求檔不變、有 end_request_held",
              res.get("status") == status and hold.get("class") == cls and os.path.exists(moved)
              and open(moved, "rb").read() == req_bytes and (env.state(cid) or {}).get("status") == "recording"
              and any(e.get("type") == "end_request_held" for e in env.events(cid)),
              f"結果 {res.get('status')}；class={hold.get('class')}")
        rt, r = restart(env)
        ok, d = held(env, rt, r, cid)
        check(f"R-5 再重啟一次（{label}）仍 HOLD（標記不會被誤認為從未嘗試結束）", ok, d)
        if i == 0:
            env0, cid0, rid0, rt0 = env, cid, rid, rt
    # R-6：重啟後人工結束
    env, cid, rt = env0, cid0, rt0
    obs = env.obs(cid)
    calls = env.rd.calls
    env.clk.advance(30)
    rid6, res = env.request(rt, "end_cert", cid)
    ok, d = ended_ok(env, cid, rid6)
    er = (env.state(cid) or {}).get("end_recovery") or {}
    check("R-6 重啟後選單 22 重新結束 → 五項一致、不讀 HMI", ok and env.rd.calls == calls, d)
    check("R-6 end_recovery：after_restart、sampling_stopped_at＝null、final_sample＝unknown、held_requests 含重啟前請求、"
          "資料截止＝observations 最後一筆、incomplete 有延遲收尾說明",
          er.get("mode") == "after_restart" and er.get("sampling_stopped_at") is None
          and er.get("final_sample") == "unknown" and rid0 in [x.get("request_id") for x in er.get("held_requests") or []]
          and er.get("last_recorded_sample_at") == obs[-1]["t"] and "延遲收尾" in str((env.state(cid) or {}).get("incomplete"))
          and env.reports(cid) and env.reports(cid)[-1].endswith("_未完成.xlsx"),
          json.dumps({k: er.get(k) for k in ("mode", "sampling_stopped_at", "final_sample", "last_recorded_sample_at")},
                     ensure_ascii=False))
    # R-7：反覆重啟
    env, cid = disk_cert("r7")
    env.clk.advance(5)
    put_req(env, cid, where="done", result="failed")
    for _ in range(3):
        rt, r = restart(env)
    n = sum(1 for e in env.events(cid) if e.get("type") == "end_recovery_hold")
    check("R-7 反覆重啟 3 次 → end_recovery_hold 只記一次、都不續接", n == 1 and r is None, f"事件 {n}")
    # R-8：誤判 HOLD（無關檔案暫時讀不到）→ 排除後重啟照常續接
    env, cid = disk_cert("r8")
    env.clk.advance(5)
    rid = f"REQ_{env.clk():%Y%m%d_%H%M%S}_tmpbad00"
    bad = os.path.join(env.root, "results", f"{rid}.json")
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    open(bad, "w", encoding="utf-8").write("{bad")
    rt, r = restart(env)
    held1 = halt_of(rt) is not None
    REQ.atomic_write_json(bad, {"request_id": rid, "type": "stage_label", "cert_id": cid, "status": "done",
                                "created_at": env.clk().strftime("%Y-%m-%d %H:%M:%S")})
    rt, r = restart(env)
    check("R-8 讀不到 → HOLD；排除後重啟照常續接", held1 and r == cid and rt.is_active(), f"第二次 startup={r}")
    # R-9：資料截止時間（半行、cert_state 落後、全部讀取失敗）
    env, cid = disk_cert("r9", n=20)
    obs = env.obs(cid)
    st = env.state(cid)
    st["last_sample_time"] = obs[-5]["t"]               # cert_state 落後（寫入失敗或中斷）
    REQ.atomic_write_json(env.p(cid, "cert_state.json"), st)
    with open(env.p(cid, "observations.jsonl"), "a", encoding="utf-8") as f:
        f.write('{"t": "2026-11-02T23:59:59", "ok": tr')   # 寫到一半的最後一行
    put_req(env, cid, where="done", result="failed")
    rt, r = restart(env)
    rid9, res = env.request(rt, "end_cert", cid)
    er = (env.state(cid) or {}).get("end_recovery") or {}
    check("R-9 半行不計、以 observations 為準；cert_state 落後有註記",
          er.get("last_recorded_sample_at") == obs[-1]["t"] and er.get("last_valid_sample_at") == obs[-1]["t"]
          and er.get("cert_state_last_sample_time") == obs[-5]["t"] and any("不一致" in n for n in er.get("notes") or []),
          json.dumps({k: er.get(k) for k in ("last_recorded_sample_at", "last_valid_sample_at",
                                            "cert_state_last_sample_time")}, ensure_ascii=False))
    env, cid = disk_cert("r9b", n=10, mode="fail")
    put_req(env, cid, where="done", result="failed")
    rt, r = restart(env)
    env.request(rt, "end_cert", cid)
    er = (env.state(cid) or {}).get("end_recovery") or {}
    check("R-9 全部讀取失敗 → last_recorded 有值、last_valid＝null（不推估）",
          er.get("last_recorded_sample_at") == env.obs(cid)[-1]["t"] and er.get("last_valid_sample_at") is None,
          json.dumps({k: er.get(k) for k in ("last_recorded_sample_at", "last_valid_sample_at")}, ensure_ascii=False))
    # R-10：observations 讀不到
    env, cid = disk_cert("r10", n=10)
    put_req(env, cid, where="done", result="failed")
    rt, r = restart(env)
    obs_p = os.path.normcase(env.p(cid, "observations.jsonl"))

    def mk(orig):
        def f(path):
            if os.path.normcase(path) == obs_p:
                raise PermissionError(13, "模擬：observations.jsonl 讀不到")
            return orig(path)
        return f
    with Patch(CR, "_read_jsonl", mk):
        rid10, res = env.request(rt, "end_cert", cid)
    er = (env.state(cid) or {}).get("end_recovery") or {}
    check("R-10 observations 讀不到 → observations_unreadable、兩個時間 null（報告寫不明）",
          er.get("observations_unreadable") is True and er.get("last_recorded_sample_at") is None
          and er.get("last_valid_sample_at") is None and (res or {}).get("status") == "done",
          json.dumps({k: er.get(k) for k in ("observations_unreadable", "last_recorded_sample_at")}, ensure_ascii=False))


# =============================================================================================== N-1
RID_RE = re.compile(r"REQ_\d{8}_\d{6}_[0-9a-f]{8}")


def norm(v):
    if isinstance(v, str):
        today = datetime.now().strftime("%Y-%m-%d")
        v = RID_RE.sub("<RID>", v)
        v = re.sub(r"cert_bugfix_[A-Za-z0-9_]+", "<TMP>", v)   # 暫存資料夾名稱每次不同
        v = re.sub(re.escape(today) + r"[ T]\d\d:\d\d(:\d\d)?", "<NOW>", v)
        return v
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [norm(x) for x in v]
    return v


def cells(path):
    from openpyxl import load_workbook
    wb = load_workbook(path)
    out = {}
    for ws in wb.worksheets:
        out[ws.title] = [[c.coordinate, norm(c.value if not isinstance(c.value, datetime) else c.value.isoformat())]
                         for row in ws.iter_rows() for c in row if c.value is not None]
    return out


def t_normal():
    print("== N-1：正常結束與修正前相同（cert_state／events 只多 end_begin／報告逐格）==")
    env = Env("n1")
    rt = env.runtime()
    cid = env.start(rt)
    for mode, n in (("idle", 30), ("discharge", 120), ("idle", 60), ("charge", 120), ("idle", 40)):
        env.rd.mode = mode
        env.sample(rt, n)
    rid, res = env.request(rt, "end_cert", cid)
    st = norm(env.state(cid))
    ev = norm(env.events(cid))
    rep = env.reports(cid)
    cc = cells(env.p(cid, "reports", rep[-1])) if rep else {}
    check("N-1 正常結束 → done、沒有 end_recovery、報告 1 份",
          (res or {}).get("status") == "done" and "end_recovery" not in (env.state(cid) or {}) and len(rep) == 1)
    snap = {"cert_state": st, "events": ev, "report_name": norm(rep[-1]) if rep else None, "cells": cc,
            "result_message": norm((res or {}).get("message"))}
    if ARGS.dump:
        os.makedirs(ARGS.dump, exist_ok=True)
        with open(os.path.join(ARGS.dump, "n1_snapshot.json"), "x", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        print(f"  （N-1 對照已寫入 {ARGS.dump}）")
    if ARGS.compare:
        ref = norm(json.load(open(os.path.join(ARGS.compare, "n1_snapshot.json"), encoding="utf-8")))
        ev_new = [e for e in ev if e.get("type") != "end_begin"]
        begins = [e for e in ev if e.get("type") == "end_begin"]
        check("N-1 cert_state 與修正前相同", st == ref["cert_state"],
              str([k for k in set(st) | set(ref["cert_state"]) if st.get(k) != ref["cert_state"].get(k)]))
        check("N-1 events 與修正前相同，只多一筆 end_begin（含 request_id）",
              ev_new == ref["events"] and len(begins) == 1 and begins[0].get("request_id") == "<RID>",
              f"end_begin {len(begins)}；其餘相同={ev_new == ref['events']}")
        diff = [(s, a, b) for s in set(cc) | set(ref["cells"])
                for a, b in [(cc.get(s), ref["cells"].get(s))] if a != b]
        check("N-1 報告檔名與每一格與修正前相同（正規化產生時間）",
              not diff and snap["report_name"] == ref["report_name"] and snap["result_message"] == ref["result_message"],
              f"不同工作表 {[d[0] for d in diff]}")


# =============================================================================================== 補正（G-1／G-2／G-3／T-1）
class Killed(BaseException):
    """模擬行程被強制結束（不被 except Exception 攔下）。"""


def rb(path):
    with open(path, "rb") as f:
        return f.read()


def tree(root):
    """T-1：資料夾快照（相對路徑 → SHA-256）。"""
    out = {}
    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            out[os.path.relpath(p, root).replace("\\", "/")] = sha(p)
    return out


def only_changes(a, b, rids, also=()):
    """T-1：除了請求結果與歸檔（以及 also 列出的檔案）之外，沒有任何檔案新增、刪除或變更。"""
    rids = [rids] if isinstance(rids, str) else list(rids)
    allow_add = {f"results/{r}.json" for r in rids} | {f"requests/done/{r}.json" for r in rids}
    allow_rm = {f"requests/pending/{r}.json" for r in rids}
    add, rm = set(b) - set(a), set(a) - set(b)
    chg = {k for k in set(a) & set(b) if a[k] != b[k]}
    bad = sorted((add - allow_add) | (rm - allow_rm) | (chg - set(also)))
    return not bad, f"T-1 其他變動 {bad}" if bad else "T-1 只有允許的變動"


def kill_event(kind):
    def mk(orig):
        def f(path, rec):
            if path.endswith("events.jsonl") and isinstance(rec, dict) and rec.get("type") == kind:
                raise Killed(kind)
            return orig(path, rec)
        return f
    return Patch(CR, "_append_jsonl", mk)


def kill_active():
    def mk(orig):
        def f(path, data):
            if path.endswith("active.json") and isinstance(data, dict) and data.get("cert_id") is None:
                raise Killed("active")
            return orig(path, data)
        return f
    return Patch(REQ, "atomic_write_json", mk)


def fail_result():
    def mk(orig):
        def f(root, req, status, message, cert_id=None, data=None):
            if req.get("type") == "end_cert":
                raise PermissionError(13, "模擬：results 寫入失敗")
            return orig(root, req, status, message, cert_id=cert_id, data=data)
        return f
    return Patch(REQ, "write_result", mk)


def ended_pending(name, mode, before_end=None, extra=None):
    """建立「ended 已落盤、處理結果沒有寫出、請求仍在 pending」的認證。
    mode：end＝寫 end 事件時行程中止；active＝清除 active.json 時行程中止；result＝結果檔寫不出去（報告已產生）。"""
    import contextlib
    env = Env(name)
    rt, cid = recording_cert(env, 20)
    if before_end:
        before_end(env, rt, cid)
    env.clk.advance(1)
    rid = REQ.new_request(env.root, "end_cert", None, cert_id=cid, now=env.clk())
    with contextlib.ExitStack() as es:
        es.enter_context({"end": lambda: kill_event("end"), "active": kill_active, "result": fail_result}[mode]())
        for cm in extra or ():
            es.enter_context(cm)
        try:
            rt.process_requests()
        except (Killed, Exception):                     # noqa: BLE001
            pass
    return env, cid, rid, rt


def pre_ended(env, cid, rid):
    st = env.state(cid) or {}
    return (st.get("status") == "ended" and st.get("end_request") == rid and REQ.read_result(env.root, rid) is None
            and os.path.exists(os.path.join(env.root, "requests", "pending", f"{rid}.json")))


def after_restart(env, advance=60):
    env.clk.advance(advance)
    rt = env.runtime()
    rt.startup()
    return rt


def result(env, rid):
    res = REQ.read_result(env.root, rid) or {}
    return res, (res.get("data") or {})


def ends_of(env, cid):
    return [e for e in env.events(cid) if e.get("type") == "end"]


WARN = "⚠ 結束已完成，但"


def t_g1():
    print("== G-1：同一請求是否已完成（逾期判斷之前；cert_state 無法確認）==")
    # G1-1：超過 300 秒、磁碟確認相符
    env, cid, rid, rt = ended_pending("g1_1", "result")
    pre = pre_ended(env, cid, rid)
    rt2 = after_restart(env, 400)
    a = tree(env.root)
    rt2.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid)
    res, data = result(env, rid)
    check("G1-1 超過 300 秒、磁碟確認相符 → done（補寫，不寫成 expired）",
          pre and res.get("status") == "done" and data.get("resent") is True and ok1, f"結果 {res.get('status')}；{d1}")
    # G1-2／G1-3：明確不相符 → 原本流程
    env = Env("g1_2")
    rt, cid = recording_cert(env, 20)
    rid1, _ = env.request(rt, "end_cert", cid)
    rid2 = put_req(env, cid, created=env.clk() - timedelta(seconds=400))
    a = tree(env.root)
    rt.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid2)
    res, _ = result(env, rid2)
    check("G1-2a 已由其他請求結束、逾期 → expired（原本流程）", res.get("status") == "expired" and ok1,
          f"結果 {res.get('status')}；{d1}")
    calls = env.rd.calls
    rid3 = put_req(env, cid)
    a = tree(env.root)
    rt.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid3)
    res, _ = result(env, rid3)
    check("G1-2b 已由其他請求結束、未逾期 → rejected（原本流程；不讀 HMI、無 end_begin）",
          res.get("status") == "rejected" and ok1 and env.rd.calls == calls, f"結果 {res.get('status')}；{d1}")
    st = env.state(cid)
    st["cert_id"] = "CERT_Y"
    REQ.atomic_write_json(env.p(cid, "cert_state.json"), st)
    rid4 = put_req(env, cid)
    a = tree(env.root)
    rt.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid4)
    res, _ = result(env, rid4)
    check("G1-3 cert_state 的 cert_id 不同 → 原本流程（rejected）", res.get("status") == "rejected" and ok1,
          f"結果 {res.get('status')}；{d1}")
    # G1-4a／G1-4b：無法確認、沒有載入
    for key, label, adv, content in (("a", "超過 300 秒（壞 JSON）", 400, b"{bad"),
                                     ("b1", "未逾期（壞 JSON）", 60, b"{bad"),
                                     ("b2", "未逾期（list）", 60, b"[]"),
                                     ("b3", "未逾期（檔案不存在）", 60, None)):
        env, cid, rid, rt = ended_pending(f"g1_4{key}", "result")
        pre = pre_ended(env, cid, rid)
        sp = env.p(cid, "cert_state.json")
        if content is None:
            os.remove(sp)
        else:
            open(sp, "wb").write(content)
        rt2 = after_restart(env, adv)
        calls = env.rd.calls
        a = tree(env.root)
        rt2.process_requests()
        ok1, d1 = only_changes(a, tree(env.root), rid)
        res, data = result(env, rid)
        check(f"G1-4{key} cert_state 無法確認、沒有載入、{label} → failed＋unconfirmed；不執行（無 end_begin、不讀 HMI）",
              pre and res.get("status") == "failed" and data.get("end_state") == "unconfirmed"
              and "無法確認" in str(res.get("message")) and env.rd.calls == calls and ok1,
              f"結果 {res.get('status')}；end_state={data.get('end_state')}；{d1}")
    # G1-4c：載入中、recording、磁碟讀不到 → 正常結束
    env = Env("g1_4c")
    rt, cid = recording_cert(env, 20)
    with fail_read_for(env.p(cid, "cert_state.json")):
        rid, res = env.request(rt, "end_cert", cid)
    ok, d = ended_ok(env, cid, rid)
    check("G1-4c 載入中、recording、磁碟讀不到 → 正常結束", ok, d)
    # G1-4d：載入中、記憶體 ended 且 end_request＝本請求、超過 300 秒、磁碟壞 → 依記憶體補寫 done
    env, cid, rid, rt = ended_pending("g1_4d", "result")
    pre = pre_ended(env, cid, rid)
    open(env.p(cid, "cert_state.json"), "wb").write(b"{bad")
    env.clk.advance(400)
    a = tree(env.root)
    rt.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid)
    res, data = result(env, rid)
    check("G1-4d 磁碟無法確認、記憶體確認（cert_id／資料夾／ended／end_request）→ 依記憶體補寫 done",
          pre and res.get("status") == "done" and data.get("ended_source") == "memory" and ok1,
          f"結果 {res.get('status')}；來源 {data.get('ended_source')}；{d1}")
    # G1-4f：記憶體 ended 但 end_request 是其他請求 → 不得寫 done
    env, cid, rid, rt = ended_pending("g1_4f", "result")
    keep = os.path.join(TMP, "g1_4f_keep.json")
    os.replace(os.path.join(env.root, "requests", "pending", f"{rid}.json"), keep)
    open(env.p(cid, "cert_state.json"), "wb").write(b"{bad")
    calls = env.rd.calls
    rid2 = put_req(env, cid)
    a = tree(env.root)
    rt.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid2)
    res, _ = result(env, rid2)
    check("G1-4f 記憶體 ended 但 end_request 不是本請求 → 不寫 done（原本流程 rejected）",
          res.get("status") == "rejected" and env.rd.calls == calls and ok1, f"結果 {res.get('status')}；{d1}")
    # G1-4g：記憶體 cert_id 相同但資料夾不同 → 不算記憶體確認 → 磁碟無法確認 → unconfirmed
    env, cid, rid, rt = ended_pending("g1_4g", "result")
    open(env.p(cid, "cert_state.json"), "wb").write(b"{bad")
    rt.folder = os.path.join(env.root, "CERT_ELSEWHERE")
    a = tree(env.root)
    rt.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid)
    res, data = result(env, rid)
    check("G1-4g 記憶體資料夾不符 → 不依記憶體寫 done → failed＋unconfirmed",
          res.get("status") == "failed" and data.get("end_state") == "unconfirmed" and ok1,
          f"結果 {res.get('status')}；{d1}")
    # G1-4e：另一份認證進行中，請求所屬認證無法確認 → 只寫此請求結果；進行中的認證不受影響
    env, a_cid, rid, rt = ended_pending("g1_4e", "result")
    keep = os.path.join(TMP, "g1_4e_keep.json")
    pend = os.path.join(env.root, "requests", "pending", f"{rid}.json")
    os.replace(pend, keep)
    rt2 = after_restart(env, 5)
    b_cid = env.start(rt2)
    env.sample(rt2, 10)
    open(env.p(a_cid, "cert_state.json"), "wb").write(b"{bad")
    os.replace(keep, pend)
    calls = env.rd.calls
    snap_a = tree(env.root)
    rt2.process_requests()
    ok1, d1 = only_changes(snap_a, tree(env.root), rid)
    res, data = result(env, rid)
    n_b = len(env.obs(b_cid))
    env.sample(rt2, 5)
    check("G1-4e 其他認證進行中、請求所屬認證無法確認 → failed＋unconfirmed；進行中認證不 HOLD、照常取樣",
          res.get("status") == "failed" and data.get("end_state") == "unconfirmed" and ok1
          and rt2.active_id() == b_cid and halt_of(rt2) is None and len(env.obs(b_cid)) == n_b + 5
          and env.rd.calls == calls + 5,
          f"結果 {res.get('status')}；進行中 {rt2.active_id() == b_cid}；HOLD={halt_of(rt2) is not None}；{d1}")


def t_g2():
    print("== G-2／G-2b：ended 已落盤後的補寫流程（end 事件、報告狀態、active.json）==")
    # G2-1：end 事件缺少 → 補登一次
    env, cid, rid, rt = ended_pending("g2_1", "end")
    pre = pre_ended(env, cid, rid) and not ends_of(env, cid)
    evp = env.p(cid, "events.jsonl")
    ev0, n_obs = rb(evp), len(env.obs(cid))
    rt2 = after_restart(env)
    calls = env.rd.calls
    a = tree(env.root)
    rt2.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid, also={f"{cid}/events.jsonl", "active.json"})
    res, data = result(env, rid)
    st = env.state(cid) or {}
    ends = ends_of(env, cid)
    e0 = ends[0] if ends else {}
    check("G2-1 end 事件缺少 → 補登一次（t＝ended_at、backfilled、backfilled_at、request_id）；結果 done＋⚠",
          pre and res.get("status") == "done" and data.get("end_event") == "backfilled" and len(ends) == 1
          and e0.get("backfilled") is True and e0.get("t") == st.get("ended_at") and e0.get("request_id") == rid
          and e0.get("backfilled_at") and rb(evp).startswith(ev0) and str(res.get("message")).startswith(WARN)
          and st.get("status") == "ended" and data.get("active_json") == "cleared" and REQ.read_active(env.root) is None
          and ok1, f"結果 {res.get('status')}；end_event={data.get('end_event')}；end 事件 {len(ends)}；{d1}")
    check("G2-8 補寫流程不重新取樣、不讀 HMI、不產報（report_state＝none）",
          len(env.obs(cid)) == n_obs and env.rd.calls == calls and not env.reports(cid)
          and data.get("report_state") == "none", f"report_state={data.get('report_state')}")
    # G2-2：end 事件已存在 → 不重複
    env, cid, rid, rt = ended_pending("g2_2", "active")
    pre = pre_ended(env, cid, rid) and len(ends_of(env, cid)) == 1
    rt2 = after_restart(env)
    a = tree(env.root)
    rt2.process_requests()
    ok1, d1 = only_changes(a, tree(env.root), rid, also={"active.json"})
    res, data = result(env, rid)
    check("G2-2 end 事件已存在 → 不重複補登（end_event＝present）；active.json 清除",
          pre and res.get("status") == "done" and data.get("end_event") == "present" and len(ends_of(env, cid)) == 1
          and data.get("active_json") == "cleared" and REQ.read_active(env.root) is None and ok1,
          f"end_event={data.get('end_event')}；{d1}")
    # G2-3：events 無法確認（中間損壞行）→ 不補登、unknown＋⚠；事件檔原始 bytes 完全不變
    env, cid, rid, rt = ended_pending("g2_3", "end")
    evp = env.p(cid, "events.jsonl")
    with open(evp, "ab") as f:
        f.write(b'{"t": "2026-11-02T10:00:40", "ty\r\n')
    ev0 = rb(evp)
    rt2 = after_restart(env)
    rt2.process_requests()
    res, data = result(env, rid)
    check("G2-3／G3-8a events 有損壞行 → 不補登、end_event＝unknown＋⚠、維持 ended；events.jsonl bytes 不變",
          res.get("status") == "done" and data.get("end_event") == "unknown" and not ends_of(env, cid)
          and rb(evp) == ev0 and str(res.get("message")).startswith(WARN)
          and (env.state(cid) or {}).get("status") == "ended",
          f"end_event={data.get('end_event')}；bytes 不變={rb(evp) == ev0}")
    # G2-4：補登失敗 → backfill_failed＋⚠
    env, cid, rid, rt = ended_pending("g2_4", "end")
    rt2 = after_restart(env)
    with fail_event("end", times=9):
        rt2.process_requests()
    res, data = result(env, rid)
    check("G2-4 補登失敗 → end_event＝backfill_failed＋⚠、維持 ended、結果 done",
          res.get("status") == "done" and data.get("end_event") == "backfill_failed" and not ends_of(env, cid)
          and str(res.get("message")).startswith(WARN) and (env.state(cid) or {}).get("status") == "ended",
          f"end_event={data.get('end_event')}")

    # G2-5：結束前的中途報告不得當成結束報告
    def midway(env, rt, cid):
        env.request(rt, "regenerate", cid)
        env.clk.advance(2)

    def boom(orig):
        return lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模擬：save_report 失敗"))
    env, cid, rid, rt = ended_pending("g2_5", "result", before_end=midway, extra=[Patch(RX, "save_report", boom)])
    pre = pre_ended(env, cid, rid) and any("_中途" in f for f in env.reports(cid))
    rt2 = after_restart(env)
    rt2.process_requests()
    res, data = result(env, rid)
    check("G2-5 只有結束前的中途報告、結束報告失敗 → report_state＝failed、report＝null（不指向中途報告）＋⚠",
          pre and res.get("status") == "done" and data.get("report_state") == "failed" and data.get("report") is None
          and "_中途" not in str(res.get("message")) and str(res.get("message")).startswith(WARN),
          f"report_state={data.get('report_state')}；report={data.get('report')}")
    # G2-6：active.json 4 種情況（補寫流程）＋正常結束時讀不到
    for key, label, content, want in (("a", "指向本認證", "keep", "cleared"),
                                      ("b", "指向其他認證", {"cert_id": "CERT_OTHER", "updated_at": "x"}, "other"),
                                      ("c", "cert_id＝null", {"cert_id": None, "updated_at": "x"}, "none"),
                                      ("d", "讀不到（壞 JSON）", b"{bad", "unknown")):
        env, cid, rid, rt = ended_pending(f"g2_6{key}", "active")
        ap = os.path.join(env.root, "active.json")
        if isinstance(content, dict):
            REQ.atomic_write_json(ap, content)
        elif isinstance(content, bytes):
            open(ap, "wb").write(content)
        a0 = rb(ap)
        rt2 = after_restart(env)
        rt2.process_requests()
        res, data = result(env, rid)
        same = rb(ap) == a0
        check(f"G2-6{key} 補寫流程 active.json {label} → active_json＝{want}"
              + ("（已清除）" if want == "cleared" else "（不改寫）"),
              res.get("status") == "done" and data.get("active_json") == want
              and (REQ.read_active(env.root) is None if want == "cleared" else same)
              and (str(res.get("message")).startswith(WARN) if want == "unknown" else True),
              f"active_json={data.get('active_json')}；檔案不變={same}")
    env = Env("g2_6e")
    rt, cid = recording_cert(env, 20)
    ap = os.path.join(env.root, "active.json")
    open(ap, "wb").write(b"{bad")
    rid, res = env.request(rt, "end_cert", cid)
    data = (res or {}).get("data") or {}
    check("G2-6e 正常結束時 active.json 讀不到 → 不覆寫、active_json＝unknown、訊息有 ⚠",
          (res or {}).get("status") == "done" and rb(ap) == b"{bad" and data.get("active_json") == "unknown"
          and "⚠" in str((res or {}).get("message")), f"檔案不變={rb(ap) == b'{bad'}；active_json={data.get('active_json')}")
    # G2-7：同一程序內 ended 之後失敗 → 不寫成 failed
    env = Env("g2_7")
    rt, cid = recording_cert(env, 20)
    with fail_event("end"):
        rid, res = env.request(rt, "end_cert", cid)
    data = (res or {}).get("data") or {}
    ends = ends_of(env, cid)
    check("G2-7 同程序 ended 後 end 事件寫入失敗 → done（不是 failed）＋post_end_error、補登 end 事件、不 HOLD",
          (res or {}).get("status") == "done" and data.get("post_end_error") and data.get("end_event") == "backfilled"
          and len(ends) == 1 and halt_of(rt) is None and (env.state(cid) or {}).get("status") == "ended"
          and str((res or {}).get("message")).startswith(WARN),
          f"結果 {(res or {}).get('status')}；end_event={data.get('end_event')}")
    env = Env("g2_7b")
    rt, cid = recording_cert(env, 20)

    def mk_act(orig):
        def f(path, data):
            if path.endswith("active.json") and isinstance(data, dict) and data.get("cert_id") is None:
                raise PermissionError(13, "模擬：active.json 寫入失敗")
            return orig(path, data)
        return f
    with Patch(REQ, "atomic_write_json", mk_act):
        rid, res = env.request(rt, "end_cert", cid)
    data = (res or {}).get("data") or {}
    check("G2-7b 同程序 active.json 清除失敗 → done＋active_json＝clear_failed＋⚠",
          (res or {}).get("status") == "done" and data.get("active_json") == "clear_failed"
          and str((res or {}).get("message")).startswith(WARN), f"結果 {(res or {}).get('status')}；{data.get('active_json')}")
    # G2-9：報告狀態判定
    for key, label, want in (("f", "結束報告正常", "ok"), ("a", "紀錄存在但檔案不存在", "unconfirmed"),
                             ("b", "只剩 .partial.xlsx", "failed"), ("c", "at 無法解析", "unconfirmed"),
                             ("d", "成功之後有更晚的錯誤", "ok"), ("e", "只有中途報告", "none")):
        env, cid, rid, rt = ended_pending(f"g2_9{key}", "result")
        st = env.state(cid) or {}
        reps = st.get("reports") or []
        pre = pre_ended(env, cid, rid) and len(reps) == 1
        rp = env.p(cid, reps[-1]["file"]) if reps else None
        if key == "a":
            os.remove(rp)
        elif key == "b":
            os.remove(rp)
            st["reports"] = []
            open(env.p(cid, "reports", f"認證測試報告_{cid}_02.partial.xlsx"), "wb").write(b"PK\x03\x04partial")
        elif key == "c":
            reps[-1]["at"] = "bad"
        elif key == "d":
            st["last_report_error"] = {"at": (env.clk() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S"),
                                       "error": "模擬：之後的重產失敗"}
        elif key == "e":
            mid = env.p(cid, "reports", f"認證測試報告_{cid}_01_中途.xlsx")
            os.replace(rp, mid)
            st["reports"] = [{"file": os.path.relpath(mid, env.p(cid)), "at": T0.strftime("%Y-%m-%d %H:%M:%S")}]
        if key != "f":
            REQ.atomic_write_json(env.p(cid, "cert_state.json"), st)
        rt2 = after_restart(env)
        rt2.process_requests()
        res, data = result(env, rid)
        msg = str(res.get("message"))
        extra_ok = {"f": data.get("report") == rp and not msg.startswith(WARN),
                    "a": data.get("report") is None and msg.startswith(WARN),
                    "b": data.get("report") is None and data.get("partial_files") and msg.startswith(WARN),
                    "c": data.get("report") is None and msg.startswith(WARN),
                    "d": data.get("report") == rp and (data.get("later_report_error") or {}).get("error"),
                    "e": data.get("report") is None and msg.startswith(WARN)}[key]
        check(f"G2-9{key} {label} → report_state＝{want}", pre and res.get("status") == "done"
              and data.get("report_state") == want and bool(extra_ok),
              f"report_state={data.get('report_state')}；report={os.path.basename(str(data.get('report')))}")


def t_g3():
    print("== G-3：方案 S（events／cert_timing 損壞 → 保守處理；原始內容不改）==")
    # G3-1：events 中間損壞行 → 重啟 HOLD
    env, cid = disk_cert("g3_1")
    evp = env.p(cid, "events.jsonl")
    bad = b'{"t": "2026-11-02T10:00:40", "type": "end_beg\r\n'
    with open(evp, "ab") as f:
        f.write(bad)
    ev0 = rb(evp)
    rt, r = restart(env)
    ok, d = held(env, rt, r, cid)
    reason = str((halt_of(rt) or {}).get("reason"))
    check("G3-1 events.jsonl 中間有損壞行 → 重啟 HOLD（原因註明 events.jsonl、無法確認）",
          ok and "events.jsonl" in reason and "無法確認" in reason, d + f"；原因 {reason[:80]}")
    check("G3-8b 損壞行與原始內容完整保留（原始 bytes 為現檔前綴，未刪除／截斷／覆寫）",
          rb(evp).startswith(ev0) and bad in rb(evp))
    # G3-2a：尾端半行在重讀期間寫完（end_begin）→ 照一般規則判定（HOLD）
    env, cid = disk_cert("g3_2a")
    evp = env.p(cid, "events.jsonl")
    rid_x = "REQ_20261102_100030_abcdef12"
    head = ('{"t": "2026-11-02T10:00:30", "type": "end_begin", "detail": "x", "request_id": "' + rid_x).encode()
    with open(evp, "ab") as f:
        f.write(head)

    def finish():
        time.sleep(0.25)
        with open(evp, "ab") as f:
            f.write(b'"}\r\n')
    th = threading.Thread(target=finish)
    th.start()
    rt, r = restart(env)
    th.join()
    ok, d = held(env, rt, r, cid)
    reason = str((halt_of(rt) or {}).get("reason"))
    check("G3-2a 尾端半行在重讀期間寫完（end_begin）→ 依完整紀錄判定 HOLD", ok and rid_x in reason,
          d + f"；原因 {reason[:80]}")
    # G3-2b：尾端半行重讀 3 次仍未完成 → 損壞 → HOLD
    env, cid = disk_cert("g3_2b")
    evp = env.p(cid, "events.jsonl")
    with open(evp, "ab") as f:
        f.write(b'{"t": "2026-11-02T10:00:30", "type": "env_st')
    ev0 = rb(evp)
    t0 = time.perf_counter()
    rt, r = restart(env)
    el = time.perf_counter() - t0
    ok, d = held(env, rt, r, cid)
    reason = str((halt_of(rt) or {}).get("reason"))
    check("G3-2b 尾端半行重讀後仍不完整 → HOLD（原因註明不完整）", ok and "不完整" in reason,
          d + f"；原因 {reason[:80]}；startup {el:.2f} 秒")
    check("G3-8c 尾端重讀不改寫檔案（原始 bytes 為現檔前綴）", rb(evp).startswith(ev0))
    # G3-3：M7b 時 end_begin 沒有 request_id → 無法確認歸屬 → started_no_result
    env, cid = disk_cert("g3_3")
    env.clk.advance(5)
    rid = put_req(env, cid)
    with open(env.p(cid, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": env.clk().strftime("%Y-%m-%dT%H:%M:%S"), "type": "end_begin", "detail": "x"},
                           ensure_ascii=False) + "\n")
    rt, r = restart(env)
    rt.process_requests()
    res, data = result(env, rid)
    hold = data.get("hold") or {}
    check("G3-3 M7b：end_begin 沒有 request_id → 無法確認歸屬 → failed＋started_no_result（不判為未開始）",
          res.get("status") == "failed" and hold.get("class") == "started_no_result" and hold.get("uncertain"),
          f"結果 {res.get('status')}；class={hold.get('class')}")
    # G3-4：events.jsonl 整個讀不到（對照：修正前就會 HOLD）
    env, cid = disk_cert("g3_4")
    evp = env.p(cid, "events.jsonl")
    os.remove(evp)
    os.makedirs(evp)
    rt, r = restart(env)
    st = env.state(cid) or {}
    check("G3-4 events.jsonl 讀不到 → HOLD（對照）",                 # events 是資料夾 → 不用 held()（它會讀 events）
          r is None and halt_of(rt) is not None and not rt.is_active() and not st.get("interruptions"),
          f"startup={r}；HOLD={halt_of(rt) is not None}")
    # G3-5／G3-6／G3-7：M7b 時 cert_timing／events 有損壞行 → 保守分類、訊息與欄位寫明無法確認
    for key, label, fname in (("5", "cert_timing.jsonl", CR.TIMING_FILE), ("6", "events.jsonl", "events.jsonl")):
        env, cid = disk_cert(f"g3_{key}")
        env.clk.advance(5)
        rid = put_req(env, cid)
        fp = env.p(cid, fname)
        with open(fp, "ab") as f:
            f.write(b'{"wall_start": "2026-11-0\r\n')
        f0 = rb(fp)
        rt, r = restart(env)
        rt.process_requests()
        res, data = result(env, rid)
        hold = data.get("hold") or {}
        msg = str(res.get("message"))
        check(f"G3-{key} M7b：{label} 有損壞行 → failed＋started_no_result（不判為未開始）",
              res.get("status") == "failed" and hold.get("class") == "started_no_result",
              f"結果 {res.get('status')}；class={hold.get('class')}")
        check(f"G3-7{'ab'[int(key) - 5]} 保守分類（{label}）：訊息為「無法確認是否已開始執行」、evidence 只列實際證據（空）、"
              "uncertain 寫明無法確認、first_attempt_at＝null；原始 bytes 保留",
              "無法確認是否已開始執行" in msg and hold.get("evidence") == [] and hold.get("uncertain")
              and any("無法確認" in str(u) for u in hold.get("uncertain") or []) and "first_attempt_at" in hold
              and hold.get("first_attempt_at") is None and rb(fp).startswith(f0),
              f"訊息 {msg[:40]}；uncertain={str(hold.get('uncertain'))[:60]}；first_attempt_at={hold.get('first_attempt_at', '（無欄位）')}")
    # G3-7c：有實際 end_begin → first_attempt_at 取該事件時間、訊息為已開始執行
    env, cid = disk_cert("g3_7c")
    env.clk.advance(5)
    rid = put_req(env, cid)
    t_begin = env.clk().strftime("%Y-%m-%dT%H:%M:%S")
    with open(env.p(cid, "events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": t_begin, "type": "end_begin", "detail": "x", "request_id": rid}, ensure_ascii=False) + "\n")
    rt, r = restart(env)
    rt.process_requests()
    res, data = result(env, rid)
    hold = data.get("hold") or {}
    check("G3-7c 有 end_begin → first_attempt_at＝該事件時間、訊息不寫「無法確認」",
          hold.get("first_attempt_at") == t_begin and "無法確認是否已開始" not in str(res.get("message")),
          f"first_attempt_at={hold.get('first_attempt_at', '（無欄位）')}")


guard("BUG-1", t1)
guard("BUG-2", t2)
guard("重啟", t_restart)
guard("N-1", t_normal)
N_ORIG = len(RESULTS)
GROUPS = {}
for _name, _fn in (("G-1", t_g1), ("G-2", t_g2), ("G-3", t_g3)):
    _n0 = len(RESULTS)
    guard(_name, _fn)
    GROUPS[_name] = RESULTS[_n0:]
shutil.rmtree(TMP, ignore_errors=True)
n_pass = sum(RESULTS)
n_fail = len(RESULTS) - n_pass
o_pass = sum(RESULTS[:N_ORIG])
print(f"\n== 原有 {N_ORIG} 項：PASS {o_pass}／FAIL {N_ORIG - o_pass} ==")
for _name, _r in GROUPS.items():
    print(f"== {_name}：PASS {sum(_r)}／FAIL {len(_r) - sum(_r)}（共 {len(_r)}）==")
print(f"\n== test_cert_r2c_bugfix：PASS {n_pass}／FAIL {n_fail}（共 {len(RESULTS)}）==")
sys.exit(0 if n_fail == 0 else 1)
