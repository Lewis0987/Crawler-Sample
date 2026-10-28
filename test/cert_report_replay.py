# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— P1 離線回放工具
=======================================
把既有資料轉成觀測序列，跑 cert_stage_engine，產生離線 Excel 範例。
**只讀取**來源檔，輸出到 --out 指定的新檔；不連設備、不碰服務、不改單次報告的任何檔案。

用法：
  python cert_report_replay.py samples --sessions <session_dir> [...] --out <xlsx>
  python cert_report_replay.py tw --csv <TW_temp.csv> --out <xlsx>
選項：
  --gap <秒>          覆寫 G（僅供敏感度示範；報告會標示使用值與狀態）
  --decisions <jsonl> 操作人員確認紀錄（只讀）
  --cabinet-spec <規格> 測試資訊：電櫃規格（例 340KW）；不輸入 → 標題顯示「電櫃規格未設定」
  --cycles <數>       測試資訊：循環數；不輸入 → 標題不寫循環數（不自動推算）
  --shots-dir <資料夾> P2 截圖資料夾（含 screenshots.jsonl）；不指定 → 截圖頁只放「待截圖」標示
  --shot-note <文字>   截圖說明的附註（例如「P2 驗證擷取，非歷史階段結束畫面」）
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import cert_report_config as CFG
from cert_stage_engine import Obs, analyze, fmt_t, rest_between, stage_names
from cert_report_xlsx import save_report
from cert_screenshot import load_manifest

OFFLINE_NOTE = "本檔為 P1 離線範例（回放既有資料），不是實際認證測試報告。"


def load_samples(session_dirs):
    """單次報告 samples.csv → Obs。欄位不足者（傳感器、PCS 模組溫度）留空 → 報表 N/A。"""
    out = []
    for d in session_dirs:
        name = os.path.basename(os.path.normpath(d))
        path = os.path.join(d, "samples.csv")
        with open(path, encoding="utf-8-sig", newline="") as f:
            for ln, r in enumerate(csv.DictReader(f), 2):
                ok = str(r.get("communication_ok", "")).strip().lower() == "true"
                out.append(Obs(
                    t=datetime.fromisoformat(r["timestamp"]),
                    ok=ok,
                    values={
                        "soc": r.get("soc_percent"),
                        "current_a": r.get("battery_current_a"),
                        "power_kw": r.get("actual_active_power_kw"),
                        "rack_max_c": r.get("rack_max_temperature_c"),
                        "rack_min_c": r.get("rack_min_temperature_c"),
                        "voltage_v": r.get("battery_voltage_v"),
                        "sensor_c": None,
                        "pcs_module_c": None,
                    },
                    src=f"{name}/samples.csv:{ln}",
                    fail_reason="" if ok else "communication_ok=False",
                ))
    return out


def load_tw(path):
    """TW_temp CSV（每分鐘一筆）→ Obs。沒有功率欄 → 方向依電流。"""
    out = []
    name = os.path.basename(path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        for ln, r in enumerate(csv.DictReader(f), 2):
            out.append(Obs(
                t=datetime.strptime(r["時間"].strip(), "%Y/%m/%d %H:%M"),
                values={
                    "soc": r.get("SOC"),
                    "current_a": r.get("電流"),
                    "power_kw": None,
                    "rack_max_c": r.get("Rack 最大溫度"),
                    "rack_min_c": r.get("Rack 最小溫度"),
                    "voltage_v": r.get("總電壓"),
                    "sensor_c": r.get("傳感器溫度"),
                    "pcs_module_c": r.get("PCS 模組溫度"),
                },
                src=f"{name}:{ln}",
            ))
    return out


def load_decisions(path):
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize(res):
    names = stage_names(res.stages)
    return {
        "params": res.params,
        "stages": [{
            "no": st.no, "name": names[st.no], "dir": st.dir_name,
            "start": fmt_t(st.start.t, True), "start_certain": st.start_certain,
            "start_note": st.start_note, "end_note": st.end_note, "end_by": st.end_by,
            "end_certain": st.end_certain,
            "pauses_certain": sum(1 for pz in st.pauses if pz.certain),
            "uncertain_items": [pz.item_id for pz in st.pauses if not pz.certain],
            "label": st.label,
            "rest_to_next": (dict(zip(("sec", "unknown_reason"), rest_between(res, st, res.stages[k + 1])))
                             if k + 1 < len(res.stages) else None),
            # 觀測間隔 = 下一階段首筆觀測運行 − 本階段最後觀測運行（原始精度；非已證實的待機時間）
            "observed_gap_to_next_sec": ((res.stages[k + 1].start.t - st.A.t).total_seconds()
                                         if k + 1 < len(res.stages) else None),
        } for k, st in enumerate(res.stages)],
        "events": {k: sum(1 for e in res.events if e[1] == k) for k in {e[1] for e in res.events}},
        "observations": len(res.obs),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="認證報告 P1 離線回放")
    ap.add_argument("kind", choices=["samples", "tw"])
    ap.add_argument("--sessions", nargs="*", default=[])
    ap.add_argument("--csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--gap", type=float)
    ap.add_argument("--decisions")
    ap.add_argument("--cabinet-spec")
    ap.add_argument("--cycles", type=int)
    ap.add_argument("--shots-dir")
    ap.add_argument("--shot-note", default="")
    a = ap.parse_args(argv)

    over = {}
    if a.gap is not None:
        over["GAP_G_SEC"] = a.gap
    p = CFG.params(**over)
    notes = [OFFLINE_NOTE]
    if a.gap is not None and a.gap != CFG.GAP_G_SEC:
        notes.append(f"G 覆寫為 {a.gap:g} 秒：僅為參數敏感度示範，不是建議值，也不是正式設定。")

    if a.kind == "samples":
        obs = load_samples(a.sessions)
        conditions = ["冷氣溫度：（離線範例，未提供）", "硬體概述：（離線範例，未提供）",
                      "測試條件：（離線範例）回放單次報告 samples.csv，非實際認證測試。"]
        notes += [
            "資料來源：單次報告 samples.csv（依時間串接）：" + "、".join(
                os.path.basename(os.path.normpath(s)) for s in a.sessions) + "。",
            "不同 Session 之間沒有取樣：依 v3 規則屬「資料間隔過長」→ 狀態未知，不推定其間狀態。",
            "傳感器溫度：samples.csv 沒有多功能傳感器欄位 → 全部 N/A（不以其他溫度代替）。",
            "PCS 模組溫度：samples.csv 沒有此欄位 → 全部 N/A。",
            "Rack 最大／最小溫度：單次報告在 API 暫無值時會沿用上一筆有效值寫入 samples.csv，且未標記；"
            "本回放無法分辨。正式認證取樣將使用原始讀值，不沿用。",
            "方向判定：battery_current_a（充正放負）。不用 actual_active_power_kw：停止後該值延遲約 15～20 秒、"
            "待機時約 −1.2 kW，會誤判為放電。來源取樣間隔約 6 秒（服務 10/27 16:25 重啟前的設定）。",
            "停止指令時間（Tc）：samples.csv 沒有選單指令紀錄 → 未列出。",
        ]
    else:
        obs = load_tw(a.csv)
        conditions = ["冷氣溫度： 製冷21℃，製熱：17℃（取自範本 Summary，屬 2025-12-18 該次測試）",
                      "硬體概述：潤泰退回pack，無冷卻液（取自範本 Summary）",
                      "測試條件：冷氣運行超過12hr，定功率170kw，2個循環。（取自範本 Summary）"]
        notes += [
            f"資料來源：{os.path.basename(a.csv)}（每分鐘一筆、時間無秒數）。",
            "取樣間隔 60 秒：若大於 G，依 v3 規則相鄰取樣之間全部屬「資料間隔過長」→ 狀態未知，"
            "階段無法確認成立或結束。這反映來源資料解析度不足，不是判定錯誤。",
            "方向判定：沒有功率欄 → 依電流（放電為負）。",
            "PCS 模組溫度欄在來源中為空 → N/A；來源備註欄（皆為「*」）未帶入。",
            "停止指令時間（Tc）：來源沒有指令紀錄 → 未列出。",
        ]
    res = analyze(obs, p, load_decisions(a.decisions))
    meta = {"conditions": conditions, "source_notes": notes,
            "cabinet_spec": a.cabinet_spec, "cycles": a.cycles}
    if a.shots_dir:
        meta["screenshots"] = {"folder": a.shots_dir, "records": load_manifest(a.shots_dir), "note": a.shot_note}
    path = save_report(res, meta, a.out)
    summ = summarize(res)
    summ["test_info"] = {"cabinet_spec": a.cabinet_spec, "cycles": a.cycles}
    with open(os.path.splitext(path)[0] + ".summary.json", "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2, default=str)
    print(f"已產生：{path}")
    for s in summ["stages"]:
        print(f"  {s['no']}. {s['name']}  開始 {s['start']}{'' if s['start_certain'] else '（不確定）'}"
              f"  | {s['end_note']}  | 停頓 {s['pauses_certain']}  不確定 {len(s['uncertain_items'])}")
    print("  事件：", summ["events"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
