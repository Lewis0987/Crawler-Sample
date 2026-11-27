# -*- coding: utf-8 -*-
"""
跨 Session 整合認證報告 —— 分析、配對與確認（docs/Cert_Merge_Report_Design.md §6、§7、§8）
==========================================================================================
每個 Session 獨立呼叫 cert_stage_engine.analyze（只讀快照載入的資料），再依凍結規格計算：
  原始階段 ID、標記歷史與有效標記（P-7）、配對（P-4）、配對確定性（P-20：A1＋A2）、確認狀態（P-7）、
  對應不到的紀錄（P-9）、計數不變式 I1～I8（P-8）、時間量與數值量（P-22）、可比性、版本特徵（§8）。
不 import cert_runtime；觀測轉換與命令轉換依 cert_runtime.record_to_obs／_analysis_core 在此重建（N-3）。
"""
import re
from collections import Counter
from datetime import datetime

import cert_report_config as CFG
from cert_report_xlsx import _stage_title, has_pcs_temp, is_report_valid
from cert_stage_engine import Obs, analyze, minute_value, rest_between, stage_minutes, stage_names

ISO = "%Y-%m-%dT%H:%M:%S"
LABELS = ("滿充", "滿放", "未滿", "中止")
LABEL_WRONG_DIR = {"C": "滿放", "D": "滿充"}
CHOICES = ("merge", "split")
DIR_NAME = {"C": "充電", "D": "放電"}
OPP = {"C": "D", "D": "C"}

# 配對結果（P-4）
PAIR_FIRST, PAIR_SECOND = "已配對（前半）", "已配對（後半）"
UNP_HEAD, UNP_TAIL = "開頭半循環（未配對）", "結尾半循環（未配對）"
UNP_SAME, UNP_NOPREV = "未配對：其後為同向階段", "未配對：前無可配的前半階段"
# 配對確定性（P-20）
CERTAIN, PAIR_PENDING = "確定", "循環配對待確認"
# 確認狀態（P-7）
ST_ABORT, ST_INCOMPLETE, ST_NOTFULL, ST_PENDING, ST_DONE = "中止", "未完成", "未滿", "待確認", "確認完成"
STATUSES = (ST_DONE, ST_NOTFULL, ST_ABORT, ST_PENDING, ST_INCOMPLETE)


def _iso(t):
    return t.strftime(ISO) if t else None


def _parse(s):
    return datetime.strptime(s, ISO) if s else None


def _parse_any(s):
    try:
        return _parse(s)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(s).replace(" ", "T")) if s else None
        except ValueError:
            return None


# =============================================================================
# 參數與觀測（N-3：對照 cert_runtime）
# =============================================================================
# r8（G5-B 決策 B，方案 a）：依 G6 已驗收規則在此重建（不 import cert_runtime、不新增中立模組）。
# 下列常數與 _finite_num／_param_ok／session_params_check 逐字對照 cert_runtime.py（雙邊行為一致性測試＋原始碼文字比對）。
REQUIRED_PARAMS = ("W_PAUSE_SEC", "TCONF_SEC", "GAP_G_SEC", "IDLE_CURRENT_A", "IDLE_POWER_KW",
                   "CMD_STAGE_MATCH_SEC", "STOP_WARN_SEC", "SENSOR_VALID_RANGE", "SAMPLE_INTERVAL_SEC")
_PARAM_RULE = {"W_PAUSE_SEC": "gt0", "TCONF_SEC": "gt0", "GAP_G_SEC": "gt0", "IDLE_CURRENT_A": "ge0",
               "IDLE_POWER_KW": "ge0", "CMD_STAGE_MATCH_SEC": "ge0", "STOP_WARN_SEC": "gt0", "SAMPLE_INTERVAL_SEC": "gt0"}
COMPAT_PARAMS = {"MINUTE_TOL_SEC": "ge0"}       # 相容參數：分析不讀取；缺少只提示，存在時仍須合法
_RULE_TEXT = {"gt0": "大於 0", "ge0": "大於等於 0"}
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


class SessionParamsInvalid(ValueError):
    """r8：Session 原始參數缺少或不合法（前置驗證拒絕；不以 CFG.params() 或預設值補齊）。"""


def session_params(state):
    """
    r8（取代 r7 D2 的「缺少的鍵 → 程式預設值」）：依 session_params_check 驗證；不合法 → SessionParamsInvalid。
    分析使用 Session 原始參數中程式已知的鍵（不補值）；未知額外鍵保留於 cert_state 與 params_info（不參與分析）。
    """
    sp, errs, notes = session_params_check(state)
    if errs:
        raise SessionParamsInvalid("；".join(errs))
    base = CFG.params()
    p = {k: v for k, v in sp.items() if k in base}
    return p, {"missing_keys": sorted(k for k in base if k not in sp), "notes": notes,
               "differs_from_default": {k: (sp[k], base[k]) for k in base if k in sp and sp[k] != base[k]},
               "unknown_keys": sorted(k for k in sp if k not in base)}


def record_to_obs(rec, n):
    """對照 cert_runtime.record_to_obs。"""
    return Obs(t=_parse(rec["t"]), ok=bool(rec.get("ok")), values=dict(rec.get("values") or {}),
               src=f"observations.jsonl:{n}", fail_reason=rec.get("fail_reason", ""),
               flags=dict(rec.get("flags") or {}), notes=list(rec.get("notes") or []))


def commands_for_engine(commands):
    """對照 cert_runtime._analysis_core：只採 control_success is True 且有 cmd 的紀錄。"""
    return [{"t": _parse(c["t"]), "cmd": c["cmd"]} for c in commands
            if c.get("cmd") and c.get("control_success") is True]


def names_full(res):
    nm = stage_names(res.stages)
    return {st.no: _stage_title(nm, st) for st in res.stages}


# =============================================================================
# 單一 Session 分析
# =============================================================================
def analyze_session(data, s_no):
    st = data["state"]
    cid = st["cert_id"]
    p, pinfo = session_params(st)
    obs = [record_to_obs(r, i + 1) for i, r in enumerate(data["records"])]
    res = analyze(obs, p, list(data["decisions"]), commands_for_engine(data["commands"]))
    nf = names_full(res)
    nm = stage_names(res.stages)
    stages = []
    for i, s in enumerate(res.stages):
        stages.append({"id": f"{cid}#ST{s.no}", "cert_id": cid, "s_no": s_no, "idx": i, "no": s.no, "dir": s.dir,
                       "name": nm[s.no], "stage": s, "stage_start": _iso(s.start.t)})
    out = {"cert_id": cid, "s_no": s_no, "state": st, "params": p, "params_info": pinfo, "res": res,
           "names_full": nf, "stages": stages, "data": data}
    _labels(out)
    _counts(out)
    _orphans(out)
    _pauses(out)
    _metrics(out)
    return out


def _counts(sa):
    """
    r8：stage_count 決策（只讀 H2 analyze() 的結果；不另建有效性規則）。
      每個階段：count_counted（H2 Stage.counted）、count_reason、count_note、count_history（H2 摘要複本）；
      全部 stage_count 紀錄（行序、stage_start、at、by、request_id、counted、原因）；
      request_id 重複（不分決策類型）→ 列為異常（可追溯性不得判為已確認）。
    """
    for s in sa["stages"]:
        st = s["stage"]
        s["count_counted"] = getattr(st, "counted", True)
        s["count_reason"] = getattr(st, "count_reason", "")
        s["count_note"] = getattr(st, "count_note", "")
        s["count_history"] = list(getattr(st, "count_history", None) or [])
    recs, seen, dup = [], {}, []
    for ln, d in enumerate(sa["data"]["decisions"], 1):
        rid = d.get("request_id")
        if rid:
            if rid in seen:
                dup.append({"request_id": rid, "lines": [seen[rid], ln]})
            else:
                seen[rid] = ln
        if d.get("type") == "stage_count":
            recs.append({"line": ln, "stage_start": d.get("stage_start"), "counted": d.get("counted"),
                         "reason": d.get("reason"), "note": d.get("note", ""), "at": d.get("at"), "by": d.get("by"),
                         "request_id": rid})
    sa["count_records"] = recs
    sa["count_orphans"] = list(getattr(sa["res"], "count_orphans", None) or [])
    sa["decision_anomalies"] = [dict(x, kind="request_id 重複") for x in dup]


def _labels(sa):
    """P-7：標記歷史、每筆有效性、有效標記（最後一筆；無效不回退）、時間順序異常。"""
    by_start = {s["stage_start"]: s for s in sa["stages"]}
    hist = {}
    for ln, d in enumerate(sa["data"]["decisions"], 1):
        if d.get("type") != "stage_label":
            continue
        s = by_start.get(d.get("stage_start"))
        lab = d.get("label")
        if s is None:
            validity = "無法對應"
        elif lab not in LABELS:
            validity = "無效：未知值"
        elif LABEL_WRONG_DIR.get(s["dir"]) == lab:
            validity = "無效：方向不符"
        else:
            validity = "有效"
        rec = {"line": ln, "at": d.get("at"), "request_id": d.get("request_id"), "stage_start": d.get("stage_start"),
               "label": lab, "note": d.get("note", ""), "validity": validity}
        hist.setdefault(d.get("stage_start"), []).append(rec)
    sa["label_history"] = hist
    for s in sa["stages"]:
        h = hist.get(s["stage_start"], [])
        s["label_count"] = len(h)
        s["engine_label"] = s["stage"].label or None
        s["effective_label"], s["label_reasons"] = None, []
        if h:
            last = h[-1]
            if last["validity"] == "有效":
                s["effective_label"] = last["label"]
            else:
                s["label_reasons"].append(f"最新標記無效（{last['validity']}：{last['label']}）")
            ats = [_parse_any(r["at"]) for r in h]
            if any(a is None for a in ats) or any(b < a for a, b in zip(ats, ats[1:])):
                s["label_reasons"].append("標記紀錄時間順序異常")
                s["label_order_anomaly"] = True


def _orphans(sa):
    """P-9：對應不到的標記、停頓決策、無法辨識的紀錄（全部列出）。"""
    starts = {s["stage_start"] for s in sa["stages"]}
    items = {pz.item_id for pz in sa["res"].pauses}
    # r8：stage_count 對不到階段 → 以 H2 Result.count_orphans 為準（對應成功的不列為孤兒；不另判有效性）
    count_orphan_keys = {o.get("stage_start") for o in (getattr(sa["res"], "count_orphans", None) or [])}
    out = []
    for ln, d in enumerate(sa["data"]["decisions"], 1):
        typ = d.get("type", "continuity")
        t = None
        if typ == "stage_count":
            if d.get("stage_start") not in count_orphan_keys:
                continue
            kind = "無法對應的計次決策"
            ts = _parse_any(d.get("stage_start")) if isinstance(d.get("stage_start"), str) else None
            t = (ts, ts)
        elif typ == "stage_label":
            if d.get("stage_start") in starts:
                continue
            kind = "無法對應的標記"
            t = (_parse_any(d.get("stage_start")), _parse_any(d.get("stage_start")))
        elif typ == "continuity":
            if d.get("item_id") in items:
                continue
            kind = "無法對應的停頓決策"
            m = re.fullmatch(r"U-(\d{14})-(\d{14})", str(d.get("item_id") or ""))
            if m:
                t = (datetime.strptime(m.group(1), "%Y%m%d%H%M%S"), datetime.strptime(m.group(2), "%Y%m%d%H%M%S"))
        else:
            kind = "無法辨識的紀錄"
        if t is not None and (t[0] is None or t[1] is None):
            t = None
        out.append({"line": ln, "kind": kind, "at": d.get("at"), "request_id": d.get("request_id"),
                    "content": d, "span": [_iso(t[0]), _iso(t[1])] if t else None, "_span": t})
    sa["orphans"] = out


def _pauses(sa):
    """不確定停頓（含所屬階段）；未決＝certain=False 且沒有 merge／split 決策。不推論原因。"""
    out = []
    for pz in sa["res"].pauses:
        if pz.certain:
            continue
        ch = (pz.decision or {}).get("choice")
        out.append({"item_id": pz.item_id, "stage_no": pz.stage_no,
                    "stage_id": f"{sa['cert_id']}#ST{pz.stage_no}" if pz.stage_no else None,
                    "A": _iso(pz.A.t), "R": _iso(pz.R.t), "A_to_R_sec": (pz.R.t - pz.A.t).total_seconds(),
                    "system_judgment": pz.system_judgment, "decision": ch if ch in CHOICES else None,
                    "pending": ch not in CHOICES})
    sa["uncertain_pauses"] = out
    for s in sa["stages"]:
        s["pending_pauses"] = sum(1 for z in out if z["stage_no"] == s["no"] and z["pending"])
        s["certain_pauses"] = sum(1 for pz in s["stage"].pauses if pz.certain)
        s["uncertain_pauses"] = sum(1 for z in out if z["stage_no"] == s["no"])


def _metrics(sa):
    """P-22：觀測運行跨度、SOC 起訖、溫度／電壓極值（與重產明細階段頁同一算法）。"""
    res, p, nf = sa["res"], sa["params"], sa["names_full"]
    valid = lambda o: is_report_valid(o, p, nf)           # noqa: E731
    prefer = lambda o: has_pcs_temp(o, p)                 # noqa: E731
    for s in sa["stages"]:
        st = s["stage"]
        span = (st.A.t - st.start.t).total_seconds()
        exact = st.start_certain and st.end_certain
        s["span_sec"] = span if exact else None
        s["span_lower_sec"] = None if exact else span
        s["soc_start"] = (st.start.values or {}).get("soc")
        s["soc_end"] = (st.A.values or {}).get("soc")
        rows = stage_minutes(res, st, valid=valid, ext_scope="minute", prefer=prefer)
        ext = {}
        for key, fn in (("rack_max_c", max), ("rack_min_c", min), ("cell_vmax_v", max), ("cell_vmin_v", min)):
            vals = [v for v in (minute_value(mr, key, p) for mr in rows) if v is not None]
            ext[key] = fn(vals) if vals else None
        s["extremes"] = ext


# =============================================================================
# 配對（P-4）、配對確定性（P-20）、確認狀態（P-7）
# =============================================================================
UNC_EXCLUDED = "不計次（不參與配對）"


def is_uncounted(s):
    """
    r8 選項甲套用條件：H2 引擎判定的有效 stage_count＝false（Stage.counted is False）且整合判定的有效標記＝「中止」。
    任一不成立 → 照現行規則配對（不在 C 重新定義計次有效性）。
    """
    return getattr(s["stage"], "counted", True) is False and s.get("effective_label") == "中止"


# v2-align（2026-11-27 使用者裁定 Q1～Q6）：合併摘要的中止階段排除與新版 v2 單一認證報告一致
PAIRING_RULE = "v2-align"
STOPPED_EXCLUDED = "中止（不參與配對）"
UNP_BEFORE_STOP = "未配對：其後為中止階段（不跨越）"


def is_stopped(s):
    """
    v2-align Q1：整合判定的有效標記＝「中止」→ 排除於有效階段與循環配對；不需要 stage_count 不計次決策。
    有效不計次（is_uncounted）必為中止（H2：counted=false 只在標記中止時有效）→ is_uncounted ⊂ is_stopped；
    is_uncounted 的原語意（命名、原因、決策紀錄、加註）保留，不以本函式取代。
    """
    return s.get("effective_label") == "中止"


def _last_count(st):
    h = list(getattr(st, "count_history", None) or [])
    return h[-1] if h else {}


def pair_session(sa, order):
    """
    P-4 配對（v2-align，使用者裁定 Q1～Q3）：
      · 有效標記中止 → STOPPED_EXCLUDED（不參與配對；原始階段、決策、量測全部保留）；不需要不計次決策。
      · 中止階段＝不可跨越的分界：以中止階段切段，各段依 --cycle-order（first→second）由前往後配對；
        不跨 Session、不跨中止階段；不得先移除中止階段再把原本不相鄰的有效階段配對。
      · 保護①：確定性 A1／A2 仍以「排除中止階段後的有效序列」判斷（可觀察分界另一側的相鄰有效階段，只用於確定性）。
      · 保護②：相鄰同向中止階段（含不計次）交界檢查；保護③：確認狀態（P-7）與其他待確認理由不變。
      · 沒有中止階段 → 與 r7／r8 完全相同。
    """
    first, second = order[0], order[1]
    st = sa["stages"]
    cycles = []
    for s in st:
        s["pairing"], s["cycle_id"] = None, None
    for s in st:
        if is_stopped(s):
            s["pairing"] = STOPPED_EXCLUDED
    seq = [s for s in st if not is_stopped(s)]                     # 保護①用的有效序列（不用於配對）
    for j, s in enumerate(seq):
        s["seq_idx"] = j
    segments, cur = [], []                                         # 以中止階段切段（段內維持原始順序）
    for s in st:
        if is_stopped(s):
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(s)
    if cur:
        segments.append(cur)
    k = 0
    for seg in segments:
        n = len(seg)
        i = 0
        while i < n:
            s = seg[i]
            if s["dir"] == first:
                if i + 1 < n and seg[i + 1]["dir"] == second:
                    k += 1
                    cid = f"{sa['cert_id']}#CY{k}"
                    s["pairing"], seg[i + 1]["pairing"] = PAIR_FIRST, PAIR_SECOND
                    s["cycle_id"] = seg[i + 1]["cycle_id"] = cid
                    cycles.append({"id": cid, "cert_id": sa["cert_id"], "s_no": sa["s_no"], "a": s, "b": seg[i + 1]})
                    i += 2
                    continue
                if i + 1 < n:
                    s["pairing"] = UNP_SAME
                elif s["idx"] + 1 < len(st) and is_stopped(st[s["idx"] + 1]):
                    s["pairing"] = UNP_BEFORE_STOP
                else:
                    s["pairing"] = UNP_TAIL
            else:
                s["pairing"] = UNP_HEAD if s["seq_idx"] == 0 else UNP_NOPREV
            i += 1
    for c in cycles:
        _certainty(c, seq)
        _protect2(c, sa)
        _confirm(c, sa)
    sa["cycles"] = cycles
    sa["stopped_stages"] = [s for s in st if is_stopped(s)]
    sa["uncounted_stages"] = [s for s in st if is_uncounted(s)]
    return cycles


def _certainty(c, seq):
    """P-20（保護①：以排除中止階段（含不計次）後的有效序列判斷；沒有中止階段時與 r7 相同）。"""
    a, b = c["a"], c["b"]
    trig = []
    ia, ib = a["seq_idx"], b["seq_idx"]
    if ia > 0 and seq[ia - 1]["dir"] == a["dir"]:
        prev = seq[ia - 1]["stage"]
        trig.append(f"A1：前半階段之前緊接同向階段 {seq[ia - 1]['id']}（成因 end_by＝{prev.end_by}）")
    if ib + 1 < len(seq) and seq[ib + 1]["dir"] == b["dir"]:
        trig.append(f"A2：後半階段之後緊接同向階段 {seq[ib + 1]['id']}（成因 end_by＝{b['stage'].end_by}）")
    c["certainty"] = PAIR_PENDING if trig else CERTAIN
    c["certainty_triggers"] = trig
    c["uncounted_notes"] = []


def _pending_pause_spans(sa):
    """未決不確定停頓的 [A, R]（H2 Pause.A／Pause.R；未決＝certain=False 且沒有 merge／split 決策）。"""
    out = []
    for pz in getattr(sa["res"], "pauses", None) or []:
        if pz.certain or (pz.decision or {}).get("choice") in CHOICES:
            continue
        a, r = getattr(getattr(pz, "A", None), "t", None), getattr(getattr(pz, "R", None), "t", None)
        out.append((pz.item_id, a, r))
    return out


def junction_concern(x, y, sa):
    """
    r8 保護②（使用者 2026-11-24 裁定）：x＝前一階段、y＝後一階段（同向、相鄰）。符合任一 → 回傳理由清單：
      ① 交界為 operator_split（x.end_by）；② 交界邊界不確定（x.end_certain 或 y.start_certain 為 False）；
      ③ 交界窗口 [A−W, B+W]（A＝x 最後運轉時間、B＝y 開始時間、W＝Session 原始 W_PAUSE_SEC；含端點）
         與任一未決停頓區間 [Pause.A, Pause.R] 有任何重疊。
    時間無法取得 → 不視為沒有疑慮（列為理由）。不推定時間。
    """
    X, Y = x["stage"], y["stage"]
    why = []
    if X.end_by == "operator_split":
        why.append("交界為 operator_split")
    if not X.end_certain or not Y.start_certain:
        why.append("交界邊界不確定（" + "／".join(n for n, ok in (("前一階段 end_certain", X.end_certain),
                                                                ("後一階段 start_certain", Y.start_certain)) if not ok) + "）")
    A, B = getattr(getattr(X, "A", None), "t", None), getattr(getattr(Y, "start", None), "t", None)
    W = (sa.get("params") or {}).get("W_PAUSE_SEC")
    if A is None or B is None or not _finite_num(W):
        why.append("交界時間或 W_PAUSE_SEC 無法取得（不視為沒有疑慮）")
        return why
    from datetime import timedelta
    lo, hi = A - timedelta(seconds=W), B + timedelta(seconds=W)
    for item, pa, pr in _pending_pause_spans(sa):
        if pa is None or pr is None:
            why.append(f"未決停頓 {item} 時間無法取得（不視為沒有疑慮）")
        elif pa <= hi and pr >= lo:
            why.append(f"交界窗口［{_iso(lo)}～{_iso(hi)}］內有未決停頓 {item}")
    return why


def _protect2(c, sa):
    """保護②（r8；v2-align Q5 擴大至中止）：與本循環階段相鄰（原始序列）的同向中止階段（含不計次），
    交界有疑慮 → 本循環仍觸發 A1／A2 並加註。檢查內容（人工分割、邊界不確定、未決停頓／交界窗口）不變。"""
    st = sa["stages"]
    for half in (c["a"], c["b"]):
        i = half["idx"]
        for j, x_first in ((i - 1, True), (i + 1, False)):
            if not (0 <= j < len(st)):
                continue
            u = st[j]
            if not is_stopped(u) or u["dir"] != half["dir"]:
                continue
            kind = "相鄰不計次階段" if is_uncounted(u) else "相鄰中止階段"
            if is_uncounted(u):
                last = _last_count(u["stage"])
                c["uncounted_notes"].append(
                    f"相鄰不計次階段 {u['id']}（開始 {u['stage_start']}；原因：{getattr(u['stage'], 'count_reason', '')}；"
                    f"決策時間 {last.get('at')}；request_id {last.get('request_id')}）")
            else:
                c["uncounted_notes"].append(f"相鄰中止階段 {u['id']}（開始 {u['stage_start']}；有效標記中止；無有效不計次決策）")
            why = junction_concern(u, half, sa) if x_first else junction_concern(half, u, sa)
            if why:
                c["certainty_triggers"].append(
                    f"{'A1' if half is c['a'] else 'A2'}（保護②）：{kind} {u['id']} 可能與 {half['id']} 屬同一階段"
                    f"（{'；'.join(why)}）")
                c["certainty"] = PAIR_PENDING


def _confirm(c, sa):
    a, b = c["a"], c["b"]
    A, B = a["stage"], b["stage"]
    reasons = {ST_ABORT: [], ST_INCOMPLETE: [], ST_NOTFULL: [], ST_PENDING: []}
    labs = (a["effective_label"], b["effective_label"])
    for s in (a, b):
        if s["effective_label"] == "中止":
            reasons[ST_ABORT].append(f"{s['id']} 有效標記為中止")
    bounds = [("E1 前半開始", A.start_certain, A.start_note), ("E2 前半結束", A.end_certain, A.end_note),
              ("E3 後半開始", B.start_certain, B.start_note), ("E4 後半結束", B.end_certain, B.end_note)]
    c["uncertain_bounds"] = [{"bound": n, "note": note} for n, ok, note in bounds if not ok]
    for n, ok, note in bounds:
        if not ok:
            reasons[ST_INCOMPLETE].append(f"{n}不確定（{note}）")
    for s in (a, b):
        if s["stage"].end_by in ("data_end", "operator_split"):
            reasons[ST_INCOMPLETE].append(f"{s['id']} end_by＝{s['stage'].end_by}")
    if (sa["state"] or {}).get("incomplete"):
        reasons[ST_INCOMPLETE].append(f"Session 未完成：{sa['state']['incomplete']}")
    if all(labs) and "未滿" in labs:
        reasons[ST_NOTFULL].append("有效標記含「未滿」")
    for s in (a, b):
        if not s["effective_label"]:
            reasons[ST_PENDING].append(f"{s['id']} 沒有有效標記" + (f"（{'；'.join(s['label_reasons'])}）"
                                                                      if s["label_reasons"] else "（未標記）"))
        elif s.get("label_order_anomaly"):
            reasons[ST_PENDING].append(f"{s['id']} 標記紀錄時間順序異常")
        if s["pending_pauses"]:
            reasons[ST_PENDING].append(f"{s['id']} 有 {s['pending_pauses']} 個未決不確定停頓")
    lo, hi = A.start.t, B.A.t
    for o in sa["orphans"]:
        sp = o.get("_span")
        if sp is None or (sp[0] <= hi and sp[1] >= lo):
            reasons[ST_PENDING].append(f"有可能屬於本循環的{o['kind']}（第 {o['line']} 行）")
    if c["certainty"] == PAIR_PENDING:
        reasons[ST_PENDING].append("循環配對待確認")
    want = ("滿放", "滿充") if a["dir"] == "D" else ("滿充", "滿放")
    if reasons[ST_ABORT]:
        status = ST_ABORT
    elif reasons[ST_INCOMPLETE]:
        status = ST_INCOMPLETE
    elif reasons[ST_NOTFULL]:
        status = ST_NOTFULL
    elif reasons[ST_PENDING]:
        status = ST_PENDING
    elif labs == want and c["certainty"] == CERTAIN:
        status = ST_DONE
    else:
        status = ST_PENDING
        reasons[ST_PENDING].append(f"有效標記組合 {labs} 不是 {want}")
    c["status"] = status
    c["reasons"] = [f"[{k}] {r}" for k in (ST_ABORT, ST_INCOMPLETE, ST_NOTFULL, ST_PENDING) for r in reasons[k]]
    rest, why = rest_between(sa["res"], A, B)
    c["rest_sec"], c["rest_note"] = rest, why
    c["observed_gap_sec"] = (B.start.t - A.A.t).total_seconds()


# =============================================================================
# 計數不變式 I1～I8（P-8）
# =============================================================================
def invariants(sessions):
    stages = [s for sa in sessions for s in sa["stages"]]
    cycles = [c for sa in sessions for c in sa["cycles"]]
    engine_total = sum(len(sa["res"].stages) for sa in sessions)
    paired = [s for s in stages if s["pairing"] in (PAIR_FIRST, PAIR_SECOND)]
    stopped = [s for s in stages if s["pairing"] == STOPPED_EXCLUDED]
    unpaired = [s for s in stages if s["pairing"] not in (PAIR_FIRST, PAIR_SECOND, STOPPED_EXCLUDED)]   # 有效未配對
    valid = [s for s in stages if s["pairing"] != STOPPED_EXCLUDED]
    ids = Counter(x["id"] for c in cycles for x in (c["a"], c["b"]))
    st_cnt = Counter(c["status"] for c in cycles)
    ce_cnt = Counter(c["certainty"] for c in cycles)
    unp_kinds = (UNP_HEAD, UNP_TAIL, UNP_SAME, UNP_NOPREV, UNP_BEFORE_STOP)
    r = {
        "I1": len(stages) == engine_total,
        "I2": len(stages) == len(paired) + len(unpaired) + len(stopped),     # v2-align：原始＝已配對＋有效未配對＋中止排除
        "I3": len(paired) == 2 * len(cycles),
        "I4": len(cycles) == sum(st_cnt.get(k, 0) for k in STATUSES),
        "I5": len(cycles) == ce_cnt.get(CERTAIN, 0) + ce_cnt.get(PAIR_PENDING, 0),
        "I6": all(v == 1 for v in ids.values()),
        "I7": all(c["a"]["cert_id"] == c["b"]["cert_id"] for c in cycles),
        "I8": all(c["certainty"] == CERTAIN for c in cycles if c["status"] == ST_DONE),
        # v2-align：沒有任何循環包含中止階段（新版正常配對不產生中止循環）
        "I9": all(not is_stopped(x) for c in cycles for x in (c["a"], c["b"])),
        # v2-align：不計次階段一定屬於中止排除（不計次為中止排除的子集合，不重複加總）
        "I10": all(s["pairing"] == STOPPED_EXCLUDED for s in stages if is_uncounted(s)),
        # v2-align：每個原始階段只有一個且一致的分類（中止排除⇔有效標記中止；已配對⇔有循環 ID；其餘為已知的有效未配對類型）
        "I11": all(((s["pairing"] == STOPPED_EXCLUDED) == is_stopped(s))
                   and ((s["pairing"] in (PAIR_FIRST, PAIR_SECOND)) == (s["cycle_id"] is not None))
                   and s["pairing"] in (PAIR_FIRST, PAIR_SECOND, STOPPED_EXCLUDED) + unp_kinds for s in stages),
        # v2-align：每個循環＝原始序列中相鄰（不跨越中止階段）的兩個反向階段；全部循環同一個前半方向；循環 ID 不重複
        "I12": all(c["b"]["idx"] == c["a"]["idx"] + 1 and c["a"]["dir"] != c["b"]["dir"] for c in cycles)
               and len({c["a"]["dir"] for c in cycles}) <= 1 and len({c["id"] for c in cycles}) == len(cycles),
    }
    counts = {"sessions": len(sessions), "stages_D": sum(1 for s in stages if s["dir"] == "D"),
              "stages_C": sum(1 for s in stages if s["dir"] == "C"), "cycles": len(cycles),
              "certain": ce_cnt.get(CERTAIN, 0), "pair_pending": ce_cnt.get(PAIR_PENDING, 0),
              "status": {k: st_cnt.get(k, 0) for k in STATUSES}, "unpaired": len(unpaired),
              "valid_D": sum(1 for s in valid if s["dir"] == "D"), "valid_C": sum(1 for s in valid if s["dir"] == "C"),
              "stopped": len(stopped), "uncounted": sum(1 for s in stages if is_uncounted(s))}
    return r, counts


# =============================================================================
# 可比性（P-22 §7.4）與版本特徵（§8）
# =============================================================================
def read_mode(sa):
    st = sa["state"] or {}
    if st.get("hmi_reader"):
        return st["hmi_reader"]
    return None


def version_features(sa):
    st = sa["state"] or {}
    dist = Counter(len((t.get("reader") or {}).get("ep_ms") or []) for t in sa["data"]["timing"]
                   if isinstance(t.get("reader"), dict))
    has_ep = any(k > 0 for k in dist)
    has_feat = bool(st.get("hmi_reader")) or bool(st.get("hmi_reader_changes")) or has_ep
    return {"hmi_reader": st.get("hmi_reader"), "hmi_reader_changes": st.get("hmi_reader_changes") or [],
            "ep_ms_count_distribution": {str(k): v for k, v in sorted(dist.items())},
            "summary": "具 r2c O-1 特徵" if has_feat else "未見 r2c O-1 特徵", "has_feature": has_feat}


def version_info(sa, declared):
    f = version_features(sa)
    if not declared:
        cons = "無法比對（未提供宣告版本）"
    elif declared == "r2c":
        cons = "一致" if f["has_feature"] else "不一致（警示）"
    elif declared == "r2b":
        cons = "不一致（警示）" if f["has_feature"] else "一致"
    else:
        cons = "無法比對（宣告版本無對應特徵定義）"
    return {"declared_version": declared or "未提供", "version_source": "使用者提供", "version_verification": "未驗證",
            "features": f, "consistency": cons}


def _cond(sa):
    ti = (sa["state"] or {}).get("test_info") or {}
    return (str(ti.get("cabinet_spec") or "").strip(), str(ti.get("conditions") or "").strip())


def comparability(sessions, order):
    """每個已配對循環：時間量與極值是否可與其他所有循環比較（任一配對條件不成立 → 不可比並列原因）。"""
    cycles = [(sa, c) for sa in sessions for c in sa["cycles"]]
    for sa, c in cycles:
        t_why, e_why = [], []
        a, b = c["a"], c["b"]
        if a["span_sec"] is None or b["span_sec"] is None or c["rest_sec"] is None:
            t_why.append("本循環有 N/A 時間量")
        for sb, d in cycles:
            if d is c:
                continue
            if sb["params"] != sa["params"]:
                t_why.append(f"與 {d['id']} 的 params 不同")
                e_why.append(f"與 {d['id']} 的 params 不同")
            spec_a, cond_a = _cond(sa)
            spec_b, cond_b = _cond(sb)
            if not spec_a or not spec_b or spec_a != spec_b:
                t_why.append(f"與 {d['id']} 的電櫃規格不同或未設定")
                e_why.append(f"與 {d['id']} 的電櫃規格不同或未設定")
            if cond_a != cond_b:
                t_why.append(f"與 {d['id']} 的測試條件不同")
            if d["a"]["span_sec"] is None or d["b"]["span_sec"] is None or d["rest_sec"] is None:
                t_why.append(f"{d['id']} 有 N/A 時間量")
        if not _cond(sa)[0]:
            e_why.append("電櫃規格未設定")
        c["time_comparable"] = "可比" if not t_why else "不可比（" + "；".join(dict.fromkeys(t_why)) + "）"
        c["ext_comparable"] = "可比" if not e_why else "不可比（" + "；".join(dict.fromkeys(e_why)) + "）"


def endpoint_comparability(sessions):
    modes = [read_mode(sa) for sa in sessions]
    if any(m is None for m in modes):
        return "不可比（讀取模式未記錄：" + "、".join(f"S{sa['s_no']}" for sa, m in zip(sessions, modes) if m is None) + "）"
    if len(set(modes)) > 1:
        return "不可比（讀取模式不同：" + "、".join(f"S{sa['s_no']}＝{m}" for sa, m in zip(sessions, modes)) + "）"
    return "可比"


def title_spec(sessions):
    specs = [str(((sa["state"] or {}).get("test_info") or {}).get("cabinet_spec") or "").strip() for sa in sessions]
    if all(not s for s in specs):
        return "電櫃規格未設定"
    if any(not s for s in specs) or len(set(specs)) > 1:
        return "電櫃規格不一致"
    return specs[0]
