# -*- coding: utf-8 -*-
"""
phase6_remote_adapter.py — Phase 6.10-A Remote Adapter / Probe Wiring
======================================================================
把 `phase6_handoff_orchestrator` 的三個注入點接到真實的 external controller：

    probe            唯讀取得遠端事實
    pause_sender     送出 built-in "stop"
    restore_sender   送出 built-in "start"

🔴 **本模組不自行決定要不要動**
    三個 sender 都必須由呼叫端明確開啟（`armed=True`）才會真的送。
    預設 `armed=False` → sender 回 False，operator 呼叫數恆為 0。

🔴 **probe 一律唯讀**
    只執行 `screen -ls` / `ps` / `readlink /proc` / `screen -X hardcopy`。
    hardcopy 只寫入 /tmp 的診斷檔，不碰 ~/ems、不改任何設定。

🔴 **UNKNOWN 優先於猜測**
    任一必要事實取不到 → 該欄位為 None，且 `ok` 為 False。
    絕不以「沒讀到就當作沒問題」的方式推進。

已於 2026-09-01 實機驗證（READ-ONLY）
    · `screen -S auto -X stuff $'status\\n'` 在 `restrict`（no-pty）SSH 下可用
    · status branch 經原始碼確認只含 print()，且指令迴圈**沒有 else 分支**
      —— 無法匹配的輸入會被完全忽略，因此殘缺注入不會觸發任何動作
    · 注入後 screen / PID / 行程鏈 / PCS / 功率 / SOC 全部未變
"""
import re
import time
import shlex
import datetime

# ======================================================================
# 遠端常數（來自實機確認，不是猜測）
# ======================================================================
SCREEN_NAME = "auto"                 # screen 1128.auto
EXPECTED_CMD = "python3 auto_control.py"
EXPECTED_CWD = "/home/etica/ems"

CMD_PAUSE = "stop"
CMD_RESTORE = "start"
CMD_STATUS = "status"                # 唯讀，僅 print()

# controller 每秒輸出一行帶時間戳的量測 —— 這是「控制迴圈仍在活動」的證據來源。
LOOP_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


# ======================================================================
# 結構化 probe 結果
# ======================================================================
class RemoteProbe(object):
    """
    遠端唯讀事實。**任一必要欄位為 None 即代表 UNKNOWN，不得當成通過。**
    """

    REQUIRED = ("ssh_ok", "screen_alive", "process_alive", "process_identity_ok")

    def __init__(self, ssh_ok=None, screen_alive=None, process_alive=None,
                 process_identity_ok=None, loop_active=None, pid=None,
                 cwd=None, cmdline=None, observed_at=None, detail=""):
        self.ssh_ok = ssh_ok
        self.screen_alive = screen_alive
        self.process_alive = process_alive
        self.process_identity_ok = process_identity_ok
        self.loop_active = loop_active
        self.pid = pid
        self.cwd = cwd
        self.cmdline = cmdline
        self.observed_at = observed_at or datetime.datetime.now()
        self.detail = detail

    @property
    def known(self):
        """必要事實是否全部取得（不論真假）。任一為 None → UNKNOWN。"""
        return all(getattr(self, k) is not None for k in self.REQUIRED)

    @property
    def healthy(self):
        """遠端 runtime 是否健康（存活且身分正確）。UNKNOWN 一律 False。"""
        return bool(self.known and self.ssh_ok and self.screen_alive
                    and self.process_alive and self.process_identity_ok)

    def as_dict(self):
        d = {k: getattr(self, k) for k in
             ("ssh_ok", "screen_alive", "process_alive", "process_identity_ok",
              "loop_active", "pid", "cwd", "cmdline", "detail")}
        d["observed_at"] = self.observed_at.strftime("%Y-%m-%d %H:%M:%S")
        d["known"] = self.known
        d["healthy"] = self.healthy
        return d

    def __str__(self):
        return (f"[REMOTE {'HEALTHY' if self.healthy else 'UNKNOWN/UNHEALTHY'}] "
                f"pid={self.pid} loop_active={self.loop_active}")


# ======================================================================
# 唯讀 probe
# ======================================================================
PROBE_SCRIPT = (
    "screen -ls | grep -c '\\.{name}\\s' || true; "
    "pgrep -af '{cmd}' | head -1 || true; "
).format(name=SCREEN_NAME, cmd="auto_control.py")


def parse_probe(screen_count_line, pgrep_line, ps_line=None,
                cwd_line=None, now=None):
    """
    把遠端唯讀輸出解析成 RemoteProbe。**純函式、零 I/O，可離線測試。**

    任一輸入為 None → 對應欄位保持 None（UNKNOWN），不臆測。
    """
    p = RemoteProbe(observed_at=now)
    p.ssh_ok = True
    if screen_count_line is not None:
        try:
            p.screen_alive = int(str(screen_count_line).strip()) > 0
        except ValueError:
            p.screen_alive = None
    if pgrep_line is not None:
        line = str(pgrep_line).strip()
        if line:
            parts = line.split(None, 1)
            try:
                p.pid = int(parts[0])
            except (ValueError, IndexError):
                p.pid = None
            p.cmdline = parts[1] if len(parts) > 1 else ""
            p.process_alive = p.pid is not None
            p.process_identity_ok = (p.cmdline == EXPECTED_CMD)
        else:
            p.process_alive = False
            p.process_identity_ok = False
    if cwd_line is not None:
        p.cwd = str(cwd_line).strip() or None
        if p.cwd and p.process_identity_ok:
            p.process_identity_ok = (p.cwd == EXPECTED_CWD)
    return p


def loop_active_from_hardcopies(first_text, second_text):
    """
    由兩次 hardcopy 的最新時間戳是否前進，判斷控制迴圈是否仍在活動。

    🔴 這是 **RESTORE_VERIFICATION 的 B 層證據**：
       controller 每秒輸出一行帶時間戳的量測，時間戳前進 = loop 仍在跑。
       它**不要求** PCS 一定出力 —— 恢復時 SOC / TOU / demand 可能正確地
       決定不動作，那不代表沒恢復。

    回傳 True / False / None（無法判定 → UNKNOWN，不猜）。
    """
    if first_text is None or second_text is None:
        return None
    a = LOOP_TS_RE.findall(first_text)
    b = LOOP_TS_RE.findall(second_text)
    if not a or not b:
        return None
    return b[-1] > a[-1]


# ======================================================================
# Sender（預設不武裝）
# ======================================================================
# controller 的內建動詞字串 → guard allowlist 的動詞名稱。
# 兩者刻意不同：`stop` / `start` 是 auto_control.py 的 REPL 指令，
# `pause` / `restore` 是 guard 對外暴露的動詞。不可混用。
_GUARD_VERB_FOR = {CMD_PAUSE: "pause", CMD_RESTORE: "restore",
                   CMD_STATUS: "status"}


def build_stuff_command(text, screen_name=SCREEN_NAME):
    """
    組出非互動注入指令。**不 attach**，`screen -X` 不需要 PTY。

    ⚠️ 換行必須是**真正的換行字元**。若只送出文字而沒有換行，
       該行不會被提交，會殘留在輸入緩衝區 —— 之後的指令會被前綴污染
       （例如變成 "statusstop"）。由於 controller 的指令迴圈沒有 else 分支，
       被污染的指令只會被忽略、不會誤觸發，但**暫停會靜默失敗**。
       因此送出後一律以行為驗證，不以「已送出」為準。
    """
    return "screen -S {n} -X stuff $'{t}\\n'".format(
        n=shlex.quote(screen_name).strip("'"), t=text)


class RemoteSenders(object):
    """
    pause / restore 的實際送出器。

    🔴 `armed=False`（預設）時**完全不呼叫** runner —— 回 False。
       DRY_RUN / OBSERVE 的 operator 呼叫數因此結構上恆為 0。
    🔴 `runner` 由呼叫端注入（實務上是 SSH 執行函式）；未注入即不可能送出。
    """

    def __init__(self, runner=None, armed=False, transport=None):
        self._runner = runner
        self.armed = bool(armed)
        self._transport = transport
        self.calls = []
        # 最近一次控制動詞的結果與三值判定（供 orchestrator 取用）
        self.last_result = None
        self.last_send_outcome = None

    def _send(self, verb):
        """
        回傳 bool —— **只有確定送達並被接受才 True**。

        🔴 `SSH_COMMAND_TIMEOUT` 回 False，但 `last_send_outcome` 會是
           `SEND_UNKNOWN`。呼叫端**不得**只看這個 bool 就斷定「沒送出」，
           必須改讀 `last_send_outcome`（見 orchestrator.request_pause）。
        """
        self.last_result = None
        self.last_send_outcome = None
        if not self.armed:
            self.last_send_outcome = SEND_NOT_SENT
            return False

        if self._transport is not None:
            guard_verb = _GUARD_VERB_FOR.get(verb, verb)
            self.calls.append(verb)
            res = self._transport.run(guard_verb)
            self.last_result = res
            self.last_send_outcome = classify_send_outcome(res)
            return self.last_send_outcome == SEND_SENT

        if self._runner is None:
            self.last_send_outcome = SEND_NOT_SENT
            return False
        self.calls.append(verb)
        ok = bool(self._runner(build_stuff_command(verb)))
        self.last_send_outcome = SEND_SENT if ok else SEND_NOT_SENT
        return ok

    def pause(self):
        return self._send(CMD_PAUSE)

    def restore(self):
        return self._send(CMD_RESTORE)

    def pause_ex(self):
        """回傳 (send_outcome, SshResult|None)。"""
        self._send(CMD_PAUSE)
        return self.last_send_outcome, self.last_result

    def restore_ex(self):
        self._send(CMD_RESTORE)
        return self.last_send_outcome, self.last_result

    def status(self):
        """唯讀：只觸發 controller 的 status branch（僅 print）。"""
        if self._runner is None:
            return False
        self.calls.append(CMD_STATUS)
        return bool(self._runner(build_stuff_command(CMD_STATUS)))


# ======================================================================
# external_quiescent —— 多樣本行為判定
# ======================================================================
# 🔴 我們**無法**直接讀 auto_control.py 內的 `running` 變數。
#    因此不使用「running == False」這種宣稱，改以外部可觀測事實證明
#    external controller 已經不再對 PCS 施加控制。
QUIESCENT_ITEMS = ("power_converged", "pcs_legal_idle", "no_external_authority",
                   "comm_ok", "fault_clear", "alarm_clear", "state_stable")


def external_quiescent(samples, idle_baseline_kw, tolerance_kw,
                       min_samples=None):
    """
    多樣本靜止判定。回傳 (ok, checks, detail)。

    🔴 **不是單點快照**：單一瞬間看起來像 idle，可能只是兩次 setpoint
       之間的空檔。必須連續多筆都成立。
    🔴 `min_samples` 必須由呼叫端提供，不預設 —— 觀測長度是操作決策，
       不該由本模組替使用者決定。
    """
    from phase6_handoff_orchestrator import power_is_idle
    if min_samples is None:
        return False, {}, "min_samples 未提供 → Fail Closed"
    if not samples or len(samples) < int(min_samples):
        return False, {}, f"樣本數 {len(samples or [])} < 要求 {min_samples}"

    checks = {k: True for k in QUIESCENT_ITEMS}
    for s in samples:
        if not power_is_idle(s.get("ac_kw"), idle_baseline_kw, tolerance_kw):
            checks["power_converged"] = False
        if s.get("pcs_state") not in ("STANDBY", "STOPPED"):
            checks["pcs_legal_idle"] = False
        if s.get("authority") == "EXTERNAL_OR_UNKNOWN":
            checks["no_external_authority"] = False
        if s.get("comm_ok") is not True:
            checks["comm_ok"] = False
        if s.get("fault") is not False:
            checks["fault_clear"] = False
        if s.get("critical_alarms") not in (0,):
            checks["alarm_clear"] = False
    # 觀測窗口內 PCS 狀態不得變動（變動 = 仍有人在下指令）
    states = {s.get("pcs_state") for s in samples}
    checks["state_stable"] = len(states) == 1
    ok = all(checks.values())
    return ok, checks, ("行為驗證通過" if ok else "存在未滿足項目")


# ======================================================================
# Phase 6.10-A1：Idle power band（production interface）
# ======================================================================
# 🔴 production 呼叫端一律使用**已驗證的 idle band**，不是「單點 baseline ± 容差」。
#    理由：baseline=-1.3 / tolerance=1.25 會讓 0.0 kW 落在帶外 —— 那不是理想的
#    unattended policy。真正該定義的是「正常 idle 變動範圍」，而那必須由現場
#    多次觀測驗證後注入，**不得由任何單次讀值推導**。
def power_in_idle_band(ac_kw, lower_kw, upper_kw):
    """lower <= ac <= upper。任一為 None → False（Fail Closed）。"""
    if ac_kw is None or lower_kw is None or upper_kw is None:
        return False
    lo, hi = float(lower_kw), float(upper_kw)
    if lo > hi:
        return False
    return lo <= float(ac_kw) <= hi


def band_from_baseline(baseline_kw, tolerance_kw):
    """
    底層計算工具，**不是 production policy**。

    僅供需要由既有 baseline/tolerance 換算成 band 的場合；
    production caller 仍須自行證明該 band 涵蓋正常 idle variation。
    """
    if baseline_kw is None or tolerance_kw is None:
        return None, None
    b, t = float(baseline_kw), float(tolerance_kw)
    return b - t, b + t


# ======================================================================
# Phase 6.10-A1：Pause Verification 兩階段
# ======================================================================
# SETTLING       送出 pause 後的收斂期。**允許**看到
#                CHARGING/DISCHARGING -> STANDBY/STOPPED、
#                EXTERNAL_OR_UNKNOWN -> IDLE、active power -> idle band。
#                這是正常收斂過程，不得判為 pause 失敗。
# STABLE_VERIFY  首次出現完整 candidate-idle 之後才開始；
#                要求連續 N 筆 fresh sample 全部成立。
#
# 🔴 external_quiescent 只有在 STABLE_VERIFY 完成後才為 True。
#    它**不代表** auto_control.py 的 running == False —— 那是行程內部變數，
#    外部讀不到。它只代表「external controller 行為上已靜止」。
PV_SETTLING = "SETTLING"
PV_STABLE_VERIFY = "STABLE_VERIFY"
PV_VERIFIED = "VERIFIED"
PV_FAILED = "FAILED"
PV_SETTLING_TIMEOUT = "SETTLING_TIMEOUT"
PV_UNKNOWN = "UNKNOWN_SOURCE"

CANDIDATE_IDLE_ITEMS = ("screen_alive", "process_alive", "process_identity_ok",
                        "pcs_legal_idle", "power_in_idle_band",
                        "authority_idle", "comm_ok", "fault_clear",
                        "alarm_clear")


def candidate_idle(sample, probe, idle_lower_kw, idle_upper_kw):
    """單筆是否構成 candidate-idle。回傳 (ok, checks)。"""
    checks = {
        "screen_alive": probe.get("screen_alive") is True,
        "process_alive": probe.get("process_alive") is True,
        "process_identity_ok": probe.get("process_identity_ok") is True,
        "pcs_legal_idle": sample.get("pcs_state") in ("STANDBY", "STOPPED"),
        "power_in_idle_band": power_in_idle_band(sample.get("ac_kw"),
                                                 idle_lower_kw, idle_upper_kw),
        "authority_idle": sample.get("authority") == "IDLE",
        "comm_ok": sample.get("comm_ok") is True,
        "fault_clear": sample.get("fault") is False,
        "alarm_clear": sample.get("critical_alarms") == 0,
    }
    return all(checks.values()), checks


class PauseVerifier(object):
    """
    兩階段 pause 驗證。逐筆 feed()，回傳當下階段。

    🔴 stable_samples / settling_timeout_sec 皆須由呼叫端提供，不預設。
    🔴 樣本不 fresh -> UNKNOWN_SOURCE（Fail Closed），
       不當成通過，也不當成「還在收斂」。
    """

    def __init__(self, idle_lower_kw, idle_upper_kw, stable_samples,
                 settling_timeout_sec, clock=None):
        if stable_samples is None or int(stable_samples) < 1:
            raise ValueError("stable_samples 必須 >= 1（不得預設）")
        if settling_timeout_sec is None or float(settling_timeout_sec) <= 0:
            raise ValueError("settling_timeout_sec 必須為正數（不得預設為無限）")
        self.idle_lower_kw = idle_lower_kw
        self.idle_upper_kw = idle_upper_kw
        self.stable_samples = int(stable_samples)
        self.settling_timeout_sec = float(settling_timeout_sec)
        self._clock = clock or time.monotonic
        self.started_at = self._clock()
        self.state = PV_SETTLING
        self.stable_hits = 0
        self.window_states = []
        self.reason = None
        self.last_checks = {}

    @property
    def external_quiescent(self):
        """🔴 只有 STABLE_VERIFY 全數完成才為 True。"""
        return self.state == PV_VERIFIED

    def feed(self, sample, probe):
        if self.state in (PV_VERIFIED, PV_FAILED, PV_SETTLING_TIMEOUT,
                          PV_UNKNOWN):
            return self.state
        if sample.get("fresh") is False:
            self.state = PV_UNKNOWN
            self.reason = "樣本來源不 fresh -> 無法判定"
            return self.state
        ok, checks = candidate_idle(sample, probe,
                                    self.idle_lower_kw, self.idle_upper_kw)
        self.last_checks = checks

        if self.state == PV_SETTLING:
            if ok:
                # 首次 candidate-idle -> 進入 STABLE_VERIFY，本筆即計為第 1 筆
                self.state = PV_STABLE_VERIFY
                self.stable_hits = 1
                self.window_states = [sample.get("pcs_state")]
                if self.stable_hits >= self.stable_samples:
                    self.state = PV_VERIFIED
                return self.state
            if self._clock() - self.started_at > self.settling_timeout_sec:
                self.state = PV_SETTLING_TIMEOUT
                self.reason = "settling 逾時仍未出現 candidate-idle"
            return self.state

        # STABLE_VERIFY：任一筆不成立即 FAIL（不重新計數、不給第二次機會）
        if not ok:
            self.state = PV_FAILED
            self.reason = ("stable window 內出現不成立項："
                           + ",".join(k for k, v in checks.items() if not v))
            return self.state
        self.stable_hits += 1
        self.window_states.append(sample.get("pcs_state"))
        # PCS 狀態穩定性**只在 STABLE_VERIFY window 內**要求
        if len(set(self.window_states)) > 1:
            self.state = PV_FAILED
            self.reason = "stable window 內 PCS 狀態變動：%s" % (self.window_states,)
            return self.state
        if self.stable_hits >= self.stable_samples:
            self.state = PV_VERIFIED
        return self.state


# ======================================================================
# Phase 6.10-A1：Restore Verification 兩階段
# ======================================================================
RV_SENT = "RESTORE_SENT"
RV_SETTLING = "RUNTIME_SETTLING"
RV_RESUMED = "RESUMED"
RV_UNKNOWN = "NEEDS_ADDITIONAL_PROBE"
RV_FAILED = "FAILED"


class RestoreVerifier(object):
    """
    兩階段 restore 驗證。

    A 層 runtime health   ssh / screen / pid / identity
    B 層 loop activity    至少兩筆 fresh 觀測，且 loop evidence 確實前進

    🔴 **不要求 PCS 一定 CHARGING/DISCHARGING** —— 恢復時 SOC / TOU / demand
       可能正確地決定 idle，那不是恢復失敗。
    🔴 runtime 健康但 loop evidence 無法確認 -> NEEDS_ADDITIONAL_PROBE，
       **不得 PASS**。
    """

    def __init__(self, min_loop_observations, settling_timeout_sec, clock=None):
        if min_loop_observations is None or int(min_loop_observations) < 2:
            raise ValueError("min_loop_observations 必須 >= 2（單點不足以證明前進）")
        if settling_timeout_sec is None or float(settling_timeout_sec) <= 0:
            raise ValueError("settling_timeout_sec 必須為正數")
        self.min_loop_observations = int(min_loop_observations)
        self.settling_timeout_sec = float(settling_timeout_sec)
        self._clock = clock or time.monotonic
        self.started_at = self._clock()
        self.state = RV_SENT
        self.loop_marks = []
        self.reason = None

    def feed(self, probe, loop_mark):
        """probe: RemoteProbe；loop_mark: 本次觀測到的 loop 時間戳（None = 取不到）。"""
        if self.state in (RV_RESUMED, RV_FAILED, RV_UNKNOWN):
            return self.state
        if not probe.healthy:
            if self._clock() - self.started_at > self.settling_timeout_sec:
                self.state = RV_FAILED
                self.reason = "runtime 逾時仍未健康"
                return self.state
            self.state = RV_SETTLING
            return self.state
        self.state = RV_SETTLING
        if loop_mark is not None:
            self.loop_marks.append(loop_mark)
        if len(self.loop_marks) >= self.min_loop_observations:
            if self.loop_marks[-1] > self.loop_marks[0]:
                self.state = RV_RESUMED
            else:
                self.state = RV_FAILED
                self.reason = "loop evidence 未前進：%s" % (self.loop_marks,)
            return self.state
        if self._clock() - self.started_at > self.settling_timeout_sec:
            self.state = RV_UNKNOWN
            self.reason = ("runtime 健康但 loop 觀測僅 %d 筆（需 %d）"
                           % (len(self.loop_marks), self.min_loop_observations))
        return self.state


# ======================================================================
# Phase 6.10-A1：authorized_keys 部署規則
# ======================================================================
REQUIRED_KEY_OPTIONS = ('command="/home/etica/ems/phase6_remote_guard.sh"',
                        "restrict", 'from="192.168.128.234"')


def authorized_keys_ok(lines, pubkey_b64):
    """
    檢查部署後的 authorized_keys 是否合規。回傳 (ok, problems)。

    🔴 **必須 REPLACE，不得 APPEND**：若舊的無 forced-command 條目仍在，
       allowlist 就完全失去意義 —— 仍可經由舊條目執行任意遠端指令。
       因此要求該 public key 在檔案中**恰好出現一次**。
    """
    problems = []
    hits = [ln for ln in lines
            if pubkey_b64 in ln and not ln.strip().startswith("#")]
    if len(hits) == 0:
        return False, ["找不到該 dedicated public key"]
    if len(hits) > 1:
        return False, ["該 public key 出現 %d 次 —— 必須 REPLACE 而非 APPEND"
                       % len(hits)]
    entry = hits[0]
    for opt in REQUIRED_KEY_OPTIONS:
        if opt not in entry:
            problems.append("唯一條目缺少必要選項：%s" % opt)
    return (not problems), problems


# ======================================================================
# Restore Verification —— 兩層
# ======================================================================
RESTORE_HEALTHY = "HEALTHY"
RESTORE_RESUMED = "RESUMED"
RESTORE_UNKNOWN = "NEEDS_ADDITIONAL_PROBE"
RESTORE_FAILED = "FAILED"


def verify_restore(probe, loop_active):
    """
    回傳 (verdict, checks)。

    A 層 runtime health : screen / process / identity / ssh
    B 層 policy loop     : 控制迴圈是否重新活動（時間戳前進）

    🔴 **不要求恢復後 PCS 一定出力** —— SOC / TOU / demand 可能正確地
       決定 idle，那不是恢復失敗。
    🔴 B 層無法判定（loop_active is None）→ `NEEDS_ADDITIONAL_PROBE`，
       **不得假裝 PASS**。
    """
    checks = {
        "ssh_ok": probe.ssh_ok is True,
        "screen_alive": probe.screen_alive is True,
        "process_alive": probe.process_alive is True,
        "process_identity_ok": probe.process_identity_ok is True,
    }
    if not all(checks.values()):
        return RESTORE_FAILED, checks
    checks["loop_active"] = loop_active
    if loop_active is True:
        return RESTORE_RESUMED, checks
    if loop_active is False:
        return RESTORE_FAILED, checks
    return RESTORE_UNKNOWN, checks


# ======================================================================
# Remote Guard wrapper（設計，本階段**不部署**）
# ======================================================================
# 🔴 dedicated key 目前雖有 restrict,from=...，但那**不等於** command allowlist
#    —— 該 key 仍具備 etica 帳號的任意遠端指令執行能力。
#    完全 unattended 前，應改為 forced command，只放行固定動詞。
#
#    authorized_keys（**尚未部署**）：
#      command="/home/etica/ems/phase6_remote_guard.sh",restrict,from="..." ssh-ed25519 ...
REMOTE_GUARD_SH = r'''#!/bin/bash
# phase6_remote_guard.sh - forced-command allowlist for Phase 6 handoff.
#
# Deployed via authorized_keys (REPLACE the existing dedicated-key entry,
# do NOT append a second one):
#   command="/home/etica/ems/phase6_remote_guard.sh",restrict,from="<ip>" ssh-ed25519 AAAA... phase6-firstlive-2026-09
#
# Constraints (Phase 6.10-A1):
#   - reads only SSH_ORIGINAL_COMMAND, exact match against a fixed allowlist
#   - never uses eval; never passes the original command to sh -c
#   - rejects any additional arguments (exact string compare, not prefix)
#   - absolute executable paths only
#   - sanitized environment (PATH reset, IFS reset)
#   - explicit timeout on every remote action
#   - explicit exit codes
set -euo pipefail
IFS=$' \t\n'
PATH=/usr/bin:/bin
export PATH
unset BASH_ENV CDPATH ENV LD_PRELOAD LD_LIBRARY_PATH

SCREEN=/usr/bin/screen
PGREP=/usr/bin/pgrep
GREP=/usr/bin/grep
HEAD=/usr/bin/head
TAIL=/usr/bin/tail
READLINK=/usr/bin/readlink
SLEEP=/usr/bin/sleep
TIMEOUT=/usr/bin/timeout

SCREEN_NAME=auto
ACT_TIMEOUT=10
HARDCOPY=/tmp/p6_guard.txt

# Exit codes
#   0  ok
#  41  remote action timed out / internal failure
#  42  refused (not in allowlist)
#  43  SSH_ORIGINAL_COMMAND missing
EX_TIMEOUT=41
EX_REFUSED=42
EX_MISSING=43

VERB="${SSH_ORIGINAL_COMMAND-}"
if [ -z "$VERB" ]; then
  echo "REFUSED: no verb supplied" >&2
  exit $EX_MISSING
fi

run() { "$TIMEOUT" "$ACT_TIMEOUT" "$@" || return $EX_TIMEOUT; }

case "$VERB" in
  probe)
    "$SCREEN" -ls | "$GREP" -c "\.${SCREEN_NAME}[[:space:]]" || true
    "$PGREP" -af 'auto_control\.py' | "$HEAD" -1 || true
    P=$("$PGREP" -f 'auto_control\.py' | "$HEAD" -1 || true)
    if [ -n "$P" ]; then "$READLINK" -f "/proc/$P/cwd" || true; fi
    exit 0
    ;;
  loopcheck)
    run "$SCREEN" -S "$SCREEN_NAME" -X hardcopy "$HARDCOPY"
    "$SLEEP" 2
    run "$SCREEN" -S "$SCREEN_NAME" -X hardcopy "$HARDCOPY"
    "$GREP" -aoE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' \
      "$HARDCOPY" | "$TAIL" -1 || true
    exit 0
    ;;
  status)
    run "$SCREEN" -S "$SCREEN_NAME" -X stuff $'status\n'
    exit 0
    ;;
  pause)
    run "$SCREEN" -S "$SCREEN_NAME" -X stuff $'stop\n'
    exit 0
    ;;
  restore)
    run "$SCREEN" -S "$SCREEN_NAME" -X stuff $'start\n'
    exit 0
    ;;
  *)
    echo "REFUSED: only probe|loopcheck|status|pause|restore are allowed" >&2
    exit $EX_REFUSED
    ;;
esac
'''

# ======================================================================
# Phase 6.10-B1：實際部署版本 —— **只放行唯讀動詞**
# ======================================================================
# 🔴 B1 階段刻意**不**把 pause / restore 放進 deployed allowlist。
#    即使完整版 wrapper 的 offline test 已涵蓋那兩個動詞，
#    部署一個具備實機控制能力的 forced command 本身就是風險，
#    必須等 B2（且需先解決網路身分穩定性等前置）才開放。
#
# 🔴 screen 定位改用 **session name**，不依賴 PID（reboot / restart 會改變）。
#    送任何 screen 指令前要求 `.auto` 恰好 1 個；0 或 >1 一律 Fail Closed。
#
# 🔴 hardcopy 使用 mktemp + trap 清理，避免併發碰撞與 stale evidence 被誤判。
GUARD_B1_ALLOWED_VERBS = ("probe", "loopcheck", "status")
GUARD_EXIT_AMBIGUOUS = 44        # screen 數量不是 1
GUARD_EXIT_IDENTITY = 45         # 行程身分鏈不符

REMOTE_GUARD_B1_SH = r"""#!/bin/bash
# phase6_remote_guard.sh - Phase 6.10-B1 forced-command allowlist (READ-ONLY verbs).
#
# authorized_keys (REPLACE the existing dedicated-key entry, never append):
#   command="/home/etica/ems/phase6_remote_guard.sh",restrict,from="<ip>" ssh-ed25519 AAAA... phase6-firstlive-2026-09
#
# B1 deployed verbs: probe | loopcheck | status
#   pause / restore are intentionally NOT deployed in B1 -> REFUSED.
set -euo pipefail
IFS=$' \t\n'
PATH=/usr/bin:/bin
export PATH
unset BASH_ENV CDPATH ENV LD_PRELOAD LD_LIBRARY_PATH

SCREEN=/usr/bin/screen
PGREP=/usr/bin/pgrep
GREP=/usr/bin/grep
HEAD=/usr/bin/head
TAIL=/usr/bin/tail
READLINK=/usr/bin/readlink
SLEEP=/usr/bin/sleep
TIMEOUT=/usr/bin/timeout
MKTEMP=/usr/bin/mktemp
RM=/usr/bin/rm
PS=/usr/bin/ps

SCREEN_NAME=auto
EXPECTED_CMD='python3 auto_control.py'
EXPECTED_CWD=/home/etica/ems
ACT_TIMEOUT=10

EX_TIMEOUT=41
EX_REFUSED=42
EX_MISSING=43
EX_AMBIGUOUS=44
EX_IDENTITY=45

VERB="${SSH_ORIGINAL_COMMAND-}"
if [ -z "$VERB" ]; then
  echo "REFUSED: no verb supplied" >&2
  exit $EX_MISSING
fi

# ---- locate the controller by SESSION NAME, never by a hard-coded PID ----
screen_count() {
  local n
  n=$("$SCREEN" -ls 2>/dev/null | "$GREP" -c "\.${SCREEN_NAME}[[:space:]]" || true)
  echo "${n:-0}"
}

require_single_screen() {
  local n
  n=$(screen_count)
  if [ "$n" != "1" ]; then
    echo "AMBIGUOUS: found $n screen sessions named .${SCREEN_NAME} (need exactly 1)" >&2
    exit $EX_AMBIGUOUS
  fi
}

controller_pid() {
  "$PGREP" -f 'auto_control\.py' | "$HEAD" -1 || true
}

require_identity() {
  local p cwd cmd
  p=$(controller_pid)
  if [ -z "$p" ]; then
    echo "IDENTITY: auto_control.py not running" >&2
    exit $EX_IDENTITY
  fi
  cwd=$("$READLINK" -f "/proc/$p/cwd" || true)
  cmd=$("$PS" -o cmd= -p "$p" || true)
  if [ "$cwd" != "$EXPECTED_CWD" ]; then
    echo "IDENTITY: cwd=$cwd expected=$EXPECTED_CWD" >&2
    exit $EX_IDENTITY
  fi
  if [ "$cmd" != "$EXPECTED_CMD" ]; then
    echo "IDENTITY: cmd=$cmd expected=$EXPECTED_CMD" >&2
    exit $EX_IDENTITY
  fi
  echo "$p"
}

case "$VERB" in
  probe)
    echo "screen_count=$(screen_count)"
    P=$(controller_pid)
    echo "pid=${P:-}"
    if [ -n "${P:-}" ]; then
      echo "cwd=$("$READLINK" -f "/proc/$P/cwd" || true)"
      echo "exe=$("$READLINK" -f "/proc/$P/exe" || true)"
      echo "cmd=$("$PS" -o cmd= -p "$P" || true)"
      echo "ppid=$("$PS" -o ppid= -p "$P" | "$GREP" -oE '[0-9]+' || true)"
    fi
    exit 0
    ;;
  loopcheck)
    require_single_screen
    require_identity >/dev/null
    HC=$("$MKTEMP" /tmp/p6guard.XXXXXXXX)
    trap '"$RM" -f "$HC"' EXIT
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X hardcopy "$HC" \
      || exit $EX_TIMEOUT
    echo "mark1=$("$GREP" -aoE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$HC" | "$TAIL" -1 || true)"
    "$SLEEP" 3
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X hardcopy "$HC" \
      || exit $EX_TIMEOUT
    echo "mark2=$("$GREP" -aoE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$HC" | "$TAIL" -1 || true)"
    exit 0
    ;;
  status)
    require_single_screen
    require_identity >/dev/null
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X stuff $'status\n' \
      || exit $EX_TIMEOUT
    echo "status_injected"
    exit 0
    ;;
  *)
    echo "REFUSED: B1 allows only probe|loopcheck|status" >&2
    exit $EX_REFUSED
    ;;
esac
"""


# ======================================================================
# Phase 6.10-B2：加入 pause / restore 的版本（**離線開發，尚未部署**）
# ======================================================================
# 🔴 與 B1 的唯一差異是多了兩個控制動詞；B1 的所有強化一律沿用：
#      set -euo pipefail / PATH 與環境淨化 / 絕對路徑 / timeout /
#      精確比對（非前綴）/ 無 eval / 不交給 sh -c /
#      screen 以 session name 定位且要求恰好 1 個 / 身分鏈驗證 /
#      hardcopy 用 mktemp + trap
#
# 🔴 pause / restore **必須**先通過 require_single_screen + require_identity。
#    B1 的 probe 刻意不強制（probe 本來就是用來診斷「現在到底是什麼狀態」），
#    但控制動詞不同 —— 對不確定身分的目標送控制指令是不可接受的。
#
# 🔴 本常數存在**不代表已部署**。目前遠端仍是 B1（唯讀動詞）。
#    部署需另行授權，且必須 REPLACE authorized_keys 的既有條目。
GUARD_B2_ALLOWED_VERBS = ("probe", "loopcheck", "status", "pause", "restore")

REMOTE_GUARD_B2_SH = r"""#!/bin/bash
# phase6_remote_guard.sh - Phase 6.10-B2 forced-command allowlist.
#
# authorized_keys (REPLACE the existing dedicated-key entry, never append):
#   command="/home/etica/ems/phase6_remote_guard.sh",restrict,from="<ip>" ssh-ed25519 AAAA... phase6-firstlive-2026-09
#
# B2 verbs: probe | loopcheck | status | pause | restore
#   pause  -> injects the controller's built-in "stop"  (stops its automation
#             loop and converges the active power command to 0; it is NOT a
#             PCS STOP command, so the PCS may remain in STANDBY)
#   restore-> injects the controller's built-in "start"
set -euo pipefail
IFS=$' 	
'
PATH=/usr/bin:/bin
export PATH
unset BASH_ENV CDPATH ENV LD_PRELOAD LD_LIBRARY_PATH

SCREEN=/usr/bin/screen
PGREP=/usr/bin/pgrep
GREP=/usr/bin/grep
HEAD=/usr/bin/head
TAIL=/usr/bin/tail
READLINK=/usr/bin/readlink
SLEEP=/usr/bin/sleep
TIMEOUT=/usr/bin/timeout
MKTEMP=/usr/bin/mktemp
RM=/usr/bin/rm
PS=/usr/bin/ps

SCREEN_NAME=auto
EXPECTED_CMD='python3 auto_control.py'
EXPECTED_CWD=/home/etica/ems
ACT_TIMEOUT=10

EX_TIMEOUT=41
EX_REFUSED=42
EX_MISSING=43
EX_AMBIGUOUS=44
EX_IDENTITY=45

# ---- C4.1 capability reporting ----
# guard_variant 與 capability_* 一律由**本腳本自己的 allowlist** 推導。
# 同一份 ALLOWED_VERBS 既決定 dispatch 是否放行，也決定 probe 回報什麼，
# 因此「回報的能力」與「實際的能力」在結構上不可能不一致。
# Windows caller 不得自行猜測，只能解析這裡輸出的欄位。
GUARD_VARIANT=B2
ALLOWED_VERBS='probe loopcheck status pause restore'
KNOWN_VERBS='probe loopcheck status pause restore'

VERB="${SSH_ORIGINAL_COMMAND-}"
if [ -z "$VERB" ]; then
  echo "REFUSED: no verb supplied" >&2
  exit $EX_MISSING
fi

verb_allowed() {
  local v
  for v in $ALLOWED_VERBS; do
    if [ "$v" = "$1" ]; then
      return 0
    fi
  done
  return 1
}

emit_capabilities() {
  echo "guard_variant=$GUARD_VARIANT"
  local v
  for v in $KNOWN_VERBS; do
    if verb_allowed "$v"; then
      echo "capability_$v=true"
    else
      echo "capability_$v=false"
    fi
  done
}

# 精確比對，且**在 case 之前**就擋掉 —— case 的 *) 分支保留為第二道防線。
if ! verb_allowed "$VERB"; then
  echo "REFUSED: only $ALLOWED_VERBS are allowed" >&2
  exit $EX_REFUSED
fi

screen_count() {
  local n
  n=$("$SCREEN" -ls 2>/dev/null | "$GREP" -c "\.${SCREEN_NAME}[[:space:]]" || true)
  echo "${n:-0}"
}

require_single_screen() {
  local n
  n=$(screen_count)
  if [ "$n" != "1" ]; then
    echo "AMBIGUOUS: found $n screen sessions named .${SCREEN_NAME} (need exactly 1)" >&2
    exit $EX_AMBIGUOUS
  fi
}

controller_pid() {
  "$PGREP" -f 'auto_control\.py' | "$HEAD" -1 || true
}

require_identity() {
  local p cwd cmd
  p=$(controller_pid)
  if [ -z "$p" ]; then
    echo "IDENTITY: auto_control.py not running" >&2
    exit $EX_IDENTITY
  fi
  cwd=$("$READLINK" -f "/proc/$p/cwd" || true)
  cmd=$("$PS" -o cmd= -p "$p" || true)
  if [ "$cwd" != "$EXPECTED_CWD" ]; then
    echo "IDENTITY: cwd=$cwd expected=$EXPECTED_CWD" >&2
    exit $EX_IDENTITY
  fi
  if [ "$cmd" != "$EXPECTED_CMD" ]; then
    echo "IDENTITY: cmd=$cmd expected=$EXPECTED_CMD" >&2
    exit $EX_IDENTITY
  fi
  echo "$p"
}

case "$VERB" in
  probe)
    emit_capabilities
    echo "screen_count=$(screen_count)"
    P=$(controller_pid)
    echo "pid=${P:-}"
    if [ -n "${P:-}" ]; then
      echo "cwd=$("$READLINK" -f "/proc/$P/cwd" || true)"
      echo "exe=$("$READLINK" -f "/proc/$P/exe" || true)"
      echo "cmd=$("$PS" -o cmd= -p "$P" || true)"
      echo "ppid=$("$PS" -o ppid= -p "$P" | "$GREP" -oE '[0-9]+' || true)"
    fi
    exit 0
    ;;
  loopcheck)
    require_single_screen
    require_identity >/dev/null
    HC=$("$MKTEMP" /tmp/p6guard.XXXXXXXX)
    trap '"$RM" -f "$HC"' EXIT
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X hardcopy "$HC"       || exit $EX_TIMEOUT
    echo "mark1=$("$GREP" -aoE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$HC" | "$TAIL" -1 || true)"
    "$SLEEP" 3
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X hardcopy "$HC"       || exit $EX_TIMEOUT
    echo "mark2=$("$GREP" -aoE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$HC" | "$TAIL" -1 || true)"
    exit 0
    ;;
  status)
    require_single_screen
    require_identity >/dev/null
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X stuff $'status
'       || exit $EX_TIMEOUT
    echo "status_injected"
    exit 0
    ;;
  pause)
    require_single_screen
    require_identity >/dev/null
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X stuff $'stop
'       || exit $EX_TIMEOUT
    echo "pause_injected"
    exit 0
    ;;
  restore)
    require_single_screen
    require_identity >/dev/null
    "$TIMEOUT" "$ACT_TIMEOUT" "$SCREEN" -S "$SCREEN_NAME" -X stuff $'start
'       || exit $EX_TIMEOUT
    echo "restore_injected"
    exit 0
    ;;
  *)
    # 第二道防線：理論上永遠到不了這裡（verb_allowed 已先擋），
    # 但白名單一旦被改壞，這裡仍然 default deny。
    echo "REFUSED: only probe|loopcheck|status|pause|restore are allowed" >&2
    exit $EX_REFUSED
    ;;
esac
"""

# ======================================================================
# C4.2 / C4.3. Guard capability report —— 解析 probe 回報的能力
# ======================================================================
# 🔴 **能力以 guard 自己回報的為準，不以 Windows 端的常數為準。**
#    `DEPLOYED_GUARD_VARIANT` 只是本機的紀錄，可能過期、可能寫錯；
#    真正決定 pause / restore 能不能用的是遠端 guard 的 allowlist。
#
# 🔴 **舊 B1 沒有 capability 欄位** —— 那要判成 `CAPABILITY_NOT_REPORTED`，
#    **不得**自動假設 false，更不得假設 true。差別在於：
#      false          = guard 明確說「我沒有這個能力」
#      NOT_REPORTED   = 我們根本不知道（可能是舊版，也可能是輸出被截斷）
#    兩者對 live 的結論相同（REFUSED），但對診斷的意義完全不同。
CAP_REPORTED = "CAPABILITY_REPORTED"
CAP_NOT_REPORTED = "CAPABILITY_NOT_REPORTED"
CAP_MALFORMED = "CAPABILITY_MALFORMED"

CAPABILITY_VERBS = ("probe", "loopcheck", "status", "pause", "restore")
KNOWN_GUARD_VARIANTS = ("B1", "B2")

_CAP_LINE = re.compile(r"^capability_([a-z]+)=(true|false)$")
_VAR_LINE = re.compile(r"^guard_variant=(.+)$")


class GuardCapability(object):
    """
    guard 自我回報的能力。**任何不確定一律不放行。**

    status:
        CAPABILITY_REPORTED      欄位齊全且合法
        CAPABILITY_NOT_REPORTED  完全沒有 capability 欄位（舊 B1）
        CAPABILITY_MALFORMED     有欄位但殘缺 / 值非法 / variant 不認得
    """

    def __init__(self, status, variant=None, capabilities=None, detail=None):
        self.status = status
        self.variant = variant
        self.capabilities = dict(capabilities or {})
        self.detail = detail

    def can(self, verb):
        """
        該動詞是否**明確被回報為可用**。

        🔴 未回報 / 殘缺 / 未知一律 False —— 這裡不區分「不能」與
           「不知道」，因為對放行決策而言兩者都是不放行。要區分請看 status。
        """
        if self.status != CAP_REPORTED:
            return False
        return self.capabilities.get(verb) is True

    @property
    def reported(self):
        return self.status == CAP_REPORTED

    def __repr__(self):
        return "<GuardCapability %s variant=%r %r>" % (
            self.status, self.variant, self.capabilities)


def parse_capability(probe_output):
    """
    從 probe 的 stdout 解析 capability metadata。

    🔴 只接受**精確**的 `capability_<verb>=true|false` 與
       `guard_variant=<B1|B2>`。任何多餘字元、大小寫變化、非布林值
       一律判 MALFORMED —— 不做寬鬆解析。
    """
    if probe_output is None:
        return GuardCapability(CAP_MALFORMED, detail="probe 無輸出")
    lines = [ln.strip() for ln in str(probe_output).splitlines()]

    variant = None
    variant_seen = 0
    caps = {}
    bad = []
    for ln in lines:
        m = _VAR_LINE.match(ln)
        if m:
            variant_seen += 1
            variant = m.group(1).strip()
            continue
        if ln.startswith("capability"):
            m2 = _CAP_LINE.match(ln)
            if not m2:
                bad.append(ln)
                continue
            verb, val = m2.group(1), m2.group(2)
            if verb not in CAPABILITY_VERBS:
                bad.append(ln)
                continue
            if verb in caps:
                bad.append(ln)          # 重複回報 → 不可信
                continue
            caps[verb] = (val == "true")

    if variant is None and not caps and not bad:
        # 舊 B1：完全沒有 capability 欄位
        return GuardCapability(CAP_NOT_REPORTED,
                               detail="probe 輸出不含 capability 欄位")
    if bad:
        return GuardCapability(CAP_MALFORMED, variant=variant,
                               capabilities=caps,
                               detail="無法解析的欄位：%s" % (bad[:3],))
    if variant_seen != 1:
        return GuardCapability(CAP_MALFORMED, variant=variant,
                               capabilities=caps,
                               detail="guard_variant 出現 %d 次（需恰好 1 次）"
                                      % variant_seen)
    if variant not in KNOWN_GUARD_VARIANTS:
        return GuardCapability(CAP_MALFORMED, variant=variant,
                               capabilities=caps,
                               detail="未知的 guard_variant：%r" % (variant,))
    missing = [v for v in CAPABILITY_VERBS if v not in caps]
    if missing:
        return GuardCapability(CAP_MALFORMED, variant=variant,
                               capabilities=caps,
                               detail="缺少 capability 欄位：%s" % (missing,))
    return GuardCapability(CAP_REPORTED, variant=variant, capabilities=caps)


CAPABILITY_GATE_ITEMS = ("capability_reported", "variant_is_b2",
                         "capability_pause", "capability_restore")


def capability_gate(cap):
    """
    控制能力閘。回傳 (ok, checks)。

    🔴 **不得只看 `DEPLOYED_GUARD_VARIANT == "B2"`。** 那是本機紀錄，
       不是現場事實。必須同時要求 guard 自己回報 variant=B2 且
       pause / restore 兩項能力皆為 true。
    🔴 missing / malformed / unknown 全部 Fail Closed。
    """
    if cap is None:
        cap = GuardCapability(CAP_NOT_REPORTED, detail="未取得 probe 輸出")
    checks = {
        "capability_reported": cap.status == CAP_REPORTED,
        "variant_is_b2": cap.variant == "B2" and cap.status == CAP_REPORTED,
        "capability_pause": cap.can("pause"),
        "capability_restore": cap.can("restore"),
    }
    return all(checks.values()), checks


def readonly_diagnosis_allowed(cap):
    """
    🔴 capability 未回報**不影響唯讀診斷** —— B1 現場本來就沒有這些欄位，
       若因此連 probe / status / loopcheck 都不准，等於自斷觀測能力。
       只有 MALFORMED（輸出本身不可信）才連診斷都要存疑。
    """
    if cap is None:
        return True
    return cap.status in (CAP_REPORTED, CAP_NOT_REPORTED)


# 目前實際部署在遠端的版本（B1）。改動此常數不會改變遠端；部署需另行授權。
DEPLOYED_GUARD_VARIANT = "B1"


def guard_b2_would_accept(verb):
    """離線模擬 B2 allowlist（尚未部署）。"""
    return verb in GUARD_B2_ALLOWED_VERBS


def guard_b1_would_accept(verb):
    """離線模擬 B1 allowlist（pause / restore 在 B1 不放行）。"""
    return verb in GUARD_B1_ALLOWED_VERBS


GUARD_ALLOWED_VERBS = ("probe", "loopcheck", "status", "pause", "restore")
GUARD_EXIT_OK = 0
GUARD_EXIT_TIMEOUT = 41
GUARD_EXIT_REFUSED = 42
GUARD_EXIT_MISSING = 43


def guard_would_accept(verb):
    """
    離線模擬 wrapper 的 allowlist 判定（供測試；**不執行任何遠端指令**）。

    🔴 精確比對，不做前綴 / 子字串匹配 —— `pause; rm -rf /` 必須被拒。
    """
    return verb in GUARD_ALLOWED_VERBS


# ======================================================================
# C2. SSH Timeout Architecture —— **值全部 UNRESOLVED，只定架構**
# ======================================================================
# 🔴 **不得**使用單一 `ssh_timeout_sec`。至少要分成兩件事：
#
#   ssh_connect_timeout_sec   TCP / SSH handshake / authentication 階段
#                             「連不連得上這台主機」
#   ssh_command_timeout_sec   連線建立後，等待 remote verb 跑完
#                             「主機連上了，但指令跑不跑得完」
#
#   兩者失敗的意義完全不同（見 taxonomy），後續 audit / recovery 必須分辨。
#
# 🔴 command timeout **不可全部共用一個值**：guard 的各動詞 server-side
#    執行窗差很多。以實際部署的 guard 原始碼推導的結構上界：
#
#      probe      只讀 /proc 與 ps，無 $TIMEOUT 包裹  → 無明確結構上界
#      status     1 × ACT_TIMEOUT(10)                 ≈ 10 s
#      loopcheck  hardcopy(10) + sleep(3) + hardcopy(10) ≈ 23 s
#      pause      1 × ACT_TIMEOUT(10)                 ≈ 10 s
#      restore    1 × ACT_TIMEOUT(10)                 ≈ 10 s
#
# 🔴 **這些是 server-side 結構上界／估算，不是實測 SSH latency，
#    因此不得直接拿來當 ssh_command_timeout_sec 的 FINAL 值。**
#    真正的 client timeout 還要加上網路 RTT、handshake 後的資料往返、
#    以及現場 jitter —— 這些目前一筆實測都沒有。
GUARD_ACT_TIMEOUT_SEC = 10.0          # 部署中 guard 的 $ACT_TIMEOUT（既有事實）
GUARD_LOOPCHECK_SLEEP_SEC = 3.0       # loopcheck 兩次 hardcopy 之間的 sleep

# 每個動詞的 server-side 結構上界；None = 無明確上界（不得當成「沒有限制」）
GUARD_SERVER_SIDE_BOUND_SEC = {
    "probe": None,
    "status": GUARD_ACT_TIMEOUT_SEC,
    "loopcheck": GUARD_ACT_TIMEOUT_SEC * 2 + GUARD_LOOPCHECK_SLEEP_SEC,
    "pause": GUARD_ACT_TIMEOUT_SEC,
    "restore": GUARD_ACT_TIMEOUT_SEC,
}

# 唯讀動詞 vs 控制動詞 —— 兩者的 timeout 語意不同（見 classify_send_outcome）
READONLY_VERBS = ("probe", "status", "loopcheck")
CONTROL_VERBS = ("pause", "restore")


def guard_server_side_bound_sec(verb):
    """
    該動詞在 guard 內的結構執行上界（秒）。None = 無明確上界。

    🔴 這是**證據**，不是候選值。呼叫端不得把它當成 command timeout 直接用。
    """
    if verb not in GUARD_ALLOWED_VERBS:
        raise ValueError("未知的 guard 動詞：%r" % (verb,))
    return GUARD_SERVER_SIDE_BOUND_SEC[verb]


# ---- C2-6. Timeout result taxonomy ----------------------------------
# 🔴 **不得**把所有遠端失敗壓成一個 REMOTE_FAILED。
#    「連不上主機」與「主機連上了但指令沒跑完」是兩種不同故障，
#    recovery 的處置也完全不同。
SSH_OK = "SSH_OK"
SSH_CONNECT_TIMEOUT = "SSH_CONNECT_TIMEOUT"      # 沒連上 → 指令必然未送達
SSH_AUTH_FAILED = "SSH_AUTH_FAILED"              # 連上了但身分被拒 → 未執行
SSH_COMMAND_TIMEOUT = "SSH_COMMAND_TIMEOUT"      # 已連上，結果未知 ← 危險的一種
SSH_REFUSED = "SSH_REFUSED"                      # guard allowlist 拒絕 → 未執行
SSH_ERROR = "SSH_ERROR"                          # 其他非零 exit
SSH_NOT_CONFIGURED = "SSH_NOT_CONFIGURED"        # timeout / runner 未提供

SSH_OUTCOMES = (SSH_OK, SSH_CONNECT_TIMEOUT, SSH_AUTH_FAILED,
                SSH_COMMAND_TIMEOUT, SSH_REFUSED, SSH_ERROR,
                SSH_NOT_CONFIGURED)

# 「可以斷定遠端沒有執行」的 outcome。SSH_COMMAND_TIMEOUT **不在其中**。
SSH_DEFINITELY_NOT_EXECUTED = (SSH_CONNECT_TIMEOUT, SSH_AUTH_FAILED,
                               SSH_REFUSED, SSH_NOT_CONFIGURED)
# 「遠端可能已經執行，但我們收不到結果」的 outcome。
SSH_OUTCOME_UNCERTAIN = (SSH_COMMAND_TIMEOUT,)


# ---- C3. 已核准的 SSH timeout（2026-09-03 裁示）--------------------
# 依據：2026-09-03 17:33:34 ~ 17:36:16 對 B1 guard 的唯讀量測，
#       115 筆樣本、0 失敗（probe 60 / status 30 / loopcheck 25）。
#       evidence: output/phase6_ssh_timing/ssh_timing_20260903_173334.jsonl
#       sha256 beeab404917198c88121f71ee898f117cb38b38b99e07b012b2376c26da50ce8
#
#   connect  5.0   實測 connect+auth max 0.5089 / p50 0.2742（n=115）
#                  ≈ 9.8 × max；與既有 api_client.CONNECT_TIMEOUT_SEC 對齊；
#                  下限 3.0（容得下兩次 TCP SYN 重傳）、上限 8.0。
#   probe    5.0   實測 max 0.4136（n=60）。**guard 端無 ACT_TIMEOUT backstop**，
#                  client timeout 是唯一上界 → ≈ 12 × max。
#   status  12.0   結構窗 10.0（1 × ACT_TIMEOUT）+ 實測最大額外成本 1.6450
#                  = 11.645 → 12.0。必須 > 10.0，否則 client 會搶在 guard 的
#                  exit 41 之前逾時，把「server 明確回報」降級成「結果不明」。
#   loopcheck 26.0 結構窗 23.0（10 + sleep 3 + 10）+ 實測扣除固定 sleep 後的
#                  最大額外成本 2.0792 = 25.08 → 26.0。
#                  🔴 26 s 不代表 loopcheck 需要 26 秒 —— 正常實測只有 3~5 s。
#                     它的用途是「不要在 guard 合法的最壞窗完成前先自行放棄」。
APPROVED_SSH_CONNECT_TIMEOUT_SEC = 5.0
APPROVED_SSH_COMMAND_TIMEOUT_SEC = {
    "probe": 5.0,
    "status": 12.0,
    "loopcheck": 26.0,
}

# 🔴 pause / restore **沒有實測樣本** —— 本輪明文禁止控制取樣。
#    以下是「結構同構」推導出的候選值，不是 production 預設：
#      pause / restore 的 guard 路徑與 status 在 timeout primitive 上同構
#      （require_single_screen → require_identity → 1 × ACT_TIMEOUT 10 s
#        → screen -X stuff），因此數字本身合理。
#    但 **failure consequence 不同**：控制動詞的 SSH_COMMAND_TIMEOUT 會產生
#    outcome UNKNOWN → 背負恢復責任 → 禁止重送。這個非對稱性使得
#    「借用 status 的實測」不足以升格 FINAL。
#    因此它們**不會**成為 SshTimeouts 的預設值，且永遠留在 missing() 裡，
#    直到未來合法的 FIRST LIVE / handoff 自然產生控制路徑 evidence。
STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC = {
    "pause": 12.0,
    "restore": 12.0,
}

TIMEOUT_FINAL = "FINAL"
TIMEOUT_STRUCTURAL_CANDIDATE = "STRUCTURAL_CANDIDATE"

SSH_TIMEOUT_STATUS = {
    "ssh_connect_timeout_sec": TIMEOUT_FINAL,
    "probe": TIMEOUT_FINAL,
    "status": TIMEOUT_FINAL,
    "loopcheck": TIMEOUT_FINAL,
    "pause": TIMEOUT_STRUCTURAL_CANDIDATE,
    "restore": TIMEOUT_STRUCTURAL_CANDIDATE,
}

# 🔴 現場 capability 驗證的改善方向（**只記錄，本輪不部署、不改 guard**）。
#    目前要確認 guard 是 B1 還是 B2，唯一的直接辦法是真的送 pause / restore
#    ——那在安全上不可接受。未來 guard 的唯讀 probe 可以自行回報能力：
#        guard_variant=B1|B2
#        capability_probe / capability_status / capability_loopcheck
#        capability_pause / capability_restore
#    如此即可在不觸碰控制動詞的前提下確認 capability。
FUTURE_GUARD_CAPABILITY_FIELDS = (
    "guard_variant", "capability_probe", "capability_status",
    "capability_loopcheck", "capability_pause", "capability_restore",
)

_UNSET = object()


class SshTimeouts(object):
    """
    SSH timeout 契約。**兩層分離，且 command timeout 支援 per-verb。**

    🔴 預設值 = C3 已核准的四項（connect / probe / status / loopcheck）。
       `pause` / `restore` 刻意**維持 None** —— 它們只有結構推導候選值，
       沒有實測，不得假裝已定案。
    🔴 未設定的動詞在 transport 直接回 SSH_NOT_CONFIGURED，
       **不會**退回任何 magic number。
    🔴 `SshTimeouts.unconfigured()` 取得全 None 版本，
       供「未設定 → Fail Closed」的回歸演練，這條路徑沒有被拿掉。
    """

    def __init__(self, connect_timeout_sec=_UNSET, command_timeouts=_UNSET):
        if connect_timeout_sec is _UNSET:
            connect_timeout_sec = APPROVED_SSH_CONNECT_TIMEOUT_SEC
        self.connect_timeout_sec = connect_timeout_sec

        base = {v: None for v in GUARD_ALLOWED_VERBS}
        if command_timeouts is _UNSET:
            # 只帶入已核准的三項；pause / restore 維持 None
            base.update(APPROVED_SSH_COMMAND_TIMEOUT_SEC)
        else:
            for k, v in dict(command_timeouts or {}).items():
                if k not in GUARD_ALLOWED_VERBS:
                    raise ValueError("未知的 guard 動詞：%r" % (k,))
                base[k] = v
        self.command_timeouts = base

    @classmethod
    def unconfigured(cls):
        """全部未設定 —— 用來演練 Fail Closed，不是 production 用法。"""
        return cls(connect_timeout_sec=None, command_timeouts={})

    @staticmethod
    def status_of(name):
        """該項是 FINAL 還是只有結構推導候選值。"""
        return SSH_TIMEOUT_STATUS.get(name)

    def command_timeout_for(self, verb):
        """該動詞的 client command timeout。None = UNRESOLVED。"""
        if verb not in GUARD_ALLOWED_VERBS:
            raise ValueError("未知的 guard 動詞：%r" % (verb,))
        return self.command_timeouts[verb]

    def configured_for(self, verb):
        """該動詞是否兩層 timeout 都齊全。"""
        return (self.connect_timeout_sec is not None
                and self.command_timeout_for(verb) is not None)

    def missing(self, verbs=None):
        """
        列出**尚未取得實測證據**的項目。

        🔴 `pause` / `restore` 只有結構推導候選值，因此即使有人把候選值
           注入進來，它們的 status 仍是 STRUCTURAL_CANDIDATE ——
           見 `not_field_verified()`。本方法只回答「值是不是 None」。
        """
        out = []
        if self.connect_timeout_sec is None:
            out.append("ssh_connect_timeout_sec")
        for v in (verbs or GUARD_ALLOWED_VERBS):
            if self.command_timeout_for(v) is None:
                out.append("ssh_command_timeout_sec[%s]" % v)
        return out

    def not_field_verified(self, verbs=None):
        """
        尚未有實測證據的項目（不論值是否已填）。

        🔴 這才是 live 前真正要看的清單 —— 填了結構候選值 **不等於**
           已驗證。pause / restore 在取得控制路徑 evidence 之前恆在此清單。
        """
        out = []
        for v in (verbs or GUARD_ALLOWED_VERBS):
            if SSH_TIMEOUT_STATUS.get(v) != TIMEOUT_FINAL:
                out.append(v)
        return out

    def covers_server_side_bound(self, verb):
        """
        該動詞的 command timeout 是否已涵蓋 guard 的結構執行窗。

        🔴 回 False **不代表**應該把 timeout 改成那個上界 —— 上界只是
           下限的證據來源，真正的值還需要實測 latency。
        """
        t = self.command_timeout_for(verb)
        bound = guard_server_side_bound_sec(verb)
        if t is None or bound is None:
            return None
        return float(t) >= float(bound)


class SshResult(object):
    """單次 remote verb 的結果。"""

    def __init__(self, verb, outcome, exit_code=None, stdout="", detail=None,
                 elapsed_sec=None):
        self.verb = verb
        self.outcome = outcome
        self.exit_code = exit_code
        self.stdout = stdout or ""
        self.detail = detail
        self.elapsed_sec = elapsed_sec

    @property
    def ok(self):
        return self.outcome == SSH_OK

    @property
    def timed_out(self):
        return self.outcome in (SSH_CONNECT_TIMEOUT, SSH_COMMAND_TIMEOUT)

    @property
    def uncertain(self):
        """遠端是否**可能已經執行**。command timeout 一律 True。"""
        return self.outcome in SSH_OUTCOME_UNCERTAIN

    def __repr__(self):
        return "<SshResult %s %s exit=%r>" % (self.verb, self.outcome,
                                              self.exit_code)


class SshTransport(object):
    """
    Remote guard 的 SSH transport。**本身不含任何硬編 timeout。**

    🔴 `timeouts` 與 `runner` 皆由呼叫端注入；缺任一即 SSH_NOT_CONFIGURED，
       runner 不會被呼叫 —— 因此離線測試不可能真的連線。
    🔴 控制動詞（pause / restore）另外要求 `armed=True`，否則不呼叫 runner。

    runner 契約（由呼叫端提供，本模組不實作）：
        runner(verb, connect_timeout_sec, command_timeout_sec)
            -> {"exit_code": int, "stdout": str}
            或 raise ConnectTimeout / CommandTimeout（見下方兩個例外類別）
    """

    def __init__(self, timeouts=None, runner=None, armed=False, clock=None):
        self.timeouts = timeouts
        self._runner = runner
        self.armed = bool(armed)
        self._clock = clock or (lambda: 0.0)
        self.calls = []              # 實際交給 runner 的動詞
        self.results = []

    def _record(self, res):
        self.results.append(res)
        return res

    def can_run(self, verb):
        """回傳 (ok, reason)。**任何不確定一律 False。**"""
        if verb not in GUARD_ALLOWED_VERBS:
            return False, "UNKNOWN_VERB"
        if self.timeouts is None:
            return False, "TIMEOUTS_NOT_CONFIGURED"
        if not self.timeouts.configured_for(verb):
            return False, "TIMEOUT_UNRESOLVED:%s" % (
                ",".join(self.timeouts.missing([verb])),)
        if self._runner is None:
            return False, "RUNNER_NOT_INJECTED"
        if verb in CONTROL_VERBS and not self.armed:
            return False, "NOT_ARMED"
        return True, None

    def run(self, verb):
        ok, why = self.can_run(verb)
        if not ok:
            return self._record(SshResult(verb, SSH_NOT_CONFIGURED, detail=why))

        ct = self.timeouts.connect_timeout_sec
        xt = self.timeouts.command_timeout_for(verb)
        t0 = self._clock()
        self.calls.append(verb)
        try:
            out = self._runner(verb, ct, xt) or {}
        except SshConnectTimeout as e:
            # 沒連上 → 遠端**必然沒有執行**
            return self._record(SshResult(verb, SSH_CONNECT_TIMEOUT,
                                          detail=str(e),
                                          elapsed_sec=self._clock() - t0))
        except SshCommandTimeout as e:
            # 🔴 已連上但沒等到結果 → **不得推論成沒有執行**
            return self._record(SshResult(verb, SSH_COMMAND_TIMEOUT,
                                          detail=str(e),
                                          elapsed_sec=self._clock() - t0))
        except SshAuthFailed as e:
            return self._record(SshResult(verb, SSH_AUTH_FAILED,
                                          detail=str(e),
                                          elapsed_sec=self._clock() - t0))
        except Exception as e:
            return self._record(SshResult(verb, SSH_ERROR,
                                          detail="%s: %s" % (type(e).__name__, e),
                                          elapsed_sec=self._clock() - t0))

        code = out.get("exit_code")
        elapsed = self._clock() - t0
        if code == GUARD_EXIT_OK:
            return self._record(SshResult(verb, SSH_OK, exit_code=code,
                                          stdout=out.get("stdout", ""),
                                          elapsed_sec=elapsed))
        if code == GUARD_EXIT_REFUSED:
            return self._record(SshResult(verb, SSH_REFUSED, exit_code=code,
                                          stdout=out.get("stdout", ""),
                                          elapsed_sec=elapsed))
        if code == GUARD_EXIT_TIMEOUT:
            # guard 自己的 $TIMEOUT 觸發 —— server 端明確回報動作沒完成，
            # 但注入式指令是否已被 screen 收下仍不可知 → 一樣視為不確定。
            return self._record(SshResult(verb, SSH_COMMAND_TIMEOUT,
                                          exit_code=code,
                                          stdout=out.get("stdout", ""),
                                          detail="guard ACT_TIMEOUT",
                                          elapsed_sec=elapsed))
        return self._record(SshResult(verb, SSH_ERROR, exit_code=code,
                                      stdout=out.get("stdout", ""),
                                      elapsed_sec=elapsed))


class SshConnectTimeout(Exception):
    """連線／認證階段逾時。遠端必然未執行。"""


class SshCommandTimeout(Exception):
    """連線已建立，但等不到指令結果。遠端**可能已執行**。"""


class SshAuthFailed(Exception):
    """身分被拒。遠端未執行。"""


# ---- C2-7. 控制動詞的 send outcome ------------------------------------
SEND_SENT = "SENT"            # 確定已送達並被接受
SEND_NOT_SENT = "NOT_SENT"    # 確定沒有送出
SEND_UNKNOWN = "UNKNOWN"      # 🔴 可能已執行，也可能沒有


def classify_send_outcome(result):
    """
    把 SshResult 轉成三值 send outcome。

    🔴 `SSH_COMMAND_TIMEOUT` **必須**是 UNKNOWN，不得歸類為 NOT_SENT ——
       遠端可能已經執行，只是 client 在收到結果前就放棄等待。
    """
    if result is None:
        return SEND_NOT_SENT
    if result.outcome == SSH_OK:
        return SEND_SENT
    if result.outcome in SSH_DEFINITELY_NOT_EXECUTED:
        return SEND_NOT_SENT
    if result.outcome in SSH_OUTCOME_UNCERTAIN:
        return SEND_UNKNOWN
    # SSH_ERROR：exit code 非預期，無法斷定 → 保守視為不確定
    return SEND_UNKNOWN


def must_probe_before_further_action(send_outcome):
    """UNKNOWN 之後只能先 probe / recover，**不得** blind resend。"""
    return send_outcome == SEND_UNKNOWN


def blind_resend_allowed(send_outcome):
    """
    任何情況都不允許「不先觀測就重送」。

    🔴 這個函式恆回 False —— 存在的目的是讓「不得 blind resend」變成
       可回歸驗證的契約，而不是散落在註解裡的口頭約定。
    """
    return False


def main(argv=None):
    print("=" * 72)
    print("  Phase 6.10-A Remote Adapter（離線自述）")
    print("=" * 72)
    print(f"  screen           : {SCREEN_NAME}")
    print(f"  期望行程          : {EXPECTED_CMD}  (cwd {EXPECTED_CWD})")
    print(f"  注入指令範例      : {build_stuff_command(CMD_STATUS)}")
    print(f"  guard 允許動詞    : {list(GUARD_ALLOWED_VERBS)}")
    print(f"  senders 預設武裝  : {RemoteSenders().armed}")
    print(f"  quiescent 檢查項  : {list(QUIESCENT_ITEMS)}")
    t = SshTimeouts()
    print(f"  SSH timeout 架構  : connect / command 分離，command 依動詞區分")
    print(f"  connect           : {t.connect_timeout_sec} "
          f"({SshTimeouts.status_of('ssh_connect_timeout_sec')})")
    for v in GUARD_ALLOWED_VERBS:
        print(f"    {v:<10} {str(t.command_timeout_for(v)):<6}"
              f" {SshTimeouts.status_of(v)}")
    print(f"  尚未取得實測值    : {t.missing()}")
    print(f"  尚未 field 驗證   : {t.not_field_verified()}")
    print(f"  結構推導候選      : {STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC}"
          f"（**不是**預設值）")
    print(f"  guard 結構上界    : "
          + ", ".join("%s=%s" % (v, guard_server_side_bound_sec(v))
                      for v in GUARD_ALLOWED_VERBS))
    print("  ⚠ 上界為 server-side 結構估算，**不是**實測 latency，"
          "不得直接當 timeout。")
    print("  ⚠ remote guard 為設計，本階段**未部署**、authorized_keys 未修改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
