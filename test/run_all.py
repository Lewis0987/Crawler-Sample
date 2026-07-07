# -*- coding: utf-8 -*-
"""
Jietech Dashboard 爬蟲總入口
=============================
依序執行五支頁籤 scraper，逐步顯示進度與產出檔案，最後彙總成功/失敗。
單支失敗預設不中斷，會繼續跑下一支。

執行（全部）：
    cd D:\\Jietech\\test
    python run_all.py

只跑其中一支：
    python run_all.py --only dashboard
    python run_all.py --only battery
    python run_all.py --only env
    python run_all.py --only alarm
    python run_all.py --only device
"""

import os
import sys
import time
import argparse
import subprocess

# 讓自身 console 輸出走 UTF-8（Windows 中文才不會亂碼）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# (key, 說明, 腳本檔, 預期產出檔)
STEPS = [
    ("dashboard", "數據概覽", "dashboard_min.py", ["dashboard_min.json"]),
    ("battery",   "電池數據", "battery_data_scraper.py",
     ["battery_data.json", "battery_data.csv", "battery_cells.json", "battery_cells.csv"]),
    ("env",       "環控數據", "env_data_scraper.py",
     ["env_data.json", "env_data.csv", "env_curated.json", "env_curated.csv"]),
    ("alarm",     "告警記錄", "alarm_records_scraper.py",
     ["alarm_records.json", "alarm_records.csv"]),
    ("device",    "設備控制", "device_control_scraper.py",
     ["device_control_readonly.json"]),
]


def run_step(idx, total, step):
    """執行單支 scraper，回傳 (ok, produced_files)。"""
    key, label, script, expected = step
    print(f"\n[{idx}/{total}] {script}（{label}）...")
    start = time.time()

    proc = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join(HERE, script)],
        cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    ok = proc.returncode == 0

    # 檢查預期產出檔是否於本次執行後被寫出
    produced = []
    for fn in expected:
        path = os.path.join(HERE, fn)
        if os.path.exists(path) and os.path.getmtime(path) >= start - 1:
            produced.append(fn)
            print(f"[OK] {fn}")
        else:
            print(f"[MISS] {fn}（未產生或未更新）")

    if not ok:
        print(f"[FAIL] {script}（return code={proc.returncode}）")
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        for line in tail:
            print(f"       {line}")

    return ok, produced


def main():
    parser = argparse.ArgumentParser(description="Jietech Dashboard 爬蟲總入口")
    parser.add_argument("--only", choices=[s[0] for s in STEPS],
                        help="只執行指定頁籤（dashboard/battery/env/alarm/device）")
    args = parser.parse_args()

    steps = [s for s in STEPS if (args.only is None or s[0] == args.only)]
    total = len(steps)

    print("=" * 56)
    print(f"Jietech Dashboard 爬蟲總入口，將執行 {total} 支")

    succeeded, failed, all_files = [], [], []
    for i, step in enumerate(steps, 1):
        ok, produced = run_step(i, total, step)
        (succeeded if ok else failed).append(step[2])
        all_files.extend(produced)

    print("\n" + "=" * 56)
    print("執行總結")
    print(f"  成功：{len(succeeded)} 支 {succeeded}")
    print(f"  失敗：{len(failed)} 支 {failed}")
    print(f"  產出檔案（{len(all_files)}）：")
    for f in all_files:
        print(f"    - {f}")

    # 有任何失敗時以非 0 離開，方便外部判斷
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
