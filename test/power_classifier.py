# -*- coding: utf-8 -*-
"""
Phase 6.2 Grid Power Classification（功率狀態判斷）— 唯讀
======================================================================
把 Phase 6.1 的 MeterSnapshot 轉成「穩定的併網點功率狀態」。

本階段只回答一個問題
    「目前併網點處於什麼穩定功率狀態？」
不回答「現在該充電還是放電」—— 那是 Phase 6.3 Decision Engine 的責任。

⚠️ 唯讀：不控制 PCS / BMS、不切換模式、不建立 Session、不呼叫 device_control_operator。
⚠️ 不做 TOU / 尖峰離峰（Phase 6.2b）、不做決策（6.3）、不做 Safety Gate（6.4）。
⚠️ 不使用 249 / 229（業務意義未確認）。

狀態值域（只有這四個）
    IMPORT     台電取電
    EXPORT     逆送／逆灌台電
    NEAR_ZERO  併網點功率接近 0 kW
    UNKNOWN    MeterSnapshot 不可信，或尚無法形成可靠狀態
    值域**不含** CHARGE / DISCHARGE / STOP / HOLD / PEAK / OFF_PEAK。

資料流
    MeterSnapshot
        ↓  Validity Gate（不可繞過 Phase 6.1 的 Fail Closed）
        ↓  Hysteresis Classification  → raw_state
        ↓  Debounce                    → candidate_state
        ↓  Stable GridPowerState

Hysteresis 與 Debounce 是兩件事，刻意分開
    Hysteresis 解決「門檻附近數值抖動」：進入與離開用不同門檻。
    Debounce   解決「狀態短暫成立」：候選狀態需連續維持一段時間才正式切換。

UNKNOWN 優先於狀態穩定性（Fail Closed）
    任何 invalid / stale / 非有限值 → **立即** UNKNOWN，不等 debounce。
    因為資料已不可信時，不能繼續維持一個看似可靠的舊 IMPORT / EXPORT。
    恢復時則走正常 debounce：UNKNOWN → 候選 IMPORT → 持續成立 → IMPORT。

⚠️ 必須「週期性 tick」，不能只在收到資料時才更新
    斷線時不會有新 payload，若只由到達事件驅動，stable 會永遠卡在舊狀態。
    正確用法是固定週期呼叫 update(client.get_snapshot())，讓 age 增長觸發 UNKNOWN。

只使用 power_kw 做方向分類
    demand_kw 僅保留在輸出中供診斷，**不參與** IMPORT / EXPORT / NEAR_ZERO 判定。
    不使用 bess_* / soc_* / wharf，也不讀 PCS / BMS / SOC / Alarm。

用法（皆唯讀）
    python power_classifier.py                 # 連正式來源持續觀察
    python power_classifier.py --duration 60
    python power_classifier.py --json
"""

import sys
import json
import math
import time
import argparse
import threading
from dataclasses import dataclass, asdict
from datetime import datetime

import meter_client as MC
# 直接沿用 Phase 6.1 已驗收、與 6160 網頁 toFixed(2) 等價的顯示格式化。
# 刻意不另寫一份：重複實作會讓兩邊的捨入規則有機會分歧。
from meter_client import _fmt2 as fmt2


# ======================================================================
# 設定區
# ======================================================================
# 狀態值域
STATE_IMPORT = "IMPORT"
STATE_EXPORT = "EXPORT"
STATE_NEAR_ZERO = "NEAR_ZERO"
STATE_UNKNOWN = "UNKNOWN"

VALID_STATES = frozenset({STATE_IMPORT, STATE_EXPORT, STATE_NEAR_ZERO, STATE_UNKNOWN})

# 明確禁止出現在本階段輸出的字彙（由測試斷言）
FORBIDDEN_STATES = frozenset({
    "CHARGE", "DISCHARGE", "STOP", "HOLD", "PEAK", "OFF_PEAK", "HALF_PEAK",
})

# ---- 門檻：全部暫定，待 Phase 6.7 實機校正 ---------------------------
# 依據：Phase 6.0/6.1 實測 274 筆相鄰取樣的 |Δmeter| 為 p50=0.164、p90=1.107、
#       p95=1.608 kW；現場 meter 值域 13.70~97.92 kW，期間**從未**觀測到
#       |meter| < 5 kW 或負值 —— 因此 near-zero 與逆送門檻沒有實測基礎，
#       以下數值僅為「大於量測噪訊」的工程暫定值，不是 production 業務門檻。
# 遲滯寬度（ENTER 與 EXIT 的差）取 3.0 kW，明顯大於 p95 噪訊 1.608 kW，
# 使一般量測抖動不足以造成狀態來回。
IMPORT_ENTER_KW = 5.0      # 由 NEAR_ZERO/UNKNOWN 進入 IMPORT 需 power >= 此值
IMPORT_EXIT_KW = 2.0       # 已在 IMPORT，power <= 此值才離開
EXPORT_ENTER_KW = -5.0     # 進入 EXPORT 需 power <= 此值
EXPORT_EXIT_KW = -2.0      # 已在 EXPORT，power >= 此值才離開

# Debounce 採「時間」而非固定筆數：Socket.IO 目前約 0.5s 一筆，
# 若改用筆數，未來推送頻率變動會連帶改變實際防抖時間。
DEBOUNCE_SEC = 3.0         # 候選狀態需連續維持多久才正式切換（約 6 筆 @0.5s）

# CLI 週期性 tick 間隔（見上方「必須週期性 tick」）
TICK_SEC = 0.5

# 輸出原因
R_OK = "OK"                              # 穩定，且無待決候選
R_DEBOUNCING = "DEBOUNCING"              # 穩定狀態維持中，有候選正在計時
R_NO_DATA = "NO_DATA"                    # 尚未餵入任何 snapshot
R_METER_STALE = "METER_STALE"            # MeterSnapshot 過期
R_METER_INVALID = "METER_INVALID"        # MeterSnapshot 契約/狀態不可信
R_POWER_NOT_FINITE = "POWER_NOT_FINITE"  # power_kw 不是有限數（防禦性）


@dataclass(frozen=True)
class ClassifierConfig:
    """門檻集中定義，避免 magic number 散落在判定程式中；測試可注入自訂值。"""
    import_enter_kw: float = IMPORT_ENTER_KW
    import_exit_kw: float = IMPORT_EXIT_KW
    export_enter_kw: float = EXPORT_ENTER_KW
    export_exit_kw: float = EXPORT_EXIT_KW
    debounce_sec: float = DEBOUNCE_SEC

    def __post_init__(self):
        # 門檻設錯會讓遲滯失效甚至反向，寧可啟動即失敗
        if not self.import_enter_kw > self.import_exit_kw > 0:
            raise ValueError("需滿足 import_enter_kw > import_exit_kw > 0")
        if not self.export_enter_kw < self.export_exit_kw < 0:
            raise ValueError("需滿足 export_enter_kw < export_exit_kw < 0")
        if self.debounce_sec < 0:
            raise ValueError("debounce_sec 不可為負")


DEFAULT_CONFIG = ClassifierConfig()


# ======================================================================
# 資料模型
# ======================================================================
@dataclass(frozen=True)
class GridPowerState:
    """
    併網點穩定功率狀態。

    state             正式（防抖後）狀態，決策端只該看這個
    raw_state         本筆經遲滯後的原始分類（診斷用）
    candidate_state   正在等待 debounce 的候選狀態；無候選時為 None
    valid             state != UNKNOWN
    reason            為什麼是現在這個狀態／為什麼還沒切換
    """
    state: str
    raw_state: str
    candidate_state: str
    power_kw: float
    demand_kw: float                 # 僅診斷，不參與分類
    stable_since: float
    candidate_since: float
    candidate_elapsed_sec: float
    candidate_count: int
    valid: bool
    reason: str
    source_reason: str               # 來自 MeterSnapshot.reason
    detail: str = ""

    def as_dict(self):
        return asdict(self)

    def as_json_dict(self):
        """標準 JSON 不得有 Infinity / NaN（沿用 Phase 6.1 的處理原則）。"""
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        pend = ""
        if self.candidate_state is not None:
            pend = (f"  候選={self.candidate_state}"
                    f"({self.candidate_elapsed_sec:.1f}s/{self.candidate_count}筆)")
        pwr = f"{fmt2(self.power_kw)} kW" if self.power_kw is not None else "n/a"
        return (f"[GRID] {self.state:<9} raw={self.raw_state:<9} "
                f"{MC.UI_LABEL_METER}={pwr}  reason={self.reason}{pend}")


# ======================================================================
# 遲滯分類（純函式）
# ======================================================================
def _classify_fresh(power_kw, cfg):
    """無先前狀態時的分類：必須跨過 ENTER 門檻才算 IMPORT / EXPORT。"""
    if power_kw >= cfg.import_enter_kw:
        return STATE_IMPORT
    if power_kw <= cfg.export_enter_kw:
        return STATE_EXPORT
    return STATE_NEAR_ZERO


def classify_with_hysteresis(power_kw, prev_raw, cfg=DEFAULT_CONFIG):
    """
    遲滯分類：已在某狀態時用較寬鬆的 EXIT 門檻，避免門檻附近來回。

    prev_raw 傳入「上一筆的 raw_state」而非 stable_state —— 遲滯作用在原始訊號上，
    與 debounce 分工（見模組 docstring）。prev_raw 為 None / UNKNOWN 時視為無記憶。
    """
    if not MC._is_number(power_kw) or not math.isfinite(power_kw):
        return STATE_UNKNOWN
    if prev_raw == STATE_IMPORT:
        # 尚未低於 EXIT 就繼續當 IMPORT；一旦低於則重新分類（可能直接掉到 EXPORT）
        return STATE_IMPORT if power_kw > cfg.import_exit_kw else _classify_fresh(power_kw, cfg)
    if prev_raw == STATE_EXPORT:
        return STATE_EXPORT if power_kw < cfg.export_exit_kw else _classify_fresh(power_kw, cfg)
    return _classify_fresh(power_kw, cfg)


def _gate(snapshot):
    """
    Validity Gate。回傳 None 表示通過；否則回傳 UNKNOWN 的原因字串。
    不得繞過 Phase 6.1 的契約與 Fail Closed —— 這裡只是再確認，不重新判定。
    """
    if snapshot is None:
        return R_NO_DATA
    if getattr(snapshot, "reason", None) == MC.R_NO_DATA:
        return R_NO_DATA
    if snapshot.stale:
        return R_METER_STALE
    if not snapshot.valid:
        return R_METER_INVALID
    if not MC._is_number(snapshot.power_kw) or not math.isfinite(snapshot.power_kw):
        return R_POWER_NOT_FINITE
    return None


# ======================================================================
# 分類器
# ======================================================================
class PowerClassifier:
    """
    有狀態的分類器：維護 stable / candidate，並實作 UNKNOWN 立即生效。

    執行緒安全：update() 可能由 Socket.IO 背景執行緒呼叫，內部加鎖。
    """

    def __init__(self, config=DEFAULT_CONFIG):
        self.cfg = config
        self._lock = threading.Lock()
        self._reset_unlocked(initial=True)

    # ---- 內部 ----
    def _reset_unlocked(self, initial=False):
        self._stable = STATE_UNKNOWN
        self._stable_since = None
        self._last_raw = None
        self._cand = None
        self._cand_since = None
        self._cand_count = 0
        self._last = None
        if initial:
            self._last = GridPowerState(
                state=STATE_UNKNOWN, raw_state=STATE_UNKNOWN, candidate_state=None,
                power_kw=None, demand_kw=None, stable_since=None,
                candidate_since=None, candidate_elapsed_sec=0.0, candidate_count=0,
                valid=False, reason=R_NO_DATA, source_reason=R_NO_DATA,
                detail="尚未餵入任何 MeterSnapshot",
            )

    def _clear_candidate(self):
        self._cand = None
        self._cand_since = None
        self._cand_count = 0

    def _emit(self, raw, power, demand, reason, source_reason, now, detail=""):
        elapsed = 0.0 if self._cand_since is None else max(0.0, now - self._cand_since)
        st = GridPowerState(
            state=self._stable, raw_state=raw, candidate_state=self._cand,
            power_kw=power, demand_kw=demand,
            stable_since=self._stable_since, candidate_since=self._cand_since,
            candidate_elapsed_sec=elapsed, candidate_count=self._cand_count,
            valid=(self._stable != STATE_UNKNOWN),
            reason=reason, source_reason=source_reason, detail=detail,
        )
        self._last = st
        return st

    # ---- 對外 ----
    def update(self, snapshot, now=None):
        """
        餵入一筆 MeterSnapshot，回傳當下的 GridPowerState。

        必須週期性呼叫（見模組 docstring）：只在資料到達時呼叫，
        斷線後 stable 會卡在舊狀態而永遠不會轉成 UNKNOWN。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            src_reason = getattr(snapshot, "reason", R_NO_DATA)
            blocked = _gate(snapshot)

            if blocked is not None:
                # Fail Closed 優先於狀態穩定性：立即 UNKNOWN，不經 debounce。
                # 同時清掉遲滯記憶 —— 中斷期間發生什麼未知，恢復後重新以 ENTER 門檻判定。
                changed = self._stable != STATE_UNKNOWN
                self._stable = STATE_UNKNOWN
                if changed or self._stable_since is None:
                    self._stable_since = now
                self._last_raw = None
                self._clear_candidate()
                power = getattr(snapshot, "power_kw", None)
                demand = getattr(snapshot, "demand_kw", None)
                return self._emit(STATE_UNKNOWN, power, demand, blocked, src_reason, now)

            power = float(snapshot.power_kw)
            demand = snapshot.demand_kw
            raw = classify_with_hysteresis(power, self._last_raw, self.cfg)
            self._last_raw = raw

            if raw == self._stable:
                # 候選回到目前 stable → 取消候選
                self._clear_candidate()
                return self._emit(raw, power, demand, R_OK, src_reason, now)

            # raw 與 stable 不同 → 進入 / 延續 / 重啟候選
            if self._cand != raw:
                self._cand = raw
                self._cand_since = now
                self._cand_count = 1
            else:
                self._cand_count += 1

            if (now - self._cand_since) >= self.cfg.debounce_sec:
                self._stable = raw
                self._stable_since = now
                self._clear_candidate()
                return self._emit(raw, power, demand, R_OK, src_reason, now)

            return self._emit(raw, power, demand, R_DEBOUNCING, src_reason, now)

    def current(self):
        with self._lock:
            return self._last

    def reset(self):
        with self._lock:
            self._reset_unlocked(initial=True)


# ======================================================================
# CLI（唯讀觀察）
# ======================================================================
def main():
    p = argparse.ArgumentParser(description="Phase 6.2 Grid Power Classification（唯讀）")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--shadow", action="store_true",
                     help="連本機稽核實例（缺 state 欄位，預期全程 UNKNOWN）")
    src.add_argument("--url", help="自訂 Socket.IO base URL")
    p.add_argument("--duration", type=float, default=0.0, help="觀察秒數（0=持續到 Ctrl+C）")
    p.add_argument("--interval", type=float, default=2.0, help="列印間隔秒數（預設 2.0）")
    p.add_argument("--json", action="store_true", help="每次列印輸出一行 JSON")
    args = p.parse_args()

    if args.url:
        url, source = args.url, MC.SOURCE_PRODUCTION
    elif args.shadow:
        url, source = MC.URL_SHADOW, MC.SOURCE_SHADOW
    else:
        url, source = MC.URL_PRODUCTION, MC.SOURCE_PRODUCTION

    cfg = DEFAULT_CONFIG
    print("== Phase 6.2 Grid Power Classification（唯讀）==")
    print(f"  來源     : {source}  {url}")
    print(f"  遲滯門檻 : IMPORT enter>={cfg.import_enter_kw} / exit<={cfg.import_exit_kw} kW；"
          f"EXPORT enter<={cfg.export_enter_kw} / exit>={cfg.export_exit_kw} kW")
    print(f"  Debounce : {cfg.debounce_sec}s（tick {TICK_SEC}s）")
    print("  狀態值域 : IMPORT / EXPORT / NEAR_ZERO / UNKNOWN（不含 CHARGE / DISCHARGE / PEAK）")
    print("  ⚠ 門檻為暫定值，待 Phase 6.7 實機校正\n")

    client = MC.MeterClient(url=url, source=source)
    try:
        client.start()
    except Exception as e:
        print(f"  [FAIL] 連線失敗：{e!r}")
        return 1

    clf = PowerClassifier(cfg)
    t0 = time.monotonic()
    next_print = 0.0
    seen = {}
    try:
        while True:
            # 週期性 tick：即使沒有新 payload 也要更新，age 增長才會轉 UNKNOWN
            st = clf.update(client.get_snapshot())
            seen[st.state] = seen.get(st.state, 0) + 1
            el = time.monotonic() - t0
            if el >= next_print:
                if args.json:
                    print(json.dumps(st.as_json_dict(), ensure_ascii=False,
                                     allow_nan=False, default=str))
                else:
                    print(f"  {datetime.now().strftime('%H:%M:%S')}  {st}")
                next_print = el + args.interval
            if args.duration and el >= args.duration:
                break
            time.sleep(TICK_SEC)
    except KeyboardInterrupt:
        print("\n  （已中止）")
    finally:
        client.stop()

    print(f"\n  tick 統計：{seen}")
    print(f"  Meter 統計：{client.get_stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
