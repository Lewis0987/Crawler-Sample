# -*- coding: utf-8 -*-
"""
test_phase6_attempt10_gaps.py — Attempt #10 安全缺口修正（GAP-2～GAP-6，離線）
======================================================================
組別
    G3  GAP-3 restore 把關例外 → WITHHELD；journal / ledger / index 持久化如實回報
    G2  GAP-2 lease 釋放狀態（NEVER_HELD / RELEASED / RELEASE_UNKNOWN …）
        ＋ ReleaseMutex 實際 BOOL 結果
    G6  GAP-6 TOU 與時段同一時間基準（Asia/Taipei aware ＋ 取樣前後時間）
    G5  GAP-5 RestoreClearance（綁定 attempt / orchestrator / 檢查結果、一次性、逾時）
    G4  GAP-4 歷史清單 ＋ runtime index ＋ ATTEMPT_CLOSED 交叉驗證
    GA  缺陷 A：INDEX_ENTRY 確認綁定該 attempt 的紀錄（不是整份 index）
    GB  缺陷 B：runner 排除以 ledger 實際檔案身分判定（不只 attempt_id）
    GU  GAP-3 U-1：RESTORE_REQUESTED / RESTORE INTENT journal 寫入失敗

是否需要設備
    **不需要**。全部 fake / 注入 / 暫存目錄。不建立正式 runtime index，
    不寫入正式 output。只有標示「HOLD 解除」的案例在本程序內暫時解除 HOLD。

用法
    python test_phase6_attempt10_gaps.py        # exit 0 = PASS
"""
import datetime
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcs_auto_control_runtime as RT                # noqa: E402
import phase6_first_live_handoff as FL               # noqa: E402
import phase6_handoff_orchestrator as HO             # noqa: E402
import phase6_remote_adapter as RA                   # noqa: E402
import phase6_scheduled_first_live as SC             # noqa: E402
import run_scheduled_first_live as LAUNCH            # noqa: E402
import _phase6_hold_override as HOLD                 # noqa: E402
import test_phase6_attempt10_safety as A10           # noqa: E402

RESULTS = []
_TMP = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def rest_ok():
    return HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False


def tmpdir(prefix="a10g_"):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP.append(d)
    return d


def run_h(**kw):
    """HOLD 暫時解除下跑一次 A10.Harness。"""
    h = A10.Harness(**kw)
    A10.released(h.run)
    return h


def withheld(h):
    return A10._withheld(h)


# ======================================================================
# G3 — GAP-3
# ======================================================================
class _Clock(object):
    def __init__(self, seq):
        self.seq = list(seq)

    def __call__(self):
        v = self.seq.pop(0) if self.seq else self.seq_last
        self.seq_last = v
        if isinstance(v, Exception):
            raise v
        return v


class _FailingJournal(HO.Journal):
    """寫入 RESTORE_WITHHELD（或之後的任何紀錄）時拋 OSError。"""

    def __init__(self, path, fail_on_state=HO.S_RESTORE_WITHHELD):
        HO.Journal.__init__(self, path=path)
        self.fail_on_state = fail_on_state
        self.failing = False

    def append(self, kind, state=None, **kw):
        if state == self.fail_on_state or self.failing:
            self.failing = True
            raise OSError("disk full（模擬）")
        return HO.Journal.append(self, kind, state=state, **kw)


def test_G3():
    print("\n[G3] GAP-3 restore 把關例外 / 持久化如實回報（HOLD 解除）")
    for tag, seq in (("時鐘第二次拋例外", [1.0, RuntimeError("clock")]),
                     ("時鐘回傳 None", [1.0, None]),
                     ("時鐘回傳 NaN", [1.0, float("nan")]),
                     ("時鐘倒退（age < 0）", [10.0, 5.0])):
        h = run_h(clock=_Clock(seq))
        check("G3-1 ★★ %s -> restore 0、WITHHELD、責任保留" % tag, withheld(h))
        check("G3-1b %s -> journal_persistence = OK（WITHHELD 已落盤）" % tag,
              h.res["journal_persistence"] == HO.JP_OK)

    class _BadLease(object):
        acquire_attempted = True

        @property
        def held(self):
            raise RuntimeError("lease 讀取失敗")

    disp = A10.LeaseDispatcher()
    disp.lease = _BadLease()
    h = run_h(dispatcher=disp)
    check("G3-2 ★★ lease 屬性拋例外 -> restore 0、WITHHELD", withheld(h))

    # journal 寫入 WITHHELD 失敗
    h = A10.Harness(restore_over={"authority": "UNKNOWN"})
    fj = _FailingJournal(os.path.join(tmpdir(), "j.jsonl"))
    h.o.journal = fj
    A10.released(h.run)
    check("G3-3 ★★ journal 寫入失敗 -> 不往上拋、restore 0、WITHHELD（記憶體）",
          withheld(h))
    check("G3-3b ★★ journal_persistence = FAILED:OSError（不得宣稱已保存）",
          h.res["journal_persistence"] == "FAILED:OSError")
    recs = HO.Journal(path=fj.path).read_all()
    check("G3-3c 實際 journal 確實沒有 WITHHELD 紀錄（與回報一致）",
          not [r for r in recs if r.get("state") == HO.S_RESTORE_WITHHELD])
    h = A10.Harness(restore_over={"authority": "UNKNOWN"})
    h.o.journal = None
    A10.released(h.run)
    check("G3-4 沒有 journal -> journal_persistence = NOT_ATTEMPTED",
          withheld(h) and h.res["journal_persistence"] == HO.JP_NOT_ATTEMPTED)

    # --- persistence_summary 純函式 ---
    P = HO.persistence_summary
    cases = (
        (("OK", "OK", "OK"), HO.PERSISTED),
        (("OK", "OK", "NOT_ATTEMPTED"), HO.PERSISTED),
        (("OK", "FAILED:OSError", "OK"), HO.PERSISTED_PARTIAL),
        (("FAILED:OSError", "OK", "FAILED:X"), HO.PERSISTED_PARTIAL),
        (("FAILED:OSError", "FAILED:OSError", "FAILED:OSError"),
         HO.CRITICAL_NOT_PERSISTED),
        (("FAILED:OSError", "FAILED:OSError", "NOT_ATTEMPTED"),
         HO.CRITICAL_NOT_PERSISTED),
        (("NOT_ATTEMPTED", "NOT_ATTEMPTED", "NOT_ATTEMPTED"),
         HO.CRITICAL_NOT_PERSISTED))
    for args, want in cases:
        check("G3-5 persistence%r -> %s" % (args, want), P(*args)[0] == want)

    # --- scheduled runner：ledger 寫入失敗 ---
    import test_phase6_scheduled_first_live as TS

    def rig_with_failing(kind):
        rig = TS.Rig(TS.INSIDE)
        orig = rig.ledger.append

        def app(k, **f):
            if k == kind:
                raise OSError("ledger 寫入失敗（模擬）")
            return orig(k, **f)
        rig.ledger.append = app
        return rig
    rig = rig_with_failing("CONSUMED")
    ev = A10.released(lambda: rig.runner(prior_journal_dir=tmpdir()).run())
    check("G3-6 ★★ ledger consume 失敗 -> 不拋例外、result 保留",
          ev["result"] == SC.R_PASS and rig.handoff_calls == 1)
    check("G3-6b ★★ ledger_persistence FAILED、one_shot_consumed False（不宣稱已消耗）",
          ev["persistence"]["detail"]["ledger"] == "FAILED:OSError"
          and ev["one_shot_consumed"] is False)
    rig = rig_with_failing("LIFECYCLE")
    ev = A10.released(lambda: rig.runner(prior_journal_dir=tmpdir()).run())
    check("G3-7 ★★ 開始前 ledger 就寫不進去 -> EXCEPTION_FAIL_CLOSED、handoff 0",
          ev["result"] == SC.R_EXCEPTION and rig.handoff_calls == 0)

    # 三者皆失敗 -> CRITICAL_NOT_PERSISTED（launcher summary ＋ stderr）
    ev = {"persistence": {"detail": {"journal": "FAILED:OSError",
                                     "ledger": "FAILED:OSError"}}}
    summ = {}
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        ov = LAUNCH._report_persistence(summ, ev, index="FAILED:OSError")
    check("G3-8 ★★ journal / ledger / index 皆失敗 -> CRITICAL_NOT_PERSISTED",
          ov == HO.CRITICAL_NOT_PERSISTED
          and summ.get("critical") == HO.CRITICAL_NOT_PERSISTED
          and "CRITICAL_NOT_PERSISTED" in err.getvalue())
    summ = {}
    ov = LAUNCH._report_persistence(
        summ, {"persistence": {"detail": {"journal": "OK",
                                          "ledger": "FAILED:OSError"}}},
        index="OK")
    check("G3-8b 部分失敗 -> PARTIAL，且失敗項目如實列出",
          ov == HO.PERSISTED_PARTIAL
          and summ["persistence"]["detail"]["ledger"] == "FAILED:OSError"
          and "critical" not in summ)
    check("G3-9 rest state 維持", rest_ok())


# ======================================================================
# G2 — GAP-2
# ======================================================================
class _RealLeaseDispatcher(object):
    """使用**真實** `Phase6AuthorityLease` 的 dispatcher stub。"""

    def __init__(self, acquire=True, release=True, do_acquire=True,
                 do_release=True):
        self._acq, self._rel = acquire, release
        self.lease = FL.Phase6AuthorityLease(self._acquire, self._release)
        self.do_acquire, self.do_release = do_acquire, do_release
        self.calls = 0

    def _acquire(self):
        return (self._acq, "fake")

    def _release(self):
        if isinstance(self._rel, Exception):
            raise self._rel
        return self._rel

    def __call__(self, obs):
        self.calls += 1
        if self.do_acquire:
            self.lease.acquire()
        if self.do_release:
            try:
                self.lease.release()
            except Exception:                                  # noqa: BLE001
                pass
        return {"dispatched": True, "verified": True, "released": True}


class _FakeK32(object):
    def __init__(self, rc):
        self.rc = rc
        self.closed = 0

    def ReleaseMutex(self, h):
        if isinstance(self.rc, Exception):
            raise self.rc
        return self.rc

    def CloseHandle(self, h):
        self.closed += 1
        return 1


def test_G2():
    print("\n[G2] GAP-2 lease 釋放狀態 ＋ ReleaseMutex 實際 BOOL（HOLD 解除）")
    for tag, kw, want_state, sent in (
            ("從未取得（acquire 失敗）", dict(acquire=False), HO.LS_NEVER_HELD, 1),
            ("正常取得並釋放", dict(), HO.LS_RELEASED, 1),
            ("release 回傳 False", dict(release=False), HO.LS_RELEASE_UNKNOWN, 0),
            ("release 拋例外", dict(release=RuntimeError("x")),
             HO.LS_RELEASE_UNKNOWN, 0),
            ("取得後從未 release", dict(do_release=False), HO.LS_STILL_HELD, 0)):
        h = run_h(dispatcher=_RealLeaseDispatcher(**kw))
        ok_state = h.res["phase6_release_state"] == want_state
        if sent:
            check("G2-1 ★★ %s -> %s、restore 1 次" % (tag, want_state),
                  ok_state and h.sd.restore_calls == 1)
        else:
            check("G2-1 ★★ %s -> %s、restore 0、WITHHELD" % (tag, want_state),
                  ok_state and withheld(h)
                  and h.res["restore_precheck"]["checks"][
                      "phase6_released"] is False)
    check("G2-1b release_attempted 只是新增欄位（held / release_ok 語意不變）",
          set(("held", "release_ok", "acquired_once", "release_attempted"))
          <= set(vars(FL.Phase6AuthorityLease(lambda: (1, 0),
                                               lambda: True))))

    # --- 不以 is_owner() 推定原本未持有 ---
    import inspect
    src = inspect.getsource(HO.HandoffRunner._lease_state)
    check("G2-2 ★★ C3 不以 is_owner() 推定（只看 lease 自己的狀態）",
          "is_owner" not in src.split('"""')[-1])

    # --- SVC.release_ownership 回傳 ReleaseMutex 的實際 BOOL ---
    import threading
    import pcs_auto_control_service as SVC
    saved_own, saved_k = dict(SVC._own), SVC._k32
    try:
        for tag, rc, want in (("ReleaseMutex 成功（1）", 1, True),
                              ("ReleaseMutex 失敗（0）", 0, False),
                              ("ReleaseMutex 拋例外", OSError("x"), False)):
            k = _FakeK32(rc)
            SVC._k32 = (lambda k=k: k)
            SVC._own.update(owned=True, handle=12345,
                            thread_id=threading.get_ident())
            got = SVC.release_ownership()
            check("G2-3 ★★ %s -> release_ownership() = %r" % (tag, want),
                  got is want)
            check("G2-3b %s -> 記帳清除、handle 已關閉" % tag,
                  SVC._own["owned"] is False and k.closed == 1)
        SVC._own.update(owned=False, handle=None)
        check("G2-4 本來就未持有 -> False（不得推定成功）",
              SVC.release_ownership() is False)
        SVC._own.update(owned=True, handle=1, thread_id=-1)
        check("G2-5 ★★ 非取得執行緒 -> False，記帳維持持有",
              SVC.release_ownership() is False and SVC._own["owned"] is True)
    finally:
        SVC._own.clear()
        SVC._own.update(saved_own)
        SVC._k32 = saved_k
    check("G2-6 原始碼：回傳 bool(k.ReleaseMutex(...))",
          "released = bool(k.ReleaseMutex(" in inspect.getsource(
              SVC.release_ownership))


# ======================================================================
# G6 — GAP-6
# ======================================================================
TZ8 = A10.TZ8


def _t(h, m, s=0, us=0, tz=TZ8):
    return datetime.datetime(2026, 11, 7, h, m, s, us, tzinfo=tz)


def _br(before, after=None, mono=(0.0, 0.0), host=None):
    after = after or before
    return {"before": before, "after": after, "mono_before": mono[0],
            "mono_after": mono[1],
            "host_before": host if host is not None
            else before.replace(tzinfo=None)}


def _pc(br, now):
    s = A10.smp(external_liveness_verdict=RA.EXT_NOT_RUNNING,
                tou_sampled_between=br)
    ok, ck = HO.restore_precheck(s, A10.probe(), A10.PID, True, now)
    return ok, ck


def test_G6():
    print("\n[G6] GAP-6 TOU 與時段同一時間基準")
    ok, ck = _pc(_br(_t(0, 10)), _t(0, 10))
    check("G6-0 基準情境（全部 Asia/Taipei、時段內）-> PASS", ok)
    check("G6-0b 新增檢查項 time_basis_ok / time_order_ok",
          {"time_basis_ok", "time_order_ok"} <= set(ck)
          and set(ck) == set(HO.RESTORE_PRECHECK_ITEMS))
    cases = (
        ("05:29:59.999999（上界前一刻）", _br(_t(5, 29, 59, 999999)),
         _t(5, 29, 59, 999999), True),
        ("05:30:00.000000（上界）", _br(_t(5, 30)), _t(5, 30), False),
        ("00:00:00（下界）", _br(_t(0, 0)), _t(0, 0), True),
        ("取樣跨過 05:30（before 05:29:58 / after 05:30:01）",
         _br(_t(5, 29, 58), _t(5, 30, 1), mono=(0.0, 3.0)), _t(5, 30, 1), False),
        ("缺少取樣時間", None, _t(0, 10), False),
        ("after 早於 before（時間倒退）",
         _br(_t(0, 10, 5), _t(0, 10, 0), mono=(0.0, 0.0)), _t(0, 10, 6), False),
        ("precheck now 早於 after（取樣後時鐘倒退）", _br(_t(0, 20)),
         _t(0, 10), False),
        ("牆上 10 秒 / monotonic 0 秒（取樣中被調整時鐘）",
         _br(_t(0, 10, 0), _t(0, 10, 10), mono=(5.0, 5.0)), _t(0, 10, 10),
         False),
        ("monotonic 倒退", _br(_t(0, 10), mono=(9.0, 1.0)), _t(0, 10), False),
        ("monotonic 為 NaN", _br(_t(0, 10), mono=(0.0, float("nan"))),
         _t(0, 10), False),
        ("now 為 UTC", _br(_t(0, 10)), _t(16, 10, tz=datetime.timezone.utc),
         False),
        ("bracket 為 naive", _br(_t(0, 10).replace(tzinfo=None),
                                 host=_t(0, 10).replace(tzinfo=None)),
         _t(0, 10), False),
        ("now 為 naive", _br(_t(0, 10)), _t(0, 10).replace(tzinfo=None), False),
        ("主機時間與 Asia/Taipei 相差 1 小時（Windows 時區錯誤）",
         _br(_t(0, 10), host=datetime.datetime(2026, 11, 7, 1, 10)), _t(0, 10),
         False),
    )
    for tag, br, now, want in cases:
        if br is None:
            s = A10.smp(external_liveness_verdict=RA.EXT_NOT_RUNNING)
            ok = HO.restore_precheck(s, A10.probe(), A10.PID, True, now)[0]
        else:
            ok = _pc(br, now)[0]
        check("G6-1 ★★ %s -> %s" % (tag, "PASS" if want else "WITHHELD"),
              ok is want)

    # 端到端：取樣跨過 05:30 -> restore 0
    h = run_h(now=_t(5, 30, 1),
              restore_over={"tou_sampled_between":
                            _br(_t(5, 29, 58), _t(5, 30, 1), mono=(0.0, 3.0))})
    check("G6-2 ★★ 端到端：取樣跨過 05:30 -> restore 0、WITHHELD", withheld(h))

    # provider：observe() 前後各取一次與 TOU 同一來源的時間
    import phase6_first_live_field_provider as FP
    src = io.open(FP.__file__, encoding="utf-8").read()
    i_b = src.index('"before": _local_now()}')
    i_o = src.index("res = bundle.observe()", i_b - 400)
    i_a = src.index("after=_local_now()", i_o)
    check("G6-3 ★★ provider：before < bundle.observe() < after（原始碼順序）",
          i_b < i_o < i_a)
    check("G6-3b ★★ C3 時間來源 = PRD.build_local_now()（與 TOU 相同）",
          "PRD.build_local_now()" in src
          and "now_fn=_local_now, quick_sample_source" in src)
    check("G6-3c GAP-7：scheduled now_fn 已移除 ctx 覆寫（不再讀 ctx['now_fn']）",
          'ctx.get("now_fn")' not in src)

    # 全年：00:00–05:30 皆為 OFF_PEAK（production TariffProvider，離線）
    import pcs_auto_control_production as PRD
    import tariff_provider as TP
    tp = PRD.build_tariff_provider()
    tz, _w = TP.resolve_timezone(TP.PRODUCTION_TIMEZONE_NAME)
    bad = []
    for y in (2026, 2027):
        d = datetime.date(y, 1, 1)
        while d.year == y:
            for hm in ((0, 0), (2, 0), (5, 29)):
                r = tp.observe(datetime.datetime(d.year, d.month, d.day, *hm,
                                                 tzinfo=tz))
                if r.state != "OFF_PEAK":
                    bad.append((d, hm, r.state))
            d += datetime.timedelta(days=1)
    check("G6-4 ★★ 2026–2027 每日 00:00 / 02:00 / 05:29 皆為 OFF_PEAK"
          "（production TariffProvider；不符 %d 筆）" % len(bad), not bad)
    check("G6-4b TOU 與 C3 同一時區名稱（Asia/Taipei）",
          TP.PRODUCTION_TIMEZONE_NAME == "Asia/Taipei"
          and _t(0, 0).utcoffset() == HO.RESTORE_TIME_UTC_OFFSET)


# ======================================================================
# G5 — GAP-5
# ======================================================================
def _armed(attempt_id="A10-G5", sender=None, clock=None):
    calls = []

    def _send():
        calls.append(1)
        if sender is not None:
            sender()
        return True
    j = HO.Journal(path=os.path.join(tmpdir(), "j.jsonl"),
                   attempt_id=attempt_id)
    o = HO.HandoffOrchestrator(
        remote=HO.RemoteController(restore_sender=_send),
        observe=(lambda: {}), gates=(lambda x: (True, {})), journal=j,
        mode=HO.MODE_ARMED, clock=clock)
    for st in (HO.S_PREFLIGHT, HO.S_PAUSE_REQUESTED, HO.S_RESTORE_REQUESTED):
        o._to(st)
    return o, j, calls


def _intents(j):
    return [r for r in j.read_all()
            if r.get("kind") == "INTENT" and r.get("step") == "RESTORE"]


def test_G5():
    print("\n[G5] GAP-5 RestoreClearance")
    o, j, calls = _armed()
    sent, why = o.request_restore()
    check("G5-1 ★★ 沒有 clearance -> RESTORE_PRECHECK_REQUIRED、sender 0",
          sent is False and str(why).startswith(HO.RESTORE_PRECHECK_REQUIRED)
          and calls == [])
    check("G5-1b ★★ 拒絕時不寫 INTENT、不標記 attempted（仍可正常收尾）",
          _intents(j) == [] and o.guard.may_attempt("RESTORE"))
    c = o._issue_restore_clearance("d1")
    sent, _w = o.request_restore(c)
    check("G5-2 有效 clearance -> 送出 1 次，INTENT 記錄 digest",
          sent is True and len(calls) == 1
          and _intents(j)[-1].get("restore_clearance_digest") == "d1")
    sent2, why2 = o.request_restore(c)
    check("G5-3 ★★ 同一 clearance 重複呼叫 -> 不重送（sender 仍 1 次）",
          sent2 is False and len(calls) == 1)
    check("G5-3b clearance 已標記 used", c.used is True
          and o._clearance_ok(c)[0] is False)

    o, j, calls = _armed()
    c1 = o._issue_restore_clearance("d1")
    o._issue_restore_clearance("d2")
    check("G5-4 ★★ 舊 clearance（非最近核發者）-> 拒絕",
          o.request_restore(c1)[0] is False and calls == [])
    o2, j2, calls2 = _armed()
    c_o = o._issue_restore_clearance("dx")
    check("G5-5 ★★ 其他 orchestrator 的 clearance -> 拒絕",
          o2.request_restore(c_o)[0] is False and calls2 == [])
    o, j, calls = _armed()
    c = o._issue_restore_clearance("d1")
    c.attempt_id = "OTHER-ATTEMPT"
    check("G5-6 ★★ attempt_id 不符 -> 拒絕",
          o.request_restore(c)[0] is False and calls == [])
    o, j, calls = _armed()
    c = o._issue_restore_clearance("d1")
    c.checks_digest = "tampered"
    check("G5-7 ★★ 檢查結果 digest 被改 -> 拒絕",
          o.request_restore(c)[0] is False and calls == [])
    t = {"v": 0.0}
    o, j, calls = _armed(clock=(lambda: t["v"]))
    c = o._issue_restore_clearance("d1")
    t["v"] = HO.RESTORE_PRECHECK_MAX_AGE_SEC + 0.5
    check("G5-8 ★★ clearance 逾時 -> 拒絕",
          o.request_restore(c)[0] is False and calls == [])
    t["v"] = -1.0
    o, j, calls = _armed(clock=(lambda: t["v"]))
    c = o._issue_restore_clearance("d1")
    t["v"] = -5.0
    check("G5-8b 時鐘倒退（age < 0）-> 拒絕",
          o.request_restore(c)[0] is False and calls == [])

    # 重入：sender 內再呼叫一次 request_restore
    box = {}

    def reenter():
        box["inner"] = box["o"].request_restore(box["c"])
    o, j, calls = _armed(sender=reenter)
    box["o"], box["c"] = o, o._issue_restore_clearance("d1")
    sent, _w = o.request_restore(box["c"])
    check("G5-9 ★★ sender 內重入 -> 內層被拒、sender 只 1 次",
          sent is True and len(calls) == 1 and box["inner"][0] is False)

    # 兩個執行緒同時呼叫
    import threading
    o, j, calls = _armed(sender=(lambda: __import__("time").sleep(0.05)))
    c = o._issue_restore_clearance("d1")
    res = []
    ths = [threading.Thread(target=lambda: res.append(o.request_restore(c)))
           for _ in range(2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    check("G5-10 ★★ 兩個執行緒同時呼叫 -> sender 只 1 次",
          len(calls) == 1 and sum(1 for r in res if r[0]) == 1)

    # 端到端：runner 核發 clearance 並綁定檢查結果
    h = run_h()
    ints = [r for r in h.j.read_all() if r.get("kind") == "INTENT"
            and r.get("step") == "RESTORE"]
    want = HO.restore_checks_digest(h.res["restore_precheck"]["checks"],
                                    h.res["restore_recheck"]["checks"])
    check("G5-11 ★★ runner：restore 1 次，INTENT digest = 兩次檢查結果的 sha256",
          h.sd.restore_calls == 1 and ints
          and ints[-1].get("restore_clearance_digest") == want)

    # AST：正式程式只有 HandoffRunner._restore_gate_inner 核發
    import ast
    import glob as _g
    issuers = []
    for p in sorted(_g.glob(os.path.join(HERE, "*.py"))):
        b = os.path.basename(p)
        if b.startswith("test_") or b.startswith("_phase6"):
            continue
        try:
            t2 = ast.parse(io.open(p, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for fn in ast.walk(t2):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for n in ast.walk(fn):
                if (isinstance(n, ast.Call)
                        and getattr(n.func, "attr", None)
                        == "_issue_restore_clearance"):
                    issuers.append("%s:%s" % (b, fn.name))
                if (isinstance(n, ast.Call)
                        and getattr(n.func, "id", None) == "RestoreClearance"
                        and fn.name != "_issue_restore_clearance"):
                    issuers.append("%s:%s(direct)" % (b, fn.name))
    check("G5-12 ★★ 正式程式只有 _restore_gate_inner 核發 clearance：%r"
          % (issuers,),
          issuers == ["phase6_handoff_orchestrator.py:_restore_gate_inner"])


# ======================================================================
# G4 — GAP-4
# ======================================================================
import json                                           # noqa: E402

REAL_DIR = os.path.join(os.path.dirname(HERE), "output",
                        "phase6_scheduled_first_live")
REAL_MANIFEST = os.path.join(HERE, "phase6_attempt_history.json")
OFFICIAL_INDEX = r"C:\ProgramData\Phase6FirstLive\attempt_index.jsonl"


def _sha(p):
    return HO._sha256_file(p)


def _ledger(d, aid, recs):
    p = os.path.join(d, "%s.jsonl" % aid)
    with io.open(p, "w", encoding="utf-8") as f:
        for i, r in enumerate(recs, 1):
            r = dict(r, schema_version=1, seq=i)
            f.write(json.dumps(r) + "\n")
    return p


LEDGER_OK = [{"kind": "LIFECYCLE", "lifecycle": "PREFLIGHT"},
             {"kind": "INTENT", "verb": "pause"},
             {"kind": "OUTCOME", "verb": "pause", "outcome": "SENT"},
             {"kind": "OUTCOME", "verb": "restore", "outcome": "SENT"},
             {"kind": "OUTCOME", "verb": "restore_verified",
              "outcome": "RV_RESUMED"},
             {"kind": "LIFECYCLE", "lifecycle": "RESTORED"},
             {"kind": "CONSUMED", "result": "PASS"}]


def _journal_ok(d, aid, last="COMPLETE"):
    j = HO.Journal(path=HO.attempt_journal_path(d, aid), attempt_id=aid)
    j.append("INTENT", state="PAUSE_REQUESTED", step="PAUSE")
    j.append("OUTCOME", state="PAUSE_REQUESTED", step="PAUSE", ok=True)
    if last in ("COMPLETE", "RESTORED"):
        j.append("OUTCOME", state="RESTORE_REQUESTED", step="RESTORE", ok=True)
        j.append("STATE", state="RESTORED")
        if last == "COMPLETE":
            j.append("STATE", state="COMPLETE")
    else:
        j.append("STATE", state=last)
    return j.path


def _env(index_extra=(), sid=HOLD.TEST_SID, host=HOLD.TEST_HOST,
         manifest_files=(), exceptions=()):
    d = tmpdir()
    root = tmpdir()
    man = HOLD.write_test_manifest(os.path.join(root, "m.json"),
                                   manifest_files, exceptions)
    idx = HOLD.write_test_index(os.path.join(root, "i", "idx.jsonl"),
                                sid=sid, host=host, extra=index_extra)
    return d, man, idx


def _scan(d, man, idx, **kw):
    return HO.scan_prior_responsibility(d, manifest_path=man, index_path=idx,
                                        **kw)


def _blk(det):
    return [(x["source"], x["why"]) for x in det if x["blocking"]]


def test_G4():
    print("\n[G4] GAP-4 歷史清單 ／ runtime index ／ CLOSED 交叉驗證")
    check("G4-0 ★★ runtime index 固定路徑（原始碼常數）",
          'RUNTIME_INDEX_PATH = r"C:\\ProgramData\\Phase6FirstLive\\'
          'attempt_index.jsonl"' in io.open(HO.__file__,
                                            encoding="utf-8").read())
    check("G4-0b ★★ 正式 runtime index 不存在（本輪不建立、不初始化）",
          not os.path.exists(OFFICIAL_INDEX))
    import phase6_scheduled_first_live as _SC
    led = _SC.OneShotLedger(os.path.join(tmpdir(), "l.jsonl"))
    led.mark(_SC.LC_PREFLIGHT)
    led.mark_intent("pause")
    led.mark_outcome("pause", "SENT")
    led.consume("PASS")
    check("G4-0c LEDGER_KINDS 與 OneShotLedger 實際紀錄一致",
          {r["kind"] for r in led.records} <= set(HO.LEDGER_KINDS)
          and set(HO.LEDGER_PENDING_LIFECYCLE)
          == set(_SC.PENDING_RESTORE_LIFECYCLE))

    # ---- ① 歷史清單 ----
    d, man, idx = _env()
    check("G4-1 空清單 ＋ 空目錄 ＋ 已初始化 index -> CLEAR",
          _scan(d, man, idx)[0] == HO.PRIOR_CLEAR)
    for tag, path, body in (
            ("清單不存在", os.path.join(tmpdir(), "nope.json"), None),
            ("清單不是 JSON", os.path.join(tmpdir(), "bad.json"), "{broken"),
            ("清單 kind 錯誤", os.path.join(tmpdir(), "k.json"),
             json.dumps({"schema_version": 1, "kind": "X", "files": []}))):
        if body is not None:
            io.open(path, "w", encoding="utf-8").write(body)
        check("G4-2 ★★ %s -> UNRESOLVED" % tag,
              _scan(d, path, idx)[0] == HO.PRIOR_UNRESOLVED)
    d, man, idx = _env()
    lp = _ledger(d, "A-H", LEDGER_OK)
    jp = _journal_ok(d, "A-H")
    files = [{"path": os.path.basename(lp), "sha256": _sha(lp)},
             {"path": os.path.basename(jp), "sha256": _sha(jp)}]
    HOLD.write_test_manifest(man, files)
    check("G4-3 清單列出的檔案完整 -> CLEAR", _scan(d, man, idx)[0]
          == HO.PRIOR_CLEAR)
    with io.open(lp, "ab") as f:
        f.write(b" ")
    check("G4-3b ★★ 歷史檔被改一個位元組 -> UNRESOLVED（MANIFEST）",
          ("MANIFEST", "歷史檔 sha256 不符（被修改）") in _blk(_scan(d, man,
                                                                    idx)[1]))
    os.remove(lp)
    os.remove(jp)
    v, det = _scan(d, man, idx)
    check("G4-3c ★★ 目錄被清空（清單仍有紀錄）-> UNRESOLVED",
          v == HO.PRIOR_UNRESOLVED and len([b for b in _blk(det)
                                            if b[0] == "MANIFEST"]) == 2)

    # ---- 實際歷史（唯讀）----
    real, err = HO.load_history_manifest(REAL_MANIFEST)
    check("G4-4 正式歷史清單可載入且格式正確", real is not None and err is None)
    ex = {e["attempt_id"]: e for e in (real or {}).get("exceptions", [])}
    e3 = ex.get("FIRST-LIVE-ATTEMPT3-20260923") or {}
    e4 = ex.get("FIRST-LIVE-ATTEMPT4-20260924") or {}
    check("G4-4b ★★ #3 / #4 例外皆為 PENDING_USER_APPROVAL（未自動核准）",
          e3.get("approval") == HO.APPROVAL_PENDING
          and e4.get("approval") == HO.APPROVAL_PENDING
          and not e3.get("approved_by") and not e4.get("approved_by"))
    check("G4-4c ★★ #4 標記 INFERRED_CORRELATION，保留時間 / 次數 / 限制",
          e4.get("correlation") == "INFERRED_CORRELATION"
          and e4.get("times") and e4.get("counts", {}).get("ssh_pause") == 1
          and e4.get("counts", {}).get("ssh_restore") == 1
          and len(e4.get("limitations") or ()) >= 1)
    ev_ok = all(_sha(os.path.join(REAL_DIR, ev["path"])) == ev["sha256"]
                for e in (e3, e4) for ev in e.get("evidence", ()))
    check("G4-4d ★★ #3 / #4 全部證據 sha256 與實際檔案一致", ev_ok)
    d0, man0, idx0 = _env()
    v, det = HO.scan_prior_responsibility(REAL_DIR, manifest_path=REAL_MANIFEST,
                                          index_path=idx0)
    blk = sorted({x["attempt_id"] for x in det if x["blocking"]})
    check("G4-5 ★★ 實際歷史：只因 #3 / #4 未核准而阻擋；清單內檔案全部完整",
          v == HO.PRIOR_UNRESOLVED
          and blk == ["FIRST-LIVE-ATTEMPT3-20260923",
                      "FIRST-LIVE-ATTEMPT4-20260924"]
          and not [x for x in det if x["source"] == "MANIFEST"
                   and x["blocking"]])
    # 測試暫存副本：模擬「使用者核准後」的行為（正式清單維持 PENDING）
    sim = json.loads(io.open(REAL_MANIFEST, encoding="utf-8").read())
    for e in sim["exceptions"]:
        e.update(approval=HO.APPROVAL_APPROVED, approved_by="offline-test",
                 approved_at="2026-11-06")
    simp = os.path.join(tmpdir(), "sim_approved.json")
    io.open(simp, "w", encoding="utf-8").write(json.dumps(sim))
    check("G4-5b 暫存副本模擬核准 -> CLEAR（核准機制有效；正式清單未變更）",
          HO.scan_prior_responsibility(REAL_DIR, manifest_path=simp,
                                       index_path=idx0)[0] == HO.PRIOR_CLEAR
          and json.loads(io.open(REAL_MANIFEST, encoding="utf-8").read())[
              "exceptions"][1]["approval"] == HO.APPROVAL_PENDING)
    bad = json.loads(json.dumps(sim))
    bad["exceptions"][1]["evidence"][0]["sha256"] = "0" * 64
    badp = os.path.join(tmpdir(), "sim_bad_ev.json")
    io.open(badp, "w", encoding="utf-8").write(json.dumps(bad))
    check("G4-5c ★★ 已核准但證據 sha 不符 -> 例外不生效、阻擋",
          "FIRST-LIVE-ATTEMPT4-20260924" in {
              x["attempt_id"] for x in HO.scan_prior_responsibility(
                  REAL_DIR, manifest_path=badp, index_path=idx0)[1]
              if x["blocking"]})
    for tag, mut in (("APPROVED 沒有 approved_by",
                      lambda e: e.update(approved_by=None)),
                     ("缺少 evidence", lambda e: e.update(evidence=[])),
                     ("approval 值不合法",
                      lambda e: e.update(approval="AUTO"))):
        m2 = json.loads(json.dumps(sim))
        mut(m2["exceptions"][1])
        p2 = os.path.join(tmpdir(), "m2.json")
        io.open(p2, "w", encoding="utf-8").write(json.dumps(m2))
        check("G4-5d ★★ 例外格式錯誤（%s）-> 清單載入失敗" % tag,
              HO.load_history_manifest(p2)[0] is None)

    # ---- ② runtime index ----
    d, man, _i = _env()
    for tag, idx_fn, kw in (
            ("index 不存在", lambda: os.path.join(tmpdir(), "none.jsonl"), {}),
            ("SID 不符", lambda: _env(sid="S-1-5-21-9-9-9-9")[2], {}),
            ("主機名稱不符", lambda: _env(host="OTHER-HOST")[2], {}),
            ("ACL 含 Everyone", lambda: _env()[2],
             {"acl_reader": lambda p: [(HOLD.TEST_SID, "Allow"),
                                       ("S-1-1-0", "Allow")]}),
            ("ACL 讀不到", lambda: _env()[2], {"acl_reader": lambda p: None})):
        check("G4-6 ★★ %s -> UNRESOLVED" % tag,
              _scan(d, man, idx_fn(), **kw)[0] == HO.PRIOR_UNRESOLVED)
    for tag, body in (("index 損毀", '{"kind": "INDEX_INIT"\n'),
                      ("缺少 INDEX_INIT", json.dumps(
                          {"kind": "ATTEMPT_REGISTERED",
                           "attempt_id": "x"}) + "\n")):
        p = os.path.join(tmpdir(), "idx.jsonl")
        io.open(p, "w", encoding="utf-8").write(body)
        check("G4-6b ★★ %s -> UNRESOLVED" % tag,
              _scan(d, man, p)[0] == HO.PRIOR_UNRESOLVED)
    p = _env(index_extra=[{"kind": "INDEX_INIT", "sid": "x"}])[2]
    check("G4-6c 兩筆 INDEX_INIT -> UNRESOLVED",
          _scan(d, man, p)[0] == HO.PRIOR_UNRESOLVED)
    p = _env(index_extra=[{"kind": "SOMETHING", "attempt_id": "x"}])[2]
    check("G4-6d 未知紀錄種類 -> UNRESOLVED",
          _scan(d, man, p)[0] == HO.PRIOR_UNRESOLVED)

    # ---- ③ ATTEMPT_CLOSED 交叉驗證 ----
    def closed_case(mut_ledger=None, journal_last="COMPLETE", result="PASS",
                    pers=None, after=None, no_journal=False, ledger=None):
        d, man, _ = _env()
        lp = _ledger(d, "A-C", ledger or LEDGER_OK)
        jp = HO.attempt_journal_path(d, "A-C")
        if not no_journal:
            _journal_ok(d, "A-C", journal_last)
        closed = {"kind": "ATTEMPT_CLOSED", "attempt_id": "A-C",
                  "result": result, "ledger_sha256": _sha(lp),
                  "journal_sha256": (_sha(jp) if os.path.isfile(jp)
                                     else None),
                  "persistence": pers or {"journal": "OK", "ledger": "OK"}}
        idx = _env(index_extra=[
            {"kind": "ATTEMPT_REGISTERED", "attempt_id": "A-C",
             "ledger_path": lp, "journal_path": jp}, closed])[2]
        if after:
            after(d, lp, jp)
        return _scan(d, man, idx)
    check("G4-7 CLOSED 交叉驗證全部一致 -> CLEAR",
          closed_case()[0] == HO.PRIOR_CLEAR)
    cases = (
        ("CLOSED 後 ledger 被改", dict(after=lambda d, lp, jp: io.open(
            lp, "ab").write(b" "))),
        ("CLOSED 後 journal 被刪", dict(after=lambda d, lp, jp: os.remove(jp))),
        ("ledger 沒有 RV_RESUMED", dict(ledger=[r for r in LEDGER_OK
                                              if r.get("verb")
                                              != "restore_verified"])),
        ("journal 最後狀態 RESTORE_WITHHELD",
         dict(journal_last="RESTORE_WITHHELD")),
        ("ledger 持久化 FAILED", dict(pers={"journal": "OK",
                                          "ledger": "FAILED:OSError"})),
        ("journal 持久化 FAILED", dict(pers={"journal": "FAILED:OSError",
                                           "ledger": "OK"})),
        ("CLOSED 結果 RESTORE_WITHHELD", dict(result="RESTORE_WITHHELD")),
        ("ledger 未消耗", dict(ledger=[r for r in LEDGER_OK
                                     if r["kind"] != "CONSUMED"])),
        ("沒有 journal 但 ledger 有暫停責任", dict(no_journal=True)),
    )
    for tag, kw in cases:
        v, det = closed_case(**kw)
        check("G4-7b ★★ CLOSED 但 %s -> UNRESOLVED" % tag,
              v == HO.PRIOR_UNRESOLVED)
    d, man, _ = _env()
    idx = _env(index_extra=[{"kind": "ATTEMPT_CLOSED", "attempt_id": "A-Z",
                             "result": "PASS"}])[2]
    check("G4-7c ★★ CLOSED 沒有對應的 REGISTERED -> UNRESOLVED",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)
    for tag, extra in (("結果 RESTORE_WITHHELD", {"result": "RESTORE_WITHHELD"}),
                       ("持久化 FAILED", {"persistence": {
                           "ledger": "FAILED:OSError",
                           "journal": "FAILED:OSError"}})):
        d, man, _ = _env()
        c = dict({"kind": "ATTEMPT_CLOSED", "attempt_id": "A-N",
                  "result": "PASS", "ledger_sha256": None,
                  "journal_sha256": None,
                  "persistence": {"ledger": "NOT_ATTEMPTED"}}, **extra)
        idx = _env(index_extra=[{"kind": "ATTEMPT_REGISTERED",
                                 "attempt_id": "A-N",
                                 "ledger_path": os.path.join(d, "n.jsonl"),
                                 "journal_path": None}, c])[2]
        check("G4-7d ★★ CLOSED、ledger / journal 皆不存在但%s -> UNRESOLVED"
              % tag, _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)
    d, man, _ = _env()
    lp = _ledger(d, "A-R", LEDGER_OK)
    _journal_ok(d, "A-R")
    jp = HO.attempt_journal_path(d, "A-R")
    reg = {"kind": "ATTEMPT_REGISTERED", "attempt_id": "A-R",
           "ledger_path": os.path.join(d, "missing.jsonl"), "journal_path": jp}
    idx = _env(index_extra=[reg])[2]
    v, det = _scan(d, man, idx)
    check("G4-8 ★★ 已登錄未收尾且 ledger 不存在 -> UNRESOLVED",
          v == HO.PRIOR_UNRESOLVED)
    check("G4-8b 排除本次 attempt（runner 縱深防禦；ID ＋ ledger 檔案身分）"
          "-> 不因自身登錄而阻擋",
          _scan(d, man, idx, exclude_attempt_id="A-R",
                exclude_ledger_path=reg["ledger_path"])[0] == HO.PRIOR_CLEAR)
    closed = {"kind": "ATTEMPT_CLOSED", "attempt_id": "A-R", "result": "PASS",
              "ledger_sha256": _sha(lp), "journal_sha256": _sha(jp),
              "persistence": {"journal": "OK", "ledger": "OK"}}
    idx = _env(index_extra=[dict(reg, ledger_path=lp), closed,
                            dict(reg, ledger_path=os.path.join(d, "x.jsonl"))])[2]
    check("G4-8c ★★ CLOSED 之後又重新登錄（最後一筆是 REGISTERED）-> 未收尾判定",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)

    # ---- ④ 目錄內檔案 ----
    def dir_case(build):
        d, man, idx = _env()
        build(d)
        return _scan(d, man, idx)
    for tag, build in (
            ("空的 journal 檔", lambda d: io.open(
                HO.attempt_journal_path(d, "A-E"), "w").close()),
            ("ledger 有 pause INTENT 但沒有 journal",
             lambda d: _ledger(d, "A-L1", LEDGER_OK)),
            ("ledger pause SENT 但沒有 RV_RESUMED", lambda d: (
                _ledger(d, "A-L2", LEDGER_OK[:3] + LEDGER_OK[-1:]),
                _journal_ok(d, "A-L2"))),
            ("ledger 未消耗（執行中斷）", lambda d: _ledger(
                d, "A-L3", LEDGER_OK[:3])),
            ("ledger 損毀", lambda d: io.open(os.path.join(d, "A-L4.jsonl"),
                                            "w").write("{bad\n")),
            ("無法辨識的 jsonl", lambda d: io.open(
                os.path.join(d, "other.jsonl"), "w").write('{"a": 1}\n'))):
        check("G4-9 ★★ %s -> UNRESOLVED" % tag,
              dir_case(build)[0] == HO.PRIOR_UNRESOLVED)
    check("G4-9b ledger ＋ journal 一致收斂 -> CLEAR",
          dir_case(lambda d: (_ledger(d, "A-OK", LEDGER_OK),
                              _journal_ok(d, "A-OK")))[0] == HO.PRIOR_CLEAR)

    # ---- ⑤ C5：ledger / index 目標；歷史竄改不可確認 ----
    d, man, idx = _env()
    _ledger(d, "A-L1", LEDGER_OK)
    pe, ce = A10._ev()
    kw = dict(operator="op", reason="現場已確認 controller 由人工恢復並觀察正常",
              probe_evidence=pe, pcs_evidence=ce,
              confirm_phrase="RESOLVE A-L1", interactive=True,
              scan_kwargs={"manifest_path": man, "index_path": idx})
    ok, rec = HO.acknowledge_manual_resolution(
        HO.attempt_journal_path(d, "A-L1"), "A-L1",
        target_kind=HO.ACK_TARGET_LEDGER, **kw)
    check("G4-10 ★★ journal 缺失 -> 以 LEDGER 目標確認（綁定 ledger sha256）",
          ok and rec.get("target_kind") == HO.ACK_TARGET_LEDGER
          and rec.get("target_sha256") == _sha(os.path.join(d, "A-L1.jsonl")))
    check("G4-10b 確認後該 ledger 放行", _scan(d, man, idx)[0]
          == HO.PRIOR_CLEAR)
    with io.open(os.path.join(d, "A-L1.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"schema_version": 1, "kind": "LIFECYCLE",
                            "lifecycle": "PREFLIGHT"}) + "\n")
    check("G4-10c ★★ 確認後 ledger 又有新增 -> 確認失效",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)
    d, man, idx = _env()
    lp = _ledger(d, "A-M", LEDGER_OK)
    HOLD.write_test_manifest(man, [{"path": "A-M.jsonl", "sha256": "0" * 64}])
    ok, why = HO.acknowledge_manual_resolution(
        HO.attempt_journal_path(d, "A-M"), "A-M",
        target_kind=HO.ACK_TARGET_LEDGER,
        **dict(kw, confirm_phrase="RESOLVE A-M",
               scan_kwargs={"manifest_path": man, "index_path": idx}))
    v, det = _scan(d, man, idx)
    check("G4-10d ★★ 歷史檔被竄改不得以 C5 解除（MANIFEST 仍阻擋）",
          v == HO.PRIOR_UNRESOLVED
          and ("MANIFEST", "歷史檔 sha256 不符（被修改）") in _blk(det))

    # ---- ⑥ launcher：登錄 / 收尾 ----
    import test_phase6_scheduled_launcher as TL
    built = {"n": 0}
    ob = LAUNCH.build_dependencies
    orig_reg = FL.register_attempt

    def cb(*a, **k):
        built["n"] += 1
        return ob(*a, **k)

    def boom(*a, **k):
        raise OSError("index 寫入失敗（模擬）")
    dd = tmpdir()
    TL.write_provider(dd)
    cfg = TL.write_cfg(dd)
    LAUNCH.build_dependencies = cb
    FL.register_attempt = boom
    try:
        rc, summ, _o = A10.released(lambda: TL.run_in_process(cfg, dd))
    finally:
        LAUNCH.build_dependencies = ob
        FL.register_attempt = orig_reg
    check("G4-11 ★★ runtime index 登錄失敗 -> RUNTIME_INDEX_UNAVAILABLE、exit 4、"
          "build 0 次",
          rc == LAUNCH.EXIT_MANUAL and built["n"] == 0
          and (summ or {}).get("scheduled_result")
          == SC.R_RUNTIME_INDEX_UNAVAILABLE)
    dd = tmpdir()
    TL.write_provider(dd)
    cfg = TL.write_cfg(dd)
    rc, summ, _o = A10.released(lambda: TL.run_in_process(cfg, dd, mode="boom"))
    recs = HO._read_journal_strict(HO.RUNTIME_INDEX_PATH)[0] or []
    aid = (summ or {}).get("attempt_id")
    kinds = [r["kind"] for r in recs if r.get("attempt_id") == aid]
    check("G4-12 ★★ 登錄後 build 失敗 -> 仍寫 ATTEMPT_CLOSED（所有離開路徑都收尾）",
          rc == LAUNCH.EXIT_CONFIG_INVALID
          and kinds == [HO.INDEX_REGISTERED, HO.INDEX_CLOSED]
          and (summ or {}).get("runtime_index_closed") == HO.JP_OK)
    check("G4-12b 未進入執行的 attempt 收尾後不阻擋下一次",
          HO.scan_prior_responsibility()[0] == HO.PRIOR_CLEAR)

    # ---- ⑦ 初始化：只能人工、不覆寫 ----
    ip = os.path.join(tmpdir(), "pd", "attempt_index.jsonl")
    ident = {"sid": HOLD.TEST_SID, "hostname": HOLD.TEST_HOST}
    acl_ok = (lambda p: [(HOLD.TEST_SID, "Allow"), ("S-1-5-18", "Allow")])
    set_calls = []
    base_kw = dict(path=ip, identity=ident,
                   acl_setter=(lambda p, s: set_calls.append((p, s))),
                   acl_reader=acl_ok)
    for tag, args in (("非互動", ("op", "statement-long-enough",
                                  "INIT PHASE6 INDEX", False)),
                      ("確認語句錯", ("op", "statement-long-enough", "INIT",
                                      True))):
        check("G4-13 ★★ 初始化拒絕：%s（未建立檔案）" % tag,
              HO.init_runtime_index(*args, **base_kw)[0] is False
              and not os.path.exists(ip))
    ok, rec = HO.init_runtime_index("op", "statement-long-enough",
                                    "INIT PHASE6 INDEX", True, **base_kw)
    check("G4-13b 人工初始化成功：綁定 SID / 主機名稱並設定 ACL",
          ok and rec["sid"] == HOLD.TEST_SID and set_calls == [(ip,
                                                                HOLD.TEST_SID)])
    check("G4-13c ★★ 已存在 -> 拒絕（不覆寫）",
          HO.init_runtime_index("op", "statement-long-enough",
                                "INIT PHASE6 INDEX", True, **base_kw)[0]
          is False)
    ip2 = os.path.join(tmpdir(), "pd2", "attempt_index.jsonl")
    ok2, why2 = HO.init_runtime_index(
        "op", "statement-long-enough", "INIT PHASE6 INDEX", True,
        **dict(base_kw, path=ip2,
               acl_reader=lambda p: [("S-1-1-0", "Allow")]))
    check("G4-13d ACL 驗證失敗 -> 回報失敗（C4 會持續阻擋）",
          ok2 is False and _scan(tmpdir(), _env()[1], ip2,
                                 acl_reader=lambda p: [("S-1-1-0",
                                                        "Allow")])[0]
          == HO.PRIOR_UNRESOLVED)
    import phase6_manual_resolution as MR

    class _NoTty(object):
        def isatty(self):
            return False
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()), \
            contextlib.redirect_stdout(io.StringIO()):
        rc = MR.main(["init-index", "--operator", "x", "--statement",
                      "y" * 20], stdin=_NoTty(),
                     prompt=(lambda _p: "INIT PHASE6 INDEX"))
    check("G4-13e CLI init-index 非互動 -> 拒絕（return 2）", rc == 2)

    # ---- ⑧ AST：沒有自動初始化 / 正式呼叫點；scan 不寫檔 ----
    import ast
    import glob as _g
    calls = []
    for p in sorted(_g.glob(os.path.join(HERE, "*.py"))):
        b = os.path.basename(p)
        if b.startswith("test_") or b.startswith("_phase6") \
                or b == "phase6_manual_resolution.py":
            continue
        try:
            t2 = ast.parse(io.open(p, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(t2):
            if isinstance(n, ast.Call):
                nm = getattr(n.func, "attr", None) or getattr(n.func, "id",
                                                              None)
                if nm in ("init_runtime_index", "acknowledge_manual_resolution",
                          "write_test_index"):
                    calls.append("%s:%s" % (b, nm))
    check("G4-14 ★★ 正式程式沒有 init_runtime_index / C5 呼叫點：%r" % (calls,),
          calls == [])
    tree = ast.parse(io.open(HO.__file__, encoding="utf-8").read())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    names = set()
    for nm in ("scan_prior_responsibility", "load_runtime_index",
               "load_history_manifest", "_closed_cross_check",
               "evaluate_ledger_responsibility"):
        for n in ast.walk(fns[nm]):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    check("G4-14b ★★ C4 掃描沒有寫檔 / 初始化 / restore 路徑",
          not ({"_index_append", "init_runtime_index", "makedirs", "write",
                "request_restore", "send_restore_ex"} & names))


# ======================================================================
# GA — 缺陷 A：INDEX_ENTRY 確認
# ======================================================================
def _reg(aid, lp, jp=None):
    return {"kind": HO.INDEX_REGISTERED, "attempt_id": aid,
            "ledger_path": lp, "journal_path": jp}


def _ack_index(d, aid, man, idx):
    pe, ce = A10._ev()
    return HO.acknowledge_manual_resolution(
        HO.attempt_journal_path(d, aid), aid, operator="op",
        reason="現場已確認 controller 由人工恢復並觀察正常",
        probe_evidence=pe, pcs_evidence=ce,
        confirm_phrase="RESOLVE %s" % aid, interactive=True,
        target_kind=HO.ACK_TARGET_INDEX_ENTRY,
        scan_kwargs={"manifest_path": man, "index_path": idx})


def _items(det, aid):
    return [x for x in det if x["attempt_id"] == aid]


def _rewrite_index(idx, fn):
    recs = HO._read_journal_strict(idx)[0]
    with io.open(idx, "w", encoding="utf-8") as f:
        for r in fn(recs):
            f.write(json.dumps(r) + "\n")


def test_GA():
    print("\n[GA] 缺陷 A：INDEX_ENTRY 確認綁定該 attempt 的紀錄")

    def setup():
        d, man, _ = _env()
        lp = os.path.join(d, "missing-A-X.jsonl")      # ledger 不存在 -> 阻擋
        idx = _env(index_extra=[_reg("A-X", lp)])[2]
        return d, man, idx, lp
    d, man, idx, lp = setup()
    check("GA-0 前提：已登錄未收尾且 ledger 不存在 -> UNRESOLVED",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)
    ok, rec = _ack_index(d, "A-X", man, idx)
    check("GA-1 ★★ INDEX_ENTRY 確認綁定 attempt digest（不是整份 index sha256）",
          ok and rec.get("target_digest_scheme") == HO.INDEX_ENTRY_DIGEST_SCHEME
          and rec.get("target_sha256") != _sha(idx)
          and HO._hex64(rec.get("target_sha256")))
    check("GA-1b 確認後 -> CLEAR", _scan(d, man, idx)[0] == HO.PRIOR_CLEAR)
    mrp = HO.manual_resolution_path(d, "A-X")
    ack_bytes = io.open(mrp, "rb").read()

    # 其他 attempt 追加登錄（未進入執行即收尾）-> 既有確認仍有效
    HO._index_append(_reg("A-Y", os.path.join(d, "missing-A-Y.jsonl")), idx)
    HO._index_append({"kind": HO.INDEX_CLOSED, "attempt_id": "A-Y",
                      "result": "CONFIG_INVALID", "ledger_sha256": None,
                      "journal_sha256": None,
                      "persistence": {"ledger": "NOT_ATTEMPTED"}}, idx)
    v, det = _scan(d, man, idx)
    check("GA-2 ★★ 其他 attempt 新增登錄 / 收尾 -> A-X 的確認仍有效、整體 CLEAR",
          v == HO.PRIOR_CLEAR
          and all(x["resolved_by_ack"] for x in _items(det, "A-X")))
    HO._index_append(_reg("A-Z", os.path.join(d, "missing-A-Z.jsonl")), idx)
    v, det = _scan(d, man, idx)
    check("GA-2b 其他 attempt 未收尾 -> 只有該 attempt 阻擋，A-X 確認仍有效",
          v == HO.PRIOR_UNRESOLVED
          and sorted({x["attempt_id"] for x in det if x["blocking"]})
          == ["A-Z"]
          and all(x["resolved_by_ack"] for x in _items(det, "A-X")))
    check("GA-2c 其他 attempt 追加與多次掃描後，確認紀錄位元組完全不變",
          io.open(mrp, "rb").read() == ack_bytes)

    # 同一 attempt 新增責任相關紀錄 -> 確認失效
    d, man, idx, lp = setup()
    _ack_index(d, "A-X", man, idx)
    HO._index_append(_reg("A-X", lp), idx)
    v, det = _scan(d, man, idx)
    check("GA-3 ★★ 同一 attempt 又新增登錄 -> 原確認失效、UNRESOLVED",
          v == HO.PRIOR_UNRESOLVED
          and any(x["blocking"] for x in _items(det, "A-X")))
    d, man, idx, lp = setup()
    _ack_index(d, "A-X", man, idx)
    HO._index_append({"kind": HO.INDEX_CLOSED, "attempt_id": "A-X",
                      "result": "RESTORE_WITHHELD", "ledger_sha256": None,
                      "journal_sha256": None, "persistence": {}}, idx)
    check("GA-3b ★★ 同一 attempt 追加 CLOSED（後續紀錄）-> 原確認失效",
          any(x["blocking"] and not x["resolved_by_ack"]
              for x in _items(_scan(d, man, idx)[1], "A-X")))
    d, man, idx, lp = setup()
    _ack_index(d, "A-X", man, idx)
    _ledger(os.path.dirname(lp), "missing-A-X", LEDGER_OK[:3])
    check("GA-3c ★★ 確認後該 attempt 的 ledger 出現 / 變動 -> 原確認失效",
          any(x["blocking"] and x["source"] == "INDEX"
              for x in _items(_scan(d, man, idx)[1], "A-X")))

    # 目標紀錄被修改 / 刪除 / 損毀 -> UNRESOLVED
    d, man, idx, lp = setup()
    _ack_index(d, "A-X", man, idx)
    _rewrite_index(idx, lambda rs: [dict(r, journal_path="X")
                                    if r.get("attempt_id") == "A-X" else r
                                    for r in rs])
    check("GA-4 ★★ 目標紀錄被修改 -> UNRESOLVED",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)
    d, man, idx, lp = setup()
    _ack_index(d, "A-X", man, idx)
    _rewrite_index(idx, lambda rs: [r for r in rs
                                    if r.get("attempt_id") != "A-X"])
    v, det = _scan(d, man, idx)
    check("GA-5 ★★ 目標紀錄整組被刪除 -> UNRESOLVED（確認目標已不存在）",
          v == HO.PRIOR_UNRESOLVED
          and any(x["blocking"] and "已不存在" in x["why"]
                  for x in _items(det, "A-X")))
    d, man, idx, lp = setup()
    _ack_index(d, "A-X", man, idx)
    with io.open(idx, "a", encoding="utf-8") as f:
        f.write("{broken\n")
    check("GA-5b ★★ index 損毀 -> UNRESOLVED",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)

    # 舊版（整份 index sha256）確認不再生效；既有確認紀錄不被修改
    d, man, idx, lp = setup()
    legacy = {"kind": HO.MANUAL_RESOLUTION_KIND, "attempt_id": "A-X",
              "target_kind": HO.ACK_TARGET_INDEX_ENTRY,
              "target_sha256": _sha(idx), "operator": "legacy"}
    with io.open(HO.manual_resolution_path(d, "A-X"), "w",
                 encoding="utf-8") as f:
        f.write(json.dumps(legacy) + "\n")
    check("GA-6 ★★ 舊版整份 index sha256 的確認 -> 不生效（UNRESOLVED）",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)
    d, man, idx, lp = setup()
    _ack_index(d, "A-X", man, idx)
    mrp = HO.manual_resolution_path(d, "A-X")
    b0 = io.open(mrp, "rb").read()
    _scan(d, man, idx)
    HO._index_append(_reg("A-X", lp), idx)
    _scan(d, man, idx)
    ok2, _r2 = _ack_index(d, "A-X", man, idx)
    b1 = io.open(mrp, "rb").read()
    check("GA-6b ★★ 掃描不改確認紀錄；重新確認只追加（既有紀錄位元組不變）",
          ok2 and b1.startswith(b0) and len(b1) > len(b0)
          and _scan(d, man, idx)[0] == HO.PRIOR_CLEAR)


# ======================================================================
# GB — 缺陷 B：runner 排除以 ledger 實際檔案身分判定
# ======================================================================
def test_GB():
    print("\n[GB] 缺陷 B：排除以 ledger 實際檔案身分判定")
    d, man, _ = _env()
    lp = os.path.join(d, "A-R.jsonl")
    idx = _env(index_extra=[_reg("A-R", lp)])[2]
    check("GB-1 ★★ 只給 attempt_id（不給 ledger）-> 不排除任何項目",
          _scan(d, man, idx, exclude_attempt_id="A-R")[0]
          == HO.PRIOR_UNRESOLVED)
    variant = os.path.join(d, "sub", "..", "a-r.JSONL").upper()
    check("GB-2 ★★ ledger 尚未建立：ID ＋ 正規化後同一路徑 -> 排除",
          _scan(d, man, idx, exclude_attempt_id="A-R",
                exclude_ledger_path=variant)[0] == HO.PRIOR_CLEAR)
    led = SC.OneShotLedger(lp)
    led.mark(SC.LC_PREFLIGHT)
    led.mark_intent("pause")
    led.mark(SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY)
    check("GB-2b 前提：本次 ledger 在責任狀態，不排除 -> UNRESOLVED",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)
    check("GB-3 ★★ 相同檔案不同路徑寫法（大小寫 / .. / 正規化）-> 排除",
          _scan(d, man, idx, exclude_attempt_id="A-R",
                exclude_ledger_path=variant)[0] == HO.PRIOR_CLEAR)
    check("GB-3b 相同 ID、ledger 指向不存在的其他檔 -> 不排除",
          _scan(d, man, idx, exclude_attempt_id="A-R",
                exclude_ledger_path=os.path.join(tmpdir(), "A-R.jsonl"))[0]
          == HO.PRIOR_UNRESOLVED)
    check("GB-3c 不同 ID、同一 ledger 檔 -> 不排除",
          _scan(d, man, idx, exclude_attempt_id="A-OTHER",
                exclude_ledger_path=lp)[0] == HO.PRIOR_UNRESOLVED)

    # 相同 ID、不同 ledger：舊的 ledger-only 阻擋必須仍被抓到
    d, man, idx = _env()
    _ledger(d, "A-S", LEDGER_OK[:3])                 # 舊：未消耗、有暫停責任
    cur = os.path.join(tmpdir(), "A-S.jsonl")        # 本次：另一個檔案
    v, det = _scan(d, man, idx, exclude_attempt_id="A-S",
                   exclude_ledger_path=cur)
    check("GB-4 ★★ 相同 ID 不同 ledger：舊 ledger-only 阻擋仍 UNRESOLVED",
          v == HO.PRIOR_UNRESOLVED
          and any(x["source"] == "LEDGER" and x["blocking"]
                  and x["attempt_id"] == "A-S" for x in det))
    d, man, _ = _env()
    old = os.path.join(d, "old", "A-S.jsonl")
    idx = _env(index_extra=[_reg("A-S", old), _reg("A-S", cur)])[2]
    v, det = _scan(d, man, idx, exclude_attempt_id="A-S",
                   exclude_ledger_path=cur)
    check("GB-5 ★★ index 中同一 ID 登錄了不同 ledger -> 不排除、UNRESOLVED",
          v == HO.PRIOR_UNRESOLVED
          and any("不同的 ledger" in x["why"] for x in det if x["blocking"]))
    check("GB-5b 主要檢查點（不排除）同樣 UNRESOLVED",
          _scan(d, man, idx)[0] == HO.PRIOR_UNRESOLVED)

    # ---- scheduled runner 整合 ----
    import test_phase6_scheduled_first_live as TS
    aid = TS.auth().attempt_id
    pd = tmpdir()
    _ledger(pd, aid, LEDGER_OK[:3])                  # 舊的同 ID ledger（阻擋）
    rig = TS.Rig(TS.INSIDE, ledger_path=os.path.join(tmpdir(),
                                                     "%s.jsonl" % aid))
    ev = A10.released(lambda: rig.runner(prior_journal_dir=pd).run())
    check("GB-6 ★★ runner：同 ID、不同 ledger 的舊阻擋 -> "
          "PRIOR_RESPONSIBILITY_UNRESOLVED、handoff 0、不消耗",
          ev["result"] == SC.R_PRIOR_RESPONSIBILITY_UNRESOLVED
          and rig.handoff_calls == 0 and ev["one_shot_consumed"] is False)
    pd = tmpdir()
    rig = TS.Rig(TS.INSIDE, ledger_path=os.path.join(pd, "%s.jsonl" % aid))
    rig.ledger.mark(SC.LC_PREFLIGHT)
    rig.ledger.mark_intent("pause")
    rig.ledger.mark(SC.LC_EXTERNAL_PAUSE_RESPONSIBILITY)
    ev = A10.released(lambda: rig.runner(prior_journal_dir=pd).run())
    check("GB-7 runner：本次 ledger（同一檔案）由 recover() 判定，"
          "不被 C4 誤擋；handoff 不重跑（%s）" % ev["result"],
          ev["result"] != SC.R_PRIOR_RESPONSIBILITY_UNRESOLVED
          and rig.handoff_calls == 0)
    import inspect
    src = inspect.getsource(SC.ScheduledFirstLiveRunner._run_inner)
    check("GB-8 runner 呼叫 C4 時傳入本次 ledger 實際路徑",
          "exclude_ledger_path=getattr(self.ledger, \"path\", None)" in src)


# ======================================================================
# GU — GAP-3 U-1：restore 送出前的 journal 寫入失敗
# ======================================================================
class _PickyJournal(HO.Journal):
    """符合條件的那一筆（之後可選擇持續）寫入拋 OSError。"""

    def __init__(self, path, attempt_id, match, sticky):
        HO.Journal.__init__(self, path=path, attempt_id=attempt_id)
        self.match = match
        self.sticky = sticky
        self.failing = False
        self.failures = 0

    def append(self, kind, state=None, **kw):
        if self.failing or self.match(kind, state, kw):
            self.failing = self.sticky
            self.failures += 1
            raise OSError("disk full（模擬）")
        return HO.Journal.append(self, kind, state=state, **kw)


def _u1_run(aid, match, sticky):
    d = tmpdir()
    h = A10.Harness()
    h.o.journal = _PickyJournal(HO.attempt_journal_path(d, aid), aid,
                                match, sticky)
    A10.released(h.run)
    return d, h


def test_GU():
    print("\n[GU] GAP-3 U-1：RESTORE_REQUESTED / INTENT journal 寫入失敗"
          "（HOLD 解除）")
    d0, man0, idx0 = _env()

    def c4(d):
        return _scan(d, man0, idx0)
    cases = (
        ("RESTORE_REQUESTED 寫入失敗（持續）",
         lambda k, s, kw: s == HO.S_RESTORE_REQUESTED, True),
        ("RESTORE_REQUESTED 寫入失敗（暫時）",
         lambda k, s, kw: s == HO.S_RESTORE_REQUESTED, False),
        ("RESTORE INTENT 寫入失敗（持續）",
         lambda k, s, kw: k == "INTENT" and kw.get("step") == "RESTORE", True),
        ("RESTORE INTENT 寫入失敗（暫時）",
         lambda k, s, kw: k == "INTENT" and kw.get("step") == "RESTORE", False))
    results = []
    for i, (tag, match, sticky) in enumerate(cases):
        aid = "A-U1-%d" % i
        d, h = _u1_run(aid, match, sticky)
        results.append((tag, d, h))
        recs = HO.Journal(path=h.o.journal.path).read_all()
        check("GU-1 ★★ %s -> restore sender 0 次" % tag,
              h.sd.restore_calls == 0 and h.sd.pause_calls == 1)
        check("GU-2 ★★ %s -> WITHHELD、責任保留" % tag, withheld(h))
        check("GU-3 ★★ %s -> journal_persistence = FAILED:OSError"
              "（不得隱藏寫入失敗）" % tag,
              h.res["journal_persistence"] == "FAILED:OSError")
        check("GU-3b %s -> 實際 journal 沒有 RESTORE INTENT、"
              "與回報一致" % tag,
              not [r for r in recs if r.get("kind") == "INTENT"
                   and r.get("step") == "RESTORE"]
              and h.o.guard.may_attempt("RESTORE"))
        wh = [r for r in recs if r.get("state") == HO.S_RESTORE_WITHHELD]
        check("GU-3c %s -> WITHHELD 是否落盤與實際一致（%s）"
              % (tag, "已落盤" if wh else "未落盤"),
              bool(wh) == (not sticky))
        v, det = c4(d)
        check("GU-4 ★★ %s -> C4 阻擋下一次 Attempt（journal 責任未解除）" % tag,
              v == HO.PRIOR_UNRESOLVED
              and any(x["source"] == "JOURNAL" and x["blocking"]
                      and x["attempt_id"] == aid for x in det))

    # 已標記 attempted 之後的例外（可能已送出）-> 不得宣稱未送出
    d = tmpdir()
    h = A10.Harness()
    h.o.journal = _PickyJournal(
        HO.attempt_journal_path(d, "A-U1-X"), "A-U1-X",
        lambda k, s, kw: k == "OUTCOME" and kw.get("step") == "RESTORE", True)
    raised = None
    try:
        A10.released(h.run)
    except OSError as e:
        raised = e
    check("GU-5 ★★ restore OUTCOME 寫入失敗（已送出）-> 維持往上拋，"
          "不改判 WITHHELD",
          raised is not None and h.sd.restore_calls == 1
          and h.o.state != HO.S_RESTORE_WITHHELD)

    # scheduled runner：分類與持久化一致，下一次 C4 阻擋
    import test_phase6_scheduled_first_live as TS
    tag, jd, hres = results[2][0], results[2][1], results[2][2].res
    aid = TS.auth().attempt_id
    pd = tmpdir()
    jsrc = HO.Journal(path=results[2][2].o.journal.path).read_all()
    jj = HO.Journal(path=HO.attempt_journal_path(pd, aid), attempt_id=aid)
    for r in jsrc:
        r = dict(r)
        k = r.pop("kind")
        st = r.pop("state", None)
        for drop in ("seq", "ts", "at", "attempt_id"):
            r.pop(drop, None)
        jj.append(k, state=st, **r)
    art = TS.artifact(handoff_outcome="RESTORE_WITHHELD",
                      restore_attempt_count=0, restore_send_outcome=None,
                      restore_resumed=False, restore_verifier_state=None,
                      restore_responsibility_final=True)
    rig = TS.Rig(TS.INSIDE, art=art,
                 ledger_path=os.path.join(pd, "%s.jsonl" % aid))
    rig.handoff = (lambda _h=rig.handoff: (_h(), hres)[1])
    ev = A10.released(lambda: rig.runner(prior_journal_dir=tmpdir()).run())
    check("GU-6 ★★ scheduled：結果 RESTORE_WITHHELD、journal FAILED:OSError、"
          "整體 PARTIAL（%s / %s）" % (ev["result"],
                                       ev["persistence"]["overall"]),
          ev["result"] == SC.R_RESTORE_WITHHELD
          and ev["persistence"]["detail"]["journal"] == "FAILED:OSError"
          and ev["persistence"]["overall"] == HO.PERSISTED_PARTIAL)
    v, det = _scan(pd, man0, idx0)
    check("GU-7 ★★ scheduled：下一次 Attempt 的 C4 被本次 ledger / journal 阻擋",
          v == HO.PRIOR_UNRESOLVED
          and {"LEDGER", "JOURNAL"} <= {x["source"] for x in det
                                        if x["blocking"]
                                        and x["attempt_id"] == aid})
    check("GU-8 rest state 維持", rest_ok())


TESTS = [test_G3, test_G2, test_G6, test_G5, test_G4, test_GA, test_GB,
         test_GU]


def _output_snapshot():
    out = {}
    for dp, _dn, fn in os.walk(REAL_DIR):
        for f in fn:
            p = os.path.join(dp, f)
            out[os.path.relpath(p, REAL_DIR)] = (HO._sha256_file(p),
                                                 os.path.getmtime(p))
    return out


def main():
    print("=" * 72)
    print("  Phase 6 Attempt #10 安全缺口修正（GAP-2～GAP-6）—— 離線")
    print("=" * 72)
    snap0 = _output_snapshot()
    pd0 = os.path.exists(os.path.dirname(OFFICIAL_INDEX))
    try:
        # 🔴 GAP-4：C4 需要歷史清單與 runtime index -> 一律在暫存環境執行
        with HOLD.offline_c4_env():
            for fn in TESTS:
                fn()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    print("\n[GUARD] 正式資料未被修改")
    check("GUARD-1 ★★ output/phase6_scheduled_first_live 全部檔案 sha256 / mtime "
          "與測試前相同（%d 個）" % len(snap0), _output_snapshot() == snap0)
    check("GUARD-2 ★★ 正式 runtime index 目錄未被建立（測試前 %r）" % pd0,
          pd0 is False and not os.path.exists(os.path.dirname(OFFICIAL_INDEX)))
    n, tot = sum(RESULTS), len(RESULTS)
    print("=" * 72)
    print("  結果：%d/%d %s" % (n, tot, "PASS" if n == tot else "FAIL"))
    print("  rest state：HO=%r RT=%r  FIRST_LIVE_HOLD=%r"
          % (HO.DISPATCH_ENABLED, RT.DISPATCH_ENABLED, HO.FIRST_LIVE_HOLD))
    print("=" * 72)
    return 0 if (n == tot and rest_ok() and HO.FIRST_LIVE_HOLD is True) else 1


if __name__ == "__main__":
    sys.exit(main())
