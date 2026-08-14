# -*- coding: utf-8 -*-
"""
Dashboard 主迴圈 harness（device_control_menu.main）
======================================================================
用途
    驗證前景 Dashboard 迴圈的接線正確。Phase 4.5 起 main() 依 Monitor Ownership
    分岔，因此本檔拆成兩個**確定性**情境：

      Owner case    取得 Ownership       → report_resume_on_launch 恰 1 次
      Observer case 取不到（held_by_other）→ report_resume_on_launch 恰 0 次
                                            並顯示唯讀觀察模式提示

    兩者共同驗證：每輪只取樣一次、輸入 0 可離開、離開後不再取樣、
    全程不建立任何 ReportSession。

⚠️ 確定性（Phase 4.6-B 修正）
    Ownership 一律由測試自己注入 —— **不碰真實 Global Mutex**。
    舊版直接呼叫真實 acquire_ownership，結果取決於「ESSAutoMonitor 是否正在執行」：
        服務未跑 → Dashboard 取得 Ownership → resume 被呼叫 → PASS
        服務在跑 → 取不到                   → 不呼叫 resume → 同一份測試卻 FAIL
    這是測試的環境依賴，不是產品缺陷。改為注入後，
    **服務 Running 或 Not Running 皆得到相同結果**（最後一節即為此提供證據）。

是否需要設備
    **不需要**。_report_client / _read_device_state / refresh_status /
    report_resume_on_launch / acquire_ownership / ownership_state / owner_info
    全部以假物件注入；_report_output_root 指向一個空的臨時資料夾，
    使 Observer 的唯讀 Session 顯示也不依賴正式 output 內容。

安全性
    不含帳號密碼；不送控制命令；不建立/修改/刪除任何 Session；不修改 config；不執行 Git。
    _report_start_session 另設哨兵：即使迴圈邏輯有誤也不可能建立報告資料夾。

用法
    python test_dashboard_loop.py          # exit 0 = PASS
"""
import io
import os
import sys
import tempfile
import contextlib

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import device_control_menu as M          # noqa: E402
import report_monitor as RM              # noqa: E402  Phase 4.2：Monitor Core 已抽離

RESULTS = []

# Observer 的唯讀 Session 顯示改讀這個空目錄 → 與正式 output 內容無關
EMPTY_ROOT = os.path.join(tempfile.gettempdir(), "ess_dashboard_harness_empty_root")


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


class _FakeClient:
    def get(self, _path, **_kw):
        return []


def _fake_read(_c):
    return {
        "communication_ok": True, "pcs_charging_flag": False, "pcs_discharging_flag": False,
        "actual_active_power_kw": 0.0, "pcs_fault_flag": False, "pcs_status": "執行 / 併網",
        "pcs_control_mode_code": "manual", "pcs_control_mode": "手動模式",
        "pcs_schedule_enabled": False, "pcs_power_control_mode": "交流有功",
        "soc_percent": 77.0,
    }


# Ownership 的三個唯讀查詢介面，依情境給出一致的假狀態。
# 產品碼的三態判定來源是 is_owner() / ownership_state()['reason']，
# owner_info() 只用於「誰持有」的描述文字 —— 這裡一併給齊，避免顯示降級。
_OWNER_STATE = {"owned": True, "role": "dashboard", "reason": "acquired",
                "abandoned": False, "pid": os.getpid()}
_OBSERVER_STATE = {"owned": False, "role": "dashboard", "reason": "held_by_other",
                   "abandoned": False, "pid": None}
_OTHER_OWNER_INFO = {"pid": 6789, "role": "service", "started_at": "14:04:17"}


def run_case(name, owned, reason):
    """
    以**注入**的 Ownership 結果跑一次完整 main() 迴圈。

    owned / reason 直接決定 acquire_ownership 的回傳，且 is_owner /
    ownership_state / owner_info 一併對齊 —— 全程不觸碰真實 Mutex，
    因此與外部服務狀態完全無關。
    回傳 (calls, main() 的輸出文字)。
    """
    calls = {"read": 0, "scraper": 0, "resume": 0, "start_session": 0, "acquire": 0}
    answers = iter(["0"])                       # 輸入 0 → 離開；耗盡後仍回 "0"，不會卡住

    def _fake_input(*_a, **_k):
        return next(answers, "0")

    def _forbidden_start(*_a, **_k):
        calls["start_session"] += 1
        raise AssertionError("harness 不得建立 ReportSession")

    def _fake_acquire(role="unknown"):
        calls["acquire"] += 1
        return owned, reason

    _state = dict(_OWNER_STATE if owned else _OBSERVER_STATE)
    _state["reason"] = reason

    bak = (M.refresh_status, RM._report_client, RM._read_device_state,
           RM.report_resume_on_launch, RM._report_start_session,
           M._prompt_with_timeout, M._menu_input,
           RM.acquire_ownership, RM.is_owner, RM.ownership_state, RM.owner_info,
           RM._report_output_root)
    M.refresh_status = lambda: (calls.__setitem__("scraper", calls["scraper"] + 1)
                                or {"PCS": {"PCS當前狀態": "執行 / 併網"}})
    RM._report_client = lambda *a, **k: _FakeClient()
    RM._read_device_state = lambda c: (calls.__setitem__("read", calls["read"] + 1)
                                       or _fake_read(c))
    RM.report_resume_on_launch = lambda: calls.__setitem__("resume", calls["resume"] + 1)
    RM._report_start_session = _forbidden_start
    M._prompt_with_timeout = lambda *a, **k: _fake_input()
    M._menu_input = lambda *a, **k: _fake_input()
    RM.acquire_ownership = _fake_acquire
    RM.is_owner = lambda: owned
    RM.ownership_state = lambda: dict(_state)
    RM.owner_info = lambda: (None if owned else dict(_OTHER_OWNER_INFO))
    RM._report_output_root = lambda: EMPTY_ROOT
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            M.main()
    except SystemExit:
        pass
    except Exception as e:                          # noqa: BLE001
        check(f"[{name}] 主迴圈發生未預期例外：{type(e).__name__}: {e}", False)
    finally:
        (M.refresh_status, RM._report_client, RM._read_device_state,
         RM.report_resume_on_launch, RM._report_start_session,
         M._prompt_with_timeout, M._menu_input,
         RM.acquire_ownership, RM.is_owner, RM.ownership_state, RM.owner_info,
         RM._report_output_root) = bak
    return calls, buf.getvalue()


# 跑任何情境之前先拍下真實 Ownership 狀態 —— 用於最後證明「全程未動過真實 Mutex」
_OWN_BEFORE = RM.ownership_state()

# ======================================================================
# Owner case：取得 Ownership
# ======================================================================
print("[Dashboard harness] Owner case（注入：取得 Ownership）")
c_own, out_own = run_case("Owner", True, "acquired")
check(f"acquire_ownership 恰呼叫 1 次（{c_own['acquire']}）—— 啟動時只嘗試一次",
      c_own["acquire"] == 1)
check(f"取得 Ownership → report_resume_on_launch 恰 1 次（{c_own['resume']}）",
      c_own["resume"] == 1)
check(f"read_all 恰 1 次（{c_own['read']}）—— 離開迴圈後不再取樣", c_own["read"] == 1)
check(f"scraper 恰 1 次（{c_own['scraper']}）—— 每輪只刷新一次", c_own["scraper"] == 1)
check(f"全程未建立任何 ReportSession（{c_own['start_session']}）",
      c_own["start_session"] == 0)
check("Owner 顯示「本視窗…負責 Auto Report」",
      "本視窗" in out_own and "負責 Auto Report" in out_own)
check("Owner **不**出現 Observer 提示",
      "[OBSERVER]" not in out_own and "為 Observer" not in out_own)

# ======================================================================
# Observer case：held_by_other
# ======================================================================
print("\n[Dashboard harness] Observer case（注入：held_by_other）")
c_obs, out_obs = run_case("Observer", False, "held_by_other")
check(f"acquire_ownership 恰呼叫 1 次（{c_obs['acquire']}）", c_obs["acquire"] == 1)
check(f"取不到 Ownership → report_resume_on_launch **恰 0 次**（{c_obs['resume']}）",
      c_obs["resume"] == 0)
check(f"read_all 恰 1 次（{c_obs['read']}）—— 唯讀設備狀態仍正常更新",
      c_obs["read"] == 1)
check(f"scraper 恰 1 次（{c_obs['scraper']}）", c_obs["scraper"] == 1)
check(f"全程未建立任何 ReportSession（{c_obs['start_session']}）",
      c_obs["start_session"] == 0)
check("Observer 啟動時即印出唯讀觀察模式提示（[OBSERVER] + held_by_other）",
      "[OBSERVER]" in out_obs and "held_by_other" in out_obs
      and "唯讀觀察模式" in out_obs)
check("Observer 明示不會續接／建立／收尾報告",
      "不會續接、建立或收尾任何報告" in out_obs)
check("Ownership 狀態行顯示 Owner 為「背景服務」且本視窗為 Observer",
      "背景服務" in out_obs and "為 Observer" in out_obs)

# ======================================================================
# 確定性：與真實 Mutex／服務狀態無關
# ======================================================================
print("\n[Dashboard harness] 確定性（不依賴 ESSAutoMonitor 是否 Running）")
_own_after = RM.ownership_state()
check(f"本行程從未真正持有 Ownership（is_owner={RM.is_owner()}）",
      RM.is_owner() is False)
check("真實 Ownership 狀態前後完全一致 → 全程未觸碰真實 Global Mutex",
      _own_after == _OWN_BEFORE)
check("測試結束已還原 acquire_ownership / is_owner / ownership_state / owner_info",
      RM.acquire_ownership.__name__ == "acquire_ownership"
      and RM.is_owner.__name__ == "is_owner"
      and RM.ownership_state.__name__ == "ownership_state"
      and RM.owner_info.__name__ == "owner_info")
check("測試結束已還原 _report_output_root（不再指向臨時目錄）",
      RM._report_output_root.__name__ == "_report_output_root")
check("兩情境 resume 次數為 1 vs 0 → 分岔確實只由 Ownership 決定",
      c_own["resume"] == 1 and c_obs["resume"] == 0)
check("兩情境唯讀取樣次數相同（1 vs 1）→ Observer 不影響設備狀態更新",
      c_own["read"] == c_obs["read"] == 1)
check("兩情境皆未建立 Session（0 vs 0）→ harness 無任何檔案系統副作用",
      c_own["start_session"] == 0 and c_obs["start_session"] == 0)
check("Observer 的唯讀 Session 顯示改讀空目錄 → 不依賴正式 output 內容",
      not os.path.isdir(EMPTY_ROOT) or not os.listdir(EMPTY_ROOT))

ok = all(RESULTS)
print(f"\n== Dashboard 迴圈 harness {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
