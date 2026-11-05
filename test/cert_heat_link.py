# -*- coding: utf-8 -*-
"""
認證測試報告 —— 6161 連線狀態（只看既有取樣結果，不新增任何 HTTP 請求）
====================================================================
每筆取樣（observations.jsonl）若記有「6161 讀取失敗：…」即視為 6161 讀取失敗；其餘視為成功。
（「6161 無資料（API 回傳空內容）」屬有回應，不算連線失敗。）

連續失敗區段（run）：
  - 相鄰兩筆取樣間隔 ≤ gap_sec（GAP_G）才算連續；間隔過大（服務中斷／重啟）→ 區段在此斷開，
    斷開前的區段「恢復時間無法確認」，斷開後的新區段「起點無法確認」（觀測區間不完整，不虛構起點）。
  - 起點只在「前一筆為連續且成功的取樣」時才算可靠，起點時間＝第一筆失敗取樣時間（有證據的最早失敗）。
  - 認證第一筆就失敗 → 起點無法確認（之前狀態未觀測）。
  - 第一筆失敗到最新一筆失敗達 alert_sec → 判定連線異常（每個區段只提醒一次）；恢復時記錄恢復時間與中斷時長。

純函式＋可重播的 Tracker：服務重啟後由 observations 重播即可得到相同狀態；報告離線重建也用同一套。
"""

from datetime import datetime

FAIL_PREFIX = "6161 讀取失敗"
UNKNOWN = "無法確認"


def _parse(s):
    return datetime.fromisoformat(s) if s else None


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S") if t else None


def _fmt(t):
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else UNKNOWN


def _dur(sec):
    sec = int(round(sec))
    return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"


def failure_text(notes, fail_reason=""):
    """該筆取樣本身記錄的 6161 讀取失敗原因（整句「6161 讀取失敗：…」，取自 notes／fail_reason）；沒有 → None。"""
    for x in list(notes or []) + [y for y in (fail_reason or "").split("；") if y]:
        if str(x).startswith(FAIL_PREFIX):
            return str(x)
    return None


def heat_failed(rec):
    """這筆取樣的 6161 讀取是否失敗（只依既有紀錄的原因文字）。"""
    return failure_text(rec.get("notes"), rec.get("fail_reason")) is not None


class HeatLinkTracker:
    """逐筆餵入取樣紀錄，回傳應記錄的事件；同一份紀錄重播結果相同。"""

    def __init__(self, gap_sec, alert_sec):
        self.gap_sec = gap_sec
        self.alert_sec = alert_sec
        self.prev_t = None
        self.run = None             # 進行中的連續失敗區段
        self.runs = []              # 已結束的區段

    @staticmethod
    def _new_run(t, start_known, start_note):
        return {"first": t, "last": t, "start_known": start_known, "start_note": start_note,
                "end": None, "end_known": None, "end_note": "", "alerted": False}

    def _close(self, run, end, end_known, note):
        run.update(end=end, end_known=end_known, end_note=note)
        self.runs.append(run)

    def feed(self, rec):
        t = _parse(rec["t"])
        failed = heat_failed(rec)
        contiguous = self.prev_t is not None and (t - self.prev_t).total_seconds() <= self.gap_sec
        evs = []
        run = self.run
        if run is not None and not contiguous:
            note = f"取樣中斷（{_fmt(self.prev_t)}～{_fmt(t)}），恢復時間無法確認；觀測區間不完整"
            self._close(run, None, False, note)
            if run["alerted"]:
                evs.append(("heat_link_gap", run, f"6161 連線異常（{self.describe_start(run)}）期間{note}"))
            self.run = run = None
        if failed:
            if run is None:
                if self.prev_t is None:
                    run = self._new_run(t, False, "認證第一筆取樣即失敗，之前狀態未觀測")
                elif not contiguous:
                    run = self._new_run(t, False, f"取樣中斷（{_fmt(self.prev_t)}～{_fmt(t)}）後首筆即失敗")
                else:
                    run = self._new_run(t, True, "")
                self.run = run
            run["last"] = t
            if not run["alerted"] and (t - run["first"]).total_seconds() >= self.alert_sec:
                run["alerted"] = True
                evs.append(("heat_link_down", run,
                            f"6161 連續讀取失敗已達 {_dur((t - run['first']).total_seconds())}"
                            f"（{self.describe_start(run)}）；即時熱度截圖可能失敗"))
        elif run is not None:
            self._close(run, t, True, "")
            if run["alerted"]:
                d = (t - run["first"]).total_seconds()
                evs.append(("heat_link_up", run,
                            f"6161 恢復讀取 {_fmt(t)}；中斷時長 {'' if run['start_known'] else '至少 '}{_dur(d)}"
                            f"（{self.describe_start(run)}）"))
            self.run = None
        self.prev_t = t
        return evs

    @staticmethod
    def describe_start(run):
        return (f"起點 {_fmt(run['first'])}" if run["start_known"]
                else f"起點{UNKNOWN}：{run['start_note']}，最早觀測失敗 {_fmt(run['first'])}")

    def all_runs(self):
        return self.runs + ([self.run] if self.run else [])

    def status(self):
        """選單狀態用：只有已達提醒門檻、尚未恢復的區段才顯示異常。"""
        r = self.run
        if r is None or not r["alerted"]:
            return {"state": "ok"}
        return {"state": "down", "first_fail": _iso(r["first"]), "last_fail": _iso(r["last"]),
                "start_known": r["start_known"], "start_note": r["start_note"],
                "seconds": round((r["last"] - r["first"]).total_seconds())}


def replay(records, gap_sec, alert_sec):
    """依取樣紀錄重播，回傳 (tracker, 依序產生的事件)。"""
    tr = HeatLinkTracker(gap_sec, alert_sec)
    evs = []
    for rec in records:
        evs += tr.feed(rec)
    return tr, evs


def event_key(kind, run):
    return f"{kind}|{_iso(run['first'])}"


def run_at(runs, t, gap_sec):
    """觸發時間 t 當下正處於的連續失敗區段（有證據涵蓋 t）；沒有 → None。"""
    for r in runs:
        if r["first"] > t:
            continue
        if r["end"] is not None and r["end"] > t:
            return r
        if r["end"] is None and (t - r["last"]).total_seconds() <= gap_sec:
            return r
    return None


def failure_line(runs, trigger_time, gap_sec):
    """即時熱度截圖失敗的說明行：有可靠的連續失敗起點才顯示時間，否則「無法確認」。"""
    t = _parse(trigger_time) if isinstance(trigger_time, str) else trigger_time
    r = run_at(runs, t, gap_sec) if t is not None else None
    if r is None or not r["start_known"]:
        return f"6161 連續讀取失敗起點：{UNKNOWN}"
    return f"6161 連續讀取失敗起點：{_fmt(r['first'])}（認證取樣紀錄）"
