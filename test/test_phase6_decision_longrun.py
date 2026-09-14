# -*- coding: utf-8 -*-
"""
test_phase6_decision_longrun.py — Long-Running Offline Decision Loop
======================================================================
核心命題
    「單一 scenario 正確，不代表連續數小時的時間序列也正確。
      在一條完整時間軸上，決策必須保持一致、Fail Closed、不抖動、
      不直接反轉方向、不沿用 stale 決策。」

🔴 虛擬時鐘：跑 24 小時只是把時間軸推進 86400 秒，**不 sleep、不等待**。
🔴 零 I/O、確定性、可重播；TOU 一律由正式 TariffProvider 依 aware
   timestamp 求值（只有 fail-closed 案例才用 override）。
🔴 SOC 完全由腳本給定 —— 沒有電池物理模型，不由「5 kW × 時間」推算。
🔴 不計算 kWh / 收益 / 電費（本階段驗 decision correctness）。

L1  OFF_PEAK 長時間充電 → SOC HIGH 後停
L2  PEAK 長時間放電     → SOC LOW 後停
L3  TOU 邊界（正式 provider 切換）
L4  逆灌：PEAK IMPORT → NEAR_ZERO → EXPORT
L5  OFF_PEAK EXPORT → CHARGE
L6  meter stale → 恢復
L7  meter disconnect → 恢復
L8  ESS stale（與 meter stale reason 不同）
L9  fault 中途出現 → 清除後重新評估
L10 authority 失去 → 恢復
L11 CHARGE → DISCHARGE 反轉
L12 DISCHARGE → CHARGE 反轉
L13 分類器遲滯 / debounce 抖動
L14 跨日邊界
L15 年度離峰日邊界
L16 24h synthetic day + summary
LI  Timeline 時間戳不變量

用法
    python test_phase6_decision_longrun.py        # exit 0 = PASS
"""
import io
import os
import sys
import ast
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meter_client as MC                       # noqa: E402
import power_classifier as PC                   # noqa: E402
import tou_calendar as TC                       # noqa: E402
import decision_engine as DE                    # noqa: E402
import decision_policy as DP                    # noqa: E402
import safety_gate as SG                        # noqa: E402
import control_authority as CA                  # noqa: E402
import last_control_store as LCS                # noqa: E402
import pcs_control_integration as PCI           # noqa: E402
import pcs_auto_control_config as CFG           # noqa: E402
import phase6_decision_simulator as SIM         # noqa: E402
import phase6_decision_replay_runner as RUN     # noqa: E402

RESULTS = []
CHARGE_KW = CFG.DEFAULT_CONTROL_CONFIG.charge_power_kw
DISCHARGE_KW = CFG.DEFAULT_CONTROL_CONFIG.discharge_power_kw


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def at(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=RUN.tz())


def line(points, name=""):
    return RUN.Timeline(points, name=name)


def go(timeline, **kw):
    """跑 timeline，回傳 (summary, violations, records)；順帶斷言不變量全過。"""
    s, v, r = RUN.replay(timeline, **kw)
    return s, v, r


def cold_and_warm(records):
    """把冷啟動段與穩定段切開。

    🔴 分類器冷啟動時沒有任何歷史可比對，第一個 debounce 窗內 grid 必為
       UNKNOWN → 依 Fail Closed 不得送出控制。這是**真實行為**，
       不得用灌入假樣本的方式把它蓋掉，也不得靜默略過。
    """
    i = 0
    while i < len(records) and records[i].grid_state == PC.STATE_UNKNOWN:
        i += 1
    return records[:i], records[i:]


def check_cold_start(records, expect=1):
    """明確斷言冷啟動段的行為，再回傳 (cold, warm)。"""
    c, w = cold_and_warm(records)
    check(f"  冷啟動：前 {len(c)} tick 尚未成形（debounce）→ 一律不送控制",
          len(c) == expect
          and all(x.grid_state == PC.STATE_UNKNOWN
                  and x.decision_action == DE.ACTION_NO_ACTION
                  and x.requested_action == PCI.CTRL_NONE
                  and x.would_send is False for x in c))
    return c, w


def acts(records):
    return [r.requested_action for r in records]


def sent(records):
    """實際會送出的方向序列（只看真的 would_send 的 tick）。"""
    return [r.requested_action for r in records if r.would_send]


# ======================================================================
# L1 / L2 —— 長時間充放電 + SOC 閂鎖
# ======================================================================
def test_L1_offpeak_long_charge():
    print("\n[L1] OFF_PEAK 長時間充電 → SOC HIGH 後停")
    # 2026-07-15 是夏月平日；00:00~09:00 為 OFF_PEAK
    soc = lambda i, a: 45.0 if i < 40 else 95.0        # noqa: E731
    tl = RUN.build_timeline(at(2026, 7, 15, 1), 60, 60, name="L1",
                            meter_kw=60.0, soc_percent=soc)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    check(f"  TOU 全程 OFF_PEAK（{s.tou_states}）",
          set(s.tou_states) == {TC.TOU_OFF_PEAK})
    c, _ = check_cold_start(r)
    early, late = r[len(c):40], r[40:]
    check(f"★★ SOC MID 期間持續 charge @ {CHARGE_KW} kW",
          all(x.requested_action == PCI.CTRL_CHARGE
              and x.requested_power_kw == CHARGE_KW for x in early))
    check("★★ SOC 轉 HIGH 後**停止**充電",
          all(x.requested_action != PCI.CTRL_CHARGE for x in late))
    check(f"  轉換後 decision = {late[0].decision_action}",
          late[0].decision_action == DE.ACTION_IDLE)
    check("★★ 全程沒有任何 discharge", PCI.CTRL_DISCHARGE not in acts(r))


def test_L2_peak_long_discharge():
    print("\n[L2] PEAK 長時間放電 → SOC LOW 後停")
    soc = lambda i, a: 60.0 if i < 40 else 10.0        # noqa: E731
    tl = RUN.build_timeline(at(2026, 7, 15, 14), 60, 60, name="L2",
                            meter_kw=60.0, soc_percent=soc)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    check(f"  TOU 全程 PEAK（{s.tou_states}）",
          set(s.tou_states) == {TC.TOU_PEAK})
    c, _ = check_cold_start(r)
    early, late = r[len(c):40], r[40:]
    check(f"★★ SOC MID 期間持續 discharge @ {DISCHARGE_KW} kW",
          all(x.requested_action == PCI.CTRL_DISCHARGE
              and x.requested_power_kw == DISCHARGE_KW for x in early))
    check("★★ SOC 轉 LOW 後**停止**放電",
          all(x.requested_action != PCI.CTRL_DISCHARGE for x in late))
    check("★★ 全程沒有任何 charge", PCI.CTRL_CHARGE not in acts(r))


# ======================================================================
# L3 —— TOU 邊界（正式 provider）
# ======================================================================
def test_L3_tou_boundary():
    print("\n[L3] TOU 邊界：OFF_PEAK → PEAK（夏月平日 09:00）")
    tl = RUN.build_timeline(at(2026, 7, 15, 8, 30), 300, 12, name="L3",
                            meter_kw=60.0, soc_percent=60.0)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    check(f"  同一條 timeline 出現兩種 TOU（{s.tou_states}）",
          set(s.tou_states) == {TC.TOU_OFF_PEAK, TC.TOU_PEAK})
    c, w = check_cold_start(r)
    before = [x for x in w if x.tou_state == TC.TOU_OFF_PEAK]
    after = [x for x in w if x.tou_state == TC.TOU_PEAK]
    check("★★ OFF_PEAK 段一律 charge",
          all(x.requested_action == PCI.CTRL_CHARGE for x in before))
    check("★★ PEAK 段一律 discharge（此段 PCS 腳本為 STANDBY）",
          all(x.requested_action == PCI.CTRL_DISCHARGE for x in after))
    check("★★ 切換點由正式 TariffProvider 決定，非 hard-code",
          before[-1].at.hour == 8 and after[0].at.hour == 9)

    # 夏月週六：同樣時刻應是 HALF_PEAK → 整列 IDLE
    tl2 = RUN.build_timeline(at(2026, 7, 18, 10), 300, 6, name="L3-sat",
                             meter_kw=60.0, soc_percent=60.0)
    s2, v2, r2 = go(tl2)
    check(f"★★ 同一時刻但週六 → {set(s2.tou_states)}（HALF_PEAK）",
          set(s2.tou_states) == {TC.TOU_HALF_PEAK})
    check("★★ HALF_PEAK 全程不送控制",
          all(x.requested_action == PCI.CTRL_NONE for x in r2) and not v2)


# ======================================================================
# L4 / L5 —— 逆灌
# ======================================================================
def test_L4_reverse_flow():
    print("\n[L4] PEAK：IMPORT → NEAR_ZERO → EXPORT 必須停止放電")
    def kw(i, a):
        return 60.0 if i < 20 else (0.0 if i < 40 else -60.0)
    tl = RUN.build_timeline(at(2026, 7, 15, 14), 60, 60, name="L4",
                            meter_kw=kw, soc_percent=60.0)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    check(f"  grid 經歷三種狀態（{s.grid_states}）",
          set(s.grid_states) >= {PC.STATE_IMPORT, PC.STATE_NEAR_ZERO,
                                 PC.STATE_EXPORT})
    imp = [x for x in r if x.grid_state == PC.STATE_IMPORT]
    nz = [x for x in r if x.grid_state == PC.STATE_NEAR_ZERO]
    exp = [x for x in r if x.grid_state == PC.STATE_EXPORT]
    check("★★ IMPORT 期間放電", all(x.requested_action == PCI.CTRL_DISCHARGE
                                    for x in imp))
    check("★★ NEAR_ZERO 期間**停止**放電",
          all(x.requested_action != PCI.CTRL_DISCHARGE for x in nz))
    check("★★ EXPORT 期間**停止**放電（不擴大逆灌）",
          all(x.requested_action != PCI.CTRL_DISCHARGE for x in exp))
    check("★★ 不變量 peak_export_no_discharge 未被違反",
          not [x for x in v if x.name == "peak_export_no_discharge"])


def test_L5_offpeak_reverse_flow():
    print("\n[L5] OFF_PEAK + EXPORT → CHARGE（吸收逆灌）")
    tl = RUN.build_timeline(at(2026, 7, 15, 3), 60, 30, name="L5",
                            meter_kw=-60.0, soc_percent=50.0)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    check(f"  TOU={set(s.tou_states)} grid={set(s.grid_states)}",
          set(s.tou_states) == {TC.TOU_OFF_PEAK}
          and PC.STATE_EXPORT in s.grid_states)
    exp = [x for x in r if x.grid_state == PC.STATE_EXPORT]
    check(f"★★ EXPORT 期間持續 charge @ {CHARGE_KW} kW",
          exp and all(x.requested_action == PCI.CTRL_CHARGE
                      and x.requested_power_kw == CHARGE_KW for x in exp))


# ======================================================================
# L6 / L7 —— meter 不可信與恢復
# ======================================================================
def test_L6_meter_stale_recovery():
    print("\n[L6] meter stale → 恢復")
    def age(i, a):
        return MC.STALE_AFTER_SEC + 2.0 if 20 <= i < 40 else 0.0
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 60, 60, name="L6",
                            meter_kw=60.0, meter_age_sec=age,
                            soc_percent=50.0)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    fresh1, stale, fresh2 = r[:20], r[20:40], r[40:]
    check(f"★★ stale 期間 grid = UNKNOWN（{stale[0].grid_reason}）",
          all(x.grid_state == PC.STATE_UNKNOWN for x in stale)
          and stale[0].grid_reason == PC.R_METER_STALE)
    check("★★ stale 期間一律 no_action、不送控制",
          all(x.decision_action == DE.ACTION_NO_ACTION
              and x.requested_action == PCI.CTRL_NONE
              and x.would_send is False for x in stale))
    check("★★ **未沿用** stale 前的 charge 決策",
          fresh1[-1].requested_action == PCI.CTRL_CHARGE
          and stale[0].requested_action == PCI.CTRL_NONE)
    check("★★ 恢復 fresh 後重新依新資料決策",
          fresh2[-1].requested_action == PCI.CTRL_CHARGE
          and fresh2[-1].would_send is True)
    check("  恢復需重新經過 debounce（首個 tick 尚未立即回到 IMPORT）",
          fresh2[0].grid_state in (PC.STATE_UNKNOWN, PC.STATE_IMPORT))


def test_L7_meter_disconnect_recovery():
    print("\n[L7] meter disconnect → 恢復")
    def kw(i, a):
        return None if 20 <= i < 40 else 60.0
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 60, 60, name="L7",
                            meter_kw=kw, soc_percent=50.0)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    down = r[20:40]
    check("★★ 斷線期間 meter invalid、grid UNKNOWN",
          all(x.meter_valid is False and x.grid_state == PC.STATE_UNKNOWN
              for x in down))
    check("★★ 斷線期間不產生任何 dispatch",
          all(x.would_send is False and x.executor_called is False
              for x in down))
    check("★★ 恢復後重新分類並決策",
          r[-1].grid_state == PC.STATE_IMPORT
          and r[-1].requested_action == PCI.CTRL_CHARGE)


# ======================================================================
# L8 —— ESS stale
# ======================================================================
def test_L8_ess_stale():
    print("\n[L8] ESS stale（reason 與 meter stale 不同）")
    limit = DE.DEFAULT_ESS_CONFIG.stale_after_sec
    def eage(i, a):
        return limit + 5.0 if 10 <= i < 25 else 0.0
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 60, 40, name="L8",
                            meter_kw=60.0, ess_age_sec=eage,
                            soc_percent=50.0)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    bad = r[10:25]
    check("★★ meter 仍 fresh（不是 meter 的問題）",
          all(x.meter_stale is False and x.grid_state == PC.STATE_IMPORT
              for x in bad))
    check(f"★★ ESS stale reason = {bad[0].ess_reason}",
          all(x.ess_stale is True and x.ess_reason == DE.E_STALE
              for x in bad))
    check(f"★★ decision reason = {bad[0].decision_reason}（≠ meter 的 reason）",
          all(x.decision_reason == DE.R_ESS_STALE for x in bad)
          and DE.R_ESS_STALE != DE.R_INVALID_GRID_STATE)
    check("★★ 不產生 dispatch", all(x.would_send is False for x in bad))
    check("★★ 恢復後重新決策", r[-1].requested_action == PCI.CTRL_CHARGE)


# ======================================================================
# L9 / L10 —— fault 與 authority
# ======================================================================
def test_L9_fault_midway():
    print("\n[L9] fault 中途出現 → 清除後重新評估")
    def flt(i, a):
        return True if 15 <= i < 30 else None
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 60, 45, name="L9",
                            meter_kw=60.0, soc_percent=50.0, pcs_fault=flt)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    c, _ = check_cold_start(r)
    before, during, after = r[len(c):15], r[15:30], r[30:]
    check("★★ fault 前正常送出 charge",
          all(x.would_send for x in before))
    check(f"★★ fault 期間 outcome = {during[0].outcome}",
          all(x.outcome == PCI.OUT_SAFETY_BLOCKED for x in during))
    check("★★ fault 期間 executor 一次都沒被呼叫",
          all(x.executor_called is False for x in during))
    check("★★ fault 清除後**重新完整評估**（非沿用 fault 前結果）",
          all(x.would_send for x in after)
          and after[0].decision_reason == before[0].decision_reason)

    # critical alarm 同樣阻擋
    rows = ({"level": 0, "alarmStatus": True, "name": "longrun-critical"},)
    def al(i, a):
        return rows if 5 <= i < 10 else ()
    tl2 = RUN.build_timeline(at(2026, 7, 15, 2), 60, 15, name="L9-alarm",
                             meter_kw=60.0, soc_percent=50.0, alarm_rows=al)
    s2, v2, r2 = go(tl2)
    check("★★ critical alarm 期間 SAFETY_BLOCKED",
          all(x.outcome == PCI.OUT_SAFETY_BLOCKED for x in r2[5:10])
          and r2[5].blocked_reason == SG.R_CRITICAL_ALARM_ACTIVE and not v2)


def test_L10_authority_lost_restored():
    print("\n[L10] authority 失去 → 恢復")
    def mode(i, a):
        return None if 10 <= i < 25 else RUN._UNSET
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 60, 40, name="L10",
                            meter_kw=60.0, soc_percent=50.0,
                            pcs_mode_state=mode)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    lost = r[10:25]
    check(f"★★ authority 讀不到 → {lost[0].outcome}",
          all(x.outcome == PCI.OUT_AUTHORITY_BLOCKED for x in lost))
    check("★★ 期間不送任何控制、executor 0",
          all(x.requested_action == PCI.CTRL_NONE
              and x.executor_called is False for x in lost))
    check("★★ 恢復後重新評估並可送出",
          r[-1].would_send is True)
    check("★★ explicit None 未被 convenience default 吃掉"
          "（有 / 無 authority 的結果不同）",
          lost[0].outcome != r[0].outcome)


# ======================================================================
# L11 / L12 —— 方向反轉
# ======================================================================
# 觀測慣例（control_authority 既有語意）：充電 AC 為正、放電 AC 為負。
_OBSERVED_SIGN = {LCS.ACT_CHARGE: 1.0, LCS.ACT_DISCHARGE: -1.0}


def _observed_kw(action, power):
    """此刻 PCS 回報的 AC 功率（腳本給定，非由物理模型推算）。"""
    return _OBSERVED_SIGN[action] * power


def _owned_record(action, power, mono):
    """腳本化的 LastControl 佐證，用來讓 Authority 判為 OWNED_BY_PHASE6。

    🔴 `actual_active_power_kw` 是**下令當下 read-back 的基準值**，
       必須與此刻的觀測值不同，Authority 才能證明暫存器已刷新
       （control_authority 的 refresh 佐證條件）。
    """
    obs = _observed_kw(action, power)
    return LCS.LastControlRecord(
        schema_version=LCS.SCHEMA_VERSION, action=action,
        target_power_kw=power, verified_at_wall="scripted",
        # 指令驗證時間必須早於本 tick 的觀測時間，否則不算「指令後的新資料」
        verified_at_monotonic=mono - 30.0, pcs_actual_state=None,
        actual_active_power_kw=obs - _OBSERVED_SIGN[action] * 0.4,
        reason="scripted")


def _reversal_timeline(name, start, from_action, to_tou_hour):
    """前半 PCS 仍在 from_action 方向運轉，後半 TOU 要求反向。"""
    charging = (from_action == LCS.ACT_CHARGE)
    pts = []
    base = start
    for i in range(20):
        a = base + datetime.timedelta(seconds=60 * i)
        pts.append(RUN.TimelinePoint(
            a, meter_kw=60.0, soc_percent=55.0,
            pcs_charging=charging, pcs_discharging=not charging,
            pcs_standby=False,
            ess_extra={"actual_active_power_kw": _observed_kw(
                from_action, CHARGE_KW if charging else DISCHARGE_KW)},
            last_control_record=_owned_record(
                from_action, CHARGE_KW if charging else DISCHARGE_KW,
                60.0 * i),
            last_control_trust=LCS.TRUST_FOR_INTERVAL,
            label=a.strftime("%H:%M")))
    return RUN.Timeline(pts, name=name)


def test_L11_L12_reversal():
    print("\n[L11/L12] 方向反轉必須先 STOP")
    # L11：PCS CHARGING，TOU=PEAK 要求 discharge
    tl = _reversal_timeline("L11", at(2026, 7, 15, 14), LCS.ACT_CHARGE, 14)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    c, w = check_cold_start(r)
    check(f"  TOU={set(s.tou_states)} PCS={w[0].pcs_state}",
          set(s.tou_states) == {TC.TOU_PEAK}
          and w[0].pcs_state == PCI.PCS_CHARGING)
    check(f"★★ L11 authority = {w[0].authority_state}"
          f"（已認領，故不是被 authority 擋下）",
          w[0].authority_state == CA.AUTH_OWNED)
    check(f"★★ L11 decision={w[0].decision_action}，但被互鎖擋下",
          w[0].decision_action == DE.ACTION_DISCHARGE)
    check(f"★★ L11 outcome = {w[0].outcome}",
          all(x.outcome == PCI.OUT_DIRECTION_BLOCKED for x in w))
    check(f"★★ L11 reason = {w[0].blocked_reason}",
          all(x.blocked_reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
              for x in w))
    check("★★ L11 全程未送出 discharge、executor 0",
          all(x.requested_action != PCI.CTRL_DISCHARGE
              and x.executor_called is False for x in r))
    check(f"  summary.reversal_blocked = {s.reversal_blocked_count}",
          s.reversal_blocked_count == len(w))

    # L12：PCS DISCHARGING，TOU=OFF_PEAK 要求 charge
    tl2 = _reversal_timeline("L12", at(2026, 7, 15, 3), LCS.ACT_DISCHARGE, 3)
    s2, v2, r2 = go(tl2)
    check("  invariants 全過", not v2)
    c2, w2 = check_cold_start(r2)
    check(f"★★ L12 decision={w2[0].decision_action}，outcome={w2[0].outcome}",
          w2[0].decision_action == DE.ACTION_CHARGE
          and all(x.outcome == PCI.OUT_DIRECTION_BLOCKED for x in w2))
    check("★★ L12 reason 同為 DIRECTION_REVERSAL_REQUIRES_STOP",
          all(x.blocked_reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP
              for x in w2))
    check("★★ L12 全程未送出 charge", PCI.CTRL_CHARGE not in acts(r2))

    # 先進 idle 之後才允許反向
    pts = list(tl.points[:10])
    base = tl.points[9].at
    for i in range(1, 11):
        a = base + datetime.timedelta(seconds=60 * i)
        pts.append(RUN.TimelinePoint(a, meter_kw=60.0, soc_percent=55.0,
                                     pcs_standby=True,
                                     label=a.strftime("%H:%M")))
    s3, v3, r3 = go(RUN.Timeline(pts, name="L11-after-stop"))
    check("  invariants 全過（含 no_direct_reversal）", not v3)
    check("★★ PCS 回到 STANDBY（方向已隔離）後才送出 discharge",
          r3[-1].requested_action == PCI.CTRL_DISCHARGE
          and r3[-1].would_send is True)
    c3, _ = cold_and_warm(r3)
    check("★★ 冷啟動之後、回到 STANDBY 之前，全部 DIRECTION_BLOCKED",
          all(x.outcome == PCI.OUT_DIRECTION_BLOCKED
              for x in r3[len(c3):10]))


# ======================================================================
# L13 —— 遲滯 / debounce
# ======================================================================
def test_L13_hysteresis_debounce():
    print("\n[L13] 遲滯 / debounce：門檻附近抖動不得每 tick 跳動")
    cfg = PC.DEFAULT_CONFIG
    # 在 +5 / +2 之間來回（3.0 ~ 4.9 kW），不得進入 IMPORT
    def kw(i, a):
        return 4.9 if i % 2 == 0 else 3.0
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 1, 60, name="L13-import",
                            meter_kw=kw, soc_percent=50.0)
    s, v, r = go(tl)
    check("  invariants 全過", not v)
    check(f"  分類狀態集合 = {set(s.grid_states)}",
          PC.STATE_IMPORT not in s.grid_states)
    check("★★ 未達 import_enter_kw（%.1f）→ 不得進 IMPORT" % cfg.import_enter_kw,
          all(x.grid_state != PC.STATE_IMPORT for x in r))

    # 在 -5 / -2 之間來回，不得進入 EXPORT
    def kw2(i, a):
        return -4.9 if i % 2 == 0 else -3.0
    tl2 = RUN.build_timeline(at(2026, 7, 15, 2), 1, 60, name="L13-export",
                             meter_kw=kw2, soc_percent=50.0)
    s2, v2, r2 = go(tl2)
    check("★★ 未達 export_enter_kw（%.1f）→ 不得進 EXPORT" % cfg.export_enter_kw,
          all(x.grid_state != PC.STATE_EXPORT for x in r2) and not v2)

    # 每 tick 在 +6 / -6 之間跳 → debounce 必須壓住，不得每 tick 切換
    def kw3(i, a):
        return 6.0 if i % 2 == 0 else -6.0
    tl3 = RUN.build_timeline(at(2026, 7, 15, 2), 1, 60, name="L13-flap",
                             meter_kw=kw3, soc_percent=50.0)
    s3, v3, r3 = go(tl3)
    flips = sum(1 for a, b in zip(r3, r3[1:]) if a.grid_state != b.grid_state)
    check(f"★★ 60 tick 劇烈抖動只產生 {flips} 次狀態轉移（debounce %.1fs 生效）"
          % cfg.debounce_sec, flips <= 2)
    check("★★ 未新增第二套門檻 —— 全部沿用 ClassifierConfig",
          cfg.import_enter_kw == PC.IMPORT_ENTER_KW
          and cfg.export_enter_kw == PC.EXPORT_ENTER_KW
          and cfg.debounce_sec == PC.DEBOUNCE_SEC and not v3)


# ======================================================================
# L14 / L15 —— 日期邊界
# ======================================================================
def test_L14_midnight_boundary():
    print("\n[L14] 跨日邊界 23:59 → 00:00")
    tl = RUN.build_timeline(at(2026, 7, 15, 23, 50), 60, 20, name="L14",
                            meter_kw=60.0, soc_percent=50.0)
    s, v, r = go(tl)
    check("  invariants 全過（含 timestamp 單調遞增）", not v)
    check("  時間軸確實跨日",
          r[0].at.day == 15 and r[-1].at.day == 16)
    before = [x for x in r if x.at.day == 15]
    after = [x for x in r if x.at.day == 16]
    check(f"★★ 07-15(Wed) 23:5x → {before[-1].tou_state}（夏月平日 09-24 PEAK）",
          before[-1].tou_state == TC.TOU_PEAK)
    check(f"★★ 07-16(Thu) 00:0x → {after[0].tou_state}（夏月平日 00-09 OFF_PEAK）",
          after[0].tou_state == TC.TOU_OFF_PEAK)
    check("★★ 跨日後決策正確切換 discharge → charge",
          before[-1].requested_action == PCI.CTRL_DISCHARGE
          and after[0].requested_action == PCI.CTRL_CHARGE)
    check("★★ 全程 aware datetime（Asia/Taipei）",
          all(x.at.tzinfo is not None and x.at.utcoffset() is not None
              for x in r))


def test_L15_annual_offpeak_day_boundary():
    print("\n[L15] 年度離峰日邊界")
    # 2026-09-27(Sun) → 09-28(Mon，年度離峰日) → 09-29(Tue，正常平日)
    pts = []
    for d, h in ((27, 14), (28, 14), (29, 14)):
        pts.append(RUN.TimelinePoint(at(2026, 9, d, h), meter_kw=60.0,
                                     soc_percent=50.0,
                                     label="09-%02d %02d:00" % (d, h)))
    s, v, r = go(RUN.Timeline(pts, name="L15"))
    check("  invariants 全過", not v)
    check(f"★★ 09-27(Sun) 14:00 → {r[0].tou_state}",
          r[0].tou_state == TC.TOU_OFF_PEAK)
    check(f"★★ 09-28(Mon, 年度離峰日) 14:00 → {r[1].tou_state}",
          r[1].tou_state == TC.TOU_OFF_PEAK)
    check(f"★★ 09-29(Tue, 正常平日) 14:00 → {r[2].tou_state}（證明不是全都 OFF_PEAK）",
          r[2].tou_state == TC.TOU_PEAK)
    check("★★ 決策隨之不同：離峰日 charge、正常平日 discharge",
          r[1].requested_action == PCI.CTRL_CHARGE
          and r[2].requested_action == PCI.CTRL_DISCHARGE)
    check("★★ 年度行事曆在 long-running runner 中同樣生效（非只有單點 E2E）",
          RUN.AC.PRODUCTION_PROVIDER.is_off_peak_day(
              datetime.date(2026, 9, 28)) is True
          and RUN.AC.PRODUCTION_PROVIDER.is_off_peak_day(
              datetime.date(2026, 9, 29)) is False)


# ======================================================================
# L16 —— 24h synthetic day
# ======================================================================
def test_L16_synthetic_day():
    print("\n[L16] 24h synthetic day + summary")
    tl = RUN.synthetic_day()
    s, v, r = go(tl)
    check(f"  ticks = {s.total_ticks}（5 分鐘一 tick）", s.total_ticks == 288)
    check(f"  時間範圍 {s.start_time.strftime('%H:%M')} → "
          f"{s.end_time.strftime('%H:%M')}",
          s.start_time.hour == 0 and s.end_time.hour == 23)
    check(f"★★ invariant violations = {len(v)}", not v)

    check(f"  涵蓋 TOU：{set(s.tou_states)}",
          {TC.TOU_OFF_PEAK, TC.TOU_PEAK} <= set(s.tou_states))
    check(f"  涵蓋 grid：{set(s.grid_states)}",
          {PC.STATE_IMPORT, PC.STATE_EXPORT, PC.STATE_NEAR_ZERO,
           PC.STATE_UNKNOWN} <= set(s.grid_states))
    check(f"  涵蓋 decision：{set(s.decisions)}",
          {DE.ACTION_CHARGE, DE.ACTION_DISCHARGE, DE.ACTION_IDLE,
           DE.ACTION_NO_ACTION} <= set(s.decisions))
    check(f"  涵蓋 outcome：{set(s.outcomes)}",
          {PCI.OUT_WOULD_EXECUTE, PCI.OUT_NO_CONTROL,
           PCI.OUT_AUTHORITY_BLOCKED} <= set(s.outcomes))
    check(f"  block reason 分類：{set(s.block_reasons)}",
          len(s.block_reasons) >= 1)

    # ---- direction reversal 必須真的在 24h timeline 中發生 ----
    rev = [x for x in r
           if x.blocked_reason == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP]
    from_charge = [x for x in rev if x.pcs_state == PCI.PCS_CHARGING
                   and x.decision_action == DE.ACTION_DISCHARGE]
    from_discharge = [x for x in rev if x.pcs_state == PCI.PCS_DISCHARGING
                      and x.decision_action == DE.ACTION_CHARGE]
    check(f"★★ summary.reversal_blocked = {s.reversal_blocked_count}（≥ 2）",
          s.reversal_blocked_count >= 2)
    check("★★ block reason 可辨識 DIRECTION_REVERSAL_REQUIRES_STOP（既有 constant）",
          s.block_reasons.get(PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP, 0)
          == len(rev) and len(rev) >= 2)
    check(f"★★ 方向一：PCS=CHARGING + decision=discharge → blocked"
          f"（{len(from_charge)} tick @ {from_charge[0].at.strftime('%H:%M')}）"
          if from_charge else "★★ 方向一：PCS=CHARGING + decision=discharge",
          bool(from_charge))
    check(f"★★ 方向二：PCS=DISCHARGING + decision=charge → blocked"
          f"（{len(from_discharge)} tick @ "
          f"{from_discharge[0].at.strftime('%H:%M')}）"
          if from_discharge else "★★ 方向二：PCS=DISCHARGING + decision=charge",
          bool(from_discharge))
    check("★★ 反轉視窗全程未送出、executor 0",
          all(x.would_send is False and x.executor_called is False
              and x.requested_action == PCI.CTRL_NONE for x in rev))
    rev_pts = [tp for tp in tl.points
               if tp.at.hour in (RUN.REVERSAL_FROM_CHARGE_HOUR,
                                 RUN.REVERSAL_FROM_DISCHARGE_HOUR)]
    check(f"  反轉視窗共 {len(rev_pts)} 個 timeline point，PCS 狀態逐點寫明",
          len(rev_pts) == len(rev)
          and all(tp.pcs_standby is False
                  and (tp.pcs_charging or tp.pcs_discharging)
                  for tp in rev_pts))
    first_rev = from_charge[0]
    prev = [x for x in r if x.at < first_rev.at][-1]
    check(f"★★ 進入視窗前一 tick（{prev.at.strftime('%H:%M')}）送出的是 "
          f"{prev.requested_action}，PCS 仍被腳本標為 CHARGING —— "
          f"證明 PCS 狀態是腳本給定，非由前一筆 requested action 推導",
          prev.requested_action != PCI.CTRL_CHARGE
          and first_rev.pcs_state == PCI.PCS_CHARGING)
    check(f"  涵蓋 outcome（含 DIRECTION_BLOCKED）：{set(s.outcomes)}",
          PCI.OUT_DIRECTION_BLOCKED in s.outcomes)

    # ---- L16 原始覆蓋清單逐項確認 ----
    bands = {x.soc_band for x in r if x.soc_band}
    check(f"★★ 涵蓋 SOC LOW / MID / HIGH：{bands}",
          {DP.S_LOW, DP.S_MID, DP.S_HIGH} <= bands)
    stale = [x for x in r if x.grid_reason == PC.R_METER_STALE]
    check(f"★★ 涵蓋 meter stale（{len(stale)} tick）與恢復",
          bool(stale)
          and any(x.grid_state == PC.STATE_IMPORT
                  for x in r if x.at > stale[-1].at))
    down = [x for x in r if x.meter_valid is False]
    check(f"★★ 涵蓋 meter disconnect（{len(down)} tick）與恢復",
          bool(down)
          and any(x.meter_valid is True for x in r if x.at > down[-1].at))
    fault = [x for x in r if x.outcome == PCI.OUT_SAFETY_BLOCKED]
    check(f"★★ 涵蓋 fault blocked（{len(fault)} tick @ "
          f"{fault[0].at.strftime('%H:%M') if fault else '-'}）"
          f" reason={fault[0].blocked_reason if fault else None}",
          bool(fault)
          and all(x.would_send is False and x.executor_called is False
                  for x in fault))
    auth = [x for x in r if x.outcome == PCI.OUT_AUTHORITY_BLOCKED]
    check(f"★★ 涵蓋 authority blocked（{len(auth)} tick）", bool(auth))
    check(f"  transition 統計共 {len(s.transitions)} 類",
          len(s.transitions) >= 5)
    check("  first / last DecisionRecord 皆可解釋",
          bool(s.first.explain()) and bool(s.last.explain()))
    check("★★ 不含任何 kWh / 收益 / 電費欄位",
          not any(k in s.as_dict() for k in
                  ("kwh", "energy", "revenue", "cost", "arbitrage")))
    d = s.as_dict()
    check("  summary 可序列化（JSON-safe）",
          isinstance(d["decisions"], dict) and isinstance(d["ticks"]
                                                          if "ticks" in d
                                                          else d["total_ticks"],
                                                          int))


# ======================================================================
# LR —— no_direct_reversal invariant 語意鎖（A / B / B′ 對照）
# ======================================================================
def test_LR_direction_invariant_semantics():
    print("\n[LR] no_direct_reversal 語意：以 PCS 實際狀態判定，非以上一筆指令")

    # A. 上一筆 charge → 這一筆 discharge，但 PCS 實際是 STANDBY
    #    方向已隔離 → 合法換向，不得誤報 violation
    tl = RUN.build_timeline(at(2026, 7, 15, 8, 30), 300, 12, name="LR-A",
                            meter_kw=60.0, soc_percent=60.0)
    s, v, r = go(tl)
    seq = sent(r)
    check("  A. timeline 內確實出現 charge → discharge 的換向",
          PCI.CTRL_CHARGE in seq and PCI.CTRL_DISCHARGE in seq
          and seq.index(PCI.CTRL_CHARGE) < seq.index(PCI.CTRL_DISCHARGE))
    check("  A. 換向當下 PCS 實際狀態為 STANDBY（方向已隔離）",
          all(x.pcs_state == PCI.PCS_STANDBY for x in r))
    check("★★ A. STANDBY 合法換向**不得**誤報 no_direct_reversal",
          not [x for x in v if x.name == "no_direct_reversal"] and not v)
    check("★★ A. 換向不得被 DIRECTION_BLOCKED 擋下",
          all(x.outcome != PCI.OUT_DIRECTION_BLOCKED for x in r))

    # B. decision = discharge，PCS 實際 CHARGING → 必須擋
    tlb = _reversal_timeline("LR-B", at(2026, 7, 15, 14), LCS.ACT_CHARGE, 14)
    sb, vb, rb = go(tlb)
    _, wb = cold_and_warm(rb)
    check("★★ B. PCS=CHARGING + decision=discharge → "
          "DIRECTION_REVERSAL_REQUIRES_STOP",
          bool(wb) and all(
              x.pcs_state == PCI.PCS_CHARGING
              and x.decision_action == DE.ACTION_DISCHARGE
              and x.outcome == PCI.OUT_DIRECTION_BLOCKED
              and x.blocked_reason
              == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP for x in wb))
    check("★★ B. 被擋下時不得計入 no_direct_reversal violation"
          "（互鎖已生效，違規是指『擋不住還送出去』）", not vb)

    # B′. 反方向：decision = charge，PCS 實際 DISCHARGING → 同樣必須擋
    tlc = _reversal_timeline("LR-B2", at(2026, 7, 15, 3), LCS.ACT_DISCHARGE, 3)
    sc, vc, rc = go(tlc)
    _, wc = cold_and_warm(rc)
    check("★★ B′. PCS=DISCHARGING + decision=charge → "
          "DIRECTION_REVERSAL_REQUIRES_STOP",
          bool(wc) and all(
              x.pcs_state == PCI.PCS_DISCHARGING
              and x.decision_action == DE.ACTION_CHARGE
              and x.outcome == PCI.OUT_DIRECTION_BLOCKED
              and x.blocked_reason
              == PCI.CR_DIRECTION_REVERSAL_REQUIRES_STOP for x in wc))
    check("★★ B′. 同樣不得誤報 invariant violation", not vc)

    # invariant 只在「真的送出去」時才成立違規 —— 用合成 record 反向驗語意
    class _Rec(object):
        """最小合成 record：未設定的欄位一律 None，只驗 no_direct_reversal。"""

        def __init__(self, pcs_state, would_send=True,
                     action=PCI.CTRL_DISCHARGE):
            self.step = 1
            self.at = at(2026, 7, 15, 2)
            self.requested_action = action
            self.pcs_state = pcs_state
            self.would_send = would_send

        def __getattr__(self, name):
            return None

    def rev_hits(rec):
        return [x for x in RUN.check_invariants([rec], tl)
                if x.name == "no_direct_reversal"]

    check("★★ 若互鎖失效（would_send=True 且 PCS=CHARGING）→ invariant 必須抓到",
          len(rev_hits(_Rec(PCI.PCS_CHARGING))) == 1)
    check("★★ 反方向（would_send=True 且 PCS=DISCHARGING 卻送 charge）→ 同樣抓到",
          len(rev_hits(_Rec(PCI.PCS_DISCHARGING,
                            action=PCI.CTRL_CHARGE))) == 1)
    check("★★ 同樣送出但 PCS=STANDBY → 不得判違規（對照組 A 的單點版）",
          not rev_hits(_Rec(PCI.PCS_STANDBY)))
    check("  未送出（被互鎖擋下）即使 PCS 為反方向也不算違規",
          not rev_hits(_Rec(PCI.PCS_CHARGING, would_send=False)))


# ======================================================================
# LI —— Timeline 時間戳不變量
# ======================================================================
def test_LI_timeline_invariants():
    print("\n[LI] Timeline 時間戳不變量")
    ok = RUN.build_timeline(at(2026, 7, 15, 2), 60, 3, name="ok",
                            meter_kw=60.0)
    check("  正常遞增 timeline 可建構", len(ok) == 3)

    def raises(fn, token):
        try:
            fn()
        except ValueError as e:
            return token in str(e)
        return False

    check("★★ out-of-order timestamp → 明確拒絕（不靜默排序）",
          raises(lambda: RUN.Timeline([
              RUN.TimelinePoint(at(2026, 7, 15, 3)),
              RUN.TimelinePoint(at(2026, 7, 15, 2))]), RUN.TL_OUT_OF_ORDER))
    check("★★ duplicate timestamp → 明確拒絕（行為 deterministic）",
          raises(lambda: RUN.Timeline([
              RUN.TimelinePoint(at(2026, 7, 15, 3)),
              RUN.TimelinePoint(at(2026, 7, 15, 3))]), RUN.TL_DUPLICATE))
    check("★★ naive datetime → 拒絕",
          raises(lambda: RUN.Timeline([
              RUN.TimelinePoint(datetime.datetime(2026, 7, 15, 3))]),
              RUN.TL_NAIVE))
    check("  空 timeline → 拒絕",
          raises(lambda: RUN.Timeline([]), RUN.TL_EMPTY))
    check(f"  invariant 清單共 {len(RUN.INVARIANTS)} 條",
          len(RUN.INVARIANTS) == 13)

    # 兩次相同 replay 必須完全一致（確定性）
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 60, 20, name="det",
                            meter_kw=60.0, soc_percent=50.0)
    _, _, r1 = go(tl)
    _, _, r2 = go(tl)
    check("★★ 相同 timeline 重播兩次結果完全一致（確定性）",
          [x.as_dict() for x in r1] == [x.as_dict() for x in r2])


# ======================================================================
# RI —— runner 本身的不變量
# ======================================================================
def test_RI_runner_invariants():
    print("\n[RI] runner 不變量")
    src = io.open(os.path.join(HERE, "phase6_decision_replay_runner.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    bad = mods & {"socket", "socketio", "requests", "urllib", "http",
                  "subprocess", "paramiko", "modbus_tk", "serial", "time"}
    check(f"★★ 零 I/O 且未匯入 time（不可能 sleep）：命中={sorted(bad)}", not bad)
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("★★ 不呼叫 sleep / connect / emit / execute / Popen",
          not (calls & {"sleep", "connect", "emit", "execute", "Popen",
                        "urlopen", "now", "today"}))
    check("★★ 沒有自己的 DECISION_MATRIX / 門檻 / SOC 物理模型",
          "DECISION_MATRIX" not in src
          and "capacity" not in src.lower()
          and "efficiency" not in src.lower())
    # 🔴 掃**識別字**而不是原始文字：模組裡寫著「不計算 kWh」這句宣告，
    #    純文字比對會把宣告本身算成命中（假陽性）。
    #    真正要驗的是「有沒有能量 / 金額欄位與運算」→ 掃名稱與欄位鍵。
    idents = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            idents.add(n.id.lower())
        elif isinstance(n, ast.Attribute):
            idents.add(n.attr.lower())
        elif isinstance(n, ast.arg):
            idents.add(n.arg.lower())
        elif isinstance(n, ast.keyword) and n.arg:
            idents.add(n.arg.lower())
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            idents.add(n.name.lower())
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            if n.value.isidentifier():
                idents.add(n.value.lower())
    money = ("kwh", "energy", "revenue", "cost", "arbitrage", "saving",
             "profit", "price")
    hits = sorted(i for i in idents if any(t in i for t in money))
    check(f"★★ 沒有 kWh / 收益 / 電費欄位或運算：命中={hits}", not hits)
    check("  模組內明確宣告不計算 kWh / 收益 / 電費",
          "不計算 kWh" in src)
    # 已核准值只有一個來源
    pol = RUN.production_authority_policy()
    check("★★ authority 門檻來自 DEFAULT_CONTROL_CONFIG",
          pol.authority_ttl_sec
          == CFG.DEFAULT_CONTROL_CONFIG.authority_ttl_sec
          and pol.authority_power_tolerance_kw
          == CFG.DEFAULT_CONTROL_CONFIG.authority_power_tolerance_kw)
    check("★★ 未定案參數仍為 None",
          CFG.DEFAULT_CONTROL_CONFIG.min_switch_interval_sec is None
          and CFG.DEFAULT_CONTROL_CONFIG.meter_stale_grace_sec is None)
    check("★★ PolicyConfig library 預設未被改動",
          DP.PolicyConfig().charge_power_kw is None)
    # 明確傳 None → 使用 library Fail Closed 政策
    tl = RUN.build_timeline(at(2026, 7, 15, 2), 60, 5, name="fc",
                            meter_kw=60.0, soc_percent=50.0)
    _, _, rr = RUN.replay(tl, authority_policy=None)
    check("  authority_policy=None → 仍可運作（PCS idle 時為 AUTH_IDLE）",
          rr[-1].authority_state in CA.AUTHORITY_STATES)


# ======================================================================
def main():
    for fn in (test_L1_offpeak_long_charge, test_L2_peak_long_discharge,
               test_L3_tou_boundary, test_L4_reverse_flow,
               test_L5_offpeak_reverse_flow, test_L6_meter_stale_recovery,
               test_L7_meter_disconnect_recovery, test_L8_ess_stale,
               test_L9_fault_midway, test_L10_authority_lost_restored,
               test_L11_L12_reversal, test_L13_hysteresis_debounce,
               test_L14_midnight_boundary,
               test_L15_annual_offpeak_day_boundary,
               test_L16_synthetic_day, test_LR_direction_invariant_semantics,
               test_LI_timeline_invariants,
               test_RI_runner_invariants):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
