# -*- coding: utf-8 -*-
"""
認證報告 —— 選單 25「重新產生認證報告」選擇流程（離線驗證）
==========================================================
驗證重點：
  1. 選擇畫面有「0. 返回」，提示文字為「選擇認證（0=返回，Enter=第 1 項）：」。
  2. 輸入 0 → 直接返回，不建立請求、不產生報告、不修改任何既有認證紀錄。
  3. 迴歸：輸入 0 **不得**經 Python 負數索引選到清單最舊一筆（原本 certs[0 - 1] → certs[-1]）。
  4. Enter（空輸入）→ 預設第 1 項（最新一次認證）。
  5. 輸入 1～N → 選到對應認證。
  6. 超出清單範圍的編號／負數 → 維持直接退出，不建立請求。
  7. 23 階段標記／24 事後補拍／25 重新產生報告共用同一套選擇方式；Menu 21、22、26、27 不經過 pick_cert。

全部使用暫存目錄中的合成資料；不連設備、不送控制、不動正式 output。
執行：python test_cert_menu25.py
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
from cert_runtime import CertRuntime                                # noqa: E402

_results = []
_P = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def feed(answers):
    """把 answers 依序餵給 input()；用完再問 → 視為使用者中斷（UI.ask 回預設值）。"""
    it = iter(answers)

    def _inp(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise KeyboardInterrupt
    return _inp


def spy(answer):
    """記錄提示文字並回固定答案。"""
    def _inp(prompt=""):
        _P.append(prompt)
        return answer
    return _inp


CUR = {"C": 135.0, "D": -140.0, "I": 0.0}


def rec(t, k, soc):
    vals = {"soc": soc, "current_a": CUR[k], "power_kw": None, "rack_max_c": 40.0, "rack_min_c": 30.0,
            "cell_vmax_v": 3.4, "cell_vmin_v": 3.3, "voltage_v": 900.0, "sensor_c": None, "pcs_module_c": "60"}
    return {"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "hmi_ok": True, "values": vals, "missing": [],
            "fail_reason": "", "notes": [], "flags": {}}


def write_cert(root, cid, status, plan, t0):
    """plan：[(方向, 秒起, 秒訖, 首筆 SOC, 末筆 SOC)]，每 10 秒一筆。"""
    folder = os.path.join(root, cid)
    os.makedirs(folder)
    with open(os.path.join(folder, "observations.jsonl"), "w", encoding="utf-8") as f:
        for k, s0, s1, soc0, soc1 in plan:
            secs = list(range(s0, s1 + 1, 10))
            for s in secs:
                soc = soc0 if s == secs[0] else (soc1 if s == secs[-1] else 50.0)
                f.write(json.dumps(rec(t0 + timedelta(seconds=s), k, soc), ensure_ascii=False) + "\n")
    REQ.atomic_write_json(os.path.join(folder, "cert_state.json"),
                          {"cert_id": cid, "status": status, "test_info": {"cabinet_spec": "250KW"}})
    return folder


def n_requests(root):
    return len(glob.glob(os.path.join(root, "requests", "*", "*.json")))


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


def n_reports(root):
    return len(glob.glob(os.path.join(root, "CERT_*", "reports", "*.xlsx")))


def regen_targets(root):
    """目前已建立的 regenerate 請求各自指向哪一筆認證。"""
    out = []
    for p in sorted(glob.glob(os.path.join(root, "requests", "*", "*.json"))):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("type") == "regenerate":
            out.append(d.get("cert_id"))
    return out


def run_regen(root, answers):
    """跑一次 Menu 25；回傳 (輸出行, regenerate() 回傳值, regenerate 請求的目標清單)。"""
    outs = []
    ui = cert_menu.UI(root, inp=feed(answers), out=outs.append, wait_sec=0.05)
    res = ui.regenerate()
    return outs, res, regen_targets(root)


def pick(root, answer, **kw):
    return cert_menu.UI(root, inp=feed([answer]), out=lambda *_: None).pick_cert(**kw)


TMP = tempfile.mkdtemp(prefix="cert_m25_")
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

    print("\n== 選擇畫面：0 = 返回 ==")
    outs, res, _sent = run_regen(root, ["0"])
    check("清單依新→舊列出，第 1 項為最新一次認證、第 3 項為最舊",
          outs[0] == f"  1. {NEW}（已結束）" and outs[2] == f"  3. {OLD}（已結束）")
    check("清單新增「  0. 返回」", "  0. 返回" in outs)
    del _P[:]
    cert_menu.UI(root, inp=spy("0"), out=lambda *_: None, wait_sec=0.05).regenerate()
    check("提示文字為「選擇認證（0=返回，Enter=第 1 項）：」",
          _P[-1] == "選擇認證（0=返回，Enter=第 1 項）：")

    print("\n== 0 的行為：返回，且不得選到最舊一筆 ==")
    base_n = n_requests(root)
    base_snap = snapshot(root)
    outs, res, sent = run_regen(root, ["0"])
    check("輸入 0 → pick_cert 回 BACK 哨兵（不是任何 cert_id）",
          pick(root, "0") == cert_menu.BACK)
    check("輸入 0 → regenerate() 回 None（未送出請求）", res is None)
    check("輸入 0 → 明確告知已返回、未重新產生報告",
          any("已返回" in o and "未重新產生報告" in o for o in outs))
    check("輸入 0 → 完全沒有建立任何請求", n_requests(root) == base_n)
    check(f"🔴 迴歸：輸入 0 不再選到最舊一筆（沒有任何 regenerate 請求，更沒有指向 {OLD}）",
          sent == [] and OLD not in sent)
    check("輸入 0 → 既有認證紀錄一個 byte 都沒變（size / mtime_ns / sha256 全部相同）",
          snapshot(root) == base_snap)
    check("輸入 0 → 沒有產生任何報告檔", n_reports(root) == 0)

    print("\n== 1～N 與 Enter 的選擇 ==")
    for ans, want, label in (("1", NEW, "輸入 1 → 最新一次認證"),
                             ("2", MID, "輸入 2 → 第 2 項"),
                             ("3", OLD, "輸入 3 → 最舊一次認證"),
                             ("", NEW, "直接按 Enter → 預設第 1 項（最新）")):
        check(f"{label}（{want}）", pick(root, ans) == want)

    print("\n== 超出範圍與負數：維持直接退出 ==")
    for ans, label in (("4", "剛好超出清單筆數（4 / 共 3 筆）"),
                       ("99", "遠超出清單範圍（99）"),
                       ("-1", "負數（-1，不得經負索引取到最舊一筆）"),
                       ("abc", "非數字且非 cert_id")):
        check(f"{label} → 回 None（直接退出）", pick(root, ans) is None)
    n_before = n_requests(root)
    snap_before = snapshot(root)
    for ans in ("4", "99", "-1", "abc"):
        run_regen(root, [ans])
    check("超出範圍／負數／亂輸入 → 一律不建立請求", n_requests(root) == n_before)
    check("超出範圍／負數／亂輸入 → 既有認證紀錄未被修改", snapshot(root) == snap_before)
    check("以上全部嘗試後，仍未產生任何報告檔", n_reports(root) == 0)

    print("\n== 正常路徑仍可用（選 2 → 確實重產該筆，不是最舊那筆）==")
    outs, res, sent = run_regen(root, ["2"])
    check(f"選 2 → 送出 regenerate 請求且 cert_id = {MID}", sent == [MID])
    CertRuntime(root, reader=None, enable_shots=False, log=lambda *a: None).process_requests()
    check("背景服務處理後，報告產生在被選中的那一筆；最舊那筆仍然沒有報告",
          len(glob.glob(os.path.join(root, MID, "reports", "*.xlsx"))) == 1
          and len(glob.glob(os.path.join(root, OLD, "reports", "*.xlsx"))) == 0)

    print("\n== 三個選單統一；Menu 21、22、26、27 不受影響 ==")
    check("直接輸入 cert_id 仍可選（行為不變）", pick(root, MID) == MID)
    del _P[:]
    cert_menu.UI(root, inp=spy("0"), out=lambda *_: None, wait_sec=0.05).mark()
    check("Menu 23（mark）與 Menu 25 同一組 0＝返回提示文字",
          _P[-1] == "選擇認證（0=返回，Enter=第 1 項）：")
    check("Menu 21、22、26、27 完全不呼叫 pick_cert（不受本次更動影響）",
          all("pick_cert" not in inspect.getsource(f)
              for f in (cert_menu.UI.start, cert_menu.UI.end,
                        cert_menu.UI.status, cert_menu.UI.query)))

    print("\n== 沒有任何認證時 ==")
    empty = os.path.join(TMP, "empty")
    os.makedirs(empty)
    outs_e = []
    res_e = cert_menu.UI(empty, inp=feed(["0"]), out=outs_e.append, wait_sec=0.05).regenerate()
    check("沒有任何認證 → 提示並返回，不建立請求",
          res_e is None and any("尚無任何認證測試" in o for o in outs_e) and n_requests(empty) == 0)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== 認證報告 選單 25 重新產生報告 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
