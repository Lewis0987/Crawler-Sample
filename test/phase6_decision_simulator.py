# -*- coding: utf-8 -*-
"""
phase6_decision_simulator.py — Meter-Driven Decision Simulator（OFFLINE）
======================================================================
定位：**application composition layer**，不是新的決策實作。

本檔**只負責把既有模組接起來並解釋每一筆決策**：

    meter_client.evaluate        → MeterSnapshot
    power_classifier.update      → GridPowerState（遲滯 + debounce）
    tariff_provider / 注入        → TOU
    decision_engine.decide       → DecisionResult（policy = TouArbitragePolicy）
    pcs_control_integration      → ControlRequest / DryRunResult

🔴 **零 I/O。** 不 import 也不使用 socket / requests / socketio / modbus /
   subprocess / paramiko。所有外部狀態由呼叫端注入或由 replay 檔案讀入。
   不連 HTTP / Socket.IO / Modbus / SSH / HMI / PCS API；不碰 RemoteSenders、
   external controller、pause / restore、CHARGE / DISCHARGE / PCS STOP。

🔴 **不重做任何既有模組。** 本檔沒有自己的 meter client、分類器、TOU 引擎、
   決策矩陣、safety gate、authority model 或 transition model —— 一律引用。

🔴 **功率參數的兩層分離必須保持**
       decision_policy.PolicyConfig       library 預設 = None（Fail Closed）
       pcs_auto_control_config            application 已核准值 = 5.0 / 5.0
   本檔作為 application 層，**顯式**從 `DEFAULT_CONTROL_CONFIG` 取值再注入；
   既不硬編一份 5.0，也不修改 PolicyConfig 的預設。

🔴 **不新增任何 production 門檻。** `min_switch_interval_sec` 與
   `meter_stale_grace_sec` 維持 `None`（DEFERRED / REQUIRES_POLICY_DECISION），
   本檔不填值、不推算、不提供「再容許 N 秒沿用上一筆決策」的寬限路徑 ——
   一旦既有 contract 判定 stale，就直接走既有 Fail Closed 路徑。

三種 kW 語意（嚴禁混用，本檔一律使用語意名稱而非 positive / negative）
    A. Grid Meter    + = GRID_IMPORT   − = GRID_EXPORT
    B. Battery 量測  + = CHARGE        − = DISCHARGE
    C. PCS Command   CHARGE → 負 setpoint / DISCHARGE → 正 setpoint

用法（完全離線）
    python phase6_decision_simulator.py            # 自述 + 內建情境
    python phase6_decision_simulator.py --scenarios
"""
import io
import csv
import json
import math
import argparse

import meter_client as MC
import power_classifier as PC
import tou_calendar as TC
import decision_engine as DE
import decision_policy as DP
import safety_gate as SG
import control_authority as CA
import pcs_control_integration as PCI
import pcs_auto_control_config as CFG

# ======================================================================
# 語意名稱（一律引用既有常數，不另造一套文字）
# ======================================================================
GRID_IMPORT = PC.STATE_IMPORT
GRID_EXPORT = PC.STATE_EXPORT
GRID_NEAR_ZERO = PC.STATE_NEAR_ZERO
GRID_UNKNOWN = PC.STATE_UNKNOWN

CHARGE_REQUEST = PCI.CTRL_CHARGE
DISCHARGE_REQUEST = PCI.CTRL_DISCHARGE
STOP_REQUEST = PCI.CTRL_STOP
NO_REQUEST = PCI.CTRL_NONE


# ======================================================================
# Production profile —— 已核准值只有一個來源
# ======================================================================
PROFILE_OK = "PRODUCTION_PROFILE_OK"
PROFILE_POWER_NOT_CONFIGURED = "PRODUCTION_POWER_NOT_CONFIGURED"


class ProductionProfile(object):
    """
    把 application 層已核准的功率值，顯式注入 policy library。

    🔴 **唯一的 5.0 來源是 `pcs_auto_control_config.DEFAULT_CONTROL_CONFIG`。**
       本檔不保存第二份副本、不硬編、也不修改 PolicyConfig 的 library 預設。
    🔴 若 application config 本身缺值（None / 非有限數），**不得** fallback
       成任何數字 —— 一律 Fail Closed，讓決策落在
       `POLICY_POWER_NOT_CONFIGURED`。
    """

    def __init__(self, control_config=None):
        self.control_config = (control_config if control_config is not None
                               else CFG.DEFAULT_CONTROL_CONFIG)
        self.charge_power_kw = getattr(self.control_config, "charge_power_kw", None)
        self.discharge_power_kw = getattr(self.control_config,
                                          "discharge_power_kw", None)

    @staticmethod
    def _usable(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) \
            and math.isfinite(v) and v > 0

    @property
    def configured(self):
        return self._usable(self.charge_power_kw) \
            and self._usable(self.discharge_power_kw)

    @property
    def status(self):
        return PROFILE_OK if self.configured else PROFILE_POWER_NOT_CONFIGURED

    def missing(self):
        out = []
        if not self._usable(self.charge_power_kw):
            out.append("charge_power_kw")
        if not self._usable(self.discharge_power_kw):
            out.append("discharge_power_kw")
        return out

    def policy_config(self, **over):
        """
        產生要注入 `TouArbitragePolicy` 的 `PolicyConfig`。

        🔴 config 缺值時**照樣**把 None 傳下去 —— policy 會回
           `POLICY_POWER_NOT_CONFIGURED`。這正是我們要的：忘記設定就拿到
           明確錯誤，而不是默默以某個數字運轉。
        """
        kw = dict(charge_power_kw=self.charge_power_kw,
                  discharge_power_kw=self.discharge_power_kw)
        kw.update(over)
        return DP.PolicyConfig(**kw)

    def __repr__(self):
        return ("<ProductionProfile %s charge=%r discharge=%r>"
                % (self.status, self.charge_power_kw, self.discharge_power_kw))


# ======================================================================
# 注入用的極簡 TOU holder
# ======================================================================
class ScriptedTou(object):
    """
    情境腳本用的 TOU 值。**沿用 `tou_calendar` 的正式詞彙**，不另造字串。

    真實運轉請注入 `tariff_provider.TariffProvider.observe()` 的結果 ——
    兩者對 DecisionEngine 而言是同一個 duck type（只讀 .state / .valid）。
    """

    __slots__ = ("state", "valid", "reason")

    def __init__(self, state, reason="SCRIPTED"):
        if state not in TC.VALID_TOU_STATES:
            raise ValueError("不在 tou_calendar 正式詞彙內的 TOU 值：%r" % (state,))
        self.state = state
        self.valid = (state != TC.TOU_UNKNOWN)
        self.reason = reason


# ======================================================================
# 一次決策的完整紀錄
# ======================================================================
class DecisionRecord(object):
    """
    一筆決策的**可解釋**紀錄。

    目標：任何一筆都能回答「為什麼充電 / 放電 / IDLE / BLOCKED」。
    """

    FIELDS = (
        "step", "at",
        # meter
        "meter_raw_kw", "meter_normalized_kw", "meter_direction",
        "meter_valid", "meter_stale", "meter_age_sec", "meter_reason",
        # 分類
        "grid_state", "grid_valid", "grid_reason", "grid_candidate",
        # TOU
        "tou_state", "tou_valid", "tou_reason",
        # ESS / PCS
        "soc_percent", "soc_band", "pcs_state", "ess_valid", "ess_stale",
        "ess_reason", "pcs_fault", "critical_alarm_rows",
        # 決策
        "decision_action", "decision_reason", "decision_target_power_kw",
        # 控制
        "requested_action", "requested_power_kw", "request_reason",
        "authority_state", "authority_allowed",
        "outcome", "blocked_reason", "would_send", "executor_called",
        "detail",
    )

    def __init__(self, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw.get(f))

    def as_dict(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def explain(self):
        """一句話說明這筆為什麼是這個結果。"""
        if self.outcome == PCI.OUT_WOULD_EXECUTE:
            return ("%s @ %s kW —— TOU=%s / grid=%s / SOC band=%s"
                    % (self.requested_action, self.requested_power_kw,
                       self.tou_state, self.grid_state, self.soc_band))
        if self.outcome == PCI.OUT_NO_CONTROL:
            return ("不送控制（%s）—— decision=%s，PCS=%s"
                    % (self.request_reason, self.decision_action, self.pcs_state))
        return ("BLOCKED（%s）—— %s；decision=%s grid=%s tou=%s"
                % (self.outcome, self.blocked_reason, self.decision_action,
                   self.grid_state, self.tou_state))

    def __str__(self):
        return ("[#%s %s] meter=%s %s | grid=%s | tou=%s | soc=%s(%s) | pcs=%s "
                "| decision=%s | req=%s | %s"
                % (self.step, self.at, self.meter_normalized_kw,
                   self.meter_direction, self.grid_state, self.tou_state,
                   self.soc_percent, self.soc_band, self.pcs_state,
                   self.decision_action, self.requested_action, self.outcome))


# ======================================================================
# 情境輸入
# ======================================================================
_UNSET = object()


class ScenarioStep(object):
    """
    一個 tick 的完整注入輸入。**沒有任何預設會連線的東西。**

    meter_payload   6160 `update` 事件的 payload dict（或 None = 從未收到）
    received_at     本機 monotonic 接收時刻
    now             評估時刻（age = now − received_at）
    ess_reading     `charge_discharge_report.read_all()` 形狀的 dict
    """

    def __init__(self, meter_payload=None, received_at=0.0, now=0.0,
                 ess_reading=None, ess_read_started_at=None,
                 ess_read_completed_at=None, tou=None,
                 pcs_mode_state=_UNSET, alarm_rows=(), alarm_source_complete=True,
                 pcs_fault=None, last_control=None, last_control_record=None,
                 last_control_trust=None, label=""):
        self.meter_payload = meter_payload
        self.received_at = received_at
        self.now = now
        self.ess_reading = ess_reading
        self.ess_read_started_at = (ess_read_started_at
                                    if ess_read_started_at is not None
                                    else now)
        self.ess_read_completed_at = (ess_read_completed_at
                                      if ess_read_completed_at is not None
                                      else self.ess_read_started_at)
        self.tou = tou
        # 🔴 不帶這個參數 = 用「手動模式已確認」的便利預設；
        #    **明確傳入 None = 讀不到控制模式**，必須原樣往下傳讓上游 Fail Closed。
        #    用 None 當「取預設」會把「拿不到狀態」變成「狀態正常」。
        self.pcs_mode_state = ({"schedule_switch": 0, "manual_switch": 1}
                               if pcs_mode_state is _UNSET else pcs_mode_state)
        self.alarm_rows = alarm_rows
        self.alarm_source_complete = alarm_source_complete
        self.pcs_fault = pcs_fault
        self.last_control = last_control
        self.last_control_record = last_control_record
        self.last_control_trust = last_control_trust
        self.label = label


def meter_payload(meter_kw, demand_kw=None, meter_state=MC.S_OK,
                  demand_state=MC.S_OK, **extra):
    """
    產生一筆符合 production 契約的 6160 payload。

    🔴 `meter_kw` 採 **Grid Meter 語意**：正值 = GRID_IMPORT、負值 = GRID_EXPORT。
       這個語意由現場實測確立（2026-09-01 外部 controller 開始約 80 kW 充電時，
       meter 由 27.92 → 108.60 kW；充電＝多向電網取電 → meter 上升）。
    """
    p = {"meter": float(meter_kw),
         "demand": float(meter_kw if demand_kw is None else demand_kw)}
    if meter_state is not None:
        p["meter_state"] = meter_state
    if demand_state is not None:
        p["demand_state"] = demand_state
    p.update(extra)
    return p


def ess_reading(soc_percent=50.0, charging=False, discharging=False,
                standby=True, communication_ok=True, pcs_fault_flag=False,
                **over):
    """`read_all()` 形狀的 ESS reading（供 `ess_snapshot_from_reading` 使用）。"""
    r = {"communication_ok": communication_ok,
         "soc_percent": soc_percent,
         "pcs_fault_flag": pcs_fault_flag,
         "battery_power_status": SG.BATT_ON,
         "pcs_charging_flag": charging,
         "pcs_discharging_flag": discharging,
         "pcs_standby_flag": standby}
    r.update(over)
    return r


# ======================================================================
# Simulator
# ======================================================================
class DecisionSimulator(object):
    """
    離線決策模擬器。**組裝既有模組，不自行決策。**

    每次 `step()` 走完整條鏈並回傳一筆可解釋的 `DecisionRecord`。
    """

    def __init__(self, profile=None, classifier_config=PC.DEFAULT_CONFIG,
                 ess_config=DE.DEFAULT_ESS_CONFIG, policy_overrides=None,
                 authority_policy=None, gate=None, executor=None):
        self.profile = profile if profile is not None else ProductionProfile()
        self.classifier = PC.PowerClassifier(config=classifier_config)
        self.ess_config = ess_config
        self._policy_clock_value = 0.0
        self.policy = DP.TouArbitragePolicy(
            config=self.profile.policy_config(**(policy_overrides or {})),
            clock=lambda: self._policy_clock_value)
        self.engine = DE.DecisionEngine(policy=self.policy,
                                        ess_config=self.ess_config)
        self.pipeline = PCI.DryRunPipeline(
            gate=gate, executor=executor,
            authority_policy=(authority_policy if authority_policy is not None
                              else CA.DEFAULT_AUTHORITY_POLICY))
        self.records = []
        self._n = 0

    # ---- 內部：分類器暖機 ----
    def prime_classifier(self, payload, start_at, samples=None, interval=None):
        """
        連續餵入同一筆 payload，讓分類器脫離 UNKNOWN。

        🔴 這**不是**放寬 —— `PowerClassifier` 的 debounce 本來就要求候選狀態
           連續維持 `debounce_sec`。單點快照永遠不足以離開 UNKNOWN，
           模擬器必須如實跨時間餵資料。
        """
        cfg = self.classifier.cfg
        interval = interval if interval is not None else PC.TICK_SEC
        if samples is None:
            samples = int(math.ceil(cfg.debounce_sec / interval)) + 2
        t = start_at
        last = None
        for _ in range(samples):
            snap = MC.evaluate(payload, received_at=t, now=t)
            last = self.classifier.update(snap, now=t)
            t += interval
        return last, t

    # ---- 主流程 ----
    def step(self, sc):
        self._n += 1
        self._policy_clock_value = sc.now

        # 1) Meter —— 既有契約，不加任何 grace
        if sc.meter_payload is None:
            snap = MC.evaluate(None, received_at=sc.received_at, now=sc.now)
        else:
            snap = MC.evaluate(sc.meter_payload, received_at=sc.received_at,
                               now=sc.now)

        # 2) 分類 —— 既有遲滯 / debounce / stale Fail Closed
        grid = self.classifier.update(snap, now=sc.now)

        # 3) TOU —— 由呼叫端注入（ScriptedTou 或 TariffProvider.observe()）
        tou = sc.tou if sc.tou is not None else ScriptedTou(TC.TOU_UNKNOWN,
                                                            "TOU_NOT_PROVIDED")

        # 4) ESS
        ess = DE.ess_snapshot_from_reading(
            sc.ess_reading, sc.ess_read_started_at, sc.ess_read_completed_at,
            now=sc.now, config=self.ess_config)

        # 5) Decision
        dec = self.engine.decide(DE.DecisionInput(grid=grid, tou=tou, ess=ess))

        # 6) Control pipeline（Authority → 方向互鎖 → Safety Gate → MockExecutor）
        inputs = PCI.PipelineInputs(
            decision=dec, ess=ess, pcs_fault=sc.pcs_fault,
            alarm_rows=sc.alarm_rows,
            alarm_source_complete=sc.alarm_source_complete,
            pcs_mode_state=sc.pcs_mode_state,
            last_control=sc.last_control,
            last_control_record=sc.last_control_record,
            last_control_trust=sc.last_control_trust)
        dry = self.pipeline.run(inputs, now=sc.now)

        rec = self._record(sc, snap, grid, tou, ess, dec, dry)
        self.records.append(rec)
        return rec

    def _soc_band(self, ess):
        """SOC band 僅供解釋用；判定本身在 policy 內，本檔不複製判定規則。"""
        soc = getattr(ess, "soc_percent", None)
        if not isinstance(soc, (int, float)) or isinstance(soc, bool) \
                or not math.isfinite(soc):
            return None
        try:
            # 🔴 只讀 latch 的現況（decide 已更新過），不在此複製任何 band 判定規則
            return self.policy._latch.band
        except Exception:
            return None

    def _record(self, sc, snap, grid, tou, ess, dec, dry):
        ctrl = dry.control_request
        auth = dry.authority_result
        blocked = None
        if dry.outcome in (PCI.OUT_AUTHORITY_BLOCKED, PCI.OUT_DIRECTION_BLOCKED,
                           PCI.OUT_SAFETY_BLOCKED):
            blocked = dry.reason
        rows = sc.alarm_rows or ()
        return DecisionRecord(
            step=self._n, at=sc.now, label=sc.label,
            meter_raw_kw=getattr(snap, "power_kw", None),
            meter_normalized_kw=getattr(snap, "power_kw", None),
            meter_direction=getattr(snap, "direction", None),
            meter_valid=getattr(snap, "valid", None),
            meter_stale=getattr(snap, "stale", None),
            meter_age_sec=getattr(snap, "age_sec", None),
            meter_reason=getattr(snap, "reason", None),
            grid_state=getattr(grid, "state", None),
            grid_valid=getattr(grid, "valid", None),
            grid_reason=getattr(grid, "reason", None),
            grid_candidate=getattr(grid, "candidate_state", None),
            tou_state=getattr(tou, "state", None),
            tou_valid=getattr(tou, "valid", None),
            tou_reason=getattr(tou, "reason", None),
            soc_percent=getattr(ess, "soc_percent", None),
            soc_band=self._soc_band(ess),
            pcs_state=PCI.pcs_state_from_ess(ess),
            ess_valid=getattr(ess, "valid", None),
            ess_stale=getattr(ess, "stale", None),
            ess_reason=getattr(ess, "reason", None),
            pcs_fault=sc.pcs_fault,
            critical_alarm_rows=len(rows),
            decision_action=dec.action,
            decision_reason=dec.reason,
            decision_target_power_kw=dec.target_power_kw,
            requested_action=(ctrl.action if ctrl else None),
            requested_power_kw=(ctrl.target_power_kw if ctrl else None),
            request_reason=(ctrl.reason if ctrl else None),
            authority_state=(getattr(auth, "state", None) if auth else None),
            authority_allowed=(getattr(auth, "allowed", None) if auth else None),
            outcome=dry.outcome,
            blocked_reason=blocked,
            would_send=dry.would_send,
            executor_called=dry.executor_called,
            detail=dry.detail,
        )

    # ---- 輸出 ----
    def to_jsonl(self):
        return "\n".join(json.dumps(r.as_dict(), ensure_ascii=False,
                                    default=str)
                         for r in self.records)


# ======================================================================
# Replay —— 唯讀解析，不指向任何 runtime artifact
# ======================================================================
REPLAY_SCHEMA_INCOMPLETE = "REPLAY_SCHEMA_INCOMPLETE"

# PrintMeterWeb（reference / shadow 實作）寫出的 CSV 標頭
PRINTMETERWEB_CSV_COLUMNS = ("Timestamp", "Power Meter (kW)",
                             "BESS Power (kW)", "Power Demand (kW)")


def parse_meter_csv(text, power_column="Power Meter (kW)",
                    demand_column="Power Demand (kW)",
                    meter_state=None, demand_state=None):
    """
    把 meter CSV 解析成 payload 序列。**純字串處理，不開檔、不連線。**

    🔴 `meter_state` / `demand_state` 預設 **None（不補）**。PrintMeterWeb 的
       CSV 沒有這兩欄，補上等於偽造 production 契約欄位。不補的結果是
       `meter_client` 判 `INVALID_SCHEMA_MISSING` —— 那是**正確**的 Fail Closed，
       也如實反映 reference 與 production schema 的落差。
       要在測試中刻意做出「有效 payload」，才顯式傳入 `MC.S_OK`。
    """
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if power_column not in row:
            raise ValueError("CSV 缺少欄位 %r（實有：%s）"
                             % (power_column, sorted(row)))
        try:
            power = float(row[power_column])
        except (TypeError, ValueError):
            continue
        demand = None
        if demand_column and row.get(demand_column) not in (None, ""):
            try:
                demand = float(row[demand_column])
            except (TypeError, ValueError):
                demand = None
        out.append(meter_payload(power, demand_kw=demand,
                                 meter_state=meter_state,
                                 demand_state=demand_state))
    return out


def parse_meter_jsonl(text, power_key="meter", demand_key="demand"):
    """
    把每行一筆 JSON 的 meter 紀錄解析成 payload 序列。

    🔴 同樣**不補** `meter_state` / `demand_state` —— 來源有才有。
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or power_key not in d:
            continue
        p = {"meter": d[power_key],
             "demand": d.get(demand_key, d[power_key])}
        for k in ("meter_state", "demand_state"):
            if k in d:
                p[k] = d[k]
        out.append(p)
    return out


# ======================================================================
# 內建情境（deterministic synthetic，不依賴任何檔案）
# ======================================================================
def _run_case(label, tou_state, meter_kw, soc, charging=False,
              discharging=False, standby=True, **sc_kw):
    """建一個 simulator、暖機分類器、跑一個 tick。回傳 (sim, record)。"""
    sim = DecisionSimulator()
    pay = sc_kw.pop("payload", None)
    if pay is None:
        pay = meter_payload(meter_kw)
    _, t = sim.prime_classifier(pay, start_at=0.0)
    sc = ScenarioStep(
        meter_payload=pay, received_at=t, now=t,
        ess_reading=ess_reading(soc_percent=soc, charging=charging,
                                discharging=discharging, standby=standby),
        tou=ScriptedTou(tou_state), label=label, **sc_kw)
    return sim, sim.step(sc)


BUILTIN_SCENARIOS = (
    ("A  OFF_PEAK + GRID_IMPORT + SOC MID", TC.TOU_OFF_PEAK, 60.0, 50.0),
    ("B  PEAK + GRID_IMPORT + SOC MID", TC.TOU_PEAK, 60.0, 50.0),
    ("C1 PEAK + GRID_EXPORT", TC.TOU_PEAK, -60.0, 50.0),
    ("C2 OFF_PEAK + GRID_EXPORT", TC.TOU_OFF_PEAK, -60.0, 50.0),
    ("D1 PEAK + NEAR_ZERO", TC.TOU_PEAK, 0.0, 50.0),
    ("D2 OFF_PEAK + NEAR_ZERO", TC.TOU_OFF_PEAK, 0.0, 50.0),
    ("G  TOU UNKNOWN", TC.TOU_UNKNOWN, 60.0, 50.0),
    ("H  SOC HIGH（充電閂閉）", TC.TOU_OFF_PEAK, 60.0, 95.0),
    ("I  SOC LOW（放電閂閉）", TC.TOU_PEAK, 60.0, 10.0),
)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase 6 Meter-Driven Decision Simulator（離線、零 I/O）")
    ap.add_argument("--scenarios", action="store_true", help="跑內建情境")
    ap.add_argument("--jsonl", action="store_true", help="以 JSONL 輸出")
    a = ap.parse_args(argv)

    prof = ProductionProfile()
    print("=" * 72)
    print("  Phase 6 Meter-Driven Decision Simulator（OFFLINE / ZERO-I/O）")
    print("=" * 72)
    print("  production profile : %s" % prof.status)
    print("    charge_power_kw    : %s  （來源 pcs_auto_control_config）"
          % prof.charge_power_kw)
    print("    discharge_power_kw : %s" % prof.discharge_power_kw)
    print("  policy library 預設 : charge=%r discharge=%r（Fail Closed，未被修改）"
          % (DP.PolicyConfig().charge_power_kw,
             DP.PolicyConfig().discharge_power_kw))
    print("  min_switch_interval_sec : %r（Layer 2 DEFERRED，本檔不填值）"
          % CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec)
    print("  meter_stale_grace_sec   : %r（DEFERRED，本檔不提供寬限路徑）"
          % getattr(CFG.DEFAULT_CONTROL_CONFIG, "meter_stale_grace_sec", None))
    print("  meter STALE_AFTER_SEC   : %s（既有 authoritative behavior）"
          % MC.STALE_AFTER_SEC)
    print("  kW 語意 : Grid + = %s / − = %s" % (GRID_IMPORT, GRID_EXPORT))
    print("  ⚠ 本檔零 I/O：不連 HTTP / Socket.IO / Modbus / SSH / HMI / PCS")

    if not a.scenarios:
        print("\n  （加上 --scenarios 跑內建情境）")
        return 0

    print("\n" + "-" * 72)
    for label, tou, kw, soc in BUILTIN_SCENARIOS:
        _, rec = _run_case(label, tou, kw, soc)
        if a.jsonl:
            print(json.dumps(rec.as_dict(), ensure_ascii=False, default=str))
        else:
            print("  %-34s → %-16s %s" % (label, rec.outcome, rec.explain()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
