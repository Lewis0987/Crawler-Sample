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
  slots.jsonl          M-S2（1 秒格點）：每個預定格點一筆（ATTEMPTED_OK／PARTIAL／FAIL、MISSED）；
                       背景服務取樣時才寫（report_quality.SlotSegment）。補值（LIVE／CARRIED／NO_DATA）**不寫入**
                       任何原始檔，只在產生報告時由 report_display 從已保存的原始紀錄推導
                       （M-S2 r2：Raw Data 每秒一列、整筆沿用；不另設 Sampling Slots 工作表）。
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
import report_quality as RQ
import sample_scheduler as SS
from cert_controls import describe as describe_action
from cert_stage_engine import Obs, _count_invalid, analyze, classify, hmi_valid, stage_names

ISO = "%Y-%m-%dT%H:%M:%S"
LABELS = ("滿充", "滿放", "未滿", "中止")
LABEL_WRONG_DIR = {"C": "滿放", "D": "滿充"}   # 充電階段不可標滿放、放電階段不可標滿充
LABELS_IN_PROGRESS = ("中止",)                  # 進行中的階段只允許標記中止
CHOICES = ("merge", "split")

# ---- G6：歷史 Session 原始參數（已結束認證的歷史分析／重產一律使用；Menu 與 runtime 共用同一套驗證）----
# 必要參數：分析或報告說明實際使用；缺少／不合法 → 拒絕歷史分析與重產（不以 CFG.params() 或預設值補齊）。
REQUIRED_PARAMS = ("W_PAUSE_SEC", "TCONF_SEC", "GAP_G_SEC", "IDLE_CURRENT_A", "IDLE_POWER_KW",
                   "CMD_STAGE_MATCH_SEC", "STOP_WARN_SEC", "SENSOR_VALID_RANGE", "SAMPLE_INTERVAL_SEC")
_PARAM_RULE = {"W_PAUSE_SEC": "gt0", "TCONF_SEC": "gt0", "GAP_G_SEC": "gt0", "IDLE_CURRENT_A": "ge0",
               "IDLE_POWER_KW": "ge0", "CMD_STAGE_MATCH_SEC": "ge0", "STOP_WARN_SEC": "gt0", "SAMPLE_INTERVAL_SEC": "gt0"}
COMPAT_PARAMS = {"MINUTE_TOL_SEC": "ge0"}       # 相容參數：分析不讀取；缺少只提示，存在時仍須合法
_RULE_TEXT = {"gt0": "大於 0", "ge0": "大於等於 0"}


class SessionParamsError(ValueError):
    """已結束認證的原始參數缺少或不合法 → 不做歷史分析（訊息列出參數名稱與原因）。"""


_INF = float("inf")


def _finite_num(v):
    """有限數值（int／float；排除 bool、NaN、±Infinity）。不另外 import math（維持 cert_runtime 既有 import 清單）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and -_INF < v < _INF


def _param_ok(v, rule):
    return _finite_num(v) and (v > 0 if rule == "gt0" else v >= 0)


def session_params_check(state):
    """
    G6：驗證 cert_state.params（認證開始時記錄的參數）。回傳 (params, errors, notes)：
      params＝合法時的參數複本（含未知額外鍵，原樣保留）；不合法 → None。
      errors＝不合法原因（參數名稱＋原因）；notes＝相容參數缺少等提示。
    不修改 state；不以目前設定或預設值補齊。
    """
    p = state.get("params") if isinstance(state, dict) else None
    if not isinstance(p, dict):
        return None, ["cert_state.json 沒有 params（認證開始時記錄的參數）或格式不是物件"], []
    errs, notes = [], []
    for k in REQUIRED_PARAMS:
        if k not in p:
            errs.append(f"{k}：缺少")
            continue
        v = p[k]
        if k == "SENSOR_VALID_RANGE":
            if v is not None and not (isinstance(v, (list, tuple)) and len(v) == 2 and all(_finite_num(x) for x in v)
                                      and v[0] < v[1]):
                errs.append(f"{k}：不合法（須為 null，或［下限, 上限］兩個有限數值且下限小於上限）：{v!r}")
            continue
        if not _param_ok(v, _PARAM_RULE[k]):
            errs.append(f"{k}：不合法（須為有限數值且{_RULE_TEXT[_PARAM_RULE[k]]}）：{v!r}")
    for k, rule in COMPAT_PARAMS.items():
        if k not in p:
            notes.append(f"{k}：缺少（相容參數，分析不使用）")
        elif not _param_ok(p[k], rule):
            errs.append(f"{k}：不合法（須為有限數值且{_RULE_TEXT[rule]}）：{p[k]!r}")
    return (None if errs else dict(p)), errs, notes


def params_diff(session_p, current_p):
    """Session 原始參數與目前設定的差異：[(名稱, Session 值, 目前值)]（鍵為兩者聯集，依名稱排序）。"""
    s, c = session_p or {}, current_p or {}
    miss = object()
    out = []
    for k in sorted(set(s) | set(c)):
        a, b = s.get(k, miss), c.get(k, miss)
        if a != b:
            out.append((k, "（無）" if a is miss else a, "（無）" if b is miss else b))
    return out

# ---- BUG-2（W-G1c 最小修正，2026-11-16）：結束認證中途失敗 → HOLD（停止取樣，等人工選單 22 重新結束）----
END_EVIDENCE_READ_TRIES = 3          # 結束證據檔讀不到時的嘗試次數（含第一次）
END_EVIDENCE_RETRY_SEC = 0.2         # 每次重試間隔（秒）
END_REQUEST_EVENT_TYPES = ("end_begin", "end_failed")   # events.jsonl 中代表「這個結束請求已開始執行」的事件

# ---- G-1／G-2／G-3 補正（2026-11-17）----
STRICT_TAIL_RETRIES = 3              # G-3：最後一行沒有換行（可能正在寫入）→ 等待後重讀的次數
STRICT_TAIL_RETRY_SEC = 0.2          # 每次重讀間隔（秒）
END_RESEND_WARN = "⚠ 結束已完成，但"   # G-2：補寫流程有任何一項異常時，結果訊息的開頭


def _iso(t):
    return t.strftime(ISO) if t else None


def _parse(s):
    return datetime.strptime(s, ISO) if s else None


def _json_copy(obj):
    """BUG-1／BUG-2：狀態快照（與 cert_state.json 落盤內容相同的 JSON 往返複本；不新增 import）。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


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


def _new_bg_stat():
    return {"n": 0, "ms_max": 0.0, "ms_sum": 0.0}


def _obs_copy(o):
    """Obs 淺複製（新物件；values／flags／notes 與範本共用 —— 分析與報告只讀不改）。比 dataclasses.replace 快。"""
    c = Obs.__new__(Obs)
    c.__dict__.update(o.__dict__)
    return c


def _ver_ge(a, b):
    """分析版本 a 是否不比 b 舊（records／decisions／commands 筆數逐項 ≥）。"""
    return all(x >= y for x, y in zip(a, b))


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


def _read_jsonl_strict(path, tries=STRICT_TAIL_RETRIES, wait=STRICT_TAIL_RETRY_SEC):
    """
    G-3 方案 S：結束證據專用的嚴格讀法。只讀（rb），不修改、不截斷、不修復檔案。回傳 (紀錄 list, 問題 list)。
      - 檔案不存在 → ([], [])；開檔／讀取失敗 → 重試後仍失敗 → ([], [原因])。
      - 中間的行（後面已有換行）無法解析、不是 UTF-8 或不是 JSON 物件 → 問題
        （所有寫入方都是「開檔 → 一次寫入一整行加換行 → 關檔」，中間的行不會是寫到一半）。
      - 最後一行沒有換行 → 可能正在寫入：等 wait 秒重讀，最多 tries 次；仍不完整 → 問題。
      - 不從損壞的文字推測事件類型；問題非空 → 呼叫端一律視為無法確認（保守處理）。
    """
    name = os.path.basename(path)
    for k in range(tries + 1):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return [], []
        except OSError as e:
            if k < tries:
                time.sleep(wait)
                continue
            return [], [f"{name} 讀不到（{type(e).__name__}）"]
        lines = data.split(b"\n")
        tail = lines.pop()
        if tail.strip() and k < tries:
            time.sleep(wait)
            continue
        recs, probs = [], []
        for i, ln in enumerate(lines, 1):
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s.decode("utf-8"))
            except ValueError:                  # 含 UnicodeDecodeError
                obj = None
            if isinstance(obj, dict):
                recs.append(obj)
            else:
                probs.append(f"{name} 第 {i} 行無法解析")
        if tail.strip():
            probs.append(f"{name} 最後一行不完整（重讀 {tries} 次仍未完成）")
        return recs, probs
    return [], [f"{name} 讀不到"]


def _is_end_of(e, rid):
    """events.jsonl 的 end 事件是否屬於請求 rid（正常結束的 detail 帶「（請求 rid）」；補登的另帶 request_id）。"""
    return e.get("type") == "end" and (e.get("request_id") == rid or f"（請求 {rid}）" in str(e.get("detail")))


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


HMI_READER_DESC = {
    "read_cert": "認證專用讀取 read_cert（每次 5 支 GET：主控資訊、Rack 極值、PCS 英文／中文、電池上下電）",
    "read_all": "完整讀取 read_all（每次 9 支 GET，另含當日電量、PCS 模式、排程開關、告警）",
}


def hmi_reader_note(st):
    """
    r2c O-1：報告資料來源說明（一行）。只依 cert_state.json 既有 metadata（hmi_reader／hmi_reader_changes）；
    兩者皆無（r2c O-1 之前的 Session）→ None（報告維持原樣，不推測）。不影響任何判定與統計。
    """
    st = st or {}
    cur, chg = st.get("hmi_reader"), st.get("hmi_reader_changes") or []
    if not cur and not chg:
        return None
    desc = lambda m: HMI_READER_DESC.get(m, str(m))       # noqa: E731
    if chg:
        parts = [f"開始時為 {desc(chg[0].get('from'))}"
                 + ("（未記錄，依程式版本推定）" if not chg[0].get("from_recorded", True) else "")]
        parts += [f"{c.get('at')} 起改為 {desc(c.get('to'))}" for c in chg]
        head = "HMI 資料來源：" + "；".join(parts) + "。"
    else:
        head = f"HMI 資料來源：{desc(cur)}。"
    return head + ("格點判定規則不變；「部分端點失敗」只計實際讀取的端點；"
                   "read_cert 不讀 PCS 模式／排程／告警（認證不使用），因此 401 次數減少是端點數減少，不代表登入狀態改善。")


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


# =============================================================================
# M-S2：格點品質與 Raw Data 每秒列的輸入（報告產生時推導；只讀已保存的原始層，不寫任何原始檔）
# =============================================================================
NOT_ATTEMPTED = (RQ.MISSED, RQ.INFERRED_MISSED)


def _pct(a, b):
    return None if not b else round(100.0 * a / b, 2)


def build_sampling_slots(folder):
    """
    M-S2：格點品質指標＋Raw Data 每秒列的輸入。沒有 slots.jsonl（歷史 5 秒認證等）→ None（Raw Data 維持原格式）。
    來源只有**已保存**的 observations.jsonl、slots.jsonl 與 cert_state.json（started_at、interruptions）：
      - 格點：report_quality.reconstruct（依段落起點與已知格點重建；SLOT 遺失 → ATTEMPTED_SLOT_LOST／INFERRED_MISSED）
      - 品質指標：report_display.quality_metrics（覆蓋率、M-1、完整即時資料率等；寫入報告描述）
    回傳 {"metrics", "grid"}；grid 交給 cert_report_xlsx 產生 Raw Data 每秒列（report_display.build_raw_seconds）。
    """
    import report_display as RD
    path = os.path.join(folder, RQ.SLOT_LOG_FILE)
    if not os.path.exists(path):
        return None
    recs, san = RQ.read_slot_log(path)
    raw, ran = RQ.load_cert_raw(os.path.join(folder, "observations.jsonl"))
    result = RQ.reconstruct(recs, raw, san, ran)
    if not result.get("has_slot_log"):
        return None
    disp = RD.build_display(result, raw, "cert")
    qm = RD.quality_metrics(result, raw, "cert", display=disp)
    slots = result.get("slots", [])
    expected = len(slots)
    cnt = {}
    for e in slots:
        cnt[e["status"]] = cnt.get(e["status"], 0) + 1
    logged = sum(1 for e in slots if e.get("source") == "logged")
    obs_fail = sum(1 for e in slots if e.get("obs_write_failed"))
    hmi_ok = (qm.get("hmi") or {}).get("hmi_valid") or 0
    an = dict(qm.get("anomaly_counts") or {})
    unterminated = qm.get("unterminated_segments") or 0
    raw_bad = qm.get("incomplete_raw_records") or 0
    slot_bad = an.get(RQ.AN_TAIL_INCOMPLETE, 0) + an.get(RQ.AN_LINE_UNPARSEABLE, 0)
    certain = (unterminated == 0 and raw_bad == 0 and slot_bad == 0 and cnt.get(RQ.INFERRED_MISSED, 0) == 0
               and cnt.get(RQ.ATTEMPTED_SLOT_LOST, 0) == 0 and cnt.get(RQ.ATTEMPTED_CONFLICT, 0) == 0)
    cov = _pct(logged, expected)
    if cov is None:
        cov_text = "N/A（沒有格點）"
    elif unterminated:
        cov_text = f"不確定（{cov:.2f}%；有 {unterminated} 個段落未正常結束，尾端格點未知）"
    elif certain and logged == expected:
        cov_text = "100%"
    else:
        cov_text = f"{cov:.2f}%（有紀錄遺失或異常，見下方）" if not certain else f"{cov:.2f}%"
    # 段落：SEGMENT_END 的 end_wall（服務停止／重啟統計的起點）
    seg_end_wall = {r.get("seg_id"): r.get("end_wall") for r in (recs or []) if r.get("rec") == RQ.REC_SEGMENT_END}
    seg_info = {s["seg_id"]: s for s in result.get("segments", [])}
    seg_order = []
    for e in slots:
        if not seg_order or seg_order[-1] != e["seg_id"]:
            seg_order.append(e["seg_id"])
    last_evidence = {}
    for e in slots:
        t = e.get("read_done") or e.get("scheduled_time")
        if t:
            last_evidence[e["seg_id"]] = t
    down_total, down_n = 0.0, 0
    prev_sid = None
    for sid in seg_order:
        if prev_sid is not None:
            t_from = seg_end_wall.get(prev_sid) or last_evidence.get(prev_sid)
            t_to = (seg_info.get(sid) or {}).get("anchor_wall")
            sec = None
            a, b = RD.parse_time(t_from), RD.parse_time(t_to)
            if a is not None and b is not None:
                sec = round((b - a).total_seconds(), 1)
                if sec >= 0:
                    down_total += sec
            down_n += 1
        prev_sid = sid
    fq_all = qm.get("field_quality") or {}
    m = {"expected_slots": expected, "logged_slots": logged, "coverage_pct": cov, "coverage_text": cov_text,
         "coverage_certain": certain and not unterminated,
         "attempted_slots": qm.get("attempted_slots"), "attempt_pct": _pct(qm.get("attempted_slots") or 0, expected),
         "hmi_success_slots": hmi_ok, "m1_pct": _pct(hmi_ok, expected),
         "complete_live_slots": qm.get("complete_live_slots"),
         "complete_live_pct": _pct(qm.get("complete_live_slots") or 0, expected),
         "status_counts": cnt, "missed": cnt.get(RQ.MISSED, 0), "inferred_missed": cnt.get(RQ.INFERRED_MISSED, 0),
         "slot_lost": cnt.get(RQ.ATTEMPTED_SLOT_LOST, 0), "read_fail": cnt.get(RQ.ATTEMPTED_FAIL, 0),
         "obs_write_failed": obs_fail, "segments": len(seg_order), "unterminated_segments": unterminated,
         "service_down_count": down_n, "service_down_sec": round(down_total, 1),
         "raw_unparseable": raw_bad, "slot_log_unparseable": slot_bad, "anomaly_counts": an,
         "field_quality": {f: {"name": d.get("name"), "counts": d.get("counts"),
                               "max_carried_age_sec": d.get("max_carried_age_sec")} for f, d in fq_all.items()}}
    st = REQ.read_json(os.path.join(folder, "cert_state.json")) or {}
    grid = {"result": result, "raw": raw, "started_at": st.get("started_at"),
            "interruptions": list(st.get("interruptions") or [])}
    return {"metrics": m, "grid": grid}


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
    hmi_reader      → r2c O-1：reader 的讀取模式（"read_cert"／"read_all"）→ cert_state.json 的 hmi_reader；
                      None（測試／模擬）＝不記錄。舊 Session 沒有此欄位＝當時使用 read_all。只記錄，不影響判定與報告。
    """

    def __init__(self, root, reader, heat_reader=None, single_state=None, cmd_file=None,
                 params=None, shot_params=None, capturer=None, enable_shots=None, clock=datetime.now,
                 log=print, read_timing=None, env_reader=None, mono=None, hmi_reader=None):
        self.root = root
        self.hmi_reader = hmi_reader
        # M-S2：格點排程用的單調時鐘（正式＝time.monotonic；離線縮放測試可注入）
        self._mono = mono or time.monotonic
        self._mono_injected = mono is not None
        self._slot_seg = None               # 目前的格點段落（report_quality.SlotSegment；只由取樣迴圈／結束認證使用）
        self._seg_reason_next = "start"
        self._last_after_analysis = None    # 上一次「取樣後截圖觸發分析」的時間（runtime clock；節流用）
        self._last_status_analysis = None   # 上一次 status 重算分析的時間（runtime clock）
        self.reader = reader
        self.env_reader = env_reader
        self._env_thread = None
        self._env_folder = None             # G-2：_env_thread 寫入的認證資料夾（補寫流程讀 events 前等它寫完）
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
        # r2c（FAIL-1）：完整分析移出取樣關鍵路徑。run()（正式執行）設 True → 截圖觸發分析改由「cert-analysis」執行緒
        # 每 CERT_ANALYSIS_INTERVAL_SEC 秒執行（計算不持 runtime 鎖），取樣迴圈不再分析；tick()（模擬）維持同步（False）。
        self._async_analysis = False
        self._gen = 0                       # 認證工作階段世代：_reset／_load 遞增；舊世代的分析結果一律丟棄
        self._bg_stat = _new_bg_stat()      # 背景分析耗時（併入下一筆 Cycle 計時）
        # r2c（I）：增量轉換快取：records（只會 append）→ Obs 範本；records 換成新 list（新認證／續接重載）→ 重建
        self.incremental_obs = True         # False＝每次全部重新轉換（測試比對用）
        self._tpl_lock = threading.Lock()
        self._tpl_src = None
        self._tpl = []
        # BUG-2：HOLD 旗標（結束已開始但 ended 尚未落盤，或重啟時發現結束證據）。只在 ended 成功落盤後清除；
        #        _reset() 不碰它（不會被意外清掉）。重啟後由 startup() 依磁碟證據重新判定（不另寫 HOLD 標記檔）。
        self._halt = None
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
        self._end_steps = set()     # BUG-2：本程序內這份認證的結束步驟已完成哪些（state／event／active／report）
        self._end_flags = {}        # G-2：正常結束時的異常標記（例：active_json＝unknown）→ 寫入處理結果
        self._hl = None             # 6161 連線狀態（由 records 重播；重啟／新認證後於下一筆取樣時重建）
        self._hl_keys = set()       # 已記錄的 6161 連線事件 key（避免重複提醒）
        self._cur_slot = None       # 取樣迴圈設定的本格（tick 模擬時為 None → 不寫格點）
        self._obs_lines = 0         # observations.jsonl 目前的實體行數（SLOT raw_ref 用；_load 時重新計算）
        self._obs_write_failures = 0
        self._last_after_analysis = None
        self._last_status_analysis = None
        self._gen += 1                      # r2c：新世代（進行中的背景分析結果不得套用到新認證）
        self._applied_ver = None            # 最近一次已套用截圖觸發的分析版本（筆數）；較舊的結果不得覆蓋
        self._release_analysis_memory()

    def _release_analysis_memory(self):
        """
        r2c O-2：認證結束／切換／重載時釋放分析記憶體（呼叫端持 runtime 鎖，且已遞增 _gen）。
          分析快取（版本鍵持有 records／decisions／commands list → 整份原始紀錄）與增量範本都只屬於「目前這份」認證；
          已結束認證的報告重產（Menu 25）、事後補拍（Menu 24）、標記走 _offline（另建 runtime 從檔案讀）→ 不使用這裡的快取。
          範本以 _tpl_lock 清除：與進行中的轉換互斥；舊世代的轉換之後也不會再寫回範本（見 _obs_list）。
          鎖順序固定為 runtime 鎖 → _tpl_lock（_obs_list 內不取 runtime 鎖）→ 不會死結。
        """
        self._analysis_cache = None
        with self._tpl_lock:
            self._tpl_src, self._tpl = None, []

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
            # BUG-2：先看結束證據，再決定是否續接。有證據或無法確認 → HOLD（不續接、不寫 resume／interruptions、不取樣）。
            #        cert_state 讀不到（非 dict）→ 維持原行為（下面照舊拋出例外，由 cert_service 記錄；不續接）。
            if isinstance(self.cert, dict):
                try:
                    decision, ev = self._end_evidence(cid)
                except Exception as e:                  # noqa: BLE001
                    decision, ev = "hold", {"reason": f"結束證據判讀例外：{type(e).__name__}: {e}"}
                if decision != "resume":
                    return self._startup_hold(cid, ev)
            now = self.clock()
            last = _parse(self.cert.get("last_sample_time")) if self.cert.get("last_sample_time") else None
            gap = (now - last).total_seconds() if last else None
            detail = ("服務啟動，接續同一份認證；"
                      + (f"上次取樣 {_iso(last)}，中斷約 {gap:.0f} 秒（期間沒有取樣 → 報告呈現為缺值／狀態未知）"
                         if gap is not None else "尚無取樣紀錄"))
            self._event("resume", detail, now)
            self.cert.setdefault("interruptions", []).append(
                {"from": _iso(last), "to": _iso(now), "seconds": gap, "reason": "服務停止或重啟（期間無取樣）"})
            self._note_hmi_reader(now)
            self._save_state()
            if self.worker:
                n = self.worker.mark_unfinished("服務中斷，截圖未完成（服務重啟前已登記的截圖工作）")
                if n:
                    self._event("screenshot_interrupted", f"{n} 個截圖目標因服務中斷未完成，已如實記錄為失敗", now)
            self.next_sample = now
            self._seg_reason_next = "restart"           # M-S2：續接 → 新的格點段落（停機期間不產生格點）
            self.log(f"[CERT] 接續認證 {cid}（{detail}）")
            return cid

    # ---- BUG-2：結束證據、HOLD、結束事件 -------------------------------------------
    @staticmethod
    def _read_json_tries(path):
        """證據檔讀取：任何錯誤（含不存在、共用衝突、內容損壞）重試，最終回傳 (物件, 是否讀到)。"""
        for k in range(END_EVIDENCE_READ_TRIES):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f), True
            except (OSError, ValueError):
                if k + 1 < END_EVIDENCE_READ_TRIES:
                    time.sleep(END_EVIDENCE_RETRY_SEC)
        return None, False

    def _end_evidence(self, cid):
        """
        BUG-2（唯讀）：cert_state 仍為 recording 時，判定這份認證是否有「結束已要求或已開始」的證據。
          歸屬：type＝end_cert、cert_id 完全相同、created_at 不早於 started_at（檔名時間早於 started_at → 直接略過）。
          範圍：requests/pending、requests/done、results、本認證 events.jsonl（全部只讀）。
        回傳 (decision, ev)：resume＝沒有任何證據（照原規則續接）；hold＝有證據、矛盾或無法確認（Fail Closed）。
          rejected（未通過檢查，結束沒有開始）→ 忽略；但帶 data.hold（重啟時標記的結束請求）→ 仍是證據。
        """
        sa = self.cert.get("started_at")
        started = _parse(sa) if sa else None            # 缺少 → 不做時間過濾（範圍更大）；格式錯誤 → 例外 → 呼叫端 hold
        items, unreadable, held_marks = {}, [], []
        for sub, key in ((("requests", "pending"), "pending"), (("requests", "done"), "done"), (("results",), "result")):
            d = os.path.join(self.root, *sub)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".json"):
                    continue
                rel = "/".join(sub + (fn,))
                ft = None
                if fn.startswith("REQ_"):
                    try:
                        ft = datetime.strptime(fn[4:19], "%Y%m%d_%H%M%S")
                    except ValueError:
                        ft = None
                if ft is not None and started is not None and ft < started:
                    continue                            # 這份認證開始前建立的請求
                obj, ok = self._read_json_tries(os.path.join(d, fn))
                if not ok or not isinstance(obj, dict):
                    unreadable.append(rel)
                    continue
                if obj.get("type") != "end_cert":
                    continue
                if obj.get("cert_id") in (None, ""):
                    unreadable.append(rel + "（end_cert 沒有 cert_id）")
                    continue
                if obj.get("cert_id") != cid:
                    continue
                try:
                    created = datetime.strptime(str(obj.get("created_at")), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    unreadable.append(rel + "（created_at 無法判讀）")
                    continue
                if started is not None and created < started:
                    continue
                items.setdefault(obj.get("request_id") or fn[:-5], {})[key] = obj
        ev_begin, ev_failed, n_failed_ev = {}, {}, 0
        evp = self._f("events.jsonl")
        if os.path.exists(evp):
            evs, probs = _read_jsonl_strict(evp)        # G-3 方案 S：損壞行／未完成的尾行 → 無法確認 → HOLD
            unreadable.extend(f"{p}（無法確認）" for p in probs)
            for e in evs:
                if e.get("type") == "end_begin":
                    ev_begin.setdefault(e.get("request_id") or "（未記錄請求）", e.get("t"))
                elif e.get("type") == "end_failed":
                    n_failed_ev += 1
                    ev_failed.setdefault(e.get("request_id") or "（未記錄請求）", e.get("t"))
        held, contra, firsts = [], [], []
        for rid in sorted(items):
            x = items[rid]
            res = x.get("result")
            st = res.get("status") if res else None
            data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else {}
            mark = data.get("hold") if isinstance(data.get("hold"), dict) else None
            if mark:
                held_marks.append({"request_id": rid, "class": mark.get("class"), "status": st})
            if st == "rejected" and not mark:
                if rid in ev_begin:
                    contra.append(f"{rid}：結果為 rejected，但 events.jsonl 有 end_begin")
                continue                                # 未通過檢查 → 結束沒有開始
            src = x.get("pending") or x.get("done") or res
            # G-3：M7b 標記的結果 → 只用標記中實際讀到的 end_begin 時間（處理時間不是嘗試時間，不推估）
            fa_res = ((mark.get("first_attempt_at") if mark else res.get("processed_at")) if st == "failed" else None)
            firsts.append({"request_id": rid, "requested_at": src.get("created_at"),
                           "first_attempt_at": ev_begin.get(rid) or fa_res})
            if st in ("failed", "rejected"):
                held.append(f"{rid}：{st}" + ("（重啟時 HOLD 標記）" if mark else "（結束已開始執行但未完成）"))
            elif st == "expired":
                held.append(f"{rid}：expired（要求結束但未執行）")
            elif st == "done":
                contra.append(f"{rid}：結果為 done，但 cert_state 仍為 recording")
            elif st is not None:
                contra.append(f"{rid}：結果狀態無法判讀（{st!r}）")
            elif "pending" in x:
                held.append(f"{rid}：待處理、尚無結果")
            else:
                contra.append(f"{rid}：請求已歸檔但沒有處理結果")
        ev = {"held": held, "contradictions": contra, "unreadable": unreadable,
              "events_end_begin": sorted(ev_begin), "events_end_failed": sorted(ev_failed),
              "end_failed_count": n_failed_ev, "held_requests": held_marks}
        if not (held or contra or unreadable or ev_begin or ev_failed):
            return "resume", ev
        reasons = held + contra + [f"讀不到：{u}" for u in unreadable]
        if ev_begin or ev_failed:
            reasons.append("events.jsonl 有結束紀錄（" + "、".join(sorted(set(ev_begin) | set(ev_failed))) + "）")
        first = min(firsts, key=lambda i: str(i.get("requested_at"))) if firsts else {}
        fa = first.get("first_attempt_at") or (min(str(t) for t in ev_begin.values()) if ev_begin else None)
        ev.update(reason="；".join(reasons)[:600], request_id=first.get("request_id"),
                  requested_at=first.get("requested_at"), first_attempt_at=fa)
        return "hold", ev

    def _startup_hold(self, cid, ev):
        """BUG-2：重啟時有結束證據 → HOLD：不續接、不寫 resume／interruptions、不取樣、不自動完成結束。回傳 None。"""
        now = self.clock()
        self._halt = {"cert_id": cid, "origin": "startup", "since": _iso(now), "reason": ev.get("reason"),
                      "first_request_id": ev.get("request_id"), "first_requested_at": ev.get("requested_at"),
                      "first_attempt_at": ev.get("first_attempt_at"), "sampling_stopped_at": None,
                      "final_sample": "unknown", "attempts": int(ev.get("end_failed_count") or 0),
                      "held_requests": list(ev.get("held_requests") or [])}
        if self.worker:
            try:
                n = self.worker.mark_unfinished("服務中斷，截圖未完成（服務重啟前已登記的截圖工作）")
                if n:
                    self._event("screenshot_interrupted", f"{n} 個截圖目標因服務中斷未完成，已如實記錄為失敗", now)
            except Exception as e:                      # noqa: BLE001
                self.log(f"[CERT] 截圖工作補記失敗：{type(e).__name__}: {e}")
        detail = (f"服務啟動：認證 {cid} 有結束證據 → 不續接、停止自動取樣（HOLD），資料保留；"
                  "請用選單 22 重新結束")
        try:
            if not any(e.get("type") == "end_recovery_hold" for e in _read_jsonl(self._f("events.jsonl"))):
                self._end_event("end_recovery_hold", detail, now, reason=ev.get("reason"))   # 反覆重啟只記一次
        except Exception as e:                          # noqa: BLE001
            self.log(f"[CERT] HOLD 事件寫入失敗（HOLD 照常）：{type(e).__name__}: {e}")
        self.log(f"[CERT] {detail}（{ev.get('reason')}）")
        return None

    def _end_event(self, kind, detail, t=None, request_id=None, folder=None, **extra):
        """結束相關事件：寫入前確保檔尾換行（半行不黏住新紀錄；原有 bytes 不改）；一次寫入一整行。失敗 → 例外往外拋。"""
        rec = {"t": _iso(t or self.clock()), "type": kind, "detail": detail}
        if request_id is not None:
            rec["request_id"] = request_id
        rec.update(extra)
        path = self._f("events.jsonl", folder)
        try:
            RQ.ensure_newline_terminated(path)
        except OSError:
            pass
        _append_jsonl(path, rec)

    def _end_event_logged(self, rid):
        """events.jsonl 是否已有這個請求的 end 事件（跨程序去重；同程序另以 _end_steps 判斷）。讀不到 → False。"""
        try:
            return any(_is_end_of(e, rid) for e in _read_jsonl(self._f("events.jsonl")))
        except OSError:
            return False

    def _halt_after_failure(self, req, t0, final, e):
        """
        BUG-2：結束已開始執行後失敗（呼叫端仍持 _sample_lock）。ended 尚未落盤 → 設定 HOLD：停止取樣、不得開始新認證、
        不產報；只允許同一認證的 end_cert 重試。ended 已落盤（之後的步驟失敗）→ 不需要 HOLD（重啟也不會續接）。
        """
        with self._lock:
            if "state" in self._end_steps:
                return
            now = self.clock()
            if self._halt is None:
                self._halt = {"cert_id": (self.cert or {}).get("cert_id") or req.get("cert_id"), "origin": "in_process",
                              "since": _iso(now), "reason": None, "first_request_id": req["request_id"],
                              "first_requested_at": req.get("created_at"), "first_attempt_at": _iso(t0),
                              "sampling_stopped_at": _iso(now), "final_sample": final, "attempts": 0,
                              "held_requests": []}
            self._halt["attempts"] = int(self._halt.get("attempts") or 0) + 1
            self._halt["last_error"] = f"{type(e).__name__}: {e}"[:300]
            self._halt["reason"] = self._halt.get("reason") or self._halt["last_error"]
            self._halt["last_attempt_at"] = _iso(now)
            try:
                self._end_event("end_failed", f"結束認證未完成（請求 {req['request_id']}）：{type(e).__name__}: {e}"
                                              f" → 已停止自動取樣（HOLD），資料保留；請用選單 22 重新結束", now,
                                request_id=req["request_id"])
            except Exception:                           # noqa: BLE001
                pass
        self.log(f"[CERT] 結束認證未完成 → HOLD，停止自動取樣（{type(e).__name__}: {e}）")

    def _is_pre_restart_end(self, req):
        """M7b：HOLD（重啟判定）中、屬於同一認證、建立時間不晚於 HOLD 開始 → 重啟前的結束請求（不自動執行）。"""
        with self._lock:
            h = self._halt
            if not h or h.get("origin") != "startup" or req.get("cert_id") != h.get("cert_id"):
                return False
            since = h.get("since")
        try:
            created = datetime.strptime(str(req.get("created_at")), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return True                                 # 無法判定 → 視為重啟前（Fail Closed：不自動執行）
        return created <= _parse(since)

    def _classify_held_request(self, req):
        """
        M7b：重啟前的結束請求是否已開始執行。任何證據或無法確認 → started_no_result（不得誤判為從未執行）。
        G-3 方案 S：events／cert_timing 用嚴格讀法（HOLD 中沒有本程序的其他寫入方）；損壞行、未完成的尾行、
          沒有 request_id 的結束事件 → 記入 uncertain（寫明無法確認），不從損壞內容推測、不推估開始時間。
        回傳 (類別, 實際證據, 無法確認原因, 實際讀到的最早 end_begin 時間或 None)。
        """
        rid = req["request_id"]
        evidence, uncertain, begins = [], [], []
        try:
            created = datetime.strptime(str(req.get("created_at")), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            created = None
            uncertain.append("created_at 無法判讀，無法確認是否已開始")
        evs, probs = _read_jsonl_strict(self._f("events.jsonl"))
        uncertain.extend(f"{p}，無法確認是否已開始" for p in probs)
        for e in evs:
            if e.get("type") not in END_REQUEST_EVENT_TYPES:
                continue
            erid = e.get("request_id")
            if erid == rid:
                evidence.append(f"events.jsonl {e.get('type')} {e.get('t')}")
                if e.get("type") == "end_begin" and e.get("t"):
                    begins.append(str(e.get("t")))
            elif not isinstance(erid, str) or not erid:
                uncertain.append(f"events.jsonl 有 {e.get('type')}（{e.get('t')}）但沒有 request_id，無法確認歸屬")
        tims, probs = _read_jsonl_strict(self._f(TIMING_FILE))
        uncertain.extend(f"{p}，無法確認是否已開始" for p in probs)
        for t in tims:
            if t.get("trigger") != "cert_end":
                continue
            try:
                ws = datetime.fromisoformat(str(t.get("wall_start")))
            except ValueError:
                uncertain.append("cert_timing.jsonl cert_end 時間無法判讀，無法確認是否已開始")
                continue
            if created is None or ws >= created:
                evidence.append(f"cert_timing.jsonl cert_end {t.get('wall_start')}")
        cls = "started_no_result" if (evidence or uncertain) else "not_started"
        return cls, evidence, uncertain, (min(begins) if begins else None)

    def _hold_pre_restart_request(self, req):
        """M7b：寫入結果（保留原始請求檔與證據；不執行）。not_started → rejected＋hold；其他 → failed＋hold。"""
        cls, evidence, uncertain, first_at = self._classify_held_request(req)
        hold = {"class": cls, "original_created_at": req.get("created_at"), "evidence": evidence,
                "uncertain": uncertain, "first_attempt_at": first_at, "held_at": _iso(self.clock())}
        if cls == "not_started":
            status, msg = "rejected", ("服務重啟前建立的結束請求：認證處於 HOLD（已停止取樣），不自動執行；"
                                       "請重新用選單 22 結束")
        elif evidence:
            status, msg = "failed", ("服務重啟前建立的結束請求已開始執行（" + "；".join(evidence)[:200] + "）"
                                     + ("，另有無法確認的紀錄" if uncertain else "")
                                     + "，但沒有處理結果；認證處於 HOLD，不自動重新執行；請重新用選單 22 結束")
        else:                                           # G-3：只有無法確認 → 不寫成「已開始執行」
            status, msg = "failed", ("服務重啟前建立的結束請求：無法確認是否已開始執行（" + "；".join(uncertain)[:200]
                                     + "）；認證處於 HOLD，不自動執行；請重新用選單 22 結束")
        REQ.write_result(self.root, req, status, msg, data={"hold": hold})
        try:
            self._end_event("end_request_held", f"重啟前的結束請求 {req['request_id']}：{cls} → {status}（不自動執行）",
                            request_id=req["request_id"], hold_class=cls)
        except Exception as e:                          # noqa: BLE001
            self.log(f"[CERT] end_request_held 事件寫入失敗：{type(e).__name__}: {e}")
        with self._lock:
            if self._halt is not None:
                self._halt.setdefault("held_requests", []).append(
                    {"request_id": req["request_id"], "class": cls, "status": status})

    def _data_cutoff(self):
        """M9：資料截止時間只取自 observations.jsonl 可解析的完整行（不推估、不補造）。"""
        out = {"last_recorded_sample_at": None, "last_valid_sample_at": None,
               "cert_state_last_sample_time": (self.cert or {}).get("last_sample_time"),
               "observations_unreadable": False, "notes": []}
        try:
            recs = _read_jsonl(self._f("observations.jsonl"))
        except OSError as e:
            out["observations_unreadable"] = True
            out["notes"].append(f"observations.jsonl 讀不到（{type(e).__name__}）→ 資料截止時間不明")
            return out
        if recs:
            out["last_recorded_sample_at"] = recs[-1].get("t")
            for r in reversed(recs):
                if hmi_valid(r.get("values") or {}):
                    out["last_valid_sample_at"] = r.get("t")
                    break
        if out["cert_state_last_sample_time"] != out["last_recorded_sample_at"]:
            out["notes"].append(f"cert_state.last_sample_time（{out['cert_state_last_sample_time']}）與 observations 最後一筆"
                                f"完整紀錄（{out['last_recorded_sample_at']}）不一致 → 以 observations 為準")
        out["notes"].append("時間取自 observations.jsonl 可解析的完整行；寫入未 fsync，斷電時不保證最後數筆已落盤")
        return out

    def _note_hmi_reader(self, now):
        """
        r2c O-1：續接時讀取模式與 cert_state 記錄不同（例：舊 Session 沒有 hmi_reader＝read_all，續接後改用 read_cert）
        → 在 hmi_reader_changes 加一筆（時間、前、後）並更新 hmi_reader；只記錄，不改任何既有欄位與判定。
        """
        if not self.hmi_reader:
            return
        prev = self.cert.get("hmi_reader") or "read_all"
        if "hmi_reader" in self.cert and prev == self.hmi_reader:
            return
        if prev != self.hmi_reader:
            self.cert.setdefault("hmi_reader_changes", []).append(
                {"at": _iso(now), "from": prev, "to": self.hmi_reader,
                 "from_recorded": "hmi_reader" in self.cert})
            self._event("hmi_reader", f"HMI 讀取模式：{prev} → {self.hmi_reader}（服務續接）", now)
        self.cert["hmi_reader"] = self.hmi_reader

    def _load(self, cid):
        self._gen += 1
        self._applied_ver = None
        self._release_analysis_memory()
        self.folder = os.path.join(self.root, cid)
        self.cert = REQ.read_json(self._f("cert_state.json"))
        self.records = _read_jsonl(self._f("observations.jsonl"))
        self._obs_lines = RQ.count_physical_lines(self._f("observations.jsonl"))
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
        """三條執行緒：請求處理（每 CERT_LOOP_SEC）、取樣（到期才取）、截圖觸發分析（r2c；每 CERT_ANALYSIS_INTERVAL_SEC）。
        取樣慢不會擋住請求處理；完整分析不在取樣執行緒內執行，計算期間不持 runtime 鎖。"""
        self._async_analysis = True

        def req_loop():
            while not stop_event.is_set():
                try:
                    self.process_requests()
                    self.poll_commands()
                    self.write_status()
                except Exception as e:                  # noqa: BLE001
                    self.log(f"[CERT] 請求處理例外（繼續）：{type(e).__name__}: {e}")
                stop_event.wait(CFG.CERT_LOOP_SEC)

        ths = [threading.Thread(target=req_loop, name="cert-requests", daemon=True),
               threading.Thread(target=self._slot_sample_loop, args=(stop_event,), name="cert-sampler", daemon=True),
               threading.Thread(target=self._analysis_loop, args=(stop_event,), name="cert-analysis", daemon=True)]
        for th in ths:
            th.start()
        return ths

    def _analysis_loop(self, stop_event):
        """r2c：截圖觸發分析執行緒。每 CERT_ANALYSIS_INTERVAL_SEC 秒一次；例外只記錄，下一輪照常。"""
        while not stop_event.wait(CFG.CERT_ANALYSIS_INTERVAL_SEC):
            try:
                self.bg_analysis_once()
            except Exception as e:                      # noqa: BLE001
                self.log(f"[CERT] 背景分析例外（繼續；不影響取樣）：{type(e).__name__}: {e}")

    def bg_analysis_once(self):
        """
        r2c：一次背景分析＋截圖觸發。① 持鎖取快照（只取參照與筆數）② 不持鎖計算 ③ 持鎖套用：
          認證世代已改變（結束／新認證／續接重載）或已不在記錄中 → 丟棄（"stale"）；
          版本（records／decisions／commands 筆數）比已套用的舊 → 丟棄（"older"）；否則套用既有截圖觸發邏輯（"applied"）。
        截圖去重沿用既有的 _cands／_resolved／_paused_jobs（全部在 runtime 鎖內讀寫）。回傳結果字串（測試用）。
        """
        with self._lock:
            if not self.is_active():
                return "inactive"
            gen = self._gen
        t0 = time.perf_counter()
        res, ver, _hit = self._analysis_core()
        ms = _ms(t0, time.perf_counter())
        with self._lock:
            self._note_bg(ms)
            if self._gen != gen or not self.is_active():
                return "stale"
            if self._applied_ver is not None and not _ver_ge(ver, self._applied_ver):
                return "older"
            self._applied_ver = ver
            now = self.clock()
            self._last_after_analysis = now
            if self.worker:
                self._apply_shots(res, now)
            return "applied"

    # ---- M-S2：1 秒固定格點取樣 ------------------------------------------------
    def _slot_sample_loop(self, stop_event):
        """
        固定時間格點（monotonic）：第 k 格期限＝起點＋k×週期。單一執行緒同步取樣 → 不重疊、每格最多一筆。
        讀取超過週期 → 排程器跳到下一個未來格，被跳過的格點各寫一筆 MISSED（確定沒有讀取）；
        **不補造量測、不沿用數值**（補值只在報告產生時推導）。服務停止 → 本段落結束；重啟續接 → 新段落。
        """
        sch = None
        while not stop_event.is_set():
            try:
                if not self.is_active():
                    sch = None                          # 認證未進行 → 下次開始時重新起算
                    self._slot_seg_end("finalize")      # 保底（正常由結束認證寫入；已結束則無作用）
                    stop_event.wait(CFG.CERT_LOOP_SEC)
                    continue
                if sch is None:
                    sch = SS.SlotScheduler(self.p["SAMPLE_INTERVAL_SEC"], mono=self._mono,
                                           max_sleep=0.005 if self._mono_injected else None)
                    self._slot_seg_new()
                slot = sch.wait(stop_event)
                if slot is None:
                    break
                if not self.is_active():
                    continue
                seg = self._slot_seg
                if seg is not None:
                    try:
                        seg.begin_from_slot(slot)
                        slot["seg_id"] = seg.seg_id
                    except Exception:                   # noqa: BLE001
                        pass
                self._cur_slot = slot
                try:
                    with self._sample_lock:
                        self._sample_cycle("schedule")
                except Exception as e:                  # noqa: BLE001
                    # 例：原始紀錄寫入失敗（已記 SLOT ATTEMPTED_FAIL＋obs_write_failed）→ 照常前進到下一格
                    self.log(f"[CERT] 取樣例外（繼續下一格）：{type(e).__name__}: {e}")
                missed = sch.complete()
                if missed and seg is not None:
                    try:
                        seg.write_missed_after(slot["index"], missed, RQ.MISSED_READ_OVERRUN,
                                               "前一格讀取耗時 %.0f ms" % ((self._mono() - slot["start_mono"]) * 1000.0))
                    except Exception:                   # noqa: BLE001
                        pass
            except Exception as e:                      # noqa: BLE001
                self.log(f"[CERT] 取樣例外（繼續）：{type(e).__name__}: {e}")
                stop_event.wait(CFG.CERT_LOOP_SEC)
        self._slot_seg_end("stop")                      # 服務停止（認證仍進行中）→ 本段落結束；重啟後另起新段落

    def _slot_seg_new(self):
        try:
            with self._lock:
                folder, cid = self.folder, (self.cert or {}).get("cert_id")
            self._slot_seg_end("stop")                  # 理論上不會有殘留；保險
            self._slot_seg = RQ.SlotSegment(folder, "cert", cid, self.p["SAMPLE_INTERVAL_SEC"],
                                            reason=self._seg_reason_next, mono=self._mono)
            self._seg_reason_next = "resume"
        except Exception as e:                          # noqa: BLE001
            self._slot_seg = None
            self.log(f"[CERT] 格點段落建立失敗（取樣照常；本段落沒有 slots.jsonl 紀錄）：{type(e).__name__}: {e}")
        return self._slot_seg

    def _slot_seg_end(self, reason):
        seg = self._slot_seg
        if seg is None:
            return
        try:
            seg.end(reason)
        except Exception:                               # noqa: BLE001
            pass
        self._slot_seg = None

    def tick(self):
        """模擬測試用：一輪 = 處理請求 → 快照指令 → 到期取樣 → 寫狀態。"""
        self.process_requests()
        self.poll_commands()
        self.maybe_sample()
        self.write_status()

    def is_active(self):
        with self._lock:
            # BUG-2：HOLD（結束已開始但未完成／重啟時有結束證據）→ 視為未進行：取樣、分析、指令快照一起停止
            return self.cert is not None and self.cert.get("status") == "recording" and self._halt is None

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

    _SLOT_FROM_SCHEDULER = object()     # 哨兵：一般排程取樣 → 取用取樣迴圈設定的 _cur_slot

    def _sample_cycle(self, trigger, slot=_SLOT_FROM_SCHEDULER):
        """
        一個正常取樣 Cycle（呼叫端須持 _sample_lock）。trigger 只寫入計時檔：schedule＝背景排程、cert_end＝Menu 22 最後取樣。
        HMI 與 6161 平行讀取（各一次，不新增請求）：6161 逾時不再拖延 HMI。
        取樣時間（observation t）＝ HMI 讀取完成時間（語意不變）；下一筆（tick 模擬路徑）＝「取樣時間 + 取樣週期」。
        本 Cycle 一定等 6161 讀取結束（受 CERT_HEAT_API_TIMEOUT_SEC 限制）才寫入與返回 → 不留背景執行緒、不重疊取樣。
        M-S2：有格點（背景格點取樣、或 Menu 22 最後一筆）→ 原始紀錄多帶 slot_seg_id／slot_index，並寫一筆 SLOT
          （寫入順序：原始紀錄 → SLOT）。原始紀錄寫入失敗 → SLOT 記 ATTEMPTED_FAIL＋obs_write_failed，
          該筆**不**加入記憶體 records（之後的分析、報告與補值都只用已保存的資料）。
          slot：Menu 22 最後一筆由呼叫端直接傳入（可為 None），不經共用的 _cur_slot。
        """
        if slot is CertRuntime._SLOT_FROM_SCHEDULER:
            slot, self._cur_slot = self._cur_slot, None
        reading, rerr = None, ""
        box, th = {}, None
        pc = {"start": time.perf_counter()}
        wall = _wall_ms()
        if self.heat_reader is not None:
            th = threading.Thread(target=self._read_heat, args=(box,), name="cert-heat-read", daemon=True)
            th.start()
        read_start = SS.wall_now()
        try:
            reading = self.reader()
            if reading is None:
                rerr = "無讀值（未登入或連線失敗）"
        except Exception as e:                          # noqa: BLE001
            rerr = f"{type(e).__name__}: {e}"
        pc["hmi"] = time.perf_counter()
        read_done = SS.wall_now()
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
            seg = self._slot_seg
            k = slot.get("index") if slot else None
            use_slot = (seg is not None and k is not None and slot.get("seg_id") is not None
                        and slot.get("seg_id") == seg.seg_id and not seg.closed)
            if use_slot:
                rec.update(seg.ident(k))                # 只多兩個鍵（slot_seg_id、slot_index）；原有鍵與值不變
            obs_path = self._f("observations.jsonl")
            try:
                try:                                    # 檔尾半行（行程中斷）→ 先補換行，新紀錄才不會黏在半行後面
                    RQ.ensure_newline_terminated(obs_path)
                except OSError:
                    pass
                _append_jsonl(obs_path, rec)
            except Exception as e:                      # noqa: BLE001
                self._obs_write_failures += 1
                if use_slot:
                    try:
                        seg.write_slot(k, RQ.ATTEMPTED_FAIL, raw_ref=None,
                                       read_start=SS.iso_ms(read_start), read_done=SS.iso_ms(read_done),
                                       timeout=(read_done - read_start).total_seconds() > seg.interval,
                                       extra={"obs_write_failed": True,
                                              "obs_write_error": f"{type(e).__name__}: {e}"[:120]})
                    except Exception:                   # noqa: BLE001
                        pass
                self._write_timing(wall, pc, None, reading, rerr, herr, trigger)
                raise
            self._obs_lines += 1
            pc["obs"] = time.perf_counter()
            if use_slot:                                # 寫入順序：原始紀錄 → SLOT
                try:
                    seg.write_slot(k, RQ.classify_cert_attempt(rec, rerr, reading), raw_ref=self._obs_lines,
                                   read_start=SS.iso_ms(read_start), read_done=SS.iso_ms(read_done),
                                   failed_endpoints=[str(x) for x in ((reading or {}).get("_fail") or [])]
                                   if isinstance(reading, dict) else [],
                                   communication_ok=(reading.get("communication_ok")
                                                     if isinstance(reading, dict) else None),
                                   timeout=(read_done - read_start).total_seconds() > seg.interval,
                                   extra={"read_error": rerr[:120] or None})
                except Exception:                       # noqa: BLE001
                    self.timing_errors += 1
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
                self._after_sample(now, force=(trigger == "cert_end"))
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
            bg, self._bg_stat = self._bg_stat, _new_bg_stat()
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
                 # r2c：背景分析（cert-analysis 執行緒）自上一筆 Cycle 以來的次數與耗時（不在取樣關鍵路徑）
                 "bg_analysis_n": bg["n"], "bg_analysis_ms_max": round(bg["ms_max"], 1),
                 "bg_analysis_ms_avg": round(bg["ms_sum"] / bg["n"], 1) if bg["n"] else None,
                 "timing_errors": self.timing_errors}
            if self.read_timing is not None:
                t["reader"] = dict(self.read_timing() or {})
            self._last_cycle_end = end
            _append_jsonl(self._f(TIMING_FILE), t)
        except Exception:                               # noqa: BLE001
            self.timing_errors += 1

    def _note_bg(self, ms):
        """背景分析耗時（呼叫端持 runtime 鎖）；Fail Open。"""
        try:
            b = self._bg_stat
            b["n"] += 1
            b["ms_max"] = max(b["ms_max"], ms)
            b["ms_sum"] += ms
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
        r2c：持鎖只做「讀版本＋快照」與「寫 Cache」；計算不持鎖（呼叫端自己持鎖時仍在鎖內，與舊版相同）。
          快照＝筆數＋list 參照（只會 append → 前 n 筆不變）；寫 Cache 只在同一世代、同一組 list 且版本不比現有舊時才寫
          → 舊結果不會覆蓋新結果，也不會寫到另一份認證。
        """
        res, _ver, hit = self._analysis_core(update_attrs=True)
        return res

    def _analysis_core(self, update_attrs=False):
        """回傳 (Result, 版本＝(records 筆數, decisions 筆數, commands 筆數), 是否命中 Cache)。"""
        with self._lock:
            a0 = time.perf_counter()
            key = (self.records, len(self.records), self.decisions, len(self.decisions),
                   self.commands, len(self.commands))
            ver = (key[1], key[3], key[5])
            c = self._analysis_cache
            hit = (self.analysis_cache_enabled and c is not None and c[2] == self.p
                   and all(x is y if isinstance(x, list) else x == y for x, y in zip(c[0], key)))
            if hit:
                res = c[1]
            else:
                gen, recs, p = self._gen, self.records, dict(self.p)
                decs = list(self.decisions)
                cmd_src = list(self.commands)
        if not hit:
            obs = self._obs_list(recs, ver[0], gen)
            cmds = [{"t": _parse(c_["t"]), "cmd": c_["cmd"]} for c_ in cmd_src
                    if c_.get("cmd") and c_.get("control_success") is True]
            res = analyze(obs, p, decs, cmds)
            with self._lock:
                if self.analysis_cache_enabled and self._gen == gen and self.records is recs and self.p == p:
                    c = self._analysis_cache
                    same = (c is not None and c[0][0] is key[0] and c[0][2] is key[2] and c[0][4] is key[4]
                            and c[2] == p)
                    if not same or _ver_ge(ver, (c[0][1], c[0][3], c[0][5])):
                        self._analysis_cache = (key, res, p)
                elif not self.analysis_cache_enabled:
                    self._analysis_cache = None
        if update_attrs:
            with self._lock:
                self._last_analysis_hit = hit
                self._last_analysis_ms = _ms(a0, time.perf_counter())     # 量測用（cert_timing.jsonl）
        return res, ver, hit

    def _obs_list(self, recs, n, gen=None):
        """
        records 前 n 筆 → Obs list。每次分析都回傳新的 Obs 物件（Stage Engine 會寫入 seq／direction／state／stage_no，
        且會在分析開始時全部重設 → 舊結果的 Obs 不會被之後的分析改寫）。
        r2c（I）：增量轉換 —— 已轉換過的紀錄保留「範本」（時間解析、values／flags／notes 複本），之後只轉換新增的筆數，
          每次分析以淺複製產生新物件（values／flags／notes 共用範本；分析與報告只讀不改）。
          範本只對應同一個 records list 物件（list 只會 append）；換成新 list → 整個重建。多執行緒以 _tpl_lock 保護。
        O-2：gen＝取快照時的認證世代。世代已改變（認證已結束／切換／重載）→ 只轉換、不寫回範本
          （否則舊認證的 list 會被重新掛回，直到下一份認證才釋放）。世代在 _tpl_lock 內比對：_reset 先遞增世代再取此鎖清除。
        """
        if not self.incremental_obs:
            return [record_to_obs(r, i + 1) for i, r in enumerate(recs[:n])]
        with self._tpl_lock:
            stale = gen is not None and gen != self._gen
            if not stale:
                if self._tpl_src is not recs or len(self._tpl) > len(recs):
                    self._tpl_src, self._tpl = recs, []
                tpl = self._tpl
                for i in range(len(tpl), n):
                    tpl.append(record_to_obs(recs[i], i + 1))
                base = tpl[:n]
        if stale:                                       # 結果之後也會因世代不符被丟棄；只為讓呼叫端照常完成
            return [record_to_obs(r, i + 1) for i, r in enumerate(recs[:n])]
        return [_obs_copy(o) for o in base]

    def _status_analysis(self):
        """
        狀態顯示（status.json／選單 26）用的分析結果。M-S2：1 秒取樣時快取幾乎每秒失效 →
        記錄中且上一次結果未超過 CERT_STATUS_ANALYSIS_MAX_AGE_SEC 秒 → 沿用上一次結果（顯示最多落後數秒），
        否則重算。不影響截圖觸發、結束與報告（那些路徑一律呼叫 analysis()）。
        r2c：背景分析模式（run）記錄中 → 一律沿用背景分析最近一次的結果（同一份認證的 list；最多落後一個分析週期），
          不在狀態路徑重算；尚無結果才計算一次（計算不持 runtime 鎖）。
        """
        with self._lock:
            c = self._analysis_cache
            now = self.clock()
            last = self._last_status_analysis
            same = (c is not None and c[0][0] is self.records and c[0][2] is self.decisions
                    and c[0][4] is self.commands)
            if self.is_active() and same and (
                    self._async_analysis or (last is not None and 0 <= (now - last).total_seconds()
                                             < CFG.CERT_STATUS_ANALYSIS_MAX_AGE_SEC)):
                self._last_analysis_hit = True
                return c[1]
        res = self.analysis()
        with self._lock:
            self._last_status_analysis = now
        return res

    # ---- 截圖事件（方案 B v3 → P2）--------------------------------------------
    def _after_sample(self, now, force=False):
        """
        截圖觸發分析（呼叫端持 runtime 鎖）。M-S2：最多每 CERT_ANALYSIS_INTERVAL_SEC 秒一次（以 runtime clock 計），
        force=True（Menu 22 最後一筆）一定執行。截圖時效固定 CERT_SHOT_FRESH_SEC（40 秒），不隨取樣週期改變。
        r2c：背景分析模式 → 一般取樣不在此分析（由 cert-analysis 執行緒負責）；Menu 22 最後一筆（force）仍同步執行，
          確保最後階段在結束前完成判定。已套用版本較新的結果不會被較舊的覆蓋（_applied_ver）。
        """
        if not self.worker:
            return
        if self._async_analysis and not force:
            return
        last = self._last_after_analysis
        if (not force and last is not None
                and 0 <= (now - last).total_seconds() < CFG.CERT_ANALYSIS_INTERVAL_SEC):
            return                                      # 時間倒退（< 0）→ 不跳過，立即分析
        self._last_after_analysis = now
        res, ver, _hit = self._analysis_core(update_attrs=True)
        if self._applied_ver is None or _ver_ge(ver, self._applied_ver):
            self._applied_ver = ver
        self._apply_shots(res, now)

    def _apply_shots(self, res, now):
        """截圖觸發（呼叫端持 runtime 鎖）：依分析結果送出候選／停頓／階段結束截圖（既有規則與去重，不變）。"""
        names = stage_names(res.stages)
        fresh = timedelta(seconds=CFG.CERT_SHOT_FRESH_SEC)
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
            if req.get("type") == "end_cert" and self._is_pre_restart_end(req):
                # M7b：HOLD 中、重啟前建立的結束請求 → 先分類再寫結果（不自動執行；不被寫成 expired 而誤認為從未執行）
                try:
                    self._hold_pre_restart_request(req)
                except Exception as e:                      # noqa: BLE001
                    self.log(f"[CERT] 重啟前結束請求的 HOLD 標記失敗（請求保留在 pending）：{type(e).__name__}: {e}")
                    continue
                REQ.archive_request(self.root, req)
                done.append(req["request_id"])
                continue
            if req.get("type") == "end_cert":
                # G-1：逾期判斷之前，先確認同一請求是否已完成結束（已完成 → 補寫；狀態無法確認且未載入 → 不執行）
                try:
                    pc = self._end_precheck(req)
                    if pc is not None:
                        self._apply_end_precheck(req, pc)
                except Exception as e:                      # noqa: BLE001
                    self.log(f"[CERT] 結束請求前置判定／補寫失敗（請求保留在 pending，下一輪重試）：{type(e).__name__}: {e}")
                    continue
                if pc is not None:
                    REQ.archive_request(self.root, req)
                    done.append(req["request_id"])
                    continue
            if req.get("type") == "stage_count" and req.get("cert_id"):
                # G6 SC-13：決策已寫入、處理結果未寫（中斷後重啟）→ 逾期判斷之前先依 request_id 補寫結果，不重複寫入
                try:
                    sc_folder = os.path.join(self.root, req["cert_id"])
                    dup = self._stage_count_dup(sc_folder, req) if os.path.isdir(sc_folder) else None
                    if dup is not None:
                        self._stage_count_resolve_dup(req, dup)
                except Exception as e:                      # noqa: BLE001
                    self.log(f"[CERT] 計次請求前置判定失敗（請求保留在 pending，下一輪重試）：{type(e).__name__}: {e}")
                    continue
                if dup is not None:
                    REQ.archive_request(self.root, req)
                    done.append(req["request_id"])
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
                    self.log(traceback.format_exc())
                    pc = self._end_precheck(req) if req.get("type") == "end_cert" else None
                    if pc is not None and pc[0] == "resend" and pc[2] == "memory":
                        # G-2b：本請求已完成結束（ended 已落盤）之後才失敗 → 補寫流程（done＋狀態標記），不寫成 failed
                        try:
                            self._resend_end(req, pc[1], "memory", post_end_error=f"{type(e).__name__}: {e}"[:300])
                        except Exception as e2:             # noqa: BLE001
                            self.log(f"[CERT] 結束後補寫失敗（請求保留在 pending，下一輪重試）：{type(e2).__name__}: {e2}")
                            continue
                    else:
                        REQ.write_result(self.root, req, "failed", f"處理例外：{type(e).__name__}: {e}")
            REQ.archive_request(self.root, req)
            done.append(req["request_id"])
        return done

    def _check_cert(self, req, need_active=True, allow_halted=False):
        """請求所屬認證 ID 檢查。回傳 (folder, 錯誤訊息)。
        allow_halted（只有 end_cert）：HOLD 時允許同一認證重新結束（BUG-2）。"""
        cid = req.get("cert_id")
        with self._lock:
            if need_active:
                halted_ok = (allow_halted and self._halt is not None and self.cert is not None
                             and cid == self.cert.get("cert_id") == self._halt.get("cert_id"))
                if not self.is_active() and not halted_ok:
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
            if self._halt is not None:                  # BUG-2：結束未完成（HOLD）→ 不得開始新認證（原認證會成為孤兒）
                REQ.write_result(self.root, req, "rejected",
                                 f"認證 {self._halt.get('cert_id')} 結束未完成（HOLD，已停止取樣，資料保留）→ 不能開始新認證；"
                                 f"請先用選單 22 重新結束", cert_id=self._halt.get("cert_id"))
                return
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
            self._seg_reason_next = "start"             # M-S2：新認證的第一個格點段落
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
            if self.hmi_reader:                         # r2c O-1：讀取模式（None＝測試／模擬，不記錄）
                self.cert["hmi_reader"] = self.hmi_reader
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
                self._env_folder = self.folder
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
        rid = req["request_id"]
        cid = req.get("cert_id")
        # M6b／G-1：同一請求已完整結束 → 補寫流程；狀態無法確認且未載入 → 不執行
        #（process_requests 已在逾期判斷前判定；直接呼叫時同樣處理）
        pc = self._end_precheck(req)
        if pc is not None:
            self._apply_end_precheck(req, pc)
            return
        folder, err = self._check_cert(req, allow_halted=True)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        t0 = self.clock()
        with self._lock:
            halt = dict(self._halt) if self._halt is not None else None
        final = "unknown" if halt is not None else "not_taken"
        # C：結束前再跑一次正常取樣 Cycle（同一個 _sample_cycle：同 reader／_CLIENT_LOCK／寫入流程，schema 不變）。
        #    持 _sample_lock：不與背景取樣同時執行；收尾也在鎖內完成 → 結束後背景取樣不會再寫入。
        #    取樣失敗照一般失敗規則寫入（ok=False）；任何例外只記事件，不阻擋結束。
        #    BUG-2：先寫 end_begin（失敗 → HOLD，不取樣）；HOLD 重試不再取樣、不讀 HMI；收尾失敗 → HOLD 後拋出。
        with self._sample_lock:
            try:
                self._end_event("end_begin", f"開始結束認證（請求 {rid}）", t0, request_id=rid)
            except Exception as e:
                self._halt_after_failure(req, t0, "not_taken" if halt is None else final, e)
                raise
            if halt is None:
                n0 = len(self.records)
                try:
                    seg = self._slot_seg                # M-S2：最後一筆也有格點身分（目前時間所在格；中間未讀取的格記 MISSED）
                    final_slot = seg.claim_extra_slot() if seg is not None else None
                except Exception:                       # noqa: BLE001
                    final_slot = None
                try:
                    self._sample_cycle("cert_end", slot=final_slot)
                except Exception as e:                  # noqa: BLE001
                    self._event("final_poll_error", f"結束前最後取樣例外（不影響結束）：{type(e).__name__}: {e}")
                final = "taken" if len(self.records) > n0 else "not_taken"
            self._slot_seg_end("finalize")              # 冪等：段落已結束 → _slot_seg 為 None
            try:
                worker, cid = self._end_locked(
                    req, recovery=None if halt is None else ("startup" if halt.get("origin") == "startup" else "retry"))
            except Exception as e:
                self._halt_after_failure(req, t0, final, e)
                raise
        # B：「認證結束（最後狀態）」截圖：在最後一筆取樣之後擷取；等待有上限、不持 runtime 鎖；失敗／逾時照實呈現，不阻擋結束
        #    BUG-2：重新結束（HOLD 後）不截圖（截圖也是讀取 HMI；當下狀態不是結束要求當時的狀態）
        if worker:
            if halt is None:
                try:
                    worker.submit("cert_final", None, None, "認證結束（最後狀態）", "結束認證（Menu 22）時的最後狀態",
                                  trigger_time=self.clock())
                except Exception as e:                  # noqa: BLE001
                    self._event("final_shot_error", f"結束截圖排程失敗（不影響結束）：{type(e).__name__}: {e}",
                                folder=os.path.join(self.root, cid))
            worker.close(CFG.CERT_FINAL_SHOT_WAIT_SEC)  # 已排入的截圖（含結束截圖）完成或逾時後才產報
        ok, msg, path = self.generate_report(cid)
        with self._lock:                                # BUG-1：取自已落盤成功的記憶體狀態（不依賴磁碟讀取）
            self._end_steps.add("report")
            running = (self.cert or {}).get("incomplete")
            er = (self.cert or {}).get("end_recovery")
            flags = dict(self._end_flags)
        warn = f"⚠ {running}。" if running else ""
        data = {"incomplete": running, "report": path, "report_ok": ok}
        if er:
            data["end_recovery"] = er
        if flags.get("active_json"):                    # G-2：active.json 讀不到 → 未清除（正常情況不加欄位、訊息不變）
            data["active_json"] = flags["active_json"]
            warn += "⚠ active.json 讀不到或內容無效，未清除。"
        REQ.write_result(self.root, req, "done",
                         f"已結束認證 {cid} 的記錄。{warn}" + (f"報告：{path}" if ok else f"報告產生失敗：{msg}（資料已保留，可重產）"),
                         cert_id=cid, data=data)
        with self._lock:
            self._reset()

    def _end_locked(self, req, recovery=None):
        """結束認證的狀態收尾（持 runtime 鎖）。回傳 (截圖 worker 或 None, cert_id)。
        recovery（BUG-2）：None＝正常結束；retry＝同程序 HOLD 後重新結束；startup＝重啟 HOLD 後由人工重新結束。
        ended 寫入磁碟是確認點：先寫新狀態、成功後才換掉記憶體狀態；各步驟（state／event／active）只做一次。"""
        with self._lock:
            now = self.clock()
            rid = req["request_id"]
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
            h = self._halt or {}
            if recovery is not None and running is None and h.get("final_sample") != "taken":
                running = ("延遲收尾：結束當下的最後取樣"
                           + ("未完成" if h.get("final_sample") == "not_taken" else "無法確認（服務重啟後由人工結束）")
                           + "，結束時設備狀態未經確認")
            # 最後一個階段若已在待機且有候選截圖 → 以結束認證時的待機確認採用
            if res.stages and self.worker:
                st = res.stages[-1]
                key = _iso(st.start.t)
                if st.end_by == "data_end" and st.end_B is not None and running is None and key not in self._resolved:
                    jid = self._cands.get((key, _iso(st.end_B.t)))
                    if jid:
                        self.worker.resolve(jid, "stage_end", "結束認證時設備已待機 → 採用首筆觀測待機的候選截圖")
                        self._resolved.add(key)
            if "state" not in self._end_steps:
                upd = dict(status="ended", ended_at=_iso(now), end_request=rid, incomplete=running)
                if recovery is not None:
                    # M9：延遲收尾歷程（選填欄位；正常結束沒有）。時間只填實際紀錄的值，沒有就是 None，不推估、不補資料。
                    cut = self._data_cutoff()
                    upd["end_recovery"] = {
                        "mode": "after_restart" if recovery == "startup" else "in_process_retry",
                        "reason": h.get("reason") or h.get("last_error"),
                        "first_end_request_id": h.get("first_request_id") or rid,
                        "first_end_requested_at": h.get("first_requested_at") or req.get("created_at"),
                        "first_end_attempt_at": h.get("first_attempt_at"),
                        "last_recorded_sample_at": cut["last_recorded_sample_at"],
                        "last_valid_sample_at": cut["last_valid_sample_at"],
                        "cert_state_last_sample_time": cut["cert_state_last_sample_time"],
                        "observations_unreadable": cut["observations_unreadable"],
                        "sampling_stopped_at": h.get("sampling_stopped_at"),
                        "final_sample": h.get("final_sample", "unknown"),
                        "held_requests": list(h.get("held_requests") or []),
                        "manual_end_request_id": rid, "manual_end_requested_at": req.get("created_at"),
                        "completed_at": _iso(now), "failed_attempts": int(h.get("attempts") or 0),
                        "notes": cut["notes"]}
                new = dict(self.cert)
                new.update(upd)
                REQ.atomic_write_json(self._f("cert_state.json"), new)     # 失敗 → 例外（記憶體仍是 recording）
                self.cert = new
                self._end_steps.add("state")
                self._halt = None                           # ended 已落盤 → 解除 HOLD（之後的步驟失敗也不會續接）
            if "event" not in self._end_steps:
                if not self._end_event_logged(rid):         # end 事件只寫一次（跨程序以 events.jsonl 判斷）
                    self._end_event("end", f"結束認證（請求 {rid}）" + (f"；⚠ {running}；報告標記未完成" if running else "")
                                    + (f"；延遲收尾（{'服務重啟後由人工重新結束' if recovery == 'startup' else '同一程序內重新結束'}）"
                                       if recovery else ""), now)
                self._end_steps.add("event")
            if "active" not in self._end_steps:
                ap = os.path.join(self.root, "active.json")
                a, a_ok = self._read_json_tries(ap) if os.path.exists(ap) else ({}, True)
                cur = a.get("cert_id") if isinstance(a, dict) else None
                if not a_ok or not isinstance(a, dict):     # G-2：讀不到／內容無效 → 不覆寫無法確認的內容
                    self._end_flags["active_json"] = "unknown"
                    self._event("active_unreadable", "active.json 讀不到或內容無效 → 未清除（不覆寫無法確認的內容）", now)
                elif cur in (None, self.cert["cert_id"]):   # 只清除指向本認證（或沒有指向）的 active.json
                    self._set_active(None)
                else:
                    self._event("active_mismatch", f"active.json 指向其他認證 {cur}（不是 {self.cert['cert_id']}）→ 未清除", now)
                self._end_steps.add("active")
            return self.worker, self.cert["cert_id"]

    # ---- G-1／G-2：同一請求已完成結束 → 補寫流程 -------------------------------------------
    def _end_precheck(self, req):
        """
        G-1：end_cert 先確認「同一請求是否已完成結束」。回傳 None（照原本流程）或：
          ("resend", 狀態快照, "memory"／"disk")：已由本請求結束 → 補寫流程
          ("unconfirmed", 原因, None)：cert_state 無法確認、且本程序沒有載入該認證 → 不執行，只寫結果
        記憶體確認：本程序載入的就是這份認證（cert_id 與資料夾都相同）→ 以記憶體為準（磁碟狀態由它寫出）：
          ended 且 end_request＝本請求 → resend；其他（recording、HOLD、由其他請求結束）→ 原本流程。不只看 ended。
        磁碟：讀得到且 cert_id 相同、ended、end_request＝本請求 → resend；讀得到但任一項不同 → 原本流程；
          讀不到、內容損壞、不是 JSON 物件或 cert_state.json 不存在 → unconfirmed。認證資料夾不存在 → 原本流程（拒絕）。
        無法確認只影響這個請求的結果，不改變任何認證的 HOLD 狀態。
        """
        rid, cid = req["request_id"], req.get("cert_id")
        if not cid or not isinstance(cid, str):
            return None
        folder = os.path.join(self.root, cid)
        with self._lock:
            if (self.cert is not None and self.cert.get("cert_id") == cid and self.folder is not None
                    and os.path.normcase(os.path.abspath(self.folder)) == os.path.normcase(os.path.abspath(folder))):
                if self.cert.get("status") == "ended" and self.cert.get("end_request") == rid:
                    return ("resend", _json_copy(self.cert), "memory")
                return None
        if not os.path.isdir(folder):
            return None
        st, ok = self._read_json_tries(os.path.join(folder, "cert_state.json"))
        if ok and isinstance(st, dict):
            if st.get("cert_id") == cid and st.get("status") == "ended" and st.get("end_request") == rid:
                return ("resend", st, "disk")
            return None
        return ("unconfirmed", "cert_state.json " + ("讀不到或不存在" if not ok else "內容無效（不是 JSON 物件）"), None)

    def _apply_end_precheck(self, req, pc):
        kind, x, src = pc
        if kind == "resend":
            self._resend_end(req, x, src)
            return
        cid = req.get("cert_id")
        REQ.write_result(self.root, req, "failed",
                         f"無法確認認證 {cid} 是否已由此請求結束（{x}）→ 未執行（未取樣、未寫入結束紀錄、未產報），請人工確認",
                         cert_id=cid, data={"end_state": "unconfirmed", "reason": x})
        self.log(f"[CERT] 結束請求 {req['request_id']}：無法確認認證 {cid} 的狀態（{x}）→ 未執行")

    def _resend_end(self, req, st, source, post_end_error=None):
        """
        G-1／G-2／G-2b 補寫流程：本請求已完成結束（ended 已落盤）→ 只檢查並補齊結束一致性、寫處理結果。
          不重新取樣、不讀 HMI、不寫 end_begin、不重產報告、不改 cert_state.json。
          end 事件：嚴格讀法確認不存在才補登一次（t＝原 ended_at，backfilled＝true）；events 無法確認 → 不補登（unknown）。
          active.json：只在指向本認證時清除；讀不到 → 不寫（unknown）。報告：_end_report_state。
          任何一項異常 → 結果訊息以「⚠ 結束已完成，但…」開頭。
        """
        rid, cid = req["request_id"], req.get("cert_id")
        folder = os.path.join(self.root, cid)
        th = self._env_thread
        if th is not None and th.is_alive() and self._env_folder == folder:
            th.join(CFG.CERT_ENV_START_WAIT_SEC)        # 剛開始就結束：起始環控快照也寫 events.jsonl → 等它寫完（有上限）
        warns = []
        if post_end_error:
            warns.append(f"結束收尾後段發生例外（{post_end_error}）")
        rep = self._end_report_state(folder, st)
        evs, probs = _read_jsonl_strict(os.path.join(folder, "events.jsonl"))
        if probs:
            end_event = "unknown"
            warns.append("events.jsonl 無法確認（" + "；".join(probs)[:200] + "），未補登 end 事件")
        elif any(_is_end_of(e, rid) for e in evs):
            end_event = "present"
        else:
            try:
                t_end = _parse(st.get("ended_at"))
            except (TypeError, ValueError):
                t_end = None
            if t_end is None:
                end_event = "unknown"
                warns.append("ended_at 無法判讀，未補登 end 事件")
            else:
                try:
                    self._end_event("end", f"結束認證（請求 {rid}）（補登：原 end 事件缺少）", t_end, request_id=rid,
                                    folder=folder, backfilled=True, backfilled_at=_iso(self.clock()))
                    end_event = "backfilled"
                    warns.append("end 事件原本缺少，已補登")
                except Exception as e:                  # noqa: BLE001
                    end_event = "backfill_failed"
                    warns.append(f"end 事件缺少且補登失敗（{type(e).__name__}: {e}）")
        ap = os.path.join(self.root, "active.json")
        if not os.path.exists(ap):
            active_json = "none"
        else:
            a, a_ok = self._read_json_tries(ap)
            if not a_ok or not isinstance(a, dict):
                active_json = "unknown"
                warns.append("active.json 讀不到或內容無效，未清除")
            elif a.get("cert_id") == cid:
                try:
                    self._set_active(None)
                    active_json = "cleared"
                except Exception as e:                  # noqa: BLE001
                    active_json = "clear_failed"
                    warns.append(f"active.json 清除失敗（{type(e).__name__}: {e}）")
            elif a.get("cert_id") in (None, ""):
                active_json = "none"
            else:
                active_json = "other"
        rs = rep["report_state"]
        if rs == "none":
            warns.append("沒有結束報告紀錄（資料已保留，可用選單 25 重產）")
        elif rs == "failed":
            warns.append(f"結束報告產生失敗（{rep['note']}；資料已保留，可用選單 25 重產）")
        elif rs == "unconfirmed":
            warns.append(f"報告狀態無法確認（{rep['note']}）")
        running = st.get("incomplete")
        msg = ((END_RESEND_WARN + "；".join(warns) + "。") if warns else "") \
            + f"已結束認證 {cid} 的記錄（補寫處理結果；未重新取樣、未重新產生報告）。" \
            + (f"⚠ {running}。" if running else "") + (f"報告：{rep['report']}" if rs == "ok" else "")
        data = {"incomplete": running, "report": rep["report"], "report_ok": rs == "ok", "resent": True,
                "ended_confirmed": True, "ended_source": source, "report_state": rs, "report_note": rep["note"],
                "end_event": end_event, "active_json": active_json}
        if rep.get("later_report_error"):
            data["later_report_error"] = rep["later_report_error"]
            msg += f"（之後另有產報失敗紀錄：{rep['later_report_error'].get('error')}）"
        if rep["partial_files"]:
            data["partial_files"] = rep["partial_files"]
        if isinstance(st.get("end_recovery"), dict):
            data["end_recovery"] = st["end_recovery"]
        if post_end_error:
            data["post_end_error"] = post_end_error
        REQ.write_result(self.root, req, "done", msg, cert_id=cid, data=data)
        with self._lock:
            if self.cert is not None and self.cert.get("cert_id") == cid and self.cert.get("status") == "ended":
                self._reset()

    def _end_report_state(self, folder, st):
        """
        G-2：結束報告狀態（只讀；不開啟活頁簿檢查內容）。「成功的結束報告」必須同時符合：
          cert_state.reports 的 file 與 at 都存在；at 與 ended_at 都能解析且 at 不早於 ended_at；
          檔名 .xlsx、不是 .partial.xlsx、不帶 _中途；實際檔案存在、是一般檔案、大小 > 0。
        last_report_error：產報成功時清為 null、失敗時寫入 → 非 null 代表比所有成功紀錄都晚。
        report_state：ok（有成功的結束報告；之後另有錯誤 → later_report_error）／failed（沒有成功、ended_at 之後有錯誤
          或留有暫存檔）／none（結束後沒有紀錄、沒有錯誤、沒有暫存檔）／unconfirmed（任何無法確認）。無法確認一律不是 ok。
        """
        out = {"report_state": "unconfirmed", "report": None, "note": "", "partial_files": []}
        try:
            ended = _parse(st.get("ended_at"))
        except (TypeError, ValueError):
            ended = None
        if ended is None:
            out["note"] = "ended_at 無法判讀"
            return out
        reps, err = st.get("reports"), st.get("last_report_error")
        reps = [] if reps is None else reps
        if not isinstance(reps, list) or (err is not None and not isinstance(err, dict)):
            out["note"] = "reports／last_report_error 格式錯誤"
            return out
        rdir = os.path.join(folder, "reports")
        try:
            for fn in (sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []):
                p = os.path.join(rdir, fn)
                if fn.endswith(".partial.xlsx") and datetime.fromtimestamp(os.path.getmtime(p)) >= ended:
                    out["partial_files"].append(os.path.relpath(p, folder))
        except OSError as e:
            out["note"] = f"reports 資料夾讀不到（{type(e).__name__}）"
            return out
        ok_path, problems = None, []
        for r in reps:
            if not isinstance(r, dict) or not r.get("file") or not r.get("at"):
                problems.append("報告紀錄格式錯誤")
                continue
            try:
                at = datetime.strptime(str(r["at"]), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                problems.append(f"報告紀錄時間無法判讀（{r['at']}）")
                continue
            fn = os.path.basename(str(r["file"]))
            if at < ended or "_中途" in fn:
                continue                                # 結束前（中途）的報告不是結束報告
            p = os.path.join(folder, str(r["file"]))
            if not fn.endswith(".xlsx") or fn.endswith(".partial.xlsx"):
                problems.append(f"報告紀錄檔名異常（{fn}）")
                continue
            try:
                good = os.path.isfile(p) and os.path.getsize(p) > 0
            except OSError:
                good = False
            if not good:
                problems.append(f"報告紀錄的檔案不存在或大小為 0（{fn}）")
                continue
            ok_path = p
        if problems:
            out["note"] = "；".join(problems)[:300]
            return out
        if ok_path:
            out.update(report_state="ok", report=ok_path, note="")
            if err:
                out["later_report_error"] = err
            return out
        err_after = False
        if err:
            try:
                err_after = datetime.strptime(str(err.get("at")), "%Y-%m-%d %H:%M:%S") >= ended
            except ValueError:
                out["note"] = "last_report_error 時間無法判讀"
                return out
        if err_after or out["partial_files"]:
            out.update(report_state="failed",
                       note="；".join(([f"產報失敗：{err.get('error')}"] if err_after else [])
                                     + ([f"留有暫存檔 {'、'.join(out['partial_files'])}"] if out["partial_files"] else [])))
            return out
        if SUP.has_supplements(folder):
            out["note"] = "有事後補拍：之後的報告紀錄記在 supplements，不在 cert_state → 無法確認"
            return out
        out.update(report_state="none", note="結束後沒有報告紀錄")
        return out

    def _stage_keys(self, folder):
        rt = self if folder == self.folder else self._offline(folder)
        res = rt.analysis()
        return res, {_iso(st.start.t): st for st in res.stages}

    def _offline(self, folder):
        # G6：已結束的認證 → 歷史分析一律使用該 Session 原始參數（cert_state.params）；缺少／不合法 → SessionParamsError
        #     （不以目前設定或預設值補齊）。記錄中的認證 → 維持既有行為（self.p，與 live runtime 一致）。
        st = REQ.read_json(os.path.join(folder, "cert_state.json"))
        params = self.p
        if isinstance(st, dict) and st.get("status") == "ended":
            sp, errs, _notes = session_params_check(st)
            if errs:
                raise SessionParamsError(f"認證 {st.get('cert_id') or os.path.basename(folder)} 的原始參數缺少或不合法："
                                         + "；".join(errs))
            params = sp
        rt = CertRuntime(self.root, reader=None, params=params, enable_shots=False, clock=self.clock, log=self.log)
        rt.folder = folder
        rt.cert = st
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
        try:
            res, keys = self._stage_keys(folder)
        except SessionParamsError as e:                 # G6：歷史參數不合法 → 明確拒絕，不寫 decisions
            REQ.write_result(self.root, req, "rejected", f"{e} → 不能分析階段，未寫入")
            return
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

    # ---- G6 stage_count：階段計次／不計次（H2 規則與命名；只寫 decisions，不改原始資料與 Stage.no）---------------
    @staticmethod
    def _stage_count_content(rec):
        return {"stage_start": rec.get("stage_start"), "counted": rec.get("counted"),
                "reason": rec.get("reason", ""), "note": rec.get("note", "")}

    def _stage_count_dup(self, folder, req):
        """decisions.jsonl 中與本請求同一 request_id 的決策（檔案行序第一筆）；沒有 → None。"""
        rid = req.get("request_id")
        for d in _read_jsonl(os.path.join(folder, "decisions.jsonl")):
            if isinstance(d, dict) and d.get("request_id") == rid:
                return d
        return None

    def _stage_count_resolve_dup(self, req, dup):
        """同一 request_id 已有決策：內容相同 → 只補寫處理結果（done，不重複寫入）；內容不同 → 拒絕（不寫入、不覆寫）。"""
        rid = req.get("request_id")
        if dup.get("type") == "stage_count" and \
                self._stage_count_content(dup) == self._stage_count_content(req.get("payload") or {}):
            REQ.write_result(self.root, req, "done",
                             f"同一請求 {rid} 的計次決策已於 {dup.get('at')} 寫入 → 不重複寫入（只補寫處理結果）",
                             data={"duplicate": True})
        else:
            REQ.write_result(self.root, req, "rejected",
                             f"request_id {rid} 已有內容不同的決策 → 拒絕，未寫入、未覆寫原決策")

    def _h_stage_count(self, req):
        folder, err = self._check_cert(req, need_active=False)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        cid = req["cert_id"]
        with self._lock:                                # 只拒絕 HOLD 中的目標認證（不改 HOLD 狀態、不觸發恢復）
            held = self._halt is not None and self._halt.get("cert_id") == cid
        if held:
            REQ.write_result(self.root, req, "rejected",
                             f"認證 {cid} 結束未完成（HOLD）→ 不能設定計次；請先用選單 22 重新結束")
            return
        pl = req.get("payload") or {}
        key, counted = pl.get("stage_start"), pl.get("counted")
        reason, note = pl.get("reason", ""), pl.get("note", "")
        if not (isinstance(key, str) and isinstance(counted, bool) and isinstance(reason, str) and isinstance(note, str)):
            REQ.write_result(self.root, req, "rejected",
                             "請求內容不合法（stage_start 必須是字串、counted 必須是布林值、reason／note 必須是字串）")
            return
        dup = self._stage_count_dup(folder, req)        # 重送／重啟後重新處理：不重複寫入
        if dup is not None:
            self._stage_count_resolve_dup(req, dup)
            return
        try:
            res, keys = self._stage_keys(folder)        # 服務端重新分析（目前認證＝self；已結束＝Session 原始參數）
        except SessionParamsError as e:
            REQ.write_result(self.root, req, "rejected", f"{e} → 不能分析階段，未寫入")
            return
        st = keys.get(key)
        if st is None:
            REQ.write_result(self.root, req, "rejected", f"找不到階段（開始時間 {key}）")
            return
        exp = pl.get("expect")                          # 選單看到的階段識別（編號／方向／結束時間）→ 與服務端必須一致
        if exp is not None and not (isinstance(exp, dict) and exp.get("no") == st.no and exp.get("dir") == st.dir
                                    and exp.get("end") == _iso(st.A.t)):
            REQ.write_result(self.root, req, "rejected",
                             "選單顯示的階段與服務端分析結果不一致（編號／方向／結束時間）→ 無法可靠確認目標階段，未寫入")
            return
        rec = {"type": "stage_count", "stage_start": key, "counted": counted, "reason": reason, "note": note}
        why = _count_invalid(st, rec)                   # H2 有效條件（唯一實作）：已結束、標記「中止」、原因非空白
        if why:
            REQ.write_result(self.root, req, "rejected", f"不能設定{'計次' if counted else '不計次'}：{why}（未寫入）")
            return
        if st.counted == counted:
            REQ.write_result(self.root, req, "rejected",
                             f"階段目前已是「{'計次' if counted else '不計次'}」→ 無需設定（未寫入）")
            return
        name = stage_names(res.stages)[st.no]
        self._decide(req, folder, rec)
        REQ.write_result(self.root, req, "done",
                         f"已將階段 {key}（{name}）設為「{'計次' if counted else '不計次'}」"
                         "（只影響報告階段名稱與計次；原始資料不變；已產生的報告不會自動更新，請用「25 重新產生認證報告」）")

    def _h_confirm_item(self, req):
        folder, err = self._check_cert(req, need_active=False)
        if err:
            REQ.write_result(self.root, req, "rejected", err)
            return
        pl = req.get("payload") or {}
        if pl.get("choice") not in CHOICES:
            REQ.write_result(self.root, req, "rejected", "確認結果必須是 merge（同一階段）或 split（拆成兩個階段）")
            return
        try:
            res, _keys = self._stage_keys(folder)
        except SessionParamsError as e:                 # G6：歷史參數不合法 → 明確拒絕，不寫 decisions
            REQ.write_result(self.root, req, "rejected", f"{e} → 不能分析階段，未寫入")
            return
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
        res = self.analysis()                           # r2c：分析不持 runtime 鎖（不擋取樣）；排入截圖才持鎖
        with self._lock:
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
        try:
            res = self._offline(folder).analysis()      # G6：已結束認證 → Session 原始參數
        except SessionParamsError as e:
            REQ.write_result(self.root, req, "rejected", f"{e} → 不能分析階段，未排入事後補拍")
            return
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
        # G6：參數來源（只顯示於結果訊息）；已結束認證 → 先獨立驗證 Session 原始參數（不合法 → 拒絕，不產報）
        with self._lock:
            cur = self.cert is not None and folder == self.folder
        st = None if cur else REQ.read_json(os.path.join(folder, "cert_state.json"))
        if not cur and isinstance(st, dict) and st.get("status") == "ended":
            _sp, errs, _notes = session_params_check(st)
            if errs:
                REQ.write_result(self.root, req, "rejected",
                                 f"認證 {req['cert_id']} 的原始參數缺少或不合法：{'；'.join(errs)} → 不重新產生報告",
                                 data={"param_errors": errs})
                return
            src = "Session 原始參數（cert_state.params）"
        else:
            src = "服務目前載入的參數（記錄中認證，維持既有行為）"
        ok, msg, path = self.generate_report(req["cert_id"])
        REQ.write_result(self.root, req, "done" if ok else "failed",
                         f"已產生新報告：{path}（分析參數來源：{src}）" if ok
                         else f"報告產生失敗：{msg}（資料已保留，可再重產）",
                         data={"report": path, "params_source": src})

    # ---- 報告 ----------------------------------------------------------------
    def generate_report(self, cid):
        """產生新報告檔（不覆蓋舊檔）。回傳 (ok, 訊息, 路徑)。失敗不動資料。"""
        from cert_report_xlsx import save_report
        from cert_screenshot import load_manifest
        folder = os.path.join(self.root, cid)
        # BUG-1：狀態來源 —— 目前認證用鎖內記憶體快照；其他認證讀磁碟且必須可確認，否則不產報、不寫入任何檔案。
        # BUG-2：HOLD（結束未完成）→ 不產報（否則會把未完成收尾的狀態寫進磁碟）。
        with self._lock:
            cur = self.cert is not None and folder == self.folder
            if cur and self._halt is not None:
                return (False, f"認證 {cid} 結束未完成（HOLD，已停止取樣）→ 不產生報告；請先用選單 22 重新結束", None)
            st_snap = _json_copy(self.cert) if cur else None
        if not cur:
            st_snap = REQ.read_json(os.path.join(folder, "cert_state.json"))
            if not (isinstance(st_snap, dict) and st_snap.get("cert_id") == cid
                    and st_snap.get("status") in ("recording", "ended")):
                return (False, f"無法確認認證 {cid} 的 cert_state.json（讀不到、內容無效或 cert_id 不符）"
                               "→ 不產生報告、不改寫狀態", None)
        try:
            rt = self if cur else self._offline(folder)
        except SessionParamsError as e:                 # G6：已結束認證的原始參數缺少／不合法 → 不產報、不寫入任何檔案
            return False, f"{e} → 不產生報告、不改寫狀態", None
        with self._lock:
            if self._report_busy:
                return False, "另一份報告正在產生中", None
            self._report_busy = True
        part = None
        try:
            st = st_snap
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
            er = st.get("end_recovery") if isinstance(st.get("end_recovery"), dict) else None
            notes = [(f"認證測試 {cid}：背景服務自 {st.get('started_at')} 起持續取樣（含休息），"
                      f"至 {st.get('ended_at') or '（記錄中）'}；共 {len(rt.records)} 筆。") if er is None else
                     # M9：延遲收尾 → 分開標示資料截止與收尾時間（不以收尾時間代替最後取樣；正常結束文字不變）
                     (f"認證測試 {cid}：背景服務自 {st.get('started_at')} 起持續取樣（含休息），"
                      f"資料至最後一筆紀錄 {er.get('last_recorded_sample_at') or '不明'}"
                      f"（最後一筆有效 HMI 資料 {er.get('last_valid_sample_at') or '不明'}）；共 {len(rt.records)} 筆。"
                      f"結束要求 {er.get('first_end_requested_at') or '不明'}；結束收尾未能即時完成"
                      f"（{'服務重啟後由人工重新結束' if er.get('mode') == 'after_restart' else '同一程序內重新結束'}），"
                      f"實際完成收尾 {er.get('completed_at') or '不明'}；其間沒有取樣、不補資料。"),
                     "原始讀值照存：不沿用上一筆、不補 0、不內插；讀取失敗或欄位無效 → 該分鐘 N/A、曲線斷線。",
                     params_note(st.get("params")) + "。",
                     "傳感器溫度／PCS 模組溫度取自 6161 /api/temperature（st／pt.moduleTemperature），對應關係待實機確認。",
                     "停止指令時間（Tc）取自控制結果檔的快照（寫檔時間；該檔只保留最近一次，非完整歷史）。"]
            rd_note = hmi_reader_note(st)                   # r2c O-1：只在 cert_state 有讀取模式紀錄時加註（舊 Session 不變）
            if rd_note:
                notes.append(rd_note)
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
            # M-S2 r2：有 slots.jsonl（1 秒格點認證）→ Raw Data 每秒一列（整筆沿用）＋格點品質指標寫入描述；
            #          歷史認證（沒有 slots.jsonl）Raw Data 維持原格式、不推算
            ss = build_sampling_slots(folder)
            if ss is not None:
                meta["sampling_slots"] = ss
                mm = ss["metrics"]
                pct = lambda v: "N/A" if v is None else f"{v}%"
                q_note = (f"格點品質：預定格點 {mm['expected_slots']}；格點紀錄覆蓋率 {mm['coverage_text']}；"
                          f"實際執行讀取率 {pct(mm['attempt_pct'])}；"
                          f"HMI 實際成功率（M-1，分母＝預定格點）{pct(mm['m1_pct'])}；"
                          f"完整即時資料率 {pct(mm['complete_live_pct'])}；讀取失敗 {mm['read_fail']} 格；"
                          f"未執行讀取（MISSED）{mm['missed']} 格；推定缺格 {mm['inferred_missed']} 格；"
                          f"格點紀錄遺失 {mm['slot_lost']} 格；原始紀錄寫入失敗 {mm['obs_write_failed']} 格；"
                          f"段落 {mm['segments']}（未正常結束 {mm['unterminated_segments']}）；"
                          f"服務停止／重啟 {mm['service_down_count']} 次共 {mm['service_down_sec']} 秒；"
                          f"無法解析的紀錄行 {mm['raw_unparseable']}／{mm['slot_log_unparseable']}。")
                notes.insert(min(len(notes), 3 + len(self.extra_notes) + (1 if st.get("incomplete") else 0)
                                 + (1 if st.get("status") == "recording" else 0)), q_note)
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
            if cur:
                st = {}                                 # 目前認證：_write_state_any 只合併 last_report_error 到記憶體狀態
            else:
                st = REQ.read_json(os.path.join(folder, "cert_state.json"))
                if not (isinstance(st, dict) and st.get("cert_id") == cid):
                    st = None                           # BUG-1：讀不到／內容不符 → 不寫入（不得以不完整內容覆寫）
            if st is not None:
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
        # r2c：分析在取 runtime 鎖之前完成（計算不持鎖 → 不擋取樣）；期間認證世代改變 → 本次不輸出階段資訊（不混用）
        res = an_err = None
        with self._lock:
            gen, has_cert = self._gen, self.cert is not None
        a0 = a1 = time.perf_counter()
        hit = None
        if has_cert:
            try:
                res = self._status_analysis()
                hit = self._last_analysis_hit
            except Exception as e:                      # noqa: BLE001
                an_err = e
            a1 = time.perf_counter()
        with self._lock:
            s = {"updated_at": REQ.now_str(self.clock()), "pid": os.getpid(), "owner_role": owner_role,
                 "active": self.is_active(), "cert_id": self.cert["cert_id"] if self.cert else None}
            if self._halt is not None:                  # BUG-2：HOLD（顯示用；不參與任何判斷）
                s["halted"] = _json_copy(self._halt)
            if self.cert:
                s.update(started_at=self.cert.get("started_at"), sample_count=len(self.records),
                         last_sample_time=self.cert.get("last_sample_time"),
                         last_sample_ok=self.cert.get("last_sample_ok"),
                         test_info=self.cert.get("test_info"),
                         interruptions=len(self.cert.get("interruptions") or []))
                if self._hl is not None and self.is_active():
                    s["heat_link"] = self._hl.status()
                try:
                    if an_err is not None:
                        raise an_err
                    if res is not None and self._gen == gen:     # 期間切換認證 → 本次略過階段資訊（下一輪補上）
                        if self.is_active():
                            self._note_status(a0, a1, hit)
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
