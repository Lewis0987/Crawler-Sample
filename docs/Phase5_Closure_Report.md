# Phase 5 收尾報告 —— Production Ready

**期間**：2026-08-17 ~ 2026-08-19
**基線**：Phase 4 COMPLETE（`eb3a702`），Full Regression 1529/1529 PASS
**結果**：Phase 5.1 ~ 5.5 全部完成；Full Regression **1802/1802 PASS**

> 本報告是 Phase 5 的**結果彙整**。
> 驗收基準與逐項標準見 [`Phase5_Validation_Plan.md`](Phase5_Validation_Plan.md)；
> 部署與維運操作見 [`Operations_Guide.md`](Operations_Guide.md)；
> Phase 4 的架構與機制見 [`Phase4_Closure_Report.md`](Phase4_Closure_Report.md)。

---

## Executive Summary

Phase 5 的目的不是重做 Phase 4 的功能，而是確認目前版本能否進入正式使用。

| 階段 | 結果 |
|---|---|
| **5.1** Production Validation Plan | COMPLETE —— 產出可執行的驗收基準 |
| **5.2** Long-running Stability / Soak | **Original Soak = FAIL** → Root Cause = ONE-TIME COST → Fix → Deployment PASS → **Fix Validation PASS** |
| **5.3** Stress / Boundary Test | COMPLETE —— #1～#12 全部 PASS，244/244 |
| **5.4** Production Documentation | COMPLETE —— 文件 DoD 8/8 PASS |
| **5.5** Release Validation | COMPLETE —— 驗證性 DoD 全數 PASS |

**最重要的成果**：Phase 5.2 的 24 小時 Soak 在 T0+3.09h 揭露了一個**真實的資源缺陷**，
經完整的「發現 → 根因 → 修法 → 部署 → 修正後驗證」流程收斂。
若沒有這次 Soak，該問題會在正式環境長期存在而不被發現。

**Phase 5 期間未修改任何產品 Python 執行碼。** 唯一的產品面變更是
`tools/install_service_nssm.ps1`（Service 部署設定與 `-Action update` 契約）。

---

## 1. Phase 5.1 — Production Validation Plan

產出 [`Phase5_Validation_Plan.md`](Phase5_Validation_Plan.md)，定義 5.2～5.5 的
可判定驗收基準（含 Soak 的 A–F 六組 PASS/WARN/FAIL 標準、Stress 的 12 項矩陣、
Documentation 缺口清單、Release DoD）。同時簡化 `test/README.md` 為 12 節。

**5.1 = COMPLETE**（本文件即交付物）

---

## 2. Phase 5.2 — Long-running Stability / Soak Test

### 2.1 Original Soak = **FAIL**（歷史定案，不改寫）

```
T0        : 2026-08-17 14:57:25
FAIL 觸發 : 2026-08-17 18:02:30（T0+3.09h，第 186 筆採樣）
判定      : B. Resource stability —— WorkingSet 24.293 MB → 78.34 MB（+222%，>50% 即 FAIL）
```

採樣器依設計保存證據後自行停止。中斷前其餘五組：**A / C / D / F 均 PASS**；
**E** 在第一份 Session 自動建立並自然收尾後由 WARN 轉 PASS。

> ⚠️ **此 FAIL 為定案。** 後續查明 Root Cause 並完成修法，**不得將原始 Soak 改寫為 PASS**。
> Phase 5.2 標記為 COMPLETE 的意義是「發現問題 → 根因查明 → 修正 → 部署 → 修正後驗證 PASS → 收尾完成」。

### 2.2 Root Cause = **ONE-TIME COST**

```
finalize → _regenerate_outputs() → _write_xlsx() → import openpyxl.chart
        → openpyxl.compat.numbers → import numpy → scipy-openblas
        → OpenBLAS native thread pool（依 os.cpu_count() 建立）
```

- 產品碼**完全不做 BLAS 運算**；openpyxl 只拿 numpy 做 `isinstance` 型別判斷
- 離線隔離實測：`import numpy` **單獨一行**即產生 **+19 threads / +652 MB Private**
- `gc.collect()` 回收 **0 MB** —— 非 Python heap，GC 觸及不到，故不採此修法

**判定為一次性而非累積性**的依據（第二次自然 finalize 實測）：

| | ΔPrivate | ΔThreads | ΔHandles |
|---|---|---|---|
| finalize #1 | +672.49 MB | +21 | +21 |
| finalize #2 | −0.31 MB | −1 | −3 |
| stable baseline #1 → #2 | −0.21 MB | ±0 | ±0 |

### 2.3 修法 = `OPENBLAS_NUM_THREADS=1`

實作於 `tools/install_service_nssm.ps1` 的 `AppEnvironmentExtra`（Service 環境變數的
正式來源）。離線矩陣（default / 1 / 2 / 4 threads，1273 samples 真實 fixture）：

| | ΔPrivate（import numpy） | ΔThreads | 穩態 Private |
|---|---|---|---|
| default | +650.75 MB | +19 | 705.61 MB |
| **=1** | **+40.26 MB** | **+0** | **94.76 MB** |
| =2 | +72.07 MB | +1 | 126.67 MB |
| =4 | +136.44 MB | +3 | 191.61 MB |

報告保真度 12 項全通過（統計數值逐欄一致）；產出時間重複 4 次量測，
變體間 median 差距小於單一變體自身 spread → **無可測量退步**。

守門：`test/test_phase4_service_env.py` 新增 Phase 5.2 區塊（含 3 項負面測試），
防止設定日後被移除。

### 2.4 Deployment = **PASS**

為避免依賴「對既有服務執行 `install` 時 `nssm install` 失敗但後續 `nssm set` 仍會跑」
這個**副作用**，先補上正式的 `-Action update` 路徑：

- 只更新既有服務（不存在即 throw，**不退化成 install**）
- **不呼叫** `nssm install`、**不呼叫** `nssm remove`
- 設定改由 install / update 共用的 `Apply-ServiceSettings` 提供，兩者不會漂移
- `Assert-InstalledSettings` 擴充為回讀驗證 `AppEnvironmentExtra`（7 項集合比較、
  **大小寫敏感**以區分 `NO_PROXY` 與 `no_proxy`、`OPENBLAS_NUM_THREADS` 恰好 1 筆），
  不符即 throw、**不放行到 start**

部署流程 `stop → update → 獨立回讀驗證 → start`，未 remove、未手改 Registry。
部署後 **16 項驗證全數 PASS**（新 worker PID 16512）。

### 2.5 Fix Validation = **PASS**

驗證對象刻意鎖定「**部署後全新 worker 的第一次**自然 finalize」——
舊 worker 早已 import 過 numpy，其第二次 finalize 不漲只能證明 ONE-TIME COST，
無法證明修法有效。

| pre → T+0 | ΔWorkingSet | ΔPrivate | ΔThreads | ΔHandles |
|---|---|---|---|---|
| 舊環境第一次 finalize | +49.49 MB | **+672.49 MB** | **+21** | +21 |
| **新 worker 16512 第一次 finalize** | +19.73 MB | **+48.32 MB** | **+2** | +1 |

Private 降低 **92.8%**；+19 條 OpenBLAS native thread **未再出現**。
T+30m：Private 約 77.8 MB 且穩定（漂移 0.035 MB）、Threads 1~2、Handles 170~171。
Session Integrity **14/14 PASS**。

**比較限制（保留）**：舊 Session 1273 samples、新 Session 151 samples，
workbook 建構成本非同規模比較。但 OpenBLAS 的 +19 threads / ~650 MB 屬
**import 初始化成本，與 sample 數無關**；修正後 ΔThreads 僅 +2，不影響結論。
完全對等的 ~1200 samples Session 列為後續補強觀察，**非 COMPLETE 必要條件**。

### 2.6 Phase 5.2 最終狀態

```
Original Soak   : FAIL          （定案，不改寫）
Root Cause      : ONE-TIME COST
Fix             : OPENBLAS_NUM_THREADS=1
Deployment      : PASS
Fix Validation  : PASS
Phase 5.2       : COMPLETE
```

---

## 3. Phase 5.3 — Stress / Boundary Test

交付 `test/test_phase5_stress_boundary.py` 與 `test/_p53_stress_child.py`
（#11 跨行程 helper；刻意不動 Phase 4.4 的 `_p44_owner_child.py`，避免波及既有測試），
並註冊進 `run_phase3_regression.py`。

### 3.1 結果

| | |
|---|---|
| #1～#12 | **全部 PASS**（分 8 個 Batch 遞增執行，每批通過才進下一批） |
| Phase 5.3-A suite | **244 / 244 PASS**（連續執行 3 次皆相同） |
| Isolation Guard | **12 / 12 PASS**（未全過即中止，不執行任何壓力項目） |
| 既有測試 | 一項不減 |

### 3.2 鎖定的產品語意

邊界判定一律依產品碼**現行實作**，不依直覺。兩者刻意不對稱：

| 項目 | 運算子 | 邊界行為 |
|---|---|---|
| `ORPHAN_GAP_SEC`（300） | **`>`** 嚴格大於 | 299 否、**300 否**、301 是 |
| `AUTO_HOLD_STOP_SEC`（60） | **`>=`** 大於等於 | 59 否、**60 是**、61 是 |

其他鎖定的契約：`Energy.add()` 首筆只建基準不積分（N 筆 = N−1 區間）；
`ReportSession.finalize()` 本身**不守門**，idempotent 保證在監看層的
`_stop_and_finalize`（`stopping` / `finalized` 旗標）。

### 3.3 #11 Ownership contention

6 行程同時競爭 → **恰好 1 個 Owner**，落敗者一律 `held_by_other`；release 後可
takeover；崩潰（唯一 handle 持有者）→ 下一行程得 `acquired` / `abandoned=False`；
另有 handle 存活時 Wait 回 **WAIT_ABANDONED (0x80)** —— 符合 Phase 4.4/4.7 契約。

崩潰情境由子行程自行 `os._exit(0)` 模擬，**未使用任何強制終止指令**。

### 3.4 隔離證明

全程 temp root、零網路、不碰正式 output 與 Running Service。#11/#12 另加三條
前置硬性斷言（父與每個子行程回報的 mutex 皆 ≠ 正式 canonical；所有 root 位於
暫存目錄且不在正式 root 之下；正式 output 指紋前後一致），未過即中止。

Isolation Guard 另含**執行期攔截器**：每次 `acquire_ownership` 呼叫前實地斷言
取的不是正式 canonical Mutex —— 比原始碼字串掃描更強且無法繞過，並以注入測試
自我驗證確實會擋。

**結果**：正式 Mutex 零接觸、`monitor_owner.json` 零修改（size + mtime + sha256
三項比對）、正式 output 指紋前後完全一致（156 檔 `dc152f61c61814fc…`）、
worker PID / start time 全程未變。

**Phase 5.3 = COMPLETE**

---

## 4. Phase 5.4 — Production Documentation

交付 [`Operations_Guide.md`](Operations_Guide.md)（Production Operations /
Deployment Runbook，12 章 + 附錄），關閉 Plan §6.2 列出的 11 項缺口。

**三份文件的分工**：Operations Guide 講**怎麼做**、Closure Report 講**為什麼這樣設計**、
Validation Plan 講**怎麼驗證與結果**，交叉引用、不重複技術分析。

同步修正的既有文件：`tools/README.md` 確立為 **Service action 權威來源**並修正過時的
更新流程；`test/README.md` 的 Service 指令改為基本入口 + 連結（消除雙份維護漂移風險）；
`charge_discharge_report_DESIGN.md` 三處 openpyxl「尚待同意加入」改為歷史註記
（**未刪除原始紀錄**）。

**文件 DoD = 8/8 PASS**；自動化文件驗證 11/11 PASS；relative links **32/32**。

**Phase 5.4 = COMPLETE**

---

## 5. Phase 5.5 — Release Validation

### 5.1 驗證結果

| # | Release DoD | 結果 |
|---|---|---|
| 1 | Full Regression PASS | ✅ **1802/1802**，FAIL 0、SKIP 0、Environment residue PASS |
| 2 | Phase 5 新增 regression PASS | ✅ 5.2 守門 29 項／5.3 suite 244/244 |
| 3 | 5.2 Soak 收尾完成 | ✅ |
| 4 | 5.3 Stress / Boundary PASS | ✅ |
| 5 | 實機 Validation PASS | ✅ 三份自然 Session 各 8/8（見 §5.2） |
| 6 | Service health PASS | ✅ 11/11（見 §5.3） |
| 7 | Production Documentation 完成 | ✅ 8/8 |
| 8 | 無未處理 Critical / High defect | ✅ 無（見 §5.4） |
| 9 | Rollback instructions | ✅ Operations Guide §9 |

### 5.2 實機 Validation evidence

**未為 Phase 5.5 人工改動設備。** 採用 Phase 5.2 期間已完成並保存證據的自然 Session：

| Session | 角色 | samples | 時長 | end_reason |
|---|---|---|---|---|
| **`20260818_150301_auto`** | **主要 Release evidence** —— 部署後新 worker 16512 的**第一次**自然 finalize | 151 | 834.9 s | `auto_stop` |
| `20260818_093254_auto` | 輔助（部署前，discharge） | 919 | 5373.5 s | `auto_stop` |
| `20260817_160250_auto` | 輔助（部署前，charge） | 1273 | 7173.1 s | `battery_off` |

三份**逐項通過 8 項要求**：自動建立 Session、RUNNING、自然結束、finalize、
`status=completed`、`report.xlsx` 正常（13 sheets / 2 charts）、
Session integrity（`sample_index` 連續無重複、csv 列數 == `sample_count`）、
`output_status` 無 failed。

> Release 當下設備為 `mode=manual` / `sched=False` / `state=IDLE` / `session=None`
> —— Service 依設計不建立 Session，屬**正確行為**，非故障。

### 5.3 Service health（11/11 PASS）

```
Service      : Running / Automatic / LocalSystem      NSSM PID 33232
worker 16512 : 1 個行程，PPID = NSSM PID，start 2026-08-18T14:45:09
               Th=1  Hnd=173  Priv=80.76 MB
Ownership    : owner pid = worker、role=service、abandoned=False
Mutex        : owner 檔與 canonical 一致；一般使用者 probe = held_by_other
Dashboard    : 0        AppEnvironmentExtra : 7 項
Session      : 19 份，recording 0 / paused 0，find_active = None
log          : tick 持續前進、Traceback 0、err.log 0 bytes
```

### 5.4 Security / Critical / High

| 檢查 | 結果 |
|---|---|
| Service log 敏感字樣（password / token / cookie / authorization / bearer / `_raw`） | ✅ 0 |
| 10 份已輪替歷史 log 敏感字樣 | ✅ 0 |
| 版控追蹤檔案中的 credential 實值 | ✅ 無 |
| `*.env` 是否進版控 | ✅ 未進（`.gitignore` 排除 `.env` / `*.env` / `output/`） |
| Service log Traceback / err.log | ✅ 0 次 / 0 bytes |
| Critical / High 待處理缺陷 | ✅ **無** |

憑證只從 `*.env` 經 `API_ENV_FILE` 取得；診斷字串只帶端點與 application code，
不夾帶 msg / raw body。相關限制有測試守門與 log 掃描涵蓋。

**Phase 5.5 = COMPLETE**（驗證性項目；tag / push 另行決定）

---

## 6. Regression 最終基線

```
Automated checks              1802 / 1802  PASS
FAIL                                    0
SKIP                                    0
Environment residue                  PASS
```

| 套件 | 檢查數 | 期間變化 |
|---|---|---|
| py_compile | — | 新增 2 個 Phase 5.3 檔案 |
| report selftest | 267 | — |
| auto schedule | 372 | — |
| 3-B selftest | 122 | — |
| Dashboard harness | 23 | — |
| Windows file lock | 22 | — |
| PCS schedule switch | 84 | — |
| Phase 4.3 service | 56 | — |
| Phase 4.4 ownership | 62 | — |
| Phase 4.5 observer | 64 | — |
| **Phase 4.6-A service env** | **159** | 130 → 159（+29，Phase 5.2 守門與 update 契約） |
| Phase 4.6-B writer gate | 43 | — |
| Phase 4.6-B mutex DACL | 56 | — |
| Phase 4.6-B auth recovery | 116 | — |
| Phase 4.6-C mutex canon | 39 | — |
| Phase 4.7 crash recovery | 73 | — |
| **Phase 5.3-A stress/boundary** | **244** | 新增 |
| Environment residue | — | PASS |

**基線演進**：1529（Phase 4）→ 1558（Phase 5.2）→ 1802（Phase 5.3）。
既有項目**一項不減、一項不 FAIL**。

---

## 7. 已知 hardening opportunities 與技術債

### 7.1 Hardening opportunities（defense-in-depth，**非可達缺陷**）

Phase 5.3 #12 以刻意畸形的輸入探測時發現三處在**極端型別／內容**下會拋例外而非
安全回值。三者在**目前正式產品呼叫鏈皆不可達**，上游已有型別／資料來源契約守門：

| 現象 | 為何不可達 |
|---|---|
| `session_state.json` 為 JSON **陣列** 時，`_orphan_gap_seconds` 的 `st.get` 拋 `AttributeError` | `find_active_session()` 以 `isinstance(st, dict)` 過濾，陣列型 state 永不被選為 active |
| `_auth_session_lost({"_fail": 純量})` → `_ep_failed` 迭代純量拋 `TypeError` | `_fail` 一律由 `CDR.read_all._get()` 建構為 list |
| `find_active_session(None)` → `os.path.isdir(None)` 拋 `TypeError` | 產品一律傳 `_report_output_root()` 的回傳字串 |

**未修改產品碼**，測試中以 `[已知硬化機會]` 標記並鎖定現況。
⚠️ 未來若上述任一上游呼叫契約改變，**必須重新評估**是否加入型別守門。

### 7.2 既有技術債（Phase 3 已登記，未處理）

| 位置 | 內容 | 影響 |
|---|---|---|
| `alarm_records_scraper.py:69` | 告警「恢復時間」欄位 API row 未直接提供 | 低 —— 可由 `alarmStatus` 判斷是否恢復 |
| `battery_data_scraper.py:148` | `balanceStatus` 原始值語意未明，**刻意不臆測** | 低 —— 保留原始值 |

兩者**非 Critical / High**，不阻擋 Release。

### 7.3 後續補強觀察（非 DoD 必要條件）

| 項目 | 說明 |
|---|---|
| ~1200 samples 對等 Session | 使 Fix Validation 的兩次 finalize 成為完全同規模比較 |
| Dashboard 的 `OPENBLAS_NUM_THREADS` | `device_control_menu.py` 也會產報告，走同一 import 鏈；本次修法範圍限於 Windows Service，另案評估 |
| `requirements.txt` | repo 尚無；相依已在 Operations Guide §2 明列。刻意不為文件完整性而新增機制 |

---

## 8. Release 與 Rollback

### 8.1 Release 內容

| 項目 | 值 |
|---|---|
| 版本慣例 | 沿用既有 git tag `背景監控_V<Phase>`（annotated），**不新增 VERSION / SemVer 機制** |
| 預定 tag | `背景監控_V5.2` / `V5.3` / `V5.4` / `V5.5` |
| Deployment package | 版控追蹤內容 + Phase 5 新增檔案 |
| 必須包含 | `tools/nssm.exe`（SHA-256 `EEE9C44C29C2BE011F1F1E43BB8C3FCA888CB81053022EC5A0060035DE16D848`）、`test/.env.example`、`templates/ESS_Report_Template.xlsx`、`docs/Operations_Guide.md` |
| 必須排除 | `output/`、`*.env`、`__pycache__/` |

### 8.2 部署方式

見 [`Operations_Guide.md`](Operations_Guide.md)：

```
首次安裝        install → start
Service 設定更新 stop → update → verify → start
純 Python 更新   stop → 更新 .py → start
```

**`remove → install` 不是一般更新流程**，僅用於 `nssm.exe` 遺失／ImagePath 失效
需重新註冊的特殊情境。

### 8.3 Rollback

完整 SOP 見 [`Operations_Guide.md`](Operations_Guide.md) §9，含部署前 baseline、
安全窗口（`recording=0` / `paused=0` / `find_active=None` / `Dashboard=0`）、
各失敗點的處置，以及三種還原路徑（Service 設定 / 產品碼 / 服務定義損壞）。

**通用禁止**：不用強制終止當正常手段、不手改 Registry、未驗證通過不繼續、
不刪改既有 Session 輸出。

---

## 9. Phase 5 交付物

| 檔案 | 性質 |
|---|---|
| `docs/Phase5_Validation_Plan.md` | 驗收基準 + 各階段執行結果（跨 Phase） |
| `docs/Operations_Guide.md` | **新增** —— 部署 / 維運 Runbook |
| `docs/Phase5_Closure_Report.md` | **新增** —— 本文件 |
| `tools/install_service_nssm.ps1` | `-Action update` 契約、`Apply-ServiceSettings`、`AppEnvironmentExtra` 回讀驗證、`OPENBLAS_NUM_THREADS=1` |
| `test/test_phase4_service_env.py` | Phase 5.2 守門（29 項） |
| `test/test_phase5_stress_boundary.py` | **新增** —— Phase 5.3-A suite（244 項） |
| `test/_p53_stress_child.py` | **新增** —— #11 跨行程 helper |
| `test/run_phase3_regression.py` | 註冊 Phase 5.3 suite |
| `tools/README.md` | Service action 權威來源 |
| `test/README.md` | Phase 狀態、Regression baseline、文件索引 |
| `charge_discharge_report_DESIGN.md` | openpyxl 歷史註記 |

**產品 Python 執行碼零修改**（`report_monitor.py` / `charge_discharge_report.py` /
`charge_discharge_report_config.py` / `auto_monitor_service.py` 全部未動）。

---

## 10. 結論

### Phase 5 Validation = **COMPLETE**

5.1 ~ 5.5 全部完成，Full Regression 1802/1802 PASS，無未處理 Critical / High defect。

### Release 發布動作 = **尚未執行**

以下屬**後續人工 Release 動作**，**不得視為已完成**：

| 動作 | 狀態 |
|---|---|
| Release tag（`背景監控_V5.2` ~ `V5.5`） | **未建立** |
| push（remote / 目標分支未定） | **未執行** |
| 建立 upstream / merge develop / main_new | **未執行** |
| Deployment package 實際打包與驗證 | **未執行**（內容已定義，見 §8.1） |

**「Phase 5 = COMPLETE」僅代表 Validation 完成，不代表已發布。**

Phase 5 完成了 Phase 4 未做的四件事：長時間累積效應、軟體邊界條件、部署文件、
出版檢核。過程中揭露並修正了一個真實的資源缺陷，且全程未修改產品 Python 執行碼。

**尚待決定的事項**（不屬驗證範圍，保留給人工決策）：

- 4 筆 commit 的實際 staged diff 分割
- `背景監控_V5.2` ~ `V5.5` 四個 tag 的建立
- push 的 remote（`origin` 與 `Github_origin` 指向同一 GitHub repo）與目標分支
  （目前 `功能/背景監控服務` 無 upstream，領先 remote 4 個 commit）
- Deployment package 的實際打包與驗證

在上述決定完成前，**不 push、不建立 tag、不 merge**。
