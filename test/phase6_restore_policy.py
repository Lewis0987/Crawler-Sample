# -*- coding: utf-8 -*-
"""
phase6_restore_policy.py — C3 RESTORE_PRECHECK 的已核准政策常數（2026-11-06）
============================================================================
🔴 為什麼獨立成一個模組
    `phase6_handoff_orchestrator` 的既有契約是「不硬編碼任何時刻」——
    交接的**進場**條件一律由 TOU / SOC / Authority / PCS / meter / alarm 決定。
    這條規則不變。restore 時段則是使用者另行核准的**收尾政策**（P-1），
    本質上就是時刻，因此集中放在這裡，與交接進場邏輯分開，便於稽核與修改。

P-1  restore 只允許在本機時間 00:00 <= t < 05:30，**且** Phase 6 TOU == OFF_PEAK。
     · auto_control.py 的 is_offpeak() 在任何日期的 00:00–05:30 都屬於離峰。
     · 05:30 到 06:00 的 30 分鐘緩衝，假設 EC015-Server 與本機時差 < 30 分鐘、
       時區相同（現場時鐘證據尚待補齊）。
     · 時間來源：provider 的 now_fn（Windows 本機時間，Asia/Taipei，不帶時區）。
P-8  restore 時 PCS 只允許 STANDBY / STOPPED（只是必要條件之一）。
     CHARGING 時 authority 不可能是 IDLE，允許它只會造成誤解。
時效 完整 precheck 完成到送出之間不得超過 30 秒；超過即 WITHHELD（不重跑）。

修改任一項都必須重新取得使用者裁示。
"""
import datetime

RESTORE_WINDOW_START = datetime.time(0, 0)
RESTORE_WINDOW_END = datetime.time(5, 30)
RESTORE_ALLOWED_PCS_STATES = ("STANDBY", "STOPPED")
RESTORE_PRECHECK_MAX_AGE_SEC = 30.0

# ---- GAP-6（2026-11-06）：TOU 與時段使用同一時間基準 ----
# TOU（decision_observer / TariffProvider）使用 `PRD.build_local_now()`
# = datetime.now(Asia/Taipei)（aware）。C3 的所有時間點一律使用同一來源，
# 並且必須是 +08:00（台灣無日光節約）。
RESTORE_TIME_UTC_OFFSET = datetime.timedelta(hours=8)
# 主機本機（naive，Windows 時區）與 Asia/Taipei 的允許差距：
# 超過即代表 Windows 時區設定與 TOU 基準不一致。
HOST_CLOCK_MAX_SKEW_SEC = 60.0
# 取樣期間牆上時間與 monotonic 的允許偏差（偵測取樣中途被調整時鐘）。
WALL_MONO_MAX_DRIFT_SEC = 2.0


def in_restore_window(now):
    """`now` 是否在 restore 允許時段。非 datetime 一律 False（Fail Closed）。"""
    if not isinstance(now, datetime.datetime):
        return False
    t = now.time()
    return RESTORE_WINDOW_START <= t < RESTORE_WINDOW_END
