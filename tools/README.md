# tools

本目錄放置部署所需的外部工具與樣板產生器。

- `nssm.exe` —— Windows Service 宿主（見下）
- `install_service_nssm.ps1` —— ESSAutoMonitor 的安裝 / 啟動 / 停止 / 狀態 / 移除腳本
- `make_ess_report_template.py` —— 報告樣板產生器

---

## NSSM

**名稱**：NSSM - the Non-Sucking Service Manager

### 用途

ESSAutoMonitor Windows Service 的**宿主程式**。Windows SCM 實際啟動的是
`tools\nssm.exe`，NSSM 再啟動並監督：

```
test\auto_monitor_service.py
```

### ⚠️ 重要：這是 runtime dependency，不是安裝工具

`tools\nssm.exe` 是**服務執行期間的常駐行程**，不是裝完就可以丟掉的安裝程式。
Service 的 ImagePath 直接指向這個檔案路徑，因此**安裝後不可任意刪除、改名或搬移**。

若檔案消失或被移動，症狀不是「裝不起來」，而是**已安裝的服務下次開機起不來**，
且無法用 `-Action start` 修復，必須 remove → install 重新註冊。

### 版本資訊

| 項目 | 值 |
|---|---|
| 目前版本 | `2.24-101-g897c7ad` |
| 版本類型 | **prerelease / latest build**，**不是** stable 2.24 |
| 架構 | x64 / AMD64 |
| 來源 | <https://nssm.cc/download>，取用 `win64\nssm.exe` |
| SHA-256 | `EEE9C44C29C2BE011F1F1E43BB8C3FCA888CB81053022EC5A0060035DE16D848` |
| 檔案大小 | 368,640 bytes |
| 數位簽章 | NotSigned |
| 授權 | Public Domain — Author: Iain Patterson, 2003-2017 |

版本號刻意記錄完整的 git 描述：nssm.cc 的 latest build 會隨時間變動，
只寫「2.24」日後會取得不同的二進位。Phase 4.6 的所有 Service 實機驗證
都是基於上表這一顆，故一併記錄 SHA-256 供比對。

### 為何納入版本控管

1. 它是 runtime dependency（見上），缺檔會讓已安裝的服務失效。
2. 上游沒有版本釘選機制：nssm.cc 的 latest build 無官方 checksum、
   下載 URL 不帶版本，重新下載無法保證得到相同二進位。
3. Phase 4.6 的行為驗證（start/stop 語意、`AppExit`、log rotation、
   以及 NSSM 原生輸出的編碼特性）都以這一顆為前提。
4. Public Domain 授權無散布限制，檔案僅約 360 KB。

---

## 目前 Service 關係

| 項目 | 值 |
|---|---|
| Service 名稱 | `ESSAutoMonitor` |
| Service ImagePath | `<repo>\tools\nssm.exe` |
| Application | Python executable |
| AppDirectory | `<repo>\test` |
| AppParameters | `auto_monitor_service.py --interval 10` |
| 執行帳號 | LocalSystem |
| 啟動類型 | Automatic（開機自動啟動） |

`AppParameters` 使用**不含空白的相對檔名**，搭配 `AppDirectory` 指定工作目錄 ——
因為 NSSM 儲存參數時不保留引號，若寫成含空白的絕對路徑會被拆成兩個引數。

---

## 部署

clone 專案後，`tools\nssm.exe` 已隨 repo 提供，**不需要另外下載 NSSM**。

> 📌 **本節是 Service action 的權威來源。** 其他文件（含 `test/README.md`）
> 只保留最基本入口並連回此處，避免兩份完整版本各自漂移。
> 操作步驟、預期結果與失敗處置見
> [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md)。

以**系統管理員** PowerShell 執行（`install` / `update` / `start` / `stop` /
`remove` 一律需要系統管理員）：

```powershell
.\tools\install_service_nssm.ps1 -Action install   # 首次建立 Service 並套用設定
.\tools\install_service_nssm.ps1 -Action update    # 更新既有 Service 的設定
.\tools\install_service_nssm.ps1 -Action start
.\tools\install_service_nssm.ps1 -Action stop
.\tools\install_service_nssm.ps1 -Action status
.\tools\install_service_nssm.ps1 -Action remove
```

| Action | 用途 |
|---|---|
| `install` | **首次**建立 Service 並套用全部設定，最後回讀驗證 |
| `update` | 更新**既有** Service 的設定 —— **不建立、不移除**服務；服務不存在即 throw（不會退化成 install） |
| `start` / `stop` | 啟動／停止（stop 送 Ctrl+C，逾時 30 秒） |
| `status` | 查詢狀態與診斷資訊 |
| `remove` | 移除 Service（log 保留）；Running 時會先自動停止，停止失敗即不移除 |

**正式流程**

| 情境 | 流程 |
|---|---|
| 首次安裝 | `install` → `start` |
| **Service 設定更新** | **`stop` → `update` → `start`** |
| 純 Python 程式碼更新 | `stop` → 更新 `.py` → `start`（不需 `update`；理由見 Operations Guide §8.3） |
| 移除 | `stop` → `remove` |

`install` 與 `update` 共用同一份設定來源（腳本內的 `Apply-ServiceSettings`），
兩者不會漂移；`Assert-InstalledSettings` 會回讀驗證三項基本設定與
`AppEnvironmentExtra`（7 項，含 `OPENBLAS_NUM_THREADS=1`），任一不符即 throw、
**不放行到 start**（fail closed）。

> ⚠️ **`remove` → `install` 不是一般更新方式。** 它會刪除服務定義，
> 只用於「需要重新註冊 Service」的特殊情境（見下方〈nssm.exe 遺失〉）。

腳本一律以 SCM 的實際狀態（`Get-Service`）判定成敗，不解析也不顯示 NSSM 的
原生輸出 —— NSSM 在行程內部就會把非 ASCII 訊息轉成 `?`，那些文字無法還原，
因此成功／失敗訊息全部由腳本自行輸出。

---

## 更新 NSSM 的注意事項

**不要在 Service 為 Running 狀態時直接覆蓋 `nssm.exe`。**

若未來需要更新 **`nssm.exe` binary**：

1. `-Action stop`
2. 確認 Service 已 `Stopped`
3. 就地更換 binary（**檔名與路徑保持不變**）
4. 確認版本 / x64 / SHA-256，並同步更新本文件的版本資訊表
5. `-Action start`
6. 重跑 Phase 4.6 Service regression
7. 驗證 start / stop / boot auto-start / log / `AppExit` / log rotation

> **不需要 `remove` → `install`** —— Service 的 ImagePath 指向的是同一個路徑，
> 就地更換 binary 後 `start` 即生效。只有下面這個情境才需要重新註冊。

### nssm.exe 遺失 / ImagePath 已失效（需重新註冊）

若 `nssm.exe` 被刪除、改名或搬移，症狀不是「裝不起來」，而是
**已安裝的服務下次開機起不來**，且**無法**用 `-Action start` 修復。

此時才需要重新註冊 Service：

1. 先把 `nssm.exe` 還原到 `tools\nssm.exe`（SHA-256 需與版本資訊表相符）
2. `-Action stop`（若服務仍在）
3. `-Action remove`
4. `-Action install`
5. `-Action start`
6. 驗證 Service health

⚠️ `remove` 會刪除服務定義；若後續 `install` 失敗會處於「完全沒有服務」的狀態。
執行前請先依 [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md) §9.1
記錄 baseline。
