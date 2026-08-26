# -*- coding: utf-8 -*-
"""
decision_observer.py — Production Decision 觀測層（Phase D.3-C，OBSERVE-ONLY）
======================================================================
把兩個獨立的觀測來源組裝成「本輪**本來會**做什麼」的可稽核紀錄。

    ESS Adapter ──┐
                  ├─► consumer-time freshness ─► Grid 分類 ─► TOU ─► Decision
    Meter Adapter ┘                                                   │
                                                                      ▼
                                                        DecisionObservation
                                                        would_action ∈
                                                        charge / discharge / idle / None

🔴 **本模組沒有任何控制能力**
    不 import device_control_operator / pcs_control_executor / control_authority /
    last_control_store / api_client / requests / phase6_field_measure。
    不建立 ControlLegAuthorization、不寫 LastControl、不呼叫 Safety Gate 的
    dispatch 路徑、不產生 ControlRequest。would_action 只是稽核事實，不是指令。

🔴 **不重寫任何判定規則**
    尖峰／離峰 → tou_calendar；逆送／deadband／遲滯 → power_classifier；
    充放電策略 → decision_policy；Fail Closed 順序 → decision_engine。
    本模組只負責「把正確的東西餵給正確的 library，並如實記錄結果」。

🔴 Consumer-time freshness（D.3-B STALE 設計觀察的正式落點）
    Adapter freshness = 「資料取得完成時」是否已過舊。
    Consumer freshness = 「真正拿去做判斷時」是否仍然夠新。
    兩者是**不同**的問題。ess_observation.valid=True 只證明前者。
    本模組在做決策的當下，以 decision 的 monotonic now **重新計算**兩份資料的
    age，任一超過既有門檻即 STALE_BLOCKED，**不得**沿用 Adapter 當時的結論。

🔴 Snapshot coherence
    ESS 與 Meter 是兩個獨立來源，取得時刻不同。本階段**只記錄**時間差，
    **不設定**任何 coherence 門檻 —— 目前沒有已裁定的正式規格，不猜秒數。
    coherence_threshold 一律維持 UNCONFIGURED。

用法（完全離線）
    python decision_observer.py --demo
"""

import time
import math
import argparse
from datetime import datetime
from dataclasses import dataclass, asdict

import meter_client as MC
import power_classifier as PC
import tou_calendar as TC
import decision_engine as DE
import decision_policy as DP

# ======================================================================
# reason
# ======================================================================
DOBS_OK = "DECISION_OBSERVED"
DOBS_ESS_INVALID = "ESS_OBSERVATION_INVALID"
DOBS_METER_INVALID = "METER_OBSERVATION_INVALID"
DOBS_ESS_STALE_AT_DECISION = "ESS_STALE_AT_DECISION_TIME"
DOBS_METER_STALE_AT_DECISION = "METER_STALE_AT_DECISION_TIME"
DOBS_TIMEBASE_UNTRUSTED = "TIMEBASE_UNTRUSTED"

# would_action（**不是**控制動作，是稽核用的意圖紀錄）
WOULD_CHARGE = DE.ACTION_CHARGE
WOULD_DISCHARGE = DE.ACTION_DISCHARGE
WOULD_IDLE = DE.ACTION_IDLE
WOULD_ACTIONS = frozenset({WOULD_CHARGE, WOULD_DISCHARGE, WOULD_IDLE})

# 🔴 Snapshot coherence 門檻：**尚無正式規格** → 永遠不設值。
#    只記錄 ESS 與 Meter 的取得時間差，不據以放行或阻擋。
COHERENCE_THRESHOLD_SEC = None


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v):
    return _is_number(v) and math.isfinite(v)


@dataclass(frozen=True)
class DecisionObservation:
    """
    一輪決策觀測的完整稽核紀錄。

    設計原則：必須能回答「**為什麼**會想充電」，而不是只記 WOULD_CHARGE。
    🔴 dispatched 概念不存在於此 —— 本模組不可能產生任何指令。
    """
    valid: bool
    reason: str
    stale_blocked: bool = False
    would_action: str = None

    # ---- ESS ----
    ess_valid: bool = None
    ess_reason: str = None
    ess_age_at_adapter_sec: float = None
    ess_age_at_decision_sec: float = None
    ess_read_started_at: float = None
    ess_read_completed_at: float = None
    ess_read_duration_sec: float = None
    pcs_state: str = None
    soc_percent: float = None

    # ---- Meter ----
    meter_valid: bool = None
    meter_reason: str = None
    meter_age_at_adapter_sec: float = None
    meter_age_at_decision_sec: float = None
    meter_received_at: float = None
    grid_power_kw: float = None

    # ---- 分類 ----
    grid_state: str = None
    grid_reason: str = None
    tou_state: str = None
    tou_reason: str = None
    tou_season: str = None
    tou_day_type: str = None

    # ---- 決策 ----
    decision_action: str = None
    decision_reason: str = None
    decision_target_power_kw: float = None
    decision_detail: str = ""

    # ---- coherence（只記錄，無門檻）----
    coherence_gap_sec: float = None
    coherence_threshold_sec: float = None

    decided_at: float = None
    # ---- D.3-D：供仲裁層重用的原始物件（不重新計算、不重新解讀）----
    decision_result: object = None     # decision_engine.DecisionResult
    ess_observation: object = None     # ess_snapshot_adapter.ControlObservation
    detail: str = ""

    def as_dict(self):
        return asdict(self)

    def as_json_dict(self):
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def audit_line(self):
        """單行稽核摘要 —— 必須看得出「為什麼」。"""
        if not self.valid:
            return (f"[DECISION] BLOCKED reason={self.reason} "
                    f"ess={self.ess_reason} meter={self.meter_reason} "
                    f"{self.detail}".rstrip())
        pw = "n/a" if self.grid_power_kw is None else f"{self.grid_power_kw:+.2f}kW"
        soc = "n/a" if self.soc_percent is None else f"{self.soc_percent:.1f}%"
        return (f"[DECISION] would={self.would_action or 'none'} "
                f"因為 grid={self.grid_state}({pw}) tou={self.tou_state} "
                f"soc={soc} pcs={self.pcs_state} "
                f"→ {self.decision_action}/{self.decision_reason} "
                f"| ess_age={self.ess_age_at_decision_sec:.2f}s "
                f"meter_age={self.meter_age_at_decision_sec:.2f}s "
                f"gap={self.coherence_gap_sec:.2f}s")

    def __str__(self):
        return self.audit_line()


class DecisionObserver:
    """
    OBSERVE-ONLY 決策觀測器。

    ess_adapter / meter_adapter
        由呼叫端注入。兩者皆未接上來源時，觀測一律 Fail Closed。
    classifier
        PowerClassifier 實例（持有遲滯／debounce 狀態，必須跨 cycle 保留）。
        ⚠️ 這是**分類狀態**，不是快取的 snapshot —— 資料本身每輪重讀。
    engine / policy
        decision_engine.DecisionEngine + decision_policy.TouArbitragePolicy。
        policy 的充放電功率預設為 None → Decision 會 Fail Closed 成 no_action。
    holiday_provider
        未注入時 tou_calendar 使用 UnknownHolidayProvider → TOU 一律 UNKNOWN。
        🔴 這是刻意的 Fail Closed：正式離峰日來源確認前，不假裝今天不是假日。
    """

    def __init__(self, ess_adapter=None, meter_adapter=None, classifier=None,
                 engine=None, policy=None, tou_config=None, holiday_provider=None,
                 clock=None, local_now=None, ess_config=None,
                 meter_stale_after_sec=None):
        self.ess_adapter = ess_adapter
        self.meter_adapter = meter_adapter
        self.classifier = classifier if classifier is not None else PC.PowerClassifier()
        self.policy = policy if policy is not None else DP.TouArbitragePolicy()
        self.engine = engine if engine is not None else DE.DecisionEngine(policy=self.policy)
        self.tou_config = tou_config if tou_config is not None else TC.G_CONFIG
        self.holiday_provider = holiday_provider
        self._clock = clock if clock is not None else time.monotonic
        self._local_now = local_now if local_now is not None else datetime.now
        # ⚠️ 沿用既有且已裁示的新鮮度門檻，本階段不新增、不放寬任何 production 數值。
        self.ess_config = ess_config if ess_config is not None else DE.DEFAULT_ESS_CONFIG
        self.meter_stale_after_sec = (meter_stale_after_sec
                                      if meter_stale_after_sec is not None
                                      else MC.STALE_AFTER_SEC)

    # ------------------------------------------------------------------
    def _base(self, ess_obs, meter_obs, now, ess_age, meter_age, gap):
        return dict(
            ess_valid=getattr(ess_obs, "valid", None),
            ess_reason=getattr(ess_obs, "reason", None),
            ess_age_at_adapter_sec=getattr(ess_obs, "age_sec", None),
            ess_age_at_decision_sec=ess_age,
            ess_read_started_at=getattr(ess_obs, "read_started_at", None),
            ess_read_completed_at=getattr(ess_obs, "read_completed_at", None),
            ess_read_duration_sec=getattr(ess_obs, "read_duration_sec", None),
            pcs_state=getattr(ess_obs, "pcs_state", None),
            soc_percent=getattr(getattr(ess_obs, "ess", None), "soc_percent", None),
            meter_valid=getattr(meter_obs, "valid", None),
            meter_reason=getattr(meter_obs, "reason", None),
            meter_age_at_adapter_sec=getattr(meter_obs, "age_sec", None),
            meter_age_at_decision_sec=meter_age,
            meter_received_at=getattr(meter_obs, "received_at", None),
            grid_power_kw=getattr(meter_obs, "power_kw", None),
            coherence_gap_sec=gap,
            coherence_threshold_sec=COHERENCE_THRESHOLD_SEC,
            decided_at=now)

    def observe(self):
        """
        執行一輪觀測。**永遠不會拋出例外的責任在呼叫端的 Fail Closed 邊界** ——
        本函式不自行吞任何例外，讓 Runtime 的 FAULT_BLOCKED 能確實生效。

        判定順序
            1. 取得兩份觀測（各自已 Fail Closed）
            2. 以 decision 的 now **重算**兩者 age（consumer-time freshness）
            3. 記錄 coherence gap（不設門檻）
            4. 任一 invalid / 任一 consumer-time stale → 不產生決策
            5. 餵 classifier（**斷線時必須餵 no_data，否則會卡在舊狀態**）
            6. TOU → Decision → would_action
        """
        ess_obs = (self.ess_adapter.observe() if self.ess_adapter is not None
                   else None)
        meter_obs = (self.meter_adapter.observe() if self.meter_adapter is not None
                     else None)
        now = self._clock()

        # ---- consumer-time freshness（不得沿用 Adapter 當時的結論）----
        e_start = getattr(ess_obs, "read_started_at", None)
        m_recv = getattr(meter_obs, "received_at", None)
        ess_age = (now - e_start) if _finite(e_start) else None
        meter_age = (now - m_recv) if _finite(m_recv) else None
        gap = (abs(m_recv - e_start) if _finite(e_start) and _finite(m_recv)
               else None)
        base = self._base(ess_obs, meter_obs, now, ess_age, meter_age, gap)

        ess_ok = getattr(ess_obs, "valid", False) is True
        meter_ok = getattr(meter_obs, "valid", False) is True

        ess_stale_now = ess_ok and (ess_age is None
                                    or ess_age > self.ess_config.stale_after_sec)
        meter_stale_now = meter_ok and (meter_age is None
                                        or meter_age > self.meter_stale_after_sec)

        # ---- 斷線 / 過舊 → classifier 必須被推向 UNKNOWN ----
        # 🔴 power_classifier 的 stable 狀態只在 update() 時更新；
        #    若中斷期間不餵資料，它會卡在斷線前的狀態而永遠不轉 UNKNOWN。
        feed = (meter_obs.snapshot if (meter_ok and not meter_stale_now)
                else MC.no_data_snapshot())
        grid = self.classifier.update(feed, now=now)
        base.update(grid_state=grid.state, grid_reason=grid.reason)

        if not ess_ok:
            return DecisionObservation(
                valid=False, reason=DOBS_ESS_INVALID, **base,
                detail=f"ESS 觀測無效：{getattr(ess_obs, 'detail', '')}")
        if not meter_ok:
            return DecisionObservation(
                valid=False, reason=DOBS_METER_INVALID, **base,
                detail=f"電表觀測無效：{getattr(meter_obs, 'detail', '')}")
        if ess_age is None or meter_age is None:
            return DecisionObservation(
                valid=False, reason=DOBS_TIMEBASE_UNTRUSTED, stale_blocked=True,
                **base, detail="無法計算 consumer-time age（時間基準不可信）")
        if ess_stale_now:
            return DecisionObservation(
                valid=False, reason=DOBS_ESS_STALE_AT_DECISION, stale_blocked=True,
                **base,
                detail=f"決策當下 ESS age={ess_age:.2f}s > "
                       f"{self.ess_config.stale_after_sec}s"
                       f"（Adapter 當時為 {base['ess_age_at_adapter_sec']}s）")
        if meter_stale_now:
            return DecisionObservation(
                valid=False, reason=DOBS_METER_STALE_AT_DECISION, stale_blocked=True,
                **base,
                detail=f"決策當下電表 age={meter_age:.2f}s > "
                       f"{self.meter_stale_after_sec}s"
                       f"（Adapter 當時為 {base['meter_age_at_adapter_sec']}s）")

        # ---- TOU（尖峰／離峰一律由正式 library 判定）----
        tou = TC.classify_tou(self._local_now(), config=self.tou_config,
                              holiday_provider=self.holiday_provider)
        base.update(tou_state=tou.state, tou_reason=tou.reason,
                    tou_season=tou.season, tou_day_type=tou.day_type)

        # ---- Decision（Fail Closed 順序完全由 engine 決定）----
        res = self.engine.decide(DE.DecisionInput(grid=grid, tou=tou,
                                                  ess=ess_obs.ess))
        would = res.action if res.action in WOULD_ACTIONS else None
        return DecisionObservation(
            valid=True, reason=DOBS_OK, stale_blocked=False, would_action=would,
            **base,
            decision_action=res.action, decision_reason=res.reason,
            decision_target_power_kw=res.target_power_kw,
            decision_detail=getattr(res, "detail", ""),
            decision_result=res, ess_observation=ess_obs)


# ======================================================================
# CLI（完全離線）
# ======================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Production Decision 觀測層（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    import ess_snapshot_adapter as EA                     # noqa: PLC0415
    import meter_observation_adapter as MA                # noqa: PLC0415

    print("== Production Decision Observer（D.3-C，OBSERVE-ONLY、完全離線）==\n")
    print("  未接任何來源：")
    print(f"    {DecisionObserver().observe()}")

    print("\n  ⚠ would_action 只是稽核事實，不是指令；本模組沒有任何 dispatch 路徑。")
    print(f"  ⚠ coherence 門檻：{COHERENCE_THRESHOLD_SEC}（UNCONFIGURED，只記錄不判定）")
    print(f"  ⚠ 未注入正式離峰日來源時 TOU 一律 UNKNOWN → Decision Fail Closed。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
