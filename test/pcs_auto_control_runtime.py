# -*- coding: utf-8 -*-
"""
pcs_auto_control_runtime.py — Phase 6 自動控制 Runtime Skeleton（D.3-A）
======================================================================
純邏輯、零 I/O。狀態機、cycle 編排、Fail Closed 邊界。

🔴 D.3-A 硬性不變量（由 test_phase6_d3a_runtime.py 逐條鎖住）
   1. DISPATCH_ENABLED = False —— 結構上不可能進入 command dispatch。
   2. 本模組**不 import** device_control_operator / pcs_control_executor /
      pcs_control_integration / api_client / requests —— 零控制能力、零網路能力。
   3. 即使注入 desired=charge/discharge/stop，也只產生 WOULD_* 稽核事件，
      dispatched 恆為 False。
   4. cycle 內任何未知例外 → Fail Closed 進 FAULT_BLOCKED，**不得**照抄
      report monitor 的「例外→印出→下一輪繼續」—— 那對監看正確，對控制危險。
   5. 本模組**不得**有任何 input()：Windows Service 沒有 console。

狀態機（D.3-A 只實際使用前四個，其餘先定義並鎖住 transition guard）
    DISABLED         未取得 Control Ownership／被明確停用
    OBSERVE_ONLY     有 Ownership，但 production 參數未齊備或 dispatch 未實作
    FAULT_BLOCKED    cycle 發生未知例外 → Fail Closed
    SHUTTING_DOWN    收到停止訊號
    ----- 以下需要 dispatch 能力，D.3-E 之後才可能進入 -----
    IDLE / COMMAND_PENDING / VERIFYING / OWNED_CHARGE / OWNED_DISCHARGE
    EXTERNAL_CONTROL / STALE_BLOCKED

⚠️ 本檔**不做**決策（D.3-C）、不做授權（D.3-D）、不接 executor（D.3-E）、
   不寫 LastControl（D.3-F）。desired_action 目前只由測試注入，
   用來證明「即使有意圖，也送不出任何指令」。

D.3-G：Startup Recovery
    服務啟動／重啟時以 recover() 明確重建擁有權狀態。
    🔴 建構子仍一律從 DISABLED 開始 —— **不存在**「一建立就宣稱擁有」的路徑。
    🔴 recovery 只做 READ → EVALUATE → MAP STATE：不送指令、不寫控制紀錄、
       不建立授權、不改 dispatch_enabled、不恢復 COMMAND_PENDING。
    🔴 OWNED_* 平常受 dispatch guard 保護；recovery 走**明確的獨立通道**
       （_make(..., recovered=True)），且只有在 durable 證據判定為擁有時才成立。
       **擁有權恢復 ≠ 啟用 dispatch** —— 兩者完全獨立。
    🔴 recovery **只在啟動／明確呼叫時**執行；一般 tick 不會重跑 reconciliation。

D.3-E：Execution Chain
    observation_source 可改注入 production_execution_chain.ProductionExecutionChain.run。
    🔴 DISPATCH_ENABLED 仍為模組層預設 **False** —— production 建立的 Runtime
       結構上不可能進入 VERIFYING / OWNED_*。離線測試以 dispatch_enabled=True
       明確注入，且必須同時注入 Fake executor / verifier / store。
    🔴 只有「已驗證且已記錄」（EXEC_VERIFIED）才進 OWNED_CHARGE / OWNED_DISCHARGE；
       STOP 驗證成功 → IDLE（不強迫回 STANDBY）。
    🔴 其餘所有結果（未送出 / 結果不明 / 狀態不符 / 紀錄寫入失敗）
       一律 FAULT_BLOCKED —— 不宣稱擁有、不更新紀錄、不自動重送、不自動停止。

D.3-D：Arbitration / Authorization
    observation_source 可改注入 production_arbiter.ProductionArbiter.evaluate，
    其結果帶有 authorized / control_action / authorization。
    🔴 授權成功 → COMMAND_PENDING，語意是「有一張有效授權票**等待未來的**
       Executor 消費」，**不是**「指令已送出」。dispatched 仍恆為 False。
    🔴 VERIFYING / OWNED_CHARGE / OWNED_DISCHARGE 仍然禁止（需要 D.3-E）。
    🔴 COMMAND_PENDING 只有在確實持有有效授權時才可進入 —— 由 _make 的 guard 強制。

D.3-C：Decision 觀測（OBSERVE-ONLY）
    observation_source 可改注入 decision_observer.DecisionObserver.observe，
    其結果帶有 would_action（charge / discharge / idle / None）與 stale_blocked。
    🔴 would_action 只是**稽核事實**，不是指令 —— Runtime 仍然沒有任何 dispatch 路徑。
    🔴 consumer-time stale → STALE_BLOCKED（資料不足以判斷），**不是**控制失敗。
    🔴 Runtime core 仍不 import 任何 adapter / observer / 決策 library。

D.3-B：觀測注入
    observation_source 由呼叫端注入（production 為 ESS Snapshot Adapter 的 observe）。
    🔴 Runtime core **不 import** adapter，也不知道資料怎麼來的 —— 維持純邏輯。
    🔴 observation_source 的例外**不吞掉**，交給 tick() 的 Fail Closed 邊界。
    🔴 observation.valid=True **不會**讓 Runtime 進入 IDLE 或任何 dispatch 狀態；
       D.3-B 仍然只會停留在 DISABLED / OBSERVE_ONLY / FAULT_BLOCKED / SHUTTING_DOWN。

用法（完全離線）
    python pcs_auto_control_runtime.py --demo
"""

import time
import argparse
from dataclasses import dataclass, asdict

import pcs_auto_control_config as CFG

# ======================================================================
# 🔴 D.3-A 的核心閘門
# ======================================================================
# 改為 True 需要 D.3-E Executor Wiring 的明確授權，且必須同時通過
# Authorization（D.3-D）、single-flight、direction interlock 的完整回歸。
# 在此之前，任何路徑都不得產生 command dispatch。
DISPATCH_ENABLED = False

# ======================================================================
# 狀態
# ======================================================================
ST_DISABLED = "DISABLED"
ST_OBSERVE_ONLY = "OBSERVE_ONLY"
ST_IDLE = "IDLE"
ST_COMMAND_PENDING = "COMMAND_PENDING"
ST_VERIFYING = "VERIFYING"
ST_OWNED_CHARGE = "OWNED_CHARGE"
ST_OWNED_DISCHARGE = "OWNED_DISCHARGE"
ST_EXTERNAL_CONTROL = "EXTERNAL_CONTROL"
ST_FAULT_BLOCKED = "FAULT_BLOCKED"
ST_STALE_BLOCKED = "STALE_BLOCKED"
ST_SHUTTING_DOWN = "SHUTTING_DOWN"
# 🔴 Blocker 13：有 durable 證據指向「這台設備正在執行的是我們的作業」，
#    但 AC 功率暫存器尚未更新到指令之後的值，因此擁有權**還不能佐證**。
#    語意刻意與既有兩者都不同：
#      OWNED_*          已完成佐證，可宣稱擁有權
#      EXTERNAL_CONTROL 已證明不是我們的作業
#      OWNERSHIP_PENDING 還不知道 —— 不宣稱、也不放棄
#    ⚠️ 它**不在** EXECUTION_STATES／DISPATCH_STATES 之內，
#       因此結構上不可能由它送出任何指令（Fail Closed for dispatch）。
ST_OWNERSHIP_PENDING = "OWNERSHIP_PENDING"

RUNTIME_STATES = frozenset({
    ST_DISABLED, ST_OBSERVE_ONLY, ST_IDLE, ST_COMMAND_PENDING, ST_VERIFYING,
    ST_OWNED_CHARGE, ST_OWNED_DISCHARGE, ST_EXTERNAL_CONTROL,
    ST_FAULT_BLOCKED, ST_STALE_BLOCKED, ST_SHUTTING_DOWN,
    ST_OWNERSHIP_PENDING,
})

# D.3-A 允許實際進入的狀態。
D3A_REACHABLE_STATES = frozenset({
    ST_DISABLED, ST_OBSERVE_ONLY, ST_FAULT_BLOCKED, ST_SHUTTING_DOWN,
})

# D.3-C：STALE_BLOCKED 開始有 reachable 的觀測語意。
# ⚠️ 它的意思是「資料不足以做可靠判斷」，**不是**「控制失敗」。
D3C_REACHABLE_STATES = D3A_REACHABLE_STATES | {ST_STALE_BLOCKED}

# 🔴 需要「指令已經送出」才可能進入的狀態 —— D.3-E 之前一律禁止。
EXECUTION_STATES = frozenset({ST_VERIFYING, ST_OWNED_CHARGE, ST_OWNED_DISCHARGE})

# 「已進入控制流程」的完整集合。COMMAND_PENDING 自 D.3-D 起可達，
# 但**只有持有有效授權票時**才允許進入（見 _make 的 guard）。
DISPATCH_STATES = EXECUTION_STATES | {ST_COMMAND_PENDING}

# D.3-D：授權完成後可達的狀態集合。
D3D_REACHABLE_STATES = D3C_REACHABLE_STATES | {ST_COMMAND_PENDING}

# D.3-E：執行鏈接上後可達的狀態集合（**僅在 dispatch_enabled=True 時**）。
D3E_REACHABLE_STATES = D3D_REACHABLE_STATES | EXECUTION_STATES | {ST_IDLE}

# 需要真實設備觀測才可能進入的狀態 —— D.3-A 尚未接資料來源。
OBSERVATION_STATES = frozenset({ST_IDLE, ST_EXTERNAL_CONTROL, ST_STALE_BLOCKED,
                                ST_OWNERSHIP_PENDING})

# ======================================================================
# 意圖（desired）—— D.3-A 只由測試注入，尚未接 Decision Engine
# ======================================================================
WANT_CHARGE = "charge"
WANT_DISCHARGE = "discharge"
WANT_STOP = "stop"
WANT_IDLE = "idle"                 # D.3-C：Decision 判定「目前不需充放電」
WANT_NONE = None
DESIRED_ACTIONS = frozenset({WANT_CHARGE, WANT_DISCHARGE, WANT_STOP, WANT_IDLE})

# ======================================================================
# 稽核事件
# ======================================================================
AUDIT_NOT_OWNER = "NOT_OWNER"
AUDIT_CONFIG_NOT_READY = "CONFIG_NOT_READY"
AUDIT_DISPATCH_NOT_IMPLEMENTED = "DISPATCH_NOT_IMPLEMENTED"
AUDIT_WOULD_CHARGE = "WOULD_CHARGE"
AUDIT_WOULD_DISCHARGE = "WOULD_DISCHARGE"
AUDIT_WOULD_STOP = "WOULD_STOP"
AUDIT_WOULD_IDLE = "WOULD_IDLE"
AUDIT_AUTHORIZED_WOULD_CHARGE = "AUTHORIZED_WOULD_CHARGE"
AUDIT_AUTHORIZED_WOULD_DISCHARGE = "AUTHORIZED_WOULD_DISCHARGE"
AUDIT_AUTHORIZED_WOULD_STOP = "AUTHORIZED_WOULD_STOP"
AUDIT_EXECUTED_VERIFIED = "EXECUTED_VERIFIED"
# 🔴 以字串比對，**不 import** production_execution_chain —— Runtime core 維持純邏輯。
EXEC_VERIFIED_OUTCOMES = frozenset({"VERIFIED"})
AUDIT_EXECUTION_FAILED = "EXECUTION_FAILED"
AUDIT_RECOVERY_STARTED = "RECOVERY_STARTED"
AUDIT_RECOVERY_COMPLETED = "RECOVERY_COMPLETED"
AUDIT_RECOVERY_BLOCKED = "RECOVERY_BLOCKED"
AUDIT_STALE_BLOCKED = "STALE_BLOCKED"
AUDIT_NO_ACTION = "NO_ACTION"
AUDIT_CYCLE_EXCEPTION = "CYCLE_EXCEPTION"
AUDIT_SHUTDOWN = "SHUTDOWN"
AUDIT_ALREADY_SHUTDOWN = "ALREADY_SHUTDOWN"

WOULD_EVENT_OF = {WANT_CHARGE: AUDIT_WOULD_CHARGE,
                  WANT_DISCHARGE: AUDIT_WOULD_DISCHARGE,
                  WANT_STOP: AUDIT_WOULD_STOP,
                  WANT_IDLE: AUDIT_WOULD_IDLE}

# reason
R_NOT_OWNER = "NO_CONTROL_OWNERSHIP"
R_CONFIG_NOT_READY = "PRODUCTION_CONFIG_NOT_READY"
R_DISPATCH_NOT_IMPLEMENTED = "DISPATCH_NOT_IMPLEMENTED_IN_D3A"
R_CYCLE_EXCEPTION = "CYCLE_EXCEPTION_FAIL_CLOSED"
R_SHUTDOWN = "SHUTDOWN_REQUESTED"
R_OBSERVATION_STALE = "OBSERVATION_STALE_AT_DECISION_TIME"
R_AUTHORIZED = "CONTROL_LEG_AUTHORIZED_NOT_DISPATCHED"
R_EXECUTION_VERIFIED = "CONTROL_VERIFIED_AND_RECORDED"
R_EXECUTION_FAIL_CLOSED = "EXECUTION_NOT_VERIFIED_FAIL_CLOSED"
R_RECOVERY_NO_SOURCE = "RECOVERY_SOURCE_NOT_CONFIGURED"
R_RECOVERY_EXCEPTION = "RECOVERY_EXCEPTION_FAIL_CLOSED"

# reconciliation outcome → runtime 狀態。
# 🔴 以字串比對，**不 import** execution_reconciler —— core 維持純邏輯。
# 🔴 只有 RECOVER_OWNED 能到達 OWNED_*，且仍需 LastControl 動作可對應方向。
RECOVERY_OWNED_OUTCOME = "RECOVER_OWNED"
RECOVERY_STATE_MAP = {"RECOVER_IDLE": ST_IDLE,
                      "RECOVERY_EXTERNAL": ST_EXTERNAL_CONTROL,
                      "RECOVERY_CONFLICT": ST_FAULT_BLOCKED,
                      "RECOVERY_UNKNOWN": ST_FAULT_BLOCKED,
                      "RECOVERY_BLOCKED": ST_FAULT_BLOCKED}
RECOVERY_OWNED_STATE_OF = {WANT_CHARGE: ST_OWNED_CHARGE,
                           WANT_DISCHARGE: ST_OWNED_DISCHARGE}

# 已驗證且已記錄後的 runtime 狀態（**只有**這條路徑能宣稱擁有）。
# 🔴 STOP 成功 → IDLE：不強迫設備一定回 STANDBY（STOPPED 亦為合法閒置結果）。
OWNED_STATE_OF = {WANT_CHARGE: ST_OWNED_CHARGE,
                  WANT_DISCHARGE: ST_OWNED_DISCHARGE,
                  WANT_STOP: ST_IDLE}

# 授權後的稽核事件對照（依**實際控制動作**，不是 decision 意圖）。
# 🔴 這些事件的語意是「已授權」，**不是**「已送出」。
AUTHORIZED_EVENT_OF = {WANT_CHARGE: AUDIT_AUTHORIZED_WOULD_CHARGE,
                       WANT_DISCHARGE: AUDIT_AUTHORIZED_WOULD_DISCHARGE,
                       WANT_STOP: AUDIT_AUTHORIZED_WOULD_STOP}
R_ALREADY_SHUTDOWN = "ALREADY_SHUTTING_DOWN"

AUDIT_LIMIT = 200          # 記憶體中保留的稽核事件上限（避免長跑膨脹）


@dataclass(frozen=True)
class CycleInputs:
    """
    一次 cycle 的輸入。全部由呼叫端注入 —— 本模組不自行取得任何資料。

    desired_action
        ⚠️ D.3-A 尚未接 Decision Engine，此欄位**只由測試注入**，
           用途是證明「即使存在明確意圖，也不可能產生 dispatch」。
    """
    is_owner: bool = False
    desired_action: str = None
    target_power_kw: float = None
    observation: object = None        # D.3-B：ControlObservation（duck typing，不 import）
    detail: str = ""


@dataclass(frozen=True)
class CycleResult:
    """
    一次 cycle 的結果。

    🔴 dispatched 在 D.3-A **恆為 False**，且有專屬斷言鎖住。
    """
    state: str
    reason: str
    audit_event: str
    dispatched: bool = False
    would_action: str = None
    cycle: int = 0
    at: float = None
    missing_config: tuple = ()
    # ---- D.3-B：觀測結果（只記錄，不參與任何控制判定）----
    observation_valid: bool = None
    observation_reason: str = None
    pcs_state: str = None
    # ---- D.3-D：授權（**已授權 ≠ 已送出**）----
    authorized: bool = False
    authorization_id: str = None
    control_action: str = None
    # ---- D.3-E：執行（**已驗證 ≠ 已授權 ≠ 已送出**）----
    executed: bool = False
    execution_outcome: str = None
    recovery_outcome: str = None
    previous_state: str = None
    lastcontrol_written: bool = False
    phases: tuple = ()
    detail: str = ""

    def as_dict(self):
        return asdict(self)

    def __str__(self):
        w = f" would={self.would_action}" if self.would_action else ""
        o = ""
        if self.observation_reason is not None:
            o = (f" obs={'VALID' if self.observation_valid else 'INVALID'}"
                 f"/{self.observation_reason}"
                 + (f"/{self.pcs_state}" if self.pcs_state else ""))
        a = f" authz={self.authorization_id}" if self.authorization_id else ""
        return (f"[CYCLE {self.cycle:>4}] {self.state:<14} {self.audit_event:<26}"
                f" dispatched={self.dispatched}{w}{o}{a}")


class AutoControlRuntime:
    """
    Runtime Skeleton。純邏輯、零 I/O、可完全離線測試。

    observer
        可注入的觀測函式（D.3-C 之後會換成真正的 meter/ESS 讀取＋決策）。
        D.3-A 預設為 None → 使用內建的 skeleton 觀測（只回傳注入的 desired）。
        注入用途之一是測試「cycle 例外必須 Fail Closed」。
    """

    def __init__(self, config=None, clock=None, observer=None,
                 observation_source=None, dispatch_enabled=None,
                 recovery_source=None):
        self.config = config if config is not None else CFG.DEFAULT_CONTROL_CONFIG
        self._clock = clock if clock is not None else time.monotonic
        self._observer = observer
        # D.3-B：注入式觀測來源（production 為 EssSnapshotAdapter.observe）。
        # 🔴 Runtime 不知道它從哪裡拿資料，也不得 import 任何 adapter。
        self._observation_source = observation_source
        # 🔴 模組層預設 False —— production 一律不傳此參數，
        #    因此 production Runtime 結構上進不了 VERIFYING / OWNED_*。
        #    離線測試必須明確注入 True，且同時注入 Fake executor/verifier/store。
        self.dispatch_enabled = (DISPATCH_ENABLED if dispatch_enabled is None
                                 else bool(dispatch_enabled))
        # D.3-G：注入式的啟動恢復來源（回傳 ReconciliationResult）。
        # 🔴 core 不 import reconciler，也不知道證據從哪來。
        self._recovery_source = recovery_source
        self.recovery_count = 0
        self.state = ST_DISABLED
        self.cycles = 0
        self.dispatch_count = 0            # 🔴 D.3-A 必須恆為 0
        self.audit = []

    # ------------------------------------------------------------------
    def _record(self, res):
        self.audit.append(res)
        if len(self.audit) > AUDIT_LIMIT:
            del self.audit[:len(self.audit) - AUDIT_LIMIT]
        return res

    def _make(self, state, reason, event, would=None, missing=(), detail="",
              obs=None, authorized=False, recovered=False):
        # 🔴 唯一的狀態出口。
        #    ① 指令已送出才可能到達的狀態，在 D.3-E 之前一律禁止。
        # 🔴 ① 一般路徑：指令已送出才可能到達的狀態，需 dispatch_enabled。
        #    ② RECOVERY 通道（裁示十八授權）：以 durable 已驗證證據重建擁有權，
        #       這是「認出上一個行程做過什麼」，不是「本行程送了什麼」，
        #       因此不受 dispatch guard 限制 —— 但**只允許 OWNED_***，
        #       且不得因此改變 dispatch_enabled（另有專屬斷言）。
        if not self.dispatch_enabled and state in EXECUTION_STATES:
            if not (recovered and state in RECOVERY_OWNED_STATE_OF.values()):
                raise AssertionError(
                    f"不得進入需要已送出指令的狀態：{state}"
                    f"（dispatch_enabled=False）")
        if recovered and state == ST_COMMAND_PENDING:
            # 授權是一次性且僅存在於記憶體 —— 重啟後不可能有 pending
            raise AssertionError("recovery 不得恢復 COMMAND_PENDING")
        #    ② COMMAND_PENDING 只有在確實持有有效授權時才允許 ——
        #       沒有授權票就沒有「等待被消費」這回事。
        if state == ST_COMMAND_PENDING and not authorized:
            raise AssertionError("COMMAND_PENDING 需要有效授權票")
        if state not in RUNTIME_STATES:
            raise AssertionError(f"未知的 runtime 狀態：{state!r}")
        self.state = state
        # 觀測以 duck typing 讀取 —— Runtime 不依賴 adapter 的型別
        return CycleResult(state=state, reason=reason, audit_event=event,
                           dispatched=False, would_action=would,
                           authorized=bool(authorized),
                           executed=bool(getattr(obs, "executed", False)),
                           execution_outcome=getattr(obs, "outcome", None),
                           lastcontrol_written=bool(
                               getattr(obs, "lastcontrol_written", False)),
                           phases=tuple(getattr(obs, "phases", ()) or ()),
                           authorization_id=getattr(
                               getattr(obs, "authorization", None),
                               "authorization_id", None),
                           control_action=getattr(obs, "control_action", None),
                           cycle=self.cycles, at=self._clock(),
                           missing_config=tuple(missing),
                           observation_valid=getattr(obs, "valid", None),
                           observation_reason=getattr(obs, "reason", None),
                           pcs_state=getattr(obs, "pcs_state", None),
                           detail=detail)

    # ------------------------------------------------------------------
    def tick(self, inputs=None):
        """
        執行一次 cycle。**永遠不會拋出例外**（Fail Closed 邊界就在這裡）。

        🔴 控制服務的 exception philosophy 與 report monitor 相反：
           report monitor：例外 → 印出 → 下一輪照常繼續監看（正確，監看不得停擺）
           control runtime：例外 → **Fail Closed** → FAULT_BLOCKED → 不產生任何指令
           因為「不確定發生了什麼」時，繼續控制設備是不可接受的。
        """
        self.cycles += 1
        inputs = inputs if inputs is not None else CycleInputs()
        try:
            return self._record(self._cycle(inputs))
        except Exception as e:                                    # noqa: BLE001
            # 連 _make() 都可能因狀態不合法而拋出 —— 這裡直接建構結果，
            # 不再經過 _make()，確保 Fail Closed 路徑本身不可能再失敗。
            self.state = ST_FAULT_BLOCKED
            return self._record(CycleResult(
                state=ST_FAULT_BLOCKED, reason=R_CYCLE_EXCEPTION,
                audit_event=AUDIT_CYCLE_EXCEPTION, dispatched=False,
                would_action=None, cycle=self.cycles, at=None,
                detail=f"{type(e).__name__}: {e}"))

    def _cycle(self, inputs):
        if self.state == ST_SHUTTING_DOWN:
            return self._make(ST_SHUTTING_DOWN, R_ALREADY_SHUTDOWN,
                              AUDIT_ALREADY_SHUTDOWN,
                              detail="已進入關閉流程，不再產生新的判定")

        # ---- 1. Control Ownership（不是 Report Monitor Ownership）----
        if not inputs.is_owner:
            return self._make(ST_DISABLED, R_NOT_OWNER, AUDIT_NOT_OWNER,
                              detail="未持有 PCS Control Ownership → 不啟動任何判定")

        # ---- 1.5 D.3-B：取得本輪觀測 ----
        # 🔴 例外不在這裡處理 —— 交給 tick() 的 Fail Closed 邊界，
        #    確保 adapter 失效時 Runtime 一律進 FAULT_BLOCKED。
        obs = (self._observation_source() if self._observation_source is not None
               else inputs.observation)

        # ---- 2. production 參數必須全部齊備 ----
        missing = self.config.missing_required()
        if missing:
            return self._observe(inputs, R_CONFIG_NOT_READY,
                                 AUDIT_CONFIG_NOT_READY, missing, obs)

        # ---- 3. 參數齊備，但 D.3-A/B 尚未實作 dispatch ----
        # 🔴 最後一道保險：即使參數全填、且觀測 valid，也不會獲得控制能力。
        #    解除必須經過 D.3-E。observation.valid 與狀態轉移**完全無關**。
        return self._observe(inputs, R_DISPATCH_NOT_IMPLEMENTED,
                             AUDIT_DISPATCH_NOT_IMPLEMENTED, (), obs)

    def _observe(self, inputs, reason, base_event, missing, obs=None):
        """
        OBSERVE_ONLY：只記錄「本來會做什麼」，絕不產生指令。

        observer 由外部注入時，其例外**不吞掉** —— 交給 tick() 的 Fail Closed
        邊界處理，讓未知錯誤一律進 FAULT_BLOCKED。
        """
        # 🔴 D.3-C：觀測若已帶有 would_action，優先採用（那是正式 library 的決策結果）。
        #    明確注入的 observer 仍最優先，供離線測試控制輸入。
        desired = inputs.desired_action
        if self._observer is not None:
            desired = self._observer(inputs)
        elif getattr(obs, "would_action", None) is not None:
            desired = obs.would_action

        # 🔴 D.3-E：執行鏈已跑完 → 依結果決定狀態。
        #    只有「已驗證且已記錄」才可宣稱擁有；其餘一律 Fail Closed。
        if getattr(obs, "executed", False) is True:
            act = getattr(obs, "control_action", None)
            if getattr(obs, "outcome", None) in EXEC_VERIFIED_OUTCOMES:
                st = OWNED_STATE_OF.get(act)
                if st is None:
                    raise AssertionError(f"未知的已驗證動作：{act!r}")
                self.dispatch_count += int(
                    getattr(obs, "dispatch_count_delta", 0) or 0)
                return self._make(st, R_EXECUTION_VERIFIED,
                                  AUDIT_EXECUTED_VERIFIED, would=desired,
                                  missing=missing, obs=obs, authorized=True,
                                  detail=getattr(obs, "detail", ""))
            # 🔴 未驗證成功 —— **不等於**指令沒有執行。
            #    不宣稱擁有、不更新紀錄、不自動重送、不自動停止。
            self.dispatch_count += int(
                getattr(obs, "dispatch_count_delta", 0) or 0)
            return self._make(ST_FAULT_BLOCKED, R_EXECUTION_FAIL_CLOSED,
                              AUDIT_EXECUTION_FAILED, missing=missing, obs=obs,
                              detail=f"{getattr(obs, 'outcome', None)}："
                                     f"{getattr(obs, 'detail', '')}")

        # 🔴 D.3-D：已授權 → COMMAND_PENDING。
        #    這代表「有一張有效授權票等待未來的 Executor 消費」，
        #    **不代表**任何指令已經送出 —— dispatched 仍為 False。
        if getattr(obs, "authorized", False) is True:
            act = getattr(obs, "control_action", None)
            ev = AUTHORIZED_EVENT_OF.get(act)
            if ev is None:
                raise AssertionError(f"未知的已授權動作：{act!r}")
            return self._make(ST_COMMAND_PENDING, R_AUTHORIZED, ev,
                              would=desired, missing=missing, obs=obs,
                              authorized=True,
                              detail=getattr(obs, "detail", "")
                                     or "授權完成，等待未來的 Executor（本階段不存在）")

        # 🔴 consumer-time stale → 資料不足以做可靠判斷，不得產生任何意圖。
        if getattr(obs, "stale_blocked", False) is True:
            return self._make(ST_STALE_BLOCKED, R_OBSERVATION_STALE,
                              AUDIT_STALE_BLOCKED, missing=missing, obs=obs,
                              detail=getattr(obs, "detail", "")
                                     or "決策當下資料已過舊 → 不產生任何意圖")

        if desired in DESIRED_ACTIONS:
            return self._make(ST_OBSERVE_ONLY, reason, WOULD_EVENT_OF[desired],
                              would=desired, missing=missing, obs=obs,
                              detail=f"OBSERVE_ONLY：本輪意圖為 {desired}，"
                                     f"但不產生任何 PCS 指令（{reason}）")
        if desired is not None:
            # 未知意圖 → 也只是記錄，不猜測
            return self._make(ST_OBSERVE_ONLY, reason, AUDIT_NO_ACTION,
                              missing=missing, obs=obs,
                              detail=f"未知的 desired_action={desired!r} → 不動作")
        return self._make(ST_OBSERVE_ONLY, reason, base_event, missing=missing,
                          obs=obs,
                          detail=inputs.detail or "OBSERVE_ONLY：本輪無意圖")

    # ------------------------------------------------------------------
    def recover(self, result=None):
        """
        啟動／重啟恢復。**只讀、只映射狀態** —— 不送指令、不寫紀錄、不建立授權。

        result
            已算好的 ReconciliationResult（duck typing）。未提供時使用注入的
            recovery_source。兩者皆無 → Fail Closed。

        🔴 以 **durable + fresh 證據**為準，**不是**以既有 runtime 記憶體為準：
           記憶體殘留 OWNED 而證據判為外部控制時，必須被覆寫成 Fail Closed；
           反之記憶體是 FAULT_BLOCKED 而證據合法時，也應恢復成 OWNED_*。
        🔴 任何例外一律 Fail Closed，且**不得**沿用先前的 OWNED_*。
        🔴 本方法**不在** tick() 內被呼叫 —— 一般 cycle 不重跑 reconciliation。
        """
        self.cycles += 1
        self.recovery_count += 1
        prev = self.state
        self._record(CycleResult(state=prev, reason=R_RECOVERY_NO_SOURCE,
                                 audit_event=AUDIT_RECOVERY_STARTED,
                                 cycle=self.cycles, previous_state=prev))
        try:
            if result is None:
                if self._recovery_source is None:
                    self.state = ST_FAULT_BLOCKED
                    return self._record(CycleResult(
                        state=ST_FAULT_BLOCKED, reason=R_RECOVERY_NO_SOURCE,
                        audit_event=AUDIT_RECOVERY_BLOCKED, cycle=self.cycles,
                        previous_state=prev,
                        detail="未注入 recovery 來源 → Fail Closed"))
                result = self._recovery_source()

            outcome = getattr(result, "outcome", None)
            if outcome == RECOVERY_OWNED_OUTCOME:
                act = getattr(result, "lastcontrol_action", None)
                st = RECOVERY_OWNED_STATE_OF.get(act)
                if st is None:
                    # 判為擁有卻對不到運轉方向 → 不得宣稱擁有
                    st, outcome_event = ST_FAULT_BLOCKED, AUDIT_RECOVERY_BLOCKED
                else:
                    outcome_event = AUDIT_RECOVERY_COMPLETED
            else:
                st = RECOVERY_STATE_MAP.get(outcome)
                if st is None:
                    st, outcome_event = ST_FAULT_BLOCKED, AUDIT_RECOVERY_BLOCKED
                else:
                    outcome_event = (AUDIT_RECOVERY_COMPLETED if st == ST_IDLE
                                     else AUDIT_RECOVERY_BLOCKED)

            res = self._make(st, getattr(result, "reason", None) or outcome,
                             outcome_event, obs=result, recovered=True,
                             detail=getattr(result, "detail", ""))
            return self._record(CycleResult(
                **{**res.as_dict(), "recovery_outcome": outcome,
                   "previous_state": prev,
                   "observation_reason": getattr(result, "authority_reason", None),
                   "pcs_state": getattr(result, "pcs_state", None)}))
        except Exception as e:                                    # noqa: BLE001
            # 🔴 恢復失敗一律 Fail Closed，**不得**沿用先前的 OWNED_*
            self.state = ST_FAULT_BLOCKED
            return self._record(CycleResult(
                state=ST_FAULT_BLOCKED, reason=R_RECOVERY_EXCEPTION,
                audit_event=AUDIT_RECOVERY_BLOCKED, cycle=self.cycles,
                previous_state=prev, detail=f"{type(e).__name__}: {e}"))

    def shutdown(self):
        """
        關閉。**不送任何 PCS 指令**（Phase D.3 裁示：relinquish authority only）。

        🔴 停止軟體服務 ≠ 停止 PCS。三種停止情境（Windows 更新重啟／人工停止／
           程式 crash）意圖完全不同，且 crash 時根本沒有 hook 可用 ——
           與其提供一個有時失效的保證，不如明確不提供。
        """
        self.cycles += 1
        return self._record(self._make(
            ST_SHUTTING_DOWN, R_SHUTDOWN, AUDIT_SHUTDOWN,
            detail="停止核發新判定；不送出 pcs_stop_power"))

    def snapshot(self):
        """供服務層顯示與測試（不含任何可變引用）。"""
        return {"state": self.state, "cycles": self.cycles,
                "dispatch_count": self.dispatch_count,
                "has_observation_source": self._observation_source is not None,
                "dispatch_enabled_instance": self.dispatch_enabled,
                "recovery_count": self.recovery_count,
                "has_recovery_source": self._recovery_source is not None,
                "dispatch_enabled": DISPATCH_ENABLED,
                "config_ready": self.config.dispatch_ready,
                "missing_config": list(self.config.missing_required())}


def main(argv=None):
    ap = argparse.ArgumentParser(description="PCS 自動控制 Runtime Skeleton（離線演示）")
    ap.add_argument("--demo", action="store_true")
    ap.parse_args(argv)

    print("== PCS Auto Control Runtime Skeleton（D.3-A，完全離線）==\n")
    print(f"  DISPATCH_ENABLED = {DISPATCH_ENABLED}"
          f"   ← False 代表結構上不可能送出任何 PCS 指令\n")

    rt = AutoControlRuntime()
    cases = [
        ("非 Owner", CycleInputs(is_owner=False)),
        ("Owner，但參數未齊備", CycleInputs(is_owner=True)),
        ("Owner + 意圖 CHARGE", CycleInputs(is_owner=True, desired_action=WANT_CHARGE)),
        ("Owner + 意圖 DISCHARGE", CycleInputs(is_owner=True, desired_action=WANT_DISCHARGE)),
        ("Owner + 意圖 STOP", CycleInputs(is_owner=True, desired_action=WANT_STOP)),
    ]
    for label, ci in cases:
        print(f"  {label:<24} {rt.tick(ci)}")

    bad = AutoControlRuntime(observer=lambda _i: (_ for _ in ()).throw(RuntimeError("boom")))
    print(f"  {'cycle 例外（Fail Closed）':<24} {bad.tick(CycleInputs(is_owner=True))}")
    print(f"  {'shutdown':<24} {rt.shutdown()}")

    print(f"\n  dispatch 次數：{rt.dispatch_count}（必須為 0）")
    print("  ⚠ 全程未 import device_control_operator / executor，未送出任何指令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
