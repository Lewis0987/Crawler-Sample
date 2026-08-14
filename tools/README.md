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

以**系統管理員** PowerShell 執行：

```powershell
.\tools\install_service_nssm.ps1 -Action install
.\tools\install_service_nssm.ps1 -Action start
```

其他動作：`-Action stop` / `-Action status` / `-Action remove`。

腳本一律以 SCM 的實際狀態（`Get-Service`）判定成敗，不解析也不顯示 NSSM 的
原生輸出 —— NSSM 在行程內部就會把非 ASCII 訊息轉成 `?`，那些文字無法還原，
因此成功／失敗訊息全部由腳本自行輸出。

---

## 更新 NSSM 的注意事項

**不要在 Service 為 Running 狀態時直接覆蓋 `nssm.exe`。**

若未來需要更新：

1. `-Action stop`
2. 確認 Service 已 `Stopped`
3. 更換 binary
4. 確認版本 / x64 / SHA-256，並同步更新本文件的版本資訊表
5. 視需要 remove → install
6. 重跑 Phase 4.6 Service regression
7. 驗證 start / stop / boot auto-start / log / `AppExit` / log rotation
