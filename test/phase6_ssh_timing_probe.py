# -*- coding: utf-8 -*-
"""
phase6_ssh_timing_probe.py — Phase 6.10-C3 READ-ONLY SSH Timing Evidence
======================================================================
只做一件事：對已部署的 B1 Remote Guard 送出**唯讀動詞**，量測

    A. connect_sec   TCP 建立 → 認證完成（以 ssh -v 的即時 stderr 標記切分）
    B. command_sec   認證完成 → 行程結束（remote verb 實際執行時間）
    C. total_sec     行程啟動 → 行程結束

🔴 **本檔結構上不可能送出控制指令。**
    `ALLOWED_VERBS` 是硬性白名單，且明確把 pause / restore 列入
    `FORBIDDEN_VERBS`；任何不在白名單內的動詞一律 raise，不會進到
    subprocess。這不是註解約定，是 `_check_verb()` 的實作。

🔴 **不保存任何祕密。** 只記錄 timestamp / verb / 三段延遲 / exit_code /
    classification。ssh 的 -v 原始輸出**不落盤**，只即時抽取兩個標記行。

🔴 量測用的 ConnectTimeout / 行程上限是 **measurement fixture**，
    刻意取得遠大於預期值，**不是** production 候選值。

用法（唯讀）
    python phase6_ssh_timing_probe.py --precheck
    python phase6_ssh_timing_probe.py --probe 40 --status 30 --loopcheck 25
"""
import io
import os
import re
import sys
import json
import time
import argparse
import datetime
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA          # noqa: E402

# ======================================================================
# 硬性白名單
# ======================================================================
ALLOWED_VERBS = ("probe", "status", "loopcheck")
# 🔴 本輪授權明文：pause / restore = DO NOT CALL
FORBIDDEN_VERBS = ("pause", "restore", "stop", "start", "quit", "kill")

HOST = "192.168.70.201"
USER = "etica"
KEY = os.path.expanduser("~/.ssh/phase6_firstlive_ed25519")

# 🔴 measurement fixture，不是候選值 —— 取得遠大於任何預期延遲，
#    目的只是避免量測腳本無限期卡住。
FIXTURE_CONNECT_TIMEOUT_SEC = 20
FIXTURE_PROCESS_LIMIT_SEC = 120

MARK_TCP = "Connection established."
MARK_AUTH = re.compile(r"^Authenticated to |^debug1: Authenticated to ")

OUTDIR = os.path.join(os.path.dirname(HERE), "output", "phase6_ssh_timing")


def _check_verb(verb):
    if verb in FORBIDDEN_VERBS:
        raise RuntimeError("本輪未授權的動詞：%r（pause / restore = DO NOT CALL）"
                           % (verb,))
    if verb not in ALLOWED_VERBS:
        raise RuntimeError("不在唯讀白名單內的動詞：%r" % (verb,))
    return verb


def _classify(rc, stdout, tcp_at, auth_at, timed_out):
    """對齊 Phase 6.10-C2 taxonomy（本輪不修改語意，只套用）。"""
    if timed_out:
        if auth_at is None:
            return RA.SSH_CONNECT_TIMEOUT
        return RA.SSH_COMMAND_TIMEOUT
    if rc == RA.GUARD_EXIT_OK:
        return RA.SSH_OK
    if rc == RA.GUARD_EXIT_REFUSED:
        return RA.SSH_REFUSED
    if rc == RA.GUARD_EXIT_TIMEOUT:
        return RA.SSH_COMMAND_TIMEOUT
    if auth_at is None:
        # 沒走到認證完成
        if "Permission denied" in stdout or rc == 255:
            return RA.SSH_AUTH_FAILED
        return RA.SSH_CONNECT_TIMEOUT
    return RA.SSH_ERROR


def measure(verb):
    """
    送出一次唯讀動詞並回傳一筆 timing 記錄。

    connect_sec 以 ssh -v 即時輸出的「Authenticated to」為界；
    command_sec = total_sec − connect_sec。
    """
    _check_verb(verb)
    argv = ["ssh", "-v", "-i", KEY,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=%d" % FIXTURE_CONNECT_TIMEOUT_SEC,
            "%s@%s" % (USER, HOST), verb]

    t0 = time.monotonic()
    wall = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    tcp_at = auth_at = None
    stdout_lines = []
    timed_out = False

    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace",
                         bufsize=1)
    try:
        for line in p.stdout:
            now = time.monotonic()
            ln = line.rstrip("\n")
            if tcp_at is None and MARK_TCP in ln:
                tcp_at = now - t0
            elif auth_at is None and MARK_AUTH.search(ln):
                auth_at = now - t0
            # 🔴 只保留 guard 的實際輸出，debug 行不落盤（避免帶出路徑等雜訊）
            if not ln.startswith("debug") and not MARK_AUTH.search(ln):
                stdout_lines.append(ln)
            if now - t0 > FIXTURE_PROCESS_LIMIT_SEC:
                timed_out = True
                p.kill()
                break
        rc = p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        timed_out = True
        p.kill()
        rc = None
    total = time.monotonic() - t0

    out = "\n".join(stdout_lines).strip()
    cls = _classify(rc, out, tcp_at, auth_at, timed_out)
    return {
        "at": wall,
        "verb": verb,
        "tcp_sec": (round(tcp_at, 4) if tcp_at is not None else None),
        "connect_sec": (round(auth_at, 4) if auth_at is not None else None),
        "command_sec": (round(total - auth_at, 4)
                        if auth_at is not None else None),
        "total_sec": round(total, 4),
        "exit_code": rc,
        "classification": cls,
        "stdout_lines": len(stdout_lines),
    }


# ======================================================================
def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)


def summarize(rows, key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "min": round(min(vals), 4),
            "p50": pct(vals, 50), "p90": pct(vals, 90),
            "p95": pct(vals, 95), "p99": pct(vals, 99),
            "max": round(max(vals), 4),
            "mean": round(sum(vals) / len(vals), 4)}


def precheck():
    print("=" * 72)
    print("  Phase 6.10-C3 PRECHECK（唯讀）")
    print("=" * 72)
    print(f"  本機宣告的 checkpoint  : DEPLOYED_GUARD_VARIANT ="
          f" {RA.DEPLOYED_GUARD_VARIANT}")
    print(f"  B1 allowlist           : {list(RA.GUARD_B1_ALLOWED_VERBS)}")
    print(f"  本輪授權動詞           : {list(ALLOWED_VERBS)}")
    print(f"  本輪禁止動詞           : {list(FORBIDDEN_VERBS)}")
    r = measure("probe")
    print(f"\n  probe classification   : {r['classification']}"
          f"  exit={r['exit_code']}")
    print(f"  connect={r['connect_sec']}s  command={r['command_sec']}s"
          f"  total={r['total_sec']}s")
    return r


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--precheck", action="store_true")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--probe", type=int, default=0)
    ap.add_argument("--status", type=int, default=0)
    ap.add_argument("--loopcheck", type=int, default=0)
    a = ap.parse_args(argv)

    if a.precheck:
        precheck()
        return 0

    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTDIR, "ssh_timing_%s.jsonl" % stamp)

    plan = [("probe", a.probe), ("status", a.status), ("loopcheck", a.loopcheck)]
    rows = []
    with io.open(path, "w", encoding="utf-8") as f:
        for verb, n in plan:
            if n <= 0:
                continue
            _check_verb(verb)
            print(f"\n[{verb}] warm-up {a.warmup} + 量測 {n}")
            for i in range(a.warmup):
                w = measure(verb)
                w["warmup"] = True
                f.write(json.dumps(w, ensure_ascii=False) + "\n")
                print(f"   warm-up {i + 1}: {w['classification']}"
                      f" total={w['total_sec']}s")
            for i in range(n):
                r = measure(verb)
                r["warmup"] = False
                rows.append(r)
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                if (i + 1) % 10 == 0 or i == 0:
                    print(f"   {i + 1}/{n}  {r['classification']}"
                          f"  connect={r['connect_sec']}"
                          f"  command={r['command_sec']}")

    print("\n" + "=" * 72)
    print(f"  evidence: {path}")
    print("=" * 72)
    for verb, _ in plan:
        vr = [r for r in rows if r["verb"] == verb]
        if not vr:
            continue
        ok = [r for r in vr if r["classification"] == RA.SSH_OK]
        print(f"\n[{verb}] n={len(vr)}  OK={len(ok)}"
              f"  失敗={len(vr) - len(ok)}")
        for k in ("tcp_sec", "connect_sec", "command_sec", "total_sec"):
            print(f"   {k:<12} {summarize(ok, k)}")
        bad = {}
        for r in vr:
            if r["classification"] != RA.SSH_OK:
                bad[r["classification"]] = bad.get(r["classification"], 0) + 1
        if bad:
            print(f"   失敗分類     {bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
