# Jietech Dashboard 爬蟲

抓取 Jietech 內網 Dashboard 各頁籤資料，整理成 **JSON 原始資料**、**CSV 匯出檔**與 **console 可讀摘要**。

涵蓋頁籤：

1. 數據概覽（Dashboard Overview）
2. 電池數據（Battery Data）
3. 環控數據（Environmental Control）
4. 告警記錄（Alarm Records）
5. 設備控制（Device Control，僅讀取 / 記錄，不做控制）

專案位置：`D:\Jietech\test`

---

## 一、環境需求

```
pip install requests
```

所有 scraper 只使用 HTTP **GET**（設備控制頁另需登入，見第五節）。API 位於同主機 8080 埠：

```
BASE_URL = http://192.168.128.110:8080/admin-api
```

（`data-overview` / 電池 / 環控 / 告警使用 `/hmiGuest/unauthorizedAccess/...` 免登入公開端點。）

---

## 二、檔案角色總覽

| 檔案 | 角色 | 直接產出資料檔 |
|---|---|---|
| `api_client.py` | 共用 HTTP 層：session、BASE_URL、`get` / `get_many`、`unwrap`、`login`（Bearer token） | 否 |
| `dashboard_scraper.py` | 共用解析工具：`_flatten_metrics`、`_fmt_metric`、`_zh`、`STATUS_MAP`、各 summarize helper；供多支 scraper 重用 | 否 |
| `dashboard_min.py` | 數據概覽爬蟲（最小版） | `dashboard_min.json` |
| `battery_data_scraper.py` | 電池數據爬蟲（Pack 摘要 + Pack 明細 20 cell） | `battery_data.json` / `.csv`、`battery_cells.json` / `.csv` |
| `env_data_scraper.py` | 環控數據爬蟲（6 張卡 raw + curated 摘要） | `env_data.json` / `.csv`、`env_curated.json` / `.csv` |
| `alarm_records_scraper.py` | 告警記錄爬蟲（分頁抓全部 + 中文欄位/可讀時間/排序） | `alarm_records.json` / `.csv` |
| `device_control_scraper.py` | 設備控制頁盤點 / 唯讀查詢（只讀、不控制） | `device_control_readonly.json` |
| `run_all.py` | 總入口，依序執行五支並彙總 | （呼叫各 scraper） |

---

## 三、各支 scraper 說明

### 1) `dashboard_min.py` — 數據概覽

抓首頁「數據概覽」，包含電池概覽、PCS 概覽、環控概覽、告警資訊；console 印摘要，完整原始資料寫入 JSON。

```
cd D:\Jietech\test
python dashboard_min.py
```

輸出：`dashboard_min.json`

---

### 2) `battery_data_scraper.py` — 電池數據

資料來源為單一端點 `battery/getPackInformation`（免登入、無參數，一次回全部 Pack，每個 Pack 的 `packList` 已含 20 個 cell）。輸出拆成兩層：

**A. Pack 摘要層**（`battery_data.json` / `battery_data.csv`，一個 Pack 一列）
欄位：`packNo, totalVoltage, maxCellVoltage, maxCellNo, minCellVoltage, minCellNo, maxTemp, maxTempCellNo, minTemp, minTempCellNo, socMax, socMin, sohMax, sohMin`

> 註：API 沒有單一 Pack 級 SOC/SOH，故以實測的 `socMax/socMin`、`sohMax/sohMin`（最高/最低）呈現。

**B. Pack 明細層**（`battery_cells.json` / `battery_cells.csv`，一個 cell 一列）
`battery_cells.csv` 欄位順序：`Pack, 序號, 電壓, 溫度, SOC, SOH, 均衡狀態`
（`battery_cells.json` 使用機器欄位 `packNo, cellNo, voltage, temperature, soc, soh, balanceStatus`）

- cell 序號為全域編號：Pack1 `1#~20#`、Pack2 `21#~40#`、Pack3 `41#~60#`…
- 均衡狀態：`0 → 無均衡`（`1 → 均衡中`，站上目前皆為 0）
- 站上為 14 個 Pack，故 `battery_cells.csv` 共 **14 × 20 = 280** 列。
- console 顯示 Pack 數量、每個 Pack 的 cell 數（不足 20 會出現 `⚠️` 警告）、總 cell 筆數，並逐 Pack 印出 20 筆 cell 明細。

console 明細格式：

```
Pack1 cell 明細：
    [ 1] 1#    | 3.211V | 21°C | 12% | 99% | 無均衡
    ...
    [20] 20#   | 3.212V | 21°C | 12% | 99% | 無均衡
```

```
cd D:\Jietech\test
python battery_data_scraper.py
```

---

### 3) `env_data_scraper.py` — 環控數據

抓 6 張環控卡：

| 卡 | endpoint |
|---|---|
| 空調 | `envCon/air` |
| 水系統 | `envCon/water` |
| UPS | `envCon/ups` |
| 串口 ttyS0 | `envCon/ttyS0` |
| AC380 電量儀 | `envCon/voltameter` |
| 多功能傳感器 | `envCon/multifunction` |

輸出兩層：

**A. 原始完整攤平**：`env_data.json` / `env_data.csv`（帶出每張卡全部 mark）
**B. curated 精簡摘要**：`env_curated.json` / `env_curated.csv`（人看的重點欄位）

curated 欄位方向：

- **多功能傳感器**：溫度、濕度、CH4、H2
- **UPS**：運行模式、運行狀態、負載率、電池容量、電池電壓、備援時間、電池電量警示、總告警、急停狀態、過載狀態
- **AC380**：AB/BC/CA 線電壓、A/B/C 相電流、總有功功率、總無功功率、總視在功率、總功率因數
- **空調**：運行狀態、工作模式、設定溫度、櫃內溫度、櫃內濕度、高溫告警、高壓告警
- **水系統**：告警
- **ttyS0**：紅/黃/綠燈、水泵運轉、液位高/低、緊急按鈕、門禁、火警、消防故障、突波保護 SPD、空調故障、排風、蜂鳴器

翻譯規則：

- curated 欄位名稱中文化。
- 狀態值透過 `dashboard_scraper.STATUS_MAP` 翻譯（例：`type.attr.run → 運轉中`、`type.attr.mainsMode → 市電模式`）。
- 數值自動帶單位（例：`22.5 ℃`、`83 V`）。
- ttyS0 的 14 個數位訊號值保留原始 `0/1`（僅做中文欄位命名，不臆測 0/1 語意）。
- `env_data.*` 保留完整 raw；`env_curated.*` 為精簡摘要，兩者並存。

```
cd D:\Jietech\test
python env_data_scraper.py
```

---

### 4) `alarm_records_scraper.py` — 告警記錄

分頁抓完整告警（回傳 `{total, rows}`，`pageSize=100` 迴圈到抓滿 `total`），輸出可讀欄位。

1. **排序**：依 `alarmTime` 由新到舊（DESC），console / JSON / CSV 一致；`alarmTime` 缺值視為 0（排最後）。
2. **告警狀態中文化**（`alarmStatusText`，經前端頁面對照確認）：
   - `alarmStatus = false → 已恢復`
   - `alarmStatus = true → 告警中`（未恢復）
3. **時間欄位**：保留原始 epoch 毫秒 `alarmTime` / `createTime`，另補可讀 `alarmTimeText` / `createTimeText`（`YYYY-MM-DD HH:MM:SS`）。
4. **告警代碼中文欄位**：保留原始 `typeMark` / `targetMark` / `val`，另補 `typeMarkText` / `targetMarkText` / `valText`（查不到對照則保留原字串）。
5. **告警級別**：`level` 中文化（`0 → 嚴重`、`1 → 一般`）。
6. **console 預覽**欄位順序與前端表格一致（不印欄位標題列），固定欄寬對齊（依顯示寬度，中文全形算 2）：

```
[ 1] PCS        | 直流輸入欠壓             | 一般     | 已恢復   | 2026-07-07 10:11:54
[ 2] BCU        | 嚴重告警                 | 嚴重     | 已恢復   | 2026-07-06 09:57:06
```

欄位對應：告警物件 = `typeMarkText`（無則 `typeMark`）｜告警內容 = `valText`（無則 `val`）｜告警級別 = `level` 中文｜告警狀態 = `alarmStatusText`｜告警時間 = `alarmTimeText`。

`alarm_records.csv` 前導 5 欄為人可讀欄位（`告警物件, 告警內容, 告警級別, 告警狀態, 告警時間`），其後保留完整明細欄位。

```
cd D:\Jietech\test
python alarm_records_scraper.py
```

---

### 5) `device_control_scraper.py` — 設備控制（唯讀）

盤點「設備控制」頁涉及的 API 並抓唯讀資料。**只讀、不控制、不觸發任何設備操作。** 執行時先登入取得 token，再查詢唯讀白名單。

登入資訊：帳號 `hmiUser`、密碼 `hmiUser123`（僅供登入取得 token 做唯讀查詢）。

```
cd D:\Jietech\test
python device_control_scraper.py
```

輸出：`device_control_readonly.json`（登入狀態 + 唯讀查詢結果 + 控制 API 清單（僅記錄））

---

## 五、設備控制安全規則（非常重要）

1. **只允許呼叫唯讀白名單 API**（例：`/client/dynamic/dataOrControl/air`、`/client/dynamic/dataOrControl/pcs`、`/client/dynamic/dataOrControl/pcs/getRunMode`、`/schedule/config/getScheduleSwitch`、`/can/v1/getBcuState`、`/can/v1/getDOAndDIMsg`、`/sys/control/getManuallyPowerState`、`/hmiGuest/unauthorizedAccess/envCon/pcs/getBrand`）。
2. **控制 API 只記錄，不呼叫**。
3. 程式以 **exact-match 白名單** 把關：`_assert_readonly()` 若命中控制路徑或不在白名單，會直接 `raise` 擋下。
4. **即使 method 是 GET，只要用途是控制也不可呼叫**（如 `manuallyPowerOn` / `manuallyPowerOff` / `faultRecovery` 皆為 GET 但屬控制動作）。

控制 API 清單（**僅記錄，不可實際發送**）：

| endpoint | method | 用途 |
|---|---|---|
| `/client/dynamic/airConditioningControl` | POST | 空調控制 |
| `/client/dynamic/dataOrControl/manualControl` | POST | 手動控制 |
| `/client/dynamic/sinexcel/dataOrControl/manualControl` | POST | 手動控制(Sinexcel) |
| `/schedule/config/editManualSwitch` | PUT | 修改手動開關 |
| `/schedule/config/editScheduleSwitch` | PUT | 修改排程開關 |
| `/can/v1/manuallyPowerOnAndPowerOff` | POST | 手動開/關機 |
| `/sys/control/manuallyPowerOn` | GET（控制） | 手動上電 |
| `/sys/control/manuallyPowerOff` | GET（控制） | 手動斷電 |
| `/can/v1/faultRecovery` | GET（控制） | 故障復歸 |

---

## 六、共用模組說明

### `api_client.py`

- 建立並重用 `requests.Session()`、共用 `BASE_URL`、統一 timeout / headers 與錯誤處理。
- 主要函式：
  - `get(path, params=None, unwrap_envelope=True)` — 單支 GET，回傳解封包後資料，失敗回 `None`。
  - `get_many(endpoints)` — 批次 GET，回傳 `{名稱: 資料或 None}`。
  - `unwrap(resp_json)` — 解芋道封包 `{code, data, msg}`（code 0/200 取 `data`）。
  - `login(username, password)` — 設備控制頁用；POST `/system/auth/login` 取得 `accessToken`，之後帶 `Authorization: Bearer <token>`。

### `dashboard_scraper.py`

- 共用解析與格式化：`_flatten_metrics`（envCon list → `{mark: {value, unit}}`）、`_fmt_metric`（組「值 單位」、可指定小數位）、`_zh`（狀態值中文化）。
- 共用 `STATUS_MAP`（i18n 狀態值 → 中文）與各 summarize helper。
- 供 `dashboard_min.py`、`env_data_scraper.py`、`battery_data_scraper.py` 等重用。

---

## 七、輸出檔案清單

| 檔案 | 來源 scraper | 內容 |
|---|---|---|
| `dashboard_min.json` | `dashboard_min.py` | 數據概覽原始資料 |
| `battery_data.json` / `battery_data.csv` | `battery_data_scraper.py` | Pack 摘要（14 列） |
| `battery_cells.json` / `battery_cells.csv` | `battery_data_scraper.py` | Pack cell 明細（280 列） |
| `env_data.json` / `env_data.csv` | `env_data_scraper.py` | 6 卡完整攤平 |
| `env_curated.json` / `env_curated.csv` | `env_data_scraper.py` | 環控 curated 精簡摘要 |
| `alarm_records.json` / `alarm_records.csv` | `alarm_records_scraper.py` | 告警完整記錄（含中文/可讀時間欄位） |
| `device_control_readonly.json` | `device_control_scraper.py` | 設備控制唯讀資料 + 控制 API 清單（僅記錄） |

---

## 八、建議執行順序

```
cd D:\Jietech\test

python dashboard_min.py
python battery_data_scraper.py
python env_data_scraper.py
python alarm_records_scraper.py
python device_control_scraper.py
```

或用總入口一次跑完：

```
cd D:\Jietech\test
python run_all.py
```

`run_all.py` 會逐步顯示 `[n/5]` 進度、每支產出的 `[OK] 檔名`、失敗顯示 `[FAIL]`（預設不中斷、繼續下一支），最後彙總成功/失敗支數與產出檔案清單。也可只跑單一頁籤：

```
python run_all.py --only dashboard
python run_all.py --only battery
python run_all.py --only env
python run_all.py --only alarm
python run_all.py --only device
```

---

## 九、維護規則 / 注意事項

1. **先盤點前端 JS / API，再寫 scraper**，不要硬猜 API 路徑。
2. **原始值與可讀值分開保留**，例如 `alarmStatus + alarmStatusText`、`typeMark + typeMarkText`、`val + valText`、`alarmTime + alarmTimeText`。
3. **排序規則與前端一致**：告警記錄用 `alarmTime` DESC。
4. **電池明細一定保留每個 Pack 20 個 cell**（14 Pack → 280 cell）。
5. **設備控制嚴禁誤觸控制命令**：只走唯讀白名單，控制 API 僅記錄。
6. 檢查資料時請「先寫暫存 `.py` 腳本再執行」，避免 `python -c` 多行字串觸發安全提示。

---

## 十、快速操作懶人包

**只抓數據概覽**
```
cd D:\Jietech\test
python dashboard_min.py
```

**只抓電池數據**
```
cd D:\Jietech\test
python battery_data_scraper.py
```

**只抓環控數據**
```
cd D:\Jietech\test
python env_data_scraper.py
```

**只抓告警記錄**
```
cd D:\Jietech\test
python alarm_records_scraper.py
```

**只抓設備控制唯讀資料**
```
cd D:\Jietech\test
python device_control_scraper.py
```

**一次全跑**
```
cd D:\Jietech\test
python run_all.py
```
