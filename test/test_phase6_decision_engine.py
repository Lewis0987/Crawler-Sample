# -*- coding: utf-8 -*-
"""
Phase 6.3-A Decision Engine Framework 驗證 —— 完全離線
======================================================================
用途
    驗證三維度整合、ESS 資料新鮮度、以及 Fail Closed。
    最重要的斷言：**6.3-A 在任何輸入組合下都不得產生 charge / discharge。**

是否需要設備
    **不需要**。完全離線：不連 HMI / 6160 / PCS / BMS，不開 socket，無檔案 I/O。
    grid / tou 以極簡 stub 注入；ESS 以合成 reading dict 注入；時間全部以參數注入。

涵蓋範圍
    A. EssSnapshot 建構      欄位對應、direction 由外部傳入
    B. Freshness 邊界        age 14.9 / 15.0 / 15.1；age 以 read_started_at 起算
    C. EssSnapshot Fail Closed  NO_DATA / INVALID_READING / COMM_FAILED /
                                MISSING_FIELD / INVALID_TYPE / READ_TOO_SLOW / STALE
    D. DecisionInput 整合
    E. Decision Fail Closed  MISSING_INPUT / INVALID_GRID / INVALID_TOU /
                             ESS_STALE / INVALID_ESS / UNKNOWN_STATE_VOCAB
    F. NO_POLICY_CONFIGURED  三維度全有效仍不得產生控制建議
    G. Action 值域           全矩陣窮舉，不得出現 charge / discharge；不得有 HOLD
    H. Vocabulary drift      與 power_classifier / tou_calendar 交叉比對
    I. 責任邊界              runtime 未 import 其他 Phase 模組；無 249/229；未引用 SOC 保護極限

用法
    python test_phase6_decision_engine.py          # exit 0 = PASS
"""
import os
import sys
import ast
import json
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import decision_engine as DE                            # noqa: E402

# ⚠️ 必須在 import 其他 Phase 模組**之前**快照，否則 sys.modules 檢查會失真
_MODULES_AFTER_DE = set(sys.modules)

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


class Stub:
    """grid / tou 的極簡替身（duck typing）。"""

    def __init__(self, state, valid, reason="OK", detail=""):
        self.state = state
        self.valid = valid
        self.reason = reason
        self.detail = detail


def reading(**over):
    """合成 read_all() 回傳格式（欄位名取自實際 charge_discharge_report.read_all）。"""
    r = {"communication_ok": True, "soc_percent": 55.0, "battery_voltage_v": 780.0,
         "battery_current_a": -3.2, "actual_active_power_kw": -2.5,
         "pcs_fault_flag": False, "pcs_running_flag": True, "pcs_standby_flag": False,
         "pcs_charging_flag": False, "pcs_discharging_flag": True,
         "battery_power_status": "已上電", "pcs_control_mode_code": "manual",
         "alarm_total": 0, "raw_source_time": "2026-08-20 09:00:00", "_fail": []}
    r.update(over)
    return r


def ess(now=101.0, started=100.0, completed=100.5, direction=None, **over):
    return DE.ess_snapshot_from_reading(reading(**over), started, completed,
                                        now=now, direction=direction)


GOOD_ESS = ess()
G_OK = Stub("IMPORT", True)
T_OK = Stub("PEAK", True)
NA = DE.ACTION_NO_ACTION


def main():
    print("== Phase 6.3-A Decision Engine Framework 驗證（完全離線）==\n")
    print(f"ESS 門檻：stale>{DE.ESS_STALE_AFTER_SEC}s、read_duration>{DE.ESS_READ_DURATION_MAX_SEC}s"
          "（暫定，待 Phase 6.7 校正）\n")

    # ---------------- A. EssSnapshot 建構 ----------------
    print("A. EssSnapshot 建構")
    s = GOOD_ESS
    check("正常 reading → valid=True / reason=OK", s.valid and s.reason == DE.E_OK)
    check("SOC 正確搬運（55.0）", s.soc_percent == 55.0)
    check("PCS 三態旗標原樣搬運",
          s.pcs_fault_flag is False and s.pcs_running_flag is True
          and s.pcs_discharging_flag is True)
    check("電池上下電 / 控制模式搬運",
          s.battery_power_status == "已上電" and s.pcs_control_mode_code == "manual")
    check("communication_ok=True、stale=False", s.communication_ok is True and s.stale is False)
    check("read_duration 由 completed-started 算出（0.5s）", abs(s.read_duration_sec - 0.5) < 1e-9)
    s2 = ess(direction="discharge")
    check("direction 由外部傳入（不在模組內重算）", s2.direction == "discharge")
    check("未傳 direction 時為 None（不猜測）", s.direction is None)
    check("as_json_dict 無 Infinity / NaN",
          "Infinity" not in json.dumps(s.as_json_dict(), ensure_ascii=False, allow_nan=False))

    # ---------------- B. Freshness 邊界 ----------------
    print("\nB. Freshness 邊界（age 以 read_started_at 起算）")
    check("age=14.9s → 未過期、valid", (lambda x: x.valid and not x.stale)(ess(now=114.9)))
    check("age=15.0s（等於門檻，不算超過）→ valid", (lambda x: x.valid and not x.stale)(ess(now=115.0)))
    x = ess(now=115.1)
    check("age=15.1s → stale=True、valid=False、reason=STALE",
          x.stale and not x.valid and x.reason == DE.E_STALE)
    x = ess(now=110.0, started=100.0, completed=109.0)
    check("★ age 以 read_started_at 起算（100→110 為 10s，而非 completed 的 1s）",
          abs(x.age_sec - 10.0) < 1e-9)
    check("  同筆 read_duration=9.0s 仍在門檻內 → valid", x.valid and abs(x.read_duration_sec - 9.0) < 1e-9)
    check("age 不為負（now 早於 started 時夾到 0）", ess(now=99.0).age_sec == 0.0)

    # ---------------- C. EssSnapshot Fail Closed ----------------
    print("\nC. EssSnapshot Fail Closed")
    x = DE.ess_no_data()
    check("尚未取得 reading → NO_DATA / valid=False / age=inf",
          x.reason == DE.E_NO_DATA and not x.valid and math.isinf(x.age_sec))
    x = DE.ess_snapshot_from_reading(None, 100.0, 100.5, now=101.0)
    check("reading=None → NO_DATA", x.reason == DE.E_NO_DATA and not x.valid)
    x = DE.ess_snapshot_from_reading(["not", "dict"], 100.0, 100.5, now=101.0)
    check("reading 非 dict → INVALID_READING", x.reason == DE.E_INVALID_READING and not x.valid)
    x = DE.ess_snapshot_from_reading(reading(), "100", 100.5, now=101.0)
    check("read_started_at 非數值 → INVALID_READING", x.reason == DE.E_INVALID_READING)

    x = ess(communication_ok=False)
    check("communication_ok=False → COMM_FAILED / valid=False",
          x.reason == DE.E_COMM_FAILED and not x.valid)
    x = ess(communication_ok=None)
    check("communication_ok=None → COMM_FAILED（不預設為正常）", x.reason == DE.E_COMM_FAILED)

    r = reading(); del r["soc_percent"]
    x = DE.ess_snapshot_from_reading(r, 100.0, 100.5, now=101.0)
    check("缺 soc_percent → MISSING_FIELD",
          x.reason == DE.E_MISSING_FIELD and "soc_percent" in x.detail)
    check("SOC 為字串 → INVALID_TYPE", ess(soc_percent="55").reason == DE.E_INVALID_TYPE)
    check("SOC 為 bool → INVALID_TYPE（bool 不得當成 1%）",
          ess(soc_percent=True).reason == DE.E_INVALID_TYPE)
    check("SOC 為 NaN → INVALID_TYPE", ess(soc_percent=float("nan")).reason == DE.E_INVALID_TYPE)

    x = ess(now=112.5, started=100.0, completed=110.0)
    check("read_duration=10.0s（等於門檻）→ 仍 valid", x.valid)
    x = ess(now=112.5, started=100.0, completed=110.1)
    check("★ read_duration=10.1s → READ_TOO_SLOW / valid=False",
          x.reason == DE.E_READ_TOO_SLOW and not x.valid)
    check("  READ_TOO_SLOW 的 detail 指出 reading 內部時間不一致", "不一致" in x.detail)
    x = ess(now=130.0, started=100.0, completed=111.0)
    check("同時過慢又過期 → 先報 READ_TOO_SLOW（較根本的故障）",
          x.reason == DE.E_READ_TOO_SLOW and x.stale is True)

    check("所有 invalid 快照皆不外洩量測值（soc_percent is None）",
          all(v.soc_percent is None for v in (
              DE.ess_no_data(), ess(communication_ok=False), ess(soc_percent="x"),
              ess(now=200.0), ess(now=112.5, started=100.0, completed=115.0))))

    # ---------------- D. DecisionInput 整合 ----------------
    print("\nD. DecisionInput 整合")
    di = DE.DecisionInput(G_OK, T_OK, GOOD_ESS)
    check("三維度可建構", di.grid is G_OK and di.tou is T_OK and di.ess is GOOD_ESS)
    check("DecisionInput 預設全為 None", DE.DecisionInput().grid is None)

    # ---------------- E. Decision Fail Closed ----------------
    print("\nE. Decision Fail Closed")
    eng = DE.DecisionEngine()
    r0 = eng.decide(None)
    check("dinput=None → no_action / MISSING_INPUT",
          r0.action == NA and r0.reason == DE.R_MISSING_INPUT)
    for miss, di in (("grid", DE.DecisionInput(None, T_OK, GOOD_ESS)),
                     ("tou", DE.DecisionInput(G_OK, None, GOOD_ESS)),
                     ("ess", DE.DecisionInput(G_OK, T_OK, None))):
        r = eng.decide(di)
        check(f"缺少 {miss} → no_action / MISSING_INPUT / unmet 指出 {miss}",
              r.action == NA and r.reason == DE.R_MISSING_INPUT and miss in r.unmet)

    r = eng.decide(DE.DecisionInput(Stub("UNKNOWN", False), T_OK, GOOD_ESS))
    check("Grid UNKNOWN → no_action / INVALID_GRID_STATE",
          r.action == NA and r.reason == DE.R_INVALID_GRID_STATE and "grid" in r.unmet)
    r = eng.decide(DE.DecisionInput(Stub("IMPORT", False), T_OK, GOOD_ESS))
    check("Grid valid=False（狀態看似正常）→ INVALID_GRID_STATE",
          r.action == NA and r.reason == DE.R_INVALID_GRID_STATE)
    r = eng.decide(DE.DecisionInput(G_OK, Stub("UNKNOWN", False), GOOD_ESS))
    check("TOU UNKNOWN → no_action / INVALID_TOU_STATE",
          r.action == NA and r.reason == DE.R_INVALID_TOU_STATE and "tou" in r.unmet)
    r = eng.decide(DE.DecisionInput(G_OK, Stub("OFF_PEAK", False), GOOD_ESS))
    check("TOU valid=False → INVALID_TOU_STATE", r.reason == DE.R_INVALID_TOU_STATE)

    r = eng.decide(DE.DecisionInput(Stub("CHARGE", True), T_OK, GOOD_ESS))
    check("★ Grid 字彙非預期（CHARGE）→ UNKNOWN_STATE_VOCAB（不靜默誤判）",
          r.action == NA and r.reason == DE.R_UNKNOWN_STATE_VOCAB)
    r = eng.decide(DE.DecisionInput(G_OK, Stub("SUMMER", True), GOOD_ESS))
    check("TOU 字彙非預期（SUMMER）→ UNKNOWN_STATE_VOCAB", r.reason == DE.R_UNKNOWN_STATE_VOCAB)
    r = eng.decide(DE.DecisionInput(Stub(None, True), T_OK, GOOD_ESS))
    check("Grid 無 state 欄位 → UNKNOWN_STATE_VOCAB", r.reason == DE.R_UNKNOWN_STATE_VOCAB)

    r = eng.decide(DE.DecisionInput(G_OK, T_OK, ess(now=200.0)))
    check("ESS 過期 → no_action / ESS_STALE（可辨識為過期而非籠統無效）",
          r.action == NA and r.reason == DE.R_ESS_STALE and r.ess_stale is True)
    r = eng.decide(DE.DecisionInput(G_OK, T_OK, ess(communication_ok=False)))
    check("ESS 通訊失敗 → no_action / INVALID_ESS_SNAPSHOT",
          r.action == NA and r.reason == DE.R_INVALID_ESS_SNAPSHOT)
    r = eng.decide(DE.DecisionInput(G_OK, T_OK, DE.ess_no_data()))
    check("ESS 無資料 → no_action（Fail Closed）", r.action == NA and not r.inputs_valid)

    check("所有 Fail Closed 結果 inputs_valid=False",
          not eng.decide(DE.DecisionInput(Stub("UNKNOWN", False), T_OK, GOOD_ESS)).inputs_valid)

    # policy 接縫的防護（6.3-A 不提供任何 policy 實作）
    r = DE.DecisionEngine(policy=lambda d: "explode").decide(DE.DecisionInput(G_OK, T_OK, GOOD_ESS))
    check("policy 回傳非法 action → no_action / INVALID_POLICY_RESULT",
          r.action == NA and r.reason == DE.R_INVALID_POLICY_RESULT)

    def _boom(d):
        raise RuntimeError("policy bug")

    r = DE.DecisionEngine(policy=_boom).decide(DE.DecisionInput(G_OK, T_OK, GOOD_ESS))
    check("policy 拋例外 → no_action / INVALID_POLICY_RESULT",
          r.action == NA and r.reason == DE.R_INVALID_POLICY_RESULT)

    # ---------------- F. NO_POLICY_CONFIGURED ----------------
    print("\nF. NO_POLICY_CONFIGURED（6.3-A 的定義性行為）")
    r = eng.decide(DE.DecisionInput(G_OK, T_OK, GOOD_ESS))
    check("★ 三維度全部有效 → 仍 no_action / NO_POLICY_CONFIGURED",
          r.action == NA and r.reason == DE.R_NO_POLICY_CONFIGURED)
    check("  但 inputs_valid=True（輸入是好的，只是沒策略）", r.inputs_valid is True)
    check("  unmet 為空", r.unmet == ())
    check("  target_power_kw 恆為 None", r.target_power_kw is None)
    check("  policy_id 恆為 None", r.policy_id is None)
    check("  is_suggestion 恆為 True", r.is_suggestion is True)
    check("  診斷欄位可回溯 grid / tou / ess",
          r.grid_state == "IMPORT" and r.tou_state == "PEAK" and r.ess_valid is True)

    # ---------------- G. Action 值域（全矩陣）----------------
    print("\nG. Action 值域（全矩陣窮舉）")
    ess_variants = [
        ("valid", GOOD_ESS),
        ("stale", ess(now=200.0)),
        ("comm_failed", ess(communication_ok=False)),
        ("too_slow", ess(now=112.5, started=100.0, completed=111.0)),
        ("bad_type", ess(soc_percent="x")),
        ("no_data", DE.ess_no_data()),
        ("none", None),
    ]
    grid_variants = [Stub(s, v) for s in sorted(DE.GRID_STATE_VOCAB) for v in (True, False)]
    grid_variants += [Stub("BOGUS", True), None]
    tou_variants = [Stub(s, v) for s in sorted(DE.TOU_STATE_VOCAB) for v in (True, False)]
    tou_variants += [Stub("BOGUS", True), None]

    actions, reasons, n = set(), set(), 0
    for g in grid_variants:
        for t in tou_variants:
            for _, e in ess_variants:
                res = eng.decide(DE.DecisionInput(g, t, e))
                actions.add(res.action)
                reasons.add(res.reason)
                n += 1
    check(f"窮舉 {n} 組合，action 皆在合法值域內：{sorted(actions)}",
          actions <= DE.VALID_ACTIONS)
    check("★ 全矩陣未出現 charge / discharge（6.3-A 不得產生控制建議）",
          not (actions & DE.CONTROL_ACTIONS))
    check("全矩陣 action 僅有 no_action", actions == {NA})
    check(f"出現的 reason 皆為已定義值：{sorted(reasons)}",
          reasons <= {DE.R_NO_POLICY_CONFIGURED, DE.R_MISSING_INPUT, DE.R_INVALID_GRID_STATE,
                      DE.R_INVALID_TOU_STATE, DE.R_INVALID_ESS_SNAPSHOT, DE.R_ESS_STALE,
                      DE.R_UNKNOWN_STATE_VOCAB})
    check("action 值域不含 HOLD 等禁用字彙",
          DE.VALID_ACTIONS.isdisjoint(DE.FORBIDDEN_ACTIONS))
    check("VALID_ACTIONS 恰為 charge/discharge/idle/no_action",
          DE.VALID_ACTIONS == {"charge", "discharge", "idle", "no_action"})

    txt = json.dumps(r.as_json_dict(), ensure_ascii=False, allow_nan=False)
    check("DecisionResult 可嚴格 JSON 序列化且無 Infinity/NaN",
          "Infinity" not in txt and json.loads(txt)["action"] == NA)

    # ---------------- H. Vocabulary drift ----------------
    print("\nH. Vocabulary drift（與上游 Phase 交叉比對）")
    import power_classifier as PC                       # noqa: E402
    import tou_calendar as TC                           # noqa: E402
    check(f"Grid 字彙與 power_classifier.VALID_STATES 一致：{sorted(DE.GRID_STATE_VOCAB)}",
          DE.GRID_STATE_VOCAB == PC.VALID_STATES)
    check(f"TOU 字彙與 tou_calendar.VALID_TOU_STATES 一致：{sorted(DE.TOU_STATE_VOCAB)}",
          DE.TOU_STATE_VOCAB == TC.VALID_TOU_STATES)
    check("Grid 不可用狀態 = {UNKNOWN}", DE.GRID_STATE_UNUSABLE == {PC.STATE_UNKNOWN})
    check("TOU 不可用狀態 = {UNKNOWN}", DE.TOU_STATE_UNUSABLE == {TC.TOU_UNKNOWN})

    # 真實上游物件（非 stub）也要能被正確消費
    real_tou = TC.classify_tou(
        __import__("datetime").datetime(2026, 8, 20, 9, 0), TC.G_CONFIG,
        TC.StaticOffPeakDayProvider({__import__("datetime").date(2026, 1, 1)}, known_years={2026}))
    check("真實 TouState（PEAK/valid）可被消費且不報字彙錯誤",
          eng.decide(DE.DecisionInput(G_OK, real_tou, GOOD_ESS)).reason
          == DE.R_NO_POLICY_CONFIGURED)
    real_tou_unknown = TC.classify_tou(__import__("datetime").datetime(2026, 8, 20, 9, 0))
    check("真實 TouState（UNKNOWN，Holiday 來源不可用）→ INVALID_TOU_STATE",
          eng.decide(DE.DecisionInput(G_OK, real_tou_unknown, GOOD_ESS)).reason
          == DE.R_INVALID_TOU_STATE)

    # ---------------- I. 責任邊界 ----------------
    print("\nI. 責任邊界")
    for m in ("meter_client", "power_classifier", "tou_calendar",
              "charge_discharge_report", "device_control_operator", "report_monitor"):
        check(f"runtime 未載入 {m}", m not in _MODULES_AFTER_DE)

    src = open(DE.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    check(f"原始碼 imports 僅標準庫：{sorted(imported)}",
          imported <= {"sys", "math", "time", "argparse", "dataclasses"})

    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    check(f"程式碼未出現 249 / 229（命中={sorted(consts & {249, 229})}）",
          not (consts & {249, 229}))

    idents = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    idents |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    banned = idents & {"SOC_MAX_PERCENT", "SOC_MIN_PERCENT", "SafetyMonitor",
                       "read_all", "resolve_direction", "ACTIONS", "run"}
    check(f"未引用 Phase 6.4 保護極限或控制元件（命中={sorted(banned)}）", not banned)

    check("EssConfig 拒絕非法門檻",
          all(_raises(lambda: DE.EssConfig(**kw)) for kw in
              ({"stale_after_sec": 0}, {"stale_after_sec": -1},
               {"read_duration_max_sec": float("nan")}, {"stale_after_sec": "15"})))
    check("EssConfig 接受預設組合（stale=15 / read_max=10，不因交叉不等式被拒）",
          DE.EssConfig().stale_after_sec == 15.0 and DE.EssConfig().read_duration_max_sec == 10.0)

    # ---------------- 總結 ----------------
    ok_all = all(RESULTS)
    print(f"\n== Phase 6.3-A Decision Engine 驗證 {'PASS' if ok_all else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def _raises(fn):
    try:
        fn()
        return False
    except (ValueError, TypeError):
        return True


if __name__ == "__main__":
    sys.exit(main())
