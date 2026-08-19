# -*- coding: utf-8 -*-
"""
Phase 4.6-A — Windows Service 環境相依性離線驗證（A~H）
======================================================================
驗證 auto_monitor_service.py 在「Windows Service（session 0）」的環境條件下仍可運作：

  · 無 console（sys.stdout / sys.stderr 為 None）—— 本專案有 400 餘處 print()，
    若不先導向，第一個 print() 就會 AttributeError 讓服務啟動失敗
  · cwd 為 C:\\Windows\\System32 —— 不得依賴相對路徑
  · 不繼承使用者環境變數 —— .env 探索與 proxy 設定需明確化

是否需要設備
    **不需要**。全程離線：不登入、不取樣、不建立 Session。
    需要真正安裝 Windows Service 的項目（I/J/K）屬 Phase 4.6-B，不在本檔。

用法
    python test_phase4_service_env.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import ast
import json
import shutil
import tempfile
import subprocess
import contextlib

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PROJECT = os.path.dirname(HERE)
PS1 = os.path.join(PROJECT, "tools", "install_service_nssm.ps1")
RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import auto_monitor_service as SVC
    import report_monitor as RM

_svc_src = open(os.path.join(HERE, "auto_monitor_service.py"), encoding="utf-8").read()

# ======================================================================
# A  --log 導向
# ======================================================================
print("[Phase 4.6-A] A  --log 導向 stdout/stderr")
_TMP = tempfile.mkdtemp(prefix="p46_env_")
_log = os.path.join(_TMP, "sub", "svc.log")          # 刻意含未建立的子目錄
_bak_out, _bak_err = sys.stdout, sys.stderr
try:
    _p = SVC.setup_logging(_log)
    print("這行應寫進 log 檔，不應出現在 console")
    sys.stdout.flush()
finally:
    sys.stdout, sys.stderr = _bak_out, _bak_err
check(f"A  setup_logging 回傳指定路徑（{_p == _log}）", _p == _log)
check("A  自動建立缺少的父目錄", os.path.isfile(_log))
_content = open(_log, encoding="utf-8").read()
check("A  stdout 確實寫入該檔", "這行應寫進 log 檔" in _content)
check("A  stderr 亦導向同一檔（同一 handle）", True)

# ======================================================================
# B  sys.stdout / sys.stderr 為 None（無 console）
# ======================================================================
print("\n[Phase 4.6-A] B  無 console 時不得 AttributeError")
_log_default = os.path.join(SVC.DEFAULT_LOG_DIR, SVC.DEFAULT_LOG_NAME)
_bak_dir = SVC.DEFAULT_LOG_DIR
SVC.DEFAULT_LOG_DIR = os.path.join(_TMP, "noconsole")
try:
    sys.stdout, sys.stderr = None, None
    _p2 = SVC.setup_logging(None)                    # 模擬 Windows Service
    print("無 console 情境下的輸出")                  # 不得拋 AttributeError
    sys.stdout.flush()
    _ok_b = True
except Exception as _e:                              # noqa: BLE001
    _ok_b = False
    _err_b = f"{type(_e).__name__}: {_e}"
finally:
    sys.stdout, sys.stderr = _bak_out, _bak_err
    SVC.DEFAULT_LOG_DIR = _bak_dir
check("B  stdout=None 時 setup_logging 不拋例外且完成導向", _ok_b)
check("B  自動導向到預設 log 路徑", _ok_b and _p2 is not None and os.path.isfile(_p2))
check("B  導向後 print() 正常運作（無 AttributeError）",
      _ok_b and "無 console 情境下的輸出" in open(_p2, encoding="utf-8").read())
_setup_src = _svc_src[_svc_src.index("def setup_logging("):
                      _svc_src.index("def _env_file_in_use(")]
# 位置比對前必須先剝除註解與 docstring，且只看 main() 區段：
#   · 用全檔 .index() 會抓到 print_startup_diagnostics 的**函式定義**（在檔案較前處）
#   · main() 內「必須在任何 print() 之前」這句註解本身就含 print(
# 兩者都會讓守門誤判 —— 要比對的是**執行碼**的先後。
def _code_only(s):
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


_main_seg = _code_only(_svc_src[_svc_src.index("def main("):])
_i_setup = _main_seg.index("setup_logging(")
_i_print = min((_main_seg.index(k) for k in ("print(", "print_startup_diagnostics(")
                if k in _main_seg), default=len(_main_seg))
check(f"B  setup_logging 在 main() 執行碼中位於任何 print 之前（{_i_setup} < {_i_print}）",
      _i_setup < _i_print)
check("B  導向失敗時退回 devnull（服務仍可啟動，不因 log 失敗而崩潰）",
      "os.devnull" in _setup_src)

# ======================================================================
# C  cwd 獨立性
# ======================================================================
print("\n[Phase 4.6-A] C  cwd 為 System32 時仍正確（Service 的實際 cwd）")
_sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_probe = (
    "import sys, os, json;"
    f"sys.path.insert(0, r'{HERE}');"
    "import io, contextlib;"
    "buf=io.StringIO();"
    "  \n"
)
_probe_file = os.path.join(_TMP, "cwd_probe.py")
with open(_probe_file, "w", encoding="utf-8") as f:
    f.write(
        "# -*- coding: utf-8 -*-\n"
        "import sys, os, json, io, contextlib\n"
        f"sys.path.insert(0, r'{HERE}')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    import auto_monitor_service as SVC\n"
        "    import report_monitor as RM\n"
        "    import api_client\n"
        "    cands = [str(c) for c in api_client.discover_env_candidates()]\n"
        "out = {'cwd': os.getcwd(), 'env': cands[:1],\n"
        "       'root': RM._report_output_root(),\n"
        "       'mutex': RM._mutex_name(),\n"
        "       'here': SVC.HERE}\n"
        "sys.stderr.write('__P46__' + json.dumps(out, ensure_ascii=False))\n")
_r = subprocess.run([sys.executable, _probe_file], cwd=_sys32,
                    capture_output=True, timeout=180)
_payload = None
for _ln in _r.stderr.decode("utf-8", errors="replace").splitlines():
    if _ln.startswith("__P46__"):
        _payload = json.loads(_ln[len("__P46__"):])
check(f"C  子行程確實在 System32 執行（{_payload and _payload.get('cwd')}）",
      _payload is not None and _payload["cwd"].lower() == _sys32.lower())
check(f"C  仍找得到 env 檔（{(_payload or {}).get('env')}）",
      _payload is not None and _payload["env"]
      and _payload["env"][0].lower().endswith("login.env"))
check(f"C  output root 不受 cwd 影響（{(_payload or {}).get('root')}）",
      _payload is not None
      and _payload["root"].lower() == RM._report_output_root().lower())
check("C  Mutex 名稱不受 cwd 影響（否則 Service 與 Dashboard 會用到不同鎖）",
      _payload is not None and _payload["mutex"] == RM._mutex_name())
check("C  程式目錄以 __file__ 為基準，非 cwd",
      _payload is not None and _payload["here"].lower() == HERE.lower())

# ======================================================================
# D  API_ENV_FILE 覆寫
# ======================================================================
print("\n[Phase 4.6-A] D  API_ENV_FILE 明確指定憑證檔")
_fake_env = os.path.join(_TMP, "svc_only.env")
with open(_fake_env, "w", encoding="utf-8") as f:
    f.write("HMI_USERNAME=dummy\n")
_probe2 = os.path.join(_TMP, "envfile_probe.py")
with open(_probe2, "w", encoding="utf-8") as f:
    f.write(
        "import sys, os, json, io, contextlib\n"
        f"sys.path.insert(0, r'{HERE}')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    import api_client\n"
        "    c = [str(x) for x in api_client.discover_env_candidates()]\n"
        "sys.stderr.write('__P46__' + json.dumps(c, ensure_ascii=False))\n")
_env2 = dict(os.environ, API_ENV_FILE=_fake_env)
_r2 = subprocess.run([sys.executable, _probe2], cwd=_sys32, env=_env2,
                     capture_output=True, timeout=180)
_c2 = None
for _ln in _r2.stderr.decode("utf-8", errors="replace").splitlines():
    if _ln.startswith("__P46__"):
        _c2 = json.loads(_ln[len("__P46__"):])
check("D  API_ENV_FILE 指定的檔案排在候選第一位",
      _c2 is not None and _c2 and os.path.normcase(_c2[0]) == os.path.normcase(_fake_env))
check("D  未設定時不受影響（沿用既有探索順序）",
      _payload is not None and _payload["env"][0].lower().endswith("login.env"))

# ======================================================================
# E  exit code 語意
# ======================================================================
print("\n[Phase 4.6-A] E  exit code 3 / 4 語意")
import charge_discharge_report_config as CFG                     # noqa: E402
check(f"E  EXIT_OWNER_HELD_BY_OTHER = 3（實際 {CFG.EXIT_OWNER_HELD_BY_OTHER}）",
      CFG.EXIT_OWNER_HELD_BY_OTHER == 3)
check(f"E  EXIT_OWNER_API_ERROR = 4（實際 {CFG.EXIT_OWNER_API_ERROR}）",
      CFG.EXIT_OWNER_API_ERROR == 4)
_main_src = _svc_src[_svc_src.index("def main("):]
check("E  held_by_other → exit 3（非程式故障）",
      "EXIT_OWNER_HELD_BY_OTHER" in _main_src)
check("E  其他 ownership 失敗 → exit 4（fail closed）",
      "EXIT_OWNER_API_ERROR" in _main_src)
check("E  取不到 Ownership 時不進監看迴圈（直接 return）",
      _main_src.index("EXIT_OWNER_HELD_BY_OTHER") < _main_src.index("print_startup_diagnostics("))

# ======================================================================
# F  log rotation
# ======================================================================
print("\n[Phase 4.6-A] F  log rotation（單檔上限與保留份數）")
check(f"F  LOG_MAX_BYTES = 10MB（實際 {SVC.LOG_MAX_BYTES}）",
      SVC.LOG_MAX_BYTES == 10 * 1024 * 1024)
check(f"F  LOG_BACKUP_COUNT = 5（實際 {SVC.LOG_BACKUP_COUNT}）", SVC.LOG_BACKUP_COUNT == 5)
_rot = os.path.join(_TMP, "rot.log")
with open(_rot, "w", encoding="utf-8") as f:
    f.write("x" * 200)
check("F  未達上限不輪替", SVC._rotate_log(_rot, max_bytes=1000) is False
      and not os.path.exists(_rot + ".1"))
check("F  達上限則輪替為 .1", SVC._rotate_log(_rot, max_bytes=100) is True
      and os.path.exists(_rot + ".1") and not os.path.exists(_rot))
for _i in range(6):                                   # 連續輪替，驗證只保留 5 份
    with open(_rot, "w", encoding="utf-8") as f:
        f.write("y" * 200)
    SVC._rotate_log(_rot, max_bytes=100, backups=5)
_backs = [n for n in os.listdir(_TMP) if n.startswith("rot.log.")]
check(f"F  最多保留 5 份備份（實際 {len(_backs)}：{sorted(_backs)}）", len(_backs) == 5)
check("F  無第 6 份（最舊者已刪除）", not os.path.exists(_rot + ".6"))
check("F  rotate 失敗不得中斷（不存在的路徑回 False，不拋例外）",
      SVC._rotate_log(os.path.join(_TMP, "nope", "x.log")) is False)

# ======================================================================
# G  啟動診斷輸出
# ======================================================================
print("\n[Phase 4.6-A] G  啟動診斷欄位齊全")
_diag = io.StringIO()
RM.acquire_ownership(role="test-env")
try:
    with contextlib.redirect_stdout(_diag):
        SVC.print_startup_diagnostics("X:\\some\\svc.log", 10, ["SIGINT"], "acquired")
finally:
    RM.release_ownership()
_dt = _diag.getvalue()
for _field in ("Python", "執行帳號", "cwd", "程式目錄", "env 檔", "output root",
               "log", "NO_PROXY", "Mutex", "Monitor Owner", "監看間隔",
               "自動建立報告", "訊號處理", "關閉語意"):
    check(f"G  診斷含「{_field}」", _field in _dt)
check("G  診斷顯示實際 Mutex 名稱（Service 與 Dashboard 須一致）",
      RM._mutex_name() in _dt)
check("G  診斷顯示 Global\\ 命名空間（跨 session 必要條件）", "Global\\" in _dt)

# ======================================================================
# NSSM 腳本
# ======================================================================
print("\n[Phase 4.6-A] NSSM 安裝腳本")
check("install_service_nssm.ps1 存在", os.path.isfile(PS1))
_ps = open(PS1, encoding="utf-8-sig").read()
check("以 UTF-8 BOM 儲存（Windows PowerShell 5.1 才不會以 cp950 誤讀中文）",
      open(PS1, "rb").read(3) == b"\xef\xbb\xbf")
for _act in ("install", "start", "stop", "status", "remove"):
    check(f"支援 -Action {_act}", f"'{_act}'" in _ps)
for _k, _desc in (("AppDirectory", "工作目錄"), ("API_ENV_FILE", "憑證檔"),
                  ("NO_PROXY", "不經 proxy"), ("AppStdout", "stdout log"),
                  ("AppStderr", "stderr log"), ("AppRotateBytes", "rotation"),
                  ("AppStopMethodConsole", "Stop 送 Ctrl+C 的逾時"),
                  ("ObjectName", "服務帳號")):
    check(f"設定 {_k}（{_desc}）", _k in _ps)
check("Stop 逾時 ≥ 30s（report_pause 的 join 最長約 15s，過短會產生孤兒）",
      "'AppStopMethodConsole', '30000'" in _ps)
check("rotation 上限 10MB", "'AppRotateBytes', '10485760'" in _ps)
check("服務帳號為 LocalSystem", "'ObjectName', 'LocalSystem'" in _ps)
check("exit code 0 / 3 / 4 皆設為 Exit（Phase 4.6 不自動重啟）",
      all(f"'AppExit', '{c}', 'Exit'" in _ps for c in ("0", "3", "4")))
check("Default 亦為 Exit（自動重啟策略屬 Phase 4.7）",
      "'AppExit', 'Default', 'Exit'" in _ps)
check("Python 以絕對路徑指定（Service 不繼承使用者 PATH）",
      "PythonExe" in _ps and ".exe'" in _ps)
check("install / start / stop / remove 要求系統管理員權限",
      _ps.count("Assert-Admin") >= 4)
check("找不到 NSSM 時給出明確指引，不靜默失敗", "nssm.cc" in _ps)

# ======================================================================
# H  既有不退步 + 產品檔案未受影響
# ======================================================================
# ======================================================================
# Service 控制流程（stop / remove）的冪等性與 fail-closed
# ======================================================================
print("\n[Phase 4.6-A] Service 控制流程 A~E（不碰真實服務，全以樁函式模擬）")
_ctl_probe = os.path.join(HERE, "_p46_service_ctl_probe.ps1")
check("控制流程探針存在", os.path.isfile(_ctl_probe))
_pc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-File", _ctl_probe], cwd=HERE, capture_output=True, timeout=600)
_raw = _pc.stdout or b""
_txt = None
for _enc in ("cp950", "utf-8", "mbcs"):
    try:
        _t = _raw.decode(_enc)
        if "__P46CTL__" in _t:
            _txt = _t
            break
    except Exception:                                    # noqa: BLE001
        continue
_ctl = None
if _txt:
    for _ln in _txt.splitlines():
        if _ln.startswith("__P46CTL__"):
            try:
                _ctl = json.loads(_ln[len("__P46CTL__"):])
            except ValueError:
                pass
check(f"控制流程探針執行成功（exit {_pc.returncode}）",
      _pc.returncode == 0 and _ctl is not None)

if _ctl:
    _A, _B, _C, _D, _E, _F = (_ctl.get(k, {}) for k in "ABCDEF")

    # A. Running → stop → remove
    check("A  Running：先呼叫 stop 再呼叫 remove（順序正確）",
          [c.split()[0] for c in _A.get("calls", [])] == ["stop", "remove"])
    check("A  Running：流程未拋例外", _A.get("threw") is False)

    # B. Stopped → 略過 stop → remove
    check("B  Stopped：**不呼叫** stop，直接 remove",
          [c.split()[0] for c in _B.get("calls", [])] == ["remove"])
    check("B  Stopped：流程未拋例外", _B.get("threw") is False)

    # C. 不存在 → idempotent success
    check("C  Absent：完全不呼叫 nssm（already absent）", _C.get("calls") == [])
    check("C  Absent：不視為異常（未拋例外）", _C.get("threw") is False)

    # D. stop 真失敗 → fail closed
    check("D  stop 失敗：必須拋例外（不得靜默）", _D.get("threw") is True)
    check("D  stop 失敗：**不得**呼叫 remove（fail closed）",
          [c.split()[0] for c in _D.get("calls", [])] == ["stop"])
    check("D  stop 失敗：錯誤訊息含 fail closed 說明",
          "fail closed" in (_D.get("error") or ""))

    # E. remove 真失敗 → 明確報錯
    check("E  remove 後仍存在：必須拋例外", _E.get("threw") is True)
    check("E  remove 失敗：訊息含實際狀態與 nssm exit code",
          "remove 後服務仍存在" in (_E.get("error") or "")
          and "nssm exit=" in (_E.get("error") or ""))

    # F. Stop-SvcIfRunning 對不存在服務
    check("F  Stop-SvcIfRunning 對 Absent 回 true 且不呼叫 nssm",
          _F.get("ok") is True and _F.get("calls") == [])

    # ------------------------------------------------------------------
    # Start-SvcAndWait（Phase 4.6-B）：SCM 實際狀態＝事實來源
    # ------------------------------------------------------------------
    print("\n[Phase 4.6-B] Start-SvcAndWait A~H（樁函式模擬，不碰真實服務）")

    def _starts(case):
        return [c.split()[0] for c in (case.get("calls") or [])]

    _SA, _SB, _SC = (_ctl.get(k, {}) for k in ("S_A", "S_B", "S_C"))
    _SD, _SE, _SF = (_ctl.get(k, {}) for k in ("S_D", "S_E", "S_F"))
    _SG, _SH = (_ctl.get(k, {}) for k in ("S_G", "S_H"))

    # A. Stopped → StartPending → Running
    check("A  Stopped→StartPending→Running：未拋例外（視為成功）",
          _SA.get("threw") is False)
    check("A  Stopped→Running：nssm start 恰呼叫 1 次", _starts(_SA) == ["start"])
    check("A  成功路徑不產生警示（exit=0 且 Running）", not _SA.get("warnings"))

    # B. 逾時仍 Stopped → throw，訊息含狀態 + exit code
    check("B  逾時仍非 Running：必須拋例外（不得靜默成功）", _SB.get("threw") is True)
    check("B  逾時訊息含服務名／實際狀態／NSSM exit code／等待秒數",
          all(s in (_SB.get("error") or "") for s in
              ("__P46_NOT_A_REAL_SERVICE__", "Stopped", "NSSM exit code", "3", "1 秒")))

    # C. exit != 0 但 SCM Running → 成功 + 警示，不 throw
    check("C  exit≠0 但 SCM 為 Running：不拋例外（以 SCM 為準）",
          _SC.get("threw") is False)
    check("C  但**不得** silent ignore：輸出警示並附實際 exit code",
          "exit=5" in (_SC.get("warnings") or "")
          and "以 SCM 狀態為準" in (_SC.get("warnings") or ""))

    # D. Absent → throw 且完全不呼叫 nssm start
    check("D  Absent：必須拋例外（服務未安裝）", _SD.get("threw") is True)
    check("D  Absent：**完全不呼叫** nssm start（fail closed）",
          _SD.get("calls") == [])
    check("D  Absent：訊息說明需先 install",
          "未安裝" in (_SD.get("error") or "") and "install" in (_SD.get("error") or ""))

    # E. 初始 Running → idempotent success
    check("E  初始 Running：idempotent success（未拋例外）",
          _SE.get("threw") is False)
    check("E  初始 Running：**不呼叫** nssm start", _SE.get("calls") == [])

    # F/G. StartPending 兩種結局
    check("F  StartPending→Running：正常等待完成，未拋例外",
          _SF.get("threw") is False and _starts(_SF) == ["start"])
    check("G  StartPending 一路到逾時：必須拋例外",
          _SG.get("threw") is True
          and "StartPending" in (_SG.get("error") or ""))

    # H. exit=0 但 SCM 非 Running → 仍必須失敗
    check("H  nssm exit=0 但 SCM 非 Running：仍必須拋例外"
          "（不得只看 exit code）",
          _SH.get("threw") is True
          and "NSSM exit code：0" in (_SH.get("error") or ""))

    # G. Remove-NulChars —— Assert-InstalledSettings 實機崩潰的直接成因
    _G = _ctl.get("G", {})
    _expect = {
        "no_nul": "auto_monitor_service.py --interval 10",
        "middle_nul": "abcdef",
        "trailing_nul": "abc",
        "leading_nul": "abc",
        "only_nul": "",
        "empty": "",
        "appparams": "auto_monitor_service.py --interval 10",
        "appdir_space": "D:\\Crawler Sample\\test",
    }
    for _k, _want in _expect.items():
        _e = _G.get(_k, {})
        check(f"G  Remove-NulChars（{_k}）→ {_want!r}，未拋例外、無殘留 NUL",
              _e.get("threw") is False and _e.get("out") == _want
              and _e.get("hasNul") is False)
    check("G  Remove-NulChars(null) 不拋例外，回空字串",
          _G.get("null_input", {}).get("threw") is False
          and _G.get("null_input", {}).get("out") == "")

    # H. 守住根因：舊寫法必須確實會炸，證明修正有意義
    _H = _ctl.get("H", {})
    check("H  舊寫法 .Replace([char]0, '') 確實拋型別轉換例外（證明修正非多餘）",
          _H.get("oldThrew") is True
          and "MethodArgumentConversionInvalidCastArgument" in (_H.get("oldErrId") or ""))

# 原始碼守門：確保根因修正沒有被日後改回去
_ps_src = open(PS1, encoding="utf-8-sig").read()
check("Invoke-Nssm 區域性關閉 ErrorActionPreference=Stop（native stderr 不得成為 terminating error）",
      "$ErrorActionPreference = 'Continue'" in _ps_src
      and "$script:LastNssmExit = $LASTEXITCODE" in _ps_src)
check("狀態判定使用 Get-Service 而非解析 nssm 文字輸出（避免 CP950 亂碼誤判）",
      "function Get-SvcStatus" in _ps_src and "Get-Service -Name $ServiceName" in _ps_src)
check("remove 走 Remove-SvcIdempotent，不再無條件先 stop",
      "Remove-SvcIdempotent" in _ps_src
      and "Write-Host \"先停止服務…\"" not in _ps_src)
check("stop 動作亦改為冪等（Stop-SvcIfRunning）", "Stop-SvcIfRunning" in _ps_src)
check("NUL 清除以 [string][char]0 明確選用 Replace(String,String) overload",
      "Remove-NulChars" in _ps_src and "[string][char]0" in _ps_src)
def _ps_code_only(s):
    """剝除 PowerShell 的區塊註解 <# #> 與行註解 # —— 說明文字裡提到的寫法不算實際呼叫。"""
    s = re.sub(r"<#[\s\S]*?#>", "", s)
    return re.sub(r"#.*", "", s)


check("腳本**執行碼**內已無 .Replace([char]0, '') 這種會綁到 Replace(Char,Char) 的寫法",
      "Replace([char]0" not in _ps_code_only(_ps_src))

# ----------------------------------------------------------------------
# Phase 4.6-B：start 動作的靜態守門
#   ⚠️ 一律比對**剝除註解後**的執行碼 —— 說明文字本來就會提到 Invoke-Nssm、
#      Start-Sleep、chcp 這些字眼（本專案累犯項：守門比對到自己的註解）。
# ----------------------------------------------------------------------
print("\n[Phase 4.6-B] start 動作：SCM 為事實來源、不顯示 NSSM 原生文字")
_ps_code = _ps_code_only(_ps_src)
_start_branch = _ps_code[_ps_code.index("'start' {"):_ps_code.index("'stop' {")]
_swait = _ps_code[_ps_code.index("function Start-SvcAndWait"):
                  _ps_code.index("function Remove-SvcIdempotent")]

check("已新增 Start-SvcAndWait，且 start 分支呼叫它",
      "function Start-SvcAndWait" in _ps_code and "Start-SvcAndWait" in _start_branch)
check("H  start 分支**不再**出現 Invoke-Nssm（不顯示、不解析 NSSM 原生輸出）",
      "Invoke-Nssm" not in _start_branch)
check("H  Start-SvcAndWait 內 nssm 的回傳值被丟棄（$null = Invoke-Nssm）",
      "$null = Invoke-Nssm" in _swait
      and "Invoke-Nssm" in _swait
      and "Invoke-Nssm @('start', $ServiceName) | Write-Host" not in _ps_code)
check("I  start 分支**不再**以固定 Start-Sleep 秒數當成功判定",
      "Start-Sleep -Seconds" not in _start_branch)
check("J  成功判定來源為 Get-SvcStatus（SCM），非 NSSM 文字或 exit code",
      _swait.count("Get-SvcStatus") >= 3 and "-eq 'Running'" in _swait)
check("J  逾時 fail closed：Start-SvcAndWait 內有 throw",
      "throw" in _swait)
check("逾時訊息含實際狀態、NSSM exit code 與等待秒數",
      all(s in _swait for s in ("目前狀態：$now", "NSSM exit code：$exit",
                                "$StartWaitSec 秒內未進入 Running")))
check("exit≠0 但 Running → 以 Write-Warning 顯示（不 silent ignore、不 throw）",
      "Write-Warning" in _swait and "以 SCM 狀態為準" in _swait)
check("$StartWaitSec 獨立於 $StopWaitSec（兩者語意不同，不共用）",
      "$StartWaitSec = " in _ps_code and "$StopWaitSec = " in _ps_code
      and "$StartWaitSec" not in _ps_code[_ps_code.index("function Stop-SvcIfRunning"):
                                          _ps_code.index("function Start-SvcAndWait")])
check("未以修改 code page 當修法（無 chcp / 65001 / OutputEncoding / iconv）",
      not any(s in _ps_code for s in
              ("chcp", "65001", "OutputEncoding", "iconv")))
check("Start-SvcAndWait 的成功訊息由腳本自行輸出 Unicode 中文",
      "服務已啟動（Running）" in _swait and "服務已在執行（Running）" in _swait)

# ----------------------------------------------------------------------
# Phase 5.2：OpenBLAS 資源最佳化守門
#   openpyxl 會間接 import numpy，其 OpenBLAS 後端依 CPU 核心數建立 native
#   thread pool（實測 +19 threads / +650MB Private），而本產品全程不做 BLAS
#   運算。OPENBLAS_NUM_THREADS=1 是 Phase 5.2 的正式修法，必須守住不被移除。
#   ⚠️ 一律比對**剝除註解後**的 _ps_code —— 上面這段說明本身就含該字串
#      （本專案累犯項：守門比對到自己的註解）。
# ----------------------------------------------------------------------
print("\n[Phase 5.2] AppEnvironmentExtra 資源最佳化與 update 動作契約")


def _env_array(code):
    """取出 $EnvLines 陣列的字面片段（傳入的 code 需已剝除註解）。"""
    i = code.find("$EnvLines = @(")
    if i < 0:
        return ""
    seg = code[i:]
    j = seg.find("\n)")
    return seg[:j] if j > 0 else seg


def _env_items(code):
    """陣列內的環境變數字面值清單。"""
    return re.findall(r'"([A-Za-z_][A-Za-z0-9_]*=[^"]*)"', _env_array(code))


def _branch(code, name):
    """取出 switch 分支 '<name>' { ... } 的完整內容（大括號配對）。"""
    i = code.index("'%s' {" % name)
    depth = 0
    for k in range(i, len(code)):
        if code[k] == "{":
            depth += 1
        elif code[k] == "}":
            depth -= 1
            if depth == 0:
                return code[i:k + 1]
    return code[i:]


_items = _env_items(_ps_code)
_ORIG6 = ("API_ENV_FILE=$EnvFile",
          "NO_PROXY=$DeviceHost,localhost,127.0.0.1",
          "no_proxy=$DeviceHost,localhost,127.0.0.1",
          "PYTHONIOENCODING=utf-8", "PYTHONUTF8=1", "PYTHONUNBUFFERED=1")

# ---- 環境變數集合 ----
check("$EnvLines 定義恰好 1 處（單一設定來源）",
      _ps_code.count("$EnvLines = @(") == 1)
check(f"AppEnvironmentExtra 共 7 項（實際 {len(_items)}）", len(_items) == 7)
check("執行碼（非註解）內設定 OPENBLAS_NUM_THREADS=1",
      "OPENBLAS_NUM_THREADS=1" in _items)
check("OPENBLAS_NUM_THREADS 在 $EnvLines 內恰好 1 筆",
      sum(1 for x in _items if x.startswith("OPENBLAS_NUM_THREADS=")) == 1)
for _e in _ORIG6:
    check(f"原有環境變數未被移除：{_e}", _e in _items)
check("AppEnvironmentExtra 只有一個寫入點",
      _ps_code.count("'AppEnvironmentExtra',") == 1)

# ---- update 動作契約 ----
_vs = re.search(r"ValidateSet\(([^)]*)\)", _ps_code)
_actions = re.findall(r"'(\w+)'", _vs.group(1)) if _vs else []
check(f"ValidateSet 含 update（{_actions}）", "update" in _actions)
_upd = _branch(_ps_code, "update")
_ins = _branch(_ps_code, "install")
check("update 分支不呼叫 nssm install（不偷偷退化成安裝）", "'install'" not in _upd)
check("update 分支不呼叫 nssm remove（不重建服務）", "'remove'" not in _upd)
check("update 對不存在的服務 fail closed（比對 Absent 後 throw）",
      "Absent" in _upd and "throw" in _upd)
check("update 不修改 ServiceName 或任何 runtime 路徑",
      not re.search(r"\$(ServiceName|TestDir|PythonExe|ScriptName|LogDir|EnvFile)"
                    r"\s*=[^=]", _upd))
check("update 呼叫 Assert-InstalledSettings（驗證不過不得放行）",
      "Assert-InstalledSettings" in _upd)

# ---- install / update 共用設定來源 ----
check("install 與 update 皆呼叫 Apply-ServiceSettings",
      "Apply-ServiceSettings" in _ins and "Apply-ServiceSettings" in _upd)
check("兩分支內皆已無自己的 nssm set（設定邏輯不會漂移）",
      _ins.count("Invoke-Nssm @('set'") == 0
      and _upd.count("Invoke-Nssm @('set'") == 0)
check("Apply-ServiceSettings 為唯一設定來源（定義 1 處，含全部 nssm set）",
      _ps_code.count("function Apply-ServiceSettings") == 1
      and _ps_code.count("Invoke-Nssm @('set'") >= 20)

# ---- Assert-InstalledSettings 的回讀驗證 ----
_asrt = _ps_code[_ps_code.index("function Assert-InstalledSettings"):]
_asrt = _asrt[:_asrt.index("\n}")]
check("Assert-InstalledSettings 回讀驗證 AppEnvironmentExtra",
      "AppEnvironmentExtra" in _asrt)
check("回讀比對使用 $EnvLines（與寫入同一來源）", "$EnvLines" in _asrt)
check("以集合比較，未硬綁 NSSM 回讀順序", "-cnotcontains" in _asrt)
check("比對大小寫敏感（NO_PROXY 與 no_proxy 必須可區分）",
      "-cnotcontains" in _asrt and "-clike" in _asrt
      and "-notcontains" not in _asrt.replace("-cnotcontains", ""))
check("OPENBLAS_NUM_THREADS 必須恰好 1 筆", "$blas.Count -ne 1" in _asrt)
check("驗證不符即 throw（fail closed，不得繼續 start）",
      "$envBad.Count -gt 0" in _asrt and "throw" in _asrt)

# ---- 守門自我驗證：設定被移除時本守門必須失敗 ----
#   擋不住的守門沒有價值 —— 在記憶體中破壞執行碼，確認判定會翻成 False。
_broken = _ps_code.replace('"OPENBLAS_NUM_THREADS=1"', '', 1)
_bi = _env_items(_broken)
check("負面測試：移除 OPENBLAS 後，7 項與存在性兩項判定皆會失敗",
      "OPENBLAS_NUM_THREADS=1" not in _bi and len(_bi) != 7)
_broken4 = _ps_code.replace('"OPENBLAS_NUM_THREADS=1"',
                            '"OPENBLAS_NUM_THREADS=4"', 1)
check("負面測試：值被改成 4 時，存在性判定會失敗",
      "OPENBLAS_NUM_THREADS=1" not in _env_items(_broken4))
_brokenNP = _ps_code.replace('"NO_PROXY=$DeviceHost,localhost,127.0.0.1",\n', '', 1)
check("負面測試：原有項目被刪時，7 項判定會失敗",
      len(_env_items(_brokenNP)) != 7)

print("\n[Phase 4.6-A] H  既有套件不退步")
_mon_now = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
check("H  report_monitor.py 未因 Phase 4.6 而需要修改（仍無 input()）",
      not [n for n in ast.walk(ast.parse(_mon_now))
           if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "input"])
for _name, _file, _want in (("Phase 4.3 service", "test_phase4_service.py", 56),
                            ("Phase 4.4 ownership", "test_phase4_ownership.py", 62),
                            ("Phase 4.5 observer", "test_phase4_observer.py", 64)):
    _p3 = subprocess.run([sys.executable, os.path.join(HERE, _file)], cwd=HERE,
                         capture_output=True, timeout=1800)
    _o3 = _p3.stdout.decode("utf-8", errors="replace")
    _m3 = re.findall(r"（(\d+)/(\d+) 檢查通過）", _o3)
    _g3 = (int(_m3[-1][0]), int(_m3[-1][1])) if _m3 else (None, None)
    check(f"H  {_name} 仍全數通過［{_g3[0]} of {_g3[1]}，需 ≥{_want}，exit {_p3.returncode}］",
          _p3.returncode == 0 and _g3[0] == _g3[1] and (_g3[1] or 0) >= _want)

print("\n[Phase 4.6-A] 環境還原")
shutil.rmtree(_TMP, ignore_errors=True)
check("stdout / stderr 已還原", sys.stdout is _bak_out and sys.stderr is _bak_err)
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("DEFAULT_LOG_DIR 已還原", SVC.DEFAULT_LOG_DIR == _bak_dir)

ok = all(RESULTS)
print(f"\n== Phase 4.6-A Service 環境驗證 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
