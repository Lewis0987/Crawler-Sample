# -*- coding: utf-8 -*-
"""
annual_calendar_materializer.py — 年度離峰日草稿產生器（Phase D.4-C）
======================================================================
把「規則」展開成「某一年度的實際國曆日期」，**只產生草稿**。

    規則表 → Materializer → 草稿（verified=false）→ 人工核准 → Production

🔴 **Materializer 永遠不能產生已核准的資料**
    產出一律 `verified=False`。要進 production 必須經人工核准後另行升級。
    這由結構保證（本檔不接受 verified 參數，也不呼叫任何升級路徑）並由測試鎖住。

🔴 **只自動展開能由規則唯一決定的項目**
    FIXED_GREGORIAN      → 可自動展開（月日固定）
    LUNAR_SINGLE_DAY     → **必須人工提供**該年度的國曆日期
    LUNAR_RANGE          → **必須人工提供**該年度的國曆日期區間
    VARIABLE_GREGORIAN   → **必須人工提供**該年度實際適用哪一天
    ⚠️ 未提供者一律列為 unresolved，**絕不猜測、絕不固定成某一天**。

🔴 **不做農曆換算**
    專案內無農曆能力（無函式庫、標準庫不支援），且**不新增第三方相依**。
    農曆日期一律由人工提供並核對 —— 這也讓年度資料可 code review、可版本控管。

🔴 **不完整的草稿不得升級**
    只要有任何 unresolved 項目，`can_promote()` 即為 False。

用法（完全離線）
    python annual_calendar_materializer.py --year 2026
"""

import argparse
from datetime import date
from dataclasses import dataclass, asdict

import taipower_offpeak_rules as RULES
import annual_off_peak_calendar as AC

# ======================================================================
# 產出狀態
# ======================================================================
MAT_COMPLETE = "MATERIALIZED_COMPLETE"       # 全部規則都已解析
MAT_INCOMPLETE = "MATERIALIZED_INCOMPLETE"   # 有項目待人工提供
MAT_FAILED = "MATERIALIZATION_FAILED"        # 連草稿都建立不出來

# unresolved 原因
UR_LUNAR_NOT_SUPPLIED = "LUNAR_DATE_NOT_SUPPLIED"
UR_VARIABLE_NOT_SUPPLIED = "VARIABLE_DATE_NOT_SUPPLIED"
UR_SUPPLIED_WRONG_YEAR = "SUPPLIED_DATE_WRONG_YEAR"
UR_SUPPLIED_INVALID = "SUPPLIED_DATE_INVALID"
UR_NOT_A_CANDIDATE = "SUPPLIED_DATE_NOT_A_CANDIDATE"

# 產生方法（寫進草稿的 notes，供事後追溯）
METHOD = "rule_expansion_v1"


@dataclass(frozen=True)
class MaterializationResult:
    """
    一次草稿產生的結果。

    🔴 `draft` 一律 `verified=False`。
    🔴 只要 `unresolved` 非空，`can_promote` 即為 False。
    """
    year: int
    outcome: str
    draft: object = None
    resolved: tuple = ()          # ((rule_key, date), ...)
    unresolved: tuple = ()        # ((rule_key, kind, reason), ...)
    method: str = METHOD
    rule_source: str = RULES.RULE_SOURCE
    materialized_at: str = None
    reason: str = ""

    @property
    def can_promote(self):
        """不完整的草稿**不得**升級為 production 資料。"""
        return (self.draft is not None and not self.unresolved
                and self.outcome == MAT_COMPLETE)

    def as_dict(self):
        d = asdict(self)
        d["draft"] = self.draft.as_dict() if self.draft is not None else None
        d["resolved"] = [(k, x.isoformat()) for k, x in self.resolved]
        d["unresolved"] = [list(u) for u in self.unresolved]
        d["can_promote"] = self.can_promote
        return d

    def __str__(self):
        n = 0 if self.draft is None else len(self.draft.off_peak_dates)
        return (f"[MATERIALIZE {self.year}] {self.outcome} "
                f"已解析={len(self.resolved)} 待人工={len(self.unresolved)} "
                f"草稿日期數={n} can_promote={self.can_promote}")


def _coerce(d):
    """把人工提供的值正規化成 date；無法解析回 None。"""
    if isinstance(d, date) and not isinstance(d, bool):
        return d
    if isinstance(d, str):
        try:
            return date.fromisoformat(d)
        except ValueError:
            return None
    return None


def materialize(year, source_version, effective_date, supplied=None,
                rules=None, materialized_at=None):
    """
    產生某年度的草稿。回傳 MaterializationResult。

    supplied
        人工提供的年度日期：{rule_key: date | [date, ...] | "YYYY-MM-DD"}。
        農曆與逐年變動項目**必須**由此提供，否則列為 unresolved。

    🔴 本函式**不接受** verified 參數 —— 產出恆為 verified=False。
    🔴 只使用 confirmed=True 的官方規則；未確認項目一律不納入。
    """
    supplied = dict(supplied or {})
    use = RULES.rules_requiring_annual_dates(rules)
    resolved, unresolved, dates = [], [], []

    for r in use:
        if r.kind in RULES.COVERED_BY_DAYTYPE_RULE:
            # 週日已由既有日型規則涵蓋 —— 不需要年度資料，也不進入年度清單
            continue
        if r.kind == RULES.RULE_FIXED_GREGORIAN:
            # 唯一能由規則自動決定的分類
            d = date(year, r.spec["month"], r.spec["day"])
            resolved.append((r.key, d))
            dates.append(d)
            continue

        raw = supplied.get(r.key)
        if raw is None:
            why = (UR_VARIABLE_NOT_SUPPLIED
                   if r.kind == RULES.RULE_VARIABLE_GREGORIAN
                   else UR_LUNAR_NOT_SUPPLIED)
            unresolved.append((r.key, r.kind, why))
            continue

        items = raw if isinstance(raw, (list, tuple)) else [raw]
        parsed, bad = [], None
        for x in items:
            d = _coerce(x)
            if d is None:
                bad = UR_SUPPLIED_INVALID
                break
            if d.year != year:
                bad = UR_SUPPLIED_WRONG_YEAR
                break
            if (r.kind == RULES.RULE_VARIABLE_GREGORIAN
                    and (d.month, d.day) not in
                    [tuple(c) for c in r.spec["candidates"]]):
                # 🔴 逐年變動項目只能落在官方列出的候選日 —— 不接受任意日期
                bad = UR_NOT_A_CANDIDATE
                break
            parsed.append(d)
        if bad is not None:
            unresolved.append((r.key, r.kind, bad))
            continue
        for d in parsed:
            resolved.append((r.key, d))
            dates.append(d)

    notes = (f"method={METHOD}; rule_source={RULES.RULE_SOURCE}; "
             f"materialized_at={materialized_at}; "
             f"unresolved={[u[0] for u in unresolved] or '(無)'}")
    # 🔴 verified 一律 False —— 這裡是硬編碼的字面值，不接受任何覆寫。
    draft, why = AC.build_annual_list(year, sorted(set(dates)),
                                      RULES.RULE_SOURCE, source_version,
                                      effective_date, verified=False)
    if draft is None:
        return MaterializationResult(year=year, outcome=MAT_FAILED,
                                     resolved=tuple(resolved),
                                     unresolved=tuple(unresolved),
                                     materialized_at=materialized_at,
                                     reason=why)
    return MaterializationResult(
        year=year,
        outcome=MAT_COMPLETE if not unresolved else MAT_INCOMPLETE,
        draft=draft, resolved=tuple(resolved), unresolved=tuple(unresolved),
        materialized_at=materialized_at, reason=notes)


def draft_document(result):
    """
    把草稿轉成人工 provisioning 檔案的內容（dict，供寫成 JSON 交付審查）。

    🔴 `verified` 恆為 false —— 審查者必須自行改為 true 才可能進 production。
    """
    if result.draft is None:
        return None
    doc = {
        "schema_version": AC.SCHEMA_VERSION,
        "year": result.year,
        "source": result.draft.source,
        "source_version": result.draft.source_version,
        "effective_date": result.draft.effective_date.isoformat(),
        "verified": False,
        "off_peak_dates": [d.isoformat() for d in result.draft.off_peak_dates],
        "rule_source": result.rule_source,
        "materialized_at": result.materialized_at,
        "materialization_method": result.method,
        "unresolved": [list(u) for u in result.unresolved],
        "notes": result.reason,
    }
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description="年度離峰日草稿產生（唯讀、離線）")
    ap.add_argument("--year", type=int, default=2026)
    args = ap.parse_args(argv)

    print("== 年度離峰日草稿產生（D.4-C，完全離線）==\n")
    r = materialize(args.year, "draft-v1", date(args.year, 1, 1),
                    materialized_at="(未指定)")
    print(f"  {r}\n")
    print("  已自動解析（僅固定國曆日）：")
    for k, d in r.resolved:
        print(f"    {d.isoformat()}  {k}")
    print("\n  待人工提供（不得猜測）：")
    for k, kind, why in r.unresolved:
        print(f"    {k:<22} {kind:<19} {why}")
    print(f"\n  草稿 verified = {r.draft.verified}（永遠 False）")
    print(f"  can_promote   = {r.can_promote}（有 unresolved 即不得升級）")
    print("\n  ⚠ 不做農曆換算、不新增第三方相依、不猜測掃墓節日期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
