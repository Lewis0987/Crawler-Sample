# -*- coding: utf-8 -*-
"""
Phase 3 Regression Runner
======================================================================
一個指令重跑完整 Phase 3 Regression 並輸出總表。

    python run_phase3_regression.py                # 只跑離線項目（預設）
    python run_phase3_regression.py --with-device  # 另外執行需要設備的唯讀探針

設計原則
    本檔是**純測試協調器**：只負責啟動子行程、解析結果、彙總、決定 exit code。
    不含任何產品邏輯，也不修改任何設定或輸出。

判定方式（不只靠字串比對）
    每一項同時檢查兩件事，兩者皆通過才算 PASS：
      ① subprocess 的 return code
      ② 實際 PASS/FAIL 數（由輸出的「（N/M 檢查通過）」解析），且輸出中無 [FAIL] 行
    某一項失敗**不會中斷**其他項目，最後一次列出總表。

離線 vs 實機
    離線 Regression 必須全數通過 —— 這是 Phase 3 軟體品質的判定基準，
    在沒有設備的電腦上也應該可以重跑。
    需要設備的唯讀探針（probe_schedule_context.py）預設不執行；即使加上
    --with-device 但設備不可達，也只記為 SKIP，**不算 FAIL**。

exit code
    0 = 全部通過；非 0 = 有任何 FAIL
"""
import os
import re
import sys
import json
import glob
import argparse
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# 需編譯檢查的檔案（正式碼 + 測試工具）
COMPILE_TARGETS = [
    "charge_discharge_report.py",
    "charge_discharge_report_config.py",
    "device_control_menu.py",
    "phase2_real_trigger_validation.py",
    "test_auto_schedule.py",
    "test_phase3_file_lock.py",
    "test_dashboard_loop.py",
    "probe_schedule_context.py",
]

# (顯示名稱, 指令 argv)；皆為離線項目
OFFLINE_SUITES = [
    ("report selftest", ["charge_discharge_report.py", "--selftest"]),
    ("auto schedule", ["test_auto_schedule.py"]),
    ("3-B selftest", ["phase2_real_trigger_validation.py", "--selftest"]),
    ("Dashboard harness", ["test_dashboard_loop.py"]),
    ("Windows file lock", ["test_phase3_file_lock.py"]),
]

_COUNT_RE = re.compile(r"[（(](\d+)\s*/\s*(\d+)\s*檢查通過[）)]")


def _run(argv, timeout=900):
    """執行子行程，回傳 (returncode, output)。輸出一律以 UTF-8 解碼。"""
    try:
        p = subprocess.run([sys.executable] + argv, cwd=HERE, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"[RUNNER] 逾時（>{timeout}s）"
    except Exception as e:                                   # noqa: BLE001
        return 125, f"[RUNNER] 無法執行：{type(e).__name__}: {e}"


def _judge(rc, out):
    """
    以 return code + 實際檢查數 + [FAIL] 行數共同判定。
    回傳 (status, passed, total, note)；passed/total 為 None 表示該項不輸出檢查數。
    """
    m = _COUNT_RE.search(out)
    passed, total = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    fail_lines = out.count("[FAIL]")
    if rc != 0:
        return FAIL, passed, total, f"return code {rc}"
    if fail_lines:
        return FAIL, passed, total, f"輸出含 {fail_lines} 個 [FAIL]"
    if total is not None and passed != total:
        return FAIL, passed, total, "檢查數不符"
    return PASS, passed, total, ""


def step_compile():
    missing = [f for f in COMPILE_TARGETS if not os.path.exists(os.path.join(HERE, f))]
    if missing:
        return FAIL, None, None, f"檔案不存在：{', '.join(missing)}", ""
    rc, out = _run(["-m", "py_compile"] + COMPILE_TARGETS)
    if rc != 0:
        return FAIL, None, None, f"py_compile 失敗（rc={rc}）", out
    return PASS, None, None, f"{len(COMPILE_TARGETS)} 檔", out


def step_residue():
    """
    環境殘留檢查：正式 output 不得留下 recording / paused 的 Session。
    這是 Phase 3.6 的核心目標（不再產生孤兒），也是結案的必要條件。
    """
    try:
        sys.path.insert(0, HERE)
        import charge_discharge_report_config as CFG        # noqa: E402
        root = os.path.join(PROJECT, "output", CFG.OUTPUT_SUBDIR)
    except Exception as e:                                   # noqa: BLE001
        return FAIL, None, None, f"無法解析輸出路徑：{type(e).__name__}", ""
    if not os.path.isdir(root):
        return PASS, 0, 0, "輸出資料夾尚未建立", ""
    bad, total = [], 0
    for p in sorted(glob.glob(os.path.join(root, "*", "session_state.json"))):
        total += 1
        try:
            with open(p, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:                                    # noqa: BLE001
            bad.append((os.path.basename(os.path.dirname(p)), "無法讀取"))
            continue
        if st.get("status") != CFG.SESSION_COMPLETED:
            bad.append((os.path.basename(os.path.dirname(p)), st.get("status")))
    detail = "\n".join(f"    · {n}  status={s}" for n, s in bad)
    if bad:
        return FAIL, total - len(bad), total, f"{len(bad)} 個未完成 Session", detail
    return PASS, total, total, f"{total} 個 Session 皆 completed", ""


def main():
    ap = argparse.ArgumentParser(description="Phase 3 Regression Runner（純協調器）")
    ap.add_argument("--with-device", action="store_true",
                    help="另外執行需要設備的唯讀探針 probe_schedule_context.py")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="失敗時印出該項完整輸出（預設只印末 40 行）")
    args = ap.parse_args()

    rows = []          # (名稱, status, passed, total, note)
    logs = {}

    print("=" * 62)
    print("Phase 3 Regression")
    print("=" * 62)

    # ---- 1. py_compile ----
    st, p, t, note, out = step_compile()
    rows.append(("py_compile", st, p, t, note))
    logs["py_compile"] = out
    print(f"  {'py_compile':<24} {st}")

    # ---- 2~6. 離線測試套件 ----
    for name, argv in OFFLINE_SUITES:
        rc, out = _run(argv)
        st, p, t, note = _judge(rc, out)
        rows.append((name, st, p, t, note))
        logs[name] = out
        cnt = f"{p}/{t}" if t is not None else ""
        print(f"  {name:<24} {cnt:>9}  {st}")

    # ---- 7. 環境殘留 ----
    st, p, t, note, detail = step_residue()
    rows.append(("Environment residue", st, None, None, note))
    logs["Environment residue"] = detail
    print(f"  {'Environment residue':<24} {'':>9}  {st}  （{note}）")
    if detail:
        print(detail)

    # ---- 8.（選用）需設備的唯讀探針 ----
    if args.with_device:
        rc, out = _run(["probe_schedule_context.py"])
        if rc == 3:
            st, note = SKIP, "設備不可達（不計入 Regression 失敗）"
        elif rc == 0:
            st, note = PASS, "實機唯讀探針成功"
        else:
            st, note = FAIL, f"return code {rc}"
        rows.append(("Schedule probe (device)", st, None, None, note))
        logs["Schedule probe (device)"] = out
        print(f"  {'Schedule probe (device)':<24} {'':>9}  {st}  （{note}）")

    # ---- 總表 ----
    n_pass = sum(1 for r in rows if r[1] == PASS)
    n_fail = sum(1 for r in rows if r[1] == FAIL)
    n_skip = sum(1 for r in rows if r[1] == SKIP)
    checks_p = sum(r[2] for r in rows if r[2] is not None and r[1] != SKIP)
    checks_t = sum(r[3] for r in rows if r[3] is not None and r[1] != SKIP)

    print("\n" + "=" * 62)
    print("Phase 3 Regression")
    print("-" * 62)
    for name, st, p, t, note in rows:
        cnt = f"{p}/{t}" if t is not None else ""
        line = f"{name:<28}{cnt:>11}  {st}"
        print(line + (f"   （{note}）" if note and st != PASS else ""))
    print("-" * 62)
    print(f"{'Automated checks':<28}{f'{checks_p}/{checks_t}':>11}  "
          f"{PASS if checks_p == checks_t else FAIL}")
    print(f"{'FAIL':<28}{n_fail:>11}")
    print(f"{'SKIP':<28}{n_skip:>11}")
    print(f"{'RESULT':<28}{'':>11}  {FAIL if n_fail else PASS}")
    print("=" * 62)

    # ---- 失敗細節 ----
    for name, st, _p, _t, _note in rows:
        if st == FAIL and logs.get(name):
            body = logs[name]
            print(f"\n---- {name} 輸出" + ("" if args.verbose else "（末 40 行）") + " ----")
            lines = body.splitlines()
            print("\n".join(lines if args.verbose else lines[-40:]))

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
