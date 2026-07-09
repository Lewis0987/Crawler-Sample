# Jietech Dashboard 爬蟲 + 設備控制

抓取 Jietech 內網 Dashboard 各頁籤資料，整理成 **JSON 原始資料**、**CSV 匯出檔**與 **console 可讀摘要**；並提供**設備控制**（低風險 action，指令式 + 選單式）。

涵蓋頁籤：

1. 數據概覽（Dashboard Overview）
2. 電池數據（Battery Data）
3. 環控數據（Environmental Control）
4. 告警記錄（Alarm Records）
5. 設備控制（Device Control：唯讀盤點 + 低風險控制）

## 專案路徑

| 用途 | 路徑 |
|---|---|
| 專案根目錄 | `D:\Crawler Sample` |
| test 目錄（所有腳本） | `D:\Crawler Sample\test` |
| output 目錄（所有產出） | `D:\Crawler Sample\output` |
| scratchpad 目錄（分析暫存） | `D:\Crawler Sample\test\scratchpad` |

> 所有 scraper / 控制腳本的輸出檔一律寫到 `D:\Crawler Sample\output`。

---

## 一、環境需求

```
pip install requests
```

所有唯讀 scraper 只使用 HTTP **GET**（設備控制頁另需登入，見第三節）。API 位於同主機 8080 埠：

```
BASE_URL = http://192.168.128.110:8080/admin-api
```

（`data-overview` / 電池 / 環控 / 告警使用 `/hmiGuest/unauthorizedAccess/...` 免登入公開端點。前端頁面則由 `http://192.168.128.110:8853` 提供。）

---

## 二、檔案角色總覽

| 檔案 | 角色 | 直接產出資料檔 |
|---|---|---|
| `api_client.py` | 共用 HTTP 層：session、BASE_URL、`get` / `get_many`、`unwrap`、`login` / `login_hmi`（Bearer token）、`.env` / config 讀取 | 否 |
| `dashboard_scraper.py` | 共用解析工具：`_flatten_metrics`、`_fmt_metric`、`_zh`、`STATUS_MAP`、各 summarize helper | 否 |
| `dashboard_min.py` | 數據概覽爬蟲（最小版） | `dashboard_min.json` |
| `battery_data_scraper.py` | 電池數據爬蟲（Pack 摘要 + Pack 明細 20 cell） | `battery_data.json` / `.csv`、`battery_cells.json` / `.csv` |
| `env_data_scraper.py` | 環控數據爬蟲（6 張卡 raw + curated 摘要） | `env_data.json` / `.csv`、`env_curated.json` / `.csv` |
| `alarm_records_scraper.py` | 告警記錄爬蟲（分頁抓全部 + 中文欄位/可讀時間/排序） | `alarm_records.json` / `.csv` |
| `device_control_scraper.py` | 設備控制頁**唯讀**盤點 / 查詢（只讀、不控制） | `device_control_readonly.json` |
| `device_control_operator.py` | 設備控制**指令式**工具（單一 action，dry-run / `--execute`） | `device_control_action_result.json` |
| `device_control_menu.py` | 設備控制**互動式選單**（數字選單，底層呼叫 operator / scraper） | （呼叫上述兩者） |
| `run_all.py` | 唯讀總入口，依序執行五支 scraper 並彙總 | （呼叫各 scraper） |

---

## 三、登入 / `.env` 狀態

設備控制頁（`run_all.py` 的 device、`device_control_scraper.py`、`device_control_operator.py`）需登入取得 `accessToken`。

前端登入時 password 會被 **SM2 加密**（`sm-crypto doEncrypt cipherMode=1`，04 前綴 C1C3C2 hex，每次密文不同）。本專案登入支援**雙模式**（皆由 `api_client.py` 統一處理）：

- **SM2 即時加密（新・已驗證，建議長期使用）**：env 放密碼原文 `HMI_PASSWORD` + 公鑰 `SM2_PUBLIC_KEY`，登入時用 `sm2_util.py` 即時加密（每次動態產生，不需固定密文）。
- **可重放密文（舊・fallback）**：env 放 `LOGIN_PASSWORD_PAYLOAD`（DevTools 抓到的密文），直接送出。

### env 檔動態搜尋規則

`api_client.py` 會**動態搜尋** env 檔（不寫死檔名），支援 `.env` 與**任意 `*.env`**（例如 `login.env`、`1.env`、`abc.env`）。

**搜尋位置**：
1. `api_client.py` 同層目錄（`D:\Crawler Sample\test`）
2. 專案根目錄（`D:\Crawler Sample`）

**優先順序（高 → 低，只載入最高的「單一」檔）**：
1. `API_ENV_FILE` 環境變數指定的檔（若存在）
2. 同層目錄的 `.env`
3. 同層目錄其他 `*.env`（依**檔名字母序**取第一個）
4. 專案根目錄的 `.env`
5. 專案根目錄其他 `*.env`（依檔名字母序）

**執行時輸出**：
- 載入時印 `[ENV] loaded: <實際路徑>`
- 若有多個候選，會先印 `[ENV] candidates:` 全清單再載入首位
- 若都找不到，印 `[ENV] no env file found`

> 驗證腳本：`python test_env_discovery.py`（B1~B3 本地離線測搜尋/排序；加 `--with-login` 才做真實登入）。

### env 檔內容格式（`test/login.env` 或任意 `*.env`）

env 檔（不論檔名）填以下欄位（**README/範本只放格式，不放真實密文**）：

```
HMI_USERNAME=hmiUser
# --- SM2 即時加密模式（建議）---
HMI_PASSWORD=<HMI 登入密碼原文>
SM2_PUBLIC_KEY=<前端 sm-crypto 用的公鑰 hex，128 或 130（04 開頭）>
# --- 可重放密文 fallback（可留空）---
LOGIN_PASSWORD_PAYLOAD=
```

欄位說明：
- `HMI_USERNAME`（可選）：預設 `hmiUser`。
- `HMI_PASSWORD` + `SM2_PUBLIC_KEY`：走 **SM2 即時加密**。
- `LOGIN_PASSWORD_PAYLOAD`：**可留空**；填了會優先於 SM2（fallback 舊模式）。

**password 取值優先順序（高 → 低）**：
1. `LOGIN_PASSWORD_PAYLOAD`（env / `login_config.json`）→ 直接送密文
2. `HMI_PASSWORD` + `SM2_PUBLIC_KEY` → `sm2_util.sm2_encrypt()` 即時 SM2 加密
3. `client.login(USERNAME, PASSWORD)` 傳入值（相容 / 測試用）

> 要用 SM2 模式，就把 `LOGIN_PASSWORD_PAYLOAD` 留空、填好 `HMI_PASSWORD` 與 `SM2_PUBLIC_KEY`。

### 目前已確認（實測）

- **SM2 即時加密登入成功**：`password_source = sm2`、`HMI login success`（`code=200`）、每次密文不同（len 214、04 前綴）皆通過。
- `accessToken` 自動帶入 `Authorization: Bearer <token>`。
- `run_all.py`（device）、`device_control_scraper.py`、`device_control_operator.py` 皆走**同一套** `api_client` 登入，讀 `test/login.env`。
- 舊 `LOGIN_PASSWORD_PAYLOAD` 模式仍保留為 fallback（實測亦可登入）。

### 實際執行 / 驗證範例

```
cd D:\Crawler Sample\test

# 雙模式登入驗證（S1~S3 離線；--with-login 走真實 SM2 登入）
python test_sm2_login.py --with-login

# 只讀查詢（會登入 → 抓設備控制唯讀狀態）
python device_control_scraper.py

# 控制（dry-run 預覽，不送出）
python device_control_operator.py --action pcs_manual_off

# 整批唯讀
python run_all.py --only device
```

### 安全注意

- ⚠️ **`.env` 與任意 `*.env` 不可提交到版控**：`test/.gitignore` 與根目錄 `.gitignore` 皆已加入 `.env`、`*.env`、`login_config.json` 規則（涵蓋 `login.env` / `1.env` / `abc.env` 等）。
- ⚠️ **`.env.example` 只能保留範例格式，不可放真實密文**（`.env.example` 不被 `*.env` 規則誤擋，可正常提交）。
- token 與 `LOGIN_PASSWORD_PAYLOAD` 只以**遮罩**顯示（前 5 + … + 後 5 / 長度），不完整輸出。

---

## 四、各支 scraper 說明（唯讀）

### 1) `dashboard_min.py` — 數據概覽

抓首頁「數據概覽」，包含電池概覽、PCS 概覽、環控概覽、告警資訊；console 印摘要，完整原始資料寫入 JSON。

```
cd D:\Crawler Sample\test
python dashboard_min.py
```

輸出：`D:\Crawler Sample\output\dashboard_min.json`

---

### 2) `battery_data_scraper.py` — 電池數據

資料來源為單一端點 `battery/getPackInformation`（免登入、無參數，一次回全部 Pack，每個 Pack 的 `packList` 已含 20 個 cell）。輸出拆成兩層：

**A. Pack 摘要層**（`battery_data.json` / `battery_data.csv`，一個 Pack 一列）
欄位：`packNo, totalVoltage, maxCellVoltage, maxCellNo, minCellVoltage, minCellNo, maxTemp, maxTempCellNo, minTemp, minTempCellNo, socMax, socMin, sohMax, sohMin`

> 註：API 沒有單一 Pack 級 SOC/SOH，故以實測的 `socMax/socMin`、`sohMax/sohMin`（最高/最低）呈現。

**B. Pack 明細層**（`battery_cells.json` / `battery_cells.csv`，一個 cell 一列）
`battery_cells.csv` 欄位順序：`Pack, 序號, 電壓, 溫度, SOC, SOH, 均衡狀態`

- cell 序號為全域編號：Pack1 `1#~20#`、Pack2 `21#~40#`…
- 均衡狀態：`0 → 無均衡`、`1 → 均衡中`
- 站上為 14 個 Pack，故 `battery_cells.csv` 共 **14 × 20 = 280** 列。

```
cd D:\Crawler Sample\test
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

**A. 原始完整攤平**：`env_data.json` / `env_data.csv`
**B. curated 精簡摘要**：`env_curated.json` / `env_curated.csv`

翻譯規則：欄位中文化；狀態值透過 `dashboard_scraper.STATUS_MAP` 翻譯；數值自動帶單位；`env_data.*` 保留完整 raw，`env_curated.*` 為精簡摘要，兩者並存。

```
cd D:\Crawler Sample\test
python env_data_scraper.py
```

---

### 4) `alarm_records_scraper.py` — 告警記錄

分頁抓完整告警（`pageSize=100` 迴圈到抓滿 `total`），輸出可讀欄位。

1. **排序**：依 `alarmTime` 由新到舊（DESC）。
2. **告警狀態中文化**：`alarmStatus=false → 已恢復`、`true → 告警中`。
3. **時間欄位**：保留原始 epoch 毫秒，另補 `alarmTimeText` / `createTimeText`。
4. **告警代碼中文欄位**：保留原始 `typeMark/targetMark/val`，另補 `*Text`。
5. **告警級別**：`0 → 嚴重`、`1 → 一般`。

`alarm_records.csv` 前導 5 欄為人可讀欄位（`告警物件, 告警內容, 告警級別, 告警狀態, 告警時間`），其後保留完整明細。

```
cd D:\Crawler Sample\test
python alarm_records_scraper.py
```

---

### 5) `device_control_scraper.py` — 設備控制（唯讀）

盤點「設備控制」頁涉及的 API 並抓唯讀資料。**只讀、不控制、不觸發任何設備操作。** 執行時先登入取得 token，再查詢唯讀白名單。

```
cd D:\Crawler Sample\test
python device_control_scraper.py
```

輸出：`D:\Crawler Sample\output\device_control_readonly.json`（登入狀態 + 唯讀查詢結果 + 控制 API 清單（僅記錄））

**狀態顯示規則**：
- 「目前狀態」取自 **guest / envCon / overview 可讀來源**；authed `/dataOrControl/air`、`/pcs`（403）僅作控制驗證、獨立顯示，不覆蓋狀態。
- **排程開關狀態：僅當 `PCS控制模式 = 智慧模式` 時才顯示**（依 API `getRunMode` 判斷：`schedule / auto / smart / intelligent` → 智慧模式；`manual` → 手動、不顯示排程開關）。這是因為排程開關只在智慧模式下有意義。

---

### 6) `threshold_config_scraper.py` — 閥值管理（唯讀）

讀取「告警抑制」開關 + 閥值列表（含 ECM/PCS/BMS 聯動控制）。**只讀：不修改閥值、不保存、不切換開關。**

```
cd D:\Crawler Sample\test
python threshold_config_scraper.py
```

API（皆 **GET**，需登入）：
- `/client/dynamic/threshold/config/config` → `{configId, suppressionFlagMainSwitch}`
- `/client/dynamic/threshold/list?pageSize=1000` → `{total, rows[…]}`（**無 pageSize 只回第一頁 10 筆**，故帶 pageSize 抓齊全部）

**⚠️ 開關值對照（依前端閥值頁：value `0`=綠色/checked=啟用、`1`=灰色=停用）**：
- 告警抑制 `suppressionFlagMainSwitch`、每列 `linkageControlSwitch`（聯動控制開關）、`enableFlag`（告警開關）：**`0` → 已啟用、`1` → 已停用**。

**其他欄位對照**：
| 欄位 | 來源 | 對照 |
|---|---|---|
| 感測器型別 | `typeName` | device.type.temperature→多功能溫溼度、voltameter→電量儀 |
| 觸發閥值 | `target`+`operatorStr`+`targetValue`+`targetUnit` | temperature→溫度、humidity→濕度、ch4→甲烷、h2→氫氣、phaseVoltageAB/BC/CA→相電壓AB/BC/CA |
| 觸發條件 | `condition`+`conditionValue` | time → 時間持續 N 秒 |
| 告警級別 | `level` | 0→嚴重、1→一般 |
| ECM | `linkageControlVo.ipcOperate` | 0→黃燈恆亮、1→紅燈恆亮 |
| PCS | `linkageControlVo.pcsOperate` | 17→無動作、2→PCS停機 |
| BMS | `linkageControlVo.bcuOperate` | 17→無動作、10→普通下電 |

**輸出**：`output/threshold_config.json`（原始 config+list）、`output/threshold_config_summary.json`、`output/threshold_config.csv`（皆保留完整 125 筆）。
**終端**：只印前 `DETAIL_PRINT_LIMIT`（預設 10）筆，並顯示 `總筆數 / 顯示筆數`；完整資料看 JSON/CSV。

---

## 五、`run_all.py` 唯讀總入口（目前全綠）

在 `D:\Crawler Sample\test` 執行：

```
python run_all.py
```

目前結果：

| task | 結果 |
|---|---|
| dashboard | OK |
| battery | OK |
| env | OK |
| alarm | OK |
| device | OK |

```
Total: 5, OK: 5, FAIL: 0, SKIP: 0
```

代表 `dashboard_min.py` / `battery_data_scraper.py` / `env_data_scraper.py` / `alarm_records_scraper.py` / `device_control_scraper.py` 都可正常輸出到 `D:\Crawler Sample\output`。

`run_all.py` 檢查產物存在時，一律看 `D:\Crawler Sample\output\` 內的檔案。也可只跑單一頁籤：

```
python run_all.py --only dashboard
python run_all.py --only battery
python run_all.py --only env
python run_all.py --only alarm
python run_all.py --only device
```

---

## 六、設備控制檔案分工（重要）

| 檔案 | 類型 | 是否送控制 | 產出 |
|---|---|---|---|
| `device_control_scraper.py` | 唯讀 | 否 | `device_control_readonly.json` |
| `device_control_operator.py` | 指令式控制 | 是（僅 `--execute`） | `device_control_action_result.json` |
| `device_control_menu.py` | 互動式選單 | 是（底層呼叫 operator） | （同上） |

### 1) `device_control_scraper.py`
- 只讀版設備控制資訊抓取，**不送任何控制命令**。
- 把設備控制頁目前可讀狀態輸出成 JSON。
- 產出：`D:\Crawler Sample\output\device_control_readonly.json`

### 2) `device_control_operator.py`
- 指令式控制工具，送出**單一**控制 action。
- 預設 **dry-run**（只印 endpoint/method/payload，不送出）；加 `--execute` 才真正送出。
- 目前支援 action（共 13）：
  - 空調：`ac_on` / `ac_off`
  - PCS 手動模式：`pcs_manual_on` / `pcs_manual_off`
  - 進排風：`vent_on` / `vent_off`
  - 冷卻循環：`cooling_on` / `cooling_off`
  - 電池上下電（高風險，需 `--yes` 二次確認）：`battery_power_on` / `battery_power_off`
  - PCS 功率控制：`pcs_charge --power N` / `pcs_discharge --power N` / `pcs_stop_power`
- 參數：`--execute`（真正送出）、`--power N`（0~150，`pcs_charge`/`pcs_discharge` 必填）、`--yes`（高風險略過互動確認）。
- 控制後回讀狀態做 verify；**不重試、不批量**；不含離網 / 故障復位 / sys 手動上下電。
- 產出：`D:\Crawler Sample\output\device_control_action_result.json`
  （含 `logged_in / action / dry_run / power / control_request / control_response / verify / control_success / verify_success / success / warnings`）

### 3) `device_control_menu.py`
- 給人工現場操作的互動式選單版，執行後用數字選單控制，不用手打 action 名稱。
- 底層仍是 `subprocess` 呼叫 `device_control_operator.py`（控制）與 `device_control_scraper.py`（查詢）。

---

## 七、目前控制驗證結果

### 1. PCS 手動模式開 / 關 — 已完整成功 ✅

- 控制腳本：`device_control_operator.py`
- action：`pcs_manual_on` / `pcs_manual_off`
- 控制 API：`PUT /schedule/config/editManualSwitch`
- payload：
  - `pcs_manual_on`：`{"schedulePlanSwitchId":1,"schedulePlanSwitch":0,"manualModeSwitch":1}`
  - `pcs_manual_off`：`{"schedulePlanSwitchId":1,"schedulePlanSwitch":1,"manualModeSwitch":0}`
- 控制結果：`control_resp: http=200 code=200 msg=operation successful`、`success=True`
- verify 可正常讀到：`schedulePlanSwitch`、`manualModeSwitch`、`runMode`
  - `pcs_manual_on` 後：`schedulePlanSwitch = 0`、`manualModeSwitch = 1`、`runMode = manual`
  - `pcs_manual_off` 後：`schedulePlanSwitch = 1`、`manualModeSwitch = 0`

**結論：PCS 手動模式控制正常、狀態 verify 正常，「開 / 關」已完成可用。**

### 2. 空調開 / 關 — 控制 API 成功，但 verify 失敗 ⚠️

- 控制腳本：`device_control_operator.py`
- action：`ac_on` / `ac_off`
- 控制 API：`POST /client/dynamic/airConditioningControl`
- payload：
  - `ac_on`：`{"command":1,"commandValue":0}`
  - `ac_off`：`{"command":1,"commandValue":1}`
- 控制送出結果：`control_resp: http=200 code=200`（訊息為「操作成功」類）、`success=True`
- 但 verify 目前失敗：verify 走 `GET /client/dynamic/dataOrControl/air`，回 `code=403 msg=No operation permission`。

**結論：**
- 空調控制命令本身可送成功。
- 但目前帳號對 `/client/dynamic/dataOrControl/air` **沒有操作權限**。
- 因此目前只能確認「控制 API 已送成功」，**無法靠現有 verify API 確認空調實際狀態**。
- 空調**不可**視為「完全可驗證」。

### 3. 控制指令總表（script / 參數 / 驗證狀態）

所有控制都可用 **命令列**（`device_control_operator.py`，預設 dry-run，加 `--execute` 才送出）或 **選單**（`device_control_menu.py`）。下表為命令列參數：

| 功能 | 命令列參數（前綴 `python device_control_operator.py`） | endpoint | 驗證狀態 |
|---|---|---|---|
| PCS 手動模式開 | `--action pcs_manual_on --execute` | PUT `editManualSwitch` | ✅ **已驗證成功**（verify 確認 `manualModeSwitch=1`） |
| PCS 手動模式關 | `--action pcs_manual_off --execute` | PUT `editManualSwitch` | ✅ **已驗證成功**（`manualModeSwitch=0`） |
| PCS 充電（啟動充電） | `--action pcs_charge --power N --execute` | POST `sinexcel/…/manualControl` | ⚠️ 已實作、dry-run 通過；**未實測送出**；verify 只讀 403→partial |
| PCS 放電（啟動放電） | `--action pcs_discharge --power N --execute` | POST `sinexcel/…/manualControl` | ⚠️ 同上（`activePowerSetPoint = -N`） |
| PCS 停止充放電（停機） | `--action pcs_stop_power --execute` | POST `sinexcel/…/manualControl` | ⚠️ 已實作、dry-run 通過；**未實測送出** |
| 電池上電 | `--action battery_power_on --execute`（需輸入 `YES`） | POST `manuallyPowerOnAndPowerOff` | ⚠️ 已實作；**高風險、未實測**；輪詢 `getBcuState` 最多 60s |
| 電池下電 | `--action battery_power_off --execute`（需輸入 `YES`） | POST `manuallyPowerOnAndPowerOff` | ⚠️ 同上 |
| 進排風開 | `--action vent_on --execute` | POST `switchingQuantityControl` | ⚠️ 已實作、dry-run 通過；**未實測**；無 verify API（partial） |
| 進排風關 | `--action vent_off --execute` | POST `switchingQuantityControl` | ⚠️ 同上 |
| 冷卻循環開 | `--action cooling_on --execute` | POST `waterPumpControl/1` | ⚠️ 已實作、dry-run 通過；**未實測**；無 verify API（partial） |
| 冷卻循環關 | `--action cooling_off --execute` | POST `waterPumpControl/0` | ⚠️ 同上 |
| 空調開 | `--action ac_on --execute` | POST `airConditioningControl` | ⚠️ 控制曾回 `200`，但 verify（`/dataOrControl/air`）**403**，無法確認實際狀態 |
| 空調關 | `--action ac_off --execute` | POST `airConditioningControl` | ⚠️ 同上 |

**power 規則**：`pcs_charge` / `pcs_discharge` 必填 `--power N`（0~150）；充電 `+N`、放電 `-N`；`pcs_stop_power` 不需 `--power`。

**只讀查詢狀態（`device_control_scraper.py`）**：多數端點可讀；**`空調狀態` 與 `PCS狀態`（authed `/client/dynamic/dataOrControl/air`、`/pcs`）回 `403 No operation permission`**（帳號無操作權限，UI 實際改讀 guest 端點）；登入前呼叫則會是 `401`。

> ✅ = 已實際送出並由 verify 確認；⚠️ = 程式已就緒、dry-run 驗證通過，但**尚未實際 `--execute` 送出**（送出前請確認現場安全）。

---

## 八、Python 控制方式

### A. 命令列版：`device_control_operator.py`

在 `D:\Crawler Sample\test` 下：

```
python device_control_operator.py --action pcs_manual_on --execute
python device_control_operator.py --action pcs_manual_off --execute
python device_control_operator.py --action ac_on --execute
python device_control_operator.py --action ac_off --execute
python device_control_operator.py --action vent_on --execute
python device_control_operator.py --action vent_off --execute
python device_control_operator.py --action cooling_on --execute
python device_control_operator.py --action cooling_off --execute

# 電池上下電（高風險：--execute 後需輸入 YES；或帶 --yes）
python device_control_operator.py --action battery_power_on --execute
python device_control_operator.py --action battery_power_off --execute

# PCS 功率控制（充/放電需 --power 0~150；停止不需）
python device_control_operator.py --action pcs_charge --power 20 --execute
python device_control_operator.py --action pcs_discharge --power 20 --execute
python device_control_operator.py --action pcs_stop_power --execute
```

不加 `--execute` 則為 **dry-run 預覽**（只印不送）：

```
python device_control_operator.py --action pcs_charge --power 20
```

- `pcs_charge`：`activePowerSetPoint = +abs(power)`；`pcs_discharge`：`= -abs(power)`。
- `--power` 超出 0~150 直接拒絕、不送 API；`pcs_charge`/`pcs_discharge` 未帶 `--power` 會報錯。

### B. 選單版：`device_control_menu.py`

在 `D:\Crawler Sample\test` 下：

```
python device_control_menu.py
```

選單內容：

```
1. PCS 手動模式開
2. PCS 手動模式關
3. 空調開
4. 空調關
5. 查詢目前設備狀態
6. PCS 充電          （提示輸入功率 kW 0~150）
7. PCS 放電          （提示輸入功率 kW 0~150）
8. PCS 停止充放電
9. 電池上電          （高風險，需輸入 YES）
10. 電池下電         （高風險，需輸入 YES）
11. 進排風開
12. 進排風關
13. 冷卻循環開
14. 冷卻循環關
0. 離開
```

---

## 九、`operator` 與 `menu` 差異

| | `device_control_operator.py` | `device_control_menu.py` |
|---|---|---|
| 類型 | 指令式控制腳本 | 互動式選單 |
| 啟動方式 | 需帶 `--action`（`--execute` 才送出） | 執行後用數字選項 |
| 適用 | 自動化 / 命令列 / 批次整合 | 人工現場操作 |
| 範例 | `python device_control_operator.py --action pcs_manual_on --execute` | `python device_control_menu.py` |

兩者底層控制邏輯相同（menu 呼叫 operator），差別只在使用方式。

---

## 十、設備控制安全規則（非常重要）

1. `device_control_scraper.py` 是**只讀版**，禁止混入控制邏輯。
2. `device_control_operator.py` 是**控制版**（單一 action、控制端點白名單 + 禁止字樣雙重把關）。
3. 未加 `--execute` 時只能 **dry-run**，不真正送控制。
4. **不重試、不批量**；控制 API 失敗直接輸出失敗結果。
5. **不做離網模式 / 故障復位 / sys 手動上下電 / 其他未列出的控制**（`_FORBIDDEN_EXACT` + exact-match 白名單雙重把關）。
6. **PCS 功率控制**（`pcs_charge`/`pcs_discharge`）`--power` 限 0~150，超出直接拒絕、不送 API；`pcs_charge` 為正、`pcs_discharge` 為負。
7. **電池上下電為高風險**：`--execute` 後需輸入 `YES` 二次確認（或帶 `--yes`）；控制 API 只送一次，對 `getBcuState` 最多輪詢 60 秒（每 4 秒）驗證。
8. `.env` **不可提交版控**；`.env.example` **只放範例格式，不可放真實密文**。
9. verify API 若權限不足 / 欄位不足，**不可**直接視為控制失敗，需區分：
   - `control_success`：控制 API 是否送出成功
   - `verify_success`：後續狀態回讀（`True` / `False` / `partial` / `unknown` / `timeout`）
   - `success`：`control_success=True` 且 verify 為 `partial`/`unknown` 時仍為 `True`，並在 `warnings` 標註
10. 目前 **PCS 手動模式**：控制成功 + verify 成功；**空調 / PCS 狀態 / PCS 功率 / 進排風 / 冷卻循環**：控制可送出，但 verify 只讀來源權限/欄位不足（見第七節與「確認後 API 清單」）。
11. `device_control_scraper.py` 內建 `_assert_readonly()`：命中控制路徑或不在唯讀白名單即 `raise` 擋下；即使 method 是 GET，只要用途是控制（如 `manuallyPowerOn/Off`、`faultRecovery`）也不呼叫。

控制 API 清單（唯讀版**僅記錄、不發送**）：

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

## 十一、共用模組說明

### `api_client.py`
- 建立並重用 `requests.Session()`、共用 `BASE_URL`、統一 timeout / headers 與錯誤處理。
- 主要函式：
  - `get / get_many / unwrap` — 唯讀 GET 與芋道封包 `{code, data, msg}` 解封包（code 0/200 取 `data`）。
  - `login_hmi(username=None, password=None, ...)` — 依 `.env` / config 決定 password（可重放密文），POST `/system/auth/login` 取得 `accessToken`，之後帶 `Authorization: Bearer <token>`。
  - `login(username, password)` — 舊名相容，內部呼叫 `login_hmi`。
  - `load_login_config()` / `mask_secret()` — 讀 `.env` / config；遮罩密文。

### `dashboard_scraper.py`
- 共用解析與格式化：`_flatten_metrics`、`_fmt_metric`、`_zh`、`STATUS_MAP` 與各 summarize helper。

---

## 十二、輸出檔案清單

輸出目錄：`D:\Crawler Sample\output`

| 檔案 | 來源 | 內容 |
|---|---|---|
| `dashboard_min.json` | `dashboard_min.py` | 數據概覽原始資料 |
| `battery_data.json` / `.csv` | `battery_data_scraper.py` | Pack 摘要（14 列） |
| `battery_cells.json` / `.csv` | `battery_data_scraper.py` | Pack cell 明細（280 列） |
| `env_data.json` / `.csv` | `env_data_scraper.py` | 6 卡完整攤平 |
| `env_curated.json` / `.csv` | `env_data_scraper.py` | 環控 curated 精簡摘要 |
| `alarm_records.json` / `.csv` | `alarm_records_scraper.py` | 告警完整記錄 |
| `device_control_readonly.json` | `device_control_scraper.py` | 設備控制**唯讀**狀態 + 控制 API 清單（僅記錄） |
| `device_control_action_result.json` | `device_control_operator.py` | 本次控制 action 的送出內容、回應、verify 結果與 success 狀態 |

補充：

- `device_control_readonly.json` — 來源 `device_control_scraper.py`，用途：只讀抓取設備控制頁目前狀態。
- `device_control_action_result.json` — 來源 `device_control_operator.py`，用途：記錄本次控制 action 的 request / response / verify / success。

---

## 十三、指令範例（懶人包）

```
cd D:\Crawler Sample\test
```

整批抓資料（唯讀）：
```
python run_all.py
```

只跑 device 只讀：
```
python run_all.py --only device
```

PCS 手動模式開 / 關：
```
python device_control_operator.py --action pcs_manual_on --execute
python device_control_operator.py --action pcs_manual_off --execute
```

空調開 / 關：
```
python device_control_operator.py --action ac_on --execute
python device_control_operator.py --action ac_off --execute
```

進排風 / 冷卻循環：
```
python device_control_operator.py --action vent_on --execute
python device_control_operator.py --action vent_off --execute
python device_control_operator.py --action cooling_on --execute
python device_control_operator.py --action cooling_off --execute
```

電池上下電（高風險，需 YES）：
```
python device_control_operator.py --action battery_power_on --execute
python device_control_operator.py --action battery_power_off --execute
```

PCS 功率控制（充/放電需 --power 0~150）：
```
python device_control_operator.py --action pcs_charge --power 20 --execute
python device_control_operator.py --action pcs_discharge --power 20 --execute
python device_control_operator.py --action pcs_stop_power --execute
```

互動式控制選單：
```
python device_control_menu.py
Ctrl + C 退出選單
```

---

## 十四、維護規則 / 注意事項

1. **先盤點前端 JS / API，再寫 scraper / 控制**，不要硬猜 API 路徑或 payload。
2. **原始值與可讀值分開保留**（如 `alarmStatus + alarmStatusText`）。
3. **排序規則與前端一致**：告警記錄用 `alarmTime` DESC。
4. **電池明細一定保留每個 Pack 20 個 cell**（14 Pack → 280 cell）。
5. **設備控制嚴禁誤觸控制命令**：唯讀版只走唯讀白名單；控制版單一 action、預設 dry-run。
6. **登入密文只放 `.env` / config，不寫死在程式碼**；`.env` 不提交版控。
7. 檢查資料時「先寫暫存 `.py` 腳本再執行」，避免 `python -c` 多行字串觸發安全提示。
