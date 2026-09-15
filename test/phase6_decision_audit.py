# -*- coding: utf-8 -*-
"""
phase6_decision_audit.py — Phase 6 Durable Decision Audit（GAP-4）
======================================================================
用途
    讓「這一輪為什麼 CHARGE / DISCHARGE / 沒動作 / 被誰擋下」在 process
    重啟之後仍然查得到。`AutoControlRuntime.audit` 是 200 筆記憶體 ring，
    只能回答「剛剛」；本模組負責歷史 trace。

    RAM ring   = recent diagnostics（不取代、不移除）
    Durable    = historical evidence

🔴 **本模組沒有任何控制能力**：不 import 任何專案模組 ——
   不碰 runtime / arbiter / executor / operator / api_client / requests。
   它只做「把已經算好的事實寫成一行 JSON」。
🔴 **不是** Phase 6.10 ownership journal：沒有 intent→outcome 狀態機、
   沒有 handoff state、**不得**作為控制狀態恢復來源。
   正式 recovery 仍走 fresh observation + LastControlStore + ExecutionReconciler。
🔴 **不得增加任何 device I/O**：AuditRecord 一律使用該 cycle **已經取得**的
   observation，絕不為了寫稽核而再讀一次 ESS 或電表。

已裁示的 policy
    AUDIT_GRANULARITY          ONE_CYCLE_ONE_RECORD
    AUDIT_FORMAT               APPEND_ONLY_JSONL
    AUDIT_FSYNC_POLICY         EVERY_CYCLE（append → flush → fsync）
    AUDIT_WRITE_FAILURE_POLICY NON_GATING_CONTINUE
    ROTATION_POLICY            DEFERRED
    RETENTION_POLICY           DEFERRED

用法（完全離線）
    python phase6_decision_audit.py --demo
"""
import io
import os
import json
import math
import uuid
import argparse
import datetime

SCHEMA_VERSION = 1

# ---- 已裁示的 policy（字串常數，供 wiring report 與測試引用）----
AUDIT_GRANULARITY = "ONE_CYCLE_ONE_RECORD"
AUDIT_FORMAT = "APPEND_ONLY_JSONL"
AUDIT_FSYNC_POLICY = "EVERY_CYCLE"
AUDIT_WRITE_FAILURE_POLICY = "NON_GATING_CONTINUE"
# 🔴 尚未裁示 —— 不得自行填 10MB / 100MB / 30 days / 5 backups。
ROTATION_POLICY = "DEFERRED"
RETENTION_POLICY = "DEFERRED"

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AUDIT_DIR = os.path.join(os.path.dirname(HERE), "output", "phase6_audit")
DEFAULT_AUDIT_PATH = os.path.join(DEFAULT_AUDIT_DIR, "decision_audit.jsonl")

# `detail` 是自由文字，設上限避免單行無限膨脹（超出即截斷並標記）。
DETAIL_MAX_CHARS = 500
TRUNCATED_SUFFIX = "…[TRUNCATED]"

# 回復 seq 時從檔尾讀取的位元組數（不整檔掃描 —— 檔案會長到數十萬行）。
TAIL_READ_BYTES = 64 * 1024

# ---- 寫入結果 ----
W_OK = "AUDIT_WRITE_OK"
W_FAILED = "AUDIT_WRITE_FAILED"
W_DISABLED = "AUDIT_SINK_NOT_CONFIGURED"


# ======================================================================
# 欄位契約
# ======================================================================
# 🔴 REQUIRED KEY：以下每個 key **必須存在**於每一筆紀錄。
#    VALUE 可以是 null —— 觀測不到就是 null，並由對應的 *_reason 說明原因。
#    「能回答問題」的標準是：**有值 或 有明確 reason**，不是「全部非 None」。
FIELDS = (
    # Identity
    "schema_version", "seq", "run_id", "boot_id", "cycle",
    # Time
    #   at_wall      Asia/Taipei aware ISO-8601，**僅供人讀的鑑識時間戳**
    #   at_monotonic **唯一**用於 elapsed / freshness / TTL 的時間基準
    "at_wall", "at_monotonic",
    # Runtime
    "runtime_state", "runtime_reason", "audit_event",
    # Meter / Grid
    "meter_power_kw", "meter_valid", "meter_reason", "meter_age_sec",
    "grid_state", "grid_reason",
    # Tariff
    "tou_state", "tou_reason",
    # ESS
    "soc_percent", "ess_valid", "ess_reason", "ess_age_sec",
    # PCS（一律來自 observation，**不得**由上一輪意圖推導）
    "pcs_state",
    # Decision
    "decision_action", "decision_reason", "decision_target_kw", "would_action",
    # Authority
    "authority_state", "authority_reason",
    # Safety
    "safety_allowed", "safety_reason", "safety_check_count",
    # Direction interlock
    "direction_reason", "control_action",
    # Authorization
    "authorized", "authorization_id", "authorization_state",
    # Execution
    "dispatched", "executed", "execution_outcome", "lastcontrol_written",
    "phases",
    # Recovery
    "recovery_outcome", "no_action_reason",
    "detail",
    # Audit self-observability
    "audit_sink_healthy",
)

# 這些 key 一律不得出現在任何一筆紀錄中（敏感資訊 / 原始 payload）。
FORBIDDEN_KEYS = frozenset({
    "password", "passwd", "token", "access_token", "bearer", "authorization_header",
    "cookie", "session", "secret", "private_key", "privatekey", "sm2",
    "login", "credential", "credentials", "alarm_rows", "raw", "response",
    "payload", "headers",
})


def _num(v):
    """數值 → float；非有限值（NaN / ±inf）一律轉 None（JSON 無法表示）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _bool(v):
    return v if isinstance(v, bool) else None


def _text(v, limit=None):
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    if limit is not None and len(s) > limit:
        return s[:limit] + TRUNCATED_SUFFIX
    return s


def _get(obj, name, default=None):
    return default if obj is None else getattr(obj, name, default)


def format_wall(dt):
    """把 aware datetime 格式化成 ISO-8601（含 UTC 位移）。

    🔴 **只接受 aware datetime**。naive / 非 datetime 一律回 None ——
       不猜時區、**不 fallback UTC**、不以主機本機時間冒充契約。
       此時 `at_wall` 為 null，而同一筆紀錄的 `tou_reason` 會說明原因
       （時區不可解析時，TariffProvider 本來就會 Fail Closed 成 UNKNOWN）。
    🔴 `at_wall` 僅供人閱讀的鑑識用途；**所有** elapsed / freshness / TTL
       計算一律只用 `at_monotonic`。
    """
    if not isinstance(dt, datetime.datetime):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.isoformat(timespec="seconds")


def new_run_id():
    """本次 process 的執行識別（與 boot_id 不同：同一次開機可有多次 run）。"""
    return uuid.uuid4().hex[:16]


# ======================================================================
# Capture —— 保存該 cycle 已經算好的東西（不重跑、不多讀）
# ======================================================================
class ObservationCapture(object):
    """包住既有 `DecisionObserver`，**原樣回傳**，只額外留下參考。

    🔴 不改變任何判定結果、不新增任何讀取。Arbiter 一輪會 observe 兩次
       （original / fresh），因此同時保留 `previous` 與 `latest`；
       AuditRecord 一律採用 **latest（= fresh）**，不會再跑第三次 observe。
    🔴 未知屬性一律轉給被包住的 observer，讓既有呼叫端無感。
    """

    def __init__(self, observer):
        # 用 object.__setattr__ 繞過自訂 __getattr__ 的初始化順序問題
        object.__setattr__(self, "_observer", observer)
        object.__setattr__(self, "previous", None)
        object.__setattr__(self, "latest", None)
        object.__setattr__(self, "observe_count", 0)

    @property
    def observer(self):
        return self._observer

    def observe(self):
        res = self._observer.observe()
        object.__setattr__(self, "previous", self.latest)
        object.__setattr__(self, "latest", res)
        object.__setattr__(self, "observe_count", self.observe_count + 1)
        return res

    def reset_cycle(self):
        """進入新的一輪前清空 —— 避免把上一輪的觀測誤當成本輪證據。"""
        object.__setattr__(self, "previous", None)
        object.__setattr__(self, "latest", None)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_observer"), name)


class ResultCapture(object):
    """包住 `ProductionExecutionChain.run`，**原樣回傳**，只留下最後一次結果。

    🔴 `__self__` 刻意向下委派 —— 既有呼叫端習慣以 `obs_source.__self__`
       取回 chain 物件（bound method 的語意），包起來之後仍須成立。
    """

    def __init__(self, source):
        self._source = source
        self.latest = None
        self.call_count = 0

    @property
    def __self__(self):
        return getattr(self._source, "__self__", self._source)

    def __call__(self, *a, **kw):
        res = self._source(*a, **kw)
        self.latest = res
        self.call_count += 1
        return res


class CycleEvidence(object):
    """把一輪已經算好的證據兜在一起供 audit 使用。

    只從 `ResultCapture` 沿既有結構取回 `ObservationCapture`
    （`capture.__self__.arbiter.observer`），**不新增任何讀取**，
    也不要求呼叫端多傳一份參考。取不到就是 None，audit 欄位跟著 null。
    """

    def __init__(self, result_capture=None):
        self.result_capture = result_capture

    @property
    def observation_capture(self):
        chain = getattr(self.result_capture, "__self__", None)
        arbiter = getattr(chain, "arbiter", None)
        obs = getattr(arbiter, "observer", None)
        return obs if isinstance(obs, ObservationCapture) else None

    @property
    def local_now(self):
        """正式的 aware 本地時間來源 —— 沿用該鏈已經在用的那一個。

        🔴 不新造第二套 timezone helper：直接取 DecisionObserver 的
           `_local_now`（production 由 `PRD.build_local_now()` 注入，
           為 Asia/Taipei aware）。取不到就回 None，由呼叫端決定。
        """
        cap = self.observation_capture
        return getattr(cap, "_local_now", None) if cap is not None else None

    @property
    def execution_result(self):
        return getattr(self.result_capture, "latest", None)

    @property
    def observation(self):
        """AuditRecord 一律採用 latest（= arbiter 的 fresh observation）。"""
        return getattr(self.observation_capture, "latest", None)

    def begin_cycle(self):
        """進入新的一輪：清掉上一輪的觀測，避免誤用成本輪證據。"""
        cap = self.observation_capture
        if cap is not None:
            cap.reset_cycle()
        if self.result_capture is not None:
            self.result_capture.latest = None


# ======================================================================
# AuditRecord
# ======================================================================
def build_record(cycle_result=None, execution_result=None, observation=None,
                 seq=None, run_id=None, boot_id=None, at_wall=None,
                 no_action_reason=None, sink_healthy=None):
    """把該 cycle 已經算好的事實攤平成一筆紀錄。**純函式、零 I/O。**

    🔴 只取明確欄位 —— 不 dump `__dict__` / `repr()` / 整包 asdict /
       原始 HTTP response / `alarm_rows`。
    🔴 缺什麼就是 null，由對應的 *_reason 說明；不猜、不補預設值。
    """
    arb = _get(execution_result, "arbitration")
    obs = observation

    rec = {
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "run_id": run_id,
        "boot_id": boot_id,
        "cycle": _get(cycle_result, "cycle"),

        "at_wall": at_wall,
        "at_monotonic": _num(_get(cycle_result, "at")),

        "runtime_state": _text(_get(cycle_result, "state")),
        "runtime_reason": _text(_get(cycle_result, "reason")),
        "audit_event": _text(_get(cycle_result, "audit_event")),

        # 🔴 meter 值取自 DecisionObservation（ArbitrationResult 沒有這些欄位，
        #    而 ObserveRecord.meter_state 目前恆為 None，不可作為來源）。
        "meter_power_kw": _num(_get(obs, "grid_power_kw")),
        "meter_valid": _bool(_get(obs, "meter_valid")),
        "meter_reason": _text(_get(obs, "meter_reason")),
        "meter_age_sec": _num(_get(obs, "meter_age_at_decision_sec")),
        "grid_state": _text(_get(obs, "grid_state")),
        "grid_reason": _text(_get(obs, "grid_reason")),

        "tou_state": _text(_get(obs, "tou_state")),
        "tou_reason": _text(_get(obs, "tou_reason")),

        "soc_percent": _num(_get(obs, "soc_percent")),
        "ess_valid": _bool(_get(obs, "ess_valid")),
        "ess_reason": _text(_get(obs, "ess_reason")),
        "ess_age_sec": _num(_get(obs, "ess_age_at_decision_sec")),

        # PCS 實際狀態：優先取 cycle 的觀測結果，其次取本輪 observation。
        "pcs_state": _text(_get(cycle_result, "pcs_state")
                           or _get(obs, "pcs_state")),

        "decision_action": _text(_get(obs, "decision_action")),
        "decision_reason": _text(_get(obs, "decision_reason")),
        "decision_target_kw": _num(_get(obs, "decision_target_power_kw")),
        "would_action": _text(_get(cycle_result, "would_action")
                              or _get(obs, "would_action")),

        "authority_state": _text(_get(arb, "authority_state")),
        "authority_reason": _text(_get(arb, "authority_reason")),

        "safety_allowed": _bool(_get(arb, "safety_allowed")),
        "safety_reason": _text(_get(arb, "safety_reason")),
        "safety_check_count": _get(arb, "safety_check_count"),

        "direction_reason": _text(_get(arb, "direction_reason")),
        "control_action": _text(_get(cycle_result, "control_action")
                                or _get(execution_result, "control_action")),

        "authorized": _bool(_get(cycle_result, "authorized")),
        "authorization_id": _text(_get(cycle_result, "authorization_id")),
        "authorization_state": _text(_get(arb, "authorization_state")),

        "dispatched": _bool(_get(cycle_result, "dispatched")),
        "executed": _bool(_get(cycle_result, "executed")),
        "execution_outcome": _text(_get(cycle_result, "execution_outcome")
                                   or _get(execution_result, "outcome")),
        "lastcontrol_written": _bool(_get(cycle_result, "lastcontrol_written")),
        "phases": list(_get(cycle_result, "phases") or ()),

        "recovery_outcome": _text(_get(cycle_result, "recovery_outcome")),
        "no_action_reason": _text(no_action_reason),
        "detail": _text(_get(cycle_result, "detail"), DETAIL_MAX_CHARS),

        "audit_sink_healthy": _bool(sink_healthy),
    }
    # REQUIRED KEY 契約：每個 key 一定存在（值可為 null）
    for k in FIELDS:
        rec.setdefault(k, None)
    return rec


def validate_record(rec):
    """回傳 (ok, reason)。只驗結構契約，不驗內容是否「合理」。"""
    if not isinstance(rec, dict):
        return False, "NOT_A_DICT"
    missing = [k for k in FIELDS if k not in rec]
    if missing:
        return False, "MISSING_KEYS:" + ",".join(missing)
    extra = [k for k in rec if k not in FIELDS]
    if extra:
        return False, "UNKNOWN_KEYS:" + ",".join(sorted(extra))
    if rec.get("schema_version") != SCHEMA_VERSION:
        return False, f"UNSUPPORTED_SCHEMA:{rec.get('schema_version')!r}"
    bad = sorted(k for k in rec if k.lower() in FORBIDDEN_KEYS)
    if bad:
        return False, "FORBIDDEN_KEYS:" + ",".join(bad)
    return True, "OK"


# ======================================================================
# AuditSink
# ======================================================================
class AuditSink(object):
    """Append-only JSONL sink。

    🔴 寫入失敗 = **NON_GATING_CONTINUE**：控制服務照常運轉，
       但**絕不**假裝寫成功 —— healthy 轉 False、失敗計數累加、
       last_error 保留，並由呼叫端印出 WARNING。
       它不得改變 Runtime / Authority / Safety / dispatch 任何狀態。
    🔴 每筆 append → flush → fsync（AUDIT_FSYNC_POLICY = EVERY_CYCLE）。
    🔴 rotation / retention **未裁示** → 本版不做任何刪除或搬移。
    """

    def __init__(self, path=DEFAULT_AUDIT_PATH, run_id=None, boot_id=None,
                 clock=None, opener=None, fsync=None, makedirs=None):
        self.path = path
        self.run_id = run_id if run_id is not None else new_run_id()
        self.boot_id = boot_id
        self._clock = clock if clock is not None else datetime.datetime.now
        self._opener = opener if opener is not None else io.open
        self._fsync = fsync if fsync is not None else os.fsync
        self._makedirs = makedirs if makedirs is not None else os.makedirs

        self.healthy = True
        self.write_count = 0
        self.write_failure_count = 0
        self.last_error = None
        self.last_success_seq = None
        self.malformed_line_count = 0
        self.seq = 0
        self._recover_seq()

    # ------------------------------------------------------------------
    def _recover_seq(self):
        """由**檔尾**回復 seq —— 不整檔掃描（檔案可能有數十萬行）。"""
        try:
            if not os.path.exists(self.path):
                return
            size = os.path.getsize(self.path)
            with self._opener(self.path, "rb") as f:
                if size > TAIL_READ_BYTES:
                    f.seek(size - TAIL_READ_BYTES)
                    tail = f.read()
                else:
                    tail = f.read()
            lines = tail.decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    # crash 造成的半行 / 截斷：跳過，不讓整份 audit 失效
                    self.malformed_line_count += 1
                    continue
                s = rec.get("seq")
                if isinstance(s, int) and not isinstance(s, bool):
                    self.seq = s
                    return
        except OSError as e:                              # noqa: BLE001
            # 連讀都讀不到 → 不阻擋服務；從 0 開始，並如實標記不健康
            self.healthy = False
            self.last_error = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    def append(self, record):
        """寫一筆。回傳 (outcome, seq_or_None)。**永遠不拋例外。**"""
        self.seq += 1
        record = dict(record)
        record["seq"] = self.seq
        record.setdefault("run_id", self.run_id)
        record.setdefault("boot_id", self.boot_id)
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            d = os.path.dirname(self.path)
            if d:
                self._makedirs(d, exist_ok=True)
            with self._opener(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                self._fsync(f.fileno())
        except Exception as e:                            # noqa: BLE001
            # 🔴 NON_GATING_CONTINUE：不阻擋控制，但也絕不假裝成功
            self.write_failure_count += 1
            self.healthy = False
            self.last_error = f"{type(e).__name__}: {e}"
            return W_FAILED, None
        self.write_count += 1
        self.last_success_seq = self.seq
        # 失敗計數**不清零** —— 只恢復 healthy，讓歷史故障仍可見
        self.healthy = True
        return W_OK, self.seq

    # ------------------------------------------------------------------
    def status(self):
        return {"path": self.path, "run_id": self.run_id,
                "boot_id": self.boot_id, "healthy": self.healthy,
                "seq": self.seq, "write_count": self.write_count,
                "write_failure_count": self.write_failure_count,
                "last_success_seq": self.last_success_seq,
                "last_error": self.last_error,
                "malformed_line_count": self.malformed_line_count,
                "fsync_policy": AUDIT_FSYNC_POLICY,
                "failure_policy": AUDIT_WRITE_FAILURE_POLICY,
                "rotation_policy": ROTATION_POLICY,
                "retention_policy": RETENTION_POLICY}

    def __str__(self):
        return (f"[AUDIT] healthy={self.healthy} seq={self.seq} "
                f"ok={self.write_count} failed={self.write_failure_count}"
                + (f" last_error={self.last_error}" if self.last_error else ""))


# ======================================================================
# 讀取 / 檢查（供事後分析與測試；**不得**用於控制狀態恢復）
# ======================================================================
def read_records(path):
    """回傳 (records, problems)。半行 / 壞行一律跳過並如實回報。"""
    recs, problems = [], []
    if not os.path.exists(path):
        return recs, problems
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                problems.append(("MALFORMED_LINE", i))
    return recs, problems


def sequence_report(records):
    """回報 seq 的 gap / duplicate。**只回報，不修補** —— 那是事實。"""
    seqs = [r.get("seq") for r in records
            if isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool)]
    dup = sorted({s for s in seqs if seqs.count(s) > 1})
    gaps = []
    for a, b in zip(seqs, seqs[1:]):
        if b != a + 1:
            gaps.append((a, b))
    return {"count": len(records), "with_seq": len(seqs),
            "first": seqs[0] if seqs else None,
            "last": seqs[-1] if seqs else None,
            "gaps": gaps, "duplicates": dup}


# ======================================================================
# CLI（完全離線；寫到暫存目錄，不碰 production 路徑）
# ======================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase 6 Durable Decision Audit（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Phase 6 Durable Decision Audit ==\n")
    print(f"  schema_version   : {SCHEMA_VERSION}")
    print(f"  granularity      : {AUDIT_GRANULARITY}")
    print(f"  format           : {AUDIT_FORMAT}")
    print(f"  fsync policy     : {AUDIT_FSYNC_POLICY}")
    print(f"  failure policy   : {AUDIT_WRITE_FAILURE_POLICY}")
    print(f"  rotation policy  : {ROTATION_POLICY}  ← 尚未裁示")
    print(f"  retention policy : {RETENTION_POLICY}  ← 尚未裁示")
    print(f"  預設路徑         : {DEFAULT_AUDIT_PATH}")
    print(f"  欄位數           : {len(FIELDS)}")
    print("\n  ⚠ Durable Audit 是 evidence，**不是**控制狀態恢復來源。")
    print("  ⚠ rotation / retention 未裁示前，請自行留意磁碟使用量。")
    rec = build_record(seq=1, run_id="demo", boot_id="demo")
    ok, why = validate_record(rec)
    print(f"\n  空白紀錄結構檢查 : {ok} / {why}")
    print(f"  單筆位元組       : "
          f"{len(json.dumps(rec, ensure_ascii=False, default=str))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
