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
import cert_report_config as CFG                                   # noqa: E402  G6：合成 cert_state 補 params
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
                          {"cert_id": cid, "status": status, "test_info": {"cabinet_spec": "250KW"},
                           "params": CFG.params()})
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

# =============================================================================
# G6：選單 25／歷史重產使用 Session 原始參數（必要參數驗證、差異確認、缺失拒絕、服務端獨立驗證、
#     實際影響分析的參數、版本號不覆寫、Menu 23／24／25 一致、記錄中維持既有行為）
# =============================================================================
import hashlib as _hl6                                  # noqa: E402

from openpyxl import load_workbook as _lw6              # noqa: E402

import cert_runtime as CR6                              # noqa: E402
from cert_stage_engine import stage_names as _sn6       # noqa: E402

TMP6 = tempfile.mkdtemp(prefix="cert_m25g6_")
try:
    print("\n== G6 原始參數驗證規則（Menu 與 runtime 共用 session_params_check）==")
    base = CFG.params()

    def chk(p):
        return CR6.session_params_check({"params": p})

    sp0, e0, n0 = chk(dict(base))
    check("G6-PRM-U1 目前格式的完整參數 → 合法、無提示", sp0 == base and not e0 and not n0)
    miss_ok = []
    for k in CR6.REQUIRED_PARAMS:
        p = dict(base)
        del p[k]
        sp, e, _n = chk(p)
        miss_ok.append(sp is None and any(x.startswith(f"{k}：缺少") for x in e))
    check(f"G6-PRM-U2 九個必要參數逐項缺少 → 全部拒絕並列出參數名稱（{len(miss_ok)} 項）",
          len(miss_ok) == 9 and all(miss_ok))
    p = dict(base)
    del p["MINUTE_TOL_SEC"]
    sp, e, n = chk(p)
    check("G6-PRM-U3 相容參數 MINUTE_TOL_SEC 缺少 → 仍合法（提示），參數不補值",
          sp is not None and not e and any("MINUTE_TOL_SEC" in x for x in n) and "MINUTE_TOL_SEC" not in sp)
    check("G6-PRM-U4 MINUTE_TOL_SEC 存在但不合法（-1、字串、bool）→ 拒絕",
          all(chk(dict(base, MINUTE_TOL_SEC=v))[0] is None for v in (-1, "20", True)))
    bad_vals = (True, False, float("nan"), float("inf"), float("-inf"), "60", None, [60])
    check("G6-PRM-U5 數值參數拒絕 bool／NaN／±Infinity／字串／null／list",
          all(chk(dict(base, W_PAUSE_SEC=v))[0] is None for v in bad_vals)
          and all(chk(dict(base, IDLE_POWER_KW=v))[0] is None for v in bad_vals))
    check("G6-PRM-U6 範圍：W_PAUSE／TCONF／GAP_G／STOP_WARN／SAMPLE_INTERVAL 須 > 0；IDLE／CMD_STAGE_MATCH 可為 0",
          all(chk(dict(base, **{k: 0}))[0] is None for k in ("W_PAUSE_SEC", "TCONF_SEC", "GAP_G_SEC", "STOP_WARN_SEC",
                                                            "SAMPLE_INTERVAL_SEC"))
          and all(chk(dict(base, **{k: 0}))[0] is not None for k in ("IDLE_CURRENT_A", "IDLE_POWER_KW", "CMD_STAGE_MATCH_SEC"))
          and chk(dict(base, IDLE_CURRENT_A=-0.1))[0] is None)
    p = dict(base)
    del p["SENSOR_VALID_RANGE"]
    check("G6-PRM-U7 SENSOR_VALID_RANGE：缺少 → 拒絕；明確 null → 合法（兩者區分）",
          chk(p)[0] is None and any("SENSOR_VALID_RANGE：缺少" in x for x in chk(p)[1])
          and chk(dict(base, SENSOR_VALID_RANGE=None))[0] is not None)
    check("G6-PRM-U8 SENSOR_VALID_RANGE 上下限：[0,80]／(0,80) 合法；下限≥上限、bool、NaN、三個值、字串 → 拒絕",
          chk(dict(base, SENSOR_VALID_RANGE=[0, 80]))[0] is not None
          and chk(dict(base, SENSOR_VALID_RANGE=(0, 80.5)))[0] is not None
          and all(chk(dict(base, SENSOR_VALID_RANGE=v))[0] is None
                  for v in ([80, 0], [5, 5], [True, 5], [0, float("nan")], [1, 2, 3], "0-80", [0, "80"])))
    sp, e, _n = chk(dict(base, FUTURE_EXTRA_KEY="x"))
    check("G6-PRM-U9 未知額外參數 → 保留、不拒絕", sp is not None and sp.get("FUTURE_EXTRA_KEY") == "x" and not e)
    check("G6-PRM-U10 params 缺少或不是物件 → 拒絕",
          CR6.session_params_check({})[0] is None and CR6.session_params_check({"params": [1]})[0] is None
          and CR6.session_params_check(None)[0] is None)
    check("G6-PRM-U11 params_diff 列出鍵、Session 值、目前值（缺鍵標示（無））",
          CR6.params_diff(dict(base, W_PAUSE_SEC=200, NEW_K=1), base) == [("NEW_K", 1, "（無）"), ("W_PAUSE_SEC", 200, 60)])

    print("\n== G6 選單 25：參數來源、差異確認、缺失拒絕 ==")
    T6 = datetime(2026, 1, 9, 9, 0, 0)
    PLAN_P = [("C", 0, 200, 20.0, 40.0), ("I", 210, 290, 40.0, 40.0), ("C", 300, 500, 40.0, 60.0), ("I", 510, 700, 60.0, 60.0)]

    def mk(root, cid, params, status="ended", plan=PLAN_P):
        write_cert(root, cid, status, plan, T6)
        st = REQ.read_json(os.path.join(root, cid, "cert_state.json"))
        if params is None:
            st.pop("params", None)
        else:
            st["params"] = params
        REQ.atomic_write_json(os.path.join(root, cid, "cert_state.json"), st)
        return os.path.join(root, cid)

    def regen6(root, answers):
        outs = []
        r = cert_menu.UI(root, inp=feed(answers), out=outs.append, wait_sec=0.05).regenerate()
        return outs, r

    r_same = os.path.join(TMP6, "same")
    mk(r_same, "CERT_20260109_090000", dict(base))
    outs, _r = regen6(r_same, ["1"])
    check("G6-M25-1 參數完全相同 → 顯示參數來源、不另外確認，直接送出 regenerate",
          any("參數來源：本認證開始時記錄的參數" in o for o in outs) and not any("與目前設定不同" in o for o in outs)
          and regen_targets(r_same) == ["CERT_20260109_090000"])
    r_diff = os.path.join(TMP6, "diff")
    mk(r_diff, "CERT_20260109_090000", dict(base, W_PAUSE_SEC=200))
    outs, _r = regen6(r_diff, ["1", "no"])
    check("G6-M25-2 參數不同 → 逐項列出差異、說明使用原始參數；輸入 YES 以外 → 取消、不建立請求",
          any("W_PAUSE_SEC：本認證 200｜目前設定 60" in o for o in outs)
          and any("使用本認證的原始參數" in o for o in outs) and n_requests(r_diff) == 0)
    outs, _r = regen6(r_diff, ["1", "YES"])
    check("G6-M25-3 參數不同 → 輸入 YES → 送出 regenerate", regen_targets(r_diff) == ["CERT_20260109_090000"])
    r_miss = os.path.join(TMP6, "miss")
    pm = dict(base)
    del pm["STOP_WARN_SEC"]
    mk(r_miss, "CERT_20260109_090000", pm)
    outs, _r = regen6(r_miss, ["1"])
    check("G6-M25-4 必要參數缺少（STOP_WARN_SEC）→ 列出參數名稱、拒絕重產、不建立請求",
          any("STOP_WARN_SEC：缺少" in o for o in outs) and any("不能重新產生報告" in o for o in outs)
          and n_requests(r_miss) == 0)
    r_bad = os.path.join(TMP6, "bad")
    mk(r_bad, "CERT_20260109_090000", dict(base, GAP_G_SEC=True))
    outs, _r = regen6(r_bad, ["1"])
    check("G6-M25-5 參數型別不合法（GAP_G_SEC=true）→ 拒絕、不建立請求",
          any("GAP_G_SEC：不合法" in o for o in outs) and n_requests(r_bad) == 0)
    r_rec = os.path.join(TMP6, "rec")
    mk(r_rec, "CERT_20260109_090000", dict(base, W_PAUSE_SEC=200), status="recording")
    outs, _r = regen6(r_rec, ["1"])
    check("G6-M25-6 記錄中的認證 → 維持既有行為（顯示服務目前參數來源、不另外確認、照常送出）",
          any("背景服務目前載入的參數" in o for o in outs) and regen_targets(r_rec) == ["CERT_20260109_090000"])

    print("\n== G6 服務端獨立驗證與實際分析參數 ==")
    svc_m = CR6.CertRuntime(r_miss, reader=None, enable_shots=False, log=lambda *a: None)
    fm = os.path.join(r_miss, "CERT_20260109_090000")
    snap_m = snapshot(r_miss)
    rid = REQ.new_request(r_miss, "regenerate", cert_id="CERT_20260109_090000")
    svc_m.process_requests()
    rr = REQ.read_result(r_miss, rid) or {}
    ok_g, msg_g, path_g = svc_m.generate_report("CERT_20260109_090000")
    check("G6-RT-1 直接送 regenerate（繞過選單）→ 服務端拒絕、列出參數名稱；不產報、不改 cert_state／events",
          rr.get("status") == "rejected" and "STOP_WARN_SEC：缺少" in rr.get("message", "")
          and n_reports(r_miss) == 0 and not os.path.isdir(os.path.join(fm, "reports"))
          and {k: v for k, v in snapshot(r_miss).items() if not k.startswith(("requests", "results"))}
          == {k: v for k, v in snap_m.items() if not k.startswith(("requests", "results"))})
    check("G6-RT-2 直接呼叫 generate_report → (False, 原因)，不寫入任何檔案",
          ok_g is False and path_g is None and "STOP_WARN_SEC" in msg_g and not os.path.isdir(os.path.join(fm, "reports")))

    r_eff = os.path.join(TMP6, "effect")
    fe = mk(r_eff, "CERT_20260109_090000", dict(base, W_PAUSE_SEC=200, SAMPLE_INTERVAL_SEC=5))
    svc_e = CR6.CertRuntime(r_eff, reader=None, enable_shots=False, log=lambda *a: None)
    alt = CR6.CertRuntime(r_eff, reader=None, params=CFG.params(), enable_shots=False, log=lambda *a: None)
    alt.folder, alt.records = fe, CR6._read_jsonl(os.path.join(fe, "observations.jsonl"))
    alt.decisions, alt.commands = [], []
    n_cur = len(alt.analysis().stages)
    res_s = svc_e._offline(fe).analysis()
    rid = REQ.new_request(r_eff, "regenerate", cert_id="CERT_20260109_090000")
    svc_e.process_requests()
    rr = REQ.read_result(r_eff, rid) or {}
    rep1 = os.path.join(fe, "reports", "認證測試報告_CERT_20260109_090000_01.xlsx")
    wb = _lw6(rep1) if os.path.exists(rep1) else None
    sheets = wb.sheetnames if wb else []
    desc = (wb.properties.description or "") if wb else ""
    check(f"G6-RT-3 影響分析的參數（W_PAUSE_SEC 200 vs 目前 60）：目前參數會分成 {n_cur} 個階段；"
          "Session 原始參數合併為 1 個 → 重產報告只有「第1次充電」（未使用目前設定）",
          n_cur == 2 and len(res_s.stages) == 1 and rr.get("status") == "done"
          and any("第1次充電" in s for s in sheets) and not any("第2次充電" in s for s in sheets))
    check("G6-RT-4 結果訊息顯示參數來源；報告說明的取樣週期＝Session 原始值（5 秒），與實際分析參數一致",
          "Session 原始參數" in rr.get("message", "") and (rr.get("data") or {}).get("params_source", "").startswith("Session")
          and "取樣週期 5 秒（本次認證開始時的設定" in desc)
    h1 = _hl6.sha256(open(rep1, "rb").read()).hexdigest() if os.path.exists(rep1) else None
    rid2 = REQ.new_request(r_eff, "regenerate", cert_id="CERT_20260109_090000")
    svc_e.process_requests()
    rep2 = os.path.join(fe, "reports", "認證測試報告_CERT_20260109_090000_02.xlsx")
    check("G6-RT-5 再次重產 → 使用下一個 _02、不覆寫 _01（_01 雜湊不變）；cert_state.params 不被改寫",
          (REQ.read_result(r_eff, rid2) or {}).get("status") == "done" and os.path.exists(rep2)
          and _hl6.sha256(open(rep1, "rb").read()).hexdigest() == h1
          and REQ.read_json(os.path.join(fe, "cert_state.json"))["params"] == dict(base, W_PAUSE_SEC=200, SAMPLE_INTERVAL_SEC=5))
    rows23 = cert_menu.UI(r_eff, out=lambda *_: None).stage_rows("CERT_20260109_090000")
    _res_k, keys_k = svc_e._stage_keys(fe)
    check("G6-RT-6 Menu 23 清單、服務端階段判定（stage_label／stage_count／事後補拍共用）、Menu 25 報告使用同一組 Session 參數",
          [r["start"] for r in rows23] == sorted(keys_k) == [CR6._iso(s.start.t) for s in res_s.stages]
          and [r["name"] for r in rows23] == [n for _no, n in sorted(_sn6(res_s.stages).items())]
          and svc_e._offline(fe).p == dict(base, W_PAUSE_SEC=200, SAMPLE_INTERVAL_SEC=5))
    outs24 = []
    cert_menu.UI(r_miss, inp=feed(["Y", "1"]), out=outs24.append, wait_sec=0.05).post_supplement()
    check("G6-RT-7 Menu 24 事後補拍：原始參數缺少 → 無法分析、列出原因、不建立請求",
          any("無法分析" in o and "STOP_WARN_SEC：缺少" in o for o in outs24) and n_requests(r_miss) == 1)
    fr = os.path.join(r_rec, "CERT_20260109_090000")
    svc_r = CR6.CertRuntime(r_rec, reader=None, enable_shots=False, log=lambda *a: None)
    check("G6-RT-8 記錄中的認證：歷史分析維持 runtime 參數（self.p），不改用 Session 參數（既有行為）",
          svc_r._offline(fr).p is svc_r.p)
finally:
    shutil.rmtree(TMP6, ignore_errors=True)

ok = all(_results)
print(f"\n== 認證報告 選單 25 重新產生報告 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
