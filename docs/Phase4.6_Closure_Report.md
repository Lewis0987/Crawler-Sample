# Phase 4.6 收尾報告 —— Windows Service 包裝

**日期**：2026-08-14
**範圍**：Phase 4.6-A（Service 環境／NSSM 包裝）、4.6-B（跨帳號 Ownership、Writer Gate、
Auth Recovery、J/K 實機驗證）、4.6-C（Mutex canonicalization）
**結論**：**全部項目 PASS**，Full Regression 1456/1456，可進行封版。

---

## 1. 最終 PASS 矩陣

| 項目 | 離線證據 | 實機證據 |
|---|---|---|
| 4.6-A Service Environment | `test_phase4_service_env.py` **130/130** | LocalSystem 常駐、log 輪替正常、`err.log` 0 bytes |
| NSSM install / config | 同上（8 項設定守門、相對 `AppParameters` + `AppDirectory`） | 三次重啟皆正常載入 |
| remove idempotent | `_p46_service_ctl_probe.ps1` A–F（樁函式，不碰真實服務） | — |
| start fail-closed | `Start-SvcAndWait` A–H 八情境 + 10 項靜態守門 | 三次 `-Action start`，NSSM 亂碼未再出現 |
| Windows boot auto-start | `StartType=Automatic` 守門 | 2026-08-12 重開機後自動啟動（Event 6005/6006/12 佐證） |
| **4.6-B I 跨帳號 Ownership** | — | **18/18**：`create_failed_5` → `held_by_other` |
| Global Mutex / DACL | `test_phase4_mutex_dacl.py` **56/56** | 一般使用者可開啟，結果為 `held_by_other` |
| Writer Gate（三層） | `test_phase4_writer_gate.py` **43/43** | Observer 端零 writer 副作用 |
| Dashboard Observer | `test_phase4_observer.py` **64/64**<br>`test_dashboard_loop.py` **23/23** | 顯示「背景服務」／Observer |
| **Auth Recovery** | `test_phase4_auth_recovery.py` **116/116** | **4/4**，每次 21 秒恢復 |
| **4.6-C Mutex canonicalization** | `test_phase4_mutex_canon.py` **39/39** | rollout 驗證 **38/38** |
| **J** stop → paused → thread 結束 → Mutex release | — | **31/31** |
| **K** start → resume 同一 Session | — | **43/43** |

---

## 2. J / K 實機驗證

**驗證窗口**：Session `20260814_112748_auto`（canonical Mutex 修正後的第一個乾淨窗口）

### 時序

```
11:27:48  session_start（scheduler 自動建立，action=charge）
11:35:50  #84  P=+2.3kW dir=charge
          → -Action stop → recording_pause(samples=84)
11:45:25  → -Action start，新 python PID 26792
          → recording_resume(session=20260814_112748_auto, sample_index=84)
          → orphan_detected（斷線間隔 576s，門檻 300s；本次照常續接，未改變流程）
11:45:26  #85  ← resume 後 1 秒即續接同一份 samples.csv
11:46:29  idle 持續 63s ≥ 60s 門檻
          → auto_charge_stop（origin=scheduler｜排程結束自動收尾）
          → session_end(reason=auto_stop, samples=96)
11:46:33  finalize_ok
```

### J：stop → paused（31/31）

| 項目 | 結果 |
|---|---|
| Service | `Stopped`，舊 python PID 28412 已結束，無殘留 ESS python 行程 |
| Session status | **`paused`**（非 `completed`、非 `recording`） |
| 未 finalize | events 有 `recording_pause`，**無** `session_end`、**無** `finalize_ok`；`summary.session.end_reason = unknown` |
| 取樣已停止 | 12 秒複查：資料列 84 → 84 不再增加 → sampling thread 確實結束 |
| **Mutex 釋放** | 一般使用者 `acquire_ownership()` → **`(True, 'acquired')`**（stop 前為 `held_by_other`）；原因是 **`acquired` 而非 `abandoned_taken`** → 證明正常釋放，未被 TerminateProcess 強殺 |
| orphan | 無任何 `status=recording` 的 Session |
| 關閉流程 | log：`充放電報告已暫停（下次可續接）` → `[AutoMonitorService] 已停止（共執行 106 輪）`；無 Traceback、`err.log` 0 bytes |
| owner file | Service 正常釋放後自行刪除（pid 相符才刪） |

> **注意**：`ReportSession.pause()` 的契約是「status=paused，可續接**並更新報告快照**」，
> 因此 `summary.json` / `statistics.json` / `report.xlsx` 會被重新產生。
> 區分 pause 與 finalize 的正確標記是 `events.csv`（`recording_pause` vs
> `session_end` + `finalize_ok`），不是檔案是否存在。

### K：start → resume 同一 Session（43/43）

| 項目 | 結果 |
|---|---|
| Ownership | 新 PID 26792 取得 canonical Mutex；`monitor_owner.json` 重建（`role=service`、`abandoned_takeover=False`）；一般使用者 → `held_by_other` |
| 同一份 Session | `session_id` / folder / `start_time`（11:27:48）/ `action` 全部未變 |
| 未建立第二份 | 資料夾數 16 → 16；今日仍只有 2 個；`session_start` 事件僅 1 個 |
| **samples 續接** | `#84 = 11:35:50`（pause 前）→ `#85 = 11:45:26`（Service 11:45:25 啟動後 1 秒）；重啟後新增 **12 筆**（#85–#96），時間戳全部晚於重啟時刻 |
| 索引完整 | `sample_index` **1..96 連續、無重複、無斷號**；前 84 筆未被覆寫 |
| 收尾歸因 | `session_end` 發生在 `recording_resume` **之後**，`origin=scheduler`「排程結束自動收尾」→ 不是重啟造成 |
| orphan 偵測 | 正確識別 576s 斷線並記錄，**未**因此另建 Session |
| 最終結果 | `completed`、`end_reason=auto_stop`、`duration 1121.8s`（涵蓋停機牆鐘時間）、`output_status` 無 failed、8 個輸出檔齊備 |

---

## 3. Auth Recovery：4/4 實機恢復

### 缺陷

Service 於 11:03:15 登入，11:33:15（整整 30:00）起 `getRunMode` / `getScheduleSwitch`
開始失效，**約 100 分鐘、1348 筆監看紀錄完全沒有恢復**（`mode=unknown`、`sched=None`）。
同時間 guest 端點（SOC／功率／故障旗標）全部正常，另外新登入的 client 也讀得到
`smart` / `True` —— 設備正常，是那個長壽 client 的登入態失效且**無人清除**。

後果不是顯示問題：`auto_schedule_check` 條件 A 要求 `mode_code == "smart"`，
讀不到就永遠是 `"unknown"` → **Service 再也不可能自動建立報告**，
直接打掉 Phase 4「不開 Dashboard 也能自動建報告」的核心目標。

### 修法（Monitor 層，最小範圍）

兩支 authenticated 端點同輪全失敗 **且** 至少一支 guest 端點成功 → 連續達
`AUTH_LOST_STREAK = 2` 輪 → 丟棄快取 client → 由**既有**的 `_monitor_client()`
重新登入恢復。不新增第二套登入狀態機。

`_rebind_session_client()`：`ReportSession` 建構時保存 `self.client`，
只清 `_report["client"]` 不會讓取樣執行緒換到新 client，故在 `_CLIENT_LOCK` 內
把新 client 接回進行中的 Session —— 只換參照，不 finalize / pause / 建新 Session /
重啟執行緒 / 碰 Ownership。

### 實機結果：4 次自然失效、4 次全自動恢復，Service PID 全程不變

| 行程 | 首次失效 | 恢復 | 耗時 |
|---|---|---|---|
| PID 35400 | 16:11:33 | 16:11:54 | **21 秒** |
| PID 35400 | 16:42:01 | 16:42:22 | **21 秒** |
| PID 35400 | 17:12:25 | 17:12:46 | **21 秒** |
| PID 29252 | 17:57:38 | 17:57:59 | **21 秒** |

恢復鏈路（log 原文）：

```
[AUTO] authenticated API 讀取失敗（1/2 輪，authed_all_failed[...], guest_ok=5/5）→ 先觀察，未重新登入
[AUTO] authenticated API 連續失敗，判定登入態可能失效（連續 2 輪；...）
[AUTO] 清除 cached client，下一輪重新登入
嘗試登入（hmiUser）… / HMI login success
mode=smart sched=True
```

失效週期實測 30:02 / 30:11 / 30:07 / 30:07（**這是本設備在 Service 現行請求型態下的
實測特徵，不是產品 TTL**）。

---

## 4. 兩個關鍵實機發現

### 發現一：HTTP 200 + application code 401 —— 舊版只判斷 `None`，Auth Recovery 無法觸發

`ApiClient.get()` 每一條回 `None` 的路徑（ConnectTimeout / ReadTimeout /
ConnectionError / HTTPError / RequestException / 非 JSON）都會先 `print("[警告] GET ...")`。
Service degraded 期間 **1060 個 tick、`[警告] GET` 為 0 行** → 排除法證明
`client.get()` 回的**不是** `None`。

`CDR.read_all._get()` 原本只在 `v is None` 時記入 `_fail`，而 `unwrap()` 對
`code ∉ (0, 200)` 回傳 `{"_error": code, "msg":…, "_raw":…}`（非 None，且**不印任何訊息**）
→ 端點不進 `_fail` → `_auth_session_lost()` 永遠看不到失敗 → **client 永遠不會被 invalidate**。

**實機取得的決定性數值**（2026-08-14 17:57:38）：

```
authed_all_failed[/client/dynamic/dataOrControl/pcs/getRunMode:_error401,
                  /schedule/config/getScheduleSwitch:_error401], guest_ok=5/5
```

**application code = 401（Unauthorized），HTTP 層是 200。**

**修法**：`_get()` 增加一個分支，`isinstance(v, dict) and "_error" in v` 時
記入 `f"{path}:_error{code}"`。**回傳值完全不變**（仍回 `v`，避免改變所有既有消費者的
資料型態）；**不比對特定 code**（`"_error"` key 本身即 unwrap 的判定結論）；
**只記端點與 code**，`msg` / `_raw` / body 一律不進 `_fail`。

`_fail` 的三種格式：

```
"<path>"                  取回 None
"<path>:<ExceptionType>"  丟出例外
"<path>:_error<code>"     HTTP 通了但 application 層失敗   ← 本次新增
```

### 發現二：Windows `D:\` / `d:\` 大小寫造成不同 Mutex → 雙 Owner

**現象**：Service 以 `D:\Crawler Sample\...` 啟動；使用者以
`python "d:/Crawler Sample/test/device_control_menu.py"` 啟動 Dashboard。
同一個 output root 算出兩顆不同的 Global Mutex：

```
D:\...\charge_discharge_reports → Global\ESS_AutoMonitor_Owner_v1_0c305bef986a0531
d:\...\charge_discharge_reports → Global\ESS_AutoMonitor_Owner_v1_fc4cd03c3cd675eb
```

**根因**：`_mutex_name()` 以 `md5(os.path.abspath(root))` 導出，而
`os.path.abspath()` **不會**正規化 Windows 磁碟機代號大小寫（檔案系統本身不分大小寫）。

**後果**：兩個行程各自 `acquire_ownership()` 都成功 → `is_owner()` 都是 True →
**Writer Gate 三層全部放行** → 同一個 Session 資料夾可能出現兩個寫入者，
正是 Phase 4.4 Ownership 要防止的核心風險。

**衍生症狀**：Dashboard 結束時 `release_ownership()` 依「pid 相符才刪」把它先前
覆寫過的 `monitor_owner.json` 刪掉，連帶抹掉 Service 的診斷資訊。
（Ownership 判定一律以 Mutex 為準，故不影響正確性；Service 重啟後自然重建。）

**實際損害**：**無**。當時處於雙 Owner 條件的 `20260814_101240_auto`（513 筆）
經檢查 `sample_index` 完全連續、無重複、timestamp 單調遞增、`sample_count`
與資料列數一致 —— 風險真實存在但未造成資料污染。該 Session 保留作為證據。

**修正**（兩行）：

```python
root = os.path.normcase(os.path.abspath(_report_output_root()))
```

`abspath` 吸收相對路徑、`.`／`..`、尾端分隔符；`normcase` 在 Windows 上轉小寫並
統一分隔符。**刻意不使用 `realpath`** —— 部署路徑上無 symlink / junction、
NTFS 未產生 8.3 短檔名（皆已實測），`realpath` 只會多做檔案系統 I/O 而不帶來額外收斂；
UNC / 網路磁碟不在目前部署範圍。

**Regression**（`test_phase4_mutex_canon.py` 39/39）：

- 實機案例定錨：`D:\` 與 `d:\` 同名、皆為 `…ad9a00aa7db0cf8f`、不再產生舊的任一顆
- A–E 變體矩陣：磁碟機大小寫、正／反斜線、尾端分隔符有無、`.`／`..`、目錄名大小寫 → 全部收斂
- F 反向：不同 output root 仍須不同名（不得過度收斂）
- G：前綴、`Global\` 命名空間、長度、雜湊格式不變
- **H 跨行程實際互斥**：子行程以小寫 root 取得 Ownership，本行程以大寫 root →
  必須 `held_by_other`（修正前兩邊都會 `acquired`）
- I/J 守門：有 `normcase`、**無** `realpath`；SDDL / `MUTEX_MIN_ACCESS` / `CreateMutexExW` 未變

**Rollout**（38/38）：依「先關 Dashboard → 等 Session 自然 completed → 確認無未完成
Session → stop → start」順序執行，避免過渡期雙 Owner。結果：

```
D:\ / d:\ / D:/  →  Global\ESS_AutoMonitor_Owner_v1_ad9a00aa7db0cf8f
兩種 root 的 acquire_ownership() 皆為 (False, 'held_by_other')
monitor_owner.json 由 Service 重建，mutex_name = canonical
```

---

## 5. NSSM / Windows Service 最終狀態

| 項目 | 值 |
|---|---|
| Service | `ESSAutoMonitor`，Running，StartType Automatic，帳號 LocalSystem |
| ImagePath | `<repo>\tools\nssm.exe` |
| Application | Python 3.14.3 |
| AppDirectory | `<repo>\test` |
| AppParameters | `auto_monitor_service.py --interval 10` |
| Mutex | `Global\ESS_AutoMonitor_Owner_v1_ad9a00aa7db0cf8f` |
| log | `output\logs\auto_monitor_service.log`，`AppRotateFiles=1`、`AppRotateBytes=10485760` |
| stderr | `auto_monitor_service.err.log`，0 bytes |
| 關閉語意 | `AppStopMethodConsole=30000` → Ctrl+C → `report_pause()`（paused，可續接；不 finalize）→ release Ownership |

### `-Action start` 的 fail-closed 修正

實測（對不存在的服務下 `status`，取原始位元組）：NSSM 的訊息**一律走 stderr**，
stdout 為空；中文訊息在 **nssm.exe 行程內部**做寬字元→窄字元轉換時被換成
`0x3F`（`?`），資訊在那一刻就已毀損，PowerShell 端不論用 cp950 / utf-8 / utf-16 /
latin-1 解碼都只會拿到同一串 `?`。因此：

- **不改 code page、不用 `chcp 65001`、不用 iconv、不重新解碼**
- `Start-SvcAndWait` 一個字都不顯示 NSSM 原生輸出，成功／失敗訊息由腳本自行輸出 Unicode 中文
- 判定原則與 `Stop-SvcIfRunning` / `Remove-SvcIdempotent` 一致：
  **SCM 實際狀態＝唯一事實來源；NSSM exit code＝診斷資訊；NSSM 文字＝完全不採用**
- `exit code = 0` 但 SCM 非 Running → **仍失敗**；`exit ≠ 0` 但 SCM Running → 成功但輸出警示

---

## 6. Full Regression

```
py_compile                               PASS
report selftest                 267/267  PASS
auto schedule                   372/372  PASS
3-B selftest                    122/122  PASS
Dashboard harness                 23/23  PASS
Windows file lock                 22/22  PASS
PCS schedule switch               84/84  PASS
Phase 4.3 service                 56/56  PASS
Phase 4.4 ownership               62/62  PASS
Phase 4.5 observer                64/64  PASS
Phase 4.6-A service env         130/130  PASS
Phase 4.6-B writer gate           43/43  PASS
Phase 4.6-B mutex DACL            56/56  PASS
Phase 4.6-B auth recovery       116/116  PASS
Phase 4.6-C mutex canon           39/39  PASS
Environment residue                      PASS
--------------------------------------------------------------
Automated checks              1456/1456  PASS
FAIL                                  0
SKIP                                  0
RESULT                                   PASS
```

`git diff --check`：exit 0。

## 7. Environment residue

**PASS** —— 正式 output 的 16 個 Session 全部 `completed`，無 `recording` / `paused` 殘留。

本次驗證產生的兩份 Session 皆已正常收尾：

| Session | 樣本 | 結束原因 | 用途 |
|---|---|---|---|
| `20260814_101240_auto` | 513 | `auto_stop` | 雙 Owner 期間的資料完整性證據 |
| `20260814_112748_auto` | 96 | `auto_stop` | J/K 實機驗證窗口 |

---

## 8. 未 commit 的修改（分類）

### 產品碼（7 檔）

| 檔案 | 狀態 | 內容 |
|---|---|---|
| `test/report_monitor.py` | 新增 1791 行 | Monitor Core：Ownership / Writer Gate / Auth Recovery / canonicalization |
| `test/auto_monitor_service.py` | 新增 349 行 | Service 入口：訊號處理、log rotation、啟動診斷 |
| `tools/install_service_nssm.ps1` | 新增 503 行 | install / start / stop / status / remove |
| `test/device_control_menu.py` | 修改 −1156 | 抽離 Core（2542→1696 行）+ Observer／UI 守門 |
| `test/charge_discharge_report.py` | 修改 +102 | Phase 3.7 finalize 三段 + retry；`_fail` 記 `_error<code>` |
| `test/charge_discharge_report_config.py` | 修改 +48 | retry / output_status / Mutex / exit code 常數 |
| `test/device_control_operator.py` | 修改 +42 | 排程主開關專屬端點 |

### 測試 / 探針（16 檔）

新增：`test_phase4_auth_recovery.py`(748)、`test_phase4_service_env.py`(523)、
`test_phase4_mutex_dacl.py`(486)、`test_phase4_observer.py`(453)、
`test_phase4_ownership.py`(452)、`test_schedule_switch.py`(423)、
`test_phase4_service.py`(415)、`test_phase4_writer_gate.py`(302)、
`test_phase4_mutex_canon.py`(290)、`_p46_service_ctl_probe.ps1`(238)、
`_p44_owner_child.py`(118)

修改：`test_auto_schedule.py`、`test_dashboard_loop.py`、
`phase2_real_trigger_validation.py`、`probe_schedule_context.py`、
`run_phase3_regression.py`

### tools / 第三方 runtime dependency（1 檔）

`tools/nssm.exe`（見第 9 節）

### 文件（3 檔）

`tools/README.md`（新增）、`test/README.md`（修改）、本檔

---

## 9. NSSM：版本、SHA-256 與 runtime dependency 說明

| 項目 | 值 |
|---|---|
| 版本 | `2.24-101-g897c7ad` |
| 版本類型 | **prerelease / latest build**，**不是** stable 2.24 |
| 架構 | x64 / AMD64 |
| 來源 | <https://nssm.cc/download>，取用 `win64\nssm.exe` |
| SHA-256 | `EEE9C44C29C2BE011F1F1E43BB8C3FCA888CB81053022EC5A0060035DE16D848` |
| 檔案大小 | 368,640 bytes |
| 數位簽章 | NotSigned |
| 授權 | Public Domain — Author: Iain Patterson, 2003-2017 |

**它是 runtime dependency，不是安裝工具。** Windows SCM 實際啟動的就是
`tools\nssm.exe`，它常駐並監督 python 子行程；Service 的 ImagePath 直接指向此路徑。
檔案若被刪除或搬移，症狀不是「裝不起來」，而是**已安裝的服務下次開機起不來**，
且無法用 `-Action start` 修復，必須 remove → install 重新註冊。

**納入版本控管的理由**：① runtime dependency；② 上游無版本釘選機制（nssm.cc 的
latest build 無官方 checksum、下載 URL 不帶版本，重新下載無法保證相同二進位）；
③ Phase 4.6 的行為驗證（start/stop 語意、`AppExit`、log rotation、NSSM 原生輸出的
編碼特性）都以這一顆為前提；④ Public Domain 授權無散布限制，體積約 360 KB。

---

## 10. 最終結論

Phase 4.6 的既定目標（Windows 開機即可常駐、不開 Dashboard 也能自動建立報告）
**已完成並經實機驗證**。過程中額外發現並修正兩個原本不在計畫內、但會直接使
Phase 4 核心目標失效的缺陷：

1. **Auth Recovery** —— 長期常駐後 authenticated session 失效且永不恢復
   （HTTP 200 + application code 401，舊版 `_fail` 只認 `None` 而漏判）
2. **Mutex canonicalization** —— 路徑大小寫造成雙 Owner，使 Ownership 保證失效

兩者皆已修正、皆有離線 regression 與實機證據。

**單一 Owner 保證、Session 生命週期完整性、長期常駐可靠性** 三者現已同時成立。

### 已知限制與後續事項

- 失效週期 30 分鐘是**本設備在現行請求型態下的實測特徵**，不得寫死為產品 TTL
- UNC / 網路磁碟、8.3 短檔名、symlink / junction 不在目前部署範圍；
  若日後改用網路磁碟，`normcase` 無法解決 `\\server\share` 與 `Z:\` 的對應，需另行評估
- `monitor_owner.json` 的生命週期（多 Owner 情境下互相覆寫／刪除）在本次僅登記為
  診斷副作用，未擴大修改；Ownership 判定一律以 Windows Mutex 為準
- Phase 4.7（Service Restart / Recovery）尚未開始
