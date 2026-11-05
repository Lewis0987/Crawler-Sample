# -*- coding: utf-8 -*-
"""
認證測試報告 —— 已結束認證的事後補拍（post_test_supplement；Menu 24）
====================================================================
用途：原始「階段結束」即時熱度截圖失敗或缺少時，認證結束後補拍 6161 即時熱度（獨立櫃 110）供設備後續狀態參考。
**補拍不是原始截圖**：不代表階段結束時的熱度狀態、不取代原始失敗紀錄、不套用原始截圖的新鮮度合格判定。

保存（全部在目標認證的 supplements 子資料夾；原始 screenshots.jsonl／events.jsonl／cert_state.json／PNG 一律不寫）：
  supplements/screenshots.jsonl      補拍工作與結果（append-only；沿用 ScreenshotWorker 格式）
  supplements/screenshots/<job>.png  補拍照片（Job ID 唯一，重複補拍不覆寫）
  supplements/reports.jsonl          修訂報告產生紀錄（有補拍資料的認證，Menu 25 改寫這裡）

修訂報告選圖規則（固定、可重現）：同一階段採用「最早一次成功」的補拍（依實際擷取時間，同一秒依紀錄先後）。
  之後再補拍不會改變已採用的圖片；被採用的檔案若遺失 → 顯示「圖片遺失」，不改用其他補拍。
"""

import glob
import json
import os
import re

KIND = "post_test_supplement"
SUBDIR = "supplements"
REPORT_LOG = "reports.jsonl"
SOURCE = "6161 即時熱度（獨立櫃 110）"
PAGE_TIME_NOTE = "瀏覽器時間，非設備量測時間"


def folder_of(cert_folder):
    return os.path.join(cert_folder, SUBDIR)


def load(cert_folder):
    """補拍紀錄（沒有 → []）。"""
    path = os.path.join(folder_of(cert_folder), "screenshots.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def has_supplements(cert_folder):
    return os.path.isdir(folder_of(cert_folder))


def _jobs(records, stage_start, target):
    jobs = {r["job_id"]: dict(r, results={}, seq=i) for i, r in enumerate(records)
            if r["type"] == "job" and r.get("kind") == KIND and r.get("stage_start") == stage_start
            and target in r.get("targets", [])}
    for r in records:
        if r["type"] == "result" and r["job_id"] in jobs:
            jobs[r["job_id"]]["results"][r["target"]] = r
    return list(jobs.values())


def select(records, stage_start, target="heat"):
    """
    報告用：某階段的事後補拍 → (job, result, 該階段補拍工作總數)。
    有成功紀錄 → 最早一次成功（captured_at，同一秒依紀錄先後）；沒有成功 → 最後一次登記的工作（或尚未完成：result=None）；沒有補拍 → (None, None, 0)。
    """
    jobs = _jobs(records, stage_start, target)
    if not jobs:
        return None, None, 0
    ok = [(j, j["results"][target]) for j in jobs
          if target in j["results"] and j["results"][target]["status"] in ("ok", "stale")]
    if ok:
        j, r = min(ok, key=lambda jr: (jr[1].get("captured_at") or "", jr[0]["seq"]))
        return j, r, len(jobs)
    j = max(jobs, key=lambda j_: j_["seq"])
    return j, j["results"].get(target), len(jobs)


def candidates(cert_folder, res, names, select_stage_shot, load_manifest):
    """
    可事後補拍的階段：原始「階段結束」即時熱度截圖失敗或缺少（成功／資料未更新的不列入）。
    回傳 [{stage_no, stage_start, name, end_note, orig_job_id, orig_status, orig_reason, orig_trigger, n_supplements}]。
    """
    recs = load_manifest(cert_folder)
    supp = load(cert_folder)
    out = []
    for st in res.stages:
        key = st.start.t.strftime("%Y-%m-%dT%H:%M:%S")
        job, r = select_stage_shot(recs, key, "heat")
        if r is not None and r["status"] in ("ok", "stale"):
            continue
        out.append({"stage_no": st.no, "stage_start": key, "name": names[st.no], "end_note": st.end_note,
                    "stage_end": st.end_B.t.strftime("%Y-%m-%dT%H:%M:%S") if st.end_B is not None else None,
                    "orig_job_id": job["job_id"] if job else None,
                    "orig_status": r["status"] if r else ("pending" if job else "missing"),
                    "orig_reason": (r or {}).get("reason") or ("截圖尚未完成" if job else "沒有階段結束截圖紀錄"),
                    "orig_trigger": job.get("trigger_time") if job else None,
                    "n_supplements": len(_jobs(supp, key, "heat"))})
    return out


def report_numbers(cert_folder, cid, state):
    """已使用過的報告編號：reports 資料夾現有檔案＋cert_state.json 紀錄＋supplements/reports.jsonl 紀錄（刪除的編號也不重用）。"""
    pat = re.compile(re.escape(f"認證測試報告_{cid}_") + r"(\d+)")
    used = set()
    names = [os.path.basename(p) for p in glob.glob(os.path.join(glob.escape(os.path.join(cert_folder, "reports")), "*"))]
    names += [str(r.get("file", "")) for r in (state or {}).get("reports") or []]
    names += [str(r.get("file", "")) for r in load_report_log(cert_folder)]
    for n in names:
        m = pat.search(n)
        if m:
            used.add(int(m.group(1)))
    return used


def load_report_log(cert_folder):
    path = os.path.join(folder_of(cert_folder), REPORT_LOG)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_report_log(cert_folder, rec):
    os.makedirs(folder_of(cert_folder), exist_ok=True)
    with open(os.path.join(folder_of(cert_folder), REPORT_LOG), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
