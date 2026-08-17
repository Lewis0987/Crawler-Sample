# Phase 4 收尾報告 —— Auto Monitor Service（背景監控服務）

**日期**：2026-08-17
**範圍**：Phase 4.1 ～ 4.8
**結論**：**Phase 4 COMPLETE**。Full Regression 1529/1529、FAIL 0、SKIP 0，
Service 與實機驗證全部 PASS。

---

## 1. Phase 4 目標

讓充放電報告的自動生命週期**脫離 Dashboard**：

> 不需開啟 `device_control_menu.py`，Windows 開機後即有背景服務常駐，
> 智慧排程開始充/放電時自動建立報告 Session，排程結束時自動收尾產出
> Summary / Excel；同一時間只有一個寫入者。

---

## 2. Phase 4.1 ～ 4.8 完成狀態

| 階段 | 目標 | 狀態 | 主要證據 |
|---|---|---|---|
| 4.1 | 架構盤點 | **COMPLETE** | 確認 Auto Stop 已解耦，只需為 Auto Start 換位置 |
| 4.2 | 抽離 `report_monitor.py` | **COMPLETE** | 純 refactor；`device_control_menu.py` 2542 → 1696 行，行為零改變 |
| 4.3 | `auto_monitor_service.py` 可獨立執行 | **COMPLETE** | 56/56；訊號處理、log rotation、啟動診斷 |
| 4.4 | Ownership（Monitor Owner） | **COMPLETE** | 62/62（A~P 跨行程互斥） |
| 4.5 | Dashboard Observer 模式 | **COMPLETE** | 64/64 + Dashboard harness 23/23 |
| 4.6 | Windows Service 包裝 | **COMPLETE** | 見 `Phase4.6_Closure_Report.md`；I 18/18、J 31/31、K 43/43 |
| 4.7 | Service Restart / Recovery | **COMPLETE** | Crash Recovery 73/73 + 實機全鏈路 |
| 4.8 | Phase 4 Validation | **COMPLETE** | 本報告；健康檢查 26/26、Full Regression 1529/1529 |

---

## 3. Architecture 最終狀態

```
Windows SCM
  └─ tools\nssm.exe                      （Service 宿主，ImagePath）
       └─ python.exe auto_monitor_service.py --interval 10
            └─ report_monitor.py         （Monitor Core）
                 ├─ Ownership            Global\ Mutex + DACL
                 ├─ Writer Gate          三層
                 ├─ Auth Recovery        session 失效自動重登
                 └─ ReportSession        charge_discharge_report.py

device_control_menu.py（Dashboard）
  └─ import report_monitor as RM         非 Owner 時為唯讀 Observer
```

### Monitor Core 抽離（4.2）

監看決策層、Session 生命週期、設備讀取、排程 context 由
`device_control_menu.py` 抽離為 `report_monitor.py`（1791 行）。
Dashboard 改以 `import` 使用，功能零改變，Phase 3 Regression 全數維持 PASS。

### Auto Monitor Service（4.3）

`auto_monitor_service.py`（349 行）為可獨立執行的服務入口：
`tick(source)` / `install_signal_handlers()`（SIGINT / SIGBREAK / SIGTERM）/
`run(interval, …)` / `shutdown()`。
關閉語意為 `report_pause()`（paused、可續接、不 finalize）→ release Ownership。
預設監看間隔 10s（**刻意 ≠ 15s 的 Start Debounce**）。

### Ownership（4.4）

以 Windows Named Mutex 實作跨行程互斥。要防的核心風險**不是**「建立兩份
Session」，而是**同一個 Session 資料夾同時存在兩個寫入者**
（`find_active_session()` 只看 status，第二個行程會 resume 同一份並另起取樣執行緒）。

- 判定來源**只認 Mutex**，`monitor_owner.json` 僅供診斷
- `WAIT_ABANDONED` 視為成功接管（前任崩潰）
- 非取得執行緒呼叫 release → 完全不處理（Mutex 為執行緒親和）
- 任何 API 失敗一律 fail closed

### Global Mutex

```
Global\ESS_AutoMonitor_Owner_v1_<md5(normcase(abspath(output_root)))[:16]>
目前實際值：Global\ESS_AutoMonitor_Owner_v1_ad9a00aa7db0cf8f
```

`Global\` 命名空間為跨 session 前提（Service 在 session 0，Dashboard 在使用者 session）。

### Writer Gate（三層）

| 層 | 位置 | 非 Owner 行為 |
|---|---|---|
| 1 | `auto_schedule_check()` | 回 `("skipped", "not_owner")`，在 `_actual_direction` 之前 |
| 2 | `_report_start_session()` | 回 `(None, False)`，在 `ReportSession()` 與 `_SESSION_LOCK` 之前 |
| 3 | `_report_bg_start()` | 回 `False`，最後防線 |

Layer 2 的位置是實測修正的結果：原本 gate 太晚，資料夾已被建立
（離線重現：4 個 artifact + `status='recording'`）。

### Dashboard Observer（4.5）

非 Owner 時 Dashboard 不建立、不續接、不收尾任何報告，只做唯讀顯示與人工控制。
`input()` 全部留在 UI 層（Core 內任何 `input()` 都會讓無 console 的 Service 卡死）。

---

## 4. Phase 4.6 兩個關鍵實機發現

### 發現一：HTTP 200 + application code 401 —— Auth Recovery 無法觸發

Service 於 11:03:15 登入，11:33:15（整整 30:00）起 `getRunMode` /
`getScheduleSwitch` 失效，**1348 筆監看紀錄、3.5 小時完全沒有恢復**。
同時間 guest 端點全部正常，新登入的 client 也讀得到 `smart` / `True`。

`auto_schedule_check` 條件 A 要求 `mode_code == "smart"`，讀不到就永遠是
`"unknown"` → **Service 再也不可能自動建立報告**，直接打掉 Phase 4 核心目標。

根因兩層：
1. `_report_client()` 整個行程只登入一次，無任何 401 / 逾期處理
2. `read_all._get()` 只在 `v is None` 時記入 `_fail`，而 `unwrap()` 對
   `code ∉ (0,200)` 回 `{"_error": code, …}`（非 None、且不印任何訊息）

排除法證據：degraded 期間 `[警告] GET` **0 行**，而 `ApiClient.get()` 每條回
None 的路徑都會先印警告 → 回的不是 None。
實測數值（2026-08-14 17:57:38）：

```
authed_all_failed[/client/.../getRunMode:_error401,
                  /schedule/.../getScheduleSwitch:_error401], guest_ok=5/5
```

**application code = 401，HTTP 層是 200。**

修法：`_fail` 新增 `"<path>:_error<code>"` 格式（回傳值不變、不綁特定 code、
不記 msg/_raw/body）；Monitor 層在「authed 全失敗 + 至少一支 guest 正常」
連續 2 輪後丟棄 cached client，由既有 `_monitor_client()` 重新登入。
`_rebind_session_client()` 在 `_CLIENT_LOCK` 內把新 client 接回進行中的 Session。

**實機 4/4，每次 21 秒恢復，Service PID 全程不變。**

### 發現二：`D:\` / `d:\` 大小寫造成不同 Mutex → 雙 Owner

Service 以 `D:\Crawler Sample\...` 啟動、Dashboard 以
`d:/Crawler Sample/test/device_control_menu.py` 啟動，同一個 output root
算出兩顆不同的 Global Mutex：

```
D:\... → ...0c305bef986a0531
d:\... → ...fc4cd03c3cd675eb
```

根因：`os.path.abspath()` 不正規化 Windows 磁碟機代號大小寫。
後果：兩個行程各自 acquire 成功 → `is_owner()` 都是 True →
**Writer Gate 三層全部放行**。

**實際損害：無。** 當時處於雙 Owner 條件的 `20260814_101240_auto`（513 筆）
`sample_index` 完全連續、無重複 —— 風險真實存在但未造成資料污染。

修法（兩行）：`os.path.normcase(os.path.abspath(root))`。
刻意不用 `realpath`（部署路徑無 symlink / junction、NTFS 無 8.3 短檔名，皆已實測）。
rollout 依「關 Dashboard → 等 Session 自然 completed → stop → start」順序執行，
避免過渡期雙 Owner。

---

## 5. Windows Service / NSSM

| 項目 | 值 |
|---|---|
| Service | `ESSAutoMonitor`，Running，Automatic，LocalSystem |
| ImagePath | `<repo>\tools\nssm.exe` |
| Application / AppDirectory / AppParameters | python.exe / `<repo>\test` / `auto_monitor_service.py --interval 10` |
| log | `output\logs\auto_monitor_service.log`，`AppRotateFiles=1`、`AppRotateBytes=10485760` |
| 關閉語意 | `AppStopMethodConsole=30000` → Ctrl+C → `report_pause()` → release Ownership |
| 失敗策略 | `AppExit Default/0/3/4 = Exit`（**刻意不自動重啟**，維持 fail-closed） |

### Mutex DACL

```
MUTEX_SDDL      = D:(A;;0x1f0001;;;SY)(A;;0x1f0001;;;BA)(A;;0x100001;;;AU)
MUTEX_MIN_ACCESS= SYNCHRONIZE | MUTEX_MODIFY_STATE = 0x00100001
```

LocalSystem 建立的核心物件預設不授權一般使用者，且 `CreateMutexW` 對已存在
物件**隱含要求 MUTEX_ALL_ACCESS** → Dashboard 得到 `create_failed_5`
（ERROR_ACCESS_DENIED）而非 `held_by_other`，互斥語意退化成「拿不到鎖」。
修法：建立時附帶 DACL + 改用 `CreateMutexExW` 只要求最小權限。

### `-Action start` fail-closed

實測（原始位元組）：NSSM 訊息**一律走 stderr**，stdout 為空；中文在
**nssm.exe 行程內部**被轉成 `0x3F`（`?`），任何解碼都救不回來。
因此：不改 code page、不用 `chcp 65001` / iconv、不重新解碼；
`Start-SvcAndWait` 一個字都不顯示 NSSM 原生輸出，訊息由腳本自行輸出 Unicode。

判定原則（與 `Stop-SvcIfRunning` / `Remove-SvcIdempotent` 一致）：
**SCM 實際狀態＝唯一事實來源；NSSM exit code＝診斷；NSSM 文字＝不採用。**
`exit=0` 但 SCM 非 Running → 仍失敗；`exit≠0` 但 Running → 成功但輸出警示。

---

## 6. Session Recovery：兩條路徑

### Graceful Recovery（4.6 J/K）

```
recording → -Action stop → report_pause() → status=paused（不 finalize）
→ 取樣執行緒結束、Mutex 正常 release
→ -Action start → recording_resume → 同一 Session → sample_index 續接
```

實機 `20260814_112748_auto`：`#84`（paused）→ `#85` 續接 → 1..96 連續。
Mutex 釋放後他人取得原因為 **`acquired` 而非 `abandoned_taken`**，
證明是正常釋放、未被強制終止。

### Crash Recovery（4.7）

```
recording → taskkill /F worker → atexit 不執行 → status 維持 recording
→ 無 recording_pause / session_end / finalize_ok
→ NSSM AppExit Default=Exit → Service Stopped（不自動重啟）
→ -Action start → acquired → recording_resume → 續接
```

**實機證據（`20260814_163756_auto`）**

| 事件 | 時間 / 值 |
|---|---|
| crash 前最後一筆 | **#244 @ 2026-08-14 17:01:08** |
| Service start | **17:37:32** |
| `recording_resume` | 17:37:32，`sample_index = 244` |
| 第一筆新資料 | **#245 @ 17:37:33** |
| 最終 | **#1..#293 連續、無重複、無斷號** |
| 舊資料 | crash 前 244 筆**逐筆未被覆寫** |
| Session | **未建立第二份**，`session_id` / folder / `start_time` 全數未變 |
| Ownership | `acquired`、`abandoned_takeover = False` |

`orphan_detected`：**gap = 2185s**（門檻 300s），
訊息明示「本次照常續接，未改變流程」—— **只記錄、不改變 Recovery 流程**。

**Recovery 成功後另發生整體設備通訊中斷**：resume 後正常運作約 8 分鐘、
新增 49 筆樣本，於 17:43 起 authed 與 guest 端點全部逾時。
產品**未誤觸發** Auth Recovery（該機制要求 authed 失敗但 guest 正常），
而是正確走安全收尾：

```
session_end(reason=communication_error) → finalize_ok
output_status 無 failed，8 個輸出檔齊備
```

**此事件列為外部設備／網路事件，不列為 Crash Recovery 缺陷。**

### 三項需留意的契約

1. **`abandoned_taken` 不是 production crash 的必要條件。**
   worker 是 canonical Mutex 的唯一 handle 持有者，被強殺後 OS 關閉其 handle、
   kernel object 隨即消滅 → 下一行程建立全新物件 → `acquired`、
   `abandoned_takeover=False`。這是**正確的 production behavior**。
   `WAIT_ABANDONED` 需另有行程持有 handle 才會出現，以離線 regression 驗證。
2. **`orphan_detected` 只記錄，不改變流程**（`ORPHAN_GAP_SEC = 300`）。
3. **`monitor_owner.json` 在 crash 後保留舊 worker PID** 屬預期
   （強殺不執行 `_delete_owner_file()`）；Ownership 一律以 Mutex 為準。

---

## 7. Regression 最終基線

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
Phase 4.7 crash recovery          73/73  PASS
Environment residue                      PASS
--------------------------------------------------------------
Automated checks              1529/1529  PASS
FAIL 0    SKIP 0    RESULT PASS
git diff --check：exit 0
```

Phase 3 起算的成長：734 → 1271 → 1417 → 1456 → **1529**。

---

## 8. Service 實機驗證摘要

| 驗證項 | 結果 |
|---|---|
| Windows boot auto-start | 2026-08-12 重開機後自動啟動（Event 6005/6006/12） |
| 跨帳號 Ownership（4.6-B I） | `create_failed_5` → **`held_by_other`**，18/18 |
| Graceful stop → paused（J） | 31/31 |
| start → resume 同一 Session（K） | 43/43 |
| canonical Mutex rollout | 38/38，`D:\` / `d:\` / `D:/` 全部收斂 |
| Auth Recovery | **4/4**，每次 21 秒，PID 不變 |
| Crash Recovery（4.7） | 全鏈路 PASS，`#244 → #245` |
| 4.8 最終健康檢查 | **26/26** |

---

## 9. 已知限制

1. **Auth session 失效週期約 30 分鐘**是**本設備在現行請求型態下的實測特徵**，
   不得寫死為產品 TTL。45 分鐘的低頻探針（每 120s、3 支端點）未重現。
2. **UNC / 網路磁碟、8.3 短檔名、symlink / junction 不在目前部署範圍。**
   若日後改用網路磁碟，`normcase` 無法解決 `\\server\share` 與 `Z:\` 的對應。
3. **`monitor_owner.json` 的生命週期**（多 Owner 情境互相覆寫／刪除）僅登記為
   診斷副作用，未擴大修改；Ownership 判定一律以 Windows Mutex 為準。
4. **登入失敗期間 log 完全靜止。** `_monitor_client()` 的「暫停 + 每 60s
   靜默重試」刻意不輸出以免洗版，但維運人員因此無法只靠 log 區分
   「存活重試中」與「行程卡死」。4.8 健康檢查是靠 CPU 時間取樣才確認存活
   （取樣窗口必須 > `AUTO_LOGIN_RETRY_SEC` 60s，否則會量到 0）。
   屬**可觀測性**問題，非功能缺陷。
5. **NSSM / SCM 失敗自動重啟策略維持 fail-closed**（`AppExit … Exit`），
   未設定 SCM Recovery actions。crash 後需人工 `-Action start`。
6. **Windows reboot 打斷 recording 的情境未驗證**（boot auto-start 已驗證，
   但當時 `session=None`）。與 Crash Recovery 機制高度重疊，成本考量下未納入。
7. **Ownership 取得失敗時 Service 直接退出**（`EXIT_OWNER_HELD_BY_OTHER=3` /
   `EXIT_OWNER_API_ERROR=4` 對應 `AppExit … Exit`），不重試。

---

## 10. Phase 5 待辦

依 README，Phase 5（Production Ready）包含：

- **長時間穩定測試（soak test）** —— 明確不屬 Phase 4.8，保留至此
- **壓力測試**
- **實機驗證**（可涵蓋上列限制 4「log 靜止」的可觀測性改善評估）
- **文件整理**
- **Release**

另建議一併評估：限制 5（自動重啟策略）、限制 6（reboot during recording）、
限制 7（Ownership retry）是否要在 Phase 5 納入。

---

## 11. Phase 4 最終結論

Phase 4 的既定目標 —— **Windows 開機即可常駐、不開 Dashboard 也能自動建立
與收尾報告、同一時間只有一個寫入者** —— **已完成並經實機驗證**。

過程中額外發現並修正兩個原本不在計畫內、但會直接使核心目標失效的缺陷
（Auth Recovery、Mutex canonicalization），兩者皆有離線 regression 與實機證據。
Phase 4.7 為純驗證階段，**產品碼零修改**。

**單一 Owner 保證、Session 生命週期完整性、長期常駐可靠性、
Graceful 與 Crash 兩條 Recovery 路徑** —— 現已同時成立。

**Phase 4 = COMPLETE。**
