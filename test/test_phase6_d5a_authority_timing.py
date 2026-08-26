# -*- coding: utf-8 -*-
"""
test_phase6_d5a_authority_timing.py — Blocker 13 Authority Timing Guard（A~L）
======================================================================
核心命題
    「『還沒取得指令後的新功率資料』**不等於**功率衝突，也**不等於**外部控制。
      兩者都不放行 dispatch，但**不得**把 Phase 6 自己剛送出的作業誤標成別人的。」

實機依據（Phase 6.5-G 量測，4 筆完整 leg）
    PCS 充放電旗標與 AC 功率暫存器不保證同步：
    實測 0 個或 1 個 backend refresh cycle 的落差（延遲案例約 15~16 秒）。

必須明確區分的四種情況
    1. 真正的 power conflict          → CONFLICT / CONFLICT_POWER
    2. 指令後 AC power 尚未 refresh   → UNKNOWN / NOT_YET_CORROBORATED
    3. 資料 stale / unavailable       → UNKNOWN / DATA_INSUFFICIENT
    4. 正常                            → OWNED_BY_PHASE6

是否需要設備
    **不需要**。零網路、零登入、零 dispatch、零 sleep。

用法
    python test_phase6_d5a_authority_timing.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import control_authority as CA                          # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import execution_reconciler as RC                        # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import last_control_store as LCS                         # noqa: E402
import pcs_control_executor as EXEC                      # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def _tree(name):
    return ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())


def _all_imports(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


# ======================================================================
# 測試替身（皆為實機量測到的真實數值形態，非虛構）
# ======================================================================
IDLE_AC = -1.3          # STANDBY / STOPPED 時的站用電（實機觀測）
CMD_AT = 1000.0         # LastControl 驗證時刻（monotonic）


class Rec(object):
    """LastControlRecord 的 duck type。"""

    def __init__(self, action, target, at=CMD_AT, power_at_verify=IDLE_AC):
        self.action = action
        self.target_power_kw = target
        self.verified_at_monotonic = at
        self.pcs_actual_state = CA.ACTION_EXPECTED_STATE.get(action)
        self.actual_active_power_kw = power_at_verify


def auth(pcs_state, observed, rec, observed_at=CMD_AT + 5.0, now=CMD_AT + 10.0,
         tol=2.0, ttl=600.0, trust=CA.TRUST_FOR_INTERVAL, ess_valid=True,
         schedule=False):
    return CA.evaluate(
        CA.AuthorityRequest(
            pcs_state=pcs_state, actual_active_power_kw=observed,
            last_control=rec, last_control_trust=trust,
            pcs_mode_state={"schedule_switch": schedule},
            ess_valid=ess_valid, observed_at_monotonic=observed_at),
        policy=CA.AuthorityPolicy(authority_ttl_sec=ttl,
                                  authority_power_tolerance_kw=tol),
        now=now)


class Ess(object):
    def __init__(self, power, valid=True, stale=False):
        self.actual_active_power_kw = power
        self.valid, self.stale = valid, stale


class Obs(object):
    """ControlObservation 的 duck type。"""

    def __init__(self, pcs_state, power, read_at=CMD_AT + 5.0, valid=True,
                 ess_valid=True, stale=False):
        self.pcs_state = pcs_state
        self.ess = Ess(power, ess_valid, stale)
        self.valid = valid
        self.reason = "OK"
        self.read_started_at = read_at
        self.pcs_mode_state = {"schedule_switch": False}


REC_POLICY = CA.AuthorityPolicy(authority_ttl_sec=600.0,
                                authority_power_tolerance_kw=2.0)


def recon(obs, rec, trust=CA.TRUST_FOR_INTERVAL, now=CMD_AT + 10.0):
    return RC.ExecutionReconciler(authority_policy=REC_POLICY).reconcile(
        observation=obs, last_control=rec, trust=trust,
        store_reason=LCS.LOAD_OK, now=now)


# ======================================================================
def main():
    print("== Blocker 13 Authority Timing Guard 驗證（完全離線）==\n")

    # ---------------- A ----------------
    print("A. CHARGE 已送、state=CHARGING、AC 仍是指令前的閒置值")
    a = auth(PCI.PCS_CHARGING, IDLE_AC, Rec("charge", 5.0))
    check("★★ A. 判為 UNKNOWN（不是 CONFLICT）",
          a.state == CA.AUTH_UNKNOWN)
    check("★★ A. reason 為 NOT_YET_CORROBORATED",
          a.reason == CA.CA_NOT_YET_CORROBORATED)
    check("★★ A. **不是** CONFLICT_POWER",
          a.reason != CA.CA_CONFLICT_POWER and a.state != CA.AUTH_CONFLICT)
    check("★★ A. **不是** EXTERNAL", a.state != CA.AUTH_EXTERNAL)
    check("★★ A. 不允許送出指令", a.allowed is False)
    check("★★ A. detail 明說是暫存器未更新、不是功率衝突",
          "尚未更新" in a.detail and "不是**功率衝突" in a.detail)
    check("★★ A. 任何容差都不會改變這個結論（不是容差問題）",
          all(auth(PCI.PCS_CHARGING, IDLE_AC, Rec("charge", 5.0),
                   tol=t).reason == CA.CA_NOT_YET_CORROBORATED
              for t in (0.1, 0.5, 1.0, 2.0, 3.0, 4.0, 10.0)))

    # ---------------- B ----------------
    print("\nB. DISCHARGE 對稱案例")
    b = auth(PCI.PCS_DISCHARGING, IDLE_AC, Rec("discharge", 5.0))
    check("★★ B. UNKNOWN / NOT_YET_CORROBORATED",
          b.state == CA.AUTH_UNKNOWN
          and b.reason == CA.CA_NOT_YET_CORROBORATED)
    check("★★ B. 不是 CONFLICT、不是 EXTERNAL、不允許",
          b.state not in (CA.AUTH_CONFLICT, CA.AUTH_EXTERNAL)
          and b.allowed is False)
    check("★★ B. 任何容差皆同",
          all(auth(PCI.PCS_DISCHARGING, IDLE_AC, Rec("discharge", 5.0),
                   tol=t).reason == CA.CA_NOT_YET_CORROBORATED
              for t in (0.1, 1.0, 3.0, 10.0)))

    # ---------------- C ----------------
    print("\nC. 指令後取得新觀測、方向正確、幅度在容差內")
    for tag, st, act, obs in (("充電", PCI.PCS_CHARGING, "charge", 5.4),
                              ("放電", PCI.PCS_DISCHARGING, "discharge", -5.4)):
        c = auth(st, obs, Rec(act, 5.0))
        check(f"★★ C. {tag} → OWNED_BY_PHASE6",
              c.state == CA.AUTH_OWNED and c.reason == CA.CA_PHASE6
              and c.allowed is True)
    check("★★ C. 佐證通過後，連很小的容差也成立（0.4 kW 偏差）",
          auth(PCI.PCS_CHARGING, 5.4, Rec("charge", 5.0),
               tol=0.5).state == CA.AUTH_OWNED)

    # ---------------- D ----------------
    print("\nD. 指令後取得新觀測、方向錯誤")
    d = auth(PCI.PCS_CHARGING, -5.4, Rec("charge", 5.0))
    check("★★ D. 判為 CONFLICT / CONFLICT_POWER",
          d.state == CA.AUTH_CONFLICT and d.reason == CA.CA_CONFLICT_POWER)
    check("★★ D. detail 明指方向錯誤（非幅度問題）", "方向錯誤" in d.detail)
    d2 = auth(PCI.PCS_DISCHARGING, 5.4, Rec("discharge", 5.0))
    check("★★ D. 放電方向相反亦為 CONFLICT_POWER",
          d2.state == CA.AUTH_CONFLICT and d2.reason == CA.CA_CONFLICT_POWER)
    check("★★ D. 方向錯誤不因容差放大而變成 OWNED",
          auth(PCI.PCS_CHARGING, -5.4, Rec("charge", 5.0),
               tol=100.0).state == CA.AUTH_CONFLICT)

    # ---------------- E ----------------
    print("\nE. 指令後取得新觀測、方向正確但幅度超出容差")
    e = auth(PCI.PCS_CHARGING, 9.9, Rec("charge", 5.0), tol=2.0)
    check("★★ E. 判為 CONFLICT / CONFLICT_POWER",
          e.state == CA.AUTH_CONFLICT and e.reason == CA.CA_CONFLICT_POWER)
    check("★★ E. 幅度差距寫入 detail", "相差" in e.detail)
    check("★★ E. 容差邊界：恰好等於容差 → 仍為 OWNED（> 才算超出）",
          auth(PCI.PCS_CHARGING, 7.0, Rec("charge", 5.0),
               tol=2.0).state == CA.AUTH_OWNED)

    # ---------------- F ----------------
    print("\nF. 舊 observation 即使數值剛好符合，也不得當成新的佐證")
    f1 = auth(PCI.PCS_CHARGING, 5.0, Rec("charge", 5.0, power_at_verify=5.0))
    check("★★ F. 值未離開驗證當下的基準 → NOT_YET_CORROBORATED",
          f1.state == CA.AUTH_UNKNOWN
          and f1.reason == CA.CA_NOT_YET_CORROBORATED)
    f2 = auth(PCI.PCS_CHARGING, 5.4, Rec("charge", 5.0),
              observed_at=CMD_AT - 1.0)
    check("★★ F. 觀測時間早於指令驗證 → NOT_YET_CORROBORATED（即使數值吻合）",
          f2.state == CA.AUTH_UNKNOWN
          and f2.reason == CA.CA_NOT_YET_CORROBORATED
          and "之前" in f2.detail)
    f3 = auth(PCI.PCS_CHARGING, 5.4, Rec("charge", 5.0),
              observed_at=CMD_AT)
    check("★★ F. 觀測時間與指令驗證同時 → 仍不放行（必須嚴格晚於）",
          f3.reason == CA.CA_NOT_YET_CORROBORATED)
    f4 = auth(PCI.PCS_CHARGING, 5.4,
              Rec("charge", 5.0, power_at_verify=None))
    check("★★ F. LastControl 未保存驗證當下功率 → 無從證明新舊 → 不放行",
          f4.reason == CA.CA_NOT_YET_CORROBORATED)

    # ---------------- G ----------------
    print("\nG. 資料 stale / unavailable")
    g = auth(PCI.PCS_CHARGING, 5.4, Rec("charge", 5.0), ess_valid=False)
    check("★★ G. ESS 快照無效／不新鮮 → UNKNOWN",
          g.state == CA.AUTH_UNKNOWN
          and g.reason == CA.CA_DATA_INSUFFICIENT)
    check("★★ G. 與 NOT_YET_CORROBORATED 是不同 reason（四態未被壓成兩態）",
          g.reason != CA.CA_NOT_YET_CORROBORATED)
    check("★★ G. 不允許 dispatch", g.allowed is False)
    gobs = Obs(PCI.PCS_CHARGING, IDLE_AC, valid=False)
    check("★★ G. recovery 端觀測無效 → 不宣稱擁有權",
          recon(gobs, Rec("charge", 5.0)).outcome not in RC.OWNED_OUTCOMES)

    # ---------------- H ----------------
    print("\nH. 服務重啟落在 AC 更新之前")
    h = recon(Obs(PCI.PCS_CHARGING, IDLE_AC), Rec("charge", 5.0))
    check("★★ H. **不得**判成 EXTERNAL_CONTROL",
          h.runtime_state != RT.ST_EXTERNAL_CONTROL
          and h.outcome != RC.REC_EXTERNAL)
    check("★★ H. outcome 為 PENDING_CORROBORATION",
          h.outcome == RC.REC_PENDING)
    check("★★ H. runtime_state 為 OWNERSHIP_PENDING",
          h.runtime_state == RT.ST_OWNERSHIP_PENDING)
    check("★★ H. 不宣稱擁有權", h.outcome not in RC.OWNED_OUTCOMES)
    check("★★ H. 不得 dispatch", h.may_dispatch is False)
    check("★★ H. Authority reason 保留為 NOT_YET_CORROBORATED（可稽核）",
          h.authority_reason == CA.CA_NOT_YET_CORROBORATED)
    check("★★ H. 放電方向對稱",
          recon(Obs(PCI.PCS_DISCHARGING, IDLE_AC),
                Rec("discharge", 5.0)).runtime_state
          == RT.ST_OWNERSHIP_PENDING)
    check("★★ H. OWNERSHIP_PENDING 結構上不可能送指令"
          "（不在 EXECUTION／DISPATCH 集合內）",
          RT.ST_OWNERSHIP_PENDING in RT.RUNTIME_STATES
          and RT.ST_OWNERSHIP_PENDING not in RT.EXECUTION_STATES
          and RT.ST_OWNERSHIP_PENDING not in RT.DISPATCH_STATES)

    # ---------------- I ----------------
    print("\nI. 重啟後取得新觀測且吻合")
    i1 = recon(Obs(PCI.PCS_CHARGING, 5.4), Rec("charge", 5.0))
    check("★★ I. 恢復 OWNED_BY_PHASE6",
          i1.authority_state == CA.AUTH_OWNED and i1.outcome == RC.REC_OWNED)
    check("★★ I. runtime_state 為 OWNED_CHARGE",
          i1.runtime_state == "OWNED_CHARGE")
    i2 = recon(Obs(PCI.PCS_DISCHARGING, -5.4), Rec("discharge", 5.0))
    check("★★ I. 放電對稱 → OWNED_DISCHARGE",
          i2.outcome == RC.REC_OWNED and i2.runtime_state == "OWNED_DISCHARGE")
    check("★★ I. 但 recovery 仍不得 dispatch",
          i1.may_dispatch is False and i2.may_dispatch is False)

    # ---------------- J ----------------
    print("\nJ. 真正的外部控制仍必須可辨識")
    j1 = auth(PCI.PCS_CHARGING, 5.4, None)
    check("★★ J. 運轉中但無 LastControl → EXTERNAL",
          j1.state == CA.AUTH_EXTERNAL and j1.reason == CA.CA_EXTERNAL)
    j2 = auth(PCI.PCS_CHARGING, 5.4, Rec("charge", 5.0),
              trust=CA.TRUST_HISTORY_ONLY)
    check("★★ J. 信任度不足 → EXTERNAL",
          j2.state == CA.AUTH_EXTERNAL
          and j2.reason == CA.CA_TRUST_INSUFFICIENT)
    j3 = auth(PCI.PCS_CHARGING, 5.4, Rec("stop", None))
    check("★★ J. 上次命令是 stop 但設備在運轉 → EXTERNAL",
          j3.state == CA.AUTH_EXTERNAL)
    j4 = auth(PCI.PCS_CHARGING, 5.4, Rec("discharge", 5.0))
    check("★★ J. 動作與狀態矛盾 → CONFLICT_STATE（非 timing guard）",
          j4.state == CA.AUTH_CONFLICT
          and j4.reason == CA.CA_CONFLICT_STATE)
    j5 = auth(PCI.PCS_CHARGING, 5.4, Rec("charge", 5.0), schedule=1)
    check("★★ J. PCS 原生排程 ON → EXTERNAL（timing guard 未削弱此判定）",
          j5.state == CA.AUTH_EXTERNAL
          and j5.reason == CA.CA_SCHEDULE_ACTIVE)
    check("★★ J. recovery 端真外部控制仍為 EXTERNAL_CONTROL",
          recon(Obs(PCI.PCS_CHARGING, 5.4), None).runtime_state
          == RT.ST_EXTERNAL_CONTROL)

    # ---------------- K ----------------
    print("\nK. UNKNOWN / WAITING 不得被當成允許")
    for st in (CA.AUTH_UNKNOWN, CA.AUTH_CONFLICT, CA.AUTH_EXTERNAL):
        check(f"★★ K. {st} 不在 AUTHORITY_ALLOWED_STATES",
              st not in CA.AUTHORITY_ALLOWED_STATES)
    a2 = auth(PCI.PCS_CHARGING, IDLE_AC, Rec("charge", 5.0))
    check("★★ K. NOT_YET_CORROBORATED 的 allowed 為 False", a2.allowed is False)

    class _Charge(object):
        action, target_power_kw, valid = "charge", 5.0, True

    class _EssObj(object):
        valid, stale, soc_percent = True, False, 50.0
        pcs_state = PCI.PCS_CHARGING

    ctrl = PCI.build_control_request(_Charge(), ess=_EssObj(), authority=a2)
    check("★★ K. 送進控制請求建構 → 不產生任何控制動作",
          ctrl.action == PCI.CTRL_NONE)
    check("★★ K. 且理由保留為 NOT_YET_CORROBORATED（不被改寫成衝突）",
          ctrl.reason == CA.CA_NOT_YET_CORROBORATED)
    check("★★ K. Authority 模組不 import 任何 executor / operator / 網路",
          not (_all_imports(_tree("control_authority.py"))
               & {"pcs_control_executor", "device_control_operator",
                  "requests", "urllib", "http", "socket"}))
    check("★★ K. Authority 評估為純函式（無 I/O、無 sleep）",
          not any(k in io.open(os.path.join(HERE, "control_authority.py"),
                               encoding="utf-8").read()
                  for k in ("time.sleep", "open(", "urlopen")))

    # ---------------- L ----------------
    print("\nL. dry-run 行為不變")
    check("★★ L. Executor 預設仍為 dry-run（execute 需明示開啟）",
          EXEC.DEFAULT_READBACK_CONFIG.ready is False)
    check("★★ L. Reconciler 的 may_dispatch 恆為 False",
          all(r.may_dispatch is False for r in (
              h, i1, i2, recon(Obs(PCI.PCS_CHARGING, 5.4), None))))
    check("★★ L. Reconciler 不 import executor / operator",
          not (_all_imports(_tree("execution_reconciler.py"))
               & {"pcs_control_executor", "device_control_operator",
                  "production_execution_chain"}))
    check("★★ L. runtime 仍預設 dispatch 關閉",
          RT.DISPATCH_ENABLED is False)

    # ---------------- 禁止做法 ----------------
    print("\n禁止做法檢查")
    ca_src = io.open(os.path.join(HERE, "control_authority.py"),
                     encoding="utf-8").read()
    rc_src = io.open(os.path.join(HERE, "execution_reconciler.py"),
                     encoding="utf-8").read()

    def _module_numbers(name):
        """
        判定邏輯中的數值常數。

        ⚠️ 刻意排除 main() —— 那是離線示範用的情境資料（會出現 5.0 kW、
           -80.2 kW 等實機情境值），不是判定邏輯的門檻。
        """
        tree = _tree(name)
        demo = {n for fn in tree.body
                if isinstance(fn, ast.FunctionDef) and fn.name == "main"
                for n in ast.walk(fn)}
        out = []
        for n in ast.walk(tree):
            if n in demo:
                continue
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                    and not isinstance(n.value, bool):
                out.append(float(n.value))
        return out

    nums = _module_numbers("control_authority.py")
    check("★★ 無 hard-code 的 refresh 週期秒數（15/15.x/16/16.x）",
          not any(15.0 <= v <= 17.0 for v in nums))
    check("★★ 無 hard-code 的 5 kW",
          5.0 not in nums)
    check("★★ 無任何 sleep（不得阻塞 orchestrator）",
          "sleep" not in ca_src and "sleep" not in rc_src)
    check("★★ 未以放寬容差繞過（容差仍由政策注入，模組不含預設值）",
          CA.AuthorityPolicy().authority_power_tolerance_kw is None)
    check("★★ 未移除功率佐證（容差未設定仍為 UNKNOWN）",
          auth(PCI.PCS_CHARGING, 5.4, Rec("charge", 5.0),
               tol=None).reason == CA.CA_TOLERANCE_UNSET)
    check("★★ timing guard 不依賴 decision_interval_sec",
          "decision_interval" not in ca_src)
    check("★★ 判定依據為 freshness / chronology，不是固定 timer",
          "observed_at_monotonic" in ca_src
          and "verified_at_monotonic" in ca_src)
    check("★★ 四種情況各有獨立 reason code（未被壓成同一個）",
          len({CA.CA_CONFLICT_POWER, CA.CA_NOT_YET_CORROBORATED,
               CA.CA_DATA_INSUFFICIENT, CA.CA_PHASE6}) == 4
          and CA.CA_NOT_YET_CORROBORATED in CA.AUTHORITY_REASONS)
    check("★★ Control Authority 安全性未降低："
          "允許狀態集合仍只有 IDLE 與 OWNED",
          CA.AUTHORITY_ALLOWED_STATES == frozenset({CA.AUTH_IDLE,
                                                    CA.AUTH_OWNED}))
    check("★★ 控制參數仍全部維持 None（本階段未填值）",
          CA.AuthorityPolicy().authority_ttl_sec is None)

    ok_all = all(RESULTS)
    print(f"\n== Blocker 13 Authority Timing Guard 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
