# Crawler Sample — BESS Monitoring / Control / Auto Report

> **本文件是專案的 CURRENT STATE 入口。**
> 目前狀態一律以 [§9 Current Status](#9-current-status) 為唯一權威。
> 開發歷史、逐步推導與完整 evidence 見 [§15 文件索引](#15-文件索引)。

---

## 1. 專案簡介

儲能櫃（BESS）HMI 的資料擷取與設備控制工具集：

- BESS HMI 資料擷取（電池 / PCS / 環控）
- 人工設備控制（CLI，需二次確認）
- 自動充放電報告（Session → 產表）
- Windows Service 背景監控
- Phase 6 — Meter-based 自動充放電決策
- Phase 6.10 — 無人值守 ownership handoff

| 項目 | 值 |
|---|---|
| API Base | `http://192.168.128.110:8080/admin-api` |
| 前端 HMI | `http://192.168.128.110:8853/`（Vue，Yudao/ruoyi-vue-pro） |
| 正式電表來源 | `http://192.168.1.47:6160`（Socket.IO `update`） |
| External controller | `192.168.70.201`（`~/ems/auto_control.py`，screen `auto`） |
| 輸出目錄 | `D:\Crawler Sample\output` |

---

## 2. 系統架構

```
HMI / Meter
   → Observation（freshness / schema 驗證）
   → Decision（Grid 分類 → TOU → SOC → 決策）
   → Safety Gate / Control Authority / Direction Interlock
   → Execution intent（目前恆停在 WOULD_EXECUTE / NO_CONTROL / BLOCKED）
   → Report / Durable Audit
```

Phase 6.10 ownership handoff：

```
External running → Pause → Verify → Phase 6 ownership → Release → Restore → Verify
```

> 🔴 **Fail Closed 是全案唯一的預設立場**：
> 任何 unknown / invalid / stale / 無法證明的狀態一律拒絕放行。
> `None` **不是**「不限制」，而是「未配置 → 不得使用」。

---

## 3. Development Roadmap

> 每列只保留「在做什麼 / 現在到哪」。詳細設計、推導、測試數與歷史 evidence
> 一律在 [§15 文件索引](#15-文件索引) 指向的 `docs/`。
> 目前狀態的唯一權威是 [§9 CURRENT STATUS](#9-current-status)。

### 3.1 Phase 1 ~ Phase 6

| Phase | 項目 | 主要目的 | Current Status |
|---|---|---|---|
| Phase 1 | Report Framework | 建立報告框架與基本資料輸出 | **COMPLETE** |
| Phase 2 | Auto Start | 自動偵測測試開始 | **COMPLETE** |
| Phase 3 | Auto Lifecycle | Session 自動建立、Resume、Finalize | **COMPLETE** |
| Phase 4 | Auto Monitor Service | 背景監控服務化、Ownership、Observer、Recovery | **COMPLETE** |
| Phase 5 | Production Ready | Soak / Stress / Deployment / Release Validation | **COMPLETE** |
| **Phase 6** | **Meter-based Auto Charge / Discharge** | 電表 + TOU + SOC 決策，自動充放電控制架構 | **OFFLINE COMPLETE / LIVE HOLD** |
| 6.0 | Production Meter Verify | 確認正式 Meter 資料來源 | COMPLETE |
| 6.1 | Meter Client | 讀取正式電表資料 | COMPLETE |
| 6.2 | Power Classification | Grid 方向判定（IMPORT / EXPORT / NEAR_ZERO / UNKNOWN） | COMPLETE |
| 6.2b | TOU Calendar | 尖峰 / 半尖峰 / 離峰判定 | COMPLETE |
| 6.3-A | Decision Engine | 建立自動控制決策骨架 | COMPLETE |
| 6.3-B | Decision Policy | SOC / TOU / Meter 充放電規則 | COMPLETE |
| 6.4 | Safety Gate | 實機控制安全條件 | COMPLETE |
| 6.5 | PCS Control Integration | 控制出口、ReadBack、Authorization | COMPLETE / LIVE HOLD |
| 6.5-H | Control Authority | IDLE / OWNED / EXTERNAL 控制權判斷 | OFFLINE VERIFIED |
| 6.6 | Auto Report Integration | Phase 6 控制與報告 Session 整合 | COMPLETE |
| 6.7 | Field Validation | Production stack 實機唯讀驗證 | COMPLETE / OBSERVE_ONLY |
| 6.8 | Regression & Closure | Phase 6 整體離線回歸與結案 | COMPLETE |
| 6.9 | Controlled FIRST LIVE | 單次受控實機控制 | ATTEMPTED / BLOCKED |
| 6.9-A | CLI Entry Wiring | FIRST LIVE CLI 安全入口 | OFFLINE VERIFIED |

### 3.2 Phase 6 Offline Application Layer

| Item | Status |
|---|---|
| Offline Decision Simulator | COMPLETE |
| Long-Running Decision Replay | COMPLETE |
| Runtime / Arbiter Integration | COMPLETE |
| TariffProvider Production Wiring | COMPLETE / VERIFIED |
| Service Scheduling / Wait Injection | COMPLETE |
| Durable Decision Audit | COMPLETE |
| Process Lifecycle Enum | NOT CURRENTLY REQUIRED |

> **Phase 6 Offline Application Layer = COMPLETE / FROZEN** ——
> 不再主動新增架構、模組或功能；日後發現問題先分類為
> BUG / FIELD EVIDENCE / POLICY DECISION / FUTURE ENHANCEMENT，
> **不得直接解除 FROZEN**。

### 3.3 Phase 6.10

| Phase | 項目 | 主要目的 | Current Status |
|---|---|---|---|
| **Phase 6.10** | **Unattended Ownership Handoff** | External Controller 與 Phase 6 自動交接 | **OFFLINE COMPLETE / FIELD IN VALIDATION** |
| 6.10 Core | Handoff Core | State Machine / Journal / Recovery / Responsibility | COMPLETE |
| 6.10-A | Remote Adapter | SSH remote abstraction / taxonomy | COMPLETE |
| 6.10-A1 | Verification | Pause / Restore verification model | COMPLETE |
| 6.10-B1 | Read-only Guard | Forced-command SSH guard | COMPLETE |
| 6.10-B1.5 | Production Preconditions | Field prerequisites | COMPLETE |
| 6.10-B1.6 | Network Identity | SSH source identity validation | COMPLETE |
| 6.10-B1.7 | Network Identity Acceptance | 本次 deployment 的 identity 裁示 | **ACCEPTED / NOT PASS** |
| 6.10-B2 | Handoff Guard | pause / restore capability | **DEPLOYED** |
| 6.10-C | Unattended Service | 無人值守 handoff service | OFFLINE COMPLETE |
| 6.10-C1 | Service Timing | polling / lifecycle timing | COMPLETE |
| 6.10-C2 | SSH Architecture | SSH client / guard integration | COMPLETE |
| 6.10-C3 | SSH Timing | SSH timeout evidence framework | COMPLETE |
| 6.10-C4 | Live Readiness | unattended live gate | COMPLETE |
| — | Recovery Contract | Crash / restart recovery evidence | **FIXED / VERIFIED** |
| — | Field Trial #1 | pause / restore field validation | **PARTIAL PASS** |
| — | Deployed STOP Semantics | `auto_control.py` stop 語意與 Q1~Q4 | **CONFIRMED / CLOSED**（2026-09-17） |
| — | Trial #2 Harness | Active baseline / loop halt / one-attempt / evidence | **IMPLEMENTED / OFFLINE VERIFIED** |
| — | Trial #2（技術結果） | Discriminative pause validation | **PASS / DISCRIMINATIVE PAUSE VERIFIED**（2026-09-18） |
| — | Trial #2（Field Closure） | 程序與證據閉合 | **CLOSED WITH DOCUMENTED PROCEDURE DEVIATION**（2026-09-18） |
| — | FIRST LIVE Readiness Review | Offline 重新授權前檢查 | **COMPLETE**（2026-09-18） |
| — | FIRST LIVE Integration | FIRST LIVE ↔ 6.10 handoff 整合 | **OFFLINE COMPLETE** |
| — | FIRST LIVE Attempt #1 | 2026-09-18 單次 Controlled Field Attempt | **ABORTED_CURRENT_CONDITIONS**（EXPECTED FAIL-CLOSED SAFETY ABORT） |
| — | FIRST LIVE Attempt #2 | 2026-09-22 00:05–00:30 排程單次 Attempt | **ABORTED_CURRENT_CONDITIONS** —— ROOT CAUSE = **PROVIDER_WIRING_DEFECT** |
| — | Provider Wiring Defect 修正 | BUG #1 SOC band／BUG #2 classifier 暖機 | **FIXED / VERIFIED**（294 / 294，含真實 HMI／meter 複驗） |
| — | HMI Auth Renewal | 抽出 6.9 bounded re-auth 為單一 policy 來源 | **VERIFIED**（105 / 105） |
| — | FIRST LIVE Attempt #3 | 2026-09-23 00:05–00:30 排程單次 Attempt | **ABORTED_PRE_PAUSE_PREFLIGHT** —— ROOT CAUSE = **PROVIDER_WIRING_DEFECT** |
| — | Handoff Observation Adapter | BUG #3 修正（詞彙 adapter ＋ soc_max_pct ＋ external liveness canonical） | **OFFLINE FIXED / VERIFIED**（133 / 133） |
| — | FIRST LIVE Attempt #4 | 2026-09-24 00:05–00:30 排程單次 Attempt | **ABORTED_AT_7_FIRST_LIVE**（交接鏈全程打通） |
| — | FIRST LIVE Dispatch Scope | scoped temporary enable ＋ permit（16 項前置） | **OFFLINE IMPLEMENTED / VERIFIED**（108 / 108） |
| — | Scheduled Live-Leg Authorization | 非互動確認（structured authorization） | **OFFLINE VERIFIED**（106 / 106） |
| — | Full Live-Leg Readback | 完整 readback 狀態機離線收斂 | **OFFLINE VERIFIED**（94 / 94） |
| — | Attempt #5 Pre-Field Readiness | 正式狀態收斂 | **COMPLETE** |
| — | FIRST LIVE Attempt #5 | 2026-09-25 00:05–00:30 排程單次 Attempt | **MISSED / NOT EXECUTED**（HOST POWERED OFF） |
| — | FIRST LIVE Attempt #6 | 2026-09-30 00:05–00:30 排程單次 Attempt | **EXCEPTION_FAIL_CLOSED**（dispatcher factory signature defect） |
| — | Dispatcher Factory Wiring Fix | factory 透傳 ＋ real provider construction coverage | **OFFLINE FIXED / VERIFIED**（60 / 60） |
| — | FIRST LIVE Attempt #7 | 2026-10-01 00:05–00:30 排程單次 Attempt | **PAUSE_FAILED**（cross-attempt journal state leak） |
| — | Handoff Journal Scope Fix | attempt-scoped journal ＋ shared-state inventory | **◀ 目前位置：OFFLINE FIXED / VERIFIED**（66 / 66） |
| — | Scheduled FIRST LIVE（ONE-SHOT） | 一次性無人值守 commissioning runner | **◀ 目前位置：OFFLINE IMPLEMENTATION / VALIDATION** |
| — | FIRST LIVE Retry | Phase 6 controlled live retry | **HOLD** |
| 6.10-C LIVE | Unattended production deployment | 無人值守正式部署 | **NOT STARTED / BLOCKED** |

### 3.4 Flow

```text
Phase 1  Report Framework
   ↓
Phase 2  Auto Start
   ↓
Phase 3  Auto Lifecycle
   ↓
Phase 4  Background Service
   ↓
Phase 5  Production Ready
   ↓
Phase 6  Meter-based Auto Control          ← OFFLINE COMPLETE / LIVE HOLD
   ├─ Meter / TOU / Decision
   ├─ Safety / Authority
   ├─ PCS Integration
   ├─ Auto Report
   └─ Controlled FIRST LIVE                ← ATTEMPTED / BLOCKED
            ↓
Phase 6.10 Ownership Handoff               ← OFFLINE COMPLETE / FIELD IN VALIDATION
   ├─ Core / Recovery                      ← COMPLETE（Recovery Contract FIXED）
   ├─ Remote Guard                         ← B2 DEPLOYED
   ├─ Pause / Restore Verification         ← COMPLETE
   ├─ Field Trial #1                       ← PARTIAL PASS
   ├─ Deployed STOP Semantics              ← CONFIRMED / CLOSED（Q1~Q4）
   ├─ Trial #2 Harness                     ← IMPLEMENTED / OFFLINE VERIFIED
   ├─ Trial #2 技術結果                     ← PASS（DISCRIMINATIVE PAUSE VERIFIED）
   ├─ Trial #2 Field Closure                ← CLOSED WITH DOCUMENTED
   │                                             PROCEDURE DEVIATION
   ├─ FIRST LIVE Readiness Review          ← COMPLETE
   ├─ FIRST LIVE Integration               ← OFFLINE COMPLETE
   ├─ FIRST LIVE Attempt #1                ← ABORTED_CURRENT_CONDITIONS
   │                                          （EXPECTED FAIL-CLOSED SAFETY ABORT）
   ├─ FIRST LIVE Attempt #2                ← ABORTED_CURRENT_CONDITIONS
   │                                          （ROOT CAUSE = PROVIDER_WIRING_DEFECT）
   ├─ Provider Wiring Defect 修正           ← FIXED / OFFLINE VERIFIED
   ├─ Scheduled FIRST LIVE（ONE-SHOT）      ← ◀ OFFLINE IMPLEMENTATION / VALIDATION
   └─ Unattended LIVE Deployment           ← NOT STARTED
```

---

## 4. 主要檔案

| 檔案 | 用途 |
|---|---|
| `api_client.py` | API 薄封裝：session、登入、Bearer、統一 timeout |
| `dashboard_scraper.py` | Dashboard 數據概覽擷取 |
| `device_control_scraper.py` | 共用解析核心（PCS／電池／環控） |
| `device_control_menu.py` | CLI 設備控制選單 |
| `device_control_operator.py` | 控制指令組裝與送出（需 `--execute` + YES） |
| `charge_discharge_report.py` | Session 與報告產出（CSV／Excel／統計） |
| `report_monitor.py` | Monitor Core：監看決策、Session 生命週期、Ownership |
| `auto_monitor_service.py` | 背景服務入口，由 NSSM 啟動常駐 |
| `meter_client.py` | 併網點電表訂閱與 Fail Closed 驗證 |
| `power_classifier.py` | Grid 方向分類（遲滯 / debounce） |
| `tou_calendar.py` / `tariff_provider.py` / `annual_off_peak_calendar.py` | 時段規則 / 時區感知 provider / 年度離峰日 |
| `decision_engine.py` / `decision_policy.py` | 決策引擎與充放電策略 |
| `safety_gate.py` / `control_authority.py` | 安全閘與控制權判定 |
| `pcs_control_integration.py` | 方向互鎖、控制請求組裝、Dry-run pipeline |
| `decision_observer.py` | 觀測層：meter + ESS → 分類 → TOU → 決策（OBSERVE-ONLY） |
| `production_arbiter.py` | 仲裁／授權：freshness 複驗、Safety、Authority、互鎖 |
| `production_execution_chain.py` | 執行鏈（唯一可能呼叫 executor 之處；目前未注入） |
| `execution_reconciler.py` / `last_control_store.py` | 啟動恢復判定 / 控制紀錄（原子寫入） |
| `pcs_auto_control_runtime.py` | Phase 6 控制 Runtime 狀態機（純邏輯、零 I/O） |
| `pcs_auto_control_service.py` | Phase 6 服務外殼：Ownership、迴圈節奏、關閉流程 |
| `pcs_auto_control_production.py` | Production 相依組裝 |
| `phase6_decision_audit.py` | Durable Decision Audit（append-only JSONL） |
| `phase6_handoff_orchestrator.py` | Ownership 狀態機、Journal、恢復判定 |
| `phase6_remote_adapter.py` | 遠端 guard 契約、SSH timeout、capability、liveness |
| `phase6_unattended_service.py` | 無人值守服務 lifecycle 與 live readiness |
| `phase6_trial2_harness.py` | **Trial #2 field-validation harness**（trial tooling，非 production；零 I/O） |
| `phase6_first_live_handoff.py` | **FIRST LIVE ↔ 6.10 整合層**：dispatcher、Phase 6 authority lease、command-count guard、handoff execution artifact |
| `phase6_scheduled_first_live.py` | **Scheduled FIRST LIVE（ONE-SHOT）**：時間窗、durable ledger、crash recovery、self disarm。**非** Unattended LIVE |
| `run_scheduled_first_live.py` | **ONE-SHOT CLI launcher**：`--config` 解析／Fail Closed 驗證／provider allowlist／穩定 exit code |
| `phase6_first_live_field_provider.py` | **正式 field dependency provider**（WIRING ONLY；模組層只 import stdlib，其餘全部 lazy） |
| `phase6_guard_ssh_runner.py` | B2 guard 專用 SSH runner（**硬性動詞 allowlist**，非允許動詞一律拒絕） |
| `phase6_hmi_auth_renewal.py` | **HMI READ 認證續期（單一 auth policy 來源）**：bound 沿用 6.9＝2，只認 `_error401`，原地 re-login |
| `phase6_first_live_runner.py` | Phase 6.9 controlled live leg。**ROLE = INTERNAL LIVE LEG / DISPATCHER；DIRECT FIELD EXECUTION = BLOCKED** |
| `phase6_decision_simulator.py` / `phase6_decision_replay_runner.py` | 離線決策模擬器 / 虛擬時鐘 replay runner |
| `run_phase3_regression.py` | Phase 2~5 Regression 總表 |
| `run_all.py` | 一鍵執行全部**擷取腳本**（⚠️ **不是** Phase 6 regression 入口） |

---

## 5. 快速使用

```bash
python device_control_scraper.py     # 產生 device_control_readonly.json + 摘要
python dashboard_scraper.py --once   # Dashboard 抓一次
python device_control_menu.py        # CLI 設備控制選單
python run_phase3_regression.py      # Phase 2~5 Regression
```

- **登入設定**放在 `test/` 下的 `*.env`（例如 `login.env`），依 `.env.example` 建立。
  **`.env` 已由 `.gitignore` 排除，不可 commit。**
- ⚠️ **`run_all.py` 不是 Phase 6 regression 入口** —— 它會對 HMI 發出唯讀 GET 並寫入
  `output/`。任何名稱不明的 `run_all.py` / `main.py` / `service.py` / `runner.py`，
  執行前必須先確認 I/O side effects。
- Phase 6 regression 以**逐檔執行** `test_phase6*.py` 為準（見 [§14](#14-regression)）。

---

## 6. Auto Monitor / Auto Report

```
排程 / 控制方向 → Session → Sampling → Finalize → report.xlsx
Windows SCM → NSSM → auto_monitor_service.py → report_monitor.py
```

- 輸出：`output\charge_discharge_reports\<session_id>\`
- **Service = Owner；Dashboard = Observer**（唯讀，不寫報告）
- Graceful / Crash Recovery 皆續接同一 `session_id` 與 folder，
  `sample_index` 繼續遞增，**不會建立第二份 Session**
- `tools\nssm.exe` 為 runtime dependency，不可刪除／改名／搬移
- Service log：`output\logs\auto_monitor_service.log`

> Service 六個動作的權威說明見 [`../tools/README.md`](../tools/README.md)；
> 部署 / 健康檢查 / Upgrade / Rollback 見
> [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md)。

---

## 7. Phase 6 — Production Contract

### 7.1 已核准參數

| 參數 | 值 | 狀態 |
|---|---|---|
| `charge_power_kw` | 5.0 | **FINAL** |
| `discharge_power_kw` | 5.0 | **FINAL** |
| `max_power_kw` | 150 | **FINAL**（Safety Gate 指令上限，**不是**營運功率） |
| `decision_interval_sec` | 30 | **FINAL** |
| `authorization_ttl_sec` | 10 | **FINAL** |
| `authority_ttl_sec` | 180 | **FINAL** |
| `authority_power_tolerance_kw` | 1.25 | **FINAL** |
| `readback_timeout_sec` / `poll` / `samples` | 75 / 5 / 1 | **FINAL** |
| `min_switch_interval_sec` | `None` | **DEFERRED**（Layer 2 時間互鎖未啟用） |
| `meter_stale_grace_sec` | `None` | **DEFERRED**（採最保守解釋＝不等待） |

### 7.2 三種 kW 語意（**嚴禁混用**）

| 來源 | 正號 | 負號 |
|---|---|---|
| **Grid Meter**（6160 `meter`） | `+` = GRID_IMPORT 取電 | `−` = GRID_EXPORT 逆送 |
| **Battery measurement** | `+` = CHARGE | `−` = DISCHARGE |
| **PCS command** | `CHARGE` → **負** setpoint | `DISCHARGE` → **正** setpoint |

> 禁止 `meter +kW → PCS +kW` 直通。Decision 一律用 IMPORT / EXPORT，
> 只有 PCS 控制邊界才轉 setpoint 正負。

### 7.3 Layer 1 Direction Interlock

- PCS 實際為 `CHARGING` 時要求 `DISCHARGE` → `DIRECTION_REVERSAL_REQUIRES_STOP`
- PCS 實際為 `DISCHARGING` 時要求 `CHARGE` → 同上
- PCS 為 `STANDBY` → 方向已隔離，**合法換向**
- 🔴 判準一律是 **PCS 實際回報狀態**，**不得**用「上一筆送出的指令」推導
- Arbiter 層遇反轉會另外形成獨立的 **STOP leg**，不直接反向

### 7.4 `dispatch_ready` ≠ `dispatch_enabled`

- `dispatch_ready`：production 參數是否齊備
- `dispatch_enabled`：是否允許送出（模組層恆為 `False`）
- **條件齊備不等於允許送出。** OBSERVE_ONLY 下 executor / verifier 恆為 `None`，
  「送不出指令」是**結構事實**，不是旗標判斷。

### 7.5 Production TOU

```
build_production_stack() → PRD.build_tariff_provider(AC.PRODUCTION_PROVIDER)
                         → PRD.build_local_now()   # Asia/Taipei aware
                         → DecisionObserver(tariff_provider=…, local_now=…)
                         → tou_calendar.classify_tou(...)   # 規則只有這一套
```

- 時區 **Asia/Taipei**，時間一律 **aware datetime**
- **naive datetime 明確拒絕** → TOU `UNKNOWN` → `no_action`（**UNKNOWN ≠ OFF_PEAK**）
- 年度離峰日沿用已驗收的 **2026 / 2027** 清單
- 時區無法解析時**不 fallback UTC、不用系統本機時間**

### 7.6 Durable Decision Audit

| 層 | 內容 | 保存 |
|---|---|---|
| `AutoControlRuntime.audit` | 最近 200 筆 `CycleResult` | 記憶體，重啟即失 |
| `phase6_decision_audit.AuditSink` | 每 cycle **一筆** immutable 紀錄 | `output/phase6_audit/decision_audit.jsonl` |

- `ONE_CYCLE_ONE_RECORD` / `APPEND_ONLY_JSONL` / fsync `EVERY_CYCLE`
- 寫入失敗 = **`NON_GATING_CONTINUE`**：控制照跑，但**絕不假裝成功**
- `at_wall` = Asia/Taipei aware ISO-8601（**僅供鑑識**）；
  `at_monotonic` 是**唯一**用於 elapsed / freshness / TTL 的基準
- 🔴 Audit 是 **evidence，不是控制狀態恢復來源**
- 🔴 **不得增加任何 device I/O**（已由測試鎖住讀取次數相同）
- 🔴 **不得寫入**密碼 / token / cookie / 私鑰 / 原始 response / `alarm_rows`

### 7.7 Service Cadence

- cadence **唯一來源** = `DEFAULT_CONTROL_CONFIG.decision_interval_sec`
- cadence 必須 **finite 且 > 0**；`0` / 負 / `None` / `NaN` / `inf` / 非數
  → **不進入主迴圈**（Fail Closed），不 fallback 任何秒數
- `run(waiter=…)` 可注入等待機制；離線測試以「正式 cadence + fake waiter」零等待
- cadence 屬 service shell 的 I/O 排程，**不得**塞進 `AutoControlRuntime`

---

## 8. Phase 6.10 — Ownership Handoff

```
External running → Pause → Verify → Phase 6 ownership → Release → Restore → Verify
```

### 8.1 核心 Safety 規則

- **同一時間只能有一個控制者**
- 任何不確定 → **Fail Closed**
- `pause` 一旦可能送出 → **restore responsibility 成立，且不可清除**，
  直到 `RV_RESUMED` 驗證通過
- **`SEND_UNKNOWN`（SSH exit 41 / command timeout）唯一代表「可能已執行」**
  → 責任成立、**禁止重送**、必須 probe / recover
- crash / 重啟後責任不消失：`recover()` 依證據重建
- 停止軟體服務 **≠** 停止 PCS（`shutdown` = relinquish authority only）

### 8.2 Recovery Evidence Contract

`running` 的語意是 **external controller active loop confirmed**，
**不是** process exists、**不是** screen exists。

| 條件 | verdict | `recover()` |
|---|---|---|
| probe 必要事實取不到 | `EXTERNAL_UNKNOWN` | `R_UNKNOWN` |
| 事實齊備但 screen / process / identity 任一為 False | `EXTERNAL_NOT_RUNNING` | `R_PENDING_RESTORE` |
| runtime 健康 + loop marks **前進** | `EXTERNAL_RUNNING` | `R_CLEAN` |
| runtime 健康 + loop marks **未前進** | `EXTERNAL_NOT_RUNNING` | `R_PENDING_RESTORE` |
| runtime 健康但 loop 證據不足 | `EXTERNAL_UNKNOWN` | `R_UNKNOWN` |

> recovery 證據**只准** `probe` + `loopcheck`（皆 PASSIVE_READ_ONLY），
> **不得**使用 `status`。

### 8.3 遠端動詞分類

| 動詞 | 遠端動作 | 分類 |
|---|---|---|
| `probe` | `screen -ls` / `pgrep` / `ps` / `readlink` | **PASSIVE_READ_ONLY** |
| `loopcheck` | `screen -X hardcopy`（抓兩次時間戳，暫存檔即刪） | **PASSIVE_READ_ONLY** |
| `status` | `screen -X stuff $'status\n'` —— **寫入 controller stdin**；deployed source 已確認只 `print` 參數，**無** device control effect | **QUERY_ONLY_WITH_STDIN_WRITE**（transport 仍是 stdin write，**不得**稱 PASSIVE_READ_ONLY） |
| `pause` / `restore` | `screen -X stuff $'stop\n'` / `$'start\n'` | **CONTROL_VERB**（需 `armed=True`） |

> 🔴 `status` **不得**被描述為 pure read-only。

### 8.4 Deployed PAUSE / STOP Semantics（2026-09-17 CONFIRMED）

> 來源：直接讀取 `192.168.70.201:/home/etica/ems/auto_control.py`
> （`sha256 964bb642…76ce`，789 行）。**此為 deployed behavior 的最高權威**；
> 本機 2025-04 舊副本（361 / 314 / 242 行）僅為 historical reference。

```
PAUSE SEMANTICS = ZERO-UNTIL-STOP-CONFIRMED → THEN COMMAND SILENCE
```

```
stop
 ├─ running = False                       ← 正常 control loop 立即停用
 └─ 進入停止確認 loop（每 ~5s）
      ├─ bess_client_read()；registers 空 → continue（不宣稱停止完成）
      ├─ LastPd[i] = 0 / Step[i] = 0
      ├─ 讀實際 BESS power
      ├─ 若仍非 0 → request_kw(i, 0) 並 is_stop = False
      └─ 全部收斂為 0 → is_stop = True
            → print("已停止運行") / Pd = 0 / LastVer.clear() / break
之後回到 outer while：running == False → sleep(1); continue
（thread 存活，正常 control loop 不執行；僅 start 可恢復）
```

🔴 **`pause` 不是純 software flag change。**
收斂期間會**重複**送出 `request_kw(i, 0)` = 真實 HMI control write
（`activePowerSetPoint = 0`），**不是** Modbus write。

> **`PAUSE TRANSITIONAL CONTROL EFFECT = REPEATED ZERO SETPOINT WHILE CONVERGING`**
> 它**不是** Phase 6 CHARGE / DISCHARGE / PCS STOP，
> 但**是**由 B2 pause 觸發的真實 external-controller command，
> 必須在 field evidence 中明文記錄。

**Timestamp / loopcheck 語意**：deployed 的 `strftime("%Y-%m-%d %H:%M:%S")` print
位於 `if not running: sleep(1); continue` **之後**的正常 demand loop 內。因此
`running=False` 時 marks 應停止前進 —— 這可支持 **NORMAL CONTROL LOOP HALTED**，
但**不能單獨**證明 stop convergence 已完成（收斂 loop 期間正常 timestamp 已不再輸出）。

**`start` / restore 語意**：`start` 僅將 `running = True`，**本身不送 `request_kw()`**。
恢復後是否出力由 controller 依其 policy / demand / SOC / TOU 自行決定 ——
因此 `RestoreVerifier` 不要求 restore 後一定 CHARGING / DISCHARGING 是正確的。

🔒 deployed `bess_request.py` 內存在 hard-coded credential。
**SECURITY ISSUE / SEPARATE TRACK**；本專案文件不記錄任何 secret 值。

### 8.5 Trial #2 Harness（field-validation tooling）

`phase6_trial2_harness.py` —— **trial tooling，不是 production application layer**。
零 I/O：sample / probe / loop mark / sender 全部由呼叫端注入，模組本身不含
SSH / HTTP / Modbus / subprocess（由回歸 AST 掃描鎖住）。

**兩層分離（G2 POLICY）**

| 層 | 內容 |
|---|---|
| **LAYER 1 SAFETY PREFLIGHT** | 15 項。合法 PCS 觀測狀態 = `CHARGING` / `DISCHARGING` / `STANDBY` / `STOPPED`。🔴 **不得**因 PCS 正在充放電就判 FAIL（Trial #1 的 idle-only 條件明文不得沿用）；`UNKNOWN` / `FAULT` / `None` 仍 Fail Closed |
| **LAYER 2 ACTIVE BASELINE GATE** | `loop marks ADVANCING` **AND**（`PCS=CHARGING` ∨ `PCS=DISCHARGING` ∨ `Authority=EXTERNAL_OR_UNKNOWN`）。Meter = **CORROBORATING ONLY**，結構上不進入判定 |

observe duration / sample interval 是 **trial 參數**（建議 ≈600 s / 20 s），
由 `BaselineConfig` **必填**傳入，**不得**預設、**不得**寫進任何 production 模組。

**ABORT RULE**：baseline 未成立 → ABORT、**不送 pause**、**不需 restore**
（`pause_attempt_count == 0`，責任未成立）。

**LOOP HALT（G4）**

```
marks stopped  = NORMAL CONTROL LOOP HALTED
marks stopped != STOP CONVERGENCE COMPLETE
marks stopped != PAUSE PASS
```

**唯一**的鑑別結論函式：

```python
discriminative_pause_verified(normal_loop_halted, pause_verifier_state)
    -> normal_loop_halted is True AND pause_verifier_state == PV_VERIFIED
```

**ONE ATTEMPT（G5）**：`OneAttemptGuard` 已版控化，`pause` / `restore`
各硬上限 1 次，超額**拋 `OneAttemptExceeded`**（不是回 False）。
即使 `SEND_UNKNOWN` 也**禁止**自動 resend。

**Evidence artifact（G7）**：schema v1、37 欄位，必備 9 項 ——
`pause_transitional_control_effect` / `normal_loop_halted` / `pause_verified` /
`restore_resumed` / `active_baseline_established` / `pause_attempt_count` /
`restore_attempt_count` / `pause_send_outcome` / `restore_send_outcome`。
`validate_artifact()` 會擋下計數超額、鑑別結論與證據不一致、
以及把 pause 寫成 `immediate command silence` 的用語錯誤。

**BASELINE WINDOW POLICY（2026-09-18 裁示）**

```
BASELINE_WINDOW_POLICY = "MAX_OBSERVATION_WINDOW"
BASELINE_EARLY_SUCCESS_ALLOWED = True
```
`observe_duration_sec` 是 **MAX OBSERVATION WINDOW**，**不是** MANDATORY DWELL TIME。
Active Gate 在窗口內成立即可 EARLY SUCCESS 並立即保存 terminal active evidence ——
因為 Trial #2 要證明的是「pause 當下 external 確實 active」，時間鄰接性優先。
🔴 **此為 FUTURE POLICY，不回溯**抹除 2026-09-18 Trial #2 的
EXECUTION PROCEDURE DEVIATION（見 [§11](#11-current-blockers)）。

**計數語意（2026-09-18 閉合）**

| 欄位 | 意義 |
|---|---|
| `baseline_sample_count` / `sample_count` | 餵進 observer 的 baseline 樣本數，**不含** runner step 1 的 safety-preflight 取樣 |
| `observing_sample_count` | 成立前的觀測筆數（＝ baseline − 1） |
| `terminal_active_sample_index` | Active Gate 成立那筆的 **1-based** baseline 索引；未成立為 `None` |
| `counts_exclude_preflight_sample` | 恆為 `True` |

> 執行腳本若把 preflight 取樣一起編號，其日誌序號會比 artifact 大 1 ——
> 那是**執行腳本的日誌編號**，**不是** artifact off-by-one。

**SOURCE-SIDE FRESHNESS（evidence layer only）**

```
source_freshness(raw_source_time, observed_at, max_age_sec)
    -> SOURCE_FRESH / SOURCE_STALE / SOURCE_TIME_UNAVAILABLE / SOURCE_TIME_MALFORMED
    age_sec = local observation time − device raw_source_time
```
🔴 `EssSnapshotAdapter` 的 `age_sec` / `stale` 由**讀取開始時刻**推算，衡量的是
**HTTP 往返延遲**，**不代表設備端資料新鮮度** —— 兩者不得互相冒充（由 F5 測試鎖住）。
🔴 取不到 → `SOURCE_TIME_UNAVAILABLE`；解析失敗 → `SOURCE_TIME_MALFORMED`。
兩者 `source_age_sec` 皆為 `None`，**絕不造 timestamp**。
artifact schema **v2** 起必備 `raw_source_time` / `source_time_available` /
`source_age_sec` / `source_freshness_state`；**v1 歷史 artifact 維持可驗證**。

> `PauseVerifier` / `RestoreVerifier` / SSH taxonomy / responsibility 生命週期 /
> Phase 6 decision semantics / production arbiter / production safety policy
> **一律沿用既有實作，未修改**。

### 8.6 FIRST LIVE ↔ 6.10 Handoff 整合（2026-09-18 P3 裁示）

FIRST LIVE 正式路徑改走 **Phase 6.10 HANDOFF PATH**。
**不再**以 Phase 6.9「external 當下 idle 就直接執行」作為 field execution path。

> **理由（Trial #2 實測）**：external 可能 `PCS=STANDBY` / `Authority=IDLE`
> 但 normal loop marks 仍持續 advancing，之後自行轉入 `DISCHARGING`。
> 「external 當下 idle」**不等於**「external 已 relinquish control」——
> 6.9 原 Phase A 存在 **TOCTOU / ownership race**。

```
External Controller
  → Fresh observation / safety preflight
  → Phase 6.10 pause
  → NORMAL LOOP HALTED + PauseVerifier VERIFIED
  → external command silence established
  → Phase 6 FIRST LIVE dispatcher
  → 既有 5 kW charge-only live leg
  → Phase 6 release
  → Phase 6.10 restore
  → RestoreVerifier RV_RESUMED
  → post-check → COMPLETE
```

**兩種 ownership 嚴格分離**

| | 由誰管 | 清除條件 |
|---|---|---|
| EXTERNAL CONTROLLER OWNERSHIP | 6.10 pause / restore responsibility | 只有 `RV_RESUMED` |
| PHASE 6 CONTROL AUTHORITY | `Phase6AuthorityLease` | `release()`（一律在 `finally`） |

🔴 禁止：external 尚未 verified pause 就 Phase 6 acquire / send。
🔴 禁止：Phase 6 authority 尚未 release 就 restore external ——
由 `LifecycleRecorder` 的事件序列證明（`release_before_restore`）。

**Dispatcher 介面**

```
(observation) -> {"dispatched", "verified", "released", "outcome", "reason", ...}
outcome ∈ DISPATCH_NOT_ATTEMPTED / DISPATCH_SUCCEEDED / DISPATCH_FAILED / DISPATCH_UNKNOWN
```
🔴 `DISPATCH_UNKNOWN` **不得**當成 verified；例外、未知終態一律 Fail Closed 成 UNKNOWN。
🔴 dispatcher **不知道** CHARGE 怎麼送 —— `live_leg_runner` 由呼叫端注入
（production 即既有 `SVC.run_live_leg_cli`）。**不建立第二套 charge execution logic。**

**FIRST LIVE FIELD ENTRY POINT = PHASE 6.10 HANDOFF PATH ONLY**（RULING 1）

| 模組 | 角色 | 直接 CLI |
|---|---|---|
| `phase6_first_live_runner.py` | INTERNAL LIVE LEG / DISPATCHER | 🔴 **BLOCKED**（exit 2） |
| `pcs_auto_control_service.py --live-leg` | 既有 live-leg CLI | 🔴 **BLOCKED**（exit 2） |

兩者的拒絕訊息一致：
`FIRST LIVE direct execution disabled. Use Phase 6.10 handoff entry point.`

🔴 **刻意不提供** `--force` / `--bypass-handoff` / 環境變數 / 任何隱藏 override。
🔴 被限制的是 **FIELD ENTRY POINT**，**不是刪除 6.9 implementation** ——
`live_leg_main()` / `run_live_leg_cli()` / `main()` 函式本體完全未動，
整合層與離線測試仍可 import 後直接呼叫。

**Command count（硬上限，超額拋 `CommandLimitExceeded`）**

| 項目 | 上限 |
|---|---|
| `phase6_charge` | **≤ 1**（ONE CONTROLLED LEG ONLY） |
| `phase6_discharge` | **0** |
| `phase6_pcs_stop` | **≤ 1** —— 🔴 **包含** live-leg cleanup STOP |
| `pause` / `restore` | **≤ 1** |

🔴 **RULING 2**：既有 live leg 實際送出的 cleanup PCS STOP **必須**計入
`phase6_pcs_stop` aggregate。**不得**因為它叫 `live_leg_internal_stop` 就排除於總數。
`live_leg_internal_stop` 改為 **subtype / reason**（`pcs_stop_reason = LIVE_LEG_CLEANUP`），
**不是豁免**。

> 未來若 FIRST LIVE 送出一次 5 kW CHARGE 並由 live leg 送出一次 cleanup STOP，
> artifact 必須如實記 **CHARGE = 1 / DISCHARGE = 0 / PCS STOP = 1**，
> **不得**寫成 `PCS STOP = 0`。
> ⚠️ 允許 `≤ 1` **不代表**可新增任何新的 STOP 路徑 —— 只認既有 6.9 live-leg
> lifecycle 本來就存在的 cleanup STOP。本次僅為 **COUNTING / EVIDENCE SEMANTICS**
> 修正，未動 STOP policy / timing / trigger / PCS control semantics。

**兩個 ELIGIBILITY GATE（2026-09-18 P0 flow correction）**

| Gate | 問的問題 | Authority 的角色 |
|---|---|---|
| **GATE A** `pre_handoff_policy_eligibility()`（9 項） | 「**如果** ownership 可以合法取得，目前正式策略是否應該執行 CHARGE？」 | 🔴 **完全不看** —— 由 AST 鎖住不引用 `authority` / `arbitration_outcome` / `authorized` / `CONTROL_AUTHORITY_BLOCKED` |
| **GATE B** `post_handoff_revalidation()`（10 項） | 「pause 已 VERIFIED，用**新的** fresh 觀測重新確認策略是否仍成立？」 | 🔴 **必須** `authority == IDLE`（`external_relinquished`） |

🔴 `Authority = EXTERNAL_OR_UNKNOWN` 在 external identity / runtime / handoff
capability 正常時，代表 **HANDOFF REQUIRED**，**不是** FIRST LIVE POLICY INELIGIBLE。
若在 GATE A 因 `CONTROL_AUTHORITY_BLOCKED` 就 abort，等於要求「Authority 必須先
IDLE 才能進 handoff」——那正是舊 6.9 的 **TOCTOU** 問題，也讓 handoff 失去意義
（handoff 的前提本來就是 external 目前持有控制權）。

```
HANDOFF_ALWAYS_REQUIRED = True    # 刻意沒有「跳過 handoff」的分支
```
`handoff_requirement()` 只輸出**為什麼仍需 handoff**的證據
（`authority_external_or_unknown` / `external_loop_active` /
`authority_idle_is_not_relinquish_proof`），**不**提供跳過選項。

**GATE B 的位置**：在 `FirstLiveDispatcher.__call__()` 內、**`Phase6AuthorityLease.acquire()` 之前**。
未通過 → 不 acquire、不送任何 Phase 6 command（counts 維持 `0 / 0 / 0`）、
`dispatcher_outcome = DISPATCH_NOT_ATTEMPTED`，由 `HandoffRunner` 走 restore responsibility path，
分類為 `ABORTED_POST_HANDOFF_REVALIDATION`。
🔴 **不得**沿用 pause 前的 decision / SOC / TOU / PCS state 直接 dispatch；
樣本未標記 `post_handoff_fresh` 一律 Fail Closed。

**兩層 FIRST LIVE PRECHECK**

| 層 | 內容 |
|---|---|
| **LAYER 1 SAFETY PREFLIGHT**（11 項） | comm / source / fault / critical / screen / process / identity / ownership≠BOTH / guard B2 / DRY_RUN baseline / armed=False baseline。🔴 **不得要求 external 本來 idle** |
| **LAYER 2 HANDOFF READINESS**（4 項） | external runtime 可確認、loop evidence 可取得、pause capability、restore capability |

**兩套 evidence（刻意不揉合）**

| | 內容 |
|---|---|
| Durable Decision Audit（既有 46 欄，**未新增任何欄位**） | Phase 6 decision / control evidence |
| `PHASE6_10_HANDOFF_EXECUTION_ARTIFACT` v1（34 欄） | ownership / handoff evidence：pause·restore attempt 與 outcome、`normal_loop_halted`、PauseVerifier / RestoreVerifier、responsibility lifecycle、Phase 6 authority acquired/released、dispatcher outcome、command counts、`release_before_restore`、final ownership、fault / critical、timestamps |

`timeout_status` 恆記 `FIELD_VALIDATION_CANDIDATE 12.0 / final=False / n=2`；
標成 FINAL 會被 `validate_handoff_artifact()` 擋下。
`validate_handoff_artifact()` 另擋：`pcs_stop ≥ 1` 卻無 `pcs_stop_reason`、
subtype 數量大於 aggregate（cleanup STOP 被排除）、`charge > 1`、`discharge > 0`、
`pcs_stop > 1`。

**FIRST LIVE outcome 語意（三者必須並存）**

```
handoff_outcome     = 交接生命週期是否安全收斂（external 已 restore）
dispatcher_outcome  = FIRST LIVE 是否成功（只有 DISPATCH_SUCCEEDED 才算）
live_leg_outcome    = 既有 leg 的終態與 dispatch / stop 次數
```
🔴 `handoff_outcome = COMPLETE` **不等於** FIRST LIVE CHARGE PASS ——
避免把「restore 成功」誤寫成「charge 成功」。

### 8.7 Scheduled FIRST LIVE（ONE-SHOT commissioning）

```
SCHEDULED_MODE      = ONE_SHOT_COMMISSIONING
IS_UNATTENDED_LIVE  = False      IS_RECURRING = False
AUTO_RETRY_ALLOWED  = False
```

🔴 **不是** Unattended LIVE、**不是** recurring schedule、**不是** auto retry。
它是「使用者白天先授權一次，runner 在指定 field window 內自動做**一次** fresh
eligibility 判斷」的 commissioning runner。

**架構 —— 只是包在既有流程外層的 orchestration shell**

```
Scheduler / One-Shot Gate
  → Fresh STEP 0 → Fresh Safety Preflight → GATE A
  → 既有 Phase 6.10 Handoff（pause / PauseVerifier / GATE B /
     Phase6AuthorityLease / FIRST LIVE Dispatcher / 既有 6.9 5 kW live leg /
     cleanup STOP / release / restore / RestoreVerifier）
  → post-check → artifact → SELF DISARM / EXIT
```
AST 鎖住：模組**未** import `decision_engine` / `decision_policy` / `safety_gate` /
`pcs_control_executor` / `device_control_operator` / `pcs_auto_control_service` /
`control_authority` / `tou_calendar` / `subprocess`，只相依
`phase6_first_live_handoff`。**沒有第二套 control logic。**

**Time window**

| 情況 | 行為 |
|---|---|
| 早於 `window_start` | 只等待（可設 `max_wait_sec` 上限，超過即 `EXPIRED_NO_ATTEMPT`） |
| 在窗內 | 進 Fresh Preflight，**只做一次** |
| 晚於 `window_end` | `EXPIRED_NO_ATTEMPT`，0 commands |
| 非授權日期 | `WRONG_TARGET_DATE` |

🔴 窗內 GATE A 不成立 → `ABORTED_CURRENT_CONDITIONS`，**ONE-SHOT 結束**。
**不**每 10 秒 retry、**不**等 decision 改變、**不**重跑第二次 preflight。

**授權消耗**：任何結果（PASS / ABORT / FAIL / UNKNOWN / EXCEPTION / TIMEOUT /
EXPIRED）都寫入 `CONSUMED`，同一 task 再啟動一律 `ALREADY_CONSUMED`。
`target_date` / `window_start` / `window_end` / `timezone` / `attempt_id`
**全部必填、由 one-shot config 注入**，production code **不得寫死**（AST 鎖住原始碼
無 `Asia/Taipei` / `00:00` / `00:30` 字面值）。

**Durable ledger（crash / restart safety）**

```
NOT_STARTED → PREFLIGHT → EXTERNAL_PAUSE_RESPONSIBILITY → PHASE6_AUTHORITY
→ LIVE_LEG → PHASE6_RELEASED → RESTORE_PENDING → RESTORED → COMPLETE
```
append-only JSONL ＋ 每筆 `fsync`；**意圖先落盤再執行**。
只要 `pause` 的 INTENT 落過盤，`restore_responsibility` 即成立（即使 OUTCOME
因 crash 未寫入），且只有 `restore_verified = RV_RESUMED` 才解除。
重啟後 `recover()` → `RECOVERY_PENDING_RESTORE` / `RECOVERY_ALREADY_CONSUMED` /
`RECOVERY_CLEAN` / `RECOVERY_CRITICAL_MANUAL`；
🔴 **一律 NO RESEND pause / charge / restore**，UNKNOWN → Fail Closed。
半截的最後一行（crash 當下未寫完）會被忽略，且**不影響**前序狀態。

**SELF DISARM**：結束時一律確認 `DISPATCH_ENABLED=False`、`armed=False`、
production `pause`/`restore` timeout `= None`、`FIRST_LIVE_PREREQUISITE_SATISFIED=False`。

**Result states（19 項）**：含使用者指定的 15 項，另加
`NOT_ENABLED` / `ALREADY_CONSUMED` / `WRONG_TARGET_DATE` / `EXCEPTION_FAIL_CLOSED`。

**Host runtime precondition**：`evaluate_host_runtime()` 為**純判定**，
evidence 由呼叫端唯讀取得；任一未知即 Fail Closed，且**不修改任何電源設定**。

**CLI launcher（`run_scheduled_first_live.py`）**

```
"<python.exe>" "<absolute launcher path>" --config "<absolute config path>"
```
| 面向 | 內容 |
|---|---|
| config 必填欄位 | `enabled` / `attempt_id` / `target_date` / `window_start` / `window_end` / `timezone` / `dependency_provider` |
| 驗證 | 缺欄位・壞 JSON・非法 date/window/timezone → `CONFIG_INVALID`；`enabled` 非**明確 True** → `NOT_ENABLED`。**不猜預設、不自動補 enabled** |
| 路徑 | 相對路徑一律以 **config 檔所在目錄**解析（**不看 cwd**）；支援含空白路徑 |
| 互動 | **不呼叫 `input()`、不讀 stdin**；無 `--force` / `--bypass` 覆寫參數 |
| 設備相依 | 由 **symbolic provider ID ＋ launcher allowlist** 解析；launcher 模組層**不 import** 任何 control / executor 模組 |

**Provider allowlist**

```python
PROVIDER_ALLOWLIST = {"phase6_first_live_field": "phase6_first_live_field_provider"}
```
🔴 config 只能給 **symbolic ID**；任意 dotted module（`os` / `subprocess` / …）、
檔案路徑、相對路徑一律 `CONFIG_INVALID`（exit 2）。
**刻意不提供** `--provider-module` / `--force-provider` / filesystem import。

**`--validate-only` = PASSIVE ONLY**

只驗證 config schema / provider ID allowlist / module importability /
provider interface shape / path resolution / static dependency declaration。
🔴 **不連 HMI・Meter・SSH、不 probe、不建立 control transport、不 arm、
不 pause / restore、不取得 authority、不送任何 command**，
且**不建立** ledger / artifact；回報 `field_side_effects = 0`。

`validate_provider_definition()`（純離線）與 `build_field_dependencies()`
（只在 execution path）**嚴格分離** —— config parse / launcher import /
`--help` / `--validate-only` 皆不產生任何 field side effect。

**Exit code（穩定契約）**

| code | 意義 |
|---|---|
| **0** | `HANDLED_TERMINAL_RESULT` —— runner 走完並產生可辨識終態（**不代表 PASS**；安全 ABORT / EXPIRED 也是 0） |
| **2** | `CONFIG_OR_INVOCATION_INVALID` |
| **3** | `REFUSED_ALREADY_CONSUMED_OR_NOT_ENABLED` |
| **4** | `RECOVERY_REQUIRES_MANUAL_ATTENTION` |
| **5** | `UNEXPECTED_LAUNCHER_FAILURE` |

🔴 **不得**「只有 PASS 才 exit 0」—— 否則 Task Scheduler 會把安全 ABORT 誤判成 crash。

### 8.8 Production / Candidate 參數

| 項目 | 值 | 狀態 |
|---|---|---|
| SSH connect | 5 | **FINAL** |
| `probe` | 5 | **FINAL** |
| `status` | 12 | **FINAL** |
| `loopcheck` | 26 | **FINAL** |
| **`pause`** | **12** | **FIELD_VALIDATION_CANDIDATE / NOT FINAL**；production default = `None` |
| **`restore`** | **12** | **FIELD_VALIDATION_CANDIDATE / NOT FINAL**；production default = `None` |
| idle power band | −3 ~ +1 kW | FINAL |
| pause settling | 120 s | FINAL |
| stable verify | 4 samples / 15 s / 45 s | FINAL |
| restore verify | 3 observations / 3 s / 6 s | FINAL |
| `retry_backoff_sec` / `service_health_timeout_sec` | 5 / 300 | FINAL |

> `pause` / `restore` 恆列於 `missing()` 與 `not_field_verified()`。
> Trial #1 實測 pause 2.068 s / restore 0.610 s，但 **n = 1，不足以升格 FINAL**。

---

## 9. CURRENT STATUS

> **本節是 README 唯一的 Current Status Authority。**

| Item | Current Status |
|---|---|
| Phase 1 ~ Phase 5 | **COMPLETE** |
| Phase 6 | **OFFLINE COMPLETE / LIVE HOLD** |
| Phase 6 Offline Application Layer | **COMPLETE / FROZEN** |
| Phase 6 Regression | **4972 / 4972 PASS ／ 51 files ／ 0 FAIL** |
| Phase 6.10 Offline | **COMPLETE / FROZEN** |
| Recovery Evidence Contract | **FIXED / VERIFIED** |
| Deployed STOP Semantics | **CONFIRMED**（2026-09-17，`sha256 964bb642…76ce` / 789 行） |
| Q1 ~ Q4 | **CLOSED** —— Q4 = `ZERO-UNTIL-STOP-CONFIRMED → THEN NO COMMAND` |
| Trial #2 Harness | **IMPLEMENTED / OFFLINE VERIFIED**（184 / 184） |
| `request_restore` one-attempt 對稱保護 | **FIXED / VERIFIED**（P2） |
| **Trial #2 Handoff Technical Result** | **PASS**（2026-09-18 08:54:25 ~ 09:02:27） |
| **DISCRIMINATIVE_PAUSE_VERIFIED** | **TRUE** —— `normal_loop_halted` ∧ `PauseVerifier = VERIFIED` |
| **Trial #2 Field Validation Closure** | **CLOSED WITH DOCUMENTED PROCEDURE DEVIATION**（2026-09-18 裁示） |
| Documented Procedure Deviation | `EXECUTION PROCEDURE DEVIATION = BASELINE EARLY-ACTIVE TERMINATION` —— 見 [§13](#13-known-limitations--deferred) |
| Historical Evidence Limitation | `SOURCE-SIDE TIMESTAMP UNAVAILABLE`／`SOC 2 % → 98 % ROOT CAUSE NOT DETERMINABLE` |
| **FIRST LIVE FIELD ENTRY POINT** | **PHASE 6.10 HANDOFF PATH ONLY**（P3 ＋ RULING 1） |
| `phase6_first_live_runner.py` | ROLE = **INTERNAL LIVE LEG / DISPATCHER**；DIRECT FIELD EXECUTION = **BLOCKED** |
| `pcs_auto_control_service.py --live-leg` | CLI field entry = **BLOCKED** |
| Phase 6 PCS STOP 計數 | **包含** live-leg cleanup STOP（RULING 2） |
| **FIRST LIVE Integration** | **OFFLINE VALIDATION COMPLETE**（171 / 171） |
| **FIRST LIVE EXECUTION READINESS** | **READY FOR AUTHORIZATION**（≠ FIELD AUTHORIZED、≠ EXECUTED） |
| **FIRST LIVE Integration** | **OFFLINE COMPLETE** |
| **FIRST LIVE Attempt #1**（2026-09-18） | **ABORTED_CURRENT_CONDITIONS** —— EXPECTED FAIL-CLOSED SAFETY ABORT，**不是** FAIL／CODE DEFECT／HANDOFF FAILURE |
| **FIRST LIVE Attempt #2**（2026-09-22） | **ABORTED_CURRENT_CONDITIONS** —— 終端結果不變，但 ROOT CAUSE = **PROVIDER_WIRING_DEFECT**（見下） |
| **Attempt #2 ROOT CAUSE** | **SAFE FAIL-CLOSED ABORT CAUSED BY PROVIDER WIRING DEFECT** —— **不是** 現場條件不符、**不是** policy 正常否決 |
| **Attempt #2 SOC 事後判定** | **SOC_ELIGIBLE_THROUGHOUT_GATE_WINDOW** —— 窗內 SOC 2 % → 6 %，任何時點都在可充電區間 |
| **BUG #1 canonical SOC band 斷線** | **CONFIRMED / FIXED**（2026-09-22，offline） |
| **BUG #2 classifier warm-up omission** | **CONFIRMED / FIXED**（2026-09-22，offline） |
| **HMI 401 session renewal（provider 路徑）** | **HMI_AUTH_RENEWAL_UNVERIFIED** —— provider / scheduled 路徑**沒有** re-login／token refresh；唯一既有實作在 6.9 `observe_window`（≤ 2 次） |
| **FIRST LIVE SUCCESSFUL FIELD LEG** | **NOT YET COMPLETED** |
| **FIRST LIVE Attempt #3** | **NOT AUTHORIZED**（本輪僅離線修正，未建立任何 Attempt #3 Task） |
| GATE A / GATE B（pre/post-handoff） | **IMPLEMENTED / OFFLINE VERIFIED**（324 / 324） |
| **FIRST LIVE ATTEMPT #2 READINESS** | **CONSUMED**（2026-09-22 已執行並 `ABORTED_CURRENT_CONDITIONS`） |
| HMI auth renewal | **VERIFIED**（105 / 105）—— 抽出並重用既有 6.9 bounded re-auth，bound = 2 |
| REAL_SOC_WIRING | **PASS**（2026-09-22 真實 HMI：SOC 90.0 → canonical `S_HIGH`） |
| REAL_CLASSIFIER_PRIME | **PASS**（真實 meter：`UNKNOWN → NEAR_ZERO`，12.0 s／3 samples，bounded budget 內） |
| **FIRST LIVE Attempt #3**（2026-09-23） | **ABORTED_PRE_PAUSE_PREFLIGHT** —— GATE A 首次 PASS，但 6.10 PRE_PAUSE_PREFLIGHT FAIL |
| **Attempt #3 ROOT CAUSE** | **PROVIDER_WIRING_DEFECT**（handoff observation vocabulary ／ `soc_max_pct` wiring） |
| **Attempt #3 原始 runner 分類** | `AUTHORITY_ACQUIRE_FAILED` —— **POSTMORTEM: MISCLASSIFIED TERMINAL RESULT**，不是真實 field root cause |
| `external_behaviour_known` | **CONFIGURED / VERIFIED** —— canonical = `phase6_remote_adapter.external_liveness()` 三態；`RUNNING`／`NOT_RUNNING` → True，`UNKNOWN` → False |
| **FIRST LIVE Attempt #4**（2026-09-24） | **ABORTED_AT_7_FIRST_LIVE** —— PRE_PAUSE 11/11、pause SENT、PauseVerifier PASS、GATE B PASS、restore PASS |
| **Attempt #4 ROOT CAUSE** | **DISPATCH AUTHORIZATION GATE NOT SCOPED FOR FIRST LIVE** |
| **Attempt #4 原始 runner 分類** | `ABORTED_POST_HANDOFF_REVALIDATION` —— **POSTMORTEM: MISCLASSIFIED**（GATE B 其實 PASS） |
| **DISPATCH_ENABLED 安全不變式** | **AT REST = False**；僅在已授權 one-shot 的 authority-acquired live-leg scope 內 temporarily True；finally 無條件 False |
| FULL LIVE-LEG READBACK | **VERIFIED** —— 離線完整驅動到 `LEG_COMPLETE`；成功終態分類缺口已修正 |
| FIRST LIVE MOCK SUCCESS | **PASS**（CHARGE 5.0 kW ×1 / DISCHARGE 0 / readback VERIFY_SUCCESS） |
| Scheduled live-leg authorization | **VERIFIED** —— 互動路徑用真人輸入、scheduled 路徑用 permit 產生的 structured authorization，**同一道 `confirm_live_leg` gate**；無 bypass、不依賴 stdin |
| **FIRST LIVE Attempt #5**（2026-09-25） | **MISSED / NOT EXECUTED** —— ROOT CAUSE = **HOST POWERED OFF AT SCHEDULED TRIGGER** |
| Attempt #5 執行證據 | ledger／artifact／launcher log **皆未建立** -> wrapper 未執行、runner 未啟動、`one_shot_runner_consumed = False` |
| Attempt #5 Task | evidence 保存後 **Disabled**；trigger 已過期，**不得重新啟用或沿用** |
| **新增正式前提** | `HOST_POWERED_ON_REQUIRED = True`（自 Attempt #6 起） |
| **FIRST LIVE Attempt #6**（2026-09-30） | **EXCEPTION_FAIL_CLOSED** —— failure_stage = **DISPATCHER_CONSTRUCTION** |
| **Attempt #6 ROOT CAUSE** | **DISPATCHER FACTORY SIGNATURE / PROVIDER WIRING DEFECT** |
| Attempt #6 執行階段 | Fresh Safety **PASS**／GATE A **PASS**／Handoff readiness **PASS**；`build_first_live_dispatcher()` 拋 `TypeError` |
| Attempt #6 Task | evidence 保存後 **Disabled** |
| **PRODUCTION PROVIDER CONSTRUCTION** | **VERIFIED**（57 / 57）—— 走真正的 `build_field_dependencies()` ＋ fake 依賴，0 field I/O |
| **validate-only 覆蓋範圍正式界定** | 只證明 static／structural；**不能**證明 production dependency graph 建得起來 |
| **FIRST LIVE Attempt #7**（2026-10-01） | **PAUSE_FAILED** —— ROOT CAUSE = **CROSS-ATTEMPT HANDOFF JOURNAL STATE LEAK** |
| Attempt #7 執行階段 | Fresh Safety **PASS**／GATE A **PASS**／Handoff readiness **PASS**／**PRE_PAUSE 11-11 PASS**；2_PAUSE 被 OnceGuard 擋下 |
| Attempt #7 實際送出 | pause `NOT_SENT`；**SSH pause verb = 0**；restore／authority／CHARGE 全 0 |
| Attempt #7 Task | evidence 保存後 **Disabled** |
| **HANDOFF JOURNAL SCOPE** | **ATTEMPT_SCOPED** —— `handoff_journal_<attempt_id>.jsonl`，path 由 attempt_id 決定性導出 |
| LEGACY SHARED JOURNAL | `handoff_journal.jsonl` **保留 immutable**（含 Attempt #4 證據），不再寫入 |
| **FIRST LIVE ATTEMPT #8** | **COMPLETED / CONSUMED / CLOSED** —— RESULT = `ABORTED_FIRST_LIVE`，SAFE FAIL-CLOSED |
| Attempt #8 root cause | **ENTRY-POINT / CONTROL-FLOW ORDERING GAP** —— step 7 的前置閘要求完整 permit（含 authority），但 authority 只能在 dispatcher 內取得 -> CONTROL-FLOW CYCLE |
| Attempt #8 修正 | **TWO-STAGE AUTHORIZATION**：STAGE A entry gate（14 項，不含 authority）／STAGE B full permit（16 項，未降低 safety） |
| **FIRST LIVE ATTEMPT #9** | **COMPLETED / CONSUMED / CLOSED** —— historical `attempt_result = DISPATCH_UNKNOWN`；postmortem 語意更正為 **dispatch 確定成功、abort 原因為 UNCERTAIN OWNERSHIP AFTER DISPATCH** |
| 🔴 **Phase 6 首次真實 5 kW CHARGE** | `activePowerSetPoint = -5`／http 200 operation successful／readback `VERIFY_SUCCESS`／observed_state `CHARGING` —— Phase 6 real cumulative CHARGE 由 0 變 **1** |
| Attempt #9 root cause | **POST-DISPATCH AUTHORITY BECAME UNKNOWN** —— `LEG_OWNED` 之後重新觀測得 `authority=UNKNOWN`，依既有契約中止且**不自動 STOP** |
| Attempt #9 safety | **SAFE HANDOFF RECOVERED WITH UNRESOLVED PHASE6 LIVE LEG**（不是單純的 SAFE FAIL-CLOSED） |
| 🔴 Attempt #9 evidence gap | authority UNKNOWN 的 **root subreason = NOT RECORDED / UNRESOLVED** —— 歷史 artifact 未保存 `AuthorityResult.reason`，**不得猜測、不得回填、不得改寫歷史** |
| **FIRST LIVE ATTEMPT #10** | **NOT AUTHORIZED** |
| FIRST LIVE Attempt #9 Task | `Phase6_Scheduled_FirstLive_Attempt9_20261006` = **Disabled**／NextRun N/A（2026-10-06 結案後停用；**未** Unregister，`LastRunTime` / `LastTaskResult` 未回寫） |
| Phase6 Task 全盤狀態 | **10 個 Task 全部 Disabled**，NextRunTime 全 N/A，future executable project Task = **0** |
| **FIRST LIVE ATTEMPT #10** | **HOLD —— PRE-FIELD FAIL**（2026-10-06）：STEP 6 shadow rehearsal ＋ STEP 8 external baseline 皆 FAIL |
| Attempt #10 blocker | ① **HMI `ConnectTimeout`**（192.168.128.110:8080，provider 建構即 Fail Closed）② **`auto_control.py` 未運行**（`EXTERNAL_NOT_RUNNING`，三天內第二次） |
| Attempt #10 Task | **NOT CREATED**（從未註冊；ledger／journal／artifact 亦皆無） |
| Attempt #10 config / wrapper / XML | 已建立但**未武裝**；`enabled=true`、`target_date=2026-10-07`。2026-10-07 00:31 之後任何誤觸一律 `WRONG_DATE` -> 0 command |
| FIRST LIVE Attempt #9 ID | `FIRST-LIVE-ATTEMPT9-20261006`（全新 config／per-attempt handoff journal／ledger／artifact／wrapper／Task XML／baseline evidence） |
| Attempt #9 PRE-FIELD 八步 | **全部 PASS**（2026-10-05 fresh）：regression 7022／rehearsal I／construction 60-60／entry-point 92-92／deadline 133-133／host／external baseline／legacy scan |
| Attempt #9 production parameters | 🔴 **不得**自行推定日期、不得沿用 Attempt #8、不得自行填 00:05–00:30、不得依當前日期產生 —— 必須由使用者明確指定 |
| **EXECUTION DEADLINE 契約** | `window_end` = **NO-NEW-RISK-ACTION DEADLINE**，**不是** process termination deadline。到期後禁止**開始** PAUSE／authority acquire／CHARGE／DISCHARGE／第二條 live leg；但 cleanup STOP／release／restore／RestoreVerifier **必須允許完成** |
| canonical deadline | `test/phase6_execution_deadline.py`（`ExecutionDeadline`）—— 9 個 production checkpoint；wall clock 建立、**monotonic** 推進；其他 module **不得**自行解析 `target_date` / `window_end` |
| deadline terminal results | `DEADLINE_BEFORE_FIELD_ACTION`／`DEADLINE_DURING_HANDOFF`／`DEADLINE_DURING_LIVE_CLEANUP` —— 三者皆為 **HANDLED_TERMINAL_RESULT**（exit 0） |
| **attempt_result vs live_leg_result** | **兩個不同維度，不得互相覆蓋**。`attempt_result` = 排程／deadline／handoff／safety contract 結果；`live_leg_result` = 實際 PCS 5 kW control leg 結果 |
| successful real 5 kW leg 計數 | 依 **`live_leg_result == SUCCESS`** 計數（CHARGE 送出 ＋ readback `VERIFY_SUCCESS` ＋ `LEG_COMPLETE`），**不看** `attempt_result`。即使 `attempt_result = DEADLINE_DURING_LIVE_CLEANUP`，真實成功的 leg 仍計 +1 |
| ATTEMPT RESULT PRECEDENCE | P0 safety-uncertain ＞ P1 control failure ＞ **P2 deadline** ＞ P3 pause NOT_SENT ＞ P4 real terminal step ＞ P5 derived ＞ P6 PASS —— **deadline 不得掩蓋 SEND_UNKNOWN／restore responsibility／未完成 cleanup／實際 control failure** |
| `ExecutionTimeLimit=PT1H` | **LAST-RESORT OS PROCESS LIMIT** —— ≠ 正常 FIRST LIVE 執行預算、≠ `window_end`、≠ safe cleanup 機制。正常流程**不得**依賴 `AllowHardTerminate` 完成安全收尾 |
| FIRST LIVE Attempt #8 Task | `Phase6_Scheduled_FirstLive_Attempt8_20261002` = **Disabled**／NextRun N/A（2026-10-02 結案後停用；**未** Unregister，Task XML 與全部證據保留） |
| Phase6 Task 全盤狀態 | 9 個 Task **全部 Disabled**，NextRunTime 全 N/A，future executable Task = **0** |
| FIRST LIVE Attempt #8 ID | `FIRST-LIVE-ATTEMPT8-20261002`（全新 config／per-attempt handoff journal／ledger／artifact／wrapper／Task XML） |
| Attempt #8 PRE-FIELD 六步 | 全部 PASS（regression 6797／rehearsal G／smoke 60-60／host／external baseline／legacy scan） —— 但六步**不含** entry-point ordering，因此未攔下本次缺陷 |
| **FIRST LIVE ATTEMPT #7** | **COMPLETED / CONSUMED / CLOSED** —— one-shot 授權已用盡；COMPLETED 指「排程 Attempt 跑完並結案」，**不代表** FIRST LIVE 目標達成 |
| Attempt #8 執行期產物 | ledger／**handoff journal**／artifact dir／launcher log **皆尚未建立**（由 runtime 於 trigger 後首次寫入時建立） |
| **OFFLINE vs PRODUCTION COVERAGE GAP 分類** | #6 = **PRODUCTION CONSTRUCTION PATH COVERAGE GAP**；#7 = **PERSISTENT / SHARED-STATE REALISM GAP**；#8 = **ENTRY-POINT / CONTROL-FLOW ORDERING GAP** —— **三者不是同一類 root cause** |
| **PRODUCTION ENTRY-POINT SMOKE** | `test_phase6_entry_point_ordering.py`（92 項）—— 新列為 **mandatory**；唯一進入點 `arm_and_run_handoff()`，禁止直接 `disp()` 或直接建構 `FirstLiveDispatcher` |
| **CONSTRUCTION_SMOKE_COMMAND** | `cd /d "D:\Crawler Sample"` ＋ `python test/test_phase6_provider_construction.py`（exit 0、57 / 57、0 field I/O） |
| **HOST_POWERED_ON_REQUIRED** | **True** —— fresh verify 只代表建立 Task 當下成立；trigger 前仍須人工保持主機開機＋Water 登入（可 Win+L；禁關機／重開／登出／切換／睡眠／休眠） |
| **PRE-FIELD 九步前置** | ① Full Phase 6 regression → ② validate-only rehearsal → ③ **construction smoke** → ④ **entry-point / control-flow smoke** → ⑤ **execution-deadline / safe-cleanup smoke** → ⑥ **production read-only shadow rehearsal**（**0 control side effect**） → ⑦ fresh host verify → ⑧ fresh external baseline → ⑨ fresh legacy Task scan；**九步全 PASS 才可建立 field Task**。③／④／⑤／⑥ 責任不同，**不得混稱** |
| SHADOW PASS 語意 | 只證明「到 PAUSE 之前的 production 路徑可用」，**不得**描述成 real field PASS |
| **FIRST LIVE successful real 5 kW leg** | **NOT YET COMPLETED —— 0 次** |
| Attempt #1 ~ #4 分類更正 | 舊 `classify_dispatch_outcome` 對「已達 LEG_COMPLETE 後 teardown 回 OBSERVE_ONLY」存在**錯誤分類風險**；但 #1~#4 **實際都沒有完成 5 kW live leg**，**不得**回頭改判為成功 |
| FIRST LIVE Attempt #4 Task | `Phase6_Scheduled_FirstLive_Attempt4_20260924`，Trigger 2026-09-24 00:05，One Time Only，**未手動 /Run** |
| FIRST LIVE Attempt #4 ID | `FIRST-LIVE-ATTEMPT4-20260924`（全新 ledger／artifact，未沿用 #2／#3／rehearsal） |
| FIRST LIVE Attempt #3 Task | `Phase6_Scheduled_FirstLive_Attempt3_20260923`，Trigger 2026-09-23 00:05，One Time Only，**未手動 /Run** |
| FIRST LIVE Attempt #3 ID | `FIRST-LIVE-ATTEMPT3-20260923`（全新 ledger／artifact 路徑，未沿用 #2 或 rehearsal） |
| SOC 90% → 2% 差異 | **EXPLAINED BY NORMAL CHARGE / DISCHARGE CYCLE**；`SOC_SOURCE_MISMATCH = NOT INDICATED`；**不是** Attempt #3 blocker |
| **SCHEDULED FIRST LIVE ONE-SHOT** | **OFFLINE IMPLEMENTATION / VALIDATION**（core 133 / 133 ＋ launcher 132 / 132）—— **不是** Unattended LIVE、**不是** recurring、**不是** auto retry |
| CLI launcher | **IMPLEMENTED / OFFLINE VERIFIED**（134 / 134） |
| **Official field provider** | **IMPLEMENTED / OFFLINE VERIFIED**（294 / 294）—— scratchpad 相依已移除；2026-09-22 wiring defect 已修正 |
| **HOST RUNTIME READINESS** | **PASS**（2026-09-21 唯讀確認：AC Sleep = Never、AC Hibernate = Never、CBS/WU pending reboot = False） |
| **SCHEDULED FIRST LIVE READINESS** | **READY FOR FIELD SCHEDULING**（≠ Task 已建立、≠ Field 已授權、≠ 已執行） |
| Unattended LIVE（6.10-C） | **HOLD** |
| Field Precondition | **POWERED ON / AVAILABLE** |
| Fresh STEP 2 Read-Only Precheck | **COMPLETE**（2026-09-17，20 / 20） |
| B2 Field Validation Trial #1 | **PARTIAL PASS**（2026-09-17） |
| Phase 6.10 Field Work | **IN VALIDATION / NOT COMPLETE** |
| Phase 6.10-B1.7 Network Identity | **ACCEPTED FOR CURRENT DEPLOYMENT**（**不是** PASS） |
| Phase 6.10-C LIVE | **NOT STARTED** |
| Trial #2 | **HOLD** |
| FIRST LIVE | **HOLD** |

---

## 10. CURRENT SAFETY / COMMAND COUNTS

| 項目 | 值 |
|---|---|
| `DISPATCH_ENABLED` | **False** |
| `MODE` | **DRY_RUN** |
| RemoteSenders armed | **False** |
| `DEPLOYED_GUARD_VARIANT` | **B2** |
| `NETWORK_IDENTITY_STABILITY` | ACCEPTED / USER CONFIRMED |
| DHCP Reservation | NOT VERIFIED / NON-BLOCKING BY USER DECISION |

**Field command counts（累計）**

| 指令 | 次數 |
|---|---|
| Phase 6 real CHARGE / DISCHARGE / PCS STOP | **0 / 0 / 0** |
| External B2 `pause` / `restore` | **3 / 3**（Trial #1 各 1、Trial #2 各 1、Attempt #4 各 1） |

---

## 11. CURRENT BLOCKERS

> 只列**目前仍未解決**的項目。已修復者見 [§9](#9-current-status)。

1. **`pause` / `restore` timeout = FIELD_VALIDATION_CANDIDATE 12.0 / NOT FINAL**
   n = 2 field samples —— Trial #1（pause 2.068 s / restore 0.610 s）、
   Trial #2（pause 0.573 s / restore 0.573 s）。
   production default 維持 `None`；**未**升格 FINAL。

2. **Trial #2 為單次觀測（n = 1 discriminative run）**
   pause 恰好落在 external `DISCHARGING` 轉態邊緣，
   無法區分「pause 造成停止」與「external 自行結束放電」的**時間重疊部分**；
   鑑別力來自 `loop marks 停止` 而非功率變化本身。

3. **FIRST LIVE = HOLD**
   整合已離線驗證（`READY FOR AUTHORIZATION`），但**未獲 field execution 授權**。

4. **Scheduled FIRST LIVE 需要的現場 baseline 尚未重建**
   `auto_control.py` 目前為 **MANUALLY STOPPED BY OPERATOR**（使用者 2026-09-21 說明）。
   執行前需由現場以正常方式重新啟動並輸入 `start`，再 fresh verify
   `screen alive` / `process alive` / `process identity OK` / `loop marks advancing`，
   並讓 controller **依真實 OFF_PEAK 自然進入 CHARGE**
   —— 🔴 **不得**人工啟動 PCS CHARGE 作為 baseline。

5. **Phase 6.10-C LIVE = NOT STARTED**
   無人值守正式部署尚未開始。

> Trial #2 **不再列為 blocker** —— 已 `CLOSED WITH DOCUMENTED PROCEDURE DEVIATION`。
> `SOURCE-SIDE TIMESTAMP UNAVAILABLE` **不是** FIRST LIVE blocker：
> 它是現有端點的證據限制，**不是** handoff failure，列於
> [§13 KNOWN FIELD EVIDENCE LIMITATION](#13-known-limitations--deferred)。

> 🔴 **`pause` 用語規範**：**不得**寫成 `immediate command silence`。
> 正確描述為 **`ZERO-UNTIL-STOP-CONFIRMED → THEN COMMAND SILENCE`** ——
> 收斂期間會重複送出 0 kW setpoint（真實 external-controller command effect）。
> 完整 deployed evidence 見
> [`../docs/Phase6_10_Field_Validation.md`](../docs/Phase6_10_Field_Validation.md)。

---

## 12. NEXT STEP

1. ~~取得 deployed source、閉合 Q1~Q4~~ —— **COMPLETE（2026-09-17）**
   ~~Trial #2 field execution~~ —— **PASS（2026-09-18）**
2. ~~Trial #2 harness 實作與離線驗證~~ —— **COMPLETE**
   `phase6_trial2_harness.py` ＋ `test_phase6_trial2_harness.py`（184 / 184）。
   執行順序與 evidence 設計見
   [`../docs/Phase6_10_Field_Validation.md`](../docs/Phase6_10_Field_Validation.md)。
3. 🔴 **Attempt #2 已於 2026-09-22 執行並 `ABORTED_CURRENT_CONDITIONS`。**
   事後判定 ROOT CAUSE = **PROVIDER_WIRING_DEFECT**（provider 取不到 canonical
   SOC band、且未依既有 classifier contract 暖機），**不是**現場條件不符。
   兩個缺陷已於同日離線修正並加測（294 / 294）。
   **Attempt #3 = NOT AUTHORIZED**，需使用者重新明確授權。
4. **FIRST LIVE Attempt #3** —— 只在新的 fresh observation 顯示
   **formal natural decision = CHARGE**，且 TOU / SOC / Safety Gate /
   Authority / communication / fault / alarm 全部符合既有正式 policy 時
   才有執行意義。🔴 **不自動 retry、不 polling 後自行送出、不建立 scheduled auto retry**；
   必須由使用者重新明確授權**單次** Controlled Field Attempt，
   且一律從 STEP 0 / Fresh Preflight 重新開始，**不得沿用**本次的
   preflight / decision / Authority / loop evidence。

> 🔒 **SCOPE FREEZE（2026-09-18 起）**：下一次 FIRST LIVE 之前，只處理會阻止
> FIRST LIVE 的 **P0 safety issue / ownership race / restore responsibility issue /
> command resend issue / false evidence issue**。
> DEFER：README 美化、架構重構、dead path cleanup、timeout 最佳化、
> 額外 audit 欄位、新功能、非 blocker refactor。
4. 即使 Trial #2 PASS，**也不自動進 FIRST LIVE**。
5. FIRST LIVE 需另行裁示；其後才是 Phase 6.10-C LIVE 部署。

> **任何 Field 階段都需要新的明確授權。**

---

## 13. KNOWN LIMITATIONS / DEFERRED

| 項目 | 狀態 |
|---|---|
| `min_switch_interval_sec` | **DEFERRED**（Layer 2 未啟用；Layer 1 已無條件生效） |
| `meter_stale_grace_sec` | **DEFERRED**（採最保守解釋） |
| `REAL_REPLAY_FIXTURE` | **DEFERRED**（未讀任何現場 CSV / JSON / JSONL） |
| `ROTATION_POLICY` | **DEFERRED**（audit 檔約 4 MB/天，需自行留意磁碟） |
| `RETENTION_POLICY` | **DEFERRED** |
| Process Lifecycle Enum | **NOT CURRENTLY REQUIRED** |
| `ObserveRecord.meter_state` | 恆為 `None`（來源／schema 不一致）；**status / diagnostic only**，對 Decision / Authority / Safety / Audit **無影響** |
| Audit final failed-write | crash 前最後一次寫入失敗，跨 restart **不保證**留下 durable seq gap（無 sidecar watermark） |
| LIVE audit-before-control PRE/POST protocol | **NOT IMPLEMENTED / NOT REQUIRED** |
| `Orchestrator.verify_restore()` 依賴 `running` | **NON-BLOCKING DEAD-PATH**（正式 `HandoffRunner` 走 `RestoreVerifier`，不經此路徑） |
| External deployed STOP semantics | **CONFIRMED / CLOSED**（2026-09-17）；`pause` = `ZERO-UNTIL-STOP-CONFIRMED → THEN COMMAND SILENCE`，收斂期間會重複送 0 kW |
| **DOCUMENTED PROCEDURE DEVIATION**（2026-09-18 Trial #2） | `EXECUTION PROCEDURE DEVIATION = BASELINE EARLY-ACTIVE TERMINATION`。授權文字要求**完整 600 sec**，實際約 **365 sec**；原因為現有 harness 在 `ACTIVE_BASELINE_ESTABLISHED` 後 early break。**不得回溯刪除，也不得改寫成「完全符合程序」。** 未來政策 `MAX_OBSERVATION_WINDOW` **不**回頭消除本次偏差 |
| **KNOWN FIELD EVIDENCE LIMITATION** —— source-side timestamp | `EP_MAINCTL` 的 `createTime` / `collectTime` 實測皆為**空字串** → `SOURCE_TIME_UNAVAILABLE`。這是**設備資料來源本身的限制，不是 harness 漏存**。`SOURCE-SIDE FRESHNESS = NOT DETERMINABLE`。🔴 **不得**寫成 `SOURCE_COHERENCE_CONFIRMED` |
| **HISTORICAL EVIDENCE LIMITATION** —— SOC 2 % → 98 % | 已知：相同 endpoint、相同 `rackSoc` mapping、無 device selector、相同 external controller process（pid 44728）。未知：14 小時期間 SOC 變化的實際原因。🔴 **不得推測** overnight charging 或任何其他原因，除非未來有直接 evidence |
| Hard-coded credential risk | 於本機 controller 副本中觀察到。**SECURITY ISSUE / SEPARATE TRACK**，不在 Phase 6.10 scope；本專案文件不記錄任何 secret 內容 |

---

## 14. Regression

| 範圍 | 基準 | 執行方式 |
|---|---|---|
| Phase 2~5 | **1802 / 1802 PASS**，0 FAIL / 0 SKIP | `python run_phase3_regression.py` |
| Phase 6 | **6797 / 6797 PASS**，**63 檔**，0 FAIL | 逐檔執行 `test_phase6*.py` |
| Phase 6.10 dedicated | **1173 / 1173 PASS**，**11 檔**，0 FAIL | 逐檔執行 `test_phase6_610*.py` |

主要 dedicated 基準：

| 測試 | 基準 |
|---|---|
| `test_phase6_decision_matrix_e2e.py` | 174 / 174 |
| `test_phase6_decision_longrun.py` | 146 / 146 |
| `test_phase6_runtime_arbiter_longrun.py` | 100 / 100 |
| `test_phase6_gap3_tariff_wiring.py` | 50 / 50 |
| `test_phase6_gap12_service_scheduling.py` | 63 / 63 |
| `test_phase6_gap4_durable_audit.py` | 90 / 90 |
| `test_phase6_610d_recovery_contract.py` | 47 / 47 |
| `test_phase6_trial2_harness.py` | 257 / 257 |
| `test_phase6_first_live_handoff.py` | 327 / 327 |
| `test_phase6_scheduled_first_live.py` | 133 / 133 |
| `test_phase6_scheduled_launcher.py` | 134 / 134 |
| `test_phase6_first_live_field_provider.py` | 294 / 294 |
| `test_phase6_hmi_auth_renewal.py` | 105 / 105 |
| `test_phase6_attempt3_postmortem.py` | 133 / 133 |
| `test_phase6_attempt4_postmortem.py` | 108 / 108 |
| `test_phase6_scheduled_live_leg_auth.py` | 106 / 106 |
| `test_phase6_live_leg_readback.py` | 94 / 94 |
| `test_phase6_provider_construction.py` | 60 / 60 |
| `test_phase6_journal_scope.py` | 66 / 66 |
| `test_phase6_69a_cli_entry.py` | 128 / 128 |

> `run_phase3_regression.py` **不含** Phase 6；Phase 6 沒有專屬 runner 入口。
> ⚠️ Windows 主控台預設 cp950，部分測試含 `−` / emoji 會 `UnicodeEncodeError`。
> 逐檔執行前請設 `PYTHONIOENCODING=utf-8` —— 這是**輸出編碼**問題，
> 不是測試失敗。
> ⚠️ **`run_all.py` 不是 regression 入口**（見 [§5](#5-快速使用)）。

---

## 15. 文件索引

| 文件 | 內容 |
|---|---|
| [`../docs/Operations_Guide.md`](../docs/Operations_Guide.md) | **部署 / 維運手冊** |
| [`../tools/README.md`](../tools/README.md) | NSSM 版本、授權、SHA-256；**Service action 權威來源** |
| [`../docs/Phase3_Closure_Report.md`](../docs/Phase3_Closure_Report.md) | Phase 3 自動生命週期收尾 |
| [`../docs/Phase4_Closure_Report.md`](../docs/Phase4_Closure_Report.md) | Phase 4 收尾：架構、Ownership、Recovery |
| [`../docs/Phase4.6_Closure_Report.md`](../docs/Phase4.6_Closure_Report.md) | Windows Service 包裝與 NSSM 驗證 |
| [`../docs/Phase5_Validation_Plan.md`](../docs/Phase5_Validation_Plan.md) | Phase 5 驗證計畫與驗收基準 |
| [`../docs/Phase5_Closure_Report.md`](../docs/Phase5_Closure_Report.md) | Phase 5 收尾：Soak / Stress / Release |
| [`../docs/Phase6_Historical_Development.md`](../docs/Phase6_Historical_Development.md) | **Phase 6 開發歷史**：D.x 各階段、TOU / 年度行事曆推導、參數 derivation、FIRST LIVE 歷史、架構決策 |
| [`../docs/Phase6_10_Handoff_History.md`](../docs/Phase6_10_Handoff_History.md) | **Phase 6.10 開發歷史**：Core / A / A1 / B1 / B1.5 / B1.6 / B1.7 / B2 / C / C1~C4、舊 state 與舊 blocker |
| [`../docs/Phase6_10_Field_Validation.md`](../docs/Phase6_10_Field_Validation.md) | **實機驗證紀錄**：Fresh STEP 2、Trial #1 完整證據、deployed source 調查、Q1–Q4、Trial #2 provisional design |

Field evidence 原始檔：`output/phase6_field_validation/`、`output/phase6_live_charge/`、
`output/phase6_ssh_timing/`。

---

## 16. 注意事項

- **`.env` 不可 commit**（已由 `.gitignore` 排除）；文件內不得出現任何 secret。
- **`tools\nssm.exe` 為 runtime dependency**，不可刪除／改名／搬移。
- Service 運行時 **Dashboard 為 Observer**，不會寫入報告。
- **不要同時啟動第二個 Monitor writer** —— Ownership 會擋下，但應避免。
- **Phase 6 預設不允許實機控制**：`DISPATCH_ENABLED = False`、`MODE = DRY_RUN`。
- 任一條件無法確認一律 **Fail Closed**；不確定不等於安全。
- Durable Audit 目前**無 rotation / retention**，檔案持續成長，需自行留意磁碟。
