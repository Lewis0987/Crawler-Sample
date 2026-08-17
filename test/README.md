# Crawler Sample — HMI / BESS 資料擷取與設備控制工具

儲能櫃（BESS）HMI 的資料擷取（爬蟲）＋設備控制工具集。
- API Base：`http://192.168.128.110:8080/admin-api`
- 前端（HMI SPA）：`http://192.168.128.110:8853/`（Vue，Yudao/ruoyi-vue-pro）
- 輸出目錄：`D:\Crawler Sample\output`

---

## 檔案結構

| 檔案 | 角色 |
|---|---|
| `api_client.py` | API 薄封裝（session／登入／`Authorization: Bearer`／統一 timeout）。`login123.env` 存真實密碼（已 gitignore）。 |
| `device_control_scraper.py` | **共用解析核心**。PCS／電池／空調／進排風／冷卻循環等區塊解析；產生 `device_control_readonly.json`。`parse_pcs_current_status()` 於此定義（唯一）。 |
| `dashboard_scraper.py` | **唯一 Dashboard 抓取程式**。數據概覽（電池／PCS／環控）逐欄對齊 UI；`--once` 單次 / `--loop` 背景循環（`start_dashboard_loop`/`stop_dashboard_loop`，可乾淨停止、單例）；輸出 `dashboard_data.json` / `dashboard_data.csv`。 |
| `device_control_menu.py` | CLI 設備控制選單（查詢＋控制）。 |
| `device_control_operator.py` | PCS 控制模式 payload 組裝與送出（需 `--execute` + YES）。 |
| `test_pcs_modes.py` | 離線單元測試（PCS 模式／當前狀態解析）。 |
| `inspect_dashboard_sources.py` | 唯讀診斷：比對 PCS 匯流排 vs AC380 電量儀。 |
| `run_all.py` | 一鍵執行全部擷取腳本。 |

---

## PCS「當前狀態」— 100% 複製前端渲染邏輯

> 需求：`PCS當前狀態` 必須與前端 Dashboard「當前狀態」**完全一致**；Python 不得自行設計組合方式，而是**複製前端 UI 的判斷邏輯**。

### 品牌分歧：本機為 US 變體

`GET /hmiGuest/unauthorizedAccess/envCon/pcs/getBrand` 回傳 `device.brand.shengHong` → `pcsControl.vue` 據此渲染 **`pcsMode_US`**（非 TW 的 `pcsMode`）。兩者「當前狀態」邏輯**不同**：
- TW `pcsMode`：讀單一 mark `systemStatus` 的 value（本機此 mark 不存在）。
- **US `pcsMode_US`（本機採用）：由多個細分 mark 依 `oldValue` 組合 badge**。

### 前端渲染程式分析（`pcsMode_US-ad91147b.js`，去混淆後）

```js
// He：badge 設定（固定順序）
He = [
  {data:"systemOnOrOffStatus",     alwaysDisplay:true },
  {data:"systemFaultStatus",       alwaysDisplay:false},
  {data:"systemGridTiedStatus",    alwaysDisplay:false},
  {data:"systemOffGridStatus",     alwaysDisplay:false},
  {data:"systemChargingStatus",    alwaysDisplay:false},
  {data:"systemDischargingStatus", alwaysDisplay:false},
];
// 資料來源：Re() = permission 匯出 o = 函式 g = GET /hmiGuest/unauthorizedAccess/envCon/pcs
const a = await Re();                       // 請求帶 Accept-Language: zh-TW → value 為中文
a[0].metricsDataVoList.forEach(l =>
  n.value[l.mark] = { name, value, oldValue, unit });
// render「當前狀態」：
Object.keys(n.value).length > 0
  ? He.map(l =>
      ((n.value[l.data]?.oldValue==="1" && n.value[l.data]?.value) || l.alwaysDisplay)
        ? <Tag>{ n.value[l.data]?.value }</Tag> : nothing)
  : " - ";
```

**結論：US 前端「當前狀態」= 依 `He` 固定順序，`systemOnOrOffStatus` 永遠顯示、其餘 5 個 `oldValue==="1"` 才顯示，badge 文字用該 mark 的中文 `value`，以「 / 」串接。**

### 對照表

| 項目 | 內容 |
|---|---|
| **1. 前端使用的原始 mark** | `systemOnOrOffStatus`、`systemFaultStatus`、`systemGridTiedStatus`、`systemOffGridStatus`、`systemChargingStatus`、`systemDischargingStatus`（**不讀 `systemStatus`**） |
| **2. mark → 中文標籤** | 用各 mark 的 `value`（API 帶 `Accept-Language: zh-TW` 回中文，如 `停止`/`執行`/`故障`/`併網`/`離網`/`充電`/`放電`） |
| **3. 顯示條件** | `systemOnOrOffStatus`：永遠顯示；其餘 5 個：`oldValue==="1"` 且 `value` 非空才顯示；不補「正常/未知」 |
| **4. 顯示順序** | 固定為 `He` 順序（onOff → fault → gridTied → offGrid → charging → discharging），不改順序、不固定段數 |
| **5. Python 與前端是否一致** | **是，100% 一致**（同端點、同 `Accept-Language: zh-TW`、同 `He` 順序與條件） |

### Python 實作（`device_control_scraper.py`，唯一定義）

```python
_PCS_STATUS_CONFIG = [
    ("systemOnOrOffStatus", True),      # 執行
    ("systemFaultStatus", False),       # 正常
    ("systemGridTiedStatus", False),    # 併網
    ("systemOffGridStatus", False),     # 離網
    ("systemChargingStatus", False),    # 充電  
    ("systemDischargingStatus", False), # 放電
]

def parse_pcs_current_status(metrics):
    marks = {i.get("mark"): i for i in (metrics or []) if isinstance(i, dict) and i.get("mark")}
    statuses = []
    for mark, always_display in _PCS_STATUS_CONFIG:
        item = marks.get(mark)
        if not item:
            continue
        value = str(item.get("value") or "").strip()
        old_value = str(item.get("oldValue") or "").strip()
        if not value:
            continue
        if always_display or old_value == "1":
            statuses.append(value)
    return " / ".join(statuses)
```

資料來源（唯一共用）：`get_pcs_current_status(client)` → `fetch_pcs_status_data()`（帶 `Accept-Language: zh-TW`）→ `parse_pcs_current_status()`。
共用此**同一函式與同一來源**（無重複邏輯）：
- `device_control_scraper.parse_pcs()`（`pcs_status_data` 為 zh-TW 抓取）→ 寫入 `device_control_readonly.json`
- `device_control_menu.py` → 顯示 `device_control_readonly.json`

> 註：`dashboard_scraper.py` 的「數據概覽」PCS 區塊改用自身 `summarize_pcs()`（啟停狀態取 value、併網/離網與充放電取 oldValue），與上述「PCS當前狀態 badge」為不同用途、不共用。

> 注意：mode 類解析（`PCS工作模式`/`PCS功率控制模式`/`PCS控制模式`）仍讀 **enum**（不帶語言標頭）的那份資料，故 zh-TW 僅用於當前狀態 badge，不影響 mode 解析。

### 目前實測（本機當下）

`systemOnOrOffStatus.value=停止`（永遠顯示）、`systemGridTiedStatus.oldValue="1" value=併網`，其餘 5 個 `oldValue="0"` → 隱藏。
四支輸出與 JSON 皆為：**`PCS當前狀態：停止 / 併網`**（與 HMI Dashboard 一致，不再出現 `-`）。
若之後充電/放電/故障等 `oldValue` 變 `1`，會自動依 `He` 順序加入對應 badge（例 `執行 / 併網 / 充電`）。

---

## 執行方式

```bash
python device_control_scraper.py     # 產生 device_control_readonly.json + 主控台摘要
python dashboard_scraper.py --once   # Dashboard 抓一次（數據概覽）→ dashboard_data.json / .csv
python dashboard_scraper.py --loop   # Dashboard 背景循環（每 INTERVAL_SECONDS 一次，Ctrl+C 停止）
python device_control_menu.py        # CLI 設備控制選單
python run_all.py                    # 一鍵執行全部
python test_pcs_modes.py             # 離線單元測試
```

登入密碼放於 `login123.env`（`HMI_PASSWORD=...`），**已 gitignore，切勿提交**。

# 自動排程報告（Auto Schedule Report）
## Phase 1：Report Framework（報告框架）
- 建立 ReportSession
- 建立 Report 架構
- CSV / Excel 輸出
- Summary 統計

Phase 2：Auto Start（自動建立報告）
- 排程自動建立 Session
- Scheduler 整合
- Dashboard 監看
- Resume 機制

Phase 3：Auto Lifecycle（自動生命週期）
- 3.1 Device State（設備狀態判定）
- 3.2 Auto Stop Engine（自動停止引擎）
- 3.3 Auto Finalize（自動完成報告）
- 3.4 Start Debounce（啟動防抖）
- 3.5 Schedule Robustness（排程穩定性）
- 3.6 Resume & Recovery（恢復機制）
- 3.7 Reliability（可靠性強化）
- 3.8 Validation（完整驗證）

Phase 4：Auto Monitor Service（背景監控服務）
- Service 常駐監控
- Dashboard 解耦
- Session Ownership
- Windows Service
- 4.1 | 架構盤點 |（COMPLETE）
- 4.2 | 抽離 report_monitor.py，device_control_menu.py 改為 import | **Phase 3 Regression 734/734 必須維持 PASS，功能零改變** |（COMPLETE）
- 4.3 | auto_monitor_service.py 可獨立執行 | 不開 Dashboard，排程仍可自動建立/停止/完成報告 |（COMPLETE）
- 4.4 | Ownership（Monitor Owner / Session Owner） | Dashboard 與 Service 不會重複建立 Session |（COMPLETE）
- 4.5 | Dashboard Observer 模式 | Dashboard 開關完全不影響 Auto Report |（COMPLETE）
- 4.6 | Windows Service 包裝 | Windows 開機即可常駐 |（COMPLETE，見 docs/Phase4.6_Closure_Report.md）
- 4.7 | Service Restart / Recovery | Service 重啟後可 Resume，不重複建立 Session |（COMPLETE）
- 4.8 | Phase 4 Validation | Regression + Service + 實機驗證全部 PASS |（COMPLETE）

**Phase 4 = COMPLETE**（2026-08-17）
Full Regression 1529/1529 PASS、FAIL 0、SKIP 0、Environment residue PASS。
完整驗證矩陣、兩個關鍵實機發現、Recovery 兩條路徑與已知限制見：
[../docs/Phase4_Closure_Report.md](../docs/Phase4_Closure_Report.md)

Windows Service 使用 NSSM 作為宿主程式（SCM 啟動 `tools\nssm.exe`，
再由它啟動並監督 `test\auto_monitor_service.py`）。
`tools\nssm.exe` 是 **runtime dependency**，安裝後不可刪除、改名或搬移。
詳細版本、授權、SHA-256 與部署／更新說明見：[../tools/README.md](../tools/README.md)

### Session Recovery：兩條路徑

Service 中斷後續接同一份 Session 有兩條路徑，**已分別實機驗證**：

**Graceful Recovery — Phase 4.6（J/K）**

```
recording
→ install_service_nssm.ps1 -Action stop（NSSM 送 Ctrl+C）
→ report_pause() → status = paused（不 finalize）
→ 取樣執行緒結束、canonical Mutex 正常 release
→ -Action start
→ recording_resume → 同一 session_id / folder / start_time
→ sample_index 續接，不建立第二份 Session
```

實機：`20260814_112748_auto`，`#84`（paused）→ `#85` 續接 → 1..96 連續。
Mutex 釋放後他人取得原因為 `acquired` 而非 `abandoned_taken`，
證明是正常釋放、未被強制終止。

**Crash Recovery — Phase 4.7**

```
recording
→ taskkill /F worker PID（非優雅中斷；atexit 不執行）
→ Session 保持 status = recording（**不會**變成 paused / completed）
→ 無 recording_pause / session_end / finalize_ok
→ NSSM AppExit Default=Exit → Service 轉 Stopped（不自動重啟）
→ -Action start
→ 取得 canonical Mutex（reason = acquired）
→ recording_resume → 同一 session_id / folder / start_time
→ #244 → #245 續接，不建立第二份 Session
```

實機：`20260814_163756_auto`，crash 後最後 `#244 @ 17:01:08`，
Service 於 `17:37:32` 啟動並 `recording_resume`，
`#245 @ 17:37:33` 續接，最終 1..293 連續、crash 前 244 筆逐筆未被覆寫。

離線 regression：`test_phase4_recovery.py`（73 項，涵蓋 A~H 與 DoD 18 項）。

**三項需要留意的契約**

1. **`abandoned_taken` 不是 production crash 的必要條件。**
   worker 是 canonical Mutex 的唯一 handle 持有者，被強殺後 OS 關閉其 handle、
   kernel object 隨即消滅，下一個行程建立的是全新物件 → `acquired`、
   `abandoned_takeover = False`。這是**正確的 production behavior**。
   `WAIT_ABANDONED` 需要另有行程持有 handle 使 object 存活才會出現，
   該路徑以離線 regression 驗證（`test_phase4_recovery.py` 契約 B）。

2. **`orphan_detected` 只記錄，不改變流程。**
   `last_sample_time` 距今超過 `ORPHAN_GAP_SEC`（300s）即記一筆事件並提示，
   **仍照常續接**。實機曾記錄斷線間隔 2185s，未影響 resume。

3. **`monitor_owner.json` 在 crash 後會保留舊 worker PID。**
   強制終止不執行 `_delete_owner_file()`，屬預期現象。
   Ownership 判定一律以 Windows Mutex 為準，不看此檔。

**本次驗證後另發生的外部事件（非 Crash Recovery 缺陷）**

Recovery 正常運作約 8 分鐘、新增 49 筆樣本後，於 17:43 起發生
**整體設備通訊中斷**（authed 與 guest 端點全部 `連線逾時`）。
產品正確地未誤觸發 Auth Recovery（該機制要求 authed 失敗但 guest 正常），
而是走 `communication_error` 安全收尾：
`session_end(reason=communication_error)` → `finalize_ok`，
輸出檔完整、`output_status` 無 failed。
此事件另行登記為設備／網路層問題，不列為 Crash Recovery 缺陷。

Phase 5：Production Ready（正式版本）
- 長時間穩定測試
- 壓力測試
- 實機驗證
- 文件整理
- Release