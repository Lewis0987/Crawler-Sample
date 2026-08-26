# -*- coding: utf-8 -*-
"""
pcs_auto_control_service.py — PCS 自動控制服務外殼（Phase D.3-A Runtime Skeleton）
======================================================================
職責僅限「組裝層」：Ownership、訊號、迴圈節奏、logging、關閉流程。
所有判定邏輯一律在 pcs_auto_control_runtime.py，本檔不做任何決策。

🔴 與 auto_monitor_service.py 的關係：**完全分離的兩個服務**
       auto_monitor_service.py    → Report Monitor only
       pcs_auto_control_service.py→ Phase 6 Automatic Control only
   兩者重用 Phase 4/5 的 Named Mutex / DACL / Ownership **技術**，
   但各自持有**不同**的鎖：
       Report  Mutex：ESS_AutoMonitor_Owner_v1_<hash>
       Control Mutex：ESS_PcsAutoControl_Owner_v1_<hash>
   Report Monitor Ownership（誰可以寫報告檔）
     ≠ PCS Control Ownership（哪個行程可以驅動自動控制）
   共用一把鎖會讓「報告服務 crash」等同「控制服務失去所有權」，反之亦然。

🔴 本檔**不得** import 任何具備控制或網路能力的模組：
       device_control_operator / device_control_menu / device_control_scraper /
       pcs_control_executor / report_monitor / charge_discharge_report /
       api_client / requests / phase6_field_measure
   這由測試以 AST（含函式內延後 import）+ sys.modules 快照鎖住。
   ⚠️ 透過 ess_snapshot_adapter 會**間接**載入 decision_engine / safety_gate /
      pcs_control_integration —— 它們是零 I/O 的純邏輯 library（各自的測試已鎖住
      「不 import device_control_operator」），不具備任何控制或網路能力。
      Runtime core（pcs_auto_control_runtime.py）則連它們都不 import，維持完全純粹。

🔴 exception philosophy（與 report monitor 相反，不得照抄）
       report monitor：例外 → 印出 → 下一輪繼續監看（監看不得停擺）
       control service：例外 → **Fail Closed** → FAULT_BLOCKED → 不產生任何指令
   Runtime.tick() 自己就是 Fail Closed 邊界，本檔不再吞例外。

🔴 關閉語意：**relinquish authority only**
   停止軟體服務 ≠ 停止 PCS。正常停止、Windows 更新重啟、人工停止，
   一律**不**送 pcs_stop_power。

D.3-G：Startup Recovery
    Service 可組出「fresh 觀測 + 控制紀錄 → 擁有權判定」的恢復來源。
    🔴 reader / store 一律預設 None —— production 啟動時**不會**連任何設備，
       恢復來源在未注入時回報 Fail Closed，不產生任何網路 I/O。

D.3-E：Execution Chain
    Service 可建立完整執行鏈，但 executor / verifier / LastControl store
    **一律預設 None** —— 結構上不可能送出任何指令，也不可能寫入任何控制紀錄。
    Runtime 亦不傳 dispatch_enabled → 維持模組層預設 False。

D.3-D：Arbitration / Authorization
    Service 可改以 ProductionArbiter 作為 Runtime 的觀測來源。
    🔴 授權成功只會讓 Runtime 進入 COMMAND_PENDING（等待未來的 Executor），
       本階段**沒有** Executor，dispatch 恆為 0。

D.3-C：Decision 觀測（OBSERVE-ONLY）
    Service 建立 ESS Adapter + Meter Adapter + DecisionObserver，
    再把 observer.observe 注入 Runtime。
    🔴 兩個 reader/source 一律預設 None —— D.3-C **不連任何來源**，
       控制服務維持零設備能力，觀測一律 Fail Closed。
    🔴 Runtime 取得的 would_action 只是稽核事實；dispatch 路徑仍不存在。

D.3-B：Adapter 邊界已建立
    Service 持有 reader dependency 並建立 EssSnapshotAdapter，
    再把 adapter.observe 注入 Runtime。Runtime core 不知道資料從哪來。
    🔴 D.3-B **不接上 production reader**：預設 reader=None → 觀測一律
       READER_NOT_CONFIGURED。接上真實 read_all 會把網路能力帶進控制服務，
       屬後續階段的明確工作，本階段刻意不做。

尚未涵蓋（後續階段）
    production reader wiring / D.3-C Decision Wiring / D.3-D Authorization
    D.3-E Executor Wiring / D.3-F LastControl / D.3-G Report Integration

用法（完全離線，不需設備）
    python pcs_auto_control_service.py --status
    python pcs_auto_control_service.py --max-ticks 3
"""

import os
import sys
import time
import signal
import hashlib
import argparse
import threading

import pcs_auto_control_config as CFG
import pcs_auto_control_runtime as RT
import ess_snapshot_adapter as ADP
import meter_observation_adapter as MADP
import decision_observer as DOBS
import production_arbiter as ARB
import production_execution_chain as PEC
import execution_reconciler as REC
import pcs_auto_control_production as PRD

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_NAME = CFG.SERVICE_NAME
DEFAULT_OUTPUT_ROOT = os.path.join(os.path.dirname(HERE), "output")
DEFAULT_INTERVAL_SEC = 10          # ⚠️ 僅為 skeleton 的迴圈節奏，**不是** decision_interval_sec

# ======================================================================
# Named Mutex（技術沿用 Phase 4.4/4.6，鎖本身完全獨立）
# ======================================================================
SYNCHRONIZE = 0x00100000
MUTEX_MODIFY_STATE = 0x0001
MUTEX_MIN_ACCESS = SYNCHRONIZE | MUTEX_MODIFY_STATE
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
SDDL_REVISION_1 = 1

# SY LocalSystem / BA Administrators → ALL_ACCESS；AU Authenticated Users → 僅同步所需。
# 與 Report Monitor 的 DACL 相同強度（不得更寬鬆）。
MUTEX_SDDL = "D:(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)(A;;0x100001;;;AU)"

_own = {"owned": False, "handle": None, "name": None, "reason": None,
        "role": None, "abandoned": False, "thread_id": None}


def _k32():
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexExW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR,
                                     wintypes.DWORD, wintypes.DWORD]
        k.CreateMutexExW.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k.ReleaseMutex.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        k.LocalFree.argtypes = [wintypes.LPVOID]
        k.LocalFree.restype = wintypes.LPVOID
        return k
    except Exception:                                     # noqa: BLE001
        return None


def _advapi32():
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        return a
    except Exception:                                     # noqa: BLE001
        return None


def control_root(root=None):
    """控制服務的作用範圍根目錄（同機不同部署不互相搶鎖）。"""
    base = root if root is not None else DEFAULT_OUTPUT_ROOT
    return os.path.join(base, CFG.CONTROL_OUTPUT_SUBDIR)


def mutex_name(root=None):
    """
    Control Mutex 名稱＝**專屬前綴** + control root 正規化後絕對路徑的雜湊。

    ⚠️ normcase + abspath 缺一不可（Phase 4.4 實機教訓）：Windows 檔案系統
       不分大小寫，`D:\\...` 與 `d:/...` 會算出兩顆不同的 Mutex → 雙 Owner。
    🔴 前綴刻意與 Report Monitor 不同 —— 兩把鎖、兩種所有權語意。
    """
    p = os.path.normcase(os.path.abspath(control_root(root)))
    return CFG.CONTROL_MUTEX_PREFIX + hashlib.md5(p.encode("utf-8")).hexdigest()[:16]


def _build_mutex_security():
    """由 MUTEX_SDDL 建立 SECURITY_ATTRIBUTES；失敗一律 fail closed。"""
    a = _advapi32()
    if a is None:
        return None, None, "no_advapi32"
    try:
        import ctypes
        from ctypes import wintypes

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD),
                        ("lpSecurityDescriptor", wintypes.LPVOID),
                        ("bInheritHandle", wintypes.BOOL)]

        sd = wintypes.LPVOID()
        size = wintypes.DWORD()
        ok = a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            MUTEX_SDDL, SDDL_REVISION_1, ctypes.byref(sd), ctypes.byref(size))
        if not ok or not sd:
            return None, None, str(ctypes.get_last_error())
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = sd
        sa.bInheritHandle = False
        return sa, sd, 0
    except Exception as e:                                # noqa: BLE001
        return None, None, type(e).__name__


def is_owner():
    """本行程是否持有 PCS Control Ownership。**唯一**判定入口。"""
    return bool(_own["owned"])


def ownership_state():
    return {k: v for k, v in _own.items() if k != "handle"}


def acquire_ownership(role="service", root=None):
    """
    取得 PCS Control Ownership。回傳 (is_owner, reason)。**idempotent**。

    reason：already_owner / acquired / abandoned_taken / held_by_other /
            no_win32 / sd_failed_X / create_failed_N / wait_failed_N / api_error_X

    ⚠️ 一切不確定 → fail closed（回 False）。**不得**退回無 DACL 的建立方式。
    ⚠️ 執行緒親和性：Mutex 所有權綁定執行 Wait 的那條執行緒，
       acquire / release 一律在主執行緒。
    """
    if _own["owned"]:
        return True, "already_owner"
    k = _k32()
    if k is None:
        _own.update(reason="no_win32", role=role)
        return False, "no_win32"
    try:
        import ctypes
        name = mutex_name(root)
        sa, sd, sd_err = _build_mutex_security()
        if sa is None:
            _own.update(reason=f"sd_failed_{sd_err}", role=role)
            return False, _own["reason"]
        try:
            h = k.CreateMutexExW(ctypes.byref(sa), name, 0, MUTEX_MIN_ACCESS)
            err = ctypes.get_last_error()
        finally:
            if sd:
                k.LocalFree(sd)
        if not h:
            _own.update(reason=f"create_failed_{err}", role=role)
            return False, _own["reason"]
        rc = k.WaitForSingleObject(h, 0)                  # 非阻塞
        if rc in (WAIT_OBJECT_0, WAIT_ABANDONED):
            _own.update(handle=h, owned=True, role=role, name=name,
                        abandoned=(rc == WAIT_ABANDONED),
                        reason=("abandoned_taken" if rc == WAIT_ABANDONED else "acquired"),
                        thread_id=threading.get_ident())
            if rc == WAIT_ABANDONED:
                print(f"[{SERVICE_NAME}] 前一個 Control Owner 未正常釋放即結束"
                      f"（abandoned）→ 由本行程接管")
            return True, _own["reason"]
        k.CloseHandle(h)
        if rc == WAIT_TIMEOUT:
            _own.update(reason="held_by_other", role=role, name=name)
            return False, "held_by_other"
        err = ctypes.get_last_error()
        _own.update(reason=f"wait_failed_{err}", role=role)
        return False, _own["reason"]
    except Exception as e:                                # noqa: BLE001
        _own.update(reason=f"api_error_{type(e).__name__}", role=role)
        return False, _own["reason"]


def release_ownership():
    """釋放 Control Ownership；非 Owner 一律 no-op（不可能誤釋放他人的鎖）。"""
    if not _own["owned"]:
        return False
    if _own["thread_id"] is not None and _own["thread_id"] != threading.get_ident():
        # Mutex 只能由取得它的那條執行緒釋放
        print(f"[{SERVICE_NAME}] 非取得所有權的執行緒 → 不釋放（避免破壞 Mutex 語意）")
        return False
    k = _k32()
    if k is None or not _own["handle"]:
        _own.update(owned=False, handle=None)
        return False
    try:
        k.ReleaseMutex(_own["handle"])
        k.CloseHandle(_own["handle"])
    except Exception:                                     # noqa: BLE001
        pass
    _own.update(owned=False, handle=None, reason="released", thread_id=None)
    return True


# ======================================================================
# Logging（最小可用；不引入既有報告服務的 log 檔）
# ======================================================================
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][{SERVICE_NAME}] {msg}", flush=True)


# ======================================================================
# 訊號
# ======================================================================
def install_signal_handlers(stop_event):
    installed = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_a: stop_event.set())
            installed.append(name)
        except (ValueError, OSError):
            pass                                          # 非主執行緒或平台不支援
    return installed


# ======================================================================
# 主迴圈
# ======================================================================
def build_adapter(reader=None, clock=None):
    """
    建立 ESS 觀測 Adapter。

    🔴 reader 預設 None —— 不接 production reader，
       觀測一律回 READER_NOT_CONFIGURED（Fail Closed），控制服務維持零設備能力。
    """
    return ADP.EssSnapshotAdapter(reader=reader, clock=clock)


def build_meter_adapter(source=None, clock=None):
    """
    建立電表觀測 Adapter。

    🔴 source 預設 None —— 不接 production 電表來源（Fail Closed）。
    """
    return MADP.MeterObservationAdapter(source=source, clock=clock)


def build_decision_observer(reader=None, meter_source=None, policy=None,
                            holiday_provider=None, clock=None, local_now=None):
    """
    建立 OBSERVE-ONLY 決策觀測器。

    🔴 policy 預設使用 decision_policy 的預設值 —— 充放電功率為 None，
       Decision 會 Fail Closed 成 no_action。**不得**在此填入任何測試值。
    🔴 holiday_provider 預設 None → TOU 一律 UNKNOWN（正式離峰日來源未確認）。
    🔴 本函式不建立任何 executor / authorization / LastControl。
    """
    # ⚠️ clock 一路貫穿 ESS / Meter / Observer —— 三者必須是同一個時間基準，
    #    否則 freshness 會跨時基相減，算出無意義（甚至負數）的 age。
    return DOBS.DecisionObserver(ess_adapter=build_adapter(reader, clock),
                                 meter_adapter=build_meter_adapter(meter_source,
                                                                   clock),
                                 policy=policy,
                                 holiday_provider=holiday_provider,
                                 clock=clock, local_now=local_now)


def build_arbiter(reader=None, meter_source=None, config=None, policy=None,
                  holiday_provider=None, last_control_provider=None,
                  clock=None, local_now=None):
    """
    建立仲裁／授權層。

    🔴 config 預設為 production 預設值 —— 全部 None → AUTHORIZATION_NOT_READY。
    🔴 last_control_provider 預設 None：D.3-D **只讀不寫** LastControl，
       且尚未接上正式儲存 → 運轉中的 PCS 一律 EXTERNAL（Fail Closed）。
    🔴 本函式不建立任何 executor。
    """
    return ARB.ProductionArbiter(
        observer=build_decision_observer(reader, meter_source, policy,
                                         holiday_provider, clock, local_now),
        config=config if config is not None else CFG.DEFAULT_CONTROL_CONFIG,
        last_control_provider=last_control_provider, clock=clock)


def build_execution_chain(reader=None, meter_source=None, config=None,
                          policy=None, holiday_provider=None,
                          last_control_provider=None, executor=None,
                          verifier=None, store=None, clock=None, local_now=None):
    """
    建立完整執行鏈。

    🔴 executor / verifier / store **一律預設 None** ——
       production 不注入任何實機出口，結構上不可能送出指令或寫入控制紀錄。
       離線測試才注入 Fake。
    """
    return PEC.ProductionExecutionChain(
        arbiter=build_arbiter(reader, meter_source, config, policy,
                              holiday_provider, last_control_provider,
                              clock, local_now),
        executor=executor, verifier=verifier, store=store)


def build_production_stack(client=None, meter_client=None, config=None,
                           mode=PRD.MODE_OBSERVE_ONLY, store_path=None,
                           reader=None, meter_source=None,
                           last_control_provider=None,
                           clock=None, local_now=None):
    """
    Phase D.5-B：把**全部** production 相依明確接起來，回傳
    (observation_source, recovery_source, wiring_report)。

    接上的東西
        ESS reader          ← charge_discharge_report.read_all（唯讀 GET）
        Meter source        ← MeterClient.get_snapshot（唯讀訂閱）
        Tariff / TOU        ← 已人工驗收並 provision 的年度離峰日清單
        Decision policy     ← production config 的充放電功率（目前 None → no_action）
        Safety Gate         ← 由 arbiter 內部沿用既有實作
        Control Authority   ← 由 arbiter 內部沿用既有實作
        LastControl         ← 正式儲存，**唯讀**（current()，不寫入）
        Layer 1 Interlock   ← 由 pcs_control_integration 無條件生效
        ReadBack config     ← timeout 75.0 / poll 5.0 / samples 1（皆 FINAL）

    🔴 **不接上的東西（刻意）**
        executor / verifier —— OBSERVE_ONLY 一律不建立（build_executor 回 None）。
        因此「不送指令」是結構事實，不是旗標判斷。
    🔴 client / meter_client 預設 None → 對應來源 Fail Closed，**不會連線**。
    🔴 本函式不 import phase6_field_measure —— 那是量測工具，不是 production 元件。
    """
    cfg = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    # reader / meter_source 可由呼叫端直接注入（離線測試用）；
    # 未注入時才由 client 衍生 —— 兩條路徑走同一組裝，不另建測試專用鏈。
    reader = reader if reader is not None else PRD.build_ess_reader(client)
    meter_source = (meter_source if meter_source is not None
                    else PRD.build_meter_source(meter_client))
    holiday = PRD.build_holiday_provider()
    policy = PRD.build_policy(cfg)
    store = PRD.build_last_control_store(store_path)
    lc_provider = (last_control_provider if last_control_provider is not None
                   else PRD.build_last_control_provider(store))
    executor = PRD.build_executor(mode)          # OBSERVE_ONLY → None
    verifier = PRD.build_verifier(mode)          # OBSERVE_ONLY → None

    chain = build_execution_chain(
        reader=reader, meter_source=meter_source, config=cfg, policy=policy,
        holiday_provider=holiday, last_control_provider=lc_provider,
        executor=executor, verifier=verifier, store=None,
        clock=clock, local_now=local_now)
    recovery = build_recovery_source(reader=reader, store=store, config=cfg,
                                     clock=clock)
    report = PRD.wiring_report(
        reader=reader, meter_source=meter_source,
        last_control_provider=lc_provider, executor=executor,
        verifier=verifier, holiday_provider=holiday, config=cfg, mode=mode)
    return chain.run, recovery, report


def build_recovery_source(reader=None, store=None, config=None, clock=None):
    """
    組出啟動恢復來源：fresh 觀測 + durable 控制紀錄 → 擁有權判定。

    🔴 reader / store 預設 None —— production 啟動**不連設備、不讀檔**，
       一律回 Fail Closed 的判定結果。實機接線屬後續明確授權的工作。
    🔴 本函式產生的可呼叫物件**只讀不寫**，且不可能送出任何指令。
    """
    cfg = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    adapter = build_adapter(reader, clock)
    reconciler = REC.ExecutionReconciler(
        authority_policy=ARB.CA.AuthorityPolicy(
            authority_ttl_sec=cfg.authority_ttl_sec,
            authority_power_tolerance_kw=cfg.authority_power_tolerance_kw))

    def _recover():
        obs = adapter.observe()
        rec, trust, why = None, None, None
        if store is not None:
            cur = store.current()
            rec, trust, why = cur.record, cur.trust, cur.reason
        return reconciler.reconcile(obs, rec, trust, store_reason=why)

    return _recover


def run(runtime=None, interval=DEFAULT_INTERVAL_SEC, stop_event=None,
        max_ticks=None, owner=None):
    """
    Skeleton 主迴圈（跑在呼叫端執行緒，**不另開 thread**）。回傳實際輪數。

    owner : 覆寫 ownership 判定（測試用）。None = 使用真實 is_owner()。

    ⚠️ 本迴圈**不吞例外**：Runtime.tick() 自己就是 Fail Closed 邊界，
       它保證不拋出，並在未知錯誤時把狀態壓成 FAULT_BLOCKED。
       若連 tick() 都拋出，那是不變量被破壞 —— 應該讓服務停止，而不是繼續控制設備。
    """
    rt = runtime if runtime is not None else RT.AutoControlRuntime()
    stop_event = stop_event or threading.Event()
    ticks = 0
    try:
        while not stop_event.is_set():
            own = is_owner() if owner is None else bool(owner)
            res = rt.tick(RT.CycleInputs(is_owner=own))
            log(str(res))
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            stop_event.wait(interval)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，準備關閉…")
        stop_event.set()
    return ticks


def shutdown(runtime):
    """
    關閉流程：停止核發新判定 → 釋放 Ownership。**不送任何 PCS 指令**。

    🔴 停止軟體服務 ≠ 停止 PCS。若未來需要「停服務同時停 PCS」，
       必須是明確的 operator action / explicit flag，不得成為預設行為。
    """
    res = runtime.shutdown()
    log(str(res))
    log("關閉語意：relinquish authority only —— 未送出 pcs_stop_power")
    release_ownership()
    return res


def print_startup(rt, installed, why, root=None, wiring=None):
    c = rt.config
    print("=" * 70)
    print(f"{SERVICE_NAME} 啟動  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print(f"  Control Mutex   : {mutex_name(root)}")
    print(f"  Ownership       : {'OWNER' if is_owner() else 'NOT OWNER'}（{why}）")
    print(f"  訊號處理        : {', '.join(installed) or '（無）'}")
    print(f"  DISPATCH_ENABLED: {RT.DISPATCH_ENABLED}"
          f"   ← False：結構上不可能送出任何 PCS 指令")
    print(f"  config 就緒     : {c.dispatch_ready}")
    print(f"  缺少的參數      : {list(c.missing_required())}")
    print(f"  Runtime 狀態    : {rt.state}")
    print(f"  觀測來源        : {'已注入' if rt.snapshot()['has_observation_source'] else '無'}")
    print(f"  dispatch_enabled: {rt.snapshot()['dispatch_enabled_instance']}")
    log(str(rt.recover()))          # 啟動恢復（未接來源 → Fail Closed）
    if wiring is not None:
        # ---- D.5-B：啟動就緒盤點（只回報，不修補、不放行）----
        print("-" * 70)
        print(f"  Wiring          : {wiring.mode}")
        for k, v in sorted(wiring.sources.items()):
            print(f"    {k:<18}{v}")
        rb = wiring.readback
        print(f"  ReadBack        : timeout={rb.timeout_sec} "
              f"poll={rb.poll_interval_sec} samples={rb.stability_samples} "
              f"ready={rb.ready}")
        print(f"  Layer 1         : ENABLED（狀態互鎖，無條件生效）")
        print(f"  Layer 2         : DISABLED / PARAMETER NOT PROVISIONED"
              f"（min_switch_interval_sec={c.min_switch_interval_sec}）")
        print(f"  年度離峰日      : {list(PRD.build_holiday_provider().known_years)}")
        print(f"  can_dispatch    : {wiring.can_dispatch}")
    print("  ⚠ OBSERVE_ONLY：executor / verifier 未建立，operator 未 import —— "
          "沒有任何可被呼叫的控制出口。")
    print("=" * 70)


def main(argv=None):
    ap = argparse.ArgumentParser(description="PCS 自動控制服務（D.3-A Runtime Skeleton）")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                    help="skeleton 迴圈間隔秒數（不是 decision_interval_sec）")
    ap.add_argument("--max-ticks", type=int, default=None, help="達到輪數即結束（測試用）")
    ap.add_argument("--status", action="store_true", help="只顯示狀態後結束")
    ap.add_argument("--root", default=None, help="覆寫 output root（測試用）")
    args = ap.parse_args(argv)

    # Service 持有 reader / meter source dependency；
    # Runtime core 只拿到一個可呼叫物件，不知道資料從哪來。
    # D.5-B：production path 明確接線。client / meter_client 未提供時
    # 對應來源 Fail Closed —— 連線是明確操作，不是預設行為。
    obs_source, recovery, wiring = build_production_stack(
        client=None, meter_client=None)
    rt = RT.AutoControlRuntime(observation_source=obs_source,
                               recovery_source=recovery)

    if args.status:
        print(f"  Control Mutex   : {mutex_name(args.root)}")
        print(f"  ESS 觀測        : reader 未接 → {ADP.OBS_READER_NOT_CONFIGURED}")
        print(f"  電表觀測        : source 未接 → {MADP.MOBS_SOURCE_NOT_CONFIGURED}")
        print(f"  coherence 門檻  : {DOBS.COHERENCE_THRESHOLD_SEC}（UNCONFIGURED）")
        print(f"  授權必要參數    : {list(ARB.AUTHORIZATION_REQUIRED_PARAMS)}")
        print(f"  授權缺少參數    : "
              f"{list(ARB.ProductionArbiter(config=rt.config)._missing_config())}")
        print(f"  Wiring          : {wiring}")
        for _k, _v in sorted(wiring.sources.items()):
            print(f"    {_k:<18}{_v}")
        _rb = wiring.readback
        print(f"  ReadBack        : timeout={_rb.timeout_sec} "
              f"poll={_rb.poll_interval_sec} samples={_rb.stability_samples} "
              f"ready={_rb.ready}")
        print(f"  Layer 2         : min_switch_interval_sec="
              f"{rt.config.min_switch_interval_sec}（DEFERRED）")
        print(f"  DISPATCH_ENABLED: {RT.DISPATCH_ENABLED}")
        print(f"  config 就緒     : {rt.config.dispatch_ready}")
        print(f"  缺少的參數      : {list(rt.config.missing_required())}")
        print(f"  can_dispatch    : {wiring.can_dispatch}")
        # 跑一輪 OBSERVE_ONLY，示範完整可稽核紀錄
        print(f"  觀測示例        : {PRD.observe_record(obs_source())}")
        return 0

    stop_event = threading.Event()
    installed = install_signal_handlers(stop_event)

    ok, why = acquire_ownership(role="service", root=args.root)
    if not ok:
        print_startup(rt, installed, why, args.root, wiring=wiring)
        log(f"未取得 PCS Control Ownership（{why}）→ fail closed，不啟動。")
        return 0 if why == "held_by_other" else 1

    print_startup(rt, installed, why, args.root, wiring=wiring)
    try:
        n = run(rt, interval=args.interval, stop_event=stop_event,
                max_ticks=args.max_ticks)
    finally:
        shutdown(rt)
    log(f"已停止（共執行 {n} 輪）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
