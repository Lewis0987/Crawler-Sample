# -*- coding: utf-8 -*-
"""
Phase 4.4 跨行程測試用的**子行程** helper（不是測試本身）。

Mutex 互斥無法在單一行程內驗證 —— 同行程再 Wait 只會累加遞迴計數。
因此 A~G 必須真的起兩個行程，由本檔擔任被起的那一方。

以 stdout 最後一行輸出單行 JSON 結果，供父行程解析。
全程離線：不連設備、不建立真實 Session（除非父行程明確要求 hold-session 模式）。

用法（皆由 test_phase4_ownership.py 呼叫，不需人工執行）：
    python _p44_owner_child.py acquire   <root>          取得後立即回報並釋放
    python _p44_owner_child.py hold      <root> <sec>    取得後持有 N 秒
    python _p44_owner_child.py hold_kill <root>          取得後永久持有（等父行程強制終止）
    python _p44_owner_child.py resume    <root>          以非 Owner 身分嘗試 resume
    python _p44_owner_child.py service   <root>          走 Service main() 的 ownership 判斷
"""
import io
import os
import sys
import json
import time
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _emit(obj):
    """結果一律印在最後一行，避免被模組載入訊息干擾。"""
    sys.stdout.write("\n__P44__" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    mode = sys.argv[1]
    root = sys.argv[2]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):            # 壓住模組載入訊息
        import report_monitor as RM
        import charge_discharge_report as CDR
        RM._report_output_root = lambda: root

    if mode == "acquire":
        ok, why = RM.acquire_ownership(role="child")
        st = RM.ownership_state()
        RM.release_ownership()
        _emit({"ok": ok, "why": why, "abandoned": st.get("abandoned")})
        return 0

    if mode in ("hold", "hold_kill"):
        ok, why = RM.acquire_ownership(role="child")
        st = RM.ownership_state()
        _emit({"ok": ok, "why": why, "abandoned": st.get("abandoned"),
               "pid": os.getpid()})
        if not ok:
            return 1
        if mode == "hold":
            time.sleep(float(sys.argv[3]))
            RM.release_ownership()
            return 0
        while True:                                   # 等父行程 taskkill /F
            time.sleep(0.2)

    if mode == "wait_abandoned":
        # 開啟 handle 但**不**取得所有權，然後以阻塞方式等待。
        # 父行程會在這段等待期間強制終止 owner —— 此時 Mutex 物件因本行程仍持有
        # handle 而不會消滅，OS 便會讓本次 Wait 回傳 WAIT_ABANDONED。
        # （若無人持有 handle，owner 一死物件即消滅，下一個 open-or-create 會建出
        #   全新物件並得到 WAIT_OBJECT_0 —— 那條路徑無法驗證 WAIT_ABANDONED。）
        #
        # ⚠️ 一律使用 CreateMutexExW + MUTEX_MIN_ACCESS，與產品碼同一條路徑：
        #    ① CreateMutexW 已不在 _k32() 的宣告清單中，未宣告 restype 會讓 64 位元
        #       HANDLE 被截成 32 位元 int（目前僥倖可用，不可依賴）。
        #    ② 這裡要驗的是「非 Owner 以最小權限開 handle 後仍能收到 WAIT_ABANDONED」，
        #       用 ALL_ACCESS 開就不是產品實際走的路徑。
        k = RM._k32()
        name = RM._mutex_name()
        h = k.CreateMutexExW(None, name, 0, RM.MUTEX_MIN_ACCESS)
        _emit({"stage": "handle_open", "handle": bool(h), "pid": os.getpid()})
        rc = k.WaitForSingleObject(h, int(sys.argv[3]))
        res = {"rc": int(rc),
               "abandoned": rc == RM.WAIT_ABANDONED,
               "acquired": rc == RM.WAIT_OBJECT_0,
               "timeout": rc == RM.WAIT_TIMEOUT}
        if rc in (RM.WAIT_OBJECT_0, RM.WAIT_ABANDONED):
            k.ReleaseMutex(h)                     # 取得後正常釋放，不留下第二個 abandoned
        k.CloseHandle(h)
        _emit(res)
        return 0

    if mode == "resume":
        # 模擬第二個行程啟動：非 Owner 不得 resume、不得起取樣執行緒
        ok, why = RM.acquire_ownership(role="child-dashboard")
        with contextlib.redirect_stdout(buf):
            RM.report_resume_on_launch()
        _emit({"ok": ok, "why": why,
               "session": RM._report["session"] is not None,
               "thread": RM._report["thread"] is not None})
        RM.release_ownership()
        return 0

    if mode == "service":
        import auto_monitor_service as SVC
        with contextlib.redirect_stdout(buf):
            SVC.RM._report_output_root = lambda: root
        code = SVC.main(["--once", "--no-resume"])
        _emit({"exit": code})
        return code

    _emit({"error": f"unknown mode {mode}"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
