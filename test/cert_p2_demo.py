# -*- coding: utf-8 -*-
"""
認證測試報告 P2 —— 截圖示範（模擬階段事件，不連接設備控制、不充放電）
====================================================================
以 TW 兩循環資料的各階段產生「模擬階段事件」，交給 ScreenshotWorker 截圖，再產生含圖片的 Excel。
事件：每個階段先拍候選截圖（模擬首筆觀測待機）→ 判定為階段結束；第 2 階段另有一次停頓點截圖
（判定為停頓 → 只留資料夾，不放入報告）。

模式：
  live ：實際開啟兩個頁面截圖（唯讀）。圖片為 P2 驗證當下的畫面，**不是 2025-12-18 的階段結束畫面**。
  files：設備離線時的版面驗證：使用 2026-10-27 範圍確認時擷取的兩張成品（保留原擷取時間），
         **不是本次 P2 擷取，也不是歷史階段結束畫面**。

用法：
  python cert_p2_demo.py live  --folder <測試資料夾> --out <xlsx>
  python cert_p2_demo.py files --folder <測試資料夾> --out <xlsx> --overview-png <png> --heat-png <png>
       [--overview-time 2026-10-27T16:13:48] [--heat-time 2026-10-27T16:14:01]
"""

import argparse
import os
import shutil
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import cert_report_replay as RP
from cert_screenshot import ScreenshotWorker, load_manifest, png_size, shot_params

TW = r"D:\_DS\Desktop\TW_temp_2025-12-18.csv"
LABELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "cert_reports_offline",
                      "P1_sample_TW20251218_stage_labels.jsonl")


def files_capturer(paths, times):
    """把指定 PNG 當成截圖結果（保留原擷取時間）。只用於設備離線時的版面驗證。"""
    def cap(target, out_png, p, procs=None):
        shutil.copyfile(paths[target.key], out_png)
        w, h = png_size(out_png)
        t = times[target.key]
        return {"captured_at": t, "rect": [0, 0, w, h], "size": [w, h],
                "data_time": t if target.key == "heat" else None, "page_clock": None}
    return cap


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "files"])
    ap.add_argument("--folder", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--overview-png")
    ap.add_argument("--heat-png")
    ap.add_argument("--overview-time", default="2026-10-27T16:13:48")
    ap.add_argument("--heat-time", default="2026-10-27T16:14:01")
    a = ap.parse_args(argv)

    import cert_report_config as CFG
    from cert_stage_engine import analyze
    res = analyze(RP.load_tw(TW), CFG.params(GAP_G_SEC=90), RP.load_decisions(LABELS))
    if os.path.exists(os.path.join(a.folder, "screenshots.jsonl")):
        sys.exit(f"資料夾已有截圖紀錄（append-only，不覆寫）：{a.folder}")

    if a.mode == "live":
        wk = ScreenshotWorker(a.folder, params=shot_params())
        note = (f"P2 驗證擷取（{datetime.now():%Y-%m-%d}，模擬階段事件觸發）："
                "為驗證當下的實際畫面，不是 2025-12-18 的階段結束畫面。")
    else:
        times = {"overview": datetime.fromisoformat(a.overview_time), "heat": datetime.fromisoformat(a.heat_time)}
        wk = ScreenshotWorker(a.folder, params=shot_params(SHOT_RETRY_DELAY_SEC=0),
                              capturer=files_capturer({"overview": a.overview_png, "heat": a.heat_png}, times))
        note = ("版面驗證用：設備離線無法即時擷取，使用 2026-10-27 範圍確認時擷取的成品，"
                "不是本次 P2 擷取，也不是 2025-12-18 的階段結束畫面。")

    for st in res.stages:
        sim = f"P2 模擬事件：第 {st.no} 階段（TW 資料 {st.A.t:%H:%M} 結束）"
        trig = datetime.now() if a.mode == "live" else None
        if st.no == 2:
            pz = wk.submit("candidate", st.no, st.start.t, f"第{st.no}階段", sim + "中途首筆觀測待機",
                           trigger_time=trig)
            wk.wait_idle(600)
            wk.resolve(pz, "pause", "模擬：60 秒內恢復同方向 → 停頓點（只留資料夾）")
        c = wk.submit("candidate", st.no, st.start.t, f"第{st.no}階段", sim + "首筆觀測待機（B）",
                      trigger_time=datetime.now() if a.mode == "live" else None)
        wk.wait_idle(600)
        wk.resolve(c, "stage_end", "模擬：待機達 60 秒，停止確認")
    wk.close(60)

    RP.main(["tw", "--csv", TW, "--gap", "90", "--decisions", LABELS, "--cabinet-spec", "340KW",
             "--cycles", "2", "--out", a.out, "--shots-dir", a.folder, "--shot-note", note])
    recs = load_manifest(a.folder)
    for r in recs:
        if r["type"] == "result":
            print(f"  {r['job_id']} {r['target']}: {r['status']} {r.get('file') or r['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
