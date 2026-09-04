# Crawler Sample — BESS Monitoring / Control / Auto Report

## 1. 專案簡介

儲能櫃（BESS）HMI 的資料擷取與設備控制工具集，涵蓋：

- BESS HMI 資料擷取
- PCS / Battery / Environment 狀態解析
- 人工設備控制（CLI）
- 自動充放電報告
- Windows Service 背景監控
- Meter-based Phase 6 自動充放電決策
- Phase 6.10 無人值守 ownership handoff

| 項目 | 值 |
|---|---|
| API Base | `http://192.168.128.110:8080/admin-api` |
| 前端 HMI | `http://192.168.128.110:8853/`（Vue，Yudao/ruoyi-vue-pro） |
| 輸出目錄 | `D:\Crawler Sample\output` |

Phase 1~5 已 COMPLETE；Phase 6 決策鏈完成但**實機控制尚未啟用**，
Phase 6.10 離線開發完成並已凍結。目前狀態一律以 [§9](#9-development-progress) 為準。

## 2. 核心功能

| 功能 | 說明 |
|---|---|
| HMI / BESS data scraping | 電池／PCS／空調／進排風／冷卻循環，逐欄對齊 HMI UI |
| PCS / Battery status | 依 HMI `pcsMode_US` 規則解析，旗標一律讀 `oldValue`（語言無關） |
| Manual control CLI | 選單式設備控制，控制指令需二次確認 |
| Auto charge/discharge report | 排程開始自動建立 Session，結束自動收尾產表 |
| Auto Monitor Windows Service | 不開 Dashboard 也能自動建立與收尾報告 |
| Session / Auth Recovery | 服務重啟續接同一 Session；登入態失效自動重登 |
| Ownership / Observer | 同時只有一個寫入者，Dashboard 為唯讀觀察者 |
| Meter / TOU / Decision / Safety Gate | 電表功率分類、時段判定、決策引擎、安全閘 |
| Control Authority | 控制權歸屬判定與一次性授權票 |
| Unattended ownership handoff | 與外部控制程式的暫停／接管／歸還（Phase 6.10） |

## 3. 系統架構

```
HMI / ESS
   ↓
Scraper / API Client
   ↓
Monitor / Decision
   ↓
Safety / Authority
   ↓
Report / Control
```

Phase 6.10 ownership handoff：

```
External Controller
   → Pause
   → Verify Idle
   → Phase 6 Ownership
   → Release
   → Restore External Controller
```

任一環節無法確認，一律 **Fail Closed**（不送出、不接管、不清除恢復責任）。

## 4. 主要檔案

| 檔案 | 用途 |
|---|---|
| `api_client.py` | API 薄封裝：session、登入、Bearer、統一 timeout |
| `dashboard_scraper.py` | Dashboard 數據概覽擷取 |
| `device_control_scraper.py` | 共用解析核心（PCS／電池／環控） |
| `device_control_menu.py` | CLI 設備控制選單 |
| `device_control_operator.py` | 控制指令組裝與送出（需 `--execute` + YES） |
| `report_monitor.py` | Monitor Core：監看決策、Session 生命週期、Ownership |
| `auto_monitor_service.py` | 背景服務入口，由 NSSM 啟動常駐 |
| `charge_discharge_report.py` | Session 與報告產出（CSV／Excel／統計） |
| `phase6_handoff_orchestrator.py` | Ownership 狀態機、Journal、恢復判定、交接流程 |
| `phase6_remote_adapter.py` | 遠端 guard 契約、SSH timeout、capability 解析 |
| `phase6_unattended_service.py` | 無人值守服務 lifecycle 與 live readiness |
| `phase6_guard_deploy_plan.py` | Guard 部署／回滾套件（只產生指令，不執行） |
| `run_all.py` | 一鍵執行全部擷取腳本 |
| `run_phase3_regression.py` | Phase 2~5 Regression 總表 |

## 5. 快速使用

```bash
python device_control_scraper.py     # 產生 device_control_readonly.json + 摘要
python dashboard_scraper.py --once   # Dashboard 抓一次
python dashboard_scraper.py --loop   # Dashboard 背景循環（Ctrl+C 停止）
python device_control_menu.py        # CLI 設備控制選單
python run_all.py                    # 一鍵執行全部擷取
python run_phase3_regression.py      # Phase 2~5 Regression
```

**登入設定**：放在 `test/` 下的 `*.env`（例如 `login.env`），
可依 `.env.example` 建立本機檔。**`.env` 已由 `.gitignore` 排除，不可 commit。**

## 6. Auto Monitor / Auto Report

```
排程 / 控制方向 → Session → Sampling → Finalize → report.xlsx
```

輸出：`output\charge_discharge_reports\<session_id>\`
（`samples.csv`、`events.csv`、`summary.json`、`statistics.json`、`report.xlsx`
及 `alarms.csv`、`cell_snapshots.json`、`session_state.json`）

```
Windows SCM → NSSM → auto_monitor_service.py → report_monitor.py
```

重點：

- Dashboard 未開啟時也能產生報告
- **Service = Owner；Dashboard = Observer**（唯讀，不寫報告）
- Graceful Recovery（正常停止）與 Crash Recovery（異常中斷）皆續接同一
  `session_id` 與 folder，`sample_index` 繼續遞增，**不會建立第二份 Session**
- `tools\nssm.exe` 為 runtime dependency，不可刪除／改名／搬移
- Service log：`output\logs\auto_monitor_service.log`

> Service 六個動作（install / update / start / stop / status / remove）的完整說明見
> [`../tools/README.md`](../tools/README.md)（權威來源）。
> 部署、健康檢查、Upgrade、Rollback、Troubleshooting 見
> [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md)。
> Recovery 兩條路徑的實機證據見
> [`../docs/Phase4_Closure_Report.md`](../docs/Phase4_Closure_Report.md)。

## 7. Phase 6 — Meter-based Auto Charge / Discharge

以電表功率、時段與 SOC 產生充放電決策，經 Safety Gate 與 Control Authority
才可能送出 PCS 指令。逐階段狀態見
[§9 Development Progress](#9-development-progress)。

重點結論：

- Production 正常控制目標 **±5 kW**。
- `max_power_kw = 150 kW` **僅為 Safety Gate 的指令上限**，不是營運功率。
- 方向反轉 **Layer 1 direction interlock 已完成**；
  **Layer 2 `min_switch_interval` = DEFERRED**（無正式依據，不得自行填值）。
- `dispatch_ready` 與 `dispatch_enabled` **分離** —— 條件齊備不等於允許送出。
- 任一條件無法確認一律 **Fail Closed**。
- **External Controller 已 field confirmed**（`192.168.70.201` 的
  `~/ems/auto_control.py`）。
- FIRST LIVE 因該 competing controller 自動接管而**安全 ABORT**，
  Phase 6 即時偵測到外部控制並停止，未送出任何指令。
- **Phase 6 實機 CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0。**

> 三種 kW 語意（Grid Meter `+=IMPORT`、Battery `+=CHARGE`、
> PCS Command `CHARGE→負 setpoint`）**嚴禁混用**。
> 逐階段開發歷史見 [Appendix](#appendix--historical-development-notes)。

## 8. Phase 6.10 — Unattended Ownership Handoff

目的：安全完成 `External → Pause → Phase6 → Release → Restore External`。

核心特性：Ownership State Machine、Fail Closed、Restore Responsibility、
Crash / Restart Recovery、Durable JSONL Journal、Remote SSH Guard、
Capability Reporting、Single Instance、Live Readiness Gate、
Deployment / Rollback Package。

```
Phase 6.10 OFFLINE DEVELOPMENT = COMPLETE / FROZEN
```

逐子階段狀態見 [§9 Development Progress](#9-development-progress)。

### Production Parameters

**Service**

| 參數 | 值 |
|---|---|
| `decision_interval_sec` | 30 FINAL |
| `retry_backoff_sec` | 5 FINAL |
| `service_health_timeout_sec` | 300 FINAL |

**SSH**

| 項目 | 值 |
|---|---|
| connect | 5 FINAL |
| probe | 5 FINAL |
| status | 12 FINAL |
| loopcheck | 26 FINAL |
| pause | 12 **STRUCTURAL CANDIDATE / NOT FINAL**；production default = `None` |
| restore | 12 **STRUCTURAL CANDIDATE / NOT FINAL**；production default = `None` |

**Handoff**

| 項目 | 值 |
|---|---|
| idle power | −3 ~ +1 kW |
| pause settling | 120 s |
| stable verify | 4 samples / 15 s / 45 s |
| restore verify | 3 observations / 3 s / 6 s |

> `pause` / `restore` 恆列於 `missing()` 與 `not_field_verified()`，
> **不得升格 FINAL**，直到取得真正的控制路徑實測。
> 各參數的推導與 evidence 見測試檔、`output/` 下的量測紀錄，
> 以及 [Appendix](#appendix--historical-development-notes)。

## 9. Development Progress

| Phase | Item | Status |
|---|---|---|
| Phase 1 | Report Framework | COMPLETE |
| Phase 2 | Auto Start | COMPLETE |
| Phase 3 | Auto Lifecycle | COMPLETE |
| Phase 4 | Auto Monitor Service | COMPLETE |
| Phase 5 | Production Ready | COMPLETE |
| 5.1 | Production Validation Plan | COMPLETE |
| 5.2 | Long-running Stability / Soak Test | COMPLETE |
| 5.3 | Stress / Boundary Test | COMPLETE |
| 5.4 | Production Documentation / Deployment | COMPLETE |
| 5.5 | Release Validation | COMPLETE |
| Phase 6 | Meter-based Auto Charge / Discharge | OFFLINE COMPLETE / LIVE HOLD |
| 6.0 | Production Meter Verify | COMPLETE |
| 6.1 | Meter Client | COMPLETE |
| 6.2 | Power Classification | COMPLETE |
| 6.2b | TOU Calendar | COMPLETE |
| 6.3-A | Decision Engine Framework | COMPLETE |
| 6.3-B | Decision Policy | COMPLETE |
| 6.4 | Safety Gate | COMPLETE |
| 6.5 | PCS Control Integration | COMPLETE / LIVE HOLD |
| 6.5-H | Control Authority / Arbitration | OFFLINE VERIFIED |
| 6.6 | Auto Report Integration | COMPLETE |
| 6.7 | Field Validation | COMPLETE / OBSERVE_ONLY |
| 6.8 | Regression & Closure | COMPLETE |
| 6.9 | Controlled FIRST LIVE | ATTEMPTED / BLOCKED |
| 6.9-A | CLI Entry Wiring | OFFLINE VERIFIED |
| Phase 6.10 | Unattended Ownership Handoff | OFFLINE COMPLETE / FROZEN |
| 6.10 Core | Ownership Handoff Core | COMPLETE |
| 6.10-A | Remote Adapter | COMPLETE |
| 6.10-A1 | Verification | COMPLETE |
| 6.10-B1 | Read-only Guard | COMPLETE |
| 6.10-B1.5 | Production Preconditions | COMPLETE |
| 6.10-B1.6 | Network Identity Blocker | COMPLETE / BLOCKER CONFIRMED |
| 6.10-B1.7 | Network Identity Verification | DEFERRED / NOT VERIFIED |
| 6.10-B2 | Offline Handoff | COMPLETE |
| 6.10-B2 LIVE | Live Handoff Deployment | NOT STARTED / BLOCKED |
| 6.10-C | Offline Unattended Service | COMPLETE |
| 6.10-C1 | Service Timing | COMPLETE |
| 6.10-C2 | SSH Architecture | COMPLETE |
| 6.10-C3 | SSH Timing Evidence | COMPLETE |
| 6.10-C4 | Live Readiness | COMPLETE |
| 6.10-C LIVE | Unattended Service Live Deployment | NOT STARTED / BLOCKED |

### Current Summary

- Phase 1 ~ Phase 5：COMPLETE
- Phase 6：OFFLINE COMPLETE / LIVE HOLD
- Phase 6.10：OFFLINE COMPLETE / FROZEN
- Phase 6.10-B1.7：DEFERRED / NOT VERIFIED
- Phase 6.10-B2 LIVE：NOT STARTED / BLOCKED
- Phase 6.10-C LIVE：NOT STARTED / BLOCKED
- FIRST LIVE：HOLD
- LIVE_READINESS：BLOCKED

### Current Safety State

- DISPATCH_ENABLED = False
- MODE = DRY_RUN
- DEPLOYED_GUARD_VARIANT = B1
- RemoteSenders armed = False
- NETWORK_IDENTITY_STABILITY = NOT VERIFIED

Field command count：

- Phase 6 CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
- External pause / restore / stop / start = 0 / 0 / 0 / 0

### Live Blockers

- DHCP Reservation = NOT CONFIRMED
- B2 guard deployment = NOT AUTHORIZED
- Remote Guard（field）：`probe` / `loopcheck` / `status` = AVAILABLE；
  `pause` / `restore` = REFUSED（historical B1 evidence）
- `pause` / `restore` command timeout = STRUCTURAL CANDIDATE，尚無實測

### Next Field Sequence

1. B1.7 Network Identity Verification
2. B2 Guard Deployment
3. B2 read-only post-deploy verification
4. Controlled FIRST LIVE
5. pause / restore timing evidence
6. 評估 pause / restore timeout FINAL
7. Phase 6.10-C Live Service Deployment

> **上述任何 Field 階段均需要新的明確授權。**

## 10. Regression

| 範圍 | 基準 | 執行方式 |
|---|---|---|
| Phase 2~5 | **1802 / 1802 PASS**，0 FAIL / 0 SKIP | `python run_phase3_regression.py` |
| Phase 6 | **4172 / 4172 PASS**，43 檔，0 FAIL | 逐檔執行 `test_phase6*.py` |

> `run_phase3_regression.py` 只涵蓋 Phase 2~5，**不含 Phase 6**；
> Phase 6 目前**沒有**專屬的 runner 入口，以逐檔執行為準。

## 11. 文件

| 文件 | 內容 |
|---|---|
| [`../docs/Phase3_Closure_Report.md`](../docs/Phase3_Closure_Report.md) | Phase 3 自動生命週期收尾報告 |
| [`../docs/Phase4.6_Closure_Report.md`](../docs/Phase4.6_Closure_Report.md) | Windows Service 包裝與 NSSM 驗證 |
| [`../docs/Phase4_Closure_Report.md`](../docs/Phase4_Closure_Report.md) | Phase 4 收尾：架構、Ownership、Recovery、已知限制 |
| [`../docs/Phase5_Validation_Plan.md`](../docs/Phase5_Validation_Plan.md) | Phase 5 驗證計畫與驗收基準 |
| [`../docs/Phase5_Closure_Report.md`](../docs/Phase5_Closure_Report.md) | Phase 5 收尾：Soak / Stress / 文件 / Release Validation |
| [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md) | **部署 / 維運手冊** |
| [`../tools/README.md`](../tools/README.md) | NSSM 版本、授權、SHA-256；**Service action 權威來源** |

> Phase 6 / Phase 6.10 目前**沒有**專屬 closure 文件；
> 完整敘述保留在本檔 [Appendix](#appendix--historical-development-notes)、
> 測試檔與 `output/` 下的實測紀錄。

## 12. 注意事項

- **`.env` 不可 commit**（已由 `.gitignore` 排除）。
- **`tools/nssm.exe` 為 runtime dependency**，不可刪除／改名／搬移。
- Service 運行時 **Dashboard 為 Observer**，不會寫入報告。
- **不要同時啟動第二個 Monitor writer** —— Ownership 會擋下，但應避免。
- **Phase 6 預設不允許實機控制**：`DISPATCH_ENABLED = False`、`MODE = DRY_RUN`。
- 任一條件無法確認一律 **Fail Closed**；不確定不等於安全。
- 詳細歷史、實機證據與量測紀錄請看 `docs/`、`test/`、`output/`。

---

## Appendix — Historical Development Notes

> 以下為逐階段開發過程的原始紀錄，**內容未經刪改**，僅由主文搬移至此。
> 這些是 field evidence 與設計論證的來源，目前沒有對應的獨立 docs 文件，
> 因此保留全文。日常閱讀請看上方 §1~§12。

<details>
<summary><b>Phase 1~6 逐階段開發歷史（含 D.2~D.5 架構決議、6.5 blocker 推導、FIRST LIVE 逐秒紀錄、Source Authority / Annual Calendar / crash window 研究）</b></summary>

#### 10. 開發階段

| Phase | 內容 | 狀態 |
|---|---|---|
| Phase 1 | Report Framework | COMPLETE |
| Phase 2 | Auto Start | COMPLETE |
| Phase 3 | Auto Lifecycle | COMPLETE |
| Phase 4 | Auto Monitor Service | COMPLETE |
| Phase 5 | Production Ready | COMPLETE |
| Phase 6 | Meter-based Auto Charge / Discharge | IN PROGRESS |

**Phase 5 進度**

| | 項目 | 狀態 |
|---|---|---|
| 5.1 | Production Validation Plan | COMPLETE |
| 5.2 | Long-running Stability / Soak Test | COMPLETE |
| 5.3 | Stress / Boundary Test | COMPLETE |
| 5.4 | Production Documentation / Deployment | COMPLETE |
| 5.5 | Release Validation | COMPLETE |

5.2：原始 Soak 發現 OpenBLAS 一次性資源成本；限制 Windows Service
`OPENBLAS_NUM_THREADS=1` 後，部署與 Fix Validation 均 PASS。

5.3：Stress / Boundary Test 完成，#1～#12 全部 PASS；Phase 5.3-A 244/244、
Full Regression 1802/1802，正式 Service / Ownership / output 全程維持隔離。

5.4：Production Documentation / Deployment 文件化完成；新增
[`../docs/Operations_Guide.md`](../docs/Operations_Guide.md)，涵蓋部署、設定、
Service Health、Upgrade、Rollback、Troubleshooting 與 Security，文件 DoD 8/8 PASS。

5.5：Release Validation 驗證性項目全數 PASS —— Full Regression 1802/1802、
實機 Session evidence 3 份各 8/8、Service health 11/11、Security 無 Critical/High。
收尾報告見 [`../docs/Phase5_Closure_Report.md`](../docs/Phase5_Closure_Report.md)。

> **Phase 5 = COMPLETE 指的是 Validation 完成。**
> tag / push / deployment package 屬**後續人工 Release 動作，尚未執行**。

**Phase 6 進度**

| | 項目 | 狀態 |
|---|---|---|
| 6.0 | Production Meter Verify（正式環境電表確認） | COMPLETE |
| 6.1 | Meter Client（電表資料讀取） | COMPLETE |
| 6.2 | Power Classification（功率狀態判斷） | COMPLETE |
| 6.2b | TOU Calendar（尖峰／離峰時段判斷） | COMPLETE |
| 6.3-A | Decision Engine Framework（決策骨架） | COMPLETE |
| 6.3-B | Decision Policy（充放電策略規則） | COMPLETE |
| 6.4 | Safety Gate（安全條件檢查） | COMPLETE |
| 6.5 | PCS Control Integration（PCS 自動充放電控制） | **COMPLETE / LIVE AUTHORIZATION PENDING** |
| 6.5-H | Control Authority / Command Arbitration（控制權判定） | IMPLEMENTED / OFFLINE VERIFIED |
| 6.6 | Auto Report Integration（自動報告整合） | **COMPLETE / OFFLINE VERIFIED** |
| 6.7 | Field Validation（實機驗證） | **COMPLETE / OBSERVE_ONLY FIELD VALIDATED** |
| 6.8 | Regression & Closure（完整回歸與結案） | **COMPLETE / TECHNICALLY READY FOR EXPLICIT LIVE AUTHORIZATION** |
| 6.9 | Controlled LIVE Enablement（受控單次上線） | **OFFLINE COMPLETE / FIRST LIVE ATTEMPTED-BLOCKED** |
| 6.9-A | CLI Entry Wiring（上線入口接線） | **OFFLINE IMPLEMENTED / VERIFIED**（入口已接通，尚未執行任何實機 LIVE） |

**Phase 6.5 Go-Live Blockers / Check Items**

Phase 6.5 各子項雖已離線驗證，但下列項目未解除前**不得**標示 Production Go-Live Ready：

| | 項目 | 狀態 |
|---|---|---|
| 1 | ReadBack timeout 合理 SOC 實機驗證 | **CLOSED** —— `timeout_sec` 已 **FINAL**，並於 D.5-A 依裁示寫入 production config |
| 2 | Control Authority TTL | **CLOSED / FINAL**（D.5-C）—— 已依既有 freshness 契約推導並寫入；不需實機 |
| 3 | Control Authority power tolerance | **CLOSED / FINAL（綁定 target 5 kW）**（D.5-C） —— 依 4 段穩態實測（誤差 ≤0.40 kW／≤8%，無 outlier）推導；改功率須重新評估 |
| 4 | 最小切換間隔（時間互鎖） | **CLOSED FOR CURRENT GO-LIVE SCOPE / LAYER 2 DEFERRED** —— 無反轉間隔的實機依據，明確維持停用；方向安全由 Layer 1 保證 |
| 5 | STOPPED → STANDBY 無獨立啟動路徑 | **CLOSED / NOT REQUIRED — DOCUMENTED BEHAVIOR** |
| 6 | Field steady detector false-positive | **CLOSED**（Phase C.1） |
| 7 | Direction-Reversal State Interlock | **IMPLEMENTED / OFFLINE VERIFIED**（Phase D.1） |
| 8 | Production Orchestrator 尚未接通 | **WIRED / OBSERVE_ONLY**（D.5-B） —— 整條 production path 已接起並可跑完；控制出口刻意未建立，結構上不可能送出指令 |
| — | Production 參數 | **ALL REQUIRED SATISFIED**（`dispatch_ready=True`）；`dispatch_enabled` 仍為 False，OBSERVE_ONLY 未改變 |
| 10 | 同向目標功率變更的正式語意 | **OPEN** —— 現有回讀機制**結構上無法驗證**同向設定值更新；維持 Fail Closed |
| 11 | 崩潰／重啟後的執行協調 | **CLOSED / DOCUMENTED FAIL-CLOSED** —— 四個崩潰視窗全部安全，不需新增持久化執行日誌 |
| 9 | 年度離峰日清單尚未核准 | **CLOSED / OFFICIAL ANNUAL DATA PROVISIONED** —— 經人工驗收的兩個年度已升為 verified 並正式 provision（D.4-E.2）；驗收綁資料，資料一變即失效。⚠️ 關閉本項**不等於** Go-Live |
| 13 | Authority 功率佐證的時機守則 | **CLOSED / OFFLINE IMPLEMENTED**（D.5-A） —— 「尚未取得指令後的新功率」不再誤判為功率衝突或外部控制 |
| 12 | Production 時區契約 | **CLOSED** —— 已核准並完成接線（IANA 名稱，非固定時差；不含時區資訊的時間一律拒絕） |

**方向反轉採兩層互鎖，兩層彼此獨立、不得合併為同一個檢查：**

| 層 | 名稱 | 依據 | 狀態 |
|---|---|---|---|
| Layer 1 | Direction-Reversal **State** Interlock | fresh PCS 旗標 | **IMPLEMENTED / OFFLINE VERIFIED** |
| Layer 2 | Minimum **Time** Interlock（`min_switch_interval_sec`） | 尚無反轉間隔的實機依據 | **DISABLED / PARAMETER NOT PROVISIONED**（刻意，非遺漏） |

Layer 1 規則：運轉中不得直接送出相反方向的控制（`CHARGING` → discharge、
`DISCHARGING` → charge 一律 BLOCK）。上層必須自行送出一個明確的 STOP leg，
待 read-back 確認進入閒置狀態後，由**新的** decision cycle 重新決定方向。
系統**不會**自動把一個控制請求展開成 STOP + 反向兩個指令。
方向判定只採用當次的 fresh PCS 旗標，**不採用** LastControl 的動作紀錄
（可能過期、跨 boot、或與外部控制不同步）。狀態無法確認時一律 Fail Closed。
STOP 不受本互鎖限制；Safety Gate 對 STOP 的既有豁免亦維持不變。

Layer 2 目前**沒有**正式依據可決定門檻，維持未設定。
既有的排程間隔 / 去抖動 / 冷卻等常數屬監看層語意，**不得**挪用為切換間隔。
PCS 方向反轉的最小間隔列為 **VENDOR DATA REQUIRED**；
在取得正式設備規格前，不以實機做門檻的二分搜尋。

**STOPPED 是 Phase 6 合法的閒置起始狀態。** 一般 CHARGE / DISCHARGE 指令本身即完成所需的
啟動轉態（manualControl 的啟動語意內含於充放電指令），因此**不需要**獨立的 START / STANDBY 指令；
Phase 6 下一次控制可由 STOPPED 直接開始。

Production 參數狀態：`timeout_sec` / `poll_interval_sec` / `stability_samples` 皆為 **FINAL**；
`authority_ttl_sec` / `authority_power_tolerance_kw` / `min_switch_interval_sec` /
充放電功率與額定上限**仍未設定**（數值一律不寫入本文件）。

**Production Path Audit（Phase D.2 唯讀稽核結論）**

Phase 6 目前的定位是：**CONTROL ALGORITHM READY，BUT PRODUCTION ORCHESTRATOR NOT YET WIRED。**

**Phase D.3 架構決議**：Report Monitor 與 PCS Automatic Control 採**獨立服務、獨立行程、
獨立 Named Mutex**。兩者重用既有的 Mutex / DACL / Ownership / Service lifecycle **技術**，
但**不得共用同一把鎖** —— Report Monitor Ownership（誰可以寫報告檔）與
PCS Control Ownership（哪個行程可以驅動自動控制）是兩種不同的所有權語意。

**Phase D.3-A 已完成（Runtime Skeleton，離線）**：新增控制服務外殼、Runtime 狀態機骨架、
與 production 參數來源。此階段刻意**不具備任何實機出口** ——
不 import 既有控制程式、不建立 Executor 實例、不存在 dispatch 路徑；
Runtime 只會停留在 DISABLED / OBSERVE_ONLY / FAULT_BLOCKED / SHUTTING_DOWN。
即使注入明確的充放電意圖，也只產生「本來會做什麼」的稽核事件。

**Phase D.3-B 已完成（ESS Snapshot Adapter，離線）**：新增設備觀測邊界，
唯一責任是把既有的整批讀取結果轉成控制所需的觀測，只回答「設備現在回報什麼」。
它**不做**決策、政策、授權、方向互鎖或安全判定，也不重新發明 PCS 狀態分類 ——
一律重用已通過實機驗證的既有純函式。

資料新鮮度以**讀取開始時刻**為基準（Phase 6.5-G 實機教訓的正式保留）：
整批讀取本身可能耗費數秒，若從「讀完」才起算，一份已經過時的資料會被誤認為全新。
Adapter **不快取**任何觀測結果 —— 上一筆成功的觀測絕不會在本次讀取失敗時被沿用。
告警來源的完整性證明（來源不完整即 Fail Closed，空清單不等於沒有告警）於本階段完整保留。

D.3-B **未接上 production reader**：控制服務維持零設備能力，觀測一律回報「讀取來源未設定」。
觀測有效**不會**讓 Runtime 取得任何控制能力，狀態仍只停留在既有的四個。

**Phase D.3-C 已完成（電表觀測 + 決策觀測，離線）**：新增電表觀測邊界與決策觀測層。
至此 Production Runtime **已經可以看資料並做出決策，但仍然沒有能力執行決策**。

決策鏈完全由既有且已驗證的正式模組負責：尖峰／離峰判定、逆送與盲帶分類、
充放電策略、Fail Closed 順序 —— 觀測層不得出現第二套規則，僅負責把正確的輸入
交給正確的模組並如實記錄結果。電表 payload 的解讀亦完全委派既有電表客戶端，
其中「台電電錶瞬時功率」與「原始需求量」是兩個不同量測，**不得互相取代**。

**Consumer-time freshness（D.3-B 觀察的正式落點）**：Adapter 判定的是「資料取得完成時」
是否夠新；決策層必須在**真正使用資料的當下**以當時的時鐘重新計算兩份資料的年齡，
任一超過既有門檻即進入「資料不足以判斷」的阻擋狀態。
**禁止**假設「Adapter 當時有效」等同「決策當下仍然有效」。

**Snapshot coherence**：設備與電表是兩個獨立來源，取得時刻不同。本階段**只記錄**兩者
時間差，**不設定**任何門檻 —— 目前沒有已裁定的正式規格，不猜數值。

決策結果只是**稽核事實**，不是指令：整條鏈沒有建立任何執行器實例、任何一次性授權、
任何控制紀錄寫入，也沒有任何抵達設備控制介面的途徑。

⚠️ **正式離峰日來源尚未確認**，因此 TOU 目前一律為 UNKNOWN，決策必然 Fail Closed。
   這是誠實的保守行為，不是功能缺陷；在取得正式來源前不得假裝今天不是假日。

⚠️ 控制服務自 D.3-C 起因讀取電表而具備**通往電表**的網路能力。
   已有專屬驗證證明：行程內**沒有**任何通往 HMI／設備控制介面的能力。

**Phase D.3-D 已完成（仲裁與授權，離線）**：新增一次性授權票與仲裁層。
至此系統可以**授權**一個控制腿，但仍然沒有能力**送出**它 ——
「已授權」與「已送出」是完全不同的兩件事。

授權票具備三個不可違反的性質：**只對綁定的那一個動作有效**（充電票絕不能拿去放電）、
**消費一次即永久失效**（送出失敗、回讀失敗、例外都不得重用，重試必須重新走完整流程）、
**必須有明確有效期**（有效期未取得正式依據時一律不核發，絕不解讀成「永不過期」）。

授權前必須重新取得觀測並重新比對：決策當下有效**不等於**授權當下仍然有效。
兩次之間若決策、設備狀態、模式開關、告警來源任一改變，一律不得沿用舊結論。
控制權判定、方向互鎖、閒置語意、安全條件全部重用既有已驗證模組，
安全檢查失敗時**原始原因完整保留**，不得被換成籠統的阻擋碼。

反向切換時，仲裁層會**明確另外形成一個停止腿**並只授權它；
反方向的授權必須等停止完成後、由新的一輪重新評估才可能出現。

同一時間最多只有一張有效授權票；已有未消費的票時不得建立第二張。
設備已在目標狀態且目標未改變時，**不重複授權**（意圖狀態 ≠ 指令邊緣）。

⚠️ **同向但目標功率改變**的正式語意尚未裁定（見 Blocker 10）。唯讀證據盤點結論：

- 既有控制程式的前置檢查**不消費**目前運轉狀態，人工選單亦不檢查 ——
  也就是說「送得出去」在程式層面沒有被擋，但這**不等於**設備契約已確認支援。
- 前端實抓確認的範圍是 payload 欄位、模式列舉與正負號慣例，
  **完全沒有**「運轉中可更新設定值」的任何既有證據。
- 既有實機紀錄中**從未**出現同向連續指令，因此沒有任何可援引的實測樣本。
- **決定性障礙**：既有回讀機制的成功條件只看「是否觀測到目標狀態」，
  功率永不參與、也不要求發生狀態轉變。設備原本就在該方向時，
  回讀會在**第一次輪詢**就判定成功 —— 它結構上**無法**區分
  「新設定值已生效」與「舊設定值仍在跑」。
- 因此若在此前提下更新控制紀錄，等於把「已送出但未驗證」記成「已驗證」，
  直接違反控制紀錄「只記錄已驗證成功結果」的既有契約。

結論：在取得設備契約證據**並且**補上可驗證同向更新的回讀條件之前，
一律維持 Fail Closed，不猜測、不預設走「先停再啟」。

⚠️ 授權所需的正式參數全部尚未取得依據，因此以 production 預設值執行時
   **永遠不會核發任何授權**。這是誠實的保守行為。

**Phase D.3-E 已完成（執行鏈，離線）**：授權票消費 → 送出 → 回讀驗證 → 控制紀錄提交
的完整生命週期已離線跑通，並以注入的替身驗證全部失敗路徑。

**控制紀錄只記錄可證明的成功**：只有回讀驗證成功才寫入。授權核發、等待消費、
呼叫控制程式、API 已接受、驗證進行中、回讀逾時、回讀失敗、例外 —— **一律不得寫入**。

**「未驗證成功」不等於「指令沒有執行」**：API 已接受但驗證未成功時，設備**可能其實已經照做**。
因此一律回報為「指令結果不明」並 Fail Closed —— 不宣稱擁有、不更新紀錄、
**不自動重送**、**不自動停止**。只有能證明未送出的情況（控制程式未注入／
乾跑／前置檢查擋下）才會被歸類為「未送出」。

**回讀成功但紀錄寫入失敗**是獨立的失敗類別：設備確實已被控制，但持久化擁有權紀錄
沒有建立成功。既不能當成正常擁有，也不能當成指令失敗 —— 一律 Fail Closed。

**授權票一次性**：消費後永久失效。送出失敗、回讀失敗、例外、逾時都不得重用；
重試必須重走觀測 → 決策 → 仲裁 → 授權的完整流程。送出前還會再做一次新鮮度重驗，
**有效期未過期不等於資料仍然新鮮**，兩者分別檢查。

⚠️ **重複抑制是安全條件，不是效能最佳化**：既有回讀在「設備原本就在目標狀態」時
會在第一次輪詢就判定成功。因此相同動作＋相同目標必須在仲裁層就被抑制，
**絕不能**進到控制程式 —— 否則舊狀態會被誤認為新指令已成功。此不變量已由回歸鎖死。

⚠️ 崩潰視窗分析結論：四個視窗全部 Fail Closed（見下）。**不需要**新增持久化執行日誌 ——
   它只能提升可用性，且要自動恢復擁有權就必須在未驗證的情況下宣稱擁有，與既有契約衝突。

⚠️ production 預設下控制程式、回讀、控制紀錄儲存**全部未注入**，且執行狀態不可達，
   因此零授權、零送出、零紀錄寫入。

**Phase D.3-F 已完成（崩潰／重啟協調，離線）**：新增純邏輯的擁有權重判層。
它**只分類、不判定** —— 擁有權的唯一判定者仍是既有的控制權模組，
本層只把結論翻譯成「重啟後應回到哪個狀態」，不覆寫、不補強、不放寬。
它**不可能**送出任何指令，也不會改動任何控制紀錄（已由回歸逐條鎖住）。

**重啟後只有兩樣證據**：磁碟上的控制紀錄（且必須通過開機識別與信任判定），
以及設備自己的當下狀態。**記憶體中「曾經驗證成功」在重啟後並不存在**，不得作為證據。

四個崩潰視窗的結論：

| 視窗 | 重啟後判定 | 重複指令 | 錯誤宣稱擁有 | 失去擁有權 |
|---|---|---|---|---|
| 授權已消費 → 送出前崩潰 | 可重新評估並重新授權 | 否 | 否 | 否 |
| 已送出 → 回讀前崩潰 | 運轉中但無紀錄 → 判為外部控制 | 否 | 否 | **是**（安全，需人工） |
| 回讀成功 → 提交前崩潰 | 磁碟世界與上一列**完全相同**，同樣不得認領 | 否 | 否 | **是**（安全，需人工） |
| 提交成功 → 狀態更新前崩潰 | 紀錄有效且與現況相符 → **完整自動恢復** | 否 | 否 | 否 |

**服務重啟 ≠ 作業系統重開機**：服務重啟（未重開機）時控制紀錄仍可信，
擁有權可完整恢復；作業系統重開機後開機識別改變，紀錄降為僅供歷史查閱，
**自動失去擁有權信任** —— 這正是既有設計的預期行為。

**不需要新增持久化執行日誌**：四個視窗全部 Fail Closed，沒有任何一個會造成
重複指令或錯誤宣稱擁有。日誌唯一能改善的是中間兩個視窗的**可用性**，
但要自動恢復擁有權，就必須在未驗證的情況下宣稱擁有 —— 與控制紀錄
「只代表已驗證結果」的契約直接衝突。因此這兩個視窗維持「需人工介入」，
並登記為明確的可用性取捨。

**Phase D.3-G 已完成（啟動恢復整合，離線）**：服務啟動／重啟時，
以明確的恢復入口重建擁有權狀態。建構後**一律**從停用狀態開始 ——
不存在「一建立就宣稱擁有」的路徑。

恢復只做三件事：**讀取 → 判定 → 映射狀態**。它不送指令、不寫控制紀錄、
不建立授權、不改變送出開關、也不恢復「等待消費」的狀態
（授權是一次性且僅存在於記憶體，重啟後必然不存在）。

**擁有權恢復不等於啟用送出**：即使恢復判定為「我們仍擁有這台設備」，
送出開關維持原狀。兩者完全獨立，各有專屬驗證。

**以持久證據與當下實況為準，不是以記憶體殘留為準**：記憶體殘留「擁有」但證據
判為外部控制時必須被覆寫；反之記憶體是阻擋狀態而證據合法時，也應恢復為擁有。

**恢復只在啟動或明確呼叫時執行**，一般週期不重跑判定（已由回歸鎖住）。
任何讀取例外、判定例外、來源未接、觀測無效 —— 一律 Fail Closed，
且**不得**沿用先前的擁有狀態。

⚠️ 「找不到控制紀錄」與「讀不出控制紀錄」是**不同**的事：前者在設備閒置時可安全
重新開始；後者無法排除「其實有一筆有效紀錄」，一律 Fail Closed。既有儲存層
**已提供**此區分，本階段未修改其契約。

⚠️ 本階段**未新增任何 runtime 狀態**，全部沿用既有狀態；擁有狀態原本受送出開關
保護，恢復走的是**明確且獨立的通道**，並以測試逐條鎖住其邊界。

**Phase D.4 已完成（時段提供者，離線）**：新增正式的「現在屬於哪一種台電時段」來源。

**盤點結論**：專案內**已有**正式時段規則來源（Phase 6.2b 的時段模組），
其規則出處為台電電價表所定的二段式時間電價方案，並已明文記載
「這是指定的工作基準，不是已由現場電費單／契約證實採用」。
本階段**完全沿用**該規則，一行未改、未重寫任何時段表。

**Provider 與 Policy 分離**：時段提供者只回答「時段事實」，不介入充放電決策；
取得當下時間的責任集中於此，不再散落於決策層。

**兩個維度完全分離**：時段維度只有尖峰／半尖峰／離峰／未知；
逆送屬於電網潮流維度，由電表判定，**不是一種電價時段**（值域互不重疊，已鎖住）。
⚠️ 週六為**半尖峰**且有獨立費率，**不得**併入離峰。

**時區必須明確**：production 時區設定目前為**未配置** → 一律未知（Fail Closed）。
不做固定時差運算、不依賴「主機剛好設在本地」。不含時區資訊的時間戳**一律拒絕**，
避免它成為 production 契約。建議值待核准後才可填入（見 Blocker 12）。

**任何來源不足即未知**：時區未設定／無法解析、時鐘失效或回傳非時間、
離峰日來源未設定／失效／不涵蓋該年度、時段設定無效 —— 全部回未知。
**未知絕不等同離峰**，決策端據此 Fail Closed（已驗證）。

**Phase D.4-A 已完成（年度離峰日供應與時區接線，離線）**

**時區契約已核准並接線**：production 時段一律以 IANA 時區名稱交由時區資料庫解析，
**不做固定時差運算**、**不依賴主機所在地**。不含時區資訊的時間戳一律拒絕。
時區核准**不等於**啟用送出 —— 送出開關維持關閉（有專屬驗證）。

**年度離峰日採「靜態年度清單、隨程式版本控管」**，**不使用任何外部服務**：
自動控制不能因為外部服務中斷就突然不知道今天能否充放電。每份清單必須完整記載
出處、版本、生效日，並經人工核准；**未核准的清單視同不存在**。
資料有任何問題（年份不符、重複、非日期、來源或生效日缺失）一律**拒絕建立**，
不做任何自動修正。排序具決定性。

**三態語意維持不變**：年度未知／未核准 → 無法判定（**不是**「不是假日」）。
禁止「不是週末所以當平日」「查不到假日所以當非假日」這類推論。

⚠️ **production 年度清單目前為空**，因此所有年度皆為未知 → 時段一律未知 →
   決策必然 Fail Closed。**未自行產生任何離峰日**。

⚠️ 已驗證的日型優先順序（沿用既有規則，未修改）：年度離峰日**優先於**星期 ——
   落在週六的離峰日仍為全日離峰，覆蓋半尖峰。

**Phase D.4-B 已完成（年度資料供應與保守判定，離線）**

**保守判定已核准啟用**：只有在「是離峰日」與「不是離峰日」兩種互斥假設下
**得到完全相同的時段**時，才允許在離峰日來源未知的情況下回答那個唯一結果。
實務上只有**週日**符合（兩種假設皆為離峰）。

⚠️ 這**不是** best guess、預設值、fallback 或啟發式。只要兩種假設結果不同就一律未知：
週六（離峰 vs 半尖峰）與平日（離峰 vs 尖峰）在年度資料到位前**必須**維持未知。
**禁止**以「假設不是假日」作為 production 的退路。

**年度資料供應**採人工、可 code review、可版本控管的本機檔案，**零網路**：
每份必須完整記載出處、版本、生效日，並經人工核准。未核准的檔案可以載入但
**不可用**（草稿不得悄悄生效）。載入時完整驗證格式版本、日期格式、年份一致性、
重複、來源與生效日 —— 任何問題一律拒絕，不做自動修正。
production **不會自動掃描目錄**，納入必須是明確、可審查的動作。

**年度資料就緒狀態**共四態：已就緒／未提供／未核准／無效。
只有「已就緒」的年度能判定離峰日；其餘一律視同不存在。
**不得 fallback 使用其他年度的清單。**
就緒狀態**只是回報，不會因此啟用任何控制能力**。

**年度切換**：跨年後若新年度清單尚未供應，受年度資料影響的日期一律未知，
**不得沿用上一年度**（已驗證）；但該年的週日仍可由保守判定得到離峰。

**Phase D.4-C 已完成（年度日期具體化，離線）**

**規則與年度日期嚴格分離**：規則表回答「哪些節日屬於離峰日」，永遠不含任何特定年度的
國曆日期；年度資料回答「這些規則在該年度實際落在哪幾天」，每年重新產生並重新核准。

規則依可判定性分四類：固定國曆日、農曆單日、農曆區間、國曆但逐年變動。
**只有固定國曆日能由規則自動展開**；其餘三類一律必須人工提供年度日期。

⚠️ **專案內完全沒有農曆能力**（無函式庫、標準庫不支援），且**不新增第三方相依**、
   **不自行手刻農曆演算法**。因此農曆項目的國曆日期一律由人工提供並核對 ——
   這也讓年度資料可 code review、可版本控管。

⚠️ **逐年變動項目不得自行固定**：官方記載為「兩個候選日之一」者，未提供即列為待解，
   且提供非候選日一律拒絕。

**草稿永遠不可能自己變成已核准**：產生器結構上不接受核准參數，產出恆為未核准；
放進 Provider 只會是「未核准」狀態，完全無法用於判定。只有人工核准後才成為已就緒。
**有任何待解項目即不得升級。**

⚠️ **政府補假／彈性放假／補班日不得自動視為離峰日** —— 除非官方文件或正式契約另有規定。
   政府行事曆**不等於**電價離峰日曆。

**Phase D.4-D（官方規則對帳，離線）—— SUPERSEDED / CORRECTED BY D.4-E PRIMARY SOURCE**

⚠️ 下段保留 D.4-D 當時的查證紀錄，供事後追溯；
   其中三項 domain 結論已由 D.4-E 的官方年度日曆表直接證據**撤銷**，
   詳見後面的「Phase D.4-D 狀態更正」表格。不得再依據下段的舊結論。

以台電官方網站查證後，Production 規則表已對帳。查證結論：

- 官方離峰日 = **週日 + 9 個具名節日**（開國紀念日、春節、和平紀念日、兒童節、
  民族掃墓節、勞動節、端午節、中秋節、國慶日）
- **教師節、臺灣光復紀念日、行憲紀念日不在官方離峰日清單中** ——
  這三項只出現在 repo 既有測試 fixture，已標記為「官方清單中不存在」，**永不進入 production**
- repo 註解所稱「12 個離峰日」**無法由官方來源佐證**；規則數改由清單長度決定，
  程式中不再以任何數字硬編碼項目數
- **彈性放假日、補假日、颱風假不適用離峰電價** —— 官方明確排除，
  因此政府行事曆**確定不等於**電價離峰日曆
- 週日雖是官方離峰日的第一項，但**已由既有日型規則涵蓋**，不需要年度資料、
  也不進入年度清單

⚠️ **官方 PDF 的文字未能成功擷取**（檔案無法解析），因此**逐字原文未取得**。
   上述結論的依據是使用者提供的官方清單與兩次獨立查證所得的一致定義 ——
   三者互相吻合，但**不等於逐字原文核對**。此限制已寫入規則表的來源註記。

⚠️ 測試 fixture 與官方清單不一致時，**修正的是認知與註記，不是 production 規則**：
   fixture 內容刻意未修改（改它只會讓既有邊界測試失去意義），
   但已明確標註其與官方清單的差異與用途邊界。

規則版本與生效資訊（核定日、生效日、核備文號）已完整記錄於規則表，供事後追溯。

**Phase D.4-E 已完成（官方年度日曆表取得與解析，研究＋離線）**

官方年度時間電價日曆表**已成功取得並完整解析**，解析全程只用 Python 標準庫，
**未安裝任何套件、未變更 Python 環境、未使用任何第三方資料來源**。

- 兩份**互相獨立**的官方檔案（總公司電價表頁面、區營業處公告）各自下載並解析，
  重疊年度的整年度日型摘要**完全相同**，構成獨立互證
- 日型由儲存格顏色編碼，已完成幾何對位；兩個年度皆通過完整性自我檢核：
  全年日數、每月日數、星期欄位、週日全部標示為離峰、平日無週六標示 —— **零例外**
- 年度資料**直接取自日曆表本身**，未由規則表回推、未自行推算農曆

**Phase D.4-D 狀態更正：SUPERSEDED / CORRECTED BY D.4-E PRIMARY SOURCE**

D.4-D 僅依搜尋摘要（最低優先來源）建立認知，造成三項錯誤結論，已正式撤銷：

| 舊結論 | 官方年度日曆表的直接證據 |
|---|---|
| 教師節／光復節／行憲紀念日**不在**官方離峰日清單 | 三項**皆為**離峰日，且各有不受週日遮蔽的年度可佐證 |
| 春節區間自**農曆除夕**起 | 實為自**農曆除夕前一日**起 |
| 「12 個節日項目」**無法**由官方來源佐證 | 官方年度日曆還原結果**支持**「12 個節日項目 ＋ 每週日」 |

D.4-D 的**架構與測試方法保留**，但錯誤的 domain 結論已逐項撤銷並留下可追溯紀錄；
既有 regression 不再斷言舊結論（每一處更動都在測試檔內就地標註更正理由）。

**來源優先序（Source Authority）已正式建立**

P1 官方年度時間電價日曆表（年度實際日型／日期）→ P2 正式詳細電價表（規則語意）
→ P3 其他官方公告 → P4 搜尋摘要／第三方（**只能 research**）。
🔴 高優先來源與低優先來源衝突時，低優先來源**不得覆蓋**；同級亦不得互相覆蓋；
   來源未知一律視為最低優先（Fail Closed）。

**Phase D.4-E.1 已完成（官方年度資料具體化，離線）**

新增官方日曆表解析器與年度資料集模組：解析器**零網路、零第三方相依**，
任何一項對不上就整份拒絕（缺日、週日非離峰、平日被標成週六、色格對位歧義……），
**不交出半份資料**。

年度草稿已產生，但**尚未核准**：

- 兩年度草稿一律 `verified=false`，`build_draft()` **結構上不接受** verified 參數
- `PRODUCTION_ANNUAL_LISTS` **仍為空**，Production 時段判定完全未受影響
- 草稿放進 Provider 只會是 UNVERIFIED（視同不存在）
- 年度資料**不得 fallback**：未涵蓋年度一律回 None，不沿用其他年度

🔴 **Production 不保存整年度日型，但三態證據沒有遺失**：
   OFF_PEAK／SATURDAY／WEEKDAY 可由「非週日離峰日清單 ＋ 星期」完全還原，
   還原結果的摘要必須等於當初從官方 PDF 解析出來的摘要 —— 由 regression 鎖住。
   因此「只存清單」不等於「丟失日型證據」，也避免為此重構既有 schema。

⚠️ 週日雖是官方離峰日，但**不重複**寫進年度清單（已由既有日型規則涵蓋）。
⚠️ 民族掃墓節**不建立通用公式**：規則維持「4/4 或 4/5」候選，
   年度實際日期直接由官方日曆表給出。節日剛好落在週日的年度，
   日曆表的紅色來自「週日」本身，**無法單獨佐證該節日規則** —— 此限制已寫入規則表。
⚠️ 「離峰日數量」的註解已改為**不依賴任何數量的描述**：
   「節日項目數」與「年度日曆日數」本質上不相等（春節本身即為跨多日區間）。
   數量只作為 documentation／validation，**不得**成為 runtime business logic ——
   程式中不存在任何數量常數，項目數一律由清單長度決定。

**Phase D.4-E.2 已完成（已驗收年度資料上線，離線）**

經人工驗收的 2026／2027 年度離峰日清單已升為 `verified=true` 並正式
provision 到 `PRODUCTION_ANNUAL_LISTS`，`PRODUCTION_PROVIDER` 的已核准年度
精確為兩個年度，無任何被拒絕或無效的年度殘留。

🔴 **`verified=true` 的唯一依據**：官方 Calendar provenance ＋ 官方檔案
   SHA-256 ＋ 本次人工驗收，三者缺一不可。**不得**因為「測試 PASS」
   「fixture 對得上」「搜尋摘要一致」「程式自行推導」而成立。

🔴 **驗收是綁資料的**：驗收憑據記錄了當時核對過的資料摘要與檔案 hash。
   任何一天被改動、或來源檔案換了一份，該年度會**直接從 production 消失**，
   退回「年度未知 → UNKNOWN → Fail Closed」，而不是悄悄沿用舊資料。
   要讓新資料生效只能重新送人工驗收 —— 無法靠改程式繞過。
   全模組只有一個地方會產生 `verified=true`，且該函式只收年度一個參數。

明令禁止項皆有對應的結構性驗證：**runtime 自動下載日曆表、任何 runtime
網路相依、自動掃描目錄加入年度、未涵蓋年度自動 fallback、把某年度資料沿用到
相鄰年度、自行產生未涵蓋年度的日期、第三方 Calendar fallback** —— 全部不存在。

年度邊界行為（**不因相鄰年度而 fallback**）：
已 provision 年度的最後一天正常判定；下一年度的第一天即為 UNKNOWN；
未 provision 年度的任何一天都只會得到「無法判定」，不會得到 True/False。

時間契約未放寬：時區為 Asia/Taipei（IANA 名稱），不含時區資訊的時間
**一律拒絕** —— 已 provision 的離峰日也不例外。

⚠️ 年度資料上線**不等於** Production Go-Live：本階段只解決年度時段資料，
   控制參數仍全數未取得依據，dispatch 仍結構性關閉。

**Phase D.5-A 已完成（參數決策與 Authority 時機守則，離線）**

**Blocker 13 — Authority 功率佐證的時機守則**

實機證據顯示 PCS 充放電旗標與 AC 功率暫存器**不保證同步**（0 或 1 個 backend
refresh cycle，延遲案例約 15~16 秒）。原本的功率佐證在指令送出後的更新窗內，
會把**我們自己剛送出的作業**判成 `CONFLICT_POWER`，重啟時更會被 recovery
翻譯成 `EXTERNAL_CONTROL` —— 那不是保守，是把自家作業誤認成別人的。

🔴 **這不是容差數值問題**：離線重現顯示，任何有意義的容差都會誤判；
   唯一「能通過」的容差大到等於廢掉整道佐證。因此**禁止**以放寬容差
   或拉長決策間隔來掩蓋，兩者都已由 regression 鎖住。

修正後明確區分四種情況，各有獨立 reason code：

| 情況 | 結論 |
|---|---|
| 指令後 AC 尚未更新 | UNKNOWN / **NOT_YET_CORROBORATED** |
| 真正的功率衝突 | CONFLICT / CONFLICT_POWER |
| 資料無效或不新鮮 | UNKNOWN / DATA_INSUFFICIENT |
| 正常 | OWNED_BY_PHASE6 |

判定依據是**資料新舊的證據**，不是固定秒數：
① 觀測時刻必須嚴格晚於指令驗證時刻（同一時間基準才比較）；
② 觀測值必須**已經離開** LastControl 所記錄的「驗證當下觀測功率」——
   值沒變就無法證明暫存器已更新。兩者皆成立才允許進行容差比對。
⚠️ 舊資料即使數值「剛好符合」也不算佐證；巧合不是證據。
⚠️ 全程**不使用任何固定 timer、不 sleep、不阻塞**，也不依賴決策間隔。

同時補上**方向**判定：取得指令後的新觀測時，AC 有功的正負號必須與動作一致
（充電為正、放電為負，實機四筆一致）。此前只比絕對值，方向相反會被誤判為相符。

**Recovery 語意修正**：新增 `OWNERSHIP_PENDING` 這一個 runtime 狀態，
語意是「有 durable 證據指向本方作業，但擁有權尚待佐證」——
**不宣稱、也不放棄**。它不在執行／派工狀態集合內，因此結構上不可能送出指令：
**Fail Closed for dispatch，但不誤判 ownership**。真正的外部控制
（無紀錄、信任度不足、上次命令為 stop、動作與狀態矛盾、原生排程 ON）
仍然全部可辨識為 EXTERNAL。

**參數決策**

`readback_timeout_sec` 依既有 FINAL 裁示寫入 production config
（實機四筆狀態確認耗時遠低於此上限）。其餘尚無正式依據的參數**一項都沒有填**，
全部維持未設定；單一參數就緒**不構成**放行理由，dispatch 仍結構性關閉。

**Phase D.5-B 已完成（Orchestrator Wiring / OBSERVE_ONLY，離線）**

整條 production path 已**真的接起來並且會跑完**：

```
Meter → Tariff/TOU → Decision → Safety Gate → Control Authority
      → Layer 1 Interlock → Executor decision → ReadBack config → Report
```

已接上的相依：ESS reader（唯讀取樣）、Meter source（唯讀訂閱）、
已人工驗收 provision 的年度離峰日、Decision Policy（功率取自 production config）、
Safety Gate、Control Authority、LastControl（**唯讀**）、Layer 1 互鎖、
ReadBack 參數（三項皆已定案）。

🔴 **刻意沒有接上的**：executor 與 verifier。OBSERVE_ONLY 一律不建立控制出口、
   不 import operator —— 因此「不送指令」是**結構事實**，不是旗標判斷。
   即使上游全部放行（決策成立、Safety 通過、Authority IDLE、互鎖未擋），
   即使必要參數日後全部補齊，也**沒有任何東西可以被呼叫**。

🔴 **連線是明確操作**：所有來源預設未注入 → 對應層 Fail Closed。
   跑測試或啟動服務都不會意外連上真機。整個組裝**不相依量測工具**。

啟動時輸出完整就緒盤點（各層是否接上、ReadBack 參數、Layer 1/2 狀態、
缺少的必要參數、`can_dispatch`），**只回報，不修補、不放行**。

每一輪 OBSERVE_ONLY 都會產出可稽核紀錄，把「為什麼沒有動作」攤平成獨立欄位：
電表狀態與新鮮度、併網流向、時段、決策與目標功率、Safety 結果、Authority 結果、
互鎖結果、派工就緒狀態、以及最終的 no-action 理由 —— 供 Phase 6.7 事後分析。

⚠️ 觀測鏈的時鐘已一路貫穿 ESS / Meter / Observer / Arbiter。
   四者若不同時基，freshness 會跨基準相減而算出無意義的 age。

**R1 —— Direction-driven Auto Report Trigger（僅設計，未接線）**

Auto Report 目前的自動啟動條件之一是「PCS 原生排程主開關為 ON」，而 Phase 6
控制的前提**恰好相反**（排程 ON 時 Authority 一律判為 EXTERNAL）。兩者互斥。
R1 的方向是讓報告 session 由**實際控制方向與生命週期**驅動，而非排程開關。
🔴 R1 **不得反過來影響控制**：報告需求不構成送出指令的理由，也不得改變
   Control Authority、dispatch 規則或 Layer 1 互鎖。
⚠️ 既有 Auto Report lifecycle 本輪**一行未改**。

**Phase D.5-C 已完成（Production 參數決策分析，離線唯讀）**

🔴 本階段**未修改任何 production 參數** —— 原則是 **EVIDENCE FIRST**，
   不是「把所有值都填成非 None」。缺依據就維持缺依據。

七項缺項的分類結果：

| 參數 | 目前值 | 狀態 |
|---|---|---|
| `charge_power_kw` | **5.0** | FINAL —— 第一版保守 baseline（已完成實機驗證） |
| `discharge_power_kw` | **5.0** | FINAL —— 同上 |
| `decision_interval_sec` | **30** | FINAL —— 約 2 個 backend refresh cycle |
| `authorization_ttl_sec` | **10** | FINAL —— 涵蓋實測單輪最壞延遲並保留餘裕 |
| `authority_ttl_sec` | **180** | FINAL —— 涵蓋一次決策週期＋最壞回讀時間 |
| `authority_power_tolerance_kw` | **1.25** | FINAL —— **綁定目前 target 5 kW**；改功率必須重新評估 |
| `max_power_kw` | **150.0** | **FINAL** —— **HMI AC active-power command upper bound**（**不是** PCS rated power） |
| `min_switch_interval_sec` | **None** | DEFERRED —— 刻意未配置，不在必要參數之內 |

🔴 現場曾觀察到的約 80 kW **不是** production 依據 —— 那是無 LastControl 的
   OBSERVED EXTERNAL / FIELD OPERATION；`max_power_kw` 亦未由 5 kW 量測值反推，
   更**未**以實機極限功率測試取得。

**`max_power_kw = 150.0` 的來源與語意**

直接來源是本機 HMI「設備控制 → 手動模式 → 併網 → 交流有功」所示的
有功功率設定範圍 **0 ~ 150 kW**，並由 operator 在送出前強制
`abs(activePowerSetPoint) <= 150`（第二層防線）。

設備能力級別的**交叉佐證**（僅作上界，不是本值來源）：原廠 Sinexcel
PWS1-160M-H-EX/NA User Manual V1.3_7.0，欄位 `Nominal Power` = **160 kVA**。
⚠️ 官方單位是 **kVA（視在功率）**，**不得**改寫成「額定有功 160 kW」。
實測 DC 匯流排 886.6~930.5 V 落在該系列範圍，排除舊 50K~250K 系列。

⚠️ **已記錄的落差（DOCUMENTED / NON-BLOCKING）**：本機銘牌 400 V / 60 Hz；
   公開手冊 EX = 400 V/50 Hz、NA = 480 V/60 Hz，皆不完全對應。
   狀態：**MODEL_FAMILY_CONFIRMED / EXACT_VARIANT_UNRESOLVED**。
   本值不由 EX/NA 額定推導，而由本機 HMI 指令範圍決定，故不受此落差影響。

🔴 **150 kW 不是操作目標**。Production 正常指令仍為 **±5 kW**；
   這只是一道 Safety Gate 上限閘，不得據以提高充放電功率或安排實機測試。

🔴 **`dispatch_ready` 現已為 True，但 `dispatch_enabled` 仍為 False。**
   參數齊備**不等於**允許派工 —— executor / verifier 仍不建立，
   模式仍為 OBSERVE_ONLY，開啟派工必須是另一次明確授權。

**參數相依鏈**（後者的合理範圍由前者決定，不可各自獨立取值）：

```
decision_interval_sec → authorization_ttl_sec → dispatch → LastControl
                                                              ↓
authority_power_tolerance_kw ← fresh observation ← authority_ttl_sec
```

必須成立的約束：`authorization_ttl_sec` **遠小於** `authority_ttl_sec`
（兩者只是名稱都有 TTL，語意完全不同：前者是「這張一次性授權票還能不能被消費」，
後者是「LastControl 這份擁有權證據還能不能被相信」）。
授權票必須足以涵蓋「決策→安全→權限→互鎖→送出」的單輪延遲，
但**不能長到**讓一個過期很久的決策仍被執行。

🔴 **`dispatch_ready` 與 `dispatch_enabled` 完全分離**：
   即使七項全部補齊使 `dispatch_ready=True`，`dispatch_enabled` 仍為 False、
   executor / verifier 仍不建立 —— 開啟派工必須是另一次明確授權。

**Phase 6.6 已完成（Auto Report Integration / R1，離線）**

**問題**：既有自動報告的建立條件是「智慧模式 ＋ 排程主開關 ON」，
而 Phase 6 自動控制的前提**恰好相反**（排程 ON 時 Control Authority 一律判為
EXTERNAL，Phase 6 不介入）。兩者互斥 → Phase 6 在控時報告永遠不會開始。

**R1 —— Direction-driven Auto Report Trigger**：報告改由**實際控制方向與擁有權**
驅動，取代「排程開關」這一個條件。其餘守門**一項都沒有放寬** ——
方向、已有 session、PCS 故障、pending 狀態、cooldown、start debounce、
Owner 檢查、主開關全部原封不動。

🔴 **未建立第二套報告系統**：完全沿用既有 session / resume / finalize /
   ownership / Named Mutex / observer 機制，report engine 一行未重寫。
   收尾本來就是**方向無關**的（idle debounce），因此 STOP → finalize 不需任何修改。

🔴 **唯一整合點是一座橋**：報告層**不** import 任何 Phase 6 模組（只多一個注入式
   hook，預設 `None`）；Phase 6 wiring 層**不** import 任何報告模組。
   兩邊只在 `phase6_report_bridge` 相遇，要拆除只需一行 `uninstall()`。

🔴 **控制與報告解耦**：橋接只**單向**餵唯讀狀態，不呼叫任何控制函式、
   不碰任何 Mutex（AST 驗證）。報告層拿不到任何可以改變控制的把手；
   提供者拋例外時報告層安全退回既有條件，控制側完全不受影響。

🔴 **擁有權判準**：Authority == `OWNED_BY_PHASE6` —— 代表 durable LastControl
   存在、信任度足夠、狀態相符、TTL 內、且已完成指令後的功率佐證。
   只有 LastControl 存在不算；只有設備在運轉更不算。
   Phase 6 宣稱的方向與設備實際方向不符 → **Fail Closed，不建立報告**。

⚠️ **OBSERVE_ONLY 期間橋接恆回 None** —— Phase 6 不會產生任何 verified
   direction，因此既有報告行為**完全不受影響**。這是目前的實際狀態。

**Phase 6.7 已完成（Production Field Validation — OBSERVE_ONLY）**

以**正式 production stack**（真實電表、真實 ESS、正式 Tariff／年度日曆／
Decision／Safety／Authority／Interlock 設定）在現場資料上連續觀察，
全程**未送出任何 PCS 指令**。

🔴 **實機驗證發現一個 production wiring 缺口並已修正**：
   `build_api_client()` 原本回傳**未登入**的 client。
   `getRunMode` / `getScheduleSwitch` / `getDOAndDIMsg` 需要認證，未登入回 **401**
   → 排程／手動開關讀不到 → ESS 觀測 `PCS_MODE_UNAVAILABLE`
   → **整條鏈永遠 Fail Closed**。行為本身安全，但 orchestrator 等於空轉，
   在真機上跑之前看不出來。已改為預設登入（登入只取讀取權限，不送任何控制），
   並補上 regression。⚠️ 登入失敗**不拋例外、不重試** —— 交由上層 Fail Closed。

修正後現場觀測正常：排程開關 / 手動開關可讀、時段判定有效、Control Authority
可判為 IDLE、決策鏈可完整走完並停在 **NO DISPATCH**。

⚠️ 觀察期間現場自然狀態為 SOC 極低且時段為尖峰，因此**未自然出現**
   CHARGE / DISCHARGE candidate —— 如實記為 **NOT OBSERVED**，
   未以人工改動電表或控制設備的方式製造情境。

Fail Closed 情境（電表陳舊／無效、時段 UNKNOWN、ESS 陳舊、通訊失敗、
Safety BLOCK、Authority UNKNOWN、OWNERSHIP_PENDING、EXTERNAL_CONTROL、
Interlock BLOCK）一律沿用既有離線 regression 驗證，**不以破壞現場設備的方式取得**。

**Phase 6.9 / 6.9-A Controlled Single-Leg LIVE（OFFLINE COMPLETE / FIRST LIVE ATTEMPTED-BLOCKED）**

第一次上線**不採**永久旗標，也**不直接進入無人值守自動排程**。

```
啟用方式：CLI  --live-leg charge|discharge  --confirm
不採：config flag／環境變數／自動恢復 LIVE
```

🔴 **`--live-leg` 不代表立即動作** —— 它只代表「本 process 最多允許執行一次
   該方向的 leg」。真正 dispatch 仍須同時滿足 Decision／Safety／Authority／
   Interlock／fresh precheck／一次性控制授權／leg 授權，**缺一不可**。

🔴 **授權不落盤**：隨 process 生命週期存在。服務重啟即消失，預設回 OBSERVE_ONLY。

🔴 **人工確認不是 YES** —— 必須輸入完整語句（例：`CONFIRM CHARGE 5KW`）。
   `--confirm` 本身只表示「願意進入確認流程」；輸入錯誤即中止，不建立任何出口。

🔴 **出口建立時機**：CLI → 人工確認 → **fresh precheck（16 項，確認後重新取得）**
   → leg 授權有效 → 才建立 executor / verifier。任一前置失敗即維持 None。

🔴 **leg 授權下推到 executor 層** —— 送出前的最後一道閘。
   若只在執行鏈跑完後才比對方向，指令**已經送出去了**；因此方向不符時
   operator 完全不會被呼叫。

🔴 **已 dispatch 後失去擁有權 → 不自動 STOP**。對 ownership 不確定的設備送 STOP
   本身也是新的控制行為，可能中斷外部操作者。一律 `ABORTED_UNCERTAIN_OWNERSHIP`
   → 禁止任何新指令 → STOP AND REPORT，交由人工處置。
   只有 Authority 明確為 `OWNED_BY_PHASE6` 且 Safety 放行，才由本 leg 正常收尾。

🔴 **LIVE 功率只能來自正式 ProductionConfig**（±5 kW）。
   CLI **沒有** `--power`，授權物件也不接受任何 power 參數；
   150 kW 仍只是 Safety Gate 上限，**不可能**成為操作目標。

leg 完成後自動：授權 consumed → executor/verifier 銷毀 → 回 OBSERVE_ONLY。

**6.9-A：CLI 入口接線（順序本身就是安全性質）**

```
啟動 service → 解析 --live-leg → 檢查 --confirm
   → 人工確認（完整語句）
   → fresh read（確認前的快照一律作廢）
   → 自然 Decision 必須自己形成該方向
   → fresh precheck／Safety／Authority／Interlock 全數通過
   → 才核發 LiveLegAuthorization
   → 才建立 executor / verifier
   → 才進 ControlledLiveLeg
```

🔴 **`--live-leg` 是 permission，不是 decision override**。
   自然 Decision 不是該方向（含 IDLE、反向、no_action、None）一律 NO DISPATCH。

🔴 **確認之前取得的任何觀測，一律不得作為 dispatch 依據**。
   確認失敗時連 fresh read 都不會發生。

🔴 **確認階段不持有授權** —— 以「意向」物件供顯示，其 armed 恆為 False，
   即使被誤傳進出口建構函式也只會得到 None（Fail Closed）。

🔴 **process 級 one-shot**：本 process 一旦核發過 leg 授權即不再核發第二次
   （不論同向、反向、前次成功與否）。one-shot 不只靠 process 邊界成立。

🔴 **LIVE 分支不進入常駐迴圈**：一個 leg 結束就結束 process，
   因此不存在「服務持續處於 LIVE」這種狀態，重啟自然回 OBSERVE_ONLY。

🔴 **模組層永不 import operator**。控制出口只在 LIVE 路徑被明確呼叫時，
   由單一函式延後取得；該延後 import 被回歸釘死在那一個函式內。

⚠️ 本階段**僅完成離線實作與回歸**，尚未執行任何實機 LIVE。

**Phase 6 技術面結案 —— 剩餘為控制權衝突，非技術缺口**

```
Phase 6.9                      OFFLINE COMPLETE / FIRST LIVE ATTEMPTED-BLOCKED
FIRST LIVE                     NOT EXECUTED
FIRST LIVE Safety Behavior     PASS
Control Authority Detection    FIELD VERIFIED
External Controller Existence  FIELD VERIFIED
External Automation Type       SEPARATE OFF-PEAK CONTROL PROGRAM / USER CONFIRMED
PCS OEM Built-in Automation    NOT THE SOURCE FOR THIS EVENT
External Program Location      RESOLVED
External Program Identity      RESOLVED
Observed Behavior              POWER-ON → 約 20 s → CHARGE
Manual-control API Match       CONSISTENT_WITH_MANUAL_CONTROL_API
                               / NOT SERVER-LOG CONFIRMED
Technical Blockers             NONE
Operational Blocker            KNOWN COMPETING EXTERNAL CONTROLLER
                               / PAUSE NOT YET AUTHORIZED
FIRST LIVE RETRY               HOLD
Production Go-Live             BLOCKED BY OWNERSHIP CONFLICT
```

**FIRST LIVE 現場實測（2026-09-01）**

第一次受控上線於絕對觀測窗口 00:00:00~00:10:00 執行。四項自然條件**首度全部成立**：

```
00:00:03 ~ 00:00:48   Authority = IDLE   PCS = STOPPED
                      TOU = OFF_PEAK     Natural Decision = CHARGE
                      通訊健康（endpoint 失敗 0，取樣遠低於既有新鮮度契約）
```

隨後設備被 Phase 6 以外的來源接管：

```
約 00:00:55   PCS 由 STOPPED 被啟動至 STANDBY
約 00:01:16   PCS 進入 CHARGING
              Authority → EXTERNAL_OR_UNKNOWN
```

同一時間**併網點 import 增加約 80 kW**，與既有的 external charging event 高度一致。
⚠️ 這是併網點量測，**不等於**「外部 command = 80 kW」。

Phase 6 的 ownership observation 即時偵測到外部控制 → **ABORT → NO DISPATCH**：

```
本次 Phase 6 實機 command：CHARGE = 0 / DISCHARGE = 0 / STOP = 0
```

🔴 **這是 Fail Closed 正確動作，不是 Phase 6 技術失敗。**
擁有權偵測依 PCS 旗標而非 AC 功率，因此在 AC 暫存器落後更新之前就已判定。

Field evidence（**MUST_KEEP**，Phase 6 驗收完成前不得刪除）：

```
output\phase6_live_charge\first_live_20260831_235901.log
output\phase6_live_charge\first_live_20260831_235901.json
```

**External Controller 已定位（2026-09-01，READ-ONLY source inspection）**

```
EXTERNAL_PROGRAM_HOST    192.168.70.201（Ubuntu 22.04.5 LTS，user etica）
EXTERNAL_PROGRAM_NAME    /home/etica/ems/auto_control.py
RUNTIME                  screen 1128.auto → /bin/bash → python3 auto_control.py
SCREEN_1128_AUTO         CONFIRMED
AUTO_CONTROL_PY          CONTROLLER CONFIRMED
BESS_REQUEST_PY          PCS/HMI API CLIENT CONFIRMED
CONTROL_TRIGGER          OFF-PEAK + METER + SOC + DEMAND
PAUSE_METHOD             built-in "stop"  / CONFIRMED / NOT YET EXECUTED
                                          / NOT YET AUTHORIZED
RESTORE_METHOD           built-in "start" / CONFIRMED / NOT YET EXECUTED
PAUSE_VERIFICATION       DEFINED
RESTORE_VERIFICATION     DEFINED
```

🔴 該 controller 的 `stop` **不等於** PCS 的 STOP 指令 —— 它停的是 automation loop
並把 active power command 收斂到 0，PCS 可能停在 STANDBY 而非 STOPPED。
Phase 6 本來就接受兩者皆為 legal idle（precheck `pcs_idle`、Layer 1
`DIRECTION_ISOLATED_STATES`），因此**不需要為此修改任何 production 邏輯**。

⚠️ 交叉驗證：該 controller 採 `negative setpoint = CHARGE` / `positive = DISCHARGE`，
與本專案記載的第三種 kW 語意（PCS Command）**完全一致**；其 `MIN_SOC=2 / MAX_SOC=98`
亦與現場觀測到的 SOC 擺盪邊界吻合。兩者為獨立來源的相互佐證。

**下一步不是重新排一次 FIRST LIVE。**

外部控制程式若未先暫停，重排只會再次得到相同的 ABORT。順序為：
使用者明確核准 temporary pause → fresh read-only precheck → built-in `stop`
→ PAUSE_VERIFICATION → ownership observation → fresh Phase 6 precheck
→ FIRST LIVE retry → 完成後 built-in `start` → RESTORE_VERIFICATION。

`PAUSE_VERIFICATION` 最關鍵 —— 暫停後須持續觀測，確認 PCS 維持 legal idle、
功率收斂至約 0、Authority 維持 IDLE、無外部 LastControl、PCS 不自行 Power-On
或充放電、通訊健康、fault/alarm clean。

**充電時間窗（HISTORICAL OBSERVATION ONLY）**

2026-09-01 夜間實測：外部 controller 以約 80 kW 充電，SOC 由 2% 上升至 85%
約耗時 2 小時 40 分（約 5% / 10 分鐘）。因此**當日**可用的 FIRST LIVE charge
window 約為離峰開始後的前 2.5 小時。

🔴 **THIS IS HISTORICAL OBSERVATION ONLY.**
`00:00 ~ 02:40` **不是** production hard-coded window，也**不是** Safety Gate
或 production rule。FIRST LIVE 的真正判斷條件一律使用 **fresh SOC**：

```
SOC <= 85%  → charge latch 可成立
SOC >  85%  → natural decision 不應強制 CHARGE
```

選擇離峰開始後 00:00~00:30 只是**較佳的 operational window**（SOC 最低、餘裕最大），
不得寫入任何判定邏輯。

Non-blocking：Blocker 10（同向目標功率變更語意）、Layer 2 DEFERRED、
`test_pcs_modes` 既有 regression debt、CHARGE/DISCHARGE candidate 自然現場觀測
NOT OBSERVED。

**Phase 6.8 已完成（Regression & Closure）**

把散落在各階段的關鍵不變量收斂成**單一結案閘門**（21 條，A~U）。
本階段**未新增任何功能**。

最終閘門的核心命題：

> **所有必要參數皆已就緒，但結構上仍不可能送出任何指令。**

```
dispatch_ready   = True      ← 9 項必要參數全部就緒
dispatch_enabled = False     ← 未啟用
mode             = OBSERVE_ONLY
executor         = None      ← 不建立，非「有出口但被擋住」
verifier         = None
can_dispatch     = False
```

三者互相獨立且同時成立 —— 參數齊備**不等於**允許派工。
非 OBSERVE_ONLY 的模式（DISPATCH / LIVE / ACTIVE）一律拒絕建立出口。

**功率雙層防線**：Safety Gate 於 150.0 kW 放行、150.1 kW 起 BLOCK（充放電雙向）；
operator 在送出前另有獨立的 0~150 kW 範圍檢查，兩層皆不可繞過。
Production 操作功率仍為 **±5 kW**，遠低於上限。

**Fail Closed 全覆蓋**：時段 UNKNOWN、電表無資料／`demand_state=fault`、
ESS 通訊失敗／陳舊、Authority 非 IDLE/OWNED、EXTERNAL_CONTROL —— 一律未授權、0 calls。
`OWNERSHIP_PENDING` 為獨立 runtime 狀態，不在派工集合內。

**責任分離**：報告層不 import 任何控制模組、不含任何控制指令字串；
控制層不 import 任何報告監看模組；橋接未建立第二套 report engine，
既有 session 入口仍是唯一的一個，收尾仍由既有單一決策點負責。
未安裝橋接時 scheduler 舊路徑**完全不退化**。

**相依性稽核**：16 個 production path 模組**全部**不相依量測工具；
5 個時段／日曆模組**全部**無網路相依；年度資料為靜態已驗收資料，
不掃描目錄、未涵蓋年度不 fallback。

🔴 **Phase 6.8 全數通過 ≠ Go-Live**。目前狀態為
**TECHNICALLY READY FOR EXPLICIT LIVE AUTHORIZATION** ——
`dispatch_enabled` 的切換必須是另一次獨立、明確的裁示。

控制服務的例外處理原則與報告服務**相反**且不得照抄：報告監看遇到例外必須續行
（監看不得停擺），控制 runtime 遇到未知例外一律 **Fail Closed**，不再產生任何指令。

關閉語意為 **relinquish authority only**：停止軟體服務**不等於**停止 PCS，
正常停止／系統更新重啟／人工停止一律不隱含送出停止充放電指令。

稽核事實：現行 production 執行路徑（報告監看服務、自動排程監看、人工控制選單、
既有控制程式）**沒有任何一個**在模組層或函式層 import 任何 Phase 6 模組；
Phase 6 的決策鏈（電表 → 分類 → 決策 → 政策 → 控制整合 → 方向互鎖 → Safety Gate →
Control Authority → Executor）目前**不存在**任何 production 呼叫端。
Executor 亦從未在 production 被實例化，且未注入控制程式時結構上不可能接上真機。

因此：各模組離線回歸全數通過**不等於** Phase 6 已可自動運轉。
在 Production Orchestrator 完成之前，Phase 6 不得被視為具備自動控制能力。

控制路徑分類（唯讀稽核，本輪未變更任何行為）：

| 路徑 | 性質 | 是否經過 Phase 6 方向互鎖 / Safety Gate / Authority |
|---|---|---|
| 人工控制選單 → 既有控制程式 | 人工，**刻意獨立** | 否（人工逐次確認，非自動控制） |
| Field Measurement 量測工具 | 量測 harness，非 production | 部分（有 Authority 與 Safety Gate，**無**方向互鎖） |
| Phase 6 自動控制 | **尚未接通**（D.3-A 骨架已建立，無 dispatch 路徑） | 設計上必經，但目前無呼叫端 |
| 報告監看 / 自動排程監看 | 純唯讀，無控制能力 | 不適用 |

⚠️ Field Measurement 驗證成功**不代表** Production Path 已接通；兩者是不同路徑。
⚠️ **既有自動報告與 Phase 6 自動控制在觸發條件上互斥**：既有自動報告要求
   排程主開關 **ON**，而 Phase 6 自動控制的 Control Authority 要求排程主開關 **OFF**。
   因此自動控制期間，既有的排程式自動報告永遠不會被觸發 —— 報告整合需要另一條
   由「實際方向」驅動的觸發來源（設計中，尚未實作）。
⚠️ 人工控制選單屬 operator 直接控制工具，與 Phase 6 自動控制刻意分離，
   本輪未將 Phase 6 互鎖強加於人工路徑。

OPEN DESIGN ITEM（尚未實作，登記備查）：`LastControlStore` 的紀錄與 Safety Gate 的
`LastControl` 之間目前**沒有** production adapter。未來啟用時間互鎖前，必須先確認
紀錄可用於間隔判定（同一 boot、monotonic 可比較）才可轉換；**禁止**跨 boot 的
monotonic 相減。時間互鎖未取得正式門檻前不接線。

PRE-EXISTING REGRESSION DEBT（與 Phase 6 無關，另案處理）：`test_pcs_modes.py` 的
fixture 缺少排程開關的手動模式欄位，導致解析回「未知（狀態無法確認）」。
不得為了讓回歸全綠而修改 production parser 或降低既有斷言。

合理 SOC 的 DISCHARGE → STOP 實機驗證**已完成**（Blocker 1 因此 CLOSED）。
目前**沒有**已核准的下一個實機控制階段；剩餘 Blocker 以唯讀分析為主。

實機控制的前置條件（任一不成立即不得送出控制）：設備閒置、功率回基準、SOC 落在
Decision Policy 允許該方向的運轉帶內、無故障與作用中告警、排程主開關 OFF、
Control Authority 為 IDLE，且確認無其他控制來源。
⚠️ 驗證用的 SOC 目標窗口**不是 production SOC policy**；Safety Gate 的硬限制與
Decision Policy 的運轉帶語意不同，兩者不得混用。
⚠️ SOC 恢復由現場既有正常作業執行，**Phase 6 不執行 SOC Recovery**。

</details>

<details>
<summary><b>Phase 6.10 詳細設計紀錄（State Machine、Ownership Model、各子階段驗收、參數推導）</b></summary>

#### 11. Phase 6.10 — Unattended Ownership Handoff

##### 11.1 目標

讓系統未來能在**無人工操作**下完成一次完整交接：

```
External Controller → safe pause → ownership release → Phase 6 takeover
→ control → Phase 6 release → External Controller restore
```

四項核心原則：

- **不允許雙重 controller** —— 任何時刻只能有一方持有控制權
- **Fail Closed** —— 任一 Gate 不成立即停止前進，不猜、不續跑
- **Crash 後保留 restore responsibility** —— 重開機不得讓恢復責任消失
- **所有 handoff 留下 durable audit evidence**

##### 11.2 State Machine

```
IDLE → PREFLIGHT → PAUSE_REQUESTED → PAUSE_VERIFYING → EXTERNAL_PAUSED
     → PHASE6_ACTIVE → PHASE6_RELEASING → PHASE6_RELEASED
     → RESTORE_REQUESTED → RESTORE_VERIFYING → RESTORED → COMPLETE
```

失敗／Critical 狀態：

```
ABORTED_PREFLIGHT              尚未動任何東西，無恢復責任
ABORTED_PAUSE_FAILED           pause 未生效，external 仍在跑
RESTORE_FAILED_CRITICAL        🔴 場站失去套利，需人工介入
OWNERSHIP_CONFLICT_CRITICAL    🔴 雙方同時持有
```

轉移採**白名單**，非法轉移直接拋錯。`EXTERNAL_PAUSED` 不得直接跳 `COMPLETE` ——
必須經過歸還。

##### 11.3 Ownership Model

```
EXTERNAL / NEITHER / PHASE6 / UNKNOWN / BOTH
```

🔴 **`BOTH` 不是正常 operational state，而是 INVARIANT VIOLATION / CRITICAL。**
它不出現在任何狀態的可接受集合中，一旦偵測到即寫入稽核並轉 critical。
任一側無法判定 → `UNKNOWN`（Fail Closed），不猜測。

##### 11.4 Phase 6.10 Core — COMPLETE

已完成並離線驗證：State Machine、Crash Recovery、Watchdog、Idempotency、
append-only JSONL Audit Log、DRY_RUN / OBSERVE / ARMED 三段模式架構。

幾項語意值得記錄：

- **意圖先寫、結果後寫** —— crash 落在中間時必須**以觀測驗證**，不得盲目重送
- **Watchdog 逾時不會直接 restore** —— Phase 6 仍在 dispatch 時強行恢復
  external 會造成雙方同時控制，因此逾時只會要求「先收斂，再歸還」
- **恢復責任由 8 個狀態承載**，並以 durable journal + `recover()` 跨重開機保留

```
DISPATCH_ENABLED = False       MODE = DRY_RUN
```

##### 11.5 Remote Adapter — Phase 6.10-A COMPLETE

remote adapter 提供三個注入點：`probe` / `pause_sender` / `restore_sender`。
pause / restore sender **介面已備妥，但 production control 尚未啟用**
（預設 `armed=False`，未注入 runner 即結構上送不出）。

🔴 **`external_quiescent` 不得宣稱直接觀測到 `auto_control.py` 的
`running == False`** —— 那是行程內部變數，外部讀不到。只能以外部可觀測行為
（PCS 狀態、功率、Authority、通訊、告警、狀態穩定性）跨多筆樣本驗證。

##### 11.6 Pause Verification — Phase 6.10-A1 COMPLETE

兩階段：

```
SETTLING        允許正常收斂，**不判失敗**：
                  CHARGING / DISCHARGING → STANDBY / STOPPED
                  Authority EXTERNAL_OR_UNKNOWN → IDLE
                  Power active → idle band
                逾時仍未出現 candidate-idle → SETTLING_TIMEOUT

STABLE_VERIFY   首次出現完整 candidate-idle 後才開始；
                連續 fresh samples 全部成立才 external_quiescent = True
```

PCS 狀態穩定性**只在 STABLE_VERIFY window 內要求** —— SETTLING 期間的
`CHARGING → STANDBY` 是正常收斂，不得因此判定 pause 失敗。

Restore 驗證同樣兩層：runtime health（ssh / screen / pid / identity）＋
loop activity（時間戳前進）。**不要求 PCS 一定出力**；證據不足一律
`NEEDS_ADDITIONAL_PROBE`，不得假裝 PASS。

##### 11.7 已核准 handoff 參數

| 參數 | 值 |
|---|---|
| `idle_power_lower_kw` | −3.0 |
| `idle_power_upper_kw` | +1.0 |
| `pause_settling_timeout_sec` | 120.0 |
| `stable_verify_min_samples` | 4 |
| `stable_verify_interval_sec` | 15.0 |
| `stable_verify_min_span_sec` | 45.0 |
| `restore_loop_min_observations` | 3 |
| `restore_loop_interval_sec` | 3.0 |
| `restore_loop_min_span_sec` | 6.0 |

span 一律等於 `(samples − 1) × interval`（4@15s → t=0/15/30/45 → 45 s；
3@3s → t=0/3/6 → 6 s），並由 `validate()` 檢查一致性以防 off-by-one。

🔴 **參數已寫入 ≠ 實機 unattended control 已啟用。** 仍維持
`DISPATCH_ENABLED = False`、`MODE = DRY_RUN`。

##### 11.8 Remote Guard — Phase 6.10-B1 COMPLETE

```
dedicated key   phase6_firstlive_ed25519（ED25519，專用）
來源限制        from="192.168.128.234"
forced command  /home/etica/ems/phase6_remote_guard.sh
```

目前 deployed allowlist：

```
ENABLED   probe | loopcheck | status
REFUSED   pause | restore | 任意 shell | kill | screen 任意指令
```

`authorized_keys` 中該 public key **恰好 1 條** forced-command entry ——
必須 REPLACE 而非 APPEND，否則舊的無 forced-command 條目仍在，allowlist
就完全失去意義。

guard 本身：`set -euo pipefail`、PATH 與環境淨化、絕對路徑、每個動作有 timeout、
明確 exit code、精確比對（非前綴）、不使用 eval、不把原始 command 交給 `sh -c`、
screen 以 **session name** 定位（不寫死 PID）且要求恰好 1 個、hardcopy 用
`mktemp` + `trap` 清理。

##### 11.9 FIRST LIVE Handoff Plan

```
 1 PRE_PAUSE_PREFLIGHT
 2 PAUSE                    → RESTORE RESPONSIBILITY = TRUE
 3 SETTLING                 ≤ 120 s
 4 STABLE_VERIFY            4 samples @ 15 s，span ≥ 45 s
 5 ownership = NEITHER
 6 POST_PAUSE_FRESH_PRECHECK
 7 FIRST LIVE CHARGE 5.0 kW
 8 VERIFY                   ReadBack / LastControl / Phase 6 Authority
 9 PHASE6 RELEASE           → idle verified → ownership = NEITHER
10 RESTORE external
11 RESTORE_VERIFY           3 observations @ 3 s，span ≥ 6 s
12 COMPLETE
```

🔴 **`PRE_PAUSE_PREFLIGHT` 不要求 `Authority = IDLE`、`PCS idle`、
`No external controller`** —— 暫停之前，external controller 本來就可能正在
正常控制，那是預期狀態而非 failure。這三項**只有 `POST_PAUSE_FRESH_PRECHECK`
才必須 PASS**，且不得沿用 Step 1 的資料。

🔴 **RESTORE 完成後不要求 ownership 一定為 `EXTERNAL`。** 若 external policy
此刻正確選擇 idle（`ownership = NEITHER`），仍可 `COMPLETE`，前提是：
Phase 6 ownership released、external runtime healthy、external loop resumed、
`ownership != BOTH`、no critical condition。

##### 11.10 Network Identity Blocker — Phase 6.10-B1.6 COMPLETE / BLOCKER CONFIRMED

```
Wi-Fi MAC     04-EC-D8-6A-46-4A
IPv4          192.168.128.234（PrefixOrigin = Dhcp）
DHCP Server   192.168.128.16
DHCP Reservation = NOT CONFIRMED
```

```
UNATTENDED_NETWORK_IDENTITY_STABILITY = NOT VERIFIED
DHCP Reservation                      = NOT CONFIRMED
Phase 6.10-B1.7                       = DEFERRED / NOT VERIFIED
```

🔴 **`NOT VERIFIED` 不是 `PASS`。** B1.7 的欄位驗證（受控 reconnect / DHCP renew
後重新確認 IP 與 guard）**尚未執行**，不得以「歷史 renew 都拿到 .234」推論為已驗證。

⚠️ 正確描述：**DHCP 會週期性 renew，但因 reservation 尚未確認，無法保證
reconnect / DHCP restart / lease reassignment 之後仍取得 `.234`。**
（不可寫成「12 小時租約一定每 12 小時換 IP」。）

風險路徑：external 已 pause → Windows IP 改變 → `from="192.168.128.234"`
拒絕 SSH → restore 送不出去 → `RESTORE_FAILED_CRITICAL`。

🔴 **不得**以 `from="192.168.128.0/24"` 作為解法 —— 那只是擴大 dedicated key
的來源授權範圍，並未建立 stable network identity。unattended production 要的是
**stable identity**，不是 **broader authorization scope**。

需 IT 於 `192.168.128.16` 建立 reservation：MAC `04-EC-D8-6A-46-4A`
→ IPv4 `192.168.128.234`（**是 DHCP Reservation，不是 Windows 端 Static IP**）。

##### 11.11 Current Gate

| 階段 | 狀態 |
|---|---|
| Phase 6.9 | FIRST LIVE RETRY HOLD |
| Phase 6.10 Core | COMPLETE |
| Phase 6.10-A | COMPLETE |
| Phase 6.10-A1 | COMPLETE |
| Phase 6.10-B1 | COMPLETE |
| Phase 6.10-B1.5 | COMPLETE |
| Phase 6.10-B1.6 | COMPLETE / BLOCKER CONFIRMED |
| Phase 6.10-B1.7 | **DEFERRED / NOT VERIFIED** |
| Phase 6.10-B2 OFFLINE | **COMPLETE** |
| Phase 6.10-B2 LIVE | **NOT STARTED / BLOCKED** |
| Phase 6.10-C OFFLINE | **COMPLETE** |
| Phase 6.10-C LIVE DEPLOYMENT | **NOT STARTED / BLOCKED** |

```
Live blockers
  NETWORK_IDENTITY_STABILITY   NOT VERIFIED
  DHCP Reservation             NOT CONFIRMED
  Remote deployed guard        B1（pause / restore = REFUSED）
  retry_backoff_sec            UNRESOLVED
  service_health_timeout_sec   UNRESOLVED
  FIRST LIVE                   HOLD
```

FIRST LIVE / unattended live 之前，**必須**重新補做 B1.7，或以其他經驗證的
stable network identity 方案解除此 blocker。

B1.7 通過後才進 B2；即使進 B2，仍先維持 `DISPATCH_ENABLED = False` /
`MODE = DRY_RUN`，先驗證 pause / restore 安全成立，再做 Controlled FIRST LIVE
5 kW，之後才考慮 Phase 6.10-C Unattended Service，最後才可能
`MODE = ARMED` / `DISPATCH_ENABLED = True`。

##### 11.13 Phase 6.10-B2 — OFFLINE DEVELOPMENT / TEST ONLY

B2 的程式與離線測試已完成；**實機 pause / restore 未授權，guard 亦未部署**。

**HandoffRunner** —— 依 §11.9 的 12 步驅動整個交接，全部相依由注入取得：

```
1 PRE_PAUSE_PREFLIGHT   11 項；刻意**不含** authority_idle / pcs_idle /
                        no_external_controller（以 PRE_PAUSE_MUST_NOT_REQUIRE
                        常數固定，回歸直接驗）
2 PAUSE                 送出後 restore_responsibility = True
3+4 SETTLING/STABLE_VERIFY  由 PauseVerifier 以已核准參數驅動
5 OWNERSHIP = NEITHER
6 POST_PAUSE_PRECHECK   **這一階段才**要求那三項，且不得沿用 Step 1
7 FIRST LIVE            受 DISPATCH_ENABLED 擋下（目前恆為 False）
8~9 VERIFY / RELEASE
10~12 RESTORE / RESTORE_VERIFY / COMPLETE
```

🔴 **不存在「漏歸還」的路徑** —— 只要 Step 2 曾成功，之後任一失敗都由統一的
`_bail()` 導向歸還流程。離線測試涵蓋 Step 6 失敗、STABLE_VERIFY 期間出現
external evidence、probe 異常三種情境，三者皆確認仍送出 restore。

🔴 **restore 失敗（送不出或 loop 未恢復）→ `RESTORE_FAILED_CRITICAL`，
且恢復責任不解除。**

**Remote Guard B2** —— 已實作 `pause` / `restore` 兩個動詞，沿用 B1 全部強化，
且兩者**皆必須先通過** `require_single_screen` 與 `require_identity`
（B1 的 `probe` 刻意不強制，因為 probe 本就是用來診斷當下狀態；但控制動詞不同 ——
對身分不確定的目標送控制指令不可接受）。

```
DEPLOYED_GUARD_VARIANT = "B1"    ← 遠端實際部署仍是唯讀動詞版本
```

模組中存在 `REMOTE_GUARD_B2_SH` **不代表已部署**。部署需另行授權，且必須
REPLACE `authorized_keys` 的既有條目。

##### 11.14 Phase 6.10-C — Unattended Service

```
Phase 6.10-C OFFLINE           COMPLETE
Phase 6.10-C LIVE DEPLOYMENT   NOT STARTED / BLOCKED
SERVICE_MAIN_LOOP              IMPLEMENTED / OFFLINE VERIFIED
```

**主迴圈**

```
BOOT → RECOVER → STARTUP GATES → OBSERVE → EVALUATE ELIGIBILITY
     → HANDOFF RUNNER → AUDIT → LOOP
```

只有 `recovery = CLEAN` 才可進入正常 observation loop。

**Startup Recovery**

```
CLEAN            → 允許正常 loop
PENDING_RESTORE  → 禁止新 handoff，承接既有 restore responsibility
CRITICAL         → 拒絕 live operation
UNKNOWN          → Fail Closed
```

🔴 Windows reboot / Service restart **不得**把 ownership 或 restore
responsibility 重設為 IDLE / NONE。

**Live Gate（10 項，任一不成立即 REFUSED）**

```
MODE == ARMED                       DISPATCH_ENABLED == True
NETWORK_IDENTITY_STABILITY == PASS  REMOTE_GUARD_VARIANT == B2
pause capability == PASS            restore capability == PASS
recovery == CLEAN                   local data fresh
remote probe fresh                  FIRST LIVE prerequisite satisfied
```

現場實況：

```
DISPATCH_ENABLED           False
MODE                       DRY_RUN
NETWORK_IDENTITY_STABILITY NOT VERIFIED
DEPLOYED_GUARD_VARIANT     B1
pause_capable              False
restore_capable            False
FIRST LIVE                 HOLD
→ live_handoff_allowed 必定 False
```

⚠️ `remote_pause_capable()` / `remote_restore_capable()` 依**實際部署的 guard
版本**判定 —— 原始碼中存在 `REMOTE_GUARD_B2_SH` 不代表現場具備 B2 能力。

**Single Instance**

只允許單一 unattended orchestrator。判斷**不只使用 PID**，而是
`pid` + `boot_id` + `cmdline` 三者確認，並處理四種情境：

```
stale PID              前行程已死          → 可接手
PID reuse              PID 活著但 cmdline 不符 → 視為 stale，可接手
peer liveness unknown  死活不可知          → 拒絕啟動（不假設已死）
corrupt lock           lock 檔損毀         → 拒絕啟動
```

第二 instance 不得啟動，且**不寫入 `SERVICE_START`**。
production 應改用本專案既有的 Windows Named Mutex 機制（可注入）。

**Durable Journal**

append-only JSONL，必要事件八種：

```
SERVICE_START / SERVICE_SHUTDOWN / RECOVERY_RESULT / ELIGIBILITY_DECISION
HANDOFF_BEGIN / PHASE6_TAKEOVER / PHASE6_RELEASE / CRITICAL_FAILURE
```

加上既有的 pause / restore `INTENT` / `OUTCOME`。restart 不清除、seq 單調遞增、
partial tail 可容忍，且**不得因資料不足而猜測 ownership**。

**Restore Responsibility**

只要 external pause **可能已發生**，責任就必須保留到 `RESTORED`。
exception / process crash / Windows reboot / service restart / SSH failure
都不得讓責任消失。

🔴 **本輪修正的真實缺陷**

crash 若發生在「PAUSE INTENT 已 durable」但「STATE 尚未寫入」之間，
舊 `recover()` 的 `last_state()` 會回 `None` → **可能誤判 CLEAN**，
服務照常啟動，但 external 其實可能已被暫停。

已新增 `_has_unresolved_pause()`：只要存在 PAUSE `INTENT` 且其後沒有成功的
RESTORE `OUTCOME`，即視為可能仍背負責任，再配合 remote probe 判為
`PENDING_RESTORE` / `CLEAN` / `UNKNOWN` —— **不得直接假設 CLEAN**。
此修正已由 crash tests 驗證（八個切點）。

**Graceful Shutdown**

```
無 responsibility                        → CLEAN
Phase6 active → release → verify idle
  → restore external → verify loop resumed → CLEAN
restore 送不出 / loop 未恢復              → RESTORE_FAILED_CRITICAL
```

🔴 **不得記成 clean shutdown。** DRY_RUN 下若 journal 顯示存在真實 restore
responsibility，因禁止實機 sender，**也不得假裝已成功 restore** ——
同樣判為 CRITICAL。

**Crash Recovery**

八個切點皆驗證：pause INTENT 後 / pause success 後 / SETTLING /
STABLE_VERIFY / PHASE6_ACTIVE / PHASE6_RELEASED / restore INTENT 後 /
restore outcome 未落盤。每一個都 boot REFUSED、承接責任，且
**pause / restore sender 呼叫數皆為 0（不盲目重送）**。

**DRY_RUN E2E**

```
boot READY → 5 loop ticks → shutdown CLEAN
pause sender = 0 / restore sender = 0 / dispatcher = 0
```

即使 `eligibility = True`，只要 `live_handoff_allowed = False` 仍為 `NO_HANDOFF`
—— 兩層判定彼此獨立。

**Scheduling**

無硬編碼 `00:05` / `00:15` / `00:30`（AST 排除 docstring 後掃 HH:MM，
兩個模組命中皆為 0）。eligibility 一律由 Asia/Taipei TOU、fresh SOC、
Natural Decision、Authority、PCS、Meter、fault/alarm 決定。
**歷史 operational window 不得成為 production schedule。**

**Polling / Backoff**

```
decision_interval_sec       30.0     沿用既有已核准 production 值
retry_backoff_sec           None     RETRY_BACKOFF = UNRESOLVED
service_health_timeout_sec  None     SERVICE_HEALTH_TIMEOUT = UNRESOLVED
```

後兩者目前沒有足夠 field evidence 支持 production 數值，因此**刻意留 None
並列入 `missing()`**，不自行填值。它們不是 C Offline 的完成 blocker，
但屬於 **C Live Deployment 前待定的 production parameters**。

**Critical States**

```
OWNERSHIP_CONFLICT_CRITICAL / RESTORE_FAILED_CRITICAL
RECOVERY_UNKNOWN            / REMOTE_IDENTITY_MISMATCH
```

共同規則：禁止 new handoff、禁止 blind retry control、寫 durable critical
event、Fail Closed。

**測試**

| 測試 | 結果 |
|---|---|
| `test_phase6_610c_service.py` | 102 / 102 PASS |
| `test_phase6_610b2_handoff_runner.py` | 77 / 77 PASS |
| `test_phase6_610a_remote_adapter.py` | 83 / 83 PASS |
| `test_phase6_610_handoff.py` | 145 / 145 PASS |
| **Phase 6 full regression** | **3694 / 3694 PASS，39 檔，0 FAIL 檔** |

**Live Deployment Blockers**

```
NETWORK_IDENTITY_STABILITY   NOT VERIFIED（B1.7 DEFERRED）
DHCP Reservation             NOT CONFIRMED
Deployed remote guard        B1 —— pause / restore = REFUSED
retry_backoff_sec            UNRESOLVED
service_health_timeout_sec   UNRESOLVED
FIRST LIVE                   HOLD（外部控制程式仍在運行）
```

以上任一未解除，`live_handoff_allowed()` 即為 False。
**不得**以「離線測試全過」為由部署為常駐服務。

**本輪安全狀態**

```
DISPATCH_ENABLED   False              MODE            DRY_RUN
Windows Task       未建立              Service / NSSM  未安裝、未啟動
實機 Phase 6       CHARGE / DISCHARGE / STOP = 0 / 0 / 0
External           stop / start / pause / restore = 0 / 0 / 0 / 0
```

##### 11.15 Current Safety State

```
Remote Guard          probe / loopcheck / status = ENABLED
                      pause / restore            = REFUSED
DISPATCH_ENABLED      False
MODE                  DRY_RUN
PAUSE_AUTHORIZATION   NOT GRANTED
FIRST LIVE RETRY      HOLD

實機 Phase 6          CHARGE / DISCHARGE / STOP = 0 / 0 / 0
External              stop / start              = 0 / 0
```

</details>
