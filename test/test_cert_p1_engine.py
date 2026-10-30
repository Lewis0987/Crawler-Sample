# -*- coding: utf-8 -*-
"""
認證測試報告 P1 —— 階段判斷（方案 B v3）離線驗證 T01～T33 ＋ 每分鐘時點值
=====================================================================
完全離線：只用合成時間序列呼叫 cert_stage_engine，不連設備、不讀寫任何報告檔。
參數使用 cert_report_config 的值（W=60 已確認；TCONF=15、G=30、±20 秒、取樣 10 秒、
停止提示 30 秒皆為**離線測試暫用值**）。
執行：python test_cert_p1_engine.py
記號：t 為距 A（最後觀測運行）的秒數；預設前段為放電 D 由 −120 到 0 秒、每 10 秒一筆。
"""

import copy
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import cert_report_config as CFG
from cert_stage_engine import (Obs, R_GAP, R_NO_HMI, R_READ_FAIL, analyze, has_hmi_values, overall_minutes,
                               rest_between, stage_minutes, stage_names)

P = CFG.params()
BASE = datetime(2026, 1, 5, 10, 0, 0)
_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


CUR = {"D": -140.0, "C": 135.0, "I": 0.0}


def ob(t, k, base=BASE, **vals):
    """k：D 放電／C 充電／I 待機／X 讀取失敗。"""
    if k == "X":
        return Obs(t=base + timedelta(seconds=t), ok=False, fail_reason="模擬讀取失敗",
                   src=f"syn:{t}")
    v = {"soc": 50, "current_a": CUR[k], "rack_max_c": 40, "rack_min_c": 30,
         "voltage_v": 1200.0, "sensor_c": 25.0, "pcs_module_c": None, "power_kw": None}
    v.update(vals)
    return Obs(t=base + timedelta(seconds=t), values=v, src=f"syn:{t}")


def seq(t0, t1, k, step=10):
    return [ob(t, k) for t in range(t0, t1 + 1, step)]


def pre():
    return seq(-120, 0, "D")


def run(obs, decisions=None, commands=None, **over):
    return analyze(obs, CFG.params(**over), decisions, commands)


def secs(o):
    return int((o.t - BASE).total_seconds())


def only(res):
    """單一階段時回傳 (stage, pauses)。"""
    return res.stages[0], res.stages[0].pauses


# =============================================================================
print("\n== 60 秒邊界 ==")
r = run(pre() + seq(10, 60, "I") + seq(69, 109, "D"))
st, pz = only(r)
check("T01 R−B=59 → 合併（確定），停頓 1 次",
      len(r.stages) == 1 and len(pz) == 1 and pz[0].certain)
check("T01 估計停頓 50～69 秒（依假設 H1～H3）", pz[0].estimate() == (50.0, 69.0))

r = run(pre() + seq(10, 60, "I") + seq(70, 110, "D"))
check("T02 R−B 剛好 60 → 合併（確定）",
      len(r.stages) == 1 and len(r.stages[0].pauses) == 1 and r.stages[0].pauses[0].certain)

r = run(pre() + seq(10, 70, "I"))
st = r.stages[0]
check("T03 連續觀測待機達 60 秒 → 確定結束，結束時間 A～B 之間",
      st.end_by == "idle_confirmed" and st.end_certain and secs(st.A) == 0 and secs(st.end_B) == 10)
r = run(pre() + seq(10, 70, "I") + seq(80, 120, "D"))
check("T03b 之後再放電 → 第 2 個階段（確定）",
      len(r.stages) == 2 and r.stages[0].end_by == "idle_confirmed" and r.stages[0].end_certain)

r = run(pre() + seq(10, 60, "I") + [ob(65, "I")] + [ob(t, "D") for t in (73, 83, 93, 103)])
check("T04 待機觀測到 B+55、R=B+63、無狀態未知 → 新階段（確定）",
      len(r.stages) == 2 and r.stages[0].end_by == "idle_over_W" and r.stages[0].end_certain
      and not r.pauses)

# =============================================================================
print("\n== 恢復確認延後 ==")
r = run(pre() + seq(10, 60, "I") + [ob(t, "D") for t in (65, 75, 85, 95)])
check("T05 R=B+55、B+75 才確認 → 不在 B+60 宣告結束，合併（確定）",
      len(r.stages) == 1 and len(r.pauses) == 1 and r.pauses[0].certain
      and secs(r.pauses[0].R) == 65 and secs(r.pauses[0].R_conf) == 85)

r = run(pre() + seq(10, 60, "I") + [ob(65, "D")] + seq(75, 145, "I"))
st = r.stages[0]
spike = [o for o in r.obs if secs(o) == 65][0]
check("T06 B+55 單筆突波 → 記錄突波，不算恢復", spike.state == "突波"
      and any(e[1] == "突波" for e in r.events))
check("T06 從突波後 B′=B+65 重新觀測，B+125 起確定結束；結束時間仍為 A～B",
      len(r.stages) == 1 and st.end_by == "idle_confirmed" and st.end_certain
      and secs(st.end_B) == 10)

r = run(pre() + seq(10, 60, "I") + [ob(65, "D"), ob(75, "X"), ob(85, "X")]
        + [ob(t, "D") for t in (95, 105, 115, 125)])
st = r.stages[0]
check("T07 恢復確認被讀取失敗打斷 → 暫時保留同一階段＋不確定，估計未知",
      len(r.stages) == 1 and len(st.pauses) == 1 and not st.pauses[0].certain
      and st.pauses[0].estimate() is None)

r = run(pre() + seq(10, 60, "I") + [ob(t, "C") for t in (68, 78, 88, 98)])
check("T08 停頓中改為相反方向 → 分成兩個階段（確定）",
      len(r.stages) == 2 and r.stages[1].dir == "C" and r.stages[0].end_certain)

# =============================================================================
print("\n== 讀取失敗（不論是否超過 G）==")
r = run(pre() + seq(10, 40, "I") + [ob(50, "X")] + seq(60, 130, "I"))
st = r.stages[0]
g = [x for x in r.gaps if secs(x.a) == 40][0]
check("T09 單筆讀取失敗（間隔 20 秒 ≤ G）→ 標示讀取失敗、不標示資料間隔過長",
      g.reasons == (R_READ_FAIL,))
check("T09 B′=B+50 重新觀測，B+110 確定結束；結束時間不確定",
      st.end_by == "idle_confirmed" and not st.end_certain
      and any(secs(a) == 40 and secs(b) == 60 for a, b, _ in st.end_unknown))

r = run(pre() + seq(10, 40, "I") + [ob(50, "X"), ob(60, "I")] + [ob(t, "D") for t in (65, 75, 85, 95)])
st = r.stages[0]
check("T10 R−B=55 但期間有讀取失敗 → 暫時保留同一階段＋不確定（不是合併確定）",
      len(r.stages) == 1 and len(st.pauses) == 1 and not st.pauses[0].certain
      and st.pauses[0].estimate() is None)

r = run(pre() + seq(10, 40, "I") + [ob(50, "X")] + seq(60, 90, "I") + seq(100, 140, "D"))
check("T11 R−B=90、讀取失敗後只連續觀測 30 秒待機 → 暫時保留＋不確定",
      len(r.stages) == 1 and not r.stages[0].pauses[0].certain)

r = run(pre() + [ob(10, "X")] + seq(20, 60, "D"))
st = r.stages[0]
check("T12 運行中單筆讀取失敗（未超過 G）→ 暫時保留同一階段＋不確定，保留未知區間與原因",
      len(r.stages) == 1 and len(st.pauses) == 1 and not st.pauses[0].certain
      and st.pauses[0].B is None
      and st.pauses[0].unknown_spans[0][2] == (R_READ_FAIL,))

# =============================================================================
print("\n== 資料間隔過長 ==")
r = run(pre() + seq(10, 30, "I") + seq(80, 150, "I"))
st = r.stages[0]
g = [x for x in r.gaps if secs(x.a) == 30][0]
check("T13 停頓中間隔 50 秒（無失敗紀錄）→ 資料間隔過長", g.reasons == (R_GAP,))
check("T13 B′=B+70 重新觀測、B+130 確定結束；結束時間不確定",
      st.end_by == "idle_confirmed" and not st.end_certain)

r = run(pre() + seq(45, 85, "D"))
st = r.stages[0]
check("T14 運行中間隔 45 秒（無失敗紀錄）→ 暫時保留同一階段＋不確定，原因為資料間隔過長",
      len(r.stages) == 1 and len(st.pauses) == 1 and not st.pauses[0].certain
      and st.pauses[0].unknown_spans[0][2] == (R_GAP,))

r = run(pre() + seq(10, 100, "X") + seq(110, 150, "D"))
check("T15 運行中讀取失敗 110 秒 → 暫時保留＋不確定",
      len(r.stages) == 1 and not r.stages[0].pauses[0].certain)

r = run(pre() + seq(25, 65, "D"))
st = r.stages[0]
check("T33 運行中間隔 25 秒（≤ G）且無失敗紀錄 → 連續觀測，同一階段（確定）",
      len(r.stages) == 1 and not st.pauses and all(not x.unknown for x in r.gaps))

# =============================================================================
print("\n== 停頓中斷線 ==")
t16 = pre() + seq(10, 30, "I") + seq(40, 120, "X") + seq(130, 170, "D")
r = run(t16)
pz = r.stages[0].pauses[0]
check("T16 停頓中斷線、恢復後同方向 → 暫時保留＋不確定，保留 30 與 130 的觀測",
      len(r.stages) == 1 and not pz.certain and secs(pz.L) == 30 and secs(pz.R) == 130
      and secs(pz.unknown_spans[0][0]) == 30 and secs(pz.unknown_spans[0][1]) == 130)

r = run(pre() + seq(10, 30, "I") + seq(40, 120, "X") + seq(130, 170, "C"))
check("T17 恢復後相反方向 → 分成兩個階段，分界不確定",
      len(r.stages) == 2 and not r.stages[0].end_certain and not r.stages[1].start_certain)

# =============================================================================
print("\n== 中斷前後都待機 ==")
r = run(pre() + seq(10, 40, "I") + seq(50, 100, "X") + seq(110, 170, "I"))
st = r.stages[0]
check("T18 前後待機不相加；B′=110、170 確定結束；結束時間不確定",
      st.end_by == "idle_confirmed" and not st.end_certain)
r = run(pre() + seq(10, 40, "I") + seq(50, 100, "X") + seq(110, 165, "I"))
check("T18b 中斷後只連續觀測 55 秒待機 → 尚未確認結束（資料結束時未確認）",
      r.stages[0].end_by == "data_end")

r = run(pre() + seq(10, 40, "I") + seq(50, 100, "X") + [ob(110, "I"), ob(120, "I")]
        + seq(130, 170, "D"))
check("T19 中斷後只觀測 20 秒待機即恢復 → 暫時保留＋不確定",
      len(r.stages) == 1 and not r.stages[0].pauses[0].certain)

r = run(pre() + seq(10, 90, "X") + seq(100, 160, "I"))
st = r.stages[0]
check("T20 運行中斷線、恢復後待機 → 不把 A 寫成停止時間；結束不確定，首筆待機為 100",
      st.end_by == "idle_confirmed" and not st.end_certain and secs(st.end_B) == 100
      and "不確定" in st.end_note)

r = run(pre() + [ob(10, "X")] + seq(20, 80, "I"))
st = r.stages[0]
check("T32 A 與 B 之間讀取失敗 → B=20，結束時間不確定（0 之後、20 之前）",
      st.end_by == "idle_confirmed" and not st.end_certain and secs(st.end_B) == 20)

# =============================================================================
print("\n== 方向切換 ==")
r = run(pre() + seq(10, 50, "C"))
check("T21 放電直接切換充電 → 分段（確定），分界 0～10",
      len(r.stages) == 2 and r.stages[0].end_by == "opposite" and r.stages[0].end_certain
      and secs(r.stages[0].end_next) == 10 and r.stages[0].end_B is None)

r = run(pre() + seq(10, 30, "I") + seq(40, 80, "C"))
check("T22 放電→待機→充電 → 分段（確定），首筆待機 10",
      len(r.stages) == 2 and r.stages[0].end_certain and secs(r.stages[0].end_B) == 10)
rest = [o for o in r.obs if 10 <= secs(o) <= 30]
check("T22 中間待機標為「休息」（不屬任何階段）",
      all(o.state == "休息" and o.stage_no is None for o in rest))

r = run(pre() + [ob(10, "I"), ob(20, "I"), ob(30, "C"), ob(40, "I")] + seq(50, 90, "D"))
pz = r.stages[0].pauses
check("T23 停頓中相反方向單筆突波 → 記錄突波；無狀態未知、R−B=40 → 合併（確定）",
      len(r.stages) == 1 and len(pz) == 1 and pz[0].certain and len(pz[0].spikes) == 1
      and [o for o in r.obs if secs(o) == 30][0].state == "突波")

r = run(seq(0, 50, "I") + [ob(60, "C"), ob(70, "I"), ob(80, "D"), ob(90, "I"), ob(100, "C")]
        + seq(110, 150, "I"))
check("T24 交替短暫突波都未達門檻 → 不建立任何階段，只記錄事件",
      len(r.stages) == 0 and sum(1 for e in r.events if e[1] == "突波") == 3)

# =============================================================================
print("\n== 停止指令提示 ==")
stop0 = [{"t": BASE, "cmd": "stop"}]
r = run(seq(-120, 40, "D") + seq(50, 120, "I"), commands=stop0)
st = r.stages[0]
check("T25 指令後仍運行 → 提示一次「未觀測到停止」；不重送；從 B=50 起算",
      len(r.warnings) == 1 and r.warnings[0][1] == "停止確認：未觀測到停止"
      and secs(st.end_B) == 50 and st.stop_kind == "指令停止" and st.stop_cmd == BASE)

r = run(pre() + seq(10, 60, "X") + seq(70, 140, "I"), commands=stop0)
st = r.stages[0]
check("T26 指令後讀取失敗 → 提示「狀態未知，無法確認是否停止」；B=70；結束不確定",
      len(r.warnings) == 1 and r.warnings[0][1] == "停止確認：狀態未知"
      and secs(st.end_B) == 70 and not st.end_certain)

r = run(pre() + seq(10, 80, "I"), commands=[])
st = r.stages[0]
check("T27 沒有停止指令 → 規則相同、無提示，停止方式為自行停止",
      not r.warnings and st.stop_kind.startswith("自行停止") and st.end_certain)
r = run(pre() + seq(10, 80, "I"), commands=None)
check("T27b 來源沒有指令紀錄 → 停止方式「未知」，不推定為自行停止",
      r.stages[0].stop_kind.startswith("未知"))

# =============================================================================
print("\n== 操作人員確認 ==")
r0 = run(copy.deepcopy(t16))
item = r0.pauses[0].item_id
obs_before = copy.deepcopy([(o.t, o.ok, dict(o.values)) for o in t16])
dec_split = [{"item_id": item, "choice": "split", "at": "2026-01-05 12:00:00", "by": "tester"}]
r = run(t16, decisions=dec_split)
check("T28 選「拆分」→ 兩個階段，分界不確定",
      len(r.stages) == 2 and r.stages[0].end_by == "operator_split"
      and not r.stages[0].end_certain and not r.stages[1].start_certain)
check("T28 系統原始判定與不確定狀態保留，確認紀錄附在項目上",
      r.pauses[0].system_judgment.startswith("暫時保留") and not r.pauses[0].certain
      and r.pauses[0].decision["choice"] == "split")
check("T28 原始觀測資料未被改寫",
      [(o.t, o.ok, dict(o.values)) for o in t16] == obs_before)

dec2 = [{"item_id": item, "choice": "merge", "at": "2026-01-05 12:00:00", "by": "tester"},
        {"item_id": item, "choice": "split", "at": "2026-01-05 12:05:00", "by": "tester"}]
r = run(t16, decisions=dec2)
check("T29 先合併再拆分 → 兩筆紀錄都保留，採用最新一筆（拆分）",
      len(r.decisions) == 2 and len(r.stages) == 2)

r_keep = run(t16, decisions=[{"item_id": item, "choice": "keep", "at": "x", "by": "t"}])
r_none = run(t16)
check("T30 維持不確定／未確認 → 暫時保留同一階段＋不確定",
      len(r_keep.stages) == 1 and len(r_none.stages) == 1
      and not r_keep.pauses[0].certain and not r_none.pauses[0].certain)


def snap(res):
    return ([(st.no, st.dir, st.start.t, st.A.t, st.end_by, st.end_certain, st.end_note,
              [(pz.item_id, pz.certain) for pz in st.pauses]) for st in res.stages],
            [(o.seq, o.state, o.stage_no) for o in res.obs])


check("T31 同樣輸入產生兩次 → 判定完全相同", snap(run(t16, decisions=dec2)) == snap(r))

# =============================================================================
print("\n== 每分鐘時點值 ==")
base2 = datetime(2026, 1, 5, 10, 0, 0)
obs = [ob(t, "D", soc=50) for t in range(13, 310, 10)] + [ob(t, "I") for t in range(313, 450, 10)]
r = run(obs)
st = r.stages[0]
rows = stage_minutes(r, st)
check("M1 階段頁只含首筆～最後觀測運行之間的整分（10:01～10:05）",
      [m.T.strftime("%H:%M") for m in rows] == ["10:01", "10:02", "10:03", "10:04", "10:05"])
check("M1 每列取最接近的一筆（10:01 → 10:01:03，差 +3 秒）",
      rows[0].obs.t == base2 + timedelta(seconds=63))
check("M2 階段頁不使用休息資料（最後一列 10:05 取 10:05:03 放電觀測）",
      rows[-1].obs.state == "放電")
ov = overall_minutes(r)
check("M2 整體頁 10:06 取休息資料並標示休息",
      [m for m in ov if m.T.strftime("%H:%M") == "10:06"][0].obs.state == "休息")

obs = [ob(t, "D") for t in range(0, 600, 10) if not (160 <= t <= 200)]   # 10:03 前後 ±30 秒無取樣
r = run(obs)
rows = stage_minutes(r, r.stages[0])
m3 = [m for m in rows if m.T.strftime("%H:%M") == "10:03"][0]
check("M3 ±20 秒內沒有取樣 → N/A 並說明原因（不補值）",
      m3.obs is None and "無取樣" in m3.na_reason)
obs = [ob(t, "D") for t in range(0, 600, 10)]
# 10:03 前後 ±20 秒內只有讀取失敗
obs = [o if not (160 <= (o.t - BASE).seconds <= 200) else
       Obs(t=o.t, ok=False, fail_reason="x", src="x") for o in obs]
r = run(obs)
m3 = [m for m in stage_minutes(r, r.stages[0]) if m.T.strftime("%H:%M") == "10:03"][0]
check("M3b ±20 秒內只有讀取失敗 → N/A，原因為讀取失敗", m3.obs is None and "讀取失敗" in m3.na_reason)

obs = [ob(t, "D", soc=0, sensor_c=None) for t in range(0, 200, 10)]
r = run(obs)
from cert_stage_engine import field_value
o = stage_minutes(r, r.stages[0])[0].obs
check("M4 有效 0 保留（SOC=0）", field_value(o, "soc", P) == 0)
check("M4 傳感器無值 → None（呈現 N/A）", field_value(o, "sensor_c", P) is None)
o2 = ob(0, "D", sensor_c=26.5)
o2.sensor_ok = False
check("M4 傳感器關閉／異常時即使有數值也 → N/A（不以其他溫度代替）",
      field_value(o2, "sensor_c", P) is None)
check("M4 '-' 與空字串視為無效", field_value(ob(0, "D", rack_max_c="-"), "rack_max_c", P) is None
      and field_value(ob(0, "D", rack_min_c=""), "rack_min_c", P) is None)

obs = [ob(t, "D", base=datetime(2026, 1, 5, 23, 57, 30)) for t in range(0, 400, 10)] + \
      [ob(t, "I", base=datetime(2026, 1, 5, 23, 57, 30)) for t in range(410, 500, 10)]
r = run(obs)
rows = stage_minutes(r, r.stages[0])
check("M5 跨午夜：23:58～00:04 依時間遞增、不重排",
      [m.T.strftime("%H:%M") for m in rows][:3] == ["23:58", "23:59", "00:00"]
      and all(a.T < b.T for a, b in zip(rows, rows[1:])))

obs = [ob(t, "D") for t in range(10, 51, 10)] + [ob(t, "I") for t in range(60, 140, 10)]
r = run(obs)
check("M6 短階段（10:00:10～10:00:50，沒有整分）→ 階段保留但無整分列",
      len(r.stages) == 1 and stage_minutes(r, r.stages[0]) == [])

obs = []
for k, d in enumerate("DCDCD"):
    obs += [ob(t + 1000 * k, d) for t in range(0, 300, 10)]
    obs += [ob(t + 1000 * k, "I") for t in range(300, 400, 10)]
r = run(obs)
check("M7 階段數依資料變動（5 個：放電、充電、放電、充電、放電）",
      [st.dir for st in r.stages] == list("DCDCD")
      and list(stage_names(r.stages).values()) ==
      ["第1次放電", "第1次充電", "第2次放電", "第2次充電", "第3次放電"])

print("\n== 休息時間與全程曲線 ==")
obs = seq(0, 300, "D") + seq(310, 1000, "I") + seq(1010, 1300, "C")
r = run(obs)
sec, why = rest_between(r, r.stages[0], r.stages[1])
check("R1 休息時間 = 下一階段首筆運行 R − 結束後首筆待機 B（1010 − 310 = 700 秒）",
      len(r.stages) == 2 and sec == 700 and why == "")
obs = seq(0, 300, "D") + seq(310, 600, "I") + [ob(610, "X")] + seq(620, 1000, "I") + seq(1010, 1300, "C")
r = run(obs)
sec, why = rest_between(r, r.stages[0], r.stages[1])
check("R2 休息期間有讀取失敗 → 休息時間未知（不推定）", sec is None and R_READ_FAIL in why)
obs = seq(0, 300, "D") + seq(310, 600, "I") + seq(700, 1000, "I") + seq(1010, 1300, "C")
r = run(obs)
sec, why = rest_between(r, r.stages[0], r.stages[1])
check("R3 休息期間取樣間隔超過 G → 休息時間未知", sec is None and R_GAP in why)

obs = seq(0, 300, "D") + [ob(t, "I", current_a=None) for t in range(310, 400, 10)] + seq(410, 700, "I")
r = run(obs)
rows = {m.T.strftime("%H:%M:%S"): m for m in overall_minutes(r)}
m = rows["10:06:00"]
check("O1 全程曲線：電流缺值（方向無法判定）但溫度有效 → 仍呈現溫度，不是 N/A",
      m.obs is not None and m.obs.direction is None and m.obs.values["rack_max_c"] == 40)
check("O2 全程曲線：休息但有資料的每一分鐘都有值（不因階段結束而斷線）",
      all(rows[k].obs is not None for k in rows if k >= "10:05:00"))
obs = seq(0, 300, "D") + seq(310, 400, "I") + seq(600, 900, "I")
r = run(obs)
rows = {m.T.strftime("%H:%M"): m for m in overall_minutes(r)}
check("O3 全程曲線：真正沒有取樣的分鐘（10:06:40～10:10:00 之間的 10:08、10:09）才 N/A，不補值、不內插",
      rows["10:08"].obs is None and rows["10:09"].obs is None
      and rows["10:07"].obs is not None and rows["10:10"].obs is not None)

# 實機 CERT_20261029_082805 12:00 型態：HMI 重新登入空窗 → ok=True、HMI 欄位全空、只有 6161 PCS 模組溫度
NO_HMI = dict(soc=None, current_a=None, rack_max_c=None, rack_min_c=None, voltage_v=None, sensor_c=None,
              pcs_module_c=80)
obs = (seq(0, 110, "D") + [ob(113, "D"), ob(123, "D", **NO_HMI), ob(134, "D")] + seq(144, 300, "D"))
r = run(obs)
m = {x.T.strftime("%H:%M"): x for x in overall_minutes(r)}["10:02"]
check("O4 整分 ±20 秒內同時有有效觀測（−7 秒）與 HMI 無讀值觀測（+3 秒）→ 整體頁選有效觀測（真實原始值，不內插）",
      m.obs is not None and m.obs.t == BASE + timedelta(seconds=113) and has_hmi_values(m.obs))
sm = {x.T.strftime("%H:%M"): x for x in stage_minutes(r, r.stages[0])}["10:02"]
check("O5 階段頁同一分鐘結果不變（選 −7 秒那筆）", sm.obs is not None and sm.obs.t == m.obs.t)
check("O6 HMI 無讀值的一筆不切斷階段（仍 1 個放電階段、未提前結束、仍建立不確定項目）",
      len(r.stages) == 1 and r.stages[0].dir == "D" and r.stages[0].A.t == obs[-1].t
      and r.stages[0].end_by != "idle_confirmed"
      and any(not pz.certain and pz.A.t == BASE + timedelta(seconds=113) for pz in r.pauses))
obs = seq(0, 90, "D") + [ob(110, "D", **NO_HMI), ob(125, "D", **NO_HMI)] + seq(150, 300, "D")
r = run(obs)
m = {x.T.strftime("%H:%M"): x for x in overall_minutes(r)}["10:02"]
check("O7 ±20 秒內只有 HMI 無讀值觀測 → 退而選用（保留 6161 溫度），HMI 欄位仍為空（不捏造）",
      m.obs is not None and not has_hmi_values(m.obs) and m.obs.values["pcs_module_c"] == 80)
obs = seq(0, 90, "D") + [ob(110, "D", **dict(NO_HMI, pcs_module_c=None))] + seq(150, 300, "D")
r = run(obs)
m = {x.T.strftime("%H:%M"): x for x in overall_minutes(r)}["10:02"]
check("O8 ±20 秒內只有完全無值的觀測 → N/A，原因寫明 HMI 無有效讀值（通訊異常或未登入）",
      m.obs is None and R_NO_HMI in m.na_reason)
sm8 = {x.T.strftime("%H:%M"): x for x in stage_minutes(r, r.stages[0])}["10:02"]
check("O9 階段頁同樣不選 HMI 無值觀測；真正無候選 → N/A", sm8.obs is None)

print("\n== HMI 資料有效性（Runtime 記錄 → 報表共用同一定義 hmi_valid）==")
from cert_runtime import build_record, record_to_obs
READING = {"soc_percent": 50.0, "battery_current_a": -140.0, "actual_active_power_kw": -120.0,
           "rack_max_temperature_c": 40.0, "rack_min_temperature_c": 30.0, "rack_cell_max_voltage_v": 3.4,
           "rack_cell_min_voltage_v": 3.3, "battery_voltage_v": 900.0, "_fail": []}
HEAT = {"st": "-", "pt": {"moduleTemperature": "70"}}
t_ = BASE
r_full = build_record(t_, READING, "", HEAT, "")
r_none = build_record(t_, None, "無讀值（未登入或連線失敗）", HEAT, "")          # 實機 09:58:09 型態
r_part = build_record(t_, {k: (v if k == "rack_max_temperature_c" else None) for k, v in READING.items()}, "", HEAT, "")
r_dead = build_record(t_, None, "無讀值（未登入或連線失敗）", None, "URLError: timeout")
check("V1 正常完整觀測 → ok=True、hmi_ok=True", r_full["ok"] and r_full["hmi_ok"])
check("V2 HMI 全無值＋6161 有值 → ok=True（6161 溫度保留）、hmi_ok=False，原因留在 notes",
      r_none["ok"] and r_none["hmi_ok"] is False and r_none["values"]["pcs_module_c"] == "70"
      and any("無讀值" in n for n in r_none["notes"]))
check("V3 只有部分 HMI 欄位有效（Rack 最大溫度）→ hmi_ok=True（單一欄位缺值不算無效）",
      r_part["ok"] and r_part["hmi_ok"])
check("V4 HMI 與 6161 都沒有值 → ok=False（既有行為不變）", not r_dead["ok"] and r_dead["hmi_ok"] is False)
old = {k: v for k, v in r_none.items() if k != "hmi_ok"}                    # 舊資料沒有 hmi_ok 欄位
bare = {"t": "2026-01-05T10:00:00", "ok": True, "values": {"pcs_module_c": "70"}}  # 最舊格式：無 notes／flags／hmi_ok
ob_bare = record_to_obs(bare, 1)
check("V6 最舊格式紀錄（沒有 notes、flags、hmi_ok 欄位）→ Obs.notes=[]、flags={}，HMI 依數值判定無效（不需回填 JSON）",
      ob_bare.notes == [] and ob_bare.flags == {} and not has_hmi_values(ob_bare))
check("V7 Obs 只給必要欄位（離線回放 cert_report_replay 的建構方式）→ notes／flags 有預設值，不會 AttributeError",
      Obs(t=BASE).notes == [] and Obs(t=BASE).flags == {})
check("V5 舊資料（無 hmi_ok 欄位）依數值判定：ok 照原值、HMI 無效、notes 帶入",
      record_to_obs(old, 1).ok and not has_hmi_values(record_to_obs(old, 1))
      and record_to_obs(old, 1).notes == r_none["notes"] and has_hmi_values(record_to_obs(r_full, 1)))

ok = all(_results)
print(f"\n== 認證報告 P1 階段判斷 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
