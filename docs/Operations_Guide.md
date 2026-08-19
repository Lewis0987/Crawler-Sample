# Operations Guide —— Production Deployment / 維運手冊

**適用對象**：未參與 Phase 3～5 開發、需要部署或維運本系統的人。
**最後更新**：2026-08-18

> **三份文件的分工**
>
> | 文件 | 回答 |
> |---|---|
> | **本文件** | **怎麼做** —— 安裝、操作、設定、健康檢查、更新、Rollback、Troubleshooting |
> | [`Phase4_Closure_Report.md`](Phase4_Closure_Report.md) ／ [`Phase4.6_Closure_Report.md`](Phase4.6_Closure_Report.md) | **為什麼這樣設計** —— 架構、Ownership、Recovery 機制、實機發現 |
> | [`Phase5_Validation_Plan.md`](Phase5_Validation_Plan.md) | **怎麼驗證、驗證結果** —— 驗收基準與各階段實測數據 |
>
> 本文件**不複製**技術分析；需要原因時請走上表連結。

---

## 1. Purpose / Scope

本系統以 Windows Service（`ESSAutoMonitor`）常駐監看 PCS 智慧排程，
在排程開始充放電時**自動建立**充放電報告 Session，排程結束時**自動收尾產表**，
全程不需開啟 Dashboard。

**本文件涵蓋**：安裝前置、首次部署、日常操作、設定確認、log／output 查找、
Service 健康判斷、更新、Rollback、Troubleshooting、Release 前基本確認。

**本文件不涵蓋**：功能設計原理、驗證方法與測試結果（見上表連結）、
PCS 控制操作（屬 `device_control_menu.py` 的互動流程）。

---

## 2. Prerequisites

| 項目 | 要求 | 備註 |
|---|---|---|
| 作業系統 | Windows（本專案於 Windows 11 Pro 驗證） | Service 以 Windows SCM + NSSM 管理 |
| Python | **3.14.3**（實機驗證版本） | Service 以**絕對路徑**指定 python.exe，不依賴 `PATH` |
| Python 相依 | `requests`、`openpyxl` | `openpyxl` 會**間接**帶入 `numpy`（見 §5.3） |
| NSSM | `tools\nssm.exe`（**已隨 repo 提供**，不需另外下載） | **runtime dependency**，安裝後不可刪除／改名／搬移 |
| 權限 | `install` / `update` / `start` / `stop` / `remove` **一律需要系統管理員** | `status` 亦以系統管理員執行較穩定 |
| 憑證 | `test\` 下的 `*.env`（例如 `login.env`） | 見 §5.1；**已由 `.gitignore` 排除，不可提交** |
| 網路 | 可直達設備區域網路 IP | Service 設 `NO_PROXY`，**絕不經 proxy** |

> ⚠️ 目前 repo **沒有** `requirements.txt`。相依套件請依上表手動確認已安裝。
> 安裝套件屬環境變更，請由負責人自行執行，本文件不代為指定安裝指令。

**確認 Python 與套件**（唯讀）：

```powershell
& "C:\Users\<user>\AppData\Local\Programs\Python\Python314\python.exe" -c `
  "import sys, requests, openpyxl; print(sys.version); print(openpyxl.__version__)"
```

---

## 3. Initial Deployment（首次安裝）

以**系統管理員** PowerShell，於 repo 根目錄執行：

```powershell
cd "D:\Crawler Sample"

# 1) 安裝：建立 Service 並套用全部設定，最後自動回讀驗證
.\tools\install_service_nssm.ps1 -Action install

# 2) 啟動
.\tools\install_service_nssm.ps1 -Action start

# 3) 查詢
.\tools\install_service_nssm.ps1 -Action status
```

**`install` 成功的判斷**：輸出 `=== 安裝後回讀驗證 ===` 區塊，且最後一行為
`→ 三項基本設定與 7 項環境變數均符合預期`。
該驗證會回讀 `Application` / `AppDirectory` / `AppParameters` 與
`AppEnvironmentExtra`（7 項），**任一不符即 throw，不會放行到 start**。

**`start` 成功的判斷**：輸出 `服務已啟動（Running）`。
成功與否一律以 **SCM 實際狀態**（`Get-Service`）判定，不解析 NSSM 原生輸出。

啟動後請接著做 §7 的 Service Health Check。

---

## 4. Daily Service Operations

**指令的權威來源為 [`../tools/README.md`](../tools/README.md)**；本節補充用途與預期結果。

| Action | 用途 | 需 Administrator | 預期結果 | 失敗時**不要繼續**的條件 |
|---|---|---|---|---|
| `install` | **首次**建立 Service 並套用設定 | 是 | 回讀驗證三項基本設定 + 7 項環境變數皆符合 | 回讀驗證 throw → 不要 `start`，先查 §10 |
| `update` | 更新**既有** Service 的設定（不建立、不移除） | 是 | 同上回讀驗證 + `已更新服務設定` | 回讀驗證 throw → 不要 `start`，走 §9 Rollback |
| `start` | 啟動 Service | 是 | `服務已啟動（Running）` | 逾時未進 Running 會 throw → 不要重複 start，先查 §10 |
| `stop` | 停止 Service（送 Ctrl+C，逾時 30 秒） | 是 | `已停止（Stopped）` | 卡在 `StopPending` → 不要 `remove`，先查 §10 |
| `status` | 查詢狀態與診斷資訊 | 建議是 | 顯示 SCM 狀態與設定摘要 | — |
| `remove` | 移除 Service（log 保留） | 是 | `已移除服務`；Running 時會先自動停止 | 停止失敗即**不執行** remove（fail closed） |

**重要契約**

- `update` **只更新既有 Service**：服務不存在時直接 throw，**不會**自動退化成 `install`。
- `update` **不呼叫** `nssm install`、**不呼叫** `nssm remove`。
- `install` 與 `update` 共用同一份設定來源（腳本內的 `Apply-ServiceSettings`），兩者不會漂移。
- 設定寫入後**要下次啟動才生效**；在 Running 狀態執行 `update` 會看到提示 Warning，屬預期。
- **`remove` → `install` 不是一般更新流程**。一般設定更新一律走 `stop → update → start`（見 §8）。
  只有 `nssm.exe` 遺失／ImagePath 失效等需**重新註冊 Service** 的特殊情境才適用，
  詳見 [`../tools/README.md`](../tools/README.md)。

---

## 5. Configuration

### 5.1 `*.env`（憑證）

放在 `test\` 目錄下，例如 `test\login.env`。可依 `test\.env.example` 建立本機檔案。

**必要欄位（只列 key）**

| Key | 用途 |
|---|---|
| `HMI_USERNAME` | HMI 登入帳號 |
| `HMI_PASSWORD` | HMI 登入密碼 |
| `SM2_PUBLIC_KEY` | 登入密碼加密所需公鑰 |
| `LOGIN_PASSWORD_PAYLOAD` | 登入 payload 組裝所需欄位 |

> 🔒 **本文件與任何 troubleshooting 紀錄都不得填入實際值。** 見 §11。
> `.env` 檔案已由 `.gitignore` 排除，**不可提交**。

Service 以環境變數 `API_ENV_FILE` **明確指定**要載入哪一個 env 檔，
避免目錄下有多個 `*.env` 時探索順序不確定。

### 5.2 `AppEnvironmentExtra`（Service 環境變數，共 7 項）

Service 不繼承使用者環境，故必要變數全部由安裝腳本寫入。**權威來源是
`tools\install_service_nssm.ps1` 內的 `$EnvLines`**，`install` / `update` 皆由它套用，
`Assert-InstalledSettings` 會回讀比對。

| # | 變數 | 用途 |
|---|---|---|
| 1 | `API_ENV_FILE` | 明確指定憑證檔路徑（見 §5.1） |
| 2 | `NO_PROXY` | 設備為區網 IP，絕不可經 proxy |
| 3 | `no_proxy` | 同上（部分函式庫只讀小寫，**兩者都必須存在**） |
| 4 | `PYTHONIOENCODING` | `utf-8` —— 無 console 時仍確保輸出正確 |
| 5 | `PYTHONUTF8` | `1` —— 同上 |
| 6 | `PYTHONUNBUFFERED` | `1` —— log 即時寫出，不因緩衝延遲 |
| 7 | `OPENBLAS_NUM_THREADS` | `1` —— 見 §5.3 |

**唯讀確認實際值**（不需停止 Service）：

```powershell
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\ESSAutoMonitor\Parameters').AppEnvironmentExtra
```

預期恰好 **7 行**，且 `OPENBLAS_NUM_THREADS=1` **恰好一筆**。
若不符請走 §9 Rollback，不要手改 Registry。

### 5.3 `OPENBLAS_NUM_THREADS=1`（資源最佳化，不可移除）

報告產出會 `import openpyxl.chart`，openpyxl 內部間接 `import numpy`，
而 numpy 的 OpenBLAS 後端會**依 CPU 核心數建立 native thread pool**。
本產品全程不做 BLAS 運算（openpyxl 只拿 numpy 做型別判斷），該 pool 是純負擔。

限制為 `1` 後實測：Private Memory 大幅下降、native thread 不再增生，
報告內容與產出時間無退步。

- **設定位置**：`tools\install_service_nssm.ps1` 的 `$EnvLines`（即 §5.2 第 7 項）
- **必須由 OS 在行程啟動前設定** —— numpy 一旦載入就來不及
- **迴歸守門**：`test\test_phase4_service_env.py` 有專門檢查，防止日後被移除
- 完整根因與實測數據：[`Phase5_Validation_Plan.md`](Phase5_Validation_Plan.md) §4.4

### 5.4 `charge_discharge_report_config.py`（維運相關參數）

修改這些值會直接改變 Service 的行為，**變更後需重新驗證**（見 §8）。

| 參數 | 目前值 | 維運意義 |
|---|---|---|
| `SAMPLE_INTERVAL_SEC` | `5` | 取樣間隔（秒）。調大會降低資料密度 |
| `AUTO_HOLD_START_SEC` | `15` | Start debounce：偵測到充放電後需持續多久才建立 Session |
| `AUTO_HOLD_STOP_SEC` | `60` | Stop debounce：idle 持續 **≥** 此值才自動收尾 |
| `AUTO_COOLDOWN_SEC` | `60` | 收尾後冷卻，期間不建立新報告（避免旗標抖動重複建立） |
| `ORPHAN_GAP_SEC` | `300` | 斷線間隔**大於**此值才標記孤兒（僅記錄，不改變 resume 流程） |
| `COMM_FAIL_MAX` | `3` | 連續通訊失敗達此值 → `communication_error` |
| `FINALIZE_RETRY_MAX` | `3` | 輸出檔被占用時的總嘗試次數 |
| `FINALIZE_RETRY_WAIT_SEC` | `2.0` | 每次重試前等待秒數 |
| `AUTO_END_TIMEOUT_SEC` | `0` | Session 硬性上限（`0` = 不限） |
| `SESSION_TIMEOUT_SEC` | `3600` | 單一 Session 最長監測秒數 → `timeout` |
| `ENERGY_METHOD` | `trapezoid` | 能量積分方法 |
| `OUTPUT_SUBDIR` | `charge_discharge_reports` | 報告輸出子目錄名 |

> ⚠️ 兩個門檻的比較語意**刻意不對稱**，調整時請注意：
> `AUTO_HOLD_STOP_SEC` 是 **`>=`**（idle 剛好 60 秒就收尾）；
> `ORPHAN_GAP_SEC` 是 **`>`**（間隔剛好 300 秒**不**算孤兒）。

---

## 6. Log / Output

| 項目 | 路徑 |
|---|---|
| Service log（stdout） | `output\logs\auto_monitor_service.log` |
| Service err.log（stderr） | `output\logs\auto_monitor_service.err.log` |
| 輪替後的舊 log | `output\logs\auto_monitor_service-<YYYYMMDD>T<HHMMSS>.<ms>.log` |
| 報告輸出 | `output\charge_discharge_reports\<session_id>\` |

**單一 Session 的輸出檔**

| 檔案 | 內容 |
|---|---|
| `samples.csv` | 逐筆取樣資料 |
| `events.csv` | Session 事件（開始／方向偵測／暫停／續接／結束／`finalize_ok`） |
| `summary.json` | 報告摘要（含 `end_reason`） |
| `statistics.json` | 統計數據 |
| `report.xlsx` | Excel 報告（含內嵌圖表） |
| `alarms.csv`、`cell_snapshots.json`、`session_state.json` | 告警、電芯快照、Session 狀態 |

### ⚠️ restart 後 log 「變小」不代表舊 log 遺失

NSSM 啟用了 log rotation（`AppRotateFiles=1`、`AppRotateOnline=1`、`AppRotateBytes=10485760`）。
**每次 Service 啟動時 NSSM 會先把現有 log 改名為帶時間戳的檔案**，再開一個新的
`auto_monitor_service.log`。因此 restart 後主 log 只有幾 KB 是**正常現象**。

**找回舊 log**：

```powershell
Get-ChildItem 'D:\Crawler Sample\output\logs\auto_monitor_service-*.log' |
  Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name, Length, LastWriteTime
```

每個輪替檔的最後一行通常是 `[AutoMonitorService] 已停止（共執行 N 輪）。`，
可據此確認該次執行是正常收尾。

> ⚠️ **NSSM 的輪替沒有「保留 N 份」上限**，舊 log 會持續累積，需自行或以排程清理。
> `auto_monitor_service.py` 另有 `.1~.5`（保留 5 份）的機制，但**僅在不經 NSSM 直接執行時生效**。

---

## 7. Service Health Check

**只看 `Service = Running` 不足以判定健康。** 以下 11 項全部唯讀，不需停止 Service。

| # | 檢查 | 正常 | **STOP AND INVESTIGATE** |
|---|---|---|---|
| 1 | Service 狀態 | `Running` / `Automatic` / `LocalSystem` | 非 Running；或 StartType 非 Automatic |
| 2 | worker 行程 | 恰好 **1 個** python.exe，PPID = NSSM PID | 0 個；或 ≥2 個；或 PPID 不符 |
| 3 | worker 存活時間 | 與預期上線時間一致 | 非預期改變 = 曾重啟（Service 設定為**不自動重啟**） |
| 4 | Ownership | `monitor_owner.json` 的 `pid` == worker PID，`role=service` | pid 不符（且非剛重啟） |
| 5 | Mutex | `monitor_owner.json` 的 `mutex_name` 與程式算出的 canonical 一致 | 兩者不同 = 路徑表示法不一致（雙 Owner 風險） |
| 6 | 一般使用者 probe | `held_by_other` | `acquired` = Service 已失去 Ownership |
| 7 | Dashboard / Observer | Service-only 運行時應為 **0** | 有 Dashboard 且出現 Session 副作用 |
| 8 | Active Session | `recording` ≤ 1；無殘留 `paused` | 同時 ≥2 份 `recording`；或長期殘留 `paused` |
| 9 | tick 前進 | log 持續新增 `[AUTO] mode=` 行 | 設備可達時 log 靜止 > 15 分鐘 |
| 10 | `Traceback` | log 內 **0** 次 | 出現任何 Traceback |
| 11 | `err.log` | **0 bytes** | 非 0（NSSM 自身訊息除外，需判讀內容） |

**唯讀檢查指令**

```powershell
# 1~3, 7
Get-Service ESSAutoMonitor | Format-List Status, StartType
$c = Get-CimInstance Win32_Service -Filter "Name='ESSAutoMonitor'"
Get-CimInstance Win32_Process -Filter "ParentProcessId=$($c.ProcessId)" |
  Where-Object { $_.Name -eq 'python.exe' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, ThreadCount, HandleCount

# 9~11
Select-String -Path 'D:\Crawler Sample\output\logs\auto_monitor_service.log' `
  -Pattern '\[AUTO\] mode=' | Select-Object -Last 1
Select-String -Path 'D:\Crawler Sample\output\logs\auto_monitor_service.log' -Pattern 'Traceback'
(Get-Item 'D:\Crawler Sample\output\logs\auto_monitor_service.err.log').Length
```

第 4～6、8 項需讀取 `output\charge_discharge_reports\` 下的
`monitor_owner.json` 與各 Session 的 `session_state.json`。

### 正常 log 讀法

一行典型 tick：

```
[HH:MM:SS] [AUTO] mode=smart sched=True chg=False dis=False run=False stby=False
           fault=False(否) dir=idle state=IDLE session=None owner=self src=service
           hold=NNNs/15s｜SOC N.N% P -N.NkW
```

| 欄位 | 正常 | 說明 |
|---|---|---|
| `mode` | `smart` | 偶發 `unknown` 單筆後恢復 = 登入態暫時失效並自動恢復，屬設計中的降級路徑 |
| `sched` | `True` | 排程主開關 |
| `state` | `IDLE` / `START_HOLD` / `RUNNING` / `STOP_PENDING` / `COOLDOWN` | 狀態機合法轉移 |
| `owner` | `self` | 本行程為 Monitor Owner |
| `src` | `service` | 由 Service 而非 Dashboard 驅動 |

---

## 8. Production Upgrade

三種情境**流程不同**，請先判斷屬於哪一種。

### 8.1 首次安裝

```
install → start → §7 health check
```

### 8.2 Service 設定 / 部署更新

適用時機：`tools\install_service_nssm.ps1` 內宣告的內容有變動 —— 環境變數
（含 `AppEnvironmentExtra`）、Python 路徑、`AppDirectory`、`--interval`、
log 路徑、執行帳號、`AppExit` 策略、rotation 設定等。

```
0) 記錄 baseline（§9.1）
1) stop      → 確認 Stopped、舊 worker 消失
2) update    → Assert-InstalledSettings 必須 PASS
3) verify    → 獨立回讀 AppEnvironmentExtra = 7 項（§5.2）
4) start     → 服務已啟動（Running）
5) verify    → §7 health check
```

任一步失敗 → **停止後續操作**，走 §9 Rollback。

> **不要**用 `remove → install` 當一般更新流程。

### 8.3 純 Python 程式碼更新

適用時機：只改了 `test\*.py`，**沒有**改動 `install_service_nssm.ps1` 宣告的任何設定。

```
1) stop   → 確認 Stopped、舊 worker 消失
2) 更新 .py 檔
3) start  → 服務已啟動（Running）
4) §7 health check
```

**為何不需要 `update`**（依目前產品契約確認，非假設）：

- Service 的 `Application` 是 **python.exe 絕對路徑**，`AppParameters` 是**相對腳本名**，
  `AppDirectory` 固定為 `test\` —— 這些都不隨 `.py` 內容改變。
- 每次 `start` 都會啟動**全新的 python 行程**，在該時刻才從磁碟讀取 `.py`。
- 產品碼**沒有**任何 `.pyc` 快取控制、`importlib.reload` 或預編譯機制；
  Python 依 mtime 自動重新編譯。

⚠️ **但若 `.py` 的變更牽動了 ps1 宣告的內容**（例如改了服務進入點檔名、
改了 `--interval` 的預期值、改了 log 或 output 路徑），
則屬 §8.2，**必須走 `update`**。

### 8.4 更新後建議的驗證

```powershell
cd "D:\Crawler Sample\test"
py -3 run_phase3_regression.py      # 完整離線 Regression
```

判定基準見 [`../test/README.md`](../test/README.md) §9。
若既有項目減少或出現 FAIL → 停止並調查，不要直接更新 baseline。

---

## 9. Rollback

### 9.1 部署前必做：建立 baseline

**任何 Service 操作之前**先記錄，這是唯一的回復依據：

```powershell
# 目前的 AppEnvironmentExtra（回復點）
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\ESSAutoMonitor\Parameters'
 ).AppEnvironmentExtra | Set-Content 'D:\Crawler Sample\output\appenv_before.txt'

# Service / worker / Ownership / Session 現況
Get-Service ESSAutoMonitor | Format-List Status, StartType
$c = Get-CimInstance Win32_Service -Filter "Name='ESSAutoMonitor'"
"NSSM PID = $($c.ProcessId)"
Get-CimInstance Win32_Process -Filter "ParentProcessId=$($c.ProcessId)" |
  Where-Object { $_.Name -eq 'python.exe' } |
  Select-Object ProcessId, CreationDate
```

另請一併記錄：目前 `git` HEAD 與 working tree 狀態、
`monitor_owner.json` 內容、Session 統計（recording／paused／completed）、
以及 log 目前大小（部署後只看此位置之後的新內容）。

> `output\` 已由 `.gitignore` 排除，把 baseline 放在該目錄不會污染版控。

### 9.2 安全窗口（不滿足就不要部署）

| 條件 | 要求 |
|---|---|
| `recording` Session | **0** |
| `paused` Session | **0** |
| `find_active_session()` | **None** |
| Dashboard 行程 | **0** |

有進行中的 Session 就部署，會中斷正在錄製的報告。

### 9.3 部署流程與失敗處置

```
stop → update → verify → start → post-deployment verification
```

| 失敗點 | 處置 |
|---|---|
| `stop` 卡在 `StopPending` 或 worker 未消失 | **不要**繼續 `update`；查 §10。**不要**用強制終止當正常流程 |
| `update` 的 `Assert-InstalledSettings` throw | **不要** `start`。走 §9.4 設定 rollback |
| `verify`（回讀 7 項）不符 | **不要** `start`。走 §9.4 |
| `start` 逾時未進 Running | **不要**重複 `start`。查 §10 後走 §9.4 |
| `post-deployment verification`（§7）不通過 | 停止一切後續驗證（例如不要進行實機 finalize 觀察），走 §9.4 |

### 9.4 三種 Rollback（依變更性質選擇）

**(a) Service 設定 / `AppEnvironmentExtra` rollback**

一律**透過正式設定來源**還原，**不手改 Registry**：

```powershell
cd "D:\Crawler Sample"
git checkout -- tools\install_service_nssm.ps1   # 還原腳本到部署前版本
.\tools\install_service_nssm.ps1 -Action update  # 以還原後的腳本重新套用
# 回讀確認：應與 output\appenv_before.txt 一致
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\ESSAutoMonitor\Parameters'
 ).AppEnvironmentExtra
.\tools\install_service_nssm.ps1 -Action start
```

還原後重跑 §7 health check。

**(b) 產品碼 rollback**

```powershell
cd "D:\Crawler Sample"
.\tools\install_service_nssm.ps1 -Action stop
git checkout -- test\<檔名>.py          # 或還原到部署前的 commit
.\tools\install_service_nssm.ps1 -Action start
```

還原後重跑 §7 health check 與 §8.4 的 Regression。

**(c) 服務定義本身損壞**

僅在 Service 無法以 `update` 修復（例如 ImagePath 失效、服務項目異常）時採用：

```
stop → remove → install → verify → start → §7 health check
```

⚠️ 這**不是**一般 rollback。`remove` 會刪除服務定義，若後續 `install` 失敗會處於
「完全沒有服務」的狀態。執行前務必確認 §9.1 baseline 已保存、且 `tools\nssm.exe` 存在。

### 9.5 Rollback 的通用禁止事項

- **不要**用強制終止行程（taskkill／Stop-Process）當作正常 rollback 手段
- **不要**手改 Registry 的 `AppEnvironmentExtra`
- **不要**在 rollback 未驗證通過前繼續後續驗證或部署
- **不要**刪除或改寫既有 Session 輸出（`output\charge_discharge_reports\`）

---

## 10. Troubleshooting

格式：**症狀 → 可能原因 → 確認方式 → 處置**。
以下僅收錄**本專案實際遇過並已驗證**的案例。

### 10.1 Service 無法啟動（start 逾時未進 Running）

- **原因**：`AppParameters` 含空白絕對路徑而遺失引號、Python 路徑錯誤、
  `nssm.exe` 不存在、env 檔缺失。
- **確認**：讀 `output\logs\auto_monitor_service.log` 的最新啟動區塊；
  以 §5.2 指令回讀 Registry 設定；確認 `tools\nssm.exe` 存在。
- **處置**：`AppParameters` 必須是**不含空白的相對腳本名**（`auto_monitor_service.py --interval 10`），
  由 `install` / `update` 自動套用。以 `update` 重新套用設定後再 `start`。

### 10.2 `AppParameters` 出現含空白的絕對路徑

- **原因**：NSSM 會把參數經 argv 解析再以空白 join 存入 Registry，
  含空白的絕對路徑會在往返中遺失引號 → Python 收到被切斷的路徑而報
  `can't open file 'D:\Crawler'`。
- **確認**：回讀 `AppParameters`，若出現 `[A-Za-z]:\` 即為此問題。
- **處置**：`Assert-InstalledSettings` 已內建此檢查並會 throw。以 `update` 重新套用即可。

### 10.3 NSSM 訊息出現 `?` 亂碼

- **原因**：NSSM **在自己的行程內**就把非 ASCII 訊息轉成 `?`，那些字元無法還原。
- **確認**：亂碼只出現在 NSSM 原生輸出，腳本自行輸出的中文正常。
- **處置**：**不需處理**。腳本一律以 SCM 實際狀態判定成敗，
  成功／失敗訊息全部由腳本自行輸出。**不要**改 code page、不要用 `chcp 65001`
  當修法、不要加 `iconv`、不要嘗試重新解碼 NSSM 的 stderr。

### 10.4 `tools\nssm.exe` 遺失或被搬移

- **原因**：`nssm.exe` 是 **runtime dependency**，Service ImagePath 直接指向它。
- **確認**：`Get-Service` 顯示服務存在但無法啟動；ImagePath 指向不存在的檔案。
- **處置**：症狀不是「裝不起來」而是「**已安裝的服務下次開機起不來**」，
  且**無法**用 `-Action start` 修復 —— 需**重新註冊 Service**：
  還原 `nssm.exe` 後走 §9.4(c)（`stop → remove → install → start`）。
  詳見 [`../tools/README.md`](../tools/README.md)。

### 10.5 `AppEnvironmentExtra` 項數或內容不符

- **原因**：手改 Registry、或安裝腳本被改動。
- **確認**：以 §5.2 指令回讀，應恰好 7 項且 `OPENBLAS_NUM_THREADS=1` 恰好一筆。
- **處置**：**不要**手改 Registry。以 `stop → update → 回讀確認 → start` 重新套用；
  若腳本本身被改壞，先 `git checkout -- tools\install_service_nssm.ps1`（§9.4(a)）。

### 10.6 Service = Running 但 worker 異常

- **原因**：worker 行程數不為 1、PPID 與 NSSM PID 不符、或 worker 曾非預期重生。
- **確認**：§7 第 2～3 項。
- **處置**：Service 設定為**不自動重啟**（`AppExit` 全為 `Exit`），
  因此 worker 消失即代表確實結束。取證（log、err.log、Session 狀態、Ownership）後
  以 `stop → start` 重新啟動；**不要**用強制終止。

### 10.7 一般使用者 probe 得到 `held_by_other`

- **這是正常的。** Service 正在持有 Ownership 時，任何其他行程嘗試取得都應得到
  `held_by_other` —— 這正是 Writer Gate 生效的證據。
- **反過來才要注意**：Service 為 Running 卻 probe 到 `acquired`，代表 Service
  已失去 Ownership → 依 §7 第 6 項 STOP AND INVESTIGATE。

### 10.8 `monitor_owner.json` 的 pid 與 worker 不符

- **原因**：Service 剛重啟（短暫不一致，會很快被覆寫），或曾出現雙 Owner。
- **確認**：比對 `mutex_name` 與程式算出的 canonical 是否一致。
- **處置**：若 mutex 名稱不同，屬**路徑表示法不一致**（例如 `D:\` 與 `d:\`
  被算成兩顆 Mutex）造成的雙 Owner 風險 —— 產品已以路徑正規化修正並有迴歸守門。
  請確認啟動方式使用的路徑，並取證後回報。原因分析見
  [`Phase4_Closure_Report.md`](Phase4_Closure_Report.md)。

### 10.9 restart 後 log 看似被清空

- **原因**：NSSM 在啟動時輪替 log。
- **確認 / 處置**：見 §6 的「restart 後 log 變小不代表舊 log 遺失」。**不是故障。**

### 10.10 `err.log` 非 0 bytes

- **原因**：worker 有未預期的 stderr 輸出；或 NSSM 自身訊息。
- **確認**：讀取 `err.log` 內容並判斷來源（NSSM 訊息通常含 `?` 亂碼）。
- **處置**：若是 Python 例外或 Traceback → 取證後 STOP AND INVESTIGATE，
  不要重複 restart 掩蓋問題。

### 10.11 log 出現 `Traceback`

- **原因**：未處理例外。
- **確認**：取出完整 Traceback 與前後 tick。
- **處置**：**STOP AND INVESTIGATE**。保留 log（注意 restart 會輪替，先複製一份）、
  記錄 Session 狀態與 Ownership，再回報。不要先重啟。

### 10.12 API / 登入失敗

- **原因**：憑證失效、env 檔路徑錯誤、網路不可達，或**登入態逾時**。
- **確認**：log 是否出現 `mode=unknown`、`[警告] GET`、連線失敗字樣；
  以及是否出現「判定登入態可能失效」與「自動監看已恢復」成對出現。
- **處置**：
  - **登入態逾時屬正常且自動恢復** —— 產品內建 Auth Recovery：連續判定失效達門檻即
    清除 cached client，下一輪自動重新登入；失敗時每 60 秒**靜默**重試。
    偶發單筆 `mode=unknown` 後恢復**不需處理**。
  - 若 degraded 狀態持續不恢復，檢查 `API_ENV_FILE` 指向的 env 檔是否存在、
    欄位是否完整（§5.1）、設備是否可達。
  - ⚠️ 排錯時**絕不**把實際帳密／token 貼進任何紀錄（§11）。

### 10.13 出現殘留 `recording` / `paused` Session

- **原因**：worker 非正常收尾中斷（`paused` 為 `stop` 的正常結果，
  `recording` 殘留則代表未經收尾即中斷）。
- **確認**：檢查各 `session_state.json` 的 `status`；讀 `events.csv` 最後事件。
- **處置**：**不要人工 finalize、不要手改 Session 檔**。
  Service 重新啟動時會自動續接（resume）同一份 Session 並繼續遞增 `sample_index`，
  收尾時正常產表。若 `paused` 長期殘留，確認 Service 是否正常啟動。
  機制說明見 [`Phase4_Closure_Report.md`](Phase4_Closure_Report.md)。

### 10.14 Dashboard 與 Service 的 Ownership 衝突

- **原因**：Service 執行中又開啟 Dashboard。
- **確認**：§7 第 7 項；Dashboard 端會顯示自己為 Observer。
- **處置**：**這是設計行為，不是故障。** Service 為 Monitor Owner，
  Dashboard 自動降為 **Observer（唯讀，不寫報告）**。
  仍應避免同時啟動第二個 writer。

### 10.15 報告輸出檔被占用（例如 Excel 開著 `report.xlsx`）

- **原因**：目標檔被其他程式獨佔開啟，寫入遭 sharing violation。
- **確認**：log 出現「請關閉正在開啟該檔的程式」與重試提示；
  `session_state.json` 的 `output_status` 出現 `failed_locked`。
- **處置**：關閉占用該檔的程式。產品會在 `FINALIZE_RETRY_MAX` 次內重試；
  重試耗盡時 Session 仍會標記 `completed` 並記 `finalize_partial`，
  **既有檔案不會被破壞**，且不影響下一次 Session。

---

## 11. Security

| 規則 | 說明 |
|---|---|
| 憑證只從正式來源取得 | 帳密／公鑰等只放在 `test\*.env`，由 `API_ENV_FILE` 明確指定 |
| `.env` 不可提交 | 已由 `.gitignore` 排除（`.env`、`*.env`） |
| log 不得輸出 `password` | — |
| log 不得輸出 `token` / `accessToken` | — |
| log 不得輸出 `cookie` | — |
| log 不得輸出 `Authorization` / `Bearer` | — |
| 診斷字串只帶端點與錯誤碼 | 登入態失效的診斷訊息只包含端點路徑與 application code，不夾帶 msg／raw body |
| Troubleshooting 紀錄不得貼實際 credential | 回報問題時請以 `<redacted>` 取代 |
| 本文件不含任何實際 credential | 僅列 key 名稱（§5.1） |

上述限制有自動化檢查涵蓋（Service log 掃描與測試守門），
但**人工回報與截圖仍需自行留意**。

---

## 12. Release Checklist（簡表）

完整驗收基準與判定標準見
[`Phase5_Validation_Plan.md`](Phase5_Validation_Plan.md) §7，本節不複製。

部署到正式環境前的基本確認：

- [ ] §2 Prerequisites 全部滿足
- [ ] 完整離線 Regression PASS（`py -3 run_phase3_regression.py`），FAIL 0、SKIP 0
- [ ] `AppEnvironmentExtra` 回讀 = 7 項，含 `OPENBLAS_NUM_THREADS=1`（§5.2）
- [ ] §7 Service Health Check 11 項全部正常
- [ ] §9.1 部署前 baseline 已記錄
- [ ] `git diff --check` 乾淨、working tree 狀態已確認
- [ ] 至少一次完整的自然 Session 生命週期已驗證（建立 → 收尾 → 產表）
- [ ] 無未處理的 Critical / High 問題

---

## 附錄：相關文件

| 文件 | 內容 |
|---|---|
| [`../test/README.md`](../test/README.md) | 專案入口：簡介、功能、檔案、執行方式、Regression baseline、Phase 狀態 |
| [`../tools/README.md`](../tools/README.md) | **Service action / NSSM 操作的權威來源**：版本、SHA-256、runtime dependency、部署 |
| [`Phase4_Closure_Report.md`](Phase4_Closure_Report.md) | 架構、Ownership、Writer Gate、兩條 Recovery 路徑、已知限制 |
| [`Phase4.6_Closure_Report.md`](Phase4.6_Closure_Report.md) | NSSM 包裝細節、start fail-closed、NSSM 輸出編碼根因 |
| [`Phase5_Validation_Plan.md`](Phase5_Validation_Plan.md) | Phase 5 驗證計畫與各階段實測結果、Release Validation DoD |
| [`Phase3_Closure_Report.md`](Phase3_Closure_Report.md) | Phase 3 自動生命週期收尾 |
