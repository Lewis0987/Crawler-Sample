# -*- coding: utf-8 -*-
"""
認證報告 —— 選單 24「已結束認證事後補拍」認證紀錄選擇流程（離線驗證）
======================================================================
把 Menu 24 的認證選擇與 Menu 23／25 統一：
  1. 選擇畫面有「0. 返回」，提示文字為「選擇認證（0=返回，Enter=第 1 項）：」。
  2. 輸入 0 → 返回「認證測試」選單，不建立補拍請求、不修改任何既有認證資料。
  3. 迴歸：輸入 0 **不得**經 Python 負數索引選到清單最舊一筆（certs[0 - 1] → certs[-1]）。
  4. Enter（空輸入）→ 預設第 1 項（最新一次認證）。
  5. 輸入 1～N → 選到對應認證。
  6. 超出清單範圍／負數 → 維持原本退出行為，不建立請求。
  7. Menu 21、22、26、27 完全不經過 pick_cert，不受本次更動影響。
  8. 前置確認畫面（pick_cert 之前）採 fail safe，提示文字為
     「進入事後補拍？（Y=進入，0=返回）：」—— 提示中不再有 Enter=取消。
  9. 只有明確輸入 Y／YES 才進入補拍；0／Enter／任何其他輸入一律返回，
     返回訊息統一為「已返回「認證測試」選單，未送出事後補拍。」。
 10. 兩個返回階段（前置確認／認證選擇）都不得建立補拍請求或修改認證資料。

全部使用暫存目錄中的合成資料；不連設備、不送控制、不動正式 output。
執行：python test_cert_menu24.py
"""

import glob
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cert_menu                                                    # noqa: E402
import cert_requests as REQ                                         # noqa: E402
import cert_report_config as CFG                                   # noqa: E402  G6：合成 cert_state 補 params

_results = []
_P = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def feed(answers, record=False):
    """依序餵給 input()；答案用完 → 視為使用者中斷（UI.ask 回預設值）。"""
    it = iter(answers)

    def _inp(prompt=""):
        if record:
            _P.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise KeyboardInterrupt
    return _inp


CUR = {"C": 135.0, "D": -140.0, "I": 0.0}


def rec(t, k, soc):
    vals = {"soc": soc, "current_a": CUR[k], "power_kw": None, "rack_max_c": 40.0, "rack_min_c": 30.0,
            "cell_vmax_v": 3.4, "cell_vmin_v": 3.3, "voltage_v": 900.0, "sensor_c": None, "pcs_module_c": "60"}
    return {"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "hmi_ok": True, "values": vals, "missing": [],
            "fail_reason": "", "notes": [], "flags": {}}


def write_cert(root, cid, status, plan, t0):
    """plan：[(方向, 秒起, 秒訖, 首筆 SOC, 末筆 SOC)]，每 10 秒一筆。沒有 screenshots.jsonl
    → 每個階段的原始即時熱度截圖都算「缺少」，正好是事後補拍的對象。"""
    folder = os.path.join(root, cid)
    os.makedirs(folder)
    with open(os.path.join(folder, "observations.jsonl"), "w", encoding="utf-8") as f:
        for k, s0, s1, soc0, soc1 in plan:
            secs = list(range(s0, s1 + 1, 10))
            for s in secs:
                soc = soc0 if s == secs[0] else (soc1 if s == secs[-1] else 50.0)
                f.write(json.dumps(rec(t0 + timedelta(seconds=s), k, soc), ensure_ascii=False) + "\n")
    REQ.atomic_write_json(os.path.join(folder, "cert_state.json"),
                          {"cert_id": cid, "status": status, "test_info": {"cabinet_spec": "250KW"},
                           "params": CFG.params()})
    return folder


def n_requests(root):
    return len(glob.glob(os.path.join(root, "requests", "*", "*.json")))


def supp_targets(root):
    """目前已建立的事後補拍請求各自指向哪一筆認證。"""
    out = []
    for p in sorted(glob.glob(os.path.join(root, "requests", "*", "*.json"))):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("type") == "post_test_supplement":
            out.append(d.get("cert_id"))
    return out


def snapshot(root):
    """認證資料夾下每個檔案的 (size, mtime_ns, sha256)；用來證明「什麼都沒改」。"""
    out = {}
    for cid in sorted(d for d in os.listdir(root) if d.startswith("CERT_")):
        for dirpath, _dirs, files in os.walk(os.path.join(root, cid)):
            for fn in sorted(files):
                p = os.path.join(dirpath, fn)
                st = os.stat(p)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns,
                                                     hashlib.sha256(f.read()).hexdigest())
    return out


def run24(root, answers, record=False):
    """從 Menu 24 的正式入口 shot() 進入（沒有進行中的認證 → 事後補拍）。"""
    outs = []
    res = cert_menu.UI(root, inp=feed(answers, record), out=outs.append, wait_sec=0.05).shot()
    return outs, res


def pick(root, answer):
    return cert_menu.UI(root, inp=feed([answer]), out=lambda *_: None).pick_cert()


TMP = tempfile.mkdtemp(prefix="cert_m24_")
try:
    T0 = datetime(2026, 1, 5, 9, 0, 0)
    root = os.path.join(TMP, "syn")
    OLD = "CERT_20260105_090000"       # 最舊
    MID = "CERT_20260106_090000"
    NEW = "CERT_20260107_090000"       # 最新 → 清單第 1 項
    for i, cid in enumerate((OLD, MID, NEW)):
        write_cert(root, cid, "ended",
                   [("C", 0, 300, 0.0, 30.0), ("I", 310, 430, 30.0, 30.0),
                    ("D", 440, 740, 100.0, 20.0), ("I", 750, 870, 20.0, 20.0)],
                   T0 + timedelta(days=i))

    print("\n== Menu 24 入口：沒有進行中的認證 → 事後補拍 ==")
    outs, res = run24(root, ["Y", "0"], record=True)
    check("shot() 在沒有進行中的認證時進入事後補拍說明",
          any("已結束認證的事後補拍" in o for o in outs))
    check("認證清單依新→舊列出，第 1 項為最新一次認證",
          f"  1. {NEW}（已結束）" in outs and f"  3. {OLD}（已結束）" in outs)
    check("認證清單新增「  0. 返回」", "  0. 返回" in outs)
    check("提示文字與 Menu 23／25 統一：選擇認證（0=返回，Enter=第 1 項）：",
          _P[-1] == "選擇認證（0=返回，Enter=第 1 項）：")

    print("\n== 前置確認階段（pick_cert 之前）的 0＝返回 ==")
    #  Menu 24 的實際流程：shot() → 沒有進行中的認證 → post_supplement()
    #    ① 前置確認「進入事後補拍？」  ② pick_cert() 認證紀錄選擇
    #  ① 比 ② 先發生，所以 ① 也必須能返回，否則使用者得先按 Y 進到 ② 才看得到 0。
    c_n, c_snap = n_requests(root), snapshot(root)
    del _P[:]
    outs, res = run24(root, ["0"], record=True)
    check("前置確認提示文字為「進入事後補拍？（Y=進入，0=返回）：」",
          _P[-1] == "進入事後補拍？（Y=進入，0=返回）：")
    check("前置確認提示文字已移除「Enter=取消」",
          "Enter" not in _P[-1] and "取消" not in _P[-1])
    check("前置確認輸入 0 → 直接返回 None", res is None)
    check("前置確認輸入 0 → 明確告知已返回、未送出事後補拍",
          any("已返回" in o and "未送出事後補拍" in o for o in outs))
    check("前置確認輸入 0 → 不再往下進入認證紀錄選擇（看不到清單與「0. 返回」）",
          not any("0. 返回" in o for o in outs)
          and not any(f"1. {NEW}" in o for o in outs))
    check("前置確認輸入 0 → 只被問了 1 個問題（確認畫面之後就返回）", len(_P) == 1)
    check("前置確認輸入 0 → 完全沒有建立任何補拍請求",
          n_requests(root) == c_n and supp_targets(root) == [])
    check("前置確認輸入 0 → 既有認證資料一個 byte 都沒變（size / mtime_ns / sha256）",
          snapshot(root) == c_snap)
    outs_sp, res_sp = run24(root, [" 0 "])
    check("前置確認輸入「 0 」（前後空白）→ 一樣視為返回",
          res_sp is None and any("已返回" in o for o in outs_sp))

    print("\n== 前置確認 fail safe：非 Y 一律安全返回（不得預設進入補拍）==")
    BACK_MSG = "已返回「認證測試」選單，未送出事後補拍。"
    for ans, label in (("", "直接按 Enter（空輸入）"), ("n", "輸入 n"), ("x", "輸入其他字元"),
                       ("y1", "誤打 y1"), ("Ｙ", "全形Ｙ"), ("0", "輸入 0"),
                       ("  ", "只輸入空白")):
        outs_c, res_c = run24(root, [ans])
        check(f"{label} → 返回主選單、不進入補拍（統一訊息，且不再顯示「已取消」）",
              res_c is None and BACK_MSG in outs_c
              and not any("已取消" in o for o in outs_c)
              and not any("0. 返回" in o for o in outs_c))
    check("Enter／0／其他輸入 → 一律不建立補拍請求，認證資料一個 byte 都沒變",
          n_requests(root) == c_n and supp_targets(root) == [] and snapshot(root) == c_snap)
    check("前置確認不存在「預設進入補拍」的路徑（原始碼只認 Y／YES）",
          '("Y", "YES")' in inspect.getsource(cert_menu.UI.post_supplement))
    for ans in ("Y", "y", "YES", "yes"):
        outs_y, _ = run24(root, [ans, "0"])
        check(f"輸入 {ans} → 維持進入事後補拍流程（有列出認證清單與「0. 返回」）",
              any("0. 返回" in o for o in outs_y)
              and any(f"1. {NEW}" in o for o in outs_y))

    print("\n== 兩個返回階段的一致性 ==")
    del _P[:]
    outs_s1, res_s1 = run24(root, ["0"], record=True)          # ① 前置確認返回
    n_p1 = len(_P)
    del _P[:]
    outs_s2, res_s2 = run24(root, ["Y", "0"], record=True)     # ② 認證選擇返回
    check("兩個階段都回 None（都不往下執行）", res_s1 is None and res_s2 is None)
    check("兩個階段的返回訊息完全一致（逐字相同的統一訊息）",
          [o for o in outs_s1 if "已返回" in o] == [BACK_MSG]
          and [o for o in outs_s2 if "已返回" in o] == [BACK_MSG])
    check("① 比 ② 少問一個問題（證明前置確認確實在 pick_cert 之前）", n_p1 == len(_P) - 1)
    check("兩個階段返回後都沒有任何補拍請求、認證資料未變",
          n_requests(root) == c_n and supp_targets(root) == [] and snapshot(root) == c_snap)
    check("post_supplement 的前置確認在 pick_cert 之前（原始碼順序）",
          inspect.getsource(cert_menu.UI.post_supplement).index("進入事後補拍？")
          < inspect.getsource(cert_menu.UI.post_supplement).index("self.pick_cert()"))

    print("\n== 前置確認不影響其他選單 ==")
    check("「進入事後補拍？」只出現在 Menu 24，Menu 21／22／23／25／26／27 都沒有",
          all("進入事後補拍？" not in inspect.getsource(f)
              for f in (cert_menu.UI.start, cert_menu.UI.end, cert_menu.UI.mark,
                        cert_menu.UI.regenerate, cert_menu.UI.status, cert_menu.UI.query)))
    check("Menu 23／25 入口第一個問題仍是認證選擇（沒有被加上前置確認）",
          "進入" not in inspect.getsource(cert_menu.UI.mark).split("pick_cert")[0]
          and "進入" not in inspect.getsource(cert_menu.UI.regenerate).split("pick_cert")[0])

    print("\n== 0 的行為：返回，且不得選到最舊一筆 ==")
    base_n = n_requests(root)
    base_snap = snapshot(root)
    outs, res = run24(root, ["Y", "0"])
    check("輸入 0 → pick_cert 回 BACK 哨兵（不是任何 cert_id）", pick(root, "0") == cert_menu.BACK)
    check("輸入 0 → post_supplement 直接返回 None（不再往下問階段）", res is None)
    check("輸入 0 → 明確告知已返回、未送出事後補拍",
          any("已返回" in o and "未送出事後補拍" in o for o in outs))
    check("輸入 0 → 不往下顯示任何可補拍階段清單",
          not any("原始即時熱度截圖" in o for o in outs))
    check("輸入 0 → 完全沒有建立任何補拍請求",
          n_requests(root) == base_n and supp_targets(root) == [])
    check(f"🔴 迴歸：輸入 0 不再經負索引選到最舊一筆（沒有任何指向 {OLD} 的補拍請求）",
          OLD not in supp_targets(root))
    check("輸入 0 → 既有認證資料一個 byte 都沒變（size / mtime_ns / sha256 全部相同）",
          snapshot(root) == base_snap)

    print("\n== 1～N 與 Enter 的選擇 ==")
    for ans, want, label in (("1", NEW, "輸入 1 → 最新一次認證"),
                             ("2", MID, "輸入 2 → 第 2 項"),
                             ("3", OLD, "輸入 3 → 最舊一次認證"),
                             ("", NEW, "直接按 Enter → 預設第 1 項（最新）")):
        check(f"{label}（{want}）", pick(root, ans) == want)
    outs, res = run24(root, ["Y", "2"])
    check(f"Menu 24 選 2 → 確實進入 {MID} 的可補拍階段清單（不是最舊那筆）",
          any("1. 階段 1 第1次充電" in o for o in outs)
          and any("原始即時熱度截圖：缺少" in o for o in outs))

    print("\n== 超出範圍與負數：維持原本退出行為 ==")
    for ans, label in (("4", "剛好超出清單筆數（4 / 共 3 筆）"), ("99", "遠超出清單範圍（99）"),
                       ("-1", "負數（-1，不得經負索引取到最舊一筆）"), ("abc", "非數字且非 cert_id")):
        check(f"{label} → 回 None（維持原本退出行為）", pick(root, ans) is None)
    n_before, snap_before = n_requests(root), snapshot(root)
    for ans in ("4", "99", "-1", "abc"):
        run24(root, ["Y", ans])
    check("超出範圍／負數／亂輸入 → 一律不建立補拍請求", n_requests(root) == n_before)
    check("超出範圍／負數／亂輸入 → 既有認證資料未被修改", snapshot(root) == snap_before)

    print("\n== 正常路徑仍可用（選 2 → 階段 1 → YES）==")
    outs, res = run24(root, ["Y", "2", "1", "YES"])
    check(f"送出 post_test_supplement 請求且 cert_id = {MID}", supp_targets(root) == [MID])
    check("送出前完整顯示警語（事後補拍不代表階段結束時的設備狀態）",
          any("不代表階段結束時的設備狀態" in o for o in outs))
    n_after = n_requests(root)
    outs, res = run24(root, ["Y", "2", "1", "no"])
    check("最後一關不是 YES → 已取消，不新增請求",
          res is None and n_requests(root) == n_after and any("已取消" in o for o in outs))

    print("\n== 三個選單統一：23 階段標記／24 事後補拍／25 重新產生報告 ==")
    prompts = {}
    del _P[:]
    cert_menu.UI(root, inp=feed(["0"], True), out=lambda *_: None, wait_sec=0.05).mark()
    prompts["23"] = _P[-1]
    del _P[:]
    cert_menu.UI(root, inp=feed(["Y", "0"], True), out=lambda *_: None, wait_sec=0.05).shot()
    prompts["24"] = _P[-1]
    del _P[:]
    cert_menu.UI(root, inp=feed(["0"], True), out=lambda *_: None, wait_sec=0.05).regenerate()
    prompts["25"] = _P[-1]
    check("23／24／25 的認證選擇提示文字完全相同",
          set(prompts.values()) == {"選擇認證（0=返回，Enter=第 1 項）："})
    check("pick_cert 已不再需要 allow_back 開關（三個呼叫端共用同一行為）",
          "allow_back" not in inspect.getsource(cert_menu.UI.pick_cert))
    check("三個呼叫端都處理 BACK（0＝返回一律不執行任何動作）",
          all("BACK" in inspect.getsource(f)
              for f in (cert_menu.UI.mark, cert_menu.UI.post_supplement, cert_menu.UI.regenerate)))

    print("\n== 不影響 Menu 21、22、26、27 ==")
    check("Menu 21／22／26／27 的流程完全不呼叫 pick_cert",
          all("pick_cert" not in inspect.getsource(f)
              for f in (cert_menu.UI.start, cert_menu.UI.end,
                        cert_menu.UI.status, cert_menu.UI.query)))
    check("Menu 24 有進行中的認證時仍走原本的立即截圖（不進事後補拍）",
          "post_supplement" in inspect.getsource(cert_menu.UI.shot)
          and "manual_shot" in inspect.getsource(cert_menu.UI.shot))
    root_r = os.path.join(TMP, "rec")
    REC = "CERT_20260108_090000"
    write_cert(root_r, REC, "recording", [("D", 0, 600, 100.0, 80.0)], T0 + timedelta(days=3))
    REQ.atomic_write_json(os.path.join(root_r, "active.json"), {"cert_id": REC})
    outs, res = run24(root_r, ["測試原因"])
    types_r = []
    for p_ in sorted(glob.glob(os.path.join(root_r, "requests", "*", "*.json"))):
        with open(p_, encoding="utf-8") as f_:
            types_r.append(json.load(f_).get("type"))
    check("有進行中的認證 → shot() 送出 manual_shot（不經 pick_cert、不受 0＝返回影響）",
          types_r == ["manual_shot"]
          and not any("事後補拍" in o for o in outs)
          and not any("0. 返回" in o for o in outs))

    print("\n== 沒有任何認證時 ==")
    empty = os.path.join(TMP, "empty")
    os.makedirs(empty)
    outs_e = []
    res_e = cert_menu.UI(empty, inp=feed(["Y", "0"]), out=outs_e.append, wait_sec=0.05).shot()
    check("沒有任何認證 → 提示並返回，不建立請求",
          res_e is None and any("尚無任何認證測試" in o for o in outs_e) and n_requests(empty) == 0)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== 認證報告 選單 24 事後補拍選擇 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
