# -*- coding: utf-8 -*-
"""
Phase 5.3 — Stress / Boundary Test（#1～#10，全部離線）
======================================================================
範圍來源：docs/Phase5_Validation_Plan.md §5.2 的 12 項矩陣。
本檔只涵蓋安全分級 A 的 #1～#10；#11（Ownership contention）與 #12（路徑
canonicalization + 異常資料）需跨行程 / 觸及 Mutex 命名，暫列 B 級不在此執行。

    python test_phase5_stress_boundary.py        # exit 0 = PASS

設計原則
    · **不修改產品碼**。本檔只驗證現有產品在邊界與壓力條件下的行為。
    · 邊界判定一律**依產品碼現行語意**，不依直覺：
        #8 orphan gap    report_monitor.py: `gap > ORPHAN_GAP_SEC`      → 嚴格大於
        #9 idle debounce charge_discharge_report.py: `>= AUTO_HOLD_STOP_SEC` → 大於等於
      兩者刻意不對稱，本測試的目的就是把這個語意鎖住。
    · 全程 temp root，零網路，不碰正式 output、不碰 Running Service。

Isolation Guard
    在跑任何壓力項目**之前**先證明隔離成立（見 §G）。任一項無法證明即中止，
    不執行後續測試 —— 寧可不測，也不能污染正式環境。
"""
import io
import os
import re
import csv
import sys
import json
import time
import shutil
import hashlib
import tempfile
import contextlib
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def fatal(msg):
    print(f"\n  ★ 中止：{msg}")
    print(f"\n== Phase 5.3-A Stress / Boundary FAIL"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過，隔離未成立即中止）==")
    sys.exit(1)


# ======================================================================
# G. Isolation Guard —— 必須先全部成立
# ======================================================================
print("=" * 66)
print("Phase 5.3-A — Stress / Boundary Test（#1～#10）")
print("=" * 66)
print("\n[G] Isolation Guard（未全部成立即中止，不執行壓力測試）")

_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

# 產品實際使用的 output root 與 canonical Mutex（僅計算名稱，不 acquire）
PROD_ROOT = RM._report_output_root()
PROD_MUTEX = RM._mutex_name()
PROD_OWNER = os.path.join(PROD_ROOT, CFG.MONITOR_OWNER_FILE)

TMP = tempfile.mkdtemp(prefix="p53_stress_")
_BAK_ROOT = RM._report_output_root
RM._report_output_root = lambda: TMP            # ← 必須早於任何 Mutex 相關呼叫
TMP_MUTEX = RM._mutex_name()


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _fp(root):
    """正式 output 的指紋（檔名 + 大小 + mtime），用於證明零修改。"""
    if not os.path.isdir(root):
        return (0, "absent")
    items = []
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
                items.append((os.path.relpath(p, root), st.st_size, st.st_mtime_ns))
            except OSError:
                items.append((os.path.relpath(p, root), -1, -1))
    blob = "\n".join(f"{a}|{b}|{c}" for a, b, c in items)
    return len(items), hashlib.sha256(blob.encode("utf-8")).hexdigest()

PROD_FP_BEFORE = _fp(PROD_ROOT)

check(f"G1 temp root 位於系統暫存目錄（{TMP}）",
      _norm(TMP).startswith(_norm(tempfile.gettempdir())))
check("G2 temp root ≠ 正式 output root",
      _norm(TMP) != _norm(PROD_ROOT))
check("G3 temp root 不在正式 output root 之下（不會寫入任何正式 Session 目錄）",
      not _norm(TMP).startswith(_norm(PROD_ROOT)))
check(f"G4 _report_output_root() 已指向 temp（{RM._report_output_root() == TMP}）",
      RM._report_output_root() == TMP)
check("G5 Mutex 名稱已隨 root 改變，≠ 正式 canonical",
      TMP_MUTEX != PROD_MUTEX and TMP_MUTEX.startswith("Global\\"))
check("G6 正式 monitor_owner.json 未被本檔開啟寫入（僅記錄指紋）",
      os.path.exists(PROD_OWNER) or True)   # 僅宣告：後續於 §Z 驗證未變動

# ---- 零網路：注入假 client / 假讀數，並證明真函式已被取代 ----
_REAL_READ_ALL = CDR.read_all
_REAL_FETCH = CDR._fetch_cell_packs


def fake_read_all(_client, direction="charge", soc=50.0, power=10.0,
                  fault=False, comm_ok=True):
    return {
        "communication_ok": comm_ok, "raw_source_time": "",
        "pcs_charging_flag": direction == "charge",
        "pcs_discharging_flag": direction == "discharge",
        "soc_percent": soc, "battery_voltage_v": 800.0, "battery_current_a": 12.5,
        "rack_max_temperature_c": 28.0, "rack_min_temperature_c": 25.0,
        "battery_status": "運轉", "actual_active_power_kw": power,
        "actual_reactive_power_kvar": 0.0, "calculated_power_kw": power,
        "pcs_status": "執行 / 併網", "pcs_control_mode": "智慧模式",
        "pcs_control_mode_code": "smart", "pcs_work_mode": "併網",
        "pcs_power_control_mode": "交流有功", "pcs_manual_switch": 0,
        "pcs_schedule_enabled": True, "pcs_fault_flag": fault,
        "pcs_running_flag": True, "pcs_standby_flag": False,
        "battery_power_status": "已上電",
        "device_daily_charge_kwh": 1.0, "device_daily_discharge_kwh": 0.5,
        "alarm_rows": [], "alarm_total": 0, "_fail": [],
    }


CDR.read_all = fake_read_all
CDR._fetch_cell_packs = lambda _c: (CDR._fake_pack_data(), None)


class FakeClient:
    """零網路 client：任何 get / login 都不出網。"""

    def get(self, *_a, **_k):
        return {}

    def login_hmi(self, *_a, **_k):
        return "offline-token"


check("G7 CDR.read_all 已被假讀數取代（不連真實設備）",
      CDR.read_all is fake_read_all and CDR.read_all is not _REAL_READ_ALL)
check("G8 CDR._fetch_cell_packs 已被取代（不抓真實 cell 資料）",
      CDR._fetch_cell_packs is not _REAL_FETCH)

# ---- 本檔原始碼不得出現任何 Service 控制 / 強制終止 / 正式路徑寫入 ----
_SELF = io.open(os.path.abspath(__file__), encoding="utf-8").read()


def _code_only(s):
    """
    剝除 docstring、註解，以及**本守門自身的掃描區**。

    ⚠️ 為何要排除自身：守門的禁用字清單本身就是這些字串的字面值，若不排除，
       守門會命中自己的資料而恆為 FAIL。本專案的累犯陷阱（守門比對到自己的
       註解）在此更深一層 —— 比對到自己的清單。以哨兵標記明確界定排除範圍，
       範圍內只有清單與兩個 check()，不含任何實際呼叫。
    """
    s = re.sub(r"GUARD_SCAN_EXCLUDE_BEGIN[\s\S]*?GUARD_SCAN_EXCLUDE_END", "", s)
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


_SELF_CODE = _code_only(_SELF)
# GUARD_SCAN_EXCLUDE_BEGIN
# acquire_ownership 刻意**不**列於此：#11 需要合法取得 temp mutex。
# 該項改由下方的執行期攔截器把關 —— 直接斷言「取的不是正式 canonical」，
# 比文字掃描更強且無法繞過。
_FORBIDDEN = ("Stop-Service", "Start-Service", "Restart-Service", "taskkill",
              "nssm", "Stop-Process", "shutdown",
              "report_stop", "report_pause", "device_control_operator",
              "charge_discharge_reports")
_hit = [w for w in _FORBIDDEN if w in _SELF_CODE]
check(f"G9 本檔執行碼不含 Service 控制 / 強制終止 / Ownership acquire /"
      f" 正式路徑字面值（命中 {_hit or '無'}）", not _hit)
check(f"G10 守門自我驗證：排除區外確實掃得到禁用字"
      f"（注入測試 {'taskk' + 'ill' in _code_only('x = 1  ' + chr(10) + 'taskk' + 'ill')}）",
      ("taskk" + "ill") in _code_only("x = 1" + chr(10) + "taskk" + "ill"))
# GUARD_SCAN_EXCLUDE_END

# ---- 執行期攔截器：任何一次 acquire 都不得指向正式 canonical Mutex ----
#   比「原始碼不得出現 acquire_ownership」更強：#11 需要合法取得 temp mutex，
#   真正的不變量是「絕不取得正式 canonical」。此處於每次呼叫前實地斷言。
_REAL_ACQUIRE = RM.acquire_ownership
_ACQ = {"n": 0, "names": set()}


class ProductionMutexTouched(RuntimeError):
    pass


def _guarded_acquire(*a, **k):
    name = RM._mutex_name()
    if name == PROD_MUTEX:
        raise ProductionMutexTouched(
            f"嘗試取得正式 canonical Mutex（{name}）—— 攔截並中止")
    _ACQ["n"] += 1
    _ACQ["names"].add(name)
    return _REAL_ACQUIRE(*a, **k)


RM.acquire_ownership = _guarded_acquire
check("G11 已安裝 acquire 執行期攔截器（取代文字掃描，無法繞過）",
      RM.acquire_ownership is _guarded_acquire
      and RM.acquire_ownership is not _REAL_ACQUIRE)
_bak_root_probe = RM._report_output_root
RM._report_output_root = _BAK_ROOT               # 暫時指回正式 root 做注入測試
try:
    _blocked = False
    try:
        RM.acquire_ownership(role="guard-selftest")
    except ProductionMutexTouched:
        _blocked = True
finally:
    RM._report_output_root = _bak_root_probe
check("G12 攔截器自我驗證：root 指回正式時 acquire 確實被擋下（未真的取得）",
      _blocked and RM.is_owner() is False)

if not all(RESULTS):
    fatal("Isolation Guard 未全數通過")
print(f"  → Isolation Guard 全數通過（正式 output 指紋 "
      f"{PROD_FP_BEFORE[0]} 檔 {PROD_FP_BEFORE[1][:16]}…）")


# ======================================================================
# 共用 fixture
# ======================================================================
_CLK = {"dt": datetime(2026, 3, 12, 10, 0, 0)}


def _now():
    return _CLK["dt"]


def mk_session(root=None, direction="charge", interval=5):
    """在 temp root 建一份 Session（假時鐘、假讀數、零網路）。"""
    root = root or tempfile.mkdtemp(prefix="p53_sess_", dir=TMP)
    CDR.read_all = lambda c: fake_read_all(c, direction=direction)
    with contextlib.redirect_stdout(io.StringIO()):
        s = CDR.ReportSession("auto", None, "交流有功", FakeClient(), root,
                              now_fn=_now)
        s.start()
    return s, root


def advance(sess, n, step=5):
    """推進假時鐘並取樣 n 次。"""
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(n):
            _CLK["dt"] += timedelta(seconds=step)
            sess.sample_once()


def rows(folder, name):
    p = os.path.join(folder, name)
    if not os.path.exists(p):
        return []
    with io.open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def state(folder):
    p = os.path.join(folder, CFG.FILE_SESSION_STATE)
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


# ======================================================================
# Batch 1 —— #8 orphan gap ／ #9 idle debounce
# ======================================================================
print("\n[#8] orphan gap 邊界（ORPHAN_GAP_SEC，語意 = 嚴格大於）")

check(f"ORPHAN_GAP_SEC = 300（實際 {CFG.ORPHAN_GAP_SEC}）",
      CFG.ORPHAN_GAP_SEC == 300)

# 鎖定產品現行運算子：report_monitor 內為 `gap > _gap_limit`
_rm_src = io.open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
_m = re.search(r"if gap is not None and _gap_limit > 0 and gap ([<>=]+) _gap_limit",
               _rm_src)
check(f"產品現行運算子為嚴格大於（原始碼實測 `gap {_m.group(1) if _m else '?'} "
      f"_gap_limit`）", bool(_m) and _m.group(1) == ">")


def mk_state(last_offset_sec, status="recording"):
    d = tempfile.mkdtemp(prefix="p53_orphan_", dir=TMP)
    payload = {"status": status}
    if last_offset_sec is not None:
        payload["last_sample_time"] = (
            datetime.now() - timedelta(seconds=last_offset_sec)
        ).strftime("%Y-%m-%d %H:%M:%S")
    with io.open(os.path.join(d, CFG.FILE_SESSION_STATE), "w",
                 encoding="utf-8") as fh:
        json.dump(payload, fh)
    return d


def is_orphan(gap):
    """完全複製產品判定式，不自行決定 >= 或 >。"""
    lim = getattr(CFG, "ORPHAN_GAP_SEC", 0) or 0
    return gap is not None and lim > 0 and gap > lim


# 精確邊界一律用**直接判定式**驗證（確定性）。
# ⚠️ 不可用檔案路徑測「正好 300」：last_sample_time 只有秒級解析度，且從寫入到
#    讀取之間時間仍在前進，實測 gap 會落在 300 稍上方（如 300.4）而判為孤兒。
#    那是 harness 的時序競爭，不是產品行為 —— 故精確邊界走判定式，檔案路徑只用
#    遠離邊界的位移，另加一條「兩者結果必須一致」把兩條路徑綁在一起。
for val, want in ((299.0, False), (299.999, False), (300.0, False),
                  (300.001, True), (301.0, True), (600.0, True)):
    got = is_orphan(val)
    check(f"#8 gap={val} → 判定孤兒={got}（預期 {want}）", got is want)

check("#8 邊界語意鎖定：gap == 300 **不**算孤兒（`>` 而非 `>=`）",
      is_orphan(300.0) is False)

for off, want in ((250, False), (350, True)):
    g = RM._orphan_gap_seconds(mk_state(off))
    ok_g = g is not None and abs(g - off) <= 5
    got = is_orphan(g)
    check(f"#8 檔案路徑：斷線 {off}s → gap≈{g:.0f}，判定孤兒={got}（預期 {want}）",
          ok_g and got is want)

_g300 = RM._orphan_gap_seconds(mk_state(300))
check(f"#8 檔案路徑與判定式一致（gap={_g300:.3f}，"
      f"is_orphan={is_orphan(_g300)} == (gap>300)={_g300 > 300}）",
      is_orphan(_g300) is (_g300 > CFG.ORPHAN_GAP_SEC))
check("#8 last_sample_time 缺失 → gap None → 不判孤兒（保守）",
      RM._orphan_gap_seconds(mk_state(None)) is None
      and is_orphan(None) is False)
_bad = tempfile.mkdtemp(prefix="p53_orphan_bad_", dir=TMP)
with io.open(os.path.join(_bad, CFG.FILE_SESSION_STATE), "w",
             encoding="utf-8") as fh:
    json.dump({"status": "recording", "last_sample_time": "not-a-time"}, fh)
check("#8 last_sample_time 格式非預期 → 回 None，不拋例外",
      RM._orphan_gap_seconds(_bad) is None)
check("#8 無 session_state.json → 回 None",
      RM._orphan_gap_seconds(tempfile.mkdtemp(prefix="p53_orphan_none_",
                                              dir=TMP)) is None)
check("#8 門檻為 0（停用）時一律不判孤兒",
      (lambda: (setattr(CFG, "ORPHAN_GAP_SEC", 0),
                is_orphan(99999.0) is False,
                setattr(CFG, "ORPHAN_GAP_SEC", 300))[1])())
check(f"#8 測試後 ORPHAN_GAP_SEC 已還原（{CFG.ORPHAN_GAP_SEC}）",
      CFG.ORPHAN_GAP_SEC == 300)


print("\n[#9] idle debounce 邊界（AUTO_HOLD_STOP_SEC，語意 = 大於等於）")

check(f"AUTO_HOLD_STOP_SEC = 60（實際 {CFG.AUTO_HOLD_STOP_SEC}）",
      CFG.AUTO_HOLD_STOP_SEC == 60)

_cdr_src = io.open(os.path.join(HERE, "charge_discharge_report.py"),
                   encoding="utf-8").read()
_m2 = re.search(r"if self\.idle_held_seconds\(\) ([<>=]+) CFG\.AUTO_HOLD_STOP_SEC",
                _cdr_src)
check(f"產品現行運算子為大於等於（原始碼實測 `idle_held_seconds() "
      f"{_m2.group(1) if _m2 else '?'} AUTO_HOLD_STOP_SEC`）",
      bool(_m2) and _m2.group(1) == ">=")

# continuity 保護會攔在 auto_stop 之前 → 邊界測試需先停用，測完還原
_BAK_GAP = CFG.AUTO_NEXT_PLAN_GAP_SEC
_BAK_GRACE = getattr(CFG, "AUTO_PLAN_START_GRACE_SEC", None)
CFG.AUTO_NEXT_PLAN_GAP_SEC = 0
if _BAK_GRACE is not None:
    CFG.AUTO_PLAN_START_GRACE_SEC = 0


def sess_idle(sec):
    """建一份 Session 並把 idle 起點往前推 sec 秒（monotonic，不實際等待）。"""
    s, _ = mk_session()
    s._idle_since = time.monotonic() - sec
    return s


try:
    for sec, want_end, want_reason in ((59, False, None), (60, True, "auto_stop"),
                                       (61, True, "auto_stop")):
        s = sess_idle(sec)
        end, reason = s.should_auto_end()
        check(f"#9 idle {sec}s → should_auto_end=({end}, {reason!r})"
              f"（預期 ({want_end}, {want_reason!r})）",
              end is want_end and reason == want_reason)

    check("#9 邊界語意鎖定：idle == 60 **會**收尾（`>=` 而非 `>`）",
          sess_idle(60).should_auto_end()[0] is True)
    check("#9 idle 59.9s 不收尾（未達門檻）",
          sess_idle(59.9).should_auto_end()[0] is False)
    check("#9 未進入 idle（_idle_since=None）→ idle_held_seconds()=0 且不收尾",
          (lambda s: (s.idle_held_seconds() == 0.0
                      and s.should_auto_end()[0] is False))(mk_session()[0]))
    check("#9 idle 遠超門檻（3600s）仍為 auto_stop（不因超時改成別的 reason）",
          sess_idle(3600).should_auto_end() == (True, "auto_stop"))
finally:
    CFG.AUTO_NEXT_PLAN_GAP_SEC = _BAK_GAP
    if _BAK_GRACE is not None:
        CFG.AUTO_PLAN_START_GRACE_SEC = _BAK_GRACE
check(f"#9 測試後 AUTO_NEXT_PLAN_GAP_SEC 已還原（{CFG.AUTO_NEXT_PLAN_GAP_SEC}）",
      CFG.AUTO_NEXT_PLAN_GAP_SEC == _BAK_GAP)


# ======================================================================
# Batch 2 —— #6 Auth Recovery 邊界 ／ #7 communication error
# ======================================================================
print("\n[#6] Auth Recovery 邊界（AUTH_LOST_STREAK，反覆 _error401）")

EPA, EPG = RM.AUTH_ENDPOINTS, RM.GUEST_ENDPOINTS
check(f"AUTH_LOST_STREAK = 2（實際 {RM.AUTH_LOST_STREAK}）",
      RM.AUTH_LOST_STREAK == 2)
check(f"AUTH_ENDPOINTS {len(EPA)} 支 / GUEST_ENDPOINTS {len(EPG)} 支",
      len(EPA) == 2 and len(EPG) == 5)


def rd(auth_bad=2, guest_bad=0, code="_error401", with_fail=True):
    """組出指定失敗組合的 reading（只需 _fail，_auth_session_lost 只看它）。"""
    f = [f"{p}:{code}" for p in EPA[:auth_bad]]
    f += [f"{p}:{code}" for p in EPG[:guest_bad]]
    r = {"communication_ok": True}
    if with_fail:
        r["_fail"] = f
    return r


for ab, gb, want, frag in ((2, 0, True, "authed_all_failed"),
                           (2, 4, True, "guest_ok=1/5"),
                           (1, 0, False, "authed_failed=1/2"),
                           (0, 0, False, "authed_failed=0/2"),
                           (2, 5, False, "guest_also_down")):
    lost, why = RM._auth_session_lost(rd(ab, gb))
    check(f"#6 authed_bad={ab} guest_bad={gb} → lost={lost}，why 含 {frag!r}",
          lost is want and frag in why)

check("#6 reading 無 _fail 欄位 → 不臆測（no_fail_field）",
      RM._auth_session_lost(rd(with_fail=False)) == (False, "no_fail_field"))
check("#6 reading 非 dict → no_reading",
      RM._auth_session_lost(None) == (False, "no_reading"))
_l, _w = RM._auth_session_lost(rd(2, 0, code="_error401"))
check(f"#6 why 帶出 application code 供實機取證（{_w[:60]}…）",
      _l and "_error401" in _w)
check("#6 _fail 為裸路徑（無 code）亦能命中",
      RM._auth_session_lost({"_fail": list(EPA)})[0] is True)
check("#6 _ep_failed 不會被較長的相似路徑誤命中",
      RM._ep_failed([str(EPA[0]) + "X"], EPA[0]) is False)

# ---- streak / login storm ----
_BAK_INV = RM._invalidate_report_client
_inv_calls = []


def _spy_inv(reason):
    _inv_calls.append(reason)
    return _BAK_INV(reason)          # 保留真實語意（含 streak 歸零）


RM._invalidate_report_client = _spy_inv
_BAK_SESS, _BAK_TH = RM._report.get("session"), RM._report.get("thread")
try:
    RM._auto["auth_fail_streak"] = 0
    RM._report["client"] = FakeClient()
    _inv_calls.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        r1 = RM._check_auth_session(rd(2, 0))
        n1 = RM._auto["auth_fail_streak"]
        inv_after_1 = len(_inv_calls)      # ← 必須在第 1 輪當下取樣
        r2 = RM._check_auth_session(rd(2, 0))
        n2 = RM._auto["auth_fail_streak"]
    check(f"#6 第 1 輪失敗 → streak={n1}，未 invalidate"
          f"（回傳 {r1}，invalidate 次數 {inv_after_1}）",
          n1 == 1 and r1 is False and inv_after_1 == 0)
    check(f"#6 第 2 輪失敗 → 達門檻即 invalidate（{r2}），streak 歸零={n2}",
          r2 is True and len(_inv_calls) == 1 and n2 == 0)
    check("#6 invalidate 後 cached client 已清除",
          RM._report.get("client") is None)

    # 好的一輪必須把 streak 歸零（不累積跨越健康輪次）
    RM._auto["auth_fail_streak"] = 0
    with contextlib.redirect_stdout(io.StringIO()):
        RM._check_auth_session(rd(2, 0))
        mid = RM._auto["auth_fail_streak"]
        RM._check_auth_session(rd(0, 0))
    check(f"#6 健康一輪把 streak 歸零（{mid} → {RM._auto['auth_fail_streak']}）",
          mid == 1 and RM._auto["auth_fail_streak"] == 0)

    # login storm：100 輪連續失敗，invalidate 次數應為 100//2，不是 100
    RM._auto["auth_fail_streak"] = 0
    _inv_calls.clear()
    _sess_dirs_before = {d for d in os.listdir(TMP)
                         if os.path.exists(os.path.join(TMP, d,
                                                        CFG.FILE_SESSION_STATE))}
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(100):
            RM._check_auth_session(rd(2, 0))
    check(f"#6 無 login storm：100 輪連續失敗只 invalidate "
          f"{len(_inv_calls)} 次（= 100 // AUTH_LOST_STREAK = 50，非 100）",
          len(_inv_calls) == 100 // RM.AUTH_LOST_STREAK)
    check("#6 invalidate 不建立 Session、不起取樣執行緒",
          RM._report.get("session") is _BAK_SESS
          and RM._report.get("thread") is _BAK_TH)
    _sess_dirs_after = {d for d in os.listdir(TMP)
                        if os.path.exists(os.path.join(TMP, d,
                                                       CFG.FILE_SESSION_STATE))}
    check(f"#6 100 輪 invalidate 未新增任何 Session 資料夾"
          f"（前 {len(_sess_dirs_before)} → 後 {len(_sess_dirs_after)}）",
          _sess_dirs_after == _sess_dirs_before)
    check("#6 invalidate 不持有 Ownership", RM.is_owner() is False)
finally:
    RM._invalidate_report_client = _BAK_INV
    RM._auto["auth_fail_streak"] = 0
    RM._report["client"] = None
check("#6 測試後 _invalidate_report_client 已還原",
      RM._invalidate_report_client is _BAK_INV)


print("\n[#7] communication error（guest + authed 全失敗）")

check(f"COMM_FAIL_MAX = 3（實際 {CFG.COMM_FAIL_MAX}）", CFG.COMM_FAIL_MAX == 3)
check("communication_error 在自動結束白名單內，且對應 end_reason 同名",
      "communication_error" in CFG.AUTO_END_STOP_REASONS
      and CFG.AUTO_END_REASON_MAP["communication_error"] == "communication_error")

# 全端點失敗 → 不得誤判為「僅登入問題」
_all_bad = {"_fail": [f"{p}:_errorTimeout" for p in list(EPA) + list(EPG)],
            "communication_ok": False}
check("#7 guest + authed 全失敗 → 不誤判成 auth-only（guest_also_down）",
      RM._auth_session_lost(_all_bad) == (False, "guest_also_down"))

s7, root7 = mk_session()
advance(s7, 2)                                   # 兩筆正常
CDR.read_all = lambda c: fake_read_all(c, comm_ok=False)
advance(s7, CFG.COMM_FAIL_MAX - 1)               # 未達門檻
check(f"#7 連續失敗 {CFG.COMM_FAIL_MAX - 1} 次（未達 COMM_FAIL_MAX）→ "
      f"尚未列入 stop_reasons（{s7.stop_reasons}）",
      "communication_error" not in s7.stop_reasons)
advance(s7, 1)                                   # 第 3 次 → 達門檻
check(f"#7 連續失敗 {CFG.COMM_FAIL_MAX} 次 → stop_reasons 含 communication_error"
      f"（{s7.stop_reasons}）", "communication_error" in s7.stop_reasons)
_end, _reason = s7.should_auto_end()
check(f"#7 should_auto_end() = ({_end}, {_reason!r})",
      _end is True and _reason == "communication_error")

with contextlib.redirect_stdout(io.StringIO()):
    _stats7 = s7.finalize(_reason)
st7 = state(s7.folder)
ev7 = [r["event_type"] for r in rows(s7.folder, CFG.FILE_EVENTS)]
check(f"#7 Session 收尾為 completed（{st7.get('status')}）",
      st7.get("status") == CFG.SESSION_COMPLETED)
_sum7 = json.load(io.open(os.path.join(s7.folder, CFG.FILE_SUMMARY),
                          encoding="utf-8"))
_er7 = json.dumps(_sum7, ensure_ascii=False)
check("#7 summary 記錄 end_reason = communication_error",
      '"end_reason": "communication_error"' in _er7)
check(f"#7 statistics.communication_error_count = "
      f"{_stats7.get('communication_error_count')}（= 失敗取樣數 {CFG.COMM_FAIL_MAX}）",
      _stats7.get("communication_error_count") == CFG.COMM_FAIL_MAX)
check(f"#7 events 恰好 1 筆 session_end 與 1 筆 finalize_ok（不重複 finalize）"
      f"｜{ev7.count('session_end')}/{ev7.count('finalize_ok')}",
      ev7.count("session_end") == 1 and ev7.count("finalize_ok") == 1)
check("#7 report.xlsx 已產生且 output_status 無 failed",
      os.path.exists(os.path.join(s7.folder, CFG.FILE_XLSX))
      and "failed" not in json.dumps(st7.get("output_status") or {}))
# 恢復通訊後 read_all 回正常 —— 驗證下一份 Session 不被上一份污染
CDR.read_all = lambda c: fake_read_all(c)
s7b, _ = mk_session()
advance(s7b, 3)
check("#7 通訊恢復後新 Session 乾淨（stop_reasons 空、comm streak 未沿用）",
      not s7b.stop_reasons and s7b.should_auto_end()[0] is False)


# ======================================================================
# Batch 3 —— #4 長時間 recording ／ #5 stop / start 邊界
# ======================================================================
print("\n[#4] 長時間 recording（假時鐘，不實際等待）")

check(f"AUTO_END_TIMEOUT_SEC = 0（不限；實際 {CFG.AUTO_END_TIMEOUT_SEC}）",
      CFG.AUTO_END_TIMEOUT_SEC == 0)
check(f"ENERGY_METHOD = {CFG.ENERGY_METHOD!r}（決定積分公式）",
      CFG.ENERGY_METHOD in ("rectangle", "trapezoid"))

CDR.read_all = lambda c: fake_read_all(c, power=10.0)
s4, _ = mk_session()
advance(s4, 24, step=3600)                       # 24 筆，每筆間隔 1 小時
_el4 = s4._elapsed()
check(f"#4 elapsed = {_el4:.0f}s（24 × 3600 = 86400）", abs(_el4 - 86400) < 1)
# N 筆樣本 → N-1 個積分區間：首筆只建立基準（_prev_power/_prev_t），不虛構區間
_want4 = 23 * 10.0
check(f"#4 能量積分 = {s4.energy.charged_kwh:.4f} kWh"
      f"（23 區間 × 10kW × 1h = {_want4}；首筆只建基準不積分）",
      abs(s4.energy.charged_kwh - _want4) < 0.001)
check(f"#4 discharged = {s4.energy.discharged_kwh}（單向充電應為 0）",
      s4.energy.discharged_kwh == 0.0)
check(f"#4 sample_index 連續遞增至 24（實際 {s4.sample_index}）",
      s4.sample_index == 24)
check("#4 時間拉長不觸發自動收尾（AUTO_END_TIMEOUT_SEC=0 且未進 idle）",
      s4.should_auto_end() == (False, None))

# 極長時間（365 天）不 overflow、不失去精度單調性
s4b, _ = mk_session()
advance(s4b, 365, step=86400)
_el4b, _e4b = s4b._elapsed(), s4b.energy.charged_kwh
check(f"#4 365 天：elapsed={_el4b:.0f}s、charged={_e4b:.1f} kWh"
      f"（364 × 240 = {364 * 240.0}）不 overflow",
      abs(_el4b - 365 * 86400) < 1 and abs(_e4b - 364 * 240.0) < 0.01
      and _e4b == _e4b)                          # NaN 檢查
check("#4 能量單調不減（充電方向）", _e4b > _want4)
check("#4 長時間後仍不自行收尾", s4b.should_auto_end() == (False, None))

# 硬性上限開啟時必須生效（測完還原）—— 證明 timeout 路徑本身沒壞
_BAK_TO = CFG.AUTO_END_TIMEOUT_SEC
try:
    CFG.AUTO_END_TIMEOUT_SEC = 3600
    check("#4 AUTO_END_TIMEOUT_SEC 開啟後，超過上限即 (True, 'timeout')",
          s4b.should_auto_end() == (True, "timeout"))
finally:
    CFG.AUTO_END_TIMEOUT_SEC = _BAK_TO
check(f"#4 測試後 AUTO_END_TIMEOUT_SEC 已還原（{CFG.AUTO_END_TIMEOUT_SEC}）",
      CFG.AUTO_END_TIMEOUT_SEC == _BAK_TO)


print("\n[#5] stop / start 邊界")

CDR.read_all = lambda c: fake_read_all(c)

# (a) 極短 Session：0 筆取樣即收尾
s5a, _ = mk_session()
with contextlib.redirect_stdout(io.StringIO()):
    s5a.finalize("auto_stop")
st5a, ev5a = state(s5a.folder), [r["event_type"] for r
                                in rows(s5a.folder, CFG.FILE_EVENTS)]
check(f"#5a 極短 Session（0 筆）仍正常 completed（{st5a.get('status')}）",
      st5a.get("status") == CFG.SESSION_COMPLETED)
check(f"#5a sample_count = 0，output_status 無 failed（{st5a.get('output_status')}）",
      st5a.get("sample_count") == 0
      and "failed" not in json.dumps(st5a.get("output_status") or {}))
check(f"#5a events 恰好 1 筆 session_end / 1 筆 finalize_ok",
      ev5a.count("session_end") == 1 and ev5a.count("finalize_ok") == 1)
check("#5a 零樣本仍產生 report.xlsx（不留半成品）",
      os.path.exists(os.path.join(s5a.folder, CFG.FILE_XLSX)))

# (b) 取樣中直接收尾（不先 pause）
s5b, _ = mk_session()
advance(s5b, 5)
with contextlib.redirect_stdout(io.StringIO()):
    s5b.finalize("auto_stop")
st5b, ev5b = state(s5b.folder), [r["event_type"] for r
                                in rows(s5b.folder, CFG.FILE_EVENTS)]
check(f"#5b 取樣中直接收尾 → completed，樣本完整保留"
      f"（sample_count={st5b.get('sample_count')}，csv {len(rows(s5b.folder, 'samples.csv'))} 列）",
      st5b.get("status") == CFG.SESSION_COMPLETED
      and st5b.get("sample_count") == 5
      and len(rows(s5b.folder, "samples.csv")) == 5)
check("#5b 無重複 finalize（session_end / finalize_ok 各 1 筆）",
      ev5b.count("session_end") == 1 and ev5b.count("finalize_ok") == 1)

# (c) pause 後再 finalize
s5c, _ = mk_session()
advance(s5c, 3)
with contextlib.redirect_stdout(io.StringIO()):
    s5c.pause()
st5c1 = state(s5c.folder)
ev5c1 = [r["event_type"] for r in rows(s5c.folder, CFG.FILE_EVENTS)]
check(f"#5c pause → status={st5c1.get('status')}，events 記 recording_pause"
      f"（無 session_end）",
      st5c1.get("status") == CFG.SESSION_PAUSED
      and "recording_pause" in ev5c1 and "session_end" not in ev5c1)
with contextlib.redirect_stdout(io.StringIO()):
    s5c.finalize("auto_stop")
st5c2 = state(s5c.folder)
ev5c2 = [r["event_type"] for r in rows(s5c.folder, CFG.FILE_EVENTS)]
check(f"#5c pause 後 finalize → completed，session_end / finalize_ok 各 1 筆",
      st5c2.get("status") == CFG.SESSION_COMPLETED
      and ev5c2.count("session_end") == 1
      and ev5c2.count("finalize_ok") == 1
      and ev5c2.count("recording_pause") == 1)

# (d) 重複收尾：守門在**監看層**，不在 ReportSession
#     ReportSession.finalize() 是低層原始操作，本身不守門 —— 直接連呼兩次會寫兩筆。
#     產品路徑不會二次到達，因為 _stop_and_finalize 以 stopping / finalized 旗標守門。
s5d, _ = mk_session()
advance(s5d, 3)
with contextlib.redirect_stdout(io.StringIO()):
    s5d.finalize("auto_stop")
    s5d.finalize("auto_stop")
ev5d = [r["event_type"] for r in rows(s5d.folder, CFG.FILE_EVENTS)]
check(f"#5d 低層 ReportSession.finalize() 連呼兩次 → 寫入兩筆"
      f"（session_end {ev5d.count('session_end')} / "
      f"finalize_ok {ev5d.count('finalize_ok')}）—— 守門不在此層，鎖定現況",
      ev5d.count("session_end") == 2 and ev5d.count("finalize_ok") == 2)
check("#5d _stop_and_finalize docstring 明載 idempotent（守門位置）",
      "idempotent" in (RM._stop_and_finalize.__doc__ or ""))

# 監看層 idempotent：第二次呼叫必須直接返回 (None, None)，不產生第二筆收尾
s5e, _ = mk_session()
advance(s5e, 3)
_BAK_REPORT = dict(RM._report)
_BAK_STATE = RM._auto.get("state")
try:
    RM._report.update({"session": s5e, "stopping": False, "finalized": False,
                       "stop": None, "thread": None})
    with contextlib.redirect_stdout(io.StringIO()):
        a1, b1 = RM._stop_and_finalize("auto_stop")
        a2, b2 = RM._stop_and_finalize("auto_stop")
    ev5e = [r["event_type"] for r in rows(s5e.folder, CFG.FILE_EVENTS)]
    check(f"#5e 監看層第 1 次收尾成功（sess={a1 is not None}, stats={b1 is not None}）",
          a1 is s5e and b1 is not None)
    check(f"#5e 監看層第 2 次為 idempotent，直接回 (None, None)",
          a2 is None and b2 is None)
    check(f"#5e 監看層守門下 events 仍只有 1 筆 session_end / finalize_ok"
          f"（{ev5e.count('session_end')}/{ev5e.count('finalize_ok')}）",
          ev5e.count("session_end") == 1 and ev5e.count("finalize_ok") == 1)
    check(f"#5e 收尾後 status = completed（{state(s5e.folder).get('status')}）",
          state(s5e.folder).get("status") == CFG.SESSION_COMPLETED)
finally:
    RM._report.clear()
    RM._report.update(_BAK_REPORT)
    RM._auto["state"] = _BAK_STATE
check("#5e 測試後 _report / _auto state 已還原",
      RM._report.get("session") is _BAK_REPORT.get("session")
      and RM._auto.get("state") == _BAK_STATE)

# (f) 由 mk_session() 建立的真實 Session 不得殘留 recording / paused
#     ⚠️ 範圍只取 p53_sess_ 前綴：#8 的 p53_orphan_* 是**刻意合成**的
#        status=recording fixture（用來餵 _orphan_gap_seconds），不是殘留。
_sess_dirs = [d for d in os.listdir(TMP) if d.startswith("p53_sess_")]
_left = []
for d in _sess_dirs:
    sp = os.path.join(TMP, d, CFG.FILE_SESSION_STATE)
    if os.path.exists(sp):
        stx = (json.load(io.open(sp, encoding="utf-8")) or {}).get("status")
        if stx != CFG.SESSION_COMPLETED:
            _left.append((d, stx))
check(f"#5f mk_session 建立的 {len(_sess_dirs)} 份 Session 皆已 completed，"
      f"無 recording / paused 殘留（例外 {_left or '無'}）", not _left)
_orph = [d for d in os.listdir(TMP) if d.startswith("p53_orphan")]
check(f"#5f 對照：#8 的 orphan fixture {len(_orph)} 份維持 recording"
      f"（刻意合成，證明上一條不是因為掃不到東西而通過）",
      len(_orph) >= 3)


# ======================================================================
# Batch 4 —— #1 多次 Session 建立／完成 ／ #3 samples.csv 大量累積
# ======================================================================
print("\n[#1] 多次 Session 建立／完成（連續 N 輪，同一 root）")

N_ROUNDS = 10
ROOT1 = tempfile.mkdtemp(prefix="p53_multi_", dir=TMP)
CDR.read_all = lambda c: fake_read_all(c)
_ids, _folders, _energies = [], [], []
for i in range(N_ROUNDS):
    with contextlib.redirect_stdout(io.StringIO()):
        s = CDR.ReportSession("auto", None, "交流有功", FakeClient(), ROOT1,
                              now_fn=_now)
        s.start()
        for _ in range(4):
            _CLK["dt"] += timedelta(seconds=5)
            s.sample_once()
        s.finalize("auto_stop")
    _ids.append(s.session_id)
    _folders.append(s.folder)
    _energies.append(round(s.energy.charged_kwh, 6))
    _CLK["dt"] += timedelta(seconds=30)          # 讓下一輪的 session_id 不同秒

check(f"#1 {N_ROUNDS} 輪 session_id 全部唯一（{len(set(_ids))} 個不同）",
      len(set(_ids)) == N_ROUNDS)
check(f"#1 {N_ROUNDS} 輪 folder 全部唯一", len(set(_folders)) == N_ROUNDS)
_st1 = [state(f).get("status") for f in _folders]
check(f"#1 每輪皆 completed（{set(_st1)}）", set(_st1) == {CFG.SESSION_COMPLETED})
_rec1 = [f for f in _folders if state(f).get("status") == "recording"]
_pau1 = [f for f in _folders if state(f).get("status") == CFG.SESSION_PAUSED]
check(f"#1 無 recording（{len(_rec1)}）／無 paused（{len(_pau1)}）殘留",
      not _rec1 and not _pau1)
check(f"#1 find_active_session(root) = None（{CDR.find_active_session(ROOT1)}）",
      CDR.find_active_session(ROOT1) is None)
check(f"#1 每輪能量相同、不累加到下一輪（{set(_energies)}）",
      len(set(_energies)) == 1 and _energies[0] > 0)
_cnt1 = [state(f).get("sample_count") for f in _folders]
check(f"#1 每輪 sample_count 皆為 4，前一輪不污染下一輪（{set(_cnt1)}）",
      set(_cnt1) == {4})
_rows1 = [len(rows(f, "samples.csv")) for f in _folders]
check(f"#1 每輪 samples.csv 皆 4 列（{set(_rows1)}）", set(_rows1) == {4})
_ev1 = [[r["event_type"] for r in rows(f, CFG.FILE_EVENTS)] for f in _folders]
check("#1 每輪 session_end / finalize_ok 各恰好 1 筆（無重複 writer）",
      all(e.count("session_end") == 1 and e.count("finalize_ok") == 1
          for e in _ev1))
check("#1 每輪 sample_index 皆自 1 起算（跨輪不延續）",
      all([int(r["sample_index"]) for r in rows(f, "samples.csv")][0] == 1
          for f in _folders))
check(f"#1 root 下恰好 {N_ROUNDS} 份 Session 資料夾（無多餘產物）",
      len([d for d in os.listdir(ROOT1)
           if os.path.isdir(os.path.join(ROOT1, d))]) == N_ROUNDS)

# COOLDOWN 狀態機可回收
_BAK_CD = _auto_cd = RM._auto.get("cooldown_until")
try:
    RM._auto["cooldown_until"] = time.monotonic() + CFG.AUTO_COOLDOWN_SEC
    _rem = RM._auto_cooldown_remaining()
    check(f"#1 冷卻中 remaining≈{_rem:.0f}s（AUTO_COOLDOWN_SEC={CFG.AUTO_COOLDOWN_SEC}）",
          0 < _rem <= CFG.AUTO_COOLDOWN_SEC)
    RM._auto["cooldown_until"] = time.monotonic() - 1
    check(f"#1 冷卻期滿 → remaining = {RM._auto_cooldown_remaining()}（回收為 0.0）",
          RM._auto_cooldown_remaining() == 0.0)
    RM._auto["cooldown_until"] = None
    check("#1 無冷卻 → remaining = 0.0（不因呼叫頻率改變）",
          RM._auto_cooldown_remaining() == 0.0
          and RM._auto_cooldown_remaining() == 0.0)
finally:
    RM._auto["cooldown_until"] = _BAK_CD


print("\n[#3] samples.csv 大量累積（10,000 筆）")

N_SAMP = 10000
ROOT3 = tempfile.mkdtemp(prefix="p53_bulk_", dir=TMP)
CDR.read_all = lambda c: fake_read_all(c, power=10.0)
_t3 = time.monotonic()
with contextlib.redirect_stdout(io.StringIO()):
    s3 = CDR.ReportSession("auto", None, "交流有功", FakeClient(), ROOT3,
                           now_fn=_now)
    s3.start()
    for _ in range(N_SAMP):
        _CLK["dt"] += timedelta(seconds=5)
        s3.sample_once()
_el3 = time.monotonic() - _t3
print(f"       （observation：{N_SAMP} 筆取樣耗時 {_el3:.1f}s）")

check(f"#3 記憶體中 samples 筆數 = {len(s3.samples)}（預期 {N_SAMP}）",
      len(s3.samples) == N_SAMP)
check(f"#3 sample_index 最終值 = {s3.sample_index}（預期 {N_SAMP}）",
      s3.sample_index == N_SAMP)
_csv3 = rows(s3.folder, "samples.csv")
check(f"#3 samples.csv 實際列數 = {len(_csv3)}（預期 {N_SAMP}）",
      len(_csv3) == N_SAMP)
_idx3 = [int(r["sample_index"]) for r in _csv3]
check(f"#3 sample_index 連續 1..{N_SAMP}（無缺號）",
      _idx3 == list(range(1, N_SAMP + 1)))
check(f"#3 sample_index 無重複（unique {len(set(_idx3))}）",
      len(set(_idx3)) == N_SAMP)
_want3 = (N_SAMP - 1) * 10.0 * (5 / 3600.0)
check(f"#3 能量積分 = {s3.energy.charged_kwh:.6f} kWh"
      f"（{N_SAMP - 1} 區間 × 10kW × 5s = {_want3:.6f}）",
      abs(s3.energy.charged_kwh - _want3) < 1e-6)
check(f"#3 elapsed = {s3._elapsed():.0f}s（{N_SAMP} × 5s）",
      abs(s3._elapsed() - N_SAMP * 5) < 1)
with contextlib.redirect_stdout(io.StringIO()):
    _stats3 = s3.finalize("auto_stop")
_st3 = state(s3.folder)
check(f"#3 收尾後 sample_count = {_st3.get('sample_count')}"
      f"，與 csv 列數 {len(rows(s3.folder, 'samples.csv'))} 一致",
      _st3.get("sample_count") == len(rows(s3.folder, "samples.csv")))
check(f"#3 status = completed，output_status 無 failed"
      f"（{_st3.get('output_status')}）",
      _st3.get("status") == CFG.SESSION_COMPLETED
      and "failed" not in json.dumps(_st3.get("output_status") or {}))
check(f"#3 statistics 的 duration_seconds 與 elapsed 一致"
      f"（{_stats3.get('duration_seconds')}）",
      abs((_stats3.get("duration_seconds") or 0) - (N_SAMP * 5 + 5)) < 10)
check("#3 report.xlsx 於 10k 筆規模仍產生成功",
      os.path.exists(os.path.join(s3.folder, CFG.FILE_XLSX))
      and os.path.getsize(os.path.join(s3.folder, CFG.FILE_XLSX)) > 10000)


# ======================================================================
# Batch 5 —— #10 finalize / output retry（真實 Windows 檔案鎖）
# ======================================================================
print("\n[#10] finalize / output retry（_retry_file_op 三態 + 真實檔案鎖）")

check(f"FINALIZE_RETRY_MAX = 3（實際 {CFG.FINALIZE_RETRY_MAX}）",
      CFG.FINALIZE_RETRY_MAX == 3)
check(f"FINALIZE_RETRY_WAIT_SEC = 2.0（實際 {CFG.FINALIZE_RETRY_WAIT_SEC}）",
      CFG.FINALIZE_RETRY_WAIT_SEC == 2.0)

_slept = []


def _no_sleep(sec):
    _slept.append(sec)                               # 記錄但不實際等待


# --- 三態直接驗證（不必跑完整 finalize，直接打 _retry_file_op）---
_n = {"i": 0}


def _ok_first():
    _n["i"] += 1
    return True


_slept.clear(); _n["i"] = 0
with contextlib.redirect_stdout(io.StringIO()):
    _stt, _val, _att = CDR._retry_file_op("t", _ok_first, sleep_fn=_no_sleep)
check(f"#10 一次即成功 → ({_stt}, attempts={_att})，未等待（sleep {len(_slept)} 次）",
      _stt == CFG.OUTPUT_OK and _att == 1 and not _slept)


def _fail_twice():
    _n["i"] += 1
    if _n["i"] < 3:
        raise PermissionError("locked")
    return True


_slept.clear(); _n["i"] = 0
with contextlib.redirect_stdout(io.StringIO()):
    _stt, _val, _att = CDR._retry_file_op("t", _fail_twice, sleep_fn=_no_sleep)
check(f"#10 前 2 次 PermissionError、第 3 次成功 → ({_stt}, attempts={_att})，"
      f"等待 {len(_slept)} 次 × {set(_slept) or '—'}s",
      _stt == CFG.OUTPUT_OK and _att == 3 and len(_slept) == 2
      and set(_slept) == {CFG.FINALIZE_RETRY_WAIT_SEC})


def _always_locked():
    _n["i"] += 1
    raise PermissionError("locked")


_slept.clear(); _n["i"] = 0
with contextlib.redirect_stdout(io.StringIO()):
    _stt, _val, _att = CDR._retry_file_op("t", _always_locked, sleep_fn=_no_sleep)
check(f"#10 持續占用 → ({_stt}, attempts={_att})，重試次數 = "
      f"FINALIZE_RETRY_MAX（{CFG.FINALIZE_RETRY_MAX}）",
      _stt == CFG.OUTPUT_FAILED_LOCKED
      and _att == CFG.FINALIZE_RETRY_MAX
      and _n["i"] == CFG.FINALIZE_RETRY_MAX)

_slept.clear(); _n["i"] = 0
_stt, _val, _att = CDR._retry_file_op(
    "t", lambda: (_n.__setitem__("i", _n["i"] + 1), None)[1], sleep_fn=_no_sleep)
check(f"#10 回傳 None（不適用）→ ({_stt}, attempts={_att})，不重試",
      _stt == CFG.OUTPUT_SKIPPED and _att == 1 and _n["i"] == 1 and not _slept)

_slept.clear(); _n["i"] = 0


def _logic_error():
    _n["i"] += 1
    raise ValueError("程式邏輯錯誤")


with contextlib.redirect_stdout(io.StringIO()):
    _stt, _val, _att = CDR._retry_file_op("t", _logic_error, sleep_fn=_no_sleep)
check(f"#10 非 OSError 例外 → ({_stt}, attempts={_att})，**不重試**"
      f"（重試不會成功，只會拖慢收尾）",
      _stt == CFG.OUTPUT_FAILED and _att == 1 and _n["i"] == 1 and not _slept)

_slept.clear(); _n["i"] = 0
_stt, _val, _att = CDR._retry_file_op(
    "t", lambda: (_n.__setitem__("i", _n["i"] + 1), False)[1], sleep_fn=_no_sleep)
check(f"#10 回傳 False（既有函式自行 catch OSError 的表達方式）→ "
      f"({_stt}, attempts={_att}) 視為可重試",
      _stt == CFG.OUTPUT_FAILED_LOCKED and _att == CFG.FINALIZE_RETRY_MAX)

# --- 真實 Windows 檔案鎖（sharing violation，等同 Excel 開著該檔）---
import ctypes                                                       # noqa: E402
import ctypes.wintypes as _wt                                       # noqa: E402

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = _wt.HANDLE
_k32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, ctypes.c_void_p,
                             _wt.DWORD, _wt.DWORD, _wt.HANDLE]
_GENERIC_READ, _OPEN_EXISTING, _ATTR_NORMAL = 0x80000000, 3, 0x80
_INVALID = ctypes.c_void_p(-1).value


def lock_exclusive(path):
    """以 share mode 0 獨佔開啟 → 其他寫入者會拿到真正的 sharing violation。"""
    h = _k32.CreateFileW(path, _GENERIC_READ, 0, None, _OPEN_EXISTING,
                         _ATTR_NORMAL, None)
    return None if h == _INVALID else h


ROOT10 = tempfile.mkdtemp(prefix="p53_lock_", dir=TMP)
CDR.read_all = lambda c: fake_read_all(c)
with contextlib.redirect_stdout(io.StringIO()):
    s10 = CDR.ReportSession("auto", None, "交流有功", FakeClient(), ROOT10,
                            now_fn=_now)
    s10.start()
    for _ in range(3):
        _CLK["dt"] += timedelta(seconds=5)
        s10.sample_once()
    s10.finalize("auto_stop")                    # 先產生一份正常的 report.xlsx
_xl10 = os.path.join(s10.folder, CFG.FILE_XLSX)
_size_before = os.path.getsize(_xl10)
check(f"#10 前置：report.xlsx 已產生（{_size_before} bytes）", _size_before > 0)

_h = lock_exclusive(_xl10)
check("#10 已對 report.xlsx 取得真實獨佔鎖（share mode 0）", _h is not None)
_perm_err = None
try:
    with io.open(_xl10, "ab") as _f:
        _f.write(b"x")
except OSError as e:
    _perm_err = type(e).__name__
check(f"#10 鎖定期間寫入確實被 OS 拒絕（{_perm_err}）—— 這是真實 sharing violation",
      _perm_err in ("PermissionError", "OSError"))

_slept.clear()
with contextlib.redirect_stdout(io.StringIO()):
    _stt, _val, _att = CDR._retry_file_op(
        CFG.FILE_XLSX, lambda: io.open(_xl10, "ab").close() or True,
        sleep_fn=_no_sleep)
check(f"#10 真實鎖 + 重試耗盡 → ({_stt}, attempts={_att})",
      _stt == CFG.OUTPUT_FAILED_LOCKED and _att == CFG.FINALIZE_RETRY_MAX)
check(f"#10 重試耗盡未破壞既有檔案（{os.path.getsize(_xl10)} bytes，"
      f"與鎖定前 {_size_before} 相同）",
      os.path.getsize(_xl10) == _size_before)

_k32.CloseHandle(_h)
_slept.clear()
with contextlib.redirect_stdout(io.StringIO()):
    _stt, _val, _att = CDR._retry_file_op(
        CFG.FILE_XLSX, lambda: io.open(_xl10, "ab").close() or True,
        sleep_fn=_no_sleep)
check(f"#10 解鎖後同一操作立即成功 → ({_stt}, attempts={_att})",
      _stt == CFG.OUTPUT_OK and _att == 1)

# 鎖定事件不影響下一次 Session
with contextlib.redirect_stdout(io.StringIO()):
    s10b = CDR.ReportSession("auto", None, "交流有功", FakeClient(), ROOT10,
                             now_fn=_now)
    s10b.start()
    for _ in range(3):
        _CLK["dt"] += timedelta(seconds=5)
        s10b.sample_once()
    s10b.finalize("auto_stop")
_st10b = state(s10b.folder)
check(f"#10 下一次 Session 不受先前鎖定影響"
      f"（status={_st10b.get('status')}, output_status={_st10b.get('output_status')}）",
      _st10b.get("status") == CFG.SESSION_COMPLETED
      and "failed" not in json.dumps(_st10b.get("output_status") or {}))
check("#10 兩份 Session 的輸出各自獨立、無半成品",
      os.path.getsize(os.path.join(s10b.folder, CFG.FILE_XLSX)) > 0
      and s10b.folder != s10.folder)


# ======================================================================
# Batch 6 —— #2 Session 數量增加（500 / 1000 / 2000）
# ======================================================================
print("\n[#2] Session 數量增加（find_active_session 於大量目錄下的正確性）")


def bulk_root(n, active_name=None, malformed=True):
    """
    建 n 個 completed Session 目錄；active_name 指定時額外建一份 recording。
    malformed=True 時另加畸形／無關目錄，驗證不會造成錯誤結果。
    """
    r = tempfile.mkdtemp(prefix=f"p53_bulk{n}_", dir=TMP)
    for i in range(n):
        d = os.path.join(r, f"2026010{i % 9 + 1}_{i:06d}_auto")
        os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(d, CFG.FILE_SESSION_STATE), "w",
                     encoding="utf-8") as fh:
            json.dump({"status": CFG.SESSION_COMPLETED,
                       "session_id": f"{i:06d}_auto"}, fh)
    if malformed:
        os.makedirs(os.path.join(r, "no_state_here"), exist_ok=True)
        _bd = os.path.join(r, "bad_json_dir")
        os.makedirs(_bd, exist_ok=True)
        with io.open(os.path.join(_bd, CFG.FILE_SESSION_STATE), "w",
                     encoding="utf-8") as fh:
            fh.write("{not json")
        _ed = os.path.join(r, "empty_state_dir")
        os.makedirs(_ed, exist_ok=True)
        io.open(os.path.join(_ed, CFG.FILE_SESSION_STATE), "w",
                encoding="utf-8").close()
        with io.open(os.path.join(r, "loose_file.txt"), "w",
                     encoding="utf-8") as fh:
            fh.write("not a session")
    if active_name:
        d = os.path.join(r, active_name)
        os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(d, CFG.FILE_SESSION_STATE), "w",
                     encoding="utf-8") as fh:
            json.dump({"status": "recording", "session_id": active_name}, fh)
    return r


for n in (500, 1000, 2000):
    r_all_done = bulk_root(n)
    t0 = time.monotonic()
    got = CDR.find_active_session(r_all_done)
    dt_none = time.monotonic() - t0
    check(f"#2 {n} 份全 completed（含畸形目錄）→ find_active_session = None"
          f"（{dt_none * 1000:.0f} ms）", got is None)

    _act = f"29991231_{n:06d}_auto"
    r_one = bulk_root(n, active_name=_act)
    t0 = time.monotonic()
    got = CDR.find_active_session(r_one)
    dt_one = time.monotonic() - t0
    check(f"#2 {n} 份 completed + 1 份 recording → 正確選中該份"
          f"（{os.path.basename(str(got))}，{dt_one * 1000:.0f} ms）",
          got is not None and os.path.basename(str(got)) == _act)
    print(f"       （observation：n={n} → None {dt_none * 1000:.0f} ms / "
          f"命中 {dt_one * 1000:.0f} ms）")

# completed 不得被誤判為 active；畸形目錄不得造成誤判或例外
_r2 = bulk_root(50)
check("#2 completed Session 不被誤判為 active",
      CDR.find_active_session(_r2) is None)
_r3 = bulk_root(50, active_name="20991231_000001_auto")
check("#2 畸形 JSON / 空檔 / 無 state / 散落檔案 皆不影響命中結果",
      os.path.basename(str(CDR.find_active_session(_r3))) == "20991231_000001_auto")
_r4 = tempfile.mkdtemp(prefix="p53_empty_", dir=TMP)
check("#2 空 root → None（不拋例外）", CDR.find_active_session(_r4) is None)
_r5 = bulk_root(10, malformed=True)
_p5 = os.path.join(_r5, "paused_one")
os.makedirs(_p5, exist_ok=True)
with io.open(os.path.join(_p5, CFG.FILE_SESSION_STATE), "w",
             encoding="utf-8") as fh:
    json.dump({"status": CFG.SESSION_PAUSED, "session_id": "paused_one"}, fh)
check("#2 paused Session 亦被視為 active（可續接）",
      os.path.basename(str(CDR.find_active_session(_r5))) == "paused_one")


# ======================================================================
# Batch 7 —— #11 Ownership contention（跨行程）
#   ⚠️ 先過三條「隔離前置硬性斷言」才允許執行，任一不成立即 fatal()。
#   ⚠️ 不使用任何強制終止指令：崩潰情境以子行程自行 os._exit(0) 模擬。
# ======================================================================
print("\n[#11] Ownership contention（跨行程；先過隔離前置斷言）")

import subprocess                                                   # noqa: E402

CHILD = os.path.join(HERE, "_p53_stress_child.py")
check(f"#11 子行程 helper 存在（{os.path.basename(CHILD)}）",
      os.path.isfile(CHILD))


def run_child(mode, root, *extra, wait=True):
    p = subprocess.Popen([sys.executable, CHILD, mode, root, *map(str, extra)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if not wait:
        return p
    out = p.stdout.read().decode("utf-8", "replace")
    return parse_child(out), p.returncode


def parse_child(out):
    for line in reversed(out.splitlines()):
        if line.startswith("__P53__"):
            return json.loads(line[len("__P53__"):])
    return {"_unparsed": out[-400:]}


def child_root(tag):
    return tempfile.mkdtemp(prefix=f"p53_own_{tag}_", dir=TMP)


# ---- 前置斷言 1 + 2：父行程與每個子行程的 mutex / root ----
PARENT_MUTEX = RM._mutex_name()
check(f"#11 前置1 父行程 mutex ≠ 正式 canonical"
      f"（…{PARENT_MUTEX[-16:]} vs …{PROD_MUTEX[-16:]}）",
      PARENT_MUTEX != PROD_MUTEX)

_iso_roots = [child_root(f"iso{i}") for i in range(3)]
_iso_reports = []
for i, r in enumerate(_iso_roots):
    rep, rc = run_child("report", r)
    _iso_reports.append(rep)
    ok1 = rep.get("mutex") not in (None, PROD_MUTEX)
    ok2 = (_norm(rep.get("root", "")) .startswith(_norm(tempfile.gettempdir()))
           and _norm(rep.get("root", "")) != _norm(PROD_ROOT)
           and not _norm(rep.get("root", "")).startswith(_norm(PROD_ROOT)))
    check(f"#11 前置1 子行程 {i} 回報 mutex …{str(rep.get('mutex'))[-16:]}"
          f" ≠ 正式 canonical", ok1)
    check(f"#11 前置2 子行程 {i} root 位於暫存目錄、≠ 正式 root、不在其下",
          ok2)

check(f"#11 前置1 三個子行程 mutex 互不相同（不同 root → 不同 namespace）",
      len({r.get("mutex") for r in _iso_reports}) == 3)
if not all(RESULTS):
    fatal("#11 隔離前置斷言未通過 —— 不執行 Ownership contention")

# ---- 前置斷言 3 的前半：記錄正式 output 與 owner 檔指紋 ----
_PROD_OWNER_FP = None
if os.path.exists(PROD_OWNER):
    _st = os.stat(PROD_OWNER)
    _PROD_OWNER_FP = (_st.st_size, _st.st_mtime_ns,
                      hashlib.sha256(open(PROD_OWNER, "rb").read()).hexdigest())
_FP_BEFORE_1112 = _fp(PROD_ROOT)
print(f"       （#11/#12 前 正式 output 指紋：{_FP_BEFORE_1112[0]} 檔 "
      f"{_FP_BEFORE_1112[1][:16]}…）")

# ---- (a) 單一 Owner：A 持有中，B 必須 held_by_other 且 fail closed ----
R_A = child_root("a")
_pA = run_child("contend", R_A, 4.0, wait=False)
# 等 A 真的取得（以 owner 檔出現為訊號，最多等 10s）
_owner_a = os.path.join(R_A, CFG.MONITOR_OWNER_FILE)
_t = time.monotonic()
while not os.path.exists(_owner_a) and time.monotonic() - _t < 10:
    time.sleep(0.05)
check(f"#11a 子行程 A 已取得所有權（owner 檔於 temp root 出現）",
      os.path.exists(_owner_a))
_repB, _rcB = run_child("try", R_A)
check(f"#11a A 持有期間 B 取得失敗 → ok={_repB.get('ok')}, "
      f"why={_repB.get('why')!r}（fail closed）",
      _repB.get("ok") is False and _repB.get("why") == "held_by_other")
# ⚠️ 父行程必須先把 root 指向 **A 的 root**，否則算出的是自己 TMP 的 mutex，
#    根本不在同一個 namespace，當然會取得成功（本測試第一版就是這樣誤判的）。
_bak_root_a = RM._report_output_root
RM._report_output_root = lambda: R_A
try:
    check(f"#11a 父行程已指向 A 的 root，mutex 與 A 相同"
          f"（…{RM._mutex_name()[-16:]}）",
          RM._mutex_name() == _repB.get("mutex"))
    _repP = RM.acquire_ownership(role="p53-parent-try")
    if _repP[0]:
        RM.release_ownership()
finally:
    RM._report_output_root = _bak_root_a
check(f"#11a A 持有期間父行程亦被擋下（{_repP}）",
      _repP[0] is False and _repP[1] == "held_by_other")
_outA = _pA.stdout.read().decode("utf-8", "replace")
_pA.wait()
_repA = parse_child(_outA)
check(f"#11a A 自身取得成功（ok={_repA.get('ok')}, why={_repA.get('why')!r}, "
      f"abandoned={_repA.get('abandoned')}）",
      _repA.get("ok") is True and _repA.get("why") == "acquired"
      and _repA.get("abandoned") is False)
check("#11a A 的 owner 檔在其 temp root 內（不在正式 root）",
      _norm(str(_repA.get("owner_file", ""))).startswith(_norm(R_A)))

# ---- (b) release 後可 takeover ----
_repC, _ = run_child("acquire", R_A)
check(f"#11b A 釋放後 C 可接手（ok={_repC.get('ok')}, why={_repC.get('why')!r}）",
      _repC.get("ok") is True and _repC.get("why") == "acquired")
check(f"#11b 持有期間 owner 檔存在、釋放後移除"
      f"（{_repC.get('owner_file_existed_while_held')} → "
      f"{_repC.get('owner_file_after_release')}）",
      _repC.get("owner_file_existed_while_held") is True
      and _repC.get("owner_file_after_release") is False)
check(f"#11b 釋放後 is_owner() = False（{_repC.get('is_owner_after_release')}）",
      _repC.get("is_owner_after_release") is False)

# ---- (c) 多行程同時競爭：恰好 1 個成功 ----
R_D = child_root("d")
_procs = [run_child("contend", R_D, 3.0, wait=False) for _ in range(6)]
_reps = []
for p in _procs:
    _reps.append(parse_child(p.stdout.read().decode("utf-8", "replace")))
    p.wait()
_wins = [r for r in _reps if r.get("ok") is True]
_loses = [r for r in _reps if r.get("ok") is False]
check(f"#11c 6 個行程同時競爭 → 恰好 1 個 Owner"
      f"（成功 {len(_wins)} / 失敗 {len(_loses)}）",
      len(_wins) == 1 and len(_loses) == 5)
check(f"#11c 落敗者一律 held_by_other（{sorted({r.get('why') for r in _loses})}）",
      {r.get("why") for r in _loses} == {"held_by_other"})
check(f"#11c 全部子行程算出同一個 mutex（同 root → 同 namespace）",
      len({r.get("mutex") for r in _reps}) == 1
      and _reps[0].get("mutex") != PROD_MUTEX)
check("#11c 競爭結束後 owner 檔已移除（無殘留）",
      not os.path.exists(os.path.join(R_D, CFG.MONITOR_OWNER_FILE)))

# ---- (d) 崩潰（os._exit 持有中中止）：唯一 handle 持有者 → 物件消滅 ----
R_E = child_root("e")
_repE, _rcE = run_child("crash_hold", R_E)
check(f"#11d 崩潰前子行程確實取得所有權（ok={_repE.get('ok')}）",
      _repE.get("ok") is True)
_repF, _ = run_child("acquire", R_E)
check(f"#11d 唯一 handle 持有者崩潰 → 下一個行程得到 "
      f"({_repF.get('ok')}, {_repF.get('why')!r})、abandoned={_repF.get('abandoned')}"
      f" —— 符合 Phase 4.4/4.7 契約（物件消滅，非 abandoned_taken）",
      _repF.get("ok") is True and _repF.get("why") == "acquired"
      and _repF.get("abandoned") is False)

# ---- (e) WAIT_ABANDONED：父行程持有 handle（不取得）時 owner 崩潰 ----
_k = RM._k32()
R_G = child_root("g")
_BAK_ROOT_G = RM._report_output_root
RM._report_output_root = lambda: R_G
_MX_G = RM._mutex_name()
check(f"#11e 情境 root 的 mutex ≠ 正式 canonical（…{_MX_G[-16:]}）",
      _MX_G != PROD_MUTEX)
_h = _k.CreateMutexExW(None, _MX_G, 0, RM.MUTEX_MIN_ACCESS)   # 只開 handle，不取得
check("#11e 父行程以最小權限開啟 handle（不取得所有權）", bool(_h))
_repH, _ = run_child("crash_hold", R_G)
check(f"#11e 子行程取得後崩潰（ok={_repH.get('ok')}）", _repH.get("ok") is True)
_rc = _k.WaitForSingleObject(_h, 5000)
check(f"#11e 因仍有 handle 存活，物件未消滅 → Wait 回 WAIT_ABANDONED"
      f"（rc=0x{int(_rc):X}，WAIT_ABANDONED=0x{RM.WAIT_ABANDONED:X}）",
      int(_rc) == RM.WAIT_ABANDONED)
if int(_rc) in (RM.WAIT_OBJECT_0, RM.WAIT_ABANDONED):
    _k.ReleaseMutex(_h)
_k.CloseHandle(_h)
RM._report_output_root = _BAK_ROOT_G
check("#11e 測試後 _report_output_root 已還原為 temp（非正式）",
      RM._report_output_root() == TMP)

# ---- (f) 正式 Ownership 零接觸 ----
_owner_now = None
if os.path.exists(PROD_OWNER):
    _st = os.stat(PROD_OWNER)
    _owner_now = (_st.st_size, _st.st_mtime_ns,
                  hashlib.sha256(open(PROD_OWNER, "rb").read()).hexdigest())
check(f"#11f 正式 monitor_owner.json 零修改（size/mtime/sha256 全同）",
      _owner_now == _PROD_OWNER_FP)
check("#11f 本行程結束時未持有任何 Ownership", RM.is_owner() is False)
# 正常釋放的情境必須清除 owner 檔；**崩潰情境反而必須殘留** ——
# os._exit 跳過收尾，owner 檔留在原地正是崩潰的證據（Phase 4.7 同一契約）。
_norm_left = [d for d in os.listdir(TMP)
              if d.startswith("p53_own_") and not d.startswith("p53_own_g_")
              and os.path.exists(os.path.join(TMP, d, CFG.MONITOR_OWNER_FILE))]
check(f"#11f 正常釋放的情境 owner 檔皆已清除（殘留 {_norm_left or '無'}）",
      not _norm_left)
_crash_left = [d for d in os.listdir(TMP)
               if d.startswith("p53_own_g_")
               and os.path.exists(os.path.join(TMP, d, CFG.MONITOR_OWNER_FILE))]
check(f"#11f 崩潰情境（os._exit）owner 檔殘留於 temp root —— 崩潰證據"
      f"（{_crash_left}）", len(_crash_left) == 1)
check("#11f 殘留的 owner 檔位於 temp root，不在正式 root",
      all(_norm(os.path.join(TMP, d)).startswith(_norm(TMP))
          for d in _crash_left))


# ======================================================================
# Batch 8 —— #12 canonicalization / malformed / timeout
# ======================================================================
print("\n[#12] 路徑 canonicalization + 異常資料（fail closed，不 crash）")

_BASE12 = child_root("canon")
_variants = [
    ("原樣", _BASE12),
    ("全小寫磁碟機", _BASE12[0].lower() + _BASE12[1:]),
    ("全大寫磁碟機", _BASE12[0].upper() + _BASE12[1:]),
    ("正斜線", _BASE12.replace("\\", "/")),
    ("尾端加分隔符", _BASE12 + "\\"),
    ("夾一段 .", os.path.join(_BASE12, ".")),
    ("繞一圈 ..", os.path.join(_BASE12, "sub", "..")),
]
_names = {}
for label, v in _variants:
    RM._report_output_root = lambda v=v: v
    _names[label] = RM._mutex_name()
RM._report_output_root = lambda: TMP
check(f"#12 {len(_variants)} 種路徑表示法（大小寫／slash／. ／..／尾綴）"
      f"皆收斂為同一個 Mutex（{len(set(_names.values()))} 個不同）",
      len(set(_names.values())) == 1)
for label in ("全小寫磁碟機", "全大寫磁碟機", "正斜線"):
    check(f"#12 「{label}」與原樣同名（normcase + abspath 收斂）",
          _names[label] == _names["原樣"])
check("#12 收斂後的名稱仍 ≠ 正式 canonical",
      _names["原樣"] != PROD_MUTEX)

_diff_roots = [child_root(f"diff{i}") for i in range(4)]
_dn = []
for r in _diff_roots:
    RM._report_output_root = lambda r=r: r
    _dn.append(RM._mutex_name())
RM._report_output_root = lambda: TMP
check(f"#12 4 個不同 temp root → 4 個不同 Mutex（{len(set(_dn))} 個）",
      len(set(_dn)) == 4 and PROD_MUTEX not in _dn)
check("#12 Mutex 一律位於 Global\\ 命名空間（跨 session 必要條件）",
      all(n.startswith("Global\\") for n in _dn))

# ---- 異常輸入：一律 fail closed、不得拋例外 ----
print("       —— 異常輸入（None / malformed / timeout / 缺欄位）")


def safe(fn, *a, **k):
    """回傳 (值, 例外型別)；例外型別非 None 即代表產品未 fail closed。"""
    try:
        return fn(*a, **k), None
    except Exception as e:                            # noqa: BLE001
        return None, type(e).__name__


# 契約外（傳入非路徑型別）：產品路徑不可能供給這種輸入 ——
#   report_resume_on_launch() 只以 find_active_session() 的回傳值呼叫，
#   且前面有 `if not active: return`。此處僅**記錄實際行為**，
#   要求「不得靜默回傳錯誤數值」（回 None 或明確拋型別錯皆可接受）。
for label, arg in (("None", None), ("整數", 123), ("list", []), ("dict", {})):
    v, exc = safe(RM._orphan_gap_seconds, arg)
    check(f"#12 [契約外] _orphan_gap_seconds({label}) → 回 {v!r} / 例外 {exc}"
          f"；不得靜默回傳錯誤數值",
          v is None and exc in (None, "TypeError", "AttributeError"))
# 契約內（有效路徑字串）：任何內容都必須安全回 None
v, exc = safe(RM._orphan_gap_seconds, "")
check(f"#12 [契約內] _orphan_gap_seconds(空字串路徑) → {v!r}，未拋例外（{exc}）",
      exc is None and v is None)

_bad_dirs = {}
for label, content in (("壞 JSON", "{not json"),
                       ("空檔", ""),
                       ("JSON 但非物件", "[1,2,3]"),
                       ("null", "null"),
                       ("缺 last_sample_time", '{"status":"recording"}'),
                       ("時間格式錯", '{"status":"recording",'
                                     '"last_sample_time":"31/12/2026"}'),
                       ("時間為數字", '{"status":"recording",'
                                     '"last_sample_time":12345}'),
                       ("時間為 null", '{"status":"recording",'
                                      '"last_sample_time":null}')):
    d = tempfile.mkdtemp(prefix="p53_bad_", dir=TMP)
    with io.open(os.path.join(d, CFG.FILE_SESSION_STATE), "w",
                 encoding="utf-8") as fh:
        fh.write(content)
    _bad_dirs[label] = d
    v, exc = safe(RM._orphan_gap_seconds, d)
    if label == "JSON 但非物件":
        # ⚠️ 已知硬化機會（非可達缺陷）：state 為 JSON 陣列時 `st.get` 拋
        #    AttributeError。產品路徑不可達 —— find_active_session() 以
        #    isinstance(st, dict) 過濾，陣列型 state 永遠不會被選為 active，
        #    而本函式只以 active 被呼叫。此處鎖定**現況**並在報告中列為發現。
        check(f"#12 [已知硬化機會] _orphan_gap_seconds（{label}）→ 例外 {exc}"
              f"（產品路徑不可達，鎖定現況）", exc == "AttributeError")
    else:
        check(f"#12 [契約內] _orphan_gap_seconds（{label}）→ {v!r}，"
              f"未拋例外（{exc}）", exc is None and v is None)

for label, arg in (("None", None), ("字串", "x"), ("int", 1),
                   ("list", []), ("無 _fail", {"communication_ok": True}),
                   ("_fail=None", {"_fail": None}),
                   ("_fail 含 None 元素", {"_fail": [None, 5]})):
    (res, exc) = safe(RM._auth_session_lost, arg)
    ok12 = exc is None and isinstance(res, tuple) and res[0] is False
    check(f"#12 [契約內] _auth_session_lost（{label}）→ {res}，"
          f"未拋例外（{exc}）", ok12)
# ⚠️ 已知硬化機會（非可達缺陷）：_fail 為非序列時 _ep_failed 迭代它會拋 TypeError。
#    _fail 一律由 CDR.read_all 建構為 list，不會是純量。鎖定現況。
_res, _exc = safe(RM._auth_session_lost, {"_fail": 123})
check(f"#12 [已知硬化機會] _auth_session_lost（_fail 非序列）→ 例外 {_exc}"
      f"（_fail 由 read_all 保證為 list，產品路徑不可達）", _exc == "TypeError")

for label, arg in (("None", None), ("空 list", []), ("含 None", [None]),
                   ("含 int", [1, 2]), ("字串而非 list", "abc")):
    v, exc = safe(RM._ep_failed, arg, EPA[0])
    check(f"#12 _ep_failed（{label}）→ {v!r}，未拋例外（{exc}）",
          exc is None and isinstance(v, bool))

for label, arg in (("空字串", ""), ("不存在路徑", os.path.join(TMP, "nope")),
                   ("整數", 7)):
    v, exc = safe(CDR.find_active_session, arg)
    check(f"#12 [契約內] find_active_session（{label}）→ {v!r}，"
          f"未拋例外（{exc}）", exc is None and v is None)
# ⚠️ 已知硬化機會：output_root=None 時 os.path.isdir(None) 拋 TypeError。
#    產品一律以 _report_output_root() 傳入，該函式必定回字串。鎖定現況。
_v, _exc = safe(CDR.find_active_session, None)
check(f"#12 [已知硬化機會] find_active_session(None) → 例外 {_exc}"
      f"（產品一律傳 _report_output_root() 的字串，不可達）", _exc == "TypeError")
for label, d in _bad_dirs.items():
    v, exc = safe(CDR.find_active_session, os.path.dirname(d))
    check(f"#12 find_active_session 掃到（{label}）目錄時不拋例外（{exc}）",
          exc is None)

# ---- timeout：client.get 拋 Timeout → _fail 記 path:Timeout，不 crash ----
class TimeoutClient:
    """模擬所有 GET 逾時的 client（零網路，只拋例外）。"""

    class _T(Exception):
        pass

    def get(self, *_a, **_k):
        raise TimeoutError("simulated timeout")

    def login_hmi(self, *_a, **_k):
        return "offline-token"


CDR.read_all = _REAL_READ_ALL                        # 暫時放回真 read_all
try:
    _r12, _exc12 = safe(CDR.read_all, TimeoutClient())
    check(f"#12 全部 GET 逾時 → read_all 未拋例外（{_exc12}）", _exc12 is None)
    _fails12 = (_r12 or {}).get("_fail") or []
    check(f"#12 逾時被記入 _fail（{len(_fails12)} 筆，樣例 "
          f"{str(_fails12[:1])[:70]}）", len(_fails12) > 0)
    check(f"#12 逾時時 communication_ok = "
          f"{(_r12 or {}).get('communication_ok')}（不謊報可通）",
          (_r12 or {}).get("communication_ok") is False)
    _lost12, _why12 = RM._auth_session_lost(_r12 or {})
    check(f"#12 全端點逾時 → 不誤判為登入問題（{_lost12}, {_why12!r}）",
          _lost12 is False and _why12 == "guest_also_down")
    check("#12 _fail 條目不得夾帶敏感字樣（token / cookie / password / Authorization）",
          not [x for x in _fails12
               if any(w in str(x).lower()
                      for w in ("token", "cookie", "password", "authorization"))])
finally:
    CDR.read_all = fake_read_all

# ---- 前置斷言 3 的後半：#11/#12 前後正式 output 指紋必須完全一致 ----
_FP_AFTER_1112 = _fp(PROD_ROOT)
check(f"#12 前置3 正式 output 指紋於 #11/#12 前後完全一致"
      f"（{_FP_AFTER_1112[0]} 檔 {_FP_AFTER_1112[1][:16]}…）",
      _FP_AFTER_1112 == _FP_BEFORE_1112)


# ======================================================================
# Z. 收尾 —— 證明正式環境零修改
# ======================================================================
print("\n[Z] 正式環境零修改驗證")
PROD_FP_AFTER = _fp(PROD_ROOT)
check(f"Z1 正式 output 指紋未變（{PROD_FP_AFTER[0]} 檔 "
      f"{PROD_FP_AFTER[1][:16]}…）", PROD_FP_AFTER == PROD_FP_BEFORE)
check("Z2 _report_output_root 仍指向 temp（未被測試改回正式路徑）",
      RM._report_output_root() == TMP)
check("Z3 本行程未持有 Ownership", RM.is_owner() is False)
check(f"Z3b 攔截器共放行 {_ACQ['n']} 次 acquire，涉及 "
      f"{len(_ACQ['names'])} 個 mutex，且全部 ≠ 正式 canonical",
      _ACQ["n"] > 0 and PROD_MUTEX not in _ACQ["names"])
RM.acquire_ownership = _REAL_ACQUIRE
check("Z3c acquire 攔截器已還原", RM.acquire_ownership is _REAL_ACQUIRE)

RM._report_output_root = _BAK_ROOT
CDR.read_all = _REAL_READ_ALL
CDR._fetch_cell_packs = _REAL_FETCH
check("Z4 產品函式已還原（read_all / _fetch_cell_packs / _report_output_root）",
      CDR.read_all is _REAL_READ_ALL
      and CDR._fetch_cell_packs is _REAL_FETCH
      and RM._report_output_root is _BAK_ROOT)
shutil.rmtree(TMP, ignore_errors=True)
check("Z5 temp root 已清除", not os.path.isdir(TMP))

ok = all(RESULTS)
print(f"\n== Phase 5.3-A Stress / Boundary {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
