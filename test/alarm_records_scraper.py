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

import os
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
# 後端依此標頭直接中文化 typeMark/targetMark/val/triggerVal（與 charge_discharge_report 同一機制、
# 與 HMI 一致）：device.type.bcu→電池、1838F4.soc.underAlarm→SOC過低報警、1838F4.minorAlarm→輕微報警。
# ⚠️ 必須用連字號 zh-TW（底線 zh_TW 無效，會回原始 i18n key）。
ALARM_LANG_HEADER = {"Accept-Language": "zh-TW"}

# 統一輸出目錄：專案根目錄下的 output/（不論從哪個資料夾執行都一致），不存在則自動建立
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(_OUTPUT_DIR, exist_ok=True)
JSON_PATH = os.path.join(_OUTPUT_DIR, "alarm_records.json")
CSV_PATH = os.path.join(_OUTPUT_DIR, "alarm_records.csv")

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


# 告警級別中文：完全比照 HMI 告警頁 render 規則（前端 index-68732cea.js）：
#   level===0 → 嚴重(serious)；===1 → 一般(medium)；其他（2,3…）→ 輕微(slight)。
# 中文取自 zh_TW 語系 routes.custom_header.*。⚠️ 非「級別名稱表」，是 UI 實際顯示規則。
def _level_text(v):
    """level → 中文級別，依 UI render 規則；非數字（已是中文）原樣返回。"""
    try:
        lv = int(v)
    except (TypeError, ValueError):
        return v
    if lv == 0:
        return "嚴重"
    if lv == 1:
        return "一般"
    return "輕微"


def _normalize(a):
    """
    委派 charge_discharge_report.normalize_alarm_record（單一翻譯/正規化流程，Console/JSON/CSV/Excel 共用，
    不另建 mapping）。lazy import 以打破 report ↔ scraper 相互匯入的循環。
    """
    from charge_discharge_report import normalize_alarm_record
    return normalize_alarm_record(a)


def fetch_all_alarm_records(client, lang=True):
    """
    分頁抓全部告警。回傳 (total, rows_all)。
    lang=True → 帶 Accept-Language: zh-TW（後端直接回中文，供 JSON/CSV/內容）；
    lang=False → 不帶（回原始 i18n key，僅供取原始 typeMark 推導裝置代碼 BCU/PCS）。
    """
    headers = ALARM_LANG_HEADER if lang else None
    rows_all = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        resp = client.get(ALARM_LIST_API, params={"pageNo": page, "pageSize": PAGE_SIZE},
                          headers=headers)
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


def _device_code(typemark_raw):
    """裝置代碼（BCU / PCS…）：用『原始 typeMark』（device.type.bcu）經既有 ALARM_CODE_MAP → BCU；
    查不到取末段大寫（device.type.bcu → BCU）。⚠️ 不翻成「電池」，與 UI/API 的裝置代碼一致。"""
    code = _code_text(typemark_raw)
    if code and code != typemark_raw:
        return code
    s = str(typemark_raw or "")
    return s.rsplit(".", 1)[-1].upper() if s else s


def _console_alarm_message(target, val, operator):
    """
    Console 專用：只保留『主要告警訊息（Alarm Message）』，不含「觸發條件：」/「當前值：」前後綴。
    等值告警(==) → 觸發條件(targetMark) 即主訊息（如 SOC過低報警）；
    其餘(!= / < / > …) → 當前值(val) 為實際狀態（如 直流輸入欠壓）。
    （不影響 report/CSV/JSON，那裡仍保留完整「觸發條件…；當前值…」。）
    """
    target = (target or "").strip()
    val = (val or "").strip()
    if operator == "==":
        return target or val
    return val or target


def summarize_alarm_records(total, rows, raw_type=None):
    """統計：總數、啟用中(alarmStatus=True)筆數、前幾筆精簡預覽（Console 專用精簡格式）。
    raw_type = {id: 原始 typeMark}（未中文化）→ 供 Console 顯示裝置代碼 BCU/PCS。"""
    active = sum(1 for a in rows if isinstance(a, dict) and _is_active(a.get("alarmStatus")))
    raw_type = raw_type or {}

    # 預覽依前端表格排序（alarmTime DESC）取前 5 筆，欄位順序與前端一致：
    # 告警物件 / 告警內容 / 告警級別 / 告警狀態 / 告警時間
    valid = [a for a in rows if isinstance(a, dict)]
    valid.sort(key=lambda r: int(r.get("alarmTime") or 0), reverse=True)
    preview = []
    for a in valid[:5]:
        rid = str(a.get("id") or "")
        # 物件：裝置代碼（BCU/PCS）取自原始 typeMark；無 raw 時退回本列（zh-TW）typeMark
        obj = _device_code(raw_type.get(rid, a.get("typeMark")))
        preview.append({
            "告警物件": obj,
            "告警內容": _console_alarm_message(a.get("targetMark"), a.get("val"), a.get("operator")),
            "告警級別": _level_text(a.get("level")),     # 與 report 同規則（0嚴重/1一般/其他輕微）
            "告警狀態": _alarm_status_text(a.get("alarmStatus")),
            "告警時間": _epoch_ms_to_text(a.get("alarmTime")),
        })
    return {"total_api": total, "抓回筆數": len(rows), "告警中未恢復(全量)": active, "預覽前5筆": preview}


def build_alarm_rows(rows):
    """CSV：每筆告警一列。前導 5 欄（告警物件/內容/級別/狀態/時間）一律經 _normalize（與 report 同源）。"""
    out = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        n = _normalize(a)
        row = {k: a.get(k, "") for k in ROW_FIELDS}
        row["alarmTimeText"] = n["alarm_start_time"]
        row["createTimeText"] = _epoch_ms_to_text(a.get("createTime"))
        row["alarmStatusText"] = _alarm_status_text(a.get("alarmStatus"))
        row["typeMarkText"] = n["target_object"]
        row["targetMarkText"] = a.get("targetMark", "")   # 後端已中文化（zh-TW）
        row["valText"] = n["alarm_content"]
        # 前導人可讀 5 欄（順序與 console 一致；與 report 之 target_object/alarm_content/level 完全相同）
        row["告警物件"] = n["target_object"]
        row["告警內容"] = n["alarm_content"]
        row["告警級別"] = n["level"]
        row["告警狀態"] = row["alarmStatusText"]
        row["告警時間"] = n["alarm_start_time"]
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
        # CSV 只輸出 UI 告警表格欄位（5 欄，順序與前端一致）；
        # 原始/除錯欄位（alarmTime/createTime/level/alarmStatus… 等）只保留在 JSON。
        # extrasaction="ignore"：rows 內多出的明細欄位不寫入 CSV；restval=""：缺值輸出空字串。
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CURATED_ORDER,
                                    extrasaction="ignore", restval="")
            writer.writeheader()
            writer.writerows(rows)
        print(f"告警明細已寫入：{path}（{len(rows)} 列，UI 欄位 {len(CURATED_ORDER)} 欄）")
    except OSError as e:
        print(f"  [警告] 寫入 CSV 失敗：{e}")


def main():
    client = ApiClient()
    total, rows = fetch_all_alarm_records(client)           # zh-TW（供 JSON/CSV/內容）

    # 另抓一份未中文化清單，僅為 Console 取『裝置代碼 BCU/PCS』（原始 typeMark）；失敗則略過。
    raw_type = {}
    try:
        _, raw_rows = fetch_all_alarm_records(client, lang=False)
        raw_type = {str(r["id"]): r.get("typeMark") for r in raw_rows
                    if isinstance(r, dict) and r.get("id") is not None}
    except Exception as e:
        print(f"  [提示] 略過裝置代碼原始抓取（Console 物件退回中文）：{type(e).__name__}")

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_api": ALARM_LIST_API,
        "total": total,
        "fetched": len(rows),
        "rows": rows,
    }
    save_json(record)

    summ = summarize_alarm_records(total, rows, raw_type)
    print_summary(summ)
    save_csv(build_alarm_rows(rows))


if __name__ == "__main__":
    main()
