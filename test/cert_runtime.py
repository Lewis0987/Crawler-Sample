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
"""

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

import cert_report_config as CFG
import cert_requests as REQ
from cert_controls import describe as describe_action
from cert_stage_engine import Obs, analyze, classify, stage_names

ISO = "%Y-%m-%dT%H:%M:%S"
LABELS = ("滿充", "滿放", "未滿", "中止")
CHOICES = ("merge", "split")


def _iso(t):
    return t.strftime(ISO) if t else None


def _parse(s):
    return datetime.strptime(s, ISO) if s else None


def _append_jsonl(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


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
    """
    from cert_stage_engine import _num
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
    return {"t": _iso(t), "ok": ok, "values": vals, "missing": missing,
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
               flags=dict(rec.get("flags") or {}))


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
    """

    def __init__(self, root, reader, heat_reader=None, single_state=None, cmd_file=None,
                 params=None, shot_params=None, capturer=None, enable_shots=None, clock=datetime.now,
                 log=print):
        self.root = root
        self.reader = reader
        self.heat_reader = heat_reader
        self.single_state = single_state or (lambda: None)
        self.cmd_file = cmd_file
        self.p = params or CFG.params()
        self.shot_params = shot_params
        self.capturer = capturer
        self.enable_shots = CFG.CERT_SCREENSHOT_ENABLED if enable_shots is None else enable_shots
        self.clock = clock
        self.log = log
        self.extra_notes = []       # 模擬／示範用的報告附註（正式執行為空）
        self.shot_note = ""         # 截圖頁附註（正式執行為空）
        self._lock = threading.RLock()
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
        """取樣（網路讀取不持鎖）→ 寫一筆 → 分析與截圖事件。"""
        reading, rerr, heat, herr = None, "", None, ""
        try:
            reading = self.reader()
            if reading is None:
                rerr = "無讀值（未登入或連線失敗）"
        except Exception as e:                          # noqa: BLE001
            rerr = f"{type(e).__name__}: {e}"
        if self.heat_reader is not None:
            try:
                heat = self.heat_reader()
            except Exception as e:                      # noqa: BLE001
                herr = f"{type(e).__name__}: {e}"
        with self._lock:
            if not self.is_active():
                return None
            now = self.clock()
            rec = build_record(now, reading, rerr, heat, herr)
            _append_jsonl(self._f("observations.jsonl"), rec)
            self.records.append(rec)
            self.cert["sample_count"] = len(self.records)
            self.cert["last_sample_time"] = rec["t"]
            self.cert["last_sample_ok"] = rec["ok"]
            self._save_state()
            self.next_sample = now + timedelta(seconds=self.p["SAMPLE_INTERVAL_SEC"])
            try:
                self._after_sample(now)
            except Exception as e:                      # noqa: BLE001
                self._event("analysis_error", f"{type(e).__name__}: {e}", now)
            return rec

    def analysis(self):
        obs = [record_to_obs(r, i + 1) for i, r in enumerate(self.records)]
        cmds = [{"t": _parse(c["t"]), "cmd": c["cmd"]} for c in self.commands
                if c.get("cmd") and c.get("control_success") is True]
        return analyze(obs, self.p, self.decisions, cmds)

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
                "params_note": "取樣週期、G、TCONF、待機電流門檻、每分鐘容許範圍與截圖參數為暫用測試值，非現場正式值"}
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

    def _h_end_cert(self, req):
        folder, err = self._check_cert(req)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
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
            if self.worker:
                self.worker.close(0)                    # 已排入的截圖仍會完成；不等待
            cid = self.cert["cert_id"]
        ok, msg, path = self.generate_report(cid)
        warn = f"⚠ {running}。" if running else ""
        REQ.write_result(self.root, req, "done",
                         f"已結束認證 {cid} 的記錄。{warn}" + (f"報告：{path}" if ok else f"報告產生失敗：{msg}（資料已保留，可重產）"),
                         cert_id=cid, data={"incomplete": running, "report": path, "report_ok": ok})
        with self._lock:
            self._reset()

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
        _res, keys = self._stage_keys(folder)
        if pl.get("stage_start") not in keys:
            REQ.write_result(self.root, req, "rejected", f"找不到階段（開始時間 {pl.get('stage_start')}）")
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
            import glob as _glob
            n = 1
            while _glob.glob(os.path.join(_glob.escape(rep), f"認證測試報告_{cid}_{n:02d}*.xlsx")):
                n += 1
            # 檔名直接標示狀態（不改動已確認的頁面版面）：設備仍運行時結束 → _未完成；記錄中產生 → _中途
            suffix = "_未完成" if st.get("incomplete") else ("_中途" if st.get("status") == "recording" else "")
            path = os.path.join(rep, f"認證測試報告_{cid}_{n:02d}{suffix}.xlsx")
            part = path[:-5] + ".partial.xlsx"
            res = rt.analysis()
            ti = st.get("test_info") or {}
            notes = [f"認證測試 {cid}：背景服務自 {st.get('started_at')} 起持續取樣（含休息），"
                     f"至 {st.get('ended_at') or '（記錄中）'}；共 {len(rt.records)} 筆。",
                     "原始讀值照存：不沿用上一筆、不補 0、不內插；讀取失敗或欄位無效 → 該分鐘 N/A、曲線斷線。",
                     "參數（取樣週期、G、確認門檻、待機電流門檻、每分鐘容許範圍、截圖等待／重試）為暫用測試值，非現場正式值。",
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
            meta = {"conditions": [f"冷氣溫度：{ti.get('aircon') or '（未輸入）'}",
                                   f"硬體概述：{ti.get('hardware') or '（未輸入）'}",
                                   f"測試條件：{ti.get('conditions') or '（未輸入）'}"],
                    "source_notes": notes, "cabinet_spec": ti.get("cabinet_spec"), "cycles": ti.get("cycles"),
                    "commands": meta_cmds}
            if os.path.exists(os.path.join(folder, "screenshots.jsonl")) or os.path.isdir(os.path.join(folder, "screenshots")):
                meta["screenshots"] = {"folder": folder, "records": load_manifest(folder), "note": self.shot_note}
            save_report(res, meta, part)
            os.replace(part, path)
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
                try:
                    res = self.analysis()
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
