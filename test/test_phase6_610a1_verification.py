# -*- coding: utf-8 -*-
"""
test_phase6_610a1_verification.py — Phase 6.10-A1 驗證語意強化
======================================================================
核心命題
    「pause 驗證必須分兩階段：SETTLING 期間**允許**看到正常收斂
      （CHARGING→STANDBY、EXTERNAL→IDLE、active power→idle band），
      只有首次出現完整 candidate-idle 之後，才開始要求連續 N 筆全部成立。
      不得因為正常的收斂過程就把 pause 判定失敗。」

是否需要設備
    **不需要**。verifier 為純狀態機、clock 注入；guard allowlist 以本機
    bash 執行離線腳本驗證。零 SSH、零實機 command。

用法
    python test_phase6_610a1_verification.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA                 # noqa: E402

RESULTS = []
_TMP = []

# 測試用 idle band（**非 production 值**；production 必須由現場驗證後注入）
BAND_LO, BAND_HI = -2.5, 1.0
PUBKEY = "AAAAC3NzaC1lZDI1NTE5AAAAIDJUh72HpZWt9opR1OgBPL0AGiCFuufM+mwgc3OgAOAD"


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def _bash():
    """
    找出可用的 bash。

    ⚠️ 不能直接用 "bash" —— 在 Windows 上它可能解析到
       C:\\Windows\\System32\\bash.exe（WSL 轉接器），而該 WSL 環境
       未必安裝 /bin/bash，會得到 execvpe 失敗而非測試失敗。
       這是環境問題，不是 guard 的缺陷，因此明確指定 Git Bash 路徑。
    """
    import shutil
    for c in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files\Git\usr\bin\bash.exe",
              "/usr/bin/bash", "/bin/bash"):
        if os.path.exists(c):
            return c
    w = shutil.which("bash")
    return w


def probe_ok():
    return {"screen_alive": True, "process_alive": True,
            "process_identity_ok": True}


def smp(**over):
    s = {"pcs_state": "STANDBY", "ac_kw": -1.3, "authority": "IDLE",
         "comm_ok": True, "fault": False, "critical_alarms": 0, "fresh": True}
    s.update(over)
    return s


class Clk(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def verifier(stable=3, timeout=60.0, clk=None):
    return RA.PauseVerifier(BAND_LO, BAND_HI, stable_samples=stable,
                            settling_timeout_sec=timeout, clock=clk or Clk())


# ======================================================================
# 1. SETTLING 允許 CHARGING → STANDBY
# ======================================================================
def test_1():
    print("\n[1] SETTLING 允許 CHARGING → STANDBY 的正常收斂")
    v = verifier(stable=3)
    st = v.feed(smp(pcs_state="CHARGING", ac_kw=-80.0,
                    authority="EXTERNAL_OR_UNKNOWN"), probe_ok())
    check("★★ 第 1 筆仍在充電 → 維持 SETTLING（不判失敗）",
          st == RA.PV_SETTLING)
    st = v.feed(smp(pcs_state="CHARGING", ac_kw=-40.0,
                    authority="EXTERNAL_OR_UNKNOWN"), probe_ok())
    check("  收斂中（功率下降）→ 仍 SETTLING", st == RA.PV_SETTLING)
    st = v.feed(smp(), probe_ok())
    check("★★ 首次 candidate-idle → 進入 STABLE_VERIFY",
          st == RA.PV_STABLE_VERIFY)
    check("  此時尚未 quiescent", v.external_quiescent is False)
    v.feed(smp(), probe_ok())
    st = v.feed(smp(), probe_ok())
    check("★★ 連續 3 筆成立 → VERIFIED", st == RA.PV_VERIFIED)
    check("★★ 此時 external_quiescent 才為 True", v.external_quiescent is True)


# ======================================================================
# 2. SETTLING 允許 Authority external → IDLE
# ======================================================================
def test_2():
    print("\n[2] SETTLING 允許 Authority EXTERNAL_OR_UNKNOWN → IDLE")
    v = verifier(stable=2)
    st = v.feed(smp(authority="EXTERNAL_OR_UNKNOWN"), probe_ok())
    check("★★ Authority 仍為 EXTERNAL → SETTLING（不判失敗）",
          st == RA.PV_SETTLING)
    st = v.feed(smp(), probe_ok())
    check("轉為 IDLE → 進入 STABLE_VERIFY", st == RA.PV_STABLE_VERIFY)
    st = v.feed(smp(), probe_ok())
    check("連續 2 筆 → VERIFIED", st == RA.PV_VERIFIED)


# ======================================================================
# 3. settling timeout → FAIL
# ======================================================================
def test_3():
    print("\n[3] settling 逾時仍未 candidate-idle → FAIL")
    c = Clk()
    v = verifier(stable=2, timeout=30.0, clk=c)
    v.feed(smp(pcs_state="CHARGING", ac_kw=-80.0), probe_ok())
    c.t = 20.0
    check("20s < 30s → 仍 SETTLING",
          v.feed(smp(pcs_state="CHARGING", ac_kw=-80.0), probe_ok())
          == RA.PV_SETTLING)
    c.t = 31.0
    st = v.feed(smp(pcs_state="CHARGING", ac_kw=-80.0), probe_ok())
    check("★★ 逾時 → SETTLING_TIMEOUT", st == RA.PV_SETTLING_TIMEOUT)
    check("★★ 且 external_quiescent 為 False", v.external_quiescent is False)
    check("  已記錄原因", "逾時" in (v.reason or ""))
    check("★★ 終態不再改變（後續 sample 不得翻盤）",
          v.feed(smp(), probe_ok()) == RA.PV_SETTLING_TIMEOUT)
    for bad in (None, 0, -1):
        try:
            RA.PauseVerifier(BAND_LO, BAND_HI, 2, bad)
            ok = False
        except ValueError:
            ok = True
        check(f"  settling_timeout_sec={bad} 必須拒絕", ok)
    for bad in (None, 0):
        try:
            RA.PauseVerifier(BAND_LO, BAND_HI, bad, 30.0)
            ok = False
        except ValueError:
            ok = True
        check(f"  stable_samples={bad} 必須拒絕", ok)


# ======================================================================
# 4. stable window 內任 1 筆 external evidence → FAIL
# ======================================================================
def test_4():
    print("\n[4] stable window 內任一筆不成立 → FAIL")
    cases = {
        "authority_idle": smp(authority="EXTERNAL_OR_UNKNOWN"),
        "power_in_idle_band": smp(ac_kw=-80.0),
        "pcs_legal_idle": smp(pcs_state="CHARGING"),
        "comm_ok": smp(comm_ok=False),
        "fault_clear": smp(fault=True),
        "alarm_clear": smp(critical_alarms=1),
    }
    for item, bad in cases.items():
        v = verifier(stable=4)
        v.feed(smp(), probe_ok())          # 進入 STABLE_VERIFY
        v.feed(smp(), probe_ok())
        st = v.feed(bad, probe_ok())       # 第 3 筆壞掉
        check(f"★★ stable window 第 3 筆 {item} 不成立 → FAILED",
              st == RA.PV_FAILED)
        check(f"  external_quiescent=False", v.external_quiescent is False)
    # probe 面向
    v2 = verifier(stable=3)
    v2.feed(smp(), probe_ok())
    st2 = v2.feed(smp(), {"screen_alive": False, "process_alive": True,
                          "process_identity_ok": True})
    check("★★ stable window 內 screen 消失 → FAILED", st2 == RA.PV_FAILED)


# ======================================================================
# 5. stable window 內 PCS state 變動 → FAIL
# ======================================================================
def test_5():
    print("\n[5] stable window 內 PCS 狀態變動 → FAIL")
    v = verifier(stable=4)
    v.feed(smp(pcs_state="STANDBY"), probe_ok())
    v.feed(smp(pcs_state="STANDBY"), probe_ok())
    st = v.feed(smp(pcs_state="STOPPED"), probe_ok())
    check("★★ STANDBY → STOPPED 發生在 stable window 內 → FAILED",
          st == RA.PV_FAILED)
    check("  原因記錄了狀態變動", "狀態變動" in (v.reason or ""))
    # 但在 SETTLING 階段的狀態變化不算失敗
    v2 = verifier(stable=2)
    v2.feed(smp(pcs_state="CHARGING", ac_kw=-80.0), probe_ok())
    v2.feed(smp(pcs_state="STANDBY"), probe_ok())
    st2 = v2.feed(smp(pcs_state="STANDBY"), probe_ok())
    check("★★ 對照：SETTLING 期間 CHARGING→STANDBY 不算失敗 → VERIFIED",
          st2 == RA.PV_VERIFIED)


# ======================================================================
# 6. stable window 樣本不 fresh → Fail Closed
# ======================================================================
def test_6():
    print("\n[6] 樣本不 fresh → UNKNOWN_SOURCE（Fail Closed）")
    v = verifier(stable=3)
    v.feed(smp(), probe_ok())
    st = v.feed(smp(fresh=False), probe_ok())
    check("★★ stable window 內來源不 fresh → UNKNOWN_SOURCE",
          st == RA.PV_UNKNOWN)
    check("★★ 不得視為通過", v.external_quiescent is False)
    v2 = verifier(stable=2)
    check("SETTLING 期間不 fresh 同樣 UNKNOWN",
          v2.feed(smp(fresh=False), probe_ok()) == RA.PV_UNKNOWN)


# ======================================================================
# 7. idle band —— 包含 0 時 0 PASS
# ======================================================================
def test_7():
    print("\n[7] idle band 取代 baseline±tolerance")
    check("★★ band 含 0 → 0.0 PASS", RA.power_in_idle_band(0.0, -2.5, 1.0))
    check("★★ −1.3 亦 PASS", RA.power_in_idle_band(-1.3, -2.5, 1.0))
    check("−1.4 PASS", RA.power_in_idle_band(-1.4, -2.5, 1.0))
    check("★★ 帶外 −80 FAIL", not RA.power_in_idle_band(-80.0, -2.5, 1.0))
    check("★★ 帶外 80 FAIL", not RA.power_in_idle_band(80.0, -2.5, 1.0))
    check("邊界值視為在帶內（閉區間）",
          RA.power_in_idle_band(-2.5, -2.5, 1.0) and
          RA.power_in_idle_band(1.0, -2.5, 1.0))
    for a, b, c in ((None, -2.5, 1.0), (0.0, None, 1.0), (0.0, -2.5, None)):
        check("任一為 None → Fail Closed", not RA.power_in_idle_band(a, b, c))
    check("★★ lower > upper（設定錯誤）→ Fail Closed",
          not RA.power_in_idle_band(0.0, 1.0, -2.5))
    lo, hi = RA.band_from_baseline(-1.3, 1.25)
    # -1.3 - 1.25 = -2.55 ；-1.3 + 1.25 = **-0.05**（不是 +0.05）
    check(f"band_from_baseline 為底層工具（{lo:.2f}~{hi:.2f}）",
          abs(lo - (-2.55)) < 1e-9 and abs(hi - (-0.05)) < 1e-9)
    check("★★ 該 helper 導出的 band 不含 0 → 正說明不該當 production policy",
          not RA.power_in_idle_band(0.0, lo, hi))
    src = io.open(os.path.join(HERE, "phase6_remote_adapter.py"),
                  encoding="utf-8").read()
    check("★★ 模組明載 band 須由現場驗證後注入、不得由單次讀值推導",
          "不得由任何單次讀值推導" in src)


# ======================================================================
# 8. Restore 兩階段
# ======================================================================
def test_8():
    print("\n[8] Restore 兩階段 —— 不強迫 PCS 出力")
    c = Clk()
    rv = RA.RestoreVerifier(min_loop_observations=2, settling_timeout_sec=30.0,
                            clock=c)
    p = RA.parse_probe("1", "1140 python3 auto_control.py",
                       cwd_line="/home/etica/ems")
    check("初始為 RESTORE_SENT", rv.state == RA.RV_SENT)
    st = rv.feed(p, "2026-09-01 13:08:44")
    check("第 1 筆 loop 觀測 → RUNTIME_SETTLING", st == RA.RV_SETTLING)
    st = rv.feed(p, "2026-09-01 13:08:47")
    check("★★ 第 2 筆時間戳前進 → RESUMED（未要求 PCS 出力）",
          st == RA.RV_RESUMED)

    # loop 未前進 → FAILED
    rv2 = RA.RestoreVerifier(2, 30.0, clock=Clk())
    rv2.feed(p, "2026-09-01 13:08:44")
    check("★★ 兩筆時間戳相同 → FAILED",
          rv2.feed(p, "2026-09-01 13:08:44") == RA.RV_FAILED)

    # runtime 不健康且逾時 → FAILED
    c3 = Clk()
    rv3 = RA.RestoreVerifier(2, 10.0, clock=c3)
    dead = RA.parse_probe("0", "")
    check("runtime 未健康 → SETTLING", rv3.feed(dead, None) == RA.RV_SETTLING)
    c3.t = 11.0
    check("★★ 逾時仍未健康 → FAILED", rv3.feed(dead, None) == RA.RV_FAILED)

    for bad in (None, 1, 0):
        try:
            RA.RestoreVerifier(bad, 10.0)
            ok = False
        except ValueError:
            ok = True
        check(f"★★ min_loop_observations={bad} 必須拒絕（單點無法證明前進）", ok)


# ======================================================================
# 9. Restore loop 證據不足 → UNKNOWN
# ======================================================================
def test_9():
    print("\n[9] runtime 健康但 loop 證據不足 → NEEDS_ADDITIONAL_PROBE")
    c = Clk()
    rv = RA.RestoreVerifier(min_loop_observations=3, settling_timeout_sec=20.0,
                            clock=c)
    p = RA.parse_probe("1", "1140 python3 auto_control.py",
                       cwd_line="/home/etica/ems")
    rv.feed(p, "2026-09-01 13:08:44")
    rv.feed(p, None)                       # 取不到 loop 證據
    c.t = 21.0
    st = rv.feed(p, None)
    check("★★ runtime 健康但觀測不足 → NEEDS_ADDITIONAL_PROBE", st == RA.RV_UNKNOWN)
    check("★★ 明確不是 RESUMED", st != RA.RV_RESUMED)
    check("  原因說明缺幾筆", "loop 觀測僅" in (rv.reason or ""))


# ======================================================================
# 10. authorized_keys：REPLACE not APPEND
# ======================================================================
def test_10():
    print("\n[10] authorized_keys —— 必須 REPLACE，不得 APPEND")
    good = [f'command="/home/etica/ems/phase6_remote_guard.sh",restrict,'
            f'from="192.168.128.234" ssh-ed25519 {PUBKEY} phase6-firstlive-2026-09']
    ok, probs = RA.authorized_keys_ok(good, PUBKEY)
    check("★★ 唯一條目且選項齊全 → PASS", ok and not probs)

    dup = good + [f'restrict,from="192.168.128.234" ssh-ed25519 {PUBKEY} old-entry']
    ok2, probs2 = RA.authorized_keys_ok(dup, PUBKEY)
    check("★★ 同一 public key 出現 2 次 → FAIL（舊的無 forced-command 條目仍在）",
          not ok2)
    check(f"  問題明確指出必須 REPLACE（{probs2[0][:30]}…）",
          "REPLACE" in probs2[0])

    for missing in ('command="/home/etica/ems/phase6_remote_guard.sh"',
                    "restrict", 'from="192.168.128.234"'):
        entry = good[0].replace(missing + ",", "").replace("," + missing, "")
        entry = entry.replace(missing + " ", "")
        ok3, probs3 = RA.authorized_keys_ok([entry], PUBKEY)
        check(f"★★ 缺少 {missing[:28]}… → FAIL", not ok3)

    ok4, probs4 = RA.authorized_keys_ok([], PUBKEY)
    check("找不到金鑰 → FAIL", not ok4)
    commented = ["# " + good[0]]
    ok5, _ = RA.authorized_keys_ok(commented, PUBKEY)
    check("★★ 只存在於註解行 → 視為找不到", not ok5)


# ======================================================================
# 11/12. Remote guard —— 以本機 bash 實際執行 allowlist
# ======================================================================
def test_11_12():
    print("\n[11/12] Remote guard —— 本機 bash 實測 allowlist（離線）")
    sh = RA.REMOTE_GUARD_SH
    # 不能整檔 grep —— wrapper 自己的禁止性註解就寫著
    # 「never uses eval; never passes the original command to sh -c」。
    # 必須先剝除註解行，只檢查實際會執行的程式碼。
    code = "\n".join(ln for ln in sh.splitlines()
                     if not ln.lstrip().startswith("#"))
    for token, why in (("eval", "不得使用 eval"),
                       ("sh -c", "不得把原始 command 交給 sh -c"),
                       ("$SSH_ORIGINAL_COMMAND\"", "不得直接展開執行")):
        check(f"★★ wrapper {why}（僅檢查程式碼行）", token not in code)
    check("★★ 使用 set -euo pipefail", "set -euo pipefail" in sh)
    check("★★ PATH 已淨化", "PATH=/usr/bin:/bin" in sh)
    check("★★ 危險環境變數已 unset", "unset BASH_ENV" in sh)
    check("★★ 每個動作皆有 timeout", "/usr/bin/timeout" in sh and "ACT_TIMEOUT" in sh)
    check("★★ 使用絕對路徑", "SCREEN=/usr/bin/screen" in sh)
    check("★★ exit code 明確定義",
          all(str(c) in sh for c in (RA.GUARD_EXIT_TIMEOUT,
                                     RA.GUARD_EXIT_REFUSED,
                                     RA.GUARD_EXIT_MISSING)))
    check("★★ caller 不得提供 screen name（寫死在 wrapper 內）",
          "SCREEN_NAME=auto" in sh)

    # 實際執行
    d = tempfile.mkdtemp(prefix="p610a1_")
    _TMP.append(d)
    path = os.path.join(d, "guard.sh")
    body = re.sub(r"^(SCREEN|PGREP|GREP|HEAD|TAIL|READLINK|SLEEP|TIMEOUT)="
                  r"/usr/bin/\w+$",
                  lambda m: m.group(1) + "=/usr/bin/echo", sh, flags=re.M)
    io.open(path, "w", encoding="utf-8", newline="\n").write(body)
    cases = [("probe", 0), ("loopcheck", 0), ("status", 0), ("pause", 0),
             ("restore", 0), ("hostname", RA.GUARD_EXIT_REFUSED),
             ("kill 1140", RA.GUARD_EXIT_REFUSED),
             ("pause; rm -rf /", RA.GUARD_EXIT_REFUSED),
             ("PAUSE", RA.GUARD_EXIT_REFUSED),
             (" pause", RA.GUARD_EXIT_REFUSED),
             ("probe extra", RA.GUARD_EXIT_REFUSED),
             ("screen -X quit", RA.GUARD_EXIT_REFUSED),
             ("restore;start", RA.GUARD_EXIT_REFUSED),
             ("__MISSING__", RA.GUARD_EXIT_MISSING)]
    for verb, want in cases:
        env = dict(os.environ)
        if verb == "__MISSING__":
            env.pop("SSH_ORIGINAL_COMMAND", None)
        else:
            env["SSH_ORIGINAL_COMMAND"] = verb
        r = subprocess.run([_bash(), path], env=env, capture_output=True,
                           text=True)
        label = "無 SSH_ORIGINAL_COMMAND" if verb == "__MISSING__" else repr(verb)
        check(f"  {label} → exit {want}", r.returncode == want)


# ======================================================================
# 13. Phase 6.10-B1 deployed guard —— 只放行唯讀動詞
# ======================================================================
def test_13():
    print("\n[13] B1 deployed guard —— pause / restore 不在部署 allowlist")
    check("B1 允許動詞恰為三個唯讀動詞",
          RA.GUARD_B1_ALLOWED_VERBS == ("probe", "loopcheck", "status"))
    for v in ("probe", "loopcheck", "status"):
        check(f"  允許 {v}", RA.guard_b1_would_accept(v))
    for v in ("pause", "restore"):
        check(f"★★ B1 拒絕 {v}（實機控制能力不在此階段暴露）",
              not RA.guard_b1_would_accept(v))
    sh = RA.REMOTE_GUARD_B1_SH
    code = "\n".join(ln for ln in sh.splitlines()
                     if not ln.lstrip().startswith("#"))
    check("★★ B1 腳本的 case 分支不含 pause)/restore)",
          "  pause)" not in code and "  restore)" not in code)
    check("★★ 以 session name 定位，不用寫死 PID",
          "SCREEN_NAME=auto" in code and "1128" not in code)
    check("★★ 要求 .auto 恰好 1 個（0 或 >1 皆 Fail Closed）",
          "require_single_screen" in code and "need exactly 1" in sh)
    check("★★ 身分鏈驗證（cwd + cmd）", "require_identity" in code
          and "EXPECTED_CWD" in code and "EXPECTED_CMD" in code)
    check("★★ hardcopy 使用 mktemp（非固定檔名）",
          "MKTEMP" in code and "/tmp/p6guard.XXXXXXXX" in code)
    check("★★ 有 trap 清理", "trap " in code)
    check("★★ 固定 /tmp/p6_guard.txt 已不再使用",
          "/tmp/p6_guard.txt" not in code)
    check("★★ 新增 AMBIGUOUS / IDENTITY exit code",
          RA.GUARD_EXIT_AMBIGUOUS == 44 and RA.GUARD_EXIT_IDENTITY == 45
          and "EX_AMBIGUOUS=44" in code and "EX_IDENTITY=45" in code)
    check("沿用 A1 強化：set -euo pipefail / PATH 淨化 / timeout",
          "set -euo pipefail" in code and "PATH=/usr/bin:/bin" in code
          and "TIMEOUT" in code)
    for token in ("eval", "sh -c"):
        check(f"★★ 不使用 {token}", token not in code)


def main():
    print("=" * 72)
    print("  Phase 6.10-A1 Pause/Restore Verification Semantics（完全離線）")
    print("=" * 72)
    try:
        for fn in (test_1, test_2, test_3, test_4, test_5, test_6,
                   test_7, test_8, test_9, test_10, test_11_12, test_13):
            fn()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    total, bad = len(RESULTS), RESULTS.count(False)
    print("\n" + "=" * 72)
    print(f"  結果：{total - bad}/{total} PASS" + ("" if not bad else f"   ❌ {bad} FAIL"))
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
