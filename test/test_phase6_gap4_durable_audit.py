# -*- coding: utf-8 -*-
"""
test_phase6_gap4_durable_audit.py — GAP-4 Durable Decision Audit
======================================================================
核心命題
    「process 重啟之後，仍然必須查得到『這一輪為什麼 CHARGE / DISCHARGE /
      沒動作 / 被誰擋下』。而稽核本身，絕不能反過來影響控制。」

🔴 完全 OFFLINE / ZERO-I/O（對設備而言）：沿用既有 harness，
   **不 sleep、不連任何設備、不執行 CLI**。
🔴 所有寫入一律指向 **暫存目錄**，絕不碰 production 的 output/phase6_audit。
🔴 Audit **不得增加任何 device I/O**，也**不得**成為控制狀態恢復來源。

P   已裁示的 policy 常數
W   at_wall = Asia/Taipei aware ISO-8601；時間差一律只用 monotonic
A   schema 契約與敏感資訊
B   seq / restart / 每行獨立可讀
C   partial line / malformed / gap / duplicate / truncate
D   directory missing / write failure（NON_GATING_CONTINUE）
E   audit 不是恢復來源；RAM ring 並存
F   八個問題皆可回答（value 或 explicit reason）
I   with / without audit 的 observation call count 必須完全相同
L   長跑：300 cycles、seq 連續、實耗時 ≈ 0
G   scope：只做 GAP-4；GAP-1/2/3 不回歸

用法
    python test_phase6_gap4_durable_audit.py        # exit 0 = PASS
"""
import io
import os
import sys
import ast
import json
import time
import shutil
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tariff_provider as TP                       # noqa: E402
import annual_off_peak_calendar as AC              # noqa: E402
import last_control_store as LCS                   # noqa: E402
import pcs_control_integration as PCI              # noqa: E402
import pcs_auto_control_config as CFG              # noqa: E402
import pcs_auto_control_runtime as RT              # noqa: E402
import pcs_auto_control_production as PRD          # noqa: E402
import pcs_auto_control_service as SVC             # noqa: E402
import phase6_decision_audit as AUD                # noqa: E402
import phase6_decision_replay_runner as RUN        # noqa: E402
import test_phase6_runtime_arbiter_longrun as LR   # noqa: E402
import test_phase6_gap3_tariff_wiring as G3        # noqa: E402

RESULTS = []
TMPDIRS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def scratch(name="audit.jsonl"):
    """一律寫到暫存目錄 —— 絕不碰 production 的 output/phase6_audit。"""
    d = tempfile.mkdtemp(prefix="phase6_gap4_")
    TMPDIRS.append(d)
    return os.path.join(d, name)


class Quiet(object):
    """收集 service log，避免長跑洗版，同時能斷言 WARNING 有印出來。"""

    def __enter__(self):
        self.lines = []
        self._real = SVC.log
        SVC.log = self.lines.append
        return self

    def __exit__(self, *a):
        SVC.log = self._real
        return False


class AuditRig(object):
    """正式 production wiring + 假設備 + 暫存 audit sink。"""

    def __init__(self, path=None, sink=None, clock=None):
        self.clock = clock if clock is not None else LR.VirtualClock()
        self.plant = LR.ScriptedPlant(self.clock)
        self._at = {"now": None}
        self.sink = sink if sink is not None else AUD.AuditSink(
            path=path or scratch(), run_id="test-run", boot_id="test-boot")
        self.obs_source, self.recovery, self.wiring = \
            SVC.build_production_stack(
                reader=self.plant.ess_reader,
                meter_source=self.plant.meter_source,
                clock=self.clock, local_now=lambda: self._at["now"],
                audit_sink=self.sink)
        self.evidence = AUD.CycleEvidence(self.obs_source)
        self.chain = self.obs_source.__self__
        self.arbiter = self.chain.arbiter
        self.observer = self.arbiter.observer
        self.runtime = RT.AutoControlRuntime(
            observation_source=self.obs_source,
            recovery_source=self.recovery, clock=self.clock)

    def run(self, timeline, step_sec=30.0, audit=True, hook=None):
        out = []
        with Quiet() as q:
            self.log_lines = q.lines
            for i, p in enumerate(timeline.points):
                self.plant.point = p
                self._at["now"] = p.at
                if hook is not None:
                    hook(i, p, self)
                self.evidence.begin_cycle()
                res = self.runtime.tick(RT.CycleInputs(is_owner=True))
                SVC.write_audit(self.sink if audit else None, res,
                                self.evidence, config=self.runtime.config)
                out.append(res)
                self.clock.advance(step_sec)
        return out

    def records(self):
        return AUD.read_records(self.sink.path)[0]


def _code_idents(path):
    """模組**程式碼**中的識別字與字串常數（排除 module docstring）。"""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    doc = ast.get_docstring(tree)
    out = set()
    for node in tree.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and node.value.value == doc):
            continue
        for x in ast.walk(node):
            if isinstance(x, ast.Name):
                out.add(x.id)
            elif isinstance(x, ast.Attribute):
                out.add(x.attr)
            elif isinstance(x, ast.arg):
                out.add(x.arg)
            elif isinstance(x, (ast.FunctionDef, ast.ClassDef)):
                out.add(x.name)
            elif isinstance(x, ast.keyword) and x.arg:
                out.add(x.arg)
            elif isinstance(x, ast.Constant) and isinstance(x.value, str):
                if x.value.isidentifier():
                    out.add(x.value)
    return out


def tl(start=None, n=5, **kw):
    kw.setdefault("meter_kw", 60.0)
    kw.setdefault("soc_percent", 50.0)
    return RUN.build_timeline(start or G3.aware(2026, 7, 15, 3), 30, n,
                              name="gap4", **kw)


# ======================================================================
# P —— 已裁示的 policy
# ======================================================================
def test_P_policies():
    print("\n[P] 已裁示的 policy 常數")
    check(f"★★ P1. granularity = {AUD.AUDIT_GRANULARITY}",
          AUD.AUDIT_GRANULARITY == "ONE_CYCLE_ONE_RECORD")
    check(f"★★ P2. format = {AUD.AUDIT_FORMAT}",
          AUD.AUDIT_FORMAT == "APPEND_ONLY_JSONL")
    check(f"★★ P3. fsync = {AUD.AUDIT_FSYNC_POLICY}",
          AUD.AUDIT_FSYNC_POLICY == "EVERY_CYCLE")
    check(f"★★ P4. write failure = {AUD.AUDIT_WRITE_FAILURE_POLICY}",
          AUD.AUDIT_WRITE_FAILURE_POLICY == "NON_GATING_CONTINUE")
    check(f"★★ P5. rotation = {AUD.ROTATION_POLICY}（未裁示，不得自行填值）",
          AUD.ROTATION_POLICY == "DEFERRED")
    check(f"★★ P6. retention = {AUD.RETENTION_POLICY}（未裁示）",
          AUD.RETENTION_POLICY == "DEFERRED")
    check("★★ P7. 未自行實作 rotation / retention（無 MAX_BYTES / BACKUP / DAYS）",
          not any(n in dir(AUD) for n in
                  ("LOG_MAX_BYTES", "MAX_BYTES", "BACKUP_COUNT",
                   "RETENTION_DAYS", "rotate", "_rotate")))
    check(f"  P8. 預設路徑 = output/phase6_audit/decision_audit.jsonl",
          AUD.DEFAULT_AUDIT_PATH.replace("\\", "/").endswith(
              "output/phase6_audit/decision_audit.jsonl"))
    check("★★ P9. audit 模組零專案相依（無控制能力）",
          not ({n.names[0].name for n in ast.walk(
              ast.parse(io.open(AUD.__file__, encoding="utf-8").read()))
              if isinstance(n, ast.Import)}
              & {"pcs_auto_control_runtime", "production_arbiter",
                 "pcs_control_executor", "device_control_operator",
                 "api_client", "requests", "production_execution_chain"}))


# ======================================================================
# W —— at_wall 時間契約（GAP-A1）
# ======================================================================
def test_W_wall_clock():
    print("\n[W] at_wall = Asia/Taipei aware ISO-8601（僅供鑑識；不用於任何判定）")
    import datetime as _dt
    tz = TP.resolve_timezone(TP.PRODUCTION_TIMEZONE_NAME)[0]

    r = AuditRig()
    r.run(tl(n=3))
    rec = r.records()[-1]
    w = rec["at_wall"]
    check(f"★★ W-A1. at_wall 含 UTC 位移：{w}",
          isinstance(w, str) and ("+" in w[10:] or w.endswith("Z")))

    parsed = _dt.datetime.fromisoformat(w)
    off = parsed.utcoffset()
    check(f"★★ W-A2. tzinfo 語意成立：utcoffset={off}"
          f"（= Asia/Taipei 的 {_dt.datetime.now(tz).utcoffset()}）",
          parsed.tzinfo is not None and off is not None
          and off == _dt.datetime.now(tz).utcoffset())

    # A3：注入固定 local_now → deterministic timestamp
    fixed = G3.aware(2026, 7, 15, 3, 45)
    r3 = AuditRig()
    r3.plant.point = tl(n=1).points[0]
    r3._at["now"] = fixed
    with Quiet():
        res = r3.runtime.tick(RT.CycleInputs(is_owner=True))
        SVC.write_audit(r3.sink, res, r3.evidence, config=r3.runtime.config)
        SVC.write_audit(r3.sink, res, r3.evidence, config=r3.runtime.config)
    got = [x["at_wall"] for x in r3.records()]
    check(f"★★ W-A3. 注入 local_now → deterministic：{got[0]}",
          got == [fixed.isoformat(timespec="seconds")] * 2
          and got[0] == "2026-07-15T03:45:00+08:00")

    # naive / 非法時間源一律 null —— 不猜時區、不 fallback UTC
    check("★★ W-A4a. naive datetime → at_wall = null（不猜、不 fallback UTC）",
          AUD.format_wall(_dt.datetime(2026, 7, 15, 3, 45)) is None
          and AUD.format_wall("2026-07-15") is None
          and AUD.format_wall(None) is None)
    src = io.open(AUD.__file__, encoding="utf-8").read()
    svc_audit = io.open(SVC.__file__, encoding="utf-8").read().split(
        "def write_audit(")[1].split("\ndef ")[0]
    # 🔴 掃**程式碼識別字**，不掃 docstring ——
    #    說明文字本來就會寫「不 fallback UTC」「TTL 只用 monotonic」。
    audit_idents = _code_idents(AUD.__file__)
    # 🔴 用 AST 掎 write_audit 本體：註解裡寫的
    #    「不使用 time.strftime()/datetime.now()」不得算命中。
    svc_tree = ast.parse(io.open(SVC.__file__, encoding="utf-8").read())
    wa = [n for n in ast.walk(svc_tree)
          if isinstance(n, ast.FunctionDef) and n.name == "write_audit"][0]
    wa_calls = set()
    for x in ast.walk(wa):
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute):
            base = getattr(x.func.value, "id", None)
            wa_calls.add(f"{base}.{x.func.attr}" if base else x.func.attr)
    naive = sorted(wa_calls & {"time.strftime", "strftime", "datetime.now",
                               "utcnow", "datetime.utcnow", "time.time",
                               "time.localtime", "gmtime"})
    check(f"★★ W-A4b. write_audit 未呼叫 naive 時間來源：命中={naive}",
          not naive and "AUD.format_wall" in wa_calls
          and not (audit_idents & {"utcnow", "utc", "gmtime",
                                   "utcfromtimestamp"}))
    check("★★ W-A4c. 未新造第二套 timezone helper（沿用既有 local_now）",
          not (audit_idents & {"resolve_timezone", "ZoneInfo", "timezone",
                               "pytz", "tzinfo_for"})
          and "local_now" in svc_audit)

    # A4：audit 時間戳不參與任何判定 —— 所有時間差只用 monotonic
    check(f"★★ W-A4d. at_monotonic 取自 CycleResult.at（monotonic），"
          f"與 at_wall 完全分離（{rec['at_monotonic']}）",
          isinstance(rec["at_monotonic"], float)
          and rec["at_monotonic"] != 0.0)
    hits = sorted(i for i in audit_idents
                  if any(t in i.lower()
                         for t in ("ttl", "stale", "fresh", "age_limit")))
    check(f"★★ W-A4e. audit 程式碼不含任何 TTL / freshness / stale "
          f"計算：命中={hits}", not hits)

    # 同一條 timeline：有 / 無 audit 的 CycleResult 必須完全相同
    a = AuditRig()
    ra = a.run(tl(n=8), audit=False)
    b = AuditRig()
    rb = b.run(tl(n=8), audit=True)
    check("★★ W-A4f. 有 / 無 audit 的每一輪判定完全相同"
          "（TTL / freshness / authority / decision / safety 均不受影響）",
          [x.as_dict() for x in ra] == [x.as_dict() for x in rb])
    check("★★ W-A4g. write_audit 在 tick() **之後**才執行，結構上不可能影響判定",
          svc_audit.index("sink.append") > 0
          and "rt.tick" not in svc_audit)


# ======================================================================
# A —— schema 契約與敏感資訊
# ======================================================================
def test_A_schema():
    print("\n[A] schema 契約與敏感資訊")
    r = AuditRig()
    r.run(tl(n=4))
    recs = r.records()
    check(f"  A0. 寫出 {len(recs)} 筆", len(recs) == 4)

    rec = recs[-1]
    ok, why = AUD.validate_record(rec)
    check(f"★★ A1. 結構合法（{why}），共 {len(rec)} 欄", ok
          and len(rec) == len(AUD.FIELDS))
    check("★★ A2. REQUIRED KEY 契約：每筆都含全部 key（值可為 null）",
          all(set(x.keys()) == set(AUD.FIELDS) for x in recs))
    check(f"★★ A3. schema_version = {rec['schema_version']}，"
          f"未知版本必須被拒絕",
          rec["schema_version"] == AUD.SCHEMA_VERSION
          and AUD.validate_record(dict(rec, schema_version=99))[0] is False)

    blob = json.dumps(recs, ensure_ascii=False).lower()
    bad = [t for t in ("password", "passwd", "bearer", "token", "cookie",
                       "secret", "private key", "privatekey", "sm2",
                       "authorization:") if t in blob]
    check(f"★★ A4. 無敏感字串：命中={bad}", not bad)
    check("★★ A5. 未寫入 alarm_rows / 原始 response / payload",
          not (set(rec.keys()) & AUD.FORBIDDEN_KEYS)
          and "alarm_rows" not in blob and "\"raw\"" not in blob)
    check("★★ A6. 無 object repr（不含 '<... object at 0x'）",
          "object at 0x" not in blob and " instance at " not in blob)
    types_ok = all(
        v is None or isinstance(v, (str, int, float, bool, list))
        for x in recs for v in x.values())
    check("★★ A7. 全部欄位為純量或字串清單（未巢狀 dump）", types_ok)
    check("★★ A8. detail 有長度上限（避免單行無限膨脹）",
          AUD.DETAIL_MAX_CHARS > 0
          and len(AUD._text("x" * 5000, AUD.DETAIL_MAX_CHARS))
          == AUD.DETAIL_MAX_CHARS + len(AUD.TRUNCATED_SUFFIX))
    size = len(json.dumps(rec, ensure_ascii=False))
    check(f"  A9. 單筆約 {size} bytes（一 cycle 一筆）", 200 < size < 4000)


# ======================================================================
# B —— seq / restart / 每行獨立
# ======================================================================
def test_B_sequence_and_restart():
    print("\n[B] seq / restart / 每行獨立可讀")
    path = scratch()
    r1 = AuditRig(path=path)
    r1.run(tl(n=6))
    rep = AUD.sequence_report(r1.records())
    check(f"★★ B1. 6 cycles → {rep['count']} 行，seq {rep['first']}~{rep['last']}"
          f"，gap={rep['gaps']}",
          rep["count"] == 6 and rep["first"] == 1 and rep["last"] == 6
          and not rep["gaps"] and not rep["duplicates"])
    check("★★ B2. 每一行皆可獨立 json.loads",
          all(json.loads(l) for l in
              io.open(path, encoding="utf-8").read().splitlines() if l.strip()))

    # ---- 模擬 process restart：新的 sink 指向同一個檔 ----
    sink2 = AUD.AuditSink(path=path, run_id="second-run", boot_id="test-boot")
    check(f"★★ B3. restart：由檔尾回復 seq = {sink2.seq}（不從 1 重來）",
          sink2.seq == 6)
    r2 = AuditRig(path=path, sink=sink2)
    r2.run(tl(n=3))
    rep2 = AUD.sequence_report(r2.records())
    check(f"★★ B4. restart 後續接：共 {rep2['count']} 行，seq 到 {rep2['last']}，"
          f"仍無 gap", rep2["count"] == 9 and rep2["last"] == 9
          and not rep2["gaps"])
    runs = {x["run_id"] for x in r2.records()}
    check(f"★★ B5. run_id 可區分兩次執行：{sorted(runs)}",
          runs == {"test-run", "second-run"})
    cycles = [x["cycle"] for x in r2.records()]
    check(f"★★ B6. cycle 在 restart 後歸零，但 seq 不受影響"
          f"（cycle 尾段={cycles[-3:]}）",
          cycles[:6] == [1, 2, 3, 4, 5, 6] and cycles[6:] == [1, 2, 3])
    check("★★ B7. boot_id 存在（跨 boot 不可比 monotonic）",
          all(x["boot_id"] == "test-boot" for x in r2.records()))
    check("  B8. 大檔案只讀檔尾回復 seq（不整檔掃描）",
          AUD.TAIL_READ_BYTES > 0
          and "TAIL_READ_BYTES" in io.open(AUD.__file__,
                                           encoding="utf-8").read())


# ======================================================================
# C —— 壞檔容忍
# ======================================================================
def test_C_corruption():
    print("\n[C] partial line / malformed / gap / duplicate / truncate")
    path = scratch()
    r = AuditRig(path=path)
    r.run(tl(n=5))

    # C1 crash 造成的半行
    with io.open(path, "a", encoding="utf-8") as f:
        f.write('{"seq": 6, "schema_ver')          # 沒有結尾與換行
    recs, probs = AUD.read_records(path)
    check(f"★★ C1. partial final line → 跳過（{len(recs)} 筆完好，"
          f"problems={len(probs)}）", len(recs) == 5 and len(probs) == 1)
    sink2 = AUD.AuditSink(path=path, run_id="after-crash", boot_id="b")
    check(f"★★ C2. 半行不阻礙 seq 回復（seq={sink2.seq}）", sink2.seq == 5)

    # C3 中間壞行
    path2 = scratch()
    r2 = AuditRig(path=path2)
    r2.run(tl(n=4))
    lines = io.open(path2, encoding="utf-8").read().splitlines()
    lines.insert(2, "{ this is not json }")
    io.open(path2, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    recs2, probs2 = AUD.read_records(path2)
    check(f"★★ C3. 中間 malformed 行 → 跳過並回報（{len(recs2)} 筆完好，"
          f"problems={probs2}）", len(recs2) == 4 and len(probs2) == 1)

    # C4 seq gap / duplicate 只回報不修補
    fake = [{"seq": 1}, {"seq": 2}, {"seq": 5}, {"seq": 5}, {"seq": 6}]
    rep = AUD.sequence_report(fake)
    check(f"★★ C4. gap={rep['gaps']} duplicates={rep['duplicates']}"
          f" —— 只回報，不修補",
          rep["gaps"] == [(2, 5), (5, 5)] and rep["duplicates"] == [5])

    # C5 檔案被截斷 → 新 sink 由剩餘內容回復，不回填
    path3 = scratch()
    r3 = AuditRig(path=path3)
    r3.run(tl(n=6))
    keep = io.open(path3, encoding="utf-8").read().splitlines()[:2]
    io.open(path3, "w", encoding="utf-8").write("\n".join(keep) + "\n")
    sink3 = AUD.AuditSink(path=path3, run_id="t", boot_id="b")
    check(f"★★ C5. 檔案被截斷 → 由現存內容回復（seq={sink3.seq}），"
          f"**不回填**、不覆寫", sink3.seq == 2
          and len(AUD.read_records(path3)[0]) == 2)


# ======================================================================
# D —— 目錄缺失 / 寫入失敗（NON_GATING_CONTINUE）
# ======================================================================
def test_D_write_failure():
    print("\n[D] directory missing / write failure → NON_GATING_CONTINUE")
    deep = os.path.join(os.path.dirname(scratch()), "a", "b", "audit.jsonl")
    sink = AUD.AuditSink(path=deep, run_id="t", boot_id="b")
    out, seq = sink.append(AUD.build_record())
    check(f"★★ D1. 目錄不存在 → 自動建立並寫入（{out} seq={seq}）",
          out == AUD.W_OK and os.path.exists(deep))

    # ---- 注入會拋 OSError 的 opener（模擬 permission denied / disk full）----
    class Boom(object):
        def __init__(self, err):
            self.err = err
            self.armed = False

        def __call__(self, *a, **kw):
            if self.armed:
                raise self.err
            return io.open(*a, **kw)

    for err in (PermissionError(13, "permission denied"),
                OSError(28, "no space left on device")):
        boom = Boom(err)
        s = AUD.AuditSink(path=scratch(), run_id="t", boot_id="b", opener=boom)
        boom.armed = True
        out, seq = s.append(AUD.build_record())
        check(f"★★ D2. {type(err).__name__} → {out}，healthy={s.healthy}，"
              f"failures={s.write_failure_count}",
              out == AUD.W_FAILED and seq is None and s.healthy is False
              and s.write_failure_count == 1
              and err.__class__.__name__ in (s.last_error or ""))
        boom.armed = False
        out2, seq2 = s.append(AUD.build_record())
        check("★★ D3. 下次成功 → healthy 恢復，但失敗計數**不清零**",
              out2 == AUD.W_OK and s.healthy is True
              and s.write_failure_count == 1 and s.last_success_seq == seq2)

    # ---- 寫入失敗不得影響控制 ----
    boom = Boom(OSError(28, "no space left on device"))
    sink = AUD.AuditSink(path=scratch(), run_id="t", boot_id="b", opener=boom)
    r = AuditRig(sink=sink)

    def hook(i, p, rig):
        boom.armed = (2 <= i < 5)

    recs = r.run(tl(n=8), hook=hook)
    warn = [l for l in r.log_lines if "WARNING" in l and "audit" in l]
    check(f"★★ D4. 失敗期間仍印出明確 WARNING（{len(warn)} 則）", len(warn) == 3)
    check(f"★★ D5. 控制不受影響：cycle 全數照跑（{len(recs)} 輪）"
          f"，狀態不因 audit 改變",
          len(recs) == 8
          and all(x.state in (RT.ST_OBSERVE_ONLY, RT.ST_COMMAND_PENDING)
                  for x in recs))
    check("★★ D6. audit 失敗**不得**改變 dispatch / executed / authority",
          all(x.dispatched is False and x.executed is False for x in recs)
          and r.runtime.dispatch_count == 0 and r.chain.dispatch_count == 0)
    check(f"★★ D7. 不假裝成功：實際寫出 {len(r.records())} 筆 "
          f"= 8 − 3 失敗", len(r.records()) == 5
          and sink.write_count == 5 and sink.write_failure_count == 3)
    check("★★ D8. 失敗那幾筆留下 seq 空洞（事實保留，不修補）",
          AUD.sequence_report(r.records())["gaps"] != [])


# ======================================================================
# E —— audit 不是恢復來源；RAM ring 並存
# ======================================================================
def test_E_not_recovery_source():
    print("\n[E] audit 是 evidence，不是控制狀態恢復來源")
    path = scratch()
    a = AuditRig(path=path)
    ra = a.run(tl(n=8))
    check("  E0. 前一個 process 已進入 COMMAND_PENDING 並寫入 audit",
          ra[-1].state == RT.ST_COMMAND_PENDING
          and a.records()[-1]["runtime_state"] == RT.ST_COMMAND_PENDING
          and a.records()[-1]["authorized"] is True)

    b = AuditRig(path=path, sink=AUD.AuditSink(path=path, run_id="r2",
                                               boot_id="test-boot"))
    check("★★ E1. restart：runtime 仍從 DISABLED 開始（不讀 audit）",
          b.runtime.state == RT.ST_DISABLED and b.runtime.cycles == 0)
    check("★★ E2. restart：in-memory 授權票不跨 process",
          b.arbiter.pending is None)
    rb = b.run(tl(n=3))
    check("★★ E3. restart：classifier 仍 cold start（audit 有 charge 也不沿用）",
          rb[0].would_action is None)
    check("★★ E4. audit 模組未提供任何 recovery / restore 介面",
          not any(n in dir(AUD) for n in
                  ("recover", "restore", "last_state", "resume",
                   "restore_pending", "recover_state")))
    # 🔴 掃**程式碼識別字與字串常數**，不掃 docstring ——
    #    模組說明本來就會提到 AutoControlRuntime，純文字比對會假陽性。
    tree = ast.parse(io.open(AUD.__file__, encoding="utf-8").read())
    doc = ast.get_docstring(tree)
    body = [n for n in tree.body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant)
                    and n.value.value == doc)]
    lits = set()
    for n in body:
        for x in ast.walk(n):
            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                lits.add(x.value)
            elif isinstance(x, ast.Name):
                lits.add(x.id)
            elif isinstance(x, ast.Attribute):
                lits.add(x.attr)
    hits = sorted(lits & (set(RT.RUNTIME_STATES) | {"AutoControlRuntime"}))
    check(f"★★ E5. audit 程式碼不含 runtime 狀態機符號：命中={hits}", not hits)
    check(f"★★ E6. RAM ring 仍存在且上限不變（AUDIT_LIMIT={RT.AUDIT_LIMIT}）",
          RT.AUDIT_LIMIT == 200 and len(b.runtime.audit) == 3)
    check("★★ E7. 兩層並存：durable 筆數 ≠ ring 筆數（互不取代）",
          len(b.records()) == 11 and len(b.runtime.audit) == 3)


# ======================================================================
# F —— 八個問題皆可回答
# ======================================================================
def _answered(rec, value_key, reason_key):
    """契約：有值 **或** 有明確 reason —— 不要求每個欄位都非 None。"""
    return rec.get(value_key) is not None or bool(rec.get(reason_key))


def test_F_questions():
    print("\n[F] 八個問題皆可回答（value 或 explicit reason）")
    kw = CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw

    # Q1 為什麼 CHARGE（OFF_PEAK + IMPORT + SOC MID）
    r = AuditRig()
    r.run(tl(G3.aware(2026, 7, 15, 3), n=5))
    c = r.records()[-1]
    check(f"★★ F1. 為什麼 CHARGE：tou={c['tou_state']} grid={c['grid_state']} "
          f"meter={c['meter_power_kw']}kW soc={c['soc_percent']}% "
          f"→ {c['decision_action']}/{c['decision_reason']}",
          c["decision_action"] == "charge" and c["tou_state"] == "OFF_PEAK"
          and c["grid_state"] == "IMPORT" and c["meter_power_kw"] == 60.0
          and c["soc_percent"] == 50.0)

    # Q2 為什麼 DISCHARGE（PEAK）
    r2 = AuditRig()
    r2.run(tl(G3.aware(2026, 7, 15, 14), n=5))
    d = r2.records()[-1]
    check(f"★★ F2. 為什麼 DISCHARGE：tou={d['tou_state']} "
          f"→ {d['decision_action']}",
          d["decision_action"] == "discharge" and d["tou_state"] == "PEAK")

    # Q3 為什麼沒有動作（meter 斷線）
    r3 = AuditRig()
    r3.run(tl(G3.aware(2026, 7, 15, 3), n=6,
              meter_kw=lambda i, a: None if i >= 3 else 60.0))
    n = r3.records()[-1]
    check(f"★★ F3. 為什麼沒動作：meter_valid={n['meter_valid']} "
          f"reason={n['meter_reason']} grid={n['grid_state']} "
          f"→ would={n['would_action']}",
          n["would_action"] is None and n["meter_valid"] is False
          and bool(n["meter_reason"]))
    check("★★ F3b. meter 無值時 meter_power_kw = null 並附 reason（合法 null）",
          n["meter_power_kw"] is None
          and _answered(n, "meter_power_kw", "meter_reason"))

    # Q4 為什麼被 Authority 擋（pcs_mode_state 讀不到）
    r4 = AuditRig()
    r4.run(tl(G3.aware(2026, 7, 15, 14), n=6, pcs_charging=True,
              pcs_standby=False))
    a = r4.records()[-1]
    check(f"★★ F4. 為什麼被 Authority 擋：{a['authority_state']}"
          f"/{a['authority_reason']} → control={a['control_action']}",
          a["authority_state"] is not None
          and bool(a["authority_reason"])
          and a["authorized"] is False)

    # Q5 為什麼被 Safety Gate 擋（PCS fault）
    r5 = AuditRig()
    r5.run(tl(G3.aware(2026, 7, 15, 3), n=6,
              pcs_fault=lambda i, a2: True if i >= 3 else None))
    s = r5.records()[-1]
    check(f"★★ F5. 為什麼被 Safety 擋：allowed={s['safety_allowed']} "
          f"reason={s['safety_reason']} checks={s['safety_check_count']}",
          s["safety_allowed"] is False and bool(s["safety_reason"]))

    # Q6 為什麼要求 STOP 才能反轉
    clock = LR.VirtualClock()
    rec = RUN.scripted_owned_record(LCS.ACT_CHARGE, kw, clock.t - 30.0)
    sink = AUD.AuditSink(path=scratch(), run_id="t", boot_id="b")
    rig = AuditRig(sink=sink, clock=clock)
    # 重新以帶 last_control 的 wiring 組一條（仍走正式 builder）
    rig.obs_source, rig.recovery, rig.wiring = SVC.build_production_stack(
        reader=rig.plant.ess_reader, meter_source=rig.plant.meter_source,
        last_control_provider=lambda: (rec, LCS.TRUST_FOR_INTERVAL),
        clock=clock, local_now=lambda: rig._at["now"], audit_sink=sink)
    rig.evidence = AUD.CycleEvidence(rig.obs_source)
    rig.chain = rig.obs_source.__self__
    rig.arbiter = rig.chain.arbiter
    rig.runtime = RT.AutoControlRuntime(observation_source=rig.obs_source,
                                        clock=clock)
    rig.run(tl(G3.aware(2026, 7, 15, 14), n=5, pcs_charging=True,
               pcs_standby=False,
               ess_extra={"actual_active_power_kw": kw}))
    v = rig.records()[-1]
    check(f"★★ F6. 為什麼要先 STOP：pcs={v['pcs_state']} "
          f"decision={v['decision_action']} direction={v['direction_reason']} "
          f"→ control={v['control_action']}",
          v["pcs_state"] == PCI.PCS_CHARGING
          and v["decision_action"] == "discharge"
          and v["control_action"] == PCI.CTRL_STOP
          and bool(v["direction_reason"]))

    # Q7 當時 Meter / TOU / SOC / PCS 是什麼
    check("★★ F7. 每筆都同時保留 Meter / TOU / SOC / PCS 現場",
          all(set(("meter_power_kw", "tou_state", "soc_percent", "pcs_state"))
              <= set(x.keys()) for x in r.records())
          and c["pcs_state"] == PCI.PCS_STANDBY)

    # Q8 最後有沒有 authorized / executed
    check(f"★★ F8. 結果可查：authorized={c['authorized']} "
          f"executed={c['executed']} outcome={c['execution_outcome']} "
          f"no_action={c['no_action_reason']}",
          c["authorized"] is True and c["executed"] is False
          and bool(c["execution_outcome"]) and bool(c["no_action_reason"]))


# ======================================================================
# I —— audit 不得增加 device I/O
# ======================================================================
def test_I_no_extra_io():
    print("\n[I] with / without audit 的 observation call count 必須完全相同")
    counts = {}
    for name, audit in (("without", False), ("with", True)):
        rig = AuditRig()
        before = rig.plant.counts()
        rig.run(tl(n=10), audit=audit)
        counts[name] = tuple(a - b for a, b in zip(rig.plant.counts(), before))
        if audit:
            rows = len(rig.records())
    check(f"★★ I1. 無 audit：ESS/電表 = {counts['without']}",
          counts["without"] == (20, 20))
    check(f"★★ I2. 有 audit：ESS/電表 = {counts['with']}（完全相同）",
          counts["with"] == counts["without"])
    check(f"★★ I3. 有 audit 時確實寫出 {rows} 筆（10 cycles）", rows == 10)
    # ObservationCapture 只保留參考，不重跑：呼叫 N 次就只轉發 N 次
    class _FakeObserver(object):
        def __init__(self):
            self.calls = 0

        def observe(self):
            self.calls += 1
            return f"obs{self.calls}"

    fo = _FakeObserver()
    cap = AUD.ObservationCapture(fo)
    a1, a2 = cap.observe(), cap.observe()
    ev = AUD.CycleEvidence(None)
    check(f"★★ I4. capture 只轉發不重跑：底層被呼叫 {fo.calls} 次，"
          f"latest={cap.latest!r} previous={cap.previous!r}",
          fo.calls == 2 and (a1, a2) == ("obs1", "obs2")
          and cap.latest == "obs2" and cap.previous == "obs1")
    cap.reset_cycle()
    check("  I4b. reset_cycle 清掉上一輪觀測（不誤用成本輪證據）",
          cap.latest is None and cap.previous is None and fo.calls == 2
          and ev.observation is None)
    src = io.open(AUD.__file__, encoding="utf-8").read()
    check("★★ I5. audit 模組本身不呼叫任何 reader / adapter / observe",
          "ess_adapter" not in src and "meter_adapter" not in src
          and ".observe()" in src.split("class ObservationCapture")[1]
          .split("class ResultCapture")[0])


# ======================================================================
# L —— 長跑
# ======================================================================
def test_L_longrun():
    print("\n[L] 長跑：300 cycles、seq 連續、實耗時 ≈ 0")
    r = AuditRig()
    t0 = time.monotonic()
    recs = r.run(tl(n=300))
    elapsed = time.monotonic() - t0
    rep = AUD.sequence_report(r.records())
    check(f"★★ L1. 300 cycles → {rep['count']} 行，seq 1~{rep['last']}，"
          f"無 gap / duplicate",
          len(recs) == 300 and rep["count"] == 300 and rep["last"] == 300
          and not rep["gaps"] and not rep["duplicates"])
    check(f"★★ L2. 實際耗時 {elapsed:.2f}s（虛擬 {300 * 30 / 3600:.1f} 小時）",
          elapsed < 30.0)
    check(f"  L3. sink 全程健康：{r.sink.status()['healthy']}，"
          f"失敗 {r.sink.write_failure_count} 筆",
          r.sink.healthy is True and r.sink.write_failure_count == 0)
    size = os.path.getsize(r.sink.path)
    check(f"  L4. 300 筆約 {size // 1024} KB "
          f"（每筆約 {size // 300} B；rotation 未裁示，需留意磁碟）",
          size > 0)
    check(f"★★ L5. RAM ring 仍為 {RT.AUDIT_LIMIT} 筆上限（未被 durable 取代）",
          len(r.runtime.audit) == RT.AUDIT_LIMIT)


# ======================================================================
# G —— scope
# ======================================================================
def test_G_scope():
    print("\n[G] scope：只做 GAP-4；GAP-1/2/3 不回歸")
    src, _r, w = SVC.build_production_stack(client=None, meter_client=None)
    observer = src.__self__.arbiter.observer
    check("★★ G1. GAP-3 未回歸：TariffProvider 仍已接上、Asia/Taipei aware",
          isinstance(observer.tariff_provider, TP.TariffProvider)
          and observer._local_now().tzinfo is not None
          and observer.tariff_provider.holiday_provider
          is AC.PRODUCTION_PROVIDER)
    check("★★ G2. GAP-3 未回歸：naive 仍 Fail Closed",
          observer.tariff_provider.observe(
              __import__("datetime").datetime(2026, 7, 15, 14)).reason
          == TP.TP_NAIVE_DATETIME)
    check("★★ G3. GAP-1 未回歸：cadence 唯一來源仍為 production config",
          SVC.production_interval_sec() == 30.0
          and not hasattr(SVC, "DEFAULT_INTERVAL_SEC"))
    check("★★ G4. GAP-2 未回歸：waiter 仍可注入，cadence 仍須 > 0",
          "waiter" in SVC.run.__code__.co_varnames
          and SVC._usable_interval(0) is False)
    check("★★ G5. GAP-6 未動：未新增 process lifecycle enum",
          not (RT.RUNTIME_STATES & {"INITIALIZING", "READY", "RUNNING",
                                    "STOPPING", "STOPPED"}))
    check("★★ G6. 未定案參數仍為 None",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None
          and CFG.DEFAULT_CONTROL_CONFIG.meter_stale_grace_sec is None)
    check("★★ G7. Runtime 仍為 zero-I/O：未新增 audit_sink 注入點",
          "audit_sink" not in io.open(RT.__file__, encoding="utf-8").read()
          and "audit_sink" not in RT.AutoControlRuntime.__init__
          .__code__.co_varnames)
    check("★★ G8. ArbitrationResult schema 未被擴充（未加 meter_state）",
          "meter_state" not in
          [f.name for f in __import__("dataclasses").fields(
              __import__("production_arbiter").ArbitrationResult)])
    check(f"  G9. wiring report 顯示 audit_sink = "
          f"{w.sources.get('audit_sink')}（未注入時）",
          w.sources.get("audit_sink") == PRD.SRC_NOT_WIRED)
    check("★★ G10. 測試全程未寫入 production audit 路徑",
          not os.path.exists(AUD.DEFAULT_AUDIT_DIR)
          or not os.path.exists(AUD.DEFAULT_AUDIT_PATH))


# ======================================================================
def main():
    try:
        for fn in (test_P_policies, test_W_wall_clock, test_A_schema,
                   test_B_sequence_and_restart, test_C_corruption,
                   test_D_write_failure, test_E_not_recovery_source,
                   test_F_questions, test_I_no_extra_io, test_L_longrun,
                   test_G_scope):
            fn()
    finally:
        for d in TMPDIRS:
            shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
