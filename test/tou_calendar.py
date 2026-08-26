# -*- coding: utf-8 -*-
"""
Phase 6.2b TOU Calendar（尖峰／離峰時段判斷）— 唯讀
======================================================================
由「本機日期時間」判斷目前屬於哪一個電價時段，只回答時間維度的問題。

⚠️ 唯讀：不連任何設備、不控制 PCS / BMS、不建立 Session、無任何 I/O。
⚠️ 本模組**不做決策**。CHARGE / DISCHARGE / STOP / HOLD 屬 Phase 6.3 Decision Engine。
⚠️ 不使用 249 / 229（業務意義未確認）。

實作基準（重要）
    依指定採 **[G] 高壓及特高壓－二段式時間電價** 作為工作基準。
    ⚠️ 這是**指定的實作基準，不是「已由現場電費單／契約證實採用 G」**。
    現場契約證據仍待後續確認；確認前 G 僅為工作假設。
    規則出處：台電電價表（114/10/1 起實施）第五章 四、電價 (一)二段式時間電價。
    現場已確認**沒有**與台電個別約定調整 TOU 時段，故直接採 PDF 標準時段，
    不支援場域專屬時段或客製 override。

狀態值域（只有這四個）
    PEAK       尖峰時間（G：僅平日）
    HALF_PEAK  半尖峰時間（G：僅週六）
    OFF_PEAK   離峰時間
    UNKNOWN    無法可靠判定（Fail Closed）
    值域**不含** IMPORT / EXPORT / NEAR_ZERO / CHARGE / DISCHARGE / STOP / HOLD。

    ⚠️ G 雖名為「二段式」，PDF 對週六明定「半尖峰時間」，且有獨立費率與
       獨立的「週六半尖峰契約」科目 —— **HALF_PEAK 不得併入 OFF_PEAK**。
       「二段式」指的是平日只分尖峰／離峰兩段。

責任分離（與 Phase 6.1 / 6.2 完全獨立）
    Phase 6.1  Meter Client     → MeterSnapshot
    Phase 6.2  Power Classifier → GridPowerState（IMPORT / EXPORT / NEAR_ZERO）
    Phase 6.2b TOU Calendar     → TOU State（本模組）
    本模組**不 import** meter_client / power_classifier，
    不使用 power_kw / demand_kw / GridPowerState / SOC / PCS / BMS。
    三者到 Phase 6.3 才整合。

資料流
    Local DateTime
        ↓  Season Rule      → SUMMER / NON_SUMMER
        ↓  Day Type Rule    → WEEKDAY / SATURDAY / SUNDAY / OFF_PEAK_DAY
        ↓  Time Period Rule → [start, end) 分鐘區間表
        ↓
    TOU State + reason

    Holiday 判斷獨立成 HolidayProvider，只回答 is_off_peak_day()，
    不與 TOU Engine 綁死，離線測試可直接注入。

Boundary 語意
    一律 [start, end)：start 含、end 不含。
      08:59:59 → OFF_PEAK ；09:00:00 → PEAK ；23:59:59 → PEAK
    分鐘表示：00:00 = 0、24:00 = **1440**（不可寫成 0，否則區間長度為零）。
    G 標準時段本身無跨午夜區間，但資料結構保留跨午夜能力（season 已支援跨年）。
    註：PDF 未明文定義端點含否；因相鄰區間共用端點（00:00~09:00 與 09:00~24:00），
        [start, end) 是唯一自洽解讀，並與既有 report_monitor 的排程慣例一致。

時區
    沿用專案既有慣例：naive datetime（Windows OS 本機時間）。
    是否明文固定 Asia/Taipei 尚待確認；本模組以 datetime 的「牆鐘欄位」判定。

datetime 必須可注入
    classify_tou(dt, ...) 為純函式，核心判定不呼叫 datetime.now()。

用法（皆唯讀、無網路）
    python tou_calendar.py --at "2026-08-20 09:00"      # 查詢單一時刻
    python tou_calendar.py --day 2026-08-20             # 列出當日時段表
    python tou_calendar.py --day 2026-08-20 --assume-not-holiday
"""

import sys
import argparse
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta


# ======================================================================
# 設定區
# ======================================================================
# TOU 狀態
TOU_PEAK = "PEAK"
TOU_HALF_PEAK = "HALF_PEAK"
TOU_OFF_PEAK = "OFF_PEAK"
TOU_UNKNOWN = "UNKNOWN"

VALID_TOU_STATES = frozenset({TOU_PEAK, TOU_HALF_PEAK, TOU_OFF_PEAK, TOU_UNKNOWN})
PERIOD_STATES = frozenset({TOU_PEAK, TOU_HALF_PEAK, TOU_OFF_PEAK})

# 明確禁止出現在本階段輸出的字彙（由測試斷言）—— 屬其他 Phase 的維度
FORBIDDEN_STATES = frozenset({
    "IMPORT", "EXPORT", "NEAR_ZERO", "CHARGE", "DISCHARGE", "STOP", "HOLD",
})

# 季節
SEASON_SUMMER = "SUMMER"
SEASON_NON_SUMMER = "NON_SUMMER"

# 日型
DAY_WEEKDAY = "WEEKDAY"            # 週一～週五
DAY_SATURDAY = "SATURDAY"          # 週六
DAY_SUNDAY = "SUNDAY"              # 週日
DAY_OFF_PEAK_DAY = "OFF_PEAK_DAY"  # 離峰日（PDF 用語，即國定假日）

# 一日分鐘數；24:00 以 1440 表示
MINUTES_PER_DAY = 1440

# 輸出原因
R_OK = "OK"
R_INVALID_DATETIME = "INVALID_DATETIME"
R_HOLIDAY_SOURCE_UNAVAILABLE = "HOLIDAY_SOURCE_UNAVAILABLE"
R_INVALID_CONFIG = "INVALID_CONFIG"
R_NO_MATCHING_PERIOD = "NO_MATCHING_PERIOD"


def hm(h, m=0):
    """時:分 → 一日之中的分鐘數。hm(24) == 1440。"""
    return h * 60 + m


# ---- [G] 高壓及特高壓－二段式時間電價：標準時段表 --------------------
# 出處：台電電價表（114/10/1）第五章 四、(一)二段式時間電價
# 夏月 5/16~10/15（⚠️ 不是 6/1~9/30，那是表燈與低壓電力的定義）
G_SUMMER_START = (5, 16)
G_SUMMER_END = (10, 15)

G_RULES = {
    (SEASON_SUMMER, DAY_WEEKDAY): (
        (hm(0), hm(9), TOU_OFF_PEAK),
        (hm(9), hm(24), TOU_PEAK),
    ),
    (SEASON_SUMMER, DAY_SATURDAY): (
        (hm(0), hm(9), TOU_OFF_PEAK),
        (hm(9), hm(24), TOU_HALF_PEAK),
    ),
    (SEASON_SUMMER, DAY_SUNDAY): (
        (hm(0), hm(24), TOU_OFF_PEAK),
    ),
    (SEASON_SUMMER, DAY_OFF_PEAK_DAY): (
        (hm(0), hm(24), TOU_OFF_PEAK),
    ),
    (SEASON_NON_SUMMER, DAY_WEEKDAY): (
        (hm(0), hm(6), TOU_OFF_PEAK),
        (hm(6), hm(11), TOU_PEAK),
        (hm(11), hm(14), TOU_OFF_PEAK),
        (hm(14), hm(24), TOU_PEAK),
    ),
    (SEASON_NON_SUMMER, DAY_SATURDAY): (
        (hm(0), hm(6), TOU_OFF_PEAK),
        (hm(6), hm(11), TOU_HALF_PEAK),
        (hm(11), hm(14), TOU_OFF_PEAK),
        (hm(14), hm(24), TOU_HALF_PEAK),
    ),
    (SEASON_NON_SUMMER, DAY_SUNDAY): (
        (hm(0), hm(24), TOU_OFF_PEAK),
    ),
    (SEASON_NON_SUMMER, DAY_OFF_PEAK_DAY): (
        (hm(0), hm(24), TOU_OFF_PEAK),
    ),
}

ALL_DAY_TYPES = (DAY_WEEKDAY, DAY_SATURDAY, DAY_SUNDAY, DAY_OFF_PEAK_DAY)
ALL_SEASONS = (SEASON_SUMMER, SEASON_NON_SUMMER)


# ======================================================================
# 設定驗證（Fail Closed 的第一道）
# ======================================================================
def validate_rules(rules):
    """
    檢查時段表是否可用。回傳 (ok, problems)。
    要求：8 個 (season, day_type) 組合齊備；每組區間由 0 起、至 1440 止、
    連續、無 gap、無 overlap、狀態合法。
    """
    problems = []
    if not isinstance(rules, dict):
        return False, ["rules 不是 mapping"]
    for season in ALL_SEASONS:
        for dt_ in ALL_DAY_TYPES:
            key = (season, dt_)
            periods = rules.get(key)
            if not periods:
                problems.append(f"缺少組合 {key}")
                continue
            prev_end = 0
            for idx, p in enumerate(periods):
                if len(p) != 3:
                    problems.append(f"{key}[{idx}] 欄位數非 3")
                    break
                s, e, st = p
                if not isinstance(s, int) or not isinstance(e, int):
                    problems.append(f"{key}[{idx}] 起訖非整數分鐘")
                    break
                if st not in PERIOD_STATES:
                    problems.append(f"{key}[{idx}] 狀態不合法：{st!r}")
                if s != prev_end:
                    problems.append(
                        f"{key}[{idx}] 不連續：預期起點 {prev_end}，實得 {s}"
                        f"（{'重疊' if s < prev_end else 'gap'}）")
                if e <= s:
                    problems.append(f"{key}[{idx}] 區間非正長度：{s}~{e}")
                    break
                prev_end = e
            else:
                if prev_end != MINUTES_PER_DAY:
                    problems.append(f"{key} 未覆蓋整日：結束於 {prev_end}，應為 {MINUTES_PER_DAY}")
    return (not problems), problems


@dataclass(frozen=True)
class TouConfig:
    """
    TOU 方案設定。門檻與時段集中於此，不散落在判定程式中。

    summer_start / summer_end 為 (月, 日)，兩端皆**包含**。
    若 start > end 視為跨年度夏月（G 用不到，但不把未來堵死）。
    """
    plan_id: str = "G"
    plan_name: str = "高壓及特高壓－二段式時間電價"
    plan_source: str = "台電電價表 114/10/1 第五章 四、(一)"
    summer_start: tuple = G_SUMMER_START
    summer_end: tuple = G_SUMMER_END
    rules: dict = field(default_factory=lambda: dict(G_RULES))

    def __post_init__(self):
        ok, problems = validate_rules(self.rules)
        if not ok:
            raise ValueError("TOU 時段表不合法：" + "；".join(problems[:6]))
        for name, v in (("summer_start", self.summer_start), ("summer_end", self.summer_end)):
            if (not isinstance(v, tuple) or len(v) != 2
                    or not (1 <= v[0] <= 12) or not (1 <= v[1] <= 31)):
                raise ValueError(f"{name} 需為合法的 (月, 日)：{v!r}")


G_CONFIG = TouConfig()


# ======================================================================
# Holiday Provider（與 TOU Engine 解耦）
# ======================================================================
class HolidayProvider:
    """
    離峰日（國定假日）來源介面。

    is_off_peak_day(d) 回傳三態：
        True  → 該日為離峰日
        False → 該日不是離峰日
        None  → **無法判定**（TOU 一律 Fail Closed 成 UNKNOWN）
    三態是刻意的：沒有正式來源時不可假裝「不是假日」。
    """

    def is_off_peak_day(self, d):
        raise NotImplementedError


class UnknownHolidayProvider(HolidayProvider):
    """
    預設 Provider：**目前尚無正式 production 離峰日來源**，一律回 None。

    電價表已列舉離峰日的**節日項目**，但其中春節／端午／中秋為農曆、
    民族掃墓節為 4/4 或 4/5 逐年變動，需依年度日曆表才能落實成國曆日期；
    政府調整放假／補班日亦非本表規範範圍。
    ⚠️ 這裡刻意**不記載任何離峰日數量**：「節日項目數」與「年度日曆日數」
    不相等（春節本身即為跨多日區間），記數量只會變成誤導。
    在正式來源確認前，任何日期都無法排除是離峰日，因此一律回 None，
    使 TOU 輸出 UNKNOWN —— 這是誠實的 Fail Closed，不是功能缺陷。
    """

    def is_off_peak_day(self, d):
        return None


class StaticOffPeakDayProvider(HolidayProvider):
    """
    由外部注入明確日期清單的 Provider（測試用；未來也可承接正式年度清單）。

    known_years 指出「哪些年度的清單是完整的」：
      年度在清單內 → 依 dates 回 True / False（明確）
      年度不在清單內 → 回 None（不謊稱該年沒有假日）
    未指定 known_years 時，以 dates 中出現過的年度為準。
    """

    def __init__(self, dates=(), known_years=None):
        self._dates = frozenset(dates)
        self._years = (frozenset(known_years) if known_years is not None
                       else frozenset(d.year for d in self._dates))

    def is_off_peak_day(self, d):
        if d.year not in self._years:
            return None
        return d in self._dates


class AssumeNotHolidayProvider(HolidayProvider):
    """
    一律回 False。**僅供人工觀察／示範**，不可用於正式控制決策：
    它會把所有國定假日誤判為平日／週六。
    """

    def is_off_peak_day(self, d):
        return False


# ======================================================================
# 資料模型
# ======================================================================
@dataclass(frozen=True)
class TouState:
    """
    TOU 判定結果。欄位設計以「事後能說明為什麼是這個狀態」為準。
    """
    state: str
    season: str
    day_type: str
    plan_id: str
    datetime_local: str
    minute_of_day: int
    period_start_min: int
    period_end_min: int
    is_off_peak_day: bool          # 三態：True / False / None(未知)
    valid: bool                    # state != UNKNOWN
    reason: str
    detail: str = ""

    def as_dict(self):
        return asdict(self)

    def __str__(self):
        if not self.valid:
            extra = f" {self.detail}" if self.detail else ""
            return f"[TOU/{self.plan_id}] {self.state:<9} reason={self.reason}{extra}"
        return (f"[TOU/{self.plan_id}] {self.state:<9} "
                f"{self.season:<10} {self.day_type:<12} "
                f"區間={fmt_min(self.period_start_min)}~{fmt_min(self.period_end_min)}  "
                f"{self.datetime_local}")


def fmt_min(m):
    """分鐘 → HH:MM，1440 顯示為 24:00。"""
    if m is None:
        return "--:--"
    return "%02d:%02d" % (m // 60, m % 60)


# ======================================================================
# 核心判定（純函式，datetime 一律由外部注入）
# ======================================================================
def classify_season(d, config=G_CONFIG):
    """依 (月,日) 判斷夏月／非夏月，兩端皆包含。支援跨年度夏月設定。"""
    md = (d.month, d.day)
    s, e = config.summer_start, config.summer_end
    if s <= e:
        in_summer = s <= md <= e
    else:                                   # 跨年度（G 用不到，保留擴充性）
        in_summer = md >= s or md <= e
    return SEASON_SUMMER if in_summer else SEASON_NON_SUMMER


def classify_day_type(d, is_off_peak_day):
    """
    日型判定。離峰日**優先於**星期 —— 落在週六的離峰日仍是全日離峰。
    is_off_peak_day 為 None 時回 None，交由呼叫端 Fail Closed。
    """
    if is_off_peak_day is None:
        return None
    if is_off_peak_day:
        return DAY_OFF_PEAK_DAY
    wd = d.isoweekday()                     # 1=一 … 6=六, 7=日
    if wd == 7:
        return DAY_SUNDAY
    if wd == 6:
        return DAY_SATURDAY
    return DAY_WEEKDAY


def _find_period(periods, minute):
    """在 [start, end) 區間表中找出 minute 所屬區間。"""
    for s, e, st in periods:
        if s <= minute < e:
            return s, e, st
    return None


def _unknown(dt, plan_id, reason, detail="", season=None, day_type=None,
             minute=None, is_opd=None):
    return TouState(
        state=TOU_UNKNOWN, season=season, day_type=day_type, plan_id=plan_id,
        datetime_local=(dt.isoformat(sep=" ") if isinstance(dt, datetime) else None),
        minute_of_day=minute, period_start_min=None, period_end_min=None,
        is_off_peak_day=is_opd, valid=False, reason=reason, detail=detail,
    )


def classify_tou(dt, config=G_CONFIG, holiday_provider=None):
    """
    由本機日期時間判斷 TOU State。純函式：不呼叫 datetime.now()、無任何 I/O。

    holiday_provider 未指定時使用 UnknownHolidayProvider —— 亦即在正式離峰日
    來源確認前，一律回 UNKNOWN（Fail Closed），不會假裝今天不是假日。
    """
    plan_id = getattr(config, "plan_id", "?")

    # ---- 1. datetime 合法性 ----
    if not isinstance(dt, datetime):
        return _unknown(None, plan_id, R_INVALID_DATETIME,
                        f"需要 datetime，實得 {type(dt).__name__}")

    # ---- 2. 離峰日（Holiday Provider）----
    provider = holiday_provider if holiday_provider is not None else UnknownHolidayProvider()
    try:
        is_opd = provider.is_off_peak_day(dt.date())
    except Exception as e:                  # Provider 壞掉不得讓判定「看起來正常」
        return _unknown(dt, plan_id, R_HOLIDAY_SOURCE_UNAVAILABLE,
                        f"provider 例外：{type(e).__name__}")
    if is_opd is None:
        return _unknown(dt, plan_id, R_HOLIDAY_SOURCE_UNAVAILABLE,
                        "離峰日來源無法判定該日期",
                        season=classify_season(dt.date(), config))
    if not isinstance(is_opd, bool):
        return _unknown(dt, plan_id, R_HOLIDAY_SOURCE_UNAVAILABLE,
                        f"provider 回傳非 bool/None：{type(is_opd).__name__}")

    # ---- 3. Season / Day Type ----
    season = classify_season(dt.date(), config)
    day_type = classify_day_type(dt.date(), is_opd)

    # ---- 4. 時段查表 ----
    minute = dt.hour * 60 + dt.minute        # 邊界皆對齊整分，秒數不影響歸屬
    rules = getattr(config, "rules", None)
    if not isinstance(rules, dict):
        return _unknown(dt, plan_id, R_INVALID_CONFIG, "config.rules 不是 mapping",
                        season=season, day_type=day_type, minute=minute, is_opd=is_opd)
    periods = rules.get((season, day_type))
    if not periods:
        return _unknown(dt, plan_id, R_INVALID_CONFIG,
                        f"缺少組合 ({season}, {day_type})",
                        season=season, day_type=day_type, minute=minute, is_opd=is_opd)
    hit = _find_period(periods, minute)
    if hit is None:
        return _unknown(dt, plan_id, R_NO_MATCHING_PERIOD,
                        f"分鐘 {minute} 不落在任何區間（時段表有 gap）",
                        season=season, day_type=day_type, minute=minute, is_opd=is_opd)
    s, e, st = hit
    if st not in PERIOD_STATES:
        return _unknown(dt, plan_id, R_INVALID_CONFIG, f"區間狀態不合法：{st!r}",
                        season=season, day_type=day_type, minute=minute, is_opd=is_opd)

    return TouState(
        state=st, season=season, day_type=day_type, plan_id=plan_id,
        datetime_local=dt.isoformat(sep=" "), minute_of_day=minute,
        period_start_min=s, period_end_min=e, is_off_peak_day=is_opd,
        valid=True, reason=R_OK, detail="",
    )


def day_schedule(d, config=G_CONFIG, holiday_provider=None):
    """
    回傳某日的完整時段表 [(start_min, end_min, state), ...]，供診斷／CLI 使用。
    無法判定時回 None。
    """
    provider = holiday_provider if holiday_provider is not None else UnknownHolidayProvider()
    try:
        is_opd = provider.is_off_peak_day(d)
    except Exception:
        return None
    if not isinstance(is_opd, bool):
        return None
    season = classify_season(d, config)
    day_type = classify_day_type(d, is_opd)
    periods = getattr(config, "rules", {}).get((season, day_type))
    return list(periods) if periods else None


# ======================================================================
# CLI（唯讀、無網路）
# ======================================================================
def main():
    p = argparse.ArgumentParser(description="Phase 6.2b TOU Calendar（唯讀）")
    p.add_argument("--at", help='查詢單一時刻，格式 "YYYY-MM-DD HH:MM[:SS]"')
    p.add_argument("--day", help="列出某日完整時段表，格式 YYYY-MM-DD")
    p.add_argument("--assume-not-holiday", action="store_true",
                   help="假設該日非離峰日（僅供觀察，非 production）")
    args = p.parse_args()

    cfg = G_CONFIG
    print("== Phase 6.2b TOU Calendar（唯讀）==")
    print(f"  方案     : [{cfg.plan_id}] {cfg.plan_name}")
    print(f"  出處     : {cfg.plan_source}")
    print(f"  夏月     : {cfg.summer_start[0]}/{cfg.summer_start[1]} ~ "
          f"{cfg.summer_end[0]}/{cfg.summer_end[1]}（非 6/1~9/30）")
    print("  狀態值域 : PEAK / HALF_PEAK / OFF_PEAK / UNKNOWN")
    print("  ⚠ 依指定以 G 為實作基準，尚未由現場電費單／契約證實採用 G")
    if args.assume_not_holiday:
        provider = AssumeNotHolidayProvider()
        print("  ⚠ 已啟用 --assume-not-holiday：假設非離峰日，僅供觀察")
    else:
        provider = UnknownHolidayProvider()
        print("  ⚠ 無正式離峰日來源 → 一律 UNKNOWN（Fail Closed）")
    print()

    if args.day:
        try:
            d = datetime.strptime(args.day, "%Y-%m-%d").date()
        except ValueError:
            print(f"  [FAIL] --day 格式錯誤：{args.day}")
            return 1
        sch = day_schedule(d, cfg, provider)
        season = classify_season(d, cfg)
        print(f"  {d}（{'一二三四五六日'[d.isoweekday()-1]}）  season={season}")
        if sch is None:
            print("    無法判定（離峰日來源不可用）→ 全日 UNKNOWN")
        else:
            for s, e, st in sch:
                print(f"    {fmt_min(s)} ~ {fmt_min(e)}  → {st}")
        return 0

    if args.at:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(args.at, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            print(f"  [FAIL] --at 格式錯誤：{args.at}")
            return 1
    else:
        dt = datetime.now()                 # 只有 CLI 取現在時間；核心判定不呼叫
        print(f"  （未指定 --at，使用本機現在時間 {dt.isoformat(sep=' ')}）")

    print("  ", classify_tou(dt, cfg, provider))
    return 0


if __name__ == "__main__":
    sys.exit(main())
