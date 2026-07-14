# -*- coding: utf-8 -*-
"""
電池數據頁籤 scraper（只讀 GET）— Pack 摘要 + Cell 明細兩層
==============================================================
對應前端：
  - battery-data/index.vue     （首頁每張 Pack 卡片）
  - battery-details/index.vue   （點進 Pack 詳情的 cell 表格）
API 盤點：兩頁都 import permission 的函式 d = getPackInformation（同一支 endpoint），
  且每個 pack 的 packList（20 cells）已含在同一份 payload → 無需另一支詳情 API、無需 packId。
  GET /hmiGuest/unauthorizedAccess/battery/getPackInformation （免登入、無參數）

實測結構：data = [ {packNo, sumVoltage, maxVoltage/maxVoltageIndex, minVoltage/minVoltageIndex,
  maxTemperature/Index, minTemperature/Index, maxSoc/minSoc, maxSoh/minSoh,
  packList:[{sort, voltage, temperature, soc, soh, equilibriumState}×20], *Unit}, ...×14 ]
  註：maxVoltageIndex 等 Index、以及 cell 的 sort 皆為「全域 cell 編號」（pack1:1–20、pack2:21–40…）。

輸出（兩層拆開）：
  - battery_data.json / battery_data.csv   → Pack 摘要層（一個 pack 一列）
  - battery_cells.json / battery_cells.csv → Cell 明細層（一個 cell 一列，14×20=280）
執行：python battery_data_scraper.py
"""

import os
import csv
import json
from datetime import datetime

from api_client import ApiClient

BATTERY_PACK_API = "/hmiGuest/unauthorizedAccess/battery/getPackInformation"
CELLS_PER_PACK = 20   # 預期每 pack 的 cell 數；少於此會在 console 警告

# 統一輸出目錄：專案根目錄下的 output/（不論從哪個資料夾執行都一致），不存在則自動建立
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
PACK_JSON = os.path.join(_OUTPUT_DIR, "battery_data.json")
PACK_CSV = os.path.join(_OUTPUT_DIR, "battery_data.csv")
CELL_JSON = os.path.join(_OUTPUT_DIR, "battery_cells.json")
CELL_CSV = os.path.join(_OUTPUT_DIR, "battery_cells.csv")

# 欄位固定（數值皆為原始值；單位固定：電壓 V、溫度 ℃、SOC/SOH %）
PACK_FIELDS = ["packNo", "totalVoltage",
               "maxCellVoltage", "maxCellNo", "minCellVoltage", "minCellNo",
               "maxTemp", "maxTempCellNo", "minTemp", "minTempCellNo",
               "socMax", "socMin", "sohMax", "sohMin"]
# JSON 用（機器欄位）
CELL_FIELDS = ["packNo", "cellNo", "voltage", "temperature", "soc", "soh", "balanceStatus"]
# CSV 用（人可讀中文欄位，順序固定）
CELL_CSV_FIELDS = ["Pack", "序號", "電壓", "溫度", "SOC", "SOH", "均衡狀態"]

# Pack 極值摘要 CSV（人可讀中文欄位＋單位於表頭，數值保持原始數字；順序固定）。
# 對應 UI「電池數據 / 極值資訊」：每個極值皆為「數值＋電芯編號」成對呈現。
# 機器欄位（PACK_FIELDS）仍完整保留在 battery_data.json。
PACK_CSV_FIELDS = ["Pack", "總電壓(V)",
                   "最高單體電壓(V)", "最高電壓電芯編號",
                   "最低單體電壓(V)", "最低電壓電芯編號",
                   "最高溫度(℃)", "最高溫度電芯編號",
                   "最低溫度(℃)", "最低溫度電芯編號",
                   "最高SOC(%)", "最低SOC(%)", "最高SOH(%)", "最低SOH(%)"]
# 機器欄位 → 中文欄位 對照（順序即 PACK_CSV_FIELDS）
_PACK_CSV_MAP = [
    ("packNo", "Pack"), ("totalVoltage", "總電壓(V)"),
    ("maxCellVoltage", "最高單體電壓(V)"), ("maxCellNo", "最高電壓電芯編號"),
    ("minCellVoltage", "最低單體電壓(V)"), ("minCellNo", "最低電壓電芯編號"),
    ("maxTemp", "最高溫度(℃)"), ("maxTempCellNo", "最高溫度電芯編號"),
    ("minTemp", "最低溫度(℃)"), ("minTempCellNo", "最低溫度電芯編號"),
    ("socMax", "最高SOC(%)"), ("socMin", "最低SOC(%)"),
    ("sohMax", "最高SOH(%)"), ("sohMin", "最低SOH(%)"),
]


def to_pack_csv_rows(pack_summary):
    """把 Pack 摘要（機器欄位）轉成中文欄位 CSV 列；缺值輸出空字串。"""
    out = []
    for p in pack_summary:
        row = {}
        for mkey, zh in _PACK_CSV_MAP:
            v = p.get(mkey)
            row[zh] = "" if v is None else v
        out.append(row)
    return out

# 均衡狀態對照：0=無均衡（前端確認）；1=均衡中（推定，站上目前全為 0）。查不到保留原值。
BALANCE_MAP = {0: "無均衡", 1: "均衡中", "0": "無均衡", "1": "均衡中"}


def _balance_text(v):
    return BALANCE_MAP.get(v, v)


def fetch_battery_data(client):
    """GET getPackInformation（無參數），回傳 pack list 或 None。"""
    return client.get(BATTERY_PACK_API)


# ---------------- Pack 摘要層 ----------------
def summarize_pack_summary(data):
    """把 pack list 整理成 Pack 摘要（一個 pack 一 dict）；無資料回傳 None。"""
    if not isinstance(data, list):
        return None
    out = []
    for p in data:
        if not isinstance(p, dict):
            continue
        out.append({
            "packNo":        p.get("packNo"),
            "totalVoltage":  p.get("sumVoltage"),
            "maxCellVoltage": p.get("maxVoltage"),
            "maxCellNo":     p.get("maxVoltageIndex"),
            "minCellVoltage": p.get("minVoltage"),
            "minCellNo":     p.get("minVoltageIndex"),
            "maxTemp":       p.get("maxTemperature"),
            "maxTempCellNo": p.get("maxTemperatureIndex"),
            "minTemp":       p.get("minTemperature"),
            "minTempCellNo": p.get("minTemperatureIndex"),
            # API 無單一 pack SOC/SOH，提供實測的最高/最低（socUnit/sohUnit 皆 %）
            "socMax":        p.get("maxSoc"),
            "socMin":        p.get("minSoc"),
            "sohMax":        p.get("maxSoh"),
            "sohMin":        p.get("minSoh"),
        })
    return out


# ---------------- Cell 明細層 ----------------
def build_cell_rows(data):
    """把每個 pack 的 packList 展開成 cell 明細（一個 cell 一 dict）。回傳 (rows, per_pack_counts)。"""
    rows = []
    per_pack = []  # [(packNo, cell數), ...]
    if not isinstance(data, list):
        return rows, per_pack
    for p in data:
        if not isinstance(p, dict):
            continue
        pack_no = p.get("packNo")
        cells = p.get("packList") or []
        per_pack.append((pack_no, len(cells)))
        for c in cells:
            if not isinstance(c, dict):
                continue
            rows.append({
                "packNo":       pack_no,
                "cellNo":       c.get("sort"),               # 全域 cell 編號
                "voltage":      c.get("voltage"),
                "temperature":  c.get("temperature"),
                "soc":          c.get("soc"),
                "soh":          c.get("soh"),
                "balanceStatus": c.get("equilibriumState"),  # 原始值（0/1；語意 TODO，未臆測）
            })
    return rows, per_pack


# ---------------- CSV 轉換（中文欄位）----------------
def to_cell_csv_rows(cell_rows):
    """把 cell 明細轉成中文欄位 CSV 列（Pack, 序號, 電壓, 溫度, SOC, SOH, 均衡狀態）。"""
    return [{
        "Pack":     c["packNo"],
        "序號":     c["cellNo"],
        "電壓":     c["voltage"],
        "溫度":     c["temperature"],
        "SOC":      c["soc"],
        "SOH":      c["soh"],
        "均衡狀態": _balance_text(c["balanceStatus"]),
    } for c in cell_rows]


# ---------------- console 摘要 ----------------
def print_summary(pack_summary, cell_rows, per_pack):
    print("=" * 56)
    if not pack_summary:
        print("電池數據：無資料")
        return
    n_pack = len(pack_summary)
    n_cell = len(cell_rows)
    print(f"電池數據：{n_pack} 個 Pack，明細共 {n_cell} 筆 cell（每 Pack 應 {CELLS_PER_PACK} 筆）")
    # 每個 pack 的 cell 數 + 少於預期的警告
    for pack_no, cnt in per_pack:
        flag = "" if cnt == CELLS_PER_PACK else f"  ⚠️ 少於 {CELLS_PER_PACK} 筆！"
        print(f"  Pack {pack_no}: {cnt} 筆 cell{flag}")
    # Pack 摘要一覽
    print("  ---- Pack 摘要 ----")
    for p in pack_summary:
        print(f"  Pack {p['packNo']}｜總電壓 {p['totalVoltage']} V｜"
              f"單體 max {p['maxCellVoltage']}V(#{p['maxCellNo']})/min {p['minCellVoltage']}V(#{p['minCellNo']})｜"
              f"溫度 {p['maxTemp']}~{p['minTemp']} ℃｜SOC {p['socMax']}~{p['socMin']}%｜SOH {p['sohMax']}~{p['sohMin']}%")


def print_cell_details(cell_rows):
    """逐 Pack 印出全部 cell 明細；欄位序：序號 | 電壓 | 溫度 | SOC | SOH | 均衡狀態（不印標題列）。"""
    # 依 packNo 分組（cell_rows 已按 pack 順序排列）
    groups = {}
    order = []
    for c in cell_rows:
        pn = c["packNo"]
        if pn not in groups:
            groups[pn] = []
            order.append(pn)
        groups[pn].append(c)

    for pn in order:
        print(f"\nPack{pn} cell 明細：")
        for i, c in enumerate(groups[pn], 1):
            seq = f"{c['cellNo']}#"
            print(f"    [{i:>2}] {seq:<5} | {c['voltage']}V | {c['temperature']}°C | "
                  f"{c['soc']}% | {c['soh']}% | {_balance_text(c['balanceStatus'])}")


# ---------------- 輸出 ----------------
def _save_json(obj, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        print(f"已寫入：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 {path} 失敗：{e}")


def _save_csv(rows, fields, path):
    if not rows:
        return
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        print(f"已寫入：{path}（{len(rows)} 列）")
    except OSError as e:
        print(f"  [警告] 寫入 {path} 失敗：{e}")


def main():
    client = ApiClient()
    data = fetch_battery_data(client)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pack_summary = summarize_pack_summary(data) or []
    cell_rows, per_pack = build_cell_rows(data)

    # 兩層分別輸出（不混在一起）
    _save_json({"timestamp": ts, "source_api": BATTERY_PACK_API,
                "pack_count": len(pack_summary), "packs": pack_summary}, PACK_JSON)
    _save_csv(to_pack_csv_rows(pack_summary), PACK_CSV_FIELDS, PACK_CSV)

    _save_json({"timestamp": ts, "source_api": BATTERY_PACK_API,
                "pack_count": len(pack_summary), "cell_count": len(cell_rows),
                "cells": cell_rows}, CELL_JSON)
    _save_csv(to_cell_csv_rows(cell_rows), CELL_CSV_FIELDS, CELL_CSV)

    print()
    print_summary(pack_summary, cell_rows, per_pack)
    print_cell_details(cell_rows)


if __name__ == "__main__":
    main()
