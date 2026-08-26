# -*- coding: utf-8 -*-
"""
pcs_auto_control_production.py — Production 相依注入（Phase D.5-B）
======================================================================
把已經各自完成並離線驗證的 Phase 6 模組，接成一條**真的會跑**的 production path：

    Meter → Tariff/TOU → Decision → Safety Gate → Control Authority
          → Layer 1 Interlock → Executor decision → ReadBack config → Report

🔴 **本階段只到「決定要不要送」為止，不送。**
    `dispatch_enabled = False`，且 executor **一律不建立**（見 build_executor）。
    因此不是「有出口但被旗標擋住」，而是**結構上沒有出口**。

🔴 **連線是明確的操作，不是預設行為**
    所有 build_* 預設 `client=None` → 回 None → 該來源 Fail Closed。
    要真的連上設備，必須由呼叫端明確傳入 client（或 `--observe` 明示）。
    ⚠️ 這樣「跑測試」與「跑服務」永遠不會意外連上真機。

🔴 **不得相依 phase6_field_measure**
    那是量測工具，不是 production 元件；本檔與服務層都不 import 它。

🔴 **不得為了讓 dispatch_ready=True 而補 None 參數**
    缺參數就是缺 —— readiness() 只回報，不修補。

用法（完全離線）
    python pcs_auto_control_production.py --show
"""

import os
import time
import argparse

import annual_off_peak_calendar as AC
import tariff_provider as TP
import decision_policy as DP
import last_control_store as LCS
import pcs_control_executor as EXC
import pcs_auto_control_config as CFG
import pcs_auto_control_runtime as RT

# ======================================================================
# 模式
# ======================================================================
MODE_OBSERVE_ONLY = "OBSERVE_ONLY"
# 🔴 目前**只有這一種**模式。要新增 dispatch 模式必須是另一次明確授權，
#    不得由旗標預設值悄悄開啟。
SUPPORTED_MODES = (MODE_OBSERVE_ONLY,)

# ---- 來源就緒狀態 ----
SRC_WIRED = "WIRED"                 # 已注入且可用
SRC_NOT_WIRED = "NOT_WIRED"         # 未注入 → 該層 Fail Closed
SRC_UNAVAILABLE = "UNAVAILABLE"     # 有意注入但建立失敗

# ---- 為什麼沒有送出指令（OBSERVE_ONLY 的最終理由）----
NO_ACTION_OBSERVE_ONLY = "OBSERVE_ONLY_MODE"
NO_ACTION_DISPATCH_DISABLED = "DISPATCH_DISABLED"
NO_ACTION_NO_EXECUTOR = "NO_EXECUTOR_WIRED"
NO_ACTION_CONFIG_NOT_READY = "CONFIG_NOT_READY"
NO_ACTION_UPSTREAM = "UPSTREAM_DECISION"      # 上游本來就沒有要送


# ======================================================================
# 各層相依
# ======================================================================
def build_ess_reader(client=None):
    """
    ESS 讀取來源：回傳一個 zero-arg callable（→ reading dict），或 None。

    🔴 `client=None` → 回 None → EssSnapshotAdapter 一律
       READER_NOT_CONFIGURED（Fail Closed）。**不會自行建立連線**。
    ⚠️ 這是**唯讀** GET；與任何控制出口無關。
    """
    if client is None:
        return None
    import charge_discharge_report as CDR
    return lambda: CDR.read_all(client)


def build_api_client(login=True):
    """
    建立 production API client。回傳 `(client, token)`。

    🔴 **必須登入**（Phase 6.7 實機驗證發現的 wiring 缺口）：
       `getRunMode` / `getScheduleSwitch` / `getDOAndDIMsg` 三支需要認證，
       未登入時回 401 → `pcs_schedule_enabled` / `pcs_manual_switch` 皆為 None
       → ESS 觀測 `PCS_MODE_UNAVAILABLE` → 整條鏈永遠 Fail Closed。
       行為本身是安全的，但**永遠不會有可用觀測**，等於整個 orchestrator 空轉。
    ⚠️ 登入失敗**不拋例外、不重試** —— 回傳 token=None 讓上層自行 Fail Closed；
       控制服務不得因登入失敗而改變任何控制判斷。
    ⚠️ 這裡會連線與認證，因此刻意獨立成必須被明確呼叫的函式，
       不放在任何 build_* 的預設路徑上。登入只取讀取權限，**不送任何控制**。
    """
    from api_client import ApiClient
    import charge_discharge_report as CDR
    client = ApiClient()
    if not login:
        return client, None
    try:
        token = client.login_hmi(CDR.USERNAME)
    except Exception:                     # noqa: BLE001 —— 邊界，交由上層 Fail Closed
        token = None
    return client, token


def build_meter_source(meter_client=None):
    """
    電表來源：回傳 zero-arg callable（→ MeterSnapshot），或 None。

    🔴 `meter_client=None` → 回 None → MeterObservationAdapter 一律
       SOURCE_NOT_CONFIGURED（Fail Closed）。
    ⚠️ 台電電錶 = payload.meter → MeterSnapshot.power_kw；
       原始需求量 = payload.demand → demand_kw（診斷用，不作為併網點方向依據）。
    """
    if meter_client is None:
        return None
    return meter_client.get_snapshot


def build_meter_client(url=None, connect=False):
    """
    建立 production MeterClient。`connect=False` 時**只建立不連線**。

    ⚠️ 與 build_api_client 同樣刻意獨立：連線是明確操作。
    """
    import meter_client as MC
    c = MC.MeterClient(url=url) if url else MC.MeterClient()
    if connect:
        c.start()
    return c


def build_holiday_provider():
    """
    正式離峰日來源 —— 直接使用已經人工驗收並 provision 的年度清單。

    🔴 不自行組裝、不讀檔、不連網路：清單的驗收憑據綁 dataset digest 與
       官方檔案 SHA-256，任一改動該年度就會自動從 production 消失。
    """
    return AC.PRODUCTION_PROVIDER


def build_tariff_provider(holiday_provider=None):
    """時段判定來源（Asia/Taipei；naive datetime 一律拒絕）。"""
    hp = holiday_provider if holiday_provider is not None else build_holiday_provider()
    return TP.TariffProvider(holiday_provider=hp)


def build_policy(config=None):
    """
    Decision Policy。功率取自 production config。

    🔴 `charge_power_kw` / `discharge_power_kw` 目前為 None →
       Decision 一律 Fail Closed 成 no_action。**不得**在此填任何值。
    """
    c = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    return DP.TouArbitragePolicy(config=DP.PolicyConfig(
        charge_power_kw=c.charge_power_kw,
        discharge_power_kw=c.discharge_power_kw))


def build_last_control_store(path=None):
    """
    正式 LastControl 儲存。**唯讀使用**：OBSERVE_ONLY 不會寫入任何紀錄
    （只有 VERIFY_SUCCESS 才寫，而本階段不可能有 VERIFY_SUCCESS）。
    """
    return LCS.LastControlStore(path=path or LCS.DEFAULT_STORE_PATH)


def build_last_control_provider(store=None):
    """
    回傳 zero-arg callable → `(record, trust)`，或 None。

    ⚠️ 仲裁層的契約是 `() -> (record, trust)` 這個 tuple，
       不是 `LastControlTrustResult` 本身；這裡負責轉換，
       讓儲存層的型別不必外洩到仲裁層。
    🔴 **只讀不寫**：只呼叫 current()，不可能寫入任何控制紀錄。
    """
    if store is None:
        return None

    def _current():
        cur = store.current()
        return cur.record, cur.trust

    return _current


def build_readback_config(config=None):
    """
    ReadBack 參數。三項皆已**裁示 FINAL**：
        timeout 75.0s / poll 5.0s / stability 1

    ⚠️ 這是「若要送，會怎麼驗」的設定；本階段不會真的驗，因為不會送。
    """
    c = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    return EXC.ReadBackConfig(
        timeout_sec=c.readback_timeout_sec,
        poll_interval_sec=c.readback_poll_interval_sec,
        stability_samples=c.readback_stability_samples)


def build_executor(mode=MODE_OBSERVE_ONLY):
    """
    控制出口。

    🔴 **OBSERVE_ONLY 一律回 None** —— 不建立 executor、不 import operator。
       這讓「不送指令」成為**結構事實**，而不是靠旗標判斷。
       即使上游全部放行、即使參數全部齊備，也沒有東西可以被呼叫。
    """
    if mode != MODE_OBSERVE_ONLY:
        raise ValueError(f"未支援的模式：{mode!r}（目前只允許 {MODE_OBSERVE_ONLY}）")
    return None


def build_verifier(mode=MODE_OBSERVE_ONLY):
    """ReadBack 驗證器。OBSERVE_ONLY 同樣不建立（沒有指令可驗）。"""
    if mode != MODE_OBSERVE_ONLY:
        raise ValueError(f"未支援的模式：{mode!r}")
    return None


# ======================================================================
# 就緒盤點
# ======================================================================
class WiringReport(object):
    """啟動時的相依與參數盤點。**只回報，不修補、不放行。**"""

    def __init__(self, mode, sources, config, readback, dispatch_enabled):
        self.mode = mode
        self.sources = dict(sources)
        self.config = config
        self.readback = readback
        self.dispatch_enabled = bool(dispatch_enabled)

    @property
    def missing_required(self):
        return tuple(self.config.missing_required())

    @property
    def dispatch_ready(self):
        return bool(self.config.dispatch_ready)

    @property
    def can_dispatch(self):
        """
        結構上是否可能送出指令。

        🔴 三個條件全部成立才有可能，而 OBSERVE_ONLY 至少破壞兩個：
           模式非 OBSERVE_ONLY、dispatch_enabled、且有 executor。
        """
        return bool(self.dispatch_enabled
                    and self.sources.get("executor") == SRC_WIRED
                    and self.mode != MODE_OBSERVE_ONLY)

    def as_dict(self):
        return {"mode": self.mode, "sources": dict(self.sources),
                "dispatch_enabled": self.dispatch_enabled,
                "dispatch_ready": self.dispatch_ready,
                "missing_required": list(self.missing_required),
                "readback_ready": bool(self.readback.ready),
                "can_dispatch": self.can_dispatch}

    def __str__(self):
        wired = [k for k, v in sorted(self.sources.items()) if v == SRC_WIRED]
        return (f"[WIRING {self.mode}] 已接={wired or '（無）'} "
                f"dispatch_enabled={self.dispatch_enabled} "
                f"dispatch_ready={self.dispatch_ready} "
                f"缺={len(self.missing_required)} 項 "
                f"can_dispatch={self.can_dispatch}")


def _state(obj):
    return SRC_WIRED if obj is not None else SRC_NOT_WIRED


def wiring_report(reader=None, meter_source=None, last_control_provider=None,
                  executor=None, verifier=None, holiday_provider=None,
                  config=None, mode=MODE_OBSERVE_ONLY):
    """盤點目前實際接上了什麼。"""
    c = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    return WiringReport(
        mode=mode,
        sources={"ess_reader": _state(reader),
                 "meter_source": _state(meter_source),
                 "holiday_provider": _state(holiday_provider),
                 "last_control": _state(last_control_provider),
                 "executor": _state(executor),
                 "verifier": _state(verifier)},
        config=c,
        readback=build_readback_config(c),
        dispatch_enabled=RT.DISPATCH_ENABLED)


# ======================================================================
# OBSERVE_ONLY 觀測紀錄
# ======================================================================
class ObserveRecord(object):
    """
    一輪 OBSERVE_ONLY 的完整可稽核紀錄 —— 供 Phase 6.7 事後分析。

    刻意把八件事攤平成獨立欄位，讓「為什麼沒有動作」永遠可以一眼看出來，
    而不是只留下一個 outcome。
    """

    FIELDS = ("meter_state", "meter_age_sec", "grid_state", "tariff_state",
              "decision_action", "decision_target_kw", "safety_allowed",
              "safety_reason", "authority_state", "authority_reason",
              "interlock_reason", "control_action", "would_action",
              "dispatch_ready", "dispatch_enabled", "executed",
              "outcome", "no_action_reason", "detail")

    def __init__(self, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw.get(f))

    def as_dict(self):
        return {f: getattr(self, f) for f in self.FIELDS}

    def __str__(self):
        return ("[OBSERVE] "
                f"meter={self.meter_state} grid={self.grid_state} "
                f"tariff={self.tariff_state} decision={self.decision_action} "
                f"safety={self.safety_reason} auth={self.authority_state} "
                f"interlock={self.interlock_reason} "
                f"would={self.would_action} executed={self.executed} "
                f"→ {self.outcome} / {self.no_action_reason}")


def observe_record(result, config=None):
    """
    ExecutionResult（或 ArbitrationResult）→ ObserveRecord。

    🔴 `executed` 直接取自執行層的事實欄位，不是推論 ——
       任何一輪只要它不是 False，就代表不變量被破壞。
    """
    c = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    arb = getattr(result, "arbitration", None) or result
    executed = bool(getattr(result, "executed", False))

    if executed:
        why = None
    elif not RT.DISPATCH_ENABLED:
        why = NO_ACTION_DISPATCH_DISABLED
    elif not c.dispatch_ready:
        why = NO_ACTION_CONFIG_NOT_READY
    else:
        why = NO_ACTION_UPSTREAM

    return ObserveRecord(
        meter_state=getattr(arb, "meter_state", None),
        meter_age_sec=getattr(arb, "fresh_meter_age_sec", None),
        grid_state=getattr(arb, "grid_state", None),
        tariff_state=getattr(arb, "tou_state", None),
        decision_action=getattr(arb, "fresh_decision_action", None),
        decision_target_kw=getattr(arb, "fresh_decision_target_kw", None),
        safety_allowed=getattr(arb, "safety_allowed", None),
        safety_reason=getattr(arb, "safety_reason", None),
        authority_state=getattr(arb, "authority_state", None),
        authority_reason=getattr(arb, "authority_reason", None),
        interlock_reason=getattr(arb, "direction_reason", None),
        control_action=getattr(result, "control_action", None),
        would_action=getattr(arb, "would_action", None),
        dispatch_ready=c.dispatch_ready,
        dispatch_enabled=RT.DISPATCH_ENABLED,
        executed=executed,
        outcome=getattr(result, "outcome", None),
        no_action_reason=why,
        detail=getattr(result, "reason", None))


# ======================================================================
# R1：Direction-driven Auto Report Trigger（**只有設計，未實作**）
# ======================================================================
# 問題：Auto Report 目前的自動啟動條件之一是「PCS 原生排程主開關為 ON」，
#       而 Phase 6 自動控制的前提**恰好相反** —— 必須排程 OFF
#       （排程 ON 時 Control Authority 一律判為 EXTERNAL，Phase 6 不介入）。
#       兩者互斥：照現況，Phase 6 在控時報告永遠不會自動開始。
#
# R1 的方向：報告 session 應由**實際控制方向與生命週期**驅動，
#            而不是由 PCS 排程開關驅動。
#
#     Phase 6 決定並確認方向  →  session 開始（action = charge / discharge）
#     方向改變或停止並確認    →  session 結束
#
# 觸發語意（設計，尚未接線）
R1_TRIGGER_ON_VERIFIED_DIRECTION = "ON_VERIFIED_DIRECTION"   # 確認進入充/放電 → 開始
R1_TRIGGER_ON_VERIFIED_IDLE = "ON_VERIFIED_IDLE"             # 確認回到閒置 → 結束
R1_TRIGGER_NOT_WIRED = "R1_NOT_WIRED"                        # 目前狀態

R1_DESIGN_NOTE = (
    "R1 由 Phase 6 的 session lifecycle 驅動報告，取代「排程 ON 才啟動」的條件。"
    "🔴 R1 **不得**反過來影響控制：報告需求不構成送出任何指令的理由，"
    "也不得改變 Control Authority、dispatch 規則或 Layer 1 互鎖。"
    "🔴 OBSERVE_ONLY 期間沒有任何 verified direction（不會送指令），"
    "因此 R1 在本階段**必然不會觸發** —— 這是正確行為，不是缺陷。"
    "⚠️ 既有 Auto Report lifecycle 本輪**一行未改**；R1 屬 D.5-B/6.6 的接線工作。")


def r1_trigger_state(observe_rec=None):
    """
    目前的 R1 觸發狀態。**永遠回 NOT_WIRED** —— 本階段只有設計。

    ⚠️ 刻意做成函式而不是常數，讓日後接線時有明確的落點，
       而不是在別處長出第二套判斷。
    """
    return R1_TRIGGER_NOT_WIRED


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Production 相依注入盤點（唯讀、離線、不連線）")
    ap.add_argument("--show", action="store_true")
    ap.parse_args(argv)

    print("== PCS Auto Control — Production Wiring（D.5-B，未連線）==\n")
    rep = wiring_report(holiday_provider=build_holiday_provider())
    print(f"  {rep}\n")
    for k, v in sorted(rep.sources.items()):
        print(f"    {k:<18} {v}")
    print(f"\n  模式            : {rep.mode}（唯一支援）")
    print(f"  DISPATCH_ENABLED: {rep.dispatch_enabled}")
    print(f"  dispatch_ready  : {rep.dispatch_ready}")
    print(f"  缺少的參數      : {list(rep.missing_required)}")
    rb = rep.readback
    print(f"  ReadBack        : timeout={rb.timeout_sec} poll={rb.poll_interval_sec} "
          f"samples={rb.stability_samples} ready={rb.ready}")
    print(f"  Layer 2         : min_switch_interval_sec="
          f"{CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec}"
          f"（DEFERRED —— 刻意未配置）")
    print(f"  年度離峰日      : {list(build_holiday_provider().known_years)}")
    print(f"\n  can_dispatch    : {rep.can_dispatch}")
    print(f"  R1 Auto Report  : {r1_trigger_state()}（僅設計，未接線）")
    print(f"    {R1_DESIGN_NOTE}")
    print("  ⚠ OBSERVE_ONLY：executor 不建立、operator 不 import —— "
          "沒有任何可被呼叫的控制出口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
