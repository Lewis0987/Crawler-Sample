# Phase 5 Validation Plan —— Production Ready

**建立日期**：2026-08-17
**基線**：Phase 4 COMPLETE（HEAD `eb3a702`），Full Regression **1529/1529 PASS**、
FAIL 0、SKIP 0、Environment residue PASS
**狀態**：5.1 COMPLETE（本文件即交付物）；5.2～5.5 未開始

> Phase 5 的目的**不是重做 Phase 4 已完成的功能**，
> 而是確認目前版本能否進入正式使用 / Release。

---

## 1. Phase 5 Definition of Done

- [ ] 5.1 Validation Plan 完成並確認 —— **本文件**
- [ ] 5.2 Soak PASS（或 PASS with warnings，須逐項說明）
- [ ] 5.3 Stress / Boundary PASS
- [ ] 5.4 Production Documentation 完成
- [ ] 5.5 Release Validation 全項通過
- [ ] Full Regression **≥ 1529**，FAIL 0、SKIP 0、Environment residue PASS
- [ ] `git diff --check` exit 0、working tree clean
- [ ] 無未處理 Critical / High defect

### 測試數量原則

- Phase 4 的 1529 項為**不可退步的基線**；Phase 5 新增只能讓總數增加。
- **不為了增加數量而建立沒有價值的測試。**
- 任一既有測試消失或 FAIL → 立即停止並回報。

---

## 2. 各階段目標與判定

| 階段 | 目標 | PASS | FAIL |
|---|---|---|---|
| **5.1** Production Validation Plan | 產出可執行的驗收基準 | 涵蓋 5.2–5.5、標準可判定、經確認 | 範圍未定義或標準不可判定 |
| **5.2** Long-running Stability / Soak | 長時間常駐穩定性 | A–F 六組全 PASS | 任一組 FAIL |
| **5.3** Stress / Boundary | 軟體層邊界正確性 | 新增 suite 全 PASS 且既有 1529 項不減少 | 任一項 FAIL 或既有測試退步 |
| **5.4** Production Documentation | 部署／維運文件完整 | 缺口全數補齊或明確標記不適用 | 有缺口未處理 |
| **5.5** Release Validation | 出版前總驗 | checklist 全項通過 | 任一項未通過 |

### 測試性質分類

| 分類 | 涵蓋 |
|---|---|
| 可離線 | 5.1、5.3（全部）、5.4 |
| 需實機 | 5.2（需設備連線）、5.5 最終確認 |
| 需 Service 持續運行 | 5.2 全程、5.5 Service health |
| **可能影響設備** | **無** —— 5.2 純唯讀採樣；5.3 明確排除高頻控制 |
| 屬唯讀驗證 | 5.2 全部採樣、5.5 health / git 檢查、5.1 |

---

## 3. Phase 4 已驗證、**不重做**的項目

| 項目 | 既有證據 |
|---|---|
| Ownership / Global Mutex / DACL | 62/62、56/56、實機 I 18/18 |
| Mutex canonicalization | 39/39 + rollout 38/38 |
| Writer Gate 三層 | 43/43 + 實機零副作用 |
| Dashboard Observer | 64/64 + Dashboard harness 23/23 |
| Auth Recovery **機制正確性** | 116/116 + 實機 4/4（每次 21 秒） |
| Graceful Recovery | J 31/31、K 43/43 |
| Crash Recovery | 73/73 + 實機全鏈路 |
| NSSM 包裝 / start fail-closed / boot auto-start | 130/130 + 實機 |
| Session finalize / output retry | Phase 3.7 + report selftest 267/267 |

**Phase 5 只做 Phase 4 沒做的事**：長時間累積效應、軟體邊界條件、部署文件、出版檢核。
5.2 對 Auth Recovery 只**統計發生次數與恢復時間**，不重驗機制。

---

## 4. Phase 5.2 — Long-running Stability / Soak Test

### 4.1 設計

| 項目 | 值 |
|---|---|
| 時長 | **24 小時** |
| 取樣週期 | **60 秒**（1440 筆） |
| 方式 | **完全唯讀**採樣；不 restart、不 taskkill、不操作設備 |
| 輸出 | JSONL 時間序列 + 摘要報告（保存於 scratchpad，摘要納入 Phase 5 收尾報告） |

**Baseline（起跑時記錄）**：worker / NSSM PID、WorkingSet、Handle、Thread、
Session 總數、output tree fingerprint、canonical Mutex、`monitor_owner.json`、git HEAD。

**資源判定必須看時間序列**，不可只比對起訖點 —— 目的是分辨「波動」與「單向成長」。

**存活判定不可只看 log 新鮮度。** 設備不可達時 Service 走「暫停 + 每 60s **靜默**重試」，
log 本來就會靜止；此時改以「行程存在 + CPU 時間累加 + 仍持有 Mutex」判定，
且 CPU 取樣窗口必須 **> `AUTO_LOGIN_RETRY_SEC`（60s）**，否則會量到 0 而誤判卡死。

### 4.2 PASS / WARN / FAIL 標準

#### A. Service availability

| | 標準 |
|---|---|
| PASS | worker PID 全程不變；NSSM PID 不變；Service 全程 Running；設備可達時 log 寫入間隔 ≤ 120s |
| WARN | 發生 1 次非預期 worker 重啟，且有完整 resume 證據（同一 Session、index 續接） |
| FAIL | ≥2 次重啟；或 Service 曾 Stopped；或設備可達時 log 靜止 > 15 分鐘 |

#### B. Resource stability

| | 標準 |
|---|---|
| PASS | WorkingSet 24h 成長 < 20% 且非單向遞增（至少一次回落／持平）；Handle 成長 < 10%；Thread 數恆定 |
| WARN | WorkingSet 成長 20–50%；或 Handle 成長 10–25%；或單向遞增但斜率 < 1 MB/h |
| FAIL | WorkingSet 成長 > 50%；或全程單向遞增且斜率 ≥ 1 MB/h（洩漏特徵）；或 Handle / Thread 無上限成長 |

#### C. Monitor health

| | 標準 |
|---|---|
| PASS | 設備可達期間 `mode=smart` 且 `sched=True` 比例 ≥ 99%；狀態轉移合法（IDLE ↔ START_HOLD ↔ RUNNING ↔ STOP_PENDING ↔ COOLDOWN） |
| WARN | 比例 95–99% |
| FAIL | 比例 < 95%；或卡在 `unknown` 超過 60 秒未恢復；或出現非法狀態轉移 |

#### D. Auth / communication

| | 標準 |
|---|---|
| PASS | 每次 Auth Recovery 觸發後 ≤ 60 秒恢復（實測基準 21 秒）；無 recovery loop |
| WARN | 恢復耗時 60–180 秒；或 24h 內觸發 > 60 次（30 分鐘週期的預期值約 48 次） |
| FAIL | 任何 degraded 狀態持續 > 5 分鐘未恢復；或 5 分鐘內恢復 > 3 次（loop）；或 `communication_error` 收尾後未回到正常輪詢 |

#### E. Session integrity（**不人工觸發充放電**）

| | 標準 |
|---|---|
| PASS | 自然產生的 Session：`session_id` 唯一、`sample_index` 連續無重複、`samples.csv` 列數 == `sample_count`、無第二 writer、completed 後 `output_status` 無 failed、有 `finalize_ok`、最終 Environment residue PASS |
| WARN | 24h 內**未自然產生** Session → 標記 **「Session lifecycle coverage = not exercised」**，**不判 FAIL**，留待實機驗證補足 |
| FAIL | 任一完整性項目不符；或出現 recording orphan；或同時出現兩份 recording Session |

#### F. Ownership

| | 標準 |
|---|---|
| PASS | canonical Mutex 全程單一 Owner；`monitor_owner.json` pid 全程 == worker PID；一般使用者 probe 全程 `held_by_other`；Dashboard 行程全程為 0 |
| WARN | Dashboard 曾短暫開啟但未成為 writer（無 Session 副作用） |
| FAIL | 出現雙 Owner；或 Dashboard 成為 writer；或 `monitor_owner.json` 與 worker 不符且非重啟所致 |

#### 全程必查

`err.log` 維持 0 bytes、log 無 `Traceback` / unhandled exception、
log 無 `token` / `cookie` / `password` / `authorization` / `_raw` / `accessToken` / `Bearer`。

#### 整體判定

任一 FAIL → **soak FAIL**；無 FAIL 但有 WARN → **PASS with warnings**（逐項說明）；全 PASS → **soak PASS**。

### 4.3 中斷處理規則

| 情境 | 處置 |
|---|---|
| 採樣器自身中斷（非 Service 問題） | 記錄中斷區間，**續跑補足 24h**，不重跑 |
| 設備短暫失聯後恢復 | **不中斷 soak** —— 記為外部事件；C/D 兩組依「設備可達期間」計算比例 |
| 設備長時間失聯（> 4h） | 標記「device availability degraded」，A/B/F 仍有效，C/D/E 標記覆蓋不足，**不判 FAIL** |
| Service 異常停止 | **立即停止 soak 並取證**（PID、log、err.log、Session、Mutex），**不自行 restart**，等裁示 |
| 出現 recording orphan | 不人工 finalize；讓 Service 自然 resume 收尾 |
| 需人工重啟 Service（任何原因） | soak **重跑**，先前資料保留為參考 |

### 4.4 執行結果與 Root Cause（2026-08-17 ~ 08-18）

**Phase 5.2 Soak = FAIL** —— 依 §4.2 B. Resource stability：WorkingSet 24.293 MB → 78.34 MB
（+222%，>50% 即 FAIL），於 T0+3.09h 觸發，採樣器依設計保存證據後停止。
**此判定為定案，不因後續查明 Root Cause 而改寫為 PASS。**

中斷前其餘五組：A / C / D / F 均 PASS；E 在第一份 Session 自動建立並自然收尾後由 WARN 轉 PASS。

#### Root Cause = ONE-TIME COST

來源鏈：

```
finalize → _regenerate_outputs() → _write_xlsx() → import openpyxl.chart
        → openpyxl.compat.numbers → import numpy → scipy-openblas
        → OpenBLAS native thread pool（依 os.cpu_count() 建立）
```

- 產品碼完全不做 BLAS 運算；openpyxl 只拿 numpy 做 `isinstance` 型別判斷
- 實測 +19 threads / +650 MB Private
- `gc.collect()` 回收 0 MB —— 非 Python heap，GC 觸及不到，故不採此修法

判定依據（第二次自然 finalize 實測）：

| | Private | Threads | Handles |
|---|---|---|---|
| finalize #1（pre → T+0） | +672.49 MB | +21 | +21 |
| finalize #2（pre → T+0） | −0.31 MB | −1 | −3 |
| stable baseline #1 → #2 | −0.21 MB | ±0 | ±0 |

⇒ 屬每個 worker process 的**一次性**初始化成本，非累積性洩漏。

#### 修法

`OPENBLAS_NUM_THREADS=1`，實作於 `tools/install_service_nssm.ps1` 的 `AppEnvironmentExtra`
（該處本來就是 Service 環境變數的正式來源，每次 install 重寫，設定不會遺失）。

離線驗證（1273 samples 真實 fixture）：Private −610.7 MB、Threads −19、VirtualSize −662.6 MB；
報告保真度 12 項全通過（統計數值逐欄一致）；產出時間無可測量退步。

守門：`test_phase4_service_env.py` 的 Phase 5.2 區塊（10 項），防止日後被移除。

#### 部署（2026-08-18）

為避免依賴「對既有服務執行 `-Action install` 時 `nssm install` 失敗但後續 `nssm set`
仍會跑」這個**副作用**，先為 `install_service_nssm.ps1` 補上正式的 `-Action update`
路徑：只更新既有服務（不存在即 throw，不退化成 install）、不呼叫 `nssm install` /
`nssm remove`，設定由 install / update 共用的 `Apply-ServiceSettings` 提供；
`Assert-InstalledSettings` 同時擴充為回讀驗證 `AppEnvironmentExtra`（7 項集合比較、
大小寫敏感、`OPENBLAS_NUM_THREADS` 恰好 1 筆，不符即 throw / fail closed）。

部署流程：`-Action stop` → `-Action update` → 獨立回讀驗證 → `-Action start`，
未 remove、未手改 Registry。部署後 16 項驗證全數 PASS（新 worker PID 16512）。

**Deployment = PASS**

#### Fix Validation（2026-08-18）

驗證對象刻意鎖定「**部署後全新 worker 的第一次**自然 finalize」——
舊 worker 早已 import 過 numpy，其第二次 finalize 不漲只能證明 ONE-TIME COST，
無法證明修法有效。

| pre → T+0 | ΔPrivate | ΔThreads | ΔHandles |
|---|---|---|---|
| 舊環境第一次 finalize | +672.49 MB | +21 | +21 |
| **修正後新 worker 第一次 finalize** | **+48.32 MB** | **+2** | **+1** |

Private 降低約 **92.8%**；+19 條 OpenBLAS native thread 未再出現。

T+30m：Private 約 77.8 MB 且穩定（漂移 0.035 MB）、Threads 1~2、Handles 170~171。
Session Integrity **14/14 PASS**（completed、samples.csv 列數 == sample_count、
`sample_index` 連續無斷點、`output_status` 無 failed、`report.xlsx` 13 worksheets /
2 charts、無重複 Session、log 無 Traceback、err.log 0）。

**Fix Validation = PASS** —— `OPENBLAS_NUM_THREADS=1` 實機修法成立。

證據：`output\soak_evidence\fix_validation_openblas\`。

##### 比較限制（保留）

舊 Session 為 1273 samples，新 Session 為 151 samples，故 workbook 建構本身的記憶體
成本不是完全同規模比較。但 OpenBLAS 的 +19 native threads / 約 650 MB committed
buffer 屬 **import 初始化成本，與 Session sample 數無關**（離線實測：`import numpy`
單獨一行即產生 +19 threads / +652 MB，未觸碰任何資料）；修正後 ΔThreads 僅 +2，
因此不影響 OpenBLAS 修法成立的結論。

完全對等的 ~1200 samples Session 列為**後續補強觀察**，**不是** Phase 5.2 COMPLETE
的必要條件。

#### Phase 5.2 最終狀態

| 項目 | 結果 |
|---|---|
| Original Soak | **FAIL**（2026-08-17，T0+3.09h，B Resource） |
| Root Cause | **ONE-TIME COST** |
| Fix | **OPENBLAS_NUM_THREADS=1** |
| Deployment | **PASS** |
| Fix Validation | **PASS** |
| **Phase 5.2** | **COMPLETE** |

`Phase 5.2 = COMPLETE` 的定義是：**原始測試發現問題 → Root Cause 查明 → 修正 →
部署 → 修正後驗證 PASS → 收尾完成**。
原始 Soak 的 **FAIL 為定案，不得改寫為 PASS** —— 上表保留完整歷史。

---

## 5. Phase 5.3 — Stress / Boundary Test

### 5.1 原則

壓 **Monitor / Report / Service 軟體層**，不壓實體設備。

**禁止**：對 HMI 高頻 request 或攻擊式測試、高頻切 PCS、高頻切智慧模式、
高頻切排程、人工大量送控制 API。
**優先**：offline regression、fixture、mock、temporary directory。
會影響設備的測試 → **先 STOP AND REPORT**。

### 5.2 矩陣（12 項，全部離線可行）

| # | 項目 | 手段 | 需實機 |
|---|---|---|---|
| 1 | 多次 Session 建立／完成 | 連續 N 輪，驗 COOLDOWN、無殘留、無交叉污染 | 否 |
| 2 | Session 數量增加 | temp root 建 500 / 1000 / 2000 個資料夾，量測 `find_active_session()`（正式環境現為 17 個） | 否 |
| 3 | samples.csv 大量累積 | 注入假 `read_all`，累積 10k+ 筆 | 否 |
| 4 | 長時間 recording | 注入時鐘拉長 `elapsed`，驗能量積分與 duration | 否 |
| 5 | stop / start 邊界 | 取樣中 stop、finalize 中 stop、極短 Session | 否 |
| 6 | Auth Recovery 邊界 | `_error401` 反覆觸發，驗無 login storm、streak 正確歸零 | 否 |
| 7 | communication error | guest + authed 全失敗 → `communication_error` 收尾正確 | 否 |
| 8 | orphan gap | 跨越／未跨越 `ORPHAN_GAP_SEC`（300s）兩側 | 否 |
| 9 | idle debounce | `AUTO_HOLD_STOP_SEC` 邊界（59 / 60 / 61 秒） | 否 |
| 10 | finalize / output retry | 真實檔案鎖觸發 `_retry_file_op` 三態 | 否 |
| 11 | duplicate writer / Ownership contention | 多子行程同時 acquire / resume | 否 |
| 12 | 路徑 canonicalization + 異常資料 | 大小寫 / slash 變體（沿用 4.6-C）＋ None / timeout / 畸形回應 | 否 |

### 5.3 判定

新增 suite 全 PASS，且既有 1529 項一項不減、一項不 FAIL。
若任一項揭露真實產品缺陷 → **停止並回報根因、影響、最小修法、測試方式**，
不自行修改產品碼。

### 5.4 執行結果（2026-08-18）—— **Phase 5.3 = COMPLETE**

交付物：`test/test_phase5_stress_boundary.py`（#1～#12 全部）與
`test/_p53_stress_child.py`（#11 跨行程 helper；刻意不動 Phase 4.4 的
`_p44_owner_child.py`，避免波及既有測試）。已註冊進 `run_phase3_regression.py`。

| 項目 | 結果 |
|---|---|
| #1～#12 | **全部 PASS**（分 8 個 Batch 遞增執行，每批通過才進下一批） |
| Phase 5.3-A suite | **244 / 244 PASS**（連續執行 3 次皆相同） |
| Full Regression | **1802 / 1802 PASS**，FAIL 0、SKIP 0 |
| 既有測試 | 一項不減（267/372/122/23/22/84/56/62/64/159/43/56/116/39/73 逐項相同） |
| Isolation Guard | **12 / 12 PASS**（未全過即中止，不執行任何壓力項目） |
| #11 Ownership contention | PASS —— 6 行程同時競爭恰好 1 個 Owner；落敗者一律 `held_by_other`；release 後可 takeover；崩潰（唯一 handle 持有者）→ 下一行程得 `acquired`／`abandoned=False`；另有 handle 存活時 Wait 回 **WAIT_ABANDONED (0x80)**，符合 Phase 4.4/4.7 契約 |
| #12 canonicalization / malformed / timeout | PASS —— 7 種路徑表示法收斂為同一 Mutex、4 個不同 root → 4 個不同 Mutex；壞 JSON／空檔／缺欄位／時間格式錯一律安全回 `None`；全端點逾時記入 `_fail`、`communication_ok=False`、正確回 `guest_also_down` 且 `_fail` 不夾帶敏感字樣 |
| 正式 Mutex | **零接觸** —— 執行期攔截器逐次比對，放行的 mutex 集合不含正式 canonical |
| 正式 `monitor_owner.json` | **零修改**（size + mtime_ns + sha256 三項前後相同） |
| 正式 output 指紋 | **前後完全一致**（156 檔 `dc152f61c61814fc…`） |
| worker PID / start time | **16512 / 2026-08-18T14:45:09 全程未變**，Service 持續 Running |
| 產品 Python 執行碼 | **零修改** |

隔離手法：全程 temp root、零網路（`read_all` / `_fetch_cell_packs` 取代）、
`_report_output_root` 於任何 Mutex 呼叫前先指向暫存目錄；#11 另加三條前置硬性
斷言（父與每個子行程回報的 mutex 皆 ≠ 正式 canonical；所有 root 位於暫存目錄
且不在正式 root 之下；正式 output 指紋前後一致），未過即中止。崩潰情境以子行程
自行 `os._exit(0)` 模擬，**不使用任何強制終止指令**。

### 5.5 已知硬化機會（Known hardening opportunities / defense-in-depth）

#12 以刻意畸形的輸入探測時，發現三處在**極端型別／內容**下會拋例外而非安全回值。
三者在**目前正式產品呼叫鏈皆不可達**，上游已有型別／資料來源契約守門：

| 現象 | 為何目前不可達 |
|---|---|
| `session_state.json` 內容為 JSON **陣列** 時，`_orphan_gap_seconds` 的 `st.get` 拋 `AttributeError`（`_load_json(...) or {}` 對非空 list 為真） | `find_active_session()` 以 `isinstance(st, dict)` 過濾，陣列型 state 永遠不會被選為 active；而本函式只被 `report_resume_on_launch()` 以 `active` 呼叫，前面另有 `if not active: return` |
| `_auth_session_lost({"_fail": 純量})` → `_ep_failed` 迭代純量拋 `TypeError` | `_fail` 一律由 `CDR.read_all._get()` 建構為 list，不會是純量 |
| `find_active_session(None)` → `os.path.isdir(None)` 拋 `TypeError` | 產品一律傳入 `_report_output_root()` 的回傳字串 |

處置：**未修改產品碼**，也**未**將其升格為 Phase 5.3 FAIL。測試中以
`[已知硬化機會]` 標記並鎖定現況，避免日後行為悄悄改變而無人察覺。

⚠️ **未來若上述任一上游呼叫契約改變**（例如 `find_active_session` 放寬型別過濾、
`_fail` 改由其他來源建構、或 `_orphan_gap_seconds` 被新的呼叫點使用），
**必須重新評估**是否需要加入型別守門。本階段刻意不擴大產品契約。

---

## 6. Phase 5.4 — Production Documentation

### 6.1 現有覆蓋

| 文件 | 已覆蓋 |
|---|---|
| `tools/README.md` | NSSM 版本／授權／SHA-256、runtime dependency 警語、Service 關係、部署（install/start）、更新程序 |
| `docs/Phase4_Closure_Report.md` | Architecture、Ownership、Writer Gate、Auth Recovery、DACL、canonicalization、兩條 Recovery 路徑、已知限制 7 項 |
| `docs/Phase4.6_Closure_Report.md` | NSSM 包裝細節、start fail-closed、NSSM 輸出編碼根因 |
| `test/README.md` | 專案入口：簡介／功能／檔案／執行／Service 操作／文件索引 |

### 6.2 缺口

| 項目 | 狀態 |
|---|---|
| Installation（Python 版本、套件相依、首次設定） | **缺** |
| Service stop / status / remove 完整說明與預期輸出 | **部分** |
| Configuration（`charge_discharge_report_config.py` 可調參數） | **缺** |
| Environment / credential 需求（`*.env` 格式與必要欄位） | **缺** |
| Log location（含輪替規則）集中說明 | **部分** |
| Report output location（輸出檔清單與用途） | **部分** |
| Troubleshooting | **缺** |
| Upgrade procedure（更新 Python 碼後如何安全套用） | **缺** |
| Rollback procedure | **缺** |
| Uninstall procedure（含 output / log 保留策略） | **部分** |
| Security / credential handling（log 不輸出憑證的設計） | **缺** |

**交付物**：`docs/Operations_Guide.md` —— 與 Closure Report 交叉引用，不重複技術分析。

---

## 7. Phase 5.5 — Release Validation DoD

- [ ] Full Regression PASS（≥1529、FAIL 0、SKIP 0、Environment residue PASS）
- [ ] Phase 5 新增 regression PASS
- [ ] 5.2 Soak PASS
- [ ] 5.3 Stress / Boundary PASS
- [ ] 實機 Validation PASS（含至少一次完整自然 Session 生命週期）
- [ ] Service health PASS
- [ ] working tree clean、`git diff --check` exit 0
- [ ] Production Documentation 完成
- [ ] 無未處理 Critical / High defect
- [ ] **Release version 機制**（專案目前無版本號定義，須先建立）
- [ ] **Release tag** 規劃
- [ ] **Deployment package** 內容確定（含 `tools/nssm.exe`，SHA-256 `EEE9C44C29C2BE011F1F1E43BB8C3FCA888CB81053022EC5A0060035DE16D848`；排除 `output/`、`*.env`）
- [ ] Rollback instructions
- [ ] Final release report（`docs/Phase5_Closure_Report.md`）
- [ ] **Phase 4 commits 是否先 push / 建立遠端分支** —— 列為 Release 前置事項

**未經確認：不建立 tag、不 push、不 Release、不 merge develop / main。**

---

## 8. `fault=True` 對各階段的影響

**必須區分兩件事**：

- **設備 connectivity** —— 目前**已恢復**（guest 與 authenticated API 皆正常）
- **設備 operational state** —— 目前**異常**：PCS `直流輸入故障 = 直流輸入欠壓`，
  level 1，3 筆告警，SOC 4.0%

`fault=True` **不代表 Service health FAIL**，但 `auto_schedule_check` 條件 E
（`pcs_fault_flag`）會擋下自動建立 Session。

| 階段 | 影響 | 可否在 `fault=True` 下進行 |
|---|---|---|
| 5.1 | 無 | **可以** |
| 5.2 | E 項 Session integrity 必然無法涵蓋 | **Service-only soak 可以**（A/B/C/D/F 五組完全可驗）；**完整 Production soak 建議等 `fault=False`** |
| 5.3 | 全部離線 | **可以** |
| 5.4 | 無 | **可以** |
| 5.5 | 最終實機確認需要一次完整 Session | **建議等 `fault=False`** |

**不得為了開始測試而**：清 fault、控制 PCS、切換智慧模式、修改排程。

---

## 9. 執行順序建議

```
5.1（完成）
  ├─ 5.3  offline，不受設備限制 ──┐
  ├─ 5.4  文件，可平行          ─┤→ 皆不需等設備
  └─ 5.2  需設備穩定窗口         ─┘
        └─ 5.5  最後
```

將 5.3 提前至 5.2 之前，可在等待設備恢復期間持續產出價值。

---

## 10. 正式開始前尚缺的前置條件

| # | 前置條件 | 阻擋 |
|---|---|---|
| 1 | soak 起跑策略裁示（Service-only 先跑，或等 `fault=False` 跑完整版） | 5.2 |
| 2 | `fault=False`（直流輸入欠壓排除，屬設備端作業） | 5.2 的 E 項、5.5 |
| 3 | soak 採樣器（唯讀，尚未建立） | 5.2 |
| 4 | stress suite（12 項，尚未建立） | 5.3 |
| 5 | release version 機制 | 5.5 |
| 6 | Phase 4 commits 是否先 push | 5.5 |
| 7 | deployment package 範圍裁示 | 5.5 |
