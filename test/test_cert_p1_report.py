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
from cert_report_xlsx import (LOG_HEADERS, _cn_num, arrow_text, build_workbook, overall_title, rest_minutes_text, save_report,
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
    for s in range(0, 1201, 10):                       # 10:00～10:20 放電，10:10:00～10:10:40 停頓
        if 600 <= s <= 640:
            o_ = obs_at(s, "I", cell_vmax_v=3.4015, cell_vmin_v=3.3852)
            o_.flags = {"battery_power_status": "已上電"}  # 停頓期間電池已上電 → 狀態可判定（待機）、必要欄位齊全
            o.append(o_)
        elif 290 <= s <= 310:
            o.append(obs_at(s, "D", sensor_c=None))     # 10:05 前後傳感器無值 → N/A
        elif 890 <= s <= 910:
            o.append(obs_at(s, "D", soc=0))              # 10:15 前後 SOC 有效 0
        elif 1060 <= s <= 1100:
            o.append(obs_at(s, "D", pcs_module_c=None))  # 10:18 ±20 秒內 PCS 模組溫度全部無值 → 無完整有效取樣
        else:
            o.append(obs_at(s, "D"))
    o += [obs_at(s, "I") for s in range(1210, 1501, 10)]                 # 休息
    for s in range(1510, 2401, 10):                                       # 10:25:10～10:40 充電
        o.append(obs_at(s, "X") if 1780 <= s <= 1820 else                 # 10:29:40～10:30:20 讀取失敗
                 obs_at(s, "C", cell_vmax_v=None, cell_vmin_v=None) if 2080 <= s <= 2120 else  # 10:35 前後舊資料沒有 Cell 電壓
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
    check("±20 秒內 PCS 模組溫度全部無值 → 整列 N/A＋原因「無完整有效取樣」（不虛構、不沿用鄰近值）",
          all(ws.cell(r18, c).value == "N/A" for c in range(2, NOTE))
          and "無完整有效取樣" in str(ws.cell(r18, NOTE).value) and ws.cell(2, COL["PCS 模組溫度"]).value == 60.0)
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
    check("階段頁各 1 張圖表（短階段除外）",
          len(wb["1.第1次放電"]._charts) == 1 and len(wb["2.第1次充電"]._charts) == 1)
    ws3 = wb["3.第2次放電"]
    check("短階段：無整分鐘 Log、不畫圖，保留頭尾觀測值與說明",
          len(ws3._charts) == 0 and "無整分鐘 Log" in str(ws3["A1"].value)
          and "首筆觀測" in str(ws3["A4"].value))
    ws2 = wb["2.第1次充電"]
    r30 = row_of(ws2, "10:30")
    check("±20 秒內只有讀取失敗 → 整列 N/A 並寫明原因",
          ws2.cell(r30, 2).value == "N/A" and "讀取失敗" in str(ws2.cell(r30, NOTE).value))
    r35 = row_of(ws2, "10:35")
    check("±20 秒內只有舊資料（沒有 Cell 電壓）→ 整列 N/A＋原因「無完整有效取樣」（不以總電壓代替、不補值）",
          all(ws2.cell(r35, c).value == "N/A" for c in range(2, NOTE))
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
    o_nh = obs_at(110, "D", **no_hmi)                 # 舊資料型態：ok=True、沒有 hmi_ok 欄位、HMI 全無值
    o_nh.notes = ["HMI 讀取失敗：無讀值（未登入或連線失敗）"]
    o_part = obs_at(310, "D", soc=None, current_a=None, voltage_v=None, cell_vmax_v=None, cell_vmin_v=None)
    o_pcs = obs_at(330, "D", pcs_module_c="-")        # 現場型態：6161 回傳 "-"（HMI 正常）
    o_sen = obs_at(350, "D", sensor_c=None)            # 傳感器關閉（選用欄位）
    nh = analyze([obs_at(s, "D") for s in range(0, 91, 10)] + [o_nh]
                 + [obs_at(s, "D") for s in range(150, 301, 10)] + [o_part] + [obs_at(320, "D"), o_pcs,
                                                                                obs_at(340, "D"), o_sen]
                 + [obs_at(s, "D") for s in range(360, 401, 10)], p)
    wb_nh = load_workbook(save_report(nh, META, os.path.join(tmp, "nohmi.xlsx")))
    wn = wb_nh["整體測試"]
    rw = wb_nh["Raw Data"]
    RH = [c.value for c in rw[1]]
    raw_by_t = {r_[RH.index("PC 取樣時間")]: r_ for r_ in rw.iter_rows(min_row=2, values_only=True)}
    from cert_report_xlsx import RAW_REQUIRED
    from cert_stage_engine import _num, has_hmi_values
    raw_obs_rows = [r_ for r_ in rw.iter_rows(min_row=2, values_only=True) if r_[RH.index("讀取結果")] != "（無取樣）"]
    check("Raw Data 必要欄位 = SOC／電流／Rack 最大／Rack 最小／Cell 最大／Cell 最小／總電壓／PCS 模組／功率（傳感器為選用）",
          RAW_REQUIRED == ["soc", "current_a", "rack_max_c", "rack_min_c", "cell_vmax_v", "cell_vmin_v", "voltage_v",
                           "pcs_module_c", "power_kw"])
    check("11 Raw Data Filter：ok=True 但 HMI 全無值（僅 6161 PCS 模組溫度 80 有效）→ 整筆不輸出（不是改空白）",
          o_nh.t not in raw_by_t and not any("無有效讀值" in str(v) for r_ in raw_obs_rows for v in r_))
    check("12 HMI 有效但必要欄位缺值（SOC／電流／電壓／Cell 電壓 N/A）→ 整筆不輸出", o_part.t not in raw_by_t)
    check("12 HMI 有效、只有 PCS 模組溫度為 \"-\"（6161 未給值）→ 整筆不輸出", o_pcs.t not in raw_by_t)
    _p, _n = raw_by_t.get(o_pcs.t - timedelta(seconds=10)), raw_by_t.get(o_pcs.t + timedelta(seconds=10))
    check("13/15 前後 PCS 模組溫度有效（60）→ 保留；被排除那筆不以前後值補入、不留空白列",
          _p is not None and _n is not None and _p[RH.index("PCS 模組溫度")] == 60.0
          and _n[RH.index("PCS 模組溫度")] == 60.0
          and not any(r_[RH.index("PC 取樣時間")] == o_pcs.t for r_ in raw_obs_rows))
    rs_ = raw_by_t.get(o_sen.t)
    check("14 傳感器（選用）N/A、其他必要欄位齊全 → 整筆保留、傳感器顯示 N/A",
          rs_ is not None and rs_[RH.index("讀取結果")] == "成功" and rs_[RH.index("傳感器溫度")] == "N/A")
    expect = [o for o in nh.obs if o.ok and has_hmi_values(o)
              and all(_num(o.values.get(k)) is not None for k in RAW_REQUIRED)]
    check(f"Raw Data 觀測列數 = HMI 有效且必要欄位齊全的觀測數（{len(raw_obs_rows)} = {len(expect)}；全部 {len(nh.obs)}），"
          "沒有任何必要欄位為 N/A、不留空白列",
          len(raw_obs_rows) == len(expect) == len(nh.obs) - 3
          and all(r_[0] is not None and r_[1] is not None for r_ in raw_obs_rows)
          and not any(r_[RH.index(h)] == "N/A" for r_ in raw_obs_rows
                      for h in ("SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓",
                                "總電壓", "PCS 模組溫度", "功率 kW")))
    seqs = [r_[RH.index("序號")] for r_ in raw_obs_rows]
    check("17 Traceability：序號沿用原觀測 seq（跳過被排除者、不重新編號），序號＝observations.jsonl 列號（內部 src 仍保留）",
          o_nh.seq + 1 not in seqs and o_nh.seq in seqs and o_nh.seq + 2 in seqs
          and o_pcs.seq + 1 not in seqs and o_pcs.seq in seqs and o_pcs.seq + 2 in seqs
          and all(nh.obs[r_[0] - 1].t == r_[RH.index("PC 取樣時間")]
                  and nh.obs[r_[0] - 1].src for r_ in raw_obs_rows))
    check("1/3/4 Raw Data 欄位：不含「來源」，讀取結果緊接 PC 取樣時間，其後順序正確",
          RH == ["序號", "PC 取樣時間", "讀取結果", "狀態", "所屬階段", "對應報表分鐘", "SOC", "電流",
                 "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓", "總電壓", "傳感器溫度",
                 "PCS 模組溫度", "功率 kW", "說明"])
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
    st_by_t = {r_[SH.index("PC 取樣時間")]: r_ for r_ in rws_.iter_rows(min_row=2, values_only=True)
               if r_[SH.index("讀取結果")] != "（無取樣）"}
    stv = lambda o_: (st_by_t.get(o_.t) or [None] * len(SH))[SH.index("狀態")]
    check("1/2 階段內充電／放電 → 第1次充電／第1次放電（依階段，非依數值）",
          all(stv(o_) == "第1次放電" for o_ in s_d) and all(stv(o_) == "第1次充電" for o_ in s_c[1:-1]))
    check("3/4 階段外：電池已上電 → 待機、已下電 → 停機", stv(s_on) == "待機" and stv(s_off) == "停機")
    check("5 狀態欄不出現 N/A（可判定者都顯示正確狀態）",
          "N/A" not in {r_[SH.index("狀態")] for r_ in st_by_t.values()})
    check("6 上下電未知／PCS 故障／停止後充放電旗標未歸零 → 狀態無法判定 → 整筆不輸出",
          all(o_.t not in st_by_t for o_ in (s_unk, s_flt, s_lag)))
    check("7/8/9 不以數值猜狀態：SOC 0、電流 0、功率 0 但沒有上下電資料 → 不判為待機／停機（不輸出）；SOC 100 → 待機（非滿充）",
          s_zero.t not in st_by_t and stv(s_on) == "待機")
    check("12 選用傳感器 N/A 不影響狀態：已上電 → 待機、整筆保留", stv(s_sen) == "待機"
          and st_by_t[s_sen.t][SH.index("傳感器溫度")] == "N/A")
    seq_s = [r_[0] for r_ in st_by_t.values()]
    check("15/16 序號沿用原 seq（被排除者跳號、不重新編號），序號指回原觀測",
          all(o_.seq + 1 not in seq_s for o_ in (s_unk, s_flt, s_lag, s_zero))
          and s_sen.seq + 1 in seq_s and s_zero.seq + 2 in seq_s
          and all(rs.obs[r_[0] - 1].t == r_[SH.index("PC 取樣時間")] for r_ in st_by_t.values()))
    check("13 狀態欄 Filter 不改 Stage Detection（同資料去掉 flags 重新分析，階段與 state 相同）",
          [(st.start.t, st.A.t, st.end_by) for st in rs.stages]
          == [(st.start.t, st.A.t, st.end_by) for st in analyze(
              [obs_at(int((o_.t - T0).total_seconds()), {"C": "C", "D": "D"}.get(o_.direction, "I"),
                      **o_.values) for o_ in rs.obs], p).stages])

    rn = row_of(wn, "10:02")
    check("整體頁：±20 秒內只有 HMI 無讀值觀測 → HMI 欄位 N/A、保留 6161 溫度，備註寫明原因（不只寫 N/A）",
          wn.cell(rn, COL["SOC"]).value == "N/A" and wn.cell(rn, COL["PCS 模組溫度"]).value == 80
          and wn.cell(rn, NOTE).value == "N/A：HMI 無有效讀值（通訊異常或未登入），僅有 6161 溫度")

    print("\n== Summary ==")
    ss = wb["Summary"]
    check("A1～A3 測試條件、A4「溫度曲線：」", ss["A4"].value == "溫度曲線：" and ss["A1"].value)
    anchors = [c.anchor._from.row for c in ss._charts]
    check("Summary 圖表 3 張（2 個階段 ＋ 整體；短階段以文字標示）",
          len(ss._charts) == 3 and any("不足一分鐘" in str(c.value) for row in ss.iter_rows()
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
    w1, w2 = wb["1.第1次放電"], wb["2.第1次充電"]
    last1 = w1.cell(w1.max_row, 1).value
    first2 = w2.cell(2, 1).value
    exp1 = f"{int((first2 - last1).total_seconds() // 60)} 分"
    check(f"箭頭 = 下一張圖第一個 Category − 上一張圖最後一個 Category（{last1:%H:%M} → {first2:%H:%M} ＝「{exp1}」）；"
          "下一階段不足一分鐘沒有圖表 → N/A",
          [s[2] for s in shapes] == [[exp1], ["N/A"]] and exp1 == "6 分")
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
          [c_.x_axis.tickLblSkip for c_ in ws_s._charts] == [1, 1, None])
    _rot = lambda c_: c_.x_axis.txPr.bodyPr.rot if c_.x_axis.txPr is not None else None
    check("Summary 四張階段圖 X 軸文字樣式一致：固定直式（rot=-5400000，同階段頁 Excel 自動呈現）、nextTo、10pt",
          [_rot(c_) for c_ in ws_s._charts[:2]] == [-5400000] * 2
          and [c_.x_axis.tickLblPos for c_ in ws_s._charts[:2]] == ["nextTo"] * 2
          and all(c_.x_axis.txPr.p[0].pPr.defRPr.sz == 1000 for c_ in ws_s._charts[:2]))
    check("Summary 整體圖與「整體測試」頁圖表維持原樣：不設文字樣式／tickLblSkip（方向與間隔由 Excel 自動）、類別仍參照 A 欄",
          all(c_.x_axis.txPr is None and c_.x_axis.tickLblPos is None and c_.x_axis.tickLblSkip is None
              and c_.series[0].cat.numRef is not None for c_ in [ws_s._charts[2]] + wb["整體測試"]._charts))
    _cats = lambda c_: [v.v for v in c_.series[0].cat.strLit.pt]
    sh1, sh2 = wb["1.第1次放電"]._charts[0], wb["2.第1次充電"]._charts[0]
    check("1 Summary 階段圖與對應階段頁圖表使用同一 X 軸規則：標籤文字、直式、nextTo、10pt、tickLblSkip=1 全部相同",
          _cats(ws_s._charts[0]) == _cats(sh1) and _cats(ws_s._charts[1]) == _cats(sh2)
          and all(_rot(c_) == -5400000 and c_.x_axis.tickLblPos == "nextTo" and c_.x_axis.tickLblSkip == 1
                  and c_.x_axis.txPr.p[0].pPr.defRPr.sz == 1000 for c_ in (sh1, sh2)))
    check("5/6/7 階段頁圖表首尾標籤 = 階段頁 A 欄第一／最後 Category（10:00／10:20、10:26／10:40）",
          (_cats(sh1)[0], _cats(sh1)[-1]) == ("10:00", "10:20") and (_cats(sh2)[0], _cats(sh2)[-1]) == ("10:26", "10:40")
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
          rg.stages[0].end_B is None and ag == ["68 分"])
    from cert_stage_engine import overall_minutes as _om
    gap_rows = [m for m in _om(rg) if T0 + timedelta(minutes=21) <= m.T <= T0 + timedelta(minutes=87)]
    check("同一缺口在全程資料仍為 N/A（箭頭有間隔不代表補值或連線）",
          gap_rows and all(m.obs is None for m in gap_rows))

    def overlap(b1, b2):
        return not (b1[2] < b2[0] or b2[2] < b1[0] or b1[3] < b2[1] or b2[3] < b1[1])
    check("箭頭與任何圖表的儲存格範圍都不重疊",
          all(not overlap(s[1], f) for s in shapes for f in frames))
    def title_of(ch):
        return "".join(r.t for p_ in ch.title.tx.rich.p for r in (p_.r or []))
    check("Summary 圖表標題：250KW 第一次放電、250KW 第一次充電（電櫃規格前綴；未標記 → 用方向）＋全程",
          [title_of(c) for c in ss._charts][:2] == ["250KW 第一次放電", "250KW 第一次充電"]
          and title_of(ss._charts[2]) == "【250KW】電櫃充放2個循環溫度曲線")
    check("整體溫度曲線標題：Summary 與「整體測試」頁一致＝「【250KW】電櫃充放2個循環溫度曲線」（規格取自測試資訊）",
          title_of(ss._charts[2]) == title_of(wb["整體測試"]._charts[0]) == "【250KW】電櫃充放2個循環溫度曲線")
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
    check("滿放＋滿充＋無標記＋滿充 → 250KW 第一次滿放／第一次滿充／第二次放電／第二次滿充（起始 SOC 100／0 且有人工標記）",
          titled([("D", 100, "滿放"), ("C", 0, "滿充"), ("D", 100, None), ("C", 0, "滿充")], spec)
          == ["250KW 第一次滿放", "250KW 第一次滿充", "250KW 第二次放電", "250KW 第二次滿充"])
    check("電櫃規格未輸入（無欄位／None／空白）→ 不加前綴，不顯示 None、N/A",
          all(titled([("D", 100, "滿放"), ("C", 0, None)], m_) == ["第一次滿放", "第一次充電"]
              for m_ in (None, {}, {"cabinet_spec": None}, {"cabinet_spec": "  "})))
    check("起始 SOC 不是精確 0／100（充電 SOC 1＋滿充、放電 SOC 99＋滿放）→ 充電／放電",
          titled([("C", 1, "滿充"), ("D", 99, "滿放")], spec) == ["250KW 第一次充電", "250KW 第一次放電"])
    check("首筆 SOC 缺值＋滿充／滿放標記 → 充電／放電（無法驗證起始條件）",
          titled([("C", None, "滿充"), ("D", None, "滿放")], spec) == ["250KW 第一次充電", "250KW 第一次放電"])
    check("無標記：起始 SOC 0／100 也不自動認定 → 充電／放電",
          titled([("C", 0, None), ("D", 100, None)], spec) == ["250KW 第一次充電", "250KW 第一次放電"])
    check("標記為未滿／中止 → 充電／放電",
          titled([("C", 0, "未滿"), ("D", 100, "中止"), ("C", 0, "中止"), ("D", 100, "未滿")], spec)
          == ["250KW 第一次充電", "250KW 第一次放電", "250KW 第二次充電", "250KW 第二次放電"])
    check("標記方向不符（舊資料繞過防呆：放電標滿充、充電標滿放）→ 不採用，仍為放電／充電",
          titled([("D", 100, "滿充"), ("C", 0, "滿放")], spec) == ["250KW 第一次放電", "250KW 第一次充電"])
    check("次數依充／放電方向計數、不受標記影響：滿放／放電／滿充／滿放 → 第一次／第二次／第一次／第三次",
          titled([("D", 100, "滿放"), ("D", 100, None), ("C", 0, "滿充"), ("D", 100, "滿放")], spec)
          == ["250KW 第一次滿放", "250KW 第二次放電", "250KW 第一次滿充", "250KW 第三次滿放"])
    check("中文序數：1→一、10→十、11→十一、23→二十三",
          [_cn_num(n) for n in (1, 10, 11, 23)] == ["一", "十", "十一", "二十三"])

    print("\n== 截圖頁 ==")
    sv = wb["數據概覽"].sheet_view
    check("數據概覽：切換到本頁時從頂端開始（topLeftCell=A1、選取 A1）；活頁簿預設頁仍為 Summary",
          sv.topLeftCell == "A1" and sv.selection[0].activeCell == "A1" and wb.active.title == "Summary")
    for name in ("數據概覽", "即時熱度"):
        w = wb[name]
        ph = [c.value for row in w.iter_rows() for c in row if c.value and "待截圖" in str(c.value)]
        check(f"{name}：每個階段 1 個「待截圖」標示（共 3），沒有任何圖片",
              len(ph) == 3 and not w._images and all("不使用範本" in v for v in ph))
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        check("檔案內沒有任何圖片（xl/media）", not any(n.startswith("xl/media/") for n in names))
        charts = [n for n in names if re.match(r"xl/charts/chart\d+\.xml$", n)]
        xmls = [z.read(n).decode("utf-8") for n in charts]

    print("\n== 圖表 XML ==")
    check("圖表總數 2（階段頁，短階段無圖）＋1（整體頁）＋3（Summary）＝ 6", len(xmls) == 6)
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
    check("Raw Data：Rack 最小溫度後加入 Cell 最大／最小電壓，說明欄在最後（Q，移除來源後左移一欄）",
          hdr[H["Rack 最小溫度"]:H["Rack 最小溫度"] + 3] == ["Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓"]
          and hdr[-1] == "說明" and len(hdr) == 17 and rw.auto_filter.ref.startswith("A1:Q"))
    _ok = [r for r in rows if r[H["讀取結果"]] == "成功" and r[H["Cell 最大電壓"]] not in (None, "N/A")]
    check(f"Raw Data Cell 電壓保留原始值（{_ok[0][H['Cell 最大電壓']] if _ok else None}）",
          _ok and _ok[0][H["Cell 最大電壓"]] == 3.4015 and _ok[0][H["Cell 最小電壓"]] == 3.3852)
    obs_rows = [r for r in rows if r[H["讀取結果"]] in ("成功", "失敗")]
    na_rows = [r for r in rows if r[H["讀取結果"]] == "（無取樣）"]
    from cert_report_xlsx import _stage_title, raw_rows_obs
    from cert_stage_engine import stage_names as _sn
    nf_ = {st.no: _stage_title(_sn(res.stages), st) for st in res.stages}
    check(f"Raw Data 只輸出有效報告資料：讀取失敗 5 筆、必要欄位缺值、狀態無法判定者整筆不輸出（觀測列 {len(obs_rows)}），"
          "失敗原因仍在「（無取樣）」列",
          len(obs_rows) == len(raw_rows_obs(res, p, nf_)) and not any(r[H["讀取結果"]] == "失敗" for r in obs_rows)
          and len(obs_rows) < len(res.obs) - 5)
    check("Raw Data 保留實際取樣時間（含秒）", hdr[1] == "PC 取樣時間"
          and obs_rows[0][H["PC 取樣時間"]] == T0)
    m = [r for r in obs_rows if r[H["對應報表分鐘"]] and "階段頁「1.第1次放電」 10:05（+0 秒）" in r[H["對應報表分鐘"]]]
    check("階段頁每分鐘可對回 Raw Data 原始紀錄（10:05 → 10:05:00）",
          len(m) == 1 and m[0][H["PC 取樣時間"]] == T0 + timedelta(minutes=5))
    check("N/A 分鐘在 Raw Data 有「（無取樣）」列與原因",
          any("10:30（N/A）" in str(r[H["對應報表分鐘"]]) and "讀取失敗" in str(r[H["說明"]]) for r in na_rows))

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
    _rc = [_rh.index(h) for h in ("序號", "PC 取樣時間", "讀取結果")] + list(range(_rh.index("SOC"), _rh.index("說明")))
    raw1 = [[row[i].value for i in _rc] for row in wb["Raw Data"].iter_rows(min_row=2)]
    raw2 = [[row[i].value for i in _rc] for row in wb2["Raw Data"].iter_rows(min_row=2)]
    check("確認後 Raw Data 的原始觀測欄位（時間、來源、讀取結果、數值）完全相同", raw1 == raw2)
    path3 = save_report(analyze(scenario(), p), META, os.path.join(tmp, "r_again.xlsx"))
    wb3 = load_workbook(path3)
    same = all([[c.value for c in row] for row in wb[n].iter_rows()] ==
               [[c.value for c in row] for row in wb3[n].iter_rows()] for n in wb.sheetnames)
    check("同樣輸入重複產生 → 所有頁籤內容相同", same and wb.sheetnames == wb3.sheetnames)

    print("\n== 階段頁每分鐘選樣 × 正式報表有效判定（與 Raw Data 同一 predicate）==")
    import cert_report_xlsx as RXv
    from cert_stage_engine import stage_minutes as _sm, overall_minutes as _om, stage_names as _sn

    def mk(sec, cur=-140.0, flags=None, **v):
        vals = {"soc": 50, "current_a": cur, "rack_max_c": 40 + sec / 600, "rack_min_c": 30, "cell_vmax_v": 3.4,
                "cell_vmin_v": 3.38, "voltage_v": 1200.0 + sec / 10, "sensor_c": 25.0, "pcs_module_c": 60.0,
                "power_kw": cur * 1.2}
        vals.update(v)
        o_ = Obs(t=T0 + timedelta(seconds=sec), values=vals, src=f"syn:{sec}")
        o_.flags = dict(flags or {})
        return o_
    bad = {60: dict(pcs_module_c="-"),                                  # 10:01 PCS "-"（6161 現場型態）
           120: dict(voltage_v=None),                                   # 10:02 必要欄位（總電壓）缺值
           180: dict(sensor_c=None)}                                    # 10:03 傳感器缺值（選用欄位）
    bad.update({s_: dict(pcs_module_c="-") for s_ in range(280, 321, 10)})   # 10:05 ±20 秒內全部 PCS 缺值
    bad.update({s_: dict(pcs_module_c="-") for s_ in range(340, 371, 10)})   # 10:06 階段內全部缺值、只有休息有效
    vo = []
    for s_ in range(0, 371, 10):
        vo.append(mk(s_, 0.0 if s_ == 240 else -140.0, soc=50 + s_ // 10, **bad.get(s_, {})))   # 10:04 單筆停頓、狀態 N/A
        if s_ == 120:
            vo.append(mk(125, soc=99))                                  # 10:02:05 有效且最近
    vo += [mk(s_, 0.0, {"battery_power_status": "已上電"}) for s_ in range(380, 600, 10)]   # 階段結束後休息（有效、待機）
    vres = analyze(vo, p)
    vst = vres.stages[0]
    vnames = {st_.no: RXv._stage_title(_sn(vres.stages), st_) for st_ in vres.stages}
    vvalid = lambda o_: RXv.is_report_valid(o_, p, vnames)
    vrows = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(vres, vst, valid=vvalid)}
    vold = {m_.T.strftime("%H:%M"): m_ for m_ in _sm(vres, vst)}
    sec_of = lambda m_: None if m_.obs is None else int((m_.obs.t - T0).total_seconds())
    check("V1 Raw Data 與階段頁共用同一 predicate：raw_rows_obs = is_report_valid 過濾",
          RXv.raw_rows_obs(vres, p, vnames) == [o_ for o_ in vres.obs if vvalid(o_)])
    _iv = RXv.is_report_valid
    RXv.is_report_valid = lambda o_, p_, n_: _iv(o_, p_, n_) and o_.t != T0 + timedelta(minutes=3)   # 額外排除 10:03:00
    try:
        wv = load_workbook(save_report(vres, META, os.path.join(tmp, "valid_patched.xlsx")))
    finally:
        RXv.is_report_valid = _iv
    _wr = wv["Raw Data"]
    _vh = [c.value for c in _wr[1]]
    _rt = {r_[_vh.index("PC 取樣時間")] for r_ in _wr.iter_rows(min_row=2, values_only=True)}
    _w1 = wv[[n_ for n_ in wv.sheetnames if re.match(r"\d+\.", n_)][0]]
    _r03 = next(r_ for r_ in range(2, _w1.max_row + 1) if _w1.cell(r_, 1).value == T0 + timedelta(minutes=3))
    check("V1b 只改 is_report_valid（排除 10:03:00）→ Raw Data 與階段頁同時不再使用該筆（不需改兩處）",
          T0 + timedelta(minutes=3) not in _rt and _w1.cell(_r03, COL["SOC"]).value != 50 + 18)
    check("V2 PCS 模組溫度 \"-\" 的觀測不會成為階段頁選用觀測（10:01 改選 10:00:50）",
          sec_of(vold["10:01"]) == 60 and sec_of(vrows["10:01"]) == 50)
    check("V3 任一必要欄位缺值（總電壓）→ 不選用；改選 ±20 秒內最近的有效觀測（10:02 → 10:02:05）",
          sec_of(vold["10:02"]) == 120 and sec_of(vrows["10:02"]) == 125)
    check("V4 傳感器缺值仍可選用（選用欄位）：10:03 選 10:03:00、傳感器 N/A",
          sec_of(vrows["10:03"]) == 180 and RXv.field_value(vrows["10:03"].obs, "sensor_c", p) is None)
    o240 = next(o_ for o_ in vres.obs if o_.t == T0 + timedelta(seconds=240))
    check("V5 狀態無法判定（停頓、無上下電資料 → N/A）的觀測不選用（10:04 改選 10:03:50）",
          RXv.status_text(o240, vnames) == CFG.NA and sec_of(vold["10:04"]) == 240 and sec_of(vrows["10:04"]) == 230)
    tol_ = p["MINUTE_TOL_SEC"]
    check(f"V6 ±{tol_} 秒規則不變：所有選用觀測 |Δt| ≤ {tol_}；10:05 範圍外的有效觀測（10:04:30／10:05:30）不選用",
          all(abs((m_.obs.t - m_.T).total_seconds()) <= tol_ for m_ in vrows.values() if m_.obs)
          and vrows["10:05"].obs is None)
    check("V7 同階段／同方向規則不變：10:06 階段內無有效觀測時，不改選範圍內有效的休息觀測（10:06:20）",
          vrows["10:06"].obs is None and all(m_.obs.stage_no == vst.no and m_.obs.direction is not None
                                             for m_ in vrows.values() if m_.obs))
    check("V8 最近優先與同距取較早不變：10:01（−10／+10 秒同距）取 10:00:50；原選用觀測有效的分鐘與原規則相同",
          sec_of(vrows["10:01"]) == 50 and all(vrows[k_].obs is vold[k_].obs for k_ in vrows
                                               if vold[k_].obs is not None and vvalid(vold[k_].obs)))
    check("V9 無有效候選 → 分鐘列保留（列數不變）、整列 N/A",
          list(vrows) == list(vold) and vrows["10:05"].obs is None and vrows["10:06"].obs is None)
    check("V10 無有效候選的原因為「無完整有效取樣」（不是「僅有其他階段或休息資料」）",
          all(f"±{tol_} 秒內無完整有效取樣" in vrows[k_].na_reason and "其他階段" not in vrows[k_].na_reason
              for k_ in ("10:05", "10:06")))
    wv0 = load_workbook(save_report(vres, META, os.path.join(tmp, "valid.xlsx")))
    w1 = wv0[[n_ for n_ in wv0.sheetnames if re.match(r"\d+\.", n_)][0]]
    vr = {w1.cell(r_, 1).value.strftime("%H:%M"): [w1.cell(r_, c_).value for c_ in range(2, NOTE + 1)]
          for r_ in range(2, w1.max_row + 1) if isinstance(w1.cell(r_, 1).value, datetime)}
    check("V11 不 forward／backward fill：10:05、10:06 整列 N/A（不沿用鄰近值），備註寫明原因",
          all(all(v_ == "N/A" for v_ in vr[k_][:-1]) and "無完整有效取樣" in str(vr[k_][-1]) for k_ in ("10:05", "10:06")))
    o50 = vrows["10:01"].obs
    check("V12 改選時整列來自同一筆觀測（10:01 的 SOC／電流／Rack／Cell／總電壓／傳感器／PCS 全部 = 10:00:50）",
          sec_of(vrows["10:01"]) == 50 and vr["10:01"][0] == 55
          and all(abs(x_ - y_) < 1e-9 for x_, y_ in zip(vr["10:01"][:-1], [RXv.field_value(o50, k_, p) for k_ in RXv.LOG_KEYS])))
    print("\n== 整體頁選樣：report-valid preferred → legacy fallback（同語意範圍）==")
    ON, OFF = {"battery_power_status": "已上電"}, {"battery_power_status": "已下電"}
    FAULT = {"battery_power_status": "已下電", "pcs_fault_flag": True}
    PCS_NA = dict(pcs_module_c="-")
    oo = []
    for s_ in range(0, 301, 10):                                        # 10:00～10:05 放電
        oo.append(mk(s_, soc=50 + s_ // 10, **(PCS_NA if s_ in (60, 280, 290, 300) else
                                               dict(voltage_v=None) if s_ == 120 else {})))
        if s_ == 120:
            oo.append(mk(125, soc=99))
    for s_ in range(310, 601, 10):                                      # 10:05:10～10:10 休息（已上電）
        oo.append(mk(s_, 0.0, ON, **(PCS_NA if s_ in (360, 580, 590, 600) else {})))
    oo += [mk(s_, 135.0) for s_ in range(610, 901, 10)]                 # 10:10:10～10:15 充電
    for s_ in range(910, 1401, 10):                                     # 10:15:10 之後休息
        if 960 <= s_ <= 1000:
            fl_, kw_ = FAULT, {}                                       # 10:16 故障（狀態 N/A）
        elif 1080 <= s_ <= 1140:
            fl_, kw_ = OFF, (PCS_NA if s_ == 1080 else {})             # 10:18 下電（停機；10:18:00 PCS 缺值）
        elif 1200 <= s_ <= 1260:
            fl_, kw_ = {}, {}                                          # 10:20 舊資料沒有上下電狀態（N/A）
        elif 1300 <= s_ <= 1340:
            fl_, kw_ = ON, dict(soc=None, current_a=None, power_kw=None, rack_max_c=None, rack_min_c=None,
                               cell_vmax_v=None, cell_vmin_v=None, voltage_v=None)   # 10:22 只有 6161 溫度
        else:
            fl_, kw_ = ON, {}
        oo.append(mk(s_, 0.0, fl_, **kw_))
    ores = analyze(oo, p)
    onames = {st_.no: RXv._stage_title(_sn(ores.stages), st_) for st_ in ores.stages}
    ovalid = lambda o_: RXv.is_report_valid(o_, p, onames)
    ophase = lambda o_: RXv.overall_phase(o_, onames)
    oleg = {m_.T.strftime("%H:%M"): m_ for m_ in _om(ores)}
    onew = {m_.T.strftime("%H:%M"): m_ for m_ in _om(ores, valid=ovalid, phase=ophase)}
    check("O0 情境：放電／休息（已上電）／充電／休息（故障、下電、無上下電狀態、僅 6161）",
          [st_.dir for st_ in ores.stages] == ["D", "C"] and list(oleg) == list(onew))
    check("O1 原選用觀測無效（PCS \"-\"）→ 改選同語意 ±20 秒內最近的有效觀測（10:01 → 10:00:50；10:02 → 10:02:05）",
          sec_of(oleg["10:01"]) == 60 and sec_of(onew["10:01"]) == 50
          and sec_of(oleg["10:02"]) == 120 and sec_of(onew["10:02"]) == 125)
    check("O2 同語意範圍內沒有有效候選 → 沿用整體頁原規則選到的觀測（不變成 N/A）",
          all(onew[k_].obs is oleg[k_].obs and not ovalid(onew[k_].obs)
              for k_ in ("10:05", "10:10", "10:16", "10:20", "10:22")))
    check("O3 階段分鐘不跨階段：10:05 階段內 −20～0 秒全部無效、休息 10:05:10 有效 → 不改選休息",
          onew["10:05"].obs.stage_no == 1 and sec_of(onew["10:05"]) == 300)
    check("O4 改選前後所屬階段與方向一致（不跨 Direction）",
          all((onew[k_].obs.stage_no, onew[k_].obs.direction) == (oleg[k_].obs.stage_no, oleg[k_].obs.direction)
              for k_ in onew if onew[k_].obs is not oleg[k_].obs))
    check("O5 休息分鐘不改選階段運行觀測：10:10 休息內全部無效、充電 10:10:10 有效 → 保留原選用；"
          "10:06 休息改選同休息的 10:05:50",
          sec_of(onew["10:10"]) == 600 and onew["10:10"].obs.state == "休息" and sec_of(onew["10:06"]) == 350)
    check("O6 故障資料保留（10:16：±20 秒內有已上電待機的有效觀測，仍保留故障觀測，備註 N/A）",
          sec_of(onew["10:16"]) == 960 and RXv.status_text(onew["10:16"].obs, onames) == CFG.NA
          and any(ovalid(o_) for o_ in ores.obs if abs((o_.t - T0).total_seconds() - 960) <= 20))
    check("O7 下電（停機）語意保留：10:18 改選同為停機的 10:18:10，不選同距較早但已上電的 10:17:50",
          sec_of(onew["10:18"]) == 1090 and RXv.status_text(onew["10:18"].obs, onames) == "停機")
    check("O8 舊資料沒有上下電狀態（狀態 N/A）→ 保留原觀測，不改選已上電的有效觀測（10:20）",
          sec_of(onew["10:20"]) == 1200 and RXv.status_text(onew["10:20"].obs, onames) == CFG.NA)
    check("O9 只有 6161 溫度 → 維持原整體頁行為（選用僅 6161 觀測，保留溫度）",
          onew["10:22"].obs is not None and not has_hmi_values(onew["10:22"].obs)
          and RXv.field_value(onew["10:22"].obs, "pcs_module_c", p) == 60.0)
    check("O10 不產生新的「有值 → 整列 N/A」：整列 N/A 的分鐘與原規則完全相同",
          [k_ for k_ in onew if onew[k_].obs is None] == [k_ for k_ in oleg if oleg[k_].obs is None])
    wo = load_workbook(save_report(ores, META, os.path.join(tmp, "overall_b.xlsx")))["整體測試"]
    orow = {wo.cell(r_, 1).value.strftime("%H:%M"): [wo.cell(r_, c_).value for c_ in range(2, NOTE)]
            for r_ in range(2, wo.max_row + 1) if isinstance(wo.cell(r_, 1).value, datetime)}
    check("O11 不 forward／backward fill：10:05、10:10 保留原觀測、PCS 仍 N/A（不沿用鄰近 PCS）",
          orow["10:05"][COL["PCS 模組溫度"] - 2] == "N/A" and orow["10:10"][COL["PCS 模組溫度"] - 2] == "N/A")
    o50x = onew["10:01"].obs
    check("O12 改選時整列來自同一筆觀測（10:01 = 10:00:50）",
          orow["10:01"][0] == 55 and all(abs(x_ - y_) < 1e-9 for x_, y_ in
                                         zip(orow["10:01"], [RXv.field_value(o50x, k_, p) for k_ in RXv.LOG_KEYS])))
    check(f"O13 ±{tol_} 秒不變：選用觀測 |Δt| ≤ {tol_}，整分時間與原規則相同",
          all(abs((m_.obs.t - m_.T).total_seconds()) <= tol_ for m_ in onew.values() if m_.obs) and list(onew) == list(oleg))
    _ivo = RXv.is_report_valid
    RXv.is_report_valid = lambda o_, p_, n_: _ivo(o_, p_, n_) and o_.t != T0 + timedelta(seconds=50)   # 額外排除 10:00:50
    try:
        wop = load_workbook(save_report(ores, META, os.path.join(tmp, "overall_b_patched.xlsx")))
    finally:
        RXv.is_report_valid = _ivo
    _wo = wop["整體測試"]
    _r01 = next(r_ for r_ in range(2, _wo.max_row + 1) if _wo.cell(r_, 1).value == T0 + timedelta(minutes=1))
    check("O17 整體頁使用同一個 is_report_valid（只改這個 predicate → 10:01 改選 10:01:10，Raw Data 同時排除 10:00:50）",
          _wo.cell(_r01, COL["SOC"]).value == 57 and T0 + timedelta(seconds=50) not in
          {r_[0] for r_ in wop["Raw Data"].iter_rows(min_row=2, values_only=True, min_col=2, max_col=2)})

    def overall_ab(rf_):
        """回傳 (原規則, 方案 B) 的整體頁每分鐘選樣；names 與報告相同。"""
        nf_ = {s_.no: RXv._stage_title(_sn(rf_.stages), s_) for s_ in rf_.stages}
        return _om(rf_), _om(rf_, valid=lambda o_: RXv.is_report_valid(o_, rf_.params, nf_),
                             phase=lambda o_: RXv.overall_phase(o_, nf_))
    from cert_runtime import CertRuntime as _CR
    for cid_, want_ in (("CERT_20261102_092500", None), ("CERT_20261029_082805", None),
                        ("CERT_20261028_165847", None), ("CERT_20261028_180341", None)):
        real_ = os.path.join(HERE, "..", "output", "cert_reports", cid_)
        if not os.path.isdir(real_):
            print(f"  （略過：本機沒有 {cid_}）")
            continue
        root_ = os.path.join(tmp, "ov_" + cid_)
        shutil.copytree(real_, os.path.join(root_, cid_), ignore=shutil.ignore_patterns("reports", "screenshots"))
        rf_ = _CR(root_, reader=None, enable_shots=False, log=lambda *a, **k: None)._offline(
            os.path.join(root_, cid_)).analysis()
        a_, b_ = overall_ab(rf_)
        pna_ = lambda rows: sum(1 for m_ in rows if m_.obs is not None
                                and RXv.field_value(m_.obs, "pcs_module_c", rf_.params) is None)
        chg_ = [(x_, y_) for x_, y_ in zip(a_, b_) if x_.obs is not y_.obs]
        new_na = sum(1 for x_, y_ in chg_ if y_.obs is None)
        same_sem = all(RXv.overall_phase(x_.obs, {s_.no: RXv._stage_title(_sn(rf_.stages), s_) for s_ in rf_.stages})
                       == RXv.overall_phase(y_.obs, {s_.no: RXv._stage_title(_sn(rf_.stages), s_) for s_ in rf_.stages})
                       for x_, y_ in chg_)
        base = (f"{cid_[5:]}：整體頁 {len(a_)} 列、改選 {len(chg_)}、PCS N/A {pna_(a_)} → {pna_(b_)}、新增整列 N/A {new_na}")
        if cid_.endswith("092500"):
            check("O14 " + base + "（預期 637／32／32 → 0／0；13:40、14:42、20:01 語意保護保留原觀測）",
                  len(a_) == len(b_) == 637 and len(chg_) == 32 and pna_(a_) == 32 and pna_(b_) == 0 and new_na == 0
                  and same_sem and all(m_.obs is n_.obs for m_, n_ in zip(a_, b_)
                                       if m_.T.strftime("%H:%M") in ("13:40", "14:42", "20:01")))
        elif cid_.endswith("082805"):
            keep = [m_.T.strftime("%H:%M") for m_, n_ in zip(a_, b_) if m_.obs is n_.obs
                    and "18:08" <= m_.T.strftime("%H:%M") <= "18:13" and n_.obs is not None]
            check("O15 " + base + "（18:08～18:13 故障／下電期間保留原觀測與數值）",
                  len(keep) == 6 and new_na == 0 and len(chg_) == 1 and same_sem)
        else:
            check("O16 舊 Cert " + base + "（無上下電狀態的休息資料保留，不新增整列 N/A）",
                  len(chg_) == 0 and new_na == 0)

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
        fobs = [r_ for r_ in rwf.iter_rows(min_row=2, values_only=True) if r_[RH.index("讀取結果")] != "（無取樣）"]
        ftimes = {r_[RH.index("PC 取樣時間")].strftime("%H:%M:%S") for r_ in fobs}
        hm = lambda t: t.strftime("%H:%M:%S")
        j165 = rtf.records[164]
        req_ok = [o for o in hv if all(_num(o.values.get(k)) is not None for k in RX.RAW_REQUIRED)]
        pcs_miss = [o for o in hv if _num(o.values.get("pcs_module_c")) is None]
        nf = {s_.no: RX._stage_title(RX.stage_names(rf.stages), s_) for s_ in rf.stages}
        st_na = [o for o in req_ok if RX.status_text(o, nf) == "N/A"]
        fin = [o for o in req_ok if RX.status_text(o, nf) != "N/A"]
        check(f"082805：Raw Data 觀測列 = HMI 有效 ∧ 必要欄位齊全 ∧ 狀態可判定（{len(fobs)} = {len(fin)}；原始 {len(rtf.records)}、"
              f"HMI 無效 {len(rf.obs) - len(hv)}、PCS 模組缺值 {len(pcs_miss)}、狀態無法判定 {len(st_na)}）",
              len(fobs) == len(fin) == 3156 and len(req_ok) == 3204 and len(hv) == 3209
              and len(rf.obs) == len(rtf.records) == 3229
              and [hm(o.t) for o in pcs_miss] == ["13:24:55", "13:43:41", "16:41:16", "17:55:49", "17:56:34"])
        from collections import Counter
        f_ = lambda o: o.flags or {}
        cause = Counter("PCS 故障" if f_(o).get("pcs_fault_flag") is True
                        else "充放電旗標未歸零" if f_(o).get("pcs_charging_flag") is True or f_(o).get("pcs_discharging_flag") is True
                        else "上下電" + str(f_(o).get("battery_power_status")) for o in st_na)
        check(f"082805 狀態 N/A 原因：{dict(cause)}；全部在階段外（direction I／休息）",
              cause == {"PCS 故障": 35, "上下電未知": 8, "充放電旗標未歸零": 5}
              and all(o.direction == "I" and o.stage_no is None for o in st_na))
        stc = Counter(r_[RH.index("狀態")] for r_ in fobs)
        check(f"082805 Raw Data 狀態分布：{dict(stc)}（無 N/A）",
              stc == {"第1次放電（滿放）": 683, "第1次充電（滿充）": 686, "第2次放電（滿放）": 689,
                      "第2次充電（滿充）": 411, "待機": 684, "停機": 3})
        check("082805：17:55:49（3138）、17:56:34（3142）JSON 仍在、Raw Data 不輸出；前後 3137／3139、3141／3143 保留",
              rtf.records[3137]["t"].endswith("17:55:49") and rtf.records[3141]["t"].endswith("17:56:34")
              and not {"17:55:49", "17:56:34"} & ftimes
              and {r_[0] for r_ in fobs} >= {3137, 3139, 3141, 3143} and not {3138, 3142} & {r_[0] for r_ in fobs})
        check("082805：傳感器（選用）全部 N/A 仍輸出；Raw Data 沒有任何必要欄位為 N/A",
              all(r_[RH.index("傳感器溫度")] == "N/A" for r_ in fobs)
              and not any(r_[RH.index(h)] == "N/A" for r_ in fobs
                          for h in ("SOC", "電流", "Rack 最大溫度", "Rack 最小溫度", "Cell 最大電壓", "Cell 最小電壓",
                                    "總電壓", "PCS 模組溫度", "功率 kW")))
        check("082805：08:57:22 在 observations.jsonl（第 165 筆）存在、Raw Data 不存在；08:57:12、08:57:33 仍在",
              j165["t"].endswith("08:57:22") and "08:57:22" not in ftimes
              and {"08:57:12", "08:57:33"} <= ftimes)
        seq_by_t = {r_[RH.index("PC 取樣時間")].strftime("%H:%M:%S"): r_[0] for r_ in fobs}
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
              == [("08:35", "10:37"), ("10:40", "12:42"), ("14:40", "16:45"), ("16:50", "18:05")])
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
              vis == [(hmm(a_), hmm(b_)) for a_, b_ in spans] and labs[0][0] == "08:35" and labs[0][-1] == "10:37"
              and skips == ["1"] * 4 and [len(l_) for l_ in labs] == [len(stage_minutes(rf, s_)) for s_ in rf.stages])
        diff_vis = [int((datetime.strptime(vis[k + 1][0], "%H:%M") - datetime.strptime(vis[k][1], "%H:%M")).total_seconds()
                        // 60) for k in range(3)]
        sp = re.findall(r"<a:t>([^<]*)</a:t>", zf.read(f"xl/drawings/{sum_dr}").decode("utf-8"))
        check(f"1～3/5～7 畫面可見時間差 {diff_vis} = Summary 箭頭 {sp}（10:37→10:40、12:42→14:40、16:45→16:50）",
              sp == [f"{d_} 分" for d_ in diff_vis] == ["3 分", "118 分", "5 分"])
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
        _chg = sum(a_.obs is not b_.obs for s_ in rf.stages for a_, b_ in
                   zip(stage_minutes(rf, s_), stage_minutes(rf, s_, valid=lambda o_: RX.is_report_valid(o_, rf.params, _nf))))
        check("V15 082805：套用正式報表有效判定後，四個階段的選用觀測 0 列改變", _chg == 0)
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
        old9 = [stage_minutes(r9, s_) for s_ in r9.stages]
        new9 = [stage_minutes(r9, s_, valid=v9) for s_ in r9.stages]
        pna = lambda rows: sum(RX.field_value(m_.obs, "pcs_module_c", p9) is None if m_.obs else True for m_ in rows)
        check(f"V13 092500：PCS 模組溫度 N/A {[pna(r_) for r_ in old9]} → {[pna(r_) for r_ in new9]}（26 → 0）",
              [pna(r_) for r_ in old9] == [16, 10, 0, 0] and [pna(r_) for r_ in new9] == [0, 0, 0, 0])
        chg9 = sum(a_.obs is not b_.obs for o_, n_ in zip(old9, new9) for a_, b_ in zip(o_, n_))
        check(f"V14 092500：階段頁分鐘列數不變（{sum(map(len, old9))} → {sum(map(len, new9))}）、整分時間相同、改選 26 列",
              [len(r_) for r_ in new9] == [126, 124, 127, 126] and sum(map(len, new9)) == 503 and chg9 == 26
              and [[m_.T for m_ in r_] for r_ in old9] == [[m_.T for m_ in r_] for r_ in new9])
        legacy = [o_ for o_ in r9.obs if o_.ok and has_hmi_values(o_)                     # 修改前 raw_rows_obs 的條件
                  and all(RX.field_value(o_, k_, p9) is not None for k_ in RX.RAW_REQUIRED)
                  and RX.status_text(o_, nf9) != RX.NA]
        raw9 = RX.raw_rows_obs(r9, p9, nf9)
        check(f"V16 092500：Raw Data 有效集合不變（{len(raw9)} 筆，與修改前條件逐筆相同）",
              raw9 == legacy and len(raw9) == 3213)
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
