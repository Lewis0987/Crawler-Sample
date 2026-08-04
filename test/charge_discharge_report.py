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
import battery_data_scraper as BDS   # Cell 快照：重用既有 getPackInformation 抓取與正規化，不複製 API 邏輯
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
EP_RACKCAP = "/hmiGuest/unauthorizedAccess/overview/rackCapacityInformation"      # 設備今日累計充/放電量（與 Dashboard 當日一致）
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


def _soc_pct_str(v):
    """SOC 變化值 → 顯示字串：原值後直接加 '%'（不乘 100、保留原精度）。
    3→'3%'、0→'0%'、3.5→'3.5%'、3.25→'3.25%'；None → None（儲存格空白）。
    僅為顯示格式，不改變任何計算結果。"""
    if v is None:
        return None
    return f"{v:g}%"


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


def pcs_is_fault(r):
    """
    PCS 是否故障（**語言無關的唯一判斷入口**；報告模組與 Menu 共用，不再各自比對中文字串）。
      1) r["pcs_fault_flag"] 為 True/False → 直接採用
         （來源：guest PCS `systemFaultStatus.oldValue`，1=故障 / 0=正常，與 Accept-Language 無關）
      2) 旗標為 None（欄位缺失 / 舊 reading / 通訊失敗）→ 退回中文 badge 子字串比對，
         維持舊行為不變（不會因新增旗標而漏判）。
    ⚠️ 只看 systemFaultStatus；systemFailedStatus（失效）/ systemAlarmStatus（告警）
       不併入此判斷，維持與原「故障 in PCS當前狀態 badge」完全相同的語意。
    """
    if not isinstance(r, dict):
        return False
    flag = r.get("pcs_fault_flag")
    if flag is not None:
        return bool(flag)
    return "故障" in str(r.get("pcs_status", ""))          # fallback：旗標缺失才用（舊行為）


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

    # 1c) 設備今日累計充/放電量（guest；overview/rackCapacityInformation，與 Dashboard「當日」同源同值）
    #     直接取 API 值、不重新積分；僅供 Summary 對照，不進 samples.csv、不影響能量統計。
    rcap = _get(EP_RACKCAP)
    r["device_daily_charge_kwh"] = (_to_float(rcap.get("accumulatedDailyChargingEnergy"))
                                    if isinstance(rcap, dict) else None)
    r["device_daily_discharge_kwh"] = (_to_float(rcap.get("accumulatedDailyDisChargingEnergy"))
                                       if isinstance(rcap, dict) else None)

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
    # PCS 故障旗標（語言無關）：systemFaultStatus.oldValue（1=故障 / 0=正常 / None=缺失）。
    # 所有故障判斷一律經 pcs_is_fault() 讀此欄位，不再依賴中文 badge 字串（避免語系/文案調整失效）。
    r["pcs_fault_flag"] = _flag("systemFaultStatus")
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
    # 機器語意（語言無關）：smart / manual / none / unknown。供監看層判斷用，
    # 不要拿上面的中文顯示字串做比較（同 pcs_fault_flag 的理由：語系/文案可能調整）。
    r["pcs_control_mode_code"] = modes["control_mode"]
    r["pcs_work_mode"] = _GRID_MODE_DISPLAY.get(modes["grid_mode"], "未知")
    r["pcs_power_control_mode"] = modes["power_control_mode"]
    r["pcs_schedule_enabled"] = modes["schedule_enabled"]
    r["pcs_manual_switch"] = (schedule.get("manualModeSwitch") if isinstance(schedule, dict) else None)

    # 4) 電池上下電（authed）
    dodi = _get(EP_DODI)
    state, _ = battery_power_state_from_dodi(dodi)
    r["battery_power_status"] = state

    # 5) 告警（guest；抓第一頁，取 total 與 rows；完整去重在 AlarmTracker）
    #    帶 Accept-Language: zh-TW → 後端把 typeMark/targetMark/val/triggerVal 直接中文化
    #    （與 HMI 一致：device.type.bcu→電池、1838F4.soc.underAlarm→SOC過低報警、1838F4.minorAlarm→輕微報警）。
    alarm = _get(EP_ALARM, params={"pageNo": 1, "pageSize": 100}, headers=_PCS_LANG_HEADER)
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


# ---- 告警級別中文：完全比照 HMI 告警頁 render 規則（前端 index-68732cea.js）----
#   level===0 → serious；level===1 → medium；其他（2,3…）→ slight
#   對應 zh_TW 語系：serious=嚴重、medium=一般、slight=輕微（絕不自行猜測）
def _alarm_level_text(level):
    """level（數字）→ 中文級別，依 UI render 規則。非數字（已是中文）原樣返回。"""
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return "" if level in (None, "") else level
    if lv == 0:
        return CFG.ALARM_LEVEL_UI["serious"]
    if lv == 1:
        return CFG.ALARM_LEVEL_UI["medium"]
    return CFG.ALARM_LEVEL_UI["slight"]


def _repair_level_text(v):
    """regen 由 CSV 載入時：裸數字（0/1/2…）→ 依 UI 規則轉中文；已是中文則保留。"""
    s = str(v).strip()
    if s.lstrip("-").isdigit():
        return _alarm_level_text(int(s))
    return v


# ---- 告警物件/內容翻譯：查不到對照 → 保留原始 key 並登記（絕不自行造字）----
# {(field, key): 出現次數}；供 [ALARM_MAPPING_MISSING] 彙整與除錯 Log。
_ALARM_MAPPING_MISSING = {}


def _has_cjk(s):
    return any("一" <= ch <= "鿿" for ch in str(s))


def _is_untranslated_key(original, translated):
    """翻譯結果＝原字串、且原字串長得像 i18n key（含 '.' 且無中文）→ 視為未翻譯。"""
    return (translated == original and isinstance(original, str)
            and "." in original and not _has_cjk(original))


def _row_fully_translated(zh):
    """後端 row 是否已完全中文化（typeMark/targetMark/val 皆非 i18n key）。
    用於 regen 補譯：只有完全中文化才採用，避免用漏譯 raw key 覆蓋既有中文。"""
    if not isinstance(zh, dict):
        return False
    for k in ("typeMark", "targetMark", "val"):
        v = zh.get(k)
        if _is_untranslated_key(v, v):
            return False
    return True


def _resolve_alarm_text(value, field):
    """
    告警文字翻譯優先序（單一流程，report 與 scraper 共用）：
      (1) 後端已中文化值（Accept-Language: zh-TW）→ 直接採用；
      (2) 仍是 i18n key → 退回舊 mapping（ALARM_CODE_MAP，_code_text）；
      (3) 舊 mapping 也無 → 保留原始 key，並登記 [ALARM_MAPPING_MISSING]（絕不自行造字）。
    （priority(2)『已建立的 alarm zh mapping』在 regen 由 translate_map 於上游套用。）
    """
    if value is None or value == "":
        return ""
    if not _is_untranslated_key(value, value):
        return value                          # (1) 已中文化
    alt = _code_text(value)                    # (2) 舊 mapping fallback
    if not _is_untranslated_key(value, alt):
        return alt
    _ALARM_MAPPING_MISSING[(field, value)] = _ALARM_MAPPING_MISSING.get((field, value), 0) + 1
    return value                               # (3) 保留原始 key


def _translate_alarm_field(key, field):
    """targetMark/val → 中文；查不到保留原 key 並登記 mapping-missing（不猜測）。"""
    if key is None or key == "":
        return ""
    txt = _code_text(key)
    if _is_untranslated_key(key, txt):
        k = (field, key)
        _ALARM_MAPPING_MISSING[k] = _ALARM_MAPPING_MISSING.get(k, 0) + 1
    return txt


def _compose_alarm_content(target, operator, trigger, val):
    """
    告警內容（比照 HMI 告警詳情）：『觸發條件：{target}[ {op} {trigger}]；當前值：{val}』。
    target/trigger/val 皆為後端已中文化的字串（Accept-Language: zh-TW）。
    operator=="=="（等值告警）時不顯示 op/trigger，與 HMI render 一致。
    """
    target = "" if target is None else str(target)
    cond = f"觸發條件：{target}"
    op = "" if operator is None else str(operator)
    if op and op != "==" and trigger not in (None, ""):
        cond += f" {op} {trigger}"
    parts = [cond]
    if val not in (None, ""):
        parts.append(f"當前值：{val}")
    return "；".join(parts)


def normalize_alarm_record(raw_alarm, origin=None):
    """
    統一告警正規化：API 原始 row → 報表標準欄位（寫入 Excel/CSV 前一律經過此函式）。
    ⚠️ 前提：告警 row 以 Accept-Language: zh-TW 取得 → typeMark/targetMark/val/triggerVal 後端已中文化
       （device.type.bcu→電池、1838F4.soc.underAlarm→SOC過低報警、1838F4.minorAlarm→輕微報警）。
    輸出固定格式（另含 AlarmTracker 生命週期所需欄位，皆以 '_' 前綴內部欄位不輸出）：
      alarm_code       : 於 export 階段由 alarm_id_raw 穩定配號（此處先留 None）
      alarm_id_raw     : API 原始告警 ID（str，不重新產生、不亂數）
      level            : 依 UI render 規則轉中文（0嚴重 / 1一般 / 其他輕微）
      target_object    : 告警物件＝裝置（typeMark，如「電池」/「PCS」）
      alarm_content    : 『觸發條件：{targetMark}[ {op} {triggerVal}]；當前值：{val}』（比照 HMI）
      origin           : 資料來源（pre_existing / new）
      alarm_start_time : 原始告警時間（epoch ms → 文字）
    後端偶有未對照 key（仍是 i18n key）→ 保留原值並登記 [ALARM_MAPPING_MISSING]（不自行造字）。
    """
    rid = raw_alarm.get("id")
    lvl = raw_alarm.get("level")
    # 翻譯優先序：zh-TW 後端中文 →（退回）舊 mapping →（最後）原始 key＋警告（見 _resolve_alarm_text）
    type_txt = _resolve_alarm_text(raw_alarm.get("typeMark"), "target_object/typeMark")
    target_txt = _resolve_alarm_text(raw_alarm.get("targetMark"), "targetMark")
    val_txt = _resolve_alarm_text(raw_alarm.get("val"), "val")
    trig_txt = _resolve_alarm_text(raw_alarm.get("triggerVal"), "triggerVal")
    op = raw_alarm.get("operator")
    return {
        "alarm_code": None,   # 於 export_rows 由 AlarmCodeAllocator 依 alarm_id_raw 穩定配號
        "alarm_id_raw": "" if rid is None else str(rid),
        "level": _alarm_level_text(lvl),
        "_level_raw": lvl,
        "target_object": type_txt,
        "alarm_content": _compose_alarm_content(target_txt, op, trig_txt, val_txt),
        "origin": origin,
        "alarm_start_time": _epoch_ms_to_text(raw_alarm.get("alarmTime")),
        "_alarm_time_ms": raw_alarm.get("alarmTime"),
        "_raw": raw_alarm,    # 原始 API row（供除錯 Log；不輸出到 CSV/Excel）
    }


class AlarmCodeAllocator:
    """
    alarm_code 穩定配號：同一 alarm_id_raw 永遠對應同一 alarm_code；新 id 才配新流水號。
    持久化於 <output_root>/alarm_code_mapping.json；重新產生報告沿用既有 mapping，
    不因當下告警集合排序變動而改號（滿足唯一且穩定）。
    """

    def __init__(self, path):
        self.path = path
        data = _load_json_file(path) if path else None
        data = data if isinstance(data, dict) else {}
        self.seq = int(data.get("seq") or 0)
        self.map = dict(data.get("map") or {})
        self._dirty = False

    def code_for(self, alarm_id_raw, date_str):
        key = str(alarm_id_raw or "").strip()
        if not key:
            return None                      # 無 id → 由呼叫端後備配號
        if key in self.map:
            return self.map[key]             # 既有 id → 沿用原碼
        self.seq += 1
        code = f"ALM-{date_str}-{self.seq:03d}"
        self.map[key] = code
        self._dirty = True
        return code

    def save(self):
        if not self.path or not self._dirty:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"seq": self.seq, "map": self.map}, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"  [警告] 寫入 alarm_code_mapping 失敗：{e}")


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
        # 統一經 normalize_alarm_record()：alarm_id_raw（字串精確、不亂數）、
        # level（UI 規則）、target_object/alarm_content（查不到保留原 key + 警告）。
        rec = normalize_alarm_record(a, origin=origin)
        lvl = rec["_level_raw"]
        rec.update({
            "first_seen_time": now_str,
            "first_seen_elapsed_seconds": elapsed,
            "recovery_time": "",
            "duration_seconds": None,
            "is_recovery": False,
            "alarm_status": _alarm_status_text(a.get("alarmStatus")),
            "caused_stop": (lvl in CFG.ALARM_STOP_LEVELS) and origin == "new",
            "_active": _alarm_active(a.get("alarmStatus")),
        })
        return rec

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

    def export_rows(self, code_map_path=None):
        """
        輸出 alarms.csv/Excel 列（依 ALARM_FIELDS）。
        alarm_code = ALM-YYYYMMDD-NNN：由 AlarmCodeAllocator 依 alarm_id_raw 穩定配號——
        同一來源告警永遠對應同一 code，新 id 才配新流水號，重新產生不改號
        （持久化於 code_map_path；未提供路徑或無 id 時後備用排序序號）。
        YYYYMMDD 優先取 alarm_start_time 日期，無效則取 first_seen_time 日期。
        （alarm_code 僅供顯示/查找，不參與去重；去重仍以 alarm_id_raw / _key 為準。）
        """
        def _sortkey(r):
            v = r.get("_alarm_time_ms")
            try:
                return (0, int(v))
            except (TypeError, ValueError):
                return (1, r.get("first_seen_elapsed_seconds") or 0)
        allocator = AlarmCodeAllocator(code_map_path) if code_map_path else None
        out = []
        for i, rec in enumerate(sorted(self.records.values(), key=_sortkey), 1):
            date = _alarm_yyyymmdd(rec)
            idr = str(rec.get("alarm_id_raw") or "").strip()
            code = allocator.code_for(idr, date) if allocator else None
            if not code:                                   # 無 mapping 路徑或無 id → 後備位置序號
                code = f"ALM-{date}-{i:03d}"
            rec["alarm_code"] = code
            out.append({k: rec.get(k) for k in CFG.ALARM_FIELDS})
        if allocator:
            allocator.save()
        return out

    def load_existing(self, rows, translate_map=None):
        """
        resume/regen：把既有 alarms.csv 列重建為 records（保留歷史、維持去重、不重複新增）。
        translate_map（可選）= {alarm_id_raw: 後端中文化 row}：用於 regen 舊報告時，
        以「當下設備（Accept-Language: zh-TW）」補譯既有紀錄的 target_object/alarm_content/level，
        不改變告警集合（僅依 alarm_id_raw 對應補譯；找不到者維持 CSV 原值）。
        """
        translate_map = translate_map or {}
        # 次要對照：以「後端中文化 targetMark/val」為簽章，讓已從設備消失（無法用 id 對應）
        # 但同型的舊紀錄也能補譯成新格式（例如多筆相同的 PCS「直流輸入故障」）。
        by_sig = {}
        for row in translate_map.values():
            by_sig[(str(row.get("targetMark")), str(row.get("val")))] = row
        for a in rows or []:
            rid = str(a.get("alarm_id_raw") or "").strip()
            key = ("id", rid) if rid else ("compose", a.get("target_object"),
                                           a.get("alarm_content"), a.get("alarm_start_time"))
            if key in self.records:
                continue
            zh = translate_map.get(rid) if rid else None
            if zh is None:      # id 對不到 → 試以既有中文 targetMark/val 簽章對應
                zh = by_sig.get((str(a.get("target_object")), str(a.get("alarm_content"))))
            # 僅在 live row「確實完全中文化」時才採用，避免用後端偶發漏譯的 raw key
            # 覆蓋 CSV 既有的中文內容（zh-TW 優先，但不可用未中文化蓋掉已存在中文）。
            if zh is not None and _row_fully_translated(zh):
                norm = normalize_alarm_record(zh, origin=a.get("origin") or "pre_existing")
                t_obj, t_cnt, lvl_txt = norm["target_object"], norm["alarm_content"], norm["level"]
            else:
                # 無補譯來源（或 live 未完全中文化）：沿用 CSV 值。裸數字級別依 UI 規則修正；
                # target_object/alarm_content 若仍是未翻譯 key（如 1838F4.*）→ 登記 mapping-missing。
                t_obj = a.get("target_object")
                t_cnt = a.get("alarm_content")
                lvl_txt = _repair_level_text(a.get("level"))
                for fld, val in (("target_object", t_obj), ("alarm_content", t_cnt)):
                    if _is_untranslated_key(val, val):
                        _ALARM_MAPPING_MISSING[(fld, val)] = _ALARM_MAPPING_MISSING.get((fld, val), 0) + 1
            self.records[key] = {
                "alarm_id_raw": rid,
                "level": lvl_txt,
                "_level_raw": None,
                "target_object": t_obj,
                "alarm_content": t_cnt,
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
                "_raw": None,
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
        # 故障判斷改讀 systemFaultStatus.oldValue（pcs_is_fault；語言無關），不再比對中文 badge 字串。
        if pcs_is_fault(r):
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
# Cell 快照：抓取（可注入）＋統計＋色階（純函式，Excel/CSV/未來 Web 共用）
# ----------------------------------------------------------------------
#   _fetch_cell_packs()       單一唯讀入口（selftest 可 monkeypatch；失敗回 (None, error)）
#   calculate_cell_statistics() 該筆快照的 Max/Min/Average/Diff（含 mean/std/IQR 供離群判定）
#   get_voltage_fill / get_temperature_fill  值→{bg,font,level}（不碰 openpyxl，可跨輸出重用）
# ======================================================================
def _fetch_cell_packs(client):
    """
    取得一次 Cell 原始 pack list（重用 battery_data_scraper.fetch_battery_cell_data）。
    回傳 (data, error_message)；任何例外/None 皆回 (None, 原因字串)，呼叫端據此標記 failed，
    **絕不沿用上一筆或 cache 冒充本次資料**。selftest 以 monkeypatch 注入合成資料。
    """
    try:
        data = BDS.fetch_battery_cell_data(client)
    except Exception as e:                       # 網路/解析/登入等任何失敗
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(data, list) or not data:
        return None, "getPackInformation 回傳空資料或非預期格式"
    return data, None


_CELL_DATA_KEY = {"voltage": "cell_voltage", "temperature": "cell_temperature"}


def _cell_valid_values(snapshot, data_type):
    """收集該快照所有 Pack、所有 Cell 的有效數值（排除 None/NaN）；data_type ∈ voltage/temperature。"""
    key = _CELL_DATA_KEY[data_type]
    out = []
    for _pk, d in (snapshot.get("packs") or {}).items():
        for v in (d.get(key) or []):
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f == f:                            # 排除 NaN
                out.append(f)
    return out


def calculate_cell_statistics(snapshot, data_type):
    """
    計算該筆快照的 Cell 統計（**先用原始值計算，顯示時才格式化**）：
      max / min / average / diff（=max-min）/ count，另含 mean / std / q1 / q3 / iqr 供色階離群判定。
    空值/None/NaN/無效字串一律不納入；無有效值 → 各統計為 None。
    """
    vals = _cell_valid_values(snapshot, data_type)
    n = len(vals)
    if n == 0:
        return {"max": None, "min": None, "average": None, "diff": None, "count": 0,
                "mean": None, "std": None, "q1": None, "q3": None, "iqr": None}
    vmax, vmin = max(vals), min(vals)
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / n            # 母體變異數（相對色階用，非統計推論）
    std = var ** 0.5

    def _pct(sorted_vals, q):                                # 線性內插百分位（Q1=0.25、Q3=0.75）
        if len(sorted_vals) == 1:
            return sorted_vals[0]
        pos = q * (len(sorted_vals) - 1)
        lo = int(pos)
        frac = pos - lo
        hi = min(lo + 1, len(sorted_vals) - 1)
        return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac
    sv = sorted(vals)
    q1, q3 = _pct(sv, 0.25), _pct(sv, 0.75)
    return {"max": vmax, "min": vmin, "average": mean, "diff": vmax - vmin, "count": n,
            "mean": mean, "std": std, "q1": q1, "q3": q3, "iqr": q3 - q1}


def _band_into(t, levels):
    """t∈[0,1] → levels 清單中的等級（依清單長度等分）。"""
    n = len(levels)
    if n <= 1:
        return levels[0] if levels else "normal"
    return levels[min(max(int(t * n), 0), n - 1)]


def _band_level(t):
    """t∈[0,1] → 完整 CELL_LEVEL_ORDER 等分（threshold 模式用）。"""
    return _band_into(t, CFG.CELL_LEVEL_ORDER)


def _band_centered(t, levels):
    """
    normal 置中、佔中央大部分（CELL_NORMAL_BAND_FRAC）的分段：
      - t 落在中央帶 → normal（levels 中央）；只有更靠近兩端者才落到外圈等級。
      - 避免資料整體偏高/偏低時整片被判成非 normal（如電壓 3.215~3.224 幾乎全綠）。
    levels 需為奇數長、對稱、normal 在中央（符合 CELL_LEVEL_GATE 各階設計）。
    """
    n = len(levels)
    if n <= 1:
        return levels[0] if levels else "normal"
    m = n // 2                                   # 中央（normal）索引
    frac = max(0.0, min(0.98, float(getattr(CFG, "CELL_NORMAL_BAND_FRAC", 0.6))))
    lo, hi = 0.5 - frac / 2, 0.5 + frac / 2
    if lo <= t <= hi:
        return levels[m]
    if t < lo:                                   # 下半：levels[0..m-1] 分佈於 [0, lo]
        if m <= 0 or lo <= 0:
            return levels[0]
        return levels[min(m - 1, int((t / lo) * m))]
    upper = n - 1 - m                            # 上半：levels[m+1..] 分佈於 [hi, 1]
    if upper <= 0 or hi >= 1:
        return levels[n - 1]
    return levels[m + 1 + min(upper - 1, int(((t - hi) / (1 - hi)) * upper))]


def _gate_active_levels(diff, data_type):
    """
    Diff-Gate：依該筆 Diff（Max−Min）大小回傳「可用等級清單」（低→高、對稱、以 normal 為中心）。
    Diff 越小可用等級越少（趨近只有 normal＝全綠）；Diff 夠大才逐步解鎖外圈等級直到 abnormal 兩端。
    """
    gate = CFG.CELL_LEVEL_GATE.get(data_type)
    if not gate:
        return list(CFG.CELL_LEVEL_ORDER)
    try:
        d = abs(float(diff)) if diff is not None else 0.0
    except (TypeError, ValueError):
        d = 0.0
    for thr, levels in gate:
        if thr is None or d <= thr:
            return levels
    return gate[-1][1]


def _gated_level(value, stats, data_type):
    """
    Diff-Gate 相對分級：先依 stats['diff'] 決定「可用等級集合」，再依 value 在 min~max 的相對位置
    映射到該集合。Diff 很小 → 集合僅 normal（全綠）；**不再對微小雜訊硬鋪滿 7 段或判離群**。
    電壓/溫度各自套用自己的門檻（見 CFG.CELL_LEVEL_GATE），不共用數值範圍。
    """
    if value is None or stats is None or stats.get("count", 0) == 0:
        return "no_data"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "no_data"
    if v != v:
        return "no_data"
    levels = _gate_active_levels(stats.get("diff"), data_type)
    if len(levels) <= 1:
        return levels[0] if levels else "normal"
    vmin, vmax = stats.get("min"), stats.get("max")
    span = (vmax - vmin) if (vmin is not None and vmax is not None) else 0
    if not span:                                   # 全相同值 → 取中央（normal）
        return levels[len(levels) // 2]
    return _band_centered((v - vmin) / span, levels)   # normal 置中、佔中央大部分


def _threshold_level(value, low, high):
    """固定門檻分級（未來 COLOR_MODE='threshold' 用）：低於 low→abnormal_low、高於 high→abnormal_high，
    介於 low~high 依相對位置等分為 len(CELL_LEVEL_ORDER) 段。"""
    if value is None:
        return "no_data"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "no_data"
    if v != v:
        return "no_data"
    if low is not None and v < low:
        return "abnormal_low"
    if high is not None and v > high:
        return "abnormal_high"
    if low is not None and high is not None and high > low:
        return _band_level((v - low) / (high - low))
    return "normal"


def _auto_font(argb):
    """依底色亮度自動選字色（深底白字、淺底黑字）；不更動使用者選定的填滿色。"""
    h = (argb or "FF000000")[-6:]
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "000000"
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "FFFFFF" if lum < 140 else "000000"


def _fill_for_level(level):
    """level key → {'bg','font','level'}；bg 取自 CFG.CELL_COLOR_PALETTE（正規化 FF+後6位不透明），
    font 由 bg 亮度自動決定。找不到 level → no_data。"""
    raw = CFG.CELL_COLOR_PALETTE.get(level) or CFG.CELL_COLOR_PALETTE["no_data"]
    argb = "FF" + str(raw)[-6:].upper()           # 統一 8 位 ARGB、FF 不透明
    return {"bg": argb, "font": _auto_font(argb), "level": level}


def get_voltage_fill(value, stats):
    """Cell 電壓 → 顏色描述 {'bg','font','level'}（不碰 openpyxl；Excel/CSV/Web 共用）。
    COLOR_MODE='relative' 走相對色階；='threshold' 走 CELL_THRESHOLDS voltage_low/high。
    與溫度**分開**判斷，不共用數值範圍。"""
    if CFG.CELL_COLOR_MODE == "threshold":
        th = CFG.CELL_THRESHOLDS
        return _fill_for_level(_threshold_level(value, th.get("voltage_low"), th.get("voltage_high")))
    return _fill_for_level(_gated_level(value, stats, "voltage"))


def get_temperature_fill(value, stats):
    """Cell 溫度 → 顏色描述 {'bg','font','level'}（與電壓分開，獨立函式、獨立 Diff-Gate 門檻）。"""
    if CFG.CELL_COLOR_MODE == "threshold":
        th = CFG.CELL_THRESHOLDS
        return _fill_for_level(_threshold_level(value, th.get("temperature_low"), th.get("temperature_high")))
    return _fill_for_level(_gated_level(value, stats, "temperature"))


def get_voltage_summary_fill(value, stats, side):
    """
    Cell Volt.「偏離平均電壓 %」上色（Summary 最大/最小/平均電壓 + Pack 矩陣每格共用）。
      side='high'：dev% = (value−avg)/avg×100 → 高端 normal→medium_high→high→abnormal_high
      side='low' ：dev% = (avg−value)/avg×100 → 低端 normal→medium_low→low→abnormal_low
      平均電壓：value=avg → dev=0 → normal。
    顏色一律由**單一 palette**（CELL_COLOR_PALETTE＝cell_palette_template.xlsx）提供（_fill_for_level），
    故 Summary 與 Pack 矩陣同 level_key 完全同色。value/avg 無效 → 回 None（不上色）。
    """
    avg = stats.get("average")
    if value is None or avg is None:
        return None
    try:
        v = float(value)
        a = float(avg)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    dev = ((v - a) if side == "high" else (a - v)) / a * 100.0
    dev = round(max(dev, 0.0), 4)                      # 只看該側正向偏差；去浮點雜訊
    if dev < CFG.CELL_VOLT_DEV1:
        band = "normal"
    elif dev < CFG.CELL_VOLT_DEV2:
        band = "medium"
    elif dev <= CFG.CELL_VOLT_DEV3:                    # 0.50 本身仍屬 high/low
        band = "strong"
    else:
        band = "abnormal"                             # 僅 >0.50 才 abnormal
    # 偏差 band → 模板 level_key；顏色一律由單一 palette（CELL_COLOR_PALETTE＝模板）提供（_fill_for_level）
    return _fill_for_level(CFG.CELL_VOLT_DEV_LEVELS[side][band])


def get_voltage_cell_fill(value, stats):
    """
    Cell Volt. **Pack 矩陣每格**：與左側 Summary 同一套「偏離全域平均電壓 %」規則（統一色階）。
      value >= avg → 高側；value < avg → 低側；相等 → normal（dev=0）。
    因此同一 Snapshot 中，最高 Cell 與 Summary 最大電壓、最低 Cell 與 Summary 最小電壓
    會得到「相同 level_key 與相同顏色」。無效值 → no_data（模板無資料色）。
    """
    avg = stats.get("average")
    if value is None or avg is None:
        return _fill_for_level("no_data")
    try:
        v = float(value)
        a = float(avg)
    except (TypeError, ValueError):
        return _fill_for_level("no_data")
    if v != v or a == 0:
        return _fill_for_level("no_data")
    side = "high" if v >= a else "low"
    fd = get_voltage_summary_fill(v, stats, side)
    return fd if fd else _fill_for_level("no_data")


def get_diff_fill(diff, data_type):
    """
    差值（Max-Min）色階 → {'bg','font','level'}：依 Diff-Gate 階層，取「該階可用等級的最高一級」
    代表嚴重度（Diff 越小越綠、越大越紅；小差＝normal 綠）。與矩陣/摘要同一套門檻，一致。
    threshold 模式優先用 CELL_THRESHOLDS 的 <type>_diff_max。
    """
    if diff is None:
        return _fill_for_level("no_data")
    if CFG.CELL_COLOR_MODE == "threshold":
        ref = CFG.CELL_THRESHOLDS.get(f"{data_type}_diff_max")
        if ref and ref > 0:
            try:
                d = abs(float(diff))
            except (TypeError, ValueError):
                return _fill_for_level("no_data")
            return _fill_for_level("abnormal_high" if d >= ref else _band_level(d / ref))
    return _fill_for_level(_gate_active_levels(diff, data_type)[-1])


# ======================================================================
# Cell Volt. / Cell Temp. 工作表：共用版面框架
# ----------------------------------------------------------------------
# 兩表用同一套版面函式，透過參數（data_type/formatter/statistics/color_provider/labels）區分。
# 未來新增 Cell Resistance / Balance / Alarm 只需提供對應 formatter + statistics + color_provider，
# 不需複製整份工作表程式。色階計算 / 顏色選擇 / Excel 上色三者已拆開（見 get_*_fill）。
# ======================================================================
def _cell_geometry():
    """由 config 推導版面幾何（不寫死於繪圖流程）：回傳各區塊尺寸（欄列數）。"""
    cols = CFG.CELL_MATRIX_COLS
    cpp = CFG.CELLS_PER_PACK
    mrows = -(-cpp // cols)                       # ceil：每 Pack 矩陣列數（20/4=5）
    layout = CFG.CELL_PACK_LAYOUT
    maxpacks = max(len(r) for r in layout) if layout else 1
    pack_w, pack_h = cols, 1 + mrows              # Pack：1 標題列 + 矩陣列
    gap_col = gap_row = 1
    grid_w = maxpacks * pack_w + (maxpacks - 1) * gap_col
    grid_h = len(layout) * pack_h + (len(layout) - 1) * gap_row
    summary_w, summary_gap, header_h, block_gap = 2, 1, 3, 2
    block_w = summary_w + summary_gap + grid_w
    block_h = header_h + grid_h
    return {"cols": cols, "cpp": cpp, "mrows": mrows, "pack_w": pack_w, "pack_h": pack_h,
            "gap_col": gap_col, "gap_row": gap_row, "grid_w": grid_w, "grid_h": grid_h,
            "summary_w": summary_w, "summary_gap": summary_gap, "header_h": header_h,
            "block_w": block_w, "block_h": block_h, "block_gap": block_gap}


def _dynamic_pack_layout(packs):
    """
    依 CELL_PACK_LAYOUT 順序，只保留快照中「實際存在」的 Pack；缺的略過並壓縮該列（不留 N/A 空位）。
    不在樣板中的 Pack 依序補在最後（每列寬度沿用樣板最大列寬）。回傳 list[list[int]]。
    """
    present = set()
    for pk in (packs or {}).keys():
        try:
            present.add(int(pk))
        except (TypeError, ValueError):
            pass
    rows, placed = [], set()
    for layout_row in CFG.CELL_PACK_LAYOUT:
        kept = [p for p in layout_row if p in present]
        if kept:
            rows.append(kept)
            placed.update(kept)
    extras = sorted(p for p in present if p not in placed)
    if extras:
        w = max((len(r) for r in CFG.CELL_PACK_LAYOUT), default=4)
        for i in range(0, len(extras), w):
            rows.append(extras[i:i + w])
    return rows


def _thin_border():
    from openpyxl.styles import Border, Side
    s = Side(style="thin", color="B0B0B0")
    return Border(left=s, right=s, top=s, bottom=s)


def _num_or_none(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def render_pack_matrix(ws, top, left, pack_no, values, stats, color_provider, number_format):
    """
    畫一個 Pack 的 Cell 矩陣：Pack 名稱置中於矩陣上方，Cell 數值置中、完整框線、依色階上色。
    values=None（缺該 Pack）或不足 → 對應格顯示 N/A（不使報表失敗）。原始值存入 cell、顯示才格式化。
    """
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    g = _cell_geometry()
    cols, mrows, cpp = g["cols"], g["mrows"], g["cpp"]
    border = _thin_border()
    center = Alignment(horizontal="center", vertical="center")
    # Pack 標題（跨矩陣欄寬）
    ws.merge_cells(start_row=top, start_column=left, end_row=top, end_column=left + cols - 1)
    tc = ws.cell(row=top, column=left, value=f"Pack {pack_no}")
    tc.font = Font(bold=True, color="000000", size=9)
    tc.alignment = center
    tc.fill = PatternFill("solid", fgColor="F2F2F2")
    for c in range(left, left + cols):
        ws.cell(row=top, column=c).border = border
    # 矩陣格（依 packList 原順序，由左至右、由上至下）
    vals = list(values) if values else []
    for idx in range(cpp):
        rr = top + 1 + idx // cols
        cc = left + idx % cols
        raw = _num_or_none(vals[idx]) if idx < len(vals) else None
        cell = ws.cell(row=rr, column=cc)
        cell.alignment = center
        cell.border = border
        if raw is None:
            cell.value = "N/A"
            spec = _fill_for_level("no_data")
        else:
            cell.value = raw
            cell.number_format = number_format
            spec = color_provider(raw, stats)
        cell.fill = PatternFill("solid", fgColor=spec["bg"])
        cell.font = Font(color=spec["font"], size=9)
    _ = get_column_letter  # 版面欄寬於 create_cell_sheet 統一設定


def render_record_block(ws, top, left, snapshot, data_type, number_format, stat_format,
                        color_provider, summary_labels):
    """
    畫一筆紀錄區塊：上方置中粗體「時間」+「Snapshot 來源」，左側統計摘要，右側 Pack 矩陣（固定排列）。
    失敗快照（cell_data_status=failed）只畫標頭與錯誤訊息，不參與統計/矩陣。
    """
    from openpyxl.styles import Font, PatternFill, Alignment
    g = _cell_geometry()
    center = Alignment(horizontal="center", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")
    ts = snapshot.get("timestamp") or ""
    reason = snapshot.get("snapshot_reason") or ""
    reason_label = CFG.CELL_SNAPSHOT_REASON_LABEL.get(reason, reason)
    # session_start 顯示文字依實際模式對齊 charge_start/discharge_start（僅顯示文字，不改 snapshot_reason/資料）：
    #   充電 → Charge Start、放電 → Discharge Start、待機 → 維持 Session Start。
    _mode = snapshot.get("mode")
    if reason == "session_start" and _mode in ("charge", "discharge"):
        reason_label = "Charge Start" if _mode == "charge" else "Discharge Start"

    def _merge_header(row, text, bold=False, color="000000", fill=None):
        ws.merge_cells(start_row=row, start_column=left,
                       end_row=row, end_column=left + g["block_w"] - 1)
        c = ws.cell(row=row, column=left, value=text)
        c.font = Font(bold=bold, color=color, size=11 if bold else 10)
        c.alignment = center
        if fill:
            c.fill = PatternFill("solid", fgColor=fill)
    _merge_header(top, f"時間：{ts}", bold=True)
    _merge_header(top + 1, f"Snapshot：{reason_label}")

    content_top = top + g["header_h"]

    # 失敗快照：只顯示錯誤，不畫矩陣
    if snapshot.get("cell_data_status") == "failed":
        ws.merge_cells(start_row=content_top, start_column=left,
                       end_row=content_top, end_column=left + g["block_w"] - 1)
        c = ws.cell(row=content_top, column=left,
                    value=f"⚠ Cell 資料擷取失敗（cell_data_status=failed）：{snapshot.get('error_message')}")
        c.font = Font(bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor="FF0000")
        c.alignment = left_align
        return

    stats = calculate_cell_statistics(snapshot, data_type)

    # ---- 左側統計摘要（標籤列 + 數值列，直向堆疊）----
    # 數值儲存格背景色**共用** color_provider（與右側 Pack 矩陣同一套 get_voltage_fill/
    # get_temperature_fill、同一份 stats）→ 兩邊必定一致；差值用 get_diff_fill。SOC/Power 不上色。
    sc = left
    rr = content_top
    border = _thin_border()

    def _summary_pair(label, value, number_fmt=None, is_str=False, bold=False,
                      fill_desc=None, border_only=False):
        nonlocal rr
        lc = ws.cell(row=rr, column=sc, value=label)
        lc.font = Font(bold=True, size=9)
        lc.alignment = left_align
        vc = ws.cell(row=rr + 1, column=sc, value=value)
        vc.alignment = left_align
        if not is_str and value is not None and number_fmt:
            vc.number_format = number_fmt
        if fill_desc is not None:
            vc.fill = PatternFill("solid", fgColor=fill_desc["bg"])
            vc.font = Font(size=10, bold=bold, color=fill_desc["font"])
            vc.border = border
        elif border_only:
            vc.font = Font(size=10, bold=bold)     # 白底（不填色）、黑字，但保留框線（壓差/溫差固定白底）
            vc.border = border
        else:
            vc.font = Font(size=10, bold=bold)     # SOC/Power：維持黑字、不上色、不加框線
        rr += 2

    title_c = ws.cell(row=rr, column=sc, value=summary_labels["title"])
    title_c.font = Font(bold=True, size=11, color="1F4E78")
    rr += 1
    # Cell Volt. 摘要 最大/最小/平均電壓：改用「偏離平均電壓 %」對稱色階（高側暖/低側冷）；
    # Cell Temp. 維持原 color_provider。
    if data_type == "voltage":
        max_fill = get_voltage_summary_fill(stats["max"], stats, "high")
        min_fill = get_voltage_summary_fill(stats["min"], stats, "low")
        avg_fill = get_voltage_summary_fill(stats["average"], stats, "high")   # dev=0 → 綠
    else:
        max_fill = color_provider(stats["max"], stats)
        min_fill = color_provider(stats["min"], stats)
        avg_fill = color_provider(stats["average"], stats)
    _summary_pair(summary_labels["max"], stats["max"], stat_format, bold=True, fill_desc=max_fill)
    _summary_pair(summary_labels["min"], stats["min"], stat_format, bold=True, fill_desc=min_fill)
    _summary_pair(summary_labels["avg"], stats["average"], stat_format, fill_desc=avg_fill)
    # 壓差 / 溫差：固定白底、不套任何 level 色階；只保留數值、數字格式、框線、字體（電壓與溫度皆同）。
    _summary_pair(summary_labels["diff"], stats["diff"], stat_format, bold=True, border_only=True)
    soc = snapshot.get("soc")
    power = snapshot.get("power_kw")
    _summary_pair("當前SOC", (f"{soc:.0f} %" if isinstance(soc, (int, float)) else "N/A"), is_str=True)
    _summary_pair("當前功率", (f"{power:.2f} kW" if isinstance(power, (int, float)) else "N/A"),
                  is_str=True)
    if snapshot.get("warnings"):
        wc = ws.cell(row=rr, column=sc,
                     value=f"⚠ {len(snapshot['warnings'])} 項資料警告（見 cell_snapshots.json）")
        wc.font = Font(size=8, color="C00000")
        wc.alignment = left_align
        rr += 1
    # 左側摘要只保留 最大/最小/平均/極差 + SOC/功率；不再逐筆重複顯示 Legend（資訊冗餘、已移除）。

    # ---- 右側 Pack 矩陣（只排「實際存在」的 Pack，依 CELL_PACK_LAYOUT 順序、壓縮缺項）----
    packs = snapshot.get("packs") or {}
    key = _CELL_DATA_KEY[data_type]
    display_rows = _dynamic_pack_layout(packs)
    grid_left = left + g["summary_w"] + g["summary_gap"]
    for li, layout_row in enumerate(display_rows):
        prow_top = content_top + li * (g["pack_h"] + g["gap_row"])
        for pj, pack_no in enumerate(layout_row):
            pcol = grid_left + pj * (g["pack_w"] + g["gap_col"])
            pdata = packs.get(str(pack_no))
            values = (pdata.get(key) if isinstance(pdata, dict) else None)
            render_pack_matrix(ws, prow_top, pcol, pack_no, values, stats,
                               color_provider, number_format)


def setup_cell_sheet_print_area(ws, total_rows, total_cols, data_type):
    """版面/列印：Landscape、Fit to Width、凍結最上方、縮放、依 data_type 設矩陣欄寬。"""
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties
    g = _cell_geometry()
    period = g["block_w"] + g["block_gap"]
    matrix_w = CFG.CELL_VOLT_COL_WIDTH if data_type == "voltage" else CFG.CELL_TEMP_COL_WIDTH
    for c in range(1, total_cols + 1):
        off = (c - 1) % period
        if off < g["summary_w"]:
            w = CFG.CELL_SUMMARY_LABEL_WIDTH if off == 0 else CFG.CELL_SUMMARY_VALUE_WIDTH
        elif off < g["block_w"]:
            w = matrix_w
        else:
            w = 2                                  # 區塊之間的間隔欄
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.page_setup.orientation = CFG.CELL_SHEET_PAGE_ORIENTATION
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_view.zoomScale = CFG.CELL_SHEET_ZOOM
    ws.freeze_panes = "A3"                          # 固定最上方模式標題/時間列


def _fmt_number_for_width(v, number_format):
    """依 number_format 概估數值顯示字串（供欄寬估算；不影響實際儲存值/格式）。"""
    try:
        fmt = str(number_format or "")
        if "." in fmt:
            dec = len(fmt.split(".")[1])
            return f"{float(v):.{dec}f}"
        if fmt.strip() in ("0", "#,##0", "General", ""):
            return f"{float(v):.0f}" if float(v) == int(float(v)) else str(v)
        return str(v)
    except (TypeError, ValueError):
        return str(v)


def _disp_width(value, number_format):
    """顯示寬度估算（CJK 全形≈2、其餘≈1）；數值先依格式轉字串。"""
    s = _fmt_number_for_width(value, number_format) if isinstance(value, (int, float)) else str(value)
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def _fit_summary_columns(ws):
    """依內容自動調整 Cell 表「左側摘要」欄（off==0）欄寬，避免數值顯示成 ###。
    以實際內容（標籤 CJK / 數值 / SOC/功率字串）最大顯示寬 × 字級比例計算；只調欄寬，其餘不變。
    在字體 +Δ 之後呼叫，故已反映放大後字級。"""
    from openpyxl.utils import get_column_letter
    g = _cell_geometry()
    period = g["block_w"] + g["block_gap"]
    # 跨欄合併（時間/Snapshot/模式帶標題）的錨點跳過——它們橫跨整個區塊，不應撐寬單一摘要欄
    merged_anchors = set()
    for rng in ws.merged_cells.ranges:
        if rng.min_col != rng.max_col:
            merged_anchors.add((rng.min_row, rng.min_col))
    for c in range(1, ws.max_column + 1):
        if (c - 1) % period != 0:          # 只處理每個區塊的摘要欄（off==0；標籤與數值同欄）
            continue
        letter = get_column_letter(c)
        need = 0.0
        for cell in ws[letter]:
            if cell.value is None or (cell.row, cell.column) in merged_anchors:
                continue
            fs = cell.font.size or 11
            need = max(need, _disp_width(cell.value, cell.number_format) * fs / 11.0)
        if need:
            ws.column_dimensions[letter].width = min(40.0, max(CFG.CELL_SUMMARY_LABEL_WIDTH, need + 1.5))


def _bump_sheet_font(ws, delta):
    """把整張工作表「已使用字體大小」逐格 +delta（其餘字型屬性/填色/框線/對齊/欄寬/列高皆不變）。
    只處理有值的儲存格（合併非錨點格 value=None 自動略過，避免 MergedCell 設定錯誤）；
    未指定大小者以 openpyxl 預設 11pt 為基準 +delta。以「現值+Δ」套用，保留各元素大小比例。"""
    from openpyxl.styles import Font
    if not delta:
        return
    for row in ws.iter_rows():
        for c in row:
            if c.value is None:
                continue
            f = c.font
            base = f.size if (f and f.size) else 11
            c.font = Font(name=f.name, size=base + delta, bold=f.bold, italic=f.italic,
                          color=f.color, underline=f.underline, strike=f.strike,
                          vertAlign=f.vertAlign)


def create_cell_sheet(wb, sheet_name, data_type, snapshots, *, number_format, stat_format,
                      color_provider, summary_labels):
    """
    共用框架：建立一張 Cell 明細工作表（Cell Volt. 或 Cell Temp.）。
      - 依 mode 分區（Discharge 黃 / Charge 藍 / Standby 灰）；標題列跨越該模式所有紀錄區塊。
      - 同模式多筆紀錄橫向依時間排列；紀錄數動態，不固定筆數。
      - 空資料模式不產生區塊；完全無快照 → 顯示提示。
    """
    from openpyxl.styles import Font, PatternFill, Alignment
    ws = wb.create_sheet(sheet_name)
    center = Alignment(horizontal="center", vertical="center")
    g = _cell_geometry()

    valid = [s for s in (snapshots or [])]
    if not valid:
        ws["A1"] = f"{sheet_name}：本次 session 無 Cell 快照資料"
        ws["A1"].font = Font(bold=True, size=12, color="808080")
        _bump_sheet_font(ws, CFG.CELL_SHEET_FONT_DELTA)
        return ws

    # 依 mode 分組（未知/idle → standby，不硬歸類）
    by_mode = {"discharge": [], "charge": [], "standby": []}
    for s in valid:
        m = s.get("mode") if s.get("mode") in by_mode else "standby"
        by_mode[m].append(s)
    for m in by_mode:
        by_mode[m].sort(key=lambda s: str(s.get("timestamp") or ""))

    period = g["block_w"] + g["block_gap"]
    cur_row = 1
    max_cols = 1
    for mode in CFG.CELL_BAND_ORDER:
        band = by_mode.get(mode) or []
        if not band:
            continue                                # 空區塊不產生
        band_cfg = CFG.CELL_MODE_BAND[mode]
        band_cols = len(band) * period - g["block_gap"]
        max_cols = max(max_cols, band_cols)
        # 模式標題列（跨越該模式所有紀錄區塊）
        ws.merge_cells(start_row=cur_row, start_column=1,
                       end_row=cur_row, end_column=max(1, band_cols))
        bc = ws.cell(row=cur_row, column=1, value=band_cfg["label"])
        bc.font = Font(bold=True, size=12, color=band_cfg["font"])
        bc.fill = PatternFill("solid", fgColor=band_cfg["bg"])
        bc.alignment = center
        block_top = cur_row + 2
        for i, snap in enumerate(band):
            left = 1 + i * period
            render_record_block(ws, block_top, left, snap, data_type, number_format,
                                stat_format, color_provider, summary_labels)
        cur_row = block_top + g["block_h"] + g["block_gap"]

    setup_cell_sheet_print_area(ws, cur_row, max_cols, data_type)
    # 最後統一把兩表所有字體 +CELL_SHEET_FONT_DELTA（現值+Δ，保比例；版面/框線/底色/對齊不變）
    _bump_sheet_font(ws, CFG.CELL_SHEET_FONT_DELTA)
    # 再依內容自動調整摘要欄寬（在放大字級之後），避免數值出現 ###
    _fit_summary_columns(ws)
    return ws


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
        # Cell 快照（起訖＋方向切換擷取；session 內累積、不覆蓋，供 Cell Volt./Temp. 產表）
        self.cell_snapshots = []
        self._modes_snapshotted = set()     # 已擷取過 start 快照的模式（判 *_start vs mode_changed_to_*）
        self._last_snapshot_mode = None     # 最近一筆快照的模式（避免同模式重複觸發）
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
        # 設備今日累計充/放電量（API 直接值，取最新一筆有效；僅供 Summary 對照，不參與能量積分）
        self.device_daily = {"charge": None, "discharge": None}

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
        # session_start Cell 快照（綁定開始狀態的方向/SOC/功率；起始若已是 charge/discharge，
        # 後續同模式的 *_start 會因去重不重複產生）
        d0, _s0 = resolve_direction(r.get("pcs_charging_flag"), r.get("pcs_discharging_flag"),
                                    r.get("actual_active_power_kw"))
        p0 = (r.get("actual_active_power_kw") if CFG.POWER_SOURCE == "pcs"
              else r.get("calculated_power_kw"))
        self._capture_cell_snapshot(r, d0, p0, "session_start")
        self._persist_state()
        return r

    def _start_resume(self):
        # 續接：載入既有 samples.csv（全 Session）與 alarms.csv（維持去重），不重寫表頭
        self.samples = self._load_all_samples()
        self.alarm_tracker.load_existing(self._load_csv_rows(CFG.FILE_ALARMS))
        self._load_cell_snapshots()          # 續接既有 Cell 快照（保留歷史、不覆蓋）
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
        # 記錄設備今日累計（API 值；取最新有效，供 Summary 對照，不影響上面的能量積分）
        if r.get("device_daily_charge_kwh") is not None:
            self.device_daily["charge"] = r.get("device_daily_charge_kwh")
        if r.get("device_daily_discharge_kwh") is not None:
            self.device_daily["discharge"] = r.get("device_daily_discharge_kwh")

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
                           f"{_code_text(a.get('val'))} level={_alarm_level_text(lvl)}")
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
        # Cell 快照：僅在進入 charge/discharge（方向切換）時擷取（idle/standby 不主動擷取）；
        # 首次進入某模式→{mode}_start，之後再切回→mode_changed_to_{mode}。
        if (getattr(CFG, "CELL_SNAPSHOT_ENABLED", True) and direction in ("charge", "discharge")
                and direction != self._last_snapshot_mode):
            reason = (f"mode_changed_to_{direction}" if direction in self._modes_snapshotted
                      else f"{direction}_start")
            self._capture_cell_snapshot(r, direction, power, reason)
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

    # ---------- Cell 快照擷取 / 持久化 ----------
    def _capture_cell_snapshot(self, r, direction, power, reason):
        """
        於關鍵時機擷取一筆完整 Cell 快照（呼叫 battery_data_scraper 唯讀取得）。
        - 綁定當下 timestamp / mode / soc / power；失敗標 cell_data_status=failed（不沿用舊值）。
        - 去重：與前一筆比對 timestamp+mode+soc+power+cell_data_hash 五者皆同 → 跳過。
        - 累積至 self.cell_snapshots 並即時寫 cell_snapshots.json（不覆蓋既有快照）。
        """
        if not getattr(CFG, "CELL_SNAPSHOT_ENABLED", True):
            return None
        mode = direction if direction in ("charge", "discharge") else "standby"
        data, err = _fetch_cell_packs(self.client)
        soc = (r or {}).get("soc_percent")
        # 綁定該次擷取的實際時間（用 session 時鐘 now_fn，與 elapsed 一致；離線測試可注入假時鐘）
        ts = self.now_fn().strftime("%Y-%m-%d %H:%M:%S")
        snap = BDS.create_cell_snapshot(
            data, ts, mode, soc, power, reason,
            error_message=err, expected_packs=CFG.CELL_EXPECTED_PACKS)
        # 去重（timestamp+mode+soc+power+cell_data_hash 五者皆同 → 視為重複，例如 session_start 與
        # charge_start 恰同一秒且同資料）
        if self.cell_snapshots:
            p = self.cell_snapshots[-1]
            if (p.get("timestamp") == snap["timestamp"] and p.get("mode") == snap["mode"]
                    and p.get("soc") == snap["soc"] and p.get("power_kw") == snap["power_kw"]
                    and p.get("cell_data_hash") == snap["cell_data_hash"]):
                self.log_event("cell_snapshot_dedup", "info",
                               f"reason={reason} 與 {p.get('snapshot_id')} 完全相同，跳過")
                self._last_snapshot_mode = mode
                if mode in ("charge", "discharge"):
                    self._modes_snapshotted.add(mode)
                return None
        snap["snapshot_id"] = f"SNAP-{len(self.cell_snapshots) + 1:03d}"
        self.cell_snapshots.append(snap)
        self._last_snapshot_mode = mode
        if mode in ("charge", "discharge"):
            self._modes_snapshotted.add(mode)
        self._write_cell_snapshots()
        status = snap["cell_data_status"]
        self.log_event("cell_snapshot", "warning" if status == "failed" else "info",
                       f"{snap['snapshot_id']} reason={reason} mode={mode} status={status} "
                       f"packs={snap['pack_count']} cells={snap['cell_count']}"
                       + (f" err={err}" if err else "")
                       + (f" warnings={len(snap['warnings'])}" if snap.get("warnings") else ""))
        return snap

    def _write_cell_snapshots(self):
        """寫 cell_snapshots.json（版本化；session 內累積歷史 Cell 快照，供 Cell Volt./Temp. 產表）。"""
        obj = {
            "schema_version": BDS.CELL_SNAPSHOT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "generated_at": _now_str(),
            "snapshot_count": len(self.cell_snapshots),
            "snapshots": self.cell_snapshots,
        }
        self._dump_json(CFG.FILE_CELL_SNAPSHOTS, obj)

    def _load_cell_snapshots(self):
        """resume/regen：讀回既有 cell_snapshots.json（保留歷史；還原去重/reason 判斷狀態）。"""
        obj = _load_json_file(self._path(CFG.FILE_CELL_SNAPSHOTS))
        snaps = obj.get("snapshots") if isinstance(obj, dict) else None
        self.cell_snapshots = snaps if isinstance(snaps, list) else []
        for s in self.cell_snapshots:
            m = s.get("mode")
            if m in ("charge", "discharge"):
                self._modes_snapshotted.add(m)
        if self.cell_snapshots:
            self._last_snapshot_mode = self.cell_snapshots[-1].get("mode")

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
        # session_end Cell 快照（綁定結束當下狀態；與前一筆完全相同會去重，但結束狀態仍存於 summary）
        d_end, _se = resolve_direction(r.get("pcs_charging_flag"), r.get("pcs_discharging_flag"),
                                       r.get("actual_active_power_kw"))
        p_end = (r.get("actual_active_power_kw") if CFG.POWER_SOURCE == "pcs"
                 else r.get("calculated_power_kw"))
        self._capture_cell_snapshot(r, d_end, p_end, "session_end")
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
        self._write_cell_snapshots()         # Cell 快照持久化（含 pause/finalize 當下的最新累積）
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
            # Session 固定標籤（self.action）不放逐筆 Raw Data；保留於 session_state.json / Summary / 資料夾名
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

    def _alarm_code_map_path(self):
        """alarm_code 穩定 mapping 檔（放輸出根目錄＝session 資料夾的上層，跨 session/regen 共用）。"""
        return os.path.join(os.path.dirname(os.path.abspath(self.folder)), CFG.FILE_ALARM_CODE_MAP)

    def _log_alarm_mapping_diagnostics(self, alarm_rows):
        """
        寫入 Excel 前的告警除錯 Log：
          - 對每個找不到對照的 i18n key 印 [ALARM_MAPPING_MISSING] key=...（去重）。
          - 對每筆「未翻譯」告警印原始 API JSON / level / targetMark / val /
            alarm_id_raw / alarm_code / 翻譯後結果，方便追查（不影響輸出內容）。
        """
        if _ALARM_MAPPING_MISSING:
            print("  [告警翻譯稽核] 有未對照的 i18n key（Excel 暫保留原始 key，未自行造字）：")
            for (field, key), n in sorted(_ALARM_MAPPING_MISSING.items()):
                print(f"    [ALARM_MAPPING_MISSING] field={field} key={key}  (出現 {n} 次)")
        # 逐筆列出未翻譯的告警明細
        abnormal = [a for a in alarm_rows
                    if _is_untranslated_key(a.get("target_object"), a.get("target_object"))
                    or _is_untranslated_key(a.get("alarm_content"), a.get("alarm_content"))]
        if not abnormal:
            return
        print(f"  [告警翻譯稽核] 未翻譯告警 {len(abnormal)} 筆明細：")
        rec_by_id = {}
        for rec in self.alarm_tracker.records.values():
            rec_by_id[str(rec.get("alarm_id_raw") or "")] = rec
        for a in abnormal:
            rec = rec_by_id.get(str(a.get("alarm_id_raw") or ""))
            raw = rec.get("_raw") if rec else None
            print(f"    - alarm_code={a.get('alarm_code')} alarm_id_raw={a.get('alarm_id_raw')}")
            print(f"      level(翻譯後)={a.get('level')!r} target_object={a.get('target_object')!r} "
                  f"alarm_content={a.get('alarm_content')!r}")
            if isinstance(raw, dict):
                print(f"      原始 API：level={raw.get('level')!r} targetMark={raw.get('targetMark')!r} "
                      f"val={raw.get('val')!r}")
                print(f"      raw JSON={json.dumps(raw, ensure_ascii=False)}")
            else:
                print("      原始 API JSON：（regen 由 CSV 載入，無原始 row）")

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
            # 設備今日累計（API 直接值；與 Dashboard「當日充/放電量」同源，非本次 Session 積分）
            "device_daily_charge_kwh": self.device_daily.get("charge"),
            "device_daily_discharge_kwh": self.device_daily.get("discharge"),
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
        rows = self.alarm_tracker.export_rows(self._alarm_code_map_path())
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
        # 寫到 test_output（selftest 合成資料）→ 標題加 TEST DATA / SELFTEST，避免誤認為正式實機報告
        _mark = "⚠ TEST DATA / SELFTEST — " if _within(self.folder, _TEST_OUTPUT_ROOT) else ""
        ws["A1"] = _mark + "PCS 充放電報告 — Summary"
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
        r = _section(ws, r, 1, "本次充放電統計（Σ功率×Δt 積分）")
        r = _kv(ws, r, 1, "本次充電量(kWh)", stats.get("charged_energy_kwh"), hi=True)
        r = _kv(ws, r, 1, "本次放電量(kWh)", stats.get("discharged_energy_kwh"), hi=True)
        r = _kv(ws, r, 1, "本次淨電量(kWh)", stats.get("net_energy_kwh"), hi=True)
        r += 1
        r = _section(ws, r, 1, "設備今日累計（API 直接值，非本次積分；同 Dashboard 當日）")
        r = _kv(ws, r, 1, "設備今日充電量 (kWh)", sess.get("device_daily_charge_kwh"))
        r = _kv(ws, r, 1, "設備今日放電量 (kWh)", sess.get("device_daily_discharge_kwh"))
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
        # 註：Health（星等評分）已移除；Summary 保留 Communication 與 Alarm，KPI 併入下半部。

        # ================= Summary 下半部：KPI（原獨立 KPI 分頁併入；公式/樣式不變，不再有 KPI 分頁）====
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
            ("SOC增加(充電)", _soc_pct_str(cs["soc_change"])),
            ("SOC下降(放電)", _soc_pct_str(round(-ds["soc_change"], 2) if ds["soc_change"] is not None else None)),
            ("本次充電量(kWh)", stats.get("charged_energy_kwh")),
            ("本次放電量(kWh)", stats.get("discharged_energy_kwh")),
            ("本次淨電量(kWh)", stats.get("net_energy_kwh")),
            ("往返效率(%)", stats.get("round_trip_efficiency_percent")),
            ("資料筆數", stats.get("sample_count")),
            ("資料完整率", f"{analysis['comm_rate'] * 100:.1f}%"),
        ]
        r += 1
        r = _section(ws, r, 1, "KPI")
        kpi_hdr = r
        ws.cell(row=kpi_hdr, column=1, value="項目")
        ws.cell(row=kpi_hdr, column=2, value="值")
        _hdr_row(ws, kpi_hdr, 2)                       # 保留原 KPI 表頭樣式
        for i, (k, v) in enumerate(kpi_rows, start=1):
            ws.cell(row=kpi_hdr + i, column=1, value=k)
            ws.cell(row=kpi_hdr + i, column=2, value=v)
        if kpi_rows:
            _table(ws, f"A{kpi_hdr}:B{kpi_hdr + len(kpi_rows)}", "tbl_kpi")
        _autofit(ws, 2, minw=16, maxw=52)             # 含 KPI 內容後統一調欄寬

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
                # §9 [DEBUG] X-LABEL 驗證列印：僅 CFG.DEBUG_XLABEL=True 時輸出；正式執行保持 Console 乾淨。
                #     只控制列印，不影響 Label 演算法 / ±3s 搜尋 / N 秒選取 / Excel 圖表。
                if CFG.DEBUG_XLABEL:
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
            ("本次充電量(kWh)", stats.get("charged_energy_kwh"), True),
            ("本次放電量(kWh)", stats.get("discharged_energy_kwh"), True),
            ("本次淨電量(kWh)", stats.get("net_energy_kwh"), True),
            ("取樣筆數", stats.get("sample_count"), False),
        ]
        _record_sheet("Charge & Discharge", all_rows, "Session Analysis",
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
        _record_sheet("Charge", charge_rows, "Charge Summary", "Charge Current & Rack Temperature",
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
        _record_sheet("Discharge", discharge_rows, "Discharge Summary", "Discharge Current & Rack Temperature",
                      discharge_pairs, [], "tbl_discharge")

        # ================= Cell Volt. / Cell Temp.（Discharge 之後、Raw Data 之前）=================
        # 只有「本次 session 有歷史 Cell 快照」才產生兩張工作表；讀既有快照、不重抓最新值覆蓋歷史。
        # 無快照（如 Cell 功能之前建立、無 cell_snapshots.json 的舊報告）→ 略過，**不產生空白表**，
        # 避免造成「有支援卻空白」的誤解。
        if self.cell_snapshots:
            create_cell_sheet(wb, "Cell Volt.", "voltage", self.cell_snapshots,
                              number_format=CFG.CELL_VOLT_CELL_FORMAT,
                              stat_format=CFG.CELL_VOLT_STAT_FORMAT,
                              color_provider=get_voltage_cell_fill,   # Pack 矩陣＝偏離全域平均%（與 Summary 統一）
                              summary_labels=CFG.CELL_VOLT_SUMMARY_LABELS)
            create_cell_sheet(wb, "Cell Temp.", "temperature", self.cell_snapshots,
                              number_format=CFG.CELL_TEMP_CELL_FORMAT,
                              stat_format=CFG.CELL_TEMP_STAT_FORMAT,
                              color_provider=get_temperature_fill,
                              summary_labels=CFG.CELL_TEMP_SUMMARY_LABELS)
        else:
            print("  [Cell] 無 Cell 快照（此報告建立時尚未支援 Cell 快照功能）"
                  "→ 略過 Cell Volt./Cell Temp.，不產生空白工作表。")

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
        alarm_rows = self.alarm_tracker.export_rows(self._alarm_code_map_path())
        self._log_alarm_mapping_diagnostics(alarm_rows)   # 寫入 Excel 前的告警翻譯稽核 Log
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
            # 僅 CFG.TAB_COLORS 有列出的工作表(Summary)上色；其餘設 None → 不寫 tabColor（Excel 預設白色）
            wsx.sheet_properties.tabColor = CFG.TAB_COLORS.get(wsx.title)

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


def _fake_pack_data(npacks=19, ncells=20, vbase=3.30, tbase=25.0, missing=(),
                    drop_cells=0, inject_nan=False, inject_none=False):
    """
    selftest 專用：產生 getPackInformation 格式的合成 pack list（預設 19×20），供 Cell 快照測試。
    - missing：略過的 packNo（模擬缺 Pack）；drop_cells：Pack 1 少幾顆 cell（模擬缺 Cell）。
    - inject_nan / inject_none：於 Pack 1 注入 NaN / None 電壓（模擬無效值）。
    """
    data = []
    for pk in range(1, npacks + 1):
        if pk in missing:
            continue
        cells = []
        m = ncells - (drop_cells if pk == 1 else 0)
        for j in range(m):
            v = round(vbase + (j % 10) * 0.003 + (pk % 3) * 0.002, 3)
            t = tbase + (j % 6) + (pk % 4)
            if inject_nan and pk == 1 and j == 0:
                v = float("nan")
            if inject_none and pk == 1 and j == 1:
                v = None
            cells.append({"sort": (pk - 1) * ncells + j + 1, "voltage": v,
                          "temperature": t, "soc": 90, "soh": 99, "equilibriumState": 0})
        data.append({"packNo": pk, "packList": cells})
    return data


def _selftest_cell_snapshots(check, output_root):
    """
    Cell 快照情境驗證（自帶 read_all / _fetch_cell_packs monkeypatch，注入合成資料、不觸網）：
      模式分組（Charge only / Discharge only / Charge↔Discharge）、資料異常（無資料/缺Pack/缺Cell/
      NaN/None）、同 timestamp 去重。
    """
    global read_all, _fetch_cell_packs
    _o_read, _o_fetch = read_all, _fetch_cell_packs
    clk = {"dt": datetime(2026, 3, 12, 9, 0, 0)}
    now = lambda: clk["dt"]                                   # noqa: E731
    cur = {"r": None, "packs": None, "err": None}

    def reading(power, soc, direction):
        v = 890.0
        i = round(power * 1000.0 / v, 2) if power else 0.0
        return {
            "communication_ok": True, "raw_source_time": "",
            "pcs_charging_flag": direction == "charge",
            "pcs_discharging_flag": direction == "discharge",
            "soc_percent": float(soc), "battery_voltage_v": v, "battery_current_a": i,
            "rack_max_temperature_c": 30.0, "rack_min_temperature_c": 25.0,
            "battery_status": "充電" if power > 0 else ("放電" if power < 0 else "待機"),
            "actual_active_power_kw": float(power), "actual_reactive_power_kvar": 0.0,
            "calculated_power_kw": round(v * i / 1000, 3),
            "pcs_status": "執行", "pcs_control_mode": "智慧模式", "pcs_work_mode": "併網",
            "pcs_power_control_mode": "交流有功", "pcs_manual_switch": 0,
            "pcs_fault_flag": False,                              # systemFaultStatus.oldValue=0（正常）
            "pcs_schedule_enabled": True, "battery_power_status": "已上電",
            "device_daily_charge_kwh": 4.6, "device_daily_discharge_kwh": 3.7,
            "alarm_rows": [], "alarm_total": 0,
        }

    read_all = lambda _c: cur["r"]                           # noqa: E731
    _fetch_cell_packs = lambda _c: ((cur["packs"], None) if cur["packs"] is not None
                                    else (None, cur["err"] or "無 Cell 資料"))   # noqa: E731

    def run_session(segs):
        """segs: [(power, soc, direction, n), ...]；回傳 finalize 後的 session。"""
        cur["packs"] = _fake_pack_data()
        p0, s0, d0, _n0 = segs[0]
        cur["r"] = reading(p0, s0, d0)
        sess = ReportSession("auto", 10.0, "交流有功", object(), output_root, now_fn=now)
        sess.start()
        for (power, soc, direction, n) in segs:
            for _ in range(n):
                clk["dt"] += timedelta(seconds=5)
                cur["r"] = reading(power, soc, direction)
                sess.sample_once()
        sess.finalize("completed")
        return sess

    try:
        print("\n[驗證：Cell 快照情境 — 模式分組 / 方向切換]")
        clk["dt"] = datetime(2026, 3, 12, 9, 0, 0)
        sco = run_session([(5.0, 60, "charge", 3)])
        check("Charge only：無 discharge 快照",
              "discharge" not in set(s["mode"] for s in sco.cell_snapshots))
        check("Charge only：含 session_start/session_end",
              {"session_start", "session_end"} <= set(s["snapshot_reason"] for s in sco.cell_snapshots))

        clk["dt"] = datetime(2026, 3, 12, 10, 0, 0)
        sdo = run_session([(-5.0, 60, "discharge", 3)])
        check("Discharge only：無 charge 快照",
              "charge" not in set(s["mode"] for s in sdo.cell_snapshots))

        clk["dt"] = datetime(2026, 3, 12, 11, 0, 0)
        scd = run_session([(5.0, 60, "charge", 2), (-5.0, 58, "discharge", 2)])
        rs = [s["snapshot_reason"] for s in scd.cell_snapshots]
        check("Charge→Discharge：兩模式皆有快照",
              {"charge", "discharge"} <= set(s["mode"] for s in scd.cell_snapshots))
        check(f"Charge→Discharge：含 discharge_start（{rs}）", "discharge_start" in rs)

        clk["dt"] = datetime(2026, 3, 12, 12, 0, 0)
        sdc = run_session([(-5.0, 60, "discharge", 2), (5.0, 62, "charge", 2)])
        rs2 = [s["snapshot_reason"] for s in sdc.cell_snapshots]
        check(f"Discharge→Charge：含 charge_start（{rs2}）", "charge_start" in rs2)

        print("\n[驗證：Cell 快照資料異常情境]")
        ts = "2026-03-12 09:26:51"
        snf = BDS.create_cell_snapshot(None, ts, "charge", 90, 5.0, "session_start")
        check("無 Cell Data：status=failed", snf["cell_data_status"] == "failed")
        check("無 Cell Data：packs 空、不沿用舊值", not snf["packs"])
        from openpyxl import Workbook
        wbf = Workbook()
        create_cell_sheet(wbf, "Cell Volt.", "voltage", [snf],
                          number_format=CFG.CELL_VOLT_CELL_FORMAT, stat_format=CFG.CELL_VOLT_STAT_FORMAT,
                          color_provider=get_voltage_cell_fill, summary_labels=CFG.CELL_VOLT_SUMMARY_LABELS)
        check("無 Cell Data：Cell Volt. 仍可產生（不崩潰）", "Cell Volt." in wbf.sheetnames)

        # 明確傳入 expected_packs=1..19 才檢查缺 Pack（預設 None＝動態、不檢查缺 Pack）
        smp = BDS.create_cell_snapshot(_fake_pack_data(missing=(5,)), ts, "charge", 90, 5.0,
                                       "session_start", expected_packs=list(range(1, 20)))
        check(f"缺 Pack 5：pack_count=18（{smp['pack_count']}）", smp["pack_count"] == 18)
        check("缺 Pack 5：warning 記錄缺少 Pack 5（有給 expected_packs）",
              any(("缺少 Pack" in w and "5" in w) for w in smp["warnings"]))
        # 動態模式（不給 expected_packs）：缺 Pack 不報 warning
        smp_dyn = BDS.create_cell_snapshot(_fake_pack_data(missing=(5,)), ts, "charge", 90, 5.0,
                                           "session_start")
        check("動態模式：缺 Pack 不列入 warning",
              not any("缺少 Pack" in w for w in smp_dyn["warnings"]))

        smc = BDS.create_cell_snapshot(_fake_pack_data(drop_cells=2), ts, "charge", 90, 5.0, "session_start")
        check("缺 Cell：Pack1 cell 數 18 → warning",
              any("cell 數 18" in w for w in smc["warnings"]))

        snan = BDS.create_cell_snapshot(_fake_pack_data(inject_nan=True), ts, "charge", 90, 5.0, "session_start")
        stv = calculate_cell_statistics(snan, "voltage")
        check("Cell NaN：max/min 非 NaN（已排除）",
              stv["max"] is not None and stv["max"] == stv["max"] and stv["min"] == stv["min"])
        check("Cell NaN：warning 記無效電壓", any("無效電壓" in w for w in snan["warnings"]))
        snone = BDS.create_cell_snapshot(_fake_pack_data(inject_none=True), ts, "charge", 90, 5.0, "session_start")
        check("Cell None：warning 記無效電壓", any("無效電壓" in w for w in snone["warnings"]))

        print("\n[驗證：同 timestamp 去重]")
        clk["dt"] = datetime(2026, 3, 12, 13, 0, 0)
        cur["packs"] = _fake_pack_data()
        cur["r"] = reading(5.0, 60, "charge")
        sdup = ReportSession("auto", 10.0, "交流有功", object(), output_root, now_fn=now)
        sdup.start()                                          # session_start（charge）
        n1 = len(sdup.cell_snapshots)
        sdup._capture_cell_snapshot(cur["r"], "charge", 5.0, "charge_start")   # 同秒同資料
        check(f"同 timestamp+mode+soc+power+hash → 去重（維持 {n1} 筆）",
              len(sdup.cell_snapshots) == n1 and n1 == 1)

        # ---- 無 Cell 快照（模擬舊報告）→ 不產生 Cell Volt./Cell Temp. 空白表 ----
        print("\n[驗證：無 Cell 快照 → 不建 Cell 工作表]")
        from openpyxl import load_workbook as _lw
        sco.cell_snapshots = []                               # 清空，模擬無 cell_snapshots.json
        sco._write_xlsx(sco._compute_statistics())
        wbx = _lw(os.path.join(sco.folder, CFG.FILE_XLSX))
        check("無 Cell 快照：report.xlsx 不含 Cell Volt./Cell Temp.（不產生空白表）",
              "Cell Volt." not in wbx.sheetnames and "Cell Temp." not in wbx.sheetnames)
    finally:
        read_all, _fetch_cell_packs = _o_read, _o_fetch


def selftest():
    """
    離線自我測試（不連設備）：驗證 Session append/resume 累積機制。
    三階段：① 新 Session 5 筆充電 → 暫停；② resume 再追加 5 筆充電 → 暫停；
            ③ resume 再追加 5 筆放電 → 完成。全程注入假時鐘與合成 reading，不觸網。
    """
    print("== charge_discharge_report 離線自我測試：Session append / resume 累積 ==")
    # selftest 一律寫入「單一測試目錄」output/test_output/selftest_latest，絕不碰正式報告目錄。
    # 每次執行前整個清空並重建 → 固定 timestamp 不再撞名累積成 _2/_3…；主流程與情境測試都在此目錄下。
    output_root = os.path.join(_TEST_OUTPUT_ROOT, CFG.SELFTEST_SUBDIR)
    if os.path.isdir(output_root):
        _safe_rmtree(output_root)   # 經保護：允許 test_output 底下，禁止刪正式報告目錄
    os.makedirs(output_root, exist_ok=True)
    print(f"   測試輸出目錄（每次清空重建）：{output_root}")
    base = datetime(2026, 7, 17, 9, 0, 0)
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
            "pcs_fault_flag": False,                              # systemFaultStatus.oldValue=0（正常）
            "battery_power_status": "已上電",
            "device_daily_charge_kwh": 4.6, "device_daily_discharge_kwh": 3.7,   # 合成設備今日累計
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

    global read_all, _fetch_cell_packs
    orig = read_all
    orig_fetch = _fetch_cell_packs
    read_all = lambda _c: cur["r"]                   # noqa: E731
    # Cell 快照抓取：注入合成 19×20 pack 資料（不觸網），供 Cell Volt./Temp. 產表與快照擷取驗證
    _fetch_cell_packs = lambda _c: (_fake_pack_data(), None)   # noqa: E731
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
            # 其餘工作表需為 Excel 預設（未設定 tabColor）
            for wsx in wbx.worksheets:
                if wsx.title in CFG.TAB_COLORS:
                    continue
                tc = wsx.sheet_properties.tabColor
                got = getattr(tc, "rgb", None) if tc is not None else None
                check(f"Tab色 {wsx.title} = 預設(未設定)（實際 {got}）", tc is None)
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
            check("KPI 分頁已移除（併入 Summary）", "KPI" not in wbx2.sheetnames)
            for sname in ("Charge & Discharge", "Charge", "Discharge"):
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

        # ---- PCS 故障判斷：語言無關（讀 systemFaultStatus.oldValue，非中文 badge 字串）----
        print("\n[驗證：pcs_fault_flag 語言無關故障判斷]")
        try:
            # 1) 旗標 True，但 badge 完全不含「故障」（英文 enum / 未翻譯）→ 仍須判為故障
            r_en = reading(5.0, 60, "charge")
            r_en.update(pcs_fault_flag=True, pcs_status="running / gridTied / fault")
            check("旗標=True 且 badge 為英文（無『故障』字樣）→ 判定故障", pcs_is_fault(r_en) is True)
            # 2) 旗標 False，但 badge 誤含「故障」字樣 → 以旗標為準，不判故障
            r_f = reading(5.0, 60, "charge")
            r_f.update(pcs_fault_flag=False, pcs_status="執行 / 併網 / 充電（故障已復歸）")
            check("旗標=False 且 badge 含『故障』字樣 → 以旗標為準，不判故障",
                  pcs_is_fault(r_f) is False)
            # 3) 旗標缺失（None，如舊 reading）→ 退回中文子字串比對（維持舊行為）
            r_none = reading(5.0, 60, "charge")
            r_none.update(pcs_fault_flag=None, pcs_status="執行 / 併網 / 故障")
            check("旗標=None → 退回中文比對（舊行為不變）", pcs_is_fault(r_none) is True)
            r_none2 = reading(5.0, 60, "charge")
            r_none2.update(pcs_fault_flag=None, pcs_status="執行 / 併網 / 充電")
            check("旗標=None 且 badge 無『故障』→ 不判故障", pcs_is_fault(r_none2) is False)
            check("非 dict 輸入 → 不判故障（不丟例外）", pcs_is_fault(None) is False)
            # 4) SafetyMonitor 需依旗標觸發 pcs_fault（英文 badge 亦然）
            hits = SafetyMonitor(10.0).check(r_en, 5.0)
            check("SafetyMonitor 依旗標觸發 pcs_fault（英文 badge）",
                  any(h[0] == "pcs_fault" for h in hits))
            hits_ok = SafetyMonitor(10.0).check(r_f, 5.0)
            check("SafetyMonitor 旗標=False 不觸發 pcs_fault",
                  not any(h[0] == "pcs_fault" for h in hits_ok))
        except Exception as e:
            check(f"pcs_fault_flag 驗證發生例外：{e}", False)
    finally:
        read_all = orig
        _fetch_cell_packs = orig_fetch

    # ---- Cell Volt. / Cell Temp. 工作表（主報告：phase③ finalize 產出）----
    print("\n[驗證：Cell Volt. / Cell Temp. 工作表]")
    try:
        from openpyxl import load_workbook
        wbc = load_workbook(os.path.join(folder, CFG.FILE_XLSX))
        names = wbc.sheetnames
        check("含 Cell Volt. 工作表", "Cell Volt." in names)
        check("含 Cell Temp. 工作表", "Cell Temp." in names)
        # 分頁順序：Cell Volt./Temp. 在 Discharge 之後、Raw Data 之前
        if all(n in names for n in ("Discharge", "Cell Volt.", "Cell Temp.", "Raw Data")):
            check("分頁順序：Discharge < Cell Volt. < Cell Temp. < Raw Data",
                  names.index("Discharge") < names.index("Cell Volt.")
                  < names.index("Cell Temp.") < names.index("Raw Data"))
        # cell_snapshots.json 存在且累積多筆（非只留最後一筆）
        snapobj = _load_json_file(os.path.join(folder, CFG.FILE_CELL_SNAPSHOTS)) or {}
        snaps = snapobj.get("snapshots") or []
        check(f"cell_snapshots.json 累積 ≥ 2 筆（{len(snaps)}）", len(snaps) >= 2)
        check("cell_snapshots.json 已版本化（schema_version）",
              snapobj.get("schema_version") == BDS.CELL_SNAPSHOT_SCHEMA_VERSION)
        reasons = [s.get("snapshot_reason") for s in snaps]
        check(f"含 session_start 與 session_end（reasons={reasons}）",
              "session_start" in reasons and "session_end" in reasons)
        check("含 discharge_start（phase③ 由充電切放電）", "discharge_start" in reasons)
        # 快照皆綁定 timestamp/mode/soc/power 且有 packs（非全部重用同一筆）
        ts_set = set(s.get("timestamp") for s in snaps)
        check(f"快照 timestamp 不全相同（{len(ts_set)} 種）", len(ts_set) >= 2)
        # Cell Volt. 顯示電壓（~3.x），Cell Temp. 顯示溫度（~2x）；抽第一個非 N/A 數值格驗證量級
        def _first_num(ws):
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, (int, float)):
                        return float(c.value)
            return None
        v0 = _first_num(wbc["Cell Volt."])
        t0 = _first_num(wbc["Cell Temp."])
        check(f"Cell Volt. 數值為電壓量級 2~5V（{v0}）", v0 is not None and 2.0 <= v0 <= 5.0)
        check(f"Cell Temp. 數值為溫度量級 0~80℃（{t0}）", t0 is not None and 0.0 <= t0 <= 80.0)
    except Exception as e:
        check(f"Cell 工作表驗證發生例外：{e}", False)

    # ---- Cell 快照情境驗證（read_all/_fetch_cell_packs 已還原，內部自帶 monkeypatch）----
    _selftest_cell_snapshots(check, output_root)

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


def _fetch_alarm_zh_map():
    """
    best-effort：以 Accept-Language: zh-TW 取回當下設備告警（guest，免登入），
    回傳 {alarm_id_raw(str): 後端中文化 row}，供 regen 補譯舊報告的未中文化 key。
    設備不可達 / 失敗 → 回 {}（不中斷 regen）。
    """
    try:
        from api_client import ApiClient
        cl = ApiClient()
        resp = cl.get(EP_ALARM, params={"pageNo": 1, "pageSize": 200}, headers=_PCS_LANG_HEADER)
        rows = []
        if isinstance(resp, dict):
            rows = resp.get("rows") or resp.get("list") or resp.get("records") or []
        m = {str(a["id"]): a for a in rows if isinstance(a, dict) and a.get("id") is not None}
        if m:
            print(f"[REGEN] 已取回 {len(m)} 筆中文化告警（Accept-Language: zh-TW）供補譯既有紀錄")
        return m
    except Exception as e:
        print(f"[REGEN] 略過告警中文補譯（設備不可達或失敗）：{type(e).__name__}")
        return {}


def regenerate_report(folder, backup_name=None):
    """
    用『唯一的 _write_xlsx』重產指定 session 資料夾的 report.xlsx（與 finalize/selftest 同一函式）。
    - 只讀該資料夾既有 samples.csv / alarms.csv / summary.json / cell_snapshots.json；不連設備、不動原始資料。
    - **不建立新的 session 資料夾**（走 resume 分支，直接在原資料夾內更新 report.xlsx）。
    - 重產前將原 report.xlsx 備份為 backup_name；預設 report_backup_YYYYMMDD_HHMMSS.xlsx（每次一份帶時間戳）。
    - 統計數值沿用 summary.json（不重算、不改變）。
    """
    global _now_str
    folder = os.path.abspath(folder)
    if not backup_name:
        backup_name = "report_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".xlsx"
    summ = _load_json_file(os.path.join(folder, CFG.FILE_SUMMARY))
    if not summ:
        print(f"[REGEN] 找不到或無法讀取 summary.json：{folder}")
        return False
    stats = summ.get("statistics") or {}
    sess_blk = summ.get("session") or {}

    sess = ReportSession.resume(folder, object())            # 僅 __init__（不 start、不寫檔）
    sess.samples = sess._load_all_samples()
    # 舊報告的 alarms.csv 可能存有未中文化的 i18n key（如 1838F4.soc.underAlarm）。
    # best-effort：若設備可達，以 Accept-Language: zh-TW 取回中文化告警，依 alarm_id_raw 補譯
    # 既有紀錄（不改變告警集合）；設備不可達則沿用 CSV 原值（不中斷 regen）。
    zh_map = _fetch_alarm_zh_map()
    sess.alarm_tracker.load_existing(sess._load_csv_rows(CFG.FILE_ALARMS), translate_map=zh_map)
    has_cell_file = os.path.exists(os.path.join(folder, CFG.FILE_CELL_SNAPSHOTS))
    sess._load_cell_snapshots()                              # 讀回歷史 Cell 快照（不重抓最新值）
    if not has_cell_file:
        # Cell 功能之前建立的舊報告：不回填、不產生 Cell 工作表（保持歷史真實性）
        print(f"[REGEN] 此報告建立時尚未支援 Cell 快照功能（無 {CFG.FILE_CELL_SNAPSHOTS}）"
              f"→ 不會產生 Cell Volt./Cell Temp.（不以現在的即時值回填歷史）。")
    sess.start_state = summ.get("start_state") or sess.start_state
    sess.end_state = summ.get("end_state")
    sess.end_reason = sess_blk.get("end_reason") or "completed"

    # 一併刷新 alarms.csv：level 依 UI 規則（裸數字 → 中文）、alarm_code 由 alarm_id_raw 穩定配號；
    # 欄位完全不變、僅修正值，使 CSV 與 report.xlsx 一致（原始量測資料 samples 不動）。
    if os.path.exists(os.path.join(folder, CFG.FILE_ALARMS)):
        sess._write_alarms()
        print("[REGEN] 已一併刷新 alarms.csv（level/alarm_code 依最新規則，欄位不變）")

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
    ap.add_argument("--backup-name", default=None,
                    help="--regen 時原 report.xlsx 的備份檔名（預設 report_backup_YYYYMMDD_HHMMSS.xlsx）")
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
