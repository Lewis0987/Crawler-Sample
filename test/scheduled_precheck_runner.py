# -*- coding: utf-8 -*-
"""
scheduled_precheck_runner.py — 排程用的唯讀 Precheck 包裝器（一次性）
======================================================================
用途
    由 Windows Task Scheduler 在最近一個 OFF_PEAK 時段開始後呼叫一次，
    執行 `phase6_live_precheck.py`（**READ-ONLY**），把 stdout / stderr
    連同摘要欄位存成帶時間戳的 log。

🔴 本檔**結構上不可能送出任何控制指令**
    · 不 import 任何控制出口（operator / executor / verifier / 狀態機）
    · 不帶、也不接受 `--live-leg` / `--confirm` 之類的參數
    · 只以 subprocess 呼叫 phase6_live_precheck.py，且**不轉傳任何參數**
    · 不論 Precheck 結果如何，一律只寫檔後結束

🔴 **即使 Precheck 全數 PASS 也不會做任何事**
    第一次 LIVE 必須由人看過結果後另行明確授權。
    本檔沒有任何分支會因為 PASS 而觸發後續動作。

🔴 **一次性**：本檔不建立、不續期、不重排任何排程。
🔴 **失敗不重試**：任何錯誤只記錄，不重跑、不送任何 PCS 指令。

用法
    python scheduled_precheck_runner.py
"""
import os
import io
import sys
import json
import glob
import datetime
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "phase6_live_precheck.py")
EVIDENCE_DIR = os.path.join(os.path.dirname(HERE), "output", "phase6_live_charge")

# 🔴 唯一允許的參數組合。刻意寫死，不從 sys.argv 轉傳任何東西 ——
#    排程環境不該有機會把 --live-leg 之類的參數帶進來。
PRECHECK_ARGS = ["--meter-wait", "25", "--cycles", "6", "--cycle-interval", "10"]

SUMMARY_KEYS = (
    ("TOU", ("arb", "tou_state")),
    ("SOC", ("reading", "soc_percent")),
    ("PCS state", ("arb", "pcs_state")),
    ("Grid state", ("arb", "grid_state")),
    ("Authority", ("arb", "authority_state")),
    ("Natural Decision", ("arb", "fresh_decision_action")),
    ("Decision target kW", ("arb", "fresh_decision_target_kw")),
    ("Interlock", ("arb", "direction_reason")),
    ("Precheck ALL PASS", ("precheck_ok",)),
)


def _now():
    return datetime.datetime.now()


def _dig(d, path):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _newest_evidence(since):
    """取本次執行之後產生的最新 evidence JSON（沒有就回 None）。"""
    best, best_m = None, None
    for p in glob.glob(os.path.join(EVIDENCE_DIR, "precheck_*.json")):
        m = os.path.getmtime(p)
        if m + 1.0 < since:            # 容許 1 秒誤差
            continue
        if best_m is None or m > best_m:
            best, best_m = p, m
    return best


def main():
    started = _now()
    started_ts = started.timestamp()
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    log_path = os.path.join(
        EVIDENCE_DIR,
        f"scheduled_precheck_{started.strftime('%Y%m%d_%H%M%S')}.log")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([sys.executable, TARGET] + PRECHECK_ARGS,
                              cwd=HERE, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=900)
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        crashed = None
    except Exception as e:                     # noqa: BLE001 —— 只記錄，不重試
        rc, out, err = None, "", ""
        crashed = f"{type(e).__name__}: {e}"

    ended = _now()
    evidence = _newest_evidence(started_ts)
    data = {}
    if evidence:
        try:
            data = json.load(io.open(evidence, encoding="utf-8"))
        except Exception as e:                 # noqa: BLE001
            crashed = (crashed or "") + f" | evidence 讀取失敗 {type(e).__name__}"

    lines = []
    lines.append("=" * 72)
    lines.append("  排程唯讀 Precheck（一次性）—— 不會、也不可能送出任何 PCS 指令")
    lines.append("=" * 72)
    lines.append(f"  執行開始時間 : {started:%Y-%m-%d %H:%M:%S}")
    lines.append(f"  執行結束時間 : {ended:%Y-%m-%d %H:%M:%S}")
    lines.append(f"  耗時         : {(ended - started).total_seconds():.1f} s")
    lines.append(f"  Exit Code    : {rc}")
    if crashed:
        lines.append(f"  ⚠ 執行例外   : {crashed}（只記錄，不重試、不送任何指令）")
    lines.append("")
    for label, path in SUMMARY_KEYS:
        lines.append(f"  {label:<20}: {_dig(data, path)}")
    lines.append(f"  {'Evidence JSON':<20}: {evidence}")
    lines.append("")
    lines.append("  實機 CHARGE / DISCHARGE / STOP : 0 / 0 / 0")
    lines.append("  本次為 READ-ONLY —— 即使全數 PASS 亦不自動進入 LIVE。")
    lines.append("  第一次 LIVE 必須由人查看結果後另行明確授權。")
    lines.append("=" * 72)
    lines.append("")
    lines.append("---------------- Precheck stdout ----------------")
    lines.append(out.rstrip())
    if err.strip():
        lines.append("---------------- Precheck stderr ----------------")
        lines.append(err.rstrip())

    text = "\n".join(lines) + "\n"
    io.open(log_path, "w", encoding="utf-8").write(text)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                          # noqa: BLE001
        pass
    print(text)
    print(f"log: {log_path}")
    # 🔴 一律回 0：排程「有沒有跑起來」與「條件有沒有成立」是兩件事，
    #    條件未成立不是任務失敗。真正的判定一律看 log / evidence。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
