# -*- coding: utf-8 -*-
"""
Phase 6.3-B Decision Policy 驗證 —— 完全離線
======================================================================
用途
    驗證第一版策略（TOU 電價套利 + 禁止逆灌）的 27 格矩陣、SOC 遲滯、
    功率未設定的 Fail Closed，以及 min_hold（含 H1：IDLE 立即生效）。

是否需要設備
    **不需要**。完全離線：不連 HMI / 6160 / PCS / BMS，不開 socket，無檔案 I/O。
    grid / tou 以 stub 注入；ESS 以合成 reading 注入；時鐘以 clock 參數注入。

涵蓋範圍
    A. SOC 遲滯      閂鎖轉移、初始化、死區的歷史相依、S_NONE 不可達
    B. 27 格矩陣     逐格斷言 + 不變量
    C. 功率 Fail Closed  未設定 / 非法，且**不得退化成 idle**
    D. min_hold      H1 立即 IDLE、重啟需 15s、同動作不重置、反向受阻
    E. Fail Closed 貫通  6.3-A 閘門優先、on_fail_closed 清除 hold
    F. 語意分離      idle 與 no_action 從不互換
    G. 邊界與禁令    無 249/229、無削峰符號、無 capacity、無 min_switch_interval

用法
    python test_phase6_decision_policy.py          # exit 0 = PASS
"""
import os
import sys
import ast

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import decision_engine as DE                            # noqa: E402
import decision_policy as DP                            # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


class Stub:
    def __init__(self, state, valid=True):
        self.state = state
        self.valid = valid


def ess(soc, now=100.5, started=100.0, completed=100.2, **over):
    r = {"communication_ok": True, "soc_percent": soc}
    r.update(over)
    return DE.ess_snapshot_from_reading(r, started, completed, now=now)


POWERED = DP.PolicyConfig(charge_power_kw=30.0, discharge_power_kw=40.0)
BAND_SOC = {DP.S_LOW: 15.0, DP.S_MID: 50.0, DP.S_HIGH: 95.0}

CHG, DIS, IDL, NA = DP.CHARGE, DP.DISCHARGE, DP.IDLE, DP.NO_ACTION


class Rig:
    """一次性測試載具：新 policy + 新 engine + 可控時鐘。"""

    def __init__(self, cfg=POWERED, t0=0.0):
        self.t = t0
        self.pol = DP.TouArbitragePolicy(cfg, clock=lambda: self.t)
        self.eng = DE.DecisionEngine(policy=self.pol)

    def at(self, t):
        self.t = t
        return self

    def step(self, tou, grid, soc, dt=None, t=None):
        if t is not None:
            self.t = t
        elif dt is not None:
            self.t += dt
        return self.eng.decide(DE.DecisionInput(Stub(grid), Stub(tou), ess(soc)))


def main():
    print("== Phase 6.3-B Decision Policy 驗證（完全離線）==\n")
    c = DP.DEFAULT_POLICY_CONFIG
    print(f"SOC 門檻：充 停{c.soc_charge_stop_pct:g}/復{c.soc_charge_resume_pct:g}、"
          f"放 停{c.soc_discharge_stop_pct:g}/復{c.soc_discharge_resume_pct:g}；"
          f"min_hold={c.min_hold_sec:g}s\n")

    # ---------------- A. SOC 遲滯 ----------------
    print("A. SOC 遲滯閂鎖")
    L = DP.SocBandLatch(DP.DEFAULT_POLICY_CONFIG)
    check("初始化 SOC=18（<=20）→ S_LOW", L.update(18) == DP.S_LOW)
    check("22（死區，前值為閂閉）→ 仍 S_LOW", L.update(22) == DP.S_LOW)
    check("24（死區）→ 仍 S_LOW", L.update(24) == DP.S_LOW)
    check("25（=resume）→ 解閂 S_MID", L.update(25) == DP.S_MID)
    check("★ 24（同一值，但歷史不同）→ 這次是 S_MID（遲滯生效）", L.update(24) == DP.S_MID)
    check("20（=stop）→ 再度閂閉 S_LOW", L.update(20) == DP.S_LOW)

    L2 = DP.SocBandLatch(DP.DEFAULT_POLICY_CONFIG)
    check("初始化 SOC=50 → S_MID", L2.update(50) == DP.S_MID)
    check("88（死區，charge 仍允許）→ S_MID", L2.update(88) == DP.S_MID)
    check("90（=stop）→ charge 閂閉 S_HIGH", L2.update(90) == DP.S_HIGH)
    check("★ 88（同一值，歷史不同）→ 這次是 S_HIGH", L2.update(88) == DP.S_HIGH)
    check("86（死區）→ 仍 S_HIGH", L2.update(86) == DP.S_HIGH)
    check("85（=resume）→ 解閂 S_MID", L2.update(85) == DP.S_MID)

    for soc, want in ((95, DP.S_HIGH), (90, DP.S_HIGH), (89, DP.S_MID),
                      (50, DP.S_MID), (21, DP.S_MID), (20, DP.S_LOW), (5, DP.S_LOW)):
        check(f"新閂鎖初始化 SOC={soc} → {want}",
              DP.SocBandLatch(DP.DEFAULT_POLICY_CONFIG).update(soc) == want)

    L3 = DP.SocBandLatch(DP.DEFAULT_POLICY_CONFIG)
    seen = set()
    for soc in list(range(0, 101)) + list(range(100, -1, -1)) * 2:
        seen.add(L3.update(soc))
    check(f"0→100→0→100→0 全掃描未出現 S_NONE（不可達組合）：{sorted(seen)}",
          DP.S_NONE not in seen and seen <= set(DP.POLICY_SOC_BANDS))
    check("S_NONE 不在矩陣鍵內", not any(k[2] == DP.S_NONE for k in DP.DECISION_MATRIX))

    # ---------------- B. 27 格矩陣 ----------------
    print("\nB. 27 格 Decision Matrix")
    ok, probs = DP.validate_matrix()
    check(f"矩陣完整性驗證通過（問題數={len(probs)}）", ok)
    check("鍵集合恰為 3×3×3 = 27", len(DP.DECISION_MATRIX) == 27)

    bad = []
    for tou in DP.POLICY_TOU_STATES:
        for grid in DP.POLICY_GRID_STATES:
            for band in DP.POLICY_SOC_BANDS:
                want = DP.DECISION_MATRIX[(tou, grid, band)]
                r = Rig().step(tou, grid, BAND_SOC[band])
                if r.action != want:
                    bad.append((tou, grid, band, want, r.action))
    for b in bad:
        print(f"       {b[0]}/{b[1]}/{b[2]}：期望 {b[3]}，實得 {b[4]}")
    check(f"27 格逐格與矩陣一致（不符={len(bad)}）", not bad)

    vals = DP.DECISION_MATRIX
    check("不變量：PEAK 列不出現 charge",
          not any(v == CHG for k, v in vals.items() if k[0] == DP.TOU_PEAK))
    check("不變量：OFF_PEAK 列不出現 discharge",
          not any(v == DIS for k, v in vals.items() if k[0] == DP.TOU_OFF_PEAK))
    check("不變量：HALF_PEAK 整列皆 idle",
          all(v == IDL for k, v in vals.items() if k[0] == DP.TOU_HALF_PEAK))
    check("不變量：任何 EXPORT 組合皆不 discharge（禁止擴大逆灌）",
          not any(v == DIS for k, v in vals.items() if k[1] == DP.GRID_EXPORT))
    check("不變量：任何 NEAR_ZERO 組合皆不 discharge",
          not any(v == DIS for k, v in vals.items() if k[1] == DP.GRID_NEAR_ZERO))
    check("矩陣值不含 no_action（no_action 只能由 Fail Closed 產生）",
          NA not in set(vals.values()))
    check("統計：discharge 2 格 / charge 6 格 / idle 19 格",
          sum(1 for v in vals.values() if v == DIS) == 2
          and sum(1 for v in vals.values() if v == CHG) == 6
          and sum(1 for v in vals.values() if v == IDL) == 19)

    r = Rig().step(DP.TOU_PEAK, DP.GRID_IMPORT, 50)
    check("discharge 帶 target_power_kw=40", r.action == DIS and r.target_power_kw == 40.0)
    r = Rig().step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50)
    check("charge 帶 target_power_kw=30", r.action == CHG and r.target_power_kw == 30.0)
    r = Rig().step(DP.TOU_HALF_PEAK, DP.GRID_IMPORT, 50)
    check("idle 不帶 target_power_kw", r.action == IDL and r.target_power_kw is None)
    check("結果帶 policy_id", r.policy_id == DP.POLICY_ID)

    ok2, probs2 = DP.validate_matrix({(DP.TOU_PEAK, DP.GRID_IMPORT, DP.S_MID): NA})
    check("validate_matrix 能偵測缺漏與非法值", (not ok2) and len(probs2) > 1)

    # ---------------- C. 功率 Fail Closed ----------------
    print("\nC. 功率未設定 / 非法（不得退化成 idle）")
    r = Rig(DP.DEFAULT_POLICY_CONFIG).step(DP.TOU_PEAK, DP.GRID_IMPORT, 50)
    check("★ 矩陣要 discharge 但功率未設定 → no_action / POLICY_POWER_NOT_CONFIGURED",
          r.action == NA and r.reason == DP.R_POWER_NOT_CONFIGURED)
    check("★ 且**不是** idle（語意不可退化）", r.action != IDL)
    check("  target_power_kw 為 None", r.target_power_kw is None)
    r = Rig(DP.DEFAULT_POLICY_CONFIG).step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50)
    check("矩陣要 charge 但功率未設定 → no_action / POLICY_POWER_NOT_CONFIGURED",
          r.action == NA and r.reason == DP.R_POWER_NOT_CONFIGURED)
    r = Rig(DP.DEFAULT_POLICY_CONFIG).step(DP.TOU_HALF_PEAK, DP.GRID_IMPORT, 50)
    check("矩陣要 idle 時不受功率設定影響 → 仍 idle", r.action == IDL)

    only_chg = DP.PolicyConfig(charge_power_kw=30.0)
    check("★ 方向獨立：只設 charge 時 OFF_PEAK 可 charge",
          Rig(only_chg).step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50).action == CHG)
    r = Rig(only_chg).step(DP.TOU_PEAK, DP.GRID_IMPORT, 50)
    check("★ 方向獨立：同一設定下 PEAK 仍 no_action（discharge 未設定）",
          r.action == NA and r.reason == DP.R_POWER_NOT_CONFIGURED)

    for bad_v, label in ((0, "0"), (-5, "負值"), (float("nan"), "NaN"),
                         (float("inf"), "inf"), (True, "bool")):
        cfg = DP.PolicyConfig(discharge_power_kw=bad_v)
        r = Rig(cfg).step(DP.TOU_PEAK, DP.GRID_IMPORT, 50)
        check(f"discharge_power_kw={label} → no_action / POLICY_POWER_INVALID",
              r.action == NA and r.reason == DP.R_POWER_INVALID)

    # ---------------- D. min_hold（含 H1）----------------
    print("\nD. min_hold = 15s（H1：IDLE 立即生效）")
    g = Rig()
    r = g.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=0.0)
    check("t=0 開始放電", r.action == DIS)
    r = g.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=2.0)
    check("t=2 相同建議 → 維持（不重置計時）", r.action == DIS and r.reason == "OK")

    r = g.step(DP.TOU_PEAK, DP.GRID_NEAR_ZERO, 50, t=3.0)
    check("★ DISCHARGE → IDLE（Grid 轉 NEAR_ZERO）**立即生效**，非 HELD",
          r.action == IDL and r.reason != DP.R_HELD_BY_MIN_HOLD)

    g2 = Rig()
    g2.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=0.0)
    r = g2.step(DP.TOU_PEAK, DP.GRID_EXPORT, 50, t=1.0)
    check("★ DISCHARGE → IDLE（Grid 轉 EXPORT）**立即生效**，不得繼續放電",
          r.action == IDL and r.reason != DP.R_HELD_BY_MIN_HOLD)
    check("  轉 IDLE 後不帶功率", r.target_power_kw is None)

    g3 = Rig()
    g3.step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50, t=0.0)
    r = g3.step(DP.TOU_HALF_PEAK, DP.GRID_IMPORT, 50, t=1.0)
    check("★ CHARGE → IDLE 立即生效", r.action == IDL and r.reason != DP.R_HELD_BY_MIN_HOLD)

    g4 = Rig()
    g4.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=0.0)          # discharge
    g4.step(DP.TOU_PEAK, DP.GRID_EXPORT, 50, t=1.0)          # → idle（held=idle @1.0）
    r = g4.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=15.9)
    check("停止後重啟：未滿 15s → 維持 idle / HELD_BY_MIN_HOLD",
          r.action == IDL and r.reason == DP.R_HELD_BY_MIN_HOLD)
    check("  HELD 診斷含 intended 與剩餘秒數",
          "intended=discharge" in r.detail and "尚需" in r.detail)
    r = g4.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=16.0)
    check("停止後重啟：滿 15s → 恢復 discharge", r.action == DIS and r.reason == "OK")

    g5 = Rig()
    g5.step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50, t=0.0)      # charge
    r = g5.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=5.0)
    check("★ CHARGE → DISCHARGE 反向：未滿 15s → 維持 charge / HELD",
          r.action == CHG and r.reason == DP.R_HELD_BY_MIN_HOLD)
    check("  HELD 期間仍帶 held 的功率（charge=30）", r.target_power_kw == 30.0)
    r = g5.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=15.0)
    check("CHARGE → DISCHARGE 反向：滿 15s → 切換", r.action == DIS)

    g6 = Rig()
    g6.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=0.0)
    g6.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=10.0)         # 同動作
    r = g6.step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50, t=14.9)
    check("同動作不重置計時（t=14.9 仍未滿）", r.reason == DP.R_HELD_BY_MIN_HOLD)
    r = g6.step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50, t=15.0)
    check("t=15.0（=門檻）→ 允許切換", r.action == CHG)

    # ---------------- E. Fail Closed 貫通 ----------------
    print("\nE. Fail Closed 貫通（6.3-A 閘門優先）")
    g7 = Rig()
    g7.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=0.0)
    r = g7.eng.decide(DE.DecisionInput(Stub("UNKNOWN", False), Stub(DP.TOU_PEAK), ess(50)))
    check("Grid UNKNOWN → no_action / INVALID_GRID_STATE（policy 不被呼叫）",
          r.action == NA and r.reason == DE.R_INVALID_GRID_STATE)
    check("★ Fail Closed 已清除 hold", g7.pol.held_action is None)
    r = g7.step(DP.TOU_OFF_PEAK, DP.GRID_IMPORT, 50, t=1.0)
    check("★ 恢復後可立即採用新建議（不受中斷前的 hold 影響）", r.action == CHG)

    g8 = Rig()
    g8.step(DP.TOU_PEAK, DP.GRID_IMPORT, 50, t=0.0)
    r = g8.eng.decide(DE.DecisionInput(Stub(DP.GRID_IMPORT), Stub(DP.TOU_PEAK),
                                       ess(50, now=200.0)))
    check("ESS stale → no_action / ESS_STALE", r.reason == DE.R_ESS_STALE)
    check("  hold 亦被清除", g8.pol.held_action is None)

    r = Rig().eng.decide(DE.DecisionInput(Stub(DP.GRID_IMPORT), Stub(DP.TOU_PEAK),
                                          ess(50, communication_ok=False)))
    check("ESS 通訊失敗 → no_action", r.action == NA)
    r = Rig().eng.decide(DE.DecisionInput(None, Stub(DP.TOU_PEAK), ess(50)))
    check("缺 grid → no_action / MISSING_INPUT", r.reason == DE.R_MISSING_INPUT)
    check("on_fail_closed 可重複呼叫且不拋例外",
          _no_raise(lambda: (Rig().pol.on_fail_closed(), Rig().pol.on_fail_closed())))

    # ---------------- F. 語意分離 ----------------
    print("\nF. 語意分離（idle vs no_action）")
    acts, reasons = set(), set()
    for cfg in (POWERED, DP.DEFAULT_POLICY_CONFIG, only_chg,
                DP.PolicyConfig(discharge_power_kw=40.0)):
        for tou in DP.POLICY_TOU_STATES:
            for grid in DP.POLICY_GRID_STATES:
                for band in DP.POLICY_SOC_BANDS:
                    r = Rig(cfg).step(tou, grid, BAND_SOC[band])
                    acts.add(r.action)
                    reasons.add(r.reason)
                    want = DP.DECISION_MATRIX[(tou, grid, band)]
                    if want == IDL and r.action != IDL:
                        acts.add("BUG_IDLE_DEGRADED")
                    if r.action == IDL and want in (CHG, DIS):
                        acts.add("BUG_CONTROL_DEGRADED_TO_IDLE")
    check(f"4 種功率設定 × 27 格 = 108 組，action 皆合法：{sorted(acts)}",
          acts <= DE.VALID_ACTIONS)
    check("★ 矩陣要 idle 時從不變成其他值；要 charge/discharge 時從不退化成 idle",
          not ({"BUG_IDLE_DEGRADED", "BUG_CONTROL_DEGRADED_TO_IDLE"} & acts))
    check(f"出現的 reason 皆為已定義值：{sorted(reasons)}",
          reasons <= {"OK", DP.R_POWER_NOT_CONFIGURED, DP.R_POWER_INVALID,
                      DP.R_HELD_BY_MIN_HOLD})

    # ---------------- G. 邊界與禁令 ----------------
    print("\nG. 邊界與禁令")
    src = open(DP.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    check(f"imports 僅標準庫 + decision_engine：{sorted(imported)}",
          imported <= {"sys", "math", "time", "argparse", "threading",
                       "dataclasses", "decision_engine"})
    for m in ("meter_client", "power_classifier", "tou_calendar",
              "charge_discharge_report", "device_control_operator", "report_monitor"):
        check(f"未 import {m}", m not in imported)

    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    check(f"未出現 249 / 229（命中={sorted(consts & {249, 229})}）", not (consts & {249, 229}))

    idents = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    idents |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    idents |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    banned = idents & {"peak_target_kw", "contract_capacity_kw", "demand_target_kw",
                       "rechargeableBatteryCapacity", "dischargeableCapacity",
                       "SOC_MAX_PERCENT", "SOC_MIN_PERCENT",
                       "min_switch_interval_sec", "next_transition_at"}
    check(f"未出現削峰 / 容量 / Safety 極限 / 切換間隔 / 前瞻 符號（命中={sorted(banned)}）",
          not banned)

    check("PolicyConfig 拒絕不遞增的 SOC 門檻",
          _raises(lambda: DP.PolicyConfig(soc_charge_resume_pct=95.0)))
    check("PolicyConfig 拒絕越界 SOC", _raises(lambda: DP.PolicyConfig(soc_charge_stop_pct=101)))
    check("PolicyConfig 拒絕負的 min_hold_sec", _raises(lambda: DP.PolicyConfig(min_hold_sec=-1)))
    check("PolicyConfig 預設功率為未設定（不自行填值）",
          DP.DEFAULT_POLICY_CONFIG.charge_power_kw is None
          and DP.DEFAULT_POLICY_CONFIG.discharge_power_kw is None)
    check("預設 SOC 門檻為 90 / 85 / 20 / 25",
          (c.soc_charge_stop_pct, c.soc_charge_resume_pct,
           c.soc_discharge_stop_pct, c.soc_discharge_resume_pct) == (90.0, 85.0, 20.0, 25.0))
    check("預設 min_hold_sec = 15", c.min_hold_sec == 15.0)
    check("policy 字彙與 decision_engine 一致",
          set(DP.POLICY_TOU_STATES) | {"UNKNOWN"} == DE.TOU_STATE_VOCAB
          and set(DP.POLICY_GRID_STATES) | {"UNKNOWN"} == DE.GRID_STATE_VOCAB)

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.3-B Decision Policy 驗證 {'PASS' if ok_all else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


def _raises(fn):
    try:
        fn()
        return False
    except (ValueError, TypeError):
        return True


def _no_raise(fn):
    try:
        fn()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
