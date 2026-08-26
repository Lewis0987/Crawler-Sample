# -*- coding: utf-8 -*-
"""
ess_snapshot_adapter.py — Production ESS Snapshot Adapter（Phase D.3-B）
======================================================================
唯一責任：把 `read_all()` 的 raw reading 轉成 Production Control 需要的觀測。

    reader() ──► raw reading dict ──► ControlObservation
                 + read 起訖 monotonic

只回答一個問題：**「設備現在回報什麼」**。

🔴 本模組**不做**（責任邊界，不得擴張）
    TOU / Grid Power 分類 / Decision / 充放電政策 / Control Authority /
    Direction Interlock / Safety Gate / Authorization / Executor /
    LastControl ownership / Report trigger

🔴 零控制能力
    不 import device_control_operator / pcs_control_executor / device_control_menu /
    api_client / requests。reader 一律由呼叫端注入；未注入即 fail closed。

🔴 不重新發明 PCS 狀態分類
    一律重用已通過實機驗證的 pcs_control_integration.pcs_state_from_ess()
    （純函式、零 I/O）。四旗標 C/D/S/R 的語意不因本階段改變。

🔴 不 import phase6_field_measure
    Field Measurement 永遠只是 validation / reference harness。
    其 freshness / timing / state mapping 的經驗以正式 library 重新組裝，不得直接沿用其程式碼。

Freshness（Phase 6.5-G 實機教訓，必須正式保留）
    age 的基準是 **read_started_at**，不是 read_completed_at。
    read_all() 的 GET latency 可達數秒；若從「讀完」才開始計時，
    一份實際上已經 5 秒舊的資料會被當成 age=0。
    age = now - read_started_at（now 於讀取**完成後**取得）→ age >= read_duration。

用法（完全離線；不注入 reader 即不可能碰設備）
    python ess_snapshot_adapter.py --demo
"""

import time
import math
import argparse
from dataclasses import dataclass, asdict

import decision_engine as DE                # EssSnapshot / ess_snapshot_from_reading（純函式）
import safety_gate as SG                    # alarm_source_complete_from_reading（純函式）
import pcs_control_integration as PCI       # pcs_state_from_ess（純函式，零 I/O）

# ======================================================================
# reason
# ======================================================================
OBS_OK = "OBSERVATION_OK"
OBS_READER_NOT_CONFIGURED = "READER_NOT_CONFIGURED"
OBS_READER_EXCEPTION = "READER_EXCEPTION"
OBS_READING_NOT_DICT = "READING_NOT_DICT"
OBS_ESS_INVALID = "ESS_SNAPSHOT_INVALID"
OBS_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
OBS_FIELD_INVALID = "REQUIRED_FIELD_INVALID"
OBS_MODE_UNAVAILABLE = "PCS_MODE_UNAVAILABLE"
OBS_ALARM_SOURCE_INCOMPLETE = "ALARM_SOURCE_INCOMPLETE"
OBS_PCS_STATE_UNUSABLE = "PCS_STATE_UNUSABLE"

# ======================================================================
# Production Control 真正需要的欄位（**不是** read_all 的全部資料）
# ======================================================================
# 數值型必要欄位（缺失或非有限值 → Fail Closed）
#   soc_percent            Decision Policy 運轉帶 + Safety Gate 硬限制
#   actual_active_power_kw Control Authority 的功率比對（只比絕對值）
REQUIRED_NUMERIC = ("soc_percent", "actual_active_power_kw")

# 布林型必要欄位（必須是明確的 True/False；None 一律 Fail Closed）
#   四旗標           Direction Interlock / PCS state 的唯一依據
#   pcs_fault_flag   Safety Gate 故障判定
# 🔴 刻意**不**採用 charge_discharge_report.pcs_is_fault 的中文 badge fallback：
#    那是顯示語言相依的啟發式，適合人工介面，不適合自動控制。
#    旗標缺失時寧可 Fail Closed，也不從 badge 文字猜測故障與否。
REQUIRED_BOOL = ("pcs_charging_flag", "pcs_discharging_flag",
                 "pcs_standby_flag", "pcs_running_flag", "pcs_fault_flag")


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _mode_switch(v):
    """
    把 read_all 的模式欄位正規化成 Safety Gate / Control Authority 使用的 0/1。

    True/1 → 1、False/0 → 0、其餘（含 None、字串、浮點）→ None（無法確認）。
    ⚠️ 只做**型別正規化**，不做任何價值判斷 ——
       「schedule_switch=1 代表智慧模式因此不可控制」是 Safety Gate / Authority 的職責。
    """
    if v is True or v == 1:
        return 1
    if v is False or v == 0:
        return 0
    return None


@dataclass(frozen=True)
class ControlObservation:
    """
    一次觀測的完整結果。**只包含 Production Control 需要的資料。**

    valid=False 時不得用於任何決策；reason 一律明確，不得為空。
    ⚠️ pcs_state 即使在 valid=False 時也會如實填入（含 UNKNOWN / CONFLICT），
       供稽核判讀 —— 但**不得**因此推論設備狀態可信。
    """
    valid: bool
    reason: str
    ess: object = None                 # decision_engine.EssSnapshot（可能 valid=False）
    pcs_state: str = None
    pcs_mode_state: dict = None        # {schedule_switch, manual_switch}
    pcs_fault: bool = None
    alarm_rows: tuple = ()
    alarm_source_complete: bool = False
    alarm_source_reason: str = None
    read_started_at: float = None
    read_completed_at: float = None
    read_duration_sec: float = None
    age_sec: float = None
    stale: bool = None
    detail: str = ""

    def as_dict(self):
        d = asdict(self)
        d["ess"] = self.ess.as_dict() if self.ess is not None else None
        return d

    def as_json_dict(self):
        d = self.as_dict()
        if isinstance(d.get("ess"), dict):
            for k, v in d["ess"].items():
                if isinstance(v, float) and not math.isfinite(v):
                    d["ess"][k] = None
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        if not self.valid:
            extra = f" {self.detail}" if self.detail else ""
            return f"[OBS] INVALID reason={self.reason}{extra}"
        age = "n/a" if self.age_sec is None else f"{self.age_sec:.2f}s"
        return (f"[OBS] VALID pcs={self.pcs_state} soc={self.ess.soc_percent:.1f}% "
                f"age={age} read={self.read_duration_sec:.2f}s "
                f"alarm_complete={self.alarm_source_complete}")


class EssSnapshotAdapter:
    """
    Production ESS 觀測邊界。**無狀態**：每次 observe() 都是獨立的一次讀取。

    reader
        由呼叫端注入（production 為 charge_discharge_report.read_all 的薄包裝）。
        **未注入即不可能碰設備** —— 回 READER_NOT_CONFIGURED。
    clock
        monotonic 時鐘，可注入以利離線測試。

    🔴 本類別**不快取**任何 snapshot。
       上一筆成功的觀測絕不能在本次讀取失敗時被沿用 ——
       那會讓「設備已離線 10 分鐘」看起來像「一切正常」。
       物件上刻意沒有任何 last / cache / previous 欄位，並由測試鎖住。
    """

    def __init__(self, reader=None, clock=None, ess_config=None):
        self._reader = reader
        self._clock = clock if clock is not None else time.monotonic
        # ⚠️ 沿用 Phase 6.3-A 既有且已裁示不得修改的新鮮度門檻，
        #    **不**在本階段新增或放寬任何 production 數值。
        self.ess_config = ess_config if ess_config is not None else DE.DEFAULT_ESS_CONFIG

    # ------------------------------------------------------------------
    def _fail(self, reason, detail, t0=None, t1=None, ess=None, pcs_state=None,
              mode=None, rows=(), complete=False, alarm_reason=None):
        dur = None if (t0 is None or t1 is None) else t1 - t0
        age = None if t0 is None else max(0.0, self._clock() - t0)
        return ControlObservation(
            valid=False, reason=reason, ess=ess, pcs_state=pcs_state,
            pcs_mode_state=mode, pcs_fault=None, alarm_rows=tuple(rows),
            alarm_source_complete=bool(complete), alarm_source_reason=alarm_reason,
            read_started_at=t0, read_completed_at=t1, read_duration_sec=dur,
            age_sec=age,
            stale=(None if age is None else age > self.ess_config.stale_after_sec),
            detail=detail)

    # ------------------------------------------------------------------
    def observe(self):
        """
        讀取一次並回傳 ControlObservation。**永遠不會拋出例外**。

        判定順序（先能不能讀 → 再結構 → 再 ESS → 再欄位 → 再模式 → 再告警 → 最後 PCS 狀態）
            1. 未注入 reader                → READER_NOT_CONFIGURED
            2. reader 拋例外                → READER_EXCEPTION
            3. reading 非 dict              → READING_NOT_DICT
            4. EssSnapshot 無效             → ESS_SNAPSHOT_INVALID
               （涵蓋 communication_ok=False / soc 缺失或非有限 / 讀太慢 / stale）
            5. Control 必要欄位缺失或非法   → REQUIRED_FIELD_MISSING / _INVALID
            6. 模式開關無法確認             → PCS_MODE_UNAVAILABLE
            7. 告警來源不完整               → ALARM_SOURCE_INCOMPLETE
            8. PCS 狀態為 UNKNOWN/CONFLICT  → PCS_STATE_UNUSABLE
        """
        if self._reader is None:
            return self._fail(OBS_READER_NOT_CONFIGURED,
                              "未注入 reader —— 本模組不會自行連線設備")

        t0 = self._clock()
        try:
            reading = self._reader()
        except Exception as e:                                # noqa: BLE001
            t1 = self._clock()
            return self._fail(OBS_READER_EXCEPTION, f"{type(e).__name__}: {e}", t0, t1)
        t1 = self._clock()

        if not isinstance(reading, dict):
            return self._fail(OBS_READING_NOT_DICT,
                              f"reading 型別為 {type(reading).__name__}", t0, t1)

        # ---- 4. EssSnapshot（重用 Phase 6.3-A 已驗證的純函式）----
        # now 於讀取**完成後**取得 → age 必然涵蓋 read_all 的耗時。
        ess = DE.ess_snapshot_from_reading(reading, t0, t1, now=self._clock(),
                                           config=self.ess_config)
        mode = {"schedule_switch": _mode_switch(reading.get("pcs_schedule_enabled")),
                "manual_switch": _mode_switch(reading.get("pcs_manual_switch"))}
        rows = tuple(reading.get("alarm_rows") or ())
        a = SG.alarm_source_complete_from_reading(reading)
        a_ok, a_reason = bool(getattr(a, "complete", False)), getattr(a, "reason", None)

        if not ess.valid:
            return self._fail(OBS_ESS_INVALID, f"ess.reason={ess.reason} {ess.detail}".strip(),
                              t0, t1, ess=ess, mode=mode, rows=rows,
                              complete=a_ok, alarm_reason=a_reason)

        # ---- 5. Control 專屬必要欄位 ----
        miss = [k for k in REQUIRED_NUMERIC + REQUIRED_BOOL if k not in reading]
        if miss:
            return self._fail(OBS_FIELD_MISSING, ",".join(sorted(miss)), t0, t1,
                              ess=ess, mode=mode, rows=rows, complete=a_ok,
                              alarm_reason=a_reason)
        bad = [k for k in REQUIRED_NUMERIC
               if not _is_number(reading[k]) or not math.isfinite(float(reading[k]))]
        bad += [k for k in REQUIRED_BOOL if reading[k] is not True and reading[k] is not False]
        if bad:
            return self._fail(OBS_FIELD_INVALID, ",".join(sorted(bad)), t0, t1,
                              ess=ess, mode=mode, rows=rows, complete=a_ok,
                              alarm_reason=a_reason)

        # PCS 狀態（重用既有純函式，不重新發明第二套分類器）
        pcs_state = PCI.pcs_state_from_ess(ess)

        # ---- 6. 模式開關必須能確認 ----
        if mode["schedule_switch"] is None or mode["manual_switch"] is None:
            return self._fail(OBS_MODE_UNAVAILABLE,
                              f"schedule={reading.get('pcs_schedule_enabled')!r} "
                              f"manual={reading.get('pcs_manual_switch')!r}",
                              t0, t1, ess=ess, pcs_state=pcs_state, mode=mode,
                              rows=rows, complete=a_ok, alarm_reason=a_reason)

        # ---- 7. 告警來源必須可證明完整 ----
        # 🔴 「alarm list 為空」**不等於**「沒有告警」。端點失敗或分頁截斷時
        #    rows 同樣是空的 —— 唯一能分辨的是 provenance。
        if not a_ok:
            return self._fail(OBS_ALARM_SOURCE_INCOMPLETE, f"alarm_source={a_reason}",
                              t0, t1, ess=ess, pcs_state=pcs_state, mode=mode,
                              rows=rows, complete=False, alarm_reason=a_reason)

        # ---- 8. PCS 狀態必須可信（不得猜測）----
        if pcs_state in PCI.PCS_STATE_UNUSABLE:
            return self._fail(OBS_PCS_STATE_UNUSABLE,
                              f"pcs_state={pcs_state}（旗標矛盾或無法解釋，不猜測）",
                              t0, t1, ess=ess, pcs_state=pcs_state, mode=mode,
                              rows=rows, complete=a_ok, alarm_reason=a_reason)

        return ControlObservation(
            valid=True, reason=OBS_OK, ess=ess, pcs_state=pcs_state,
            pcs_mode_state=mode, pcs_fault=bool(reading["pcs_fault_flag"]),
            alarm_rows=rows, alarm_source_complete=True, alarm_source_reason=a_reason,
            read_started_at=ess.read_started_at, read_completed_at=ess.read_completed_at,
            read_duration_sec=ess.read_duration_sec, age_sec=ess.age_sec,
            stale=ess.stale, detail="")


# ======================================================================
# CLI（完全離線；使用假 reading，不連任何設備）
# ======================================================================
def _demo_reading(**over):
    r = {"communication_ok": True, "soc_percent": 50.0,
         "actual_active_power_kw": -1.3, "pcs_fault_flag": False,
         "pcs_charging_flag": False, "pcs_discharging_flag": False,
         "pcs_standby_flag": True, "pcs_running_flag": True,
         "battery_power_status": "已上電", "pcs_control_mode_code": "manual",
         "pcs_schedule_enabled": False, "pcs_manual_switch": 1,
         "alarm_rows": [], "alarm_total": 0, "alarm_total_raw": 0, "_fail": []}
    r.update(over)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description="Production ESS Snapshot Adapter（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Production ESS Snapshot Adapter（D.3-B，完全離線）==\n")
    print(f"  未注入 reader : {EssSnapshotAdapter().observe()}")

    cases = [
        ("正常（待機）", _demo_reading()),
        ("充電中", _demo_reading(pcs_charging_flag=True, pcs_standby_flag=False,
                                actual_active_power_kw=5.3)),
        ("已停機", _demo_reading(pcs_standby_flag=False, pcs_running_flag=False,
                               actual_active_power_kw=-1.3)),
        ("旗標矛盾", _demo_reading(pcs_charging_flag=True, pcs_discharging_flag=True,
                                pcs_standby_flag=False)),
        ("通訊失敗", _demo_reading(communication_ok=False)),
        ("告警來源不完整", _demo_reading(alarm_total_raw=None)),
        ("模式無法確認", _demo_reading(pcs_manual_switch=None)),
    ]
    for label, rd in cases:
        obs = EssSnapshotAdapter(reader=lambda r=rd: r).observe()
        print(f"  {label:<16} {obs}")

    def _boom():
        raise RuntimeError("reader failed")

    print(f"  {'reader 例外':<16} {EssSnapshotAdapter(reader=_boom).observe()}")
    print("\n  ⚠ 全程未 import device_control_operator / executor / api_client，"
          "未連線任何設備。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
