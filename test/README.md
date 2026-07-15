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
| `dashboard_scraper.py` | Dashboard 擷取；`pcs_mode_view()`、AC380 電量儀摘要；輸出 `dashboard_data.csv`。 |
| `dashboard_min.py` | 精簡 Dashboard（電池／PCS／AC380／環控／告警概覽）。 |
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
四支輸出全部共用此**同一函式與同一來源**（無重複邏輯）：
- `device_control_scraper.parse_pcs()`（`pcs_status_data` 為 zh-TW 抓取）→ 寫入 `device_control_readonly.json`
- `dashboard_scraper.pcs_mode_view()` → `get_pcs_current_status(_get_client())`
- `dashboard_min.py` → 使用 `dashboard_scraper.pcs_mode_view()`
- `device_control_menu.py` → 顯示 `device_control_readonly.json`

> 注意：mode 類解析（`PCS工作模式`/`PCS功率控制模式`/`PCS控制模式`）仍讀 **enum**（不帶語言標頭）的那份資料，故 zh-TW 僅用於當前狀態 badge，不影響 mode 解析。

### 目前實測（本機當下）

`systemOnOrOffStatus.value=停止`（永遠顯示）、`systemGridTiedStatus.oldValue="1" value=併網`，其餘 5 個 `oldValue="0"` → 隱藏。
四支輸出與 JSON 皆為：**`PCS當前狀態：停止 / 併網`**（與 HMI Dashboard 一致，不再出現 `-`）。
若之後充電/放電/故障等 `oldValue` 變 `1`，會自動依 `He` 順序加入對應 badge（例 `執行 / 併網 / 充電`）。

---

## 執行方式

```bash
python device_control_scraper.py     # 產生 device_control_readonly.json + 主控台摘要
python dashboard_scraper.py          # Dashboard + dashboard_data.csv
python dashboard_min.py              # 精簡 Dashboard
python device_control_menu.py        # CLI 設備控制選單
python run_all.py                    # 一鍵執行全部
python test_pcs_modes.py             # 離線單元測試
```

登入密碼放於 `login123.env`（`HMI_PASSWORD=...`），**已 gitignore，切勿提交**。
