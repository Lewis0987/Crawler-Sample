# -*- coding: utf-8 -*-
"""
sample_scheduler.py — 固定時間格點取樣排程（認證報告／單次充放電報告共用）
==========================================================================
第 k 格的期限 = 起點（monotonic）+ k × 間隔。

  - 排程只用 monotonic clock：系統時間被調整（含倒退）不影響間隔，也不會累積漂移。
  - 實際時間（具時區）只用來**記錄**，不參與排程。
  - 單執行緒同步使用：一格的取樣做完才會等下一格 → 不會重疊。
  - 取樣做完時若已超過後面的格子 → 直接跳到下一個**未來**的格子，跳過的格數記為
    missed_slots。**不補造歷史資料、不在同一格產生第二筆。**

本模組只負責「何時取樣」，不讀設備、不寫檔。
"""
import math
import time
from datetime import datetime


def wall_now():
    """具時區資訊的實際時間（記錄用）。"""
    return datetime.now().astimezone()


def iso_ms(dt):
    """具時區、毫秒精度的時間字串；None → None。"""
    return dt.isoformat(timespec="milliseconds") if dt is not None else None


class SlotScheduler(object):
    """
    用法（單一取樣執行緒）：
        sch = SlotScheduler(1.0)
        while True:
            slot = sch.wait(stop_event)      # None = 已要求停止
            if slot is None: break
            ...取樣一次...
            missed = sch.complete()          # 本格之後被跳過的格數
    """

    def __init__(self, interval_sec, mono=time.monotonic, wall=wall_now, max_sleep=None):
        if not (isinstance(interval_sec, (int, float)) and interval_sec > 0):
            raise ValueError("interval_sec 必須 > 0：%r" % (interval_sec,))
        self.interval = float(interval_sec)
        self._mono = mono
        self._wall = wall
        # max_sleep：每次等待的上限（真實秒）。None＝一次等到期限（正式）；
        # 注入縮放時鐘的離線測試給小值（輪詢），等待長度才會跟著縮放。
        self._max_sleep = max_sleep
        self.reset()

    def reset(self):
        """重新起算（例如報告結束後再開始）。下一次 wait() 立即取樣（第 0 格）。"""
        self.t0 = None
        self.k = 0
        self.total_missed = 0
        self.slots_used = 0

    def deadline(self, k=None):
        k = self.k if k is None else k
        return None if self.t0 is None else self.t0 + k * self.interval

    def wait(self, stop_event=None):
        """
        等到目前格子的期限。回傳 slot dict；stop_event 被設定 → None。
        slot：index / scheduled_mono / start_mono（實際開始）/ start_wall（具時區）/ lag_ms（開始落後期限）
        """
        if self.t0 is None:
            self.t0 = self._mono()
        due = self.deadline()
        while True:
            now = self._mono()
            rem = due - now
            if rem <= 0:
                break
            if self._max_sleep is not None:
                rem = min(rem, self._max_sleep)
            if stop_event is not None:
                if stop_event.wait(rem):
                    return None
            else:
                time.sleep(rem)
        if stop_event is not None and stop_event.is_set():
            return None
        start = self._mono()
        return {"index": self.k, "scheduled_mono": due, "start_mono": start,
                "start_wall": self._wall(), "lag_ms": round((start - due) * 1000.0, 1)}

    def complete(self):
        """
        本格取樣完成。下一格 = 第一個期限**不早於**「現在」的格子（剛好到期 → 立即使用，不跳過）。
        回傳跳過的格數（≥ 0）。
        """
        if self.t0 is None:
            return 0
        now = self._mono()
        nxt = max(self.k + 1, int(math.ceil((now - self.t0) / self.interval - 1e-9)))
        missed = nxt - (self.k + 1)
        self.k = nxt
        self.total_missed += missed
        self.slots_used += 1
        return missed
