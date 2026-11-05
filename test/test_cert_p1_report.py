# -*- coding: utf-8 -*-
"""
認證測試報告 P1 —— Excel 報告格式離線驗證
==========================================
以合成資料產生報告到暫存資料夾（測試結束即刪除），檢查：
頁籤順序與動態數量、欄位、Excel Table 樣式、圖表（系列、缺值斷線、plotVisOnly、h:mm）、
Summary 蛇形圖表＋真正箭頭（休息時間）、圖表下方不放表格與說明、格線淡灰、待截圖標示（不含任何圖片）、N/A 與有效 0、
Raw Data 可追溯、操作人員確認不改寫原始資料、重複產生結果一致。
不連設備、不讀寫單次報告的任何檔案。
執行：python test_cert_p1_report.py
"""

import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

import cert_report_config as CFG
from cert_stage_engine import Obs, analyze
from cert_report_xlsx import (LOG_HEADERS, arrow_text, build_workbook, overall_title, rest_minutes_text, save_report,
                              summary_titles)

_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


T0 = datetime(2026, 1, 5, 10, 0, 0)
CUR = {"D": -140.0, "C": 135.0, "I": 0.0}


def obs_at(sec, k, **v):
    t = T0 + timedelta(seconds=sec)
    if k == "X":
        return Obs(t=t, ok=False, fail_reason="模擬讀取失敗", src=f"syn:{sec}")
    vals = {"soc": 50, "current_a": CUR[k], "rack_max_c": 40 + sec / 600, "rack_min_c": 30,
            "voltage_v": 1200.0, "sensor_c": 25.0, "pcs_module_c": 60.0, "power_kw": CUR[k] * 1.2}
    if k in ("D", "C"):                               # 充放電段有 Cell 電壓（原始 4 位小數，確認不被降精度）；
        vals.update(cell_vmax_v=3.4015, cell_vmin_v=3.3852)   # 休息段模擬舊資料沒有此欄
    vals.update(v)
    return Obs(t=t, values=vals, src=f"syn:{sec}")


def scenario():
    o = []
    for s in range(0, 1201, 10):                       # 10:00～10:20 放電，10:10:10～10:10:50 停頓（10:10 最後一筆為停頓）
        if 610 <= s <= 650:
            o_ = obs_at(s, "I", cell_vmax_v=3.4015, cell_vmin_v=3.3852)
            o_.flags = {"battery_power_status": "已上電"}  # 停頓期間電池已上電 → 狀態可判定（待機）、必要欄位齊全
            o.append(o_)
        elif 300 <= s <= 359:
            o.append(obs_at(s, "D", sensor_c=None))     # 10:05 整分鐘傳感器無值 → N/A
        elif 900 <= s <= 959:
            o.append(obs_at(s, "D", soc=0))              # 10:15 整分鐘 SOC 有效 0
        elif 1080 <= s <= 1139:
            o.append(obs_at(s, "D", pcs_module_c=None))  # 10:18 整分鐘 PCS 模組溫度無值 → 一般欄位無完整有效取樣
        else:
            o.append(obs_at(s, "D"))
    o += [obs_at(s, "I") for s in range(1210, 1501, 10)]                 # 休息
    for s in range(1510, 2401, 10):                                       # 10:25:10～10:40 充電
        o.append(obs_at(s, "X") if 1800 <= s <= 1859 else                 # 10:30:00～10:30:50 讀取失敗
                 obs_at(s, "C", cell_vmax_v=None, cell_vmin_v=None) if 2100 <= s <= 2159 else  # 10:35 整分鐘舊資料沒有 Cell 電壓
                 obs_at(s, "C"))
    o += [obs_at(s, "I") for s in range(2410, 2521, 10)]
    o += [obs_at(s, "D") for s in range(2530, 2571, 10)]                 # 10:42:10～10:42:50 短放電
    o += [obs_at(s, "I") for s in range(2580, 2701, 10)]
    return o


META = {"conditions": ["冷氣溫度：測試", "硬體概述：測試", "測試條件：測試"],
        "source_notes": ["合成資料"], "generated_at": "2026-01-05 12:00:00",
        "cabinet_spec": "250KW", "cycles": 2}         # 刻意不用 340KW：確認規格來自測試資訊、沒有寫死

tmp = tempfile.mkdtemp(prefix="cert_p1_")
try:
    p = CFG.params()
    res = analyze(scenario(), p)
    path = save_report(res, META, os.path.join(tmp, "r.xlsx"))
    wb = load_workbook(path)

    print("\n== 頁籤與階段 ==")
    check("階段判定：第1次放電（含 1 次確定停頓）、第1次充電（不確定 1）、第2次放電（短）",
          [st.dir for st in res.stages] == ["D", "C", "D"]
          and [len(st.pauses) for st in res.stages] == [1, 1, 0]
          and res.stages[0].pauses[0].certain and not res.stages[1].pauses[0].certain)
    check("頁籤順序：Summary／數據概覽／即時熱度／各階段（動態 3 個）／整體測試／Raw Data（不含控制紀錄）",
          wb.sheetnames == ["Summary", "數據概覽", "即時熱度", "1.第1次放電", "2.第1次充電",
                            "3.第2次放電", "整體測試", "Raw Data"])
    check("報告不建立「控制紀錄」頁（含 commands 有資料時）",
          "控制紀錄" not in wb.sheetnames
          and "控制紀錄" not in build_workbook(res, dict(META, commands=[
              {"t": "2026-01-01T00:00:00", "action": "pcs_stop_power", "control_success": True}])).sheetnames)

    print("\n== 階段頁 ==")
    ws = wb["1.第1次放電"]
    COL = {h: i for i, h in enumerate(LOG_HEADERS, 1)}         # 欄名 → 欄號（不寫死欄位字母）
    NOTE = COL["備註"]
    check("欄位 A～K：時間、SOC、電流、Rack 最大／最小溫度、Cell 最大／最小電壓、總電壓、傳感器溫度、PCS 模組溫度、備註",
          [ws.cell(1, c).value for c in range(1, 12)] == ["時間", "SOC", "電流", "Rack 最大溫度", "Rack 最小溫度",
                                                         "Cell 最大電壓", "Cell 最小電壓", "總電壓", "傳感器溫度",
                                                         "PCS 模組溫度", "備註"] == LOG_HEADERS)
    times = [ws.cell(r, 1).value for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value]
    check("每分鐘一列，只含階段範圍內的整分（10:00～10:20，21 列）",
          len(times) == 21 and times[0] == T0 and times[-1] == T0 + timedelta(minutes=20))
    check("時間欄格式 h:mm（不新增取樣時間欄）",
          ws.cell(2, 1).number_format == "h:mm" and ws.max_column <= 29)
    tbl = list(ws.tables.values())[0]
    check("Excel Table A1:K22（含備註欄）、樣式 TableStyleMedium7（同範本）",
          tbl.ref == "A1:K22" and tbl.tableStyleInfo.name == "TableStyleMedium7")
    cvx, cvn = ws.cell(2, COL["Cell 最大電壓"]), ws.cell(2, COL["Cell 最小電壓"])
    check(f"Cell 最大／最小電壓：原始精度保留（{cvx.value}／{cvn.value} V）、顯示格式 0.000、不是總電壓",
          cvx.value == 3.4015 and cvn.value == 3.3852 and cvx.number_format == "0.000"
          and cvn.number_format == "0.000" and ws.cell(2, COL["總電壓"]).value == 1200.0)
    check("Cell 電壓欄寬設定一致（F、G）",
          ws.column_dimensions["F"].width == ws.column_dimensions["G"].width >= 14)

    def row_of(ws, hhmm):
        for r in range(2, ws.max_row + 1):
            v = ws.cell(r, 1).value
            if isinstance(v, datetime) and v.strftime("%H:%M") == hhmm:
                return r
    r10 = row_of(ws, "10:10")
    check("停頓資料照實保留，階段頁備註保留分析標記並附設備狀態（電池已上電 →「停頓（待機）」）",
          str(ws.cell(r10, NOTE).value).startswith("停頓（待機）")
          and ws.cell(r10, 3).value == 0)
    r05 = row_of(ws, "10:05")
    check("傳感器無效 → 顯示 N/A；圖表輔助欄為真正空白（不畫成 0）",
          ws.cell(r05, COL["傳感器溫度"]).value == "N/A" and ws.cell(r05, 29).value is None)
    r15 = row_of(ws, "10:15")
    check("有效 0 照實保留（SOC=0 為數值 0，不是 N/A）", ws.cell(r15, 2).value == 0)
    r18 = row_of(ws, "10:18")
    _gen = [COL[h] for h in ("SOC", "電流", "總電壓", "傳感器溫度", "PCS 模組溫度")]
    _ext = [COL[h] for h in ("Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓")]
    check("該分鐘 PCS 模組溫度全部無值（6161 缺值、HMI 正常）→ 只有 PCS N/A；SOC／電流／總電壓／傳感器照實取該分鐘最後一筆"
          "（10:18:50，不虛構、不沿用鄰近值）；極值欄位照實取分鐘 MAX／MIN（Policy B）",
          ws.cell(r18, COL["PCS 模組溫度"]).value == "N/A"
          and [ws.cell(r18, COL[h]).value for h in ("SOC", "電流", "總電壓", "傳感器溫度")] == [50, CUR["D"], 1200.0, 25.0]
          and abs(ws.cell(r18, _ext[0]).value - (40 + 1130 / 600)) < 1e-9
          and [ws.cell(r18, c).value for c in _ext[1:]] == [30, 3.4015, 3.3852]
          and "無完整有效取樣" not in str(ws.cell(r18, NOTE).value) and ws.cell(2, COL["PCS 模組溫度"]).value == 60.0)
    pair_ok = True
    for r in range(2, ws.max_row + 1):
        for vis, hid in ((COL["Rack 最大溫度"], 27), (COL["Rack 最小溫度"], 28), (COL["傳感器溫度"], 29)):
            v, h = ws.cell(r, vis).value, ws.cell(r, hid).value
            pair_ok &= (h is None) if v == "N/A" else (h == v)
    check("圖表輔助欄 AA／AB／AC 逐列等於 Rack 最大溫度（D）／Rack 最小溫度（E）／傳感器溫度（I）（N/A → 空白），"
          "不含電壓", pair_ok and [ws.cell(1, c).value for c in (27, 28, 29)]
          == ["（圖表用）Rack 最大溫度", "（圖表用）Rack 最小溫度", "（圖表用）傳感器溫度"])
    check("圖表輔助欄 AA～AC 隱藏",
          all(ws.column_dimensions[c].hidden for c in ("AA", "AB", "AC")))
    _anc = [c.anchor._from.col for c in wb["1.第1次放電"]._charts + wb["整體測試"]._charts]
    check(f"階段頁／整體頁圖表在備註欄（K）右側、不遮資料（起始欄 {[get_column_letter(c + 1) for c in _anc]}）",
          _anc and all(c >= NOTE for c in _anc))
    check("階段頁各 1 張圖表（含不足一分鐘的短階段）",
          all(len(wb[n_]._charts) == 1 for n_ in ("1.第1次放電", "2.第1次充電", "3.第2次放電")))
    ws3 = wb["3.第2次放電"]
    check("短階段（10:42:10～10:42:50，不足一分鐘）：Calendar Minute 仍有所在分鐘 10:42 一列（取 10:42:50），並畫圖",
          ws3.max_row == 2 and ws3.cell(2, 1).value == T0 + timedelta(minutes=42) and len(ws3._charts) == 1
          and ws3.cell(2, COL["SOC"]).value == 50)
    ws2 = wb["2.第1次充電"]
    r30 = row_of(ws2, "10:30")
    check("該分鐘內只有讀取失敗 → 整列 N/A 並寫明原因（不跨分鐘取 10:29:50／10:31:00）",
          all(ws2.cell(r30, c).value == "N/A" for c in range(2, NOTE))
          and "該分鐘內讀取失敗" in str(ws2.cell(r30, NOTE).value))
    r35 = row_of(ws2, "10:35")
    check("該分鐘只有舊資料（沒有 Cell 電壓）→ 一般欄位與 Cell 電壓 N/A＋原因「無完整有效取樣」（不以總電壓代替、不補值）；"
          "Rack 溫度本身有效 → 照實取分鐘極值",
          all(ws2.cell(r35, c).value == "N/A" for c in _gen + [COL["Cell 最大電壓"], COL["Cell 最小電壓"]])
          and abs(ws2.cell(r35, COL["Rack 最大溫度"]).value - (40 + 2150 / 600)) < 1e-9
          and ws2.cell(r35, COL["Rack 最小溫度"]).value == 30
          and "無完整有效取樣" in str(ws2.cell(r35, NOTE).value))

    print("\n== 整體頁 ==")
    wa = wb["整體測試"]
    rr = row_of(wa, "10:22")
    check("整體頁休息期間：資料沒有保存電池上下電狀態 → 備註 N/A（不以電流 0／idle 推測為待機）",
          wa.cell(rr, NOTE).value == "N/A")
    check("整體頁同一欄位配置（含 Cell 電壓與 0.000 格式）",
          [wa.cell(1, c).value for c in range(1, 12)] == LOG_HEADERS
          and wa.cell(2, COL["Cell 最大電壓"]).number_format == "0.000")
    check("整體頁含全部時間（10:00～10:45）",
          wa.cell(2, 1).value == T0 and row_of(wa, "10:45") is not None)
    no_hmi = dict(soc=None, current_a=None, rack_max_c=None, rack_min_c=None, voltage_v=None, sensor_c=None,
                  cell_vmax_v=None, cell_vmin_v=None, power_kw=None, pcs_module_c=80)
    o_nh = obs_at(70, "D", **no_hmi)                  # 舊資料型態：ok=True、沒有 hmi_ok 欄位、HMI 全無值
    o_nh.notes = ["HMI 讀取失敗：無讀值（未登入或連線失敗）"]
    o_nh2 = obs_at(95, "D", **no_hmi)                 # 10:01 整分鐘只有 HMI 無讀值觀測
    o_part = obs_at(310, "D", soc=None, current_a=None, voltage_v=None, cell_vmax_v=None, cell_vmin_v=None)
    o_pcs = obs_at(330, "D", pcs_module_c="-")        # 現場型態：6161 回傳 "-"（HMI 正常）
    o_sen = obs_at(350, "D", sensor_c=None)            # 傳感器關閉（選用欄位）
    nh = analyze([obs_at(s, "D") for s in range(0, 51, 10)] + [o_nh, o_nh2]
                 + [obs_at(s, "D") for s in range(120, 301, 10)] + [o_part] + [obs_at(320, "D"), o_pcs,
                                                                                obs_at(340, "D"), o_sen]
                 + [obs_at(s, "D") for s in range(360, 401, 10)], p)
    wb_nh = load_workbook(save_report(nh, META, os.path.join(tmp, "nohmi.xlsx")))
    wn = wb_nh["整體測試"]
    rw = wb_nh["Raw Data"]
    RH = [c.value for c in rw[1]]
    raw_by_t = {r_[RH.index("時間")]: r_ for r_ in rw.iter_rows(min_row=2, values_only=True)}
    from cert_report_xlsx import RAW_REQUIRED
    from cert_stage_engine import _num, has_hmi_values
    raw_obs_rows = [r_ for r_ in rw.iter_rows(min_row=2, values_only=True) if r_[RH.index("序號")] is not None]
    check("Raw Data 必要欄位 = SOC／電流／Rack 最大／Rack 最小／Cell 最大／Cell 最小／總電壓／功率（全部 HMI；"
          "傳感器、PCS 模組溫度＝6161 選用）",
          RAW_REQUIRED == ["soc", "current_a", "rack_max_c", "rack_min_c", "cell_vmax_v", "cell_vmin_v", "voltage_v",
                           "power_kw"])
    check("11 Raw Data Filter：ok=True 但 HMI 全無值（僅 6161 PCS 模組溫度 80 有效）→ 整筆不輸出（不是改空白）",
          o_nh.t not in raw_by_t and not any("無有效讀值" in str(v) for r_ in raw_obs_rows for v in r_))
    check("12 HMI 有效但必要欄位缺值（SOC／電流／電壓／Cell 電壓 N/A）→ 整筆不輸出", o_part.t not in raw_by_t)
    rp_ = raw_by_t.get(o_pcs.t)
    check("12 HMI 有效、只有 PCS 模組溫度為 \"-\"（6161 未給值）→ 整筆照常輸出，PCS 顯示 N/A，HMI 數值照實",
          rp_ is not None and rp_[RH.index("PCS 模組溫度")] == "N/A" and rp_[RH.index("SOC")] == 50
          and rp_[RH.index("電流")] == CUR["D"] and rp_[RH.index("序號")] == o_pcs.seq + 1)
    _p, _n = raw_by_t.get(o_pcs.t - timedelta(seconds=10)), raw_by_t.get(o_pcs.t + timedelta(seconds=10))
    check("13/15 前後 PCS 模組溫度有效（60）→ 保留；PCS 缺值那筆不以前後值補入（仍為 N/A）",
          _p is not None and _n is not None and _p[RH.index("PCS 模組溫度")] == 60.0
          and _n[RH.index("PCS 模組溫度")] == 60.0 and rp_ is not None and rp_[RH.index("PCS 模組溫度")] == "N/A")
    rs_ = raw_by_t.get(o_sen.t)
    check("14 傳感器（選用）N/A、其他必要欄位齊全 → 整筆保留、傳感器顯示 N/A",
          rs_ is not None and rs_[RH.index("序號")] == o_sen.seq + 1 and rs_[RH.index("傳感器溫度")] == "N/A")
    expect = [o for o in nh.obs if o.ok and has_hmi_values(o)
              and all(_num(o.values.get(k)) is not None for k in RAW_REQUIRED)]
    check(f"Raw Data 觀測列數 = HMI 有效且必要欄位齊全的觀測數（{len(raw_obs_rows)} = {len(expect)}；全部 {len(nh.obs)}），"
          "沒有任何必要（HMI）欄位為 N/A、不留空白列",
          len(raw_obs_rows) == len(expect) == len(nh.obs) - 3
          and all(r_[0] is not None and r_[1] is not None for r_ in raw_obs_rows)
          and not any(r_[RH.index(h)] == "N/A" for r_ in raw_obs_rows
                      for h in ("SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓",
                                "總電壓", "功率 kW")))
    seqs = [r_[RH.index("序號")] for r_ in raw_obs_rows]
    check("17 Traceability：序號沿用原觀測 seq（跳過被排除者、不重新編號），序號＝observations.jsonl 列號（內部 src 仍保留）",
          o_nh.seq + 1 not in seqs and o_nh2.seq + 1 not in seqs and o_nh.seq in seqs and o_nh2.seq + 2 in seqs
          and o_part.seq + 1 not in seqs and o_part.seq in seqs and o_pcs.seq + 1 in seqs
          and all(nh.obs[r_[0] - 1].t == r_[RH.index("時間")]
                  and nh.obs[r_[0] - 1].src for r_ in raw_obs_rows))
    check("1/3/4 Raw Data 欄位：不含「來源」「讀取結果」「所屬階段」「對應報表分鐘」，「PC 取樣時間」改名「時間」，固定 14 欄 A～N",
          RH == ["序號", "時間", "狀態", "SOC", "電流",
                 "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓", "總電壓", "傳感器溫度",
                 "PCS 模組溫度", "功率 kW", "說明"]
          and "讀取結果" not in RH and "PC 取樣時間" not in RH and "所屬階段" not in RH and "對應報表分鐘" not in RH)
    _tc = [r_[1] for r_ in rw.iter_rows(min_row=2)]
    check("Raw Data「時間」欄（B）＝PC 取樣時間原值：datetime 含秒、格式 yyyy-mm-dd hh:mm:ss；凍結首列、篩選 A1:N",
          _tc and all(isinstance(c_.value, datetime) and c_.number_format == "yyyy-mm-dd hh:mm:ss" for c_ in _tc)
          and any(c_.value.second for c_ in _tc) and rw.freeze_panes == "A2"
          and rw.auto_filter.ref == f"A1:N{rw.max_row}" and rw.column_dimensions["B"].width == 20)
    check("2 Raw Data 任何儲存格都不出現 observations.jsonl:*（來源只留在程式內部）",
          not any("observations.jsonl" in str(v) for r_ in rw.iter_rows(values_only=True) for v in r_ if v is not None))
    print("\n== Raw Data 狀態 ==")
    BP_ON = {"battery_power_status": "已上電", "pcs_fault_flag": False}

    def obs_f(sec, k, flags, **v):
        o_ = obs_at(sec, k, **dict({"cell_vmax_v": 3.40, "cell_vmin_v": 3.38}, **v))   # 必要欄位齊全，只測狀態
        o_.flags = dict(flags)
        return o_
    s_d = [obs_f(s_, "D", dict(BP_ON, pcs_discharging_flag=True)) for s_ in range(0, 301, 10)]
    s_on = obs_f(400, "I", BP_ON, soc=100)                                        # SOC 100 不代表滿充
    s_off = obs_f(410, "I", {"battery_power_status": "已下電", "pcs_fault_flag": False})
    s_sen = obs_f(420, "I", BP_ON, sensor_c=None)                                 # 選用傳感器 N/A
    s_unk = obs_f(430, "I", {"battery_power_status": "未知", "pcs_fault_flag": False})
    s_flt = obs_f(440, "I", {"battery_power_status": "已下電", "pcs_fault_flag": True})
    s_lag = obs_f(450, "I", dict(BP_ON, pcs_discharging_flag=True))              # 停止後旗標未歸零
    s_zero = obs_f(460, "I", {}, soc=0, current_a=0.0, power_kw=0.0)             # 無上下電資料；SOC／電流／功率皆 0
    s_c = [obs_f(s_, "C", dict(BP_ON, pcs_charging_flag=True)) for s_ in range(700, 1001, 10)]
    idle = [obs_f(s_, "I", BP_ON) for s_ in range(310, 400, 10)] + [s_on, s_off, s_sen, s_unk, s_flt, s_lag, s_zero]         + [obs_f(s_, "I", BP_ON) for s_ in range(470, 700, 10)]
    rs = analyze(s_d + idle + s_c, p)
    wb_s = load_workbook(save_report(rs, META, os.path.join(tmp, "status.xlsx")))
    rws_ = wb_s["Raw Data"]
    SH = [c.value for c in rws_[1]]
    st_by_t = {r_[SH.index("時間")]: r_ for r_ in rws_.iter_rows(min_row=2, values_only=True)
               if r_[SH.index("序號")] is not None}
    stv = lambda o_: (st_by_t.get(o_.t) or [None] * len(SH))[SH.index("狀態")]
    check("1/2 階段內充電／放電 → 第1次充電／第1次放電（依階段，非依數值）",
          all(stv(o_) == "第1次放電" for o_ in s_d) and all(stv(o_) == "第1次充電" for o_ in s_c[1:-1]))
    check("3/4 階段外：電池已上電 → 待機、已下電 → 停機", stv(s_on) == "待機" and stv(s_off) == "停機")
    check("5 狀態欄不出現 N/A（可判定者都顯示正確狀態）",
          "N/A" not in {r_[SH.index("狀態")] for r_ in st_by_t.values()})
    check("6 上下電未知／停止後充放電旗標未歸零 → 狀態無法判定 → 整筆不輸出（Unknown ≠ Fault）",
          all(o_.t not in st_by_t for o_ in (s_unk, s_lag)))
    check("6b PCS 故障（設備明確回報）→ 狀態「故障」、照常輸出（數值為原始 observation）",
          s_flt.t in st_by_t and st_by_t[s_flt.t][SH.index("狀態")] == "故障"
          and st_by_t[s_flt.t][SH.index("SOC")] == s_flt.values["soc"])
    check("7/8/9 不以數值猜狀態：SOC 0、電流 0、功率 0 但沒有上下電資料 → 不判為待機／停機（不輸出）；SOC 100 → 待機（非滿充）",
          s_zero.t not in st_by_t and stv(s_on) == "待機")
    check("12 選用傳感器 N/A 不影響狀態：已上電 → 待機、整筆保留", stv(s_sen) == "待機"
          and st_by_t[s_sen.t][SH.index("傳感器溫度")] == "N/A")
    seq_s = [r_[0] for r_ in st_by_t.values()]
    check("15/16 序號沿用原 seq（被排除者跳號、不重新編號），序號指回原觀測",
          all(o_.seq + 1 not in seq_s for o_ in (s_unk, s_lag, s_zero)) and s_flt.seq + 1 in seq_s
          and s_sen.seq + 1 in seq_s and s_zero.seq + 2 in seq_s
          and all(rs.obs[r_[0] - 1].t == r_[SH.index("時間")] for r_ in st_by_t.values()))
    check("13 狀態欄 Filter 不改 Stage Detection（同資料去掉 flags 重新分析，階段與 state 相同）",
          [(st.start.t, st.A.t, st.end_by) for st in rs.stages]
          == [(st.start.t, st.A.t, st.end_by) for st in analyze(
              [obs_at(int((o_.t - T0).total_seconds()), {"C": "C", "D": "D"}.get(o_.direction, "I"),
                      **o_.values) for o_ in rs.obs], p).stages])

    rn = row_of(wn, "10:01")
    check("整體頁：該分鐘內只有 HMI 無讀值觀測 → HMI 欄位 N/A、保留 6161 溫度，備註寫明原因（不只寫 N/A）",
          wn.cell(rn, COL["SOC"]).value == "N/A" and wn.cell(rn, COL["PCS 模組溫度"]).value == 80
          and wn.cell(rn, NOTE).value == "N/A：HMI 無有效讀值（通訊異常或未登入），僅有 6161 溫度")

    print("\n== Summary ==")
    ss = wb["Summary"]
    check("A1～A3 測試條件、A4「溫度曲線：」", ss["A4"].value == "溫度曲線：" and ss["A1"].value)
    anchors = [c.anchor._from.row for c in ss._charts]
    check("Summary 圖表 4 張（3 個階段 ＋ 整體；不足一分鐘的短階段也有所在分鐘一列 → 有圖）",
          len(ss._charts) == 4 and not any("不足一分鐘" in str(c.value) for row in ss.iter_rows()
                                           for c in row if c.value))
    below = [c for row in ss.iter_rows() for c in row
             if c.value is not None and c.row >= max(anchors) + 1]
    check("Summary 最後一張圖表下方沒有任何儲存格內容（無階段表、明細、參數、說明）", not below)
    texts = " ".join(str(c.value) for row in ss.iter_rows() for c in row if c.value)
    check("Summary 不含階段表／停頓明細／確認紀錄／名詞參數說明",
          not any(k in texts for k in ("階段表", "停頓與", "確認紀錄", "名詞與判定前提",
                                       "暫用（未定案）", "資料來源與限制")))
    item = res.stages[1].pauses[0].item_id
    check("箭頭不再用文字儲存格（→／←／↓ 間隔）",
          not any(k in texts for k in ("→", "←", "↓", "間隔")))

    with zipfile.ZipFile(path) as z:
        dr = z.read("xl/drawings/drawing1.xml").decode("utf-8")
    anchors_x = re.findall(r"<(?:xdr:)?twoCellAnchor[^>]*>(.*?)</(?:xdr:)?twoCellAnchor>", dr, re.S)

    def box(a):
        n = [int(v) for v in re.findall(r"<(?:xdr:)?(?:col|row)>(\d+)<", a)]
        return n[0], n[1], n[2], n[3]          # from col, from row, to col, to row
    shapes = [(re.search(r'prst="(\w+)"', a).group(1), box(a),
               re.findall(r"<a:t>([^<]*)</a:t>", a)) for a in anchors_x if "prstGeom" in a]
    frames = [box(a) for a in anchors_x if "graphicFrame" in a]
    check("Summary 有真正的箭頭圖形：第1→2 向右、第2→3 向下（蛇形）",
          [s[0] for s in shapes] == ["rightArrow", "downArrow"])
    w1, w2, w3 = wb["1.第1次放電"], wb["2.第1次充電"], wb["3.第2次放電"]
    last1, first2 = w1.cell(w1.max_row, 1).value, w2.cell(2, 1).value
    last2, first3 = w2.cell(w2.max_row, 1).value, w3.cell(2, 1).value
    exp1 = f"{int((first2 - last1).total_seconds() // 60)} 分"
    exp2 = f"{int((first3 - last2).total_seconds() // 60)} 分"
    check(f"箭頭 = 下一張圖第一個 Category − 上一張圖最後一個 Category（{last1:%H:%M} → {first2:%H:%M} ＝「{exp1}」、"
          f"{last2:%H:%M} → {first3:%H:%M} ＝「{exp2}」；起始不完整分鐘已納入圖表）",
          [s[2] for s in shapes] == [[exp1], [exp2]] and (exp1, exp2) == ("5 分", "2 分"))
    sps = [a for a in anchors_x if "prstGeom" in a]
    rpr = [re.findall(r"<a:rPr ([^>]*)>", a) for a in sps]
    check("箭頭文字：單行、粗體、14pt（ARROW_TEXT_SZ）、置中（水平 algn=ctr、垂直 anchor=ctr）",
          len(rpr) == 2 and all(len(r_) == 1 and 'sz="1400"' in r_[0] and 'b="1"' in r_[0] for r_ in rpr)
          and all('algn="ctr"' in a and 'anchor="ctr"' in a for a in sps))
    hm_ = lambda h, m: datetime(2026, 10, 29, h, m)
    check("Category 為整分：10:37 → 10:40 ＝「3 分」、12:42 → 14:40 ＝「118 分」、16:45 → 16:50 ＝「5 分」",
          [arrow_text((None, a_), (b_, None)) for a_, b_ in ((hm_(10, 37), hm_(10, 40)), (hm_(12, 42), hm_(14, 40)),
                                                            (hm_(16, 45), hm_(16, 50)))]
          == [["3 分"], ["118 分"], ["5 分"]])
    check("任一側沒有圖表 → N/A；Category 時間倒置 → N/A（不產生負分鐘、不猜時間）",
          arrow_text(None, (hm_(10, 40), hm_(10, 50))) == ["N/A"]
          and arrow_text((hm_(10, 0), hm_(10, 37)), None) == ["N/A"]
          and arrow_text((hm_(10, 0), hm_(10, 40)), (hm_(10, 37), hm_(10, 50))) == ["N/A"])
    check("rest_minutes_text：None／負值 → N/A、0 → 0 分",
          rest_minutes_text(None) == "N/A" and rest_minutes_text(-30) == "N/A" and rest_minutes_text(0) == "0 分")
    from cert_report_xlsx import summary_axis_labels
    lab_t = [hm_(8, 35) + timedelta(minutes=k) for k in range(123)]          # 08:35～10:37（同 082805 第1張）
    lab = summary_axis_labels(lab_t)
    vis_ = [v for v in lab if v]
    check(f"Summary X 軸標籤：同階段頁 Excel 自動密度（123 筆 → 每 4 個：08:35、08:39…10:31），另強制顯示最後 10:37、"
          f"離尾端不到 4 個的 10:35 留空；筆數不變（{len(lab)}）",
          len(lab) == 123 and vis_[:3] == ["08:35", "08:39", "08:43"] and vis_[-2:] == ["10:31", "10:37"]
          and "10:35" not in vis_ and all(lab[i] for i in range(0, 120, 4)))
    l76 = summary_axis_labels([hm_(16, 50) + timedelta(minutes=k) for k in range(76)])
    l126 = summary_axis_labels([hm_(14, 40) + timedelta(minutes=k) for k in range(126)])
    check("共通規則 k = ceil(筆數 / 32)：76 筆 → 每 3 個（16:50、16:53…18:05，尾端剛好整除）；126 筆 → 每 4 個、強制 16:45、16:44 留空",
          [v for v in l76 if v][:2] == ["16:50", "16:53"] and l76[-1] == "18:05" and l76[-4] == "18:02"
          and [v for v in l126 if v][-2:] == ["16:40", "16:45"])
    ws_s = wb["Summary"]
    check("Summary 階段圖 tickLblSkip=1（標籤不被 Excel 自動省略）；Summary 整體圖不設定",
          [c_.x_axis.tickLblSkip for c_ in ws_s._charts] == [1, 1, 1, None])
    _rot = lambda c_: c_.x_axis.txPr.bodyPr.rot if c_.x_axis.txPr is not None else None
    check("Summary 四張階段圖 X 軸文字樣式一致：固定直式（rot=-5400000，同階段頁 Excel 自動呈現）、nextTo、10pt",
          [_rot(c_) for c_ in ws_s._charts[:2]] == [-5400000] * 2
          and [c_.x_axis.tickLblPos for c_ in ws_s._charts[:2]] == ["nextTo"] * 2
          and all(c_.x_axis.txPr.p[0].pPr.defRPr.sz == 1000 for c_ in ws_s._charts[:2]))
    check("Summary 整體圖與「整體測試」頁圖表維持原樣：不設文字樣式／tickLblSkip（方向與間隔由 Excel 自動）、類別仍參照 A 欄",
          all(c_.x_axis.txPr is None and c_.x_axis.tickLblPos is None and c_.x_axis.tickLblSkip is None
              and c_.series[0].cat.numRef is not None for c_ in [ws_s._charts[-1]] + wb["整體測試"]._charts))
    _cats = lambda c_: [v.v for v in c_.series[0].cat.strLit.pt]
    sh1, sh2 = wb["1.第1次放電"]._charts[0], wb["2.第1次充電"]._charts[0]
    check("1 Summary 階段圖與對應階段頁圖表使用同一 X 軸規則：標籤文字、直式、nextTo、10pt、tickLblSkip=1 全部相同",
          _cats(ws_s._charts[0]) == _cats(sh1) and _cats(ws_s._charts[1]) == _cats(sh2)
          and all(_rot(c_) == -5400000 and c_.x_axis.tickLblPos == "nextTo" and c_.x_axis.tickLblSkip == 1
                  and c_.x_axis.txPr.p[0].pPr.defRPr.sz == 1000 for c_ in (sh1, sh2)))
    check("5/6/7 階段頁圖表首尾標籤 = 階段頁 A 欄第一／最後 Category（10:00／10:20、10:25／10:40；充電 10:25:10 開始 → 10:25）",
          (_cats(sh1)[0], _cats(sh1)[-1]) == ("10:00", "10:20") and (_cats(sh2)[0], _cats(sh2)[-1]) == ("10:25", "10:40")
          and _cats(sh1)[-1] == f"{wb['1.第1次放電'].cell(wb['1.第1次放電'].max_row, 1).value:%H:%M}")
    gap_obs = ([obs_at(s, "D") for s in range(0, 1201, 10)]
               + [obs_at(s, "C") for s in range(5249, 5550, 10)])      # A=10:20:00，R=11:27:29，中間無取樣
    rg = analyze(gap_obs, p)
    wg = load_workbook(save_report(rg, META, os.path.join(tmp, "gap.xlsx")))
    with zipfile.ZipFile(os.path.join(tmp, "gap.xlsx")) as z:
        ag = re.findall(r"<a:t>([^<]*)</a:t>", z.read("xl/drawings/drawing1.xml").decode("utf-8"))
    g1, g2 = [wg[n] for n in wg.sheetnames if re.match(r"\d+\.", n)]
    check(f"沒有 end_B（直接換向）也一樣以圖表 Category 計算：{g1.cell(g1.max_row, 1).value:%H:%M} → "
          f"{g2.cell(2, 1).value:%H:%M} ＝ {ag}",
          rg.stages[0].end_B is None and ag == ["67 分"])
    from cert_stage_engine import overall_minutes as _om
    gap_rows = [m for m in _om(rg) if T0 + timedelta(minutes=21) <= m.T <= T0 + timedelta(minutes=86)]
    check("同一缺口在全程資料仍為 N/A（10:21～11:26 整分鐘無取樣；箭頭有間隔不代表補值或連線）",
          gap_rows and all(m.obs is None for m in gap_rows))

    def overlap(b1, b2):
        return not (b1[2] < b2[0] or b2[2] < b1[0] or b1[3] < b2[1] or b2[3] < b1[1])
    check("箭頭與任何圖表的儲存格範圍都不重疊",
          all(not overlap(s[1], f) for s in shapes for f in frames))
    def title_of(ch):
        return "".join(r.t for p_ in ch.title.tx.rich.p for r in (p_.r or []))
    check("Summary 圖表標題：250KW 第1次放電、250KW 第1次充電（電櫃規格前綴；未標記 → 只有方向）＋全程",
          [title_of(c) for c in ss._charts][:3] == ["250KW 第1次放電", "250KW 第1次充電", "250KW 第2次放電"]
          and title_of(ss._charts[-1]) == "【250KW】電櫃充放2個循環溫度曲線")
    check("整體溫度曲線標題：Summary 與「整體測試」頁一致＝「【250KW】電櫃充放2個循環溫度曲線」（規格取自測試資訊）",
          title_of(ss._charts[-1]) == title_of(wb["整體測試"]._charts[0]) == "【250KW】電櫃充放2個循環溫度曲線")
    check("階段頁圖表標題共用同一電櫃規格（250KW 第1次放電）",
          title_of(wb["1.第1次放電"]._charts[0]) == "250KW 第1次放電")
    check("未輸入循環數 → 不寫循環數（不自動推算）；未輸入規格 → 【電櫃規格未設定】",
          overall_title({"cabinet_spec": "340KW"}) == "【340KW】電櫃充放溫度曲線"
          and overall_title({}) == "【電櫃規格未設定】電櫃充放溫度曲線"
          and overall_title({"cabinet_spec": "340KW", "cycles": 3}) == "【340KW】電櫃充放3個循環溫度曲線")
    from openpyxl.cell.rich_text import CellRichText
    ws_rt = load_workbook(path, rich_text=True)["Summary"]
    bold_ok = True
    for a, lab_ in (("A1", "冷氣溫度："), ("A2", "硬體概述："), ("A3", "測試條件：")):
        v = ws_rt[a].value
        bold_ok &= (isinstance(v, CellRichText) and str(v.__getitem__(0).text) == lab_
                    and v[0].font.b and not v[1].font.b)
    check("Summary 冷氣溫度：／硬體概述：／測試條件： 標籤粗體、內容一般字（同範本）", bold_ok)
    def titled(plan, meta=None):
        """plan：[(方向, 首筆 SOC, 標記或 None)]；各階段 5 分鐘、中間待機 2 分鐘（≥ W，分成不同階段）。"""
        obs_, lab_, t = [], [], 0
        for k, soc0, w in plan:
            obs_ += [obs_at(t + s, k, soc=soc0 if s == 0 else 50) for s in range(0, 301, 10)]
            if w:
                lab_.append({"type": "stage_label", "stage_start": (T0 + timedelta(seconds=t)).strftime(
                    "%Y-%m-%dT%H:%M:%S"), "label": w, "at": "2026-01-05 12:00:00", "by": "tester"})
            obs_ += [obs_at(t + s, "I") for s in range(310, 431, 10)]
            t += 440
        r_ = analyze(obs_, p, lab_)
        assert [st.dir for st in r_.stages] == [k for k, _s, _w in plan]
        return list(summary_titles(r_.stages, meta).values())
    spec = {"cabinet_spec": "250KW"}
    check("Summary 圖表標題＝階段名稱＋操作人員標記：滿放／滿充／無標記／滿充 → 250KW 第1次放電（滿放）／第1次充電（滿充）／"
          "第2次放電／第2次充電（滿充）",
          titled([("D", 100, "滿放"), ("C", 0, "滿充"), ("D", 100, None), ("C", 0, "滿充")], spec)
          == ["250KW 第1次放電（滿放）", "250KW 第1次充電（滿充）", "250KW 第2次放電", "250KW 第2次充電（滿充）"])
    check("電櫃規格未輸入（無欄位／None／空白）→ 不加前綴，不顯示 None、N/A",
          all(titled([("D", 100, "滿放"), ("C", 0, None)], m_) == ["第1次放電（滿放）", "第1次充電"]
              for m_ in (None, {}, {"cabinet_spec": None}, {"cabinet_spec": "  "})))
    check("起始 SOC 不是 0／100（充電 SOC 1＋滿充、放電 SOC 99＋滿放）→ 仍依操作人員標記顯示（同階段頁）",
          titled([("C", 1, "滿充"), ("D", 99, "滿放")], spec) == ["250KW 第1次充電（滿充）", "250KW 第1次放電（滿放）"])
    check("首筆 SOC 缺值＋滿充／滿放標記 → 依標記顯示",
          titled([("C", None, "滿充"), ("D", None, "滿放")], spec) == ["250KW 第1次充電（滿充）", "250KW 第1次放電（滿放）"])
    check("無標記：起始 SOC 0／100 也不自動認定滿充／滿放 → 充電／放電",
          titled([("C", 0, None), ("D", 100, None)], spec) == ["250KW 第1次充電", "250KW 第1次放電"])
    check("標記為未滿／中止 → 照實顯示",
          titled([("C", 0, "未滿"), ("D", 100, "中止"), ("C", 0, "中止"), ("D", 100, "未滿")], spec)
          == ["250KW 第1次充電（未滿）", "250KW 第1次放電（中止）", "250KW 第2次充電（中止）", "250KW 第2次放電（未滿）"])
    check("標記方向不符（舊資料繞過防呆）→ 與階段頁相同，照實顯示該標記（不改寫、不推論）",
          titled([("D", 100, "滿充"), ("C", 0, "滿放")], spec) == ["250KW 第1次放電（滿充）", "250KW 第1次充電（滿放）"])
    check("次數依充／放電方向各自計數、不受標記影響：滿放／放電／滿充／滿放 → 第1／第2次放電、第1次充電、第3次放電",
          titled([("D", 100, "滿放"), ("D", 100, None), ("C", 0, "滿充"), ("D", 100, "滿放")], spec)
          == ["250KW 第1次放電（滿放）", "250KW 第2次放電", "250KW 第1次充電（滿充）", "250KW 第3次放電（滿放）"])
    sum_t = [ch.title.tx.rich.p[0].r[0].t for ch in wb["Summary"]._charts][:-1]       # 最後一張是全程圖
    stg_t = [wb[n]._charts[0].title.tx.rich.p[0].r[0].t for n in wb.sheetnames
             if re.match(r"\d+\.", n) and wb[n]._charts]
    check(f"整份報告：Summary 各階段圖表標題與各階段頁圖表標題逐一相同（{sum_t}）", sum_t == stg_t and len(sum_t) == 3)

    print("\n== 截圖頁 ==")
    sv = wb["數據概覽"].sheet_view
    check("數據概覽：切換到本頁時從頂端開始（topLeftCell=A1、選取 A1）；活頁簿預設頁仍為 Summary",
          sv.topLeftCell == "A1" and sv.selection[0].activeCell == "A1" and wb.active.title == "Summary")
    for name in ("數據概覽", "即時熱度"):
        w = wb[name]
        ph = [c.value for row in w.iter_rows() for c in row if c.value and "待截圖" in str(c.value)]
        check(f"{name}：每個階段 1 個「待截圖」標示（共 3），沒有任何圖片；循環數 2（循環測試）→ 不顯示「認證結束（最後狀態）」",
              len(ph) == 3 and not w._images and all("不使用範本" in v for v in ph)
              and not any("認證結束（最後狀態）" in str(c.value) for row in w.iter_rows() for c in row if c.value))
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        check("檔案內沒有任何圖片（xl/media）", not any(n.startswith("xl/media/") for n in names))
        charts = [n for n in names if re.match(r"xl/charts/chart\d+\.xml$", n)]
        xmls = [z.read(n).decode("utf-8") for n in charts]

    print("\n== 圖表 XML ==")
    check("圖表總數 3（階段頁，含不足一分鐘的短階段）＋1（整體頁）＋4（Summary）＝ 8", len(xmls) == 8)
    ok_all = True
    for x in xmls:
        x = re.sub(r"<(/?)c:", r"<\1", x)          # openpyxl 以預設命名空間輸出（無 c: 前綴）
        ok_all &= ('plotVisOnly val="0"' in x and 'dispBlanksAs val="gap"' in x
                   and x.count("<ser>") == 3 and "<lineChart>" in x
                   and re.search(r"\$D\$1.*\$E\$1.*\$I\$1", x, re.S) is not None
                   and "$F$" not in x and "$G$" not in x and "$H$" not in x
                   and re.search(r"\$AA\$2.*\$AB\$2.*\$AC\$2", x, re.S) is not None
                   and 'formatCode="h:mm"' in x and 'legendPos val="b"' in x
                   and '<majorUnit val="1"' in x and "<min " in x and "<max " in x
                   and re.search(r'<catAx>.*<axPos val="b"', x, re.S) is not None)
        gl = re.search(r"<majorGridlines>(.*?)</majorGridlines>", x, re.S)
        ok_all &= (gl is not None and 'schemeClr val="tx1"' in gl.group(1)
                   and 'lumMod val="15000"' in gl.group(1) and 'lumOff val="85000"' in gl.group(1))
        ymax = int(re.search(r'<max val="(\d+)"', x).group(1))
        ok_all &= ymax < 100                       # 溫度軸（防止誤接電壓等欄位）
    check("每張圖：折線、3 系列（名稱取 D1/E1/I1＝三條溫度，不引用 Cell 電壓 F/G、總電壓 H）、讀隱藏輔助欄、缺值斷線、plotVisOnly=0、"
          "h:mm、圖例在下、Y 軸手動範圍 majorUnit 1、水平格線淡灰（同範本 tx1 85%）", ok_all)

    print("\n== Raw Data 追溯 ==")
    rw = wb["Raw Data"]
    hdr = [c.value for c in rw[1]]
    rows = [[c.value for c in r_] for r_ in rw.iter_rows(min_row=2)]
    H = {h: i for i, h in enumerate(hdr)}                     # 欄名 → 索引（不寫死欄號）
    check("Raw Data：Rack 最小溫度後加入 Cell 最大／最小電壓，說明欄在最後（N，移除來源、讀取結果、所屬階段、對應報表分鐘）",
          hdr[H["Rack 最小溫度"]:H["Rack 最小溫度"] + 3] == ["Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓"]
          and hdr[-1] == "說明" and len(hdr) == 14 and rw.auto_filter.ref.startswith("A1:N"))
    _ok = [r for r in rows if r[H["序號"]] is not None and r[H["Cell 最大電壓"]] not in (None, "N/A")]
    check(f"Raw Data Cell 電壓保留原始值（{_ok[0][H['Cell 最大電壓']] if _ok else None}）",
          _ok and _ok[0][H["Cell 最大電壓"]] == 3.4015 and _ok[0][H["Cell 最小電壓"]] == 3.3852)
    obs_rows = [r for r in rows if r[H["序號"]] is not None]
    from cert_report_xlsx import _stage_title, raw_rows_obs, status_text as _status_text
    from cert_stage_engine import stage_names as _sn
    nf_ = {st.no: _stage_title(_sn(res.stages), st) for st in res.stages}
    check(f"Raw Data 只輸出有效報告資料：讀取失敗 5 筆、必要欄位缺值、狀態無法判定者整筆不輸出（觀測列 {len(obs_rows)}）；"
          "每一列都是取樣列（序號＝觀測列號），沒有 N/A 分鐘列",
          len(obs_rows) == len(rows) == len(raw_rows_obs(res, p, nf_)) and all(res.obs[r[H["序號"]] - 1].ok for r in obs_rows)
          and len(obs_rows) < len(res.obs) - 5)
    check("Raw Data「時間」保留實際取樣時間（含秒）", hdr[1] == "時間"
          and obs_rows[0][H["時間"]] == T0)
    _s05 = _stage_title(_sn(res.stages), res.stages[0])
    _r1005 = [r for r in obs_rows if r[H["時間"]] == T0 + timedelta(minutes=5, seconds=50)]
    check("不輸出分鐘對照：任何儲存格都沒有「階段頁「…」HH:MM（±N 秒）」「整體 HH:MM」「（N/A）」等對照文字；"
          "階段頁 10:05 選用的 10:05:50 仍在 Raw Data、狀態只顯示階段名稱",
          not any(re.search(r"（[+-]\d+ 秒）|階段頁「|整體 \d\d:\d\d|\d\d:\d\d（N/A）", str(v))
                  for r in rows for v in r if v is not None)
          and len(_r1005) == 1 and _r1005[0][H["狀態"]] == _s05)

    print("\n== Raw Data 永遠沒有「所屬階段」「對應報表分鐘」（0／1／多階段固定 14 欄 A～N；階段由「狀態」呈現）==")
    from cert_report_xlsx import RAW_HEADERS, RAW_WIDTHS

    def raw_layout(ws_):
        """表頭、欄名→欄字母、各欄寬度、篩選、凍結、時間欄格式、沒有序號空白的 N/A 分鐘列、欄數不超出。"""
        hd = [c_.value for c_ in ws_[1]]
        L = {h_: get_column_letter(i_) for i_, h_ in enumerate(hd, 1)}
        num_w = dict(zip(["SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓", "總電壓",
                          "傳感器溫度", "PCS 模組溫度", "功率 kW"], [7, 9, 13, 13, 13, 13, 9, 11, 13, 9]))
        w_ok = all(ws_.column_dimensions[L[h_]].width == dict(RAW_WIDTHS, **num_w)[h_] for h_ in hd)
        rows_ = list(ws_.iter_rows(min_row=2))
        t_ok = all(r_[hd.index("時間")].number_format == "yyyy-mm-dd hh:mm:ss"
                   and isinstance(r_[hd.index("時間")].value, datetime) for r_ in rows_)
        na_ = [r_ for r_ in rows_ if r_[0].value is None]
        cols_ok = ws_.max_column == len(hd)
        return hd, L, w_ok, t_ok, na_, cols_ok

    FIXED = ["序號", "時間", "狀態", "SOC", "電流", "Rack 最大溫度", "Rack 最小溫度",
             "Cell 最大電壓", "Cell 最小電壓", "總電壓", "傳感器溫度", "PCS 模組溫度", "功率 kW", "說明"]
    LETTERS = {"狀態": "C", "SOC": "D", "Cell 最大電壓": "H", "功率 kW": "M", "說明": "N"}

    def fixed_ok(ws_, hd_, L_):
        return (hd_ == FIXED == RAW_HEADERS and ws_.max_column == 14 and all(L_[k_] == v_ for k_, v_ in LETTERS.items())
                and ws_.auto_filter.ref == f"A1:N{ws_.max_row}" and ws_.freeze_panes == "A2")

    # Case C：多階段（第1次放電 → 第1次充電 → 第2次放電）
    hdB, LB, wB, tB, naB, fB = raw_layout(rw)
    nB = {st.no: _stage_title(_sn(res.stages), st) for st in res.stages}
    by_seq_B = {r[H["序號"]]: r for r in obs_rows}
    staged = [o for o in res.obs if o.stage_no and o.seq + 1 in by_seq_B]
    check(f"Case C 多階段（{len(res.stages)} 個）→ 仍固定 14 欄 A～N、沒有「所屬階段」「對應報表分鐘」；篩選 A1:N、凍結 A2、"
          "欄寬、時間格式全部正確；階段頁有 N/A 分鐘也不在 Raw Data 加 N/A 分鐘列",
          fixed_ok(rw, hdB, LB) and wB and tB and fB and not naB)
    # 階段前／階段間／階段後的待機（已上電、必要欄位齊全 → 會進 Raw Data）
    def idle_on(s_):
        o_ = obs_at(s_, "I", cell_vmax_v=3.40, cell_vmin_v=3.38)
        o_.flags = {"battery_power_status": "已上電", "pcs_fault_flag": False}
        return o_
    resB = analyze([idle_on(s_) for s_ in range(0, 121, 10)] + [obs_at(s_, "D") for s_ in range(130, 501, 10)]
                   + [idle_on(s_) for s_ in range(510, 701, 10)] + [obs_at(s_, "C") for s_ in range(710, 1101, 10)]
                   + [idle_on(s_) for s_ in range(1110, 1301, 10)], p)
    wbB = load_workbook(save_report(resB, META, os.path.join(tmp, "stage_idle.xlsx")))
    hB, LB2, wB2, tB2, _naB2, fB2 = raw_layout(wbB["Raw Data"])
    rB = {r_[0]: r_ for r_ in wbB["Raw Data"].iter_rows(min_row=2, values_only=True) if r_[0] is not None}
    stagedB = [o for o in resB.obs if o.stage_no and o.seq + 1 in rB]
    idleB = [o for o in resB.obs if not o.stage_no and o.seq + 1 in rB]
    first = [o for o in stagedB if o.stage_no == 1]
    check(f"Case B 有階段（放電＋充電，{len(resB.stages)} 個）→ 仍固定 14 欄 A～N、沒有「所屬階段」，格式正確",
          len(resB.stages) == 2 and fixed_ok(wbB["Raw Data"], hB, LB2) and wB2 and tB2 and fB2)
    check(f"Case B「狀態」照原規則呈現階段：階段內 {len(stagedB)} 筆＝第1次放電／第1次充電；"
          f"階段前／間／後的待機 {len(idleB)} 筆＝待機；數值欄未錯位",
          first and all(rB[o.seq + 1][hB.index("狀態")] == "第1次放電" for o in first)
          and all(rB[o.seq + 1][hB.index("狀態")] == "第1次充電" for o in stagedB if o.stage_no == 2)
          and len(idleB) >= 30 and all(rB[o.seq + 1][hB.index("狀態")] == "待機" for o in idleB)
          and all(rB[o.seq + 1][hB.index("SOC")] == 50 and rB[o.seq + 1][hB.index("時間")] == o.t for o in stagedB + idleB))
    staged = [o for o in staged if o.state in ("充電", "放電")]     # 階段內停頓依原規則顯示「待機」，不列入
    shown = [by_seq_B[o.seq + 1][H["狀態"]] for o in staged]
    check(f"Case C「狀態」依原規則區分各階段（{list(dict.fromkeys(shown))}），不會全部顯示成同一階段",
          list(dict.fromkeys(shown)) == [_status_text(o, nB) for o in
                                         (next(o for o in staged if o.stage_no == k_) for k_ in (1, 2, 3))]
          and len(set(shown)) == 3 and all(s_.startswith(("第1次放電", "第1次充電", "第2次放電")) for s_ in set(shown)))

    # Case A：整份沒有階段（Idle-only）；10:05 整分鐘無取樣 → 整體頁 N/A（Raw Data 只列取樣，不加 N/A 分鐘列）
    idle_obs = []
    for s in list(range(0, 300, 10)) + list(range(360, 601, 10)):
        o_ = obs_at(s, "I", cell_vmax_v=3.40, cell_vmin_v=3.38)
        o_.flags = {"battery_power_status": "已上電", "pcs_fault_flag": False}
        idle_obs.append(o_)
    res0 = analyze(idle_obs, p)
    wb0 = load_workbook(save_report(res0, META, os.path.join(tmp, "idle_only.xlsx")))
    rw0 = wb0["Raw Data"]
    hdA, LA, wA, tA, naA, fA = raw_layout(rw0)
    rows0 = [[c_.value for c_ in r_] for r_ in rw0.iter_rows(min_row=2)]
    obs0 = [r_ for r_ in rows0 if r_[0] is not None]
    check("Case A 整份沒有階段 → 同樣固定 14 欄 A～N、沒有「所屬階段」「對應報表分鐘」",
          not res0.stages and fixed_ok(rw0, hdA, LA))
    check("Case A 篩選 A1:N、凍結首列、各欄寬度依欄名、時間欄 datetime yyyy-mm-dd hh:mm:ss（含秒）",
          rw0.auto_filter.ref == f"A1:N{rw0.max_row}" and rw0.freeze_panes == "A2" and wA and tA)
    check(f"Case A 觀測列：序號／時間＝原觀測，狀態＝待機，數值欄對齊（{len(obs0)} 筆＝全部觀測）",
          len(obs0) == len(idle_obs) == len(rows0) and all(res0.obs[r_[0] - 1].t == r_[hdA.index("時間")] for r_ in obs0)
          and {r_[hdA.index("狀態")] for r_ in obs0} == {"待機"}
          and all(r_[hdA.index("SOC")] == 50 and r_[hdA.index("電流")] == 0 for r_ in obs0))
    _oa = wb0["整體測試"]
    _r05 = next(r_ for r_ in range(2, _oa.max_row + 1) if _oa.cell(r_, 1).value == T0 + timedelta(minutes=5))
    check("Case A 10:05 整分鐘無取樣：整體頁照常 N/A＋原因（無取樣）；Raw Data 不加 N/A 分鐘列（只列取樣）",
          not naA and fA and _oa.cell(_r05, 2).value == "N/A" and "無取樣" in str(_oa.cell(_r05, NOTE).value))

    print("\n== 操作人員確認與一致性 ==")
    dec = [{"item_id": item, "choice": "split", "at": "2026-01-05 12:10:00", "by": "tester"}]
    res2 = analyze(scenario(), p, dec)
    path2 = save_report(res2, META, os.path.join(tmp, "r_split.xlsx"))
    wb2 = load_workbook(path2)
    pz2 = [pz for pz in res2.pauses if pz.item_id == item]          # 拆分後不屬任何階段，但紀錄仍在
    check("選「拆分」→ 階段頁變為 4 個；底層紀錄仍保留系統原始判定與確認紀錄",
          len([n for n in wb2.sheetnames if re.match(r"\d+\.", n)]) == 4
          and len(pz2) == 1 and pz2[0].system_judgment.startswith("暫時保留同一階段")
          and pz2[0].decision and pz2[0].decision["choice"] == "split" and res2.decisions == dec)
    _rh = [c.value for c in wb["Raw Data"][1]]                     # 依欄名取原始觀測欄位（狀態／所屬階段會隨拆分改名，不比）
    _rc = [_rh.index(h) for h in ("序號", "時間")] + list(range(_rh.index("SOC"), _rh.index("說明")))
    raw1 = [[row[i].value for i in _rc] for row in wb["Raw Data"].iter_rows(min_row=2)]
    raw2 = [[row[i].value for i in _rc] for row in wb2["Raw Data"].iter_rows(min_row=2)]
    check("確認後 Raw Data 的原始觀測欄位（序號、時間、數值）完全相同", raw1 == raw2)
    path3 = save_report(analyze(scenario(), p), META, os.path.join(tmp, "r_again.xlsx"))
    wb3 = load_workbook(path3)
    same = all([[c.value for c in row] for row in wb[n].iter_rows()] ==
               [[c.value for c in row] for row in wb3[n].iter_rows()] for n in wb.sheetnames)
    check("同樣輸入重複產生 → 所有頁籤內容相同", same and wb.sheetnames == wb3.sheetnames)

    print("\n== Calendar Minute 彙整：一般欄位 = 分鐘最後一筆有效觀測、極值 = 分鐘 MAX／MIN（Policy B）==")
    import cert_report_xlsx as RXv
    from cert_stage_engine import stage_minutes as _sm, overall_minutes as _om, stage_names as _sn, minute_value as _mv

    ON = {"battery_power_status": "已上電"}
    FAULT = {"battery_power_status": "已下電", "pcs_fault_flag": True}

    def mk(sec, cur=-140.0, flags=None, **v):
        vals = {"soc": 80, "current_a": cur, "rack_max_c": 40, "rack_min_c": 30, "cell_vmax_v": 3.30,
                "cell_vmin_v": 3.28, "voltage_v": 910.0, "sensor_c": 25.0, "pcs_module_c": 60.0, "power_kw": cur * 0.9}
        vals.update(v)
        o_ = Obs(t=T0 + timedelta(seconds=sec), values=vals, src=f"syn:{sec}")
        o_.flags = dict(flags or {})
        return o_
    REST = dict(rack_max_c=20, rack_min_c=10)                           # 休息資料極值刻意更低／更高，混入即可被偵測
    ag = [mk(s_, 0.0, ON, **REST) for s_ in (0, 10, 20)]               # 10:00:00～10:00:20 休息（已上電）
    ag += [mk(32), mk(45)]                                              # 10:00:32 放電開始（起始不完整分鐘）
    ag += [mk(61, soc=80, current_a=-140.1, voltage_v=910.2, rack_max_c=33, rack_min_c=29, cell_vmax_v=3.251, cell_vmin_v=3.220),
           mk(65, rack_max_c=34, rack_min_c=30, cell_vmax_v=3.254, cell_vmin_v=3.218),
           mk(70, soc=80, current_a=-140.0, voltage_v=910.0, rack_max_c=33, rack_min_c=28, cell_vmax_v=3.253, cell_vmin_v=3.221),
           mk(74, rack_max_c=32, rack_min_c=29, cell_vmax_v=3.256, cell_vmin_v=3.219),
           mk(90, soc=79, current_a=-140.2, voltage_v=909.8, rack_max_c=33, rack_min_c=29, cell_vmax_v=3.250, cell_vmin_v=3.222),
           mk(108, soc=79, current_a=-140.1, voltage_v=909.6, rack_max_c=34, rack_min_c=29, cell_vmax_v=3.252,
              cell_vmin_v=3.221, sensor_c=26.5, pcs_module_c=61.0)]     # 10:01 = 使用者範例（全部有效）
    ag += [mk(125), mk(135, pcs_module_c="-", rack_max_c=45, cell_vmin_v=3.100),     # 10:02：PCS 缺值但極值真實
           mk(150, rack_max_c=41), mk(160, voltage_v=None, rack_min_c=25),          # 總電壓缺值但 Rack 最小真實
           mk(175, soc=78)]
    ag += [mk(s_) for s_ in range(185, 226, 10)] + [mk(235, sensor_c=None)]       # 10:03 最後一筆傳感器缺值（選用）
    ag += [mk(245), mk(255), mk(260), mk(270, 135.0, rack_max_c=60), mk(280), mk(290)]   # 10:04:30 反向突波
    ag += [mk(s_, pcs_module_c="-", rack_max_c=42) for s_ in range(300, 351, 10)]    # 10:05 整分鐘 PCS 缺值
    ag += [mk(s_, soc=77) for s_ in range(360, 401, 10)]                # 10:06:00～10:06:40 放電（A = 10:06:40）
    ag += [mk(s_, 0.0, ON, **REST) for s_ in range(410, 551, 10)]       # 10:06:50 起休息（已上電、有效）
    ag[[k_ for k_, o_ in enumerate(ag) if o_.t == T0 + timedelta(seconds=540)][0]].values["rack_max_c"] = 50
    ag += [mk(s_, 0.0, FAULT, **REST) for s_ in range(560, 591, 10)]    # 10:09:20 起故障（狀態 N/A，不符合有效判定）
    NO_HMI = dict(soc=None, current_a=None, power_kw=None, rack_max_c=None, rack_min_c=None, cell_vmax_v=None,
                  cell_vmin_v=None, voltage_v=None, sensor_c=None, pcs_module_c=80.0)
    ag += [mk(s_, 0.0, ON, **NO_HMI) for s_ in range(600, 651, 10)]     # 10:10 只有 6161 溫度
    ag += [mk(s_, 0.0, ON, **REST) for s_ in range(660, 701, 10)]
    ares = analyze(ag, p)
    ast = ares.stages[0]
    anames = {st_.no: RXv._stage_title(_sn(ares.stages), st_) for st_ in ares.stages}
    avalid = lambda o_: RXv.is_report_valid(o_, p, anames)
    srow = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(ares, ast, valid=avalid)}                   # ext_scope="stage"（舊行為）
    srow_m = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(ares, ast, valid=avalid, ext_scope="minute")}   # 正式階段頁
    # 正式整體頁：極值＝完整日曆分鐘（ext_scope="minute"）；orow_ph＝舊行為（預設 "phase"），用來確認舊行為仍保留
    orow = {m_.T.strftime("%H:%M"): m_ for m_ in _om(ares, valid=avalid, phase=lambda o_: RXv.overall_phase(o_, anames),
                                                     ext_scope="minute")}
    orow_ph = {m_.T.strftime("%H:%M"): m_ for m_ in _om(ares, valid=avalid, phase=lambda o_: RXv.overall_phase(o_, anames))}
    sec = lambda o_: None if o_ is None else int((o_.t - T0).total_seconds())
    ext = lambda m_, k_: (m_.ext[k_][0], sec(m_.ext[k_][1])) if k_ in m_.ext else (None, None)
    check("N0 情境：休息 → 放電（10:00:32～10:06:40，含 10:04:30 反向突波）→ 休息／故障／僅 6161",
          len(ares.stages) == 1 and ast.dir == "D" and sec(ast.start) == 32 and sec(ast.A) == 400)
    check("N1 Calendar Minute：階段頁 10:00～10:06（首筆／A 所在分鐘），每列使用的觀測都落在 [HH:MM:00, 下一分鐘)",
          list(srow) == ["10:00", "10:01", "10:02", "10:03", "10:04", "10:05", "10:06"]
          and all(m_.T <= o_.t < m_.T + timedelta(minutes=1) for m_ in srow.values()
                  for o_ in ([m_.obs] if m_.obs else []) + [v_[1] for v_ in m_.ext.values()]))
    check("N2 一般欄位 = 該分鐘最後一筆有效觀測（10:01 → 10:01:48；10:02 → 10:02:55；10:03 → 10:03:55）",
          (sec(srow["10:01"].obs), sec(srow["10:02"].obs), sec(srow["10:03"].obs)) == (108, 175, 235))
    wa_ = load_workbook(save_report(ares, META, os.path.join(tmp, "agg.xlsx")))
    a1 = wa_[[n_ for n_ in wa_.sheetnames if re.match(r"\d+\.", n_)][0]]
    arow = {a1.cell(r_, 1).value.strftime("%H:%M"): {h_: a1.cell(r_, COL[h_]).value for h_ in LOG_HEADERS[1:]}
            for r_ in range(2, a1.max_row + 1)}
    check("N3 一般欄位同一筆觀測：10:01 SOC 79／電流 -140.1／總電壓 909.6／傳感器 26.5／PCS 61 全部 = 10:01:48",
          [arow["10:01"][h_] for h_ in ("SOC", "電流", "總電壓", "傳感器溫度", "PCS 模組溫度")] == [79, -140.1, 909.6, 26.5, 61])
    check("N4 Rack 最大 = 分鐘 MAX：10:01 [33,34,33,32,33,34] → 34（來源 10:01:05，同值取較早），不是 10:01:01 的 33",
          ext(srow["10:01"], "rack_max_c") == (34, 65) and arow["10:01"]["Rack 最大溫度"] == 34)
    check("N5 Rack 最小 = 分鐘 MIN：10:01 [29,30,28,29,29,29] → 28（來源 10:01:10）",
          ext(srow["10:01"], "rack_min_c") == (28, 70) and arow["10:01"]["Rack 最小溫度"] == 28)
    check("N6 Cell 最大 = 分鐘 MAX：10:01 [3.251,3.254,3.253,3.256,3.250,3.252] → 3.256（來源 10:01:14）",
          ext(srow["10:01"], "cell_vmax_v") == (3.256, 74) and arow["10:01"]["Cell 最大電壓"] == 3.256)
    check("N7 Cell 最小 = 分鐘 MIN：10:01 [3.220,3.218,3.221,3.219,3.222,3.221] → 3.218（來源 10:01:05）",
          ext(srow["10:01"], "cell_vmin_v") == (3.218, 65) and arow["10:01"]["Cell 最小電壓"] == 3.218)
    o160 = next(o_ for o_ in ares.obs if sec(o_) == 160)
    check("N8 極值只需該欄位本身有效：10:02:40 總電壓缺值（不符合正式報表有效判定）的 Rack 最小 25 仍參與 → 25",
          not avalid(o160) and ext(srow["10:02"], "rack_min_c") == (25, 160))
    o135 = next(o_ for o_ in ares.obs if sec(o_) == 135)
    check("N9 PCS 缺值（6161）不影響 HMI 有效性：10:02:15（PCS \"-\"）仍為有效取樣、進 Raw Data；"
          "Rack 最大 45、Cell 最小 3.100 照實參與分鐘極值；一般欄位取 10:02:55",
          avalid(o135) and ext(srow["10:02"], "rack_max_c") == (45, 135) and ext(srow["10:02"], "cell_vmin_v") == (3.1, 135)
          and sec(srow["10:02"].obs) == 175 and o135 in RXv.raw_rows_obs(ares, p, anames))
    # 2026-11-03 正式需求：階段頁一般欄位仍只用本階段；四個極值＝階段所涵蓋分鐘的完整日曆分鐘（含同分鐘待機／休息），排除明確反方向
    check("N10 Stage 起始不完整分鐘：10:00 一般欄位只用放電（10:00:45）；"
          "極值＝完整分鐘，含 10:00:00～20 待機（Rack 最小 10）；舊 stage 範圍仍為 30",
          sec(srow_m["10:00"].obs) == 45 and ext(srow_m["10:00"], "rack_min_c") == (10, 0)
          and ext(srow["10:00"], "rack_min_c")[0] == 30)
    check("N11 Stage 結束不完整分鐘：10:06 一般欄位 = A 點 10:06:40（不含之後的休息）；"
          "極值來源可到 A 之後的 10:06:50 但不超出 10:06 這一分鐘",
          sec(srow_m["10:06"].obs) == 400 and all(sec(v_[1]) < 420 for v_ in srow_m["10:06"].ext.values())
          and any(sec(v_[1]) == 410 for v_ in srow_m["10:06"].ext.values()))
    o270 = next(o_ for o_ in ares.obs if sec(o_) == 270)
    check("N12 Stage 不跨 Direction（正式完整分鐘仍保留）：10:04:30 明確反向（充電）突波 Rack 最大 60 不參與階段頁 10:04 "
          "極值與一般欄位 → 40",
          o270.direction == "C" and ext(srow_m["10:04"], "rack_max_c")[0] == 40 and srow_m["10:04"].obs is not o270
          and ext(srow["10:04"], "rack_max_c")[0] == 40)
    check("N13 Stage 結束分鐘跨 Rest：一般欄位不混入休息（10:06 = A）；極值納入同分鐘 10:06:50 休息（Rack 最小 10、最大 40）；"
          "舊 stage 範圍仍為 30／40",
          srow_m["10:06"].obs.state == "放電"
          and (ext(srow_m["10:06"], "rack_min_c")[0], ext(srow_m["10:06"], "rack_max_c")[0]) == (10, 40)
          and (ext(srow["10:06"], "rack_min_c")[0], ext(srow["10:06"], "rack_max_c")[0]) == (30, 40))
    # 2026-11-03 正式需求：整體頁四個極值＝完整日曆分鐘（不因語意範圍排除同分鐘其他觀測）；一般欄位仍＝最後語意範圍
    check("N14 Overall Running → Rest：10:06 一般欄位仍代表最後語意（休息，10:06:50）；"
          "極值＝完整分鐘（放電 40／30 ＋ 休息 20／10 → 最大 40、最小 10）",
          sec(orow["10:06"].obs) == 410 and orow["10:06"].obs.state == "休息"
          and (ext(orow["10:06"], "rack_max_c")[0], ext(orow["10:06"], "rack_min_c")[0]) == (40, 10))
    check("N15 Overall Rest → Running：10:00 一般欄位仍代表最後語意（放電，10:00:45）；"
          "極值＝完整分鐘（休息 20／10 ＋ 放電 40／30 → 最大 40、最小 10）",
          sec(orow["10:00"].obs) == 45 and orow["10:00"].obs.stage_no == 1
          and (ext(orow["10:00"], "rack_max_c")[0], ext(orow["10:00"], "rack_min_c")[0]) == (40, 10))
    check("N14p／N15p ext_scope=\"phase\"（預設）保留舊行為：10:06 極值只聚合休息 20／10；10:00 只聚合放電 40／30；"
          "一般欄位與正式整體頁相同",
          (ext(orow_ph["10:06"], "rack_max_c")[0], ext(orow_ph["10:06"], "rack_min_c")[0]) == (20, 10)
          and (ext(orow_ph["10:00"], "rack_max_c")[0], ext(orow_ph["10:00"], "rack_min_c")[0]) == (40, 30)
          and ext(orow_ph["10:09"], "rack_max_c")[0] == 20
          and all(orow_ph[k_].obs is orow[k_].obs for k_ in orow))
    check("N11b 10:05 整分鐘 PCS 缺值（HMI 正常）→ 一般欄位取該分鐘 HMI 有效的最後一筆 10:05:50，只有 PCS N/A"
          "（不沿用 10:04:50／10:06:00 的 PCS）；極值照實（Rack 最大 42）",
          sec(srow["10:05"].obs) == 350
          and [arow["10:05"][h_] for h_ in ("SOC", "電流", "總電壓", "傳感器溫度", "PCS 模組溫度")] == [80, -140.0, 910.0, 25.0, "N/A"]
          and arow["10:05"]["Rack 最大溫度"] == 42)
    check("N14b Overall 故障：10:09 一般欄位仍代表故障（10:09:50 故障觀測）；"
          "極值＝完整分鐘，同分鐘已上電休息的 Rack 最大 50（10:09:00）也納入 → 50（故障不隔離極值）",
          sec(orow["10:09"].obs) == 590 and RXv.status_text(orow["10:09"].obs, anames) == "故障"
          and ext(orow["10:09"], "rack_max_c") == (50, 540))
    check("N14c Overall 只有 6161 溫度的分鐘：維持原整體頁 fallback（10:10 取 10:10:50、PCS 80、HMI 欄位 N/A）",
          sec(orow["10:10"].obs) == 650 and RXv.field_value(orow["10:10"].obs, "pcs_module_c", p) == 80.0
          and not orow["10:10"].ext)
    check("N14d Overall 沒有新增整列 N/A：每一分鐘都有觀測（休息、故障、僅 6161 都照常呈現）",
          all(m_.obs is not None for m_ in orow.values()))

    # 2026-11-05 方案 B：PCS 模組溫度（6161）改為選用；每分鐘一般欄位優先選「含 PCS 溫度」的有效取樣（階段頁＝整體頁），
    # 沒有才選 HMI 有效的最後一筆；極值聚合不受影響；Raw Data 不套用此優先規則
    PCS_NA = dict(pcs_module_c="-")
    pb = [mk(s_, 0.0, ON, **REST) for s_ in (0, 10, 20)]
    pb += [mk(s_) for s_ in range(30, 100, 10)]                                       # 10:00～10:01:30 全部含 PCS
    pb += [mk(100, soc=70, **PCS_NA), mk(110, soc=70, rack_max_c=55, **PCS_NA)]      # 10:01 最後兩筆 PCS 缺值（Rack 最大 55）
    pb += [mk(s_, soc=69, **PCS_NA) for s_ in range(120, 180, 10)]                   # 10:02 整分鐘 PCS 缺值
    pb += [mk(s_, soc=68) for s_ in range(180, 230, 10)] + [mk(230, 0.0, ON, **NO_HMI)]   # 10:03:50 HMI 失敗（僅 6161）
    pb += [mk(s_, soc=68) for s_ in range(240, 300, 10)] + [mk(s_, 0.0, ON, **REST) for s_ in range(300, 361, 10)]
    pres = analyze(pb, p)
    pnames = {st_.no: RXv._stage_title(_sn(pres.stages), st_) for st_ in pres.stages}
    pvalid = lambda o_: RXv.is_report_valid(o_, p, pnames)
    pref = lambda o_: RXv.has_pcs_temp(o_, p)
    ps = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(pres, pres.stages[0], valid=pvalid, ext_scope="minute", prefer=pref)}
    po = {m_.T.strftime("%H:%M"): m_ for m_ in _om(pres, valid=pvalid, phase=lambda o_: RXv.overall_phase(o_, pnames),
                                                  ext_scope="minute", prefer=pref)}
    ps0 = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(pres, pres.stages[0], valid=pvalid, ext_scope="minute")}
    check("B1 10:01 最後兩筆 PCS 缺值 → 階段頁與整體頁都優先選含 PCS 的最後一筆 10:01:30（SOC 80、PCS 60），"
          "不是 10:01:50（無 prefer 時才會選 10:01:50）",
          sec(ps["10:01"].obs) == sec(po["10:01"].obs) == 90 and RXv.field_value(ps["10:01"].obs, "pcs_module_c", p) == 60.0
          and sec(ps0["10:01"].obs) == 110)
    check("B2 極值不受選樣優先影響：10:01 Rack 最大仍為 PCS 缺值那筆的 55（來源 10:01:50）",
          ext(ps["10:01"], "rack_max_c") == (55, 110) and ext(po["10:01"], "rack_max_c") == (55, 110))
    check("B3 10:02 整分鐘 PCS 缺值 → 兩頁都選 HMI 有效的最後一筆 10:02:50（SOC 69），PCS N/A；不沿用 10:01 的 PCS",
          sec(ps["10:02"].obs) == sec(po["10:02"].obs) == 170
          and RXv.field_value(ps["10:02"].obs, "pcs_module_c", p) is None and _num(ps["10:02"].obs.values["soc"]) == 69)
    check("B4 HMI 失敗（僅 6161 PCS 80）不當成有效：10:03 兩頁都選 10:03:40（HMI 有效），10:03:50 不選、不進 Raw Data",
          sec(ps["10:03"].obs) == sec(po["10:03"].obs) == 220
          and all(sec(o_) != 230 for o_ in RXv.raw_rows_obs(pres, p, pnames)))
    praw = {sec(o_) for o_ in RXv.raw_rows_obs(pres, p, pnames)}
    check("B5 Raw Data 不套用優先規則：PCS 缺值的 HMI 有效取樣（10:01:40／50、10:02 整分鐘）全部輸出",
          {100, 110, 120, 130, 140, 150, 160, 170} <= praw)
    check("B6 階段頁與整體頁同一分鐘選用同一筆取樣（階段範圍內全部分鐘）",
          all(ps[k_].obs is po[k_].obs for k_ in ps if ps[k_].obs is not None))

    # 2026-11-05 PCS 模組溫度呈現：有值 → 數值；該筆記錄 6161 讀取失敗 → FAIL＋該筆實際原因；其他缺值 → N/A＋說明（不推論）
    def mkn(sec_, notes_, cur=-140.0, flags=None, **v):
        o_ = mk(sec_, cur, flags, **v)
        o_.notes = list(notes_)
        return o_
    F_REF = "6161 讀取失敗：ConnectionRefusedError: [WinError 10061] 拒絕連線"
    F_TO = "6161 讀取失敗：TimeoutError: timed out"
    F_URL = "6161 讀取失敗：URLError: <urlopen error timed out>"
    pc = [mk(s_, 0.0, ON, **REST) for s_ in (0, 10, 20)]
    pc += [mk(s_) for s_ in range(30, 110, 10)]                                       # 10:00～10:01:40 含 PCS
    pc += [mkn(110, [F_REF], pcs_module_c=None)]                                      # 10:01:50 讀取失敗（拒絕連線）
    pc += [mkn(s_, [F_URL if s_ == 160 else F_TO], pcs_module_c=None, sensor_c=None) for s_ in range(120, 180, 10)]
    pc += [mkn(s_, ["6161 無資料（API 回傳空內容）"], pcs_module_c=None) for s_ in range(180, 240, 10)]
    pc += [mkn(s_, [], pcs_module_c=None, sensor_c="-") for s_ in range(240, 300, 10)]   # 原因無法確認；傳感器停用
    pc += [mk(s_, 0.0, ON, **REST) for s_ in range(300, 350, 10)] + [mkn(350, [F_TO], 0.0, ON, pcs_module_c=None, **REST)]
    pcres = analyze(pc, p)
    wpc = load_workbook(save_report(pcres, META, os.path.join(tmp, "pcsfail.xlsx")))
    sp_ = wpc[[n_ for n_ in wpc.sheetnames if re.match(r"\d+\.", n_)][0]]
    op_ = wpc["整體測試"]
    rp2 = wpc["Raw Data"]
    RH2 = [c.value for c in rp2[1]]
    rraw = {int((r_[RH2.index("時間")] - T0).total_seconds()): r_ for r_ in rp2.iter_rows(min_row=2, values_only=True)
            if r_[RH2.index("序號")] is not None}
    cell = lambda ws_, hhmm, h_: ws_.cell(row_of(ws_, hhmm), COL[h_]).value
    note = lambda ws_, hhmm: str(ws_.cell(row_of(ws_, hhmm), NOTE).value or "")
    check("P1 10:01 同分鐘較早有 PCS 值、最後一筆 6161 拒絕連線 → 選用 10:01:40：PCS 60，備註不混入同分鐘的失敗原因",
          cell(sp_, "10:01", "PCS 模組溫度") == 60.0 and cell(op_, "10:01", "PCS 模組溫度") == 60.0
          and "PCS" not in note(sp_, "10:01") and "PCS" not in note(op_, "10:01"))
    check("P2 10:02 整分鐘 6161 讀取失敗 → 階段頁、整體頁 PCS = FAIL，備註為選用那筆（10:02:50）的實際原因 TimeoutError"
          "（不是同分鐘 10:02:40 的 URLError、也不寫死）；SOC／電流／電壓／Rack／Cell 照實",
          cell(sp_, "10:02", "PCS 模組溫度") == "FAIL" == cell(op_, "10:02", "PCS 模組溫度")
          and f"PCS 模組溫度 FAIL（資料取得失敗）：{F_TO}" in note(sp_, "10:02") and "URLError" not in note(sp_, "10:02")
          and [cell(sp_, "10:02", h_) for h_ in ("SOC", "電流", "總電壓", "Rack 最小溫度", "Cell 最大電壓")]
          == [80, -140.0, 910.0, 30, 3.30])
    check("P3 整體頁備註保留既有狀態文字，再附 PCS 失敗原因（不覆蓋）：「第1次放電；PCS 模組溫度 FAIL…」",
          note(op_, "10:02") == f"第1次放電；PCS 模組溫度 FAIL（資料取得失敗）：{F_TO}")
    check("P4 6161 有回應但空內容 → N/A＋「6161 回傳空內容」；沒有任何原因紀錄 → N/A＋「原因無法確認」（不判定為通訊故障）",
          cell(sp_, "10:03", "PCS 模組溫度") == "N/A" and "6161 回傳空內容" in note(sp_, "10:03")
          and cell(sp_, "10:04", "PCS 模組溫度") == "N/A" and "原因無法確認" in note(sp_, "10:04")
          and "FAIL" not in note(sp_, "10:03") + note(sp_, "10:04"))
    check("P5 傳感器停用／缺值 → 維持 N/A（6161 讀取失敗時也不顯示 FAIL）",
          cell(sp_, "10:02", "傳感器溫度") == "N/A" and cell(sp_, "10:04", "傳感器溫度") == "N/A")
    check("P6 Raw Data 逐筆同一規則：10:01:50 FAIL＋拒絕連線原因；10:02:40 FAIL＋URLError；10:02:50 FAIL＋TimeoutError；"
          "10:03 N/A＋空內容；有值者無 PCS 備註",
          rraw[110][RH2.index("PCS 模組溫度")] == "FAIL" and F_REF in rraw[110][RH2.index("說明")]
          and rraw[160][RH2.index("PCS 模組溫度")] == "FAIL" and F_URL in rraw[160][RH2.index("說明")]
          and F_TO in rraw[170][RH2.index("說明")] and rraw[180][RH2.index("PCS 模組溫度")] == "N/A"
          and "回傳空內容" in rraw[180][RH2.index("說明")]
          and rraw[100][RH2.index("PCS 模組溫度")] == 60.0 and not rraw[100][RH2.index("說明")])
    check("P7 Raw Data 備註不覆蓋：休息取樣既有「分析狀態／電池上下電」說明保留，後面再附 PCS 失敗原因",
          rraw[350][RH2.index("說明")] == f"分析狀態：休息；電池上下電：已上電；PCS 模組溫度 FAIL（資料取得失敗）：{F_TO}")
    _off = mk(9999, 0.0, {"battery_power_status": "已下電"}, **REST)
    _on = mk(9999, 0.0, ON, **REST)
    _ra = analyze(ag[:3] + [_on, _off], p)
    _no = {st_.no: RXv._stage_title(_sn(_ra.stages), st_) for st_ in _ra.stages}
    check("N14e Battery Off 語意：停機與待機的 overall_phase 不同（一般欄位不混合、不以已上電觀測取代下電）",
          RXv.status_text(_off, _no) == "停機" and RXv.status_text(_on, _no) == "待機"
          and RXv.overall_phase(_off, _no) != RXv.overall_phase(_on, _no))

    print("\n== Overall 極值＝完整日曆分鐘（ext_scope=\"minute\"）；一般欄位＝最後語意；階段頁不變 ==")
    from cert_stage_engine import EXTREME_FIELDS, _in_minute, has_hmi_values as _hv, minute_aggregate as _ma

    def ov_pair(res_):
        nm_ = {st_.no: RXv._stage_title(_sn(res_.stages), st_) for st_ in res_.stages}
        v_ = lambda o_: RXv.is_report_valid(o_, p, nm_)
        ph_ = lambda o_: RXv.overall_phase(o_, nm_)
        mn = {m_.T.strftime("%H:%M"): m_ for m_ in _om(res_, valid=v_, phase=ph_, ext_scope="minute")}
        pp = {m_.T.strftime("%H:%M"): m_ for m_ in _om(res_, valid=v_, phase=ph_)}
        return mn, pp, nm_, v_

    def xv(m_, k_):
        return m_.ext[k_][0] if k_ in m_.ext else None

    # 充電情境：待機（Cell 3.483／3.356、Rack 20／10）→ 充電（Cell 3.40／3.30、Rack 40／30；結束前 10:06:30～40 Cell 最大 3.61）→ 待機
    IDLE_V = dict(REST, cell_vmax_v=3.483, cell_vmin_v=3.356)
    cg = [mk(s_, 0.0, ON, **IDLE_V) for s_ in (0, 10, 20)]
    cg += [mk(s_, 135.0, ON, cell_vmax_v=3.61 if s_ >= 390 else 3.40, cell_vmin_v=3.30) for s_ in range(32, 401, 10)]
    cg += [mk(s_, 0.0, ON, **dict(REST, cell_vmax_v=3.45, cell_vmin_v=3.35)) for s_ in range(410, 551, 10)]
    cres = analyze(cg, p)
    cmn, cpp, cnm, cv = ov_pair(cres)
    csr = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(cres, cres.stages[0], valid=cv)}
    check("E3 Idle → Charge（10:00）：極值＝完整分鐘（Cell 最大 3.483 來自待機、Rack 最小 10）；一般欄位＝充電最後一筆 10:00:52",
          cres.stages and cres.stages[0].dir == "C" and xv(cmn["10:00"], "cell_vmax_v") == 3.483
          and xv(cmn["10:00"], "rack_min_c") == 10 and cmn["10:00"].obs.stage_no == 1 and sec(cmn["10:00"].obs) == 52)
    check("E5 Charge → Idle（10:06）：極值＝完整分鐘（充電結束 Cell 最大 3.61 保留）；一般欄位＝待機最後一筆 10:06:50",
          xv(cmn["10:06"], "cell_vmax_v") == 3.61 and xv(cmn["10:06"], "rack_max_c") == 40
          and cmn["10:06"].obs.state == "休息" and sec(cmn["10:06"].obs) == 410)
    check("E10 ext_scope=\"phase\" 舊行為：10:00 只看充電（Cell 最大 3.40、Rack 最小 30）；10:06 只看待機（Cell 最大 3.45）",
          xv(cpp["10:00"], "cell_vmax_v") == 3.40 and xv(cpp["10:00"], "rack_min_c") == 30
          and xv(cpp["10:06"], "cell_vmax_v") == 3.45)
    csr_m = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(cres, cres.stages[0], valid=cv, ext_scope="minute")}
    # 2026-11-03 正式需求變更：階段頁一般欄位不變；四個極值在階段所涵蓋分鐘改用完整日曆分鐘（排除明確反方向）
    check("E9 階段頁：一般欄位與分鐘範圍不變（10:00～10:06）；四個極值在邊界分鐘納入同分鐘待機——"
          "10:00 Cell 最大 3.483、Rack 最小 10（舊 3.40／30）；10:06 Cell 最大 3.61、Rack 最小 10（舊 3.61／30）",
          list(csr_m) == list(csr) == ["10:00", "10:01", "10:02", "10:03", "10:04", "10:05", "10:06"]
          and all(csr_m[k_].obs is csr[k_].obs for k_ in csr)
          and xv(csr_m["10:00"], "cell_vmax_v") == 3.483 and xv(csr_m["10:00"], "rack_min_c") == 10
          and xv(csr_m["10:06"], "cell_vmax_v") == 3.61 and xv(csr_m["10:06"], "rack_min_c") == 10
          and xv(csr["10:00"], "cell_vmax_v") == 3.40 and xv(csr["10:00"], "rack_min_c") == 30
          and all(o_.stage_no == 1 for m_ in csr.values() for _v, o_ in m_.ext.values()))
    # 放電情境（ag）：10:00 Idle → Discharge、10:06 Discharge → Idle
    check("E1／E4 Idle → Discharge（10:00）：完整分鐘 Rack 最小 10（待機）＋最大 40（放電）；phase 只看放電 30",
          xv(orow["10:00"], "rack_min_c") == 10 and xv(orow["10:00"], "rack_max_c") == 40
          and xv(orow_ph["10:00"], "rack_min_c") == 30)
    check("E2／E6 Discharge → Idle（10:06）：完整分鐘 Rack 最大 40（放電結束）＋最小 10（待機）；phase 只看待機 20",
          xv(orow["10:06"], "rack_max_c") == 40 and xv(orow["10:06"], "rack_min_c") == 10
          and xv(orow_ph["10:06"], "rack_max_c") == 20)
    # 停機（已下電）與待機同分鐘：Rack 最大 50 出現在待機，最後一筆是停機
    so = [mk(s_, 0.0, ON, **REST) for s_ in (0, 10, 20)] + [mk(30, 0.0, ON, **dict(REST, rack_max_c=50))]
    so += [mk(s_, 0.0, {"battery_power_status": "已下電", "pcs_fault_flag": False}, **dict(REST, rack_min_c=5))
           for s_ in (40, 50)]
    sres = analyze(so, p)
    smn, spp, snm, _sv = ov_pair(sres)
    check("E7 Stop／Fault 與其他語意同分鐘：一般欄位＝停機最後一筆（10:00:50）；完整分鐘極值 Rack 最大 50（待機）、最小 5（停機）；"
          "故障同理（N14b：10:09 Rack 最大 50）",
          RXv.status_text(smn["10:00"].obs, snm) == "停機" and sec(smn["10:00"].obs) == 50
          and xv(smn["10:00"], "rack_max_c") == 50 and xv(smn["10:00"], "rack_min_c") == 5
          and xv(spp["10:00"], "rack_max_c") == 20 and xv(orow["10:09"], "rack_max_c") == 50)
    gen_same = all(a_.obs is b_.obs and a_.na_reason == b_.na_reason
                   for mn_, pp_ in ((orow, orow_ph), (cmn, cpp), (smn, spp)) for a_, b_ in zip(mn_.values(), pp_.values()))
    check("E8 一般欄位不變：三個情境每一分鐘，minute 與 phase 選用的一般觀測（SOC／電流／電壓／功率／PCS／傳感器來源）完全相同",
          gen_same and len(orow) == len(orow_ph))

    def full_ext(res_, T_):
        hm_ = [o_ for o_ in res_.obs if o_.ok and _hv(o_)]
        return _ma([], _in_minute(hm_, [o_.t for o_ in hm_], T_), p)[1]
    check("E11 ext_scope=\"minute\" 定義：每一分鐘四個極值＝該分鐘全部 HMI 候選（欄位本身有效即可，Policy B）的 MAX／MIN",
          all({k_: v_[0] for k_, v_ in m_.ext.items()} == {k_: v_[0] for k_, v_ in full_ext(r_, m_.T).items()}
              for r_, mn_ in ((ares, orow), (cres, cmn), (sres, smn)) for m_ in mn_.values() if m_.ext)
          and set(EXTREME_FIELDS) == {"rack_max_c", "rack_min_c", "cell_vmax_v", "cell_vmin_v"})
    wc_ = load_workbook(save_report(cres, META, os.path.join(tmp, "ext_minute.xlsx")))["整體測試"]
    hc_ = [c_.value for c_ in wc_[1]]
    xr = {r_[0].strftime("%H:%M"): r_ for r_ in wc_.iter_rows(min_row=2, values_only=True) if r_[0]}
    check("E12 正式報告整體測試頁採完整分鐘：10:00 Cell 最大 3.483、10:06 Cell 最大 3.61（Excel 實際儲存格）",
          xr["10:00"][hc_.index("Cell 最大電壓")] == 3.483 and xr["10:06"][hc_.index("Cell 最大電壓")] == 3.61)

    print("\n== Stage 極值＝階段所涵蓋分鐘的完整日曆分鐘（排除明確反方向）；一般欄位／分鐘範圍不變 ==")
    # 放電情境：10:00:00 待機、10:00:05 明確充電突波（Rack 最大 99、Cell 最大 3.95）、10:00:10 待機 → 10:00:20 起放電至 A=10:05:00
    #          10:05:05 停止後放電旗標未歸零（狀態 N/A、方向 I）Cell 最大 3.80；10:05:10 電流／功率皆缺（方向未明）Cell 最小 2.50
    #          10:05:20 起待機；10:06:00 待機 Cell 最小 2.00（下一分鐘）
    ux = [mk(0, 0.0, ON, **REST), mk(5, 135.0, ON, rack_max_c=99, cell_vmax_v=3.95), mk(10, 0.0, ON, **REST)]
    ux += [mk(s_) for s_ in range(20, 301, 10)]
    ux += [mk(305, 0.0, dict(ON, pcs_discharging_flag=True), **dict(REST, cell_vmax_v=3.80)),
           mk(310, 0.0, ON, **dict(REST, current_a=None, power_kw=None, cell_vmin_v=2.50))]
    ux += [mk(s_, 0.0, ON, **REST) for s_ in range(320, 360, 10)] + [mk(360, 0.0, ON, **dict(REST, cell_vmin_v=2.00))]
    ux += [mk(s_, 0.0, ON, **REST) for s_ in range(370, 401, 10)]
    ures = analyze(ux, p)
    ust = ures.stages[0]
    unm = {st_.no: RXv._stage_title(_sn(ures.stages), st_) for st_ in ures.stages}
    uv = lambda o_: RXv.is_report_valid(o_, p, unm)
    us_ = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(ures, ust, valid=uv)}
    um_ = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(ures, ust, valid=uv, ext_scope="minute")}
    o305 = next(o_ for o_ in ures.obs if sec(o_) == 305)
    o310 = next(o_ for o_ in ures.obs if sec(o_) == 310)
    o5 = next(o_ for o_ in ures.obs if sec(o_) == 5)
    check("S0 情境：1 個放電階段 10:00:20～A 10:05:00；10:00:05 為明確充電；10:05:05 狀態 N/A（方向 I）；10:05:10 方向未明",
          len(ures.stages) == 1 and ust.dir == "D" and sec(ust.start) == 20 and sec(ust.A) == 300
          and o5.direction == "C" and o305.direction == "I" and RXv.status_text(o305, unm) == "N/A"
          and o310.direction is None)
    check("S1 Idle → Discharge（10:00）：同分鐘待機 Rack 最小 10 納入（舊 stage 範圍 30）",
          xv(um_["10:00"], "rack_min_c") == 10 and xv(us_["10:00"], "rack_min_c") == 30)
    check("S6 放電階段同分鐘明確充電突波（10:00:05 Rack 最大 99、Cell 最大 3.95）→ 排除：10:00 Rack 最大 40、Cell 最大 3.30",
          xv(um_["10:00"], "rack_max_c") == 40 and xv(um_["10:00"], "cell_vmax_v") == 3.30
          and all(o_ is not o5 for _v, o_ in um_["10:00"].ext.values()))
    check("S5 階段結束後同分鐘：狀態 N/A（旗標未歸零）Cell 最大 3.80、方向未明 Cell 最小 2.50 → 皆納入 10:05 極值；"
          "Raw Data 不顯示這兩筆不影響極值資格",
          um_["10:05"].ext["cell_vmax_v"] == (3.80, o305) and um_["10:05"].ext["cell_vmin_v"] == (2.50, o310)
          and not uv(o305) and xv(us_["10:05"], "cell_vmax_v") == 3.30)
    check("S2 Discharge → Idle（10:05）：同分鐘 10:05:20 起待機 Rack 最小 10 納入；Rack 最大仍為放電 40",
          xv(um_["10:05"], "rack_min_c") == 10 and xv(um_["10:05"], "rack_max_c") == 40)
    check("S8 下一分鐘資料不納入：10:06:00 Cell 最小 2.00 不進 10:05；階段頁不新增 10:06（分鐘範圍仍 10:00～10:05）",
          list(um_) == list(us_) == ["10:00", "10:01", "10:02", "10:03", "10:04", "10:05"]
          and all(sec(o_) < 360 for m_ in um_.values() for _v, o_ in m_.ext.values()))
    check("S3／S4 Idle → Charge（10:00 Cell 最大 3.483 來自待機）、Charge → Idle（10:06 Rack 最小 10 來自 10:06:50 待機）",
          csr_m["10:00"].ext["cell_vmax_v"][1].state == "休息" and xv(csr_m["10:00"], "cell_vmax_v") == 3.483
          and csr_m["10:06"].ext["rack_min_c"][1].state == "休息" and sec(csr_m["10:06"].ext["rack_min_c"][1]) == 410)
    # 充電階段同分鐘明確放電突波：10:00:25 放電（Rack 最小 1、Cell 最小 2.00）
    cg2 = cg + [mk(25, -140.0, ON, rack_min_c=1, cell_vmin_v=2.00)]
    c2res = analyze(cg2, p)
    c2nm = {st_.no: RXv._stage_title(_sn(c2res.stages), st_) for st_ in c2res.stages}
    c2m = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(c2res, c2res.stages[0], valid=lambda o_: RXv.is_report_valid(o_, p, c2nm),
                                                        ext_scope="minute")}
    o25 = next(o_ for o_ in c2res.obs if sec(o_) == 25)
    check("S7 充電階段同分鐘明確放電突波（10:00:25 Rack 最小 1、Cell 最小 2.00）→ 排除：10:00 Rack 最小 10（待機）、Cell 最小 3.30",
          c2res.stages[0].dir == "C" and o25.direction == "D" and xv(c2m["10:00"], "rack_min_c") == 10
          and xv(c2m["10:00"], "cell_vmin_v") == 3.30 and all(o_ is not o25 for _v, o_ in c2m["10:00"].ext.values()))
    pairs = [(srow, srow_m), (csr, csr_m), (us_, um_)]
    check("S9／S10 一般欄位與分鐘數完全不變：三個情境每一分鐘 stage／minute 選用的一般觀測、N/A 原因、列數皆相同",
          all(list(a_) == list(b_) and all(a_[k_].obs is b_[k_].obs and a_[k_].na_reason == b_[k_].na_reason for k_ in a_)
              for a_, b_ in pairs))
    check("S11 ext_scope=\"stage\"（預設）保持舊行為：極值來源全部屬本階段、時間在首筆～A 之間、方向為放電或停頓",
          all(o_.stage_no == ust.no and ust.start.t <= o_.t <= ust.A.t and o_.direction in ("D", "I")
              for m_ in us_.values() for _v, o_ in m_.ext.values()))
    uov = {m_.T.strftime("%H:%M"): m_ for m_ in _om(ures, valid=uv, phase=lambda o_: RXv.overall_phase(o_, unm),
                                                    ext_scope="minute")}
    check("S12 Overall 規則不變：整體頁完整分鐘不套階段方向過濾（10:00 Rack 最大 99 含充電突波）、仍等於該分鐘全部候選",
          xv(uov["10:00"], "rack_max_c") == 99
          and all({k_: v_[0] for k_, v_ in m_.ext.items()} == {k_: v_[0] for k_, v_ in full_ext(ures, m_.T).items()}
                  for m_ in uov.values() if m_.ext))
    wu_ = load_workbook(save_report(ures, META, os.path.join(tmp, "stage_minute.xlsx")))
    su_ = wu_[[n_ for n_ in wu_.sheetnames if re.match(r"\d+\.", n_)][0]]
    hu_ = [c_.value for c_ in su_[1]]
    ur_ = {r_[0].strftime("%H:%M"): r_ for r_ in su_.iter_rows(min_row=2, values_only=True) if r_[0]}
    check("S13 正式報告階段頁採新規則（Excel 儲存格）：10:00 Rack 最小 10、Rack 最大 40；10:05 Cell 最大 3.80、Cell 最小 2.50；"
          "一般欄位 SOC／電流仍為放電 A 點",
          ur_["10:00"][hu_.index("Rack 最小溫度")] == 10 and ur_["10:00"][hu_.index("Rack 最大溫度")] == 40
          and ur_["10:05"][hu_.index("Cell 最大電壓")] == 3.80 and ur_["10:05"][hu_.index("Cell 最小電壓")] == 2.50
          and ur_["10:05"][hu_.index("電流")] == -140.0 and len(ur_) == 6)
    raw_a = {r_[0]: r_ for r_ in wa_["Raw Data"].iter_rows(min_row=2, values_only=True) if r_[0] is not None}
    RHa = [c.value for c in wa_["Raw Data"][1]]
    m01 = [o_ for o_ in ares.obs if 60 <= sec(o_) < 120]
    check("N17 Raw Data 原始值不被分鐘彙整覆寫：10:01 六筆 Rack 最大仍為 33／34／33／32／33／34、SOC 80／80／80／80／79／79",
          [raw_a[o_.seq + 1][RHa.index("Rack 最大溫度")] for o_ in m01] == [33, 34, 33, 32, 33, 34]
          and [raw_a[o_.seq + 1][RHa.index("SOC")] for o_ in m01] == [80, 80, 80, 80, 79, 79])
    # 2026-11-03 正式需求由 10 秒改為 5 秒（Phase 1 實機 PASS）→ 期待值依新需求更新（舊 10 秒期待已過時）
    check("N18 取樣間隔＝正式設定：SAMPLE_INTERVAL_SEC = 5",
          CFG.SAMPLE_INTERVAL_SEC == 5 and p["SAMPLE_INTERVAL_SEC"] == 5)
    check("V1 Raw Data 與階段頁／整體頁共用同一 predicate：raw_rows_obs = is_report_valid 過濾",
          RXv.raw_rows_obs(ares, p, anames) == [o_ for o_ in ares.obs if avalid(o_)])
    _iv = RXv.is_report_valid
    RXv.is_report_valid = lambda o_, p_, n_: _iv(o_, p_, n_) and o_.t != T0 + timedelta(seconds=108)   # 額外排除 10:01:48
    try:
        wv = load_workbook(save_report(ares, META, os.path.join(tmp, "agg_patched.xlsx")))
    finally:
        RXv.is_report_valid = _iv
    _w1 = wv[[n_ for n_ in wv.sheetnames if re.match(r"\d+\.", n_)][0]]
    _wo = wv["整體測試"]
    _r = lambda ws_, hhmm: next(r_ for r_ in range(2, ws_.max_row + 1) if ws_.cell(r_, 1).value.strftime("%H:%M") == hhmm)
    check("V1b 只改 is_report_valid（排除 10:01:48）→ Raw Data、階段頁、整體頁同時改用 10:01:30（SOC 79／總電壓 909.8）",
          T0 + timedelta(seconds=108) not in {r_[1] for r_ in wv["Raw Data"].iter_rows(min_row=2, values_only=True)}
          and _w1.cell(_r(_w1, "10:01"), COL["總電壓"]).value == 909.8
          and _wo.cell(_r(_wo, "10:01"), COL["總電壓"]).value == 909.8)

    print("\n== 舊 Cert 相容（Calendar Minute；暫存複本）==")
    from cert_runtime import CertRuntime as _CR
    for cid_ in ("CERT_20261028_165847", "CERT_20261028_180341"):
        real_ = os.path.join(HERE, "..", "output", "cert_reports", cid_)
        if not os.path.isdir(real_):
            print(f"  （略過：本機沒有 {cid_}）")
            continue
        root_ = os.path.join(tmp, "old_" + cid_)
        shutil.copytree(real_, os.path.join(root_, cid_), ignore=shutil.ignore_patterns("reports", "screenshots"))
        rf_ = _CR(root_, reader=None, enable_shots=False, log=lambda *a, **k: None)._offline(
            os.path.join(root_, cid_)).analysis()
        nf_ = {s_.no: RXv._stage_title(_sn(rf_.stages), s_) for s_ in rf_.stages}
        v_ = lambda o_: RXv.is_report_valid(o_, rf_.params, nf_)
        st_rows = [_sm(rf_, s_, valid=v_) for s_ in rf_.stages]
        o_rows = _om(rf_, valid=v_, phase=lambda o_: RXv.overall_phase(o_, nf_))
        fl_ = lambda t_: t_.replace(second=0, microsecond=0)
        has_ok = lambda m_: any(o_.ok and m_.T <= o_.t < m_.T + timedelta(minutes=1) for o_ in rf_.obs)
        check(f"N16b 舊 Cert {cid_[5:]}：階段頁 {[len(r_) for r_ in st_rows]} 列（首筆～A 所在分鐘、無 N/A），"
              f"整體頁 {len(o_rows)} 列、整列 N/A 只出現在真的沒有讀取成功觀測的分鐘（無上下電狀態的休息照常呈現）",
              all(len(r_) == (fl_(s_.A.t) - fl_(s_.start.t)).seconds // 60 + 1 and all(m_.obs for m_ in r_)
                  for r_, s_ in zip(st_rows, rf_.stages))
              and all((m_.obs is None) == (not has_ok(m_)) for m_ in o_rows))

    print("\n== Field 資料 CERT_20261029_082805：Raw Data Filter／Summary 箭頭（暫存複本；不動原始資料）==")
    real = os.path.join(HERE, "..", "output", "cert_reports", "CERT_20261029_082805")
    if os.path.isdir(real):
        import hashlib
        import cert_report_xlsx as RX
        from cert_runtime import CertRuntime
        from cert_stage_engine import _num, has_hmi_values, overall_minutes, stage_minutes
        sha = lambda f: hashlib.sha256(open(f, "rb").read()).hexdigest()
        src_obs = os.path.join(real, "observations.jsonl")
        sha0 = sha(src_obs)
        cid = "CERT_20261029_082805"
        froot = os.path.join(tmp, "field")
        shutil.copytree(real, os.path.join(froot, cid), ignore=shutil.ignore_patterns("reports"))
        rtf = CertRuntime(froot, reader=None, enable_shots=False, log=lambda *a, **k: None)._offline(
            os.path.join(froot, cid))
        rf = rtf.analysis()
        bounds0 = [(s_.start.t, s_.A.t, s_.end_B.t if s_.end_B else None, s_.end_by) for s_ in rf.stages]
        hv = [o for o in rf.obs if o.ok and has_hmi_values(o)]
        meta_f = {"conditions": [], "source_notes": [], "cabinet_spec": "250KW"}
        wbf = load_workbook(save_report(rf, meta_f, os.path.join(tmp, "field.xlsx")))
        rwf = wbf["Raw Data"]
        RH = [c.value for c in rwf[1]]
        fobs = [r_ for r_ in rwf.iter_rows(min_row=2, values_only=True) if r_[RH.index("序號")] is not None]
        ftimes = {r_[RH.index("時間")].strftime("%H:%M:%S") for r_ in fobs}
        hm = lambda t: t.strftime("%H:%M:%S")
        j165 = rtf.records[164]
        req_ok = [o for o in hv if all(_num(o.values.get(k)) is not None for k in RX.RAW_REQUIRED)]
        pcs_miss = [o for o in hv if _num(o.values.get("pcs_module_c")) is None]
        nf = {s_.no: RX._stage_title(RX.stage_names(rf.stages), s_) for s_ in rf.stages}
        st_na = [o for o in req_ok if RX.status_text(o, nf) == "N/A"]
        fin = [o for o in req_ok if RX.status_text(o, nf) != "N/A"]
        check(f"082805：Raw Data 觀測列 = HMI 有效 ∧ 必要（HMI）欄位齊全 ∧ 狀態可判定（{len(fobs)} = {len(fin)}；"
              f"原始 {len(rtf.records)}、HMI 無效 {len(rf.obs) - len(hv)}、狀態無法判定 {len(st_na)}；"
              f"PCS 模組缺值 {len(pcs_miss)} 筆改為照常輸出、PCS 顯示 N/A）",
              len(fobs) == len(fin) == 3196 and len(req_ok) == 3209 and len(hv) == 3209
              and len(rf.obs) == len(rtf.records) == 3229
              and [hm(o.t) for o in pcs_miss] == ["13:24:55", "13:43:41", "16:41:16", "17:55:49", "17:56:34"]
              and {hm(o.t) for o in pcs_miss} <= ftimes)
        from collections import Counter
        f_ = lambda o: o.flags or {}
        cause = Counter("PCS 故障" if f_(o).get("pcs_fault_flag") is True
                        else "充放電旗標未歸零" if f_(o).get("pcs_charging_flag") is True or f_(o).get("pcs_discharging_flag") is True
                        else "上下電" + str(f_(o).get("battery_power_status")) for o in st_na)
        check(f"082805 狀態 N/A 原因：{dict(cause)}；全部在階段外（direction I／休息）",
              cause == {"上下電未知": 8, "充放電旗標未歸零": 5}
              and all(o.direction == "I" and o.stage_no is None for o in st_na))
        stc = Counter(r_[RH.index("狀態")] for r_ in fobs)
        check(f"082805 Raw Data 狀態分布：{dict(stc)}（無 N/A）",
              stc == {"第1次放電（滿放）": 683, "第1次充電（滿充）": 686, "第2次放電（滿放）": 690,
                      "第2次充電（滿充）": 413, "待機": 686, "停機": 3, "故障": 35})
        flt_rows = [r_ for r_ in fobs if r_[RH.index("狀態")] == "故障"]
        KH = {"SOC": "soc", "電流": "current_a", "總電壓": "voltage_v", "Rack 最大溫度": "rack_max_c",
              "Rack 最小溫度": "rack_min_c", "Cell 最大電壓": "cell_vmax_v", "Cell 最小電壓": "cell_vmin_v",
              "PCS 模組溫度": "pcs_module_c", "功率 kW": "power_kw"}
        check(f"15 082805 故障 Regression：PCS 故障觀測以「故障」進 Raw Data（{len(flt_rows)} 筆，"
              f"{hm(rf.obs[flt_rows[0][0] - 1].t)}～{hm(rf.obs[flt_rows[-1][0] - 1].t)}），"
              "SOC／電流／電壓／Rack／Cell／PCS／功率全部等於原始 observation（不補值、不推論）",
              len(flt_rows) == 35 and all(rf.obs[r_[0] - 1].flags.get("pcs_fault_flag") is True for r_ in flt_rows)
              and all(r_[RH.index(h_)] == RX.field_value(rf.obs[r_[0] - 1], k_, rf.params)
                      for r_ in flt_rows for h_, k_ in KH.items()))
        _nf15 = {s_.no: RX._stage_title(RX.stage_names(rf.stages), s_) for s_ in rf.stages}
        _ov15 = overall_minutes(rf, valid=lambda o_: RX.is_report_valid(o_, rf.params, _nf15),
                                phase=lambda o_: RX.overall_phase(o_, _nf15), ext_scope="minute")
        _ov15p = overall_minutes(rf, valid=lambda o_: RX.is_report_valid(o_, rf.params, _nf15),
                                 phase=lambda o_: RX.overall_phase(o_, _nf15))
        _fm = [m_ for m_ in _ov15 if m_.obs is not None and RX.status_text(m_.obs, _nf15) == "故障"]
        _fmp = [m_ for m_ in _ov15p if m_.obs is not None and RX.status_text(m_.obs, _nf15) == "故障"]
        _mix = [m_.T for m_ in _fm if any(RX.overall_phase(v_[1], _nf15) != RX.overall_phase(m_.obs, _nf15)
                                          for v_ in m_.ext.values())]
        # 2026-11-03 正式需求：整體頁極值＝完整分鐘（故障不隔離極值）；一般欄位仍保留故障語意
        check(f"15b 082805 整體頁故障分鐘（{len(_fm)} 列）：一般欄位仍全部取自故障觀測；四個極值＝完整分鐘全部 HMI 候選的 MAX／MIN"
              f"（其中 {len(_mix)} 分鐘的極值來自同分鐘其他語意）；phase 舊行為仍只用故障語意",
              _fm and [m_.T for m_ in _fm] == [m_.T for m_ in _fmp]
              and all(m_.obs is mp_.obs for m_, mp_ in zip(_fm, _fmp))
              and all({k_: v_[0] for k_, v_ in m_.ext.items()} == {k_: v_[0] for k_, v_ in full_ext(rf, m_.T).items()}
                      for m_ in _fm)
              and all(RX.overall_phase(v_[1], _nf15) == RX.overall_phase(m_.obs, _nf15)
                      for m_ in _fmp for v_ in m_.ext.values()))
        by_seq = {r_[0]: r_ for r_ in fobs}
        check("082805：17:55:49（3138）、17:56:34（3142）PCS 缺值（HMI 正常）→ Raw Data 照常輸出、PCS N/A；"
              "前後 3137／3139、3141／3143 保留且 PCS 有值（不互相補值）",
              rtf.records[3137]["t"].endswith("17:55:49") and rtf.records[3141]["t"].endswith("17:56:34")
              and {"17:55:49", "17:56:34"} <= ftimes
              and all(by_seq[s_][RH.index("PCS 模組溫度")] == "N/A" for s_ in (3138, 3142))
              and all(by_seq[s_][RH.index("PCS 模組溫度")] != "N/A" for s_ in (3137, 3139, 3141, 3143)))
        pcs_bad = {hm(r_[RH.index("時間")]): (r_[RH.index("PCS 模組溫度")], r_[RH.index("說明")]) for r_ in fobs
                   if r_[RH.index("PCS 模組溫度")] in ("N/A", "FAIL")}
        check("082805：傳感器（選用）全部 N/A 仍輸出；PCS 模組溫度缺值只有原始缺值的 5 筆："
              "13:43:41 記錄 6161 讀取失敗 → FAIL＋實際原因；其餘 4 筆 6161 回傳「-」→ N/A＋說明；HMI 必要欄位沒有任何 N/A",
              all(r_[RH.index("傳感器溫度")] == "N/A" for r_ in fobs)
              and set(pcs_bad) == {"13:24:55", "13:43:41", "16:41:16", "17:55:49", "17:56:34"}
              and pcs_bad["13:43:41"][0] == "FAIL"
              and pcs_bad["13:43:41"][1] == ("分析狀態：休息；電池上下電：已上電；"      # 既有說明保留，PCS 原因附在後面
                                             "PCS 模組溫度 FAIL（資料取得失敗）：6161 讀取失敗：URLError: <urlopen error timed out>")
              and all(v_[0] == "N/A" and v_[1].endswith("PCS 模組溫度 N/A：6161 回傳非數值（-）")
                      for k_, v_ in pcs_bad.items() if k_ != "13:43:41")
              and not any(r_[RH.index(h)] == "N/A" for r_ in fobs
                          for h in ("SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓",
                                    "總電壓", "功率 kW")))
        check("082805：08:57:22 在 observations.jsonl（第 165 筆）存在、Raw Data 不存在；08:57:12、08:57:33 仍在",
              j165["t"].endswith("08:57:22") and "08:57:22" not in ftimes
              and {"08:57:12", "08:57:33"} <= ftimes)
        seq_by_t = {r_[RH.index("時間")].strftime("%H:%M:%S"): r_[0] for r_ in fobs}
        check("082805 Traceability：08:57:12 → 序號 164、08:57:33 → 序號 166（165 跳號）；序號＝observations.jsonl 列號",
              seq_by_t["08:57:12"] == 164 and seq_by_t["08:57:33"] == 166
              and rf.obs[163].src == "observations.jsonl:164" and rf.obs[165].src == "observations.jsonl:166"
              and rtf.records[163]["t"].endswith("08:57:12") and rtf.records[165]["t"].endswith("08:57:33"))
        check("082805 Raw Data 不含「來源」欄、沒有 observations.jsonl:* 文字",
              "來源" not in RH and not any("observations.jsonl" in str(v) for r_ in fobs for v in r_ if v is not None))
        check("7 observations.jsonl 未修改（SHA256 前後一致，複本 = 原始）",
              sha(src_obs) == sha0 == sha(os.path.join(froot, cid, "observations.jsonl")))
        spans = [RX.chart_span(wbf, {s_.no: n_ for s_, n_ in zip(rf.stages, [n_ for n_ in wbf.sheetnames
                                                                           if re.match(r"\d+\.", n_)])},
                               {s_.no: len(stage_minutes(rf, s_)) for s_ in rf.stages}, s_) for s_ in rf.stages]
        hmm = lambda t: t.strftime("%H:%M")
        check(f"1～4 082805 四張圖 Category：{[(hmm(a_), hmm(b_)) for a_, b_ in spans]}",
              [(hmm(a_), hmm(b_)) for a_, b_ in spans]
              == [("08:34", "10:37"), ("10:39", "12:42"), ("14:39", "16:45"), ("16:49", "18:05")])   # 起始不完整分鐘納入
        zf = zipfile.ZipFile(os.path.join(tmp, "field.xlsx"))
        sum_rel = zf.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
        sum_dr = re.search(r'Target="[^"]*drawings/(drawing\d+\.xml)"', sum_rel).group(1)
        dr_rel = zf.read(f"xl/drawings/_rels/{sum_dr}.rels").decode("utf-8")
        ch_files = re.findall(r'Target="[^"]*charts/(chart\d+\.xml)"', dr_rel)
        stage_sheets = [n_ for n_ in wbf.sheetnames if re.match(r"\d+\.", n_)]
        labs, vals, skips = [], [], []
        for cf in ch_files[:4]:
            x_ = zf.read(f"xl/charts/{cf}").decode("utf-8")
            cat_ = re.search(r"<(?:c:)?cat>(.*?)</(?:c:)?cat>", x_, re.S).group(1)
            pts = re.findall(r'<(?:c:)?pt idx="\d+">\s*<(?:c:)?v(?:\s*/>|>([^<]*)</)', cat_)   # 空標籤寫成 <v />
            labs.append(pts)
            vals.append(re.findall(r"<(?:c:)?val>.*?<(?:c:)?f>([^<]*)</", x_, re.S))
            skips.append(re.search(r'<(?:c:)?tickLblSkip val="(\d+)"', x_).group(1))
        vis = [(next(v for v in l_ if v), [v for v in l_ if v][-1]) for l_ in labs]
        check(f"8 Summary 圖表 X 軸可見首尾標籤 = Category 首尾（同 chart_span）：{vis}，每個標籤都顯示（tickLblSkip=1）",
              vis == [(hmm(a_), hmm(b_)) for a_, b_ in spans] and labs[0][0] == "08:34" and labs[0][-1] == "10:37"
              and skips == ["1"] * 4 and [len(l_) for l_ in labs] == [len(stage_minutes(rf, s_)) for s_ in rf.stages])
        diff_vis = [int((datetime.strptime(vis[k + 1][0], "%H:%M") - datetime.strptime(vis[k][1], "%H:%M")).total_seconds()
                        // 60) for k in range(3)]
        sp = re.findall(r"<a:t>([^<]*)</a:t>", zf.read(f"xl/drawings/{sum_dr}").decode("utf-8"))
        check(f"1～3/5～7 畫面可見時間差 {diff_vis} = Summary 箭頭 {sp}（10:37→10:39、12:42→14:39、16:45→16:49；"
              "Calendar Minute 起始分鐘納入後 3／118／5 分 → 2／117／4 分）",
              sp == [f"{d_} 分" for d_ in diff_vis] == ["2 分", "117 分", "4 分"])
        check("4 溫度系列仍參照階段頁輔助欄 AA／AB／AC（數值不變、沒有新增資料點）",
              vals == [[f"'{n_}'!${c_}$2:${c_}${len(stage_minutes(rf, s_)) + 1}" for c_ in ("AA", "AB", "AC")]
                       for n_, s_ in zip(stage_sheets, rf.stages)])
        st_charts = [c_ for n_ in stage_sheets for c_ in wbf[n_]._charts]
        st_labs = []
        for n_ in stage_sheets:
            c_ = wbf[n_]._charts[0]
            st_labs.append(["" if v.v in (None, "None") else v.v for v in c_.series[0].cat.strLit.pt])   # openpyxl 讀回空 <v /> 為 "None"
        check(f"7 082805 階段頁圖表 X 軸首尾 = 真正首尾 Category = Summary：{[(l_[0], l_[-1]) for l_ in st_labs]}",
              [(l_[0], l_[-1]) for l_ in st_labs] == vis and st_labs == labs
              and all(c_.x_axis.tickLblSkip == 1 for c_ in st_charts))
        check("8 階段頁圖表溫度系列仍參照 AA／AB／AC、類別筆數不變（只改 X 軸標籤顯示）",
              [[s_.val.numRef.f for s_ in c_.series] for c_ in st_charts] == vals
              and [len(l_) for l_ in st_labs] == [len(stage_minutes(rf, s_)) for s_ in rf.stages])
        rf2 = rtf.analysis()
        check("9/20 Stage Detection／邊界不變（重新分析 start／A／end_B／end_by 相同）",
              bounds0 == [(s_.start.t, s_.A.t, s_.end_B.t if s_.end_B else None, s_.end_by) for s_ in rf2.stages])
        _orig = RX.raw_rows_obs
        RX.raw_rows_obs = lambda r_, p_, n_: list(r_.obs)        # 對照組：不過濾
        try:
            wbu = load_workbook(save_report(rf2, meta_f, os.path.join(tmp, "field_unfiltered.xlsx")))
        finally:
            RX.raw_rows_obs = _orig
        others = [n_ for n_ in wbf.sheetnames if n_ != "Raw Data"]
        check("10/20 Raw Data Filter 不影響其他頁籤（Summary／數據概覽／即時熱度／階段頁／整體測試 內容相同）",
              wbf.sheetnames == wbu.sheetnames and all(
                  [[c.value for c in row] for row in wbf[n_].iter_rows()]
                  == [[c.value for c in row] for row in wbu[n_].iter_rows()] for n_ in others)
              and wbu["Raw Data"].max_row - wbf["Raw Data"].max_row == len(rf.obs) - len(fin))
        check("20 階段頁／整體頁列數 = 每分鐘選樣數（圖表資料範圍不變）",
              all(wbf[n_].max_row - 1 == len(stage_minutes(rf, s_))
                  for n_, s_ in zip([n_ for n_ in wbf.sheetnames if re.match(r"\d+\.", n_)], rf.stages))
              and wbf["整體測試"].max_row - 1 == len(overall_minutes(rf)))
        from cert_stage_engine import stage_names as _sn2
        _nf = {s_.no: RX._stage_title(_sn2(rf.stages), s_) for s_ in rf.stages}
        _v = lambda o_: RX.is_report_valid(o_, rf.params, _nf)
        _st = [stage_minutes(rf, s_, valid=_v) for s_ in rf.stages]
        check(f"N15b 082805：階段頁 {[len(r_) for r_ in _st]} 列（起始分鐘納入）、一般欄位全部取自有效觀測、PCS N/A 0、"
              "最後一列一般欄位 = A 點",
              [len(r_) for r_ in _st] == [124, 124, 127, 77]
              and all(m_.obs is not None and _v(m_.obs) for r_ in _st for m_ in r_)
              and all(r_[-1].obs is s_.A for r_, s_ in zip(_st, rf.stages)))
        _ov = {m_.T.strftime("%H:%M"): m_ for m_ in overall_minutes(rf, valid=_v,
                                                                    phase=lambda o_: RX.overall_phase(o_, _nf))}
        _w18 = [o_ for o_ in rf.obs if datetime(2026, 10, 29, 18, 13) <= o_.t < datetime(2026, 10, 29, 18, 14)]
        check("N16 082805 整體頁 18:13：18:13:00～18:13:59 完全沒有 observation → N/A，原因「該分鐘內無取樣」"
              "（不跨回 18:12:46）；18:14 為讀取失敗",
              not _w18 and _ov["18:13"].obs is None and _ov["18:13"].na_reason == "該分鐘內無取樣"
              and _ov["18:14"].obs is None and _ov["18:14"].na_reason == "該分鐘內讀取失敗"
              and _ov["18:12"].obs is not None and len(_ov) == 587)
    else:
        print("  （略過：本機沒有 CERT_20261029_082805）")

    print("\n== Field 資料 CERT_20261102_092500：階段頁選樣 × 正式報表有效判定（暫存複本；不動原始資料）==")
    real9 = os.path.join(HERE, "..", "output", "cert_reports", "CERT_20261102_092500")
    if os.path.isdir(real9):
        import hashlib
        import cert_report_xlsx as RX
        from cert_runtime import CertRuntime
        from cert_stage_engine import has_hmi_values, stage_minutes, stage_names
        sha9 = lambda f: hashlib.sha256(open(f, "rb").read()).hexdigest()
        cid9 = "CERT_20261102_092500"
        sha9_0 = sha9(os.path.join(real9, "observations.jsonl"))
        root9 = os.path.join(tmp, "field9")
        shutil.copytree(real9, os.path.join(root9, cid9), ignore=shutil.ignore_patterns("reports", "screenshots"))
        r9 = CertRuntime(root9, reader=None, enable_shots=False, log=lambda *a, **k: None)._offline(
            os.path.join(root9, cid9)).analysis()
        p9 = r9.params
        nf9 = {s_.no: RX._stage_title(stage_names(r9.stages), s_) for s_ in r9.stages}
        v9 = lambda o_: RX.is_report_valid(o_, p9, nf9)
        from cert_stage_engine import minute_value, overall_minutes
        pf9 = lambda o_: RX.has_pcs_temp(o_, p9)                                         # 正式報表選樣優先條件（方案 B）
        new9 = [stage_minutes(r9, s_, valid=v9, prefer=pf9) for s_ in r9.stages]
        ov9 = overall_minutes(r9, valid=v9, phase=lambda o_: RX.overall_phase(o_, nf9), prefer=pf9)
        pna = lambda rows: sum(minute_value(m_, "pcs_module_c", p9) is None for m_ in rows)
        check(f"V13 092500：階段頁 {[len(r_) for r_ in new9]} = {sum(map(len, new9))} 列（起始不完整分鐘納入）、"
              f"整體頁 {len(ov9)} 列、階段頁 PCS N/A {[pna(r_) for r_ in new9]}",
              [len(r_) for r_ in new9] == [127, 125, 128, 127] and sum(map(len, new9)) == 507 and len(ov9) == 638
              and [pna(r_) for r_ in new9] == [0, 0, 0, 0]
              and [r_[0].T.strftime("%H:%M") for r_ in new9] == ["09:26", "11:35", "15:40", "17:54"])
        m1131 = next(m_ for m_ in new9[0] if m_.T.strftime("%H:%M") == "11:31")
        src = m1131.ext["cell_vmin_v"][1]
        check(f"V14 092500 Extreme Policy B：Stage 1 11:31 Cell 最小 = {m1131.ext['cell_vmin_v'][0]} V"
              f"（來源 {src.t:%H:%M:%S} #{src.seq + 1}，PCS 缺值但 HMI 有效 → 現在也進 Raw Data）；"
              "一般欄位優先取含 PCS 的有效觀測",
              m1131.ext["cell_vmin_v"][0] == 2.861 and src.seq + 1 == 677 and v9(src) and v9(m1131.obs)
              and pf9(m1131.obs)
              and all(r_[-1].obs is s_.A for r_, s_ in zip(new9, r9.stages)))
        legacy = [o_ for o_ in r9.obs if o_.ok and has_hmi_values(o_)                     # 方案 B 前的條件（PCS 必要）
                  and all(RX.field_value(o_, k_, p9) is not None for k_ in RX.RAW_REQUIRED + ["pcs_module_c"])
                  and RX.status_text(o_, nf9) != RX.NA]
        raw9 = RX.raw_rows_obs(r9, p9, nf9)
        added9 = [o_ for o_ in raw9 if o_ not in legacy]
        check(f"V16 092500：Raw Data＝修改前 3213 筆＋只因 PCS 缺值被排除的 HMI 有效取樣 {len(added9)} 筆"
              f"（共 {len(raw9)} 筆；新增者 PCS 全部缺值、其他必要欄位齊全）",
              len(legacy) == 3213 and all(o_ in raw9 for o_ in legacy) and len(raw9) == 3213 + len(added9)
              and added9 and all(not pf9(o_) for o_ in added9))
        rid = {id(o_) for o_ in raw9}
        check("V16b 092500：每一個階段頁選用觀測都在 Raw Data 有效集合內（不再使用 Raw Data 已排除的觀測）",
              all(id(m_.obs) in rid for r_ in new9 for m_ in r_ if m_.obs) and all(m_.obs for r_ in new9 for m_ in r_))
        check("092500 原始 observations.jsonl 未被修改（SHA256 相同）",
              sha9(os.path.join(real9, "observations.jsonl")) == sha9_0)
    else:
        print("  （略過：本機沒有 CERT_20261102_092500）")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

ok = all(_results)
print(f"\n== 認證報告 P1 Excel 格式 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
