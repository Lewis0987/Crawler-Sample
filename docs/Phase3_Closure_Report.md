# Phase 3 結案報告
### 智慧排程自動建立充放電報告（Auto Schedule Report）

**判定** 結案（PASS） ｜ **日期** 2026-08-10

---

## Executive Summary

**Auto Start** — PCS 進入智慧排程充放電時自動建立 Report Session

- 語言無關的模式／排程／方向判定，不依賴中文字串比對
- Start Debounce（15s）避免旗標抖動造成誤建立
- 程序級鎖保證 Session 全程唯一

**Auto Stop** — 排程結束時自動偵測並停止

- `should_auto_end()` 為唯一停止決策點，採白名單條件
- 語言無關 idle／standby 旗標判定
- Stop Debounce（60s）＋ Cooldown（60s）

**Auto Finalize** — 自動產出完整報告

- Summary／statistics／alarms／Excel／Cell 快照全自動產生
- finalize 三階段拆分，網路階段與檔案階段分離
- 輸出失敗有限次重試，並記錄 `output_status`

**Recovery & Reliability** — 異常情況下不遺失資料

- Resume：續接未完成 Session，累積值不中斷
- atexit 保底：行程結束自動標記 `paused`，孤兒來源已堵住
- 孤兒分類：斷線間隔超過門檻即記錄，但不改變處置流程
- `_CLIENT_LOCK` 縮小至網路階段，Excel 產出不再阻塞前景

**Validation & Regression** — 可重複驗證

- Regression Runner：一鍵重跑、總表輸出、exit code 判定
- Windows 真實檔案鎖驗證（OS 層級，非注入例外）
- Dashboard 迴圈 harness（前景接線驗證）
- 實機唯讀探針（排程 Context 驗證工具）

---

## 1. Phase 一覽

| Phase | 功能 | 完成 | 實機 | Regression |
|---|---|:---:|:---:|:---:|
| **Phase 1** | `pcs_fault_flag` 語言無關故障判定 | ✅ | ✅ | 7 |
| **Phase 2** | Auto Start（自動建立報告，監看層） | ✅ | ✅ | 207 |
| **Phase 3** | **自動停止與收尾** | | | |
| 　3.1 | 語言無關 idle／standby 旗標（`pcs_is_idle_state`） | ✅ | ✅ | 12 |
| 　3.2 | `should_auto_end()` 單一停止決策點（白名單） | ✅ | ✅ | 24 |
| 　3.3 | Auto Stop／Auto Finalize（背景迴圈接線） | ✅ | ✅ | 86 |
| 　3.4 | Start Debounce／Cooldown | ✅ | ✅ | 46 |
| 　3.5 | Schedule Context（排程窗口感知） | ✅ | ⚠️ 核心 ✅ | 57 |
| 　3.6 | Resume／atexit／孤兒分類／共用整併 | ✅ | ✅ | 33 |
| 　3.7 | Finalize 三階段／鎖範圍縮小／輸出重試 | ✅ | ✅ | 95 |
| 　3.8 | 全面 Regression／驗證工具入庫／Runner | ✅ | ✅ | 全數 |

> Regression 欄為該 Phase 直接對應的檢查數。總數 734 另含未歸屬特定 Phase 的既有基礎回歸
> （Excel／圖表／Cell／告警／resume 等）。
>
> **Phase 3.1 為「語言無關 idle／standby 旗標」**，Resume／atexit 屬 Phase 3.6。

---

## 2. Regression 結果

### 2.1 Offline Regression — **734 / 734 PASS**

```
py_compile                               PASS   （8 檔）
report selftest                 267/267  PASS
auto schedule                   318/318  PASS
3-B selftest                    122/122  PASS
Dashboard harness                   5/5  PASS
Windows file lock                 22/22  PASS
Environment residue                      PASS
--------------------------------------------------------------
Automated checks                734/734  PASS
FAIL  0     SKIP  0     RESULT  PASS     exit code  0
```

重跑指令：`python run_phase3_regression.py`（**不需要設備**）

### 2.2 Real Device Validation — **PASS**

| 日期 | 項目 | 證據來源 |
|---|---|---|
| 08-04 | 排程起充旗標轉換 | 3-B 實測 |
| 08-06 | Auto Stop → finalize | console |
| 08-06 | atexit `recording` → `paused` | `session_state.json` |
| 08-07 | Start Debounce（方向持續 16s ≥ 15s） | `events.csv` |
| 08-07 | Cooldown（`cooldown=49s/60s`，未建第二份） | console ＋ 資料夾清點 |
| 08-07 | Stop Debounce（idle 持續 62s ≥ 60s） | `events.csv` |
| 08-07 | `finalize_ok` ／ `output_status` | `events.csv` ＋ `session_state.json` |
| 08-07 | Excel Summary 控制模式＝智慧模式 | `report.xlsx` |

**Offline Regression 與 Real Device Validation 均已完成，兩者結果一致。**

### 2.3 Read-only Probe — **PASS**

```
執行時間  2026-08-10 09:45:57              exit code 0

登入 HMI                        ✅
讀取設備狀態                     ✅  13 欄位 + 3 衍生判定
讀取排程 context                 ✅  ok=True，2 筆
不輸出帳號                       ✅  含帳號的登入訊息已濾除
不建立 Session                   ✅  Session 資料夾數 11 → 11
不修改排程 / 不送控制命令          ✅  遞移呼叫路徑靜態掃描 0 命中
active session 前後不變           ✅  None → None
輸出目錄檔案指紋                  ✅  177 檔，零新增 / 零刪除 / 零異動
```

> **分區理由**：三者性質不同 —— Offline 是軟體品質基準（必須全過）、Device Validation 是行為
> 證據、Probe 是工具驗證。設備不可達不應計為軟體 Regression 失敗；未來新增測試時只需調整
> 2.1 的數字。

---

## 3. Known Remaining Items

### 3.1 Dashboard `window=` 顯示證據

- **Status**：Pending
- **Reason**：僅缺 Console 顯示證據，不影響功能。核心（`fetch_enabled_plans` ／
  `compute_schedule_ctx`）已兩次實機驗證，顯示函式 `_schedule_ctx_text()` 有 5 項離線覆蓋。
- **理論重建值**：`window=in(Charge 16:35~16:45 charge)` ——【重建計算，非實際 Console 證據】
- **補齊方式**：下一次自然排程 idle 倒數時抓一行即可，無需重跑完整 3.5 驗證。

### 3.2 `run_phase3_regression.py --with-device`

- **Status**：Not Executed
- **Reason**：整合路徑未授權執行。
- **已驗證**：探針本身連真實設備正常；runner 對 rc=0／3／非 0 的判定邏輯（單元 6/6）；
  runner 端到端偵測失敗與 exit code（負向測試）。
- **未驗證**：「runner 呼叫探針」這一段串接。

### 3.3 Phase 3.4／3.5 未測「窗口起點 idle→charge」情境

- **Status**：Accepted
- **Reason**：實測到的是更嚴格的情境（設備已在充電中才啟動 Dashboard，Start Debounce 仍照常
  等滿 15s），風險低於已測情境。

### 3.4 Accepted — 刻意不做（設計決定，非缺口）

| 項目 | 理由 |
|---|---|
| `ORPHAN_AUTO_FINALIZE` | 孤兒不自動 finalize，待累積實機案例再評估 |
| Exit finalize／Resume 自動 finalize | Phase 3.6 只堵孤兒來源，不改處置方式 |
| 正式補產入口 | Retry 耗盡後以既有 `--regen` 人工補產 |
| `summary.json` 不含 `output_status` | 避免變更 Summary schema |
| `report_monitor.py` 抽離 | 屬技術債 D-6 |

### 3.5 技術債（已登記，未處理）

- **D-2** 排程 API 整併
- **D-3** `auto_schedule_check()` 拆分
- **D-6** `report_monitor.py` 抽離

---

## 4. Phase 4

Phase 4 不屬於本次結案範圍，待 Phase 3 Commit 完成後，再依新需求另行規劃。

---

## Conclusion

Phase 3 已完成全部既定功能。

本階段完成：

- **Offline Regression**：734 / 734 PASS
- **Real Device Validation**：PASS
- **Read-only Probe**：PASS
- **Regression Runner**：PASS

目前 Known Remaining Items 均屬已知保留事項或後續改善項目，不影響 Phase 3 功能完整性。

**建議正式結案。**

---

## Appendix A — 交付檔案

| 檔案 | 用途 | 需設備 | 納入預設 Regression |
|---|---|:---:|:---:|
| `run_phase3_regression.py` | Regression Runner（純協調器） | ✘ | — |
| `test_phase3_file_lock.py` | Windows 真實檔案鎖驗證（22 項） | ✘ | ✔ |
| `test_dashboard_loop.py` | Dashboard 迴圈 harness（5 項） | ✘ | ✔ |
| `probe_schedule_context.py` | 排程 Context 實機唯讀探針 | ✔ | ✘（需 `--with-device`） |

## Appendix B — 範圍註記

> `test/test_dashboard_loop.py` —— 為滿足已核准的 Regression Runner Dashboard harness 規格
> 而補入正式測試檔；屬於範圍補充。原先明示核准範圍僅含 `test_phase3_file_lock.py` 與
> `probe_schedule_context.py`。
