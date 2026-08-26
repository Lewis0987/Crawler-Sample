# Crawler Sample — HMI / BESS 資料擷取與設備控制工具

## 1. 專案簡介

儲能櫃（BESS）HMI 的資料擷取（爬蟲）＋設備控制工具集，
並提供**自動充放電報告**與**背景監控服務（Auto Monitor Service）**。

| 項目 | 值 |
|---|---|
| API Base | `http://192.168.128.110:8080/admin-api` |
| 前端 HMI | `http://192.168.128.110:8853/`（Vue，Yudao/ruoyi-vue-pro） |
| 輸出目錄 | `D:\Crawler Sample\output` |

## 2. 主要功能

- **設備資料擷取** —— 電池／PCS／空調／進排風／冷卻循環等區塊，逐欄對齊 HMI UI。
- **PCS／排程控制** —— CLI 選單操作，控制指令需二次確認。
- **自動充放電報告** —— 智慧排程開始充放電時自動建立 Session，結束時自動收尾產表。
- **Windows Service 背景監控** —— 不開 Dashboard 也能自動建立與收尾報告。
- **Session Recovery** —— Service 正常停止或異常中斷後，重啟可續接同一份 Session。
- **Auth Recovery** —— 登入態失效時自動重新登入，不需人工介入。
- **Ownership / Dashboard Observer** —— 同一時間只有一個寫入者，Dashboard 為唯讀觀察者。

## 3. 主要檔案

| 檔案 | 用途 |
|---|---|
| `api_client.py` | API 薄封裝：session、登入、`Authorization: Bearer`、統一 timeout。 |
| `dashboard_scraper.py` | Dashboard 數據概覽擷取；輸出 `dashboard_data.json` / `.csv`。 |
| `device_control_scraper.py` | 共用解析核心（PCS／電池／環控）；輸出 `device_control_readonly.json`。 |
| `device_control_menu.py` | CLI 設備控制選單（查詢＋控制）。 |
| `device_control_operator.py` | 控制指令的 payload 組裝與送出（需 `--execute` + YES）。 |
| `report_monitor.py` | Monitor Core：監看決策、Session 生命週期、Ownership、Auth Recovery。 |
| `auto_monitor_service.py` | 背景服務入口，由 NSSM 啟動並常駐。 |
| `charge_discharge_report.py` | `ReportSession` 與報告產出（CSV／Excel／Summary／統計）。 |
| `run_phase3_regression.py` | 一鍵執行完整 Regression 並輸出總表。 |
| `run_all.py` | 一鍵執行全部擷取腳本。 |

## 4. 執行方式

```bash
python device_control_scraper.py     # 產生 device_control_readonly.json + 主控台摘要
python dashboard_scraper.py --once   # Dashboard 抓一次 → dashboard_data.json / .csv
python dashboard_scraper.py --loop   # Dashboard 背景循環（Ctrl+C 停止）
python device_control_menu.py        # CLI 設備控制選單
python run_all.py                    # 一鍵執行全部擷取
python run_phase3_regression.py      # 完整 Regression
```

### 登入設定

登入資訊放在 `test/` 目錄下的 `*.env` 檔案（例如 `login.env`）。
可依 `.env.example` 建立本機 env 檔。

**`.env` 檔案已由 Git 排除，請勿提交。**

## 5. PCS 當前狀態

PCS 當前狀態依 HMI 實際顯示邏輯解析。

- 使用 US 版本 `pcsMode_US` 規則
- 顯示順序：啟停 → 故障 → 併網 → 離網 → 充電 → 放電
- 啟停狀態固定顯示，其餘狀態於 `oldValue=1` 時顯示
- 共用解析位於 `device_control_scraper.py`
- 顯示結果需與 HMI Dashboard 一致

```
PCS當前狀態：執行 / 併網 / 充電
```

自動監看層的旗標判斷一律讀 `oldValue`（語言無關），不比對翻譯後的中文字串。

## 6. 自動充放電報告

```
排程開始 → 建立 Session → 持續取樣 → 自動停止 → 產生報告
```

輸出位置：`output\charge_discharge_reports\<session_id>\`

| 檔案 | 內容 |
|---|---|
| `samples.csv` | 逐筆取樣資料 |
| `events.csv` | Session 事件（開始／方向切換／暫停／續接／結束） |
| `summary.json` | 報告摘要 |
| `statistics.json` | 統計數據 |
| `report.xlsx` | Excel 報告（含圖表） |

另有 `alarms.csv`、`cell_snapshots.json`、`session_state.json`。

## 7. Auto Monitor Service

```
Windows SCM → NSSM → auto_monitor_service.py → report_monitor.py → Session / Report
```

- Service 可在 **Dashboard 未開啟時獨立監控**。
- Service 為 **Monitor Owner**；Dashboard 為 **Observer**（唯讀，不寫報告）。
- `tools\nssm.exe` 是 **runtime dependency**，安裝後不可刪除、改名或搬移。
- Windows Service 限制 OpenBLAS 為單執行緒，以降低背景服務資源占用。

以**系統管理員** PowerShell 操作（`install` / `update` / `start` / `stop` /
`status` / `remove` 六個動作）：

```powershell
.\tools\install_service_nssm.ps1 -Action status    # 查詢
.\tools\install_service_nssm.ps1 -Action start     # 啟動
.\tools\install_service_nssm.ps1 -Action stop      # 停止
```

| 情境 | 流程 |
|---|---|
| 首次安裝 | `install` → `start` |
| Service 設定更新 | `stop` → `update` → `start` |
| 純 Python 程式碼更新 | `stop` → 更新 `.py` → `start` |

Service log：`output\logs\auto_monitor_service.log`

> 六個動作的完整說明、預期結果與失敗處置見
> [`../tools/README.md`](../tools/README.md)（Service action 權威來源）。
> 部署、設定確認、健康檢查、Rollback、Troubleshooting 見
> [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md)。

## 8. Recovery

**Graceful Recovery**（正常停止）

```
recording → Service stop → paused → Service start → resume
```

**Crash Recovery**（異常中斷）

```
recording → worker 中斷 → Service start → resume
```

兩者的共同結果：

- 同一 `session_id`
- 同一 folder
- 舊 samples 完整保留
- `sample_index` 繼續遞增
- **不建立第二份 Session**

> 兩條路徑的實機驗證證據與底層機制見
> [`../docs/Phase4_Closure_Report.md`](../docs/Phase4_Closure_Report.md)。

## 9. Regression

目前正式 baseline：

```
Automated checks : 1802 / 1802 PASS
FAIL             : 0
SKIP             : 0
Environment      : PASS
```

執行：`python run_phase3_regression.py`

## 10. 開發階段

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
| 6.5 | PCS Control Integration（PCS 自動充放電控制） | **READY / Go-Live BLOCKED** |
| 6.5-H | Control Authority / Command Arbitration（控制權判定） | IMPLEMENTED / OFFLINE VERIFIED |
| 6.6 | Auto Report Integration（自動報告整合） | PENDING |
| 6.7 | Field Validation（實機驗證） | PENDING |
| 6.8 | Regression & Closure（完整回歸與結案） | PENDING |

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

## 11. 文件

| 文件 | 內容 |
|---|---|
| [`../docs/Phase3_Closure_Report.md`](../docs/Phase3_Closure_Report.md) | Phase 3 自動生命週期收尾報告 |
| [`../docs/Phase4.6_Closure_Report.md`](../docs/Phase4.6_Closure_Report.md) | Windows Service 包裝與 NSSM 驗證 |
| [`../docs/Phase4_Closure_Report.md`](../docs/Phase4_Closure_Report.md) | Phase 4 整體收尾：架構、Ownership、Recovery、已知限制 |
| [`../docs/Phase5_Validation_Plan.md`](../docs/Phase5_Validation_Plan.md) | Phase 5 驗證計畫與驗收基準 |
| [`../docs/Phase5_Closure_Report.md`](../docs/Phase5_Closure_Report.md) | Phase 5 收尾：Soak / Stress / 文件 / Release Validation 結果 |
| [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md) | **部署 / 維運手冊**：安裝、操作、設定、健康檢查、更新、Rollback、Troubleshooting |
| [`../tools/README.md`](../tools/README.md) | NSSM 版本、授權、SHA-256；**Service action 權威來源** |

## 12. 注意事項

- **`.env` 檔案不可 commit**（已由 `.gitignore` 排除）。
- **`tools/nssm.exe` 為 runtime dependency**，不可刪除、改名或搬移。
- Service 運行時 **Dashboard 為 Observer**，不會寫入報告。
- **不要同時啟動第二個 Monitor writer** —— Ownership 會擋下，但應避免。
- 詳細測試結果、實機證據與歷史紀錄請查看 `docs/`。
