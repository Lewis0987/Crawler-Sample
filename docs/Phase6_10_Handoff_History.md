# Phase 6.10 — Unattended Ownership Handoff（開發歷史）

> **Historical evidence only.**
> Current project status is authoritative in [`test/README.md`](../test/README.md).
> 本文件內容為**當時**的開發紀錄與 baseline，可能已被後續裁示
> **HISTORICAL / SUPERSEDED / CORRECTED**；不得當作目前狀態引用。

涵蓋：6.10 Core、A / A1、B1 / B1.5 / B1.6 / B1.7 / B2、
C / C1 / C2 / C3 / C4，以及當時的 state、blocker 與 handoff 設計歷史。

⚠️ 本文件內多處 `pause / restore = 0 / 0`、`DEPLOYED_GUARD_VARIANT = B1`、
`STRUCTURAL_CANDIDATE`、`NOT STARTED` 等敘述皆為**當時**狀態，
**已被取代**。目前狀態一律以 `test/README.md` 為準。


> ⚠️ **SUPERSEDED BY 2026-09-17 DEPLOYED SOURCE INSPECTION** ——
> 本文件中關於 `auto_control.py` 的 `DEPLOYED_SOURCE_UNCONFIRMED` /
> `Q1~Q4 UNKNOWN` 等敘述皆為**當時**狀態，已由直接讀取
> `192.168.70.201` deployed source 取代。
> 最新結論見 [`Phase6_10_Field_Validation.md`](Phase6_10_Field_Validation.md)。
> **歷史內容刻意保留原貌，未改寫。**

---

#### 11. Phase 6.10 — Unattended Ownership Handoff

##### 11.1 目標

讓系統未來能在**無人工操作**下完成一次完整交接：

```
External Controller → safe pause → ownership release → Phase 6 takeover
→ control → Phase 6 release → External Controller restore
```

四項核心原則：

- **不允許雙重 controller** —— 任何時刻只能有一方持有控制權
- **Fail Closed** —— 任一 Gate 不成立即停止前進，不猜、不續跑
- **Crash 後保留 restore responsibility** —— 重開機不得讓恢復責任消失
- **所有 handoff 留下 durable audit evidence**

##### 11.2 State Machine

```
IDLE → PREFLIGHT → PAUSE_REQUESTED → PAUSE_VERIFYING → EXTERNAL_PAUSED
     → PHASE6_ACTIVE → PHASE6_RELEASING → PHASE6_RELEASED
     → RESTORE_REQUESTED → RESTORE_VERIFYING → RESTORED → COMPLETE
```

失敗／Critical 狀態：

```
ABORTED_PREFLIGHT              尚未動任何東西，無恢復責任
ABORTED_PAUSE_FAILED           pause 未生效，external 仍在跑
RESTORE_FAILED_CRITICAL        🔴 場站失去套利，需人工介入
OWNERSHIP_CONFLICT_CRITICAL    🔴 雙方同時持有
```

轉移採**白名單**，非法轉移直接拋錯。`EXTERNAL_PAUSED` 不得直接跳 `COMPLETE` ——
必須經過歸還。

##### 11.3 Ownership Model

```
EXTERNAL / NEITHER / PHASE6 / UNKNOWN / BOTH
```

🔴 **`BOTH` 不是正常 operational state，而是 INVARIANT VIOLATION / CRITICAL。**
它不出現在任何狀態的可接受集合中，一旦偵測到即寫入稽核並轉 critical。
任一側無法判定 → `UNKNOWN`（Fail Closed），不猜測。

##### 11.4 Phase 6.10 Core — COMPLETE

已完成並離線驗證：State Machine、Crash Recovery、Watchdog、Idempotency、
append-only JSONL Audit Log、DRY_RUN / OBSERVE / ARMED 三段模式架構。

幾項語意值得記錄：

- **意圖先寫、結果後寫** —— crash 落在中間時必須**以觀測驗證**，不得盲目重送
- **Watchdog 逾時不會直接 restore** —— Phase 6 仍在 dispatch 時強行恢復
  external 會造成雙方同時控制，因此逾時只會要求「先收斂，再歸還」
- **恢復責任由 8 個狀態承載**，並以 durable journal + `recover()` 跨重開機保留

```
DISPATCH_ENABLED = False       MODE = DRY_RUN
```

##### 11.5 Remote Adapter — Phase 6.10-A COMPLETE

remote adapter 提供三個注入點：`probe` / `pause_sender` / `restore_sender`。
pause / restore sender **介面已備妥，但 production control 尚未啟用**
（預設 `armed=False`，未注入 runner 即結構上送不出）。

🔴 **`external_quiescent` 不得宣稱直接觀測到 `auto_control.py` 的
`running == False`** —— 那是行程內部變數，外部讀不到。只能以外部可觀測行為
（PCS 狀態、功率、Authority、通訊、告警、狀態穩定性）跨多筆樣本驗證。

##### 11.6 Pause Verification — Phase 6.10-A1 COMPLETE

兩階段：

```
SETTLING        允許正常收斂，**不判失敗**：
                  CHARGING / DISCHARGING → STANDBY / STOPPED
                  Authority EXTERNAL_OR_UNKNOWN → IDLE
                  Power active → idle band
                逾時仍未出現 candidate-idle → SETTLING_TIMEOUT

STABLE_VERIFY   首次出現完整 candidate-idle 後才開始；
                連續 fresh samples 全部成立才 external_quiescent = True
```

PCS 狀態穩定性**只在 STABLE_VERIFY window 內要求** —— SETTLING 期間的
`CHARGING → STANDBY` 是正常收斂，不得因此判定 pause 失敗。

Restore 驗證同樣兩層：runtime health（ssh / screen / pid / identity）＋
loop activity（時間戳前進）。**不要求 PCS 一定出力**；證據不足一律
`NEEDS_ADDITIONAL_PROBE`，不得假裝 PASS。

##### 11.7 已核准 handoff 參數

| 參數 | 值 |
|---|---|
| `idle_power_lower_kw` | −3.0 |
| `idle_power_upper_kw` | +1.0 |
| `pause_settling_timeout_sec` | 120.0 |
| `stable_verify_min_samples` | 4 |
| `stable_verify_interval_sec` | 15.0 |
| `stable_verify_min_span_sec` | 45.0 |
| `restore_loop_min_observations` | 3 |
| `restore_loop_interval_sec` | 3.0 |
| `restore_loop_min_span_sec` | 6.0 |

span 一律等於 `(samples − 1) × interval`（4@15s → t=0/15/30/45 → 45 s；
3@3s → t=0/3/6 → 6 s），並由 `validate()` 檢查一致性以防 off-by-one。

🔴 **參數已寫入 ≠ 實機 unattended control 已啟用。** 仍維持
`DISPATCH_ENABLED = False`、`MODE = DRY_RUN`。

##### 11.8 Remote Guard — Phase 6.10-B1 COMPLETE

```
dedicated key   phase6_firstlive_ed25519（ED25519，專用）
來源限制        from="192.168.128.234"
forced command  /home/etica/ems/phase6_remote_guard.sh
```

目前 deployed allowlist：

```
ENABLED   probe | loopcheck | status
REFUSED   pause | restore | 任意 shell | kill | screen 任意指令
```

`authorized_keys` 中該 public key **恰好 1 條** forced-command entry ——
必須 REPLACE 而非 APPEND，否則舊的無 forced-command 條目仍在，allowlist
就完全失去意義。

guard 本身：`set -euo pipefail`、PATH 與環境淨化、絕對路徑、每個動作有 timeout、
明確 exit code、精確比對（非前綴）、不使用 eval、不把原始 command 交給 `sh -c`、
screen 以 **session name** 定位（不寫死 PID）且要求恰好 1 個、hardcopy 用
`mktemp` + `trap` 清理。

##### 11.9 FIRST LIVE Handoff Plan

```
 1 PRE_PAUSE_PREFLIGHT
 2 PAUSE                    → RESTORE RESPONSIBILITY = TRUE
 3 SETTLING                 ≤ 120 s
 4 STABLE_VERIFY            4 samples @ 15 s，span ≥ 45 s
 5 ownership = NEITHER
 6 POST_PAUSE_FRESH_PRECHECK
 7 FIRST LIVE CHARGE 5.0 kW
 8 VERIFY                   ReadBack / LastControl / Phase 6 Authority
 9 PHASE6 RELEASE           → idle verified → ownership = NEITHER
10 RESTORE external
11 RESTORE_VERIFY           3 observations @ 3 s，span ≥ 6 s
12 COMPLETE
```

🔴 **`PRE_PAUSE_PREFLIGHT` 不要求 `Authority = IDLE`、`PCS idle`、
`No external controller`** —— 暫停之前，external controller 本來就可能正在
正常控制，那是預期狀態而非 failure。這三項**只有 `POST_PAUSE_FRESH_PRECHECK`
才必須 PASS**，且不得沿用 Step 1 的資料。

🔴 **RESTORE 完成後不要求 ownership 一定為 `EXTERNAL`。** 若 external policy
此刻正確選擇 idle（`ownership = NEITHER`），仍可 `COMPLETE`，前提是：
Phase 6 ownership released、external runtime healthy、external loop resumed、
`ownership != BOTH`、no critical condition。

##### 11.10 Network Identity Blocker — Phase 6.10-B1.6 COMPLETE / BLOCKER CONFIRMED

```
Wi-Fi MAC     04-EC-D8-6A-46-4A
IPv4          192.168.128.234（PrefixOrigin = Dhcp）
DHCP Server   192.168.128.16
DHCP Reservation = NOT CONFIRMED
```

```
UNATTENDED_NETWORK_IDENTITY_STABILITY = NOT VERIFIED
DHCP Reservation                      = NOT CONFIRMED
Phase 6.10-B1.7                       = DEFERRED / NOT VERIFIED
```

🔴 **`NOT VERIFIED` 不是 `PASS`。** B1.7 的欄位驗證（受控 reconnect / DHCP renew
後重新確認 IP 與 guard）**尚未執行**，不得以「歷史 renew 都拿到 .234」推論為已驗證。

⚠️ 正確描述：**DHCP 會週期性 renew，但因 reservation 尚未確認，無法保證
reconnect / DHCP restart / lease reassignment 之後仍取得 `.234`。**
（不可寫成「12 小時租約一定每 12 小時換 IP」。）

風險路徑：external 已 pause → Windows IP 改變 → `from="192.168.128.234"`
拒絕 SSH → restore 送不出去 → `RESTORE_FAILED_CRITICAL`。

🔴 **不得**以 `from="192.168.128.0/24"` 作為解法 —— 那只是擴大 dedicated key
的來源授權範圍，並未建立 stable network identity。unattended production 要的是
**stable identity**，不是 **broader authorization scope**。

需 IT 於 `192.168.128.16` 建立 reservation：MAC `04-EC-D8-6A-46-4A`
→ IPv4 `192.168.128.234`（**是 DHCP Reservation，不是 Windows 端 Static IP**）。

##### 11.11 Current Gate

| 階段 | 狀態 |
|---|---|
| Phase 6.9 | FIRST LIVE RETRY HOLD |
| Phase 6.10 Core | COMPLETE |
| Phase 6.10-A | COMPLETE |
| Phase 6.10-A1 | COMPLETE |
| Phase 6.10-B1 | COMPLETE |
| Phase 6.10-B1.5 | COMPLETE |
| Phase 6.10-B1.6 | COMPLETE / BLOCKER CONFIRMED |
| Phase 6.10-B1.7 | **DEFERRED / NOT VERIFIED** |
| Phase 6.10-B2 OFFLINE | **COMPLETE** |
| Phase 6.10-B2 LIVE | **NOT STARTED / BLOCKED** |
| Phase 6.10-C OFFLINE | **COMPLETE** |
| Phase 6.10-C LIVE DEPLOYMENT | **NOT STARTED / BLOCKED** |

```
Live blockers
  NETWORK_IDENTITY_STABILITY   NOT VERIFIED
  DHCP Reservation             NOT CONFIRMED
  Remote deployed guard        B1（pause / restore = REFUSED）
  retry_backoff_sec            UNRESOLVED
  service_health_timeout_sec   UNRESOLVED
  FIRST LIVE                   HOLD
```

FIRST LIVE / unattended live 之前，**必須**重新補做 B1.7，或以其他經驗證的
stable network identity 方案解除此 blocker。

B1.7 通過後才進 B2；即使進 B2，仍先維持 `DISPATCH_ENABLED = False` /
`MODE = DRY_RUN`，先驗證 pause / restore 安全成立，再做 Controlled FIRST LIVE
5 kW，之後才考慮 Phase 6.10-C Unattended Service，最後才可能
`MODE = ARMED` / `DISPATCH_ENABLED = True`。

##### 11.13 Phase 6.10-B2 — OFFLINE DEVELOPMENT / TEST ONLY

B2 的程式與離線測試已完成；**實機 pause / restore 未授權，guard 亦未部署**。

**HandoffRunner** —— 依 §11.9 的 12 步驅動整個交接，全部相依由注入取得：

```
1 PRE_PAUSE_PREFLIGHT   11 項；刻意**不含** authority_idle / pcs_idle /
                        no_external_controller（以 PRE_PAUSE_MUST_NOT_REQUIRE
                        常數固定，回歸直接驗）
2 PAUSE                 送出後 restore_responsibility = True
3+4 SETTLING/STABLE_VERIFY  由 PauseVerifier 以已核准參數驅動
5 OWNERSHIP = NEITHER
6 POST_PAUSE_PRECHECK   **這一階段才**要求那三項，且不得沿用 Step 1
7 FIRST LIVE            受 DISPATCH_ENABLED 擋下（目前恆為 False）
8~9 VERIFY / RELEASE
10~12 RESTORE / RESTORE_VERIFY / COMPLETE
```

🔴 **不存在「漏歸還」的路徑** —— 只要 Step 2 曾成功，之後任一失敗都由統一的
`_bail()` 導向歸還流程。離線測試涵蓋 Step 6 失敗、STABLE_VERIFY 期間出現
external evidence、probe 異常三種情境，三者皆確認仍送出 restore。

🔴 **restore 失敗（送不出或 loop 未恢復）→ `RESTORE_FAILED_CRITICAL`，
且恢復責任不解除。**

**Remote Guard B2** —— 已實作 `pause` / `restore` 兩個動詞，沿用 B1 全部強化，
且兩者**皆必須先通過** `require_single_screen` 與 `require_identity`
（B1 的 `probe` 刻意不強制，因為 probe 本就是用來診斷當下狀態；但控制動詞不同 ——
對身分不確定的目標送控制指令不可接受）。

```
DEPLOYED_GUARD_VARIANT = "B1"    ← 遠端實際部署仍是唯讀動詞版本
```

模組中存在 `REMOTE_GUARD_B2_SH` **不代表已部署**。部署需另行授權，且必須
REPLACE `authorized_keys` 的既有條目。

##### 11.14 Phase 6.10-C — Unattended Service

```
Phase 6.10-C OFFLINE           COMPLETE
Phase 6.10-C LIVE DEPLOYMENT   NOT STARTED / BLOCKED
SERVICE_MAIN_LOOP              IMPLEMENTED / OFFLINE VERIFIED
```

**主迴圈**

```
BOOT → RECOVER → STARTUP GATES → OBSERVE → EVALUATE ELIGIBILITY
     → HANDOFF RUNNER → AUDIT → LOOP
```

只有 `recovery = CLEAN` 才可進入正常 observation loop。

**Startup Recovery**

```
CLEAN            → 允許正常 loop
PENDING_RESTORE  → 禁止新 handoff，承接既有 restore responsibility
CRITICAL         → 拒絕 live operation
UNKNOWN          → Fail Closed
```

🔴 Windows reboot / Service restart **不得**把 ownership 或 restore
responsibility 重設為 IDLE / NONE。

**Live Gate（10 項，任一不成立即 REFUSED）**

```
MODE == ARMED                       DISPATCH_ENABLED == True
NETWORK_IDENTITY_STABILITY == PASS  REMOTE_GUARD_VARIANT == B2
pause capability == PASS            restore capability == PASS
recovery == CLEAN                   local data fresh
remote probe fresh                  FIRST LIVE prerequisite satisfied
```

現場實況：

```
DISPATCH_ENABLED           False
MODE                       DRY_RUN
NETWORK_IDENTITY_STABILITY NOT VERIFIED
DEPLOYED_GUARD_VARIANT     B1
pause_capable              False
restore_capable            False
FIRST LIVE                 HOLD
→ live_handoff_allowed 必定 False
```

⚠️ `remote_pause_capable()` / `remote_restore_capable()` 依**實際部署的 guard
版本**判定 —— 原始碼中存在 `REMOTE_GUARD_B2_SH` 不代表現場具備 B2 能力。

**Single Instance**

只允許單一 unattended orchestrator。判斷**不只使用 PID**，而是
`pid` + `boot_id` + `cmdline` 三者確認，並處理四種情境：

```
stale PID              前行程已死          → 可接手
PID reuse              PID 活著但 cmdline 不符 → 視為 stale，可接手
peer liveness unknown  死活不可知          → 拒絕啟動（不假設已死）
corrupt lock           lock 檔損毀         → 拒絕啟動
```

第二 instance 不得啟動，且**不寫入 `SERVICE_START`**。
production 應改用本專案既有的 Windows Named Mutex 機制（可注入）。

**Durable Journal**

append-only JSONL，必要事件八種：

```
SERVICE_START / SERVICE_SHUTDOWN / RECOVERY_RESULT / ELIGIBILITY_DECISION
HANDOFF_BEGIN / PHASE6_TAKEOVER / PHASE6_RELEASE / CRITICAL_FAILURE
```

加上既有的 pause / restore `INTENT` / `OUTCOME`。restart 不清除、seq 單調遞增、
partial tail 可容忍，且**不得因資料不足而猜測 ownership**。

**Restore Responsibility**

只要 external pause **可能已發生**，責任就必須保留到 `RESTORED`。
exception / process crash / Windows reboot / service restart / SSH failure
都不得讓責任消失。

🔴 **本輪修正的真實缺陷**

crash 若發生在「PAUSE INTENT 已 durable」但「STATE 尚未寫入」之間，
舊 `recover()` 的 `last_state()` 會回 `None` → **可能誤判 CLEAN**，
服務照常啟動，但 external 其實可能已被暫停。

已新增 `_has_unresolved_pause()`：只要存在 PAUSE `INTENT` 且其後沒有成功的
RESTORE `OUTCOME`，即視為可能仍背負責任，再配合 remote probe 判為
`PENDING_RESTORE` / `CLEAN` / `UNKNOWN` —— **不得直接假設 CLEAN**。
此修正已由 crash tests 驗證（八個切點）。

**Graceful Shutdown**

```
無 responsibility                        → CLEAN
Phase6 active → release → verify idle
  → restore external → verify loop resumed → CLEAN
restore 送不出 / loop 未恢復              → RESTORE_FAILED_CRITICAL
```

🔴 **不得記成 clean shutdown。** DRY_RUN 下若 journal 顯示存在真實 restore
responsibility，因禁止實機 sender，**也不得假裝已成功 restore** ——
同樣判為 CRITICAL。

**Crash Recovery**

八個切點皆驗證：pause INTENT 後 / pause success 後 / SETTLING /
STABLE_VERIFY / PHASE6_ACTIVE / PHASE6_RELEASED / restore INTENT 後 /
restore outcome 未落盤。每一個都 boot REFUSED、承接責任，且
**pause / restore sender 呼叫數皆為 0（不盲目重送）**。

**DRY_RUN E2E**

```
boot READY → 5 loop ticks → shutdown CLEAN
pause sender = 0 / restore sender = 0 / dispatcher = 0
```

即使 `eligibility = True`，只要 `live_handoff_allowed = False` 仍為 `NO_HANDOFF`
—— 兩層判定彼此獨立。

**Scheduling**

無硬編碼 `00:05` / `00:15` / `00:30`（AST 排除 docstring 後掃 HH:MM，
兩個模組命中皆為 0）。eligibility 一律由 Asia/Taipei TOU、fresh SOC、
Natural Decision、Authority、PCS、Meter、fault/alarm 決定。
**歷史 operational window 不得成為 production schedule。**

**Polling / Backoff**

```
decision_interval_sec       30.0     沿用既有已核准 production 值
retry_backoff_sec           None     RETRY_BACKOFF = UNRESOLVED
service_health_timeout_sec  None     SERVICE_HEALTH_TIMEOUT = UNRESOLVED
```

後兩者目前沒有足夠 field evidence 支持 production 數值，因此**刻意留 None
並列入 `missing()`**，不自行填值。它們不是 C Offline 的完成 blocker，
但屬於 **C Live Deployment 前待定的 production parameters**。

**Critical States**

```
OWNERSHIP_CONFLICT_CRITICAL / RESTORE_FAILED_CRITICAL
RECOVERY_UNKNOWN            / REMOTE_IDENTITY_MISMATCH
```

共同規則：禁止 new handoff、禁止 blind retry control、寫 durable critical
event、Fail Closed。

**測試**

| 測試 | 結果 |
|---|---|
| `test_phase6_610c_service.py` | 102 / 102 PASS |
| `test_phase6_610b2_handoff_runner.py` | 77 / 77 PASS |
| `test_phase6_610a_remote_adapter.py` | 83 / 83 PASS |
| `test_phase6_610_handoff.py` | 145 / 145 PASS |
| **Phase 6 full regression** | **3694 / 3694 PASS，39 檔，0 FAIL 檔** |

**Live Deployment Blockers**

```
NETWORK_IDENTITY_STABILITY   NOT VERIFIED（B1.7 DEFERRED）
DHCP Reservation             NOT CONFIRMED
Deployed remote guard        B1 —— pause / restore = REFUSED
retry_backoff_sec            UNRESOLVED
service_health_timeout_sec   UNRESOLVED
FIRST LIVE                   HOLD（外部控制程式仍在運行）
```

以上任一未解除，`live_handoff_allowed()` 即為 False。
**不得**以「離線測試全過」為由部署為常駐服務。

**本輪安全狀態**

```
DISPATCH_ENABLED   False              MODE            DRY_RUN
Windows Task       未建立              Service / NSSM  未安裝、未啟動
實機 Phase 6       CHARGE / DISCHARGE / STOP = 0 / 0 / 0
External           stop / start / pause / restore = 0 / 0 / 0 / 0
```

##### 11.15 Current Safety State

```
Remote Guard          probe / loopcheck / status = ENABLED
                      pause / restore            = REFUSED
DISPATCH_ENABLED      False
MODE                  DRY_RUN
PAUSE_AUTHORIZATION   NOT GRANTED
FIRST LIVE RETRY      HOLD

實機 Phase 6          CHARGE / DISCHARGE / STOP = 0 / 0 / 0
External              stop / start              = 0 / 0
```

</details>
