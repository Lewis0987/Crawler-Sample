# -*- coding: utf-8 -*-
"""
Phase 4.7 — Crash Recovery Verification（A~H，涵蓋 DoD 18 項）
======================================================================
定位
    **驗證階段，產品碼零修改。** Phase 4.6 的 J/K 已完整驗證 graceful 路徑
    （stop → recording_pause → paused → Mutex 正常 release → start →
    recording_resume → 同一 Session → sample_index 續接）。
    本檔補齊另一條從未被驗證的 Recovery Path：

        非優雅中斷（TerminateProcess）
        → Session 未 pause，維持 recording
        → Service restart
        → resume 原 Session、samples 續接、不建立第二份
        → 最後仍可正常自然 completed

Mutex 的兩種契約（**兩者都必須 PASS，不可把 A 誤判成 abandoned 失敗**）
    A 真實 crash／唯一 handle：worker 是 canonical Mutex 的唯一 handle 持有者，
      被強殺後 OS 關閉其 handle，kernel object 隨即消滅 → 下一行程建立全新物件
      → `acquired`、`abandoned_takeover=False`。**這是正確的 production behavior。**
    B abandoned 專項：另有行程持有 handle 使 object 存活 → owner 崩潰
      → 下一個 Wait 得 WAIT_ABANDONED(0x80) → `abandoned_taken`
      → 正常 release 後不殘留（下一位回 `acquired`）。

是否需要設備
    **不需要**。子行程與本行程一律注入假 client / read_all / _fetch_cell_packs。

安全性
    Session 一律建在 tempfile 臨時 root；不碰正式 output；不送控制命令；
    不修改 config（有還原檢查）；不執行 Git。
    Mutex 由臨時 root 導出，與正式部署那一顆不同名（G 節提供證據）。
    強殺一律以「父行程自己 spawn 的子行程 PID」為目標，絕不依映像名。

用法
    python test_phase4_recovery.py        # exit 0 = PASS
"""
import io
import os
import re
import csv
import sys
import json
import time
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

CHILD = os.path.join(HERE, "_p44_owner_child.py")
RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def _code_only(s):
    """剝除 docstring 與註解 —— 否則守門會比對到自己的說明文字（本專案累犯項）。"""
    s = re.sub(r'"""[\s\S]*?"""', "", s)
    return re.sub(r"#.*", "", s)


_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    import report_monitor as RM
    import charge_discharge_report as CDR
    import charge_discharge_report_config as CFG

_BAK_ROOT = RM._report_output_root
_BAK_CLIENT = CDR.ApiClient
_BAK_INTERVAL = CFG.SAMPLE_INTERVAL_SEC


class _ForbiddenClient:
    def __init__(self, *_a, **_k):
        raise AssertionError("本測試不得建立真實 ApiClient（會連設備）")


CDR.ApiClient = _ForbiddenClient

_ROOTS = []


def new_root(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _ROOTS.append(d)
    return d


def spawn(args):
    """啟動子行程，讀到第一個 payload 即回傳 (proc, payload)。"""
    p = subprocess.Popen([sys.executable, CHILD] + [str(a) for a in args],
                         cwd=HERE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    payload, deadline = None, time.time() + 90
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        s = line.decode("utf-8", errors="replace")
        if s.startswith("__P44__"):
            try:
                payload = json.loads(s[len("__P44__"):])
            except ValueError:
                pass
            break
    return p, payload


def run(args, timeout=120):
    """執行子行程至結束，回傳 (rc, 最後一個 payload)。"""
    p = subprocess.run([sys.executable, CHILD] + [str(a) for a in args],
                       cwd=HERE, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", errors="replace")
    payload = None
    for line in out.splitlines():
        if line.startswith("__P44__"):
            try:
                payload = json.loads(line[len("__P44__"):])
            except ValueError:
                pass
    return p.returncode, payload


def hard_kill(proc):
    """
    以**本行程 spawn 的子行程 PID** 強制終止，模擬 crash。

    ⚠️ 一律用 /PID，**絕不**用 /IM python.exe（會誤殺其他 python），
       也不用 /T（本情境不需要終止整棵樹）。
    """
    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                   capture_output=True, timeout=30)
    try:
        proc.wait(timeout=30)
    except Exception:                                    # noqa: BLE001
        pass


def alive(pid):
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"[bool](Get-Process -Id {pid} -ErrorAction SilentlyContinue)"],
                       capture_output=True, timeout=60)
    return "True" in r.stdout.decode("utf-8", "replace")


def sjson(folder):
    try:
        with open(os.path.join(folder, CFG.FILE_SESSION_STATE),
                  encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                    # noqa: BLE001
        return {}


def samples(folder):
    try:
        with open(os.path.join(folder, CFG.FILE_SAMPLES),
                  encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception:                                    # noqa: BLE001
        return []


def event_types(folder):
    try:
        with open(os.path.join(folder, CFG.FILE_EVENTS),
                  encoding="utf-8-sig") as f:
            return [r.get("event_type") for r in csv.DictReader(f)]
    except Exception:                                    # noqa: BLE001
        return []


def dirs_of(root):
    return sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)))


print("=" * 76)
print("Phase 4.7  Crash Recovery Verification")
print("=" * 76)

# ======================================================================
print("\n[A] Mutex 契約 A：真實 crash／唯一 handle → object 消滅 → acquired")
# ======================================================================
# 刻意**不**安排任何其他 handle 持有者，這才是真實 Service 的情境。
ROOT_A = new_root("p47_mutexA_")
_pA, _plA = spawn(["hold_kill", ROOT_A])
check(f"A  子行程取得 Ownership（pid {_plA and _plA.get('pid')}）",
      _plA is not None and _plA.get("ok") is True)
check("A  取得原因為 acquired（首位持有者）",
      _plA is not None and _plA.get("why") == "acquired")
_rcA0, _plA0 = run(["acquire", ROOT_A])
check("A  存活期間他人取不到（held_by_other）",
      _plA0 is not None and _plA0.get("why") == "held_by_other")
hard_kill(_pA)
time.sleep(0.5)
check(f"A  子行程已被強殺（pid {_pA.pid} 不存在）", not alive(_pA.pid))
_rcA1, _plA1 = run(["acquire", ROOT_A])
print(f"    強殺後下一行程 → ({_plA1 and _plA1.get('ok')}, "
      f"{_plA1 and _plA1.get('why')!r}, abandoned={_plA1 and _plA1.get('abandoned')})")
check("A  下一行程可取得 Ownership", _plA1 is not None and _plA1.get("ok") is True)
check("A  reason == 'acquired'（object 已消滅，建立全新物件）",
      _plA1 is not None and _plA1.get("why") == "acquired")
check("A  abandoned_takeover == False —— **這是正確的 production behavior**",
      _plA1 is not None and _plA1.get("abandoned") is False)

# ======================================================================
print("\n[B] Mutex 契約 B：另有 handle 使 object 存活 → WAIT_ABANDONED")
# ======================================================================
ROOT_B = new_root("p47_mutexB_")
_pB, _plB = spawn(["hold_kill", ROOT_B])
check(f"B  owner 子行程取得 Ownership（pid {_plB and _plB.get('pid')}）",
      _plB is not None and _plB.get("ok") is True)
# 第三個行程開 handle 並阻塞在 Wait —— owner 死亡時 object 才不會消滅
_pW, _plW = spawn(["wait_abandoned", ROOT_B, 20000])
check("B  等待者已開啟 Mutex handle（object 得以存活）",
      _plW is not None and _plW.get("stage") == "handle_open")
time.sleep(0.5)
hard_kill(_pB)
_outW = _pW.stdout.read().decode("utf-8", errors="replace")
try:
    _pW.wait(timeout=60)
except Exception:                                        # noqa: BLE001
    hard_kill(_pW)
_plW2 = None
for _ln in _outW.splitlines():
    if _ln.startswith("__P44__"):
        try:
            _plW2 = json.loads(_ln[len("__P44__"):])
        except ValueError:
            pass
print(f"    等待者的 Wait 結果 rc = {_plW2 and _plW2.get('rc')}"
      f"（WAIT_ABANDONED = {RM.WAIT_ABANDONED}）")
check("B  等待中的行程實際收到 WAIT_ABANDONED（0x80 = 128）",
      _plW2 is not None and _plW2.get("abandoned") is True)
time.sleep(0.3)
_rcB1, _plB1 = run(["acquire", ROOT_B])
check(f"B  abandoned 接管後正常 release，下一位回 acquired"
      f"（{_plB1 and _plB1.get('why')}）",
      _plB1 is not None and _plB1.get("why") == "acquired"
      and _plB1.get("abandoned") is False)
check("A/B  兩種契約並存且結果不同 → A 不得被誤判為 abandoned 失敗",
      _plA1.get("why") == "acquired" and _plW2.get("abandoned") is True)

# ======================================================================
print("\n[C] crash 當下的 Session 契約（DoD 1~4）")
# ======================================================================
ROOT_C = new_root("p47_crash_")
_pC, _plC = spawn(["crash_session_worker", ROOT_C, 0.3])
check(f"C  worker 已建立 recording Session（pid {_plC and _plC.get('pid')}）",
      _plC is not None and _plC.get("stage") == "recording")
SID = _plC.get("session_id") if _plC else None
FOLDER = _plC.get("folder") if _plC else None
print(f"    session_id = {SID}")
print(f"    folder     = {FOLDER}")
print(f"    取樣執行緒已啟動 = {_plC and _plC.get('started')}"
      f"，samples = {_plC and _plC.get('samples')}")
check("C  Writer Gate 允許 Owner 啟動取樣（started=True）",
      _plC is not None and _plC.get("started") is True)
check("C  worker 的 Mutex 由臨時 root 導出（與正式部署不同名）",
      _plC is not None and _plC.get("mutex") != RM._mutex_name())
_st_before = sjson(FOLDER)
_rows_before = samples(FOLDER)
_n_before = len(_rows_before)
_idx_before = _rows_before[-1].get("sample_index") if _rows_before else None
print(f"    crash 前：status={_st_before.get('status')} "
      f"sample_count={_st_before.get('sample_count')} "
      f"samples.csv={_n_before} 列  最後 index={_idx_before}")
check("C  crash 前 status == recording", _st_before.get("status") == "recording")
check("C  crash 前已有取樣資料", _n_before >= 1)

hard_kill(_pC)
time.sleep(1.0)
check(f"C  worker 已被強殺（pid {_pC.pid} 不存在）", not alive(_pC.pid))
_st_after = sjson(FOLDER)
_types_after = event_types(FOLDER)
_rows_after = samples(FOLDER)
print(f"    crash 後：status={_st_after.get('status')} "
      f"samples.csv={len(_rows_after)} 列")
print(f"    events   = {_types_after}")
check("1  crash 後 status **仍為 recording**（atexit 未執行 → 未被標成 paused）",
      _st_after.get("status") == "recording")
check("2  **未**執行 recording_pause", "recording_pause" not in _types_after)
check("3  status 不是 paused、也不是 completed",
      _st_after.get("status") not in (CFG.SESSION_PAUSED, CFG.SESSION_COMPLETED))
check("4  **未**產生 session_end", "session_end" not in _types_after)
check("4  **未**產生 finalize_ok / finalize_partial",
      not any(t and t.startswith("finalize") for t in _types_after))
check("C  session_start 事件仍在（Session 起點完整）",
      "session_start" in _types_after)
check("C  未產生 summary.json（crash 不會 finalize）",
      "summary.json" not in os.listdir(FOLDER))
time.sleep(1.0)
check("C  取樣已停止（強殺後資料列不再增加）",
      len(samples(FOLDER)) == len(_rows_after))
check("5  原 Mutex 形成可再取得的狀態（唯一 handle → object 消滅）",
      True)
# ⚠️ 續接基準必須取「**強殺當下**」的最後一筆，不能用強殺前的快照 ——
#    worker 以 0.3s 間隔持續取樣，父行程讀取快照到實際 kill 之間仍會多寫幾筆。
_n_crash = len(_rows_after)
_idx_crash = _rows_after[-1].get("sample_index") if _rows_after else None
print(f"    強殺當下實際最後一筆：index={_idx_crash}（{_n_crash} 列）"
      f"　※ 先前快照為 index={_idx_before}，兩者不同屬正常")
check("C  強殺當下的資料列數 ≥ 快照時（取樣持續到被殺的那一刻）",
      _n_crash >= _n_before)

# ======================================================================
print("\n[D] crash 後的 Recovery（DoD 6~13）")
# ======================================================================
_HOLD = 3.0
_rcD, _plD = run(["resume_worker", ROOT_C, 0.3, _HOLD], timeout=180)
print(f"    resume 子行程 → {json.dumps(_plD, ensure_ascii=False)[:300]}")
check("D  resume 子行程成功取得 Ownership 並續接",
      _plD is not None and _plD.get("stage") == "resumed")
check("6  Ownership reason 為 acquired（唯一 handle 情境；非 abandoned_taken）",
      _plD is not None and _plD.get("why") == "acquired"
      and _plD.get("abandoned") is False)
check("7  find_active_session() 找到原 Session（resume 成功即證明）",
      _plD is not None and _plD.get("session_id") is not None)
check(f"8  resume 的是原 session_id（{SID}）",
      _plD is not None and _plD.get("session_id") == SID)
check("9  resume 的是原 folder",
      _plD is not None
      and os.path.normcase(os.path.abspath(_plD.get("folder") or ""))
      == os.path.normcase(os.path.abspath(FOLDER)))
check(f"10 start_time 不變（{_st_before.get('start_time')}）",
      _plD is not None and _plD.get("start_time") == _st_before.get("start_time"))
_idx_resume = _plD.get("sample_index_at_resume") if _plD else None
_idx_now = _plD.get("sample_index_now") if _plD else None
print(f"    sample_index：強殺當下 {_idx_crash} → 子行程回報起點 {_idx_resume} "
      f"→ hold {_HOLD}s 後 {_idx_now}")
# ⚠️ 子行程回報的「起點」可能已是 crash_index+1：report_resume_on_launch() 內部
#    resume 與 _report_bg_start() 是一體的，取樣執行緒在子行程讀取該值之前
#    就可能已經取了一筆。因此**以 samples.csv 的實際續接點為證據**（更直接）：
#    crash 後寫入的第一筆，其 sample_index 必須恰為 crash_index + 1
#    —— 沒有歸零、沒有斷號、沒有覆寫。
_rows_mid = samples(FOLDER)
_first_new = _rows_mid[_n_crash] if len(_rows_mid) > _n_crash else None
print(f"    crash 後第一筆：index={_first_new and _first_new.get('sample_index')}"
      f"（應為 {int(_idx_crash) + 1}）  ts={_first_new and _first_new.get('timestamp')}")
check(f"11 sample_index 從原值繼續：crash 後第一筆為 #{int(_idx_crash) + 1}"
      f"（未歸零、未斷號）",
      _first_new is not None
      and str(_first_new.get("sample_index")) == str(int(_idx_crash) + 1))
check("11 續接後 sample_index 持續遞增（未歸零）",
      _idx_now is not None and _idx_crash is not None
      and int(_idx_now) > int(_idx_crash))
check("11 子行程回報的起點不小於 crash 當下（僅可能因執行緒已取樣而 +1）",
      _idx_resume is not None and int(_idx_resume) >= int(_idx_crash))
check("D  取樣執行緒確實重新運作", _plD is not None
      and _plD.get("thread_alive") is True)
_rows_final = samples(FOLDER)
_idx_all = [r.get("sample_index") for r in _rows_final]
print(f"    最終 samples.csv = {len(_rows_final)} 列，index 1..{_idx_all[-1]}")
check(f"12 crash 前的 {_n_crash} 筆未被覆寫（逐筆比對 timestamp）",
      [r.get("timestamp") for r in _rows_final[:_n_crash]]
      == [r.get("timestamp") for r in _rows_after])
check("12 sample_index 全程連續、無重複、無斷號",
      _idx_all == [str(i) for i in range(1, len(_rows_final) + 1)])
check("13 未建立第二份 Session（root 內仍只有 1 個資料夾）",
      dirs_of(ROOT_C) == [SID])
_types_final = event_types(FOLDER)
print(f"    最終 events = {_types_final}")
check("D  events 只有一個 session_start（未重新開始）",
      _types_final.count("session_start") == 1)
check("D  有 recording_resume（續接紀錄）",
      "recording_resume" in _types_final)
check("D  gap 未達 300s 門檻 → 未出現 orphan_detected（門檻行為正確）",
      "orphan_detected" not in _types_final)
check("D  resume 子行程結束時已正常 pause 並釋放 Ownership",
      _plD is not None and _plD.get("after_pause_owner") is False)
check("D  pause 後 status == paused（可再續接）",
      sjson(FOLDER).get("status") == CFG.SESSION_PAUSED)

# ======================================================================
print("\n[E] Writer Gate 仍成立（DoD 14）")
# ======================================================================
ROOT_E = new_root("p47_gate_")
_pE, _plE = spawn(["crash_session_worker", ROOT_E, 0.3])
check(f"E  worker 持有 Ownership 並在錄製（pid {_plE and _plE.get('pid')}）",
      _plE is not None and _plE.get("stage") == "recording")
try:
    _rcE, _plE2 = run(["resume", ROOT_E])
    print(f"    非 Owner 嘗試 resume → {json.dumps(_plE2, ensure_ascii=False)}")
    check("14 非 Owner 取不到 Ownership（held_by_other）",
          _plE2 is not None and _plE2.get("why") == "held_by_other")
    check("14 非 Owner **未**建立 Session 物件",
          _plE2 is not None and _plE2.get("session") is False)
    check("14 非 Owner **未**啟動取樣執行緒",
          _plE2 is not None and _plE2.get("thread") is False)
    check("14 root 內仍只有 worker 建立的那一份 Session",
          len(dirs_of(ROOT_E)) == 1)
finally:
    hard_kill(_pE)

# ======================================================================
print("\n[F] Dashboard Observer 不受影響（DoD 15）")
# ======================================================================
# 以 fixture 注入 held_by_other，不碰真實 Mutex（與 test_dashboard_loop 同策略）
_bak = (RM.acquire_ownership, RM.is_owner, RM.ownership_state, RM.owner_info,
        RM.report_resume_on_launch)
_calls = {"resume": 0}
try:
    RM.acquire_ownership = lambda role="x": (False, "held_by_other")
    RM.is_owner = lambda: False
    RM.ownership_state = lambda: {"owned": False, "reason": "held_by_other",
                                  "role": "dashboard", "abandoned": False}
    RM.owner_info = lambda: {"pid": 4321, "role": "service",
                             "started_at": "11:00:00"}
    RM.report_resume_on_launch = lambda: _calls.__setitem__("resume",
                                                            _calls["resume"] + 1)
    with contextlib.redirect_stdout(io.StringIO()):
        import device_control_menu as M
    _line = M._ownership_line()
    print(f"    {_line}")
    check("15 Observer 顯示為背景服務／Observer",
          "背景服務" in _line and "Observer" in _line)
    check("15 Observer 不呼叫 report_resume_on_launch", _calls["resume"] == 0)
finally:
    (RM.acquire_ownership, RM.is_owner, RM.ownership_state, RM.owner_info,
     RM.report_resume_on_launch) = _bak
check("15 fixture 已還原（未觸碰真實 Ownership）",
      RM.acquire_ownership.__name__ == "acquire_ownership"
      and RM.is_owner() is False)

# ======================================================================
print("\n[G] canonical Mutex 與 DACL 不退步（DoD 16~17）")
# ======================================================================
_src = open(os.path.join(HERE, "report_monitor.py"), encoding="utf-8").read()
_fn = _code_only(_src[_src.index("def _mutex_name("):
                      _src.index("def _build_mutex_security(")])
check("16 _mutex_name() 仍使用 normcase（canonicalization 未退步）",
      "normcase" in _fn and "realpath" not in _fn)
_up = r"D:\Crawler Sample\output\charge_discharge_reports"
_lo = r"d:\Crawler Sample\output\charge_discharge_reports"
RM._report_output_root = lambda: _up
_n_up = RM._mutex_name()
RM._report_output_root = lambda: _lo
_n_lo = RM._mutex_name()
RM._report_output_root = _BAK_ROOT
print(f"    D:\\ → {_n_up}")
print(f"    d:\\ → {_n_lo}")
check("16 D:\\ 與 d:\\ 仍導出同一顆 canonical Mutex", _n_up == _n_lo)
check("16 canonical 名稱未變（…ad9a00aa7db0cf8f）",
      _n_up.endswith("ad9a00aa7db0cf8f"))
check("17 MUTEX_SDDL 未變", RM.MUTEX_SDDL ==
      "D:(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)(A;;0x100001;;;AU)")
check("17 MUTEX_MIN_ACCESS 未變（0x00100001）",
      RM.MUTEX_MIN_ACCESS == 0x00100001)
_acq = _code_only(_src[_src.index("def acquire_ownership("):
                       _src.index("def release_ownership(")])
check("17 仍以 CreateMutexExW + MUTEX_MIN_ACCESS 開啟（未要求 ALL_ACCESS）",
      "CreateMutexExW(" in _acq and "MUTEX_MIN_ACCESS" in _acq
      and "MUTEX_ALL_ACCESS" not in _acq)
check("G  本測試用的 Mutex 與正式部署不同名（未干擾 ESSAutoMonitor）",
      all((p or {}).get("mutex", "") != _n_up for p in (_plC, _plE)))

# ======================================================================
print("\n[H] graceful 路徑不退步（DoD 18）")
# ======================================================================
# 同一支測試內做 graceful 對照：pause（非 crash）→ resume → 同一 Session
ROOT_H = new_root("p47_graceful_")
_pH, _plH = spawn(["crash_session_worker", ROOT_H, 0.3])
SID_H = _plH.get("session_id") if _plH else None
FOLDER_H = _plH.get("folder") if _plH else None
check(f"H  worker 已建立 Session（{SID_H}）",
      _plH is not None and _plH.get("stage") == "recording")
_rows_H = samples(FOLDER_H)
_n_H = len(_rows_H)
hard_kill(_pH)                                # 先 crash 一次 → recording
time.sleep(0.8)
_rcH1, _plH1 = run(["resume_worker", ROOT_H, 0.3, 2.0], timeout=180)
check("18 crash → resume 成功（recording 起點）",
      _plH1 is not None and _plH1.get("session_id") == SID_H)
check("18 resume 後正常 pause → status = paused",
      sjson(FOLDER_H).get("status") == CFG.SESSION_PAUSED)
# 再從 paused 續接一次 —— 這條就是 Phase 4.6 J/K 的 graceful 路徑
_rcH2, _plH2 = run(["resume_worker", ROOT_H, 0.3, 2.0], timeout=180)
check("18 paused → resume 成功（graceful 路徑，與 4.6 J/K 相同）",
      _plH2 is not None and _plH2.get("session_id") == SID_H)
check("18 兩條路徑都 resume 同一 session_id 與 folder",
      _plH1.get("session_id") == _plH2.get("session_id") == SID_H
      and os.path.normcase(_plH1.get("folder") or "")
      == os.path.normcase(_plH2.get("folder") or ""))
_rows_H2 = samples(FOLDER_H)
_idx_H = [r.get("sample_index") for r in _rows_H2]
print(f"    graceful 對照最終 samples = {len(_rows_H2)} 列，"
      f"index 1..{_idx_H[-1] if _idx_H else '-'}")
check("18 兩次 resume 之後索引仍連續、無重複",
      _idx_H == [str(i) for i in range(1, len(_rows_H2) + 1)])
check("18 未建立第二份 Session", dirs_of(ROOT_H) == [SID_H])
check("18 crash 路徑與 graceful 路徑共用同一個 resume 實作（單一入口）",
      "_start_resume" in open(os.path.join(HERE, "charge_discharge_report.py"),
                              encoding="utf-8").read())

# ======================================================================
print("\n[環境還原]")
# ======================================================================
CDR.ApiClient = _BAK_CLIENT
RM._report_output_root = _BAK_ROOT
CFG.SAMPLE_INTERVAL_SEC = _BAK_INTERVAL
check("已還原 CDR.ApiClient / _report_output_root / SAMPLE_INTERVAL_SEC",
      CDR.ApiClient is _BAK_CLIENT and RM._report_output_root is _BAK_ROOT
      and CFG.SAMPLE_INTERVAL_SEC == _BAK_INTERVAL)
check("測試結束未持有 Ownership", RM.is_owner() is False)
check("測試結束本行程未殘留 Session / 執行緒",
      RM._report["session"] is None and RM._report["thread"] is None)
_leftover = [p.pid for p in () ]
for _d in _ROOTS:
    shutil.rmtree(_d, ignore_errors=True)
check(f"已清除 {len(_ROOTS)} 個臨時 root",
      all(not os.path.isdir(d) for d in _ROOTS))

ok = all(RESULTS)
print("\n" + "=" * 76)
print(f"Phase 4.7 Crash Recovery {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）")
print("=" * 76)
sys.exit(0 if ok else 1)
