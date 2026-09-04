# -*- coding: utf-8 -*-
"""
phase6_unattended_service.py — Phase 6.10-C Unattended Service（OFFLINE）
======================================================================
無人值守 orchestrator 的完整 lifecycle：

    BOOT → RECOVER → STARTUP GATES → OBSERVE → EVALUATE ELIGIBILITY
    → HANDOFF RUNNER → AUDIT → LOOP

🔴 **本階段只做離線實作與 mock 測試**
    不部署、不安裝 Service、不建立 Task。預設 `MODE_DRY_RUN`，
    `DISPATCH_ENABLED` 恆為 False，因此：
        remote pause sender = 0 / remote restore sender = 0 / PCS dispatcher = 0

🔴 **現場能力以實際部署為準，不以原始碼存在為準**
    `phase6_remote_adapter` 內雖有 `REMOTE_GUARD_B2_SH`，但遠端實際部署的是
    B1（唯讀動詞）。因此 `remote_pause_capable` / `remote_restore_capable`
    在 `DEPLOYED_GUARD_VARIANT != "B2"` 時一律 False。

🔴 **NETWORK_IDENTITY_STABILITY 目前是 NOT_VERIFIED，不是 PASS**
    B1.7（受控 reconnect / DHCP renew 後重新驗證）尚未執行。
    程式中不得把它假設為 PASS —— 只要它不是 PASS，live handoff 一律 REFUSED。

🔴 **恢復責任跨重啟存活**
    Windows reboot / Service restart **不得**把 ownership 重設為 IDLE。
    啟動第一件事一定是 `recover()`。
"""
import io
import os
import json
import datetime

import phase6_handoff_orchestrator as HO
import phase6_remote_adapter as RA

# ======================================================================
# 現場能力／驗證狀態（**事實登記**，不是可調旗標）
# ======================================================================
NET_PASS = "PASS"
NET_NOT_VERIFIED = "NOT_VERIFIED"

# 🔴 B1.7 = DEFERRED / NOT VERIFIED。這裡如實記錄，**不得**寫成 PASS。
NETWORK_IDENTITY_STABILITY = NET_NOT_VERIFIED

# 🔴 FIRST LIVE 尚未完成
FIRST_LIVE_PREREQUISITE_SATISFIED = False


def remote_pause_capable(capability=None):
    """
    現場是否真的具備 pause 能力。

    🔴 兩個條件**都要**成立，不是二選一：
       ① 本機紀錄的部署版本是 B2（`DEPLOYED_GUARD_VARIANT`）
       ② guard 自己在 probe 裡回報 variant=B2 且 capability_pause=true
       本機紀錄可能過期或寫錯，guard 的回報才是現場事實；反過來，
       只信 guard 回報也不行 —— 那等於接受一個我們沒記錄部署過的版本。
    🔴 未提供 capability report → False（Fail Closed）。
    """
    return (RA.DEPLOYED_GUARD_VARIANT == "B2"
            and "pause" in RA.GUARD_B2_ALLOWED_VERBS
            and RA.capability_gate(capability)[1]["capability_pause"]
            and RA.capability_gate(capability)[1]["variant_is_b2"])


def remote_restore_capable(capability=None):
    return (RA.DEPLOYED_GUARD_VARIANT == "B2"
            and "restore" in RA.GUARD_B2_ALLOWED_VERBS
            and RA.capability_gate(capability)[1]["capability_restore"]
            and RA.capability_gate(capability)[1]["variant_is_b2"])


# ======================================================================
# C7. Durable Journal —— 必須記錄的事件（供回歸逐項驗證）
# ======================================================================
EV_SERVICE_START = "SERVICE_START"
EV_SERVICE_SHUTDOWN = "SERVICE_SHUTDOWN"
EV_RECOVERY = "RECOVERY_RESULT"
EV_ELIGIBILITY = "ELIGIBILITY_DECISION"
EV_HANDOFF_BEGIN = "HANDOFF_BEGIN"
EV_PHASE6_TAKEOVER = "PHASE6_TAKEOVER"
EV_PHASE6_RELEASE = "PHASE6_RELEASE"
EV_CRITICAL = "CRITICAL_FAILURE"

REQUIRED_JOURNAL_EVENTS = (
    EV_SERVICE_START, EV_SERVICE_SHUTDOWN, EV_RECOVERY, EV_ELIGIBILITY,
    EV_HANDOFF_BEGIN, EV_PHASE6_TAKEOVER, EV_PHASE6_RELEASE, EV_CRITICAL,
)
# pause / restore 的 INTENT / OUTCOME 由 orchestrator 自行寫入（見 OnceGuard）。


# ======================================================================
# C13. Critical states
# ======================================================================
CRIT_OWNERSHIP_CONFLICT = HO.S_OWNERSHIP_CONFLICT
CRIT_RESTORE_FAILED = HO.S_RESTORE_FAILED
CRIT_RECOVERY_UNKNOWN = "RECOVERY_UNKNOWN"
CRIT_REMOTE_IDENTITY_MISMATCH = "REMOTE_IDENTITY_MISMATCH"

CRITICAL_CONDITIONS = (CRIT_OWNERSHIP_CONFLICT, CRIT_RESTORE_FAILED,
                       CRIT_RECOVERY_UNKNOWN, CRIT_REMOTE_IDENTITY_MISMATCH)


# ======================================================================
# C6. Single instance
# ======================================================================
# 🔴 **不能只靠 PID file** —— PID 會被回收、crash 會留下 stale lock、
#    非預期關機會讓 lock 永遠存在。因此 lock 內容必須同時記錄
#    pid / boot_id / cmdline，接手前三者都要對得上才算「仍在執行」。
# 🔴 production 應改用本專案既有的 Windows Named Mutex 機制
#    （report_monitor / pcs_auto_control_service 已採用），
#    本類別的 PID-file 實作是為了讓離線測試不需要 Win32。
class SingleInstanceGuard(object):

    def __init__(self, lock_path, pid=None, boot_id=None, cmdline=None,
                 is_alive=None, mutex_acquire=None):
        self.lock_path = lock_path
        self.pid = pid if pid is not None else os.getpid()
        self.boot_id = boot_id
        self.cmdline = cmdline
        # is_alive(pid) -> True/False/None(不可知)
        self._is_alive = is_alive or (lambda p: None)
        # production：注入 Named Mutex 取得函式；未注入時退回 PID-file
        self._mutex_acquire = mutex_acquire
        self.held = False
        self.refuse_reason = None

    def _read(self):
        if not os.path.exists(self.lock_path):
            return None
        try:
            return json.loads(io.open(self.lock_path, encoding="utf-8").read())
        except (ValueError, OSError):
            return {"corrupt": True}

    def acquire(self):
        """回傳 (ok, reason)。任何不確定 → 拒絕啟動（Fail Closed）。"""
        if self._mutex_acquire is not None:
            ok = bool(self._mutex_acquire())
            self.held = ok
            self.refuse_reason = None if ok else "NAMED_MUTEX_HELD_BY_OTHER"
            return ok, self.refuse_reason

        cur = self._read()
        if cur is not None:
            if cur.get("corrupt"):
                self.refuse_reason = "LOCK_FILE_CORRUPT"
                return False, self.refuse_reason
            alive = self._is_alive(cur.get("pid"))
            if alive is None:
                # 🔴 判不出對方死活 → 不得假設它已經死了
                self.refuse_reason = "PEER_LIVENESS_UNKNOWN"
                return False, self.refuse_reason
            if alive:
                same_boot = (cur.get("boot_id") == self.boot_id)
                same_cmd = (cur.get("cmdline") == self.cmdline)
                if same_boot and same_cmd:
                    self.refuse_reason = "ANOTHER_INSTANCE_RUNNING"
                    return False, self.refuse_reason
                # PID 還活著但不是我們的程式 → PID 被回收，視為 stale
            # 到這裡代表 stale（行程已死，或 PID 已被別的程式回收）
        payload = {"pid": self.pid, "boot_id": self.boot_id,
                   "cmdline": self.cmdline,
                   "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        tmp = self.lock_path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.lock_path)
        self.held = True
        return True, None

    def release(self):
        if self.held and os.path.exists(self.lock_path):
            try:
                os.remove(self.lock_path)
            except OSError:
                pass
        self.held = False


# ======================================================================
# C3/C4/C5. Live gates
# ======================================================================
LIVE_GATE_ITEMS = ("mode_armed", "dispatch_enabled", "network_identity_pass",
                   "remote_guard_b2", "pause_capable", "restore_capable",
                   "recovery_clean", "local_data_fresh", "remote_probe_fresh",
                   "first_live_prerequisite", "service_health_healthy")


def live_handoff_allowed(mode, recovery_verdict, local_fresh, remote_probe,
                         health=None, capability=None):
    """
    回傳 (allowed, checks)。**任一不成立即 False。**

    🔴 `network_identity_pass` 直接讀模組常數 —— 目前為 NOT_VERIFIED，
       因此無論其他條件如何，live handoff 一律 REFUSED。
    """
    checks = {
        "mode_armed": mode == HO.MODE_ARMED,
        "dispatch_enabled": HO.DISPATCH_ENABLED is True,
        "network_identity_pass": NETWORK_IDENTITY_STABILITY == NET_PASS,
        # 🔴 不只看本機常數 —— 還要 guard 自己回報 variant=B2。
        "remote_guard_b2": (RA.DEPLOYED_GUARD_VARIANT == "B2"
                            and RA.capability_gate(capability)[1]
                            ["variant_is_b2"]),
        "pause_capable": remote_pause_capable(capability),
        "restore_capable": remote_restore_capable(capability),
        "recovery_clean": recovery_verdict == HO.R_CLEAN,
        "local_data_fresh": local_fresh is True,
        "remote_probe_fresh": bool(remote_probe
                                   and remote_probe.get("screen_alive") is True
                                   and remote_probe.get("process_alive") is True
                                   and remote_probe.get("process_identity_ok") is True),
        "first_live_prerequisite": FIRST_LIVE_PREREQUISITE_SATISFIED is True,
        # 🔴 服務自身沒有進展時，其餘十項全過也不得開始交接。
        #    HEALTHY 才放行；UNKNOWN / UNCONFIGURED / 未提供一律 FAIL。
        #    這是 **recoverable fail-closed**：進展恢復後本項可再度 PASS，
        #    不是 critical latch（見 CRITICAL_CONDITIONS 維持四項）。
        "service_health_healthy": health == HEALTH_OK,
    }
    return all(checks.values()), checks


# ======================================================================
# C11. Eligibility —— 由現場條件決定，**沒有任何硬編碼時刻**
# ======================================================================
ELIGIBILITY_ITEMS = ("tou_off_peak", "soc_in_charge_band", "decision_is_charge",
                     "comm_ok", "fault_clear", "alarm_clear",
                     "local_fresh", "remote_healthy")


def evaluate_eligibility(sample, probe, soc_max_pct):
    """
    回傳 (eligible, checks)。

    🔴 進場時機一律由 TOU / fresh SOC / Natural Decision / Authority /
       PCS / Meter / fault-alarm 決定。**不得**以任何時刻常數判斷 ——
       歷史觀測到的時間窗只是 observation，不是 production schedule。
    """
    # 🔴 觀測拿不到資料（重試用盡、端點掛掉）時 sample 會是 None。
    #    這不是「例外」，是**正常的失敗路徑** —— 必須 Fail Closed 回一組
    #    全 False 的 checks，而不是讓 AttributeError 打斷整個主迴圈。
    if not isinstance(sample, dict):
        return False, {k: False for k in ELIGIBILITY_ITEMS}

    soc = sample.get("soc")
    checks = {
        "tou_off_peak": sample.get("tou") == "OFF_PEAK",
        "soc_in_charge_band": (soc_max_pct is not None
                               and isinstance(soc, (int, float))
                               and float(soc) <= float(soc_max_pct)),
        "decision_is_charge": sample.get("decision") == "charge",
        "comm_ok": sample.get("comm_ok") is True,
        "fault_clear": sample.get("fault") is False,
        "alarm_clear": sample.get("critical_alarms") == 0,
        "local_fresh": (sample.get("fresh") is not False
                        and sample.get("meter_fresh") is True
                        and sample.get("ess_fresh") is True),
        "remote_healthy": bool(probe
                               and probe.get("screen_alive") is True
                               and probe.get("process_alive") is True
                               and probe.get("process_identity_ok") is True),
    }
    return all(checks.values()), checks


# ======================================================================
# C12. Polling / backoff —— 沿用既有契約；新增項目一律不預設
# ======================================================================
# ======================================================================
# C12-1. Retry backoff —— 「某次 READ / PROBE 失敗後，多久再試一次」
# ======================================================================
# 🔴 這**不是** service health timeout。兩者用途完全不同：
#      retry_backoff_sec          一次失敗後隔多久重試
#      service_health_timeout_sec 多久沒有任何有效進展 → 判服務失效
#    本檔刻意分成兩個獨立類別，避免任何一方被誤用成另一方。
#
# 🔴 值本身仍未定案（ServiceTiming 預設 None）。未提供即 **不重試**，
#    退回「一輪一次」的既有行為 —— Fail Closed，不自行發明數字。
#
# 契約邊界（**不是**候選值，是既有契約推導出的「不得越過」界線）：
#   下限 3.0 s  = Grid Meter STALE_AFTER_SEC(3.0) 與 PowerClassifier
#                 debounce_sec(3.0)。比這更快的重試不可能看到新狀態。
#   上限 10.0 s = ESS_READ_DURATION_MAX_SEC(10.0)；同時確保在
#                 decision_interval_sec(30) 內至少還容得下 2 次嘗試，
#                 才能複製既有 AUTH_LOST_STREAK=2 的「連兩輪才反應」語意。
RETRY_BACKOFF_LOWER_BOUND_SEC = 3.0
RETRY_BACKOFF_UPPER_BOUND_SEC = 10.0


def retry_backoff_in_contract(backoff_sec):
    """候選值是否落在既有契約推導出的區間內。None → False（未定案）。"""
    if backoff_sec is None:
        return False
    return (RETRY_BACKOFF_LOWER_BOUND_SEC <= float(backoff_sec)
            <= RETRY_BACKOFF_UPPER_BOUND_SEC)


class RetryPolicy(object):
    """
    唯讀重試器。**結構上不可能送出任何控制指令** —— 本類別不持有
    sender / dispatcher，只呼叫呼叫端注入的 read / probe 函式。

    storm ceiling：一個 decision interval 內的嘗試次數上限為
        floor(decision_interval_sec / backoff_sec)
    未設定 backoff 時上限固定為 1（等於不重試）。
    """

    def __init__(self, backoff_sec, decision_interval_sec,
                 max_attempts=None, sleeper=None):
        self.backoff_sec = backoff_sec
        self.decision_interval_sec = float(decision_interval_sec)
        self._max_attempts_override = max_attempts
        self._sleep = sleeper or (lambda s: None)
        self.attempts = 0
        self.waits = []

    @property
    def configured(self):
        return self.backoff_sec is not None

    def max_attempts(self):
        if self._max_attempts_override is not None:
            return max(1, int(self._max_attempts_override))
        if not self.configured or float(self.backoff_sec) <= 0:
            return 1
        return max(1, int(self.decision_interval_sec // float(self.backoff_sec)))

    def run(self, read_fn, ok_fn=None):
        """
        重試直到成功或用完 max_attempts()。回傳 (value, attempts, waits)。

        🔴 每次重試**之前**一定 sleep backoff_sec —— 不存在 busy-loop 路徑。
        🔴 成功即停；不做「成功後仍補打幾次」這種無謂流量。
        """
        ok_fn = ok_fn or (lambda v: v is not None)
        limit = self.max_attempts()
        value = None
        for i in range(limit):
            if i:
                self._sleep(float(self.backoff_sec))
                self.waits.append(float(self.backoff_sec))
            self.attempts += 1
            try:
                value = read_fn()
            except Exception:
                value = None
            if ok_fn(value):
                return value, self.attempts, list(self.waits)
        return value, self.attempts, list(self.waits)


# ======================================================================
# C12-2. Service health timeout —— 「多久沒有有效進展 → 服務健康未知」
# ======================================================================
# 🔴 這**不是** data freshness gate。資料新鮮度已由 ESS stale_after(15s) /
#    Meter STALE_AFTER_SEC(3s) 各自 Fail Closed。本機制看的是
#    **服務自身是否還在推進**（watchdog），四種進展任一發生都算：
PROGRESS_LOCAL = "local_observation"
PROGRESS_REMOTE = "remote_probe"
PROGRESS_AUDIT = "audit"
PROGRESS_CONTROL = "control_loop"
PROGRESS_KINDS = (PROGRESS_LOCAL, PROGRESS_REMOTE, PROGRESS_AUDIT,
                  PROGRESS_CONTROL)

HEALTH_OK = "HEALTHY"
HEALTH_UNKNOWN = "SERVICE_HEALTH_UNKNOWN"
HEALTH_UNCONFIGURED = "UNCONFIGURED"

# 契約邊界（同樣不是候選值本身）：
#   下限 = 最長「合法且連續」的無里程碑期間
#          pause_settling_timeout(120) + stable_verify span(45)
#          + 一個 decision interval(30) = 195 s，向上取 decision interval
#          整數倍 → 210 s。低於此值會在合法的 pause verification 中誤觸。
#   上限 = 現場實測外部 controller 約 80 kW 充電時 SOC 約 5% / 10 分鐘。
#          超過 600 s 等於容許卡死的服務在背著恢復責任的情況下，
#          睡過一次 >= 5% 的 SOC 位移而不自覺。
SERVICE_HEALTH_TIMEOUT_LOWER_BOUND_SEC = 210.0
SERVICE_HEALTH_TIMEOUT_UPPER_BOUND_SEC = 600.0


def service_health_timeout_in_contract(timeout_sec):
    if timeout_sec is None:
        return False
    return (SERVICE_HEALTH_TIMEOUT_LOWER_BOUND_SEC <= float(timeout_sec)
            <= SERVICE_HEALTH_TIMEOUT_UPPER_BOUND_SEC)


class ServiceHealth(object):
    """
    服務進展 watchdog。

    🔴 **結構上不可能送出任何指令** —— 本類別不接受、也不持有
       sender / dispatcher / remote / operator。逾時的唯一效果是回一個
       字串裁決；要不要因此 Fail Closed 由呼叫端決定。
    🔴 timeout 未設定 → `HEALTH_UNCONFIGURED`（**不是** HEALTHY）。
    """

    def __init__(self, timeout_sec, clock=None):
        self.timeout_sec = timeout_sec
        self._clock = clock or (lambda: 0.0)
        self.started_at = self._clock()
        self.last_progress_at = self.started_at
        self.last_kind = None
        self.marks = []

    def mark(self, kind):
        if kind not in PROGRESS_KINDS:
            raise ValueError("未知的 progress 種類：%r" % (kind,))
        self.last_progress_at = self._clock()
        self.last_kind = kind
        self.marks.append((kind, self.last_progress_at))
        return self.last_progress_at

    def stalled_sec(self):
        return self._clock() - self.last_progress_at

    def verdict(self):
        if self.timeout_sec is None:
            return HEALTH_UNCONFIGURED
        if self.stalled_sec() > float(self.timeout_sec):
            return HEALTH_UNKNOWN
        return HEALTH_OK

    def healthy(self):
        return self.verdict() == HEALTH_OK


# 🔴 Phase 6.10-C1 已核准（2026-09-03）。修改任一項都必須重新取得裁示。
#
#   retry_backoff_sec = 5.0
#     合理區間 3.0 ~ 10.0：下限 = Meter STALE_AFTER_SEC / PowerClassifier
#     debounce（皆 3.0），低於此值重試看到的必然是同一筆快照；上限 =
#     ESS_READ_DURATION_MAX_SEC(10.0)，且需在 30 s decision interval 內
#     容得下 2 次嘗試。5.0 對齊既有 Socket.IO reconnection_delay_max(5)，
#     遠低於 ESS stale_after(15)。storm ceiling = floor(30/5) = 6 次／輪。
#
#   service_health_timeout_sec = 300.0
#     下限 = 最長合法連續無里程碑期間 settling(120) + stable verify span(45)
#     + 一個 decision interval(30) = 195 s，向上取 30 的整數倍 → 210 s。
#     300 = 10 × decision_interval，對 195 s 有約 1.54 倍餘裕、> 2×settling，
#     且涵蓋現場 iteration 分布約 99.31%；又不至於寬到 600 s 才察覺卡死。
APPROVED_RETRY_BACKOFF_SEC = 5.0
APPROVED_SERVICE_HEALTH_TIMEOUT_SEC = 300.0

REQUIRED_TIMING_FIELDS = ("retry_backoff_sec", "service_health_timeout_sec")


class ServiceTiming(object):
    """
    🔴 `decision_interval_sec` 沿用既有 production config，不另訂。
    🔴 `retry_backoff_sec` / `service_health_timeout_sec` 已於 Phase 6.10-C1
       取得裁示（5.0 / 300.0），證據見上方註解。**參數已定案 ≠ live 已啟用**
       —— DISPATCH_ENABLED 仍為 False、MODE 仍為 DRY_RUN。
    🔴 仍可注入 None 來演練「未設定」路徑：RetryPolicy 退回不重試、
       ServiceHealth 回 UNCONFIGURED，兩者都是 Fail Closed。
    """

    def __init__(self, decision_interval_sec=None,
                 retry_backoff_sec=APPROVED_RETRY_BACKOFF_SEC,
                 service_health_timeout_sec=APPROVED_SERVICE_HEALTH_TIMEOUT_SEC):
        if decision_interval_sec is None:
            import pcs_auto_control_config as CFG
            decision_interval_sec = CFG.DEFAULT_CONTROL_CONFIG.decision_interval_sec
        self.decision_interval_sec = decision_interval_sec
        self.retry_backoff_sec = retry_backoff_sec
        self.service_health_timeout_sec = service_health_timeout_sec

    def missing(self):
        """仍未取得證據的 timing 參數。兩項皆已定案後應為 []。"""
        return [k for k in REQUIRED_TIMING_FIELDS
                if getattr(self, k) is None]


# ======================================================================
# C4.10. Live Readiness Report —— 純離線評估，不連線、不送任何指令
# ======================================================================
READY = "READY"
BLOCKED = "BLOCKED"

READINESS_ITEMS = (
    "NETWORK_IDENTITY_STABILITY", "REMOTE_GUARD_VARIANT",
    "REMOTE_CAPABILITY_REPORT", "PAUSE_CAPABILITY", "RESTORE_CAPABILITY",
    "SSH_CONNECT_TIMEOUT", "PROBE_TIMEOUT", "STATUS_TIMEOUT",
    "LOOPCHECK_TIMEOUT", "PAUSE_TIMEOUT_VERIFIED", "RESTORE_TIMEOUT_VERIFIED",
    "SERVICE_TIMING", "RECOVERY_CLEAN", "DISPATCH_ENABLED", "MODE",
    "FIRST_LIVE_PREREQUISITE",
)


def live_readiness_report(capability=None, recovery_verdict=None,
                          mode=HO.MODE_DRY_RUN, timeouts=None, timing=None):
    """
    回傳 (verdict, rows, blocked)。

    🔴 **純離線**：只讀模組常數與呼叫端注入的物件，不連線、不執行任何動詞。
    🔴 每一列都同時給「值」與「是否放行」，因此 BLOCKED 的理由是逐項可讀的，
       不是一句「還沒好」。
    """
    to = timeouts if timeouts is not None else RA.SshTimeouts()
    tm = timing if timing is not None else ServiceTiming()
    cg_ok, cg = RA.capability_gate(capability)
    cap = capability

    def row(name, value, ok, note=None):
        return {"item": name, "value": value, "ok": bool(ok), "note": note}

    rows = [
        row("NETWORK_IDENTITY_STABILITY", NETWORK_IDENTITY_STABILITY,
            NETWORK_IDENTITY_STABILITY == NET_PASS,
            "B1.7 受控 reconnect / DHCP renew 驗證"),
        row("REMOTE_GUARD_VARIANT", RA.DEPLOYED_GUARD_VARIANT,
            RA.DEPLOYED_GUARD_VARIANT == "B2", "本機部署紀錄"),
        row("REMOTE_CAPABILITY_REPORT",
            (cap.status if cap is not None else RA.CAP_NOT_REPORTED),
            cg["capability_reported"], "guard 自我回報，非本機推測"),
        row("PAUSE_CAPABILITY",
            (cap.capabilities.get("pause") if cap is not None else None),
            cg["capability_pause"]),
        row("RESTORE_CAPABILITY",
            (cap.capabilities.get("restore") if cap is not None else None),
            cg["capability_restore"]),
        row("SSH_CONNECT_TIMEOUT", to.connect_timeout_sec,
            to.connect_timeout_sec is not None,
            RA.SshTimeouts.status_of("ssh_connect_timeout_sec")),
        row("PROBE_TIMEOUT", to.command_timeout_for("probe"),
            to.command_timeout_for("probe") is not None,
            RA.SshTimeouts.status_of("probe")),
        row("STATUS_TIMEOUT", to.command_timeout_for("status"),
            to.command_timeout_for("status") is not None,
            RA.SshTimeouts.status_of("status")),
        row("LOOPCHECK_TIMEOUT", to.command_timeout_for("loopcheck"),
            to.command_timeout_for("loopcheck") is not None,
            RA.SshTimeouts.status_of("loopcheck")),
        # 🔴 「有候選值」不等於「已驗證」。這兩列看的是 field 驗證狀態，
        #    不是值本身 —— 注入結構候選值也不會讓它們變 True。
        row("PAUSE_TIMEOUT_VERIFIED",
            RA.SshTimeouts.status_of("pause") == RA.TIMEOUT_FINAL,
            RA.SshTimeouts.status_of("pause") == RA.TIMEOUT_FINAL,
            "目前僅 STRUCTURAL CANDIDATE 12.0"),
        row("RESTORE_TIMEOUT_VERIFIED",
            RA.SshTimeouts.status_of("restore") == RA.TIMEOUT_FINAL,
            RA.SshTimeouts.status_of("restore") == RA.TIMEOUT_FINAL,
            "目前僅 STRUCTURAL CANDIDATE 12.0"),
        row("SERVICE_TIMING", tm.missing() or "COMPLETE", not tm.missing(),
            "retry_backoff / service_health_timeout"),
        row("RECOVERY_CLEAN", recovery_verdict,
            recovery_verdict == HO.R_CLEAN),
        row("DISPATCH_ENABLED", HO.DISPATCH_ENABLED,
            HO.DISPATCH_ENABLED is True),
        row("MODE", mode, mode == HO.MODE_ARMED),
        row("FIRST_LIVE_PREREQUISITE", FIRST_LIVE_PREREQUISITE_SATISFIED,
            FIRST_LIVE_PREREQUISITE_SATISFIED is True),
    ]
    blocked = [r["item"] for r in rows if not r["ok"]]
    return (READY if not blocked else BLOCKED), rows, blocked


def format_readiness(verdict, rows, blocked):
    out = ["LIVE_READINESS = %s" % verdict, ""]
    for r in rows:
        out.append("  %-4s %-28s %s%s"
                   % ("OK" if r["ok"] else "NO", r["item"], r["value"],
                      ("   # " + r["note"]) if r["note"] else ""))
    if blocked:
        out.append("")
        out.append("  blocked reasons (%d):" % len(blocked))
        for b in blocked:
            out.append("    - %s" % b)
    return "\n".join(out)


# ======================================================================
# C1/C2/C9. Service
# ======================================================================
BOOT_OK = "READY"
BOOT_REFUSED = "REFUSED"

SHUTDOWN_CLEAN = "CLEAN"
SHUTDOWN_CRITICAL = "RESTORE_FAILED_CRITICAL"


class UnattendedService(object):
    """
    無人值守 orchestrator。**離線可完整演練，預設不可能送出任何指令。**
    """

    def __init__(self, journal, instance_guard, sample_source, probe_source,
                 loop_mark_source=None, senders=None, dispatcher=None,
                 mode=HO.MODE_DRY_RUN, soc_max_pct=None, timing=None,
                 sleeper=None, config=None, clock=None):
        self.journal = journal
        self.guard = instance_guard
        self.sample_source = sample_source
        self.probe_source = probe_source
        self.loop_mark_source = loop_mark_source
        self.senders = senders
        self.dispatcher = dispatcher
        self.mode = mode
        self.soc_max_pct = soc_max_pct
        self.timing = timing or ServiceTiming()
        self._sleep = sleeper or (lambda s: None)
        self.cfg = config or HO.HandoffConfig
        self.state = None
        self.recovery = None
        self.critical = None
        self.restore_responsibility = False
        self.ticks = 0
        self.handoffs = []
        self._clock = clock or (lambda: 0.0)
        # 🔴 兩個 retry policy 刻意分開：
        #    本機觀測可在一輪內重試數次（純 HTTP GET，量級與既有
        #    auto_monitor 相同）；遠端 probe 每輪只准一次 —— 對別人的
        #    production 主機連續打 SSH 是不可接受的流量。
        self.retry_local = RetryPolicy(self.timing.retry_backoff_sec,
                                       self.timing.decision_interval_sec,
                                       sleeper=self._sleep)
        self.retry_remote = RetryPolicy(self.timing.retry_backoff_sec,
                                        self.timing.decision_interval_sec,
                                        max_attempts=1, sleeper=self._sleep)
        self.health = ServiceHealth(self.timing.service_health_timeout_sec,
                                    clock=self._clock)

    # ---- C2. 啟動 ----
    def boot(self):
        """回傳 (verdict, detail)。啟動第一件事一定是 recover()。"""
        ok, why = self.guard.acquire()
        if not ok:
            # 🔴 第二個 instance 一律拒絕啟動，且**不寫 SERVICE_START**
            return BOOT_REFUSED, f"SINGLE_INSTANCE: {why}"
        self.journal.append(EV_SERVICE_START, detail=f"mode={self.mode}")

        probe = self.probe_source()
        running = None
        if probe:
            # 遠端 probe 只能回答「controller 是否仍在跑」，不猜 ownership
            running = probe.get("controller_running")
        verdict, last, action = HO.recover(self.journal, {"running": running})
        self.recovery = verdict
        self.journal.append(EV_RECOVERY, state=last,
                            detail=f"{verdict}: {action}")

        if verdict == HO.R_PENDING_RESTORE:
            # 🔴 重啟不得把 ownership 重設為 IDLE；責任必須被承接
            self.restore_responsibility = True
            self.state = BOOT_REFUSED
            return BOOT_REFUSED, "PENDING_RESTORE: 必須先處理既有恢復責任"
        if verdict == HO.R_CRITICAL:
            self.critical = CRIT_RESTORE_FAILED
            self.journal.append(EV_CRITICAL, detail="上次以 CRITICAL 收場")
            self.state = BOOT_REFUSED
            return BOOT_REFUSED, "CRITICAL: 需人工確認後才可再啟動"
        if verdict == HO.R_UNKNOWN:
            self.critical = CRIT_RECOVERY_UNKNOWN
            self.journal.append(EV_CRITICAL, detail=CRIT_RECOVERY_UNKNOWN)
            self.state = BOOT_REFUSED
            return BOOT_REFUSED, "RECOVERY_UNKNOWN: Fail Closed"

        self.state = BOOT_OK
        return BOOT_OK, "recovery CLEAN"

    # ---- C1. 主迴圈 ----
    def tick(self):
        """
        一輪：OBSERVE → EVALUATE → （允許才）HANDOFF → AUDIT。
        回傳該輪決策 dict。
        """
        self.ticks += 1
        sample, _sa, _sw = self.retry_local.run(self.sample_source)
        probe, _pa, _pw = self.retry_remote.run(self.probe_source)
        # 只有「真的拿到東西」才算進展；拿到 None 不算。
        if sample is not None:
            self.health.mark(PROGRESS_LOCAL)
        if probe is not None:
            self.health.mark(PROGRESS_REMOTE)

        elig, echecks = evaluate_eligibility(sample, probe, self.soc_max_pct)
        # 🔴 health 是 live gate 的第 11 項，不是平行的另一層判斷。
        #    UNKNOWN / UNCONFIGURED 皆使 gate FAIL → LIVE HANDOFF REFUSED，
        #    但**不設 self.critical** —— 這是 recoverable fail-closed，
        #    進展恢復後同一個 gate 可以再度 PASS。
        hv = self.health.verdict()
        cap = RA.parse_capability((probe or {}).get("raw_output")) \
            if isinstance(probe, dict) else None
        allowed, gchecks = live_handoff_allowed(
            self.mode, self.recovery,
            local_fresh=echecks["local_fresh"], remote_probe=probe,
            health=hv, capability=cap)
        decision = "HANDOFF" if (elig and allowed) else "NO_HANDOFF"
        blocked = [k for k, v in list(echecks.items()) + list(gchecks.items())
                   if not v]
        # durable：本輪為何不交接，逐項留在 ELIGIBILITY_DECISION 裡。
        self.journal.append(EV_ELIGIBILITY,
                            detail=f"{decision}; health={hv}; 未過={blocked}")

        out = {"tick": self.ticks, "decision": decision,
               "eligible": elig, "live_allowed": allowed,
               "eligibility": echecks, "gates": gchecks, "blocked": blocked,
               "health": hv, "stalled_sec": self.health.stalled_sec(),
               "local_attempts": _sa, "remote_attempts": _pa}
        if decision != "HANDOFF":
            return out

        self.journal.append(EV_HANDOFF_BEGIN)
        o = HO.HandoffOrchestrator(
            remote=HO.RemoteController(
                pause_sender=(self.senders.pause if self.senders else None),
                restore_sender=(self.senders.restore if self.senders else None)),
            observe=self.sample_source, gates=(lambda x: (True, {})),
            journal=self.journal, mode=self.mode,
            idle_baseline_kw=self.cfg.idle_power_lower_kw,
            power_tolerance_kw=0.0)
        r = HO.HandoffRunner(o, self.sample_source, self.probe_source,
                             loop_mark_source=self.loop_mark_source,
                             dispatcher=self.dispatcher, config=self.cfg,
                             soc_max_pct=self.soc_max_pct,
                             sleeper=self._sleep)
        res = r.run()
        # 交接跑完一輪 = control-loop 有進展
        self.health.mark(PROGRESS_CONTROL)
        self.restore_responsibility = res["restore_responsibility"]
        if res["outcome"] == "RESTORE_FAILED_CRITICAL":
            self.critical = CRIT_RESTORE_FAILED
            self.journal.append(EV_CRITICAL, detail=res["detail"])
        else:
            self.journal.append(EV_PHASE6_TAKEOVER, detail=res["outcome"])
            self.journal.append(EV_PHASE6_RELEASE, detail=res["outcome"])
        self.handoffs.append(res)
        out["handoff"] = res
        return out

    def run(self, max_ticks=1):
        results = []
        for _ in range(max_ticks):
            if self.critical:
                break
            # storm ceiling 是「每輪」而非「終生」，因此每輪歸零
            self.retry_local.attempts = 0
            self.retry_remote.attempts = 0
            results.append(self.tick())
            self._sleep(self.timing.decision_interval_sec)
        return results

    # ---- C9. Graceful shutdown ----
    def shutdown(self):
        """
        回傳 (verdict, detail)。

        🔴 若 external 已 pause，**不得直接 exit** —— 必須先歸還。
        🔴 歸還失敗一律 RESTORE_FAILED_CRITICAL，**不得**記成 clean shutdown。
        """
        if not self.restore_responsibility:
            self.journal.append(EV_SERVICE_SHUTDOWN, detail=SHUTDOWN_CLEAN)
            self.guard.release()
            return SHUTDOWN_CLEAN, "無恢復責任"

        sent = False
        if self.senders is not None and self.mode == HO.MODE_ARMED:
            self.journal.append("INTENT", step="RESTORE",
                                detail="shutdown 前歸還")
            sent = bool(self.senders.restore())
            self.journal.append("OUTCOME", step="RESTORE", ok=sent)
        if not sent:
            self.critical = CRIT_RESTORE_FAILED
            self.journal.append(EV_CRITICAL, detail="shutdown 時歸還失敗")
            self.journal.append(EV_SERVICE_SHUTDOWN, detail=SHUTDOWN_CRITICAL)
            self.guard.release()
            return SHUTDOWN_CRITICAL, "歸還失敗 —— 不得記為 clean shutdown"

        mark_a = self.loop_mark_source() if self.loop_mark_source else None
        mark_b = self.loop_mark_source() if self.loop_mark_source else None
        resumed = (mark_a is not None and mark_b is not None and mark_b > mark_a)
        if not resumed:
            self.critical = CRIT_RESTORE_FAILED
            self.journal.append(EV_CRITICAL, detail="歸還後 loop 未恢復")
            self.journal.append(EV_SERVICE_SHUTDOWN, detail=SHUTDOWN_CRITICAL)
            self.guard.release()
            return SHUTDOWN_CRITICAL, "loop 未恢復"
        self.restore_responsibility = False
        self.journal.append(EV_SERVICE_SHUTDOWN, detail=SHUTDOWN_CLEAN)
        self.guard.release()
        return SHUTDOWN_CLEAN, "已歸還並確認 loop 恢復"


def main(argv=None):
    print("=" * 72)
    print("  Phase 6.10-C Unattended Service（離線自述）")
    print("=" * 72)
    print(f"  MODE 預設                 : {HO.MODE_DRY_RUN}")
    print(f"  DISPATCH_ENABLED          : {HO.DISPATCH_ENABLED}")
    print(f"  NETWORK_IDENTITY_STABILITY: {NETWORK_IDENTITY_STABILITY}")
    print(f"  部署中的 guard            : {RA.DEPLOYED_GUARD_VARIANT}")
    print(f"  remote pause capable      : {remote_pause_capable()}")
    print(f"  remote restore capable    : {remote_restore_capable()}")
    print(f"  FIRST LIVE prerequisite   : {FIRST_LIVE_PREREQUISITE_SATISFIED}")
    allowed, checks = live_handoff_allowed(HO.MODE_ARMED, HO.R_CLEAN, True,
                                           {"screen_alive": True,
                                            "process_alive": True,
                                            "process_identity_ok": True},
                                           health=HEALTH_OK)
    print(f"\n  即使以最寬鬆輸入評估 live_handoff_allowed : {allowed}")
    for k, v in checks.items():
        print(f"    {'OK ' if v else 'NO '} {k}")
    t = ServiceTiming()
    print(f"\n  decision_interval_sec     : {t.decision_interval_sec}（沿用既有）")
    print(f"  retry_backoff_sec         : {t.retry_backoff_sec}（C1 已核准）")
    print(f"  service_health_timeout_sec: {t.service_health_timeout_sec}"
          f"（C1 已核准）")
    print(f"  尚未取得證據的新參數       : {t.missing()}")
    print("  SSH client timeout        : UNRESOLVED"
          "（connect / command 需分開，且 command 需依 verb 區分）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
