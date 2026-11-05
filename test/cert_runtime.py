# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— P3 服務端執行核心（只在 Monitor Owner＝背景服務 內執行）
================================================================================
職責：
  1. 處理選單送來的請求（cert_requests）：開始／結束認證、階段標記、不確定項目確認、手動補拍、重產報告
  2. 認證進行中**持續取樣**：從「開始認證」到「結束認證」不中斷，包含充電、放電、停頓、休息
       - 階段結束只影響分類與截圖，不結束整份認證；**沒有**單次報告的「待機 60 秒自動收尾」
       - 原始讀值照存：不沿用上一筆、不補 0、不內插；個別欄位缺值只影響該欄位
       - 讀取失敗也寫一筆（ok=False＋原因），斷線期間如實呈現為未知
  3. 每次取樣後用 P1 引擎（cert_stage_engine.analyze）重新分析全程 → 依方案 B v3 觸發 P2 截圖
       候選（首筆觀測待機 B）→ 階段結束／停頓點；方向直接切換 → 階段結束；失敗 → 延遲補拍
  4. 服務重啟後接續同一份認證：停機期間沒有取樣 → 引擎依 G 判為「資料間隔過長」（未知），並記錄中斷事件
  5. 產生報告：每次產出新檔（不覆蓋），失敗保留資料、可重產

本模組**不送任何控制指令**，結束認證也不會停止設備；只做唯讀 GET 與本地檔案寫入。
本模組不判斷 Ownership —— 由呼叫端（auto_monitor_service）保證只在 Owner 內建立 CertRuntime。

資料夾（output/cert_reports/<cert_id>/）：
  cert_state.json      狀態（recording／ended）、測試資訊、取樣數、報告清單
  observations.jsonl   每筆取樣的原始讀值（append-only）
  decisions.jsonl      操作人員確認紀錄（階段標記、不確定項目）（append-only）
  commands.jsonl       控制結果檔的快照（該檔只保留最近一次 → 不是完整歷史指令紀錄）（append-only）
  events.jsonl         開始、續接（含中斷期間）、結束、報告產生／失敗等事件（append-only）
  screenshots.jsonl、screenshots/   P2 截圖紀錄
  reports/             認證測試報告（每次新檔：認證測試報告_<cert_id>_NN.xlsx）
  cert_timing.jsonl    取樣 Cycle 計時（診斷／量測用，不是 Historical Evidence；寫入失敗不影響取樣 → Fail Open）
"""

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

import cert_report_config as CFG
import cert_env as ENV
import cert_heat_link as HL
import cert_supplement as SUP
import cert_requests as REQ
from cert_controls import describe as describe_action
from cert_stage_engine import Obs, analyze, classify, stage_names

ISO = "%Y-%m-%dT%H:%M:%S"
LABELS = ("滿充", "滿放", "未滿", "中止")
LABEL_WRONG_DIR = {"C": "滿放", "D": "滿充"}   # 充電階段不可標滿放、放電階段不可標滿充
LABELS_IN_PROGRESS = ("中止",)                  # 進行中的階段只允許標記中止
CHOICES = ("merge", "split")


def _iso(t):
    return t.strftime(ISO) if t else None


def _parse(s):
    return datetime.strptime(s, ISO) if s else None


def _append_jsonl(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


TIMING_FILE = "cert_timing.jsonl"


def params_note(params):
    """參數狀態說明（cert_state.json params_note 與報告來源說明共用）。
    取樣週期已正式確認（CFG.SAMPLE_INTERVAL_SEC）；其餘列出者仍為暫用值。
    params＝該認證開始時記錄的參數：與目前正式設定不同（例：舊認證 10 秒）→ 照實寫出本次實際值，不改寫成正式值。"""
    cur = CFG.SAMPLE_INTERVAL_SEC
    n = (params or {}).get("SAMPLE_INTERVAL_SEC")
    iv = (f"取樣週期 {cur} 秒為正式設定" if n in (None, cur)
          else f"取樣週期 {n} 秒（本次認證開始時的設定；目前正式設定 {cur} 秒）")
    return f"{iv}；其他參數（G、確認門檻、待機電流門檻、每分鐘容許範圍、截圖等待／重試）為暫用測試值，非現場正式值"


def _ms(a, b):
    """兩個 time.perf_counter() 讀值的差（毫秒）；耗時一律以單調時鐘計算，不用 datetime。"""
    return round((b - a) * 1000.0, 1)


def _wall_ms():
    return datetime.now().isoformat(timespec="milliseconds")      # 只供人工對照，不用來算耗時


def _new_status_stat():
    return {"n": 0, "analysis_ms_max": 0.0, "analysis_ms_sum": 0.0, "interval_ms_max": 0.0,
            "cache_hits": 0, "cache_misses": 0}


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass                # 行程中斷時最後一行可能寫一半 → 略過該行，不影響其他紀錄
    return out


def cmd_kind(action):
    """action → charge／discharge／stop／None（對照表集中於 cert_controls）。"""
    return describe_action(action)["direction"]


# =============================================================================
# 讀值 → 觀測紀錄
# =============================================================================
FIELD_SRC = {           # 引擎欄位 ← read_all 欄位
    "soc": "soc_percent", "current_a": "battery_current_a", "power_kw": "actual_active_power_kw",
    "rack_max_c": "rack_max_temperature_c", "rack_min_c": "rack_min_temperature_c",
    # Cell 最大／最小電壓（V）：read_all 由同一份 rackExtremeValueInformation 解析（目前 110 號櫃單一 Rack 的 Cell 極值）
    "cell_vmax_v": "rack_cell_max_voltage_v", "cell_vmin_v": "rack_cell_min_voltage_v",
    "voltage_v": "battery_voltage_v",
}


def build_record(t, reading, read_err, heat, heat_err):
    """
    一次取樣 → observations.jsonl 的一筆。**原始值照存**，不沿用、不補值。
    ok：至少一個量測欄位有值（例如電流缺值但溫度有值 → 仍 ok，溫度照常進報表；方向判定由引擎標示未知）。
        含 6161 溫度：HMI 無讀值但 6161 有值時仍 ok（6161 溫度照常保留）。
    hmi_ok：HMI 資料是否有效（cert_stage_engine.hmi_valid，與報表共用同一定義）；
        ok=True、hmi_ok=False ＝ 只有 6161 溫度。舊資料沒有此欄位 → 報表依數值同樣判定。
    """
    from cert_stage_engine import _num, hmi_valid
    vals, missing = {}, []
    r = reading if isinstance(reading, dict) else {}
    for k, src in FIELD_SRC.items():
        vals[k] = r.get(src)
    h = heat if isinstance(heat, dict) else {}
    vals["sensor_c"] = h.get("st")
    pt = h.get("pt") if isinstance(h.get("pt"), dict) else {}
    vals["pcs_module_c"] = pt.get("moduleTemperature")
    for k, v in vals.items():
        if _num(v) is None:
            missing.append(k)
    ok = len(missing) < len(vals)
    reasons = []
    if read_err:
        reasons.append(f"HMI 讀取失敗：{read_err}")
    elif r.get("_fail"):
        reasons.append("HMI 端點失敗：" + "、".join(str(x) for x in r["_fail"][:6]))
    if heat_err:
        reasons.append(f"6161 讀取失敗：{heat_err}")
    elif heat is not None and not h:
        reasons.append("6161 無資料（API 回傳空內容）")
    return {"t": _iso(t), "ok": ok, "hmi_ok": hmi_valid(vals), "values": vals, "missing": missing,
            "fail_reason": "" if ok else ("；".join(reasons) or "所有欄位皆無有效值"),
            "notes": reasons if ok else [],
            # 設備狀態原始值（全部取自同一次 read_all，不新增請求）：
            #   battery_power_status＝電池上下電（已上電／已下電／切換中／未知；接觸器回授）→ 報告「待機／停機」的唯一依據
            #   pcs_running_flag／pcs_standby_flag／pcs_status 只供診斷，不用來推論待機或停機
            "flags": {k: r.get(k) for k in ("pcs_charging_flag", "pcs_discharging_flag", "pcs_fault_flag",
                                            "battery_power_status", "pcs_running_flag", "pcs_standby_flag",
                                            "pcs_status", "communication_ok", "raw_source_time")}}


def record_to_obs(rec, n):
    return Obs(t=_parse(rec["t"]), ok=bool(rec.get("ok")), values=dict(rec.get("values") or {}),
               src=f"observations.jsonl:{n}", fail_reason=rec.get("fail_reason", ""),
               flags=dict(rec.get("flags") or {}), notes=list(rec.get("notes") or []))


# =============================================================================
# CertRuntime
# =============================================================================
class CertRuntime:
    """
    reader()        → read_all 讀值 dict（失敗回 None 或丟例外）
    heat_reader()   → 6161 /api/temperature 的 dict（失敗丟例外）；None = 不讀
    single_state()  → 單次報告記錄中時回傳說明文字，否則 None（用於互斥）
    cmd_file        → 控制結果檔路徑（快照 Tc 用；None = 不讀）
    capturer        → P2 截圖函式（測試注入假的）；enable_shots=False 不截圖
    clock           → 現在時間（模擬測試注入）
    read_timing()   → reader 最近一次的計時 dict（例：等鎖／讀取毫秒）；只寫入 cert_timing.jsonl，None = 不提供
    env_reader()    → 開始認證時讀一次環控（cert_env.read_env 的結果；None＝無 client）；None = 不讀（不產生起始環控快照）
    """

    def __init__(self, root, reader, heat_reader=None, single_state=None, cmd_file=None,
                 params=None, shot_params=None, capturer=None, enable_shots=None, clock=datetime.now,
                 log=print, read_timing=None, env_reader=None):
        self.root = root
        self.reader = reader
        self.env_reader = env_reader
        self._env_thread = None
        self.heat_reader = heat_reader
        self.single_state = single_state or (lambda: None)
        self.cmd_file = cmd_file
        self.p = params or CFG.params()
        self.shot_params = shot_params
        self.capturer = capturer
        self.enable_shots = CFG.CERT_SCREENSHOT_ENABLED if enable_shots is None else enable_shots
        self.clock = clock
        self.log = log
        self.read_timing = read_timing
        self.timing_errors = 0      # cert_timing.jsonl 寫入／計算失敗次數（Fail Open，只計數）
        self._last_cycle_end = None
        self._last_analysis_ms = None
        self._last_analysis_hit = None
        self._analysis_cache = None         # 上一次 analysis() 的輸入版本與結果（只存記憶體；重啟後重建）
        self.analysis_cache_enabled = True  # False＝每次重算（測試比對 Cache ON／OFF 用）
        self._status_stat = _new_status_stat()
        self._last_status_pc = None
        self.extra_notes = []       # 模擬／示範用的報告附註（正式執行為空）
        self.shot_note = ""         # 截圖頁附註（正式執行為空）
        self._lock = threading.RLock()
        self._sample_lock = threading.Lock()   # 一次只跑一個取樣 Cycle（背景取樣與 Menu 22 最後取樣互斥，不會同時讀 HMI）
        self._supp_workers = {}     # 已結束認證的事後補拍：認證資料夾 → ScreenshotWorker（寫 supplements 子資料夾）
        self._reset()
        os.makedirs(root, exist_ok=True)

    def _reset(self):
        self.cert = None            # cert_state dict（進行中）
        self.folder = None
        self.records = []
        self.decisions = []
        self.commands = []
        self.worker = None
        self.next_sample = None
        self._cands = {}            # (stage_key, B iso) → job_id
        self._resolved = set()      # 已處理階段結束的 stage_key
        self._paused_jobs = set()
        self._delayed = set()       # (job_id, target)
        self._last_cmd_sig = None
        self._report_busy = False
        self._hl = None             # 6161 連線狀態（由 records 重播；重啟／新認證後於下一筆取樣時重建）
        self._hl_keys = set()       # 已記錄的 6161 連線事件 key（避免重複提醒）

    # ---- 檔案 ---------------------------------------------------------------
    def _f(self, name, folder=None):
        return os.path.join(folder or self.folder, name)

    def _save_state(self):
        REQ.atomic_write_json(self._f("cert_state.json"), self.cert)

    def _event(self, kind, detail, t=None, folder=None):
        _append_jsonl(self._f("events.jsonl", folder), {"t": _iso(t or self.clock()), "type": kind,
                                                         "detail": detail})

    def _set_active(self, cert_id):
        REQ.atomic_write_json(os.path.join(self.root, "active.json"),
                              {"cert_id": cert_id, "updated_at": REQ.now_str(self.clock())})

    # ---- 啟動／續接 ------------------------------------------------------------
    def startup(self):
        """服務取得 Ownership 後呼叫：有進行中的認證 → 接續同一份。"""
        cid = REQ.active_on_disk(self.root)
        if not cid:
            return None
        with self._lock:
            self._load(cid)
            now = self.clock()
            last = _parse(self.cert.get("last_sample_time")) if self.cert.get("last_sample_time") else None
            gap = (now - last).total_seconds() if last else None
            detail = ("服務啟動，接續同一份認證；"
                      + (f"上次取樣 {_iso(last)}，中斷約 {gap:.0f} 秒（期間沒有取樣 → 報告呈現為缺值／狀態未知）"
                         if gap is not None else "尚無取樣紀錄"))
            self._event("resume", detail, now)
            self.cert.setdefault("interruptions", []).append(
                {"from": _iso(last), "to": _iso(now), "seconds": gap, "reason": "服務停止或重啟（期間無取樣）"})
            self._save_state()
            if self.worker:
                n = self.worker.mark_unfinished("服務中斷，截圖未完成（服務重啟前已登記的截圖工作）")
                if n:
                    self._event("screenshot_interrupted", f"{n} 個截圖目標因服務中斷未完成，已如實記錄為失敗", now)
            self.next_sample = now
            self.log(f"[CERT] 接續認證 {cid}（{detail}）")
            return cid

    def _load(self, cid):
        self.folder = os.path.join(self.root, cid)
        self.cert = REQ.read_json(self._f("cert_state.json"))
        self.records = _read_jsonl(self._f("observations.jsonl"))
        self.decisions = _read_jsonl(self._f("decisions.jsonl"))
        self.commands = _read_jsonl(self._f("commands.jsonl"))
        self._last_cmd_sig = self.cert.get("last_cmd_sig")
        self._hl = None
        self._open_worker()
        self._rebuild_shot_track()

    def _open_worker(self):
        self.worker = None
        if self.enable_shots:
            from cert_screenshot import ScreenshotWorker, shot_params
            kw = {"params": self.shot_params or shot_params()}
            if self.capturer is not None:
                kw["capturer"] = self.capturer
            self.worker = ScreenshotWorker(self.folder, **kw)

    def _rebuild_shot_track(self):
        from cert_screenshot import load_manifest
        self._cands, self._resolved, self._paused_jobs, self._delayed = {}, set(), set(), set()
        for r in load_manifest(self.folder):
            if r["type"] == "job":
                b = (r.get("extra") or {}).get("b_time")
                if r["kind"] == "candidate" and b:
                    self._cands[(r["stage_start"], b)] = r["job_id"]
                if r["kind"] == "stage_end":
                    self._resolved.add(r["stage_start"])
                if r["kind"] == "delayed" and r.get("ref_job"):
                    for t in r["targets"]:
                        self._delayed.add((r["ref_job"], t))
            elif r["type"] == "resolve":
                if r["as"] == "pause":
                    self._paused_jobs.add(r["job_id"])
        for (key, _b), jid in self._cands.items():
            if any(x["type"] == "resolve" and x["job_id"] == jid and x["as"] == "stage_end"
                   for x in load_manifest(self.folder)):
                self._resolved.add(key)

    # ---- 主迴圈（正式執行用；測試直接呼叫 tick）-------------------------------
    def run(self, stop_event):
        """兩條執行緒：請求處理（每 CERT_LOOP_SEC）與取樣（到期才取）。取樣慢不會擋住請求處理。"""
        def req_loop():
            while not stop_event.is_set():
                try:
                    self.process_requests()
                    self.poll_commands()
                    self.write_status()
                except Exception as e:                  # noqa: BLE001
                    self.log(f"[CERT] 請求處理例外（繼續）：{type(e).__name__}: {e}")
                stop_event.wait(CFG.CERT_LOOP_SEC)

        def sample_loop():
            while not stop_event.is_set():
                try:
                    self.maybe_sample()
                except Exception as e:                  # noqa: BLE001
                    self.log(f"[CERT] 取樣例外（繼續）：{type(e).__name__}: {e}")
                stop_event.wait(CFG.CERT_LOOP_SEC)
        ths = [threading.Thread(target=req_loop, name="cert-requests", daemon=True),
               threading.Thread(target=sample_loop, name="cert-sampler", daemon=True)]
        for th in ths:
            th.start()
        return ths

    def tick(self):
        """模擬測試用：一輪 = 處理請求 → 快照指令 → 到期取樣 → 寫狀態。"""
        self.process_requests()
        self.poll_commands()
        self.maybe_sample()
        self.write_status()

    def is_active(self):
        with self._lock:
            return self.cert is not None and self.cert.get("status") == "recording"

    def active_id(self):
        with self._lock:
            return self.cert["cert_id"] if self.is_active() else None

    # ---- 取樣 ----------------------------------------------------------------
    def maybe_sample(self):
        with self._lock:
            if not self.is_active():
                return False
            now = self.clock()
            if self.next_sample is not None and now < self.next_sample:
                return False
        self.sample_once()
        return True

    def sample_once(self):
        """取樣（網路讀取不持 runtime 鎖）→ 寫一筆 → 分析與截圖事件。計時另寫 cert_timing.jsonl（Fail Open）。"""
        with self._sample_lock:
            return self._sample_cycle("schedule")

    def _read_heat(self, box):
        """6161 讀取（平行執行緒；每個 Cycle 一次）。結果與耗時寫入 box，任何例外都記錄為讀取失敗。"""
        t0 = time.perf_counter()
        try:
            box["heat"] = self.heat_reader()
        except Exception as e:                          # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {e}"
        finally:
            box["ms"] = _ms(t0, time.perf_counter())

    def _sample_cycle(self, trigger):
        """
        一個正常取樣 Cycle（呼叫端須持 _sample_lock）。trigger 只寫入計時檔：schedule＝背景排程、cert_end＝Menu 22 最後取樣。
        HMI 與 6161 平行讀取（各一次，不新增請求）：6161 逾時不再拖延 HMI。
        取樣時間（observation t）＝ HMI 讀取完成時間；下一筆仍為「取樣時間 + 取樣週期」。
        本 Cycle 一定等 6161 讀取結束（受 CERT_HEAT_API_TIMEOUT_SEC 限制）才寫入與返回 → 不留背景執行緒、不重疊取樣。
        """
        reading, rerr = None, ""
        box, th = {}, None
        pc = {"start": time.perf_counter()}
        wall = _wall_ms()
        if self.heat_reader is not None:
            th = threading.Thread(target=self._read_heat, args=(box,), name="cert-heat-read", daemon=True)
            th.start()
        try:
            reading = self.reader()
            if reading is None:
                rerr = "無讀值（未登入或連線失敗）"
        except Exception as e:                          # noqa: BLE001
            rerr = f"{type(e).__name__}: {e}"
        pc["hmi"] = time.perf_counter()
        t_hmi = self.clock()                            # HMI 實際讀取完成時間＝這筆取樣的時間
        if th is not None:
            th.join()
        heat, herr = box.get("heat"), box.get("err", "")
        pc["heat"] = time.perf_counter()
        pc["heat_ms"] = box.get("ms")
        with self._lock:
            pc["lock"] = time.perf_counter()
            if not self.is_active():
                self._write_timing(wall, pc, None, reading, rerr, herr, trigger)
                return None
            now = t_hmi
            rec = build_record(now, reading, rerr, heat, herr)
            _append_jsonl(self._f("observations.jsonl"), rec)
            pc["obs"] = time.perf_counter()
            self.records.append(rec)
            self.cert["sample_count"] = len(self.records)
            self.cert["last_sample_time"] = rec["t"]
            self.cert["last_sample_ok"] = rec["ok"] and rec["hmi_ok"]     # 只有 6161 溫度不算取樣成功（選單 26 顯示）
            self._save_state()
            pc["state"] = time.perf_counter()
            self.next_sample = now + timedelta(seconds=self.p["SAMPLE_INTERVAL_SEC"])
            self._last_analysis_ms = self._last_analysis_hit = None
            self._heat_link_update(now)
            try:
                self._after_sample(now)
            except Exception as e:                      # noqa: BLE001
                self._event("analysis_error", f"{type(e).__name__}: {e}", now)
            pc["after"] = time.perf_counter()
            self._write_timing(wall, pc, rec, reading, rerr, herr, trigger)
            return rec

    def _heat_link_update(self, now):
        """6161 連線狀態：只依剛寫入的取樣結果（不新增請求）。連續失敗達門檻記一次事件、恢復時記恢復與中斷時長。
        重啟／新認證後第一次呼叫 → 由全部 records 重播重建，已記錄過的事件（events.jsonl 的 key）不重複。Fail Open。"""
        try:
            if self._hl is None:
                self._hl_keys = {e.get("key") for e in _read_jsonl(self._f("events.jsonl")) if e.get("key")}
                self._hl = HL.HeatLinkTracker(self.p["GAP_G_SEC"], CFG.CERT_HEAT_LINK_ALERT_SEC)
                last = len(self.records) - 1
                for i, rec in enumerate(self.records):
                    for kind, run, detail in self._hl.feed(rec):
                        self._heat_link_event(kind, run, detail if i == last else "（依取樣紀錄補記）" + detail, now)
            elif self.records:
                for kind, run, detail in self._hl.feed(self.records[-1]):
                    self._heat_link_event(kind, run, detail, now)
        except Exception as e:                          # noqa: BLE001
            self._event("heat_link_error", f"6161 連線狀態判斷例外（不影響取樣）：{type(e).__name__}: {e}", now)

    def _heat_link_event(self, kind, run, detail, now):
        key = HL.event_key(kind, run)
        if key in self._hl_keys:
            return
        self._hl_keys.add(key)
        _append_jsonl(self._f("events.jsonl"), {"t": _iso(now), "type": kind, "detail": detail, "key": key})
        self.log(f"[CERT] {'⚠ ' if kind != 'heat_link_up' else ''}{detail}")

    def _write_timing(self, wall, pc, rec, reading, rerr, herr, trigger="schedule"):
        """一個 Cycle 一筆計時（診斷用）。任何錯誤只計數，不丟例外、不影響 observation 與後續流程（Fail Open）。"""
        try:
            end = pc.get("after") or pc.get("lock") or time.perf_counter()
            st, self._status_stat = self._status_stat, _new_status_stat()
            t = {"wall_start": wall, "wall_end": _wall_ms(), "trigger": trigger, "obs_t": rec["t"] if rec else None,
                 "written": rec is not None, "n_obs": len(self.records),
                 "cycle_ms": _ms(pc["start"], end),
                 "interval_ms": _ms(self._last_cycle_end, end) if self._last_cycle_end is not None else None,
                 "hmi_ms": _ms(pc["start"], pc["hmi"]), "heat_ms": pc.get("heat_ms"),     # 6161 本身耗時（與 HMI 平行）
                 "heat_wait_ms": _ms(pc["hmi"], pc["heat"]),                              # HMI 讀完後再等 6161 的時間
                 "rt_lock_wait_ms": _ms(pc["heat"], pc["lock"]),
                 "obs_write_ms": _ms(pc["lock"], pc["obs"]) if "obs" in pc else None,
                 "state_write_ms": _ms(pc["obs"], pc["state"]) if "state" in pc else None,
                 "after_sample_ms": _ms(pc["state"], pc["after"]) if "after" in pc else None,
                 "analysis_ms": self._last_analysis_ms if rec is not None else None,
                 "analysis_cache_hit": self._last_analysis_hit if rec is not None else None,
                 "ok": rec["ok"] if rec else None, "hmi_ok": rec["hmi_ok"] if rec else None,
                 "pcs_missing": (rec["values"].get("pcs_module_c") in (None, "", "-")) if rec else None,
                 "hmi_fail": list(reading.get("_fail") or [])[:20] if isinstance(reading, dict) else None,
                 "read_err": rerr[:120] or None, "heat_err": herr[:120] or None,
                 "status_writes": st["n"], "status_analysis_ms_max": round(st["analysis_ms_max"], 1),
                 "status_analysis_ms_avg": round(st["analysis_ms_sum"] / st["n"], 1) if st["n"] else None,
                 "status_interval_ms_max": round(st["interval_ms_max"], 1),
                 "status_cache_hits": st["cache_hits"], "status_cache_misses": st["cache_misses"],
                 "timing_errors": self.timing_errors}
            if self.read_timing is not None:
                t["reader"] = dict(self.read_timing() or {})
            self._last_cycle_end = end
            _append_jsonl(self._f(TIMING_FILE), t)
        except Exception:                               # noqa: BLE001
            self.timing_errors += 1

    def _note_status(self, a0, a1, hit=None):
        """write_status 的 analysis() 耗時、Cache 命中與呼叫間隔（併入下一筆 Cycle 計時）；Fail Open。"""
        try:
            s = self._status_stat
            s["n"] += 1
            s["analysis_ms_max"] = max(s["analysis_ms_max"], _ms(a0, a1))
            s["analysis_ms_sum"] += _ms(a0, a1)
            if hit is not None:
                s["cache_hits" if hit else "cache_misses"] += 1
            if self._last_status_pc is not None:
                s["interval_ms_max"] = max(s["interval_ms_max"], _ms(self._last_status_pc, a0))
            self._last_status_pc = a0
        except Exception:                               # noqa: BLE001
            self.timing_errors += 1

    def analysis(self):
        """
        Analysis Cache：輸入（records／decisions／commands／參數）沒變 → 直接回傳上一次結果，不重算。
          版本＝三個 list 物件本身＋筆數：三者只會 append（取樣／選單決策／指令快照），或在 _reset／_load 整個換新
          （換新＝不同物件 → 失效）；Cache 持有舊 list 參照，不會因物件 id 重用而誤判。參數另比內容。
          回傳的 Result 由所有呼叫端共用 → 呼叫端只能讀，不可修改。
        全程持 runtime 鎖（RLock，與 append 同一把）：讀版本、重算、寫 Cache 為同一步，不會讀到舊結果或半成品。
        """
        with self._lock:
            a0 = time.perf_counter()
            key = (self.records, len(self.records), self.decisions, len(self.decisions),
                   self.commands, len(self.commands))
            c = self._analysis_cache
            hit = (self.analysis_cache_enabled and c is not None and c[2] == self.p
                   and all(x is y if isinstance(x, list) else x == y for x, y in zip(c[0], key)))
            if hit:
                res = c[1]
            else:
                obs = [record_to_obs(r, i + 1) for i, r in enumerate(self.records)]
                cmds = [{"t": _parse(c_["t"]), "cmd": c_["cmd"]} for c_ in self.commands
                        if c_.get("cmd") and c_.get("control_success") is True]
                res = analyze(obs, self.p, self.decisions, cmds)
                self._analysis_cache = (key, res, dict(self.p)) if self.analysis_cache_enabled else None
            self._last_analysis_hit = hit
            self._last_analysis_ms = _ms(a0, time.perf_counter())     # 量測用（cert_timing.jsonl）
            return res

    # ---- 截圖事件（方案 B v3 → P2）--------------------------------------------
    def _after_sample(self, now):
        if not self.worker:
            return
        res = self.analysis()
        names = stage_names(res.stages)
        fresh = timedelta(seconds=2 * self.p["SAMPLE_INTERVAL_SEC"] + self.p["GAP_G_SEC"])
        for st in res.stages:
            key = _iso(st.start.t)
            name = names[st.no]
            # ① 首筆觀測待機 B（本階段之後、或停頓中）→ 候選截圖（只在 B 剛發生時；舊的 B 不補拍候選）
            b = st.end_B
            if b is not None and (key, _iso(b.t)) not in self._cands and now - b.t <= fresh:
                self._cands[(key, _iso(b.t))] = self.worker.submit(
                    "candidate", st.no, key, name, f"首筆觀測待機 B（{b.t:%H:%M:%S}）",
                    trigger_time=b.t, extra={"b_time": _iso(b.t)})
            # ② 停頓：B 之後在 W 內恢復同方向 → 該候選判為停頓點（只留資料夾）
            for pz in st.pauses:
                if pz.B is not None:
                    jid = self._cands.get((key, _iso(pz.B.t)))
                    if jid and jid not in self._paused_jobs:
                        self.worker.resolve(jid, "pause", f"{pz.system_judgment}：{pz.R.t:%H:%M:%S} 恢復{st.dir_name}")
                        self._paused_jobs.add(jid)
            # ③ 階段結束確認
            if st.end_by in ("idle_confirmed", "idle_over_W", "opposite", "operator_split") and key not in self._resolved:
                jid = self._cands.get((key, _iso(st.end_B.t))) if st.end_B is not None else None
                if jid:
                    self.worker.resolve(jid, "stage_end", f"階段結束（{st.end_by}）：{st.end_note}")
                else:
                    trig = st.end_next.t if st.end_next is not None else now
                    kind = "stage_end" if now - trig <= fresh else "delayed"
                    self.worker.submit(kind, st.no, key, name,
                                       f"階段結束（{st.end_by}）但沒有候選截圖：{st.end_note}"
                                       + ("" if kind == "stage_end" else "（確認時已過時，屬延遲補拍）"),
                                       trigger_time=trig)
                self._resolved.add(key)
        self._delayed_for_failures()

    def _delayed_for_failures(self):
        """階段結束（含候選轉階段結束）的某目標截圖失敗 → 對該目標延遲補拍一次（報告標示非停止當下畫面）。"""
        from cert_screenshot import effective_jobs, load_manifest
        for jid, j in effective_jobs(load_manifest(self.folder)).items():
            if j["effective_kind"] != "stage_end":
                continue
            for t, r in j["results"].items():
                if r["status"] == "failed" and (jid, t) not in self._delayed:
                    self._delayed.add((jid, t))
                    self.worker.submit("delayed", j["stage_no"], j["stage_start"], j["stage_name"],
                                       f"原階段結束截圖失敗（{r['reason'][:60]}）→ 延遲補拍",
                                       targets=(t,), ref_job=jid)

    # ---- 指令快照 -----------------------------------------------------------
    def poll_commands(self):
        """控制結果檔每次會被覆寫 → 只能定期快照；兩次輪詢之間若有多個指令會漏記（如實註明）。"""
        if not self.cmd_file:
            return
        with self._lock:
            if not self.is_active():
                return
            data = REQ.read_json(self.cmd_file)
            if not isinstance(data, dict):
                return
            sig = f"{data.get('timestamp')}|{data.get('action')}"
            if sig == self._last_cmd_sig:
                return
            self._last_cmd_sig = sig
            self.cert["last_cmd_sig"] = sig
            try:
                ts = datetime.strptime(str(data.get("timestamp")), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return
            if ts < _parse(self.cert["started_at"]):
                self._save_state()
                return                              # 認證開始前的舊指令
            # 原始事件紀錄：只存控制結果檔已有的資料（不新增請求）；**不寫階段**——階段於產報時依時間對應
            info = describe_action(data.get("action"))
            rec = {"t": _iso(ts), "cmd": info["direction"], "action": data.get("action"),
                   "direction": info["direction"], "control_mode": info["control_mode"],
                   "control_mode_text": info["control_mode_text"],
                   "setpoint": data.get("power") if info["control_mode"] not in (None, "stop") else None,
                   "unit": info["unit"],
                   "control_success": data.get("control_success"), "verify_success": data.get("verify_success"),
                   "success": data.get("success"), "dry_run": data.get("dry_run"), "blocked": data.get("blocked"),
                   "seen_at": _iso(self.clock()),
                   "note": "取自 device_control_action_result.json 快照（寫檔時間，非送出瞬間；該檔只保留最近一次）"}
            if data.get("dry_run"):
                rec["cmd"] = None
            _append_jsonl(self._f("commands.jsonl"), rec)
            self.commands.append(rec)
            self._save_state()

    # ---- 請求 ----------------------------------------------------------------
    def process_requests(self):
        done = []
        for req in REQ.pending_requests(self.root):
            if REQ.read_result(self.root, req["request_id"]):
                REQ.archive_request(self.root, req)         # 重複請求：已有結果 → 不再執行
                continue
            if REQ.is_expired(req, CFG.CERT_REQUEST_EXPIRE_SEC, self.clock()):
                REQ.write_result(self.root, req, "expired",
                                 f"請求建立後超過 {CFG.CERT_REQUEST_EXPIRE_SEC} 秒才被處理（服務未執行或延遲）→ 不執行")
            else:
                h = getattr(self, f"_h_{req['type']}", None)
                try:
                    if h is None:
                        REQ.write_result(self.root, req, "rejected", f"不支援的請求類型：{req['type']}")
                    else:
                        h(req)
                except Exception as e:                      # noqa: BLE001
                    REQ.write_result(self.root, req, "failed", f"處理例外：{type(e).__name__}: {e}")
                    self.log(traceback.format_exc())
            REQ.archive_request(self.root, req)
            done.append(req["request_id"])
        return done

    def _check_cert(self, req, need_active=True):
        """請求所屬認證 ID 檢查。回傳 (folder, 錯誤訊息)。"""
        cid = req.get("cert_id")
        with self._lock:
            if need_active:
                if not self.is_active():
                    return None, "目前沒有進行中的認證"
                if cid != self.cert["cert_id"]:
                    return None, f"請求所屬認證 {cid} 不是目前進行中的認證 {self.cert['cert_id']}"
                return self.folder, ""
        folder = os.path.join(self.root, cid or "")
        if not cid or not os.path.exists(os.path.join(folder, "cert_state.json")):
            return None, f"找不到認證 {cid}"
        return folder, ""

    def _h_start_cert(self, req):
        pl = req.get("payload") or {}
        with self._lock:
            if self.is_active():
                REQ.write_result(self.root, req, "rejected",
                                 f"已有進行中的認證 {self.cert['cert_id']}（重複開始已擋下，未建立第二份）",
                                 cert_id=self.cert["cert_id"])
                return
            single = self.single_state()
            if single:
                REQ.write_result(self.root, req, "rejected",
                                 f"單次充放電報告正在記錄（{single}）→ 拒絕開始認證；請先結束單次報告")
                return
            cyc = pl.get("cycles")
            if cyc not in (None, ""):
                try:
                    cyc = int(cyc)
                    assert cyc > 0
                except (ValueError, AssertionError):
                    REQ.write_result(self.root, req, "rejected", f"循環數格式錯誤：{pl.get('cycles')!r}")
                    return
            else:
                cyc = None
            now = self.clock()
            cid = f"CERT_{now:%Y%m%d_%H%M%S}"
            k = 2
            while os.path.exists(os.path.join(self.root, cid)):
                cid = f"CERT_{now:%Y%m%d_%H%M%S}_{k}"
                k += 1
            self._reset()
            self.folder = os.path.join(self.root, cid)
            os.makedirs(os.path.join(self.folder, "reports"), exist_ok=True)
            self.cert = {
                "cert_id": cid, "status": "recording", "started_at": _iso(now), "ended_at": None,
                "start_request": req["request_id"], "end_request": None,
                "test_info": {"cabinet_spec": (pl.get("cabinet_spec") or "").strip() or None, "cycles": cyc,
                              "aircon": pl.get("aircon", ""), "hardware": pl.get("hardware", ""),
                              "conditions": pl.get("conditions", "")},
                "sample_count": 0, "last_sample_time": None, "incomplete": None, "interruptions": [],
                "reports": [], "last_report_error": None,
                "params": {k2: self.p[k2] for k2 in self.p},
                "params_note": params_note(self.p)}
            self._save_state()
            self._set_active(cid)
            self._open_worker()
            self._last_cmd_sig = None
            if self.cmd_file:                          # 記下開始當下的控制結果檔，避免把舊指令當成新的
                d = REQ.read_json(self.cmd_file)
                if isinstance(d, dict):
                    self._last_cmd_sig = f"{d.get('timestamp')}|{d.get('action')}"
                    self.cert["last_cmd_sig"] = self._last_cmd_sig
                    self._save_state()
            self._event("start", f"開始認證（請求 {req['request_id']}）；測試資訊 {self.cert['test_info']}", now)
            self.next_sample = now
            REQ.write_result(self.root, req, "done", f"已開始認證 {cid}（背景服務持續記錄）", cert_id=cid)
            self.log(f"[CERT] 開始認證 {cid}")
            if self.env_reader is not None:                 # 起始環控快照：背景讀一次，不阻擋開始、不持 runtime 鎖
                self._env_thread = threading.Thread(target=self._capture_env_start, args=(self.folder, cid),
                                                    name="cert-env-start", daemon=True)
                self._env_thread.start()

    def _capture_env_start(self, folder, cid):
        """開始認證時讀一次環控（冷氣設定＋10 個設備狀態）→ env_start_snapshot.json（只寫一次）。
        讀取失敗照實記錄在快照（不是設備異常）；任何例外只記事件，不影響認證。"""
        path = os.path.join(folder, ENV.SNAPSHOT_FILE)
        if os.path.exists(path):
            return
        now = self.clock()
        try:
            captured = self.env_reader()
            err = None if captured is not None else "沒有已登入的 client（未讀取）"
        except Exception as e:                              # noqa: BLE001
            captured, err = None, f"{type(e).__name__}: {e}"
        try:
            snap = ENV.build_snapshot(captured, now, cid, err)
            REQ.atomic_write_json(path, snap)
            bad = [d["name"] for d in snap["devices"] if d["status"] == ENV.ABNORMAL]
            failed = [n for n, e in snap["endpoints"].items() if not e["ok"]]
            self._event("env_start", f"認證開始環控快照：冷氣 {ENV.summary_lines(snap)[0]}；異常 {'、'.join(bad) or '無'}"
                        + (f"；讀取失敗 {'、'.join(failed)}" if failed else ""), now, folder=folder)
        except Exception as e:                              # noqa: BLE001
            self._event("env_start_error", f"起始環控快照寫入失敗（不影響認證）：{type(e).__name__}: {e}", now, folder=folder)

    def _h_end_cert(self, req):
        folder, err = self._check_cert(req)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        # C：結束前再跑一次正常取樣 Cycle（同一個 _sample_cycle：同 reader／_CLIENT_LOCK／寫入流程，schema 不變）。
        #    持 _sample_lock：不與背景取樣同時執行；收尾也在鎖內完成 → 結束後背景取樣不會再寫入。
        #    取樣失敗照一般失敗規則寫入（ok=False）；任何例外只記事件，不阻擋結束。
        with self._sample_lock:
            try:
                self._sample_cycle("cert_end")
            except Exception as e:                      # noqa: BLE001
                self._event("final_poll_error", f"結束前最後取樣例外（不影響結束）：{type(e).__name__}: {e}")
            worker, cid = self._end_locked(req)
        # B：「認證結束（最後狀態）」截圖：在最後一筆取樣之後擷取；等待有上限、不持 runtime 鎖；失敗／逾時照實呈現，不阻擋結束
        if worker:
            try:
                worker.submit("cert_final", None, None, "認證結束（最後狀態）", "結束認證（Menu 22）時的最後狀態",
                              trigger_time=self.clock())
            except Exception as e:                      # noqa: BLE001
                self._event("final_shot_error", f"結束截圖排程失敗（不影響結束）：{type(e).__name__}: {e}",
                            folder=os.path.join(self.root, cid))
            worker.close(CFG.CERT_FINAL_SHOT_WAIT_SEC)  # 已排入的截圖（含結束截圖）完成或逾時後才產報
        ok, msg, path = self.generate_report(cid)
        running = (REQ.read_json(os.path.join(self.root, cid, "cert_state.json")) or {}).get("incomplete")
        warn = f"⚠ {running}。" if running else ""
        REQ.write_result(self.root, req, "done",
                         f"已結束認證 {cid} 的記錄。{warn}" + (f"報告：{path}" if ok else f"報告產生失敗：{msg}（資料已保留，可重產）"),
                         cert_id=cid, data={"incomplete": running, "report": path, "report_ok": ok})
        with self._lock:
            self._reset()

    def _end_locked(self, req):
        """結束認證的狀態收尾（持 runtime 鎖）。回傳 (截圖 worker 或 None, cert_id)。"""
        with self._lock:
            now = self.clock()
            res = self.analysis()
            last = self.records[-1] if self.records else None
            running = None
            if last is not None:
                if not last.get("ok"):
                    running = "最後一筆取樣讀取失敗，結束時設備狀態未知"
                else:
                    d = classify(last["values"], self.p)
                    if d in ("C", "D"):
                        running = f"結束認證時設備仍在{'充' if d == 'C' else '放'}電（未送出停止指令，請自行確認設備）"
                    elif d is None:
                        running = "最後一筆沒有有效方向資料，結束時設備狀態未知"
            # 最後一個階段若已在待機且有候選截圖 → 以結束認證時的待機確認採用
            if res.stages and self.worker:
                st = res.stages[-1]
                key = _iso(st.start.t)
                if st.end_by == "data_end" and st.end_B is not None and running is None and key not in self._resolved:
                    jid = self._cands.get((key, _iso(st.end_B.t)))
                    if jid:
                        self.worker.resolve(jid, "stage_end", "結束認證時設備已待機 → 採用首筆觀測待機的候選截圖")
                        self._resolved.add(key)
            self.cert.update(status="ended", ended_at=_iso(now), end_request=req["request_id"],
                             incomplete=running)
            self._save_state()
            self._event("end", f"結束認證（請求 {req['request_id']}）" + (f"；⚠ {running}；報告標記未完成" if running else ""), now)
            self._set_active(None)
            return self.worker, self.cert["cert_id"]

    def _stage_keys(self, folder):
        rt = self if folder == self.folder else self._offline(folder)
        res = rt.analysis()
        return res, {_iso(st.start.t): st for st in res.stages}

    def _offline(self, folder):
        rt = CertRuntime(self.root, reader=None, params=self.p, enable_shots=False, clock=self.clock, log=self.log)
        rt.folder = folder
        rt.cert = REQ.read_json(os.path.join(folder, "cert_state.json"))
        rt.records = _read_jsonl(os.path.join(folder, "observations.jsonl"))
        rt.decisions = _read_jsonl(os.path.join(folder, "decisions.jsonl"))
        rt.commands = _read_jsonl(os.path.join(folder, "commands.jsonl"))
        return rt

    def _decide(self, req, folder, rec):
        rec.update(at=REQ.now_str(self.clock()), by=req.get("source", "menu"), request_id=req["request_id"])
        _append_jsonl(os.path.join(folder, "decisions.jsonl"), rec)
        with self._lock:
            if folder == self.folder:
                self.decisions.append(rec)

    def _h_stage_label(self, req):
        folder, err = self._check_cert(req, need_active=False)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        pl = req.get("payload") or {}
        if pl.get("label") not in LABELS:
            REQ.write_result(self.root, req, "rejected", f"標記必須是 {'／'.join(LABELS)}")
            return
        res, keys = self._stage_keys(folder)
        if pl.get("stage_start") not in keys:
            REQ.write_result(self.root, req, "rejected", f"找不到階段（開始時間 {pl.get('stage_start')}）")
            return
        st = keys[pl["stage_start"]]
        wrong = LABEL_WRONG_DIR.get(st.dir)
        if pl["label"] == wrong:                # 方向不符 → 拒絕，不改成另一個標記
            REQ.write_result(self.root, req, "rejected", f"此階段為{st.dir_name}階段，不可標記為{wrong}")
            return
        # 進行中＝記錄中認證的最後階段、資料結束時仍運行（與選單 cert_menu.stage_rows 的「進行中」同一定義）：
        # 滿充／滿放／未滿是階段完成後的結果判定 → 只允許「中止」
        recording = (REQ.read_json(os.path.join(folder, "cert_state.json")) or {}).get("status") == "recording"
        if (recording and st is res.stages[-1] and st.end_by == "data_end"
                and pl["label"] not in LABELS_IN_PROGRESS):
            REQ.write_result(self.root, req, "rejected", "此階段仍在進行中，滿充／滿放／未滿請待階段結束後再標記")
            return
        self._decide(req, folder, {"type": "stage_label", "stage_start": pl["stage_start"], "label": pl["label"],
                                   "note": pl.get("note", "")})
        REQ.write_result(self.root, req, "done", f"已標記階段 {pl['stage_start']} 為「{pl['label']}」"
                         "（只影響報告標題與頁籤名稱，不改原始資料與系統判定）")

    def _h_confirm_item(self, req):
        folder, err = self._check_cert(req, need_active=False)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        pl = req.get("payload") or {}
        if pl.get("choice") not in CHOICES:
            REQ.write_result(self.root, req, "rejected", "確認結果必須是 merge（同一階段）或 split（拆成兩個階段）")
            return
        res, _keys = self._stage_keys(folder)
        items = {pz.item_id: pz for pz in res.pauses if not pz.certain}
        if pl.get("item_id") not in items:
            REQ.write_result(self.root, req, "rejected", f"找不到不確定項目 {pl.get('item_id')}")
            return
        self._decide(req, folder, {"type": "continuity", "item_id": pl["item_id"], "choice": pl["choice"],
                                   "note": pl.get("note", "")})
        REQ.write_result(self.root, req, "done",
                         f"已記錄 {pl['item_id']}：{'同一階段' if pl['choice'] == 'merge' else '拆成兩個階段'}"
                         "（系統判定與原始資料保留；報告依此確認呈現）")

    def _h_manual_shot(self, req):
        folder, err = self._check_cert(req)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        if not self.worker:
            REQ.write_result(self.root, req, "rejected", "截圖功能未啟用（CERT_SCREENSHOT_ENABLED=False）")
            return
        with self._lock:
            res = self.analysis()
            names = stage_names(res.stages)
            pl = req.get("payload") or {}
            st = None
            if pl.get("stage_start"):
                st = next((s for s in res.stages if _iso(s.start.t) == pl["stage_start"]), None)
                if st is None:
                    REQ.write_result(self.root, req, "rejected", f"找不到階段 {pl['stage_start']}")
                    return
            elif res.stages:
                st = res.stages[-1]
            key = _iso(st.start.t) if st else None
            jid = self.worker.submit("manual", st.no if st else None, key, names[st.no] if st else "（尚無階段）",
                                     pl.get("reason") or "操作人員要求立即截圖", trigger_time=self.clock())
        REQ.write_result(self.root, req, "done", f"已排入手動截圖（{jid}），背景執行；結果見截圖紀錄",
                         data={"job_id": jid, "stage_start": key})

    def _h_post_test_supplement(self, req):
        """
        Menu 24（沒有進行中的認證時）：已結束認證的事後補拍 —— 只拍 6161 即時熱度（獨立櫃 110），
        只限原始「階段結束」即時熱度截圖失敗或缺少的階段。照片與紀錄獨立保存在 supplements（不寫原始紀錄）；
        先檢查 6161 是否恢復連線，未恢復 → 直接記失敗、不開瀏覽器、不產生圖片。
        """
        from cert_screenshot import load_manifest, select_stage_shot
        folder, err = self._check_cert(req, need_active=False)
        if not err and self.is_active():
            err = f"認證 {self.cert['cert_id']} 進行中 → 事後補拍請於結束後進行（進行中請用立即截圖）"
        if not err and not self.enable_shots:
            err = "截圖功能未啟用（CERT_SCREENSHOT_ENABLED=False）"
        if not err and (REQ.read_json(os.path.join(folder, "cert_state.json")) or {}).get("status") != "ended":
            err = f"認證 {req.get('cert_id')} 尚未結束 → 不能事後補拍"
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        pl = req.get("payload") or {}
        key = pl.get("stage_start")
        res = self._offline(folder).analysis()
        names = stage_names(res.stages)
        c = next((x for x in SUP.candidates(folder, res, names, select_stage_shot, load_manifest)
                  if x["stage_start"] == key), None)
        if c is None:
            known = any(_iso(st.start.t) == key for st in res.stages)
            REQ.write_result(self.root, req, "rejected",
                             f"階段 {key} 的原始即時熱度截圖已成功 → 不需事後補拍" if known else f"找不到階段 {key}")
            return
        extra = {"cert_id": req["cert_id"], "stage_no": c["stage_no"], "stage_start": key, "stage_name": c["name"],
                 "stage_end": c["stage_end"], "stage_end_note": c["end_note"], "orig_job_id": c["orig_job_id"],
                 "orig_status": c["orig_status"], "orig_reason": c["orig_reason"], "orig_trigger": c["orig_trigger"],
                 "source": SUP.SOURCE, "page_time_note": SUP.PAGE_TIME_NOTE, "request_id": req["request_id"]}
        reason = f"事後補拍：{pl.get('reason') or '操作人員要求'}（非階段結束原始畫面）"
        w = self._supp_worker(folder)
        args = (SUP.KIND, c["stage_no"], key, c["name"], reason)
        kw = {"trigger_time": self.clock(), "targets": ("heat",), "ref_job": c["orig_job_id"], "extra": extra}
        pre = ""
        if self.heat_reader is not None:
            try:
                self.heat_reader()
            except Exception as e:                      # noqa: BLE001
                pre = f"6161 尚未恢復連線（事前檢查 {type(e).__name__}: {e}）→ 未擷取"
        if pre:
            jid = w.submit_failed(*args, pre, **kw)
            REQ.write_result(self.root, req, "failed", f"事後補拍未執行：{pre}；已記錄失敗（{jid}），沒有產生圖片",
                             cert_id=req["cert_id"], data={"job_id": jid, "stage_start": key})
            return
        jid = w.submit(*args, **kw)
        REQ.write_result(self.root, req, "done",
                         f"已排入事後補拍（{jid}；{c['name']}；只拍 6161 即時熱度），背景執行；"
                         f"結果見 supplements，修訂報告請用「25 重新產生認證報告」",
                         cert_id=req["cert_id"], data={"job_id": jid, "stage_start": key})

    def _supp_worker(self, folder):
        w = self._supp_workers.get(folder)
        if w is None:
            from cert_screenshot import ScreenshotWorker, shot_params
            kw = {"params": self.shot_params or shot_params()}
            if self.capturer is not None:
                kw["capturer"] = self.capturer
            w = self._supp_workers[folder] = ScreenshotWorker(SUP.folder_of(folder), **kw)
        return w

    def _h_regenerate(self, req):
        folder, err = self._check_cert(req, need_active=False)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        ok, msg, path = self.generate_report(req["cert_id"])
        REQ.write_result(self.root, req, "done" if ok else "failed",
                         f"已產生新報告：{path}" if ok else f"報告產生失敗：{msg}（資料已保留，可再重產）",
                         data={"report": path})

    # ---- 報告 ----------------------------------------------------------------
    def generate_report(self, cid):
        """產生新報告檔（不覆蓋舊檔）。回傳 (ok, 訊息, 路徑)。失敗不動資料。"""
        from cert_report_xlsx import save_report
        from cert_screenshot import load_manifest
        folder = os.path.join(self.root, cid)
        rt = self if folder == self.folder else self._offline(folder)
        with self._lock:
            if self._report_busy:
                return False, "另一份報告正在產生中", None
            self._report_busy = True
        part = None
        try:
            st = REQ.read_json(os.path.join(folder, "cert_state.json")) or {}
            rep = os.path.join(folder, "reports")
            os.makedirs(rep, exist_ok=True)
            # 編號：現有檔案＋cert_state 紀錄＋supplements 修訂紀錄中用過的最大號 + 1（已刪除的編號也不重用）
            n = max(SUP.report_numbers(folder, cid, st) | {0}) + 1
            supp = SUP.has_supplements(folder)         # 有事後補拍 → 產報紀錄寫 supplements，不寫原始 state／events
            # 檔名直接標示狀態（不改動已確認的頁面版面）：設備仍運行時結束 → _未完成；記錄中產生 → _中途
            suffix = "_未完成" if st.get("incomplete") else ("_中途" if st.get("status") == "recording" else "")
            path = os.path.join(rep, f"認證測試報告_{cid}_{n:02d}{suffix}.xlsx")
            part = path[:-5] + ".partial.xlsx"
            res = rt.analysis()
            ti = st.get("test_info") or {}
            notes = [f"認證測試 {cid}：背景服務自 {st.get('started_at')} 起持續取樣（含休息），"
                     f"至 {st.get('ended_at') or '（記錄中）'}；共 {len(rt.records)} 筆。",
                     "原始讀值照存：不沿用上一筆、不補 0、不內插；讀取失敗或欄位無效 → 該分鐘 N/A、曲線斷線。",
                     params_note(st.get("params")) + "。",
                     "傳感器溫度／PCS 模組溫度取自 6161 /api/temperature（st／pt.moduleTemperature），對應關係待實機確認。",
                     "停止指令時間（Tc）取自控制結果檔的快照（寫檔時間；該檔只保留最近一次，非完整歷史）。"]
            for it in st.get("interruptions") or []:
                notes.append(f"服務中斷：{it.get('from')}～{it.get('to')}（約 {it.get('seconds') or 0:.0f} 秒）"
                             f"{it.get('reason')} → 期間無資料")
            notes = list(self.extra_notes) + notes
            if st.get("incomplete"):
                notes.insert(0, f"⚠ 未完成：{st['incomplete']}")
            if st.get("status") == "recording":
                notes.insert(0, "⚠ 本報告產生時認證仍在記錄中（中途報告）")
            meta_cmds = list(rt.commands)                   # 控制紀錄頁：原始指令（階段於產報時對應）
            # 冷氣溫度／硬體概述：認證開始環控快照（不連 HMI；舊認證沒有快照 → 人工輸入並標示，沒有則 N/A）
            th = self._env_thread
            if th is not None and th.is_alive() and folder == self.folder:
                th.join(CFG.CERT_ENV_START_WAIT_SEC)         # 剛開始就結束：等起始快照寫完（有上限）
            env_snap =REQ.read_json(os.path.join(folder, ENV.SNAPSHOT_FILE))
            aircon_txt, hardware_txt = ENV.summary_lines(env_snap, ti)
            env_note = ENV.failure_note(env_snap)
            if env_note:
                notes.append(env_note)
            meta = {"conditions": [f"冷氣溫度：{aircon_txt}",
                                   f"硬體概述：{hardware_txt}",
                                   f"測試條件：{ti.get('conditions') or '（未輸入）'}"],
                    "source_notes": notes, "cabinet_spec": ti.get("cabinet_spec"), "cycles": ti.get("cycles"),
                    "commands": meta_cmds}
            if os.path.exists(os.path.join(folder, "screenshots.jsonl")) or os.path.isdir(os.path.join(folder, "screenshots")):
                hl, _evs = HL.replay(rt.records, rt.p["GAP_G_SEC"], CFG.CERT_HEAT_LINK_ALERT_SEC)
                meta["screenshots"] = {"folder": folder, "records": load_manifest(folder), "note": self.shot_note,
                                       "heat_link": {"runs": hl.all_runs(), "gap_sec": rt.p["GAP_G_SEC"]}}
                if supp:
                    meta["screenshots"]["supplements"] = {"folder": SUP.folder_of(folder), "records": SUP.load(folder)}
            save_report(res, meta, part)
            os.replace(part, path)
            if supp:
                SUP.append_report_log(folder, {"type": "report", "file": os.path.relpath(path, folder),
                                               "at": REQ.now_str(self.clock()), "supplements": len(SUP.load(folder))})
                return True, "", path
            st["reports"] = (st.get("reports") or []) + [{"file": os.path.relpath(path, folder), "at": REQ.now_str(self.clock())}]
            st["last_report_error"] = None
            self._write_state_any(folder, st)
            self._event("report", f"產生報告 {os.path.basename(path)}", folder=folder)
            return True, "", path
        except Exception as e:                              # noqa: BLE001
            if part and os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            msg = f"{type(e).__name__}: {e}"
            if SUP.has_supplements(folder):
                SUP.append_report_log(folder, {"type": "report_failed", "at": REQ.now_str(self.clock()), "error": msg})
                return False, msg, None
            st = REQ.read_json(os.path.join(folder, "cert_state.json")) or {}
            st["last_report_error"] = {"at": REQ.now_str(self.clock()), "error": msg}
            self._write_state_any(folder, st)
            self._event("report_failed", f"報告產生失敗：{msg}（資料完整保留，可用「重新產生認證報告」重試）", folder=folder)
            return False, msg, None
        finally:
            with self._lock:
                self._report_busy = False

    def _write_state_any(self, folder, st):
        with self._lock:
            if folder == self.folder and self.cert is not None:
                self.cert.update({k: st[k] for k in ("reports", "last_report_error") if k in st})
                st = self.cert
            REQ.atomic_write_json(os.path.join(folder, "cert_state.json"), st)

    # ---- 狀態 ----------------------------------------------------------------
    def write_status(self, owner_role="service"):
        with self._lock:
            s = {"updated_at": REQ.now_str(self.clock()), "pid": os.getpid(), "owner_role": owner_role,
                 "active": self.is_active(), "cert_id": self.cert["cert_id"] if self.cert else None}
            if self.cert:
                s.update(started_at=self.cert.get("started_at"), sample_count=len(self.records),
                         last_sample_time=self.cert.get("last_sample_time"),
                         last_sample_ok=self.cert.get("last_sample_ok"),
                         test_info=self.cert.get("test_info"),
                         interruptions=len(self.cert.get("interruptions") or []))
                if self._hl is not None and self.is_active():
                    s["heat_link"] = self._hl.status()
                try:
                    a0 = time.perf_counter()
                    res = self.analysis()
                    if self.is_active():
                        self._note_status(a0, time.perf_counter(), self._last_analysis_hit)
                    names = stage_names(res.stages)
                    s["stages"] = [{"no": st.no, "name": names[st.no], "start": _iso(st.start.t),
                                    "end_by": st.end_by, "label": st.label} for st in res.stages]
                    s["uncertain"] = [{"item_id": pz.item_id, "stage_no": pz.stage_no,
                                       "A": _iso(pz.A.t), "R": _iso(pz.R.t),
                                       "decided": (pz.decision or {}).get("choice")}
                                      for pz in res.pauses if not pz.certain]
                    if self.records:
                        d = classify(self.records[-1]["values"], self.p) if self.records[-1]["ok"] else None
                        s["last_direction"] = {"C": "充電", "D": "放電", "I": "待機"}.get(d, "未知")
                except Exception as e:                  # noqa: BLE001
                    s["analysis_error"] = f"{type(e).__name__}: {e}"
        REQ.atomic_write_json(os.path.join(self.root, "status.json"), s)
        return s
