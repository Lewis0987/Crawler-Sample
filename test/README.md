# Jietech Dashboard 爬蟲 + 設備控制

抓取 Jietech 內網 Dashboard 各頁籤資料，整理成 **JSON 原始資料**、**CSV 匯出檔**與 **console 可讀摘要**；並提供**設備控制**（低風險 action，指令式 + 選單式）。

涵蓋頁籤：

1. 數據概覽（Dashboard Overview）
2. 電池數據（Battery Data）
3. 環控數據（Environmental Control）
4. 告警記錄（Alarm Records）
5. 設備控制（Device Control：唯讀狀態 + 低風險控制）
6. 閥值管理（Threshold：唯讀，告警抑制 + 閥值列表）

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
| `api_client.py` | 共用 HTTP 層：session、BASE_URL、`get` / `get_many`、`unwrap`、`login_hmi`（SM2 登入 + Bearer token）、`.env` / config 讀取、機密遮罩 | 否 |
| `sm2_util.py` | 零依賴 SM2（配 SM3/KDF）：`normalize_public_key()`、`sm2_encrypt()`（C1C3C2，對齊前端 cipherMode=1） | 否 |
| `dashboard_scraper.py` | 共用解析工具：`_flatten_metrics`、`_fmt_metric`、`_zh`、`STATUS_MAP`、各 summarize helper | 否 |
| `dashboard_min.py` | 數據概覽爬蟲（最小版） | `dashboard_min.json` |
| `battery_data_scraper.py` | 電池數據爬蟲（Pack 摘要 + Pack 明細 20 cell） | `battery_data.json` / `.csv`、`battery_cells.json` / `.csv` |
| `env_data_scraper.py` | 環控數據爬蟲（6 張卡 raw + curated 摘要） | `env_data.json`（raw）、`env_curated.json` / `.csv`（UI） |
| `alarm_records_scraper.py` | 告警記錄爬蟲（分頁抓全部 + 中文欄位/可讀時間/排序） | `alarm_records.json` / `.csv` |
| `device_control_scraper.py` | 設備控制頁**唯讀**狀態（PCS/電池/空調/進排風/冷卻循環，對齊 HMI） | `device_control_readonly.json` |
| `device_control_operator.py` | 設備控制**指令式**工具（單一 action，dry-run / `--execute`） | `device_control_action_result.json` |
| `device_control_menu.py` | 設備控制**互動式總覽/選單**（區塊狀態 + 顏色 + 執行子選單） | （呼叫 operator / scraper） |
| `threshold_config_scraper.py` | 閥值管理**唯讀**（告警抑制 + 125 筆閥值，含 ECM/PCS/BMS 聯動） | `threshold_config.json` / `_summary.json` / `_summary.csv` |
| `run_all.py` | 唯讀總入口，依序執行 6 支 scraper 並彙總 | （呼叫各 scraper） |
| `inspect_sm2_key.py` | 唯讀：分析 `SM2_PUBLIC_KEY` 格式（128/130/ASN.1/密文誤填），遮罩中段 | 否 |
| `test_sm2_login.py` | SM2 登入驗證（S1~S3 離線 + `--with-login` 真實登入；印長度診斷不印完整值） | 否 |
| `test_pcs_modes.py` | PCS 模式解析驗證（離線：控制模式/排程獨立解析＋工作模式） | 否 |
| `test_pcs_control.py` | PCS 3 模式 payload 組裝／方向／範圍驗證（離線，不送控制） | 否 |
| `test_env_discovery.py` | **[diagnostic]** env 動態搜尋/排序驗證（run_all 不依賴） | 否 |
| `inspect_device_status.py` / `inspect_device.py` | **[diagnostic]** dump 設備端點原始結構 / 摘要唯讀結果（run_all 不依賴） | 否 |
| `login_probe.py` | **[deprecated/diagnostic]** 密文重放登入測試（正式流程已改 SM2；run_all 不依賴） | 否 |

---

## 三、登入 / `.env` 狀態

### HMI Login（正式流程摘要）

目前採用與前端一致的 **SM2 動態登入**，所有模組共用 `api_client.login_hmi()`（不各自複製登入邏輯）。

必要設定（`.env` / 任意 `*.env`，例如 `login123.env`）：

```
HMI_USERNAME=hmiUser
HMI_PASSWORD=<明文密碼>
SM2_PUBLIC_KEY=<130 hex 公鑰>
```

`LOGIN_PASSWORD_PAYLOAD` 已**棄用**，預設保持空白（程式已不讀取）。

流程：

```
HMI_PASSWORD（明文）
   ↓  sm2_util.sm2_encrypt()  （SM2 C1C3C2 加密，對齊前端 doEncrypt cipherMode=1）
原始密文 212 hex
   ↓  外層前置單一 "04"
最終 password 214 hex
   ↓  POST /system/auth/login  {username, password, captchaVerification:""}
取得 accessToken → Authorization: Bearer <token>
```

注意：
- `SM2_PUBLIC_KEY` 是 **130 hex 公鑰**（04 開頭）。
- Network Payload 中的 **214 hex password 是「密文」不是公鑰** — 不要貼進 `SM2_PUBLIC_KEY`（`normalize_public_key()` 會擋下並報錯）。
- 密碼更新後**不需**重新抓固定 payload（每次即時加密）。
- 登入 log 固定 4 行、不含機密：`嘗試登入（hmiUser）…` / `[ENV] loaded: <path>` / `HMI login: user=hmiUser` / `HMI login success`。

以下為細節說明。

設備控制頁（`run_all.py` 的 device、`device_control_scraper.py`、`device_control_operator.py`）需登入取得 `accessToken`。

前端登入時 password 會被 **SM2 加密**（`sm-crypto doEncrypt cipherMode=1`，04 前綴 C1C3C2 hex，每次密文不同）。本專案登入採 **SM2 即時加密單一模式**（由 `api_client.py` 統一處理）：

- env 放密碼原文 `HMI_PASSWORD` + 公鑰 `SM2_PUBLIC_KEY`，登入時用 `sm2_util.py` 即時加密（每次動態產生，不需固定密文）。
- 對齊前端 `jsencrypt` 的 `Pr(c) = "04" + sm2.doEncrypt(c, publicKeyHex, 1)`（cipherMode=1 → C1C3C2，再補 `04` 前綴，最終約 214 hex）。
- `api_client.encrypt_password_sm2()` → `sm2_util.sm2_encrypt()` 產生與前端一致的密文；`normalize_public_key()` 會自動處理 128/130(04 前綴)/ASN.1-DER 三種公鑰輸入。

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

### env 檔內容格式（`test/login123.env` 或任意 `*.env`）

env 檔（不論檔名）填以下欄位（**README/範本只放格式，不放真實密文**）：

```
HMI_USERNAME=hmiUser
# --- SM2 即時加密（唯一模式）---
HMI_PASSWORD=<HMI 登入密碼原文>
SM2_PUBLIC_KEY=<前端 sm-crypto 用的公鑰 hex，128 或 130（04 開頭），亦支援 ASN.1-DER>
```

欄位說明：
- `HMI_USERNAME`（可選）：預設 `hmiUser`。
- `HMI_PASSWORD` + `SM2_PUBLIC_KEY`：登入時走 **SM2 即時加密**（兩者皆必填）。

**password 取值優先順序（高 → 低）**：
1. `HMI_PASSWORD` + `SM2_PUBLIC_KEY` → `encrypt_password_sm2()` / `sm2_util.sm2_encrypt()` 即時 SM2 加密（`password_source = sm2`）
2. `client.login_hmi(password=...)` 傳入值（相容 / 測試用，`password_source = arg`）

> 正常情況只需填 `HMI_PASSWORD` 與 `SM2_PUBLIC_KEY` 即可（走 SM2）。

### 目前已確認（實測）

- **SM2 即時加密登入成功**：`password_source = sm2`、`HMI login success`（`code=200`）、每次密文不同（len 214、04 前綴）皆通過。
- `accessToken` 自動帶入 `Authorization: Bearer <token>`。
- `run_all.py`（device）、`device_control_scraper.py`、`device_control_operator.py`、`threshold_config_scraper.py`、`device_control_menu.py` 皆走**同一套** `api_client.login_hmi()` 登入，讀 `test/login123.env`（或任意 `*.env`）。

### 實際執行 / 驗證範例

```
cd D:\Crawler Sample\test

# 登入驗證（S1~S3 離線；--with-login 走真實 SM2 登入）
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
- **機密遮罩政策**：Console / Debug log / JSON 輸出**一律不輸出**完整機密——`HMI_PASSWORD`、`accessToken`、`refreshToken`、`Authorization: Bearer`、完整 SM2 密文（`04...`）、`SM2_PRIVATE_KEY`、Cookie / Session ID 皆不落地。需除錯時只以**遮罩**顯示：`前 5 + … + 後 5 (len=N)`（例：`accessToken = fd98e...80031 (len=32)`）。`token_info` 中的完整 token 僅存在記憶體、供帶 Header 用。

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
- `battery_data.json`：保留機器欄位 `packNo, totalVoltage, maxCellVoltage, maxCellNo, minCellVoltage, minCellNo, maxTemp, maxTempCellNo, minTemp, minTempCellNo, socMax, socMin, sohMax, sohMin`。
- `battery_data.csv`：對應 UI「極值資訊」，改中文欄位（單位置於表頭、數值保持數字）：
  `Pack, 總電壓(V), 最高單體電壓(V), 最高電壓電芯編號, 最低單體電壓(V), 最低電壓電芯編號, 最高溫度(℃), 最高溫度電芯編號, 最低溫度(℃), 最低溫度電芯編號, 最高SOC(%), 最低SOC(%), 最高SOH(%), 最低SOH(%)`。

> 註：API 沒有單一 Pack 級 SOC/SOH，故以實測的最高/最低（`socMax/socMin`、`sohMax/sohMin`）呈現。

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

輸出：

**A. 原始完整資料（僅 JSON）**：`env_data.json`（保留全部 6 卡 raw API 欄位）
**B. curated 精簡摘要（UI 欄位）**：`env_curated.json` / `env_curated.csv`

> CSV 只輸出 curated/UI 欄位（`env_curated.csv`）；**不再輸出 raw 全欄位 CSV**（原 `env_data.csv` 已移除），避免與 curated 兩份大量重複。raw 完整資料仍在 `env_data.json`。

翻譯規則：欄位中文化；狀態值透過 `dashboard_scraper.STATUS_MAP` 翻譯；數值自動帶單位。

> 串口 ttyS0 的 14 個數位點（紅燈/黃燈/綠燈/水泵運轉/液位高/液位低/緊急按鈕/門禁/火警/消防故障/突波保護SPD/空調故障/排風/蜂鳴器）**刻意保留原始 `0/1`**：HMI 本身即以 0/1 呈現，CSV 與 UI 一致，不再轉中文（非遺漏）。

```
cd D:\Crawler Sample\test
python env_data_scraper.py
```

---

### 4) `alarm_records_scraper.py` — 告警記錄

分頁抓完整告警（`pageSize=100` 迴圈到抓滿 `total`），輸出可讀欄位。

1. **排序**：依 `alarmTime` 由新到舊（DESC）。
2. **告警狀態中文化**：`alarmStatus=false → 已恢復`、`true → 告警中`。
3. **告警級別**：`0 → 嚴重`、`1 → 一般`。
4. **時間**：CSV 只輸出 UI 顯示格式 `YYYY-MM-DD HH:MM:SS`（epoch 毫秒等原始欄位只在 JSON）。

- `alarm_records.csv`：只輸出 UI 告警表格 5 欄（`告警物件, 告警內容, 告警級別, 告警狀態, 告警時間`）。
- `alarm_records.json`：保留完整原始欄位（`alarmTime/createTime/level/alarmStatus/typeMark/targetMark/val…` 及各 `*Text`）供除錯。

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
- **PCS 五個固定狀態欄位（皆唯讀、永遠顯示，不因控制功能增減而移除）**：`PCS當前狀態 / PCS控制模式 / PCS排程開關狀態 / PCS工作模式 / PCS功率控制模式`，各自獨立、互不推導。
  - **PCS當前狀態**（待機 / 充電 / 放電 / 啟動中 / 停止中 / 故障 / 未知）：**直接對照 HMI/API 的狀態欄位值**（`parse_pcs_current_status`），對齊 HMI「當前狀態」。來源 guest PCS `system*Status` 欄位的**實際值**（值本身即描述字，非 true/false）：`systemChargingStatus=charging→充電`、`systemDischargingStatus=discharging→放電`、`systemStandbyStatus=standby→待機`、`systemBootingStatus=booting→啟動中`、`systemFaultStatus`/`systemFailedStatus` 非 normal→故障、`systemOnOrOffStatus=stopping→停止中`、`systemOnOrOffStatus=stop/running（且無充放/故障/啟動）→待機`（對照 HMI：停機/在網待命皆顯示待機）。優先序 故障>充電>放電>啟動中>停止中>待機；皆無→未知。**不由 控制模式/功率/電流/電壓/排程/手動 推論**（例如「工作模式=併網、功率控制模式=交流有功」不代表正在充電）。若 API 無此欄位 → **未知**。
  - **PCS控制模式**（智慧模式 / 手動模式 / 未知）：**唯一來源＝`getRunMode`**（`auto/schedule/smart/intelligent`→智慧；`manual`→手動）。**不可用排程開關反推模式**。
  - **PCS排程開關狀態**（開 / 關）：來源 `getScheduleSwitch.schedulePlanSwitch`（1/0）；即使 `=0`，控制模式仍可能是智慧模式。
  - **PCS工作模式**（併網 / 離網 / 未知）：**唯讀狀態顯示**（顯示 label＝「PCS工作模式」；底層機器語意仍為 `grid_mode`，解析 `parse_pcs_grid_mode`），來源 guest PCS `systemGridTiedStatus`（gridTied→併網）/ `systemOffGridStatus`（true→離網）。**永遠顯示**、不因值為「併網」而隱藏；與已移除的離網「控制」無關。（僅顯示文字改名，API 欄位/變數/parse 邏輯不變。）
  - **PCS功率控制模式**（交流有功 / 直流恆流 / 直流恆功率 / **離網交流電壓** / 未知）：共用 `parse_pcs_power_control_mode(raw)`，**依 API 當下實際欄位判斷、不套任何預設值**。**離網時**（`systemOffGridStatus=true` / `systemGridTiedStatus=offGrid`）→「離網交流電壓」（離網為交流電壓源，不顯示併網的直流恆流/恆功率）。**併網時**：`energyDispatchingMode`（ac→交流有功；dc 再看 `dcControlMode`：current→直流恆流、power→直流恆功率）；查不到→「未知」。（僅補離網「狀態顯示」，**不含任何離網控制**。）
  - JSON 另存機器語意 `status_summary.PCS._pcs_modes = {control_mode, schedule_enabled, grid_mode, power_control_mode}`。
  - 儀表板 PCS 顯示 **當前狀態 / 控制模式 / 排程開關狀態 / 工作模式 / 功率控制模式**（5 個固定唯讀欄位，永遠顯示）。
  - 驗證：`python test_pcs_modes.py`（模式解析）、`python test_pcs_control.py`（3 模式 payload/驗證），全 PASS。

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

**輸出**：`output/threshold_config.json`（原始 config+list）、`output/threshold_config_summary.json`、`output/threshold_config_summary.csv`（皆保留完整 125 筆）。
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
| threshold | OK |

```
Total: 6, OK: 6, FAIL: 0, SKIP: 0
```

代表 `dashboard_min.py` / `battery_data_scraper.py` / `env_data_scraper.py` / `alarm_records_scraper.py` / `device_control_scraper.py` / `threshold_config_scraper.py` 都可正常輸出到 `D:\Crawler Sample\output`。

`run_all.py` 檢查產物存在時，一律看 `D:\Crawler Sample\output\` 內的檔案。也可只跑單一頁籤：

```
python run_all.py --only dashboard
python run_all.py --only battery
python run_all.py --only env
python run_all.py --only alarm
python run_all.py --only device
python run_all.py --only threshold
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
  - PCS 功率控制（3 種模式 × 充/放電，皆需 `--power N`）：
    - 交流有功（**已確認**）：`pcs_charge` / `pcs_discharge`（單位 kW）
    - 直流恆流（**[READY]，DevTools 已確認**）：`pcs_dc_current_charge` / `pcs_dc_current_discharge`（單位 A）
    - 直流恆功率（**[READY]，DevTools 已確認**）：`pcs_dc_power_charge` / `pcs_dc_power_discharge`（單位 kW）
    - 停止充放電：`pcs_stop_power`
- 參數：`--execute`（真正送出）、`--power N`（0~150，充放電必填；直流恆流單位為 A）、`--yes`（高風險略過互動確認）。
- ✅ **三種模式（交流有功 / 直流恆流 / 直流恆功率）**：payload、enum、方向皆經 DevTools 確認 → **可 `--execute` 實送**（仍受電池已上電前置檢查與完整大寫 YES 二次確認）。三者方向一致：**充=−N、放=+N**。
- 控制後回讀狀態做 verify；**不重試、不批量**；不含故障復位 / sys 手動上下電等未列出控制。
- 產出：`D:\Crawler Sample\output\device_control_action_result.json`
  （含 `logged_in / action / dry_run / power / control_request / control_response / verify / control_success / verify_success / success / warnings`）

**PCS manualControl enum 對照（DevTools 實測）** — 共同欄位：`param=1, gridInterconnectionMode=0, offGridAcVoltRegulation=null`

| 模式 | energyDispatchingMode | activePowerControlMode | dcControlMode | 設定值欄位 | 方向 | 狀態 |
|---|---:|---:|---:|---|---|---|
| 交流有功 | 0 | 0 | null | `activePowerSetPoint` | 充=−N、放=+N | ✅ READY |
| 直流恆流 | 1 | null | 0 | `dcCurrentSetPoint` | 充=−N、放=+N | ✅ READY |
| 直流恆功率 | 1 | null | 1 | `dcPowerSetPoint` | 充=−N、放=+N | ✅ READY |

### 3) `device_control_menu.py`
- 互動式**總覽**：依區塊（PCS / 電池 / 進排風 / 空調 / 冷卻循環）顯示**當前開關狀態**（開/上電→藍、關/下電→紅）＋ 各區塊控制選單參考。總覽為顯示用、**不自動執行**。
- 輸入 `e` 進入**控制執行子選單**（沿用數字選單，含電池 YES 二次確認、PCS 功率輸入）；`r` 重新整理、`0` 離開。
- 底層仍是 `subprocess` 呼叫 `device_control_operator.py`（控制）與 `device_control_scraper.py`（狀態）。

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
| PCS 交流有功 充電 | `--action pcs_charge --power N --execute` | POST `sinexcel/…/manualControl` | ✅ **[READY] 已開放實送**（confirmed=True）；`activePowerSetPoint = -N`（本站：負=充電）；**前置檢查：電池須「已上電」**；實機送出由使用者手動 `YES` 觸發 |
| PCS 交流有功 放電 | `--action pcs_discharge --power N --execute` | POST `sinexcel/…/manualControl` | ✅ 同上（`activePowerSetPoint = +N`，本站：正=放電）；**前置檢查：電池須「已上電」** |
| PCS 直流恆流 充/放電 | `--action pcs_dc_current_charge\|pcs_dc_current_discharge --power N`（單位 A） | POST `sinexcel/…/manualControl` | ✅ **[READY] 已開放實送**（DevTools 確認）；`energyDispatchingMode=1, dcControlMode=0`；充=`dcCurrentSetPoint -N`、放=`+N` |
| PCS 直流恆功率 充/放電 | `--action pcs_dc_power_charge\|pcs_dc_power_discharge --power N`（單位 kW） | POST `sinexcel/…/manualControl` | ✅ **[READY] 已開放實送**（DevTools 確認）；`energyDispatchingMode=1, dcControlMode=1`；充=`dcPowerSetPoint -N`、放=`+N` |
| PCS 停止充放電（停機） | `--action pcs_stop_power --execute` | POST `sinexcel/…/manualControl` | ✅ **[READY] 已開放實送**；**免電池前置檢查** |
| 電池上電 | `--action battery_power_on --execute`（需輸入 `YES`） | POST `manuallyPowerOnAndPowerOff` | ⚠️ 已實作；**高風險、未實測**；輪詢接觸器反饋最多 60s |
| 電池下電 | `--action battery_power_off --execute`（需輸入 `YES`） | POST `manuallyPowerOnAndPowerOff` | ⚠️ 同上；**前置檢查：電池須「待機」**（充/放電中會擋下） |
| 進排風開 | `--action vent_on --execute` | POST `switchingQuantityControl` | ⚠️ 已實作、dry-run 通過；**未實測**；無 verify API（partial） |
| 進排風關 | `--action vent_off --execute` | POST `switchingQuantityControl` | ⚠️ 同上 |
| 冷卻循環開 | `--action cooling_on --execute` | POST `waterPumpControl/1` | ⚠️ 已實作、dry-run 通過；**未實測**；無 verify API（partial） |
| 冷卻循環關 | `--action cooling_off --execute` | POST `waterPumpControl/0` | ⚠️ 同上 |
| 空調開 | `--action ac_on --execute` | POST `airConditioningControl` | ⚠️ 控制曾回 `200`，但 verify（`/dataOrControl/air`）**403**，無法確認實際狀態 |
| 空調關 | `--action ac_off --execute` | POST `airConditioningControl` | ⚠️ 同上 |

**power 規則**：`pcs_charge` / `pcs_discharge` 必填 `--power N`（0~150）；本站方向 **充電 `activePowerSetPoint = -N`、放電 `= +N`**（action 名稱＝實際行為）；`pcs_stop_power` 不需 `--power`。

**電池狀態前置檢查（operator 內強制，CLI / 選單皆生效）**：
- `pcs_charge` / `pcs_discharge`：電池須為**已上電**（依接觸器反饋判定），否則 `blocked`、不送 API。
- `battery_power_off`：電池須為**待機**（依功率方向判定），充電中 / 放電中 / 狀態未知皆 `blocked`。
- `pcs_stop_power`：**免前置檢查**。
- 被擋下時 result JSON 帶 `precheck` / `blocked`，且 `control_success = success = false`、不送出、不等待。

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

# PCS 功率控制（充/放電需 --power；停止不需）
# 交流有功（已確認，可 --execute）：
python device_control_operator.py --action pcs_charge --power 20 --execute
python device_control_operator.py --action pcs_discharge --power 20 --execute
# 直流恆流（[READY]，可實送；充=負 A、放=正 A）：
python device_control_operator.py --action pcs_dc_current_charge --power 4 --execute
python device_control_operator.py --action pcs_dc_current_discharge --power 5 --execute
# 直流恆功率（[READY]，可實送；充=負 kW、放=正 kW）：
python device_control_operator.py --action pcs_dc_power_charge --power 5 --execute
python device_control_operator.py --action pcs_dc_power_discharge --power 6 --execute
# 停止充放電：
python device_control_operator.py --action pcs_stop_power --execute
```

不加 `--execute` 則為 **dry-run 預覽**（只印不送）：

```
python device_control_operator.py --action pcs_charge --power 20
```

- **交流有功**：`pcs_charge`→`activePowerSetPoint = -abs(power)`（充電）；`pcs_discharge`→`= +abs(power)`（放電）。
- **直流恆流**（DevTools 確認）：`energyDispatchingMode=1, dcControlMode=0`；充=`dcCurrentSetPoint -N`、放=`+N`（A）。可 `--execute`。
- **直流恆功率**（DevTools 確認）：`energyDispatchingMode=1, dcControlMode=1`；充=`dcPowerSetPoint -N`、放=`+N`（kW）。可 `--execute`。
- `--power` 超出 0~150 直接拒絕、不送 API；所有充放電未帶 `--power` 會報錯。
- 所有充放電前需電池**已上電**、`battery_power_off` 前需電池**待機**，否則直接 `blocked`、不送出。

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
6. PCS 交流有功控制（充/放電）   [READY]  → 第二層：1.充電 2.放電 0.返回 → 輸入 kW（0~150）
7. PCS 直流恆流控制（充/放電）   [READY]  → 第二層：1.充電 2.放電 0.返回 → 輸入 A（0~150）
8. PCS 直流恆功率控制（充/放電） [READY]  → 第二層：1.充電 2.放電 0.返回 → 輸入 kW（0~150）
9. PCS 停止充放電                [READY]
10. 電池上電          （高風險，需輸入 YES）
11. 電池下電         （高風險，需輸入 YES）
12. 進排風開
13. 進排風關
14. 冷卻循環開
15. 冷卻循環關
0. 離開
```

**PCS 功率控制（二層選單）**：選 6/7/8 進入模式 → 選充/放電 → 輸入參數（依單位/範圍驗證）→ 顯示「即將執行（控制類型/方向/設定值）」→ 輸入完整大寫 `YES`。依 `confirmed` 分流：
- **交流有功（`[READY]`，已確認）**：顯示「【正式控制模式】此操作將實際送出 PCS 控制命令」，`YES` 後**呼叫 operator `--execute` 實際送出並 verify**。
- **直流恆流（`[READY]`，DevTools 已確認）**：同交流有功，`YES` 後 `--execute` 實送並 verify。
- **直流恆功率（`[READY]`，DevTools 已確認）**：同上，`YES` 後 `--execute` 實送並 verify（充=−N、放=+N）。
- **PCS 停機（`[READY]`）**：可實際送出（安全停止）。

選單附加行為：
- **狀態儀表板**：進選單 / 查詢時彩色顯示各區塊狀態；PCS 顯示 5 個固定唯讀欄位 **當前狀態 / 控制模式 / 排程開關狀態 / 工作模式 / 功率控制模式**（當前狀態=待機/充電/放電/啟動中/停止中/故障/未知，讀 API 實際旗標；工作模式=併網/離網；功率控制模式=交流有功/直流恆流/直流恆功率/離網交流電壓）、電池顯示**電池上下電狀態**（接觸器反饋）。
- **前置檢查**：與 operator 相同（PCS 充/放電需電池已上電、電池下電需待機、停止充放電免檢查）；被擋下時顯示 `BLOCKED` 與原因、不送出。

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
5. **不做故障復位 / sys 手動上下電 / 其他未列出的控制**（`_FORBIDDEN_EXACT` + exact-match 白名單雙重把關）。
6. **PCS 功率控制**分 3 模式 × 充/放電，`--power` 限 0~150；**三種模式（交流有功／直流恆流／直流恆功率）皆經 DevTools 確認、可 `--execute` 實送**，方向一致（充=−N、放=+N）；送出前電池須**已上電**（未上電 → `blocked`）。
7. **電池上下電為高風險**：`--execute` 後需輸入 `YES` 二次確認（或帶 `--yes`）；`battery_power_off` 送出前電池須**待機**（充/放電中 → `blocked`）；控制 API 只送一次，對接觸器反饋最多輪詢 60 秒驗證。
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
  - `login_hmi(username=None, password=None, ...)` — 以 `HMI_PASSWORD` + `SM2_PUBLIC_KEY` 即時 SM2 加密（`encrypt_password_sm2`），POST `/system/auth/login` 取得 `accessToken`，之後帶 `Authorization: Bearer <token>`；debug 內 token 一律遮罩。
  - `encrypt_password_sm2(password, public_key_hex)` — 正規化公鑰後呼叫 `sm2_util.sm2_encrypt`，輸出 `"04"+C1C3C2` hex（對齊前端）。
  - `login(username, password)` — 舊名相容，內部呼叫 `login_hmi`。
  - `load_login_config()` / `mask_secret()` — 讀 `.env` / config；`mask_secret` 產生 `前5...後5 (len=N)` 遮罩。

### `dashboard_scraper.py`
- 共用解析與格式化：`_flatten_metrics`、`_fmt_metric`、`_zh`、`STATUS_MAP` 與各 summarize helper。

---

## 十二、輸出檔案清單

輸出目錄：`D:\Crawler Sample\output`

| 檔案 | 來源 | 內容 |
|---|---|---|
| `dashboard_min.json` | `dashboard_min.py` | 數據概覽原始資料 |
| `dashboard_data.json` / `.csv` | `dashboard_scraper.py`（連續輪詢，非 run_all） | JSON=概覽全區塊原始快照；CSV=概覽卡片欄位時間序列（40 欄） |
| `battery_data.json` / `.csv` | `battery_data_scraper.py` | JSON=Pack 機器欄位；CSV=極值 UI 中文欄位（14 列） |
| `battery_cells.json` / `.csv` | `battery_data_scraper.py` | Pack cell 明細（280 列，7 欄） |
| `env_data.json` | `env_data_scraper.py` | 6 卡完整 raw（**僅 JSON**，無 CSV） |
| `env_curated.json` / `.csv` | `env_data_scraper.py` | 環控 curated / UI 精簡摘要 |
| `alarm_records.json` / `.csv` | `alarm_records_scraper.py` | JSON=完整原始；CSV=UI 告警表格 5 欄 |
| `device_control_readonly.json` | `device_control_scraper.py` | 設備控制**唯讀**狀態（status_summary + 原始資料 + verify 403） |
| `device_control_action_result.json` | `device_control_operator.py` | 本次控制 action 的送出內容、回應、verify 結果與 success 狀態 |
| `threshold_config.json` | `threshold_config_scraper.py` | 閥值管理原始（config + 完整 125 筆 list） |
| `threshold_config_summary.json` | `threshold_config_scraper.py` | 閥值管理解析摘要（告警抑制 + 125 筆可讀列表） |
| `threshold_config_summary.csv` | `threshold_config_scraper.py` | 閥值管理表格（完整 125 筆） |

補充：

- `device_control_readonly.json` — 來源 `device_control_scraper.py`，用途：只讀抓取設備控制頁目前狀態。
- `device_control_action_result.json` — 來源 `device_control_operator.py`，用途：記錄本次控制 action 的 request / response / verify / success。
- `threshold_config*.json` / `.csv` — 來源 `threshold_config_scraper.py`；JSON/CSV 保留完整 125 筆，終端只印前 10 筆。

---

## 十三、指令範例（懶人包）

```
cd D:\Crawler Sample\test
```

整批抓資料（唯讀）：
```
python run_all.py
```

只跑 device / threshold 只讀：
```
python run_all.py --only device
python run_all.py --only threshold
```

閥值管理（唯讀，終端印前 10 筆、JSON/CSV 完整 125 筆）：
```
python threshold_config_scraper.py
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
# 交流有功、直流恆流（DevTools 已確認，可實送）
python device_control_operator.py --action pcs_charge --power 20 --execute
python device_control_operator.py --action pcs_discharge --power 20 --execute
python device_control_operator.py --action pcs_dc_current_charge --power 4 --execute
python device_control_operator.py --action pcs_dc_current_discharge --power 5 --execute
# 直流恆功率（[READY]，可實送；充=負 kW、放=正 kW）
python device_control_operator.py --action pcs_dc_power_charge --power 5 --execute
python device_control_operator.py --action pcs_dc_power_discharge --power 6 --execute
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
