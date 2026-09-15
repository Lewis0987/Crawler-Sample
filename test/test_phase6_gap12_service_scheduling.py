# -*- coding: utf-8 -*-
"""
test_phase6_gap12_service_scheduling.py — GAP-1 / GAP-2 Service Scheduling
======================================================================
核心命題
    「decision cadence 是 service shell 的 I/O 排程問題：
      正式節奏必須來自 production config（唯一來源），
      而等待機制必須可注入，否則長跑測試只能靠真的 sleep。」

🔴 只驗 `pcs_auto_control_service` 的 shell 行為。
   **不碰** AutoControlRuntime / ProductionArbiter / DecisionEngine /
   DecisionPolicy / SafetyGate / ControlAuthority / PowerClassifier。
🔴 Runtime.tick() 維持「無節奏的純 cycle」—— cadence 不得塞進 Runtime。
🔴 完全 OFFLINE / ZERO-I/O：**不執行 CLI**，一律以 constructor + 依賴注入測試。
   不 sleep、不連任何設備。

A  未指定 interval → 取 DEFAULT_CONTROL_CONFIG.decision_interval_sec
B  config 換成測試值 → service default 跟著改（service 沒有第二份 30.0）
C  explicit override → 以傳入值為準（CLI override 契約保留）
D  0 / 負 / None / NaN / inf / 非數 → Fail Closed，不進迴圈、不自行 fallback
E  fake waiter 收到正確秒數
F  tick / wait 次數依實際控制流；100 cycles 實耗時 ≈ 0
S  stop_event 仍可正常停止；訊號語意未改
H  run() 結束 → relinquish authority only，不送 pcs_stop_power
T  authorization_ttl_sec(10) vs decision_interval_sec(30) 是否衝突
N  scope：只處理 GAP-1 / GAP-2

用法
    python test_phase6_gap12_service_scheduling.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import glob
import time
import math
import threading
import dataclasses

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import control_authority as CA                    # noqa: E402
import tariff_provider as TP                      # noqa: E402
import annual_off_peak_calendar as AC             # noqa: E402
import pcs_auto_control_config as CFG             # noqa: E402
import pcs_auto_control_runtime as RT             # noqa: E402
import pcs_auto_control_service as SVC            # noqa: E402
import production_arbiter as ARB                  # noqa: E402
import phase6_decision_replay_runner as RUN       # noqa: E402
import test_phase6_runtime_arbiter_longrun as LR  # noqa: E402
import test_phase6_gap3_tariff_wiring as G3       # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


class Waiter(object):
    """假等待機制：只記錄被要求等多久，**完全不等待**。"""

    def __init__(self, stop_after=None, stop_event=None):
        self.calls = []
        self.stop_after = stop_after
        self.stop_event = stop_event

    def __call__(self, interval):
        self.calls.append(interval)
        if (self.stop_after is not None
                and len(self.calls) >= self.stop_after
                and self.stop_event is not None):
            self.stop_event.set()


class Quiet(object):
    """暫時關掉 service 的 stdout log（長跑時避免數千行雜訊）。"""

    def __enter__(self):
        self._real = SVC.log
        SVC.log = lambda _msg: None
        return self

    def __exit__(self, *a):
        SVC.log = self._real
        return False


def go(runtime=None, **kw):
    """一律注入 owner=True 與獨立 stop_event —— 不碰任何 OS ownership。"""
    kw.setdefault("owner", True)
    kw.setdefault("stop_event", threading.Event())
    return SVC.run(runtime if runtime is not None else RT.AutoControlRuntime(),
                   **kw)


def cfg_with(**over):
    return dataclasses.replace(CFG.DEFAULT_CONTROL_CONFIG, **over)


# ======================================================================
# A / B / C —— cadence 來源
# ======================================================================
def test_A_default_from_config():
    print("\n[A] 未指定 interval → 取 production config 的 decision_interval_sec")
    check(f"  A0. production config decision_interval_sec = "
          f"{CFG.DEFAULT_CONTROL_CONFIG.decision_interval_sec}",
          CFG.DEFAULT_CONTROL_CONFIG.decision_interval_sec == 30.0)
    check(f"★★ A1. production_interval_sec() = "
          f"{SVC.production_interval_sec()}（同一來源）",
          SVC.production_interval_sec()
          is CFG.DEFAULT_CONTROL_CONFIG.decision_interval_sec)

    w = Waiter()
    with Quiet():
        n = go(max_ticks=3, waiter=w)
    check(f"★★ A2. run() 未指定 interval → waiter 收到 {set(w.calls)}",
          n == 3 and set(w.calls) == {30.0})
    check("★★ A3. service 模組內沒有第二份 cadence 數值",
          not hasattr(SVC, "DEFAULT_INTERVAL_SEC"))
    src = io.open(SVC.__file__, encoding="utf-8").read()
    nums = {n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant)
            and isinstance(n.value, (int, float))
            and not isinstance(n.value, bool)}
    check(f"★★ A4. service 原始碼未 hard-code 30.0 / 30",
          30.0 not in nums and 30 not in nums)


def test_B_follows_config():
    print("\n[B] config 換成測試值 → service default 跟著改")
    for want in (7.5, 45.0, 120.0):
        rt = RT.AutoControlRuntime(config=cfg_with(decision_interval_sec=want))
        w = Waiter()
        with Quiet():
            n = go(rt, max_ticks=3, waiter=w)
        check(f"★★ B. decision_interval_sec={want} → waiter 收到 {set(w.calls)}",
              n == 3 and set(w.calls) == {want})
    check("★★ B4. production config 本身未被測試汙染（仍為 30.0 FINAL）",
          CFG.DEFAULT_CONTROL_CONFIG.decision_interval_sec == 30.0)
    # 🔴 config 既有驗證：decision_interval_sec 只接受 None 或正數。
    #    本輪未放寬它 —— 0 由 config 層擋下，不是由 service 層另訂規則。
    try:
        cfg_with(decision_interval_sec=0.0)
        bad = False
    except ValueError:
        bad = True
    check("★★ B5. config 既有驗證未被放寬（0 仍被 config 拒絕）", bad)


def test_C_explicit_override():
    print("\n[C] explicit override → 以傳入值為準（CLI --interval 契約保留）")
    w = Waiter()
    with Quiet():
        n = go(max_ticks=3, interval=5.0, waiter=w)
    check(f"★★ C1. interval=5.0 覆寫 config 的 30.0 → {set(w.calls)}",
          n == 3 and set(w.calls) == {5.0})
    w2 = Waiter()
    with Quiet():
        go(max_ticks=2, interval=2.5, waiter=w2)
    check(f"★★ C2. interval=2.5 → {w2.calls}", w2.calls == [2.5])

    # CLI 契約：--interval 仍在，且預設為「不覆寫」
    import argparse
    src = io.open(SVC.__file__, encoding="utf-8").read()
    check("★★ C3. CLI 仍保留 --interval（override 未被移除）",
          '"--interval"' in src)
    check("★★ C4. CLI --interval 預設為 None（不覆寫 → 走 config）",
          'ap.add_argument("--interval", type=float, default=None,' in src)
    check("  C5. _UNSET 與 None 語意不同（未指定 vs 不可用）",
          SVC._UNSET is not None and isinstance(SVC._UNSET, object))
    del argparse


def test_D_invalid_interval_fail_closed():
    print("\n[D] 0 / 負 / None / NaN / inf / 非數 → Fail Closed，不自行 fallback")
    check("★★ D0. cadence 規則 = finite AND > 0",
          SVC._usable_interval(30.0) is True
          and SVC._usable_interval(0.001) is True
          and SVC._usable_interval(0) is False
          and SVC._usable_interval(0.0) is False
          and SVC._usable_interval(True) is False)
    calls = {"n": 0}

    class CountingRuntime(RT.AutoControlRuntime):
        def tick(self, inputs=None):
            calls["n"] += 1
            return super().tick(inputs)

    # 🔴 0 也在其中：常駐服務以 0 秒節奏會變成 tight loop，
    #    而一個正常 cycle 至少 ESS ×2 + 電表 ×2 次讀取。
    for bad in (0, 0.0, -1.0, None, float("nan"), float("inf"), "30", True):
        calls["n"] = 0
        rt = CountingRuntime(config=cfg_with(decision_interval_sec=None))
        w = Waiter()
        with Quiet():
            n = SVC.run(rt, interval=bad, stop_event=threading.Event(),
                        max_ticks=3, owner=True, waiter=w)
        check(f"★★ D. interval={bad!r} → 不進迴圈（ticks={n}、waits="
              f"{len(w.calls)}、tick 呼叫={calls['n']}）",
              n == 0 and w.calls == [] and calls["n"] == 0)

    # config 的 decision_interval_sec 未配置時，_UNSET 同樣 Fail Closed
    calls["n"] = 0
    rt = CountingRuntime(config=cfg_with(decision_interval_sec=None))
    w = Waiter()
    with Quiet():
        n = SVC.run(rt, stop_event=threading.Event(), max_ticks=3,
                    owner=True, waiter=w)
    check("★★ D6. config 未配置 cadence → 一輪都不跑（沿用既有 Fail Closed）",
          n == 0 and w.calls == [] and calls["n"] == 0)
    check(f"★★ D7. 未自行 fallback 成任何秒數（理由常數 "
          f"{SVC.INTERVAL_NOT_CONFIGURED}）",
          SVC.INTERVAL_NOT_CONFIGURED == "DECISION_INTERVAL_NOT_CONFIGURED"
          and "decision_interval_sec" in
          [f.name for f in dataclasses.fields(CFG.DEFAULT_CONTROL_CONFIG)])
    check("★★ D8. cadence 未配置仍屬 config 既有必要參數（未放寬）",
          "decision_interval_sec" in CFG.REQUIRED_FOR_DISPATCH
          and cfg_with(decision_interval_sec=None).dispatch_ready is False)
    # tight loop 防護：cadence=0 且無 max_ticks 時，**一輪都不得跑**
    calls["n"] = 0
    rt0 = CountingRuntime()
    w0 = Waiter()
    with Quiet():
        n0 = SVC.run(rt0, interval=0, stop_event=threading.Event(),
                     owner=True, waiter=w0)
    check("★★ D9. cadence=0 且無 max_ticks → 不進迴圈（不會形成 tight loop）",
          n0 == 0 and w0.calls == [] and calls["n"] == 0)
    check("★★ D10. 零等待長跑改以注入 waiter 達成，而非把 cadence 設成 0",
          "waiter" in SVC.run.__code__.co_varnames)


# ======================================================================
# E / F —— waiter 注入與次數
# ======================================================================
def test_E_waiter_injection():
    print("\n[E] waiter 注入")
    w = Waiter()
    with Quiet():
        go(max_ticks=2, waiter=w)
    check(f"★★ E1. fake waiter 收到 {w.calls}（= production cadence）",
          w.calls == [30.0])
    # 🔴 本檔的 D 段刻意有 interval=0（那是 Fail Closed 案例），
    #    所以掃的是**其他** Phase 6 測試有沒有還在拿 0 當可用 cadence。
    others = []
    for f in glob.glob(os.path.join(HERE, "test_phase6*.py")):
        if os.path.basename(f) == os.path.basename(__file__):
            continue
        tree = ast.parse(io.open(f, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (kw.arg == "interval"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value in (0, 0.0)):
                    others.append(os.path.basename(f))
    check(f"★★ E1b. 其他 Phase 6 測試不再以 interval=0 規避等待：命中={others}",
          not others)
    check("★★ E2. production 預設仍為 stop_event.wait（未注入時行為不變）",
          "wait = waiter if waiter is not None else stop_event.wait"
          in io.open(SVC.__file__, encoding="utf-8").read())
    check("★★ E3. 未新增 Scheduler / Timer / 背景執行緒 / asyncio",
          not any(n in dir(SVC) for n in ("Scheduler", "Timer",
                                          "SchedulerService", "asyncio"))
          and "import asyncio" not in io.open(SVC.__file__,
                                              encoding="utf-8").read())
    src = ast.parse(io.open(SVC.__file__, encoding="utf-8").read())
    starts = [n for n in ast.walk(src) if isinstance(n, ast.Attribute)
              and n.attr == "start"
              and isinstance(n.value, ast.Name) and n.value.id == "Thread"]
    check("★★ E4. run() 仍跑在呼叫端執行緒，未另開 thread", not starts)


def test_F_tick_wait_counts():
    print("\n[F] tick / wait 次數依實際控制流；長跑實耗時 ≈ 0")
    # 🔴 目前控制流：達到 max_ticks 時在 wait **之前** break
    #    → N ticks 對應 N-1 waits。期望值依程式碼推得，不硬寫。
    for ticks in (1, 2, 3, 10):
        w = Waiter()
        with Quiet():
            n = go(max_ticks=ticks, waiter=w)
        check(f"★★ F. max_ticks={ticks} → ticks={n}、waits={len(w.calls)}"
              f"（最後一輪在 wait 前 break）",
              n == ticks and len(w.calls) == ticks - 1)

    t0 = time.monotonic()
    w = Waiter()
    with Quiet():
        n = go(max_ticks=100, waiter=w)
    elapsed = time.monotonic() - t0
    check(f"★★ F5. 100 cycles × cadence 30s，實際耗時 {elapsed:.3f}s"
          f"（虛擬等待 {sum(w.calls):.0f}s）",
          n == 100 and len(w.calls) == 99 and sum(w.calls) == 99 * 30.0
          and elapsed < 5.0)
    check("★★ F6. Runtime 仍是無節奏純 cycle（cadence 不在 Runtime 內）",
          not any(n in dir(RT) for n in ("DEFAULT_INTERVAL_SEC",
                                         "decision_interval_sec"))
          and "decision_interval_sec" not in
          io.open(RT.__file__, encoding="utf-8").read())


# ======================================================================
# S / H —— 停止與關閉
# ======================================================================
def test_S_stop_event():
    print("\n[S] stop_event 仍可正常停止；訊號語意未改")
    ev = threading.Event()
    w = Waiter(stop_after=4, stop_event=ev)
    with Quiet():
        n = SVC.run(RT.AutoControlRuntime(), stop_event=ev, owner=True,
                    waiter=w)
    check(f"★★ S1. waiter 於第 4 次等待時 set stop_event → 共跑 {n} 輪即停",
          n == 4 and len(w.calls) == 4)

    ev2 = threading.Event()
    ev2.set()
    w2 = Waiter()
    with Quiet():
        n2 = SVC.run(RT.AutoControlRuntime(), stop_event=ev2, owner=True,
                     waiter=w2)
    check("★★ S2. 事前已 set 的 stop_event → 一輪都不跑",
          n2 == 0 and w2.calls == [])

    # 訊號語意（結構檢查，不實際安裝 handler）
    tree = ast.parse(io.open(SVC.__file__, encoding="utf-8").read())
    fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
          and n.name == "install_signal_handlers"][0]
    names = {n.value for n in ast.walk(fn)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check(f"★★ S3. 訊號集合未改：{sorted(names)}",
          names == {"SIGINT", "SIGTERM", "SIGBREAK"})
    check("★★ S4. handler 語意未改（仍為 stop_event.set）",
          any(isinstance(n, ast.Attribute) and n.attr == "set"
              for n in ast.walk(fn)))


def test_H_shutdown():
    print("\n[H] run() 結束 → relinquish authority only")
    rt = RT.AutoControlRuntime()
    w = Waiter()
    with Quiet():
        go(rt, max_ticks=3, waiter=w)
    res = rt.shutdown()
    check(f"★★ H1. shutdown() → {res.state}",
          res.state == RT.ST_SHUTTING_DOWN and res.dispatched is False)
    check("★★ H2. detail 明確載明不送 pcs_stop_power",
          "pcs_stop_power" in (res.detail or ""))
    after = [rt.tick(RT.CycleInputs(is_owner=True)) for _ in range(2)]
    check(f"★★ H3. 之後 tick() 一律 {after[0].reason}",
          all(r.reason == RT.R_ALREADY_SHUTDOWN for r in after))
    check("★★ H4. 全程 dispatch_count = 0（軟體停止 ≠ PCS STOP）",
          rt.dispatch_count == 0)
    src = io.open(SVC.__file__, encoding="utf-8").read()
    check("★★ H5. shutdown 流程未因本輪改動而新增任何控制出口",
          "pcs_stop_power" not in src.split("def shutdown(")[1]
          .split("def ")[0].replace("未送出 pcs_stop_power", "")
          .replace("不送出 pcs_stop_power", ""))


# ======================================================================
# T —— TTL vs cadence
# ======================================================================
def test_T_ttl_vs_cadence():
    print("\n[T] authorization_ttl_sec(10) vs decision_interval_sec(30)")
    c = CFG.DEFAULT_CONTROL_CONFIG
    check(f"  T0. TTL={c.authorization_ttl_sec} < cadence="
          f"{c.decision_interval_sec}（兩者皆未被本輪修改）",
          c.authorization_ttl_sec == 10.0 and c.decision_interval_sec == 30.0)

    # 以 30s 虛擬 cadence 實際跑一條鏈，看授權票會不會跨 cycle 殘留
    h = LR.Harness()
    tl = RUN.build_timeline(G3.aware(2026, 7, 15, 3), 30, 8, name="T",
                            meter_kw=60.0, soc_percent=50.0)
    recs = h.run(tl, step_sec=30.0)
    check("★★ T1. 每個 cycle 都重新核發，無 PENDING 殘留跨 cycle",
          all(r.reason != ARB.R_PENDING_AUTHORIZATION for r in recs))
    check("★★ T2. 票在同一 cycle 內就被處置（chain 未注入 executor → 當場作廢）",
          h.arbiter.pending is None or h.arbiter.pending.usable is False)
    check("★★ T3. 因此 TTL 10s 短於 cadence 30s **不構成衝突**"
          "（票的生命週期在單一 cycle 內，不跨輪）",
          all(r.executed is False for r in recs)
          and h.chain.dispatch_count == 0)
    check("★★ T4. 本輪未修改 TTL / cadence 任一值",
          c.authorization_ttl_sec == 10.0 and c.decision_interval_sec == 30.0
          and c.min_switch_interval_sec is None)


# ======================================================================
# N —— scope / GAP-3 不回歸
# ======================================================================
def test_N_scope_and_gap3():
    print("\n[N] scope：只處理 GAP-1 / GAP-2；GAP-3 不得回歸")
    src, _r, w = SVC.build_production_stack(client=None, meter_client=None)
    observer = src.__self__.arbiter.observer
    check("★★ N1. GAP-3 未回歸：TariffProvider 仍已接上",
          isinstance(observer.tariff_provider, TP.TariffProvider)
          and w.sources.get("tariff_provider") == "WIRED")
    sample = observer._local_now()
    check(f"★★ N2. GAP-3 未回歸：Asia/Taipei aware（tzinfo={sample.tzinfo}）",
          sample.tzinfo is not None
          and observer.tariff_provider.timezone_name == "Asia/Taipei")
    naive = observer.tariff_provider.observe(
        __import__("datetime").datetime(2026, 7, 15, 14))
    check(f"★★ N3. GAP-3 未回歸：naive 仍 Fail Closed（{naive.reason}）",
          naive.reason == TP.TP_NAIVE_DATETIME)
    check(f"★★ N4. GAP-3 未回歸：年度行事曆仍為 "
          f"{tuple(AC.PRODUCTION_PROVIDER.known_years)}",
          tuple(AC.PRODUCTION_PROVIDER.known_years) == (2026, 2027)
          and observer.tariff_provider.holiday_provider
          is AC.PRODUCTION_PROVIDER)

    check("★★ N5. GAP-4 未動：AUDIT_LIMIT 仍 200、未新增 journal",
          RT.AUDIT_LIMIT == 200
          and not hasattr(RT.AutoControlRuntime(), "journal"))
    check("★★ N6. GAP-6 未動：未新增 process lifecycle enum",
          not (RT.RUNTIME_STATES & {"INITIALIZING", "READY", "RUNNING",
                                    "STOPPING", "STOPPED"}))
    check("★★ N7. 未定案參數仍為 None",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None
          and CFG.DEFAULT_CONTROL_CONFIG.meter_stale_grace_sec is None)
    check("★★ N8. Runtime / Arbiter 的公開介面未因 cadence 改動而變",
          "waiter" not in
          io.open(RT.__file__, encoding="utf-8").read()
          and "waiter" not in
          io.open(ARB.__file__, encoding="utf-8").read())
    check("★★ N9. Authority 判定邏輯未動（TTL 常數仍在 control_authority）",
          "authority_ttl_sec" in io.open(CA.__file__, encoding="utf-8").read())


# ======================================================================
def main():
    for fn in (test_A_default_from_config, test_B_follows_config,
               test_C_explicit_override, test_D_invalid_interval_fail_closed,
               test_E_waiter_injection, test_F_tick_wait_counts,
               test_S_stop_event, test_H_shutdown, test_T_ttl_vs_cadence,
               test_N_scope_and_gap3):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
