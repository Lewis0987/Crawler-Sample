# -*- coding: utf-8 -*-
"""
phase6_guard_ssh_runner.py — B2 guard 專用 SSH runner（**只跑允許動詞**）
======================================================================
唯一職責：把 `SshTransport` 需要的 runner 契約實作出來，
並且**結構上只可能執行 B2 guard 的允許動詞**。

    runner(verb, connect_timeout_sec, command_timeout_sec)
        -> {"exit_code": int, "stdout": str, ...}
        或 raise SshConnectTimeout / SshCommandTimeout

🔴 **硬性 allowlist**：`verb` 不在 `phase6_remote_adapter.GUARD_ALLOWED_VERBS`
   內一律 `ArbitraryCommandRefused` —— 不組 shell、不 `sh -c`、不接受
   任意字串、不接受多個參數。遠端一律是 forced-command guard，
   本檔送出的 argv 最後一段只會是那五個動詞其中之一。

🔴 **不得**在此新增任何 policy / 判定 / retry / fallback。
   逾時分類沿用既有 `SshConnectTimeout` / `SshCommandTimeout` 語意：
       尚未認證就逾時 -> connect timeout（遠端**必然沒執行**）
       已認證後逾時   -> command timeout（**不得**推論成沒執行）

🔴 credential：只用既有 dedicated key，**不 fallback** 到一般 SSH / 其他 key /
   password auth；key 不存在即 `CredentialMissing`（Fail Closed）。

用法
    python phase6_guard_ssh_runner.py      # 離線自述；不連線
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_AUTH_MARK = re.compile(r"^Authenticated to |^debug1: Authenticated to ")


class ArbitraryCommandRefused(RuntimeError):
    """非允許動詞 —— 結構上禁止。"""


class CredentialMissing(RuntimeError):
    """dedicated key 不存在 —— Fail Closed，不 fallback。"""


def allowed_verbs():
    import phase6_remote_adapter as RA
    return tuple(RA.GUARD_ALLOWED_VERBS)


def default_identity():
    """既有 dedicated key / user / host。**不硬編 secret 內容。**"""
    import phase6_ssh_timing_probe as PROBE
    return {"key_path": PROBE.KEY, "user": PROBE.USER, "host": PROBE.HOST}


def check_credentials(identity=None):
    """回傳 (ok, checks)。**純本機檔案存在性檢查，不連線。**"""
    ident = identity or default_identity()
    key = ident.get("key_path")
    checks = {
        "key_path_configured": bool(key),
        "key_file_exists": bool(key) and os.path.isfile(os.path.expanduser(key)),
        "user_configured": bool(ident.get("user")),
        "host_configured": bool(ident.get("host")),
    }
    return all(checks.values()), checks


class GuardSshRunner(object):
    """
    可注入 `RA.SshTransport(runner=...)` 的 runner。

    🔴 `spawn` 由呼叫端注入時可完全離線測試；未注入才使用 `subprocess.Popen`。
    """

    def __init__(self, identity=None, spawn=None, clock=None):
        self.identity = identity or default_identity()
        self._spawn = spawn                      # 測試可注入；None = 真實 ssh
        self._clock = clock or time.monotonic
        self.calls = []

    def argv(self, verb, connect_timeout_sec):
        """
        組出 argv。🔴 `verb` 已在 `__call__` 通過 allowlist；
        這裡**不做字串拼接**、不經 shell、最後一段只放單一動詞。
        """
        ident = self.identity
        return ["ssh", "-v", "-i", os.path.expanduser(ident["key_path"]),
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=%d" % int(connect_timeout_sec),
                "%s@%s" % (ident["user"], ident["host"]), verb]

    def __call__(self, verb, connect_timeout_sec, command_timeout_sec):
        import phase6_remote_adapter as RA
        if verb not in RA.GUARD_ALLOWED_VERBS:
            raise ArbitraryCommandRefused(
                "非 B2 guard 允許動詞，拒絕執行：%r" % (verb,))
        ok, checks = check_credentials(self.identity)
        if not ok:
            raise CredentialMissing(
                "dedicated key / identity 不完整：%s"
                % [k for k, v in checks.items() if not v])
        argv = self.argv(verb, connect_timeout_sec)
        self.calls.append(verb)
        if self._spawn is not None:
            return self._spawn(argv, connect_timeout_sec, command_timeout_sec)
        return self._run_real(argv, connect_timeout_sec, command_timeout_sec)

    def _run_real(self, argv, connect_timeout_sec, command_timeout_sec):
        import phase6_remote_adapter as RA
        t0 = self._clock()
        auth_at = None
        lines = []
        p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1,
                             stdin=subprocess.DEVNULL)
        deadline = t0 + float(connect_timeout_sec)
        try:
            for line in p.stdout:
                now = self._clock()
                ln = line.rstrip("\n")
                if auth_at is None and _AUTH_MARK.search(ln):
                    auth_at = now - t0
                if not ln.startswith("debug") and not _AUTH_MARK.search(ln):
                    lines.append(ln)
                if auth_at is None and now > deadline:
                    p.kill()
                    raise RA.SshConnectTimeout(
                        "connect 超過 %.1fs 仍未完成認證" % connect_timeout_sec)
                if (auth_at is not None
                        and (now - t0 - auth_at) > float(command_timeout_sec)):
                    p.kill()
                    raise RA.SshCommandTimeout(
                        "command 超過 %.1fs 仍未回傳" % command_timeout_sec)
            rc = p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            raise RA.SshCommandTimeout("等待行程結束逾時")
        return {"exit_code": rc, "stdout": "\n".join(lines).strip(),
                "connect_sec": auth_at, "total_sec": self._clock() - t0}


def main(argv=None):
    print("=" * 72)
    print("  Phase 6.10 — B2 guard SSH runner（離線自述）")
    print("=" * 72)
    print("  允許動詞  : %s" % (list(allowed_verbs()),))
    ok, checks = check_credentials()
    print("  credential: ok=%r %r" % (ok, checks))
    print("  [!] 非允許動詞一律 ArbitraryCommandRefused；不經 shell、不 fallback。")
    print("  [!] 本自述不連線、不執行任何遠端指令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
