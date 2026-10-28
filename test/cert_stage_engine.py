# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— 階段判斷與每分鐘時點值（P1 離線核心，純函式）
======================================================================
輸入：觀測序列（Obs list）＋ 參數 ＋（選擇性）操作人員確認紀錄
輸出：Result（階段、停頓／不確定項目、每筆觀測的狀態標記、事件）

本模組**不連設備、不讀寫檔案、不 import 單次報告模組**；同樣輸入永遠產生同樣結果。

規則依「方案 B 停頓判斷最終整理版 v3」：
  - 觀測狀態：觀測運行 / 觀測待機 / 狀態未知
  - 狀態未知：相鄰兩筆有效取樣之間 ①有讀取失敗紀錄（不論間隔長短）或 ②間隔 > G
  - 連續觀測：相鄰有效取樣間隔 ≤ G 且中間無失敗紀錄（取樣之間的狀態依取樣規則推定，並非逐秒觀測）
  - R3 待機區段（連續觀測待機）達 W 秒 → 階段確定結束
  - R4 恢復同方向：①已依 R3 結束 → 新階段；② A～R 無未知且 R−B ≤ W → 合併（確定）；
        ③ 無未知且 R−B > W → 新階段；④ 有任何狀態未知 → 暫時保留同一階段＋「階段是否連續不確定」
  - R6 相反方向 → 一定分段
"""

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DIR_NAME = {"C": "充電", "D": "放電", "I": "待機"}
VALUE_FIELDS = ["soc", "current_a", "rack_max_c", "rack_min_c", "cell_vmax_v", "cell_vmin_v", "voltage_v",
                "sensor_c", "pcs_module_c", "power_kw"]

R_READ_FAIL = "讀取失敗"
R_NO_DIR = "無有效方向資料"
R_GAP = "資料間隔過長"


# =============================================================================
# 資料結構
# =============================================================================
@dataclass
class Obs:
    t: datetime
    ok: bool = True                 # 讀取成功
    values: dict = field(default_factory=dict)
    src: str = ""                   # 來源（檔名:列號），供 Raw Data 追溯
    fail_reason: str = ""
    sensor_ok: object = None        # 傳感器狀態：None=未提供 / True=正常 / False=關閉或異常
    flags: dict = field(default_factory=dict)   # 設備狀態原始值（電池上下電、PCS 旗標）：只供報告顯示，不參與階段判斷
    # ---- 以下由引擎填寫 ----
    seq: int = -1
    direction: object = None        # "C" / "D" / "I" / None（無法判定）
    state: str = ""                 # 充電 / 放電 / 停頓 / 休息 / 突波 / 未確認方向 / 讀取失敗 / 無有效方向資料
    stage_no: object = None         # 所屬階段序號（1 起）；None = 不屬任何階段


@dataclass
class Gap:
    """相鄰兩筆有效觀測 a → b 之間。reasons 空 = 連續觀測。"""
    a: Obs
    b: Obs
    reasons: tuple = ()

    @property
    def unknown(self):
        return bool(self.reasons)


@dataclass
class Block:
    """連續觀測、同方向的一段有效觀測。"""
    dir: str
    i0: int                         # 在有效觀測序列 V 中的起訖索引
    i1: int
    confirm_i: object = None        # 確認成立那一筆的索引（非待機且跨度 ≥ TCONF）

    @property
    def confirmed(self):
        return self.confirm_i is not None


@dataclass
class Pause:
    """同一階段內兩段運行之間的區間（停頓或連續性不確定）。"""
    item_id: str
    A: Obs                          # 最後觀測運行
    R: Obs                          # 首筆觀測恢復同方向
    R_conf: Obs                     # 恢復確認成立的那一筆
    B: object = None                # 首筆觀測待機（可能沒有）
    L: object = None                # R 之前最後觀測待機
    idle_runs: list = field(default_factory=list)       # [(start Obs, end Obs)]
    unknown_spans: list = field(default_factory=list)   # [(from Obs, to Obs, reasons)]
    spikes: list = field(default_factory=list)          # [(Obs, dir, n, adjacent_unknown)]
    certain: bool = True
    system_judgment: str = ""
    decision: object = None         # 最新的操作人員確認 dict（None = 尚未確認）
    stage_no: object = None
    tc: object = None               # 停止指令被接受時間（Tc；None = 無紀錄）

    @property
    def kind(self):
        return "停頓" if self.certain else "階段是否連續不確定"

    def estimate(self):
        """估計停頓 (lo, hi) 秒，依假設 H1～H3；有狀態未知／突波／缺 B 時回 None。"""
        if not self.certain or self.unknown_spans or self.spikes or self.B is None:
            return None
        return ((self.L.t - self.B.t).total_seconds(), (self.R.t - self.A.t).total_seconds())


@dataclass
class Stage:
    no: int
    dir: str
    blocks: list
    pauses: list = field(default_factory=list)
    start: object = None            # 首筆觀測（Obs）
    start_certain: bool = True
    start_note: str = ""
    A: object = None                # 最後觀測運行
    end_B: object = None            # 結束後首筆觀測待機
    end_next: object = None         # 方向切換時下一階段首筆
    end_by: str = ""                # idle_confirmed / idle_over_W / opposite / operator_split / data_end
    end_certain: bool = True
    end_unknown: list = field(default_factory=list)
    end_note: str = ""
    label: str = ""                 # 操作人員標記（滿充／滿放／未滿／中止）
    stop_cmd: object = None         # 停止指令被接受時間（Tc）
    stop_kind: str = ""             # 指令停止／自行停止／方向切換／未知
    seq0: int = 0
    seq1: int = 0

    @property
    def dir_name(self):
        return DIR_NAME[self.dir]


@dataclass
class Result:
    obs: list
    stages: list
    pauses: list                    # 所有 Pause（含已拆分者）
    events: list                    # [(Obs, 類型, 說明)]
    gaps: list
    params: dict
    decisions: list
    commands: object = None         # None = 來源沒有選單指令紀錄
    warnings: list = field(default_factory=list)   # [(Tc, 類型, 說明)]


# =============================================================================
# 基本工具
# =============================================================================
def _num(v):
    """有效數值（含 0）→ float；None／空字串／'-'／非數字／NaN → None。bool 不算數值。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and not math.isfinite(v)) else v
    s = str(v).strip()
    if s in ("", "-", "--", "N/A", "NA", "null", "None", "?"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def classify(values, p):
    """觀測值 → "C"/"D"/"I"/None。電池電流優先（充正放負），沒有電流才用 PCS 功率。"""
    pw = _num(values.get("current_a"))
    if pw is not None:
        thr = p["IDLE_CURRENT_A"]
    else:
        pw = _num(values.get("power_kw"))
        thr = p["IDLE_POWER_KW"]
        if pw is None:
            return None
    if pw > thr:
        return "C"
    if pw < -thr:
        return "D"
    return "I"


def field_value(o, key, p):
    """報表欄位值：有效數值（含 0）回數值，否則 None（呈現時顯示 N/A）。"""
    if not o.ok:
        return None
    v = _num(o.values.get(key))
    if key == "sensor_c" and v is not None:
        if o.sensor_ok is False:
            return None
        rng = p.get("SENSOR_VALID_RANGE")
        if rng and not (rng[0] <= v <= rng[1]):
            return None
    return v


def fmt_t(t, with_date=False):
    return t.strftime("%m/%d %H:%M:%S" if with_date else "%H:%M:%S")


def fmt_dur(sec):
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}時{m:02d}分{s:02d}秒" if h else f"{m}分{s:02d}秒"


# =============================================================================
# 主流程
# =============================================================================
def analyze(obs_list, p, decisions=None, commands=None):
    """
    commands：選單指令紀錄 [{"t": datetime, "cmd": "stop"|"charge"|"discharge"}]；
              None = 來源沒有指令紀錄（離線資料），報告會標示停止方式未知，不推定為自行停止。
    """
    obs = sorted(obs_list, key=lambda o: o.t)
    for i, o in enumerate(obs):
        o.seq = i
        o.direction = classify(o.values, p) if o.ok else None
        o.state, o.stage_no = "", None
    decisions = list(decisions or [])
    events = []

    V = [o for o in obs if o.ok and o.direction is not None]
    gaps = _build_gaps(obs, V, p)
    blocks = _build_blocks(V, gaps, p)

    latest = {}
    labels = {}
    for d in decisions:
        if d.get("type", "continuity") == "continuity":
            latest[d["item_id"]] = d
        elif d.get("type") == "stage_label":
            labels[d["stage_start"]] = d

    stages, pauses = _build_stages(V, gaps, blocks, p, latest)
    for st in stages:
        key = st.start.t.strftime("%Y-%m-%dT%H:%M:%S")
        if key in labels:
            st.label = labels[key].get("label", "")

    _label_obs(obs, V, blocks, stages, gaps, events)
    cmds = None if commands is None else sorted(commands, key=lambda c: c["t"])
    _attach_commands(stages, cmds)
    warnings = _stop_warnings(obs, cmds, p)
    return Result(obs=obs, stages=stages, pauses=pauses, events=events,
                  gaps=gaps, params=dict(p), decisions=decisions,
                  commands=cmds, warnings=warnings)


def _attach_commands(stages, cmds):
    """把停止指令時間（Tc）對應到階段結束與停頓；只記錄，不參與階段判定。"""
    for st in stages:
        lo = st.start.t
        for pz in st.pauses:
            hi = (pz.B or pz.R).t
            pz.tc = _last_stop(cmds, lo, hi)
            lo = pz.R.t
        if st.end_by in ("data_end", "operator_split"):
            st.stop_kind = ""
            continue
        hi = (st.end_B or st.end_next or st.A).t
        st.stop_cmd = _last_stop(cmds, lo, hi)
        if cmds is None:
            st.stop_kind = "未知（來源無選單指令紀錄）"
        elif st.stop_cmd is not None:
            st.stop_kind = "指令停止"
        elif st.end_B is None:
            st.stop_kind = "方向切換（無停止指令紀錄）"
        else:
            st.stop_kind = "自行停止（無停止指令紀錄）"


def command_stage(res, t, direction, p):
    """
    控制紀錄 → 所屬階段（產報時依最終階段分析計算；原始指令不寫階段）。回傳 Stage 或 None（N/A）。
      充／放電指令：同方向階段，且 t 落在［階段首筆觀測 − 容許, 階段結束］（容許＝CMD_STAGE_MATCH_SEC）
      停止指令    ：t 落在［階段首筆觀測, 階段結束 + 容許］，且早於下一階段首筆觀測
      其他／無方向：None
    符合的階段不是恰好一個 → None（無法可靠對應，不猜測）。
    """
    if t is None or direction not in ("charge", "discharge", "stop"):
        return None
    win = timedelta(seconds=p.get("CMD_STAGE_MATCH_SEC", 120))
    hits = []
    for k, st in enumerate(res.stages):
        end = (st.end_B or st.end_next or st.A).t
        if direction == "stop":
            nxt = res.stages[k + 1].start.t if k + 1 < len(res.stages) else None
            if st.start.t <= t <= end + win and (nxt is None or t < nxt):
                hits.append(st)
        elif st.dir == ("C" if direction == "charge" else "D") and st.start.t - win <= t <= end:
            hits.append(st)
    return hits[0] if len(hits) == 1 else None


def _last_stop(cmds, lo, hi):
    if not cmds:
        return None
    hits = [c["t"] for c in cmds if c.get("cmd") == "stop" and lo <= c["t"] <= hi]
    return hits[-1] if hits else None


def _stop_warnings(obs, cmds, p):
    """
    停止指令被接受後 STOP_WARN_SEC 秒內仍未觀測到待機 → 只記錄提示（不重送指令、不判定已停止）。
    該期間沒有有效觀測或有讀取失敗 → 提示「狀態未知，無法確認是否停止」。
    """
    out = []
    win = timedelta(seconds=p.get("STOP_WARN_SEC", 30))
    for c in cmds or []:
        if c.get("cmd") != "stop":
            continue
        tc = c["t"]
        near = [o for o in obs if tc < o.t <= tc + win]
        valid = [o for o in near if o.ok and o.direction is not None]
        if any(o.direction == "I" for o in valid):
            continue
        if not valid or len(valid) < len(near):
            out.append((tc, "停止確認：狀態未知",
                        f"停止指令已接受 {fmt_t(tc, True)}；之後 {win.seconds} 秒內狀態未知，無法確認是否停止"))
        else:
            out.append((tc, "停止確認：未觀測到停止",
                        f"停止指令已接受 {fmt_t(tc, True)}；之後 {win.seconds} 秒內仍觀測到運行，"
                        f"未觀測到設備停止（不重送指令，觀測到待機前不判定已停止）"))
    return out


def _build_gaps(obs, V, p):
    G = timedelta(seconds=p["GAP_G_SEC"])
    gaps = []
    for a, b in zip(V, V[1:]):
        reasons = []
        between = obs[a.seq + 1:b.seq]
        if any(not o.ok for o in between):
            reasons.append(R_READ_FAIL)
        if any(o.ok and o.direction is None for o in between):
            reasons.append(R_NO_DIR)
        if b.t - a.t > G:
            reasons.append(R_GAP)
        gaps.append(Gap(a, b, tuple(reasons)))
    return gaps


def _build_blocks(V, gaps, p):
    blocks = []
    for i, o in enumerate(V):
        if blocks and blocks[-1].dir == o.direction and not gaps[i - 1].unknown:
            blocks[-1].i1 = i
        else:
            blocks.append(Block(o.direction, i, i))
    tconf = timedelta(seconds=p["TCONF_SEC"])
    for b in blocks:
        if b.dir == "I":
            continue
        for j in range(b.i0, b.i1 + 1):
            if V[j].t - V[b.i0].t >= tconf:
                b.confirm_i = j
                break
    return blocks


def _region(V, gaps, blocks, bi0, bi1, p):
    """分析 blocks[bi0] 結束之後、blocks[bi1] 開始之前（bi1=None → 到資料結尾）的區間。"""
    end_i = blocks[bi1].i0 if bi1 is not None else len(V) - 1
    start_i = blocks[bi0].i1
    unknown = [(g.a, g.b, g.reasons) for g in gaps[start_i:end_i] if g.unknown]
    mids = blocks[bi0 + 1:bi1] if bi1 is not None else blocks[bi0 + 1:]
    idle_runs = [(V[b.i0], V[b.i1]) for b in mids if b.dir == "I"]
    spikes = []
    for b in mids:
        if b.dir != "I":
            adj = (b.i0 > 0 and gaps[b.i0 - 1].unknown) or (b.i1 < len(gaps) and gaps[b.i1].unknown)
            spikes.append((V[b.i0], b.dir, b.i1 - b.i0 + 1, adj))
    W = timedelta(seconds=p["W_PAUSE_SEC"])
    confirmed = None
    for s, e in idle_runs:
        if e.t - s.t >= W:
            # 連續觀測待機達 W：確定結束於 s 起第一筆 ≥ W 的待機觀測
            confirmed = (s, e)
            break
    return {"unknown": unknown, "idle_runs": idle_runs, "spikes": spikes,
            "confirmed": confirmed,
            "B": idle_runs[0][0] if idle_runs else None,
            "L": idle_runs[-1][1] if idle_runs else None}


def _close(st, V, blocks, reg, end_by, nxt=None):
    """依區間資訊填寫階段結束資訊。"""
    lb = st.blocks[-1]
    st.A = V[lb.i1]
    st.end_by = end_by
    st.end_B = reg["B"]
    st.end_next = nxt
    if end_by in ("idle_confirmed", "idle_over_W"):
        # 只看「確認結束的待機區段」開始之前的狀態未知
        cut = reg["confirmed"][0] if (end_by == "idle_confirmed" and reg["confirmed"]) else None
        unk = [u for u in reg["unknown"] if cut is None or u[1].t <= cut.t]
        st.end_unknown = unk
        st.end_certain = not unk
    elif end_by == "opposite":
        st.end_unknown = list(reg["unknown"])
        st.end_certain = not reg["unknown"]
    else:  # operator_split / data_end
        st.end_unknown = list(reg["unknown"])
        st.end_certain = False
    st.end_note = _end_note(st)


def _end_note(st):
    A = st.A
    if st.end_by == "data_end":
        return f"資料結束時尚未確認結束（最後觀測{st.dir_name} {fmt_t(A.t, True)}）"
    if st.end_by == "operator_split":
        return (f"操作人員確認拆分：最後觀測{st.dir_name} {fmt_t(A.t, True)}，"
                f"之後狀態未知；實際結束時間未知")
    other = st.end_B if st.end_B is not None else st.end_next
    if other is None:
        return f"最後觀測{st.dir_name} {fmt_t(A.t, True)}"
    tag = "首筆觀測待機" if st.end_B is not None else "下一階段首筆觀測"
    if st.end_certain:
        return f"{fmt_t(A.t, True)}～{fmt_t(other.t, True)} 之間（最後觀測{st.dir_name}→{tag}）"
    spans = "、".join(f"{fmt_t(a.t)}～{fmt_t(b.t)}（{'／'.join(r)}）" for a, b, r in st.end_unknown)
    return (f"結束時間不確定：最後觀測{st.dir_name} {fmt_t(A.t, True)}；{tag} {fmt_t(other.t, True)}；"
            f"狀態未知 {spans}")


def _new_stage(no, V, blocks, bi, gaps):
    b = blocks[bi]
    st = Stage(no=no, dir=b.dir, blocks=[b])
    st.start = V[b.i0]
    if b.i0 == 0:
        st.start_certain = False
        st.start_note = f"資料開始時已在{st.dir_name}（實際開始時間未知）"
    elif gaps[b.i0 - 1].unknown:
        g = gaps[b.i0 - 1]
        st.start_certain = False
        st.start_note = (f"開始時間不確定：{fmt_t(g.a.t)}～{fmt_t(g.b.t)} 狀態未知"
                         f"（{'／'.join(g.reasons)}）")
    return st


def _build_stages(V, gaps, blocks, p, latest):
    W = timedelta(seconds=p["W_PAUSE_SEC"])
    conf_idx = [k for k, b in enumerate(blocks) if b.dir != "I" and b.confirmed]
    stages, pauses = [], []
    if not conf_idx:
        return stages, pauses
    cur = _new_stage(1, V, blocks, conf_idx[0], gaps)
    cur_bi = conf_idx[0]
    for bi in conf_idx[1:]:
        nb = blocks[bi]
        reg = _region(V, gaps, blocks, cur_bi, bi, p)
        A, R, Rc = V[blocks[cur_bi].i1], V[nb.i0], V[nb.confirm_i]
        if nb.dir != cur.dir:
            # 方向切換一定分段；若切換前已連續觀測待機達 W，結束依 R3 判定
            _close(cur, V, blocks, reg, "idle_confirmed" if reg["confirmed"] else "opposite", nxt=R)
            stages.append(cur)
            cur = _new_stage(len(stages) + 1, V, blocks, bi, gaps)
            cur_bi = bi
            continue
        if reg["confirmed"]:
            verdict = "new"                                   # ①
            end_by = "idle_confirmed"
        elif not reg["unknown"]:
            if reg["B"] is None or (R.t - reg["B"].t) <= W:   # ②（剛好 W 算在內）
                verdict = "merge"
            else:                                             # ③
                verdict = "new"
                end_by = "idle_over_W"
        else:                                                 # ④
            verdict = "uncertain"
        if verdict == "new":
            _close(cur, V, blocks, reg, end_by)
            stages.append(cur)
            cur = _new_stage(len(stages) + 1, V, blocks, bi, gaps)
            cur_bi = bi
            continue
        item_id = f"U-{A.t:%Y%m%d%H%M%S}-{R.t:%Y%m%d%H%M%S}"
        pz = Pause(item_id=item_id, A=A, R=R, R_conf=Rc, B=reg["B"], L=reg["L"],
                   idle_runs=reg["idle_runs"], unknown_spans=reg["unknown"],
                   spikes=reg["spikes"], certain=(verdict == "merge"))
        if pz.certain:
            pz.system_judgment = "合併（確定）"
        else:
            pz.system_judgment = "暫時保留同一階段（階段是否連續不確定）"
            pz.decision = latest.get(item_id)
        pauses.append(pz)
        if pz.decision and pz.decision.get("choice") == "split":
            pz.stage_no = cur.no
            _close(cur, V, blocks, reg, "operator_split")
            stages.append(cur)
            cur = _new_stage(len(stages) + 1, V, blocks, bi, gaps)
            cur.start_certain = False
            cur.start_note = (f"操作人員確認拆分：首筆觀測{cur.dir_name} {fmt_t(R.t, True)}，"
                              f"之前狀態未知；實際開始時間未知")
            cur_bi = bi
            continue
        pz.stage_no = cur.no
        cur.pauses.append(pz)
        cur.blocks.append(nb)
        cur_bi = bi
    reg = _region(V, gaps, blocks, cur_bi, None, p)
    _close(cur, V, blocks, reg, "idle_confirmed" if reg["confirmed"] else "data_end")
    stages.append(cur)
    # 依方向編號「第N次充電／放電」由呈現層處理；這裡只固定序號
    for st in stages:
        st.seq0 = V[st.blocks[0].i0].seq
        st.seq1 = V[st.blocks[-1].i1].seq
    return stages, pauses


def _label_obs(obs, V, blocks, stages, gaps, events):
    vpos = {o.seq: i for i, o in enumerate(V)}
    blk_of = {}
    for b in blocks:
        for j in range(b.i0, b.i1 + 1):
            blk_of[j] = b
    stage_at = {}
    for st in stages:
        for s in range(st.seq0, st.seq1 + 1):
            stage_at[s] = st
    active_seq = set()
    for st in stages:
        for b in st.blocks:
            for j in range(b.i0, b.i1 + 1):
                active_seq.add(V[j].seq)
    for o in obs:
        st = stage_at.get(o.seq)
        o.stage_no = st.no if st else None
        if not o.ok:
            o.state = R_READ_FAIL
            events.append((o, R_READ_FAIL, o.fail_reason or ""))
            continue
        if o.direction is None:
            o.state = R_NO_DIR
            events.append((o, R_NO_DIR, ""))
            continue
        if o.seq in active_seq:
            o.state = DIR_NAME[o.direction]
            continue
        b = blk_of[vpos[o.seq]]
        if o.direction == "I":
            o.state = "停頓" if st else "休息"
            continue
        adj = (b.i0 > 0 and gaps[b.i0 - 1].unknown) or (b.i1 < len(gaps) and gaps[b.i1].unknown)
        if b.confirmed:
            # 已確認但未成為階段的方向區段只會出現在資料邊界之外；保守標示
            o.state = "未確認方向"
        else:
            o.state = "未確認方向" if adj else "突波"
        if o.seq == V[b.i0].seq:
            events.append((o, o.state, f"{DIR_NAME[b.dir]} {b.i1 - b.i0 + 1} 筆，"
                                       f"未達確認門檻" + ("（鄰接狀態未知）" if adj else "")))
    for g in gaps:
        if R_GAP in g.reasons:
            events.append((g.a, R_GAP, f"{fmt_t(g.a.t, True)}～{fmt_t(g.b.t, True)} "
                                       f"間隔 {fmt_dur((g.b.t - g.a.t).total_seconds())}"))
    events.sort(key=lambda e: (e[0].t, e[0].seq))


# =============================================================================
# 階段命名
# =============================================================================
def stage_names(stages):
    """「第N次充電／放電」依方向各自計數；有操作人員標記時附註。"""
    cnt = {"C": 0, "D": 0}
    out = {}
    for st in stages:
        cnt[st.dir] += 1
        out[st.no] = f"第{cnt[st.dir]}次{st.dir_name}"
    return out


# =============================================================================
# 每分鐘時點值
# =============================================================================
@dataclass
class MinuteRow:
    T: datetime
    obs: object                     # 選用的 Obs；None = N/A
    na_reason: str = ""


def _ceil_min(t):
    base = t.replace(second=0, microsecond=0)
    return base if base == t else base + timedelta(minutes=1)


def _floor_min(t):
    return t.replace(second=0, microsecond=0)


def select_minutes(cands, all_obs, t_from, t_to, tol_sec):
    """
    cands：可選用的有效觀測（已依時間排序）。整分 T ∈ [t_from, t_to]，
    取 |t − T| ≤ tol 且最小者（同距取較早）；沒有則 N/A 並說明原因。
    """
    tol = timedelta(seconds=tol_sec)
    ts = [o.t for o in cands]
    all_ts = [o.t for o in all_obs]
    rows = []
    T = _ceil_min(t_from)
    end = _floor_min(t_to)
    while T <= end:
        k = bisect.bisect_left(ts, T)
        best = None
        for j in (k - 1, k):
            if 0 <= j < len(cands):
                d = abs(ts[j] - T)
                if d <= tol and (best is None or d < abs(best.t - T)):
                    best = cands[j]
        if best is not None:
            rows.append(MinuteRow(T, best))
        else:
            lo = bisect.bisect_left(all_ts, T - tol)
            hi = bisect.bisect_right(all_ts, T + tol)
            near = all_obs[lo:hi]
            if any(not o.ok for o in near):
                reason = f"±{tol_sec} 秒內讀取失敗"
            elif any(o.ok and o.direction is None for o in near):
                reason = f"±{tol_sec} 秒內無有效方向資料"
            elif near:
                reason = f"±{tol_sec} 秒內僅有其他階段或休息資料"
            else:
                reason = f"±{tol_sec} 秒內無取樣"
            rows.append(MinuteRow(T, None, reason))
        T += timedelta(minutes=1)
    return rows


def stage_minutes(res, st):
    """階段頁：只用該階段範圍內的有效觀測（運行與停頓），整分 T 落在首筆～最後運行觀測之間。"""
    cands = [o for o in res.obs if o.stage_no == st.no and o.ok and o.direction is not None]
    return select_minutes(cands, res.obs, st.start.t, st.A.t, res.params["MINUTE_TOL_SEC"])


def overall_minutes(res):
    """整體頁：開始～結束全程，含休息；讀取成功的觀測都可選用（方向無法判定也照樣呈現溫度）。
    只有真正沒有取樣／讀取失敗時才 N/A（圖表斷線，不補值、不內插）。"""
    cands = [o for o in res.obs if o.ok]
    if not res.obs:
        return []
    return select_minutes(cands, res.obs, res.obs[0].t, res.obs[-1].t, res.params["MINUTE_TOL_SEC"])


# =============================================================================
# 相鄰階段之間的休息時間
# =============================================================================
def rest_between(res, st, nxt):
    """
    休息時間 = 下一階段首筆觀測運行 R − 本階段結束後首筆觀測待機 B（同 W 的起算點）。
    回傳 (秒數或 None, 說明)；A～R 之間有任何狀態未知、或未觀測到待機 → None（未知）。
    """
    unk = [g for g in res.gaps if g.unknown and g.a.t >= st.A.t and g.b.t <= nxt.start.t]
    if unk:
        return None, "期間有狀態未知（" + "／".join(sorted({r for g in unk for r in g.reasons})) + "）"
    if st.end_B is None:
        return None, "未觀測到待機"
    return (nxt.start.t - st.end_B.t).total_seconds(), ""
