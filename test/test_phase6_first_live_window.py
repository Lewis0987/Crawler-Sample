# -*- coding: utf-8 -*-
"""
test_phase6_first_live_window.py — FIRST LIVE 絕對觀測窗口的時間數學
======================================================================
核心命題
    「Phase A 的結束時間是**絕對牆鐘 00:10:00**，
      不是 runner_start + 10 分鐘，也不是 bootstrap 時間 + 10 分鐘。
      晚啟動不得以順延補足。」

為什麼要單獨測
    600 秒和 660 秒只差一個「sleep 之後有沒有重新取時間」。
    這種錯誤在實機上會安靜地把觀測窗口整段位移，卻不會拋任何例外 ——
    只能用注入時鐘在離線環境逐項證明。

是否需要設備
    **不需要**。純函式 + mock clock，零網路、零實機 command。

用法
    python test_phase6_first_live_window.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_first_live_runner as R                 # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def dt(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


W_START = "2026-09-01 00:00:00"
W_SEC = 600.0
W_END = dt("2026-09-01 00:10:00")


# ======================================================================
# Case A：23:59 bootstrap → 窗口必須恰為 600 s、結束於絕對 00:10:00
# ======================================================================
def case_a():
    print("\n[Case A] bootstrap 23:59:00 → Phase A 必須是 600 s / 絕對 00:10:00")
    boot = dt("2026-08-31 23:59:00")
    w0, w1, wait, missed = R.plan_window(W_START, W_SEC, boot)

    check("窗口起點 = 2026-09-01 00:00:00", w0 == dt(W_START))
    check("窗口終點 = 2026-09-01 00:10:00", w1 == W_END)
    check("未判定為錯過", missed is None)
    check(f"等待秒數 = 60（實得 {wait}）", wait == 60.0)

    # sleep 精準返回 → 等待後重新取時間
    now2 = boot + datetime.timedelta(seconds=wait)
    check("等待後牆鐘 = 00:00:00", now2 == dt(W_START))
    remaining, missed2 = R.window_remaining(w1, now2)
    check("未判定為錯過（remaining > 0）", missed2 is None)
    check(f"★★ Phase A 可用秒數 = 600，**不是** 660（實得 {remaining}）",
          remaining == 600.0)
    check("★★ 絕對結束時間 = 00:10:00",
          now2 + datetime.timedelta(seconds=remaining) == W_END)

    # 🔴 對照組：若誤用 bootstrap 當時的舊時間，就會得到 660（此為錯誤行為）
    wrong, _ = R.window_remaining(w1, boot)
    check(f"對照：用 bootstrap 舊時間會得到 660（實得 {wrong}）→ 證明兩者不同",
          wrong == 660.0 and wrong != remaining)


# ======================================================================
# Case B：00:00:01 才啟動 → MISSED_OBSERVATION_WINDOW
# ======================================================================
def case_b():
    print("\n[Case B] bootstrap 00:00:01（晚 1 秒）→ MISSED_OBSERVATION_WINDOW")
    late = dt("2026-09-01 00:00:01")
    w0, w1, wait, missed = R.plan_window(W_START, W_SEC, late)
    check("★★ 判定為 MISSED_OBSERVATION_WINDOW",
          missed == R.A_WINDOW_MISSED)
    check("等待秒數為 0（不會進入等待）", wait == 0.0)
    check("晚 1 秒即算錯過（無任何 tolerance）",
          (late - w0).total_seconds() == 1.0 and missed is not None)

    print("        其他晚啟動情境：")
    for tag, t in (("晚 3 分鐘（Task 00:03 才啟動）", "2026-09-01 00:03:00"),
                   ("晚 6 分鐘（StartWhenAvailable 補跑）", "2026-09-01 00:06:00"),
                   ("窗口內但已過起點", "2026-09-01 00:09:59"),
                   ("窗口已完全結束", "2026-09-01 00:10:01")):
        _w0, _w1, _wait, m = R.plan_window(W_START, W_SEC, dt(t))
        check(f"  {tag} → MISSED", m == R.A_WINDOW_MISSED)

    print("        🔴 不得以 00:03~00:13 取代 00:00~00:10：")
    m3 = R.plan_window(W_START, W_SEC, dt("2026-09-01 00:03:00"))[3]
    check("  00:03 啟動不會產生任何可用窗口，只會 ABORT",
          m3 == R.A_WINDOW_MISSED)


# ======================================================================
# Case C：等待期間時鐘跳動 / sleep 超時 → remaining <= 0 也算錯過
# ======================================================================
def case_c():
    print("\n[Case C] 等待後窗口已關閉（時鐘跳動 / sleep 超時）")
    _w0, w1, _wait, _m = R.plan_window(W_START, W_SEC, dt("2026-08-31 23:59:00"))
    for tag, t in (("等待超時到 00:10:00（剛好關閉）", "2026-09-01 00:10:00"),
                   ("等待超時到 00:12:00", "2026-09-01 00:12:00"),
                   ("系統暫停後在 01:30 才醒", "2026-09-01 01:30:00")):
        remaining, m = R.window_remaining(w1, dt(t))
        check(f"  {tag} → MISSED（remaining {remaining:.0f}s）",
              m == R.A_WINDOW_MISSED and remaining <= 0)
    # 邊界：還剩 1 秒仍算可用
    r1, m1 = R.window_remaining(w1, dt("2026-09-01 00:09:59"))
    check("  剩 1 秒仍算可用（不多不少）", m1 is None and r1 == 1.0)


# ======================================================================
# Case D：程式結構稽核 —— deadline 必須用等待後的時間
# ======================================================================
def case_d():
    print("\n[Case D] 結構稽核：deadline 不得由 bootstrap 舊時間推導")
    src = io.open(os.path.join(HERE, "phase6_first_live_runner.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    # 取得 main() 內對 window_remaining 的呼叫，確認第 2 個引數不是 `now`
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Name) and c.func.id == "window_remaining"]
    check("main() 有呼叫 window_remaining", len(calls) == 1)
    if calls:
        arg2 = calls[0].args[1]
        name = arg2.id if isinstance(arg2, ast.Name) else "<non-name>"
        check(f"★★ 第 2 引數不是 bootstrap 的 now（實為 {name!r}）",
              name != "now")
    check("原始碼不再直接以 (w1 - now) 推導 deadline",
          "(w1 - now)." not in src)
    check("plan_window / window_remaining 皆為零 I/O（不 import 任何模組）",
          not any(isinstance(n, (ast.Import, ast.ImportFrom))
                  for f in ast.walk(tree)
                  if isinstance(f, ast.FunctionDef)
                  and f.name in ("plan_window", "window_remaining")
                  for n in ast.walk(f)))


def main():
    print("=" * 70)
    print("  FIRST LIVE 絕對觀測窗口時間數學（純函式，零設備、零指令）")
    print("=" * 70)
    for fn in (case_a, case_b, case_c, case_d):
        fn()
    total, bad = len(RESULTS), RESULTS.count(False)
    print("\n" + "=" * 70)
    print(f"  結果：{total - bad}/{total} PASS" + ("" if not bad else f"   ❌ {bad} FAIL"))
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
