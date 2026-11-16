# -*- coding: utf-8 -*-
"""
report_quality.py — 1 Hz 取樣格點層（slots.jsonl）：寫入、讀取、重建（Phase A）
================================================================================
規格：D5／S2（每個錄製格點一筆紀錄）、Q3（INFERRED_MISSED 證據式重建）、Q5（原始紀錄格點識別）。

三層資料（本模組只碰「格點層」，並唯讀對照「原始層」）：
  原始層   認證 observations.jsonl／單次 samples.csv —— 只存實際讀到的值；每筆帶 slot_seg_id、slot_index
  格點層   slots.jsonl（本模組）—— SEGMENT_START／SLOT／SEGMENT_END，只追加、不改寫
  顯示層   Excel／品質統計（Phase B 起；本模組只提供重建後的格點序列）

slots.jsonl 紀錄：
  SEGMENT_START  seg_id、owner_kind（session／cert）、owner_id、interval_sec、anchor_wall（具時區）、
                 anchor_mono、reason（start／resume／restart）、pid
  SLOT           seg_id、slot_index、scheduled_time（＝anchor_wall＋slot_index×interval）、slot_status、
                 raw_ref（認證＝observations.jsonl 實體行號（1 起算）；單次＝sample_index；MISSED＝null）、
                 read_start、read_done、failed_endpoints、communication_ok、timeout、missed_reason
  SEGMENT_END    seg_id、last_slot_index（本段落已發出的最後格點編號；沒有任何格點＝null）、reason（pause／stop／finalize）
                 ⚠ SEGMENT_END 只代表「取樣段落結束」。reason=finalize ＝因收尾而停止取樣，**不代表報告產生成功**；
                   報告是否完成看 session_state.json（status／output_status）與 events.csv（finalize_ok／
                   finalize_partial／finalize_failed）。
  OFFGRID_SAMPLE （收尾決策 方案 C）段落已結束後才發生的真實原始取樣（例：自動收尾失敗後以 Menu 19 重試時的最後一筆）。
                 raw_ref、reason（finalize_retry）、after_seg_id（最後一個段落）、read_start／read_done、read_time_source、
                 communication_ok、failed_endpoints、wall。**沒有 slot_index**：不算入預期格點數，
                 也不算 ATTEMPTED／MISSED／INFERRED_MISSED；重建時歸類為「非格點紀錄」，不是異常。

slot_status（寫入端）：
  ATTEMPTED_OK       有執行讀取、原始紀錄已寫入、所有端點成功
  ATTEMPTED_PARTIAL  有執行讀取、原始紀錄已寫入、關鍵來源成功但部分端點失敗
  ATTEMPTED_FAIL     有執行讀取，但關鍵來源失敗或讀取例外（可能沒有原始紀錄）
  MISSED             **確定**沒有執行讀取（排程器回報跳格／停止交接期間）；不寫原始層

重建（產生報表時；只讀不寫）另外會出現：
  ATTEMPTED_SLOT_LOST  原始紀錄帶有此格點識別、但 SLOT 沒有落盤 →「格點紀錄遺失」
  ATTEMPTED_CONFLICT   SLOT 寫 MISSED，卻有帶同一格點識別的原始紀錄 →「格點狀態矛盾」（以有讀取顯示，不計入完整）
  INFERRED_MISSED      未落盤、但有可靠證據證明是缺格（同段落兩個已知格點之間的缺號；或 SEGMENT_END 涵蓋的尾端）

本模組不做任何網路 I/O；安全判定、方向、COMM_FAIL、能量積分都**不得**使用本模組的結果。
寫入失敗一律 Fail Open（只計數，不中斷取樣）。
"""
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta

SLOT_LOG_FILE = "slots.jsonl"
SLOT_SCHEMA = 1

REC_SEGMENT_START = "SEGMENT_START"
REC_SLOT = "SLOT"
REC_SEGMENT_END = "SEGMENT_END"
REC_OFFGRID = "OFFGRID_SAMPLE"
OFFGRID_FINALIZE_RETRY = "finalize_retry"

READ_TIME_API = "api_read"              # read_start／read_done＝實際 API 讀取區間
READ_TIME_UNAVAILABLE = "unavailable"   # 取不到實際讀取時間（例：sample_once 例外）→ 不偽裝為讀取時間

ATTEMPTED_OK = "ATTEMPTED_OK"
ATTEMPTED_PARTIAL = "ATTEMPTED_PARTIAL"
ATTEMPTED_FAIL = "ATTEMPTED_FAIL"
MISSED = "MISSED"
ATTEMPTED_SLOT_LOST = "ATTEMPTED_SLOT_LOST"
ATTEMPTED_CONFLICT = "ATTEMPTED_CONFLICT"
INFERRED_MISSED = "INFERRED_MISSED"

WRITER_STATUSES = (ATTEMPTED_OK, ATTEMPTED_PARTIAL, ATTEMPTED_FAIL, MISSED)
ATTEMPTED_STATUSES = (ATTEMPTED_OK, ATTEMPTED_PARTIAL, ATTEMPTED_FAIL, ATTEMPTED_SLOT_LOST, ATTEMPTED_CONFLICT)
NOT_ATTEMPTED_STATUSES = (MISSED, INFERRED_MISSED)

SEG_REASONS_START = ("start", "resume", "restart")
SEG_REASONS_END = ("pause", "stop", "finalize")

MISSED_READ_OVERRUN = "read_overrun"          # 前一格讀取超過週期 → 排程器跳過
MISSED_STOP_TRANSITION = "stop_transition"    # 取樣迴圈已停止、結束前最後一筆取樣之前的格點

# ---- 異常類型（重建時記錄；不補造資料） ----
AN_TAIL_INCOMPLETE = "TAIL_INCOMPLETE"                  # 最後一行只寫了一半（略過）
AN_LINE_UNPARSEABLE = "LINE_UNPARSEABLE"                # 中間行無法解析（略過）
AN_RECORD_INVALID = "RECORD_INVALID"                    # 可解析但欄位不合法（略過）
AN_DUPLICATE_SEGMENT_START = "DUPLICATE_SEGMENT_START"
AN_DUPLICATE_SEGMENT_END = "DUPLICATE_SEGMENT_END"
AN_DUPLICATE_SLOT = "DUPLICATE_SLOT"                    # 同一格多筆 SLOT（保留第一筆）
AN_SLOT_WITHOUT_START = "SLOT_WITHOUT_SEGMENT_START"
AN_SLOT_LOST = "SLOT_LOST"                              # 原始紀錄存在、SLOT 未落盤（已重建為 ATTEMPTED_SLOT_LOST）
AN_STATUS_CONFLICT = "STATUS_CONFLICT"                  # SLOT=MISSED 但有原始紀錄
AN_REF_MISSING = "REF_MISSING"                          # raw_ref 指向的原始紀錄不存在
AN_REF_MISMATCH = "REF_MISMATCH"                        # raw_ref 指向的原始紀錄格點識別不同
AN_REF_NULL_RAW_EXISTS = "REF_NULL_RAW_EXISTS"          # SLOT 沒有 raw_ref，卻有帶此格點識別的原始紀錄
AN_RAW_DUPLICATE_IDENT = "RAW_DUPLICATE_IDENT"          # 同一格多筆原始紀錄（採第一筆）
AN_RAW_UNMAPPED = "RAW_UNMAPPED"                        # 原始紀錄沒有格點識別、也沒有 SLOT 參照 → 無法對應格點
AN_RAW_UNPARSEABLE = "RAW_UNPARSEABLE"                  # 原始層某行無法解析
AN_SEGMENT_UNTERMINATED = "SEGMENT_UNTERMINATED"        # 段落沒有 SEGMENT_END（例如當機）→ 尾端不推算
AN_SEGMENT_HEAD_GAP = "SEGMENT_HEAD_GAP"                # 段落第一個已知格點不是 0 → 前段不推算
AN_SEGMENT_END_INCONSISTENT = "SEGMENT_END_INCONSISTENT"  # SEGMENT_END.last_slot_index 小於已知格點
AN_SLOT_AFTER_END = "SLOT_AFTER_SEGMENT_END"            # SEGMENT_END 之後又出現的 SLOT
AN_RAW_DUPLICATE_REF = "RAW_DUPLICATE_REF"              # 同一 raw_ref 對應多筆原始紀錄（歷史 sample_index 重複）
AN_RAW_INCOMPLETE = "RAW_INCOMPLETE"                    # 原始層不完整列（欄位數不符／尾端半行）→ 不視為有效取樣
AN_OFFGRID_REF_MISSING = "OFFGRID_REF_MISSING"          # OFFGRID_SAMPLE 的 raw_ref 不存在
AN_OFFGRID_REF_HAS_IDENT = "OFFGRID_REF_HAS_IDENT"      # OFFGRID_SAMPLE 指向的原始紀錄卻帶有格點識別（矛盾）


def _wall_now():
    return datetime.now().astimezone()


def _iso_ms(dt):
    return dt.isoformat(timespec="milliseconds") if dt is not None else None


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def _as_int(v):
    """格點編號／raw_ref → int；空值或無法解析 → None（CSV 讀回為字串、可能是 '3' 或 '3.0'）。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or f != int(f):
        return None
    return int(f)


def ensure_newline_terminated(path):
    """
    追加前確認檔案以換行結尾：行程中斷可能留下沒有換行的半行，直接追加會把新紀錄黏在半行後面
    → 兩筆都無法解析（新紀錄也一起遺失）。補一個換行，讓半行獨立成一行（讀取端照常略過、記異常）。
    回傳 True＝有補換行。檔案不存在／空檔 → False。
    """
    try:
        if os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    with open(path, "rb+") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return False
        f.seek(0, os.SEEK_END)
        f.write(b"\n")
        return True


def count_physical_lines(path):
    """
    檔案的實體行數（以換行結尾的行，加上沒有換行的最後半行）。給 observations.jsonl 的 raw_ref（實體行號）用。
    """
    try:
        n = 0
        last = b""
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
                last = chunk[-1:]
        if last and last != b"\n":
            n += 1
        return n
    except OSError:
        return 0


def classify_attempt(communication_ok, failed_endpoints, read_error=None, raw_written=True):
    """
    有執行讀取的格點狀態（單次報告用；認證另有 classify_cert_attempt）：
      讀取例外／沒有原始紀錄／關鍵來源失敗（communication_ok 不是 True） → ATTEMPTED_FAIL
      關鍵來源成功、部分端點失敗                                          → ATTEMPTED_PARTIAL
      全部成功                                                            → ATTEMPTED_OK
    """
    if read_error or not raw_written or communication_ok is not True:
        return ATTEMPTED_FAIL
    if failed_endpoints:
        return ATTEMPTED_PARTIAL
    return ATTEMPTED_OK


def classify_cert_attempt(rec, read_error, reading):
    """
    認證格點狀態只依 HMI（D7：6161 另計，不影響 slot_status）：
      HMI 讀取例外／無讀值／HMI 無有效資料（hmi_ok=False） → ATTEMPTED_FAIL
      HMI 有效、但部分端點失敗                             → ATTEMPTED_PARTIAL
      HMI 有效、無端點失敗                                 → ATTEMPTED_OK
    """
    if rec is None or read_error or not isinstance(reading, dict) or not rec.get("hmi_ok"):
        return ATTEMPTED_FAIL
    if reading.get("_fail"):
        return ATTEMPTED_PARTIAL
    return ATTEMPTED_OK


# =============================================================================
# 寫入端
# =============================================================================
class SlotSegment(object):
    """
    一個錄製段落（一次開始／續接／重啟 → 一個段落）。執行緒安全（內部鎖）。

    用法（取樣執行緒）：
        seg = SlotSegment(folder, "session", session_id, interval, reason="start")
        slot = sch.wait(stop)                 # sample_scheduler.SlotScheduler
        seg.begin_from_slot(slot)             # 第一格時寫 SEGMENT_START（之後重複呼叫無作用）
        ...先寫原始紀錄（帶 seg.seg_id、slot["index"]）...
        seg.write_slot(slot["index"], status, raw_ref=..., ...)
        missed = sch.complete()
        seg.write_missed_after(slot["index"], missed, MISSED_READ_OVERRUN, detail)
        ...
        seg.end("pause"|"stop"|"finalize")

    保證：
      - 同一段落內格點編號**嚴格遞增**；同一格第二次寫入 → 拒絕（回 False、計數），每格最多一筆。
      - SEGMENT_END 之後的任何寫入 → 拒絕（回 False、計數）；end() 只寫一次。
      - 任何 I/O 例外只計數（write_errors），不往外拋。
    """

    _seq_lock = threading.Lock()
    _seq = 0

    def __init__(self, folder, owner_kind, owner_id, interval_sec, reason="start",
                 mono=time.monotonic, wall=_wall_now, log_name=SLOT_LOG_FILE):
        self.path = os.path.join(folder, log_name)
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.interval = float(interval_sec)
        self.reason = reason if reason in SEG_REASONS_START else "start"
        self._mono = mono
        self._wall = wall
        self._lock = threading.RLock()
        self.seg_id = None
        self.anchor_mono = None
        self.anchor_wall = None
        self.started = False
        self.closed = False
        self.last_index = None            # 已寫入（ATTEMPTED 或 MISSED）的最大格點編號
        self.counts = {s: 0 for s in WRITER_STATUSES}
        self.rejected = 0                 # 被拒絕的寫入（重複格點／編號倒退／段落已結束）
        self.write_errors = 0
        self.tail_repaired = 0

    # ---- 基本 ----
    @classmethod
    def _next_seq(cls):
        with cls._seq_lock:
            cls._seq += 1
            return cls._seq

    def _append(self, rec):
        try:
            if ensure_newline_terminated(self.path):
                self.tail_repaired += 1
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            return True
        except Exception:                               # noqa: BLE001  Fail Open
            self.write_errors += 1
            return False

    def scheduled_time(self, k):
        if self.anchor_wall is None:
            return None
        return _iso_ms(self.anchor_wall + timedelta(seconds=k * self.interval))

    # ---- 段落開始 ----
    def begin(self, anchor_mono, anchor_wall):
        """寫 SEGMENT_START（只寫一次）。anchor＝第 0 格的期限（monotonic 與對應的具時區時間）。"""
        with self._lock:
            if self.started or self.closed:
                return False
            self.anchor_mono = float(anchor_mono)
            self.anchor_wall = anchor_wall
            self.seg_id = "seg-%s-p%d-%d" % (anchor_wall.strftime("%Y%m%dT%H%M%S%f"), os.getpid(),
                                             self._next_seq())
            self.started = True
            self._append({"rec": REC_SEGMENT_START, "schema": SLOT_SCHEMA, "seg_id": self.seg_id,
                          "owner_kind": self.owner_kind, "owner_id": self.owner_id,
                          "interval_sec": self.interval, "anchor_wall": _iso_ms(anchor_wall),
                          "anchor_mono": round(self.anchor_mono, 6), "reason": self.reason,
                          "pid": os.getpid()})
            return True

    def begin_from_slot(self, slot):
        """由排程器第一格的 slot dict 推出 anchor（scheduled_mono；具時區時間依開始落後量回推）。"""
        with self._lock:
            if self.started:
                return False
            sm = slot.get("scheduled_mono")
            st = slot.get("start_mono")
            sw = slot.get("start_wall") or self._wall()
            if sm is None:
                sm = self._mono() if st is None else st
            lag = (st - sm) if (st is not None) else 0.0
            return self.begin(sm, sw - timedelta(seconds=max(0.0, lag)))

    # ---- 格點 ----
    def _accept_index(self, k):
        if not self.started or self.closed:
            return False
        if not isinstance(k, int) or isinstance(k, bool) or k < 0:
            return False
        if self.last_index is not None and k <= self.last_index:
            return False
        return True

    def write_slot(self, k, status, raw_ref=None, read_start=None, read_done=None, failed_endpoints=None,
                   communication_ok=None, timeout=None, missed_reason=None, extra=None):
        """寫一筆 SLOT。回傳 True＝已寫入；False＝被拒絕（重複／倒退／段落已結束／未開始）或寫入失敗。"""
        with self._lock:
            if status not in WRITER_STATUSES or not self._accept_index(k):
                self.rejected += 1
                return False
            rec = {"rec": REC_SLOT, "seg_id": self.seg_id, "slot_index": k,
                   "scheduled_time": self.scheduled_time(k), "slot_status": status,
                   "raw_ref": None if status == MISSED else raw_ref,
                   "read_start": None if status == MISSED else read_start,
                   "read_done": None if status == MISSED else read_done,
                   "failed_endpoints": list(failed_endpoints or []) if status != MISSED else [],
                   "communication_ok": None if status == MISSED else communication_ok,
                   "timeout": None if status == MISSED else timeout,
                   "missed_reason": missed_reason if status == MISSED else None}
            if extra:
                for kk, vv in extra.items():
                    rec.setdefault(kk, vv)
            self.last_index = k                  # 先佔用編號：即使寫檔失敗也不會在同一格寫第二筆
            ok = self._append(rec)
            if ok:
                self.counts[status] += 1
            return ok

    def write_missed_range(self, first, n, reason, detail=None):
        """first…first+n-1 每格一筆 MISSED。回傳實際寫入筆數。"""
        wrote = 0
        if not n or n <= 0:
            return 0
        for k in range(first, first + n):
            if self.write_slot(k, MISSED, missed_reason=(reason if not detail else "%s：%s" % (reason, detail))):
                wrote += 1
        return wrote

    def write_missed_after(self, k_done, n, reason=MISSED_READ_OVERRUN, detail=None):
        """排程器 complete() 回報第 k_done 格之後跳過 n 格 → k_done+1 … k_done+n 各一筆 MISSED。"""
        if k_done is None or not n:
            return 0
        return self.write_missed_range(k_done + 1, n, reason, detail)

    def claim_extra_slot(self, reason=MISSED_STOP_TRANSITION):
        """
        取樣迴圈之外的一次取樣（單次報告停止時的最後一筆、認證結束前的最後一筆）也要有格點身分：
        格點＝目前時間所在的格（floor），且必須大於已用的最後一格；中間沒有執行讀取的格點寫 MISSED（reason）。
        回傳與排程器相同格式的 slot dict（多 seg_id 鍵）；段落未開始或已結束 → None（不寫格點）。
        """
        with self._lock:
            if not self.started or self.closed:
                return None
            now = self._mono()
            k = int(math.floor((now - self.anchor_mono) / self.interval + 1e-9))
            nxt = 0 if self.last_index is None else self.last_index + 1
            k = max(k, nxt)
            if k > nxt:
                self.write_missed_range(nxt, k - nxt, reason)
            due = self.anchor_mono + k * self.interval
            return {"index": k, "scheduled_mono": due, "start_mono": now, "start_wall": self._wall(),
                    "lag_ms": round((now - due) * 1000.0, 1), "seg_id": self.seg_id, "extra": True}

    # ---- 段落結束 ----
    def end(self, reason):
        """寫 SEGMENT_END（只寫一次；段落沒開始 → 不寫）。回傳 True＝本次寫入。"""
        with self._lock:
            if self.closed:
                return False
            self.closed = True
            if not self.started:
                return False
            return self._append({"rec": REC_SEGMENT_END, "seg_id": self.seg_id,
                                 "last_slot_index": self.last_index,
                                 "reason": reason if reason in SEG_REASONS_END else "stop",
                                 "end_wall": _iso_ms(self._wall())})

    def ident(self, k):
        """原始紀錄要帶的格點識別。"""
        return {"slot_seg_id": self.seg_id, "slot_index": k}


def write_offgrid_record(folder, raw_ref, reason=OFFGRID_FINALIZE_RETRY, after_seg_id=None, read_start=None,
                         read_done=None, read_time_source=READ_TIME_UNAVAILABLE, communication_ok=None,
                         failed_endpoints=None, extra=None, log_name=SLOT_LOG_FILE):
    """
    方案 C：段落已結束後的真實原始取樣 → slots.jsonl 一筆 OFFGRID_SAMPLE（沒有 slot_index，不偽造格點）。
    回傳 True＝已寫入；任何失敗 Fail Open 回 False。
    """
    try:
        path = os.path.join(folder, log_name)
        ensure_newline_terminated(path)
        rec = {"rec": REC_OFFGRID, "raw_ref": raw_ref, "reason": reason, "after_seg_id": after_seg_id,
               "read_start": read_start, "read_done": read_done, "read_time_source": read_time_source,
               "communication_ok": communication_ok, "failed_endpoints": list(failed_endpoints or []),
               "wall": _iso_ms(_wall_now())}
        if extra:
            for k, v in extra.items():
                rec.setdefault(k, v)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception:                                   # noqa: BLE001
        return False


# =============================================================================
# 讀取端
# =============================================================================
def read_slot_log(path):
    """
    讀 slots.jsonl → (records, anomalies)。records 每筆多一個 _line（實體行號，1 起算）。
    最後一行沒有換行且無法解析 → TAIL_INCOMPLETE；中間無法解析 → LINE_UNPARSEABLE。皆略過、不補造。
    檔案不存在 → (None, [])。
    """
    if not os.path.exists(path):
        return None, []
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    tail_unterminated = bool(data) and not data.endswith(b"\n")
    if not tail_unterminated and lines and lines[-1] == b"":
        lines = lines[:-1]
    recs, an = [], []
    last_i = len(lines) - 1
    for i, raw in enumerate(lines):
        ln = i + 1
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError):
            kind = AN_TAIL_INCOMPLETE if (i == last_i and tail_unterminated) else AN_LINE_UNPARSEABLE
            an.append(_anomaly(kind, None, None, "slots.jsonl 第 %d 行無法解析（略過）" % ln, line=ln))
            continue
        obj["_line"] = ln
        recs.append(obj)
    return recs, an


def _anomaly(kind, seg_id, slot_index, detail, **kw):
    a = {"type": kind, "seg_id": seg_id, "slot_index": slot_index, "detail": detail}
    a.update(kw)
    return a


def load_cert_raw(path):
    """
    observations.jsonl → (raw list, anomalies)。raw：{"raw_ref": 實體行號, "seg_id", "slot_index", "record"}。
    無法解析的行 → RAW_UNPARSEABLE（最後半行同樣略過）。
    """
    out, an = [], []
    if not os.path.exists(path):
        return out, an
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines = lines[:-1]
    for i, raw in enumerate(lines):
        ln = i + 1
        s = raw.strip()
        if not s:
            continue
        try:
            rec = json.loads(s.decode("utf-8"))
            if not isinstance(rec, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            an.append(_anomaly(AN_RAW_UNPARSEABLE, None, None, "observations.jsonl 第 %d 行無法解析（略過）" % ln,
                               raw_ref=ln))
            continue
        out.append({"raw_ref": ln, "seg_id": rec.get("slot_seg_id") or None,
                    "slot_index": _as_int(rec.get("slot_index")), "record": rec})
    return out, an


def load_single_raw(path_or_rows, incomplete_lines=None):
    """
    單次報告 samples.csv（路徑或已讀回的 list[dict]）→ (raw list, anomalies)。
    raw：{"raw_ref": sample_index, "seg_id", "slot_index", "record", "line"}。舊版 CSV 沒有兩個格點欄位 → 識別為 None。
    不完整列（欄位數與標題不符，或 incomplete_lines 列出的實體行號）→ RAW_INCOMPLETE 異常，不列入原始層。
    """
    import csv
    bad = set(incomplete_lines or [])
    numbered = []
    if isinstance(path_or_rows, str):
        if os.path.exists(path_or_rows):
            with open(path_or_rows, "r", encoding="utf-8-sig", newline="") as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    numbered.append((rdr.line_num, row))
    else:
        numbered = [(None, r) for r in (path_or_rows or [])]
    out, an = [], []
    for n, (line_no, row) in enumerate(numbered):
        if None in row or any(v is None for v in row.values()) or (line_no is not None and line_no in bad):
            an.append(_anomaly(AN_RAW_INCOMPLETE, None, None, "samples.csv 第 %s 行不完整（保留原樣、不視為有效取樣）"
                               % line_no, line=line_no, raw_ref=_as_int(row.get("sample_index"))))
            continue
        ref = _as_int(row.get("sample_index"))
        if ref is None:
            an.append(_anomaly(AN_RAW_UNPARSEABLE, None, None, "samples.csv 第 %d 筆沒有有效 sample_index" % (n + 1)))
            continue
        out.append({"raw_ref": ref, "seg_id": (row.get("slot_seg_id") or None),
                    "slot_index": _as_int(row.get("slot_index")), "record": row, "line": line_no})
    return out, an


def csv_tail_incomplete_lines(events_path, file_name="samples.csv"):
    """events.csv 的 csv_tail_incomplete 事件（file=<file_name> line=<N>）→ 行號清單（唯讀）。"""
    import csv
    out = []
    if not os.path.exists(events_path):
        return out
    try:
        with open(events_path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if (row or {}).get("event_type") != "csv_tail_incomplete":
                    continue
                d = row.get("detail") or ""
                if "file=%s " % file_name not in d:
                    continue
                try:
                    out.append(int(d.split("line=", 1)[1].split("｜", 1)[0].split()[0]))
                except (IndexError, ValueError):
                    pass
    except (OSError, csv.Error):
        pass
    return out


# =============================================================================
# 重建
# =============================================================================
def reconstruct(slot_records, raw_items, slot_log_anomalies=None, raw_anomalies=None):
    """
    格點層＋原始層 → 重建後的格點序列。純函式（不讀寫檔、無副作用）；同樣輸入 → 同樣輸出。

    slot_records：read_slot_log() 的 records；None＝沒有 slots.jsonl（舊版 Session）
    raw_items：load_cert_raw()/load_single_raw() 的 raw list

    同一 (seg_id, slot_index) 只產生一筆，優先順序（Q5）：
      1. 已落盤的 SLOT（ATTEMPTED_*／MISSED；MISSED 但有原始紀錄 → ATTEMPTED_CONFLICT）
      2. 沒有 SLOT、原始紀錄帶此格點識別 → ATTEMPTED_SLOT_LOST
      3. 沒有 SLOT、沒有原始紀錄，但有證據 → INFERRED_MISSED
           a) 同段落兩個已知格點 k1<k2 之間的缺號
           b) 段落有 SEGMENT_END，且 last_slot_index 大於最後已知格點
      4. 其他（段落沒有 END 的尾端、第一個已知格點之前、跨段落、只靠牆上時間差） → 只記異常，不產生格點

    回傳 dict：
      has_slot_log、slots（依段落開始順序、段落內格點順序）、segments、anomalies、counts、raw_map（raw_ref → (seg, k)）
      每筆 slot：seg_id、slot_index、scheduled_time、status、source（logged／rebuilt_from_raw／inferred）、
                 raw_ref、evidence、anomalies（此格的異常類型）、complete_eligible
    """
    anomalies = list(slot_log_anomalies or []) + list(raw_anomalies or [])
    result = {"has_slot_log": slot_records is not None, "slots": [], "segments": [],
              "anomalies": anomalies, "counts": {}, "raw_map": {}, "legacy_raw_without_slot_log": 0,
              "legacy_raw_before_slot_log": 0, "offgrid": [], "raw_ref_conflicts": {}}
    raw_items = list(raw_items or [])
    if slot_records is None:
        # 舊版 Session：沒有格點紀錄 → 不推算 MISSED／INFERRED_MISSED，只照實際取樣列
        result["legacy_raw_without_slot_log"] = len(raw_items)
        return result

    # ---- 1. 段落與 SLOT ----
    seg_order, seg_start, seg_end = [], {}, {}
    slots = {}                       # (seg, k) → SLOT 紀錄（第一筆）
    slot_order = []
    offgrid_recs = []
    for r in slot_records:
        kind = r.get("rec")
        sid = r.get("seg_id")
        if kind == REC_OFFGRID:
            offgrid_recs.append(r)                       # 非格點紀錄：沒有 slot_index，另外處理
            continue
        if not sid or kind not in (REC_SEGMENT_START, REC_SLOT, REC_SEGMENT_END):
            anomalies.append(_anomaly(AN_RECORD_INVALID, sid, None, "slots.jsonl 第 %s 行欄位不合法（略過）"
                                      % r.get("_line"), line=r.get("_line")))
            continue
        if kind == REC_SEGMENT_START:
            if sid in seg_start:
                anomalies.append(_anomaly(AN_DUPLICATE_SEGMENT_START, sid, None, "重複的 SEGMENT_START（保留第一筆）",
                                          line=r.get("_line")))
                continue
            seg_start[sid] = r
            if sid not in seg_order:
                seg_order.append(sid)
        elif kind == REC_SEGMENT_END:
            if sid in seg_end:
                anomalies.append(_anomaly(AN_DUPLICATE_SEGMENT_END, sid, None, "重複的 SEGMENT_END（保留第一筆）",
                                          line=r.get("_line")))
                continue
            seg_end[sid] = r
            if sid not in seg_order:
                seg_order.append(sid)
        else:
            k = _as_int(r.get("slot_index"))
            st = r.get("slot_status")
            if k is None or k < 0 or st not in WRITER_STATUSES:
                anomalies.append(_anomaly(AN_RECORD_INVALID, sid, k, "SLOT 欄位不合法（略過）", line=r.get("_line")))
                continue
            if sid in seg_end:
                anomalies.append(_anomaly(AN_SLOT_AFTER_END, sid, k, "SEGMENT_END 之後出現的 SLOT（仍保留，標記異常）",
                                          line=r.get("_line")))
            if (sid, k) in slots:
                anomalies.append(_anomaly(AN_DUPLICATE_SLOT, sid, k, "同一格點有多筆 SLOT（保留第一筆）",
                                          line=r.get("_line")))
                continue
            slots[(sid, k)] = r
            slot_order.append((sid, k))
            if sid not in seg_order:
                seg_order.append(sid)
    for sid in seg_order:
        if sid not in seg_start and any(s == sid for (s, _k) in slots):
            anomalies.append(_anomaly(AN_SLOT_WITHOUT_START, sid, None, "段落沒有 SEGMENT_START（SLOT 仍保留）"))

    # ---- 2. 原始層索引 ----
    raw_by_ref = {}                  # raw_ref → [原始紀錄]（多筆＝歷史 raw_ref 重複）
    raw_by_ident = {}
    raw_dup_slots = set()
    for i, it in enumerate(raw_items):
        it["_i"] = i
        ref = it.get("raw_ref")
        if ref is not None:
            raw_by_ref.setdefault(ref, []).append(it)
        sid, k = it.get("seg_id"), it.get("slot_index")
        if sid and k is not None:
            if (sid, k) in raw_by_ident:
                raw_dup_slots.add((sid, k))
                anomalies.append(_anomaly(AN_RAW_DUPLICATE_IDENT, sid, k, "同一格點有多筆原始紀錄（採第一筆 raw_ref=%s，"
                                          "本筆 raw_ref=%s）" % (raw_by_ident[(sid, k)]["raw_ref"], ref), raw_ref=ref))
            else:
                raw_by_ident[(sid, k)] = it

    dup_refs = {ref for ref, lst in raw_by_ref.items() if len(lst) > 1}
    for ref in sorted(dup_refs):
        lst = raw_by_ref[ref]
        anomalies.append(_anomaly(AN_RAW_DUPLICATE_REF, None, None,
                                  "raw_ref=%s 對應 %d 筆原始紀錄（%s）→ 不採用任一筆作為唯一對應"
                                  % (ref, len(lst), "、".join("行%s/識別(%s,%s)" % (x.get("line"), x.get("seg_id"),
                                                                                x.get("slot_index")) for x in lst)),
                                  raw_ref=ref))

    # ---- 3. 逐格決定 ----
    entries = {}                     # (seg, k) → entry
    used_raw_refs = set()            # 已被引用的原始紀錄 raw_ref（供「啟用格點紀錄之前」的判斷）
    used_items = set()               # 已被引用的原始紀錄（逐筆；raw_ref 重複時仍能分辨）

    def _use(it):
        used_items.add(it["_i"])
        if it.get("raw_ref") is not None:
            used_raw_refs.add(it["raw_ref"])

    def _entry(sid, k, status, source, raw_ref=None, evidence=None, slot_rec=None):
        e = {"seg_id": sid, "slot_index": k, "status": status, "source": source, "raw_ref": raw_ref,
             "scheduled_time": (slot_rec or {}).get("scheduled_time") or _sched_from_start(seg_start.get(sid), k),
             "evidence": evidence, "anomalies": [], "slot_line": (slot_rec or {}).get("_line")}
        if slot_rec is not None:
            for key in ("read_start", "read_done", "failed_endpoints", "communication_ok", "timeout", "missed_reason"):
                e[key] = slot_rec.get(key)
            for key, val in slot_rec.items():           # 寫入端額外欄位（例如 heat_status）照帶
                if key not in e and key not in ("rec", "slot_status", "_line"):
                    e[key] = val
        entries[(sid, k)] = e
        return e

    def _flag(e, kind, detail, **kw):
        e["anomalies"].append(kind)
        anomalies.append(_anomaly(kind, e["seg_id"], e["slot_index"], detail, **kw))

    # 3-1 已落盤的 SLOT
    for (sid, k) in slot_order:
        r = slots[(sid, k)]
        st = r.get("slot_status")
        ident_raw = raw_by_ident.get((sid, k))
        if st == MISSED:
            if ident_raw is not None:
                e = _entry(sid, k, ATTEMPTED_CONFLICT, "logged", raw_ref=ident_raw["raw_ref"], slot_rec=r)
                _flag(e, AN_STATUS_CONFLICT, "SLOT=MISSED 但有帶此格點識別的原始紀錄 raw_ref=%s（以有讀取顯示，不計入完整）"
                      % ident_raw["raw_ref"], raw_ref=ident_raw["raw_ref"])
                _use(ident_raw)
            else:
                _entry(sid, k, MISSED, "logged", slot_rec=r)
            continue
        ref = _as_int(r.get("raw_ref"))
        e = _entry(sid, k, st, "logged", raw_ref=ref, slot_rec=r)
        if ref is None:
            if ident_raw is not None:
                e["raw_ref"] = ident_raw["raw_ref"]
                _use(ident_raw)
                _flag(e, AN_REF_NULL_RAW_EXISTS, "SLOT 沒有 raw_ref，但有帶此格點識別的原始紀錄 raw_ref=%s"
                      % ident_raw["raw_ref"], raw_ref=ident_raw["raw_ref"])
            elif st != ATTEMPTED_FAIL:
                _flag(e, AN_REF_MISSING, "ATTEMPTED 狀態卻沒有 raw_ref")
            continue
        cands = raw_by_ref.get(ref) or []
        if ref in dup_refs:
            # 歷史 raw_ref 重複：格點識別可辨識是哪一筆，但衝突仍明確標記（不計入完整、不靜默採用第一筆）
            e["anomalies"].append(AN_RAW_DUPLICATE_REF)
            hit = [x for x in cands if (x.get("seg_id"), x.get("slot_index")) == (sid, k)]
            if len(hit) == 1:
                _use(hit[0])
                e["raw_ident_verified"] = True
            else:
                e["raw_ref_ambiguous"] = True            # 無法唯一辨識 → 不採用任一筆的值
                e["raw_ident_verified"] = False
                for x in cands:
                    if x.get("seg_id") is None and x.get("slot_index") is None:
                        _use(x)                          # 已由 RAW_DUPLICATE_REF 涵蓋，不另報 RAW_UNMAPPED
            continue
        raw = cands[0] if cands else None
        if raw is None:
            if ident_raw is not None:
                e["raw_ref"] = ident_raw["raw_ref"]
                _use(ident_raw)
                _flag(e, AN_REF_MISMATCH, "raw_ref=%s 不存在；依格點識別找到 raw_ref=%s" % (ref, ident_raw["raw_ref"]),
                      raw_ref=ref)
            else:
                e["raw_ref"] = None
                _flag(e, AN_REF_MISSING, "raw_ref=%s 指向的原始紀錄不存在" % ref, raw_ref=ref)
            continue
        r_sid, r_k = raw.get("seg_id"), raw.get("slot_index")
        if r_sid is None and r_k is None:
            # 舊欄位續接（CSV 沒有格點欄位）：只能以 raw_ref 對應，無法驗證識別
            e["raw_ident_verified"] = False
            _use(raw)
            continue
        if (r_sid, r_k) != (sid, k):
            if ident_raw is not None:
                e["raw_ref"] = ident_raw["raw_ref"]
                _use(ident_raw)
                _flag(e, AN_REF_MISMATCH, "raw_ref=%s 的格點識別為 (%s, %s)，與本格不同；依格點識別改用 raw_ref=%s"
                      % (ref, r_sid, r_k, ident_raw["raw_ref"]), raw_ref=ref)
            else:
                e["raw_ref"] = None
                _flag(e, AN_REF_MISMATCH, "raw_ref=%s 的格點識別為 (%s, %s)，與本格不同；找不到本格的原始紀錄"
                      % (ref, r_sid, r_k), raw_ref=ref)
            continue
        e["raw_ident_verified"] = True
        _use(raw)

    # 3-2 原始紀錄存在、SLOT 未落盤 → ATTEMPTED_SLOT_LOST
    for (sid, k), raw in raw_by_ident.items():
        if (sid, k) in entries:
            continue
        e = _entry(sid, k, ATTEMPTED_SLOT_LOST, "rebuilt_from_raw", raw_ref=raw["raw_ref"])
        e["raw_ident_verified"] = True
        _use(raw)
        if raw.get("raw_ref") in dup_refs:
            e["anomalies"].append(AN_RAW_DUPLICATE_REF)
        _flag(e, AN_SLOT_LOST, "原始紀錄 raw_ref=%s 帶有此格點識別，但 SLOT 未落盤（格點紀錄遺失，依原始紀錄重建）"
              % raw["raw_ref"], raw_ref=raw["raw_ref"])
        if sid not in seg_order:
            seg_order.append(sid)
            anomalies.append(_anomaly(AN_SLOT_WITHOUT_START, sid, None,
                                      "原始紀錄引用的段落在 slots.jsonl 中沒有任何紀錄"))

    # 3-3 同一格多筆原始紀錄 → 此格不計入完整
    for key in raw_dup_slots:
        if key in entries:
            entries[key]["anomalies"].append(AN_RAW_DUPLICATE_IDENT)

    # 3-3b 非格點紀錄（方案 C）：段落結束後的真實原始取樣；不屬於任何格點、不是異常
    for og in offgrid_recs:
        ref = _as_int(og.get("raw_ref"))
        cands = raw_by_ref.get(ref) or []
        item = {"raw_ref": ref, "reason": og.get("reason"), "after_seg_id": og.get("after_seg_id"),
                "read_start": og.get("read_start"), "read_done": og.get("read_done"),
                "read_time_source": og.get("read_time_source"), "communication_ok": og.get("communication_ok"),
                "wall": og.get("wall"), "slot_line": og.get("_line"), "anomalies": []}
        if not cands:
            item["anomalies"].append(AN_OFFGRID_REF_MISSING)
            anomalies.append(_anomaly(AN_OFFGRID_REF_MISSING, og.get("after_seg_id"), None,
                                      "OFFGRID_SAMPLE raw_ref=%s 指向的原始紀錄不存在" % ref, raw_ref=ref))
        else:
            if ref in dup_refs:
                item["anomalies"].append(AN_RAW_DUPLICATE_REF)
            for x in cands:
                if x.get("seg_id") or x.get("slot_index") is not None:
                    item["anomalies"].append(AN_OFFGRID_REF_HAS_IDENT)
                    anomalies.append(_anomaly(AN_OFFGRID_REF_HAS_IDENT, x.get("seg_id"), x.get("slot_index"),
                                              "OFFGRID_SAMPLE raw_ref=%s 指向帶格點識別的原始紀錄" % ref, raw_ref=ref))
                elif x["_i"] not in used_items:
                    used_items.add(x["_i"])              # 不計入 used_raw_refs：不影響「啟用格點紀錄之前」的判斷
        result["offgrid"].append(item)

    # 3-4 原始紀錄沒有格點識別、也沒被引用
    ref_list = sorted(r for r in used_raw_refs if r is not None)
    first_ref = ref_list[0] if ref_list else None
    for it in raw_items:
        ref = it.get("raw_ref")
        if it["_i"] in used_items:
            continue
        if it.get("seg_id") and it.get("slot_index") is not None:
            continue                                     # 已由 3-2 處理（或為重複識別）
        if first_ref is None or (ref is not None and ref < first_ref):
            result["legacy_raw_before_slot_log"] += 1    # 啟用格點紀錄之前的舊資料（不是異常）
            continue
        anomalies.append(_anomaly(AN_RAW_UNMAPPED, None, None, "原始紀錄 raw_ref=%s 沒有格點識別，也沒有 SLOT 參照"
                                  "（無法對應格點，不推算）" % ref, raw_ref=ref))

    # 3-5 INFERRED_MISSED（只在同段落內、有證據時）
    seg_summ = []
    for sid in seg_order:
        ks = sorted(k for (s, k) in entries if s == sid)
        end = seg_end.get(sid)
        last_end = _as_int(end.get("last_slot_index")) if end else None
        inferred = 0
        if ks:
            if ks[0] > 0:
                anomalies.append(_anomaly(AN_SEGMENT_HEAD_GAP, sid, None, "段落第一個已知格點為 %d（0～%d 無紀錄，不推算）"
                                          % (ks[0], ks[0] - 1)))
            for a, b in zip(ks, ks[1:]):
                if b - a > 1:
                    ev = "seg %s：%d 與 %d 之間缺 %d 格" % (sid, a, b, b - a - 1)
                    for k in range(a + 1, b):
                        _entry(sid, k, INFERRED_MISSED, "inferred", evidence=ev)
                        inferred += 1
            if end is not None:
                if last_end is not None and last_end > ks[-1]:
                    ev = "seg %s：SEGMENT_END.last_slot_index=%d，最後已知格點 %d" % (sid, last_end, ks[-1])
                    for k in range(ks[-1] + 1, last_end + 1):
                        _entry(sid, k, INFERRED_MISSED, "inferred", evidence=ev)
                        inferred += 1
                elif last_end is None or last_end < ks[-1]:
                    anomalies.append(_anomaly(AN_SEGMENT_END_INCONSISTENT, sid, None,
                                              "SEGMENT_END.last_slot_index=%s 小於最後已知格點 %d" % (last_end, ks[-1])))
            else:
                anomalies.append(_anomaly(AN_SEGMENT_UNTERMINATED, sid, None,
                                          "段落沒有 SEGMENT_END（最後已知格點 %d 之後不推算）" % ks[-1]))
        else:
            if end is None:
                anomalies.append(_anomaly(AN_SEGMENT_UNTERMINATED, sid, None, "段落沒有 SEGMENT_END，也沒有任何格點"))
            elif last_end is not None:
                anomalies.append(_anomaly(AN_SEGMENT_HEAD_GAP, sid, None, "SEGMENT_END.last_slot_index=%d，但沒有任何已知格點"
                                          "（不推算）" % last_end))
        st = seg_start.get(sid) or {}
        seg_summ.append({"seg_id": sid, "reason": st.get("reason"), "interval_sec": st.get("interval_sec"),
                         "anchor_wall": st.get("anchor_wall"), "owner_id": st.get("owner_id"),
                         "has_start": sid in seg_start, "has_end": end is not None,
                         "end_reason": (end or {}).get("reason"), "last_slot_index": last_end,
                         "known_slots": len(ks), "inferred_missed": inferred})

    # ---- 4. 輸出 ----
    counts = {}
    for sid in seg_order:
        for k in sorted(k for (s, k) in entries if s == sid):
            e = entries[(sid, k)]
            e["complete_eligible"] = (e["status"] in (ATTEMPTED_OK, ATTEMPTED_PARTIAL) and not e["anomalies"])
            result["slots"].append(e)
            counts[e["status"]] = counts.get(e["status"], 0) + 1
            if e.get("raw_ref") is not None and e["status"] in ATTEMPTED_STATUSES:
                if e["raw_ref"] in dup_refs:
                    result["raw_ref_conflicts"].setdefault(e["raw_ref"], []).append((sid, k))
                else:
                    result["raw_map"].setdefault(e["raw_ref"], (sid, k))
    result["segments"] = seg_summ
    result["counts"] = counts
    for ref in dup_refs:
        result["raw_ref_conflicts"].setdefault(ref, [])
    for it in raw_items:
        it.pop("_i", None)
    return result


def _sched_from_start(start_rec, k):
    if not start_rec:
        return None
    aw = _parse_iso(start_rec.get("anchor_wall"))
    iv = start_rec.get("interval_sec")
    if aw is None or not isinstance(iv, (int, float)):
        return None
    return _iso_ms(aw + timedelta(seconds=k * float(iv)))


def check_consistency(result):
    """
    重建結果的不變式（測試與診斷用）。回傳違反清單（空＝一致）：
      - 每個 (seg_id, slot_index) 只有一筆
      - 同一格不會同時是 ATTEMPTED 與 INFERRED_MISSED
      - MISSED／INFERRED_MISSED 沒有 raw_ref
      - 每個 raw_ref 最多對應一個 ATTEMPTED 格點
      - INFERRED_MISSED 一定有 evidence；不會出現在沒有 SEGMENT_END 的段落尾端
    """
    bad = []
    seen = {}
    ref_owner = {}
    seg_info = {s["seg_id"]: s for s in result.get("segments", [])}
    for e in result.get("slots", []):
        key = (e["seg_id"], e["slot_index"])
        if key in seen:
            bad.append("重複格點 %r（%s / %s）" % (key, seen[key], e["status"]))
        seen[key] = e["status"]
        if e["status"] in NOT_ATTEMPTED_STATUSES and e.get("raw_ref") is not None:
            bad.append("%r 狀態 %s 卻有 raw_ref" % (key, e["status"]))
        if e["status"] in ATTEMPTED_STATUSES and e.get("raw_ref") is not None:
            if e["raw_ref"] in result.get("raw_ref_conflicts", {}):
                if AN_RAW_DUPLICATE_REF not in e.get("anomalies", []):
                    bad.append("%r 使用重複的 raw_ref=%s 卻沒有標記 RAW_DUPLICATE_REF" % (key, e["raw_ref"]))
                if e.get("complete_eligible"):
                    bad.append("%r raw_ref 重複卻被標為可計入完整" % (key,))
                continue
            if e["raw_ref"] in ref_owner and ref_owner[e["raw_ref"]] != key:
                bad.append("raw_ref=%s 同時對應 %r 與 %r" % (e["raw_ref"], ref_owner[e["raw_ref"]], key))
            ref_owner[e["raw_ref"]] = key
        if e["status"] == INFERRED_MISSED:
            if not e.get("evidence"):
                bad.append("%r INFERRED_MISSED 沒有證據" % (key,))
            si = seg_info.get(e["seg_id"]) or {}
            known_max = max([x["slot_index"] for x in result["slots"]
                             if x["seg_id"] == e["seg_id"] and x["status"] != INFERRED_MISSED] or [-1])
            if e["slot_index"] > known_max and not si.get("has_end"):
                bad.append("%r 在沒有 SEGMENT_END 的段落尾端被推算" % (key,))
        if e["status"] in (MISSED, INFERRED_MISSED) and e.get("complete_eligible"):
            bad.append("%r 未讀取的格點被標為可計入完整" % (key,))
    og_refs = {o.get("raw_ref") for o in result.get("offgrid", []) if o.get("raw_ref") is not None}
    for ref in og_refs & set(ref_owner):
        if ref not in result.get("raw_ref_conflicts", {}):
            bad.append("raw_ref=%s 同時是非格點紀錄與格點 %r" % (ref, ref_owner[ref]))
    return bad


def reconstruct_folder(folder, kind):
    """
    便利函式：kind＝"cert"（observations.jsonl）或 "single"（samples.csv）。舊 Session 沒有 slots.jsonl → has_slot_log=False。
    """
    recs, an = read_slot_log(os.path.join(folder, SLOT_LOG_FILE))
    if kind == "cert":
        raw, ran = load_cert_raw(os.path.join(folder, "observations.jsonl"))
    else:
        raw, ran = load_single_raw(os.path.join(folder, "samples.csv"),
                                   incomplete_lines=csv_tail_incomplete_lines(os.path.join(folder, "events.csv")))
    return reconstruct(recs, raw, an, ran)


def summarize(result):
    """重建結果的品質摘要（Phase A：只提供計數；Excel 呈現屬 Phase B）。"""
    c = result.get("counts", {})
    types = {}
    for a in result.get("anomalies", []):
        types[a["type"]] = types.get(a["type"], 0) + 1
    return {"has_slot_log": result.get("has_slot_log"),
            "segments": len(result.get("segments", [])),
            "slots_total": len(result.get("slots", [])),
            "status_counts": dict(sorted(c.items())),
            "complete_eligible": sum(1 for e in result.get("slots", []) if e.get("complete_eligible")),
            "anomaly_counts": dict(sorted(types.items())),
            "legacy_raw_without_slot_log": result.get("legacy_raw_without_slot_log", 0),
            "legacy_raw_before_slot_log": result.get("legacy_raw_before_slot_log", 0),
            "offgrid_samples": len(result.get("offgrid", [])),
            "raw_ref_conflicts": len(result.get("raw_ref_conflicts", {}))}
