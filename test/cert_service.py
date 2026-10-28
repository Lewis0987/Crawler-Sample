# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— P3 服務端組裝（auto_monitor_service 呼叫）
==================================================================
只在「Monitor Owner 且角色為 service」的行程啟動認證執行緒：
  - Dashboard／選單即使持有 Ownership 也不記錄認證（關閉選單不得中斷認證記錄）
  - CERT_MODE_ENABLED=False → 完全不啟動、不安裝互斥 hook，既有單次報告行為不變

讀值一律沿用服務主迴圈已登入的共用 client（RM._report["client"]）並在 RM._CLIENT_LOCK 內讀取：
本模組不自行登入（避免與主迴圈搶登入），沒有 client 時該筆取樣記為讀取失敗。
6161 只做唯讀 GET /api/temperature（不呼叫 /api/log 等任何會改變狀態的端點）。
"""

import json
import os
import urllib.request

import cert_report_config as CFG
import cert_requests as REQ

_runtime = None


def _reader():
    import report_monitor as RM
    import charge_discharge_report as CDR
    client = RM._report.get("client")
    if client is None:
        return None
    with RM._CLIENT_LOCK:
        return CDR.read_all(client)


def _heat_reader():
    with urllib.request.urlopen(CFG.CERT_HEAT_API_URL, timeout=CFG.CERT_HEAT_API_TIMEOUT_SEC) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def single_report_state():
    """單次報告是否正在記錄（記憶體中的 Session，或磁碟上 recording/paused 的 Session）。"""
    import report_monitor as RM
    import charge_discharge_report as CDR
    s = RM._report.get("session")
    if s is not None:
        return f"記錄中 {getattr(s, 'session_id', '?')}"
    try:
        a = CDR.find_active_session(RM._report_output_root())
    except Exception:                                   # noqa: BLE001
        a = None
    return f"未完成的單次報告 {os.path.basename(a)}（recording／paused）" if a else None


def start_if_enabled(stop_event, log=print):
    """服務取得 Ownership 後呼叫。回傳 CertRuntime 或 None（未啟用／非服務 Owner）。"""
    global _runtime
    if not CFG.CERT_MODE_ENABLED:
        return None
    import report_monitor as RM
    st = RM.ownership_state()
    if not RM.is_owner() or st.get("role") != "service":
        log("[CERT] 非背景服務 Owner → 不啟動認證記錄")
        return None
    from cert_runtime import CertRuntime
    root = REQ.cert_root(RM.OUTPUT_DIR)
    rt = CertRuntime(root, reader=_reader, heat_reader=_heat_reader, single_state=single_report_state,
                     cmd_file=os.path.join(RM.OUTPUT_DIR, "device_control_action_result.json"), log=log)
    # 認證進行中 → 單次報告所有建立入口一律拒絕；記憶體狀態之外再看磁碟（續接失敗時也不會誤建單次報告）
    RM.set_cert_active_provider(lambda: rt.active_id() or REQ.active_on_disk(root))
    try:
        rt.startup()
    except Exception as e:                          # noqa: BLE001
        log(f"[CERT] 接續認證失敗（單次報告仍被擋下；請檢查資料夾）：{type(e).__name__}: {e}")
    rt.run(stop_event)
    _runtime = rt
    log(f"[CERT] 認證記錄執行緒已啟動（資料夾 {root}）")
    return rt
