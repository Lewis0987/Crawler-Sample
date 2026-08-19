# -*- coding: utf-8 -*-
"""
Phase 5.3 #11（Ownership contention）的**子行程** helper —— 不是測試本身。

Mutex 互斥無法在單一行程內驗證（同行程再 Wait 只會累加遞迴計數），
因此 #11 必須真的起多個行程，由本檔擔任被起的那一方。

⚠️ 刻意**不**沿用 `_p44_owner_child.py`：那支是 Phase 4.4 / 4.7 的既有資產，
   被 test_phase4_ownership.py / test_phase4_recovery.py 解析，
   在它身上加欄位會有波及既有測試的風險。本檔為 Phase 5.3 專用。

隔離契約（父行程會逐一驗證，任一不成立即中止全部測試）：
    · root 必須由父行程傳入，且位於系統暫存目錄之下
    · 每個模式都回報自己的 `_report_output_root()` 與 `_mutex_name()`
      → 父行程比對其**必定不等於**正式 canonical Mutex
    · 全程零網路、不碰正式 output、不碰 Service

以 stdout 最後一行輸出 `__P53__` + 單行 JSON，供父行程解析。

用法（皆由測試呼叫，不需人工執行）：
    python _p53_stress_child.py report     <root>
    python _p53_stress_child.py acquire    <root>            取得後立即釋放
    python _p53_stress_child.py contend    <root> <hold_sec> 取得後持有 N 秒（競爭用）
    python _p53_stress_child.py try        <root>            只嘗試，不持有
    python _p53_stress_child.py crash_hold <root>            取得後以 os._exit 中止
"""
import io
import os
import sys
import json
import time
import tempfile
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def emit(obj):
    """結果一律印在最後一行，避免被模組載入訊息干擾。"""
    sys.stdout.write("\n__P53__" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    mode = sys.argv[1]
    root = sys.argv[2]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):            # 壓住模組載入訊息
        import report_monitor as RM
        import charge_discharge_report as CDR
        import charge_discharge_report_config as CFG
        # ⚠️ 必須早於任何 Mutex / owner 檔案相關呼叫
        RM._report_output_root = lambda: root

        # 零網路：本檔完全不需要 client，但仍取代掉避免任何意外出網
        CDR.read_all = lambda _c: {"communication_ok": True, "_fail": []}
        CDR._fetch_cell_packs = lambda _c: (CDR._fake_pack_data(), None)

    base = {
        "mode": mode, "pid": os.getpid(), "root": RM._report_output_root(),
        "mutex": RM._mutex_name(),
        "owner_file": os.path.join(root, CFG.MONITOR_OWNER_FILE),
        "tmp_root": tempfile.gettempdir(),
    }

    if mode == "report":
        emit(base)
        return 0

    if mode == "acquire":
        ok, why = RM.acquire_ownership(role="p53-child")
        st = RM.ownership_state()
        owner_exists = os.path.exists(base["owner_file"])
        RM.release_ownership()
        emit({**base, "ok": ok, "why": why,
              "abandoned": st.get("abandoned"),
              "owner_file_existed_while_held": owner_exists,
              "owner_file_after_release": os.path.exists(base["owner_file"]),
              "is_owner_after_release": RM.is_owner()})
        return 0

    if mode == "try":
        ok, why = RM.acquire_ownership(role="p53-child-try")
        st = RM.ownership_state()
        if ok:
            RM.release_ownership()
        emit({**base, "ok": ok, "why": why, "abandoned": st.get("abandoned")})
        return 0

    if mode == "contend":
        hold = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
        ok, why = RM.acquire_ownership(role="p53-child-contend")
        st = RM.ownership_state()
        emit({**base, "ok": ok, "why": why, "abandoned": st.get("abandoned"),
              "acquired_at": time.time()})
        if ok:
            time.sleep(hold)
            RM.release_ownership()
        return 0

    if mode == "crash_hold":
        # 取得所有權後以 os._exit 中止 —— 跳過 atexit / finally，
        # 從 Mutex 的角度等同崩潰，**不需要也不使用任何強制終止指令**。
        ok, why = RM.acquire_ownership(role="p53-child-crash")
        st = RM.ownership_state()
        emit({**base, "ok": ok, "why": why, "abandoned": st.get("abandoned")})
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    emit({**base, "error": f"unknown mode {mode}"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
