# -*- coding: utf-8 -*-
"""
實機資料預覽：唯讀抓取真實 Cell 資料，產生 Cell Volt./Cell Temp. 兩張工作表並檢核。
**純 GET 唯讀**，不送任何控制命令；getPackInformation 為 guest 端點，soc/power/方向亦為 guest。

用法：
  python realdata_cell_preview.py [快照數 預設2] [間隔秒 預設5]
輸出：
  output/realdata_cell_preview/report_cellpreview.xlsx（僅含兩張 Cell 工作表，供版面/顏色/資料核對）
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charge_discharge_report as R
import charge_discharge_report_config as CFG
from openpyxl import Workbook
from api_client import ApiClient

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2
GAP = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
OUT_DIR = os.path.join(R._OUTPUT_ROOT, "realdata_cell_preview")
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "report_cellpreview.xlsx")


def main():
    client = ApiClient()
    logged = False
    try:
        logged = bool(client.login_hmi("hmiUser"))
    except Exception as e:
        print(f"[登入] 失敗（不影響 guest 端點）：{e}")
    print(f"[連線] login_hmi={'OK' if logged else '未登入（仍可讀 guest 端點）'}")

    snaps = []
    reasons = ["session_start", "session_end", "mid_1", "mid_2", "mid_3"]
    for i in range(N):
        r = R.read_all(client)
        power = (r.get("actual_active_power_kw") if CFG.POWER_SOURCE == "pcs"
                 else r.get("calculated_power_kw"))
        d, src = R.resolve_direction(r.get("pcs_charging_flag"),
                                     r.get("pcs_discharging_flag"), power)
        mode = d if d in ("charge", "discharge") else "standby"
        data, err = R._fetch_cell_packs(client)
        ts = R._now_str()
        snap = R.BDS.create_cell_snapshot(
            data, ts, mode, r.get("soc_percent"), power,
            reasons[i] if i < len(reasons) else f"snap_{i}",
            snapshot_id=f"SNAP-{i + 1:03d}", error_message=err,
            expected_packs=CFG.CELL_EXPECTED_PACKS)
        snaps.append(snap)
        print(f"\n[快照 {snap['snapshot_id']}] ts={ts} mode={mode}(source={src}) "
              f"soc={r.get('soc_percent')} power={power}kW "
              f"comm_ok={r.get('communication_ok')} status={snap['cell_data_status']}")
        if snap["cell_data_status"] == "failed":
            print(f"    ✗ Cell 抓取失敗：{snap['error_message']}")
        else:
            packs = snap["packs"]
            print(f"    pack_count={snap['pack_count']} cell_count={snap['cell_count']}")
            per = {pk: len(v.get('cell_voltage') or []) for pk, v in packs.items()}
            print(f"    每 Pack cell 數：{per}")
            vst = R.calculate_cell_statistics(snap, "voltage")
            tst = R.calculate_cell_statistics(snap, "temperature")
            print(f"    電壓  max/min/avg/diff = {_r(vst['max'])}/{_r(vst['min'])}/"
                  f"{_r(vst['average'])}/{_r(vst['diff'])} V （有效 {vst['count']} 顆）")
            print(f"    溫度  max/min/avg/diff = {_r(tst['max'])}/{_r(tst['min'])}/"
                  f"{_r(tst['average'])}/{_r(tst['diff'])} ℃（有效 {tst['count']} 顆）")
            # 無效值統計
            badv = sum(1 for v in packs.values() for x in (v.get('cell_voltage') or []) if x is None)
            badt = sum(1 for v in packs.values() for x in (v.get('cell_temperature') or []) if x is None)
            print(f"    無效值：電壓 {badv} 顆、溫度 {badt} 顆（None/NaN，已排除計算）")
            if snap["warnings"]:
                print(f"    warnings（{len(snap['warnings'])}）：")
                for w in snap["warnings"]:
                    print(f"       - {w}")
        if i < N - 1:
            time.sleep(GAP)

    # 產生兩張工作表
    wb = Workbook()
    R.create_cell_sheet(wb, "Cell Volt.", "voltage", snaps,
                        number_format=CFG.CELL_VOLT_CELL_FORMAT, stat_format=CFG.CELL_VOLT_STAT_FORMAT,
                        color_provider=R.get_voltage_fill, summary_labels=CFG.CELL_VOLT_SUMMARY_LABELS)
    R.create_cell_sheet(wb, "Cell Temp.", "temperature", snaps,
                        number_format=CFG.CELL_TEMP_CELL_FORMAT, stat_format=CFG.CELL_TEMP_STAT_FORMAT,
                        color_provider=R.get_temperature_fill, summary_labels=CFG.CELL_TEMP_SUMMARY_LABELS)
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]
    out = OUT
    try:
        wb.save(out)
    except PermissionError:
        # 目標檔正被 Excel 開啟鎖定 → 存到帶時間戳的替代檔名，避免整個作廢
        from datetime import datetime as _dt
        out = OUT.replace(".xlsx", "_" + _dt.now().strftime("%H%M%S") + ".xlsx")
        wb.save(out)
        print(f"\n[提醒] 原檔被 Excel 鎖定，已改存新檔（請關閉舊視窗）。")
    print(f"\n[輸出] {out}")
    print(f"[快照數] {len(snaps)}（Cell Volt. / Cell Temp. 各含相同筆數紀錄）")


def _r(v):
    return "N/A" if v is None else round(v, 4)


if __name__ == "__main__":
    main()
