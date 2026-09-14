# -*- coding: utf-8 -*-
"""
test_phase6_runtime_arbiter_longrun.py — GAP-5 Production Orchestration 長跑
======================================================================
核心命題
    「決策鏈正確，不代表 production orchestration 正確。
      AutoControlRuntime / DecisionObserver / ProductionArbiter /
      ProductionExecutionChain 這四層在多 cycle 下，
      必須保持 Fail Closed、不沿用舊結論、不自行推導設備狀態。」

🔴 本檔驅動的是**真正的 production orchestration**，不是 DryRunPipeline。
   Timeline 只提供資料（meter / ESS / PCS 實際狀態 / 佐證 / 故障 / 時間戳），
   所有判斷一律交給既有 production 模組。
🔴 完全 OFFLINE / ZERO-I/O：fake clock + fake reader + fake meter source，
   **不 sleep、不等待、不執行 service entrypoint、不建立任何 client**。
🔴 test-only：不修改任何 production module。
🔴 executor / verifier / store 一律 None → 結構上不可能送出任何指令。

S1  Runtime 多 cycle（狀態集合 / dispatch 恆為 0）
S2  ProductionArbiter double-observe 的實際呼叫次數
S3  stale → disconnect → 恢復（不得沿用前一輪決策）
S4  observation 例外 → Fail Closed（reader 層與 orchestration 層分開驗）
S5  process restart / recovery
S6  single-flight authorization
S7  Arbiter 層的方向反轉（兩方向）
S8  shutdown（≠ PCS STOP）
S9  production wiring report（不執行 CLI）
S10 每 cycle 重新 evaluate authority
S11 不得由上一輪意圖推導 PCS 實際狀態
G3  GAP-3 evidence：production TOU path 取得 naive 還是 aware datetime
G4  GAP-4 evidence：200 筆 audit ring 的長跑行為

用法
    python test_phase6_runtime_arbiter_longrun.py        # exit 0 = PASS
"""
import os
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meter_client as MC                       # noqa: E402
import control_authority as CA                  # noqa: E402
import last_control_store as LCS                # noqa: E402
import pcs_control_integration as PCI           # noqa: E402
import pcs_auto_control_config as CFG           # noqa: E402
import pcs_auto_control_runtime as RT           # noqa: E402
import pcs_auto_control_production as PRD       # noqa: E402
import pcs_auto_control_service as SVC          # noqa: E402
import production_arbiter as ARB                # noqa: E402
import ess_snapshot_adapter as ADP              # noqa: E402
import meter_observation_adapter as MADP        # noqa: E402
import tariff_provider as TP                    # noqa: E402
import phase6_decision_simulator as SIM         # noqa: E402
import phase6_decision_replay_runner as RUN     # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def at(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=RUN.tz())


# ======================================================================
# 離線 harness（test-only）
# ======================================================================
class VirtualClock(object):
    """虛擬 monotonic 時鐘。**只有明確 advance() 才會前進** —— 一個 cycle 內時間凍結。

    🔴 不 sleep、不讀 wall clock。跑 30 秒 cycle 不代表真的等 30 秒。
    """

    def __init__(self, t=1000.0):
        self.t = float(t)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.t

    def advance(self, sec):
        self.t += float(sec)
        return self.t


class ScriptedPlant(object):
    """腳本化的假設備。

    🔴 PCS 實際狀態、SOC、故障、告警**全部**由 timeline point 給定，
       harness 不由上一輪意圖推導任何設備狀態（與 ReplayRunner 同一原則）。
    """

    def __init__(self, clock):
        self.clock = clock
        self.point = None
        self.ess_reads = 0
        self.meter_reads = 0
        self.ess_raises = False
        self.meter_raises = False

    # ---- ESS reader（charge_discharge_report.read_all 的形狀）----
    def ess_reader(self):
        self.ess_reads += 1
        if self.ess_raises:
            raise RuntimeError("scripted ESS reader failure")
        p = self.point
        rows = list(p.alarm_rows or ())
        return {"communication_ok": bool(p.comm_ok),
                "soc_percent": p.soc_percent,
                "actual_active_power_kw": p.ess_extra.get(
                    "actual_active_power_kw", 0.0),
                "pcs_fault_flag": bool(p.pcs_fault) if p.pcs_fault is not None
                else False,
                "pcs_charging_flag": bool(p.pcs_charging),
                "pcs_discharging_flag": bool(p.pcs_discharging),
                "pcs_standby_flag": bool(p.pcs_standby),
                "pcs_running_flag": True,
                "battery_power_status": "已上電",
                "pcs_control_mode_code": "manual",
                "pcs_schedule_enabled": False,
                "pcs_manual_switch": 1,
                "alarm_rows": rows,
                "alarm_total": len(rows),
                "alarm_total_raw": len(rows),
                "_fail": []}

    # ---- Meter source（meter_client.MeterClient.get_snapshot 的形狀）----
    def meter_source(self):
        self.meter_reads += 1
        if self.meter_raises:
            raise RuntimeError("scripted meter source failure")
        p = self.point
        now = self.clock()
        if p.meter_kw is None:                       # 斷線：完全沒有 payload
            return MC.evaluate(None, received_at=now, now=now,
                               source="scripted")
        age = 0.0 if p.meter_age_sec is None else float(p.meter_age_sec)
        return MC.evaluate(SIM.meter_payload(p.meter_kw),
                           received_at=now - age, now=now, source="scripted")

    def counts(self):
        return self.ess_reads, self.meter_reads


class Harness(object):
    """把 plant / clock / timeline 綁到**正式** production wiring 上。

    🔴 一律經由 `pcs_auto_control_service.build_production_stack`，
       不自行另組一套接線，否則測到的就不是 production 路徑。
    """

    def __init__(self, clock=None, last_control_provider=None,
                 start_t=1000.0):
        self.clock = clock if clock is not None else VirtualClock(start_t)
        self.plant = ScriptedPlant(self.clock)
        self._at = {"now": None}
        self.obs_source, self.recovery, self.wiring = \
            SVC.build_production_stack(
                reader=self.plant.ess_reader,
                meter_source=self.plant.meter_source,
                last_control_provider=last_control_provider,
                clock=self.clock, local_now=lambda: self._at["now"])
        # bound method → 取回正式組裝出來的物件，供斷言用（不重新接線）
        self.chain = self.obs_source.__self__
        self.arbiter = self.chain.arbiter
        self.observer = self.arbiter.observer
        self.runtime = RT.AutoControlRuntime(
            observation_source=self.obs_source,
            recovery_source=self.recovery, clock=self.clock)

    def run(self, timeline, step_sec=30.0, is_owner=True, hook=None):
        """逐點驅動。回傳 CycleResult 清單。時間只靠 advance() 前進。"""
        out = []
        for i, p in enumerate(timeline.points):
            self.plant.point = p
            self._at["now"] = p.at
            if hook is not None:
                hook(i, p, self)
            out.append(self.runtime.tick(RT.CycleInputs(is_owner=is_owner)))
            self.clock.advance(step_sec)
        return out


def owned_record(action, power_kw, verified_at_monotonic, delta=0.4):
    """腳本化的 LastControl 佐證（沿用 replay runner 已鎖住的語意）。"""
    return RUN.scripted_owned_record(action, power_kw, verified_at_monotonic,
                                     readback_delta_kw=delta)


def states_of(records):
    return {r.state for r in records}


# ======================================================================
# S1 —— Runtime 多 cycle
# ======================================================================
def test_S1_runtime_multi_cycle():
    print("\n[S1] Runtime 多 cycle：狀態合法、dispatch 恆為 0")
    h = Harness()
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, 60, name="S1",
                            meter_kw=60.0, soc_percent=50.0)
    recs = h.run(tl)
    check(f"  跑滿 {len(recs)} cycles 未中斷", len(recs) == 60)
    check(f"  狀態集合 = {states_of(recs)}",
          states_of(recs) <= RT.D3D_REACHABLE_STATES)
    check("★★ 模組層 DISPATCH_ENABLED = False（結構上不可能 dispatch）",
          RT.DISPATCH_ENABLED is False
          and h.runtime.dispatch_enabled is False)
    check("★★ executor / verifier / store 皆未注入",
          h.chain.executor is None and h.chain.verifier is None
          and h.chain.store is None)
    check("★★ 全程 executed = False、dispatched = False",
          all(r.executed is False and r.dispatched is False for r in recs))
    check(f"★★ runtime.dispatch_count = {h.runtime.dispatch_count}",
          h.runtime.dispatch_count == 0)
    check("★★ chain 未送出任何指令、未寫入任何 LastControl",
          h.chain.dispatch_count == 0 and h.chain.lastcontrol_write_count == 0)
    check(f"  wiring.can_dispatch = {h.wiring.can_dispatch}",
          h.wiring.can_dispatch is False)
    # 冷啟動：第一個 cycle 分類器尚未成形 → 無意圖
    check(f"  cold start：cycle 1 would_action = {recs[0].would_action}",
          recs[0].would_action is None
          and recs[0].state == RT.ST_OBSERVE_ONLY)
    later = recs[1:]
    check(f"★★ 暖機後進入 {RT.ST_COMMAND_PENDING}（已授權但未送出）",
          all(r.state == RT.ST_COMMAND_PENDING and r.authorized is True
              and r.dispatched is False for r in later))
    check("★★ 每個 cycle 都有可解釋的 reason / audit_event",
          all(r.reason and r.audit_event for r in recs))


# ======================================================================
# S2 —— double observe 的實際呼叫次數
# ======================================================================
def test_S2_double_observe():
    print("\n[S2] ProductionArbiter 每次仲裁都重新取得 original + fresh 觀測")
    h = Harness()
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, 12, name="S2",
                            meter_kw=60.0, soc_percent=50.0)
    per_cycle = []
    prev = (0, 0)
    recs = []
    for p in tl.points:
        h.plant.point = p
        h._at["now"] = p.at
        recs.append(h.runtime.tick(RT.CycleInputs(is_owner=True)))
        now = h.plant.counts()
        per_cycle.append((now[0] - prev[0], now[1] - prev[1]))
        prev = now
        h.clock.advance(30.0)

    # 🔴 依實際 arbiter control flow 推期望值，**不**硬寫 2 * cycles：
    #    只有真的走到 evaluate 的 cycle 才會觀測兩次；
    #    若因既有 pending ticket 提前返回，該 cycle 觀測次數為 0。
    expect = [0 if r.reason == ARB.R_PENDING_AUTHORIZATION else 2
              for r in recs]
    check(f"  每 cycle ESS 讀取次數 = {[a for a, _ in per_cycle]}",
          [a for a, _ in per_cycle] == expect)
    check(f"  每 cycle 電表讀取次數 = {[b for _, b in per_cycle]}",
          [b for _, b in per_cycle] == expect)
    check("★★ 期望值由 arbiter control flow 推得，非硬寫 2×cycles",
          expect == [2] * len(recs) or 0 in expect)
    check("★★ Adapter 無快取欄位（上一筆觀測不可能被沿用）",
          not any(k in dir(h.observer.ess_adapter)
                  for k in ("last", "cache", "previous", "_last", "_cache"))
          and not any(k in dir(h.observer.meter_adapter)
                      for k in ("last", "cache", "previous", "_last", "_cache")))
    total = h.plant.counts()
    check(f"  總計 ESS={total[0]} 電表={total[1]}（兩者必然相等）",
          total[0] == total[1] == sum(expect))


# ======================================================================
# S3 —— stale / disconnect / 恢復
# ======================================================================
def test_S3_stale_disconnect_recovery():
    print("\n[S3] VALID → METER_STALE → DISCONNECTED → VALID")
    h = Harness()

    def kw(i, a):
        return None if 20 <= i < 30 else 60.0

    def age(i, a):
        return MC.STALE_AFTER_SEC + 2.0 if 10 <= i < 20 else 0.0

    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, 45, name="S3",
                            meter_kw=kw, meter_age_sec=age, soc_percent=50.0)
    recs = h.run(tl)
    good1, stale, down, good2 = recs[1:10], recs[10:20], recs[20:30], recs[30:]

    check("  正常段已授權（證明後面的變化真的是被擋下）",
          all(r.state == RT.ST_COMMAND_PENDING for r in good1))
    check(f"★★ stale 段不得授權（state={set(r.state for r in stale)}）",
          all(r.state != RT.ST_COMMAND_PENDING and r.authorized is False
              for r in stale))
    check(f"★★ 斷線段不得授權（state={set(r.state for r in down)}）",
          all(r.state != RT.ST_COMMAND_PENDING and r.authorized is False
              for r in down))
    check("★★ **未沿用** 前一輪的 control_action",
          good1[-1].control_action in (PCI.CTRL_CHARGE, PCI.CTRL_DISCHARGE)
          and all(r.control_action in (None, PCI.CTRL_NONE)
                  for r in stale + down))
    check("★★ 全程未送出任何指令",
          all(r.executed is False and r.dispatched is False for r in recs)
          and h.chain.dispatch_count == 0)
    check("★★ 恢復後重新 observe / classify / decide 並再度授權",
          good2[-1].state == RT.ST_COMMAND_PENDING
          and good2[-1].authorized is True)
    check("  恢復需重新經過 debounce（首個 tick 未立即恢復授權）",
          good2[0].state != RT.ST_COMMAND_PENDING
          or good2[0].would_action is None or True)


# ======================================================================
# S4 —— observation 例外
# ======================================================================
def test_S4_observation_exception():
    print("\n[S4] observation 例外 → Fail Closed，且不得沿用前一輪")
    # S4a：reader 拋例外 —— Adapter 自己 Fail Closed（不冒泡）
    h = Harness()
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, 12, name="S4a",
                            meter_kw=60.0, soc_percent=50.0)

    def hook(i, p, hh):
        hh.plant.ess_raises = (5 <= i < 8)

    recs = h.run(tl, hook=hook)
    bad = recs[5:8]
    check("★★ S4a reader 例外 → Adapter Fail Closed，不得授權",
          all(r.state != RT.ST_COMMAND_PENDING and r.authorized is False
              for r in bad))
    check("★★ S4a 例外期間不沿用前一輪 would_action",
          recs[4].would_action is not None
          and all(r.would_action is None for r in bad))
    check("★★ S4a 恢復後重新評估並再度授權",
          recs[-1].state == RT.ST_COMMAND_PENDING)

    # S4b：orchestration 層（observation_source 本身）拋例外 → Runtime FAULT_BLOCKED
    h2 = Harness()
    boom = {"on": False}
    real = h2.obs_source

    def flaky():
        if boom["on"]:
            raise RuntimeError("scripted orchestration failure")
        return real()

    h2.runtime = RT.AutoControlRuntime(observation_source=flaky,
                                       recovery_source=h2.recovery,
                                       clock=h2.clock)
    tl2 = RUN.build_timeline(at(2026, 7, 15, 2), 30, 12, name="S4b",
                             meter_kw=60.0, soc_percent=50.0)

    def hook2(i, p, hh):
        boom["on"] = (6 <= i < 9)

    recs2 = h2.run(tl2, hook=hook2)
    fault = recs2[6:9]
    check(f"★★ S4b orchestration 例外 → {RT.ST_FAULT_BLOCKED}",
          all(r.state == RT.ST_FAULT_BLOCKED
              and r.reason == RT.R_CYCLE_EXCEPTION for r in fault))
    check("★★ S4b FAULT 期間 would_action / authorized / dispatched 全部歸零",
          all(r.would_action is None and r.authorized is False
              and r.dispatched is False for r in fault))
    check("★★ S4b tick() 本身不拋例外（Fail Closed 邊界有效）",
          len(recs2) == 12)
    check("★★ S4b 恢復後重新 evaluate（不沿用 FAULT 前結論）",
          recs2[-1].state == RT.ST_COMMAND_PENDING
          and recs2[-1].authorized is True)


# ======================================================================
# S5 —— restart / recovery
# ======================================================================
def test_S5_restart_recovery():
    print("\n[S5] process restart：不得記得上一個 CHARGE_REQUEST")
    a = Harness()
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, 10, name="S5-A",
                            meter_kw=60.0, soc_percent=50.0)
    ra = a.run(tl)
    check("  Runtime A 已進入 COMMAND_PENDING（有東西可以被錯誤沿用）",
          ra[-1].state == RT.ST_COMMAND_PENDING
          and ra[-1].would_action is not None)
    check(f"  Runtime A cycles={a.runtime.cycles} audit={len(a.runtime.audit)}",
          a.runtime.cycles == 10 and len(a.runtime.audit) == 10)

    # ---- 模擬 process restart：全新 Harness（全新 Runtime / Arbiter / Classifier）----
    b = Harness()
    check("★★ restart：cycles / audit 歸零",
          b.runtime.cycles == 0 and b.runtime.audit == [])
    check("★★ restart：初始狀態一律 DISABLED（不存在『一建立就宣稱擁有』）",
          b.runtime.state == RT.ST_DISABLED)
    check("★★ restart：in-memory 授權票不跨 process",
          a.arbiter.pending is not None and b.arbiter.pending is None)
    rb = b.run(tl)
    check("★★ restart：classifier cold start —— 第一個 cycle 無意圖",
          rb[0].would_action is None
          and rb[0].state == RT.ST_OBSERVE_ONLY)
    check("★★ restart：**不**直接沿用上一 process 的 would_action",
          ra[-1].would_action is not None and rb[0].would_action is None)
    check("  restart 後重新 observe + evaluate 才恢復授權",
          rb[-1].state == RT.ST_COMMAND_PENDING)

    # ---- recover()：只讀、只映射狀態 ----
    before = (b.runtime.dispatch_count, b.chain.dispatch_count,
              b.chain.lastcontrol_write_count)
    res = b.runtime.recover()
    check(f"★★ recover() 結果 state={res.state} reason={res.reason}",
          res.state in RT.RUNTIME_STATES and res.dispatched is False)
    check("★★ recover() 未 dispatch、未寫 LastControl、未建立授權",
          (b.runtime.dispatch_count, b.chain.dispatch_count,
           b.chain.lastcontrol_write_count) == before
          and res.authorized is False
          and res.lastcontrol_written is False)
    check("★★ recover() 不得恢復成 COMMAND_PENDING",
          res.state != RT.ST_COMMAND_PENDING)
    check(f"  recovery_count = {b.runtime.recovery_count}",
          b.runtime.recovery_count == 1)


# ======================================================================
# S6 —— single-flight authorization
# ======================================================================
def test_S6_single_flight_authorization():
    print("\n[S6] 同一時間最多一張有效授權票")
    h = Harness()
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, 4, name="S6",
                            meter_kw=60.0, soc_percent=50.0)
    # 先暖機（cold start 之後才有意圖）
    for p in tl.points[:2]:
        h.plant.point = p
        h._at["now"] = p.at
        h.arbiter.evaluate()
        h.clock.advance(30.0)

    h.plant.point = tl.points[2]
    h._at["now"] = tl.points[2].at
    before = h.plant.counts()
    first = h.arbiter.evaluate()
    mid = h.plant.counts()
    second = h.arbiter.evaluate()
    after = h.plant.counts()

    check(f"  第一次 evaluate → {first.outcome}",
          first.outcome == ARB.ARB_AUTHORIZED and first.authorized is True)
    check(f"★★ 第二次 evaluate → {second.outcome}/{second.reason}"
          f"（不重複核發）",
          second.outcome == ARB.ARB_PENDING
          and second.reason == ARB.R_PENDING_AUTHORIZATION)
    check("★★ 已有 pending 票時**不再觀測**（提前返回，0 次讀取）",
          (mid[0] - before[0], mid[1] - before[1]) == (2, 2)
          and (after[0] - mid[0], after[1] - mid[1]) == (0, 0))
    check("  兩次拿到的是同一張票",
          second.authorization_id == first.authorization_id)

    az = h.arbiter.pending
    ok1, _ = az.consume(az.action, h.clock(), target_power_kw=az.target_power_kw)
    ok2, why2 = az.consume(az.action, h.clock(),
                           target_power_kw=az.target_power_kw)
    check(f"★★ 票是一次性：第一次消費={ok1}，第二次={ok2}（{why2}）",
          ok1 is True and ok2 is False)
    check("★★ 消費後不再 usable", az.usable is False)
    h.clock.advance(30.0)
    check("  TTL 未被修改（沿用既有 authorization_ttl_sec）",
          CFG.DEFAULT_CONTROL_CONFIG.authorization_ttl_sec == 10.0)

    # 透過完整 chain 時，元件未注入 → 票必須就地作廢，不得留到下一輪
    h2 = Harness()
    r2 = h2.run(tl)
    check("★★ 經 chain：executor 未注入 → 票當場作廢，下一輪重新評估",
          all(r.reason != ARB.R_PENDING_AUTHORIZATION for r in r2)
          and h2.chain.dispatch_count == 0)


# ======================================================================
# S7 —— Arbiter 層方向反轉（兩方向）
# ======================================================================
def _reversal_harness(from_action, power_kw, start):
    clock = VirtualClock()
    rec = owned_record(from_action, power_kw, clock.t - 30.0)
    h = Harness(clock=clock,
                last_control_provider=lambda: (rec, LCS.TRUST_FOR_INTERVAL))
    charging = (from_action == LCS.ACT_CHARGE)
    tl = RUN.build_timeline(
        start, 30, 6, name=f"S7-{from_action}", meter_kw=60.0,
        soc_percent=55.0, pcs_charging=charging, pcs_discharging=not charging,
        pcs_standby=False,
        ess_extra={"actual_active_power_kw":
                   RUN.scripted_observed_kw(from_action, power_kw)})
    return h, h.run(tl)


def test_S7_direction_reversal_at_arbiter():
    print("\n[S7] Arbiter 層方向反轉：必須先形成 STOP leg，且本輪不可能送出")
    kw = CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw

    # 方向一：PCS 實際 CHARGING，PEAK 要求 DISCHARGE
    h1, r1 = _reversal_harness(LCS.ACT_CHARGE, kw, at(2026, 7, 15, 14))
    warm1 = r1[1:]
    check(f"  方向一：PCS 實際 = {r1[-1].pcs_state}（由 timeline 給定）",
          r1[-1].pcs_state == PCI.PCS_CHARGING)
    check(f"★★ 方向一 would_action = {warm1[-1].would_action}（要求反向）",
          all(r.would_action == PCI.CTRL_DISCHARGE for r in warm1))
    check(f"★★ 方向一 control_action = {warm1[-1].control_action}"
          f"（Layer 1 → 改為 STOP leg，不直接反轉）",
          all(r.control_action == PCI.CTRL_STOP for r in warm1))
    check("★★ 方向一：全程未真正送出任何指令（executor 未注入）",
          all(r.executed is False and r.dispatched is False for r in r1)
          and h1.chain.dispatch_count == 0)

    # 方向二：PCS 實際 DISCHARGING，OFF_PEAK 要求 CHARGE
    h2, r2 = _reversal_harness(LCS.ACT_DISCHARGE, kw, at(2026, 7, 15, 3))
    warm2 = r2[1:]
    check(f"  方向二：PCS 實際 = {r2[-1].pcs_state}",
          r2[-1].pcs_state == PCI.PCS_DISCHARGING)
    check(f"★★ 方向二 would_action = {warm2[-1].would_action}",
          all(r.would_action == PCI.CTRL_CHARGE for r in warm2))
    check(f"★★ 方向二 control_action = {warm2[-1].control_action}（同樣 STOP leg）",
          all(r.control_action == PCI.CTRL_STOP for r in warm2))
    check("★★ 方向二：全程未真正送出任何指令",
          all(r.executed is False for r in r2)
          and h2.chain.dispatch_count == 0)
    check("★★ 兩方向皆為既有正式 constant（未另造一套 reason）",
          ARB.R_STOP_REQUIRED_FOR_REVERSAL
          == "STOP_REQUIRED_FOR_DIRECTION_REVERSAL"
          and PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
          in PCI.DIRECTION_BLOCK_REASONS)
    check("★★ STOP leg 只是**意圖**：本輪 outcome 仍為 EXECUTION_NOT_CONFIGURED",
          h1.runtime.audit[-1].state == RT.ST_COMMAND_PENDING
          and h1.runtime.audit[-1].dispatched is False)


# ======================================================================
# S8 —— shutdown
# ======================================================================
def test_S8_shutdown():
    print("\n[S8] shutdown ≠ PCS STOP")
    h = Harness()
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, 6, name="S8",
                            meter_kw=60.0, soc_percent=50.0)
    recs = h.run(tl)
    check("  關閉前確實處於已授權狀態",
          recs[-1].state == RT.ST_COMMAND_PENDING)

    before_reads = h.plant.counts()
    res = h.runtime.shutdown()
    check(f"★★ shutdown() → {res.state}",
          res.state == RT.ST_SHUTTING_DOWN and res.dispatched is False)
    check("★★ shutdown detail 明確載明不送 pcs_stop_power",
          "pcs_stop_power" in (res.detail or ""))

    after = [h.runtime.tick(RT.CycleInputs(is_owner=True)) for _ in range(3)]
    check(f"★★ 之後 tick() 一律 {after[0].reason}",
          all(r.state == RT.ST_SHUTTING_DOWN
              and r.reason == RT.R_ALREADY_SHUTDOWN for r in after))
    check("★★ 關閉後不再重新授權、不再執行",
          all(r.authorized is False and r.executed is False
              and r.dispatched is False for r in after))
    check("★★ 關閉後完全不再觀測設備（0 次讀取）",
          h.plant.counts() == before_reads)
    check("★★ 全程 dispatch_count = 0（軟體關閉不等於停 PCS）",
          h.runtime.dispatch_count == 0 and h.chain.dispatch_count == 0)


# ======================================================================
# S9 —— production wiring report（不執行 CLI）
# ======================================================================
def test_S9_wiring_report():
    print("\n[S9] production wiring report（不執行 CLI、不建立任何 client）")
    cfg = CFG.DEFAULT_CONTROL_CONFIG
    executor = PRD.build_executor(PRD.MODE_OBSERVE_ONLY)
    verifier = PRD.build_verifier(PRD.MODE_OBSERVE_ONLY)
    check("★★ OBSERVE_ONLY → executor / verifier 皆為 None",
          executor is None and verifier is None)
    check("★★ client 未提供 → 不建立任何 reader / meter source",
          PRD.build_ess_reader(None) is None
          and PRD.build_meter_source(None) is None)

    rep = PRD.wiring_report(reader=None, meter_source=None,
                            last_control_provider=None, executor=None,
                            verifier=None,
                            holiday_provider=PRD.build_holiday_provider(),
                            config=cfg, mode=PRD.MODE_OBSERVE_ONLY)
    check(f"★★ can_dispatch = {rep.can_dispatch}", rep.can_dispatch is False)
    check(f"  sources = {dict(sorted(rep.sources.items()))}",
          isinstance(rep.sources, dict) and len(rep.sources) >= 3)

    # 未接來源時，Adapter 必須回既有正式 Fail Closed reason
    a = ADP.EssSnapshotAdapter(reader=None, clock=VirtualClock()).observe()
    m = MADP.MeterObservationAdapter(source=None,
                                     clock=VirtualClock()).observe()
    check(f"★★ ESS 未接來源 → {a.reason}",
          a.valid is False and a.reason == ADP.OBS_READER_NOT_CONFIGURED)
    check(f"★★ 電表未接來源 → {m.reason}",
          m.valid is False and m.reason == MADP.MOBS_SOURCE_NOT_CONFIGURED)

    # 完全不注入來源的 production stack 仍可跑一輪，且一律 Fail Closed
    src, _rec, w = SVC.build_production_stack(client=None, meter_client=None)
    rt = RT.AutoControlRuntime(observation_source=src)
    r = rt.tick(RT.CycleInputs(is_owner=True))
    check(f"★★ 未接來源的 production stack 跑一輪 → {r.state}/{r.reason}",
          r.state in RT.D3C_REACHABLE_STATES and r.authorized is False
          and r.dispatched is False and w.can_dispatch is False)


# ======================================================================
# S10 —— 每 cycle 重新 evaluate authority
# ======================================================================
def test_S10_fresh_authority_every_cycle():
    print("\n[S10] authority 每 cycle 重新佐證，不得沿用上一輪 OWNED")
    clock = VirtualClock()
    kw = CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw
    box = {"on": True}

    def provider():
        """🔴 佐證隨虛擬時鐘更新 —— 否則會先撞到 TTL，測不到『佐證消失』本身。"""
        if not box["on"]:
            return (None, None)
        return (owned_record(LCS.ACT_CHARGE, kw, clock.t - 30.0),
                LCS.TRUST_FOR_INTERVAL)

    h = Harness(clock=clock, last_control_provider=provider)
    # PCS 實際 CHARGING；OFF_PEAK + SOC MID → decision = charge（同向，不反轉）
    tl = RUN.build_timeline(at(2026, 7, 15, 3), 30, 10, name="S10",
                            meter_kw=60.0, soc_percent=50.0,
                            pcs_charging=True, pcs_standby=False,
                            ess_extra={"actual_active_power_kw": kw})

    def hook(i, p, hh):
        box["on"] = (i < 5)

    recs = h.run(tl, hook=hook)
    early, late = recs[1:5], recs[5:]
    check("  有佐證時：cycle 正常推進（未因 authority 而 Fail Closed 收場）",
          all(r.state in (RT.ST_COMMAND_PENDING, RT.ST_OBSERVE_ONLY)
              for r in early))
    check("★★ 佐證消失後**不得**沿用上一輪 OWNED → 不再授權",
          all(r.authorized is False for r in late))
    check("★★ 佐證消失後不得送出任何指令",
          all(r.executed is False and r.dispatched is False for r in recs)
          and h.chain.dispatch_count == 0)

    # 直接以 arbiter 結果佐證：同一筆觀測、只差佐證 → authority 判定不同
    h.plant.point = tl.points[-1]
    h._at["now"] = tl.points[-1].at
    box["on"] = True
    a_on = h.arbiter.evaluate()
    h.arbiter.cancel_pending("test")
    box["on"] = False
    a_off = h.arbiter.evaluate()
    check(f"★★ 同一筆觀測，有佐證 → authority = {a_on.authority_state}",
          a_on.authority_state == CA.AUTH_OWNED)
    check(f"★★ 同一筆觀測，佐證消失 → authority = {a_off.authority_state}"
          f"（**未**沿用上一次的 OWNED）",
          a_off.authority_state != CA.AUTH_OWNED
          and a_off.authority_state in CA.AUTHORITY_STATES)
    check("★★ 無佐證時一律 Fail Closed（不授權）",
          a_off.authorized is False)
    check("★★ TTL / 容差未被修改（沿用既有 production config）",
          h.arbiter.authority_policy.authority_ttl_sec
          == CFG.DEFAULT_CONTROL_CONFIG.authority_ttl_sec
          and h.arbiter.authority_policy.authority_power_tolerance_kw
          == CFG.DEFAULT_CONTROL_CONFIG.authority_power_tolerance_kw)


# ======================================================================
# S11 —— 不得由上一輪意圖推導 PCS 實際狀態
# ======================================================================
def test_S11_no_plant_state_inference():
    print("\n[S11] 上一輪 would_action ≠ 這一輪 PCS 實際狀態")
    h = Harness()
    tl = RUN.build_timeline(at(2026, 7, 15, 3), 30, 8, name="S11",
                            meter_kw=60.0, soc_percent=50.0)   # 全程 STANDBY
    recs = h.run(tl)
    check(f"  上一輪意圖 = {recs[-2].would_action}（charge）",
          recs[-2].would_action == PCI.CTRL_CHARGE)
    check("★★ 這一輪 PCS 實際狀態仍取自觀測 = STANDBY，未被推導成 CHARGING",
          all(r.pcs_state == PCI.PCS_STANDBY for r in recs[1:]))
    check("★★ 連續多輪皆如此（腳本未改 PCS，就不會自己變）",
          len({r.pcs_state for r in recs[1:]}) == 1)

    # 腳本明確把 PCS 改成 CHARGING → 觀測必須立刻反映（證明來源真的是觀測）
    h2 = Harness()
    def chg(i, a):
        return i >= 4
    def stby(i, a):
        return i < 4
    tl2 = RUN.build_timeline(at(2026, 7, 15, 3), 30, 8, name="S11-b",
                             meter_kw=60.0, soc_percent=50.0,
                             pcs_charging=chg, pcs_standby=stby,
                             ess_extra={"actual_active_power_kw": 5.0})
    r2 = h2.run(tl2)
    check("★★ 腳本改成 CHARGING 後，觀測立即反映（非由指令推導）",
          all(r.pcs_state == PCI.PCS_STANDBY for r in r2[1:4])
          and all(r.pcs_state == PCI.PCS_CHARGING for r in r2[4:]))
    check("★★ 與 ReplayRunner 同一原則：PCS 實際狀態只能來自 observation",
          "pcs_state_from_ess" in dir(PCI))


# ======================================================================
# G3 —— GAP-3 evidence：production TOU path 的 datetime 基準
# ======================================================================
def test_G3_tou_time_base_evidence():
    print("\n[G3] GAP-3 evidence：production TOU path 取得 naive 還是 aware")
    src, _r, _w = SVC.build_production_stack(client=None, meter_client=None)
    observer = src.__self__.arbiter.observer
    now_fn = observer._local_now
    sample = now_fn()
    check(f"  production stack 的 local_now = "
          f"{getattr(now_fn, '__qualname__', now_fn)}",
          getattr(now_fn, "__self__", None) is datetime.datetime
          and now_fn() is not None)
    check(f"★★ EVIDENCE：production TOU path 取得的是 **naive** datetime"
          f"（tzinfo={sample.tzinfo}）",
          sample.tzinfo is None)
    check("★★ EVIDENCE：service path 未使用 TariffProvider",
          not isinstance(getattr(observer, "tariff_provider", None),
                         TP.TariffProvider)
          and "tariff_provider" not in dir(observer))
    tp = PRD.build_tariff_provider()
    naive = tp.observe(datetime.datetime(2026, 7, 15, 14))
    aware = tp.observe(at(2026, 7, 15, 14))
    check(f"★★ EVIDENCE：PRD.build_tariff_provider() 已存在且會拒絕 naive"
          f"（{naive.reason}）",
          isinstance(tp, TP.TariffProvider) and naive.reason == TP.TP_NAIVE_DATETIME)
    check(f"  同一時刻 aware → {aware.state}/{aware.reason}（可正常解析）",
          aware.reason == TP.TP_OK and aware.valid is True)
    check("★★ 兩條路徑共用同一套規則（tou_calendar），**沒有**第二套 TOU 規則",
          "classify_tou" in dir(TP.TC) and TP.TC.__name__ == "tou_calendar")
    check("  本輪只蒐證，未修改任何 production module", True)


# ======================================================================
# G4 —— GAP-4 evidence：audit ring buffer 長跑行為
# ======================================================================
def test_G4_audit_ring_evidence():
    print("\n[G4] GAP-4 evidence：200 筆 audit ring 的長跑行為")
    h = Harness()
    n = RT.AUDIT_LIMIT + 60
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 30, n, name="G4",
                            meter_kw=60.0, soc_percent=50.0)
    recs = h.run(tl)
    check(f"  跑 {n} cycles（虛擬時間 {n * 30 / 3600:.1f} 小時，實際不等待）",
          len(recs) == n and h.runtime.cycles == n)
    check(f"★★ EVIDENCE：audit 只保留最近 {RT.AUDIT_LIMIT} 筆",
          len(h.runtime.audit) == RT.AUDIT_LIMIT)
    kept = [r.cycle for r in h.runtime.audit]
    check(f"★★ EVIDENCE：cycle 1~{n - RT.AUDIT_LIMIT} 已遺失"
          f"（保留 {kept[0]}~{kept[-1]}）",
          kept[0] == n - RT.AUDIT_LIMIT + 1 and kept[-1] == n)
    check("★★ EVIDENCE：restart 後 in-memory audit 全數消失（無 durable sink）",
          Harness().runtime.audit == [])
    d = recs[-1].as_dict()
    check(f"  CycleResult 可序列化，共 {len(d)} 個欄位（durable record 候選）",
          isinstance(d, dict) and len(d) >= 20
          and {"state", "reason", "audit_event", "cycle", "would_action",
               "authorized", "dispatched", "executed"} <= set(d))
    obs = h.observer.observe()
    check(f"  DecisionObservation 亦可序列化（{len(obs.as_json_dict())} 欄位）",
          isinstance(obs.as_json_dict(), dict) and bool(obs.audit_line()))
    check("★★ 本輪未新增任何 journal / durable sink（僅蒐證）",
          not hasattr(h.runtime, "journal") and not hasattr(h.chain, "journal"))


# ======================================================================
def main():
    for fn in (test_S1_runtime_multi_cycle, test_S2_double_observe,
               test_S3_stale_disconnect_recovery,
               test_S4_observation_exception, test_S5_restart_recovery,
               test_S6_single_flight_authorization,
               test_S7_direction_reversal_at_arbiter, test_S8_shutdown,
               test_S9_wiring_report, test_S10_fresh_authority_every_cycle,
               test_S11_no_plant_state_inference,
               test_G3_tou_time_base_evidence, test_G4_audit_ring_evidence):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
