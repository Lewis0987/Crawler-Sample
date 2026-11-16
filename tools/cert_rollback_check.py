# -*- coding: utf-8 -*-
"""
cert_rollback_check.py — M-S2：回復舊版（99e2046）前的唯讀安全檢查
================================================================
舊版 cert_runtime.startup() 只要磁碟上有進行中的認證就會以 5 秒續接（沒有格點、沒有結束證據檢查），
所以回復前必須確認：認證都已結束、沒有待處理請求、沒有寫入中斷的暫存檔、服務與選單都已停止。

    python tools\\cert_rollback_check.py                       # 預設檢查 ..\\output\\cert_reports
    python tools\\cert_rollback_check.py --root <cert_reports 路徑>
    python tools\\cert_rollback_check.py --root <路徑> --assume-stopped   # 離線測試用：不查詢服務／程序（輸出會註明）

檢查項目（全部成立才「可以回復」）：
  E-1  ESSAutoMonitor 不是執行中；沒有開啟中的 cert_menu／device_control_menu Python 程序
  E-2  active.json 可以解析且 cert_id 為 null（檔案不存在 → 視為沒有進行中）；沒有 active.json 的暫存檔
  E-3  requests\\pending\\ 沒有任何請求檔（*.json）或暫存檔（*.tmp）
  E-4  每個 CERT_*\\cert_state.json 都可以解析，且 status = ended（涵蓋記錄中、結束失敗、待續接）
  E-5  整個 cert_reports 底下沒有 *.tmp（原子寫入中斷）

**只讀**：不修改、不刪除、不移動任何檔案；不連 HMI／6161；不停止或啟動服務（E-1 只用 sc.exe query 與程序清單查詢）；
不輸出程序命令列內容。結束代碼：0＝可以回復；1＝禁止回復（列出原因）；2＝檢查本身失敗（視同禁止回復）。
"""
import argparse
import glob
import json
import os
import subprocess
import sys

SERVICE_NAME = "ESSAutoMonitor"
MENU_MARKERS = ("cert_menu", "device_control_menu")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}"


def check_service(service_name=SERVICE_NAME):
    """E-1（服務）：sc.exe query（唯讀）。回傳 (ok, 說明)。"""
    try:
        out = subprocess.run(["sc.exe", "query", service_name], capture_output=True, text=True, timeout=20)
    except Exception as e:                              # noqa: BLE001
        return False, f"無法查詢服務狀態（{type(e).__name__}）→ 無法證明已停止"
    txt = (out.stdout or "") + (out.stderr or "")
    if "1060" in txt:                                    # 服務不存在
        return True, f"服務 {service_name} 不存在"
    for line in txt.splitlines():
        if "STATE" in line.upper():
            if "STOPPED" in line.upper():
                return True, f"服務 {service_name} 已停止"
            return False, f"服務 {service_name} 不是停止狀態（{line.strip()}）"
    return False, "無法判讀服務狀態 → 無法證明已停止"


def check_menu_processes():
    """E-1（選單）：列出 python 程序的命令列，只比對是否包含選單模組名稱；不輸出命令列內容。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "ForEach-Object { $_.CommandLine }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=30)
    except Exception as e:                              # noqa: BLE001
        return False, f"無法查詢程序清單（{type(e).__name__}）→ 無法證明選單已關閉"
    if out.returncode != 0:
        return False, "無法查詢程序清單 → 無法證明選單已關閉"
    hits = sum(1 for line in (out.stdout or "").splitlines() if any(m in line for m in MENU_MARKERS))
    if hits:
        return False, f"有 {hits} 個選單程序仍在執行（cert_menu／device_control_menu）"
    return True, "沒有選單程序"


def check_files(root):
    """E-2～E-5（檔案，唯讀）。回傳 [(項目, ok, 說明)]。"""
    res = []
    if not os.path.isdir(root):
        return [("E-0", False, f"找不到認證資料夾：{root}")]
    # E-2
    act, err = _read_json(os.path.join(root, "active.json"))
    tmp_act = glob.glob(os.path.join(root, "active.json.*.tmp"))
    if err == "missing":
        ok, msg = True, "沒有 active.json（沒有進行中的認證）"
    elif err:
        ok, msg = False, f"active.json 無法解析（{err}）"
    elif not isinstance(act, dict):
        ok, msg = False, "active.json 格式不正確"
    elif act.get("cert_id"):
        ok, msg = False, f"active.json 仍指向認證 {act.get('cert_id')}"
    else:
        ok, msg = True, "active.json 的 cert_id 為 null"
    if tmp_act:
        ok, msg = False, msg + f"；有 {len(tmp_act)} 個 active.json 暫存檔（寫入中斷）"
    res.append(("E-2", ok, msg))
    # E-3
    pend = os.path.join(root, "requests", "pending")
    files = sorted(glob.glob(os.path.join(pend, "*.json")) + glob.glob(os.path.join(pend, "*.tmp")))
    res.append(("E-3", not files, "requests\\pending 是空的" if not files
                else f"requests\\pending 有 {len(files)} 個檔案（例：{os.path.basename(files[0])}）"))
    # E-4
    bad = []
    certs = sorted(d for d in glob.glob(os.path.join(root, "CERT_*")) if os.path.isdir(d))
    for d in certs:
        st, err = _read_json(os.path.join(d, "cert_state.json"))
        name = os.path.basename(d)
        if err:
            bad.append(f"{name}（cert_state.json {'不存在' if err == 'missing' else '無法解析'}）")
        elif not isinstance(st, dict) or st.get("status") != "ended":
            bad.append(f"{name}（status={st.get('status') if isinstance(st, dict) else '?'}）")
    res.append(("E-4", not bad, f"{len(certs)} 份認證都已結束" if not bad
                else f"有 {len(bad)} 份認證未結束或狀態不明：" + "、".join(bad[:5])))
    # E-5
    tmps = []
    for dp, _dn, fns in os.walk(root):
        tmps += [os.path.join(dp, f) for f in fns if f.endswith(".tmp")]
    res.append(("E-5", not tmps, "沒有暫存檔（*.tmp）" if not tmps
                else f"有 {len(tmps)} 個暫存檔（寫入中斷）：例 {os.path.relpath(tmps[0], root)}"))
    return res


def run(root, assume_stopped=False, service_name=SERVICE_NAME):
    items = []
    if assume_stopped:
        items.append(("E-1", True, "未查詢服務與程序（--assume-stopped，離線測試用；正式回復不得使用）"))
    else:
        ok1, m1 = check_service(service_name)
        ok2, m2 = check_menu_processes()
        items.append(("E-1", ok1 and ok2, f"{m1}；{m2}"))
    items += check_files(root)
    ok = all(x[1] for x in items)
    return ok, items


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="回復舊版前的唯讀安全檢查（M-S2）")
    ap.add_argument("--root", default=os.path.join(here, "..", "output", "cert_reports"),
                    help="cert_reports 資料夾（預設 ..\\output\\cert_reports）")
    ap.add_argument("--assume-stopped", action="store_true", help="離線測試用：不查詢服務與程序")
    ap.add_argument("--service", default=SERVICE_NAME)
    a = ap.parse_args(argv)
    try:
        ok, items = run(os.path.abspath(a.root), a.assume_stopped, a.service)
    except Exception as e:                              # noqa: BLE001
        print(f"檢查失敗（{type(e).__name__}: {e}）→ 禁止回復")
        return 2
    for k, good, msg in items:
        print(f"  {k} {'OK ' if good else 'NG '} {msg}")
    print("結果：可以回復" if ok else "結果：禁止回復（保持停止並回報人工處理）")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass
    sys.exit(main())
