# -*- coding: utf-8 -*-
"""
PCS 充放電報告 — 設定檔（charge_discharge_report_config.py）
================================================================
所有取樣間隔、輸出路徑、欄位、門檻、enum 皆集中於此，**不散落寫死於主程式**。
本檔為純設定，不含任何 API 呼叫、不送任何控制命令。

正負號慣例（已與現場確認，見 charge_discharge_report_DESIGN.md §3）：
  - 以「量測值」為方向基準：Power > 0 → 充電；Power < 0 → 放電。
  - 控制 setpoint（充=負/放=正）僅保留原始值，**不**作為方向判斷。
"""

# ======================================================================
# 版本
# ======================================================================
REPORT_VERSION = "1.0.0"     # 報告產生器版本（輸出檔 report_version）
SCHEMA_VERSION = "1.3.0"     # 輸出資料結構版本（欄位/檔案結構變更時 +1）
# schema 1.1.0：alarms.csv/Excel 告警 ID 調整 —— alarm_id→alarm_id_raw（全程字串、防精度遺失），
#               新增 alarm_code（ALM-YYYYMMDD-NNN，僅供顯示/查找），欄位順序調整。
# schema 1.2.0：Session append/resume —— samples.csv 新增 sample_index /
#               cumulative_charge_energy_kwh / cumulative_discharge_energy_kwh；
#               新增 session_state.json（recording/paused/completed，供續接累積）。
# schema 1.3.0：samples.csv 新增 rack_max_temperature_c / rack_min_temperature_c
#               （來源 overview/rackExtremeValueInformation.rackCellMax/MinTemperature）；
#               紀錄頁整合圖改為 電流 + Rack 最高/最低溫度；Summary 移除 Health。

# ======================================================================
# 取樣與計算
# ======================================================================
SAMPLE_INTERVAL_SEC = 5          # 取樣間隔（秒）
POWER_SOURCE = "pcs"             # 實際功率主來源：pcs=totalActivePowerOfAcBus / battery_va=V×A
IDLE_KW = 0.1                    # |功率| ≤ 此值視為待機（沿用既有 _BATTERY_IDLE_KW）
ENERGY_METHOD = "trapezoid"      # 電量積分法：trapezoid（梯形）/ rectangle（矩形，備用）

# 往返效率防呆：只有同一 session 內同時具備「完整」充電與放電循環才計算。
# 判定：較小相位電量 ÷ 較大相位電量 ≥ 此比例，才視為完整循環；否則一律 N/A
# （避免單次充/放電中的微量反向雜訊被硬算成效率）。
RTE_MIN_PHASE_RATIO = 0.2

# 方向判斷（量測慣例）：Power > +IDLE_KW → charge；Power < -IDLE_KW → discharge；否則 idle
# 電流方向同理（充=正、放=負）。

# ======================================================================
# 輸出
# ======================================================================
# 相對專案根 output/ 的子目錄；主程式會轉為絕對路徑並自動建立
OUTPUT_SUBDIR = "charge_discharge_reports"

# session 資料夾命名：<開始時間>_<action>，例如 20260716_103000_discharge
SESSION_FOLDER_TIME_FMT = "%Y%m%d_%H%M%S"

# 輸出檔名
FILE_SUMMARY = "summary.json"
FILE_STATISTICS = "statistics.json"
FILE_SAMPLES = "samples.csv"
FILE_ALARMS = "alarms.csv"
FILE_EVENTS = "events.csv"
FILE_XLSX = "report.xlsx"
FILE_SESSION_STATE = "session_state.json"   # Session 狀態（recording/paused/completed，供續接累積）

# Session 狀態值
SESSION_RECORDING = "recording"
SESSION_PAUSED = "paused"
SESSION_COMPLETED = "completed"

# ======================================================================
# 設備識別（無實際來源者留 None；operator 帳號充當操作人員）
# ======================================================================
DEVICE_NAME = None               # 設備名稱（None → 未提供）
PCS_NO = None                    # PCS 編號
RACK_NO = None                   # 電池櫃 / Rack 編號
DEFAULT_OPERATOR = "hmiUser"     # 目前登入固定帳號；無獨立操作人員來源時充當
CREATED_BY = None                # 產生報告者；None → 自動取本機登入帳號（getpass.getuser）
# 設備 IP：留 None 時由 api_client.BASE_URL 自動解析（見主程式 _device_ip()）
DEVICE_IP = None

# ======================================================================
# 控制模式 ↔ setpoint 對照（對齊 device_control_operator.PCS_CONTROL_MODES）
#   交流有功  → activePowerSetPoint（功率 kW）
#   直流恆流  → dcCurrentSetPoint（電流 A）
#   直流恆功率→ dcPowerSetPoint（功率 kW）
# 值：(setpoint 種類, 單位, summary 欄位名)
# ======================================================================
MODE_SETPOINT = {
    "交流有功":   ("power",   "kW", "power_setpoint_kw"),
    "直流恆流":   ("current", "A",  "current_setpoint_a"),
    "直流恆功率": ("power",   "kW", "dc_power_setpoint_kw"),
}

# ======================================================================
# 結束原因 enum 與中文對照（需求第三點）
# ======================================================================
END_REASON = {
    "completed": "正常完成",
    "manual_stop": "人工停止",
    "user_stop": "人工停止",             # 手動 Menu 17 結束
    "control_stop": "控制停止",           # Menu 7 送出 PCS 停止充放電後結束
    "auto_stop": "自動偵測停止",           # 背景偵測設備回停止/待機（非經 Menu 7）
    "fault_stop": "故障停止",             # 背景偵測 PCS 故障
    "direction_switch": "方向切換",        # 充↔放切換：先結束舊 Session
    "pcs_stop": "PCS停止",
    "alarm_stop": "告警中止",
    "timeout": "執行超時",
    "communication_error": "通訊異常",
    "control_failed": "控制失敗",
    "unknown": "未知",
}

# ======================================================================
# 安全 / 異常監測門檻（報告只監測、只建議，**不送任何控制命令**）
# ======================================================================
SOC_MAX_PERCENT = 95             # SOC 上限；超過 → 建議停止
SOC_MIN_PERCENT = 10             # SOC 下限；低於 → 建議停止
COMM_FAIL_MAX = 3                # 連續通訊失敗次數 → 建議停止（communication_error）
SESSION_TIMEOUT_SEC = 3600       # 單一 session 最長監測秒數 → timeout
# 實際功率長時間偏離設定值
POWER_DEVIATION_KW = 15          # 偏離門檻（|實際 - 設定|）
POWER_DEVIATION_SEC = 30         # 持續超過此秒數才觸發
# 電壓 / 電流門檻（None = 不檢查；依站點填實際上下限）
VOLTAGE_MAX_V = None
VOLTAGE_MIN_V = None
CURRENT_MAX_A = None             # 取絕對值比較
# 告警級別觸發（0=嚴重、1=一般）；新增這些級別的告警 → 建議停止
ALARM_STOP_LEVELS = {0}

# 觸發嚴重條件時是否讓「獨立監測模式」自動結束（True：偵測到即結束並標記對應 end_reason）。
# 注意：這只影響「報告是否停止記錄」，**永遠不會**送出任何控制命令。
AUTO_END_ON_CRITICAL = True
# 連續判定為 idle / PCS 停止的取樣次數 → 視為充放電結束（pcs_stop / completed）
IDLE_END_SAMPLES = 3

# ---- 啟動門檻（Arming）：未真正進入充/放電前「不建立 Session/資料夾」----
# run_live 先進入 arming 監測（不建資料夾），需同時滿足：
#   1) 方向為 charge 或 discharge
#   2) |battery_current_a| >= START_CURRENT_THRESHOLD_A（排除 0A / 小幅漂移）
#   連續達到 START_CONFIRM_SAMPLES 次才判定「真正進入充/放電」→ 此時才建立 Session。
START_CURRENT_THRESHOLD_A = 1.0     # 啟動電流門檻（絕對值，A）
START_CONFIRM_SAMPLES = 2           # 需連續成立的取樣次數（去抖動）

# X 軸時間 Label 間隔（秒）：以第一筆為基準，每隔此秒數挑最接近的一筆標記；
# 首/尾一定顯示；曲線仍用完整資料（只調整 Label 顯示）。改此值即同步改變三個紀錄頁。
# N 秒一個；10 → 每 10 秒一個；30 → 每 30 秒一個
X_AXIS_LABEL_INTERVAL_SEC = 10  # 註：N 必須大於取樣間隔（約 5s），否則幾乎每筆都命中目標→形同全顯示。

# X 軸 Label 選取的除錯列印（[X-LABEL 驗證]…目標/區間/選中）。預設 False＝正式執行不輸出；
# 僅開發驗證時設 True 才印。此旗標只控制 Console 輸出，不影響 Label 演算法/±3s 搜尋/Excel 圖表。
DEBUG_XLABEL = False

# ---- 圖表自適應尺寸（三個紀錄頁共用；只變寬度、高度固定）----
# 依 X 軸 Label 數量決定圖表寬度：Label 越多圖越寬，避免右側/底部大片空白或 Label 過密。
#   width = CHART_BASE_WIDTH_CM + label_count * CHART_WIDTH_PER_LABEL_CM
#   再限制在 [CHART_MIN_WIDTH_CM, CHART_MAX_WIDTH_CM]
# Plot Area／Legend／左右留白皆為比例式（manualLayout 分數）→ 隨寬度同步縮放、不跑位。
CHART_ADAPTIVE_SIZE = False       # 固定版型：三頁同尺寸、不依 Label 數/資料量改變（True→依 Label 數自適應）
CHART_HEIGHT_CM = 22.0            # 固定高度
CHART_WIDTH_CM = 32.0            # 固定寬度（CHART_ADAPTIVE_SIZE=False 時使用）
CHART_BASE_WIDTH_CM = 16.0       # 自適應基準寬度
CHART_WIDTH_PER_LABEL_CM = 0.7   # 每個 X 軸 Label 增加的寬度
CHART_MIN_WIDTH_CM = 20.0        # 寬度下限
CHART_MAX_WIDTH_CM = 60.0        # 寬度上限

# ======================================================================
# 輸出欄位（samples / alarms / events）— writer 與 Excel 共用同一份定義
# ======================================================================
# 註：Session 固定動作標籤（charge/discharge/auto）刻意「不」放入逐筆 Raw Data
#     （每筆都重複同一值、無資訊量）；僅保留於 session_state.json / Summary / metadata / 資料夾名。
#     每筆即時充放電方向請看 charge_discharge_direction 欄（charge/discharge/idle，逐筆變化）。
SAMPLE_FIELDS = [
    "sample_index", "timestamp", "elapsed_seconds", "target_power_kw",
    "pcs_status", "pcs_control_mode", "pcs_work_mode", "pcs_power_control_mode",
    "actual_active_power_kw", "actual_reactive_power_kvar",
    "battery_status", "battery_power_status",
    "soc_percent", "battery_voltage_v", "battery_current_a",
    "rack_max_temperature_c", "rack_min_temperature_c",
    "calculated_power_kw",
    "cumulative_charge_energy_kwh", "cumulative_discharge_energy_kwh", "cumulative_energy_kwh",
    "charge_discharge_direction", "direction_source",
    "alarm_count", "communication_ok", "raw_source_time",
    # 追查用（保留原始 vs 正規化）
    "raw_active_power", "normalized_active_power", "raw_current", "normalized_current",
]
# samples.csv 中「應解析為數值」的欄位（resume 讀回時轉 float）
SAMPLE_NUMERIC_FIELDS = {
    "sample_index", "elapsed_seconds", "target_power_kw",
    "actual_active_power_kw", "actual_reactive_power_kvar",
    "soc_percent", "battery_voltage_v", "battery_current_a",
    "rack_max_temperature_c", "rack_min_temperature_c", "calculated_power_kw",
    "cumulative_charge_energy_kwh", "cumulative_discharge_energy_kwh", "cumulative_energy_kwh",
    "raw_active_power", "normalized_active_power", "raw_current", "normalized_current",
}

ALARM_FIELDS = [
    "alarm_code", "alarm_id_raw", "level", "target_object", "alarm_content", "origin",
    "first_seen_time", "first_seen_elapsed_seconds", "alarm_start_time",
    "recovery_time", "duration_seconds", "is_recovery", "alarm_status", "caused_stop",
]
# 註：
#  - alarm_id_raw：後端原始 ID，**全程字串**處理（不轉 int、CSV/Excel 皆文字），避免 Excel
#    對 >15 位數字精度遺失或顯示成 2.11873E+17。去重仍以 alarm_id_raw 為準。
#  - alarm_code：ALM-YYYYMMDD-NNN，僅供報告顯示與人工查找，不參與去重。
#  - API 的 alarm row 無「恢復時間」欄位；recovery_time / duration 由本模組觀測
#    alarmStatus 由「告警中」翻為「已恢復」時記錄（duration = 觀測到的持續秒數）。
# Excel 告警工作表另設定：alarm_id_raw 欄文字格式(@)、凍結標題列、啟用篩選、欄寬自動。

EVENT_FIELDS = [
    "timestamp", "elapsed_seconds", "event_type", "severity", "detail",
]

# 單軸時序圖（Excel report.xlsx「圖表」分頁內嵌，資料來源為「時序數據」分頁）
CHART_SERIES = {
    "soc":     {"title": "SOC 對時間", "y_title": "SOC (%)", "fields": ["soc_percent"]},
    "power":   {"title": "功率對時間（設定 vs 實際）", "y_title": "Power (kW)",
                "fields": ["actual_active_power_kw", "target_power_kw"]},
    "voltage": {"title": "電池電壓對時間", "y_title": "Voltage (V)", "fields": ["battery_voltage_v"]},
    "current": {"title": "電池電流對時間", "y_title": "Current (A)", "fields": ["battery_current_a"]},
}

# 雙軸組合圖（主/次 Y 軸各自單位，不混）；Excel report.xlsx「圖表」分頁內嵌
CHART_COMBOS = {
    "soc_vs_power": {
        "title": "SOC vs Power",
        "primary":   {"fields": ["soc_percent"], "y_title": "SOC (%)"},
        "secondary": {"fields": ["actual_active_power_kw"], "y_title": "Power (kW)"},
    },
    "voltage_vs_current": {
        "title": "Voltage vs Current",
        "primary":   {"fields": ["battery_voltage_v"], "y_title": "Voltage (V)"},
        "secondary": {"fields": ["battery_current_a"], "y_title": "Current (A)"},
    },
}

# ======================================================================
# report.xlsx 分析版：紀錄表欄位（顯示名 → samples 欄位）＋每張圖表定義
#   充放電紀錄 / 充電紀錄 / 放電紀錄 三頁共用同一組欄位，僅依 direction 過濾
# ======================================================================
RECORD_COLUMNS = [
    ("時間", "timestamp"),
    ("elapsed_s", "elapsed_seconds"),
    ("Direction", "charge_discharge_direction"),
    ("PCS狀態", "pcs_status"),
    ("Battery狀態", "battery_status"),
    ("SOC(%)", "soc_percent"),
    ("Voltage(V)", "battery_voltage_v"),
    ("Current(A)", "battery_current_a"),
    ("Power(kW)", "actual_active_power_kw"),
    ("累積Charge(kWh)", "cumulative_charge_energy_kwh"),
    ("累積Discharge(kWh)", "cumulative_discharge_energy_kwh"),
    ("Net Energy(kWh)", "cumulative_energy_kwh"),
    ("Rack最大溫度(°C)", "rack_max_temperature_c"),
    ("Rack最小溫度(°C)", "rack_min_temperature_c"),
]
# 紀錄頁內嵌圖表（顯示名 → 對應 RECORD_COLUMNS 的欄位鍵）；X 軸用「時間」欄
RECORD_CHARTS = [
    ("SOC vs Time", "SOC (%)", "SOC(%)"),
    ("Voltage vs Time", "Voltage (V)", "Voltage(V)"),
    ("Current vs Time", "Current (A)", "Current(A)"),
    ("Power vs Time", "Power (kW)", "Power(kW)"),
]

# ======================================================================
# 自動分析門檻（report.xlsx Analysis / Health）
# ======================================================================
ANALYSIS_POWER_FLUCT_KW = 30.0     # 單一方向內 |功率|(最大-最小) 超過此值 → 功率波動過大
ANALYSIS_SOC_TOL_PERCENT = 0.5     # SOC 方向一致性容差（充電不應下降、放電不應上升）
ANALYSIS_COMM_MIN_RATE = 1.0       # 資料完整率低於此 → 通訊 Warning（1.0＝要求 100%）

# report.xlsx 分頁標籤顏色（openpyxl sheet_properties.tabColor；hex RGB，不影響標題列底色）
TAB_COLORS = {
    "Summary": "548235",      # 綠
    "KPI": "2E75B6",          # 藍
    "Charge & Discharge": "548235",    # 綠
    "Charge": "ED7D31",                # 橘黃 Orange
    "Discharge": "FFC000",             # 黃 Yellow
    "Raw Data": "808080",     # 灰
    "Alarm": "C00000",        # 紅 Red
}
