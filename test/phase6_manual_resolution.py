# -*- coding: utf-8 -*-
"""
phase6_manual_resolution.py — C5 人工確認（2026-11-06 Attempt #10 安全強化）
==========================================================================
用途
    某份 handoff journal 留下未解除的恢復責任（RESTORE_WITHHELD / PENDING /
    CRITICAL / 未完成的 PAUSE INTENT）時，C4 會擋下之後所有 attempt。
    現場人員**親自處理完畢**後，用本工具留下責任紀錄，解除 C4 對該 journal 的阻擋。

🔴 這是**責任紀錄（accountability），不是身分驗證**。
   系統無法證明輸入的 operator 就是本人；正當性取決於誰能登入這台主機。
🔴 效果**只有**解除 C4 對**這一份** journal 的阻擋：
     · 不解除 FIRST_LIVE_HOLD（本工具不讀也不寫該常數）
     · 不送 restore、不送任何 PCS / 遠端指令
     · 不略過下一次 attempt 的任何檢查（C1 / C2 / C3 / C6 / C7 照常）
🔴 本工具**不連任何設備**。唯讀證據必須由操作者事先以既有唯讀方式取得，
   存成 JSON 檔傳入；兩份證據都必須是 5 分鐘內取得，liveness 為 UNKNOWN 即拒絕。
🔴 只能在互動式終端機執行，並且必須輸入確認語句 `RESOLVE <attempt_id>`。

probe evidence JSON  : {"at": ISO 時間, "liveness": "EXTERNAL_RUNNING" |
                        "EXTERNAL_NOT_RUNNING", "pid": ..., "screen_count": ...}
pcs evidence JSON    : {"at": ISO 時間, "pcs_state": ..., "fault": ...,
                        "critical_alarms": ..., "comm_ok": ..., "soc": ...,
                        "power_kw": ...}
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO              # noqa: E402


def _load(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="phase6_manual_resolution.py",
        description="C5 人工確認：只解除 C4 對單一 journal 的阻擋。"
                    "不解除 HOLD、不送 restore、不連設備。")
    ap.add_argument("--attempt-id", required=True)
    ap.add_argument("--journal-dir", default=HO.DEFAULT_SCHEDULED_JOURNAL_DIR)
    ap.add_argument("--operator", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--probe-evidence", required=True)
    ap.add_argument("--pcs-evidence", required=True)
    # GAP-4：journal 缺失時可改為綁定 ledger 或 runtime index 登錄
    ap.add_argument("--target", choices=("journal", "ledger", "index"),
                    default="journal")
    return ap


_TARGETS = {"journal": HO.ACK_TARGET_JOURNAL, "ledger": HO.ACK_TARGET_LEDGER,
            "index": HO.ACK_TARGET_INDEX_ENTRY}


def init_index_main(argv=None, stdin=None, prompt=None, **kw):
    """
    🔴 GAP-4：人工初始化 runtime index（固定路徑 HO.RUNTIME_INDEX_PATH）。
       只能在互動式終端機、輸入 'INIT PHASE6 INDEX'；已存在即拒絕。
       綁定目前執行帳號 SID 與主機名稱，並限制 ACL。
    """
    ap = argparse.ArgumentParser(prog="phase6_manual_resolution.py init-index")
    ap.add_argument("--operator", required=True)
    ap.add_argument("--statement", required=True,
                    help="聲明：本機為何可以開始記錄（例：歷史清單已核對）")
    args = ap.parse_args(argv)
    stdin = stdin or sys.stdin
    interactive = bool(getattr(stdin, "isatty", lambda: False)())
    if not interactive:
        print("[拒絕] 非互動式終端機。index 只能由人工初始化。", file=sys.stderr)
        return 2
    print("  runtime index : %s" % (kw.get("path") or HO.RUNTIME_INDEX_PATH))
    phrase = (prompt or input)("  輸入 'INIT PHASE6 INDEX' 確認：")
    ok, rec = HO.init_runtime_index(args.operator, args.statement, phrase,
                                    interactive, **kw)
    if not ok:
        print("[拒絕] %s" % rec, file=sys.stderr)
        return 2
    print("  已初始化（SID=%s, host=%s）" % (rec["sid"], rec["hostname"]))
    return 0


def main(argv=None, stdin=None, prompt=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "init-index":
        return init_index_main(argv[1:], stdin=stdin, prompt=prompt)
    args = build_parser().parse_args(argv)
    stdin = stdin or sys.stdin
    interactive = bool(getattr(stdin, "isatty", lambda: False)())
    if not interactive:
        print("[拒絕] 非互動式終端機。C5 只能由人工親自執行。", file=sys.stderr)
        return 2
    try:
        probe_ev = _load(args.probe_evidence)
        pcs_ev = _load(args.pcs_evidence)
    except (OSError, ValueError) as e:
        print("[拒絕] 證據檔無法讀取：%s" % type(e).__name__, file=sys.stderr)
        return 2
    jpath = HO.attempt_journal_path(args.journal_dir, args.attempt_id)
    print("=" * 72)
    print("  C5 人工確認 —— 只解除 C4 對下列 journal 的阻擋")
    print("  journal   : %s" % jpath)
    print("  operator  : %s" % args.operator)
    print("  🔴 不解除 FIRST_LIVE_HOLD、不送 restore、不略過任何檢查。")
    print("  🔴 這是責任紀錄，不是身分驗證。")
    print("=" * 72)
    phrase = (prompt or input)("  輸入 'RESOLVE %s' 確認：" % args.attempt_id)
    ok, rec = HO.acknowledge_manual_resolution(
        jpath, args.attempt_id, args.operator, args.reason,
        probe_ev, pcs_ev, confirm_phrase=phrase, interactive=interactive,
        target_kind=_TARGETS[args.target])
    if not ok:
        print("[拒絕] %s" % rec, file=sys.stderr)
        return 2
    print("  已寫入：%s" % HO.manual_resolution_path(args.journal_dir,
                                                     args.attempt_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
