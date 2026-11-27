# -*- coding: utf-8 -*-
"""
跨 Session 整合認證報告工具（第一版）—— 入口與流程（docs/Cert_Merge_Report_Design.md）
====================================================================================
用法：
  python -B -I cert_merge_report.py --source <Session 資料夾> --source <Session 資料夾> [...]
         [--version <cert_id>=<版本> ...] --cycle-order DC|CD --out-root D:\\Crawler_1hz_cert_merged

流程 F0～F13（§12.1）：網路封鎖 → 前置驗證 → MERGE 資料夾 → 快照 H1～H3 → 唯讀屬性 → 分析／配對／確認／I1～I8
  → 重產明細 → 資料值驗證 → 整合活頁簿 → 雜湊 → H5 → H4 → 發布協定或收尾 → 最終 STATUS。
原則：只讀來源；報告只來自已驗證快照；不跨 Session 配對；UNVERIFIABLE 不視為 VERIFIED；任一 FAILED 阻擋全部正式檔名；
      不提供強制放行或自動修復；不覆蓋既有 MERGE 輸出；不做任何網路連線或設備控制。
結束碼：0＝已正式發布（COMPLETED／COMPLETED_WITH_UNVERIFIED）；1＝未發布（任何失敗）；2＝前置驗證拒絕（未建立輸出）。
是否為正式發布，一律以 STATUS.json（publish.state＝PUBLISHED）＋manifest 雜湊＋各檔 SHA-256 判定（§11.4）。
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

# -I（isolated）隱含 -P：Python 不會把本檔所在目錄放進 sys.path，同層模組會 ModuleNotFoundError。
# 補回本檔目錄（與 test_cert_merge_report.py 相同作法）；-E、-s 等其他隔離效果完全不受影響。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cert_merge_source as SRC

# tempfile.tempdir 是程序層級的全域設定：同一程序內同時（或重入）執行兩個 main 會互相覆蓋暫存位置。
# 以非阻塞鎖拒絕同程序內的並行／重入呼叫（rc=2）；不同程序各自有獨立的 tempfile 設定，不互相影響。
_RUN_LOCK = threading.Lock()
TMP_DIR = "_tmp"           # openpyxl 等函式庫的暫存檔只建立在本次 MERGE 資料夾的 _tmp（受寫入守門管理）

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLISHABLE = ("COMPLETED", "COMPLETED_WITH_UNVERIFIED")
REUSED_MODULES = ("cert_stage_engine.py", "cert_report_xlsx.py", "report_quality.py", "report_display.py",
                  "cert_report_config.py", "cert_screenshot.py", "cert_supplement.py", "cert_heat_link.py")
REFERENCE_ONLY = ("cert_runtime.py", "cert_env.py")      # 邏輯在新模組逐字重建（N-3）；只記錄雜湊作為對照來源，不 import
OWN_MODULES = ("cert_merge_report.py", "cert_merge_source.py", "cert_merge_analysis.py", "cert_merge_xlsx.py",
               "cert_merge_verify.py")
INTEGRITY_STATEMENT = ("來源完整性只驗證到 H4 時點（{at}）；本工具不保證發布完成時原始 Session 仍未改變。"
                       "整合報告與重產明細的內容全部來自 H2 驗證、H5 再確認的快照。")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


# =============================================================================
# 檔名（§9.2）
# =============================================================================
def summary_base(infos):
    return f"整合認證報告_{infos[0]['cert_id']}_{infos[-1]['cert_id']}"


def detail_base(x):
    return os.path.join("重產明細", f"重產明細_S{x['s_no']}_{x['cert_id']}")


def output_names(infos):
    """所有可能的輸出相對路徑（供最長路徑檢查；含全部後綴與暫存名）。"""
    names = ["STATUS.json", "STATUS.json.tmp", "merge_manifest.json", "merge_manifest.json.partial",
             "publish_journal.jsonl", os.path.join(TMP_DIR, "openpyxl.XXXXXXXXXXXX.xml")]
    sb = summary_base(infos)
    names += [sb + ".partial.xlsx", sb + ".xlsx", sb + "_含未驗證明細.xlsx"]
    for x in infos:
        db = detail_base(x)
        names += [db + ".partial.xlsx", db + ".partial.xlsx.title_tmp", db + ".xlsx", db + "_資料值未驗證.xlsx"]
        rb = rbase_rel(x)                                     # r8：基準重放 R_base（只供驗證，不發布）
        names += [rb, rb + ".title_tmp"]
    return names


def rbase_rel(x):
    return os.path.join("_verify", f"R_base_S{x['s_no']}_{x['cert_id']}.xlsx")


# =============================================================================
# STATUS（§11.2）：寫暫存 → 單檔取代 → 讀回驗證
# =============================================================================
class Run:
    def __init__(self, merge_dir, hooks):
        self.dir = merge_dir
        self.guard = SRC.Guard(merge_dir)
        self.hooks = hooks
        import cert_merge_analysis as AN
        self.status = {"merge_id": os.path.basename(merge_dir), "stage": None, "overall": None, "reason": None,
                       "pairing_rule": AN.PAIRING_RULE,                 # 合併摘要配對規格（v2-align；不取代雜湊與驗證）
                       "snapshot": {}, "integrity": {}, "count_invariants": {}, "sessions": [],
                       "publish": {"state": "NOT_ATTEMPTED", "planned": [], "done": []},
                       "manifest_sha256": None, "updated_at": None}

    def p(self, *parts):
        return self.guard.check(os.path.join(self.dir, *parts))

    def set_stage(self, stage, **kw):
        self.status["stage"] = stage
        self.status.update(kw)
        if self.hooks.get("on_stage"):
            self.hooks["on_stage"](stage)
        self.write_status()

    def write_status(self):
        self.status["updated_at"] = _now()
        fail = self.hooks.get("fail_status_write")
        if fail and fail(self.status):
            raise OSError("（測試注入）STATUS 寫入失敗")
        tmp, dst = self.p("STATUS.json.tmp"), self.p("STATUS.json")
        data = _dump(self.status)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
        with open(dst, encoding="utf-8") as f:
            if json.load(f) != json.loads(data):
                raise OSError("STATUS 讀回內容不一致")


# =============================================================================
# 工具版本（§11.3）
# =============================================================================
def tool_info():
    import openpyxl
    info = {"modules": {}, "reference_only": {}, "python": platform.python_version(), "openpyxl": openpyxl.__version__}
    for m in OWN_MODULES + REUSED_MODULES:
        p = os.path.join(HERE, m)
        info["modules"][m] = SRC.sha256_file(p) if os.path.exists(p) else None
    for m in REFERENCE_ONLY:                               # 只計算雜湊作為 N-3 對照來源；不 import
        rp = os.path.join(HERE, m)
        info["reference_only"][m] = SRC.sha256_file(rp) if os.path.exists(rp) else None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    repo = os.path.dirname(HERE)
    try:
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, env=env,
                              timeout=30).stdout.strip()
        st = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True, env=env,
                            encoding="utf-8", timeout=60).stdout.splitlines()
        info["git"] = {"head": head, "status_count": len(st), "status": st}
    except Exception as e:                                                           # noqa: BLE001
        info["git"] = {"error": f"{type(e).__name__}: {e}"}
    return info


# =============================================================================
# 主流程
# =============================================================================
def parse_args(argv):
    ap = argparse.ArgumentParser(description="跨 Session 整合認證報告（第一版）")
    ap.add_argument("--source", action="append", default=[], required=True)
    ap.add_argument("--version", action="append", default=[])
    ap.add_argument("--cycle-order", default=None)
    ap.add_argument("--out-root", required=True)
    return ap.parse_args(argv)


def main(argv=None, *, active_json=SRC.PROD_ACTIVE_JSON, disk_free=None, now=None, hooks=None):
    SRC.block_network()                                                              # F0
    if not _RUN_LOCK.acquire(blocking=False):
        print(json.dumps({"overall": "REJECTED", "code": "CONCURRENT",
                          "reason": "同一程序內已有整合工具在執行（不支援並行或重入）"}, ensure_ascii=False))
        return 2
    try:
        return _main(argv, active_json=active_json, disk_free=disk_free, now=now, hooks=hooks)
    finally:
        _RUN_LOCK.release()


def _main(argv, *, active_json, disk_free, now, hooks):
    hooks = hooks or {}
    a = parse_args(argv)
    try:                                                                             # F1
        infos = SRC.validate(a.source, a.version, a.cycle_order, a.out_root, active_json=active_json)
        import cert_merge_analysis as AN
        for x in infos:                                                              # r8 V10：Session 原始參數（G6 規則）
            _sp, errs, _notes = AN.session_params_check(x["state"])
            if errs:
                raise SRC.Rejected("V10", f"{x['cert_id']} 的原始參數缺少或不合法：{'；'.join(errs)}"
                                          "（不以目前設定或預設值補齊）")
        space = SRC.check_space(a.out_root, infos, disk_free=disk_free)
        now = now or datetime.now()
        planned_dir = os.path.join(os.path.abspath(a.out_root), f"MERGE_{now:%Y%m%d_%H%M%S}")
        SRC.check_path_length(SRC.planned_paths(planned_dir, infos, output_names(infos)))
        merge_dir = SRC.create_merge_dir(os.path.abspath(a.out_root), now)          # F2
    except SRC.Rejected as e:
        print(json.dumps({"overall": "REJECTED", "code": e.code, "reason": e.msg}, ensure_ascii=False))
        return 2
    run = Run(merge_dir, hooks)
    tmp = run.p(TMP_DIR)
    os.mkdir(tmp)                                                                    # 不存在才建立（MERGE 是新資料夾）
    saved_tempdir = tempfile.tempdir                                                 # 保存原值（可能為 None）
    tempfile.tempdir = tmp                                                           # 在任何 openpyxl 存檔之前設定
    try:
        if os.path.normcase(os.path.abspath(tempfile.gettempdir())) != os.path.normcase(os.path.abspath(tmp)):
            raise RuntimeError("tempfile 暫存位置設定失敗")
        ctx = {"infos": infos, "space": space, "args": a, "started_at": _now(), "tool": tool_info(), "outputs": [],
               "tmp_dir": tmp}
        try:
            return _pipeline(run, ctx)
        except SRC.StopRun as e:
            return _finish_no_publish(run, ctx, e.overall, e.reason, e.detail)
        except Exception as e:                                                       # noqa: BLE001
            reason = "write_failed" if run.status.get("stage") in ("WRITE_DETAILS", "WRITE_SUMMARY") else "analysis_failed"
            return _finish_no_publish(run, ctx, "FAILED", reason, {"error": f"{type(e).__name__}: {e}"})
    finally:
        tempfile.tempdir = saved_tempdir                                             # 正常、例外、SystemExit 都還原


def _pipeline(run, ctx):
    import cert_merge_analysis as AN
    import cert_merge_verify as VF
    import cert_merge_xlsx as XL
    from cert_report_xlsx import raw_second_rows
    infos, a = ctx["infos"], ctx["args"]
    run.set_stage("SNAPSHOT")
    snap = SRC.make_snapshot(infos, run.dir, run.guard, run.hooks)                   # F3（H1～H3）
    ctx["snap"] = snap
    run.status["snapshot"] = {"h1_at": snap["h1_at"], "h2_at": snap["h2_at"], "h3_at": snap["h3_at"],
                              "h2": "一致", "h3": "一致"}
    ctx["readonly_failures"] = SRC.set_readonly(snap)                                # F4
    run.set_stage("ANALYZE")                                                         # F5
    snap_root = os.path.join(run.dir, "_snapshot")
    sessions = []
    for x in infos:
        data = SRC.load_session(snap["sessions"][x["cert_id"]]["dst"], snap_root)
        sa = AN.analyze_session(data, x["s_no"])
        sa["declared_version"] = x["declared_version"]
        AN.pair_session(sa, a.cycle_order)
        sessions.append(sa)
    AN.comparability(sessions, a.cycle_order)
    if run.hooks.get("after_analysis"):
        run.hooks["after_analysis"](sessions)
    inv, counts = AN.invariants(sessions)
    ctx.update(sessions=sessions, inv=inv, counts=counts)
    run.status["count_invariants"] = inv
    if not all(inv.values()):
        raise SRC.StopRun("FAILED", "count_invariant", {"invariants": inv})
    run.set_stage("WRITE_DETAILS")                                                   # F6
    details = {}
    for sa, x in zip(sessions, infos):
        rel = detail_base(x) + ".partial.xlsx"
        path = run.p(rel)
        disclaimer = (f"{XL.DISCLAIMER_PREFIX}（開發版程式）於 {_now()} 從來源快照重產，不是原始認證紀錄；原始報告：{{orig}}")
        reps, base = VF.report_list(sa["data"]["folder"], sa["cert_id"], sa["state"], SRC.sha256_file)
        disclaimer = disclaimer.format(orig=(f"{base['file']}（SHA-256 {base['sha256'][:12]}）" if base else "無"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ss = XL.write_detail(sa, path, disclaimer, run.guard)
        rs = raw_second_rows(sa["res"], ss["grid"], sa["names_full"]) if ss else None
        # r8：決定原始報告使用的決策集合（新版 keywords／舊版時間路徑）；有新增決策且基準已確定 → 另產 R_base
        bd, base_target = None, None
        if base is not None:
            src_meta = snap["sessions"][sa["cert_id"]].get("h1") or {}
            bd = VF.baseline_decisions(sa, base, VF.report_keywords(base["path"]), src_meta)
            if bd["snapshot"] in (VF.SNAP_CONFIRMED, VF.SNAP_PRESUMED) and bd["added"]:
                sa_b = AN.analyze_session(dict(sa["data"], decisions=list(bd["base"])), sa["s_no"])
                bpath = run.p(rbase_rel(x))
                os.makedirs(os.path.dirname(bpath), exist_ok=True)
                ss_b = XL.write_detail(sa_b, bpath, disclaimer, run.guard)
                rs_b = raw_second_rows(sa_b["res"], ss_b["grid"], sa_b["names_full"]) if ss_b else None
                base_target = (sa_b, bpath, rs_b)
        details[sa["cert_id"]] = {"rel": rel, "path": path, "rs": rs, "ss": ss, "bd": bd, "base_target": base_target}
    if run.hooks.get("before_verify"):                                               # 測試注入（替身）用
        run.hooks["before_verify"](sessions, details)
    run.set_stage("VERIFY")                                                          # F7
    verify = []
    for sa in sessions:
        d = details[sa["cert_id"]]
        v = VF.verify_session(sa, d["path"], d["rs"], SRC.sha256_file, bd=d["bd"], base_target=d["base_target"])
        if run.hooks.get("after_verify"):
            v = run.hooks["after_verify"](sa, v) or v
        verify.append(v)
    ctx["verify"] = verify
    run.status["sessions"] = [_status_session(sa, v) for sa, v in zip(sessions, verify)]
    results = [v["result"] for v in verify]
    if VF.FAILED in results:
        provisional = "VERIFY_FAILED"
    elif VF.UNVERIFIABLE in results:
        provisional = "COMPLETED_WITH_UNVERIFIED"
    else:
        provisional = "COMPLETED"
    ctx["provisional"] = provisional
    run.set_stage("WRITE_SUMMARY")                                                   # F8
    names = _final_names(infos, verify, provisional)
    ctx["names"] = names
    srel = summary_base(infos) + ".partial.xlsx"
    model = _model(ctx, sessions, verify, names, details)
    XL.write_summary(run.p(srel), model, run.guard)
    outputs = [{"kind": "detail", "cert_id": x["cert_id"], "partial": details[x["cert_id"]]["rel"],
                "final": names["details"][x["cert_id"]]} for x in infos]
    outputs.append({"kind": "summary", "partial": srel, "final": names["summary"]})
    for o in outputs:                                                                # F9
        o["sha256"] = SRC.sha256_file(run.p(o["partial"]))
    ctx["outputs"] = outputs
    if run.hooks.get("after_f9"):
        run.hooks["after_f9"](run.dir, outputs)
    run.set_stage("INTEGRITY")                                                       # F10、F11
    if run.hooks.get("before_h5"):
        run.hooks["before_h5"](snap)
    h5_at, h5 = SRC.recheck_snapshot(snap)
    if run.hooks.get("before_h4"):
        run.hooks["before_h4"](snap)
    h4_at, h4 = SRC.recheck_source(snap)
    run.status["integrity"] = {
        "h5": {"at": h5_at, "result": "一致" if not any(h5.values()) else "不一致",
               "diffs": {k: v for k, v in h5.items() if v},
               **({"note": "依據的快照不可信，只供調查"} if any(h5.values()) else {})},
        "h4": {"at": h4_at, "result": "一致" if not any(h4.values()) else "不一致",
               "diffs": {k: v for k, v in h4.items() if v}}}
    ctx["h4_at"], ctx["h5_at"] = h4_at, h5_at
    overall = provisional
    if provisional != "VERIFY_FAILED":
        if any(h5.values()):
            overall = "SNAPSHOT_CHANGED"
        elif any(h4.values()):
            overall = "SOURCE_CHANGED"
    if run.hooks.get("on_stage"):
        run.hooks["on_stage"]("AFTER_H4")
    if overall in PUBLISHABLE:                                                       # F12
        return _publish(run, ctx, overall)
    return _finish_no_publish(run, ctx, overall, overall.lower(), None)


def _status_session(sa, v):
    return {"s_no": sa["s_no"], "cert_id": sa["cert_id"], "data_value_verification": v["result"],
            "reason_codes": v.get("reason_codes"), "reason": v.get("reason"),
            "param_traceability": v["param_traceability"],
            # r8：三種保證分開呈現（不合併成單一通過）
            "decision_snapshot": v.get("decision_snapshot"), "reproducibility": v.get("reproducibility"),
            "traceability": v.get("traceability"), "traceability_note": v.get("traceability_note"),
            "uncounted_stages": [s["id"] for s in sa.get("uncounted_stages") or []],
            "stopped_stages": [s["id"] for s in sa.get("stopped_stages") or []],
            "declared_version": sa["declared_version"] or "未提供", "version_source": "使用者提供",
            "version_verification": "未驗證"}


def _final_names(infos, verify, provisional):
    vm = {v["cert_id"]: v["result"] for v in verify}
    det = {}
    for x in infos:
        db = detail_base(x)
        det[x["cert_id"]] = db + ("_資料值未驗證" if vm[x["cert_id"]] == "UNVERIFIABLE" else "") + ".xlsx"
    sb = summary_base(infos) + ("_含未驗證明細" if provisional == "COMPLETED_WITH_UNVERIFIED" else "") + ".xlsx"
    return {"details": det, "summary": sb}


# =============================================================================
# 整合活頁簿資料（只用記憶體內容）
# =============================================================================
def _model(ctx, sessions, verify, names, details):
    import cert_merge_analysis as AN
    import cert_merge_xlsx as XL
    snap, infos = ctx["snap"], ctx["infos"]
    title = f"{AN.title_spec(sessions)} 跨 Session 整合認證報告（{len(sessions)} 個 Session）"
    cycles, k = [], 0
    for cy in sorted((c for sa in sessions for c in sa["cycles"]), key=lambda c: c["a"]["stage"].start.t):
        k += 1
        cy["global_id"] = f"CY{k}"
        cycles.append(cy)
    stages = []
    for sa in sessions:
        st = sa["stages"]
        for i, s in enumerate(st):
            s["rest_from_prev_same"] = ((s["stage"].start.t - st[i - 1]["stage"].A.t).total_seconds()
                                        if i > 0 and st[i - 1]["dir"] == s["dir"] else None)
            stages.append(s)
    vmap = {v["cert_id"]: v for v in verify}
    sess_rows, quality, files = [], [], []
    for sa, x in zip(sessions, infos):
        st = sa["state"]
        v = vmap[sa["cert_id"]]
        vi = AN.version_info(sa, x["declared_version"])
        sa["version"] = vi
        sl, _ = SRC.read_jsonl(os.path.join(sa["data"]["folder"], "slots.jsonl"))
        part = sum(1 for r in sl if r.get("slot_status") == "ATTEMPTED_PARTIAL")
        s401 = sum(1 for r in sl if r.get("slot_status") and ("401" in json.dumps(r.get("failed_endpoints") or "")
                                                               or "401" in str(r.get("read_error") or "")))
        ss = details[sa["cert_id"]]["ss"]
        quality.append({"s_no": sa["s_no"], "cert_id": sa["cert_id"], "metrics": (ss or {}).get("metrics"),
                        "partial_slots": part if sa["data"]["has_slots"] else None,
                        "slots_401": s401 if sa["data"]["has_slots"] else None,
                        "read_mode": AN.read_mode(sa), "declared_version": vi["declared_version"]})
        sess_rows.append({"s_no": sa["s_no"], "cert_id": sa["cert_id"], "started_at": st.get("started_at"),
                          "ended_at": st.get("ended_at"), "status": st.get("status"),
                          "sample_count": st.get("sample_count"),
                          "stages_D": sum(1 for s in sa["stages"] if s["dir"] == "D"),
                          "stages_C": sum(1 for s in sa["stages"] if s["dir"] == "C"),
                          "cycles": len(sa["cycles"]),
                          "status_counts": {k2: sum(1 for c in sa["cycles"] if c["status"] == k2) for k2 in AN.STATUSES},
                          "version": vi, "baseline_report": v.get("baseline"),
                          "detail_name": names["details"][sa["cert_id"]], "verify_display": v["display"],
                          "params": sa["params"], "test_info": st.get("test_info"),
                          "params_note": XL.params_note(st.get("params")), "params_info": sa["params_info"],
                          "report_list": v["report_list"]})
        s = snap["sessions"][sa["cert_id"]]
        for rel, h in sorted(s["h1"].items()):
            files.append({"s_no": sa["s_no"], "cert_id": sa["cert_id"],
                          "src": os.path.join(s["src"], rel.replace("/", os.sep)),
                          "dst": os.path.join(s["dst"], rel.replace("/", os.sep)), "rel": rel, "size": h["size"],
                          "mtime": h["mtime"], "h1": h["sha256"], "h2": s["h2"][rel]["sha256"],
                          "h3": s["h3"][rel]["sha256"], "use": _use(rel)})
    pauses = [dict(z, s_no=sa["s_no"]) for sa in sessions for z in sa["uncertain_pauses"]]
    hist = [dict(h, s_no=sa["s_no"]) for sa in sessions for hs in sa["label_history"].values() for h in hs]
    hist.sort(key=lambda h: (h["s_no"], h["line"]))
    orph = [dict({k2: v2 for k2, v2 in o.items() if k2 != "_span"}, s_no=sa["s_no"]) for sa in sessions
            for o in sa["orphans"]]
    count_records = [dict(c, s_no=sa["s_no"]) for sa in sessions for c in sa.get("count_records") or []]
    anomalies = [dict(a, s_no=sa["s_no"]) for sa in sessions for a in sa.get("decision_anomalies") or []]
    unverified = [{"s_no": v["s_no"], "cert_id": v["cert_id"], "reason": v["reason"],
                   "scope": "該 Session 的重產明細未完成資料值比對，不得視為已驗證；整合報告的階段、配對與確認判定不受影響"}
                  for v in verify if v["result"] == "UNVERIFIABLE"]
    ctx["unverified"] = unverified
    return {"written_at": _now(), "invariants": ctx["inv"], "verify": verify,
            "planned_summary_name": (names["summary"] + "（尚未發布）") if ctx["provisional"] != "VERIFY_FAILED"
            else "不發布（VERIFY_FAILED）",
            "unverified_sessions": unverified, "title": title, "counts": ctx["counts"], "cycles": cycles,
            "sessions": sess_rows, "stages": stages, "pauses": pauses, "label_history": hist, "orphans": orph,
            "count_records": count_records, "decision_anomalies": anomalies,
            "quality": quality, "endpoint_comparability": AN.endpoint_comparability(sessions),
            "tool_note": "開發版（r2c 程式碼）；模組 SHA-256 見來源追溯與 manifest；r2b 記錄的 Session 在重產明細套用 FAIL-2 規則（X1～X3）",
            "files": files, "tool": ctx["tool"]["modules"]}


def _use(rel):
    n = rel.split("/")[-1]
    return {"observations.jsonl": "觀測", "slots.jsonl": "格點", "decisions.jsonl": "操作人員決策",
            "commands.jsonl": "指令", "cert_state.json": "認證狀態", "screenshots.jsonl": "截圖紀錄",
            "env_start_snapshot.json": "環控快照", "cert_timing.jsonl": "計時（版本特徵）",
            "events.jsonl": "事件（未使用）"}.get(n, "原始報告" if rel.startswith("reports/") else
                                              ("截圖" if rel.startswith("screenshots/") else
                                               ("事後補拍" if rel.startswith("supplements/") else "（未使用）")))


# =============================================================================
# manifest（§11.3）
# =============================================================================
def _manifest(run, ctx, overall, publish_state, final_names=True):
    a, infos = ctx["args"], ctx["infos"]
    snap = ctx.get("snap") or {"sessions": {}}
    srcs = []
    for x in infos:
        s = snap["sessions"].get(x["cert_id"], {})
        files = []
        for rel in sorted(s.get("h1", {})):
            g = lambda k: (s.get(k) or {}).get(rel, {}).get("sha256")     # noqa: E731
            files.append({"path": rel, "size": s["h1"][rel]["size"], "mtime": s["h1"][rel]["mtime"],
                          "h1": g("h1"), "h2": g("h2"), "h3": g("h3"), "h4": g("h4"), "h5": g("h5")})
        sa = next((z for z in ctx.get("sessions", []) if z["cert_id"] == x["cert_id"]), None)
        v = next((z for z in ctx.get("verify", []) if z["cert_id"] == x["cert_id"]), None)
        st = x["state"]
        srcs.append({"s_no": x["s_no"], "cert_id": x["cert_id"], "source_path": x["path"], "snapshot_path": s.get("dst"),
                     "started_at": st.get("started_at"), "ended_at": st.get("ended_at"), "status": st.get("status"),
                     "sample_count": st.get("sample_count"), "params": st.get("params"), "test_info": st.get("test_info"),
                     "version": (sa or {}).get("version") or {"declared_version": x["declared_version"] or "未提供",
                                                               "version_source": "使用者提供",
                                                               "version_verification": "未驗證"},
                     "files": files,
                     "reports": [{k: r[k] for k in ("file", "sha256", "size", "recorded_at", "note", "is_baseline")}
                                 for r in (v or {}).get("report_list", [])],
                     "data_value_verification": v, "param_traceability": "UNVERIFIABLE",
                     "unknown_keys": (sa or {}).get("params_info", {}).get("unknown_keys")})
    sessions = ctx.get("sessions", [])
    import cert_merge_analysis as AN
    m = {"merge_id": os.path.basename(run.dir), "generated_at": ctx["started_at"], "finished_at": _now(),
         "pairing_rule": AN.PAIRING_RULE,
         "cycle_order": a.cycle_order, "version_args": a.version, "source_count": len(infos), "tool": ctx["tool"],
         "space": ctx.get("space"), "snapshot": run.status.get("snapshot"),
         "readonly_attribute_failures": ctx.get("readonly_failures"),
         "readonly_attribute_note": "唯讀屬性只用來降低誤操作，不是防竄改或完整性保證；完整性只依雜湊",
         "sources": srcs,
         "stages": [{"id": s["id"], "dir": s["dir"], "stage_start": s["stage_start"], "end_by": s["stage"].end_by,
                     "start_certain": s["stage"].start_certain, "end_certain": s["stage"].end_certain,
                     "engine_label": s["engine_label"], "effective_label": s["effective_label"],
                     "pairing": s["pairing"], "cycle_id": s["cycle_id"], "stopped": AN.is_stopped(s),
                     "counted": s.get("count_counted", True), "count_reason": s.get("count_reason"),
                     "count_note": s.get("count_note"), "count_history": s.get("count_history")}
                    for sa in sessions for s in sa["stages"]],
         "cycles": [{"id": c["id"], "global_id": c.get("global_id"), "certainty": c["certainty"],
                     "certainty_triggers": c["certainty_triggers"], "status": c["status"], "reasons": c["reasons"],
                     "uncounted_notes": c.get("uncounted_notes", [])}
                    for sa in sessions for c in sa["cycles"]],
         "stage_count_records": {sa["cert_id"]: sa.get("count_records", []) for sa in sessions},
         "stage_count_orphans": {sa["cert_id"]: sa.get("count_orphans", []) for sa in sessions},
         "decision_anomalies": {sa["cert_id"]: sa.get("decision_anomalies", []) for sa in sessions},
         "uncertain_pauses": [dict(z, cert_id=sa["cert_id"]) for sa in sessions for z in sa["uncertain_pauses"]],
         "label_history": {sa["cert_id"]: sa["label_history"] for sa in sessions},
         "orphan_records": [dict({k: v for k, v in o.items() if k != "_span"}, cert_id=sa["cert_id"])
                            for sa in sessions for o in sa["orphans"]],
         "comparability": [{"id": c["id"], "time": c.get("time_comparable"), "extremes": c.get("ext_comparable")}
                           for sa in sessions for c in sa["cycles"]],
         "count_invariants": run.status.get("count_invariants"),
         "verify_scope": __import__("cert_merge_verify").VERIFY_SCOPE,
         "integrity": dict(run.status.get("integrity") or {},
                           statement=INTEGRITY_STATEMENT.format(at=ctx.get("h4_at") or "（未執行）")),
         "outputs": [{"partial": o["partial"], "final": o["final"] if final_names else None, "sha256": o.get("sha256")}
                     for o in ctx.get("outputs", [])],
         "overall": overall, "publish": {"state": publish_state}}
    return m


def _write_new(path, data):
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


# =============================================================================
# 發布協定（§11.4）
# =============================================================================
def _publish(run, ctx, overall):
    outs = ctx["outputs"]
    for o in outs:                                                                   # G5
        if os.path.exists(run.p(o["final"])):
            return _finish_no_publish(run, ctx, "FAILED", "final_name_exists", {"path": o["final"]})
    man = _manifest(run, ctx, overall, "PUBLISHED")                                  # G6
    data = _dump(man)
    mp = run.p("merge_manifest.json.partial")
    try:
        _write_new(mp, data)
        if run.hooks.get("tamper_manifest"):
            run.hooks["tamper_manifest"](mp)
        # 只讀取一次原始 bytes：同一份 bytes 用於 JSON 解析、逐欄比對與 SHA-256（避免兩次讀取之間被改動而未發現）
        with open(mp, "rb") as f:
            raw = f.read()
        back = json.loads(raw.decode("utf-8"))
        if back != json.loads(data):
            raise OSError("manifest 讀回內容不一致")
        msha = SRC.sha256_bytes(raw)
    except (OSError, ValueError) as e:                       # 讀取失敗、解碼失敗、JSON 無法解析、內容不一致
        return _finish_no_publish(run, ctx, "FAILED", "manifest_write_failed", {"error": f"{type(e).__name__}: {e}"},
                                  manifest_name="merge_manifest.json.failed")
    if run.hooks.get("after_manifest_hash"):
        run.hooks["after_manifest_hash"](raw, msha)
    plan = [{"partial": o["partial"], "final": o["final"], "sha256": o["sha256"]} for o in outs]
    plan.append({"partial": "merge_manifest.json.partial", "final": "merge_manifest.json", "sha256": msha})
    try:                                                                             # 預寫 PUBLISHING
        run.status["publish"] = {"state": "PUBLISHING", "planned": plan, "done": []}
        run.set_stage("PUBLISHING", overall=None, manifest_sha256=msha)
    except OSError as e:
        print(json.dumps({"overall": "FAILED", "reason": "status_publishing_write_failed", "error": str(e),
                          "merge_dir": run.dir}, ensure_ascii=False))
        return 1
    journal = run.p("publish_journal.jsonl")
    rename = run.hooks.get("rename") or os.rename
    done = []
    try:                                                                             # R1
        for step in plan:
            rename(run.p(step["partial"]), run.p(step["final"]))
            done.append(step)
            _journal(journal, {"at": _now(), "op": "rename", "from": step["partial"], "to": step["final"]})
            run.status["publish"]["done"] = [d["final"] for d in done]
        for step in plan:                                                            # R2
            if SRC.sha256_file(run.p(step["final"])) != step["sha256"]:
                raise RuntimeError(f"post_rename_hash_mismatch: {step['final']}")
        run.status["publish"]["state"] = "PUBLISHED"                                 # R3
        run.set_stage("DONE", overall=overall, reason=None, manifest_sha256=msha)
    except Exception as e:                                                           # noqa: BLE001
        reason = "post_rename_hash_mismatch" if "post_rename_hash_mismatch" in str(e) else (
            "status_final_write_failed" if run.status["publish"].get("state") == "PUBLISHED" else "rename_failed")
        return _rollback(run, ctx, done, journal, reason, str(e), msha)
    print(f"manifest SHA-256：{msha}")                                               # R4
    print(f"發布完成（判定依據：{run.p('STATUS.json')} 的 publish.state＝PUBLISHED，manifest SHA-256＝{msha}）")
    print(json.dumps({"overall": overall, "merge_dir": run.dir, "manifest_sha256": msha,
                      "publish_state": "PUBLISHED"}, ensure_ascii=False))
    return 0


def _journal(path, rec):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _rollback(run, ctx, done, journal, reason, err, msha):
    rename = run.hooks.get("rename_back") or os.rename
    failed = []
    for step in reversed(done):
        try:
            rename(run.p(step["final"]), run.p(step["partial"]))
            _journal(journal, {"at": _now(), "op": "rollback", "from": step["final"], "to": step["partial"]})
        except Exception as e:                                                       # noqa: BLE001
            failed.append({"file": step["final"], "error": f"{type(e).__name__}: {e}"})
            try:
                _journal(journal, {"at": _now(), "op": "rollback_failed", "file": step["final"], "error": str(e)})
            except OSError:
                pass
    if failed:
        overall, pstate = "PUBLISH_INCONSISTENT", "INCONSISTENT"
    else:
        overall, pstate = "PUBLISH_FAILED", "ROLLED_BACK"
    actual = []
    for step in [p for p in run.status["publish"].get("planned", [])]:
        for nm in (step["final"], step["partial"]):
            if os.path.exists(run.p(nm)):
                actual.append(nm)
    run.status["publish"].update(state=pstate, actual_names=actual, rollback_failures=failed)
    try:
        run.set_stage("DONE", overall=overall, reason=reason, error=err, manifest_sha256=msha)
    except Exception as e:                                                           # noqa: BLE001
        print(json.dumps({"overall": overall, "reason": reason, "status_write_failed": str(e),
                          "note": "STATUS 停在 PUBLISHING → 依判定規則無效"}, ensure_ascii=False))
    print(f"manifest SHA-256：{msha}")
    print(json.dumps({"overall": overall, "reason": reason, "merge_dir": run.dir, "manifest_sha256": msha,
                      "publish_state": pstate}, ensure_ascii=False))
    return 1


def _finish_no_publish(run, ctx, overall, reason, detail, manifest_name="merge_manifest.json"):
    """不可發布結果的收尾（§11.4）：manifest（publish＝NOT_ATTEMPTED）→ 最終 STATUS → 印出 manifest SHA-256。"""
    msha = None
    try:
        man = _manifest(run, ctx, overall, "NOT_ATTEMPTED", final_names=False)
        man["reason"], man["detail"] = reason, detail
        mp = run.p(manifest_name)
        if os.path.exists(mp):
            mp = run.p(manifest_name + f".{datetime.now():%H%M%S%f}")
        _write_new(mp, _dump(man))
        msha = SRC.sha256_file(mp)
    except Exception as e:                                                           # noqa: BLE001
        detail = dict(detail or {}, manifest_error=f"{type(e).__name__}: {e}")
    try:
        run.status["publish"]["state"] = run.status["publish"].get("state") or "NOT_ATTEMPTED"
        if run.status["publish"]["state"] == "PUBLISHING":
            run.status["publish"]["state"] = "NOT_ATTEMPTED"
        run.set_stage("DONE", overall=overall, reason=reason, detail=detail, manifest_sha256=msha)
    except Exception as e:                                                           # noqa: BLE001
        print(json.dumps({"overall": overall, "status_write_failed": str(e)}, ensure_ascii=False))
    if msha:
        print(f"manifest SHA-256：{msha}")
    print(json.dumps({"overall": overall, "reason": reason, "merge_dir": run.dir, "manifest_sha256": msha,
                      "publish_state": "NOT_ATTEMPTED"}, ensure_ascii=False, default=str))
    return 1


if __name__ == "__main__":
    sys.exit(main())
