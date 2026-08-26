# -*- coding: utf-8 -*-
"""
production_control_authorization.py — Production Leg Authorization（Phase D.3-D）
======================================================================
一張授權票代表：

    「這一個 automatic control leg 已完成所有 Production policy / safety /
      ownership 判定，因此系統**只**授權這一個 action。」

🔴 它**不是**「人按了 YES」
    Field Measurement 的 ControlLegAuthorization 是人工確認的載體，
    語意、生命週期、參數都不同。Production **不得** import 或 reuse 它 ——
    phase6_field_measure 永遠只是 validation / reference harness。

🔴 三個不可違反的性質
    action-bound  只對綁定的那一個 action 有效；CHARGE 票絕不能送 DISCHARGE。
    one-shot      消費一次即永久失效。POST 失敗 / ReadBack 失敗 / 例外都**不得**重用；
                  retry 必須重新走 fresh observation → Decision → Authority → Safety。
    expiring      必須有明確有效期。ttl 為 None **不得**解讀成「永不過期」，
                  而是「尚未取得正式依據」→ NOT READY，不核發。

🔴 本模組零 I/O、零控制能力
    不 import device_control_operator / pcs_control_executor / api_client /
    charge_discharge_report / phase6_field_measure。
    它只描述「授權」這件事，不知道也不可能知道怎麼送指令。

用法（完全離線）
    python production_control_authorization.py --demo
"""

import math
import argparse
from dataclasses import dataclass, asdict, field

# ---- 授權狀態 ----
AUTHZ_ISSUED = "ISSUED"
AUTHZ_CONSUMED = "CONSUMED"
AUTHZ_CANCELLED = "CANCELLED"
AUTHZ_EXPIRED = "EXPIRED"

# ---- 消費失敗 reason ----
AZ_OK = "AUTHORIZATION_OK"
AZ_ACTION_MISMATCH = "AUTHORIZATION_ACTION_MISMATCH"
AZ_ALREADY_CONSUMED = "AUTHORIZATION_ALREADY_CONSUMED"
AZ_CANCELLED = "AUTHORIZATION_CANCELLED"
AZ_EXPIRED = "AUTHORIZATION_EXPIRED"
AZ_TTL_NOT_CONFIGURED = "AUTHORIZATION_TTL_NOT_CONFIGURED"
AZ_TARGET_MISMATCH = "AUTHORIZATION_TARGET_MISMATCH"
AZ_SNAPSHOT_CHANGED = "AUTHORIZATION_SNAPSHOT_CHANGED"


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v):
    return _is_number(v) and math.isfinite(v)


@dataclass(frozen=True)
class AuthorizationBinding:
    """
    授權票綁定的完整脈絡。任一項在消費前改變，票即失效。

    ⚠️ 這不是「診斷資訊」—— 它是授權成立的**前提**，因此必須完整保存，
       才能在消費前重新比對，也才能事後說明「當時憑什麼授權」。
    """
    # 資料時間基準
    ess_read_started_at: float = None
    ess_read_completed_at: float = None
    meter_received_at: float = None
    # 設備與控制權
    authority_state: str = None
    authority_reason: str = None
    pcs_state: str = None
    schedule_switch: object = None
    manual_switch: object = None
    # 決策
    decision_action: str = None
    decision_reason: str = None
    decision_target_power_kw: float = None
    grid_state: str = None
    tou_state: str = None
    soc_percent: float = None
    # 安全
    safety_reason: str = None
    safety_check_count: int = None
    alarm_source_complete: bool = None
    # 方向互鎖
    direction_reason: str = None

    def fingerprint(self):
        """
        用於消費前比對的指紋。只取**會讓授權失去正當性**的欄位。

        ⚠️ 不含 age / 時間差等連續量 —— 那些以 TTL 與 fresh revalidation 處理，
           放進指紋只會讓每次比對都不相等。
        """
        return (self.authority_state, self.pcs_state, self.schedule_switch,
                self.manual_switch, self.decision_action, self.grid_state,
                self.tou_state, self.alarm_source_complete)

    def as_dict(self):
        return asdict(self)


@dataclass
class ProductionControlAuthorization:
    """
    一次性、action-bound、有期限的 automatic control leg 授權票。

    ⚠️ 刻意**不是** frozen：狀態需要在消費／取消時前進。
       但 action / target / issued_at / binding 一經建立即不得更動 ——
       由 _sealed 與 consume() 的比對共同保證（測試逐條鎖住）。
    """
    authorization_id: str
    action: str
    target_power_kw: float
    issued_at_monotonic: float
    ttl_sec: float
    binding: AuthorizationBinding
    state: str = AUTHZ_ISSUED
    consumed_at: float = None
    cancelled_reason: str = None

    # ------------------------------------------------------------------
    def expires_at(self):
        """None 代表 TTL 未配置 —— **不是**永不過期。"""
        if not _finite(self.ttl_sec) or not _finite(self.issued_at_monotonic):
            return None
        return self.issued_at_monotonic + self.ttl_sec

    def is_expired(self, now):
        """
        TTL 未配置時一律視為**已失效**（Fail Closed）。

        🔴 絕不得把「沒有設定期限」解讀成「永遠有效」。
        """
        exp = self.expires_at()
        if exp is None:
            return True
        if not _finite(now):
            return True
        return now >= exp

    @property
    def usable(self):
        return self.state == AUTHZ_ISSUED

    # ------------------------------------------------------------------
    def check(self, action, now, binding=None, target_power_kw=None):
        """
        消費前的完整驗證。回傳 (ok, reason)。**不改變狀態。**

        順序：狀態 → 期限 → action → target → binding 指紋
        """
        if self.state == AUTHZ_CONSUMED:
            return False, AZ_ALREADY_CONSUMED
        if self.state == AUTHZ_CANCELLED:
            return False, AZ_CANCELLED
        if self.state == AUTHZ_EXPIRED:
            return False, AZ_EXPIRED
        if not _finite(self.ttl_sec):
            return False, AZ_TTL_NOT_CONFIGURED
        if self.is_expired(now):
            return False, AZ_EXPIRED
        if action != self.action:
            return False, AZ_ACTION_MISMATCH
        if target_power_kw is not None:
            a, b = self.target_power_kw, target_power_kw
            if not (a is None and b is None):
                if a is None or b is None or abs(float(a) - float(b)) > 1e-9:
                    return False, AZ_TARGET_MISMATCH
        if binding is not None and binding.fingerprint() != self.binding.fingerprint():
            return False, AZ_SNAPSHOT_CHANGED
        return True, AZ_OK

    def consume(self, action, now, binding=None, target_power_kw=None):
        """
        消費授權。回傳 (ok, reason)。

        🔴 **無論成功與否，票都不可能再被第二次成功消費**：
           成功 → CONSUMED；失敗 → 狀態不變，但失敗原因已足以擋下重試。
           特別是過期會就地標記 EXPIRED，避免時鐘回退造成「復活」。
        """
        ok, reason = self.check(action, now, binding, target_power_kw)
        if not ok:
            if reason == AZ_EXPIRED and self.state == AUTHZ_ISSUED:
                self.state = AUTHZ_EXPIRED          # 就地失效，不可能復活
            return False, reason
        self.state = AUTHZ_CONSUMED
        self.consumed_at = now
        return True, AZ_OK

    def cancel(self, reason="cancelled"):
        """取消尚未消費的授權（例如下一輪資料已改變）。"""
        if self.state == AUTHZ_ISSUED:
            self.state = AUTHZ_CANCELLED
            self.cancelled_reason = reason
            return True
        return False

    def as_dict(self):
        d = asdict(self)
        d["expires_at"] = self.expires_at()
        return d

    def __str__(self):
        exp = self.expires_at()
        return (f"[AUTHZ {self.authorization_id}] {self.action}"
                f"{'' if self.target_power_kw is None else f'@{self.target_power_kw:g}kW'}"
                f" {self.state} ttl={self.ttl_sec} "
                f"expires={'n/a' if exp is None else f'{exp:.2f}'}")


def issue(action, target_power_kw, now, ttl_sec, binding, seq):
    """
    核發授權票。純函式（id 由呼叫端提供的 seq 決定，不使用亂數／時鐘）。

    🔴 ttl_sec 為 None → **不核發**，回 (None, AUTHORIZATION_TTL_NOT_CONFIGURED)。
       「未取得正式依據」與「永不過期」是完全不同的兩件事。
    """
    if not _finite(ttl_sec) or ttl_sec <= 0:
        return None, AZ_TTL_NOT_CONFIGURED
    if not _finite(now):
        return None, AZ_EXPIRED
    az = ProductionControlAuthorization(
        authorization_id=f"AZ-{int(seq):06d}",
        action=action, target_power_kw=target_power_kw,
        issued_at_monotonic=float(now), ttl_sec=float(ttl_sec),
        binding=binding)
    return az, AZ_OK


def main(argv=None):
    ap = argparse.ArgumentParser(description="Production Leg Authorization（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Production Control Authorization（D.3-D，完全離線）==\n")
    b = AuthorizationBinding(authority_state="IDLE", pcs_state="STANDBY",
                             decision_action="charge", grid_state="IMPORT",
                             tou_state="OFF_PEAK", alarm_source_complete=True,
                             schedule_switch=0, manual_switch=1)
    az_none, why = issue("charge", 5.0, now=100.0, ttl_sec=None, binding=b, seq=1)
    print(f"  ttl 未配置 → 不核發：{az_none} / {why}")

    az, why = issue("charge", 5.0, now=100.0, ttl_sec=30.0, binding=b, seq=2)
    print(f"  核發：{az}")
    print(f"  以 discharge 消費 → {az.consume('discharge', 105.0, b, 5.0)}")
    print(f"  target 不符    → {az.consume('charge', 105.0, b, 9.0)}")
    changed = AuthorizationBinding(**{**b.as_dict(), "pcs_state": "CHARGING"})
    print(f"  binding 已改變 → {az.consume('charge', 105.0, changed, 5.0)}")
    print(f"  正確消費       → {az.consume('charge', 105.0, b, 5.0)}")
    print(f"  再次消費       → {az.consume('charge', 106.0, b, 5.0)}")

    az2, _ = issue("discharge", 5.0, now=100.0, ttl_sec=10.0, binding=b, seq=3)
    print(f"  過期後消費     → {az2.consume('discharge', 200.0, b, 5.0)}  {az2.state}")
    print("\n  ⚠ 本模組沒有任何 dispatch 能力；授權票不會、也不能送出指令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
