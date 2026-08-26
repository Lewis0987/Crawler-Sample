# -*- coding: utf-8 -*-
"""
test_phase6_d3a_runtime.py — Phase D.3-A Runtime Skeleton 驗證（A~M）
======================================================================
核心命題：**D.3-A 在結構上不可能送出任何 PCS 指令。**

不是「目前沒有送」，而是「連路徑都不存在」：
    · 模組層與函式層都沒有 device_control_operator / executor 的 import
    · DISPATCH_ENABLED = False，且進入 dispatch 狀態會直接被 assert 擋下
    · 即使把全部 production 參數填滿，仍停在 OBSERVE_ONLY

是否需要設備
    **不需要**。零網路、零控制。Mutex 測試使用真實 Windows Named Mutex，
    但只驗證互斥語意，不碰任何設備。

用法
    python test_phase6_d3a_runtime.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_MODULES_BEFORE = set(sys.modules)

import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402

# 於 import 三個新模組後立即快照 —— 用來證明它們不具備任何控制／網路能力
_MODULES_AFTER = set(sys.modules)

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


# ----------------------------------------------------------------------
# 靜態工具
# ----------------------------------------------------------------------
def _tree(mod):
    return ast.parse(io.open(mod.__file__, encoding="utf-8").read())


def _top_imports(tree):
    out = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


def _all_imports(tree):
    """含函式內延後 import —— 延後 import 一樣是能力，不得漏檢。"""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or ".").split(".")[0])
    return out


def _calls_named(tree, name):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == name for n in ast.walk(tree))


# 🔴 **PCS 控制路徑** —— 三個模組全部不得 import，且 sys.modules 必須零載入。
#    這是本階段的核心不變量：控制服務沒有任何抵達 PCS 的途徑。
FORBIDDEN_PCS_PATH = {
    "device_control_operator", "device_control_menu", "device_control_scraper",
    "report_monitor", "charge_discharge_report", "api_client",
    "phase6_field_measure",
}

# ⚠️ pcs_control_executor 自 D.3-E 起會被執行鏈載入。它是**零 I/O 的薄 adapter**
#    （只 import stdlib + pcs_control_integration + safety_gate，全部純邏輯），
#    未注入 operator_run 時一律回 COMMAND_NOT_SENT / OPERATOR_NOT_CONFIGURED。
#    因此「模組被載入」不構成實機能力；真正的設備出口是
#    device_control_operator（唯一持有控制 POST 的模組）與 api_client。
#    下方有專屬斷言證明 production 的 executor 實例確實**未武裝**。
UNARMED_ADAPTERS = {"pcs_control_executor", "production_execution_chain"}

# ⚠️ **電表路徑**（D.3-C 起）：讀取台電電表是被明確授權的資料來源，
#    而既有且已驗證的 meter_client 於模組層 import socketio（連帶 requests）。
#    因此「行程內存在網路函式庫」是預期事實，但它只通往**電表**，
#    不構成任何 PCS 控制能力 —— 下方有專屬斷言證明兩者分離。
METER_PATH_MODULES = {"meter_client", "socketio", "requests", "urllib3",
                      "meter_observation_adapter"}

FORBIDDEN_CAPABILITY = FORBIDDEN_PCS_PATH

# 🔴 Runtime core 與 config 的**更嚴格**清單：連零 I/O 的 Phase 6 純邏輯 library
#    也不得 import —— core 必須保持完全純粹，資料一律由外部注入。
#    （D.3-B 的 Adapter 依裁示五必須重用 pcs_state_from_ess()，
#      那是 Adapter 層的責任，不得滲入 Runtime core。）
FORBIDDEN_FOR_CORE = FORBIDDEN_PCS_PATH | METER_PATH_MODULES | UNARMED_ADAPTERS | {
    "pcs_control_integration", "decision_engine", "safety_gate",
    "control_authority", "last_control_store", "decision_policy",
    "power_classifier", "tou_calendar", "ess_snapshot_adapter",
    "decision_observer",
}


# ----------------------------------------------------------------------
# Mutex 子行程（不新增檔案；以 -c 內嵌）
# ----------------------------------------------------------------------
_CHILD = (
    "import sys\n"
    "sys.path.insert(0, r'{here}')\n"
    "import pcs_auto_control_service as S\n"
    "ok, why = S.acquire_ownership(role='child')\n"
    "print('READY', ok, why, flush=True)\n"
    "sys.stdin.readline()\n"
    "S.release_ownership()\n"
)


def _spawn_holder():
    """啟動一個持有 Control Mutex 的子行程；回傳 (proc, ok, why)。"""
    p = subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(here=HERE)],
        cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    line = p.stdout.readline().strip()
    parts = line.split()
    if len(parts) >= 3 and parts[0] == "READY":
        return p, parts[1] == "True", parts[2]
    return p, False, f"child_unexpected:{line!r}"


def _stop_holder(p, kill=False):
    try:
        if kill:
            p.kill()                      # 模擬 crash → abandoned mutex
        else:
            p.stdin.write("\n")
            p.stdin.flush()
        p.wait(timeout=20)
    except Exception:                     # noqa: BLE001
        try:
            p.kill()
        except Exception:                 # noqa: BLE001
            pass


# keeper：只開啟 handle、**不** Wait —— 讓 Mutex 物件在 owner 死亡後仍存在，
# 這是 WAIT_ABANDONED 能夠發生的必要條件（否則最後一個 handle 關閉時物件會被銷毀，
# 下一個 CreateMutexExW 會建立全新的未擁有物件而回 WAIT_OBJECT_0）。
_KEEPER = (
    "import sys, ctypes\n"
    "sys.path.insert(0, r'{here}')\n"
    "import pcs_auto_control_service as S\n"
    "k = S._k32()\n"
    "sa, sd, err = S._build_mutex_security()\n"
    "h = k.CreateMutexExW(ctypes.byref(sa), S.mutex_name(), 0, S.MUTEX_MIN_ACCESS)\n"
    "k.LocalFree(sd)\n"
    "print('KEEPER', bool(h), flush=True)\n"
    "sys.stdin.readline()\n"
)


def _spawn_keeper():
    p = subprocess.Popen(
        [sys.executable, "-c", _KEEPER.format(here=HERE)],
        cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    line = p.stdout.readline().strip()
    return p, line == "KEEPER True"


def _raw_probe(name):
    """
    非破壞式探測：能否取得此名稱的 Mutex。取得後立即釋放，不改變任何長期狀態。

    ⚠️ 回傳值會受環境影響（例如 AutoMonitorService 正在執行時，
       Report Mutex 本來就取不到）—— 因此只能用來比較「前後是否改變」，
       不得直接斷言「一定取得得到」。
    """
    ok, h = _raw_hold(name)
    if ok:
        _raw_release(h)
    return ok


def _raw_hold(name):
    """
    以原生 API 直接持有指定名稱的 Mutex（不經過 report_monitor 的
    ownership 簿記，因此不會產生任何檔案副作用）。回傳 (ok, handle)。
    """
    import ctypes
    k = SVC._k32()
    if k is None:
        return False, None
    sa, sd, err = SVC._build_mutex_security()
    if sa is None:
        return False, None
    try:
        h = k.CreateMutexExW(ctypes.byref(sa), name, 0, SVC.MUTEX_MIN_ACCESS)
    finally:
        if sd:
            k.LocalFree(sd)
    if not h:
        return False, None
    rc = k.WaitForSingleObject(h, 0)
    if rc in (SVC.WAIT_OBJECT_0, SVC.WAIT_ABANDONED):
        return True, h
    k.CloseHandle(h)
    return False, None


def _raw_release(h):
    k = SVC._k32()
    if k is None or not h:
        return
    try:
        k.ReleaseMutex(h)
        k.CloseHandle(h)
    except Exception:                     # noqa: BLE001
        pass


# ======================================================================
def main():
    print("== Phase D.3-A Runtime Skeleton 驗證（完全離線）==\n")

    # ---------------- A. Config Fail Closed ----------------
    print("A. Production Config Fail Closed")
    c = CFG.DEFAULT_CONTROL_CONFIG
    # 🔁 D.5-A/D.5-B 更正：三項 ReadBack 參數已依裁示升 FINAL 並寫入 production
    #    （timeout 75.0 / poll 5.0 / samples 1）。
    #    其餘尚無正式依據的參數**一項都沒有**被填值。
    #    ⚠️ min_switch_interval_sec 為**刻意** DEFERRED（Layer 2 明確停用），
    #       不是遺漏 —— 沒有反轉間隔的實機依據，不得填猜測值。
    # 🔁 D.5-C 更正：六項參數已依裁示寫入。**未經裁示者一項都沒有被填。**
    check("★★ A. 已裁示的參數皆為裁示值",
          c.charge_power_kw == 5.0 and c.discharge_power_kw == 5.0
          and c.decision_interval_sec == 30.0
          and c.authorization_ttl_sec == 10.0
          and c.authority_ttl_sec == 180.0
          and c.authority_power_tolerance_kw == 1.25
          and c.readback_timeout_sec == 75.0
          and c.readback_poll_interval_sec == 5.0
          and c.readback_stability_samples == 1)
    check("★★ A. 未經裁示的參數一律仍為 None（無一被自行填值）",
          all(getattr(c, n) is None for n in (
              "min_switch_interval_sec", "meter_stale_grace_sec")))
    # 🔁 max_power_kw 寫入後更正：required 已全部就緒。
    #    但**參數就緒不等於允許派工** —— 這才是要守住的不變量。
    check("★★ A. required 全部就緒（dispatch_ready=True），"
          "但 dispatch 仍未啟用",
          c.dispatch_ready is True and c.missing_required() == ()
          and RT.DISPATCH_ENABLED is False)
    check("★★ A. 缺參數 → Runtime 停在 OBSERVE_ONLY",
          RT.AutoControlRuntime().tick(RT.CycleInputs(is_owner=True)).state
          == RT.ST_OBSERVE_ONLY)
    # 🔁 更正：config 已就緒，因此停在 OBSERVE_ONLY 的理由不再是「參數未就緒」，
    #    而是「dispatch 未啟用」—— 這是更強的保證（參數齊了也不動）。
    check("  A. config 已就緒時仍停在 OBSERVE_ONLY（理由為 dispatch 未實作／未啟用）",
          RT.AutoControlRuntime().tick(RT.CycleInputs(is_owner=True)).reason
          != RT.R_CONFIG_NOT_READY)
    # 🔴 「部分參數已 FINAL」不得成為放行理由。
    #    ⚠️ 必須**顯式**把其餘欄位設為 None —— dataclass 的預設值現在就是
    #       production 值，只給幾個 kwargs 會把已核准的值一起帶進來。
    partial = CFG.AutoControlConfig(
        charge_power_kw=None, discharge_power_kw=None, max_power_kw=None,
        authority_ttl_sec=None, authority_power_tolerance_kw=None,
        decision_interval_sec=None, authorization_ttl_sec=None,
        readback_timeout_sec=75.0, readback_poll_interval_sec=5.0)
    check("★★ A. 只填 readback（已 FINAL）仍不放行 —— required 是 all-or-nothing",
          partial.dispatch_ready is False
          and set(partial.missing_required()) == {
              "charge_power_kw", "discharge_power_kw", "max_power_kw",
              "authority_ttl_sec", "authority_power_tolerance_kw",
              "decision_interval_sec", "authorization_ttl_sec"}
          and RT.AutoControlRuntime(config=partial).tick(
              RT.CycleInputs(is_owner=True)).state == RT.ST_OBSERVE_ONLY)
    check("  A. 該情境的理由確為「production 參數未就緒」",
          RT.AutoControlRuntime(config=partial).tick(
              RT.CycleInputs(is_owner=True)).reason == RT.R_CONFIG_NOT_READY)
    check("  A. min_switch_interval_sec / meter_stale_grace_sec 刻意不在 required 之內",
          "min_switch_interval_sec" not in CFG.REQUIRED_FOR_DISPATCH
          and "meter_stale_grace_sec" not in CFG.REQUIRED_FOR_DISPATCH)
    for bad in ({"charge_power_kw": 0}, {"charge_power_kw": -1},
                {"decision_interval_sec": float("nan")},
                {"min_switch_interval_sec": -0.1},
                {"readback_stability_samples": 0},
                {"authority_ttl_sec": True}):
        try:
            CFG.AutoControlConfig(**bad)
            ok = False
        except ValueError:
            ok = True
        check(f"  A. 非法設定被拒：{bad}", ok)

    # ---------------- B/C/D. 意圖 → 零 dispatch ----------------
    print("\nB/C/D. 即使有明確意圖也不得產生任何 dispatch")
    full = CFG.AutoControlConfig(
        charge_power_kw=5.0, discharge_power_kw=5.0, max_power_kw=100.0,
        authority_ttl_sec=120.0, authority_power_tolerance_kw=1.0,
        decision_interval_sec=30.0, authorization_ttl_sec=30.0,
        readback_timeout_sec=75.0, readback_poll_interval_sec=5.0)
    check("  前提：此測試用 config 為完整（僅測試注入，非 production 值）",
          full.dispatch_ready is True)

    for tag, want, ev in (("B. CHARGE", RT.WANT_CHARGE, RT.AUDIT_WOULD_CHARGE),
                          ("C. DISCHARGE", RT.WANT_DISCHARGE, RT.AUDIT_WOULD_DISCHARGE),
                          ("D. STOP", RT.WANT_STOP, RT.AUDIT_WOULD_STOP)):
        for label, cfg in (("參數未齊備", CFG.DEFAULT_CONTROL_CONFIG),
                           ("參數已齊備", full)):
            rt = RT.AutoControlRuntime(config=cfg)
            r = rt.tick(RT.CycleInputs(is_owner=True, desired_action=want,
                                       target_power_kw=5.0))
            check(f"★★ {tag}（{label}）→ dispatched=False / {ev} / OBSERVE_ONLY",
                  r.dispatched is False and r.audit_event == ev
                  and r.would_action == want and r.state == RT.ST_OBSERVE_ONLY
                  and rt.dispatch_count == 0)
    check("★★ B/C/D. 參數齊備時 reason 明示為『D.3-A 尚未實作 dispatch』",
          RT.AutoControlRuntime(config=full).tick(
              RT.CycleInputs(is_owner=True, desired_action=RT.WANT_CHARGE)).reason
          == RT.R_DISPATCH_NOT_IMPLEMENTED)
    check("★★ B/C/D. DISPATCH_ENABLED 必須為 False", RT.DISPATCH_ENABLED is False)
    check("★★ B/C/D. D.3-A 可達狀態與 dispatch 狀態完全不相交",
          not (RT.D3A_REACHABLE_STATES & RT.DISPATCH_STATES))

    # 🔴 最後一道保險：直接要求進入 dispatch 狀態必須被擋下
    rt_guard = RT.AutoControlRuntime(config=full)
    for st in sorted(RT.DISPATCH_STATES):
        try:
            rt_guard._make(st, "x", "x")
            blocked = False
        except AssertionError:
            blocked = True
        check(f"★★ transition guard：直接進入 {st} 被 assert 擋下", blocked)

    # ---------------- E. 例外 → Fail Closed ----------------
    print("\nE. cycle 例外必須 Fail Closed（不得照抄 report monitor 的續行哲學）")

    def _boom(_inputs):
        raise RuntimeError("injected")

    rt_e = RT.AutoControlRuntime(config=full, observer=_boom)
    r_e = rt_e.tick(RT.CycleInputs(is_owner=True, desired_action=RT.WANT_CHARGE))
    check("★★ E. observer 例外 → FAULT_BLOCKED / CYCLE_EXCEPTION / 不 dispatch",
          r_e.state == RT.ST_FAULT_BLOCKED and r_e.reason == RT.R_CYCLE_EXCEPTION
          and r_e.audit_event == RT.AUDIT_CYCLE_EXCEPTION
          and r_e.dispatched is False and rt_e.dispatch_count == 0)
    check("  E. tick() 本身不得拋出（Fail Closed 邊界不可再失敗）",
          rt_e.tick(RT.CycleInputs(is_owner=True)).state == RT.ST_FAULT_BLOCKED)
    check("  E. 例外 detail 保留型別與訊息，便於稽核",
          "RuntimeError" in r_e.detail and "injected" in r_e.detail)

    class _EvilClock:
        def __call__(self):
            raise OSError("clock exploded")

    rt_c = RT.AutoControlRuntime(config=full, clock=_EvilClock())
    check("★★ E. 連 clock 失效也必須 Fail Closed，而非拋出",
          rt_c.tick(RT.CycleInputs(is_owner=True)).state == RT.ST_FAULT_BLOCKED)

    # ---------------- F/G. Ownership ----------------
    print("\nF/G. PCS Control Ownership（獨立於 Report Monitor Ownership）")
    import report_monitor as _RM_FOR_NAME                 # noqa: PLC0415  只取名稱
    rep_name = _RM_FOR_NAME._mutex_name()
    ctl_name = SVC.mutex_name()
    check("★★ 兩把鎖名稱不同（前綴即不同）",
          ctl_name != rep_name
          and ctl_name.startswith(CFG.CONTROL_MUTEX_PREFIX)
          and not ctl_name.startswith("Global\\ESS_AutoMonitor_"))
    check("  Control Mutex 名稱含 normcase+abspath 雜湊（同機不同部署不互搶）",
          ctl_name != CFG.CONTROL_MUTEX_PREFIX and len(ctl_name) > len(CFG.CONTROL_MUTEX_PREFIX))
    check("  DACL 強度不低於既有服務（SDDL 完全相同）",
          SVC.MUTEX_SDDL == _RM_FOR_NAME.MUTEX_SDDL
          and SVC.MUTEX_MIN_ACCESS == _RM_FOR_NAME.MUTEX_MIN_ACCESS)

    if os.name != "nt":
        check("  （非 Windows：略過實際 Mutex 測試）", True)
    else:
        # 🔴 Report Mutex 的可用性取決於環境（AutoMonitorService 可能正在執行），
        #    因此斷言的是「**是否改變**」而不是「一定取得得到」。
        rep_before = _raw_probe(rep_name)
        print(f"    （本機 Report Mutex 目前{'可' if rep_before else '不可'}取得"
              f" —— AutoMonitorService {'未' if rep_before else '正在'}執行）")

        holder, cok, cwhy = _spawn_holder()
        try:
            check(f"  子行程成功取得 Control Ownership（{cwhy}）", cok is True)
            ok2, why2 = SVC.acquire_ownership(role="second-instance")
            check("★★ F. 第二個 control service instance → ownership denied",
                  ok2 is False and why2 == "held_by_other" and SVC.is_owner() is False)
            rep_during = _raw_probe(rep_name)
            check("★★ G. 持有 Control Mutex **不影響** Report Mutex 的狀態（兩把獨立）",
                  rep_during == rep_before)
        finally:
            _stop_holder(holder)

        ok3, why3 = SVC.acquire_ownership(role="after-release")
        # 🔴 這一條才是真正的共存證明：本機的 AutoMonitorService 正在執行且持有
        #    Report Mutex 時，Control Service 依然能取得自己的 Ownership。
        check("★★ G. Report Monitor 服務執行中，Control Ownership 仍可取得（可共存）",
              ok3 is True and SVC.is_owner() is True)
        check("  G. 取得 Control Ownership 後，Report Mutex 狀態依舊不變",
              _raw_probe(rep_name) == rep_before)
        SVC.release_ownership()
        ok3, why3 = SVC.acquire_ownership(role="after-release")
        check("  持有者釋放後，本行程可取得 Ownership",
              ok3 is True and why3 in ("acquired", "abandoned_taken"))
        check("  acquire 具 idempotent（重複呼叫不重複 Wait）",
              SVC.acquire_ownership()[1] == "already_owner")
        check("  ownership_state 不外洩 handle",
              "handle" not in SVC.ownership_state()
              and SVC.ownership_state()["owned"] is True)
        check("  release 後 is_owner 轉為 False",
              SVC.release_ownership() is True and SVC.is_owner() is False)
        check("  非 Owner 呼叫 release 為 no-op（不可能誤釋放他人的鎖）",
              SVC.release_ownership() is False)

        # abandoned：持有者被強制結束（模擬 crash）
        # ⚠️ 必須同時有一個 keeper 持有 handle（但不擁有），Mutex 物件才會在 owner
        #    死亡後繼續存在並被標記為 abandoned。否則最後一個 handle 關閉時物件即銷毀，
        #    下一次 CreateMutexExW 會建立全新物件 → 回 acquired 而非 abandoned_taken。
        keeper, kok = _spawn_keeper()
        try:
            check("  keeper 子行程已持有 handle（abandoned 測試前提）", kok is True)
            holder2, cok2, _ = _spawn_holder()
            check("  owner 子行程取得 Ownership（abandoned 測試前提）", cok2 is True)
            _stop_holder(holder2, kill=True)          # 模擬 crash
            ok4, why4 = SVC.acquire_ownership(role="abandoned-test")
            check("★★ abandoned mutex：前任未釋放即死亡 → 由本行程接管（視為成功）",
                  ok4 is True and why4 == "abandoned_taken")
            check("  abandoned 事實有被記錄（與 Phase 4 既有規則一致）",
                  SVC.ownership_state()["abandoned"] is True)
            SVC.release_ownership()
        finally:
            _stop_holder(keeper)

        # owner 死亡且無 keeper 時，物件被銷毀 → 下一個行程仍可正常取得（不得死鎖）
        holder3, cok3, _ = _spawn_holder()
        check("  owner 子行程取得 Ownership（無 keeper 情境前提）", cok3 is True)
        _stop_holder(holder3, kill=True)
        ok5, why5 = SVC.acquire_ownership(role="no-keeper")
        check("★★ owner crash 後不得留下死鎖：下一行程必定可取得",
              ok5 is True and why5 in ("acquired", "abandoned_taken"))
        SVC.release_ownership()

    # ---------------- H. Shutdown ----------------
    print("\nH. Shutdown：relinquish authority only")
    rt_h = RT.AutoControlRuntime(config=full)
    rt_h.tick(RT.CycleInputs(is_owner=True, desired_action=RT.WANT_CHARGE))
    r_h = rt_h.shutdown()
    check("★★ H. shutdown → SHUTTING_DOWN / 零 dispatch",
          r_h.state == RT.ST_SHUTTING_DOWN and r_h.dispatched is False
          and rt_h.dispatch_count == 0)
    check("★★ H. shutdown 不得產生 STOP 意圖（不隱含停止 PCS）",
          r_h.would_action is None and r_h.audit_event == RT.AUDIT_SHUTDOWN)
    check("  H. 進入關閉後不再產生新的判定",
          rt_h.tick(RT.CycleInputs(is_owner=True, desired_action=RT.WANT_CHARGE)
                    ).audit_event == RT.AUDIT_ALREADY_SHUTDOWN)
    # ⚠️ 只比對「**完全等於** action 名稱」的字串常數 —— 說明文字裡提到
    #    pcs_stop_power 是文件，不是能力；用子字串比對會被自己的註解誤判。
    ACTION_LITERALS = {"pcs_stop_power", "pcs_charge", "pcs_discharge",
                       "pcs_start", "pcs_enable"}
    for mod in (RT, SVC, CFG):
        lits = {n.value for n in ast.walk(_tree(mod))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        hit = lits & ACTION_LITERALS
        check(f"★★ H. {os.path.basename(mod.__file__)} 無任何 operator action 字面值"
              f"（命中={sorted(hit)}）", not hit)

    # ---------------- I. Restart ----------------
    print("\nI. Restart：不得直接進入 OWNED_*")
    fresh = RT.AutoControlRuntime(config=full)
    check("★★ I. 全新 runtime 起始狀態為 DISABLED", fresh.state == RT.ST_DISABLED)
    seen = {fresh.state}
    for want in (RT.WANT_CHARGE, RT.WANT_DISCHARGE, RT.WANT_STOP, None):
        for owner in (True, False):
            seen.add(fresh.tick(RT.CycleInputs(is_owner=owner,
                                               desired_action=want)).state)
    seen.add(fresh.shutdown().state)
    check("★★ I. 任何輸入組合都不可能到達 OWNED_CHARGE / OWNED_DISCHARGE",
          not (seen & {RT.ST_OWNED_CHARGE, RT.ST_OWNED_DISCHARGE}))
    check("★★ I. 實際出現過的狀態全部落在 D.3-A 允許集合內",
          seen <= RT.D3A_REACHABLE_STATES)
    check("  I. 且全程 dispatch_count 維持 0", fresh.dispatch_count == 0)
    check("  I. 每一筆稽核事件的 dispatched 都是 False",
          all(a.dispatched is False for a in fresh.audit))

    # ---------------- J. 零 input() ----------------
    print("\nJ. Core runtime 不得有任何 input()（Windows Service 沒有 console）")
    for mod in (RT, SVC, CFG):
        check(f"★★ J. {os.path.basename(mod.__file__)} 無 input() 呼叫",
              not _calls_named(_tree(mod), "input"))

    # ---------------- K. 零 operator / 零 executor ----------------
    print("\nK. 零實機出口（AST + sys.modules 雙重驗證）")
    # 三個模組一律不得具備控制/網路能力（模組層 + 函式內延後 import）
    for mod in (RT, SVC, CFG):
        base = os.path.basename(mod.__file__)
        hit_top = _top_imports(_tree(mod)) & FORBIDDEN_CAPABILITY
        hit_all = _all_imports(_tree(mod)) & FORBIDDEN_CAPABILITY
        check(f"★★ K. {base} 模組層未 import 任何控制/網路模組（命中={sorted(hit_top)}）",
              not hit_top)
        check(f"★★ K. {base} 連函式內延後 import 也沒有（命中={sorted(hit_all)}）",
              not hit_all)
    # Runtime core / config 更嚴格：連零 I/O 的 Phase 6 純邏輯 library 也不得 import
    for mod in (RT, CFG):
        base = os.path.basename(mod.__file__)
        hit_core = _all_imports(_tree(mod)) & FORBIDDEN_FOR_CORE
        check(f"★★ K. {base}（core）連純邏輯 Phase 6 library 都不 import"
              f"（命中={sorted(hit_core)}）", not hit_core)
    loaded = (_MODULES_AFTER - _MODULES_BEFORE) & FORBIDDEN_PCS_PATH
    check(f"★★ K. import 三個新模組後，未載入任何 PCS 控制路徑模組"
          f"（命中={sorted(loaded)}）", not loaded)
    # 🔴 新增（D.3-C）：行程內存在網路函式庫是電表路徑的必然結果，
    #    但必須證明它**只**通往電表 —— 沒有任何 HMI / PCS API 客戶端。
    api_hit = {m for m in ("api_client", "device_control_operator",
                           "device_control_scraper")
               if m in _MODULES_AFTER}
    check(f"★★ K. 行程內沒有任何 HMI / PCS API 能力（命中={sorted(api_hit)}）",
          not api_hit)
    # 補強：即使 service 透過 Adapter 間接載入了純邏輯 library，
    #       真正的實機出口在**匯入這三個模組的當下**仍必須完全不存在。
    # ⚠️ 這裡必須用 _MODULES_AFTER 快照，不能用當下的 sys.modules ——
    #    本測試檔自己在 F/G 段 import 了 report_monitor（只為取得 Mutex 名稱），
    #    那會連帶載入 api_client / requests。那是**測試檔**的相依，
    #    不是受測模組的能力，不得混為一談。
    still = {m for m in ("device_control_operator", "api_client",
                         "charge_discharge_report", "device_control_scraper")
             if m in _MODULES_AFTER}
    check(f"★★ K. 匯入三個模組（含 Adapter / Observer 傳遞相依）後仍無任何實機出口"
          f"（命中={sorted(still)}）", not still)
    # 電表路徑的存在是預期事實，明文記錄以免日後被誤認為缺陷
    check("  K. 網路函式庫僅來自電表路徑（meter_client → socketio → requests）",
          ("socketio" not in _MODULES_AFTER)
          or ("meter_client" in _MODULES_AFTER))
    names_rt = ({n.id for n in ast.walk(_tree(RT)) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(_tree(RT)) if isinstance(n, ast.Attribute)})
    # 🔴 D.3-E 新增：即使 adapter 已被載入，production 的執行鏈仍**未武裝**。
    _chain = SVC.build_execution_chain()
    check("★★ K. production 執行鏈的 executor / readback / store 皆未注入",
          _chain.executor is None and _chain.verifier is None
          and _chain.store is None)
    check("★★ K. 因此 production 執行鏈跑一輪必為零送出、零紀錄寫入",
          _chain.run().executed is False and _chain.dispatch_count == 0
          and _chain.lastcontrol_write_count == 0)
    check("★★ K. pcs_control_executor 本身零 I/O（只依賴 stdlib 與純邏輯模組）",
          not (_all_imports(ast.parse(io.open(
              os.path.join(HERE, "pcs_control_executor.py"),
              encoding="utf-8").read()))
               & {"device_control_operator", "api_client", "requests",
                  "charge_discharge_report", "device_control_scraper"}))
    check("★★ K. production Runtime 的 dispatch_enabled 為 False（執行狀態不可達）",
          RT.AutoControlRuntime().dispatch_enabled is False
          and RT.DISPATCH_ENABLED is False)

    check("★★ K. runtime 未出現 executor / operator / send / dispatch 相關識別字",
          not (names_rt & {"PcsControlExecutor", "operator_run", "send",
                           "DCO", "EX", "run_operator"}))
    check("★★ K. runtime 的 dispatch_count 欄位存在且恆為 0（稽核用）",
          RT.AutoControlRuntime().snapshot()["dispatch_count"] == 0)
    check("  K. snapshot 明示 dispatch_enabled=False",
          RT.AutoControlRuntime().snapshot()["dispatch_enabled"] is False)

    # ---------------- 服務層迴圈（不需設備）----------------
    print("\n服務層：迴圈與關閉（完全離線）")
    rt_s = RT.AutoControlRuntime(config=full)
    n = SVC.run(rt_s, interval=0, max_ticks=3, owner=True)
    check("★★ 服務迴圈跑 3 輪 → 零 dispatch，狀態仍為 OBSERVE_ONLY",
          n == 3 and rt_s.dispatch_count == 0 and rt_s.state == RT.ST_OBSERVE_ONLY)
    check("  非 Owner 時迴圈只會產生 DISABLED",
          RT.AutoControlRuntime(config=full).tick(
              RT.CycleInputs(is_owner=False)).state == RT.ST_DISABLED)
    check("  --status 可離線執行（不需 Ownership、不需設備）",
          SVC.main(["--status"]) == 0)
    check("  稽核事件數有上限，長跑不會無限膨脹",
          RT.AUDIT_LIMIT > 0 and len(rt_s.audit) <= RT.AUDIT_LIMIT)

    # ---------------- 既有模組未被汙染 ----------------
    print("\n既有 production 參數未被汙染")
    import safety_gate as SG                              # noqa: PLC0415
    import control_authority as CA                        # noqa: PLC0415
    import decision_policy as DP                          # noqa: PLC0415
    check("★★ safety_gate 的 min_switch_interval_sec / max_power_kw 仍為 None",
          SG.DEFAULT_SAFETY_CONFIG.min_switch_interval_sec is None
          and SG.DEFAULT_SAFETY_CONFIG.max_power_kw is None)
    check("★★ control_authority 的 ttl / tolerance 仍為 None",
          CA.DEFAULT_AUTHORITY_POLICY.authority_ttl_sec is None
          and CA.DEFAULT_AUTHORITY_POLICY.authority_power_tolerance_kw is None)
    check("★★ decision_policy 的充放電功率仍為 None",
          DP.PolicyConfig().charge_power_kw is None
          and DP.PolicyConfig().discharge_power_kw is None)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.3-A Runtime Skeleton 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
