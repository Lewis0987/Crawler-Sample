# -*- coding: utf-8 -*-
"""
phase6_decision_replay_runner.py — Long-Running Offline Decision Runner
======================================================================
定位：**測試側的時間軸驅動器**，不是第二套 service。

    Timeline（虛擬時鐘）
        → phase6_decision_simulator.DecisionSimulator.step()
            → 既有 meter / classifier / TariffProvider / decision / safety /
              authority / Layer 1 互鎖
        → DecisionRecord 序列
        → Invariant 檢查 + RunSummary

🔴 **零 I/O、確定性、可重播、虛擬時鐘。**
   不使用 `time.sleep()` 當測試時鐘、不以真實 wall clock 作為決策依據。
   跑「24 小時」只是把虛擬時間推進 86400 秒，不會真的等待。
   不連 HTTP / Socket.IO / Modbus / SSH / ESS HMI / PCS API；
   不碰 RemoteSenders / external controller。

🔴 **不重新實作任何決策邏輯。** 本檔沒有自己的 classification、TOU、
   SOC policy、decision matrix、safety gate、authority、direction interlock。
   全部走 `DecisionSimulator`，而它本身只是既有模組的組裝層。

🔴 **沒有電池物理模型。** SOC 一律由 timeline **腳本明確給定**，
   不由「5 kW × 時間」推算，也沒有容量 / 充放電效率 / 能量平衡。
   本階段驗的是 decision correctness，不是 energy economics。

🔴 **不新增任何 production 門檻。** `min_switch_interval_sec` /
   `meter_stale_grace_sec` 維持 `None`（DEFERRED）。若長時序顯示 Layer 2
   確有必要，只回報 evidence，不自行制定秒數。

時間模型
    每個 tick 同時帶兩種時間，兩者由同一條時間軸導出，不會互相漂移：
        wall   aware datetime（Asia/Taipei）→ 給 TariffProvider
        mono   自 timeline 起點的秒數        → 給 meter / classifier / ESS

用法（完全離線）
    python phase6_decision_replay_runner.py            # 自述
    python phase6_decision_replay_runner.py --day      # 24h synthetic day
"""
import json
import math
import argparse
import datetime

import meter_client as MC
import power_classifier as PC
import tou_calendar as TC
import decision_engine as DE
import decision_policy as DP
import safety_gate as SG
import tariff_provider as TP
import annual_off_peak_calendar as AC
import control_authority as CA
import last_control_store as LCS
import pcs_control_integration as PCI
import pcs_auto_control_config as CFG
import phase6_decision_simulator as SIM

try:
    from zoneinfo import ZoneInfo
except ImportError:                                        # pragma: no cover
    ZoneInfo = None

TZ_NAME = TP.PRODUCTION_TIMEZONE_NAME                      # Asia/Taipei


def tz():
    return ZoneInfo(TZ_NAME)


# ======================================================================
# Timeline
# ======================================================================
TL_OK = "TIMELINE_OK"
TL_OUT_OF_ORDER = "TIMELINE_TIMESTAMP_OUT_OF_ORDER"
TL_DUPLICATE = "TIMELINE_TIMESTAMP_DUPLICATE"
TL_NAIVE = "TIMELINE_NAIVE_DATETIME_REJECTED"
TL_EMPTY = "TIMELINE_EMPTY"

_UNSET = object()


class TimelinePoint(object):
    """
    一個 tick 的完整腳本輸入。**每一項都由腳本給定，不由 runner 推算。**

    at                aware datetime（Asia/Taipei）
    meter_kw          Grid Meter 語意：正 = GRID_IMPORT、負 = GRID_EXPORT
                      None = 該 tick 沒有收到任何 meter payload（斷線）
    meter_age_sec     本 tick 的 meter 資料年齡；None = 用本 tick 時刻（fresh）
    meter_state       6160 的 meter_state；None = 欄位缺失（schema 不完整）
    soc_percent       **腳本給定**，不由功率推算
    pcs_*             PCS 三旗標，腳本給定（不模擬設備反應）
    tou_override      僅供 fail-closed 測試；預設 None = 走正式 TariffProvider
    """

    __slots__ = ("at", "meter_kw", "meter_age_sec", "meter_state",
                 "demand_state", "soc_percent", "ess_age_sec", "comm_ok",
                 "pcs_charging", "pcs_discharging", "pcs_standby",
                 "pcs_fault", "alarm_rows", "alarm_source_complete",
                 "pcs_mode_state", "tou_override", "label",
                 "last_control_record", "last_control_trust", "last_control",
                 "ess_extra")

    def __init__(self, at, meter_kw=0.0, meter_age_sec=None,
                 meter_state=MC.S_OK, demand_state=MC.S_OK,
                 soc_percent=50.0, ess_age_sec=None, comm_ok=True,
                 pcs_charging=False, pcs_discharging=False, pcs_standby=True,
                 pcs_fault=None, alarm_rows=(), alarm_source_complete=True,
                 pcs_mode_state=_UNSET, tou_override=None, label="",
                 last_control_record=None, last_control_trust=None,
                 last_control=None, ess_extra=None):
        self.at = at
        self.meter_kw = meter_kw
        self.meter_age_sec = meter_age_sec
        self.meter_state = meter_state
        self.demand_state = demand_state
        self.soc_percent = soc_percent
        self.ess_age_sec = ess_age_sec
        self.comm_ok = comm_ok
        self.pcs_charging = pcs_charging
        self.pcs_discharging = pcs_discharging
        self.pcs_standby = pcs_standby
        self.pcs_fault = pcs_fault
        self.alarm_rows = alarm_rows
        self.alarm_source_complete = alarm_source_complete
        # 🔴 _UNSET = 未指定（用便利預設）；None = 明確讀不到（必須 Fail Closed）
        self.pcs_mode_state = ({"schedule_switch": 0, "manual_switch": 1}
                               if pcs_mode_state is _UNSET else pcs_mode_state)
        self.tou_override = tou_override
        self.label = label
        # Control Authority 佐證（腳本給定；runner 不自行捏造控制歷史）
        self.last_control_record = last_control_record
        self.last_control_trust = last_control_trust
        self.last_control = last_control
        # 腳本給定的額外 ESS 欄位（例如 actual_active_power_kw）。
        # 🔴 一律由腳本提供，runner **不**由 request 功率反推觀測值。
        self.ess_extra = dict(ess_extra or {})


class Timeline(object):
    """
    有序的 TimelinePoint 序列。**建構時就驗證時間單調性。**

    🔴 out-of-order timestamp 一律拒絕（raise），不是靜默排序 ——
       靜默排序會讓「腳本寫錯」變成「測試照樣過」。
    🔴 duplicate timestamp 也拒絕：同一時刻兩筆決策的先後是未定義的，
       任何「取第一筆 / 取最後一筆」都是我們自行發明的規則。
    """

    def __init__(self, points, name=""):
        pts = list(points)
        if not pts:
            raise ValueError(TL_EMPTY)
        prev = None
        for i, p in enumerate(pts):
            if p.at.tzinfo is None or p.at.utcoffset() is None:
                raise ValueError("%s @ index %d" % (TL_NAIVE, i))
            if prev is not None:
                if p.at == prev:
                    raise ValueError("%s @ index %d: %s" % (TL_DUPLICATE, i,
                                                            p.at.isoformat()))
                if p.at < prev:
                    raise ValueError("%s @ index %d: %s < %s"
                                     % (TL_OUT_OF_ORDER, i, p.at.isoformat(),
                                        prev.isoformat()))
            prev = p.at
        self.points = tuple(pts)
        self.name = name

    @property
    def start(self):
        return self.points[0].at

    @property
    def end(self):
        return self.points[-1].at

    def monotonic_of(self, p):
        """把 wall 時間換算成自起點的秒數 —— 兩種時鐘由同一條軸導出。"""
        return (p.at - self.start).total_seconds()

    def __len__(self):
        return len(self.points)


def build_timeline(start, step_sec, count, name="", **point_kw):
    """等間隔時間軸。每個 point 的欄位可用 callable(i, at) 逐 tick 決定。"""
    pts = []
    for i in range(count):
        at = start + datetime.timedelta(seconds=step_sec * i)
        kw = {}
        for k, v in point_kw.items():
            kw[k] = v(i, at) if callable(v) else v
        pts.append(TimelinePoint(at, **kw))
    return Timeline(pts, name=name)


# ======================================================================
# Summary
# ======================================================================
class RunSummary(object):
    """一次 replay 的統計。只數既有正式 reason / outcome，不自創分類。"""

    def __init__(self, records, timeline):
        self.records = list(records)
        self.timeline = timeline
        self.total_ticks = len(self.records)
        self.start_time = timeline.start
        self.end_time = timeline.end
        self.decisions = self._count("decision_action")
        self.requests = self._count("requested_action")
        self.outcomes = self._count("outcome")
        self.block_reasons = self._count("blocked_reason", skip_none=True)
        self.decision_reasons = self._count("decision_reason")
        self.grid_states = self._count("grid_state")
        self.tou_states = self._count("tou_state")
        self.transitions = self._transitions()
        self.first = self.records[0] if self.records else None
        self.last = self.records[-1] if self.records else None

    def _count(self, field, skip_none=False):
        out = {}
        for r in self.records:
            v = getattr(r, field, None)
            if skip_none and v is None:
                continue
            out[v] = out.get(v, 0) + 1
        return out

    def _transitions(self):
        """相鄰 tick 之間的狀態轉移次數（grid / requested action）。"""
        out = {}
        for a, b in zip(self.records, self.records[1:]):
            if a.grid_state != b.grid_state:
                k = "grid:%s->%s" % (a.grid_state, b.grid_state)
                out[k] = out.get(k, 0) + 1
            if a.requested_action != b.requested_action:
                k = "req:%s->%s" % (a.requested_action, b.requested_action)
                out[k] = out.get(k, 0) + 1
        return out

    @property
    def would_send_count(self):
        return sum(1 for r in self.records if r.would_send)

    @property
    def executor_calls(self):
        return sum(1 for r in self.records if r.executor_called)

    @property
    def reversal_blocked_count(self):
        return sum(1 for r in self.records
                   if r.blocked_reason in PCI.DIRECTION_BLOCK_REASONS
                   or r.request_reason in PCI.DIRECTION_BLOCK_REASONS)

    def as_dict(self):
        return {
            "timeline": self.timeline.name,
            "total_ticks": self.total_ticks,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "decisions": self.decisions,
            "requests": self.requests,
            "outcomes": self.outcomes,
            "block_reasons": self.block_reasons,
            "decision_reasons": self.decision_reasons,
            "grid_states": self.grid_states,
            "tou_states": self.tou_states,
            "transitions": self.transitions,
            "would_send": self.would_send_count,
            "executor_calls": self.executor_calls,
            "reversal_blocked": self.reversal_blocked_count,
            "first": self.first.as_dict() if self.first else None,
            "last": self.last.as_dict() if self.last else None,
        }

    def format(self):
        L = ["timeline : %s" % (self.timeline.name or "(unnamed)"),
             "ticks    : %d   %s → %s"
             % (self.total_ticks, self.start_time.isoformat(),
                self.end_time.isoformat()),
             "decision : %s" % _fmt(self.decisions),
             "request  : %s" % _fmt(self.requests),
             "outcome  : %s" % _fmt(self.outcomes),
             "tou      : %s" % _fmt(self.tou_states),
             "grid     : %s" % _fmt(self.grid_states)]
        if self.block_reasons:
            L.append("blocked  : %s" % _fmt(self.block_reasons))
        if self.transitions:
            L.append("transition:")
            for k in sorted(self.transitions):
                L.append("    %-40s %d" % (k, self.transitions[k]))
        L.append("would_send=%d  executor_calls=%d  reversal_blocked=%d"
                 % (self.would_send_count, self.executor_calls,
                    self.reversal_blocked_count))
        if self.first:
            L.append("first    : %s" % self.first.explain())
            L.append("last     : %s" % self.last.explain())
        return "\n".join(L)


def _fmt(d):
    return "  ".join("%s=%d" % (k, v) for k, v in sorted(
        d.items(), key=lambda kv: (-kv[1], str(kv[0]))))


# ======================================================================
# Invariants（STEP 5 的 13 條）
# ======================================================================
INVARIANTS = (
    "stale_no_dispatch", "unknown_no_fallback", "fault_no_dispatch",
    "authority_no_dispatch", "power_none_no_fallback",
    "power_from_approved_config", "no_direct_reversal",
    "peak_export_no_discharge", "explicit_none_not_defaulted",
    "reason_explainable", "timestamp_monotonic",
    "out_of_order_rejected", "duplicate_deterministic",
)


# 送出方向 → 「不得處於」的 PCS 實際狀態（方向反轉互鎖）
_OPPOSITE_PCS_STATE = {
    PCI.CTRL_CHARGE: PCI.PCS_DISCHARGING,
    PCI.CTRL_DISCHARGE: PCI.PCS_CHARGING,
}


class InvariantViolation(object):
    def __init__(self, name, tick, detail):
        self.name = name
        self.tick = tick
        self.detail = detail

    def __str__(self):
        return "[%s] tick=%s %s" % (self.name, self.tick, self.detail)


def check_invariants(records, timeline):
    """回傳 violations 清單。空清單 = 全部成立。"""
    v = []
    approved_charge = CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw
    approved_discharge = CFG.DEFAULT_CONTROL_CONFIG.discharge_power_kw

    prev_at = None
    for r in records:
        n = r.step
        # 1 stale 不得產生可執行請求
        if r.meter_stale is True and r.would_send:
            v.append(InvariantViolation("stale_no_dispatch", n,
                                        "meter stale 卻 would_send"))
        if r.ess_stale is True and r.would_send:
            v.append(InvariantViolation("stale_no_dispatch", n,
                                        "ESS stale 卻 would_send"))
        # 2 UNKNOWN 不得 fallback
        if r.grid_state == PC.STATE_UNKNOWN and r.would_send:
            v.append(InvariantViolation("unknown_no_fallback", n,
                                        "grid UNKNOWN 卻 would_send"))
        if r.tou_state in (TC.TOU_UNKNOWN, None) and r.would_send:
            v.append(InvariantViolation("unknown_no_fallback", n,
                                        "TOU UNKNOWN 卻 would_send"))
        # 3 fault / alarm 不得 dispatch
        if (r.pcs_fault is True or (r.critical_alarm_rows or 0) > 0) \
                and r.executor_called:
            v.append(InvariantViolation("fault_no_dispatch", n,
                                        "fault/alarm 期間 executor 被呼叫"))
        # 4 authority 未放行不得 dispatch
        if r.authority_allowed is False and r.executor_called:
            v.append(InvariantViolation("authority_no_dispatch", n,
                                        "authority 未放行卻呼叫 executor"))
        # 5/6 功率只能來自已核准 config
        if r.requested_action == PCI.CTRL_CHARGE:
            if r.requested_power_kw != approved_charge:
                v.append(InvariantViolation("power_from_approved_config", n,
                                            "charge 功率 %r != 已核准 %r"
                                            % (r.requested_power_kw,
                                               approved_charge)))
        if r.requested_action == PCI.CTRL_DISCHARGE:
            if r.requested_power_kw != approved_discharge:
                v.append(InvariantViolation("power_from_approved_config", n,
                                            "discharge 功率 %r != 已核准 %r"
                                            % (r.requested_power_kw,
                                               approved_discharge)))
        if r.decision_reason == DP.R_POWER_NOT_CONFIGURED and r.would_send:
            v.append(InvariantViolation("power_none_no_fallback", n,
                                        "功率未設定卻 would_send"))
        # 8 PEAK + EXPORT 不得放電
        if r.tou_state == TC.TOU_PEAK and r.grid_state == PC.STATE_EXPORT \
                and r.requested_action == PCI.CTRL_DISCHARGE:
            v.append(InvariantViolation("peak_export_no_discharge", n,
                                        "PEAK + EXPORT 竟送出 discharge"))
        # 10 reason 必須可解釋
        if r.outcome is None or (r.would_send is False
                                 and not (r.blocked_reason
                                          or r.request_reason
                                          or r.decision_reason)):
            v.append(InvariantViolation("reason_explainable", n,
                                        "缺少可解釋的 reason"))
        # 11 timestamp 單調遞增
        if prev_at is not None and not (r.at > prev_at):
            v.append(InvariantViolation("timestamp_monotonic", n,
                                        "at=%r 未大於前一筆 %r" % (r.at, prev_at)))
        prev_at = r.at

    # 7 不得直接反轉（Layer 1 互鎖語意）
    #    🔴 判準是「送出當下 PCS 實際回報的方向」，**不是**上一筆送出的指令。
    #       上一筆指令只代表意圖；設備是否仍在該方向運轉，只能看 PCS 狀態。
    #       若腳本把 PCS 寫成 STANDBY，代表方向已隔離，反向控制是合法的，
    #       不得因為「上一筆指令是反方向」就誤報違規。
    for r in records:
        opposite = _OPPOSITE_PCS_STATE.get(r.requested_action)
        if opposite is None or not r.would_send:
            continue
        if r.pcs_state == opposite:
            v.append(InvariantViolation(
                "no_direct_reversal", r.step,
                "PCS 實際為 %s 時仍送出 %s —— 反轉必須先經過 STOP/idle 隔離"
                % (r.pcs_state, r.requested_action)))
    return v


# ======================================================================
# Runner
# ======================================================================
class ReplayRunner(object):
    """
    逐 tick 驅動既有決策鏈。**本身不做任何決策判斷。**

    tariff_provider 未注入時，使用正式組合：
        Asia/Taipei + 已 materialize 的 2026/2027 年度離峰日清單
    """

    def __init__(self, simulator=None, tariff_provider=None,
                 authority_policy=_UNSET, control_config=None):
        cfg = (control_config if control_config is not None
               else CFG.DEFAULT_CONTROL_CONFIG)
        # 🔴 與功率同一原則：authority 門檻也**只能**從已核准的 application
        #    config 取得，runner 不自行填 ttl / tolerance。
        #    明確傳 None → 使用 library 的 Fail Closed 預設（兩項皆 None）。
        if authority_policy is _UNSET:
            authority_policy = production_authority_policy(cfg)
        elif authority_policy is None:
            authority_policy = CA.DEFAULT_AUTHORITY_POLICY
        self.authority_policy = authority_policy
        self.sim = (simulator if simulator is not None
                    else SIM.DecisionSimulator(authority_policy=authority_policy))
        self.tariff = (tariff_provider if tariff_provider is not None
                       else TP.TariffProvider(
                           timezone_name=TZ_NAME,
                           holiday_provider=AC.PRODUCTION_PROVIDER))
        self.records = []

    def _tou_for(self, p):
        # 🔴 預設一律走正式 TariffProvider 依 aware timestamp 求值；
        #    只有明確指定 tou_override 的 fail-closed 案例才使用腳本值。
        if p.tou_override is not None:
            return p.tou_override
        return self.tariff.observe(p.at)

    def _payload_for(self, p):
        if p.meter_kw is None:
            return None
        return SIM.meter_payload(p.meter_kw, meter_state=p.meter_state,
                                 demand_state=p.demand_state)

    def run(self, timeline):
        for p in timeline.points:
            mono = timeline.monotonic_of(p)
            age = 0.0 if p.meter_age_sec is None else float(p.meter_age_sec)
            ess_age = 0.0 if p.ess_age_sec is None else float(p.ess_age_sec)
            sc = SIM.ScenarioStep(
                meter_payload=self._payload_for(p),
                received_at=mono - age, now=mono,
                ess_reading=SIM.ess_reading(
                    soc_percent=p.soc_percent, charging=p.pcs_charging,
                    discharging=p.pcs_discharging, standby=p.pcs_standby,
                    communication_ok=p.comm_ok, **p.ess_extra),
                ess_read_started_at=mono - ess_age,
                ess_read_completed_at=mono - ess_age,
                tou=self._tou_for(p),
                pcs_mode_state=p.pcs_mode_state,
                alarm_rows=p.alarm_rows,
                alarm_source_complete=p.alarm_source_complete,
                pcs_fault=p.pcs_fault, label=p.label,
                last_control=p.last_control,
                last_control_record=p.last_control_record,
                last_control_trust=p.last_control_trust)
            rec = self.sim.step(sc)
            # 用 wall 時間覆蓋顯示欄位，讓紀錄可對回時間軸
            rec.at = p.at
            self.records.append(rec)
        return RunSummary(self.records, timeline)

    def to_jsonl(self):
        return "\n".join(json.dumps(r.as_dict(), ensure_ascii=False,
                                    default=str) for r in self.records)


def production_authority_policy(control_config=None):
    """
    由已核准的 application config 組出 Control Authority 政策。

    🔴 `authority_ttl_sec` / `authority_power_tolerance_kw` 是既有已核准值
       （見 `pcs_auto_control_config`），本檔只是**讀取並注入**，
       不保存第二份、不自行填值。config 缺值時原樣傳 None → library
       會因無法證明控制權而 BLOCK（正確的 Fail Closed）。
    """
    cfg = (control_config if control_config is not None
           else CFG.DEFAULT_CONTROL_CONFIG)
    return CA.AuthorityPolicy(
        authority_ttl_sec=getattr(cfg, "authority_ttl_sec", None),
        authority_power_tolerance_kw=getattr(
            cfg, "authority_power_tolerance_kw", None))


def replay(timeline, simulator=None, tariff_provider=None,
           authority_policy=_UNSET):
    """跑一條 timeline，回傳 (summary, violations, records)。"""
    r = ReplayRunner(simulator=simulator, tariff_provider=tariff_provider,
                     authority_policy=authority_policy)
    summary = r.run(timeline)
    return summary, check_invariants(r.records, timeline), r.records


# ======================================================================
# 24h synthetic day（deterministic，無任何現場資料）
# ======================================================================
# ----------------------------------------------------------------------
# 腳本化的「已認領」佐證
#
# 🔴 這兩個 helper **只提供給 timeline 作者呼叫**。runner 不會在任何地方
#    自動產生它們 —— 不得由「上一筆送出 CHARGE」推導「PCS 現在是 CHARGING」，
#    那會變成假的 plant model。PCS 實際狀態一律由 timeline 明確給定。
# ----------------------------------------------------------------------
# 觀測慣例沿用 control_authority：充電 AC 為正、放電 AC 為負。
OBSERVED_SIGN = {LCS.ACT_CHARGE: 1.0, LCS.ACT_DISCHARGE: -1.0}


def scripted_observed_kw(action, power_kw):
    """腳本給定的「此刻 PCS 回報的 AC 功率」。"""
    return OBSERVED_SIGN[action] * power_kw


def scripted_owned_record(action, power_kw, verified_at_monotonic,
                          readback_delta_kw=0.4):
    """腳本化的 LastControl（含下令當下的 read-back 基準值）。

    `actual_active_power_kw` 必須與此刻觀測值**不同**，Authority 才能證明
    暫存器已刷新（control_authority 既有的 refresh 佐證條件）。
    """
    obs = scripted_observed_kw(action, power_kw)
    return LCS.LastControlRecord(
        schema_version=LCS.SCHEMA_VERSION, action=action,
        target_power_kw=power_kw, verified_at_wall="scripted",
        verified_at_monotonic=verified_at_monotonic, pcs_actual_state=None,
        actual_active_power_kw=obs - OBSERVED_SIGN[action] * readback_delta_kw,
        reason="scripted")


# 24h synthetic day 中兩段「方向反轉互鎖」視窗（腳本給定的 PCS 實際狀態）
REVERSAL_FROM_CHARGE_HOUR = 9      # PCS 仍 CHARGING，PEAK 要求 discharge
REVERSAL_FROM_DISCHARGE_HOUR = 2   # PCS 仍 DISCHARGING，OFF_PEAK 要求 charge
REVERSAL_COMMAND_AGE_SEC = 30.0    # LastControl 驗證時間早於本 tick 觀測時間


def synthetic_day(date=None, step_sec=300):
    """
    一條確定性的 24 小時時間軸（預設 5 分鐘一 tick = 288 ticks）。

    🔴 SOC 完全由腳本給定（分段常數），**不由功率推算**。
    🔴 PCS 狀態也由腳本給定，不模擬設備反應。
    """
    d = date if date is not None else datetime.date(2026, 7, 15)   # 夏月平日
    start = datetime.datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz())

    def soc(i, at):
        h = at.hour
        if h < 4:
            return 45.0
        if h < 8:
            return 70.0
        if h < 12:
            return 95.0          # 充電上限帶
        if h < 18:
            return 55.0
        if h < 20:
            return 15.0          # 放電下限帶（SOC LOW）
        if h < 22:
            # 🔴 20:00 fault 視窗必須讓決策**真的要求 discharge**，
            #    否則 safety gate 不會被觸發，SAFETY_BLOCKED 就只是紙上宣稱。
            return 55.0
        return 40.0

    def meter(i, at):
        h = at.hour
        if h in (11, 12):
            return -40.0         # 逆灌時段
        if h == 13:
            return 0.0           # idle band
        if 3 <= h < 5:
            return None          # 斷線
        return 60.0              # 其餘為 GRID_IMPORT

    def age(i, at):
        return 10.0 if at.hour == 6 else 0.0        # 06:00~07:00 meter stale

    def fault(i, at):
        return True if at.hour == 20 else None      # 20:00~21:00 fault

    def mode(i, at):
        return None if at.hour == 15 else _UNSET    # 15:00~16:00 authority 讀不到

    # ---- 方向反轉互鎖視窗（PCS 實際狀態全部由腳本明確給定）----
    #   09:00 PCS 仍在 CHARGING，但 PEAK + SOC HIGH 會要求 discharge
    #   02:00 PCS 仍在 DISCHARGING，但 OFF_PEAK + SOC MID 會要求 charge
    def _rev_action(at):
        if at.hour == REVERSAL_FROM_CHARGE_HOUR:
            return LCS.ACT_CHARGE
        if at.hour == REVERSAL_FROM_DISCHARGE_HOUR:
            return LCS.ACT_DISCHARGE
        return None

    def _rev_power(action):
        cfg = CFG.DEFAULT_CONTROL_CONFIG
        return (cfg.charge_power_kw if action == LCS.ACT_CHARGE
                else cfg.discharge_power_kw)

    def charging(i, at):
        return _rev_action(at) == LCS.ACT_CHARGE

    def discharging(i, at):
        return _rev_action(at) == LCS.ACT_DISCHARGE

    def standby(i, at):
        return _rev_action(at) is None

    def last_control(i, at):
        a = _rev_action(at)
        if a is None:
            return None
        return scripted_owned_record(
            a, _rev_power(a),
            step_sec * i - REVERSAL_COMMAND_AGE_SEC)

    def trust(i, at):
        return None if _rev_action(at) is None else LCS.TRUST_FOR_INTERVAL

    def observed(i, at):
        a = _rev_action(at)
        if a is None:
            return None
        return {"actual_active_power_kw": scripted_observed_kw(
            a, _rev_power(a))}

    return build_timeline(
        start, step_sec, int(86400 // step_sec), name="synthetic-24h",
        meter_kw=meter, meter_age_sec=age, soc_percent=soc,
        pcs_fault=fault, pcs_mode_state=mode,
        pcs_charging=charging, pcs_discharging=discharging,
        pcs_standby=standby, ess_extra=observed,
        last_control_record=last_control, last_control_trust=trust,
        label=lambda i, at: at.strftime("%H:%M"))


# ======================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase 6 Long-Running Offline Decision Replay Runner")
    ap.add_argument("--day", action="store_true", help="跑 24h synthetic day")
    ap.add_argument("--jsonl", action="store_true")
    a = ap.parse_args(argv)

    print("=" * 72)
    print("  Phase 6 Long-Running Offline Decision Replay Runner")
    print("=" * 72)
    print("  模式          : OFFLINE / ZERO-I/O / DETERMINISTIC / VIRTUAL CLOCK")
    print("  時區          : %s（aware datetime）" % TZ_NAME)
    print("  年度行事曆     : %s" % (AC.PRODUCTION_PROVIDER.known_years,))
    print("  已核准功率     : charge=%s / discharge=%s（DEFAULT_CONTROL_CONFIG）"
          % (CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw,
             CFG.DEFAULT_CONTROL_CONFIG.discharge_power_kw))
    print("  未定案         : min_switch_interval_sec=%r / meter_stale_grace_sec=%r"
          % (CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec,
             CFG.DEFAULT_CONTROL_CONFIG.meter_stale_grace_sec))
    print("  invariants     : %d 條" % len(INVARIANTS))
    print("  ⚠ SOC 由腳本給定，無電池物理模型；不計算 kWh / 收益 / 電費")

    if not a.day:
        print("\n  （加上 --day 跑 24h synthetic day）")
        return 0

    tl = synthetic_day()
    summary, violations, recs = replay(tl)
    print("\n" + "-" * 72)
    print(summary.format())
    print("-" * 72)
    print("invariant violations : %d" % len(violations))
    for v in violations[:10]:
        print("   %s" % v)
    if a.jsonl:
        print()
        for r in recs[:5]:
            print(json.dumps(r.as_dict(), ensure_ascii=False, default=str))
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
