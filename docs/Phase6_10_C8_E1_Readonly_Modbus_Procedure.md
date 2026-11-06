# C8 / E-1 — 三分鐘唯讀 Modbus FC4 現場測試程序（設計稿）

> **狀態：DESIGN ONLY（2026-11-06）。未授權執行。**
>
> 🔴 本文件只描述程序。**執行 E-1 需要使用者另外明確授權**，並由使用者指定執行者與時間。
>
> 🔴 **全程禁止：** Modbus 寫入（FC5 / FC6 / FC15 / FC16 / FC23 及任何非 FC4 功能碼）、
> PCS / BESS 控制、HMI `manualControl`、Pause / Restore、`screen -X stuff`、
> 啟動 / 停止 / 重啟 `auto_control.py` 或任何服務、操作 ESSAutoMonitor、修改任何設定檔。
>
> 🔴 `FIRST_LIVE_HOLD = True`、Attempt #10 = HOLD。E-1 **不是** Attempt，不寫入 runtime index、
> 不使用 one-shot ledger、不碰 `output\` 既有歷史檔。

---

## 0. 目的與範圍

| 項目 | 內容 |
|---|---|
| 要回答的問題 | PCS **正常待機**時，`auto_control.py` 歸零迴圈讀到的原始功率（unit 1 TW、unit 2 US，FC4 input register 4–5）落在哪裡？是否在「已歸零」範圍內並留有餘裕？ |
| 用途 | ① 判定風險 R-B（歸零迴圈無法結束）是否存在；② 提供 P-9 安全餘裕**候選值**的資料；③ 判定 C8 推論檢查是否可行 |
| **不能**回答的問題 | 外部控制器是否已釋放控制權；歸零迴圈在 `auto_control.py` **運行中**是否會結束；Phase 6 執行期間多一條 Modbus 連線是否相容（見 §8 E-1b） |
| 產出 | 原始資料 JSONL、主機環境紀錄、SHA256 清單、判定摘要（PASS / INCONCLUSIVE / FAIL） |

### 0.1 `auto_control.py` 的對應事實（本機參考副本，commit `cb63093`）

> ⚠ 行號取自本機參考副本 `D:\Crawler Sample\auto_control.py`。**現場必須以 sha256 比對遠端
> `/home/etica/ems/auto_control.py`**（§2-A6），不一致時本節結論全部改為 UNVERIFIED。

| 項目 | 參考副本 | 行號 |
|---|---|---|
| Modbus 主機 / 埠 | `BESS_HOST = "192.168.70.201"`、`BESS_PORT = 502` | 17–18 |
| Unit / 功能碼 / 位址 / 數量 | `bess_client.execute(i + 1, 0x4, 0, 12)`，i = 0（TW）、1（US） | 117 |
| unit 不存在 | `regs[0] == 0` → `registers.pop(i)` | 118–120 |
| 功率位置 | `BESS_PW_START = 4`、`BESS_PW_COUNT = 2` | 19–20 |
| 解碼 | `modbus_to_ieee754(lo, hi)`：`(hi << 16) \| lo` → `struct.pack("!I")` → `struct.unpack("!f")` | 81–83 |
| 歸零迴圈 | `stop` 後每 5 秒讀一次；所有 `LastVer` unit 都「已歸零」才印 `已停止運行` 並跳出 | 557–583 |
| 「未歸零」判定 | `(p > 0 and round(p) != 0) or (p < 0 and int(p) != 0)` | 573–575 |
| 讀取失敗 | `registers.clear()` 並重建連線；迴圈中 `len(registers) == 0` → `continue`（**不寫入，也不結束**） | 121–125、563–564 |

**「已歸零」的精確範圍**（Python 3 的 `round` 採銀行家進位）：

- `p = 0.5` → `round(0.5) = 0` → 已歸零；`0.5 < p < 1.5` → `round(p) = 1` → 未歸零；
- `p = -0.999` → `int(p) = 0` → 已歸零；`p = -1.0` → `int(p) = -1` → 未歸零；
- 因此已歸零 ⇔ **−1.0 < p ≤ +0.5**（NaN 會被判為「已歸零」，因為 NaN 的比較都是 False —— E-1 品質判定必須另外排除 NaN / Inf）。

**參考副本上觀察到、與 C8 有關的行為（只記錄，不修改 `auto_control.py`）：**

1. 歸零迴圈中只要 BESS 讀取失敗，就會持續 `continue`，不寫入也不結束。此時 `start` 只會把
   `running` 設為 True，控制執行緒仍停在歸零迴圈 → restore 後 loop 不會恢復前進
   （C3 / RestoreVerifier 會判為 RESTORE_FAILED，Fail Closed）。
2. 歸零迴圈以 `for i in LastVer` 讀 `registers[i]`；若某 unit 在迴圈期間變成不存在
   （`regs[0] == 0` 被 `pop`），參考副本會出現 `KeyError`，而外層只捕捉 `queue.Empty` →
   控制執行緒可能結束。**未在現場證實**；E-1 的 `present` 紀錄可提供兩個 unit 是否穩定在線的證據。

---

## 1. 執行者、時間與授權（待決策）

| 項目 | 候選 | 備註 |
|---|---|---|
| 執行位置 | **A**（建議）：使用者在 EC015-Server（192.168.70.201）本機執行；**B**：使用者另外授權 Claude 在 Windows 本機執行 | A 不經網路設備、與 `auto_control.py` 相同路徑；B 屬於 Claude 端設備 I/O，需要明確授權 |
| 執行時間 | PCS 故障排除、上電且穩定待機之後；`auto_control.py` 已停止 | 目前 PCS 在故障狀態，**此時量到的不是正常待機值** |
| 授權方式 | 使用者以文字明確授權「執行 E-1」，並指定位置、日期 | 本文件不構成授權 |

---

## 2. 前置確認（全部唯讀；任一項不符 → 不執行 E-1）

### A. PCS 故障排除與上電（現場人員 / HMI 唯讀畫面）

| # | 確認項目 | 方法 | 通過條件 |
|---|---|---|---|
| A1 | PCS 故障已排除 | HMI 畫面 / 唯讀 API（不得按任何控制按鈕） | fault = 無、critical alarm = 0 |
| A2 | 電池單體過壓告警已排除 | 同上 | 無過壓告警 |
| A3 | PCS 已上電且為待機 | 同上 | 狀態 = STANDBY（或 STOPPED），有效功率約 0 |
| A4 | 兩個 unit 都在線 | 同上 | TW、US 皆可讀到資料 |
| A5 | 待機已穩定 | 記錄 A3 的時間 | 待機持續 ≥ 10 分鐘，期間無人工操作 |
| A6 | 遠端 `auto_control.py` 版本 | `sha256sum /home/etica/ems/auto_control.py` | 與本機參考副本一致；不一致 → §0.1 改為 UNVERIFIED，並回報 |

### B. `auto_control.py` 停止狀態（唯讀）

在 EC015-Server 以 `etica` 執行，**輸出全部保存**：

```bash
date -Is
pgrep -fa 'auto_control\.py' ; echo "rc=$?"        # 必須沒有輸出、rc=1
ps -eo pid,ppid,pgid,tpgid,tty,stat,etime,cmd --forest | grep -E 'SCREEN|python' | grep -v grep
screen -ls                                          # 只看，不 attach
```

| # | 通過條件 |
|---|---|
| B1 | `pgrep -fa 'auto_control\.py'` 沒有輸出、`rc=1` |
| B2 | process tree 中沒有任何執行 `auto_control.py` 的 python |
| B3 | 若 `auto` screen 仍存在，只記錄狀態（Attached / Detached），**不 attach、不 stuff** |
| B4 | 現場人員書面確認：本次 E-1 期間不會有人啟動 `auto_control.py`、不會操作 `device_control_menu.py` |

🔴 B 段只能證明「這台主機上沒有這個名稱的程序」，**不能證明沒有其他控制來源**（HMI 排程、其他主機）。
E-1 期間若 HMI 顯示功率或狀態有非預期變化，該段資料標記為 `CONTAMINATED`。

### C. Modbus 服務連線相容性（唯讀，先觀察再試讀）

```bash
ss -ltnp 'sport = :502'          # 誰在 listen 502（記錄程序名稱 / PID）
ss -tnp  'sport = :502'          # 目前有哪些 client 連線
```

| # | 步驟 | 通過條件 |
|---|---|---|
| C1 | 記錄 listen 502 的服務程序與現有連線數 | 能辨識服務程序；記錄 baseline 連線清單 |
| C2 | 確認是否已知該服務的連線上限 / 單一 client 限制 | 已知 → 記錄；**未知 → 標記 UNKNOWN，仍可進行 C3，但需使用者同意** |
| C3 | **單次試讀**：建立 1 條連線、對 unit 1 送 1 次 FC4（位址 0、數量 12）、立即關閉 | 2 秒內回應、12 個暫存器 |
| C4 | 試讀後 30 秒內再看 `ss -tnp 'sport = :502'` 與 HMI 畫面 | baseline 連線仍在、HMI 資料仍持續更新、沒有新告警 |
| C5 | unit 2 重複 C3、C4 | 同上 |

C3～C5 任一項不符 → **停止，不進入 §3**，保存紀錄並回報。

---

## 3. 取樣程序

| 參數 | 值 | 理由 |
|---|---|---|
| 連線 | **1 條** TCP 連線，`finally` 一定關閉 | 降低對服務的負載與連線數 |
| 功能碼 | **只允許 FC4**；程式中以常數固定，不接受參數 | 結構上不可能寫入 |
| 每輪 | unit 1、unit 2 各 1 次 `execute(unit, 4, 0, 12)` | 與 `auto_control.py` 相同 |
| 週期 | 5 秒 | 與歸零迴圈相同 |
| 長度 | 36 輪（約 3 分鐘），另設**硬上限 200 秒** | 超時一律停止 |
| 逾時 | 每次請求 2 秒 | 與既有草案相同 |
| 期間監看 | 每 6 輪重新執行一次 `pgrep -f 'auto_control\.py'` | 出現 → 立即停止（§6） |

### 3.1 每筆紀錄的欄位（JSONL，一行一筆）

```json
{"schema": "PHASE6_E1_SAMPLE_V1", "round": 7, "unit": 1,
 "wall": "2026-11-xxT01:23:45.123+08:00", "mono": 12345.678,
 "latency_ms": 18.4, "ok": true, "error": null,
 "regs": [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
 "present": 1, "power_raw": -0.123, "power_finite": true,
 "converged_rule": true, "status_reg": 0, "mode_reg": 0, "soc_reg": 0}
```

- `regs` 保存**全部 12 個原始暫存器**（不只解碼後的值），以便日後重新解碼；
- `wall` 必須含時區（Asia/Taipei，+08:00），`mono` 為單調時鐘；
- `converged_rule` = 參考副本第 573–575 行運算式取反，**只是紀錄**；判定見 §4。

### 3.2 主機環境紀錄（`host.txt`）

開始前與結束後各記錄一次：`date -Is`、`timedatectl`、`hostname`、`whoami`、
§2-B 的三條指令、§2-C 的兩條 `ss` 指令、執行腳本自身的 `sha256sum`。

### 3.3 腳本要求（草案，未撰寫成可執行檔）

> 本輪只規定要求，**不建立可執行腳本**。實際腳本需另外審閱後才可使用。

1. 獨立撰寫，**不得 `import auto_control`**（import 時就會建立連線、啟動控制執行緒、進入 `input()`）；
2. 只使用 `modbus_tk` 的 `TcpMaster.execute`，功能碼常數 `4`，原始碼中不得出現其他功能碼或 `write`；
3. 開始前先執行 §2-B 檢查，不符就結束（exit ≠ 0），不建立 Modbus 連線；
4. 連續 3 次錯誤（任一 unit）→ 停止；
5. 每筆紀錄寫入後 `flush` + `fsync`；
6. 結束時（含例外、Ctrl+C）一律關閉連線並寫入 `END` 紀錄（含停止原因）。

---

## 4. 品質判定

### 4.1 單筆有效條件（全部成立）

| 條件 | 說明 |
|---|---|
| `ok == true`，回傳恰好 12 個暫存器 | 通訊成功 |
| `present != 0` | unit 在線 |
| `power_finite == true` | 排除 NaN / Inf（NaN 在參考副本的判定中會被當成「已歸零」，必須排除） |
| `abs(power_raw) < 500` | 合理範圍（排除解碼錯誤；上限為候選值，可調） |
| `latency_ms < 2000` | 未逾時 |
| `wall` 與 `mono` 皆單調遞增 | 時鐘沒有倒退 |

### 4.2 整體判定

| 結果 | 條件 |
|---|---|
| **PASS（資料可用）** | 每個 unit 有效筆數 ≥ 34 / 36；最大相鄰間隔 ≤ 15 秒；兩個 unit 全程在線；§2 前置條件全部成立；期間無 CONTAMINATED |
| **INCONCLUSIVE** | 任一條 PASS 條件不成立，但沒有 FAIL 事件 → 資料**不得**用於決定 P-9 |
| **FAIL** | 試讀或取樣期間 Modbus 服務異常（baseline 連線中斷、HMI 停止更新、新告警）；或期間偵測到 `auto_control.py` |

### 4.3 對 R-B 的解讀（只在 PASS 時進行）

| 觀察 | 解讀 |
|---|---|
| 兩個 unit 的全部有效樣本都在 −1.0 < p ≤ +0.5，且離邊界有距離 | 正常待機時，歸零迴圈**應該**在一個週期內結束（推論） |
| 任一樣本落在範圍外，或貼近邊界 | **R-B 成立**：待機時歸零迴圈可能無法結束 → C8 推論檢查不可行，需另行決策 |

🔴 這是 **auto_control.py 停止時**的待機基準值，**不是**外部控制器釋放控制權的證據。

---

## 5. 原始資料與 SHA256 保存

| 項目 | 規則 |
|---|---|
| 目錄 | Windows：`D:\Crawler Sample\output\phase6_field_evidence\E1_<YYYYMMDD_HHMMSS>\`（**新目錄**，不寫入既有歷史檔，也不在 C4 掃描的 journal 目錄內） |
| 檔案 | `samples.jsonl`、`host_before.txt`、`host_after.txt`、`probe_script.py`（實際使用的腳本）、`summary.json`、`SHA256SUMS` |
| 產生 SHA256 | 在**取樣主機**上結束後立即執行 `sha256sum * > SHA256SUMS` |
| 傳輸 | 複製到 Windows 後，以 `Get-FileHash -Algorithm SHA256` 逐檔比對 `SHA256SUMS`；不一致 → 整組標記 `INTEGRITY_FAIL`，不使用 |
| 不可修改 | 原始檔只讀；分析結果另存 `analysis_*.json`，不得回寫 `samples.jsonl` |
| 版本控制 | 是否把 `SHA256SUMS` 與 `summary.json` 納入 Git（類似 `phase6_attempt_history.json`）—— **待決策** |

---

## 6. Fail Closed（異常、中斷、資料不足）

| 事件 | 處置 | E-1 結果 |
|---|---|---|
| §2 任一前置條件不符 | 不建立 Modbus 連線 | NOT_RUN |
| 試讀逾時 / 錯誤 / 服務異常 | 停止，保存紀錄 | FAIL |
| 取樣期間偵測到 `auto_control.py` | 立即關閉連線、停止 | FAIL（CONTAMINATED） |
| 連續 3 次錯誤 | 停止 | INCONCLUSIVE（或 FAIL，若服務異常） |
| 單一 unit `present == 0` | 繼續取樣但標記 | INCONCLUSIVE |
| 超過 200 秒硬上限 | 停止 | 依已取得資料判定，不足即 INCONCLUSIVE |
| 人工中斷（Ctrl+C） | `finally` 關閉連線、寫入 END | INCONCLUSIVE |
| HMI 顯示非預期功率 / 狀態變化 | 記錄時間範圍 | 該段 CONTAMINATED；全程受影響 → INCONCLUSIVE |
| SHA256 比對失敗 | 不使用 | INTEGRITY_FAIL |

**任何非 PASS 結果：** 不決定 P-9、不實作 C8 推論檢查、R-B 視為**未排除**。
不得以重複執行直到 PASS 為止的方式挑選資料；重跑需記錄所有次數與結果。

---

## 7. P-9 安全餘裕（只列候選，**不核准**）

P-9 有兩個子題：(a) 推論檢查的功率門檻；(b) Phase 6 執行期間是否允許 FC4 唯讀讀取。

### 7.1 功率門檻候選

| 候選 | 門檻 | 說明 |
|---|---|---|
| P9-A | −0.8 < p < +0.3（兩個 unit 都要符合） | 先前提出的初步值；距 auto_control 邊界各留 0.2 kW |
| P9-B | −0.5 < p < +0.2 | 較保守；若 E-1 待機值很接近 0 才可行 |
| P9-C | 由 E-1 資料推導：〔待機最小值 − m, 待機最大值 + m〕∩（−0.8, +0.3） | m 為量測抖動的倍數（例如 3 × 標準差）；需要 PASS 資料 |

**共同要求（候選）：** ≥ 4 筆、跨度 ≥ 15 秒（≥ 3 個外部週期）、兩個 unit 全部在範圍內、
任何一筆讀取失敗或 NaN → 判為 **UNKNOWN**（不是通過）。

**選擇原則：** 若 E-1 待機值本身就落在候選門檻外或貼近邊界，候選門檻無效，不得為了通過而放寬。

### 7.2 執行期間 FC4 讀取（P-9b）

| 候選 | 內容 | 風險 |
|---|---|---|
| P9b-1 | 允許，只在 PauseVerifier 期間、單一連線、5 秒週期 | 與運行中的 `auto_control.py` **同時**連線 —— E-1 無法證明相容（§8） |
| P9b-2 | 不允許，C8 只作為離線分析 | C8 無法作為 Attempt 的把關條件 |

---

## 8. 已知限制與後續證據

1. **E-1 不能證明同時連線相容**：E-1 在 `auto_control.py` 停止時執行；Phase 6 執行期間
   `auto_control.py` 仍在運行（暫停中）。若 P-9b 選擇允許，需要另一項證據
   **E-1b**（`auto_control.py` 運行中的唯讀並行讀取）—— 涉及運行中的控制器，**需另外決策**。
2. PCS 故障期間量到的值不能代表正常待機；故障排除後才執行。
3. 「已進入歸零範圍」是我方取樣時間點的直接觀測；「外部控制器已跳出歸零迴圈」只是**推論**；
   「控制權已釋放」**無法由 E-1 或 C8 證明**。C8 的欄位名稱固定為
   `external_zeroing_converged_inferred`，不得出現 `released`。
4. 取樣只代表該時段、該設備狀態；季節 / SOC / 溫度不同時可能不同。

---

## 9. C8 後續（E-1 PASS 且使用者核准 P-9 之後，另外授權）

| 步驟 | 內容 |
|---|---|
| C8-4 | provider 新增唯讀 `read_external_zeroing_state()`：只用 FC4、unit 1 / 2、與 §0.1 相同解碼；任何錯誤回 UNKNOWN |
| C8-5 | `external_zeroing_converged_inferred`：≥ 4 筆、跨度 ≥ 15 秒、兩個 unit 都在 P-9 門檻內；作為暫停後把關的**必要條件**，不是充分條件 |
| C8-6 | 離線測試：邊界值、NaN、unit 不在線、讀取失敗、跨度不足、AST 驗證沒有寫入功能碼；完整 regression |

---

## 10. 待決策事項

1. E-1 由誰、在哪裡執行（§1 A / B）。
2. §2-C2 Modbus 服務連線上限未知時，是否仍同意進行單次試讀。
3. §4.1 合理範圍上限（500 kW）與 §4.2 PASS 門檻（34 / 36、間隔 15 秒）。
4. §5 證據是否納入 Git。
5. P-9a 門檻候選（P9-A / B / C），**等 E-1 資料後再決定**。
6. P-9b 執行期間是否允許 FC4 讀取，以及是否需要 E-1b。
