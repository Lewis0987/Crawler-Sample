# -*- coding: utf-8 -*-
"""
test_phase6_d3g_recovery_integration.py — Phase D.3-G Runtime Recovery（A~AO）
======================================================================
核心命題
    「重啟後的 runtime 狀態，完全由 durable + fresh 證據決定 ——
      不是由記憶體殘留、也不是由任何預設值決定。」

不可違反的界線
    1. 建構子一律 DISABLED；不存在「一建立就宣稱擁有」的路徑。
    2. recovery 只做 READ → EVALUATE → MAP STATE：
       零送出、零控制紀錄寫入、零授權、不改 dispatch_enabled、不恢復 COMMAND_PENDING。
    3. 擁有權恢復 **≠** 啟用 dispatch —— 兩者完全獨立。
    4. recovery 只在啟動／明確呼叫時執行；一般 tick 不重跑 reconciliation。
    5. 任何失敗一律 Fail Closed，且不得沿用先前的 OWNED_*。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d3g_recovery_integration.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import json
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import control_authority as CA                           # noqa: E402
import safety_gate as SG                                 # noqa: E402
import last_control_store as LCS                         # noqa: E402
import execution_reconciler as REC                       # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402

import test_phase6_d3f_reconciliation as D3F             # noqa: E402

RESULTS = []
_TMP = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


RC = REC.ExecutionReconciler(authority_policy=D3F.TEST_POLICY)


def recon(pcs="STANDBY", power=None, record=None, trust=None, store_reason=None,
          schedule=False, manual=1, now=5000.0, observation=...):
    """建立一份 reconciliation 結果（重啟後的世界）。"""
    obs = (D3F.observe(pcs, power=power, schedule=schedule, manual=manual)
           if observation is ... else observation)
    return RC.reconcile(obs, record, trust, store_reason=store_reason, now=now)


def committed(action="charge", power=5.0, observed=5.1, state="CHARGING",
              at=4990.0, boot="BOOT-1", restart_boot="BOOT-1"):
    # 🔁 Blocker 13：預設寫入時刻改為早於重啟後的讀取時刻（觀測固定在 5000.0）。
    #    原本兩者同為 5000.0，Authority 會（正確地）判定觀測不晚於指令驗證。
    p, clk = D3F.committed_store(action, power, observed, state, at=at,
                                 boot=(lambda: boot))
    s, t = D3F.restart_store(p, clk, boot=restart_boot)
    return s, t


def runtime(state=None, **kw):
    rt = RT.AutoControlRuntime(**kw)
    if state is not None:
        rt.state = state
    return rt


def _tree(name):
    return ast.parse(io.open(os.path.join(HERE, name), encoding="utf-8").read())


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def _names(node):
    return ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})


# ======================================================================
def main():
    print("== Phase D.3-G Runtime Recovery Integration 驗證（完全離線）==\n")

    # ---------------- A. 建構子 ----------------
    print("A. 建構子")
    for kw, tag in (({}, "production"), ({"dispatch_enabled": True}, "offline")):
        check(f"★★ A. {tag} Runtime 建構後一律為 DISABLED",
              RT.AutoControlRuntime(**kw).state == RT.ST_DISABLED)
    check("  A. 建構子不曾呼叫 recovery（recovery_count = 0）",
          RT.AutoControlRuntime().snapshot()["recovery_count"] == 0)

    # ---------------- B~E. Happy path ----------------
    print("\nB~E. 服務重啟：durable 證據充分")
    happy = [("B. CHARGE", "charge", 5.0, 5.1, "CHARGING", RT.ST_OWNED_CHARGE),
             ("C. DISCHARGE", "discharge", 5.0, -5.1, "DISCHARGING",
              RT.ST_OWNED_DISCHARGE)]
    for tag, act, tgt, obs, pcs, want in happy:
        s, t = committed(act, tgt, obs, pcs)
        r = recon(pcs, power=obs, record=t.record, trust=t.trust)
        check(f"  {tag} reconciliation 判為可恢復擁有", r.owned is True)
        rt = runtime()
        res = rt.recover(r)
        check(f"★★ {tag} 重啟 → DISABLED → {want}",
              res.state == want and rt.state == want
              and res.previous_state == RT.ST_DISABLED
              and res.audit_event == RT.AUDIT_RECOVERY_COMPLETED)
        check(f"  {tag} 零 dispatch",
              res.dispatched is False and rt.dispatch_count == 0)
        check(f"★★ {tag} production 的 dispatch_enabled 仍為 False（擁有權≠啟用送出）",
              rt.dispatch_enabled is False)

    for pcs, tag in (("STOPPED", "D. STOPPED"), ("STANDBY", "E. STANDBY")):
        s, t = committed("stop", None, -1.3, "STOPPED")
        r = recon(pcs, power=-1.3, record=t.record, trust=t.trust)
        rt = runtime()
        res = rt.recover(r)
        check(f"★★ {tag}. 最後一筆為 STOP + 設備 {pcs} → IDLE（非 OWNED_STOP）",
              res.state == RT.ST_IDLE and rt.state == RT.ST_IDLE
              and res.audit_event == RT.AUDIT_RECOVERY_COMPLETED)
        check(f"  {tag}. 不自動送出停止指令", rt.dispatch_count == 0)

    # ---------------- F~H. Window B / C ----------------
    print("\nF~H. Window B / C：無 durable 紀錄")
    for pcs, power, tag in (("CHARGING", 5.1, "F. CHARGING"),
                            ("DISCHARGING", -5.1, "G. DISCHARGING")):
        r = recon(pcs, power=power, record=None, trust=None,
                  store_reason=LCS.LOAD_NOT_FOUND)
        rt = runtime()
        res = rt.recover(r)
        check(f"★★ {tag} 無紀錄 → EXTERNAL_CONTROL（絕不 OWNED_*）",
              res.state == RT.ST_EXTERNAL_CONTROL
              and res.state not in RT.EXECUTION_STATES
              and res.audit_event == RT.AUDIT_RECOVERY_BLOCKED)
        rt2 = rt.tick(RT.CycleInputs(is_owner=True))
        check(f"  {tag} 下一個 normal tick 也不會因 runtime state 自行認領",
              rt2.state not in (RT.ST_OWNED_CHARGE, RT.ST_OWNED_DISCHARGE))
    check("★★ H. Window C（回讀成功但未提交）在 durable 世界與 Window B 相同 → 同樣 blocked",
          runtime().recover(recon("CHARGING", power=5.1, record=None, trust=None,
                                  store_reason=LCS.LOAD_NOT_FOUND)).state
          == RT.ST_EXTERNAL_CONTROL)

    # ---------------- I~N. 各種不得認領 ----------------
    print("\nI~N. 不得認領的情境")
    s, t = committed("charge", 5.0, 5.1, "CHARGING", restart_boot="BOOT-2")
    r = recon("CHARGING", power=5.1, record=t.record, trust=t.trust)
    rt = runtime()
    res = rt.recover(r)
    check("★★ I. OS 重開機（開機識別不同）→ 不得 OWNED",
          res.state != RT.ST_OWNED_CHARGE and r.owned is False
          and t.trust == LCS.TRUST_HISTORY_ONLY)
    check("★★ I. 且**不刪除**控制紀錄（仍保留歷史用途）",
          s.load().outcome == LCS.LOAD_OK)

    s, t = committed("charge", 5.0, 5.1, "CHARGING", at=1000.0)
    r = recon("CHARGING", power=5.1, record=t.record, trust=t.trust,
              now=1000.0 + D3F.TEST_POLICY.authority_ttl_sec + 10.0)
    check("★★ J. 紀錄已過期 → 不得 OWNED，且保留 Authority 原因",
          runtime().recover(r).state != RT.ST_OWNED_CHARGE
          and r.authority_reason == CA.CA_EXPIRED)

    s, t = committed("charge", 5.0, 5.1, "CHARGING")
    mismatches = [
        ("K. 紀錄 charge 但設備放電", dict(pcs="DISCHARGING", power=-5.1)),
        ("K. 紀錄 charge 但設備充電（反向紀錄）", dict(pcs="CHARGING", power=5.1)),
        ("L. 功率明顯不符", dict(pcs="CHARGING", power=30.0)),
        ("M. 排程接管", dict(pcs="CHARGING", power=5.1, schedule=True)),
        ("N. 人工接管（方向被改）", dict(pcs="DISCHARGING", power=-7.7)),
    ]
    for tag, kw in mismatches:
        r = recon(record=t.record, trust=t.trust, **kw)
        st = runtime().recover(r).state
        if "反向紀錄" in tag:
            check(f"  對照：{tag} → 一致時可 OWNED", st == RT.ST_OWNED_CHARGE)
            continue
        check(f"★★ {tag} → 不得 OWNED（實得 {st}）",
              st not in (RT.ST_OWNED_CHARGE, RT.ST_OWNED_DISCHARGE))

    # ---------------- O/P. 記憶體不得決定結果 ----------------
    print("\nO/P. durable + fresh 證據優先於 runtime 記憶體")
    r_ext = recon("CHARGING", power=5.1, record=None, trust=None,
                  store_reason=LCS.LOAD_NOT_FOUND)
    rt = runtime(state=RT.ST_OWNED_CHARGE)
    res = rt.recover(r_ext)
    check("★★ O. 記憶體為 OWNED 但證據判為外部 → 必須被覆寫成 Fail Closed",
          res.state == RT.ST_EXTERNAL_CONTROL and rt.state != RT.ST_OWNED_CHARGE
          and res.previous_state == RT.ST_OWNED_CHARGE)
    s, t = committed("charge", 5.0, 5.1, "CHARGING")
    r_own = recon("CHARGING", power=5.1, record=t.record, trust=t.trust)
    rt = runtime(state=RT.ST_FAULT_BLOCKED)
    res = rt.recover(r_own)
    check("★★ P. 記憶體為 FAULT_BLOCKED 但證據合法 → 可恢復成 OWNED_CHARGE",
          res.state == RT.ST_OWNED_CHARGE
          and res.previous_state == RT.ST_FAULT_BLOCKED)

    # ---------------- Q~T. 冪等 ----------------
    print("\nQ~T. Recovery 冪等")
    idem = [("Q. CHARGE", r_own, RT.ST_OWNED_CHARGE)]
    s2, t2 = committed("discharge", 5.0, -5.1, "DISCHARGING")
    idem.append(("R. DISCHARGE",
                 recon("DISCHARGING", power=-5.1, record=t2.record,
                       trust=t2.trust), RT.ST_OWNED_DISCHARGE))
    s3, t3 = committed("stop", None, -1.3, "STOPPED")
    idem.append(("S. IDLE",
                 recon("STOPPED", power=-1.3, record=t3.record, trust=t3.trust),
                 RT.ST_IDLE))
    idem.append(("T. BLOCKED", r_ext, RT.ST_EXTERNAL_CONTROL))
    for tag, r, want in idem:
        rt = runtime()
        a, b = rt.recover(r), rt.recover(r)
        check(f"★★ {tag} 連續兩次 recovery 結果相同（{want}）",
              a.state == want and b.state == want and rt.state == want)
        check(f"  {tag} 兩次皆零 dispatch、零授權",
              rt.dispatch_count == 0 and a.authorization_id is None
              and b.authorization_id is None)
        check(f"  {tag} 第二次的 previous_state 為第一次的結果（可稽核）",
              b.previous_state == want)

    # ---------------- U~Y. 不變量 ----------------
    print("\nU~Y. Recovery 的五個不變量")
    rec_fn = _func(_tree("pcs_auto_control_runtime.py"), "recover")
    check("★★ U. recover() 無任何送出相關呼叫",
          not (_names(rec_fn) & {"send", "post", "execute_and_verify",
                                 "operator_run", "run"}))
    check("★★ V. recover() 無任何控制紀錄寫入呼叫",
          not (_names(rec_fn) & {"update_from_verified_result", "save",
                                 "_atomic_write"}))
    check("★★ W. recover() 不建立授權",
          not (_names(rec_fn) & {"issue", "ProductionControlAuthorization",
                                 "consume", "authorization"}))
    rt = runtime(dispatch_enabled=True)
    before = rt.dispatch_enabled
    rt.recover(r_own)
    check("★★ X. recover() 不改變 dispatch_enabled（True 維持 True）",
          rt.dispatch_enabled is before is True)
    rt2 = runtime()
    rt2.recover(r_own)
    check("★★ X. production 的 False 也維持 False（即使恢復成 OWNED）",
          rt2.dispatch_enabled is False and rt2.state == RT.ST_OWNED_CHARGE)
    check("★★ X. recover() 原始碼未指派 dispatch_enabled",
          "dispatch_enabled" not in {n.attr for n in ast.walk(rec_fn)
                                     if isinstance(n, ast.Attribute)
                                     and isinstance(n.ctx, ast.Store)})

    class _Pending:
        outcome, lastcontrol_action, reason, detail = ("RECOVER_OWNED", "charge",
                                                       "x", "")

    check("★★ Y. recovery 結果不可能是 COMMAND_PENDING",
          RT.ST_COMMAND_PENDING not in RT.RECOVERY_STATE_MAP.values()
          and RT.ST_COMMAND_PENDING not in RT.RECOVERY_OWNED_STATE_OF.values())
    try:
        runtime()._make(RT.ST_COMMAND_PENDING, "x", "x", recovered=True,
                        authorized=True)
        blocked = False
    except AssertionError:
        blocked = True
    check("★★ Y. 即使強行指定，recovery 通道也擋下 COMMAND_PENDING", blocked)

    # ---------------- Z/AA. 失敗 ----------------
    print("\nZ/AA. Recovery 失敗一律 Fail Closed")

    def _boom():
        raise OSError("device unreachable")

    rt = runtime(state=RT.ST_OWNED_CHARGE, recovery_source=_boom)
    res = rt.recover()
    check("★★ Z. 讀取例外 → FAULT_BLOCKED，且**不得**沿用先前的 OWNED_CHARGE",
          res.state == RT.ST_FAULT_BLOCKED and rt.state == RT.ST_FAULT_BLOCKED
          and res.reason == RT.R_RECOVERY_EXCEPTION
          and res.audit_event == RT.AUDIT_RECOVERY_BLOCKED)
    check("  Z. 例外細節保留供稽核", "OSError" in (res.detail or ""))
    rt = runtime(state=RT.ST_OWNED_CHARGE)
    check("★★ AA. 未注入 recovery 來源 → FAULT_BLOCKED",
          rt.recover().state == RT.ST_FAULT_BLOCKED
          and rt.state == RT.ST_FAULT_BLOCKED)
    r_inv = recon(observation=None)
    check("★★ AA. 觀測無效 → 不得 OWNED",
          runtime().recover(r_inv).state == RT.ST_FAULT_BLOCKED)

    class _Weird:
        outcome, reason, detail = "SOMETHING_ELSE", "x", ""

    check("  未知的 reconciliation outcome → FAULT_BLOCKED（不猜測）",
          runtime().recover(_Weird()).state == RT.ST_FAULT_BLOCKED)

    class _OwnedNoAction:
        outcome, lastcontrol_action, reason, detail = "RECOVER_OWNED", None, "x", ""

    check("  判為擁有但對不到運轉方向 → FAULT_BLOCKED（不宣稱擁有）",
          runtime().recover(_OwnedNoAction()).state == RT.ST_FAULT_BLOCKED)

    # ---------------- AB/AC. 控制紀錄讀取 ----------------
    print("\nAB/AC. 控制紀錄：找不到 vs 讀不出")
    r = recon("STANDBY", record=None, trust=None, store_reason=LCS.LOAD_NOT_FOUND)
    check("★★ AB. 找不到紀錄 + 設備閒置 → IDLE（可安全重新開始）",
          r.outcome == REC.REC_IDLE
          and runtime().recover(r).state == RT.ST_IDLE)
    d = tempfile.mkdtemp(prefix="d3g_")
    _TMP.append(d)
    p = os.path.join(d, "bad.json")
    io.open(p, "w", encoding="utf-8").write("{not json")
    cur = LCS.LastControlStore(path=p).current()
    check("  AC. 既有儲存層已區分讀取錯誤（無需修改其契約）",
          cur.reason == LCS.LOAD_INVALID_JSON
          and cur.reason != LCS.LOAD_NOT_FOUND)
    for bad in sorted(REC.STORE_FAILURE_REASONS):
        r = recon("STANDBY", record=None, trust=None, store_reason=bad)
        check(f"★★ AC. 紀錄讀取失敗（{bad}）→ Fail Closed，**不得**當成沒有紀錄",
              r.outcome == REC.REC_BLOCKED
              and r.reason == REC.R_STORE_UNREADABLE
              and runtime().recover(r).state == RT.ST_FAULT_BLOCKED)

    # ---------------- 稽核 ----------------
    print("\nRecovery 稽核")
    rt = runtime(state=RT.ST_FAULT_BLOCKED)
    res = rt.recover(r_own)
    evs = [a.audit_event for a in rt.audit]
    check("★★ 稽核含 RECOVERY_STARTED 與 RECOVERY_COMPLETED",
          RT.AUDIT_RECOVERY_STARTED in evs and RT.AUDIT_RECOVERY_COMPLETED in evs)
    check("★★ 稽核記錄 previous_state / new state / outcome / authority reason",
          res.previous_state == RT.ST_FAULT_BLOCKED
          and res.state == RT.ST_OWNED_CHARGE
          and res.recovery_outcome == REC.REC_OWNED
          and res.observation_reason is not None)
    blocked = runtime().recover(r_ext)
    check("  被擋下時使用 RECOVERY_BLOCKED 事件",
          blocked.audit_event == RT.AUDIT_RECOVERY_BLOCKED)
    dump = json.dumps(res.as_dict(), ensure_ascii=False, default=str)
    check("★★ 稽核不含任何憑證／token／完整 payload",
          not any(k in dump.lower() for k in ("password", "token", "accesstoken",
                                              "credential", "authorization:")))

    # ---------------- 一般 tick 不重跑 recovery ----------------
    print("\nRecovery 只在啟動／明確呼叫時執行")
    tick_fn = _func(_tree("pcs_auto_control_runtime.py"), "_cycle")
    check("★★ 一般 cycle 不呼叫 recover / reconciler",
          not (_names(tick_fn) & {"recover", "_recovery_source", "reconcile"}))
    rt = runtime(dispatch_enabled=True)
    n0 = rt.snapshot()["recovery_count"]
    rt.tick(RT.CycleInputs(is_owner=True))
    rt.tick(RT.CycleInputs(is_owner=False))
    check("★★ 連續 tick 不增加 recovery 次數",
          rt.snapshot()["recovery_count"] == n0 == 0)
    check("  recovery 次數只由明確呼叫遞增",
          (lambda x: x.recover(r_own) or x.snapshot()["recovery_count"] == 1)(
              runtime()))

    # ---------------- Service wiring ----------------
    print("\nService wiring：零網路 I/O")
    src = SVC.build_recovery_source()
    before_mods = {"device_control_operator", "api_client",
                   "device_control_scraper"} & set(sys.modules)
    res = src()
    check("★★ AD. production 恢復來源可呼叫，但不連任何設備",
          res.outcome in (REC.REC_BLOCKED, REC.REC_UNKNOWN)
          and res.may_dispatch is False)
    check("★★ AD. 呼叫後仍未載入任何實機出口模組",
          ({"device_control_operator", "api_client", "device_control_scraper"}
           & set(sys.modules)) == before_mods)
    rt = RT.AutoControlRuntime(recovery_source=SVC.build_recovery_source())
    r = rt.recover()
    check("★★ AE. production 啟動恢復 → Fail Closed，且 dispatch_enabled 仍 False",
          r.state == RT.ST_FAULT_BLOCKED and rt.dispatch_enabled is False
          and rt.dispatch_count == 0)
    rec_src = _func(_tree("pcs_auto_control_service.py"), "build_recovery_source")
    check("★★ 恢復來源不建立 executor、不寫紀錄",
          not (_names(rec_src) & {"PcsControlExecutor", "send",
                                  "update_from_verified_result",
                                  "build_execution_chain"}))

    # ---------------- Production defaults ----------------
    print("\nAF. Production defaults")
    # 🔁 D.5-C 更正：六項參數已依裁示寫入 production。
    #    契約不變，只是更精確：**未經裁示者仍為 None、dispatch 仍不就緒**。
    # 🔁 max_power_kw 寫入後更正：參數已齊備，dispatch_ready 不再是 False。
    #    真正的不變量是「dispatch 未啟用」—— 改為斷言 DISPATCH_ENABLED。
    check("★★ AF. Layer 2 仍未配置，dispatch 仍關閉",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None
          and RT.DISPATCH_ENABLED is False)
    # 🔁 Blocker 13 更正：11 → 12。新增的 OWNERSHIP_PENDING 是**唯一**新增，
    #    語意是「有 durable 證據指向本方作業，但 AC 功率尚未更新到指令後的值」——
    #    既有狀態無法表達：OWNED_* 會變成無證據宣稱擁有權，
    #    EXTERNAL_CONTROL 會把自家作業誤標成別人的。
    #    它不在 EXECUTION／DISPATCH 集合內，因此結構上不可能送出指令。
    check("★★ AF. runtime 狀態只增加 OWNERSHIP_PENDING 一個（12 個）",
          len(RT.RUNTIME_STATES) == 12
          and RT.ST_OWNERSHIP_PENDING in RT.RUNTIME_STATES
          and set(RT.RECOVERY_STATE_MAP.values())
          | set(RT.RECOVERY_OWNED_STATE_OF.values()) <= RT.RUNTIME_STATES)
    check("★★ AF. 新增的狀態結構上不可能 dispatch",
          RT.ST_OWNERSHIP_PENDING not in RT.EXECUTION_STATES
          and RT.ST_OWNERSHIP_PENDING not in RT.DISPATCH_STATES)

    for d in _TMP + D3F._TMP:
        shutil.rmtree(d, ignore_errors=True)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-G Runtime Recovery Integration 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
