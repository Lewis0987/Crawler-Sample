# PCS 充放電報告 — 設計與資料來源盤點報告（第一階段）

> 狀態：**設計階段**。本文件僅盤點與規劃，**未修改任何控制 API、未觸發任何充放電**。
> 所有 API 皆以既有 GET 唯讀來源驗證。等你確認後才進入實作。
> 產出日期基準：2026-07-16。

---

## 0. 一句話結論

報告模組可以**完全建立在既有 GET 唯讀端點上**，不需要新的控制權限。
最關鍵的發現有兩點，實作前務必先確認：

1. **PCS 實際有功功率不必依賴會 403 的 authed 端點** —— 免登入的 `envCon/pcs`
   （`totalActivePowerOfAcBus`）就能拿到，且同源還有充放電狀態、無功、當日電量、直流側資料。
2. **「控制設定值」與「量測遙測值」的正負號相反**（見 §3），這是整個報告最大的陷阱，
   `normalize_*` 一定要以「量測值慣例」為準，不可沿用控制設定值的符號。

---

## 1. 現有程式與可重用資產盤點（第十三點）

程式都在 `test/`，輸出都在專案根的 `output/`。共用登入與 GET 都集中在 `api_client.py`。

| 檔案 | 角色 | 報告可重用的東西 |
|---|---|---|
| `api_client.py` | 共用 HTTP client | `ApiClient` 單一 session、`login_hmi()`、`get()`/`get_many()`、封包解封 `unwrap()`（code 0/200→data）。**登入一次重複使用**。 |
| `device_control_scraper.py` | 設備狀態唯讀解析 | `battery_power_state_from_dodi()`、`battery_flow_state()`、`parse_pcs_modes()`、`get_pcs_current_status()`、`_flatten_metrics()`、`_fmt_metric()`、`parse_pcs_grid_mode()`、`parse_pcs_power_control_mode()`。**開始/結束狀態幾乎全部可直接呼叫**。 |
| `dashboard_scraper.py` | 概覽解析 | `_flatten_metrics()`、`_fmt_metric()`、`_zh()`、`PCS_GROUPS`（PCS 有功/無功/直流欄位對照）、`summarize_pcs()`、`summarize_battery()`。 |
| `battery_data_scraper.py` | 電芯層級 | `getPackInformation`（pack/cell 明細）。報告主流程用不到，選配（電芯極值）才需要。 |
| `alarm_records_scraper.py` | 告警 | `fetch_all_alarm_records()`、`build_alarm_rows()`、`_epoch_ms_to_text()`、`_alarm_status_text()`、`LEVEL_MAP`、`ALARM_CODE_MAP`。**告警取樣/去重可直接用**。 |
| `device_control_operator.py` | 單一控制 + 回讀 | 控制端點與 `PCS_CONTROL_MODES`（含**正負號定義**）、`run()` 的回傳 record 結構。報告**只讀取它的產出，不改它**。 |
| `device_control_menu.py` | 互動選單 | 整合點（§8）。`refresh_status_silent()`、`_execute_live()`、`wait_and_verify()`。 |
| `run_all.py` | 批次執行 | 只做 orchestration，可仿照它的 task 註冊方式（選配）。 |

**共用連線事實**：`BASE_URL = http://192.168.128.110:8080/admin-api`，芋道封包 `{code,data,msg}`，
`code ∈ {0,200}` 為成功。`envCon/*` 回 list（含 `metricsDataVoList`：`{mark,value,unit,oldValue}`），
`overview/*` 回扁平 dict（`key` + `<key>Unit`）。

---

## 2. 欄位 × API × mark 對照表（第十五點 1、3、4）

以下皆為 **GET 唯讀**。標「guest」者免登入；標「authed」者需 `login_hmi()`。

### 2.1 電池 SOC / 電壓 / 電流 —— `overview/mainControlCollectsInformation`（guest）

`GET /hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation`

| 報告欄位 | key | 單位 key | 實測值（放電快照） |
|---|---|---|---|
| SOC | `rackSoc` | `rackSocUnit`(%) | 14 |
| 電池總電壓 | `rackTotalBatteryVoltage` | `rackTotalBatteryVoltageUnit`(V) | 892.2 |
| 電池總電流 | `rackElectricCurrent` | `rackElectricCurrentUnit`(A) | **-23.2** |
| SOH | `rackSoh` | — | 99 |

> 電流可為負值。此快照為「放電」→ 電流負。詳見 §3。

### 2.2 PCS 有功/無功/直流/當日電量/狀態 —— `envCon/pcs`（guest，**免登入**）

`GET /hmiGuest/unauthorizedAccess/envCon/pcs` → `data[0].metricsDataVoList[*]`，以 `mark` 取值。

| 報告欄位 | mark | 單位 | 實測 value（放電快照） | oldValue |
|---|---|---|---|---|
| **PCS 實際有功功率（主來源）** | `totalActivePowerOfAcBus` | kW | **-20.20** | -202 |
| 無功功率 | `totalReactivePowerOfAcBus` | kVar | 0.00 | 0 |
| 視在功率 | `totalApparentPowerOfAcBus` | kVA | 20.10 | 201 |
| 可用有功容量 | `availableActivePowerCapacity` | kW | 160.00 | 1600 |
| 當日充電電量 | `dailyChargedEnergyThroughAcPort` | kWh | 0 | 0 |
| 當日放電電量 | `dailyDischargedEnergyThroughAcPort` | kWh | 0 | 0 |
| 直流輸入電壓 | `dcInputVoltage` | V | 888.40 | 8884 |
| 直流電流 | `dcCurrent` | A | -24.00 | -240 |
| 直流功率 | `dcPower` | kW | -21.30 | -213 |
| PCS 啟停 | `systemOnOrOffStatus` | — | running (oldValue=1) | 1 |
| PCS 充電中 | `systemChargingStatus` | — | false (oldValue=0) | 0 |
| PCS 放電中 | `systemDischargingStatus` | — | discharging (oldValue=1) | 1 |
| 併網/離網 | `systemGridTiedStatus` | — | gridTied | 1 |
| 故障狀態 | `systemFaultStatus` | — | normal | 0 |
| 告警狀態 | `systemAlarmStatus` | — | normal | 0 |
| 調度/控制模式 | `controlMode` | — | remote | 2 |

> **重要**：每個 mark 有兩個值 —— `value`（已縮放為小數＋單位、狀態值可中文化）與
> `oldValue`（原始整數，數值型通常 = value × 10）。
> - 數值欄位（功率/電壓/電流）：報告採用 `value`（float）＋ `unit`，`oldValue` 存為 raw 備查。
> - 狀態旗標（charging/discharging…）：以 `oldValue == "1"` 判斷開/關（對齊前端 `pcsMode_US.vue`）。
>
> **這一條直接解決 `device_control_operator.py` 註記的痛點**：authed
> `/client/dynamic/dataOrControl/pcs` 對本帳號可能 403、拿不到 active power；
> 但 guest `envCon/pcs` 免登入就有 `totalActivePowerOfAcBus` 與充放電狀態。

### 2.3 PCS 五種模式/狀態 —— 沿用 `device_control_scraper`（authed）

- `PCS當前狀態`：`get_pcs_current_status(client)`（需 `Accept-Language: zh-TW` 取中文 badge）
- `PCS控制模式`（智慧/手動）：`getRunMode` → `GET /client/dynamic/dataOrControl/pcs/getRunMode`
- `PCS排程開關`：`getScheduleSwitch.schedulePlanSwitch` → `GET /schedule/config/getScheduleSwitch`
- `PCS手動模式開關`：`getScheduleSwitch.manualModeSwitch`
- `PCS工作模式`（併網/離網）：`envCon/pcs` 的 `systemGridTiedStatus`/`systemOffGridStatus`
- `PCS功率控制模式`（交流有功/直流恆流/直流恆功率/離網交流電壓）：`parse_pcs_power_control_mode()`

→ 一次呼叫 `parse_pcs_modes(runmode, schedule, pcs_raw)` 即可拿到 4 個機器語意欄位，
不需要報告自己重寫判斷。

### 2.4 電池上下電狀態 —— `getDOAndDIMsg`（authed）

`GET /can/v1/getDOAndDIMsg` → `battery_power_state_from_dodi(dodi)`
→ 已上電 / 已下電 / 切換中 / 未知（依主正/主負接觸器反饋）。

### 2.5 告警 —— `alarm/list`（guest）

`GET /hmiGuest/unauthorizedAccess/alarm/list?pageNo=1&pageSize=100`（分頁抓全）→ `{total, rows}`。

實測 row 欄位（每筆）：

| 欄位 | 意義 | 對照 |
|---|---|---|
| `id` | 告警唯一鍵 | **去重主鍵** |
| `deviceId` | 設備 id | — |
| `typeMark` | 設備類別 | `device.type.pcs`→PCS、`device.type.bcu`→BCU |
| `targetMark` | 告警對象/項目 | i18n key |
| `val` | 告警內容 | i18n key |
| `level` | 級別 | **0=嚴重、1=一般** |
| `alarmStatus` | 狀態 | **True=告警中/未恢復、False=已恢復** |
| `alarmTime` | 告警開始 | epoch ms |
| `createTime` | 建立 | epoch ms |
| `alertContent` | 內容文字 | 可能為 null |
| `thresholdId`/`condition`/`operator`/`triggerVal`/`intervalTime` | 門檻資訊 | 備查 |

> 恢復時間欄位目前 row 中沒有直接欄位（`alarm_records_scraper` 已註記 TODO）；
> 只能用 `alarmStatus` 判斷是否已恢復。

---

## 3. 充放電方向與正負號（第六點）—— ⚠️ 最重要

**存在兩套方向相反的符號系統，切勿混用：**

### A. 控制設定值（送給 PCS 的 setpoint）
來源：`device_control_operator._build_pcs_payload()` →
`setval = -v if direction == "charge" else v`（三種模式 `direction:"sign"`）。

> **充電 = 負值，放電 = 正值**（`activePowerSetPoint` / `dcCurrentSetPoint` / `dcPowerSetPoint`）。

### B. 量測遙測值（讀回來的實際值）
實測放電快照（`systemDischargingStatus=1`、`systemChargingStatus=0`）：

- 電池電流 `rackElectricCurrent = -23.2 A`（負）
- PCS 有功 `totalActivePowerOfAcBus = -20.2 kW`（負）
- 直流功率 `dcPower = -21.3 kW`（負）

且既有 `battery_flow_state()` 判斷：`power = V×A/1000`，`>0→充電`、`<0→放電`。

> **充電 = 正值，放電 = 負值**（量測慣例，與設定值恰好相反）。

### 結論：方向判斷「以 PCS 狀態旗標為最高優先，Power 僅作 fallback」（定案）

> ⚠️ 更新（實機驗證後定案）：待機時 PCS 常有殘餘自耗功率（實測 -1.3kW），
> 若只看 Power 正負號會把待機誤判成放電、與 UI/設備狀態不一致。
> 故 **方向判斷改以 PCS 充/放電狀態旗標為準**，Power 正負號僅在旗標缺失時 fallback。

判斷順序（`resolve_direction()`）：

| 條件 | `charge_discharge_direction` | `direction_source` |
|---|---|---|
| `systemChargingStatus = true` | `charge` | `systemChargingStatus` |
| `systemDischargingStatus = true` | `discharge` | `systemDischargingStatus` |
| 兩者皆 `false` | `idle` | `pcs_status_flag` |
| 旗標不存在 / None / 解析失敗 | 依 Power 正負號 | `power_fallback` |

- 旗標來源：`envCon/pcs` 的 `systemChargingStatus` / `systemDischargingStatus`（以 `oldValue` 1/0 判斷）。
- **Power 正負號僅作 fallback**（`normalize_power_direction`：>+IDLE_KW 充、<−IDLE_KW 放、否則 idle，量測慣例充正放負）。
- 新增 `direction_source` 欄位，明確標示方向由旗標或 Power fallback 判定，便於除錯與維護；
  未來韌體若有更精確欄位，只需改此判斷、不影響 Energy 計算。
- **Energy／累積電量／功率統計一律依實際 Active Power 梯形積分，與 direction 解耦**；
  即使 `direction = idle`，待機自耗電仍持續被積分記錄（不會因 idle 而停止計算）。
- 仍同時保留 `raw_active_power` / `normalized_active_power`、`raw_current` / `normalized_current` 供追查。
- `IDLE_KW` 沿用既有 `_BATTERY_IDLE_KW = 0.1`（於 config）。

> 本方向邏輯為**唯讀分析**，不修改 `device_control_operator/scraper/menu` 任何控制程式。

### 驗證狀態（實機唯讀，純 GET）
- ✅ 已驗證（待機）：`systemChargingStatus=false`、`systemDischargingStatus=false`
  → `direction=idle`、`direction_source=pcs_status_flag`；Power=-1.3kW 未誤判為放電。
- ✅ 已驗證（sign 慣例）：今早 discharge 快照 `rackElectricCurrent=-23.2A`、
  `totalActivePowerOfAcBus=-20.2kW`（`systemDischargingStatus=1`）→ 放電為負值。
- ✅ 已驗證：SOC/電壓/電流/PCS 有功/工作模式/告警 與原始 GET（同 UI/Dashboard 來源）一致。
- ⏳ 待設備進入實際充/放電後確認雙向：
  - 充電：`systemChargingStatus=true` → `direction=charge` / `source=systemChargingStatus`
  - 放電：`systemDischargingStatus=true` → `direction=discharge` / `source=systemDischargingStatus`
- 「實際功率」採 PCS `totalActivePowerOfAcBus` 為主、`V×A` 為輔（已定案）。

---

## 4. 電量與效率計算（第七、八點）

### 4.1 電量（梯形積分）
```
dt_h = elapsed_seconds / 3600
energy_kwh += (prev_power_kw + curr_power_kw) / 2 * dt_h
```
分開累積，不混絕對值：

- `charged_energy_kwh`：僅累積 `normalized_power > 0` 的區段
- `discharged_energy_kwh`：僅累積 `normalized_power < 0` 的區段（取正值累加）
- `net_energy_kwh = charged - discharged`

「實際功率」預設用 PCS `totalActivePowerOfAcBus`；同時算 `calculated_power_kw = V×A/1000` 作輔助比對欄位（不作為主電量）。

**交叉比對（選配）**：記錄 session 起訖時 `dailyChargedEnergyThroughAcPort` /
`dailyDischargedEnergyThroughAcPort` 的差值，與積分結果對照。
但**不可拿當日累計當 session 電量**（每日歸零、全站共用、跨 session 汙染）。

### 4.2 效率
- 預留 `charge_energy_kwh` / `discharge_energy_kwh` / `round_trip_efficiency_percent`。
- **僅當**同一 session 內同時具備完整充電與放電循環才算：
  `round_trip = discharge / charge × 100`。
- 資料不足 → `"N/A（缺少完整充放電循環）"`。單次放電**禁止**硬算效率。

---

## 5. 檔案架構（第二點）

```
test/
├── charge_discharge_report.py          # 報告主程式（session/取樣/統計/輸出）
├── charge_discharge_report_config.py   # 全部門檻與設定（不寫死）
└── (重用) api_client / device_control_scraper / dashboard_scraper / alarm_records_scraper

output/
└── charge_discharge_reports/
    └── 20260716_103000_discharge/
        ├── summary.json
        ├── samples.csv
        ├── alarms.csv
        └── report.xlsx
```

**主程式模組職責（初步）**：
- `ReportSession`：`session_id`、目錄建立、開始/結束狀態快照、統計累積器。
- `sample_once(client)`：一次取樣（呼叫 §2 各 GET，組出一筆 sample dict）。
- `AlarmTracker`：以 `id` 去重、只記新增告警。
- `SafetyMonitor`：依 config 門檻判斷，回傳 `stop_recommended`（**只建議、不送控制**）。
- `writers`：JSON / CSV / Excel 輸出。

`charge_discharge_report_config.py` 內容（草案）：
```python
SAMPLE_INTERVAL_SEC = 5
OUTPUT_ROOT = "output/charge_discharge_reports"
POWER_SOURCE = "pcs"          # pcs=totalActivePowerOfAcBus / battery_va=V×A
IDLE_KW = 0.1
ENERGY_METHOD = "trapezoid"
# 安全門檻（只監測、不送控制）
SOC_MAX = 95
SOC_MIN = 10
POWER_DEVIATION_KW = 15       # 實際偏離設定超過此值且持續
POWER_DEVIATION_SEC = 30
COMM_FAIL_MAX = 3             # 連續通訊失敗次數
SESSION_TIMEOUT_SEC = 3600
ALARM_STOP_LEVELS = {0}       # 0=嚴重 → 觸發 stop_recommended
```

---

## 6. 輸出欄位規格（第五、十一、十二點）

### 6.1 samples.csv（每 5 秒一列）
`timestamp, elapsed_seconds, action, target_power_kw, pcs_status, pcs_control_mode,
pcs_work_mode, pcs_power_control_mode, actual_active_power_kw, actual_reactive_power_kvar,
battery_status, battery_power_status, soc_percent, battery_voltage_v, battery_current_a,
calculated_power_kw, cumulative_energy_kwh, charge_discharge_direction, direction_source,
alarm_count, communication_ok, raw_source_time` + 追查欄位
`raw_active_power, normalized_active_power, raw_current, normalized_current`。

> `direction_source` ∈ `systemChargingStatus / systemDischargingStatus / pcs_status_flag / power_fallback`（見 §3）。

### 6.2 alarms.csv（於 finalize 一次寫入，`id` 去重，含 baseline/recovery）
`alarm_id, level, target_object, alarm_content, origin, first_seen_time, first_seen_elapsed,
alarm_start_time, recovery_time, duration_seconds, is_recovery, alarm_status, caused_stop`。
> API 無恢復時間欄位；`recovery_time`/`duration_seconds` 由觀測 `alarmStatus` 由「告警中」翻為
> 「已恢復」時記錄。`origin ∈ {pre_existing, new}`（session 開始既有者為 baseline，不計為新增）。

### 6.3 summary.json
沿用你需求文件第十一點結構（`session / start_state / end_state / statistics / validation`）。
`start_state`/`end_state` 用同一組欄位（§2.1–2.4）；差異欄位（SOC變化、最大充/放電功率、
平均/最大/最小 功率電壓電流、執行秒數）由統計累積器算出。
`end_reason` enum：`completed / manual_stop / pcs_stop / alarm_stop / timeout /
communication_error / control_failed / unknown`（中文對照見需求第三點）。

### 6.4 report.xlsx（openpyxl）
工作表：`測試摘要 / 時序數據 / 告警紀錄 / 開始與結束狀態 / 統計分析`。
圖表（各自獨立、不混單位）：SOC-t、實際功率-t、電壓-t、電流-t、設定vs實際功率-t。

> 需新增相依套件：`openpyxl`（Excel）。目前 requirements 只用 `requests`，需你同意加入。

---

## 7. 安全與監測（第十點）—— 只記錄、不送控制

- 報告模組**不送任何控制命令**，只 GET 唯讀 + 讀取 operator 產出的結果檔。
- 監測項目（門檻全在 config）：PCS 故障/停止、電池下電、SOC 超上/下限、連續通訊失敗、
  新增嚴重告警、實際功率長時間偏離設定、電壓/電流超門檻。
- 觸發時：`stop_recommended = True`（回傳給呼叫端），**不自行 POST 停止**。
  是否真的送 `pcs_stop_power` 由原控制流程/使用者決定。

---

## 8. 與選單整合方式（第十三、十四點）

**建議流程**（沿用既有、登入只一次）：
```
使用者於選單選 PCS 充/放電 (4/5/6 → 充/放電 → 輸入功率 → YES)
  → 建立 report session、記錄開始狀態
  → 原控制流程送命令（既有 _execute_live，報告不介入控制）
  → 依 SAMPLE_INTERVAL 取樣 + 監測告警
  → 使用者選「7. 停止充放電」或達結束條件
  → 記錄結束狀態、算統計、輸出 JSON/CSV/Excel
```

**選單改動（兩案，擇一，等你決定）**：
- 案A：新增 `16 開始充放電報告 / 17 結束報告 / 18 查看報告狀態`。
- 案B：在既有 PCS 充/放電動作時詢問「是否同步建立充放電報告？(Y/N)」，預設 Y，**不影響原控制**。

執行中的動態顯示區塊（同一區塊更新，不重複新增行，沿用 `print_progress_line`）：
Session ID / 動作 / 設定功率 / 經過時間 / 目前 SOC / 實際功率 / 累積電量 / 告警數 / 報告狀態。

> 整合到 menu 屬「修改既有控制檔」，依你的謹慎工作流，這步會**等你確認後**才動。
> 建議先讓 `charge_discharge_report.py` 能**獨立執行**（旁路監測既有 operator 的產出），
> menu 整合列為第二步。

---

## 9. 需修改 / 新增的檔案清單（第十五點 7）

**新增（低風險）**
- `test/charge_discharge_report.py`
- `test/charge_discharge_report_config.py`
- `output/charge_discharge_reports/`（執行時自動建立）

**修改（需你確認，屬既有控制檔）**
- `test/device_control_menu.py`：加入報告選項/詢問與執行中顯示區塊（案A或案B）。

**不修改**
- `device_control_operator.py` / `device_control_scraper.py` / 其他 scraper（只 import 重用）。

**相依**
- 新增 `openpyxl`（Excel 與圖表）。

---

## 10. 風險與待確認事項（第十五點 8）

| # | 項目 | 說明 | 需你確認 |
|---|---|---|---|
| R1 | **正負號慣例** | 控制setpoint(充負放正) 與 量測(充正放負) 相反；已用一次放電快照佐證量測慣例 | 建議再抓一次「充電中」快照複核；確認 UI 未反向處理 |
| R2 | PCS 有功來源 | 採 guest `totalActivePowerOfAcBus`（免登入）為主、V×A 為輔 | 是否同意此優先序 |
| R3 | authed 端點 403 | `/client/dynamic/dataOrControl/pcs` 本帳號可能 403；報告改用 guest 源已可繞開 | 確認 getRunMode/getScheduleSwitch（authed）在報告執行時可讀 |
| R4 | 當日電量計數 | `daily*EnergyThroughAcPort` 每日歸零、全站共用 | 僅作交叉比對、不當 session 電量（已如此設計） |
| R5 | 取樣間隔 vs 精度 | 5 秒梯形積分；快速變動時可能低估 | 是否需要可調（已放 config） |
| R6 | 結束觸發來源 | 報告如何得知「控制已停止」：讀 operator 結果檔 / 監測 PCS 狀態 / 使用者手動 | 選定結束判定來源 |
| R7 | 告警恢復時間 | row 無恢復時間欄位，只有 alarmStatus | 是否需要恢復時間（可能要另找 API） |
| R8 | 選單整合時機 | 修改 menu 屬既有控制檔 | 先獨立版、後整合？（建議） |
| R9 | 新增 openpyxl | 目前僅依賴 requests | 是否同意加入相依 |
| R10 | 操作人員欄位 | 目前登入為固定 `hmiUser`，無「實際操作人員」來源 | 是否以登入帳號充當、或留空 |

---

## 11. 建議的下一步（等你點頭）

1. 你確認 §3 正負號與 §10 風險清單。
2. 我先寫 `charge_discharge_report_config.py` + `charge_discharge_report.py`（**獨立可跑、純唯讀取樣**），
   先用「不觸發控制」的旁路模式驗證取樣/統計/輸出三個檔正確。
3. 驗證 OK 後，再談 `device_control_menu.py` 整合（案A / 案B）。

> 本階段完全未動控制程式、未送任何充放電命令。
