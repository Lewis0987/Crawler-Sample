# Phase 6.10 — Field Validation（實機驗證紀錄）

> **Evidence record.**
> Current project status is authoritative in [`test/README.md`](../test/README.md).
> 本文件保存 **2026-09-17** 的完整實機證據；摘要與目前狀態見 README。

涵蓋：Fresh STEP 2 Read-Only Precheck、B2 Field Validation Trial #1、
preflight 20/20、pause 2.068s / restore 0.610s、PauseVerifier 與
RestoreVerifier 證據、evidence 檔路徑、deployed source 調查、
Q1–Q4、Trial #2 provisional design。

---

### Field Status（2026-09-17 Fresh Read-Only Precheck）

> 🔴 以下為**當次唯讀取樣**的事實，不是持續保證。任何 Field 階段開始前
> 都必須重新 fresh observe —— 舊讀值一律作廢。

| 項目 | 觀測值 |
|---|---|
| ESS HMI `192.168.128.110:8080` | REACHABLE；login OK；`read_all` 3.1s；9 支端點全通 |
| Battery | `已上電`；**SOC = 2.0 %**；894.3 V；0.0 A |
| PCS | **STANDBY**；`manual` 模式；schedule=0 / manual=1；fault=False |
| Alarm | 2 筆皆 level=1 且已復歸；critical = 0；alarm source COMPLETE |
| Meter `192.168.1.47:6160` | FRESH（age 0.29s）；`meter_state='ok'`；**GRID_IMPORT** 67~91 kW |
| External Controller | **PROCESS EXISTS 且 ACTIVELY LOOPING**（pid 44728、screen 1、console 時間戳持續前進） |
| Authority | **IDLE / `CONTROL_AUTHORITY_IDLE`** —— 只代表 PCS 目前閒置且無人證明擁有，**不代表**外部已讓出控制權 |
| Safety Gate | 本輪 NOT EVALUATED（decision=idle 且 PCS 已 STANDBY，arbiter 於 `IDLE_NO_CONTROL_LEG_REQUIRED` 正常提前返回）—— **不是 FAIL** |
| SSH / B2 guard | `guard_variant=B2`；五項 capability 皆 true；connect 0.48~0.99s（< 已核准 5.0s） |

**SOC = 2.0 % 使任何放電情境都會被政策擋下**（discharge 停止帶 20 / 復歸 25，
Safety Gate 下限 1）。目前**不因「只剩 CHARGE 方向可行」而送出任何控制**。

#### B2 Field Validation Trial #1（2026-09-17）— **PARTIAL PASS**

| 階段 | 結果 |
|---|---|
| STEP 1 Fresh Preflight | **ALL PASS（20 / 20）** |
| STEP 2 Arm（僅該次） | armed=True、runner 注入、pause·restore timeout 12.0 |
| STEP 3 Pause | **SSH_OK / exit 0 / SEND_SENT**，`pause_injected`，**2.068s** |
| STEP 4 Pause Verification | **VERIFIED**（4 筆 / 55s，candidate-idle 9 項全程成立） |
| STEP 5 Restore | **SSH_OK / exit 0 / SEND_SENT**，`restore_injected`，**0.610s** |
| STEP 6 Restore Verification | **RV_RESUMED**（3 次觀測 / 21s，marks 前進） |
| restore responsibility | 成立 → 驗證通過後**已解除** |
| CRITICAL | 無 |

SSH 呼叫統計：`{'pause': 1, 'probe': 7, 'restore': 1, 'loopcheck': 3}`
—— **pause / restore 各恰好 1 次**（`OneAttemptGuard` 強制，重送直接拋例外）。

已由實機證明：

- **SSH control path = VERIFIED**（connect/command timeout 分離、taxonomy 分類正確）
- **OneAttemptGuard = VERIFIED**
- **restore responsibility lifecycle = VERIFIED**（成立 → RV_RESUMED → 解除）
- **RestoreVerifier = RV_RESUMED**；`external_liveness = EXTERNAL_RUNNING`
- controller `pid 44728` 全程未變 —— 未重啟、未崩潰

> 🔴 **PauseVerifier = VERIFIED BY CURRENT CRITERIA，但 pause causal effectiveness
> = NOT YET FULLY DISCRIMINATED。**
> pause 前 PCS 已是 STANDBY、AC ≈ −1.4 kW、Authority = IDLE；pause 後幾乎相同。
> 「行為靜止」在本輪**不具鑑別力** —— 無法區分「controller 因 stop 而停止控制」
> 與「controller 本來就沒在控制」。本輪**缺少 DISCRIMINATIVE PAUSE EVIDENCE**。
>
> 🔴 **不得**因此新增規則「pause 後 loop marks 必須停止前進」——
> `screen -X stuff 'stop'` 不等於 kill process / terminate loop，
> 在取得 `auto_control.py` 原始碼佐證前，marks 前進**不可**視為 pause failure，
> marks 停止也**不可**逕自視為 pause success。

**Timing evidence（首次實機樣本，n = 1）**

| 動詞 | SSH outcome | exit | elapsed |
|---|---|---|---|
| pause | SSH_OK | 0 | **2.068s** |
| restore | SSH_OK | 0 | **0.610s** |

兩者遠低於候選值，但**單一樣本不足以升格 FINAL**：
`pause` / `restore` 維持 **`FIELD_VALIDATION_CANDIDATE = 12.0` / NOT FINAL**，
production `SshTimeouts` 的兩者仍為 `None`（執行當下已驗證未被改動）。

**Evidence 落盤**

```
output/phase6_field_validation/preflight_20260917_133919.json    （第一次 18/20，DECISION_CHANGED 診斷）
output/phase6_field_validation/preflight_20260917_134122.json    （重跑 ALL PASS 20/20）
output/phase6_field_validation/pause_restore_20260917_134500.json（pause/restore 逐步證據）
```

> ⚠️ 第一次 preflight 為 18/20（`authority_fresh_evaluated` 失敗，
> `arbitration = BLOCKED / DECISION_CHANGED`）。根因是**檢查腳本暖機不足** ——
> 分類器 debounce 恰在 arbiter 的 original / fresh 兩次觀測之間成形，
> arbiter **正確地** Fail Closed。非設備異常、非 production 缺陷。
> 修正暖機條件後重跑 ALL PASS 才進入 arm。

#### 歷史 evidence 更正與 Trial #2 設計前提

🔴 **更正一項先前的推論：`SOC = 2.0%` **不**代表 external controller 會維持 idle。**

`output/phase6_live_charge/first_live_20260831_235901.log`（2026-09-01，OFF_PEAK）：

```
#03 00:00:48  auth=IDLE                 pcs=STOPPED   AC=-1.30  meter= 27.92 kW  soc=2.0
#04 00:01:10  auth=EXTERNAL_OR_UNKNOWN  pcs=CHARGING  AC=-1.40  meter=108.60 kW  soc=2.0
```

同樣是 SOC 2.0%，external controller 仍自行進入 CHARGING，電表跳增 **+80.68 kW**。
→ Trial #1 缺乏鑑別力的真正原因是 **當時為 PEAK window，controller 本來就沒有主動控制**，
**不是** SOC 太低。

🔴 **另一項重要發現：`actual_active_power_kw` 不是可靠的鑑別器。**
外部以約 80 kW 充電當下，ESS HMI 的 AC 功率仍顯示 **−1.40 kW**。
因此 `candidate_idle` 的 `power_in_idle_band(−3.0 ~ +1.0)` 在外部主動充電時**仍會通過**。
真正抓到外部控制的是 `pcs_legal_idle`（CHARGING ∉ {STANDBY, STOPPED}）與
`authority_idle`（EXTERNAL_OR_UNKNOWN ≠ IDLE）；**meter 跳增訊號最強但不在既有判準內**。

**Trial #2 設計前提（尚未執行、需另行授權）**

| 項目 | 規則 |
|---|---|
| Phase A BASELINE | 純被動觀測（建議 10 min / 20s 間隔），**必須實際觀測到 external control** |
| **BASELINE ABORT** | Phase A 若未觀測到外部控制（PCS 一直 STANDBY、Authority 一直 IDLE、無其他既有 external-control evidence）→ **ABORT，不送 pause**。沒有 active baseline 就沒有因果對照 |
| Phase B PAUSE | 僅在 Phase A 確認後；**ONE ATTEMPT**；timeout 12.0（CANDIDATE / NOT FINAL） |
| Phase C PAUSED | 與 Phase A 相同 cadence 與 observation set；PauseVerifier **沿用既有規則**，不改 candidate-idle / external_quiescent 語意 |
| Meter kW | 納入為 **CORROBORATING / DISCRIMINATIVE EVIDENCE**，記錄 Phase A / Phase C 的範圍與 delta；**不單獨升格為 PASS/FAIL policy criterion**（PCC 電表同時受其他 site load 影響） |
| loopcheck | pause window 可額外取樣，但**僅 OBSERVATION ONLY** —— 在 source 證實 stop 後 loop 語意前，**marks 停止 ≠ PASS、marks 前進 ≠ FAIL** |
| Phase D/E RESTORE | **ONE ATTEMPT**；沿用 RestoreVerifier（runtime healthy + marks resumed）；**先不修改 RestoreVerifier** |
| 執行前 | 一律重新 fresh：TOU / ESS / PCS / Meter / Authority / Alarm·Fault / external liveness。**不得因時段到了就假設 controller 一定會動** |

> 🔴 即使 Trial #2 PASS，**FIRST LIVE 仍保持 HOLD**，直到
> 「stop 後是否仍寫 BESS command」能由可靠 evidence 閉合。

#### Read-only 動詞的實際遠端行為（重新分類）

| 動詞 | 遠端動作 | 分類 |
|---|---|---|
| `probe` | `screen -ls` / `pgrep` / `ps` / `readlink /proc/<pid>/…` | **PASSIVE_READ_ONLY** |
| `loopcheck` | `screen -X hardcopy`（抓兩次畫面時間戳，暫存檔用完即刪） | **PASSIVE_READ_ONLY** |
| `status` | `screen -X stuff $'status\n'` —— **寫入 controller stdin** | **QUERY_ONLY_WITH_STDIN_WRITE** |

> 🔴 `status` 的語意是查詢、不改 setpoint，但 transport 確實會寫入 controller
> stdin。**不得再把它描述為 pure read-only。** 若某輪規則是 NO STDIN WRITE，
> 則 `status` 不得使用。B2 allowlist 本身暫不修改。
>
> 🔴 **recovery 證據只准 `probe` + `loopcheck`**（皆 PASSIVE_READ_ONLY）——
> `recover()` / `UnattendedService.boot()` **不得**使用 `status` 取得 liveness。
> `external_liveness` 的三態：runtime 事實取不到 → `EXTERNAL_UNKNOWN`；
> runtime 不健康 → `EXTERNAL_NOT_RUNNING`；runtime 健康且 loop marks 前進 →
> `EXTERNAL_RUNNING`；loop 證據不足 → `EXTERNAL_UNKNOWN`（Fail Closed）。
> **`running` 的語意是「active loop confirmed」，不是 process/screen exists。**
>
> 📋 PROCESS DEVIATION（2026-09-17）：`QUERY SENT BEFORE IMPLEMENTATION
> INSPECTION` —— `status` 曾在未先檢視遠端實作前送出一次。前後 fresh evidence
> 一致（PCS=STANDBY、0.0 A、fault=False、SOC 2.0%），**未觀察到設備狀態改變**；
> `pause` / `restore` / `start` / `stop` / `quit` 執行次數為 0。無需補償操作。

#### `demand_state = 'degraded'`（READ-ONLY 根因調查）

現場 payload 同時出現：`bess_0_state='offline'`、`soc_0=None`、
頂層 `status='BESS_0_OFFLINE'`，而 `bess_1_state='ok'`、`soc_1=2`
（與 ESS HMI 的 SOC 2.0% 相符）。`demand = meter − bess_sum`，
離線單元貢獻 0 → demand 由**不完整的輸入**算出。

- `degraded` 是 `DEMAND_STATE_VOCAB` 內的**合法值**，語意為「值可用但來源不完整」，
  與 `fault`（值不可用）不同。
- **Control impact = NONE**：`demand_kw` / `demand_state` 在 `power_classifier`、
  `meter_observation_adapter`、`pcs_auto_control_production` 均明載**僅供診斷**，
  併網點方向唯一來源是 `meter` / `meter_state`（本輪為 `ok`）。
- **Fail-Closed impact = NONE**：`meter_client` 只在 `demand_state == 'fault'`
  時判為 `INVALID_DEMAND_STATE_FAULT`；`degraded` 不影響 `valid`。

### Live Blockers

- Remote Guard（field）：`probe` / `loopcheck` / `status` / `pause` /
  `restore` 皆在 B2 allowlist 內；`pause` / `restore` **實機執行次數 = 0**
- `pause` / `restore` command timeout = **FIELD_VALIDATION_CANDIDATE = 12.0**
  （**NOT FINAL**）；production `SshTimeouts` 仍為 `None`，恆列於 `missing()`。
  真正做第一輪 field pause / restore 時才明確注入以取得 timing evidence，
  field test 完成後再決定是否升格 FINAL
- ~~Phase 6.10 pause/restore execution = BLOCKED BY RECOVERY EVIDENCE CONTRACT~~
  → **已修復（2026-09-17）**：`recover()` 不再依賴 guard 不輸出的 `running`，
  改用與 `RestoreVerifier` 同一組證據（probe runtime health + loopcheck 前進），
  由 `phase6_remote_adapter.external_liveness` 正規化成三態。
  回歸 `test_phase6_610d_recovery_contract.py` 47 / 47 PASS。
- 🔴 **FIRST LIVE BLOCKER：External Controller STOP semantics
  = `DEPLOYED_SOURCE_UNCONFIRMED`**

  本機**已找到** 3 份 `auto_control.py` 副本（2026-09-17 補搜 `D:\_vs_prj` 才發現；
  先前記為 `SOURCE_UNAVAILABLE` 是**搜尋範圍不足**，在此更正）：

  | 路徑 | 行數 | 日期 |
  |---|---|---|
  | `D:\_vs_prj\_dean\ECO15 一次控2台電櫃\auto_control.py` | 361 | 2025-04-10 |
  | `D:\_vs_prj\ems_prototype\auto_control.py` | 314 | 2025-04-09 |
  | `D:\_vs_prj\ems_prototype\backup\auto_control.py` | 242 | 2025-04-09 |

  🔴 **但這三份都不是現場部署版**（決定性反證）：三份原始碼**均無任何
  印出時間戳的程式碼**，而現場 `loopcheck` 實測到 screen buffer 內
  `YYYY-MM-DD HH:MM:SS` 每約 3 秒前進一次。
  → 部署版**至少**多了帶時間戳的輸出，**因此也可能在 stop 分支上有差異**。
  本機副本只能作為 **HYPOTHESIS**，不得當作部署版的證明。

  **本機副本（三份一致）顯示的語意 —— 標記為 HYPOTHESIS：**

  | 問題 | 本機副本顯示 | 部署版狀態 |
  |---|---|---|
  | Q1 `stop` 改變的狀態 | `running = False`、`Pd = 0` | **UNCONFIRMED** |
  | Q2 main loop 是否繼續 | 繼續（`while True`），但 `not running` 時只 `sleep(1); continue` | **UNCONFIRMED** |
  | Q3 停止後是否仍讀 meter/BESS/計算/heartbeat | **否**（全部跳過，且不印任何訊息） | **UNCONFIRMED** |
  | **Q4 停止後是否仍寫 BESS setpoint** | **`ZERO COMMAND`（stop 當下一次性寫 0），其後 `NO COMMAND`** | **UNCONFIRMED** |

  **證據分層（不得混用）**

  | 主題 | LOCAL-COPY（副本顯示） | FIELD OBSERVATION（實測） | DEPLOYED（部署版） |
  |---|---|---|---|
  | **Q4 stop 後 command 行為** | `ONE-TIME ZERO COMMAND` → 其後 `NO COMMAND` | 無法觀測（HMI 無 setpoint readback） | **UNCONFIRMED** |
  | **控制面（control plane）** | `request_kw()` 為 **HTTP POST 至 ESS HMI manualControl**，非 Modbus write → external controller 與 Phase 6 **可能**共用 HMI control plane | 未觀測 | **UNCONFIRMED** |
  | **`status` 語意** | 主迴圈只有 `start`/`stop`/`quit`，**無 else 分支** → 會被忽略 | Trial #1 注入前後 **NO OBSERVED DEVICE EFFECT**（PCS state / current / fault / SOC 皆無變化） | **UNCONFIRMED**（僅 `LOCAL-COPY CONSISTENT`） |
  | **`pause` 是否寫一次 0 kW** | 是（`request_kw(..., 0)` 一次） | Trial #1 時 PCS 已 STANDBY，寫 0 為 no-op，無法鑑別 | **UNCONFIRMED** |

  🔴 以上四項**一律不得**寫成 deployed fact。
  特別是：**不得**寫「deployed control plane = HMI HTTP」、
  **不得**寫「deployed pause = one-time zero」、
  **不得**寫「DEPLOYED STATUS SEMANTICS CONFIRMED」。

  🔴 **仍為 FIRST LIVE BLOCKER**：Q4 對**部署版**尚未閉合。
  即使副本顯示 stop 後是 command silence，也**不得**據此宣稱
  `pause` = command silence。

  ⚠️ **Trial #2 必須預先納入的可能副作用**：若 deployed 確認與副本一致，
  則 B2 `pause` 會伴隨 **`PAUSE TRANSITIONAL SIDE EFFECT = ONE-TIME ZERO SETPOINT`**
  —— 它**不是** Phase 6 CHARGE / DISCHARGE / PCS STOP，
  但**是**由 B2 pause 觸發的 real external-controller command effect，
  必須在 preflight 前明確知悉並記入 field evidence。
  目前此副作用本身亦為 **UNCONFIRMED**。

  🔒 **Hard-coded credential risk observed in local controller copies.**
  分類：**SECURITY ISSUE / SEPARATE TRACK**，不在 Phase 6.10 scope 內處理；
  本文件不記錄帳號密碼內容，亦未複製、未修改、未旋轉任何 credential。

  以下四項對**部署版**仍為 **UNKNOWN，不得推測**：

  | 問題 | 狀態 |
  |---|---|
  | `stop` 改變哪個內部狀態（running / enabled / …） | **UNKNOWN** |
  | `stop` 後 main loop 是否繼續 | **UNKNOWN** |
  | `stop` 後是否仍讀 meter / BESS / 計算 / heartbeat | **UNKNOWN** |
  | **`stop` 後是否仍寫 BESS setpoint** | **UNKNOWN**（四態：`A. NO COMMAND` / `B. ONE-TIME ZERO COMMAND THEN NO COMMAND` / `C. PERIODIC ZERO COMMAND` / `D. NORMAL CONTROL COMMAND`） |

  🔴 **最後一項是 FIRST LIVE BLOCKER。** 即使 Trial #2 證明 PCS idle / Authority idle /
  Meter 回到 baseline，仍**無法排除** external controller 持續週期性寫入 0 setpoint；
  若真如此，會與未來 Phase 6 command 形成 controller contention。
  **不得宣稱 `pause` = command silence。**

  ⚠️ 補充（本機唯讀查證）：HMI **沒有**可讀回「已下達 setpoint」的端點 ——
  `device_control_operator.py` 既有註記 `verify source does not expose PCS power
  status yet`，`read_all()` 亦無任何 setpoint 欄位。因此此項**無法**靠現有 HMI
  唯讀管道閉合，需原始碼或 Modbus 層證據。
- **External Controller ACTIVE**（2026-09-17 觀測到 process 存在且 loop 前進）
  → 依既有規則 FIRST LIVE 保持 HOLD
- **SOC = 2.0 %**：放電方向被政策全面擋下；充電方向未獲授權
- FIRST_LIVE_PREREQUISITE = False；`DISPATCH_ENABLED = False`


---

# DEPLOYED SOURCE EVIDENCE（2026-09-17）

> 由具 `192.168.70.201` shell 權限者提供的**唯讀**證據。
> **此為 deployed behavior 的最高權威。**

## 版本識別

| 項目 | 值 |
|---|---|
| 路徑 | `/home/etica/ems/auto_control.py` |
| SHA-256 | `964bb642aa0e70a9663d636dc926150aaece60f13d28a745adbc53c41b3976ce` |
| 行數 | **789** |
| 結論 | **CONFIRMED DIFFERENT VERSION** |

本機三份舊副本（**361 / 314 / 242** 行，2025-04）**皆不相符**，
自此僅保留為 **historical reference**，不得用於推斷 deployed behavior。

## Q1 ~ Q4 — 全部 CLOSED

### Q1 `stop` 改變哪個 internal state — **CLOSED**

```
立即            running = False
停止過程中      LastPd[i] = 0 / Step[i] = 0
停止確認完成後  Pd = 0 / LastVer.clear()
```

### Q2 `stop` 後 main process / loop 是否繼續 — **CLOSED**

**YES。** process 不退出、`read_and_control` thread 不退出。
停止確認完成後回到 outer `while True`，因 `running == False` 走
`sleep(1); continue`。thread 存活但**正常控制 loop 不執行**；
僅 `start`（`running = True`）可恢復。

### Q3 `stop` 後是否仍 read / calculate / heartbeat — **CLOSED（分兩階段）**

| 階段 | 行為 |
|---|---|
| **STOPPING / CONVERGENCE** | 每 ~5 s `bess_client_read()`、讀實際 BESS power、判斷是否收斂 0、必要時再送 `request_kw(i, 0)`。**不**走正常 `check_demand()` / strategy / demand control loop |
| **STOP CONFIRMED** | outer loop `sleep(1); continue`。**不再**執行 `check_demand()`、正常 meter/demand strategy、正常 BESS control、正常 timestamp heartbeat |

### Q4 `stop` 後 BESS command behavior — **CLOSED**

```
正式分類：E. ZERO-UNTIL-STOP-CONFIRMED → THEN NO COMMAND
```

比原先的 A/B/C/D 四態更精確 —— **皆不符**：

| 原分類 | 判定 |
|---|---|
| A. NO COMMAND immediately | ❌ |
| B. ONE-TIME ZERO ONLY | ❌ |
| C. PERIODIC ZERO FOREVER | ❌ |
| D. NORMAL CONTROL COMMAND | ❌ |

**deployed STOP 語意（逐步）**

```
stop
 ├─ running = False
 └─ 停止確認 while loop
      ├─ sleep(5)
      ├─ bess_client_read()
      ├─ registers 為空 → continue（不宣稱停止完成）
      ├─ 對 LastVer 中每台 BESS：LastPd[i] = 0 / Step[i] = 0
      ├─ 讀取實際 BESS power
      ├─ 實際 power 仍非 0 → request_kw(i, 0)；is_stop = False
      ├─ 下一輪 5 秒後重新確認
      └─ 全部實際收斂為 0 → is_stop = True
            → print("已停止運行") / Pd = 0 / LastVer.clear() / break
之後：running == False → sleep(1); continue（正常 loop 保持停用，無後續 command）
```

## `request_kw` deployed control path

deployed `bess_request.py` 的 `request_kw(ver, value, status=1)`：

1. 先登入對應 ESS HMI，取得 Bearer token
2. HTTP POST 至 `admin-api/client/dynamic/sinexcel/dataOrControl/manualControl`
3. payload 含 `activePowerSetPoint = value`

→ `request_kw(i, 0)` = **真實 HMI control write**（`activePowerSetPoint` 0 kW），
**不是** Modbus write。

🔒 原始碼內存在 hard-coded credential。
**SECURITY ISSUE / SEPARATE TRACK** —— 本文件**不記錄**任何 credential 值。

## Deployed `status` semantics — CLOSED

`elif command == "status":` 只 `print`：
`MAX_kW` / `MAX_PD_CH` / `MAX_PD_CH_UNDER` / `MAX_PD_DC` / `MAX_PD_DC_OVER` /
`MIN_SOC` / `MAX_SOC` / `OVER_flag` / `UNDER_flag`。

**沒有** `running` 變更、沒有 `command_queue` 控制入列、沒有 `request_kw()` /
`request_mode()` / 任何 device control。

| 面向 | 分類 |
|---|---|
| transport | **QUERY_ONLY_WITH_STDIN_WRITE**（`screen -X stuff 'status\n'` 仍是 stdin write） |
| deployed device semantics | **PRINT / QUERY ONLY** |
| DEVICE CONTROL EFFECT | **NONE BY SOURCE** |

🔴 仍**不可**稱為 `PASSIVE_READ_ONLY`。

## Loopcheck semantics — source confirmed

deployed 的 `print(now.strftime("%Y-%m-%d %H:%M:%S"), …)` 位於
`if not running: sleep(1); continue` **之後**的正常 demand loop 內。

| 狀態 | timestamp marks |
|---|---|
| `running = True` | 正常 demand loop → marks 持續前進 |
| `running = False` | 正常 demand loop 不再抵達 timestamp print → marks 應停止前進 |

🔴 **但 marks 停止 ≠ stop convergence 完成** ——
stop handler 的 convergence loop 期間仍可能在 read BESS / 送 `request_kw(i,0)`，
而此時正常 timestamp 已不再輸出。

| 觀測 | 可以證明 | 不能證明 |
|---|---|---|
| marks stopped | **NORMAL CONTROL LOOP HALTED** | STOP CONVERGENCE COMPLETE |
| marks advancing（pause 處理後） | 正常 control loop 可能仍 active → **BLOCK / INVESTIGATE** | — |

## `start` / restore semantics

`start` 在 `running == False` 時僅設 `running = True`，
**未見 `start` 本身直接呼叫 `request_kw()`**。
恢復後是否出力由 controller 依正常 policy / demand / SOC / TOU 自行決定。

→ 支持既有 `RestoreVerifier` 的設計：**不要求** restore 後一定
CHARGE / DISCHARGE，只需 runtime healthy + normal loop resumed。

## 🔴 FIELD SAFETY IMPLICATION

```
PAUSE TRANSITIONAL CONTROL EFFECT = REPEATED ZERO SETPOINT WHILE CONVERGING
```

B2 `pause` **不是**純 software flag change：
它會觸發真實 external-controller control effect `request_kw(i, 0)`，
且**可能不只一次** —— 重送條件為「實際 BESS power 尚未收斂為 0」，每約 5 秒重新確認。

它**不是** Phase 6 CHARGE / DISCHARGE / PCS STOP，
但**是**由 B2 pause 觸發的真實 external-controller command，
Trial #2 與未來 handoff evidence **必須明文記錄**。

## 用語規範

| ❌ 不得使用 | ✅ 正確 |
|---|---|
| `pause = immediate command silence` | `PAUSE SEMANTICS = ZERO-UNTIL-STOP-CONFIRMED → THEN COMMAND SILENCE` |

---

# TRIAL #2 — DISCRIMINATIVE PAUSE VALIDATION（OFFLINE DESIGN REVIEW）

> **本節僅為設計，未執行。** Trial #2 field execution **尚未獲授權**。

## 目標

證明以下因果鏈，而非僅觀察到「設備很安靜」：

```
external controller 確實 active control
   → pause
   → normal control loop halted
   → zero convergence 完成
   → stable idle
   → restore
   → normal control loop resumed
```

## Evidence 分層

| Phase | 內容 | 判準 |
|---|---|---|
| **A. ACTIVE BASELINE** | 純被動觀測（建議 10 min / 20 s 間隔） | **必須**觀測到 external active control：`PCS = CHARGING/DISCHARGING` **或** `Authority = EXTERNAL_OR_UNKNOWN`，並搭配 **loop marks advancing**。Meter kW 為 **CORROBORATING EVIDENCE ONLY** |
| **ABORT RULE** | Phase A 未觀測到 active control | **ABORT，不送 pause** —— 沒有 baseline 就沒有因果對照 |
| **B. PAUSE** | **ONE ATTEMPT ONLY** | 🔴 **不得**把第一個 zero command 當成 pause complete（收斂可能重複送 0） |
| **C1. NORMAL LOOP HALT** | `loopcheck` marks 停止前進 | 支持 **NORMAL CONTROL LOOP HALTED**；**不是** full pause PASS |
| **C2. STOP CONVERGENCE** | 沿用既有 `PauseVerifier` | PCS legal idle / Authority idle / stable sample window / comm healthy / fault clear / critical alarm clear 等既有 criteria |
| **D. RESTORE** | **ONE ATTEMPT ONLY** | `start` 僅 `running = True`，不要求立即出力 |
| **E. RESTORE VERIFY** | 沿用既有 `RestoreVerifier` | runtime healthy + loop marks resumed / advancing → `RV_RESUMED`。**不要求** PCS 一定 CHARGING / DISCHARGING、Meter 一定跳變 |

## PASS concept（提案，未定案）

```
A. Baseline 證明 normal external control loop active
B. pause ONE ATTEMPT
C. normal loop marks cease
D. PauseVerifier VERIFIED → stable legal idle
E. restore ONE ATTEMPT
F. RestoreVerifier RV_RESUMED → marks resumed
G. 無 fault / critical；無 ownership BOTH
```

🔴 **`normal loop halted` + `PauseVerifier VERIFIED` 兩者同時成立**，
才可把 external pause behavior 視為 **discriminatively verified**。
**不得只靠 loop marks。**

## 必須預先納入的副作用

```
PAUSE TRANSITIONAL CONTROL EFFECT = REPEATED ZERO SETPOINT WHILE CONVERGING
```
須於 preflight 前明確知悉，並在 field evidence 中逐筆記錄。

## 未變更的既有約束

- pause / restore timeout = **FIELD_VALIDATION_CANDIDATE 12.0 / NOT FINAL**（n = 1）；
  production `pause` / `restore` 維持 `None`
- 執行前一律重新 fresh：TOU / ESS / PCS / Meter / Authority / Alarm·Fault / external liveness
- preflight 遠端只用 `probe` + `loopcheck`
- **不得**為了 Trial #2 先改 production semantics

---

# CURRENT SAFETY STATE（正式狀態；每輪必須完整列出）

```
DISPATCH_ENABLED            = False
MODE                        = DRY_RUN
RemoteSenders armed         = False
DEPLOYED_GUARD_VARIANT      = B2
NETWORK_IDENTITY_STABILITY  = ACCEPTED / USER CONFIRMED（不是 PASS）
Trial #2 harness            = IMPLEMENTED / OFFLINE VERIFIED（184 / 184）
Trial #2 execution readiness= READY FOR AUTHORIZATION
Trial #2 field execution    = HOLD（未授權、未執行）
FIRST LIVE                  = HOLD

Phase 6 real CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2  pause / restore               = 1 / 1

pause / restore SSH command timeout
    = FIELD_VALIDATION_CANDIDATE 12.0 / NOT FINAL（n = 1）
    production default：SshTimeouts().command_timeouts['pause'] / ['restore'] = None
```

🔴 `DEPLOYED_GUARD_VARIANT` **不得留空**。
正式狀態恆為 **B2**（2026-09-07 部署、2026-09-17 Trial #1 再次以 `probe`
的 `guard_variant=B2` 欄位確認）。歷史文件中的 `DEPLOYED_GUARD_VARIANT = B1`
為**當時**狀態，已被取代。

---

# TRIAL #2 HARNESS — IMPLEMENTED / OFFLINE VERIFIED（2026-09-17）

> 本節取代前面「OFFLINE DESIGN REVIEW」的**待實作**狀態。
> 設計本身未變；以下為實際落地的模組與驗證結果。
> 🔴 **Trial #2 仍未執行；FIELD EXECUTION = HOLD。**

## 交付物

| 檔案 | 內容 |
|---|---|
| `test/phase6_trial2_harness.py` | Trial #2 field-validation harness（trial tooling，零 I/O） |
| `test/test_phase6_trial2_harness.py` | 專屬離線回歸 **184 / 184 PASS** |

`phase6_trial2_harness.py` 只 import `phase6_remote_adapter`。
**未** import decision engine / arbiter / service / production 組裝 /
handoff orchestrator —— 由 S10 AST 掃描鎖住，確保不碰 Phase 6 frozen
application layer。

## G1 / G3 — ACTIVE BASELINE 與 ABORT

```
ActiveBaselineObserver.feed(sample, loop_mark)
    -> OBSERVING / ACTIVE_BASELINE_ESTABLISHED / NO_ACTIVE_BASELINE
```

成立條件（**AND**）：

```
loop_marks_advancing is True
AND  至少一筆 sample 具備 active evidence：
         pcs_charging  |  pcs_discharging  |  authority_external
```

`BaselineConfig(observe_duration_sec, sample_interval_sec,
min_loop_observations)` —— **三項全必填**，任一為 None / 非正數即 `ValueError`。
建議值 `RECOMMENDED_BASELINE_DURATION_SEC = 600.0` /
`RECOMMENDED_BASELINE_INTERVAL_SEC = 20.0`，**只是建議常數**，
呼叫端仍必須明確傳入；production 模組內**沒有**這兩個參數。

**ABORT RULE**：gate 未成立 → `OUT_ABORTED_NO_ACTIVE_BASELINE`，
`pause_attempt_count == 0`、sender 完全未被呼叫、責任未成立、不需 restore。

**Meter**：`active_evidence()` 結構上**不讀任何 meter 欄位**
（T6 以 AST 掃描字串與識別字雙重鎖住）。meter 觀測仍逐筆保存於
`meter_observations_corroborating_only`，欄位名即宣告其角色。

## G2 — 兩層分離（POLICY 落地）

`LEGAL_OBSERVED_PCS_STATES = ("CHARGING", "DISCHARGING", "STANDBY", "STOPPED")`

Trial #1 的 `pcs_state in ("STANDBY","STOPPED")` **未**出現在 LAYER 1
（S1 以 AST 確認 `safety_preflight` 不含該判定字串）。
`UNKNOWN` / `FAULT` / `None` / 未知字串仍 Fail Closed。

LAYER 1 的 15 項：
`hmi_comm_ok` `meter_fresh` `source_fresh` `remote_ssh_ok` `screen_alive`
`process_alive` `process_identity_ok` `guard_identity_ok` `pcs_state_legal`
`fault_clear` `alarm_clear` `ownership_not_both` `dispatch_disabled`
`mode_dry_run` `senders_not_armed`。

## G4 — LOOP HALT

```
LoopHaltTracker -> OBSERVING / NORMAL_CONTROL_LOOP_HALTED
                 / STILL_ADVANCING / LOOP_EVIDENCE_UNAVAILABLE
normal_loop_halted -> True / False / None
```

🔴 `None`（取不到 loop 證據）**不得**視為 halted。

鑑別結論唯一入口：

```python
discriminative_pause_verified(normal_loop_halted, pause_verifier_state)
    = (normal_loop_halted is True) and (pause_verifier_state == PV_VERIFIED)
```

真值表（S7 全數鎖住）：`(True, VERIFIED)` 才是 True；
`(True, FAILED/SETTLING_TIMEOUT/UNKNOWN_SOURCE/None)`、`(False, VERIFIED)`、
`(None, VERIFIED)` 一律 False。

## G5 — ONE ATTEMPT（版控化）

```python
class OneAttemptGuard:
    MAX_ATTEMPTS = 1
    take(verb)  # 超額 -> raise OneAttemptExceeded（不是回 False）
    pause_attempt_count / restore_attempt_count
```

`Trial2Runner._send()` 是**唯一**送出路徑，先 `guard.take(verb)` 再呼叫 sender，
因此結構上不可能重送。`SEND_UNKNOWN` 亦不例外。
**不再依賴任何 scratchpad 腳本。**

## G7 — EVIDENCE ARTIFACT

`ARTIFACT_SCHEMA_VERSION = 1`，`ARTIFACT_FIELDS` 37 欄。
`REQUIRED_TRIAL2_FIELDS`（9 項，回歸鎖）：

```
pause_transitional_control_effect   normal_loop_halted
pause_verified                      restore_resumed
active_baseline_established         pause_attempt_count
restore_attempt_count               pause_send_outcome
restore_send_outcome
```

固定寫入的語意標籤：

```
pause_semantics                     = ZERO-UNTIL-STOP-CONFIRMED -> THEN COMMAND SILENCE
pause_transitional_control_effect   = ZERO-UNTIL-STOP-CONFIRMED
meter_role                          = CORROBORATING_ONLY
actual_active_power_kw_discriminative_value = LOW
deployed_source_sha256              = 964bb642…3976ce
```

`validate_artifact()` 會擋下：計數 > 1、缺必要欄位、
`discriminative_pause_verified` 與證據不一致、
把 pause 寫成 `immediate command silence`。

未登記於 `ARTIFACT_FIELDS` 的欄位一律 `KeyError`，不得偷渡。

## Trial2Runner — 12 步驟

```
 1 SAFETY PREFLIGHT        未過 -> ABORTED_SAFETY_PREFLIGHT（不送 pause）
 2 ACTIVE BASELINE OBSERVE duration / interval 由 BaselineConfig 提供
 3 ACTIVE GATE             未過 -> ABORTED_NO_ACTIVE_BASELINE（不送 pause）
 4 PAUSE ONE ATTEMPT       NOT_SENT -> ABORTED_PAUSE_FAILED（責任未成立）
                           SENT / UNKNOWN -> restore_responsibility = True
 5 LOOP HALT OBSERVATION   normal_loop_halted
 6 PAUSE VERIFIER          沿用既有 9 項 criteria / 4 筆 / 15 s / 120 s
 7 RESPONSIBILITY STATE
 8 RESTORE ONE ATTEMPT     責任成立即必須歸還
 9 RESTORE VERIFIER        RV_RESUMED 才可清除責任
10 POST CHECK              ownership != BOTH / fault / alarm / runtime / 責任
11 EVIDENCE ARTIFACT
12 FINAL SAFETY RESET      無論成敗都執行
```

## 離線驗證結果

`test_phase6_trial2_harness.py` = **184 / 184 PASS**
（使用者指定的 T1~T15 全覆蓋，另加 S1~S10 結構鎖）

| 情境 | 結果 |
|---|---|
| PCS CHARGING / DISCHARGING / Authority EXTERNAL + marks advancing | Active Gate **PASS** |
| PCS STANDBY + Authority IDLE | Active Gate **FAIL** |
| marks not advancing（即使 PCS CHARGING） | Active Gate **FAIL** |
| Meter 999 kW 但無其他 active evidence | Active Gate **FAIL** |
| No Active Baseline | **ABORT**，`pause_attempt_count == 0` |
| pause `SEND_UNKNOWN` | count = 1、無 resend、責任成立 |
| marks stopped only | `discriminative_pause_verified = False` |
| marks stopped + `PV_VERIFIED` | `discriminative_pause_verified = True` |
| restore `SEND_UNKNOWN` | count = 1、無 resend |
| 第二次 restore | `may_attempt` 擋下（harness 與 production 雙層） |
| `RV_RESUMED` | 責任清除 |
| restore runtime healthy 但 marks 未前進 | 非 `RV_RESUMED`，**責任保留** |

## 未變更項目（明文確認）

- `PauseVerifier` 九項 criteria / 4 筆 / 15 s / 45 s / 120 s —— **未改**
- `RestoreVerifier` 3 筆 / 3 s、不要求 PCS 充放電、不要求 Meter 跳變 —— **未改**
- SSH taxonomy、`classify_send_outcome`、responsibility 生命週期 —— **未改**
- `SshTimeouts().command_timeouts['pause'] / ['restore']` —— 仍為 **None**
- `STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC = {pause: 12.0, restore: 12.0}` ——
  仍是 **FIELD_VALIDATION_CANDIDATE / NOT FINAL（n = 1）**，未升格
- `DISPATCH_ENABLED = False`、`RemoteSenders().armed = False`、
  `DEPLOYED_GUARD_VARIANT = B2` —— **未改**
- `verify_pause._quiescent_hint` / `verify_restore` 對 `probe["running"]` 的
  latent 依賴 —— **刻意未改**，維持 KNOWN LIMITATION，不擴 scope

## P2 — `request_restore` one-attempt 對稱保護

| | |
|---|---|
| **OLD** | `request_restore()` 直接 `mark_attempt("RESTORE")` 後送出，無 `may_attempt` 前置閘 |
| **NEW** | 開頭加入 `if not self.guard.may_attempt(step): return False, "ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND"` |
| **WHY** | 與 `request_pause()` 對稱；`OnceGuard.attempted` 由 journal `INTENT` 重建，故 crash / 重啟後亦不得盲目重送 |
| **SAFETY EFFECT** | 只會**減少**送出。第二次呼叫改走 `S_RESTORE_FAILED`（critical，責任保留）＝ Fail Closed |
| **EVIDENCE** | T12b：第一次送出、第二次被擋、sender 僅 1 次呼叫、journal 只有一筆 RESTORE INTENT。Phase 6.10 dedicated 1173 / 1173、full Phase 6 5156 / 5156 全 PASS |

## 狀態

```
TRIAL #2 HARNESS            = IMPLEMENTED / OFFLINE VERIFIED
TRIAL #2 EXECUTION READINESS= READY FOR AUTHORIZATION
TRIAL #2 FIELD EXECUTION    = HOLD（未授權、未執行）
FIRST LIVE                  = HOLD
```

🔴 `READY FOR AUTHORIZATION` **不等於** `FIELD AUTHORIZED`，
更**不等於** `EXECUTED`。

---

# TRIAL #2 — FIELD EXECUTION RESULT（2026-09-18）

```
TRIAL #2 FIELD RESULT          = PASS
DISCRIMINATIVE_PAUSE_VERIFIED  = TRUE
```

| 項目 | 值 |
|---|---|
| 授權 | 使用者 2026-09-18 明確授權（**僅** Trial #2；FIRST LIVE / CHARGE / DISCHARGE / PCS STOP 皆未授權） |
| 起訖 | `2026-09-18 08:54:25` → `09:02:27`（481.7 s） |
| evidence | `output/phase6_field_validation/trial2_20260918_085422.json` |
| STEP 0 preflight | `output/phase6_field_validation/trial2_step0_20260917_175703.json` |

## STEP 0 — FRESH PREFLIGHT：15 / 15 PASS

`probe` ×1 + `loopcheck` ×3，**未使用 `status`**。
`guard_variant=B2` / `capability_ok=True` / identity OK /
`DISPATCH_ENABLED=False` / `MODE=DRY_RUN` / `armed=False` /
production `pause`·`restore` timeout 皆 `None`。

## STEP 1 — ACTIVE BASELINE：ESTABLISHED

| 項目 | 值 |
|---|---|
| 觀測 | **15 samples / 15 loop marks**，08:54:27 → 09:00:32（365 s，窗 600 s） |
| `loop_marks_advancing` | **True** |
| 首次成立 | **sample #16**（09:00:28）`PCS=DISCHARGING` / `Authority=EXTERNAL_OR_UNKNOWN` / `ac=-44.0 kW` |
| `active_hits` | `['pcs_discharging', 'authority_external']` —— **兩項同時成立** |
| Meter | 37.0 → 87.6 → 5.19 kW，**CORROBORATING ONLY，未進入判定** |

sample #1 ~ #15 全為 `STANDBY` / `IDLE`；harness **未**因等待而放寬條件。

## STEP 2 — PAUSE：ONE ATTEMPT

Active Gate 成立後才 temporary arm（`armed=True`，command timeout 12.0 s）。

```
pause_attempt_count = 1
ssh = SSH_OK   exit = 0   elapsed = 0.573 s   -> send_outcome = SENT
guard stdout: pause_injected
```

## STEP 3 — NORMAL LOOP HALT

```
marks after pause: 09:00:32 / 09:00:32 / 09:00:32   （三次完全相同）
LoopHaltTracker  = NORMAL_CONTROL_LOOP_HALTED
normal_loop_halted = True
```

🔴 pause 送出時間 09:00:33，最後一筆前進中的 mark 為 `09:00:31`，
其後 mark 凍結在 `09:00:32` 不再前進 —— 與 deployed source
「timestamp print 位於 `if not running: continue` 之後」完全一致。

## STEP 4 — PAUSE VERIFIER：VERIFIED

| # | 狀態 | PCS | Authority | ac kW |
|---|---|---|---|---|
| 1 | `SETTLING` | DISCHARGING | EXTERNAL_OR_UNKNOWN | −69.1（`pcs_legal_idle` / `power_in_idle_band` / `authority_idle` 未成立） |
| 2 | `STABLE_VERIFY` | STANDBY | IDLE | −1.4 |
| 3 | `STABLE_VERIFY` | STANDBY | IDLE | −1.3 |
| 4 | `STABLE_VERIFY` | STANDBY | IDLE | −1.3 |
| 5 | **`VERIFIED`** | STANDBY | IDLE | −1.6 |

第 1 筆正確落在 `SETTLING`（收斂中），**未**被誤判為失敗 ——
這正是 `ZERO-UNTIL-STOP-CONFIRMED` 的可觀測收斂過程。

```
discriminative_pause_verified = normal_loop_halted(True) AND PV_VERIFIED = TRUE
```

## STEP 5 ~ 7 — RESPONSIBILITY / RESTORE

```
pause SENT         -> restore_responsibility = TRUE
restore_attempt_count = 1
ssh = SSH_OK  exit = 0  elapsed = 0.573 s -> send_outcome = SENT
RestoreVerifier marks: 09:02:07 / 09:02:17 / 09:02:24  (advancing)
RestoreVerifier = RESUMED   -> restore_responsibility = FALSE
```

restore 後 PCS 維持 `STANDBY`、Meter 降至 5.73 kW ——
**未**要求立即出現 CHARGING / DISCHARGING，與 deployed `start` 語意一致。

## STEP 8 — POST CHECK：全數通過

`ownership_not_both` / `fault_clear` / `alarm_clear` / `runtime_healthy` /
`responsibility_cleared` = **全 True**。
Phase 6 CHARGE / DISCHARGE / PCS STOP = **0 / 0 / 0**（未送出任何 setpoint）。

## SSH VERB COUNTS

```
probe 10   loopcheck 21   pause 1   restore 1   status 0
```

## TIMING EVIDENCE（累積 n = 2，仍 NOT FINAL）

| 動詞 | Trial #1 | Trial #2 |
|---|---|---|
| `pause` | 2.068 s | **0.573 s** |
| `restore` | 0.610 s | **0.573 s** |

🔴 `pause` / `restore` timeout 維持 **FIELD_VALIDATION_CANDIDATE 12.0 / NOT FINAL**。
production default 仍為 `None`（`SSH_TIMEOUT_STATUS['pause'] = STRUCTURAL_CANDIDATE`）。
本次**只增加 timing evidence，未升格**。

## STEP 10 — FINAL SAFETY RESET（已驗證）

```
DISPATCH_ENABLED = False      MODE = DRY_RUN
RemoteSenders().armed = False SshTransport().armed = False
SshTimeouts().command_timeouts['pause'|'restore'] = None
DEPLOYED_GUARD_VARIANT = B2   FIRST LIVE = HOLD
```

## 證據限制（必須與 PASS 一併記錄）

1. **n = 1 discriminative run**。
2. pause 恰好落在 external `DISCHARGING` 的**轉態邊緣**，
   因此**不能**單憑功率由 −69.1 → −1.4 kW 宣稱 pause 造成停止；
   本次的鑑別力來自 **loop marks 凍結**（normal control loop halted），
   而非功率變化本身。
3. `actual_active_power_kw` 仍標記 **LOW DISCRIMINATIVE VALUE**。
4. **Trial #2 PASS ≠ FIRST LIVE READY。** FIRST LIVE 維持 HOLD。


---

# TRIAL #2 EVIDENCE CLOSURE（2026-09-18）

```
TRIAL #2 HANDOFF TECHNICAL RESULT
    = PASS

TRIAL #2 FIELD VALIDATION CLOSURE
    = PASS WITH PROCEDURE DEVIATION AND OPEN EVIDENCE ITEMS
```

> 前一節「TRIAL #2 — FIELD EXECUTION RESULT」的技術數據**完全未變更**。
> 本節只補上程序與證據層的閉合結果。

## 1. EXECUTION PROCEDURE DEVIATION —— baseline window（歷史，保留）

| 項目 | 值 |
|---|---|
| 當次 field authorization | 要求**完整 600 sec** observation window |
| 實際執行 | **365 sec / 15 baseline samples** |
| 原因 | `Trial2Runner.run()` 於 `ACTIVE_BASELINE_ESTABLISHED` 後立即 `break`（harness early-success contract，離線已由 T1/T2/T3 驗證） |
| `observe_duration_sec` 傳入值 | **600.0**（artifact 記錄一致）—— **參數未被縮短** |
| artifact 記錄 | `{'observe_duration_sec': 600.0, 'sample_interval_sec': 20.0, 'min_loop_observations': 3, 'max_samples': 31}` |

```
分類 = EXECUTION PROCEDURE DEVIATION
```

當次授權文字本身存在衝突（「必須跑完 600 s」vs「保持既有 harness contract / 禁止臨時改流程」），
執行時選擇了不改 harness，因而發生 early termination；**執行前未先提請裁示**。

**是否影響技術有效性 —— 不影響核心鑑別力：**

- active evidence（baseline#15，09:00:28，`DISCHARGING` / `EXTERNAL_OR_UNKNOWN` / −44.0 kW）
  距 pause 送出（09:00:33）**僅 5 秒**，時間鄰接性為最強
- pause 後 PauseVerifier 第 1 筆（09:00:54）仍見 `DISCHARGING` / −69.1 kW，**第二次獨立證實**
- `normal_loop_halted` 來自 loop marks 凍結，**與 baseline 長度無關**
- Active Gate 條件未被放寬

**受影響之處**：active evidence 僅出現於 baseline 最後 1 筆，前 14 筆皆 `STANDBY` / `IDLE` ——
本次是「捕捉到 active 轉態瞬間」，**不是**「持續 active 窗口中的 pause」。

## 2. FIELD EVIDENCE LIMITATION —— SOC `SOURCE_COHERENCE = UNRESOLVED`

**已確認一致（CONFIRMED）**

| 項目 | 證據 |
|---|---|
| endpoint | 兩次皆 `EP_MAINCTL = /hmiGuest/unauthorizedAccess/overview/mainControlCollectsInformation` |
| SOC mapping | `charge_discharge_report.py:485` → `r["soc_percent"] = _to_float(main.get("rackSoc"))` |
| device selector | **不存在** —— 端點零參數，結構上不可能指向不同機櫃 |
| external controller | 兩次 probe 皆 `pid = 44728`，同一行程 |
| 程式路徑 | 兩次皆 `FieldSources.sample()` → `EssSnapshotAdapter.observe()` → `CDR.read_all(client)` |

**無法確認（UNRESOLVED）**

```
raw_source_time 未進 Trial #2 artifact
2 % → 98 % 實際原因 = NOT DETERMINABLE
```

🔴 **2026-09-18 09:30 唯讀複查（TASK 6，純 HMI GET，SSH 動詞 0、控制指令 0）**：

| # | `raw_source_time` | `source_age_sec` | state | HTTP read | `adapter_stale` |
|---|---|---|---|---|---|
| 1–5 | `''`（空字串） | `None` | **`SOURCE_TIME_UNAVAILABLE`** | 0.483 ~ 1.937 s | `False` |

→ `EP_MAINCTL` 的 `createTime` / `collectTime` **設備端一律為空**。
這不是 harness 漏存，而是**設備本身不提供 source timestamp**。

```
SOURCE_COHERENCE = UNRESOLVED（且此端點結構上無法補齊）
分類 = HISTORICAL EVIDENCE LIMITATION
```

依裁示保留為歷史限制 —— **不為了還原歷史原因再次控制設備**。

🔴 同時確立：`EssSnapshotAdapter.age_sec` / `stale` 是由**讀取開始時刻**推算，
衡量的是 **HTTP 往返延遲**。上表 `adapter_stale` 全為 `False`（讀取很快），
但 device-side 新鮮度 **完全未知** —— 兩者**不得互相冒充**。

evidence：`output/phase6_field_validation/source_freshness_20260918_093020.json`

## 3. ARTIFACT IDENTITY = **CLOSED**

| 項目 | 結果 |
|---|---|
| Trial #2 artifact | `trial2_20260918_085422.json` —— **副檔名確為 `.json`**，21,685 bytes |
| 目錄中 `*.js` | **0 個**（先前引述的 `.js` 為換行截斷） |
| `trial2_step0_20260917_175703.json` | filename = **執行時刻**（policy 正確，非 typo）；為**前一日**的獨立 STEP 0 evidence |
| 本次真正的 fresh preflight | `Trial2Runner` 內 `1_SAFETY_PREFLIGHT` @ **`2026-09-18 08:54:29`**，`safety_preflight_ok = True`，**15 / 15 PASS，0 項未過** |

## 4. 計數語意閉合 —— `sample_count = 15` vs 報告中的 `sample #16`

**既非 A 亦非 B（artifact 無 off-by-one）。**

| 來源 | 計數方式 |
|---|---|
| artifact `sample_count = 15` | 只數餵進 `ActiveBaselineObserver` 的 baseline 樣本 |
| 執行腳本日誌 `sample#16` | 該腳本的計數器把 runner step 1 的 **safety-preflight 取樣**也編為 `sample#1` |

驗證：`active_baseline.samples` 長度 = 15，唯一帶 `hits` 的是 **baseline#15**
（`['pcs_discharging', 'authority_external']`）。log 的 `sample#2 ~ #16` 正好對應 baseline#1 ~ #15。

```
分類 = RUN-SCRIPT LOG NUMBERING ARTIFACT（非 HARNESS EVIDENCE BUG）
```

**已實作的防再誤讀措施**（harness evidence layer，不碰 production）：
`baseline_sample_count` / `observing_sample_count` /
`terminal_active_sample_index` / `counts_exclude_preflight_sample`。

## 5. 本輪變更範圍

| 檔案 | 變更 |
|---|---|
| `test/phase6_trial2_harness.py` | 新增 source freshness evidence 層、baseline window policy 常數、計數語意欄位；schema v1 → **v2**（v1 歷史 artifact 仍可驗證） |
| `test/test_phase6_trial2_harness.py` | 新增 F1 ~ F10，**184 → 257 / 257 PASS** |

**未修改**：Phase 6 decision semantics、production arbiter、production safety policy、
ESS production control logic、`PauseVerifier`、`RestoreVerifier`、SSH taxonomy、
responsibility 生命週期、任何 production 參數。

## 6. 升級為 `CLOSED WITH DOCUMENTED PROCEDURE DEVIATION` 的剩餘條件

| # | NEXT GATE 項目 | 狀態 |
|---|---|---|
| 1 | README / Field doc 修正 | ✅ **本輪完成** |
| 2 | `sample_count` 語意閉合 | ✅ **本輪完成** |
| 3 | `raw_source_time` evidence 欄位實作 | ✅ **本輪完成**（schema v2） |
| 4 | source freshness dedicated offline tests | ✅ **本輪完成**（F1 ~ F10） |
| 5 | 純 read-only source freshness verification | ✅ **本輪完成** —— 結果為 `SOURCE_TIME_UNAVAILABLE` |

🔴 第 5 項雖已執行完成，但**結果是設備不提供 timestamp**，
因此 `SOURCE_COHERENCE` **無法**由本端點升級為 CONFIRMED。

---

# TRIAL #2 FINAL STATUS（2026-09-18 使用者正式裁示）

```
TRIAL #2 HANDOFF TECHNICAL RESULT   = PASS
TRIAL #2 FIELD VALIDATION CLOSURE   = CLOSED WITH DOCUMENTED PROCEDURE DEVIATION
DISCRIMINATIVE_PAUSE_VERIFIED       = TRUE
```

> `PASS WITH PROCEDURE DEVIATION AND OPEN EVIDENCE ITEMS` 已**停止使用** ——
> evidence investigation 已完成，open item 轉為 **DOCUMENTED / ACCEPTED LIMITATION**。

## DOCUMENTED PROCEDURE DEVIATION（不得回溯刪除）

```
EXECUTION PROCEDURE DEVIATION = BASELINE EARLY-ACTIVE TERMINATION
```

2026-09-18 Trial #2 baseline：授權文字要求**完整 600 sec**，實際約 **365 sec**。
原因：現有 harness 在 `ACTIVE_BASELINE_ESTABLISHED` 後 early break。

🔴 **不得回溯刪除，也不得改寫成「完全符合程序」。**

## FUTURE BASELINE POLICY（不回溯）

```
BASELINE_WINDOW_POLICY = MAX_OBSERVATION_WINDOW
observe_duration_sec   = maximum observation window（600 sec 不是 mandatory dwell）
窗口內 ACTIVE_BASELINE_ESTABLISHED -> EARLY SUCCESS，立即保存 terminal active evidence
直到 max window 仍無 active        -> ABORTED_NO_ACTIVE_BASELINE / NO PAUSE SEND
```

🔴 **不得**用此 policy 回頭消除 2026-09-18 的 procedure deviation。

## SOURCE COHERENCE FINAL CLASSIFICATION

🔴 **不得寫 `SOURCE_COHERENCE_CONFIRMED`** —— source-side freshness 無法確認。

| 項目 | 正式分類 |
|---|---|
| endpoint identity | **CONFIRMED** |
| field mapping（`rackSoc`） | **CONFIRMED** |
| device selector mismatch | **NOT PRESENT** |
| external process identity（pid 44728） | **CONFIRMED** |
| **SOURCE-SIDE TIMESTAMP** | **UNAVAILABLE** |
| **SOURCE-SIDE FRESHNESS** | **NOT DETERMINABLE** |

`EP_MAINCTL` 的 `createTime` / `collectTime` 實測皆為空字串 →
`SOURCE_TIME_UNAVAILABLE` 是**設備資料來源本身的限制，不是 harness 漏存**。

## SOC 2 % → 98 %

```
分類 = HISTORICAL EVIDENCE LIMITATION
```

| | |
|---|---|
| **已知** | 相同 endpoint、相同 `rackSoc` mapping、無 device selector、相同 external controller process |
| **未知** | 14 小時期間 SOC 實際變化原因 |

🔴 **不得推測** overnight charging 或其他原因，除非未來有直接 evidence。

## SAMPLE COUNT CLOSURE（正式記錄）

```
artifact baseline_sample_count = 15
terminal active sample         = baseline #15
run-script 的 sample #16       = 含先前 safety-preflight sample 編號

分類 = RUN-SCRIPT LOG NUMBERING ARTIFACT
     ≠ off-by-one bug   ≠ HARNESS EVIDENCE BUG
```

保留欄位：`baseline_sample_count` / `observing_sample_count` /
`terminal_active_sample_index` / `counts_exclude_preflight_sample`。

## SOURCE FRESHNESS EVIDENCE LAYER（採納 schema v2）

```
raw_source_time   source_time_available   source_age_sec   source_freshness_state
states: SOURCE_FRESH / SOURCE_STALE / SOURCE_TIME_UNAVAILABLE / SOURCE_TIME_MALFORMED
```

🔴 HTTP latency / adapter age **不得**當成 device-side freshness。
歷史 schema v1 artifact 保持相容，**不重寫歷史 artifact**。

## REGRESSION BASELINE（採納）

```
Trial #2 dedicated    = 257 / 257 PASS
Phase 6.10 dedicated  = 1173 / 1173 PASS
Full Phase 6          = 5229 / 5229 PASS，52 files，0 FAIL
```


---

# FIRST LIVE ↔ PHASE 6.10 INTEGRATION（2026-09-18，OFFLINE ONLY）

```
FIRST LIVE PATH                = PHASE 6.10 HANDOFF PATH（P3 裁示）
FIRST LIVE INTEGRATION         = OFFLINE VALIDATION COMPLETE
FIRST LIVE EXECUTION READINESS = READY FOR AUTHORIZATION
FIRST LIVE                     = HOLD（未授權、未執行）
```

## 為什麼改路徑

Trial #2 實測：external 可能 `PCS=STANDBY` / `Authority=IDLE`（連續 14 筆），
但 normal loop marks 持續 advancing，第 15 筆自行轉入 `DISCHARGING` / `EXTERNAL_OR_UNKNOWN`。

```
「external 當下 idle」 ≠ 「external 已 relinquish control」
6.9 原 Phase A = TOCTOU / ownership race
```

## 交付物

| 檔案 | 內容 |
|---|---|
| `test/phase6_first_live_handoff.py` | 整合層：dispatcher / authority lease / command-count guard / lifecycle recorder / handoff execution artifact / 兩層 preflight |
| `test/test_phase6_first_live_handoff.py` | 專屬離線回歸 **171 / 171 PASS** |

`phase6_first_live_runner.py`（6.9）**未刪除、未重寫** ——
既有 5 kW contract / Safety Gate / ReadBack / Authorization / execution chain /
CLI confirmation / live-leg logic 全部保留，定位改為 **FIRST LIVE DISPATCHER / LIVE LEG**。

## Dispatcher 介面（P5）

```
(observation) -> {"dispatched", "verified", "released", "outcome", "reason",
                  "live_leg", "audit", "action"}

DISPATCH_NOT_ATTEMPTED  authority 未取得 / 未進入送出流程
DISPATCH_SUCCEEDED      leg_state == COMPLETE
DISPATCH_FAILED         已送出方向指令但 leg 以其他終態結束
DISPATCH_UNKNOWN        ABORTED_UNCERTAIN_OWNERSHIP / 例外 / 未知終態
```

🔴 只有 `DISPATCH_SUCCEEDED` 才算 `verified`。**UNKNOWN 一律不得當成通過。**
🔴 dispatcher 內未出現 `charge_power_kw` / `max_power_kw` / `build_executor` /
`operator_run` / `run_live_leg_cli` 等識別字（由 N2 AST 鎖住）。
🔴 模組層未 import `device_control_operator` / `pcs_control_executor` /
`decision_engine` / `safety_gate` 等（N3），且未出現 `5.0` / `150.0` 常數。

## Ownership 分離與順序

```
1  6.10 pause external
2  PauseVerifier VERIFIED（＋ normal loop halted）
3  Phase 6 acquire authority        Phase6AuthorityLease.acquire()
4  run 既有 5 kW live leg
5  Phase 6 release authority        finally 保證
6  6.10 restore external
7  RestoreVerifier RV_RESUMED       → 責任才可清除
```

`LifecycleRecorder` 以單調 seq 記錄事件，`release_before_restore` 即
`PHASE6_AUTHORITY_RELEASED` 先於 `EXTERNAL_RESTORE_ATTEMPTED` 的證明。
**成功 / 失敗 / UNKNOWN / authority 取得失敗 四條路徑皆已鎖住**（I8）。

## Rollback 契約

| 情境 | 行為 |
|---|---|
| pause `SEND_SENT` 或 `SEND_UNKNOWN` | responsibility = TRUE，直到 `RV_RESUMED` |
| Phase 6 acquire 失敗 | 不送任何指令 → 仍 restore external |
| dispatch 失敗 | Phase 6 release → restore external |
| dispatch UNKNOWN | **不重試 charge** → release → restore |
| 任何例外 | Fail Closed，authority 仍於 `finally` 釋放，責任保留 |
| audit writer 例外 | NON_GATING_CONTINUE，不影響控制流程 |

> ⚠️ `HandoffRunner` 的 `outcome = "COMPLETE"` 意思是
> **「交接生命週期已收斂（external 已歸還）」**，**不是**「FIRST LIVE leg 成功」。
> leg 成敗一律看 `steps["8_VERIFY"]` 與 `dispatcher_outcome`。

## Command count guard

```
phase6_charge   <= 1      phase6_discharge == 0      phase6_pcs_stop == 0
pause           <= 1      restore          <= 1
live_leg_internal_stop    另計（既有 leg lifecycle 收尾 STOP，非獨立指令）
```
超額一律拋 `CommandLimitExceeded`（不回 False）。

## 兩套 evidence（P6）

**P6-A｜Durable Decision Audit** —— 沿用既有 46 欄 schema，**未新增任何欄位**。
dispatcher 透過 `build_first_live_audit_writer()` 把 `execution_result` /
`observation` / `local_now` 餵給既有 `SVC.write_audit()`。`sink=None` 即完全不寫檔。

**P6-B｜`PHASE6_10_HANDOFF_EXECUTION_ARTIFACT` v1（34 欄）** —— ownership / handoff evidence。
`validate_handoff_artifact()` 會擋下：command count 超限、非法 dispatcher outcome、
`release_before_restore` 不成立、責任被清除但未達 `RV_RESUMED`、timeout 被標成 FINAL。

🔴 兩套 schema **刻意不揉合**。

## 離線驗證結果

`test_phase6_first_live_handoff.py` = **171 / 171 PASS**（I1~I18 ＋ N1~N5）

| 情境 | 結果 |
|---|---|
| pause verified | dispatcher 恰好 1 次、leg 1 次 |
| pause 未 verified（PCS 仍 CHARGING） | dispatcher **0 次**、authority 未取得 |
| pause `SEND_UNKNOWN` | dispatcher **0 次**、pause 不重送、責任維持 |
| dispatch success / fail / unknown | 三者皆 release → restore 各 1 次 |
| authority acquire 失敗 | 無任何 Phase 6 指令，external 仍 restore |
| restore `SEND_UNKNOWN` | 不重送、責任保留 |
| `RV_RESUMED` | 責任清除 |
| `DISPATCH_ENABLED=False` | dispatcher 0 次，仍完成 restore |
| `armed=False` | control verb `can_run` 全 False，runner 未被呼叫 |
| 例外 | UNKNOWN、authority 仍釋放、charge 不重試 |

## Regression

```
Integration dedicated = 171 / 171 PASS
Phase 6.10 dedicated  = 1173 / 1173 PASS（11 檔）
FIRST LIVE dedicated  = 196 / 196 PASS（2 檔）＋ 6.9 220 / 220（2 檔）
Full Phase 6          = 5400 / 5400 PASS，53 files，0 FAIL
```
（5229 + 171 = 5400；52 + 1 = 53。**未修改任何既有斷言。**）

## 未變更項目（明文確認）

Phase 6 decision policy / TOU logic / SOC policy / Safety Gate semantics /
`charge_power_kw = 5.0` / `max_power_kw = 150.0` / `PauseVerifier` 9 項 criteria /
`RestoreVerifier` 3 筆 3 s / deployed remote guard / external controller /
Durable Decision Audit 既有 46 欄 / `verify_pause(probe["running"])` /
`verify_restore(probe["running"])` latent dead path —— **全部未動**。

`SshTimeouts().command_timeouts['pause'|'restore']` 仍為 **None**；
12.0 s 仍為 **FIELD_VALIDATION_CANDIDATE / NOT FINAL / n = 2**。

## 待裁示

1. `phase6_first_live_runner.py` 仍可被直接執行（其 Phase A 為舊 TOCTOU 判斷）。
   是否要為該檔加上「直接執行阻擋 / 導向 handoff path」的閘門？
2. 正式 field entry 腳本（等同 Trial #2 的 `trial2_run.py`）於執行授權時再組裝，
   沿用本整合層＋既有 live leg，不另建第三套流程。


---

# RULING 1 / RULING 2 CLOSURE（2026-09-18）

## RULING 1 —— FIRST LIVE FIELD ENTRY POINT = PHASE 6.10 HANDOFF PATH ONLY

```
正式唯一 field path：
    Phase 6.10 Handoff -> FIRST LIVE Dispatcher -> existing 6.9 live leg
```

**被封鎖的兩個 CLI field entry**

| 模組 | 封鎖點 | 行為 |
|---|---|---|
| `phase6_first_live_runner.py` | `__main__` → `refuse_direct_execution()` | 印拒絕訊息、**exit 2**、**不呼叫 `main()`** |
| `pcs_auto_control_service.py` | `main()` 內 `--live-leg` 判斷之**最前**（不看 `--confirm`） | 印拒絕訊息、**exit 2** |

兩者訊息一致：
`FIRST LIVE direct execution disabled. Use Phase 6.10 handoff entry point.`

🔴 **無任何 bypass**：argparse 未註冊 `force` / `bypass` / `override` / `allow_direct`；
`refuse_direct_execution()` 在被呼叫時就已決定拒絕，不看 argv、不看環境變數。
AST 確認它未引用 `acquire_ownership` / `run_live_leg_cli` / `build_executor` /
`build_api_client` / `main`。

🔴 **被限制的是 FIELD ENTRY POINT，不是刪除 6.9 implementation**：
`phase6_first_live_runner.main()`、`SVC.live_leg_main()`、`SVC.run_live_leg_cli()`
函式本體**完全未動**，整合層與離線測試仍可 import 後直接呼叫。

### 既有斷言變更（OLD / NEW / WHY / EVIDENCE）

`test_phase6_69a_cli_entry.py`

| | |
|---|---|
| **OLD** | `check("main() 只在一處呼叫 live_leg_main", calls.count("live_leg_main") == 1)` |
| **NEW** | `main()` **完全不呼叫** `live_leg_main`（`calls.count(...) == 0`），並新增 `LIVE_LEG_CLI_BLOCKED` / exit code / 函式仍存在三項斷言 |
| **WHY** | RULING 1 —— `--live-leg` 會繞過 external pause / normal-loop halt / PauseVerifier / restore responsibility，形成 TOCTOU / ownership race |
| **EVIDENCE** | R4 / R4b：CLI exit ≠ 0、未進入 live leg 流程、`live_leg_main` 與 `run_live_leg_cli` 仍可呼叫 |

🔴 這是**收緊**（送出可能性只減不增），不是為了湊數字改斷言。

## RULING 2 —— PCS STOP 必須計入 aggregate

```
OLD: phase6_pcs_stop == 0   （live_leg_internal_stop 被當成豁免）
NEW: phase6_pcs_stop <= 1   （包含 live-leg cleanup STOP）
     live_leg_internal_stop -> subtype / reason，不是豁免
     pcs_stop_reason = LIVE_LEG_CLEANUP
```

**未來若 FIRST LIVE 執行一次完整 leg**

```
CHARGE = 1      DISCHARGE = 0      PCS STOP = 1
live_leg_internal_stop = 1         pcs_stop_reason = LIVE_LEG_CLEANUP
```
🔴 **不得**寫成 `PCS STOP = 0`。

⚠️ 允許 `≤ 1` **不代表**可新增任何新的 STOP 路徑 —— 只認既有 6.9 live-leg
lifecycle 本來就存在的 cleanup STOP。本次僅為 **COUNTING / EVIDENCE SEMANTICS**
修正，**未動** live-leg STOP policy / timing / trigger / PCS control semantics。

### Artifact validation 調整

`validate_handoff_artifact()` 新增擋下：

- `phase6_pcs_stop > 1` / `phase6_charge > 1` / `phase6_discharge > 0` → INVALID
- `phase6_pcs_stop ≥ 1` 但缺 `pcs_stop_reason` → INVALID
- `live_leg_internal_stop > phase6_pcs_stop`（cleanup STOP 被排除）→ INVALID

Durable Decision Audit 仍為**既有 46 欄 schema，未因本次修改**。
command aggregate counts 與 PCS STOP subtype / reason 一律由
6.10 execution artifact 負責。

## FIRST LIVE OUTCOME 語意（三者並存）

```
handoff_outcome     交接生命週期是否安全收斂（external 已 restore）
dispatcher_outcome  FIRST LIVE 是否成功（只有 DISPATCH_SUCCEEDED 才算）
live_leg_outcome    既有 leg 終態、dispatch / stop 次數
```
🔴 `handoff_outcome = COMPLETE` **不等於** FIRST LIVE CHARGE PASS。

## Dedicated tests

`test_phase6_first_live_handoff.py` = **239 / 239 PASS**（I1–I18、N1–N5、R1–R9、R4b）

| | 情境 | 結果 |
|---|---|---|
| R1 | 三種 argv 直接 CLI 執行 | exit 2、拒絕訊息 |
| R2 | 不送指令 / 不 acquire authority / 無 bypass 參數 | 全數確認 |
| R3 | live leg 仍可 import 並經 dispatcher 使用 | PASS |
| R4 / R4b | repo 內含 live-leg CLI 的模組皆已登記並封鎖 | PASS |
| R5 | 成功 leg + cleanup STOP | 1 / 0 / 1 / subtype 1 |
| R6 | 未進 live leg | 0 / 0 / 0，`pcs_stop_reason = None` |
| R7 | charge>1 / discharge>0 / pcs_stop>1 | artifact reject |
| R8 | cleanup STOP 被排除、或缺 reason | 皆被抓出 |
| R9 | handoff COMPLETE 但 dispatcher FAILED | FIRST LIVE 不得標 PASS |

## Regression

```
FIRST LIVE integration dedicated = 239 / 239 PASS
FIRST LIVE existing dedicated    = 264 / 264 PASS（2 檔）＋ 6.9 222 / 222（2 檔）
Phase 6.10 dedicated             = 1173 / 1173 PASS（11 檔）
Full Phase 6                     = 5470 / 5470 PASS，53 files，0 FAIL
```
（5400 + 68 + 2 = 5470。既有斷言只有 `test_phase6_69a_cli_entry.py` 一處變更，
已附 OLD / NEW / WHY / EVIDENCE，方向為收緊。）


---

# FIRST LIVE ATTEMPT #1（2026-09-18）

```
RESULT = ABORTED_CURRENT_CONDITIONS
分類   = EXPECTED FAIL-CLOSED SAFETY ABORT
       不是 FAIL、不是 CODE DEFECT、不是 HANDOFF FAILURE
```

| 項目 | 值 |
|---|---|
| 授權 | 使用者 2026-09-18 授權**單次** Controlled Field Attempt（CHARGE 5.0 kW，ONE LEG ONLY） |
| 執行 | 2026-09-18 11:08 |
| evidence | `output/phase6_live_charge/first_live_step12_20260918_110827.json` |

## STEP 0 — Source / Code State：確認通過

```
phase6_first_live_handoff.py      存在，為本次正式 entry path
DIRECT_FIELD_EXECUTION_BLOCKED    True   （phase6_first_live_runner.py）
LIVE_LEG_CLI_BLOCKED              True   （pcs_auto_control_service --live-leg）
```
**未**從舊 6.9 CLI 執行。

## STEP 1 — Fresh Safety Preflight：**11 / 11 PASS**

`communication_healthy` `source_available` `fault_clear` `critical_alarm_clear`
`screen_alive` `process_alive` `process_identity_ok` `ownership_not_both`
`guard_variant_b2` `mode_dry_run_baseline` `senders_not_armed_baseline`

| fresh 觀測 | 值 |
|---|---|
| PCS / Authority | `DISCHARGING` / `EXTERNAL_OR_UNKNOWN` |
| SOC | 47.0 % |
| fault / critical alarms | `False` / `0` |
| ownership | `EXTERNAL`（≠ BOTH） |
| loop marks | 3 筆，`advancing = True` |
| guard | `B2`，capability OK，pid 一致 |
| production config | charge 5.0 / discharge 5.0 / max 150.0（未變） |

> `EP_MAINCTL` 無 device-side timestamp —— 維持 **KNOWN FIELD EVIDENCE LIMITATION**，
> **未**寫成 SOURCE-SIDE FRESHNESS CONFIRMED。本次 fresh read 無 timeout /
> error / 欄位解析失敗 / 資料矛盾。

## STEP 2 — Current CHARGE Eligibility：**FAIL（正確 Fail Closed）**

以既有正式決策鏈（`build_live_chain_bundle`，鏈上 executor / verifier 皆 `None`）
暖機至分類器連續穩定 3 次後取得：

```
grid_state            = IMPORT
tou_state             = PEAK
soc_percent           = 47.0
pcs_state             = DISCHARGING
authority_state       = EXTERNAL_OR_UNKNOWN
authority_reason      = CONTROL_AUTHORITY_EXTERNAL
fresh_decision_action = discharge          ← 自然決策不是 charge
arbitration_outcome   = BLOCKED
reason                = CONTROL_AUTHORITY_BLOCKED
authorized / executed = False / False
```

**CAUSE**：`TOU = PEAK` ／ `natural action = DISCHARGE` ／
`authority = EXTERNAL_OR_UNKNOWN` → **5 kW CHARGE 不得執行**。

依授權明文「AUTHORIZATION TO ATTEMPT，不是 AUTHORIZATION TO FORCE EXECUTION」——
**未**修改 policy / SOC gate / TOU / 功率，**未**改成 DISCHARGE，
**未**偽造任何 prerequisite。

## STEP 3 以後 —— 全部未發生

STEP 2 未通過即中止：**未評估** handoff readiness、**未 arm**、**未 pause**、
**未取得** Phase 6 authority、**未 dispatch**、**未** restore。
`restore responsibility` 從未成立（pause 從未嘗試）。

## Command counts

```
本次 attempt   pause = 0   restore = 0
Phase 6        CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
本次 SSH 動詞  probe = 1   loopcheck = 3   status = 0
本次控制指令   0

External B2 lifecycle cumulative  pause / restore = 2 / 2（未變）
```

🔴 **不得**因為 FIRST LIVE 沒有真正 dispatch 而補送任何 command。

## Artifact

本次**未進入 handoff**，故**未**產生 `PHASE6_10_HANDOFF_EXECUTION_ARTIFACT`
（未發生的事不得造假紀錄），`validate_handoff_artifact()` 不適用。
Durable Decision Audit 亦無 FIRST LIVE 記錄（未 dispatch）；既有 46 欄 schema **未修改**。

## 結論與後續

```
FIRST LIVE Integration          = OFFLINE COMPLETE
FIRST LIVE EXECUTION READINESS  = READY FOR AUTHORIZATION
FIRST LIVE ATTEMPT #1           = ABORTED_CURRENT_CONDITIONS
FIRST LIVE SUCCESSFUL FIELD LEG = NOT YET COMPLETED
Unattended LIVE                 = HOLD
```

Attempt #1 已證明 **current-condition gate 正確 Fail Closed**。
目前**沒有新的 code blocker**，因此**不修改** decision policy / TOU / SOC gate /
Safety Gate / handoff / dispatcher / live leg / timeout，**不新增** workaround 或 retry logic。

**Attempt #2 條件**：新的 fresh observation 顯示 formal natural decision = `CHARGE`，
且 TOU / SOC / Safety Gate / Authority / communication / fault / alarm 全部符合既有正式 policy。
🔴 不自動 retry、不 polling 後自行送出、不建立 scheduled auto retry；
必須由使用者重新明確授權單次 Controlled Field Attempt，
並一律從 **STEP 0 / Fresh Preflight 重新開始**，
**不得沿用**本次的 preflight / decision / Authority / loop evidence。


---

# P0 FLOW CORRECTION —— PRE/POST-HANDOFF ELIGIBILITY（2026-09-18，OFFLINE ONLY）

## Source inspection 結論

| 檢查 | 結果 |
|---|---|
| `first_live_safety_preflight`（LAYER 1）引用 authority？ | **否** ✅ |
| `pre_pause_gates`（HandoffRunner step 1）引用 authority？ | **否** ✅ |
| `post_pause_precheck`（step 6）引用 authority？ | **是** ✅（正確 —— 那是 post-pause） |
| 整合層是否已有明文 eligibility 函式？ | **否** ← 這是缺口 |

→ **production code 沒有把 `CONTROL_AUTHORITY_BLOCKED` 當成 pre-handoff CHARGE
policy blocker**。真正的風險在於：整合層沒有版控化的 eligibility 函式，
Attempt #1 的判定寫在 run-script 裡（判準本身正確，只看 `fresh_decision_action == "charge"`，
但同時醒目印出 `CONTROL_AUTHORITY_BLOCKED`，易被後續作者誤用）。

## 修正內容（只動 FIRST LIVE handoff integration layer）

### GATE A — `pre_handoff_policy_eligibility(sample)`

只回答「**如果** ownership 可以合法取得，目前正式策略是否應該執行 CHARGE？」

```
natural_decision_is_charge   tou_allows_charge      soc_allows_charge
fault_clear                  critical_alarm_clear   communication_healthy
required_data_available      pcs_state_legal        non_ownership_safety_ok
```

🔴 **完全不看 Control Authority**（AST 鎖住不引用 `authority` /
`arbitration_outcome` / `authorized` / `CONTROL_AUTHORITY_BLOCKED` /
`EXTERNAL_OR_UNKNOWN`）。

verdict：`CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED` 或 `CHARGE_POLICY_NOT_ELIGIBLE`。

### `handoff_requirement()` / `HANDOFF_ALWAYS_REQUIRED = True`

只輸出**為什麼仍需 handoff**的證據，**沒有**跳過分支 ——
留一條可跳過 handoff 的路，就是把 6.9 的 TOCTOU 漏洞再開一次。

證據欄位：`authority_external_or_unknown`、`external_loop_active`、
`authority_idle_is_not_relinquish_proof`（Trial #2 已證明 Authority=IDLE
不足以證明 external 已 relinquish）。

### GATE B — `post_handoff_revalidation(sample)`

```
fresh_reobservation          natural_decision_is_charge   tou_allows_charge
soc_allows_charge            fault_clear                  critical_alarm_clear
communication_healthy        pcs_state_legal              ownership_not_both
external_relinquished  (authority == IDLE)
```

位置：`FirstLiveDispatcher.__call__()` 內、**`acquire()` 之前**。
未通過 → 不 acquire、不送任何 Phase 6 command、`DISPATCH_NOT_ATTEMPTED`、
`reason = ABORTED_POST_HANDOFF_REVALIDATION`，由 `HandoffRunner` 走 restore path。

🔴 樣本未標記 `post_handoff_fresh` → Fail Closed（**不得沿用** pause 前樣本）。

### 正式流程

```
GATE A（pre-handoff policy eligible）
  → handoff readiness
  → pause ONE ATTEMPT
  → normal loop halted + PauseVerifier VERIFIED
  → fresh re-observation
  → GATE B（post-handoff revalidation）
  → Phase 6 acquire authority
  → dispatcher
```

## Attempt #1 不重分類

Attempt #1 的 `natural decision = DISCHARGE`，因此即使移除 authority 因素，
`CHARGE_POLICY_ELIGIBLE = False` 仍成立 → 結果維持
**`ABORTED_CURRENT_CONDITIONS`**。**歷史結果未修改。**

## 未變更項目

production decision semantics / TOU policy / SOC policy / Safety Gate core semantics /
Control Authority core classifier / 5 kW target / `max_power_kw` /
`PauseVerifier` / `RestoreVerifier` / deployed guard / Durable Decision Audit 46 欄
—— **全部未動**。

## Dedicated tests（P1 ~ P10）

`test_phase6_first_live_handoff.py` = **324 / 324 PASS**（239 → +85）

| | 情境 | 結果 |
|---|---|---|
| P1 | decision DISCHARGE + authority EXTERNAL | pre-handoff NOT ELIGIBLE，pause 0 次 |
| P2 | decision CHARGE + authority EXTERNAL | ELIGIBLE，**HANDOFF_REQUIRED** |
| P3 | decision CHARGE + authority IDLE 但 loop active | 仍 **HANDOFF_REQUIRED**；AST 鎖住 GATE A 不看 authority |
| P4 | post-handoff 仍 CHARGE | acquire + dispatch 成功，charge = 1 |
| P5 | post-handoff 變 DISCHARGE | no dispatch → restore |
| P6 | post-handoff 變 NO_ACTION | no dispatch → restore |
| P7 | post-handoff fault / critical / 未 relinquish / ownership BOTH / 未標 fresh | 全數 Fail Closed |
| P8 | GATE B 過但 acquire 失敗 | no dispatch → restore，release 先於 restore |
| P9 | 六種 post-handoff abort | Phase 6 counts 全部 **0 / 0 / 0**，artifact validate 無問題 |
| P10 | RV_RESUMED / 未達 RV_RESUMED | 責任清除 / 責任保留 |

## Regression

```
FIRST LIVE integration = 324 / 324 PASS
FIRST LIVE dedicated   = 349 / 349（2 檔）＋ 6.9 222 / 222（2 檔）
Phase 6.10 dedicated   = 1173 / 1173（11 檔）
Full Phase 6           = 5555 / 5555 PASS，53 files，0 FAIL
```
（5470 + 85 = 5555；**未修改任何既有斷言**。）


---

# SCHEDULED FIRST LIVE（ONE-SHOT）—— OFFLINE IMPLEMENTATION（2026-09-21）

```
SCHEDULED FIRST LIVE ONE-SHOT  = OFFLINE IMPLEMENTATION / VALIDATION
SCHEDULED FIRST LIVE READINESS = NOT READY（host blocker）
FIRST LIVE / Unattended LIVE   = HOLD
```

🔴 **定位**：一次性 commissioning runner。
**不是** Unattended LIVE、**不是** recurring schedule、**不是**每日尖離峰自動控制、
**不是**背景永久排程服務、**不是** auto retry。

## 交付物

| 檔案 | 內容 |
|---|---|
| `test/phase6_scheduled_first_live.py` | one-shot 授權 / 時間窗 / durable ledger / crash recovery / self disarm / result 分類 |
| `test/test_phase6_scheduled_first_live.py` | 專屬離線回歸 **133 / 133 PASS** |

## 架構（orchestration shell，不複製任何既有邏輯）

```
Scheduler / One-Shot Gate
  → Fresh STEP 0 → Fresh Safety Preflight → GATE A
  → 既有 Phase 6.10 Handoff
       pause → loop halted + PauseVerifier → Fresh GATE B
       → Phase6AuthorityLease → FIRST LIVE Dispatcher → 既有 6.9 5 kW live leg
       → cleanup STOP → release → restore → RestoreVerifier
  → post-check → artifact → SELF DISARM / EXIT
```
AST 鎖住未 import `decision_engine` / `decision_policy` / `safety_gate` /
`pcs_control_executor` / `device_control_operator` / `pcs_auto_control_service` /
`control_authority` / `tou_calendar` / `subprocess` / `requests`，
且未呼叫 `run_live_leg_cli` / `live_leg_main` / `build_executor` /
`acquire_ownership` / `classify_tou`。

## One-shot state machine

```
NOT_STARTED → PREFLIGHT → EXTERNAL_PAUSE_RESPONSIBILITY → PHASE6_AUTHORITY
→ LIVE_LEG → PHASE6_RELEASED → RESTORE_PENDING → RESTORED → COMPLETE
```
`PENDING_RESTORE_LIFECYCLE`（5 個狀態）＝ external 可能仍被暫停。

## Time window / 授權消耗

| 情況 | 結果 |
|---|---|
| 早於窗 | 等待（`max_wait_sec` 超過即 `EXPIRED_NO_ATTEMPT`） |
| 窗內 | 執行**一次** |
| 晚於窗 | `EXPIRED_NO_ATTEMPT`，0 commands |
| 非授權日 | `WRONG_TARGET_DATE` |
| 窗內 GATE A 不成立 | `ABORTED_CURRENT_CONDITIONS`，ONE-SHOT 結束（不等下一筆 CHARGE） |

任何結果都寫 `CONSUMED`；再次啟動 → `ALREADY_CONSUMED`。

## Crash / restart recovery

- 意圖先落盤（`INTENT`）再執行；append-only JSONL ＋ 每筆 `fsync`
- 只要 `pause` INTENT 落過盤，責任即成立（即使 OUTCOME 未寫入）
- 只有 `restore_verified = RV_RESUMED` 才解除責任
- `recover()` → `RECOVERY_CLEAN` / `RECOVERY_PENDING_RESTORE` /
  `RECOVERY_ALREADY_CONSUMED` / `RECOVERY_CRITICAL_MANUAL`
- 🔴 **一律 NO RESEND pause / charge / restore**；UNKNOWN → Fail Closed
- 半截尾行被忽略，前序狀態不受影響

## Dedicated tests（S1–S20 ＋ X1–X5）= **133 / 133 PASS**

| | 情境 | 結果 |
|---|---|---|
| S1 | 窗前啟動 | 等待後執行**一次**；超過等待上限 → EXPIRED |
| S2 | 窗過期 / 非授權日 | EXPIRED / WRONG_TARGET_DATE，0 commands |
| S3 | GATE A fail（含 preflight / readiness / source） | no pause，0 commands，ONE-SHOT 消耗 |
| S4 | GATE A PASS | handoff 恰好一次，pause 意圖先落盤 |
| S5 | pause UNKNOWN / NOT_SENT | PAUSE_UNKNOWN / PAUSE_FAILED，責任保留 |
| S6–S7 | GATE B fresh / stale | fresh PASS；未標記 fresh → Fail Closed |
| S8–S9 | GATE B decision DISCHARGE / TOU 離開 OFF_PEAK | no charge → restore |
| S10 | authority acquire fail | no charge → restore |
| S11 | successful leg | charge 1 / discharge 0 / pcs_stop 1 / cleanup reason |
| S12–S13 | dispatcher / restore UNKNOWN | 各只一筆意圖，無 retry |
| S14–S16 | crash after pause / after charge / before restore | 不重送、責任可恢復、半截尾行不破壞狀態 |
| S17–S19 | restart / PASS 後 / ABORT 後再啟動 | 一律 `ALREADY_CONSUMED` |
| S20 | exception | EXCEPTION_FAIL_CLOSED，safety reset 仍執行，durable evidence 保留 |
| X1–X5 | 無重複邏輯 / 無寫死排程 / 定位常數 / host Fail Closed / production defaults 未變 | 全數鎖住 |

## Regression

```
Scheduled dedicated    = 133 / 133 PASS
FIRST LIVE integration = 349 / 349（2 檔）＋ 6.9 222 / 222（2 檔）
Phase 6.10 dedicated   = 1173 / 1173（11 檔）
Full Phase 6           = 5688 / 5688 PASS，54 files，0 FAIL
```
（5555 + 133 = 5688；53 + 1 = 54。**未修改任何既有斷言。**）

## 🔴 Windows host runtime precondition —— **BLOCKER**

2026-09-21 唯讀實測（**未修改任何設定**）：

| 項目 | 實測 | 判定 |
|---|---|---|
| `SUB_SLEEP / STANDBYIDLE` | **AC = 300 s**（DC = 180 s） | 🔴 **FAIL** —— 閒置 5 分鐘即睡眠 |
| `SUB_SLEEP / HIBERNATEIDLE` | AC = 0（停用） | PASS |
| 待處理重開機 | `CBS_RebootPending=False`、`WU_RebootRequired=False` | PASS |
| 網路 | Wi-Fi 325 Mbps（Up） | ⚠ 穩定性未驗證 |
| 睡眠狀態支援 | 待命 S0 / 休眠 / 快速啟動 皆可用 | 參考 |

→ 00:00~00:30 的 scheduled attempt 會在**等待期間被睡眠中斷**。
依裁示「不要本輪直接修改系統電源設定」，故僅回報，**未變更**。

```
HOST RUNTIME = NOT READY
```


---

# HOST RUNTIME READINESS ＋ FIELD CLASSIFICATION CORRECTION（2026-09-21 14:45）

## Windows Host Runtime —— 唯讀重新確認（**未修改任何電源設定**）

| # | 項目 | 實測 | 判定 |
|---|---|---|---|
| 1 | Active Power Scheme | `381b4222-f694-41f0-9685-ff5bb260df2e`（平衡） | — |
| 2 | **AC STANDBYIDLE** | **0 s → Never** | ✅ PASS |
| 3 | DC STANDBYIDLE | 180 s（維持原值，不要求修改） | — |
| 4 | **AC HIBERNATEIDLE** | **0 s → Never / disabled** | ✅ PASS |
| 5 | DC HIBERNATEIDLE | 2147483647 s（實質 Never） | — |
| 6 | Pending reboot | `CBS_RebootPending=False`、`WU_RebootRequired=False` | ✅ PASS |

⚠️ 補充：`PendingFileRenameOperations = True`。這是 Windows 常見的「下次重開機時
處理檔案改名」佇列，**不會主動觸發重開機**，兩個權威的 reboot-required 旗標皆為
False，故不視為 blocker；但據實記錄。

⚠️ 網路為 **Wi-Fi**（234~325 Mbps）。`network_stable` 與 `survives_unattended`
**無法由唯讀查詢直接證明**，執行時需由操作者提供 evidence
（`evaluate_host_runtime()` 對未知一律 Fail Closed）。

```
HOST RUNTIME READINESS = PASS
```

🔴 **RESTORE PLAN（不要現在還原）**
```
original_ac_sleep_sec = 300   （2026-09-21 09:xx 唯讀觀測值）
current_ac_sleep_sec  = 0     （Never）
```
Scheduled FIRST LIVE Attempt #2 完成後，**不論 PASS / ABORT / FAIL / EXPIRED**，
最終收尾都必須提醒：**RESTORE WINDOWS AC SLEEP → 300 s**。
🔴 host power setting 是**外部 prerequisite** ——
**不得**由 Phase 6 control runner 自行修改。

## FIELD CLASSIFICATION CORRECTION（使用者 2026-09-21 說明）

| 先前寫法 | 🔴 正式更正為 |
|---|---|
| `SCREEN_ALIVE_PROCESS_NOT_RUNNING`（原因未知） | **External Controller Current State = MANUALLY STOPPED BY OPERATOR** |
| 2026-09-21 14:09 CHARGING → `CONTROL ORIGIN = UNRESOLVED` | **14:09 Charge Origin = MANUAL OPERATOR ACTION** |

→ 這兩筆**不再**視為不明異常，也**不再**列為 FIRST LIVE blocker。
先前的 host read-only 調查指令單（`Phase6_10_Host_ReadOnly_Investigation_Commands.md`）
已加註更正，僅保留供未來出現**無法解釋**的 controller 狀態時使用。

## NEXT FIELD BASELINE（Scheduled FIRST LIVE 執行前必須重建）

```
1. 由使用者／現場以正常方式重新啟動 auto_control.py，並輸入 start
2. fresh verify：screen alive / process alive / process identity OK /
   loop marks advancing
3. 讓 auto_control.py 依**真實 OFF_PEAK** 自然進入 CHARGE
```
🔴 **不得**人工啟動 PCS CHARGE 作為 Scheduled FIRST LIVE 的 baseline。

## 狀態

```
HOST RUNTIME READINESS          = PASS
SCHEDULED FIRST LIVE ATTEMPT #2 = READY FOR FIELD SCHEDULING
  （≠ Task 已建立 ≠ Field 已授權 ≠ 已執行）
FIRST LIVE / Unattended LIVE    = HOLD
```

---

# SCHEDULED FIRST LIVE — CLI LAUNCHER（2026-09-21，OFFLINE ONLY）

```
TASK_LAUNCHER_MISSING  ->  RESOLVED
launcher = test/run_scheduled_first_live.py（132 / 132 PASS）
```

## Architecture —— 非常薄的 entry point

責任只有六件事：parse CLI / config → Fail Closed 驗證 → 組相依 →
呼叫既有 `phase6_scheduled_first_live` → 寫 artifact / exit code → safe exit。

🔴 模組層**只** import `argparse` / `datetime` / `importlib` / `json` / `os` /
`sys` ＋ `phase6_scheduled_first_live`。
**未** import `pcs_control_executor` / `device_control_operator` /
`pcs_auto_control_service` / `pcs_auto_control_production` /
`pcs_control_integration` / `phase6_remote_adapter` /
`phase6_handoff_orchestrator` / `phase6_first_live_handoff` /
`decision_engine` / `decision_policy` / `safety_gate` / `control_authority`
（L17 AST 鎖住）。

設備相依一律由 config 指名的 **dependency provider** 在執行時動態載入；
`provider.build(config)` 必須回傳含
`now_fn` / `source_state_check` / `safety_preflight` / `gate_a` /
`handoff_readiness` / `arm_and_run_handoff` 的 dict，
載入或組裝失敗即 **ConfigError → exit 2**。

## CLI

```
"<python.exe>" "<absolute launcher path>" --config "<absolute config path>"
"<python.exe>" "<absolute launcher path>" --config "<...>" --validate-only
```
🔴 **刻意不提供** `--force` / `--bypass` / `--now` / `--window` 覆寫參數。

## Config validation（全部 Fail Closed）

| 情況 | 結果 |
|---|---|
| 未給 `--config` / 檔不存在 / 壞 JSON / 最外層非 object | `CONFIG_INVALID`（exit 2） |
| 缺任一必填欄位（7 項） | `CONFIG_INVALID` |
| `target_date` / `window_start` / `window_end` 格式錯 | `CONFIG_INVALID` |
| `window_end <= window_start` | `CONFIG_INVALID` |
| `timezone` 無效（stdlib `zoneinfo` 驗證） | `CONFIG_INVALID` |
| `dependency_provider` 缺 / 無法載入 / build 失敗 / 相依不全 | `CONFIG_INVALID` |
| `enabled` 非**明確 `True`**（含 `"true"` / `1` / `None`） | `NOT_ENABLED`（exit 3） |

🔴 **不猜**預設日期 / 時區 / window，**不自動補** `enabled=true`。
🔴 **config 存在 ≠ field authorization** —— 一次性授權契約仍由
`OneShotAuthorization` ＋ durable ledger 負責。

## Path resolution

相對路徑一律以 **config 檔所在目錄**解析（**不看 cwd**），
因此 Task Scheduler 以任何工作目錄啟動結果相同。
已驗證含空白路徑（`D:\Crawler Sample`、`has space *` 暫存目錄）與
Task 風格完整 quoting。

## Exit code（穩定契約）

| code | 名稱 | 涵蓋 |
|---|---|---|
| **0** | `HANDLED_TERMINAL_RESULT` | PASS、EXPIRED、ABORTED_PREFLIGHT、ABORTED_CURRENT_CONDITIONS、ABORTED_HANDOFF_READINESS… |
| **2** | `CONFIG_OR_INVOCATION_INVALID` | config / provider 問題 |
| **3** | `REFUSED_...` | `ALREADY_CONSUMED` / `NOT_ENABLED` |
| **4** | `RECOVERY_REQUIRES_MANUAL_ATTENTION` | `PAUSE_UNKNOWN` / `DISPATCH_UNKNOWN` / `RESTORE_UNKNOWN` / `RESTORE_FAILED` / `EXCEPTION_FAIL_CLOSED` |
| **5** | `UNEXPECTED_LAUNCHER_FAILURE` | launcher 自身非預期錯誤 |

🔴 **不得**「只有 PASS 才 exit 0」—— 安全 ABORT 若回非 0，
Task Scheduler 會誤判成 launcher crash。

## Dedicated tests（L1–L17）= **132 / 132 PASS**

valid config / missing config / invalid JSON / missing field ×7 /
invalid timezone ×4 / invalid date ×5 / invalid window ×4 / enabled false ×4 /
already consumed（跨 process one-shot）/ independent cwd ×3 /
paths with spaces / no stdin（`stdin=DEVNULL` ＋ AST 無 `input()`）/
handled ABORT exit 語意（12 種 result → exit 對照）/ unexpected exception ×3 /
no duplicate control logic / no subprocess control path /
never imports executor·control modules。

## 順帶修正的 schema 缺口

`post_check` 原本**未登記**於 `HANDOFF_ARTIFACT_FIELDS`，
導致以 `HandoffExecutionArtifact.set(post_check=...)` 會拋 `KeyError`。
已**追加**該欄位，並讓 `collect_handoff_artifact(..., post_check=...)` 可寫入。
屬 additive，未更動任何既有欄位語意。

## Regression

```
Scheduled dedicated（core + launcher）= 265 / 265（2 檔）
FIRST LIVE integration               = 349 / 349（2 檔）
6.9 existing                         = 222 / 222（2 檔）
Phase 6.10 dedicated                 = 1173 / 1173（11 檔）
Full Phase 6                         = 5820 / 5820 PASS，55 files，0 FAIL
```
（5688 + 132 = 5820；54 + 1 = 55。**未修改任何既有斷言。**）

## 🔴 仍未具備：version-controlled field dependency provider

launcher 已可由獨立 Windows process 啟動，但 config 指名的
**field provider 模組尚未版控化** —— 目前真實 SSH / HMI / Meter / GATE A /
handoff 的接線仍只存在於 scratchpad wiring。
在該 provider 版控化之前，實際建立 Task 只會在 window 內得到
`CONFIG_INVALID`（exit 2）。


---

# FIELD DEPENDENCY PROVIDER FORMALIZATION（2026-09-21，OFFLINE ONLY）

```
Independent launcher + official field provider  ->  PASS
scratchpad 相依  ->  REMOVED
```

## 交付物

| 檔案 | 內容 |
|---|---|
| `test/phase6_first_live_field_provider.py` | 正式 field dependency provider（**WIRING ONLY**） |
| `test/phase6_guard_ssh_runner.py` | B2 guard 專用 SSH runner（硬性動詞 allowlist） |
| `test/test_phase6_first_live_field_provider.py` | P1–P28，**210 / 210 PASS** |

## Provider interface

```python
PROVIDER_ID = "phase6_first_live_field"

@dataclass FieldDependencies(now_fn, source_state_check, safety_preflight,
                             gate_a, handoff_readiness, arm_and_run_handoff,
                             collect_artifact, safety_reset, host_evidence, waiter)

validate_provider_definition(config) -> (ok, checks)      # PURE / OFFLINE
build_field_dependencies(config, runtime_context) -> FieldDependencies
build(config, runtime_context) -> dict                    # launcher 相容入口
```
typed dataclass，**不回大型 untyped dict**；`missing()` 可偵測缺件。

## Allowlist

```python
PROVIDER_ALLOWLIST = {"phase6_first_live_field": "phase6_first_live_field_provider"}
```
未登記 ID、任意 dotted module（`os`・`subprocess`・`some.random.module`）、
檔案路徑與相對路徑一律 **exit 2**。
launcher 內無 `spec_from_file_location` / `module_from_spec` / `exec_module` /
`exec` / `eval`。

## validate-only passive guarantee

以 tripwire 取代 `build_api_client` / `build_meter_client` / `build_ess_reader` /
`build_live_chain_bundle` / `acquire_ownership` / `run_live_leg_cli` /
`GuardSshRunner.__call__` —— validate 期間 **0 次觸發**。
AST 另證 validate 從未以 `armed=True` 建立 transport。
subprocess `--validate-only` 回報 `field_side_effects = 0`，
且**未建立** ledger / artifact。

## Wiring 對照（全部沿用既有實作）

| 面向 | 使用的既有元件 |
|---|---|
| HMI | `PRD.build_api_client` ＋ `PRD.build_ess_reader` ＋ `ADP.EssSnapshotAdapter`（**未新增第二套 scraper**） |
| Meter | `PRD.build_meter_client` ＋ `get_snapshot()`（**未新增第二套 protocol**） |
| B2 SSH | `RA.SshTransport` ＋ `phase6_guard_ssh_runner`；唯讀 transport `armed=False`，控制 transport 只在 `arm_and_run_handoff` 內建立 |
| Safety Preflight | `FL.first_live_safety_preflight` |
| GATE A | `FL.pre_handoff_policy_eligibility`；TOU / SOC 只**查** `DP.DECISION_MATRIX` |
| Handoff readiness | `FL.handoff_readiness` ＋ `US.remote_pause_capable` / `remote_restore_capable` |
| GATE B | `FL.build_post_handoff_revalidator` |
| Authority | `FL.Phase6AuthorityLease`（`SVC.acquire_ownership` / `release_ownership`） |
| Dispatcher | `FL.build_first_live_dispatcher` |
| Live leg | `SVC.run_live_leg_cli`（既有 6.9，**未重寫**） |
| Handoff | `HO.HandoffRunner` ＋ `FL.wrap_pause_sender` / `wrap_restore_sender` |
| artifact | `FL.collect_handoff_artifact` |

🔴 provider **未** import `pcs_control_executor` / `device_control_operator`；
未呼叫 `build_executor` / `build_verifier`；未硬編 TOU / SOC band 字面值；
未出現 `5.0` / `150.0`；未直接呼叫 `send_pause` / `send_restore` /
`request_pause` / `request_restore`。

## SSH contract

`phase6_guard_ssh_runner` 只接受 `RA.GUARD_ALLOWED_VERBS`
（`probe` / `loopcheck` / `status` / `pause` / `restore`），
其餘一律 `ArbitraryCommandRefused`；argv 最後一段只放**單一動詞**、
不經 shell（無 `shell=True`）、`stdin=DEVNULL`。
credential 只用既有 dedicated key **路徑**（不讀內容、不複製、不寫 artifact）；
缺件即 `CredentialMissing`，**不 fallback** 到一般 SSH / 其他 key / password 認證。
provider 本身**不 import** `subprocess`。

## Scratchpad dependency removal

`phase6_first_live_field_provider` / `phase6_guard_ssh_runner` /
`run_scheduled_first_live` / `phase6_scheduled_first_live`
皆**未引用** scratchpad / `trial2_sources` / `fl_step12` / 暫存目錄路徑。
全新獨立 process（`cwd = %TEMP%`）可完成 provider 驗證。

## Handoff artifact 36 -> 44（additive，非 Decision Audit 變更）

| 新增欄位 | 用途 |
|---|---|
| `pre_handoff_eligibility_ok` | GATE A 總判定 |
| `pre_handoff_eligibility_checks` | GATE A 9 項逐項結果 |
| `pre_handoff_verdict` | `CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED` / `..._NOT_ELIGIBLE` |
| `handoff_requirement_evidence` | 為何仍需 handoff 的證據 |
| `post_handoff_revalidation_ok` | GATE B 總判定 |
| `post_handoff_revalidation_checks` | GATE B 10 項逐項結果 |
| `post_handoff_verdict` | `POST_HANDOFF_REVALIDATION_OK` / `..._FAILED` |
| `post_check` | 收尾 post-check（原本已被 set 但**未登記**，導致 `KeyError`；本次補登記） |

🔴 這是 **handoff artifact schema 的 additive change**，**不是** Durable Decision
Audit schema change —— `phase6_decision_audit` 仍為 `SCHEMA_VERSION = 1`、
**46 欄未變**。**沒有**改既有欄位語意、**沒有**移除、**沒有** rename；
舊 artifact reader 不受影響。不再藉此繼續擴充。

## Regression

```
field provider dedicated = 210 / 210
launcher dedicated       = 134 / 134
scheduled core dedicated = 133 / 133
FIRST LIVE integration   = 327 / 327（＋ window 25 / 25）
6.9 existing             = 222 / 222
Phase 6.10 dedicated     = 1173 / 1173
Full Phase 6             = 6035 / 6035 PASS，56 files，0 FAIL
```

既有斷言僅 `test_phase6_first_live_handoff.py` 的 R4 一處變更
（登記新 provider，並**加上 main() 對 live leg 的 AST 可達性證明**），
方向為**加嚴**，已附 OLD / NEW / WHY / EVIDENCE。


---

# POSTMORTEM ROOT CAUSE CORRECTION —— FIRST LIVE Attempt #2（2026-09-22，OFFLINE ONLY）

```
TERMINAL RESULT (UNCHANGED)  :  ABORTED_CURRENT_CONDITIONS
ROOT CAUSE (CORRECTED)       :  PROVIDER_WIRING_DEFECT
CLASSIFICATION               :  SAFE FAIL-CLOSED ABORT CAUSED BY PROVIDER WIRING DEFECT
SOC POSTMORTEM               :  SOC_ELIGIBLE_THROUGHOUT_GATE_WINDOW
ATTEMPT #3                   :  NOT AUTHORIZED
```

🔴 **落地的 Attempt #2 證據一律不得修改。** 本節是**追加的事後更正**，
`*.jsonl` / `*.launcher.log` / `scheduled_first_live_*.json` / `*.task.xml`
維持原樣（回歸測試 A~M 的第 K 項以 sha256 鎖住）。

## 先前敘述 vs 更正後敘述

| | 先前（Attempt #2 當下） | **更正後（2026-09-22 事後）** |
|---|---|---|
| 終端結果 | `ABORTED_CURRENT_CONDITIONS` | `ABORTED_CURRENT_CONDITIONS`（**不變**） |
| 成因敘述 | 「現場條件不符，policy 正常否決」 | **`PROVIDER_WIRING_DEFECT`** —— GATE A 的兩個輸入由 provider 錯誤佈線而恆為否 |
| 安全性 | SAFE | SAFE（**不變**；Fail Closed 正確動作，未送出任何指令） |
| SOC | 未判定 | **`SOC_ELIGIBLE_THROUGHOUT_GATE_WINDOW`** |
| 是否為 policy 正確否決 | 是 | **否** —— policy 從未取得真實輸入 |

## 失敗的兩項 GATE A 檢查與其成因（一對一對應）

ledger 記錄的失敗項為 `["natural_decision_is_charge", "soc_allows_charge"]`。

| 失敗項 | 成因 | 狀態 |
|---|---|---|
| `soc_allows_charge` | **BUG #1** canonical SOC band 斷線 | **CONFIRMED / FIXED** |
| `natural_decision_is_charge` | **BUG #2** classifier warm-up omission | **CONFIRMED / FIXED** |

### BUG #1 —— canonical SOC band 斷線（CONFIRMED）

```
OLD SOURCE : getattr(getattr(getattr(bundle, "arbiter", None),
                              "observer", None), "soc_band", None)
NEW SOURCE : bundle.factory(None, None).arbiter.observer.policy.soc_band
             （= TouArbitragePolicy.soc_band -> SocBandLatch.band）
```

**WHY**
`LiveChainBundle.__init__(self, factory, observe, reader, store)` —— **沒有
`arbiter` 屬性**；且 `soc_band` 掛在 `policy` 上，不在 `observer` 上。
兩層都錯，`getattr(..., None)` 把結構性缺陷**靜默降級**成「值未知」，
於是 band 恆為 `None`。`DECISION_MATRIX` 中 band = `None` / `S_NONE`
**永無 charge**，因此 `soc_allows_charge` 在**任何**真實 SOC 下都是 `False`。

**EVIDENCE（離線可重現）**

```
真實 LiveChainBundle：hasattr(bundle, "arbiter") == False
OLD accessor  -> None        （恆為 None）
NEW accessor  -> "S_NONE"/"S_LOW"/…（policy.soc_band 本人）
factory(None, None).arbiter 於多次呼叫間為同一實例 -> latch 狀態不流失
```

**名稱差異申報**：canonical 屬性名為 `soc_band`（未改名），
但**持有者**由（誤認的）`observer` 更正為 `policy`，
並補上被遺漏的 `factory(None, None)` 一層。

🔴 provider 維持 **WIRING ONLY**：未新增第二套 SOC policy、
未硬編 `SOC < 90`、未出現 `90.0` / `20.0` / `85.0` / `25.0` 任何門檻字面值
（由 N 項測試以 AST 鎖住）。

### BUG #2 —— classifier warm-up omission（CONFIRMED）

原 `_decision_sample()` 只做**一次** `bundle.observe()`。

**既有 contract 白紙黑字**（非本次發明）：

* `phase6_first_live_runner.prime_classifier` docstring ——
  「PowerClassifier 的 stable 狀態採 time-based debounce ——
  **單次 `update()` 結構上不可能脫離 UNKNOWN**。」
* `pcs_auto_control_service.build_live_chain_bundle` 內註解 ——
  「若每次呼叫都重建 arbiter，分類器永遠停在『第一次 update』，
  `grid_state` 恆為 UNKNOWN → 決策恆為 `no_action`。」

`DECISION_MATRIX` 中 **`grid = UNKNOWN` 完全沒有任何一列**，
因此 `natural_decision_is_charge` 必然為 `False`。

**修正 —— 沿用既有正式實作，未自訂次數／秒數**

| 項目 | 值 | 來源 |
|---|---|---|
| 暖機函式 | `phase6_first_live_runner.prime_classifier` | 既有 6.9 實作 |
| 取樣間隔 | `PRIME_INTERVAL_SEC` = `PC.DEFAULT_CONFIG.debounce_sec` = 3.0 s | 既有 canonical 常數 |
| 暖機預算 | `PRIME_INTERVAL_SEC * 4` = 12.0 s | 既有 6.9 relative-window 分支原式 |

🔴 **暖機 ≠ attempt retry**：每次 build 只暖機一次（`_prime_classifier_once`），
`gate_a()` 連呼兩次仍只暖機一次；有界（預算用盡即停，provider 內無 `while`）；
GATE A 失敗後**不重跑 attempt**，ONE-SHOT 語意不變。

## 結構缺陷不得再被靜默吞掉

新增 `ProviderWiringError`。canonical accessor 明確區分兩種 `None`：

| 情況 | 行為 |
|---|---|
| 路徑斷掉（無 `factory` / `arbiter` / `observer` / `policy` / `soc_band`） | **raise `ProviderWiringError`**（大聲失敗） |
| 路徑健在、latch 尚未定狀態 | 回傳 `None` -> 呼叫端 Fail Closed 拒絕 |

Attempt #2 之所以能靜默中止，正是因為舊寫法把前者降級成後者。

## SOC 事後判定（read-only，來源：`output/logs/auto_monitor_service.log`）

```
23:54 – 00:00:38   chg=False run=False dir=idle    SOC=2.0%  P=-1.3kW
00:00:49           run=True stby=True  dir=idle    SOC=2.0%
00:00:59           chg=True            dir=charge  SOC=3.0%  P=-1.4kW
00:01:20           chg=True            dir=charge  SOC=4.0%  P=+80.2kW
00:05:00           chg=True            dir=charge  SOC=6.0%  P=+80.1kW
```

GATE 窗（00:05–00:30）內 SOC = 2 % ~ 6 %，遠低於 `soc_charge_stop_pct = 90.0`，
判定 **`SOC_ELIGIBLE_THROUGHOUT_GATE_WINDOW`**。

## HMI 401 / re-login / token refresh（READ-ONLY 檢查）

| 路徑 | 401 偵測 | 重新登入 | token refresh |
|---|---|---|---|
| `phase6_first_live_runner.observe_window`（6.9 Phase A） | `_has_401()` | **有**，上限 **2 次** | 重新 `build_api_client(login=True)` |
| `PRD.build_ess_reader` -> `CDR.read_all(client)` | 無 | **無** | **無** |
| `PRD.build_api_client(login=True)` | — | 只登入**一次**，失敗回 `token=None` 不重試 | **無** |
| **provider / scheduled FIRST LIVE 路徑** | **無** | **無** | **無** |

判定：**`HMI_AUTH_RENEWAL_UNVERIFIED`** —— scheduled provider 路徑在
長時間等待窗內若 session 失效，`getRunMode` / `getScheduleSwitch` 會回 401，
`pcs_mode_state` 變 `None` -> ESS 觀測 `PCS_MODE_UNAVAILABLE` -> Fail Closed。
行為安全，但會讓整個窗口空轉。

🔴 **時序澄清**：monitor log 的 401 出現在 **~00:07**，
Attempt #2 的 artifact 時間為 **00:05:08** —— 401 發生在中止**之後**，
**不是** Attempt #2 的成因。此項列為**待處理的獨立議題**，本輪未修改。

## 回歸

```
field provider dedicated = 294 / 294   （210 -> 294，+84；無任何既有斷言被放寬）
launcher dedicated       = 134 / 134
scheduled core dedicated = 133 / 133
FIRST LIVE integration   = 327 / 327
Trial #2 harness         = 257 / 257
6.9 CLI entry            = 128 / 128
6.10-D recovery contract =  47 / 47
Full Phase 6             = 6119 / 6119 PASS，56 files，0 FAIL
```

新增測試群組：**R1~R6**（重現 / before-after）、**N**（負向 SOC ＋ 無硬編門檻）、
**W**（結構缺陷必須大聲失敗）、**C1~C8**（classifier contract）、
**A~M**（修正後整體約束，含 K 項以 sha256 鎖住 Attempt #2 證據不被更動）。

## 本輪未做（依授權）

* **未**建立 Attempt #3 Task、**未**執行任何 field control、**未**送出任何指令。
* **未**修改 Attempt #2 任何證據檔。
* **未**修改 decision semantics（`DECISION_MATRIX` 仍 27 列、`debounce_sec` 仍 3.0）。
* **未**處理 HMI 401 renewal（獨立議題，待授權）。


---

# ATTEMPT #3 PRE-READINESS CLOSURE（2026-09-22，OFFLINE FIX ＋ READ-ONLY VERIFY）

```
HMI AUTH RENEWAL      = VERIFIED
REAL SOC WIRING       = PASS
REAL CLASSIFIER PRIME = PASS
```

## PART A — HMI READ 認證續期（單一 auth policy）

### 既有 6.9 契約（`phase6_first_live_runner.observe_window`）

```python
偵測 : reading["_fail"] 內含 "_error401"
上限 : len(relogins) < 2          ->  最多 2 次
動作 : 重新登入後重讀同一筆；只取唯讀權限
```

### 抽出與重用

新增 `test/phase6_hmi_auth_renewal.py` 作為**唯一** auth policy 來源：
`AUTH_RENEWAL_MAX_ATTEMPTS = 2`（沿用，未加大）、`is_auth_expired()`、
`build_inplace_relogin()`、`AuthRenewingReader`。
6.9 的 `_has_401()` 改為**委派**同一 predicate（OLD/NEW/WHY/EVIDENCE 已註於原始碼），
bound 與 `observe_window` 流程**未更動**。

### 機制差異（明文記錄）

6.9 以 `build_api_client(login=True)` 建立**新** client，但 `obs_source` 仍持舊 client
—— 存在「new client + old reader」的混合狀態。
provider 改採**原地** re-login：`client.login_hmi()` 更新的是**同一個** client 物件，
因此 provider adapter / live chain / live-leg verifier 立即共用新憑證，
且**不重建 chain**，不會重置 `PowerClassifier` debounce 與 `SocBandLatch`。

### Provider HMI read call graph（A5）

| 讀取點 | 路徑 | 續期覆蓋 |
|---|---|---|
| Fresh Safety Preflight | `safety_preflight` → `sample()` → `AuthRenewingReader` | ✅ 直接包裝 |
| GATE A observation | `gate_a` → `_decision_sample()` → `sample()` → 再 `bundle.observe()` | ✅ 先續期再讀 chain |
| Fresh GATE B | `arm_and_run_handoff` → `_decision_sample(post=True)` | ✅ 同上 |
| live-leg readback / verifier | `run_live_leg_cli` → `bundle.reader` | ✅ 共用同一 client（原地續期即生效） |
| post-check / collect_artifact | `collect_artifact` → `sample()` | ✅ |
| Handoff / PauseVerifier 的 probe·loopcheck | SSH guard | 不適用（非 HMI 認證） |
| restore verification | probe + loopcheck | 不適用 |

🔴 順序修正：`_decision_sample()` 原本先 `bundle.observe()` 才 `sample()`，
會讓 chain 讀落在續期**之前**；已改為 **先 `sample()` 再 `observe()`**。

### Command resend safety proof（A6）

- AST：`phase6_hmi_auth_renewal` 模組層**未** import
  `phase6_remote_adapter` / `phase6_handoff_orchestrator` /
  `phase6_first_live_handoff` / `pcs_auto_control_service` / `subprocess`。
- AST：模組內**無** `send_pause` / `send_restore` / `request_pause` /
  `request_restore` / `run_live_leg_cli` / `acquire_ownership` /
  `HandoffRunner` / `FirstLiveDispatcher` 等識別字或字串。
- `AuthRenewingReader` 內的呼叫集合只有
  `{_reader, _relogin, is_auth_expired, _log, append, bool, int, type, ValueError}`。
- 行為：續期達 bound 期間，`CommandCountGuard` 的
  `pause` / `phase6_charge` / `restore` 計數**完全未變**。

### Credential safety（A7）

evidence 只有 `auth_renewal_attempted` / `auth_renewal_count` /
`auth_renewal_result` / `read_recovered`（＋ `auth_renewal_max`）。
不記錄 password / token / session secret。

### Dedicated tests — **105 / 105 PASS**（AUTH1 ~ AUTH16 ＋ provider read path）

## PART B — REAL HMI SOC BAND WIRING = **PASS**

```
timestamp             2026-09-22 09:18
raw SOC (HMI)         90.0 %
canonical path        bundle.factory(None, None).arbiter.observer.policy.soc_band
canonical soc_band    S_HIGH          （legal band；非 None、非 ProviderWiringError）
soc_charge_stop_pct   90.0            -> SOC 90.0 >= 90.0 => charge 閂閉 => S_HIGH ✅ 一致
soc_allows_charge     False           （查既有 DECISION_MATRIX）
grid / tou / decision IMPORT / PEAK / discharge
auth renewal          attempted=False, count=0, result=AUTH_OK
```

⚠️ **驗證方法更正**：第一次驗證腳本**未先暖機**就讀 band，取到 latch 初始值
`S_NONE`（policy 因 grid=UNKNOWN 而從未被呼叫）。
改以 provider 自己的 `gate_a()`（內含 canonical prime）端到端驗證後得 `S_HIGH`。
→ 這是**驗證腳本的順序瑕疵**，不是 provider 缺陷；provider 的
`_decision_sample()` 本來就先 `_prime_classifier_once()`。

🔴 本次**不要求** SOC 為 `S_LOW`、**不要求** decision 為 CHARGE；
白天 SOC 90 %／PEAK 得到 `CHARGE_POLICY_NOT_ELIGIBLE` 是**正確**的 policy 行為。

## PART C — REAL METER CLASSIFIER PRIME = **PASS**

```
PRIME_INTERVAL_SEC = 3.0  (= PowerClassifier DEFAULT_CONFIG.debounce_sec)
budget             = PRIME_INTERVAL_SEC x 4 = 12.0 s   （既有 6.9 contract，未放寬）
samples            3
  #1 09:17:15  grid=UNKNOWN     authority=None
  #2 09:17:20  grid=NEAR_ZERO   authority=EXTERNAL_OR_UNKNOWN
  #3 09:17:24  grid=NEAR_ZERO   authority=EXTERNAL_OR_UNKNOWN
elapsed            12.0 s
final              grid=NEAR_ZERO  decision=idle
```
**UNKNOWN → STABLE 在 bounded budget 內成立**（第 2 筆即脫離 UNKNOWN）。
未延長 budget、未改 interval、未改 threshold。

## Regression

```
auth renewal dedicated = 105 / 105
provider dedicated     = 294 / 294
scheduled launcher+core= 267 / 267
FIRST LIVE integration = 646 / 646（3 檔）
6.9                    = 222 / 222
Phase 6.10             = 1173 / 1173
Full Phase 6           = 6224 / 6224 PASS，57 files，0 FAIL
```
未放寬任何 assertion、未刪 test、未改 expected result。

## 現場狀態（本輪唯讀觀測）

```
控制指令 = 0    SSH 動詞 = 0
AC Sleep = 300 s（未改回 Never）
三個 Phase6 Task 全部 Disabled；未建立 Attempt #3 Task／config
```


---

# FIRST LIVE ATTEMPT #3 — FIELD TASK CREATION（2026-09-22）

```
FIELD TASK CREATION = PASS
FIRST LIVE ATTEMPT #3 = AUTHORIZED / WAITING FOR SCHEDULED TRIGGER
Unattended LIVE       = HOLD
```

🔴 **本輪只建立 Task，未手動 `/Run`，未執行任何 field control。**

## STEP 0 — VALIDATE-ONLY REHEARSAL（重跑，因 provider／auth 有 code change）

```
task            Phase6_FirstLive_ValidateOnly_B（執行後已刪除）
LastTaskResult  0
scheduled_result        VALIDATE_ONLY_NOT_EXECUTED
exit_classification     HANDLED_TERMINAL_RESULT
field_side_effects      0
provider_validation     provider_id_match / core_modules_importable /
                        guard_variant_b2 / credentials_present /
                        safe_defaults_intact / interface_complete /
                        no_field_io_on_import  ── 全部 true
ledger 產生             無（`VALIDATE-ONLY-REHEARSAL-20260922B.jsonl` 不存在）
artifact 產生           無
0 HMI control ／0 Meter control ／0 SSH verb ／0 pause ／0 restore
0 authority acquire ／0 PCS command
```
rehearsal config 的 `target_date` 刻意設為過去日（2026-09-20）：即使誤以非
`--validate-only` 呼叫，也只會得到 `WRONG_TARGET_DATE` 且 0 command。

## WINDOWS HOST POWER

🔴 **事實更正**：先前報告寫「AC Sleep = 300 s」是**誤判** ——
300 s 是 `SUB_VIDEO/VIDEOIDLE`（**顯示器關閉**），
`SUB_SLEEP/STANDBYIDLE`（真正的睡眠）**本來就已是 0 = Never**。

```
BEFORE  AC STANDBYIDLE = 0x00000000 (Never)
指令    powercfg /setacvalueindex <SCHEME_BALANCED> SUB_SLEEP STANDBYIDLE 0   （冪等）
AFTER   AC STANDBYIDLE = 0x00000000 (Never)   ✅ read-back verified

未觸碰  DC STANDBYIDLE = 0x00000000（原狀）
        HIBERNATEIDLE AC = 0x00000000 / DC = 0x7fffffff（原狀）
        VIDEOIDLE     AC = 0x0000012c / DC = 0x000000b4（原狀，顯示器關閉≠睡眠）
```
🔴 因觀測到的 baseline 本來就是 Never，
**Attempt #3 結束後「恢復 AC Sleep = 300 s」這項需要重新確認** ——
照觀測值恢復應為 **維持 Never**（見結案時裁示）。

## FRESH EXTERNAL CONTROLLER BASELINE（建立 Task 前，唯讀）

```
2026-09-22 09:34
probe outcome        SSH_OK
auto_control.py      RUNNING
pid                  57483
cwd / cmd            /home/etica/ems  /  python3 auto_control.py     ✅ 與期望一致
screen_count         1        -> screen_alive = True
guard_variant        B2
capability_ok        True
loop marks (x3)      09:34:18 -> 09:34:27 -> 09:34:36   （嚴格遞增）
loop_marks_advancing True

VERDICT = BASELINE_READY
SSH verbs used = {probe: 1, loopcheck: 3}（皆唯讀，transport armed = False）
pause 0 ／restore 0 ／authority acquire 0 ／PCS command 0
```

## FORMAL CONFIG

`test/scheduled_first_live_attempt3_20260923.json`
```
enabled              true
attempt_id           FIRST-LIVE-ATTEMPT3-20260923
target_date          2026-09-23
window_start/end     00:05 / 00:30
timezone             Asia/Taipei
dependency_provider  phase6_first_live_field  ->  phase6_first_live_field_provider
ledger_path          output/phase6_scheduled_first_live/FIRST-LIVE-ATTEMPT3-20260923.jsonl
artifact_dir         output/phase6_scheduled_first_live/attempt3_20260923
```
離線驗證（未執行 runner）：`load_config` = `CONFIG_OK`，problems = []。
`window_state` 推演：
```
2026-09-22 現在      BEFORE_WINDOW
2026-09-23 00:04     BEFORE_WINDOW
2026-09-23 00:05     INSIDE_WINDOW
2026-09-23 00:29     INSIDE_WINDOW
2026-09-23 00:31     WINDOW_EXPIRED
```
🔴 **未沿用** Attempt #2 ledger／artifact，**未沿用** rehearsal ID。
建立時 ledger 與 artifact_dir **皆尚未存在**（已驗證）。

## TASK

`Phase6_Scheduled_FirstLive_Attempt3_20260923`（由顯式 XML 註冊）
```
Trigger              2026-09-23T00:05:00      Next Run = 2026/9/23 00:05
Schedule Type        One Time Only
Repeat Every/Until   Disabled / Disabled
RestartCount         0        （XML 未含 RestartOnFailure -> 無 auto retry）
StartWhenAvailable   False
MultipleInstances    IgnoreNew
ExecutionTimeLimit   PT1H
WakeToRun            False    RunOnlyIfIdle = False   StopOnIdleEnd = False
DisallowStartIfOnBatteries / StopIfGoingOnBatteries = False
Principal            Water ／ LogonType = InteractiveToken ／ RunLevel = Limited
Action               "D:\Crawler Sample\test\run_first_live_attempt3_20260923.cmd"
State                Ready / Enabled
```
XML evidence：`test/Phase6_Scheduled_FirstLive_Attempt3_20260923.xml`
wrapper：`test/run_first_live_attempt3_20260923.cmd`（絕對路徑，不依賴 PATH／cwd／stdin）

## LEGACY TASK STATE（fresh verify）

```
Phase6_FirstChargeLive_Precheck_OneTime        Disabled   Next Run = N/A
Phase6_FirstLive_Charge_OneTime                Disabled   Next Run = N/A
Phase6_Scheduled_FirstLive_Attempt2_20260922   Disabled   Next Run = N/A
Phase6_FirstLive_ValidateOnly_B                DELETED
```
全系統 Task 掃描：除 Attempt #3 外，**無**其他會執行本專案腳本的可執行 future Task。
（`Phase6_FirstLive_Charge_OneTime` 即使被啟用也會被
`DIRECT_FIELD_EXECUTION_BLOCKED` 以 exit 2 拒絕 —— 雙重保護。）

## FINAL SAFETY STATE

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0
本輪 pause 0 ／restore 0 ／authority acquire 0
本輪 armed transport 0（baseline transport armed = False）
SSH verbs 本輪合計 = probe 1 + loopcheck 3（唯讀）
Attempt #3 ledger    尚未建立
Attempt #3 artifact  尚未建立
Task Scheduler 服務  Running
登入使用者           Water
AC Sleep             Never
```


---

# SOC 90% → 2% 觀測差異 — 正式分類（2026-09-22）

```
SOC CYCLE EXPLANATION                        = CONFIRMED BY OPERATOR CONTEXT
SOC 90% -> 2% discrepancy                    = EXPLAINED BY NORMAL CHARGE / DISCHARGE CYCLE
SOC_SOURCE_MISMATCH                          = NOT INDICATED
ATTEMPT #3 BLOCKER FROM SOC SOURCE DIFFERENCE = CLEARED
```

## 觀測

| 時間 | 來源 | SOC |
|---|---|---|
| 2026-09-22 09:18 | Phase 6 唯讀觀測（正式 provider，真實 HMI） | **90.0 %** |
| 2026-09-22 14:05 | `auto_monitor_service.log`（NSSM stdout） | **2.0 %** |

## 正式成因（operator context）

**不是** SOC source mismatch、**不是** endpoint mismatch、**不是** selector mismatch、
**不是** provider mapping bug，而是**同一顆電池在不同時間點的正常充放電週期讀值**：

```
昨晚進入 OFF_PEAK
  auto_control.py 依既有 OFF_PEAK 邏輯自然開始充電
  SOC 約 2 % -> 持續充電 -> 90 % 以上
  達 external controller 既有充電停止條件 -> 停止充電
        => 今早 Phase 6 讀到 SOC ~ 90 %   正常

2026-09-22 09:00 後進入 PEAK
  auto_control.py 依既有 PEAK 邏輯開始放電
  SOC 90 %+ -> 持續下降 -> 約 2 %
  達低 SOC 條件 -> 停止放電
        => auto_monitor_service 最新讀到 SOC ~ 2 %   正常
```

## 不需再做的調查

- SOC endpoint mismatch investigation
- selector mismatch investigation
- provider SOC source mismatch investigation

🔴 **例外條件**：若日後出現 **fresh same-time evidence**（同一時刻兩個來源不同值），
才需重新開啟 source mismatch 調查。本次兩筆觀測相距約 4 小時 47 分，不構成 same-time 證據。

## 對 Attempt #3 的意義

SOC 已回到約 2 %，external controller 已因低 SOC 停止放電。
今晚再次進入真實 OFF_PEAK 時，**有合理機會**重新形成
`OFF_PEAK + 低 SOC + external natural CHARGE` —— 正是 Attempt #3 要驗證的 GATE A 情境。

🔴 **不得預先判定今晚一定 PASS。**
正式 Attempt #3 仍必須於 2026-09-23 00:05 重新取得全新證據：

```
fresh SOC              fresh canonical soc_band     fresh classifier state
fresh TOU              fresh decision               fresh fault / alarm
fresh communication    fresh ownership
```
只有正式 GATE A 全 PASS 才可進 Handoff。
（本項僅為分類更正，**未**修改 SOC policy、TOU、provider、Task 或 `auto_control.py`。）

## 安全狀態（離線複驗，0 field action）

```
HO.DISPATCH_ENABLED                      False
RT.DISPATCH_ENABLED (pcs_auto_control_runtime)  False
RA.RemoteSenders().armed                 False
RA.SshTransport().armed                  False
provider validate_provider_definition    ok=True（safe_defaults_intact=True）

Phase 6 real CHARGE / DISCHARGE / PCS STOP   0 / 0 / 0
External B2 pause / restore                  2 / 2（Trial #1 各 1、Trial #2 各 1）

FIRST LIVE      HOLD（除已授權的 Attempt #3）
Unattended LIVE HOLD
```

## 現況

```
ATTEMPT #3 = AUTHORIZED / WAITING FOR SCHEDULED TRIGGER
Task       = Phase6_Scheduled_FirstLive_Attempt3_20260923
Trigger    = 2026-09-23 00:05
Window     = 00:05 ~ 00:30  Asia/Taipei
```


---

# FIRST LIVE ATTEMPT #3 — POSTMORTEM CORRECTION（2026-09-23）

```
ATTEMPT #3                      = ABORTED_PRE_PAUSE_PREFLIGHT
ROOT CAUSE                      = PROVIDER_WIRING_DEFECT
SAFETY                          = SAFE FAIL-CLOSED
FIRST LIVE SUCCESSFUL 5 kW LEG  = NOT YET COMPLETED
ATTEMPT #4                      = NOT READY
```

🔴 **Attempt #3 原始 evidence 維持 immutable** —— ledger / artifact / launcher log
一律未被回頭修改。本章為**追加**的 postmortem correction。

## 1. 終態更正

| 項目 | 值 |
|---|---|
| 實際 field execution | **ABORTED_PRE_PAUSE_PREFLIGHT** |
| scheduled runner 原始分類 | `AUTHORITY_ACQUIRE_FAILED` |
| 該分類狀態 | **POSTMORTEM: MISCLASSIFIED TERMINAL RESULT**（RESULT CLASSIFICATION DEFECT） |
| Root Cause | **PROVIDER_WIRING_DEFECT** |
| Safety | **SAFE FAIL-CLOSED** |

🔴 文件**不得**再把 `AUTHORITY_ACQUIRE_FAILED` 當成 Attempt #3 的真實 field outcome。

實際計數：`pause = 0`、`restore = 0`、`Phase6 CHARGE = 0`、`Phase6 DISCHARGE = 0`、
`PCS STOP = 0`、ownership 全程 `EXTERNAL`。

## 2. 應保留的正面結果

```
Task / launcher            PASS   （00:05:00 觸發，LastTaskResult = 0）
Fresh Safety Preflight     PASS   （11 / 11）
GATE A                     PASS   ← 首次真實 field 通過
    verdict = CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED
    natural_decision_is_charge / tou_allows_charge / soc_allows_charge
    fault_clear / critical_alarm_clear / communication_healthy
    required_data_available / pcs_state_legal / non_ownership_safety_ok
Handoff readiness          PASS   （4 / 4）
HMI auth renewal           AUTH_OK（count 0）
------------------------------------------------------------------
6.10 PRE_PAUSE_PREFLIGHT   FAIL   ← 中止點
pause                      NOT ATTEMPTED
PauseVerifier              NOT EXECUTED
GATE B                     NOT EXECUTED
Authority acquire          NOT ATTEMPTED
5 kW live leg              NOT EXECUTED
restore                    NOT REQUIRED
```
Attempt #2 修正的 **SOC canonical wiring / classifier prime / HMI auth renewal**
在真實 OFF_PEAK field path 已證明有效。

## 3. BUG #3 — HANDOFF SAMPLE CONTRACT（CONFIRMED）

`HandoffOrchestrator(observe=sample)` / `HandoffRunner(..., sample, ...)`
收到的是**原始** `sample()`，但 `pre_pause_gates` 要求的是 handoff-enriched 詞彙。
實測 11 項中 6 項 FAIL：

| `pre_pause_gates` 欄位 | 原始 `sample()` | 結果 |
|---|---|---|
| `meter_fresh` / `comm_ok` / `fault_clear` / `alarm_clear` | 有 | PASS |
| `soc_fresh` | 無 | FAIL |
| `ess_fresh` | 名為 `fresh` | FAIL |
| `tou_off_peak` | 只有 `tou_state` | FAIL |
| `decision_is_charge` | 不在 `sample()` | FAIL |
| `external_behaviour_known` | 無 | FAIL |
| `soc_in_charge_band` | `soc_max_pct = ctx.get(...)` = `None` | FAIL |

## 4. CANONICAL MAPPING TABLE

新增 `build_handoff_observation()`（**純 mapping**，無 I/O、無門檻、無 policy）：

| 欄位 | canonical source |
|---|---|
| `soc_fresh` | `EssSnapshotAdapter.observe().valid/.stale`（同一筆 ESS 觀測） |
| `meter_fresh` | `GridMeterClient.get_snapshot().valid/.stale` |
| `ess_fresh` | `EssSnapshotAdapter.observe().valid/.stale` |
| `tou_off_peak` | `arbitration.tou_state`（production TariffProvider） |
| `soc_in_charge_band` | canonical `soc_charge_stop_pct` ＋ `ess.soc_percent` |
| `decision_is_charge` | `arbitration.fresh_decision_action` |
| `comm_ok` | `ControlObservation.valid` |
| `fault_clear` | `ControlObservation.pcs_fault` |
| `alarm_clear` | `ControlObservation.alarm_rows`（level 0 且 alarmStatus） |
| `external_runtime_healthy` | B2 guard probe（screen / process / identity） |
| `external_behaviour_known` | 🔴 **UNCONFIGURED —— 無 canonical 來源** |

SOC 新鮮度說明：設備未提供獨立 SOC 時戳（`SOURCE_COHERENCE = UNRESOLVED`），
故 canonical SOC 新鮮度**就是**該筆 ESS 觀測的判定；
`EssSnapshot` 有效性本身已涵蓋 soc 缺失／非有限／讀太慢／stale。
**未發明新的 freshness rule。**

## 5. 🔴 BLOCKER — `external_behaviour_known` 無 canonical 來源

全專案掃描結果：該欄位**只出現在**
`phase6_handoff_orchestrator.pre_pause_gates` 的消費端，與 5 個離線測試的
**合成樣本**中。**沒有任何 production 產生者，也沒有文件定義。**

依既有原則（不得發明 production 參數），provider 維持：

```python
EXTERNAL_BEHAVIOUR_KNOWN_CANONICAL_SOURCE = None
EXTERNAL_BEHAVIOUR_KNOWN_STATUS = "UNCONFIGURED"
```
後果：`pre_pause_gates` 對此項 **Fail Closed**。
即使其餘 10 項全部正確映射，pre-pause preflight 仍**不會**通過。

⚠️ 一併記錄已評估但**不採用**的候選（避免日後重覆討論）：
`phase6_trial2_harness.active_gate()` / `ActiveBaselineObserver` 是
「external 正在主動控制」的 Trial #2 構件，語意比「behaviour known」更強，
且與 orchestrator 明文的
「PRE-PAUSE 階段刻意不要求 Authority=IDLE / PCS idle / no external controller」
相衝突 —— 直接沿用等於**新增 policy**，故不採用。

## 6. soc_max_pct — OLD / NEW / WHY / CANONICAL SOURCE / EVIDENCE

```
OLD              soc_max_pct = ctx.get("soc_max_pct")   -> 永遠 None
NEW              canonical_soc_max_pct(bundle)
CANONICAL SOURCE bundle.factory(None, None).arbiter.observer.policy
                     .cfg.soc_charge_stop_pct
                 —— 與 canonical_soc_band() **同一個** policy 物件
WHY              None 使 soc_in_charge_band 結構上不可能通過；
                 不得硬編 90.0，也不得另建第二份 PolicyConfig
EVIDENCE         Attempt #3 artifact：soc_in_charge_band FAIL，
                 但同一時刻 GATE A 的 soc_allows_charge = True
```
路徑斷裂 -> `ProviderWiringError`（大聲失敗，**不**降級成 `None`）。

## 7. RESULT CLASSIFICATION — 修正

新增終態 `ABORTED_PRE_PAUSE_PREFLIGHT`、`ABORTED_HANDOFF_STEP`，
artifact 新增 `handoff_state` / `handoff_abort_step` / `handoff_abort_detail` /
`authority_acquire_attempted`，`Phase6AuthorityLease` 新增 `acquire_attempted`。

```
OLD  dispatcher NOT_ATTEMPTED 且 phase6_authority_acquired False
     -> AUTHORITY_ACQUIRE_FAILED
NEW  依 handoff_abort_step 決定 stage；
     只有 authority_acquire_attempted is True 且未取得
     才允許 AUTHORITY_ACQUIRE_FAILED
```
未登記的步驟 -> `ABORTED_HANDOFF_STEP`（誠實標示，不猜）。

## 8. DURABLE LEDGER ORDERING — 修正

```
OLD  進入 HandoffRunner 之前就寫 pause INTENT ＋ 責任成立
NEW  pre-pause preflight PASS -> 即將呼叫 pause sender
     -> durable INTENT ＋ 責任成立 -> 實際 send
```
實作：scheduled runner 傳入 `register_pause_intent_hook`，
provider 於 `_send("pause")` 的**前一刻**回呼。
未提供 hook 即沒有 pause 能力。

## 9. RESPONSIBILITY CONTRACT

pre-pause preflight 就失敗、sender 從未呼叫 -> `restore responsibility = False`，
**不留** `PENDING_RESTORE`。Attempt #3 的 artifact `final = False` 才符合 field truth；
ledger 的 `True` 屬 evidence/state ordering defect，已由上述修正根除
（原始 ledger 保持 immutable）。

## 10. DEDICATED TESTS — `test_phase6_attempt3_postmortem.py` **91 / 91 PASS**

```
H1~H13e  handoff adapter（含 H12 Attempt #3 條件重建：
         SOC 2.0 / 50.0 / 90.0 三點皆 PRE_PAUSE PASS，
         H12d 重現原始 6 項失敗，H12e 證明修正後轉 PASS）
S1~S5    soc_max_pct canonical wiring
C1~C9b   result classification（C1：Attempt #3 artifact -> 正確終態）
L1~L8a3  pause intent / responsibility ordering ＋ crash recovery
K1~K5    ledger / artifact 終態一致性
E1~E8    encoding
N1~N4b   no duplicated policy（AST）
```

## 11. MOJIBAKE — 🔴 誤報更正

```
分類 = NOT A DEFECT（我方終端顯示問題，非證據缺陷）
```
以 UTF-8 重新讀取 Attempt #3 artifact 與 ledger：

```
recovery.action       = 尚未開始 -> 可在 window 內執行一次
handoff_result.detail = 未通過：['soc_fresh', 'ess_fresh', 'tou_off_peak',
                        'soc_in_charge_band', 'decision_is_charge',
                        'external_behaviour_known']
ledger seq2           = 即將 temporary arm 並進入既有 handoff
ledger seq3           = pause 意圖已落盤 -> 責任成立
```
**檔案本身是正確的 UTF-8，內容完整無損。**
先前回報的 mojibake來自我方 Git Bash（cp950）stdout 轉碼，
與 `run_scheduled_first_live._write_artifact`
（`open(..., encoding="utf-8")` ＋ `ensure_ascii=False`）無關。
E1~E8 測試仍保留，用以**鎖住**此正確行為，並禁止用 `errors="ignore"` 掩蓋。

## 12. Regression

```
新增 test_phase6_attempt3_postmortem.py     91 / 91
provider dedicated                         294 / 294
scheduled core                             137 / 137（原 133，+4 為新增收緊）
launcher                                   134 / 134
FIRST LIVE integration ／ Trial #2 ／ 6.9 ／ 6.10   全數 PASS
Full Phase 6   6319 / 6319 PASS，58 files，0 FAIL   （baseline 6224 / 57）
```
未刪 test、未放寬 assertion、未改 expected result 掩蓋 defect。
3 處既有斷言變更皆附 OLD / NEW / WHY / EVIDENCE，且**全為收緊**：
`test_phase6_first_live_field_provider.py` P9（相依集合涵蓋範圍）、
`test_phase6_scheduled_first_live.py` S10（多一個必要條件）、
同檔 S20（依例外位置分別斷言，並新增 S20b）。

## 13. 安全狀態

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0
本輪 authority acquire 0 ／armed transport 0 ／SSH 動詞 0 ／HMI 連線 0
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 2 / 2
FIRST LIVE HOLD          Unattended LIVE HOLD
四個 Phase6 Task 全部 Disabled（Attempt #3 已於 evidence 保存後停用）
ATTEMPT #4 = NOT AUTHORIZED（未建立 config、未建立 Task）
```


---

# `external_behaviour_known` — 正式定義（2026-09-23 裁示）

```
EXTERNAL_BEHAVIOUR_KNOWN          = CONFIGURED / VERIFIED
ATTEMPT #3 RECONSTRUCTED PRE_PAUSE = PASS（11 / 11）
```

## 1. FORMAL SEMANTICS

> 目前對 external controller 的 loop / runtime behaviour，已有足夠的既有
> read-only evidence，可以確定分類為 **RUNNING** 或 **NOT_RUNNING**，
> 而不是 **UNKNOWN**。

🔴 **KNOWN ≠ ACTIVE。** 本欄位**不得**被定義成下列任何一項（語意都更強）：

```
external currently charging      external currently discharging
external currently controlling PCS   PCS non-zero power
Authority = EXTERNAL             Trial #2 Active Gate PASS
```

## 2. CANONICAL SOURCE（既有 symbol，未新增第二套 classifier）

```python
phase6_remote_adapter.external_liveness(
    probe, loop_active=None, loop_marks=None, min_loop_observations=None)
        -> (verdict, detail)

EXT_RUNNING     = "EXTERNAL_RUNNING"        # 已證明仍在主動 loop
EXT_NOT_RUNNING = "EXTERNAL_NOT_RUNNING"    # 已證明沒有在 loop
EXT_UNKNOWN     = "EXTERNAL_UNKNOWN"        # 無法判定 -> Fail Closed
EXTERNAL_LIVENESS_VERDICTS = (EXT_RUNNING, EXT_NOT_RUNNING, EXT_UNKNOWN)
```
Optional[bool] 包裝（recovery 既有用法）：
`phase6_handoff_orchestrator._resolve_external_running(dict) -> True/False/None`。

既有裁決順序（**未更動**）：

```
1. probe 必要事實不完整                 -> UNKNOWN
2. 事實齊備但 runtime 不健康            -> NOT_RUNNING（實證的否定）
3. runtime 健康 + loop evidence 前進    -> RUNNING
4. runtime 健康 + loop evidence 未前進  -> NOT_RUNNING
5. runtime 健康但 loop evidence 不足    -> UNKNOWN
```
觀測數沿用既有 `HandoffConfig.restore_loop_min_observations = 3`
（與 `handoff_readiness()` / RestoreVerifier 同一份契約），**未新增常數**。

## 3. FORMAL MAPPING

```python
def external_behaviour_known_from(verdict):
    return verdict in (EXT_RUNNING, EXT_NOT_RUNNING)
```

| canonical verdict | `external_behaviour_known` |
|---|---|
| `EXT_RUNNING` | **True** |
| `EXT_NOT_RUNNING` | **True** |
| `EXT_UNKNOWN` | **False** |
| 取不到 / 非法值（None、其他字串） | **False**（Fail Closed） |

本質即 `resolved_external_running is not UNKNOWN`；**不另外推導**。

## 4. 與 `external_runtime_healthy` 嚴格分離

兩者是 `PRE_PAUSE_ITEMS` 中**獨立的兩欄**，不得合併。

```
runtime 不健康（process / identity 失敗）
  -> canonical = NOT_RUNNING  -> external_behaviour_known = True
  -> external_runtime_healthy = False
  -> 整體 pre-pause 仍然 FAIL
```
🔴 **不得**因 `behaviour_known = True` 就繞過 `external_runtime_healthy`。
由 EB6c 直接鎖住。

## 5. 不採用的來源（明文禁止）

- `phase6_trial2_harness.active_gate()` / `ActiveBaselineObserver`
  —— 語意是「external **正在主動控制**」（看 PCS CHARGING/DISCHARGING、
  Authority、loop advancing），比 behaviour-known 更強，且與 6.10 pre-pause
  明文的「不要求 external 處於特定 active PCS state」相衝突。
- PCS state、`actual_active_power_kw`、`meter_kw`、Authority、SOC、
  CHARGING / DISCHARGING flags —— 這些最多是其他 evidence，
  **不是**本欄位的 canonical source。

AST 鎖住：provider 未引用 `active_gate` / `ActiveBaselineObserver`、
未 import `phase6_trial2_harness`；mapping 函式內無任何數值常數、
只引用 `EXT_RUNNING` / `EXT_NOT_RUNNING` 兩個 canonical enum。

## 6. PROVIDER WIRING

```python
def _external_liveness():
    prb = probe()
    marks = [loop_mark() for _ in range(CFG.restore_loop_min_observations)]
    verdict, why = RA.external_liveness(
        prb, loop_marks=marks,
        min_loop_observations=CFG.restore_loop_min_observations)
    state["external_liveness"] = {...}      # 進 artifact 作為證據
    return verdict

def _handoff_sample():
    return build_handoff_observation(
        _decision_sample(), external_liveness_verdict=_external_liveness())
```
provider **只 mapping**：不自行判 mark age / mark count / advancing threshold /
process semantics；canonical resolver 取不到 -> `UNKNOWN` -> Fail Closed。
artifact 新增欄位 `external_liveness`（verdict / detail / min_loop_observations /
loop_marks），`external_liveness_verdict` 一併留在 handoff observation 中。

## 7. EB 測試（EB1 ~ EB10 ＋ EBN）

```
EB0/EB0b  canonical symbol 與觀測數皆沿用既有（未新增第二套 / 未新增常數）
EB1/EB1b  loop marks 前進      -> RUNNING      -> known True
EB2/EB2b  runtime 健康未前進   -> NOT_RUNNING  -> known True
EB3       EXT_UNKNOWN                          -> known False
EB4~EB4c  loop evidence 不足 / 完全沒有 -> UNKNOWN -> known False
EB5~EB5c  probe 事實不完整 / probe 取不到 -> UNKNOWN -> known False
EB6~EB6d  runtime 不健康 -> known True 但 runtime_healthy False
          -> 整體 pre-pause FAIL；且兩欄位確實獨立存在
EB7/EB7b  Trial #2 Active Gate 不參與 mapping（AST）
EB8       PCS CHARGING 但 canonical UNKNOWN -> known False（不以 PCS 代理）
EB9       PCS STOPPED  但 canonical RUNNING -> known True
EB10~c    Attempt #3 現場重建 -> RUNNING -> known True
          -> ★★ PRE_PAUSE_PREFLIGHT 11 / 11 全 True
EBN1~7    AST：mapping 無數值常數、只用 canonical enum、
          未硬編 PCS/Authority/power、觀測數取自 HandoffConfig
```

## 8. ATTEMPT #3 RECONSTRUCTED PRE_PAUSE = **PASS**

Attempt #3 當時：external process healthy、identity OK、loop marks advancing、
OFF_PEAK、decision = charge、SOC eligible、meter/HMI fresh、no fault/alarm
-> canonical behaviour = **RUNNING** -> `external_behaviour_known = True`。

```
soc_fresh True    meter_fresh True    ess_fresh True
tou_off_peak True    soc_in_charge_band True    decision_is_charge True
comm_ok True    fault_clear True    alarm_clear True
external_runtime_healthy True    external_behaviour_known True
------------------------------------------------------------------
PRE_PAUSE_PREFLIGHT = 11 / 11 PASS
```

## 9. Regression

```
test_phase6_attempt3_postmortem.py   133 / 133（原 91，+42 為 EB/EBN 新增）
Full Phase 6   6361 / 6361 PASS，58 files，0 FAIL   （baseline 6319 / 58）
launcher --validate-only             CONFIG_OK / 7 項 provider validation /
                                     VALIDATE_ONLY_NOT_EXECUTED /
                                     field_side_effects = 0
```
未刪 test、未放寬 assertion、未改 expected result。

## 10. Attempt #4

```
ATTEMPT #4 = READY FOR NEW FIELD AUTHORIZATION
ATTEMPT #4 = NOT AUTHORIZED   （未建立 config、未建立 Task）
FIRST LIVE HOLD    Unattended LIVE HOLD
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 2 / 2
四個 Phase6 Task 全部 Disabled
```


---

# FIRST LIVE ATTEMPT #4 — FIELD TASK CREATION（2026-09-23）

```
FIELD TASK CREATION   = PASS
FIRST LIVE ATTEMPT #4 = AUTHORIZED / WAITING FOR SCHEDULED TRIGGER
Unattended LIVE       = HOLD
```

🔴 **本輪只建立 Task，未手動 `/Run`，未執行任何 field control。**

## STEP 0 — VALIDATE-ONLY REHEARSAL

```
task                    Phase6_FirstLive_ValidateOnly_C（執行後已刪除）
LastTaskResult          0
config_validation       CONFIG_OK
provider_validation     7 / 7 全 true
scheduled_result        VALIDATE_ONLY_NOT_EXECUTED
exit_classification     HANDLED_TERMINAL_RESULT
field_side_effects      0
ledger                  NOT CREATED      artifact  NOT CREATED
0 HMI control ／0 Meter control ／0 SSH control verb
0 pause ／0 restore ／0 authority acquire ／0 CHARGE ／0 DISCHARGE ／0 PCS STOP
```
rehearsal config 的 `target_date` 設為過去日（2026-09-21）：即使誤以非
`--validate-only` 呼叫，也只會得到 `WRONG_TARGET_DATE` 且 0 command。

## WINDOWS HOST（fresh verify，唯讀）

```
AC Sleep  (SUB_SLEEP/STANDBYIDLE)   0x00000000 = Never   ✅（未執行任何 setacvalueindex）
AC Hibernate                        0x00000000
VIDEOIDLE                           0x0000012c = 300 s（顯示器關閉，**不是** AC Sleep；未觸碰）
登入工作階段                        water / console / Active（自 2026-09-16 08:56）
Task Scheduler 服務                 Running
本機時間 / 時區                     2026-09-23 09:08  Taipei Standard Time
```

## LEGACY TASK CHECK（fresh verify）

```
Phase6_FirstChargeLive_Precheck_OneTime        Disabled   Next Run = N/A
Phase6_FirstLive_Charge_OneTime                Disabled   Next Run = N/A
Phase6_Scheduled_FirstLive_Attempt2_20260922   Disabled   Next Run = N/A
Phase6_Scheduled_FirstLive_Attempt3_20260923   Disabled   Next Run = N/A
Phase6_FirstLive_ValidateOnly_C                DELETED
```
除 Attempt #4 外，無其他 Phase6 / FirstLive / ValidateOnly 可執行 future Task。

## EXTERNAL BASELINE（fresh verify，唯讀）

```
2026-09-23 09:09~09:10
probe outcome        SSH_OK
auto_control.py      RUNNING          pid = 57483
cwd / cmd            /home/etica/ems  /  python3 auto_control.py     ✅
screen_count         1        guard_variant B2        capability_ok True
loop marks (x3)      09:09:56 -> 09:10:05 -> 09:10:13（嚴格遞增）

canonical external liveness
  symbol   phase6_remote_adapter.external_liveness()
  min_loop_observations = 3 （= HandoffConfig.restore_loop_min_observations）
  verdict  EXTERNAL_RUNNING
  detail   runtime 健康且 loop evidence 前進

VERDICT = BASELINE_READY
SSH verbs = {probe: 1, loopcheck: 3}（唯讀，transport armed = False）
pause 0 ／restore 0 ／authority acquire 0 ／PCS command 0
```

⚠️ 過程記錄：第一次 baseline 腳本把 **probe stdout 的原始 key-value** 傳給
`external_liveness()`，得到 `EXTERNAL_UNKNOWN`（「probe 必要事實不完整」）。
這是**驗證腳本的形狀錯誤**，不是 provider 缺陷 —— 已以 AST 確認 production
`provider.probe()` 回傳的正是 `ssh_ok / screen_alive / process_alive /
process_identity_ok / pid` 正規化鍵，且 `_external_liveness()` 傳入的就是它。
腳本改傳正規化 dict 後得 `EXTERNAL_RUNNING`。

## FORMAL CONFIG

`test/scheduled_first_live_attempt4_20260924.json`
```
enabled              true
attempt_id           FIRST-LIVE-ATTEMPT4-20260924
target_date          2026-09-24
window_start/end     00:05 / 00:30
timezone             Asia/Taipei
dependency_provider  phase6_first_live_field -> phase6_first_live_field_provider
ledger_path          output/phase6_scheduled_first_live/FIRST-LIVE-ATTEMPT4-20260924.jsonl
artifact_dir         output/phase6_scheduled_first_live/attempt4_20260924
```
離線驗證：`load_config = CONFIG_OK`，problems = []。`window_state` 推演：
`00:04 BEFORE_WINDOW` → `00:05 INSIDE_WINDOW` → `00:29 INSIDE_WINDOW` →
`00:31 WINDOW_EXPIRED`。
🔴 全新 ledger／artifact／wrapper／Task XML，**未沿用** Attempt #2／#3／rehearsal；
Attempt #2 與 #3 的原始 ledger 檔案未被觸碰。

## TASK

`Phase6_Scheduled_FirstLive_Attempt4_20260924`（顯式 XML 註冊）
```
Trigger              2026-09-24T00:05:00      NextRunTime = 2026/9/24 00:05
LastRunTime          11/30/1999（哨兵值）-> 從未執行，未手動 /Run
Schedule Type        One Time Only
Repeat Every/Until   Disabled / Disabled      Repetition Interval/Duration 皆空
RestartCount         0（XML 未含 RestartOnFailure -> 無 auto retry）
StartWhenAvailable   False
MultipleInstances    IgnoreNew    ExecutionTimeLimit PT1H
WakeToRun False   RunOnlyIfIdle False   StopOnIdleEnd False
DisallowStartIfOnBatteries / StopIfGoingOnBatteries = False
Principal            Water ／ InteractiveToken ／ Limited
Action               "D:\Crawler Sample\test\run_first_live_attempt4_20260924.cmd"
State                Ready / Enabled
```

## 本次執行時將套用的新修正（Attempt #3 之後）

```
handoff observation adapter   build_handoff_observation()（純 mapping）
soc_max_pct                   canonical_soc_max_pct(bundle)
external_behaviour_known      phase6_remote_adapter.external_liveness() 三態
result classification         handoff_abort_step -> stage；
                              只有 acquire() 真被呼叫且失敗才 AUTHORITY_ACQUIRE_FAILED
pause intent ordering         PRE_PAUSE_PREFLIGHT PASS -> hook -> durable INTENT
                              -> 責任成立 -> 實際 send
```
🔴 Attempt #4 是**第一次真正可能觸發 external pause** 的 Attempt。

## EVIDENCE PRESERVED

```
config    test/scheduled_first_live_attempt4_20260924.json
Task XML  test/Phase6_Scheduled_FirstLive_Attempt4_20260924.xml
wrapper   test/run_first_live_attempt4_20260924.cmd
baseline  output/phase6_field_validation/attempt4_baseline_20260923_091014.json
rehearsal output/phase6_scheduled_first_live/rehearsal_20260923c_task.log
```
執行後另將產生：launcher log／ledger／scheduled artifact／handoff artifact。

## FINAL SAFETY STATE

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0
本輪 authority acquire 0 ／armed transport 0
本輪 SSH 動詞 = probe 1 + loopcheck 3（唯讀）
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
Attempt #4 ledger / artifact   尚未建立
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 2 / 2
FIRST LIVE HOLD（除已授權的 Attempt #4）   Unattended LIVE HOLD
```


---

# FIRST LIVE ATTEMPT #4 — POSTMORTEM ＋ DISPATCH ENABLEMENT CONTRACT（2026-09-24）

```
ATTEMPT #4                     = ABORTED_AT_7_FIRST_LIVE
ROOT CAUSE                     = DISPATCH AUTHORIZATION GATE NOT SCOPED FOR FIRST LIVE
SAFETY                         = SAFE FAIL-CLOSED
FIRST LIVE SUCCESSFUL 5 kW LEG = NOT YET COMPLETED
ATTEMPT #5                     = READY FOR NEW FIELD AUTHORIZATION（≠ AUTHORIZED）
```

🔴 Attempt #4 原始 evidence（ledger / artifact / launcher log / handoff journal）
維持 **immutable**，本章為追加的 postmortem correction。

## 1. 正式結果

| 階段 | 結果 |
|---|---|
| Task / launcher | **PASS**（00:05:00 觸發，LastTaskResult = 0） |
| Fresh Safety Preflight | **PASS**（11/11） |
| GATE A | **PASS**（`CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED`） |
| Handoff readiness | **PASS**（4/4） |
| **PRE_PAUSE** | **PASS 11/11** |
| **pause** | **SENT** |
| **PauseVerifier** | **PASS**（`external_quiescent`，6 筆） |
| ownership | **NEITHER** |
| **GATE B** | **PASS** |
| **FIRST LIVE** | **NOT EXECUTED** |
| blocking cause | **DISPATCH SAFETY FLAG NOT TEMPORARILY ENABLED** |
| **restore** | **SENT** |
| **RestoreVerifier** | **PASS** |
| final external | **RESTORED / RUNNING**（2026-09-24 08:56 唯讀複驗 `EXTERNAL_RUNNING`） |
| Safety | **SAFE FAIL-CLOSED** |

🔴 runner 原始分類 `ABORTED_POST_HANDOFF_REVALIDATION` 標記為
**POSTMORTEM: MISCLASSIFIED TERMINAL RESULT**，**不得**當成正式 postmortem result。

計數：`Phase6 CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0`；
External B2 cumulative `pause / restore` 由 2/2 → **3 / 3**。

## 2. PART G — SAFETY INVARIANT 正式修正

```
OLD  HO.DISPATCH_ENABLED = False ／ RT.DISPATCH_ENABLED = False —— 永遠不可改

NEW  REST STATE                        : False / False
     AUTHORIZED FIRST LIVE SCOPED LEG  : 僅必要 flag temporarily enabled
     FINALLY / POST ATTEMPT            : False / False
     Unattended LIVE                   : 仍 HOLD（完全不共用本機制）
```
這是 **scoped authorization**，**不是** production dispatch enabled。

## 3. Dispatch gates call graph（source inspection）

```
HandoffRunner.run() step 7
    if not DISPATCH_ENABLED and not _permit_authorizes(self.dispatch_permit): bail
        └─ FirstLiveDispatcher.__call__()
             ├─ GATE B revalidator
             ├─ Phase6AuthorityLease.acquire()
             ├─ with first_live_dispatch_scope(permit):      ← 唯一 True 區間
             │     guard.take("phase6_charge")
             │     SVC.run_live_leg_cli()
             │        ├─ _live_leg_guard（process one-shot）
             │        ├─ confirm_live_leg（人工確認字串）
             │        ├─ live_leg_gates（decision / precheck / safety /
             │        │                  authority / interlock）
             │        ├─ PRD.LiveLegAuthorization（armed）
             │        ├─ PRD.build_executor(MODE_CONTROLLED_SINGLE_LEG)
             │        └─ ControlledLiveLeg.run_leg()
             └─ finally: lease.release()
```

## 4. 哪些 flag 真正需要 temporary enable

| FLAG | OLD REST STATE | TEMP STATE | ENABLE POINT | RESET POINT | WHY REQUIRED |
|---|---|---|---|---|---|
| `HO.DISPATCH_ENABLED` | False | **True** | `first_live_dispatch_scope.__enter__`（authority acquired 之後） | scope `finally`（無條件 False） | `HandoffRunner` step 7 的唯一 gate |
| `RT.DISPATCH_ENABLED` | False | **不變 False** | — | — | 🔴 source inspection 證實 6.9 live-leg 呼叫鏈（`run_live_leg_cli` → `live_leg_gates` → `LiveLegAuthorization` → `build_executor` → `ControlledLiveLeg` → `PcsControlExecutor` → `device_control_operator`）**完全不讀**它；它只用於 `Runtime.__init__` 預設與 `ObserveRecord` 報告欄位 |
| `RemoteSenders.armed` | False | **不變 False** | — | — | 只影響 external SSH sender；live leg 走 HMI |
| `SshTransport.armed` | False（唯讀 transport） | **不變** | — | — | pause / restore 另有獨立 armed transport，與 live leg 無關 |

🔴 **只打開真正需要的那一個**；不得為了測試方便全部一起打開（D6 / D6b / D6c 鎖住）。

## 5. Scoped enable 實作

```python
FIRST_LIVE_PERMIT_PRECONDITIONS = (16 項)
    attempt_field_authorized / attempt_id_matches / one_shot_not_consumed /
    inside_authorized_window / fresh_safety_preflight_ok / gate_a_ok /
    handoff_readiness_ok / pre_pause_preflight_ok / pause_sent /
    pause_verified / ownership_neither / gate_b_ok /
    authority_acquire_attempted / authority_acquired /
    charge_budget_available / about_to_run_live_leg
＋ permit_not_used（同一 permit 只能進入一次）

class FirstLiveDispatchPermit:  evaluate() / authorizes() / as_dict()
@contextlib.contextmanager first_live_dispatch_scope(permit, ...)
```
`__enter__` 前另檢查 **rest state 必須是 False**（不得疊加）。
任一條件不成立 -> `FirstLivePermitDenied`，**完全不改動任何旗標**。

🔴 **未提供** force flag / bypass / `--force-dispatch` / 全域 production switch。
permit 授權事實一律來自**正式 config**（launcher 建立 `runtime_context`），
provider 不得自行假定。

## 6. Reset / finally

```
live leg 結束（PASS / FAIL / UNKNOWN / timeout / 例外 / cleanup 失敗）
  -> scope finally：HO.DISPATCH_ENABLED = False（無條件，不還原「進入前的值」）
  -> Phase6AuthorityLease.release()（dispatcher 的 finally）
  -> external restore
  -> RestoreVerifier
```
dispatch reset 發生在 authority release 之前，authority release 又在
external restore 之前 —— 三者順序由 `with` / `finally` 的結構保證。

## 7. Crash safety

```
D10：新 process import -> HO.DISPATCH_ENABLED=False, RT.DISPATCH_ENABLED=False
```
permit **不持久化**；ledger 只記 `PERMIT_ENTERED` / `PERMIT_EXITED` 事件，
**不得**用 ledger recovery 自動重新 enable dispatch。
live leg 期間 send UNKNOWN / 例外 / crash 一律不得重取 permit、不得重送 CHARGE、
不得重跑 Attempt（D9 / D9b / D11 / D12 鎖住）。

## 8. DEFECT #1 — launcher pause intent hook（FIXED）

```
OLD  run_scheduled_first_live.py 的顯式 kwarg 清單漏傳
     register_pause_intent_hook -> hook 從未註冊
     -> scheduled ledger 只有 OUTCOME、沒有 INTENT
NEW  register_pause_intent_hook=deps.get("register_pause_intent_hook")
```
🔴 Attempt #4 當下 intent-before-action 在 **6.10 journal 層仍然成立**
（00:05:47 INTENT -> 00:05:48 OUTCOME），失守的是 **one-shot ledger 層**；
而 `SC.recover()` 讀的正是後者。
新增 **real-launcher 測試**（RL1 ~ RL5），走 `build_dependencies()` 實際建構路徑：
`INTENT seq < OUTCOME seq`；pre-pause FAIL -> 無 INTENT；
**crash before outcome -> `SC.recover()` 不得回 `R_CLEAN`**；
RL4 另以「不傳 hook」重現舊缺陷，證明修正的必要性。

## 9. DEFECT #2 — 終態分類 precedence（FIXED）

```
RESULT_PRECEDENCE = (real_terminal_step, derived_booleans, generic_fallback)

7_FIRST_LIVE            -> ABORTED_FIRST_LIVE          （新增終態）
6_POST_PAUSE_PRECHECK   -> ABORTED_POST_HANDOFF_REVALIDATION
1_PRE_PAUSE_PREFLIGHT   -> ABORTED_PRE_PAUSE_PREFLIGHT
未登記步驟               -> ABORTED_HANDOFF_STEP（誠實標示）
```
🔴 OLD 只在 `pause_send_outcome is None` 時才看 `handoff_abort_step`，
於是 pause 已 SENT 的 7_FIRST_LIVE 中止被誤判成 GATE B 失敗。
NEW：只要有 `handoff_abort_step` 就以它為準。
`AUTHORITY_ACQUIRE_FAILED` 仍要求 `authority_acquire_attempted is True`。

## 10. DEFECT #3 — artifact evidence wiring（FIXED）

`HandoffRunner` 把 `PauseVerifier` / `RestoreVerifier` / ownership /
release / post-check 的 canonical 結果外露到 result；
`collect_handoff_artifact()` 在呼叫端未提供時改用它們（**不重新推算**）。

```
pause_verifier_state / normal_loop_halted / discriminative_pause_verified
restore_verifier_state / restore_resumed / release_before_restore
post_check / final_ownership
```
`normal_loop_halted` 由 PauseVerifier 取樣同步收集的 loop marks ＋ canonical
`loop_active_from_marks()` 推得（沿用既有規則，未新增 threshold）。
`final_ownership`：走到 step 12 的前提就是 `RV_RESUMED`，其 canonical 語意即
external loop 已恢復，故 `check_ownership(external_running=True, ...)`（附
OLD/NEW/WHY/EVIDENCE）。沒有 evidence 時仍維持 `None` / `False`，**不造假**。

## 11. External liveness 歷程（additive）

```
pre_pause_external_liveness / post_pause_external_liveness /
post_restore_external_liveness / external_liveness_history
external_liveness_final（backward compatible，等同舊 external_liveness）
```
階段由**實際送出的動詞**推進（pause -> post_pause、restore -> post_restore），
不由 provider 重新判斷現場狀態。

## 12. Attempt #4 RECONSTRUCTION（offline，mock live leg）

```
Fresh Safety PASS / GATE A PASS / PRE_PAUSE 11/11 / pause SENT /
PauseVerifier PASS / ownership NEITHER / GATE B PASS /
authority acquire simulated SUCCESS
  -> permit 授權成立
  -> 進入 scope：DISPATCH_ENABLED = True（R4-4）
  -> mock live leg：CHARGE 5.0 kW ×1（R4-5）
  -> 離開 scope：DISPATCH_ENABLED = False（R4-6）
  -> permit 事件 = [PERMIT_ENTERED, PERMIT_EXITED]（R4-7）
  -> CHARGE 計數 1 / DISCHARGE 0 / 無違規（R4-9、R4-10）
  -> authority 已釋放（R4-11）  outcome = DISPATCH_SUCCEEDED（R4-12）
對照組 R4-14：無 permit -> live leg 期間 DISPATCH_ENABLED 仍為 False
```

## 13. Regression

```
test_phase6_attempt4_postmortem.py   108 / 108（D×34、D-7×8、A×10、LH×6、
                                      CL×8、RL×13、R4×14）
Full Phase 6   6471 / 6471 PASS，59 files，0 FAIL   （baseline 6361 / 58）
```
未刪 test、未放寬 assertion、未改 expected result。
3 處既有斷言變更皆附 OLD/NEW/WHY/EVIDENCE，且**全為收緊或等價**：
`test_phase6_scheduled_launcher.py` L17（stdlib allowlist 增列 `inspect`，
「只准 stdlib」的本質未放寬）、
`test_phase6_610_handoff.py` 與 `test_phase6_610b2_handoff_runner.py`
（`DISPATCH_ENABLED = True` 的**文字掃描改為 AST**，避免被自己的說明註解誤觸，
並額外斷言模組層 rest state 為 False —— 比原斷言更嚴格）。

## 14. 安全狀態

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0
本輪 authority acquire 0 ／armed transport 0 ／SSH 動詞 0 ／HMI 連線 0
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
FIRST LIVE HOLD          Unattended LIVE HOLD
ATTEMPT #5 = NOT AUTHORIZED（未建立 config、未建立 Task）
```


---

# SCHEDULED LIVE-LEG AUTHORIZATION（2026-09-24 裁示 PART A~M）

```
SCHEDULED LIVE-LEG AUTHORIZATION = VERIFIED
ATTEMPT #5                       = READY FOR NEW FIELD AUTHORIZATION（≠ AUTHORIZED）
```

## 1. `confirm_live_leg` 正式契約（source inspection）

```python
def confirm_live_leg(leg, prompt=None):
    want = PRD.confirmation_phrase(leg)          # 例：CONFIRM CHARGE 5KW
    got  = (prompt or input)("  > ")
    ok   = isinstance(got, str) and got.strip() == want
    return ok
```

| 項目 | 事實 |
|---|---|
| signature | `confirm_live_leg(leg, prompt=None)` |
| 需要的確認 | **完整字串** `CONFIRM {ACTION} {POWER:g}KW`，由既有 `PRD.confirmation_phrase()` 計算；刻意不接受 `y` / `yes` / 任意非空字串 |
| 確認來源 | `prompt` **未注入** -> `input()`（stdin，真人）；**已注入** -> 由呼叫端提供的 callable 回傳確認證據 |
| stdin 不存在時 | `input()` 立即 `EOFError`（實測：stdin=DEVNULL 時 `sys.stdin.read() == ""`）。**絕不會**被誤判成通過 |
| Scheduled Task（InteractiveToken、無人輸入） | 若走 `input()` 路徑 -> EOF -> 例外 -> dispatcher 記為 UNKNOWN -> **不會** hang、不會等 30 分鐘 |

🔴 設計目的判定：`confirm_live_leg` 是 **互動 CLI safeguard**（防止 CLI 誤操作），
其 `prompt` 參數本來就是**注入點**；它**不是**「每次 dispatch 都必須真人即時輸入」
的不可替代條件。

## 2. 🔴 發現的缺口（Attempt #4 當時已存在）

```python
# OLD（provider）
prompt=(lambda _p: PRD.confirmation_phrase(PRD.LiveLegIntent(FL.FIRST_LIVE_ACTION)))
```
這個 lambda **無條件**回傳正確確認字串 —— 與 permit / attempt_id / authority /
GATE B / one-shot **毫無綁定**，實質等同 auto-confirm。
即使 Attempt #4 沒走到 step 7，這仍是必須修掉的 bypass。

## 3. NEW — Scheduled live-leg authorization adapter

```
INTERACTIVE PATH : human confirmation                 -> confirm_live_leg()
SCHEDULED PATH   : validated FirstLiveDispatchPermit  -> confirm_live_leg()
                   （ScheduledLiveLegAuthorization）
```
**兩條路徑進入同一道 gate**；scheduled path 沒有繞過整層的捷徑，
`confirm_live_leg()` 本身**一行未改**。

`ScheduledLiveLegAuthorization.__call__()` 只有在下列**全部**成立時才回傳
canonical 確認字串，否則回 `SCHEDULED-AUTHORIZATION-DENIED`（必然不等於 `want`）：

```
permit_present               permit_active_scope（必須正在 PERMIT_ACTIVE scope 內）
action_is_first_live         action_is_canonical（= charge）
authorization_not_used       binding_complete
binding_attempt_id_matches   binding_action_matches
power_from_production_config binding_power_matches
```

## 4. PERMIT → LIVE AUTHORIZATION BINDING（PART G）

```
SCHEDULED_AUTH_BINDING = (attempt_id, action, power_kw,
                          target_date, window_start, window_end)
```
`power_kw` **不接受呼叫端傳入**，一律由 `PRD.leg_power_kw(action, config)` 從
ProductionConfig 解析（= 5.0 kW）。若實際 leg 功率與 canonical 不同，
`want` 自然不同 -> 結構性拒絕（C9 鎖住）。
permit 端另有 16 項前置條件（含 `inside_authorized_window`、`gate_b_ok`、
`authority_acquired`、`charge_budget_available`、`permit_not_used`）。

結果：**permit → authorization 只能產生一次 CHARGE 5.0 kW**；
DISCHARGE / 任意功率 / 第二次 command 皆被拒。

## 5. LiveLegAuthorization 契約（PART F，既有，未新增第二套）

| # | 項目 | 事實 |
|---|---|---|
| 1 | constructor | `LiveLegAuthorization(action, config=None)` —— **無 power 參數** |
| 2 | armed 預設 | 建構即 `LEG_ARMED`（`armed` 為衍生屬性） |
| 3 | 誰可以 arm | 只有建構者；**沒有公開 `arm()`** |
| 4 | arm 前置 | `leg_power_kw()` 必須解析成功，否則不核發 |
| 5 | persistent | 否 —— 純 process 內物件，無 save/persist API |
| 6 | one-shot | 是 —— `dispatched_direction` 後同方向不再允許 |
| 7 | 綁定 CHARGE | 是 —— 方向不符一律拒絕；`stop` 只在方向指令後允許一次 |
| 8 | 綁定 attempt_id | **否** —— attempt 綁定由 permit ＋ scheduled authorization 提供 |
| 9 | 綁定 5.0 kW | 是 —— 來源唯一為 ProductionConfig |
| 10 | dispatch 後 consumed | 是 —— `consume()` -> `LEG_CONSUMED` |
| 11 | 例外後 disarm | 是 —— `abort()` -> `LEG_ABORTED` |
| 12 | process restart | 預設 disarmed（物件不存在；`build_executor` 需 `leg.armed`） |

## 6. NO BYPASS 證明（AST）

```
provider / handoff 模組內：
  0 處 --force / --yes / --skip-confirm / --no-confirm / AUTO_CONFIRM /
      --force-dispatch 字面值（排除 docstring）
  0 處 input() 引用
  0 處 subprocess stdin echo
  0 個「直接回傳 confirmation_phrase」的 lambda / 函式（AST 判定）
確認字串由 PRD.confirmation_phrase 計算，FL 內無 "CONFIRM CHARGE" 硬編
confirm_live_leg 原始碼三項特徵（signature / prompt-or-input / 完整比對）皆未變
```

## 7. NO INTERACTIVE BLOCKING（PART H，真實子行程，stdin=DEVNULL）

```
H1 scheduled path 不 hang / 不 timeout      PASS
H2 exit 0（未 crash）                        PASS
H3 無 EOFError                               PASS
H4 SCHEDULED_CONFIRMED=True（不需 stdin）    PASS
H5 該環境讀 stdin 立即 EOF                   PASS
H6 互動路徑（無 prompt）在無 stdin 時 EOF/DENY，絕不放行   PASS
```
⚠️ 記錄一項環境事實：Windows 的 `NUL` 裝置 `isatty()` 會回 **True**，
不能當「非互動」判準；真正的判準是**讀 stdin 立即 EOF**。

## 8. NO CONCURRENCY（PART K）

```
K1 scheduled process 相關 9 個模組內 0 處 Thread(...) 建立
K2 orchestrator 內以 DISPATCH_ENABLED 為條件的分支 **只有 1 處**
   （HandoffRunner step 7，主執行緒）
K3 其他模組唯一的 `X.DISPATCH_ENABLED` 分支 =
   pcs_auto_control_production.py `_observe_record()` 的
   `why = NO_ACTION_DISPATCH_DISABLED` —— **純報告分類**，不控制任何送出
K4 run_scheduled_first_live 未啟動 unattended service 迴圈
K5 live-leg 期間 RT.DISPATCH_ENABLED 未被改動
```
meter client 以 `socketio` 連線（其內部執行緒只接收電表資料，
`meter_client.py` 內 0 處 DISPATCH / executor / operator 引用）。
結論：**scheduled process 內不存在第二條 command path**，無 race。

## 9. REAL SCHEDULED RECONSTRUCTION（PART J）

只 mock **最後真正送設備的 transport**（`operator_run`，回傳 shape 沿用既有
`device_control_operator.run()` 契約）；confirmation 與 authorization 兩層
**完全不 mock**，走真正的 `run_live_leg_cli`：

```
confirm_live_leg（structured authorization）      PASS
live_leg_gates（decision / precheck / safety /
                authority / interlock）           PASS
LiveLegAuthorization（armed，5.0 kW）             PASS
build_executor(MODE_CONTROLLED_SINGLE_LEG)        PASS
LegBoundExecutor 授權閘 ＋ mark_dispatched        PASS
------------------------------------------------------------------
transport 實際收到 actions = ['charge', 'stop']
CHARGE 5.0 kW 恰好一次    DISCHARGE 0    guard 違規 0
live leg 期間 DISPATCH_ENABLED = True；離開後 False
scheduled authorization 已使用一次，permit 用畢不再授權
authority 已釋放（release 在 finally）
全程不需 stdin
```
⚠️ 誠實記錄：本重建的 `live_leg outcome` 收斂為 `DISPATCH_FAILED /
reason=OBSERVE_ONLY` —— 因為最小 chain 未驅動完整的 read-back 狀態機，
`leg_state != LEG_COMPLETE`。這**不影響**授權鏈的結論（授權層全部真實執行），
但也**不宣稱**已驗證完整 readback 流程。

## 10. Regression

```
test_phase6_scheduled_live_leg_auth.py   106 / 106
Full Phase 6   6577 / 6577 PASS，60 files，0 FAIL   （baseline 6471 / 59）
```
未刪 test、未放寬 assertion、未繞過 confirmation。本輪**未變更任何既有斷言**。

## 11. 安全狀態

```
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
FIRST LIVE HOLD   Unattended LIVE HOLD
Attempt #5 config / Task / ledger 皆未建立
```


---

# FULL LIVE-LEG READBACK — OFFLINE CLOSURE（2026-09-24）

```
FULL LIVE-LEG READBACK  = VERIFIED
FIRST LIVE MOCK SUCCESS = PASS
ATTEMPT #5              = READY FOR NEW FIELD AUTHORIZATION（≠ AUTHORIZED）
```

## 1. Readback call graph（source inspection）

```
run_live_leg_cli()
 └─ ControlledLiveLeg.run_leg()
     ├─ chain = bundle.factory(executor, verifier)      # ProductionExecutionChain
     ├─ res = chain.run()                               ← ① 方向指令
     │    ├─ arbiter.evaluate() -> authorized?
     │    ├─ _do_revalidate(az)                         # dispatch 前 fresh 複驗
     │    ├─ az.consume(action)                         # one-shot 授權票
     │    ├─ EX.execute_and_verify(ctrl, executor, verifier)
     │    │    ├─ LegBoundExecutor.send()               # leg 授權閘
     │    │    │    └─ PcsControlExecutor.send()
     │    │    │         └─ operator_run(action=pcs_charge, power=5.0)   ← ② command send
     │    │    └─ ReadBackVerifier.verify("charge")     ← ③④⑤ readback / poll / timeout
     │    ├─ VERIFY_SUCCESS -> store.update_from_verified_result()
     │    └─ 分類：VERIFY_MISMATCH / OUTCOME_UNKNOWN / COMMAND_NOT_SENT
     ├─ control_action 必須等於 leg.action
     ├─ readback_outcome 必須 VERIFY_SUCCESS            ← ⑥ failure condition
     ├─ lastcontrol_written 必須 True
     ├─ LEG_DISPATCHED -> LEG_VERIFIED -> LEG_OWNED
     ├─ observe() 必須 authority == OWNED_BY_PHASE6
     ├─ stop_res = chain.run()                          ← ⑧ cleanup STOP
     ├─ LEG_IDLE_VERIFIED -> LEG_REPORT_FINALIZED
     ├─ leg.consume()                                   ← ⑨ authorization consume
     ├─ LEG_COMPLETE
     └─ _teardown() -> LEG_OBSERVE_ONLY -> return       ← ⑦ cleanup
                                                          ⑩ authority release
                                                             由 FirstLiveDispatcher
                                                             的 finally 負責
```

## 2. Canonical readback success contract

```
timeout_sec            75.0     （ProductionConfig.readback_timeout_sec）
poll_interval_sec       5.0     （ProductionConfig.readback_poll_interval_sec）
stability_samples         1     （ProductionConfig.readback_stability_samples）
charge_power_kw         5.0     （ProductionConfig.charge_power_kw）
TARGET_STATE_MAP       charge -> CHARGING / discharge -> DISCHARGING / stop -> STOPPED
ACCEPTED_STATES_MAP    charge -> {CHARGING}      stop -> {STANDBY, STOPPED}
```

🔴 **成功條件只看 PCS 旗標**：連續 `stability_samples` 次觀測到可接受狀態即
`VERIFY_SUCCESS`。既有原始碼明文記載
「`actual_active_power_kw` 純觀測，不參與成功條件 —— 實機已證實 AC 功率會晚約
1 個 refresh cycle，等它回落會造成假 timeout」。

⚠️ 因此**不存在 readback power tolerance 門檻**。裁示提到的「tolerance = 1.0」
對應的其實是 `stability_samples = 1`（連續觀測次數），不是功率容差。
本輪**未新增**任何第二套 threshold。

失敗條件：PCS 旗標互斥 → **立即** `VERIFY_FAILED`（不等 timeout）；
逾時 → `VERIFY_TIMEOUT`；讀取缺失 / UNKNOWN → 繼續輪詢至 timeout。

## 3. 🔴 OBSERVE_ONLY ROOT CAUSE = PRODUCTION CONTRACT 缺口（非 harness 問題）

```
run_leg() 成功路徑終段：
    LEG_IDLE_VERIFIED -> LEG_REPORT_FINALIZED -> leg.consume()
    -> LEG_COMPLETE -> _teardown() -> LEG_OBSERVE_ONLY -> return self.state
```
**回傳值是 `LEG_OBSERVE_ONLY`**；`LEG_COMPLETE` 只存在於 `runner.history`。
而 `classify_dispatch_outcome()` 原本比對
`leg_state == LEG_COMPLETE` -> 不成立 -> `DISPATCH_FAILED`。

🔴 這代表**即使真實 Attempt #5 完全成功，也會被記成 `DISPATCH_FAILED`**。
屬 production contract 缺口，必須修 production，而不是改測試去迎合。

### 修正（附 OLD / NEW / WHY / EVIDENCE）

```python
def leg_reached_complete(live_outcome):
    # leg_state == LEG_COMPLETE（既有契約保留）
    # 或 runner.history 中**真的出現** LEG_COMPLETE（正面證據）
```
`classify_dispatch_outcome()` 改用它。**這是收緊**：要求正面完成證據，
而不是「不是 abort 就算過」；沒有 runner / 沒有 history 一律 False。

## 4. Harness / fixture 修正（只補 fixture，未改 production 判定）

- `FakeDevice._flags()`：`STANDBY` 的 canonical 旗標是 `running=True`
  （運轉中但未充放電）。原先寫成 `running=False` 會被
  `PCI.classify_pcs_state` 正確判成 **CONFLICT**。
- operator action 名稱使用 canonical `OPERATOR_ACTION_MAP`
  （`charge -> pcs_charge`、`stop -> pcs_stop_power`）。
- `operator_run` mock 回傳 shape 沿用既有 `device_control_operator.run()`
  契約（`control_success` 決定 executor 的 `sent`）。

## 5. Additive production 變更（evidence only，不改判定）

```
ControlledLiveLeg.dispatch_result / .stop_result   保留 chain 的 ExecutionResult
ExecutionResult.readback_attempts                  保留 ReadBackVerifier 的觀測樣本
FirstLiveDispatcher.last_live_outcome              保留 LiveLegOutcome
artifact 新增欄位：leg_reached_complete / leg_state_history /
                   readback_outcome / readback_observed_state /
                   readback_attempts / readback_elapsed_sec /
                   live_leg_authorization_consumed
```
以上皆為**新增欄位 / 新增保存點**，預設 None，不改變任何既有判定或順序。

## 6. FULL SUCCESS PATH（真正的狀態機）

```
device plan : STANDBY -> CHARGING -> CHARGING -> CHARGING -> STANDBY
actions     : ['charge', 'stop']
readbacks   : ['VERIFY_SUCCESS', 'VERIFY_SUCCESS']
history     : ARMED -> PRECHECKED -> DISPATCHED -> VERIFIED -> OWNED
              -> STOP_REQUESTED -> IDLE_VERIFIED -> REPORT_FINALIZED
              -> COMPLETE -> OBSERVE_ONLY
outcome     : DISPATCH_SUCCEEDED          ← 不再是 DISPATCH_FAILED
phase6_charge 1 / phase6_discharge 0 / phase6_pcs_stop <= 1 / violations 0
scheduled authorization consumed = True    permit = PERMIT_EXITED
live leg 期間 DISPATCH_ENABLED = True      離開後 False
authority released（release 在 finally）
operator 實收：pcs_charge ×1（power = 5.0）＋ pcs_stop_power ×1
```

## 7. NEGATIVE READBACK（R1 ~ R14，直接驅動 production ReadBackVerifier）

```
R1  readback 永不改變      -> VERIFY_TIMEOUT，elapsed >= 75 s
R2  讀值缺失 / stale       -> 不得判成功
R3  方向錯誤（DISCHARGING）-> 不得判成功
R4  PCS 已 CHARGING、功率尚未跟上 -> 仍 VERIFY_SUCCESS
    （canonical：功率純觀測；功率值仍完整記錄）
R5  PCS 旗標互斥           -> 立即 VERIFY_FAILED（elapsed < poll）
R6  通訊中斷               -> 不得判成功
R7  SOC 不合理 / 超出 0~100 -> live_leg_precheck.soc_sane FAIL（dispatch 前擋下）
R8  401 可恢復             -> bounded re-auth（<= 2）後讀取繼續 -> VERIFY_SUCCESS
    且 verifier 結構上沒有 executor 參考 -> 不可能重送指令
R9  401 不可恢復           -> 不得判成功
R10 readback 成功          -> VERIFY_SUCCESS，attempts 完整保留
R12 STOP 接受 STANDBY      -> VERIFY_SUCCESS
R13 STOP 未達成            -> VERIFY_TIMEOUT（不得重送）
R14 新 process             -> leg guard 未使用、dispatch False，無自動 retry
```

## 8. NO COMMAND RESEND

```
G1 readback 多輪輪詢（含 401 re-auth）期間 CHARGE 只送出一次
G2 CommandCountGuard phase6_charge <= 1
G3 無 guard 違規
G4 結束後 dispatch flag 已 reset
G5 readback **逾時**路徑 CHARGE 仍只送出一次（不重送）
G6 逾時路徑 outcome 不得宣稱成功
G7 逾時後 dispatch flag 仍 reset
```

## 9. FULL SUCCESS ARTIFACT

```
dispatcher_attempted True          dispatcher_outcome DISPATCH_SUCCEEDED
leg_reached_complete True          leg_state_history 含 COMPLETE
readback_outcome VERIFY_SUCCESS    readback_observed_state CHARGING
readback_attempts > 0              readback_elapsed_sec 已記錄
live_leg_authorization_consumed True
phase6_command_counts charge 1 / discharge 0
phase6_authority_released True
```
無 None / default False 掩蓋已完成 evidence（A12 鎖住）。

## 10. Regression

```
test_phase6_live_leg_readback.py         94 / 94
test_phase6_scheduled_live_leg_auth.py  106 / 106
Full Phase 6   6671 / 6671 PASS，61 files，0 FAIL   （baseline 6577 / 60）
```
未刪 test、未放寬 assertion、未繞過 readback、未把 `OBSERVE_ONLY` 當 PASS
（S6 明文斷言 outcome != DISPATCH_FAILED）。本輪**未變更任何既有斷言**。

## 11. 安全狀態

```
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
FIRST LIVE HOLD   Unattended LIVE HOLD
Attempt #5 config / Task / ledger 皆未建立
```


---

# ATTEMPT #5 — PRE-FIELD READINESS CLOSURE（2026-09-24，正式狀態）

```
FULL LIVE-LEG READBACK             = VERIFIED
SCHEDULED LIVE-LEG AUTHORIZATION   = VERIFIED
FIRST LIVE MOCK SUCCESS            = PASS
FULL PHASE 6                       = 6671 / 6671 PASS（61 files，0 FAIL）

ATTEMPT #5                         = READY FOR NEW FIELD AUTHORIZATION
ATTEMPT #5                         = NOT AUTHORIZED
FIRST LIVE SUCCESSFUL REAL 5 kW LEG = NOT YET COMPLETED
Unattended LIVE                    = HOLD
```

## 1. FULL LIVE-LEG READBACK = VERIFIED

已完整驗證正式 production chain：

```
run_live_leg_cli -> ControlledLiveLeg.run_leg() -> ProductionExecutionChain
-> CHARGE dispatch -> ReadBackVerifier -> VERIFY_SUCCESS -> LEG_COMPLETE
-> cleanup STOP -> authorization consume -> teardown -> authority release
```
canonical 參數沿用 production：`readback timeout 75.0 s`／`poll 5.0 s`／
`stability_samples 1`／`CHARGE power 5.0 kW`。

🔴 readback success contract **依 PCS state flag 判定**；
`actual_active_power_kw` 僅作 observation，**不參與成功 gate**。
**不得新增 power tolerance。**

## 2. OBSERVE_ONLY CLASSIFICATION DEFECT（已修正）

```
原 production flow : LEG_COMPLETE -> _teardown() -> LEG_OBSERVE_ONLY -> return
舊判定             : returned state == LEG_COMPLETE  -> 成功路徑被誤分類
新判定             : history contains LEG_COMPLETE（正面 evidence）
                     -> DISPATCH_SUCCEEDED
```
🔴 **不得直接把 `OBSERVE_ONLY` 當成功**；成功仍必須有 `LEG_COMPLETE`
的 positive evidence。

## 3. 🔴 歷史 ATTEMPT #1 ~ #4 說明更正

**更正前（措辭易生誤解）**
> 「Attempt #1–#4 期間若曾成功，也會被誤記為失敗」

**正式更正為**
> 舊 `classify_dispatch_outcome` 對「**真正已走到 `LEG_COMPLETE`**、
> 之後 teardown 回 `OBSERVE_ONLY`」的成功路徑，存在**錯誤分類風險**。
> 但 **Attempt #1 ~ #4 實際都沒有成功完成 5 kW live leg**，
> 因此**不得**回頭把任何一次改判為成功。

歷史維持不變：

| Attempt | 正式結果 |
|---|---|
| #1（2026-09-18） | 未完成 real 5 kW live leg（`ABORTED_CURRENT_CONDITIONS`） |
| #2（2026-09-22） | GATE A 前中止（`ABORTED_CURRENT_CONDITIONS` / root cause `PROVIDER_WIRING_DEFECT`） |
| #3（2026-09-23） | `ABORTED_PRE_PAUSE_PREFLIGHT` |
| #4（2026-09-24） | `ABORTED_AT_7_FIRST_LIVE` |

```
FIRST LIVE successful real 5 kW leg = 仍為 0 次
```

## 4. FIRST LIVE MOCK SUCCESS = PASS

完整 OFFLINE reconstruction 逐項通過：
Fresh Safety／GATE A／PRE_PAUSE 11-11／pause SENT／PauseVerifier／GATE B／
authority acquire SUCCESS／`ScheduledLiveLegAuthorization`／
`FirstLiveDispatchPermit`／**CHARGE 5.0 kW exactly once**／readback
`VERIFY_SUCCESS`／`LEG_COMPLETE`／cleanup STOP bounded／dispatch scope reset／
authorization consumed／permit consumed／authority released／restore SENT／
RestoreVerifier／final ownership `EXTERNAL`。

## 5. NO-RESEND

```
phase6_charge <= 1     DISCHARGE = 0     STOP <= 1
readback poll   -> 不得重新送 CHARGE
401 re-auth     -> 只 retry READ（bound 2）
timeout         -> 不得重新送 CHARGE
UNKNOWN         -> 不得 resend
```

## 6. SCHEDULED LIVE-LEG AUTHORIZATION = VERIFIED

scheduled path 不依賴 stdin、不使用 auto-confirm，且**不存在**
`--force` / `--yes` / `--skip-confirm` / `--no-confirm` / `AUTO_CONFIRM`。
structured binding：`attempt_id` / `action` / `power_kw` /
`target_date` / `window_start` / `window_end`；只允許 **CHARGE 5.0 kW one-shot**。

## 7. DISPATCH SCOPE

```
REST                    HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
AUTHORIZED SCOPE 內     HO.DISPATCH_ENABLED temporarily True
                        （需 authority acquired ＋ valid FirstLiveDispatchPermit）
離開 scope（PASS / FAIL / exception）  HO.DISPATCH_ENABLED False
全程                    RT.DISPATCH_ENABLED False
                        RemoteSenders armed False ／ SshTransport armed False
```

## 8. REGRESSION

```
Full Phase 6                       6671 / 6671 PASS，61 files，0 FAIL
test_phase6_live_leg_readback.py     94 / 94 PASS
```
未放寬 assertion、未刪 test、未把 `OBSERVE_ONLY` 當成功、未繞過 readback。

## 9. CURRENT FIELD COUNTS

```
Phase 6 real cumulative   CHARGE 0 ／ DISCHARGE 0 ／ PCS STOP 0
External B2 cumulative    pause 3 ／ restore 3
FIRST LIVE successful real 5 kW leg = NOT YET COMPLETED
```

## 10. ATTEMPT #5 READINESS

13 項條件皆 PASS：full readback state machine／OBSERVE_ONLY root cause／
full success fixture／CHARGE exactly once／no-resend／LEG_COMPLETE／
cleanup bounded／permit consumed／authorization consumed／
dispatch flags reset／authority release／restore path／full regression。

```
ATTEMPT #5 = READY FOR NEW FIELD AUTHORIZATION
```
🔴 READY **≠** AUTHORIZED、**≠** TASK CREATED、**≠** EXECUTED。

## 11. ATTEMPT #5 現況

```
Attempt #5 config  = NOT CREATED
Attempt #5 Task    = NOT CREATED
Attempt #5 ledger  = NOT CREATED
FIRST LIVE         = HOLD
Unattended LIVE    = HOLD
```
不得自行建立 Attempt #5／Task／trigger，不得 pause／restore／CHARGE／
DISCHARGE／PCS STOP／authority acquire／arm transport。
**等待使用者新的明確 field authorization。**

## 12. 若後續授權 ATTEMPT #5

必須重新準備：**全新** Attempt ID／config／ledger／artifact／one-shot Task。
**不得沿用 Attempt #4。**
若安排下一個 OFF_PEAK 夜間窗口，必須重新取得
fresh host state／fresh external baseline／fresh loop evidence／
fresh provider validation／fresh safety state；
正式 execution 全部使用 fresh evidence。


---

# FIRST LIVE ATTEMPT #5 — FIELD TASK CREATION（2026-09-24）

```
FIELD TASK CREATION   = PASS
FIRST LIVE ATTEMPT #5 = AUTHORIZED / WAITING FOR SCHEDULED TRIGGER
Unattended LIVE       = HOLD
```

🔴 **本輪只建立 Task，未手動 `/Run`，未執行任何 field control。**

授權依據：使用者 2026-09-24「正式授權 ATTEMPT #5，2026-09-25 00:05，OFF_PEAK 窗口」。
⚠️ 授權僅指明起始時刻；`window_end` 沿用 Attempt #2 ~ #4 既有慣例 **00:30**（25 分鐘視窗），已於 config 中明示。

## STEP 0 — VALIDATE-ONLY REHEARSAL

```
task                 Phase6_FirstLive_ValidateOnly_D（執行後已刪除）
LastTaskResult       0
config_validation    CONFIG_OK
provider_validation  7 / 7 全 true
scheduled_result     VALIDATE_ONLY_NOT_EXECUTED
exit_classification  HANDLED_TERMINAL_RESULT
field_side_effects   0
ledger / artifact    NOT CREATED
```
rehearsal config 的 `target_date` 設為過去日（2026-09-22）：即使誤以非
`--validate-only` 呼叫，也只會得到 `WRONG_TARGET_DATE` 且 0 command。

## WINDOWS HOST（fresh verify，唯讀）

```
AC Sleep  (SUB_SLEEP/STANDBYIDLE)  0x00000000 = Never   ✅（未執行任何 setacvalueindex）
AC Hibernate                       0x00000000
VIDEOIDLE                          0x0000012c = 300 s（顯示器關閉，非 Sleep；未觸碰）
本機時間 / 時區                    2026-09-24 10:54  Taipei Standard Time
Task Scheduler 服務                Running
登入工作階段                       water / console / Active（自 2026-09-16 08:56）
Microsoft.Update.SystemInfo.RebootRequired   False
```

## LEGACY TASK CHECK（fresh verify）

```
Phase6_FirstChargeLive_Precheck_OneTime        Disabled   Next Run = N/A
Phase6_FirstLive_Charge_OneTime                Disabled   Next Run = N/A
Phase6_Scheduled_FirstLive_Attempt2_20260922   Disabled   Next Run = N/A
Phase6_Scheduled_FirstLive_Attempt3_20260923   Disabled   Next Run = N/A
Phase6_Scheduled_FirstLive_Attempt4_20260924   Disabled   Next Run = N/A
Phase6_FirstLive_ValidateOnly_D                DELETED
```
除 Attempt #5 外，無其他可執行 future Task。

## EXTERNAL BASELINE（fresh verify，唯讀）

```
2026-09-24 10:54
probe outcome        SSH_OK
auto_control.py      RUNNING          pid = 57483
cwd / cmd            /home/etica/ems  /  python3 auto_control.py     ✅
screen_count 1       guard_variant B2       capability_ok True
loop marks (x3)      10:54:23 -> 10:54:32 -> 10:54:41（嚴格遞增）

canonical external liveness
  symbol   phase6_remote_adapter.external_liveness()
  min_loop_observations = 3（= HandoffConfig.restore_loop_min_observations）
  verdict  EXTERNAL_RUNNING
  detail   runtime 健康且 loop evidence 前進

VERDICT = BASELINE_READY（10 / 10）
SSH verbs = {probe: 1, loopcheck: 3}（唯讀，transport armed = False）
pause 0 ／restore 0 ／authority acquire 0 ／PCS command 0
```

## FORMAL CONFIG

`test/scheduled_first_live_attempt5_20260925.json`
```
enabled              true
attempt_id           FIRST-LIVE-ATTEMPT5-20260925
target_date          2026-09-25
window_start/end     00:05 / 00:30
timezone             Asia/Taipei
dependency_provider  phase6_first_live_field -> phase6_first_live_field_provider
ledger_path          output/phase6_scheduled_first_live/FIRST-LIVE-ATTEMPT5-20260925.jsonl
artifact_dir         output/phase6_scheduled_first_live/attempt5_20260925
```
離線驗證：`load_config = CONFIG_OK`，problems = []。`window_state` 推演：
`00:04 BEFORE_WINDOW` → `00:05 INSIDE_WINDOW` → `00:29 INSIDE_WINDOW` →
`00:31 WINDOW_EXPIRED`。
🔴 全新 ledger／artifact／wrapper／Task XML，**未沿用** Attempt #2／#3／#4／rehearsal；
三份舊 ledger 檔案未被觸碰。

## TASK

`Phase6_Scheduled_FirstLive_Attempt5_20260925`（顯式 XML 註冊）
```
Trigger              2026-09-25T00:05:00      NextRunTime = 2026/9/25 00:05
LastRunTime          11/30/1999（哨兵值）-> 從未執行，未手動 /Run
Schedule Type        One Time Only
Repeat Every/Until   Disabled / Disabled（Repetition Interval/Duration 皆空）
RestartCount         0（XML 無 RestartOnFailure -> 無 auto retry）
StartWhenAvailable   False
MultipleInstances    IgnoreNew    ExecutionTimeLimit PT1H
WakeToRun False  RunOnlyIfIdle False  StopOnIdleEnd False
DisallowStartIfOnBatteries / StopIfGoingOnBatteries = False
Principal            Water ／ InteractiveToken ／ Limited
Action               "D:\Crawler Sample\test\run_first_live_attempt5_20260925.cmd"
State                Ready / Enabled
```

## 本次將首度套用的機制（Attempt #4 之後）

```
FirstLiveDispatchPermit        16 項前置條件，permit_not_used
first_live_dispatch_scope      authority acquired 後才 temporarily True；
                               finally 無條件 False
ScheduledLiveLegAuthorization  非互動 structured authorization，
                               不依賴 stdin、無 auto-confirm
leg_reached_complete           以 history 中的 LEG_COMPLETE 正面證據判定成功
launcher pause intent hook     scheduled ledger 的 INTENT 在 send 前一刻落盤
```
🔴 Attempt #5 是**第一次**有機會真正送出 CHARGE 5.0 kW 的 Attempt。

## EVIDENCE PRESERVED

```
config    test/scheduled_first_live_attempt5_20260925.json
Task XML  test/Phase6_Scheduled_FirstLive_Attempt5_20260925.xml
wrapper   test/run_first_live_attempt5_20260925.cmd
baseline  output/phase6_field_validation/attempt5_baseline_20260924_105442.json
rehearsal output/phase6_scheduled_first_live/rehearsal_20260924d_task.log
```

## FINAL SAFETY STATE

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0
本輪 authority acquire 0 ／armed transport 0
本輪 SSH 動詞 = probe 1 + loopcheck 3（唯讀）
dispatch rest state  HO False ／ RT False
RemoteSenders armed False   SshTransport armed False
Attempt #5 ledger / artifact 尚未建立
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
FIRST LIVE successful real 5 kW leg = NOT YET COMPLETED
Unattended LIVE HOLD
```


---

# ATTEMPT #5 MISSED CLOSURE ＋ ATTEMPT #6 TASK CREATION（2026-09-29）

```
ATTEMPT #5 = MISSED / NOT EXECUTED
ROOT CAUSE = HOST POWERED OFF AT SCHEDULED TRIGGER
ATTEMPT #6 = AUTHORIZED / WAITING FOR 2026-09-30 00:05 SCHEDULED TRIGGER
Unattended LIVE = HOLD
```

## 1. ATTEMPT #5 — 正式關閉

```
Trigger            2026-09-25 00:05
ledger             NOT CREATED
artifact           NOT CREATED
launcher log       NOT CREATED
  -> wrapper       NOT EXECUTED
  -> runner        NOT STARTED
Fresh Preflight    NOT EXECUTED      GATE A     NOT EXECUTED
PRE_PAUSE          NOT EXECUTED
pause 0 ／restore 0 ／authority acquire 0 ／CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0
ONE_SHOT RUNNER CONSUMED = False
scheduled window         = EXPIRED
```

🔴 wrapper 第一行即以重導向建立 launcher log —— **log 不存在即證明 wrapper 從未被執行**。

### 根因（Windows 事件記錄）

```
2026-09-24 18:04   User32 1074    使用者發動關機
2026-09-24 18:04   EventLog 6006  事件記錄服務停止（系統關機）
      ───────  主機關機 4 天 15 小時  ───────
2026-09-29 08:56   EventLog 6005  事件記錄服務啟動（系統開機）
2026-09-29 08:57   使用者 water 登入
```
Trigger 時刻主機關機；Task 依裁示設定 `StartWhenAvailable = False`、
`WakeToRun = False`，故**不執行、不補跑、不喚醒主機**。
`LastRunTime 2026-09-29 09:04:17 / LastTaskResult 0x800710E0` 是開機後
Task Scheduler 對已過期 one-time trigger 的拒絕結果，**不是** wrapper 執行。

### Attempt #5 Task 處置

evidence 保存後 **Disabled**（未刪除）：

```
FIRST-LIVE-ATTEMPT5-20260925.task.missed.xml       Task XML 快照
FIRST-LIVE-ATTEMPT5-20260925.task.missed.txt       Task metadata 快照
FIRST-LIVE-ATTEMPT5-20260925.missed_closure.json   正式結案紀錄
test/scheduled_first_live_attempt5_20260925.json   原 config（保留）
test/run_first_live_attempt5_20260925.cmd          原 wrapper（保留）
test/Phase6_Scheduled_FirstLive_Attempt5_20260925.xml
```
🔴 **不得重新啟用或沿用 Attempt #5 Task。**

## 2. HISTORICAL STATUS

| Attempt | 正式結果 |
|---|---|
| #1（2026-09-18） | 未完成 5 kW live leg |
| #2（2026-09-22） | GATE A 前中止 —— `PROVIDER_WIRING_DEFECT` |
| #3（2026-09-23） | `ABORTED_PRE_PAUSE_PREFLIGHT` |
| #4（2026-09-24） | `ABORTED_AT_7_FIRST_LIVE` |
| #5（2026-09-25） | **MISSED / NOT EXECUTED** —— HOST POWERED OFF |

```
FIRST LIVE successful real 5 kW leg = 0 次
```
🔴 Attempt #5 雖未執行，仍保留**獨立歷史身份**；下一次使用全新編號 **#6**。

## 3. 新增正式前提 — HOST_POWERED_ON_REQUIRED = True

自 Attempt #6 起，建立正式 Task 前必須 fresh verify 五項：

```
1. Windows host powered on   YES（LastBootUpTime 2026-09-29 08:56:35）
2. Water session             water / console / Active（登入 2026-09-29 08:57）
3. AC STANDBYIDLE            0x00000000 = Never
4. pending reboot            RebootRequired = False；CBS / WU key 皆不存在
5. Task Scheduler            Running

HOST POWER REQUIREMENT = ACKNOWLEDGED
```

### 人工作業要求（至 2026-09-30 00:05 前）

```
主機必須     POWERED ON
Water 帳號   LOGGED IN
允許         Win + L
禁止         Shut down ／Restart ／Log out ／Switch user ／
             Manual Sleep ／Hibernate
```

🔴 **Task Scheduler 安全設定維持不變**：`StartWhenAvailable = False`、
`WakeToRun = False`、`RestartCount = 0`、`Repeat Disabled`、
`MultipleInstances = IgnoreNew`。
**不因 Attempt #5 missed 而改成補跑或自動喚醒** —— 補跑可能發生於非授權時窗。

## 4. VALIDATE-ONLY REHEARSAL

```
task                 Phase6_FirstLive_ValidateOnly_E（執行後已刪除）
LastTaskResult       0
config_validation    CONFIG_OK
provider_validation  7 / 7 全 true
scheduled_result     VALIDATE_ONLY_NOT_EXECUTED
field_side_effects   0
ledger / artifact    NOT CREATED
```

## 5. FRESH EXTERNAL BASELINE（2026-09-29 10:44）

```
probe outcome     SSH_OK
auto_control.py   RUNNING        pid = 57483
cwd / cmd         /home/etica/ems  /  python3 auto_control.py
screen_count 1    guard_variant B2    capability_ok True
loop marks (x3)   10:44:14 -> 10:44:23 -> 10:44:32（嚴格遞增）
canonical external_liveness() = EXTERNAL_RUNNING
VERDICT = BASELINE_READY（10 / 10）
SSH verbs = {probe: 1, loopcheck: 3}（唯讀，armed = False）
```
🔴 pid 57483 自 2026-09-24 起未變 —— 對側 Linux 主機全程未重啟，
Windows 主機關機並未影響外部控制器。

## 6. ATTEMPT #6 CONFIG

`test/scheduled_first_live_attempt6_20260930.json`
```
enabled              true
attempt_id           FIRST-LIVE-ATTEMPT6-20260930
target_date          2026-09-30
window_start/end     00:05 / 00:30
timezone             Asia/Taipei
dependency_provider  phase6_first_live_field -> phase6_first_live_field_provider
ledger_path          output/phase6_scheduled_first_live/FIRST-LIVE-ATTEMPT6-20260930.jsonl
artifact_dir         output/phase6_scheduled_first_live/attempt6_20260930
```
`load_config = CONFIG_OK`（problems = []）。window_state 推演：
`00:04 BEFORE_WINDOW` → `00:05 INSIDE_WINDOW` → `00:29 INSIDE_WINDOW` →
`00:31 WINDOW_EXPIRED`。
🔴 全新 config／ledger／artifact／wrapper／Task XML，**未沿用 Attempt #5**；
舊 ledger（#2／#3／#4）檔案未被觸碰。

## 7. ATTEMPT #6 TASK

`Phase6_Scheduled_FirstLive_Attempt6_20260930`（顯式 XML 註冊）
```
Trigger              2026-09-30T00:05:00      NextRunTime = 2026/9/30 00:05
LastRunTime          11/30/1999（哨兵值）-> 從未執行，未手動 /Run
Schedule Type        One Time Only
Repeat Every/Until   Disabled / Disabled
RestartCount         0（XML 無 RestartOnFailure）
StartWhenAvailable   False        WakeToRun  False
MultipleInstances    IgnoreNew    ExecutionTimeLimit PT1H
RunOnlyIfIdle False  DisallowStartIfOnBatteries / StopIfGoingOnBatteries False
Principal            Water ／ InteractiveToken ／ Limited
Action               "D:\Crawler Sample\test\run_first_live_attempt6_20260930.cmd"
State                Ready / Enabled
```

## 8. PRODUCTION BASELINE（未變更）

```
FULL LIVE-LEG READBACK            VERIFIED
SCHEDULED LIVE-LEG AUTHORIZATION  VERIFIED
FIRST LIVE MOCK SUCCESS           PASS
Full Phase 6                      6671 / 6671 PASS，61 files，0 FAIL（2026-09-29 複跑）
```
本輪未變更任何 production code。

## 9. FIELD COUNTS（未因 Attempt #5 missed 增加）

```
Phase 6 real cumulative   CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0
External B2 cumulative    pause 3 ／restore 3
FIRST LIVE successful real 5 kW leg = NOT YET COMPLETED
```

## 10. FINAL SAFETY STATE

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0 ／authority acquire 0
本輪 SSH 動詞 = probe 1 + loopcheck 3（唯讀）
dispatch rest state  HO False ／RT False
RemoteSenders armed False   SshTransport armed False
Attempt #6 ledger / artifact 尚未建立
七個 Phase6 Task 中六個 Disabled，僅 Attempt #6 為 Ready
Unattended LIVE HOLD
```


---

# ATTEMPT #6 POSTMORTEM ＋ DISPATCHER FACTORY WIRING FIX（2026-09-30）

```
ATTEMPT #6      = EXCEPTION_FAIL_CLOSED
failure_stage   = DISPATCHER_CONSTRUCTION
ROOT CAUSE      = DISPATCHER FACTORY SIGNATURE / PROVIDER WIRING DEFECT
SAFETY          = SAFE FAIL-CLOSED
PRODUCTION PROVIDER CONSTRUCTION = VERIFIED
FIRST LIVE SUCCESSFUL REAL 5 kW LEG = 0
ATTEMPT #7      = READY FOR NEW FIELD AUTHORIZATION（≠ AUTHORIZED）
```

## 1. 實際中止位置

```
00:05:00  Task 觸發（主機開機、Water 登入，HOST_POWERED_ON_REQUIRED 條件達成）
00:05:01  HMI login success
00:05:03  PREFLIGHT fresh start
          Fresh Safety Preflight  PASS
          GATE A                  PASS
          Handoff readiness       PASS
00:05:34  build_first_live_dispatcher()  -> TypeError -> EXCEPTION_FAIL_CLOSED
------------------------------------------------------------------
HandoffRunner  NOT STARTED      pause  NOT ATTEMPTED
restore        NOT REQUIRED     authority acquire  NOT ATTEMPTED
5 kW live leg  NOT EXECUTED
```
ledger 僅 2 筆（PREFLIGHT / CONSUMED），handoff journal 於 2026-09-30 **0 筆**。

## 2. CONFIRMED DEFECT

```
caller  phase6_first_live_field_provider.py
        build_first_live_dispatcher(..., dispatch_permit=permit,
                                         permit_event=_permit_event)
factory phase6_first_live_handoff.build_first_live_dispatcher()
        signature 沒有 dispatch_permit / permit_event
   -> TypeError: unexpected keyword argument 'dispatch_permit'
```
2026-09-24 為 `FirstLiveDispatcher.__init__` 新增 permit 支援時，**漏改本工廠**。

## 3. OLD / NEW / WHY / EVIDENCE

| | 內容 |
|---|---|
| **OLD signature** | `(live_leg_runner, acquire_authority, release_authority, command_guard=None, recorder=None, audit_writer=None, action=FIRST_LIVE_ACTION, revalidator=None)` |
| **NEW signature** | 同上 ＋ `dispatch_permit=None, permit_event=None` |
| **OLD forwarding** | `FirstLiveDispatcher(live_leg_runner, lease, guard, recorder=, audit_writer=, action=, revalidator=)` |
| **NEW forwarding** | 同上 ＋ `dispatch_permit=dispatch_permit, permit_event=permit_event` |
| **WHY** | 新增 permit 支援時漏改工廠；provider 以 kwargs 呼叫即 TypeError |
| **EVIDENCE** | Attempt #6（2026-09-30 00:05:34）`TypeError: build_first_live_dispatcher() got an unexpected keyword argument 'dispatch_permit'` |

🔴 工廠**只做 argument forwarding**：不建立 permit、不產生 permit_event、
**不使用 `**kwargs`**（那會讓同類 wiring defect 再次被掩蓋）。

## 4. OBJECT IDENTITY PROOF

```
F2  dispatcher.permit        is  caller 傳入的 FirstLiveDispatchPermit   ✔
F3  dispatcher._permit_event is  caller 傳入的 callable                  ✔
F9  factory 內未建立第二份 FirstLiveDispatchPermit（AST）                ✔
F9b factory 內未建立 ScheduledLiveLegAuthorization（AST）                ✔
FM2 production provider 建出的 dispatcher 持有同一 permit                ✔
FM2b ScheduledLiveLegAuthorization 綁定同一 permit                       ✔
```
caller → factory → dispatcher 全程 **same object identity**。

## 5. REAL PROVIDER CONSTRUCTION TEST（本次最大缺口的補強）

新增 `test/test_phase6_provider_construction.py`（**57 / 57 PASS**）。

🔴 本檔**不自行重拼** `build_first_live_dispatcher(...)` 的呼叫 ——
由 **provider production code 自己**呼叫工廠。
未來 caller 多一個 kwarg 而 factory 沒跟上時，本檔會直接 FAIL。

```
走真正的  FP.build_field_dependencies(config, runtime_context)
          -> provider 內部 arm_and_run_handoff()
          -> FL.build_first_live_dispatcher()
          -> FirstLiveDispatcher / Phase6AuthorityLease / HandoffRunner

fake 注入  ssh_runner / api_client / ess_reader / relogin / meter_client /
          live_bundle / last_control_store / prime_classifier / journal_path
實測      construction smoke 期間 SSH 動詞 = []（0 verb、0 network、0 subprocess）
```
fake reading 的欄位沿用既有 `ess_snapshot_adapter._demo_reading()` 形狀；
SOC band 由既有 `TouArbitragePolicy.__call__(dinput)` 驅動（canonical path，
不直接碰 `_latch`、不自建第二套 band 判定）。

additive 注入點：`ctx["last_control_store"]`（未注入即維持既有 production 行為），
讓 smoke 不碰正式 LastControl 檔案。

## 6. VALIDATE-ONLY COVERAGE GAP（正式記錄）

```
validate-only 能證明   config / module imports / provider metadata /
                       safe defaults / interface presence / no field I/O on import
validate-only 不能證明  production field dependency graph 可以完整 construction
```
🔴 因此**不得**再把 validate-only PASS 描述成
「provider production construction PASS」。
Attempt #6 的三次 rehearsal 全數 PASS，卻無法發現本缺陷 ——
因為 `--validate-only` 依設計在 `build_dependencies()` **之前**就返回
（這正是它 `field_side_effects = 0` 的原因）。

## 7. 是否整合進 validate-only —— 評估結果：**不整合**

construction smoke 需要**注入 fake 依賴**才能保證零 I/O；
若塞進 launcher 的 `--validate-only`，等於要 production launcher 自備
test double，或在沒有 fake 的情況下真的去連 HMI／meter／SSH ——
兩者都不可接受。

正式作法：保留為**獨立的必要步驟**

```
python test/test_phase6_provider_construction.py
```

## 8. PRE-FIELD REQUIRED CHECK（自下一個 Attempt 起）

```
1. VALIDATE-ONLY REHEARSAL            = static / structural validation
2. OFFLINE PRODUCTION CONSTRUCTION SMOKE = real production dependency
                                           construction with fake I/O
```
**兩者都 PASS 才允許建立 field Task。**

## 9. ATTEMPT #6 RECONSTRUCTION

```
A6R1  provider 確實呼叫 build_first_live_dispatcher
A6R2  ★★ provider 的每個 kwarg 都在 factory signature 內
      實際 kwargs = acquire_authority / command_guard / dispatch_permit /
                    live_leg_runner / permit_event / recorder /
                    release_authority / revalidator
A6R3  dispatch_permit / permit_event 確實在 provider 呼叫中
A6R4  ★★ Attempt #6 條件重建 -> 無 TypeError
A6R5  三個 gate 皆可執行（與 Attempt #6 當時一致）
```

## 10. FULL PROVIDER -> FIRST LIVE MOCK

從 **production provider construction** 開始（不再從 `FirstLiveDispatcher` 中間切入）：

```
FP.build_field_dependencies -> provider gates -> production factory
-> dispatcher -> permit scope -> run_live_leg_cli -> ControlledLiveLeg
-> execute_and_verify -> ReadBackVerifier

actions   = ['charge', 'stop']
readbacks = ['VERIFY_SUCCESS', 'VERIFY_SUCCESS']
outcome   = DISPATCH_SUCCEEDED
CHARGE 5.0 kW ×1 ／DISCHARGE 0 ／PCS STOP <= 1 ／violations 0
LEG_COMPLETE ✔   permit consumed ✔   authorization consumed ✔
live leg 期間 DISPATCH_ENABLED = True；離開後 False ✔
authority released（finally）✔
```
只 mock 最底層 field transport（`operator_run`）與設備回讀。

## 11. failure_stage

scheduled runner 新增 `failure_stage`：

```
STAGE_DISPATCHER_CONSTRUCTION   例外發生時尚無 handoff_result
STAGE_HANDOFF_EXECUTION         例外發生時已有 handoff_result
```
Attempt #6 正式分類維持 `EXCEPTION_FAIL_CLOSED` ＋
`failure_stage = DISPATCHER_CONSTRUCTION`，
**不得**改寫成 GATE FAIL／PRE_PAUSE FAIL／AUTHORITY FAIL／DISPATCH FAIL。

## 12. HISTORY

| Attempt | 正式結果 |
|---|---|
| #1（2026-09-18） | 未完成 real 5 kW live leg |
| #2（2026-09-22） | GATE A 前中止 —— `PROVIDER_WIRING_DEFECT` |
| #3（2026-09-23） | `ABORTED_PRE_PAUSE_PREFLIGHT` |
| #4（2026-09-24） | `ABORTED_AT_7_FIRST_LIVE` |
| #5（2026-09-25） | `MISSED / NOT EXECUTED` —— HOST POWERED OFF |
| #6（2026-09-30） | `EXCEPTION_FAIL_CLOSED` —— DISPATCHER FACTORY SIGNATURE DEFECT |

```
FIRST LIVE successful real 5 kW leg = 0 次
```

## 13. REGRESSION

```
test_phase6_provider_construction.py    57 / 57
Full Phase 6   6728 / 6728 PASS，62 files，0 FAIL   （baseline 6671 / 61）
```
未刪 test、未放寬 assertion、未以 `**kwargs` 掩蓋 signature defect。
本輪**未變更任何既有斷言**。

## 14. FIELD COUNTS / SAFETY

```
Phase 6 real cumulative   CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0
External B2 cumulative    pause 3 ／restore 3
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
FIRST LIVE HOLD   Unattended LIVE HOLD
Attempt #7 config / Task / ledger 皆未建立
七個 Phase6 Task 全部 Disabled
```


---

# ATTEMPT #7 正式狀態 ＋ CONSTRUCTION SMOKE 前置規則（2026-09-30）

```
ATTEMPT #6                          = EXCEPTION_FAIL_CLOSED
ROOT CAUSE                          = DISPATCHER FACTORY SIGNATURE DEFECT
SAFETY                              = SAFE FAIL-CLOSED
PRODUCTION PROVIDER CONSTRUCTION    = VERIFIED
FIRST LIVE FULL MOCK                = PASS
FULL PHASE 6                        = 6728 / 6728 PASS（62 files，0 FAIL）
FIRST LIVE SUCCESSFUL REAL 5 kW LEG = 0 次
ATTEMPT #7                          = READY FOR NEW FIELD AUTHORIZATION
ATTEMPT #7                          = NOT AUTHORIZED
Unattended LIVE                     = HOLD
```

## 1. CONSTRUCTION_SMOKE_COMMAND（正式命令，無歧義）

```
CONSTRUCTION_SMOKE_COMMAND
    cd /d "D:\Crawler Sample"
    python test/test_phase6_provider_construction.py
```

實測（自 repo root `D:\Crawler Sample` 執行）：

```
檔案          test/test_phase6_provider_construction.py（存在，29,429 bytes）
結果          57 / 57 PASS
exit code     0
FAIL 行數     0
rest state    HO=False  RT=False
0 field I/O   PC3 未觸發任何 armed transport
              PC8 建構後 dispatch flags 仍為 rest state
              PC9 全程只用唯讀 SSH 動詞（construction smoke 期間實測 = []）
              PC12 safety_reset 回到 rest state
              測試檔未 import socket / requests / urllib / http /
                        socketio / paramiko / subprocess
```

⚠️ 曾於對話回報中看到的縮短檔名，是輸出端的**顯示截斷**，不是文件筆誤。
本專案不存在該縮短檔名的檔案；正式文件記載的一直是上列完整檔名。
（依裁示，該截斷字串不寫入文件。）

## 2. PRE-FIELD 正式前置步驟（六步，全部 PASS 才可建立 field Task）

```
1. Full Phase 6 Regression            PASS
2. Validate-only rehearsal            PASS
3. Production construction smoke      PASS   ← 本輪新增為必要步驟
4. Fresh host verify                  PASS
     powered on ／Water logged in ／AC Sleep = Never
     pending reboot = False ／Task Scheduler = Running
5. Fresh external baseline            PASS
     process healthy ／identity OK ／loop marks advancing
     canonical liveness = EXTERNAL_RUNNING
6. Fresh legacy Task scan             PASS
   -> 才允許建立 field Task
```

正式區分（**不得再混用**）：

| | 涵蓋範圍 |
|---|---|
| `--validate-only` | static / structural validation：config、module imports、provider metadata、safe defaults、interface presence、no field I/O on import |
| construction smoke | **real production dependency construction with fake field I/O** |

🔴 `validate-only PASS` **不得**再被描述為 `production provider construction PASS`。

## 3. ZERO FIELD I/O REQUIREMENT（construction smoke）

```
0 HMI network        0 Meter network      0 SSH network
0 subprocess field control
0 pause  0 restore  0 CHARGE  0 DISCHARGE  0 PCS STOP
0 authority acquire
```
所有 field dependency 一律 fake / injected：
`ssh_runner` ／ `api_client` ／ `ess_reader` ／ `relogin` ／ `meter_client` ／
`live_bundle` ／ `last_control_store` ／ `prime_classifier` ／ `journal_path`。

🔴 若任何情況下無法保證 0 field I/O，**不得**整合進 `--validate-only`。

## 4. VALIDATE-ONLY —— 正式決定：不整合 construction smoke

construction smoke 需要 fake dependency injection；塞進 launcher 的
`--validate-only` 等於要 production launcher 自備 test double，
或在沒有 fake 的情況下真的連 HMI／meter／SSH。兩者皆不可接受。

因此兩者維持獨立，**兩者都 PASS 才允許下一個 Attempt**。

## 5. FACTORY FIX（已完成）

```
OLD  (live_leg_runner, acquire_authority, release_authority,
      command_guard=None, recorder=None, audit_writer=None,
      action=FIRST_LIVE_ACTION, revalidator=None)
NEW  以上全部保留 ＋ dispatch_permit=None, permit_event=None
     -> FirstLiveDispatcher(..., dispatch_permit=dispatch_permit,
                                 permit_event=permit_event)
```
禁止 `**kwargs`；未知 kwarg 仍必須拋 `TypeError`（F5 / F5b 鎖住）。

## 6. OBJECT IDENTITY（已驗證）

```
dispatcher.permit         is  caller permit
dispatcher._permit_event  is  caller permit_event
production provider 建出的 FirstLiveDispatcher 與
ScheduledLiveLegAuthorization 使用**同一份**正式 permit
```
未重新建立第二份 permit／authorization／dispatch policy（AST 鎖住）。

## 7. ATTEMPT #6 REGRESSION

Attempt #6 production caller 的八個 kwargs
（`acquire_authority` / `command_guard` / `dispatch_permit` /
`live_leg_runner` / `permit_event` / `recorder` / `release_authority` /
`revalidator`）全部存在於 factory 正式 signature；
原 TypeError **cannot reproduce**。

## 8. FULL PROVIDER -> FIRST LIVE MOCK

自 production provider construction 開始，完整離線走到
dispatcher → HandoffRunner → scoped dispatch → CHARGE 5.0 kW → readback →
LEG_COMPLETE → cleanup STOP → release → restore：

```
DISPATCH_SUCCEEDED
CHARGE 5.0 kW ×1 ／DISCHARGE 0 ／PCS STOP <= 1 ／violations 0
permit consumed True ／authorization consumed True ／authority released True
dispatch scope exit 後 HO.DISPATCH_ENABLED = False
```

## 9. ATTEMPT HISTORY（不得回頭改判為成功）

| Attempt | 正式結果 |
|---|---|
| #1 | 未完成 real 5 kW live leg |
| #2 | GATE A 前中止 —— `PROVIDER_WIRING_DEFECT` |
| #3 | `ABORTED_PRE_PAUSE_PREFLIGHT` |
| #4 | `ABORTED_AT_7_FIRST_LIVE` |
| #5 | `MISSED / NOT EXECUTED` —— HOST POWERED OFF |
| #6 | `EXCEPTION_FAIL_CLOSED` —— DISPATCHER FACTORY SIGNATURE DEFECT |

```
FIRST LIVE successful real 5 kW leg = 0 次
```

## 10. ATTEMPT #7 現況

```
Attempt #7 config  = NOT CREATED
Attempt #7 Task    = NOT CREATED
Attempt #7 ledger  = NOT CREATED
FIRST LIVE         = HOLD
Unattended LIVE    = HOLD
```
不得自行建立 Attempt #7／Task，不得 pause／restore／CHARGE／DISCHARGE／
PCS STOP／authority acquire／arm transport。
**等待使用者新的明確 field authorization。**

## 11. 若後續授權 ATTEMPT #7

必須重新做完第 2 節的六步，並使用**全新**
Attempt ID／config／ledger path／artifact path／wrapper／Task XML。
**不得沿用 Attempt #6。**

## 11-A. FRESH HOST VERIFY —— 正式語意界定

建立 Task 前的五項 fresh verify：

```
Windows host      POWERED ON
Water             LOGGED IN
AC STANDBYIDLE    0 / Never
pending reboot    False
Task Scheduler    Running

正式 prerequisite：HOST_POWERED_ON_REQUIRED = True
```

🔴 **fresh verify 只代表「建立 Task 當下」成立** —— 它**不保證** trigger 時刻仍成立。
Attempt #5 正是在建立 Task 時條件全數達成、卻在 trigger 前主機被關機而 missed。

因此 trigger 前仍**必須由人工保持**：

```
主機         開機
Water 帳號   已登入

允許         Win + L
禁止         Shutdown ／Restart ／Log out ／Switch user ／
             Manual Sleep ／Hibernate
```
Task 維持 `StartWhenAvailable = False`、`WakeToRun = False`，
**不補跑、不自行喚醒主機**。

## 11-B. FRESH LEGACY TASK SCAN —— 正式要求

建立新 Attempt Task 前，重新確認所有舊
`Phase6` ／ `FirstLive` ／ `ValidateOnly` Task 狀態：

```
除新的 Attempt 外，不得存在其他 future executable Task
舊 Attempt        保持 Disabled
舊 Attempt NextRun = N/A / empty
```

## 12. NEXT AUTHORIZED ATTEMPT —— 正式九步

只有使用者後續**明確授權** Attempt #7，才依序執行：

```
1. Full regression fresh PASS
2. validate-only rehearsal
3. production construction smoke
4. fresh host prerequisite（HOST_POWERED_ON_REQUIRED = True）
5. fresh external baseline
6. fresh legacy Task scan
7. 建立全新 Attempt ID / config / ledger / artifact / wrapper / Task XML
8. 建立 one-shot field Task
9. STOP AND REPORT
```

🔴 不得手動 `/Run`、不得自動 retry、
**不得沿用 Attempt #6 的任何 execution artifact**。

## 13. FINAL SAFETY

```
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
FIRST LIVE HOLD   Unattended LIVE HOLD
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
七個 Phase6 Task 全部 Disabled
```


---

# FIRST LIVE ATTEMPT #7 — FIELD TASK CREATION（2026-09-30）

```
FIELD TASK CREATION   = PASS
FIRST LIVE ATTEMPT #7 = AUTHORIZED / WAITING FOR 2026-10-01 00:05 SCHEDULED TRIGGER
Unattended LIVE       = HOLD
```

🔴 **本輪只建立 Task，未手動 `/Run`，未執行任何 field control。**

授權依據：使用者 2026-09-30「正式授權執行 FIRST LIVE ATTEMPT #7，
2026-10-01，Trigger 00:05，Window 00:05 ～ 00:30，Asia/Taipei」。

## PRE-FIELD 六步（全部 fresh 執行，全部 PASS）

```
1. Full Phase 6 Regression      6728 / 6728 PASS，62 files，0 FAIL
2. Validate-only rehearsal      Phase6_FirstLive_ValidateOnly_F（已刪除）
                                LastTaskResult 0 ／CONFIG_OK ／
                                provider_validation 7-7 ／
                                VALIDATE_ONLY_NOT_EXECUTED ／
                                field_side_effects 0 ／ledger NOT CREATED
3. Production construction smoke  57 / 57 PASS，exit code 0，FAIL 行數 0
                                PC3 未觸發 armed transport
                                PC8 建構後 dispatch flags 仍為 rest state
                                PC9 全程只用唯讀 SSH 動詞
                                PC12 safety_reset 回到 rest state
4. Fresh host verify            powered on（LastBootUpTime 2026-09-29 08:56:38）
                                Water / console / Active（登入 2026-09-29 08:57）
                                AC STANDBYIDLE 0x00000000 = Never
                                RebootRequired False，CBS / WU key 皆不存在
                                Task Scheduler Running
5. Fresh external baseline      BASELINE_READY（10 / 10）
                                pid 57483 ／cwd /home/etica/ems ／
                                cmd python3 auto_control.py ／
                                screen_count 1 ／guard_variant B2 ／
                                capability_ok True
                                loop marks 10:35:49 -> 10:35:58 -> 10:36:07（遞增）
                                canonical external_liveness = EXTERNAL_RUNNING
                                SSH verbs = {probe 1, loopcheck 3}（唯讀）
6. Fresh legacy Task scan       7 個舊 Task 全部 Disabled，NextRun 全 N/A
                                future executable Task = 無
                                全系統掃描：無其他會執行本專案腳本的 future task
```

## ATTEMPT #7 CONFIG

`test/scheduled_first_live_attempt7_20261001.json`
```
enabled              true
attempt_id           FIRST-LIVE-ATTEMPT7-20261001
target_date          2026-10-01
window_start/end     00:05 / 00:30
timezone             Asia/Taipei
dependency_provider  phase6_first_live_field -> phase6_first_live_field_provider
ledger_path          output/phase6_scheduled_first_live/FIRST-LIVE-ATTEMPT7-20261001.jsonl
artifact_dir         output/phase6_scheduled_first_live/attempt7_20261001
```
`load_config = CONFIG_OK`（problems = []）。`window_state` 推演：
`00:04 BEFORE_WINDOW` → `00:05 INSIDE_WINDOW` → `00:29 INSIDE_WINDOW` →
`00:31 WINDOW_EXPIRED`。
🔴 全新 config／ledger／artifact／wrapper／Task XML，
**未沿用** Attempt #5／#6 任何 execution artifact；舊 ledger 檔案未被觸碰。

## ATTEMPT #7 TASK

`Phase6_Scheduled_FirstLive_Attempt7_20261001`（顯式 XML 註冊）
```
Trigger              2026-10-01T00:05:00      NextRunTime = 2026/10/1 00:05
LastRunTime          11/30/1999（哨兵值）-> 從未執行，未手動 /Run
Schedule Type        One Time Only
Repeat Every/Until   Disabled / Disabled（Repetition Interval/Duration 皆空）
RestartCount         0（XML 無 RestartOnFailure -> 無 auto retry）
StartWhenAvailable   False        WakeToRun  False
MultipleInstances    IgnoreNew    ExecutionTimeLimit PT1H
RunOnlyIfIdle False  DisallowStartIfOnBatteries / StopIfGoingOnBatteries False
Principal            Water ／ InteractiveToken ／ Limited
Action               "D:\Crawler Sample\test\run_first_live_attempt7_20261001.cmd"
State                Ready / Enabled
```
無 Daily／Recurring／On Startup／On Logon／Repeat every N minutes／Auto Retry。

## HOST POWER REQUIREMENT

```
HOST_POWERED_ON_REQUIRED = True
```
🔴 Attempt #5 已證明「建立 Task 時 host verify PASS」**不保證** trigger 時仍開機。
自建立 Task 起至 **2026-10-01 00:05**：

```
主機         必須保持 POWERED ON
Water 帳號   必須保持 LOGGED IN
允許         Win + L
禁止         Shutdown ／Restart ／Log out ／Switch user ／
             Manual Sleep ／Hibernate
```
**不得**改成 `StartWhenAvailable=True` 或 `WakeToRun=True`。

## EVIDENCE PRESERVED

```
config     test/scheduled_first_live_attempt7_20261001.json
wrapper    test/run_first_live_attempt7_20261001.cmd
Task XML   test/Phase6_Scheduled_FirstLive_Attempt7_20261001.xml
baseline   output/phase6_field_validation/attempt7_baseline_20260930_103607.json
rehearsal  output/phase6_scheduled_first_live/rehearsal_20260930f_task.log
```

## FINAL SAFETY STATE

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0 ／authority acquire 0
本輪 SSH 動詞 = probe 1 + loopcheck 3（唯讀）
dispatch rest state  HO False ／RT False
RemoteSenders armed False   SshTransport armed False
Attempt #7 ledger / artifact / launcher log 皆尚未建立
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
FIRST LIVE successful real 5 kW leg = 0 次
八個 Phase6 Task 中七個 Disabled，僅 Attempt #7 為 Ready
Unattended LIVE HOLD
```


---

# ATTEMPT #7 POSTMORTEM ＋ HANDOFF JOURNAL SCOPE FIX（2026-10-01）

```
ATTEMPT #7  = PAUSE_FAILED
ROOT CAUSE  = CROSS-ATTEMPT HANDOFF JOURNAL STATE LEAK
SAFETY      = SAFE FAIL-CLOSED
FIRST LIVE SUCCESSFUL REAL 5 kW LEG = 0 次
ATTEMPT #8  = READY FOR NEW FIELD AUTHORIZATION（≠ AUTHORIZED）
```

## 1. 實際中止位置

```
00:05:00  Task 觸發（主機開機、Water 登入 —— HOST_POWERED_ON_REQUIRED 達成）
00:05:03  PREFLIGHT fresh start
          Fresh Safety Preflight  PASS（11 / 11）
          GATE A                  PASS（CHARGE_POLICY_ELIGIBLE_HANDOFF_REQUIRED）
          Handoff readiness       PASS（4 / 4）
00:05:46  1_PRE_PAUSE_PREFLIGHT   PASS 11 / 11
00:05:46  2_PAUSE                 FAIL  ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND
------------------------------------------------------------------
pause  NOT_SENT（實際 SSH pause verb = 0）
restore 0 ／authority acquire 0 ／CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0
ssh_verb_counts = {probe 4, loopcheck 6} —— 全部唯讀
final_ownership = EXTERNAL     ONE_SHOT = CONSUMED
```

## 2. ROOT CAUSE

`OnceGuard(journal)` 在建構時把**整個 journal 檔案**的歷史還原成 `attempted`；
而 provider 先前使用**單一共用** `handoff_journal.jsonl`。
該檔案留有 **Attempt #4（2026-09-24）** 的紀錄：

```
2026-09-24 00:05:47  INTENT   step=PAUSE
2026-09-24 00:05:48  OUTCOME  step=PAUSE   send_pause -> SENT
2026-09-24 00:08:46  INTENT   step=RESTORE
2026-09-24 00:08:46  OUTCOME  step=RESTORE send_restore -> SENT
-> attempted = {PAUSE, RESTORE} -> may_attempt("PAUSE") = False
```
本意是「同一次交接內 pause 只能一次」，卻因跨 Attempt 共用而變成
**「這台機器永遠不能再 pause」**。自 Attempt #4 起，任何後續 attempt
只要走到 step 2 必然被擋。

🔴 這是**保守方向**的失敗（拒送而非誤送），安全屬性未失守。

## 3. FORMAL JOURNAL SEMANTICS（正式裁示）

```
Handoff Journal = ATTEMPT-SCOPED（不是 GLOBAL LIFETIME-SCOPED）

OnceGuard one-shot 語意：
    同一 Attempt / 同一 handoff session：PAUSE 最多一次、RESTORE 最多一次
    **不得**解讀成「整個 Phase 6 歷史期間 PAUSE 永遠只能一次」
```

## 4. OLD / NEW journal path

```
OLD  output/phase6_scheduled_first_live/handoff_journal.jsonl   （所有 Attempt 共用）
NEW  output/phase6_scheduled_first_live/handoff_journal_<attempt_id>.jsonl

例：handoff_journal_FIRST-LIVE-ATTEMPT7-20261001.jsonl
```
由 `HO.attempt_journal_path(base_dir, attempt_id)` **決定性**導出：
同一 attempt_id -> 同一路徑（crash recovery 必須能在新 process 重新打開同一檔）。
`attempt_id` 缺失／空白 -> `ValueError`（Fail Closed），
🔴 **不得** fallback 回 legacy 共用 journal。

additive 注入點 `ctx["journal_dir"]`：只換 base dir，**導出邏輯不變**，
讓 offline smoke 驗到的就是 production 行為。

## 5. attempt_id JOURNAL BINDING（defense-in-depth）

`Journal(path, attempt_id=...)` 把 `attempt_id` 寫進**每一筆**記錄
（STATE / INTENT / OUTCOME / PLAN_STEP 皆然）。
主要隔離仍是 per-attempt 檔案；attempt_id filtering 是第二層。

## 6. ONCEGUARD RESTORE SCOPE

```
journal 有 attempt_id 時：只採用同一 attempt_id 的紀錄
                          他人 attempt_id 的紀錄 -> 略過（skipped_foreign 計數）
紀錄缺 attempt_id        -> 仍計入（Fail Closed，不放行）
新 Attempt               -> attempted = empty
同一 Attempt crash 後重啟 -> 已有 INTENT PAUSE 則 attempted 含 PAUSE
```

## 7. SAME-ATTEMPT CRASH RECOVERY（J2 ~ J6）

```
J2 同一 attempt_id -> 同一 journal path；重啟後 may_attempt(PAUSE)=False（NO RESEND）
J3 pause OUTCOME UNKNOWN -> 重啟不得重送
J4 只有 INTENT（crash before outcome）-> 不得再送；needs_verification=True
J5 RESTORE INTENT 後 crash -> 不得再送 RESTORE；needs_verification=True
J6 RESTORE 已有 OUTCOME -> confirmed，不需再驗證
```

## 8. CROSS-ATTEMPT ISOLATION（X1 ~ X8）

```
X1 A 完成 pause/restore -> B 全新乾淨
X2 A 的 PAUSE UNKNOWN 不污染 B；A 自身仍保留 one-shot 限制
X3 A consumed -> B 獨立
X4 不同 attempt_id -> 不同 path      X5 同一 attempt_id -> 同一 path
X6 同檔內缺 attempt_id 的紀錄仍計入（Fail Closed）
X7 attempt_id 缺失/空白 -> ValueError，不得 fallback 共用 journal
X8 同檔內**他人 attempt_id** 的紀錄被略過
```

## 9. ATTEMPT #7 EXACT REGRESSION（用 Attempt #4 真實 journal 歷史）

```
A7R0  legacy 共用 journal 仍存在（immutable），且確含 Attempt #4 PAUSE/RESTORE
A7R1  直接用 legacy 共用 journal -> may_attempt("PAUSE") = False（重現根因）
A7R2  Attempt #7 等價 runtime（per-attempt journal）-> may_attempt = True
A7R3  PAUSE mock send = exactly once
A7R4  不再出現 ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND
A7R5  legacy journal 內容未被測試改動
```

## 10. LEGACY SHARED JOURNAL（保留 immutable）

```
output/phase6_scheduled_first_live/handoff_journal.jsonl
    LEGACY SHARED JOURNAL — CONTAINS ATTEMPT #4 EVIDENCE
    29 筆，4726 bytes，sha256 92d2987301a24583…
    另存快照 handoff_journal.LEGACY_SHARED.snapshot.jsonl（位元組一致）
```
🔴 **禁止**刪除／truncate／rename-overwrite／改寫。
**不得**為了讓新 Attempt 能跑而清空舊 journal。

## 11. pause step vs actual send 計數（欄位語意修正）

```
OLD  只有 pause_attempt_count，易被讀成「真的送出過一次 pause」
NEW  pause_step_attempt_count   2_PAUSE 步驟是否執行過
     pause_send_attempt_count   **實際送出**（含 UNKNOWN＝可能已送出）的次數
     pause_attempt_count        保留不變（backward compatible）

Attempt #7 形狀：step=1 ／ send=0 ／ outcome=NOT_SENT
真的送出：      step=1 ／ send=1
UNKNOWN：       step=1 ／ send=1（可能已送出，責任成立）
```

## 12. SHARED STATE INVENTORY

| 項目 | 分類 |
|---|---|
| handoff journal | **ATTEMPT_SCOPED**（本輪修正） |
| one-shot ledger | ATTEMPT_SCOPED |
| artifact dir | ATTEMPT_SCOPED |
| dispatch permit | ATTEMPT_SCOPED |
| scheduled live-leg authorization | ATTEMPT_SCOPED |
| CommandCountGuard | ATTEMPT_SCOPED |
| recovery state | ATTEMPT_SCOPED |
| `_live_leg_guard` | **PROCESS_SCOPED** |
| `HO.DISPATCH_ENABLED` / `RT.DISPATCH_ENABLED` | PROCESS_SCOPED |
| LastControl store | **SYSTEM_PERSISTENT** |
| Decision Audit | SYSTEM_PERSISTENT |
| legacy shared journal | **INTENTIONALLY_SHARED**（immutable 證據） |

### LastControl 維持 SYSTEM_PERSISTENT —— 理由與證明

其語意本來就是「跨 process / 跨 attempt 保存最後**已驗證**的控制結果」，
因此 SYSTEM_PERSISTENT 是合理的。已證明 stale LastControl **不能**讓新 Attempt
跳過任何閘門：

```
INV3 LastControl 不參與 live_leg_gates 的放行判定
INV4 人工/結構確認後一律重新取得 fresh 觀測（確認前快照不採用）
INV5 readback 由 ReadBackVerifier 獨立執行，不讀 LastControl
INV6 LastControl 只在 VERIFY_SUCCESS 之後才寫入
```

### `_live_leg_guard` 維持 PROCESS_SCOPED —— 理由與證明

```
INV1 為 process 內 dict，無 save / persist / to_file API
INV2 實測新 process 自然重置為 used=False
```
跨 process 的重送防護由 **Attempt ledger ＋ CommandCountGuard ＋
one-shot authorization** 負責，因此**不需要**為了本次 journal bug 把它持久化。

## 13. TEST ENVIRONMENT REALISM（新增原則）

🔴 涉及 durable production state 的測試，**不得**只用「永遠空白的 tmpdir」
就宣稱 production-ready。必須至少有一條
**PREPOPULATED STATE TEST** —— 以上一個 Attempt 已有的 durable evidence 啟動
下一個 Attempt，確認哪些該隔離、哪些該共享。
本輪的 `test_phase6_journal_scope.py` 即以 **Attempt #4 的真實 journal** 做前置狀態。

## 14. CONSTRUCTION SMOKE UPDATE

```
PC6d  journal_path_is_attempt_scoped（路徑含 attempt_id）
PC6e  journal path != legacy 共用 journal
PC6f  journal 綁定 attempt_id
0 field I/O 維持不變
```

## 15. REGRESSION

```
test_phase6_journal_scope.py            66 / 66
test_phase6_provider_construction.py    60 / 60（含 FIRST LIVE full mock）
Full Phase 6   6797 / 6797 PASS，63 files，0 FAIL   （baseline 6728 / 62）
```
未刪 test、未放寬 assertion、未清空 legacy journal、
未把 previous-attempt records 當成 current attempt。

## 16. ATTEMPT HISTORY

| Attempt | 正式結果 |
|---|---|
| #1 | 未完成 real 5 kW live leg |
| #2 | GATE A 前中止 —— `PROVIDER_WIRING_DEFECT` |
| #3 | `ABORTED_PRE_PAUSE_PREFLIGHT` |
| #4 | `ABORTED_AT_7_FIRST_LIVE` |
| #5 | `MISSED / NOT EXECUTED` —— HOST POWERED OFF |
| #6 | `EXCEPTION_FAIL_CLOSED` —— DISPATCHER FACTORY SIGNATURE DEFECT |
| #7 | `PAUSE_FAILED` —— CROSS-ATTEMPT JOURNAL STATE LEAK |

```
FIRST LIVE successful real 5 kW leg = 0 次
```

## 17. FINAL SAFETY

```
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
FIRST LIVE HOLD   Unattended LIVE HOLD
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
八個 Phase6 Task 全部 Disabled
Attempt #8 config / Task / ledger 皆未建立
```


---

# OFFLINE vs PRODUCTION COVERAGE GAP — 正式分類（2026-10-01）

```
ATTEMPT #8 = READY FOR NEW FIELD AUTHORIZATION（≠ AUTHORIZED）
ATTEMPT #8 = NOT AUTHORIZED
```

## 1. 正式分類（取代先前不精確的歸納）

🔴 **不得**把 Attempt #6 與 #7 歸納成同一類 root cause ——
那種把兩者併為單一「測試隔離問題」的說法並不精確，不得寫入任何正式文件或回報。

正式表述：

> 本階段已多次發現 **offline test environment** 與
> **production runtime context** 之間存在**不同類型**的 coverage gap。

| Attempt | Coverage gap 分類 | 具體內容 |
|---|---|---|
| **#6** | **PRODUCTION CONSTRUCTION PATH COVERAGE GAP** | 測試直接 `FirstLiveDispatcher(...)` 建構，繞過 production 的 `build_first_live_dispatcher()` 工廠；工廠 signature 與 caller kwargs 不符的缺陷因此未被發現 |
| **#7** | **PERSISTENT / SHARED-STATE REALISM GAP** | 測試一律使用「永遠空白的 tmpdir」journal；production 的共用 `handoff_journal.jsonl` 帶有前次 Attempt 的 durable 紀錄，此落差因此未被發現 |

**兩者不是同一類 root cause**：
前者是「**路徑**沒走到 production 的建構鏈」；
後者是「**狀態**沒重現 production 的 durable 內容」。

對應的補強也不同：

```
#6 -> REAL PROVIDER CONSTRUCTION TEST
      （必須由 provider production code 自己呼叫工廠）
#7 -> PREPOPULATED STATE TEST
      （必須以上一個 Attempt 的 durable evidence 作為前置狀態）
```

## 2. ATTEMPT #7 CLOSURE（正式）

```
ATTEMPT #7           = COMPLETED / CONSUMED / CLOSED
ATTEMPT #7 RESULT    = PAUSE_FAILED
ROOT CAUSE           = CROSS-ATTEMPT JOURNAL STATE LEAK
SAFETY               = SAFE FAIL-CLOSED

Fresh Safety         PASS          GATE A              PASS
Handoff readiness    PASS          PRE_PAUSE_PREFLIGHT PASS 11 / 11
2_PAUSE              ENTERED       pause_send_outcome  NOT_SENT
實際 SSH pause verb   0
restore 0 ／authority acquire 0 ／CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0
ONE_SHOT             CONSUMED
```

## 3. 已確立的正式結論（本輪複驗）

```
SAME-ATTEMPT CRASH RECOVERY   = PASS
CROSS-ATTEMPT ISOLATION       = PASS
LEGACY JOURNAL                = PRESERVED
                                29 records ／4726 bytes ／
                                sha256 92d2987301a24583… ／bit-identical
FIRST LIVE FULL MOCK          = PASS（DISPATCH_SUCCEEDED，CHARGE 5.0 kW ×1，
                                DISCHARGE 0，PCS STOP <= 1，violations 0）
test_phase6_journal_scope.py          66 / 66 PASS
test_phase6_provider_construction.py  60 / 60 PASS
Full Phase 6                  6797 / 6797 PASS，63 files，0 FAIL
```

## 4. ATTEMPT #8 現況

```
Attempt #8 config           = NOT CREATED
Attempt #8 Task             = NOT CREATED
Attempt #8 ledger           = NOT CREATED
Attempt #8 handoff journal  = NOT CREATED
Attempt #8 artifact         = NOT CREATED
八個既有 Phase6 Task = 全部 Disabled
FIRST LIVE         = HOLD
Unattended LIVE    = HOLD
```
不得自行建立 Attempt #8／Task，不得 pause／restore／CHARGE／DISCHARGE／
PCS STOP／authority acquire／arm transport。
**等待使用者新的明確 field authorization。**

## 5. NEXT AUTHORIZED ATTEMPT —— 正式九步（已含 per-attempt journal）

```
1. Fresh Full Phase 6 regression
2. Validate-only rehearsal
3. Production construction smoke
4. Fresh host verify
5. Fresh external baseline
6. Fresh legacy Task scan
7. 建立全新：Attempt ID ／config ／**per-attempt handoff journal** ／
            ledger path ／artifact path ／wrapper ／Task XML
8. 建立 one-shot field Task
9. STOP AND REPORT
```
🔴 不得手動 `/Run`、不得自動 retry、
**不得沿用 Attempt #7 的 journal／ledger／artifact**。

## 6. FINAL SAFETY

```
HO.DISPATCH_ENABLED False   RT.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
FIRST LIVE HOLD   Unattended LIVE HOLD
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
FIRST LIVE successful real 5 kW leg = 0 次
```


## 7. 用語定義 —— COMPLETED 不等於成功

```
ATTEMPT #7 = COMPLETED / CONSUMED / CLOSED
```

| 詞 | 精確語意 |
|---|---|
| **COMPLETED** | 該次**排程 Attempt 已跑完並結案**（不再有待執行的步驟） |
| **CONSUMED** | one-shot 授權**已用盡**，同一 Attempt ID 不得再次執行 |
| **CLOSED** | postmortem 已完成、root cause 已定案、證據已保存 |

🔴 這三個詞**都不代表 FIRST LIVE 目標達成**。
Attempt #7 的 RESULT 是 `PAUSE_FAILED`：
實際 SSH pause verb 0、authority acquire 0、CHARGE 0。

```
FIRST LIVE successful real 5 kW leg = 0 次（Attempt #1 ～ #7 全部未達成）
```

🔴 歷史 Attempt **不得**被重新判定為成功。


---

# FIRST LIVE ATTEMPT #8 — FIELD TASK CREATION（2026-10-01）

```
FIELD TASK CREATION   = PASS
FIRST LIVE ATTEMPT #8 = AUTHORIZED / WAITING FOR 2026-10-02 00:05 SCHEDULED TRIGGER
Unattended LIVE       = HOLD
```

🔴 **本輪只建立 Task，未手動 `/Run`，未執行任何 field control。**

授權依據：使用者 2026-10-01「我同意跑 Attempt #8」，並經確認排程參數為
**2026-10-02，Trigger 00:05，Window 00:05 ～ 00:30，Asia/Taipei**。
🔴 target_date / window 屬 production parameter，**由使用者指定，未自行發明**。

## PRE-FIELD 六步（全部 fresh 執行，全部 PASS）

```
1. Full Phase 6 Regression      6797 / 6797 PASS，63 files，0 FAIL
2. Validate-only rehearsal      Phase6_FirstLive_ValidateOnly_G（已刪除）
                                LastTaskResult 0 ／CONFIG_OK ／
                                provider_validation 7-7 ／
                                VALIDATE_ONLY_NOT_EXECUTED ／
                                field_side_effects 0 ／
                                ledger NOT CREATED ／journal NOT CREATED
3. Production construction smoke  60 / 60 PASS，exit code 0
                                FM3 CHARGE 5.0 kW 恰好一次
                                FM8 live leg 期間 DISPATCH_ENABLED = True
                                FM9 結束後 dispatch flags reset
                                rest state HO=False RT=False
4. Fresh host verify            powered on（LastBootUpTime 2026-09-29 08:56:38）
                                water / console / Active（登入 2026-09-29 08:57）
                                AC STANDBYIDLE   0x00000000 = Never
                                AC HIBERNATEIDLE 0x00000000 = Never
                                （AC VIDEOIDLE 0x0000012c = 300 s 僅關閉顯示器，非睡眠）
                                RebootPending(CBS) False／RebootRequired(WU) False
                                Schedule service Running
5. Fresh external baseline      BASELINE_READY（10 / 10）
                                pid 57483 ／cwd /home/etica/ems ／
                                cmd python3 auto_control.py ／
                                screen_count 1 ／guard_variant B2 ／
                                capability_ok True
                                loop marks 10:07:49 -> 10:07:57 -> 10:08:05（遞增）
                                canonical external_liveness = EXTERNAL_RUNNING
                                SSH verbs = {probe 1, loopcheck 3}（唯讀）
6. Fresh legacy Task scan       8 個舊 Task 全部 Disabled，NextRun 全 N/A
                                全系統掃描：唯一未停用且指向本專案者 = Attempt #8
```

## ATTEMPT #8 CONFIG

`test/scheduled_first_live_attempt8_20261002.json`
```
enabled              true
attempt_id           FIRST-LIVE-ATTEMPT8-20261002
target_date          2026-10-02
window_start/end     00:05 / 00:30
timezone             Asia/Taipei
dependency_provider  phase6_first_live_field -> phase6_first_live_field_provider
ledger_path          output/phase6_scheduled_first_live/FIRST-LIVE-ATTEMPT8-20261002.jsonl
artifact_dir         output/phase6_scheduled_first_live/attempt8_20261002
```
`load_config = CONFIG_OK`（problems = []）。`window_state` 推演：

```
2026-10-01 23:59  BEFORE_WINDOW (360 s)
2026-10-02 00:04  BEFORE_WINDOW (60 s)
2026-10-02 00:05  INSIDE_WINDOW
2026-10-02 00:29  INSIDE_WINDOW
2026-10-02 00:31  WINDOW_EXPIRED
2026-10-03 00:10  WRONG_DATE
```

## ATTEMPT-SCOPED HANDOFF JOURNAL（Attempt #7 修正後首次實戰）

```
derived path = output/phase6_scheduled_first_live/
               handoff_journal_FIRST-LIVE-ATTEMPT8-20261002.jsonl
由 attempt_id 決定性導出（deterministic），建立 Task 當下 = NOT CREATED
LEGACY SHARED JOURNAL = handoff_journal.jsonl
                        29 records ／4726 bytes ／sha256 92d2987301a24583… ／
                        與 snapshot bit-identical，immutable，不再寫入
```
🔴 Attempt #8 的 `OnceGuard` **不會**看到 Attempt #4 / #7 的 PAUSE INTENT。

## ATTEMPT #8 TASK

`Phase6_Scheduled_FirstLive_Attempt8_20261002`（顯式 XML 註冊，UTF-16）
```
Trigger              2026-10-02T00:05:00      NextRunTime = 2026/10/2 00:05
LastRunTime          11/30/1999（哨兵值）
LastTaskResult       267011 = 0x00041303 SCHED_S_TASK_HAS_NOT_RUN
                     -> 從未執行，未手動 /Run
Schedule Type        One Time Only
Repetition           無（Repetition Interval / Duration 皆空）
RestartCount         0（XML 無 RestartOnFailure -> 無 auto retry）
StartWhenAvailable   False        WakeToRun  False
MultipleInstances    IgnoreNew    ExecutionTimeLimit PT1H
RunOnlyIfIdle False  DisallowStartIfOnBatteries / StopIfGoingOnBatteries False
Principal            Water ／ InteractiveToken ／ LeastPrivilege
Action               run_first_live_attempt8_20261002.cmd（絕對路徑）
State                Ready / Enabled
```
XML 內 `Repetition` ／`RestartOnFailure` ／`CalendarTrigger` ／`LogonTrigger` ／
`BootTrigger` 出現次數 = **0**。

## HOST POWER REQUIREMENT

```
HOST_POWERED_ON_REQUIRED = True
```
🔴 Attempt #5 已證明「建立 Task 時 host verify PASS」**不保證** trigger 時仍開機。
自建立 Task 起至 **2026-10-02 00:05**：

```
主機         必須保持 POWERED ON
Water 帳號   必須保持 LOGGED IN
允許         Win + L
禁止         Shutdown ／Restart ／Log out ／Switch user ／
             Manual Sleep ／Hibernate
```
**不得**改成 `StartWhenAvailable=True` 或 `WakeToRun=True`。

## EVIDENCE PRESERVED

```
config     test/scheduled_first_live_attempt8_20261002.json
wrapper    test/run_first_live_attempt8_20261002.cmd
Task XML   test/Phase6_Scheduled_FirstLive_Attempt8_20261002.xml
rehearsal  test/scheduled_first_live_rehearsal_20261001g.json
           test/rehearsal_20261001g.cmd
           output/phase6_scheduled_first_live/rehearsal_20261001g_task.log
baseline   output/phase6_field_validation/attempt8_baseline_20261001_100807.json
```

## FINAL SAFETY STATE

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0 ／authority acquire 0
本輪 SSH 動詞 = probe 1 + loopcheck 3（唯讀）
dispatch rest state  HO False ／RUNTIME False
RemoteSenders armed False   SshTransport armed False
Attempt #8 ledger ／handoff journal ／artifact dir ／launcher log 皆尚未建立
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 3 / 3
FIRST LIVE successful real 5 kW leg = 0 次
九個 Phase6 Task 中八個 Disabled，僅 Attempt #8 為 Ready
Unattended LIVE HOLD
```


---

# FIRST LIVE ATTEMPT #8 — POSTMORTEM（2026-10-02）

```
ATTEMPT #8 = COMPLETED / CONSUMED / CLOSED
RESULT     = ABORTED_FIRST_LIVE
ROOT CAUSE = ENTRY-POINT / CONTROL-FLOW ORDERING GAP
SAFETY     = SAFE FAIL-CLOSED
```

## 1. 本次實際

```
PRE_PAUSE            PASS 11 / 11      PAUSE              SENT
PauseVerifier        PASS              external quiescent PASS（4 筆）
ownership            NEITHER           POST_PAUSE / GATE B PASS
7_FIRST_LIVE         FAIL              authority acquire  0
CHARGE 0   DISCHARGE 0   PCS STOP 0
RESTORE              SENT              RestoreVerifier    RV_RESUMED
```

🔴 交接前段**首次全部走通** —— Attempt #7 的 attempt-scoped journal 修正確實生效。
per-attempt journal 21 筆、非本 attempt 紀錄 **0**；LEGACY journal 未被觸碰。

外部控制器暫停 00:05:46 ～ 00:08:52（約 3 分鐘）後復原；
隔日 08:56 獨立 fresh read-only probe = `EXTERNAL_RUNNING`。

```
External B2 cumulative   pause / restore = 4 / 4
FIRST LIVE successful real 5 kW leg = 0 次
```

## 2. ROOT CAUSE — FORMAL

```
HandoffRunner step 7
  在呼叫 FirstLiveDispatcher.dispatch() 之前
  先要求 _permit_authorizes(dispatch_permit) == True
       ↓
完整 FirstLiveDispatchPermit 要求
  authority_acquire_attempted ／ authority_acquired
       ↓
這兩項 evidence 只有在 dispatch() 內執行 lease.acquire() 之後才可能成立
       ↓
CONTROL-FLOW CYCLE —— production path 結構性無法前進
```

Attempt #8 artifact 證據：

```
dispatch_permit.state = PERMIT_NOT_ENTERED
denied_reason         = ["authority_acquire_attempted", "authority_acquired"]
其餘 15 項 checks       = 全 true
handoff_abort_step    = 7_FIRST_LIVE
authority_acquire_attempted = false
```

🔴 這**不是**現場條件問題、**不是** journal 問題、
也**不是**「DISPATCH_ENABLED 應提前打開」。

## 3. 正式修正 — TWO-STAGE AUTHORIZATION

### STAGE A — DISPATCHER ENTRY GATE

```
FIRST_LIVE_ENTRY_PRECONDITIONS = 14 項
  = FIRST_LIVE_PERMIT_PRECONDITIONS（16）
  - authority_acquire_attempted
  - authority_acquired
permit.evaluate_entry() / permit.allows_dispatcher_entry()
HandoffRunner 以 duck typing 呼叫（避免循環 import）
```

Entry Gate **只能**使用進入 dispatcher 前確實已成立的 evidence：
attempt / one-shot context、scheduled authorization、PRE_PAUSE、pause verified、
ownership NEITHER、GATE B、external relinquished、safety / comm / fault / alarm、
permit 尚未 consumed。

🔴 **不得**把 `authority_acquire_attempted` / `authority_acquired`
當作 entry prerequisite。

### STAGE B — FULL DISPATCH PERMIT（未降低 safety）

```
FIRST_LIVE_PERMIT_PRECONDITIONS 維持 16 項，含 authority 兩項。
在 lease.acquire() 之後、進入 first_live_dispatch_scope 之前重新評估。
```

### 正式順序

```
HandoffRunner Step 7
  → REST INVARIANT：HO.DISPATCH_ENABLED 必須為 False
  → Dispatcher Entry Gate PASS
  → FirstLiveDispatcher.dispatch()
  → authority_acquire_attempted = True
  → lease.acquire()
  → authority_acquired = True
  → FULL FirstLiveDispatchPermit 重新評估
  → FULL PERMIT PASS
  → first_live_dispatch_scope
  → HO.DISPATCH_ENABLED = True
  → live leg
  → finally：HO.DISPATCH_ENABLED = False
  → authority release
  → external restore
```

## 4. DISPATCH_ENABLED 在 STEP 7 的正式語意

```
進入 Dispatcher 前：
  HO.DISPATCH_ENABLED = False
  RT.DISPATCH_ENABLED = False
→ 這是 EXPECTED REST STATE，**不是** failure condition
```

🔴 舊寫法 `if not DISPATCH_ENABLED: reject` 已移除。
🔴 新寫法：step 7 前若 `DISPATCH_ENABLED` 已是 True ->
**Fail Closed**（代表有人在 scope 外打開過）。
🔴 `RT.DISPATCH_ENABLED` 全程保持 False。

## 5. 失敗路徑（皆為 0 command）

| 情境 | 行為 |
|---|---|
| **AUTHORITY FAILURE** | 不進 scope；`DISPATCH_ENABLED` 維持 False；CHARGE / DISCHARGE / PCS STOP = 0；仍走 restore |
| **FULL PERMIT FAILURE AFTER AUTHORITY** | 不進 scope；0 CHARGE / 0 DISCHARGE；**release authority**；依正式 cleanup / restore path 安全歸還 |

🔴 `FirstLivePermitDenied` 另外被分類為 `DISPATCH_NOT_ATTEMPTED`
（**確定沒送出**），不再誤判為 `DISPATCH_UNKNOWN`（可能已送出）。
依據：該例外只會在 scope 的 `yield` **之前**拋出，
此時 `guard.take()` 與 `_run_leg()` 都尚未執行。
其餘例外一律維持 UNKNOWN（Fail Closed）。

## 6. MANDATORY PRODUCTION ENTRY-POINT TEST

新增 `test/test_phase6_entry_point_ordering.py` —— **92 / 92 PASS**，
正式列為 mandatory。

```
唯一進入點 = deps.arm_and_run_handoff()
禁止       = 直接 disp()
禁止       = 直接 new FirstLiveDispatcher(...)
禁止       = dict((k, True) for k in FIRST_LIVE_PERMIT_PRECONDITIONS)
             作為 control-flow correctness 的主要證據
```

authority evidence **必須**由真實的 `lease.acquire()` 在正確 control-flow
時點產生；step 7 不得自行手填 `authority_acquired=True`。

| 群組 | 內容 |
|---|---|
| `EP1 ~ EP18` | 完整成功路徑：step 1→12、CHARGE 5.0 kW ×1、readback PASS、restore RESUMED、rest state 復原 |
| `A8R1 ~ A8R14` | Attempt #8 exact regression（以 **step 7 當下**取樣） |
| `O1` | Entry Gate PASS 但 authority acquire FAIL -> 0 command、dispatch False |
| `O2` | authority PASS 但 Full Permit FAIL -> 0 command、authority released、`DISPATCH_NOT_ATTEMPTED` |
| `O3` | scheduled authorization invalid -> dispatcher 不得進入、authority acquire 0 |
| `O4` | GATE B FAIL -> dispatcher 不得進入、authority acquire 0 |
| `O5` | ownership BOTH -> dispatcher 不得進入 |
| `O6` | step 7 前 `DISPATCH_ENABLED` 已是 True -> Fail Closed |
| `O7` | Full Permit 不得在 authority acquire 前被偽造為 PASS |
| `O8` | canonical ordering 不得改序 |
| `NB1 ~ NB5` | 禁用全 True 合成 evidence／禁止繞過進入點（**AST 判定**，不用文字掃描） |

### canonical ordering（O8）

```
ENTRY_GATE_PASS
  < AUTHORITY_ACQUIRE_ATTEMPT
  < AUTHORITY_ACQUIRED
  < FULL_PERMIT_PASS
  < DISPATCH_SCOPE_ENTER
  < CHARGE_SEND
```

`ordering_is_canonical()` 少一個／多一個／順序不同一律 False；
artifact 另帶 `dispatch_ordering_events` / `dispatch_ordering_canonical`。

### 測試環境真實性（本輪新教訓）

`SVC._live_leg_guard` 是 **PROCESS_SCOPED** 一次性狀態。
同一 process 內跑多次完整 live leg 時必須逐次重置，
否則第二次之後測到的是「測試自己的殘留狀態」而非 production 行為。

## 7. CONSTRUCTION SMOKE vs ENTRY-POINT SMOKE

```
construction smoke  = production dependency construction ＋ wiring
entry-point smoke   = production control-flow ordering
```

🔴 **construction smoke PASS 不等於 entry-point ordering PASS。**
兩者責任不同，**不得混稱**。
`test_phase6_provider_construction.py`（60 / 60）維持不變。

## 8. PRE-FIELD SEVEN-STEP RULE（取代六步）

```
1. Full Phase 6 Regression
2. Validate-only rehearsal
3. Production construction smoke            建構 / wiring
4. Production entry-point / control-flow smoke   真正的呼叫順序
5. Fresh host verify
6. Fresh external baseline
7. Fresh legacy Task scan
```

## 9. EXIT CODE 分類修正

```
R_ABORTED_FIRST_LIVE / R_ABORTED_PRE_PAUSE / R_ABORTED_HANDOFF_STEP
  -> 正式納入 RESULT_STATES（19 -> 22 項）
  -> classify_exit() = EXIT_HANDLED(0) / HANDLED_TERMINAL_RESULT
```

🔴 **不改變** Attempt RESULT 本身：Attempt #8 仍是 `ABORTED_FIRST_LIVE`。
🔴 exit 0 的語意是 **handled**，不是 PASS；PASS 只有 `R_PASS` 一個。
🔴 未知 result 仍落到 exit 5 `UNEXPECTED_LAUNCHER_FAILURE`（Fail Closed）。

## 10. ARTIFACT LIVENESS 修正

```
restore ＋ RestoreVerifier 完成後
  -> 重新取得 canonical external_liveness
  -> 填入 post_restore_external_liveness
  -> external_liveness_final 指向**最後一次真正的 fresh observation**
無法判定 -> EXTERNAL_UNKNOWN（Fail Closed）
restore 未送出 -> 不做任何額外 field I/O
```

🔴 **不得**保留 restore 之前的 `EXTERNAL_NOT_RUNNING` 卻稱為 final。

## 11. 歷史 ARTIFACT 保存（不得修改）

Attempt #8 已生成的 artifact 是 **immutable historical evidence**，
本輪修正**只作用於未來 Attempt 與 regression test**。

```
Attempt #8 historical artifact 的 external_liveness_final
  = PRE-RESTORE VALUE MISLABELED AS FINAL
  （EXTERNAL_NOT_RUNNING @ 2026-10-02 00:08:30）

但現場實際：
  RestoreVerifier          = RV_RESUMED @ 00:08:52
  後續 fresh field probe    = EXTERNAL_RUNNING @ 2026-10-02 08:56
```

### LEDGER EMBEDDED SNAPSHOT

```
artifact 內嵌 ledger snapshot   consumed=false ／record_count=10
ledger 檔案最終                 consumed=true  ／record_count=11
```

**不是 execution defect** —— snapshot 產生於最後一筆 CONSUMED 寫入之前。
schema 新增自我說明欄位 `ledger_snapshot_is_pre_consume`，
**不改寫** Attempt #8 的歷史資料。

## 12. COVERAGE GAP CLASSIFICATION（正式三類）

| Attempt | 分類 | 具體內容 |
|---|---|---|
| **#6** | **PRODUCTION CONSTRUCTION PATH COVERAGE GAP** | 測試繞過 production 工廠 `build_first_live_dispatcher()` 直接建構 |
| **#7** | **PERSISTENT / SHARED-STATE REALISM GAP** | 測試使用永遠空白的 tmpdir journal，未重現 production 的 durable 內容 |
| **#8** | **ENTRY-POINT / CONTROL-FLOW ORDERING GAP** | 測試直接 `disp()` 進入，未經 `HandoffRunner.run()` step 7；permit 斷言使用全 True 合成 evidence |

**三者不是同一 root cause。**
正式總結：本階段多次發現 offline test environment 與 production runtime
context 之間存在**不同種類**的 coverage gap。

## 13. ATTEMPT #9 READINESS（13 項）

```
 1. Step 7 two-stage authorization implemented   DONE
 2. Full Permit authority requirements retained  DONE
 3. Production Entry-Point ordering test PASS    DONE
 4. Attempt #8 exact regression PASS             DONE
 5. authority failure negative tests PASS        DONE（O1）
 6. full permit failure after authority PASS     DONE（O2）
 7. dispatch scope ordering PASS                 DONE（O8）
 8. exit code classification fixed               DONE
 9. post-restore final liveness fixed            DONE
10. construction smoke PASS                      DONE
11. production entry-point smoke PASS            DONE
12. FIRST LIVE full mock PASS                    DONE
13. Full Phase 6 regression PASS                 DONE
```

```
ATTEMPT #9 = READY FOR NEW FIELD AUTHORIZATION
ATTEMPT #9 = NOT AUTHORIZED
READY != AUTHORIZED
```

## 14. FINAL STATUS

```
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 4 / 4
FIRST LIVE successful real 5 kW leg = 0 次

HO.DISPATCH_ENABLED False   pcs_auto_control_runtime.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
External Controller = EXTERNAL_RUNNING

Attempt #9 config / Task / ledger / handoff journal / artifact = NOT CREATED
FIRST LIVE = HOLD      Unattended LIVE = HOLD
```


---

# FIRST LIVE ATTEMPT #8 — TASK CLOSURE（2026-10-02）

```
Phase6_Scheduled_FirstLive_Attempt8_20261002
  BEFORE   State = Ready      NextRun = N/A
  AFTER    State = Disabled   NextRun = N/A
  LastRunTime    = 2026-10-02 00:05:00
  LastTaskResult = 5（修正前的 exit classification；RESULT 本身仍為
                     ABORTED_FIRST_LIVE —— 歷史紀錄不得改寫）
```

🔴 只做 **Disable**，**未** Unregister / Delete。

Disable 的理由（皆非「阻止既有自動重跑」—— 本來就不會自動重跑）：

```
1. 與 Attempt #2 ～ #7 結案後狀態一致
2. 防止未來人工誤觸 /Run
3. 讓 Task Scheduler 狀態明確反映 Attempt #8 已 CLOSED
4. 避免 Ready 被誤讀成「仍待執行」
```

## AFTER DISABLE — 唯讀複驗

```
Phase6 / FirstLive / ValidateOnly Tasks     9 個
  全部 Disabled                              9 / 9
  非 Disabled                                0
  NextRunTime 非空者                         0
全系統：未停用且會執行本專案腳本的 Task      0
```

| Task | State | NextRun |
|---|---|---|
| `Phase6_FirstChargeLive_Precheck_OneTime` | Disabled | N/A |
| `Phase6_FirstLive_Charge_OneTime` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt2_20260922` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt3_20260923` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt4_20260924` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt5_20260925` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt6_20260930` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt7_20261001` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt8_20261002` | **Disabled** | N/A |

## HISTORICAL EVIDENCE — 全部保留，mtime 未變

```
test/scheduled_first_live_attempt8_20261002.json                     3102 B
test/run_first_live_attempt8_20261002.cmd                             923 B
test/Phase6_Scheduled_FirstLive_Attempt8_20261002.xml                3556 B
test/scheduled_first_live_rehearsal_20261001g.json                    909 B
test/rehearsal_20261001g.cmd                                          760 B
output/.../FIRST-LIVE-ATTEMPT8-20261002.jsonl                        1562 B
output/.../handoff_journal_FIRST-LIVE-ATTEMPT8-20261002.jsonl        4282 B
output/.../FIRST-LIVE-ATTEMPT8-20261002.launcher.log                 1564 B
output/.../attempt8_20261002/scheduled_first_live_..._000852.json   14657 B
output/phase6_field_validation/attempt8_baseline_20261001_100807.json 1101 B

LEGACY SHARED JOURNAL  29 records ／4726 bytes ／
                       sha256 92d2987301a24583… ／與 snapshot bit-identical
```

## FINAL STATUS

```
ATTEMPT #8 = COMPLETED / CONSUMED / CLOSED
RESULT     = ABORTED_FIRST_LIVE
Task       = Disabled        NextRun = N/A

Phase6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative  pause / restore = 4 / 4
FIRST LIVE successful real 5 kW leg = 0

ATTEMPT #9 = READY FOR NEW FIELD AUTHORIZATION
ATTEMPT #9 = NOT AUTHORIZED
FIRST LIVE = HOLD      Unattended LIVE = HOLD
```


---

# EXECUTION DEADLINE / SAFE CLEANUP CONTRACT（2026-10-02 裁示）

```
ATTEMPT #9 = NOT READY
BLOCKER    = EXECUTION DEADLINE / SAFE CLEANUP CONTRACT   -> 本輪已修正
```

## 1. 修正前的事實（已查證）

```
window_start / window_end 只在 ScheduledFirstLiveRunner 啟動階段檢查
  ScheduledFirstLiveRunner._run_inner()  lines 526-618
  window_state 呼叫行 = [548, 556]
  self._handoff() 行  = [612]
  handoff 之後是否還有 window 檢查 -> False

Watchdog 存在但未接 production flow
  provider 建構 HandoffOrchestrator 時未傳 watchdog -> watchdog = None
  Watchdog.verdict() 在 production 的呼叫次數 = 0

實測：handoff 執行中把時鐘推到 window_end + 20 min
  -> handoff 照常跑完，result = PASS，未被中止
```

## 2. 正式 deadline 語意

```
window_end = NO-NEW-RISK-ACTION DEADLINE
window_end ≠ PROCESS TERMINATION DEADLINE
```

| 類別 | 動作 | 到期後 |
|---|---|---|
| **NEW RISK ACTION** | `pause` ／`authority_acquire` ／`charge` ／`discharge` ／`live_leg` | **禁止開始** |
| **SAFE CLEANUP** | `command_resolution` ／`cleanup_stop` ／`authority_release` ／`restore` ／`restore_verify` | **必須允許完成** |

🔴 cleanup / restore path **不得**被 deadline 自己阻擋。
🔴 未登錄的動作一律視為新風險動作（Fail Closed）。

## 3. 為什麼另建，而不是接上既有 Watchdog

| | `Watchdog` | `ExecutionDeadline` |
|---|---|---|
| 量的是什麼 | external **已被 pause 多久**（`max_paused_sec`，由 `mark_paused()` 起算） | **絕對截止時刻**（由 `target_date + window_end + timezone` 導出） |
| 回答什麼問題 | 逾時後**該做什麼**（`REQUIRE_RESTORE` ／`REQUIRE_PHASE6_RELEASE_FIRST`） | 這個動作**現在准不准開始** |
| 風險／收尾二分 | 無 | 有（`NEW_RISK_ACTIONS` / `SAFE_CLEANUP_ACTIONS`） |

語意不同，因此**另建**，不為了「已有 class」硬接。
`Watchdog` 維持原狀、未被取代，也仍未接入 production flow。

## 4. CANONICAL DEADLINE

`test/phase6_execution_deadline.py`

```
ExecutionDeadline(deadline_at, now_fn=None, monotonic_fn=None, label=None)
ExecutionDeadline.from_window(target_date, window_end, tzinfo=None, ...)
  -> 只接受 datetime.date / datetime.time 原型別，**不解析字串**
     （避免成為第二套解析實作）

建立時   wall clock 算出 budget_sec（只算一次）
執行中   monotonic clock 推進 -> 系統時間被調整不會讓 deadline 倒退或跳動
判定例外 一律 Fail Closed（視為已到期），但 SAFE CLEANUP 仍放行
```

🔴 **唯一**建立點 = provider 的 `_build_deadline()`。
其他 module **不得**自行解析 `target_date` / `window_end`。
launcher 以 `target_date_obj` / `window_end_obj` / `tzinfo` 把原型別帶進
runtime context，下游不再解析第二次。

## 5. 九個 production checkpoint

| # | checkpoint | 即將開始的動作 | 到期時 |
|---|---|---|---|
| 1 | `1_BEFORE_PAUSE` | `pause` | **擋** |
| 2 | `2_AFTER_PAUSE_RESPONSIBILITY` | `live_leg` | **擋** |
| 3 | `3_BEFORE_GATE_B` | `live_leg` | **擋** |
| 4 | `4_BEFORE_AUTHORITY_ACQUIRE` | `authority_acquire` | **擋** |
| 5 | `5_AFTER_AUTHORITY_ACQUIRE` | `live_leg` | **擋** |
| 6 | `6_BEFORE_FULL_PERMIT` | `live_leg` | **擋** |
| 7 | `7_BEFORE_CHARGE_SEND` | `charge` | **擋** |
| 8 | `8_LIVE_LEG_CLEANUP` | `cleanup_stop` | **放行** |
| 9 | `9_BEFORE_RESTORE_COMPLETION` | `restore` | **放行** |

1~4 在 `HandoffRunner`；5~8 在 `FirstLiveDispatcher`；9 在 `_restore_phase`。
checkpoint 7 位於 `guard.take()` **之前** —— 被擋時指令額度不會被消耗。

## 6. 四種 CASE

```
CASE A  deadline 到時 external 尚未 pause
        PAUSE 0 ／authority 0 ／CHARGE 0 ／DISCHARGE 0
        -> handled terminal result

CASE B  pause 已 SENT / UNKNOWN 或 restore responsibility = True
        不得進 live leg、不得 acquire authority、不得 CHARGE
        -> external state resolution -> restore -> RestoreVerifier
        UNKNOWN 維持既有 NO RESEND contract

CASE C  authority 已取得但 CHARGE 尚未送
        CHARGE 0 -> release authority -> restore -> RestoreVerifier

CASE D  CHARGE 已送出之後才跨 deadline
        不得直接 return / raise / hard abort
        -> readback / command resolution -> cleanup STOP
        -> release -> restore -> RestoreVerifier
        deadline 後不得再送第二個 CHARGE、不得 DISCHARGE、
        不得開始第二條 live leg
```

## 7. 新增 terminal results（皆為 HANDLED）

```
DEADLINE_BEFORE_FIELD_ACTION     exit 0  HANDLED_TERMINAL_RESULT
DEADLINE_DURING_HANDOFF          exit 0  HANDLED_TERMINAL_RESULT
DEADLINE_DURING_LIVE_CLEANUP     exit 0  HANDLED_TERMINAL_RESULT
RESULT_STATES  22 -> 25 項
```

`_classify()` 的 precedence：

```
0  pause UNKNOWN          -> R_PAUSE_UNKNOWN      （NO RESEND 契約最高優先）
1  deadline 事件           -> 三個 DEADLINE_* 之一
2  pause NOT_SENT          -> R_PAUSE_FAILED
3  handoff_abort_step      -> 既有 STEP_RESULT_MAP
```

🔴 `DEADLINE_DURING_LIVE_CLEANUP` 只表示「跨過 deadline」，
**不表示 CHARGE 未發生**。CHARGE 是否發生一律以 artifact 的
`phase6_command_counts.counts.phase6_charge` 與 `leg_reached_complete` 為準。

## 8. Task Scheduler `PT1H` 的正式定位

```
ExecutionTimeLimit = PT1H  = LAST-RESORT OS PROCESS LIMIT

PT1H ≠ normal FIRST LIVE execution budget
PT1H ≠ window_end
PT1H ≠ safe cleanup mechanism
```

🔴 正常流程**不得**依賴 `AllowHardTerminate` 完成安全收尾 ——
硬殺行程不會做 release / restore。
PT1H 保留，但它只是「行程失控時的最後一道 OS 限制」。

## 9. MANDATORY TESTS

`test/test_phase6_execution_deadline.py` —— **93 / 93 PASS**，列為 mandatory。
全部走 production entry point（`deps.arm_and_run_handoff()`）
＋ fake field I/O ＋ **injectable fake monotonic clock**，無真實等待。

| 項目 | 內容 |
|---|---|
| `D1` | 啟動時已過 deadline -> handoff 0 |
| `D2` | handoff 中、PAUSE 前跨 deadline -> pause 0 ／command 0 |
| `D3` | PAUSE SENT/VERIFIED 後跨 deadline -> CHARGE 0 ／restore 恰一次 ／RV_RESUMED |
| `D4` | authority 已取得、CHARGE 前跨 deadline -> CHARGE 0 ／release ／restore |
| `D5` | CHARGE 送出後跨 deadline -> 不 hard abort ／STOP cleanup ／release ／restore ／verify |
| `D6` | deadline 後不得第二個 CHARGE |
| `D7` | deadline 後不得 DISCHARGE |
| `D8` | restore UNKNOWN -> no resend ／responsibility 保留 |
| `D9` | deadline guard 例外 -> Fail Closed，不新增 risk action |
| `D10` | wall clock 被往回調／往前跳 -> monotonic deadline 不倒退、不跳動 |
| `D11` | 正常 FIRST LIVE 在 deadline 前完成 -> 與現行成功路徑一致（9 個 checkpoint 全放行） |
| `W1~W5` | Watchdog 語意比較（為何另建） |
| `X1~X8` | production wiring（真實 config -> 真的建得出 deadline） |

### 本輪連帶修正的兩個 evidence 缺口

```
1. artifact 的 deadline_blocked_at 原本只從 dispatcher 取 ->
   漏掉 HandoffRunner checkpoint 1~4 擋下的情況。已改為兩邊都取。

2. release_before_restore 原本只在**正常** step 9 設為 True；
   走 _bail() 的路徑恆為 None，即使 authority 確實已釋放。
   已改為進入 restore 前以**可觀測的 lease 狀態**補記
   （authority 從未被嘗試取得時維持 None，不得偽稱 True）。
```

## 10. PRE-FIELD EIGHT-STEP RULE（取代七步）

```
1. Full Phase 6 Regression
2. Validate-only rehearsal
3. Production construction smoke              建構 / wiring
4. Production entry-point / control-flow smoke 呼叫順序
5. Production execution-deadline / safe-cleanup smoke  runtime deadline / 收尾
6. Fresh host verify
7. Fresh external baseline
8. Fresh legacy Task scan
```

🔴 ③ ／④ ／⑤ 責任不同，**不得混稱**。

## 11. FINAL PRINCIPLE

```
00:05 = scheduled start
00:30 = 禁止開始新的風險動作

若 00:30 時仍有既有 safety responsibility：
    必須安全完成 STOP / release / restore / verify
    不能因 deadline 到達直接殺 process
```


---

# ATTEMPT RESULT vs LIVE LEG RESULT — REPORTING CONTRACT（2026-10-02 裁示）

## 1. 正式分工

```
attempt_result   整體排程 / deadline / handoff / safety contract 結果
live_leg_result  實際 PCS 5 kW control leg 結果
```

🔴 **兩者不得互相覆蓋。**

不能因為 live leg 成功，就掩蓋 execution deadline 已違反；
也不能因為 `attempt_result = DEADLINE_DURING_LIVE_CLEANUP`，
就把真實已成功的 5 kW leg 回頭改判成失敗。

## 2. `live_leg_result` 詞彙

| 值 | 條件（全部由既有證據導出，未新增判定規則） |
|---|---|
| `NOT_ATTEMPTED` | 完全沒送出方向指令（charge / discharge 計數皆 0） |
| `SUCCESS` | `dispatcher_outcome == DISPATCH_SUCCEEDED` **且** `leg_reached_complete is True` **且** `readback_outcome == VERIFY_SUCCESS` |
| `UNKNOWN` | 送出過但 `dispatcher_outcome == DISPATCH_UNKNOWN`（Fail Closed，不得樂觀判成 FAILED） |
| `FAILED` | 送出過且非上述情形 |

🔴 `SUCCESS` 需要**三項正面證據同時成立**，少任一項都不得稱 SUCCESS。

## 3. FIRST LIVE SUCCESS COUNT 計數規則

```
successful real 5 kW leg  +1   <=>   live_leg_result == SUCCESS
```

🔴 依**真實 live leg 是否完成**計數，**不看** `attempt_result`。
即使 `attempt_result = DEADLINE_DURING_LIVE_CLEANUP`，
也不得把這次真實成功的 leg 改回 0 次。

`collect_handoff_artifact()` 直接提供 `counts_as_first_live_success`，
不需要讀者自行推論。

## 4. 成功但跨 DEADLINE 的正式表示

```
CHARGE 5.0 kW                     exactly once
readback                          VERIFY_SUCCESS
LEG_COMPLETE                      True
cleanup STOP                      SUCCESS
authority release                 SUCCESS
RESTORE                           SUCCESS
RestoreVerifier                   RV_RESUMED
deadline_exceeded                 True
------------------------------------------------
live_leg_result                   SUCCESS
counts_as_first_live_success      True      -> successful real 5 kW leg +1
attempt_result                    DEADLINE_DURING_LIVE_CLEANUP
Attempt PASS                      False
```

正式解讀：
> **5 kW 實機控制成功，但 Scheduled Attempt 超過 execution deadline。**

## 5. DEADLINE 不可改寫歷史事實

deadline result **只能**表示「排程時間契約違反」。
它**不得**改寫：

```
command 是否送出 ／readback 是否成功 ／LEG_COMPLETE 是否達成 ／
STOP 是否完成 ／restore 是否完成
```

artifact 必須保留**真實事件序列**。

## 6. ATTEMPT RESULT PRECEDENCE TABLE

| 層級 | 內容 |
|---|---|
| **P0 SAFETY UNCERTAIN** | `PAUSE_UNKNOWN` ／`RESTORE_UNKNOWN` ／`RESTORE_FAILED` ／`DISPATCH_UNKNOWN` |
| **P1 CONTROL FAILURE** | `DISPATCH_FAILED` |
| **P2 DEADLINE** | `DEADLINE_BEFORE_FIELD_ACTION` ／`DEADLINE_DURING_HANDOFF` ／`DEADLINE_DURING_LIVE_CLEANUP` |
| **P3 PAUSE NOT SENT** | `PAUSE_FAILED` |
| **P4 REAL TERMINAL STEP** | `STEP_RESULT_MAP` |
| **P5 DERIVED BOOLEANS** | — |
| **P6 GENERIC FALLBACK** | `PASS` |

🔴 **deadline 不得掩蓋** `SEND_UNKNOWN` ／restore responsibility 未解除 ／
未完成 cleanup ／實際 control failure —— 因此 **P2 低於 P0 / P1**。

🔴 P2 置於 P0 / P1 之後**不是**把 deadline 降級：
`attempt_result` 只有一個欄位，必須先說出「最需要人處理的事」。
deadline 是否被跨越一律另由 `deadline_exceeded` ／`deadline_blocked_at` ／
`deadline_events` 保留，**不會遺失**。

## 7. DEADLINE RESULT EXAMPLES

| CASE | attempt_result | live_leg_result | CHARGE | 備註 |
|---|---|---|---|---|
| **A** deadline 在 PAUSE 前到期 | `DEADLINE_BEFORE_FIELD_ACTION` | `NOT_ATTEMPTED` | 0 | 無恢復責任 |
| **B** PAUSE 後、CHARGE 前到期 | `DEADLINE_DURING_HANDOFF` | `NOT_ATTEMPTED` | 0 | RESTORE required |
| **C** CHARGE 後跨 deadline，leg 成功 | `DEADLINE_DURING_LIVE_CLEANUP` | `SUCCESS` | 1 | `LEG_COMPLETE = True`，計數 +1 |
| **D** CHARGE 後跨 deadline，readback/cleanup 失敗 | 依 precedence（P0 / P1 優先） | **不得假裝 SUCCESS** | 1 | 依真實 command / readback outcome 記錄 |

## 8. REPORTING 欄位（artifact 必須能分辨）

```
attempt_result                      live_leg_result
leg_reached_complete                deadline_exceeded
deadline_blocked_at                 phase6_command_counts
readback_result                     cleanup_stop_result
authority_release_result            restore_result
post_restore_external_liveness      external_liveness_final
＋ counts_as_first_live_success
```

🔴 **不要只看 `attempt_result` 去推論 live leg 是否成功。**

## 9. MANDATORY TESTS

`test/test_phase6_execution_deadline.py` —— **133 / 133 PASS**（93 -> 133，+40）。

| 項目 | 內容 |
|---|---|
| `D12` | CHARGE 成功 ＋ LEG_COMPLETE ＋ 跨 deadline ＋ cleanup/restore 成功 -> `attempt_result = DEADLINE_DURING_LIVE_CLEANUP`、`live_leg_result = SUCCESS`、計數 **+1** |
| `D13` | 同場景但 readback FAIL -> **不得**記 Live Leg SUCCESS、不得計數 |
| `D14` | deadline result **不得掩蓋** `SEND_UNKNOWN`（-> `PAUSE_UNKNOWN`） |
| `D15` | deadline result **不得清除** restore responsibility（-> `RESTORE_UNKNOWN`、restore 不重送） |
| `D16` | 同一份 artifact 同時看得到 deadline violation ＋ live leg success；12 個 reporting 欄位齊備；CASE A / B 的 `live_leg_result = NOT_ATTEMPTED` 且不計數 |

## 10. ATTEMPT #8 不受本次語意影響

🔴 **不得**因新的 deadline / reporting semantics 回頭改寫 Attempt #8。

```
ATTEMPT #8 = COMPLETED / CONSUMED / CLOSED
RESULT     = ABORTED_FIRST_LIVE
ROOT CAUSE = ENTRY-POINT / CONTROL-FLOW ORDERING GAP
SAFETY     = SAFE FAIL-CLOSED
External pause / restore = SUCCESS / SUCCESS   External final = EXTERNAL_RUNNING
Phase6 real CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative pause / restore = 4 / 4
FIRST LIVE successful real 5 kW leg = 0 次
```

Attempt #8 的 `live_leg_result` 依同一規則導出為 `NOT_ATTEMPTED`
（CHARGE 計數 0）—— 與既有紀錄一致，無任何改寫。


---

# FIRST LIVE ATTEMPT #9 — PRE-FIELD EIGHT-STEP（2026-10-02）

```
ATTEMPT #9 = FIELD AUTHORIZATION INTENT CONFIRMED
ATTEMPT #9 = PRE-FIELD PASS
TARGET_DATE / WINDOW = WAITING FOR USER
Task = NOT CREATED
```

🔴 使用者已同意繼續 Attempt #9，但**未指定** `target_date` ／
`window_start` ／`window_end`。這三項屬 production parameter，
**不得**自行推定、不得沿用 Attempt #8、不得自行填 00:05–00:30、
不得依當前日期產生。因此本輪**未建立**任何正式 field Task。

## 八步結果（全部 fresh 執行）

```
1. Full Phase 6 Regression        7022 / 7022 PASS，65 files，0 FAIL
2. Validate-only rehearsal        Phase6_FirstLive_ValidateOnly_H（已刪除）
                                  LastTaskResult 0 ／CONFIG_OK ／
                                  provider_validation 7-7 ／
                                  VALIDATE_ONLY_NOT_EXECUTED ／
                                  field_side_effects 0 ／
                                  ledger NOT CREATED ／journal NOT CREATED
3. Production construction smoke   60 /  60 PASS，exit 0，FAIL 行數 0
4. Production entry-point smoke    92 /  92 PASS，exit 0，FAIL 行數 0
                                  ordering = ENTRY_GATE_PASS <
                                  AUTHORITY_ACQUIRE_ATTEMPT < AUTHORITY_ACQUIRED <
                                  FULL_PERMIT_PASS < DISPATCH_SCOPE_ENTER < CHARGE_SEND
5. Execution-deadline smoke       133 / 133 PASS，exit 0，FAIL 行數 0
6. Fresh host verify              powered on（LastBootUpTime 2026-09-29 08:56:38）
                                  water / console / Active（登入 2026-09-29 08:57）
                                  AC STANDBYIDLE   0x00000000 = Never
                                  AC HIBERNATEIDLE 0x00000000 = Never
                                  （AC VIDEOIDLE 0x12c = 300 s 僅關螢幕，非睡眠）
                                  CBS / WU pending reboot 皆 False
                                  Schedule service Running
7. Fresh external baseline        BASELINE_READY（10 / 10）／EXTERNAL_RUNNING
                                  at 2026-10-02T10:39:45
                                  pid 57483 ／cwd /home/etica/ems ／
                                  cmd python3 auto_control.py ／
                                  screen_count 1 ／guard_variant B2 ／capability_ok
                                  loop marks 10:39:25 -> 10:39:35 -> 10:39:43（遞增）
                                  SSH = {probe 1, loopcheck 3}（唯讀）
                                  pause / restore / authority / pcs = 0 / 0 / 0 / 0
8. Fresh legacy Task scan         9 個 Task 全部 Disabled，NextRun 全 N/A
                                  future executable project Task = 0
                                  無殘留 rehearsal / validate-only task
```

🔴 STEP 7 的 baseline 是**全新取得**（`ATTEMPT9_PRE_TASK_FRESH_BASELINE`），
未沿用 Attempt #8 或任何 cached evidence。

## 本輪發現並修正的一個真實缺陷

```
STEP 5 第一次執行 exit=1，FAIL 1 項：X1 config 可解析
```

原因：`test_phase6_execution_deadline.py` 的 X1 以 **cwd 相對路徑**
讀取 `scheduled_first_live_attempt8_20261002.json`。
裁示指定的正式命令格式是從 repo root 執行
（`cd /d "D:\Crawler Sample"` -> `python test/...`），因此找不到檔案。

修正：改為 `os.path.join(HERE, ...)`，cwd-independent。
修正後**兩種 cwd 皆 133 / 133 PASS**：

```
cd test              -> python test_phase6_execution_deadline.py        133/133 exit 0
cd "D:\Crawler Sample" -> python test/test_phase6_execution_deadline.py 133/133 exit 0
```

其餘兩個 smoke 一併複驗 cwd 獨立性：
construction 60/60、entry-point 92/92，兩種 cwd 皆 exit 0。

🔴 此缺陷**只影響測試可攜性**，不影響 production 行為；
但它會讓正式 PRE-FIELD 命令誤報 FAIL，因此必須修。

## 尚未進行的動作

```
Attempt #9 config / wrapper / Task XML / ledger /
per-attempt handoff journal / artifact dir / launcher log = NOT CREATED
手動 /Run ／pause ／restore ／authority acquire ／
CHARGE ／DISCHARGE ／PCS STOP ／arm transport = 全部未執行
HO.DISPATCH_ENABLED ／pcs_auto_control_runtime.DISPATCH_ENABLED = 未修改
```

等待使用者明確指定 `target_date` / `window_start` / `window_end`
（timezone 若維持現有設定為 Asia/Taipei）後，才會進入 Task creation。


---

# FIRST LIVE ATTEMPT #9 — FIELD TASK CREATION（2026-10-05）

```
FIELD TASK CREATION   = PASS
FIRST LIVE ATTEMPT #9 = AUTHORIZED / WAITING FOR 2026-10-06 00:05 SCHEDULED TRIGGER
Unattended LIVE       = HOLD
```

🔴 **本輪只建立 Task，未手動 `/Run`，未執行任何 field control。**

授權依據：使用者 2026-10-05 確認 FIELD AUTHORIZATION INTENT，並於恢復外部
`auto_control.py` 後指定 **target_date 2026-10-06 ／window 00:05 ～ 00:30
／Asia/Taipei**。
🔴 `window_end` 由使用者明確選定（00:30），**未自行沿用**前幾次的設定。

## 期間發生的兩件現場事實

```
1. 外部控制器中斷
   2026-10-02 10:39  EXTERNAL_RUNNING   pid 57483
   2026-10-05 09:26  EXTERNAL_NOT_RUNNING   pid 空／loop marks 全 None
                     -> PRE-FIELD STEP 7 FAIL，Attempt #9 一度判為 NOT READY
   2026-10-05 10:41  使用者恢復後複驗 EXTERNAL_RUNNING   pid 3209（新行程）
   期間 Phase 6 未送出任何 pause / restore / 控制指令（累計仍 4 / 4）

2. 主機重新登入
   boot 仍為 2026-09-29 08:56:38（uptime 6 天，未重開機）
   但 2026-10-05 08:57:09 有一次新的互動登入（session ID 1 -> 2、新 explorer.exe）
   當時無 armed Task，故無安全影響；Task 建立後不得再登出。
```

## PRE-FIELD 八步（2026-10-05 全部 fresh，全部 PASS）

```
1. Full Phase 6 Regression        7022 / 7022 PASS，65 files，0 FAIL
2. Validate-only rehearsal        Phase6_FirstLive_ValidateOnly_I（已刪除）
                                  LastTaskResult 0 ／CONFIG_OK ／
                                  provider_validation 7-7 ／
                                  VALIDATE_ONLY_NOT_EXECUTED ／
                                  field_side_effects 0 ／problems []
                                  ledger NOT CREATED ／journal NOT CREATED
3. Production construction smoke   60 /  60 PASS，exit 0，FAIL 行 0
4. Production entry-point smoke    92 /  92 PASS，exit 0，FAIL 行 0
                                  ordering = ENTRY_GATE_PASS <
                                  AUTHORITY_ACQUIRE_ATTEMPT < AUTHORITY_ACQUIRED <
                                  FULL_PERMIT_PASS < DISPATCH_SCOPE_ENTER < CHARGE_SEND
5. Execution-deadline smoke       133 / 133 PASS，exit 0，FAIL 行 0
6. Fresh host verify              uptime 6 天／water console Active
                                  AC STANDBYIDLE   0x00000000 = Never
                                  AC HIBERNATEIDLE 0x00000000 = Never
                                  （AC VIDEOIDLE 0x12c = 300 s 僅關螢幕）
                                  CBS / WU pending reboot 皆 False
                                  Schedule service Running
7. Fresh external baseline        BASELINE_READY（10 / 10）／EXTERNAL_RUNNING
                                  at 2026-10-05T10:41:41 ／pid 3209
                                  cwd /home/etica/ems ／cmd python3 auto_control.py
                                  screen_count 1 ／guard_variant B2 ／capability_ok
                                  loop 10:41:24 -> 10:41:33 -> 10:41:41（遞增）
                                  SSH = {probe 1, loopcheck 3}（唯讀）
8. Fresh legacy Task scan         9 個舊 Task 全部 Disabled，NextRun 全 N/A
                                  future executable project Task = 0
```

## ATTEMPT #9 CONFIG

`test/scheduled_first_live_attempt9_20261006.json`
```
enabled              true
attempt_id           FIRST-LIVE-ATTEMPT9-20261006
target_date          2026-10-06
window_start/end     00:05 / 00:30
timezone             Asia/Taipei
dependency_provider  phase6_first_live_field -> phase6_first_live_field_provider
ledger_path          output/phase6_scheduled_first_live/FIRST-LIVE-ATTEMPT9-20261006.jsonl
artifact_dir         output/phase6_scheduled_first_live/attempt9_20261006
```
`load_config = CONFIG_OK`（problems = []）。`window_state` 推演：

```
2026-10-05 23:59  BEFORE_WINDOW (360 s)
2026-10-06 00:04  BEFORE_WINDOW (60 s)
2026-10-06 00:05  INSIDE_WINDOW
2026-10-06 00:29  INSIDE_WINDOW
2026-10-06 00:31  WINDOW_EXPIRED
2026-10-07 00:10  WRONG_DATE
```

## EXECUTION DEADLINE（本次首度實戰）

```
ExecutionDeadline = 2026-10-06 00:30:00+08:00    budget = 1500 s
語意 = NO-NEW-RISK-ACTION DEADLINE（不是 process termination）
到期後禁止開始   PAUSE / authority acquire / CHARGE / DISCHARGE / 第二條 live leg
到期後仍須完成   command resolution / cleanup STOP / authority release /
                 RESTORE / RestoreVerifier
ExecutionTimeLimit PT1H = LAST-RESORT OS PROCESS LIMIT（≠ 執行預算、≠ window_end）
```

## ATTEMPT-SCOPED HANDOFF JOURNAL

```
derived = output/phase6_scheduled_first_live/
          handoff_journal_FIRST-LIVE-ATTEMPT9-20261006.jsonl
建立 Task 當下 = NOT CREATED（由 runtime 首次寫入時建立）
LEGACY SHARED JOURNAL  29 records ／4726 bytes ／
                       sha256 92d2987301a24583… ／bit-identical，未被觸碰
```

## ATTEMPT #9 TASK

`Phase6_Scheduled_FirstLive_Attempt9_20261006`（顯式 XML 註冊，UTF-16）
```
Trigger              2026-10-06T00:05:00      NextRunTime = 2026/10/6 00:05
LastRunTime          11/30/1999（哨兵值）
LastTaskResult       267011 = 0x00041303 SCHED_S_TASK_HAS_NOT_RUN
                     -> 從未執行，未手動 /Run
Schedule Type        One Time Only       Repetition 無
RestartCount         0（無 auto retry）
StartWhenAvailable   False        WakeToRun  False
MultipleInstances    IgnoreNew    ExecutionTimeLimit PT1H
RunOnlyIfIdle False  DisallowStartIfOnBatteries / StopIfGoingOnBatteries False
Principal            Water ／ InteractiveToken ／ Limited
Action               run_first_live_attempt9_20261006.cmd（絕對路徑）
State                Ready / Enabled
```
XML 內 `Repetition` ／`RestartOnFailure` ／`CalendarTrigger` ／`LogonTrigger` ／
`BootTrigger` 出現次數 = **0**。

## HOST / EXTERNAL PREREQUISITE

```
HOST_POWERED_ON_REQUIRED = True
```
自建立 Task 起至 **2026-10-06 00:05**：

```
主機         必須保持 POWERED ON
Water 帳號   必須保持 LOGGED IN      允許 Win + L
禁止         Shutdown ／Restart ／Log out ／Switch user ／
             Manual Sleep ／Hibernate
外部         auto_control.py 必須保持運行（EXTERNAL_RUNNING）
```
🔴 2026-10-05 08:57 已發生過一次重新登入；Task 建立後**不得再登出**。
🔴 若外部控制器再次停止，pre-pause preflight 會 Fail Closed 並中止，0 command。

## EVIDENCE PRESERVED

```
config     test/scheduled_first_live_attempt9_20261006.json
wrapper    test/run_first_live_attempt9_20261006.cmd
Task XML   test/Phase6_Scheduled_FirstLive_Attempt9_20261006.xml
rehearsal  test/scheduled_first_live_rehearsal_20261005i.json
           test/rehearsal_20261005i.cmd
           output/phase6_scheduled_first_live/rehearsal_20261005i_task.log
baseline   output/phase6_field_validation/attempt9_baseline_20261005_104141.json
           （另保留 20261005_092657 = 外部中斷當下的 NOT_RUNNING 證據）
```

## FINAL SAFETY STATE

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0 ／authority acquire 0
本輪 SSH 動詞 = probe 2 + loopcheck 6（兩次唯讀 baseline）
dispatch rest state  HO False ／RUNTIME False
RemoteSenders armed False   SshTransport armed False
Attempt #9 ledger ／handoff journal ／artifact dir ／launcher log 皆尚未建立
Phase 6 real cumulative  CHARGE / DISCHARGE / PCS STOP = 0 / 0 / 0
External B2 cumulative   pause / restore = 4 / 4
FIRST LIVE successful real 5 kW leg = 0 次
十個 Phase6 Task 中九個 Disabled，僅 Attempt #9 為 Ready
Unattended LIVE HOLD
```


---

# FIRST LIVE ATTEMPT #9 — POSTMORTEM（2026-10-06）

```
ATTEMPT #9 = COMPLETED / CONSUMED / CLOSED
Historical recorded result = DISPATCH_UNKNOWN   exit 4
ROOT CAUSE = POST-DISPATCH AUTHORITY BECAME UNKNOWN
SAFETY     = SAFE HANDOFF RECOVERED WITH UNRESOLVED PHASE6 LIVE LEG
```

## 1. IMPORTANT MILESTONE —— Phase 6 首次真實 5 kW CHARGE

```
timestamp        2026-10-06 00:08:46
payload          activePowerSetPoint = -5     （negative = CHARGE）
control_resp     HTTP 200 ／code 200 ／operation successful
control_success  True
readback         VERIFY_SUCCESS   observed_state = CHARGING
                 attempts 4 ／elapsed 17.34 s
```

因此 **dispatch 本身不是 UNKNOWN**。

```
leg_state_history
  ARMED → PRECHECKED → DISPATCHED → VERIFIED → OWNED
        → ABORTED_UNCERTAIN_OWNERSHIP
```

```
Phase6 real cumulative   CHARGE = 1 ／DISCHARGE = 0 ／PCS STOP = 0
External B2 cumulative   pause / restore = 5 / 5
LEG_COMPLETE = False   live_leg_result = UNKNOWN
counts_as_first_live_success = False
FIRST LIVE successful real 5 kW leg = 0 次
```

## 2. AUTHORITY EVIDENCE GAP —— 已修

### Attempt #9 的 subreason 無法還原

```
ATTEMPT #9 AUTHORITY UNKNOWN ROOT SUBREASON = NOT RECORDED / UNRESOLVED
```

歷史 artifact 全文搜尋 `CA_` ／`CONTROL_AUTHORITY` ／`authority_reason` 皆為 **0 筆**；
唯一可得的只有 `_authority_state: "IDLE"`（dispatch 前）與 launcher log 的
`authority=UNKNOWN` 字串。

已由設定值排除 `CA_TTL_UNSET` ／`CA_TOLERANCE_UNSET`
（`DEFAULT_CONTROL_CONFIG.authority_ttl_sec = 180.0`、
`authority_power_tolerance_kw = 1.25`，arbiter 用的就是這組）。
🔴 **不猜測、不回填、不改寫歷史 artifact。**

### 修正內容

```
ArbitrationResult 新增   authority_detail ／authority_result（完整 as_dict）
                        authority_ttl_sec ／authority_power_tolerance_kw
ControlledLiveLeg 新增   authority_snapshots，四個時點：
                          before_dispatch ／after_dispatch ／
                          after_readback ／at_abort
artifact 新增            authority_snapshots ／authority_at_abort ／
                        authority_result ／authority_reason ／
                        authority_detail ／authority_ttl_sec ／
                        authority_power_tolerance_kw
每個 snapshot 另含        pcs_state ／actual_active_power_kw ／
                        schedule_switch ／manual_switch ／ess_age_sec ／
                        observation_valid ／note
```

🔴 **零額外觀測**：只從「本來就會發生」的兩次觀測取樣 ——
`chain.run()` 的 arbitration（dispatch 前）與 `LEG_OWNED` 之後的 `_observe()`。
**不得**為了取樣而多呼叫 `_observe()`：arbiter 底下的 `PowerClassifier`
是**有狀態**的（time-based debounce），多餘的 observation 會改變 production 判定。
因此 `after_dispatch` 必然記為 `NOT_OBSERVED`（附 note），
`after_readback` 與 `at_abort` 來自**同一次**觀測。
取不到一律記 `NOT_OBSERVED` / `None`，**不得只省略**。

### 連帶修正的安全缺陷

```
control_authority.evaluate() 的例外處理原本是
    detail = f"{type(e).__name__}: {e}"
-> 例外**訊息**會進入 durable artifact，可能挾帶 password / token。
已改為只記例外**類別**：
    detail = "評估例外（類別）：%s" % type(e).__name__
診斷能力不變（reason 仍是 CA_EVALUATION_ERROR），但不再外洩內容。
```

## 3. UNCERTAIN OWNERSHIP + STOP POLICY —— 維持不變

```
ABORTED_UNCERTAIN_OWNERSHIP -> DO NOT AUTOMATICALLY SEND PCS STOP
DEFAULT = NO PCS COMMAND WHEN OWNERSHIP UNKNOWN
```

🔴 ownership 不可信時再送 STOP 可能覆蓋 external controller 或其他合法控制來源。
🔴 **不得**把 UNKNOWN ownership 推論成「Phase 6 仍持有 PCS」。
🔴 **目前不放寬**。只有未來能正式區分
「telemetry evidence loss 造成的 UNKNOWN」與
「可能有競爭 ownership 造成的 UNKNOWN」，
且具備完整安全證據與 regression，才可重新評估。

## 4. POST-DISPATCH UNCERTAIN OWNERSHIP RECOVERY —— 新增

但**不得**只是「不 STOP -> restore -> 結束」。正式 reconciliation 流程：

```
1 Freeze all new Phase6 PCS commands
2 不送第二次 CHARGE     3 不送 DISCHARGE     4 不送 PCS STOP
5 release Phase6 authority
6 restore external controller exactly once
7 RestoreVerifier
8 RV_RESUMED 之後
9 執行 read-only post-restore reconciliation
```

### POST-RESTORE RECONCILIATION（READ ONLY）

`phase6_first_live_field_provider.post_restore_reconciliation()`，
fresh 取得 11 項：

```
external_liveness ／external_loop_marks ／pcs_state ／
pcs_charging_flag ／pcs_discharging_flag ／pcs_standby_flag ／
pcs_fault_flag ／actual_active_power_kw ／pcs_control_mode_code ／
soc_percent ／communication_ok
```

🔴 **不得**在 reconciliation 再送任何 PCS command
（由 AST 測試證明函式內無任何控制呼叫）。
取不到一律記 `None`，**不得省略**。

### RECONCILIATION RESULT

| verdict | 條件 |
|---|---|
| `EXTERNAL_TAKEOVER_VERIFIED` | RestoreVerifier `RV_RESUMED` **且** canonical `external_liveness = EXTERNAL_RUNNING` **且** fresh 唯讀 PCS observation 可取得 |
| `EXTERNAL_TAKEOVER_UNCONFIRMED` | 上述任一不成立（Fail Closed） |
| `EXTERNAL_TAKEOVER_NOT_APPLICABLE` | 沒有未收尾的 Phase 6 指令 |

🔴 `VERIFIED` **仍不得**把 `live_leg_result` 改成 `SUCCESS`。

## 5. MANUAL ATTENTION SEMANTICS —— 新增

| 狀況 | manual_attention |
|---|---|
| CHARGE 已送出、Phase 6 未 STOP、takeover **UNCONFIRMED** | `MANUAL_ATTENTION_REQUIRED` |
| CHARGE 已送出、Phase 6 未 STOP、takeover **VERIFIED** | `OPERATOR_REVIEW_RECOMMENDED` |
| 無未收尾指令且 leg 正常 | `MANUAL_ATTENTION_NOT_REQUIRED` |

artifact 另有 `pcs_state_warning`，在 takeover 未驗證時明寫：

```
PCS MAY STILL BE UNDER LAST PHASE6 SETPOINT
```

🔴 **不得**只寫 `DISPATCH_UNKNOWN`。

## 6. ATTEMPT / DISPATCH / LIVE LEG CLASSIFICATION —— 更正

```
dispatch_result   DISPATCH_SUCCEEDED          （新欄位）
readback_result   VERIFY_SUCCESS
live_leg_result   UNKNOWN
attempt_result    AUTHORITY_UNKNOWN_AFTER_DISPATCH   （canonical enum）
```

🔴 全專案只用 `R_AUTHORITY_UNKNOWN_AFTER_DISPATCH` 表達此終態；
`DISPATCH_UNKNOWN` 保留給「真的不知道有沒有送出」的情況。

`_classify()` 只有在 **dispatch 確定成功**
（`dispatch_result == DISPATCH_SUCCEEDED` 且 `readback_result == VERIFY_SUCCESS`）
時才改記新 enum —— 這是**精確化**，不是放寬：兩者都不是 PASS，
`live_leg_result` 仍為 `UNKNOWN`、仍不計數。

### 為何 live_leg_result 維持 UNKNOWN

指令與回讀確實成功，但 `LEG_COMPLETE = False` 且 cleanup STOP 未由 Phase 6 執行 ——
既不得標 `SUCCESS`，也不應簡化為 `FAILED`。

### SUCCESS COUNT 規則不變

```
counts_as_first_live_success = True   <=>   live_leg_result == SUCCESS
Attempt #9 -> False -> FIRST LIVE successful real 5 kW leg 仍為 0
```

## 7. EXIT CODE CLASSIFICATION —— 更正

`classify_exit(result, artifact=None)`：

```
REFUSED                        -> exit 3（永遠最優先）
MANUAL_ATTENTION_REQUIRED      -> exit 4
OPERATOR_REVIEW_RECOMMENDED    -> exit 0
MANUAL_ATTENTION_NOT_REQUIRED  -> exit 0
artifact 未提供 manual_attention -> 沿用舊的 result-based 判定（Fail Closed）
未知 result                     -> exit 5
```

🔴 不再單純由 `DISPATCH_UNKNOWN` 推導 exit 4。
「authority UNKNOWN after dispatch ＋ restore VERIFIED ＋ EXTERNAL_RUNNING ＋
reconciliation PASS」與「restore UNKNOWN ＋ ownership 未解決」
不再共用同一個 exit classification。

## 8. HISTORICAL ARTIFACT —— 不得改寫

```
Historical stored attempt_result = DISPATCH_UNKNOWN        （保持原樣）
Postmortem semantic correction：
    Actual dispatch      = DISPATCH_SUCCEEDED
    Actual abort reason  = UNCERTAIN_OWNERSHIP_AFTER_DISPATCH
```

Attempt #9 的 artifact ／ledger ／journal ／launcher log ／Task history
全部 immutable，**未回寫**（由 regression N4 / N4b 斷言）。

## 9. MANDATORY TESTS

`test/test_phase6_authority_evidence.py` —— **83 / 83 PASS**，列為 mandatory。

| 群組 | 內容 |
|---|---|
| `A1 ~ A9` | AuthorityResult reason/detail 可診斷；六個 reason 常數存在且互異；例外 -> UNKNOWN 且**不洩漏 secret**；TTL / tolerance 未設定各有專屬 reason |
| `A1W` | reason/detail 真的進入 artifact schema；四個取樣時點；取不到記 `NOT_OBSERVED` |
| `A9R1 ~ A9R20` | **Attempt #9 exact regression**（production entry point）：CHARGE 1／DISCHARGE 0／STOP 0／`dispatch_result = DISPATCH_SUCCEEDED`／readback `VERIFY_SUCCESS`／`live_leg_result = UNKNOWN`／`attempt_result = AUTHORITY_UNKNOWN_AFTER_DISPATCH`／不計數／authority reason・detail・門檻皆保存 |
| `R1 ~ R5` | post-restore reconciliation：11 欄位齊備／`READ_ONLY_NO_CONTROL`／takeover VERIFIED 仍不得改 SUCCESS／restore UNKNOWN 與 liveness UNKNOWN 皆 `MANUAL_ATTENTION_REQUIRED`／PCS 觀測取不到不得偽稱 reconciled／**AST 證明無任何控制呼叫** |
| `N1 ~ N4` | STOP policy 未放寬／計數規則未變／exit 分類四種情境／歷史 artifact 未被回寫 |
| `S1 ~ S8` | shadow rehearsal 契約（AST 證明不呼叫任何 side-effect 入口） |

## 10. PRODUCTION READ-ONLY SHADOW REHEARSAL —— 新增

`test/phase6_shadow_rehearsal.py`

```
用真實 production config ／provider ／HMI 唯讀 ／meter 唯讀 ／
external probe & loopcheck ／production entry path
但禁止 PAUSE ／authority acquire ／CHARGE ／DISCHARGE ／PCS STOP
必須在第一個 field side effect 之前硬性停止（SHADOW_STOP_BEFORE）
field_authorized = False ／inside_window = False
side effect 以「**本次是否新建** ledger / journal」判定，不誤判歷史檔案
```

2026-10-06 對 Attempt #9 的正式 config 實跑：**`SHADOW_REHEARSAL_PASS`**，
11 項全 PASS，0 field side effect。

🔴 `SHADOW PASS` **不得**被描述成 real field PASS ——
它只證明「到 PAUSE 之前的 production 路徑可用」，
**不**證明交接、live leg 或收尾。

## 11. PRE-FIELD NINE-STEP RULE（取代八步）

```
1. Full Phase 6 Regression
2. Validate-only rehearsal
3. Production construction smoke                建構 / wiring
4. Production entry-point / control-flow smoke  呼叫順序
5. Production execution-deadline / safe-cleanup smoke   runtime deadline / 收尾
6. Production read-only shadow rehearsal        0 control side effect
7. Fresh host verify
8. Fresh external baseline
9. Fresh legacy Task scan
```

## 12. ATTEMPT #10

```
ATTEMPT #10 = NOT AUTHORIZED
```

Blockers 進度：

```
1 Authority reason/detail durable evidence          DONE
2 Attempt #9 exact regression                       DONE（A9R1~A9R20）
3 post-dispatch uncertain ownership reconciliation  DONE（R1~R5）
4 attempt_result classification correction          DONE
5 exit / manual-attention semantics                 DONE（N3a~N3e）
6 Production read-only shadow rehearsal             DONE（S1~S8 ＋ 實跑 PASS）
7 Full regression                                   見下
```

## 13. FINAL SAFETY

```
HO.DISPATCH_ENABLED False   pcs_auto_control_runtime.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
External = EXTERNAL_RUNNING

Phase6 real cumulative   CHARGE / DISCHARGE / PCS STOP = 1 / 0 / 0
External B2 cumulative   pause / restore = 5 / 5
FIRST LIVE successful real 5 kW leg = 0

FIRST LIVE = HOLD      Unattended LIVE = HOLD
```


---

# FIRST LIVE ATTEMPT #9 — TASK CLOSURE（2026-10-06）

```
Phase6_Scheduled_FirstLive_Attempt9_20261006
  BEFORE   State = Ready      NextRun = N/A
  AFTER    State = Disabled   NextRun = N/A
  LastRunTime    = 2026-10-06 00:05:00   （未回寫）
  LastTaskResult = 4                     （未回寫）
```

🔴 只做 **Disable**，**未** Unregister / Delete。
🔴 Disable **不是**為了修 execution defect —— 目前本來就不會自動重跑。

目的：

```
1. 與 Attempt #8 等歷史 Attempt 結案方式一致
2. 防止未來人工誤觸 /Run
3. Task Scheduler 狀態明確反映 Attempt #9 已 CLOSED
```

## AFTER DISABLE — 唯讀複驗

```
Phase6 / FirstLive / ValidateOnly Tasks     10 個
  全部 Disabled                              10 / 10
  非 Disabled                                0
  NextRunTime 非空者                         0
全系統：未停用且會執行本專案腳本的 Task      0
殘留 rehearsal / validate-only task          無
```

| Task | State | NextRun |
|---|---|---|
| `Phase6_FirstChargeLive_Precheck_OneTime` | Disabled | N/A |
| `Phase6_FirstLive_Charge_OneTime` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt2_20260922` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt3_20260923` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt4_20260924` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt5_20260925` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt6_20260930` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt7_20261001` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt8_20261002` | Disabled | N/A |
| `Phase6_Scheduled_FirstLive_Attempt9_20261006` | **Disabled** | N/A |

## HISTORICAL EVIDENCE — 全部保留，未更動

```
test/scheduled_first_live_attempt9_20261006.json                   4837 B
test/run_first_live_attempt9_20261006.cmd                           996 B
test/Phase6_Scheduled_FirstLive_Attempt9_20261006.xml              3556 B
test/scheduled_first_live_rehearsal_20261002h.json ／.cmd
output/.../FIRST-LIVE-ATTEMPT9-20261006.jsonl                      2065 B
output/.../handoff_journal_FIRST-LIVE-ATTEMPT9-20261006.jsonl      5884 B
output/.../FIRST-LIVE-ATTEMPT9-20261006.launcher.log               3736 B
output/.../attempt9_20261006/scheduled_first_live_..._000939.json 33309 B
output/phase6_field_validation/attempt9_baseline_20261005_104141.json

歷史 artifact 的 attempt_result 仍是 DISPATCH_UNKNOWN（未回寫）
handoff_artifact 仍無 authority_reason —— evidence gap 原樣保留，未回填
LEGACY SHARED JOURNAL  29 records ／4726 bytes ／
                       sha256 92d2987301a24583… ／bit-identical
```

## AUTHORITY SNAPSHOT — ZERO-EXTRA-OBSERVATION DESIGN（正式接受）

```
DIAGNOSTIC EVIDENCE 不得為了增加觀測密度而改變 production control semantics。
```

🔴 **不得**為了取得四個 snapshot 而額外呼叫 `_observe()` ——
它會更新有狀態的 `PowerClassifier`（time-based debounce），
可能改變 production 判定。

某時點若無既有 production observation，一律記：

```
state             = NOT_OBSERVED
reason            = NOT_OBSERVED
detail            = NOT_OBSERVED
authority_result  = NOT_OBSERVED
note              = 為避免額外 observation 擾動 stateful classifier
                    （PowerClassifier 使用 time-based debounce），此時點未額外觀測
```

🔴 **不得**省略欄位；**不得**複製前一筆 authority result 假裝成新 observation。
🔴 `after_dispatch = NOT_OBSERVED` **不是** evidence defect ——
真正必要的是：後續既有 observation 若產生 `authority UNKNOWN`，
必須保存 reason ／detail ／authority_result ／thresholds ／observable inputs。
本輪已完成。

### 不建立第二套 authority 判定

```
不建立：authority-only evaluator ／second classifier ／
        second arbitration path ／diagnostic-only authority policy
原因：會帶來第二套判定語意與 production arbitration drift 的風險。
```

除非未來證明既有自然 observation 仍不足以定位 production failure，再另案設計。

## 正式維持的三項契約

```
1. ATTEMPT #9 AUTHORITY UNKNOWN ROOT SUBREASON = NOT RECORDED / UNRESOLVED
   不得猜測 ／回填 ／推定；只能記錄 historical evidence 不足。

2. DEFAULT = NO PCS COMMAND WHEN OWNERSHIP UNKNOWN
   authority UNKNOWN after dispatch 不得自動 PCS STOP / CHARGE / DISCHARGE。

3. SECRET-SAFE AUTHORITY DETAIL
   authority evaluation exception 不得持久化 str(exception)，只記 class / category。
```

## FINAL STATUS

```
ATTEMPT #9 = COMPLETED / CONSUMED / CLOSED      Task = Disabled ／NextRun N/A
Historical attempt_result = DISPATCH_UNKNOWN（immutable）
Postmortem：CHARGE 5 kW = SENT / SUCCESS ／Readback = VERIFY_SUCCESS ／
            Abort = AUTHORITY_UNKNOWN_AFTER_DISPATCH

Phase6 real cumulative   CHARGE / DISCHARGE / PCS STOP = 1 / 0 / 0
External B2 cumulative   pause / restore = 5 / 5
FIRST LIVE successful real 5 kW leg = 0

HO.DISPATCH_ENABLED False   pcs_auto_control_runtime.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
External = EXTERNAL_RUNNING

ATTEMPT #10 = READY FOR NEW FIELD AUTHORIZATION
ATTEMPT #10 = NOT AUTHORIZED
Attempt #10 config / Task / ledger / handoff journal / artifact = NOT CREATED

FIRST LIVE = HOLD      Unattended LIVE = HOLD
```


---

# FIRST LIVE ATTEMPT #10 — PRE-FIELD FAIL ／HOLD（2026-10-06）

```
ATTEMPT #10 = HOLD
PRE-FIELD   = FAIL（九步中 STEP 6 與 STEP 8 未通過）
Task        = NOT CREATED
```

使用者 2026-10-06 指定條件：

```
target_date  = 2026-10-07     window_start = 00:05
window_end   = 00:30          timezone     = Asia/Taipei
```

並明列 15 項成功條件（全達成則 `live_leg_result = SUCCESS`、
`counts_as_first_live_success = True`、FIRST LIVE successful real 5 kW leg = 1、
Attempt #10 = SUCCESSFUL FIRST LIVE、FIRST LIVE 階段可結案、Attempt #11 不需要）。
條件已完整寫入 config 的 `_success_criteria`。

同日稍晚使用者裁示：**目前實體無法驗證，Attempt #10 流程先暫停。**

## PRE-FIELD 九步結果

```
1. Full Phase 6 Regression        7105 / 7105 PASS，66 files，0 FAIL      PASS
2. Validate-only rehearsal        Phase6_FirstLive_ValidateOnly_J（已刪除）
                                  LastTaskResult 0 ／CONFIG_OK ／
                                  provider_validation 7-7 ／
                                  field_side_effects 0 ／problems []
                                  ledger NOT CREATED ／journal NOT CREATED   PASS
3. Production construction smoke   60 /  60 PASS，exit 0                    PASS
4. Production entry-point smoke    92 /  92 PASS，exit 0                    PASS
5. Execution-deadline smoke       133 / 133 PASS，exit 0                    PASS
   （authority evidence 83 / 83 PASS 一併複驗）
6. Production shadow rehearsal    🔴 SHADOW_REHEARSAL_ERROR                 FAIL
                                  dependencies_built = False
                                  ProviderPreconditionFailed
                                  「HMI 登入失敗 -> Fail Closed」
7. Fresh host verify              uptime 7 天 ／water console Active
                                  AC STANDBYIDLE / HIBERNATEIDLE 皆 Never
                                  無 pending reboot ／Schedule Running       PASS
8. Fresh external baseline        🔴 ABORTED_BASELINE_NOT_READY              FAIL
                                  EXTERNAL_NOT_RUNNING
9. Fresh legacy Task scan         10 個 Task 全部 Disabled ／
                                  future executable project Task = 0        PASS
```

## 兩個 blocker

### ① HMI 不通（最硬的前提）

```
endpoint   http://192.168.128.110:8080/admin-api/system/auth/login
exception  ConnectTimeout（connect 5 s / read 15 s）
重試       13:12:18 ／13:12:50 ／13:19 三次皆逾時
對照       同日 10:00 與 10:41 的 HMI login 皆成功
```

Phase 6 的 ESS 觀測與 PCS 指令**都走 HMI**。不通時
`build_field_dependencies()` 直接擲 `ProviderPreconditionFailed` ——
不只是不能充電，連觀測都建不起來。

### ② 外部 `auto_control.py` 未運行

```
13:13:08   probe = SSH_OK（Linux 主機可達）／guard_variant = B2
           screen_count = 1 但 pid 空
           process_alive / process_identity_ok FAIL
           external_liveness = EXTERNAL_NOT_RUNNING
對照       2026-10-05 10:41 使用者恢復後 pid = 3209
```

🔴 這是三天內**第二次**自行停止（10-02～10-05 一次、10-05～10-06 一次）。
`screen_count = 1` 但 pid 空 —— screen session 還在、裡面的 python 行程死了，
比較像 `auto_control.py` 自己 crash / exit，而非 screen 被關掉。

### 為什麼不能「略過外部控制器直接充電」

程式上的硬性要求（非推論）：

```
HANDOFF_READINESS_ITEMS = external_runtime_confirmable ／loop_evidence_available ／
                          pause_capability_available ／restore_capability_available
PRE_PAUSE_ITEMS 含       external_runtime_healthy ／external_behaviour_known
PRE_PAUSE_MUST_NOT_REQUIRE 明列不得要求 no_external_controller
```

整個 6.10 的設計前提就是「**有一個正在跑的外部控制器可以被 pause 再 restore**」。
外部控制器不在就沒有東西可以交接，readiness 與 pre-pause 一律 Fail Closed。
要改成「無外部控制器也能跑」屬**新的設計裁示**，不得自行為之。

## HOLD 狀態下的殘留風險評估

```
Attempt #10 Task          NEVER REGISTERED -> 不會有任何東西自行觸發
ledger / handoff journal / artifact / launcher log   皆 NOT CREATED
已建立但未武裝            config ／wrapper ／Task XML（非 execution state）
```

config 仍為 `enabled = true`、`target_date = 2026-10-07`。離線推演誤觸結果：

```
2026-10-07 00:05   INSIDE_WINDOW    （唯一仍會嘗試執行的區間）
2026-10-07 00:31   WINDOW_EXPIRED   R_EXPIRED     -> 0 command
2026-10-08 之後     WRONG_DATE       R_WRONG_DATE  -> 0 command
```

🔴 亦即：**2026-10-07 00:05–00:30 若有人手動執行 wrapper**，流程會嘗試啟動；
00:31 之後則永久 `WRONG_DATE` -> 0 command。
由於 Task 從未註冊、且 HMI 與外部控制器皆不可用（會在 provider 建構即
Fail Closed），實際風險極低；若要完全消除，可將 config 的 `enabled` 改為 `false`
（-> `R_NOT_ENABLED` -> `EXIT_REFUSED` -> 0 command），該動作可逆。

## 復原後的要求

```
1. 修復 HMI 連線（硬前提）
2. 恢復 auto_control.py，並查明三天內兩次自行停止的原因
3. 由使用者重新指定 target_date / window（2026-10-07 已不可用）
4. PRE-FIELD 九步**全部重跑 fresh**，不得沿用 2026-10-06 任何證據
5. 九步全 PASS 才建立 Attempt #10 field Task
```

## 本輪安全狀態

```
本輪 CHARGE 0 ／DISCHARGE 0 ／PCS STOP 0 ／pause 0 ／restore 0 ／authority acquire 0
本輪 SSH 動詞 = probe 3 + loopcheck 9（三次唯讀 baseline）
HO.DISPATCH_ENABLED False   pcs_auto_control_runtime.DISPATCH_ENABLED False
RemoteSenders armed False   SshTransport armed False
HMI        = UNREACHABLE
External   = EXTERNAL_NOT_RUNNING
10 個 Phase6 Task 全部 Disabled ／future executable project Task = 0

Phase6 real cumulative   CHARGE / DISCHARGE / PCS STOP = 1 / 0 / 0
External B2 cumulative   pause / restore = 5 / 5
FIRST LIVE successful real 5 kW leg = 0 次

FIRST LIVE = HOLD      Unattended LIVE = HOLD
```
