# -*- coding: utf-8 -*-
"""
phase6_runtime_index_host.py — GAP-4 runtime index 的主機查詢（2026-11-06）
==========================================================================
`phase6_handoff_orchestrator` 維持「模組層零控制相依」（不 import subprocess）。
runtime index 需要的三個主機查詢集中在本模組：

    current_identity()        whoami /user            唯讀：執行帳號 SID
    read_acl_sids(path)       PowerShell Get-Acl      唯讀：檔案 ACL（SID）
    restrict_index_acl(...)   icacls                  **只有**人工 init-index 會呼叫

🔴 本模組**不送任何控制**、不連任何設備，也不讀寫 runtime index 的內容。
🔴 任何查詢失敗一律回 None（呼叫端 Fail Closed）。
"""
import os
import platform
import subprocess

_ACL_PS = ("(Get-Acl -LiteralPath $env:P6_ACL_PATH).Access | ForEach-Object { "
           "$_.IdentityReference.Translate("
           "[System.Security.Principal.SecurityIdentifier]).Value + '|' + "
           "$_.AccessControlType }")


def current_identity():
    """目前執行帳號 SID ＋ 主機名稱。取不到 -> None。"""
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL).stdout
        sid = out.strip().split(",")[-1].strip().strip('"')
    except Exception:                                          # noqa: BLE001
        return None
    if not sid.startswith("S-1-"):
        return None
    return {"sid": sid, "hostname": platform.node()}


def read_acl_sids(path):
    """檔案 ACL 的 (SID, Allow/Deny) 清單。讀不到 -> None。"""
    try:
        env = dict(os.environ)
        env["P6_ACL_PATH"] = path
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", _ACL_PS], capture_output=True,
                           text=True, timeout=60, env=env,
                           stdin=subprocess.DEVNULL)
    except Exception:                                          # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    rows = []
    for ln in (r.stdout or "").splitlines():
        if "|" in ln:
            sid, _, typ = ln.strip().partition("|")
            rows.append((sid.strip(), typ.strip()))
    return rows or None


def restrict_index_acl(path, sid, builtin_sids=("S-1-5-18", "S-1-5-32-544")):
    """以 icacls 移除繼承，只授權初始化帳號 / SYSTEM / Administrators。"""
    grants = ["*%s:(F)" % sid] + ["*%s:(F)" % s for s in builtin_sids]
    for target in (os.path.dirname(path), path):
        subprocess.run(["icacls", target, "/inheritance:r", "/grant:r"]
                       + grants, capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL, check=True)
