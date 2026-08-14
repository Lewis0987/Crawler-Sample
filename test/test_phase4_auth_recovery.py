# -*- coding: utf-8 -*-
"""
Phase 4.6-B — authenticated session 失效偵測與復原（A~O）
======================================================================
要修的缺陷（實機發現）
    Service 於 2026-08-13 11:03:15 登入成功；11:33:15（整整 30:00）起
    getRunMode / getScheduleSwitch 開始回 None，之後約 100 分鐘、598 筆監看
    紀錄完全沒有恢復（mode=unknown、sched=None）。同一時間 guest 端點
    （SOC / 功率 / 故障旗標 / PCS 狀態）全部正常，另外新登入的 client 也讀得到
    smart / True —— 設備正常，是那個長壽 client 的登入態失效且無人清除。

    後果不是顯示問題：auto_schedule_check 條件 A 要求 mode_code == "smart"，
    讀不到就永遠是 "unknown" → **Service 再也不可能自動建立報告**，
    直接打掉 Phase 4「不開 Dashboard 也能自動建報告」的核心目標。

修法（Monitor 層，最小範圍）
    authenticated 端點同輪全失敗 + 至少一支 guest 端點成功 → 連續達
    AUTH_LOST_STREAK 輪 → 丟棄快取 client → 由**既有**的 _monitor_client()
    重新登入恢復。不新增第二套登入狀態機、不碰 Ownership / Writer Gate /
    ReportSession 生命週期。

是否需要設備
    **不需要**。client 與 read_all 全部以假物件注入；零網路 I/O。

安全性
    不送控制命令；不建立/修改/刪除任何真實 Session（Session 一律建在臨時目錄）；
    不碰正式 output；不修改 config（有還原檢查）；不執行 Git；
    Ownership 一律以臨時 output root 導出的專屬 Mutex，不碰正式部署那一顆。

用法
    python test_phase4_auth_recovery.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import ast
import time
import shutil
import tempfile
import threading
import contextlib

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


ROOT = tempfile.mkdtemp(prefix="p46b_auth_")
_BAK = (CDR.read_all, CDR.ApiClient, RM._report_output_root, CDR._fetch_cell_packs)
RM._report_output_root = lambda: ROOT

A_RUN, A_SCH = RM.AUTH_ENDPOINTS
GUESTS = list(RM.GUEST_ENDPOINTS)


class FakeClient:
    """假 client：只用來辨識身分（generation），不做任何網路存取。"""
    _seq = [0]

    def __init__(self, *_a, **_k):
        FakeClient._seq[0] += 1
        self.generation = FakeClient._seq[0]
        self.logged_in = False

    def login_hmi(self, *_a, **_k):
        if LOGIN["fail"] > 0:
            LOGIN["fail"] -= 1
            LOGIN["attempts"] += 1
            return None
        LOGIN["attempts"] += 1
        self.logged_in = True
        return "fake-token"                 # 僅為布林用途；測試不輸出任何憑證

    def get(self, *_a, **_k):
        return {}


LOGIN = {"fail": 0, "attempts": 0}
STATE = {"auth_ok": True, "guest_ok": True}


def fake_read_all(client):
    """
    依 STATE 合成 reading，包含 read_all 真正會產生的 _fail 結構。
    authed 失敗時，mode/sched 會像實機一樣退化成 unknown / None。
    """
    fails = []
    if not STATE["auth_ok"]:
        fails += [A_RUN, A_SCH]
    if not STATE["guest_ok"]:
        fails += GUESTS
    r = {
        "_fail": fails, "communication_ok": STATE["guest_ok"],
        "pcs_charging_flag": False, "pcs_discharging_flag": False,
        "pcs_running_flag": False, "pcs_standby_flag": True,
        "pcs_fault_flag": False, "actual_active_power_kw": 0.0,
        "soc_percent": 50.0, "raw_source_time": "",
        "pcs_control_mode_code": "unknown" if not STATE["auth_ok"] else "smart",
        "pcs_control_mode": "未知" if not STATE["auth_ok"] else "智慧模式",
        "pcs_schedule_enabled": None if not STATE["auth_ok"] else True,
        "pcs_power_control_mode": "交流有功", "pcs_status": "執行 / 併網",
        "alarm_rows": [], "alarm_total": 0,
    }
    return r


CDR.read_all = fake_read_all
CDR.ApiClient = FakeClient
CDR._fetch_cell_packs = lambda _c: (CDR._fake_pack_data(), None)


def reset(auth_ok=True, guest_ok=True, login_fail=0):
    STATE.update(auth_ok=auth_ok, guest_ok=guest_ok)
    LOGIN.update(fail=login_fail, attempts=0)
    RM._auto.update(auth_fail_streak=0, disabled=False, login_failed_at=None,
                    last_auth_note=None, last_status=None)


def tick():
    """模擬 Service 的一輪：取得 client → 取樣（內含登入態偵測）。"""
    client, _printed = RM._monitor_client()
    if client is None:
        return None, None
    return client, RM._read_device_state(client)


print("=" * 70)
print("Phase 4.6-B  authenticated session 失效偵測與復原")
print("=" * 70)
print(f"  authenticated 端點 : {A_RUN}")
print(f"                       {A_SCH}")
print(f"  guest 對照端點     : {len(GUESTS)} 支")
print(f"  去抖動門檻         : AUTH_LOST_STREAK = {RM.AUTH_LOST_STREAK}")

# ======================================================================
print("\n[A] authenticated 端點正常 → 不清除 client")
# ======================================================================
reset()
RM._report["client"] = None
c1, r1 = tick()
check("首輪建立並登入 client", c1 is not None and LOGIN["attempts"] == 1)
check("mode 讀到 smart（正常情境）", r1.get("pcs_control_mode_code") == "smart")
for _ in range(5):
    tick()
check("連續 6 輪皆正常 → client 未被更換（generation 不變）",
      RM._report["client"] is c1)
check("auth_fail_streak 維持 0", RM._auto["auth_fail_streak"] == 0)
check("未額外登入（attempts 仍為 1）", LOGIN["attempts"] == 1)

# ======================================================================
print("\n[B] 單一 authenticated 端點失敗 → **不**立即清除")
# ======================================================================
reset()
RM._report["client"] = None
c_b, _ = tick()
_orig_read = CDR.read_all


def _only_runmode_fail(_c):
    r = fake_read_all(_c)
    r["_fail"] = [A_RUN]                       # 只有一支失敗
    return r


CDR.read_all = _only_runmode_fail
for _ in range(5):
    tick()
CDR.read_all = _orig_read
check("只有一支 authed 端點失敗，連續 5 輪也不清除 client",
      RM._report["client"] is c_b)
check("auth_fail_streak 未累積（判定不成立）", RM._auto["auth_fail_streak"] == 0)

# ======================================================================
print("\n[C] 兩支 authenticated 端點同時失敗 + guest 正常 → 達門檻才清除")
# ======================================================================
reset()
RM._report["client"] = None
c_c, _ = tick()
STATE["auth_ok"] = False
_, _r1 = tick()                                       # 第 1 輪：只記 pending
check(f"第 1 輪：streak={RM._auto['auth_fail_streak']}，client 尚未清除",
      RM._auto["auth_fail_streak"] == 1 and RM._report["client"] is c_c)
_buf2 = io.StringIO()
with contextlib.redirect_stdout(_buf2):
    tick()                                            # 第 2 輪：達門檻 → invalidate + 重新登入
_out_c = _buf2.getvalue()
check(f"第 {RM.AUTH_LOST_STREAK} 輪達門檻 → 清除快取 client",
      RM._report["client"] is not c_c)
check("log 明確說明「判定登入態可能失效」與「清除 cached client」",
      "判定登入態可能失效" in _out_c and "清除 cached client" in _out_c)
check("log 不含任何憑證字樣（token / cookie / password / authorization）",
      not re.search(r"token|cookie|password|authorization", _out_c, re.I))
check("清除後 streak 歸零（不會連環觸發 login storm）",
      RM._auto["auth_fail_streak"] == 0)

# ======================================================================
print("\n[D] 下一輪重新登入 → mode / schedule 恢復")
# ======================================================================
STATE["auth_ok"] = True
_c_new, _r_new = tick()
check("已建立新的 client（generation 遞增）",
      _c_new is not None and _c_new.generation > c_c.generation)
check(f"重新登入次數增加（attempts={LOGIN['attempts']}）", LOGIN["attempts"] >= 2)
check("mode 恢復為 smart", _r_new.get("pcs_control_mode_code") == "smart")
check("schedule 恢復為 True", _r_new.get("pcs_schedule_enabled") is True)
check("恢復後 streak 為 0", RM._auto["auth_fail_streak"] == 0)

# ======================================================================
print("\n[E/F] 重新登入第一次失敗 → 走既有 retry，不崩潰；之後成功即自動恢復")
# ======================================================================
reset(auth_ok=False, login_fail=1)
RM._report["client"] = FakeClient()             # 既有（失效）client
_ok_e = True
_buf3 = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf3):
        # 前 AUTH_LOST_STREAK 輪把 client 清掉；**多跑一輪**才會真的去重新登入
        #（invalidate 發生在該輪 _monitor_client() 之後，故同一輪不會登入）。
        for _ in range(RM.AUTH_LOST_STREAK + 1):
            tick()                              # 觸發 invalidate → 重新登入（會失敗一次）
except Exception as e:                          # noqa: BLE001
    _ok_e = False
    check(f"E  重新登入失敗時發生例外：{type(e).__name__}: {e}", False)
_out_e = _buf3.getvalue()
check("E  重新登入失敗不會拋例外（走既有 retry 路徑）", _ok_e)
check("E  已進入既有的「登入失敗 → 暫停 + 定時重試」狀態",
      RM._auto["disabled"] is True)
check("E  失敗訊息明確（含重試說明）", "登入失敗" in _out_e and "重試" in _out_e)
STATE["auth_ok"] = True
LOGIN["fail"] = 0
_buf4 = io.StringIO()
with contextlib.redirect_stdout(_buf4):
    _c_f, _r_f = tick()                         # 節流：login_failed_at 已設 → 需 force 或等待
    if _c_f is None:
        RM._auto["login_failed_at"] = None      # 模擬 60s 節流期滿
        _c_f, _r_f = tick()
_out_f = _buf4.getvalue()
check("F  登入成功後監看自動恢復", _c_f is not None and RM._auto["disabled"] is False)
check("F  恢復訊息為既有的「登入成功 → 自動監看已恢復」",
      "自動監看已恢復" in _out_f)
check("F  恢復後 mode 為 smart", (_r_f or {}).get("pcs_control_mode_code") == "smart")

# ======================================================================
print("\n[G] guest 端點也一起失敗 → **不得**誤判為登入逾期")
# ======================================================================
reset(auth_ok=False, guest_ok=False)
RM._report["client"] = None
_c_g, _ = tick()
for _ in range(6):
    tick()
check("authed + guest 全掛（網路/設備問題）→ 連續 6 輪皆不清除 client",
      RM._report["client"] is _c_g)
check("G  判定明確歸類為 guest_also_down",
      RM._auth_session_lost(fake_read_all(None))[1] == "guest_also_down")
check("G  未產生額外登入（不會 login storm）", LOGIN["attempts"] == 1)

# ======================================================================
print("\n[H/I] API 正常但模式/排程本來就不是 smart / True → 不得清除 client")
# ======================================================================
reset()
RM._report["client"] = None
_c_h, _ = tick()


def _manual_mode(_c):
    r = fake_read_all(_c)
    r.update(pcs_control_mode_code="manual", pcs_schedule_enabled=False,
             _fail=[])                          # API 全部成功
    return r


CDR.read_all = _manual_mode
for _ in range(6):
    tick()
CDR.read_all = _orig_read
check("H  真的是手動模式（API 正常）→ 不清除 client", RM._report["client"] is _c_h)
check("I  排程主開關關閉（API 正常）→ 不清除 client", RM._report["client"] is _c_h)
check("H/I  未額外登入", LOGIN["attempts"] == 1)
check("H/I  判定成立與否只看 _fail（此情境 _fail 為空 → 不成立）",
      RM._auth_session_lost(_manual_mode(None))[0] is False)

# ======================================================================
print("\n[J] 進行中的 recording Session：復原不得破壞 Session")
# ======================================================================
reset()
RM._report["client"] = None
_c_j0, _ = tick()
_owned, _why = RM.acquire_ownership(role="test-auth-recovery")
check(f"J  取得專屬 Mutex 的 Ownership（{_why}）", _owned is True)
_sess = CDR.ReportSession("auto", None, "交流有功", _c_j0, ROOT)
_sess.start()
_folder0, _sid0 = _sess.folder, _sess.session_id
_bak_interval = CFG.SAMPLE_INTERVAL_SEC
CFG.SAMPLE_INTERVAL_SEC = 0.2
_started = RM._report_bg_start(_sess)
check("J  背景取樣執行緒已啟動", _started is True and RM._report["thread"] is not None)
_thread0 = RM._report["thread"]
time.sleep(0.8)
_n0 = len(_sess.samples)
STATE["auth_ok"] = False
_buf5 = io.StringIO()
with contextlib.redirect_stdout(_buf5):
    for _ in range(RM.AUTH_LOST_STREAK):
        tick()                                  # 觸發 invalidate
    STATE["auth_ok"] = True
    _c_j1, _ = tick()                           # 重新登入 + rebind
_out_j = _buf5.getvalue()
time.sleep(0.6)
check("J  client 確實已更換", _c_j1 is not None and _c_j1 is not _c_j0)
check("J  Session 的 client 已接到新 client（Session 未中斷）",
      _sess.client is _c_j1)
check("J  log 說明 Session 已改用新 client",
      "Session 已改用重新登入後的 client" in _out_j)
check(f"J  session_id 未變（{_sid0}）", _sess.session_id == _sid0)
check("J  report folder 未變", _sess.folder == _folder0)
check("J  未建立第二個 Session", RM._report["session"] is _sess)
check("J  取樣執行緒未被重啟（仍是同一個 thread 物件）",
      RM._report["thread"] is _thread0 and _thread0.is_alive())
check(f"J  取樣持續累積（{_n0} → {len(_sess.samples)} 筆）",
      len(_sess.samples) > _n0)
check("J  Session 未被 finalize / pause（status 仍為 recording）",
      RM._report["finalized"] is False)
check("J  復原期間未新增任何 Session 資料夾（仍只有 1 個）",
      len([d for d in os.listdir(ROOT)
           if os.path.isdir(os.path.join(ROOT, d))]) == 1)

# ======================================================================
print("\n[K] Ownership 不受 client 復原影響")
# ======================================================================
check("K  復原前後 is_owner() 皆為 True", RM.is_owner() is True)
check("K  ownership_state 的 reason 未被改動",
      RM.ownership_state().get("reason") in ("acquired", "abandoned_taken"))
_src = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()


def _code_only(s):
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


_inv = _code_only(_src[_src.index("def _invalidate_report_client("):
                       _src.index("def _rebind_session_client(")])
check("K  _invalidate_report_client 不碰 Ownership／Session／執行緒",
      not any(s in _inv for s in ("acquire_ownership", "release_ownership",
                                  "ReportSession", "_report_bg_start",
                                  "finalize", "report_pause", "makedirs")))
_reb = _code_only(_src[_src.index("def _rebind_session_client("):
                       _src.index("def _check_auth_session(")])
check("K  _rebind_session_client 只改 client 參照，不動 Session 生命週期",
      "sess.client = new_client" in _reb
      and not any(s in _reb for s in ("finalize", "report_pause", "start()",
                                      "_report_bg_start", "makedirs",
                                      "release_ownership")))
check("K  _rebind_session_client 在 _CLIENT_LOCK 內進行（與背景取樣互斥）",
      "_CLIENT_LOCK" in _reb)

# ---- 收尾 Session（測試自己建立的臨時 Session，收在臨時目錄）----
if RM._report["stop"] is not None:
    RM._report["stop"].set()
if RM._report["thread"] is not None:
    RM._report["thread"].join(timeout=5)
RM._report.update(session=None, thread=None, stop=None, finalized=False)
CFG.SAMPLE_INTERVAL_SEC = _bak_interval

# ======================================================================
print("\n[L] 非 Owner（Observer）：client 復原不得建立任何 Session")
# ======================================================================
RM.release_ownership()
check("L  已釋放 Ownership（模擬 Observer）", RM.is_owner() is False)
_before = sorted(os.listdir(ROOT))
reset(auth_ok=False)
RM._report["client"] = FakeClient()
_buf6 = io.StringIO()
with contextlib.redirect_stdout(_buf6):
    for _ in range(RM.AUTH_LOST_STREAK + 1):
        tick()
    STATE["auth_ok"] = True
    tick()
check("L  Observer 也會做 client 復原（唯讀顯示需要正確資料）",
      RM._report["client"] is not None)
check("L  但**未**建立任何 Session", RM._report["session"] is None)
check("L  未啟動任何取樣執行緒", RM._report["thread"] is None)
check("L  output root 內容未新增（無新資料夾／檔案）",
      sorted(os.listdir(ROOT)) == _before)
check("L  復原過程未取得 Ownership", RM.is_owner() is False)

# ======================================================================
print("\n[靜態] 判定依據與範圍守門")
# ======================================================================
_chk = _code_only(_src[_src.index("def _auth_session_lost("):
                       _src.index("def _invalidate_report_client(")])
check("判定以 reading['_fail'] 為準，**不**以 mode_code == 'unknown' 為觸發",
      "_fail" in _chk and "unknown" not in _chk)
check("端點清單引用 CDR 的 EP_* 常數（唯一定義處，未另抄字串）",
      "CDR.EP_RUNMODE" in _src and "CDR.EP_SCHEDULE" in _src
      and "/schedule/config/getScheduleSwitch\"" not in _code_only(
          _src[_src.index("AUTH_ENDPOINTS ="):_src.index("AUTH_LOST_STREAK =")]))
check("去抖動門檻為常數且 ≥2（避免瞬斷造成 login storm）",
      isinstance(RM.AUTH_LOST_STREAK, int) and RM.AUTH_LOST_STREAK >= 2)
check("未新增第二套登入流程（invalidate 內不自行 login）",
      "login_hmi" not in _inv and "ApiClient" not in _inv)
check("CDR.read_all 只補 _fail metadata，回傳語意未變（詳見 [_error] 節守門）",
      "_fail" in open(os.path.join(HERE, "charge_discharge_report.py"),
                      encoding="utf-8").read())
_tree = ast.parse(_src)
_names = {n.name for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)}
check("新增的三個 helper 皆存在且職責分離",
      {"_auth_session_lost", "_invalidate_report_client",
       "_rebind_session_client", "_check_auth_session"} <= _names)
check("report_monitor.py 仍無 input()（Service 不得被 console 阻塞）",
      not [n for n in ast.walk(_tree)
           if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "input"])

# ======================================================================
# application-error 失敗語意（Phase 4.6-B 第二次修正）
#   ⚠️ 本節刻意走**真實** CDR.read_all，只把 client 換成假的 ——
#      前面各節注入的是假 read_all，證明不了「_get 有沒有把 {"_error":…}
#      記進 _fail」。這一段才是實機失效模式的端到端驗證。
#   實機背景：Service degraded 期間 log 的「[警告] GET」為 0 行，而
#      ApiClient.get() 每條回 None 的路徑都會先印警告 → 回的不是 None，
#      而是 unwrap() 產生的 {"_error": code, …}。原本 _fail 只認 None，
#      因此偵測完全看不到 → 永遠不會重新登入。
# ======================================================================
print("\n[_error] application-error 失敗語意（走真實 read_all）")

_REAL_READ_ALL = _BAK[0]
_RAISE = object()
_MISS = object()
TABLE = {}


class EndpointClient:
    """假 client：依 TABLE 決定每支端點回什麼。零網路存取。"""
    _seq = [0]

    def __init__(self, *_a, **_k):
        EndpointClient._seq[0] += 1
        self.generation = EndpointClient._seq[0]

    def login_hmi(self, *_a, **_k):
        if LOGIN["fail"] > 0:
            LOGIN["fail"] -= 1
            LOGIN["attempts"] += 1
            return None
        LOGIN["attempts"] += 1
        return "fake-token"

    def get(self, path, **_kw):
        v = TABLE.get(path, _MISS)
        if v is _RAISE:
            raise TimeoutError("injected timeout")
        if v is _MISS:
            return {} if path != CDR.EP_PCS else []
        return v


def set_table(runmode=_MISS, schedule=_MISS, guest_error=False):
    """組出一輪的端點回應。預設 authed 正常、guest 正常。"""
    TABLE.clear()
    TABLE[A_RUN] = "0" if runmode is _MISS else runmode
    TABLE[A_SCH] = ({"schedulePlanSwitchId": 1, "schedulePlanSwitch": 1,
                     "manualModeSwitch": 0} if schedule is _MISS else schedule)
    if guest_error:
        for g in GUESTS:
            TABLE[g] = {"_error": 401, "msg": "x", "_raw": {}}


def fails_for(path, reading):
    return [f for f in (reading or {}).get("_fail", []) if str(f).startswith(path)]


CDR.read_all = _REAL_READ_ALL
CDR.ApiClient = EndpointClient
_ERR_FULL = {"_error": 401, "msg": "SENSITIVE-MSG", "_raw": {"tok": "SECRET"}}

# ---- A~E：_fail 的四種語意 ----
_c = EndpointClient()
set_table()
_rA = _REAL_READ_ALL(_c)
check("A  正常 dict / str 且無 _error → 不進 _fail",
      fails_for(A_RUN, _rA) == [] and fails_for(A_SCH, _rA) == [])
set_table(runmode=None)
check("B  回 None → 維持原本格式（僅 path）",
      fails_for(A_RUN, _REAL_READ_ALL(_c)) == [A_RUN])
set_table(runmode={"_error": 401, "msg": "m", "_raw": {}})
check(f"C  {{'_error': 401}} → 記為 path:_error401",
      fails_for(A_RUN, _REAL_READ_ALL(_c)) == [f"{A_RUN}:_error401"])
set_table(runmode={"_error": 1002, "msg": "m", "_raw": {}})
check("D  任意非 0 code 一樣進 _fail（未比對特定 code）",
      fails_for(A_RUN, _REAL_READ_ALL(_c)) == [f"{A_RUN}:_error1002"])
set_table(runmode=_ERR_FULL)
_fE = fails_for(A_RUN, _REAL_READ_ALL(_c))
check("E  只記端點與 code —— msg / _raw 不進 _fail",
      _fE == [f"{A_RUN}:_error401"]
      and not any(s in str(_fE) for s in ("SENSITIVE", "SECRET", "_raw")))
set_table(runmode=_RAISE)
check("   例外路徑格式未受影響（path:ExcType + path）",
      fails_for(A_RUN, _REAL_READ_ALL(_c))
      == [f"{A_RUN}:TimeoutError", A_RUN])

# ---- F/G：判定特異性（_auth_session_lost 未修改，靠格式相容）----
set_table(runmode=_ERR_FULL)
_rF = _REAL_READ_ALL(_c)
check("F  只有 EP_RUNMODE 回 _error（EP_SCHEDULE 正常）→ auth lost **不**成立",
      RM._auth_session_lost(_rF)[0] is False)
set_table(runmode=_ERR_FULL, schedule=_ERR_FULL)
_rG = _REAL_READ_ALL(_c)
check("G  兩支 authed 皆回 _error + guest 正常 → auth lost 成立",
      RM._auth_session_lost(_rG)[0] is True)
check("G  _ep_failed() 未修改即可匹配 ':_error<code>' 格式（格式相容已驗證）",
      RM._ep_failed(_rG["_fail"], A_RUN) and RM._ep_failed(_rG["_fail"], A_SCH))
check("G  此情境下 mode/sched 確實退化成 unknown / None（與實機一致）",
      _rG.get("pcs_control_mode_code") == "unknown"
      and _rG.get("pcs_schedule_enabled") is None)

# ------------------------------------------------------------------
# 診斷字串（observability）：實機只讀 Service log 就要能取得 application code
#   ⚠️ 這一段驗的是「印什麼」，**不是**「何時判定」——
#      判定條件 / 門檻 / invalidate / relogin 流程一律不得因此改變（第 6、7 項）。
# ------------------------------------------------------------------
print("\n[why] 診斷字串含 application code（純 observability）")
_SENS = re.compile(r"msg|_raw|token|cookie|password|authorization|SENSITIVE|SECRET",
                   re.I)


def why_of(fail_list):
    return RM._auth_session_lost({"_fail": list(fail_list)})[1]


_w1 = why_of([f"{A_RUN}:_error401", f"{A_SCH}:_error401"])
print(f"    ① {_w1}")
check("1  兩支皆 _error401 → why 同時含兩支端點與 _error401",
      A_RUN in _w1 and A_SCH in _w1 and _w1.count("_error401") == 2)
_w2 = why_of([f"{A_RUN}:_error1002", f"{A_SCH}:_error503"])
print(f"    ② {_w2}")
check("2  兩支不同 code → 各自保留自己的 code",
      "_error1002" in _w2 and "_error503" in _w2)
_w3 = why_of([f"{A_RUN}:TimeoutError", A_RUN, f"{A_SCH}:ConnectionError", A_SCH])
print(f"    ③ {_w3}")
check("3  例外型別 metadata 可安全顯示（endpoint:ExceptionType）",
      "TimeoutError" in _w3 and "ConnectionError" in _w3)
_w3b = why_of([f"{A_RUN}:_error401", A_SCH])
check("   混合格式（一支 _error、一支僅 path）亦正確呈現",
      "_error401" in _w3b and _w3b.count(A_SCH) == 1)
_w4 = why_of([f"{A_RUN}:_error401", f"{A_SCH}:_error401",
              f"{GUESTS[0]}:_error999", f"{GUESTS[1]}:TimeoutError"])
print(f"    ④ {_w4}")
check("4  guest 端點的失敗詳情**不得**出現在 why（只回報成功支數）",
      GUESTS[0] not in _w4 and GUESTS[1] not in _w4
      and "_error999" not in _w4 and "guest_ok=3/5" in _w4)
check("4  每支 auth 端點最多一筆（未因例外重複記兩次而重覆輸出）",
      _w3.count(A_RUN) == 1 and _w3.count(A_SCH) == 1)
_w5 = why_of([f"{A_RUN}:_error401", f"{A_SCH}:_error401"])
check("5  why 不含 msg / _raw / token / cookie / password / authorization",
      not _SENS.search(_w5))
check("5  _fail 條目依構造只含端點與 code —— 即使原始回應帶敏感內容也不會流出",
      not _SENS.search(str(fails_for(A_RUN, _rG) + fails_for(A_SCH, _rG))))
# 6/7：判定與門檻不得因診斷字串改變
_TRUTH = [
    ("全部正常", [], False),
    ("只有 RUNMODE 失敗", [f"{A_RUN}:_error401"], False),
    ("只有 SCHEDULE 失敗", [f"{A_SCH}:_error401"], False),
    ("兩支失敗 + guest 正常", [f"{A_RUN}:_error401", f"{A_SCH}:_error401"], True),
    ("兩支失敗（None 格式）", [A_RUN, A_SCH], True),
    ("兩支失敗 + guest 全掛", [A_RUN, A_SCH] + GUESTS, False),
    ("兩支失敗 + guest 部分掛", [A_RUN, A_SCH, GUESTS[0]], True),
]
_ok6 = all(RM._auth_session_lost({"_fail": f})[0] is exp for _n, f, exp in _TRUTH)
for _n, _f, _exp in _TRUTH:
    _got = RM._auth_session_lost({"_fail": _f})[0]
    if _got is not _exp:
        print(f"      ✗ {_n}: 期望 {_exp}，實際 {_got}")
check(f"6  判定 True/False 與修改前完全一致（{len(_TRUTH)} 種輸入逐一比對）", _ok6)
check("6  非成立路徑的診斷字串未變（authed_failed=N/2 / guest_also_down / no_*）",
      why_of([f"{A_RUN}:_error401"]) == "authed_failed=1/2"
      and why_of([A_RUN, A_SCH] + GUESTS) == "guest_also_down"
      and RM._auth_session_lost(None)[1] == "no_reading"
      and RM._auth_session_lost({})[1] == "no_fail_field")
check(f"7  AUTH_LOST_STREAK 仍為 2（門檻未因 observability 改動）",
      RM.AUTH_LOST_STREAK == 2)
_swait_src = _code_only(_src[_src.index("def _auth_session_lost("):
                             _src.index("def _invalidate_report_client(")])
check("7  診斷字串只影響回傳的字串 —— 未在判定函式內新增任何副作用",
      not any(s in _swait_src for s in ("_report[", "_auto[", "print(",
                                        "login_hmi", "ApiClient", "acquire_ownership")))

# ---- H/I：連續兩輪 → invalidate → 下一輪 relogin ----
reset()
RM._report["client"] = None
set_table()
_cH0, _ = tick()
check("H  前置：正常情境下已取得 client", _cH0 is not None)
set_table(runmode=_ERR_FULL, schedule=_ERR_FULL)
tick()
check(f"H  第 1 輪：streak={RM._auto['auth_fail_streak']}，client 未清除",
      RM._auto["auth_fail_streak"] == 1 and RM._report["client"] is _cH0)
_bufH = io.StringIO()
with contextlib.redirect_stdout(_bufH):
    tick()
check(f"H  第 {RM.AUTH_LOST_STREAK} 輪達門檻 → 清除 cached client",
      RM._report["client"] is not _cH0)
check("H  log 說明判定登入態可能失效並清除 cached client",
      "判定登入態可能失效" in _bufH.getvalue()
      and "清除 cached client" in _bufH.getvalue())
check("H  log 未輸出憑證字樣",
      not re.search(r"token|cookie|password|authorization",
                    _bufH.getvalue(), re.I))
set_table()                                        # authed 恢復正常
_cI, _rI = tick()
check("I  下一輪重新登入並取得**新** client",
      _cI is not None and _cI.generation > _cH0.generation)
check("I  mode / schedule 恢復（smart / True）",
      _rI.get("pcs_control_mode_code") == "smart"
      and _rI.get("pcs_schedule_enabled") is True)
check("I  恢復後 _fail 不含 authed 端點",
      fails_for(A_RUN, _rI) == [] and fails_for(A_SCH, _rI) == [])

# ---- J：recording Session 在 _error 失效模式下的 rebind ----
_owned2, _why2 = RM.acquire_ownership(role="test-auth-recovery-error")
check(f"J  取得專屬 Mutex 的 Ownership（{_why2}）", _owned2 is True)
CFG.SAMPLE_INTERVAL_SEC = 0.2
_sess2 = CDR.ReportSession("auto", None, "交流有功", _cI, ROOT)
_sess2.start()
_sid2, _folder2 = _sess2.session_id, _sess2.folder
_dirs_before = len([d for d in os.listdir(ROOT)
                    if os.path.isdir(os.path.join(ROOT, d))])
check("J  背景取樣執行緒已啟動", RM._report_bg_start(_sess2) is True)
_thread2 = RM._report["thread"]
time.sleep(0.8)
_n2 = len(_sess2.samples)
set_table(runmode=_ERR_FULL, schedule=_ERR_FULL)
_bufJ = io.StringIO()
with contextlib.redirect_stdout(_bufJ):
    for _ in range(RM.AUTH_LOST_STREAK):
        tick()
    set_table()
    _cJ, _ = tick()
time.sleep(0.6)
check("J  client 已更換", _cJ is not None and _cJ is not _cI)
check("J  Session 的 client 已 rebind 到新 client", _sess2.client is _cJ)
check(f"J  session_id 未變（{_sid2}）", _sess2.session_id == _sid2)
check("J  report folder 未變", _sess2.folder == _folder2)
check("J  取樣執行緒為同一個物件且仍存活",
      RM._report["thread"] is _thread2 and _thread2.is_alive())
check(f"J  取樣持續累積（{_n2} → {len(_sess2.samples)} 筆）",
      len(_sess2.samples) > _n2)
check("J  未 finalize、未建立第二個 Session",
      RM._report["finalized"] is False and RM._report["session"] is _sess2)
check("J  未新增任何 Session 資料夾",
      len([d for d in os.listdir(ROOT)
           if os.path.isdir(os.path.join(ROOT, d))]) == _dirs_before)
if RM._report["stop"] is not None:
    RM._report["stop"].set()
if RM._report["thread"] is not None:
    RM._report["thread"].join(timeout=5)
RM._report.update(session=None, thread=None, stop=None, finalized=False)
CFG.SAMPLE_INTERVAL_SEC = _bak_interval
RM.release_ownership()
check("J  收尾後未持有 Ownership", RM.is_owner() is False)

# ---- K：guest 也一起回 _error → 不得誤判為 auth-only ----
reset()
RM._report["client"] = None
set_table()
_cK, _ = tick()
set_table(runmode=_ERR_FULL, schedule=_ERR_FULL, guest_error=True)
for _ in range(RM.AUTH_LOST_STREAK + 2):
    tick()
check("K  authed 與 guest 全回 _error → 連續多輪皆不清除 client",
      RM._report["client"] is _cK)
check("K  判定歸類為 guest_also_down",
      RM._auth_session_lost(_REAL_READ_ALL(_cK))[1] == "guest_also_down")
check("K  未產生額外登入（無 login storm）", LOGIN["attempts"] == 1)

# ---- L/M：API 正常但模式/排程本來就不是 smart / True ----
reset()
RM._report["client"] = None
set_table(runmode="1", schedule={"schedulePlanSwitchId": 1,
                                "schedulePlanSwitch": 0, "manualModeSwitch": 1})
_cL, _rL = tick()
for _ in range(RM.AUTH_LOST_STREAK + 2):
    tick()
check("L/M  API 全部正常（無 _fail）→ 連續多輪皆不清除 client",
      RM._report["client"] is _cL)
check("L/M  _fail 為空 → 判定不成立",
      _rL.get("_fail") == [] and RM._auth_session_lost(_rL)[0] is False)
check(f"M  排程主開關關閉時 sched 讀到 False（非 None）—— 與失效可區分",
      _rL.get("pcs_schedule_enabled") is False)
check("L/M  未額外登入", LOGIN["attempts"] == 1)

# ---- 靜態守門：修法範圍 ----
_cdr_src = open(os.path.join(HERE, "charge_discharge_report.py"),
                encoding="utf-8").read()
_get_src = _code_only(_cdr_src[_cdr_src.index("    def _get(path, **kw):"):
                               _cdr_src.index("    # 1) 電池 SOC")])
check("_get 仍回傳原值（未把 _error 轉成 None，避免改變既有消費者）",
      "return v" in _get_src and "return None" not in _get_src)
check("_get 未比對特定 application code（只認 '_error' key）",
      '"_error" in v' in _get_src
      and not re.search(r"_error.*==\s*\d|==\s*401|==\s*403", _get_src))
check("_fail 只寫入端點與 code（未寫入 msg / _raw / body）",
      "_error{v.get('_error')}" in _get_src
      and "msg" not in _get_src and "_raw" not in _get_src)
check("read_all 的回傳 schema 未變（communication_ok / _fail 仍為原本欄位）",
      '"communication_ok": True, "_fail": []' in _cdr_src)

# ======================================================================
print("\n[環境還原]")
# ======================================================================
(CDR.read_all, CDR.ApiClient, RM._report_output_root, CDR._fetch_cell_packs) = _BAK
RM._report["client"] = None
RM._auto.update(auth_fail_streak=0, disabled=False, login_failed_at=None,
                last_auth_note=None)
check("已還原 CDR.read_all / CDR.ApiClient / _report_output_root",
      CDR.read_all is _BAK[0] and CDR.ApiClient is _BAK[1]
      and RM._report_output_root is _BAK[2])
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("測試結束未殘留 Session / 執行緒",
      RM._report["session"] is None and RM._report["thread"] is None)
check("SAMPLE_INTERVAL_SEC 已還原", CFG.SAMPLE_INTERVAL_SEC == _bak_interval)
shutil.rmtree(ROOT, ignore_errors=True)

ok = all(RESULTS)
print("\n" + "=" * 70)
print(f"Phase 4.6-B authenticated session 復原 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）")
print("=" * 70)
sys.exit(0 if ok else 1)
