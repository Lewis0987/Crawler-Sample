# -*- coding: utf-8 -*-
"""
PCS 充放電報告（charge_discharge_report.py）— 獨立唯讀監測與報告
==================================================================
每次充放電期間，固定間隔取樣，計算統計，輸出可追蹤報告。

⚠️ 安全規則（硬性）：
  - 本模組**只做 GET 唯讀取樣與記錄**，不送任何控制命令（無 POST/PUT/DELETE）。
  - 偵測到異常時只回報 stop_recommended，**不自行送停止指令**。
  - 充放電控制一律由既有 device_control_operator / device_control_menu 負責。

資料來源（全部唯讀，見 charge_discharge_report_DESIGN.md §2）：
  - SOC/電壓/電流   ← GET /hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation
  - PCS 有功/狀態   ← GET /hmiGuest/unauthorizedAccess/envCon/pcs（totalActivePowerOfAcBus 為主）
  - PCS 模式        ← getRunMode / getScheduleSwitch（重用 device_control_scraper.parse_pcs_modes）
  - 電池上下電      ← GET /can/v1/getDOAndDIMsg（battery_power_state_from_dodi）
  - 告警            ← GET /hmiGuest/unauthorizedAccess/alarm/list（以 id 去重）

輸出（每個 session 一個資料夾）：
  output/charge_discharge_reports/<YYYYmmdd_HHMMSS>_<action>/
    ├── summary.json      報告總覽（session/開始/結束/統計/驗證）
    ├── statistics.json   統計明細
    ├── samples.csv       時序取樣
    ├── alarms.csv        新增告警（去重）
    ├── events.csv        重要事件時間軸
    ├── report.xlsx       Excel 報告（含原生內嵌折線圖）
    └── session_state.json  Session 狀態（recording/paused/completed，供續接累積）

用法：
  # 獨立監測（讀當前設備，需先以既有流程啟動充/放電）：
  python charge_discharge_report.py --action discharge --power 20 --mode 交流有功
  python charge_discharge_report.py --action auto --duration 120      # 方向自動判斷、監測 120 秒
  # 離線自我測試（合成資料，不連設備，驗證輸出檔）：
  python charge_discharge_report.py --selftest
"""

import os
import csv
import sys
import json
import time
import argparse
from datetime import datetime, timedelta
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass    # 舊環境無 reconfigure 或 stdout 已包裝：沿用現有編碼（非報告錯誤）

import charge_discharge_report_config as CFG

# 重用既有唯讀解析（不重寫登入/判斷邏輯）
from api_client import ApiClient
from device_control_scraper import (
    battery_power_state_from_dodi,
    battery_flow_state,
    parse_pcs_modes,
    parse_pcs_current_status,
    _pcs_metrics_list,
    _CONTROL_MODE_DISPLAY,
    _GRID_MODE_DISPLAY,
)

USERNAME = "hmiUser"

# ---- 唯讀端點（全部 GET；與既有 scraper 同源）----
EP_MAINCTL = "/hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation"
EP_PCS = "/hmiGuest/unauthorizedAccess/envCon/pcs"
EP_RUNMODE = "/client/dynamic/dataOrControl/pcs/getRunMode"
EP_SCHEDULE = "/schedule/config/getScheduleSwitch"
EP_DODI = "/can/v1/getDOAndDIMsg"
EP_ALARM = "/hmiGuest/unauthorizedAccess/alarm/list"
EP_RACKEXT = "/hmiGuest/unauthorizedAccess/overview/rackExtremeValueInformation"  # Rack 最高/最低溫度
_PCS_LANG_HEADER = {"Accept-Language": "zh-TW"}

# ======================================================================
# 報表版本識別（偵測背景程式是否載入到舊模組）
#   REPORT_BUILD 烤進程式碼：執行中的舊程序會回報啟動當下載入的舊值，
#   對照 source_mtime（產生當下讀磁碟）即可看出「記憶體舊模組 vs 磁碟新程式」。
# ======================================================================
REPORT_BUILD = "20260722_01"
_MODULE_LOAD_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # 本程序載入模組的時間


def _git_commit():
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL, timeout=3)
        return out.decode("utf-8", "ignore").strip() or None
    except Exception:
        return None


def module_version_info():
    """本模組（實際載入中）的版本識別；auto 流程與 --regen 共用同一份。"""
    src = os.path.abspath(__file__)
    try:
        src_mtime = datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        src_mtime = "unknown"
    return {
        "REPORT_VERSION": REPORT_BUILD,
        "schema_version": CFG.SCHEMA_VERSION,
        "report_version": CFG.REPORT_VERSION,
        "source": src,
        "build_time": _MODULE_LOAD_TIME,
        "source_mtime": src_mtime,
        "git_commit": _git_commit() or "(none)",
    }


def print_module_banner():
    """啟動時印出模組版本橫幅，避免背景程式載入舊模組卻無法察覺。"""
    v = module_version_info()
    print("[Report Module]")
    print(f"  Version: {v['REPORT_VERSION']}  (schema {v['schema_version']} / report {v['report_version']})")
    print(f"  Source : {v['source']}")
    print(f"  Build  : load={v['build_time']}  mtime={v['source_mtime']}  git={v['git_commit']}")

# 告警代碼→中文（沿用 alarm scraper 的最小對照；查不到保留原字串）
try:
    from alarm_records_scraper import _code_text, _level_text, _alarm_status_text, _epoch_ms_to_text
except Exception:  # 保底：alarm scraper 不可用時的本地簡版
    def _code_text(v):
        return v

    def _level_text(v):
        return {0: "嚴重", 1: "一般", "0": "嚴重", "1": "一般"}.get(v, v)

    def _alarm_status_text(v):
        return "告警中" if v in (True, 1, "1", "true", "True") else "已恢復"

    def _epoch_ms_to_text(v):
        try:
            return datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return ""


# ======================================================================
# 小工具
# ======================================================================
def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_float(v):
    """把 API 值安全轉 float；None/非數字回 None。"""
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _col_floats(rows, field):
    """從 rows 取某欄的 float 值清單（略過 None/空/非數字）。"""
    out = []
    for s in rows:
        v = s.get(field)
        if v is None or v == "":
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            pass
    return out


def _y_axis_scale(vals):
    """
    共用 Y 軸 (min, max, major_unit)：
      - Major Unit 固定 5（不因範圍變大而自動改成 10/20/25）。
      - 上下限依資料最大/最小值取整到 5 的倍數並含 0；不裁切任何資料。
      - 整數刻度；正/負對稱皆每 5 一格（…-10,-5,0,5,10…）。
    """
    mu = 5
    if not vals:
        return -mu, mu, mu
    dmin = min(0.0, min(vals))       # 一定含 0
    dmax = max(0.0, max(vals))
    lo = int((dmin // mu) * mu)               # 向下取到 5 的倍數（dmin<=0）
    hi = int(-((-dmax) // mu) * mu)           # 向上取到 5 的倍數
    if hi - lo < mu:                          # 退化（全為 0）→ 至少一格
        hi = lo + mu
    return lo, hi, mu


def _chart_width_cm(label_count):
    """
    依 X 軸 Label 數量決定圖表寬度（cm）；三個紀錄頁共用同一邏輯、高度固定。
      - 非自適應（CHART_ADAPTIVE_SIZE=False）→ 固定 CHART_WIDTH_CM。
      - 自適應：width = 基準 + Label數×每Label寬，夾在 [MIN, MAX]。
    """
    if not getattr(CFG, "CHART_ADAPTIVE_SIZE", True):
        return CFG.CHART_WIDTH_CM
    w = CFG.CHART_BASE_WIDTH_CM + max(0, int(label_count)) * CFG.CHART_WIDTH_PER_LABEL_CM
    return max(CFG.CHART_MIN_WIDTH_CM, min(CFG.CHART_MAX_WIDTH_CM, w))


def _fmt_hms(seconds):
    """秒數 → 'XhYm' / 'YmZs'（人可讀）。None → '-'。"""
    if seconds is None:
        return "-"
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def _load_json_file(path):
    """讀 JSON 檔；不存在/失敗回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _parse_dt(s):
    """'YYYY-MM-DD HH:MM:SS' → datetime；失敗回 None。"""
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def find_active_session(output_root):
    """回傳最近一個未完成（status=recording/paused）的 session 資料夾；無則 None。"""
    if not os.path.isdir(output_root):
        return None
    cands = []
    for name in os.listdir(output_root):
        folder = os.path.join(output_root, name)
        st = _load_json_file(os.path.join(folder, CFG.FILE_SESSION_STATE))
        if isinstance(st, dict) and st.get("status") in (CFG.SESSION_RECORDING, CFG.SESSION_PAUSED):
            try:
                cands.append((os.path.getmtime(folder), folder))
            except OSError:
                pass
    return max(cands)[1] if cands else None


_GIT_COMMIT_CACHE = None


def _git_commit():
    """取目前 repo 的 short commit（唯讀 git，best-effort；取不到回 None）。結果快取一次。"""
    global _GIT_COMMIT_CACHE
    if _GIT_COMMIT_CACHE is not None:
        return _GIT_COMMIT_CACHE or None
    val = ""
    try:
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            val = out.stdout.strip()
    except Exception:
        val = ""
    _GIT_COMMIT_CACHE = val
    return val or None


def _created_by():
    """產生報告者：config.CREATED_BY 優先，否則本機登入帳號（best-effort）。"""
    if CFG.CREATED_BY:
        return CFG.CREATED_BY
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return None


def _device_ip(client=None):
    """設備 IP：優先 config.DEVICE_IP，否則由 client.base_url / api_client.BASE_URL 解析 host。"""
    if CFG.DEVICE_IP:
        return CFG.DEVICE_IP
    base = getattr(client, "base_url", None)
    if not base:
        try:
            from api_client import BASE_URL as base
        except Exception:
            base = None
    if not base:
        return None
    netloc = urlparse(base).netloc or base
    return netloc.split("@")[-1]        # 去除可能的 user:pass@


def normalize_power_direction(power_kw, idle_kw=CFG.IDLE_KW):
    """量測慣例：Power > +idle → charge；Power < -idle → discharge；否則 idle。None → unknown。"""
    if power_kw is None:
        return "unknown"
    if power_kw > idle_kw:
        return "charge"
    if power_kw < -idle_kw:
        return "discharge"
    return "idle"


def resolve_direction(charging_flag, discharging_flag, power_kw, idle_kw=CFG.IDLE_KW):
    """
    充放電方向判斷（方向以 PCS 狀態旗標為最高優先；旗標缺失才退回 Active Power 正負號）：
      1) systemChargingStatus == True      → ("charge",    "systemChargingStatus")
      2) systemDischargingStatus == True   → ("discharge", "systemDischargingStatus")
      3) 兩者皆 False                       → ("idle",      "pcs_status_flag")
      4) 旗標不存在/None/解析失敗           → 依 Power 正負號 fallback，source="power_fallback"
    註：待機時 PCS 常有殘餘自耗功率（例如 -1.3kW），不代表在放電，故不可只看 Power。
        Energy/累積/功率統計仍依實際 Active Power，不受本方向判斷影響。
    """
    if charging_flag is True:
        return "charge", "systemChargingStatus"
    if discharging_flag is True:
        return "discharge", "systemDischargingStatus"
    if charging_flag is False and discharging_flag is False:
        return "idle", "pcs_status_flag"
    return normalize_power_direction(power_kw, idle_kw), "power_fallback"


def normalize_current_direction(current_a, idle_a=CFG.IDLE_KW):
    """電流方向（量測慣例，充=正/放=負）；門檻沿用 idle。"""
    if current_a is None:
        return "unknown"
    if current_a > idle_a:
        return "charge"
    if current_a < -idle_a:
        return "discharge"
    return "idle"


def _canonical_signed(value, api_charge_positive=True):
    """
    正規化為「充電為正」的標準符號。
    目前站點量測值即為『充正放負』(api_charge_positive=True) → normalized == raw。
    若日後某站點量測為『充負放正』，只需把此旗標改 False，raw 仍保留原值可追查。
    """
    if value is None:
        return None
    return value if api_charge_positive else -value


def _pcs_metric_map(pcs_data):
    """envCon/pcs → {mark: {"value","unit","oldValue"}}（保留原始三值）。"""
    out = {}
    if isinstance(pcs_data, list) and pcs_data and isinstance(pcs_data[0], dict):
        for m in pcs_data[0].get("metricsDataVoList", []) or []:
            if isinstance(m, dict) and m.get("mark"):
                out[m["mark"]] = {"value": m.get("value"), "unit": m.get("unit"),
                                  "oldValue": m.get("oldValue")}
    return out


# ======================================================================
# 取樣：一次讀取所有唯讀來源，解析成統一 reading dict
# ======================================================================
def read_all(client):
    """
    一次取樣（全部 GET 唯讀）。回傳 reading dict；任何關鍵來源失敗則 communication_ok=False。
    不丟例外（單支失敗以 None 表示），確保長時間監測不中斷。
    """
    r = {"raw_source_time": "", "communication_ok": True, "_fail": []}

    def _get(path, **kw):
        try:
            v = client.get(path, **kw)
        except Exception as e:
            v = None
            r["_fail"].append(f"{path}:{type(e).__name__}")
        if v is None:
            r["_fail"].append(path)
        return v

    # 1) 電池 SOC/電壓/電流（guest）
    main = _get(EP_MAINCTL)
    soc = volt = curr = None
    batt_flow = "未知"
    if isinstance(main, dict):
        soc = _to_float(main.get("rackSoc"))
        volt = _to_float(main.get("rackTotalBatteryVoltage"))
        curr = _to_float(main.get("rackElectricCurrent"))
        batt_flow, _ = battery_flow_state(main)
        r["raw_source_time"] = str(main.get("createTime") or main.get("collectTime") or "")
    r["soc_percent"] = soc
    r["battery_voltage_v"] = volt
    r["battery_current_a"] = curr
    r["battery_status"] = batt_flow

    # 1b) Rack 最高/最低溫度（guest；來源 overview/rackExtremeValueInformation）
    rex = _get(EP_RACKEXT)
    r["rack_max_temperature_c"] = (_to_float(rex.get("rackCellMaxTemperature"))
                                   if isinstance(rex, dict) else None)
    r["rack_min_temperature_c"] = (_to_float(rex.get("rackCellMinTemperature"))
                                   if isinstance(rex, dict) else None)

    # 2) PCS 有功/無功/直流/狀態（guest）
    #    - 英文 enum（不帶語言）：供數值與 parse_pcs_modes 解析（gridTied/ac/dc 等）
    #    - 中文 value（zh-TW）：僅供 PCS當前狀態 badge（parse_pcs_current_status）
    #    兩者數值相同；狀態 mode 解析一定要用英文，否則會判成「未知」。
    pcs_data = _get(EP_PCS)
    pcs_data_tw = _get(EP_PCS, headers=_PCS_LANG_HEADER)
    marks = _pcs_metric_map(pcs_data)

    def _mv(mark):
        return _to_float(marks.get(mark, {}).get("value"))

    def _flag(mark):
        """PCS 狀態旗標 → True/False/None（依 oldValue 1/0；缺失或無法解析回 None）。"""
        md = marks.get(mark)
        if not md:
            return None
        ov = md.get("oldValue")
        if ov in (1, "1"):
            return True
        if ov in (0, "0"):
            return False
        return None

    r["pcs_charging_flag"] = _flag("systemChargingStatus")
    r["pcs_discharging_flag"] = _flag("systemDischargingStatus")
    r["actual_active_power_kw"] = _mv("totalActivePowerOfAcBus")
    r["actual_reactive_power_kvar"] = _mv("totalReactivePowerOfAcBus")
    r["dc_voltage_v"] = _mv("dcInputVoltage")
    r["dc_current_a"] = _mv("dcCurrent")
    r["dc_power_kw"] = _mv("dcPower")
    r["daily_charged_kwh"] = _mv("dailyChargedEnergyThroughAcPort")
    r["daily_discharged_kwh"] = _mv("dailyDischargedEnergyThroughAcPort")
    r["pcs_status"] = parse_pcs_current_status(_pcs_metrics_list(pcs_data_tw)) or "未知"

    # 3) PCS 模式（authed；重用 parse_pcs_modes）
    runmode = _get(EP_RUNMODE)
    schedule = _get(EP_SCHEDULE)
    pcs_raw = {mk: md.get("value") for mk, md in marks.items()}
    modes = parse_pcs_modes(runmode if isinstance(runmode, str) else "", schedule, pcs_raw)
    r["pcs_control_mode"] = _CONTROL_MODE_DISPLAY.get(modes["control_mode"], "未知")
    r["pcs_work_mode"] = _GRID_MODE_DISPLAY.get(modes["grid_mode"], "未知")
    r["pcs_power_control_mode"] = modes["power_control_mode"]
    r["pcs_schedule_enabled"] = modes["schedule_enabled"]
    r["pcs_manual_switch"] = (schedule.get("manualModeSwitch") if isinstance(schedule, dict) else None)

    # 4) 電池上下電（authed）
    dodi = _get(EP_DODI)
    state, _ = battery_power_state_from_dodi(dodi)
    r["battery_power_status"] = state

    # 5) 告警（guest；抓第一頁，取 total 與 rows；完整去重在 AlarmTracker）
    alarm = _get(EP_ALARM, params={"pageNo": 1, "pageSize": 100})
    rows = []
    total = None
    if isinstance(alarm, dict):
        rows = alarm.get("rows") or alarm.get("list") or alarm.get("records") or []
        total = alarm.get("total")
    r["alarm_rows"] = rows if isinstance(rows, list) else []
    r["alarm_total"] = total if total is not None else len(r["alarm_rows"])

    # 通訊判定：關鍵來源（電池主資訊 + PCS 有功）任一失敗 → 通訊異常
    if main is None or pcs_data is None:
        r["communication_ok"] = False
    r["calculated_power_kw"] = (volt * curr / 1000) if (volt is not None and curr is not None) else None
    return r


# ======================================================================
# 電量累積（梯形積分）
# ======================================================================
class EnergyAccumulator:
    """依取樣功率梯形積分，分開累積充/放電電量（kWh）。功率採量測慣例：充正、放負。"""

    def __init__(self):
        self.charged_kwh = 0.0
        self.discharged_kwh = 0.0
        self._prev_power = None
        self._prev_t = None

    def seed(self, charged_kwh, discharged_kwh):
        """續接既有 Session：載入先前累積值；prev 保持 None，故不積分暫停空檔（首筆重新錨定）。"""
        self.charged_kwh = float(charged_kwh or 0.0)
        self.discharged_kwh = float(discharged_kwh or 0.0)
        self._prev_power = None
        self._prev_t = None

    def add(self, power_kw, t):
        if power_kw is None:
            return
        if self._prev_power is not None and self._prev_t is not None:
            dt_h = (t - self._prev_t) / 3600.0
            if dt_h > 0:
                if CFG.ENERGY_METHOD == "rectangle":
                    avg = power_kw
                else:
                    avg = (self._prev_power + power_kw) / 2.0
                e = avg * dt_h
                if e >= 0:
                    self.charged_kwh += e
                else:
                    self.discharged_kwh += -e
        self._prev_power = power_kw
        self._prev_t = t

    @property
    def net_kwh(self):
        return self.charged_kwh - self.discharged_kwh

    @property
    def cumulative_signed_kwh(self):
        """目前累積淨電量（充為正）。作為 samples 的 cumulative_energy_kwh。"""
        return self.net_kwh


# ======================================================================
# 告警去重追蹤
# ======================================================================
def _alarm_active(v):
    return v in (True, 1, "1", "true", "True")


def _alarm_yyyymmdd(rec):
    """alarm_code 的日期段：優先取 alarm_start_time，無效則 first_seen_time（皆 'YYYY-MM-DD ...'）。"""
    for src in (rec.get("alarm_start_time"), rec.get("first_seen_time")):
        s = str(src or "").strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[0:4] + s[5:7] + s[8:10]
    return "00000000"


class AlarmTracker:
    """
    告警生命週期追蹤（以 id 去重；id 缺時退回 targetMark+val+alarmTime）：
      - prime_baseline()：session 開始時把「已存在」告警設為 baseline（origin=pre_existing），不算新增。
      - update()：之後每次取樣，新出現的告警 origin=new 並回報；已追蹤告警若 alarmStatus
        由「告警中」翻為「已恢復」→ 記錄 recovery_time 與 duration（觀測到的持續秒數）。
    API 的 alarm row 無恢復時間欄位，故 recovery 以觀測翻轉時間為準。
    """

    def __init__(self):
        self.records = {}          # key -> record dict

    def _key(self, row):
        rid = row.get("id")
        if rid is not None:
            return ("id", str(rid))     # 以字串化 ID 去重（與 alarm_id_raw 一致，避免型別差異）
        return ("compose", row.get("targetMark"), row.get("val"), row.get("alarmTime"))

    def _make_record(self, a, now_str, elapsed, origin):
        lvl = a.get("level")
        rid = a.get("id")
        return {
            # 原始 ID 全程字串：Python json 對整數為任意精度 int，str() 即精確；
            # 絕不從 float/科學記號還原（避免 >15 位精度遺失）。
            "alarm_id_raw": "" if rid is None else str(rid),
            "level": _level_text(lvl),
            "_level_raw": lvl,
            "target_object": _code_text(a.get("targetMark")),
            "alarm_content": _code_text(a.get("val")),
            "origin": origin,
            "first_seen_time": now_str,
            "first_seen_elapsed_seconds": elapsed,
            "alarm_start_time": _epoch_ms_to_text(a.get("alarmTime")),
            "_alarm_time_ms": a.get("alarmTime"),      # 供 alarm_code 排序（不輸出）
            "recovery_time": "",
            "duration_seconds": None,
            "is_recovery": False,
            "alarm_status": _alarm_status_text(a.get("alarmStatus")),
            "caused_stop": (lvl in CFG.ALARM_STOP_LEVELS) and origin == "new",
            "_active": _alarm_active(a.get("alarmStatus")),
        }

    def prime_baseline(self, rows, now_str, elapsed):
        for a in rows or []:
            if not isinstance(a, dict):
                continue
            k = self._key(a)
            if k not in self.records:
                self.records[k] = self._make_record(a, now_str, elapsed, origin="pre_existing")

    def update(self, rows, now_str, elapsed):
        """回傳本次『新出現』的告警 list[dict]（供事件記錄）。"""
        new = []
        for a in rows or []:
            if not isinstance(a, dict):
                continue
            k = self._key(a)
            active = _alarm_active(a.get("alarmStatus"))
            if k not in self.records:
                rec = self._make_record(a, now_str, elapsed, origin="new")
                self.records[k] = rec
                new.append(a)
            else:
                rec = self.records[k]
                if rec["_active"] and not active and not rec["is_recovery"]:
                    rec["is_recovery"] = True
                    rec["recovery_time"] = now_str
                    rec["duration_seconds"] = round(elapsed - rec["first_seen_elapsed_seconds"], 1)
                rec["_active"] = active
                rec["alarm_status"] = _alarm_status_text(a.get("alarmStatus"))
        return new

    def export_rows(self):
        """
        輸出 alarms.csv/Excel 列（依 ALARM_FIELDS）。
        alarm_code = ALM-YYYYMMDD-NNN：依告警時間排序、session 內流水號（3 位，從 001）；
        YYYYMMDD 優先取 alarm_start_time 日期，無效則取 first_seen_time 日期。
        （alarm_code 僅供顯示/查找，不參與去重；去重仍以 alarm_id_raw / _key 為準。）
        """
        def _sortkey(r):
            v = r.get("_alarm_time_ms")
            try:
                return (0, int(v))
            except (TypeError, ValueError):
                return (1, r.get("first_seen_elapsed_seconds") or 0)
        out = []
        for i, rec in enumerate(sorted(self.records.values(), key=_sortkey), 1):
            rec["alarm_code"] = f"ALM-{_alarm_yyyymmdd(rec)}-{i:03d}"
            out.append({k: rec.get(k) for k in CFG.ALARM_FIELDS})
        return out

    def load_existing(self, rows):
        """resume：把既有 alarms.csv 列重建為 records（保留歷史、維持去重、不重複新增）。"""
        for a in rows or []:
            rid = str(a.get("alarm_id_raw") or "").strip()
            key = ("id", rid) if rid else ("compose", a.get("target_object"),
                                           a.get("alarm_content"), a.get("alarm_start_time"))
            if key in self.records:
                continue
            self.records[key] = {
                "alarm_id_raw": rid,
                "level": a.get("level"),
                "_level_raw": None,
                "target_object": a.get("target_object"),
                "alarm_content": a.get("alarm_content"),
                "origin": a.get("origin") or "pre_existing",
                "first_seen_time": a.get("first_seen_time"),
                "first_seen_elapsed_seconds": _to_float(a.get("first_seen_elapsed_seconds")) or 0,
                "alarm_start_time": a.get("alarm_start_time"),
                "_alarm_time_ms": None,
                "recovery_time": a.get("recovery_time") or "",
                "duration_seconds": _to_float(a.get("duration_seconds")),
                "is_recovery": str(a.get("is_recovery")) in ("True", "true", "1"),
                "alarm_status": a.get("alarm_status"),
                "caused_stop": str(a.get("caused_stop")) in ("True", "true", "1"),
                "_active": str(a.get("alarm_status") or "") == "告警中",
            }

    @property
    def new_count(self):
        return sum(1 for r in self.records.values() if r["origin"] == "new")


# ======================================================================
# 安全監測（只建議、不送控制）
# ======================================================================
class SafetyMonitor:
    """依 config 門檻檢查每筆 reading，回傳觸發的條件清單（只記錄/建議，不控制）。"""

    def __init__(self, target_power_kw):
        self.target_power_kw = target_power_kw
        self._comm_fail_streak = 0
        self._deviation_since = None

    def check(self, r, elapsed):
        """回傳 list[(condition, severity, detail)]。severity ∈ info/warning/critical。"""
        hits = []

        # 通訊連續失敗
        if not r.get("communication_ok"):
            self._comm_fail_streak += 1
            if self._comm_fail_streak >= CFG.COMM_FAIL_MAX:
                hits.append(("communication_error", "critical",
                             f"連續通訊失敗 {self._comm_fail_streak} 次"))
        else:
            self._comm_fail_streak = 0

        soc = r.get("soc_percent")
        if soc is not None:
            if soc >= CFG.SOC_MAX_PERCENT:
                hits.append(("soc_over_max", "critical", f"SOC {soc}% ≥ 上限 {CFG.SOC_MAX_PERCENT}%"))
            elif soc <= CFG.SOC_MIN_PERCENT:
                hits.append(("soc_under_min", "critical", f"SOC {soc}% ≤ 下限 {CFG.SOC_MIN_PERCENT}%"))

        # PCS 故障 / 停止 / 電池下電
        if "故障" in str(r.get("pcs_status", "")):
            hits.append(("pcs_fault", "critical", f"PCS 狀態：{r.get('pcs_status')}"))
        if r.get("battery_power_status") == "已下電":
            hits.append(("battery_off", "critical", "電池已下電"))

        # 電壓 / 電流門檻
        v = r.get("battery_voltage_v")
        if v is not None and CFG.VOLTAGE_MAX_V is not None and v > CFG.VOLTAGE_MAX_V:
            hits.append(("voltage_over", "warning", f"電壓 {v}V > {CFG.VOLTAGE_MAX_V}V"))
        if v is not None and CFG.VOLTAGE_MIN_V is not None and v < CFG.VOLTAGE_MIN_V:
            hits.append(("voltage_under", "warning", f"電壓 {v}V < {CFG.VOLTAGE_MIN_V}V"))
        a = r.get("battery_current_a")
        if a is not None and CFG.CURRENT_MAX_A is not None and abs(a) > CFG.CURRENT_MAX_A:
            hits.append(("current_over", "warning", f"電流 |{a}|A > {CFG.CURRENT_MAX_A}A"))

        # 實際功率長時間偏離設定值
        p = r.get("actual_active_power_kw")
        if p is not None and self.target_power_kw is not None:
            if abs(abs(p) - abs(self.target_power_kw)) > CFG.POWER_DEVIATION_KW:
                if self._deviation_since is None:
                    self._deviation_since = elapsed
                elif elapsed - self._deviation_since >= CFG.POWER_DEVIATION_SEC:
                    hits.append(("power_deviation", "warning",
                                 f"實際 {p}kW 偏離設定 {self.target_power_kw}kW "
                                 f"逾 {CFG.POWER_DEVIATION_SEC}s"))
            else:
                self._deviation_since = None
        return hits


# ======================================================================
# 報告 Session
# ======================================================================
class ReportSession:
    def __init__(self, action, setpoint_value, control_mode_label, client, output_root,
                 now_fn=None, folder=None, state=None):
        """
        state 為 None → 建立新 Session；否則以 state 續接既有 Session（append/resume）。
        now_fn()：回傳 datetime（可注入供離線測試）；elapsed_seconds = now_fn() - start_dt（牆鐘、跨程序持續）。
        """
        self.client = client
        self.now_fn = now_fn or datetime.now
        self.samples = []
        self.events = []
        self.energy = EnergyAccumulator()
        self.alarm_tracker = AlarmTracker()
        self.end_state = None
        self.end_reason = "unknown"
        self.stop_recommended = False
        self.stop_reasons = []
        self._idle_streak = 0
        self._last_dir_event = None          # 最近一次已記錄的 charge/idle/discharge_start 方向
        self._last_rack_max = None           # Rack 最高溫 fallback：某次 API 無值時沿用上一筆有效值
        self._last_rack_min = None           # Rack 最低溫 fallback

        if state is None:
            # ---------- 新 Session ----------
            self._resumed = False
            self.action = action
            self.control_mode_label = control_mode_label
            self.setpoint_value = setpoint_value
            self.start_dt = self.now_fn()
            # session_id 唯一化：秒級時間可能碰撞（同秒內建立兩個同方向 Session），
            # 若資料夾已存在則加序號，避免撞名共用/覆蓋既有 Session 資料。
            base_id = self.start_dt.strftime(CFG.SESSION_FOLDER_TIME_FMT) + f"_{action}"
            sid, n = base_id, 2
            while os.path.exists(os.path.join(output_root, sid)):
                sid, n = f"{base_id}_{n}", n + 1
            self.session_id = sid
            self.folder = os.path.join(output_root, sid)
            self.sample_index = 0
            self.start_state = None
        else:
            # ---------- 續接既有 Session（resume） ----------
            self._resumed = True
            self.folder = folder
            self.session_id = state.get("session_id") or os.path.basename(folder)
            self.action = state.get("action") or action or "auto"
            self.control_mode_label = state.get("control_mode") or control_mode_label or "交流有功"
            self.setpoint_value = state.get("setpoint_value", setpoint_value)
            self.start_dt = _parse_dt(state.get("start_time")) or self.now_fn()
            self.sample_index = int(state.get("sample_count") or 0)
            self.start_state = state.get("start_state")
            # 續接累積電量（prev 保持 None → 不積分暫停空檔）
            self.energy.seed(state.get("cumulative_charge_energy_kwh"),
                             state.get("cumulative_discharge_energy_kwh"))

        self.status = CFG.SESSION_RECORDING
        # setpoint 衍生（交流有功=功率kW；直流恆流=電流A；直流恆功率=功率kW）
        self.setpoint_kind, self.setpoint_unit, self.setpoint_field = \
            CFG.MODE_SETPOINT.get(self.control_mode_label, ("power", "kW", "power_setpoint_kw"))
        self.target_power_kw = self.setpoint_value if self.setpoint_kind == "power" else None
        self.current_setpoint_a = self.setpoint_value if self.setpoint_kind == "current" else None
        self.device_ip = _device_ip(client)
        self.monitor = SafetyMonitor(self.target_power_kw)

    @classmethod
    def resume(cls, folder, client, now_fn=None):
        """由 session_state.json 續接既有 Session。"""
        state = _load_json_file(os.path.join(folder, CFG.FILE_SESSION_STATE)) or {}
        return cls(None, None, None, client, os.path.dirname(folder),
                   now_fn=now_fn, folder=folder, state=state)

    # ---------- 生命週期 ----------
    def start(self):
        new_folder = not os.path.isdir(self.folder)
        os.makedirs(self.folder, exist_ok=True)
        if new_folder and not self._resumed:
            _log_session_create(self)     # 建立來源 Log（追出是哪個入口/函式建立資料夾）
        if self._resumed:
            return self._start_resume()
        return self._start_new()

    def _start_new(self):
        self._init_csv(CFG.FILE_SAMPLES, CFG.SAMPLE_FIELDS)
        self._init_csv(CFG.FILE_EVENTS, CFG.EVENT_FIELDS)   # alarms.csv 於 finalize/pause 一次寫入
        self.log_event("session_start", "info",
                       f"action={self.action} setpoint={self.setpoint_value}{self.setpoint_unit} "
                       f"mode={self.control_mode_label} device={self.device_ip}")
        r = read_all(self.client)
        self.start_state = self._state_from_reading(r)
        # 既有告警設為 baseline（不算 session 內新增）
        self.alarm_tracker.prime_baseline(r.get("alarm_rows"), _now_str(), self._elapsed())
        # action=auto → 以第一筆方向決定（PCS 旗標優先）
        if self.action == "auto":
            d, src = resolve_direction(r.get("pcs_charging_flag"),
                                       r.get("pcs_discharging_flag"),
                                       r.get("actual_active_power_kw"))
            self.action = d if d in ("charge", "discharge") else "idle"
            self.log_event("direction_detected", "info", f"auto → {self.action}（source={src}）")
        self.log_event("start_state_captured", "info",
                       f"SOC={self.start_state.get('soc_percent')} "
                       f"P={self.start_state.get('actual_active_power_kw')}kW "
                       f"baseline_alarms={len(self.alarm_tracker.records)}")
        self._persist_state()
        return r

    def _start_resume(self):
        # 續接：載入既有 samples.csv（全 Session）與 alarms.csv（維持去重），不重寫表頭
        self.samples = self._load_all_samples()
        self.alarm_tracker.load_existing(self._load_csv_rows(CFG.FILE_ALARMS))
        # 事件 append（不重寫表頭）：只記 recording_resume（session_start 只在新建時出現一次）
        self.log_event("recording_resume", "info",
                       f"resume session={self.session_id} sample_index={self.sample_index} "
                       f"charged={round(self.energy.charged_kwh,4)} discharged={round(self.energy.discharged_kwh,4)}")
        self._persist_state()
        return None

    def sample_once(self):
        elapsed = self._elapsed()
        self.sample_index += 1               # 持續遞增（跨 resume 不歸零）
        r = read_all(self.client)
        power = r.get("actual_active_power_kw") if CFG.POWER_SOURCE == "pcs" else r.get("calculated_power_kw")
        self.energy.add(power, elapsed)      # 以相對秒數積分（dt = elapsed - prev_elapsed）

        # 新增告警（baseline 之後才出現者才算新增）
        new_al = self.alarm_tracker.update(r.get("alarm_rows"), _now_str(), elapsed)

        # 安全監測
        hits = self.monitor.check(r, elapsed)
        critical_stop = None
        for cond, sev, detail in hits:
            self.log_event(cond, sev, detail)
            if sev == "critical":
                self.stop_recommended = True
                if cond not in self.stop_reasons:
                    self.stop_reasons.append(cond)
                critical_stop = cond

        # 新增告警 → 事件記錄 + 是否為停止級別（alarms.csv 於 finalize 一次寫入）
        for a in new_al:
            lvl = a.get("level")
            caused = lvl in CFG.ALARM_STOP_LEVELS
            self.log_event("alarm_new", "critical" if caused else "warning",
                           f"id={a.get('id')} {_code_text(a.get('targetMark'))} "
                           f"{_code_text(a.get('val'))} level={_level_text(lvl)}")
            if caused:
                self.stop_recommended = True
                if "alarm_stop" not in self.stop_reasons:
                    self.stop_reasons.append("alarm_stop")
                critical_stop = critical_stop or "alarm_stop"

        # 方向：PCS 狀態旗標優先，旗標缺失才退回 Power 正負號
        direction, direction_source = resolve_direction(
            r.get("pcs_charging_flag"), r.get("pcs_discharging_flag"), power)
        r["_direction"] = direction
        # 方向切換事件：charge_start / idle_start / discharge_start（同方向不重複記錄）
        if direction in ("charge", "idle", "discharge") and direction != self._last_dir_event:
            self.log_event(f"{direction}_start", "info",
                           f"P={power}kW source={direction_source}")
            self._last_dir_event = direction
        # idle 連續判定（供自動結束）
        if direction == "idle" or "停" in str(r.get("pcs_status", "")):
            self._idle_streak += 1
        else:
            self._idle_streak = 0

        row = self._sample_row(r, elapsed, power, direction, direction_source, len(new_al))
        self.samples.append(row)
        self._append_csv(CFG.FILE_SAMPLES, CFG.SAMPLE_FIELDS, row)
        self._persist_state()               # 每筆取樣後即時持久化（供 resume 續接累積）
        return r, critical_stop

    def should_auto_end(self):
        """回傳 (end?, reason) —— 依 config 判斷是否自動結束（不送控制）。"""
        if CFG.AUTO_END_ON_CRITICAL and self.stop_recommended:
            reason = ("communication_error" if "communication_error" in self.stop_reasons
                      else "alarm_stop" if "alarm_stop" in self.stop_reasons
                      else "pcs_stop" if "battery_off" in self.stop_reasons or "pcs_fault" in self.stop_reasons
                      else "pcs_stop")
            return True, reason
        if self._idle_streak >= CFG.IDLE_END_SAMPLES:
            return True, "pcs_stop"
        if self._elapsed() >= CFG.SESSION_TIMEOUT_SEC:
            return True, "timeout"
        return False, None

    def finalize(self, end_reason):
        """結束 Session（status=completed）並產生完整累積報告。"""
        self.end_reason = end_reason or "unknown"
        r = read_all(self.client)
        self.end_state = self._state_from_reading(r)
        self.status = CFG.SESSION_COMPLETED
        self.log_event("session_end", "info",
                       f"reason={self.end_reason}({CFG.END_REASON.get(self.end_reason,'')}) "
                       f"samples={len(self.samples)}")
        return self._regenerate_outputs()

    def pause(self):
        """暫停 Session（status=paused，可續接）並更新報告快照；不寫 session_end。"""
        self.status = CFG.SESSION_PAUSED
        self.log_event("recording_pause", "info", f"samples={len(self.samples)}")
        return self._regenerate_outputs()

    def _regenerate_outputs(self):
        """以「整份 Session（全 samples/alarms）」重算並重寫 summary/statistics/alarms/xlsx + 狀態。
        圖表以 report.xlsx 內嵌方式產生（不另建 charts/ CSV）。"""
        stats = self._compute_statistics()
        self._write_alarms()
        self._write_statistics(stats)
        self._write_summary(stats)
        self._write_xlsx(stats)
        self._persist_state(stats)
        return stats

    # ---------- 取樣 → 結構 ----------
    def _elapsed(self):
        """牆鐘秒數（相對 Session 開始時間，跨程序 resume 持續累加）。"""
        return round((self.now_fn() - self.start_dt).total_seconds(), 1)

    def _state_from_reading(self, r):
        return {
            "pcs_status": r.get("pcs_status"),
            "pcs_control_mode": r.get("pcs_control_mode"),
            "pcs_work_mode": r.get("pcs_work_mode"),
            "pcs_power_control_mode": r.get("pcs_power_control_mode"),
            "pcs_manual_switch": r.get("pcs_manual_switch"),
            "pcs_schedule_enabled": r.get("pcs_schedule_enabled"),
            "battery_power_status": r.get("battery_power_status"),
            "battery_status": r.get("battery_status"),
            "soc_percent": r.get("soc_percent"),
            "battery_voltage_v": r.get("battery_voltage_v"),
            "battery_current_a": r.get("battery_current_a"),
            "actual_active_power_kw": r.get("actual_active_power_kw"),
            "actual_reactive_power_kvar": r.get("actual_reactive_power_kvar"),
            "alarm_count": r.get("alarm_total"),
            "communication_ok": r.get("communication_ok"),
        }

    def _sample_row(self, r, elapsed, power, direction, direction_source, alarm_new_count):
        raw_p = r.get("actual_active_power_kw")
        raw_i = r.get("battery_current_a")
        # Rack 溫度 fallback：某次 API 暫無值 → 沿用上一筆有效值（避免整欄空白）
        rmax = r.get("rack_max_temperature_c")
        rmin = r.get("rack_min_temperature_c")
        if rmax is None:
            rmax = self._last_rack_max
        else:
            self._last_rack_max = rmax
        if rmin is None:
            rmin = self._last_rack_min
        else:
            self._last_rack_min = rmin
        return {
            "sample_index": self.sample_index,
            "timestamp": _now_str(),
            "elapsed_seconds": elapsed,
            "action": self.action,
            "target_power_kw": self.target_power_kw,
            "pcs_status": r.get("pcs_status"),
            "pcs_control_mode": r.get("pcs_control_mode"),
            "pcs_work_mode": r.get("pcs_work_mode"),
            "pcs_power_control_mode": r.get("pcs_power_control_mode"),
            "actual_active_power_kw": raw_p,
            "actual_reactive_power_kvar": r.get("actual_reactive_power_kvar"),
            "battery_status": r.get("battery_status"),
            "battery_power_status": r.get("battery_power_status"),
            "soc_percent": r.get("soc_percent"),
            "battery_voltage_v": r.get("battery_voltage_v"),
            "battery_current_a": raw_i,
            "rack_max_temperature_c": rmax,
            "rack_min_temperature_c": rmin,
            "calculated_power_kw": (round(r["calculated_power_kw"], 3)
                                    if r.get("calculated_power_kw") is not None else None),
            "cumulative_charge_energy_kwh": round(self.energy.charged_kwh, 4),
            "cumulative_discharge_energy_kwh": round(self.energy.discharged_kwh, 4),
            "cumulative_energy_kwh": round(self.energy.cumulative_signed_kwh, 4),
            "charge_discharge_direction": direction,
            "direction_source": direction_source,
            "alarm_count": r.get("alarm_total"),
            "communication_ok": r.get("communication_ok"),
            "raw_source_time": r.get("raw_source_time"),
            "raw_active_power": raw_p,
            "normalized_active_power": _canonical_signed(raw_p),
            "raw_current": raw_i,
            "normalized_current": _canonical_signed(raw_i),
        }

    def log_event(self, event_type, severity, detail=""):
        row = {"timestamp": _now_str(), "elapsed_seconds": self._elapsed(),
               "event_type": event_type, "severity": severity, "detail": detail}
        self.events.append(row)
        self._append_csv(CFG.FILE_EVENTS, CFG.EVENT_FIELDS, row)

    # ---------- 統計 ----------
    def _compute_statistics(self):
        def _vals(field):
            return [s[field] for s in self.samples if s.get(field) is not None]

        p = _vals("actual_active_power_kw")
        v = _vals("battery_voltage_v")
        i = _vals("battery_current_a")
        socs = _vals("soc_percent")
        start_soc = self.start_state.get("soc_percent") if self.start_state else None
        end_soc = self.end_state.get("soc_percent") if self.end_state else None
        comm_err = sum(1 for s in self.samples if s.get("communication_ok") is False)

        # 充/放電最大功率（量測慣例：充為正、放為負）
        max_charge = max([x for x in p if x > 0], default=0.0)
        max_discharge = min([x for x in p if x < 0], default=0.0)

        charge_e = round(self.energy.charged_kwh, 4)
        discharge_e = round(self.energy.discharged_kwh, 4)
        # 往返效率：需同時具備「完整」充電與放電循環才計算，否則 N/A。
        # 防呆：較小相位須達較大相位的 RTE_MIN_PHASE_RATIO，避免單次充/放電中的
        # 微量反向雜訊被硬算成效率（需求第八點：禁止用單次放電硬算效率）。
        smaller, larger = min(charge_e, discharge_e), max(charge_e, discharge_e)
        if larger > 0 and (smaller / larger) >= CFG.RTE_MIN_PHASE_RATIO:
            rte = round(discharge_e / charge_e * 100, 2)
        else:
            rte = "N/A（缺少完整充放電循環）"

        duration = self._elapsed()
        return {
            "start_soc_percent": start_soc,
            "end_soc_percent": end_soc,
            "max_soc_percent": max(socs) if socs else None,
            "min_soc_percent": min(socs) if socs else None,
            "soc_delta_percent": (round(end_soc - start_soc, 2)
                                  if (start_soc is not None and end_soc is not None) else None),
            "charged_energy_kwh": charge_e,
            "discharged_energy_kwh": discharge_e,
            "net_energy_kwh": round(self.energy.net_kwh, 4),
            "charge_energy_kwh": charge_e,
            "discharge_energy_kwh": discharge_e,
            "round_trip_efficiency_percent": rte,
            "average_active_power_kw": round(sum(p) / len(p), 3) if p else None,
            "max_active_power_kw": max(p) if p else None,
            "min_active_power_kw": min(p) if p else None,
            "max_charge_power_kw": round(max_charge, 3),
            "max_discharge_power_kw": round(max_discharge, 3),
            "max_voltage_v": max(v) if v else None,
            "min_voltage_v": min(v) if v else None,
            "average_voltage_v": round(sum(v) / len(v), 3) if v else None,
            "voltage_delta_v": (round(v[-1] - v[0], 3) if len(v) >= 2 else None),
            "max_current_a": max(i) if i else None,
            "min_current_a": min(i) if i else None,
            "average_current_a": round(sum(i) / len(i), 3) if i else None,
            "sample_count": len(self.samples),
            "alarm_count": self.alarm_tracker.new_count,
            "alarm_total_tracked": len(self.alarm_tracker.records),
            "communication_error_count": comm_err,
            "duration_seconds": duration,
        }

    # ---------- 輸出 ----------
    def _path(self, name):
        return os.path.join(self.folder, name)

    def _init_csv(self, name, fields):
        with open(self._path(name), "w", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()

    def _append_csv(self, name, fields, row):
        try:
            with open(self._path(name), "a", encoding="utf-8-sig", newline="") as f:
                csv.DictWriter(f, fieldnames=fields, extrasaction="ignore",
                               restval="").writerow(row)
        except OSError as e:
            print(f"  [警告] 寫入 {name} 失敗：{e}")

    def _load_csv_rows(self, name):
        """讀既有 CSV → list[dict]（字串值）；檔案不存在回 []。"""
        path = self._path(name)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
        except OSError:
            return []

    def _load_all_samples(self):
        """resume：讀回既有 samples.csv 全部列，數值欄轉 float、communication_ok 轉 bool、空字串轉 None。"""
        out = []
        for row in self._load_csv_rows(CFG.FILE_SAMPLES):
            rec = {}
            for k, v in row.items():
                if v == "" or v is None:
                    rec[k] = None
                elif k in CFG.SAMPLE_NUMERIC_FIELDS:
                    rec[k] = _to_float(v)
                elif k == "communication_ok":
                    rec[k] = (str(v) == "True")
                else:
                    rec[k] = v
            out.append(rec)
        return out

    def _persist_state(self, stats=None):
        """寫 session_state.json（供 resume 續接：狀態/起始時間/最後取樣/累積電量/start_state）。"""
        last = self.samples[-1] if self.samples else {}
        state = {
            "session_id": self.session_id,
            "status": self.status,
            "action": self.action,
            "control_mode": self.control_mode_label,
            "setpoint_value": self.setpoint_value,
            "start_time": self.start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "last_sample_time": last.get("timestamp"),
            "last_elapsed_seconds": last.get("elapsed_seconds"),
            "sample_count": self.sample_index,
            "cumulative_charge_energy_kwh": round(self.energy.charged_kwh, 4),
            "cumulative_discharge_energy_kwh": round(self.energy.discharged_kwh, 4),
            "net_energy_kwh": round(self.energy.net_kwh, 4),
            "start_state": self.start_state,
            "device_ip": self.device_ip,
            "report_version": CFG.REPORT_VERSION,
            "schema_version": CFG.SCHEMA_VERSION,
        }
        self._dump_json(CFG.FILE_SESSION_STATE, state)

    def _meta(self):
        import platform
        return {
            "report_version": CFG.REPORT_VERSION,
            "schema_version": CFG.SCHEMA_VERSION,
            "git_commit": _git_commit(),
            "python_version": platform.python_version(),
            "created_by": _created_by(),
            "session_id": self.session_id,
            "session_folder": self.folder,
        }

    def _session_block(self, stats):
        ss = self.start_state or {}
        return {
            "session_id": self.session_id,
            "action": self.action,
            "control_mode": self.control_mode_label,
            "report_created_time": self.start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "start_time": self.start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": _now_str(),
            "duration_seconds": stats.get("duration_seconds"),
            "target_power_kw": self.target_power_kw,
            "operator": CFG.DEFAULT_OPERATOR,
            "device_name": CFG.DEVICE_NAME,
            "device_ip": self.device_ip,
            "pcs_no": CFG.PCS_NO,
            "rack_no": CFG.RACK_NO,
            # PCS Mode / Battery Mode（取自開始狀態；PCS Mode 用功率控制模式，控制模式另列）
            "pcs_mode": ss.get("pcs_power_control_mode"),
            "pcs_control_mode": ss.get("pcs_control_mode"),
            "battery_mode": ss.get("battery_status"),
            "report_version": CFG.REPORT_VERSION,
            "schema_version": CFG.SCHEMA_VERSION,
            "result": "completed" if self.end_reason == "completed" else self.end_reason,
            "end_reason": self.end_reason,
            "end_reason_text": CFG.END_REASON.get(self.end_reason, "未知"),
        }

    def _control_block(self, stats):
        """控制資訊：控制模式 / 方向 / setpoint（功率或電流）/ 實際量測 / 設定 vs 實際比較。"""
        actual_avg = stats.get("average_active_power_kw")
        actual_max = stats.get("max_active_power_kw")
        setpoint_signed = None
        deviation = None
        if self.setpoint_kind == "power" and self.setpoint_value is not None:
            # 設定值以量測慣例帶號（充=正、放=負）方便與實際量測直接比較
            setpoint_signed = (abs(self.setpoint_value) if self.action == "charge"
                               else -abs(self.setpoint_value) if self.action == "discharge"
                               else self.setpoint_value)
            if actual_avg is not None:
                deviation = round(actual_avg - setpoint_signed, 3)
        return {
            "control_mode": self.control_mode_label,      # 交流有功 / 直流恆流 / 直流恆功率
            "direction": self.action,                     # charge / discharge
            "setpoint_kind": self.setpoint_kind,          # power / current
            "setpoint_value": self.setpoint_value,
            "setpoint_unit": self.setpoint_unit,
            "power_setpoint_kw": self.target_power_kw,
            "current_setpoint_a": self.current_setpoint_a,
            "setpoint_signed_by_measured_convention_kw": setpoint_signed,
            "measured_avg_active_power_kw": actual_avg,
            "measured_max_active_power_kw": actual_max,
            "setpoint_vs_actual": {
                "setpoint": self.setpoint_value,
                "unit": self.setpoint_unit,
                "actual_avg_power_kw": actual_avg,
                "deviation_avg_kw": deviation,
            },
        }

    def _write_statistics(self, stats):
        obj = {**self._meta(), "generated_at": _now_str(), "statistics": stats}
        self._dump_json(CFG.FILE_STATISTICS, obj)

    def _write_summary(self, stats):
        obj = {
            **self._meta(),
            "session": self._session_block(stats),
            "control": self._control_block(stats),
            "start_state": self.start_state,
            "end_state": self.end_state,
            "statistics": stats,
            "validation": {
                "control_success": None,       # 獨立報告不介入控制 → None
                "verify_success": None,
                "data_complete": stats.get("sample_count", 0) > 0
                                 and stats.get("communication_error_count", 0) == 0,
                "stop_recommended": self.stop_recommended,
                "stop_reasons": self.stop_reasons,
                "warnings": [],
            },
        }
        self._dump_json(CFG.FILE_SUMMARY, obj)

    def _dump_json(self, name, obj):
        try:
            with open(self._path(name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"  [警告] 寫入 {name} 失敗：{e}")

    def _write_alarms(self):
        """alarms.csv：於 finalize 一次寫入（含 baseline/new、recovery/duration）。"""
        rows = self.alarm_tracker.export_rows()
        try:
            with open(self._path(CFG.FILE_ALARMS), "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CFG.ALARM_FIELDS, extrasaction="ignore", restval="")
                w.writeheader()
                w.writerows(rows)
        except OSError as e:
            print(f"  [警告] 寫入 {CFG.FILE_ALARMS} 失敗：{e}")

    # ---------- 分析（純資料；供 Excel Summary / KPI / Analysis 使用）----------
    def _direction_seconds(self):
        """依 elapsed 差分把時間歸屬給後一筆的方向，回傳 {'charge','discharge','idle'} 秒數。"""
        out = {"charge": 0.0, "discharge": 0.0, "idle": 0.0}
        prev = None
        for s in self.samples:
            e = _to_float(s.get("elapsed_seconds"))
            d = s.get("charge_discharge_direction")
            if prev is not None and e is not None and e > prev and d in out:
                out[d] += (e - prev)
            if e is not None:
                prev = e
        return out

    def _dir_summary(self, rows):
        """單一方向（charge 或 discharge）之 SOC/功率/電壓/電流摘要。"""
        soc = _col_floats(rows, "soc_percent")
        p = _col_floats(rows, "actual_active_power_kw")
        v = _col_floats(rows, "battery_voltage_v")
        i = _col_floats(rows, "battery_current_a")
        start_soc = soc[0] if soc else None
        end_soc = soc[-1] if soc else None
        return {
            "count": len(rows),
            "start_soc": start_soc, "end_soc": end_soc,
            "soc_change": (round(end_soc - start_soc, 2)
                           if (start_soc is not None and end_soc is not None) else None),
            "avg_power": round(sum(p) / len(p), 3) if p else None,
            "max_power": (max(p, key=abs) if p else None),
            "avg_voltage": round(sum(v) / len(v), 3) if v else None,
            "max_voltage": max(v) if v else None, "min_voltage": min(v) if v else None,
            "avg_current": round(sum(i) / len(i), 3) if i else None,
            "max_current": (max(i, key=abs) if i else None),
        }

    def _analyze(self, stats, charge_rows, discharge_rows):
        """自動分析：Power/Voltage/Current/SOC/Communication/Alarm；回傳 items/warnings/stars/comm_rate。"""
        all_rows = self.samples
        items = []

        # Power：單一方向內波動過大 + Power 與 Direction 不一致
        pc = _col_floats(charge_rows, "actual_active_power_kw")
        pd = _col_floats(discharge_rows, "actual_active_power_kw")
        power_ok, pmsgs = True, []
        for nm, arr in (("充電", pc), ("放電", pd)):
            if len(arr) >= 2 and (max(arr) - min(arr)) > CFG.ANALYSIS_POWER_FLUCT_KW:
                power_ok = False
                pmsgs.append(f"{nm}期間功率波動過大({round(max(arr) - min(arr), 1)}kW)")
        mism = (sum(1 for s in charge_rows if (_to_float(s.get("actual_active_power_kw")) or 0) < 0)
                + sum(1 for s in discharge_rows if (_to_float(s.get("actual_active_power_kw")) or 0) > 0))
        if mism:
            power_ok = False
            pmsgs.append(f"Power與Direction不一致({mism}筆)")
        items.append(("Power Analysis", power_ok, "功率穩定" if power_ok else "；".join(pmsgs)))

        # Voltage
        v = _col_floats(all_rows, "battery_voltage_v")
        volt_ok, vmsgs = True, []
        if v and CFG.VOLTAGE_MAX_V is not None and max(v) > CFG.VOLTAGE_MAX_V:
            volt_ok = False; vmsgs.append(f"電壓過高{max(v)}V")
        if v and CFG.VOLTAGE_MIN_V is not None and min(v) < CFG.VOLTAGE_MIN_V:
            volt_ok = False; vmsgs.append(f"電壓過低{min(v)}V")
        items.append(("Voltage Analysis", volt_ok, "電壓正常" if volt_ok else "；".join(vmsgs)))

        # Current
        i = _col_floats(all_rows, "battery_current_a")
        cur_ok, cmsgs = True, []
        if i and CFG.CURRENT_MAX_A is not None and max(abs(x) for x in i) > CFG.CURRENT_MAX_A:
            cur_ok = False; cmsgs.append(f"電流過大{max(i, key=abs)}A")
        items.append(("Current Analysis", cur_ok, "電流正常" if cur_ok else "；".join(cmsgs)))

        # SOC 方向一致性
        soc_ok, smsgs = True, []
        tol = CFG.ANALYSIS_SOC_TOL_PERCENT
        sc = _col_floats(charge_rows, "soc_percent")
        if len(sc) >= 2 and sc[-1] < sc[0] - tol:
            soc_ok = False; smsgs.append(f"充電期間SOC下降({sc[0]}→{sc[-1]})")
        sd = _col_floats(discharge_rows, "soc_percent")
        if len(sd) >= 2 and sd[-1] > sd[0] + tol:
            soc_ok = False; smsgs.append(f"放電期間SOC增加({sd[0]}→{sd[-1]})")
        items.append(("SOC Analysis", soc_ok, "SOC正常" if soc_ok else "；".join(smsgs)))

        # Communication（資料完整率）
        n = len(all_rows)
        ok_n = sum(1 for s in all_rows if s.get("communication_ok") in (True, "True"))
        comm_rate = (ok_n / n) if n else 1.0
        comm_ok = comm_rate >= CFG.ANALYSIS_COMM_MIN_RATE
        items.append(("Communication Analysis", comm_ok,
                      f"資料完整率{comm_rate * 100:.1f}%" + ("" if comm_ok else "（有通訊中斷）")))

        # Alarm
        ac = stats.get("alarm_count") or 0
        alarm_ok = (ac == 0)
        items.append(("Alarm Analysis", alarm_ok, "無PCS故障/告警" if alarm_ok else f"發生{ac}筆新增告警"))

        warnings = [f"{cat}：{det}" for cat, ok, det in items if not ok]
        return {"items": items, "warnings": warnings, "comm_rate": comm_rate}

    def _write_xlsx(self, stats):
        try:
            from openpyxl import Workbook
            from openpyxl.chart import LineChart, ScatterChart, Series, Reference
            from openpyxl.chart.axis import ChartLines
            from openpyxl.chart.marker import Marker
            from openpyxl.chart.legend import Legend
            from openpyxl.chart.layout import Layout, ManualLayout
            from openpyxl.chart.shapes import GraphicalProperties
            from openpyxl.drawing.line import LineProperties
            from openpyxl.chart.text import RichText, Text
            from openpyxl.chart.title import Title
            from openpyxl.chart.data_source import AxDataSource, StrRef
            from openpyxl.chart.series import SeriesLabel
            from openpyxl.drawing.text import (Paragraph, ParagraphProperties,
                                               CharacterProperties, RichTextProperties,
                                               RegularTextRun)
            from openpyxl.utils import get_column_letter
            from openpyxl.worksheet.table import Table, TableStyleInfo
            from openpyxl.worksheet.properties import PageSetupProperties
            from openpyxl.styles import Font, PatternFill
        except ImportError:
            print("  [提示] 未安裝 openpyxl，略過 report.xlsx（pip install openpyxl 後可產生）。")
            return

        # ---- 資料分割（共用同一份 samples，只依 direction 過濾）----
        all_rows = self.samples
        charge_rows = [s for s in all_rows if s.get("charge_discharge_direction") == "charge"]
        discharge_rows = [s for s in all_rows if s.get("charge_discharge_direction") == "discharge"]
        dir_sec = self._direction_seconds()
        analysis = self._analyze(stats, charge_rows, discharge_rows)
        sess = self._session_block(stats)
        cs = self._dir_summary(charge_rows)
        ds = self._dir_summary(discharge_rows)

        # ---- 樣式 ----
        TITLE = Font(bold=True, size=14, color="1F4E78")
        SECT = Font(bold=True, color="FFFFFF")
        SECT_FILL = PatternFill("solid", fgColor="305496")
        HDR = Font(bold=True, color="FFFFFF")
        HDR_FILL = PatternFill("solid", fgColor="305496")
        KEY = Font(bold=True)
        HI = PatternFill("solid", fgColor="FFF2CC")     # 醒目（重要數值）
        OKF = PatternFill("solid", fgColor="C6EFCE")    # 綠：正常
        WARNF = PatternFill("solid", fgColor="FFC7CE")  # 紅：警告

        def _autofit(ws, ncols, minw=10, maxw=44):
            for c in range(1, ncols + 1):
                L = get_column_letter(c)
                w = minw
                for cell in ws[L]:
                    if cell.value is not None:
                        w = max(w, min(maxw, len(str(cell.value)) + 2))
                ws.column_dimensions[L].width = w

        def _hdr_row(ws, row, ncols):
            for c in range(1, ncols + 1):
                cell = ws.cell(row=row, column=c)
                cell.font = HDR
                cell.fill = HDR_FILL

        def _table(ws, ref, name, style="TableStyleMedium9"):
            t = Table(displayName=name, ref=ref)
            t.tableStyleInfo = TableStyleInfo(name=style, showRowStripes=True, showColumnStripes=False)
            ws.add_table(t)

        def _section(ws, row, col, text):
            c = ws.cell(row=row, column=col, value=text)
            c.font = SECT
            c.fill = SECT_FILL
            ws.cell(row=row, column=col + 1).fill = SECT_FILL
            return row + 1

        def _kv(ws, row, col, key, val, hi=False):
            kc = ws.cell(row=row, column=col, value=key)
            kc.font = KEY
            vc = ws.cell(row=row, column=col + 1, value=val)
            if hi:
                vc.fill = HI
                vc.font = KEY
            return row + 1

        def _axis_font(axis, sz=950, rot=None):
            """設定座標軸文字字型（sz 單位 1/100 pt；950=9.5pt）；rot 為旋轉角（1/60000 度，-5400000＝-90°）。"""
            cp = CharacterProperties(sz=sz)
            body = RichTextProperties(rot=rot, vert="horz") if rot is not None else None
            axis.txPr = RichText(bodyPr=body,
                                 p=[Paragraph(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])

        def _tick_skip(rows):
            """依 timestamp（elapsed 差分）換算 X 軸每約 60 秒一個刻度所需的『跳過筆數』。"""
            el = _col_floats(rows, "elapsed_seconds")
            diffs = sorted(b - a for a, b in zip(el, el[1:]) if b > a)
            if not diffs:
                return 1
            med = diffs[len(diffs) // 2]
            return max(1, int(round(60.0 / med))) if med > 0 else 1

        def _combined_chart(ws_data, title, colidx, n, has_temp, cat_col, y_lo, y_hi, major_unit,
                            label_count):
            """
            整合圖（LineChart 類別軸，每個紀錄頁只此一張，三頁規格一致）：
              - X 類別軸：cat_col 存「稀疏 Label 欄」（±3s 演算法手選的實際時間，其餘空）；
                tickLblSkip=1（逐格，但欄本身多為空→只顯示手選 Label）；標籤垂直(-90°)、tickLblPos=low。
              - 折線用完整資料（不降採樣）。電流 + Rack 最大/最小溫度 共用左側 Y 軸（無副軸）。
              - Y 軸：以 0 為基準、Major Unit 固定 5、上下限為 5 的倍數（含 0）、整數。
              - 主要水平格線（淡灰）；無垂直格線；底部精簡 Legend；無 X/Y 軸標題；保留圖表標題。
              - has_temp=False → 只畫電流並於標題註記「無 Rack 溫度資料」。
            """
            cat = Reference(ws_data, min_col=cat_col, min_row=2, max_row=n + 1)
            cat_f = str(cat)
            def _catsrc():
                return AxDataSource(strRef=StrRef(f=cat_f))
            left = LineChart()
            # 標題 12pt 粗體、水平置中（Excel 標題預設置中）；overlay=False 置於繪圖區上方
            _title_text = title if has_temp else f"{title}（無 Rack 溫度資料）"
            _tcp = CharacterProperties(sz=1200, b=True)   # 12pt 粗體
            _tpara = Paragraph(pPr=ParagraphProperties(defRPr=_tcp),
                               r=[RegularTextRun(rPr=_tcp, t=_title_text)])
            left.title = Title(tx=Text(rich=RichText(bodyPr=RichTextProperties(), p=[_tpara])))
            left.title.overlay = False
            left.x_axis.title = None
            left.y_axis.title = None
            left.width = _chart_width_cm(label_count)    # 尺寸（固定或自適應，依 config）
            left.height = CFG.CHART_HEIGHT_CM
            left.layout = Layout(manualLayout=ManualLayout(
                layoutTarget="inner", xMode="edge", yMode="edge",
                x=0.045, y=0.045, w=0.95, h=0.84))
            left.add_data(Reference(ws_data, min_col=colidx["Current(A)"], min_row=1, max_row=n + 1),
                          titles_from_data=True)
            if has_temp:
                for hdr in ("Rack最大溫度(°C)", "Rack最小溫度(°C)"):
                    left.add_data(Reference(ws_data, min_col=colidx[hdr], min_row=1, max_row=n + 1),
                                  titles_from_data=True)
            for s in left.series:
                s.cat = _catsrc()
            left.series[0].tx = SeriesLabel(v="電流")     # 圖例：Current(A) → 電流

            # X 類別軸（底部）：稀疏 Label、垂直、tickLblPos=low（不隨 Y=0 crossing 跑到中間）
            left.x_axis.axId = 10
            left.x_axis.axPos = "b"
            left.x_axis.crossAx = 100
            left.x_axis.auto = False
            left.x_axis.delete = False
            left.x_axis.tickLblSkip = 1
            left.x_axis.tickMarkSkip = 1
            left.x_axis.tickLblPos = "low"
            left.x_axis.majorGridlines = None            # 無垂直格線

            # 共用左 Y 軸：Major Unit 固定 5、取整到 5 倍數含 0、整數；關閉左右 Margin（首尾貼邊）
            left.y_axis.axId = 100
            left.y_axis.axPos = "l"
            left.y_axis.crossAx = 10
            left.y_axis.scaling.min = y_lo
            left.y_axis.scaling.max = y_hi
            left.y_axis.majorUnit = major_unit
            left.y_axis.number_format = "0"
            left.y_axis.crosses = "autoZero"
            left.y_axis.crossBetween = "midCat"
            left.y_axis.delete = False
            grid_gp = GraphicalProperties()
            grid_gp.line = LineProperties(solidFill="D9D9D9", w=9525)   # 淡灰水平格線
            left.y_axis.majorGridlines = ChartLines(spPr=grid_gp)

            # Legend 精簡、底部置中、貼近 X 標籤（h 小、y 近底部）
            left.legend = Legend()                       # 電流/Rack最大溫度/Rack最小溫度（同一列）
            left.legend.position = "b"
            left.legend.overlay = False
            left.legend.layout = Layout(manualLayout=ManualLayout(
                xMode="edge", yMode="edge", x=0.20, y=0.95, w=0.60, h=0.04))
            # Legend 文字 10pt、水平（vert=horz，維持一列不直排）
            _lcp = CharacterProperties(sz=1000)
            left.legend.txPr = RichText(bodyPr=RichTextProperties(rot=0, vert="horz"),
                                        p=[Paragraph(pPr=ParagraphProperties(defRPr=_lcp),
                                                     endParaRPr=_lcp)])
            _axis_font(left.x_axis, sz=800, rot=-5400000)   # X 標籤 8pt、垂直 -90°
            _axis_font(left.y_axis, sz=900)                  # Y 刻度 9pt
            return left

        def _hms(ts):
            """'YYYY-MM-DD HH:MM:SS' → 純文字字串 'HH:MM:SS'（X 軸文字分類用，非 Excel 時間值）。"""
            s = str(ts) if ts is not None else ""
            if len(s) >= 19 and s[10] == " ":
                return s[11:19]
            return s

        def _axis_time_labels(rows, step_sec=CFG.X_AXIS_LABEL_INTERVAL_SEC, window_sec=3):
            """
            以「表單原始時間戳」產生稀疏 X 軸 Label（回傳 (labels, log)）：
              1) 以第一筆時間 t0 為基準，目標時間 = t0+N, t0+2N, …（N=step_sec）。
              2) 每個目標取 ±window_sec 秒區間內、最接近目標的一筆「實際紀錄時間」為 Label。
              3) 區間內無資料 → 該目標略過（不顯示、不占位、不抓區間外）。
              4) 同一筆不可被兩個目標重複選中（去重）。
              5) 第一筆與最後一筆一定強制顯示（即使不合 N±window 規則）。
              6) 其餘筆為 ''；折線仍用完整資料（不降採樣）。
            log：[(target_sec, win_lo, win_hi, selected_sec 或 None)]，供列印驗證（§9）。
            """
            def _to_sec(t):
                try:
                    h, m, sec = (int(x) for x in t.split(":"))
                    return h * 3600 + m * 60 + sec
                except (ValueError, AttributeError):
                    return None
            secs = [_to_sec(_hms(s.get("timestamp"))) for s in rows]
            valid = [(i, sv) for i, sv in enumerate(secs) if sv is not None]
            labels = [""] * len(rows)
            log = []
            if not valid:
                return labels, log
            t0, tlast = valid[0][1], valid[-1][1]
            first_idx, last_idx = valid[0][0], valid[-1][0]
            used = set()
            k = 1
            while t0 + step_sec * k < tlast:          # 中間目標（首尾另外強制）
                target = t0 + step_sec * k
                lo, hi = target - window_sec, target + window_sec
                cands = [(i, sv) for (i, sv) in valid
                         if lo <= sv <= hi and i not in used and i not in (first_idx, last_idx)]
                if cands:
                    idx, sv = min(cands, key=lambda p: (abs(p[1] - target), p[1]))  # 最近；同距取較早
                    labels[idx] = _hms(rows[idx].get("timestamp"))
                    used.add(idx)
                    log.append((target, lo, hi, sv))
                else:
                    log.append((target, lo, hi, None))     # 略過（區間內無可用資料）
                k += 1
            # 強制首尾（實際時間）
            labels[first_idx] = _hms(rows[first_idx].get("timestamp"))
            labels[last_idx] = _hms(rows[last_idx].get("timestamp"))
            return labels, log

        wb = Workbook()

        # ================= Sheet 1：Summary（Dashboard）=================
        ws = wb.active
        ws.title = "Summary"
        ws["A1"] = "PCS 充放電報告 — Summary"
        ws["A1"].font = TITLE
        r = 3
        r = _section(ws, r, 1, "Session資訊")
        r = _kv(ws, r, 1, "Session ID", sess["session_id"])
        r = _kv(ws, r, 1, "開始時間", sess["start_time"])
        r = _kv(ws, r, 1, "結束時間", sess["end_time"])
        r = _kv(ws, r, 1, "總持續時間", _fmt_hms(stats.get("duration_seconds")))
        r = _kv(ws, r, 1, "完成原因", f"{sess['end_reason']}（{sess['end_reason_text']}）")
        r = _kv(ws, r, 1, "控制模式", sess["control_mode"])
        r = _kv(ws, r, 1, "PCS模式", sess["pcs_mode"])
        r += 1
        r = _section(ws, r, 1, "充放電統計")
        r = _kv(ws, r, 1, "Charge Energy (kWh)", stats.get("charged_energy_kwh"), hi=True)
        r = _kv(ws, r, 1, "Discharge Energy (kWh)", stats.get("discharged_energy_kwh"), hi=True)
        r = _kv(ws, r, 1, "Net Energy (kWh)", stats.get("net_energy_kwh"), hi=True)
        r += 1
        r = _section(ws, r, 1, "SOC (開始→最高→結束)")
        r = _kv(ws, r, 1, "開始SOC(%)", stats.get("start_soc_percent"))
        r = _kv(ws, r, 1, "最高SOC(%)", stats.get("max_soc_percent"))
        r = _kv(ws, r, 1, "結束SOC(%)", stats.get("end_soc_percent"))
        r += 1
        r = _section(ws, r, 1, "Voltage (最低/平均/最高 V)")
        r = _kv(ws, r, 1, "最低", stats.get("min_voltage_v"))
        r = _kv(ws, r, 1, "平均", stats.get("average_voltage_v"))
        r = _kv(ws, r, 1, "最高", stats.get("max_voltage_v"))
        r += 1
        r = _section(ws, r, 1, "Current (最低/平均/最高 A)")
        r = _kv(ws, r, 1, "最低", stats.get("min_current_a"))
        r = _kv(ws, r, 1, "平均", stats.get("average_current_a"))
        r = _kv(ws, r, 1, "最高", stats.get("max_current_a"))
        r += 1
        r = _section(ws, r, 1, "Power (最低/平均/最高 kW)")
        r = _kv(ws, r, 1, "最低", stats.get("min_active_power_kw"))
        r = _kv(ws, r, 1, "平均", stats.get("average_active_power_kw"))
        r = _kv(ws, r, 1, "最高", stats.get("max_active_power_kw"))
        r += 1
        r = _section(ws, r, 1, "Communication")
        r = _kv(ws, r, 1, "資料完整率", f"{analysis['comm_rate'] * 100:.1f}%",
                hi=(analysis["comm_rate"] < 1.0))
        r += 1
        r = _section(ws, r, 1, "Alarm")
        r = _kv(ws, r, 1, "告警數", stats.get("alarm_count"), hi=(stats.get("alarm_count") or 0) > 0)
        # 註：Health（星等評分）已移除；Summary 最後保留 Communication 與 Alarm。
        _autofit(ws, 2, minw=16, maxw=52)

        # ================= Sheet 2：KPI =================
        wsk = wb.create_sheet("KPI")
        wsk.append(["項目", "值"])
        kpi_rows = [
            ("充電時間", _fmt_hms(dir_sec["charge"])),
            ("放電時間", _fmt_hms(dir_sec["discharge"])),
            ("平均功率(kW)", stats.get("average_active_power_kw")),
            ("最大功率(kW)", stats.get("max_active_power_kw")),
            ("最小功率(kW)", stats.get("min_active_power_kw")),
            ("最大充電功率(kW)", stats.get("max_charge_power_kw")),
            ("最大放電功率(kW)", stats.get("max_discharge_power_kw")),
            ("平均電壓(V)", stats.get("average_voltage_v")),
            ("最高電壓(V)", stats.get("max_voltage_v")),
            ("最低電壓(V)", stats.get("min_voltage_v")),
            ("平均電流(A)", stats.get("average_current_a")),
            ("最大電流(A)", stats.get("max_current_a")),
            ("最小電流(A)", stats.get("min_current_a")),
            ("SOC增加(充電)", cs["soc_change"]),
            ("SOC下降(放電)", (round(-ds["soc_change"], 2) if ds["soc_change"] is not None else None)),
            ("充電量(kWh)", stats.get("charged_energy_kwh")),
            ("放電量(kWh)", stats.get("discharged_energy_kwh")),
            ("Net Energy(kWh)", stats.get("net_energy_kwh")),
            ("往返效率(%)", stats.get("round_trip_efficiency_percent")),
            ("資料筆數", stats.get("sample_count")),
            ("資料完整率", f"{analysis['comm_rate'] * 100:.1f}%"),
        ]
        for k, v in kpi_rows:
            wsk.append([k, v])
        _hdr_row(wsk, 1, 2)
        wsk.freeze_panes = "A2"
        if kpi_rows:
            _table(wsk, f"A1:B{len(kpi_rows) + 1}", "tbl_kpi")
        _autofit(wsk, 2, minw=16, maxw=40)
        # KPI 頁只保留統計表格：不新增任何圖表、不留圓餅圖輔助資料區塊。

        # ================= 紀錄頁（充放電/充電/放電）共用建構 =================
        rec_headers = [h for h, _f in CFG.RECORD_COLUMNS]
        rec_fields = [f for _h, f in CFG.RECORD_COLUMNS]
        rec_colidx = {h: k + 1 for k, (h, _f) in enumerate(CFG.RECORD_COLUMNS)}

        def _record_sheet(title, rows, dash_title, chart_title, summary_pairs, analysis_lines, tname):
            wr = wb.create_sheet(title)
            wr.append(rec_headers)
            for s in rows:
                # 時間欄存為純文字字串（X 軸文字分類，非 Excel 時間值）；其餘欄照舊
                wr.append([_hms(s.get(f)) if f == "timestamp" else s.get(f) for f in rec_fields])
            _hdr_row(wr, 1, len(rec_headers))
            wr.freeze_panes = "A2"
            n = len(rows)
            # 時間欄（A）資料格設文字格式 @（category 為純文字字串，避免 Excel 判為時間軸）
            tcol = get_column_letter(rec_colidx["時間"])
            for rw in range(2, n + 2):
                wr[f"{tcol}{rw}"].number_format = "@"
            if n >= 1:
                _table(wr, f"A1:{get_column_letter(len(rec_headers))}{n + 1}", tname)
            _autofit(wr, len(rec_headers))
            # 右側資訊區（表格右方空一欄）
            scol = len(rec_headers) + 2
            rr = 1
            wr.cell(row=rr, column=scol, value=f"【{dash_title}】").font = TITLE
            rr += 2
            rr = _section(wr, rr, scol, "Summary")
            for k, v, hi in summary_pairs:
                rr = _kv(wr, rr, scol, k, v, hi)
            if analysis_lines:
                rr += 1
                rr = _section(wr, rr, scol, "Analysis")
                for line, ok in analysis_lines:
                    cc = wr.cell(row=rr, column=scol,
                                 value=("✓ " if ok else "⚠ ") + line)
                    cc.fill = OKF if ok else WARNF
                    rr += 1
            wr.column_dimensions[get_column_letter(scol)].width = 26
            wr.column_dimensions[get_column_letter(scol + 1)].width = 22
            # 單一整合折線圖（此頁唯一圖表；X=±3s 演算法稀疏標籤、電流+Rack溫度共用 Y 軸、底部精簡 Legend）
            if n >= 1:
                has_temp = bool(_col_floats(rows, "rack_max_temperature_c")
                                or _col_floats(rows, "rack_min_temperature_c"))
                # 稀疏 Label 欄（AN，隱藏、遠離 Summary）：±3s 演算法手選的實際時間，其餘空。
                lbl_col = 40                                 # AN 欄
                lbl_letter = get_column_letter(lbl_col)
                lbl_step = CFG.X_AXIS_LABEL_INTERVAL_SEC     # 統一由 config 控制（不硬編碼）
                x_labels, x_log = _axis_time_labels(rows, step_sec=lbl_step, window_sec=3)
                wr.cell(row=1, column=lbl_col, value=f"X軸標籤(每{lbl_step}秒±3s)")
                for i, lab in enumerate(x_labels):
                    c = wr.cell(row=2 + i, column=lbl_col, value=lab)
                    c.number_format = "@"
                wr.column_dimensions[lbl_letter].width = 12
                wr.column_dimensions[lbl_letter].hidden = True   # 隱藏整欄（不顯示給使用者）
                label_count = sum(1 for l in x_labels if l)
                # §9 驗證列印：第一/最後時間、N、每個目標±3s區間、選中實際時間 / 略過
                def _fs(sec):
                    sec = int(sec) % 86400
                    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
                _hms_rows = [_hms(s.get("timestamp")) for s in rows]
                print(f"[X-LABEL 驗證] {title}｜第一筆={_hms_rows[0]} 最後={_hms_rows[-1]} "
                      f"N={lbl_step}s ±3s｜目標數={len(x_log)} 顯示={label_count}")
                for (tg, lo, hi, sel) in x_log:
                    print(f"    目標 {_fs(tg)}  區間[{_fs(lo)}~{_fs(hi)}] → "
                          + (f"選中 {_fs(sel)}" if sel is not None else "略過(無資料)"))
                # 共用 Y 軸：取「電流 + Rack 最大/最小溫度」整體，含 0；Major Unit 固定 5、取整到 5 的倍數。
                hf = dict(CFG.RECORD_COLUMNS)
                yvals = list(_col_floats(rows, hf["Current(A)"]))
                if has_temp:
                    yvals += _col_floats(rows, hf["Rack最大溫度(°C)"])
                    yvals += _col_floats(rows, hf["Rack最小溫度(°C)"])
                y_lo, y_hi, y_mu = _y_axis_scale(yvals)
                ch = _combined_chart(wr, chart_title, rec_colidx, n, has_temp, lbl_col,
                                     y_lo, y_hi, y_mu, label_count)
                wr.add_chart(ch, f"{get_column_letter(scol)}{rr + 2}")
            return wr

        # Sheet 3：充放電紀錄（全部資料）
        analysis_lines = [(f"{cat}：{det}", ok) for cat, ok, det in analysis["items"]]
        session_pairs = [
            ("開始", sess["start_time"], False),
            ("結束", sess["end_time"], False),
            ("充電時間", _fmt_hms(dir_sec["charge"]), False),
            ("放電時間", _fmt_hms(dir_sec["discharge"]), False),
            ("Charge(kWh)", stats.get("charged_energy_kwh"), True),
            ("Discharge(kWh)", stats.get("discharged_energy_kwh"), True),
            ("Net(kWh)", stats.get("net_energy_kwh"), True),
            ("取樣筆數", stats.get("sample_count"), False),
        ]
        _record_sheet("充放電紀錄", all_rows, "Session Analysis",
                      "Charge / Discharge Current & Rack Temperature",
                      session_pairs, analysis_lines, "tbl_all")

        # Sheet 4：充電紀錄（direction == charge）
        charge_pairs = [
            ("開始SOC(%)", cs["start_soc"], False),
            ("結束SOC(%)", cs["end_soc"], False),
            ("SOC增加(%)", cs["soc_change"], True),
            ("充電時間", _fmt_hms(dir_sec["charge"]), False),
            ("充電量(kWh)", stats.get("charged_energy_kwh"), True),
            ("平均Power(kW)", cs["avg_power"], False),
            ("最大Power(kW)", cs["max_power"], False),
            ("平均Current(A)", cs["avg_current"], False),
            ("平均Voltage(V)", cs["avg_voltage"], False),
            ("筆數", cs["count"], False),
        ]
        _record_sheet("充電紀錄", charge_rows, "Charge Summary", "Charge Current & Rack Temperature",
                      charge_pairs, [], "tbl_charge")

        # Sheet 5：放電紀錄（direction == discharge）
        discharge_pairs = [
            ("開始SOC(%)", ds["start_soc"], False),
            ("結束SOC(%)", ds["end_soc"], False),
            ("SOC下降(%)", (round(-ds["soc_change"], 2) if ds["soc_change"] is not None else None), True),
            ("放電時間", _fmt_hms(dir_sec["discharge"]), False),
            ("放電量(kWh)", stats.get("discharged_energy_kwh"), True),
            ("平均Power(kW)", ds["avg_power"], False),
            ("最大Power(kW)", ds["max_power"], False),
            ("平均Current(A)", ds["avg_current"], False),
            ("平均Voltage(V)", ds["avg_voltage"], False),
            ("筆數", ds["count"], False),
        ]
        _record_sheet("放電紀錄", discharge_rows, "Discharge Summary", "Discharge Current & Rack Temperature",
                      discharge_pairs, [], "tbl_discharge")

        # Summary 頁只保留摘要與統計（不放任何圖表）。

        # ================= Sheet 6：Raw Data（完整欄位，不刪任何欄）=================
        wraw = wb.create_sheet("Raw Data")
        wraw.append(CFG.SAMPLE_FIELDS)
        for s in all_rows:
            wraw.append([s.get(k) for k in CFG.SAMPLE_FIELDS])
        _hdr_row(wraw, 1, len(CFG.SAMPLE_FIELDS))
        wraw.freeze_panes = "A2"
        if all_rows:
            _table(wraw, f"A1:{get_column_letter(len(CFG.SAMPLE_FIELDS))}{len(all_rows) + 1}", "tbl_raw")
        _autofit(wraw, len(CFG.SAMPLE_FIELDS))

        # ================= Sheet 7：Alarm（維持既有：alarm_id_raw 文字、凍結、篩選）=================
        wa = wb.create_sheet("Alarm")
        wa.append(CFG.ALARM_FIELDS)
        alarm_rows = self.alarm_tracker.export_rows()
        for a in alarm_rows:
            wa.append([a.get(k) for k in CFG.ALARM_FIELDS])
        _hdr_row(wa, 1, len(CFG.ALARM_FIELDS))
        idraw_col = CFG.ALARM_FIELDS.index("alarm_id_raw") + 1
        for row in range(2, wa.max_row + 1):
            c = wa.cell(row=row, column=idraw_col)
            if c.value is not None:
                c.value = str(c.value)          # 文字，避免 >15 位精度遺失 / 科學記號
            c.number_format = "@"
        wa.freeze_panes = "A2"
        wa.auto_filter.ref = wa.dimensions
        _autofit(wa, len(CFG.ALARM_FIELDS))

        # ---- 隱藏工作表：報表版本識別（auto 流程與 --regen 共用 module_version_info）----
        vinfo = module_version_info()
        wsm = wb.create_sheet("_ReportMeta")
        wsm["A1"] = "報表版本識別 (Report Version Identity)"
        for i, (k, val) in enumerate([
            ("REPORT_VERSION", vinfo["REPORT_VERSION"]),
            ("schema_version", vinfo["schema_version"]),
            ("report_version", vinfo["report_version"]),
            ("Source (__file__)", vinfo["source"]),
            ("Build Time (module load)", vinfo["build_time"]),
            ("Source MTime", vinfo["source_mtime"]),
            ("Git Commit", vinfo["git_commit"]),
            ("Generated At", _now_str()),
        ], start=3):
            wsm.cell(row=i, column=1, value=k)
            wsm.cell(row=i, column=2, value=str(val))
        wsm.column_dimensions["A"].width = 26
        wsm.column_dimensions["B"].width = 60
        wsm.sheet_state = "hidden"
        print(f"  [Report Module] {vinfo['REPORT_VERSION']} src={vinfo['source']} "
              f"load={vinfo['build_time']} mtime={vinfo['source_mtime']} git={vinfo['git_commit']}")

        # ---- 版面：A4 橫式、fit-to-width、分頁標籤顏色（tabColor 取自 config）----
        for wsx in wb.worksheets:
            wsx.page_setup.orientation = "landscape"
            wsx.page_setup.paperSize = wsx.PAPERSIZE_A4
            wsx.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
            wsx.page_setup.fitToWidth = 1
            wsx.page_setup.fitToHeight = 0
            if wsx.title in CFG.TAB_COLORS:
                wsx.sheet_properties.tabColor = CFG.TAB_COLORS[wsx.title]

        try:
            wb.save(self._path(CFG.FILE_XLSX))
        except OSError as e:
            print(f"  [警告] 寫入 {CFG.FILE_XLSX} 失敗：{e}")
            print(f"         → 檔案可能正被 Excel 開啟而鎖定，請關閉 {CFG.FILE_XLSX} 後重試。")
            return False
        # 讓隱藏欄(AN 稀疏標籤)的類別標籤仍繪出 → 圖表 plotVisOnly 設為 0（openpyxl 預設寫 1，需存檔後改）
        try:
            _force_plot_visible_all(self._path(CFG.FILE_XLSX))
        except Exception as e:
            print(f"  [提醒] 設定 plotVisOnly=0 失敗（隱藏欄 X 軸標籤可能不顯示）：{e}")
        return True


def _force_plot_visible_all(xlsx_path):
    """
    將 xlsx 內所有圖表的 plotVisOnly 由 1 改為 0（openpyxl 預設寫 1）。
    plotVisOnly=0 即 Excel「顯示隱藏列與欄中的資料」→ 隱藏的 X 軸稀疏標籤欄類別仍會顯示。
    以 zip 逐檔複製、僅改寫 chart XML，其餘原封不動。
    """
    import zipfile
    import re as _re
    tmp = xlsx_path + ".tmp"
    pat = _re.compile(r'(<(?:c:)?plotVisOnly val=")1(")')
    with zipfile.ZipFile(xlsx_path, "r") as zin, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if _re.match(r"xl/charts/chart\d+\.xml$", item.filename):
                data = pat.sub(r"\g<1>0\g<2>", data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
    os.replace(tmp, xlsx_path)


def _log_session_create(sess):
    """
    建立新 Session 資料夾時印出來源（呼叫者/模組/thread/pid），
    便於日後直接看出是哪個入口（run_all / device_control_menu / 背景 thread / 其他）建立資料夾。
    """
    import inspect
    import threading
    caller = "?"
    try:
        me = os.path.basename(__file__)
        for fr in inspect.stack()[1:]:
            if os.path.basename(fr.filename) != me:
                caller = f"{os.path.basename(fr.filename)}:{fr.lineno} {fr.function}()"
                break
        else:
            top = inspect.stack()[-1]
            caller = f"{os.path.basename(top.filename)}:{top.lineno} {top.function}()"
    except Exception:
        pass
    print("[REPORT SESSION CREATE]")
    print(f"  session_id: {sess.session_id}")
    print(f"  folder    : {sess.folder}")
    print(f"  caller    : {caller}")
    print(f"  module    : {os.path.abspath(__file__)}  (build {REPORT_BUILD})")
    print(f"  thread    : {threading.current_thread().name}   pid: {os.getpid()}")
    print(f"  action    : {sess.action}")
    print(f"  timestamp : {_now_str()}")


# ======================================================================
# 執行模式
# ======================================================================
def _arm_for_charge_discharge(client, interval, duration):
    """
    Arming 監測（**不建立任何 Session/資料夾**）：等待設備真正進入充/放電。
    需連續 START_CONFIRM_SAMPLES 次同時滿足：
      - 方向為 charge / discharge（PCS 旗標優先，退回功率符號）
      - |battery_current_a| >= START_CURRENT_THRESHOLD_A（排除 0A / 小幅漂移 / API 缺值）
    回傳 True=已確認進入充/放電（可建立 Session）；False=idle/逾時/中斷（不建立）。
    離開條件：Ctrl+C、達 duration（>0）、或達 SESSION_TIMEOUT_SEC。
    """
    thr = CFG.START_CURRENT_THRESHOLD_A
    need = CFG.START_CONFIRM_SAMPLES
    streak = 0
    t0 = time.monotonic()
    deadline = t0 + duration if duration else None
    print(f"[ARM] 監測中，等待實際充/放電（門檻 |I|≥{thr}A、連續 {need} 次；idle/0A/待機不建立 Session）…")
    try:
        while True:
            r = read_all(client)
            power = (r.get("actual_active_power_kw") if CFG.POWER_SOURCE == "pcs"
                     else r.get("calculated_power_kw"))
            direction, _src = resolve_direction(
                r.get("pcs_charging_flag"), r.get("pcs_discharging_flag"), power)
            cur = r.get("battery_current_a")
            ok = (direction in ("charge", "discharge")
                  and cur is not None and abs(cur) >= thr)
            streak = streak + 1 if ok else 0
            print(f"  [ARM {time.monotonic() - t0:6.1f}s] 方向={direction} I={cur}A P={power}kW "
                  f"確認={streak}/{need}{'  ✓進入充/放電' if streak >= need else ''}")
            if streak >= need:
                return True
            now = time.monotonic()
            if (deadline and now >= deadline) or (now - t0 >= CFG.SESSION_TIMEOUT_SEC):
                return False
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  [ARM] 使用者中斷（Ctrl+C）→ 未建立 Session。")
        return False


def run_live(action, power, mode_label, duration, interval, ignore_stop=False):
    client = ApiClient()
    token = client.login_hmi(USERNAME)
    if not token:
        print("[錯誤] 登入失敗，無法取樣（getRunMode/getScheduleSwitch 需登入）。")
        print("       仍可嘗試 guest 來源，但 PCS 模式欄位將為未知。")

    output_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "output", CFG.OUTPUT_SUBDIR)

    # === 狀態機（所有模式共用）：Idle → Arming → Recording → Stopping ===
    # Arming：先唯讀監測、確認真正進入充/放電，才建立 Session（idle/0A/待機 → 不建任何資料夾）。
    # --ignore-stop 不影響 Arming（仍需確認充/放電才建立）；它只在 Recording 階段決定是否
    # 忽略 stop_recommended 自動結束（跑滿指定時長）。
    armed = _arm_for_charge_discharge(client, interval, duration)
    if not armed:
        print("[ARM] 監測結束：未偵測到實際充/放電（idle/0A）→ 未建立任何 Session 或資料夾。")
        return
    if ignore_stop:
        print("（--ignore-stop：Recording 階段不因 stop_recommended 提早結束，跑滿指定時長；仍只做唯讀取樣）")

    sess = ReportSession(action, power, mode_label, client, output_root)
    print(f"\n【充放電報告】Session：{sess.session_id}")
    print(f"資料夾：{sess.folder}")
    sess.start()

    end_reason = "completed"
    try:
        deadline = time.monotonic() + duration if duration else None
        while True:
            r, critical = sess.sample_once()
            print(f"  [{sess._elapsed():6.1f}s] 方向={r.get('_direction')} "
                  f"SOC={r.get('soc_percent')}% P={r.get('actual_active_power_kw')}kW "
                  f"累積={sess.energy.cumulative_signed_kwh:.3f}kWh 告警={r.get('alarm_total')} "
                  f"{'⚠stop_recommended' if sess.stop_recommended else ''}")
            # 驗證模式（ignore_stop）：不因 stop_recommended/idle 提早結束，只受時長/timeout 限制
            if not ignore_stop:
                end, reason = sess.should_auto_end()
                if end:
                    end_reason = reason
                    print(f"  自動結束：{reason}（{CFG.END_REASON.get(reason,'')}）")
                    break
            if sess._elapsed() >= CFG.SESSION_TIMEOUT_SEC:
                end_reason = "timeout"
                print("  已達 SESSION_TIMEOUT，結束。")
                break
            if deadline and time.monotonic() >= deadline:
                end_reason = "completed"
                print("  已達指定監測時長，結束。")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        end_reason = "manual_stop"
        print("\n  使用者中斷（Ctrl+C）→ manual_stop")

    stats = sess.finalize(end_reason)
    _print_final(sess, stats)


def _read_csv_dicts(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ======================================================================
# 報告目錄刪除保護（防止誤刪 output/charge_discharge_reports 歷史報告）
# ======================================================================
# 預設禁止刪除正式報告目錄；一般流程不應開啟。需刪除時必須明確、手動設為 True。
ALLOW_REPORT_DELETE = False

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUTPUT_ROOT = os.path.join(_PROJECT_ROOT, "output")
_REPORT_ROOT = os.path.join(_OUTPUT_ROOT, CFG.OUTPUT_SUBDIR)     # output/charge_discharge_reports（正式，受保護）
_TEST_OUTPUT_ROOT = os.path.join(_OUTPUT_ROOT, "test_output")    # selftest 專用，永不碰正式報告


def _within(child, parent):
    """child 是否等於 parent 或位於 parent 底下（皆取絕對路徑比較）。"""
    child = os.path.abspath(child)
    parent = os.path.abspath(parent)
    return child == parent or child.startswith(parent + os.sep)


def _safe_rmtree(path):
    """
    受保護的資料夾刪除（全專案唯一允許的刪除入口）：
      (4) 目標為「正式報告根目錄」本身或其上層（output/、專案根）→ 立即中止並拋 RuntimeError。
      (3) 目標位於正式報告目錄底下（session 子資料夾）→ 預設禁止（除非 ALLOW_REPORT_DELETE=True）。
          → 正式模式（ALLOW_REPORT_DELETE=False）任何程式都無法刪除 output/charge_discharge_reports。
      其餘（例如 output/test_output 底下）→ 允許刪除。
    """
    import shutil
    ap = os.path.abspath(path)
    # (4) 報告根目錄本身，或其上層（刪上層會連帶刪到報告）→ 一律中止
    if ap == _REPORT_ROOT or _within(_REPORT_ROOT, ap):
        raise RuntimeError(f"[報告保護] 拒絕刪除正式報告根目錄或其上層，已中止：{ap}")
    # (3) 正式報告目錄底下的任何內容 → 預設禁止
    if _within(ap, _REPORT_ROOT) and not ALLOW_REPORT_DELETE:
        raise RuntimeError(f"[報告保護] 正式報告目錄禁止刪除（ALLOW_REPORT_DELETE=False）：{ap}")
    shutil.rmtree(ap, ignore_errors=True)


def selftest():
    """
    離線自我測試（不連設備）：驗證 Session append/resume 累積機制。
    三階段：① 新 Session 5 筆充電 → 暫停；② resume 再追加 5 筆充電 → 暫停；
            ③ resume 再追加 5 筆放電 → 完成。全程注入假時鐘與合成 reading，不觸網。
    """
    print("== charge_discharge_report 離線自我測試：Session append / resume 累積 ==")
    # selftest 一律寫入獨立 output/test_output，絕不碰正式報告目錄 output/charge_discharge_reports
    output_root = _TEST_OUTPUT_ROOT
    os.makedirs(output_root, exist_ok=True)
    base = datetime(2026, 7, 17, 9, 0, 0)
    # selftest 用固定示範時間 → 每次都清掉「test_output 內」舊示範資料夾（含歷史 _2/_3… 殘留），
    # 讓示範 Session 永遠重用同一個乾淨資料夾。（正式報告目錄不受影響、且被 _safe_rmtree 保護禁止刪除）
    import glob as _glob
    _demo_base = base.strftime(CFG.SESSION_FOLDER_TIME_FMT) + "_auto"
    for _p in ([os.path.join(output_root, _demo_base)]
               + _glob.glob(os.path.join(output_root, _demo_base + "_*"))):
        if os.path.isdir(_p):
            _safe_rmtree(_p)   # 經保護：允許 test_output，禁止刪正式報告目錄
    clk = {"dt": base}
    now_fn = lambda: clk["dt"]                       # noqa: E731
    cur = {"r": None}
    ALARM_ID = 211845073895164933                    # 18 位，驗證跨 resume 去重

    def reading(power, soc, direction, alarms=None):
        v = 890.0
        i = round(power * 1000.0 / v, 2) if power else 0.0
        return {
            "communication_ok": True, "raw_source_time": "",
            "pcs_charging_flag": direction == "charge",
            "pcs_discharging_flag": direction == "discharge",
            "soc_percent": float(soc), "battery_voltage_v": v, "battery_current_a": i,
            "rack_max_temperature_c": 30.0 + abs(power) * 0.3,   # 合成 Rack 溫度（驗證溫度序列）
            "rack_min_temperature_c": 25.0 + abs(power) * 0.2,
            "battery_status": "充電" if power > 0 else ("放電" if power < 0 else "待機"),
            "actual_active_power_kw": float(power), "actual_reactive_power_kvar": 0.0,
            "calculated_power_kw": round(v * i / 1000, 3),
            "pcs_status": "執行", "pcs_control_mode": "智慧模式", "pcs_work_mode": "併網",
            "pcs_power_control_mode": "交流有功", "pcs_manual_switch": 0, "pcs_schedule_enabled": True,
            "battery_power_status": "已上電",
            "alarm_rows": alarms or [], "alarm_total": len(alarms or []),
        }

    def alarm_a(active=True):
        return {"id": ALARM_ID, "targetMark": "type.attr.dcInputFault",
                "val": "type.attr.dcInputUnderVoltage", "level": 1,
                "alarmStatus": active, "alarmTime": 1784183072748}

    def run(sess, n, power, soc, direction, alarms=None, step=5):
        for _ in range(n):
            clk["dt"] += timedelta(seconds=step)     # 先推進時鐘 → elapsed 持續增加
            cur["r"] = reading(power, soc, direction, alarms)
            sess.sample_once()

    results = []

    def check(label, ok):
        results.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    global read_all
    orig = read_all
    read_all = lambda _c: cur["r"]                   # noqa: E731
    folder = None
    try:
        # ---- 階段①：新 Session，5 筆充電（告警 A 於取樣期間出現＝new）→ 暫停 ----
        clk["dt"] = base
        cur["r"] = reading(5.0, 60, "charge", alarms=[])          # start baseline：無告警
        sess = ReportSession("auto", 10.0, "交流有功", object(), output_root, now_fn=now_fn)
        sess.start()
        run(sess, 5, 5.0, 60, "charge", alarms=[alarm_a(True)])
        folder = sess.folder
        rows5 = _read_csv_dicts(os.path.join(folder, CFG.FILE_SAMPLES))
        cum_charge_5 = float(rows5[-1]["cumulative_charge_energy_kwh"])
        sess.pause()

        clk["dt"] += timedelta(minutes=10)                        # 暫停空檔（不應被積分）

        # ---- 階段②：resume，再追加 5 筆充電（告警 A 仍在＝應去重）→ 暫停 ----
        cur["r"] = reading(5.0, 62, "charge", alarms=[alarm_a(True)])
        sess2 = ReportSession.resume(folder, object(), now_fn=now_fn)
        sess2.start()
        run(sess2, 5, 5.0, 62, "charge", alarms=[alarm_a(True)])
        sess2.pause()

        rows10 = _read_csv_dicts(os.path.join(folder, CFG.FILE_SAMPLES))
        ev10 = _read_csv_dicts(os.path.join(folder, CFG.FILE_EVENTS))
        print("\n[驗證：resume 追加後]")
        check("samples.csv 共 10 筆", len(rows10) == 10)
        check("sample_index 連續 1..10",
              [int(r["sample_index"]) for r in rows10] == list(range(1, 11)))
        el = [float(r["elapsed_seconds"]) for r in rows10]
        check("elapsed_seconds 持續遞增（含暫停空檔）", all(el[j + 1] > el[j] for j in range(9)))
        cum_charge_10 = float(rows10[-1]["cumulative_charge_energy_kwh"])
        check(f"cumulative_charge 第10筆({cum_charge_10}) > 第5筆({cum_charge_5})",
              cum_charge_10 > cum_charge_5)
        check("前 5 筆內容未被覆蓋", rows10[:5] == rows5)
        check("events session_start 恰 1 次",
              sum(1 for e in ev10 if e["event_type"] == "session_start") == 1)
        check("events recording_resume 恰 1 次",
              sum(1 for e in ev10 if e["event_type"] == "recording_resume") == 1)

        # ---- 階段③：resume，再追加 5 筆放電（告警 A 恢復）→ finalize ----
        clk["dt"] += timedelta(seconds=5)
        cur["r"] = reading(-5.0, 61, "discharge", alarms=[alarm_a(False)])
        sess3 = ReportSession.resume(folder, object(), now_fn=now_fn)
        sess3.start()
        run(sess3, 5, -5.0, 61, "discharge", alarms=[alarm_a(False)])
        stats = sess3.finalize("completed")

        rows15 = _read_csv_dicts(os.path.join(folder, CFG.FILE_SAMPLES))
        summ = _load_json_file(os.path.join(folder, CFG.FILE_SUMMARY)) or {}
        print("\n[驗證：追加放電後完整報告]")
        dirs = set(r["charge_discharge_direction"] for r in rows15)
        check("同一 samples.csv 同時含 charge 與 discharge", {"charge", "discharge"} <= dirs)
        check(f"charged 保留（{stats['charged_energy_kwh']}>0）", stats["charged_energy_kwh"] > 0)
        check(f"discharged 開始累積（{stats['discharged_energy_kwh']}>0）", stats["discharged_energy_kwh"] > 0)
        check(f"RTE 依完整循環計算（非 N/A）：{stats['round_trip_efficiency_percent']}",
              isinstance(stats["round_trip_efficiency_percent"], (int, float)))
        check("alarm 去重跨 resume：tracked=1", stats["alarm_total_tracked"] == 1)
        check("summary/statistics 反映全部 15 筆",
              stats["sample_count"] == 15 and summ.get("statistics", {}).get("sample_count") == 15)
        st = _load_json_file(os.path.join(folder, CFG.FILE_SESSION_STATE)) or {}
        check("session_state.status=completed", st.get("status") == CFG.SESSION_COMPLETED)

        # ---- report.xlsx 分頁標籤顏色驗證（tabColor 需符合 CFG.TAB_COLORS）----
        print("\n[驗證：Excel 分頁標籤顏色]")
        try:
            from openpyxl import load_workbook
            wbx = load_workbook(os.path.join(folder, CFG.FILE_XLSX))
            for sname, hexcol in CFG.TAB_COLORS.items():
                tc = wbx[sname].sheet_properties.tabColor if sname in wbx.sheetnames else None
                rgb = getattr(tc, "rgb", None)
                got = rgb.upper() if isinstance(rgb, str) else None
                check(f"Tab色 {sname} = #{hexcol}（實際 {got}）",
                      got is not None and got.endswith(hexcol.upper()))
        except Exception as e:
            check(f"分頁顏色驗證發生例外：{e}", False)

        # ---- Summary 已無 Health（無星號/評分/標題）----
        print("\n[驗證：Summary 移除 Health]")
        try:
            wsh = load_workbook(os.path.join(folder, CFG.FILE_XLSX))["Summary"]
            texts = [str(c.value) for row in wsh.iter_rows() for c in row if c.value is not None]
            check("Summary 無 Health 標題", not any("Health" in t for t in texts))
            check("Summary 無星號評分(★/☆)", not any(("★" in t or "☆" in t) for t in texts))
        except Exception as e:
            check(f"Summary Health 驗證發生例外：{e}", False)

        # ---- report.xlsx 圖表結構驗證（整合圖：每紀錄頁 1 張、3 series＝電流+Rack溫度、無 timestamp）----
        print("\n[驗證：Excel 圖表結構]")
        try:
            import zipfile, re as _re
            wbx2 = load_workbook(os.path.join(folder, CFG.FILE_XLSX))
            per_sheet = {n: len(wbx2[n]._charts) for n in wbx2.sheetnames}
            check(f"Summary 圖表=0（{per_sheet.get('Summary')}）", per_sheet.get("Summary") == 0)
            check(f"KPI 圖表=0（{per_sheet.get('KPI')}）", per_sheet.get("KPI") == 0)
            for sname in ("充放電紀錄", "充電紀錄", "放電紀錄"):
                check(f"{sname} 圖表=1（{per_sheet.get(sname)}）", per_sheet.get(sname) == 1)
                # 輔助標籤欄移到最右側 AN 且隱藏（不顯示給使用者），Summary 旁不再有輔助標籤
                an1 = wbx2[sname]["AN1"].value
                s1 = wbx2[sname]["S1"].value
                an_hidden = getattr(wbx2[sname].column_dimensions.get("AN"), "hidden", None)
                _exp_hdr = f"X軸標籤(每{CFG.X_AXIS_LABEL_INTERVAL_SEC}秒±3s)"
                check(f"{sname}: X 稀疏標籤欄在 AN、隱藏、標題依 config 間隔（AN1={an1!r} hidden={an_hidden}）",
                      an1 == _exp_hdr and an_hidden is True)
                check(f"{sname}: Summary 區旁(S1)無輔助欄（{s1!r}）",
                      not (s1 and ("X軸標籤" in str(s1) or "X時間序值" in str(s1))))
                # 第一筆(AN2)與最後一筆一定有 Label（強制首尾）；資料末列以 A 欄(時間)最後非空列為準
                wsr = wbx2[sname]
                a_rows = [c.row for c in wsr["A"] if c.value is not None and c.row >= 2]
                lastrow = max(a_rows) if a_rows else 2
                an_first = wsr["AN2"].value
                an_last = wsr.cell(row=lastrow, column=40).value
                check(f"{sname}: X 軸第一筆有 Label（AN2={an_first!r}）", bool(an_first))
                check(f"{sname}: X 軸最後一筆有 Label（AN{lastrow}={an_last!r}）", bool(an_last))
            check("Raw Data 圖表=0", per_sheet.get("Raw Data") == 0)
            check("Alarm 圖表=0", per_sheet.get("Alarm") == 0)
            total = sum(per_sheet.values())
            check(f"整份 report.xlsx 總圖表數=3（{total}）", total == 3)
            # 逐張 chart XML：3 series（電流 H + Rack 最大/最小溫度 M/N）、legend=b、雙軸、時間為分類
            z = zipfile.ZipFile(os.path.join(folder, CFG.FILE_XLSX))
            cxmls = sorted(n for n in z.namelist() if _re.match(r"xl/charts/chart\d+\.xml", n))
            check(f"chart XML 檔數=3（{len(cxmls)}）", len(cxmls) == 3)
            series_cols = {"H", "M", "N"}   # Current(A)=H、Rack最大溫度=M、Rack最小溫度=N
            for cx in cxmls:
                base = os.path.basename(cx)
                xml = z.read(cx).decode("utf-8")
                nser = len(_re.findall(r"</(?:c:)?ser>", xml))                        # 系列數
                title_refs = _re.findall(r"<(?:c:)?tx>.*?<(?:c:)?f>([^<]*)</(?:c:)?f>", xml, _re.S)
                tcols = set(_re.findall(r"!\$?([A-Z]+)\$?1\b", " ".join(title_refs)))
                ts_in_title = any(_re.search(r"\d{2}:\d{2}:\d{2}", t or "") for t in title_refs)
                # LineChart 類別軸：cat 以 strRef 參照隱藏 AN 稀疏標籤欄
                cat_refs = _re.findall(r'<(?:c:)?cat>.*?<(?:c:)?f>([^<]*)</(?:c:)?f>', xml, _re.S)
                is_line = bool(_re.search(r'<(?:c:)?lineChart>', xml)) and not _re.search(r'<(?:c:)?scatterChart>', xml)
                rotated = 'rot="-5400000"' in xml                     # X 軸標籤 -90° 垂直
                m = _re.search(r'<(?:c:)?legendPos val="(\w+)"', xml)
                legend = m.group(1) if m else None
                valsegs = _re.findall(r'<(?:c:)?valAx>.*?</(?:c:)?valAx>', xml, _re.S)
                catsegs = _re.findall(r'<(?:c:)?catAx>.*?</(?:c:)?catAx>', xml, _re.S)
                catseg = catsegs[0] if catsegs else None
                yax = valsegs[0] if valsegs else None                 # 單一共用 Y 軸
                _has_title = lambda seg: bool(seg) and bool(_re.search(r'<(?:c:)?title>', seg))
                axis_has_title = any(_has_title(v) for v in valsegs) or (bool(catseg) and _has_title(catseg))
                has_manual_layout = bool(_re.search(r'<(?:c:)?manualLayout>', xml))
                skips = _re.findall(r'<(?:c:)?tickLblSkip val="(\d+)"', xml)
                catpos = (_re.search(r'<(?:c:)?tickLblPos val="(\w+)"', catseg).group(1)
                          if catseg and _re.search(r'<(?:c:)?tickLblPos', catseg) else None)
                y_mu = _re.search(r'<(?:c:)?majorUnit val="([\d.]+)"', yax) if yax else None
                major_unit = float(y_mu.group(1)) if y_mu else None
                yminv = float(_re.search(r'<(?:c:)?min val="([-\d.]+)"', yax).group(1)) if yax and _re.search(r'<(?:c:)?min val=', yax) else None
                ymaxv = float(_re.search(r'<(?:c:)?max val="([-\d.]+)"', yax).group(1)) if yax and _re.search(r'<(?:c:)?max val=', yax) else None
                _ysz = _re.search(r'sz="(\d+)"', yax) if yax else None
                grid_color = None
                if yax:
                    mg = _re.search(r'<(?:c:)?majorGridlines>(.*?)</(?:c:)?majorGridlines>', yax, _re.S)
                    if mg:
                        cm = _re.search(r'srgbClr val="([0-9A-Fa-f]{6})"', mg.group(1))
                        grid_color = cm.group(1).upper() if cm else None
                cur_literal = bool(_re.search(r'<(?:c:)?tx>\s*<(?:c:)?v>電流</(?:c:)?v>', xml))
                check(f"{base}: LineChart（類別軸）", is_line)
                check(f"{base}: 3 series（{nser}）", nser == 3)
                check(f"{base}: 電流圖例名稱=「電流」（文字標題）", cur_literal)
                check(f"{base}: 溫度序列標題參照 M/N（{sorted(tcols)}）", tcols == {"M", "N"})
                check(f"{base}: series 標題不含 timestamp/SOC/Voltage/Power", not ts_in_title)
                check(f"{base}: X 類別軸參照隱藏 AN 稀疏標籤欄（{set(cat_refs)}）",
                      bool(cat_refs) and all("$AN$" in c for c in cat_refs))
                check(f"{base}: plotVisOnly=0（隱藏欄類別仍繪出）",
                      bool(_re.search(r'<(?:c:)?plotVisOnly val="0"', xml)))
                check(f"{base}: 單一共用 Y 軸 + 一條類別軸（valAx={len(valsegs)} catAx={len(catsegs)}）",
                      len(valsegs) == 1 and len(catsegs) == 1)
                check(f"{base}: X 每格顯示 tickLblSkip=1（{skips}）",
                      bool(skips) and all(s == "1" for s in skips))
                check(f"{base}: X 時間 Label 固定底部 tickLblPos=low（{catpos}）", catpos == "low")
                check(f"{base}: 關閉 X 左右 Margin crossBetween=midCat",
                      bool(_re.search(r'<(?:c:)?crossBetween val="midCat"', xml)))
                check(f"{base}: Y 軸 Major Unit 固定=5（{major_unit}）", major_unit == 5)
                check(f"{base}: Y 軸刻度字級 9pt（{(int(_ysz.group(1))/100) if _ysz else None}）",
                      bool(_ysz) and _ysz.group(1) == "900")
                check(f"{base}: Y 軸上下限為 5 的倍數且含 0（min={yminv} max={ymaxv}）",
                      yminv is not None and ymaxv is not None
                      and yminv % 5 == 0 and ymaxv % 5 == 0 and yminv <= 0 <= ymaxv)
                _legseg = _re.search(r'<(?:c:)?legend>.*?</(?:c:)?legend>', xml, _re.S)
                _leg_manual = bool(_legseg) and bool(_re.search(r'<(?:c:)?manualLayout>', _legseg.group(0)))
                _leg_overlay0 = bool(_legseg) and bool(_re.search(r'<(?:c:)?overlay val="0"', _legseg.group(0)))
                _leg_y = _re.search(r'<(?:c:)?y val="([\d.]+)"', _legseg.group(0)) if _legseg else None
                _leg_yv = float(_leg_y.group(1)) if _leg_y else None
                _leg_h = _re.search(r'<(?:c:)?h val="([\d.]+)"', _legseg.group(0)) if _legseg else None
                _leg_hv = float(_leg_h.group(1)) if _leg_h else None
                _leg_sz = _re.search(r'sz="(\d+)"', _legseg.group(0)) if _legseg else None
                _titleseg = _re.search(r'<(?:c:)?title>.*?</(?:c:)?title>', xml, _re.S)
                _title_sz = _re.search(r'sz="(\d+)"', _titleseg.group(0)) if _titleseg else None
                _title_bold = bool(_titleseg) and bool(_re.search(r'\bb="1"', _titleseg.group(0)))
                check(f"{base}: 底部 Legend（{legend}）", legend == "b")
                check(f"{base}: Legend 精簡貼底（overlay=0、y={_leg_yv} 貼底、h={_leg_hv} 精簡）",
                      _leg_manual and _leg_overlay0 and _leg_yv is not None and _leg_yv >= 0.90
                      and _leg_hv is not None and _leg_hv <= 0.05)
                check(f"{base}: 標題字級 12pt 粗體（sz={_title_sz.group(1) if _title_sz else None} b={_title_bold}）",
                      bool(_title_sz) and _title_sz.group(1) == "1200" and _title_bold)
                check(f"{base}: Legend 字級 10pt（sz={_leg_sz.group(1) if _leg_sz else None}）",
                      bool(_leg_sz) and _leg_sz.group(1) == "1000")
                check(f"{base}: 水平格線淡灰 D9D9D9（{grid_color}）", grid_color == "D9D9D9")
                check(f"{base}: 有水平格線（Y valAx）、無垂直格線（X catAx）",
                      bool(yax) and bool(_re.search(r'<(?:c:)?majorGridlines', yax))
                      and not (catseg and _re.search(r'<(?:c:)?majorGridlines', catseg)))
                check(f"{base}: Plot Area 手動填滿(manualLayout)", has_manual_layout)
                check(f"{base}: Plot Area 用內部矩形(layoutTarget=inner)",
                      bool(_re.search(r'<(?:c:)?layoutTarget val="inner"', xml)))
                check(f"{base}: 無 X/Y 軸標題（僅保留圖表標題）", not axis_has_title)
                check(f"{base}: X 軸標籤垂直(-90°)", rotated)
            # 固定尺寸 32×22 cm（EMU 11520000×7920000），不隨資料筆數放大
            draws = [nm for nm in z.namelist() if _re.match(r'xl/drawings/drawing\d+\.xml', nm)]
            exts = []
            for d in draws:
                exts += _re.findall(r'ext cx="(\d+)" cy="(\d+)"', z.read(d).decode("utf-8"))
            h_emu = int(round(CFG.CHART_HEIGHT_CM * 360000))
            check(f"圖表高度固定 {CFG.CHART_HEIGHT_CM}cm（cy={set(cy for _cx, cy in exts)}）",
                  bool(exts) and all(int(cy) == h_emu for _cx, cy in exts))
            if CFG.CHART_ADAPTIVE_SIZE:
                min_emu = int(round(CFG.CHART_MIN_WIDTH_CM * 360000))
                max_emu = int(round(CFG.CHART_MAX_WIDTH_CM * 360000))
                check(f"圖表寬度自適應且在 [{CFG.CHART_MIN_WIDTH_CM},{CFG.CHART_MAX_WIDTH_CM}]cm 內"
                      f"（cx={set(cx for cx, _cy in exts)}）",
                      bool(exts) and all(min_emu <= int(cx) <= max_emu for cx, _cy in exts))
            else:
                w_emu = int(round(CFG.CHART_WIDTH_CM * 360000))
                check(f"圖表固定寬度 {CFG.CHART_WIDTH_CM}cm、三頁同尺寸（cx={set(cx for cx, _cy in exts)}）",
                      bool(exts) and all(int(cx) == w_emu for cx, _cy in exts))
        except Exception as e:
            check(f"圖表結構驗證發生例外：{e}", False)

        # ---- Y 軸固定 Major Unit=5：大範圍也不改間距，上下限取整到 5 的倍數、含 0、不裁切 ----
        print("\n[驗證：Y 軸固定每 5 一格]")
        try:
            def _ck_scale(vals, exp_lo, exp_hi):
                lo, hi, mu = _y_axis_scale(vals)
                check(f"_y_axis_scale({min(vals)}~{max(vals)}) → mu={mu} lo={lo} hi={hi}"
                      f"（期望 mu=5 lo={exp_lo} hi={exp_hi}）",
                      mu == 5 and lo == exp_lo and hi == exp_hi and lo <= 0 <= hi)
            _ck_scale([-185.0, 160.0], -185, 160)     # 大電流：仍每 5、不跳成 25（-185~160）
            _ck_scale([0.0, 22.0], 0, 25)             # 正值：0~25
            _ck_scale([-60.0, 55.0], -60, 55)         # -60~55
            _ck_scale([-183.0, 158.0], -185, 160)     # 需求範例：158→160、-183→-185、mu=5
        except Exception as e:
            check(f"Y 軸固定刻度驗證發生例外：{e}", False)

        # ---- 圖表尺寸：固定或自適應（依 config 旗標）----
        print("\n[驗證：圖表尺寸模式]")
        try:
            if CFG.CHART_ADAPTIVE_SIZE:
                b, per = CFG.CHART_BASE_WIDTH_CM, CFG.CHART_WIDTH_PER_LABEL_CM
                lo_w, hi_w = CFG.CHART_MIN_WIDTH_CM, CFG.CHART_MAX_WIDTH_CM
                def _ckw(count, exp):
                    got = _chart_width_cm(count)
                    check(f"_chart_width_cm({count})={got}（期望 {exp}）", abs(got - exp) < 1e-6)
                _ckw(9, max(lo_w, min(hi_w, b + 9 * per)))
                _ckw(25, max(lo_w, min(hi_w, b + 25 * per)))
                _ckw(100000, hi_w)
                _ckw(0, lo_w)
                check("Label 越多圖越寬（單調遞增）",
                      _chart_width_cm(30) > _chart_width_cm(10) >= lo_w)
            else:
                check(f"固定尺寸：寬度與 Label 數無關（皆={CFG.CHART_WIDTH_CM}cm）",
                      _chart_width_cm(0) == CFG.CHART_WIDTH_CM
                      and _chart_width_cm(9) == CFG.CHART_WIDTH_CM
                      and _chart_width_cm(500) == CFG.CHART_WIDTH_CM)
        except Exception as e:
            check(f"圖表尺寸模式驗證發生例外：{e}", False)

        # ---- X 軸 Label 間隔由 config 控制（改 CFG 值 → 標籤依新間隔）----
        print("\n[驗證：X 軸 Label 間隔可調（config）]")
        try:
            check(f"config 有 X_AXIS_LABEL_INTERVAL_SEC（={CFG.X_AXIS_LABEL_INTERVAL_SEC}）",
                  isinstance(CFG.X_AXIS_LABEL_INTERVAL_SEC, (int, float))
                  and CFG.X_AXIS_LABEL_INTERVAL_SEC > 0)
        except Exception as e:
            check(f"X 軸 Label 間隔設定驗證發生例外：{e}", False)

        # ---- Arming 門檻：idle/0A 不進入（不建立 Session）；charge 且 |I|≥門檻 才確認 ----
        print("\n[驗證：Arming 啟動門檻]")
        try:
            cur["r"] = reading(0.0, 60, "idle")               # idle、I=0
            armed_idle = _arm_for_charge_discharge(object(), 0.02, 0.08)
            check("Arming：idle/0A → 不進入充放電（不建立 Session）", armed_idle is False)
            cur["r"] = reading(5.0, 60, "charge")             # charge、I≈5.6A ≥ 門檻
            armed_chg = _arm_for_charge_discharge(object(), 0.0, 0.0)
            check("Arming：charge 且 |I|≥門檻 → 確認進入（可建立 Session）", armed_chg is True)
        except Exception as e:
            check(f"Arming 驗證發生例外：{e}", False)
    finally:
        read_all = orig

    ok = all(results)
    print(f"\n== SelfTest {'PASS' if ok else 'FAIL'}（{sum(results)}/{len(results)} 檢查通過）==")
    if folder:
        print("輸出檔：")
        for name in (CFG.FILE_SESSION_STATE, CFG.FILE_SUMMARY, CFG.FILE_STATISTICS,
                     CFG.FILE_SAMPLES, CFG.FILE_ALARMS, CFG.FILE_EVENTS, CFG.FILE_XLSX):
            p = os.path.join(folder, name)
            print(f"  {'✓' if os.path.exists(p) else '✗'} {name}")
        print(f"  資料夾：{folder}")


def _print_final(sess, stats):
    print("\n================ 報告摘要 ================")
    print(f"Session ID      : {sess.session_id}")
    print(f"動作 / 控制方式  : {sess.action} / {sess.control_mode_label}")
    print(f"結束原因        : {sess.end_reason}（{CFG.END_REASON.get(sess.end_reason,'')}）")
    print(f"取樣筆數        : {stats.get('sample_count')}")
    print(f"SOC 變化        : {stats.get('start_soc_percent')} → {stats.get('end_soc_percent')} "
          f"(Δ{stats.get('soc_delta_percent')})")
    print(f"充入 / 放出電量 : {stats.get('charged_energy_kwh')} / {stats.get('discharged_energy_kwh')} kWh")
    print(f"淨電量          : {stats.get('net_energy_kwh')} kWh")
    print(f"往返效率        : {stats.get('round_trip_efficiency_percent')}")
    print(f"平均/最大/最小 P : {stats.get('average_active_power_kw')} / "
          f"{stats.get('max_active_power_kw')} / {stats.get('min_active_power_kw')} kW")
    print(f"告警數          : {stats.get('alarm_count')}")
    print(f"stop_recommended: {sess.stop_recommended} {sess.stop_reasons}")
    print(f"輸出資料夾      : {sess.folder}")


def regenerate_report(folder, backup_name="report_before_chart_fix.xlsx"):
    """
    用『唯一的 _write_xlsx』重產指定 session 資料夾的 report.xlsx（與 finalize/selftest 同一函式）。
    - 只讀該資料夾既有 samples.csv / alarms.csv / summary.json；不連設備、不動原始資料。
    - 重產前將原 report.xlsx 備份為 backup_name（若該備份已存在則不覆蓋）。
    - 不建立 charts/、不產生 report_analysis.xlsx；統計數值沿用 summary.json（不重算、不改變）。
    """
    global _now_str
    folder = os.path.abspath(folder)
    summ = _load_json_file(os.path.join(folder, CFG.FILE_SUMMARY))
    if not summ:
        print(f"[REGEN] 找不到或無法讀取 summary.json：{folder}")
        return False
    stats = summ.get("statistics") or {}
    sess_blk = summ.get("session") or {}

    sess = ReportSession.resume(folder, object())            # 僅 __init__（不 start、不寫檔）
    sess.samples = sess._load_all_samples()
    sess.alarm_tracker.load_existing(sess._load_csv_rows(CFG.FILE_ALARMS))
    sess.start_state = summ.get("start_state") or sess.start_state
    sess.end_state = summ.get("end_state")
    sess.end_reason = sess_blk.get("end_reason") or "completed"

    xlsx = os.path.join(folder, CFG.FILE_XLSX)
    bak = os.path.join(folder, backup_name)
    if os.path.exists(xlsx) and not os.path.exists(bak):
        import shutil
        shutil.copy2(xlsx, bak)
        print(f"[REGEN] 已備份原報表 → {os.path.basename(bak)}")

    orig_now = _now_str
    if sess_blk.get("end_time"):
        _now_str = lambda: sess_blk["end_time"]              # Summary「結束時間」顯示原值
    try:
        ok = sess._write_xlsx(stats)                         # 唯一 Excel 產生函式；回傳是否成功寫入
    finally:
        _now_str = orig_now
    if not ok:
        print(f"[REGEN] ✗ report.xlsx 寫入失敗（未更新）：{xlsx}")
        print("        請先關閉 Excel 中開啟的 report.xlsx，再重新執行 --regen。")
        return False
    print(f"[REGEN] ✓ 已用最新 _write_xlsx 重新產生：{xlsx}")
    print(f"        樣本 {len(sess.samples)} 筆；統計沿用 summary.json（未改變）")
    return True


def main():
    ap = argparse.ArgumentParser(description="PCS 充放電報告（獨立唯讀監測；不送任何控制）")
    ap.add_argument("--action", choices=["charge", "discharge", "auto"], default="auto",
                    help="充電 / 放電 / 自動（依第一筆量測方向判斷）")
    ap.add_argument("--power", type=float, default=None, help="設定功率 kW（僅記錄，不送控制）")
    ap.add_argument("--mode", default="交流有功", help="控制方式標籤（交流有功/直流恆流/直流恆功率）")
    ap.add_argument("--duration", type=int, default=0,
                    help="監測時長秒數；0=直到自動結束/Ctrl+C（上限 config SESSION_TIMEOUT_SEC）")
    ap.add_argument("--interval", type=float, default=CFG.SAMPLE_INTERVAL_SEC, help="取樣間隔秒")
    ap.add_argument("--ignore-stop", action="store_true",
                    help="驗證用：跑滿指定時長，不因 stop_recommended/idle 提早結束（仍只唯讀取樣）")
    ap.add_argument("--selftest", action="store_true", help="離線自我測試（合成資料，不連設備）")
    ap.add_argument("--regen", metavar="FOLDER", default=None,
                    help="以最新 _write_xlsx 重產指定 session 資料夾的 report.xlsx（備份原檔、不動原始資料）")
    ap.add_argument("--backup-name", default="report_before_chart_fix.xlsx",
                    help="--regen 時原 report.xlsx 的備份檔名")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.regen:
        regenerate_report(args.regen, backup_name=args.backup_name)
        return
    run_live(args.action, args.power, args.mode, args.duration, args.interval,
             ignore_stop=args.ignore_stop)


# 模組載入即印出版本橫幅：任何程序（含背景 device_control_menu / run_all）一 import 就顯示，
# 立即辨識載入的是哪一版模組（避免長駐程序用到舊模組卻無人察覺）。
print_module_banner()


if __name__ == "__main__":
    main()
