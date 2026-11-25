# -*- coding: utf-8 -*-
"""
跨 Session 整合認證報告 —— 來源處理（docs/Cert_Merge_Report_Design.md §4、§5）
================================================================================
前置驗證 V1～V9、事前空間與路徑長度檢查、MERGE 資料夾建立、快照與 H1～H5 逐檔雜湊、從快照載入。

原則：
  * 來源只以 "rb" 讀取；不對來源呼叫任何會改變內容、屬性或時間的 API。
  * 快照只在建立時寫入（"xb"，目的地存在即失敗）；之後只讀。
  * 本模組不 import cert_runtime／cert_service／api_client／report_monitor，不做任何網路連線。
"""
import hashlib
import json
import os
import re
import shutil
import socket
import stat
from datetime import datetime, timedelta

PROD_ROOT = r"D:\Crawler Sample"
PROD_ACTIVE_JSON = os.path.join(PROD_ROOT, "output", "cert_reports", "active.json")
PATH_MAX = 240
MIN_SOURCES, MAX_SOURCES = 2, 6
ISO = "%Y-%m-%dT%H:%M:%S"


class Rejected(Exception):
    """前置驗證不通過（rc=2；不建立任何輸出）。"""

    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg = code, msg


class StopRun(Exception):
    """建立 MERGE 資料夾之後的停止（依 overall／reason 記錄）。"""

    def __init__(self, overall, reason, detail=None):
        super().__init__(f"{overall}: {reason}")
        self.overall, self.reason, self.detail = overall, reason, detail or {}


# =============================================================================
# 網路封鎖（F0）
# =============================================================================
_NET_ATTEMPTS = []


def _blocked(*a, **k):
    _NET_ATTEMPTS.append(repr(a)[:120])
    raise OSError("cert_merge：禁止網路連線（不得連線 HMI／6161／PCS 或任何設備）")


def block_network():
    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked


def network_attempts():
    return list(_NET_ATTEMPTS)


# =============================================================================
# 雜湊與檔案清單
# =============================================================================
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def list_tree(root):
    """root 底下全部檔案 → {相對路徑（/ 分隔）: {"size", "mtime", "sha256"}}（只讀）。"""
    out = {}
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for fn in sorted(files):
            fp = os.path.join(d, fn)
            rel = os.path.relpath(fp, root).replace("\\", "/")
            st = os.stat(fp)
            out[rel] = {"size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "sha256": sha256_file(fp)}
    return out


def compare_trees(a, b, use_mtime=True):
    """兩份清單的差異：增、減、大小／mtime／SHA-256 不同。回傳差異清單（空＝一致）。"""
    diffs = []
    for rel in sorted(set(a) | set(b)):
        x, y = a.get(rel), b.get(rel)
        if x is None:
            diffs.append({"path": rel, "kind": "added"})
        elif y is None:
            diffs.append({"path": rel, "kind": "removed"})
        else:
            keys = ("size", "mtime", "sha256") if use_mtime else ("size", "sha256")
            bad = [k for k in keys if x.get(k) != y.get(k)]
            if bad:
                diffs.append({"path": rel, "kind": "changed", "fields": bad})
    return diffs


# =============================================================================
# 路徑工具
# =============================================================================
def norm(p):
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def is_inside(child, parent):
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def in_git_repo(path):
    """path 或其上層有 .git（目錄或 worktree 的 .git 檔）→ True。只讀檢查。"""
    p = norm(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def read_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def read_jsonl(path):
    """與 cert_runtime._read_jsonl 相同的容錯：不存在 → []；無法解析的行略過。回傳 (紀錄, 略過行數)。"""
    if not os.path.exists(path):
        return [], 0
    out, bad = [], 0
    with open(path, "rb") as f:
        for line in f.read().decode("utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    bad += 1
    return out, bad


def _parse_iso(s):
    try:
        return datetime.strptime(s, ISO) if s else None
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None


# =============================================================================
# 前置驗證 V1～V9（F1）
# =============================================================================
def validate(sources, versions, cycle_order, out_root, active_json=PROD_ACTIVE_JSON):
    """
    sources：來源資料夾清單；versions：["<cert_id>=<版本>", ...]；回傳依 started_at 排序的來源資訊清單。
    任一不通過 → Rejected（rc=2；呼叫端不得建立任何輸出）。
    """
    if cycle_order not in ("DC", "CD"):                                              # V9
        raise Rejected("V9", f"--cycle-order 必填且為 DC 或 CD（收到 {cycle_order!r}）")
    if not (MIN_SOURCES <= len(sources) <= MAX_SOURCES):                             # V1
        raise Rejected("V1", f"來源數必須 {MIN_SOURCES}～{MAX_SOURCES}（收到 {len(sources)}）")
    infos = []
    for s in sources:                                                                # V2
        if not os.path.isdir(s):
            raise Rejected("V2", f"來源資料夾不存在：{s}")
        sp = os.path.join(s, "cert_state.json")
        try:
            st = read_json(sp)
        except Exception as e:                                                       # noqa: BLE001
            raise Rejected("V2", f"cert_state.json 無法解析：{s}（{type(e).__name__}）")
        cid = st.get("cert_id")
        if cid != os.path.basename(os.path.normpath(s)):
            raise Rejected("V2", f"cert_id（{cid}）與資料夾名稱不同：{s}")
        if st.get("status") != "ended":
            raise Rejected("V2", f"{cid} 的 status 為 {st.get('status')!r}，只接受 ended（記錄中一律拒絕）")
        if not st.get("ended_at"):
            raise Rejected("V2", f"{cid} 沒有 ended_at")
        obs_p = os.path.join(s, "observations.jsonl")
        if not os.path.exists(obs_p):
            raise Rejected("V2", f"{cid} 沒有 observations.jsonl")
        infos.append({"path": os.path.abspath(s), "norm": norm(s), "cert_id": cid, "state": st,
                      "started": _parse_iso(st.get("started_at")), "ended": _parse_iso(st.get("ended_at")),
                      "obs_sha": sha256_file(obs_p)})
    for i, a in enumerate(infos):                                                    # V3
        for b in infos[i + 1:]:
            if a["norm"] == b["norm"]:
                raise Rejected("V3", f"重複輸入同一路徑：{a['path']}")
            if a["cert_id"] == b["cert_id"]:
                raise Rejected("V3", f"重複的 cert_id：{a['cert_id']}")
            if a["obs_sha"] == b["obs_sha"]:
                raise Rejected("V3", f"{a['cert_id']} 與 {b['cert_id']} 的 observations.jsonl 完全相同（疑似複製改名）")
    for x in infos:
        if x["started"] is None or x["ended"] is None:
            raise Rejected("V2", f"{x['cert_id']} 的 started_at／ended_at 無法解析")
    for i, a in enumerate(infos):                                                    # V4
        for b in infos[i + 1:]:
            if a["started"] <= b["ended"] and b["started"] <= a["ended"]:
                raise Rejected("V4", f"{a['cert_id']} 與 {b['cert_id']} 的時間區間重疊")
    tol = timedelta(seconds=5)                                                       # V5
    for x in infos:
        recs, _bad = read_jsonl(os.path.join(x["path"], "observations.jsonl"))
        ts = [t for t in (_parse_iso(r.get("t")) for r in recs) if t is not None]
        if not ts:
            raise Rejected("V5", f"{x['cert_id']} 的 observations.jsonl 沒有可解析的時間")
        if min(ts) < x["started"] - tol or max(ts) > x["ended"] + tol:
            raise Rejected("V5", f"{x['cert_id']} 的觀測時間（{min(ts)}～{max(ts)}）超出 started_at～ended_at ±5 秒")
    out_abs = os.path.abspath(out_root)                                              # V6
    for x in infos:
        if is_inside(x["path"], out_abs):
            raise Rejected("V6", f"來源位於輸出根目錄之下：{x['path']}")
        if is_inside(out_abs, x["path"]):
            raise Rejected("V6", f"輸出根目錄位於來源之下：{out_abs}")
    if is_inside(out_abs, PROD_ROOT):
        raise Rejected("V6", f"輸出根目錄位於正式版之下：{out_abs}")
    if in_git_repo(out_abs if os.path.exists(out_abs) else os.path.dirname(out_abs)):
        raise Rejected("V6", f"輸出根目錄位於 Git repository／worktree 之下：{out_abs}")
    if active_json and os.path.exists(active_json):                                  # V7（只讀）
        try:
            act = read_json(active_json)
        except Exception as e:                                                       # noqa: BLE001
            raise Rejected("V7", f"active.json 無法解析（{type(e).__name__}）")
        if act.get("cert_id") in {x["cert_id"] for x in infos}:
            raise Rejected("V7", f"active.json 指向來源 {act.get('cert_id')}（記錄中）")
    ver = {}                                                                         # V8
    ids = {x["cert_id"] for x in infos}
    for v in versions or []:
        m = re.fullmatch(r"([^=\s]+)=(\S+)", v or "")
        if not m:
            raise Rejected("V8", f"--version 格式錯誤：{v!r}（應為 <cert_id>=<版本>）")
        if m.group(1) not in ids:
            raise Rejected("V8", f"--version 的 cert_id 不在來源中：{m.group(1)}")
        if m.group(1) in ver:
            raise Rejected("V8", f"--version 重複指定：{m.group(1)}")
        ver[m.group(1)] = m.group(2)
    infos.sort(key=lambda x: x["started"])
    for i, x in enumerate(infos, 1):
        x["s_no"] = i
        x["declared_version"] = ver.get(x["cert_id"])
        del x["norm"]
    return infos


# =============================================================================
# 事前空間與路徑長度（F1）
# =============================================================================
def tree_size(root):
    total = 0
    for d, _dirs, files in os.walk(root):
        for fn in files:
            total += os.path.getsize(os.path.join(d, fn))
    return total


def required_free(total_src):
    return max(2 * total_src + 500 * 1024 * 1024, 1024 * 1024 * 1024)


def nearest_existing(path):
    """path 本身或最近一層已存在的上層資料夾（只讀判斷；不建立任何資料夾）。找不到 → None。"""
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    return p


def check_space(out_root, infos, disk_free=None):
    """
    事前空間檢查（U11、P-19）。查詢位置＝輸出根目錄或其最近一層已存在的上層資料夾（不建立任何資料夾）。
    無法取得空間資訊（找不到存在的上層、查詢例外）→ Rejected（rc=2，明確原因），不崩潰、不視為空間足夠。
    """
    total = sum(tree_size(x["path"]) for x in infos)
    probe = nearest_existing(out_root)
    if probe is None:
        raise Rejected("SPACE", f"無法取得磁碟空間資訊：輸出根目錄及其上層都不存在（{out_root}）")
    try:
        free = disk_free(probe) if disk_free else shutil.disk_usage(probe).free
    except OSError as e:
        raise Rejected("SPACE", f"無法取得磁碟空間資訊（查詢位置 {probe}；{type(e).__name__}: {e}）")
    if not isinstance(free, int) or free < 0:
        raise Rejected("SPACE", f"無法取得磁碟空間資訊（查詢位置 {probe}；回傳值 {free!r} 無效）")
    need = required_free(total)
    if free < need:
        raise Rejected("SPACE", f"輸出磁碟可用空間不足：可用 {free} bytes，需要至少 {need} bytes")
    return {"source_bytes": total, "free_bytes": free, "required_bytes": need, "probe": probe}


def planned_paths(merge_dir, infos, names):
    """所有快照目的地與輸出檔的最長可能完整路徑（含後綴）。names＝cert_merge_report.output_names 的結果。"""
    paths = []
    for x in infos:
        for d, _dirs, files in os.walk(x["path"]):
            for fn in files:
                rel = os.path.relpath(os.path.join(d, fn), x["path"])
                paths.append(os.path.join(merge_dir, "_snapshot", x["cert_id"], rel))
    for n in names:
        paths.append(os.path.join(merge_dir, n))
    return paths


def check_path_length(paths):
    long = [p for p in paths if len(os.path.abspath(p)) > PATH_MAX]
    if long:
        raise Rejected("PATH", f"{len(long)} 個路徑超過 {PATH_MAX} 字元（例 {long[0]}）")


# =============================================================================
# MERGE 資料夾（F2）與寫入守門
# =============================================================================
def _redirected(path):
    """path 的實際位置（解析 symlink／junction 後）與字面位置不同 → True（路徑中任何一層被重新導向）。"""
    a = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    return os.path.normcase(os.path.realpath(path)) != a


def create_merge_dir(out_root, now):
    """
    F2：
      * 輸出根目錄已存在 → 必須是資料夾、且整條路徑沒有 symlink／junction 重新導向 → 沿用。
      * 不存在 → 只有直接父目錄已存在、父目錄路徑沒有重新導向時，才建立輸出根目錄本身（os.mkdir，單層）；
        不遞迴建立多層上層資料夾。父目錄不存在或被重新導向 → Rejected（rc=2），不建立任何資料夾。
      * MERGE_<ts> 以 os.mkdir 建立：已存在 → Rejected（不覆蓋、不合併、不清理）。
      * 建立後再確認 MERGE 實際位置位於輸出根目錄實際位置之內（防止建立期間被導向）。
    """
    out = os.path.abspath(out_root)
    if os.path.lexists(out):
        if not os.path.isdir(out):
            raise Rejected("F2", f"輸出根目錄已存在但不是資料夾：{out}")
        if _redirected(out):
            raise Rejected("F2", f"輸出根目錄路徑含 symlink／junction 重新導向（實際位置 {os.path.realpath(out)}）：{out}")
    else:
        parent = os.path.dirname(out)
        if not os.path.isdir(parent):
            raise Rejected("F2", f"輸出根目錄的父目錄不存在（不遞迴建立上層資料夾）：{parent}")
        if _redirected(parent):
            raise Rejected("F2", f"輸出根目錄的父目錄含 symlink／junction 重新導向（實際位置 {os.path.realpath(parent)}）")
        try:
            os.mkdir(out)               # 只建立輸出根目錄本身（單層）
        except FileExistsError:
            if not os.path.isdir(out) or _redirected(out):
                raise Rejected("F2", f"輸出根目錄在建立期間被其他程序建立且不符合條件：{out}")
    d = os.path.join(out, f"MERGE_{now:%Y%m%d_%H%M%S}")
    try:
        os.mkdir(d)                     # 不存在才建立；已存在 → FileExistsError（不覆蓋、不合併）
    except FileExistsError:
        raise Rejected("F2", f"MERGE 資料夾已存在：{d}（不覆蓋）")
    if _redirected(d) or not is_inside(d, out):
        raise Rejected("F2", f"MERGE 資料夾實際位置不在輸出根目錄內（{os.path.realpath(d)}）")
    return d


class Guard:
    """寫入守門：所有寫入目標必須在本次 MERGE 資料夾之內。"""

    def __init__(self, merge_dir):
        self.root = norm(merge_dir)

    def check(self, path):
        p = norm(path)
        if not (p == self.root or p.startswith(self.root + os.sep)):
            raise PermissionError(f"寫入守門：目標不在本次 MERGE 資料夾內：{path}")
        return path


# =============================================================================
# 快照（F3、F4）與 H1～H5
# =============================================================================
def make_snapshot(infos, merge_dir, guard, hooks=None):
    """
    F3：H1 → 逐檔複製 → H2 → H3。回傳 {"sessions": {cid: {...}}, "h1_at", "h2_at", "h3_at"}。
    H2 不一致 → StopRun(SNAPSHOT_MISMATCH)；H3 不一致 → StopRun(SOURCE_CHANGED_DURING_SNAPSHOT)；
    讀寫錯誤 → StopRun(FAILED, snapshot_failed／disk_full)。皆不進入分析。
    """
    hooks = hooks or {}
    snap_root = guard.check(os.path.join(merge_dir, "_snapshot"))
    os.mkdir(snap_root)
    out = {"sessions": {}}
    out["h1_at"] = _now_iso()
    h1 = {x["cert_id"]: list_tree(x["path"]) for x in infos}
    for x in infos:
        cid = x["cert_id"]
        dst_root = guard.check(os.path.join(snap_root, cid))
        for rel in sorted(h1[cid]):
            src = os.path.join(x["path"], rel.replace("/", os.sep))
            dst = guard.check(os.path.join(dst_root, rel.replace("/", os.sep)))
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(src, "rb") as fi, open(dst, "xb") as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
            except OSError as e:
                reason = "disk_full" if getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) == 112 \
                    else "snapshot_failed"
                raise StopRun("FAILED", reason, {"cert_id": cid, "path": rel, "error": f"{type(e).__name__}: {e}"})
        out["sessions"][cid] = {"src": x["path"], "dst": dst_root, "h1": h1[cid]}
    if hooks.get("after_copy"):
        hooks["after_copy"]()
    out["h2_at"] = _now_iso()
    for cid, s in out["sessions"].items():
        s["h2"] = list_tree(s["dst"])
        d = compare_trees(s["h1"], s["h2"], use_mtime=False)          # 快照 mtime 是複製時間，只比內容
        if d:
            raise StopRun("SNAPSHOT_MISMATCH", "h2_mismatch", {"cert_id": cid, "diffs": d})
    out["h3_at"] = _now_iso()
    for x in infos:
        s = out["sessions"][x["cert_id"]]
        s["h3"] = list_tree(x["path"])
        d = compare_trees(s["h1"], s["h3"])
        if d:
            raise StopRun("SOURCE_CHANGED_DURING_SNAPSHOT", "h3_mismatch", {"cert_id": x["cert_id"], "diffs": d})
    return out


def set_readonly(snap):
    """F4：快照檔設唯讀屬性（只防誤操作，不是防竄改保證）。回傳失敗清單（只記錄）。"""
    fails = []
    for s in snap["sessions"].values():
        for rel in s["h2"]:
            p = os.path.join(s["dst"], rel.replace("/", os.sep))
            try:
                os.chmod(p, stat.S_IREAD)
            except OSError as e:
                fails.append({"path": p, "error": str(e)})
    return fails


def recheck_snapshot(snap):
    """H5：快照逐檔再算，與 H2 比對。回傳 (時間, {cid: 差異清單})。"""
    at = _now_iso()
    res = {}
    for cid, s in snap["sessions"].items():
        s["h5"] = list_tree(s["dst"])
        res[cid] = compare_trees(s["h2"], s["h5"], use_mtime=False)
    return at, res


def recheck_source(snap):
    """H4：來源逐檔再算，與 H1 比對（來源完整性只驗證到此時點）。回傳 (時間, {cid: 差異清單})。"""
    at = _now_iso()
    res = {}
    for cid, s in snap["sessions"].items():
        s["h4"] = list_tree(s["src"])
        res[cid] = compare_trees(s["h1"], s["h4"])
    return at, res


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


# =============================================================================
# 從快照載入（只讀；只接受快照路徑）
# =============================================================================
def load_session(snap_dir, snap_root):
    if not is_inside(snap_dir, snap_root):
        raise PermissionError(f"只能從快照載入：{snap_dir}")
    j = lambda n: os.path.join(snap_dir, n)        # noqa: E731
    state = read_json(j("cert_state.json"))
    records, obs_bad = read_jsonl(j("observations.jsonl"))
    decisions, dec_bad = read_jsonl(j("decisions.jsonl"))
    commands, cmd_bad = read_jsonl(j("commands.jsonl"))
    timing, _tb = read_jsonl(j("cert_timing.jsonl"))
    return {"folder": snap_dir, "state": state, "records": records, "decisions": decisions,
            "commands": commands, "timing": timing,
            "has_decisions_file": os.path.exists(j("decisions.jsonl")),
            "has_commands_file": os.path.exists(j("commands.jsonl")),
            "has_slots": os.path.exists(j("slots.jsonl")),
            "unparseable": {"observations": obs_bad, "decisions": dec_bad, "commands": cmd_bad}}
