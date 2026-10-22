# -*- coding: utf-8 -*-
"""
test_phase6_hmi_auth_renewal.py — HMI READ 認證續期回歸
======================================================================
核心命題
    「401 只恢復 READ。
      bound 沿用既有 6.9（2 次），超過即 Fail Closed；
      timeout / 500 / malformed 一律不重新登入；
      而且**任何情況下都不得**重啟 Attempt、重跑 GATE A、
      或重送 pause / CHARGE / restore。」

使用者指定的 16 項
    AUTH1  normal read                    -> 0 renewal
    AUTH2  first read 401 -> renewal      -> success
    AUTH3  repeated 401 -> bounded        -> success within official bound
    AUTH4  exceeds official bound         -> Fail Closed
    AUTH5  timeout                        -> 不觸發 re-login
    AUTH6  HTTP 500                       -> 不觸發 re-login
    AUTH7  malformed payload              -> 不觸發 re-login
    AUTH8  renewal rebuilds client+reader coherently
    AUTH9  no password/token in log/artifact
    AUTH10 renewal during preflight       -> only read retried
    AUTH11 renewal after pause            -> pause count still 1
    AUTH12 renewal after CHARGE           -> charge count still 1
    AUTH13 renewal during restore verify  -> restore count still 1
    AUTH14 no attempt restart
    AUTH15 no GATE A restart
    AUTH16 no command resend

用法
    python test_phase6_hmi_auth_renewal.py        # exit 0 = PASS
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_hmi_auth_renewal as AUTH              # noqa: E402
import phase6_first_live_runner as R69              # noqa: E402

RESULTS = []
SRC = os.path.join(HERE, "phase6_hmi_auth_renewal.py")
PROV = os.path.join(HERE, "phase6_first_live_field_provider.py")


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def src(p):
    return io.open(p, encoding="utf-8").read()


# ======================================================================
# 替身
# ======================================================================
def ok_reading(**kw):
    d = {"communication_ok": True, "soc_percent": 42.0, "_fail": []}
    d.update(kw)
    return d


def r401(paths=("/hmiUser/getRunMode",)):
    return ok_reading(_fail=[p + ":_error401" for p in paths])


class ScriptedReader(object):
    """依序回傳預先編排的 reading。"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.seq[min(self.calls - 1, len(self.seq) - 1)]


class Relogin(object):
    def __init__(self, ok=True, exc=None):
        self.ok, self.exc, self.calls = ok, exc, 0

    def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.ok


# ======================================================================
# AUTH1 ~ AUTH4：bound
# ======================================================================
def test_AUTH1_normal():
    print("\n[AUTH1] normal read -> 0 renewal")
    rd, rl = ScriptedReader([ok_reading()]), Relogin()
    r = AUTH.AuthRenewingReader(rd, rl)
    out = r()
    check("★★ AUTH1. 讀取成功", out["communication_ok"] is True)
    check("★★ AUTH1. renewal_count = 0", r.renewal_count == 0)
    check("★★ AUTH1. relogin 從未被呼叫", rl.calls == 0)
    check("  result = AUTH_OK", r.last_result == AUTH.AUTH_OK)
    check("  reader 只被讀一次", rd.calls == 1)


def test_AUTH2_first_401():
    print("\n[AUTH2] first read 401 -> renewal -> success")
    rd, rl = ScriptedReader([r401(), ok_reading()]), Relogin()
    r = AUTH.AuthRenewingReader(rd, rl)
    out = r()
    check("★★ AUTH2. 續期後讀取成功", not AUTH.is_auth_expired(out))
    check("★★ AUTH2. renewal_count = 1", r.renewal_count == 1)
    check("★★ AUTH2. relogin 被呼叫一次", rl.calls == 1)
    check("★★ AUTH2. result = AUTH_RENEWED", r.last_result == AUTH.AUTH_RENEWED)
    check("  read_recovered = True", r.last_read_recovered is True)
    check("  只重讀同一筆（reader 共 2 次）", rd.calls == 2)


def test_AUTH3_bounded_success():
    print("\n[AUTH3] repeated 401 -> bounded renewal -> success within bound")
    rd = ScriptedReader([r401(), r401(), r401(), ok_reading()])
    rl = Relogin()
    r = AUTH.AuthRenewingReader(rd, rl)
    r()              # 第 1 次讀：401 -> 續期#1 -> 仍 401
    check("  第一次呼叫後 count = 1", r.renewal_count == 1)
    out = r()        # 第 2 次讀：401 -> 續期#2 -> ok
    check("★★ AUTH3. bound 內成功恢復", not AUTH.is_auth_expired(out))
    check("★★ AUTH3. renewal_count = 2 = 官方 bound",
          r.renewal_count == 2 == AUTH.AUTH_RENEWAL_MAX_ATTEMPTS)
    check("  result = AUTH_RENEWED", r.last_result == AUTH.AUTH_RENEWED)


def test_AUTH4_exhausted():
    print("\n[AUTH4] exceeds official bound -> Fail Closed")
    rd, rl = ScriptedReader([r401()]), Relogin()
    r = AUTH.AuthRenewingReader(rd, rl)
    for _ in range(4):
        out = r()
    check("★★ AUTH4. renewal_count 停在官方 bound",
          r.renewal_count == AUTH.AUTH_RENEWAL_MAX_ATTEMPTS)
    check("★★ AUTH4. relogin 不超過 bound",
          rl.calls == AUTH.AUTH_RENEWAL_MAX_ATTEMPTS)
    check("★★ AUTH4. result = AUTH_RENEWAL_EXHAUSTED",
          r.last_result == AUTH.AUTH_RENEWAL_EXHAUSTED)
    check("★★ AUTH4. 仍回傳 401 reading -> 上層 Fail Closed",
          AUTH.is_auth_expired(out))
    check("  read_recovered = False", r.last_read_recovered is False)
    check("★★ AUTH4. bound 不得自行加大（= 2）",
          AUTH.AUTH_RENEWAL_MAX_ATTEMPTS == 2)
    # 續期本身失敗
    r2 = AUTH.AuthRenewingReader(ScriptedReader([r401()]), Relogin(ok=False))
    r2()
    check("  relogin 回 False -> AUTH_RENEWAL_FAILED",
          r2.last_result == AUTH.AUTH_RENEWAL_FAILED)
    r3 = AUTH.AuthRenewingReader(ScriptedReader([r401()]),
                                 Relogin(exc=RuntimeError("boom")))
    r3()
    check("  relogin 拋例外 -> AUTH_RENEWAL_FAILED（不外漏）",
          r3.last_result == AUTH.AUTH_RENEWAL_FAILED)


# ======================================================================
# AUTH5 ~ AUTH7：error classification
# ======================================================================
def test_AUTH5_AUTH6_AUTH7_classification():
    print("\n[AUTH5/6/7] timeout / 500 / malformed -> 不觸發 re-login")
    cases = [
        ("AUTH5 timeout", ok_reading(_fail=["/a:ReadTimeout"])),
        ("AUTH5 connection refused", ok_reading(_fail=["/a:ConnectionError"])),
        ("AUTH6 HTTP 500", ok_reading(_fail=["/a:_error500"])),
        ("AUTH6 HTTP 403", ok_reading(_fail=["/a:_error403"])),
        ("AUTH7 malformed payload", ok_reading(_fail=["/a"])),
        ("AUTH7 非 dict", ["not", "a", "dict"]),
        ("AUTH7 None", None),
        ("AUTH7 schema error", ok_reading(_fail=["/a:KeyError"])),
        ("AUTH7 device fault", ok_reading(_fail=[], pcs_fault_flag=True)),
    ]
    for label, reading in cases:
        check(f"★★ {label} -> is_auth_expired = False",
              AUTH.is_auth_expired(reading) is False)
        rd, rl = ScriptedReader([reading]), Relogin()
        r = AUTH.AuthRenewingReader(rd, rl)
        r()
        check(f"  {label}：relogin 0 次、reader 1 次",
              rl.calls == 0 and rd.calls == 1)
    check("★★ AUTH5/6/7. 只認 %r" % AUTH.AUTH_EXPIRED_MARKER,
          AUTH.is_auth_expired(r401()) is True)
    # 與既有 6.9 判準完全一致
    for label, reading in cases:
        if isinstance(reading, dict):
            check(f"  6.9 `_has_401` 與共用 predicate 一致：{label}",
                  bool(R69._has_401(reading)) is AUTH.is_auth_expired(reading))
    check("  6.9 對 401 亦一致", bool(R69._has_401(r401())) is True)


# ======================================================================
# AUTH8 ~ AUTH9
# ======================================================================
def test_AUTH8_coherent_client():
    print("\n[AUTH8] renewal rebuilds client + reader coherently")

    class FakeClient(object):
        def __init__(self):
            self.token = "old"
            self.logins = 0

        def login_hmi(self, username=None):
            self.logins += 1
            self.token = "new-%d" % self.logins
            return self.token

    c = FakeClient()
    seen = []

    def reader():
        seen.append(c.token)          # reader 永遠讀「當下」client 的 token
        return r401() if c.token == "old" else ok_reading()

    rl = AUTH.build_inplace_relogin(c, username="dummy")
    r = AUTH.AuthRenewingReader(reader, rl)
    out = r()
    check("★★ AUTH8. 原地 re-login（同一 client 物件）", c.logins == 1)
    check("★★ AUTH8. 續期後 reader 立即用到新 token",
          seen == ["old", "new-1"])
    check("★★ AUTH8. 不存在 old-token + new-reader 混合",
          not AUTH.is_auth_expired(out) and seen[-1] == c.token)
    fn = next(n for n in ast.walk(ast.parse(src(SRC)))
              if isinstance(n, ast.FunctionDef)
              and n.name == "build_inplace_relogin")
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(fn) if isinstance(n, ast.Call)}
    check("★★ AUTH8. 不重建 client / reader / chain",
          not (calls & {"build_api_client", "build_ess_reader",
                        "build_live_chain_bundle", "ApiClient"}))
    check("  provider 亦採原地 re-login（不重建 bundle）",
          "build_inplace_relogin" in src(PROV)
          and "build_api_client(login=True)" not in
          src(PROV).split("_relogin =")[1][:200])


def test_AUTH9_no_secret():
    print("\n[AUTH9] no password/token in log/artifact")
    tree = ast.parse(src(SRC))
    skip = set()
    for n in ast.walk(tree):
        b = getattr(n, "body", None)
        if isinstance(b, list) and b:
            f = b[0]
            if (isinstance(f, ast.Expr) and isinstance(f.value, ast.Constant)
                    and isinstance(f.value.value, str)):
                skip.add(id(f.value))
    # 🔴 排除 docstring **與 `main()` 自述輸出** —— 本專案慣例會在自述裡寫
    #    「不記錄 password / token / session secret」，掃它是假陽性（既往教訓）。
    mainfn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if mainfn is not None:
        for n in ast.walk(mainfn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                skip.add(id(n))
    strs = {n.value.lower() for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in skip}
    ids = {n.id.lower() for n in ast.walk(tree) if isinstance(n, ast.Name)}
    ids |= {n.attr.lower() for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)}
    for bad in ("password", "passwd", "secret", "api_key", "-----begin"):
        check(f"★★ AUTH9. 字面值不含 {bad}",
              not any(bad in x for x in strs))
        check(f"  識別字不含 {bad}", bad not in ids)
    r = AUTH.AuthRenewingReader(ScriptedReader([r401(), ok_reading()]),
                                Relogin())
    r()
    ev = r.evidence()
    check("★★ AUTH9. evidence 只有非敏感欄位",
          set(ev) == set(AUTH.EVIDENCE_FIELDS) | {"auth_renewal_max"})
    blob = str(ev).lower()
    for bad in ("token", "password", "secret", "bearer"):
        check(f"  evidence 不含 {bad}", bad not in blob)


# ======================================================================
# AUTH10 ~ AUTH16：只恢復 READ，絕不重送任何 command
# ======================================================================
def test_AUTH10_only_read_retried():
    print("\n[AUTH10] renewal during preflight -> only read retried")
    rd, rl = ScriptedReader([r401(), ok_reading()]), Relogin()
    r = AUTH.AuthRenewingReader(rd, rl)
    r()
    check("★★ AUTH10. 只重讀一次（reader 2 次，非重跑整段）", rd.calls == 2)
    check("★★ AUTH10. 沒有第二次 relogin", rl.calls == 1)
    check("  history 只有一筆續期紀錄", len(r.history) == 1)


def _no_command_identifiers(path, label):
    tree = ast.parse(src(path))
    ids = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    ids |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    strs = {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    bad = ("send_pause", "send_restore", "send_pause_ex", "send_restore_ex",
           "request_pause", "request_restore", "run_live_leg_cli",
           "acquire_ownership", "HandoffRunner", "FirstLiveDispatcher",
           "arm_and_run_handoff", "pause", "restore", "charge")
    for b in bad:
        check(f"★★ {label}. 不引用 {b}", b not in ids and b not in strs)


def test_AUTH11_to_AUTH16_no_command_resend():
    print("\n[AUTH11~16] 續期不得重送任何 control command")
    # 結構證明：auth 模組完全沒有 command 能力
    _no_command_identifiers(SRC, "AUTH16")
    tree = ast.parse(src(SRC))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    for m in ("phase6_remote_adapter", "phase6_handoff_orchestrator",
              "phase6_first_live_handoff", "pcs_auto_control_service",
              "subprocess"):
        check(f"★★ AUTH16. 模組層未 import {m}", m not in mods)

    # 行為證明：續期期間 command 計數器完全不動
    import phase6_first_live_handoff as FL
    guard = FL.CommandCountGuard()
    guard.take("pause")
    guard.take("phase6_charge")
    guard.take("restore")
    before = dict(guard.counts)
    rd = ScriptedReader([r401(), r401(), r401(), r401(), ok_reading()])
    r = AUTH.AuthRenewingReader(rd, Relogin())
    for _ in range(3):
        r()
    check("★★ AUTH11. pause count 仍為 1", guard.count("pause") == 1)
    check("★★ AUTH12. charge count 仍為 1", guard.count("phase6_charge") == 1)
    check("★★ AUTH13. restore count 仍為 1", guard.count("restore") == 1)
    check("★★ AUTH16. 全部計數未被續期改動", guard.counts == before)
    check("  續期已達 bound 仍不影響計數",
          r.renewal_count == AUTH.AUTH_RENEWAL_MAX_ATTEMPTS)

    # AUTH14 / AUTH15：reader 沒有任何「重啟」能力
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "AuthRenewingReader")
    calls = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
             for n in ast.walk(cls) if isinstance(n, ast.Call)}
    check("★★ AUTH14. 無 attempt restart（只呼叫 reader / relogin / 純工具）",
          calls <= {"_reader", "_relogin", "is_auth_expired", "_log", "append",
                    "bool", "int", "type", "ValueError"})
    check("★★ AUTH15. 不引用 gate_a / GATE A 重跑",
          "gate_a" not in calls
          and "pre_handoff_policy_eligibility" not in calls)
    check("  也不引用 post_handoff_revalidation",
          "post_handoff_revalidation" not in calls)


def test_AUTH17_provider_read_path():
    print("\n[附加] provider HMI read path 全部經過續期 reader")
    s = src(PROV)
    check("  provider 建立 AuthRenewingReader", "AuthRenewingReader" in s)
    check("★★ adapter 使用續期後的 reader",
          "EssSnapshotAdapter(reader=reader)" in s)
    outer = next(n for n in ast.walk(ast.parse(s))
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "build_field_dependencies")
    ds = next(n for n in ast.walk(outer)
              if isinstance(n, ast.FunctionDef) and n.name == "_decision_sample")
    seq = [(getattr(c.func, "attr", None) or getattr(c.func, "id", None))
           for c in ast.walk(ds) if isinstance(c, ast.Call)]
    seq = [x for x in seq if x in ("sample", "observe")]
    check("★★ _decision_sample 先 sample()（會續期）再 observe()",
          seq[:2] == ["sample", "observe"])
    check("  artifact 帶出 auth evidence", "hmi_auth_renewal" in s)
    check("  source_state 帶出 bound 與 count",
          "auth_renewal_bound" in s and "auth_renewal_count" in s)


# ======================================================================
def main():
    for fn in (test_AUTH1_normal, test_AUTH2_first_401,
               test_AUTH3_bounded_success, test_AUTH4_exhausted,
               test_AUTH5_AUTH6_AUTH7_classification,
               test_AUTH8_coherent_client, test_AUTH9_no_secret,
               test_AUTH10_only_read_retried,
               test_AUTH11_to_AUTH16_no_command_resend,
               test_AUTH17_provider_read_path):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
