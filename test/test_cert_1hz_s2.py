# -*- coding: utf-8 -*-
"""
test_cert_1hz_s2.py — M-S2（認證 1 秒固定格點＋報告時推導）離線測試；r2：Raw Data 每秒一列、整筆沿用
==========================================================================================
全部離線：假 HMI reader、模擬單調時鐘（由測試推進，確定性）、暫存資料夾；不連任何設備、不送任何控制。

涵蓋（M-S2 r2）：
  T-4 格點／MISSED／不重複／不漂移　T-5 LIVE→CARRIED→LIVE（30／60／300 秒，無上限）　T-6 N/A
  A-1～A-5 整筆沿用（不逐欄混用）　B-1～B-3 服務中斷補秒（SERVICE_STOPPED）／三種情況分開／未正常結束
  N-1～N-5 沒有中斷證據 → NO_SLOT　CR-1～CR-9 時鐘回撥／往前跳／段落內調整
  SE-1～SE-6 認證開始／結束邊界、只有少數格點、沒有格點、格點間隔非 1 秒
  SRC-1 來源序號＝observations.jsonl 實體行號（含半行）　INV 每個情境的不變條件（連續、整筆沿用、年齡）
  C-1～C-3 覆蓋率與 M-1　D-1～D-4 寫入失敗與壞行　T-7 補值隔離（原始層、AST）　T-11 Excel
  T-10 分析節流　E-1～E-3 回復檢查工具（唯讀）　F-1 不使用未來資料　X 禁止控制（AST）
"""
import ast
import glob
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                       # noqa: BLE001
    pass

import cert_report_config as CFG                        # noqa: E402
import cert_report_xlsx as RX                           # noqa: E402
import cert_requests as REQ                             # noqa: E402
import cert_runtime as CR                               # noqa: E402
import report_display as RD                             # noqa: E402
import report_quality as RQ                             # noqa: E402
import sample_scheduler as SS                           # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


TZ = timezone(timedelta(hours=8))
WALL0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=TZ)
T0 = WALL0.replace(tzinfo=None)


class Sim:
    """模擬時間：mono（秒）、wall（具時區）、clock（無時區，runtime 用）。off＝牆鐘偏移（模擬時鐘調整；mono 不受影響）。"""
    def __init__(self):
        self.t = 0.0
        self.off = 0.0

    def mono(self):
        return self.t

    def wall(self):
        return WALL0 + timedelta(seconds=self.t + self.off)

    def clock(self):
        return T0 + timedelta(seconds=self.t + self.off)


SIM = Sim()
_ORIG = {"ss_wall": SS.wall_now, "ss_def": SS.SlotScheduler.__init__.__defaults__,
         "rq_def": RQ.SlotSegment.__init__.__defaults__}


def _install_sim_wall():
    SS.wall_now = SIM.wall
    SS.SlotScheduler.__init__.__defaults__ = (time.monotonic, SIM.wall, None)
    RQ.SlotSegment.__init__.__defaults__ = ("start", time.monotonic, SIM.wall, RQ.SLOT_LOG_FILE)


def _restore():
    SS.wall_now = _ORIG["ss_wall"]
    SS.SlotScheduler.__init__.__defaults__ = _ORIG["ss_def"]
    RQ.SlotSegment.__init__.__defaults__ = _ORIG["rq_def"]


class SimStop:
    """取樣迴圈的 stop_event：wait(x) 只推進模擬時間。到達 end_at 時先送 Menu 22 結束（模擬請求執行緒），到達 stop_at 停止。"""
    def __init__(self, rt, root, stop_at, end_at=None):
        self.rt, self.root, self.stop_at, self.end_at = rt, root, stop_at, end_at
        self.flag = False
        self.ended = False

    def is_set(self):
        return self.flag or SIM.t >= self.stop_at

    def set(self):
        self.flag = True

    def wait(self, timeout=None):
        SIM.t += timeout if timeout else 1.0
        if self.end_at is not None and not self.ended and SIM.t >= self.end_at:
            self.ended = True
            cid = self.rt.active_id()
            REQ.new_request(self.root, "end_cert", cert_id=cid, now=SIM.clock())
            self.rt.process_requests()
            self.flag = True
        return self.is_set()


GOOD = dict(soc=50.0, v=950.0, i=0.0, p=0.0, rmax=30.0, rmin=25.0, cmax=3.40, cmin=3.30)
# r2：預設為待機（電流／功率 0、充放電旗標 False → Raw Data「狀態」＝待機，通過報告有效判定）；
#     充放電旗標依電流方向給（|I|>1 A），與實機一致；尚未形成階段的充放電觀測狀態無法判定 → 依現行規則不是有效觀測。
KEYS = {"soc": "soc_percent", "v": "battery_voltage_v", "i": "battery_current_a", "p": "actual_active_power_kw",
        "rmax": "rack_max_temperature_c", "rmin": "rack_min_temperature_c",
        "cmax": "rack_cell_max_voltage_v", "cmin": "rack_cell_min_voltage_v"}
ENGINE = {"soc": "soc", "v": "voltage_v", "i": "current_a", "p": "power_kw", "rmax": "rack_max_c",
          "rmin": "rack_min_c", "cmax": "cell_vmax_v", "cmin": "cell_vmin_v"}


def reading(fail=(), **kw):
    vals = dict(GOOD)
    vals.update(kw)
    r = {KEYS[k]: v for k, v in vals.items()}
    cur = vals.get("i")
    cur = cur if isinstance(cur, (int, float)) and cur == cur else 0.0
    r.update({"_fail": list(fail), "communication_ok": True, "raw_source_time": "",
              "pcs_charging_flag": cur > 1.0, "pcs_discharging_flag": cur < -1.0, "pcs_fault_flag": False,
              "battery_power_status": "已上電"})
    return r


class Plant:
    """假 HMI：script(i, t) → (耗時秒, 讀值 dict｜None｜Exception)。只提供讀取函式，沒有任何控制介面。"""
    def __init__(self, script):
        self.script = script
        self.calls = []

    def read(self):
        i = len(self.calls)
        dur, out = self.script(i, SIM.t)
        self.calls.append(SIM.t)
        SIM.t += dur
        if isinstance(out, Exception):
            raise out
        return out


TMP = tempfile.mkdtemp(prefix="cert_1hz_s2_")


def new_rt(root, plant, **kw):
    return CR.CertRuntime(root, reader=plant.read, heat_reader=None, enable_shots=False, clock=SIM.clock,
                          log=lambda *a: None, mono=SIM.mono, **kw)


def start(rt, root):
    REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW"}, now=SIM.clock())
    rt.process_requests()
    return rt.active_id()


def run_cert(name, script, seconds, end=True):
    """開始 → 背景格點取樣 seconds 秒 → （end）Menu 22 結束並產報。回傳 (rt, root, cid, folder, plant)。"""
    SIM.t, SIM.off = 0.0, 0.0
    root = os.path.join(TMP, name)
    plant = Plant(script)
    rt = new_rt(root, plant)
    cid = start(rt, root)
    stop = SimStop(rt, root, stop_at=seconds + (5 if end else 0), end_at=seconds if end else None)
    rt._slot_sample_loop(stop)
    return rt, root, cid, os.path.join(root, cid), plant


def jl(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


# ---------------------------------------------------------------- r2：Raw Data 每秒列的取得與共通檢查
VKEYS = RX.LOG_KEYS + ["power_kw"]


def raw_of(rt, folder):
    """離線重算（與產報同一路徑）→ (rs, res, ss)。rs＝RX.raw_second_rows（不適用 → None）。"""
    res = rt._offline(folder).analysis()
    ss = CR.build_sampling_slots(folder)
    rs = RX.raw_second_rows(res, ss["grid"]) if ss else None
    return rs, res, ss


# r2b：Excel Raw Data 只顯示 13 欄（時間／狀態／10 數值／說明）、全部黑色、不顯示技術標記
TECH = ("LIVE", "CARRIED", "MISSED", "SERVICE_STOPPED", "NO_SLOT", "非即時", "沿用", "跳格", "未執行讀取", "本秒",
        "時鐘異常", "無格點紀錄", "尚無有效觀測", "服務停止", "第一個格點前")


def excel_expect(rs, res):
    """依內部列（含真實資料狀態與來源）推算 Excel 每列應顯示的內容；N/A（沒有來源）→ 狀態 N/A、其餘空白。"""
    nm = RX.stage_names(res.stages)
    names = {s.no: RX._stage_title(nm, s) for s in res.stages}
    out = []
    for r in rs["rows"]:
        o = r["obs"]
        if o is None:
            out.append((r["time"], RX.NA) + (None,) * len(VKEYS) + (None,))
            continue
        pcs = RX.pcs_display(o, res.params)[0]
        v = tuple(pcs if k == "pcs_module_c" else RX._v(RX.field_value(o, k, res.params)) for k in VKEYS)
        out.append((r["time"], RX.status_text(o, names)) + v + (RX._raw1_note(r, res.params) or None,))
    return out


def excel_raw(path):
    """(標題, 資料列, 全部儲存格黑色?, 含技術文字的儲存格)。"""
    from openpyxl import load_workbook as _lw
    ws = _lw(path)["Raw Data"]
    cells = [c for r in ws.iter_rows() for c in r]
    black = all(c.font is not None and c.font.color is not None and str(c.font.color.rgb).upper().endswith("000000")
                for c in cells)
    tech = [c.value for c in cells if isinstance(c.value, str) and any(t in c.value for t in TECH)]
    return [c.value for c in ws[1]], [tuple(c.value for c in r) for r in ws.iter_rows(min_row=2)], black, tech


def at(rs, k):
    """第 k 秒（T0＋k）那一列；沒有 → None。"""
    t = T0 + timedelta(seconds=k)
    return next((r for r in rs["rows"] if r["time"] == t), None)


def V(res, r, key):
    return None if r is None or r["obs"] is None else RX.field_value(r["obs"], ENGINE.get(key, key), res.params)


def vals(res, r):
    return tuple(None if r["obs"] is None else RX.field_value(r["obs"], k, res.params) for k in VKEYS)


def phys(folder, ln):
    """observations.jsonl 第 ln 個實體行（1 起算）→ dict｜None。"""
    lines = open(os.path.join(folder, "observations.jsonl"), "rb").read().split(b"\n")
    try:
        return json.loads(lines[ln - 1].decode("utf-8"))
    except (IndexError, ValueError):
        return None


def invariants(rs, res, folder):
    """所有情境共用：每秒連續不重複；狀態合法；LIVE 來源＝實體行；非 LIVE 整筆＝較早某一 LIVE 列；年齡＝秒差或時鐘異常。"""
    bad = []
    rows = rs["rows"]
    one = timedelta(seconds=1)
    for a, b in zip(rows, rows[1:]):
        if b["time"] - a["time"] != one:
            bad.append(("不連續", a["time"], b["time"]))
    live = {}
    for r in rows:
        st = r["state"]
        if st not in RD.RAW_STATES:
            bad.append(("狀態", r["time"], st))
        if st == RD.RAW_LIVE:
            rec = phys(folder, r["src"])
            if r["obs"] is None or r["age"] != 0 or rec is None or rec.get("values") != r["obs"].values \
                    or rec.get("t") != r["obs"].t.strftime("%Y-%m-%dT%H:%M:%S"):
                bad.append(("LIVE 來源", r["time"], r["src"]))
            live[r["src"]] = r
        elif st == RD.RAW_NA:
            if r["obs"] is not None or r["src"] is not None or r["age"] is not None:
                bad.append(("N/A", r["time"]))
        else:
            src = live.get(r["src"])
            if src is None or r["obs"] is not src["obs"] or r["src_time"] != src["time"] or r["src_time"] >= r["time"]:
                bad.append(("整筆沿用", r["time"], r["src"]))
            exp = int((r["time"] - r["src_time"]).total_seconds())
            if not ((r["age"] == exp and r["clock_ok"]) or (r["age"] is None and not r["clock_ok"])):
                bad.append(("年齡", r["time"], r["age"], exp))
    return bad


INV = []


def inv(name, rs, res, folder):
    b = invariants(rs, res, folder) if rs else [("沒有 Raw Data 每秒列",)]
    INV.append((name, len(rs["rows"]) if rs else 0, b[:3]))
    return not b


def states(rs):
    c = {}
    for r in rs["rows"]:
        c[r["state"]] = c.get(r["state"], 0) + 1
    return c


ok_all = True
try:
    _install_sim_wall()

    # ------------------------------------------------------------------ T-4
    print("== T-4 格點：每個預定秒數一筆、跳格記 MISSED、每格最多一筆、不漂移 ==")
    durs = [0.3, 0.9, 1.2, 0.3, 2.5, 0.3, 0.3]

    def s_t4(i, t):
        if i == 7:
            return 0.2, RuntimeError("timeout")
        return (durs[i] if i < len(durs) else 0.3), reading()
    rt, root, cid, folder, pl = run_cert("t4", s_t4, 20)
    recs, _an = RQ.read_slot_log(os.path.join(folder, RQ.SLOT_LOG_FILE))
    slots = [r for r in recs if r["rec"] == RQ.REC_SLOT]
    idx = [r["slot_index"] for r in slots]
    st = {r["slot_index"]: r["slot_status"] for r in slots}
    start_rec = next(r for r in recs if r["rec"] == RQ.REC_SEGMENT_START)
    aw = datetime.fromisoformat(start_rec["anchor_wall"])
    drift_ok = all(abs((datetime.fromisoformat(r["scheduled_time"]) - aw).total_seconds() - r["slot_index"]) < 1e-6
                   for r in slots)
    check(f"T-4a 格點編號連續且不重複（0～{max(idx)}，共 {len(idx)} 筆）",
          idx == list(range(len(idx))) and len(set(idx)) == len(idx))
    check("T-4b 讀取 1.2 秒 → 下一格 MISSED（1 格）；2.5 秒 → MISSED 2 格；0.3／0.9 秒 → 不跳格",
          st[1] == RQ.ATTEMPTED_OK and st[2] == RQ.ATTEMPTED_OK and st[3] == RQ.MISSED and st[4] == RQ.ATTEMPTED_OK
          and st[5] == RQ.ATTEMPTED_OK and st[6] == RQ.MISSED and st[7] == RQ.MISSED and st[8] == RQ.ATTEMPTED_OK)
    check("T-4c 讀取例外 → ATTEMPTED_FAIL（有執行讀取但失敗），之後照常下一格",
          RQ.ATTEMPTED_FAIL in st.values() and st[max(k for k, v in st.items() if v == RQ.ATTEMPTED_FAIL) + 1]
          in (RQ.ATTEMPTED_OK, RQ.MISSED))
    check("T-4d 格點預定時間 ＝ 段落起點 ＋ k 秒（不累積漂移）", drift_ok)
    obs = jl(os.path.join(folder, "observations.jsonl"))
    check("T-4e 原始紀錄每筆帶格點識別；筆數 ＝ 有執行讀取的格點數；MISSED 不寫原始層",
          all("slot_index" in o for o in obs)
          and len(obs) == sum(1 for r in slots if r["slot_status"] != RQ.MISSED))
    ss = CR.build_sampling_slots(folder)
    check("T-4f Menu 22 結束：段落有 SEGMENT_END、最後一筆有格點；覆蓋率 100%（全部格點已保存）",
          any(r["rec"] == RQ.REC_SEGMENT_END for r in recs) and ss["metrics"]["coverage_text"] == "100%")
    reps = glob.glob(os.path.join(folder, "reports", "*.xlsx"))
    check("T-4g Menu 22 產生報告", len(reps) == 1)

    rs, res, ss = raw_of(rt, folder)
    check(f"T-4h Raw Data 每秒一列：{len(rs['rows'])} 列＝格點 {ss['metrics']['expected_slots']}；"
          f"跳格 → MISSED（{states(rs).get('MISSED', 0)}）；讀取例外 → CARRIED",
          inv("t4", rs, res, folder) and len(rs["rows"]) == ss["metrics"]["expected_slots"]
          and at(rs, 3)["state"] == RD.RAW_MISSED and at(rs, 6)["state"] == RD.RAW_MISSED
          and states(rs).get(RD.RAW_CARRIED, 0) >= 1)

    # ------------------------------------------------------------------ T-5／T-6
    print("\n== T-5 LIVE → CARRIED → LIVE（30／60／300 秒；方案 A 無時間上限）＋ T-6 N/A ==")
    for fail_s in (30, 60, 300):
        def s_t5(i, t, fail_s=fail_s):
            if 10 <= t < 10 + fail_s:
                return 0.3, None
            return 0.3, reading(soc=60.0 if t >= 10 + fail_s else 50.0)
        rt, root, cid, folder, pl = run_cert(f"t5_{fail_s}", s_t5, 20 + fail_s)
        rs, res, ss = raw_of(rt, folder)
        car = [r for r in rs["rows"] if r["state"] == RD.RAW_CARRIED]
        after = at(rs, 10 + fail_s)
        check(f"T-5 失敗 {fail_s} 秒：期間每秒 CARRIED（整筆＝失敗前第 9 秒那一筆、來源序號不變、SOC 50），"
              f"資料年齡 1～{max(r['age'] for r in car)} 秒（不設上限）；恢復後 LIVE（60）",
              inv(f"t5_{fail_s}", rs, res, folder) and len(car) == fail_s
              and len({r["src"] for r in car}) == 1 and car[0]["src"] == at(rs, 9)["src"]
              and all(V(res, r, "soc") == 50.0 for r in car)
              and [r["age"] for r in car] == list(range(1, fail_s + 1))
              and after["state"] == RD.RAW_LIVE and V(res, after, "soc") == 60.0)

    rt, root, cid, folder, pl = run_cert("t6", lambda i, t: (0.3, None), 10)
    rs, res, ss = raw_of(rt, folder)
    check("T-6 開始後一直讀不到：每秒一列、全部 N/A，數值空白（沒有觀測，不是 0）",
          inv("t6", rs, res, folder) and rs["rows"] and all(r["state"] == RD.RAW_NA and r["obs"] is None
                                                            for r in rs["rows"]))

    # ------------------------------------------------------------------ A
    print("\n== A 整筆沿用：任一必要欄位缺值 → 整列沿用上一筆有效觀測（不逐欄混用不同時間）==")

    def s_a(i, t):
        soc = 40.0 + math.floor(t)                                            # 每秒不同，可辨識來源
        if 5 <= t < 6:
            return 0.3, reading(soc=soc, v=None)                                # A-1 只有電壓缺值
        if 8 <= t < 9:
            return 0.3, reading(fail=["/overview/rackExtremeValueInformation"], soc=soc, rmax=None, rmin=None,
                                cmax=None, cmin=None)                           # A-2 Rack 端點失敗
        if 11 <= t < 12:
            return 0.3, reading(soc=soc, i=None)                                # A-3 電流缺值
        if 12 <= t < 13:
            return 0.3, reading(p=None, soc=0.0)                                # A-3 功率缺值（SOC 0）
        if 14 <= t < 15:
            return 0.3, reading(soc=soc, p="N/A", v=float("nan"))               # A-5 "N/A"、NaN 缺值
        if 16 <= t < 17:
            return 0.3, reading(soc=0.0, rmin=-5.0)                             # A-5 SOC 0、負數（Rack 最低溫）→ 有效
        return 0.3, reading(soc=soc)
    rt, root, cid, folder, pl = run_cert("a", s_a, 20)
    rs, res, ss = raw_of(rt, folder)
    r4, r5, r7, r8, r10, r11, r12 = (at(rs, k) for k in (4, 5, 7, 8, 10, 11, 12))
    check("A-1 只有電壓缺值：整列 CARRIED＝第 4 秒那一筆（SOC 44、電壓 950，10 欄全部相同、來源序號相同），"
          "不採用本秒的 SOC 45",
          inv("a", rs, res, folder) and r5["state"] == RD.RAW_CARRIED and r5["src"] == r4["src"]
          and vals(res, r5) == vals(res, r4) and V(res, r5, "soc") == 44.0 and r5["age"] == 1)
    check("A-2 Rack 端點失敗（格點 PARTIAL）：整列 CARRIED＝第 7 秒那一筆（Rack／Cell 與 SOC 都來自同一筆）",
          r8["state"] == RD.RAW_CARRIED and r8["slot_status"] == RQ.ATTEMPTED_PARTIAL and r8["src"] == r7["src"]
          and vals(res, r8) == vals(res, r7))
    check("A-3 電流、功率在相鄰兩秒各自缺值：第 11、12 秒都整列沿用第 10 秒（SOC 50，不是第 12 秒的 0）、年齡 1／2 秒",
          r11["src"] == r10["src"] == r12["src"] and V(res, r12, "soc") == 50.0 and (r11["age"], r12["age"]) == (1, 2))
    r13, r14, r16 = at(rs, 13), at(rs, 14), at(rs, 16)
    check("A-5 \"N/A\"／NaN → 該筆無效 → 整列沿用第 13 秒；SOC 0 與負數是有效值 → 第 16 秒 LIVE（SOC 0、Rack 最低溫 −5）",
          r14["state"] == RD.RAW_CARRIED and r14["src"] == r13["src"]
          and r16["state"] == RD.RAW_LIVE and V(res, r16, "soc") == 0.0 and V(res, r16, "rmin") == -5.0)

    def s_a4(i, t):
        return 0.3, reading(cmax=None)
    rt, root, cid, folder, pl = run_cert("a4", s_a4, 8)
    rs, res, ss = raw_of(rt, folder)
    check("A-4 Cell 最大電壓從開始就沒有值（必要欄位）：沒有任何有效觀測 → 全部 N/A，不顯示其他欄位的部分數值",
          inv("a4", rs, res, folder) and rs["rows"] and all(r["state"] == RD.RAW_NA for r in rs["rows"]))

    # ------------------------------------------------------------------ C
    print("\n== C 覆蓋率與實際成功率分開（CARRIED 不算成功；M-1 分母＝預定格點）==")

    def s_c1(i, t):
        return 0.3, (None if i % 10 in (1, 4, 7) else reading())
    rt, root, cid, folder, pl = run_cert("c1", s_c1, 40)
    m = CR.build_sampling_slots(folder)["metrics"]
    check(f"C-1 30% 格點讀取失敗：覆蓋率 {m['coverage_text']}；M-1 ＝ {m['m1_pct']}%（約 70%）；讀取失敗 {m['read_fail']} 格",
          m["coverage_text"] == "100%" and 65 <= m["m1_pct"] <= 75 and m["read_fail"] >= 10)

    def s_c2(i, t):
        return 0.3, (reading(rmax=None) if i % 2 else reading())
    rt, root, cid, folder, pl = run_cert("c2", s_c2, 20)
    m = CR.build_sampling_slots(folder)["metrics"]
    check(f"C-2 單欄 CARRIED 的格點不算完整即時資料：完整即時 {m['complete_live_pct']}%（約 50%），M-1 {m['m1_pct']}%（約 100%）",
          40 <= m["complete_live_pct"] <= 60 and m["m1_pct"] >= 95)

    def s_c3(i, t):
        return (1.5 if i % 2 else 0.3), reading()
    rt, root, cid, folder, pl = run_cert("c3", s_c3, 30)
    m = CR.build_sampling_slots(folder)["metrics"]
    check(f"C-3 M-1 分母＝預定格點（含 MISSED）：預定 {m['expected_slots']}、MISSED {m['missed']}、"
          f"M-1 {m['m1_pct']}% ＝ 成功 {m['hmi_success_slots']} ÷ 預定",
          m["missed"] > 0 and m["m1_pct"] == round(100.0 * m["hmi_success_slots"] / m["expected_slots"], 2)
          and m["m1_pct"] < 100 and m["coverage_text"] == "100%")

    # ------------------------------------------------------------------ B／T-9
    print("\n== B 服務重啟：停止期間補秒（SERVICE_STOPPED，整筆沿用、年齡累加）／三種情況分開／未正常結束 ==")

    def run_b1(name):
        SIM.t, SIM.off = 0.0, 0.0
        root = os.path.join(TMP, name)
        pl = Plant(lambda i, t: (0.3, reading(soc=50.0) if t < 15 else (None if t < 80 else reading(soc=70.0))))
        rt = new_rt(root, pl)
        cid = start(rt, root)
        rt._slot_sample_loop(SimStop(rt, root, stop_at=15))              # 服務停止（認證仍進行中）
        SIM.t += 60.0                                                     # 停機 60 秒
        rt2 = new_rt(root, pl)
        rt2.startup()
        rt2._slot_sample_loop(SimStop(rt2, root, stop_at=110, end_at=100))
        return rt2, root, cid, os.path.join(root, cid)
    rt, root, cid, folder = run_b1("b1")
    rs, res, ss = raw_of(rt, folder)
    gap = [r for r in rs["rows"] if r["state"] in (RD.RAW_SERVICE_STOPPED, RD.RAW_NO_SLOT)]
    stopped = [r for r in gap if r["state"] == RD.RAW_SERVICE_STOPPED]
    last1 = at(rs, 14)
    check(f"B-1 停機 60 秒：每秒補一列（{len(gap)} 列），有中斷證據（restart＋interruptions）→ 全部 SERVICE_STOPPED；"
          "整筆沿用停機前最後一筆、年齡逐秒累加；段落 2 從格點 0 起算",
          inv("b1", rs, res, folder) and 59 <= len(gap) <= 61 and len(stopped) == len(gap)
          and all(r["src"] == last1["src"] and V(res, r, "soc") == 50.0 for r in gap)
          and [r["age"] for r in gap] == list(range(1, len(gap) + 1))
          and ss["metrics"]["service_down_count"] == 1
          and len(rs["rows"]) == ss["metrics"]["expected_slots"] + len(gap))
    first2 = rs["rows"][rs["rows"].index(gap[-1]) + 1]
    check(f"T-9 重啟後讀取失敗：沿用停機前的有效值（SOC 50），資料年齡跨段落不歸零（{first2['age']} 秒 ≥ 60）",
          first2["state"] == RD.RAW_CARRIED and V(res, first2, "soc") == 50.0 and first2["age"] >= 60
          and first2["age"] == gap[-1]["age"] + 1)
    st_b = {r["state"] for r in rs["rows"]}
    same_note = all(RX._raw1_note(r, res.params) == RX._raw1_note(last1, res.params)
                    for r in rs["rows"] if r["src"] == last1["src"])
    check("B-2 內部狀態仍分開（讀取失敗＝CARRIED、服務停止＝SERVICE_STOPPED）；Excel 說明＝來源列本身的說明，"
          "不含沿用／服務停止等技術文字",
          {RD.RAW_CARRIED, RD.RAW_SERVICE_STOPPED} <= st_b and same_note
          and not any(t in RX._raw1_note(r, res.params) for r in rs["rows"] for t in TECH))

    def s_c3b(i, t):
        return (1.5 if i % 3 == 0 else 0.3), reading()
    rt, root, cid, folder, pl = run_cert("b2", s_c3b, 15)
    rs, res, ss = raw_of(rt, folder)
    mis = [r for r in rs["rows"] if r["state"] == RD.RAW_MISSED]
    check("B-2b 跳格 → 內部 MISSED（與 CARRIED／SERVICE_STOPPED 不同）；Excel 說明不顯示「跳格」等技術文字",
          inv("b2", rs, res, folder) and mis and all(r["slot_status"] == RQ.MISSED for r in mis)
          and not any(t in RX._raw1_note(r, res.params) for r in mis for t in TECH))

    SIM.t, SIM.off = 0.0, 0.0
    root = os.path.join(TMP, "b3")
    pl = Plant(lambda i, t: (0.3, reading(soc=40.0 + math.floor(t))))
    rt = new_rt(root, pl)
    cid = start(rt, root)
    rt._slot_seg_end = lambda reason: setattr(rt, "_slot_seg", None)  # 模擬當機：段落沒有寫 SEGMENT_END
    rt._slot_sample_loop(SimStop(rt, root, stop_at=10))
    with open(os.path.join(root, cid, "observations.jsonl"), "ab") as f:
        f.write(b'{"t": "2026-11-11T10:00:1')                           # 當機留下半行（沒有換行）
    SIM.t += 30.0
    rt2 = new_rt(root, pl)
    rt2.startup()
    rt2._slot_sample_loop(SimStop(rt2, root, stop_at=50, end_at=45))
    b3 = os.path.join(root, cid)
    rs, res, ss = raw_of(rt2, b3)
    check(f"B-3 當機（沒有 SEGMENT_END）：覆蓋率標示不確定（{ss['metrics']['coverage_text'][:12]}…）；"
          "重啟有證據 → 停機秒數 SERVICE_STOPPED；Raw Data 仍每秒連續",
          ss["metrics"]["coverage_text"].startswith("不確定") and not ss["metrics"]["coverage_certain"]
          and inv("b3", rs, res, b3) and states(rs).get(RD.RAW_SERVICE_STOPPED, 0) >= 25)
    seg2_live = [r for r in rs["rows"] if r["state"] == RD.RAW_LIVE and r["time"] >= T0 + timedelta(seconds=40)]
    check("SRC-1 原始檔有半行（分析端略過該行、索引錯位）：來源序號仍＝observations.jsonl 實體行號，"
          "且該行內容＝本列數值（SOC＝秒數＋40）",
          seg2_live and all(phys(b3, r["src"])["values"]["soc"] == V(res, r, "soc")
                            == 40.0 + (r["time"] - T0).total_seconds() for r in seg2_live)
          and any(r["src"] != res.obs.index(r["obs"]) + 1 for r in seg2_live))

    # ------------------------------------------------------------------ N：沒有中斷證據 → NO_SLOT
    print("\n== N 沒有明確中斷證據時不推定服務停止（NO_SLOT）==")
    b1_root = os.path.join(TMP, "b1")
    b1_cid = [d for d in os.listdir(b1_root) if d.startswith("CERT_")][0]

    def variant_b1(name, fn):
        d = os.path.join(TMP, "n_" + name)
        shutil.copytree(os.path.join(b1_root, b1_cid), d)
        fn(d)
        return raw_of(rt, d) + (d,)

    def edit_state(d, f):
        p = os.path.join(d, "cert_state.json")
        s = json.load(open(p, encoding="utf-8"))
        f(s)
        json.dump(s, open(p, "w", encoding="utf-8"), ensure_ascii=False)

    def edit_seg2_reason(d, reason):
        p = os.path.join(d, RQ.SLOT_LOG_FILE)
        recs = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
        starts = [r for r in recs if r.get("rec") == RQ.REC_SEGMENT_START]
        starts[1]["reason"] = reason
        open(p, "w", encoding="utf-8").write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))

    def gap_states(rs):
        return {r["state"] for r in rs["rows"] if r["state"] in (RD.RAW_SERVICE_STOPPED, RD.RAW_NO_SLOT)}
    res_n = {}
    res_n["no_intr"] = variant_b1("no_intr", lambda d: edit_state(d, lambda s: s.update(interruptions=[])))
    res_n["resume"] = variant_b1("resume", lambda d: edit_seg2_reason(d, "resume"))
    res_n["rev"] = variant_b1("rev", lambda d: edit_state(d, lambda s: s["interruptions"][0].update(
        {"from": s["interruptions"][0]["to"], "to": s["interruptions"][0]["from"]})))
    res_n["outside"] = variant_b1("outside", lambda d: edit_state(d, lambda s: s["interruptions"][0].update(
        {"to": "2026-11-11T09:00:00", "from": "2026-11-11T08:59:00"})))
    check("N-1 沒有 interruptions 紀錄（只有 restart）→ 空檔全部 NO_SLOT",
          gap_states(res_n["no_intr"][0]) == {RD.RAW_NO_SLOT} and inv("n1", *res_n["no_intr"][:2], res_n["no_intr"][3]))
    check("N-2 段落原因不是 restart（resume）→ 空檔全部 NO_SLOT（不推定服務停止）",
          gap_states(res_n["resume"][0]) == {RD.RAW_NO_SLOT})
    check("N-3 interruptions 的 from 晚於 to（時鐘異常）→ 不採用為證據 → NO_SLOT",
          gap_states(res_n["rev"][0]) == {RD.RAW_NO_SLOT})
    check("N-4 interruptions 的時間不在空檔內 → NO_SLOT",
          gap_states(res_n["outside"][0]) == {RD.RAW_NO_SLOT})
    res_n["part"] = variant_b1("part", lambda d: edit_state(d, lambda s: s["interruptions"][0].update(
        {"from": "2026-11-11T10:00:44"})))
    rs5 = res_n["part"][0]
    g5 = [r for r in rs5["rows"] if r["state"] in (RD.RAW_SERVICE_STOPPED, RD.RAW_NO_SLOT)]
    check("N-5 證據只涵蓋部分空檔（from＝10:00:44）：from 之前（含）NO_SLOT、之後 SERVICE_STOPPED；兩種都整筆沿用",
          all(r["state"] == (RD.RAW_NO_SLOT if r["time"] <= T0 + timedelta(seconds=44) else RD.RAW_SERVICE_STOPPED)
              for r in g5) and {r["state"] for r in g5} == {RD.RAW_NO_SLOT, RD.RAW_SERVICE_STOPPED}
          and inv("n5", rs5, res_n["part"][1], res_n["part"][3]))

    # ------------------------------------------------------------------ CR：時鐘回撥
    print("\n== CR 時鐘回撥／往前跳／段落內調整 ==")

    def run_two(name, script, stop1, down, off, stop2, end2):
        SIM.t, SIM.off = 0.0, 0.0
        root = os.path.join(TMP, name)
        pl = Plant(script)
        rt = new_rt(root, pl)
        cid = start(rt, root)
        rt._slot_sample_loop(SimStop(rt, root, stop_at=stop1))
        SIM.t += down
        SIM.off = off
        rt2 = new_rt(root, pl)
        rt2.startup()
        rt2._slot_sample_loop(SimStop(rt2, root, stop_at=stop2, end_at=end2))
        return rt2, os.path.join(root, cid)

    def s_cr(i, t):
        w = t + SIM.off                                                   # 牆鐘秒
        if t < 15:
            return 0.3, reading(soc=50.0)
        if w < 15:
            return 0.3, reading(soc=80.0)                                 # 回撥重疊期間的觀測（不得採用）
        if w < 16:
            return 0.3, None                                              # 回撥結束後第一格讀取失敗
        if w < 17:
            return 2.5, reading(soc=91.0)                                 # 耗時 2.5 秒 → 之後兩格 MISSED
        return 0.3, reading(soc=92.0)
    rt, cr1 = run_two("cr1", s_cr, 15, 3.0, -10.0, 45, 40)
    rs, res, ss = raw_of(rt, cr1)
    st80 = {i + 1 for i, o in enumerate(jl(os.path.join(cr1, "observations.jsonl"))) if o["values"].get("soc") == 80.0}
    check(f"CR-1 重啟時時鐘回撥 10 秒：重疊 {rs['stats']['overlap_slots']} 個格點不產生列；全段秒數嚴格遞增、不重複",
          inv("cr1", rs, res, cr1) and rs["stats"]["overlap_slots"] == 7 and st80
          and len({r["time"] for r in rs["rows"]}) == len(rs["rows"]))
    check("CR-2 重疊期間的觀測（SOC 80）不會成為 LIVE，也不會成為任何一列的來源",
          all(V(res, r, "soc") != 80.0 for r in rs["rows"]) and not ({r["src"] for r in rs["rows"]} & st80))
    r15, r16, r17, r18, r19 = (at(rs, k) for k in (15, 16, 17, 18, 19))
    check("CR-3 回撥結束後第一格讀取失敗：沿用回撥前的觀測（SOC 50，不是重疊期間的 80）；"
          "跨回撥的資料年齡無法正確計算 → 內部年齡為 None（時鐘異常，計入描述統計）；Excel 不顯示年齡",
          r15["state"] == RD.RAW_CARRIED and V(res, r15, "soc") == 50.0 and r15["src"] == at(rs, 14)["src"]
          and r15["age"] is None and not r15["clock_ok"] and "時鐘異常" not in RX._raw1_note(r15, res.params))
    check("CR-4 回撥後 MISSED（F-1）：第 16 秒 LIVE 91（讀取 2.5 秒），第 17、18 秒 MISSED 仍沿用 50（不使用尚未完成的 91）；"
          "第 19 秒 LIVE 92",
          r16["state"] == RD.RAW_LIVE and V(res, r16, "soc") == 91.0
          and r17["state"] == r18["state"] == RD.RAW_MISSED and V(res, r17, "soc") == V(res, r18, "soc") == 50.0
          and r19["state"] == RD.RAW_LIVE and V(res, r19, "soc") == 92.0)
    check("CR-8 回撥造成 interruptions 的 from 晚於 to：不視為中斷證據（本例沒有 SERVICE_STOPPED 列）",
          states(rs).get(RD.RAW_SERVICE_STOPPED, 0) == 0
          and json.load(open(os.path.join(cr1, "cert_state.json"), encoding="utf-8"))["interruptions"][0]["from"]
          > json.load(open(os.path.join(cr1, "cert_state.json"), encoding="utf-8"))["interruptions"][0]["to"])

    # CR-5：整個新段落都落在重疊範圍內
    SIM.t, SIM.off = 0.0, 0.0
    root = os.path.join(TMP, "cr5")
    pl = Plant(lambda i, t: (0.3, reading(soc=50.0) if t < 15 else (reading(soc=80.0) if t < 22 else reading(soc=60.0))))
    rt = new_rt(root, pl)
    cid = start(rt, root)
    rt._slot_sample_loop(SimStop(rt, root, stop_at=15))
    SIM.t += 2.0
    SIM.off = -10.0                                                       # 段落 2：牆鐘 10:00:07～10:00:11（全部重疊）
    rt2 = new_rt(root, pl)
    rt2.startup()
    rt2._slot_sample_loop(SimStop(rt2, root, stop_at=22))
    SIM.t += 3.0                                                          # 段落 3：牆鐘 10:00:15 起
    rt3 = new_rt(root, pl)
    rt3.startup()
    rt3._slot_sample_loop(SimStop(rt3, root, stop_at=40, end_at=35))
    cr5 = os.path.join(root, cid)
    rs, res, ss = raw_of(rt3, cr5)
    check(f"CR-5 新段落整段落在重疊範圍內（{rs['stats']['overlap_slots']} 格）：整段不產生列、其觀測不採用；"
          "下一段正常接續（LIVE 60）",
          inv("cr5", rs, res, cr5) and rs["stats"]["overlap_slots"] >= 4 and ss["metrics"]["segments"] == 3
          and all(V(res, r, "soc") != 80.0 for r in rs["rows"]) and at(rs, 16)["state"] == RD.RAW_LIVE
          and V(res, at(rs, 16), "soc") == 60.0)

    def s_cr6(i, t):
        return 0.3, reading(soc=50.0 if t < 15 else 70.0)
    rt, cr6 = run_two("cr6", s_cr6, 15, 3.0, 100.0, 140, 135)
    rs, res, ss = raw_of(rt, cr6)
    g6 = [r for r in rs["rows"] if r["state"] in (RD.RAW_SERVICE_STOPPED, RD.RAW_NO_SLOT)]
    intr = json.load(open(os.path.join(cr6, "cert_state.json"), encoding="utf-8"))["interruptions"][0]
    t_from, t_to = datetime.fromisoformat(intr["from"]), datetime.fromisoformat(intr["to"])
    check(f"CR-6 重啟時時鐘往前跳 100 秒：空檔 {len(g6)} 秒；中斷紀錄範圍內 SERVICE_STOPPED、範圍外 NO_SLOT；"
          "整筆沿用、秒數連續（年齡以牆鐘計算，屬已知限制）",
          inv("cr6", rs, res, cr6) and len(g6) >= 100
          and all(r["state"] == (RD.RAW_SERVICE_STOPPED if t_from < r["time"] <= t_to else RD.RAW_NO_SLOT) for r in g6)
          and states(rs).get(RD.RAW_SERVICE_STOPPED, 0) >= 100)

    SIM.t, SIM.off = 0.0, 0.0
    root = os.path.join(TMP, "cr7")
    pl = Plant(lambda i, t: (0.3, reading(soc=40.0 + i)))
    rt = new_rt(root, pl)
    cid = start(rt, root)

    class ShiftStop(SimStop):
        def wait(self, timeout=None):
            r = super().wait(timeout)
            if 10 <= SIM.t < 11:
                SIM.off = -5.0                                            # 同一段落內，電腦時鐘被往回調 5 秒
            return r
    rt._slot_sample_loop(ShiftStop(rt, root, stop_at=25, end_at=20))
    cr7 = os.path.join(root, cid)
    rs, res, ss = raw_of(rt, cr7)
    obs7 = jl(os.path.join(cr7, "observations.jsonl"))
    back = any(a["t"] > b["t"] for a, b in zip(obs7, obs7[1:]))
    check("CR-7 同一段落內電腦時鐘往回調 5 秒（原始紀錄時間倒退）：格點時間由單調時鐘推算 → Raw Data 秒數仍連續、"
          "全部 LIVE（年齡 0）、沒有重疊",
          back and inv("cr7", rs, res, cr7) and rs["stats"]["overlap_slots"] == 0
          and all(r["state"] == RD.RAW_LIVE for r in rs["rows"][:-1]))

    # CR-9：報告描述的統計與實際相符
    from openpyxl import load_workbook
    rt_c, cr1b = rt, cr1
    rsx, resx, ssx = raw_of(rt, cr1)
    px = os.path.join(TMP, "cr9.xlsx")
    RX.save_report(resx, {"conditions": ["x"], "source_notes": ["n"], "cabinet_spec": "250KW", "cycles": None,
                          "commands": [], "generated_at": "2026-11-11 12:00:00", "sampling_slots": ssx}, px)
    desc = load_workbook(px).properties.description or ""
    c9 = states(rsx)
    check("CR-9 報告描述：每秒列數、各資料狀態列數、時鐘回撥重疊格點數、年齡無法計算的列數都與實際相符",
          f"共 {len(rsx['rows'])} 列" in desc and f"時鐘回撥：{rsx['stats']['overlap_slots']} 個格點" in desc
          and all(f"{k} {c9.get(k, 0)}" in desc for k in RD.RAW_STATES)
          and f"資料年齡無法計算（時鐘異常）{rsx['stats']['age_unknown']} 列" in desc)

    # ------------------------------------------------------------------ SE：認證開始／結束邊界
    print("\n== SE 認證開始／結束邊界 ==")
    SIM.t, SIM.off = 0.0, 0.0
    root = os.path.join(TMP, "se1")
    pl = Plant(lambda i, t: (0.3, reading()))
    rt = new_rt(root, pl)
    cid = start(rt, root)                                                 # started_at＝10:00:00
    SIM.t = 2.4                                                           # 第一個格點在 10:00:02.4
    rt._slot_sample_loop(SimStop(rt, root, stop_at=15, end_at=12))
    se1 = os.path.join(root, cid)
    rs, res, ss = raw_of(rt, se1)
    pre = [r for r in rs["rows"] if r["time"] < T0 + timedelta(seconds=2)]
    check("SE-1 認證開始到第一個格點之間（10:00:00、10:00:01）：補 N/A 列（沒有來源 → 不虛構數值、說明空白），"
          "沒有未顯示的秒數；第一個格點那一秒起 LIVE",
          inv("se1", rs, res, se1) and rs["rows"][0]["time"] == T0 and len(pre) == 2
          and all(r["state"] == RD.RAW_NA and r["obs"] is None and RX._raw1_note(r, res.params) == "" for r in pre)
          and at(rs, 2)["state"] == RD.RAW_LIVE and rs["stats"]["pre_rows"] == 2)
    p_se1 = os.path.join(TMP, "se1.xlsx")
    RX.save_report(res, {"conditions": ["x"], "source_notes": ["n"], "cabinet_spec": "250KW", "cycles": None,
                         "commands": [], "generated_at": "2026-11-11 12:00:00", "sampling_slots": ss}, p_se1)
    h7, rows7, black7, tech7 = excel_raw(p_se1)
    check("V-7 沒有有效來源的秒數（開始到第一個格點前）：Excel 顯示時間＋狀態 N/A，10 個數值與說明全部空白（不虛構）；"
          "之後的列有數值；全部黑色、無技術標記",
          rows7[0] == (T0, RX.NA) + (None,) * 11 and rows7[1] == (T0 + timedelta(seconds=1), RX.NA) + (None,) * 11
          and rows7[2][2] is not None and black7 and not tech7 and rows7 == excel_expect(rs, res))
    slots_se1 = [r for r in RQ.read_slot_log(os.path.join(se1, RQ.SLOT_LOG_FILE))[0] if r["rec"] == RQ.REC_SLOT]
    last_slot = max(datetime.fromisoformat(r["scheduled_time"]).replace(tzinfo=None, microsecond=0) for r in slots_se1)
    st_se1 = json.load(open(os.path.join(se1, "cert_state.json"), encoding="utf-8"))
    check(f"SE-3 結束邊界：最後一列＝最後一個已知格點（{last_slot:%H:%M:%S}；含 Menu 22 結束取樣），"
          f"不補到 ended_at 之後（ended_at {st_se1['ended_at'][11:]}）",
          rs["rows"][-1]["time"] == last_slot and all(r["time"] <= last_slot for r in rs["rows"]))

    rt, root, cid, folder, pl = run_cert("se2", lambda i, t: (0.3, reading()), 6)
    rs, res, ss = raw_of(rt, folder)
    check("SE-2 開始時間與第一個格點同一秒：不補 N/A 列，第一列就是第一個格點（LIVE）",
          inv("se2", rs, res, folder) and rs["stats"]["pre_rows"] == 0 and rs["rows"][0]["time"] == T0
          and rs["rows"][0]["state"] == RD.RAW_LIVE)

    rt, root, cid, folder, pl = run_cert("se4", lambda i, t: (0.3, reading()), 1)
    rs, res, ss = raw_of(rt, folder)
    check(f"SE-4 只有極少格點（{ss['metrics']['expected_slots']} 格）：列數＝格點數、連續",
          inv("se4", rs, res, folder) and 1 <= len(rs["rows"]) == ss["metrics"]["expected_slots"] <= 3)

    SIM.t, SIM.off = 0.0, 0.0
    root = os.path.join(TMP, "se5")
    rt = new_rt(root, Plant(lambda i, t: (0.3, reading())))
    cid = start(rt, root)
    rt._sample_cycle = lambda *a, **k: None                               # 結束時不取樣 → 沒有任何格點
    SIM.t = 3.0
    REQ.new_request(root, "end_cert", cert_id=cid, now=SIM.clock())
    rt.process_requests()
    se5 = os.path.join(root, cid)
    rep5 = glob.glob(os.path.join(se5, "reports", "*.xlsx"))
    head5 = [c.value for c in load_workbook(rep5[0])["Raw Data"][1]] if rep5 else []
    check("SE-5 結束時尚未產生任何格點（沒有 slots.jsonl）→ 報告照常產生，Raw Data 維持原格式（只列實際觀測）",
          CR.build_sampling_slots(se5) is None and rep5 and head5 == RX.RAW_HEADERS)

    se6 = os.path.join(TMP, "se6")
    shutil.copytree(os.path.join(TMP, "se2", [d for d in os.listdir(os.path.join(TMP, "se2"))
                                              if d.startswith("CERT_")][0]), se6)
    p6 = os.path.join(se6, RQ.SLOT_LOG_FILE)
    recs6 = [json.loads(x) for x in open(p6, encoding="utf-8") if x.strip()]
    for r in recs6:
        if r.get("rec") == RQ.REC_SEGMENT_START:
            r["interval_sec"] = 5
    open(p6, "w", encoding="utf-8").write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs6))
    rs6, res6, ss6 = raw_of(rt, se6)
    p6x = os.path.join(TMP, "se6.xlsx")
    RX.save_report(res6, {"conditions": ["x"], "source_notes": ["n"], "cabinet_spec": "250KW", "cycles": None,
                          "commands": [], "generated_at": "2026-11-11 12:00:00", "sampling_slots": ss6}, p6x)
    w6 = load_workbook(p6x)
    check("SE-6 格點間隔不是 1 秒：不產生每秒列（不把 5 秒格點補成每秒），Raw Data 維持原格式並在描述註記",
          rs6 is None and [c.value for c in w6["Raw Data"][1]] == RX.RAW_HEADERS
          and "不是全部 1 秒間隔" in (w6.properties.description or ""))

    # ------------------------------------------------------------------ D
    print("\n== D 寫入失敗與壞行：不得宣稱 100% ==")
    real_append = CR._append_jsonl
    state = {"n": 0, "fail_at": None}

    def flaky_append(path, rec):
        if path.endswith("observations.jsonl"):
            state["n"] += 1
            if state["n"] == state["fail_at"]:
                raise OSError("disk full (simulated)")
        return real_append(path, rec)

    def s_d1(i, t):
        return 0.3, reading(soc=77.0 if i == 5 else 50.0)            # 第 6 次讀取（寫入失敗）的值從未保存
    CR._append_jsonl = flaky_append
    state.update(n=0, fail_at=6)
    try:
        rt, root, cid, folder, pl = run_cert("d1", s_d1, 15)
    finally:
        CR._append_jsonl = real_append
    rs, res, ss = raw_of(rt, folder)
    m = ss["metrics"]
    bad = [r for r in rs["rows"] if r["slot_status"] == RQ.ATTEMPTED_FAIL]
    check("D-1 原始紀錄寫入失敗：取樣不中斷（之後格點照常）；SLOT 標記 obs_write_failed；不算 HMI 成功",
          m["obs_write_failed"] == 1 and len(bad) == 1 and rs["rows"][-1]["state"] == RD.RAW_LIVE
          and m["hmi_success_slots"] == m["expected_slots"] - 1)
    check("D-1b 未保存的讀值（SOC 77）不會出現：該秒 CARRIED（整筆沿用已保存的上一筆 50），之後也沒有 77",
          inv("d1", rs, res, folder) and all(V(res, r, "soc") != 77.0 for r in rs["rows"])
          and bad[0]["state"] == RD.RAW_CARRIED and V(res, bad[0], "soc") == 50.0)

    real_seg_append = RQ.SlotSegment._append
    lose = {"k": set()}

    def lossy(self, rec):
        if rec.get("rec") == RQ.REC_SLOT and rec.get("slot_index") in lose["k"]:
            self.write_errors += 1
            return False                                            # 模擬 slots.jsonl 寫入失敗（沒有寫入）
        return real_seg_append(self, rec)
    RQ.SlotSegment._append = lossy
    lose["k"] = {4}
    try:
        rt, root, cid, folder, pl = run_cert("d2", lambda i, t: (0.3, reading()), 12)
    finally:
        RQ.SlotSegment._append = real_seg_append
    m = CR.build_sampling_slots(folder)["metrics"]
    check(f"D-2 slots.jsonl 寫入失敗（原始紀錄有）：依預定格點重建為 SLOT_LOST（{m['slot_lost']}）；覆蓋率 {m['coverage_text'][:9]}… < 100%",
          m["slot_lost"] == 1 and m["coverage_text"] != "100%" and m["logged_slots"] == m["expected_slots"] - 1)

    CR._append_jsonl = flaky_append
    RQ.SlotSegment._append = lossy
    state.update(n=0, fail_at=5)
    lose["k"] = {4}
    try:
        rt, root, cid, folder, pl = run_cert("d3", lambda i, t: (0.3, reading()), 12)
    finally:
        CR._append_jsonl = real_append
        RQ.SlotSegment._append = real_seg_append
    m = CR.build_sampling_slots(folder)["metrics"]
    check(f"D-3 兩者都寫入失敗：依前後格點重建為 INFERRED_MISSED（{m['inferred_missed']}）；覆蓋率 < 100%",
          m["inferred_missed"] == 1 and m["coverage_text"] != "100%")

    rt, root, cid, folder, pl = run_cert("d4", lambda i, t: (0.3, reading()), 8)
    with open(os.path.join(folder, "observations.jsonl"), "a", encoding="utf-8") as f:
        f.write('{"t": "2026-11-11T10:00:0\n')                     # 模擬壞行
    m = CR.build_sampling_slots(folder)["metrics"]
    check(f"D-4 原始檔有壞行：異常數 {m['raw_unparseable']}；不顯示「100%」（{m['coverage_text'][:10]}…）",
          m["raw_unparseable"] >= 1 and m["coverage_text"] != "100%")

    # ------------------------------------------------------------------ T-7／T-11
    print("\n== T-7 補值隔離／T-11 Excel ==")
    rt, root, cid, folder, pl = run_cert("t7", lambda i, t: (0.3, None if 25 <= t < 32 else reading(soc=50.0 + t / 100.0)), 60)
    obs = jl(os.path.join(folder, "observations.jsonl"))
    fails = [o for o in obs if not o.get("hmi_ok")]
    check("T-7a observations.jsonl 只有實際讀值：讀取失敗的筆數值全部是 None（沒有沿用值）",
          fails and all(all(o["values"].get(ENGINE[f]) is None for f in ENGINE) for o in fails))
    from cert_report_xlsx import save_report
    rs, res, ss = raw_of(rt, folder)
    meta = {"conditions": ["x"], "source_notes": ["n"], "cabinet_spec": "250KW", "cycles": None, "commands": [],
            "generated_at": "2026-11-11 12:00:00"}
    pa, pb = os.path.join(TMP, "with.xlsx"), os.path.join(TMP, "without.xlsx")
    save_report(res, dict(meta, sampling_slots=ss), pa)
    save_report(res, dict(meta), pb)
    wa, wb_ = load_workbook(pa), load_workbook(pb)
    same = wa.sheetnames == wb_.sheetnames
    for n in wb_.sheetnames:
        if n == "Raw Data":
            continue
        a_vals = [[c for c in r] for r in wa[n].iter_rows(values_only=True)]
        b_vals = [[c for c in r] for r in wb_[n].iter_rows(values_only=True)]
        same = same and a_vals == b_vals and wa[n].merged_cells.ranges == wb_[n].merged_cells.ranges
    check(f"T-11a 不再有 Sampling Slots 工作表；Raw Data 以外的工作表（{len(wb_.sheetnames) - 1} 頁）逐格相同",
          same and "Sampling Slots" not in wa.sheetnames and wa.sheetnames[-1] == "Raw Data")
    ws = wa["Raw Data"]
    head = [c.value for c in ws[1]]
    data = list(ws.iter_rows(min_row=2, values_only=True))
    C = {h: i for i, h in enumerate(head)}
    times = [r[C["時間"]] for r in data]
    head_x, rows_x, black_x, tech_x = excel_raw(pa)
    check(f"T-11b／V-1／V-3／V-5 Raw Data：正好 13 欄（時間／狀態／10 個數值／說明）；每秒一列（{len(data)} 列）、"
          "連續不重複；表頭與全部資料格黑色 #000000；描述保留每秒列摘要與各狀態秒數",
          head == RX.RAW1_HEADERS and len(head) == 13 and head[:2] == ["時間", "狀態"] and head[-1] == "說明"
          and len(data) == len(rs["rows"]) == ss["metrics"]["expected_slots"]
          and all((b - a) == timedelta(seconds=1) for a, b in zip(times, times[1:])) and black_x
          and "Raw Data（1 秒格點認證）" in (wa.properties.description or "")
          and all(f"{k} {states(rs).get(k, 0)}" in (wa.properties.description or "") for k in RD.RAW_STATES))
    exp_x = excel_expect(rs, res)
    check(f"V-2／V-4 Raw Data 不顯示 LIVE／CARRIED／MISSED 等技術標記（{len(tech_x)} 格）；每一列的時間、狀態、"
          "10 個數值與說明＝內部來源觀測推算的內容（逐格相同）",
          not tech_x and rows_x == exp_x)
    phys_soc = {i + 1: o["values"]["soc"] for i, o in enumerate(jl(os.path.join(folder, "observations.jsonl")))}
    fail_rows = [r for r in data if T0 + timedelta(seconds=25) <= r[C["時間"]] < T0 + timedelta(seconds=32)]
    before = [r for r in data if r[C["時間"]] == T0 + timedelta(seconds=24)][0]
    live_int = [r for r in rs["rows"] if r["state"] == RD.RAW_LIVE]
    check(f"T-7b／V-6 讀取失敗期間（25～32 秒，{len(fail_rows)} 列）Excel 整筆沿用第 24 秒（狀態、10 個數值、說明全部相同）；"
          "內部仍為 CARRIED、年齡 1～7 秒；LIVE 的 SOC＝來源實體行的實際讀值",
          len(fail_rows) == 7 and all(r[C["狀態"]:] == before[C["狀態"]:] for r in fail_rows)
          and [at(rs, k)["state"] for k in range(25, 32)] == [RD.RAW_CARRIED] * 7
          and [at(rs, k)["age"] for k in range(25, 32)] == list(range(1, 8))
          and live_int and all(V(res, r, "soc") == phys_soc[r["src"]] for r in live_int))
    wbp = __import__("openpyxl").Workbook()
    nm = RX.stage_names(res.stages)
    names = RX.write_raw_seconds(wbp, rs, res.params, {s.no: RX._stage_title(nm, s) for s in res.stages}, max_rows=12)
    tot = sum(1 for n in names for r in wbp[n].iter_rows(min_row=2, values_only=True))
    check(f"T-11c 超過列數上限自動分頁（上限 12 列 → {len(names)} 頁），每頁有標題列、資料不遺失（{tot} 列）",
          len(names) > 1 and names[1] == "Raw Data (2)" and tot == len(rs["rows"])
          and all([c.value for c in wbp[n][1]] == RX.RAW1_HEADERS for n in names))

    hist = os.path.join(TMP, "hist")
    os.makedirs(hist)
    check("T-11d 沒有 slots.jsonl 的資料夾（歷史 5 秒認證）→ 沒有格點資料（None）→ Raw Data 維持原格式",
          CR.build_sampling_slots(hist) is None and RX.raw_second_rows(res, None) is None)

    def imports(path):
        tree = ast.parse(open(path, encoding="utf-8").read())
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module.split(".")[0])
        return out

    def rd_users(path):
        tree = ast.parse(open(path, encoding="utf-8").read())
        funcs = sorted(f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                       and any(isinstance(n, ast.Import) and any(a.name == "report_display" for a in n.names)
                               for n in ast.walk(f)))
        top = any(isinstance(n, (ast.Import, ast.ImportFrom)) and "report_display" in
                  ([a.name for a in n.names] if isinstance(n, ast.Import) else [n.module]) for n in tree.body)
        return funcs, top
    eng = imports(os.path.join(HERE, "cert_stage_engine.py"))
    cr_f, cr_top = rd_users(os.path.join(HERE, "cert_runtime.py"))
    rx_f, rx_top = rd_users(os.path.join(HERE, "cert_report_xlsx.py"))
    check("T-7c AST：分析引擎不 import report_display；cert_runtime 只在 build_sampling_slots、cert_report_xlsx 只在"
          "Raw Data 每秒列的函式內使用（階段頁／整體頁不使用）",
          "report_display" not in eng and cr_f == ["build_sampling_slots"] and not cr_top and not rx_top
          and set(rx_f) <= {"raw_second_rows", "_raw1_note", "write_raw_seconds", "raw_seconds_note"})

    # ------------------------------------------------------------------ T-10
    print("\n== T-10 分析節流（截圖事件不遺失）與狀態不重算 ==")

    class FakeWorker:
        def __init__(self):
            self.ev, self.n = [], 0

        def submit(self, kind, no, key, name, reason, trigger_time=None, extra=None, targets=None, ref_job=None):
            self.n += 1
            self.ev.append(("submit", kind, key, (extra or {}).get("b_time"), trigger_time))
            return f"J{self.n}"

        def resolve(self, jid, as_, reason):
            self.ev.append(("resolve", jid, as_))

        def mark_unfinished(self, reason):
            return 0

    def direction(t):
        if t < 120:
            return reading(i=100.0, p=95.0)                              # 充電
        if t < 200:
            return reading(i=0.0, p=0.0)                                 # 待機
        if t < 320:
            return reading(i=-100.0, p=-95.0)                            # 放電
        return reading(i=0.0, p=0.0)

    def tick_run(interval):
        SIM.t = 0.0
        root = os.path.join(TMP, f"t10_{interval}")
        plant = Plant(lambda i, t: (0.3, direction(t)))
        rt = new_rt(root, plant)
        start(rt, root)
        fw = FakeWorker()
        rt.worker = fw
        rt._delayed_for_failures = lambda: None
        old = CFG.CERT_ANALYSIS_INTERVAL_SEC
        CFG.CERT_ANALYSIS_INTERVAL_SEC = interval
        n_an = {"n": 0}
        real = rt.analysis

        def counted():
            n_an["n"] += 1
            return real()
        rt.analysis = counted
        try:
            while SIM.t < 400:
                rt.maybe_sample()
                SIM.t = math.floor(SIM.t) + 1.0
        finally:
            CFG.CERT_ANALYSIS_INTERVAL_SEC = old
        return fw.ev, n_an["n"], rt
    ev5, n5, rt5 = tick_run(5)
    ev0, n0, rt0 = tick_run(0)
    sig = lambda ev: [(e[0], e[1], e[2], e[3]) if e[0] == "submit" else (e[0], e[2]) for e in ev]
    tdiff = max([abs((a[4] - b[4]).total_seconds()) for a, b in zip(ev5, ev0)
                 if a[0] == "submit" and a[4] is not None and b[4] is not None] or [0])
    check(f"T-10a 分析每 5 秒一次（{n5} 次）vs 每筆（{n0} 次）：截圖候選／階段結束事件相同（{len(ev5)} 件），"
          f"觸發時間差 ≤ 5 秒（最大 {tdiff:.0f}）",
          sig(ev5) == sig(ev0) and len(ev5) >= 3 and n5 <= n0 / 2.5 and tdiff <= 5)
    r5, r0 = rt5.analysis(), rt0.analysis()
    check("T-10b 結束時的分析結果（階段數、每階段起訖）與每筆都分析完全相同",
          [(s.start.t, s.end_by) for s in r5.stages] == [(s.start.t, s.end_by) for s in r0.stages])

    SIM.t = 0.0
    root = os.path.join(TMP, "t10s")
    plant = Plant(lambda i, t: (0.3, reading()))
    rt = new_rt(root, plant)
    start(rt, root)
    import cert_runtime as _crm
    real_an = _crm.analyze
    cnt = {"n": 0}

    def counted_an(*a, **k):
        cnt["n"] += 1
        return real_an(*a, **k)
    _crm.analyze = counted_an
    try:
        while SIM.t < 60:
            rt.maybe_sample()
            rt.write_status()
            SIM.t = math.floor(SIM.t) + 1.0
    finally:
        _crm.analyze = real_an
    check(f"T-10c 狀態寫入不每秒重算：60 秒內完整分析 {cnt['n']} 次（每秒重算會是約 60 次）", cnt["n"] <= 14)

    # ------------------------------------------------------------------ E
    print("\n== E 回復檢查工具（唯讀）==")
    tool = os.path.join(HERE, "..", "tools", "cert_rollback_check.py")

    def run_tool(r):
        p = subprocess.run([sys.executable, "-B", tool, "--root", r, "--assume-stopped"], capture_output=True,
                           text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return p.returncode, p.stdout

    def tree_hash(r):
        h = hashlib.sha256()
        for dp, _dn, fns in sorted(os.walk(r)):
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                h.update(p.encode("utf-8"))
                h.update(open(p, "rb").read())
        return h.hexdigest()
    good = os.path.join(TMP, "t4")                                   # 已正常結束的認證
    h0 = tree_hash(good)
    rc, out = run_tool(good)
    check("E-2 全部條件成立 → 可以回復（結束代碼 0）", rc == 0 and "可以回復" in out)
    check("E-3 工具只讀：執行前後所有檔案雜湊相同", tree_hash(good) == h0)
    cases = {}
    base = os.path.join(TMP, "rb_base")
    shutil.copytree(good, base)

    def variant(name, fn):
        d = os.path.join(TMP, "rb_" + name)
        shutil.copytree(base, d)
        fn(d)
        cases[name] = run_tool(d)
    cdir = lambda d: glob.glob(os.path.join(d, "CERT_*"))[0]
    variant("active", lambda d: json.dump({"cert_id": os.path.basename(cdir(d))},
                                          open(os.path.join(d, "active.json"), "w", encoding="utf-8")))
    variant("pending", lambda d: (os.makedirs(os.path.join(d, "requests", "pending"), exist_ok=True),
                                  open(os.path.join(d, "requests", "pending", "REQ_x.json"), "w").write("{}")))

    def rec_state(d):
        p = os.path.join(cdir(d), "cert_state.json")
        s = json.load(open(p, encoding="utf-8"))
        s["status"] = "recording"
        json.dump(s, open(p, "w", encoding="utf-8"))
    variant("recording", rec_state)
    variant("tmp", lambda d: open(os.path.join(cdir(d), "cert_state.json.123.abc.tmp"), "w").write("x"))
    variant("badstate", lambda d: open(os.path.join(cdir(d), "cert_state.json"), "w").write("{bad"))
    check("E-1 任一條件不成立 → 禁止回復並寫出原因（active.json 指向認證／pending 有請求／"
          "認證仍 recording（含結束失敗）／有 *.tmp／cert_state 無法解析）",
          all(cases[k][0] == 1 and "禁止回復" in cases[k][1] for k in cases)
          and "E-2 NG" in cases["active"][1] and "E-3 NG" in cases["pending"][1]
          and "E-4 NG" in cases["recording"][1] and "E-5 NG" in cases["tmp"][1] and "E-4 NG" in cases["badstate"][1])

    # ------------------------------------------------------------------ F-1
    print("\n== F-1 長讀取跨格：MISSED 格不得沿用格點時間之後才完成／保存的讀值 ==")

    def s_f1(i, t):
        if 9 <= t < 10:
            return 0.3, reading(soc=51.0, v=951.0)                    # 第 9 格：有效（SOC 51）
        if 10 <= t < 11:
            return 2.5, reading(soc=70.0, v=990.0, cmax=3.45)         # 第 10 格：耗時 2.5 秒（10:00:12.5 才完成）
        if 13 <= t < 14:
            return 0.3, None                                          # 第 13 格：讀取失敗 → 應沿用第 10 格
        return 0.3, reading()
    rt, root, cid, folder, pl = run_cert("f1", s_f1, 40)
    obs_path = os.path.join(folder, "observations.jsonl")
    obs_bytes = open(obs_path, "rb").read()
    raw3 = {f: open(os.path.join(folder, f), "rb").read() for f in ("observations.jsonl", "slots.jsonl", "cert_timing.jsonl")}
    rs, res, ss = raw_of(rt, folder)
    r9, r10, r11, r12, r13 = (at(rs, k) for k in (9, 10, 11, 12, 13))
    ok_shape = (r10["slot_status"] == RQ.ATTEMPTED_OK and r11["slot_status"] == RQ.MISSED
                and r12["slot_status"] == RQ.MISSED and r13["slot_status"] == RQ.ATTEMPTED_FAIL)
    check("F-1a 情境成立：第 10 格讀取 2.5 秒 → 第 11、12 格 MISSED（格點歸屬不變），第 13 格讀取失敗", ok_shape)
    check("F-1b MISSED 格不沿用第 10 格（完成於 10:00:12.5）：第 11、12 格整筆沿用第 9 格（SOC 51、電壓 951），"
          "不是 70／990",
          r11["state"] == r12["state"] == RD.RAW_MISSED and r11["src"] == r12["src"] == r9["src"]
          and V(res, r11, "soc") == 51.0 and V(res, r12, "v") == 951.0)
    check("F-1c 資料年齡正確（無負值、無錯誤 0 秒）：第 11 格 2 秒、第 12 格 3 秒", (r11["age"], r12["age"]) == (2, 3))
    check("F-1d 整筆沿用不混用：第 11、12 格 10 個數值全部＝第 9 格那一列",
          vals(res, r11) == vals(res, r12) == vals(res, r9))
    check("F-1e 讀取完成並保存後，後續格點正常沿用：第 13 格（讀取失敗）整筆沿用第 10 格 SOC 70／電壓 990／"
          "Cell 最大電壓 3.45，年齡 3 秒",
          r13["state"] == RD.RAW_CARRIED and r13["src"] == r10["src"] and V(res, r13, "soc") == 70.0
          and V(res, r13, "v") == 990.0 and V(res, r13, "cmax") == 3.45 and r13["age"] == 3)

    def f1_rows_ok(rs):
        """所有沿用列：來源列早於本列、來源觀測的原始時間 ≤ 本列時間、年齡 ≥ 1 秒。"""
        return [r["time"] for r in rs["rows"] if r["state"] not in (RD.RAW_LIVE, RD.RAW_NA)
                and (r["src_time"] >= r["time"] or r["obs"].t > r["time"] or r["age"] is None or r["age"] < 1)]
    check("F-1f 全部沿用列：來源早於本列、來源觀測時間 ≤ 本列時間、資料年齡 ≥ 1 秒",
          not f1_rows_ok(rs) and inv("f1", rs, res, folder))

    def s_f1g(i, t):
        if i == 0:
            return 2.5, reading(soc=55.0)                             # 第一筆讀取 2.5 秒才完成
        return 0.3, reading()
    rt, root, cid, folder2, pl = run_cert("f1g", s_f1g, 8)
    rs2, res2, ss2 = raw_of(rt, folder2)
    b0, b1_, b2_, b3_ = (at(rs2, k) for k in (0, 1, 2, 3))
    check("F-1g 第一筆讀取尚未完成：第 1、2 格 MISSED 但尚無可用的有效觀測 → N/A（數值空白），"
          "不使用第 0 格的未來資料；第 3 格 LIVE",
          b0["state"] == RD.RAW_LIVE and b1_["state"] == b2_["state"] == RD.RAW_NA
          and b1_["slot_status"] == b2_["slot_status"] == RQ.MISSED and b1_["obs"] is None
          and RX._raw1_note(b1_, res2.params) == "" and b3_["state"] == RD.RAW_LIVE
          and not f1_rows_ok(rs2) and inv("f1g", rs2, res2, folder2))
    reps = glob.glob(os.path.join(folder, "reports", "*.xlsx"))
    raw_ws = None
    if reps:
        wb = load_workbook(reps[0], read_only=True)
        raw_ws = [r for r in wb["Raw Data"].iter_rows(values_only=True)]
        wb.close()
    hd = list(raw_ws[0]) if raw_ws else []
    rtimes = [r[0] for r in (raw_ws or [])[1:]]
    row11 = [r for r in (raw_ws or [])[1:] if r[0] == T0 + timedelta(seconds=11)]
    row9 = [r for r in (raw_ws or [])[1:] if r[0] == T0 + timedelta(seconds=9)]
    check("F-1h 原始資料不變、Menu 22 報告的 Raw Data 每秒一列：產報後 observations.jsonl 位元組不變、沒有 CARRIED；"
          "10:00:11（內部 MISSED）有一列，顯示整筆沿用第 9 格的內容、不顯示 MISSED；秒數連續不重複",
          open(obs_path, "rb").read() == obs_bytes and b"CARRIED" not in obs_bytes and raw_ws is not None
          and hd == RX.RAW1_HEADERS and rtimes and len(set(rtimes)) == len(rtimes)
          and all((b - a) == timedelta(seconds=1) for a, b in zip(rtimes, rtimes[1:]))
          and row11 and row9 and row11[0][1:] == row9[0][1:] and RD.RAW_MISSED not in row11[0]
          and at(rs, 11)["state"] == RD.RAW_MISSED)
    p_f1 = os.path.join(TMP, "f1_regen.xlsx")
    RX.save_report(res, {"conditions": ["x"], "source_notes": ["n"], "cabinet_spec": "250KW", "cycles": None,
                         "commands": [], "generated_at": "2026-11-11 12:00:00", "sampling_slots": ss}, p_f1)
    check("V-9 產生報告前後 observations.jsonl／slots.jsonl／cert_timing.jsonl 位元組完全相同",
          all(open(os.path.join(folder, f), "rb").read() == b for f, b in raw3.items()))

    # ------------------------------------------------------------------ INV
    print("\n== INV 各情境共通條件（每秒連續不重複、整筆沿用、來源序號、資料年齡）==")
    bad_inv = [x for x in INV if x[2]]
    check(f"INV {len(INV)} 個情境、共 {sum(x[1] for x in INV)} 列：全部成立"
          + ("" if not bad_inv else f"（不成立：{bad_inv[:3]}）"), INV and not bad_inv)

    # ------------------------------------------------------------------ X
    print("\n== X 禁止控制（只讀取、沒有控制命令）==")
    allowed = {"json", "math", "os", "threading", "time", "datetime", "report_quality", "argparse", "glob", "csv",
               "subprocess", "sys"}
    mods = {m: imports(os.path.join(HERE, m + ".py")) for m in ("sample_scheduler", "report_quality", "report_display")}
    mods["cert_rollback_check"] = imports(tool)
    check("X-1 新增模組只 import 標準函式庫（與 report_quality）：沒有 HMI client、控制、PCS／BMS、Phase 6 模組",
          all(v <= allowed for v in mods.values()))
    src = open(os.path.join(HERE, "cert_runtime.py"), encoding="utf-8").read()
    check("X-2 cert_runtime 沒有新增任何控制相關 import（只有既有的 cert_controls.describe 對照表）",
          imports(os.path.join(HERE, "cert_runtime.py")) - {"json", "os", "threading", "time", "traceback", "datetime",
                                                            "cert_report_config", "cert_env", "cert_heat_link",
                                                            "cert_supplement", "cert_requests", "report_quality",
                                                            "sample_scheduler", "cert_controls", "cert_stage_engine",
                                                            "report_display", "cert_report_xlsx", "cert_screenshot"}
          == set() and "post(" not in src.lower())
finally:
    _restore()
    shutil.rmtree(TMP, ignore_errors=True)

n_ok = sum(RESULTS)
print(f"\n== M-S2 1Hz 認證格點與 Raw Data 每秒列 {'PASS' if all(RESULTS) else 'FAIL'}（{n_ok}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if RESULTS and all(RESULTS) else 1)
