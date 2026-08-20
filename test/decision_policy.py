# -*- coding: utf-8 -*-
"""
Phase 6.3-B Decision Policy（第一版：TOU 電價套利 + 禁止逆灌）— 唯讀、零 I/O
======================================================================
把 GridPowerState × TouState × SOC band 對應到充放電**建議**。
注入 Phase 6.3-A 的 DecisionEngine 使用：`DecisionEngine(policy=TouArbitragePolicy())`。

⚠️ 零 I/O：不連 HMI / 6160 / PCS / BMS，不發任何請求，不讀寫檔案。
⚠️ 只產生建議。是否可執行由 Phase 6.4 Safety Gate 判定，實際控制由 Phase 6.5 執行。
⚠️ 不使用 249 / 229；不做削峰；不做閉迴路（P2）；不做時段前瞻（next_transition_at）；
   不使用 rechargeableBatteryCapacity / dischargeableCapacity。

第一版策略
    PEAK       目標放電（僅 Grid=IMPORT 且 SOC 允許）
    HALF_PEAK  一律不動作 —— 其流動電費與 OFF_PEAK 僅差約 0.06~0.08 元/度
               （G 方案：夏月 2.77 vs 2.71、非夏月 2.54 vs 2.46），
               不值得為此增加一次充放電循環。
               ⚠️ HALF_PEAK 仍是**獨立的 TouState**，資料模型上不併入 OFF_PEAK。
    OFF_PEAK   目標充電（SOC 允許時）
    EXPORT     禁止因 ESS 放電造成或擴大逆灌 → PEAK/HALF_PEAK + EXPORT 一律 IDLE；
               OFF_PEAK + EXPORT 仍可充電（充電會吸收逆灌），但只決定**方向**，
               不依 EXPORT kW 計算功率 —— 這不是零逆送閉迴路控制。

SOC 營運門檻（工程策略值，非設備 Safety Limit；Phase 6.7 可依實機調整）
    充電：SOC >= 90 停止；停止後降到 <= 85 才重新允許
    放電：SOC <= 20 停止；停止後升到 >= 25 才重新允許
    ⚠️ SOC 1% / 99% 是 Phase 6.4 Safety Gate 的保護極限，本模組**不引用、不取代**。
    只做 hysteresis，不做 debounce（SOC 單調緩變，無量測噪訊）。

功率策略（P3：固定功率 + 電表狀態保護）
    charge_power_kw / discharge_power_kw **預設未設定**，不自行填值。
    未設定 → no_action / POLICY_POWER_NOT_CONFIGURED
    非正數 / 非有限值 / bool → no_action / POLICY_POWER_INVALID
    ⚠️ 絕不退化成 idle。兩者語意不同：
        idle       Policy 明確判定「目前不需動作」
        no_action  Policy 想動作，但必要參數不足 → 禁止產生控制建議

min_hold_sec = 15（歸 Phase 6.3-B；min_switch_interval 歸 Phase 6.4）
    A. Fail Closed        → no_action，立即生效，清除 hold（由 engine 的 on_fail_closed 通知）
    B. intended == IDLE   → **立即生效，不受 min_hold**（H1）
    C. 無 held            → 直接採用 charge / discharge
    D. intended == held   → 維持，不重置計時
    E. 不同且未滿 15s     → 維持 held，reason = HELD_BY_MIN_HOLD
    F. 不同且已滿 15s     → 採用新的 charge / discharge
    B 的理由：停止永遠是較安全的方向。若 IDLE 也受 min_hold，PEAK 放電中遇到
    Grid 轉 EXPORT 時會延遲停止（加上 Phase 6.2 的 3s debounce 最壞約 18 秒），
    與「禁止擴大逆灌」相衝突。min_hold 的目的是抑制**重新啟動與方向反轉**，不是抑制停止。

用法（完全離線）
    python decision_policy.py --matrix     # 印出 27 格矩陣
    python decision_policy.py --demo       # 情境演示
"""

import sys
import math
import time
import argparse
import threading
from dataclasses import dataclass, field

import decision_engine as DE


# ======================================================================
# 設定區
# ======================================================================
POLICY_ID = "tou_arbitrage_v1"

# ---- 上游狀態（字面值；與 decision_engine 字彙的一致性由測試鎖住）----
TOU_PEAK = "PEAK"
TOU_HALF_PEAK = "HALF_PEAK"
TOU_OFF_PEAK = "OFF_PEAK"
POLICY_TOU_STATES = (TOU_PEAK, TOU_HALF_PEAK, TOU_OFF_PEAK)

GRID_IMPORT = "IMPORT"
GRID_NEAR_ZERO = "NEAR_ZERO"
GRID_EXPORT = "EXPORT"
POLICY_GRID_STATES = (GRID_IMPORT, GRID_NEAR_ZERO, GRID_EXPORT)

# ---- SOC band ----
S_LOW = "S_LOW"      # 只能充（discharge 閂閉）
S_MID = "S_MID"      # 可充可放
S_HIGH = "S_HIGH"    # 只能放（charge 閂閉）
S_NONE = "S_NONE"    # 兩者皆不可 —— 數學上不可達，僅防禦性存在
POLICY_SOC_BANDS = (S_LOW, S_MID, S_HIGH)

# ---- Action（沿用 decision_engine 的值域）----
CHARGE = DE.ACTION_CHARGE
DISCHARGE = DE.ACTION_DISCHARGE
IDLE = DE.ACTION_IDLE
NO_ACTION = DE.ACTION_NO_ACTION

# ---- Policy 層 reason ----
R_OK = "OK"
R_POWER_NOT_CONFIGURED = "POLICY_POWER_NOT_CONFIGURED"
R_POWER_INVALID = "POLICY_POWER_INVALID"
R_HELD_BY_MIN_HOLD = "HELD_BY_MIN_HOLD"
R_MATRIX_MISS = "POLICY_MATRIX_MISS"          # 防禦性：矩陣查無此組合
R_SOC_UNUSABLE = "POLICY_SOC_UNUSABLE"        # 防禦性：SOC 非有限數值


# ======================================================================
# 27 格 Decision Matrix（全部明確列出，**不得有 fallback**）
# ======================================================================
DECISION_MATRIX = {
    # ---- PEAK：目標放電，但只在 IMPORT 才放；NEAR_ZERO / EXPORT 一律停 ----
    (TOU_PEAK, GRID_IMPORT, S_LOW): IDLE,          # 無電可放
    (TOU_PEAK, GRID_IMPORT, S_MID): DISCHARGE,
    (TOU_PEAK, GRID_IMPORT, S_HIGH): DISCHARGE,
    (TOU_PEAK, GRID_NEAR_ZERO, S_LOW): IDLE,       # 再放就會變逆灌
    (TOU_PEAK, GRID_NEAR_ZERO, S_MID): IDLE,
    (TOU_PEAK, GRID_NEAR_ZERO, S_HIGH): IDLE,
    (TOU_PEAK, GRID_EXPORT, S_LOW): IDLE,          # 禁止擴大逆灌
    (TOU_PEAK, GRID_EXPORT, S_MID): IDLE,
    (TOU_PEAK, GRID_EXPORT, S_HIGH): IDLE,

    # ---- HALF_PEAK：整列 IDLE（與 OFF_PEAK 價差僅 0.06~0.08 元/度）----
    (TOU_HALF_PEAK, GRID_IMPORT, S_LOW): IDLE,
    (TOU_HALF_PEAK, GRID_IMPORT, S_MID): IDLE,
    (TOU_HALF_PEAK, GRID_IMPORT, S_HIGH): IDLE,
    (TOU_HALF_PEAK, GRID_NEAR_ZERO, S_LOW): IDLE,
    (TOU_HALF_PEAK, GRID_NEAR_ZERO, S_MID): IDLE,
    (TOU_HALF_PEAK, GRID_NEAR_ZERO, S_HIGH): IDLE,
    (TOU_HALF_PEAK, GRID_EXPORT, S_LOW): IDLE,
    (TOU_HALF_PEAK, GRID_EXPORT, S_MID): IDLE,
    (TOU_HALF_PEAK, GRID_EXPORT, S_HIGH): IDLE,

    # ---- OFF_PEAK：目標充電；S_HIGH 已達充電停止點 → IDLE ----
    (TOU_OFF_PEAK, GRID_IMPORT, S_LOW): CHARGE,
    (TOU_OFF_PEAK, GRID_IMPORT, S_MID): CHARGE,
    (TOU_OFF_PEAK, GRID_IMPORT, S_HIGH): IDLE,
    (TOU_OFF_PEAK, GRID_NEAR_ZERO, S_LOW): CHARGE,
    (TOU_OFF_PEAK, GRID_NEAR_ZERO, S_MID): CHARGE,
    (TOU_OFF_PEAK, GRID_NEAR_ZERO, S_HIGH): IDLE,
    (TOU_OFF_PEAK, GRID_EXPORT, S_LOW): CHARGE,    # 充電吸收逆灌（只決定方向）
    (TOU_OFF_PEAK, GRID_EXPORT, S_MID): CHARGE,
    (TOU_OFF_PEAK, GRID_EXPORT, S_HIGH): IDLE,
}


def validate_matrix(matrix=None):
    """
    檢查矩陣完整性。回傳 (ok, problems)。
    要求：鍵集合恰為 3×3×3；值皆為 charge / discharge / idle（不得出現 no_action）。
    """
    m = DECISION_MATRIX if matrix is None else matrix
    problems = []
    if not isinstance(m, dict):
        return False, ["matrix 不是 mapping"]
    expected = {(t, g, b) for t in POLICY_TOU_STATES
                for g in POLICY_GRID_STATES for b in POLICY_SOC_BANDS}
    missing = expected - set(m)
    extra = set(m) - expected
    for k in sorted(missing):
        problems.append(f"缺少組合 {k}")
    for k in sorted(extra):
        problems.append(f"多餘組合 {k}")
    for k, v in sorted(m.items(), key=lambda x: str(x[0])):
        if v not in (CHARGE, DISCHARGE, IDLE):
            problems.append(f"{k} 的值不合法：{v!r}（矩陣不得直接產生 no_action）")
    return (not problems), problems


# ======================================================================
# Policy 設定
# ======================================================================
def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class PolicyConfig:
    """
    第一版策略設定。SOC 門檻於建構時驗證（結構性錯誤要大聲失敗）；
    功率則刻意**於使用時**才驗證 —— 未設定是預期狀態，必須以 Fail Closed 回報而非拋例外。
    """
    soc_charge_stop_pct: float = 90.0
    soc_charge_resume_pct: float = 85.0
    soc_discharge_stop_pct: float = 20.0
    soc_discharge_resume_pct: float = 25.0
    charge_power_kw: float = None          # 🔴 未設定：待實機決定
    discharge_power_kw: float = None       # 🔴 未設定：待實機決定
    min_hold_sec: float = 15.0

    def __post_init__(self):
        c = self
        seq = [("soc_discharge_stop_pct", c.soc_discharge_stop_pct),
               ("soc_discharge_resume_pct", c.soc_discharge_resume_pct),
               ("soc_charge_resume_pct", c.soc_charge_resume_pct),
               ("soc_charge_stop_pct", c.soc_charge_stop_pct)]
        for name, v in seq:
            if not _is_number(v) or not math.isfinite(v) or not (0 <= v <= 100):
                raise ValueError(f"{name} 需為 0~100 的有限數值：{v!r}")
        for (n1, v1), (n2, v2) in zip(seq, seq[1:]):
            if not v1 < v2:
                raise ValueError(f"SOC 門檻需嚴格遞增：{n1}({v1}) < {n2}({v2}) 不成立")
        if not _is_number(c.min_hold_sec) or not math.isfinite(c.min_hold_sec) or c.min_hold_sec < 0:
            raise ValueError(f"min_hold_sec 需為非負有限數值：{c.min_hold_sec!r}")


DEFAULT_POLICY_CONFIG = PolicyConfig()


# ======================================================================
# SOC 遲滯閂鎖
# ======================================================================
class SocBandLatch:
    """
    兩個獨立閂鎖，共同決定 SOC band。

        charge_allowed :  True --[SOC >= stop ]--> False --[SOC <= resume]--> True
        discharge_allowed: True --[SOC <= stop ]--> False --[SOC >= resume]--> True

    初值只用 **stop** 門檻推得（resume 門檻依定義只在「停止後」才適用），
    否則在 85~90 死區啟動會永久卡住不充電。
    (charge=False, discharge=False) 在數學上不可達
    （前者蘊含 SOC>85、後者蘊含 SOC<25），僅作防禦性回報 S_NONE。
    """

    def __init__(self, config=DEFAULT_POLICY_CONFIG):
        self.cfg = config
        self.reset()

    def reset(self):
        self._charge_allowed = None
        self._discharge_allowed = None

    @property
    def charge_allowed(self):
        return self._charge_allowed

    @property
    def discharge_allowed(self):
        return self._discharge_allowed

    @property
    def band(self):
        c, d = self._charge_allowed, self._discharge_allowed
        if c and d:
            return S_MID
        if c and not d:
            return S_LOW
        if d and not c:
            return S_HIGH
        return S_NONE

    def update(self, soc):
        c = self.cfg
        if self._charge_allowed is None:
            self._charge_allowed = soc < c.soc_charge_stop_pct
        elif self._charge_allowed:
            if soc >= c.soc_charge_stop_pct:
                self._charge_allowed = False
        else:
            if soc <= c.soc_charge_resume_pct:
                self._charge_allowed = True

        if self._discharge_allowed is None:
            self._discharge_allowed = soc > c.soc_discharge_stop_pct
        elif self._discharge_allowed:
            if soc <= c.soc_discharge_stop_pct:
                self._discharge_allowed = False
        else:
            if soc >= c.soc_discharge_resume_pct:
                self._discharge_allowed = True

        return self.band


# ======================================================================
# Policy
# ======================================================================
class TouArbitragePolicy:
    """
    第一版策略：TOU 電價套利 + 禁止逆灌。可直接注入 DecisionEngine(policy=…)。

    有狀態（SOC 閂鎖、min_hold），內部加鎖；clock 可注入以便離線測試。
    """

    policy_id = POLICY_ID

    def __init__(self, config=DEFAULT_POLICY_CONFIG, clock=time.monotonic):
        ok, problems = validate_matrix()
        if not ok:                                   # 矩陣是常數，壞掉就該啟動即失敗
            raise ValueError("Decision Matrix 不合法：" + "；".join(problems[:6]))
        self.cfg = config
        self._clock = clock
        self._lock = threading.Lock()
        self._latch = SocBandLatch(config)
        self._held = None
        self._held_since = None

    # ---- 診斷 ----
    @property
    def held_action(self):
        return self._held

    @property
    def held_since(self):
        return self._held_since

    @property
    def soc_band(self):
        return self._latch.band

    def reset(self):
        with self._lock:
            self._latch.reset()
            self._clear_hold()

    def _clear_hold(self):
        self._held = None
        self._held_since = None

    def on_fail_closed(self):
        """
        engine 在任何 no_action 結果時呼叫。清除 min_hold —— 中斷期間沒有任何建議
        在「維持」，恢復後應能立刻採用當下正確的建議，而不是延續中斷前的計時。

        ⚠️ 刻意**不**重設 SOC 閂鎖：閂鎖反映的是實際發生過的 SOC 跨越，
           資料中斷並不會使那些跨越失效；保留閂鎖也是較少循環的保守選擇。
           （此取捨列為 Phase 6.7 實機檢視項目。）
        """
        with self._lock:
            self._clear_hold()

    # ---- 功率解析 ----
    def _resolve_power(self, action):
        """回傳 (power, error_reason, detail)；error_reason 為 None 表示可用。"""
        if action == CHARGE:
            name, v = "charge_power_kw", self.cfg.charge_power_kw
        else:
            name, v = "discharge_power_kw", self.cfg.discharge_power_kw
        if v is None:
            return None, R_POWER_NOT_CONFIGURED, f"{name} 未設定（第一版不自行填值）"
        if not _is_number(v) or not math.isfinite(v) or v <= 0:
            return None, R_POWER_INVALID, f"{name}={v!r} 非正的有限數值"
        return float(v), None, ""

    # ---- 主流程 ----
    def __call__(self, dinput):
        now = self._clock()
        with self._lock:
            tou_state = getattr(getattr(dinput, "tou", None), "state", None)
            grid_state = getattr(getattr(dinput, "grid", None), "state", None)
            soc = getattr(getattr(dinput, "ess", None), "soc_percent", None)

            # 防禦：engine 已保證 EssSnapshot.valid，此處僅避免例外
            if not _is_number(soc) or not math.isfinite(soc):
                self._clear_hold()
                return DE.PolicyOutcome(NO_ACTION, R_SOC_UNUSABLE, f"soc_percent={soc!r}")

            band = self._latch.update(float(soc))
            intended = DECISION_MATRIX.get((tou_state, grid_state, band))
            if intended is None:
                self._clear_hold()
                return DE.PolicyOutcome(
                    NO_ACTION, R_MATRIX_MISS,
                    f"查無組合 ({tou_state}, {grid_state}, {band})")

            base = f"band={band} soc={soc:g}"

            # ---- B：IDLE 立即生效，不受 min_hold（H1）----
            if intended == IDLE:
                if self._held != IDLE:
                    self._held = IDLE
                    self._held_since = now
                return DE.PolicyOutcome(IDLE, R_OK, base)

            # ---- 功率閘（Fail Closed 優先於 min_hold）----
            power, err, edetail = self._resolve_power(intended)
            if err is not None:
                self._clear_hold()
                return DE.PolicyOutcome(NO_ACTION, err, f"{base} intended={intended} {edetail}")

            # ---- C：尚無 held ----
            if self._held is None:
                self._held = intended
                self._held_since = now
                return DE.PolicyOutcome(intended, R_OK, base, power)

            # ---- D：與 held 相同 → 維持，不重置計時 ----
            if intended == self._held:
                return DE.PolicyOutcome(intended, R_OK, base, power)

            # ---- E / F：不同 → 看 min_hold ----
            elapsed = now - self._held_since
            if elapsed < self.cfg.min_hold_sec:
                remain = self.cfg.min_hold_sec - elapsed
                if self._held == IDLE:
                    return DE.PolicyOutcome(
                        IDLE, R_HELD_BY_MIN_HOLD,
                        f"{base} intended={intended} 尚需 {remain:.1f}s")
                hp, herr, hdetail = self._resolve_power(self._held)
                if herr is not None:                 # 防禦：held 的功率設定已不可用
                    self._clear_hold()
                    return DE.PolicyOutcome(NO_ACTION, herr, f"{base} held={self._held} {hdetail}")
                return DE.PolicyOutcome(
                    self._held, R_HELD_BY_MIN_HOLD,
                    f"{base} intended={intended} 尚需 {remain:.1f}s", hp)

            self._held = intended
            self._held_since = now
            return DE.PolicyOutcome(intended, R_OK, base, power)


# ======================================================================
# CLI（完全離線）
# ======================================================================
class _Stub:
    def __init__(self, state, valid=True):
        self.state = state
        self.valid = valid


def _ess(soc):
    return DE.ess_snapshot_from_reading(
        {"communication_ok": True, "soc_percent": soc}, 100.0, 100.2, now=100.5)


def _print_matrix():
    print("  27 格 Decision Matrix（TouState × GridPowerState × SOC band）\n")
    print("  %-11s %-11s %-8s %-8s %-8s" % ("TOU", "GRID", S_LOW, S_MID, S_HIGH))
    print("  " + "-" * 50)
    for t in POLICY_TOU_STATES:
        for g in POLICY_GRID_STATES:
            row = [DECISION_MATRIX[(t, g, b)] for b in POLICY_SOC_BANDS]
            print("  %-11s %-11s %-8s %-8s %-8s" % (t, g, *row))
    ok, probs = validate_matrix()
    print(f"\n  完整性：{'OK' if ok else probs}")


def main():
    p = argparse.ArgumentParser(description="Phase 6.3-B Decision Policy（唯讀、零 I/O）")
    p.add_argument("--matrix", action="store_true", help="印出 27 格矩陣")
    p.add_argument("--demo", action="store_true", help="情境演示")
    args = p.parse_args()

    print("== Phase 6.3-B Decision Policy（唯讀、零 I/O）==")
    print(f"  policy_id : {POLICY_ID}")
    c = DEFAULT_POLICY_CONFIG
    print(f"  SOC 門檻  : 充電 停{c.soc_charge_stop_pct:g}/復{c.soc_charge_resume_pct:g}、"
          f"放電 停{c.soc_discharge_stop_pct:g}/復{c.soc_discharge_resume_pct:g}（工程策略值）")
    print(f"  功率      : charge={c.charge_power_kw} / discharge={c.discharge_power_kw}"
          "  ⚠ 未設定 → no_action / POLICY_POWER_NOT_CONFIGURED")
    print(f"  min_hold  : {c.min_hold_sec:g}s（IDLE 豁免，立即生效）")
    print("  ⚠ 不做削峰、不做閉迴路、不使用 249/229；結果僅為建議\n")

    if args.matrix:
        _print_matrix()
        return 0
    if not args.demo:
        print("  （--matrix 印矩陣、--demo 情境演示）")
        return 0

    # 演示需要功率才看得到 charge/discharge；此處為**示範值**，非 production 設定
    demo_cfg = PolicyConfig(charge_power_kw=30.0, discharge_power_kw=40.0)
    print("  demo 使用示範功率 charge=30 / discharge=40 kW（僅為演示，非 production 設定）\n")

    t = {"v": 0.0}
    pol = TouArbitragePolicy(demo_cfg, clock=lambda: t["v"])
    eng = DE.DecisionEngine(policy=pol)

    def step(label, tou, grid, soc, dt=1.0):
        t["v"] += dt
        r = eng.decide(DE.DecisionInput(_Stub(grid), _Stub(tou), _ess(soc)))
        pw = "" if r.target_power_kw is None else f" power={r.target_power_kw:g}kW"
        print(f"  t={t['v']:>5.1f}s {label:<28} {r.action:<10} {r.reason:<22}{pw}")

    print("  ── 尖峰放電，Grid 轉 EXPORT 後必須立即停止（H1）──")
    step("PEAK+IMPORT SOC=60", TOU_PEAK, GRID_IMPORT, 60)
    step("PEAK+IMPORT（持續）", TOU_PEAK, GRID_IMPORT, 59, dt=2)
    step("PEAK+EXPORT → 立即 IDLE", TOU_PEAK, GRID_EXPORT, 58, dt=1)
    step("PEAK+IMPORT 想重啟（未滿 15s）", TOU_PEAK, GRID_IMPORT, 58, dt=1)
    step("PEAK+IMPORT 已滿 15s", TOU_PEAK, GRID_IMPORT, 58, dt=15)
    print("\n  ── SOC 遲滯 ──")
    step("PEAK+IMPORT SOC=20 → 放電停止", TOU_PEAK, GRID_IMPORT, 20, dt=20)
    step("PEAK+IMPORT SOC=22（死區，仍停）", TOU_PEAK, GRID_IMPORT, 22)
    step("PEAK+IMPORT SOC=25（解閂）", TOU_PEAK, GRID_IMPORT, 25, dt=20)
    print("\n  ── 離峰充電 / HALF_PEAK ──")
    step("OFF_PEAK+IMPORT SOC=50", TOU_OFF_PEAK, GRID_IMPORT, 50, dt=20)
    step("OFF_PEAK+EXPORT（吸收逆灌）", TOU_OFF_PEAK, GRID_EXPORT, 51)
    step("OFF_PEAK SOC=90 → 充電停止", TOU_OFF_PEAK, GRID_IMPORT, 90)
    step("HALF_PEAK（一律 IDLE）", TOU_HALF_PEAK, GRID_IMPORT, 60, dt=20)
    print("\n  ── 功率未設定（production 預設）──")
    pol2 = TouArbitragePolicy(DEFAULT_POLICY_CONFIG, clock=lambda: t["v"])
    r = DE.DecisionEngine(policy=pol2).decide(
        DE.DecisionInput(_Stub(GRID_IMPORT), _Stub(TOU_PEAK), _ess(60)))
    print(f"  {'PEAK+IMPORT SOC=60':<40} {r.action:<10} {r.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
