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
import control_authority as CA              # 只用其狀態常數（純邏輯、零 I/O）
import pcs_control_integration as PCI       # 只用方向互鎖的 BLOCK 理由集合

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


# ======================================================================
# Phase 6.9：Controlled Single-Leg LIVE
# ======================================================================
# 🔴 `--live-leg` **不代表立即動作** —— 它只代表「本 process 最多允許執行
#    一次該方向的 leg」。真正 dispatch 仍須同時滿足 Decision / Safety /
#    Authority / Interlock / fresh precheck / 一次性控制授權 / leg 授權。
#    缺一不可 → NO DISPATCH。
#
# 🔴 授權**不落盤**：隨 process 生命週期存在，服務重啟即消失，
#    預設回到 OBSERVE_ONLY。不採 config flag、不採環境變數。
LEG_ARMED = "ARMED"
LEG_PRECHECKED = "PRECHECKED"
LEG_AUTHORIZED = "AUTHORIZED"
LEG_DISPATCHED = "DISPATCHED"
LEG_VERIFIED = "VERIFIED"
LEG_OWNED = "OWNED"
LEG_STOP_REQUESTED = "STOP_REQUESTED"
LEG_IDLE_VERIFIED = "IDLE_VERIFIED"
LEG_REPORT_FINALIZED = "REPORT_FINALIZED"
LEG_COMPLETE = "COMPLETE"
LEG_OBSERVE_ONLY = "OBSERVE_ONLY"
LEG_ABORTED = "ABORTED"
# 🔴 已 dispatch 但擁有權不再可信 —— **不自動 STOP**。
#    對 ownership 不確定的設備送 STOP 本身也是新的控制行為，
#    可能中斷外部操作者。一律 STOP AND REPORT，交由人工處置。
LEG_ABORTED_UNCERTAIN = "ABORTED_UNCERTAIN_OWNERSHIP"

LEG_STATES = (LEG_ARMED, LEG_PRECHECKED, LEG_AUTHORIZED, LEG_DISPATCHED,
              LEG_VERIFIED, LEG_OWNED, LEG_STOP_REQUESTED, LEG_IDLE_VERIFIED,
              LEG_REPORT_FINALIZED, LEG_COMPLETE, LEG_OBSERVE_ONLY,
              LEG_ABORTED, LEG_ABORTED_UNCERTAIN)
LEG_TERMINAL = frozenset({LEG_COMPLETE, LEG_OBSERVE_ONLY, LEG_ABORTED,
                          LEG_ABORTED_UNCERTAIN})

# 已 dispatch 之後，這些 Authority 狀態代表「不能再證明設備由我方控制」
UNCERTAIN_AUTHORITY = frozenset({"UNKNOWN", "EXTERNAL_OR_UNKNOWN", "CONFLICT"})

# fresh precheck 的 16 項（人工確認**之後**、dispatch **之前**重新取得）
PRECHECK_ITEMS = (
    "meter_valid", "meter_fresh", "ess_comm_ok", "pcs_idle", "soc_sane",
    "no_pcs_fault", "no_active_alarm", "alarm_source_complete",
    "schedule_off", "manual_switch_ok", "authority_idle", "no_other_operator",
    "no_report_session", "dispatch_ready", "calendar_ready", "tou_valid")


def confirm_live_leg(leg, prompt=None):
    """
    人工確認。**必須輸入完整字串**（例：CONFIRM CHARGE 5KW）。

    🔴 `--confirm` 本身**不代表確認完成** —— 它只表示「願意進入確認流程」。
    🔴 輸入錯誤 → 立即回 False：不建立 executor、不啟用 dispatch。
    ⚠️ 刻意不接受 y / yes / 任意非空字串。
    """
    want = PRD.confirmation_phrase(leg)
    print("=" * 70)
    print(f"  LIVE LEG      : {leg.action.upper()}")
    print(f"  POWER         : {leg.power_kw:g} kW（來源：ProductionConfig）")
    print(f"  CURRENT MODE  : {PRD.MODE_OBSERVE_ONLY}")
    print("  SCHEDULE      : 需為 OFF（fresh precheck 會再次確認）")
    print(f"  MAX POWER     : {CFG.DEFAULT_CONTROL_CONFIG.max_power_kw:g} kW"
          "（Safety Gate 上限，**不是**操作目標）")
    print("=" * 70)
    print(f"  請輸入下列字串以確認（其他任何輸入一律中止）：")
    print(f"    {want}")
    got = (prompt or input)("  > ")
    ok = isinstance(got, str) and got.strip() == want
    print("  確認" + ("通過" if ok else "**失敗** → 中止，不建立任何控制出口"))
    return ok


def live_leg_precheck(observation, meter_snapshot=None, report_session=None,
                      config=None):
    """
    dispatch 前的 fresh precheck。回傳 (ok, results_dict)。

    🔴 **必須使用人工確認之後重新取得的觀測** —— 不得重用確認前的快照。
    🔴 任一項 FAIL → 不得建立或使用 dispatch authorization。
    """
    c = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    arb = getattr(observation, "arbitration", None) or observation
    pcs = getattr(arb, "pcs_state", None)
    auth = getattr(arb, "authority_state", None)
    age = getattr(meter_snapshot, "age_sec", None)
    soc = getattr(arb, "soc_percent", None)
    r = {
        "meter_valid": bool(getattr(meter_snapshot, "valid", False)),
        "meter_fresh": bool(age is not None and age == age
                            and not getattr(meter_snapshot, "stale", True)),
        "ess_comm_ok": getattr(arb, "valid", None) is True,
        "pcs_idle": pcs in ("STANDBY", "STOPPED"),
        "soc_sane": isinstance(soc, (int, float)) and 0.0 <= float(soc) <= 100.0,
        "no_pcs_fault": getattr(arb, "pcs_fault", False) is not True,
        "no_active_alarm": getattr(arb, "active_alarm_count", 0) in (0, None),
        "alarm_source_complete": getattr(arb, "alarm_source_complete", None) is True,
        "schedule_off": getattr(arb, "schedule_switch", None) in (0, "0", False),
        "manual_switch_ok": getattr(arb, "manual_switch", None) in (1, "1", True),
        "authority_idle": auth == "IDLE",
        "no_other_operator": auth != "EXTERNAL_OR_UNKNOWN",
        "no_report_session": report_session is None,
        "dispatch_ready": c.dispatch_ready is True,
        "calendar_ready": bool(PRD.build_holiday_provider().known_years),
        "tou_valid": getattr(arb, "tou_state", None) not in (None, "UNKNOWN"),
    }
    return all(r.get(k) for k in PRECHECK_ITEMS), r


class ControlledLiveLeg(object):
    """
    受控單次 leg 的狀態機。**一個 process 只跑一次，結束即回 OBSERVE_ONLY。**

    🔴 本類別不自行決定要不要動 —— 只在上游全部放行時把既有執行鏈跑一次；
       任何一層擋下就直接收斂到終態。不 sleep、不重試、不自動補送。
    🔴 已 dispatch 後若 Authority 轉為不可信 → ABORTED_UNCERTAIN_OWNERSHIP，
       **不自動 STOP**。
    """

    def __init__(self, leg, chain_factory, observe_source):
        self.leg = leg
        self._chain_factory = chain_factory      # (executor, verifier) -> chain
        self._observe = observe_source
        self.state = LEG_ARMED
        self.history = [LEG_ARMED]
        self.dispatch_count = 0
        self.stop_count = 0
        self.abort_reason = None
        self.executor = None
        self.verifier = None

    def _to(self, st, reason=None):
        self.state = st
        self.history.append(st)
        if reason:
            self.abort_reason = reason
        return st

    def _teardown(self):
        """銷毀控制出口 —— 回到「結構上送不出指令」的狀態。"""
        self.executor = None
        self.verifier = None

    def abort(self, reason, uncertain=False):
        self.leg.abort(reason)
        self._to(LEG_ABORTED_UNCERTAIN if uncertain else LEG_ABORTED, reason)
        self._teardown()
        return self.state

    def arm(self, executor, verifier):
        """fresh precheck 通過後才拿到出口。"""
        if not self.leg.armed:
            return self.abort("leg 授權已失效")
        if executor is None or verifier is None:
            return self.abort("控制出口未建立")
        self.executor, self.verifier = executor, verifier
        return self._to(LEG_PRECHECKED)

    def run_leg(self):
        """跑一次完整 leg，回傳最終狀態。"""
        if self.state != LEG_PRECHECKED:
            return self.abort(f"狀態不正確：{self.state}")
        chain = self._chain_factory(self.executor, self.verifier)

        # ---- 方向指令 ----
        res = chain.run()
        act = getattr(res, "control_action", None)
        if act != self.leg.action:
            return self.abort(f"決策動作 {act!r} 與 leg 授權 "
                              f"{self.leg.action!r} 不符 → 不執行")
        if not getattr(res, "executed", False):
            return self.abort(f"未送出：{getattr(res, 'outcome', None)}")
        self.dispatch_count += 1
        # 🔴 **不得**在這裡重問 leg.allows(act)：真正的 leg 授權閘在
        #    LegBoundExecutor（operator 之前），它放行時就已 mark_dispatched。
        #    事後再問一次，得到的必然是「已消費」——那會把每一次**成功的**
        #    方向指令都判成 abort，STOP 永遠送不出去，設備留在充電狀態。
        #    這裡改為驗證「出口確實把這次送出記進了 leg」：
        #    若沒有，代表注入的不是 leg-bound executor，指令已在無授權閘的
        #    情況下送出 → 擁有權不可信，停止一切後續指令並交人工處置。
        if not self.leg.dispatched_direction:
            return self.abort("控制出口未經 leg 授權閘（送出未被記錄）"
                              " → 不再送出任何指令", uncertain=True)
        self._to(LEG_DISPATCHED)
        if getattr(res, "readback_outcome", None) != "VERIFY_SUCCESS":
            # 🔴 回讀失敗**不重試方向指令**（沿用既有契約）
            return self.abort("ReadBack 未成功："
                              f"{getattr(res, 'readback_outcome', None)}")
        self._to(LEG_VERIFIED)
        if not getattr(res, "lastcontrol_written", False):
            return self.abort("LastControl 未寫入")
        self._to(LEG_OWNED)

        # ---- STOP（同一 leg 的收尾）----
        obs = self._observe()
        arb = getattr(obs, "arbitration", None) or obs
        auth = getattr(arb, "authority_state", None)
        if auth is None or auth in UNCERTAIN_AUTHORITY:
            return self.abort(f"dispatch 後擁有權不可信（authority={auth}）",
                              uncertain=True)
        if auth != "OWNED_BY_PHASE6":
            return self.abort(f"非 OWNED（{auth}）→ 不得送 STOP", uncertain=True)
        self._to(LEG_STOP_REQUESTED)
        stop_res = chain.run()
        if getattr(stop_res, "control_action", None) != "stop":
            return self.abort("未形成 STOP leg", uncertain=True)
        if not getattr(stop_res, "executed", False):
            return self.abort("STOP 未送出", uncertain=True)
        self.stop_count += 1
        # 同上：STOP 的授權閘一樣在 LegBoundExecutor，本層只驗證有被記錄。
        if not self.leg.dispatched_stop:
            return self.abort("STOP 未經 leg 授權閘（送出未被記錄）", uncertain=True)
        if getattr(stop_res, "readback_outcome", None) != "VERIFY_SUCCESS":
            return self.abort("STOP 回讀未成功", uncertain=True)
        self._to(LEG_IDLE_VERIFIED)
        # 報告收尾沿用既有 idle debounce / should_auto_end，本層不催、不代勞
        self._to(LEG_REPORT_FINALIZED)
        self.leg.consume()
        self._to(LEG_COMPLETE)
        self._teardown()
        self._to(LEG_OBSERVE_ONLY)
        return self.state

    def snapshot(self):
        return {"state": self.state, "history": list(self.history),
                "dispatch_count": self.dispatch_count,
                "stop_count": self.stop_count,
                "abort_reason": self.abort_reason,
                "leg": self.leg.as_dict(),
                "executor_present": self.executor is not None,
                "verifier_present": self.verifier is not None}


# ======================================================================
# Phase 6.9-A：CLI Entry Wiring
# ======================================================================
# 🔴 這裡是**唯一**會把 `--live-leg` 接到 ControlledLiveLeg 的地方。
#    順序由裁示固定，且順序本身就是安全性質，不得為了「程式比較好寫」調換：
#
#        人工確認  →  fresh read  →  fresh 自然 Decision
#                  →  fresh precheck / Safety / Authority / Interlock
#                  →  LiveLegAuthorization
#                  →  executor / verifier
#                  →  ControlledLiveLeg
#
#    確認**之前**取得的任何觀測一律不得作為 dispatch 依據；
#    授權與控制出口一律在全部 fresh 閘門通過**之後**才存在。
#
# 🔴 `--live-leg` 是 permission，不是 decision override。
#    自然 Decision 不是該方向 → NO DISPATCH，沒有例外。
LIVE_R_NOT_REQUESTED = "LIVE_NOT_REQUESTED"
LIVE_R_NO_CONFIRM_FLAG = "LIVE_CONFIRM_FLAG_MISSING"
LIVE_R_CONFIRM_FAILED = "LIVE_CONFIRMATION_FAILED"
LIVE_R_OBSERVATION_INVALID = "LIVE_FRESH_OBSERVATION_INVALID"
LIVE_R_DECISION_NOT_LEG = "LIVE_NATURAL_DECISION_NOT_LEG_ACTION"
LIVE_R_PRECHECK_FAILED = "LIVE_FRESH_PRECHECK_FAILED"
LIVE_R_SAFETY_BLOCKED = "LIVE_SAFETY_BLOCKED"
LIVE_R_AUTHORITY_BLOCKED = "LIVE_AUTHORITY_NOT_IDLE"
LIVE_R_INTERLOCK_BLOCKED = "LIVE_DIRECTION_INTERLOCK_BLOCKED"
LIVE_R_NO_OPERATOR = "LIVE_OPERATOR_NOT_INJECTED"
LIVE_R_EXIT_NOT_BUILT = "LIVE_CONTROL_EXIT_NOT_BUILT"
LIVE_R_ALREADY_USED = "LIVE_LEG_ALREADY_USED_IN_THIS_PROCESS"

# 🔴 process 級 one-shot 守門。
#    `live_leg_main` 本來就是「一個 leg 就結束 process」，但 one-shot 不該
#    只靠 process 邊界成立 —— 只要本 process 曾經核發過 leg 授權，
#    就不得再核發第二次（不論同向、反向、或前一次是否成功）。
#    ⚠️ 記憶體內狀態，**不落盤**：新 process 自然回到可申請的初始狀態。
_live_leg_guard = {"used": False, "action": None, "final_state": None}


def live_leg_guard_state():
    """目前 process 的 one-shot 守門狀態（唯讀）。"""
    return dict(_live_leg_guard)


def reset_live_leg_guard():
    """
    ⚠️ **測試專用**。清掉 process 級 one-shot 記錄，讓各測試彼此獨立。

    🔴 production 路徑（main / live_leg_main / run_live_leg_cli）**不得**呼叫，
       此事由 regression 以 AST 驗證。
    """
    _live_leg_guard.update(used=False, action=None, final_state=None)

# dispatch 前 Authority 唯一可接受的狀態。
# 🔴 OWNED 不算 —— 那代表上一次控制尚未收斂，不是乾淨的起點。
LIVE_AUTHORITY_OK = (CA.AUTH_IDLE,)


class LiveLegOutcome(object):
    """CLI LIVE 路徑的完整稽核紀錄（不含任何憑證資訊）。"""

    def __init__(self, action=None, requested=False, confirmed=False,
                 reason=LIVE_R_NOT_REQUESTED, gates=None, precheck=None,
                 leg_state=None, runner=None, executor_built=False,
                 verifier_built=False, dispatch_count=0, stop_count=0,
                 detail=""):
        self.action = action
        self.requested = requested
        self.confirmed = confirmed
        self.reason = reason
        self.gates = dict(gates or {})
        self.precheck = dict(precheck or {})
        self.leg_state = leg_state
        self.runner = runner
        self.executor_built = executor_built
        self.verifier_built = verifier_built
        self.dispatch_count = dispatch_count
        self.stop_count = stop_count
        self.detail = detail

    @property
    def dispatched(self):
        return self.dispatch_count > 0

    def as_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != "runner"}
        d["dispatched"] = self.dispatched
        return d

    def __str__(self):
        return (f"[LIVE {self.action}] {self.reason} "
                f"confirmed={self.confirmed} exit_built={self.executor_built} "
                f"dispatch={self.dispatch_count} stop={self.stop_count} "
                f"leg_state={self.leg_state}")


class LiveChainBundle(object):
    """一組共用相依，能依需要生出「有／沒有控制出口」的兩種執行鏈。"""

    def __init__(self, factory, observe, reader, store):
        self.factory = factory        # (executor, verifier) -> chain
        self.observe = observe        # zero-arg -> ExecutionResult（無出口）
        self.reader = reader
        self.store = store


def build_live_chain_bundle(client=None, meter_client=None, config=None,
                            store_path=None, reader=None, meter_source=None,
                            last_control_provider=None, clock=None,
                            local_now=None):
    """
    LIVE 路徑的相依組裝。與 build_production_stack 共用**同一組** builder，
    差別只在於「能不能依需要注入控制出口」。

    🔴 `observe()` 產生的鏈 executor / verifier / store **一律 None** ——
       所有 fresh 閘門判定都跑在結構上不可能送出指令、也不可能寫入
       LastControl 的鏈上。
    🔴 store 只在真的有 executor 時才接上（VERIFY_SUCCESS 才寫）。
    """
    cfg = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    reader = reader if reader is not None else PRD.build_ess_reader(client)
    meter_source = (meter_source if meter_source is not None
                    else PRD.build_meter_source(meter_client))
    holiday = PRD.build_holiday_provider()
    policy = PRD.build_policy(cfg)
    store = PRD.build_last_control_store(store_path)
    lc_provider = (last_control_provider if last_control_provider is not None
                   else PRD.build_last_control_provider(store))

    # 🔴 arbiter **只建立一次**，之後每個 chain 共用同一個。
    #    仲裁層底下的 DecisionObserver 持有 PowerClassifier，而它是**有狀態**的：
    #    stable 狀態靠 time-based debounce 跨多次 update 才會成立。
    #    若每次呼叫都重建 arbiter，分類器永遠停在「第一次 update」，
    #    grid_state 恆為 UNKNOWN → 決策恆為 no_action → 自然 Decision
    #    永遠不可能是 charge/discharge，且 dispatch 後的擁有權複驗也永遠
    #    拿不到 authority（None）→ 會被判成 ABORTED_UNCERTAIN_OWNERSHIP。
    #    build_production_stack 本來就是「建一次鏈、重複呼叫 chain.run」，
    #    這裡必須維持同樣語意，只在需要時換上 executor / verifier。
    arbiter = build_arbiter(reader, meter_source, cfg, policy,
                            holiday, lc_provider, clock, local_now)

    def factory(executor=None, verifier=None):
        return PEC.ProductionExecutionChain(
            arbiter=arbiter, executor=executor, verifier=verifier,
            store=(store if executor is not None else None))

    return LiveChainBundle(factory, lambda: factory(None, None).run(),
                           reader, store)


def build_live_operator_run():
    """
    真正的控制出口函式。**只有 CLI 明確走 LIVE 路徑時才會被呼叫。**

    🔴 刻意在函式內部 import —— 模組層永遠不 import operator，
       因此 OBSERVE_ONLY 執行期間「送不出指令」仍是結構事實。
    """
    import device_control_operator as OPR
    return OPR.run


def live_leg_gates(result, meter_snapshot=None, report_session=None,
                   config=None, action=None):
    """
    確認**之後**取得的 fresh 觀測 → 各閘門判定。純函式，不做任何 I/O。

    回傳 (ok, gates, precheck)。gates 逐項保留，讓失敗原因可稽核。
    """
    arb = getattr(result, "arbitration", None) or result
    pre_ok, precheck = live_leg_precheck(result, meter_snapshot=meter_snapshot,
                                         report_session=report_session,
                                         config=config)
    decision = getattr(arb, "fresh_decision_action", None)
    direction_reason = getattr(arb, "direction_reason", None)
    gates = {
        # 🔴 自然 Decision 必須自己形成該方向；--live-leg 不能把它變出來。
        "natural_decision_is_leg_action": decision == action,
        "fresh_precheck": bool(pre_ok),
        "safety_pass": getattr(arb, "safety_allowed", None) is True,
        "authority_ok": getattr(arb, "authority_state", None) in LIVE_AUTHORITY_OK,
        # Fail Closed：未評估（None）不算通過
        "interlock_pass": (direction_reason is not None
                           and direction_reason not in PCI.DIRECTION_BLOCK_REASONS),
    }
    gates["_observed_decision"] = decision
    gates["_authority_state"] = getattr(arb, "authority_state", None)
    gates["_safety_reason"] = getattr(arb, "safety_reason", None)
    gates["_direction_reason"] = direction_reason
    ok = all(v for k, v in gates.items() if not k.startswith("_"))
    return ok, gates, precheck


def _gate_failure_reason(gates):
    """把第一個未通過的閘門對映成明確 reason（順序＝裁示的檢查順序）。"""
    # ⚠️ 順序＝裁示的檢查順序。precheck 放最後**不是**因為它比較不重要，
    #    而是它與 Safety / Authority 有重疊項（例如 authority_idle）：
    #    專屬閘門先報專屬原因，才不會把 Authority 問題含糊成一句「precheck 失敗」。
    #    所有閘門仍**全部必須通過**（見 live_leg_gates 的 all(...)），順序只影響回報。
    for key, reason in (("natural_decision_is_leg_action", LIVE_R_DECISION_NOT_LEG),
                        ("safety_pass", LIVE_R_SAFETY_BLOCKED),
                        ("authority_ok", LIVE_R_AUTHORITY_BLOCKED),
                        ("interlock_pass", LIVE_R_INTERLOCK_BLOCKED),
                        ("fresh_precheck", LIVE_R_PRECHECK_FAILED)):
        if not gates.get(key):
            return reason
    return None


def run_live_leg_cli(action, bundle, config=None, prompt=None,
                     operator_run=None, meter_snapshot_source=None,
                     report_session_source=None, verbose=True):
    """
    受控單次 LIVE 的完整入口。回傳 LiveLegOutcome。

    🔴 **唯一**會建立 executor / verifier 的地方，而且一定在最後才建立。
    🔴 任何一步失敗 → 不建立出口、不 dispatch、直接回報。
    ⚠️ operator_run 未注入 → 一律不建立出口（離線測試注入 Fake）。
    """
    cfg = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
    say = (lambda m: print(m)) if verbose else (lambda m: None)

    # ---- ⓪ process 級 one-shot：本 process 曾核發過 leg → 一律不再核發 ----
    if _live_leg_guard["used"]:
        say(f"[LIVE] 本 process 已使用過 leg 授權"
            f"（{_live_leg_guard['action']} → {_live_leg_guard['final_state']}）"
            f" → 拒絕，不再進入確認流程。")
        return LiveLegOutcome(action=action, requested=True, confirmed=False,
                              reason=LIVE_R_ALREADY_USED,
                              detail="one-shot：同一 process 不得再次核發 leg 授權")

    # ---- ① 人工確認（最先，且**不持有**任何授權）----
    intent = PRD.LiveLegIntent(action, cfg)
    if not confirm_live_leg(intent, prompt=prompt):
        return LiveLegOutcome(action=action, requested=True, confirmed=False,
                              reason=LIVE_R_CONFIRM_FAILED,
                              detail="確認字串不符 → 未建立任何控制出口")

    # ---- ② fresh read（確認**之後**才取，確認前的快照一律作廢）----
    say("\n[LIVE] 人工確認通過 → 重新取得 fresh 觀測（確認前的快照一律不採用）")
    result = bundle.observe()
    arb = getattr(result, "arbitration", None) or result
    snap = meter_snapshot_source() if meter_snapshot_source else None
    session = report_session_source() if report_session_source else None

    # ---- ③ fresh 閘門：Decision → precheck → Safety → Authority → Interlock ----
    ok, gates, precheck = live_leg_gates(result, meter_snapshot=snap,
                                         report_session=session,
                                         config=cfg, action=action)
    if verbose:
        say(f"[LIVE] 自然 Decision = {gates['_observed_decision']}"
            f"（本次 leg 授權方向 = {action}）")
        for k in ("natural_decision_is_leg_action", "fresh_precheck",
                  "safety_pass", "authority_ok", "interlock_pass"):
            say(f"       [{'PASS' if gates[k] else 'FAIL'}] {k}")
    if not ok:
        reason = _gate_failure_reason(gates)
        say(f"[LIVE] {reason} → NO DISPATCH（未建立 executor / verifier）")
        return LiveLegOutcome(action=action, requested=True, confirmed=True,
                              reason=reason, gates=gates, precheck=precheck,
                              detail="fresh 閘門未全數通過 → 不建立控制出口")

    # ---- ④ 全部通過，才核發一次性授權 ----
    leg = PRD.LiveLegAuthorization(action, cfg)
    _live_leg_guard.update(used=True, action=action, final_state=None)
    say(f"[LIVE] 核發一次性 leg 授權：{leg}")

    # ---- ⑤ 才建立控制出口 ----
    if operator_run is None:
        operator_run = build_live_operator_run()
    executor = PRD.build_executor(PRD.MODE_CONTROLLED_SINGLE_LEG, leg, operator_run)
    verifier = PRD.build_verifier(PRD.MODE_CONTROLLED_SINGLE_LEG, leg,
                                  bundle.reader, cfg)
    if executor is None or verifier is None:
        leg.abort(LIVE_R_EXIT_NOT_BUILT)
        _live_leg_guard["final_state"] = leg.state
        return LiveLegOutcome(action=action, requested=True, confirmed=True,
                              reason=(LIVE_R_NO_OPERATOR if operator_run is None
                                      else LIVE_R_EXIT_NOT_BUILT),
                              gates=gates, precheck=precheck,
                              leg_state=leg.state,
                              detail="控制出口未能建立 → 不 dispatch")

    # ---- ⑥ 才進狀態機 ----
    runner = ControlledLiveLeg(leg, bundle.factory, bundle.observe)
    runner.arm(executor, verifier)
    state = runner.run_leg()
    _live_leg_guard["final_state"] = state
    say(f"[LIVE] leg 結束：{state}（dispatch={runner.dispatch_count} "
        f"stop={runner.stop_count}）")
    if runner.abort_reason:
        say(f"[LIVE] abort 原因：{runner.abort_reason}")
    return LiveLegOutcome(action=action, requested=True, confirmed=True,
                          reason=state, gates=gates, precheck=precheck,
                          leg_state=state, runner=runner,
                          executor_built=True, verifier_built=True,
                          dispatch_count=runner.dispatch_count,
                          stop_count=runner.stop_count,
                          detail=runner.abort_reason or "")


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


def live_leg_main(action, root=None, prompt=None, operator_run=None,
                  client=None, meter_client=None, meter_warmup_sec=8.0,
                  config=None):
    """
    `--live-leg` 的實際執行體。回傳 process exit code（0 = 流程正常收斂）。

    🔴 **不進入常駐迴圈**：一個 leg 結束就結束 process，
       因此不存在「服務持續處於 LIVE」這種狀態，重啟自然回到 OBSERVE_ONLY。
    🔴 仍需取得 Control Ownership —— 避免與常駐 service 同時控制同一台設備。
    ⚠️ client / meter_client / operator_run 可注入（離線測試用）；
       未注入時才建立真實連線，而 operator 更是**到最後一刻**才 import。
    """
    print("=" * 70)
    print(f"  CONTROLLED SINGLE-LEG LIVE —— {action.upper()}")
    print("  ⚠ --live-leg 是 permission，不是 decision override。")
    print("    自然 Decision 不是此方向 → 一律 NO DISPATCH。")
    print("=" * 70)

    ok_own, why = acquire_ownership(role="live-leg", root=root)
    if not ok_own:
        print(f"[LIVE] 未取得 PCS Control Ownership（{why}）→ fail closed，不執行。")
        return 0 if why == "held_by_other" else 1

    owns_client = client is None
    try:
        if client is None:
            client, _token = PRD.build_api_client(login=True)
        if meter_client is None:
            meter_client = PRD.build_meter_client(connect=True)
            time.sleep(float(meter_warmup_sec))    # 讓電表訂閱先有資料
        bundle = build_live_chain_bundle(client=client, meter_client=meter_client,
                                         config=config)
        outcome = run_live_leg_cli(
            action, bundle, config=config, prompt=prompt,
            operator_run=operator_run,
            meter_snapshot_source=meter_client.get_snapshot)
        print("\n" + "=" * 70)
        print(f"  {outcome}")
        print(f"  實機 {action} 次數 : {outcome.dispatch_count}")
        print(f"  實機 stop 次數    : {outcome.stop_count}")
        print("  ⚠ leg 結束 → 授權已消費/作廢、控制出口已銷毀、回到 OBSERVE_ONLY。")
        print("    重新啟動服務**不會**自動回到 LIVE。")
        print("=" * 70)
        return 0
    finally:
        if owns_client and meter_client is not None:
            try:
                meter_client.stop()
            except Exception:                      # noqa: BLE001
                pass
        release_ownership()


def main(argv=None):
    ap = argparse.ArgumentParser(description="PCS 自動控制服務（D.3-A Runtime Skeleton）")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                    help="skeleton 迴圈間隔秒數（不是 decision_interval_sec）")
    ap.add_argument("--max-ticks", type=int, default=None, help="達到輪數即結束（測試用）")
    ap.add_argument("--status", action="store_true", help="只顯示狀態後結束")
    ap.add_argument("--root", default=None, help="覆寫 output root（測試用）")
    # ---- Phase 6.9：Controlled Single-Leg LIVE ----
    # 🔴 刻意**沒有** --power：LIVE leg 的功率只能來自正式 ProductionConfig，
    #    不接受任何 override（150 kW 更不可能成為操作目標）。
    ap.add_argument("--live-leg", choices=PRD.LEG_ACTIONS, default=None,
                    help="受控單次上線：本 process 最多允許執行一次該方向的 leg"
                         "（不代表立即動作；仍須通過全部守門）")
    ap.add_argument("--confirm", action="store_true",
                    help="願意進入人工確認流程（**本身不代表確認完成**，"
                         "仍須輸入完整確認字串）")
    args = ap.parse_args(argv)
    if args.live_leg and not args.confirm:
        print("[LIVE] --live-leg 必須搭配 --confirm 才會進入確認流程 → 中止。")
        return 2

    # ---- Phase 6.9-A：受控單次 LIVE 入口 ----
    # 🔴 這條分支**不進入常駐迴圈**：一個 leg 結束（或任何一步失敗）就結束
    #    process，因此不存在「LIVE 模式持續運轉」這種狀態。
    # 🔴 沒有 --live-leg 時，下面完全走原本的 OBSERVE_ONLY 路徑，
    #    executor / verifier 一律不建立。
    if args.live_leg:
        return live_leg_main(args.live_leg, root=args.root)

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
