# -*- coding: utf-8 -*-
"""
Phase 6.5-E LastControl Store（最後一次已驗證成功控制的持久化）
======================================================================
記錄「上一次真正經 Read-back 驗證成功的 PCS 控制是什麼、何時發生」。
**不是**每一次 API 呼叫的紀錄。

⚠️ 只有 Phase 6.5-D 的 `VERIFY_SUCCESS`（`lastcontrol_eligible=True`）才允許更新。
   `control_success=True`（API 已接受）**不得**更新 —— 那還沒經過 PCS read-back。
⚠️ 本模組不控制 PCS/BMS、不登入 HMI、不連 6160、不呼叫 device_control_operator。
   唯一的副作用是讀寫自己的 JSON 檔。
⚠️ 不設定 `min_switch_interval_sec`；未設定即 `CONFIG_NOT_READY`，
   **絕不 fallback 到 15 / 60 秒，也不得挪用 Decision Policy 的 min_hold_sec**
   （那是策略切換抑制，與真實 PCS 控制命令間隔是兩回事）。

為什麼不沿用 device_control_action_result.json
    那是既有 operator 的「最後一次 action 結果」，語意是「API 呼叫發生過什麼」，
    且其 success 會被 partial verify 汙染。本模組語意是「最後一次**經 read-back
    驗證成功**的控制」，必須獨立檔案，且不修改既有格式。

時間策略（monotonic 與 wall clock 分工）
    verified_at_monotonic  只用於「同一 boot 內」計算 elapsed
    verified_at_wall       只用於人類稽核與 log，**永不參與 interval 計算**
                           （wall clock 可能被校時而倒退）
    跨 process / 跨 reboot 後，舊的 monotonic 與新的 monotonic 不可比較，
    因此載入的紀錄必須先判定信任層級：
        TRUSTED_FOR_INTERVAL  可用於正式 interval 判斷
        HISTORY_ONLY          只能當歷史看，不得用於 interval
        INVALID               無可用紀錄

boot identity
    以可注入的 boot_id_provider 表示；**production 預設為 windows_boot_id()**
    （ntdll BootTime，見下方區塊的實測依據與選型理由）。
    boot_id 無法取得（回 None）時，任何從檔案載入的紀錄一律只能是 HISTORY_ONLY。
    本 process 自己寫入的紀錄則保留在記憶體中，可直接信任（同 process 必同 boot）。

    另已實測：Windows 上 time.monotonic() 跨 process 共用同一時間軸
    （不同 PID 的 Δmonotonic 與 Δwall 差異 0.0000s，且與 GetTickCount64 uptime 吻合），
    因此 verified_at_monotonic 在 **Service restart 後仍可比較**；
    boot_id 的唯一職責就是判斷「是否已 reboot」。

Atomic write
    已檢查專案既有程式：沒有可重用的 atomic JSON helper
    （各模組的 save_json 皆為直接覆寫；os.replace 只用於 log rotation 與 xlsx）。
    本模組自行實作 temp → flush → fsync → os.replace，避免 crash 留下半份 JSON。

用法（完全離線）
    python last_control_store.py --demo
"""

import os
import sys
import json
import math
import time
import ctypes
import argparse
from dataclasses import dataclass, asdict, field
from datetime import datetime


# ======================================================================
# 設定區
# ======================================================================
SCHEMA_VERSION = 1

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE_PATH = os.path.join(_PROJECT_ROOT, "output", "phase6_last_control.json")

# 可記錄的控制動作（封閉值域；**不含 none**）
ACT_CHARGE = "charge"
ACT_DISCHARGE = "discharge"
ACT_STOP = "stop"
VALID_ACTIONS = frozenset({ACT_CHARGE, ACT_DISCHARGE, ACT_STOP})
ACTIONS_WITH_POWER = frozenset({ACT_CHARGE, ACT_DISCHARGE})     # stop 的 power 必須為 None

# ---- 載入結果 ----
LOAD_OK = "OK"
LOAD_NOT_FOUND = "NOT_FOUND"
LOAD_INVALID_JSON = "INVALID_JSON"
LOAD_INVALID_SCHEMA = "INVALID_SCHEMA"
LOAD_UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
LOAD_INVALID_FIELD = "INVALID_FIELD"
LOAD_IO_ERROR = "IO_ERROR"

LOAD_OUTCOMES = frozenset({LOAD_OK, LOAD_NOT_FOUND, LOAD_INVALID_JSON, LOAD_INVALID_SCHEMA,
                           LOAD_UNSUPPORTED_SCHEMA, LOAD_INVALID_FIELD, LOAD_IO_ERROR})

# ---- 信任層級 ----
TRUST_FOR_INTERVAL = "TRUSTED_FOR_INTERVAL"
TRUST_HISTORY_ONLY = "HISTORY_ONLY"
TRUST_INVALID = "INVALID"

# ---- 更新結果 ----
UPD_UPDATED = "UPDATED"
UPD_NOT_UPDATED = "NOT_UPDATED"
UPD_WRITE_FAILED = "WRITE_FAILED"

# ---- interval 判定結果 ----
IV_CONFIG_NOT_READY = "CONFIG_NOT_READY"
IV_NO_TRUSTED_HISTORY = "NO_TRUSTED_HISTORY"
IV_WITHIN_INTERVAL = "WITHIN_INTERVAL"
IV_INTERVAL_SATISFIED = "INTERVAL_SATISFIED"

# ---- 原因 ----
R_OK = "OK"
R_NOT_ELIGIBLE = "NOT_ELIGIBLE"                 # 未經 read-back 驗證成功
R_SAME_BOOT = "SAME_BOOT"
R_SAME_PROCESS = "SAME_PROCESS"
R_BOOT_ID_UNKNOWN = "BOOT_ID_UNKNOWN"
R_BOOT_ID_MISMATCH = "BOOT_ID_MISMATCH"
R_NO_RECORD = "NO_RECORD"
R_INTERVAL_NOT_CONFIGURED = "MIN_SWITCH_INTERVAL_NOT_CONFIGURED"

_WALL_FMT = "%Y-%m-%d %H:%M:%S"


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v):
    return _is_number(v) and math.isfinite(v)


class _NonFiniteConstant(ValueError):
    """JSON 內出現 NaN / Infinity / -Infinity（非標準 JSON）。"""


def _reject_constant(s):
    raise _NonFiniteConstant(s)


# ======================================================================
# Windows boot identity（Phase 6.5-G 核可的 production 來源）
# ======================================================================
# 來源：ntdll.NtQuerySystemInformation(SystemTimeOfDayInformation=3).BootTime
#       BootTime 為自 1601-01-01 起算的 100ns 整數。
#
# 實測依據（2026-08-20，本機唯讀量測）
#   同一 process 連讀 5 次      → 完全相同
#   4 個獨立 Python process     → 完全相同
#   不需 Administrator（實測 IsAdmin=False 亦可取得）
#   純標準庫 ctypes，不需 subprocess，取得即時
#   換算本地時間 2026-08-17 10:40:34.082604
#     與 PowerShell Win32_OperatingSystem.LastBootUpTime 完全吻合（獨立來源交叉驗證）
#
# 為何不用其他候選
#   GetTickCount64 推導 boot epoch：同一 process 內就有 ~10.8ms 抖動 → 非 deterministic
#   QueryUnbiasedInterruptTime    ：排除睡眠時間，不是真實 uptime
#   WMI LastBootUpTime            ：值同樣穩定，但每次 subprocess 約 0.5s
#
# ⚠️ **保留 BootTime 原始精度，不取整到秒。**
#    若日後 NTP 校時導致 BootTime 微調 → boot_id 改變 → LastControl 降為 HISTORY_ONLY，
#    那是 fail-closed 的正確方向；不得為了避免 HISTORY_ONLY 而預先降低識別精度。
#
# ⚠️ 任何取得失敗（非 Windows、API 不可用、NTSTATUS 非 0）一律回 None
#    → 載入的紀錄只能是 HISTORY_ONLY，同樣 fail closed。
_SYSTEM_TIME_OF_DAY_INFORMATION = 3


class _SystemTimeOfDayInformation(ctypes.Structure):
    _fields_ = [("BootTime", ctypes.c_longlong),
                ("CurrentTime", ctypes.c_longlong),
                ("TimeZoneBias", ctypes.c_longlong),
                ("TimeZoneId", ctypes.c_ulong),
                ("Reserved", ctypes.c_ulong),
                ("BootTimeBias", ctypes.c_ulonglong),
                ("SleepTimeBias", ctypes.c_ulonglong)]


def windows_boot_time_100ns():
    """回傳本次開機時刻（自 1601 起算的 100ns 整數）；無法取得時回 None。"""
    ntdll = getattr(ctypes, "windll", None)
    if ntdll is None:                                  # 非 Windows
        return None
    try:
        info = _SystemTimeOfDayInformation()
        status = ctypes.windll.ntdll.NtQuerySystemInformation(
            _SYSTEM_TIME_OF_DAY_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None)
    except Exception:
        return None
    if status != 0 or info.BootTime <= 0:
        return None
    return int(info.BootTime)


def windows_boot_id():
    """
    production boot identity。回傳 `"winboot-<BootTime>"`，無法取得時回 None。

    同一次 Windows boot 內固定不變（含 Python / Service 重啟）；reboot 後必然改變。
    """
    bt = windows_boot_time_100ns()
    return None if bt is None else f"winboot-{bt}"


# ======================================================================
# 資料模型
# ======================================================================
@dataclass(frozen=True)
class LastControlRecord:
    """
    最後一次**經 read-back 驗證成功**的 PCS 控制。

    target_power_kw   charge / discharge 保留實際送出的原始 float；stop 一律 None
    verified_at_wall  人類稽核用，**不參與 interval 計算**
    verified_at_monotonic  僅同一 boot 內可比較
    actual_active_power_kw  read-back 當下的觀測值（診斷用，不參與任何判定）
    """
    schema_version: int
    action: str
    target_power_kw: float
    verified_at_wall: str
    verified_at_monotonic: float
    pcs_actual_state: str
    actual_active_power_kw: float
    reason: str
    boot_id: str = None

    def as_dict(self):
        return asdict(self)

    def __str__(self):
        pw = "n/a" if self.target_power_kw is None else f"{self.target_power_kw:g}kW"
        return (f"[LASTCTRL] {self.action:<9} power={pw} "
                f"wall={self.verified_at_wall} mono={self.verified_at_monotonic:.3f} "
                f"pcs={self.pcs_actual_state} boot={self.boot_id}")


@dataclass(frozen=True)
class LastControlLoadResult:
    """區分「沒有歷史」與「歷史檔損壞」—— 兩者對維運的意義完全不同。"""
    outcome: str
    record: object = None
    reason: str = ""
    detail: str = ""
    path: str = None

    @property
    def ok(self):
        return self.outcome == LOAD_OK

    def as_dict(self):
        d = asdict(self)
        d["record"] = self.record.as_dict() if self.record else None
        return d

    def __str__(self):
        return f"[LOAD] {self.outcome:<20} {self.detail or self.reason}"


@dataclass(frozen=True)
class LastControlTrustResult:
    trust: str
    record: object = None
    reason: str = ""
    detail: str = ""
    source: str = None              # "memory" / "file" / None

    @property
    def usable_for_interval(self):
        return self.trust == TRUST_FOR_INTERVAL

    def as_dict(self):
        d = asdict(self)
        d["record"] = self.record.as_dict() if self.record else None
        return d

    def __str__(self):
        act = self.record.action if self.record else "-"
        return f"[TRUST] {self.trust:<20} action={act:<9} source={self.source} reason={self.reason}"


@dataclass(frozen=True)
class UpdateResult:
    outcome: str
    record: object = None
    reason: str = ""
    detail: str = ""
    path: str = None

    @property
    def updated(self):
        return self.outcome == UPD_UPDATED

    def __str__(self):
        return f"[UPDATE] {self.outcome:<14} reason={self.reason} {self.detail}"


@dataclass(frozen=True)
class IntervalCheckResult:
    """
    ⚠️ 本結果只是「可信歷史 + 已配置門檻」下的算術判定，
       **實際是否放行控制仍由 Phase 6.4 Safety Gate 決定**。
    """
    outcome: str
    reason: str
    elapsed_sec: float = None
    required_sec: float = None
    trust: str = None
    last_action: str = None
    detail: str = ""

    @property
    def satisfied(self):
        return self.outcome == IV_INTERVAL_SATISFIED

    def __str__(self):
        el = "n/a" if self.elapsed_sec is None else f"{self.elapsed_sec:.1f}s"
        return (f"[INTERVAL] {self.outcome:<22} elapsed={el} "
                f"required={self.required_sec} trust={self.trust}")


# ======================================================================
# 驗證（載入路徑）
# ======================================================================
_REQUIRED_STR = ("action", "verified_at_wall", "pcs_actual_state", "reason")


def validate_record_dict(data):
    """
    驗證載入的 dict。回傳 (outcome, record_or_None, detail)。
    任何不符一律 fail closed —— 不「盡量猜著讀」。
    """
    if not isinstance(data, dict):
        return LOAD_INVALID_SCHEMA, None, f"頂層型別為 {type(data).__name__}"
    if "schema_version" not in data:
        return LOAD_INVALID_SCHEMA, None, "缺少 schema_version"
    sv = data.get("schema_version")
    if not isinstance(sv, int) or isinstance(sv, bool):
        return LOAD_INVALID_SCHEMA, None, f"schema_version 型別錯誤：{sv!r}"
    if sv != SCHEMA_VERSION:
        return LOAD_UNSUPPORTED_SCHEMA, None, f"schema_version={sv}，本版支援 {SCHEMA_VERSION}"

    missing = [k for k in _REQUIRED_STR + ("target_power_kw", "verified_at_monotonic",
                                           "actual_active_power_kw") if k not in data]
    if missing:
        return LOAD_INVALID_FIELD, None, "缺少欄位：" + ",".join(sorted(missing))

    for k in _REQUIRED_STR:
        if not isinstance(data[k], str) or not data[k]:
            return LOAD_INVALID_FIELD, None, f"{k} 需為非空字串：{data[k]!r}"

    act = data["action"]
    if act not in VALID_ACTIONS:
        return LOAD_INVALID_FIELD, None, f"未知 action：{act!r}（合法：{sorted(VALID_ACTIONS)}）"

    mono = data["verified_at_monotonic"]
    if not _finite(mono):
        return LOAD_INVALID_FIELD, None, f"verified_at_monotonic 需為有限數值：{mono!r}"

    pw = data["target_power_kw"]
    if act in ACTIONS_WITH_POWER:
        if not _finite(pw) or pw <= 0:
            return LOAD_INVALID_FIELD, None, f"{act} 的 target_power_kw 需為正的有限數值：{pw!r}"
    elif pw is not None:
        return LOAD_INVALID_FIELD, None, f"stop 的 target_power_kw 必須為 None：{pw!r}"

    obs = data["actual_active_power_kw"]
    if obs is not None and not _finite(obs):
        return LOAD_INVALID_FIELD, None, f"actual_active_power_kw 需為 None 或有限數值：{obs!r}"

    try:
        datetime.strptime(data["verified_at_wall"], _WALL_FMT)
    except ValueError:
        return LOAD_INVALID_FIELD, None, f"verified_at_wall 格式錯誤：{data['verified_at_wall']!r}"

    bid = data.get("boot_id")
    if bid is not None and (not isinstance(bid, str) or not bid):
        return LOAD_INVALID_FIELD, None, f"boot_id 需為 None 或非空字串：{bid!r}"

    return LOAD_OK, LastControlRecord(
        schema_version=sv, action=act, target_power_kw=pw,
        verified_at_wall=data["verified_at_wall"], verified_at_monotonic=float(mono),
        pcs_actual_state=data["pcs_actual_state"],
        actual_active_power_kw=(float(obs) if obs is not None else None),
        reason=data["reason"], boot_id=bid), ""


# ======================================================================
# Store
# ======================================================================
class LastControlStore:
    """
    LastControl 的建立、持久化與信任判定。

    boot_id_provider / clock / wall_clock 皆可注入，供離線測試模擬
    service restart 與 Windows reboot。**production boot id 來源尚未確認 → 預設 None。**
    """

    def __init__(self, path=DEFAULT_STORE_PATH, boot_id_provider=None,
                 clock=time.monotonic, wall_clock=None):
        self.path = path
        # 預設採 Phase 6.5-G 核可的 production 來源；測試以注入覆寫。
        self._boot_id_provider = boot_id_provider if boot_id_provider is not None else windows_boot_id
        self._clock = clock
        self._wall = wall_clock if wall_clock is not None else (
            lambda: datetime.now().strftime(_WALL_FMT))
        self._memo = None                 # 本 process 自己寫入的紀錄（同 process 必同 boot）

    # ---- 內部 ----
    def _boot_id(self):
        try:
            v = self._boot_id_provider()
        except Exception:
            return None
        return v if (isinstance(v, str) and v) else None

    def _atomic_write(self, payload):
        """temp → flush → fsync → os.replace，避免 crash 留下半份 JSON。"""
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ---- 寫入 ----
    def update_from_verified_result(self, result, ignore_eligibility=False):
        """
        由 Phase 6.5-C/D 的 VerifiedControlResult 更新。

        **第一件事就是檢查 lastcontrol_eligible** —— 避免上層忘記檢查 VERIFY_SUCCESS。
        ignore_eligibility 僅供測試用，production 不得使用。
        """
        if result is None:
            return UpdateResult(UPD_NOT_UPDATED, reason=R_NOT_ELIGIBLE,
                                detail="未提供 VerifiedControlResult", path=self.path)
        if not ignore_eligibility and getattr(result, "lastcontrol_eligible", False) is not True:
            return UpdateResult(UPD_NOT_UPDATED, reason=R_NOT_ELIGIBLE,
                                detail=f"outcome={getattr(result, 'outcome', None)}"
                                       "（僅 VERIFY_SUCCESS 可更新）", path=self.path)

        action = getattr(result, "control_action", None)
        if action not in VALID_ACTIONS:
            return UpdateResult(UPD_NOT_UPDATED, reason=R_NOT_ELIGIBLE,
                                detail=f"不可記錄的 action：{action!r}", path=self.path)

        rb = getattr(result, "readback_result", None)
        power = getattr(result, "target_power_kw", None)
        if action == ACT_STOP:
            power = None                              # stop 一律不帶功率
        elif not _finite(power) or power <= 0:
            return UpdateResult(UPD_NOT_UPDATED, reason=R_NOT_ELIGIBLE,
                                detail=f"{action} 的 target_power_kw 不合法：{power!r}",
                                path=self.path)

        obs = getattr(rb, "actual_active_power_kw", None)
        if obs is not None and not _finite(obs):
            obs = None
        rec = LastControlRecord(
            schema_version=SCHEMA_VERSION, action=action, target_power_kw=power,
            verified_at_wall=self._wall(), verified_at_monotonic=float(self._clock()),
            pcs_actual_state=getattr(rb, "observed_state", None) or "UNKNOWN",
            actual_active_power_kw=obs,
            reason=getattr(result, "reason", "") or R_OK,
            boot_id=self._boot_id(),
        )
        try:
            self._atomic_write(rec.as_dict())
        except (OSError, ValueError) as e:
            return UpdateResult(UPD_WRITE_FAILED, rec, reason=type(e).__name__,
                                detail=str(e), path=self.path)
        self._memo = rec
        return UpdateResult(UPD_UPDATED, rec, reason=R_OK, path=self.path)

    # ---- 讀取 ----
    def load(self):
        """從檔案載入。任何問題一律 fail closed 並回報明確 outcome，不 crash。"""
        if not os.path.exists(self.path):
            return LastControlLoadResult(LOAD_NOT_FOUND, reason=R_NO_RECORD,
                                         detail="檔案不存在", path=self.path)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            return LastControlLoadResult(LOAD_IO_ERROR, reason=type(e).__name__,
                                         detail=str(e), path=self.path)
        if not raw.strip():
            return LastControlLoadResult(LOAD_INVALID_JSON, reason="EMPTY_FILE",
                                         detail="檔案為空", path=self.path)
        try:
            data = json.loads(raw, parse_constant=_reject_constant)
        except _NonFiniteConstant as e:
            return LastControlLoadResult(LOAD_INVALID_FIELD, reason="NON_FINITE_CONSTANT",
                                         detail=f"JSON 含非有限常數：{e}", path=self.path)
        except ValueError as e:
            return LastControlLoadResult(LOAD_INVALID_JSON, reason="JSON_DECODE_ERROR",
                                         detail=str(e)[:120], path=self.path)
        outcome, rec, detail = validate_record_dict(data)
        if outcome != LOAD_OK:
            return LastControlLoadResult(outcome, reason=outcome, detail=detail, path=self.path)
        return LastControlLoadResult(LOAD_OK, rec, reason=R_OK, path=self.path)

    def current(self):
        """
        取得目前可用的 LastControl 與其信任層級。

        優先使用本 process 自己寫入的記憶體副本（同 process 必同 boot → 可信）；
        否則載入檔案，並依 boot_id 判定 monotonic 是否可比較。
        """
        if self._memo is not None:
            return LastControlTrustResult(TRUST_FOR_INTERVAL, self._memo, R_SAME_PROCESS,
                                          "本 process 寫入，monotonic 可比較", "memory")
        lr = self.load()
        if not lr.ok:
            return LastControlTrustResult(TRUST_INVALID, None, lr.outcome, lr.detail, "file")
        cur, rec_boot = self._boot_id(), lr.record.boot_id
        if cur is None or rec_boot is None:
            return LastControlTrustResult(
                TRUST_HISTORY_ONLY, lr.record, R_BOOT_ID_UNKNOWN,
                f"boot_id 現值={cur!r} 紀錄={rec_boot!r} → monotonic 不可比較", "file")
        if cur != rec_boot:
            return LastControlTrustResult(
                TRUST_HISTORY_ONLY, lr.record, R_BOOT_ID_MISMATCH,
                f"已重開機（{rec_boot} → {cur}）→ 舊 monotonic 不可信", "file")
        return LastControlTrustResult(TRUST_FOR_INTERVAL, lr.record, R_SAME_BOOT,
                                      f"同一 boot（{cur}）", "file")

    # ---- interval（僅算術判定；是否放行由 Phase 6.4 決定）----
    def check_interval(self, min_switch_interval_sec, now=None):
        """
        ⚠️ min_switch_interval_sec 為 None → CONFIG_NOT_READY。
           **不得** fallback 到 15 / 60 或 Decision Policy 的 min_hold_sec。
        ⚠️ 只用 monotonic 計算 elapsed；verified_at_wall 永不參與（wall clock 可能倒退）。
        """
        if min_switch_interval_sec is None:
            return IntervalCheckResult(IV_CONFIG_NOT_READY, R_INTERVAL_NOT_CONFIGURED,
                                       detail="min_switch_interval_sec 尚未取得正式數值")
        if not _finite(min_switch_interval_sec) or min_switch_interval_sec < 0:
            return IntervalCheckResult(IV_CONFIG_NOT_READY, R_INTERVAL_NOT_CONFIGURED,
                                       required_sec=None,
                                       detail=f"門檻不合法：{min_switch_interval_sec!r}")
        t = self.current()
        if not t.usable_for_interval:
            return IntervalCheckResult(IV_NO_TRUSTED_HISTORY, t.reason,
                                       required_sec=float(min_switch_interval_sec),
                                       trust=t.trust,
                                       last_action=(t.record.action if t.record else None),
                                       detail="無可信歷史 → 不得用於 interval 判斷")
        now = self._clock() if now is None else now
        elapsed = now - t.record.verified_at_monotonic
        out = (IV_INTERVAL_SATISFIED if elapsed >= min_switch_interval_sec
               else IV_WITHIN_INTERVAL)
        return IntervalCheckResult(out, R_OK, float(elapsed), float(min_switch_interval_sec),
                                   t.trust, t.record.action)


# ======================================================================
# CLI（離線；寫入暫存目錄，不碰正式 output）
# ======================================================================
class _FakeVerified:
    def __init__(self, action, power, eligible=True, outcome="VERIFY_SUCCESS",
                 observed="CHARGING", obs_power=-29.9):
        self.control_action = action
        self.target_power_kw = power
        self.lastcontrol_eligible = eligible
        self.outcome = outcome
        self.reason = "TARGET_STATE_REACHED"
        self.readback_result = type("RB", (), {"observed_state": observed,
                                               "actual_active_power_kw": obs_power})()


def main():
    p = argparse.ArgumentParser(description="Phase 6.5-E LastControl Store（離線）")
    p.add_argument("--demo", action="store_true", help="以暫存目錄演示")
    args = p.parse_args()

    print("== Phase 6.5-E LastControl Store ==")
    print(f"  schema_version   : {SCHEMA_VERSION}")
    print(f"  production 路徑  : {DEFAULT_STORE_PATH}")
    print(f"  可記錄 action    : {sorted(VALID_ACTIONS)}（不含 none）")
    print("  ⚠ 只有 read-back VERIFY_SUCCESS 才更新；API control_success 不算")
    print("  ⚠ min_switch_interval_sec 仍未設定 → interval 判斷一律 CONFIG_NOT_READY\n")

    if not args.demo:
        print("  （加上 --demo 以暫存目錄演示）")
        return 0

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "phase6_last_control.json")
        boot = {"id": "boot-A"}
        clk = {"t": 1000.0}
        st = LastControlStore(path, boot_id_provider=lambda: boot["id"],
                              clock=lambda: clk["t"],
                              wall_clock=lambda: "2026-08-20 12:00:00")

        print("  ── 更新資格 ──")
        print("   ", st.update_from_verified_result(
            _FakeVerified("charge", 30.0, eligible=False, outcome="COMMAND_ACCEPTED")))
        print("   ", st.update_from_verified_result(
            _FakeVerified("charge", 30.0, eligible=False, outcome="VERIFY_TIMEOUT")))
        u = st.update_from_verified_result(_FakeVerified("charge", 30.0))
        print("   ", u)
        print("     ", u.record)

        print("\n  ── 同 process ──")
        print("   ", st.current())
        print("   ", st.check_interval(None))
        clk["t"] = 1020.0
        print("   ", st.check_interval(30.0))
        clk["t"] = 1040.0
        print("   ", st.check_interval(30.0))

        print("\n  ── Service restart（同 boot，新 store 實例）──")
        st2 = LastControlStore(path, boot_id_provider=lambda: boot["id"],
                               clock=lambda: clk["t"])
        print("   ", st2.current())
        print("   ", st2.check_interval(30.0))

        print("\n  ── Windows reboot（boot 變更）──")
        boot["id"] = "boot-B"
        st3 = LastControlStore(path, boot_id_provider=lambda: boot["id"],
                               clock=lambda: clk["t"])
        print("   ", st3.current())
        print("   ", st3.check_interval(30.0))

        print("\n  ── boot_id 未知（production 尚未確認）──")
        st4 = LastControlStore(path, clock=lambda: clk["t"])
        print("   ", st4.current())
        print("   ", st4.check_interval(30.0))

        print("\n  ── 壞檔案 ──")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        print("   ", LastControlStore(path).load())
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"schema_version": 99, "action": "charge"}')
        print("   ", LastControlStore(path).load())
        os.remove(path)
        print("   ", LastControlStore(path).load())
    return 0


if __name__ == "__main__":
    sys.exit(main())
