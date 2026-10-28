# 充／放電認證測試報告 —— 服務整合（P3）需求與驗收條件

狀態：**Cert Report V1 —— READY WITH KNOWN ISSUES（Freeze 候選，2026-10-29）**，見 C-8。
（C-1～C-7 保留各階段當時的紀錄；其中「未部署／未實機驗收」等字樣為當時狀態，現況以 C-8 為準。）本文件記錄服務整合必須滿足的需求與驗收條件。
P1（離線核心）程式：`test/cert_report_config.py`、`test/cert_stage_engine.py`、`test/cert_report_xlsx.py`、
`test/cert_report_replay.py`；離線範例：`output/cert_reports_offline/`。

---

## C-1 認證期間持續收集資料（2026-10-28 使用者確認）

### 需求

1. 認證模式從「開始認證」到「結束認證」**持續取樣與記錄**，包含充電、放電、停頓、休息與待機期間。
2. 階段停止（設備停止、休息、方向切換）**只影響階段分類與截圖**，不可停止或結束整份認證的資料記錄。
3. Summary 的全程溫度曲線與「整體測試」頁籤，都必須包含休息期間的有效溫度資料。
4. 只有在真正的通訊中斷、讀取失敗或數值無效時，才保留缺值（N/A）與曲線斷線，並在 Raw Data 寫明原因。
   不可補 0、沿用前值、內插，也不可把缺口直接連線。

### 預期呈現

- 參照 `output/cert_reports_offline/P1_sample_TW20251218_G90_sensitivity.xlsx`：
  「【340KW】電櫃充放2個循環溫度曲線」在休息期間有連續溫度資料，全程曲線沒有中斷，這是正式報告預期的樣子。
- `P1_sample_samples_20261027.xlsx` 是四份單次報告串接的離線範例，Session 之間沒有溫度資料：
  10/27 12:37:54～13:44:07、15:34:49～16:06:22、16:17:54～16:53:09。
  這些歷史缺漏無法補回，範例保留斷線；**這種缺漏不可視為正式認證模式的正常結果**。

### 不可沿用的單次報告行為

單次報告（`test/charge_discharge_report_config.py`）在設備待機後會自動結束 Session：

| 設定 | 值 | 行為 |
|---|---|---|
| `AUTO_HOLD_STOP_SEC` | 60 | 待機持續 60 秒 → 自動收尾，結束 Session |
| `AUTO_COOLDOWN_SEC` | 60 | 收尾後冷卻期間不建立新 Session |
| `START_CURRENT_THRESHOLD_A` | 1.0 | 電流達門檻才重新建立 Session |

因此休息期間不會有任何取樣。認證模式不可使用這套「停止即結束」的流程。服務 log 只記錄狀態、SOC 與功率，
沒有溫度，也不能作為認證資料的補充來源。

另外，單次報告的 `_sample_row`（`test/charge_discharge_report.py`）在 API 暫無值時會沿用上一筆 Rack 溫度，
而且沒有標記；認證取樣必須使用原始讀值，不可共用這個行為。

### P1 已具備的部分

- `overall_minutes()`：全程資料使用所有讀取成功的觀測，包含休息與方向無法判定的取樣。
- 只有在整分 ±容許範圍內沒有有效取樣時才顯示 N/A，圖表設定為缺值斷線（`dispBlanksAs=gap`）。
- 測試：`test_cert_p1_engine.py` O1～O3、`test_cert_p1_report.py` 缺口仍為 N/A 的檢查。

尚未具備：實際的連續取樣流程。這部分要在 P3 服務整合時實作。

### 驗收條件

| 編號 | 條件 | 驗證方式 |
|---|---|---|
| AC-1.1 | 開始認證後，取樣在充電、放電、停頓、休息期間都不中斷，直到操作人員執行「結束認證」 | 現場測試至少包含一次休息時間 ≥ 5 分鐘；檢查 Raw Data 的取樣時間連續，相鄰間隔不超過取樣週期的合理範圍 |
| AC-1.2 | 階段結束不會結束認證、不建立新的認證或單次報告 Session | 測試期間只有一個認證資料夾；單次報告的自動收尾不作用於認證 |
| AC-1.3 | 通訊正常時，休息期間的「整體測試」每分鐘都有值，沒有 N/A | 統計休息期間的 N/A 分鐘數＝0 |
| AC-1.4 | Summary 全程曲線與「整體測試」頁圖表引用同一資料範圍、標題一致，休息期間曲線連續 | 檢查圖表 XML 引用範圍；Excel 開啟目視確認 |
| AC-1.5 | 通訊中斷、讀取失敗或數值無效時，該分鐘為 N/A、曲線斷線，Raw Data 寫明原因；不補 0、不沿用前值、不內插 | 現場或模擬讀取失敗；比對 Raw Data 原始值與報表值 |
| AC-1.6 | 服務在認證中重啟時，恢復後接續同一份認證；重啟期間沒有資料的時段如實呈現為缺值，不靜默跳過 | 離線模擬＋實機驗證（需另行授權重啟） |
| AC-1.7 | 箭頭可顯示兩端觀測時間差（「間隔約 N 分」），但不因此補值或連線 | 檢查有缺口時曲線仍斷線 |

---

## 其他待整合項目（尚未確認，列出供後續討論）

- 選單：開始／結束認證、標記階段、確認不確定項目、立即截圖、重新產生報告、解除狀態。
- 單次報告與認證報告互斥（認證期間拒絕建立單次報告，反之亦然）。
- 選單指令時間（Tc）紀錄，用於停止指令提示。
- 認證取樣週期、整分取值容許範圍、G、確認門檻等參數仍為暫用值。
- 測試資訊輸入：電櫃規格、循環數（不可自動推算）、冷氣溫度、硬體概述、測試條件。
- 階段結束截圖：見下方 C-2。

---

## C-2 階段截圖整合（P2 已完成離線模組，2026-10-28）

P2 程式：`test/cert_screenshot.py`（截圖與背景工作）、`test/cert_report_xlsx.py`（圖片插入）、
`test/cert_p2_demo.py`（模擬事件示範）。截圖紀錄為 `<測試資料夾>/screenshots.jsonl`（append-only）與
`<測試資料夾>/screenshots/*.png`。

### P3 需要接上的事件

| 階段判定事件（方案 B v3） | 截圖動作 |
|---|---|
| 首筆觀測待機 B | `submit("candidate", …)`：先拍候選截圖 |
| 待機達 W（60 秒）、確認階段結束 | `resolve(候選, "stage_end")` |
| W 內恢復同方向（停頓） | `resolve(候選, "pause")`：只留資料夾，不放入報告 |
| 方向直接切換、沒有觀測到待機 | `submit("stage_end", …)` |
| 候選或階段結束截圖失敗 | `submit("delayed", …, ref_job=原工作)`：報告標示「非停止當下畫面」 |
| 操作人員要求 | `submit("manual", …)` |

### 驗收條件

| 編號 | 條件 |
|---|---|
| AC-2.1 | 截圖在背景執行，取樣週期不因截圖而延遲（比對截圖期間的取樣間隔） |
| AC-2.2 | 每個階段結束都有數據概覽與即時熱度截圖，或有如實的失敗紀錄與原因 |
| AC-2.3 | 停頓點截圖只在測試資料夾，不出現在報告階段結束位置 |
| AC-2.4 | 截圖類型、實際擷取時間、頁面時間、觸發原因都保留在 `screenshots.jsonl`；報告圖片上方不放多行說明（2026-10-28 使用者確認），只在必要時以單行標示：補拍「非停止當下畫面」、資料未更新、Cell 缺值；失敗與尚未截圖在預留框內如實寫出原因 |
| AC-2.5 | 服務帳號（LocalSystem）下 headless Chrome 可正常啟動與截圖（P2 只在使用者帳號驗證過） |
| AC-2.6 | 設備在線時實際擷取的圖片範圍、清晰度與 2026-10-27 確認成品一致（P2 期間設備離線，尚未驗證） |
| AC-2.7 | 操作人員拆分階段後，截圖歸屬仍正確（以階段開始時間對應；拆分情境尚未驗證） |
| AC-2.8 | 即時熱度截圖只在「區塊只屬於設定櫃號、Pack 編號集合與設定完全相同、每 Pack 格數正確、必要欄位有數值、Cell 缺值不超過上限」時判成功；允許缺值欄位（Sensor 等）只記註記 |

### P2 狀態（2026-10-28，如實區分）

| 項目 | 狀態 |
|---|---|
| 截圖模組、背景執行、失敗處理、Excel 圖片插入 | 已實作，離線測試通過（程式測試，不等於實機驗收） |
| 使用 2026-10-27 確認成品的 Excel 版面 | 已驗證 |
| 兩個頁面的本次正常成功截圖（AC-2.6） | **尚未驗證**：10/28 兩次唯讀檢查（P2 期間、13:21）HMI 都無法連線，6161 獨立櫃 110 API 回傳空資料 |
| LocalSystem 服務帳號啟動 Chrome（AC-2.5） | **尚未驗證**，列為 P3 整合項目 |

### 即時熱度區塊驗證（`cert_screenshot.heat_validate`，參數 `cert_report_config.HEAT_*`）

- 定位只用櫃名與固定標籤，判定改由內容驗證，不再依部分 Pack 存在或區塊寬度判斷。
- 範圍：區塊內只能有設定櫃號（目前 110），出現 109 等其他櫃即不符；Pack 編號集合須與設定（目前 1～14）完全相同，
  缺漏、重複、非預期都判「擷取範圍不符」；每 Pack 須有 20 格 Cell 溫度。
- 資料：必要欄位（Cell 極值 最大／最小溫度、溫差）須有數值；Cell 溫度缺值不得超過上限（暫用 0 格）；全部「-」判「頁面資料無效」。
- 允許缺值欄位：Sensor（已知多功能傳感器關閉）、PCS 環境／櫃內／模組溫度、當前 SOC、功率 —— 顯示「-」或「?」只寫入說明列註記。
- 無資料時的 19 Pack 版面：**原因已確認**（頁面原始碼，2026-10-28 唯讀讀取）。6161 頁面在 `/api/temperature?no=1`（獨立櫃 110）
  回傳空資料時，以 `Array.from({length: 19})` 產生 Pack 1～19、全部「-」的預設版面（短版排版）；有資料時依 API 實際 Pack 數排版
  （10/27 為 14 個）。10/27 無資料的 109 號櫃也是同一 19 Pack 版面。P2 報告中寫的「Pack 1～20」有誤，實際為 1～19。
- 6161「更新時間」是瀏覽器本機時間（`formatDate(new Date())`），重新開頁時幾乎等於擷取時間，**不能代表設備資料新舊**；
  報告說明列已標示「瀏覽器取得資料的時間，非設備量測時間」。設備端的資料時間戳目前頁面沒有提供（未驗證 API 有資料時是否含時間欄位）。

---

## C-3 P3 開始前盤點：其他人修改的既有檔案（2026-10-28 唯讀盤點，未修改、未還原）

`git diff` 顯示以下三個檔案有未 commit 的修改（非本認證工作所改；最後修改時間 11:51:00、11:52:13、12:04:53）：

| 檔案 | 差異內容 | 與認證功能的關係 |
|---|---|---|
| `test/charge_discharge_report_config.py` | `END_REASON["control_stop"]` 改為「停止充放電（手動停止）」；新增 `MANUAL_STOP_CLAIM_ENABLED/FILE/ACTIONS/REASON/MAX_AGE_SEC(180)` | `device_control_action_result.json`（action、control_success、dry_run、timestamp）可作為認證「停止指令時間 Tc」來源；該檔只保存最近一次結果，必須有時間窗 |
| `test/device_control_menu.py` | `report_on_stop_control()` 在 Observer 時印出說明（仍不寫檔）；`report_view_latest()` 標示進行中 Session | 再次確認：服務 ESSAutoMonitor 常駐為 Monitor Owner，選單視窗是 Observer、不能寫入 → 認證的開始／結束／標記階段必須設計「選單 → 服務」的溝通方式 |
| `test/report_monitor.py` | 新增 `_manual_stop_claim()`（唯讀、Fail Closed）；收尾時跨行程認列人工停止；finalize 事件 origin 依實際來源標示 | 單次報告的 idle hold 自動收尾仍存在；認證模式不可沿用，且需與單次報告互斥 |

P3 開始前須重新盤點一次（12:04 仍有修改，代表其他人可能仍在進行中），整合時不覆寫或還原這些修改。

### P3 仍須驗證（本次只記錄，未整合、未部署）

1. 開始認證到結束認證持續記錄，休息期間不中斷（C-1、AC-1.1～1.7）。
2. 單次報告與認證報告互斥。
3. 選單與服務溝通（Observer 不寫入的限制下，指令如何交給 Owner）。
4. Ownership：認證 Session 由誰擁有、服務重啟後如何接續。
5. LocalSystem 下的截圖能力（AC-2.5）。
6. 設備在線時的實際成功截圖（AC-2.6）。

---

## C-4 P3 實作（2026-10-28：程式＋模擬測試完成；未部署、未實機驗收）

### 架構

| 元件 | 檔案 | 職責 |
|---|---|---|
| 請求機制 | `test/cert_requests.py` | 選單 → 服務的請求／結果檔（request_id、所屬 cert_id、status、重複保護、逾期不執行、原子寫入） |
| 服務端核心 | `test/cert_runtime.py` | 處理請求、持續取樣、每筆後以 P1 引擎重新分析並觸發 P2 截圖、續接、產報（新檔不覆蓋） |
| 服務組裝 | `test/cert_service.py` | 只在 service 角色的 Monitor Owner 啟動；沿用共用 client 與 `_CLIENT_LOCK`；安裝單次報告互斥 |
| 選單操作 | `test/cert_menu.py` | Menu 21～27：只送請求、讀狀態與結果，不寫認證資料、不需要 Ownership |
| 互斥 hook | `test/report_monitor.py` | `set_cert_active_provider` + `_cert_block()`：認證中拒絕單次報告四個建立入口（預設未安裝） |
| 服務入口 | `test/auto_monitor_service.py` | `run()` 先啟動認證（若啟用）再續接單次報告 |
| 選單入口 | `test/device_control_menu.py` | 功能啟用時加入「認證測試」卡片；認證進行中開啟選單**不取得** Ownership |

功能總開關 `cert_report_config.CERT_MODE_ENABLED`，**預設 False**：關閉時選單、服務、單次報告行為與 P3 前相同（回歸測試確認）。

### 設計決定

- **記錄只在背景服務**：選單即使持有 Ownership（服務未執行時）也不記錄認證，並拒絕開始認證
  —— 否則關閉選單會中斷記錄。認證進行中開啟選單不取得 Ownership，服務重啟時可立即接手。
- **請求**：逾時顯示「結果待確認」與請求 ID，可用 Menu 27 查詢，不自動重送；同一 request_id 只執行一次；
  認證中再送「開始」→ 拒絕；請求建立超過 300 秒才被處理 → expired、不執行。
- **全程取樣**：開始到結束每 10 秒（暫用）一筆，含休息；不套用單次報告 idle 60 秒收尾。
  原始值照存；任一量測欄位有值即保留整筆（電流缺值時溫度照存）；全部失敗也寫一筆 ok=False＋原因。
- **續接**：服務重啟讀回同一認證，記錄中斷期間與原因；中斷前未完成的截圖補記失敗。
- **產報**：每次新檔 `認證測試報告_<cert_id>_NN[_未完成|_中途].xlsx`，先寫 `.partial` 成功才改名；
  失敗保留資料與舊報告，記錄錯誤，可重產。來源說明寫入活頁簿屬性「描述」（不改動已確認版面）。
- **結束認證**：只結束記錄，不送停止指令；最後一筆仍在充／放電或狀態未知 → 結果提示並標記未完成。
- **指令時間 Tc**：每秒快照 `device_control_action_result.json`（寫檔時間，非送出瞬間）；該檔只保留最近一次，
  兩次快照間的多個指令可能漏記 —— 不是完整歷史紀錄。開始前的舊指令不記。
- **截圖事件**：每筆取樣後重新分析全程：首筆觀測待機 B（剛發生時）→ 候選；W 內恢復同向 → 停頓點；
  階段結束確認 → 候選轉階段結束；無候選（直接切換）→ 階段結束（過時則延遲補拍）；某頁失敗 → 該頁延遲補拍一次。

### 模擬測試（`test/test_cert_p3_integration.py`，61 項）

功能關閉不變、等待逾時→結果待確認→查詢、重複請求、逾期請求、認證 ID 不符、單次↔認證雙向互斥（四入口＋fail closed）、
6 分鐘休息持續取樣與全程曲線、通訊失敗 N/A、電流缺值保留溫度、指令快照、服務重啟續接（停機 3 分鐘 N/A）、
截圖不阻塞取樣、截圖歸屬（停頓點／階段結束／延遲補拍／兩頁分別記錄）、階段標記與不確定項目確認、
產報失敗重試不覆蓋、結束時仍運行標記未完成、不含控制出口、三種啟動順序的 Ownership（暫存 Mutex）、正式兩執行緒模式。
模擬報告副本：`output/cert_reports_offline/P3_simulation/`（模擬讀值與模擬時鐘，非實際認證）。

### 尚未驗證（需部署後實機驗收）

| 編號 | 項目 |
|---|---|
| AC-3.1 | 部署：`CERT_MODE_ENABLED=True` 後重啟服務（需另行授權），服務 log 出現「[CERT] 認證記錄執行緒已啟動」 |
| AC-3.2 | 實機持續取樣：取樣間隔符合 10 秒（暫用）、與單次報告監看並行時無 login storm、`_CLIENT_LOCK` 無長時間阻塞 |
| AC-3.3 | 6161 `/api/temperature?no=1` 的 `st`／`pt.moduleTemperature` 確實對應「傳感器溫度」「PCS 模組溫度」 |
| AC-3.4 | 實機休息 ≥5 分鐘：休息期間每分鐘有溫度（AC-1.1、1.3）；真正斷線才 N/A |
| AC-3.5 | 實機截圖（AC-2.5 LocalSystem 啟動 Chrome、AC-2.6 設備在線成功截圖）與截圖期間取樣不延遲（AC-2.1） |
| AC-3.6 | 選單（Observer）送請求 → 服務處理 → 結果顯示；服務停止時請求逾期不執行 |
| AC-3.7 | 認證中按 Menu 4～7：設備控制照常、不建立單次報告；Tc 快照記入 commands.jsonl |
| AC-3.8 | 服務重啟續接（需另行授權重啟）：同一 cert_id、中斷期間 N/A |
| AC-3.9 | 暫用參數（取樣週期、G、TCONF、待機電流、±20 秒、截圖等待／重試、請求逾期 300 秒、選單等待 20 秒）現場確認 |

### Cell 最大／最小電壓（2026-10-28 新增）

- 來源：`charge_discharge_report.read_all` 由**既有** `rackExtremeValueInformation` 回應解析
  `rackCellMaxVoltage`／`rackCellMinVoltage` → `rack_cell_max_voltage_v`／`rack_cell_min_voltage_v`（不新增請求；原欄位與單次報告 samples.csv 不變）。
- 意義：目前 110 號櫃**單一 Rack** 全部 Cell 的極值（10/27 快照與 280 顆 Cell 明細比對一致）；不延伸宣稱為多 Rack 全系統極值；不是總電壓。
- 報告：各階段頁、整體測試、Raw Data 於「Rack 最小溫度」後加入兩欄，單位 V、格式 0.000（儲存原始精度）；舊資料無此欄 → N/A。
- AC-3.10：實機確認 API 實際精度（10/27 為 0.001 V）與報告值一致。

---

## C-5 本次正式認證參數狀態（使用者確認紀錄）

| 項目 | 狀態 | 內容／說明 | 確認時間 |
|---|---|---|---|
| 電櫃規格 | **CONFIRMED** | **250KW**（Menu 21 開始認證時輸入；報告標題「【250KW】…」） | 2026-10-28 |
| 340KW | HISTORICAL ONLY | 僅為 2025-12-18 歷史範本的規格，**不得用於本次認證** | 2026-10-28 |
| 循環數 | NOT DEFINED | 程式不自動推算；範本「2個循環」屬歷史紀錄 | — |
| 控制模式 | NOT DEFINED | 2026 C-0 曾用直流恆流／直流恆功率；範本為定功率 170kW（皆歷史） | — |
| 放電設定值 | NOT DEFINED | 歷史實測約 −136～−148 A（非設定值、非規格） | — |
| 充電設定值 | NOT DEFINED | 歷史實測約 135 A；另有 31.6 kW 直流恆功率紀錄（皆歷史） | — |
| 滿放停止條件 | NOT DEFINED | 歷史：SOC 1%（2025）、SOC 0%／電池下電（2026）——非規格 | — |
| 滿充停止條件 | NOT DEFINED | 歷史：SOC 99～100%——非規格 | — |
| 設備保護數值門檻 | NOT DEFINED | HMI 閥值管理只有告警等級→聯動規則，數值在 BMS 內部未取得 | — |

開始認證前，NOT DEFINED 項目需由使用者確認；不得以歷史觀察值代替。

---

## C-6 控制紀錄可追溯性（2026-10-28 實作，離線驗證；**需重啟服務才生效**）

- 對照表集中於 `test/cert_controls.py`（action → 方向／控制模式／單位），與 `device_control_operator.ACTIONS` 由測試交叉檢查。
- `commands.jsonl` 每筆：`t`、`seen_at`、`action`、`cmd`／`direction`、`control_mode`、`control_mode_text`、`setpoint`（取自控制結果檔 `power`）、
  `unit`、`control_success`、`verify_success`、`success`、`dry_run`、`blocked`、`note`；**不寫階段**、不存 token／payload。
- 報告新增「控制紀錄」頁（整體測試之後、Raw Data 之前）：時間、階段、方向、控制模式、設定值、單位、Action、控制結果、驗證結果、整體結果、備註。
  階段於產報時依 `cert_stage_engine.command_stage()` 對應（容許 `CMD_STAGE_MATCH_SEC`=120 秒，暫用）；不是恰好一個階段 → N/A。
- 舊資料：模式／單位依 action 對照；設定值、驗證結果 N/A；無指令來源（TW CSV）→「無控制紀錄」。
- AC-3.11：實機以 Menu 5／6 操作後，控制紀錄頁設定值、單位、階段正確。

---

## C-7 設備狀態備註（2026-10-28 離線完成；未部署）

- observation `flags` 新增（取自同一次 `read_all`，不新增請求）：`battery_power_status`（已上電／已下電／切換中／未知）、
  `pcs_running_flag`、`pcs_standby_flag`、`pcs_status`（後三者僅診斷）。
- 顯示（`cert_report_xlsx.status_text／device_state`）：充電階段→第N次充電；放電階段→第N次放電；PCS 故障→N/A；
  已下電→停機；已上電且無充放電、無故障→待機；未知／切換中／None／讀取失敗／舊資料→N/A。
  不以電流 0、idle、PCS 運行／待機旗標推論。
- 套用：整體測試「備註」、Raw Data「狀態」（說明欄保留分析狀態與上下電原始值）；階段頁備註保留分析標記並附設備狀態（例「停頓（待機）」）。
  Summary 箭頭「休息 N 分」為休息時間，不變。內部 Stage Detection 不變。
- 驗證結果：True→成功、False→失敗、partial→部分驗證、timeout→驗證逾時、None／缺→N/A、其他→N/A＋備註原始值。

---

## C-8 Cert Report V1 Freeze（2026-10-29）

### C-8.1 V1 定位

**READY WITH KNOWN ISSUES** —— 不是 Prototype。目前**沒有 Blocking Bug**。

| 項目 | 狀態 | 依據 |
|---|---|---|
| Offline Regression | 完成 | 正式 runner `run_phase3_regression.py` 1838/1838；Cert P1 Engine 62/62、P1 Excel 56/56、P2 61/61、P3 105/105；Phase 6.6 50/50；D.3-B 96/96（見 C-8.11 KNOWN ISSUE #2） |
| Field Validation | 完成 | CERT_20261028_165847（放電，C-8.4）、CERT_20261028_180341（充電，C-8.5） |
| Multi-Stage lifecycle 架構 | 完成 | 程式架構確認（C-8.2、C-8.3）＋P3 多階段模擬；TW 2025-12-18 離線回放 4 個階段 |
| Manual Stop 路徑（Menu 7） | 完成 | CERT_20261028_165847 |
| Device Auto Stop 路徑（設備自行停止） | 完成 | CERT_20261028_180341 |
| Cell 最大／最小電壓 | 完成 | 兩次實機皆有值；TW 歷史回放維持 N/A |
| 控制紀錄 | 完成 | 兩次實機控制紀錄頁；舊格式相容由 P3 驗證 |
| battery_power_status | 完成 | CERT_20261028_180341 69／69 筆有值；待機／停機／N/A 實機顯示 |
| 向下相容 | 完成 | 舊 observation 無上下電欄位 → N/A；舊格式 commands 照常產報；TW 無 Cell → N/A |
| C-0 互斥 | 完成 | Cert active 期間服務 log「認證測試…進行中 → 不建立單次充放電報告」，session 全程 None |
| 服務整合 | 完成 | ESSAutoMonitor（pid 13020，2026-10-28 18:00:24 起）載入目前 working tree 程式；Cert 執行緒啟動、Mutex Owner role=service |

API：`read_all()` 仍為 9 GET、順序與 HEAD 相同；Cell 最大／最小電壓取自同一份 `rackExtremeValueInformation` 回應，未新增請求。

### C-8.2 正式操作生命週期（V1）

```
Menu 21 → Start Cert → Cert Recording

每個 Stage：
  開始 Charge / Discharge → 持續取樣
  → 人工 Menu 7 或 設備自行停止
  → 實際方向轉 Idle → Idle ≥ 60 秒
  → Stage End（end_by = idle_confirmed）

還有下一個 Stage → Cert 保持 Recording → 繼續下一 Stage

全部 Stage 完成 → Menu 22 → Cert End → generate_report → Excel
```

1. **Menu 7 不是 Stage End 的必要條件。** Stage 結束依實際電流方向判定（`cert_stage_engine.analyze`）：轉為 Idle 並持續 ≥ 60 秒 → `idle_confirmed`；
   方向直接反轉 → `opposite`（C-8.17 #2）。兩者都不需要停止指令。
2. **Menu 7 只是人工 PCS Stop 控制。** 在 Cert 中只作為控制紀錄與「指令停止」的判別依據。
3. **設備自行停止同樣形成 idle_confirmed。** 階段頁標示「自行停止（無停止指令紀錄）」，不會補寫停止指令。
4. **Stage End ≠ Cert End。** Stage 結束只觸發階段結束截圖（`cert_runtime._after_sample`），Cert 狀態不變。
5. **Stage 結束後不會自動產生最終認證 Excel。**
6. **Menu 22 才代表「整份認證測試完成」。**
7. **Menu 22 會結束 Cert 並產生最終 Report**（`_h_end_cert` → `status=ended` → `generate_report`）。
8. **Menu 25 只負責 Regenerate**（`_h_regenerate`，另存新檔、不覆蓋），不負責正常結束 Cert。
9. **V1 不實作 Single Stage Auto End。**
10. **V1 不沿用一般 charge_discharge_report 的「Idle 60 秒直接 Finalize 整份報告」流程**（亦見 C-1「不可沿用的單次報告行為」）。

### C-8.3 為什麼 Cert 不自動 End（CONFIRMED DESIGN，不是 BUG）

一般 charge_discharge_report：**一個 Session = 一份報告**，因此
`Device Stop → Idle Hold（AUTO_HOLD_STOP_SEC=60）→ should_auto_end() → Finalize → Excel` 是合理的。

Cert Report：**一個 Cert 可以包含多個 Stage**，例如
`第1次放電 → Idle → 第1次充電 → Idle → 第2次放電 → Idle → …`。

若第一個 Stage 結束就 Cert End（`_h_end_cert` 會設 `status=ended`、清除 active、關閉截圖背景工作（不再接受新截圖；已排入的仍會完成）、停止取樣），將會：

- 中斷後續 Stage；
- 後續 Stage 無法加入同一份 Cert；
- Stage 之間的休息期資料停止取樣（全程溫度曲線中斷，違反 C-1）；
- C-0 mutual exclusion 提前解除（單次報告可能在測試中途被建立）。

因此 V1 確認採用：**Stage Auto End ＋ Cert Manual End（Menu 22）**。

兩套流程的分岔點：一般報告在 Idle ≥ 60 秒後由 `ReportSession.should_auto_end()` 回傳結束 → `_auto_finalize_from_loop()`；
Cert 在 Idle ≥ 60 秒後只由 `analyze()` 標記該 Stage `end_by=idle_confirmed`，`recording → ended` 的狀態轉換只存在於 `_h_end_cert()`（Menu 22）。

另：一般報告把 `battery_off`（電池已下電）、`pcs_fault`、`communication_error` 列為立即 Finalize 條件（`AUTO_END_STOP_REASONS`）；
Cert 中 `battery_power_status` 與 PCS 故障旗標只用於狀態顯示，本身不參與 Stage 判定、也不結束 Cert，資料持續記錄；
Stage 仍只依實際電流方向結束（例：CERT_20261028_180341 下電後電流歸零 → 依 Idle ≥ 60 秒以 `idle_confirmed` 結束，Cert 繼續記錄到 Menu 22）。

### C-8.4 Field Validation A —— CERT_20261028_165847

用途：DC Constant Current 140 A 放電 Field Test（電櫃規格 250KW）。

流程：`Menu 21 → 140 A 放電 → Menu 7 → PCS Stop → Idle ≥ 60 秒 → Stage 1 End → Menu 22 → Report`

| 項目 | 結果 |
|---|---|
| Cert 期間 | 16:58:47 開始 → 17:13:59 Menu 22 結束（status=ended、incomplete=None） |
| observations | 84 筆，全部讀取成功；實際取樣間隔平均 10.87 秒 |
| commands | 2 筆：17:04:29 `pcs_dc_current_discharge` 140 A、17:10:55 `pcs_stop_power`（verify_success 皆為 partial） |
| Stage | 第1次放電 17:05:14 開始；最後放電觀測 17:10:48、首筆待機 17:10:59；`end_by=idle_confirmed`；停止類型「指令停止」（對應 17:10:55 停止指令） |
| 截圖 | 數據概覽、即時熱度各 1 張（階段結束） |
| Report | `認證測試報告_CERT_20261028_165847_01.xlsx`（17:14:00）：Summary、數據概覽、即時熱度、1.第1次放電、整體測試、控制紀錄、Raw Data |

註：本報告由 C-7 部署**前**的程式產生，非階段時間顯示「休息」、驗證結果顯示 N/A，屬當時版本的正確產物；observations 沒有 `battery_power_status`，
依規則不回填、不列 BUG。**V1 Freeze 決定保留原始報告，不執行 Menu 25 重產**；原始 `_01.xlsx` 作為 Field Evidence。

結果：**Field PASS**。

### C-8.5 Field Validation B —— CERT_20261028_180341

用途：DC Constant Current 140 A 充電 Field Test。

流程：`Menu 21 → 140 A 充電 → 設備自行停止（沒有 Menu 7）→ Idle ≥ 60 秒 → Stage 1 End → Menu 22 → Report`

| 項目 | 結果 |
|---|---|
| Cert 期間 | 18:03:41 開始 → 18:16:21 Menu 22 結束（status=ended、incomplete=None） |
| observations | 69 筆，全部讀取成功；實際取樣間隔平均 11.01 秒 |
| Stage | 第1次充電 18:06:17 開始；最後充電觀測 18:12:13、首筆待機 18:12:23；`end_by=idle_confirmed`（18:13:28 待機滿 60 秒確認） |
| 停止類型 | **自行停止（無停止指令紀錄）** |
| commands | 只有 1 筆開始充電：18:05:33 `pcs_dc_current_charge` 140 A（verify_success=partial →「部分驗證」）；**不存在偽造的 `pcs_stop_power`** |
| 截圖 | 數據概覽、即時熱度各 1 張（18:12:23 候選 → 18:13:28 採用為階段結束） |
| Report | `認證測試報告_CERT_20261028_180341_01.xlsx`（18:16:23）正常產生 |
| battery_power_status | 69／69 筆 observation 有值；整體測試：充電前「待機」、充電中「第1次充電」、停止後依規則「停機」／N/A（PCS 故障） |
| C-0 互斥 | Cert active 期間正確阻擋；session=None；沒有建立單次 charge_discharge report |

結果：**Field PASS**。

另記：`cabinet_spec` 當時人工輸入為 **250W**，正確設備規格為 **250KW**。
分類：**OPERATOR INPUT ERROR**，不是程式 BUG；歷史原始資料不修改。

### C-8.6 Field Evidence —— 設備自行停止事件（CERT_20261028_180341）

觀察紀錄：SOC 100%；最後充電電流約 134.4 A；Cell Max 曾到 3.633 V；之後 `battery_power_status=已下電`；PCS 隨後 Fault；
出現單體／模組過壓相關告警及 PCS 直流輸入故障告警。

紀錄結論：**設備在滿充附近自行停止，並伴隨過壓相關告警、電池下電及 PCS DC fault。**
Possible equipment protection event / **cause not confirmed**（目前沒有足夠實證確認各事件的因果順序）。

分類：**FIELD EVIDENCE**，不是 Cert Report BUG。

### C-8.7 Cell 最大／最小電壓（V1 正式來源）

| 報告欄位 | API 欄位 | 來源 |
|---|---|---|
| Cell 最大電壓 | `rackCellMaxVoltage` | 既有 `rackExtremeValueInformation` response |
| Cell 最小電壓 | `rackCellMinVoltage` | 同上（同一份 response） |

- 不新增 API request；`read_all()` GET 數維持 **9**，GET 順序不變。
- Cert observation 保存原始值（`values.cell_vmax_v`／`cell_vmin_v`）。
- 顯示於 Stage 頁、整體測試、Raw Data（單位 V、格式 0.000）。
- 歷史資料缺值 → N/A；不 backfill、不推算（TW 2025-12-18 回放全部 N/A）。
- Field Validation：兩次實機皆可正常取得並寫入 Report。

### C-8.8 battery_power_status 狀態規則（V1 正式）

Primary Source：`battery_power_status`（取自 `getDOAndDIMsg` 主正／主負接觸器回饋）。PCS flags 只作 diagnostic。

顯示優先序（`cert_report_xlsx.status_text` → `device_state`）：

| 優先 | 條件 | 顯示 |
|---|---|---|
| 1 | Charge Stage | 第N次充電 |
| 2 | Discharge Stage | 第N次放電 |
| 3 | read failure／no sample | N/A |
| 4 | `pcs_fault_flag=True` | N/A |
| 5 | charging／discharging flag 存在（或電流方向為充放電），但沒有可靠 Stage | N/A |
| 6 | `battery_power_status=已下電` | 停機 |
| 7 | `battery_power_status=已上電`，且沒有 Charge／Discharge／Fault | 待機 |
| 8 | 未知、切換中、None、舊資料缺欄位 | N/A |

禁止推論：
- `Current == 0` → 待機（禁止）
- PCS stopped → 停機（禁止）
- 以 `pcs_running_flag`／`pcs_standby_flag` 作為待機／停機主要判定（禁止；兩者只保留作 diagnostic，寫入 Raw Data 說明欄）

### C-8.9 控制紀錄（Control Record）

- 來源：既有控制結果檔 `device_control_action_result.json`（每秒輪詢快照）；**不新增設備控制 API**，Cert 程式不含任何控制出口。
- 支援 action：AC Active Power（交流有功功率）、DC Constant Current（直流恆流）、DC Constant Power（直流恆功率）、PCS Stop（對照表 `test/cert_controls.py`）。
- 記錄欄位：command time、action、direction、control mode、setpoint、unit、control result、verify result、overall result、note。
  command time（Tc）為控制結果檔寫檔時間，非送出瞬間；該檔只保留最近一次結果。
- verify 顯示：

| verify_success | 顯示 |
|---|---|
| True | 成功 |
| False | 失敗 |
| `"partial"` | 部分驗證 |
| `"timeout"` | 驗證逾時 |
| None／欄位不存在 | N/A |
| 其他未知值 | N/A，note 保留原始值 |

- 舊 commands 缺新欄位（setpoint、verify_success、control_mode 等）：不得報錯，保持 backward compatible；模式／單位依 action 對照，缺值顯示 N/A。

### C-8.10 Backward Compatibility

- 舊 Cert observation 沒有 `battery_power_status` 時，不得以 Current、direction idle、PCS running、PCS standby 反推狀態 → 顯示 **N/A**。
- 已形成的 Charge／Discharge Stage 仍顯示「第N次充電」／「第N次放電」。
- TW Historical Replay 沒有 Cell Max／Min → N/A；不得 backfill、不得推算。

### C-8.11 Known Issues

| 編號 | 項目 | 內容 | V1 處理 |
|---|---|---|---|
| KNOWN ISSUE #1 | Cert 取樣間隔 | 目標約 10 秒；Field：CERT_20261028_165847 平均約 10.87 秒、CERT_20261028_180341 平均約 11.01 秒，中位數約 11 秒 | **ACCEPTED / DEFERRED**（不在 V1 修正範圍，不修改程式） |
| KNOWN ISSUE #2 | D.3-B 測試輸出編碼 | `test_phase6_d3b_snapshot_adapter.py` 在 Windows cp950 console（輸出導向時）印出「≈」可能造成 UnicodeEncodeError；設定 `PYTHONIOENCODING=utf-8` 後 96/96 PASS | **Test Environment Known Issue**，不是 Cert Runtime BUG |

### C-8.12 Design Gaps／Future Enhancement

| 編號 | 項目 | 內容 | 分類 |
|---|---|---|---|
| DESIGN GAP #1 | Summary 資訊 | 尚未明確列出 Cert 開始時間、Cert 結束時間、起始 SOC、結束 SOC、Stage 數量 | Future Enhancement；不是 V1 blocker |
| DESIGN GAP #2 | Menu 21 `cabinet_spec` 驗證 | 目前沒有格式驗證，無法阻擋「250W」這類人工輸入錯誤；候選：格式／選單／固定值驗證 | Future Enhancement；目前不實作 |

### C-8.13 Confirmed Design（全部不是 BUG）

| 項目 | 分類 |
|---|---|
| Stage 頁沒有 Power 欄位 | CONFIRMED DESIGN |
| Menu 22 才結束整份 Cert | CONFIRMED DESIGN |
| idle_confirmed 只結束 Stage | CONFIRMED DESIGN |
| Stage End 不等於 Cert End | CONFIRMED DESIGN |
| Stage 之間持續取樣 | CONFIRMED DESIGN |
| 支援 Multi-Stage | CONFIRMED DESIGN |
| cycles 目前只顯示，不參與自動完成判定 | CONFIRMED DESIGN |
| 不做 Single Stage Auto End | CONFIRMED DESIGN |
| battery_off 不自動結束整份 Cert | CONFIRMED DESIGN |
| PCS Fault 不自動結束整份 Cert | CONFIRMED DESIGN |

### C-8.14 C-0 Mutual Exclusion

- Cert active → C-0 不得建立單次 charge_discharge report（`report_monitor._cert_block()` 擋下四個建立入口；
  判定來源為記憶體中的 active cert 或磁碟 `active.json`＋`cert_state.json status=recording`）。
- Cert End（Menu 22）→ `_set_active(None)` → C-0 mutual exclusion 解除 → 下一輪依正常規則判斷。
- Field Validation：CERT_20261028_180341 active 期間 session=None、沒有單次報告 → **PASS**。

### C-8.15 Service Restart／Resume

- ESSAutoMonitor 重啟時，若有 active Cert（`active.json` 指向的 `cert_state.json` 為 `status=recording`），
  `CertRuntime.startup()` 以**相同 cert_id** 接續：讀回既有 observations／decisions／commands，追加 `resume` 事件與中斷區間（中斷期間無取樣 → 報告呈現缺值）。
- 不得自動建立第二份 Cert；不得重置原 observations、commands、events。
- 此項已有 Offline Regression（`test_cert_p3_integration.py` 服務重啟續接）；**不為文件再次進行實機 Service Restart**。

### C-8.16 V1 Standard Operating Procedure

| 步驟 | 操作 |
|---|---|
| 【開始】 | Menu 21 → 輸入 Cert 資訊（電櫃規格請輸入 **250KW**）→ Start Cert |
| 【Stage】 | 開始 Charge／Discharge。人工停止 → Menu 7；或設備自行停止。兩者最後都必須確認：實際 Idle → Idle ≥ 60 秒 → Stage End |
| 【下一 Stage】 | 如果還有測試 → 繼續下一個 Charge／Discharge Stage；**不要重新 Menu 21** |
| 【全部完成】 | Menu 22 → Cert End → Excel Report |
| 【重產】 | Menu 25：只在需要重新產生既有 Cert Report 時使用（另存新檔，不覆蓋） |

（SOP 中「電櫃規格請輸入 250KW」只是操作提醒，**不代表程式已有 `cabinet_spec` 格式驗證**；該驗證仍列為 C-8.12 DESIGN GAP #2。）

### C-8.17 V1 Freeze Scope

Cert Report V1 本次 Freeze Scope 至少包含以下項目：

| # | 範圍 | 內容 |
|---|---|---|
| 1 | Cert Lifecycle | Menu 21 Start Cert；Recording；Menu 22 End Cert；Report Generation |
| 2 | Stage Detection | Charge Stage；Discharge Stage；Idle detection；direction reversal handling（方向直接反轉 → `end_by=opposite`） |
| 3 | Multi-Stage | 同一 Cert 可包含多個 Stage；Stage End 不等於 Cert End；Stage 間持續取樣；不需要重新執行 Menu 21 |
| 4 | Idle ≥ 60 sec Stage Closure | 實際設備方向轉 Idle；Idle 持續達門檻；`end_by=idle_confirmed`；Stage 自動關閉；不因此自動關閉整份 Cert |
| 5 | Manual Stop | Menu 7 為人工 PCS Stop；Stop command 可記錄至 Control Record；Menu 7 不是 Stage End 的必要條件 |
| 6 | Device Auto Stop | 設備自行停止也可以形成 idle_confirmed；不要求一定存在 Menu 7；不得偽造 `pcs_stop_power`；CERT_20261028_180341 已完成 Field Validation |
| 7 | Observations | Cert 持續保存 observation；Stage 間 Idle period 仍持續取樣；保留實際 raw／diagnostic data |
| 8 | Stage Analysis | Stage direction；Stage start／end；Stage numbering；`end_by`；`end_certain`；stop type |
| 9 | Screenshot | Stage screenshot attribution；automatic screenshot；manual screenshot／reshoot；Screenshot 與 Stage 關聯 |
| 10 | Cell Max／Min | `rackCellMaxVoltage`；`rackCellMinVoltage`；使用既有 `rackExtremeValueInformation` response；不增加 API request；Historical missing value → N/A；不 backfill；不推算 |
| 11 | battery_power_status | 已上電；已下電；切換中；未知；old／missing data；待機／停機／N/A 顯示規則（C-8.8）；PCS flags 僅供 diagnostic |
| 12 | Control Record | AC Active Power；DC Constant Current；DC Constant Power；PCS Stop；setpoint；unit；control result；verify result；overall result；note；command-to-stage matching |
| 13 | Verify Result Display | True → 成功；False → 失敗；partial → 部分驗證；timeout → 驗證逾時；None／missing → N/A；unknown → N/A＋raw note |
| 14 | Excel Generation | Summary；數據概覽；即時熱度；Stage sheet；整體測試；控制紀錄；Raw Data；Screenshot embedding／attribution |
| 15 | Historical Replay | Historical source replay；舊資料缺少新欄位仍可產生報告；不修改歷史原始資料 |
| 16 | Backward Compatibility | 舊 observations；舊 commands；missing battery_power_status；missing Cell Max／Min；missing setpoint；missing verify_success；missing control_mode；不因新欄位造成舊報告失敗 |
| 17 | C-0 Mutual Exclusion | Cert active 時禁止 C-0 建立單次報告；Cert End 後解除阻擋；CERT_20261028_180341 已完成 Field Validation |
| 18 | Service Restart／Resume | active Cert 使用相同 cert_id 接續；不建立第二份 Cert；不重置 observations、commands、events；記錄 resume／interruption information |
| 19 | Menu Integration | Menu 21 Start Cert；Menu 22 End Cert；Menu 23 Stage mark／uncertain confirm；Menu 24 Screenshot／manual reshoot；Menu 25 Regenerate；Menu 26 Cert status；Menu 27 request result |

**Freeze Readiness：READY WITH KNOWN ISSUES**
**Blocking Bugs：NONE**

V1 Freeze **不包含**以下 Future Enhancement（不得因為未完成而判定 V1 Freeze Fail）：

- 修正約 11 秒實際取樣間隔
- Summary 新增開始／結束時間
- Summary 新增起始／結束 SOC
- Summary 新增 Stage 數量
- Menu 21 `cabinet_spec` 格式驗證
- Single Stage Auto End
- cycles 自動完成判定

### C-8.18 Final Documentation Audit（2026-10-29；僅文件，非 Final Freeze Audit）

- 範圍：本階段只修改 `docs/Cert_Report_Integration_Acceptance.md`（未追蹤檔，`git diff` 不列出；以全文逐段對照程式碼稽核）。
- 未新增 Python／Test／Config／Fixture 修改；正式輸出（`output/cert_reports`、`output/charge_discharge_reports`、
  `device_control_action_result.json`）與 regression 前快照的 SHA256 完全相同；追蹤檔 `git diff` 雜湊不變。未重跑 Full Regression（沿用 C-8.1 結果）。
- 對照程式碼修正的文件描述（僅文件寫法，程式不變）：
  1. C-8.1 D.3-B 註記指向改為 C-8.11 KNOWN ISSUE #2（原誤指 C-8.6）。
  2. C-8.2 第 1 點：Stage 結束除 `idle_confirmed` 外也包含方向直接反轉 `opposite`（原寫「只依 Idle」）。
  3. C-8.3：`battery_power_status`／PCS 故障旗標不參與 Stage 判定、不結束 Cert；Stage 仍依電流方向結束（原寫「不結束 Stage」不精確）。
  4. C-8.3：`_h_end_cert` 對截圖背景工作為「不再接受新截圖；已排入的仍會完成」（原寫「停止」）。
- 檢查項目：無推測寫成事實（C-8.6 標註 cause not confirmed）；Future Enhancement 未寫成已實作；Menu 7 非 Stage End 必要條件；
  Stage End ≠ Cert End；Menu 25 只重產；cycles 不參與完成判定；battery_power_status fallback 與 `device_state` 一致；舊資料不 backfill；
  250W 為 OPERATOR INPUT ERROR；SOP「250KW」提醒不代表已有格式驗證。
- C-1～C-7 為當時階段紀錄，未改寫；目前 V1 Freeze 狀態以 C-8 為準（見文件開頭說明）。
- 結果：**Blocking Documentation Issue：無 → READY FOR FINAL FREEZE AUDIT**。
