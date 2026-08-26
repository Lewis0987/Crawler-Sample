# -*- coding: utf-8 -*-
"""
production_arbiter.py — Production Arbitration + Authorization（Phase D.3-D）
======================================================================
把 D.3-C 的「本來會做什麼」推進到「系統是否**授權**這一個 leg」。

    DecisionObservation（original）
            ↓  fresh re-observation（不得沿用）
    Consumer-time revalidation
            ↓
    Decision revalidation（action / target 必須仍然一致）
            ↓
    Control Authority ── 重用 control_authority.py
            ↓
    Direction Interlock ── 重用 pcs_control_integration.build_control_request()
            ↓
    Duplicate suppression（desired state ≠ command edge）
            ↓
    Safety Gate ── 重用 safety_gate.py（原 reason 完整保留）
            ↓
    ProductionControlAuthorization（action-bound / one-shot / expiring）
            ↓
    AUTHORIZED_WOULD_CHARGE / _DISCHARGE / _STOP

🔴 **到此為止。dispatch_count 永遠 = 0。**
    不 import device_control_operator / pcs_control_executor / api_client /
    charge_discharge_report / phase6_field_measure，
    不建立任何 executor 實例、不寫 LastControl、不 POST、不 ReadBack。
    「已授權」與「已送出」是完全不同的兩件事。

🔴 **不重寫任何既有判定**
    Ownership → control_authority；方向互鎖與 IDLE 語意 → build_control_request；
    安全條件 → safety_gate。本層只負責編排、比對、授權與稽核。

🔴 反向切換
    偵測到 DIRECTION_REVERSAL_REQUIRES_STOP 時，本層**明確地另外形成一個
    STOP leg**（以 IDLE 語意經由既有 build_control_request 產生），
    而不是把一個 request 偷偷展開成兩個 command ——
    反方向的授權必須等 STOP leg 完成後、由新的一輪重新評估才可能出現。

用法（完全離線）
    python production_arbiter.py --demo
"""

import time
import argparse
from dataclasses import dataclass, asdict

import decision_engine as DE
import safety_gate as SG
import control_authority as CA
import pcs_control_integration as PCI
import pcs_auto_control_config as ACFG
import production_control_authorization as PAZ

# ======================================================================
# outcome
# ======================================================================
ARB_AUTHORIZED = "AUTHORIZED"
ARB_NO_CONTROL = "NO_CONTROL"
ARB_SUPPRESSED = "SUPPRESSED"
ARB_PENDING = "AUTHORIZATION_PENDING"
ARB_BLOCKED = "BLOCKED"
ARB_NOT_READY = "AUTHORIZATION_NOT_READY"

# ======================================================================
# reason
# ======================================================================
R_AUTHORIZED = "LEG_AUTHORIZED"
R_NO_DECISION = "NO_CONTROL_INTENT"              # Decision Fail Closed，無意圖
R_OBSERVATION_INVALID = "OBSERVATION_INVALID"
R_STALE_AT_AUTHORIZATION = "STALE_AT_AUTHORIZATION_TIME"
R_DECISION_CHANGED = "DECISION_CHANGED"
R_AUTHORITY_BLOCKED = "CONTROL_AUTHORITY_BLOCKED"
R_DIRECTION_BLOCKED = "DIRECTION_BLOCKED"
R_SAFETY_BLOCKED = "SAFETY_BLOCKED"
R_ALARM_SOURCE_INCOMPLETE = "ALARM_SOURCE_INCOMPLETE"
R_ALREADY_AT_DESIRED_STATE = "ALREADY_AT_DESIRED_STATE"
R_TARGET_CHANGE_UNSUPPORTED = "TARGET_CHANGE_UNSUPPORTED"     # OPEN DESIGN ITEM
R_PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION_EXISTS"
R_CONFIG_NOT_READY = "PRODUCTION_CONFIG_NOT_READY"
R_STOP_REQUIRED_FOR_REVERSAL = "STOP_REQUIRED_FOR_DIRECTION_REVERSAL"
R_IDLE_NO_LEG = "IDLE_NO_CONTROL_LEG_REQUIRED"

# would_action → runtime 稽核事件的對照（AUTHORIZED_* 由 runtime 命名）
DESIRED_RUNNING_STATE = {PCI.CTRL_CHARGE: PCI.PCS_CHARGING,
                         PCI.CTRL_DISCHARGE: PCI.PCS_DISCHARGING}

# Authorization 真正需要的 production 參數（缺一即 NOT READY）
AUTHORIZATION_REQUIRED_PARAMS = ("authorization_ttl_sec", "authority_ttl_sec",
                                 "authority_power_tolerance_kw", "max_power_kw")

_FP_GUARD = 1e-9


def _same_target(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= _FP_GUARD


@dataclass(frozen=True)
class ArbitrationResult:
    """
    一輪仲裁的完整稽核紀錄。

    🔴 dispatched 恆為 False —— 本層結構上沒有任何送出指令的途徑。
    duck typing：valid / reason / pcs_state / would_action / stale_blocked
    讓 Runtime 不需認識本型別即可消費。
    """
    outcome: str
    reason: str
    authorized: bool = False
    dispatched: bool = False
    control_action: str = None            # charge / discharge / stop / none
    would_action: str = None
    authorization: object = None

    # ---- duck typing for Runtime ----
    valid: bool = False
    pcs_state: str = None
    stale_blocked: bool = False

    # ---- audit：原始 vs 重新取得 ----
    original_decision_action: str = None
    original_decision_target_kw: float = None
    original_ess_age_sec: float = None
    original_meter_age_sec: float = None
    fresh_decision_action: str = None
    fresh_decision_target_kw: float = None
    fresh_ess_age_sec: float = None
    fresh_meter_age_sec: float = None

    # ---- audit：各閘門 ----
    authority_state: str = None
    authority_reason: str = None
    direction_reason: str = None
    safety_allowed: bool = None
    safety_reason: str = None
    safety_check_count: int = None
    alarm_source_complete: bool = None
    duplicate_suppressed: bool = False
    schedule_switch: object = None
    manual_switch: object = None
    grid_state: str = None
    tou_state: str = None
    soc_percent: float = None

    # ---- audit：授權 ----
    authorization_id: str = None
    authorization_issued_at: float = None
    authorization_expires_at: float = None
    authorization_state: str = None
    missing_config: tuple = ()
    arbitrated_at: float = None
    detail: str = ""

    def as_dict(self):
        d = asdict(self)
        d["authorization"] = (self.authorization.as_dict()
                              if self.authorization is not None else None)
        return d

    def audit_line(self):
        return (f"[ARB] {self.outcome:<22} {self.reason:<38} "
                f"action={self.control_action or '-':<9} "
                f"authority={self.authority_state or '-'} "
                f"dir={self.direction_reason or '-'} "
                f"safety={self.safety_reason or '-'} "
                f"dispatched={self.dispatched}")

    def __str__(self):
        return self.audit_line()


class _IdleDecision:
    """
    反向切換時，由本層**明確另外形成**的 STOP leg 決策。

    ⚠️ 這不是「把一個 request 展開成兩個 command」——
       它是一個獨立、單一、可稽核的 STOP leg；反方向必須等下一輪重新評估。
    """
    action = DE.ACTION_IDLE
    target_power_kw = None
    reason = R_STOP_REQUIRED_FOR_REVERSAL
    detail = "方向反轉前必須先停止"


class ProductionArbiter:
    """
    仲裁 + 授權。零 I/O、零控制能力。

    observer
        decision_observer.DecisionObserver（或等價，需提供 observe()）。
    config
        pcs_auto_control_config.AutoControlConfig；production 預設全為 None → NOT READY。
    last_control_provider
        可注入的 () -> (record, trust)。D.3-D **只讀不寫**；
        未注入時視為「沒有可信歷史」→ 運轉中的 PCS 一律 EXTERNAL（Fail Closed）。
    """

    def __init__(self, observer=None, config=None, gate=None,
                 authority_policy=None, last_control_provider=None, clock=None):
        self.observer = observer
        self.config = config if config is not None else ACFG.DEFAULT_CONTROL_CONFIG
        self.gate = gate if gate is not None else SG.SafetyGate(
            SG.SafetyConfig(max_power_kw=self.config.max_power_kw,
                            min_switch_interval_sec=self.config.min_switch_interval_sec))
        self.authority_policy = (authority_policy if authority_policy is not None
                                 else CA.AuthorityPolicy(
                                     authority_ttl_sec=self.config.authority_ttl_sec,
                                     authority_power_tolerance_kw=(
                                         self.config.authority_power_tolerance_kw)))
        self._last_control_provider = last_control_provider
        self._clock = clock if clock is not None else time.monotonic
        self._pending = None
        self._seq = 0

    # ------------------------------------------------------------------
    @property
    def pending(self):
        return self._pending

    def cancel_pending(self, reason="cancelled"):
        """取消目前掛起的授權，讓下一輪可以從 fresh 資料重新評估。"""
        if self._pending is not None and self._pending.cancel(reason):
            return True
        return False

    def _missing_config(self):
        return tuple(sorted(n for n in AUTHORIZATION_REQUIRED_PARAMS
                            if getattr(self.config, n, None) is None))

    def _res(self, outcome, reason, **kw):
        kw.setdefault("arbitrated_at", self._clock())
        return ArbitrationResult(outcome=outcome, reason=reason, **kw)

    # ------------------------------------------------------------------
    def evaluate(self):
        """
        執行一輪仲裁。**不吞任何例外** —— 交給 Runtime 的 Fail Closed 邊界。

        🔴 一輪內會取得**兩次**觀測：
           original = 決策當下；fresh = 授權當下。
           兩者之間任何必要條件改變都不得沿用舊結論。
        """
        now = self._clock()

        # ---- 0. Single-flight：一次最多一張有效授權 ----
        if self._pending is not None and self._pending.usable:
            if self._pending.is_expired(now):
                self._pending.state = PAZ.AUTHZ_EXPIRED     # 就地失效，允許重評估
            else:
                p = self._pending
                return self._res(ARB_PENDING, R_PENDING_AUTHORIZATION,
                                 authorization=p, authorization_id=p.authorization_id,
                                 authorization_state=p.state,
                                 authorization_issued_at=p.issued_at_monotonic,
                                 authorization_expires_at=p.expires_at(),
                                 control_action=p.action,
                                 detail="已有尚未消費的授權票 → 本輪不再建立第二張")

        if self.observer is None:
            return self._res(ARB_BLOCKED, R_OBSERVATION_INVALID,
                             detail="未注入 observer")

        # ---- 1. 原始決策 ----
        original = self.observer.observe()
        # ---- 2. Fresh 重新觀測（consumer-time revalidation 在 observer 內完成）----
        fresh = self.observer.observe()
        now = self._clock()

        aud = dict(
            original_decision_action=original.decision_action,
            original_decision_target_kw=original.decision_target_power_kw,
            original_ess_age_sec=original.ess_age_at_decision_sec,
            original_meter_age_sec=original.meter_age_at_decision_sec,
            fresh_decision_action=fresh.decision_action,
            fresh_decision_target_kw=fresh.decision_target_power_kw,
            fresh_ess_age_sec=fresh.ess_age_at_decision_sec,
            fresh_meter_age_sec=fresh.meter_age_at_decision_sec,
            pcs_state=fresh.pcs_state, grid_state=fresh.grid_state,
            tou_state=fresh.tou_state, soc_percent=fresh.soc_percent,
            arbitrated_at=now)

        # ---- 3. Fresh 觀測必須有效 ----
        if fresh.stale_blocked or original.stale_blocked:
            src = fresh if fresh.stale_blocked else original
            return self._res(ARB_BLOCKED, R_STALE_AT_AUTHORIZATION,
                             stale_blocked=True, **aud,
                             detail=f"{src.reason}：{src.detail}")
        if not fresh.valid or not original.valid:
            src = fresh if not fresh.valid else original
            return self._res(ARB_BLOCKED, R_OBSERVATION_INVALID, **aud,
                             detail=f"{src.reason}：{src.detail}")

        # ---- 4. Decision revalidation ----
        if (original.decision_action != fresh.decision_action
                or not _same_target(original.decision_target_power_kw,
                                    fresh.decision_target_power_kw)):
            return self._res(ARB_BLOCKED, R_DECISION_CHANGED, **aud,
                             detail=f"決策已改變："
                                    f"{original.decision_action}"
                                    f"@{original.decision_target_power_kw}"
                                    f" → {fresh.decision_action}"
                                    f"@{fresh.decision_target_power_kw}")

        ess_obs = fresh.ess_observation
        aud["alarm_source_complete"] = ess_obs.alarm_source_complete
        aud["schedule_switch"] = ess_obs.pcs_mode_state.get("schedule_switch")
        aud["manual_switch"] = ess_obs.pcs_mode_state.get("manual_switch")

        # ---- 5. 告警來源必須可信（空清單 ≠ 沒有告警）----
        if ess_obs.alarm_source_complete is not True:
            return self._res(ARB_BLOCKED, R_ALARM_SOURCE_INCOMPLETE, **aud,
                             detail=f"alarm_source={ess_obs.alarm_source_reason}")

        # ---- 6. 沒有意圖就沒有 leg ----
        if fresh.would_action is None:
            return self._res(ARB_NO_CONTROL, R_NO_DECISION, **aud,
                             would_action=None, control_action=PCI.CTRL_NONE,
                             detail=f"decision={fresh.decision_action}"
                                    f"/{fresh.decision_reason}")

        rec, trust = (None, None)
        if self._last_control_provider is not None:
            rec, trust = self._last_control_provider()

        inputs = PCI.PipelineInputs(
            decision=fresh.decision_result, ess=ess_obs.ess,
            pcs_fault=ess_obs.pcs_fault, alarm_rows=ess_obs.alarm_rows,
            alarm_source_complete=ess_obs.alarm_source_complete,
            pcs_mode_state=ess_obs.pcs_mode_state,
            last_control=None,                    # C_SWITCH 未配置 → 規則未啟用
            last_control_record=rec, last_control_trust=trust)

        # ---- 7. Control Authority（重用既有，不建第二套）----
        auth = CA.evaluate(PCI.build_authority_request(inputs),
                           policy=self.authority_policy, now=now)
        aud.update(authority_state=auth.state, authority_reason=auth.reason)

        # ---- 8. Direction Interlock + IDLE 語意（重用既有）----
        ctrl = PCI.build_control_request(fresh.decision_result, ess=ess_obs.ess,
                                         authority=auth)
        aud["direction_reason"] = ctrl.reason

        if ctrl.action == PCI.CTRL_NONE:
            r = ctrl.reason
            if r in PCI.DIRECTION_BLOCK_REASONS:
                if r != PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP:
                    return self._res(ARB_BLOCKED, R_DIRECTION_BLOCKED, **aud,
                                     control_action=PCI.CTRL_NONE,
                                     would_action=fresh.would_action,
                                     detail=ctrl.detail)
                # 🔴 反向切換：本層明確另外形成一個 STOP leg（單一 command）。
                ctrl = PCI.build_control_request(_IdleDecision(), ess=ess_obs.ess,
                                                 authority=auth)
                aud["direction_reason"] = R_STOP_REQUIRED_FOR_REVERSAL
                if ctrl.action != PCI.CTRL_STOP:
                    return self._res(ARB_BLOCKED, R_DIRECTION_BLOCKED, **aud,
                                     control_action=ctrl.action,
                                     would_action=fresh.would_action,
                                     detail=f"無法形成 STOP leg：{ctrl.reason}")
            elif r.startswith("CONTROL_AUTHORITY_"):
                return self._res(ARB_BLOCKED, R_AUTHORITY_BLOCKED, **aud,
                                 control_action=PCI.CTRL_NONE,
                                 would_action=fresh.would_action,
                                 detail=ctrl.detail)
            elif r in (PCI.CR_IDLE_ALREADY_STANDBY, PCI.CR_IDLE_ALREADY_STOPPED):
                # Decision=IDLE 且設備已閒置 → 不需要任何 leg（不得反覆建立 STOP）
                return self._res(ARB_NO_CONTROL, R_IDLE_NO_LEG, **aud,
                                 control_action=PCI.CTRL_NONE,
                                 would_action=fresh.would_action,
                                 detail=ctrl.detail)
            else:
                return self._res(ARB_BLOCKED, r, **aud,
                                 control_action=PCI.CTRL_NONE,
                                 would_action=fresh.would_action,
                                 detail=ctrl.detail)

        # ---- 9. Duplicate suppression（desired state ≠ command edge）----
        want_state = DESIRED_RUNNING_STATE.get(ctrl.action)
        if (want_state is not None and fresh.pcs_state == want_state
                and auth.state == CA.AUTH_OWNED and rec is not None):
            if _same_target(getattr(rec, "target_power_kw", None),
                            ctrl.target_power_kw):
                return self._res(ARB_SUPPRESSED, R_ALREADY_AT_DESIRED_STATE,
                                 **aud, control_action=ctrl.action,
                                 would_action=fresh.would_action,
                                 duplicate_suppressed=True,
                                 detail=f"PCS 已在 {want_state} 且目標未改變 "
                                        f"→ 不建立新的 control leg")
            # 🔴 OPEN DESIGN ITEM：同向但目標功率改變時，Authority 於轉態期間
            #    會以舊 LastControl 目標比對實際功率而落入 CONFLICT，
            #    正式語意尚未裁定 → Fail Closed，不猜測。
            return self._res(ARB_BLOCKED, R_TARGET_CHANGE_UNSUPPORTED, **aud,
                             control_action=ctrl.action,
                             would_action=fresh.would_action,
                             detail=f"同向目標由 {getattr(rec, 'target_power_kw', None)}"
                                    f" 變更為 {ctrl.target_power_kw}"
                                    f"（OPEN DESIGN ITEM，未裁定前不授權）")

        # ---- 10. Safety Gate（重用既有；原 reason 完整保留）----
        sreq = PCI.build_safety_request(ctrl, inputs)
        sres = self.gate.check(sreq, now=now)
        aud.update(safety_allowed=sres.allowed, safety_reason=sres.reason,
                   safety_check_count=len(sres.checks))
        if not sres.allowed:
            # 🔴 不得把 Safety 的原因換成 generic BLOCKED 而失去診斷力
            return self._res(ARB_BLOCKED, R_SAFETY_BLOCKED, **aud,
                             control_action=ctrl.action,
                             would_action=fresh.would_action,
                             detail=f"{sres.reason}：{sres.detail}")

        # ---- 11. Production 參數就緒（None ≠ 無限制／永不過期）----
        missing = self._missing_config()
        if missing:
            return self._res(ARB_NOT_READY, R_CONFIG_NOT_READY, **aud,
                             control_action=ctrl.action,
                             would_action=fresh.would_action,
                             missing_config=missing,
                             detail=f"尚未取得正式數值：{list(missing)}")

        # ---- 12. 核發一次性授權 ----
        binding = PAZ.AuthorizationBinding(
            ess_read_started_at=fresh.ess_read_started_at,
            ess_read_completed_at=fresh.ess_read_completed_at,
            meter_received_at=fresh.meter_received_at,
            authority_state=auth.state, authority_reason=auth.reason,
            pcs_state=fresh.pcs_state,
            schedule_switch=aud["schedule_switch"],
            manual_switch=aud["manual_switch"],
            decision_action=fresh.decision_action,
            decision_reason=fresh.decision_reason,
            decision_target_power_kw=fresh.decision_target_power_kw,
            grid_state=fresh.grid_state, tou_state=fresh.tou_state,
            soc_percent=fresh.soc_percent, safety_reason=sres.reason,
            safety_check_count=len(sres.checks),
            alarm_source_complete=ess_obs.alarm_source_complete,
            direction_reason=aud["direction_reason"])

        self._seq += 1
        az, why = PAZ.issue(ctrl.action, ctrl.target_power_kw, now,
                            self.config.authorization_ttl_sec, binding, self._seq)
        if az is None:
            return self._res(ARB_NOT_READY, why, **aud,
                             control_action=ctrl.action,
                             would_action=fresh.would_action,
                             detail="授權票有效期未取得正式依據 → 不核發")

        self._pending = az
        return self._res(ARB_AUTHORIZED, R_AUTHORIZED, authorized=True,
                         valid=True, **aud, control_action=ctrl.action,
                         would_action=fresh.would_action, authorization=az,
                         authorization_id=az.authorization_id,
                         authorization_issued_at=az.issued_at_monotonic,
                         authorization_expires_at=az.expires_at(),
                         authorization_state=az.state,
                         detail=f"已授權 {ctrl.action}"
                                f"{'' if ctrl.target_power_kw is None else f'@{ctrl.target_power_kw:g}kW'}"
                                f" —— **尚未送出，且本階段無法送出**")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Production 仲裁與授權（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Production Arbiter（D.3-D，完全離線）==\n")
    print(f"  未注入 observer：{ProductionArbiter().evaluate()}")
    print(f"\n  Authorization 必要參數：{list(AUTHORIZATION_REQUIRED_PARAMS)}")
    print(f"  production 缺少：{list(ProductionArbiter()._missing_config())}")
    print("\n  ⚠ 本模組沒有 executor、沒有 operator、沒有 POST；"
          "dispatch 恆為 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
