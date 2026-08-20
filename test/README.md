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
| 6.3 | Decision Engine（自動充放電決策） | READY |
| 6.4 | Safety Gate（安全條件檢查） | PENDING |
| 6.5 | PCS Control Integration（PCS 自動充放電控制） | PENDING |
| 6.6 | Auto Report Integration（自動報告整合） | PENDING |
| 6.7 | Field Validation（實機驗證） | PENDING |
| 6.8 | Regression & Closure（完整回歸與結案） | PENDING |

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
