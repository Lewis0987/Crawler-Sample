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
REPORT_VERSION = "1.1.0"     # 報告產生器版本（輸出檔 report_version）
SCHEMA_VERSION = "1.4.0"     # 輸出資料結構版本（欄位/檔案結構變更時 +1）
# schema 1.1.0：alarms.csv/Excel 告警 ID 調整 —— alarm_id→alarm_id_raw（全程字串、防精度遺失），
#               新增 alarm_code（ALM-YYYYMMDD-NNN，僅供顯示/查找），欄位順序調整。
# schema 1.2.0：Session append/resume —— samples.csv 新增 sample_index /
#               cumulative_charge_energy_kwh / cumulative_discharge_energy_kwh；
#               新增 session_state.json（recording/paused/completed，供續接累積）。
# schema 1.3.0：samples.csv 新增 rack_max_temperature_c / rack_min_temperature_c
#               （來源 overview/rackExtremeValueInformation.rackCellMax/MinTemperature）；
#               紀錄頁整合圖改為 電流 + Rack 最高/最低溫度；Summary 移除 Health。
# schema 1.4.0：新增 Cell 快照（cell_snapshots.json，來源 battery_data_scraper.getPackInformation）；
#               report.xlsx 新增 Cell Volt. / Cell Temp. 兩工作表（Discharge 之後、Raw Data 之前）。
#               快照時機：session_start / charge_start / discharge_start /
#               mode_changed_to_charge / mode_changed_to_discharge / session_end（起訖＋方向切換）。

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

# 離線 --selftest 專用單一測試目錄（位於 output/test_output/ 下）；每次執行前整個清空重建，
# 避免固定 timestamp 撞名累積成 _2/_3…。正式設備報告不會寫入此處。
SELFTEST_SUBDIR = "selftest_latest"

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
FILE_CELL_SNAPSHOTS = "cell_snapshots.json"  # Cell 電壓/溫度歷史快照（session 內累積，供 Cell Volt./Temp. 產表）
# alarm_code 穩定配號 mapping（放輸出根目錄，跨 session/regen 共用）：
#   同一 alarm_id_raw 永遠對應同一 alarm_code；重新產生報告不改號。
FILE_ALARM_CODE_MAP = "alarm_code_mapping.json"

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
SOC_MAX_PERCENT = 99             # SOC 上限；超過 → 建議停止
SOC_MIN_PERCENT = 1             # SOC 下限；低於 → 建議停止
COMM_FAIL_MAX = 3                # 連續通訊失敗次數 → 建議停止（communication_error）
SESSION_TIMEOUT_SEC = 3600       # 單一 session 最長監測秒數 → timeout
# 實際功率長時間偏離設定值
POWER_DEVIATION_KW = 15          # 偏離門檻（|實際 - 設定|）
POWER_DEVIATION_SEC = 30         # 持續超過此秒數才觸發
# 電壓 / 電流門檻（None = 不檢查；依站點填實際上下限）
VOLTAGE_MAX_V = None
VOLTAGE_MIN_V = None
CURRENT_MAX_A = None             # 取絕對值比較
# 告警級別觸發（level 0 = 嚴重）；新增這些級別的告警 → 建議停止
ALARM_STOP_LEVELS = {0}

# 告警級別中文（完全比照 HMI 告警頁 render 規則，來源：前端 index-68732cea.js）：
#   level===0 → serious；level===1 → medium；其他（2,3…）→ slight
# 中文字串取自 zh_TW 語系檔 routes.custom_header.*（serious=嚴重 / medium=一般 / slight=輕微）。
# ⚠️ 不可自行猜測：此為 UI 實際顯示規則，非「級別名稱表」。
ALARM_LEVEL_UI = {"serious": "嚴重", "medium": "一般", "slight": "輕微"}

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
# 僅 Summary 設定自訂顏色；未列於此的工作表一律不設 tabColor → Excel 預設頁籤顏色（白色）
TAB_COLORS = {
    "Summary": "4F81BD",      # 藍（KPI 已併入 Summary 下半部，不再有獨立 KPI 分頁）
    "Charge & Discharge": "548235",   # 綠
    # "KPI": "2E75B6",          # 藍
    # "Charge": "ED7D31",                # 橘黃 Orange
    # "Discharge": "FFC000",             # 黃 Yellow
    # "Cell Volt.": "7030A0",   # 紫（Cell 電壓）
    # "Cell Temp.": "C55A11",   # 深橘（Cell 溫度）
    # "Raw Data": "808080",     # 灰
    # "Alarm": "C00000",        # 紅 Red
}

# ======================================================================
# Cell 快照（Cell Volt. / Cell Temp. 工作表）— 設定集中於此，不寫死於主程式
# ======================================================================
# 是否於取樣關鍵時機擷取 Cell 完整快照（來源 battery_data_scraper.getPackInformation）
CELL_SNAPSHOT_ENABLED = True

# 擷取時機（起訖＋方向切換）；底層每 5 秒取樣照舊，只在這些 reason 擷取 280-cell 完整快照。
# 相同 (timestamp, mode, soc, power, cell_data_hash) 五者皆同 → 視為重複，不建立第二筆。
CELL_SNAPSHOT_REASONS = [
    "session_start", "charge_start", "discharge_start",
    "mode_changed_to_charge", "mode_changed_to_discharge", "session_end",
]
# snapshot_reason → 報表 Header 顯示文字（讓人一眼看出快照來源，而非只有時間）
CELL_SNAPSHOT_REASON_LABEL = {
    "session_start": "Session Start",
    "charge_start": "Charge Start",
    "discharge_start": "Discharge Start",
    "mode_changed_to_charge": "Switched → Charge",
    "mode_changed_to_discharge": "Switched → Discharge",
    "session_end": "Session End",
}

# 期望 Pack 清單（供 validate 檢查是否缺 Pack）。
# None = 動態：不做「缺 Pack」檢查，只依「實際存在的 Pack」排版與驗證
#        （不同站點 Pack 數不同，如本站實測為 14 Pack；不會對不存在的 15~19 報警或顯示 N/A）。
# 若某站需強制固定 Pack 數，改成 list(range(1, N+1)) 即可恢復缺 Pack 檢查。
CELL_EXPECTED_PACKS = None
CELLS_PER_PACK = 20                          # 每 Pack 預期 cell 數（與 battery_data_scraper 一致）

# Pack 版面「偏好順序/位置」樣板；每個子清單為一列，由左到右。
# 實際渲染只排出快照中「實際存在」的 Pack：缺的略過並壓縮該列（不留 N/A 空位）；
# 不在樣板中的 Pack 依序補在最後。
# CELL_PACK_LAYOUT = [
#     [11, 10, 1],
#     [19, 12, 9, 2],
#     [18, 13, 8, 3],
#     [17, 14, 7, 4],
#     [16, 15, 6, 5],
# ]
CELL_PACK_LAYOUT = [
    [1, 2, 3, 4],
    [5, 6, 7, 8],
    [9, 10, 11, 12],
    [13, 14, 15, 16],
    [17, 18, 19],
]
# 每個 Pack 內 Cell 矩陣欄數（20 cell → 5 列 × 4 欄）。Cell 依 packList 原順序由左至右、由上至下填。
CELL_MATRIX_COLS = 4

# 顯示數值格式：Cell Volt. 3 位小數（不轉整數、不省略小數）；Cell Temp. 整數（cell 方格）。
# 統計摘要（Max/Min/Average/Diff）：電壓 3 位小數；溫度 1 位小數（Average 保留 1 位較有意義）。
CELL_VOLT_CELL_FORMAT = "0.000"
CELL_TEMP_CELL_FORMAT = "0"
CELL_VOLT_STAT_FORMAT = "0.000"
CELL_TEMP_STAT_FORMAT = "0.0"

# 兩工作表左側摘要標籤（共用框架、以參數區分）。
CELL_VOLT_SUMMARY_LABELS = {
    "title": "Cell Volt.", "max": "最大電壓", "min": "最小電壓",
    "avg": "平均電壓", "diff": "壓差", "unit": "V",
}
CELL_TEMP_SUMMARY_LABELS = {
    "title": "Cell Temp.", "max": "最高溫度", "min": "最低溫度",
    "avg": "平均溫度", "diff": "溫差", "unit": "℃",
}

# --------------------- 色階 / 門檻（集中管理，可無痛切換）---------------------
# COLOR_MODE = "relative" → 依每筆紀錄自身 Min~Max 相對上色（目前採用，不寫死門檻）。
# COLOR_MODE = "threshold" → 未來填入 CELL_THRESHOLDS 後改走固定門檻（介面已預留）。
CELL_COLOR_MODE = "relative"

# 固定門檻介面（目前 None＝未使用；未來設定後把 CELL_COLOR_MODE 改成 "threshold" 即可切換，
# 不需修改報表產生程式，只走 get_voltage_fill / get_temperature_fill 內的 threshold 分支）。
CELL_THRESHOLDS = {
    "voltage_high": None,   # V，超過→紅
    "voltage_low": None,    # V，低於→紅
    "temperature_high": None,  # ℃，超過→紅
    "temperature_low": None,   # ℃，低於→紅（電壓/溫度分開，不共用）
    "voltage_diff_max": None,      # V，壓差達此值→紅（threshold 模式；None 則走 relative 的 Diff-Gate）
    "temperature_diff_max": None,  # ℃，溫差達此值→紅
}

# 色階等級（由低到高，7 段）＋ no_data；名稱固定，Cell Volt./Temp. 共用「同一組名稱」
# （但數值判斷各自獨立：get_voltage_fill / get_temperature_fill，不共用數值區間）。
CELL_LEVEL_ORDER = ["abnormal_low", "low", "medium_low", "normal",
                    "medium_high", "high", "abnormal_high"]      # t 由小到大對應
CELL_LEVEL_LABEL = {
    "abnormal_high": "異常高值", "high": "偏高", "medium_high": "中高",
    "normal": "正常", "medium_low": "中低", "low": "偏低",
    "abnormal_low": "異常低值", "no_data": "無資料 / N/A",
}

# 色盤（單一集中管理；key → ARGB 十六進位字串）。Cell Matrix / 左側 Summary / Legend 共用同一組。
# ★ 來源：使用者手動填色範本 cell_palette_template.xlsx，由 read_palette_from_excel.py 讀取；
#   theme+tint 者已解析為 Excel 實際顯示 RGB（medium_high=Accent6 較淺80%、no_data=Accent3 較淺80%）。
# ★ 字色（黑/白）由底色亮度自動決定，不更動使用者選定的填滿色。
# ★ ARGB 一律正規化為「FF+後6位」(不透明)，避免 00/FF alpha 前綴造成顯示差異。
CELL_COLOR_PALETTE = {
    "abnormal_low":  "FFFFFF00",   # 黃（使用者選色）
    "low":           "FF99FF33",   # 黃綠
    "medium_low":    "FF66FF33",   # 綠
    "normal":        "FF00FF00",   # 亮綠
    "medium_high":   "FFFDEADA",   # Accent6 較淺80%（theme 解析）
    "high":          "FFFFCCCC",   # 淺紅
    "abnormal_high": "FFFF0000",   # 紅
    "no_data":       "FFEBF1DE",   # Accent3 較淺80%（theme 解析）
}
# 色階啟用門檻（Diff-Gate）：先看該筆 Diff（Max−Min）多大，再決定「可以用哪幾個等級」。
# Diff 很小 → 只給 normal（全綠）；Diff 越大才逐步解鎖更外圈的等級（中高/中低 → 偏高/偏低 → 異常）。
# 每一階 (diff_上限 或 None=以上, 可用等級清單[低→高、對稱、以 normal 為中心])；依序找第一個 diff ≤ 上限者。
# ★ 只影響「數值→等級」的映射範圍，不改 palette/RGB/統計；電壓與溫度各自獨立門檻。
CELL_LEVEL_GATE = {
    "voltage": [                     # 單位 V（已放寬：小壓差幾乎全綠，僅極少數最高值用淡提示色）
        (0.004, ["normal"]),                                                    # Diff ≤ 0.004 → 全綠
        (0.010, ["medium_low", "normal", "medium_high"]),                       # ≤ 0.010 → 大部分綠、少量最高值淡提示
        (0.060, ["low", "medium_low", "normal", "medium_high", "high"]),        # ≤ 0.060 → 加 偏低/偏高
        (None,  ["abnormal_low", "low", "medium_low", "normal",                 # > 0.060 → 開放異常兩端
                 "medium_high", "high", "abnormal_high"]),
    ],
    "temperature": [                 # 單位 ℃
        (2.0, ["normal"]),                                                      # Diff ≤ 2 → 全綠
        (5.0, ["medium_low", "normal", "medium_high"]),                         # ≤ 5 → 中低/正常/中高
        (8.0, ["low", "medium_low", "normal", "medium_high", "high"]),          # ≤ 8 → 加 偏低/偏高
        (None, ["abnormal_low", "low", "medium_low", "normal",                  # > 8 → 開放異常兩端
                "medium_high", "high", "abnormal_high"]),
    ],
}
# normal 中央帶寬（分段時「normal」佔正規化位置 t 的中央比例）：越大→越多 Cell 判為 normal（綠），
# 只有更靠近極值的少數 Cell 才落到外圈等級。避免資料偏高/偏低時整片被判成非 normal。
CELL_NORMAL_BAND_FRAC = 0.8

# Cell Volt.「偏離平均電壓 %」判斷（Summary 最大/最小/平均電壓 + Pack 矩陣每格共用同一套規則）。
# 高側 dev% = (值−平均)/平均×100；低側 dev% = (平均−值)/平均×100（平均格 dev=0 → normal）。
# 比較（dev 四捨五入到 1e-4 去浮點雜訊；0.50 本身仍屬 high/low，僅「>0.50」才 abnormal）：
#   dev < DEV1            → normal
#   DEV1 ≤ dev < DEV2     → medium(_high/_low)
#   DEV2 ≤ dev ≤ DEV3     → (high/low)      ← 含 DEV3=0.50 本身
#   dev > DEV3            → abnormal(_high/_low)
CELL_VOLT_DEV1 = 0.15   # %
CELL_VOLT_DEV2 = 0.30   # %
CELL_VOLT_DEV3 = 0.50   # %
# 偏差 band → 模板 level_key。★ 顏色一律由「單一 palette」CELL_COLOR_PALETTE（＝cell_palette_template.xlsx）提供，
#   不再另存色表 → 保證 Summary 與 Pack Matrix、Cell Volt. 與 Cell Temp.「同一 level_key 完全同色」。
#   高側→暖端（medium_high/high/abnormal_high）；低側→冷端（medium_low/low/abnormal_low）；normal 共用。
CELL_VOLT_DEV_LEVELS = {
    "high": {"normal": "normal", "medium": "medium_high", "strong": "high", "abnormal": "abnormal_high"},
    "low":  {"normal": "normal", "medium": "medium_low", "strong": "low", "abnormal": "abnormal_low"},
}

# 顏色說明（Legend）：放在左側摘要「當前功率」下方；等級順序由 CELL_LEVEL_ORDER 反轉（高→低）＋ no_data。
CELL_LEGEND_TITLE_RELATIVE = "顏色說明（相對色階）"
CELL_LEGEND_TITLE_THRESHOLD = "顏色說明（固定門檻）"

# 最上方模式分類區塊：Discharge 黃底、Charge 藍底（標題列跨越該模式所有紀錄區塊）。
# Standby 用於 session 起訖當下無明確充/放電狀態者（不硬歸類成 Charge/Discharge）。
CELL_MODE_BAND = {
    "discharge": {"label": "Discharge", "bg": "FFC000", "font": "000000"},   # 黃底黑字
    "charge":    {"label": "Charge",    "bg": "2E75B6", "font": "FFFFFF"},   # 藍底白字
    "standby":   {"label": "Standby",   "bg": "808080", "font": "FFFFFF"},   # 灰底白字
}
CELL_BAND_ORDER = ["discharge", "charge", "standby"]   # 由上而下排列（無資料的區塊不產生）

# Cell 工作表版面 / 列印
# Cell Volt./Temp. 兩表所有字體在「目前實際大小」上的加減量（pt）；正=放大、0=不變。
# 以「現值 +Δ」套用（不重指定固定字級），保留各元素大小比例；欄寬/列高/框線/底色/置中/色彩不變。
CELL_SHEET_FONT_DELTA = 2
CELL_SHEET_ZOOM = 80                 # 檢視縮放（70~85）
CELL_SHEET_PAGE_ORIENTATION = "landscape"
CELL_VOLT_COL_WIDTH = 8.5            # Cell Volt. 矩陣欄寬（3 位小數，較寬；加大 Cell 顯示空間）
CELL_TEMP_COL_WIDTH = 6.5            # Cell Temp. 矩陣欄寬（整數，較窄；加大 Cell 顯示空間）
CELL_SUMMARY_LABEL_WIDTH = 5         # 左側摘要第 1 欄（統計標籤/數值、Legend 色塊）
CELL_SUMMARY_VALUE_WIDTH = 16        # 左側摘要第 2 欄（Legend 說明文字，需容中文）
