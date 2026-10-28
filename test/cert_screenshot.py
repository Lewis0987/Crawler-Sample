# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— P2 自動截圖（唯讀）
==========================================
只做：開頁、等待、讀取 DOM、截圖。**不點擊、不輸入、不寫入頁面儲存、不操作設備。**

目標（範圍沿用 2026-10-27 已確認成品）：
  - overview：HMI 數據概覽 → 「Battery Overview（電池概覽）」卡片（HMI 目前為英文畫面）
  - heat    ：即時熱度 → 「獨立櫃:110」完整區塊（名稱、更新時間、Pack 1～14）；
              櫃號、Pack 編號集合、必要欄位與允許缺值欄位見 cert_report_config.HEAT_*

截圖方式：headless Chrome ＋ CDP（Chrome DevTools Protocol）直連，依元素文字定位後
Page.captureScreenshot(clip, captureBeyondViewport)。不使用 chromedriver（版本與 Chrome 不符）。
依賴：websocket-client（已安裝）；不需要 Pillow。

執行模型：
  ScreenshotWorker.submit() 立即回傳 job_id，截圖在背景執行緒進行，不阻塞資料收集。
  每個目標各自有限重試；單次嘗試有內部逾時與外部看門狗。
  所有結果 append-only 寫入 <資料夾>/screenshots.jsonl；候選截圖之後的判定（階段結束／停頓點）
  以新的 resolve 紀錄表示，不改寫原紀錄。

截圖類型：
  candidate 候選截圖：首筆觀測待機（B）時先拍，尚未確定是階段結束或停頓
  stage_end 階段結束截圖：停止已確認時拍；或候選被判定為階段結束（resolve）
  pause     停頓點截圖：候選被判定為停頓（60 秒內恢復同向）→ 只留在資料夾，不放入報告
  manual    手動補拍：操作人員要求
  delayed   延遲補拍：原截圖失敗後補拍；**不是停止當下的畫面**，報告如實標示
"""

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import cert_report_config as CFG

KIND_TEXT = {"candidate": "候選截圖", "stage_end": "階段結束截圖", "pause": "停頓點截圖",
             "manual": "手動補拍", "delayed": "延遲補拍"}
STATUS_TEXT = {"ok": "成功", "stale": "資料未更新", "failed": "失敗"}

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

DT_RE = r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)"


@dataclass
class Target:
    key: str
    name: str                       # 報告顯示名稱
    url: str
    must: list                      # 區塊必須同時包含的文字（定位用：取符合條件的最小元素）
    mustnot: list                   # 區塊不可包含的文字（避免截到過大範圍）
    ready: list = field(default_factory=list)       # [(regex, 說明)]：區塊內數值已載入（非「-」或空白）
    size_min: tuple = (0, 0)        # 合理尺寸（px）：用來偵測擷取範圍錯誤
    size_max: tuple = (10 ** 5, 10 ** 5)
    data_time_re: str = ""          # 區塊內「資料更新時間」regex（有才做資料未更新判斷）
    page_clock: bool = False        # 是否從整頁文字讀取頁面時鐘（僅記錄，不判斷資料新舊）
    extra_check: object = None      # 額外檢查 fn(text) -> (錯誤訊息或 "", [註記])；錯誤訊息須含完整分類前綴


# =============================================================================
# 即時熱度區塊驗證（範圍＋資料有效性）
# =============================================================================
NUM_RE = r"-?\d+(?:\.\d+)?"
PLACEHOLDER_PACKS = tuple(range(1, 20))     # 頁面在 API 無資料時的預設版面（頁面原始碼：Array.from({length: 19})）


def parse_heat(text):
    """把獨立櫃區塊的 innerText 拆成：櫃號、欄位值、各 Pack 的 Cell 溫度（依頁面出現順序）。"""
    lines = [ln.strip() for ln in text.split("\n")]
    cabinets = re.findall(r"獨立櫃[:：]\s*(\d+)", text)
    fields = {}
    for i, ln in enumerate(lines[:-1]):
        if ln in CFG.HEAT_REQUIRED_FIELDS + CFG.HEAT_OPTIONAL_FIELDS and ln not in fields:
            fields[ln] = lines[i + 1]
    packs = []                              # [(pack_id, [cell 文字…])]
    i = 0
    while i < len(lines):
        m = re.fullmatch(r"Pack\s*(\d+)", lines[i])
        if not m:
            i += 1
            continue
        cells, i = [], i + 1
        while i < len(lines) and lines[i] != "+" and not re.fullmatch(r"Pack\s*\d+", lines[i]):
            cells.append(lines[i])
            i += 1
        packs.append((int(m.group(1)), cells))
    return cabinets, fields, packs


def heat_validate(text, cabinet=None, pack_ids=None, cells_per_pack=None, max_missing=None):
    """
    回傳 (錯誤訊息或 "", [註記])。依序檢查：
      1. 範圍：區塊內只有目標櫃；Pack 編號集合與設定完全相同（缺漏、重複、非預期都不行）；每 Pack 格數正確
      2. 資料：必要欄位（Cell 極值）有數值；Cell 溫度缺值不超過上限
    允許缺值欄位（例 Sensor）顯示「-」只記為註記，不拒絕。
    """
    cabinet = str(cabinet or CFG.HEAT_CABINET_NO)
    want = tuple(pack_ids or CFG.HEAT_PACK_IDS)
    n_cells = cells_per_pack or CFG.HEAT_CELLS_PER_PACK
    max_missing = CFG.HEAT_MAX_MISSING_CELLS if max_missing is None else max_missing
    cabinets, fields, packs = parse_heat(text)

    if cabinets != [cabinet]:
        other = [c for c in cabinets if c != cabinet]
        return (f"擷取範圍不符：區塊應只屬於獨立櫃 {cabinet}，實際為 "
                + ("、".join(f"獨立櫃 {c}" for c in cabinets) if cabinets else "找不到櫃號")
                + (f"（包含其他櫃 {'、'.join(other)}）" if other and cabinet in cabinets else ""), [])

    ids = [pid for pid, _ in packs]
    missing = sorted(set(want) - set(ids))
    extra = sorted(set(ids) - set(want))
    dup = sorted({pid for pid in ids if ids.count(pid) > 1})
    if missing or extra or dup:
        parts = ([f"缺少 Pack {_ids(missing)}"] if missing else []) + \
                ([f"非預期 Pack {_ids(extra)}"] if extra else []) + \
                ([f"重複 Pack {_ids(dup)}"] if dup else [])
        msg = (f"擷取範圍不符：Pack 編號與設定不符（設定 {len(want)} 個：{_ids(want)}；"
               f"頁面 {len(ids)} 個）：" + "；".join(parts))
        if tuple(sorted(ids)) == PLACEHOLDER_PACKS and all(c == "-" for _, cs in packs for c in cs):
            msg += "。與頁面無資料時的 19 Pack 預設版面相符（全部「-」）"
        return msg, []
    bad_len = [f"Pack {pid}：{len(cs)} 格" for pid, cs in packs if len(cs) != n_cells]
    if bad_len:
        return f"擷取範圍不符：每個 Pack 應有 {n_cells} 格 Cell 溫度（{'；'.join(bad_len)}）", []

    no_val = [f for f in CFG.HEAT_REQUIRED_FIELDS if not re.fullmatch(NUM_RE, fields.get(f, ""))]
    blank = {pid: sum(1 for c in cs if not re.fullmatch(NUM_RE, c)) for pid, cs in packs}
    total = sum(blank.values())
    if total == len(packs) * n_cells:
        return "頁面資料無效：所有 Cell 溫度皆為「-」" + (
            f"，Cell 極值（{'、'.join(no_val)}）無數值" if no_val else ""), []
    if no_val:
        return f"頁面資料無效：必要欄位無數值（{'、'.join(f'{f}「{fields.get(f, '（缺）')}」' for f in no_val)}）", []
    if total > max_missing:
        detail = "；".join(f"Pack {pid}：{n} 格" for pid, n in blank.items() if n)
        return f"頁面資料無效：Cell 溫度缺值 {total} 格，超過上限 {max_missing} 格（{detail}）", []

    notes = [f"{f} 顯示「{fields[f]}」（允許缺值欄位，不影響判定）" for f in CFG.HEAT_OPTIONAL_FIELDS
             if f in fields and not re.search(r"\d", fields[f])]
    if total:
        notes.append(f"Cell 溫度缺值 {total} 格（上限 {max_missing} 格內）")
    return "", notes


def _ids(ids):
    """[1,2,3,5] → '1～3、5'"""
    ids, out, i = sorted(ids), [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        out.append(str(ids[i]) if i == j else f"{ids[i]}～{ids[j]}")
        i = j + 1
    return "、".join(out)


def _check_heat(text):
    return heat_validate(text)


TARGETS = {
    "overview": Target(
        key="overview", name="數據概覽－電池概覽",
        url="http://192.168.128.110:8853/data-overview",
        must=["Battery Overview", "SOC", "SOH", "Rack Capacity Information",
              "Rack Extreme Value Information", "Minimum SOH"],
        mustnot=["PCS Overview", "Environmental Control Overview"],
        ready=[(r"Voltage\s*[\d.,]+\s*V", "Voltage 無數值"), (r"Current\s*-?[\d.,]+\s*A", "Current 無數值"),
               (r"Minimum SOH\s*[\d.]+\s*%", "Rack 極值無數值")],
        size_min=(800, 300), size_max=(1600, 800),
        page_clock=True),
    "heat": Target(
        key="heat", name="即時熱度－獨立櫃 110",
        url="http://192.168.70.201:6161/",
        # 定位只靠櫃名＋固定標籤（最小的共同元素就是整個櫃區塊）；櫃號、Pack 集合與資料有效性由 heat_validate 判定
        must=[f"獨立櫃:{CFG.HEAT_CABINET_NO}", "更新時間", "Cell極值"],
        mustnot=[],
        ready=[(r"更新時間[:：]\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "沒有更新時間"),
               (r"最大溫度\s*\d+", "Cell 極值顯示「-」，頁面沒有資料")],
        size_min=(400, 1200), size_max=(900, 3000),      # 只作粗略防呆，不作為 Pack 判定依據
        data_time_re=r"更新時間[:：]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
        extra_check=_check_heat),
}


def shot_params(**override):
    p = {k: getattr(CFG, k) for k in dir(CFG) if k.startswith("SHOT_")}
    p.update(override)
    return p


# =============================================================================
# PNG 尺寸（不需要 Pillow）
# =============================================================================
def png_size(path_or_bytes):
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read(32)
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("不是 PNG 檔")
    return struct.unpack(">II", data[16:24])


# =============================================================================
# CDP 截圖（一次嘗試）
# =============================================================================
FIND_JS = r"""
(function(must, mustnot, wantPage){
  const all=[...document.querySelectorAll('body *')];
  let best=null, bestArea=Infinity;
  for(const el of all){
    const t=el.innerText||''; if(!t) continue;
    if(!must.every(m=>t.includes(m))) continue;
    if(mustnot.some(m=>t.includes(m))) continue;
    const r=el.getBoundingClientRect(); const a=r.width*r.height;
    if(a>0 && a<bestArea){best=el;bestArea=a;}
  }
  const page = wantPage ? (document.body.innerText||'').slice(0,20000) : '';
  if(!best) return {found:false, href:location.href, page:page,
                    pageHasMust: must.map(m=>(document.body.innerText||'').includes(m))};
  const r=best.getBoundingClientRect();
  return {found:true, href:location.href, x:r.left+scrollX, y:r.top+scrollY, w:r.width, h:r.height,
          text:best.innerText||'', page:page};
})(%s,%s,%s)
"""


class ShotError(Exception):
    pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _chrome_path():
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    raise ShotError("找不到 Chrome 執行檔")


def _parse_dt(s):
    s = re.sub(r"\s+", " ", s.strip()).replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def cdp_capture(target, out_png, p, cancel=None):
    """
    單次嘗試。成功回傳 dict（captured_at, data_time, page_clock, rect, size）；失敗丟 ShotError（中文原因）。
    只讀取：Page.navigate、Runtime.evaluate（讀 DOM）、Page.captureScreenshot。
    """
    import websocket
    deadline = time.monotonic() + p["SHOT_PAGE_TIMEOUT_SEC"]
    port = _free_port()
    prof = tempfile.mkdtemp(prefix="cert_shot_")
    vw, vh = p["SHOT_VIEWPORT"]
    proc = subprocess.Popen(
        [_chrome_path(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
         f"--window-size={vw},{vh}", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
         "--disable-sync", "--disable-background-networking", "--hide-scrollbars", "--mute-audio",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if cancel is not None:
        cancel.append(proc)                 # 看門狗逾時時可直接結束 Chrome
    ws = None
    try:
        tabs = None
        while time.monotonic() < deadline:
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1))
                break
            except Exception:
                time.sleep(0.2)
        if not tabs:
            raise ShotError("Chrome 啟動逾時")
        page = [t for t in tabs if t.get("type") == "page"][0]
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)
        mid = [0]

        def cmd(method, **params):
            mid[0] += 1
            my = mid[0]
            ws.send(json.dumps({"id": my, "method": method, "params": params}))
            while True:
                if time.monotonic() > deadline:
                    raise ShotError(f"等待逾時（{p['SHOT_PAGE_TIMEOUT_SEC']:g} 秒）：{method} 未回應"
                                    + ("（頁面無法連線或回應過慢）" if method == "Page.navigate" else ""))
                ws.settimeout(max(0.5, min(5.0, deadline - time.monotonic())))
                try:
                    m = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if m.get("id") == my:
                    if "error" in m:
                        raise ShotError(f"CDP 錯誤（{method}）：{m['error'].get('message')}")
                    return m.get("result", {})

        cmd("Emulation.setDeviceMetricsOverride", width=vw, height=vh, deviceScaleFactor=1, mobile=False)
        nav = cmd("Page.navigate", url=target.url)
        if nav.get("errorText"):
            raise ShotError(f"開頁失敗：{nav['errorText']}")

        expr = FIND_JS % (json.dumps(target.must, ensure_ascii=False),
                          json.dumps(target.mustnot, ensure_ascii=False),
                          "true" if target.page_clock else "false")
        last = "區塊未出現"
        box = None
        while True:
            r = cmd("Runtime.evaluate", expression=expr, returnByValue=True).get("result", {}).get("value")
            if r and r.get("found"):
                text = r["text"]
                miss = [desc for rx, desc in target.ready if not re.search(rx, text)]
                if not miss:
                    box = r
                    break
                last = f"區塊數值未載入完成（{miss[0]}）"
                if target.extra_check:          # 一併說明區塊內容（例：無資料時的 19 Pack 預設版面）
                    err = target.extra_check(text)[0]
                    if err:
                        last += f"；{err}"
            elif r:
                if not r["href"].startswith(target.url.split("#")[0].rstrip("/")):
                    last = f"頁面被導向 {r['href']}"
                else:
                    last = "找不到符合條件的區塊（頁面缺少：" + "、".join(
                        m for m, ok in zip(target.must, r.get("pageHasMust", [])) if not ok).replace("\n", "") + "）"
            if time.monotonic() + p["SHOT_POLL_SEC"] > deadline:
                raise ShotError(f"等待逾時（{p['SHOT_PAGE_TIMEOUT_SEC']:g} 秒）：{last}")
            time.sleep(p["SHOT_POLL_SEC"])

        time.sleep(p["SHOT_SETTLE_SEC"])
        r = cmd("Runtime.evaluate", expression=expr, returnByValue=True).get("result", {}).get("value")
        if not (r and r.get("found")):
            raise ShotError("穩定等待後區塊消失")
        box = r
        w, h = box["w"], box["h"]
        if not (target.size_min[0] <= w <= target.size_max[0] and target.size_min[1] <= h <= target.size_max[1]):
            raise ShotError(f"擷取範圍不符：區塊 {w:.0f}×{h:.0f}px，預期寬 {target.size_min[0]}～"
                            f"{target.size_max[0]}、高 {target.size_min[1]}～{target.size_max[1]}")
        notes = []
        if target.extra_check:
            err, notes = target.extra_check(box["text"])
            if err:
                raise ShotError(err)
        pad = p["SHOT_PAD_PX"]
        clip = {"x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                "width": w + 2 * pad, "height": h + 2 * pad, "scale": 1}
        shot = cmd("Page.captureScreenshot", format="png", captureBeyondViewport=True, clip=clip)
        captured_at = datetime.now()
        data = base64.b64decode(shot["data"])
        with open(out_png, "wb") as f:
            f.write(data)
        info = {"captured_at": captured_at, "rect": [round(clip[k]) for k in ("x", "y", "width", "height")],
                "size": list(png_size(data)), "data_time": None, "page_clock": None, "notes": notes}
        if target.data_time_re:
            m = re.search(target.data_time_re, box["text"])
            info["data_time"] = _parse_dt(m.group(1)) if m else None
        if target.page_clock:
            m = re.search(DT_RE, box.get("page") or "")
            info["page_clock"] = _parse_dt(m.group(1)) if m else None
        return info
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        for _ in range(5):                  # Chrome 子行程可能還鎖著檔案 → 稍候重試刪除暫存設定檔
            shutil.rmtree(prof, ignore_errors=True)
            if not os.path.exists(prof):
                break
            time.sleep(1)


# =============================================================================
# 背景工作者
# =============================================================================
def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S") if t else None


class ScreenshotWorker:
    """
    submit() 不阻塞；工作依序在背景執行緒處理，每個工作內兩個目標並行擷取。
    capturer 可替換（離線測試用假的截圖函式）。
    """

    def __init__(self, folder, params=None, capturer=cdp_capture, targets=None):
        self.folder = folder
        self.shot_dir = os.path.join(folder, "screenshots")
        os.makedirs(self.shot_dir, exist_ok=True)
        self.manifest = os.path.join(folder, "screenshots.jsonl")
        self.p = params or shot_params()
        self.capturer = capturer
        self.targets = targets or TARGETS
        self._q = []
        self._cv = threading.Condition()
        self._lock = threading.Lock()
        self._busy = False
        self._stop = False
        self._th = threading.Thread(target=self._loop, name="cert-screenshot", daemon=True)
        self._th.start()

    # ---- 紀錄（append-only）----
    def _append(self, rec):
        with self._lock:
            with open(self.manifest, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def submit(self, kind, stage_no, stage_start, stage_name, reason, trigger_time=None,
               targets=("overview", "heat"), ref_job=None, extra=None):
        assert kind in KIND_TEXT, kind
        now = datetime.now()
        job_id = f"{now:%Y%m%d_%H%M%S}_{kind}_{uuid.uuid4().hex[:6]}"
        job = {"type": "job", "job_id": job_id, "kind": kind, "stage_no": stage_no,
               "stage_start": _iso(stage_start) if isinstance(stage_start, datetime) else stage_start,
               "stage_name": stage_name, "reason": reason, "trigger_time": _iso(trigger_time),
               "requested_at": _iso(now), "targets": list(targets), "ref_job": ref_job,
               "extra": extra or {}}
        self._append(job)
        with self._cv:
            self._q.append(job)
            self._cv.notify()
        return job_id

    def mark_unfinished(self, reason):
        """
        服務重啟後呼叫：先前已登記、但某目標沒有結果紀錄的工作（行程中斷時尚未完成）
        → 補一筆失敗結果（如實寫明原因），避免報告永遠顯示「截圖尚未完成」。回傳補記筆數。
        """
        recs = load_manifest(self.folder)
        done = {(r["job_id"], r["target"]) for r in recs if r["type"] == "result"}
        n = 0
        for j in (r for r in recs if r["type"] == "job"):
            for t in j["targets"]:
                if (j["job_id"], t) not in done:
                    self._append({"type": "result", "job_id": j["job_id"], "target": t,
                                  "target_name": self.targets[t].name if t in self.targets else t,
                                  "status": "failed", "reason": reason, "attempts": [], "file": None})
                    n += 1
        return n

    def resolve(self, job_id, as_kind, reason):
        """候選截圖的後續判定（stage_end／pause）；只新增紀錄，不改寫原工作。"""
        assert as_kind in ("stage_end", "pause"), as_kind
        self._append({"type": "resolve", "job_id": job_id, "as": as_kind, "reason": reason,
                      "at": _iso(datetime.now())})

    def wait_idle(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._q or self._busy:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cv.wait(left if left is not None else 1)
        return True

    def close(self, timeout=None):
        self.wait_idle(timeout)
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    # ---- 執行 ----
    def _loop(self):
        while True:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait()
                if self._stop and not self._q:
                    return
                job = self._q.pop(0)
                self._busy = True
            try:
                ths = [threading.Thread(target=self._run_target, args=(job, t), daemon=True)
                       for t in job["targets"]]
                for th in ths:
                    th.start()
                for th in ths:
                    th.join()
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def _run_target(self, job, tkey):
        tgt = self.targets[tkey]
        p = self.p
        final_file = os.path.join(self.shot_dir, f"{job['job_id']}_{tkey}.png")
        attempts = []
        status, info, reason = "failed", None, ""
        for n in range(1, int(p["SHOT_ATTEMPTS"]) + 1):
            tmp = final_file + f".try{n}"
            started = datetime.now()
            box, procs = {}, []

            def work():
                try:
                    box["info"] = self.capturer(tgt, tmp, p, procs)
                except ShotError as e:
                    box["err"] = str(e)
                except Exception as e:  # noqa: BLE001 —— 任何例外都記錄為失敗，不中斷其他工作
                    box["err"] = f"{type(e).__name__}: {e}"
            th = threading.Thread(target=work, daemon=True)
            th.start()
            th.join(p["SHOT_ATTEMPT_HARD_TIMEOUT_SEC"])
            if th.is_alive():
                for pr in procs:
                    try:
                        pr.kill()
                    except Exception:
                        pass
                box = {"err": f"單次嘗試超過 {p['SHOT_ATTEMPT_HARD_TIMEOUT_SEC']:g} 秒（看門狗逾時，已強制結束）"}
            att = {"n": n, "started": _iso(started), "ended": _iso(datetime.now())}
            if "info" in box:
                info = box["info"]
                stale = None
                if info.get("data_time"):
                    lag = (info["captured_at"] - info["data_time"]).total_seconds()
                    info["data_lag_sec"] = lag
                    if lag > p["SHOT_STALE_SEC"]:
                        stale = f"頁面更新時間 {info['data_time']:%H:%M:%S} 落後擷取時間 {lag:.0f} 秒"
                os.replace(tmp, final_file)
                if stale:
                    att.update(status="stale", reason=stale)
                    status, reason = "stale", stale
                else:
                    att.update(status="ok")
                    status, reason = "ok", ""
                    attempts.append(att)
                    break
            else:
                att.update(status="failed", reason=box.get("err", "未知錯誤"))
                reason = att["reason"]
                if os.path.exists(tmp):
                    os.remove(tmp)
            attempts.append(att)
            if n < int(p["SHOT_ATTEMPTS"]):
                time.sleep(p["SHOT_RETRY_DELAY_SEC"])
        rec = {"type": "result", "job_id": job["job_id"], "target": tkey, "target_name": tgt.name,
               "status": status, "reason": reason, "attempts": attempts,
               "file": os.path.relpath(final_file, self.folder) if status != "failed" else None}
        if info and status != "failed":
            rec.update(captured_at=_iso(info["captured_at"]), data_time=_iso(info.get("data_time")),
                       page_clock=_iso(info.get("page_clock")), data_lag_sec=info.get("data_lag_sec"),
                       rect=info.get("rect"), size=info.get("size"), notes=info.get("notes") or [])
        self._append(rec)


# =============================================================================
# 讀取紀錄、選出各階段報告用截圖
# =============================================================================
def load_manifest(folder):
    path = os.path.join(folder, "screenshots.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


SLOT_PRIORITY = ("stage_end", "delayed", "manual")      # 報告「階段結束」位置可用的類型（依序）


def effective_jobs(records):
    """回傳 {job_id: job dict（含 effective_kind、results{target: rec}）}；resolve 以最後一筆為準。"""
    jobs = {}
    for r in records:
        if r["type"] == "job":
            jobs[r["job_id"]] = dict(r, effective_kind=r["kind"], resolved=None, results={})
    for r in records:
        if r["type"] == "resolve" and r["job_id"] in jobs:
            jobs[r["job_id"]]["effective_kind"] = r["as"]
            jobs[r["job_id"]]["resolved"] = r
        elif r["type"] == "result" and r["job_id"] in jobs:
            jobs[r["job_id"]]["results"][r["target"]] = r
    return jobs


def select_stage_shot(records, stage_start, target):
    """
    報告用：某階段、某目標的「階段結束」截圖。
    回傳 (job, result)；沒有可用圖片時 result 為最後一筆失敗紀錄或 None。
    停頓點與未判定的候選截圖永遠不會被選入。
    """
    jobs = [j for j in effective_jobs(records).values()
            if j["stage_start"] == stage_start and target in j["targets"]]
    for want in ("ok", "stale"):
        for kind in SLOT_PRIORITY:
            cands = [(j, j["results"].get(target)) for j in jobs if j["effective_kind"] == kind]
            cands = [(j, r) for j, r in cands if r and r["status"] == want]
            if cands:
                return min(cands, key=lambda jr: jr[1]["captured_at"])
    fails = [(j, j["results"][target]) for j in jobs
             if j["effective_kind"] in SLOT_PRIORITY and target in j["results"]]
    if fails:
        return max(fails, key=lambda jr: jr[0]["requested_at"])
    pending = [j for j in jobs if j["effective_kind"] in SLOT_PRIORITY + ("candidate",)
               and target not in j["results"]]
    return (pending[-1], None) if pending else (None, None)


def _fmt(t):
    return t.replace("T", " ") if t else "—"


def _dur(sec):
    sec = int(sec)
    return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"


def caption(job, res, note=""):
    """報告圖片說明（如實標示類型、擷取時間、頁面時間、補拍）。"""
    if job is None:
        return "尚未截圖"
    lines = []
    eff = job["effective_kind"]
    if res is None:
        return f"{KIND_TEXT[eff]}：截圖尚未完成"
    if res["status"] == "failed":
        tries = len(res.get("attempts", []))
        lines.append(f"【截圖失敗】{KIND_TEXT[eff]}，嘗試 {tries} 次")
        lines.append(f"原因：{res['reason']}")
        lines.append("不使用範本或其他時間的畫面代替。")
        return "\n".join(lines)
    head = KIND_TEXT[eff]
    if job["kind"] == "candidate" and eff == "stage_end":
        head += "（候選截圖於首筆觀測待機時擷取，停止確認後採用）"
    if eff in ("delayed", "manual"):
        head += "：非停止當下畫面"
    lines.append(f"{head}｜實際擷取 {_fmt(res['captured_at'])}（PC 時間）")
    trig = f"觸發：{job['reason']}"
    if job.get("trigger_time") and res.get("captured_at"):
        d = (datetime.fromisoformat(res["captured_at"]) - datetime.fromisoformat(job["trigger_time"]))
        trig += f"（觸發時間 {_fmt(job['trigger_time'])}，擷取晚 {_dur(d.total_seconds())}）"
    lines.append(trig)
    if res.get("data_time") and res.get("target") == "heat":
        lines.append(f"頁面更新時間 {_fmt(res['data_time'])}（瀏覽器取得資料的時間，非設備量測時間）")
    elif res.get("data_time"):
        lines.append(f"頁面更新時間 {_fmt(res['data_time'])}")
    elif res.get("page_clock"):
        lines.append(f"頁面時鐘 {_fmt(res['page_clock'])}（HMI 時鐘與 PC 可能有偏差，僅供參考）")
    if res["status"] == "stale":
        lines.append(f"⚠ 頁面資料未更新：{res['reason']}")
    for n in res.get("notes") or []:
        lines.append(f"註：{n}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def shot_flag(job, res):
    """
    報告圖片上方的必要狀態（單行）；正常截圖回傳 ""（詳細資訊只留在 screenshots.jsonl）。
    只顯示會影響判讀的狀態：補拍（非停止當下畫面）、資料未更新、Cell 缺值；
    允許缺值欄位（例 Sensor「-」）屬已知正常情況，不顯示。
    """
    if not res or res.get("status") not in ("ok", "stale"):
        return ""
    out = []
    eff = job["effective_kind"]
    if eff in ("delayed", "manual"):
        out.append(f"{KIND_TEXT[eff]}：非停止當下畫面（擷取 {_fmt(res.get('captured_at'))}）")
    if res["status"] == "stale":
        out.append(f"頁面資料未更新：{res['reason']}")
    out += [n for n in res.get("notes") or [] if "允許缺值欄位" not in n]
    return ("⚠ " + "；".join(out)) if out else ""
