# -*- coding: utf-8 -*-
"""
test_phase6_d3f_reconciliation.py — Phase D.3-F Crash / Restart Reconciliation（A~AH）
======================================================================
核心命題
    「行程中途死亡後，只有 durable 的**已驗證**證據能重新宣稱擁有權。」

四個 Crash Window（以真實檔案 + 真實 store 模擬「重啟」）
    A  授權已消費 → POST 前崩潰
    B  POST 已送出 → 回讀前崩潰          ← 最重要
    C  回讀已成功 → 紀錄提交前崩潰        ← 決定 journal 是否有價值
    D  紀錄已提交 → runtime 狀態更新前崩潰 ← 應可完整自動恢復

🔴 「重啟」在本檔的模擬方式：**丟棄所有記憶體物件，只留下磁碟上的 LastControl 檔案
   與設備當下的旗標**，再以全新的 store / reconciler 重新判定。
   這正是重啟後真實可用的東西 —— 記憶體中的「曾經驗證成功」不存在。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d3f_reconciliation.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import decision_engine as DE                             # noqa: E402
import safety_gate as SG                                 # noqa: E402
import control_authority as CA                           # noqa: E402
import pcs_control_integration as PCI                    # noqa: E402
import pcs_control_executor as EX                        # noqa: E402
import last_control_store as LCS                         # noqa: E402
import ess_snapshot_adapter as EA                        # noqa: E402
import execution_reconciler as REC                       # noqa: E402
import production_execution_chain as PEC                 # noqa: E402
import production_arbiter as ARB                         # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402

import test_phase6_d3d_arbitration as D3D                # noqa: E402
import test_phase6_d3e_execution as D3E                  # noqa: E402

RESULTS = []
_TMP = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


TEST_POLICY = CA.AuthorityPolicy(authority_ttl_sec=120.0,
                                 authority_power_tolerance_kw=1.0)


def tmpdir():
    d = tempfile.mkdtemp(prefix="d3f_")
    _TMP.append(d)
    return d


def observe(pcs="STANDBY", power=None, schedule=False, manual=1, **over):
    """建立一份 fresh 觀測（重啟後重新讀到的東西）。"""
    r = dict(D3D.ess_reading(pcs))
    if power is not None:
        r["actual_active_power_kw"] = power
    r["pcs_schedule_enabled"] = schedule
    r["pcs_manual_switch"] = manual
    r.update(over)
    clk = {"t": 5000.0}
    return EA.EssSnapshotAdapter(reader=lambda: r,
                                 clock=lambda: clk["t"]).observe()


def committed_store(action="charge", power=5.0, observed=5.1, state="CHARGING",
                    at=None, boot=None, power_at_verify=-1.3):
    """
    以**真實 store + 真實檔案**寫入一筆已驗證紀錄，再以**全新 store 實例**載入
    —— 模擬「重啟後只剩磁碟證據」。回傳 (fresh_store, trust_result)。
    """
    d = tmpdir()
    p = os.path.join(d, "last_control.json")
    clk = (lambda: 5000.0) if at is None else (lambda: at)
    # 🔁 Blocker 13：指令驗證必須發生在「重啟後重新讀取」之前。
    #    預設情境原本兩者同為 5000.0，Authority 會（正確地）判定觀測不晚於指令。
    #    這裡把寫入時刻往前挪，讓時序符合真實的「先下指令、後重啟讀取」。
    write_clk = (lambda: 4990.0) if at is None else clk
    w = LCS.LastControlStore(path=p, clock=write_clk,
                             boot_id_provider=(boot or (lambda: "BOOT-1")))
    # 🔁 Blocker 13：read-back 成功當下的 AC 功率，實機上通常**還是指令前的閒置值**
    #    （暫存器比旗標慢 0~1 個 refresh cycle）。原本這裡填的是設備穩定後的功率，
    #    與實機不符，會讓「暫存器尚未更新」這個真實中間狀態測不出來。
    rb = EX.ReadBackResult(EX.VERIFY_SUCCESS, EX.R_TARGET_STATE_REACHED,
                           action, state, state, power_at_verify)
    vr = EX.VerifiedControlResult(action, power, EX.VERIFY_SUCCESS,
                                  EX.R_TARGET_STATE_REACHED,
                                  readback_result=rb, lastcontrol_eligible=True)
    upd = w.update_from_verified_result(vr)
    assert upd.outcome == LCS.UPD_UPDATED, upd
    return p, clk


def restart_store(path, clock, boot="BOOT-1"):
    """全新的 store 實例（無 _memo）＝ 重啟後的世界。"""
    s = LCS.LastControlStore(path=path, clock=clock,
                             boot_id_provider=(lambda: boot))
    t = s.current()
    return s, t


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
def main():
    print("== Phase D.3-F Crash / Restart Reconciliation 驗證（完全離線）==\n")
    rc = REC.ExecutionReconciler(authority_policy=TEST_POLICY)

    # ---------------- A. Window A ----------------
    print("A. Window A：授權已消費 → POST 前崩潰")
    # durable 世界：沒有任何新紀錄；設備未被改變（仍待機）
    r = rc.reconcile(observe("STANDBY"), None, None, now=5000.0)
    check("★★ A. 重啟後：無紀錄 + 設備閒置 → RECOVER_IDLE（可重新開始）",
          r.outcome == REC.REC_IDLE and r.runtime_state == "IDLE"
          and r.owned is False)
    rig = D3D.Rig(pcs="STANDBY", soc=30.0).settle()
    check("★★ A. 重啟後可重新建立新授權（舊的 in-memory 票消失不造成永久 pending）",
          rig.arbiter.evaluate().authorized is True)
    check("  A. 且不是沿用舊票（新票 id 由新的序號產生）",
          rig.arbiter.pending is not None
          and rig.arbiter.pending.state == "ISSUED")

    # ---------------- B. Window B ----------------
    print("\nB. Window B：POST 已送出 → 回讀前崩潰（最重要）")
    # durable 世界：沒有 verified 紀錄；設備**可能**已經在充電
    for pcs, tag in (("CHARGING", "指令已生效"), ("STANDBY", "指令未生效")):
        r = rc.reconcile(observe(pcs, power=5.1), None, None, now=5000.0)
        if pcs == "CHARGING":
            check(f"★★ B. 重啟後設備在 {pcs}（{tag}）但無紀錄 → 絕不 RECOVER_OWNED",
                  r.outcome == REC.REC_EXTERNAL and r.owned is False
                  and r.authority_state == CA.AUTH_EXTERNAL)
        else:
            check(f"★★ B. 重啟後設備 {pcs}（{tag}）→ RECOVER_IDLE，不重送指令",
                  r.outcome == REC.REC_IDLE and r.owned is False)
    check("★★ B. 判定結果不含任何指令建議（reconciler 不可能 dispatch）",
          r.may_dispatch is False)

    # ---------------- C. Window C ----------------
    print("\nC. Window C：回讀已成功 → 紀錄提交前崩潰")
    # 🔴 durable 世界與 Window B **完全相同** —— 記憶體中的 VERIFY_SUCCESS 已不存在
    rb = rc.reconcile(observe("CHARGING", power=5.1), None, None, now=5000.0)
    rcc = rc.reconcile(observe("CHARGING", power=5.1), None, None, now=5000.0)
    check("★★ C. durable 世界與 Window B 完全相同（無法區分）",
          (rb.outcome, rb.authority_state) == (rcc.outcome, rcc.authority_state))
    check("★★ C. 因此仍不得 RECOVER_OWNED（不因『理論上曾驗證成功』而認領）",
          rcc.outcome == REC.REC_EXTERNAL and rcc.owned is False)

    # ---------------- D/E/F/G. Window D ----------------
    print("\nD~G. Window D：紀錄已提交 → runtime 狀態更新前崩潰")
    cases = [("E. CHARGE", "charge", 5.0, 5.1, "CHARGING", REC.REC_OWNED,
              "OWNED_CHARGE"),
             ("F. DISCHARGE", "discharge", 5.0, -5.1, "DISCHARGING",
              REC.REC_OWNED, "OWNED_DISCHARGE")]
    for tag, act, tgt, obs, pcs, want, rstate in cases:
        p, clk = committed_store(act, tgt, obs, pcs)
        s, t = restart_store(p, clk)
        check(f"  {tag} 重啟後載入紀錄成功且信任度足夠",
              t.trust == LCS.TRUST_FOR_INTERVAL and t.record.action == act)
        r = rc.reconcile(observe(pcs, power=obs), t.record, t.trust, now=5000.0)
        check(f"★★ D/{tag}. 紀錄有效 + 現況相符 → RECOVER_OWNED",
              r.outcome == want and r.owned is True
              and r.runtime_state == rstate
              and r.authority_state == CA.AUTH_OWNED)
        check(f"  {tag} reason 明示為 durable 已驗證證據",
              r.reason == REC.R_DURABLE_EVIDENCE_OK)

    # G. STOP → RECOVER_IDLE
    p, clk = committed_store("stop", None, -1.3, "STOPPED")
    s, t = restart_store(p, clk)
    check("  G. STOP 紀錄的 target 一律 None（既有契約）",
          t.record.action == LCS.ACT_STOP and t.record.target_power_kw is None)
    for pcs in ("STOPPED", "STANDBY"):
        r = rc.reconcile(observe(pcs, power=-1.3), t.record, t.trust, now=5000.0)
        check(f"★★ G. 最後一筆為 STOP + 設備 {pcs} → RECOVER_IDLE（非 OWNED_STOP）",
              r.outcome == REC.REC_IDLE and r.runtime_state == "IDLE"
              and r.owned is False)
    check("★★ G. 不會形成 STOP loop（IDLE 決策 + 已閒置 → 不需要 leg）",
          D3D.Rig(pcs="STOPPED", soc=50.0, local_now=D3D.DT_PEAK,
                  meter_kw=-9.0).settle().arbitrate().outcome
          == ARB.ARB_NO_CONTROL)

    # ---------------- H/I. 無紀錄 ----------------
    print("\nH/I. 無 LastControl + 設備運轉中")
    for pcs, power in (("CHARGING", 5.1), ("DISCHARGING", -5.1)):
        r = rc.reconcile(observe(pcs, power=power), None, None, now=5000.0)
        check(f"★★ H/I. 無紀錄 + {pcs} → EXTERNAL（不得認領）",
              r.outcome == REC.REC_EXTERNAL and r.owned is False
              and r.authority_reason == CA.CA_EXTERNAL)

    # ---------------- J/K. 過期與 boot 信任 ----------------
    print("\nJ/K. 紀錄過期與 boot 信任")
    p, clk = committed_store("charge", 5.0, 5.1, "CHARGING", at=1000.0)
    s, t = restart_store(p, clk)
    r = rc.reconcile(observe("CHARGING", power=5.1), t.record, t.trust,
                     now=1000.0 + TEST_POLICY.authority_ttl_sec + 10.0)
    check("★★ J. 紀錄已超過控制權有效期 → 不得 RECOVER_OWNED",
          r.owned is False and r.authority_state == CA.AUTH_UNKNOWN
          and r.authority_reason == CA.CA_EXPIRED)
    p2, clk2 = committed_store("charge", 5.0, 5.1, "CHARGING")
    s2, t2 = restart_store(p2, clk2, boot="BOOT-2")     # OS 已重開機
    check("★★ K. OS reboot（boot id 不同）→ 信任度降為 HISTORY_ONLY",
          t2.trust == LCS.TRUST_HISTORY_ONLY
          and t2.reason == LCS.R_BOOT_ID_MISMATCH)
    r = rc.reconcile(observe("CHARGING", power=5.1), t2.record, t2.trust,
                     now=5000.0)
    check("★★ K. 因此 OS reboot 後不得 RECOVER_OWNED",
          r.owned is False and r.authority_state == CA.AUTH_EXTERNAL
          and r.authority_reason == CA.CA_TRUST_INSUFFICIENT)
    s3, t3 = restart_store(p2, clk2, boot=None)
    check("  K. boot id 無法取得 → 同樣只能 HISTORY_ONLY",
          t3.trust == LCS.TRUST_HISTORY_ONLY)
    check("★★ 10. service restart（未 reboot）→ 仍為 TRUSTED_FOR_INTERVAL",
          restart_store(p2, clk2)[1].trust == LCS.TRUST_FOR_INTERVAL)

    # ---------------- L/M. 狀態與功率不符 ----------------
    print("\nL/M. 紀錄與現況不符")
    p, clk = committed_store("charge", 5.0, 5.1, "CHARGING")
    s, t = restart_store(p, clk)
    r = rc.reconcile(observe("DISCHARGING", power=-5.1), t.record, t.trust,
                     now=5000.0)
    check("★★ L. 紀錄為 charge 但設備在放電 → CONFLICT，不得認領",
          r.outcome == REC.REC_CONFLICT and r.owned is False
          and r.authority_reason == CA.CA_CONFLICT_STATE)
    r = rc.reconcile(observe("CHARGING", power=30.0), t.record, t.trust,
                     now=5000.0)
    check("★★ M. 功率與紀錄明顯不符 → CONFLICT，不得認領",
          r.outcome == REC.REC_CONFLICT and r.owned is False
          and r.authority_reason == CA.CA_CONFLICT_POWER)

    # ---------------- N/O. 接管 ----------------
    print("\nN/O. 重啟後發現已被接管")
    r = rc.reconcile(observe("CHARGING", power=5.1, schedule=True),
                     t.record, t.trust, now=5000.0)
    check("★★ N. 排程主開關已開啟 → EXTERNAL / SCHEDULE_ACTIVE，不得認領",
          r.outcome == REC.REC_EXTERNAL and r.owned is False
          and r.authority_reason == CA.CA_SCHEDULE_ACTIVE)
    r = rc.reconcile(observe("DISCHARGING", power=-7.7), t.record, t.trust,
                     now=5000.0)
    check("★★ O. 人工接管（方向已被改變）→ 不得認領",
          r.owned is False and r.authority_state == CA.AUTH_CONFLICT)

    # ---------------- P. restart + same target ----------------
    print("\nP. 重啟後相同目標 → duplicate suppressed（不重送）")
    for pcs, act, dt, soc, kw, tag in (
            ("CHARGING", "charge", D3D.DT_OFF_PEAK, 30.0, 5.2, "P. CHARGE"),
            ("DISCHARGING", "discharge", D3D.DT_PEAK, 80.0, -5.2, "P. DISCHARGE")):
        # ⚠️ 紀錄的 monotonic 必須早於 Rig 的仲裁時刻（Rig 由 1000.0 起算），
        #    否則 Authority 會以「紀錄時間在未來」判為時間基準異常。
        p, clk = committed_store(act, 5.0, kw, pcs, at=1000.0)
        s, t = restart_store(p, clk)
        op = D3E.fake_operator()
        rig = D3D.Rig(pcs=pcs, soc=soc, local_now=dt,
                      ess_over={"actual_active_power_kw": kw},
                      last_control=t.record).settle()
        rig.trust = t.trust
        ch = PEC.ProductionExecutionChain(
            arbiter=rig.arbiter,
            executor=EX.PcsControlExecutor(operator_run=op, execute=True),
            verifier=D3E.make_verifier([D3E.flags(pcs, kw)]), store=s,
            clock=rig.dec_clk)
        res = ch.run()
        check(f"★★ {tag}. 重啟後相同目標 → 抑制；Executor 0 / 寫入 0",
              res.executed is False and len(op.calls) == 0
              and ch.dispatch_count == 0 and ch.lastcontrol_write_count == 0
              and res.arbitration.reason == ARB.R_ALREADY_AT_DESIRED_STATE)

    # ---------------- Q. restart + 反向 ----------------
    print("\nQ. 重啟後反向決策 → 只能形成 STOP candidate")
    p, clk = committed_store("charge", 5.0, 5.2, "CHARGING", at=1000.0)
    s, t = restart_store(p, clk)
    rig = D3D.Rig(pcs="CHARGING", soc=80.0, local_now=D3D.DT_PEAK,
                  ess_over={"actual_active_power_kw": 5.2},
                  last_control=t.record).settle()
    rig.trust = t.trust
    a = rig.arbitrate()
    check("★★ Q. 重啟後不得直接反向；只授權 STOP",
          a.control_action == PCI.CTRL_STOP
          and a.direction_reason == ARB.R_STOP_REQUIRED_FOR_REVERSAL)

    # ---------------- R/S. 未記錄 / 結果不明 後重啟 ----------------
    print("\nR/S. OWNERSHIP_UNRECORDED 與 COMMAND_OUTCOME_UNKNOWN 之後重啟")
    c = D3E.Chain(verify_states=[D3E.flags("CHARGING", 5.1)],
                  revalidate=D3E.ALWAYS_OK, store=D3E.temp_store(broken=True))
    er = c.run()
    check("  前提：回讀成功但寫入失敗 → OWNERSHIP_UNRECORDED",
          er.outcome == PEC.EXEC_OWNERSHIP_UNRECORDED)
    r = rc.reconcile(observe("CHARGING", power=5.1), None, None, now=5000.0)
    check("★★ R. 重啟後只看得到「運轉中 + 無紀錄」→ EXTERNAL，不得自動認領",
          r.outcome == REC.REC_EXTERNAL and r.owned is False)
    c2 = D3E.Chain(verify_states=[D3E.flags("STANDBY")], revalidate=D3E.ALWAYS_OK)
    er2 = c2.run()
    check("  前提：POST 接受但回讀逾時 → COMMAND_OUTCOME_UNKNOWN",
          er2.outcome == PEC.EXEC_OUTCOME_UNKNOWN)
    check("★★ S. 該情境不會寫入任何紀錄 → 重啟後同樣不得認領",
          c2.store.load().outcome == LCS.LOAD_NOT_FOUND
          and rc.reconcile(observe("CHARGING", power=5.1), None, None,
                           now=5000.0).owned is False)
    check("★★ R/S. 重啟後不得自動重送、不得自動 STOP（reconciler 無此能力）",
          r.may_dispatch is False and REC.OWNED_OUTCOMES == {REC.REC_OWNED})

    # ---------------- T. Runtime 記憶體不得建立擁有權 ----------------
    print("\nT. Runtime 記憶體狀態不是擁有權來源")
    check("★★ T. 全新 Runtime 一律從 DISABLED 起始（不預設 OWNED）",
          RT.AutoControlRuntime().state == RT.ST_DISABLED
          and RT.AutoControlRuntime(dispatch_enabled=True).state == RT.ST_DISABLED)
    rt = RT.AutoControlRuntime(dispatch_enabled=True)
    rt.state = RT.ST_OWNED_CHARGE                      # 假裝上一輪是 OWNED
    check("★★ T. 即使記憶體殘留 OWNED，下一輪仍完全由 fresh 觀測重新推導",
          rt.tick(RT.CycleInputs(is_owner=False)).state == RT.ST_DISABLED)
    check("★★ T. reconciler 的輸入不含任何 runtime 記憶體狀態",
          "runtime" not in {a for a in
                            REC.ExecutionReconciler.reconcile.__code__.co_varnames[:5]})

    # ---------------- U~Y. 零能力 ----------------
    print("\nU~Y. reconciliation 不得 dispatch、不得寫入")
    imps = _all_imports(_tree("execution_reconciler.py"))
    check("★★ V/W/X. reconciler 未 import executor / operator / API 路徑",
          not (imps & {"pcs_control_executor", "device_control_operator",
                       "api_client", "device_control_scraper", "requests",
                       "production_execution_chain", "charge_discharge_report"}))
    names = ({n.id for n in ast.walk(_tree("execution_reconciler.py"))
              if isinstance(n, ast.Name)}
             | {n.attr for n in ast.walk(_tree("execution_reconciler.py"))
                if isinstance(n, ast.Attribute)})
    check("★★ U. reconciler 無任何送出/寫入的呼叫",
          not (names & {"send", "post", "run", "update_from_verified_result",
                        "execute_and_verify", "save"}))
    lits = {n.value for n in ast.walk(_tree("execution_reconciler.py"))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("★★ X. reconciler 無任何 operator action 字面值",
          not (lits & {"pcs_charge", "pcs_discharge", "pcs_stop_power"}))
    p, clk = committed_store("charge", 5.0, 5.1, "CHARGING")
    s, t = restart_store(p, clk)
    before = io.open(p, encoding="utf-8").read()
    rc.reconcile(observe("CHARGING", power=5.1), t.record, t.trust, now=5000.0)
    check("★★ Y. reconciliation 期間 LastControl 檔案完全未被改動",
          io.open(p, encoding="utf-8").read() == before)
    check("  reconciler 不覆寫 Authority（結論一對一翻譯）",
          set(REC._OUTCOME_OF) == {CA.AUTH_OWNED, CA.AUTH_IDLE, CA.AUTH_EXTERNAL,
                                   CA.AUTH_CONFLICT, CA.AUTH_UNKNOWN})

    # ---------------- Z. production defaults ----------------
    print("\nZ. Production defaults")
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ Z. dispatch 仍未啟用，Safety Gate 模組預設仍未配置",
          RT.DISPATCH_ENABLED is False
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None)
    check("★★ Z. production Authority 政策未配置 → 運轉中設備一律不得認領",
          REC.ExecutionReconciler().reconcile(
              observe("CHARGING", power=5.1), t.record, t.trust,
              now=5000.0).owned is False)
    check("  Z. 且原因是 TTL 未設定（不是被誤判成擁有）",
          REC.ExecutionReconciler().reconcile(
              observe("CHARGING", power=5.1), t.record, t.trust,
              now=5000.0).authority_reason == CA.CA_TTL_UNSET)

    for d in _TMP + D3E._TMP:
        shutil.rmtree(d, ignore_errors=True)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-F Crash / Restart Reconciliation 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
