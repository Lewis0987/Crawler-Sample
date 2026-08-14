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
# 智慧排程自動建立報告（監看層開關；由 device_control_menu 的監看層讀取）
# ----------------------------------------------------------------------
# ⚠️ 這兩個開關**只影響「自動」建立報告**，完全不影響：
#      - Menu 18/19 手動開始/停止報告
#      - Menu 4/5/6/7 手動控制後的自動跟隨（report_on_charge_discharge_control）
#      - 既有的 fault_stop / communication_error 自動結束
# ======================================================================
# False（預設）：Dashboard 仍每 15 秒讀取並顯示監看狀態，auto_schedule_check() 照常判斷
#                並印出 Debug，但**不建立任何 ReportSession**（實機唯讀觀察階段用）。
# True         ：條件全部成立時才允許自動建立報告（實機唯讀觀察通過後再由使用者手動改為 True）。
AUTO_SCHEDULE_REPORT_ENABLED = True

# True ：每次刷新都印一行完整監看 Debug（實機唯讀觀察階段用，可看見每 15 秒確實在更新）
# False：只在監看狀態「改變」時才印（日常使用，避免洗畫面）
AUTO_MONITOR_DEBUG = True

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

# ======================================================================
# 電表報告（ESS_Meter_Report.xlsx）— 獨立檔案，與 report.xlsx 並存
# ======================================================================
# 資料源與 report.xlsx 完全不同，刻意分成兩個檔案：
#   report.xlsx            PCS/BMS API 遙測，5 秒取樣，kWh 由 kW 梯形積分而來
#   ESS_Meter_Report.xlsx  電表 Log，1 秒取樣，kWh+/kWh- 為電表累計器直讀值
# 兩者不混在同一份 Summary，避免「積分值」與「電表值」被誤讀為互相驗證過。
# report.xlsx 的 Summary 只放一個 Meter Report 參照區塊（檔名/CSV/筆數/起訖時間），不放 KPI。
FILE_METER_XLSX = "ESS_Meter_Report.xlsx"
FILE_METER_CSV = "meter_log.csv"        # session 資料夾內的正規檔名
METER_CSV_GLOB = "meter*.csv"           # 也接受保留原始檔名的 meter Log（保留可追溯性）

# 模板位置（相對專案根）；只由 tools/make_ess_report_template.py 重建，正式流程只讀不寫
METER_TEMPLATE_SUBDIR = "templates"
METER_TEMPLATE_NAME = "ESS_Report_Template.xlsx"

# 模板內的工作表／範圍（與 templates/ESS_Report_Template.xlsx 一致）
METER_SHEET_SUMMARY = "Summary"
METER_SHEET_RAW = "Raw Data"
METER_SHEET_POWER = "Power Trend"
METER_SHEET_ENERGY = "Energy Counter"
METER_SHEET_COMBO = "Power + Energy"
METER_TABLE_NAME = "RawData"
METER_CHART_COL_FIRST = 9               # I 欄：降採樣後的 Chart Timestamp/kW/kWh+/kWh-
METER_CHART_ROW_FIRST = 3

# 圖表降採樣：Excel 二維圖表單一 series 上限 32,000 點，完整 meter Log 會超過。
# 採 min/max per bucket（保留首筆、末筆與每個 bucket 的 kW 極值），不是固定每 N 筆抽樣。
# Raw Data 一律保留完整資料；KPI 一律以完整資料計算，不使用降採樣結果。
METER_CHART_POINTS = 4000

# Y 軸 padding（資料跨距的百分比，再向外對齊整數刻度）。
# 不用 Excel Auto Scale：跨距低於最大值約 1/6 時 Excel 會把最小值吸附到 0，
# 使 ~90,000 的累計電量曲線被壓成水平直線。
METER_PAD_POWER = 0.10                  # kW 軸：跨距 ±10%
METER_PAD_ENERGY = 0.05                 # kWh 軸：跨距 ±5%

# 數值格式
METER_NF_DT = "yyyy-mm-dd hh:mm:ss"
METER_NF_KW = "#,##0.00"
METER_NF_COUNTER = "#,##0.0"

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
    "battery_off": "電池下電",            # 背景偵測電池已下電（原落到 pcs_stop，語意不精確）
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

# ======================================================================
# 自動結束（should_auto_end）—— 唯一停止決策點所使用的門檻與白名單
# ----------------------------------------------------------------------
# ⚠️ should_auto_end() 是全專案唯一的自動停止判定入口。
#    Menu / Dashboard / 背景取樣器都不得自行判斷是否結束報告。
# ======================================================================
# 排程結束後是否自動收尾（背景取樣器依 should_auto_end() 的判定 finalize）。
# **True = 正式產品預設**：智慧排程自動建立報告，並於排程結束自動 finalize、產出 Summary/Excel。
# False 僅供 Phase 3 開發／實機驗證／問題除錯時暫時關閉 —— 此時仍會照常判定並顯示
# STOP_PENDING 與 idle 倒數，只是不收尾，需人工以 Menu 19 結束。
AUTO_STOP_ENABLED = True

# ----------------------------------------------------------------------
# 去抖動（Debounce）與冷卻門檻 —— **全部集中於此，其他模組一律以 CFG. 引用**
#
# ⚠️ 不得在 device_control_menu.py 或任何地方另行宣告同名常數。
#    （曾發生 menu 自行宣告 AUTO_HOLD_STOP_SEC = 30、與此處生效值 60 矛盾的情況）
#
# ⚠️ 三者一律以 time.monotonic() 計算「實際經過時間」，**不是**取樣/刷新次數：
#      - Debounce 的門檻與累積值完全與 UI 解耦
#      - Dashboard / （未來）Auto Monitor Service 只決定「多久呼叫一次
#        auto_schedule_check()」，不影響 Debounce 的計算方式
#      - 未來改由 Service 以更短週期呼叫時，Debounce 邏輯一行都不需要改，
#        只是「判斷發生的時機」變密集、延遲變短
#
# 狀態流程（Phase 3.4 起固定）：
#   IDLE → START_HOLD → START_PENDING → RUNNING → STOP_PENDING → AUTO_STOP
#        → COOLDOWN → IDLE
# ----------------------------------------------------------------------
# charge/discharge 需**持續**多少秒才建立 Session（Start Debounce）。
# 目的：濾掉設備旗標的瞬時抖動，避免誤建報告。
# 取 15 秒的理由：足以擋掉單次誤判，且相較 30 秒可少漏約 15 秒的排程開頭資料。
AUTO_HOLD_START_SEC = 15

# idle 需**持續**多少秒才視為「已離開充放電」→ 以 auto_stop 結束（Stop Debounce）。
# 只負責去抖動（濾掉旗標瞬時跳動與充↔放切換之間的短暫 idle），
# **不負責**判斷排程是否真的結束（那是 Phase 3.5 的排程時段感知）。
# 60 秒經 2026-08-06 實機驗證恰當：充電中兩次短暫 idle（27s / 16s）被吸收，
# 真正結束時 idle 持續 61 秒觸發收尾。
AUTO_HOLD_STOP_SEC = 60

# ----------------------------------------------------------------------
# 排程感知（Schedule Context）—— Phase 3.5
#
# schedule_ctx 由監看層計算後傳入 should_auto_end()，內容：
#   {"in_window": bool, "current_plan": str|None,
#    "next_plan_in_sec": float|None, "source": "ok"|"unknown"}
# 決策仍 100% 留在 should_auto_end()；監看層只負責把 ctx 算出來。
# schedule_ctx=None 或 source="unknown" → **完全退回 Phase 3.4 行為**。
# ----------------------------------------------------------------------
# 「下一段啟用排程即將開始」的門檻秒數：idle 已達門檻、但下段排程在此秒數內就要開始時，
# 不收尾 —— 避免相鄰排程（如 Charge 17:00~17:50、Discharge 17:55~18:10）
# 被拆成兩份報告而失去往返效率（RTE 只能在同一 Session 內計算）。
#
# 語意（Case A：下一排程**尚未**開始）：
#   前一排程結束 → 下一排程 nominal start 之間，允許的最大「排程間隔」。
#   對應 ctx 的 next_plan_in_sec（未來式倒數）。
#
# 0 = 停用 → should_auto_end() 的 Case A 完全跳過（退回 Phase 3.4 行為）。
# 180：涵蓋 2026-08-10 實機觀察的相鄰排程間隔（16:00~16:10 discharge → 16:11~16:21 charge，
#      間隔 60s）。刻意不放大 AUTO_HOLD_STOP_SEC —— 那會讓**每一份**報告都延後收尾。
AUTO_NEXT_PLAN_GAP_SEC = 180

# 「新排程 nominal start 已到、但 PCS/API 狀態尚未追上」的最大等待秒數。
#
# 語意（Case B：下一排程**已經**開始，但設備還沒動）：
#   與 AUTO_NEXT_PLAN_GAP_SEC 是**兩個不同的概念，不可混用**——
#     AUTO_NEXT_PLAN_GAP_SEC        ：排程與排程之間的「時刻表間隔」（未來式）
#     AUTO_SCHEDULE_CONTINUITY_GRACE_SEC：排程已開始後，等待設備狀態反映的「執行延遲」（過去式）
#
# 為何需要：排程在**設備端**執行，nominal start 到實際出現 charge/discharge 旗標之間，
#   要經過 排程觸發 → API → PCS 命令 → PCS 狀態改變 → 資料刷新 → 監看偵測 的完整鏈路。
#   2026-08-10 實機：charge 排程 16:11 開始，Session 至 16:13:37 才偵測到 charge（延遲 157s）。
#
# 這同時也是 continuity 的**逾時上限**：超過此秒數仍未出現充/放電 → 正常 finalize，
#   Session 不會因為「等下一段排程」而無限期卡住。
AUTO_SCHEDULE_CONTINUITY_GRACE_SEC = 180

# 排程窗口比對的寬限秒數（前後各放寬）。
# 排程在**設備端**執行，但比對用的是 PC 時間；2026-08-07 實測設備 dataCollectTime
# 較 PC 慢約 72 秒，故在窗口邊界前後各留 120 秒緩衝，避免邊界瞬間判斷相反。
# 刻意不直接依賴 dataCollectTime（該值本身是「上次採集時間」，另有落後與失敗風險）。
AUTO_WINDOW_GRACE_SEC = 120

# 排程清單的快取秒數。背景取樣器每 5 秒一輪，若每輪都查會造成不必要的 API 負擔；
# 且僅在 idle 開始累積（idle_held_seconds() > 0）時才查，idle=0 時完全不呼叫 API。
AUTO_WINDOW_CACHE_SEC = 60

# ----------------------------------------------------------------------
# 孤兒 Session 防護（Phase 3.6）
#
# 孤兒＝session_state.json 停在 recording，但已無任何行程在取樣。
# 實測兩次孤兒（2026-08-04）成因相同：行程**正常結束**但未走 main() 的 finally
# （驗證腳本、任何 import device_control_menu 的工具都會如此），
# 背景 daemon thread 被直接終止 → 狀態永遠停在 recording。
#
# 對策分兩層：
#   ① atexit 保底：行程正常結束時把 Session 標記為 paused（只寫 JSON，不重產報表、
#      不做網路 I/O）→ 最差情況從「recording 孤兒」變成「paused 可續接」。
#   ② 啟動時分類：以 last_sample_time 與現在的間隔判斷是否為孤兒，**只記錄不改變流程**。
#
# ⚠️ Phase 3.6 刻意**不**加入「孤兒 + idle → 自動 finalize」與「Exit 時 finalize」。
#    先把孤兒來源堵住（atexit），觀察是否仍有實際案例，再決定是否需要更積極的回收。
# ----------------------------------------------------------------------
# 判定「孤兒」的斷線間隔門檻（秒）：last_sample_time 距現在超過此值即記 orphan_detected。
# **僅供分類與記錄，不改變 Resume 流程**（超過門檻仍照常續接）。
# 300 秒 = 背景取樣間隔(5s) 的 60 倍，足以排除短暫卡頓造成的誤判。
ORPHAN_GAP_SEC = 300

# atexit 保底等待背景取樣執行緒結束的最長秒數。
# 必須夠短 —— atexit 期間不可讓行程退出被長時間阻塞（背景執行緒為 daemon，
# 逾時未結束也會隨行程一起被回收，狀態已先寫入故不影響資料）。
ATEXIT_JOIN_TIMEOUT_SEC = 2

# ----------------------------------------------------------------------
# Monitor Ownership（Phase 4.4）
# ----------------------------------------------------------------------
# 跨行程互斥：同一時間只有一個行程可以驅動自動報告生命週期並寫入 Session。
#
# ⚠️ 互斥的**唯一依據**是 Windows 命名 Mutex。
#    MONITOR_OWNER_FILE 只是診斷資訊（誰持有、何時取得），
#    **絕不可**用它的 pid 或存在與否來判斷所有權 —— PID 會被回收、檔案會殘留。
#
# 命名空間用 Global\：Phase 4.6 的 Windows Service 跑在 session 0，
# Dashboard 跑在使用者 session，Local\ 無法跨 session 互斥。
# 名稱附加 output_root 的雜湊，讓同機的不同部署不互相搶鎖。
MONITOR_MUTEX_PREFIX = "Global\\ESS_AutoMonitor_Owner_v1_"
MONITOR_OWNER_FILE = "monitor_owner.json"

# Service 取不到所有權時的 exit code（供 Phase 4.6 的服務管理員判讀）
#   3 = 預期的競爭結果，**不是程式故障**，不應觸發自動重啟
#   4 = ownership API / 系統異常（fail closed），需人工介入
EXIT_OWNER_HELD_BY_OTHER = 3
EXIT_OWNER_API_ERROR = 4

# ----------------------------------------------------------------------
# Finalize 輸出重試（Phase 3.7）
# ----------------------------------------------------------------------
# Excel / Chart / Meter 產出失敗最常見的原因是「檔案正被 Excel 開啟而鎖定」，
# 這是暫時性的 —— 給有限次重試即可，不需要人工重跑整份報告。
#
# ⚠️ 只重試「檔案占用」類失敗（OSError / PermissionError，或既有函式以 False 表示的存檔失敗）。
#    程式邏輯錯誤（TypeError / KeyError / AttributeError…）第一次就放棄：重試不會成功，
#    只會拖慢收尾（自動收尾發生在背景執行緒，不應無謂延長）。
FINALIZE_RETRY_MAX = 3           # 總嘗試次數（含第一次）；1 = 不重試
FINALIZE_RETRY_WAIT_SEC = 2.0    # 每次重試前等待秒數（最壞情況 = (MAX-1) × 此值 × 輸出項數）

# output_status 的值（寫入 session_state.json；**不寫入 summary.json**，避免變更 Summary schema）
OUTPUT_OK = "ok"                      # 產生成功
OUTPUT_SKIPPED = "skipped"            # 非錯誤而略過（無 openpyxl / 無電表 Log / 無模板）
OUTPUT_FAILED_LOCKED = "failed_locked"  # 檔案占用，重試耗盡仍失敗
OUTPUT_FAILED = "failed"              # 其他錯誤（未重試）

# ----------------------------------------------------------------------
# 排程項目方向 enum（**全專案唯一定義**）
# device_control_menu.py 與 phase2_real_trigger_validation.py 一律引用此處，
# 不得各自維護副本（Phase 3.6 / D-1 整併）。
# 對照 HMI 前端：1=充電 / 2=放電 / 3=不充不放。
# 註：menu 另有中文版 _SCHED_CD（僅供 TUI 顯示），與此表為同一組 enum 的兩種呈現。
# ----------------------------------------------------------------------
SCHED_CD_CODE = {1: "charge", 2: "discharge", 3: "none"}


def as_int(v):
    """
    enum 值正規化為 int（**全專案唯一定義**）：`"2"` → `2`；bool/None/非數值 → `None`。

    為何需要：後端若把 chargeOrDischarge 等 enum 回成字串，直接查 int-key 對照表會漏判
    （安全閘會把「放電」誤判成 unknown 而放行）。一律先轉型再查表。
    bool 明確排除 —— Python 中 bool 是 int 子型別，True 會被 int() 轉成 1（＝充電）。

    註：本檔為純設定，此處僅有一個無外部相依的純函式，不含任何 API 呼叫。
    """
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# 自動收尾（auto_stop）後的冷卻秒數；期間**一律不得建立新 Session**。
# 設 60 秒（與 Stop Debounce 對稱）的理由：
#   1. 避免設備停止後旗標短暫抖動，造成剛收尾又立刻建立第二份 Session
#   2. 避免充↔放切換瞬間（charge→discharge / discharge→charge）被誤判為新的一輪
#   3. 避免 Dashboard 與未來的 Auto Monitor Service 同時偵測時發生重複建立
# 流程：Stop → idle 60s → Finalize → Cooldown 60s → 才允許再次建立
AUTO_COOLDOWN_SEC = 60

# should_auto_end() 的 session 時間上限（秒）；**0 = 不限**。
# 設 0 的理由：排程可能長達數小時（實測有 11:40~18:00＝6.3 小時的充電排程），
# 若沿用 SESSION_TIMEOUT_SEC(3600) 會把長排程的報告砍成一小時。
# 客戶若需要上限（2/4/8 小時等），直接改此值即可，timeout 機制完整保留。
AUTO_END_TIMEOUT_SEC = 0

# **白名單**：只有這些 stop_reason 會觸發自動結束。
# 其餘 critical 條件（alarm_stop / soc_over_max / soc_under_min …）一律
# 「只記錄事件與 Summary 標示，不結束 Session」：
#   - alarm_stop     ：設備既有告警在 resume 時會被一次性記為新增，不應中斷記錄
#   - soc_over_max   ：充電排程充飽（SOC 達上限）是**正常完成**，應走 idle → auto_stop
#   - soc_under_min  ：放電排程放到下限同理
# 採白名單而非黑名單：日後新增 critical 條件不會意外造成 Session 被提前 finalize。
AUTO_END_STOP_REASONS = ("communication_error", "pcs_fault", "battery_off")

# stop_reason → end_reason 對照（統一命名，避免同一種狀況出現兩種結束原因）。
# 例：pcs_fault 原本會落到 pcs_stop，與 Menu 的 fault_stop 不一致 → 統一為 fault_stop。
AUTO_END_REASON_MAP = {
    "communication_error": "communication_error",
    "pcs_fault": "fault_stop",
    "battery_off": "battery_off",
}

# 連續判定為 idle / PCS 停止的取樣「次數」。
# ⚠️ 已**不再**作為自動結束的觸發依據（改由 AUTO_HOLD_STOP_SEC 時間制判定）；
#    保留供 Debug / 統計 / 驗證使用，請勿再拿它做停止判斷。
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
