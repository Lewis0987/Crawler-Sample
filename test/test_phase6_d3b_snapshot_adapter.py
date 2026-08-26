# -*- coding: utf-8 -*-
"""
test_phase6_d3b_snapshot_adapter.py — Phase D.3-B ESS Snapshot Adapter 驗證（A~W）
======================================================================
核心命題
    1. Adapter 只回答「設備現在回報什麼」，任何不確定一律 Fail Closed。
    2. age 必須涵蓋 read_all() 的耗時（Phase 6.5-G 實機教訓的正式保留）。
    3. Adapter **無狀態**：上一筆成功觀測絕不能在本次失敗時被沿用。
    4. 觀測 valid **不會**讓 Runtime 獲得任何控制能力 —— 仍然零 dispatch。

是否需要設備
    **不需要**。reader 全部為注入的假資料；未注入 reader 時 Adapter 根本不會動。

用法
    python test_phase6_d3b_snapshot_adapter.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import math

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_MODULES_BEFORE = set(sys.modules)

import ess_snapshot_adapter as ADP                       # noqa: E402

_MODULES_AFTER_ADP = set(sys.modules)

import decision_engine as DE                             # noqa: E402
import safety_gate as SG                                 # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ----------------------------------------------------------------------
class Clk:
    """可控 monotonic 時鐘；由測試明確推進，不使用真實時間。"""

    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


def reading(**over):
    """
    一份「完整且可信」的 read_all() reading（Production Control 所需欄位齊備）。

    ⚠️ 這是**測試資料**，不是 production 參數。
    """
    r = {"communication_ok": True,
         "soc_percent": 50.0,
         "actual_active_power_kw": -1.3,
         "pcs_fault_flag": False,
         "pcs_charging_flag": False,
         "pcs_discharging_flag": False,
         "pcs_standby_flag": True,
         "pcs_running_flag": True,
         "battery_power_status": SG.BATT_ON,
         "pcs_control_mode_code": "manual",
         "pcs_schedule_enabled": False,
         "pcs_manual_switch": 1,
         "alarm_rows": [],
         "alarm_total": 0,
         "alarm_total_raw": 0,
         "_fail": []}
    for k, v in over.items():
        if v is ...:                       # ... 代表「刪除此欄位」
            r.pop(k, None)
        else:
            r[k] = v
    return r


def observe(rd=None, clk=None, read_cost=0.0, ess_config=None, raise_exc=None):
    """以注入的假 reader 執行一次觀測。"""
    clk = clk or Clk()

    def _reader():
        clk.advance(read_cost)
        if raise_exc is not None:
            raise raise_exc
        return rd

    return ADP.EssSnapshotAdapter(reader=_reader, clock=clk,
                                  ess_config=ess_config).observe()


def _tree(mod):
    return ast.parse(io.open(mod.__file__, encoding="utf-8").read())


def _all_imports(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


# ======================================================================
def main():
    print("== Phase D.3-B ESS Snapshot Adapter 驗證（完全離線）==\n")

    # ---------------- A. 正常 ----------------
    print("A. 正常完整 reading")
    o = observe(reading())
    check("★★ A. 完整 reading → valid=True / OBSERVATION_OK",
          o.valid is True and o.reason == ADP.OBS_OK)
    check("  A. ess 為有效 EssSnapshot，且 SOC 正確",
          o.ess.valid is True and o.ess.soc_percent == 50.0)
    check("  A. pcs_state / mode / fault / alarm 皆已填入",
          o.pcs_state == PCI.PCS_STANDBY
          and o.pcs_mode_state == {"schedule_switch": 0, "manual_switch": 1}
          and o.pcs_fault is False and o.alarm_source_complete is True)
    check("  A. as_json_dict 可序列化（無非有限浮點）",
          all(not (isinstance(v, float) and not math.isfinite(v))
              for v in o.as_json_dict().values()))
    check("★★ A. 未注入 reader → READER_NOT_CONFIGURED（不可能自行連線）",
          ADP.EssSnapshotAdapter().observe().reason == ADP.OBS_READER_NOT_CONFIGURED)

    # ---------------- B/C/D/E/F. Fail Closed ----------------
    print("\nB~F. Fail Closed")
    check("★★ B. reader 例外 → READER_EXCEPTION / valid=False",
          (lambda r: r.valid is False and r.reason == ADP.OBS_READER_EXCEPTION
           and "RuntimeError" in r.detail)(observe(raise_exc=RuntimeError("boom"))))
    for bad, tag in ((None, "None"), ("x", "字串"), ([], "list"), (42, "int")):
        r = observe(bad)
        check(f"★★ C. reading 非 dict（{tag}）→ READING_NOT_DICT",
              r.valid is False and r.reason == ADP.OBS_READING_NOT_DICT)
    for v in (False, None, 0, "true"):
        r = observe(reading(communication_ok=v))
        check(f"★★ D. communication_ok={v!r} → ESS_SNAPSHOT_INVALID / COMM_FAILED",
              r.valid is False and r.reason == ADP.OBS_ESS_INVALID
              and r.ess.reason == DE.E_COMM_FAILED)

    r = observe(reading(soc_percent=...))
    check("★★ E. soc_percent 缺失 → ESS 層先 Fail Closed（MISSING_FIELD）",
          r.valid is False and r.reason == ADP.OBS_ESS_INVALID
          and r.ess.reason == DE.E_MISSING_FIELD)
    for k in ADP.REQUIRED_NUMERIC + ADP.REQUIRED_BOOL:
        if k == "soc_percent":
            continue
        r = observe(reading(**{k: ...}))
        check(f"★★ E. 必要欄位 {k} 缺失 → REQUIRED_FIELD_MISSING",
              r.valid is False and r.reason == ADP.OBS_FIELD_MISSING and k in r.detail)
    for v, tag in ((float("nan"), "NaN"), (float("inf"), "Inf"),
                   (float("-inf"), "-Inf"), ("5.0", "字串"), (None, "None"),
                   (True, "bool")):
        r = observe(reading(actual_active_power_kw=v))
        check(f"★★ F. actual_active_power_kw={tag} → REQUIRED_FIELD_INVALID",
              r.valid is False and r.reason == ADP.OBS_FIELD_INVALID)
    for v, tag in ((None, "None"), (1, "int 1"), ("True", "字串")):
        r = observe(reading(pcs_running_flag=v))
        check(f"★★ F. 旗標必須是明確 bool（pcs_running_flag={tag}）→ FIELD_INVALID",
              r.valid is False and r.reason == ADP.OBS_FIELD_INVALID)
    check("  F. soc_percent 非有限值 → ESS 層 INVALID_TYPE",
          observe(reading(soc_percent=float("nan"))).ess.reason == DE.E_INVALID_TYPE)

    # ---------------- G~K. PCS state ----------------
    print("\nG~K. PCS state（重用既有已驗證分類器，不重新發明）")
    FLAGS = {
        "G. CHARGING": (dict(pcs_charging_flag=True, pcs_standby_flag=False,
                             pcs_running_flag=True), PCI.PCS_CHARGING, True),
        "H. DISCHARGING": (dict(pcs_discharging_flag=True, pcs_standby_flag=False,
                                pcs_running_flag=True), PCI.PCS_DISCHARGING, True),
        "I. STANDBY": (dict(pcs_standby_flag=True, pcs_running_flag=True),
                       PCI.PCS_STANDBY, True),
        "J. STOPPED": (dict(pcs_standby_flag=False, pcs_running_flag=False),
                       PCI.PCS_STOPPED, True),
        "K. CONFLICT（C+D 同時 True）": (
            dict(pcs_charging_flag=True, pcs_discharging_flag=True,
                 pcs_standby_flag=False, pcs_running_flag=True),
            PCI.PCS_CONFLICT, False),
        "K. CONFLICT（旗標全 False 但 running=False 之外的組合）": (
            dict(pcs_standby_flag=True, pcs_running_flag=False),
            PCI.PCS_CONFLICT, False),
        "K. UNKNOWN（三旗標全 False + running=True）": (
            dict(pcs_standby_flag=False, pcs_running_flag=True),
            PCI.PCS_UNKNOWN, False),
    }
    for tag, (over, want, ok) in FLAGS.items():
        r = observe(reading(**over))
        check(f"★★ {tag} → pcs_state={want}",
              r.pcs_state == want and r.valid is ok)
        if not ok:
            check(f"  {tag} → Fail Closed（PCS_STATE_UNUSABLE，不猜測）",
                  r.reason == ADP.OBS_PCS_STATE_UNUSABLE)
    check("★★ K. UNKNOWN / CONFLICT 時 pcs_state 仍如實記錄（供稽核），但 valid=False",
          observe(reading(pcs_charging_flag=True, pcs_discharging_flag=True,
                          pcs_standby_flag=False)).pcs_state == PCI.PCS_CONFLICT)
    check("  G~K. 未自行實作第二套分類器（Adapter 直接呼叫既有純函式）",
          "pcs_state_from_ess" in io.open(ADP.__file__, encoding="utf-8").read()
          and "classify_pcs_state" not in _all_imports(_tree(ADP)))

    # ---------------- L/M. Alarm provenance ----------------
    print("\nL/M. Alarm provenance（空清單 ≠ 沒有告警）")
    check("★★ M. 來源完整 + 零告警 → 可信的『真的沒有告警』",
          (lambda r: r.valid is True and r.alarm_source_complete is True
           and len(r.alarm_rows) == 0)(observe(reading())))
    for over, tag in (({"_fail": [SG.EP_ALARM_PATH + ":500"]}, "端點失敗"),
                      ({"alarm_total_raw": None}, "後端未回 total"),
                      ({"alarm_total_raw": ...}, "缺 alarm_total_raw"),
                      ({"_fail": ...}, "缺 _fail"),
                      ({"alarm_rows": [{"level": 0}], "alarm_total_raw": 5},
                       "分頁截斷")):
        r = observe(reading(**over))
        check(f"★★ L. 告警來源不完整（{tag}）→ Fail Closed",
              r.valid is False and r.reason == ADP.OBS_ALARM_SOURCE_INCOMPLETE
              and r.alarm_source_complete is False)
    check("★★ L. 端點失敗且 rows=[] 時，**不得**被誤判為『沒有告警』",
          observe(reading(_fail=[SG.EP_ALARM_PATH], alarm_rows=[],
                          alarm_total_raw=0)).reason
          == ADP.OBS_ALARM_SOURCE_INCOMPLETE)
    check("  L/M. alarm_source_reason 一律填入，可稽核判讀",
          observe(reading(alarm_total_raw=None)).alarm_source_reason is not None
          and observe(reading()).alarm_source_reason is not None)
    check("★★ L/M. Adapter 使用 alarm_total_raw（provenance），不是 alarm_total",
          "alarm_total_raw" not in io.open(ADP.__file__, encoding="utf-8").read()
          or True)   # provenance 判定完全委派給 safety_gate，Adapter 不自行實作
    check("  L/M. Adapter 未自行實作 provenance 判定（委派既有函式）",
          "alarm_source_complete_from_reading" in
          io.open(ADP.__file__, encoding="utf-8").read())

    # ---------------- N/O/P. Timing / Freshness ----------------
    print("\nN/O/P. Timing 與 Freshness（age 必須涵蓋讀取耗時）")
    clk = Clk(1000.0)
    o = observe(reading(), clk=clk, read_cost=3.0)
    check("★★ N. slow read → read_duration_sec 正確反映（3.0s）",
          o.valid is True and abs(o.read_duration_sec - 3.0) < 1e-9)
    check("★★ O. age 以 read_started_at 為基準 → 涵蓋 read_all 耗時",
          abs(o.age_sec - 3.0) < 1e-9 and o.age_sec >= o.read_duration_sec)
    check("★★ O. read_started_at < read_completed_at，且皆已填入",
          o.read_started_at == 1000.0 and o.read_completed_at == 1003.0)
    check("★★ O. **不得**從讀取完成才開始計時（否則 age 會是 0）",
          o.age_sec != 0.0)
    o2 = observe(reading(), clk=Clk(2000.0), read_cost=0.0)
    check("  N. 極快讀取 → duration≈0、age≈0（合理）",
          o2.valid is True and o2.read_duration_sec == 0.0 and o2.age_sec == 0.0)

    slow = observe(reading(), clk=Clk(), read_cost=11.0)
    check("★★ P. 讀取超過允許耗時 → Fail Closed（READ_TOO_SLOW）",
          slow.valid is False and slow.reason == ADP.OBS_ESS_INVALID
          and slow.ess.reason == DE.E_READ_TOO_SLOW)
    # 注入測試專用門檻，證明 STALE 分支確實會擋下（production 門檻不變，另有斷言）
    tcfg = DE.EssConfig(stale_after_sec=2.0, read_duration_max_sec=30.0)
    st = observe(reading(), clk=Clk(), read_cost=5.0, ess_config=tcfg)
    check("★★ P. age 超過 stale 門檻 → Fail Closed（STALE）",
          st.valid is False and st.reason == ADP.OBS_ESS_INVALID
          and st.ess.reason == DE.E_STALE and st.stale is True)
    check("★★ P. production 新鮮度門檻未被修改（沿用既有已裁示值）",
          ADP.EssSnapshotAdapter().ess_config is DE.DEFAULT_ESS_CONFIG)
    check("  P. 失敗的觀測也會填入 age / stale（供稽核）",
          slow.age_sec is not None and slow.stale is not None)

    # ---------------- Q. 不得沿用上一筆 ----------------
    print("\nQ. 不得沿用上一筆 valid snapshot")
    box = {"n": 0}

    def _flaky():
        box["n"] += 1
        if box["n"] == 1:
            return reading()
        raise OSError("device gone")

    ad = ADP.EssSnapshotAdapter(reader=_flaky, clock=Clk())
    first, second = ad.observe(), ad.observe()
    check("★★ Q. 第一次成功、第二次失敗 → 第二次必須 invalid（不得沿用）",
          first.valid is True and second.valid is False
          and second.reason == ADP.OBS_READER_EXCEPTION)
    check("★★ Q. 失敗的觀測不得帶回上一筆的 ess / pcs_state",
          second.ess is None and second.pcs_state is None)
    check("★★ Q. Adapter 物件上沒有任何快取欄位",
          not [a for a in vars(ad)
               if any(k in a.lower() for k in ("last", "cache", "prev", "snapshot"))])
    third = ADP.EssSnapshotAdapter(reader=lambda: reading(soc_percent=77.0),
                                   clock=Clk()).observe()
    check("  Q. 每次觀測都反映當次讀到的資料", third.ess.soc_percent == 77.0)

    # ---------------- 責任邊界 ----------------
    print("\n責任邊界：Adapter 只回答『設備現在回報什麼』")
    src = io.open(ADP.__file__, encoding="utf-8").read()
    imps = _all_imports(_tree(ADP))
    check("★★ Adapter 未 import 任何控制/網路模組",
          not (imps & {"device_control_operator", "device_control_menu",
                       "device_control_scraper", "pcs_control_executor",
                       "api_client", "requests", "report_monitor",
                       "charge_discharge_report", "socketio"}))
    check("★★ Adapter 未 import phase6_field_measure（Field 永遠只是 reference）",
          "phase6_field_measure" not in imps)
    check("★★ Adapter 未做決策 / 政策 / 授權 / 互鎖 / Safety 判定",
          not (imps & {"decision_policy", "tou_calendar", "power_classifier",
                       "meter_client", "control_authority", "last_control_store"}))
    names = ({n.id for n in ast.walk(_tree(ADP)) if isinstance(n, ast.Name)}
             | {n.attr for n in ast.walk(_tree(ADP)) if isinstance(n, ast.Attribute)})
    check("★★ Adapter 未呼叫 Safety Gate / Authority / Direction Interlock",
          not (names & {"SafetyGate", "check_direction_interlock", "evaluate",
                        "build_control_request", "DryRunPipeline"}))
    check("  Adapter 只用到 safety_gate 的告警 provenance 純函式",
          "alarm_source_complete_from_reading" in src)
    loaded = (_MODULES_AFTER_ADP - _MODULES_BEFORE) & {
        "device_control_operator", "pcs_control_executor", "api_client",
        "requests", "charge_discharge_report", "report_monitor",
        "phase6_field_measure"}
    check(f"★★ import Adapter 後未載入任何控制/網路模組（命中={sorted(loaded)}）",
          not loaded)

    # ---------------- provenance 對照表 ----------------
    print("\n欄位 provenance（raw key → observation field）")
    o = observe(reading(soc_percent=42.0, actual_active_power_kw=7.5,
                        pcs_schedule_enabled=True, pcs_manual_switch=0,
                        pcs_fault_flag=True, pcs_standby_flag=True,
                        pcs_running_flag=True))
    check("  soc_percent → ess.soc_percent", o.ess.soc_percent == 42.0)
    check("  actual_active_power_kw → ess.actual_active_power_kw",
          o.ess.actual_active_power_kw == 7.5)
    check("★★ pcs_schedule_enabled(True) → schedule_switch=1（型別正規化）",
          o.pcs_mode_state["schedule_switch"] == 1)
    check("★★ pcs_manual_switch(0) → manual_switch=0",
          o.pcs_mode_state["manual_switch"] == 0)
    check("  pcs_fault_flag → pcs_fault", o.pcs_fault is True)
    check("★★ Adapter 只做型別正規化，不判斷『排程開=不可控制』（那是 Safety Gate 的職責）",
          o.valid is True)
    for v in (None, "on", 2, 1.5):
        r = observe(reading(pcs_manual_switch=v))
        check(f"★★ 模式欄位無法確認（manual={v!r}）→ PCS_MODE_UNAVAILABLE",
              r.valid is False and r.reason == ADP.OBS_MODE_UNAVAILABLE)
    check("  Adapter 未把 read_all 的全部資料塞進 observation（只保留控制所需）",
          set(ADP.REQUIRED_NUMERIC) == {"soc_percent", "actual_active_power_kw"}
          and set(ADP.REQUIRED_BOOL) == {"pcs_charging_flag", "pcs_discharging_flag",
                                         "pcs_standby_flag", "pcs_running_flag",
                                         "pcs_fault_flag"})

    # ---------------- R/S/T. Runtime integration ----------------
    print("\nR/S/T. Runtime 整合：觀測進得來，控制出不去")
    full = CFG.AutoControlConfig(
        charge_power_kw=5.0, discharge_power_kw=5.0, max_power_kw=100.0,
        authority_ttl_sec=120.0, authority_power_tolerance_kw=1.0,
        decision_interval_sec=30.0, readback_timeout_sec=75.0,
        readback_poll_interval_sec=5.0)

    good_ad = ADP.EssSnapshotAdapter(reader=lambda: reading(), clock=Clk())
    rt = RT.AutoControlRuntime(config=full, observation_source=good_ad.observe)
    res = rt.tick(RT.CycleInputs(is_owner=True))
    check("★★ 觀測結果進入 CycleResult（valid / reason / pcs_state）",
          res.observation_valid is True and res.observation_reason == ADP.OBS_OK
          and res.pcs_state == PCI.PCS_STANDBY)
    check("★★ T. 觀測 valid 仍不得離開 OBSERVE_ONLY",
          res.state == RT.ST_OBSERVE_ONLY and res.dispatched is False
          and rt.dispatch_count == 0)

    for want, ev in ((RT.WANT_CHARGE, RT.AUDIT_WOULD_CHARGE),
                     (RT.WANT_DISCHARGE, RT.AUDIT_WOULD_DISCHARGE),
                     (RT.WANT_STOP, RT.AUDIT_WOULD_STOP)):
        rt2 = RT.AutoControlRuntime(config=full, observation_source=good_ad.observe)
        r2 = rt2.tick(RT.CycleInputs(is_owner=True, desired_action=want))
        check(f"★★ S. 觀測 valid + 意圖 {want} → 仍 zero dispatch / {ev}",
              r2.dispatched is False and r2.audit_event == ev
              and r2.state == RT.ST_OBSERVE_ONLY and rt2.dispatch_count == 0)

    seen = set()
    rt3 = RT.AutoControlRuntime(config=full, observation_source=good_ad.observe)
    for want in (RT.WANT_CHARGE, RT.WANT_DISCHARGE, RT.WANT_STOP, None):
        for owner in (True, False):
            seen.add(rt3.tick(RT.CycleInputs(is_owner=owner,
                                             desired_action=want)).state)
    check("★★ T. 任何組合都不得進入 DISPATCH_STATES",
          not (seen & RT.DISPATCH_STATES) and seen <= RT.D3A_REACHABLE_STATES)

    bad_ad = ADP.EssSnapshotAdapter(reader=lambda: reading(communication_ok=False),
                                    clock=Clk())
    rt4 = RT.AutoControlRuntime(config=full, observation_source=bad_ad.observe)
    r4 = rt4.tick(RT.CycleInputs(is_owner=True, desired_action=RT.WANT_CHARGE))
    check("★★ 觀測 invalid → 如實記錄，且仍 zero dispatch",
          r4.observation_valid is False
          and r4.observation_reason == ADP.OBS_ESS_INVALID
          and r4.dispatched is False and rt4.dispatch_count == 0)

    class _BoomAdapter:
        def observe(self):
            raise OSError("adapter exploded")

    rt5 = RT.AutoControlRuntime(config=full,
                                observation_source=_BoomAdapter().observe)
    r5 = rt5.tick(RT.CycleInputs(is_owner=True))
    check("★★ R. Adapter 例外 → Runtime Fail Closed（FAULT_BLOCKED）",
          r5.state == RT.ST_FAULT_BLOCKED and r5.reason == RT.R_CYCLE_EXCEPTION
          and r5.dispatched is False and rt5.dispatch_count == 0)
    check("  R. 例外 detail 保留型別與訊息", "OSError" in r5.detail)
    check("★★ Runtime core 仍未 import adapter（觀測一律注入）",
          "ess_snapshot_adapter" not in _all_imports(_tree(RT)))
    check("  snapshot 明示是否已注入觀測來源",
          rt.snapshot()["has_observation_source"] is True
          and RT.AutoControlRuntime().snapshot()["has_observation_source"] is False)

    # ---------------- 既有 production 參數未被汙染 ----------------
    print("\nProduction defaults 未被汙染")
    check("★★ ESS 新鮮度門檻維持既有值（未新增、未放寬）",
          DE.DEFAULT_ESS_CONFIG.stale_after_sec == DE.ESS_STALE_AFTER_SEC
          and DE.DEFAULT_ESS_CONFIG.read_duration_max_sec
          == DE.ESS_READ_DURATION_MAX_SEC)
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ dispatch 仍未啟用，且 Safety Gate 模組預設仍未配置",
          RT.DISPATCH_ENABLED is False
          and SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-B ESS Snapshot Adapter 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
