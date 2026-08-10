# -*- coding: utf-8 -*-
"""
Phase 3.5 Schedule Context —— 實機唯讀探針
======================================================================
用途
    對**真實設備**驗證 Phase 3.5 的排程窗口判定：
      · fetch_enabled_plans() 能否正確解析啟用中的排程模板與項目
      · planType=1（每週）／planType=2（指定日期 MM-DD）是否判別正確
      · compute_schedule_ctx() 對真實排程算出的 in_window / next_plan_in_sec
      · Dashboard 會顯示的 window= 字串
    並印出下一段排程的起始時間，供安排實機驗證時段。

是否需要設備
    **需要**。必須能登入 HMI。因此本檔**不納入**離線 Regression 的預設項目
    （run_phase3_regression.py 需加 --with-device 才會執行）。
    設備離線／登入失敗不應被視為 Phase 3 Regression 失敗。

安全性（結構上不可能產生副作用）
    · **完全不呼叫決策層**：不呼叫 auto_schedule_check()、不呼叫
      _report_start_session() —— 兩者連同 ReportSession.start() 都被換成
      會立即中止行程的哨兵，即使日後誤改也不可能建立 Session。
    · 只呼叫既有唯讀 GET：read_all() / fetch_enabled_plans()
    · 不送任何控制命令、不修改排程、不呼叫 report_stop()
    · 不修改 config、不修改任何 Session 檔案、不執行 Git
    · 不輸出帳號 / 密碼 / Token / Authorization header（登入訊息一律過濾）

用法
    python probe_schedule_context.py        # exit 0 = 成功取得並印出 ctx
"""
import io
import os
import sys
import contextlib
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import device_control_menu as M                    # noqa: E402
import charge_discharge_report as CDR              # noqa: E402
import charge_discharge_report_config as CFG       # noqa: E402


# ---------- 哨兵：任何可能建立 Session 的路徑都立刻中止 ----------
def _forbidden(*_a, **_k):
    print("\n[中止] 唯讀探針不得建立 ReportSession —— 偵測到呼叫，立即結束。")
    raise SystemExit(2)


M._report_start_session = _forbidden
M.auto_schedule_check = _forbidden
CDR.ReportSession.start = _forbidden

# 疑似憑證的字樣一律不輸出
_SECRET_HINTS = ("password", "passwd", "token", "authorization", "secret",
                 "user=", "帳號", "登入（")


def _safe(v):
    t = str(v)
    return "***" if any(h in t.lower() for h in
                        (h.lower() for h in _SECRET_HINTS)) else t


def _login_quietly():
    """登入並過濾登入訊息 —— api_client 會印出帳號，不得出現在探針輸出中。"""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            client = M._report_client()
    except Exception as e:                          # noqa: BLE001
        print(f"[FAIL] 登入時發生例外：{type(e).__name__}")
        return None
    for line in buf.getvalue().splitlines():
        if line.strip() and not any(h.lower() in line.lower() for h in _SECRET_HINTS):
            print(f"  {line.strip()}")
    return client


def _fmt_eta(sec):
    if sec is None:
        return "無（未來 8 天內無排程）"
    d = timedelta(seconds=int(sec))
    return f"{int(sec)}s（約 {d}，{datetime.now() + d:%Y-%m-%d %H:%M:%S}）"


_WD = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "日"}


def _when_text(p):
    """plan_type 為數字：1=每週（executeTime 為 isoweekday 逗號字串）、2=按日期（MM-DD）。"""
    if p["plan_type"] == 1:
        return "每週 " + "、".join(_WD.get(x, x) for x in sorted(p["days"]))
    if p["plan_type"] == 2:
        return f"指定日期 {p['date']}（MM-DD）"
    return f"未知 plan_type={p['plan_type']!r}"


def main():
    print("=" * 68)
    print(f"Phase 3.5 Schedule Context 實機唯讀探針    {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 68)

    client = _login_quietly()
    if client is None:
        print("[SKIP] 無法登入設備 —— 本探針需要設備，請確認 HMI 可達。")
        print("       （設備不可達不代表 Phase 3 軟體 Regression 失敗）")
        return 3                                    # 3 = 需設備但不可用（非 FAIL）
    print("[OK] 已登入（僅供唯讀查詢）")

    # ---------- 1. 目前設備狀態 ----------
    r = M._read_device_state(client)
    if not isinstance(r, dict):
        print("[SKIP] 讀取設備狀態失敗。")
        return 3
    print("\n--- 目前設備狀態（唯讀）---")
    for k in ("pcs_control_mode", "pcs_control_mode_code", "pcs_schedule_enabled",
              "pcs_status", "battery_status", "pcs_running_flag", "pcs_standby_flag",
              "pcs_charging_flag", "pcs_discharging_flag", "pcs_fault_flag",
              "soc_percent", "actual_active_power_kw", "communication_ok"):
        print(f"  {k:<26} = {_safe(r.get(k))}")
    d, src = CDR.resolve_direction(r.get("pcs_charging_flag"), r.get("pcs_discharging_flag"),
                                   r.get("actual_active_power_kw"))
    print(f"  {'→ resolve_direction':<26} = {d}（來源 {src}）")
    print(f"  {'→ pcs_is_idle_state':<26} = {CDR.pcs_is_idle_state(r)}")
    print(f"  {'→ pcs_is_fault':<26} = {CDR.pcs_is_fault(r)}")
    if r.get("_fail"):
        print(f"  [注意] 部分欄位讀取失敗：{r['_fail']}")

    # ---------- 2. 已啟用排程 ----------
    plans, ok = M.fetch_enabled_plans(client, M._SCHED_TPL_LIST, M._SCHED_ITEM_LIST)
    print(f"\n--- 已啟用排程項目（enableFlag==1）--- ok={ok}  共 {len(plans)} 筆")
    today = datetime.now().date()
    for p in plans:
        print(f"  · {p['name']:<12} {p['start_txt']}~{p['end_txt']}  {p['cd']:<9} "
              f"{_when_text(p):<24} 今日適用={M._plan_applies_on(p, today)}")
    if not plans:
        print("  （無啟用排程）→ 需先於 HMI 啟用一段排程才能安排實機驗證時段。")

    # ---------- 3. Schedule Context ----------
    now = datetime.now()
    ctx = (M.compute_schedule_ctx(plans, now) if ok else
           {"in_window": False, "current_plan": None,
            "next_plan_in_sec": None, "source": "unknown"})
    print("\n--- Phase 3.5 schedule_ctx（Dashboard 的 window= 內容）---")
    print(f"  in_window        = {ctx.get('in_window')}")
    print(f"  current_plan     = {ctx.get('current_plan')}")
    print(f"  next_plan_in_sec = {_fmt_eta(ctx.get('next_plan_in_sec'))}")
    print(f"  source           = {ctx.get('source')}")
    print(f"  Dashboard 顯示   ={M._schedule_ctx_text(ctx)}")

    # ---------- 4. 實測時段建議 ----------
    print("\n--- 實機驗證時段 ---")
    print(f"  AUTO_HOLD_START_SEC={CFG.AUTO_HOLD_START_SEC}s  "
          f"AUTO_HOLD_STOP_SEC={CFG.AUTO_HOLD_STOP_SEC}s  "
          f"AUTO_COOLDOWN_SEC={CFG.AUTO_COOLDOWN_SEC}s  "
          f"Dashboard 刷新={M.AUTO_REFRESH_SEC}s")
    if r.get("pcs_control_mode_code") != "smart" or not r.get("pcs_schedule_enabled"):
        print("  ⚠ 目前非「智慧模式 + 排程主開關開啟」→ Auto Start 條件 A/B 不成立，")
        print("    排程時間到了也不會自動建立報告。需先於 HMI 切換（屬控制操作，本探針不執行）。")
    if ctx.get("in_window"):
        print(f"  ▶ 目前**在**排程窗口內：{ctx.get('current_plan')}")
        print("    → 可立即觀測 window=in(...)；Start Debounce 需等下次進入窗口。")
    else:
        nxt = ctx.get("next_plan_in_sec")
        print(f"  ▶ 目前在窗口外，下一段排程：{_fmt_eta(nxt)}")
        if nxt is not None:
            t0 = now + timedelta(seconds=nxt)
            print(f"    → 請於 {t0 - timedelta(minutes=3):%H:%M} 前啟動 device_control_menu.py "
                  f"並停在數據概覽，即可觀測 Start Debounce（{t0:%H:%M:%S} 起）")

    print("\n[完成] 全程唯讀：未送出任何控制命令、未建立或修改任何 Session。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
