# -*- coding: utf-8 -*-
"""
認證測試報告 P2 —— 實際頁面截圖驗證（唯讀）
==========================================
只開頁、讀 DOM、截圖；不點擊、不輸入、不操作設備。
  - 與設備狀態無關的情境（錯誤範圍、逾時、尺寸不符、背景工作串接）→ 必須 PASS
  - 正常截圖需要設備與頁面有資料；若頁面無法連線或數值未載入 → 標示「無法驗證」（不算通過）
執行：python test_cert_p2_live.py [輸出資料夾]
"""

import dataclasses
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from cert_screenshot import (TARGETS, ScreenshotWorker, ShotError, cdp_capture, load_manifest, png_size,
                             shot_params)

_results, _blocked = [], []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def blocked(label, why):
    _blocked.append(label)
    print(f"  [無法驗證] {label}：{why}")


out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="cert_p2_live_")
os.makedirs(out, exist_ok=True)
P = shot_params()


def attempt(tgt, name, p=P):
    t0 = time.monotonic()
    try:
        info = cdp_capture(tgt, os.path.join(out, name), p)
        return True, info, time.monotonic() - t0
    except ShotError as e:
        return False, str(e), time.monotonic() - t0


print(f"輸出：{out}")
print("\n== 正常截圖（需設備在線）==")
for key, tgt in TARGETS.items():
    ok, info, el = attempt(tgt, f"live_{key}.png")
    if ok:
        w, h = png_size(os.path.join(out, f"live_{key}.png"))
        check(f"{tgt.name}：成功（{el:.1f} 秒），圖片 {w}×{h}px，擷取 {info['captured_at']:%H:%M:%S}，"
              f"頁面時間 {info.get('data_time') or info.get('page_clock')}",
              tgt.size_min[0] <= w <= tgt.size_max[0] + 2 * P["SHOT_PAD_PX"])
    else:
        blocked(f"{tgt.name} 正常截圖", f"{info}（{el:.1f} 秒）")

heat = TARGETS["heat"]
print("\n== 錯誤範圍、逾時、尺寸不符（即時熱度頁可連線即可驗證）==")
bad = dataclasses.replace(heat, must=["獨立櫃:999", "更新時間"], ready=[])
ok, err, el = attempt(bad, "bad_range.png", shot_params(SHOT_PAGE_TIMEOUT_SEC=8))
check(f"找不到指定區塊（獨立櫃:999）→ 失敗並說明缺少的文字（{el:.1f} 秒）：{err}",
      not ok and "找不到符合條件的區塊" in err and "獨立櫃:999" in err and not os.path.exists(
          os.path.join(out, "bad_range.png")))
slow = dataclasses.replace(heat, ready=[(r"不可能出現的文字\d{9}", "測試用：永遠不會就緒")])
ok, err, el = attempt(slow, "timeout.png", shot_params(SHOT_PAGE_TIMEOUT_SEC=4))
check(f"區塊始終未就緒 → 4 秒等待逾時（實際 {el:.1f} 秒）：{err}",
      not ok and "等待逾時（4 秒）" in err and el < 15)
small = dataclasses.replace(heat, ready=[], size_max=(100, 100), extra_check=None)
ok, err, el = attempt(small, "size.png", shot_params(SHOT_PAGE_TIMEOUT_SEC=10))
check(f"區塊尺寸超出預期 → 判「擷取範圍不符」，不存圖：{err}",
      not ok and "擷取範圍不符" in err and not os.path.exists(os.path.join(out, "size.png")))
unreach = dataclasses.replace(heat, url="http://192.168.70.201:6/")
ok, err, el = attempt(unreach, "unreach.png", shot_params(SHOT_PAGE_TIMEOUT_SEC=8))
check(f"頁面無法連線 → 失敗並記錄原因（{el:.1f} 秒）：{err}", not ok and el < 25)

print("\n== 背景工作串接實際截圖函式 ==")
wk = ScreenshotWorker(os.path.join(out, "worker"), params=shot_params(SHOT_ATTEMPTS=2, SHOT_RETRY_DELAY_SEC=2,
                                                                      SHOT_PAGE_TIMEOUT_SEC=15))
t0 = time.monotonic()
jid = wk.submit("manual", 1, "2026-10-28T00:00:00", "P2 驗證", "P2 實際頁面驗證（唯讀）")
check(f"submit() 立即回傳（{(time.monotonic() - t0) * 1000:.1f} ms）", time.monotonic() - t0 < 0.1)
done = wk.wait_idle(180)
res = {r["target"]: r for r in load_manifest(wk.folder) if r["type"] == "result"}
check(f"工作完成（{time.monotonic() - t0:.1f} 秒），兩個目標都有紀錄（含每次嘗試的時間與原因）",
      done and set(res) == {"overview", "heat"} and all(r["attempts"] for r in res.values()))
for k, r in res.items():
    print(f"     {TARGETS[k].name}：{r['status']}，嘗試 {len(r['attempts'])} 次"
          + (f"，原因：{r['reason']}" if r["reason"] else f"，檔案 {r['file']}"))

ok = all(_results)
print(f"\n== 認證報告 P2 實際頁面 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過；"
      f"無法驗證 {len(_blocked)} 項）==")
sys.exit(0 if ok else 1)
