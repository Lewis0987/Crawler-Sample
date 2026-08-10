# Phase 3.3 驗證報告（Validation Report）

> **狀態：結案**
> 建立日期：2026-08-07
> 範圍：Auto Stop 接線、Summary Mapping 修正、`AUTO_REFRESH_SEC` 還原
> 前置：Phase 2（Auto Start）已於 2026-08-04 完成實機驗證

---

## 0. 一句話結論

**排程結束後報告可自動收尾並產出完整輸出，且不會被充放電途中的短暫 idle 誤切成多份。**

本階段最重要的成果**不是** Auto Stop 本身（該路徑離線 E2E 已完整覆蓋），而是**實機證明 idle debounce 在真實設備的旗標抖動下有效** —— 這是離線測試無法驗證、只能靠實機取得的結論。

---

## 1. 修正項目

### 1.1 Auto Stop 接線（3.3 主體）

| 項目 | 內容 |
|---|---|
| 檔案 | `test/device_control_menu.py` |
| 變更 | 背景取樣迴圈 `_report_bg_start._loop()` 的停止判定，全部委派 `sess.should_auto_end()` |
| 移除 | Menu 內自行判斷 `fault_stop` / `communication_error` 的重複邏輯 |
| 新增函式 | `_last_active_direction()`、`_sync_stop_pending()`、`_auto_finalize_from_loop()` |

**停止決策唯一入口**：`ReportSession.should_auto_end()`（`charge_discharge_report.py`）。
Menu / Dashboard / 背景取樣器一律不得自行判斷停止 —— 已加原始碼守門測試。

**為何由背景取樣器負責停止**（而非 Dashboard 監看層）：

- 週期較短（5 秒 vs 15 秒）
- 使用者進入控制選單期間仍持續運作 —— 否則排程結束時若正在控制選單就永遠不會收尾
- 不新增執行緒（該執行緒本來就存在）

### 1.2 `_report_finalize()` 例外安全修正

原本 `finalized` 旗標在呼叫 `sess.finalize()` **之前**就設為 `True`，若 finalize 拋例外（例如 Excel 被開啟鎖住），旗標已鎖死，之後用 Menu 19 重試會被 idempotent 檢查擋掉，**整份 Session 永遠無法補產報告**。

修正：例外時還原 `finalized = False`，並保留 `_report["session"]`，讓 Menu 19 可重試。

自動收尾是無人值守的，此修正為必要的例外安全，非功能新增。

### 1.3 提示去重集中化

背景取樣器（5s）與 Dashboard 監看層（15s）原本共用 `_auto["last_note"]`，兩者交替覆寫導致去重失效，同一條件會反覆洗版（實測 6 輪印 2 次，應為 1 次）。

| 變更 | 內容 |
|---|---|
| 新增 key | `_auto["last_stop_note"]`，與 `last_note` / `last_status` 三者完全獨立 |
| 新增函式 | `_auto_clear_notes()` —— 統一清除全部去重 key |
| 集中化 | 6 條 reset 路徑全部改呼叫該函式：`_auto_finalize_from_loop` / `_stop_and_finalize` / `report_pause` / `_monitor_client` / `_report_bg_start` / `auto_schedule_check` |

全檔僅 `_auto_clear_notes()` 內指派這些 key（已加守門測試），未來新增去重 key 只需改一處。

### 1.4 Dashboard 顯示強化

`_monitor_status_line()` 新增四項顯示欄位：

```
[17:52:20] [AUTO] mode=smart sched=True chg=False dis=False run=False stby=False
           fault=False(否) dir=idle state=STOP_PENDING session=20260806_173504_auto
           src=auto held=0s idle=59s/60s｜SOC 4.0% P -1.3kW
```

- `run=` / `stby=`：`systemOnOrOffStatus` / `systemStandbyStatus` 的 `oldValue`，讓現場直接看見排程結束的旗標轉換
- `idle=XXs/60s`：收尾倒數，讓現場明確知道「只是等待收尾，不是卡住」
- `state=STOP_PENDING`：idle 累積中的狀態

**皆為顯示強化，不參與任何判定**（已加守門測試）。

### 1.5 Summary Mapping 修正（bug fix）

| 欄位 | 修正前 | 修正後 | 來源 |
|---|---|---|---|
| 控制模式 | 交流有功 ❌ | **智慧模式** ✅ | `summary.session.pcs_control_mode` |
| PCS模式 | 交流有功 ✅ | 交流有功 ✅ | `summary.session.pcs_mode` |

**根因**：Excel Summary 兩列都間接讀到 `pcs_power_control_mode`，語意重複。
`sess["control_mode"]` 實際是 `control_mode_label`，語意為「功率控制方式」（同時是 `CFG.MODE_SETPOINT` 的查表 key），**不是** PCS 控制模式。

**修正位置**：`charge_discharge_report.py` Summary 分頁 1 行

```python
r = _kv(ws, r, 1, "控制模式", sess.get("pcs_control_mode") or "未知")
```

用 `.get()` 以相容舊報告（缺欄位顯示「未知」而非 KeyError）。

**未動**：`ReportSession` / `session_state.json` / `summary.json` 欄位 / CSV schema / `SAMPLE_FIELDS` / `EVENT_FIELDS` / `MODE_SETPOINT` / `SCHEMA_VERSION`。
`control_mode` 欄位保留於 JSON（向下相容），未新增 `power_control_mode` alias。

> 正確值「智慧模式」其實**早就正確擷取並存進 `summary.json` 的 `session.pcs_control_mode`**，只是 Excel 那一列取錯欄位。屬顯示層 Mapping 錯誤，非資料寫入錯誤。

### 1.6 `AUTO_REFRESH_SEC` 還原

`device_control_menu.py` 的 `AUTO_REFRESH_SEC` 曾被改為 `60`，已還原為設計值 `15`，並加註設計原因：

```python
# ⚠️ Auto Start 目前依賴本刷新間隔：排程開始後最久要等這麼久才會建立 Session。
#    設太大會漏掉排程開頭的資料（曾誤設 60s）。維持 15s。
AUTO_REFRESH_SEC = 15
```

Auto Start 目前依賴 Dashboard 前景刷新，`60` 會讓排程開始後最多 60 秒才建立 Session，漏掉開頭資料。Auto Stop 不受影響（走背景取樣器 5 秒）。

---

## 2. 設計變更

### 2.1 停止決策流程

```
背景取樣器 _loop()（每 5 秒）
  ├─ sess.sample_once()                    取樣（例外不中斷迴圈）
  ├─ end, reason = sess.should_auto_end()  ★唯一停止決策點（例外→視為不結束，繼續記錄）
  ├─ _sync_stop_pending(sess)              僅反映 STOP_PENDING 到 _auto，不參與判定
  └─ if end:
        if _report["pending_end_reason"]:  Menu 7 人工停止優先，不被 auto_stop 覆蓋
            reason = pending_end_reason
        if not AUTO_STOP_ENABLED:          開關關閉 → 只提示一次（last_stop_note），不收尾
            continue
        _auto_finalize_from_loop(sess, reason)
            ├─ _report["stopping"] = True  收尾期間擋掉任何新 Session（條件 F）
            ├─ log_event(auto_charge_stop / auto_discharge_stop)   僅 auto_stop 時
            ├─ _report_finalize(reason)    ★既有唯一 finalize 入口（idempotent）
            │     └─ sess.finalize(reason) → Summary / statistics / alarms / report.xlsx
            └─ 成功 → 清空 _report / _auto，回 IDLE
               失敗 → 保留 Session 供 Menu 19 重試
```

**背景執行緒不可呼叫 `_stop_and_finalize()`** —— 那會 join 自己所在的執行緒造成死鎖。已加守門測試。

### 2.2 結束原因優先序

```
1. communication_error   （AUTO_END_STOP_REASONS[0]）
2. fault_stop            （pcs_fault）
3. battery_off
4. auto_stop             （idle ≥ AUTO_HOLD_STOP_SEC）
5. timeout               （AUTO_END_TIMEOUT_SEC > 0 時；目前 0 = 不限）
—— alarm_stop / soc_over_max / soc_under_min 只記錄，不觸發結束
```

例外：`_report["pending_end_reason"]`（Menu 7 的 `control_stop`）優先於 `auto_stop`。

### 2.3 新增事件型別

| 事件 | 時機 | detail 範例 |
|---|---|---|
| `auto_charge_stop` | 排程結束自動收尾，最後方向為 charge | `origin=scheduler｜排程結束自動收尾｜idle 持續 61s｜最後方向 charge` |
| `auto_discharge_stop` | 同上，最後方向為 discharge | — |
| `auto_stop` | 同上，方向無法判定時 | — |

**僅新增 `event_type` 值，`EVENT_FIELDS` 與 CSV 欄位數未變（仍 5 欄）。**

### 2.4 生效中的設定值

| 常數 | 值 | 位置 | 說明 |
|---|---|---|---|
| `AUTO_SCHEDULE_REPORT_ENABLED` | `True` | config | 自動建立報告（正式產品預設） |
| `AUTO_STOP_ENABLED` | `True` | config | 自動收尾（正式產品預設） |
| `AUTO_HOLD_STOP_SEC` | **`60`** | config | idle 去抖動門檻（**實際生效值**） |
| `AUTO_END_TIMEOUT_SEC` | `0` | config | session 上限，0 = 不限 |
| `AUTO_MONITOR_DEBUG` | `True` | config | 每輪印監看 Debug |
| `AUTO_REFRESH_SEC` | `15` | menu | Dashboard 前景刷新間隔 |

---

## 3. 實機驗證時間軸

**Session**：`20260806_173504_auto`
**排程**：模板「Charge」【啟用】17:00~17:50、charge、-1 kW、Target SOC 98%（模板「DisCharge」停用）
**設備**：`192.168.128.110:8080`

### 3.1 執行前 Safety Gate（唯讀，全數通過）

```
[1] active/paused/recording Session : None（全部 completed）
[2] 電池上下電狀態                 : 已上電
[3] pcs_fault_flag                 : False
[4] pcs_control_mode_code          : smart
[5] pcs_schedule_enabled           : True（schedulePlanSwitch=1, manualModeSwitch=0）
[6] 排程配置                       : Charge【啟用】/ DisCharge（停用）
[7] 現場人員待命                   : 確認
```

### 3.2 完整時間軸

| 時間 | 事件 | 判定 |
|---|---|---|
| 17:35:04 | Dashboard 啟動 → 首次刷新偵測到充電 → **自動建立 Session**<br>`auto_charge_start  origin=scheduler｜排程配置 enabled｜刷新來源 manual` | Auto Start ✅ |
| 17:35:05 | `charge_start  P=2.5kW  source=systemChargingStatus` | — |
| **17:38:35** | `idle_start`（**短暫 idle #1**） | 觀察點 |
| **17:39:02** | `charge_start` ← 27 秒後恢復充電 | **debounce 吸收 ✅** |
| **17:44:58** | `idle_start`（**短暫 idle #2**） | 觀察點 |
| **17:45:14** | `charge_start` ← 16 秒後恢復充電 | **debounce 吸收 ✅** |
| 17:51:20 | `idle_start` ← 排程結束（設定 17:50，實際 17:51:20 旗標翻轉） | — |
| 17:52:20 | 畫面：`chg=False dis=False run=False stby=False dir=idle state=STOP_PENDING idle=59s/60s` | 旗標轉換 ✅ |
| **17:52:21** | `auto_charge_stop  origin=scheduler｜idle 持續 61s｜最後方向 charge`<br>`session_end  reason=auto_stop(自動偵測停止) samples=190`<br>**自動 finalize** | Auto Stop ✅ |

### 3.3 最重要的實機成果：debounce 有效

充電期間出現**兩次短暫 idle**（27 秒、16 秒），皆未達 `AUTO_HOLD_STOP_SEC = 60` 門檻：

```
方向分佈：charge 170 筆 / idle 20 筆   ← 全部在同一份 Session
Session 資料夾數：1                    ← 未被切成 2~3 份
```

真正停止時 idle 持續 **61 秒**觸發（門檻 60 秒、背景取樣 5 秒週期 → 第 13 次取樣，誤差 1 秒）。

**結論：`AUTO_HOLD_STOP_SEC = 60` 設定值恰當** —— 抖動間隔（16~27 秒）被吸收，真正結束（61 秒）被觸發。此為離線測試無法驗證的成果。

### 3.4 旗標轉換實測（Phase 3.1 語言無關旗標驗證）

| 狀態 | `run` | `stby` | `pcs_is_idle_state()` |
|---|---|---|---|
| 充電中 | `True` | `False` | `False` |
| 排程結束後 | `False` | `False` | **`True`** |

與離線假設完全一致，`systemOnOrOffStatus.oldValue = 0` 觸發 idle 判定，不依賴中文 badge。

### 3.5 產出結果

```
status         = completed          end_reason = auto_stop（自動偵測停止）
start → end    = 17:35:04 → 17:52:21    duration = 1037.5 s    samples = 190
SOC            = 3.0 → 4.0 %
累積充/放/淨   = 0.587 / 0.0294 / 0.5576 kWh
往返效率       = N/A（放電量僅佔 5%，低於 RTE_MIN_PHASE_RATIO=0.2；該 0.0294 為待機自耗）
結果           = PASS               資料完整 True / 建議停止 False
```

**輸出檔（8 個全部產生）**：`summary.json` / `statistics.json` / `samples.csv` / `events.csv` / `alarms.csv` / `report.xlsx` / `session_state.json` / `cell_snapshots.json`

**Excel**：9 個工作表（Summary / Charge & Discharge / Charge / Discharge / Cell Volt. / Cell Temp. / Raw Data / Alarm / _ReportMeta），圖表 Charge & Discharge ×1、Charge ×1

**資料完整性**：190 筆 `sample_index` 連續、`elapsed_seconds` 單調遞增、累積充/放電單調不減、末筆方向 idle、samples 30 欄、events 5 欄

### 3.6 Summary Mapping 修正後重產

以 `--regen` 重產 Excel（原檔自動備份為 `report_backup_20260807_091255.xlsx`）：

```
控制模式 = '智慧模式'    ← 修正成功
PCS模式  = '交流有功'    ← 維持正確
```

JSON 全部未變（mtime 仍為 8/6 17:52）。`alarms.csv` 被 `--regen` 一併刷新（既有行為），已確認內容實質未變：9 筆、14 欄、`alarm_code` 編號未變。

---

## 4. Regression 結果

| 測試 | 結果 | 備註 |
|---|---|---|
| `py_compile`（5 檔） | **PASS** | — |
| `charge_discharge_report.py --selftest` | **PASS 206/206** | 199 → 206（+7 Summary mapping 驗證） |
| `test_auto_schedule.py` | **PASS 160/160** | 86 → 160（+74，含正式流程 E2E） |
| `phase2_real_trigger_validation.py --selftest` | **PASS 122/122** | 不變 |
| Dashboard 迴圈 harness | **PASS** | — |

### 4.1 正式流程 E2E Regression

`test_auto_schedule.py` 內含一項端對端測試，走**完全真實**的路徑（真 `ReportSession` + 真 `_report_bg_start` 背景執行緒 + 真 `should_auto_end()` + 真 `_report_finalize()`），只把設備讀值與 Cell 抓取換成合成資料，並調快節奏：

```
Charge → Idle → Auto Stop → Finalize → Summary → Excel
```

驗證 16 項，包含 `status=completed`、`end_reason=auto_stop`、`auto_charge_stop` 恰 1 筆、schema 未變、只建立 1 個資料夾。

### 4.2 關鍵守門測試（原始碼層級）

```
[PASS] _loop() 內不再自行判斷 fault_stop / communication_error / battery_off / timeout
[PASS] _loop() 不呼叫 _stop_and_finalize（避免 join 自己造成死鎖）
[PASS] 全專案僅 should_auto_end() 一處做停止判定
[PASS] pending_end_reason 優先於 auto_stop
[PASS] finalize 失敗時把 finalized 還原為 False
[PASS] 清除邏輯集中：全檔只有 _auto_clear_notes() 內指派去重 key
[PASS] 所有回到 AUTO_IDLE / Session 起訖的路徑都呼叫 _auto_clear_notes()
[PASS] 顯示強化未涉入判定（_monitor_status_line 不呼叫 should_auto_end / pcs_is_idle_state）
[PASS] EVENT_FIELDS 仍 5 欄 / SAMPLE_FIELDS 仍 30 欄
```

**確認本次修正未造成任何既有功能 Regression。**

---

## 5. 已知限制

### 5.1 架構性限制（Phase 2 既有設計，非 3.3 引入）

| # | 限制 | 說明 |
|---|---|---|
| L1 | **Auto Start 依賴 Dashboard 前景刷新** | `auto_schedule_check()` 全專案唯一呼叫點在 `dashboard_refresh()`。未啟動 `device_control_menu.py` 就不會建立報告。已於 2026-08-06 實機重現：17:30 排程啟動時無人開 Dashboard → 未建立；17:35 啟動後立即建立。**→ Phase 4（Auto Monitor Service）解決** |
| L2 | Auto Start 最長延遲 `AUTO_REFRESH_SEC`（15 秒） | 排程開始後最久 15 秒才建立 Session，會漏掉開頭資料 |
| L3 | 進入控制選單期間不做 Auto Start | `_exec_menu()` 無刷新（設計如此，避免控制與偵測並行）。Auto **Stop** 不受影響 |
| L4 | 關閉程式時若仍在記錄 → 存為 `paused` | 若行程被強制結束（非正常離開）會停在 `recording` 變孤兒。**→ Phase 3.6 強化** |

### 5.2 3.3 刻意未處理（留待 Phase 3.4）

| # | 限制 | 風險 |
|---|---|---|
| L5 | **無 Start Debounce** | 旗標抖動可能誤建 Session |
| L6 | **無 COOLDOWN** | `auto_stop` 收尾後若旗標瞬間回報 charging，Dashboard 下次刷新可能立刻建立第二份報告 |

> 本次實機未觸發 L6（`auto_stop` 前提是 idle 已持續 60 秒，設備已穩定停止），但今天觀察到設備**確實會出現旗標抖動**（充電中兩次短暫 idle），反向抖動同樣可能發生。

### 5.3 既有缺陷（非 3.3 引入，未處理）

| # | 缺陷 | 影響 |
|---|---|---|
| L7 | `_report_finalize()` 以 `_CLIENT_LOCK` 包住整個 `finalize()`（含 Excel 產出，數秒） | 背景自動收尾期間，Dashboard 下一次刷新會延後數秒。不死鎖、不漏資料。**→ Phase 3.7 拆分 finalize** |
| L8 | `_force_plot_visible_all()` 遇檔案鎖定只印一行提醒 | 圖表 X 軸稀疏標籤可能失效，無人值守時不會被發現。**→ Phase 3.7 加 Retry** |
| L9 | **`AUTO_HOLD_STOP_SEC` 有兩處定義且值不同** | `device_control_menu.py:1174` = `30`（Phase 2 遺留，**未被任何程式使用**）<br>`charge_discharge_report_config.py:223` = `60`（**實際生效值**，`should_auto_end()` 讀 `CFG.`）<br>⚠️ 死常數與生效值矛盾，易誤導。**→ Phase 3.4 一併清理** |
| L10 | `charge_discharge_report.py` 有重複的 `_git_commit` 定義（第 88、270 行）、4 個未使用 import | 無功能影響 |
| L11 | `summary.json` 的 `statistics.data_complete` 為 `None`，但終端摘要顯示「資料完整：True」 | 欄位命名/位置不一致，不影響資料 |

### 5.4 語意說明（非缺陷，避免日後誤解）

`summary.session.control_mode` 儲存的是 `control_mode_label`，語意為**功率控制方式**（交流有功 / 直流恆流 / 直流恆功率），同時是 `CFG.MODE_SETPOINT` 的查表 key，用於決定 setpoint 記錄欄位。

**它不是 PCS 控制模式。** PCS 控制模式請讀 `summary.session.pcs_control_mode`。

該欄位保留於 JSON 以維持向下相容，未刪除、未改名、未新增 alias。

---

## 6. Git 影響範圍

### 6.1 Phase 3.3 涉及的檔案

| 檔案 | 變更內容 |
|---|---|
| `test/device_control_menu.py` | `_loop()` 接線 `should_auto_end()`、新增 3 個輔助函式、`_auto_clear_notes()` 集中化、`last_stop_note`、顯示欄位 `run/stby/idle`、`_report_finalize` 例外安全、`AUTO_REFRESH_SEC` 還原 |
| `test/charge_discharge_report.py` | Summary Mapping 1 行 + 註解 + 7 項 selftest 驗證 |
| `test/charge_discharge_report_config.py` | `AUTO_STOP_ENABLED = True` |
| `test/test_auto_schedule.py` | 新增 74 項驗證（含正式流程 E2E Regression） |

### 6.2 未變更

- `SCHEMA_VERSION`（維持 `1.4.0`）
- `SAMPLE_FIELDS`（30 欄）/ `EVENT_FIELDS`（5 欄）/ `ALARM_FIELDS`（14 欄）
- `session_state.json` / `summary.json` 的欄位結構
- `MODE_SETPOINT`
- `ReportSession` 的建構與 `finalize()` 流程
- Dashboard 的控制選單與所有控制 API

### 6.3 Git 狀態（截至結案時）

**本階段全程未執行任何 Git 寫入操作**（無 `add` / `commit` / `push` / `merge` / `rebase` / `reset` / `stash`）。

```
HEAD = e3f8236 實測自動排程產報告

 M test/charge_discharge_report.py
 M test/charge_discharge_report_config.py
 M test/device_control_menu.py
 M test/phase2_real_trigger_validation.py
 M test/test_auto_schedule.py
?? templates/     ESS_Report_Template.xlsx
?? tools/         make_ess_report_template.py
```

⚠️ **工作區目前混有 ESS 電表報告（Meter Report）的並行工作** —— `templates/`、`tools/`、config 的 `METER_*` 一整組設定、以及 `charge_discharge_report.py` 的相關變更。

**Phase 3.3 的變更與 ESS 電表報告的變更目前混在同一個工作區**，commit 前需要分辨。建議：

1. 先確認 ESS 電表報告工作是否已完成到可提交狀態
2. 若要分開提交，需以 `git add -p` 逐段挑選
3. 若一起提交，commit message 需同時涵蓋兩項工作

---

## 7. 結案判定

| 項目 | 結果 |
|---|---|
| Auto Start 實機驗證 | **PASS** |
| Auto Stop 實機驗證 | **PASS** |
| idle 60 秒判定 | **PASS**（實測 61 秒觸發） |
| debounce 吸收旗標抖動 | **PASS**（兩次短暫 idle 均未切分 Session） |
| Summary Mapping 修正 | **PASS** |
| `AUTO_REFRESH_SEC` 還原 | **PASS** |
| Regression | **全部 PASS，無回歸** |

**Phase 3.3 結案。** 下一階段：Phase 3.4（Start Debounce + COOLDOWN）。
