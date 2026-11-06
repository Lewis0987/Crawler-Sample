# 測試與腳本入口分類

> 本文件是 [`README.md`](README.md) §5 / §14 的細節延伸：
> **哪些入口是純離線、哪些需要實體 HMI、哪些根本是實機 scraper。**

**看名字最容易猜錯的那一個：`run_all.py` 不是 Regression Runner，它是實機唯讀 scraper 套件。**
執行它會連線 HMI 並覆寫 `output\` 下 15 個快照檔。要跑 Regression 請用
`run_offline_regression.py`。

---

## 一、純離線 Regression（預設入口）

```bash
python run_offline_regression.py              # 全部純離線套件
python run_offline_regression.py --only cert  # 只跑認證相關
python run_offline_regression.py -k menu      # 只跑名稱含 menu 的套件
python run_offline_regression.py --list       # 只列出分類，不執行
python run_offline_regression.py --json out.json
```

| 離場代碼 | 意義 |
|---|---|
| `0` | 全部通過 |
| `1` | 有「非既有」的失敗 → 要處理 |
| `2` | 只有既有失敗（如實列示，**不視為通過**） |

Runner 內建 `PYTHONIOENCODING=utf-8`，不需要另外設定（README §14 提到的 cp950 問題）。

### 兩道防線（不只靠排除清單）

| | 防什麼 | 做法 |
|---|---|---|
| 第 1 道 | **已知**的實機入口 | `LIVE_SUITES` 排除清單，預設不執行 |
| 第 2 道 | **意外**的對外連線 | `_offline_guard/sitecustomize.py` 經 `PYTHONPATH` 注入，攔截 `socket.connect` / `connect_ex` |

排除清單只能擋掉你已經知道的；網路阻擋才擋得住沒料到的（新測試、相依套件、子行程）。
兩者互補，單獨任何一道都不夠。

阻擋器經 `PYTHONPATH` 注入，所以**受測行程與它啟動的每一個 python 子行程**都會套用，
不需要修改任何受測程式。每次阻擋都會記錄 pid 與目標位址，Runner 結束時一併列出。

**它刻意把 `socket` 移出 `sys.modules`。** 因為 `test_phase6_safety_gate`、
`test_phase6_last_control_store`、`test_phase6_control_authority`、
`test_phase6_field_measure`、`test_phase6_pcs_control_executor`、
`test_phase6_pcs_control_integration` 會快照 `set(sys.modules)` 來證明「這個模組不具備
網路能力」；若阻擋器把 `socket` 留在 `sys.modules`，那些斷言會變成假 FAIL。移出之後改用
meta_path finder 交出同一份已修補的模組 —— 既擋得住連線，也**不放寬任何既有斷言**。

### 已知限制

- 非 python 的子行程（`chrome.exe`、`git`、bash 測試腳本）不受阻擋器影響 →
  這正是排除清單仍然必要的原因。
- 本機 socket（`127.0.0.1`）同樣會被擋下。目前沒有測試需要本機伺服器；
  若未來有，要另行處理。
- `PYTHONPATH` 注入會蓋掉環境中其他的 `sitecustomize`。

---

## 二、需要實體 HMI 的測試（預設不執行）

| 套件 | 原因 |
|---|---|
| `test_cert_p2_live` | 需開啟實際 HMI 頁面擷圖（`cert_screenshot.cdp_capture` 會啟動 Chrome） |
| `test_sm2_login` | 實測會連線 `192.168.128.110:8080`（HMI SM2 登入） |

這兩個是用 socket 阻擋器**實測**出來的，不是用猜的。

> **為什麼光靠排除清單不夠：**`test_phase6_first_live_field_provider` 不在這張表上，
> 但實測它會嘗試連 `192.168.128.110:8080`（另一次執行則是 `127.0.0.1:9`）。
> 阻擋器攔下之後它仍然 294/294 全過 —— 代表那是可容忍的探測，不需要排除整個套件，
> 但**沒有第 2 道防線就會變成一次真實的實機存取**。這正是兩道防線都要留的理由。

要執行它們必須同時明確指定兩個旗標，避免手滑：

```bash
python run_offline_regression.py --include-live --no-guard
```

---

## 三、實機 scraper／腳本（Runner 永不執行）

| 腳本 | 實際行為 |
|---|---|
| `run_all.py` | **實機唯讀 scraper 套件**（dashboard / battery / env / alarm / device / threshold）。會連 HMI，並覆寫 `output\` 下 15 個快照檔：`dashboard_data.*`、`battery_data.*`、`battery_cells.*`、`env_data.json`、`env_curated.*`、`alarm_records.*`、`device_control_readonly.json`、`threshold_config*.*`。不送控制（allowlist / blocklist 擋下）。 |
| `run_phase3_regression.py` | Phase 2~5 Regression 總表；非純離線入口 |
| `run_scheduled_first_live.py` | ONE-SHOT 排程實機 launcher |

> 2026-11-05 曾因名稱誤導把 `run_all.py` 當成 Regression Runner 執行，覆寫了上述 15 個
> 快照檔（唯讀、未送控制、未影響 `device_control_action_result.json` 與
> `output\cert_reports`）。本文件與 `run_offline_regression.py` 就是為了讓這個錯不再發生。

---

## 四、既有失敗（PRE-EXISTING / OUT-OF-SCOPE）

| 套件 | 失敗數 | 原因 |
|---|---|---|
| `test_pcs_modes` | 5 | fixture 仍停在舊的控制模式契約。commit `0ffc726`（2026-07-28）把資料來源改為 `schedulePlanSwitch` + `manualModeSwitch` 並新增「未開啟模式」，fixture 未同步。需實機核對後才決定修正方向。 |

Runner 會把這些**如實列印在獨立區塊**並計入失敗數，只是用離場代碼 `2` 與「非既有失敗」
區分開來。**不會隱藏、不會跳過、不會放寬任何斷言。**

---

## 五、慣例

- 新增測試一律命名 `test_*.py`，Runner 會自動納入。
- 測試**不得寫入正式 `output\`**。需要寫檔請用 `tempfile` 或注入測試專用路徑
  （例如 `test_schedule_switch.py` 對 `device_control_operator.RESULT_PATH` 的做法）。
- 新測試若需要連線，請加進 `run_offline_regression.py` 的 `LIVE_SUITES` 並寫明原因；
  否則阻擋器會讓它失敗 —— 這是刻意的。
