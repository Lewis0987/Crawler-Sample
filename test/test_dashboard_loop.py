# -*- coding: utf-8 -*-
"""
Dashboard 主迴圈 harness（device_control_menu.main）
======================================================================
用途
    驗證前景 Dashboard 迴圈的接線正確 —— Auto Start 完全依賴這條迴圈呼叫
    auto_schedule_check()，因此迴圈本身若壞掉，所有自動排程功能都會靜默失效。
    驗證：
      · 啟動時呼叫一次 report_resume_on_launch()（續接未完成 Session）
      · 每輪只取樣一次（read_all 與 scraper 各一次，不重複打 API）
      · 輸入 0 可正常離開，且離開後不再取樣

是否需要設備
    **不需要**。完全離線：_report_client / _read_device_state / refresh_status /
    report_resume_on_launch 全部以假物件注入，不連任何設備、不建立任何 Session。

安全性
    不含帳號密碼；不送控制命令；不建立/修改 Session；不修改 config；不執行 Git。
    _report_start_session 另設哨兵：即使迴圈邏輯有誤也不可能建立報告資料夾。

用法
    python test_dashboard_loop.py          # exit 0 = PASS
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import device_control_menu as M          # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


calls = {"read": 0, "scraper": 0, "resume": 0, "start_session": 0}


class _FakeClient:
    def get(self, _path, **_kw):
        return []


def _fake_read(_c):
    calls["read"] += 1
    return {
        "communication_ok": True, "pcs_charging_flag": False, "pcs_discharging_flag": False,
        "actual_active_power_kw": 0.0, "pcs_fault_flag": False, "pcs_status": "執行 / 併網",
        "pcs_control_mode_code": "manual", "pcs_control_mode": "手動模式",
        "pcs_schedule_enabled": False, "pcs_power_control_mode": "交流有功",
        "soc_percent": 77.0,
    }


def _forbidden_start(*_a, **_k):
    calls["start_session"] += 1
    raise AssertionError("harness 不得建立 ReportSession")


def main():
    # 依序回應主迴圈的輸入要求；耗盡後回 "0"（離開），避免任何情況下卡住不退出。
    _answers = iter(["0"])

    def _fake_input(*_a, **_k):
        return next(_answers, "0")

    _bak = (M.refresh_status, M._report_client, M._read_device_state,
            M.report_resume_on_launch, M._report_start_session,
            M._prompt_with_timeout, M._menu_input)
    M.refresh_status = lambda: (calls.__setitem__("scraper", calls["scraper"] + 1)
                                or {"PCS": {"PCS當前狀態": "執行 / 併網"}})
    M._report_client = lambda *a, **k: _FakeClient()
    M._read_device_state = _fake_read
    M.report_resume_on_launch = lambda: calls.__setitem__("resume", calls["resume"] + 1)
    M._report_start_session = _forbidden_start
    M._prompt_with_timeout = lambda *a, **k: _fake_input()
    M._menu_input = lambda *a, **k: _fake_input()
    try:
        M.main()
    except SystemExit:
        pass
    except Exception as e:                          # noqa: BLE001
        check(f"主迴圈發生未預期例外：{type(e).__name__}: {e}", False)
    finally:
        (M.refresh_status, M._report_client, M._read_device_state,
         M.report_resume_on_launch, M._report_start_session,
         M._prompt_with_timeout, M._menu_input) = _bak

    print()
    check(f"啟動時呼叫 report_resume_on_launch 恰 1 次（{calls['resume']}）",
          calls["resume"] == 1)
    check(f"read_all 恰 1 次（{calls['read']}）—— 離開後不再取樣", calls["read"] == 1)
    check(f"scraper 恰 1 次（{calls['scraper']}）—— 每輪只刷新一次", calls["scraper"] == 1)
    check(f"全程未建立任何 ReportSession（{calls['start_session']}）",
          calls["start_session"] == 0)
    check("結束後已還原所有注入", M._read_device_state is _bak[2]
          and M.refresh_status is _bak[0])

    ok = all(RESULTS)
    print(f"\n== Dashboard 迴圈 harness {'PASS' if ok else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
