# -*- coding: utf-8 -*-
"""
跨 Session 整合認證報告工具 —— 離線測試（docs/Cert_Merge_Report_Design.md §15：P-24、TA、TP、TB、TN）
====================================================================================================
用法：python -B -I test_cert_merge_report.py --phase 3A|3B|3C --work <隔離測試資料夾（不得已存在）> [--c1 <C1 Session 副本>]

  * 全部資料只在 --work（合成 Session、輸出）與 --c1（唯讀，只建立快照）之下；不讀寫正式版、不連網路、不碰正式 Session。
  * V7 一律使用 --work 內的隔離 active.json（N-4）；不存取正式版 active.json。
  * 共同停止條件：寫入 --work 以外路徑、網路連線嘗試、來源副本雜湊改變 → 立即停止整個測試並回報。
  * 單一案例失敗 → 記錄並繼續；最後整體 FAILED（不修改門檻或預期值）。
"""
import argparse
import ast
import builtins
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import traceback
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cert_merge_source as SRC          # noqa: E402

SRC.block_network()

import cert_merge_analysis as AN         # noqa: E402
import cert_merge_report as MR           # noqa: E402
import cert_merge_verify as VF           # noqa: E402
import cert_merge_xlsx as XL             # noqa: E402
import cert_report_config as CFG         # noqa: E402
from cert_report_xlsx import save_report  # noqa: E402

RESULTS = []
WORK = None
ACTIVE = None
TEST_TMP = None
BANNED = ("完全一致", "相同檔案", "與原始報告一致", "全部驗證通過")


class Abort(Exception):
    """共同停止條件。"""


def check(tid, name, ok, detail=""):
    RESULTS.append((tid, name, "PASS" if ok else "FAIL", detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tid} {name}" + (f" —— {detail}" if detail and not ok else ""))


def skip(tid, name, reason):
    RESULTS.append((tid, name, "SKIP", reason))
    print(f"  [SKIP] {tid} {name} —— {reason}")


HELD_VR = None      # REV-1 已核准（2026-11-16）；T23-2、T23-3 已解除暫停


# =============================================================================
# 合成 Session
# =============================================================================
T0 = datetime(2026, 12, 1, 9, 0, 0)


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _jl(path, recs):
    _w(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))


def make_session(name, start, pattern, *, fail_secs=(), flag_delay=(), decisions=None, cabinet="250KW",
                 conditions="140A", params=None, slots=True, report=True, report_suffix="", report_no=1,
                 hmi_reader=None, ep_ms=0, incomplete=None, root=None):
    """
    pattern：[(方向 "C"/"D"/"I", 秒數), ...]，1 Hz；fail_secs：讀取失敗的秒序號；flag_delay：電流 0 但 PCS 旗標仍為前一方向。
    回傳 Session 資料夾路徑（位於 --work/src 下）。
    """
    root = root or os.path.join(WORK, "src")
    cid = name
    d = os.path.join(root, cid)
    assert not os.path.exists(d), d
    recs, slots_l = [], []
    seg = f"seg-{cid}"
    soc, last_dir, i = 50.0, "I", 0
    for dr, secs in pattern:
        for _ in range(secs):
            t = start + timedelta(seconds=i + 1)
            cur = {"C": 140.0, "D": -140.0, "I": 0.0}[dr]
            soc = max(0.0, min(100.0, soc + (0.01 if dr == "C" else -0.01 if dr == "D" else 0)))
            fd = i in flag_delay
            flags = {"pcs_charging_flag": dr == "C" or (fd and last_dir == "C"),
                     "pcs_discharging_flag": dr == "D" or (fd and last_dir == "D"), "pcs_fault_flag": False,
                     "battery_power_status": "已上電", "pcs_running_flag": dr != "I", "pcs_standby_flag": dr == "I",
                     "pcs_status": "運行 / 放電" if (dr == "D" or (fd and last_dir == "D")) else
                     ("運行 / 充電" if (dr == "C" or (fd and last_dir == "C")) else "停止 / 併網"),
                     "communication_ok": True, "raw_source_time": ""}
            if i in fail_secs:
                rec = {"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": False, "hmi_ok": False,
                       "values": {k: None for k in ("soc", "current_a", "power_kw", "rack_max_c", "rack_min_c",
                                                    "cell_vmax_v", "cell_vmin_v", "voltage_v", "sensor_c", "pcs_module_c")},
                       "missing": [], "fail_reason": "HMI 讀取失敗：測試", "notes": [], "flags": {}}
                st = "ATTEMPTED_FAIL"
            else:
                rec = {"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True, "hmi_ok": True,
                       "values": {"soc": round(soc, 2), "current_a": cur, "power_kw": cur * 0.95, "rack_max_c": 30.0,
                                  "rack_min_c": 25.0, "cell_vmax_v": 3.4, "cell_vmin_v": 3.3, "voltage_v": 950.0,
                                  "sensor_c": "-", "pcs_module_c": "32"},
                       "missing": ["sensor_c"], "fail_reason": "", "notes": [], "flags": flags}
                st = "ATTEMPTED_OK"
            if slots:
                rec["slot_seg_id"], rec["slot_index"] = seg, i
                ts = t.strftime("%Y-%m-%dT%H:%M:%S") + ".000+08:00"
                slots_l.append({"rec": "SLOT", "seg_id": seg, "slot_index": i, "scheduled_time": ts, "slot_status": st,
                                "raw_ref": i + 1, "read_start": ts, "read_done": ts, "failed_endpoints": [],
                                "communication_ok": st == "ATTEMPTED_OK", "timeout": False, "missed_reason": None,
                                "read_error": None if st == "ATTEMPTED_OK" else "測試"})
            recs.append(rec)
            if dr != "I":
                last_dir = dr
            i += 1
    end = start + timedelta(seconds=i + 1)
    _jl(os.path.join(d, "observations.jsonl"), recs)
    if slots:
        a = (start + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S") + ".000+08:00"
        e = end.strftime("%Y-%m-%dT%H:%M:%S") + ".000+08:00"
        _jl(os.path.join(d, "slots.jsonl"),
            [{"rec": "SEGMENT_START", "schema": 1, "seg_id": seg, "owner_kind": "cert", "owner_id": cid,
              "interval_sec": 1.0, "anchor_wall": a, "anchor_mono": 1000.0, "reason": "start", "pid": 1}]
            + slots_l + [{"rec": "SEGMENT_END", "seg_id": seg, "last_slot_index": i - 1, "reason": "finalize",
                          "end_wall": e}])
    if decisions:
        _jl(os.path.join(d, "decisions.jsonl"), decisions)
    if ep_ms:
        _jl(os.path.join(d, "cert_timing.jsonl"), [{"reader": {"read_ms": 300, "ep_ms": [["x", 1]] * ep_ms}}] * 3)
    st = {"cert_id": cid, "status": "ended", "started_at": start.strftime("%Y-%m-%dT%H:%M:%S"),
          "ended_at": end.strftime("%Y-%m-%dT%H:%M:%S"),
          "test_info": {"cabinet_spec": cabinet, "cycles": 1, "aircon": "", "hardware": "", "conditions": conditions},
          "sample_count": len(recs), "incomplete": incomplete, "interruptions": [], "reports": [],
          "params": params if params is not None else CFG.params()}
    if hmi_reader:
        st["hmi_reader"] = hmi_reader
    _w(os.path.join(d, "cert_state.json"), json.dumps(st, ensure_ascii=False, indent=2))
    if report:
        make_original(d, suffix=report_suffix, no=report_no)
    return d


def make_original(d, suffix="", no=1, at=None):
    """以目前程式產生「原始報告」（不含重產聲明、標題為原始標題），並記錄在 cert_state.reports。"""
    data = SRC.load_session(d, os.path.dirname(d))
    sa = AN.analyze_session(data, 1)
    meta, _ss = XL.detail_meta(sa, "TEMP_DISCLAIMER")
    meta["source_notes"] = [n for n in meta["source_notes"] if n != "TEMP_DISCLAIMER"]
    cid = sa["cert_id"]
    fn = f"認證測試報告_{cid}_{no:02d}{suffix}.xlsx"
    p = os.path.join(d, "reports", fn)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    save_report(sa["res"], meta, p)
    stp = os.path.join(d, "cert_state.json")
    st = json.load(open(stp, encoding="utf-8"))
    st["reports"].append({"file": f"reports\\{fn}", "at": at or st["ended_at"].replace("T", " ")})
    _w(stp, json.dumps(st, ensure_ascii=False, indent=2))
    return p


STD = [("I", 30), ("D", 200), ("I", 90), ("C", 200), ("I", 90)]     # 一個 DC 循環


def run_tool(sources, *, order="DC", versions=(), out=None, hooks=None, now=None, disk_free=None, expect_rc=None):
    out = out or os.path.join(WORK, "out")
    argv = []
    for s in sources:
        argv += ["--source", s]
    for v in versions:
        argv += ["--version", v]
    if order is not None:
        argv += ["--cycle-order", order]
    argv += ["--out-root", out]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = MR.main(argv, active_json=ACTIVE, disk_free=disk_free, now=now, hooks=hooks or {})
    text = buf.getvalue()
    last = json.loads(text.strip().splitlines()[-1]) if text.strip() else {}
    return rc, last, text


_NOW = [datetime(2030, 1, 1, 0, 0, 0)]


def nxt_now():
    _NOW[0] += timedelta(seconds=1)
    return _NOW[0]


def status_of(mdir):
    return json.load(open(os.path.join(mdir, "STATUS.json"), encoding="utf-8"))


def manifest_of(mdir):
    p = os.path.join(mdir, "merge_manifest.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def is_published(mdir):
    """§11.4 判定規則（TP-8）。"""
    try:
        st = status_of(mdir)
    except Exception:                                    # noqa: BLE001
        return False
    if not (st.get("publish", {}).get("state") == "PUBLISHED" and st.get("stage") == "DONE"
            and st.get("overall") in ("COMPLETED", "COMPLETED_WITH_UNVERIFIED")):
        return False
    mp = os.path.join(mdir, "merge_manifest.json")
    if not os.path.exists(mp) or SRC.sha256_file(mp) != st.get("manifest_sha256"):
        return False
    man = manifest_of(mdir)
    for o in man.get("outputs", []):
        p = os.path.join(mdir, o["final"])
        if not os.path.exists(p) or SRC.sha256_file(p) != o["sha256"]:
            return False
    return True


def files_in(mdir, ext):
    out = []
    for d, _ds, fs in os.walk(mdir):
        if "_snapshot" in d:
            continue
        out += [os.path.relpath(os.path.join(d, f), mdir) for f in fs if f.endswith(ext)]
    return sorted(out)


def summary_first_screen(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    ws = wb["整合Summary"]
    lines = [r[0] for r in ws.iter_rows(min_row=1, max_row=14, max_col=1, values_only=True) if r[0]]
    wb.close()
    return lines


def all_text_of_outputs(mdir):
    from openpyxl import load_workbook
    texts = []
    for rel in files_in(mdir, ".xlsx"):
        if os.path.basename(rel).startswith("整合認證報告"):
            wb = load_workbook(os.path.join(mdir, rel), read_only=True)
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    texts += [str(v) for v in row if isinstance(v, str)]
            wb.close()
    for n in ("STATUS.json", "merge_manifest.json"):
        p = os.path.join(mdir, n)
        if os.path.exists(p):
            texts.append(open(p, encoding="utf-8").read())
    return "\n".join(texts)


def writable(p):
    os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


# =============================================================================
# 共同停止條件：寫入守門（--work 以外）
# =============================================================================
_real_open = builtins.open


def _guarded_open(file, mode="r", *a, **k):
    if any(c in mode for c in "wax+") and isinstance(file, (str, bytes, os.PathLike)):
        p = os.path.abspath(os.fsdecode(file))
        if not SRC.is_inside(p, WORK) and not p.lower().endswith(".pyc"):
            raise Abort(f"寫入 --work 以外的路徑：{p}")
    return _real_open(file, mode, *a, **k)


# =============================================================================
# 測試
# =============================================================================
def t_static():
    print("== TN-2 靜態檢查 ==")
    banned_imp = {"cert_runtime", "cert_service", "api_client", "report_monitor", "auto_control", "bess_request",
                  "cert_env", "env_data_scraper"}
    banned_call = {"generate_report", "write_quality_metrics", "write_offgrid_record", "CertRuntime"}
    hits = []
    for m in MR.OWN_MODULES:
        tree = ast.parse(open(os.path.join(HERE, m), encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                hits += [f"{m}: import {a.name}" for a in n.names if a.name.split(".")[0] in banned_imp]
            elif isinstance(n, ast.ImportFrom):
                if (n.module or "").split(".")[0] in banned_imp:
                    hits.append(f"{m}: from {n.module}")
            elif isinstance(n, ast.Call):
                f = n.func
                nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                if nm in banned_call:
                    hits.append(f"{m}: call {nm}")
    check("TN-2", "新模組不 import 禁止模組、不呼叫禁止函式", not hits, str(hits))
    loaded = [m for m in banned_imp if m in sys.modules]
    check("TN-2", "執行期沒有載入禁止模組（含間接）", not loaded, str(loaded))


def t_network():
    print("== TN-1 網路封鎖 ==")
    import socket
    try:
        socket.create_connection(("192.168.128.110", 8080), timeout=1)
        ok = False
    except OSError:
        ok = True
    check("TN-1", "socket 連線一律丟例外", ok)


def t_guard():
    print("== TN-3 寫入守門 ==")
    g = SRC.Guard(os.path.join(WORK, "guard_root"))
    try:
        g.check(os.path.join(WORK, "elsewhere.txt"))
        ok = False
    except PermissionError:
        ok = True
    check("TN-3", "寫到 MERGE 之外 → 例外", ok)


def t_validate():
    print("== T-V 前置驗證 ==")
    a = make_session("CERT_V_A", T0, STD)
    b = make_session("CERT_V_B", T0 + timedelta(days=1), STD)
    out = os.path.join(WORK, "out_v")
    cases = []
    rc, last, _ = run_tool([a], out=out)
    cases.append(("V1", rc == 2 and last.get("code") == "V1"))
    rec = make_session("CERT_V_REC", T0 + timedelta(days=2), STD, report=False)
    stp = os.path.join(rec, "cert_state.json")
    st = json.load(open(stp, encoding="utf-8")); st["status"] = "recording"; _w(stp, json.dumps(st))
    rc, last, _ = run_tool([a, rec], out=out)
    cases.append(("V2", rc == 2 and last.get("code") == "V2"))
    rc, last, _ = run_tool([a, a], out=out)
    cases.append(("V3-path", rc == 2 and last.get("code") == "V3"))
    cp = os.path.join(WORK, "src", "copy", "CERT_V_A2")
    shutil.copytree(a, cp)
    stc = json.load(open(os.path.join(cp, "cert_state.json"), encoding="utf-8")); stc["cert_id"] = "CERT_V_A2"
    _w(os.path.join(cp, "cert_state.json"), json.dumps(stc))
    rc, last, _ = run_tool([a, cp], out=out)
    cases.append(("V3-copy", rc == 2 and last.get("code") in ("V3", "V4")))
    ov = make_session("CERT_V_OV", T0 + timedelta(seconds=100), STD, report=False)
    rc, last, _ = run_tool([a, ov], out=out)
    cases.append(("V4", rc == 2 and last.get("code") == "V4"))
    bad = make_session("CERT_V_V5", T0 + timedelta(days=3), STD, report=False)
    stp = os.path.join(bad, "cert_state.json")
    st = json.load(open(stp, encoding="utf-8")); st["ended_at"] = (T0 + timedelta(days=3, seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
    _w(stp, json.dumps(st))
    rc, last, _ = run_tool([a, bad], out=out)
    cases.append(("V5", rc == 2 and last.get("code") == "V5"))
    rc, last, _ = run_tool([a, b], out=os.path.join(a, "inside"))
    cases.append(("V6", rc == 2 and last.get("code") == "V6"))
    _w(ACTIVE, json.dumps({"cert_id": "CERT_V_B", "updated_at": "x"}))
    rc, last, _ = run_tool([a, b], out=out)
    _w(ACTIVE, json.dumps({"cert_id": None, "updated_at": "x"}))
    cases.append(("V7", rc == 2 and last.get("code") == "V7"))
    rc, last, _ = run_tool([a, b], out=out, versions=["CERT_X=r2b"])
    cases.append(("V8", rc == 2 and last.get("code") == "V8"))
    rc, last, _ = run_tool([a, b], out=out, order=None)
    cases.append(("V9", rc == 2 and last.get("code") == "V9"))
    for cid, ok in cases:
        check("T-V", f"{cid} 拒絕且 rc=2", ok)
    check("T-V", "被拒絕時沒有建立任何 MERGE 資料夾", not os.path.exists(out) or not os.listdir(out))
    rc, last, _ = run_tool([a, b], out=os.path.join(WORK, "out_space"), disk_free=lambda p: 0)
    check("TH-6", "事前空間不足 → rc=2、不建立 MERGE",
          rc == 2 and last.get("code") == "SPACE" and not os.path.exists(os.path.join(WORK, "out_space"))
          or (rc == 2 and not [x for x in os.listdir(os.path.join(WORK, "out_space")) if x.startswith("MERGE_")]))
    return a, b


def t_basic(a, b):
    print("== TA-3／TB-3／TB-5／TB-6／TP-8 正常產報 ==")
    stages = []
    rc, last, text = run_tool([a, b], versions=["CERT_V_A=r2b", "CERT_V_B=r2c"], now=nxt_now(),
                              hooks={"on_stage": stages.append})
    md = last.get("merge_dir")
    st = status_of(md)
    check("TA-3", "全部 VERIFIED → COMPLETED、rc=0", rc == 0 and st["overall"] == "COMPLETED", f"{rc} {st.get('overall')} {st.get('reason')} {st.get('detail')}")
    names = files_in(md, ".xlsx")
    check("TA-3", "無任何後綴", all("未驗證" not in n for n in names) and len(names) == 3, str(names))
    first = summary_first_screen(os.path.join(md, [n for n in names if os.path.basename(n).startswith("整合")][0]))
    check("TA-3", "第一屏仍顯示原始參數 UNVERIFIABLE", any("原始參數：UNVERIFIABLE（原始報告未記錄）" in x for x in first))
    check("TB-3", "第一屏為「產生本檔時的狀態」格式且無最終式措辭",
          first[0].startswith("產生本檔時的狀態") and not any(w in "\n".join(first) for w in
                                                       ("H5 通過", "H4 通過", "已發布", "發布成功") + BANNED))
    man = manifest_of(md)
    check("TB-5", "manifest 含 integrity.h4／h5 與 statement",
          man and man["integrity"].get("h4", {}).get("at") and man["integrity"].get("h5", {}).get("at")
          and man["integrity"].get("statement", "").startswith("來源完整性只驗證到 H4 時點"))
    want = ["SNAPSHOT", "ANALYZE", "WRITE_DETAILS", "VERIFY", "WRITE_SUMMARY", "INTEGRITY", "AFTER_H4", "PUBLISHING", "DONE"]
    check("TB-6", "STATUS stage 序列", stages == want, str(stages))
    check("TP-8", "判定規則：正常完成 → 正式發布", is_published(md))
    check("TH-8", "結束時印出 manifest SHA-256", f"manifest SHA-256：{st['manifest_sha256']}" in text)
    check("TA-7", "輸出文字沒有禁用措辭", not any(w in all_text_of_outputs(md) for w in BANNED))
    sess = st["sessions"]
    check("TA-3", "STATUS 每個 Session 分欄記錄資料值驗證與原始參數可追溯性",
          all(s["data_value_verification"] == "VERIFIED" and s["param_traceability"] == "UNVERIFIABLE" for s in sess))
    # T-X：檔名、標題、描述聲明
    from openpyxl import load_workbook
    det = [n for n in names if "重產明細" in n]
    wb = load_workbook(os.path.join(md, det[0]), read_only=True)
    check("T-X", "重產明細標題與描述第一行為重產聲明",
          wb.properties.title == XL.DETAIL_TITLE and (wb.properties.description or "").startswith(XL.DISCLAIMER_PREFIX))
    check("T-X", "重產明細檔名不是原始報告格式", all(not os.path.basename(n).startswith("認證測試報告_") for n in det))
    check("T-X", "整合活頁簿工作表名稱 ≤31、不重名、9 頁",
          XL.SHEETS == load_workbook(os.path.join(md, [n for n in names if os.path.basename(n).startswith("整合")][0]),
                                     read_only=True).sheetnames and all(len(s) <= 31 for s in XL.SHEETS))
    wb.close()
    # TP-8 竄改正式檔 → 判定無效
    t = os.path.join(md, det[0])
    with _real_open(t, "ab") as f:
        f.write(b"x")
    check("TP-8", "正式檔被改動 → 判定無效", not is_published(md))
    return md


def t_unverifiable_and_failed():
    print("== TA-1／TA-2／TA-6／TA-8 驗證結果與檔名 ==")
    s1 = make_session("CERT_U_1", T0 + timedelta(days=10), STD, report=False)
    s2 = make_session("CERT_U_2", T0 + timedelta(days=11), STD)
    rc, last, _ = run_tool([s1, s2], now=nxt_now())
    md = last["merge_dir"]
    st = status_of(md)
    names = files_in(md, ".xlsx")
    check("TA-2", "S1 UNVERIFIABLE → COMPLETED_WITH_UNVERIFIED、rc=0", rc == 0 and st["overall"] == "COMPLETED_WITH_UNVERIFIED",
          f"{rc} {st['overall']}")
    check("TA-2", "整合報告含 _含未驗證明細；S1 明細含 _資料值未驗證；S2 無後綴",
          any("_含未驗證明細" in n for n in names) and any("S1_CERT_U_1_資料值未驗證" in n for n in names)
          and any(n.endswith("S2_CERT_U_2.xlsx") for n in names), str(names))
    summ = [n for n in names if os.path.basename(n).startswith("整合")][0]
    check("TA-2", "第一屏列出未驗證 Session 與原因",
          any("未驗證 Session：S1" in x for x in summary_first_screen(os.path.join(md, summ))))
    # TA-1／TA-6：S2 原始報告的階段頁改一格 → FAILED
    f1 = make_session("CERT_F_1", T0 + timedelta(days=20), STD)
    f2 = make_session("CERT_F_2", T0 + timedelta(days=21), STD)
    from openpyxl import load_workbook
    rp = os.path.join(f2, "reports", "認證測試報告_CERT_F_2_01.xlsx")
    wb = load_workbook(rp)
    ws = wb.worksheets[3]
    ws["B3"] = 12345.678
    wb.save(rp)
    rc, last, _ = run_tool([f1, f2], now=nxt_now())
    md = last["merge_dir"]
    st = status_of(md)
    check("TA-1", "S2 FAILED → VERIFY_FAILED、rc=1", rc == 1 and st["overall"] == "VERIFY_FAILED", st["overall"])
    check("TA-1", "整合報告與兩份重產明細都是 .partial", files_in(md, ".xlsx") and
          all(n.endswith(".partial.xlsx") for n in files_in(md, ".xlsx")), str(files_in(md, ".xlsx")))
    man = manifest_of(md)
    v2 = [s for s in man["sources"] if s["cert_id"] == "CERT_F_2"][0]["data_value_verification"]
    check("TA-1", "manifest 有差異明細與原因", v2["result"] == "FAILED" and v2["reason_codes"])
    check("TA-6", "數值差異 → UNEXPLAINED_VALUE_DIFF（含 X6 附註），不是 UNVERIFIABLE",
          "UNEXPLAINED_VALUE_DIFF" in v2["reason_codes"]
          and any("X6" in json.dumps(i.get("detail"), ensure_ascii=False) for i in v2["items"]))
    check("TP-8", "VERIFY_FAILED → 判定無效", not is_published(md))
    # TA-8：公式／表格範圍／已使用範圍
    dims_info = {}

    def grow_used_range(w):
        """在原使用範圍之外（最後一列再往下 5 列）實際寫入一格，並記錄修改前後的使用範圍。"""
        ws = w.worksheets[3]
        before = (ws.max_row, ws.max_column)
        ws.cell(row=ws.max_row + 5, column=1, value="x")
        dims_info["before"], dims_info["after"] = before, (ws.max_row, ws.max_column)
    for tag, mut, code in (("formula", lambda w: w.worksheets[3].__setitem__("B3", "=1+1"), "FORMULA_DIFF"),
                           ("table", None, "TABLE_MISMATCH"),
                           ("dims", grow_used_range, "STRUCTURE")):
        g1 = make_session(f"CERT_G_{tag}_1", T0 + timedelta(days=30 + len(tag)), STD)
        g2 = make_session(f"CERT_G_{tag}_2", T0 + timedelta(days=40 + len(tag)), STD)
        rp = os.path.join(g2, "reports", f"認證測試報告_CERT_G_{tag}_2_01.xlsx")
        wb = load_workbook(rp)
        if tag == "table":
            ws = [w for w in wb.worksheets if w.tables][0]
            t = list(ws.tables.values())[0]
            from openpyxl.utils.cell import range_boundaries, get_column_letter
            c1, r1, c2, r2 = range_boundaries(t.ref)
            t.ref = f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2 - 1}"
        else:
            mut(wb)
        wb.save(rp)
        if tag == "dims":
            wb2 = load_workbook(rp)
            saved_dims = (wb2.worksheets[3].max_row, wb2.worksheets[3].max_column)
            check("TA-8", "dims 測試資料：修改前後使用範圍確實改變（含存檔後重新讀取）",
                  dims_info.get("after", (0, 0))[0] > dims_info.get("before", (0, 0))[0]
                  and saved_dims == dims_info.get("after"), f"{dims_info} saved={saved_dims}")
        rc, last, _ = run_tool([g1, g2], now=nxt_now())
        st = status_of(last["merge_dir"])
        codes = [s for s in st["sessions"] if s["cert_id"].endswith("_2")][0]["reason_codes"]
        check("TA-8", f"{tag} 不同 → FAILED（{code}）", st["overall"] == "VERIFY_FAILED" and code in (codes or []), str(codes))


def t_integrity():
    print("== TH-1～TH-5、TA-5、TB-1 快照完整性 ==")
    out = []

    def pair(tag):
        x = make_session(f"CERT_H_{tag}_1", T0 + timedelta(days=60 + len(out) * 2), STD)
        y = make_session(f"CERT_H_{tag}_2", T0 + timedelta(days=61 + len(out) * 2), STD)
        out.append(tag)
        return x, y

    x, y = pair("h1")
    rc, last, _ = run_tool([x, y], now=nxt_now())
    man = manifest_of(last["merge_dir"])
    ok = all(f["h1"] == SRC.sha256_file(os.path.join(s["source_path"], f["path"])) for s in man["sources"] for f in s["files"])
    check("TH-1", "H1 逐檔雜湊＝來源實際雜湊（H1 為比對基準）", ok)
    x, y = pair("h2")

    def corrupt_snapshot():
        p = os.path.join(WORK, "out")
        md = sorted(d for d in os.listdir(p) if d.startswith("MERGE_"))[-1]
        f = os.path.join(p, md, "_snapshot", "CERT_H_h2_1", "cert_state.json")
        with _real_open(f, "a", encoding="utf-8") as fh:
            fh.write(" ")
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"after_copy": corrupt_snapshot})
    st = status_of(last["merge_dir"])
    check("TH-2", "H2 不一致 → SNAPSHOT_MISMATCH、無報告檔", st["overall"] == "SNAPSHOT_MISMATCH" and not files_in(last["merge_dir"], ".xlsx"))
    x, y = pair("h3")
    rc, last, _ = run_tool([x, y], now=nxt_now(),
                           hooks={"after_copy": lambda: _real_open(os.path.join(x, "events.jsonl"), "a").write("{}\n")})
    st = status_of(last["merge_dir"])
    check("TH-3", "H3 不一致 → SOURCE_CHANGED_DURING_SNAPSHOT、無報告檔",
          st["overall"] == "SOURCE_CHANGED_DURING_SNAPSHOT" and not files_in(last["merge_dir"], ".xlsx"))
    x, y = pair("h4")
    rc, last, _ = run_tool([x, y], now=nxt_now(),
                           hooks={"before_h4": lambda s: _real_open(os.path.join(x, "events.jsonl"), "a").write("{}\n")})
    md = last["merge_dir"]
    st = status_of(md)
    man = manifest_of(md)
    check("TH-4／TB-1", "H4 不一致 → SOURCE_CHANGED、全部 .partial、manifest 含 H4 時間與清單",
          st["overall"] == "SOURCE_CHANGED" and all(n.endswith(".partial.xlsx") for n in files_in(md, ".xlsx"))
          and man["integrity"]["h4"]["diffs"] and man["integrity"]["h4"]["at"])
    x, y = pair("h5")

    def touch_snap(s):
        f = os.path.join(s["sessions"]["CERT_H_h5_1"]["dst"], "cert_state.json")
        writable(f)
        with _real_open(f, "a", encoding="utf-8") as fh:
            fh.write(" ")
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"before_h5": touch_snap})
    md = last["merge_dir"]
    st = status_of(md)
    summ = [n for n in files_in(md, ".xlsx") if os.path.basename(n).startswith("整合")][0]
    first = "\n".join(summary_first_screen(os.path.join(md, summ)))
    check("TH-5／TA-5", "H5 不一致 → SNAPSHOT_CHANGED、全部 .partial",
          st["overall"] == "SNAPSHOT_CHANGED" and all(n.endswith(".partial.xlsx") for n in files_in(md, ".xlsx")))
    check("TA-5", "第一屏沒有「H4／H5 通過」「已發布」措辭", not any(w in first for w in ("H5 通過", "H4 通過", "已發布", "發布成功")))
    check("TA-5", "STATUS 記錄不一致檔案與「快照不可信，只供調查」",
          st["integrity"]["h5"].get("note") == "依據的快照不可信，只供調查" and st["integrity"]["h5"]["diffs"])
    # TH-7：複製中磁碟滿
    x, y = pair("h7")
    real = shutil.copyfileobj
    cnt = [0]

    def boom(fi, fo, n=0):
        cnt[0] += 1
        if cnt[0] == 2:
            e = OSError(28, "No space left on device")
            e.errno = 28
            raise e
        return real(fi, fo, n) if n else real(fi, fo)
    shutil.copyfileobj = boom
    try:
        rc, last, _ = run_tool([x, y], now=nxt_now())
    finally:
        shutil.copyfileobj = real
    st = status_of(last["merge_dir"])
    check("TH-7", "複製中磁碟滿 → FAILED（disk_full）、無報告檔、來源不變",
          st["overall"] == "FAILED" and st["reason"] == "disk_full" and not files_in(last["merge_dir"], ".xlsx"))


def t_publish():
    print("== TP-1～TP-7、TB-2、TB-4、TA-4 發布協定 ==")
    seq = [0]

    def pair():
        seq[0] += 1
        return (make_session(f"CERT_P{seq[0]}_1", T0 + timedelta(days=100 + seq[0] * 2), STD),
                make_session(f"CERT_P{seq[0]}_2", T0 + timedelta(days=101 + seq[0] * 2), STD))

    def failing_rename(n_fail):
        c = [0]

        def r(a, b):
            c[0] += 1
            if c[0] == n_fail:
                raise OSError("（測試注入）改名失敗")
            os.rename(a, b)
        return r
    x, y = pair()
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"rename": failing_rename(2)})
    md = last["merge_dir"]
    st = status_of(md)
    jl = [json.loads(l) for l in _real_open(os.path.join(md, "publish_journal.jsonl"), encoding="utf-8")]
    check("TP-1", "第 2 個改名失敗 → PUBLISH_FAILED／ROLLED_BACK、日誌完整",
          st["overall"] == "PUBLISH_FAILED" and st["publish"]["state"] == "ROLLED_BACK"
          and [j["op"] for j in jl] == ["rename", "rollback"] and all(n.endswith(".partial.xlsx") for n in files_in(md, ".xlsx")))
    check("TP-8", "PUBLISH_FAILED → 判定無效", not is_published(md))
    x, y = pair()

    def bad_back(a, b):
        raise OSError("（測試注入）回復失敗")
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"rename": failing_rename(2), "rename_back": bad_back})
    st = status_of(last["merge_dir"])
    check("TP-2", "回復也失敗 → PUBLISH_INCONSISTENT、列出實際檔名、無效",
          st["overall"] == "PUBLISH_INCONSISTENT" and st["publish"]["state"] == "INCONSISTENT"
          and st["publish"].get("actual_names") and not is_published(last["merge_dir"]))
    x, y = pair()
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={
        "fail_status_write": lambda s: s.get("stage") == "DONE" and s["publish"].get("state") == "PUBLISHED"})
    md = last["merge_dir"]
    st = status_of(md)
    check("TP-3", "寫最終 STATUS 失敗 → 回復改名、STATUS 非 PUBLISHED、無效",
          st["publish"]["state"] in ("ROLLED_BACK", "PUBLISHING") and not is_published(md)
          and all(n.endswith(".partial.xlsx") for n in files_in(md, ".xlsx")))
    x, y = pair()
    rc, last, text = run_tool([x, y], now=nxt_now(), hooks={"fail_status_write": lambda s: s.get("stage") == "PUBLISHING"})
    md = [os.path.join(WORK, "out", d) for d in sorted(os.listdir(os.path.join(WORK, "out")))][-1]
    check("TP-4", "預寫 PUBLISHING 失敗 → 沒有任何改名、無效",
          all(n.endswith(".partial.xlsx") for n in files_in(md, ".xlsx")) and not os.path.exists(
              os.path.join(md, "publish_journal.jsonl")) and not is_published(md))
    x, y = pair()
    marker = {}

    def pre_place(mdir, outputs):
        f = os.path.join(mdir, outputs[0]["final"])
        with _real_open(f, "w", encoding="utf-8") as fh:
            fh.write("existing")
        marker["f"] = f
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"after_f9": pre_place})
    st = status_of(last["merge_dir"])
    check("TP-5", "預定正式檔名已存在 → 不發布、不覆蓋",
          st["overall"] == "FAILED" and st["reason"] == "final_name_exists"
          and _real_open(marker["f"], encoding="utf-8").read() == "existing")
    x, y = pair()
    tamper = {}

    def change_field(p):
        """改 manifest 的實際欄位值（仍為合法 JSON，內容與預期不一致）。"""
        with _real_open(p, encoding="utf-8") as fh:
            m = json.load(fh)
        m["overall"] = "TAMPERED"
        with _real_open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(m, ensure_ascii=False, indent=1))
        with _real_open(p, encoding="utf-8") as fh:
            tamper["valid_json"] = json.load(fh).get("overall") == "TAMPERED"
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"tamper_manifest": change_field})
    st = status_of(last["merge_dir"])
    check("TP-6", "manifest 欄位值被改（仍為合法 JSON）→ FAILED（manifest_write_failed）、不發布、無正式檔名",
          tamper.get("valid_json") and st["overall"] == "FAILED" and st["reason"] == "manifest_write_failed"
          and not is_published(last["merge_dir"]) and st["publish"]["state"] != "PUBLISHED"
          and all(n.endswith(".partial.xlsx") for n in files_in(last["merge_dir"], ".xlsx")), f"{tamper} {st['reason']}")
    # TP-6b：manifest 雜湊記錄之後（STATUS 已預寫 PUBLISHING）任何位元組變更 → R2 偵測 → 回復、不發布
    x, y = pair()
    seen = {}

    def rename_with_byte_change(src, dst):
        if os.path.basename(src) == "merge_manifest.json.partial":
            st_now = status_of(os.path.dirname(src))
            seen["recorded_sha"] = st_now.get("manifest_sha256")
            with _real_open(src, "ab") as fh:
                fh.write(b" ")                     # 只改一個位元組（JSON 內容不變）
            seen["sha_after_change"] = SRC.sha256_file(src)
        os.rename(src, dst)
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"rename": rename_with_byte_change})
    st = status_of(last["merge_dir"])
    check("TP-6b", "manifest 雜湊記錄後只改 1 個位元組 → post_rename_hash_mismatch → PUBLISH_FAILED、判定無效",
          seen.get("recorded_sha") and seen.get("sha_after_change") != seen.get("recorded_sha")
          and st["overall"] == "PUBLISH_FAILED" and st["reason"] == "post_rename_hash_mismatch"
          and not is_published(last["merge_dir"]), f"{seen} {st.get('overall')} {st.get('reason')}")
    x, y = pair()

    def die_rename():
        c = [0]

        def r(a, b):
            c[0] += 1
            if c[0] == 2:
                raise SystemExit("（測試注入）程序終止")
            os.rename(a, b)
        return r
    now = nxt_now()
    try:
        run_tool([x, y], now=now, hooks={"rename": die_rename()})
    except SystemExit:
        pass
    md = os.path.join(WORK, "out", f"MERGE_{now:%Y%m%d_%H%M%S}")
    st = status_of(md)
    check("TP-7／TB-6", "R1 中途終止 → STATUS 停在 PUBLISHING、無效", st["stage"] == "PUBLISHING" and not is_published(md))
    rc, last, _ = run_tool([x, y], now=nxt_now())
    check("TP-7", "下次執行建立新資料夾、不碰舊資料夾", last["merge_dir"] != md and status_of(md)["stage"] == "PUBLISHING")
    # TB-4：F9 之後改動 .partial
    x, y = pair()
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={
        "after_f9": lambda mdir, outs: _real_open(os.path.join(mdir, outs[0]["partial"]), "ab").write(b"x")})
    st = status_of(last["merge_dir"])
    check("TB-4", "F9 之後內容被改 → R2 不符 → 回復 → PUBLISH_FAILED",
          st["overall"] == "PUBLISH_FAILED" and st["reason"] == "post_rename_hash_mismatch")
    # TB-2：H4 之後不開啟來源；F4 之後不寫快照
    x, y = pair()
    stage = {"s": None}
    opened = []
    srcs = (SRC.norm(x), SRC.norm(y))

    def spy(file, mode="r", *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            p = SRC.norm(os.fsdecode(file))
            if stage["s"] == "AFTER" and any(p.startswith(s) for s in srcs):
                opened.append(("src", p))
            if stage["s"] in ("ANALYZE", "WRITE_DETAILS", "VERIFY", "WRITE_SUMMARY", "INTEGRITY", "AFTER") \
                    and "_snapshot" in p and any(c in mode for c in "wax+"):
                opened.append(("snapwrite", p))
        return _guarded_open(file, mode, *a, **k)

    def on_stage(s):
        stage["s"] = "AFTER" if s in ("AFTER_H4", "PUBLISHING", "DONE") else s
    builtins.open = spy
    try:
        rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"on_stage": on_stage})
    finally:
        builtins.open = _guarded_open
    check("TB-2", "H4 之後開啟來源 0 次、F4 之後寫入快照 0 次", rc == 0 and not opened, str(opened[:3]))
    # TA-4：I6 失敗
    x, y = pair()

    def dup(sessions):
        c = sessions[0]["cycles"][0]
        sessions[1]["cycles"].append(dict(c, id="DUP"))
    rc, last, _ = run_tool([x, y], now=nxt_now(), hooks={"after_analysis": dup})
    st = status_of(last["merge_dir"])
    check("TA-4／T8-5", "同一階段出現在兩個循環 → FAILED（count_invariant）、沒有報告檔",
          st["overall"] == "FAILED" and st["reason"] == "count_invariant" and not st["count_invariants"]["I6"]
          and not files_in(last["merge_dir"], ".xlsx"))
    # TH-8：manifest 已寫出後失敗仍印出雜湊（VERIFY_FAILED 路徑中最終 STATUS 寫入失敗）
    x, y = pair()
    rc, last, text = run_tool([x, y], now=nxt_now(), hooks={
        "after_verify": lambda sa, v: dict(v, result="FAILED", reason_codes=["UNEXPLAINED_VALUE_DIFF"], display="FAILED"),
        "fail_status_write": lambda s: s.get("stage") == "DONE"})
    check("TH-8", "manifest 已寫出後失敗 → 仍印出 manifest SHA-256", "manifest SHA-256：" in text)


def t_paths():
    print("== T11-1／T11-2／TA-10 路徑 ==")
    a = make_session("CERT_L_1", T0 + timedelta(days=200), STD)
    b = make_session("CERT_L_2", T0 + timedelta(days=201), STD)
    deep = os.path.join(WORK, "d" * 60, "e" * 60, "f" * 40)
    rc, last, _ = run_tool([a, b], out=deep, now=nxt_now())
    check("T11-1／TA-10", "最長輸出路徑 > 240 → rc=2、不建立資料夾",
          rc == 2 and last.get("code") == "PATH" and (not os.path.exists(deep) or not os.listdir(deep)))
    now = nxt_now()
    md = os.path.join(WORK, "out", f"MERGE_{now:%Y%m%d_%H%M%S}")
    os.makedirs(md)
    _w(os.path.join(md, "keep.txt"), "keep")
    rc, last, _ = run_tool([a, b], now=now)
    check("T11-2", "MERGE_<ts> 已存在 → rc=2、既有內容不變",
          rc == 2 and os.listdir(md) == ["keep.txt"] and _real_open(os.path.join(md, "keep.txt")).read() == "keep")


# ---- 分析單元測試（合成 Session；不經完整流程）-------------------------------
def analyzed(name, start, pattern, order="DC", **kw):
    d = make_session(name, start, pattern, report=False, **kw)
    sa = AN.analyze_session(SRC.load_session(d, os.path.dirname(d)), 1)
    AN.pair_session(sa, order)
    return sa


class FS:
    """假階段（配對與確認邏輯單元測試）。"""

    def __init__(self, no, d, t, *, sc=True, ec=True, end_by="idle_confirmed", label=""):
        self.no, self.dir, self.label, self.end_by = no, d, label, end_by
        self.start_certain, self.end_certain = sc, ec
        self.start_note = self.end_note = "（測試）"
        self.start = type("O", (), {"t": t, "values": {"soc": 50}})()
        self.A = type("O", (), {"t": t + timedelta(seconds=100), "values": {"soc": 50}})()
        self.end_B = type("O", (), {"t": t + timedelta(seconds=101)})()
        self.pauses = []


def fake_session(spec, order="DC", decisions=(), incomplete=None, cid="CERT_FAKE"):
    """spec：[(方向, 標記, 選項 dict), ...]"""
    stages, t = [], T0
    for i, (d, lab, opt) in enumerate(spec, 1):
        stages.append(FS(i, d, t, label=lab, **opt))
        t += timedelta(seconds=300)
    res = type("R", (), {"stages": stages, "pauses": [], "gaps": []})()
    sa = {"cert_id": cid, "s_no": 1, "state": {"incomplete": incomplete}, "res": res,
          "data": {"decisions": list(decisions)}, "stages": []}
    for i, s in enumerate(stages):
        sa["stages"].append({"id": f"{cid}#ST{s.no}", "cert_id": cid, "s_no": 1, "idx": i, "no": s.no, "dir": s.dir,
                             "name": "", "stage": s, "stage_start": s.start.t.strftime("%Y-%m-%dT%H:%M:%S")})
    decs = []
    for s in sa["stages"]:
        if s["stage"].label:
            decs.append({"type": "stage_label", "stage_start": s["stage_start"], "label": s["stage"].label,
                         "at": "2026-12-01 12:00:00"})
    sa["data"]["decisions"] = decs + list(decisions)
    AN._labels(sa)
    AN._orphans(sa)
    for s in sa["stages"]:
        s["pending_pauses"] = s["certain_pauses"] = s["uncertain_pauses"] = 0
    sa["uncertain_pauses"] = []
    AN.pair_session(sa, order)
    return sa


def _cy(sa):
    return [(c["certainty"], c["status"]) for c in sa["cycles"]]


def _pairings(sa):
    return [s["pairing"] for s in sa["stages"]]


def t_pairing_units():
    print("== T-K1～K19、T-A、T7、T9 配對與確認（假階段） ==")
    N = {}
    sa = fake_session([("D", "滿放", N), ("C", "滿充", N)])
    check("T-K1", "DC 正確標記 → 確認完成", _cy(sa) == [(AN.CERTAIN, AN.ST_DONE)], str(_cy(sa)))
    sa = fake_session([("C", "滿充", N), ("D", "滿放", N)], order="CD")
    check("T-K2", "CD 正確標記 → 確認完成", _cy(sa) == [(AN.CERTAIN, AN.ST_DONE)])
    sa = fake_session([("C", "", N), ("D", "", N)])
    check("T-K3", "順序 DC、資料 C、D → 開頭＋結尾半循環、0 循環", not sa["cycles"] and _pairings(sa) == [AN.UNP_HEAD, AN.UNP_TAIL])
    sa = fake_session([("C", "", N), ("D", "滿放", N), ("C", "滿充", N)])
    check("T-K4", "開頭 C 未配對；1 循環確認完成", _pairings(sa)[0] == AN.UNP_HEAD and _cy(sa) == [(AN.CERTAIN, AN.ST_DONE)])
    sa = fake_session([("D", "滿放", N), ("C", "滿充", N), ("D", "", N)])
    check("T-K5", "結尾 D 未配對", _pairings(sa)[2] == AN.UNP_TAIL and _cy(sa) == [(AN.CERTAIN, AN.ST_DONE)])
    sa = fake_session([("D", "滿放", N), ("C", "滿充", N)] * 3)
    check("T-K6", "3 循環確認完成", _cy(sa) == [(AN.CERTAIN, AN.ST_DONE)] * 3)
    sa = fake_session([("D", "滿放", N), ("C", "未滿", N), ("D", "滿放", N), ("C", "滿充", N)])
    check("T-K7", "未滿、確認完成", [c[1] for c in _cy(sa)] == [AN.ST_NOTFULL, AN.ST_DONE])
    sa = fake_session([("D", "中止", N), ("C", "滿充", N)])
    check("T-K8", "中止", [c[1] for c in _cy(sa)] == [AN.ST_ABORT])
    sa = fake_session([("D", "滿放", N), ("C", "", N)])
    check("T-K9", "後半未標記 → 待確認", [c[1] for c in _cy(sa)] == [AN.ST_PENDING])
    sa = fake_session([("D", "滿放", N), ("C", "滿充", {"end_by": "data_end", "ec": False})])
    check("T-K10／T7-5", "data_end → 未完成", [c[1] for c in _cy(sa)] == [AN.ST_INCOMPLETE])
    sa = fake_session([("D", "", N)])
    check("T-K11", "只有 D → 0 循環、結尾半循環", not sa["cycles"] and _pairings(sa) == [AN.UNP_TAIL])
    sa = fake_session([])
    check("T-K12", "沒有階段 → 0 循環", not sa["cycles"] and not sa["stages"])
    sa = fake_session([("D", "", {"end_by": "operator_split", "ec": False}), ("D", "", {"sc": False}), ("C", "", N)])
    check("T-K14", "人工 split：D1a 未配對（其後同向）；(D1b, C) 配對待確認（A1）且未完成",
          _pairings(sa)[0] == AN.UNP_SAME and _cy(sa) == [(AN.PAIR_PENDING, AN.ST_INCOMPLETE)], str(_cy(sa)))
    sa = fake_session([("D", "", N), ("D", "滿放", N), ("C", "滿充", N)])
    check("T-K15／T-A", "待機分段：配對待確認（A1），標記正確仍為待確認",
          _cy(sa) == [(AN.PAIR_PENDING, AN.ST_PENDING)] and sa["cycles"][0]["certainty_triggers"][0].startswith("A1"))
    sa = fake_session([("D", "滿放", N), ("C", "", N), ("C", "滿充", N)])
    check("T-K16／T-A", "充電被分段：配對待確認（A2）；第二個 C＝未配對：前無可配的前半階段",
          _cy(sa) == [(AN.PAIR_PENDING, AN.ST_PENDING)] and _pairings(sa)[2] == AN.UNP_NOPREV
          and sa["cycles"][0]["certainty_triggers"][0].startswith("A2"))
    sa = fake_session([("D", "滿放", {"sc": False}), ("C", "滿充", N)])
    check("T-K17／T7-2", "E1 不確定（標記正確）→ 未完成", [c[1] for c in _cy(sa)] == [AN.ST_INCOMPLETE])
    sa = fake_session([("D", "滿放", {"ec": False, "end_by": "opposite"}), ("C", "滿充", N)])
    check("T7-3", "E2 不確定 → 未完成", [c[1] for c in _cy(sa)] == [AN.ST_INCOMPLETE]
          and any("E2" in r for r in sa["cycles"][0]["reasons"]))
    sa = fake_session([("D", "滿放", N), ("C", "滿充", {"sc": False})])
    check("T7-4", "E3 不確定 → 未完成", [c[1] for c in _cy(sa)] == [AN.ST_INCOMPLETE]
          and any("E3" in r for r in sa["cycles"][0]["reasons"]))
    sa = fake_session([("D", "滿放", N), ("C", "滿充", N)])
    check("T7-1", "四邊界確定、DC 標記正確 → 確認完成", _cy(sa) == [(AN.CERTAIN, AN.ST_DONE)])
    st0 = T0.strftime("%Y-%m-%dT%H:%M:%S")
    sa = fake_session([("D", "", N), ("C", "滿充", N)], decisions=[
        {"type": "stage_label", "stage_start": st0, "label": "滿充", "at": "2026-12-01 12:00:00"}])
    s0 = sa["stages"][0]
    check("T7-6", "放電標滿充（唯一一筆）→ 無有效標記、待確認、列為無效",
          s0["effective_label"] is None and _cy(sa)[0][1] == AN.ST_PENDING
          and sa["label_history"][st0][0]["validity"] == "無效：方向不符")
    sa = fake_session([("D", "", N), ("C", "滿充", N)], decisions=[
        {"type": "stage_label", "stage_start": st0, "label": "全滿", "at": "2026-12-01 12:00:00"}])
    check("T7-7", "未知標記值 → 無效：未知值", sa["label_history"][st0][0]["validity"] == "無效：未知值"
          and sa["stages"][0]["effective_label"] is None)
    sa = fake_session([("D", "", N), ("C", "滿充", N)], decisions=[
        {"type": "stage_label", "stage_start": st0, "label": x, "at": f"2026-12-01 12:0{i}:00"}
        for i, x in enumerate(("滿放", "未滿", "滿放"))])
    check("T7-8", "滿放→未滿→滿放 → 有效＝滿放、歷史 3 筆",
          sa["stages"][0]["effective_label"] == "滿放" and len(sa["label_history"][st0]) == 3)
    sa = fake_session([("D", "", N), ("C", "滿充", N)], decisions=[
        {"type": "stage_label", "stage_start": st0, "label": "滿放", "at": "2026-12-01 12:00:00"},
        {"type": "stage_label", "stage_start": st0, "label": "滿充", "at": "2026-12-01 12:01:00"}])
    check("T7-9", "最後一筆方向不符 → 無有效標記（不回退）、待確認",
          sa["stages"][0]["effective_label"] is None and _cy(sa)[0][1] == AN.ST_PENDING
          and any("最新標記無效" in r for r in sa["cycles"][0]["reasons"]))
    sa = fake_session([("D", "", N), ("C", "滿充", N)], decisions=[
        {"type": "stage_label", "stage_start": st0, "label": "滿放", "at": "2026-12-01 12:05:00"},
        {"type": "stage_label", "stage_start": st0, "label": "滿放", "at": "2026-12-01 12:01:00"}])
    check("T7-10", "行序與 at 顛倒 → 照行序；待確認（時間順序異常）",
          sa["stages"][0]["effective_label"] == "滿放" and _cy(sa)[0][1] == AN.ST_PENDING
          and any("時間順序異常" in r for r in sa["cycles"][0]["reasons"]))
    sa = fake_session([("D", "", N), ("D", "滿放", N), ("C", "滿充", N)])
    check("T7-11", "A1 且標記正確、邊界確定 → 待確認；配對待確認（兩欄分開）",
          sa["cycles"][0]["status"] == AN.ST_PENDING and sa["cycles"][0]["certainty"] == AN.PAIR_PENDING)
    sa = fake_session([("D", "中止", N), ("C", "滿充", {"end_by": "data_end", "ec": False})])
    check("T7-12", "中止＋未完成 → 中止，理由同時列出", sa["cycles"][0]["status"] == AN.ST_ABORT
          and any("[未完成]" in r for r in sa["cycles"][0]["reasons"]))
    sa = fake_session([("D", "滿放", N), ("C", "滿充", N)], decisions=[
        {"type": "continuity", "item_id": f"U-{(T0 + timedelta(seconds=50)):%Y%m%d%H%M%S}-{(T0 + timedelta(seconds=60)):%Y%m%d%H%M%S}",
         "choice": "merge", "at": "2026-12-01 12:00:00"}])
    check("T7-13", "無法對應的 continuity 決策落在循環時間內 → 待確認並列出",
          sa["cycles"][0]["status"] == AN.ST_PENDING and sa["orphans"] and sa["orphans"][0]["kind"] == "無法對應的停頓決策")
    sa = fake_session([("D", "滿放", N), ("C", "滿充", N)], decisions=[
        {"type": "stage_label", "stage_start": "2020-01-01T00:00:00", "label": "滿放", "at": "x"}])
    check("T9-1", "stage_start 對不到 → 無法對應的標記、不套用", sa["orphans"][0]["kind"] == "無法對應的標記")
    sa = fake_session([("D", "滿放", N), ("C", "滿充", N)], decisions=[{"type": "something", "at": "x"}])
    check("T9-2", "未知 type → 無法辨識的紀錄", sa["orphans"][0]["kind"] == "無法辨識的紀錄"
          and sa["cycles"][0]["status"] == AN.ST_PENDING)
    # 跨 Session 不配對（K18）：兩個獨立 Session
    s1 = fake_session([("D", "滿放", N)], cid="CERT_K18_1")
    s2 = fake_session([("C", "滿充", N)], cid="CERT_K18_2")
    check("T-K18", "跨 Session 不配對：S1 結尾半循環、S2 開頭半循環、0 循環",
          not s1["cycles"] and not s2["cycles"] and _pairings(s1) == [AN.UNP_TAIL] and _pairings(s2) == [AN.UNP_HEAD])
    inv, counts = AN.invariants([fake_session([("D", "滿放", N), ("C", "滿充", N), ("D", "", N)])])
    check("T8-4／T-K20", "I1～I8 正常資料全部成立", all(inv.values()), str(inv))


def t_titles():
    print("== T8-1～T8-3 標題 ==")
    mk = lambda specs: [{"state": {"test_info": {"cabinet_spec": s}}} for s in specs]   # noqa: E731
    check("T8-1", "規格相同 → 標題含規格", AN.title_spec(mk(["250KW", "250KW"])) == "250KW")
    check("T8-2", "規格不同 → 電櫃規格不一致", AN.title_spec(mk(["250KW", "340KW"])) == "電櫃規格不一致")
    check("T8-3", "全部未設定 → 電櫃規格未設定", AN.title_spec(mk(["", None])) == "電櫃規格未設定")


def t_engine_metrics():
    print("== T22 時間量與可比性（合成觀測） ==")
    sa = analyzed("CERT_M_1", T0 + timedelta(days=300), STD)
    s = sa["stages"]
    c = sa["cycles"][0] if sa["cycles"] else None
    check("T22-1", "邊界確定 → 跨度＝A−start", s and s[0]["span_sec"] == (s[0]["stage"].A.t - s[0]["stage"].start.t).total_seconds())
    sa2 = analyzed("CERT_M_2", T0 + timedelta(days=301), [("D", 200), ("I", 90), ("C", 200), ("I", 90)])
    s2 = sa2["stages"][0]
    check("T22-2", "開始不確定 → N/A＋下限欄", s2["span_sec"] is None and s2["span_lower_sec"] is not None)
    sa3 = analyzed("CERT_M_3", T0 + timedelta(days=302), STD, fail_secs=set(range(260, 275)))
    c3 = sa3["cycles"][0] if sa3["cycles"] else None
    check("T22-3", "休息期間狀態未知 → 休息 N/A、觀測間隔照列", c3 and c3["rest_sec"] is None and c3["observed_gap_sec"] > 0,
          str(c3 and (c3["rest_sec"], c3["rest_note"])))
    sa4 = analyzed("CERT_M_4", T0 + timedelta(days=303), [("I", 30), ("D", 200), ("C", 200), ("I", 90)])
    c4 = sa4["cycles"][0] if sa4["cycles"] else None
    check("T22-4", "未觀測到待機 → 休息 N/A", c4 and c4["rest_sec"] is None, str(c4 and c4["rest_note"]))
    p2 = dict(CFG.params(), GAP_G_SEC=45)
    sa5 = analyzed("CERT_M_5", T0 + timedelta(days=304), STD, params=p2)
    AN.comparability([sa, sa5], "DC")
    check("T22-5", "params 不同 → 不可比", sa["cycles"][0]["time_comparable"].startswith("不可比")
          and sa["cycles"][0]["ext_comparable"].startswith("不可比"))
    sa6 = analyzed("CERT_M_6", T0 + timedelta(days=305), STD, hmi_reader="read_cert", ep_ms=5)
    check("T22-6", "read_cert vs 未記錄 → 端點失敗類不可比", AN.endpoint_comparability([sa, sa6]).startswith("不可比"))
    check("T22-9", "極值算法有值（與重產明細同函式）", s[0]["extremes"]["rack_max_c"] == 30.0 and s[0]["extremes"]["cell_vmin_v"] == 3.3)
    v = AN.version_info(sa6, "r2b")
    check("T-S5", "宣告 r2b 但具 r2c 特徵 → 不一致（警示）、驗證狀態未驗證",
          v["consistency"].startswith("不一致") and v["version_verification"] == "未驗證")


def t_verify_cases():
    print("== T23 資料值驗證 ==")
    from openpyxl import load_workbook
    base = T0 + timedelta(days=400)
    v2 = make_session("CERT_X_2", base + timedelta(days=1), STD)
    # ---- T23-2／T23-3（REV-1 解除暫停）：放電結束後 10 秒 PCS 放電旗標延遲（電流 0、旗標仍 True） ----
    pat = [("I", 30), ("D", 200), ("I", 90), ("C", 200), ("I", 90)]
    fd = set(range(230, 240))

    def like_r2b(rp, extra=None):
        """把原始報告中旗標延遲列（狀態 N/A、電流 0）改成整列沿用前一列（模擬 r2b 行為）；extra 可再做其他改動。"""
        wb = load_workbook(rp)
        ws = wb["Raw Data"]
        rows = list(ws.iter_rows(min_row=2))
        idx = []
        for i, r in enumerate(rows):
            if r[1].value == "N/A" and i > 0 and isinstance(r[3].value, (int, float)) and r[3].value == 0:
                for c in range(1, len(r)):
                    r[c].value = rows[i - 1][c].value
                idx.append(i)
        if extra:
            extra(rows, idx)
        wb.save(rp)
        return idx

    def vr_of(md, cid):
        man = manifest_of(md)
        v = [s for s in man["sources"] if s["cert_id"] == cid][0]["data_value_verification"]
        return v, [i for i in v["items"] if i["item"] == "V-R Raw Data"][0]
    x1 = make_session("CERT_X_1", base, pat, flag_delay=fd)
    n = like_r2b(os.path.join(x1, "reports", "認證測試報告_CERT_X_1_01.xlsx"))
    rc, last, _ = run_tool([x1, v2], now=nxt_now())
    v, vr = vr_of(last["merge_dir"], "CERT_X_1")
    check("T23-2", "只有符合 REV-1 X1 (a)～(f) 的差異列 → V-R VERIFIED、Session VERIFIED",
          len(n) > 0 and vr["result"] == "VERIFIED" and (vr["detail"] or {}).get("x1_rows") == len(n)
          and v["result"] == "VERIFIED", f"n={len(n)} {vr} {v['result']}")
    x1b = make_session("CERT_X_1B", base + timedelta(days=20), pat, flag_delay=fd)
    n = like_r2b(os.path.join(x1b, "reports", "認證測試報告_CERT_X_1B_01.xlsx"),
                 extra=lambda rows, idx: rows[idx[0]][2].__setattr__("value", 12.34))     # 旗標延遲列再改一個無關數值（SOC）
    rc, last, _ = run_tool([x1b, v2], now=nxt_now())
    v, vr = vr_of(last["merge_dir"], "CERT_X_1B")
    fails = [f for r in (vr["detail"] or {}).get("rows", []) for f in r["failed"]]
    check("T23-2", "X1 列同時有無關數值差異（原始列 SOC 不等於前一列）→ FAILED（RAW_ROW_NOT_X1），不豁免",
          vr["result"] == "FAILED" and vr["reason_code"] == "RAW_ROW_NOT_X1" and any(f.startswith("(b)") for f in fails)
          and status_of(last["merge_dir"])["overall"] == "VERIFY_FAILED", str(fails)[:300])
    x1c = make_session("CERT_X_1C", base + timedelta(days=21), pat, flag_delay=fd)
    n = like_r2b(os.path.join(x1c, "reports", "認證測試報告_CERT_X_1C_01.xlsx"),
                 extra=lambda rows, idx: rows[idx[0]][12].__setattr__("value", "被改過的說明"))   # 沿用列的說明欄被改
    rc, last, _ = run_tool([x1c, v2], now=nxt_now())
    v, vr = vr_of(last["merge_dir"], "CERT_X_1C")
    fails = [f for r in (vr["detail"] or {}).get("rows", []) for f in r["failed"]]
    check("T23-2", "沿用列說明欄不等於前一列（條件 (b) 不成立）→ FAILED", vr["result"] == "FAILED"
          and any("說明欄" in f for f in fails), str(fails)[:300])
    v3 = make_session("CERT_X_3", base + timedelta(days=2), STD)
    rp = os.path.join(v3, "reports", "認證測試報告_CERT_X_3_01.xlsx")
    wb = load_workbook(rp)
    wb["Raw Data"]["C10"] = 1.234
    wb.save(rp)
    rc, last, _ = run_tool([v2, v3], now=nxt_now())
    v, vr = vr_of(last["merge_dir"], "CERT_X_3")
    check("T23-3", "一列非 X1 型差異（未知原因）→ V-R FAILED（RAW_ROW_NOT_X1）", vr["result"] == "FAILED"
          and vr["reason_code"] == "RAW_ROW_NOT_X1" and status_of(last["merge_dir"])["overall"] == "VERIFY_FAILED", str(vr)[:300])
    x3b = make_session("CERT_X_3B", base + timedelta(days=22), pat, flag_delay=fd)
    n = like_r2b(os.path.join(x3b, "reports", "認證測試報告_CERT_X_3B_01.xlsx"),
                 extra=lambda rows, idx: rows[5][2].__setattr__("value", 77.7))        # 另一個非旗標延遲列被改
    rc, last, _ = run_tool([x3b, v2], now=nxt_now())
    v, vr = vr_of(last["merge_dir"], "CERT_X_3B")
    det = vr["detail"] or {}
    check("T23-3", "同一 Session 同時有 X1 列與未知差異列 → 整體 FAILED（不因部分 X1 特徵放行）",
          vr["result"] == "FAILED" and det.get("x1_rows") == len(n) and det.get("n") == 1, str(det)[:300])
    v4 = make_session("CERT_X_4", base + timedelta(days=3), STD)
    rp = os.path.join(v4, "reports", "認證測試報告_CERT_X_4_01.xlsx")
    wb = load_workbook(rp)
    wb.worksheets[3]["B3"] = 99.9
    wb.save(rp)
    rc, last, _ = run_tool([v2, v4], now=nxt_now())
    codes = [s for s in status_of(last["merge_dir"])["sessions"] if s["cert_id"] == "CERT_X_4"][0]["reason_codes"]
    check("T23-4", "階段頁一格不同、無新決策 → V-S FAILED", "UNEXPLAINED_VALUE_DIFF" in (codes or []), str(codes))
    v5 = make_session("CERT_X_5", base + timedelta(days=4), STD)
    rp = os.path.join(v5, "reports", "認證測試報告_CERT_X_5_01.xlsx")
    wb = load_workbook(rp)
    wb.worksheets[3]["B3"] = 99.9
    wb.save(rp)
    st5 = json.load(open(os.path.join(v5, "cert_state.json"), encoding="utf-8"))
    _jl(os.path.join(v5, "decisions.jsonl"), [{"type": "stage_label", "stage_start": "2000-01-01T00:00:00", "label": "滿放",
                                                 "at": "2099-01-01 00:00:00"}])
    rc, last, _ = run_tool([v2, v5], now=nxt_now())
    s5 = [s for s in status_of(last["merge_dir"])["sessions"] if s["cert_id"] == "CERT_X_5"][0]
    check("T23-5", "階段頁不同、基準後有新 stage_label（新版 keywords 已確認基準）→ FAILED（r8 NK-05；不以 X7 放行）",
          s5["data_value_verification"] == "FAILED" and "UNEXPLAINED_VALUE_DIFF" in (s5["reason_codes"] or [])
          and s5.get("decision_snapshot") == "CONFIRMED", str(s5))
    v6 = make_session("CERT_X_6", base + timedelta(days=5), STD)
    rp = os.path.join(v6, "reports", "認證測試報告_CERT_X_6_01.xlsx")
    wb = load_workbook(rp)
    wb.properties.description = (wb.properties.description or "") + "\n未知的一行"
    wb.save(rp)
    rc, last, _ = run_tool([v2, v6], now=nxt_now())
    codes = [s for s in status_of(last["merge_dir"])["sessions"] if s["cert_id"] == "CERT_X_6"][0]["reason_codes"]
    check("T23-6", "描述多一行未知文字 → V-D FAILED", "DESC_UNKNOWN_LINE" in (codes or []), str(codes))
    v7 = make_session("CERT_X_7", base + timedelta(days=6), STD, report_suffix="_中途")
    rc, last, _ = run_tool([v2, v7], now=nxt_now())
    s7 = [s for s in status_of(last["merge_dir"])["sessions"] if s["cert_id"] == "CERT_X_7"][0]
    check("T23-7", "基準為 _中途 → UNVERIFIABLE", s7["data_value_verification"] == "UNVERIFIABLE" and "_中途" in s7["reason"])
    v8 = make_session("CERT_X_8", base + timedelta(days=7), STD, report=False)
    rc, last, _ = run_tool([v2, v8], now=nxt_now())
    s8 = [s for s in status_of(last["merge_dir"])["sessions"] if s["cert_id"] == "CERT_X_8"][0]
    check("T23-8", "沒有原始報告 → UNVERIFIABLE（無基準）", s8["data_value_verification"] == "UNVERIFIABLE" and "無比較基準" in s8["reason"])
    v9 = make_session("CERT_X_9", base + timedelta(days=8), STD, slots=False)
    rc, last, _ = run_tool([v2, v9], now=nxt_now())
    s9 = [s for s in status_of(last["merge_dir"])["sessions"] if s["cert_id"] == "CERT_X_9"][0]
    check("T23-9", "沒有 slots.jsonl（合成）→ UNVERIFIABLE（格式不支援）", s9["data_value_verification"] == "UNVERIFIABLE"
          and "格式不支援" in s9["reason"])
    v10 = make_session("CERT_X_10", base + timedelta(days=9), STD)
    rp = os.path.join(v10, "reports", "認證測試報告_CERT_X_10_01.xlsx")
    wb = load_workbook(rp)
    wb.remove(wb["整體測試"])
    wb.save(rp)
    rc, last, _ = run_tool([v2, v10], now=nxt_now())
    codes = [s for s in status_of(last["merge_dir"])["sessions"] if s["cert_id"] == "CERT_X_10"][0]["reason_codes"]
    check("T23-10", "工作表清單不同 → FAILED（STRUCTURE）", "STRUCTURE" in (codes or []), str(codes))
    v11 = make_session("CERT_X_11", base + timedelta(days=10), STD)
    make_original(v11, no=2)
    stp = os.path.join(v11, "cert_state.json")
    st11 = json.load(open(stp, encoding="utf-8"))
    st11["reports"].append({"file": "reports\\認證測試報告_CERT_X_11_03.xlsx", "at": "2099-01-01 00:00:00"})
    _w(stp, json.dumps(st11, ensure_ascii=False))
    rc, last, _ = run_tool([v2, v11], now=nxt_now())
    man = manifest_of(last["merge_dir"])
    reps = [s for s in man["sources"] if s["cert_id"] == "CERT_X_11"][0]["reports"]
    base_rep = [r for r in reps if r["is_baseline"]]
    check("T23-11", "多個原始報告＋一筆記錄無檔案 → 基準＝最大編號、清單全列並標示",
          len(reps) == 3 and base_rep and base_rep[0]["file"].endswith("_02.xlsx")
          and any(r["note"] == "有產報紀錄但檔案不存在" for r in reps), str(reps))


def t_c1(c1):
    print("== T-C1／T22-8／T23-1 C1 歷史 Session（唯讀快照） ==")
    if not c1:
        check("T-C1", "未提供 --c1（跳過不算通過）", False, "需要 C1 副本")
        return
    before = SRC.list_tree(c1)
    sa = AN.analyze_session(SRC.load_session(c1, os.path.dirname(c1)), 1)
    AN.pair_session(sa, "DC")
    st = sa["stages"]
    ok = (len(st) == 2 and st[0]["dir"] == "D" and st[1]["dir"] == "C"
          and st[0]["stage_start"] == "2026-11-12T11:19:20" and st[1]["stage_start"] == "2026-11-12T13:24:56"
          and len(sa["cycles"]) == 1 and sa["cycles"][0]["certainty"] == AN.CERTAIN
          and sa["cycles"][0]["status"] == AN.ST_PENDING and len(sa["uncertain_pauses"]) == 8
          and all(z["decision"] is None for z in sa["uncertain_pauses"]))
    check("T-C1", "C1：2 階段、1 循環、配對確定、待確認、8 個未決停頓", ok,
          str([(s["dir"], s["stage_start"]) for s in st]) + str(_cy(sa)))
    c = sa["cycles"][0]
    check("T22-8", "C1 時間量：2:01:04、1:59:56、休息 271 秒、觀測間隔 272 秒",
          st[0]["span_sec"] == 7264 and st[1]["span_sec"] == 7196 and c["rest_sec"] == 271 and c["observed_gap_sec"] == 272,
          f"{st[0]['span_sec']} {st[1]['span_sec']} {c['rest_sec']} {c['observed_gap_sec']}")
    other = make_session("CERT_C1_PARTNER", datetime(2026, 11, 20, 9, 0, 0), STD)
    rc, last, _ = run_tool([c1, other], versions=["CERT_20261112_111816=r2b"], now=nxt_now())
    if "merge_dir" not in last:
        check("T23-1", "C1 端到端執行（工具在前置檢查即拒絕，未產生 MERGE）", False, json.dumps(last, ensure_ascii=False))
        return
    md = last["merge_dir"]
    stj = status_of(md)
    s1 = [s for s in stj["sessions"] if s["cert_id"] == "CERT_20261112_111816"][0]
    man = manifest_of(md)
    v = [s for s in man["sources"] if s["cert_id"] == "CERT_20261112_111816"][0]["data_value_verification"]
    print(f"    （U-3）C1 資料值驗證＝{s1['data_value_verification']}；原因代碼 {s1['reason_codes']}；"
          + "；".join(f"{i['item']}={i['result']}" for i in v["items"]))
    check("T23-1", "C1 重產 vs 原始 _01：預期 VERIFIED（如有未解釋差異照實回報）", s1["data_value_verification"] == "VERIFIED",
          json.dumps([i for i in v["items"] if i["result"] != "VERIFIED" and i["result"] != "不適用"], ensure_ascii=False)[:600])
    # ---- Phase 3C 端到端細項 ----
    rep = {"merge_dir": md, "overall": stj.get("overall"), "publish_state": stj["publish"]["state"],
           "manifest_sha256": stj.get("manifest_sha256"), "items": v["items"], "status_sessions": stj["sessions"]}
    snap = stj.get("snapshot") or {}
    integ = stj.get("integrity") or {}
    check("3C-1", "C1 H1～H5：H2、H3、H5、H4 全部一致",
          snap.get("h2") == "一致" and snap.get("h3") == "一致" and integ.get("h5", {}).get("result") == "一致"
          and integ.get("h4", {}).get("result") == "一致", str((snap, {k: x.get("result") for k, x in integ.items()})))
    msrc = [s for s in man["sources"] if s["cert_id"] == "CERT_20261112_111816"][0]
    check("3C-1", "C1 每個來源檔 H1＝H2＝H3＝H4＝H5", all(len({f["h1"], f["h2"], f["h3"], f["h4"], f["h5"]}) == 1
                                                         for f in msrc["files"]), str(len(msrc["files"])))
    names = files_in(md, ".xlsx")
    rep["xlsx"] = names
    det = [n for n in names if "重產明細_S1_CERT_20261112_111816" in n]
    check("3C-2", "C1 重產明細 Excel 已產生", len(det) == 1, str(names))
    vr = [i for i in v["items"] if i["item"] == "V-R Raw Data"]
    vrd = (vr[0]["detail"] or {}) if vr else {}
    rep["vr"] = vr[0] if vr else None
    check("3C-4", "Raw Data 14,878 列 × 13 欄逐列比較完成", vr and vrd.get("rows") == 14878, str(vrd)[:300])
    check("3C-5", "已知 26 列 FAIL-2 差異依 REV-1 判為 X1（V-R VERIFIED、x1_rows＝26）",
          vr and vr[0]["result"] == "VERIFIED" and vrd.get("x1_rows") == 26, str(vr[0] if vr else None)[:600])
    others = [i for i in v["items"] if i["item"] != "V-R Raw Data" and i["result"] not in ("VERIFIED", "不適用")]
    check("3C-6", "其他資料值驗證項目沒有任何差異（全部 VERIFIED 或不適用）", not others,
          json.dumps(others, ensure_ascii=False, default=str)[:800])
    cyc = [c for c in man["cycles"] if c["id"].startswith("CERT_20261112_111816")]
    stg = [s for s in man["stages"] if s["id"].startswith("CERT_20261112_111816")]
    check("3C-7", "整合結果：C1 2 階段、1 個 DC 循環、配對確定",
          len(stg) == 2 and len(cyc) == 1 and cyc[0]["certainty"] == AN.CERTAIN
          and [s["dir"] for s in stg] == ["D", "C"], str((stg, cyc))[:400])
    check("3C-8", "完成狀態＝待確認（不依 SOC 推定）；未配對 0；8 個不確定停頓決策待確認",
          cyc and cyc[0]["status"] == AN.ST_PENDING and all(s["cycle_id"] for s in stg)
          and len([z for z in man["uncertain_pauses"] if z["cert_id"] == "CERT_20261112_111816" and z["pending"]]) == 8,
          str(cyc)[:300])
    summ = [n for n in names if os.path.basename(n).startswith("整合")]
    if summ:
        from openpyxl import load_workbook
        wbs = load_workbook(os.path.join(md, summ[0]), read_only=True)
        sheets = wbs.sheetnames
        wbs.close()
        first = summary_first_screen(os.path.join(md, summ[0]))
        check("3C-9", "整合 Excel 9 頁；第一屏為產生本檔時狀態、含原始參數 UNVERIFIABLE、無禁用措辭",
              sheets == XL.SHEETS and first[0].startswith("產生本檔時的狀態")
              and any("原始參數：UNVERIFIABLE（原始報告未記錄）" in x for x in first)
              and not any(w in "\n".join(first) for w in BANNED + ("已發布", "發布成功")), str(sheets))
    else:
        check("3C-9", "整合 Excel 已產生", False, str(names))
    if stj.get("overall") in ("COMPLETED", "COMPLETED_WITH_UNVERIFIED"):
        check("3C-10", "STATUS、manifest、正式報告 SHA-256 一致（判定規則三項成立）", is_published(md))
    else:
        partial_ok = all(n.endswith(".partial.xlsx") for n in names) and not is_published(md)
        check("3C-10", f"未發布（{stj.get('overall')}）→ 全部 .partial、判定無效", partial_ok, str(names))
    check("3C-11", "原始參數可追溯性獨立標示為 UNVERIFIABLE（STATUS 每個 Session）",
          all(s["param_traceability"] == "UNVERIFIABLE" for s in stj["sessions"]))
    results = {s["cert_id"]: s["data_value_verification"] for s in stj["sessions"]}
    expect = ("VERIFY_FAILED" if "FAILED" in results.values() else
              "COMPLETED_WITH_UNVERIFIED" if "UNVERIFIABLE" in results.values() else "COMPLETED")
    suffix_ok = (any("_含未驗證明細" in n for n in names) == (expect == "COMPLETED_WITH_UNVERIFIED"))
    check("3C-12", f"發布規則：資料值結果 {results} → 整體 {expect}（實際 {stj.get('overall')}）、檔名後綴相符",
          stj.get("overall") == expect and (expect == "VERIFY_FAILED" or suffix_ok))
    rep["output_sha256"] = {n: SRC.sha256_file(os.path.join(md, n)) for n in names}
    mp = os.path.join(md, "merge_manifest.json")
    rep["manifest_file_sha256"] = SRC.sha256_file(mp) if os.path.exists(mp) else None
    _w(os.path.join(WORK, "phase3C_report.json"), json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    after = SRC.list_tree(c1)
    check("T23-12", "C1 來源雜湊不變（驗證全程未寫入原始報告與來源）", SRC.compare_trees(before, after) == [])
    if SRC.compare_trees(before, after):
        raise Abort("C1 來源副本雜湊改變")


def t_tables_unit():
    print("== T-TAB 表格名稱與範圍比較（_tables 修正） ==")
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table

    def ws_with(name, ref):
        wb = Workbook()
        ws = wb.active
        for r in range(1, 5):
            ws.append([f"h{r}a", f"h{r}b"] if r == 1 else [r, r * 2])
        ws.add_table(Table(displayName=name, ref=ref))
        return ws
    base = ws_with("T_stage1", "A1:B4")
    r1 = VF._cmp_sheet("V-S", "x", base, ws_with("T_stage1", "A1:B4"), False)
    check("T-TAB-1", "表格名稱、範圍相同 → VERIFIED", r1["result"] == "VERIFIED", str(r1))
    r2 = VF._cmp_sheet("V-S", "x", base, ws_with("T_stage9", "A1:B4"), False)
    check("T-TAB-2", "表格名稱不同 → FAILED（TABLE_MISMATCH）", r2["result"] == "FAILED" and r2["reason_code"] == "TABLE_MISMATCH",
          str(r2))
    r3 = VF._cmp_sheet("V-S", "x", base, ws_with("T_stage1", "A1:B3"), False)
    check("T-TAB-3", "表格範圍不同 → FAILED（TABLE_MISMATCH）", r3["result"] == "FAILED" and r3["reason_code"] == "TABLE_MISMATCH",
          str(r3))
    fake = type("W", (), {"tables": {"T_stage1": Table(displayName="T_stage1", ref="A1:B4")}})()
    check("T-TAB-4", "表格物件形式也能取得範圍（.ref）", VF._tables(fake) == {"T_stage1": "A1:B4"})
    check("T-TAB-5", "openpyxl 實際回傳（名稱, 範圍字串）時正確取值", VF._tables(base) == {"T_stage1": "A1:B4"})


def t_vr_unit():
    print("== T-VR V-R 條件 (a)～(f) 單元測試（REV-1） ==")
    from openpyxl import Workbook
    from cert_report_xlsx import RAW1_HEADERS, _raw1_note, _v, field_value, pcs_display, status_text
    from cert_stage_engine import Obs
    p = CFG.params()
    nf = {1: "第1次放電"}
    t0 = datetime(2026, 12, 2, 13, 20, 23)
    base_vals = {"soc": 0.0, "current_a": -140.1, "power_kw": -113.7, "rack_max_c": 30.0, "rack_min_c": 25.0,
                 "cell_vmax_v": 3.033, "cell_vmin_v": 2.792, "voltage_v": 829.6, "sensor_c": "-", "pcs_module_c": "32"}

    def obs(t, vals, flags, direction, state, stage_no):
        o = Obs(t=t, ok=True, values=dict(vals), flags=dict(flags))
        o.direction, o.state, o.stage_no = direction, state, stage_no
        return o
    fd_flags = {"pcs_charging_flag": False, "pcs_discharging_flag": True, "pcs_fault_flag": False,
                "battery_power_status": "已上電", "pcs_status": "執行 / 併網 / 放電"}

    def build(*, fd_vals=None, fd_flag=None, fd_dir="I", mut_orig=None, mut_regen=None, regen_time_shift=0):
        o0 = obs(t0, base_vals, dict(fd_flags), "D", "放電", 1)
        o1 = obs(t0 + timedelta(seconds=1), fd_vals or dict(base_vals, current_a=0.0, voltage_v=833.2, cell_vmax_v=3.066,
                                                             cell_vmin_v=2.842),
                 fd_flags if fd_flag is None else fd_flag, fd_dir, "休息", None)
        rows = [{"obs": o0, "time": t0}, {"obs": o1, "time": t0 + timedelta(seconds=1)}]

        def cells(r):
            o = r["obs"]
            pcs, _ = pcs_display(o, p)
            return [r["time"], status_text(o, nf)] + [pcs if k == "pcs_module_c" else _v(field_value(o, k, p))
                                                      for k in VF.RAW_VALUE_KEYS] + [_raw1_note(r, p) or None]
        regen = [cells(r) for r in rows]
        orig = [list(regen[0]), list(regen[0])]
        orig[1][0] = regen[1][0]
        if regen_time_shift:
            regen[1][0] = regen[1][0] + timedelta(seconds=regen_time_shift)
        if mut_orig:
            mut_orig(orig)
        if mut_regen:
            mut_regen(regen)

        def wb_of(rs_):
            wb = Workbook()
            ws = wb.active
            ws.title = "Raw Data"
            ws.append(RAW1_HEADERS)
            for r in rs_:
                ws.append(r)
            return wb
        return VF._cmp_raw(wb_of(orig), wb_of(regen), {"rows": rows}, {"params": p, "names_full": nf})

    def failed(res):
        return [f for r in (res.get("detail") or {}).get("rows", []) for f in r["failed"]]
    r = build()
    check("T-VR-0", "全部條件成立 → VERIFIED（X1 列 1）", r["result"] == "VERIFIED" and r["detail"]["x1_rows"] == 1, str(r))
    cases = [
        ("T-VR-a", "(a) 時間欄不同", dict(regen_time_shift=5), "(a)"),
        ("T-VR-b1", "(b) 原始列數值欄不等於前一列", dict(mut_orig=lambda o: o[1].__setitem__(2, 0.5)), "(b) 原始列數值欄"),
        ("T-VR-b2", "(b) 原始列說明欄不等於前一列", dict(mut_orig=lambda o: o[1].__setitem__(12, "其他說明")), "(b) 原始列說明欄"),
        ("T-VR-c1", "(c) 旗標皆非 True（N/A 由其他原因造成）",
         dict(fd_flag={"pcs_charging_flag": False, "pcs_discharging_flag": False, "battery_power_status": None}), "(c)"),
        ("T-VR-c2", "(c) 缺少旗標欄位（無法證明）", dict(fd_flag={"battery_power_status": None}), "(c) 來源觀測缺少"),
        ("T-VR-c3", "(c) 方向不是待機", dict(fd_dir="D"), "(c) 來源觀測方向"),
        ("T-VR-d", "(d) 重產狀態不是 N/A", dict(mut_regen=lambda g: g[1].__setitem__(1, "待機")), "(d)"),
        ("T-VR-e", "(e) 重產數值欄與來源讀值不同（無關欄位）", dict(mut_regen=lambda g: g[1].__setitem__(8, 999.9)), "(e)"),
        ("T-VR-f", "(f) 重產說明欄與重新計算不同", dict(mut_regen=lambda g: g[1].__setitem__(12, "被改過")), "(f)")]
    for tid, name, kw, key in cases:
        r = build(**kw)
        check(tid, f"{name} → FAILED（RAW_ROW_NOT_X1）",
              r["result"] == "FAILED" and r["reason_code"] == "RAW_ROW_NOT_X1" and any(f.startswith(key) for f in failed(r)),
              str(failed(r))[:300])
    r = build(mut_orig=lambda o: o[1].__setitem__(2, 0.5), mut_regen=lambda g: g[1].__setitem__(12, "被改過"))
    check("T-VR-g", "多個條件同時不成立 → 全部列出", len({f[:3] for f in failed(r)}) >= 2, str(failed(r)))


def t_space_unit():
    print("== T-SP check_space（輸出根目錄上層不存在、查詢失敗） ==")
    s = make_session("CERT_SP_1", T0 + timedelta(days=600), STD, report=False)
    infos = [{"path": s}]
    r = SRC.check_space(WORK, infos)
    check("T-SP-1", "正常路徑（已存在）→ 以該路徑查詢、空間足夠", SRC.norm(r["probe"]) == SRC.norm(WORK) and r["free_bytes"] > 0)
    deep = os.path.join(WORK, "sp_missing_1", "sp_missing_2", "out")
    r = SRC.check_space(deep, infos)
    check("T-SP-2", "上層不存在 → 以最近一層已存在的上層查詢；不建立任何資料夾",
          SRC.norm(r["probe"]) == SRC.norm(WORK) and not os.path.exists(os.path.join(WORK, "sp_missing_1")), str(r))

    def boom(p):
        raise OSError(5, "（測試注入）磁碟資訊查詢失敗")
    try:
        SRC.check_space(deep, infos, disk_free=boom)
        ok, msg = False, "未拒絕"
    except SRC.Rejected as e:
        ok, msg = e.code == "SPACE" and "無法取得磁碟空間資訊" in e.msg, e.msg
    check("T-SP-3", "磁碟資訊查詢失敗 → Rejected（SPACE，明確原因），不崩潰", ok, msg)
    try:
        SRC.check_space(WORK, infos, disk_free=lambda p: None)
        ok, msg = False, "未拒絕"
    except SRC.Rejected as e:
        ok, msg = e.code == "SPACE" and "無效" in e.msg, e.msg
    check("T-SP-4", "查詢回傳無效值 → Rejected（SPACE）", ok, msg)
    s2 = make_session("CERT_SP_2", T0 + timedelta(days=601), STD, report=False)
    out = os.path.join(WORK, "sp_missing_3", "out")
    rc, last, _ = run_tool([s, s2], out=out, now=nxt_now(), disk_free=boom)
    check("T-SP-5", "完整流程：查詢失敗 → rc=2（SPACE）、不建立任何輸出資料夾",
          rc == 2 and last.get("code") == "SPACE" and not os.path.exists(os.path.join(WORK, "sp_missing_3")), str(last))


def _dirs_under(root):
    out = []
    for d, ds, _fs in os.walk(root):
        out += [os.path.join(d, x) for x in ds]
    return set(out)


def t_dirs_unit():
    print("== T-DIR 輸出根目錄建立限制（create_merge_dir） ==")
    import subprocess
    a = make_session("CERT_DIR_1", T0 + timedelta(days=700), STD)
    b = make_session("CERT_DIR_2", T0 + timedelta(days=701), STD)
    base = os.path.join(WORK, "dirs")
    os.makedirs(base)
    existing = os.path.join(base, "exists")
    os.makedirs(existing)
    rc, last, _ = run_tool([a, b], out=existing, now=nxt_now())
    check("T-DIR-1", "輸出根目錄已存在 → 沿用、正常產報", rc == 0 and SRC.is_inside(last.get("merge_dir", ""), existing), str(last))
    new_root = os.path.join(base, "new_root")
    rc, last, _ = run_tool([a, b], out=new_root, now=nxt_now())
    check("T-DIR-2", "不存在但直接父目錄存在 → 只建立輸出根目錄本身並正常產報",
          rc == 0 and os.path.isdir(new_root) and SRC.is_inside(last.get("merge_dir", ""), new_root), str(last))
    before = _dirs_under(base)
    deep = os.path.join(base, "missing_parent", "out_root")
    rc, last, _ = run_tool([a, b], out=deep, now=nxt_now())
    check("T-DIR-3", "父目錄不存在 → rc=2（F2）、不遞迴建立任何上層資料夾",
          rc == 2 and last.get("code") == "F2" and _dirs_under(base) == before
          and not os.path.exists(os.path.join(base, "missing_parent")), str(last))
    afile = os.path.join(base, "a_file")
    _w(afile, "x")
    rc, last, _ = run_tool([a, b], out=afile, now=nxt_now())
    check("T-DIR-4", "輸出根目錄是檔案 → rc=2、檔案不變",
          rc == 2 and last.get("code") in ("F2", "V6") and _real_open(afile).read() == "x", str(last))
    target = os.path.join(base, "junction_target")
    os.makedirs(target)
    link = os.path.join(base, "junction_link")
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    if r.returncode != 0 or not os.path.isdir(link):
        skip("T-DIR-5", "輸出根目錄是 junction → 拒絕", f"無法建立測試用 junction（rc={r.returncode}）")
        skip("T-DIR-6", "父目錄是 junction → 拒絕且不在目標內建立", "同上")
    else:
        rc, last, _ = run_tool([a, b], out=link, now=nxt_now())
        check("T-DIR-5", "輸出根目錄是 junction（重新導向）→ rc=2、目標資料夾內沒有建立任何東西",
              rc == 2 and last.get("code") == "F2" and os.listdir(target) == [], str(last))
        rc, last, _ = run_tool([a, b], out=os.path.join(link, "sub_out"), now=nxt_now())
        check("T-DIR-6", "父目錄是 junction → rc=2、不在 junction 目標內建立輸出根目錄",
              rc == 2 and last.get("code") == "F2" and os.listdir(target) == [], str(last))
    now = nxt_now()
    md = os.path.join(existing, f"MERGE_{now:%Y%m%d_%H%M%S}")
    os.makedirs(md)
    _w(os.path.join(md, "keep.txt"), "keep")
    rc, last, _ = run_tool([a, b], out=existing, now=now)
    check("T-DIR-7", "MERGE_<ts> 已存在 → rc=2、不覆蓋、不清理",
          rc == 2 and os.listdir(md) == ["keep.txt"] and _real_open(os.path.join(md, "keep.txt")).read() == "keep")


def t_manifest_single():
    print("== T-MAN manifest 單次讀取與雜湊 ==")
    import hashlib
    a = make_session("CERT_MAN_1", T0 + timedelta(days=800), STD)
    b = make_session("CERT_MAN_2", T0 + timedelta(days=801), STD)
    cap = {}

    def grab(raw, msha):
        cap["raw"], cap["msha"] = raw, msha
    rc, last, _ = run_tool([a, b], now=nxt_now(), hooks={"after_manifest_hash": grab})
    md = last.get("merge_dir")
    final = os.path.join(md, "merge_manifest.json") if md else ""
    fb = _real_open(final, "rb").read() if os.path.exists(final) else b""
    st = status_of(md) if md else {}
    check("T-MAN-1", "合法 manifest 可以發布；STATUS 雜湊＝讀取的 bytes 雜湊＝正式 manifest 雜湊",
          rc == 0 and is_published(md) and cap.get("msha") == hashlib.sha256(cap.get("raw", b"")).hexdigest()
          == st.get("manifest_sha256") == hashlib.sha256(fb).hexdigest() and fb == cap.get("raw"), str(last))
    check("T-MAN-2", "manifest 欄位被竄改會被拒絕（見 TP-6）", any(r[0] == "TP-6" and r[2] == "PASS" for r in RESULTS))
    check("T-MAN-3", "發布後位元組被修改會被 R2 偵測（見 TP-6b）", any(r[0] == "TP-6b" and r[2] == "PASS" for r in RESULTS))
    # T-MAN-4：讀取失敗
    a2 = make_session("CERT_MAN_3", T0 + timedelta(days=802), STD)
    b2 = make_session("CERT_MAN_4", T0 + timedelta(days=803), STD)
    prev = builtins.open

    def fail_read(file, mode="r", *x, **k):
        if isinstance(file, str) and file.endswith("merge_manifest.json.partial") and "r" in mode and "b" in mode:
            raise OSError("（測試注入）manifest 讀取失敗")
        return prev(file, mode, *x, **k)
    builtins.open = fail_read
    try:
        rc, last, _ = run_tool([a2, b2], now=nxt_now())
    finally:
        builtins.open = prev
    md = last.get("merge_dir")
    st = status_of(md)
    check("T-MAN-4", "讀取失敗 → FAILED（manifest_write_failed）、沒有有效正式報告、全部 .partial",
          st["overall"] == "FAILED" and st["reason"] == "manifest_write_failed" and not is_published(md)
          and all(n.endswith(".partial.xlsx") for n in files_in(md, ".xlsx")), f"{st['overall']} {st['reason']}")
    # T-MAN-5：讀取完成後檔案立刻被改 → 解析與雜湊仍來自同一份已讀 bytes；之後 R2 偵測到磁碟內容不同
    a3 = make_session("CERT_MAN_5", T0 + timedelta(days=804), STD)
    b3 = make_session("CERT_MAN_6", T0 + timedelta(days=805), STD)
    seen = {}

    class AfterRead:
        def __init__(self, f, path):
            self.f, self.path = f, path

        def __enter__(self):
            return self

        def read(self, *x):
            data = self.f.read(*x)
            seen["read_bytes"] = data
            return data

        def __exit__(self, *exc):
            self.f.close()
            with prev(self.path, "ab") as fh:              # 讀取結束後才改動磁碟上的檔案
                fh.write(b" ")
            seen["disk_after"] = hashlib.sha256(prev(self.path, "rb").read()).hexdigest()
            return False

    def spy(file, mode="r", *x, **k):
        if isinstance(file, str) and file.endswith("merge_manifest.json.partial") and "r" in mode and "b" in mode:
            seen["opens"] = seen.get("opens", 0) + 1
            return AfterRead(prev(file, mode, *x, **k), file)
        return prev(file, mode, *x, **k)
    builtins.open = spy
    try:
        rc, last, _ = run_tool([a3, b3], now=nxt_now(), hooks={"after_manifest_hash": grab})
    finally:
        builtins.open = prev
    md = last.get("merge_dir")
    st = status_of(md)
    rb = seen.get("read_bytes", b"")
    check("T-MAN-5", "只讀取一次；解析與 SHA-256 來自同一份 bytes；讀後的磁碟改動由 R2 偵測 → PUBLISH_FAILED",
          seen.get("opens") == 1 and cap.get("raw") == rb and cap.get("msha") == hashlib.sha256(rb).hexdigest()
          and st.get("manifest_sha256") == cap.get("msha") and seen.get("disk_after") != cap.get("msha")
          and st["overall"] == "PUBLISH_FAILED" and st["reason"] == "post_rename_hash_mismatch" and not is_published(md),
          f"opens={seen.get('opens')} {st.get('overall')} {st.get('reason')}")


# =============================================================================
# 3R：Runner 分類（SKIP-CLI）與 test_auto_schedule 輸出隔離（PHASE33_E2E_ROOT）
# =============================================================================
def _sub_env(extra=None):
    tmp = os.path.join(WORK, "_tmp_sub")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TEMP=tmp, TMP=tmp, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.update(extra or {})
    return env


def t_runner_class():
    print("== T-RUN Runner CLI 測試工具分類（SKIP-CLI） ==")
    import importlib.util
    import subprocess
    spec = importlib.util.spec_from_file_location("_runner_probe", os.path.join(HERE, "run_offline_regression.py"))
    rr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rr)                       # 只載入定義，不呼叫 main()
    check("T-RUN-1", "test_cert_merge_report 列在 CLI_TOOL_SUITES，且不在 LIVE_SUITES／KNOWN_FAILURES",
          "test_cert_merge_report" in rr.CLI_TOOL_SUITES and "test_cert_merge_report" not in rr.LIVE_SUITES
          and "test_cert_merge_report" not in rr.KNOWN_FAILURES)
    runner = os.path.join(HERE, "run_offline_regression.py")
    r = subprocess.run([sys.executable, "-B", "-I", runner, "--list"], cwd=HERE, env=_sub_env(), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    out = r.stdout
    offline = out.split("== 需要實體 HMI")[0]
    cli_sec = out.split("== 需專用 CLI 參數的測試工具")[1].split("== 實機 scraper")[0] if "== 需專用 CLI 參數的測試工具" in out else ""
    hmi_sec = out.split("== 需要實體 HMI")[1].split("== 需專用 CLI")[0] if "== 需要實體 HMI" in out else ""
    check("T-RUN-2", "--list 顯示 SKIP-CLI 分類並列出 test_cert_merge_report",
          r.returncode == 0 and "test_cert_merge_report" in cli_sec, out[-400:])
    check("T-RUN-3", "--list 中 test_cert_merge_report 不在純離線清單、不在需實體 HMI 分類",
          "test_cert_merge_report" not in offline and "test_cert_merge_report" not in hmi_sec)
    js = os.path.join(WORK, "runner_cli_probe.json")
    r = subprocess.run([sys.executable, "-B", "-I", runner, "-k", "test_cert_merge_report", "--json", js], cwd=HERE,
                       env=_sub_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    out = r.stdout
    j = json.load(_real_open(js, encoding="utf-8")) if os.path.exists(js) else {}
    rows = j.get("rows", [])
    check("T-RUN-4", "Runner 不執行 CLI 工具：rc＝SKIP-CLI、PASS 0／FAIL 0、執行 0 套件",
          len(rows) == 1 and rows[0]["rc"] == "SKIP-CLI" and rows[0]["pass"] == 0 and rows[0]["fail"] == 0
          and j.get("pass") == 0 and j.get("fail") == 0 and "執行 0 套件" in out, out[-500:])
    check("T-RUN-5", "SKIP-CLI 不計入 PASS、不算需處理的失敗；摘要另列 SKIP-CLI 與說明",
          "SKIP-CLI" in out and "需處理的失敗" not in out and "略過 1（SKIP-CLI" in out
          and "需實體 HMI（需要 --phase" not in out, out[-500:])
    check("T-RUN-6", "沒有實際執行該測試（無 argparse 錯誤輸出）", "the following arguments are required" not in out
          and "usage:" not in out)


def _p33_func(name="_phase33_e2e_root"):
    """自 test_auto_schedule.py 取出全部 _phase33_e2e_* 函式（彼此互相呼叫）後回傳指定函式。"""
    tree = ast.parse(open(os.path.join(HERE, "test_auto_schedule.py"), encoding="utf-8").read())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("_phase33_e2e_")]
    ns = {"os": os}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "_phase33_e2e", "exec"), ns)
    return ns[name]


def t_phase33_root():
    print("== T-P33 test_auto_schedule 輸出隔離（PHASE33_E2E_ROOT） ==")
    import subprocess
    fn = _p33_func()
    proj = os.path.dirname(HERE)
    base = os.path.join(WORK, "p33")
    os.makedirs(base)
    os.makedirs(os.path.join(base, "cert_reports"))
    os.makedirs(os.path.join(base, "phase33_e2e_proj", "inner"))
    _w(os.path.join(base, "phase33_e2e_file"), "x")
    jt = os.path.join(base, "jt")
    os.makedirs(jt)
    jl = os.path.join(base, "jlink")
    subprocess.run(["cmd", "/c", "mklink", "/J", jl, jt], capture_output=True)
    jl2 = os.path.join(base, "phase33_e2e_j")
    subprocess.run(["cmd", "/c", "mklink", "/J", jl2, jt], capture_output=True)
    check("T-P33-1", "未設定 → 原預設路徑（語意不變）",
          fn(None, proj) == os.path.join(proj, "output", "test_output", "phase33_e2e"))
    bad = [("空字串", ""), ("空白", "   "), ("相對路徑", "phase33_e2e_x"), ("磁碟根目錄", "C:\\"),
           ("名稱非 phase33_e2e 開頭", os.path.join(base, "other")),
           ("專案根目錄的上層", os.path.join(base, "phase33_e2e_proj")),
           ("專案目錄內（含既有 phase33_e2e）", os.path.join(proj, "output", "test_output", "phase33_e2e")),
           ("正式版目錄", r"D:\Crawler Sample\phase33_e2e"),
           ("cert_reports 目錄", os.path.join(base, "cert_reports", "phase33_e2e")),
           ("父目錄不存在", os.path.join(base, "missing", "phase33_e2e")),
           ("已存在但是檔案", os.path.join(base, "phase33_e2e_file"))]
    if os.path.isdir(jl):
        bad.append(("父目錄是 junction", os.path.join(jl, "phase33_e2e")))
    if os.path.isdir(jl2):
        bad.append(("目標本身是 junction", jl2))
    for name, v in bad:
        proj_arg = os.path.join(base, "phase33_e2e_proj", "inner") if name == "專案根目錄的上層" else proj
        try:
            fn(v, proj_arg)
            ok = False
        except ValueError:
            ok = True
        check("T-P33-2", f"拒絕：{name}", ok, v)
    if not os.path.isdir(jl) or not os.path.isdir(jl2):
        skip("T-P33-2", "junction 情境", "無法建立測試用 junction")
    good = os.path.join(base, "phase33_e2e_ok")
    check("T-P33-3", "合法的隔離路徑 → 採用", SRC.norm(fn(good, proj)) == SRC.norm(good))
    check("T-P33-4", "檢查函式本身不建立任何資料夾", not os.path.exists(good))
    # 實際執行 test_auto_schedule（網路阻擋、暫存導向）：合法隔離路徑 → 開發 clone 既有 phase33_e2e 不變
    default = os.path.join(proj, "output", "test_output", "phase33_e2e")
    before = SRC.list_tree(default) if os.path.isdir(default) else {}
    guard = {"PYTHONPATH": os.path.join(HERE, "_offline_guard"), "OFFLINE_GUARD_LOG": os.path.join(WORK, "_tmp_sub", "netlog.tmp")}
    r = subprocess.run([sys.executable, os.path.join(HERE, "test_auto_schedule.py")], cwd=HERE,
                       env=_sub_env(dict(guard, PHASE33_E2E_ROOT=good)), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = r.stdout + r.stderr
    subs = [n for n in os.listdir(good) if os.path.isdir(os.path.join(good, n))] if os.path.isdir(good) else []
    check("T-P33-5", "設定合法隔離路徑執行 test_auto_schedule → rc=0、E2E 輸出落在隔離目錄且只有 1 個子資料夾",
          r.returncode == 0 and len(subs) == 1 and "[FAIL]" not in out, out[-600:])
    check("T-P33-6", "開發 clone 既有 phase33_e2e 逐檔不變", SRC.list_tree(default) == before if before else not os.path.exists(default))
    r2 = subprocess.run([sys.executable, os.path.join(HERE, "test_auto_schedule.py")], cwd=HERE,
                        env=_sub_env(dict(guard, PHASE33_E2E_ROOT=default)), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=900)
    out2 = r2.stdout + r2.stderr
    check("T-P33-7", "設定為專案內既有路徑 → E2E 判 FAIL（例外）、不清除既有輸出",
          r2.returncode != 0 and "E2E 正式流程發生例外" in out2 and SRC.list_tree(default) == before, out2[-400:])
    check("T-P33-8", "子程序網路連線嘗試＝0", not os.path.exists(guard["OFFLINE_GUARD_LOG"]))
    # ---------------- L1：核准根目錄／所有權標記／reparse／清理前重新驗證（T-P33-9～16） ----------------
    # 全部只在本次 WORK 內建立合成目錄、junction 與 sentinel；真實受保護路徑只做 stat／lstat／realpath。
    safe = _p33_func("_phase33_e2e_safe_path")
    chk = _p33_func("_phase33_e2e_check")
    clean = _p33_func("_phase33_e2e_clean")
    create = _p33_func("_phase33_e2e_create")
    marker_ok = _p33_func("_phase33_e2e_marker_ok")

    def raises(f, *a, **k):
        try:
            f(*a, **k)
            return False
        except ValueError:
            return True

    def junction(link, target):
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        return os.path.isjunction(link)

    def run_case(f, *a):
        try:
            return bool(f(*a)), ""
        except Exception as e:                            # noqa: BLE001
            return False, repr(e)

    def pf(x):
        return "PASS" if x else "FAIL"

    L = os.path.join(base, "l1")
    ab = os.path.join(L, "phase33_e2e_approved")
    os.makedirs(ab)
    outside = os.path.join(L, "outside")
    _w(os.path.join(outside, "sentinel.txt"), "sentinel-L1")
    for t in ("env", "def"):
        _w(os.path.join(outside, "jt14_" + t, "keep.txt"), "keep-" + t)
    moved = os.path.join(L, "moved")
    os.makedirs(moved)
    out_before = SRC.list_tree(outside)

    def proj_of(name):
        p_ = os.path.join(L, "proj_" + name)
        os.makedirs(os.path.join(p_, "output", "test_output"))
        return p_

    def droot(p_):
        return os.path.join(p_, "output", "test_output", "phase33_e2e")

    def eroot(name):
        return os.path.join(ab, "phase33_e2e_" + name)

    # T-P33-9 核准範圍外拒絕；核准根目錄本身不得為清理目標
    r9 = [raises(fn, os.path.join(outside, "phase33_e2e_x"), proj, approved_base=ab),
          raises(fn, ab, proj, approved_base=ab),
          raises(clean, ab, proj, True, ab),
          raises(fn, r"D:\Crawler WT\phase33_e2e_x", proj),
          os.path.isdir(ab)]
    check("T-P33-9", f"核准範圍外路徑拒絕、核准根目錄本身不得為清理目標（{r9}）", all(r9), str(r9))

    # T-P33-10 核准根目錄本身是 junction → 拒絕
    abj, abt = os.path.join(L, "abj"), os.path.join(L, "abt")
    os.makedirs(abt)
    r10 = [junction(abj, abt),
           raises(fn, os.path.join(abt, "phase33_e2e_x"), proj, approved_base=abj),
           raises(fn, os.path.join(abj, "phase33_e2e_x"), proj, approved_base=abj)]
    check("T-P33-10", f"核准根目錄為 junction → 拒絕（{r10}）", all(r10), str(r10))

    # T-P33-11 真實受保護路徑（唯讀 stat／realpath，不建立任何東西）
    real_paths = [r"D:\Crawler Sample\phase33_e2e", r"D:\Crawler_1hz_r2c\phase33_e2e",
                  r"D:\Crawler WT\histregen_20261123\phase33_e2e", r"D:\Crawler WT\histregen_20261124_083020\phase33_e2e",
                  r"D:\Crawler WT\g3g4_evidence_20261124\phase33_e2e", r"D:\Crawler WT\g5g6_evidence_20261124\phase33_e2e",
                  r"D:\Crawler WT\b0_baseline_20261124\phase33_e2e", r"D:\Crawler WT\g0_archive_20261124\phase33_e2e",
                  r"D:\Crawler WT\batch4_evidence_20261125\phase33_e2e",
                  os.path.join(proj, "output", "cert_reports", "phase33_e2e")]
    ex0 = [os.path.lexists(v) for v in real_paths]
    r11 = [raises(fn, v, proj) and raises(safe, v) for v in real_paths]
    r11d = [raises(chk, droot(p_), p_, False) for p_ in (r"D:\Crawler Sample", r"D:\Crawler_1hz_r2c",
                                                         r"D:\Crawler WT\histregen_20261123")]
    ex1 = [os.path.lexists(v) for v in real_paths]
    check("T-P33-11", f"真實受保護路徑一律拒絕（env {sum(r11)}/{len(r11)}、預設模式 {sum(r11d)}/{len(r11d)}；未建立任何東西）",
          all(r11) and all(r11d) and ex0 == ex1, f"{r11} {r11d} {ex0} {ex1}")

    # T-P33-12 非空且無有效所有權標記 → 拒絕清理（無標記、及綁定其他路徑的標記），內容不變
    def nomark_case(root, args):
        _w(os.path.join(root, "x.txt"), "unknown-source")
        t0 = SRC.list_tree(root)
        a = raises(clean, root, *args)
        with open(os.path.join(root, ".phase33_e2e_owned"), "w", encoding="utf-8", newline="\n") as f:
            f.write("ESS phase33_e2e test-owned v1\nroot=%s\n" % os.path.normcase(os.path.join(L, "other")))
        t1 = SRC.list_tree(root)
        b = raises(clean, root, *args)
        return a and b and SRC.list_tree(root) == t1 and set(t1) - set(t0) == {".phase33_e2e_owned"} and not marker_ok(root)
    p12 = proj_of("12")
    a12, ea12 = run_case(nomark_case, eroot("e12"), (proj, True, ab))
    b12, eb12 = run_case(nomark_case, droot(p12), (p12, False))
    check("T-P33-12", f"非空且無有效所有權標記 → 拒絕清理、內容不變（env={pf(a12)}／預設={pf(b12)}）", a12 and b12, ea12 + eb12)

    # T-P33-13 首次合法建立即寫入標記；有效標記目錄可清理並重建
    def legit_case(root, args):
        s0 = clean(root, *args) == "absent"
        create(root)
        m1 = marker_ok(root) and os.listdir(root) == [".phase33_e2e_owned"]
        _w(os.path.join(root, "20261201_000000_auto", "session_state.json"), "{}")
        s1 = clean(root, *args) == "removed" and not os.path.lexists(root)
        create(root)
        m2 = marker_ok(root) and os.listdir(root) == [".phase33_e2e_owned"]
        return s0 and m1 and s1 and m2
    p13 = proj_of("13")
    a13, ea13 = run_case(legit_case, eroot("e13"), (proj, True, ab))
    b13, eb13 = run_case(legit_case, droot(p13), (p13, False))
    check("T-P33-13", f"首次合法建立即寫入所有權標記；有效標記目錄可清理並重建（env={pf(a13)}／預設={pf(b13)}）",
          a13 and b13, ea13 + eb13)

    # T-P33-14 目錄樹內含 junction（即使標記有效）→ 拒絕清理，junction 目標內容不變
    def junction_case(root, args, tag):
        create(root)
        tgt = os.path.join(outside, "jt14_" + tag)
        os.makedirs(os.path.join(root, "sub"))
        jok = junction(os.path.join(root, "sub", "j"), tgt)
        t0 = SRC.list_tree(tgt)
        a = raises(clean, root, *args)
        return jok and marker_ok(root) and a and os.path.isdir(root) and SRC.list_tree(tgt) == t0
    p14 = proj_of("14")
    a14, ea14 = run_case(junction_case, eroot("e14"), (proj, True, ab), "env")
    b14, eb14 = run_case(junction_case, droot(p14), (p14, False), "def")
    check("T-P33-14", f"目錄樹內含 junction → 拒絕清理、目標內容不變（env={pf(a14)}／預設={pf(b14)}）", a14 and b14, ea14 + eb14)

    # T-P33-15 合法建立後目標被換成 junction（清理前路徑變動）→ 重新驗證拒絕，原內容不變
    def swap_case(root, args, tag):
        create(root)
        _w(os.path.join(root, "data.txt"), "moved-" + tag)
        dst = os.path.join(moved, tag)
        os.rename(root, dst)
        t0 = SRC.list_tree(dst)
        jok = junction(root, dst)
        a = raises(clean, root, *args)
        return jok and a and os.path.isjunction(root) and SRC.list_tree(dst) == t0
    p15 = proj_of("15")
    a15, ea15 = run_case(swap_case, eroot("e15"), (proj, True, ab), "env")
    b15, eb15 = run_case(swap_case, droot(p15), (p15, False), "def")
    check("T-P33-15", f"清理前目標路徑變動（換成 junction）→ 拒絕、原內容不變（env={pf(a15)}／預設={pf(b15)}）",
          a15 and b15, ea15 + eb15)

    # T-P33-16 核准範圍外 sentinel 與其他目錄內容不變
    r16 = [SRC.list_tree(outside) == out_before, os.path.isdir(ab), os.path.isdir(abt),
           all(os.path.isfile(os.path.join(moved, t, "data.txt")) for t in ("env", "def"))]
    check("T-P33-16", f"核准範圍外 sentinel 及其他目錄內容不變（{r16}）", all(r16), str(r16))


# =============================================================================
# 3N：多 Session／N Cycle 完整流程（A～J）
# =============================================================================
F_, S_ = None, None
PAIRS = {}


def _seq_pattern(seq):
    pat = [("I", 30)]
    for ch in seq:
        pat += [(ch, 90), ("I", 70)]
    return pat


def _expected_pairs(seq):
    F, S = AN.PAIR_FIRST, AN.PAIR_SECOND
    table = {"DC": [F, S], "DCDC": [F, S] * 2, "DCDCDC": [F, S] * 3, "DCDCDCDC": [F, S] * 4,
             "CDC": [AN.UNP_HEAD, F, S], "DCD": [F, S, AN.UNP_TAIL], "DDC": [AN.UNP_SAME, F, S],
             "C": [AN.UNP_HEAD], "D": [AN.UNP_TAIL], "CDCD": [AN.UNP_HEAD, F, S, AN.UNP_TAIL]}
    return table[seq]


def _nc_session(cid, day, seq, *, report=True, labels=False):
    start = T0 + timedelta(days=day)
    d = make_session(cid, start, _seq_pattern(seq), report=False)
    if labels:
        sa = AN.analyze_session(SRC.load_session(d, os.path.dirname(d)), 1)
        decs = [{"type": "stage_label", "stage_start": s["stage_start"], "label": "滿放" if s["dir"] == "D" else "滿充",
                 "at": json.load(_real_open(os.path.join(d, "cert_state.json"), encoding="utf-8"))["started_at"].replace("T", " ")}
                for s in sa["stages"]]
        _jl(os.path.join(d, "decisions.jsonl"), decs)
    if report:
        make_original(d)
    return d


def _sheet_count(path, sheet, col, pred):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    n = sum(1 for row in wb[sheet].iter_rows(values_only=True) if len(row) > col and pred(row[col]))
    wb.close()
    return n


def _nc_case(tag, specs, *, expect_overall, mutate=None, done_cids=()):
    """specs：[(cid, seq, opts dict)]；回傳 None。逐項檢查 1～11。"""
    srcs = []
    for i, (cid, seq, opts) in enumerate(specs):
        srcs.append(_nc_session(cid, 900 + 10 * len(RESULTS) + i * 2, seq, **opts))
    if mutate:
        mutate(srcs)
    before = {s: SRC.list_tree(s) for s in srcs}
    rc, last, _ = run_tool(srcs, now=nxt_now())
    md = last.get("merge_dir")
    if not md:
        check(f"T-NC-{tag}", "完整流程執行（工具拒絕）", False, json.dumps(last, ensure_ascii=False))
        return
    st, man = status_of(md), manifest_of(md)
    exp_stages = sum(len(seq) for _, seq, _ in specs)
    exp_pairs = {cid: _expected_pairs(seq) for cid, seq, _ in specs}
    exp_cycles = sum(p.count(AN.PAIR_FIRST) for p in exp_pairs.values())
    check(f"T-NC-{tag}-1", f"Session 數＝{len(specs)}", len(st["sessions"]) == len(specs))
    check(f"T-NC-{tag}-2", f"階段數＝{exp_stages}", len(man["stages"]) == exp_stages, str(len(man["stages"])))
    check(f"T-NC-{tag}-3", f"循環數＝{exp_cycles}", len(man["cycles"]) == exp_cycles, str(len(man["cycles"])))
    got = {}
    for s in man["stages"]:
        got.setdefault(s["id"].split("#")[0], []).append(s["pairing"])
    check(f"T-NC-{tag}-4", "配對結果逐階段相符（含未配對、不跨 Session）", got == exp_pairs,
          json.dumps({k: v for k, v in got.items() if v != exp_pairs.get(k)}, ensure_ascii=False)[:400])
    ok_status = True
    for c in man["cycles"]:
        cid = c["id"].split("#")[0]
        seq = [q for i_, q, _ in specs if i_ == cid][0]
        want_cert = AN.PAIR_PENDING if seq == "DDC" else AN.CERTAIN
        want_status = AN.ST_DONE if cid in done_cids else AN.ST_PENDING
        if c["certainty"] != want_cert or c["status"] != want_status:
            ok_status = False
    check(f"T-NC-{tag}-5", "完成狀態與配對確定性相符（未標記→待確認；有正確標記→確認完成；同向相鄰→配對待確認）", ok_status,
          json.dumps([(c["id"], c["certainty"], c["status"]) for c in man["cycles"]], ensure_ascii=False)[:400])
    names = files_in(md, ".xlsx")
    summ = [n for n in names if os.path.basename(n).startswith("整合")]
    if summ:
        sp = os.path.join(md, summ[0])
        from openpyxl import load_workbook
        wb = load_workbook(sp, read_only=True)
        sheets = wb.sheetnames
        wb.close()
        n_src_files = sum(len(v) for v in before.values())
        ok9 = (sheets == XL.SHEETS
               and _sheet_count(sp, "階段清單", 0, lambda v: isinstance(v, str) and "#ST" in v) == exp_stages
               and _sheet_count(sp, "循環判定", 1, lambda v: isinstance(v, str) and "#CY" in v) == exp_cycles
               and _sheet_count(sp, "整合Summary", 1, lambda v: isinstance(v, str) and "#CY" in v) == exp_cycles
               and _sheet_count(sp, "Session總覽", 1, lambda v: isinstance(v, str) and v.startswith("CERT_")) == len(specs)
               and _sheet_count(sp, "資料品質", 1, lambda v: isinstance(v, str) and v.startswith("CERT_")) == len(specs)
               and _sheet_count(sp, "版本資訊", 1, lambda v: isinstance(v, str) and v.startswith("CERT_")) == len(specs)
               and _sheet_count(sp, "來源追溯", 1, lambda v: isinstance(v, str) and v.startswith("CERT_")) >= n_src_files)
        check(f"T-NC-{tag}-6", "整合 Excel 9 頁，各頁筆數（階段、循環、Session、品質、版本、來源）相符", ok9, str(sheets))
    else:
        check(f"T-NC-{tag}-6", "整合 Excel 已產生", False, str(names))
    det = [n for n in names if "重產明細_S" in n]
    check(f"T-NC-{tag}-7", f"重產明細檔數＝{len(specs)}", len(det) == len(specs), str(det))
    check(f"T-NC-{tag}-8", f"STATUS 整體＝{expect_overall}；manifest 存在且 overall 相同",
          st["overall"] == expect_overall and man.get("overall") == expect_overall and st["stage"] == "DONE",
          f"{st['overall']} {st.get('reason')}")
    if expect_overall in ("COMPLETED", "COMPLETED_WITH_UNVERIFIED"):
        check(f"T-NC-{tag}-9", "SHA-256：判定規則三項成立（STATUS、manifest、各正式檔）", is_published(md))
        check(f"T-NC-{tag}-10", "發布成功：publish.state＝PUBLISHED、無 .partial",
              st["publish"]["state"] == "PUBLISHED" and not any(n.endswith(".partial.xlsx") for n in names))
    else:
        mp = os.path.join(md, "merge_manifest.json")
        check(f"T-NC-{tag}-9", "SHA-256：manifest 雜湊＝STATUS 記錄；判定為無效",
              os.path.exists(mp) and SRC.sha256_file(mp) == st.get("manifest_sha256") and not is_published(md))
        check(f"T-NC-{tag}-10", "拒絕發布：全部 .partial、publish.state 非 PUBLISHED",
              all(n.endswith(".partial.xlsx") for n in names) and st["publish"]["state"] != "PUBLISHED")
    after = {s: SRC.list_tree(s) for s in srcs}
    snap_ok = all(len({f["h1"], f["h2"], f["h3"], f["h4"], f["h5"]}) == 1 for s in man["sources"] for f in s["files"])
    check(f"T-NC-{tag}-11", "原始 Session 逐檔不變；快照 H1＝H2＝H3＝H4＝H5", after == before and snap_ok)
    return md


def t_multi_session():
    print("== T-NC 多 Session／N Cycle 完整流程（A～J） ==")
    N = {}
    guarded_case = lambda f: f()      # noqa: E731
    _nc_case("A", [("CERT_NA1", "DC", N), ("CERT_NA2", "DC", N)], expect_overall="COMPLETED")
    _nc_case("B", [(f"CERT_NB{i}", "DC", N) for i in range(1, 4)], expect_overall="COMPLETED")
    _nc_case("C", [("CERT_NC1", "DC", N), ("CERT_NC2", "DCDC", N), ("CERT_NC3", "DCDCDC", N), ("CERT_NC4", "DC", N)],
             expect_overall="COMPLETED")
    _nc_case("D", [("CERT_ND1", "CDC", N), ("CERT_ND2", "DCD", N), ("CERT_ND3", "DC", N), ("CERT_ND4", "DDC", N),
                   ("CERT_ND5", "C", N)], expect_overall="COMPLETED")
    _nc_case("E", [("CERT_NE1", "D", N), ("CERT_NE2", "DC", N), ("CERT_NE3", "DCDC", N), ("CERT_NE4", "DCDCDC", N),
                   ("CERT_NE5", "CDCD", N), ("CERT_NE6", "DCDCDCDC", N)], expect_overall="COMPLETED")
    _nc_case("F", [("CERT_NF1", "DCDCDCDC", {"labels": True}), ("CERT_NF2", "DC", N)], expect_overall="COMPLETED",
             done_cids=("CERT_NF1",))
    # G：超過 6 個 Session → 拒絕
    srcs = [_nc_session(f"CERT_NG{i}", 1400 + i * 2, "DC") for i in range(1, 8)]
    out_g = os.path.join(WORK, "out_g")
    rc, last, _ = run_tool(srcs, out=out_g, now=nxt_now())
    check("T-NC-G", "7 個 Session → rc=2（V1）、不建立任何 MERGE、不發布",
          rc == 2 and last.get("code") == "V1" and not os.path.exists(out_g), str(last))
    _nc_case("H", [("CERT_NH1", "DCD", N), ("CERT_NH2", "CDC", N)], expect_overall="COMPLETED")

    def break_s2(srcs_):
        from openpyxl import load_workbook
        rp = os.path.join(srcs_[1], "reports", f"認證測試報告_{os.path.basename(srcs_[1])}_01.xlsx")
        wb = load_workbook(rp)
        wb.worksheets[3]["B3"] = 999.9
        wb.save(rp)
    _nc_case("I", [(f"CERT_NI{i}", "DC", N) for i in range(1, 4)], expect_overall="VERIFY_FAILED", mutate=break_s2)
    md = _nc_case("J", [("CERT_NJ1", "DC", N), ("CERT_NJ2", "DCDC", N), ("CERT_NJ3", "DC", {"report": False})],
                  expect_overall="COMPLETED_WITH_UNVERIFIED")
    if md:
        names = files_in(md, ".xlsx")
        check("T-NC-J-12", "UNVERIFIABLE：整合報告 _含未驗證明細、該 Session 明細 _資料值未驗證、其他無後綴",
              any("_含未驗證明細" in n for n in names) and any("S3_CERT_NJ3_資料值未驗證" in n for n in names)
              and sum(1 for n in names if "_資料值未驗證" in n) == 1, str(names))


def t_env_safety(c1):
    print("== 3A 測試環境與安全守門 ==")
    loaded = [m for m in ("cert_runtime", "cert_service", "api_client", "report_monitor", "auto_control", "bess_request",
                          "cert_env", "env_data_scraper") if m in sys.modules]
    check("3A-1", "import 全部新模組後，禁止模組（含間接）未載入", not loaded, str(loaded))
    probe = os.path.join(os.path.dirname(WORK), "outside_probe.txt")
    blocked = False
    try:
        _guarded_open(probe, "w")
    except Abort:
        blocked = True
    check("3A-2", "測試寫入守門：寫到 --work 以外 → 被攔下（未建立檔案）", blocked and not os.path.exists(probe))
    check("3A-3", "--work 位於隔離 scratchpad、不在正式版或開發 clone 內",
          not SRC.is_inside(WORK, SRC.PROD_ROOT) and not SRC.is_inside(WORK, os.path.dirname(HERE))
          and "scratchpad" in WORK.lower())
    check("3A-4", "V7 使用隔離 active.json（--work 內），不是正式版路徑",
          SRC.is_inside(ACTIVE, WORK) and SRC.norm(ACTIVE) != SRC.norm(SRC.PROD_ACTIVE_JSON))
    if c1:
        check("3A-5", "--c1 指向 scratchpad 內的 C1 副本、不在正式版內",
              os.path.isdir(c1) and not SRC.is_inside(c1, SRC.PROD_ROOT) and "scratchpad" in c1.lower()
              and os.path.basename(os.path.normpath(c1)) == "CERT_20261112_111816")
    else:
        skip("3A-5", "--c1 路徑檢查", "未提供 --c1")
    check("3A-6", "python 以 -B 執行（不寫 .pyc）", sys.flags.dont_write_bytecode == 1)
    check("3A-7", "python 以 -I 執行（隔離模式）", sys.flags.isolated == 1)


def t_tempdir():
    print("== TT 暫存位置隔離（openpyxl 暫存檔只在 MERGE\\_tmp／--work\\_tmp） ==")
    import tempfile
    sys_tmp = os.path.dirname(WORK)
    while os.path.basename(sys_tmp).lower() != "temp" and os.path.dirname(sys_tmp) != sys_tmp:
        sys_tmp = os.path.dirname(sys_tmp)
    def sys_openpyxl():
        try:
            return sorted(f for f in os.listdir(sys_tmp) if f.startswith("openpyxl."))
        except OSError:
            return None
    before_sys = sys_openpyxl()
    check("TT-0", "測試端 tempfile 指向 --work\\_tmp", SRC.norm(tempfile.gettempdir()) == SRC.norm(TEST_TMP))
    a = make_session("CERT_TT_1", T0 + timedelta(days=500), STD)
    b = make_session("CERT_TT_2", T0 + timedelta(days=501), STD)
    seen, nested = {}, {}

    def on_stage(s):
        if s in ("WRITE_DETAILS", "WRITE_SUMMARY", "PUBLISHING"):
            seen[s] = tempfile.gettempdir()
        if s == "VERIFY" and "rc" not in nested:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                nested["rc"] = MR.main(["--source", a, "--source", b, "--cycle-order", "DC", "--out-root",
                                        os.path.join(WORK, "out_nested")], active_json=ACTIVE)
            nested["out"] = buf.getvalue()
    rc, last, _ = run_tool([a, b], now=nxt_now(), hooks={"on_stage": on_stage})
    md = last.get("merge_dir")
    want = SRC.norm(os.path.join(md, "_tmp")) if md else None
    check("TT-1", "WRITE_DETAILS／WRITE_SUMMARY／PUBLISHING 期間 tempfile 指向本次 MERGE\\_tmp",
          rc == 0 and set(seen) == {"WRITE_DETAILS", "WRITE_SUMMARY", "PUBLISHING"}
          and all(SRC.norm(v) == want for v in seen.values()), f"{rc} {seen}")
    check("TT-2", "正常完成後 tempfile 還原為呼叫前的值", SRC.norm(tempfile.gettempdir()) == SRC.norm(TEST_TMP))
    check("TT-3", "同程序內重入／並行呼叫 → rc=2（CONCURRENT），不建立輸出",
          nested.get("rc") == 2 and '"CONCURRENT"' in nested.get("out", "")
          and not os.path.exists(os.path.join(WORK, "out_nested")))
    c = make_session("CERT_TT_3", T0 + timedelta(days=502), STD)
    d = make_session("CERT_TT_4", T0 + timedelta(days=503), STD)

    def boom(sessions):
        raise RuntimeError("（測試注入）分析例外")
    rc2, last2, _ = run_tool([c, d], now=nxt_now(), hooks={"after_analysis": boom})
    check("TT-4", "例外中止（analysis_failed）後 tempfile 還原",
          rc2 == 1 and status_of(last2["merge_dir"])["reason"] == "analysis_failed"
          and SRC.norm(tempfile.gettempdir()) == SRC.norm(TEST_TMP))
    e = make_session("CERT_TT_5", T0 + timedelta(days=504), STD)
    f = make_session("CERT_TT_6", T0 + timedelta(days=505), STD)

    def die(x, y):
        raise SystemExit("（測試注入）程序終止")
    try:
        run_tool([e, f], now=nxt_now(), hooks={"rename": die})
    except SystemExit:
        pass
    check("TT-5", "SystemExit 中斷後 tempfile 仍還原", SRC.norm(tempfile.gettempdir()) == SRC.norm(TEST_TMP))
    after_sys = sys_openpyxl()
    check("TT-6", "系統暫存資料夾的 openpyxl.* 清單前後相同（沒有新增、沒有刪除他人的檔案）",
          before_sys is not None and before_sys == after_sys, f"{before_sys} → {after_sys}")
    check("TT-7", "MERGE\\_tmp 位於本次 MERGE 資料夾內", md and SRC.is_inside(os.path.join(md, "_tmp"), md)
          and os.path.isdir(os.path.join(md, "_tmp")))


def _src_func(path, name):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise KeyError(name)


def _strip_doc(node):
    node = ast.parse(ast.unparse(node)).body[0]
    node.decorator_list = []
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) \
            and isinstance(node.body[0].value.value, str):
        node.body = node.body[1:]
    return node


def t_equivalence():
    print("== T-ENV／T-N3 重建邏輯等價性（讀原始碼文字，不 import 來源模組） ==")
    env_src = os.path.join(HERE, "cert_env.py")
    rt_src = os.path.join(HERE, "cert_runtime.py")
    mine = os.path.join(HERE, "cert_merge_xlsx.py")
    an = os.path.join(HERE, "cert_merge_analysis.py")
    tree = ast.parse(open(mine, encoding="utf-8").read())
    envcls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ENV"][0]
    meth = {n.name: n for n in envcls.body if isinstance(n, ast.FunctionDef)}
    for fn in ("_temp", "summary_lines", "failure_note"):
        a = ast.unparse(_strip_doc(_src_func(env_src, fn)))
        b = ast.unparse(_strip_doc(meth[fn])).replace("ENV.", "")
        check("T-ENV", f"{fn} 與 cert_env.{fn} 逐字等價（正規化：去 ENV. 前綴、@staticmethod、docstring）", a == b)
    src_txt = open(env_src, encoding="utf-8").read()
    check("T-ENV", "常數 SNAPSHOT_FILE／NORMAL／ABNORMAL／READ_FAIL 相同",
          'SNAPSHOT_FILE = "env_start_snapshot.json"' in src_txt
          and 'NORMAL, ABNORMAL, READ_FAIL = "正常", "異常", "讀取失敗"' in src_txt
          and (XL.ENV.SNAPSHOT_FILE, XL.ENV.NORMAL, XL.ENV.ABNORMAL, XL.ENV.READ_FAIL)
          == ("env_start_snapshot.json", "正常", "異常", "讀取失敗"))
    snap = {"schema": 1, "aircon": {"cooling": {"value": "21"}, "heating": {"value": "17.5"}},
            "devices": [{"name": "冷氣", "status": "正常"}, {"name": "消防", "status": "異常"}],
            "endpoints": {"ttyS0": {"ok": True}, "fire": {"ok": False}}, "captured_at": "2026-12-01T09:00:00"}
    cases = [
        (XL.ENV.summary_lines(snap, {}), ("製冷 21°C／製熱 17.5°C", "消防：異常")),
        (XL.ENV.summary_lines(None, {"aircon": "製冷21", "hardware": ""}), ("製冷21（人工輸入）", "N/A")),
        (XL.ENV.summary_lines({"schema": 1, "aircon": {}, "devices": []}, None), ("N/A", "N/A")),
        (XL.ENV.failure_note(snap), "認證開始環控快照（2026-12-01T09:00:00）：fire 讀取失敗（相關設備狀態無法取得，不列為異常）"),
        (XL.ENV.failure_note({"schema": 1, "endpoints": {"a": {"ok": True}}}), None),
        (XL.ENV.failure_note(None), None)]
    check("T-ENV", "多組環控快照輸入的輸出符合原函式邏輯", all(x == y for x, y in cases), str([x for x, y in cases if x != y]))
    a = ast.unparse(_src_func(rt_src, "build_sampling_slots")).replace("REQ.read_json(", "_read_json(")
    b = ast.unparse(_src_func(mine, "build_sampling_slots"))
    check("T-N3", "build_sampling_slots 與 cert_runtime 逐字等價（只差 REQ.read_json→_read_json）", a == b)
    for fn, f2 in (("params_note", mine), ("hmi_reader_note", mine), ("record_to_obs", an)):
        a = ast.unparse(_strip_doc(_src_func(rt_src, fn)))
        b = ast.unparse(_strip_doc(_src_func(f2, fn)))
        check("T-N3", f"{fn} 與 cert_runtime 等價（忽略 docstring）", a == b)
    rt_txt = open(rt_src, encoding="utf-8").read()
    check("T-N3", "HMI_READER_DESC 文字相同", all(f'"{k}": "{v}"' in rt_txt for k, v in XL.HMI_READER_DESC.items()))


# =============================================================================
# 3S：r8（G5-B）— 參數一致性 PRM、stage_count 辨識 SCR、選項甲 PA、新版 dec_v1／NK、競態 RC、舊版時間路徑 LG、
#     基準重放與差異歸因 MR／F3／X10、三種保證 GU。全部在 --work 下的合成 Session；替身注入只改本次輸出檔。
# =============================================================================
S_ABORT = [("I", 30), ("D", 200), ("I", 90), ("C", 40), ("I", 90), ("C", 200), ("I", 90)]   # D1、C（中止）、C2
GOLD_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
GOLD_TWO = "3771fb150709bab134744b9711584b767822760d127715fb4d3567a22d3ec5a5"
REASON3S = "誤操作：原定執行第二次放電，誤啟動充電，約 40 秒後停止；不納入計次。"
S3_083020_REASON = "誤操作：原定執行第二次放電，誤啟動充電，約 30 秒後停止；此階段標記為中止充電，不納入充放電計次。"


def _lab(ss, lab, at, rid):
    return {"type": "stage_label", "stage_start": ss, "label": lab, "note": "", "at": at, "by": "test", "request_id": rid}


def _cnt(ss, counted, at, rid, reason=REASON3S):
    return {"type": "stage_count", "stage_start": ss, "counted": counted, "reason": reason, "note": "", "at": at,
            "by": "test", "request_id": rid}


def _append(path, recs):
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    _w(path, old + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))


def _starts(d):
    sa = AN.analyze_session(SRC.load_session(d, os.path.dirname(d)), 1)
    return [s["stage_start"] for s in sa["stages"]]


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def s3_session(name, day, pattern=S_ABORT, *, base_decs=None, old=False, end_report=True, tamper=False):
    """合成 Session：先寫基準決策（base_decs(starts, ended) → list）再產原始報告；old＝移除 keywords（舊版報告）；
    end_report＝寫 events.jsonl（end → report）；tamper＝原始報告階段頁 B3 竄改。回傳 (資料夾, 階段開始清單, ended datetime, 報告路徑)。"""
    d = make_session(name, T0 + timedelta(days=day), pattern, report=False)
    ss = _starts(d)
    st = json.load(open(os.path.join(d, "cert_state.json"), encoding="utf-8"))
    ended = datetime.strptime(st["ended_at"], "%Y-%m-%dT%H:%M:%S")
    if base_decs:
        _jl(os.path.join(d, "decisions.jsonl"), base_decs(ss, ended))
    rp = make_original(d)
    if tamper:
        from openpyxl import load_workbook
        wb = load_workbook(rp)
        wb.worksheets[3]["B3"] = 99.9
        wb.save(rp)
    if old:
        _strip_keywords(rp)
    if end_report:
        _jl(os.path.join(d, "events.jsonl"), [{"t": st["ended_at"], "type": "end", "detail": "結束"},
                                              {"t": st["ended_at"], "type": "report", "detail": f"產生報告 {os.path.basename(rp)}"}])
    os.utime(rp, (ended.timestamp(), ended.timestamp()))
    return d, ss, ended, rp


def _strip_keywords(path):
    import re
    import zipfile
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    tmp = path + ".kw_tmp"
    with zipfile.ZipFile(tmp, "x", zipfile.ZIP_DEFLATED) as z:
        for i, b in items:
            if i.filename == "docProps/core.xml":
                b = re.sub(rb"<cp:keywords>.*?</cp:keywords>|<cp:keywords\s*/>", b"", b, flags=re.S)
            z.writestr(i, b)
    os.replace(tmp, path)


def _set_keywords(path, kw):
    import re
    import zipfile
    from xml.sax.saxutils import escape
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    tmp = path + ".kw_tmp"
    with zipfile.ZipFile(tmp, "x", zipfile.ZIP_DEFLATED) as z:
        for i, b in items:
            if i.filename == "docProps/core.xml":
                b = re.sub(rb"<cp:keywords>.*?</cp:keywords>", b"<cp:keywords>" + escape(kw).encode("utf-8") + b"</cp:keywords>",
                           b, flags=re.S)
            z.writestr(i, b)
    os.replace(tmp, path)


def _touch(path, dt):
    os.utime(path, (dt.timestamp(), dt.timestamp()))


def _sess(md, cid):
    return [s for s in status_of(md)["sessions"] if s["cert_id"] == cid][0]


def _dv(md, cid):
    return [s for s in manifest_of(md)["sources"] if s["cert_id"] == cid][0]["data_value_verification"]


def _item(v, prefix):
    return next((i for i in v["items"] if i["item"].startswith(prefix)), None)


def _inject(cid, fn):
    """before_verify 替身：修改該 Session 的 R_all（本次輸出檔），模擬範圍外差異。"""
    def hook(sessions, details):
        from openpyxl import load_workbook
        p = details[cid]["path"]
        wb = load_workbook(p)
        fn(wb)
        wb.save(p)
    return hook


def _hooks(*hs):
    def run(sessions, details):
        for h in hs:
            h(sessions, details)
    return {"before_verify": run}


def t_3s():
    print("== 3S r8（G5-B）==")
    # ------------------------------------------------------------------ PRM 參數一致性（雙邊行為）
    import subprocess
    base = CFG.params()
    cases = {"full": dict(base), "extra_key": dict(base, FUTURE_X=1), "no_params": None, "params_list": [1]}
    for k in AN.REQUIRED_PARAMS:
        p = dict(base)
        del p[k]
        cases[f"miss_{k}"] = p
    p = dict(base)
    del p["MINUTE_TOL_SEC"]
    cases["miss_MINUTE_TOL_SEC"] = p
    for v_name, v in (("neg", -1), ("str", "20"), ("bool", True)):
        cases[f"MINUTE_TOL_{v_name}"] = dict(base, MINUTE_TOL_SEC=v)
    for k in [x for x in AN.REQUIRED_PARAMS if x != "SENSOR_VALID_RANGE"]:
        for v_name, v in (("true", True), ("false", False), ("nan", float("nan")), ("inf", float("inf")),
                          ("ninf", float("-inf")), ("str", "60"), ("null", None), ("list", [60]), ("zero", 0), ("neg", -1)):
            cases[f"{k}_{v_name}"] = dict(base, **{k: v})
    for v_name, v in (("null", None), ("ok", [0, 80]), ("tuple", (0, 80.5)), ("rev", [80, 0]), ("eq", [5, 5]),
                      ("bool", [True, 5]), ("nan", [0, float("nan")]), ("three", [1, 2, 3]), ("str", "0-80"), ("mixed", [0, "80"])):
        cases[f"SVR_{v_name}"] = dict(base, SENSOR_VALID_RANGE=v)
    states = {n: ({} if v is None else {"params": v}) for n, v in cases.items()}

    def norm_res(r):
        sp, errs, notes = r
        return {"valid": sp is not None, "errs": sorted(e.split("：")[0] for e in errs), "notes": sorted(notes),
                "params": json.dumps(sp, sort_keys=True, default=str) if sp is not None else None}
    mine = {n: norm_res(AN.session_params_check(s)) for n, s in states.items()}
    code = ("import json,sys\nsys.path.insert(0, sys.argv[1])\nimport cert_runtime as CR\nst=json.loads(sys.stdin.read())\n"
            "out={}\nfor n,s in st.items():\n    sp,e,no=CR.session_params_check(s)\n"
            "    out[n]={'valid':sp is not None,'errs':sorted(x.split('：')[0] for x in e),'notes':sorted(no),"
            "'params':json.dumps(sp,sort_keys=True,default=str) if sp is not None else None}\nprint(json.dumps(out))\n")
    r = subprocess.run([sys.executable, "-I", "-B", "-c", code, HERE], input=json.dumps(states), capture_output=True,
                       text=True, encoding="utf-8", env=_sub_env(), timeout=120)
    theirs = json.loads(r.stdout) if r.returncode == 0 else {}
    diff = [n for n in states if mine[n] != theirs.get(n)]
    check("PRM-C1", f"參數驗證雙邊行為一致（C 重建 vs cert_runtime，獨立子行程；{len(states)} 個輸入：合法與否、錯誤參數、提示、回傳參數）",
          r.returncode == 0 and not diff and len(theirs) == len(states), str(diff[:5]) + r.stderr[-300:])
    check("PRM-C1b", "矩陣涵蓋：9 必要參數逐項缺少、MINUTE_TOL 缺少／不合法、bool／NaN／±Inf／字串、SENSOR_VALID_RANGE、未知鍵",
          all(f"miss_{k}" in states for k in AN.REQUIRED_PARAMS) and "miss_MINUTE_TOL_SEC" in states and "extra_key" in states
          and mine["miss_MINUTE_TOL_SEC"]["valid"] and not mine["SVR_bool"]["valid"] and mine["SVR_null"]["valid"]
          and mine["extra_key"]["valid"] and not mine["W_PAUSE_SEC_nan"]["valid"])
    rt_path = os.path.join(HERE, "cert_runtime.py")
    same = all(ast.dump(_strip_doc(_src_func(rt_path, n))) == ast.dump(_strip_doc(_src_func(os.path.join(HERE, "cert_merge_analysis.py"), n)))
               for n in ("_finite_num", "_param_ok", "session_params_check"))
    check("PRM-C4", "（輔助）原始碼比對：_finite_num／_param_ok／session_params_check 與 cert_runtime 相同（不 import）", same)
    bad = dict(base)
    del bad["W_PAUSE_SEC"]
    pa_ = make_session("CERT_S3_PRMA", T0 + timedelta(days=400), STD)
    pb_ = make_session("CERT_S3_PRMB", T0 + timedelta(days=401), STD, params=bad, report=False)   # 參數不合法 → 本工具不能產原始報告
    out_before = sorted(os.listdir(os.path.join(WORK, "out"))) if os.path.isdir(os.path.join(WORK, "out")) else []
    rc, last, _t = run_tool([pa_, pb_], now=nxt_now())
    out_after = sorted(os.listdir(os.path.join(WORK, "out"))) if os.path.isdir(os.path.join(WORK, "out")) else []
    check("PRM-C2", "原始參數不合法 → 前置驗證拒絕 V10、rc=2、不建立 MERGE 輸出（不以目前設定補齊）",
          rc == 2 and last.get("code") == "V10" and "W_PAUSE_SEC" in str(last.get("reason")) and out_before == out_after, str(last))
    pc_ = make_session("CERT_S3_PRMC", T0 + timedelta(days=402), STD, params=dict(base, FUTURE_X=7))
    sa_c = AN.analyze_session(SRC.load_session(pc_, os.path.dirname(pc_)), 1)
    check("PRM-C3", "未知額外參數保留（params_info 記錄、cert_state 原值不變），分析只用已知鍵、不補值",
          sa_c["params_info"]["unknown_keys"] == ["FUTURE_X"] and "FUTURE_X" not in sa_c["params"]
          and json.load(open(os.path.join(pc_, "cert_state.json"), encoding="utf-8"))["params"]["FUTURE_X"] == 7)

    # ------------------------------------------------------------------ SCR stage_count 辨識（H2 結果）
    print("-- SCR --")
    d_s, ss, ended, _rp = s3_session("CERT_S3_SCR1", 410, base_decs=lambda ss, e: [
        _lab(ss[1], "中止", _ts(e - timedelta(hours=1)), "L1"), _cnt(ss[1], False, _ts(e - timedelta(minutes=50)), "C1"),
        _lab(ss[2], "滿充", _ts(e - timedelta(minutes=49)), "L2"), _cnt(ss[2], False, _ts(e - timedelta(minutes=48)), "C2"),
        _cnt("2000-01-01T00:00:00", False, _ts(e - timedelta(minutes=47)), "C3"), {"type": "something", "at": "x"},
        _cnt(ss[1], False, _ts(e - timedelta(minutes=46)), "C1")])
    sa = AN.analyze_session(SRC.load_session(d_s, os.path.dirname(d_s)), 1)
    AN.pair_session(sa, "DC")
    st_ = {s["stage_start"]: s for s in sa["stages"]}
    check("SCR-01", "有效不計次（標記中止＋counted=false，H2 判定）→ 不參與配對，原始階段保留",
          st_[ss[1]]["count_counted"] is False and st_[ss[1]]["pairing"] == AN.UNC_EXCLUDED and len(sa["stages"]) == 3)
    check("SCR-02", "標記不是「中止」的 counted=false → H2 判為無效 → 視為計次並列原因（不排除配對）",
          st_[ss[2]]["count_counted"] is True and "不是「中止」" in st_[ss[2]]["count_note"] and st_[ss[2]]["pairing"] != AN.UNC_EXCLUDED)
    kinds = [o["kind"] for o in sa["orphans"]]
    check("SCR-03", "對不到階段的 stage_count → 「無法對應的計次決策」（依 H2 count_orphans）", "無法對應的計次決策" in kinds)
    check("SCR-04", "其他未知類型仍為「無法辨識的紀錄」；有效 stage_count 不列為孤兒",
          "無法辨識的紀錄" in kinds and kinds.count("無法對應的計次決策") == 1)
    check("SCR-05", "request_id 重複 → 列為決策異常", any(a["request_id"] == "C1" for a in sa["decision_anomalies"]))
    check("SCR-06", "保留決策行號、stage_start、at、by、request_id、counted、原因；H2 count_history 完整",
          [c["request_id"] for c in sa["count_records"]] == ["C1", "C2", "C3", "C1"]
          and all(k in sa["count_records"][0] for k in ("line", "stage_start", "at", "by", "request_id", "counted", "reason"))
          and len(st_[ss[1]]["count_history"]) == 2)
    blank = s3_session("CERT_S3_SCR2", 411, base_decs=lambda ss, e: [
        _lab(ss[1], "中止", _ts(e - timedelta(hours=1)), "L1"), _cnt(ss[1], False, _ts(e - timedelta(minutes=50)), "C1"),
        _cnt(ss[1], False, _ts(e - timedelta(minutes=40)), "C2", reason="  ")])[0]
    sb_ = AN.analyze_session(SRC.load_session(blank, os.path.dirname(blank)), 1)
    s2 = sb_["stages"][1]
    check("SCR-07", "同一 stage_start 最後一筆無效（原因空白）→ 不回退前一筆、視為計次、列原因",
          s2["count_counted"] is True and "原因空白" in s2["count_note"] and len(s2["count_history"]) == 2)

    # ------------------------------------------------------------------ PA 選項甲與保護①②③（合成階段）
    print("-- PA --")
    _t3s_pairing()
    _t3s_083020()

    # ------------------------------------------------------------------ 新版 dec_v1（NK、案例 1～6、10）＋ GU
    print("-- NK／GU --")
    lab1 = lambda ss, e: [_lab(ss[1], "中止", _ts(e - timedelta(hours=1)), "L1")]                       # noqa: E731
    n1, ss1, e1, _ = s3_session("CERT_S3_N01", 420, base_decs=lab1)                                   # 案例 1
    n2, ss2, e2, _ = s3_session("CERT_S3_N02", 421, base_decs=lab1, tamper=True)                      # 案例 2
    n3, ss3, e3, _ = s3_session("CERT_S3_N03", 422, base_decs=lab1, tamper=True)                      # 案例 3
    _append(os.path.join(n3, "decisions.jsonl"), [_lab(ss3[2], "滿充", _ts(e3 + timedelta(days=1)), "L9")])
    n4, ss4, e4, _ = s3_session("CERT_S3_N04", 423, base_decs=lab1, tamper=True)                      # 案例 4
    _append(os.path.join(n4, "decisions.jsonl"), [_cnt(ss4[1], False, _ts(e4 + timedelta(days=1)), "C9")])
    n5, ss5, e5, _ = s3_session("CERT_S3_N05", 424, base_decs=lab1, tamper=True)                      # 案例 5
    _append(os.path.join(n5, "decisions.jsonl"), [_cnt("2000-01-01T00:00:00", False, _ts(e5 + timedelta(days=1)), "C8"),
                                                  _cnt(ss5[2], False, _ts(e5 + timedelta(days=1, seconds=5)), "C7")])
    n6, ss6, e6, _ = s3_session("CERT_S3_N06", 425, base_decs=lab1)                                   # 案例 6／NK-02／MR-02／X10-P1
    _append(os.path.join(n6, "decisions.jsonl"), [_cnt(ss6[1], False, _ts(e6 + timedelta(days=1)), "C6")])
    rc, last, _t = run_tool([n1, n2, n3, n4, n5, n6], now=nxt_now())
    md = last["merge_dir"]
    s1, v1 = _sess(md, "CERT_S3_N01"), _dv(md, "CERT_S3_N01")
    check("NK-01", "案例1：dec_v1 合法、雜湊相符、原始報告正確 → VERIFIED；決策快照 CONFIRMED；可追溯性已確認",
          s1["data_value_verification"] == "VERIFIED" and s1["decision_snapshot"] == "CONFIRMED"
          and s1["traceability"] == "已確認" and v1["decision_verification"]["path"] == "new", str(s1))
    s2_ = _sess(md, "CERT_S3_N02")
    check("NK-05", "案例2：雜湊相符但原始報告數值被竄改 → FAILED（UNEXPLAINED_VALUE_DIFF），決策快照仍 CONFIRMED",
          s2_["data_value_verification"] == "FAILED" and "UNEXPLAINED_VALUE_DIFF" in s2_["reason_codes"]
          and s2_["decision_snapshot"] == "CONFIRMED" and s2_["reproducibility"] == "FAILED", str(s2_))
    for tid, cid, txt in (("NK-05b", "CERT_S3_N03", "案例3：竄改後新增合法 stage_label"),
                          ("NK-05c", "CERT_S3_N04", "案例4：竄改後新增合法 stage_count"),
                          ("NK-05d", "CERT_S3_N05", "案例5：竄改後新增孤兒／無效決策")):
        sx = _sess(md, cid)
        check(tid, f"{txt} → 仍 FAILED（UNEXPLAINED_VALUE_DIFF；不被後續決策吸收）",
              sx["data_value_verification"] == "FAILED" and "UNEXPLAINED_VALUE_DIFF" in sx["reason_codes"]
              and sx["decision_snapshot"] == "CONFIRMED", str(sx))
    s6, v6 = _sess(md, "CERT_S3_N06"), _dv(md, "CERT_S3_N06")
    att = _item(v6, "D-A")
    check("NK-02", "案例6／MR-02：雜湊相符、報告後新增合法 stage_count → 基準重放 VERIFIED＋變更清單（不因人工決策一律 FAILED）",
          s6["data_value_verification"] == "VERIFIED" and att and att["result"] == "VERIFIED"
          and [c["request_id"] for c in v6["change_list"]] == ["C6"], str(s6) + str(att and att["detail"].get("problems")))
    check("X10-P1", "新增不計次 → 計次描述（階段計次＋決策 1/1）依決策集合重算逐字一致",
          att and att["detail"]["x10"]["result"] == "VERIFIED" and any("不計次；原因" in x for x in att["detail"]["x10"]["expected_all"]))
    check("GU-01", "STATUS／manifest 三種保證分開（決策快照、報告重現性、可追溯性）；FAILED 不發布正式檔名",
          all(k in s2_ for k in ("decision_snapshot", "reproducibility", "traceability")) and not is_published(md)
          and status_of(md)["overall"] == "VERIFY_FAILED")
    check("GU-03", "T23-5 型：決策快照 CONFIRMED 與報告重現性 FAILED 可同時成立（不互相推論）",
          s2_["decision_snapshot"] == "CONFIRMED" and s2_["reproducibility"] == "FAILED" and s2_["traceability"] == "已確認")

    m3, ss7, e7, rp7 = s3_session("CERT_S3_N07", 430, base_decs=lab1)                                # NK-03（MR-08）
    pth = os.path.join(m3, "decisions.jsonl")
    recs = [json.loads(x) for x in open(pth, encoding="utf-8").read().splitlines() if x.strip()]
    recs[0]["note"] = "內容被改"
    _jl(pth, recs)
    m4a, _s, _e, _r = s3_session("CERT_S3_N08", 431, base_decs=lab1)                                 # NK-04 筆數不足
    os.remove(os.path.join(m4a, "decisions.jsonl"))
    m4b, ss9, e9, _r = s3_session("CERT_S3_N09", 432, base_decs=lab1)                                # NK-04 前 N 筆內無效行
    _w(os.path.join(m4b, "decisions.jsonl"), "{bad json\n" + open(os.path.join(m4b, "decisions.jsonl"), encoding="utf-8").read())
    m4c, _s, _e, rpc = s3_session("CERT_S3_N10", 433, base_decs=lab1)                                # 案例10：格式錯誤
    _set_keywords(rpc, "dec_v=1;dec_n=one;dec_sha256=xyz")
    m4d, _s, _e, rpd = s3_session("CERT_S3_N11", 434, base_decs=lab1)                                # dec_v=2
    from cert_stage_engine import decisions_digest
    _set_keywords(rpd, f"dec_v=2;dec_n=1;dec_sha256={decisions_digest([lab1(_s, _e)[0]])[1]}")
    m3f, ss12, e12, _r = s3_session("CERT_S3_N12", 435, base_decs=lab1)                              # F3-03：基準不可確認＋結構差異
    p12 = os.path.join(m3f, "decisions.jsonl")
    r12 = [json.loads(x) for x in open(p12, encoding="utf-8").read().splitlines() if x.strip()]
    r12[0]["note"] = "改"
    _jl(p12, r12 + [_lab(ss12[2], "滿充", _ts(e12 + timedelta(days=1)), "L7")])
    rc, last, _t = run_tool([m3, m4a, m4b, m4c, m4d, m3f], now=nxt_now())
    md = last["merge_dir"]
    sx = _sess(md, "CERT_S3_N07")
    check("NK-03", "MR-08：筆數相同但內容被改 → 決策快照 UNVERIFIABLE、整體不判 VERIFIED",
          sx["decision_snapshot"] == "UNVERIFIABLE" and sx["data_value_verification"] != "VERIFIED", str(sx))
    ok4 = [(_sess(md, c)["decision_snapshot"], _sess(md, c)["data_value_verification"],
            _dv(md, c)["decision_verification"]["path"]) for c in ("CERT_S3_N08", "CERT_S3_N09", "CERT_S3_N10", "CERT_S3_N11")]
    check("NK-04", "筆數不足／前 N 筆內有無效行／keywords 格式錯誤／dec_v 不是 1 → UNVERIFIABLE（不判 VERIFIED）",
          all(a == "UNVERIFIABLE" and b != "VERIFIED" for a, b, _p in ok4), str(ok4))
    check("NK-04b", "案例10：新版 keywords 格式錯誤 → 仍為新版路徑（不降級為舊版時間路徑）",
          ok4[2][2] == "new" and ok4[3][2] == "new", str(ok4))
    s12 = _sess(md, "CERT_S3_N12")
    check("F3-03", "基準不可確認時結構差異不豁免（沿用 r7：原始報告 vs R_all → STRUCTURE FAILED）",
          s12["data_value_verification"] == "FAILED" and "STRUCTURE" in (s12["reason_codes"] or []), str(s12))
    check("X10-N4", "基準不可確認 → 不做差異歸因（不套 X10）", _item(_dv(md, "CERT_S3_N12"), "D-A") is None)
    check("NK-07", "dec_v1 黃金值（完整 SHA-256）：空清單、固定兩筆清單",
          decisions_digest([])[1] == GOLD_EMPTY and decisions_digest([{"b": 1, "a": "中止"}, {"type": "x", "v": True}])[1] == GOLD_TWO)

    # ------------------------------------------------------------------ MR／F3／X10（基準重放與差異歸因，含替身注入）
    print("-- MR／F3／X10 --")
    a1, sa1, ea1, _ = s3_session("CERT_S3_M01", 440, base_decs=lab1)                                 # MR-04：報告後加 stage_label
    _append(os.path.join(a1, "decisions.jsonl"), [_lab(sa1[2], "滿充", _ts(ea1 + timedelta(days=1)), "L8")])
    a2, sa2, ea2, _ = s3_session("CERT_S3_M02", 441, base_decs=lambda ss, e: lab1(ss, e) + [            # X10-P2
        _cnt(ss[1], False, _ts(e - timedelta(minutes=30)), "C1")])
    _append(os.path.join(a2, "decisions.jsonl"), [_cnt(sa2[1], True, _ts(ea2 + timedelta(days=1)), "C2", reason="恢復")])
    a3, sa3, ea3, _ = s3_session("CERT_S3_M03", 442, base_decs=lab1)                                 # X10-P3 孤兒
    _append(os.path.join(a3, "decisions.jsonl"), [_cnt("2001-01-01T00:00:00", False, _ts(ea3 + timedelta(days=1)), "C5")])
    a4, sa4, ea4, _ = s3_session("CERT_S3_M04", 443, base_decs=lab1)                                 # MR-11（數值替身）
    _append(os.path.join(a4, "decisions.jsonl"), [_cnt(sa4[1], False, _ts(ea4 + timedelta(days=1)), "C4")])
    a5, sa5, ea5, _ = s3_session("CERT_S3_M05", 444, base_decs=lab1)                                 # F3-02（頁籤替身）
    _append(os.path.join(a5, "decisions.jsonl"), [_cnt(sa5[1], False, _ts(ea5 + timedelta(days=1)), "C4")])
    a6, sa6, ea6, _ = s3_session("CERT_S3_M06", 445, base_decs=lab1)                                 # X10-N1 插入無關文字
    _append(os.path.join(a6, "decisions.jsonl"), [_cnt(sa6[1], False, _ts(ea6 + timedelta(days=1)), "C4")])

    def set_cell(wb):
        wb.worksheets[3]["B4"] = 12345.6

    def rename_sheet(wb):
        wb.worksheets[4].title = "不明頁"

    def add_line(wb):
        wb.properties.description = (wb.properties.description or "") + "\n插入的不相關文字"
    hk = _hooks(_inject("CERT_S3_M04", set_cell), _inject("CERT_S3_M05", rename_sheet), _inject("CERT_S3_M06", add_line))
    rc, last, _t = run_tool([a1, a2, a3, a4, a5, a6], now=nxt_now(), hooks=hk)
    md = last["merge_dir"]
    s_m1 = _sess(md, "CERT_S3_M01")
    check("MR-04", "報告後加 stage_label（改頁籤名稱）→ 不再判結構失敗，基準重放 VERIFIED＋變更清單",
          s_m1["data_value_verification"] == "VERIFIED" and len(_dv(md, "CERT_S3_M01")["change_list"]) == 1, str(s_m1))
    check("F3-01", "合法改名由 H2 _sheet_name 推導 → 原始報告 vs R_base 工作表清單相同、R_base vs R_all 名稱對照合法",
          _item(_dv(md, "CERT_S3_M01"), "F3") is None and _item(_dv(md, "CERT_S3_M01"), "D-A")["result"] == "VERIFIED")
    at2 = _item(_dv(md, "CERT_S3_M02"), "D-A")
    check("X10-P2", "已有一筆決策再新增第二筆（恢復計次）：k/n 合法重新編號、歷史保留 → VERIFIED",
          _sess(md, "CERT_S3_M02")["data_value_verification"] == "VERIFIED" and at2["detail"]["x10"]["result"] == "VERIFIED"
          and any("決策 2/2" in x for x in at2["detail"]["x10"]["expected_all"])
          and any("決策 1/1" in x for x in at2["detail"]["x10"]["expected_base"]), str(at2 and at2["detail"].get("problems")))
    at3 = _item(_dv(md, "CERT_S3_M03"), "D-A")
    check("X10-P3", "報告後新增對不到階段的計次決策 → 「未套用」描述行合法；孤兒列入 P-9",
          _sess(md, "CERT_S3_M03")["data_value_verification"] == "VERIFIED" and any("未套用" in x for x in at3["detail"]["x10"]["expected_all"]))
    p9 = []
    for rel in files_in(md, ".xlsx"):
        if rel.startswith("_verify"):
            continue
        from openpyxl import load_workbook
        wbx = load_workbook(os.path.join(md, rel), read_only=True)
        if "不確定停頓與標記紀錄" in wbx.sheetnames:
            p9 += [[str(v) for v in row if v is not None] for row in wbx["不確定停頓與標記紀錄"].iter_rows(values_only=True)]
        wbx.close()
    m03_orph = manifest_of(md)["stage_count_orphans"].get("CERT_S3_M03") or []
    check("MR-05", "無法對應的計次決策 → P-9（不確定停頓與標記紀錄）列出，manifest stage_count_orphans 記錄",
          bool(m03_orph) and any(r[:1] == ["S3"] and "無法對應的計次決策" in r for r in p9), str(m03_orph))   # S3＝CERT_S3_M03（第 3 個來源）
    a_m4, g_m4 = _fmt_cells(md, "CERT_S3_M04")
    check("FMT-11", "合法底色延伸同時伴隨量測數值竄改（替身）→ 底色格仍逐格歸因，但整體 FAILED（數值差異不被吸收）",
          bool(g_m4) and _sess(md, "CERT_S3_M04")["data_value_verification"] == "FAILED"
          and any("B4" in p_ for p_ in a_m4["detail"]["problems"]), str(a_m4 and a_m4["detail"].get("problems")))
    for tid, cid, txt in (("MR-11", "CERT_S3_M04", "只有 stage_count 的變更中混入被竄改的數值格（替身）"),
                          ("F3-02", "CERT_S3_M05", "R_all 出現不是由 H2 推導的頁籤名稱（替身）"),
                          ("X10-N1", "CERT_S3_M06", "描述插入不相關文字（替身）")):
        sx = _sess(md, cid)
        check(tid, f"{txt} → FAILED（DECISION_DIFF_OUT_OF_SCOPE）",
              sx["data_value_verification"] == "FAILED" and "DECISION_DIFF_OUT_OF_SCOPE" in (sx["reason_codes"] or []), str(sx))

    b1, sb1, eb1, _ = s3_session("CERT_S3_X01", 450, base_decs=lambda ss, e: lab1(ss, e) + [
        _cnt(ss[1], False, _ts(e - timedelta(minutes=30)), "C1")])
    xs = []
    for nm_, day in (("CERT_S3_X02", 451), ("CERT_S3_X03", 452), ("CERT_S3_X04", 453), ("CERT_S3_X05", 454)):
        dd, sss, ee, _ = s3_session(nm_, day, base_decs=lambda ss, e: lab1(ss, e) + [
            _cnt(ss[1], False, _ts(e - timedelta(minutes=30)), "C1")])
        _append(os.path.join(dd, "decisions.jsonl"), [_cnt(sss[1], True, _ts(ee + timedelta(days=1)), "C2", reason="恢復")])
        xs.append(dd)
    _append(os.path.join(b1, "decisions.jsonl"), [_cnt(sb1[1], True, _ts(eb1 + timedelta(days=1)), "C2", reason="恢復")])

    def del_hist(wb):
        wb.properties.description = "\n".join(x for x in (wb.properties.description or "").split("\n") if "決策 1/2" not in x)

    def tamper_hist(wb):
        wb.properties.description = (wb.properties.description or "").replace(REASON3S, "被竄改的原因")

    def tamper_params(wb):
        wb.properties.description = "\n".join(("取樣週期 99 秒（竄改）" if x.startswith("取樣週期") else x)
                                              for x in (wb.properties.description or "").split("\n"))

    def trunc(wb):
        wb.properties.description = (wb.properties.description or "") + "\n" + "長" * 3100

    def noop(wb):                                   # 控制組：替身工具本身（openpyxl 讀寫）不造成差異
        pass
    hk = _hooks(_inject("CERT_S3_X01", noop), _inject("CERT_S3_X02", del_hist), _inject("CERT_S3_X03", tamper_hist), _inject("CERT_S3_X04", tamper_params),
                _inject("CERT_S3_X05", trunc))
    rc, last, _t = run_tool([b1] + xs, now=nxt_now(), hooks=hk)
    md = last["merge_dir"]
    check("X10-P2b", "恢復計次（控制組：替身只讀寫不改內容）→ VERIFIED（證明反向案例不是替身工具造成）", _sess(md, "CERT_S3_X01")["data_value_verification"] == "VERIFIED",
          str(_sess(md, "CERT_S3_X01")))
    for tid, cid, txt in (("X10-N2a", "CERT_S3_X02", "原有決策歷史行遭刪除"), ("X10-N2b", "CERT_S3_X03", "原有決策歷史遭竄改"),
                          ("X10-N3", "CERT_S3_X04", "修改 params_note（實際分析參數說明）")):
        sx = _sess(md, cid)
        check(tid, f"{txt} → FAILED（DECISION_DIFF_OUT_OF_SCOPE；X10 不吸收）",
              sx["data_value_verification"] == "FAILED" and "DECISION_DIFF_OUT_OF_SCOPE" in (sx["reason_codes"] or []), str(sx))
    a_t = _item(_dv(md, "CERT_S3_X05"), "D-A")
    check("X10-N6", "描述達 3,000 字元截斷 → 無法完整驗證 → UNVERIFIABLE（不判 VERIFIED）",
          _sess(md, "CERT_S3_X05")["data_value_verification"] == "UNVERIFIABLE" and a_t["reason_code"] == "X10_TRUNCATED")
    c5, sc5, ec5, rp5 = s3_session("CERT_S3_X06", 455, base_decs=lambda ss, e: lab1(ss, e) + [
        _cnt(ss[1], False, _ts(e - timedelta(minutes=30)), "C1")])
    from openpyxl import load_workbook
    wb5 = load_workbook(rp5)
    wb5.properties.description = "\n".join(x for x in (wb5.properties.description or "").split("\n") if not x.startswith("　決策"))
    wb5.save(rp5)
    c6 = make_session("CERT_S3_X07", T0 + timedelta(days=456), STD)
    rc, last, _t = run_tool([c5, c6], now=nxt_now())
    sx = _sess(last["merge_dir"], "CERT_S3_X06")
    check("X10-N5", "原始報告 vs R_base 的計次描述差異 → 依 V-D FAILED（X10 不適用第一階段）",
          sx["data_value_verification"] == "FAILED" and "DESC_UNKNOWN_LINE" in (sx["reason_codes"] or []), str(sx))

    # ------------------------------------------------------------------ RC 競態
    print("-- RC --")
    r1d = make_session("CERT_S3_R01", T0 + timedelta(days=460), S_ABORT, report=False)
    rss = _starts(r1d)
    st_r = json.load(open(os.path.join(r1d, "cert_state.json"), encoding="utf-8"))
    er = datetime.strptime(st_r["ended_at"], "%Y-%m-%dT%H:%M:%S")
    _jl(os.path.join(r1d, "decisions.jsonl"), [_lab(rss[1], "中止", _ts(er - timedelta(hours=1)), "L1")])
    data = SRC.load_session(r1d, os.path.dirname(r1d))
    sar = AN.analyze_session(data, 1)                                    # 分析使用 1 筆
    _append(os.path.join(r1d, "decisions.jsonl"), [_cnt(rss[1], False, _ts(er + timedelta(seconds=1)), "RACE")])   # 分析後、存檔前
    meta, _ss = XL.detail_meta(sar, "TEMP_DISCLAIMER")
    meta["source_notes"] = [x for x in meta["source_notes"] if x != "TEMP_DISCLAIMER"]
    fn = "認證測試報告_CERT_S3_R01_01.xlsx"
    rpr = os.path.join(r1d, "reports", fn)
    os.makedirs(os.path.dirname(rpr), exist_ok=True)
    save_report(sar["res"], meta, rpr)
    st_r["reports"].append({"file": f"reports\\{fn}", "at": _ts(er + timedelta(seconds=2))})
    _w(os.path.join(r1d, "cert_state.json"), json.dumps(st_r, ensure_ascii=False, indent=2))
    r2d, rss2, er2, rp2 = s3_session("CERT_S3_R02", 461, base_decs=lab1, old=True)    # RC-03 舊版：同一競態落入窗口
    _append(os.path.join(r2d, "decisions.jsonl"), [_cnt(rss2[1], False, _ts(er2 + timedelta(seconds=1)), "RACE")])
    _touch(os.path.join(r2d, "decisions.jsonl"), er2 + timedelta(seconds=2))
    rc, last, _t = run_tool([r1d, r2d], now=nxt_now())
    md = last["merge_dir"]
    v_r1 = _dv(md, "CERT_S3_R01")
    check("RC-01", "MR-10：分析後、存檔前新增決策 → keywords 為分析時的 N（1），整合取前 N → VERIFIED；那筆列入變更清單",
          v_r1["decision_verification"]["evidence"].get("dec_n") == 1 and _sess(md, "CERT_S3_R01")["data_value_verification"] == "VERIFIED"
          and [c["request_id"] for c in v_r1["change_list"]] == ["RACE"], str(_sess(md, "CERT_S3_R01")))
    check("RC-03", "MR-10 舊版：同一競態（決策落在模糊窗口）→ UNVERIFIABLE",
          _sess(md, "CERT_S3_R02")["decision_snapshot"] == "UNVERIFIABLE"
          and "模糊窗口" in _dv(md, "CERT_S3_R02")["decision_verification"]["reason"])
    code = ("import json,sys,os\nsys.path.insert(0, sys.argv[1])\nimport cert_runtime as CR\n"
            "root, cid = sys.argv[2], sys.argv[3]\nrt = CR.CertRuntime(root, reader=None, enable_shots=False, log=lambda *a, **k: None)"
            "._offline(os.path.join(root, cid))\nr1 = rt.analysis(); r1b = rt.analysis()\n"
            "rt.decisions.append({'type':'stage_count','stage_start':sys.argv[4],'counted':False,'reason':'x','note':'',"
            "'at':'2099-01-01 00:00:00','by':'t','request_id':'R'})\nr2 = rt.analysis()\n"
            "from cert_stage_engine import decisions_digest\n"
            "print(json.dumps({'hit_before': r1 is r1b, 'recomputed': r2 is not r1, 'n': [r1.dec_n, r2.dec_n],"
            "'sha_ok': r2.dec_sha256 == decisions_digest(rt.decisions)[1]}))\n")
    r = subprocess.run([sys.executable, "-I", "-B", "-c", code, HERE, os.path.dirname(r1d), "CERT_S3_R01", rss[1]],
                       capture_output=True, text=True, encoding="utf-8", env=_sub_env(), timeout=300)
    j = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    check("RC-02", "分析快取命中後新增決策 → 一定重算；dec_n＋1、雜湊對應新清單（cert_runtime，獨立子行程）",
          j.get("hit_before") and j.get("recomputed") and j.get("n", [0, 0])[1] == j.get("n", [0, 0])[0] + 1 and j.get("sha_ok"),
          str(j) + r.stderr[-300:])

    # ------------------------------------------------------------------ LG 舊版時間路徑（案例 7～9）＋ GU-02
    print("-- LG --")
    _t3s_legacy(lab1)
    _t3s_fmt(lab1)

    # ------------------------------------------------------------------ MR-09 唯一識別
    print("-- MR-09 --")
    u1, su1, eu1, _ = s3_session("CERT_S3_U01", 470, base_decs=lambda ss, e: lab1(ss, e) + [
        _cnt(ss[1], False, _ts(e - timedelta(minutes=30)), "C1")])
    u2, su2, eu2, _ = s3_session("CERT_S3_U02", 471, base_decs=lambda ss, e: lab1(ss, e) + [
        _cnt(ss[1], False, _ts(e - timedelta(minutes=30)), "C1")])
    rc, last, _t = run_tool([u1, u2], now=nxt_now())
    man = manifest_of(last["merge_dir"])
    unc = [s for s in man["stages"] if s["pairing"] == AN.UNC_EXCLUDED]
    keys = [(s["id"].split("#")[0], s["stage_start"]) for s in man["stages"]]
    check("MR-09a", "兩個 Session 都有「中止充電（不計次）」→ 整合後各自以 Session ID＋stage_start 唯一對回，不合併",
          len(unc) == 2 and len(set(keys)) == len(keys) and len({s["id"] for s in man["stages"]}) == len(man["stages"]))
    from cert_report_xlsx import _sheet_name
    from cert_stage_engine import stage_names, strip_count_time
    class _S:                                                            # noqa: E306
        def __init__(self, no, d, t, counted=True, label=""):
            self.no, self.dir, self.counted, self.label = no, d, counted, label
            self.dir_name = {"C": "充電", "D": "放電"}[d]
            self.start = type("O", (), {"t": t})()
    t0 = datetime(2026, 12, 9, 14, 35, 36)
    sts = [_S(1, "D", t0 - timedelta(hours=3)), _S(2, "C", t0, False, "中止"), _S(3, "C", t0 + timedelta(days=1), False, "中止"),
           _S(4, "C", t0 + timedelta(days=1, hours=2))]
    nm = stage_names(sts)
    shn = [_sheet_name(i, nm, s) for i, s in enumerate(sts, 1)]
    check("MR-09b", "同一 Session 跨日同時分秒的兩個不計次階段 → 名稱用完整日期時間區分；頁籤 31 字截斷後仍唯一（序號）、可對回階段",
          nm[2] != nm[3] and "2026-12-09" in nm[2] and len(set(shn)) == 4 and all(len(x) <= 31 for x in shn)
          and [int(x.split(".")[0]) for x in shn] == [1, 2, 3, 4] and strip_count_time(nm[2]) == strip_count_time(nm[3]), str((nm, shn)))


def _t3s_pairing():
    W = CFG.params()["W_PAUSE_SEC"]

    def unc_session(spec, unc_no, *, w=None, pauses=(), pending=None, end_by=None, sc=None, ec=None):
        sa = fake_session(spec)
        for s in sa["stages"]:
            st = s["stage"]
            st.counted = not (st.no == unc_no)
        if end_by:
            for no, v in end_by.items():
                sa["stages"][no - 1]["stage"].end_by = v
        for no, v in (sc or {}).items():
            sa["stages"][no - 1]["stage"].start_certain = v
        for no, v in (ec or {}).items():
            sa["stages"][no - 1]["stage"].end_certain = v
        sa["params"] = dict(CFG.params(), W_PAUSE_SEC=w or W)
        sa["res"].pauses = [type("P", (), {"item_id": f"U-{i}", "certain": False, "decision": None,
                                           "A": type("O", (), {"t": a})(), "R": type("O", (), {"t": b})()})() for i, (a, b) in enumerate(pauses)]
        for no, n in (pending or {}).items():
            sa["stages"][no - 1]["pending_pauses"] = n
        AN.pair_session(sa, "DC")
        return sa
    spec5 = [("D", "滿放", {}), ("C", "滿充", {}), ("C", "中止", {}), ("D", "滿放", {}), ("C", "滿充", {})]
    sa = unc_session(spec5, 3)
    check("PA-01", "放、充、充（中止＋不計次）、放、充 → 2 個循環、不因不計次階段觸發 A1／A2、有加註、不計次階段保留",
          len(sa["cycles"]) == 2 and all(c["certainty"] == AN.CERTAIN for c in sa["cycles"])
          and sa["stages"][2]["pairing"] == AN.UNC_EXCLUDED and any(c["uncounted_notes"] for c in sa["cycles"])
          and len(sa["stages"]) == 5, str(_cy(sa)))
    sa = unc_session([("D", "滿放", {}), ("C", "滿充", {}), ("C", "中止", {}), ("C", "滿充", {})], 3)
    check("PA-02", "保護①：充1、充（不計次）、充2 → 排除後充1、充2 相鄰 → A2 仍觸發",
          sa["cycles"] and sa["cycles"][0]["certainty"] == AN.PAIR_PENDING
          and any(t.startswith("A2：") for t in sa["cycles"][0]["certainty_triggers"]), str(_cy(sa)))
    sa_n = fake_session([("D", "滿放", {}), ("C", "中止", {}), ("C", "滿充", {})])
    check("PA-04", "只有標記中止、沒有有效不計次 → 照現行規則（D 與中止階段配對、第二個充電未配對）",
          sa_n["cycles"] and sa_n["cycles"][0]["b"]["no"] == 2 and sa_n["stages"][2]["pairing"] != AN.UNC_EXCLUDED)
    # 保護②：交界窗口 [A−W, B+W]；階段 2（充）→ 階段 3（充，不計次）；FS：A＝開始＋100s，下一階段開始＋300s
    T = T0 + timedelta(seconds=300)                       # 階段 2 開始
    A = T + timedelta(seconds=100)
    B = T + timedelta(seconds=300)
    spec = [("D", "滿放", {}), ("C", "滿充", {}), ("C", "中止", {}), ("D", "滿放", {}), ("C", "滿充", {})]

    def trig(sa_):
        return any("保護②" in t for c in sa_["cycles"] for t in c["certainty_triggers"])
    s = lambda sec: A + timedelta(seconds=sec)              # noqa: E731
    cases = [("PA-B01", "未決停頓完全位於 A～B 之間 → 觸發", [(A + timedelta(seconds=50), A + timedelta(seconds=60))], True, None),
             ("PA-B02", "未決停頓位於 A 前 W 秒內 → 觸發", [(s(-50), s(-40))], True, None),
             ("PA-B03", "未決停頓位於 B 後 W 秒內 → 觸發", [(B + timedelta(seconds=20), B + timedelta(seconds=30))], True, None),
             ("PA-B04", "未決停頓完全位於窗口外 → 不觸發", [(s(-500), s(-490))], False, None),
             ("PA-B05a", "未決停頓剛好接觸窗口起點 → 觸發", [(s(-W - 10), s(-W))], True, None),
             ("PA-B05b", "未決停頓剛好接觸窗口終點 → 觸發", [(B + timedelta(seconds=W), B + timedelta(seconds=W + 10))], True, None),
             ("PA-B06", "停頓起點在窗口外、區間與窗口重疊 → 觸發", [(s(-W - 30), s(-W + 5))], True, None),
             ("PA-B07a", "距窗口起點 1 秒且不重疊 → 不觸發", [(s(-W - 10), s(-W - 1))], False, None),
             ("PA-B07b", "距窗口終點 1 秒且不重疊 → 不觸發", [(B + timedelta(seconds=W + 1), B + timedelta(seconds=W + 10))], False, None)]
    for tid, txt, pz, want, _x in cases:
        sa_ = unc_session(spec, 3, pauses=pz)
        check(tid, f"保護②：{txt}", trig(sa_) == want and len(sa_["cycles"]) == 2, str([c["certainty_triggers"] for c in sa_["cycles"]]))
    pz8 = [(s(-100), s(-90))]
    check("PA-B08", "判定範圍隨 Session 原始 W_PAUSE_SEC 變動（同一停頓：W＝60 不觸發、W＝120 觸發；不是固定 60 秒）",
          not trig(unc_session(spec, 3, pauses=pz8, w=60)) and trig(unc_session(spec, 3, pauses=pz8, w=120)))
    sa9 = unc_session(spec, 3, pauses=[(s(-500), s(-490))], pending={2: 1})
    c1 = sa9["cycles"][0]
    check("PA-B09", "窗口外未決停頓不觸發保護②，但 P-7 確認狀態仍為待確認（保護③；配對確定性與確認狀態分開）",
          c1["certainty"] == AN.CERTAIN and c1["status"] == AN.ST_PENDING and any("未決不確定停頓" in r for r in c1["reasons"]))
    sa10a = unc_session(spec, 3, end_by={2: "operator_split"})
    sa10b = unc_session(spec, 3, ec={2: False})
    check("PA-B10", "沒有未決停頓，但交界為 operator_split 或邊界不確定 → 保護②仍有效",
          trig(sa10a) and trig(sa10b), str([c["certainty_triggers"] for c in sa10a["cycles"] + sa10b["cycles"]]))
    sa12 = unc_session([("D", "滿放", {}), ("C", "中止", {}), ("D", "滿放", {}), ("C", "滿充", {})], 2,
                       pauses=[(T0 + timedelta(seconds=150), T0 + timedelta(seconds=160))])
    check("PA-B12", "相鄰階段為反向 → 不套用同向的保護②（無保護②觸發、無相鄰不計次加註）",
          not trig(sa12) and not any(c["uncounted_notes"] for c in sa12["cycles"]))

    def r7_pair(spec_, order="DC"):
        """r7 原始配對演算法（對照用）。"""
        st = [d for d, _l, _o in spec_]
        out, i = [], 0
        while i < len(st):
            if st[i] == order[0] and i + 1 < len(st) and st[i + 1] == order[1]:
                a1 = i > 0 and st[i - 1] == st[i]
                a2 = i + 2 < len(st) and st[i + 2] == st[i + 1]
                out.append((i + 1, i + 2, a1 or a2))
                i += 2
            else:
                i += 1
        return out
    specs = [[("D", "", {}), ("C", "", {})], [("D", "", {}), ("D", "", {}), ("C", "", {}), ("C", "", {})],
             [("C", "", {}), ("D", "", {}), ("C", "", {}), ("D", "", {}), ("C", "", {})], [("D", "", {}), ("C", "", {}), ("C", "", {})]]
    same = all([(c["a"]["no"], c["b"]["no"], c["certainty"] == AN.PAIR_PENDING) for c in fake_session(sp)["cycles"]] == r7_pair(sp)
               for sp in specs)
    check("PA-B13", "沒有 stage_count 的 Session：配對與確定性和 r7 演算法完全相同（另由既有 T-K 測試覆蓋）", same)


def _t3s_083020():
    """PA-B11（保護④）：083020 獨立副本（--work 內，原 fixture 只讀並先核對 B0 雜湊）＋一筆與 G6 合法流程同格式的不計次決策。"""
    import hashlib
    from shutil import copytree
    cid, key = "CERT_20261120_083020", "2026-11-20T14:35:36"
    src = os.path.join(os.path.dirname(HERE), "output", "cert_reports", cid)
    man_p = r"D:\Crawler WT\b0_baseline_20261124\B0_fixture_manifest.json"
    if not (os.path.isdir(src) and os.path.exists(man_p)):
        check("PA-B11", "083020 獨立副本：fixture 或 B0 manifest 不存在", False, src)
        return
    want = json.load(open(man_p, encoding="utf-8"))["manifest"][cid]["file_sha256"]
    root = os.path.join(WORK, "s3_083020")
    d = os.path.join(root, cid)
    copytree(src, d)
    got = {os.path.relpath(os.path.join(dp, f), d).replace(os.sep, "/"): hashlib.sha256(_real_open(os.path.join(dp, f), "rb").read()).hexdigest()
           for dp, _dn, fn in os.walk(d) for f in fn}
    check("PA-B11-0", "083020 副本逐檔 SHA-256 與 B0 fixture manifest 相同（原 fixture 不修改）", got == want)
    if got != want:
        return
    _append(os.path.join(d, "decisions.jsonl"), [{"type": "stage_count", "stage_start": key, "counted": False, "reason": S3_083020_REASON,
                                                  "note": "", "at": "2026-11-24 15:35:53", "by": "dev-3S", "request_id": "REQ_S3_083020"}])
    sa = AN.analyze_session(SRC.load_session(d, root), 1)
    AN.pair_session(sa, "DC")
    st = sa["stages"]
    cy = sa["cycles"]
    unc = [s for s in st if s["pairing"] == AN.UNC_EXCLUDED]
    paired = [s for s in st if s["pairing"] != AN.UNC_EXCLUDED and any(s["no"] in (c["a"]["no"], c["b"]["no"]) for c in cy)]
    det = {"stages": [(s["no"], s["stage"].dir, s["stage"].label, s["pairing"], s.get("pending_pauses")) for s in st],
           "cycles": [(c["a"]["no"], c["b"]["no"], c["certainty"], c["status"], c["certainty_triggers"]) for c in cy]}
    check("PA-B11", "保護④ 083020 獨立副本：5 階段、1 不計次（第 3 階段）、4 階段參與配對、2 個循環",
          len(st) == 5 and [s["no"] for s in unc] == [3] and len(paired) == 4 and len(cy) == 2
          and [(c["a"]["no"], c["b"]["no"]) for c in cy] == [(1, 2), (4, 5)], str(det))
    check("PA-B11b", "保護④：保護②不觸發；相鄰階段（第 2 階段）7 個未決不確定停頓保留（不因不計次刪除）；全部 20 個保留",
          not any("保護②" in t for c in cy for t in c["certainty_triggers"]) and st[1].get("pending_pauses") == 7
          and sum(s.get("pending_pauses") or 0 for s in st) == 20, str(det))
    check("PA-B11c", "保護④：循環 1 確認狀態仍為「待確認」（不因不計次或名稱判為完成）；不計次階段原始資料保留",
          cy[0]["status"] == AN.ST_PENDING and unc and unc[0]["stage"].start.t.strftime("%Y-%m-%dT%H:%M:%S") == key
          and any(cid + "#ST3" in n for n in cy[0]["uncounted_notes"]), str(det))
    _t3s_fmt_083020(d)


def _fmt_expected(md, s_no, cid):
    """測試端獨立推導：由 R_base／R_all 實際活頁簿找 H2 標題格（14pt 粗體＋F4B183），以連續底色欄數求兩邊差集。"""
    import glob
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    pb = glob.glob(os.path.join(md, "_verify", f"R_base_S{s_no}_{cid}.xlsx"))
    pa = [p for p in glob.glob(os.path.join(md, "重產明細", f"重產明細_S{s_no}_{cid}*.xlsx")) if p.endswith(".xlsx")]
    if not pb or not pa:
        return None, None
    wb_b, wb_a = load_workbook(pb[0]), load_workbook(pa[0])
    out, info = set(), {}

    def filled(c):
        return c is not None and c.fill is not None and c.fill.fill_type == "solid" and str(c.fill.fgColor.rgb).upper().endswith("F4B183")

    for sh in ("數據概覽", "即時熱度"):
        B, A = wb_b[sh], wb_a[sh]
        heads = sorted(k for k, c in B._cells.items() if isinstance(c.value, str) and c.font.b and c.font.sz == 14 and filled(c))
        for (r, c0) in heads:
            def span(ws):
                n = 0
                while filled(ws._cells.get((r, c0 + n))):
                    n += 1
                return n
            nb_, na_ = span(B), span(A)
            for j in range(min(nb_, na_), max(nb_, na_)):
                out.add((sh, f"{get_column_letter(c0 + j)}{r}"))
        info[sh] = {"heads": len(heads), "dims_base": (B.max_row, B.max_column), "dims_all": (A.max_row, A.max_column)}
    return out, info


def _fmt_cells(md, cid):
    at = _item(_dv(md, cid), "D-A")
    return at, {(c["sheet"], c["cell"]) for c in ((at or {}).get("detail", {}).get("format_cells") or [])}


def _fmt_put(sh, coord, **kw):
    def f(wb):
        c = wb[sh][coord]
        for a, v in kw.items():
            setattr(c, a, v)
    return f


def _t3s_fmt(lab1):
    """FMT-01～FMT-20（STOP3／STOP4 裁定 A2）：H2 標題底色欄數規則推導的合法空白底色格（只限第二階段 R_base vs R_all）。"""
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill
    from cert_stage_engine import stage_names
    print("-- FMT（標題底色差異歸因）--")

    def unc(d, ss, e, rid="F1"):
        _append(os.path.join(d, "decisions.jsonl"), [_cnt(ss[1], False, _ts(e + timedelta(days=1)), rid)])
    S = {}
    for k, day in (("F01", 600), ("F03", 601), ("F04", 602), ("F05", 603), ("F06", 604), ("F07", 605)):    # F05：兩個非標題列格
        d, ss, e, _rp = s3_session(f"CERT_S3_{k}", day, base_decs=lab1)
        unc(d, ss, e)
        S[k] = d
    fill = PatternFill("solid", fgColor="F4B183")
    put = _fmt_put
    hk = _hooks(_inject("CERT_S3_F03", put("數據概覽", "E1", value=1.0)),                 # FMT-03 數值
                _inject("CERT_S3_F04", put("即時熱度", "E1", value="=1+1")),              # FMT-04 公式
                _inject("CERT_S3_F05", put("即時熱度", "K2", fill=fill)),                 # FMT-05 非標題列（區塊間、非合併格）
                _inject("CERT_S3_F05", put("數據概覽", "T2", fill=fill)),                 # FMT-05 非標題列（合併佔位之外）
                _inject("CERT_S3_F06", put("數據概覽", "F1", fill=fill)),                 # FMT-06 超出推導範圍
                _inject("CERT_S3_F07", put("數據概覽", "E1", fill=PatternFill("solid", fgColor="FF0000"))))   # FMT-07 格式不符
    rc, last, _t = run_tool([S[k] for k in ("F01", "F03", "F04", "F05", "F06", "F07")], now=nxt_now(), hooks=hk)
    check("FMT-RUN1", "FMT 第一組合併執行完成", "merge_dir" in last, f"rc={rc} {last}")
    md = last["merge_dir"]
    exp, info = _fmt_expected(md, 1, "CERT_S3_F01")
    at, got = _fmt_cells(md, "CERT_S3_F01")
    s1 = _sess(md, "CERT_S3_F01")
    cells = (at or {}).get("detail", {}).get("format_cells") or []
    check("FMT-01", "合法 stage_count 使標題變長跨欄界（4→5 欄）→ 底色延伸判合法，整體 VERIFIED、決策快照 CONFIRMED",
          s1["data_value_verification"] == "VERIFIED" and s1["decision_snapshot"] == "CONFIRMED"
          and all(r["tcols_base"] == 4 and r["tcols_all"] == 5 for r in at["detail"]["format_rule"]), str(at and at["detail"].get("problems")))
    check("FMT-16", "兩張工作表同時合法延伸：產品逐格紀錄＝測試端由實際活頁簿獨立推導的集合（完全相等，不是子集合）",
          exp is not None and got == exp and {s for s, _c in got} == {"數據概覽", "即時熱度"}, f"got={sorted(got)} exp={sorted(exp or [])}")
    check("FMT-13", "即時熱度合法延伸但整張 used range 不變 → 仍逐格辨識並記錄",
          info["即時熱度"]["dims_base"] == info["即時熱度"]["dims_all"] and any(s == "即時熱度" for s, _c in got), str(info))
    check("FMT-08", "文字未改名的標題列也依 H2 共用欄數同步延伸 → 合法並記錄",
          any(c["title_base"] == c["title_all"] for c in cells) and any(c["title_base"] != c["title_all"] for c in cells),
          str([(c["cell"], c["title_base"] == c["title_all"]) for c in cells]))
    check("FMT-01b", "逐格紀錄欄位完整（Session、工作表、格位、增減、前後狀態、標題格、寬度 px、欄數、決策行、理由）",
          bool(cells) and all(all(k in c for k in ("session", "sheet", "cell", "kind", "before", "after", "title_cell", "px_base",
                                                    "px_all", "tcols_base", "tcols_all", "decision_lines", "reason")) for c in cells)
          and all(c["decision_lines"] for c in cells))
    for tid, k, txt in (("FMT-03", "F03", "新增底色格含數值"), ("FMT-04", "F04", "新增底色格含公式（即時熱度）"),
                        ("FMT-05", "F05", "格式格不在標題列"), ("FMT-06", "F06", "標題列上但超出 H2 推導範圍"),
                        ("FMT-07", "F07", "位置正確但格式不是 H2 標題底色")):
        sx = _sess(md, f"CERT_S3_{k}")
        ax = _item(_dv(md, f"CERT_S3_{k}"), "D-A")
        check(tid, f"{txt} → FAILED（DECISION_DIFF_OUT_OF_SCOPE）",
              sx["data_value_verification"] == "FAILED" and "DECISION_DIFF_OUT_OF_SCOPE" in (sx["reason_codes"] or []),
              str(ax and ax["detail"].get("problems")))

    # 第二組：即時熱度反向、兩頁一合法一非法、缺一格、無寬度變化、第一階段
    G = {}
    for k, day in (("G15", 610), ("G17", 611), ("G19", 612), ("G09", 613)):
        d, ss, e, _rp = s3_session(f"CERT_S3_{k}", day, base_decs=lab1)
        if k == "G09":                                   # 新增決策不改變標題寬度（第 3 階段標記滿充：200 px，欄數不變）
            _append(os.path.join(d, "decisions.jsonl"), [_lab(ss[2], "滿充", _ts(e + timedelta(days=1)), "L9")])
        else:
            unc(d, ss, e)
        G[k] = d
    d10, ss10, e10, rp10 = s3_session("CERT_S3_G10", 614, base_decs=lab1)
    wb10 = load_workbook(rp10)
    wb10["數據概覽"]["E1"] = "x"                          # FMT-10：原始報告在底色範圍內被寫入內容
    wb10.save(rp10)
    unc(d10, ss10, e10)
    G["G10"] = d10

    def unfill(sh, coord):
        def f(wb):
            wb[sh][coord].fill = PatternFill(fill_type=None)
        return f
    hk2 = _hooks(_inject("CERT_S3_G15", put("即時熱度", "I1", fill=fill)),                  # FMT-15 區塊之間（非預期區塊）
                 _inject("CERT_S3_G17", put("即時熱度", "F1", fill=fill)),                  # FMT-17 數據概覽合法＋即時熱度非法
                 _inject("CERT_S3_G19", unfill("即時熱度", "Q1")),                          # FMT-19 應延伸卻缺一格
                 _inject("CERT_S3_G09", put("數據概覽", "E1", fill=fill)))                 # FMT-09 無寬度變化卻有同樣格式差異
    rc, last, _t = run_tool([G[k] for k in ("G15", "G17", "G19", "G09", "G10")], now=nxt_now(), hooks=hk2)
    check("FMT-RUN2", "FMT 第二組合併執行完成", "merge_dir" in last, f"rc={rc} {last}")
    md2 = last["merge_dir"]
    for tid, k, txt in (("FMT-15", "G15", "即時熱度底色出現在非預期區塊"),
                        ("FMT-19", "G19", "預期應延伸但實際缺一格（不完整的格式變化）"),
                        ("FMT-09", "G09", "沒有改變標題寬度的合法決策，卻出現相同底色差異（本規則不豁免）")):
        sx = _sess(md2, f"CERT_S3_{k}")
        ax = _item(_dv(md2, f"CERT_S3_{k}"), "D-A")
        check(tid, f"{txt} → FAILED", sx["data_value_verification"] == "FAILED"
              and "DECISION_DIFF_OUT_OF_SCOPE" in (sx["reason_codes"] or []), str(ax and ax["detail"].get("problems")))
    a17, _g17 = _fmt_cells(md2, "CERT_S3_G17")
    s17 = _sess(md2, "CERT_S3_G17")
    check("FMT-17", "數據概覽合法延伸（逐格記錄）、即時熱度同時有非法底色 → 整體 FAILED；問題只指向即時熱度",
          s17["data_value_verification"] == "FAILED" and a17["detail"]["problems"]
          and all(p.startswith("即時熱度") for p in a17["detail"]["problems"])
          and any(s_ == "數據概覽" for s_, _c in _g17), str(a17["detail"]["problems"]) + str(sorted(_g17)))
    s10 = _sess(md2, "CERT_S3_G10")
    v10 = _dv(md2, "CERT_S3_G10")
    check("FMT-10", "原始報告 vs R_base 出現差異（底色範圍內寫入內容）→ 第一階段照原規則 FAILED（UNEXPLAINED_VALUE_DIFF），不套用底色規則",
          s10["data_value_verification"] == "FAILED" and "UNEXPLAINED_VALUE_DIFF" in (s10["reason_codes"] or [])
          and s10["decision_snapshot"] == "CONFIRMED"
          and not any("format_cells" in (i.get("detail") or {}) for i in v10["items"] if not i["item"].startswith("D-A")), str(s10))

    # 第三組：縮減（FMT-02／14）、不同階段數（FMT-18）、同類名稱多候選（FMT-20）
    d2_, ss2_, e2_, _ = s3_session("CERT_S3_G02", 615, base_decs=lambda ss, e: lab1(ss, e) + [_cnt(ss[1], False, _ts(e - timedelta(minutes=30)), "C1")])
    _append(os.path.join(d2_, "decisions.jsonl"), [_cnt(ss2_[1], True, _ts(e2_ + timedelta(days=1)), "C2", reason="恢復")])
    p18 = [("I", 30), ("D", 200), ("I", 90), ("C", 200), ("I", 90), ("C", 40), ("I", 90), ("D", 200), ("I", 90), ("C", 200), ("I", 90),
           ("D", 200), ("I", 90)]                            # 6 階段（D、C、C（中止）、D、C、D）
    d18 = make_session("CERT_S3_G18", T0 + timedelta(days=616), p18, report=False)
    ss18 = _starts(d18)
    e18 = datetime.strptime(json.load(open(os.path.join(d18, "cert_state.json"), encoding="utf-8"))["ended_at"], "%Y-%m-%dT%H:%M:%S")
    _jl(os.path.join(d18, "decisions.jsonl"), [_lab(ss18[2], "中止", _ts(e18 - timedelta(hours=1)), "L1")])
    make_original(d18)
    _append(os.path.join(d18, "decisions.jsonl"), [_cnt(ss18[2], False, _ts(e18 + timedelta(days=1)), "F1")])
    p20 = [("I", 30), ("D", 200), ("I", 90), ("C", 40), ("I", 90), ("C", 40), ("I", 90), ("C", 200), ("I", 90)]
    G20 = {}
    for k, day in (("G20", 617), ("G20N", 618)):
        d = make_session(f"CERT_S3_{k}", T0 + timedelta(days=day), p20, report=False)
        ss = _starts(d)
        e = datetime.strptime(json.load(open(os.path.join(d, "cert_state.json"), encoding="utf-8"))["ended_at"], "%Y-%m-%dT%H:%M:%S")
        _jl(os.path.join(d, "decisions.jsonl"), [_lab(ss[1], "中止", _ts(e - timedelta(hours=2)), "L1"), _lab(ss[2], "中止", _ts(e - timedelta(hours=2)), "L2"),
                                                 _cnt(ss[1], False, _ts(e - timedelta(hours=1)), "C1"), _cnt(ss[2], False, _ts(e - timedelta(hours=1)), "C2")])
        make_original(d)
        _append(os.path.join(d, "decisions.jsonl"), [_cnt(ss[1], True, _ts(e + timedelta(days=1)), "C3", reason="恢復")])
        G20[k] = d
    sa20 = AN.analyze_session(SRC.load_session(G20["G20N"], os.path.dirname(G20["G20N"])), 1)
    st20 = sa20["res"].stages
    nm20 = stage_names(st20)
    t_a = f"{nm20[st20[1].no]}（中止）結束"                   # 恢復後階段 2：第1次充電（中止）
    t_b = f"{nm20[st20[2].no]}結束"                          # 階段 3：中止充電（不計次）
    swapped = []

    def swap(wb):                                            # 把階段 2 的新標題換成階段 3 的新標題（其他階段的名稱）
        for c in wb["數據概覽"]._cells.values():
            if c.value == t_a:
                c.value = t_b
                swapped.append(c.coordinate)
                break
    hk3 = _hooks(_inject("CERT_S3_G20N", swap))
    rc, last, _t = run_tool([d2_, d18, G20["G20"], G20["G20N"]], now=nxt_now(), hooks=hk3)
    check("FMT-RUN3", "FMT 第三組合併執行完成", "merge_dir" in last, f"rc={rc} {last}")
    md3 = last["merge_dir"]
    a2x, g2x = _fmt_cells(md3, "CERT_S3_G02")
    e2x, _i = _fmt_expected(md3, 1, "CERT_S3_G02")
    fc2 = a2x["detail"].get("format_cells") or []
    check("FMT-02", "恢復計次後標題變短 → 兩頁底色縮減（5→4）判合法、整體 VERIFIED，集合與獨立推導完全相等",
          _sess(md3, "CERT_S3_G02")["data_value_verification"] == "VERIFIED" and {c["kind"] for c in fc2} == {"減少"}
          and bool(g2x) and g2x == e2x, str(sorted(g2x)))
    check("FMT-14", "即時熱度合法縮減 → 正確辨識（逐格紀錄為減少）",
          any(c["sheet"] == "即時熱度" for c in fc2) and all(c["kind"] == "減少" for c in fc2 if c["sheet"] == "即時熱度"))
    _a18, g18 = _fmt_cells(md3, "CERT_S3_G18")
    e18x, i18 = _fmt_expected(md3, 2, "CERT_S3_G18")
    check("FMT-18", "不同階段數的 Session：合法格數依 H2 規則推導（每頁＝標題區塊數），不是固定 5 或 10 格",
          _sess(md3, "CERT_S3_G18")["data_value_verification"] == "VERIFIED" and g18 == e18x
          and len(g18) == i18["數據概覽"]["heads"] + i18["即時熱度"]["heads"] and len(g18) not in (5, 6, 10), f"{len(g18)} {i18}")
    s20, a20 = _sess(md3, "CERT_S3_G20"), _item(_dv(md3, "CERT_S3_G20"), "D-A")
    check("FMT-20a", "兩個不計次充電（名稱附時間區分）恢復其中一個 → 名稱重新編號，多候選對照依同一 Stage.no 推導 → VERIFIED（不誤拒合法同類名稱）",
          s20["data_value_verification"] == "VERIFIED" and not any("來源不唯一" in p for p in a20["detail"]["problems"]),
          str(a20["detail"].get("problems")) + str(a20["detail"].get("rename_map")))
    s20n, a20n = _sess(md3, "CERT_S3_G20N"), _item(_dv(md3, "CERT_S3_G20N"), "D-A")
    check("FMT-20b", "把某階段標題換成另一階段的新名稱（替身）→ 多候選對照不接受其他階段名稱 → FAILED",
          bool(swapped) and s20n["data_value_verification"] == "FAILED" and "DECISION_DIFF_OUT_OF_SCOPE" in (s20n["reason_codes"] or []),
          f"{swapped} {a20n['detail'].get('problems')}")


def _t3s_fmt_083020(d):
    """FMT-12：PA-B11 的 083020 獨立副本（舊報告＋不計次決策）實際合併，依 H2 規則精確歸因。"""
    other = make_session("CERT_S3_G12", T0 + timedelta(days=620), STD)
    rc, last, _t = run_tool([d, other], now=nxt_now())
    check("FMT-RUN4", "083020 副本合併執行完成", "merge_dir" in last, f"rc={rc} {last}")
    md = last["merge_dir"]
    cid = os.path.basename(d)
    s = _sess(md, cid)
    _at, got = _fmt_cells(md, cid)
    exp, _info = _fmt_expected(md, 1, cid)
    per = {sh: sorted(c for s_, c in got if s_ == sh) for sh in ("數據概覽", "即時熱度")}
    check("FMT-12", "083020 副本：原始 vs R_base VERIFIED；合法底色格（數據概覽 5、即時熱度 5）與獨立推導完全相等；整體 VERIFIED、PRESUMED／推定",
          s["data_value_verification"] == "VERIFIED" and s["decision_snapshot"] == "PRESUMED" and s["traceability"] == "推定（未確認）"
          and got == exp and len(per["數據概覽"]) == 5 and len(per["即時熱度"]) == 5, f"{s} {per} {sorted(exp or [])}")


def _t3s_legacy(lab1):
    """舊版（無 keywords）時間路徑：案例 7～9、窗口邊界、跨日、時區、request 不符、時鐘回調、行序、輔助檢查。"""
    def legacy(name, day, added, *, end_report=True, dec_mtime_shift=1, tamper=False, at_override=None, rep_mtime=None,
               base=lab1):
        d, ss, e, rp = s3_session(name, day, base_decs=base, old=True, end_report=end_report, tamper=tamper)
        recs = added(ss, e)
        if recs:
            _append(os.path.join(d, "decisions.jsonl"), recs)
        ats = [datetime.strptime(r["at"][:19], "%Y-%m-%d %H:%M:%S") for r in recs if r.get("at") and r["at"][:4].isdigit()]
        last_at = max(ats + [e - timedelta(hours=1)])
        _touch(os.path.join(d, "decisions.jsonl"), last_at + timedelta(seconds=dec_mtime_shift))
        if rep_mtime is not None:
            _touch(rp, rep_mtime(e))
        return d
    W = 120
    after = lambda sec: (lambda ss, e: [_cnt(ss[1], False, _ts(e + timedelta(seconds=sec)), "A1")])       # noqa: E731
    L = {
        "L01": legacy("CERT_S3_L01", 480, lambda ss, e: []),                                       # 無新增決策
        "L02": legacy("CERT_S3_L02", 481, after(W + 1)),                                          # 案例8：窗口外 1 秒（終點）
        "L03": legacy("CERT_S3_L03", 482, after(W)),                                              # 終點（含）→ 窗口內
        "L04": legacy("CERT_S3_L04", 483, after(86400 + 600)),                                     # 跨日
        "L05": legacy("CERT_S3_L05", 484, after(3600), tamper=True),                               # LG-03／LG-06
        "L06": legacy("CERT_S3_L06", 485, lambda ss, e: [_lab("2000-01-01T00:00:00", "滿放", _ts(e + timedelta(hours=1)), "A1")],
                      end_report=False, tamper=True),     # 案例7（r7 X7：基準後有新決策＋階段頁不同、無法重建）／案例9
    }
    rc, last, _t = run_tool(list(L.values()), now=nxt_now())
    md = last["merge_dir"]
    g = {k: _sess(md, f"CERT_S3_{k}") for k in L}
    check("LG-01", "舊版、報告後沒有新增決策 → 與 r7 相同嚴格比對 VERIFIED；決策快照 PRESUMED、可追溯性「推定（未確認）」",
          g["L01"]["data_value_verification"] == "VERIFIED" and g["L01"]["decision_snapshot"] == "PRESUMED"
          and g["L01"]["traceability"] == "推定（未確認）", str(g["L01"]))
    check("LG-02", "案例8：結束報告、時間證據完整、窗口外（終點＋1 秒）、嚴格重放相同 → VERIFIED（可追溯性仍為推定）",
          g["L02"]["data_value_verification"] == "VERIFIED" and g["L02"]["decision_snapshot"] == "PRESUMED"
          and g["L02"]["traceability"] == "推定（未確認）", str(g["L02"]))
    check("GU-02", "舊版 VERIFIED 時呈現仍標示可追溯性為推定（不宣稱原始決策集合已確認）",
          "推定" in _dv(md, "CERT_S3_L02")["display"])
    check("LG-04a", "決策剛好在窗口終點（產生時間＋120 秒，含端點）→ UNVERIFIABLE",
          g["L03"]["decision_snapshot"] == "UNVERIFIABLE" and g["L03"]["data_value_verification"] != "VERIFIED", str(g["L03"]))
    check("LG-04b", "跨日新增決策（窗口外）→ 時間路徑成立 → VERIFIED", g["L04"]["data_value_verification"] == "VERIFIED", str(g["L04"]))
    check("LG-03", "LG-03／LG-06：條件成立（超過 120 秒）但原始報告與重放不同 → UNVERIFIABLE（不判 VERIFIED、不判 FAILED）",
          g["L05"]["data_value_verification"] == "UNVERIFIABLE", str(g["L05"]))
    v6 = _dv(md, "CERT_S3_L06")
    check("LG-07", "案例9：無法確認報告對應的 request（不是結束認證報告；Menu 25 型）→ 決策快照 UNVERIFIABLE",
          g["L06"]["decision_snapshot"] == "UNVERIFIABLE" and "報告請求" in v6["decision_verification"]["reason"])
    check("LG-08", "案例7：無 keywords、符合 r7 X7 條件（基準時間後有新決策、無法重建）→ 維持 r7 X7 比對，整體 UNVERIFIABLE",
          g["L06"]["data_value_verification"] == "UNVERIFIABLE" and not any(i["result"] == "FAILED" for i in v6["items"]), str(g["L06"]))
    L2 = {
        "L07": legacy("CERT_S3_L07", 490, after(3600),                                             # 窗口起點（結束−120，含端點）
                      base=lambda ss, e: lab1(ss, e) + [_lab(ss[2], "滿充", _ts(e - timedelta(seconds=W)), "B2")]),
        "L07b": legacy("CERT_S3_L07b", 489, after(3600),                                           # 窗口起點前 1 秒（不重疊）
                       base=lambda ss, e: lab1(ss, e) + [_lab(ss[2], "滿充", _ts(e - timedelta(seconds=W + 1)), "B2")]),
        "L08": legacy("CERT_S3_L08", 491, lambda ss, e: [_cnt(ss[1], False, _ts(e + timedelta(hours=1)), "A1"),
                                                           _lab(ss[2], "滿充", _ts(e + timedelta(minutes=30)), "A2")]),   # 行序不一致
        "L09": legacy("CERT_S3_L09", 492, lambda ss, e: [_cnt(ss[1], False, _ts(e + timedelta(hours=1)) + "+08:00", "A1")]),  # 時區
        "L10": legacy("CERT_S3_L10", 493, after(3600), rep_mtime=lambda e: e + timedelta(seconds=W + 30)),   # 報告檔修改時間不符
        "L11": legacy("CERT_S3_L11", 494, after(3600), dec_mtime_shift=-7200),                    # decisions.jsonl 修改時間早於最後決策
        "L12": legacy("CERT_S3_L12", 495, lambda ss, e: [_lab(ss[2], "滿充", _ts(e - timedelta(hours=2)), "A9")]),  # 時鐘回調
    }
    MD, keys = {}, list(L2)
    for part in (keys[:4], keys[4:]):                       # 工具一次最多 6 個來源（V1）→ 分兩批
        rc, last, _t = run_tool([L2[k] for k in part], now=nxt_now())
        check("LG-RUN2", f"第二組舊報告 Session 合併執行完成（{'、'.join(part)}）", "merge_dir" in last, f"rc={rc} {last}")
        for k in part:
            MD[k] = last["merge_dir"]
    g = {k: _sess(MD[k], f"CERT_S3_{k}") for k in L2}
    check("LG-04c", "決策剛好在窗口起點（認證結束−120 秒，含端點）→ 決策快照 UNVERIFIABLE（模糊窗口）",
          g["L07"]["decision_snapshot"] == "UNVERIFIABLE" and "模糊窗口" in _dv(MD["L07"], "CERT_S3_L07")["decision_verification"]["reason"],
          str(g["L07"]))
    check("LG-04h", "決策在窗口起點前 1 秒（不重疊）→ 時間路徑成立 → VERIFIED",
          g["L07b"]["data_value_verification"] == "VERIFIED" and g["L07b"]["decision_snapshot"] == "PRESUMED", str(g["L07b"]))
    check("LG-04d", "決策時間與檔案行序不一致 → 決策快照 UNVERIFIABLE（MR-07）",
          g["L08"]["decision_snapshot"] == "UNVERIFIABLE" and "行序" in _dv(MD["L08"], "CERT_S3_L08")["decision_verification"]["reason"])
    check("LG-04e", "時區無法確認（時間帶 +08:00）→ UNVERIFIABLE",
          g["L09"]["decision_snapshot"] == "UNVERIFIABLE" and "時區" in _dv(MD["L09"], "CERT_S3_L09")["decision_verification"]["reason"])
    check("LG-04f", "輔助檢查：報告檔修改時間與產生時間相差超過 120 秒 → UNVERIFIABLE（修改時間只作輔助，不單獨推定）",
          g["L10"]["decision_snapshot"] == "UNVERIFIABLE" and "報告檔修改時間" in _dv(MD["L10"], "CERT_S3_L10")["decision_verification"]["reason"])
    check("LG-04g", "輔助檢查：decisions.jsonl 修改時間早於最後一筆決策 → UNVERIFIABLE",
          g["L11"]["decision_snapshot"] == "UNVERIFIABLE" and "decisions.jsonl 修改時間" in _dv(MD["L11"], "CERT_S3_L11")["decision_verification"]["reason"])
    check("LG-05", "MR-12 時鐘回調：報告後新增的決策 at 早於產生時間 → 不判 VERIFIED（FAILED 或 UNVERIFIABLE）",
          g["L12"]["data_value_verification"] in ("FAILED", "UNVERIFIABLE"), str(g["L12"]))
    d13, ss13, e13, rp13 = s3_session("CERT_S3_L13", 496, base_decs=lab1, old=True)          # 產生時間缺失
    st13 = json.load(open(os.path.join(d13, "cert_state.json"), encoding="utf-8"))
    st13["reports"] = []
    _w(os.path.join(d13, "cert_state.json"), json.dumps(st13, ensure_ascii=False, indent=2))
    d14, ss14, e14, rp14 = s3_session("CERT_S3_L14", 497, base_decs=lab1, old=True)          # request 與報告版本不符（_02 重產）
    from shutil import copy2
    rp14b = rp14.replace("_01.xlsx", "_02.xlsx")
    copy2(rp14, rp14b)
    st14 = json.load(open(os.path.join(d14, "cert_state.json"), encoding="utf-8"))
    st14["reports"].append({"file": f"reports\\{os.path.basename(rp14b)}", "at": _ts(e14 + timedelta(days=2))})
    _w(os.path.join(d14, "cert_state.json"), json.dumps(st14, ensure_ascii=False, indent=2))
    _touch(rp14b, e14 + timedelta(days=2))
    _append(os.path.join(d14, "decisions.jsonl"), [_cnt(ss14[1], False, _ts(e14 + timedelta(days=3)), "A1")])
    _touch(os.path.join(d14, "decisions.jsonl"), e14 + timedelta(days=3, seconds=1))
    rc, last, _t = run_tool([d13, d14], now=nxt_now())
    md = last["merge_dir"]
    s13 = _sess(md, "CERT_S3_L13")
    check("LG-09", "原始報告產生時間缺失 → UNVERIFIABLE（沒有產報紀錄的報告不可確認）", s13["data_value_verification"] == "UNVERIFIABLE", str(s13))
    v14 = _dv(md, "CERT_S3_L14")
    check("LG-10", "request 與報告版本不符（基準為 Menu 25 重產 _02，結束事件對應 _01）→ 不使用其他報告時間 → UNVERIFIABLE",
          _sess(md, "CERT_S3_L14")["decision_snapshot"] == "UNVERIFIABLE" and v14["baseline"].endswith("_02.xlsx")
          and "報告請求" in v14["decision_verification"]["reason"], str(v14["decision_verification"]))


# =============================================================================
def main():
    global WORK, ACTIVE
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=("3A", "3B", "3C", "3R", "3N", "3S"))
    ap.add_argument("--work", required=True)
    ap.add_argument("--c1")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # -I 會忽略 PYTHONIOENCODING
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                                    # noqa: BLE001
        pass
    WORK = os.path.abspath(a.work)
    if os.path.exists(WORK):
        print(f"--work 已存在（不覆蓋）：{WORK}")
        return 2
    if SRC.is_inside(WORK, SRC.PROD_ROOT) or SRC.is_inside(WORK, os.path.dirname(HERE)):
        print("--work 不得位於正式版或開發 clone 之內")
        return 2
    c1 = os.path.abspath(a.c1) if a.c1 else None
    c1_before = SRC.list_tree(c1) if c1 else None
    os.makedirs(WORK)
    ACTIVE = os.path.join(WORK, "isolated_active.json")
    _w(ACTIVE, json.dumps({"cert_id": None, "updated_at": "2030-01-01 00:00:00"}))
    import tempfile
    test_tmp = os.path.join(WORK, "_tmp")
    os.makedirs(test_tmp)
    saved_tempdir = tempfile.tempdir                 # 保存原值；結束時（含例外、Abort）還原
    tempfile.tempdir = test_tmp                      # 測試端 openpyxl 暫存檔只建立在 --work\_tmp
    global TEST_TMP
    TEST_TMP = test_tmp
    builtins.open = _guarded_open
    net0 = len(SRC.network_attempts())

    def guarded(fn, *args):
        try:
            return fn(*args)
        except Abort:
            raise
        except Exception:                                                            # noqa: BLE001
            check("ERR", f"{fn.__name__} 例外", False, traceback.format_exc()[-1500:])
    try:
        if a.phase == "3A":
            guarded(t_static)
            guarded(t_network)
            guarded(t_guard)
            guarded(t_env_safety, c1)
            guarded(t_tempdir)
        elif a.phase == "3B":
            guarded(t_equivalence)
            guarded(t_tables_unit)
            guarded(t_vr_unit)
            guarded(t_space_unit)
            guarded(t_dirs_unit)
            for fn in (t_titles, t_pairing_units, t_engine_metrics):
                guarded(fn)
            ab = guarded(t_validate)
            if ab:
                guarded(t_basic, *ab)
            for fn in (t_unverifiable_and_failed, t_integrity, t_publish, t_manifest_single, t_paths, t_verify_cases,
                       t_multi_session):
                guarded(fn)
        elif a.phase == "3R":
            guarded(t_runner_class)
            guarded(t_phase33_root)
        elif a.phase == "3N":
            guarded(t_multi_session)
        elif a.phase == "3S":
            guarded(t_3s)
        else:
            guarded(t_c1, c1)
        n_net = len(SRC.network_attempts()) - net0 - (1 if a.phase == "3A" else 0)
        check("TN-1", "本階段非預期網路連線嘗試＝0", n_net == 0, str(SRC.network_attempts()))
        if n_net:
            raise Abort("非預期網路連線")
        if c1:
            changed = SRC.compare_trees(c1_before, SRC.list_tree(c1))
            check("SAFE", "C1 副本逐檔 SHA-256 不變", not changed, str(changed[:3]))
            if changed:
                raise Abort("C1 副本雜湊改變")
    except Abort as e:
        print(f"\n*** 共同停止條件觸發：{e} —— 停止整個測試 ***")
        builtins.open = _real_open
        return 3
    finally:
        builtins.open = _real_open
        tempfile.tempdir = saved_tempdir
    cnt = {k: sum(1 for r in RESULTS if r[2] == k) for k in ("PASS", "FAIL", "SKIP")}
    print(f"\n== test_cert_merge_report Phase {a.phase}：PASS {cnt['PASS']}／FAIL {cnt['FAIL']}／SKIP {cnt['SKIP']}（共 {len(RESULTS)}） ==")
    for r in RESULTS:
        if r[2] != "PASS":
            print(f"  {r[2]} {r[0]} {r[1]}")
    return 0 if not cnt["FAIL"] else 1


if __name__ == "__main__":
    sys.exit(main())
