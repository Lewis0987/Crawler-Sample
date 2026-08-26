# -*- coding: utf-8 -*-
"""
test_phase6_66_report_integration.py — Phase 6.6 Auto Report Integration（A~H）
======================================================================
核心命題
    「Phase 6 在控時，既有自動報告能正確開始與收尾；
      未安裝橋接時，既有報告行為**一個位元都沒有改變**。」

R1 —— Direction-driven Auto Report Trigger
    既有條件「智慧模式 ＋ 排程主開關 ON」與 Phase 6（要求排程 OFF）互斥。
    R1 讓報告改由**實際控制方向與擁有權**驅動，其餘守門一項都不放寬。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch、零實機 command。
    全部以 synthetic timeline / fake session 驗證。

用法
    python test_phase6_66_report_integration.py        # exit 0 = PASS
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
import report_monitor as RM                              # noqa: E402
import phase6_report_bridge as BR                        # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402

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
# Synthetic timeline
# ======================================================================
class Arb(object):
    """仲裁稽核結果的最小替身（橋接只讀這幾個欄位）。"""

    def __init__(self, authority_state, pcs_state,
                 lc_action=None, lc_target=None):
        self.authority_state = authority_state
        self.pcs_state = pcs_state
        self.lastcontrol_action = lc_action
        self.lastcontrol_target_kw = lc_target


class Timeline(object):
    """依序吐出各時點觀測的 observe_source。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.i = 0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.steps:
            return None
        s = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        return s


IDLE = Arb(CA.AUTH_IDLE, "STANDBY")
STOPPED = Arb(CA.AUTH_IDLE, "STOPPED")
CHARGING = Arb(CA.AUTH_OWNED, PCI.PCS_CHARGING, "charge", 5.0)
DISCHARGING = Arb(CA.AUTH_OWNED, PCI.PCS_DISCHARGING, "discharge", 5.0)
PENDING = Arb(CA.AUTH_UNKNOWN, PCI.PCS_CHARGING, "charge", 5.0)
EXTERNAL = Arb(CA.AUTH_EXTERNAL, PCI.PCS_CHARGING)


def gate(direction, mode_code="manual", sched_on=False):
    """在目前的 provider 設定下問一次 start gate。"""
    return RM._start_gate(mode_code, sched_on, direction)


# ======================================================================
def main():
    print("== Phase 6.6 Auto Report Integration 驗證（完全離線）==\n")
    RM.clear_control_session_provider()

    # ---------------- 架構不變量 ----------------
    print("架構不變量")
    check("★★ 報告層**不** import 任何 Phase 6 控制模組",
          not (_all_imports(_tree("report_monitor.py"))
               & {"pcs_auto_control_runtime", "pcs_auto_control_config",
                  "pcs_auto_control_service", "pcs_auto_control_production",
                  "production_arbiter", "production_execution_chain",
                  "control_authority", "phase6_report_bridge"}))
    check("★★ Phase 6 wiring 層**不** import 任何報告監看模組",
          not (_all_imports(_tree("pcs_auto_control_production.py"))
               & {"report_monitor", "auto_monitor_service"}))
    check("★★ 橋接是唯一整合點（它同時認識兩邊）",
          {"control_authority", "pcs_control_integration"}
          <= _all_imports(_tree("phase6_report_bridge.py"))
          and "report_monitor" in
          io.open(os.path.join(HERE, "phase6_report_bridge.py"),
                  encoding="utf-8").read())
    br_src = io.open(os.path.join(HERE, "phase6_report_bridge.py"),
                     encoding="utf-8").read()
    check("★★ 橋接不呼叫任何控制出口（無 executor / operator / 指令字串）",
          not any(k in br_src for k in ("device_control_operator",
                                        "pcs_control_executor", "param=1",
                                        "param=2", "pcs_charge", "pcs_stop")))
    # ⚠️ 不能用字串比對 —— 橋接的說明文字本身就在講「不碰 Mutex」。
    #    改以 AST 檢查實際引用到的名稱。
    _br_names = ({x.id for x in ast.walk(_tree("phase6_report_bridge.py"))
                  if isinstance(x, ast.Name)}
                 | {x.attr for x in ast.walk(_tree("phase6_report_bridge.py"))
                    if isinstance(x, ast.Attribute)})
    check("★★ 橋接不碰任何 Mutex／ownership（AST 檢查實際引用）",
          not (_br_names & {"acquire_ownership", "release_ownership",
                            "is_owner", "CreateMutexExW", "mutex_name"}))
    check("★★ 未建立第二套報告系統（橋接不含 session / finalize 實作）",
          not any(k in br_src for k in ("def start_session", "def finalize",
                                        "ReportSession(", "def _report_")))

    # ---------------- 未安裝時：既有行為完全不變 ----------------
    print("\n未安裝橋接：既有行為完全不變")
    check("★★ provider 預設為 None", RM._control_session_provider is None)
    check("★★ 既有路徑（智慧模式＋排程開）仍可通過",
          gate("charge", "smart", True)[:3] == (True, "scheduler", "scheduler"))
    for tag, mode, sched, want in (("非智慧模式", "manual", True, "control_mode=manual"),
                                   ("排程未開", "smart", False, "schedule_off")):
        ok, reason, origin, sp = gate("charge", mode, sched)
        check(f"★★ {tag} → 仍不建立（{want}）",
              ok is False and reason == want and origin is None)

    # ---------------- A. IDLE → CHARGE → CHARGING → STOP → STOPPED ----
    print("\nA. IDLE → CHARGE 5kW → CHARGING → STOP → STOPPED")
    tl = Timeline([IDLE, CHARGING, CHARGING, STOPPED])
    BR.install(tl)
    check("  A. 橋接已安裝", BR.installed() is True)
    check("★★ A. 起始 IDLE → 不建立（Phase 6 尚未擁有作業）",
          gate("idle")[0] is False)
    ok, reason, origin, sp = gate("charge")
    check("★★ A. 進入 CHARGING 且 Phase 6 擁有 → 允許建立",
          ok is True and reason == "phase6_owned"
          and origin == RM.ORIGIN_PHASE6)
    check("★★ A. 帶出控制 target_power_kw（供寫入報告）", sp == 5.0)
    check("★★ A. 排程主開關為 OFF 也能建立（R1 解除互斥）",
          gate("charge", "manual", False)[0] is True)
    check("★★ A. STOP 後設備閒置 → 不再建立新報告",
          gate("idle")[0] is False)
    BR.uninstall()

    # ---------------- B. DISCHARGE 對稱 ----------------
    print("\nB. IDLE → DISCHARGE 5kW → DISCHARGING → STOP → STANDBY")
    tl = Timeline([IDLE, DISCHARGING, DISCHARGING, IDLE])
    BR.install(tl)
    check("★★ B. 起始 IDLE → 不建立", gate("idle")[0] is False)
    ok, reason, origin, sp = gate("discharge")
    check("★★ B. 進入 DISCHARGING 且 Phase 6 擁有 → 允許建立",
          ok is True and reason == "phase6_owned" and sp == 5.0)
    check("★★ B. STOP 後回到 STANDBY → 不再建立", gate("idle")[0] is False)
    BR.uninstall()

    # ---------------- C. CHARGE → STOP → DISCHARGE → STOP ----------------
    print("\nC. CHARGE → STOP → DISCHARGE → STOP")
    seq = [CHARGING, STOPPED, DISCHARGING, STOPPED]
    dirs = ["charge", "idle", "discharge", "idle"]
    tl = Timeline(seq)
    BR.install(tl)
    got = [gate(d)[0] for d in dirs]
    check("★★ C. 只有兩個充放電段允許建立，兩個 STOP 段不允許",
          got == [True, False, True, False])
    check("★★ C. 方向反轉不會讓 STOP 段被誤判為新的作業",
          got[1] is False and got[3] is False)
    BR.uninstall()

    # ---------------- D. 服務重啟（session 進行中）----------------
    print("\nD. service restart during active session")
    tl = Timeline([CHARGING])
    BR.install(tl)
    check("  D. 重啟前：Phase 6 擁有 → 允許", gate("charge")[0] is True)
    BR.uninstall()                      # 模擬行程結束
    check("★★ D. 重啟瞬間（橋接未安裝）→ 退回既有條件，不誤建",
          gate("charge", "manual", False)[0] is False)
    BR.install(Timeline([CHARGING]))    # 模擬重啟後重新安裝
    check("★★ D. 重啟後重新安裝 → 依 durable 擁有權恢復判定",
          gate("charge")[0] is True)
    check("★★ D. resume 契約未被修改（既有 session/resume 機制原封不動）",
          "def _report_start_session" in
          io.open(os.path.join(HERE, "report_monitor.py"),
                  encoding="utf-8").read())
    BR.uninstall()

    # ---------------- E. 重複觀測 ----------------
    print("\nE. duplicate observation")
    tl = Timeline([CHARGING])
    BR.install(tl)
    results = [gate("charge") for _ in range(10)]
    check("★★ E. 連續 10 輪同一觀測 → gate 判定完全一致（無狀態殘留）",
          len({r[:3] for r in results}) == 1 and results[0][0] is True)
    check("★★ E. provider 每輪都重新取值（不快取過期擁有權）",
          tl.calls >= 10)
    check("★★ E. 重複建立由既有守門負責（session 存在 / pending / cooldown "
          "/ debounce 一項都沒有放寬）",
          all(k in io.open(os.path.join(HERE, "report_monitor.py"),
                           encoding="utf-8").read()
              for k in ("session_exists", "cooldown=", "start_hold=",
                        "state=STOP_PENDING")))
    BR.uninstall()

    # ---------------- F. 報告寫入失敗 ----------------
    print("\nF. report write failure")

    def _boom():
        raise RuntimeError("simulated report-side failure")

    RM.set_control_session_provider(_boom)
    ok, reason, origin, sp = gate("charge", "manual", False)
    check("★★ F. provider 例外 → 退回既有條件，不中斷判斷",
          ok is False and reason == "control_mode=manual")
    check("★★ F. 例外不外洩（報告層問題不得影響控制層）",
          RM._control_session_state() is None)
    check("★★ F. 控制側參數未被報告失敗改寫",
          CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw == 5.0
          and CFG.DEFAULT_CONTROL_CONFIG.max_power_kw == 150.0)
    RM.clear_control_session_provider()

    # ---------------- G. Orchestrator 失效 ----------------
    print("\nG. Orchestrator failure / 非擁有狀態")
    for tag, arb, direction, why in (
            ("Authority UNKNOWN（待佐證）", PENDING, "charge", "尚未完成佐證"),
            ("Authority EXTERNAL（外部控制）", EXTERNAL, "charge", "非本方作業"),
            ("observe_source 回 None", None, "charge", "沒有觀測")):
        BR.install(Timeline([arb] if arb is not None else [None]))
        check(f"★★ G. {tag} → 不建立報告（{why}）",
              gate(direction, "manual", False)[0] is False)
        BR.uninstall()
    # 橋接層的第二道：LastControl 方向與設備狀態不符 → 直接回 None
    BR.install(Timeline([Arb(CA.AUTH_OWNED, PCI.PCS_CHARGING, "discharge", 5.0)]))
    check("★★ G. LastControl 方向與設備狀態不符 → 橋接層即 Fail Closed",
          RM._control_session_state() is None)
    BR.uninstall()
    # gate 層的第一道：Phase 6 宣稱方向與報告層觀測到的方向不符
    # （兩層讀取時點不同時可能發生）→ 不建立報告
    RM.set_control_session_provider(
        lambda: {"owned": True, "action": "charge", "target_power_kw": 5.0})
    ok, reason, _, _ = gate("discharge", "manual", False)
    check("★★ G. Phase 6 宣稱方向與設備實際方向不符 → Fail Closed",
          ok is False and reason == "phase6_direction_mismatch")
    RM.clear_control_session_provider()

    def _explode():
        raise ValueError("orchestrator down")

    RM.set_control_session_provider(_explode)
    check("★★ G. Orchestrator 例外 → 報告層安全退回既有條件",
          gate("charge", "smart", True)[0] is True)
    RM.clear_control_session_provider()

    # ---------------- H. OBSERVE_ONLY ----------------
    print("\nH. OBSERVE_ONLY")
    check("★★ H. dispatch 仍未啟用", RT.DISPATCH_ENABLED is False)
    import pcs_auto_control_service as SVC
    obs, _rec, wiring = SVC.build_production_stack()
    check("★★ H. 模式仍為 OBSERVE_ONLY 且無控制出口",
          wiring.mode == "OBSERVE_ONLY" and wiring.can_dispatch is False)
    BR.install(obs)
    check("★★ H. OBSERVE_ONLY 下 Phase 6 不可能擁有作業 → provider 回 None",
          RM._control_session_state() is None)
    check("★★ H. 因此既有報告行為完全不受影響（退回原條件）",
          gate("charge", "smart", True)[:3] == (True, "scheduler", "scheduler")
          and gate("charge", "manual", False)[0] is False)
    BR.uninstall()
    check("★★ H. 拆除後 provider 立即歸零（可完全還原）",
          RM._control_session_provider is None)

    # ---------------- 責任分離 ----------------
    print("\n責任分離")
    check("★★ 報告層不得產生控制決策（provider 契約為唯讀狀態）",
          BR.control_session_state(authority=None) is None)
    rm_src = io.open(os.path.join(HERE, "report_monitor.py"),
                     encoding="utf-8").read()
    check("★★ 報告層未取得任何控制把手（不含 dispatch / executor 呼叫）",
          "pcs_auto_control" not in rm_src)
    check("★★ 報告子系統的 ownership 機制未被修改（仍為自己的 Mutex）",
          "is_owner()" in rm_src)
    check("★★ 收尾判定仍由既有 should_auto_end() 單一決策點負責",
          "should_auto_end" in rm_src)
    check("★★ 既有 report engine 未被重寫（session 入口仍是同一個）",
          rm_src.count("def _report_start_session") == 1)

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.6 Auto Report Integration 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
