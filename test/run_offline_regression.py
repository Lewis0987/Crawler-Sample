# -*- coding: utf-8 -*-
"""
離線 Regression 正式入口（唯一）
================================
逐一執行 test/test_*.py，**預設不連線任何實體設備**。

    python run_offline_regression.py              # 全部純離線套件
    python run_offline_regression.py --only cert  # 只跑認證相關
    python run_offline_regression.py -k menu      # 只跑名稱含 menu 的套件
    python run_offline_regression.py --list       # 只列出分類，不執行
    python run_offline_regression.py --json out.json

⚠ **run_all.py 不是 Regression Runner。** 它是實機唯讀 scraper 套件：執行後會連 HMI
   並覆寫 output\\ 下 15 個快照檔。run_phase3_regression.py、run_scheduled_first_live.py
   同樣不是純離線入口。分類請見 test/README.md。

兩道防線（不只靠排除清單）
--------------------------
  第 1 道　排除清單 LIVE_SUITES：已知**設計上就需要實機**的套件，預設不執行。
  第 2 道　網路阻擋 _offline_guard/sitecustomize.py：經 PYTHONPATH 注入，受測行程與
           它啟動的每一個 python 子行程都會被攔截 socket.connect / connect_ex。
           排除清單防的是「已知」的實機入口，網路阻擋防的是「意外」的對外連線 ——
           兩者互補，任何一道單獨都不夠。

  阻擋器刻意把 socket 移出 sys.modules（細節見該檔說明），因此
  test_phase6_safety_gate 等「import 後未載入 socket」的既有斷言**不會被影響**。
  本 Runner 不修改、不跳過、不放寬任何套件的斷言。

離場代碼
--------
  0  全部通過
  1  有「非既有」的失敗 → 要處理
  2  只有 KNOWN_FAILURES 列出的既有失敗（如實列示，不視為通過）
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD_DIR = os.path.join(HERE, "_offline_guard")

# --- 第 1 道防線：設計上就需要實機的套件，預設不執行 -------------------------
LIVE_SUITES = {
    "test_cert_p2_live": "需開啟實際 HMI 頁面擷圖（cert_screenshot.cdp_capture 會啟動 Chrome）",
    "test_sm2_login": "實測會連線 192.168.128.110:8080（HMI SM2 登入）",
}

# --- 需要專用 CLI 參數的測試工具：不由本 Runner 直接執行（SKIP-CLI，不計入 PASS／FAIL）------
#     與 LIVE_SUITES 不同：這些不是需要實機，而是必須用專用命令與參數執行，否則只會回傳 argparse 錯誤。
CLI_TOOL_SUITES = {
    "test_cert_merge_report": "需要 --phase/--work 等專用參數；由 Phase 3A～3C 專用命令獨立驗證",
}

# --- 既有失敗：如實列示，不在離線 Regression 範圍內修正 ----------------------
KNOWN_FAILURES = {
    "test_pcs_modes": (
        5,
        "fixture 仍停在舊的控制模式契約。commit 0ffc726（2026-07-28）把資料來源改為 "
        "schedulePlanSwitch + manualModeSwitch 並新增「未開啟模式」，fixture 未同步。"
        "PRE-EXISTING / OUT-OF-SCOPE —— 需實機核對後才決定修正方向，不在本入口範圍。"),
}


def suites():
    return [(os.path.splitext(os.path.basename(p))[0], p)
            for p in sorted(glob.glob(os.path.join(HERE, "test_*.py")))]


def count_checks(out):
    """各套件輸出格式不一，依序嘗試三種統計方式。回傳 (pass, fail)。"""
    m = re.search(r"（(\d+)\s*/\s*(\d+)\s*檢查通過）", out)
    if m:
        return int(m.group(1)), int(m.group(2)) - int(m.group(1))
    m = re.search(r"結果：(\d+)\s*/\s*(\d+)\s*PASS", out)
    if m:
        return int(m.group(1)), int(m.group(2)) - int(m.group(1))
    return (len(re.findall(r"\[PASS\]|^\s*PASS ", out, re.M)),
            len(re.findall(r"\[FAIL\]|^\s*FAIL ", out, re.M)))


def child_env(guard, log):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if guard:
        old = env.get("PYTHONPATH")
        env["PYTHONPATH"] = GUARD_DIR + (os.pathsep + old if old else "")
        env["OFFLINE_GUARD_LOG"] = log
    else:
        env.pop("OFFLINE_GUARD_LOG", None)
    return env


def main():
    ap = argparse.ArgumentParser(description="離線 Regression 正式入口（預設不連實機）")
    ap.add_argument("-k", metavar="SUBSTR", help="只執行名稱包含 SUBSTR 的套件")
    ap.add_argument("--only", choices=("cert", "phase4", "phase6"), help="只執行該族群")
    ap.add_argument("--list", action="store_true", help="只列出分類，不執行任何測試")
    ap.add_argument("--json", metavar="PATH", help="把逐套件結果寫成 JSON")
    ap.add_argument("--no-guard", action="store_true",
                    help="關閉網路阻擋（預設開啟；關閉後無法保證不連實機）")
    ap.add_argument("--include-live", action="store_true",
                    help="連同 LIVE_SUITES 一起執行（需同時指定 --no-guard，會連實機）")
    ap.add_argument("--timeout", type=float, default=900, help="單一套件逾時秒數（預設 900）")
    args = ap.parse_args()

    if args.include_live and not args.no_guard:
        print("[拒絕] --include-live 會連線實體設備，必須同時明確指定 --no-guard。")
        return 1

    all_suites = suites()
    if args.k:
        all_suites = [s for s in all_suites if args.k in s[0]]
    if args.only:
        all_suites = [s for s in all_suites if s[0].startswith("test_" + args.only)]

    if args.list:
        print("== 純離線（本 Runner 會執行）==")
        for n, _ in all_suites:
            if n not in LIVE_SUITES and n not in CLI_TOOL_SUITES:
                note = ""
                if n in KNOWN_FAILURES:
                    note = "   ← 既有失敗 %d 項（PRE-EXISTING / OUT-OF-SCOPE）" % KNOWN_FAILURES[n][0]
                print("  %s%s" % (n, note))
        print("\n== 需要實體 HMI（預設不執行）==")
        for n, why in LIVE_SUITES.items():
            print("  %-26s %s" % (n, why))
        print("\n== 需專用 CLI 參數的測試工具（SKIP-CLI；本 Runner 不執行，須用專用命令另外執行）==")
        for n, why in CLI_TOOL_SUITES.items():
            print("  %-26s %s" % (n, why))
        print("\n== 實機 scraper（本 Runner 永不執行）==")
        print("  run_all.py                 實機唯讀 scraper 套件，會覆寫 output\\ 下 15 個快照檔")
        print("  run_phase3_regression.py   非純離線入口")
        print("  run_scheduled_first_live.py 排程實機腳本")
        return 0

    guard = not args.no_guard
    # 記錄檔放系統暫存目錄（不在 repo 留下暫存檔），並帶 pid：
    # 允許同時跑多個 Runner（例如 --only cert 與全量），彼此不互相清掉。
    log = os.path.join(tempfile.gettempdir(), "ess_offline_netlog_%d.tmp" % os.getpid())
    if guard and os.path.exists(log):
        os.remove(log)

    print("=" * 78)
    print("離線 Regression｜網路阻擋：%s｜實機套件：%s"
          % ("啟用" if guard else "⚠ 已關閉", "一併執行 ⚠" if args.include_live else "預設排除"))
    print("=" * 78)

    rows, t0 = [], time.time()
    for name, path in all_suites:
        if name in CLI_TOOL_SUITES:                     # 先於 LIVE_SUITES 判斷：--include-live 也不執行
            rows.append({"suite": name, "rc": "SKIP-CLI", "pass": 0, "fail": 0,
                         "why": CLI_TOOL_SUITES[name]})
            print("%-44s SKIP-CLI 需專用參數（%s）" % (name, CLI_TOOL_SUITES[name]))
            continue
        if name in LIVE_SUITES and not args.include_live:
            rows.append({"suite": name, "rc": "SKIP", "pass": 0, "fail": 0,
                         "why": LIVE_SUITES[name]})
            print("%-44s SKIP   需實體 HMI（%s）" % (name, LIVE_SUITES[name]))
            continue
        n0 = os.path.getsize(log) if (guard and os.path.exists(log)) else 0
        try:
            r = subprocess.run([sys.executable, path], cwd=HERE, env=child_env(guard, log),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=args.timeout)
            out, rc = (r.stdout or "") + (r.stderr or ""), r.returncode
        except subprocess.TimeoutExpired:
            out, rc = "", "TIMEOUT"
        n1 = os.path.getsize(log) if (guard and os.path.exists(log)) else 0
        npass, nfail = count_checks(out)
        rows.append({"suite": name, "rc": rc, "pass": npass, "fail": nfail, "net": n1 > n0})
        print("%-44s rc=%-7s PASS=%-5d FAIL=%-3d%s"
              % (name, rc, npass, nfail, "   ⚠ 有對外連線嘗試（已阻擋）" if n1 > n0 else ""))

    ran = [r for r in rows if r["rc"] not in ("SKIP", "SKIP-CLI")]
    skip_cli = [r for r in rows if r["rc"] == "SKIP-CLI"]
    skip_live = [r for r in rows if r["rc"] == "SKIP"]
    known = [r for r in ran if r["suite"] in KNOWN_FAILURES and (r["fail"] or r["rc"] != 0)]
    bad = [r for r in ran if r["suite"] not in KNOWN_FAILURES and (r["fail"] or r["rc"] != 0)]
    tp = sum(r["pass"] for r in ran)
    tf = sum(r["fail"] for r in ran)
    kf = sum(r["fail"] for r in known)

    print("\n" + "=" * 78)
    print("執行 %d 套件｜略過 %d（需實體 HMI）｜略過 %d（SKIP-CLI，需專用參數）｜耗時 %.0fs"
          % (len(ran), len(skip_live), len(skip_cli), time.time() - t0))
    print("通過 %d 項｜失敗 %d 項（其中既有失敗 %d 項）" % (tp, tf, kf))
    if skip_cli:
        print("\n-- SKIP-CLI（未執行、不計入 PASS／FAIL；必須另外以專用命令執行）--")
        for r in skip_cli:
            print("  %s：%s" % (r["suite"], r["why"]))
    if known:
        print("\n-- 既有失敗（PRE-EXISTING / OUT-OF-SCOPE，如實列示、不修正）--")
        for r in known:
            n, why = KNOWN_FAILURES[r["suite"]]
            print("  %s：%d/%d（既有 %d 項）\n    %s"
                  % (r["suite"], r["pass"], r["pass"] + r["fail"], n, why))
    if bad:
        print("\n-- 需處理的失敗 --")
        for r in bad:
            print("  %s：%d/%d（rc=%s）" % (r["suite"], r["pass"], r["pass"] + r["fail"], r["rc"]))

    if guard:
        lines = []
        if os.path.exists(log):
            with open(log, encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        print("\n-- 網路阻擋 --")
        if lines:
            print("  共阻擋 %d 次對外連線嘗試：" % len(lines))
            for ln in lines[:20]:
                print("    pid %s" % ln.replace("\t", " → "))
        else:
            print("  0 次對外連線嘗試 → 全程未碰實機。")

    if guard and os.path.exists(log):
        try:
            os.remove(log)                          # 證據已列印完畢，不留暫存檔
        except OSError:
            pass
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "pass": tp, "fail": tf, "known_fail": kf,
                       "guard": guard, "include_live": args.include_live}, f,
                      ensure_ascii=False, indent=2)
        print("\n逐套件結果已寫入：%s" % args.json)

    if bad:
        return 1
    return 2 if known else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
