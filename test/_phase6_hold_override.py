# -*- coding: utf-8 -*-
"""
離線測試專用：在**單一測試程序內**暫時把 FIRST_LIVE_HOLD 設為 False，
並（GAP-4）提供暫存的 C4 環境。

🔴 只供既有的離線 regression 驗證「HOLD 解除後」的原有流程行為。
   每個 fake / injected 相依都沒有真實設備 I/O（runner 另有網路阻擋器）。
🔴 一律在 `finally` 改回原值，且結束時斷言已恢復成 True ——
   測試程序結束後，原始碼與其他程序的 HOLD 永遠是 True。
🔴 GAP-4 的 C4 環境**只使用暫存資料**：暫存 journal 目錄、空的暫存歷史清單、
   已初始化的暫存 runtime index、固定的測試身分與 ACL。
   **不讀寫**正式 runtime index（C:\\ProgramData\\Phase6FirstLive），
   **不寫入**正式 output。
🔴 正式程式**不得** import 本模組（AST regression 驗證）。
"""
import contextlib
import io
import json
import os
import shutil
import tempfile

import phase6_handoff_orchestrator as HO

TEST_SID = "S-1-5-21-0-0-0-1001"
TEST_HOST = "PHASE6-OFFLINE-TEST"


@contextlib.contextmanager
def hold_released_for_offline_test():
    prev = HO.FIRST_LIVE_HOLD
    HO.FIRST_LIVE_HOLD = False
    try:
        yield
    finally:
        HO.FIRST_LIVE_HOLD = prev


def write_test_index(path, sid=TEST_SID, host=TEST_HOST, extra=()):
    """建立暫存 runtime index（第一筆 INDEX_INIT）。只用於暫存路徑。"""
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": HO.INDEX_INIT, "schema_version": 1,
                            "sid": sid, "hostname": host,
                            "os_user": "offline-test", "operator": "test",
                            "statement": "offline regression fixture",
                            "at": "2026-11-06T00:00:00+08:00"}) + "\n")
        for r in extra:
            f.write(json.dumps(r) + "\n")
    return path


def write_test_manifest(path, files=(), exceptions=()):
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "kind": HO.HISTORY_MANIFEST_KIND,
                   "files": list(files), "exceptions": list(exceptions)}, f)
    return path


@contextlib.contextmanager
def offline_c4_env():
    """
    GAP-4：暫存 C4 環境。回傳 dict（journal_dir / manifest / index / root）。
    """
    root = tempfile.mkdtemp(prefix="p6c4_")
    jd = os.path.join(root, "journals")
    os.makedirs(jd)
    man = write_test_manifest(os.path.join(root, "manifest.json"))
    idx = write_test_index(os.path.join(root, "index", "attempt_index.jsonl"))
    saved = (HO.DEFAULT_SCHEDULED_JOURNAL_DIR, HO.HISTORY_MANIFEST_PATH,
             HO.RUNTIME_INDEX_PATH, HO._current_identity, HO._read_acl_sids)
    HO.DEFAULT_SCHEDULED_JOURNAL_DIR = jd
    HO.HISTORY_MANIFEST_PATH = man
    HO.RUNTIME_INDEX_PATH = idx
    HO._current_identity = (lambda: {"sid": TEST_SID, "hostname": TEST_HOST})
    HO._read_acl_sids = (lambda p: [(TEST_SID, "Allow"),
                                    ("S-1-5-18", "Allow"),
                                    ("S-1-5-32-544", "Allow")])
    try:
        yield {"root": root, "journal_dir": jd, "manifest": man, "index": idx}
    finally:
        (HO.DEFAULT_SCHEDULED_JOURNAL_DIR, HO.HISTORY_MANIFEST_PATH,
         HO.RUNTIME_INDEX_PATH, HO._current_identity,
         HO._read_acl_sids) = saved
        shutil.rmtree(root, ignore_errors=True)


def run_main(main, release_hold=True):
    """
    執行測試的 main()：暫存 C4 環境 ＋（預設）HOLD 暫時解除；
    結束後確認 HOLD 已改回 True、正式路徑常數已還原。
    """
    with offline_c4_env():
        if release_hold:
            with hold_released_for_offline_test():
                rc = main()
        else:
            rc = main()
    if HO.FIRST_LIVE_HOLD is not True:
        print("[FAIL] FIRST_LIVE_HOLD 未在 finally 改回 True")
        return 1
    if HO.RUNTIME_INDEX_PATH != (
            r"C:\ProgramData\Phase6FirstLive\attempt_index.jsonl"):
        print("[FAIL] RUNTIME_INDEX_PATH 未還原")
        return 1
    return rc
