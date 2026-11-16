# -*- coding: utf-8 -*-
"""
report_display.py — 1 Hz Phase B：顯示層補值（Carry Forward）與資料品質統計（Quality Metrics）
================================================================================================
規格：D1（只補數值型量測）、D2（LIVE／CARRIED／NO_DATA，可追溯）、D3／D6（原始層只存實際讀值；補值每次重新推導）、
      D7（HMI 與 6161 分開統計；6161 不補值）、D9（完整即時格點）、D13（暫停／重啟後可沿用，age 含暫停）、
      方案 A（CARRIED 無時間上限）。

輸入：Phase A 的重建結果（report_quality.reconstruct）＋原始層（samples.csv／observations.jsonl）。
輸出：純資料（dict／list）。**不寫任何原始檔**、不持久化補值快取；同樣輸入 → 同樣輸出。
      Excel 外觀（灰字、分頁）屬 Phase D，不在本模組。
      M-S2 r2：build_raw_seconds（認證 Raw Data 每秒一列、整筆沿用）也在本模組，同樣只供顯示。

⚠ 安全邊界：本模組的結果**只供報表顯示與品質統計**。安全判定、方向、COMM_FAIL、能量積分、
   認證整分選點都不得使用（有 AST 測試把關）。
"""
import json
import math
import os
from datetime import datetime, timedelta

import report_quality as RQ

LIVE = "LIVE"
CARRIED = "CARRIED"
NO_DATA = "NO_DATA"
QUALITY_SCHEMA = 1
QUALITY_FILE = "quality_metrics.json"

# ---- D1：允許補值的數值欄位（顯示名 → 原始層欄位）----
#   單次報告：samples.csv 欄位名（SAMPLE_FIELDS）
CARRY_FIELDS_SINGLE = [
    ("SOC", "soc_percent"),
    ("Battery Voltage", "battery_voltage_v"),
    ("Battery Current", "battery_current_a"),
    ("Active Power", "actual_active_power_kw"),
    ("Reactive Power", "actual_reactive_power_kvar"),
    ("Rack Max Temperature", "rack_max_temperature_c"),
    ("Rack Min Temperature", "rack_min_temperature_c"),
]
CARRY_UNAVAILABLE_SINGLE = {
    "Cell Max Voltage": "samples.csv 沒有 Cell 最大電壓欄位（read_all 有 rack_cell_max_voltage_v，但單次報告原始層未保存）",
    "Cell Min Voltage": "samples.csv 沒有 Cell 最小電壓欄位（read_all 有 rack_cell_min_voltage_v，但單次報告原始層未保存）",
}
#   認證報告：observations.jsonl 的 values 鍵（cert_runtime.FIELD_SRC）
CARRY_FIELDS_CERT = [
    ("SOC", "soc"),
    ("Battery Voltage", "voltage_v"),
    ("Battery Current", "current_a"),
    ("Active Power", "power_kw"),
    ("Rack Max Temperature", "rack_max_c"),
    ("Rack Min Temperature", "rack_min_c"),
    ("Cell Max Voltage", "cell_vmax_v"),
    ("Cell Min Voltage", "cell_vmin_v"),
]
CARRY_UNAVAILABLE_CERT = {
    "Reactive Power": "observations.jsonl 沒有無功功率欄位（認證原始層未保存）",
}

# ---- D9：必要欄位 ----
BATTERY_POWER_VALID = ("已上電", "已下電", "切換中")       # 「未知」不算
REQUIRED_CERT_NUMERIC = ["soc", "voltage_v", "current_a", "power_kw",
                         "rack_max_c", "rack_min_c", "cell_vmax_v", "cell_vmin_v"]
REQUIRED_CERT_FLAGS = ["pcs_charging_flag", "pcs_discharging_flag", "pcs_fault_flag"]
REQUIRED_CERT_BPS = "battery_power_status"
REQUIRED_SINGLE_NUMERIC = ["soc_percent", "battery_voltage_v", "battery_current_a", "actual_active_power_kw"]
REQUIRED_SINGLE_FLAGS = ["pcs_charging_flag", "pcs_discharging_flag", "pcs_fault_flag",
                         "pcs_running_flag", "pcs_standby_flag"]
REQUIRED_SINGLE_UNAVAILABLE = {
    f: "samples.csv（schema < 1.7.0，30／32 欄）／slots.jsonl 都沒有保存 %s（read_all 有此旗標，但舊版原始層未記錄）" % f
    for f in REQUIRED_SINGLE_FLAGS}
ALARM_ENDPOINT_KEY = "alarm/list"                          # charge_discharge_report.EP_ALARM 的結尾
# 1 Hz D9 方案 A（schema 1.7.0）：samples.csv 第 33～38 欄（與 charge_discharge_report_config.SAMPLE_D9_FIELDS 相同；測試比對）
SINGLE_D9_FIELDS = REQUIRED_SINGLE_FLAGS + ["alarm_list_ok"]
AN_ALARM_EVIDENCE_CONFLICT = "ALARM_EVIDENCE_CONFLICT"     # samples.csv alarm_list_ok 與 SLOT failed_endpoints 矛盾
SINGLE_D9_NA_REASON = ("samples.csv 為舊版標題（schema < 1.7.0，30／32 欄），原始層沒有 5 個 PCS 旗標與 alarm_list_ok "
                       "→ 完整即時格點無法判定（不補寫、不推測、不以 charge_discharge_direction 反推）；"
                       "其餘必要項的 LIVE 計數見 required_fields")


def num(v):
    """有效數值（含 0）→ float；None／空字串／'-'／非數字／NaN／bool → None（與 cert_stage_engine._num 同語意）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip()
        if s in ("", "-", "None", "nan", "NaN"):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def flag(v):
    """布林旗標：True／False 為有效（False 不是缺資料）；None／其他 → None。CSV 字串 'True'／'False' 也接受。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip() in ("True", "False"):
        return v.strip() == "True"
    return None


def parse_time(s):
    """原始時間 → 具時區 datetime；沒有時區的視為本機時間。無法解析 → None。"""
    if not s:
        return None
    if isinstance(s, datetime):
        dt = s
    else:
        dt = None
        try:
            dt = datetime.fromisoformat(str(s))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(str(s), fmt)
                    break
                except ValueError:
                    pass
        if dt is None:
            return None
    return dt if dt.tzinfo is not None else dt.astimezone()


def _iso(dt):
    return dt.isoformat(timespec="milliseconds") if dt is not None else None


# =============================================================================
# 原始紀錄存取（單次／認證）
# =============================================================================
def _raw_value(kind, rec, field):
    if rec is None:
        return None
    if kind == "cert":
        return num((rec.get("values") or {}).get(field))
    return num(rec.get(field))


def _raw_time(kind, rec):
    if rec is None:
        return None
    return parse_time(rec.get("t") if kind == "cert" else rec.get("timestamp"))


def resolve_raw(entry, raw_by_ident, raw_by_ref_unique):
    """
    重建格點 → 原始紀錄（沒有 → None）。不靜默採用：
      raw_ref_ambiguous（歷史 raw_ref 重複且無法以識別辨識）→ None
      有格點識別且與 raw_ref 一致 → 該筆；否則 raw_ref 在原始層唯一 → 該筆；其餘 → None
    """
    if entry.get("raw_ref_ambiguous"):
        return None
    ref = entry.get("raw_ref")
    if ref is None:
        return None
    it = raw_by_ident.get((entry.get("seg_id"), entry.get("slot_index")))
    if it is not None and it.get("raw_ref") == ref:
        return it
    return raw_by_ref_unique.get(ref)


def _index_raw(raw_items):
    by_ident, by_ref, dup = {}, {}, set()
    for it in raw_items:
        sid, k = it.get("seg_id"), it.get("slot_index")
        if sid and k is not None and (sid, k) not in by_ident:
            by_ident[(sid, k)] = it
        ref = it.get("raw_ref")
        if ref is None:
            continue
        if ref in by_ref:
            dup.add(ref)
        else:
            by_ref[ref] = it
    for ref in dup:
        by_ref.pop(ref, None)
    return by_ident, by_ref


# =============================================================================
# 顯示層補值（Carry Forward）
# =============================================================================
def carry_fields(kind):
    return CARRY_FIELDS_CERT if kind == "cert" else CARRY_FIELDS_SINGLE


def carry_unavailable(kind):
    return CARRY_UNAVAILABLE_CERT if kind == "cert" else CARRY_UNAVAILABLE_SINGLE


def build_display(result, raw_items, kind):
    """
    重建結果＋原始層 → 顯示列（每個排程格點一列；舊 Session 沒有格點紀錄 → 每筆實際取樣一列）。

    每列：seg_id、slot_index、row_time（格點預定時間；舊 Session＝取樣時間）、status、raw_ref、raw、
          fields{原始欄位: {value, quality, source_time, source_raw_ref, age_sec}}
    規則（D2）：本列原始紀錄有有效數值 → LIVE；否則有先前的有效原始值 → CARRIED（value＝沿用值，
          source_time＝原始量測時間，age_sec＝本列時間 − 原始量測時間，含暫停／停機）；都沒有 → NO_DATA。
    跨段落沿用（D13）；OFFGRID_SAMPLE 是真實讀值 → 可作為之後列的沿用來源，但**不產生列**、不算格點。
    F-1（M-S2 G-2）：沿用來源只能是「本格格點時間之前已完成並保存」的讀值。
          一筆讀值先放在 pending，等到下一個**有執行讀取**的格點（ATTEMPTED_*）或新段落開始才成為沿用來源：
          排程器在上一格讀取＋保存完成後才開始下一個格點，期間被跳過的 MISSED／INFERRED_MISSED 格
          的格點時間都早於保存完成 → 這些格只能沿用更早的資料（沒有 → NO_DATA）。
          依格點順序判定，不比較牆鐘時間（不受時鐘漂移影響）；LIVE／MISSED 歸屬不變。
    """
    fields = [f for _n, f in carry_fields(kind)]
    by_ident, by_ref = _index_raw(raw_items)
    last = {}
    pending = []                                        # F-1：已讀到但在目前格點尚不能沿用的讀值（依序）
    rows = []

    def _feed(rec, t, ref):
        vals = {}
        for f in fields:
            v = _raw_value(kind, rec, f)
            if v is not None:
                vals[f] = (v, t, ref)
        if vals:
            pending.append(vals)

    def _promote():
        for vals in pending:
            last.update(vals)
        del pending[:]

    def _row(meta, rec, t_row, promote=True):
        if promote:
            _promote()
        out = {}
        for f in fields:
            v = _raw_value(kind, rec, f)
            if v is not None:
                out[f] = {"value": v, "quality": LIVE, "source_time": _iso(_raw_time(kind, rec)),
                          "source_raw_ref": meta.get("raw_ref"), "age_sec": 0.0}
            elif f in last:
                lv, lt, lref = last[f]
                age = None
                if t_row is not None and lt is not None:
                    age = round((t_row - lt).total_seconds(), 3)
                    if age < 0:
                        age = None                      # 時鐘不一致：不給負的資料年齡
                out[f] = {"value": lv, "quality": CARRIED, "source_time": _iso(lt), "source_raw_ref": lref,
                          "age_sec": age}
            else:
                out[f] = {"value": None, "quality": NO_DATA, "source_time": None, "source_raw_ref": None,
                          "age_sec": None}
        row = dict(meta)
        row["row_time"] = _iso(t_row)
        row["fields"] = out
        row["raw"] = rec
        rows.append(row)
        if rec is not None:
            _feed(rec, _raw_time(kind, rec), meta.get("raw_ref"))

    if not result.get("has_slot_log"):
        for it in raw_items:                            # 舊 Session：照實際取樣列，不推算格點
            rec = it.get("record")
            _row({"seg_id": None, "slot_index": None, "status": "RAW_ROW", "raw_ref": it.get("raw_ref"),
                  "complete_eligible": False, "entry": None}, rec, _raw_time(kind, rec))
        return rows

    og_after = {}
    for og in result.get("offgrid", []):
        og_after.setdefault(og.get("after_seg_id"), []).append(og)
    seg_seen = []
    for e in result.get("slots", []):
        new_seg = not seg_seen or seg_seen[-1] != e["seg_id"]
        if seg_seen and seg_seen[-1] != e["seg_id"]:
            _flush_offgrid(og_after.pop(seg_seen[-1], []), by_ref, kind, _feed)
        if not seg_seen or seg_seen[-1] != e["seg_id"]:
            seg_seen.append(e["seg_id"])
        attempted = e["status"] in RQ.ATTEMPTED_STATUSES
        it = resolve_raw(e, by_ident, by_ref) if attempted else None
        rec = it.get("record") if it else None
        _row({"seg_id": e["seg_id"], "slot_index": e["slot_index"], "status": e["status"],
              "raw_ref": e.get("raw_ref") if it else None, "complete_eligible": bool(e.get("complete_eligible")),
              "entry": e}, rec, parse_time(e.get("scheduled_time")), promote=attempted or new_seg)
    if seg_seen:
        _flush_offgrid(og_after.pop(seg_seen[-1], []), by_ref, kind, _feed)
    return rows


def _flush_offgrid(ogs, by_ref, kind, feed):
    for og in ogs:
        if og.get("anomalies"):
            continue
        it = by_ref.get(og.get("raw_ref"))
        if it is not None:
            feed(it.get("record"), _raw_time(kind, it.get("record")), og.get("raw_ref"))


# =============================================================================
# M-S2 r2：認證 Raw Data 每秒一列（整筆沿用；只供 Raw Data 顯示）
# =============================================================================
RAW_LIVE = "LIVE"                       # 本秒格點有執行讀取，且該筆通過報告有效判定（is_valid）
RAW_CARRIED = "CARRIED"                 # 本秒有執行讀取，但讀取失敗／無效／原始紀錄無法對應 → 整筆沿用
RAW_MISSED = "MISSED"                   # 本秒格點沒有執行讀取（MISSED／INFERRED_MISSED）→ 整筆沿用
RAW_SERVICE_STOPPED = "SERVICE_STOPPED"  # 兩段落之間、有明確服務中斷證據的秒數 → 整筆沿用
RAW_NO_SLOT = "NO_SLOT"                 # 兩段落之間、沒有格點紀錄且沒有中斷證據的秒數 → 整筆沿用
RAW_NA = "N/A"                          # 尚未取得任何有效觀測（數值空白）
RAW_STATES = (RAW_LIVE, RAW_CARRIED, RAW_MISSED, RAW_SERVICE_STOPPED, RAW_NO_SLOT, RAW_NA)
RAW_SLOT_TEXT = {
    RQ.ATTEMPTED_OK: "讀取成功",
    RQ.ATTEMPTED_PARTIAL: "讀取成功（部分端點失敗）",
    RQ.ATTEMPTED_FAIL: "讀取失敗",
    RQ.ATTEMPTED_SLOT_LOST: "格點紀錄遺失（依原始紀錄重建）",
    RQ.ATTEMPTED_CONFLICT: "格點狀態矛盾",
    RQ.MISSED: "未執行讀取（跳格）",
    RQ.INFERRED_MISSED: "紀錄遺失（推定缺格）",
}


def _sec(dt):
    """具時區／無時區 → 本機時間（無時區）的整秒（捨去小數）。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _plain(s):
    """時間字串 → 無時區整秒（保留字串上的本機時間；與格點時間同一基準）。"""
    if not s:
        return None
    if isinstance(s, datetime):
        return _sec(s)
    try:
        return _sec(datetime.fromisoformat(str(s)))
    except ValueError:
        dt = parse_time(s)
        return None if dt is None else _sec(dt)


def build_raw_seconds(result, raw_items, is_valid, started_at=None, interruptions=None):
    """
    認證 Raw Data 每秒一列（M-S2 r2）。不適用 → None（沒有格點紀錄、沒有任何格點、或有段落的格點間隔不是 1 秒），
    呼叫端改用原本的 Raw Data（只列實際有效觀測）。

    is_valid(raw_ref) → 該筆原始紀錄是否通過報告有效判定（與階段頁同一判定；由呼叫端提供）。
    每列：time（本機整秒）、state（RAW_STATES）、src（來源 raw_ref＝observations.jsonl 實體行號）、
          src_time（來源列的秒）、age（本列秒 − 來源列秒；無法正確計算 → None）、slot_status、cause、gap。
    規則：
      - 整筆沿用：來源一定是一筆通過 is_valid 的觀測，不同欄位不會混用不同時間。
      - F-1：讀值先放 pending，到下一個有執行讀取的格點或新段落開始才成為沿用來源（依格點順序，不比較牆鐘）。
      - 起點：started_at 那一秒到第一個格點之前 → N/A（認證已開始、尚無格點）；終點＝最後一個已知格點（不往後推算）。
      - 段落之間的空秒：下一段原因為 restart，且 interruptions 有一筆 from≤to、to 落在空檔內 →
        from＜秒≤to 為 SERVICE_STOPPED，其餘空秒 NO_SLOT；任何證據不足 → 全部 NO_SLOT（不推定服務停止）。
      - 時鐘回撥：格點秒 ≤ 已輸出的最後一秒 → 不產生列；該格觀測不計入 LIVE、也不作為沿用來源（保守）。
      - 資料年齡跨段落累加，不歸零；只有新的 LIVE 才為 0；算出負值 → None（時鐘異常）。
        發生時鐘回撥（新段落第一格 ≤ 已輸出的最後一秒）後，來源在回撥之前的沿用列 → None（兩邊時鐘基準不同，
        以格點時間相減會低估）。同一段落內格點時間由單調時鐘推算，不受牆鐘調整影響。
        限制：服務停止期間若時鐘被往前調、或往回調但未造成重疊，無法從紀錄偵測，年齡以牆鐘計算（見報告說明）。
    """
    if not result or not result.get("has_slot_log"):
        return None
    slots = result.get("slots") or []
    if not slots:
        return None
    segs = {s.get("seg_id"): s for s in result.get("segments") or []}
    for sid in {e.get("seg_id") for e in slots}:
        if num((segs.get(sid) or {}).get("interval_sec")) != 1:
            return None
    by_ident, by_ref = _index_raw(raw_items)
    intr = [(_plain((it or {}).get("from")), _plain((it or {}).get("to"))) for it in interruptions or []]
    og_after = {}
    for og in result.get("offgrid", []):
        og_after.setdefault(og.get("after_seg_id"), []).append(og)

    rows = []
    st = {"pending": [], "last": None, "S": None, "epoch": 0}      # epoch：時鐘回撥次數（來源與本列不同 → 年齡不可信）
    stats = {"counts": {s: 0 for s in RAW_STATES}, "overlap_slots": 0, "overlap_from": None, "overlap_to": None,
             "max_age": None, "age_unknown": 0, "gaps": [], "pre_rows": 0, "invalid_attempts": 0}
    one = timedelta(seconds=1)

    def promote():
        for item in st["pending"]:
            st["last"] = item
        del st["pending"][:]

    def emit(sec, state, slot_status=None, cause=None, gap=None):
        last = st["last"]
        if state == RAW_LIVE:
            row = {"src": None, "src_time": sec, "age": 0, "clock_ok": True}
        elif last is None:
            row = {"src": None, "src_time": None, "age": None, "clock_ok": True}
            cause, state = cause or state, RAW_NA
        else:
            clock_ok = last[2] == st["epoch"]
            age = int((sec - last[1]).total_seconds()) if (last[1] is not None and clock_ok) else None
            if age is not None and age < 0:
                age, clock_ok = None, False             # 時鐘異常：不顯示錯誤的年齡
            if age is None:
                stats["age_unknown"] += 1
            elif stats["max_age"] is None or age > stats["max_age"]:
                stats["max_age"] = age
            row = {"src": last[0], "src_time": last[1], "age": age, "clock_ok": clock_ok}
        row.update(time=sec, state=state, slot_status=slot_status, cause=cause, gap=gap)
        rows.append(row)
        stats["counts"][state] += 1
        st["S"] = sec
        return row

    def flush_offgrid(sid):
        for og in og_after.pop(sid, []):
            ref = og.get("raw_ref")
            if by_ref.get(ref) is not None and not og.get("anomalies") and st["S"] is not None and is_valid(ref):
                st["pending"].append((ref, st["S"], st["epoch"]))   # 段落後的補讀：只作為之後的沿用來源（不產生列）

    first = _plain(slots[0].get("scheduled_time"))
    s0 = _plain(started_at)
    if s0 is not None and first is not None and s0 < first:
        k = s0
        while k < first:
            emit(k, RAW_NA, cause="認證已開始，第一個格點前")
            stats["pre_rows"] += 1
            k += one

    prev = None
    for e in slots:
        sid = e.get("seg_id")
        new_seg = sid != prev
        if new_seg and prev is not None:
            flush_offgrid(prev)
        status = e.get("status")
        attempted = status in RQ.ATTEMPTED_STATUSES
        if new_seg or attempted:
            promote()
        prev = sid
        sec = _plain(e.get("scheduled_time"))
        S = st["S"]
        if sec is None:
            continue
        if S is not None and sec <= S:                  # 時鐘回撥造成的重疊：不產生列、不採用該格觀測
            if new_seg:
                st["epoch"] += 1
            stats["overlap_slots"] += 1
            stats["overlap_from"] = stats["overlap_from"] or sec
            stats["overlap_to"] = sec
            continue
        if S is not None and sec > S + one:
            g0, g1 = S + one, sec - one
            ev = None
            if new_seg and (segs.get(sid) or {}).get("reason") == "restart":
                for f, t in intr:
                    if f is not None and t is not None and f <= t and S < t <= sec:
                        ev = (f, t)
                        break
            stats["gaps"].append({"from": g0, "to": g1, "seconds": int((g1 - g0).total_seconds()) + 1,
                                  "evidence": ev})
            k = g0
            while k <= g1:
                stopped = ev is not None and ev[0] < k <= ev[1]
                emit(k, RAW_SERVICE_STOPPED if stopped else RAW_NO_SLOT,
                     cause=("服務停止（%s～%s，無取樣）" % (ev[0], ev[1])) if stopped else "無格點紀錄（原因未確認）",
                     gap=len(stats["gaps"]))
                k += one
        if attempted:
            it = resolve_raw(e, by_ident, by_ref)
            ref = it.get("raw_ref") if it else None
            if ref is not None and is_valid(ref):
                row = emit(sec, RAW_LIVE, slot_status=status)
                row["src"] = ref
                st["pending"].append((ref, sec, st["epoch"]))
            else:
                stats["invalid_attempts"] += 1
                emit(sec, RAW_CARRIED, slot_status=status)
        else:
            emit(sec, RAW_MISSED, slot_status=status)
    return {"rows": rows, "stats": stats}


def excel_rack_display_rows(samples):
    """
    D3 過渡：單次報告 Excel 的 Rack 最高／最低溫在原始層缺值時，顯示上一筆有效值（與舊版 Excel 呈現相同）。
    只產生**顯示用複本**，self.samples／samples.csv 不變。其他欄位不補（CARRIED 標示屬 Phase D，未標示前不新增補值）。
    """
    out = []
    last = {}
    for s in samples or []:
        row = dict(s)
        for f in ("rack_max_temperature_c", "rack_min_temperature_c"):
            v = num(row.get(f))
            if v is None:
                if f in last:
                    row[f] = last[f]
            else:
                last[f] = row.get(f)
        out.append(row)
    return out


# =============================================================================
# D9：完整即時格點
# =============================================================================
def single_d9_available(raw_items=None, header=None):
    """
    單次報告原始層是否有 D9 欄位（schema 1.7.0 的 38 欄標題）。依實際 CSV 標題判定，不看程式版本：
    舊 30／32 欄 Session 由新版程式續接時，標題沿用舊版 → 仍是 False。
    header 已知 → 以 header 為準；否則看原始紀錄的鍵（DictReader 的鍵＝標題）。
    """
    if header is not None:
        return all(f in header for f in SINGLE_D9_FIELDS)
    for it in raw_items or []:
        rec = it.get("record") or {}
        return all(f in rec for f in SINGLE_D9_FIELDS)
    return False


def alarm_evidence(row):
    """
    單次報告：本格 alarm/list 的兩份證據 → (csv, slot_failed, conflict)。
      csv         ：samples.csv 的 alarm_list_ok（True／False／None；沒有原始紀錄 → None）
      slot_failed ：SLOT.failed_endpoints 是否含 alarm/list（沒有已記錄的 ATTEMPTED SLOT → None＝無 SLOT 證據）
      conflict    ：兩份證據互相矛盾（只在兩份都存在時判定）：
                    SLOT 記 alarm/list 失敗，但 CSV 不是 False（True 或空白）；或 SLOT 沒記失敗，但 CSV 是 False。
                    （CSV 空白＋SLOT 沒記失敗＝有回應但結構不完整，兩者一致，不算矛盾。）
    只讀、不修正任何一方。
    """
    rec = row.get("raw")
    csv_v = flag((rec or {}).get("alarm_list_ok")) if rec is not None else None
    e = row.get("entry") or {}
    slot_failed = None
    if e.get("source") == "logged" and row.get("status") in RQ.ATTEMPTED_STATUSES:
        slot_failed = any(ALARM_ENDPOINT_KEY in str(x) for x in (e.get("failed_endpoints") or []))
    conflict = False
    if rec is not None and slot_failed is not None:
        conflict = (slot_failed and csv_v is not False) or (not slot_failed and csv_v is False)
    return csv_v, slot_failed, conflict


def required_status(kind, row, d9=None):
    """
    回傳 (complete, detail)。complete：True／False；單次報告的原始層沒有 D9 欄位（舊 30／32 欄）→ None（無法判定）。
    detail：{必要項: "LIVE"|"MISSING"|"UNAVAILABLE"|"CONFLICT"}。CARRIED 一律不算 LIVE；布林 False 是有效值。
    d9（只用於單次）：本 Session 的 samples.csv 是否有 D9 欄位；None → 依本列原始紀錄的鍵判定。
    """
    rec = row.get("raw")
    fl = row["fields"]
    det = {}
    if kind == "cert":
        for f in REQUIRED_CERT_NUMERIC:
            det[f] = "LIVE" if fl.get(f, {}).get("quality") == LIVE else "MISSING"
        flags = (rec or {}).get("flags") or {}
        for f in REQUIRED_CERT_FLAGS:
            det[f] = "LIVE" if flag(flags.get(f)) is not None else "MISSING"
        det[REQUIRED_CERT_BPS] = "LIVE" if flags.get(REQUIRED_CERT_BPS) in BATTERY_POWER_VALID else "MISSING"
        ok = (row.get("status") in (RQ.ATTEMPTED_OK, RQ.ATTEMPTED_PARTIAL) and row.get("complete_eligible")
              and all(v == "LIVE" for v in det.values()))
        return bool(ok), det
    for f in REQUIRED_SINGLE_NUMERIC:
        det[f] = "LIVE" if fl.get(f, {}).get("quality") == LIVE else "MISSING"
    if d9 is None:
        d9 = rec is not None and all(f in rec for f in SINGLE_D9_FIELDS)
    if d9:
        # ---- D9 方案 A（38 欄）：11 項全部由本格原始證據判定 ----
        for f in REQUIRED_SINGLE_FLAGS:
            det[f] = "LIVE" if flag((rec or {}).get(f)) is not None else "MISSING"
        det["battery_power_status"] = ("LIVE" if (rec or {}).get("battery_power_status") in BATTERY_POWER_VALID
                                       else "MISSING")
        csv_v, _sf, conflict = alarm_evidence(row)
        det["alarm_list_ok"] = "CONFLICT" if conflict else ("LIVE" if csv_v is True else "MISSING")
        ok = (row.get("status") in (RQ.ATTEMPTED_OK, RQ.ATTEMPTED_PARTIAL) and row.get("complete_eligible")
              and rec is not None and all(v == "LIVE" for v in det.values()))
        return bool(ok), det
    # ---- 舊 30／32 欄：PCS 旗標原始層沒有 → UNAVAILABLE、完整性 None（行為與 schema 1.6.0 相同）----
    for f in REQUIRED_SINGLE_FLAGS:
        det[f] = "UNAVAILABLE"
    det["battery_power_status"] = ("LIVE" if (rec or {}).get("battery_power_status") in BATTERY_POWER_VALID
                                   else "MISSING")
    e = row.get("entry") or {}
    if e.get("source") == "logged" and row.get("status") in RQ.ATTEMPTED_STATUSES:
        fails = [str(x) for x in (e.get("failed_endpoints") or [])]
        det["alarm_list_ok"] = "MISSING" if any(ALARM_ENDPOINT_KEY in x for x in fails) else "LIVE"
    else:
        det["alarm_list_ok"] = "MISSING"           # 沒有 SLOT（例：格點紀錄遺失）→ 無法證明本次 alarm/list 成功
    return None, det


def heat_status(row):
    """認證 6161（D7）：本格 6161 狀態與有效值；不補值。"""
    e = row.get("entry") or {}
    rec = row.get("raw") or {}
    vals = rec.get("values") or {}
    valid = num(vals.get("sensor_c")) is not None or num(vals.get("pcs_module_c")) is not None
    st = e.get("heat_status") if e.get("source") == "logged" else None
    return st, valid


# =============================================================================
# Quality Metrics
# =============================================================================
def _rate(a, b):
    return round(a / b, 6) if (isinstance(a, int) and isinstance(b, int) and b > 0) else None


def quality_metrics(result, raw_items, kind, display=None, schema_version=None, single_header=None):
    """
    重建結果＋原始層 → 品質統計 dict（純資料；可重現）。各指標的定義與分母見 METRIC_DEFINITIONS。
    single_header：單次報告 samples.csv 的實際標題（判定是否有 D9 欄位）；None → 由原始紀錄的鍵判定。
    """
    rows = display if display is not None else build_display(result, raw_items, kind)
    an = {}
    for a in result.get("anomalies", []):
        an[a["type"]] = an.get(a["type"], 0) + 1
    d9 = single_d9_available(raw_items, single_header) if kind == "single" else None
    m = {"quality_schema": QUALITY_SCHEMA, "kind": kind, "has_slot_log": bool(result.get("has_slot_log")),
         "definitions": METRIC_DEFINITIONS, "na_reasons": {}}
    c = dict(result.get("counts", {}))
    seg = result.get("segments", [])
    if not m["has_slot_log"]:
        for k in ("expected_slots", "missed", "inferred_missed", "attempted_slots", "complete_live_slots",
                  "complete_live_rate", "attempt_rate"):
            m[k] = None
            m["na_reasons"][k] = "舊版 Session 沒有 slots.jsonl（無格點紀錄）→ 不推算格點與漏秒"
        m["raw_rows"] = len(rows)
        base = len(rows)
        base_name = "raw_rows"
    else:
        m["expected_slots"] = len(result.get("slots", []))
        m["status_counts"] = {s: c.get(s, 0) for s in (RQ.ATTEMPTED_OK, RQ.ATTEMPTED_PARTIAL, RQ.ATTEMPTED_FAIL,
                                                       RQ.ATTEMPTED_SLOT_LOST, RQ.ATTEMPTED_CONFLICT,
                                                       RQ.MISSED, RQ.INFERRED_MISSED)}
        m["attempted_slots"] = sum(c.get(s, 0) for s in RQ.ATTEMPTED_STATUSES)
        m["missed"] = c.get(RQ.MISSED, 0)
        m["inferred_missed"] = c.get(RQ.INFERRED_MISSED, 0)
        m["attempt_rate"] = _rate(m["attempted_slots"], m["expected_slots"])
        m["api_ok_rate"] = _rate(c.get(RQ.ATTEMPTED_OK, 0), m["attempted_slots"])
        m["segments"] = len(seg)
        m["unterminated_segments"] = sum(1 for s in seg if not s.get("has_end"))
        base = m["expected_slots"]
        base_name = "expected_slots"

    # ---- 完整即時格點（D9）----
    comp, req_cnt, conflicts = 0, {}, []
    for r in rows:
        ok, det = required_status(kind, r, d9=d9)
        if ok:
            comp += 1
        for k, v in det.items():
            req_cnt.setdefault(k, {"LIVE": 0, "MISSING": 0, "UNAVAILABLE": 0})
            req_cnt[k][v] = req_cnt[k].get(v, 0) + 1
        if d9:
            csv_v, slot_failed, conflict = alarm_evidence(r)
            if conflict:
                fails = [str(x) for x in ((r.get("entry") or {}).get("failed_endpoints") or [])]
                conflicts.append({"seg_id": r.get("seg_id"), "slot_index": r.get("slot_index"),
                                  "raw_ref": r.get("raw_ref"), "status": r.get("status"),
                                  "csv_alarm_list_ok": (r.get("raw") or {}).get("alarm_list_ok"),
                                  "slot_alarm_failed": slot_failed,
                                  "slot_failed_endpoints": [x for x in fails if ALARM_ENDPOINT_KEY in x]})
    m["required_fields"] = req_cnt
    m["required_fields_denominator"] = base_name
    if kind == "single":
        m["single_d9_available"] = bool(d9)
        m["alarm_evidence_conflicts"] = len(conflicts) if d9 else None
        m["alarm_evidence_conflict_slots"] = conflicts[:50] if d9 else None
        if conflicts:
            an[AN_ALARM_EVIDENCE_CONFLICT] = len(conflicts)
        if not d9:
            m["na_reasons"]["alarm_evidence_conflicts"] = SINGLE_D9_NA_REASON
    if (kind == "cert" or d9) and m["has_slot_log"]:
        m["complete_live_slots"] = comp
        m["incomplete_attempted_slots"] = m["attempted_slots"] - comp
        m["incomplete_expected_slots"] = m["expected_slots"] - comp
        m["complete_live_rate"] = _rate(comp, m["expected_slots"])
    elif kind == "single" and not d9:
        for k in ("complete_live_slots", "incomplete_attempted_slots", "incomplete_expected_slots",
                  "complete_live_rate"):
            m[k] = None
            m["na_reasons"][k] = SINGLE_D9_NA_REASON
    else:
        for k in ("complete_live_slots", "incomplete_attempted_slots", "incomplete_expected_slots"):
            m[k] = None
            m["na_reasons"][k] = "舊版 Session 沒有 slots.jsonl → 無法判定格點完整性"

    # ---- 各數值欄位 LIVE／CARRIED／NO_DATA ----
    fq = {}
    for name, f in carry_fields(kind):
        cnt = {LIVE: 0, CARRIED: 0, NO_DATA: 0}
        max_age = None
        for r in rows:
            q = r["fields"][f]
            cnt[q["quality"]] += 1
            if q["quality"] == CARRIED and q["age_sec"] is not None:
                max_age = q["age_sec"] if max_age is None else max(max_age, q["age_sec"])
        fq[f] = {"name": name, "counts": cnt, "denominator": base_name, "live_rate": _rate(cnt[LIVE], base),
                 "max_carried_age_sec": max_age}
    m["field_quality"] = fq
    m["field_unavailable"] = dict(carry_unavailable(kind))

    # ---- HMI 品質（D7）----
    att_rows = [r for r in rows if r["status"] in RQ.ATTEMPTED_STATUSES or r["status"] == "RAW_ROW"]
    if kind == "cert":
        hmi_ok = sum(1 for r in att_rows if (r.get("raw") or {}).get("hmi_ok") is True
                     or ((r.get("raw") or {}).get("hmi_ok") is None
                         and any(r["fields"][f]["quality"] == LIVE for _n, f in CARRY_FIELDS_CERT)))
    else:
        hmi_ok = sum(1 for r in att_rows if any(r["fields"][f]["quality"] == LIVE for _n, f in CARRY_FIELDS_SINGLE))
    no_raw = sum(1 for r in att_rows if r.get("raw") is None)
    m["hmi"] = {"attempted": len(att_rows), "hmi_valid": hmi_ok, "hmi_invalid": len(att_rows) - hmi_ok - no_raw,
                "no_raw_record": no_raw, "hmi_valid_rate": _rate(hmi_ok, len(att_rows)),
                "denominator": "attempted（實際嘗試讀取的格點；舊 Session＝實際取樣列）"}

    # ---- 6161 品質（D7；只有認證報告）----
    if kind == "cert":
        st_cnt, valid, unknown = {}, 0, 0
        for r in att_rows:
            st, ok = heat_status(r)
            if st is None and (r.get("entry") or {}).get("source") != "logged":
                unknown += 1
            st_cnt[str(st)] = st_cnt.get(str(st), 0) + 1
            if ok:
                valid += 1
        m["heat_6161"] = {"attempted": len(att_rows), "valid_value": valid, "status_counts": st_cnt,
                          "status_unknown_no_slot": unknown, "valid_rate": _rate(valid, len(att_rows)),
                          "carried": 0, "denominator": "attempted（與 HMI 同一組格點，各自獨立判定）",
                          "note": "6161 不補值（D7）；status 來自 SLOT.heat_status（ok／failed／late／skipped_inflight／None＝未設定讀取）"}
    else:
        m["heat_6161"] = None
        m["na_reasons"]["heat_6161"] = "單次報告不讀 6161"

    # ---- 其他 ----
    conf = result.get("raw_ref_conflicts", {})
    m["raw_ref_conflicts"] = len(conf)
    m["raw_ref_conflict_slots"] = sum(len(v) for v in conf.values())
    m["incomplete_raw_records"] = an.get(RQ.AN_RAW_INCOMPLETE, 0) + an.get(RQ.AN_RAW_UNPARSEABLE, 0)
    m["offgrid_samples"] = len(result.get("offgrid", []))
    m["anomaly_counts"] = dict(sorted(an.items()))
    m["legacy_raw_before_slot_log"] = result.get("legacy_raw_before_slot_log", 0)
    if kind == "single":
        legacy = _schema_lt(schema_version, "1.6.0")
        m["legacy_rack_carry_untraceable"] = legacy
        if legacy:
            m["na_reasons"]["rack_temperature_quality"] = ("schema %s（< 1.6.0）：舊版程式在原始層沿用 Rack 溫度、未標記 → "
                                                           "Rack 溫度的 LIVE／CARRIED 無法追溯" % schema_version)
    return m


def _schema_lt(v, ref):
    if not v:
        return True
    try:
        a = tuple(int(x) for x in str(v).split("."))
        b = tuple(int(x) for x in ref.split("."))
        return a < b
    except ValueError:
        return True


METRIC_DEFINITIONS = {
    "expected_slots": "重建後的排程格點數＝ATTEMPTED_OK＋PARTIAL＋FAIL＋SLOT_LOST＋CONFLICT＋MISSED＋INFERRED_MISSED；"
                      "不含 OFFGRID_SAMPLE；不含沒有 SEGMENT_END 段落的未知尾端（見 unterminated_segments）",
    "attempted_slots": "實際嘗試讀取的格點＝ATTEMPTED_OK＋PARTIAL＋FAIL＋SLOT_LOST＋CONFLICT",
    "missed": "已落盤的 MISSED（確定沒有讀取）；與 inferred_missed 互斥、不重複計數",
    "inferred_missed": "有證據推算的缺格（Q3）；沒有證據的時間空白不計",
    "attempt_rate": "attempted_slots ÷ expected_slots",
    "api_ok_rate": "ATTEMPTED_OK ÷ attempted_slots（API 讀取全部成功的比例；**不等於**資料完整率）",
    "complete_live_slots": "D9：格點狀態為 OK／PARTIAL、無任何異常、且全部必要項本格 LIVE（CARRIED 不算；布林 False 有效）；"
                           "單次報告需 samples.csv 為 38 欄（schema 1.7.0），alarm_list_ok=True 且與 SLOT 無矛盾",
    "alarm_evidence_conflicts": "單次報告：samples.csv alarm_list_ok 與 SLOT.failed_endpoints（alarm/list）矛盾的格點數；"
                                "兩份原始證據保留不修正；該格不計入完整；同時列入 anomaly_counts.ALARM_EVIDENCE_CONFLICT",
    "complete_live_rate": "complete_live_slots ÷ expected_slots",
    "incomplete_attempted_slots": "attempted_slots − complete_live_slots",
    "incomplete_expected_slots": "expected_slots − complete_live_slots（含 MISSED／INFERRED_MISSED）",
    "field_quality": "每個 D1 數值欄位在每一列的 LIVE／CARRIED／NO_DATA 計數；分母＝expected_slots（舊 Session＝raw_rows）",
    "hmi": "HMI 有效（認證：hmi_ok；單次：任一 D1 數值欄位 LIVE）÷ attempted",
    "heat_6161": "6161 有效值（sensor_c 或 pcs_module_c 有數值）÷ attempted；依 SLOT.heat_status 分類；不補值",
    "raw_ref_conflicts": "同一 raw_ref 對應多筆原始紀錄的 raw_ref 數（raw_ref_conflict_slots＝受影響格點數；皆不計入完整）",
    "incomplete_raw_records": "RAW_INCOMPLETE（不完整 CSV 列）＋RAW_UNPARSEABLE（無法解析的 JSONL 行）",
    "offgrid_samples": "OFFGRID_SAMPLE（段落結束後的真實取樣，例：Menu 19 重試）；不計入任何格點指標",
}


# =============================================================================
# 資料夾層（報表產生時呼叫；只讀原始檔，只寫衍生的 quality_metrics.json）
# =============================================================================
def folder_quality(folder, kind, schema_version=None):
    recs, an = RQ.read_slot_log(os.path.join(folder, RQ.SLOT_LOG_FILE))
    if kind == "cert":
        raw, ran = RQ.load_cert_raw(os.path.join(folder, "observations.jsonl"))
    else:
        raw, ran = RQ.load_single_raw(os.path.join(folder, "samples.csv"),
                                      incomplete_lines=RQ.csv_tail_incomplete_lines(os.path.join(folder, "events.csv")))
    header = _csv_header(os.path.join(folder, "samples.csv")) if kind == "single" else None
    res = RQ.reconstruct(recs, raw, an, ran)
    disp = build_display(res, raw, kind)
    return res, disp, quality_metrics(res, raw, kind, display=disp, schema_version=schema_version,
                                      single_header=header)


def _csv_header(path):
    """
    samples.csv 第一列（標題）；不存在／空檔 → None（唯讀）。
    標題全為英數底線欄位名（SAMPLE_*_FIELDS），以逗號分割即可；本模組不 import csv（G3 匯入白名單）。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            line = f.readline()
    except (OSError, UnicodeDecodeError):
        return None
    line = line.rstrip("\r\n")
    return [c.strip() for c in line.split(",")] if line else None


def write_quality_metrics(folder, kind, schema_version=None):
    """產生 quality_metrics.json（衍生檔；每次由原始層重新推導、原子替換）。回傳 metrics；失敗往外拋由呼叫端 Fail Open。"""
    _res, _disp, m = folder_quality(folder, kind, schema_version=schema_version)
    path = os.path.join(folder, QUALITY_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)
    return m
