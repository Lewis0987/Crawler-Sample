# -*- coding: utf-8 -*-
"""
phase6_trial2_harness.py — Trial #2 Discriminative Pause Validation Harness
======================================================================
定位
    **field-validation / trial tooling**，不是 production application layer。

    本模組只負責「Trial #2 這一次實機驗證要怎麼取證、怎麼判 ABORT、
    怎麼保存證據」。它：
      - 不修改、不包裝、不取代 Phase 6 frozen application layer
      - 不修改 decision engine / arbiter / production execution semantics
      - 不定義任何 production 參數
      - 不含任何 I/O：sample / probe / loop mark / sender 全部由呼叫端注入
      - 不含任何 SSH、HTTP、Modbus、subprocess

🔴 兩層分離（G2 POLICY，使用者 2026-09-17 裁示）
    LAYER 1  SAFETY PREFLIGHT    —— 只確認「現在動手是安全的」
             PCS 合法觀測狀態包含 CHARGING / DISCHARGING / STANDBY / STOPPED。
             **不得**因為 PCS 正在 CHARGING / DISCHARGING 就判 FAIL。
             UNKNOWN / FAULT / 非法狀態仍 Fail Closed。
    LAYER 2  ACTIVE BASELINE GATE —— 確認「external controller 確實 active」
             marks ADVANCING **AND**（PCS CHARGING ∨ PCS DISCHARGING
             ∨ Authority EXTERNAL_OR_UNKNOWN）。
             Meter 一律 CORROBORATING ONLY，**不得**進入判定。

🔴 deployed PAUSE 語意（2026-09-17 由 deployed source 確認）
        PAUSE SEMANTICS = ZERO-UNTIL-STOP-CONFIRMED -> THEN COMMAND SILENCE
    pause 過程可能重複送出 request_kw(i, 0) 直到實際 power 收斂為 0。
    因此 **pause SSH_OK 不是 pause complete**，
         **first zero write 也不是 pause complete**。

🔴 LOOP HALT 語意
        marks stopped  = NORMAL CONTROL LOOP HALTED
        marks stopped != STOP CONVERGENCE COMPLETE
        marks stopped != PAUSE PASS
    只有 `normal_loop_halted is True` **AND** `PauseVerifier == PV_VERIFIED`
    才構成 DISCRIMINATIVE PAUSE VERIFIED。

🔴 ONE ATTEMPT
    pause / restore 各自硬上限 1 次，由本模組的 `OneAttemptGuard` 強制。
    即使 SEND_UNKNOWN 也**禁止**自動 resend。
    計數器隨 evidence artifact 一起落盤，不再依賴任何 scratchpad 腳本。

用法
    python phase6_trial2_harness.py      # 離線自述；不做任何 I/O
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA                  # noqa: E402

TRIAL_NAME = "TRIAL_2_DISCRIMINATIVE_PAUSE_VALIDATION"

# ======================================================================
# 常數：語意標籤（寫進 evidence artifact，供日後閱讀者不必再推導）
# ======================================================================
PAUSE_TRANSITIONAL_CONTROL_EFFECT = "ZERO-UNTIL-STOP-CONFIRMED"
PAUSE_SEMANTICS = "ZERO-UNTIL-STOP-CONFIRMED -> THEN COMMAND SILENCE"
DEPLOYED_SOURCE_SHA256 = (
    "964bb642aa0e70a9663d636dc926150aaece60f13d28a745adbc53c41b3976ce")

# actual_active_power_kw 的鑑別力：Trial #1 曾在 external 充電約 80 kW 時
# 讀到 -1.40 kW。它**不能單獨形成 PASS**，只能作為 PauseVerifier 九項之一。
ACTUAL_ACTIVE_POWER_KW_DISCRIMINATIVE_VALUE = "LOW"

# Meter 的角色：只能佐證，不得單獨決定 external 是否 active。
METER_ROLE = "CORROBORATING_ONLY"

# ======================================================================
# Source-side freshness（evidence layer only）
# ======================================================================
# 🔴 為什麼需要這一層：
#    `EssSnapshotAdapter` 的 age_sec / stale 由 **讀取開始時刻** 推算，
#    衡量的是本機 HTTP 往返延遲 —— 它**不代表設備端資料有多新**。
#    2026-09-18 Trial #2 的 evidence closure 因此無法判定 SOC 來源新鮮度
#    （SOURCE_COHERENCE_UNRESOLVED）。本層改以設備自報的 raw_source_time
#    計算，**不得**再拿 HTTP call latency 冒充 device-side freshness。
#
# 🔴 本層**只影響 evidence artifact**：
#    不改 Phase 6 decision semantics / production arbiter /
#    production safety policy / ESS production control logic。
SOURCE_FRESH = "SOURCE_FRESH"
SOURCE_STALE = "SOURCE_STALE"
SOURCE_TIME_UNAVAILABLE = "SOURCE_TIME_UNAVAILABLE"
SOURCE_TIME_MALFORMED = "SOURCE_TIME_MALFORMED"
SOURCE_FRESHNESS_STATES = (SOURCE_FRESH, SOURCE_STALE,
                           SOURCE_TIME_UNAVAILABLE, SOURCE_TIME_MALFORMED)

# 設備 raw_source_time 可能的格式（只接受這些；不猜、不補零、不造時間）
SOURCE_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                       "%Y-%m-%dT%H:%M:%S")


def parse_source_time(raw):
    """設備自報時間字串 -> datetime。無法解析 -> None。**絕不造 timestamp。**"""
    if raw is None:
        return None
    if isinstance(raw, datetime.datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in SOURCE_TIME_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def source_freshness(raw_source_time, observed_at, max_age_sec):
    """
    回傳 (state, age_sec)。

        age_sec = local observation time - device raw_source_time

    🔴 **不是** HTTP call latency。
    🔴 取不到 -> SOURCE_TIME_UNAVAILABLE；解析失敗 -> SOURCE_TIME_MALFORMED。
       兩者 age 皆 None，且**不得**被當成 fresh（Fail Closed / Evidence Unknown）。
    """
    if max_age_sec is None or float(max_age_sec) <= 0:
        raise ValueError("max_age_sec 必須為正數（不得預設為無限）")
    if raw_source_time is None or str(raw_source_time).strip() == "":
        return SOURCE_TIME_UNAVAILABLE, None
    dt = parse_source_time(raw_source_time)
    if dt is None:
        return SOURCE_TIME_MALFORMED, None
    if observed_at is None:
        return SOURCE_TIME_UNAVAILABLE, None
    age = (observed_at - dt).total_seconds()
    if age <= float(max_age_sec):
        return SOURCE_FRESH, age
    return SOURCE_STALE, age


def source_evidence(sample, observed_at, max_age_sec):
    """單筆 sample 的來源新鮮度 evidence（四個欄位）。"""
    raw = (sample or {}).get("raw_source_time")
    state, age = source_freshness(raw, observed_at, max_age_sec)
    return {"raw_source_time": raw,
            "source_time_available": state != SOURCE_TIME_UNAVAILABLE,
            "source_age_sec": age,
            "source_freshness_state": state}


# ======================================================================
# LAYER 1 — SAFETY PREFLIGHT
# ======================================================================
# 🔴 Trial #1 的 `pcs_state in ("STANDBY", "STOPPED")` **不得**沿用：
#    Trial #2 的目的就是要先觀測到 external controller ACTIVE。
LEGAL_OBSERVED_PCS_STATES = ("CHARGING", "DISCHARGING", "STANDBY", "STOPPED")

SAFETY_PREFLIGHT_ITEMS = (
    "hmi_comm_ok", "meter_fresh", "source_fresh", "remote_ssh_ok",
    "screen_alive", "process_alive", "process_identity_ok",
    "guard_identity_ok", "pcs_state_legal", "fault_clear", "alarm_clear",
    "ownership_not_both", "dispatch_disabled", "mode_dry_run",
    "senders_not_armed",
)


def pcs_state_legal(pcs_state):
    """PCS 觀測狀態是否合法。UNKNOWN / FAULT / None / 未知字串一律 False。"""
    return pcs_state in LEGAL_OBSERVED_PCS_STATES


def safety_preflight(sample, probe, safety_flags):
    """
    LAYER 1。回傳 (ok, checks)。**任一未知一律 False（Fail Closed）。**

    sample  : dict —— comm_ok / fresh / meter_fresh / pcs_state / fault /
                       critical_alarms / ownership
    probe   : dict —— ssh_ok / screen_alive / process_alive /
                       process_identity_ok / guard_variant / capability_ok
    safety_flags : dict —— dispatch_enabled / mode / senders_armed

    🔴 **不檢查** PCS 是否 idle —— 那是 Trial #1 的閘，Trial #2 明文不得沿用。
    """
    sample = sample or {}
    probe = probe or {}
    flags = safety_flags or {}
    checks = {
        "hmi_comm_ok": sample.get("comm_ok") is True,
        "meter_fresh": sample.get("meter_fresh") is True,
        "source_fresh": sample.get("fresh") is True,
        "remote_ssh_ok": probe.get("ssh_ok") is True,
        "screen_alive": probe.get("screen_alive") is True,
        "process_alive": probe.get("process_alive") is True,
        "process_identity_ok": probe.get("process_identity_ok") is True,
        "guard_identity_ok": (probe.get("guard_variant") == "B2"
                              and probe.get("capability_ok") is True),
        "pcs_state_legal": pcs_state_legal(sample.get("pcs_state")),
        "fault_clear": sample.get("fault") is False,
        "alarm_clear": sample.get("critical_alarms") == 0,
        "ownership_not_both": sample.get("ownership") != "BOTH",
        "dispatch_disabled": flags.get("dispatch_enabled") is False,
        "mode_dry_run": flags.get("mode") == "DRY_RUN",
        "senders_not_armed": flags.get("senders_armed") is False,
    }
    return all(checks.values()), checks


# ======================================================================
# LAYER 2 — ACTIVE BASELINE GATE
# ======================================================================
AB_OBSERVING = "OBSERVING"
AB_ACTIVE_ESTABLISHED = "ACTIVE_BASELINE_ESTABLISHED"
AB_NO_ACTIVE = "NO_ACTIVE_BASELINE"

# 允許成立 ACTIVE 的單項證據（至少一項），全部只讀 PCS / Authority。
ACTIVE_EVIDENCE_ITEMS = ("pcs_charging", "pcs_discharging",
                         "authority_external")


class BaselineConfig(object):
    """
    Trial #2 baseline 觀測參數。

    🔴 **全部必填、不得預設** —— duration / interval 是 trial 參數，
       不是 production logic 的一部分，也不得寫死在任何 production 模組。
    """

    def __init__(self, observe_duration_sec, sample_interval_sec,
                 min_loop_observations):
        if observe_duration_sec is None or float(observe_duration_sec) <= 0:
            raise ValueError("observe_duration_sec 必須為正數（不得預設）")
        if sample_interval_sec is None or float(sample_interval_sec) <= 0:
            raise ValueError("sample_interval_sec 必須為正數（不得預設）")
        if min_loop_observations is None or int(min_loop_observations) < 2:
            raise ValueError("min_loop_observations 必須 >= 2（單點無法證明前進）")
        self.observe_duration_sec = float(observe_duration_sec)
        self.sample_interval_sec = float(sample_interval_sec)
        self.min_loop_observations = int(min_loop_observations)

    @property
    def max_samples(self):
        return int(self.observe_duration_sec // self.sample_interval_sec) + 1

    def as_dict(self):
        return {"observe_duration_sec": self.observe_duration_sec,
                "sample_interval_sec": self.sample_interval_sec,
                "min_loop_observations": self.min_loop_observations,
                "max_samples": self.max_samples}


# 🔴 BASELINE WINDOW POLICY（使用者 2026-09-18 正式裁示）
#    `observe_duration_sec` 的語意是 **MAX OBSERVATION WINDOW**，
#    **不是** MANDATORY DWELL TIME。Active Gate 在窗口內成立即可 EARLY SUCCESS，
#    立即保存 terminal active evidence 並進入 pause ——
#    Trial #2 要證明的是「pause 當下 external 確實 active」，
#    時間鄰接性比多等數分鐘更有價值。
#
#    ⚠️ 這是 FUTURE POLICY。它**不**回溯抹除 2026-09-18 Trial #2 的
#       EXECUTION PROCEDURE DEVIATION —— 當次授權文字要求完整 600 s window，
#       而 harness 依 early-success contract 於 365 s 結束。歷史照實保留。
BASELINE_WINDOW_POLICY = "MAX_OBSERVATION_WINDOW"
BASELINE_EARLY_SUCCESS_ALLOWED = True

# 建議值（≈10 min / 20 s）。**只是建議** —— 呼叫端仍必須明確傳入。
RECOMMENDED_BASELINE_DURATION_SEC = 600.0
RECOMMENDED_BASELINE_INTERVAL_SEC = 20.0


def active_evidence(sample):
    """
    單筆 active 證據。**只讀 PCS 與 Authority。**

    🔴 這裡**刻意不讀任何 meter 欄位** —— Meter 是 CORROBORATING ONLY，
       不得只因 meter kW 高/低就判定 external active。
    """
    sample = sample or {}
    return {
        "pcs_charging": sample.get("pcs_state") == "CHARGING",
        "pcs_discharging": sample.get("pcs_state") == "DISCHARGING",
        "authority_external": sample.get("authority") == "EXTERNAL_OR_UNKNOWN",
    }


class ActiveBaselineObserver(object):
    """
    ACTIVE BASELINE 建立。逐筆 feed(sample, loop_mark)，回傳當下狀態。

    成立條件（兩者**同時**）：
        loop marks ADVANCING
        AND 至少一筆 sample 具備 active evidence（PCS 充 / 放 或 Authority 外部）

    觀測窗結束仍未成立 -> AB_NO_ACTIVE -> 呼叫端必須 ABORT 且不得送 pause。
    """

    def __init__(self, config, clock=None):
        if config is None:
            raise ValueError("BaselineConfig 必填（不得預設）")
        self.cfg = config
        self._clock = clock or __import__("time").monotonic
        self.started_at = self._clock()
        self.state = AB_OBSERVING
        self.samples = []
        self.loop_marks = []
        self.active_hits = []
        self.meter_observations = []     # 只保存，不參與判定
        self.reason = None

    # ---- 唯讀衍生事實 ----
    @property
    def loop_marks_advancing(self):
        return RA.loop_active_from_marks(self.loop_marks,
                                         self.cfg.min_loop_observations)

    @property
    def active_evidence_seen(self):
        return len(self.active_hits) > 0

    @property
    def elapsed_sec(self):
        return self._clock() - self.started_at

    @property
    def established(self):
        return self.state == AB_ACTIVE_ESTABLISHED

    def feed(self, sample, loop_mark):
        if self.state in (AB_ACTIVE_ESTABLISHED, AB_NO_ACTIVE):
            return self.state
        ev = active_evidence(sample)
        hit = [k for k, v in ev.items() if v]
        self.samples.append({"evidence": ev, "hits": hit,
                             "pcs_state": (sample or {}).get("pcs_state"),
                             "authority": (sample or {}).get("authority"),
                             "loop_mark": loop_mark})
        # meter 只記錄，不參與判定（CORROBORATING ONLY）
        self.meter_observations.append((sample or {}).get("meter_kw"))
        if loop_mark is not None:
            self.loop_marks.append(loop_mark)
        if hit:
            self.active_hits.append(hit)

        if self.loop_marks_advancing is True and self.active_evidence_seen:
            self.state = AB_ACTIVE_ESTABLISHED
            self.reason = ("loop marks 前進且已觀測到 active evidence：%s"
                           % (sorted({k for h in self.active_hits for k in h}),))
            return self.state
        if self.elapsed_sec >= self.cfg.observe_duration_sec:
            self.state = AB_NO_ACTIVE
            self.reason = self._no_active_reason()
        return self.state

    def close(self):
        """觀測窗提前結束（樣本用盡）時呼叫。未成立即 AB_NO_ACTIVE。"""
        if self.state == AB_OBSERVING:
            self.state = AB_NO_ACTIVE
            self.reason = self._no_active_reason()
        return self.state

    def _no_active_reason(self):
        why = []
        adv = self.loop_marks_advancing
        if adv is None:
            why.append("loop marks 樣本不足（%d 筆，需 %d）"
                       % (len(self.loop_marks), self.cfg.min_loop_observations))
        elif adv is False:
            why.append("loop marks 未前進")
        if not self.active_evidence_seen:
            why.append("整個觀測窗未出現 PCS CHARGING / DISCHARGING "
                       "或 Authority EXTERNAL_OR_UNKNOWN")
        return "；".join(why) or "未知"

    @property
    def terminal_active_sample_index(self):
        """
        Active Gate 成立的那一筆在 **baseline sample list** 中的 1-based 索引。
        未成立 -> None。

        🔴 計數語意（2026-09-18 閉合）：
           `baseline_sample_count` 只數餵進 observer 的 baseline samples，
           **不含** Trial2Runner step 1 的 safety-preflight 取樣。
           外部執行腳本若把 preflight 那筆一起編號，其日誌序號會比本欄位大 1 ——
           那是**執行腳本的日誌編號**，不是 artifact 計數錯誤（非 off-by-one）。
        """
        for i, s in enumerate(self.samples, 1):
            if s.get("hits"):
                return i
        return None

    def as_dict(self):
        n = len(self.samples)
        return {"state": self.state, "reason": self.reason,
                "established": self.established,
                "loop_marks": list(self.loop_marks),
                "loop_marks_advancing": self.loop_marks_advancing,
                "active_evidence_seen": self.active_evidence_seen,
                "active_hits": [list(h) for h in self.active_hits],
                # 🔴 計數欄位語意明確化，避免與執行腳本日誌序號混淆
                "sample_count": n,                 # 保留（向後相容）
                "baseline_sample_count": n,
                "observing_sample_count": (n - 1 if self.established else n),
                "terminal_active_sample_index":
                    self.terminal_active_sample_index,
                "counts_exclude_preflight_sample": True,
                "window_policy": BASELINE_WINDOW_POLICY,
                "early_success_allowed": BASELINE_EARLY_SUCCESS_ALLOWED,
                "samples": list(self.samples),
                "meter_observations_corroborating_only":
                    list(self.meter_observations),
                "meter_role": METER_ROLE}


def active_gate(observer):
    """
    ACTIVE GATE 正式判定。回傳 (ok, reason)。

    🔴 未成立 -> 呼叫端必須 ABORT、**不得送 pause**、
       也**不需要** restore（pause 從未嘗試，責任未成立）。
    """
    if observer is None:
        return False, "未提供 ActiveBaselineObserver -> Fail Closed"
    if observer.state != AB_ACTIVE_ESTABLISHED:
        return False, (observer.reason or "ACTIVE BASELINE 未成立")
    return True, observer.reason


# ======================================================================
# G4 — pause 後的 NORMAL CONTROL LOOP HALT 觀測
# ======================================================================
LH_OBSERVING = "OBSERVING"
LH_HALTED = "NORMAL_CONTROL_LOOP_HALTED"
LH_STILL_ADVANCING = "STILL_ADVANCING"
LH_UNKNOWN = "LOOP_EVIDENCE_UNAVAILABLE"


class LoopHaltTracker(object):
    """
    pause 之後的 loop-mark 取樣。

    deployed source 已證實 timestamp print 位於
    `if not running: sleep(1); continue` **之後**的正常 demand loop 內，
    因此 marks 停止前進 = NORMAL CONTROL LOOP HALTED。

    🔴 但 **halted != stop convergence complete != pause PASS**。
       收斂期間 stop handler 仍可能在 read BESS / 送 request_kw(i, 0)，
       而此時正常 timestamp 已不再輸出。
    """

    def __init__(self, min_observations, timeout_sec, clock=None):
        if min_observations is None or int(min_observations) < 2:
            raise ValueError("min_observations 必須 >= 2")
        if timeout_sec is None or float(timeout_sec) <= 0:
            raise ValueError("timeout_sec 必須為正數（不得預設為無限）")
        self.min_observations = int(min_observations)
        self.timeout_sec = float(timeout_sec)
        self._clock = clock or __import__("time").monotonic
        self.started_at = self._clock()
        self.state = LH_OBSERVING
        self.marks = []
        self.reason = None

    @property
    def normal_loop_halted(self):
        """True / False / None(未知)。**None 不得視為 halted。**"""
        if self.state == LH_HALTED:
            return True
        if self.state == LH_STILL_ADVANCING:
            return False
        return None

    def feed(self, loop_mark):
        if self.state in (LH_HALTED, LH_STILL_ADVANCING, LH_UNKNOWN):
            return self.state
        if loop_mark is not None:
            self.marks.append(loop_mark)
        adv = RA.loop_active_from_marks(self.marks, self.min_observations)
        if adv is True:
            self.state = LH_STILL_ADVANCING
            self.reason = "pause 後 marks 仍在前進：%s" % (self.marks,)
            return self.state
        if adv is False:
            self.state = LH_HALTED
            self.reason = "pause 後 marks 未再前進：%s" % (self.marks,)
            return self.state
        if self._clock() - self.started_at > self.timeout_sec:
            self.state = LH_UNKNOWN
            self.reason = ("loop marks 僅取得 %d 筆（需 %d）-> 無法判定"
                           % (len(self.marks), self.min_observations))
        return self.state

    def as_dict(self):
        return {"state": self.state, "reason": self.reason,
                "marks": list(self.marks),
                "normal_loop_halted": self.normal_loop_halted}


# ======================================================================
# 鑑別結論
# ======================================================================
def discriminative_pause_verified(normal_loop_halted, pause_verifier_state):
    """
    🔴 **唯一**可以宣稱 external pause 已被鑑別驗證的函式。

        normal_loop_halted is True  AND  PauseVerifier == PV_VERIFIED

    任一為 None / False -> False。**不得只靠 loop marks。**
    """
    return (normal_loop_halted is True
            and pause_verifier_state == RA.PV_VERIFIED)


# ======================================================================
# G5 — version-controlled ONE ATTEMPT 硬計數器
# ======================================================================
class OneAttemptExceeded(RuntimeError):
    """控制動詞第二次嘗試 —— 結構上禁止。"""


class OneAttemptGuard(object):
    """
    控制動詞硬上限。**不再依賴任何 scratchpad 腳本。**

    🔴 `pause` / `restore` 各自 <= 1 次。
    🔴 即使上一次結果是 SEND_UNKNOWN 也**禁止**自動 resend ——
       不確定的責任由 restore responsibility 承擔，不是靠重送解決。
    """

    MAX_ATTEMPTS = 1

    def __init__(self, control_verbs=None):
        self.control_verbs = tuple(control_verbs
                                   if control_verbs is not None
                                   else RA.CONTROL_VERBS)
        self.counts = {}

    def attempts(self, verb):
        return self.counts.get(verb, 0)

    @property
    def pause_attempt_count(self):
        return self.attempts("pause")

    @property
    def restore_attempt_count(self):
        return self.attempts("restore")

    def may_attempt(self, verb):
        if verb not in self.control_verbs:
            return True
        return self.attempts(verb) < self.MAX_ATTEMPTS

    def take(self, verb):
        """取用一次額度。超過即拋 OneAttemptExceeded（不回 False，避免被忽略）。"""
        if not self.may_attempt(verb):
            raise OneAttemptExceeded(
                "ONE ATTEMPT 已用盡，禁止重送：%s（已嘗試 %d 次）"
                % (verb, self.attempts(verb)))
        self.counts[verb] = self.attempts(verb) + 1
        return self.counts[verb]

    def as_dict(self):
        return {"max_attempts": self.MAX_ATTEMPTS,
                "control_verbs": list(self.control_verbs),
                "counts": dict(self.counts),
                "pause_attempt_count": self.pause_attempt_count,
                "restore_attempt_count": self.restore_attempt_count}


# ======================================================================
# G7 — Evidence artifact
# ======================================================================
ARTIFACT_SCHEMA_VERSION = 2

ARTIFACT_FIELDS = (
    # --- 識別 ---
    "schema_version", "trial", "started_at", "finished_at",
    "deployed_source_sha256",
    # --- 語意標籤（供日後閱讀者不必再推導）---
    "pause_semantics", "pause_transitional_control_effect",
    "meter_role", "actual_active_power_kw_discriminative_value",
    "baseline_window_policy",
    # --- source-side freshness（schema v2 起必備）---
    "raw_source_time", "source_time_available", "source_age_sec",
    "source_freshness_state", "source_freshness_samples",
    # --- LAYER 1 ---
    "safety_preflight_ok", "safety_preflight_checks",
    # --- LAYER 2（G1 / G3）---
    "baseline_config", "active_baseline_established", "active_baseline",
    "active_gate_ok", "active_gate_reason",
    # --- pause（G5）---
    "pause_attempt_count", "pause_send_outcome",
    # --- G4 ---
    "normal_loop_halted", "loop_halt",
    # --- PauseVerifier ---
    "pause_verified", "pause_verifier_state", "pause_verifier_reason",
    "pause_verifier_samples",
    # --- 鑑別結論 ---
    "discriminative_pause_verified",
    # --- responsibility / restore ---
    "restore_responsibility", "restore_attempt_count", "restore_send_outcome",
    "restore_resumed", "restore_verifier_state", "restore_verifier_reason",
    "restore_verifier_marks",
    # --- 收尾 ---
    "post_check", "outcome", "abort_reason", "safety_reset", "steps",
)

# 使用者明文要求必須出現在 artifact 的欄位（回歸鎖）
REQUIRED_TRIAL2_FIELDS = (
    "pause_transitional_control_effect",
    "normal_loop_halted",
    "pause_verified",
    "restore_resumed",
    "active_baseline_established",
    "pause_attempt_count",
    "restore_attempt_count",
    "pause_send_outcome",
    "restore_send_outcome",
)

# schema v2 起額外必備（v1 歷史 artifact 不適用，以保持其可驗證性）
REQUIRED_SOURCE_FRESHNESS_FIELDS = (
    "raw_source_time", "source_time_available", "source_age_sec",
    "source_freshness_state",
)

OUT_ABORTED_SAFETY_PREFLIGHT = "ABORTED_SAFETY_PREFLIGHT"
OUT_ABORTED_NO_ACTIVE_BASELINE = "ABORTED_NO_ACTIVE_BASELINE"
OUT_ABORTED_PAUSE_FAILED = "ABORTED_PAUSE_FAILED"
OUT_PAUSE_OUTCOME_UNKNOWN = "PAUSE_OUTCOME_UNKNOWN"
OUT_RESTORE_FAILED_CRITICAL = "RESTORE_FAILED_CRITICAL"
OUT_COMPLETE = "COMPLETE"


class EvidenceArtifact(object):
    """Trial #2 證據。**所有欄位預設 None，未觀測就是 None，不得填假值。**"""

    def __init__(self, now_fn=None):
        self._now = now_fn or (lambda: datetime.datetime.now())
        self.data = dict((k, None) for k in ARTIFACT_FIELDS)
        self.data.update({
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "trial": TRIAL_NAME,
            "started_at": self._now().isoformat(timespec="seconds"),
            "deployed_source_sha256": DEPLOYED_SOURCE_SHA256,
            "pause_semantics": PAUSE_SEMANTICS,
            "pause_transitional_control_effect":
                PAUSE_TRANSITIONAL_CONTROL_EFFECT,
            "meter_role": METER_ROLE,
            "actual_active_power_kw_discriminative_value":
                ACTUAL_ACTIVE_POWER_KW_DISCRIMINATIVE_VALUE,
            "baseline_window_policy": BASELINE_WINDOW_POLICY,
            # 未觀測就是 UNAVAILABLE，**不得**填假值
            "source_freshness_state": SOURCE_TIME_UNAVAILABLE,
            "source_time_available": False,
            "source_freshness_samples": [],
            "pause_attempt_count": 0,
            "restore_attempt_count": 0,
            "restore_responsibility": False,
            "discriminative_pause_verified": False,
            "steps": [],
        })

    def set(self, **kw):
        for k, v in kw.items():
            if k not in ARTIFACT_FIELDS:
                raise KeyError("未登記於 ARTIFACT_FIELDS 的欄位：%s" % k)
            self.data[k] = v
        return self

    def step(self, name, ok, detail=None):
        self.data["steps"].append(
            {"step": name, "ok": bool(ok), "detail": detail,
             "at": self._now().isoformat(timespec="seconds")})
        return ok

    def finish(self, outcome, abort_reason=None):
        self.data["outcome"] = outcome
        self.data["abort_reason"] = abort_reason
        self.data["finished_at"] = self._now().isoformat(timespec="seconds")
        self.data["discriminative_pause_verified"] = (
            discriminative_pause_verified(self.data.get("normal_loop_halted"),
                                          self.data.get("pause_verifier_state")))
        return self

    def to_dict(self):
        return dict(self.data)

    def missing_required(self):
        return [k for k in REQUIRED_TRIAL2_FIELDS if k not in self.data]

    def write(self, path):
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2,
                      default=str)
        return path


def validate_artifact(record):
    """回傳 problems 清單（空 = 合格）。供離線回歸鎖住 schema。"""
    p = []
    if not isinstance(record, dict):
        return ["artifact 必須是 dict"]
    ver = record.get("schema_version")
    if ver not in (1, ARTIFACT_SCHEMA_VERSION):
        p.append("schema_version 不支援：%r" % (ver,))
    for k in REQUIRED_TRIAL2_FIELDS:
        if k not in record:
            p.append("缺少必要欄位：%s" % k)
    # 🔴 v1 是 2026-09-18 Trial #2 的歷史 artifact —— 保持其可驗證性，
    #    不得因為新增 schema 就讓既有 field evidence 變成「不合格」。
    if ver == ARTIFACT_SCHEMA_VERSION:
        for k in REQUIRED_SOURCE_FRESHNESS_FIELDS:
            if k not in record:
                p.append("缺少 source freshness 欄位：%s" % k)
        st = record.get("source_freshness_state")
        if st is not None and st not in SOURCE_FRESHNESS_STATES:
            p.append("source_freshness_state 非法：%r" % (st,))
        if (st in (SOURCE_TIME_UNAVAILABLE, SOURCE_TIME_MALFORMED)
                and record.get("source_age_sec") is not None):
            p.append("%s 不得帶 source_age_sec（不造 timestamp）" % st)
    if record.get("pause_transitional_control_effect") \
            != PAUSE_TRANSITIONAL_CONTROL_EFFECT:
        p.append("pause_transitional_control_effect 必須為 %s"
                 % PAUSE_TRANSITIONAL_CONTROL_EFFECT)
    for k in ("pause_attempt_count", "restore_attempt_count"):
        v = record.get(k)
        if not isinstance(v, int) or v < 0 or v > OneAttemptGuard.MAX_ATTEMPTS:
            p.append("%s 必須是 0..%d 的整數" % (k, OneAttemptGuard.MAX_ATTEMPTS))
    want = discriminative_pause_verified(record.get("normal_loop_halted"),
                                         record.get("pause_verifier_state"))
    if record.get("discriminative_pause_verified") != want:
        p.append("discriminative_pause_verified 與 "
                 "(normal_loop_halted, pause_verifier_state) 不一致")
    return p


# ======================================================================
# Trial #2 Runner —— 12 步驟
# ======================================================================
class Trial2Runner(object):
    """
    Trial #2 執行器。**全部相依由注入取得，本類別不含任何 I/O。**

    sample_source()      -> dict（每次都必須 fresh）
    probe_source()       -> dict
    loop_mark_source()   -> str|None
    safety_flags_source()-> dict
    pause_sender()       -> RA.SEND_SENT / SEND_UNKNOWN / SEND_NOT_SENT
    restore_sender()     -> 同上
    safety_reset()       -> dict|None（收尾：解除 arm、timeout 歸 None…）

    🔴 sender 未注入 -> 結構上不可能送出任何東西（離線測試安全）。
    🔴 pause / restore 一律經過 OneAttemptGuard；第二次呼叫會拋例外。
    """

    def __init__(self, sample_source, probe_source, loop_mark_source,
                 safety_flags_source, baseline_config,
                 pause_verifier_factory, restore_verifier_factory,
                 loop_halt_min_observations, loop_halt_timeout_sec,
                 stable_verify_interval_sec, restore_loop_interval_sec,
                 pause_sender=None, restore_sender=None, safety_reset=None,
                 sleeper=None, clock=None, now_fn=None, guard=None,
                 max_pause_verify_samples=200):
        self.sample_source = sample_source
        self.probe_source = probe_source
        self.loop_mark_source = loop_mark_source
        self.safety_flags_source = safety_flags_source
        self.cfg = baseline_config
        self._pv_factory = pause_verifier_factory
        self._rv_factory = restore_verifier_factory
        self.loop_halt_min_observations = loop_halt_min_observations
        self.loop_halt_timeout_sec = loop_halt_timeout_sec
        self.stable_verify_interval_sec = stable_verify_interval_sec
        self.restore_loop_interval_sec = restore_loop_interval_sec
        self._pause_sender = pause_sender
        self._restore_sender = restore_sender
        self._safety_reset = safety_reset
        self._sleep = sleeper or (lambda s: None)
        self._clock = clock or __import__("time").monotonic
        self.guard = guard if guard is not None else OneAttemptGuard()
        self.max_pause_verify_samples = int(max_pause_verify_samples)
        self.art = EvidenceArtifact(now_fn=now_fn)
        self.restore_responsibility = False

    # ---- 送出（唯一出口，一律過 guard）----
    def _send(self, verb, sender):
        self.guard.take(verb)                     # 超額即拋，結構上不可能重送
        self.art.set(**{"%s_attempt_count" % verb: self.guard.attempts(verb)})
        if sender is None:
            return RA.SEND_NOT_SENT
        return sender()

    # ---- 主流程 ----
    def run(self):
        art = self.art

        # 1 SAFETY PREFLIGHT
        smp, prb = self.sample_source(), self.probe_source()
        ok, checks = safety_preflight(smp, prb, self.safety_flags_source())
        art.set(safety_preflight_ok=ok, safety_preflight_checks=checks)
        art.step("1_SAFETY_PREFLIGHT", ok,
                 None if ok else [k for k, v in checks.items() if not v])
        if not ok:
            return self._finish(OUT_ABORTED_SAFETY_PREFLIGHT,
                                "safety preflight 未通過：%s"
                                % [k for k, v in checks.items() if not v])

        # 2 ACTIVE BASELINE OBSERVE
        obs = ActiveBaselineObserver(self.cfg, clock=self._clock)
        art.set(baseline_config=self.cfg.as_dict())
        for i in range(self.cfg.max_samples):
            st = obs.feed(self.sample_source(), self.loop_mark_source())
            if st != AB_OBSERVING:
                break
            if i < self.cfg.max_samples - 1:
                self._sleep(self.cfg.sample_interval_sec)
        obs.close()
        art.set(active_baseline=obs.as_dict(),
                active_baseline_established=obs.established)
        art.step("2_ACTIVE_BASELINE_OBSERVE", obs.established, obs.reason)

        # 3 ACTIVE GATE
        gate_ok, gate_why = active_gate(obs)
        art.set(active_gate_ok=gate_ok, active_gate_reason=gate_why)
        art.step("3_ACTIVE_GATE", gate_ok, gate_why)
        if not gate_ok:
            # 🔴 ABORT / NO PAUSE SEND / NO RESTORE NEEDED
            return self._finish(OUT_ABORTED_NO_ACTIVE_BASELINE, gate_why)

        # 4 PAUSE（ONE ATTEMPT）
        outcome = self._send("pause", self._pause_sender)
        art.set(pause_send_outcome=outcome)
        art.step("4_PAUSE_ONE_ATTEMPT", outcome == RA.SEND_SENT, outcome)
        if outcome == RA.SEND_NOT_SENT:
            # 責任未成立 -> 不需 restore
            return self._finish(OUT_ABORTED_PAUSE_FAILED,
                                "pause 明確未送出")
        # SEND_SENT 與 SEND_UNKNOWN 皆成立責任，且**禁止重送**
        self.restore_responsibility = True
        art.set(restore_responsibility=True)

        # 5 LOOP HALT OBSERVATION（G4）
        lh = LoopHaltTracker(self.loop_halt_min_observations,
                             self.loop_halt_timeout_sec, clock=self._clock)
        for _ in range(self.loop_halt_min_observations + 3):
            if lh.feed(self.loop_mark_source()) != LH_OBSERVING:
                break
            self._sleep(self.restore_loop_interval_sec)
        art.set(loop_halt=lh.as_dict(),
                normal_loop_halted=lh.normal_loop_halted)
        art.step("5_LOOP_HALT_OBSERVATION",
                 lh.normal_loop_halted is True, lh.reason)

        # 6 PAUSE VERIFIER（沿用既有 criteria，不修改）
        pv = self._pv_factory()
        samples = []
        for _ in range(self.max_pause_verify_samples):
            s, p = self.sample_source(), self.probe_source()
            st = pv.feed(s, p)
            samples.append({"state": st, "sample": s,
                            "checks": dict(pv.last_checks or {})})
            if st not in (RA.PV_SETTLING, RA.PV_STABLE_VERIFY):
                break
            self._sleep(self.stable_verify_interval_sec)
        art.set(pause_verifier_state=pv.state, pause_verifier_reason=pv.reason,
                pause_verifier_samples=samples,
                pause_verified=(pv.state == RA.PV_VERIFIED))
        art.step("6_PAUSE_VERIFIER", pv.state == RA.PV_VERIFIED, pv.state)

        # 7 RESPONSIBILITY STATE
        art.step("7_RESPONSIBILITY", self.restore_responsibility,
                 "restore_responsibility=%r" % self.restore_responsibility)

        # 8 RESTORE（ONE ATTEMPT）—— 責任成立即必須歸還
        r_outcome = self._send("restore", self._restore_sender)
        art.set(restore_send_outcome=r_outcome)
        art.step("8_RESTORE_ONE_ATTEMPT", r_outcome == RA.SEND_SENT, r_outcome)
        if r_outcome == RA.SEND_NOT_SENT:
            return self._finish(OUT_RESTORE_FAILED_CRITICAL,
                                "restore 明確未送出，責任保留")

        # 9 RESTORE VERIFIER
        rv = self._rv_factory()
        marks = []
        st = None
        for _ in range(self.cfg.min_loop_observations + 3):
            prb2 = self.probe_source()
            p = prb2 if isinstance(prb2, RA.RemoteProbe) else RA.RemoteProbe(
                ssh_ok=prb2.get("ssh_ok"),
                screen_alive=prb2.get("screen_alive"),
                process_alive=prb2.get("process_alive"),
                process_identity_ok=prb2.get("process_identity_ok"))
            m = self.loop_mark_source()
            marks.append(m)
            st = rv.feed(p, m)
            if st in (RA.RV_RESUMED, RA.RV_FAILED, RA.RV_UNKNOWN):
                break
            self._sleep(self.restore_loop_interval_sec)
        art.set(restore_verifier_state=rv.state, restore_verifier_reason=rv.reason,
                restore_verifier_marks=marks,
                restore_resumed=(rv.state == RA.RV_RESUMED))
        art.step("9_RESTORE_VERIFIER", rv.state == RA.RV_RESUMED, rv.state)
        if rv.state != RA.RV_RESUMED:
            return self._finish(OUT_RESTORE_FAILED_CRITICAL,
                                "restore 未達 RV_RESUMED，責任保留：%s" % rv.state)
        # 🔴 只有 RV_RESUMED 才可清除責任
        self.restore_responsibility = False
        art.set(restore_responsibility=False)

        # 10 POST CHECK
        smp3, prb3 = self.sample_source(), self.probe_source()
        post = {
            "ownership_not_both": smp3.get("ownership") != "BOTH",
            "fault_clear": smp3.get("fault") is False,
            "alarm_clear": smp3.get("critical_alarms") == 0,
            "runtime_healthy": (prb3.get("screen_alive") is True
                                and prb3.get("process_alive") is True
                                and prb3.get("process_identity_ok") is True),
            "responsibility_cleared": self.restore_responsibility is False,
        }
        art.set(post_check=post)
        art.step("10_POST_CHECK", all(post.values()),
                 [k for k, v in post.items() if not v])

        # 11 + 12（artifact / safety reset）在 _finish 完成
        outcome_final = (OUT_COMPLETE if all(post.values())
                         else OUT_RESTORE_FAILED_CRITICAL)
        return self._finish(outcome_final,
                            None if all(post.values()) else "post-check 未通過")

    def _finish(self, outcome, abort_reason=None):
        # 12 FINAL SAFETY RESET —— 無論成敗都要做
        reset = None
        if self._safety_reset is not None:
            reset = self._safety_reset()
        self.art.set(safety_reset=reset,
                     restore_responsibility=self.restore_responsibility)
        self.art.finish(outcome, abort_reason)
        return self.art


# ======================================================================
def main(argv=None):
    """離線自述。**不做任何 I/O，不接觸設備。**"""
    print("=" * 72)
    print("  Phase 6.10 Trial #2 — Discriminative Pause Validation Harness")
    print("=" * 72)
    print("  定位              : field-validation / trial tooling（非 production）")
    print("  PAUSE SEMANTICS   : %s" % PAUSE_SEMANTICS)
    print("  TRANSITIONAL      : PAUSE TRANSITIONAL CONTROL EFFECT = %s"
          % PAUSE_TRANSITIONAL_CONTROL_EFFECT)
    print("  deployed sha256   : %s" % DEPLOYED_SOURCE_SHA256)
    print()
    print("  LAYER 1 SAFETY PREFLIGHT（%d 項）" % len(SAFETY_PREFLIGHT_ITEMS))
    print("    合法 PCS 觀測狀態 : %s" % (LEGAL_OBSERVED_PCS_STATES,))
    print("    [!] 不得因 CHARGING / DISCHARGING 就判 FAIL")
    print()
    print("  LAYER 2 ACTIVE BASELINE GATE")
    print("    marks ADVANCING AND 至少一項：%s" % (ACTIVE_EVIDENCE_ITEMS,))
    print("    Meter             : %s" % METER_ROLE)
    print("    建議觀測          : %.0f s / 每 %.0f s（呼叫端仍須明確傳入）"
          % (RECOMMENDED_BASELINE_DURATION_SEC,
             RECOMMENDED_BASELINE_INTERVAL_SEC))
    print()
    print("  ONE ATTEMPT       : %s 各上限 %d 次（SEND_UNKNOWN 亦不得 resend）"
          % (list(RA.CONTROL_VERBS), OneAttemptGuard.MAX_ATTEMPTS))
    print("  DISCRIMINATIVE    : normal_loop_halted AND PauseVerifier=%s"
          % RA.PV_VERIFIED)
    print("  artifact schema   : v%d，%d 欄位（必備 %d 項）"
          % (ARTIFACT_SCHEMA_VERSION, len(ARTIFACT_FIELDS),
             len(REQUIRED_TRIAL2_FIELDS)))
    print()
    print("  [!] 本模組不執行 Trial #2，也不含任何 SSH / HTTP / subprocess。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
