# -*- coding: utf-8 -*-
"""
tariff_provider.py — Production Tariff Period Provider（Phase D.4）
======================================================================
只回答一個問題：**「現在屬於哪一種台電時段」**。

    Clock（可注入、必須時區明確）
        + Holiday Source（可注入）
        + Tariff Rules（既有 tou_calendar，不重寫）
              ↓
        TariffObservation

🔴 **不重寫任何電價規則**
    夏月／非夏月、平日／週六／週日／離峰日、時段區間表 —— 全部沿用既有且已驗收的
    tou_calendar（Phase 6.2b）。本模組只負責「把正確的時間交給它，並如實回報」。
    ⚠️ 規則出處：台電電價表（114/10/1 起實施）第五章 四、(一)二段式時間電價，
       [G] 高壓及特高壓－二段式時間電價。**這是指定的工作基準**，
       現場契約證據仍待確認 —— 本模組不改變此定位。

🔴 **Provider 與 Policy 分離**
    本模組是 Provider：只提供「時段事實」。
    要不要因此充電／放電是 Decision Policy 的事，本模組不介入。
    `datetime.now()` 不得散落在決策層 —— 一律由本模組的可注入 clock 提供。

🔴 **時段維度與電網潮流維度完全分離**
    Tariff 維度：PEAK / HALF_PEAK / OFF_PEAK / UNKNOWN
    潮流維度  ：IMPORT / EXPORT / NEAR_ZERO / UNKNOWN（由電表判定，不在此處）
    **逆送不是一種電價時段**，本模組的值域**不含**任何潮流狀態。

🔴 **時區必須明確**
    production 預設 `timezone_name=None` → 一律 UNKNOWN（Fail Closed）。
    **不做固定 UTC+8 加減**，也**不依賴「主機剛好設在台灣」**。
    naive datetime 一律拒絕（明確定義的行為），避免 naive 時間成為 production 契約。

🔴 **UNKNOWN 一律 Fail Closed**
    時區未設定／無法解析、時鐘失效、離峰日來源未設定或失效、規則設定無效 ——
    全部回 UNKNOWN。**UNKNOWN 絕不等同 OFF_PEAK。**

用法（完全離線）
    python tariff_provider.py --demo
"""

import time
import argparse
from datetime import datetime, tzinfo
from dataclasses import dataclass, asdict

import tou_calendar as TC

# ======================================================================
# reason
# ======================================================================
TP_OK = "TARIFF_OBSERVED"
TP_TIMEZONE_NOT_CONFIGURED = "TIMEZONE_NOT_CONFIGURED"
TP_TIMEZONE_INVALID = "TIMEZONE_INVALID"
TP_TIMEZONE_UNAVAILABLE = "TIMEZONE_DATABASE_UNAVAILABLE"
TP_CLOCK_FAILURE = "CLOCK_FAILURE"
TP_CLOCK_INVALID = "CLOCK_RETURNED_INVALID_DATETIME"
TP_NAIVE_DATETIME = "NAIVE_DATETIME_REJECTED"
TP_HOLIDAY_SOURCE_NOT_CONFIGURED = "HOLIDAY_SOURCE_NOT_CONFIGURED"
TP_HOLIDAY_SOURCE_UNAVAILABLE = "HOLIDAY_SOURCE_UNAVAILABLE"
TP_RULES_INVALID = "TARIFF_RULES_INVALID"
TP_UNKNOWN = "TARIFF_UNKNOWN"
# 日型無法確認，但**在該日的所有可能日型下結果都相同** → 答案本身可判定。
TP_OK_DAYTYPE_IRRELEVANT = "TARIFF_OBSERVED_DAYTYPE_IRRELEVANT"

# ======================================================================
# 值域
# ======================================================================
# 🔴 完全沿用既有 TOU 值域，**不新增、不合併**。
#    HALF_PEAK 不得併入 OFF_PEAK —— 電價表對週六明定半尖峰且有獨立費率。
TARIFF_PEAK = TC.TOU_PEAK
TARIFF_HALF_PEAK = TC.TOU_HALF_PEAK
TARIFF_OFF_PEAK = TC.TOU_OFF_PEAK
TARIFF_UNKNOWN = TC.TOU_UNKNOWN
TARIFF_STATES = TC.VALID_TOU_STATES

# 🔴 潮流狀態**不得**出現在本模組的值域中（有專屬斷言）。
FORBIDDEN_IN_TARIFF = frozenset({"IMPORT", "EXPORT", "NEAR_ZERO", "REVERSE_EXPORT",
                                 "CHARGE", "DISCHARGE", "STOP", "HOLD"})

# 🔴 Production 時區契約：**已正式核准為 Asia/Taipei**（Phase D.4-A）。
#    一律使用 IANA 名稱交由時區資料庫解析，**不做任何固定時差運算**。
#    時區資料庫不可用 / 名稱無法解析 → Fail Closed（UNKNOWN），不退回本機時間。
PRODUCTION_TIMEZONE_NAME = "Asia/Taipei"
RECOMMENDED_TIMEZONE_NAME = PRODUCTION_TIMEZONE_NAME

# 🔴 保守判定（Phase D.4-B 核准啟用）
#    只有在「是離峰日」與「不是離峰日」兩種互斥假設下**得到完全相同的時段**時，
#    才允許在離峰日來源未知的情況下回答那個唯一結果。
#    ⚠️ 這不是 best guess / default / fallback / heuristic ——
#       它只解決「未知輸入不影響最終答案」的情境；只要兩種假設結果不同，一律 UNKNOWN。
#    ⚠️ **禁止**以 assume-not-holiday 作為 production fallback。
PRODUCTION_RESOLVE_DAYTYPE_IRRELEVANT = True


def resolve_timezone(name):
    """
    解析 IANA 時區名稱。回傳 (tzinfo, reason)；失敗時 tzinfo 為 None。

    🔴 不做任何固定時差運算 —— 一律交給時區資料庫。
    """
    if name is None:
        return None, TP_TIMEZONE_NOT_CONFIGURED
    if not isinstance(name, str) or not name.strip():
        return None, TP_TIMEZONE_INVALID
    try:
        from zoneinfo import ZoneInfo                      # noqa: PLC0415
    except ImportError:
        return None, TP_TIMEZONE_UNAVAILABLE
    try:
        return ZoneInfo(name), TP_OK
    except Exception:                                      # noqa: BLE001
        return None, TP_TIMEZONE_INVALID


@dataclass(frozen=True)
class TariffObservation:
    """
    一次時段觀測。

    🔴 `state` 只可能是 PEAK / HALF_PEAK / OFF_PEAK / UNKNOWN。
       **UNKNOWN 絕不等同 OFF_PEAK** —— 消費端必須分別處理。
    """
    valid: bool
    state: str
    reason: str
    plan_id: str = None
    season: str = None
    day_type: str = None
    is_off_peak_day: object = None          # True / False / None（三態）
    timezone_name: str = None
    utc_offset: str = None
    local_datetime: str = None
    observed_at_monotonic: float = None
    detail: str = ""

    def as_dict(self):
        return asdict(self)

    def __str__(self):
        if not self.valid:
            extra = f" {self.detail}" if self.detail else ""
            return f"[TARIFF] {self.state} reason={self.reason}{extra}"
        return (f"[TARIFF] {self.state:<10} {self.local_datetime} "
                f"({self.timezone_name} {self.utc_offset}) "
                f"season={self.season} day={self.day_type}")


class TariffProvider:
    """
    時段提供者。**無狀態**：每次 observe() 都重新取得時間並重新判定。

    timezone_name
        IANA 名稱。production 預設 None → 一律 UNKNOWN（Fail Closed）。
    clock
        可注入。未注入時使用「該時區的當下時間」（aware datetime）。
        🔴 離線測試一律注入固定時鐘，不得依賴真實時間。
    holiday_provider
        離峰日來源。**未注入即 UNKNOWN** —— 不假裝今天不是假日。
    config
        時段規則設定（預設沿用既有 G 基準）。
    """

    def __init__(self, timezone_name=PRODUCTION_TIMEZONE_NAME, clock=None,
                 holiday_provider=None, config=None, monotonic=None,
                 resolve_when_daytype_irrelevant=PRODUCTION_RESOLVE_DAYTYPE_IRRELEVANT):
        self.timezone_name = timezone_name
        # 🔴 保守判定：只在「所有可能日型都得到同一個時段」時才回答 ——
        #    那不是放寬，而是該日的答案本來就與離峰日無關（實務上只有週日符合）。
        #    週六與平日的兩種假設結果不同，因此一律維持 UNKNOWN。
        self.resolve_when_daytype_irrelevant = bool(resolve_when_daytype_irrelevant)
        self._clock = clock
        self.holiday_provider = holiday_provider
        self.config = config if config is not None else TC.G_CONFIG
        self._monotonic = monotonic if monotonic is not None else time.monotonic

    # ------------------------------------------------------------------
    def _fail(self, reason, detail, **kw):
        # kw 可能已帶 timezone_name / utc_offset / local_datetime（來自 base），
        # 因此以 setdefault 合併，避免重複關鍵字。
        kw.setdefault("timezone_name", self.timezone_name)
        return TariffObservation(
            valid=False, state=TARIFF_UNKNOWN, reason=reason,
            plan_id=getattr(self.config, "plan_id", None),
            observed_at_monotonic=self._safe_monotonic(), detail=detail, **kw)

    def _safe_monotonic(self):
        try:
            return self._monotonic()
        except Exception:                                  # noqa: BLE001
            return None

    def observe(self, now=None):
        """
        取得目前時段。**永遠不會拋出例外**。

        判定順序（任一失敗即 UNKNOWN，Fail Closed）
            1. 時區未設定 / 無法解析 / 時區資料庫不存在
            2. 時鐘失效 / 回傳非 datetime
            3. naive datetime（明確拒絕，不猜測它屬於哪個時區）
            4. 離峰日來源未設定
            5. 交給既有時段規則判定（其自身已處理來源例外與設定錯誤）
        """
        tz, why = resolve_timezone(self.timezone_name)
        if tz is None:
            return self._fail(why, f"timezone_name={self.timezone_name!r} "
                                   f"→ 無法建立時區感知的時間")

        if now is None:
            try:
                now = self._clock() if self._clock is not None else datetime.now(tz)
            except Exception as e:                         # noqa: BLE001
                return self._fail(TP_CLOCK_FAILURE, f"{type(e).__name__}: {e}")

        if not isinstance(now, datetime):
            return self._fail(TP_CLOCK_INVALID,
                              f"時鐘回傳 {type(now).__name__}，不是 datetime")
        # 🔴 naive datetime 的行為是**明確拒絕**：不猜測它屬於哪個時區，
        #    也不以主機本機時間充當契約。
        if now.tzinfo is None or now.utcoffset() is None:
            return self._fail(TP_NAIVE_DATETIME,
                              "收到不含時區資訊的時間 → 拒絕（不得成為 production 契約）")
        local = now.astimezone(tz)

        base = dict(timezone_name=self.timezone_name,
                    utc_offset=str(local.utcoffset()),
                    local_datetime=local.isoformat())
        if self.holiday_provider is None:
            return self._fail(TP_HOLIDAY_SOURCE_NOT_CONFIGURED,
                              "尚未設定正式離峰日來源 → 無法判定日型",
                              season=TC.classify_season(local.date(), self.config),
                              **base)

        # 交給既有且已驗收的規則引擎（其內部已處理 provider 例外／設定錯誤）
        tou = TC.classify_tou(local, config=self.config,
                              holiday_provider=self.holiday_provider)
        if (tou.reason == TC.R_HOLIDAY_SOURCE_UNAVAILABLE
                and self.resolve_when_daytype_irrelevant):
            settled = self._daytype_irrelevant_state(local)
            if settled is not None:
                return TariffObservation(
                    valid=True, state=settled, reason=TP_OK_DAYTYPE_IRRELEVANT,
                    plan_id=getattr(self.config, "plan_id", None),
                    season=TC.classify_season(local.date(), self.config),
                    day_type=None, is_off_peak_day=None,
                    observed_at_monotonic=self._safe_monotonic(),
                    detail="離峰日無法確認，但該日在所有可能日型下時段相同",
                    **base)
        reason = {TC.R_OK: TP_OK,
                  TC.R_HOLIDAY_SOURCE_UNAVAILABLE: TP_HOLIDAY_SOURCE_UNAVAILABLE,
                  TC.R_INVALID_CONFIG: TP_RULES_INVALID,
                  TC.R_INVALID_DATETIME: TP_CLOCK_INVALID,
                  TC.R_NO_MATCHING_PERIOD: TP_RULES_INVALID}.get(tou.reason,
                                                                 TP_UNKNOWN)
        return TariffObservation(
            valid=bool(tou.valid), state=tou.state, reason=reason,
            plan_id=tou.plan_id, season=tou.season, day_type=tou.day_type,
            is_off_peak_day=tou.is_off_peak_day,
            observed_at_monotonic=self._safe_monotonic(),
            detail=tou.detail, **base)


# ======================================================================
# CLI（完全離線）
# ======================================================================
    def _daytype_irrelevant_state(self, local):
        """
        以兩個互斥假設試算：若「是離峰日」與「不是離峰日」得到**同一個**時段，
        則該日的答案與離峰日無關，可安全回答；否則回 None（維持 UNKNOWN）。

        🔴 這不是猜測，也不是放寬 —— 兩種假設都成立時，答案唯一。
        🔴 實務上只有週日符合（週六與平日兩種假設會得到不同時段）。
        """
        a = TC.classify_tou(local, config=self.config,
                            holiday_provider=_AssumeOffPeakDay())
        b = TC.classify_tou(local, config=self.config,
                            holiday_provider=TC.AssumeNotHolidayProvider())
        if a.valid and b.valid and a.state == b.state:
            return a.state
        return None


class _AssumeOffPeakDay(TC.HolidayProvider):
    """僅供上述「兩個假設」試算，**不對外**、不可作為正式來源。"""

    def is_off_peak_day(self, d):
        return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Production 時段提供者（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== Production Tariff Provider（D.4，完全離線）==\n")
    print(f"  production 時區設定 : {PRODUCTION_TIMEZONE_NAME}（未核准 → Fail Closed）")
    print(f"  建議值（待核准）    : {RECOMMENDED_TIMEZONE_NAME}")
    print(f"  時段值域            : {sorted(TARIFF_STATES)}")
    print(f"  規則基準            : {getattr(TC.G_CONFIG, 'plan_id', '?')}\n")

    print(f"  production 預設     : {TariffProvider().observe()}")
    p = TariffProvider(timezone_name=RECOMMENDED_TIMEZONE_NAME)
    print(f"  僅設時區、無離峰日源: {p.observe()}")

    hp = TC.StaticOffPeakDayProvider(dates=[], known_years=[2026])
    from zoneinfo import ZoneInfo                          # noqa: PLC0415
    tz = ZoneInfo(RECOMMENDED_TIMEZONE_NAME)
    p2 = TariffProvider(timezone_name=RECOMMENDED_TIMEZONE_NAME, holiday_provider=hp)
    for label, dt in (("夏月平日 14:00", datetime(2026, 7, 15, 14, 0, tzinfo=tz)),
                      ("夏月平日 03:00", datetime(2026, 7, 15, 3, 0, tzinfo=tz)),
                      ("夏月週六 14:00", datetime(2026, 7, 18, 14, 0, tzinfo=tz)),
                      ("非夏月平日 12:00", datetime(2026, 12, 10, 12, 0, tzinfo=tz))):
        print(f"  {label:<18} {p2.observe(dt)}")

    print("\n  ⚠ 電價規則完全沿用既有時段模組，本檔不重寫任何規則。")
    print("  ⚠ UNKNOWN 絕不等同 OFF_PEAK；逆送不是時段狀態。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
