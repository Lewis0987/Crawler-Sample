# -*- coding: utf-8 -*-
"""
告警記錄頁籤 scraper（只讀 GET，支援分頁抓全部）
==================================================
對應前端：alarm-records/index.vue（import permission 的 c = alarm/list，BasicTable + useTable 分頁）
API（免登入 guest）：GET /hmiGuest/unauthorizedAccess/alarm/list
  - 參數：pageNo、pageSize（已確認）。回傳 {total, rows, code, msg}。
  - 抓全部 = 迴圈 pageNo，累積到 len(all) >= total。

每筆 row 欄位（實測）：id, deviceId, typeMark, targetMark, thresholdId, condition,
  operator, triggerVal, val, level, alarmStatus, intervalTime, alarmTime, createTime, alertContent

輸出：alarm_records.json（完整原始，含全部頁）＋ console 摘要 ＋ alarm_records.csv
執行：python alarm_records_scraper.py
"""

import csv
import json
import unicodedata
from datetime import datetime

from api_client import ApiClient

# console 對齊欄寬（以「顯示寬度」計；中文全形算 2）
W_OBJ, W_MSG, W_LVL, W_STS, W_TIME = 10, 24, 8, 8, 19
# CSV 前導的人可讀欄位順序（與 console 一致）
CURATED_ORDER = ["告警物件", "告警內容", "告警級別", "告警狀態", "告警時間"]


def _disp_width(s):
    """字串的終端顯示寬度：東亞全形/寬字元算 2，其餘算 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in str(s))


def _fit(s, width):
    """依顯示寬度把字串補/截成剛好 width 欄（中文對齊用）。"""
    s = str(s)
    out, w = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out) + " " * (width - w)

ALARM_LIST_API = "/hmiGuest/unauthorizedAccess/alarm/list"
PAGE_SIZE = 100        # 每頁筆數（分頁抓全部用）
MAX_PAGES = 200        # 安全上限，避免異常時無限迴圈

JSON_PATH = "alarm_records.json"
CSV_PATH = "alarm_records.csv"

# 想輸出的欄位（以實測 row 欄位為主）。
# 保留原始 i18n key（typeMark/targetMark/val），並各自在其後補中文欄位 *Text。
# alarmTime/createTime 為 epoch 毫秒，保留原欄位並在其後補可讀欄位 *Text。
ROW_FIELDS = ["alarmTime", "alarmTimeText", "createTime", "createTimeText",
              "level", "alarmStatus", "alarmStatusText",
              "typeMark", "typeMarkText", "targetMark", "targetMarkText", "val", "valText",
              "alertContent", "deviceId", "triggerVal", "condition", "operator", "intervalTime"]
# TODO: 「恢復時間」欄位在 row 中未直接出現；alarmStatus 可判斷是否恢復。
#       若頁面另有恢復時間欄位（例如 recoveryTime），確認後再補進 ROW_FIELDS。

# 告警代碼中文對照（依目前 alarm_records.json 出現的 key 建立；查不到就保留原字串）
ALARM_CODE_MAP = {
    # typeMark（設備類別）
    "device.type.pcs":              "PCS",
    "device.type.bcu":              "BCU",
    # targetMark（告警對象/項目）
    "type.attr.dcInputFault":       "直流輸入故障",
    "bcu.connect.timeout":          "BCU通訊逾時",
    # val（告警內容/等級）
    "type.attr.dcInputUnderVoltage":"直流輸入欠壓",
    "1838F4.criticalAlarm":         "嚴重告警",
}


def _epoch_ms_to_text(v):
    """epoch 毫秒 → 'YYYY-MM-DD HH:MM:SS'；非數字則回空字串。"""
    try:
        return datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _code_text(v):
    """告警代碼 → 中文；查不到就保留原字串（不填空）。"""
    return ALARM_CODE_MAP.get(v, v)


# alarmStatus 語意（經前端頁面對照確認）：true=告警中/未恢復；false=已恢復
_ACTIVE_VALUES = (True, 1, "1", "true", "True")


def _is_active(v):
    """alarmStatus 是否為『告警中/未恢復』。"""
    return v in _ACTIVE_VALUES


def _alarm_status_text(v):
    """alarmStatus → 中文：true→告警中；false→已恢復（不改原始布林值）。"""
    return "告警中" if _is_active(v) else "已恢復"


# 告警級別中文（依前端頁面對照：0=嚴重、1=一般）。未知值保留原字串。
LEVEL_MAP = {0: "嚴重", 1: "一般", "0": "嚴重", "1": "一般"}


def _level_text(v):
    """level → 中文級別；查不到就保留原值。"""
    return LEVEL_MAP.get(v, v)


def fetch_all_alarm_records(client):
    """分頁抓全部告警。回傳 (total, rows_all)。"""
    rows_all = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        resp = client.get(ALARM_LIST_API, params={"pageNo": page, "pageSize": PAGE_SIZE})
        if not isinstance(resp, dict):
            break
        rows = resp.get("rows") or resp.get("list") or resp.get("records") or []
        if total is None:
            total = resp.get("total", len(rows))
        rows_all.extend(rows)
        print(f"  第 {page} 頁：取回 {len(rows)} 筆（累積 {len(rows_all)}/{total}）")
        # 已抓滿或本頁無資料 → 結束
        if not rows or (total is not None and len(rows_all) >= total):
            break
    return total, rows_all


def summarize_alarm_records(total, rows):
    """統計：總數、啟用中(alarmStatus=True)筆數、前幾筆精簡預覽。"""
    active = sum(1 for a in rows if isinstance(a, dict) and _is_active(a.get("alarmStatus")))

    # 預覽依前端表格排序（alarmTime DESC）取前 5 筆，欄位順序與前端一致：
    # 告警物件 / 告警內容 / 告警級別 / 告警狀態 / 告警時間
    valid = [a for a in rows if isinstance(a, dict)]
    valid.sort(key=lambda r: int(r.get("alarmTime") or 0), reverse=True)
    preview = []
    for a in valid[:5]:
        preview.append({
            "告警物件": _code_text(a.get("typeMark")),      # 優先 text，_code_text 查不到自動退回原值
            "告警內容": _code_text(a.get("val")),
            "告警級別": _level_text(a.get("level")),
            "告警狀態": _alarm_status_text(a.get("alarmStatus")),
            "告警時間": _epoch_ms_to_text(a.get("alarmTime")),
        })
    return {"total_api": total, "抓回筆數": len(rows), "告警中未恢復(全量)": active, "預覽前5筆": preview}


def build_alarm_rows(rows):
    """CSV：每筆告警一列，取 ROW_FIELDS，並補上可讀時間 *Text。"""
    out = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        row = {k: a.get(k, "") for k in ROW_FIELDS}
        row["alarmTimeText"] = _epoch_ms_to_text(a.get("alarmTime"))
        row["createTimeText"] = _epoch_ms_to_text(a.get("createTime"))
        row["alarmStatusText"] = _alarm_status_text(a.get("alarmStatus"))
        row["typeMarkText"] = _code_text(a.get("typeMark"))
        row["targetMarkText"] = _code_text(a.get("targetMark"))
        row["valText"] = _code_text(a.get("val"))
        # 前導人可讀 5 欄（順序與 console 一致；明細欄位仍保留在後）
        row["告警物件"] = row["typeMarkText"]
        row["告警內容"] = row["valText"]
        row["告警級別"] = _level_text(a.get("level"))
        row["告警狀態"] = row["alarmStatusText"]
        row["告警時間"] = row["alarmTimeText"]
        out.append(row)
    return out


def print_summary(summ):
    print("=" * 56)
    print(f"告警記錄：API total={summ['total_api']}，實際抓回 {summ['抓回筆數']} 筆，"
          f"告警中/未恢復（全量統計）{summ['告警中未恢復(全量)']} 筆")
    # 固定欄寬（依顯示寬度補空白）→ PowerShell / CMD 也能對齊。不印欄位標題列與分隔線。
    print("  預覽前 5 筆：")
    for i, a in enumerate(summ["預覽前5筆"], 1):
        print(f"    [{i:>2}] {_fit(a['告警物件'], W_OBJ)} | {_fit(a['告警內容'], W_MSG)} | "
              f"{_fit(a['告警級別'], W_LVL)} | {_fit(a['告警狀態'], W_STS)} | {_fit(a['告警時間'], W_TIME)}")


def save_json(record, path=JSON_PATH):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        print(f"\n完整原始資料已寫入：{path}")
    except OSError as e:
        print(f"  [警告] 寫入 JSON 失敗：{e}")


def save_csv(rows, path=CSV_PATH):
    if not rows:
        return
    try:
        # 前導 5 個人可讀欄（順序固定）＋ 後面保留完整明細欄位
        fieldnames = CURATED_ORDER + ROW_FIELDS
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"告警明細已寫入：{path}（{len(rows)} 列）")
    except OSError as e:
        print(f"  [警告] 寫入 CSV 失敗：{e}")


def main():
    client = ApiClient()
    total, rows = fetch_all_alarm_records(client)

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_api": ALARM_LIST_API,
        "total": total,
        "fetched": len(rows),
        "rows": rows,
    }
    save_json(record)

    summ = summarize_alarm_records(total, rows)
    print_summary(summ)
    save_csv(build_alarm_rows(rows))


if __name__ == "__main__":
    main()
