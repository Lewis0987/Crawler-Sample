# -*- coding: utf-8 -*-
"""
Phase 6.2b TOU Calendar 驗證 —— 完全離線
======================================================================
用途
    驗證 [G] 高壓及特高壓－二段式時間電價 的季節、日型、時段邊界與 Fail Closed。
    重點在**時間邊界**：08:59:59 與 09:00:00 必須落在不同時段，且規則明確。

是否需要設備
    **不需要**。完全離線：不連任何設備、不開 socket、無檔案 I/O。
    datetime 一律以參數注入，離峰日以 StaticOffPeakDayProvider 注入。

涵蓋範圍
    A. Season        5/15、5/16、10/15、10/16 邊界（G 夏月為 5/16~10/15）
    B. 夏月平日      08:59:59 / 09:00:00 / 23:59:59
    C. 夏月週六      同上，且為 HALF_PEAK
    D. 夏月週日      全天 OFF_PEAK
    E. 非夏月平日    06:00 / 11:00 / 14:00 三組邊界
    F. 非夏月週六    同上，且為 HALF_PEAK
    G. 離峰日        全天 OFF_PEAK；且優先於週六
    H. Fail Closed   無效 datetime / 離峰日來源不可用 / 無效 config / overlap / gap
    I. Config 驗證   時段表完整性
    J. Independence  不依賴 Phase 6.1 / 6.2，不產生其他維度的狀態

測試用離峰日資料
    僅為**測試 fixture**（國曆固定的 8 日），不是 production 離峰日來源。
    農曆節日與逐年變動日刻意不納入 —— 見 tou_calendar.UnknownHolidayProvider。

用法
    python test_phase6_tou_calendar.py          # exit 0 = PASS
"""
import os
import sys
import ast
from datetime import datetime, date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tou_calendar as TC                              # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# ---- 測試 fixture：只含國曆固定日，不是 production 來源 ----
FIXTURE_2026 = {
    date(2026, 1, 1),    # 開國紀念日
    date(2026, 2, 28),   # 和平紀念日
    date(2026, 4, 4),    # 兒童節
    date(2026, 5, 1),    # 勞動節
    date(2026, 9, 28),   # 教師節
    date(2026, 10, 10),  # 國慶日（2026 為週六）
    date(2026, 10, 25),  # 臺灣光復紀念日
    date(2026, 12, 25),  # 行憲紀念日
}
HP = TC.StaticOffPeakDayProvider(FIXTURE_2026, known_years={2026})


def st(y, mo, d, h, mi=0, se=0, provider=HP):
    """在指定時刻求 TOU 狀態字串。"""
    return TC.classify_tou(datetime(y, mo, d, h, mi, se), TC.G_CONFIG, provider).state


def full(y, mo, d, h, mi=0, se=0, provider=HP):
    return TC.classify_tou(datetime(y, mo, d, h, mi, se), TC.G_CONFIG, provider)


# 測試日期（星期已核對）
SUM_WD = (2026, 8, 20)    # 夏月・週四
SUM_SAT = (2026, 8, 22)   # 夏月・週六
SUM_SUN = (2026, 8, 23)   # 夏月・週日
NON_WD = (2026, 1, 15)    # 非夏月・週四
NON_SAT = (2026, 1, 17)   # 非夏月・週六
NON_SUN = (2026, 1, 18)   # 非夏月・週日

P, H_, O, U = TC.TOU_PEAK, TC.TOU_HALF_PEAK, TC.TOU_OFF_PEAK, TC.TOU_UNKNOWN


def main():
    print("== Phase 6.2b TOU Calendar 驗證（完全離線）==\n")
    print(f"方案：[{TC.G_CONFIG.plan_id}] {TC.G_CONFIG.plan_name}")
    print(f"夏月：{TC.G_CONFIG.summer_start} ~ {TC.G_CONFIG.summer_end}")
    print("⚠ 依指定以 G 為實作基準，未經現場電費單／契約證實\n")

    # ---------------- A. Season ----------------
    print("A. Season（G 夏月 5/16~10/15，非 6/1~9/30）")
    S, N = TC.SEASON_SUMMER, TC.SEASON_NON_SUMMER
    cs = lambda m, d: TC.classify_season(date(2026, m, d), TC.G_CONFIG)
    check("5/15 → 非夏月", cs(5, 15) == N)
    check("5/16 → 夏月（起始日包含）", cs(5, 16) == S)
    check("10/15 → 夏月（結束日包含）", cs(10, 15) == S)
    check("10/16 → 非夏月", cs(10, 16) == N)
    check("6/1 與 9/30 皆在夏月內（不因舊定義而改變）", cs(6, 1) == S and cs(9, 30) == S)
    check("5/31 在夏月內（舊 6/1 定義會誤判為非夏月）", cs(5, 31) == S)
    check("10/1 在夏月內（舊 9/30 定義會誤判為非夏月）", cs(10, 1) == S)
    check("1/1、12/31 → 非夏月", cs(1, 1) == N and cs(12, 31) == N)

    # ---------------- B. 夏月平日 ----------------
    print("\nB. 夏月・平日（00:00-09:00 OFF_PEAK / 09:00-24:00 PEAK）")
    check("00:00:00 → OFF_PEAK", st(*SUM_WD, 0, 0, 0) == O)
    check("08:59:59 → OFF_PEAK", st(*SUM_WD, 8, 59, 59) == O)
    check("09:00:00 → PEAK（邊界含起點）", st(*SUM_WD, 9, 0, 0) == P)
    check("09:00:01 → PEAK", st(*SUM_WD, 9, 0, 1) == P)
    check("23:59:59 → PEAK（不含 24:00 端點）", st(*SUM_WD, 23, 59, 59) == P)
    r = full(*SUM_WD, 9, 30)
    check("命中區間為 09:00~24:00", r.period_start_min == 540 and r.period_end_min == 1440)
    check("day_type=WEEKDAY、season=SUMMER",
          r.day_type == TC.DAY_WEEKDAY and r.season == S and r.valid)

    # ---------------- C. 夏月週六 ----------------
    print("\nC. 夏月・週六（00:00-09:00 OFF_PEAK / 09:00-24:00 HALF_PEAK）")
    check("08:59:59 → OFF_PEAK", st(*SUM_SAT, 8, 59, 59) == O)
    check("09:00:00 → HALF_PEAK", st(*SUM_SAT, 9, 0, 0) == H_)
    check("23:59:59 → HALF_PEAK", st(*SUM_SAT, 23, 59, 59) == H_)
    check("週六不得判成 PEAK", st(*SUM_SAT, 15, 0) != P)
    check("day_type=SATURDAY", full(*SUM_SAT, 15, 0).day_type == TC.DAY_SATURDAY)

    # ---------------- D. 夏月週日 ----------------
    print("\nD. 夏月・週日（全日 OFF_PEAK）")
    allday = [st(*SUM_SUN, h, m) for h in range(24) for m in (0, 30, 59)]
    check("00:00~23:59 全部 OFF_PEAK", set(allday) == {O})
    check("day_type=SUNDAY", full(*SUM_SUN, 12, 0).day_type == TC.DAY_SUNDAY)

    # ---------------- E. 非夏月平日 ----------------
    print("\nE. 非夏月・平日（06/11/14 三組邊界）")
    check("05:59:59 → OFF_PEAK", st(*NON_WD, 5, 59, 59) == O)
    check("06:00:00 → PEAK", st(*NON_WD, 6, 0, 0) == P)
    check("10:59:59 → PEAK", st(*NON_WD, 10, 59, 59) == P)
    check("11:00:00 → OFF_PEAK", st(*NON_WD, 11, 0, 0) == O)
    check("13:59:59 → OFF_PEAK", st(*NON_WD, 13, 59, 59) == O)
    check("14:00:00 → PEAK", st(*NON_WD, 14, 0, 0) == P)
    check("23:59:59 → PEAK", st(*NON_WD, 23, 59, 59) == P)
    check("00:00:00 → OFF_PEAK", st(*NON_WD, 0, 0, 0) == O)

    # ---------------- F. 非夏月週六 ----------------
    print("\nF. 非夏月・週六（同邊界，但為 HALF_PEAK）")
    check("05:59:59 → OFF_PEAK", st(*NON_SAT, 5, 59, 59) == O)
    check("06:00:00 → HALF_PEAK", st(*NON_SAT, 6, 0, 0) == H_)
    check("10:59:59 → HALF_PEAK", st(*NON_SAT, 10, 59, 59) == H_)
    check("11:00:00 → OFF_PEAK", st(*NON_SAT, 11, 0, 0) == O)
    check("13:59:59 → OFF_PEAK", st(*NON_SAT, 13, 59, 59) == O)
    check("14:00:00 → HALF_PEAK", st(*NON_SAT, 14, 0, 0) == H_)
    check("23:59:59 → HALF_PEAK", st(*NON_SAT, 23, 59, 59) == H_)
    check("非夏月週六全日不得出現 PEAK",
          P not in {st(*NON_SAT, h, m) for h in range(24) for m in (0, 30, 59)})

    # ---------------- G. 週日／離峰日 ----------------
    print("\nG. 週日／離峰日")
    check("非夏月週日全日 OFF_PEAK",
          {st(*NON_SUN, h, m) for h in range(24) for m in (0, 30)} == {O})
    check("離峰日（2026-01-01 週四，非夏月）全日 OFF_PEAK",
          {st(2026, 1, 1, h, m) for h in range(24) for m in (0, 30)} == {O})
    r = full(2026, 1, 1, 9, 0)
    check("離峰日 day_type=OFF_PEAK_DAY（非 WEEKDAY）",
          r.day_type == TC.DAY_OFF_PEAK_DAY and r.is_off_peak_day is True)
    # 2026-10-10 為週六且在夏月 → 若無優先權會被判成 HALF_PEAK
    check("★ 離峰日優先於週六：2026-10-10（夏月・週六）15:00 → OFF_PEAK 而非 HALF_PEAK",
          st(2026, 10, 10, 15, 0) == O)
    check("  同日 day_type=OFF_PEAK_DAY", full(2026, 10, 10, 15, 0).day_type == TC.DAY_OFF_PEAK_DAY)
    check("  對照：同為夏月週六但非離峰日（8/22）15:00 → HALF_PEAK",
          st(*SUM_SAT, 15, 0) == H_)
    check("非離峰日的平日 is_off_peak_day=False", full(*SUM_WD, 9, 0).is_off_peak_day is False)

    # ---------------- H. Fail Closed ----------------
    print("\nH. Fail Closed")
    r = TC.classify_tou("2026-08-20 09:00", TC.G_CONFIG, HP)
    check("dt 為字串 → UNKNOWN / INVALID_DATETIME",
          r.state == U and r.reason == TC.R_INVALID_DATETIME and not r.valid)
    r = TC.classify_tou(date(2026, 8, 20), TC.G_CONFIG, HP)
    check("dt 為 date（非 datetime）→ UNKNOWN / INVALID_DATETIME",
          r.state == U and r.reason == TC.R_INVALID_DATETIME)
    r = TC.classify_tou(None, TC.G_CONFIG, HP)
    check("dt 為 None → UNKNOWN", r.state == U and r.reason == TC.R_INVALID_DATETIME)

    r = TC.classify_tou(datetime(2026, 8, 20, 9, 0))          # 預設 provider
    check("★ 預設無正式離峰日來源 → UNKNOWN / HOLIDAY_SOURCE_UNAVAILABLE（不假裝非假日）",
          r.state == U and r.reason == TC.R_HOLIDAY_SOURCE_UNAVAILABLE)
    r = TC.classify_tou(datetime(2027, 8, 20, 9, 0), TC.G_CONFIG, HP)
    check("查詢 provider 未涵蓋年度（2027）→ UNKNOWN / HOLIDAY_SOURCE_UNAVAILABLE",
          r.state == U and r.reason == TC.R_HOLIDAY_SOURCE_UNAVAILABLE)

    class BoomProvider(TC.HolidayProvider):
        def is_off_peak_day(self, d):
            raise RuntimeError("source down")

    r = TC.classify_tou(datetime(2026, 8, 20, 9, 0), TC.G_CONFIG, BoomProvider())
    check("provider 拋例外 → UNKNOWN（不得讓判定看起來正常）",
          r.state == U and r.reason == TC.R_HOLIDAY_SOURCE_UNAVAILABLE)

    class BadTypeProvider(TC.HolidayProvider):
        def is_off_peak_day(self, d):
            return "yes"

    r = TC.classify_tou(datetime(2026, 8, 20, 9, 0), TC.G_CONFIG, BadTypeProvider())
    check("provider 回傳非 bool/None → UNKNOWN",
          r.state == U and r.reason == TC.R_HOLIDAY_SOURCE_UNAVAILABLE)

    broken = SimpleNamespace(plan_id="X", summer_start=(5, 16), summer_end=(10, 15),
                             rules={})
    r = TC.classify_tou(datetime(2026, 8, 20, 9, 0), broken, HP)
    check("config.rules 缺組合 → UNKNOWN / INVALID_CONFIG",
          r.state == U and r.reason == TC.R_INVALID_CONFIG)

    broken2 = SimpleNamespace(plan_id="X", summer_start=(5, 16), summer_end=(10, 15),
                              rules=None)
    r = TC.classify_tou(datetime(2026, 8, 20, 9, 0), broken2, HP)
    check("config.rules 非 mapping → UNKNOWN / INVALID_CONFIG",
          r.state == U and r.reason == TC.R_INVALID_CONFIG)

    gap_rules = dict(TC.G_RULES)
    gap_rules[(S, TC.DAY_WEEKDAY)] = ((0, 540, O), (600, 1440, P))     # 540~600 gap
    gapcfg = SimpleNamespace(plan_id="X", summer_start=(5, 16), summer_end=(10, 15),
                             rules=gap_rules)
    r = TC.classify_tou(datetime(2026, 8, 20, 9, 30), gapcfg, HP)
    check("時段表有 gap 且落在 gap 內 → UNKNOWN / NO_MATCHING_PERIOD",
          r.state == U and r.reason == TC.R_NO_MATCHING_PERIOD)

    check("UNKNOWN 時 valid=False 且無時段資訊",
          (not r.valid) and r.period_start_min is None and r.period_end_min is None)

    # ---------------- I. Config 驗證 ----------------
    print("\nI. Config 驗證（時段表完整性）")
    ok, probs = TC.validate_rules(TC.G_RULES)
    check(f"G 標準時段表通過驗證（問題數={len(probs)}）", ok)
    bad = dict(TC.G_RULES); bad[(S, TC.DAY_WEEKDAY)] = ((0, 540, O), (600, 1440, P))
    ok, probs = TC.validate_rules(bad)
    check("偵測到 gap", (not ok) and any("gap" in p for p in probs))
    bad = dict(TC.G_RULES); bad[(S, TC.DAY_WEEKDAY)] = ((0, 600, O), (540, 1440, P))
    ok, probs = TC.validate_rules(bad)
    check("偵測到 overlap", (not ok) and any("重疊" in p for p in probs))
    bad = dict(TC.G_RULES); bad[(S, TC.DAY_WEEKDAY)] = ((0, 540, O), (540, 1400, P))
    ok, probs = TC.validate_rules(bad)
    check("偵測到未覆蓋整日（結束於 1400 而非 1440）",
          (not ok) and any("未覆蓋整日" in p for p in probs))
    bad = dict(TC.G_RULES); bad[(S, TC.DAY_WEEKDAY)] = ((0, 540, O), (540, 1440, "IMPORT"))
    ok, probs = TC.validate_rules(bad)
    check("偵測到非法狀態（IMPORT 不屬 TOU 維度）",
          (not ok) and any("狀態不合法" in p for p in probs))
    bad = dict(TC.G_RULES); del bad[(N, TC.DAY_SATURDAY)]
    ok, probs = TC.validate_rules(bad)
    check("偵測到缺少組合", (not ok) and any("缺少組合" in p for p in probs))

    try:
        TC.TouConfig(rules={})
        raised = False
    except ValueError:
        raised = True
    check("TouConfig 以不合法時段表建構 → 立即 ValueError（不靜默）", raised)
    try:
        TC.TouConfig(summer_start=(13, 1))
        raised = False
    except ValueError:
        raised = True
    check("TouConfig 以不合法 (月,日) 建構 → ValueError", raised)

    check("24:00 以 1440 表示（非 0）",
          TC.hm(24) == 1440 and TC.G_RULES[(S, TC.DAY_WEEKDAY)][-1][1] == 1440)
    check("G 標準時段無跨午夜區間（每段 start < end）",
          all(s < e for ps in TC.G_RULES.values() for s, e, _ in ps))

    # ---------------- J. Independence ----------------
    print("\nJ. Independence（與 Phase 6.1 / 6.2 完全獨立）")
    check("未載入 meter_client", "meter_client" not in sys.modules)
    check("未載入 power_classifier", "power_classifier" not in sys.modules)

    src = open(TC.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    check(f"原始碼未 import 其他 Phase 模組（imports={sorted(imported)}）",
          not (imported & {"meter_client", "power_classifier"}))

    idents = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            idents.add(node.id)
        elif isinstance(node, ast.Attribute):
            idents.add(node.attr)
    banned = idents & {"power_kw", "demand_kw", "GridPowerState", "MeterSnapshot",
                       "meter_state", "demand_state", "soc", "bess_sum"}
    check(f"程式碼未引用 Phase 6.1/6.2 的資料欄位（命中={sorted(banned)}）", not banned)

    check("狀態值域與其他維度字彙無交集",
          TC.VALID_TOU_STATES.isdisjoint(TC.FORBIDDEN_STATES))

    texts, states = [], set()
    for d0 in (SUM_WD, SUM_SAT, SUM_SUN, NON_WD, NON_SAT, NON_SUN):
        for h in range(0, 24, 3):
            r = full(*d0, h, 0)
            texts.append(str(r)); states.add(r.state)
    texts.append(str(TC.classify_tou(datetime(2027, 1, 1, 0, 0), TC.G_CONFIG, HP)))
    blob = " ".join(texts)
    hit = sorted(w for w in TC.FORBIDDEN_STATES if w in blob)
    check(f"輸出不含 IMPORT / EXPORT / CHARGE / DISCHARGE 等字彙（命中={hit}）", not hit)
    check(f"實際出現的狀態皆合法：{sorted(states)}", states <= TC.VALID_TOU_STATES)

    d = full(*SUM_WD, 9, 0).as_dict()
    check("TouState 具備診斷欄位（season/day_type/區間/reason/valid）",
          all(k in d for k in ("season", "day_type", "period_start_min",
                               "period_end_min", "reason", "valid", "is_off_peak_day")))
    # 以 AST 偵測「實際呼叫」而非字串比對 —— docstring 內提到 datetime.now() 不算違規
    CORE_FUNCS = ("classify_tou", "classify_season", "classify_day_type",
                  "_find_period", "validate_rules", "day_schedule")
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    check(f"核心判定函式齊備（{', '.join(CORE_FUNCS)}）",
          all(f in fns for f in CORE_FUNCS))
    offenders = []
    for fname in CORE_FUNCS:
        for node in ast.walk(fns[fname]):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("now", "today", "utcnow")):
                offenders.append(f"{fname}:{node.func.attr}()")
    check(f"核心判定不呼叫 now()/today()/utcnow()，dt 一律由外部注入（違規={offenders}）",
          not offenders)
    main_fn = fns.get("main")
    main_now = [n for n in ast.walk(main_fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "now"] if main_fn else []
    check("僅 CLI 的 main() 取用現在時間（責任邊界清楚）", len(main_now) == 1)

    # ---------------- 總結 ----------------
    ok_all = all(RESULTS)
    print(f"\n== Phase 6.2b TOU Calendar 驗證 {'PASS' if ok_all else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
