# -*- coding: utf-8 -*-
"""
認證取樣計時分析（唯讀）—— 讀 cert_timing.jsonl／observations.jsonl／服務 log／程序取樣 CSV，輸出 Baseline 指標與 PASS／FAIL。
只讀檔案，不寫任何資料、不連設備、不碰服務。

用法：
  python cert_timing_report.py <認證資料夾> [--log 服務log路徑 ...] [--proc 程序取樣CSV] [--interval 10]

  認證資料夾：output/cert_reports/CERT_YYYYMMDD_HHMMSS
  --log ：output/logs/auto_monitor_service*.log（測試期間的 401、重新登入、AUTO 讀取失敗、Traceback；只計數，不輸出 log 內容）
          ※ AUTO 監看間隔無法從 log 量測：[AUTO] 狀態行只在內容改變時才印（report_monitor._auto_note）
  --proc：cert_proc_sampler.ps1 產生的 CSV（CPU／記憶體）
  --interval：本次設定的 SAMPLE_INTERVAL_SEC（判定門檻用；10 或 5）
"""
import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def jl(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def q(vals, p):
    """分位數（nearest-rank）；空 → None。"""
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    k = max(0, min(len(v) - 1, int(round(p * len(v) + 0.5)) - 1))
    return v[k]


def stats(vals, unit="ms"):
    v = [x for x in vals if x is not None]
    if not v:
        return "N/A（沒有資料）"
    return (f"Median {statistics.median(v):.1f}{unit}  P95 {q(v, .95):.1f}{unit}  P99 {q(v, .99):.1f}{unit}  "
            f"Max {max(v):.1f}{unit}  (n={len(v)})")


def trend(vals):
    """最後 1/3 中位數 − 最初 1/3 中位數。"""
    v = [x for x in vals if x is not None]
    if len(v) < 9:
        return None
    k = len(v) // 3
    return statistics.median(v[-k:]) - statistics.median(v[:k])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--log", nargs="*", default=[])
    ap.add_argument("--proc")
    ap.add_argument("--interval", type=float, default=10)
    a = ap.parse_args()

    obs = jl(os.path.join(a.folder, "observations.jsonl"))
    tim = jl(os.path.join(a.folder, "cert_timing.jsonl"))
    if not obs or not tim:
        print("observations.jsonl 或 cert_timing.jsonl 沒有資料 → 無法分析")
        return 2
    wr = [x for x in tim if x.get("written")]
    t_first, t_last = datetime.fromisoformat(obs[0]["t"]), datetime.fromisoformat(obs[-1]["t"])
    hours = max((t_last - t_first).total_seconds() / 3600, 1e-9)
    res = []        # (項目, 實測, 門檻說明, PASS?)

    def rule(name, value, cond, crit):
        res.append((name, value, crit, bool(cond)))

    I = a.interval
    # Menu 22 最後取樣（trigger=cert_end）不是排程間隔 → 不列入 Actual Interval（仍計入 Cycle 耗時與筆數）
    iv = [x["interval_ms"] / 1000 for x in wr[1:] if x.get("interval_ms") is not None and x.get("trigger") != "cert_end"]
    n_final = sum(1 for x in wr if x.get("trigger") == "cert_end")
    cyc = [x["cycle_ms"] for x in wr]
    print(f"== 認證取樣計時分析：{os.path.basename(os.path.abspath(a.folder))} ==")
    print(f"期間 {t_first} → {t_last}（{hours * 60:.1f} 分鐘）  設定間隔 {I:g} 秒")
    print(f"Actual Interval（monotonic）: {stats(iv, 's')}")
    print(f"Cycle Duration           : {stats(cyc)}")
    print(f"HMI Duration（含等鎖）   : {stats([x['hmi_ms'] for x in wr])}")
    print(f"  └ read_all             : {stats([(x.get('reader') or {}).get('read_ms') for x in wr])}")
    print(f"  └ 等 _CLIENT_LOCK      : {stats([(x.get('reader') or {}).get('lock_wait_ms') for x in wr])}")
    print(f"6161 Duration            : {stats([x['heat_ms'] for x in wr])}")
    print(f"Runtime Lock Wait        : {stats([x['rt_lock_wait_ms'] for x in wr])}")
    print(f"Observation Write        : {stats([x['obs_write_ms'] for x in wr])}")
    print(f"cert_state.json Write    : {stats([x['state_write_ms'] for x in wr])}")
    print(f"analysis()（每次取樣）   : {stats([x.get('analysis_ms') for x in wr])}")
    print(f"analysis()（status，最大）: {stats([x.get('status_analysis_ms_max') for x in wr])}")
    print(f"status.json 呼叫間隔最大 : {stats([x.get('status_interval_ms_max') for x in wr])}")
    # Analysis Cache 命中（新欄位；Cache 上線前的舊 timing 沒有這些欄位 → 只標示，不影響其他判定）
    if any("status_cache_hits" in x for x in tim):
        sh = sum(x.get("status_cache_hits") or 0 for x in tim)
        sm = sum(x.get("status_cache_misses") or 0 for x in tim)
        ah = [x.get("analysis_cache_hit") for x in wr if x.get("analysis_cache_hit") is not None]
        print(f"analysis Cache（status）: Hit {sh}／Miss {sm}"
              + (f"（命中率 {sh / (sh + sm) * 100:.1f}%）" if sh + sm else ""))
        print(f"analysis Cache（每次取樣）: Hit {sum(ah)}／Miss {len(ah) - sum(ah)}（新取樣後應為 Miss＝實際重算）")
    else:
        print("analysis Cache          : 舊版 timing（無 Cache 欄位）")

    ivt, cyt = trend(iv), trend(cyc)
    rule("Actual Interval 中位數", f"{statistics.median(iv):.2f}s" if iv else "N/A",
         iv and statistics.median(iv) <= I + 2, f"≤ {I + 2:g}s")
    rule("Actual Interval P95", f"{q(iv, .95):.2f}s" if iv else "N/A", iv and q(iv, .95) <= I + 4, f"≤ {I + 4:g}s")
    rule("Actual Interval 最大", f"{max(iv):.2f}s" if iv else "N/A", iv and max(iv) <= I + 15, f"≤ {I + 15:g}s")
    rule("Interval 趨勢（後 1/3 − 前 1/3）", f"{ivt:+.2f}s" if ivt is not None else "N/A",
         ivt is not None and ivt <= 0.5, "≤ +0.5s")
    rule("Cycle 中位數", f"{statistics.median(cyc):.0f}ms", statistics.median(cyc) <= 1500, "≤ 1500ms")
    rule("Cycle P95", f"{q(cyc, .95):.0f}ms", q(cyc, .95) <= 3000, "≤ 3000ms")
    rule("Cycle 最大", f"{max(cyc):.0f}ms", max(cyc) < 10000, "< 10000ms")
    rule("Cycle 趨勢（後 1/3 − 前 1/3）", f"{cyt:+.0f}ms" if cyt is not None else "N/A",
         cyt is not None and cyt <= 200, "≤ +200ms")
    ws = [datetime.fromisoformat(x["wall_start"]) for x in tim]
    we = [datetime.fromisoformat(x["wall_end"]) for x in tim]
    overlap = sum(1 for k in range(1, len(tim)) if ws[k] < we[k - 1])
    rule("重疊 Cycle", overlap, overlap == 0, "= 0")
    an = [x.get("analysis_ms") for x in wr if x.get("analysis_ms") is not None]
    rule("analysis() P95（每次取樣）", f"{q(an, .95):.1f}ms" if an else "N/A（截圖關閉）",
         not an or q(an, .95) <= 100, "≤ 100ms")
    sti = [x.get("status_interval_ms_max") for x in wr if x.get("status_interval_ms_max")]
    rule("status.json 呼叫間隔最大", f"{max(sti):.0f}ms" if sti else "N/A", not sti or max(sti) <= 2000, "≤ 2000ms")

    n_obs = len(obs)
    hmi_miss = sum(1 for o in obs if not o.get("hmi_ok", True) or not o.get("ok"))
    pcs_miss = sum(1 for o in obs if (o.get("values") or {}).get("pcs_module_c") in (None, "", "-"))
    heat_miss = sum(1 for x in wr if x.get("heat_err"))
    fails = Counter()
    for x in wr:
        for f_ in x.get("hmi_fail") or []:
            fails[re.sub(r"^.*/", "", f_)] += 1
    e401 = sum(c for k, c in fails.items() if "_error401" in k)
    timeouts = sum(c for k, c in fails.items() if "Timeout" in k) + sum(1 for x in wr if "Timeout" in str(x.get("heat_err")))
    print(f"\nObservation {n_obs} 筆；Timing Cycle {len(tim)} 筆（寫入 {len(wr)}，未寫入 {len(tim) - len(wr)}；"
          f"其中 Menu 22 最後取樣 {n_final}）")
    print(f"HMI 端點失敗（Cycle 次數，依端點）：{dict(fails) or '無'}")
    rule("Observation Count == 寫入 Cycle Count", f"{n_obs} / {len(wr)}",
         n_obs == len(wr) and [o["t"] for o in obs] == [x["obs_t"] for x in wr], "相等且逐筆對應")
    terr = max([x.get("timing_errors") or 0 for x in tim] or [0])
    rule("Timing Log Errors", terr, terr == 0, "= 0")
    rule("HMI Missing", f"{hmi_miss} ({hmi_miss / n_obs:.1%})", hmi_miss / n_obs <= 0.01, "≤ 1%")
    rule("PCS Missing", f"{pcs_miss} ({pcs_miss / n_obs:.1%})", pcs_miss / n_obs <= 0.05, "≤ 5%")
    rule("6161 Missing", f"{heat_miss} ({heat_miss / max(len(wr), 1):.1%})", heat_miss / max(len(wr), 1) <= 0.01, "≤ 1%")
    rule("Timeout（HMI＋6161）", timeouts, timeouts <= 1, "≤ 1 次")
    rule("HMI 401（Cycle 內授權端點 _error401）", e401, True, "記錄（與 log 對照）")

    lw = [(x.get("reader") or {}).get("lock_wait_ms") for x in wr]
    lw = [v for v in lw if v is not None]
    rule("等 _CLIENT_LOCK P95（與 AUTO 監看共用 client 的爭用）", f"{q(lw, .95):.0f}ms" if lw else "N/A",
         not lw or q(lw, .95) <= 2000, "≤ 2000ms")
    if a.log:
        t0s, t1s = t_first.strftime("%H:%M:%S"), t_last.strftime("%H:%M:%S")
        n401 = relog = invalid = tb = autofail = lines = 0
        for lp in a.log:
            cur = None                                   # 沒有時間戳的行沿用最近一個 [HH:MM:SS]
            for line in open(lp, encoding="utf-8", errors="replace"):
                m = re.match(r"\[(\d\d:\d\d:\d\d)\]", line)
                if m:
                    cur = m.group(1)
                if cur is None or not (t0s <= cur <= t1s):
                    continue
                lines += 1
                n401 += "_error401" in line
                relog += line.startswith("嘗試登入")
                invalid += "判定登入態可能失效" in line
                autofail += "讀取設備狀態失敗" in line or "authenticated API 讀取失敗" in line
                tb += line.startswith("Traceback")
        print(f"log（測試期間 {t0s}～{t1s}，{lines} 行）：401 行 {n401}、嘗試登入 {relog}、判定登入態失效 {invalid}、"
              f"AUTO 讀取失敗 {autofail}、Traceback {tb}")
        print("AUTO 監看間隔：無法由 log 量測（狀態行只在內容改變時印出）→ 以等 _CLIENT_LOCK 時間與 AUTO 讀取失敗評估")
        rule("Traceback（測試期間）", tb, tb == 0, "= 0")
        rule("判定登入態失效（測試期間）", invalid, invalid <= max(1, round(hours)), "≤ 每小時 1 次")
        rule("AUTO 讀取失敗行（測試期間）", autofail, autofail <= max(2, 2 * round(hours)), "≤ 每小時 2 行")
        rule("Re-login（嘗試登入，測試期間）", relog, True, "記錄（5 秒測試時與本基準比較：每小時 ≤ 基準 + 1）")
    else:
        print("（未提供 --log：401、Re-login、AUTO 讀取失敗未評估）")

    if a.proc and os.path.exists(a.proc):
        rows = [l.strip().split(",") for l in open(a.proc, encoding="utf-8-sig") if l.strip()][1:]
        pts = [(datetime.fromisoformat(r[0]), float(r[1]), float(r[2])) for r in rows if len(r) >= 3]
        if len(pts) >= 2:
            span = (pts[-1][0] - pts[0][0]).total_seconds()
            cpu = (pts[-1][1] - pts[0][1]) / span * 100 if span else 0
            mem0, mem1, memx = pts[0][2], pts[-1][2], max(p[2] for p in pts)
            print(f"CPU（單核 %，平均）{cpu:.1f}%；記憶體 {mem0:.0f} → {mem1:.0f} MB（最大 {memx:.0f}）")
            rule("CPU 平均（單核）", f"{cpu:.1f}%", cpu <= 10, "≤ 10%")
            rule("記憶體成長", f"{mem1 - mem0:+.0f} MB", mem1 - mem0 <= 50, "≤ +50 MB")
    else:
        print("（未提供 --proc：CPU／記憶體未評估）")

    ob_b = os.path.getsize(os.path.join(a.folder, "observations.jsonl"))
    tm_b = os.path.getsize(os.path.join(a.folder, "cert_timing.jsonl"))
    print(f"JSONL 成長：observations {ob_b / 1024:.0f} KB（{ob_b / hours / 1e6:.2f} MB/小時）、"
          f"cert_timing {tm_b / 1024:.0f} KB（{tm_b / hours / 1e6:.2f} MB/小時）")

    print("\n== 判定 ==")
    for name, val, crit, ok in res:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {val}（{crit}）")
    allok = all(r[3] for r in res)
    print(f"\n{I:g} sec Baseline：{'PASS' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
