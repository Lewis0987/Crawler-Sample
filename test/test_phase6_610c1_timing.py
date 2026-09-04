# -*- coding: utf-8 -*-
"""
test_phase6_610c1_timing.py — Phase 6.10-C1 Service Timing（候選值驗證）
======================================================================
核心命題
    「`retry_backoff_sec` 與 `service_health_timeout_sec` 是兩件不同的事：
      前者決定『失敗後多久再讀一次』，後者決定『多久沒有任何進展就把服務
      判成健康未知』。兩個候選值都必須落在既有 production 契約推導出的
      區間內，且無論怎麼逾時、怎麼重試，都不得自己送出任何指令。」

已核准值（Phase 6.10-C1 裁示，2026-09-03）
    retry_backoff_sec          = 5.0   FINAL
    service_health_timeout_sec = 300.0 FINAL

    本檔仍以 config injection 逐項驗證邊界行為；
    另驗 `ServiceTiming()` 的預設值確實是這兩個已核准常數，
    且「注入 None」的 Fail Closed 路徑沒有被拿掉。

    🔴 參數定案 **不等於** live 啟用：DISPATCH_ENABLED 仍為 False。

是否需要設備
    **不需要**。clock / sleeper / sample / probe / sender / dispatcher
    全部注入。零 SSH、零實機 command。

用法
    python test_phase6_610c1_timing.py        # exit 0 = PASS
"""
import io
import os
import sys
import ast
import inspect
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import decision_engine as DE                     # noqa: E402
import meter_client as MC                        # noqa: E402
import power_classifier as PC                    # noqa: E402
import phase6_handoff_orchestrator as HO         # noqa: E402
import phase6_unattended_service as SVC          # noqa: E402

RESULTS = []
_TMP = []

# 已核准值（與 production 常數對照驗證，見 test_12）
RETRY_BACKOFF_SEC_CANDIDATE = 5.0
SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE = 300.0

DECISION_INTERVAL = 30.0


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def tmpdir():
    d = tempfile.mkdtemp(prefix="p610c1_")
    _TMP.append(d)
    return d


class Clock(object):
    """可注入的假時鐘。"""

    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)
        return self.t


class Sleeper(object):
    """記錄每次 sleep，並同步推進假時鐘 —— 這樣 busy-loop 無所遁形。"""

    def __init__(self, clock=None):
        self.calls = []
        self.clock = clock

    def __call__(self, sec):
        self.calls.append(float(sec))
        if self.clock is not None:
            self.clock.advance(sec)

    @property
    def total(self):
        return sum(self.calls)


class Senders(object):
    def __init__(self):
        self.pause_calls = self.restore_calls = 0

    def pause(self):
        self.pause_calls += 1
        return True

    def restore(self):
        self.restore_calls += 1
        return True


def timing(retry=None, health=None):
    return SVC.ServiceTiming(decision_interval_sec=DECISION_INTERVAL,
                             retry_backoff_sec=retry,
                             service_health_timeout_sec=health)


def probe(**over):
    p = {"screen_alive": True, "process_alive": True,
         "process_identity_ok": True, "controller_running": True}
    p.update(over)
    return p


def smp(**over):
    s = {"pcs_state": "STANDBY", "ac_kw": -1.3, "authority": "IDLE",
         "comm_ok": True, "fault": False, "critical_alarms": 0, "fresh": True,
         "soc": 5.0, "soc_fresh": True, "meter_fresh": True, "ess_fresh": True,
         "tou": "OFF_PEAK", "decision": "charge",
         "external_behaviour_known": True}
    s.update(over)
    return s


def service(sample_fn, probe_fn, clock, sleeper, retry=None, health=None,
            mode=HO.MODE_DRY_RUN, dispatcher=None):
    d = tmpdir()
    sd = Senders()
    g = SVC.SingleInstanceGuard(os.path.join(d, "svc.lock"), pid=2222,
                                boot_id="B1", cmdline="phase6",
                                is_alive=(lambda p: False))
    s = SVC.UnattendedService(
        journal=HO.Journal(path=os.path.join(d, "j.jsonl")),
        instance_guard=g, sample_source=sample_fn, probe_source=probe_fn,
        loop_mark_source=(lambda: "2026-09-03 03:00:00"),
        senders=sd, dispatcher=dispatcher, mode=mode, soc_max_pct=85.0,
        timing=timing(retry, health), sleeper=sleeper, clock=clock)
    return s, sd


# ======================================================================
# 1. 兩種 timeout 不得混為一談
# ======================================================================
def test_1_not_conflated():
    print("\n[1] retry_backoff 與 service_health_timeout 是兩件事")
    check("  兩者由不同類別實作（RetryPolicy / ServiceHealth）",
          SVC.RetryPolicy is not SVC.ServiceHealth)
    check("  RetryPolicy 不持有任何 health 概念",
          not any("health" in a.lower() for a in
                  inspect.signature(SVC.RetryPolicy.__init__).parameters))
    check("  ServiceHealth 不持有任何 retry / backoff 概念",
          not any(("retry" in a.lower() or "backoff" in a.lower()) for a in
                  inspect.signature(SVC.ServiceHealth.__init__).parameters))
    check("  ServiceTiming 兩個欄位彼此獨立、可分別注入",
          timing(retry=5.0).service_health_timeout_sec is None
          and timing(health=300.0).retry_backoff_sec is None)
    # 兩者的界線區間完全不重疊 —— 值域上就不可能互相冒充
    check("★★ 兩者的合法區間完全不重疊（3~10 vs 210~600）",
          SVC.RETRY_BACKOFF_UPPER_BOUND_SEC
          < SVC.SERVICE_HEALTH_TIMEOUT_LOWER_BOUND_SEC)
    check("★★ 候選值互換必定雙雙落在區間外",
          not SVC.retry_backoff_in_contract(
              SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
          and not SVC.service_health_timeout_in_contract(
              RETRY_BACKOFF_SEC_CANDIDATE))


# ======================================================================
# 2. 契約邊界本身要對得上既有 production 常數
# ======================================================================
def test_2_bounds_traceable():
    print("\n[2] 界線值可回溯到既有 production 契約，不是憑空取的")
    check(f"  retry 下限 3.0 = Meter STALE_AFTER_SEC（{MC.STALE_AFTER_SEC}）",
          SVC.RETRY_BACKOFF_LOWER_BOUND_SEC == MC.STALE_AFTER_SEC)
    check(f"  retry 下限 3.0 = PowerClassifier debounce"
          f"（{PC.DEFAULT_CONFIG.debounce_sec}）",
          SVC.RETRY_BACKOFF_LOWER_BOUND_SEC == PC.DEFAULT_CONFIG.debounce_sec)
    check(f"  retry 上限 10.0 = ESS_READ_DURATION_MAX_SEC"
          f"（{DE.ESS_READ_DURATION_MAX_SEC}）",
          SVC.RETRY_BACKOFF_UPPER_BOUND_SEC == DE.ESS_READ_DURATION_MAX_SEC)
    check(f"  retry 上限 < ESS stale_after（{DE.ESS_STALE_AFTER_SEC}）",
          SVC.RETRY_BACKOFF_UPPER_BOUND_SEC < DE.ESS_STALE_AFTER_SEC)

    legit = (HO.HandoffConfig.pause_settling_timeout_sec
             + HO.HandoffConfig.stable_verify_min_span_sec
             + DECISION_INTERVAL)
    check(f"  最長合法無里程碑期間 = 120+45+30 = {legit:.0f} s", legit == 195.0)
    check("★★ health 下限 210 > 最長合法無里程碑期間 195",
          SVC.SERVICE_HEALTH_TIMEOUT_LOWER_BOUND_SEC > legit)
    check("  health 下限為 decision_interval 的整數倍（7×30）",
          SVC.SERVICE_HEALTH_TIMEOUT_LOWER_BOUND_SEC % DECISION_INTERVAL == 0)
    check("  health 上限 600 s = 現場 SOC 約 5%/10 分鐘的那 10 分鐘",
          SVC.SERVICE_HEALTH_TIMEOUT_UPPER_BOUND_SEC == 600.0)


# ======================================================================
# 3. 候選值落在區間內；區間外一律拒絕
# ======================================================================
def test_3_candidates():
    print("\n[3] 候選值 5.0 / 300.0")
    check("★★ RETRY_BACKOFF 候選 5.0 在契約區間內",
          SVC.retry_backoff_in_contract(RETRY_BACKOFF_SEC_CANDIDATE))
    check("★★ SERVICE_HEALTH_TIMEOUT 候選 300.0 在契約區間內",
          SVC.service_health_timeout_in_contract(
              SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE))
    check("  300 = decision_interval 的整數倍（10×30）",
          SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE % DECISION_INTERVAL == 0)
    for bad in (0.0, 0.5, 1.0, 2.9, 10.1, 15.0, 30.0):
        check(f"  retry {bad} 被拒（超出 3~10）",
              not SVC.retry_backoff_in_contract(bad))
    for bad in (30.0, 120.0, 195.0, 209.9, 601.0, 3600.0):
        check(f"  health {bad} 被拒（超出 210~600）",
              not SVC.service_health_timeout_in_contract(bad))
    check("★★ None 一律不算合格（未定案 = Fail Closed）",
          not SVC.retry_backoff_in_contract(None)
          and not SVC.service_health_timeout_in_contract(None))


# ======================================================================
# 4. retry 不 busy-loop
# ======================================================================
def test_4_no_busy_loop():
    print("\n[4] retry 不 busy-loop")
    clk = Clock()
    slp = Sleeper(clk)
    p = SVC.RetryPolicy(RETRY_BACKOFF_SEC_CANDIDATE, DECISION_INTERVAL,
                        sleeper=slp)
    calls = []
    v, n, waits = p.run(lambda: calls.append(1) or None)   # 永遠失敗
    check(f"  嘗試 {n} 次後停止（上限 {p.max_attempts()}）",
          n == p.max_attempts())
    check("★★ 每一次重試之前都有 sleep（次數 = 嘗試數 − 1）",
          len(waits) == n - 1)
    check(f"★★ 每次等待都恰為 backoff {RETRY_BACKOFF_SEC_CANDIDATE}",
          all(w == RETRY_BACKOFF_SEC_CANDIDATE for w in waits))
    check("★★ 不存在 0 秒等待的路徑", all(w > 0 for w in waits))
    check(f"  假時鐘實際前進 {clk.t:.0f} s（不是瞬間燒完）",
          clk.t == RETRY_BACKOFF_SEC_CANDIDATE * (n - 1))
    check("  失敗回傳 None，不編造資料", v is None)

    # 成功即停：不做無謂補打
    p2 = SVC.RetryPolicy(RETRY_BACKOFF_SEC_CANDIDATE, DECISION_INTERVAL,
                         sleeper=Sleeper())
    v2, n2, w2 = p2.run(lambda: {"ok": True})
    check("★★ 第一次就成功 → 只嘗試 1 次、0 次等待",
          v2 == {"ok": True} and n2 == 1 and w2 == [])

    # 例外不得逃出，也不得變成無限重試
    p3 = SVC.RetryPolicy(RETRY_BACKOFF_SEC_CANDIDATE, DECISION_INTERVAL,
                         sleeper=Sleeper())

    def boom():
        raise RuntimeError("端點掛了")

    v3, n3, _ = p3.run(boom)
    check("  read 拋例外 → 視為失敗，不外洩例外，且仍受上限約束",
          v3 is None and n3 == p3.max_attempts())


# ======================================================================
# 5. retry 不造成 command storm
# ======================================================================
def test_5_no_storm():
    print("\n[5] storm ceiling")
    p = SVC.RetryPolicy(RETRY_BACKOFF_SEC_CANDIDATE, DECISION_INTERVAL,
                        sleeper=Sleeper())
    ceiling = int(DECISION_INTERVAL // RETRY_BACKOFF_SEC_CANDIDATE)
    check(f"★★ 一輪最多 {ceiling} 次嘗試（floor(30/5)）",
          p.max_attempts() == ceiling == 6)
    check("  未設定 backoff → 上限 1（等於不重試，退回既有行為）",
          SVC.RetryPolicy(None, DECISION_INTERVAL).max_attempts() == 1)
    check("  backoff <= 0 也不得放大流量",
          SVC.RetryPolicy(0.0, DECISION_INTERVAL).max_attempts() == 1)

    # 遠端 SSH probe：一輪只准一次
    pr = SVC.RetryPolicy(RETRY_BACKOFF_SEC_CANDIDATE, DECISION_INTERVAL,
                         max_attempts=1, sleeper=Sleeper())
    n = pr.run(lambda: None)[1]
    check("★★ 遠端 probe 一輪只送 1 次 SSH（不對別人的 production 連打）",
          pr.max_attempts() == 1 and n == 1)

    # 服務層：長時間全失敗，流量仍受每輪上限約束
    clk, slp = Clock(), Sleeper()
    s, sd = service(lambda: None, lambda: None, clk, slp,
                    retry=RETRY_BACKOFF_SEC_CANDIDATE,
                    health=SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
    s.boot()
    res = s.run(max_ticks=10)
    check(f"★★ 10 輪全失敗：本機每輪嘗試皆 <= {ceiling}",
          all(r["local_attempts"] <= ceiling for r in res))
    check("★★ 10 輪全失敗：遠端每輪嘗試皆 = 1",
          all(r["remote_attempts"] == 1 for r in res))
    check("★★ 全程 pause / restore sender = 0",
          sd.pause_calls == 0 and sd.restore_calls == 0)


# ======================================================================
# 6. 單次瞬斷不得立刻 critical
# ======================================================================
def test_6_transient_not_critical():
    print("\n[6] transient single failure 不立刻 critical")
    clk = Clock()
    slp = Sleeper(clk)
    seq = [None, smp()]          # 第一次失敗、重試即成功

    def src():
        return seq.pop(0) if seq else smp()

    s, sd = service(src, lambda: probe(), clk, slp,
                    retry=RETRY_BACKOFF_SEC_CANDIDATE,
                    health=SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
    s.boot()
    r = s.tick()
    check("★★ 同一輪內重試成功，未進入 critical", s.critical is None)
    check("  該輪嘗試 2 次（1 失敗 + 1 成功）", r["local_attempts"] == 2)
    check("★★ health 仍為 HEALTHY", r["health"] == SVC.HEALTH_OK)
    check("  未寫入任何 CRITICAL_FAILURE 事件",
          not any(x.get("kind") == SVC.EV_CRITICAL
                  for x in s.journal.read_all()))
    check("  sender 仍為 0", sd.pause_calls == 0 and sd.restore_calls == 0)

    # 沒有 backoff 時（未定案）→ 一輪一次，仍不得 critical
    clk2 = Clock()
    slp2 = Sleeper(clk2)
    seq2 = [None]

    def src2():
        return seq2.pop(0) if seq2 else smp()

    s2, sd2 = service(src2, lambda: probe(), clk2, slp2)
    s2.boot()
    r2 = s2.tick()
    check("  未設定 backoff → 該輪只嘗試 1 次（退回既有行為）",
          r2["local_attempts"] == 1)
    check("★★ 未設定 backoff 也不得因單次失敗 critical", s2.critical is None)


# ======================================================================
# 7. 持續失敗最終 Fail Closed
# ======================================================================
def test_7_prolonged_fail_closed():
    print("\n[7] prolonged failure 最終 Fail Closed")
    clk = Clock()
    slp = Sleeper(clk)
    s, sd = service(lambda: None, lambda: None, clk, slp,
                    retry=RETRY_BACKOFF_SEC_CANDIDATE,
                    health=SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
    s.boot()
    res = s.run(max_ticks=20)
    check("★★ 全部輪次皆 NO_HANDOFF",
          all(r["decision"] == "NO_HANDOFF" for r in res))
    stalled = [r for r in res if r["health"] == SVC.HEALTH_UNKNOWN]
    check(f"★★ 持續無進展後 health 轉為 SERVICE_HEALTH_UNKNOWN"
          f"（第 {res.index(stalled[0]) + 1} 輪起）", bool(stalled))
    first = res.index(stalled[0])
    check(f"  逾時發生時停滯 {res[first]['stalled_sec']:.0f} s"
          f" > {SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE:.0f} s",
          res[first]["stalled_sec"] > SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
    check("  逾時前的輪次不得已經 UNKNOWN",
          all(r["health"] == SVC.HEALTH_OK for r in res[:first]))
    # C1 裁示：SERVICE_HEALTH_UNKNOWN 是 recoverable fail-closed，**不是**
    # critical latch，因此不得寫成 CRITICAL_FAILURE；durable 證據改為
    # ELIGIBILITY_DECISION 內逐項記錄。
    check("★★ 逾時**不**寫 CRITICAL_FAILURE（不是 critical latch）",
          not any(x.get("kind") == SVC.EV_CRITICAL
                  for x in s.journal.read_all()))
    check("★★ 逾時仍留下 durable 證據（ELIGIBILITY_DECISION 記到 health）",
          any(x.get("kind") == SVC.EV_ELIGIBILITY
              and SVC.HEALTH_UNKNOWN in (x.get("detail") or "")
              for x in s.journal.read_all()))
    check("★★ 逾時不設 critical latch（self.critical 仍為 None）",
          s.critical is None)
    check("★★ 逾時後 blocked 明確列出 service_health_healthy（第 11 個 gate）",
          "service_health_healthy" in res[first]["blocked"])
    check("★★ 從頭到尾 pause / restore sender = 0",
          sd.pause_calls == 0 and sd.restore_calls == 0)


# ======================================================================
# 8. 進展正常時不得誤觸 timeout
# ======================================================================
def test_8_no_false_timeout():
    print("\n[8] 有進展就不得逾時")
    clk = Clock()
    h = SVC.ServiceHealth(SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE, clock=clk)
    for i in range(40):          # 40 × 30 s = 20 分鐘正常運轉
        clk.advance(DECISION_INTERVAL)
        h.mark(SVC.PROGRESS_LOCAL)
        if h.verdict() != SVC.HEALTH_OK:
            break
    check("★★ 每 30 s 有一次進展 → 20 分鐘內從未誤觸",
          h.verdict() == SVC.HEALTH_OK and clk.t == 40 * DECISION_INTERVAL)

    # 最長合法連續期間：settling 120 + stable verify 45 + 一個 interval 30
    clk2 = Clock()
    h2 = SVC.ServiceHealth(SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE, clock=clk2)
    clk2.advance(HO.HandoffConfig.pause_settling_timeout_sec)
    check("  settling 120 s 全程未逾時", h2.verdict() == SVC.HEALTH_OK)
    clk2.advance(HO.HandoffConfig.stable_verify_min_span_sec)
    check("  再加 stable verify 45 s 仍未逾時", h2.verdict() == SVC.HEALTH_OK)
    clk2.advance(DECISION_INTERVAL)
    check(f"★★ 再加一個 decision interval（共 {clk2.t:.0f} s）仍未逾時",
          clk2.t == 195.0 and h2.verdict() == SVC.HEALTH_OK)

    # 四種進展任一種都算數
    for kind in SVC.PROGRESS_KINDS:
        c = Clock()
        hh = SVC.ServiceHealth(SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE, clock=c)
        c.advance(299.0)
        hh.mark(kind)
        c.advance(299.0)
        check(f"  {kind} 也算進展 → 不逾時", hh.verdict() == SVC.HEALTH_OK)
    check("  未知的 progress 種類必須拒收",
          _raises(lambda: SVC.ServiceHealth(300.0, clock=Clock())
                  .mark("whatever")))


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


# ======================================================================
# 9. 進展停止 → timeout 成立
# ======================================================================
def test_9_timeout_fires():
    print("\n[9] 進展停止 → timeout 成立")
    clk = Clock()
    h = SVC.ServiceHealth(SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE, clock=clk)
    clk.advance(SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
    check("  恰好等於 timeout → 尚未逾時（用 > 不用 >=）",
          h.verdict() == SVC.HEALTH_OK)
    clk.advance(0.1)
    check("★★ 超過 timeout → SERVICE_HEALTH_UNKNOWN",
          h.verdict() == SVC.HEALTH_UNKNOWN)
    check(f"  停滯秒數如實回報（{h.stalled_sec():.1f} s）",
          abs(h.stalled_sec() - 300.1) < 1e-6)

    # 恢復進展 → 回到 HEALTHY（這不是不可逆的 critical state）
    h.mark(SVC.PROGRESS_AUDIT)
    check("  恢復進展後回到 HEALTHY", h.verdict() == SVC.HEALTH_OK)

    # timeout 未設定 → UNCONFIGURED，且**不得**被當成 HEALTHY
    hu = SVC.ServiceHealth(None, clock=Clock())
    check("★★ timeout=None → UNCONFIGURED（不是 HEALTHY）",
          hu.verdict() == SVC.HEALTH_UNCONFIGURED and not hu.healthy())


# ======================================================================
# 10. 逾時不得自己送 pause / restore / control
# ======================================================================
def test_10_timeout_sends_nothing():
    print("\n[10] 逾時的唯一效果是裁決字串，不是動作")
    params = inspect.signature(SVC.ServiceHealth.__init__).parameters
    check("★★ ServiceHealth 建構子不接受 sender / dispatcher / remote",
          not any(k in " ".join(params) for k in
                  ("sender", "dispatch", "remote", "operator")))
    src = inspect.getsource(SVC.ServiceHealth)
    tree = ast.parse(src.lstrip())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("★★ ServiceHealth 內不呼叫 pause / restore / stop / start / dispatch",
          not (called & {"pause", "restore", "stop", "start", "dispatch",
                         "charge", "discharge"}))
    rsrc = ast.parse(inspect.getsource(SVC.RetryPolicy).lstrip())
    rcalled = {n.func.attr for n in ast.walk(rsrc)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("★★ RetryPolicy 內同樣不呼叫任何控制動詞",
          not (rcalled & {"pause", "restore", "stop", "start", "dispatch"}))

    # 服務層：讓 health 逾時，確認 sender / dispatcher 完全沒有被碰
    clk, slp = Clock(), Sleeper()
    hits = []
    s, sd = service(lambda: None, lambda: None, clk, slp,
                    retry=RETRY_BACKOFF_SEC_CANDIDATE,
                    health=SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE,
                    dispatcher=(lambda x: hits.append(x) or {}))
    s.boot()
    for _ in range(12):
        clk.advance(DECISION_INTERVAL)
        s.tick()
    check("  已確實進入逾時", s.health.verdict() == SVC.HEALTH_UNKNOWN)
    check("★★ 逾時後 pause sender = 0", sd.pause_calls == 0)
    check("★★ 逾時後 restore sender = 0", sd.restore_calls == 0)
    check("★★ 逾時後 dispatcher = 0", hits == [])
    check("  journal 中沒有任何 pause / restore 的 INTENT",
          not any(x.get("step") in ("PAUSE", "RESTORE")
                  for x in s.journal.read_all()))


# ======================================================================
# 11. DRY_RUN：注入候選值後 sender / dispatcher 仍恆為 0
# ======================================================================
def test_11_dry_run_zero():
    print("\n[11] DRY_RUN 注入候選值後仍然全 0")
    for mode in (HO.MODE_DRY_RUN, HO.MODE_OBSERVE):
        clk, slp = Clock(), Sleeper()
        hits = []
        s, sd = service(lambda: smp(), lambda: probe(), clk, slp,
                        retry=RETRY_BACKOFF_SEC_CANDIDATE,
                        health=SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE,
                        mode=mode, dispatcher=(lambda x: hits.append(x) or {}))
        v, _ = s.boot()
        check(f"  {mode}: boot READY", v == SVC.BOOT_OK)
        res = s.run(max_ticks=12)
        check(f"  {mode}: 12 輪皆 NO_HANDOFF",
              all(r["decision"] == "NO_HANDOFF" for r in res))
        check(f"  {mode}: health 全程 HEALTHY（有進展）",
              all(r["health"] == SVC.HEALTH_OK for r in res))
        check(f"★★ {mode}: pause / restore sender = 0",
              sd.pause_calls == 0 and sd.restore_calls == 0)
        check(f"★★ {mode}: dispatcher = 0", hits == [])
        v2, _ = s.shutdown()
        check(f"  {mode}: shutdown CLEAN", v2 == SVC.SHUTDOWN_CLEAN)


# ======================================================================
# 12. production config 仍未被寫入候選值
# ======================================================================
def test_12_approved():
    print("\n[12] C1 裁示落地：兩項已定案")
    t = SVC.ServiceTiming()
    check("★★ ServiceTiming().retry_backoff_sec = 5.0",
          t.retry_backoff_sec == RETRY_BACKOFF_SEC_CANDIDATE == 5.0)
    check("★★ ServiceTiming().service_health_timeout_sec = 300.0",
          t.service_health_timeout_sec
          == SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE == 300.0)
    check("★★ missing() 為空（不再列出這兩項）", t.missing() == [])
    check(f"  decision_interval_sec 沿用既有 production 值"
          f"（{t.decision_interval_sec}）", t.decision_interval_sec == 30.0)
    check("  核准值本身仍落在契約區間內",
          SVC.retry_backoff_in_contract(t.retry_backoff_sec)
          and SVC.service_health_timeout_in_contract(
              t.service_health_timeout_sec))

    # 預設值必須引用具名的已核准常數，不得是散落的魔術數字
    src = io.open(os.path.join(HERE, "phase6_unattended_service.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    defaults = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            args = node.args
            for a, dflt in zip(args.args[len(args.args) - len(args.defaults):],
                               args.defaults):
                if a.arg in ("retry_backoff_sec", "service_health_timeout_sec"):
                    defaults[a.arg] = dflt
    check("★★ 兩個預設值在 AST 上是具名常數（非硬編數字）",
          len(defaults) == 2
          and all(isinstance(d, ast.Name) and d.id.startswith("APPROVED_")
                  for d in defaults.values()))
    check("  APPROVED 常數值正確",
          SVC.APPROVED_RETRY_BACKOFF_SEC == 5.0
          and SVC.APPROVED_SERVICE_HEALTH_TIMEOUT_SEC == 300.0)
    check("★★ 仍保留「注入 None」的 Fail Closed 路徑",
          SVC.ServiceTiming(retry_backoff_sec=None,
                            service_health_timeout_sec=None).missing()
          == list(SVC.REQUIRED_TIMING_FIELDS))
    check("★★ 參數定案 != live 啟用：DISPATCH_ENABLED 仍為 False",
          HO.DISPATCH_ENABLED is False)
    check("★★ CRITICAL_CONDITIONS 維持 4 項（health 未升為第 5 項）",
          len(SVC.CRITICAL_CONDITIONS) == 4
          and SVC.HEALTH_UNKNOWN not in SVC.CRITICAL_CONDITIONS)
    check("★★ LIVE_GATE_ITEMS 為 11 項且含 service_health_healthy",
          len(SVC.LIVE_GATE_ITEMS) == 11
          and "service_health_healthy" in SVC.LIVE_GATE_ITEMS)


# ======================================================================
# 13. health 作為第 11 個 live gate
# ======================================================================
def test_13_health_gate():
    print("\n[13] service_health_healthy = 第 11 個 live gate")
    base = dict(mode=HO.MODE_ARMED, recovery_verdict=HO.R_CLEAN,
                local_fresh=True, remote_probe=probe())
    for hv, want in ((SVC.HEALTH_OK, True),
                     (SVC.HEALTH_UNKNOWN, False),
                     (SVC.HEALTH_UNCONFIGURED, False),
                     (None, False)):
        ok, ch = SVC.live_handoff_allowed(health=hv, **base)
        check(f"  health={hv} → gate {'PASS' if want else 'FAIL'}",
              ch["service_health_healthy"] is want)
        check(f"  health={hv} → live handoff 仍 REFUSED（其餘 gate 未過）",
              ok is False)
    check("★★ 未帶 health 參數 → 預設 Fail Closed",
          SVC.live_handoff_allowed(HO.MODE_ARMED, HO.R_CLEAN, True,
                                   probe())[1]["service_health_healthy"]
          is False)

    # 服務層：UNKNOWN → refused；恢復進展 → gate 重新 PASS，但不得自動開始交接
    clk = Clock()
    slp = Sleeper()
    live = {"v": None}

    def src():
        return live["v"]

    def prb():
        # 🔴 remote probe 成功本身就是一種進展，因此要模擬「完全沒有進展」
        #    必須讓兩個來源都失敗，只讓觀測失敗是不夠的。
        return None if live["v"] is None else probe()

    s, sd = service(src, prb, clk, slp,
                    retry=RETRY_BACKOFF_SEC_CANDIDATE,
                    health=SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
    s.boot()
    for _ in range(12):                      # 觀測持續失敗
        clk.advance(DECISION_INTERVAL)
        r = s.tick()
    check("★★ 持續無進展 → health UNKNOWN",
          r["health"] == SVC.HEALTH_UNKNOWN)
    check("★★ health UNKNOWN → gate service_health_healthy = False",
          r["gates"]["service_health_healthy"] is False)
    check("★★ health UNKNOWN → LIVE HANDOFF REFUSED",
          r["live_allowed"] is False and r["decision"] == "NO_HANDOFF")
    check("  UNKNOWN 期間未設 critical latch", s.critical is None)

    live["v"] = smp()                        # 進展恢復
    clk.advance(DECISION_INTERVAL)
    r2 = s.tick()
    check("★★ 進展恢復 → health 回到 HEALTHY", r2["health"] == SVC.HEALTH_OK)
    check("★★ 進展恢復 → service_health_healthy 重新 PASS",
          r2["gates"]["service_health_healthy"] is True)
    check("★★ 但仍不得自動開始 handoff（其餘 10 項未全過）",
          r2["decision"] == "NO_HANDOFF" and r2["live_allowed"] is False)
    still = [k for k in SVC.LIVE_GATE_ITEMS
             if k != "service_health_healthy" and not r2["gates"][k]]
    check(f"  仍未過的 gate：{still}", len(still) >= 1)
    check("★★ 全程 sender / dispatcher 為 0",
          sd.pause_calls == 0 and sd.restore_calls == 0)


# ======================================================================
# 14. watchdog 不得被自己的 log 餵活
# ======================================================================
def test_14_no_self_feeding():
    print("\n[14] 禁止 self-feeding watchdog")
    clk = Clock()
    slp = Sleeper()
    s, _ = service(lambda: None, lambda: None, clk, slp,
                   retry=RETRY_BACKOFF_SEC_CANDIDATE,
                   health=SERVICE_HEALTH_TIMEOUT_SEC_CANDIDATE)
    s.boot()
    before = len(s.journal.read_all())
    for _ in range(12):
        clk.advance(DECISION_INTERVAL)
        s.tick()
    after = len(s.journal.read_all())
    check(f"  這 12 輪確實有持續寫 journal（{before} → {after} 筆）",
          after > before + 10)
    check("★★ 但 watchdog 仍然逾時 —— 寫 log 不算進展",
          s.health.verdict() == SVC.HEALTH_UNKNOWN)
    check("★★ 全程未產生任何 audit 進展（audit 尚無 producer）",
          not any(k == SVC.PROGRESS_AUDIT for k, _ in s.health.marks))

    # 原始碼層級：journal.append 附近不得夾帶 mark(PROGRESS_AUDIT)
    src = io.open(os.path.join(HERE, "phase6_unattended_service.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mark"):
            continue
        for a in node.args:
            if isinstance(a, ast.Name) and a.id == "PROGRESS_AUDIT":
                bad.append(node.lineno)
            if isinstance(a, ast.Attribute) and a.attr == "PROGRESS_AUDIT":
                bad.append(node.lineno)
            if isinstance(a, ast.Constant) and a.value == PROGRESS_AUDIT_STR:
                bad.append(node.lineno)
    check(f"★★ production 內完全沒有 mark(PROGRESS_AUDIT) 呼叫（命中={bad}）",
          not bad)
    marked = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mark"):
            for a in node.args:
                if isinstance(a, ast.Name):
                    marked.add(a.id)
    check(f"  實際會被 mark 的進展種類：{sorted(marked)}",
          marked <= {"PROGRESS_LOCAL", "PROGRESS_REMOTE", "PROGRESS_CONTROL"})


PROGRESS_AUDIT_STR = "audit"


# ======================================================================
def main():
    for fn in (test_1_not_conflated, test_2_bounds_traceable,
               test_3_candidates, test_4_no_busy_loop, test_5_no_storm,
               test_6_transient_not_critical, test_7_prolonged_fail_closed,
               test_8_no_false_timeout, test_9_timeout_fires,
               test_10_timeout_sends_nothing, test_11_dry_run_zero,
               test_12_approved, test_13_health_gate,
               test_14_no_self_feeding):
        fn()
    import shutil
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
