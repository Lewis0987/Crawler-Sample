# -*- coding: utf-8 -*-
"""
meter_observation_adapter.py — Production Grid Meter 觀測邊界（Phase D.3-C）
======================================================================
唯一責任：把電表資料來源的一筆結果轉成 Production Control 需要的觀測。

    source() ──► MeterSnapshot ──► MeterObservation

🔴 **不重複建立第二套型別**
    meter_client.MeterSnapshot 已是 production-safe 且已驗證的契約
    （valid / reason / detail / power_kw / received_at / age_sec / stale）。
    MeterObservation 只在其外層補上 Adapter 邊界真正缺少的三件事：
        1. source 注入與例外收斂（來源可能拋例外）
        2. observed_at —— Adapter 取樣時刻，供 consumer-time freshness 使用
        3. 與 ESS 觀測一致的 reason 命名空間
    power_kw / age_sec / stale 一律以 property **委派**給 snapshot，不另存一份。

🔴 資料來源契約（Phase 6.1 已確認，不得改變）
    台電電錶瞬時功率 → payload 的 `meter` → MeterSnapshot.power_kw  ← Grid 判斷唯一來源
    原始需求量       → payload 的 `demand` → MeterSnapshot.demand_kw ← **僅診斷，不參與判定**
    🔴 **不得**把「原始需求量」當成台電瞬時功率 —— 兩者是不同的量測。
    本模組不自行解析 payload，一律由 meter_client 既有且已驗證的 validation 負責，
    避免出現第二套解讀。

🔴 零控制能力
    不 import device_control_operator / pcs_control_executor / api_client /
    charge_discharge_report / phase6_field_measure。
    source 由呼叫端注入；未注入即 fail closed，不可能自行連線。

用法（完全離線）
    python meter_observation_adapter.py --demo
"""

import time
import math
import argparse
from dataclasses import dataclass, asdict

import meter_client as MC

# ======================================================================
# reason
# ======================================================================
MOBS_OK = "METER_OBSERVATION_OK"
MOBS_SOURCE_NOT_CONFIGURED = "METER_SOURCE_NOT_CONFIGURED"
MOBS_SOURCE_EXCEPTION = "METER_SOURCE_EXCEPTION"
MOBS_NOT_SNAPSHOT = "METER_NOT_A_SNAPSHOT"
MOBS_SNAPSHOT_INVALID = "METER_SNAPSHOT_INVALID"
MOBS_POWER_NOT_FINITE = "METER_POWER_NOT_FINITE"
MOBS_TIMESTAMP_UNTRUSTED = "METER_TIMESTAMP_UNTRUSTED"


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class MeterObservation:
    """
    一次電表觀測。**不複製** MeterSnapshot 的欄位，一律委派。

    observed_at
        Adapter 取樣時刻（monotonic）。與 snapshot.received_at 不同 ——
        後者是資料抵達時刻，前者是我們把它取出來看的時刻。
        consumer-time freshness 一律以 received_at 為基準重算，observed_at 只供稽核。
    """
    valid: bool
    reason: str
    snapshot: object = None            # meter_client.MeterSnapshot
    observed_at: float = None
    detail: str = ""

    # ---- 委派（不另存一份，避免兩處數值 drift）----
    @property
    def power_kw(self):
        return getattr(self.snapshot, "power_kw", None)

    @property
    def received_at(self):
        return getattr(self.snapshot, "received_at", None)

    @property
    def age_sec(self):
        return getattr(self.snapshot, "age_sec", None)

    @property
    def stale(self):
        return getattr(self.snapshot, "stale", None)

    @property
    def source_reason(self):
        return getattr(self.snapshot, "reason", None)

    def as_dict(self):
        d = asdict(self)
        d["snapshot"] = self.snapshot.as_dict() if self.snapshot is not None else None
        d.update(power_kw=self.power_kw, received_at=self.received_at,
                 age_sec=self.age_sec, stale=self.stale,
                 source_reason=self.source_reason)
        return d

    def __str__(self):
        if not self.valid:
            extra = f" {self.detail}" if self.detail else ""
            return f"[METER] INVALID reason={self.reason}{extra}"
        return (f"[METER] VALID power={self.power_kw:+.2f} kW "
                f"age={self.age_sec:.2f}s")


class MeterObservationAdapter:
    """
    電表觀測邊界。**無狀態**：每次 observe() 都獨立取樣。

    source
        由呼叫端注入（production 為 meter_client.MeterClient.get_snapshot）。
        **未注入即不可能連線** → METER_SOURCE_NOT_CONFIGURED。

    🔴 不快取。上一筆有效的 power 絕不能在本次失敗時被沿用 ——
       那會讓「電表已離線」看起來像「電網功率沒變」。
    """

    def __init__(self, source=None, clock=None):
        self._source = source
        self._clock = clock if clock is not None else time.monotonic

    def _fail(self, reason, detail, snap=None, at=None):
        return MeterObservation(valid=False, reason=reason, snapshot=snap,
                                observed_at=at, detail=detail)

    def observe(self, now=None):
        """
        取樣一次。**永遠不會拋出例外**。

        判定順序
            1. 未注入 source            → METER_SOURCE_NOT_CONFIGURED
            2. source 拋例外            → METER_SOURCE_EXCEPTION
            3. 非 MeterSnapshot         → METER_NOT_A_SNAPSHOT
            4. snapshot.valid 不成立    → METER_SNAPSHOT_INVALID
               （涵蓋 NO_DATA / payload 非法 / 欄位缺失 / 型別錯 / 非有限值 /
                 meter_state 異常 / STALE 等既有 reason，一律原樣帶出）
            5. received_at 不可信       → METER_TIMESTAMP_UNTRUSTED
            6. power_kw 非有限數        → METER_POWER_NOT_FINITE（防禦性重檢）
        """
        at = self._clock()
        if self._source is None:
            return self._fail(MOBS_SOURCE_NOT_CONFIGURED,
                              "未注入 source —— 本模組不會自行連線電表", at=at)
        try:
            snap = self._source() if now is None else self._source(now)
        except Exception as e:                                # noqa: BLE001
            return self._fail(MOBS_SOURCE_EXCEPTION, f"{type(e).__name__}: {e}",
                              at=at)

        if not isinstance(snap, MC.MeterSnapshot):
            return self._fail(MOBS_NOT_SNAPSHOT,
                              f"來源回傳 {type(snap).__name__}，不是 MeterSnapshot",
                              at=at)
        if snap.valid is not True:
            return self._fail(MOBS_SNAPSHOT_INVALID,
                              f"meter.reason={snap.reason} {snap.detail}".strip(),
                              snap, at)
        # 🔴 時間戳不可信 → 無法計算任何 freshness → Fail Closed
        if not _is_number(snap.received_at) or not math.isfinite(snap.received_at):
            return self._fail(MOBS_TIMESTAMP_UNTRUSTED,
                              f"received_at={snap.received_at!r}", snap, at)
        # 防禦性重檢：meter_client 已驗證過，但控制端不得依賴上游永不出錯
        if not _is_number(snap.power_kw) or not math.isfinite(snap.power_kw):
            return self._fail(MOBS_POWER_NOT_FINITE, f"power_kw={snap.power_kw!r}",
                              snap, at)

        return MeterObservation(valid=True, reason=MOBS_OK, snapshot=snap,
                                observed_at=at, detail="")


# ======================================================================
# CLI（完全離線；使用假 payload，不連任何電表）
# ======================================================================
def _demo_payload(**over):
    """假 payload（欄位名依 meter_client 既有契約：meter / demand / *_state）。"""
    p = {"meter": 12.5, "meter_state": MC.S_OK,
         "demand": 8.0, "demand_state": MC.S_OK}
    p.update(over)
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Production 電表觀測 Adapter（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Production Meter Observation Adapter（D.3-C，完全離線）==\n")
    print(f"  未注入 source : {MeterObservationAdapter().observe()}")

    def snap_of(payload, age=0.0):
        return MC.evaluate(payload, received_at=1000.0, now=1000.0 + age)

    cases = [
        ("正常 IMPORT", snap_of(_demo_payload())),
        ("正常 EXPORT", snap_of(_demo_payload(meter=-9.0))),
        ("payload 非 mapping", snap_of("x")),
        ("台電電錶值缺失", snap_of({k: v for k, v in _demo_payload().items()
                                 if k != "meter"})),
        ("台電電錶值非有限", snap_of(_demo_payload(meter=float("nan")))),
        ("無資料", MC.no_data_snapshot()),
        ("stale", snap_of(_demo_payload(), age=MC.STALE_AFTER_SEC + 1.0)),
    ]
    for label, s in cases:
        print(f"  {label:<18} "
              f"{MeterObservationAdapter(source=lambda s=s: s).observe()}")

    def _boom():
        raise ConnectionError("socket closed")

    print(f"  {'source 例外':<18} "
          f"{MeterObservationAdapter(source=_boom).observe()}")
    print(f"  {'非 MeterSnapshot':<18} "
          f"{MeterObservationAdapter(source=lambda: {'power': 1}).observe()}")
    print("\n  ⚠ 全程未連線任何電表；payload 解讀完全委派 meter_client。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
