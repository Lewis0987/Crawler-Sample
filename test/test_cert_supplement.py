# -*- coding: utf-8 -*-
"""
已結束認證的事後補拍（post_test_supplement；Menu 24）測試（離線）
================================================================
不連 6161、不開瀏覽器：假 6161 讀取器（可切換恢復／未恢復）與假截圖函式（複製確認成品並加上可辨識的 PNG 文字區塊）。
合成情境比照 CERT_20261104_090053：第 1 階段即時熱度截圖成功、最後階段（充電）即時熱度截圖失敗（6161 逾時）、
認證結束（最後狀態）也失敗；已有 _01，且 cert_state 記錄的 _02 已不在 reports 資料夾。
驗證：Menu 24 進行中功能不變、已結束認證可指定階段、6161 未恢復不產生假圖、重複補拍不覆寫、
     原始失敗紀錄與補拍照片同時顯示、補拍時間與階段結束時間分開、不判為原始截圖成功、
     Menu 25 重建一致、報告編號不重複、原始 Historical Evidence 內容不變。
執行：python test_cert_supplement.py
"""

import glob
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import zipfile
import zlib
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from openpyxl import load_workbook

import cert_requests as REQ
import cert_supplement as SUP
from cert_menu import UI
from cert_runtime import CertRuntime, _read_jsonl
from cert_screenshot import ShotError, load_manifest, png_size, select_stage_shot, shot_params

_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


CROPS = os.path.join(HERE, "..", "output", "cert_reports_offline", "P2_layout_TW20251218_confirmed_crops", "screenshots")
SRC_HEAT = sorted(glob.glob(os.path.join(CROPS, "*_heat.png")))[0]
SRC_OV = sorted(glob.glob(os.path.join(CROPS, "*_overview.png")))[0]
SP = shot_params(SHOT_ATTEMPTS=2, SHOT_RETRY_DELAY_SEC=0, SHOT_ATTEMPT_HARD_TIMEOUT_SEC=10)
CID = "CERT_20261104_090000"


def png_with_tag(src, dst, tag):
    """複製 PNG 並在 IEND 前加 tEXt 區塊（仍是合法 PNG；用來辨識是哪一次補拍）。"""
    b = open(src, "rb").read()
    i = b.rfind(b"IEND") - 4
    data = b"Comment\x00" + tag.encode()
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data + struct.pack(">I", zlib.crc32(b"tEXt" + data) & 0xFFFFFFFF)
    open(dst, "wb").write(b[:i] + chunk + b[i:])


class FakeCap:
    """假截圖：fail=True → 開頁失敗；頁面時間刻意落後 10 分鐘（驗證事後補拍不套用新鮮度判定）。"""

    def __init__(self):
        self.fail = False
        self.calls = 0

    def __call__(self, target, out_png, p, procs=None):
        self.calls += 1
        if self.fail:
            raise ShotError("模擬：開頁失敗 net::ERR_CONNECTION_TIMED_OUT")
        png_with_tag(SRC_HEAT if target.key == "heat" else SRC_OV, out_png, f"call{self.calls}")
        w, h = png_size(out_png)
        now = datetime.now()
        return {"captured_at": now, "rect": [0, 0, w, h], "size": [w, h],
                "data_time": now - timedelta(minutes=10), "page_clock": None, "notes": []}


class Heat:
    def __init__(self):
        self.down = True
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.down:
            raise TimeoutError("timed out")
        return {"st": 22.0, "pt": {"moduleTemperature": 40}}


def feed(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def build_cert(root):
    """合成已結束認證：10:00 放電 → 10:30 待機 → 10:40 充電 → 11:10 待機 → 11:20 結束。"""
    fd = os.path.join(root, CID)
    os.makedirs(os.path.join(fd, "screenshots"))
    os.makedirs(os.path.join(fd, "reports"))
    obs, t = [], datetime(2026, 11, 4, 9, 55, 0)
    while t <= datetime(2026, 11, 4, 11, 20, 0):
        if datetime(2026, 11, 4, 10, 0) <= t < datetime(2026, 11, 4, 10, 30):
            cur = -140.0
        elif datetime(2026, 11, 4, 10, 40) <= t < datetime(2026, 11, 4, 11, 10):
            cur = 140.0
        else:
            cur = 0.0
        down = t >= datetime(2026, 11, 4, 11, 0)
        obs.append({"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "hmi_ok": True,
                    "values": {"soc": 50, "current_a": cur, "power_kw": cur, "rack_max_c": 30, "rack_min_c": 25,
                               "cell_vmax_v": 3.4, "cell_vmin_v": 3.38, "voltage_v": 1250.0,
                               "sensor_c": None if down else "-", "pcs_module_c": None if down else "40"},
                    "missing": [], "fail_reason": "",
                    "notes": ["6161 讀取失敗：URLError: <urlopen error timed out>"] if down else [], "flags": {}})
        t += timedelta(seconds=5)
    with open(os.path.join(fd, "observations.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in obs)
    with open(os.path.join(fd, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in ({"t": "2026-11-04T09:55:00", "type": "start", "detail": "開始認證"},
                  {"t": "2026-11-04T11:20:00", "type": "end", "detail": "結束認證"},
                  {"t": "2026-11-04T11:21:00", "type": "report", "detail": f"產生報告 認證測試報告_{CID}_01.xlsx"},
                  {"t": "2026-11-04T11:25:00", "type": "report", "detail": f"產生報告 認證測試報告_{CID}_02.xlsx"}):
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    REQ.atomic_write_json(os.path.join(fd, "cert_state.json"), {
        "cert_id": CID, "status": "ended", "started_at": "2026-11-04T09:55:00", "ended_at": "2026-11-04T11:20:00",
        "test_info": {"cabinet_spec": "250KW", "cycles": 1}, "interruptions": [],
        "reports": [{"file": f"reports\\認證測試報告_{CID}_01.xlsx", "at": "2026-11-04 11:21:00"},
                    {"file": f"reports\\認證測試報告_{CID}_02.xlsx", "at": "2026-11-04 11:25:00"}]})
    open(os.path.join(fd, "reports", f"認證測試報告_{CID}_01.xlsx"), "wb").write(b"original _01 placeholder")
    # 原始截圖：第 1 階段兩張成功；最後階段即時熱度失敗（3 次）、數據概覽成功；結束（最後狀態）熱度失敗
    rt0 = CertRuntime(root, reader=None, enable_shots=False, log=lambda *a: None)
    stages = rt0._offline(fd).analysis().stages
    k1, k2 = (s.start.t.strftime("%Y-%m-%dT%H:%M:%S") for s in stages[:2])
    for name, src in (("J1_heat.png", SRC_HEAT), ("J1_overview.png", SRC_OV), ("J2_overview.png", SRC_OV)):
        shutil.copyfile(src, os.path.join(fd, "screenshots", name))
    fail3 = [{"n": i, "status": "failed", "reason": "開頁失敗：net::ERR_CONNECTION_TIMED_OUT"} for i in (1, 2, 3)]

    def ok(jid, tgt, cap):
        return {"type": "result", "job_id": jid, "target": tgt, "status": "ok", "reason": "", "attempts": [{"n": 1}],
                "file": f"screenshots\\{jid}_{tgt}.png", "captured_at": cap, "data_time": cap, "notes": []}

    def bad(jid, tgt):
        return {"type": "result", "job_id": jid, "target": tgt, "status": "failed",
                "reason": "開頁失敗：net::ERR_CONNECTION_TIMED_OUT", "attempts": fail3, "file": None}
    man = [{"type": "job", "job_id": "J1", "kind": "candidate", "stage_no": 1, "stage_start": k1, "stage_name": "第1次放電",
            "reason": "B", "trigger_time": "2026-11-04T10:30:00", "requested_at": "2026-11-04T10:30:00",
            "targets": ["overview", "heat"], "ref_job": None, "extra": {"b_time": "2026-11-04T10:30:00"}},
           ok("J1", "heat", "2026-11-04T10:30:04"), ok("J1", "overview", "2026-11-04T10:30:05"),
           {"type": "resolve", "job_id": "J1", "as": "stage_end", "reason": "階段結束", "at": "2026-11-04T10:31:00"},
           {"type": "job", "job_id": "J2", "kind": "candidate", "stage_no": 2, "stage_start": k2, "stage_name": "第1次充電",
            "reason": "B", "trigger_time": "2026-11-04T11:10:00", "requested_at": "2026-11-04T11:10:00",
            "targets": ["overview", "heat"], "ref_job": None, "extra": {"b_time": "2026-11-04T11:10:00"}},
           ok("J2", "overview", "2026-11-04T11:10:05"), bad("J2", "heat"),
           {"type": "resolve", "job_id": "J2", "as": "stage_end", "reason": "階段結束", "at": "2026-11-04T11:11:00"},
           {"type": "job", "job_id": "J3", "kind": "cert_final", "stage_no": None, "stage_start": None,
            "stage_name": "認證結束（最後狀態）", "reason": "結束", "trigger_time": "2026-11-04T11:20:00",
            "requested_at": "2026-11-04T11:20:00", "targets": ["heat"], "ref_job": None, "extra": {}},
           bad("J3", "heat")]
    with open(os.path.join(fd, "screenshots.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in man)
    return fd, k1, k2


def snapshot(fd):
    """原始 Historical Evidence：認證資料夾第一層檔案＋原始 screenshots／reports（supplements 不算）。"""
    out = {}
    for p in glob.glob(os.path.join(fd, "*")) + glob.glob(os.path.join(fd, "screenshots", "*")):
        if os.path.isfile(p):
            out[os.path.relpath(p, fd)] = hashlib.md5(open(p, "rb").read()).hexdigest()
    p = os.path.join(fd, "reports", f"認證測試報告_{CID}_01.xlsx")
    out["reports/_01"] = hashlib.md5(open(p, "rb").read()).hexdigest()
    return out


def heat_view(xlsx):
    """即時熱度頁：所有文字＋嵌入圖片（位置 → md5）。"""
    wb = load_workbook(xlsx)
    ws = wb["即時熱度"]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value]
    z = zipfile.ZipFile(xlsx)
    names = re.findall(r'<sheet [^>]*name="([^"]+)"[^>]*r:id="(rId\d+)"', z.read("xl/workbook.xml").decode())
    idx = [n for n, _ in names].index("即時熱度") + 1
    rels = z.read(f"xl/worksheets/_rels/sheet{idx}.xml.rels").decode()
    drawing = re.search(r'Target="/?(?:xl/)?(?:\.\./)?drawings/(drawing\d+\.xml)"', rels).group(1)
    dx = z.read(f"xl/drawings/{drawing}").decode()
    drels = z.read(f"xl/drawings/_rels/{drawing}.rels").decode()
    rmap = {i: t for t, i in re.findall(r'Target="([^"]+)"[^>]*Id="(rId\d+)"', drels)}
    imgs = []
    pat = r"<(?:xdr:)?from><(?:xdr:)?col>(\d+)</(?:xdr:)?col>.*?<(?:xdr:)?row>(\d+)</(?:xdr:)?row>.*?r:embed=\"(rId\d+)\""
    for col, row, rid in re.findall(pat, dx, re.S):
        tgt = rmap[rid].lstrip("/")
        tgt = tgt if tgt.startswith("xl/") else "xl/" + tgt.replace("../", "")
        imgs.append((int(col), int(row), hashlib.md5(z.read(tgt)).hexdigest()))     # (欄, 列（0 起）, 圖片內容)
    return texts, sorted(imgs)


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


TMP = tempfile.mkdtemp(prefix="cert_supp_")
try:
    root = os.path.join(TMP, "root")
    fd, k1, k2 = build_cert(root)
    ev0 = snapshot(fd)
    heat, cap = Heat(), FakeCap()

    def runtime():
        return CertRuntime(root, reader=None, heat_reader=heat, shot_params=SP, capturer=cap, enable_shots=True,
                           log=lambda *a: None)
    rt = runtime()

    print("== Menu 24 ==")
    # 原有功能：有進行中的認證 → 立即截圖（manual_shot），不出現事後補拍流程
    root_a = os.path.join(TMP, "active")
    os.makedirs(os.path.join(root_a, "CERT_20261104_120000"))
    REQ.atomic_write_json(os.path.join(root_a, "CERT_20261104_120000", "cert_state.json"),
                          {"cert_id": "CERT_20261104_120000", "status": "recording"})
    REQ.atomic_write_json(os.path.join(root_a, "active.json"), {"cert_id": "CERT_20261104_120000"})
    out = []
    UI(root_a, inp=feed(["現場要求"]), out=out.append, wait_sec=0).shot()
    reqs = REQ.pending_requests(root_a)
    check("1 有進行中認證 → Menu 24 照舊送出「立即截圖」（manual_shot、同一份認證、只問原因），不出現事後補拍",
          len(reqs) == 1 and reqs[0]["type"] == "manual_shot" and reqs[0]["cert_id"] == "CERT_20261104_120000"
          and reqs[0]["payload"] == {"reason": "現場要求"} and not any("事後補拍" in o for o in out))

    out = []
    UI(root, inp=feed([""]), out=out.append, wait_sec=0).shot()
    check("2 沒有進行中認證 → 先顯示事後補拍說明與提醒；Enter 取消 → 不送請求",
          any("不代表階段結束時的設備狀態" in o for o in out) and REQ.pending_requests(root) == [])

    out = []
    ui = UI(root, inp=feed(["Y", "1", "1", "no"]), out=out.append, wait_sec=0)
    ui.shot()
    rows = ui.supplement_rows(CID)
    joined = "\n".join(out)
    check("3 已結束認證：只列原始即時熱度截圖失敗的階段（第1次充電），成功的第1次放電不列入；"
          "顯示失敗原因、階段結束時間與提醒；不是 YES → 不送出",
          [r["stage_start"] for r in rows] == [k2] and "第1次充電" in joined and "1. 階段 2" in joined
          and "ERR_CONNECTION_TIMED_OUT" in joined and "階段結束時間：" in joined and "11:10:00" in joined
          and "不代表階段結束時的設備狀態" in joined and REQ.pending_requests(root) == [])

    print("\n== 補拍執行（服務端）==")
    UI(root, inp=feed(["Y", "1", "1", "YES"]), out=lambda *a: None, wait_sec=0).shot()
    reqs = REQ.pending_requests(root)
    check("4 輸入 YES → 送出 post_test_supplement（指定認證與階段）",
          len(reqs) == 1 and reqs[0]["type"] == "post_test_supplement" and reqs[0]["cert_id"] == CID
          and reqs[0]["payload"]["stage_start"] == k2)
    rt.process_requests()                                       # 6161 未恢復
    res_down = REQ.read_result(root, reqs[0]["request_id"])
    supp = SUP.load(fd)
    sdir = os.path.join(fd, SUP.SUBDIR, "screenshots")
    check("5 6161 未恢復連線 → 不開瀏覽器、不產生圖片；記一筆補拍失敗（原因：6161 尚未恢復連線）",
          res_down["status"] == "failed" and "6161 尚未恢復連線" in res_down["message"] and cap.calls == 0
          and os.listdir(sdir) == [] and [r["type"] for r in supp] == ["job", "result"]
          and supp[1]["status"] == "failed" and supp[1]["file"] is None and "6161 尚未恢復連線" in supp[1]["reason"])

    heat.down = False
    jids = []
    for _ in range(2):                                          # 同一階段補拍兩次
        rq = REQ.new_request(root, "post_test_supplement", {"stage_start": k2, "reason": "測試"}, cert_id=CID)
        rt.process_requests()
        rt._supp_workers[fd].wait_idle(30)
        jids.append(REQ.read_result(root, rq)["data"]["job_id"])
    supp = SUP.load(fd)
    jobs = {r["job_id"]: r for r in supp if r["type"] == "job"}
    resl = {r["job_id"]: r for r in supp if r["type"] == "result"}
    j1 = jobs[jids[0]]
    ex = j1["extra"]
    files = sorted(os.listdir(sdir))
    check("6 補拍成功：只拍即時熱度；metadata 含 Cert ID、Stage ID、原始階段結束時間、原始失敗 Job ID、補拍 Job ID、"
          "類型與來源；結果含實際 PC 拍攝時間與頁面時間（標註瀏覽器時間）",
          j1["kind"] == "post_test_supplement" and j1["targets"] == ["heat"] and ex["cert_id"] == CID
          and ex["stage_start"] == k2 and ex["stage_no"] == 2 and ex["orig_job_id"] == "J2" and j1["ref_job"] == "J2"
          and ex["stage_end"] == "2026-11-04T11:10:00" and "11:10:00" in ex["stage_end_note"]
          and ex["source"] == SUP.SOURCE and ex["page_time_note"] == "瀏覽器時間，非設備量測時間"
          and resl[jids[0]]["status"] == "ok" and resl[jids[0]].get("captured_at") and resl[jids[0]].get("data_time"))
    check("7 事後補拍不套用新鮮度判定：頁面時間落後 10 分鐘仍記為成功（只記錄落後秒數）",
          resl[jids[0]]["status"] == "ok" and resl[jids[0]]["data_lag_sec"] >= 590)
    first_md5 = md5(os.path.join(fd, SUP.SUBDIR, resl[jids[0]]["file"]))
    check(f"8 同一階段重複補拍：Job ID 各自獨立、照片不覆寫（{len(files)} 張）",
          len(set(jids)) == 2 and len(files) == 2 and all(f"{j}_heat.png" in files for j in jids)
          and md5(os.path.join(fd, SUP.SUBDIR, resl[jids[1]]["file"])) != first_md5)
    sel_job, sel_res, n = SUP.select(supp, k2, "heat")
    check("9 選圖規則：採用最早一次成功補拍（不受之後補拍或失敗紀錄影響）",
          sel_job["job_id"] == jids[0] and n == 3)
    check("10 原始截圖判定不受補拍影響：原始紀錄仍是失敗（補拍不會被選成原始截圖）",
          select_stage_shot(load_manifest(fd), k2, "heat")[1]["status"] == "failed")

    rej = {}
    for name, pl, cid in (("ok_stage", {"stage_start": k1}, CID), ("no_stage", {"stage_start": "2026-01-01T00:00:00"}, CID),
                          ("no_cert", {"stage_start": k2}, "CERT_19990101_000000")):
        rq = REQ.new_request(root, "post_test_supplement", pl, cert_id=cid)
        rt.process_requests()
        rej[name] = REQ.read_result(root, rq)
    check("11 拒絕：原始截圖成功的階段、不存在的階段、不存在的認證",
          all(r["status"] == "rejected" for r in rej.values()) and "已成功" in rej["ok_stage"]["message"])

    print("\n== 修訂報告（Menu 25）==")
    rq = REQ.new_request(root, "regenerate", cert_id=CID)
    rt.process_requests()
    r3 = REQ.read_result(root, rq)
    p3 = r3["data"]["report"]
    check("12 修訂報告編號 _03：_01 仍在、_02 已刪除但有紀錄 → 不重用 _02",
          r3["status"] == "done" and os.path.basename(p3) == f"認證測試報告_{CID}_03.xlsx")
    texts, imgs = heat_view(p3)
    sel_md5 = first_md5
    orig1_md5 = md5(os.path.join(fd, "screenshots", "J1_heat.png"))
    orig_fail = select_stage_shot(load_manifest(fd), k2, "heat")[1]
    want_reason = f"原始截圖失敗原因：{orig_fail['reason']}"
    check("13 有成功補拍 → Excel 不顯示原本整段失敗紀錄（【截圖失敗】、重試次數、6161 起點都不顯示）；"
          "原始失敗紀錄仍完整保存在 screenshots.jsonl",
          not any(k in t for t in texts for k in ("【截圖失敗】", "嘗試 3 次", "6161 連續讀取失敗", "不使用範本"))
          and orig_fail["status"] == "failed" and len(orig_fail["attempts"]) == 3 and orig_fail["job_id"] == "J2")
    cap_t = datetime.fromisoformat(resl[jids[0]]["captured_at"]).strftime("%Y-%m-%d %H:%M:%S")
    want_label = f"事後補拍：{cap_t}（非階段結束畫面）"
    supp_txt = [t for t in texts if "事後補拍" in t or "原始截圖失敗原因" in t]
    print(f"    ▸ 標示：{supp_txt}")
    check(f"14 只有兩行紅字：補拍時間（supplements captured_at＝{cap_t}）＋原始失敗原因（取自原始截圖紀錄，"
          "不是補拍時的「6161 尚未恢復連線」）；沒有黃底說明；補拍 metadata 完整保存",
          supp_txt == [want_label, want_reason] and "ERR_CONNECTION_TIMED_OUT" in want_reason
          and not any("6161 尚未恢復連線" in t for t in texts) and not any("瀏覽器時間" in t for t in texts)
          and any("6161 尚未恢復連線" in (r.get("reason") or "") for r in supp if r["type"] == "result")
          and j1["kind"] == "post_test_supplement" and resl[jids[0]].get("captured_at")
          and j1["extra"]["stage_end_note"] and j1["extra"]["orig_job_id"] == "J2")
    ws3 = load_workbook(p3)["即時熱度"]
    col = imgs[1][0] + 1 if len(imgs) == 2 else 1
    l1, l2 = ws3.cell(3, col), ws3.cell(4, col)
    check("15 圖片：第1次放電原始截圖不變；第1次充電：第 3 列補拍時間 → 第 4 列原始失敗原因 → 第 5 列直接接補拍圖片"
          "（紅字、無底色；與其他階段圖片頂端對齊；沒有預留框或空白）",
          len(imgs) == 2 and imgs[0][2] == orig1_md5 and imgs[1][2] == sel_md5 and imgs[0][0] < imgs[1][0]
          and l1.value == want_label and l2.value == want_reason
          and all(x.font.color.rgb.endswith("C00000") and x.fill.fgColor.rgb in (None, "00000000") for x in (l1, l2))
          and imgs[1][1] == 4 and imgs[0][1] == 4
          and not any(m.min_row >= 3 and m.max_row > m.min_row for m in ws3.merged_cells.ranges))
    ov3 = [str(c.value) for row in load_workbook(p3)["數據概覽"].iter_rows() for c in row if c.value]
    check("16 循環數 1（循環測試）→ 即時熱度、數據概覽都不顯示「認證結束（最後狀態）」；原始最後狀態紀錄仍在 screenshots.jsonl",
          not any("認證結束（最後狀態）" in t for t in texts + ov3)
          and any(r.get("kind") == "cert_final" for r in load_manifest(fd)))
    log = SUP.load_report_log(fd)
    check("17 修訂報告紀錄寫 supplements/reports.jsonl；原始 cert_state.json／events.jsonl 不寫",
          [os.path.basename(r["file"]) for r in log] == [f"認證測試報告_{CID}_03.xlsx"])

    # 再補拍一次（之後的）→ 重建仍用同一張；新 Runtime（服務重啟）＋離線重建
    rq = REQ.new_request(root, "post_test_supplement", {"stage_start": k2}, cert_id=CID)
    rt.process_requests()
    rt._supp_workers[fd].wait_idle(30)
    rt2 = runtime()
    ok4, _m, p4 = rt2.generate_report(CID)
    t4, i4 = heat_view(p4)
    check("18 Menu 25 重建（服務重啟後、之後又補拍）：編號 _04、採用圖片、位置與文字完全一致",
          ok4 and os.path.basename(p4) == f"認證測試報告_{CID}_04.xlsx" and i4 == imgs and t4 == texts)
    shutil.copy(os.path.join(fd, SUP.SUBDIR, resl[jids[0]]["file"]), os.path.join(TMP, "keep.png"))
    os.remove(os.path.join(fd, SUP.SUBDIR, resl[jids[0]]["file"]))
    ok5, _m, p5 = rt2.generate_report(CID)
    t5, i5 = heat_view(p5)
    check("19 被採用的補拍檔案遺失 → 不放圖、不改用其他補拍（只剩第1次放電原圖；失敗紀錄照舊）",
          ok5 and len(i5) == 1 and i5[0][2] == orig1_md5 and any("【截圖失敗】" in t and "第1次充電" in t for t in t5))
    shutil.copy(os.path.join(TMP, "keep.png"), os.path.join(fd, SUP.SUBDIR, resl[jids[0]]["file"]))
    check("20 原始 Historical Evidence 內容完全不變（observations／screenshots.jsonl／events／cert_state／原始 PNG／_01）",
          snapshot(fd) == ev0)

    print("\n== 補拍失敗 ==")
    root_f = os.path.join(TMP, "fail")
    fd_f, _k1, k2f = build_cert(root_f)
    cap.fail = True
    rtf = CertRuntime(root_f, reader=None, heat_reader=heat, shot_params=SP, capturer=cap, enable_shots=True,
                      log=lambda *a: None)
    rq = REQ.new_request(root_f, "post_test_supplement", {"stage_start": k2f}, cert_id=CID)
    rtf.process_requests()
    rtf._supp_workers[fd_f].wait_idle(30)
    sf = [r for r in SUP.load(fd_f) if r["type"] == "result"]
    okf, _m, pf = rtf.generate_report(CID)
    tf, imf = heat_view(pf)
    check("21 補拍失敗（6161 已連線但開頁失敗，重試 2 次）→ 不產生圖片、失敗原因記在 supplements；"
          "報告只有原始失敗紀錄，不放舊圖、不放其他圖代替",
          len(sf) == 1 and sf[0]["status"] == "failed" and len(sf[0]["attempts"]) == 2
          and "ERR_CONNECTION_TIMED_OUT" in sf[0]["reason"]
          and os.listdir(os.path.join(fd_f, SUP.SUBDIR, "screenshots")) == []
          and len(imf) == 1 and any("【截圖失敗】" in t and "第1次充電" in t for t in tf))

    print("\n== 沒有補拍資料的認證（相容）==")
    root_n = os.path.join(TMP, "none")
    fd_n, _a, _b = build_cert(root_n)
    rtn = CertRuntime(root_n, reader=None, enable_shots=False, log=lambda *a: None)
    okn, _m, pn = rtn.generate_report(CID)
    st_n = REQ.read_json(os.path.join(fd_n, "cert_state.json"))
    tn, imn = heat_view(pn)
    check("22 沒有補拍的認證：Menu 25 行為不變（記錄寫 cert_state／events），編號同樣不重用 _02；不顯示補拍資訊",
          okn and os.path.basename(pn) == f"認證測試報告_{CID}_03.xlsx"
          and st_n["reports"][-1]["file"].endswith("_03.xlsx") and not os.path.exists(os.path.join(fd_n, SUP.SUBDIR))
          and not any("事後補拍" in t for t in tn) and len(imn) == 1)

    print("\n== 循環測試判斷 ==")

    def final_shown(xlsx):
        wb = load_workbook(xlsx)
        return {n: any("認證結束（最後狀態）" in str(c.value) for row in wb[n].iter_rows() for c in row if c.value)
                for n in ("數據概覽", "即時熱度")}
    stf = os.path.join(fd_n, "cert_state.json")
    st_ = REQ.read_json(stf)
    st_["test_info"]["cycles"] = None                       # 舊認證：沒填循環數
    REQ.atomic_write_json(stf, st_)
    ok6, _m, p6 = rtn.generate_report(CID)
    t6, _i6 = heat_view(p6)
    check("23 沒填循環數 → 兩個截圖頁維持原本版面（顯示「認證結束（最後狀態）」與其失敗紀錄）",
          ok6 and final_shown(p6) == {"數據概覽": True, "即時熱度": True}
          and any("認證結束（最後狀態）" in t and "【截圖失敗】" in t for t in t6))
    st_["test_info"]["cycles"] = 3                          # 設定 3 個循環，實際只有 1 次放電＋1 次充電
    REQ.atomic_write_json(stf, st_)
    ok7, _m, p7 = rtn.generate_report(CID)
    t7, i7 = heat_view(p7)
    check("24 循環數 3、實際階段未完成（只有 2 個階段）→ 仍判為循環測試，兩頁都不顯示最後狀態；兩個階段完整保留",
          ok7 and final_shown(p7) == {"數據概覽": False, "即時熱度": False}
          and [c.value for c in load_workbook(p7)["即時熱度"][2] if c.value] == ["獨立櫃Pack 即時熱度圖"] * 2
          and len(i7) == 1)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== 已結束認證事後補拍 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
