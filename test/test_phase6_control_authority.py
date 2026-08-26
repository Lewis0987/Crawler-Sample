# -*- coding: utf-8 -*-
"""
Phase 6.5-H Control Authority 驗證 —— 完全離線
======================================================================
是否需要設備
    **不需要**。control_authority 是純邏輯、零 I/O 模組，所有輸入以參數注入。

涵蓋範圍
    A~S  裁示指定的完整測試矩陣
    T    Fail-Closed Baseline：production default（ttl / tolerance 皆 None）行為
    U    值域、reason code、與 pcs_control_integration 的常數一致性
    V    零 I/O 與相依邊界（不得 import 任何連線或檔案模組）

用法
    python test_phase6_control_authority.py          # exit 0 = PASS
"""
import os
import sys
import ast
import io as _io
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import control_authority as CA                       # noqa: E402

# 於 import control_authority 後立即快照 —— 證明它不具備任何 I/O 能力
_MODULES_AFTER_CA = set(sys.modules)

import pcs_control_integration as PCI                # noqa: E402
import last_control_store as LC                      # noqa: E402

RESULTS = []
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = _io.open(os.path.join(HERE, "control_authority.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

NOW = 1000.0
MODE_OFF = {"schedule_switch": 0, "manual_switch": 1}
MODE_ON = {"schedule_switch": 1, "manual_switch": 0}
# ⚠️ 測試注入政策；production default 仍為 None（T 節鎖住）
POL = CA.AuthorityPolicy(authority_ttl_sec=120.0, authority_power_tolerance_kw=1.0)


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


class LCRec:
    """
    LastControlRecord 的極簡替身。

    🔁 Blocker 13 補齊：`actual_active_power_kw`（read-back 當下的觀測功率）
       是真實 LastControlRecord 的**必填**欄位，此替身原本漏了。
       Authority 需要它作為「暫存器是否已更新」的基準，因此必須提供。
       預設為實機閒置值 −1.3 kW（指令當下 AC 尚未跟上的典型情形）。
    """

    def __init__(self, action="discharge", power=5.0, at=NOW - 10.0,
                 power_at_verify=-1.3):
        self.action = action
        self.target_power_kw = power
        self.verified_at_monotonic = at
        self.actual_active_power_kw = power_at_verify


_KEEP = object()          # sentinel：區分「不覆寫」與「明確給 None」


def req(state="STANDBY", power=-1.4, lc=None, trust=None, mode=_KEEP, valid=True,
        observed_at=NOW):
    # 🔁 Blocker 13：觀測時戳預設為「現在」，晚於 LCRec 的驗證時刻（NOW-10）
    return CA.AuthorityRequest(pcs_state=state, actual_active_power_kw=power,
                               last_control=lc, last_control_trust=trust,
                               pcs_mode_state=MODE_OFF if mode is _KEEP else mode,
                               ess_valid=valid,
                               observed_at_monotonic=observed_at)


def ev(r, pol=None, now=NOW):
    return CA.evaluate(r, pol if pol is not None else POL, now=now)


def main():
    print("== Phase 6.5-H Control Authority 驗證（完全離線）==\n")
    print(f"production default：ttl={CA.DEFAULT_AUTHORITY_POLICY.authority_ttl_sec} "
          f"tolerance={CA.DEFAULT_AUTHORITY_POLICY.authority_power_tolerance_kw} "
          f"require_schedule_off={CA.DEFAULT_AUTHORITY_POLICY.require_schedule_off}")
    print(f"測試注入政策：ttl={POL.authority_ttl_sec} tolerance={POL.authority_power_tolerance_kw}\n")

    # ---------------- A ----------------
    print("A. PCS 閒置、無 LastControl → IDLE")
    for st in ("STANDBY", "STOPPED"):
        r = ev(req(st))
        check(f"  {st} → IDLE / allowed", r.state == CA.AUTH_IDLE and r.allowed is True)
        check(f"  {st} reason = CONTROL_AUTHORITY_IDLE", r.reason == CA.CA_IDLE)
    check("★ IDLE 不需要 LastControl（未提供也放行）",
          ev(req("STANDBY", lc=None, trust=None)).allowed is True)

    # ---------------- B / C ----------------
    print("\nB/C. PCS 運轉中、無 LastControl → EXTERNAL_OR_UNKNOWN")
    for st in ("DISCHARGING", "CHARGING"):
        r = ev(req(st, power=-80.2 if st == "DISCHARGING" else 80.2))
        check(f"★★ {st} 無 LastControl → EXTERNAL_OR_UNKNOWN / BLOCK",
              r.state == CA.AUTH_EXTERNAL and r.allowed is False)
        check(f"  {st} reason = CONTROL_AUTHORITY_EXTERNAL", r.reason == CA.CA_EXTERNAL)
    check("★ reason 明確表達控制權問題，不是設備安全失敗",
          ev(req("DISCHARGING", -80.2)).reason.startswith("CONTROL_AUTHORITY_"))

    # ---------------- D ----------------
    print("\nD. 運轉中 + 可對應的 LastControl（注入政策）→ OWNED_BY_PHASE6")
    r = ev(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 10.0), LC.TRUST_FOR_INTERVAL))
    check("★★ DISCHARGING −5.4kW + LC discharge 5kW + TTL 內 + same boot → OWNED",
          r.state == CA.AUTH_OWNED and r.allowed is True and r.reason == CA.CA_PHASE6)
    check("  回傳帶出年齡與功率佐證",
          abs(r.age_sec - 10.0) < 1e-9 and r.observed_power_kw == -5.4
          and r.last_control_power_kw == 5.0)
    r = ev(req("CHARGING", 5.3, LCRec("charge", 5.0, NOW - 1.0), LC.TRUST_FOR_INTERVAL))
    check("  CHARGING +5.3kW + LC charge 5kW → OWNED（符號由 state 表達，功率取絕對值比較）",
          r.state == CA.AUTH_OWNED)

    # ---------------- E ----------------
    print("\nE. 功率明顯不符 → CONFLICT")
    r = ev(req("DISCHARGING", -80.2, LCRec("discharge", 5.0, NOW - 10.0), LC.TRUST_FOR_INTERVAL))
    check("★★ DISCHARGING −80.2kW + LC discharge 5kW → CONFLICT / BLOCK（本次實機情境）",
          r.state == CA.AUTH_CONFLICT and r.allowed is False
          and r.reason == CA.CA_CONFLICT_POWER)
    check("  detail 說明差距與容差", "75.20" in r.detail and "容差" in r.detail)
    check("★ 只比 action 會誤判 —— 此處 action 相符但仍必須 CONFLICT",
          r.last_control_action == "discharge" and r.pcs_state == "DISCHARGING")
    check("  剛好等於容差 → 仍算相符",
          ev(req("DISCHARGING", -6.0, LCRec("discharge", 5.0, NOW - 1.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_OWNED)
    check("  超過容差一點點 → CONFLICT",
          ev(req("DISCHARGING", -6.2, LCRec("discharge", 5.0, NOW - 1.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_CONFLICT)

    # ---------------- F ----------------
    print("\nF. 紀錄超過 TTL → UNKNOWN / EXPIRED")
    r = ev(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 500.0), LC.TRUST_FOR_INTERVAL))
    check("★★ action 相符但年齡 500s > TTL 120s → UNKNOWN / EXPIRED / BLOCK",
          r.state == CA.AUTH_UNKNOWN and r.reason == CA.CA_EXPIRED and r.allowed is False)
    check("  剛好等於 TTL → 仍有效",
          ev(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 120.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_OWNED)
    check("★ 紀錄時間在未來（時間基準異常）→ UNKNOWN",
          ev(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW + 5.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_UNKNOWN)
    check("  now 缺失 → UNKNOWN（無法計算年齡）",
          ev(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 10.0),
                 LC.TRUST_FOR_INTERVAL), now=None).state == CA.AUTH_UNKNOWN)

    # ---------------- G / H ----------------
    print("\nG/H. Restart / Reboot")
    r = ev(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 30.0), LC.TRUST_FOR_INTERVAL))
    check("★★ G. 服務重啟、同一 boot、全部吻合 → OWNED（future reclaim 可成立）",
          r.state == CA.AUTH_OWNED and r.allowed is True)
    for trust, label in ((LC.TRUST_HISTORY_ONLY, "HISTORY_ONLY（已重開機／boot 未知）"),
                         (LC.TRUST_INVALID, "INVALID（紀錄損毀）"),
                         (None, "trust 未提供")):
        r = ev(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 10.0), trust))
        check(f"★★ H. {label} → BLOCK / TRUST_INSUFFICIENT",
              r.allowed is False and r.reason == CA.CA_TRUST_INSUFFICIENT
              and r.state == CA.AUTH_EXTERNAL)
    check("★ 只有 TRUSTED_FOR_INTERVAL 可作為認領證據",
          CA.TRUST_USABLE_FOR_AUTHORITY == {LC.TRUST_FOR_INTERVAL})

    # ---------------- I / J ----------------
    print("\nI/J. PCS 原生排程主開關")
    for st in ("STANDBY", "STOPPED", "CHARGING", "DISCHARGING", "UNKNOWN", "CONFLICT"):
        r = ev(req(st, lc=LCRec("discharge", 5.0, NOW - 1.0),
                   trust=LC.TRUST_FOR_INTERVAL, mode=MODE_ON))
        check(f"★★ I. schedule_switch=ON + PCS {st} → BLOCK / SCHEDULE_ACTIVE",
              r.allowed is False and r.reason == CA.CA_SCHEDULE_ACTIVE
              and r.state == CA.AUTH_EXTERNAL)
    check("★★ I. 排程 ON 時連 IDLE 都不放行（含 STOP 路徑）",
          ev(req("STANDBY", mode=MODE_ON)).allowed is False)
    check("★ J. schedule_switch=OFF → 繼續正常判斷", ev(req("STANDBY", mode=MODE_OFF)).allowed)
    for bad in ({"schedule_switch": None, "manual_switch": 1}, {"manual_switch": 1},
                {"schedule_switch": 2}, None, "oops", []):
        r = ev(req("STANDBY", mode=bad))
        check(f"  排程開關無法確認 {str(bad)[:28]} → UNKNOWN / MODE_UNKNOWN",
              r.state == CA.AUTH_UNKNOWN and r.reason == CA.CA_MODE_UNKNOWN)
    check("  manual_switch 不影響 Authority（模式屬 Safety Gate 的職責）",
          ev(req("STANDBY", mode={"schedule_switch": 0, "manual_switch": 0})).allowed is True)

    # ---------------- M / O ----------------
    print("\nM/O. 資料不足 / 狀態不可用")
    for st in ("UNKNOWN", "CONFLICT"):
        r = ev(req(st, lc=LCRec(), trust=LC.TRUST_FOR_INTERVAL))
        check(f"★★ O. pcs_state={st} → UNKNOWN / PCS_STATE_UNUSABLE / BLOCK",
              r.state == CA.AUTH_UNKNOWN and r.reason == CA.CA_PCS_STATE_UNUSABLE
              and r.allowed is False)
    check("  pcs_state 不在值域（None / 亂填）→ UNKNOWN",
          ev(req(None)).reason == CA.CA_PCS_STATE_UNUSABLE
          and ev(req("RUNNING")).reason == CA.CA_PCS_STATE_UNUSABLE)
    for v in (False, None, 0, "yes"):
        check(f"★★ M. ess_valid={v!r}（stale / 無效）→ UNKNOWN / DATA_INSUFFICIENT",
              ev(req("STANDBY", valid=v)).state == CA.AUTH_UNKNOWN
              and ev(req("STANDBY", valid=v)).reason == CA.CA_DATA_INSUFFICIENT)
    check("  ess_valid=True 才放行", ev(req("STANDBY", valid=True)).allowed is True)
    check("  request=None → UNKNOWN", ev(None).state == CA.AUTH_UNKNOWN)
    check("  policy=None → UNKNOWN", CA.evaluate(req("STANDBY"), None, NOW).state
          == CA.AUTH_UNKNOWN)
    check("  運轉中但功率缺失 → UNKNOWN",
          ev(req("DISCHARGING", None, LCRec("discharge", 5.0, NOW - 1.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_UNKNOWN)
    check("  LastControl.target_power_kw 缺失 → UNKNOWN",
          ev(req("DISCHARGING", -5.4, LCRec("discharge", None, NOW - 1.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_UNKNOWN)

    # ---------------- N ----------------
    print("\nN. 判定過程例外 → FAIL CLOSED")

    class Boom:
        @property
        def pcs_mode_state(self):
            raise RuntimeError("boom")

    r = CA.evaluate(Boom(), POL, NOW)
    check("★★ 存取輸入時拋例外 → UNKNOWN / EVALUATION_ERROR / BLOCK",
          r.state == CA.AUTH_UNKNOWN and r.reason == CA.CA_EVALUATION_ERROR
          and r.allowed is False)
    check("  例外型別被帶出（可診斷）", "RuntimeError" in r.detail)

    class BoomPolicy:
        @property
        def require_schedule_off(self):
            raise ValueError("policy boom")

    check("  policy 存取拋例外 → 同樣 fail closed",
          CA.evaluate(req("STANDBY"), BoomPolicy(), NOW).reason == CA.CA_EVALUATION_ERROR)
    check("★ 原始碼不存在 `except` 後放行的路徑",
          "allowed=True" not in SRC.split("def _evaluate")[0].split("except Exception")[-1])

    # ---------------- P / Q / R ----------------
    print("\nP/Q/R. LastControl 與現況的對應")
    r = ev(req("STOPPED", -1.3, LCRec("discharge", 5.0, NOW - 9999.0), LC.TRUST_FOR_INTERVAL))
    check("★★ P. PCS STOPPED + 很舊的 LastControl → IDLE（舊紀錄不構成占用）",
          r.state == CA.AUTH_IDLE and r.allowed is True)
    check("  P. STANDBY 亦同",
          ev(req("STANDBY", -1.4, LCRec("charge", 5.0, NOW - 9999.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_IDLE)
    r = ev(req("DISCHARGING", -5.4, LCRec("stop", None, NOW - 1.0), LC.TRUST_FOR_INTERVAL))
    check("★★ Q. LastControl=stop 但 PCS DISCHARGING → EXTERNAL / BLOCK",
          r.state == CA.AUTH_EXTERNAL and r.allowed is False and r.reason == CA.CA_EXTERNAL)
    r = ev(req("DISCHARGING", -5.4, LCRec("charge", 5.0, NOW - 1.0), LC.TRUST_FOR_INTERVAL))
    check("★★ R. LastControl=charge 但實際 DISCHARGING → CONFLICT / CONFLICT_STATE",
          r.state == CA.AUTH_CONFLICT and r.reason == CA.CA_CONFLICT_STATE
          and r.allowed is False)
    check("  R. 反向亦然（LastControl=discharge 但實際 CHARGING）",
          ev(req("CHARGING", 5.3, LCRec("discharge", 5.0, NOW - 1.0),
                 LC.TRUST_FOR_INTERVAL)).reason == CA.CA_CONFLICT_STATE)
    check("★ R. 方向矛盾不需要任何門檻即可斷定（TTL/tolerance 未設定時也是 CONFLICT）",
          CA.evaluate(req("DISCHARGING", -5.4, LCRec("charge", 5.0, NOW - 1.0),
                          LC.TRUST_FOR_INTERVAL),
                      CA.DEFAULT_AUTHORITY_POLICY, NOW).state == CA.AUTH_CONFLICT)
    check("  LastControl.action 無法辨識 → EXTERNAL",
          ev(req("DISCHARGING", -5.4, LCRec("wat", 5.0, NOW - 1.0),
                 LC.TRUST_FOR_INTERVAL)).state == CA.AUTH_EXTERNAL)

    # ---------------- S / T ----------------
    print("\nS/T. Fail-Closed Baseline（production default = None）")
    D = CA.DEFAULT_AUTHORITY_POLICY
    check("★★ production default authority_ttl_sec 仍為 None", D.authority_ttl_sec is None)
    check("★★ production default authority_power_tolerance_kw 仍為 None",
          D.authority_power_tolerance_kw is None)
    check("  預設要求排程主開關為 OFF", D.require_schedule_off is True)
    for st in ("CHARGING", "DISCHARGING"):
        r = CA.evaluate(req(st, -5.4, LCRec("discharge" if st == "DISCHARGING" else "charge",
                                            5.0, NOW - 1.0), LC.TRUST_FOR_INTERVAL), D, NOW)
        check(f"★★ S. TTL=None + PCS {st} + 有 LastControl → UNKNOWN / TTL_UNSET / BLOCK",
              r.state == CA.AUTH_UNKNOWN and r.reason == CA.CA_TTL_UNSET
              and r.allowed is False)
    for st in ("STANDBY", "STOPPED"):
        r = CA.evaluate(req(st), D, NOW)
        check(f"★★ S. TTL=None + PCS {st} → 仍為 IDLE（可開始新控制）",
              r.state == CA.AUTH_IDLE and r.allowed is True)
    r = CA.evaluate(req("DISCHARGING", -80.2), D, NOW)
    check("★★ TTL=None 不得與『沒有 LastControl』混為一談（後者仍是 EXTERNAL）",
          r.state == CA.AUTH_EXTERNAL and r.reason == CA.CA_EXTERNAL)
    r = CA.evaluate(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 1.0),
                        LC.TRUST_FOR_INTERVAL),
                    CA.AuthorityPolicy(authority_ttl_sec=120.0), NOW)
    check("★★ tolerance=None（TTL 已設定）→ UNKNOWN / TOLERANCE_UNSET",
          r.state == CA.AUTH_UNKNOWN and r.reason == CA.CA_TOLERANCE_UNSET)
    for bad in (-1.0, float("nan"), float("inf"), "x", True):
        check(f"  ttl={bad!r} 不合法 → TTL_UNSET",
              CA.evaluate(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 1.0),
                              LC.TRUST_FOR_INTERVAL),
                          CA.AuthorityPolicy(authority_ttl_sec=bad), NOW).reason
              == CA.CA_TTL_UNSET)
    check("  tolerance 不合法 → TOLERANCE_UNSET",
          CA.evaluate(req("DISCHARGING", -5.4, LCRec("discharge", 5.0, NOW - 1.0),
                          LC.TRUST_FOR_INTERVAL),
                      CA.AuthorityPolicy(120.0, -1.0), NOW).reason == CA.CA_TOLERANCE_UNSET)

    # ---------------- U ----------------
    print("\nU. 值域 / reason / 常數一致性")
    check("★★ 只有 IDLE 與 OWNED_BY_PHASE6 允許送控制",
          CA.AUTHORITY_ALLOWED_STATES == {CA.AUTH_IDLE, CA.AUTH_OWNED})
    check("★★ EXTERNAL / CONFLICT / UNKNOWN 一律 BLOCK",
          CA.AUTHORITY_BLOCKED_STATES == {CA.AUTH_EXTERNAL, CA.AUTH_CONFLICT, CA.AUTH_UNKNOWN})
    check("  狀態值域封閉（5 個）", len(CA.AUTHORITY_STATES) == 5)
    check("★ allowed 永遠等於 state ∈ ALLOWED_STATES",
          all(CA.evaluate(r_, POL, NOW).allowed
              == (CA.evaluate(r_, POL, NOW).state in CA.AUTHORITY_ALLOWED_STATES)
              for r_ in (req("STANDBY"), req("DISCHARGING", -80.2), req("UNKNOWN"),
                         req("DISCHARGING", -80.2, LCRec(), LC.TRUST_FOR_INTERVAL))))
    check("★★ PCS 狀態集合與 pcs_control_integration 完全一致（防漂移）",
          CA.PCS_RUNNING_STATES == PCI.PCS_STATE_RUNNING
          and CA.PCS_IDLE_STATES == PCI.PCS_STATE_IDLE
          and CA.PCS_UNUSABLE_STATES == PCI.PCS_STATE_UNUSABLE)
    check("  已知 PCS 狀態集合 == PCI.PCS_STATES", CA.KNOWN_PCS_STATES == PCI.PCS_STATES)
    check("★★ 動作常數與 PCI.CTRL_* 一致",
          (CA.ACT_CHARGE, CA.ACT_DISCHARGE, CA.ACT_STOP)
          == (PCI.CTRL_CHARGE, PCI.CTRL_DISCHARGE, PCI.CTRL_STOP))
    check("★★ 信任層級常數與 last_control_store 一致",
          (CA.TRUST_FOR_INTERVAL, CA.TRUST_HISTORY_ONLY, CA.TRUST_INVALID)
          == (LC.TRUST_FOR_INTERVAL, LC.TRUST_HISTORY_ONLY, LC.TRUST_INVALID))
    check("  所有回傳的 reason 都在宣告的集合內",
          all(CA.evaluate(r_, p_, NOW).reason in CA.AUTHORITY_REASONS
              for r_, p_ in ((req("STANDBY"), POL), (req("DISCHARGING", -80.2), POL),
                             (req("STANDBY", mode=MODE_ON), POL), (req("UNKNOWN"), POL),
                             (req("STANDBY", valid=False), POL),
                             (req("DISCHARGING", -5.4, LCRec(), LC.TRUST_FOR_INTERVAL),
                              CA.DEFAULT_AUTHORITY_POLICY))))
    check("★ reason 一律以 CONTROL_AUTHORITY_ 開頭（log 可一眼分辨層級）",
          all(x.startswith("CONTROL_AUTHORITY_") for x in CA.AUTHORITY_REASONS))
    check("  as_dict() 可序列化且非有限浮點轉 None",
          isinstance(ev(req("STANDBY")).as_dict(), dict))

    # ---------------- V ----------------
    print("\nV. 零 I/O 與相依邊界")
    # ⚠️ 不檢查 os / io / ast —— 本測試檔自己就 import 了，快照必然含有，
    #    那不構成 control_authority 的相依證據。真實相依由下方 AST 檢查鎖住。
    for mod in ("requests", "socket", "urllib.request", "json", "csv", "threading",
                "api_client", "charge_discharge_report", "device_control_operator",
                "last_control_store", "pcs_control_integration", "safety_gate",
                "decision_engine", "report_monitor"):
        check(f"  import control_authority 後未載入 {mod}", mod not in _MODULES_AFTER_CA)
    imports = {a.name.split(".")[0] for n in ast.walk(TREE)
               if isinstance(n, ast.Import) for a in n.names} | \
              {n.module.split(".")[0] for n in ast.walk(TREE)
               if isinstance(n, ast.ImportFrom) and n.module}
    check(f"★★ 只依賴標準庫 math / dataclasses：{sorted(imports)}",
          imports <= {"math", "dataclasses"})
    check("★★ 未 import pcs_control_integration（避免循環 import）",
          "pcs_control_integration" not in imports)
    calls = {n.func.id for n in ast.walk(TREE)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    check(f"  無 open / eval / exec / __import__：{sorted(calls & {'open', 'eval', 'exec'})}",
          not (calls & {"open", "eval", "exec", "__import__", "compile"}))
    check("★ 未定義任何 production 功率 / 逾時參數",
          not ({"charge_power_kw", "discharge_power_kw", "max_power_kw",
                "min_switch_interval_sec", "timeout_sec", "poll_interval_sec",
                "stability_samples"}
               & {n.targets[0].id for n in TREE.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}))

    n, total = sum(RESULTS), len(RESULTS)
    print(f"\n== Phase 6.5-H Control Authority {'PASS' if n == total else 'FAIL'}"
          f"（{n}/{total} 檢查通過）==")
    return 0 if n == total else 1


if __name__ == "__main__":
    sys.exit(main())
