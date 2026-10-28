# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— P3 選單 ↔ 服務 請求機制（純檔案 I/O）
============================================================
選單不直接寫認證資料：所有操作（開始／結束認證、階段標記、不確定項目確認、手動補拍、重產報告）
都寫成一份「請求」交給背景服務（Monitor Owner）處理，結果另存一份「結果」檔。

資料夾（output/cert_reports/）：
  requests/pending/<request_id>.json   選單建立的請求（尚未處理）
  requests/done/<request_id>.json      服務處理完後移入（原內容不改）
  results/<request_id>.json            處理結果（每個請求最多一份；已存在就不再執行 → 重複請求保護）
  active.json                          服務維護：目前進行中的認證 ID（無則 cert_id=None）
  status.json                          服務維護：心跳與目前認證狀態摘要（選單「查看狀態」唯讀讀取）

請求欄位：request_id、type、cert_id（所屬認證；開始認證時為 None）、payload、created_at、source
結果欄位：request_id、type、cert_id、status（done／rejected／failed／expired）、message、data、processed_at

規則：
  - 選單等待逾時 ≠ 失敗：顯示「結果待確認」與請求 ID，之後可查詢；**不得自動重送**。
  - 服務對同一 request_id 只執行一次（結果檔已存在 → 不再執行）。
  - 請求建立後超過 CERT_REQUEST_EXPIRE_SEC 才被處理 → 結果記為 expired，不執行（避免延遲執行開始／結束）。
  - 所有寫入一律「暫存檔 → os.replace」原子替換，讀取端不會讀到半份檔案。
本模組不連設備、不碰 Ownership、不 import report_monitor。
"""

import json
import os
import socket
import time
import uuid
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(os.path.dirname(HERE), "output")

TYPES = {
    "start_cert": "開始認證測試",
    "end_cert": "結束認證測試",
    "stage_label": "階段標記",
    "confirm_item": "不確定項目確認",
    "manual_shot": "立即截圖／手動補拍",
    "regenerate": "重新產生認證報告",
}
STATUS_TEXT = {"done": "完成", "rejected": "拒絕", "failed": "失敗", "expired": "逾期未執行",
               "pending": "待處理（結果待確認）", "unknown": "查無此請求"}


def cert_root(output_dir=None):
    import cert_report_config as CFG
    return os.path.join(output_dir or OUTPUT_DIR, CFG.CERT_SUBDIR)


def _p(root, *parts):
    return os.path.join(root, *parts)


def now_str(t=None):
    return (t or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    for _ in range(20):                         # Windows：讀取端短暫開檔可能讓 replace 失敗 → 稍候重試
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# =============================================================================
# 選單端
# =============================================================================
def new_request(root, rtype, payload=None, cert_id=None, source="menu", now=None):
    """建立請求，回傳 request_id。只寫 requests/pending/，不碰任何認證資料。now：模擬測試注入時間。"""
    assert rtype in TYPES, rtype
    now = now or datetime.now()
    rid = f"REQ_{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    atomic_write_json(_p(root, "requests", "pending", f"{rid}.json"), {
        "request_id": rid, "type": rtype, "cert_id": cert_id, "payload": payload or {},
        "created_at": now_str(now), "source": source, "pid": os.getpid(), "host": socket.gethostname()})
    return rid


def read_result(root, rid):
    return read_json(_p(root, "results", f"{rid}.json"))


def request_state(root, rid):
    """回傳 (狀態, 結果 dict 或 None)。狀態：done/rejected/failed/expired/pending/unknown。"""
    res = read_result(root, rid)
    if res:
        return res.get("status", "unknown"), res
    if os.path.exists(_p(root, "requests", "pending", f"{rid}.json")):
        return "pending", None
    return "unknown", None


def wait_result(root, rid, timeout, poll=0.5, sleep=time.sleep, mono=time.monotonic):
    """等待結果；逾時回 None（**不代表失敗**，呼叫端應顯示「結果待確認」）。"""
    end = mono() + timeout
    while True:
        res = read_result(root, rid)
        if res:
            return res
        if mono() >= end:
            return None
        sleep(poll)


def recent_requests(root, n=10):
    """最近的請求（含待處理）→ [(request dict, 狀態, 結果 or None)]，新到舊。"""
    items = []
    for sub in ("pending", "done"):
        d = _p(root, "requests", sub)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.endswith(".json"):
                    req = read_json(os.path.join(d, fn))
                    if isinstance(req, dict):
                        items.append(req)
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    out = []
    for req in items[:n]:
        st, res = request_state(root, req["request_id"])
        out.append((req, st, res))
    return out


def read_status(root):
    return read_json(_p(root, "status.json"))


def read_active(root):
    a = read_json(_p(root, "active.json"))
    return a.get("cert_id") if isinstance(a, dict) else None


def active_on_disk(root):
    """磁碟上是否有進行中的認證（純唯讀；不依賴服務是否在執行）。回傳 cert_id 或 None。"""
    cid = read_active(root)
    if not cid:
        return None
    st = read_json(_p(root, cid, "cert_state.json"))
    return cid if isinstance(st, dict) and st.get("status") == "recording" else None


# =============================================================================
# 服務端
# =============================================================================
def pending_requests(root):
    d = _p(root, "requests", "pending")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            req = read_json(os.path.join(d, fn))
            if isinstance(req, dict) and req.get("request_id"):
                out.append(req)
    out.sort(key=lambda r: (r.get("created_at", ""), r["request_id"]))
    return out


def write_result(root, req, status, message, cert_id=None, data=None):
    res = {"request_id": req["request_id"], "type": req.get("type"),
           "type_text": TYPES.get(req.get("type"), req.get("type")),
           "cert_id": cert_id if cert_id is not None else req.get("cert_id"),
           "status": status, "status_text": STATUS_TEXT.get(status, status),
           "message": message, "data": data or {}, "created_at": req.get("created_at"),
           "processed_at": now_str()}
    atomic_write_json(_p(root, "results", f"{req['request_id']}.json"), res)
    return res


def archive_request(root, req):
    """處理完（含重複、逾期）→ pending 移到 done（原內容不改）。"""
    src = _p(root, "requests", "pending", f"{req['request_id']}.json")
    dst = _p(root, "requests", "done", f"{req['request_id']}.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)
    except FileNotFoundError:
        pass


def is_expired(req, expire_sec, now=None):
    try:
        created = datetime.strptime(req.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True                         # 無法判定建立時間 → 視為逾期（fail closed，不執行）
    return ((now or datetime.now()) - created).total_seconds() > expire_sec
