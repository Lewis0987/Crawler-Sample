# -*- coding: utf-8 -*-
"""
phase6_guard_deploy_plan.py — Phase 6.10-C4.6/C4.7/C4.8 部署與回滾套件
======================================================================
**只準備，不執行。**

本模組產生三樣東西：

    1. staged B2 guard 檔（寫在本機 output/，**不上傳**）
    2. 未來部署時要逐字執行的指令清單（字串，**本模組不執行**）
    3. 部署後驗證順序與回滾程序（同樣是清單）

🔴 **結構上不可能部署。** 本模組不匯入 subprocess / paramiko / socket，
   也沒有任何 ssh / scp 呼叫。所有遠端指令都以字串形式回傳，交給人操作。
   回歸測試以 AST 驗證這一點。

🔴 **部署後的第一個 capability test 不得是 pause / restore。**
   先靠唯讀的 probe capability metadata 確認兩者「存在」；真正送出
   pause / restore 留給受控 handoff / FIRST LIVE。

🔴 2026-09-07：B2 已由使用者手動部署並通過唯讀驗證，
   `DEPLOYED_GUARD_VARIANT = B2`。本模組仍只產生指令、不執行；
   回滾件保留在 `phase6_guard_backup/`，`authorized_keys` 全程未修改。
"""
import io
import os
import sys
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_remote_adapter as RA          # noqa: E402

# ======================================================================
# 遠端路徑與權限要求
# ======================================================================
REMOTE_USER = "etica"
REMOTE_HOST = "192.168.70.201"
GUARD_PATH = "/home/etica/ems/phase6_remote_guard.sh"
GUARD_BACKUP_DIR = "/home/etica/ems/phase6_guard_backup"
STAGE_PATH = "/home/etica/ems/.phase6_remote_guard.sh.new"
AUTHORIZED_KEYS = "/home/etica/.ssh/authorized_keys"

# 🔴 forced-command 腳本必須由 owner 獨佔可寫，否則 allowlist 形同虛設。
REQUIRED_OWNER = "etica"
REQUIRED_GROUP = "etica"
REQUIRED_MODE = "0700"
REQUIRED_DIR_MODE = "0755"

STAGE_DIR = os.path.join(os.path.dirname(HERE), "output", "phase6_guard_stage")


# ======================================================================
# C4.6-1/2/5. staged artifact + SHA256
# ======================================================================
def guard_text(variant):
    if variant == "B1":
        return RA.REMOTE_GUARD_B1_SH
    if variant == "B2":
        return RA.REMOTE_GUARD_B2_SH
    raise ValueError("未知的 guard variant：%r" % (variant,))


def guard_sha256(variant):
    """以 **LF** 行尾計算 —— 遠端是 Linux，CRLF 會讓 bash 直接壞掉。"""
    return hashlib.sha256(guard_bytes(variant)).hexdigest()


def guard_bytes(variant):
    return guard_text(variant).replace("\r\n", "\n").encode("utf-8")


def stage(variant, outdir=None):
    """
    把 guard 內容寫成本機 staged 檔。**只寫本機，不上傳。**

    回傳 dict：path / sha256 / size / variant。
    """
    d = outdir or STAGE_DIR
    os.makedirs(d, exist_ok=True)
    raw = guard_bytes(variant)
    p = os.path.join(d, "phase6_remote_guard_%s.sh" % variant)
    with io.open(p, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    return {"variant": variant, "path": p, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


# ======================================================================
# C4.6-4. 語法檢查（本機，對 staged 檔）
# ======================================================================
def syntax_check_command(local_path):
    """本機語法檢查指令（`bash -n` 不執行腳本內容）。"""
    return ["bash", "-n", local_path]


# ======================================================================
# C4.6. 部署指令清單 —— **字串，本模組不執行**
# ======================================================================
def deployment_commands(variant="B2", sha=None):
    """
    未來要在**人工監督下**逐條執行的指令。回傳 (step, command) 清單。

    🔴 原子替換：先傳到 stage 檔、在遠端驗 sha256、確認 `bash -n` 通過，
       最後才 `mv`（同一檔案系統上的 rename 是原子的）。
       **不得**直接覆寫 GUARD_PATH —— 中途失敗會留下半個腳本，
       而那個腳本仍然是 forced command。
    """
    sha = sha or guard_sha256(variant)
    tgt = "%s@%s" % (REMOTE_USER, REMOTE_HOST)
    return [
        ("0_precheck_readonly",
         "ssh %s probe   # 目前仍是 B1 forced command，只能送唯讀動詞" % tgt),
        ("1_backup",
         "mkdir -p %s && cp -p %s %s/phase6_remote_guard.B1.$(date +%%Y%%m%%d_%%H%%M%%S).sh"
         % (GUARD_BACKUP_DIR, GUARD_PATH, GUARD_BACKUP_DIR)),
        ("2_backup_sha",
         "sha256sum %s %s/phase6_remote_guard.B1.*.sh" % (GUARD_PATH,
                                                          GUARD_BACKUP_DIR)),
        ("3_upload_stage",
         "scp <local staged %s file> %s:%s" % (variant, tgt, STAGE_PATH)),
        ("4_verify_sha",
         "echo '%s  %s' | sha256sum -c -" % (sha, STAGE_PATH)),
        ("5_syntax_check", "bash -n %s" % STAGE_PATH),
        ("6_ownership", "chown %s:%s %s" % (REQUIRED_OWNER, REQUIRED_GROUP,
                                            STAGE_PATH)),
        ("7_mode", "chmod %s %s" % (REQUIRED_MODE, STAGE_PATH)),
        ("8_atomic_replace", "mv -f %s %s" % (STAGE_PATH, GUARD_PATH)),
        ("9_confirm_mode", "ls -l %s && sha256sum %s" % (GUARD_PATH,
                                                         GUARD_PATH)),
    ]


# 🔴 authorized_keys **不在部署步驟裡**。forced command 指向同一個路徑，
#    換 guard 內容不需要動 authorized_keys；動它只會多一個出錯面。
AUTHORIZED_KEYS_CHANGE_REQUIRED = False


# ======================================================================
# C4.7. 部署後驗證順序
# ======================================================================
# 🔴 第一個 capability test **必須是唯讀的 probe**。
#    用 pause / restore 去「測試能不能 pause / restore」等於直接對現場
#    controller 動手 —— 那是操作，不是驗證。
POST_DEPLOY_VERIFICATION = (
    ("1_probe", "probe", "取得 stdout，供下一步解析"),
    ("2_variant", None, "parse_capability(stdout).variant == 'B2'"),
    ("3_capability", None,
     "capability_probe/status/loopcheck/pause/restore 五項齊全且合法"),
    ("4_loopcheck", "loopcheck", "確認 external loop 仍在推進"),
    ("5_status", "status", "確認注入路徑可用（唯讀，只讓 controller print）"),
    ("6_arbitrary_refused", None,
     "任意動詞仍須 REFUSED（exit 42）—— 以非控制字串驗證，不得用 pause/restore"),
)

# 部署後驗證階段**不得**出現的動詞
POST_DEPLOY_FORBIDDEN_VERBS = ("pause", "restore", "stop", "start")


def post_deploy_verification_verbs():
    return tuple(v for _, v, _ in POST_DEPLOY_VERIFICATION if v)


# ======================================================================
# C4.8. 回滾
# ======================================================================
ROLLBACK_TRIGGERS = (
    "probe_fail", "identity_mismatch", "capability_parse_fail",
    "loopcheck_fail", "status_fail", "unexpected_allowlist",
)


def rollback_commands(backup_file="<backup file from step 1>"):
    """B2 → B1 回滾。同樣是原子 rename，且回滾後必須重新驗。"""
    return [
        ("1_verify_backup_sha",
         "sha256sum %s   # 必須等於部署前記錄的 B1 sha256" % backup_file),
        ("2_stage", "cp -p %s %s" % (backup_file, STAGE_PATH)),
        ("3_syntax_check", "bash -n %s" % STAGE_PATH),
        ("4_mode", "chmod %s %s" % (REQUIRED_MODE, STAGE_PATH)),
        ("5_atomic_replace", "mv -f %s %s" % (STAGE_PATH, GUARD_PATH)),
        ("6_confirm", "ls -l %s && sha256sum %s" % (GUARD_PATH, GUARD_PATH)),
    ]


ROLLBACK_REVERIFICATION = ("probe", "loopcheck", "status")


def rollback_expected_capability(probe_stdout):
    """
    回滾後對 probe 輸出的期待：pause / restore 能力必須**回到不可用**。

    回傳 (ok, detail)。B1 沒有 capability 欄位 → `CAPABILITY_NOT_REPORTED`
    是**正確**結果；若仍看到 `capability_pause=true`，代表回滾沒生效。
    """
    cap = RA.parse_capability(probe_stdout)
    if cap.status == RA.CAP_NOT_REPORTED:
        return True, "B1（無 capability 欄位）—— 回滾成立"
    if cap.status == RA.CAP_REPORTED and cap.variant == "B1":
        return True, "guard 自報 B1 —— 回滾成立"
    if cap.can("pause") or cap.can("restore"):
        return False, "回滾後仍回報 pause/restore 可用 → 回滾未生效"
    return False, "回滾後 capability 狀態非預期：%s" % cap.status


# ======================================================================
def main(argv=None):
    print("=" * 72)
    print("  Phase 6.10-C4 Guard Deployment / Rollback Package（只準備）")
    print("=" * 72)
    for v in ("B1", "B2"):
        print(f"  {v} sha256 : {guard_sha256(v)}  ({len(guard_bytes(v))} bytes)")
    print(f"\n  目前部署  : {RA.DEPLOYED_GUARD_VARIANT}")
    print(f"  authorized_keys 需要改動 : {AUTHORIZED_KEYS_CHANGE_REQUIRED}")
    print(f"  權限要求  : {REQUIRED_OWNER}:{REQUIRED_GROUP} {REQUIRED_MODE}")
    print("\n  [部署步驟]（**本模組不執行**）")
    for k, c in deployment_commands("B2"):
        print(f"    {k:<20} {c}")
    print("\n  [部署後驗證]")
    for k, verb, note in POST_DEPLOY_VERIFICATION:
        print(f"    {k:<22} verb={str(verb):<10} {note}")
    print(f"    → 驗證階段實際送出的動詞：{list(post_deploy_verification_verbs())}")
    print(f"    → 明文禁止：{list(POST_DEPLOY_FORBIDDEN_VERBS)}")
    print("\n  [回滾步驟]")
    for k, c in rollback_commands():
        print(f"    {k:<22} {c}")
    print(f"    → 回滾後重驗：{list(ROLLBACK_REVERIFICATION)}")
    print("\n  ⚠ 本輪未部署、未上傳、未修改 authorized_keys。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
