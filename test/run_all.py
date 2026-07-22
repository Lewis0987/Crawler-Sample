# -*- coding: utf-8 -*-
"""
Jietech Dashboard 爬蟲總入口（orchestration only）
====================================================
統一執行各支 scraper，支援全部執行與 --only 指定單一項目。
本檔只負責「執行、檢查輸出、整理結果」，不含任何 API 呼叫或 scraper 邏輯。

用法：
    python run_all.py                     # 依序全部執行
    python run_all.py --only alarm        # 只跑單一 task
    python run_all.py --list              # 列出所有 task
    python run_all.py --stop-on-fail      # 任一失敗即停止，後續標 SKIP
    python run_all.py --continue-on-fail  # （預設）失敗續跑下一支
"""

import os
import sys
import argparse
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
# 所有 scraper 統一輸出到專案根目錄的 output/（用 os.path.dirname(HERE) 取專案根，
# 不寫死成目前工作目錄）；run_all 檢查輸出是否存在也看這裡。
OUTPUT_DIR = os.path.join(os.path.dirname(HERE), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 任務定義（順序即執行順序）。run_all 只知道 script 與 expected outputs，不碰其內部邏輯。
TASKS = [
    {"key": "dashboard", "script": "dashboard_scraper.py", "args": ["--once"],
     "outputs": ["dashboard_data.json", "dashboard_data.csv"]},
    {"key": "battery", "script": "battery_data_scraper.py",
     "outputs": ["battery_data.json", "battery_data.csv",
                 "battery_cells.json", "battery_cells.csv"]},
    {"key": "env", "script": "env_data_scraper.py",
     "outputs": ["env_data.json",
                 "env_curated.json", "env_curated.csv"]},
    {"key": "alarm", "script": "alarm_records_scraper.py",
     "outputs": ["alarm_records.json", "alarm_records.csv"]},
    {"key": "device", "script": "device_control_scraper.py",
     "outputs": ["device_control_readonly.json"]},
    {"key": "threshold", "script": "threshold_config_scraper.py",
     "outputs": ["threshold_config.json", "threshold_config_summary.json",
                 "threshold_config_summary.csv"]},
]
TASK_BY_KEY = {t["key"]: t for t in TASKS}
KEYS = [t["key"] for t in TASKS]


def print_list():
    """列出所有 task 與對應 script / 檢查的輸出檔完整路徑。"""
    width = max(len(k) for k in KEYS)
    print(f"OUTPUT_DIR = {OUTPUT_DIR}\n")
    for t in TASKS:
        print(f"{t['key']:<{width}} -> {t['script']}")
        for o in t["outputs"]:
            print(f"{'':<{width}}    check: {os.path.join(OUTPUT_DIR, o)}")


def _print_tail(proc, n=8):
    """印出子行程 stdout / stderr 的最後幾行（除錯用）。"""
    out = (proc.stdout or "").strip().splitlines()
    err = (proc.stderr or "").strip().splitlines()
    if out:
        print("  --- stdout (tail) ---")
        for line in out[-n:]:
            print(f"    {line}")
    if err:
        print("  --- stderr (tail) ---")
        for line in err[-n:]:
            print(f"    {line}")


def run_task(task, idx, total):
    """執行單一 task；回傳 'OK' 或 'FAIL'。"""
    key, script, outputs = task["key"], task["script"], task["outputs"]
    print(f"\n[{idx}/{total}] {key} - {script}")

    # 設備控制安全提醒（run_all 本身不呼叫任何控制 API，只執行該 scraper）
    if key == "device":
        print("[SAFE] device task is readonly only. Control APIs are blocked by allowlist/blocklist.")

    script_path = os.path.join(HERE, script)
    if not os.path.exists(script_path):
        print(f"[FAIL] {key}")
        print(f"  script not found: {script}")
        return "FAIL"

    proc = subprocess.run(
        [sys.executable, "-X", "utf8", script_path, *task.get("args", [])],
        cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )

    missing = [o for o in outputs if not os.path.exists(os.path.join(OUTPUT_DIR, o))]

    # 判定：return code != 0 → FAIL；return code 0 但缺 output → FAIL(Missing)；否則 OK
    if proc.returncode != 0:
        print(f"[FAIL] {key}")
        print(f"  return code: {proc.returncode}")
        _print_tail(proc)
        if missing:
            print(f"  Missing outputs: {', '.join(missing)}")
        return "FAIL"

    if missing:
        print(f"[FAIL] {key}")
        print(f"  return code: 0")
        print(f"  Missing outputs: {', '.join(missing)}")
        return "FAIL"

    print(f"[OK] {key}")
    for o in outputs:
        print(f"  ✓ {o}")
    return "OK"


def print_summary(results, order):
    """results: {key: 'OK'|'FAIL'|'SKIP'}；order: 執行順序的 key 清單。"""
    ok = [k for k in order if results.get(k) == "OK"]
    fail = [k for k in order if results.get(k) == "FAIL"]
    skip = [k for k in order if results.get(k) == "SKIP"]

    def fmt(lst):
        return ", ".join(lst) if lst else "none"

    print("\n========== SUMMARY ==========")
    print(f"OK: {fmt(ok)}")
    print(f"FAIL: {fmt(fail)}")
    print(f"SKIP: {fmt(skip)}")
    print(f"Total: {len(order)}, OK: {len(ok)}, FAIL: {len(fail)}, SKIP: {len(skip)}")
    return len(fail)


def main():
    parser = argparse.ArgumentParser(description="Jietech Dashboard 爬蟲總入口", add_help=True)
    parser.add_argument("--only", metavar="TASK", help="只執行指定 task")
    parser.add_argument("--list", action="store_true", help="列出所有 task 後結束")
    parser.add_argument("--stop-on-fail", action="store_true", help="任一失敗即停止，後續標 SKIP")
    parser.add_argument("--continue-on-fail", action="store_true", help="（預設）失敗續跑下一支")
    args = parser.parse_args()

    if args.list:
        print_list()
        return 0

    # 選定要跑的 task
    if args.only is not None:
        if args.only not in TASK_BY_KEY:
            print(f"Unknown task: {args.only}")
            print(f"Available tasks: {', '.join(KEYS)}")
            return 2
        selected = [TASK_BY_KEY[args.only]]
    else:
        selected = list(TASKS)

    stop_on_fail = args.stop_on_fail  # 預設 continue（失敗續跑）
    order = [t["key"] for t in selected]
    total = len(selected)

    print("=" * 44)
    print(f"執行 {total} 個 task（{'stop-on-fail' if stop_on_fail else 'continue-on-fail'}）")

    results = {}
    stopped = False
    for idx, task in enumerate(selected, 1):
        if stopped:
            print(f"\n[{idx}/{total}] {task['key']} - {task['script']}")
            print(f"[SKIP] {task['key']}")
            results[task["key"]] = "SKIP"
            continue

        status = run_task(task, idx, total)
        results[task["key"]] = status
        if status == "FAIL" and stop_on_fail:
            stopped = True  # 後續 task 標 SKIP

    fail_count = print_summary(results, order)
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
