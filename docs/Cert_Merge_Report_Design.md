# 跨 Session 整合認證報告工具 — 正式設計規格（第一版）

## 0. 文件資訊

| 項目 | 內容 |
|---|---|
| 狀態 | **第一版設計凍結基準的正式整合文件**（使用者 2026-11-16 凍結 r7；本文件為 Phase 1 產出，供實作依據） |
| 凍結基準 | `Cert_Merge_Report_Design_DRAFT_r7.md`，SHA-256 `9ca127a8f34b939de3a6a9700d43639bee0f2d637e3a274b4717c300e5b79de0` |
| 繼承來源 | r6 `15bc28a4afcbdb2ab7e460c52bb7f2c42476d24a62c2cbaab9c407d8ec873ef4`；r5 `f11e5acf950e37163d8c10e458076ace8550a2971b1f4c0b04619c88dac9d791`；r4 `ac8dde6562bd9d23ce0f063c7a65f4d5620fb9d386bd98a998e1778bb3301cfe`；r3 `f40189100db98fd26fdacac89bbaa0d4b897f2cd9835e6331e7928b28048612e`；r2 `9aeacf71…`、r1 `b13f371e…`（r1、r2 已全部由 r3 取代） |
| 繼承規則 | 條文以**最新版為準**：r7 > r6 > r5 > r4 > r3。後版明示「取代」的條文以後版為準；後版未重述者沿用前版。本文件不新增、不變更任何凍結內容；整合時發現的編輯性衝突列於 §14，**未自行裁定** |
| 開發位置 | `D:\Crawler_1hz_r2c`（獨立 clone，remote＝DISABLED） |
| 不在範圍 | 正式版 `D:\Crawler Sample`（維持 ALL_r2c）；任何正式 Session；`auto_control.py`、`bess_request.py`（使用者刻意刪除，維持刪除、不引用、不重新引入） |
| 運作旗標 | F1 = HOLD；FIRST_LIVE_HOLD = True；X1、X2、第 2 Cycle 暫緩 |

## 1. 目的、範圍與基本限制

### 1.1 目的
把 2～6 個**已結束**的認證 Session 以唯讀方式整合成一份「跨 Session 整合認證報告」，並為每個來源 Session 產生一份「重產明細」，同時以資料值驗證比對重產明細與原始 Menu 22／25 報告。

### 1.2 已確定基準 D1～D6
| # | 基準 |
|---|---|
| D1 | 各 Session 分別記錄版本；缺少欄位 → N/A，不推算 |
| D2 | 各 Session 用自己的 `cert_state.params`；參數不同時明確標示 |
| D3 | 測試條件不一致 → 標示差異；不具可比性的指標不得合併 |
| D4 | 實際完成循環數由階段分析與操作人員標記確認；標題不以 Session 數表示循環數 |
| D5 | 第一版不做跨 Session 連續時間軸 |
| D6 | 輸出到 `D:\Crawler_1hz_cert_merged\`；不得寫回來源 Session |

### 1.3 使用者決策 Q1～Q6
| # | 決策 |
|---|---|
| Q1 | 方案 A：1 個整合報告＋每個來源 Session 1 個附屬明細（重產明細）；不修改 `cert_report_xlsx.py` 或現有產報流程；來源唯讀 |
| Q2 | 使用者提供版本（例：C1＝r2b、C2＝r2c）；標示來源與驗證狀態；不得標示為系統已驗證；不同讀取模式的不可比指標明確標示 |
| Q3 | 不新增 SOC 自動判定；SOC 100%／0% 不自動認定滿充／滿放；滿充／滿放／未滿／中止由操作人員標記；未確認 → 待確認 |
| Q4 | 一個 Session 可含多次充放電；支援多 Cycle；不以 Session 數當 Cycle 數；階段數、循環數、確認狀態分開 |
| Q5 | 保留來源快照；保留來源檔案雜湊、Session ID、版本來源、產報時間；不修改原始 Session |
| Q6 | C1 的 8 個不確定停頓暫不確認；標示待確認；不推論原因；不執行 Menu 23；不修改正式 Session |

### 1.4 第一版決定 U1～U11
| # | 決定 |
|---|---|
| U1 | P-1：跨 Session 可整合統計；**不允許跨 Session 配對** |
| U2 | P-2：DC／CD 循環順序必須明確指定 |
| U3 | P-3：第一版 none，不自動合併同方向階段 |
| U4 | rule 模式暫緩；不設合併上限 M；不核准 G1～G8 |
| U5 | file 模式暫緩 |
| U6 | P-14：`operator_split` 不得自動合併 |
| U7 | P-15：不設最短階段門檻 |
| U8 | P-16：邊界不確定時不得以人工標記補足缺失證據 |
| U9 | P-17：來源上限 6 個 Session |
| U10 | P-18：重產明細必須與原始 Menu 22 報告明確區分 |
| U11 | P-19：保留事前磁碟空間檢查；不足時停止 |

### 1.5 基本限制 R1～R10
1. 每個 Session 獨立分析，不串接不同 Session 的 1 Hz 資料。
2. 同一原始 Session 不得重複計算（前置驗證 V3）。
3. C1 的 8 個不確定停頓保留原判定（Q6）。
4. 品質指標按 Session 分開呈現，不合併 M-1。
5. 保留追溯資訊（Session ID、雜湊、版本來源、產報時間、工具版本）。
6. 來源只接受 `status = ended`；recording 一律拒絕。
7. 只讀快照；所有輸出寫在獨立資料夾。
8. 不建立 `CertRuntime`；不呼叫會修改 Session 的 `generate_report`。
9. 不連 HMI、6161、PCS、Gateway 或任何設備；不新增網路請求。
10. 第 2 Cycle 期間不執行重型測試、不更新正式版、不重啟服務。

### 1.6 實作限制
- 新增檔案只限：`docs\Cert_Merge_Report_Design.md`；`test\cert_merge_report.py`、`cert_merge_source.py`、`cert_merge_analysis.py`、`cert_merge_xlsx.py`、`cert_merge_verify.py`、`test_cert_merge_report.py`。
- 不修改任何既有模組。
- **禁止 import**：`cert_runtime`、`cert_service`、`api_client`、`report_monitor`（以及 `auto_control`、`bess_request`）。
- 禁止呼叫：`generate_report`；`RQ.SlotSegment._append`、`RQ.write_offgrid_record`、`RD.write_quality_metrics`。
- 只讀引用的既有模組：`cert_stage_engine`（`analyze`、`Obs`、`stage_names`、`stage_minutes`、`overall_minutes`、`rest_between`）；`cert_report_xlsx`（`save_report`、`is_report_valid`、`raw_measurement_valid`、`has_pcs_temp`、`RAW1_HEADERS`、`raw_second_rows` 等）；`report_quality`、`report_display`（格點品質讀取路徑）；`cert_report_config`（`params`）；`cert_screenshot`（`load_manifest`）；`cert_supplement`（`load`、`load_report_log`）；`cert_env`（`summary_lines`、`failure_note`、`SNAPSHOT_FILE`）；`cert_heat_link`（`replay`）。
- 重產明細所需的 meta 組裝（對照 `cert_runtime.generate_report`）、`build_sampling_slots`、`params_note`、`hmi_reader_note`、觀測轉換（對照 `record_to_obs`）在新模組中**以只讀方式重建**，不 import `cert_runtime`（風險 K2）。
- 第三方只用既有環境的 `openpyxl`；**不得 pip 安裝或升級**共用 Python 3.14。
- 執行時第一步安裝網路封鎖（`socket.socket.connect`、`connect_ex`、`create_connection`、`getaddrinfo` 一律丟例外）。

### 1.7 第一版功能與規則索引
| 功能 | 內容 | 章節 |
|---|---|---|
| M1 | CLI：`--source`（2～6）、`--version`、`--cycle-order`（必填）、`--out-root` | §4.1 |
| M2 | 前置驗證 V1～V9、事前空間檢查、路徑長度檢查 | §4.2、§4.3 |
| M3 | 快照、H1～H5、驗證狀態、路徑對照、唯讀屬性、停止條件 | §5 |
| M4 | 每 Session 獨立分析；原始階段 ID | §3、§6.1 |
| M5 | none 模式配對、配對確定性、確認狀態 | §6.2～§6.7 |
| M6 | 品質指標分 Session；端點失敗類可比性 | §7.4、§8 |
| M7 | 版本資訊（宣告與資料特徵分欄） | §8.2 |
| M8 | 整合活頁簿 9 頁；不含圖表；第一屏固定格式 | §9.4～§9.6 |
| M9 | 重產明細（未修改的 `save_report`） | §9.3 |
| M10 | manifest、STATUS、publish_journal、發布協定、計數不變式、網路封鎖、寫入守門 | §6.6、§11、§12 |
| M11 | 資料值驗證 | §10 |

| 規則 | 狀態 | 章節 |
|---|---|---|
| P-1 跨 Session 統計 vs 配對 | 已決定（U1） | §7.1 |
| P-2 循環順序必填 | 已決定（U2） | §4.1、§4.2 V9 |
| P-4 配對掃描（含名稱修正） | 已核准 | §6.2 |
| P-7 確認狀態（四邊界、有效標記） | 已核准 | §6.4 |
| P-8 標題、計數列、I1～I8 | 已核准 | §6.6、§9.6 |
| P-9 對應不到的紀錄 | 已核准 | §6.5 |
| P-10 ID 格式 | 已核准 | §3 |
| P-11 快照位置、路徑長度、不覆蓋 | 已核准 | §4.3、§5.1 |
| P-12 唯讀屬性（非防竄改保證） | 已核准 | §5.1 |
| P-13 不自動刪除 | 已核准 | §5.1 |
| P-20 配對確定性 A1＋A2 | 已核准 | §6.3 |
| P-21 manifest SHA-256 輸出 | 已核准 | §11.3 |
| P-22 比較項目與可比性 | 已核准 | §7.2～§7.4 |
| P-23 資料值驗證 | 已核准（有條件：Q-A、Q-B、X6） | §10 |
| P-24 離線測試（含 TA、TP、TB、TN） | 已核准設計（執行另需授權） | §15 |
| H1～H5 | 已核准（r7 順序） | §5.2、§12.1 |
| I1～I8 | 已核准 | §6.6 |
| Q-A、Q-B、X6 | 已核准 | §10.2、§10.5、§10.8 |
| D-1、D-2 | 已核准 | §9.2、§10.2 |
| U-1、U-2、U-3 | 已核准／接受 | §11.4、§12.2、§13 |
| IC-1～IC-6 | 已核准 | §12.2 |
| M-1～M-5 | 已核准 | §9.5、§11.2、§11.3、§12.1、§15.8 |

## 2. 設計依據（唯讀調查事實）
1. `CertRuntime.generate_report`（`test\cert_runtime.py:1463`）會寫 `reports\*.xlsx`／`.partial.xlsx`、`cert_state.json`、`events.jsonl`、`supplements\reports.jsonl`；`CertRuntime.__init__` 會 `makedirs(root)` → 全部不用。
2. `save_report(res, meta, path)` 只寫 `path` 與同資料夾暫存檔；存檔後以 zip 層級注入 XML 圖形 → **產出後不得以 openpyxl 重新開啟另存**（會遺失圖形）。
3. `save_report` 在描述非空時把活頁簿標題固定設為「充／放電認證測試報告」、描述截斷為 3,000 字元（`cert_report_xlsx.py:1209～1210`）。
4. `cert_report_xlsx` 內部寫死工作表名稱（`Summary`、`數據概覽`、`即時熱度`、`整體測試`、`Raw Data`）→ 每個 Session 的重產明細為獨立檔案。
5. 階段只有 `C`（充電）／`D`（放電）；待機（`I`）只構成休息區段。同方向相鄰階段只會因 `idle_confirmed`（連續待機 ≥ W＝60 秒）、`idle_over_W`、`operator_split` 產生；狀態未知時保留同一階段並列為不確定停頓（`cert_stage_engine.py:448～515`）。
6. 分析程式對同一 `stage_start` 的標記取**檔案順序最後一筆**，不檢查方向（`cert_stage_engine.py:233～240`）。
7. 休息時間 `rest_between`：後半首筆觀測運行 R − 前半結束後首筆觀測待機 B；期間有狀態未知或未觀測到待機 → 未知（`cert_stage_engine.py:787～797`）。
8. r2b 與 r2c 的 observations／slots 格式相同；r2c 新增 `cert_state.hmi_reader`（`read_cert`）、`hmi_reader_changes`、`cert_timing.reader.ep_ms`（每筆 5 個）；r2b 每秒 9 支 GET、r2c 5 支。
9. C1（`CERT_20261112_111816`，r2b 記錄）：13,026 筆觀測；slots 14,880 行（MISSED 1,852）；沒有 `decisions.jsonl`／`commands.jsonl`；沒有 `hmi_reader`、沒有 `ep_ms`；原始報告 `reports\認證測試報告_CERT_20261112_111816_01.xlsx`（描述 834 字元、7 行；工作表 7 個；Raw Data 14,879 列 × 13 欄）；資料夾 24.4 MB。

## 3. 名詞與 ID
| 名詞 | 定義 |
|---|---|
| 原始階段 | `analyze` 對單一 Session 產生的 C／D 階段；**永遠全部保留** |
| 半循環 | 第一版＝一個原始階段（none 模式，不合併、沒有群組層） |
| 循環 | 同一 Session 內，前半方向原始階段＋緊接其後的反方向原始階段 |
| 循環順序 | `DC`＝放電→充電；`CD`＝充電→放電（`--cycle-order` 必填） |
| Session 編號 | S1、S2…，依 `cert_state.started_at` 由早到晚 |

**ID（P-10，已核准）**
- 原始階段 ID：`<cert_id>#ST<Stage.no>`；第二鍵 `stage_start`（ISO 秒）。
- 循環 ID：`<cert_id>#CY<Session 內序號>`；整合報告另給全報告連號 CY1…CYn（依開始時間）。
- **階段 ID、循環 ID 只在單份報告內有效，不是永久識別碼**；跨報告比對以 `cert_id`＋`stage_start` 為準。
- 第一版沒有群組 ID。

## 4. CLI 與前置驗證

### 4.1 CLI（M1）
```
python -B -I cert_merge_report.py
    --source <Session 資料夾> --source <Session 資料夾> [...]   # 2～6 個
    --version <cert_id>=<版本> [...]                          # 使用者宣告；未提供 → 「未提供」
    --cycle-order DC|CD                                       # 必填，不推定
    --out-root D:\Crawler_1hz_cert_merged
```

### 4.2 前置驗證 V1～V9（M2；任一不過 → rc=2，不建立任何輸出）
| # | 檢查 |
|---|---|
| V1 | 來源數 2～6 |
| V2 | 資料夾存在；`cert_state.json` 可解析；`cert_id` ＝ 資料夾名稱；`status == "ended"`（recording 及其他一律拒絕）；`ended_at` 存在 |
| V3 | 重複輸入：正規化路徑（`realpath`＋`normcase`）相同、`cert_id` 相同、或 `observations.jsonl` SHA-256 相同 → 拒絕 |
| V4 | 任兩個 Session 的 `[started_at, ended_at]` 不得重疊 |
| V5 | `observations.jsonl` 首末筆時間落在 `started_at～ended_at`（容許 ±5 秒） |
| V6 | 來源不在輸出根目錄之下；輸出根目錄不在任何來源、正式版、Git repository／worktree 之下 |
| V7 | 正式版 `D:\Crawler Sample\output\cert_reports\active.json` 若指向其中一個 cert_id → 拒絕（只讀） |
| V8 | `--version` 只接受出現在來源中的 cert_id；格式錯誤 → 拒絕 |
| V9 | `--cycle-order` 必填且為 `DC` 或 `CD` |

### 4.3 事前空間與路徑檢查
- **空間（U11、P-19）**：輸出磁碟可用空間 < max(2 × 來源總大小 + 500 MB, 1 GB) → rc=2，不建立 MERGE 資料夾。
- **路徑長度（P-11、S8）**：逐檔計算快照目的地完整路徑，以及所有輸出檔最長可能完整路徑（含 `_含未驗證明細`、`_資料值未驗證`、`.partial.xlsx` 等後綴）；任一 > 240 字元 → 停止（rc=2，不建立資料夾；複製中才發現 → `FAILED`／`path_too_long`），不截短、不改名。
- `MERGE_<YYYYmmdd_HHMMSS>` 以「不存在才建立」方式建立；已存在 → rc=2，**不覆蓋、不合併**。

## 5. 快照（M3）

### 5.1 位置、範圍、保留（P-11、P-12、P-13，已核准）
| 項目 | 規格 |
|---|---|
| 位置 | `D:\Crawler_1hz_cert_merged\MERGE_<ts>\_snapshot\<cert_id>\`（每次產報自成一包） |
| 範圍 | 來源 Session 資料夾全部檔案（含 reports、screenshots、supplements），保持相對路徑 |
| 讀取來源 | 一律以 `"rb"` 開啟；不對來源使用任何會改變屬性或時間的 API |
| 唯讀屬性 | H3 通過後（F4）對快照檔設 Windows 唯讀屬性；**只用來降低誤操作，不得宣稱為防竄改或完整性保證**；設定失敗只記錄 |
| 保留 | 工具**不自動刪除**任何輸出（MERGE 資料夾、快照、`.partial`、日誌、STATUS、manifest） |
| 容量參考 | 1 Hz 認證約 6 MB／小時；兩個 4～8 小時 Session ≈ 50～100 MB／次 |

### 5.2 H1～H5 逐檔雜湊（比對檔案清單增減、大小、mtime、SHA-256）
| 時點 | 內容 | 流程位置 |
|---|---|---|
| H1 | 複製前：來源逐檔 SHA-256（清單 #1） | F3 |
| H2 | 複製後：快照逐檔 SHA-256，與 H1 比對 | F3 |
| H3 | 複製完成後立即：來源再算，與 H1 比對 | F3 |
| H5 | 所有報告寫完、雜湊計算後：快照再算，與 H2 比對 | F10 |
| H4 | H5 之後：來源再算，與 H1 比對；**來源完整性只驗證到此時點** | F11 |

### 5.3 完整路徑對照
每個來源檔一列（整合報告 `來源追溯` 頁與 manifest）：S 編號、cert_id、來源完整路徑、快照完整路徑、相對路徑、大小、mtime、SHA-256（H1／H2／H3／H4／H5）、比對結果、是否被分析讀取、用途（觀測／格點／決策／截圖／原始報告…）。

### 5.4 停止條件（不自動重試）
| # | 情況 | 處理 |
|---|---|---|
| S1 | 事前空間不足 | rc=2；不建立 MERGE 資料夾 |
| S2 | 事中磁碟滿（寫入 `OSError`） | `FAILED`（`disk_full`）；已寫內容保留；無正式檔名 |
| S3 | 快照建立失敗（讀寫錯誤、權限、鎖定、路徑過長） | `FAILED`（`snapshot_failed`／`path_too_long`，列出檔案）；不分析 |
| S4 | 快照內容不符（H2） | `SNAPSHOT_MISMATCH`；不分析 |
| S5 | 複製期間來源變動（H3） | `SOURCE_CHANGED_DURING_SNAPSHOT`；不分析 |
| S6 | 產報後來源變動（H4） | `SOURCE_CHANGED`；全部 `.partial`；不發布 |
| S7 | 來源被鎖定或讀取中斷 | 同 S3 |
| S8 | 分析或產報例外 | `FAILED`（`analysis_failed`／`write_failed`）；保留快照與已寫內容 |
| — | 產報期間快照被改（H5） | `SNAPSHOT_CHANGED`；全部 `.partial`；STATUS／manifest 標「依據的快照不可信，只供調查」 |
- 失敗時不刪除 MERGE 資料夾、不碰來源；重跑一律建立新的 MERGE 資料夾。
- **不得以不可信的快照繼續分析**（H2、H3 失敗即停止）。

## 6. 分析、配對與確認（M4、M5）

### 6.1 每 Session 獨立分析
- 只從快照載入 `cert_state.json`、`observations.jsonl`、`slots.jsonl`、`decisions.jsonl`、`commands.jsonl`、`screenshots.jsonl`（＋supplements）、`env_start_snapshot.json`；不存在的檔案 → 空清單並在報告寫「無紀錄」。
- 參數：該 Session `cert_state.params`（D2）；缺少的鍵 → 程式預設值並標示。
- 觀測轉換對照 `cert_runtime.record_to_obs`（在新模組重建）；呼叫 `cert_stage_engine.analyze(obs, params, decisions, commands)`。

### 6.2 配對掃描（P-4，已核准）
依時間掃描同一 Session 的原始階段（`--cycle-order` 指定前半方向）：
1. 前半方向階段 X，下一個階段 Y 是反方向 → (X, Y) 成為一個循環，從 Y 之後繼續。
2. 前半方向階段 X，下一個階段也是前半方向 → X＝「未配對：其後為同向階段」。
3. 後半方向階段，前面沒有可配的前半階段：若為 Session 第一個階段 → 「開頭半循環（未配對）」；否則 → 「**未配對：前無可配的前半階段**」。
4. 最後一個前半方向階段之後沒有反方向階段 → 「結尾半循環（未配對）」。
5. 每個階段最多屬於一個循環；循環數只由配對產生，與 Session 數無關；**不跨 Session 配對**（U1）。

**配對結果值**：`已配對（前半）`／`已配對（後半）`／`開頭半循環（未配對）`／`結尾半循環（未配對）`／`未配對：其後為同向階段`／`未配對：前無可配的前半階段`。

### 6.3 配對確定性（P-20，已核准：A1＋A2，不加 A3，不做假設性合併）
| # | 觸發條件（任一成立 → 該循環「循環配對待確認」） |
|---|---|
| A1 | 循環的前半階段緊接在一個同方向階段之後（中間沒有其他階段） |
| A2 | 循環的後半階段緊接著一個同方向階段（之後沒有其他階段相隔） |
- 不論相鄰同方向的成因（`idle_confirmed`／`idle_over_W`／`operator_split`）一律觸發，並照實列出成因。
- 配對確定性＝`確定`／`循環配對待確認`；與確認狀態**分兩欄**。
- 配對待確認的循環，確認狀態最高只能是「待確認」（理由加註「循環配對待確認」）。

### 6.4 確認狀態（P-7，已核准）
**四個邊界（全部為必要邊界）**
| 邊界 | 欄位 |
|---|---|
| E1 前半開始 | 前半階段 `start_certain` |
| E2 前半結束 | 前半階段 `end_certain` |
| E3 後半開始 | 後半階段 `start_certain` |
| E4 後半結束 | 後半階段 `end_certain` |
- 任一不確定 → 不得判為確認完成（U8：不得以人工標記補足）；在 `循環判定` 列出不確定的邊界與分析程式原文（`start_note`／`end_note`）。

**標記歷史與有效標記**
- 標記歷史：該 Session `decisions.jsonl` 中所有 `type = stage_label` 的紀錄，依檔案行序完整列出（`at`、`request_id`、`stage_start`、`label`、`note`）；明細列在 `不確定停頓與標記紀錄`。
- 每筆有效性：有效（`label` ∈ {滿充, 滿放, 未滿, 中止} 且方向相符）／無效：方向不符（充電標滿放、放電標滿充）／無效：未知值／無法對應（`stage_start` 對不到階段）。
- **有效標記選取**：取該階段依檔案行序的**最後一筆**；最後一筆有效 → 有效標記；最後一筆無效 → **沒有有效標記、不回退**（理由「最新標記無效」）；行序與 `at` 時間順序不一致 → 照行序選取並加註「標記紀錄時間順序異常」（該循環待確認）。
- 分析程式套用的標記（`Stage.label`）與有效標記不同時，`階段清單` 兩者並列。

**確認狀態（每個已配對循環；狀態值依優先順序，理由欄列出所有成立理由）**
| 優先 | 狀態 | 條件 |
|---|---|---|
| 1 | 中止 | 任一階段有效標記為「中止」 |
| 2 | 未完成 | E1～E4 任一不確定；任一階段 `end_by` ∈ {`data_end`, `operator_split`}；Session `incomplete` 非空 |
| 3 | 未滿 | 兩階段都有有效標記，且至少一個為「未滿」 |
| 4 | 待確認 | 任一階段沒有有效標記；任一階段有未決不確定停頓（`certain=False` 且無 merge／split 決策）；有可能屬於該循環的無法對應紀錄；標記時間順序異常；配對確定性＝循環配對待確認 |
| 5 | 確認完成 | DC：前半「滿放」＋後半「滿充」（CD 相反）；E1～E4 全部確定；沒有未決停頓；配對確定；沒有任何待確認理由 |
- SOC 不參與任何判定（Q3）；不確定停頓不推論原因（Q6）。
- 未配對階段只顯示各自標記，不給確認狀態。

### 6.5 對應不到的操作人員紀錄（P-9，已核准）
- `stage_label` 的 `stage_start` 對不到 → 「無法對應的標記」，不套用。
- `continuity`（merge／split）的 `item_id` 對不到 → 「無法對應的停頓決策」。
- 未知 `type` → 「無法辨識的紀錄」。
- 三者都必須列在 `不確定停頓與標記紀錄`，含原始內容（時間、request_id、內容），**不得默默忽略**；可能屬於某個循環的時間範圍內 → 該循環待確認。

### 6.6 計數不變式 I1～I8（P-8，已核准；任一失敗 → `FAILED`（`count_invariant`），不產生正式檔名）
| # | 不變式 |
|---|---|
| I1 | 原始階段數 ＝ `analyze` 產生的階段總數（各 Session 加總） |
| I2 | 原始階段數 ＝ 已配對階段數 ＋ 未配對階段數 |
| I3 | 已配對階段數 ＝ 2 × 已配對循環數 |
| I4 | 已配對循環數 ＝ 確認完成＋未滿＋中止＋待確認＋未完成 |
| I5 | 已配對循環數 ＝ 配對確定＋循環配對待確認 |
| I6 | 任何階段 ID 在所有循環中最多出現一次 |
| I7 | 每個循環的兩個階段屬於同一 Session |
| I8 | 確認完成的循環，配對確定性必為「確定」 |

### 6.7 判定案例（第一版；DC 除非註明）
| # | 情況 | 配對結果／確定性 | 確認狀態 |
|---|---|---|---|
| K1 | D[滿放]、C[滿充] | 1 循環／確定 | 確認完成 |
| K2 | CD：C[滿充]、D[滿放] | 1 循環／確定 | 確認完成 |
| K3 | 順序設 DC、資料 C、D | C 開頭半循環＋D 結尾半循環；0 循環 | — |
| K4 | C、D[滿放]、C[滿充] | 開頭 C 未配對；1 循環／確定 | 確認完成 |
| K5 | D[滿放]、C[滿充]、D | 1 循環／確定；結尾 D 未配對 | 確認完成 |
| K6 | D、C、D、C、D、C 全部正確標記 | 3 循環／確定 | 3 個確認完成 |
| K7 | D[滿放]、C[未滿]、D[滿放]、C[滿充] | 2 循環／確定 | 未滿、確認完成 |
| K8 | D[中止]、C[滿充] | 1 循環／確定 | 中止 |
| K9 | D[滿放]、C（無標記） | 1 循環／確定 | 待確認 |
| K10 | D、C，C `data_end` | 1 循環／確定 | 未完成 |
| K11 | 只有 D | 0 循環；結尾半循環 | — |
| K12 | 沒有任何階段 | 0 循環、0 階段 | — |
| K13 | C1 實際（§13） | 1 循環／確定 | 待確認 |
| K14 | 人工 split：D1a（`operator_split`）、D1b、C | D1a 未配對（其後為同向）；(D1b, C) 1 循環／循環配對待確認（A1） | 未完成（D1b `start_certain=False`） |
| K15 | 待機分段：D（`idle_confirmed`）、D[滿放]、C[滿充] | 第一個 D 未配對；(D, C) 1 循環／循環配對待確認（A1） | 待確認 |
| K16 | D[滿放]、C、C[滿充] | (D, C) 1 循環／循環配對待確認（A2）；第二個 C＝未配對：前無可配的前半階段 | 待確認 |
| K17 | 開始不確定：D[滿放]、C[滿充] | 1 循環／確定 | 未完成 |
| K18 | 跨 Session：S1＝D[滿放] ｜ S2＝C[滿充] | S1 結尾半循環、S2 開頭半循環；0 循環 | — |
| K19 | 重複輸入或複製改名 | 前置驗證拒絕 | — |

## 7. 跨 Session 統計與比較（P-1、P-22）

### 7.1 跨 Session 統計 vs 配對（P-1）
| | 跨 Session 統計（允許） | 跨 Session 配對（禁止） |
|---|---|---|
| 內容 | 各 Session 各自算出的結果並列；Session 數、各 Session 階段數與加總、循環數與各狀態加總（標「各 Session 加總」） | 把不同 Session 的階段配成同一循環 |
| 不允許 | 合併計算 M-1 或端點失敗；串接 1 Hz 時間軸；不可比指標相加 | — |

### 7.2 時間量定義（P-22，已核准）
| 名稱 | 定義 | 確定時 | 不確定時 |
|---|---|---|---|
| 觀測運行跨度（每階段） | `A.t − start.t`（含階段內停頓，不扣除） | `HH:MM:SS`（附註「1 Hz 取樣，邊界誤差約 ±1 秒」） | 數值欄 **N/A（邊界不確定）**；另欄「觀測跨度（下限）」顯示同一計算值並標「下限，實際可能更長」 |
| 階段內停頓 | 只列數量（確定／不確定） | — | 不確定停頓時長不納入任何計算 |
| 休息時間（前半→後半） | 沿用 `rest_between`：後半 R − 前半結束後首筆待機 B | `HH:MM:SS` | **N/A**＋分析程式原文理由 |
| 觀測間隔（輔助） | 後半 `start.t` − 前半 `A.t` | `HH:MM:SS`，標「觀測間隔（上限參考），不是休息時間」 | 同左 |
- 不提供淨運行時間、循環總時長、各 Session 合計時長。

### 7.3 數值量定義
| 名稱 | 定義 |
|---|---|
| Rack 最高／最低溫、Cell 最高／最低電壓（每階段） | 與重產明細階段頁同一算法：`stage_minutes(res, st, valid=is_report_valid, ext_scope="minute", prefer=has_pcs_temp)` 各分鐘列的極值欄取 max／min；沒有有效分鐘 → N/A |
| SOC 起訖（每階段） | 首筆與最後觀測運行的 `soc`；只顯示，不參與判定 |

### 7.4 可比條件與禁止事項
- 比較表列出**所有已配對循環**，每列附配對確定性與確認狀態；未配對階段不列入比較表（列在 `階段清單`）。
| 指標類別 | 可比條件（全部成立） |
|---|---|
| 時間量 | 兩邊都不是 N/A；同一 `--cycle-order`；params 相同；電櫃規格相同且有值；`test_info.conditions` 文字相同 |
| 溫度、電壓極值 | params 相同；電櫃規格相同且有值 |
| SOC | 只顯示 |
| 品質指標（M-1、覆蓋率、讀取失敗、中斷） | 分 Session 並列；不合併計算；附讀取模式與宣告版本 |
| 端點失敗類（部分端點失敗、失敗端點、401、每秒 GET 數） | 只依**資料記錄的讀取模式**：兩邊都記錄且相同 → 可比；不同或任一未記錄 → 不可比 |
- 禁止：對不可比指標加總、差值、比率、排名、標示較好／較差；第一版整合活頁簿**不繪製任何圖表**；可比時也只並列，不自動計算差值。

## 8. 版本資訊與新舊版本 Session 相容（M6、M7）

### 8.1 欄位相容
| 項目 | r2b | r2c | 處理 |
|---|---|---|---|
| observations 鍵與 10 個數值欄位、flags | 有 | 相同 | 直接使用；缺值 → N/A |
| slots.jsonl | 有 | 相同 schema | 各自重建格點品質 |
| `cert_state.hmi_reader` | 無 | `read_cert` | 無 → 「未記錄」 |
| `cert_timing.reader.ep_ms` | 無 | 每筆 5 個 | 無 → 「未記錄」 |
| decisions／commands | 可能無 | 可能有 | 不存在 → 空清單；「無操作人員紀錄」 |
| params | 有 | 有 | 逐鍵比對；不同 → 標示；缺鍵 → N/A |
| 未知新鍵 | — | — | 記入 manifest `unknown_keys`，不推算 |

### 8.2 版本資訊（宣告與資料特徵分欄）
| 欄 | 內容 |
|---|---|
| A 宣告版本 | `--version` 的值；未提供 → 「未提供」 |
| B 宣告來源 | 「使用者提供」 |
| C 驗證狀態 | 固定「未驗證」 |
| D 資料特徵：讀取模式紀錄 | `cert_state.hmi_reader`（＋`hmi_reader_changes`） |
| E 資料特徵：端點計時 | `cert_timing.reader.ep_ms` 每筆筆數分布 |
| F 資料特徵摘要 | 「具 r2c O-1 特徵」／「未見 r2c O-1 特徵」 |
| G 一致性 | A 與 F：一致／不一致（警示，不阻擋、不改寫）／無法比對 |
| H 重產明細產生程式 | 開發版（r2c 程式碼）＋模組 SHA-256；r2b 記錄者註明 X1～X3 差異 |
- A～C 與 D～F 分兩區；資料特徵只是觀察，不得把 C 改為「已驗證」。

## 9. 輸出檔案與整合活頁簿（M8、M9）

### 9.1 輸出結構
```
MERGE_<ts>\
  STATUS.json
  merge_manifest.json            （發布前為 merge_manifest.json.partial）
  publish_journal.jsonl          （只在發布時追加）
  整合認證報告_<第一cert_id>_<最後cert_id>[_含未驗證明細].xlsx     （或 .partial.xlsx）
  重產明細\重產明細_S<n>_<cert_id>[_資料值未驗證].xlsx              （或 .partial.xlsx）
  _snapshot\<cert_id>\...
```

### 9.2 正式檔名（D-2、Q-A）
| 檔案 | 正式檔名 |
|---|---|
| 整合報告（所有 Session 資料值 VERIFIED） | `整合認證報告_<第一cert_id>_<最後cert_id>.xlsx` |
| 整合報告（任一 Session 資料值 UNVERIFIABLE、無 FAILED） | `整合認證報告_<…>_<…>_含未驗證明細.xlsx` |
| 重產明細（資料值 VERIFIED） | `重產明細_S<n>_<cert_id>.xlsx` |
| 重產明細（資料值 UNVERIFIABLE） | `重產明細_S<n>_<cert_id>_資料值未驗證.xlsx` |
| 任一 FAILED、快照失敗、不變式失敗、發布失敗 | 沒有正式檔名；維持 `.partial.xlsx`（或依失敗階段沒有報告檔） |
- 原始參數 UNVERIFIABLE **不觸發**任何後綴（D-1）。

### 9.3 重產明細與原始報告的區分（U10、P-18）
| 項目 | 原始 Menu 22／25 報告 | 重產明細 |
|---|---|---|
| 位置 | 來源 `reports\`（快照有位元組複本） | `MERGE_<ts>\重產明細\` |
| 檔名 | `認證測試報告_<cid>_NN[_未完成│_中途].xlsx` | `重產明細_S<n>_<cid>[_資料值未驗證].xlsx`（沒有編號 NN） |
| 身分 | 原始認證紀錄 | **不是原始認證紀錄**，只供整合報告對照 |
| 標示 | 不變 | 活頁簿標題＝「重產明細（非原始認證紀錄）」；描述第一行固定：「本檔由跨 Session 整合工具（開發版程式）於 <時間> 從來源快照重產，不是原始認證紀錄；原始報告：<檔名>（SHA-256 <前 12 碼>）」 |
| 禁止 | — | 不得複製到來源 Session；不得改名為原始報告格式；整合報告中不得稱為「認證報告」 |
- 重產明細由**未修改的** `save_report` 產生；標記沿用分析程式（與 Menu 22 相同行為）。

**可能與原始報告不同之處（X1～X9）**
| # | 位置 | 原因 |
|---|---|---|
| X1（REV-1 修訂） | Raw Data 每秒頁：PCS 旗標延遲期間的**整列**（狀態、數值欄與說明欄）——重產列的內容逐欄取自該秒來源觀測；原始列為整列沿用前一列。只有符合 §10.6 V-R 條件 (a)～(f) 的列才屬於 X1 | 重產使用 r2c FAIL-2 規則（`raw_measurement_valid`）；r2b 原始報告在旗標延遲期間整列沿用前一筆有效列 |
| X2（保留歷史編號） | Raw Data 說明欄「PCS 狀態：…」 | r2c 才有。**REV-1 起，說明欄差異統一由 X1 的逐欄條件 (b)、(f) 處理**；X2 不再單獨作為豁免依據，保留編號以免舊版引用失效 |
| X3 | 描述：Raw Data 摘要文字 | `raw_seconds_note` 措辭 r2b／r2c 不同 |
| X4 | 描述：「HMI 資料來源」一行 | 只有 `cert_state` 有 `hmi_reader` 時出現 |
| X5 | 描述與標題：重產聲明、產生時間 | 重產新增 |
| X6 | 階段切點、停頓判定 | 原始報告用當時服務參數；重產用 `cert_state.params`（D2）；原始參數未記錄 → 無法驗證 |
| X7 | 階段標記、拆分 | 原始報告之後才有的 Menu 23 決策 |
| X8 | 截圖頁 | 原始報告之後才有的 Menu 24 事後補拍 |
| X9 | 檔案位元組、建立時間 | 必然不同 |

### 9.4 整合活頁簿 9 頁（M8；不含任何圖表）
| 順序 | 工作表 | 內容 |
|---|---|---|
| 1 | `整合Summary` | 第一屏（§9.5）；標題與計數列（§9.6）；跨 Session 比較表（§7） |
| 2 | `Session總覽` | 每個 Session：S 編號、cert_id、開始／結束、status、sample_count、階段數（C／D）、配對與確認計數、版本（宣告／特徵分欄）、原始報告、重產明細、資料值驗證、原始參數可追溯性 |
| 3 | `階段清單` | 每個原始階段一列：階段 ID、Session、序號、方向、開始／結束、`start_certain`／`end_certain`、`end_by`、與前一同向階段間的休息秒數、標記歷史筆數、分析程式套用的標記、有效標記、配對結果與原因、所屬循環 ID、SOC 起訖（只顯示）、觀測運行跨度 |
| 4 | `循環判定` | 每個循環一列：前半／後半階段 ID、休息時間、觀測間隔、配對確定性與觸發條件（A1／A2）、確認狀態與全部理由、不確定邊界；未配對階段也各列一列 |
| 5 | `不確定停頓與標記紀錄` | 每個不確定停頓：Session、item_id、所屬階段 ID、A、R、A→R 秒、原始判定、操作人員決策（無 → 待確認）；標記歷史明細；無法對應的標記／停頓決策／無法辨識的紀錄；不推論原因 |
| 6 | `資料品質` | 依 Session：預定格點、覆蓋率、實際執行讀取率、M-1、完整即時資料率、讀取失敗、MISSED、推定缺格、格點紀錄遺失、服務中斷；端點失敗類依 §7.4 |
| 7 | `參數與條件` | 各 Session params、params_note、test_info、環控快照摘要；不同處標「不同」 |
| 8 | `版本資訊` | §8.2；標記規則差異說明（§9.7） |
| 9 | `來源追溯` | §5.3 完整路徑對照；原始報告清單與 SHA-256；重產明細檔名與 SHA-256；資料值驗證區塊（§10）；工具版本（§11.3） |
- 工作表名稱 ≤31 字元、不重名。

### 9.5 第一屏固定格式（M-2）
```
產生本檔時的狀態（<寫檔時間>）：
  快照建立驗證（H1～H3）：一致
  計數不變式 I1～I8：成立
  資料值驗證：S1 <VERIFIED／UNVERIFIABLE（原因）／FAILED（原因代碼）>、S2 …
  原始參數：UNVERIFIABLE（原始報告未記錄）
  預定檔名：<預定正式檔名>（尚未發布）      ← VERIFY_FAILED 時寫「不發布（VERIFY_FAILED）」
最終快照驗證（H5、H4）與正式發布：於本檔產生後執行，結果以同資料夾 STATUS.json 與 merge_manifest.json 為準。
本檔只有在 STATUS.json 的 publish.state＝PUBLISHED、且 manifest 雜湊與各檔 SHA-256 相符時，才是正式發布的報告；檔名為 .partial 時一律無效。
```
- UNVERIFIABLE 時，第一屏另列未驗證 Session、原因及影響範圍。
- 固定說明：「整合報告的有效標記規則比 Menu 22 嚴格：最後一筆標記方向無效時不採用、不回退；重產明細與原始 Menu 22 報告沿用原規則。」
- 驗證區塊固定註明：「資料值驗證只涵蓋儲存格值、公式文字、工作表結構、表格與指定中繼資料；圖表、圖片、注入圖形與格式未驗證。」

### 9.6 標題與計數列（P-8）
| 電櫃規格情況 | 標題 |
|---|---|
| 全部相同且有值 | 「<規格> 跨 Session 整合認證報告（N 個 Session）」 |
| 全部未設定 | 「電櫃規格未設定 跨 Session 整合認證報告（N 個 Session）」 |
| 不一致或部分未設定 | 「電櫃規格不一致 跨 Session 整合認證報告（N 個 Session）」；依賴規格的比較項一律不可比 |
- 計數列（固定順序）：`Session N｜原始階段：放電 a、充電 b｜已配對循環 c｜配對確定性：確定 x、循環配對待確認 y｜確認狀態：確認完成 d、未滿 e、中止 f、待確認 h、未完成 i｜未配對階段 u`。只有「確認完成」可稱為完成。

### 9.7 標記規則差異（C5）
重產明細沿用 Menu 22 標記行為；整合報告採 §6.4 有效標記規則。此差異是設計使然，**不屬於** P-23 比對範圍（P-23 比對的是重產明細 vs 原始報告，兩者規則相同）。

## 10. 資料值驗證（M11、P-23、Q-B、X6）

### 10.1 定位
- 每次產報執行（F7）；結果寫入 `整合Summary` 第一屏、`Session總覽`、`來源追溯` 驗證區塊、STATUS、manifest。
- 只讀：原始報告一律讀快照中的位元組複本（openpyxl 唯讀）；不開啟、不修改來源 Session 或原始報告。
- 名稱固定「**資料值驗證**」；**不得宣稱整份 Excel 完全一致**。

### 10.2 各 Session 的兩個獨立欄位
| 欄位 | 值 |
|---|---|
| 資料值驗證 `data_value_verification` | `VERIFIED`／`UNVERIFIABLE`（附原因）／`FAILED`（附原因代碼與差異） |
| 原始參數可追溯性 `param_traceability` | `TRACEABLE`／`UNVERIFIABLE`（原始報告未記錄）；**第一版一律 UNVERIFIABLE** |
- 兩欄永遠分開；資料值一致時顯示「資料值驗證：VERIFIED；原始參數：UNVERIFIABLE（原始報告未記錄）」。
- 不得因數值一致就宣稱原始參數已驗證。
- 有無法解釋的數值差異 → `FAILED`（`UNEXPLAINED_VALUE_DIFF`），附註「可能原因包含 X6（參數差異），工具無法區分」；**不得**因 X6 可能而改判 UNVERIFIABLE。

**D-1 明示條件（全部必須成立）**
1. `整合Summary` 第一屏固定顯示「原始參數：UNVERIFIABLE（原始報告未記錄）」。
2. STATUS 與 manifest 為每個 Session 獨立記錄 `param_traceability`。
3. 資料值驗證與原始參數可追溯性是兩個獨立欄位。
4. 不得把資料值 VERIFIED 稱為整份 Excel 完全一致。
5. 整體結果不得使用「全部驗證通過」等誤導名稱。

### 10.3 原始報告清單與比較基準
- 清單（全部列出檔名、SHA-256、大小、記錄時間）：快照 `reports\` 內所有 `認證測試報告_<cid>_*.xlsx`；`cert_state.reports`；`supplements\reports.jsonl`；三者不一致時照列並標示。
- 比較基準：檔名編號 NN 最大且存在於快照的那一份；其餘只列不比。
- 基準為 `_中途` → 整體 UNVERIFIABLE；沒有任何原始報告 → UNVERIFIABLE（無基準）。

### 10.4 支援格式
| # | 條件 | 不符時 |
|---|---|---|
| F1 | 來源有 `slots.jsonl` | UNVERIFIABLE（來源版本過舊或格式不支援），不做逐項比較 |
| F2 | 基準報告有 `Raw Data`，表頭 ＝ `RAW1_HEADERS`（13 欄） | 同上 |
| F3 | 基準報告與重產明細工作表清單相同（名稱與順序） | **FAILED**（`STRUCTURE`） |

### 10.5 驗證範圍（Q-B）
| 類別 | 納入 |
|---|---|
| 儲存格 | 共有工作表（各階段頁、`整體測試`、`Summary`、`數據概覽`、`即時熱度`、`Raw Data` 及續頁）已使用範圍聯集每一格的**值與公式文字**（以保留公式的方式讀取）；數字、日期時間、文字完全相同（不設容許誤差）；空白與 None 視為相同 |
| 結構 | 工作表名稱與順序；每個工作表的已使用範圍大小；`Raw Data` 列數；表格名稱與範圍 |
| 指定中繼資料 | 活頁簿描述（以行比對，差集只允許 X3、X4、X5）；活頁簿標題（只允許 X5） |

| 不納入 |
|---|
| 圖表、圖片、注入圖形；字型、填色、框線、數值格式、欄寬列高、合併儲存格、凍結窗格、條件式格式、資料驗證、註解、超連結；列印設定；未指定的中繼資料（建立者、建立／修改時間等，X9）；檔案位元組與壓縮結構 |

- 禁用措辭（工具產生的任何文字、STATUS、manifest）：「完全一致」「相同檔案」「與原始報告一致」「全部驗證通過」。
- manifest `verify_scope` 逐項列出納入與不納入。

### 10.6 逐項驗證程序
| 項目 | 範圍 | 程序 | 結果 |
|---|---|---|---|
| V-S | 各階段頁、`整體測試`、`Summary` | 逐格比較 | 全部相同 → VERIFIED；差異且基準報告時間之後有新的 stage_label／continuity 決策（X7）→ 該頁 UNVERIFIABLE；其他差異 → FAILED（`UNEXPLAINED_VALUE_DIFF`／`FORMULA_DIFF`／`TABLE_MISMATCH`） |
| V-O | `數據概覽`、`即時熱度` | 逐格比較 | 相同 → VERIFIED；差異且基準時間後有新補拍（X8）或新 stage_label（X7）→ UNVERIFIABLE；其他 → FAILED |
| V-R（REV-1 修訂） | `Raw Data` 及續頁 | 列數相同（不同 → FAILED `ROWCOUNT`）；逐列：相同 → 通過；不同時**必須同時符合以下全部條件**才歸類為 X1：(a) 原始列與重產列時間欄相同；(b) 原始列的全部數值欄（SOC～功率 kW）與說明欄均等於前一列（整列沿用）；(c) 該秒來源觀測（重產列的來源觀測）`raw_measurement_valid` 為真、`is_report_valid` 為假，且 PCS 充電或放電旗標為 True、分析方向為待機（`I`）——旗標延遲證據；(d) 重產列狀態欄為 N/A；(e) 重產列每一個數值欄均等於該來源觀測的實際讀值（與重產程式同一取值函式 `field_value`；PCS 模組溫度欄為 `pcs_display`）；(f) 重產列說明欄與依該來源觀測重新計算的說明（`_raw1_note`）完全一致。**任何條件不成立或無法驗證（例：來源觀測缺少旗標欄位）→ FAILED（`RAW_ROW_NOT_X1`）**；不得只憑原始列等於前一列判定 X1；不得因單一欄位符合就豁免其他差異 | 全部差異列符合 → VERIFIED（列出 X1 列數）；任一不符 → FAILED（`RAW_ROW_NOT_X1`，列出前 20 列與各列不成立的條件） |
| V-D | 活頁簿描述 | 以行比較；差集每一行必須屬於 X3（Raw Data 摘要行，以固定開頭辨識）、X4（「HMI 資料來源：」開頭）、X5（重產聲明、產生時間） | 全部屬於 → VERIFIED；否則 FAILED（`DESC_UNKNOWN_LINE`） |
| V-T | 活頁簿標題 | 只允許 X5 | 否則 FAILED（`TITLE_UNKNOWN`） |
| V-B | 位元組、建立時間（X9） | 不比較 | 不適用 |
- 原因代碼：`STRUCTURE`、`ROWCOUNT`、`UNEXPLAINED_VALUE_DIFF`、`RAW_ROW_NOT_X1`、`DESC_UNKNOWN_LINE`、`TITLE_UNKNOWN`、`TABLE_MISMATCH`、`FORMULA_DIFF`。

### 10.7 結果彙總
| 層級 | 規則 |
|---|---|
| 每個項目 | VERIFIED／UNVERIFIABLE（附原因）／FAILED（附差異明細）／不適用 |
| 每個 Session | 任一 FAILED → FAILED；否則任一 UNVERIFIABLE → UNVERIFIABLE；否則 VERIFIED |
| 整份報告 | 列出各 Session 結果，不合併成單一「通過」 |

### 10.8 Q-A 對正式檔名的影響
1. 任一 Session 資料值 FAILED → `VERIFY_FAILED`：阻擋本次**全部**正式檔名；整合報告與全部重產明細保留 `.partial.xlsx`；manifest 保留完整差異、原因與 SHA-256；不提供強制放行。
2. 快照完整性失敗 → 不產生正式報告；依失敗階段保留證據；不以不可信快照分析。
3. I1～I8 失敗 → `FAILED`；不產生正式檔名。
4. UNVERIFIABLE（方案 b）：沒有 FAILED、快照與計數檢查通過時允許正式檔名；整合報告加 `_含未驗證明細`、該 Session 重產明細加 `_資料值未驗證`；第一屏列出未驗證 Session、原因與影響範圍；不得視為驗證通過；STATUS、manifest 保留各 Session 實際結果。

## 11. STATUS、manifest、publish_journal.jsonl 與發布協定（M10）

### 11.1 整體結果 `overall`
| 值 | 條件 | 正式檔名 |
|---|---|---|
| `COMPLETED` | H1～H5 全部一致；I1～I8 成立；所有 Session 資料值 VERIFIED；發布完成 | 有 |
| `COMPLETED_WITH_UNVERIFIED` | 同上，但有 Session 資料值 UNVERIFIABLE、無 FAILED | 有（含後綴） |
| `VERIFY_FAILED` | 任一 Session 資料值 FAILED | 無；全部 `.partial` |
| `SNAPSHOT_MISMATCH` | H2 不一致 | 無報告檔 |
| `SOURCE_CHANGED_DURING_SNAPSHOT` | H3 不一致 | 無報告檔 |
| `SOURCE_CHANGED` | H4 不一致 | 無；全部 `.partial` |
| `SNAPSHOT_CHANGED` | H5 不一致 | 無；全部 `.partial`；快照不可信 |
| `FAILED` | 空間、路徑、讀寫錯誤、不變式、manifest 寫入、例外（附 `reason`） | 無 |
| `PUBLISH_FAILED` | 發布中途失敗且已回復為全部 `.partial` | 無 |
| `PUBLISH_INCONSISTENT` | 發布中途失敗且回復也失敗 | **無效**；人工調查（U-1） |
| `INCOMPLETE` | STATUS 停在中間階段（中斷、斷電） | 無效 |
- 判定優先：快照類（F3）→ `FAILED`（F5、F6、F8 錯誤）→ `VERIFY_FAILED`（F7）→ `SNAPSHOT_CHANGED`（F10）→ `SOURCE_CHANGED`（F11）→ 可發布者進入發布 → 發布結果。
- 不使用 `OK`、`VERIFIED`、「全部驗證通過」作為整體結果。

### 11.2 STATUS.json
- `stage`：`SNAPSHOT` → `ANALYZE` → `WRITE_DETAILS` → `VERIFY` → `WRITE_SUMMARY` → `INTEGRITY` → `PUBLISHING` → `DONE`。
- 欄位：`merge_id`、`stage`、`overall`、`reason`、`snapshot`（H1～H3 結果與時間）、`integrity`（`h5`、`h4`：結果、實際時間、不一致清單；H5 失敗另記「依據的快照不可信，只供調查」）、`count_invariants`（I1～I8 各自結果）、`sessions[]`（`s_no`、`cert_id`、`data_value_verification`、`reason_codes`、`param_traceability`、`declared_version`、`version_source`、`version_verification`）、`publish`（`state`：NOT_ATTEMPTED／PUBLISHING／PUBLISHED／ROLLED_BACK／INCONSISTENT；`planned[]`：partial 名、正式名、SHA-256；`done[]`）、`manifest_sha256`、`updated_at`。
- 每次更新以「寫暫存 → 單檔取代 → 讀回驗證」進行；`stage` 停在 `DONE` 以外 → 視為無效（`INCOMPLETE`）。

### 11.3 merge_manifest.json
- `merge_id`、`generated_at`、`cycle_order`、`--version` 原文、來源數。
- `tool`：每個 `cert_merge_*.py` 與被重用模組（`cert_stage_engine.py`、`cert_report_xlsx.py`、`report_quality.py`、`report_display.py`、`cert_report_config.py`、`cert_screenshot.py`、`cert_supplement.py`、`cert_env.py`、`cert_heat_link.py`）的 SHA-256；開發 clone HEAD 與 `git status` 項數及清單（唯讀、`GIT_OPTIONAL_LOCKS=0`）；Python 與 openpyxl 版本。
- 時間：快照開始／完成、產報開始／完成、H4、H5 實際時間（本機時間，ISO 秒）。
- `sources[]`：S 編號、cert_id、原始路徑、快照路徑、started/ended、status、sample_count、params、test_info、版本資訊（§8.2）、`files[]`（相對路徑、大小、mtime、H1／H2／H3／H4／H5 SHA-256）、原始報告清單與 SHA-256。
- `stages[]`、`cycles[]`（ID、配對確定性、確認狀態、理由）、`unpaired[]`、`uncertain_pauses[]`、`label_history[]`、`orphan_records[]`、`comparability[]`、`unknown_keys[]`。
- 各 Session `data_value_verification`（含差異明細與原因代碼）與 `param_traceability`；`verify_scope`（納入與不納入項目）。
- `integrity.h5`、`integrity.h4`（實際時間、逐檔雜湊、比對結果）；`integrity.statement` 固定文字：「來源完整性只驗證到 H4 時點（<時間>）；本工具不保證發布完成時原始 Session 仍未改變。整合報告與重產明細的內容全部來自 H2 驗證、H5 再確認的快照。」
- `outputs[]`：partial 名、預定正式名、F9 計算的 SHA-256。
- `overall`、`publish`（同 STATUS）。
- **manifest 不含自身雜湊**；其 SHA-256 記在 STATUS 並在結束時印出（P-21；失敗時只要 manifest 已寫出也印出）。

### 11.4 發布協定
**前提 G1～G6（全部成立才開始；否則不發布）**
| # | 條件 |
|---|---|
| G1 | H1～H5 全部一致 |
| G2 | I1～I8 全部成立 |
| G3 | 所有 Session 資料值驗證已完成，且沒有 FAILED |
| G4 | 所有 `*.partial.xlsx` 已寫完並已計算 SHA-256（F9） |
| G5 | 每個預定正式檔名都不存在 |
| G6 | 最終 manifest 已寫成 `merge_manifest.json.partial` 並驗證 |

**準備**
1. 在記憶體組好最終 manifest（正式名稱、SHA-256、`publish.state` 預填 PUBLISHED）。
2. 寫 `merge_manifest.json.partial` → 讀回 → 解析 → 與記憶體逐欄相同 → 計算 SHA-256（M）。失敗 → `FAILED`（`manifest_write_failed`），不發布。
3. 先把 STATUS 寫成 `stage=PUBLISHING`、`publish.state=PUBLISHING`、`planned[]`、預期 `manifest_sha256=M`，讀回驗證；**失敗 → 不開始任何改名**。

**改名（R1～R4）**
| 步驟 | 內容 |
|---|---|
| R1 | 固定順序逐檔改名：重產明細（S1、S2…）→ 整合報告 → `merge_manifest.json.partial` → `merge_manifest.json`；使用不覆蓋方式（目的地存在即失敗）；每完成一檔追加一行到 `publish_journal.jsonl`（只追加、寫入後 flush） |
| R2 | 全部改名後逐檔重算 SHA-256 ＝ `planned[]`；manifest SHA-256 ＝ M |
| R3 | 寫最終 STATUS（`stage=DONE`、`publish.state=PUBLISHED`、`overall`、`manifest_sha256=M`），讀回驗證 |
| R4 | 印出 M 與最終結果；「發布完成」只在 R3 讀回驗證成功後印出，並同時印出判定依據（STATUS 路徑、M） |

**失敗回復（不自動重試、不自動放行、不提供強制放行或自動修復）**
| 失敗點 | 處理 |
|---|---|
| R1 中途改名失敗 | 依日誌反向改回 `.partial`：全部成功 → `PUBLISH_FAILED`、`ROLLED_BACK`；任一失敗 → `PUBLISH_INCONSISTENT`、`INCONSISTENT`，STATUS 列出每個檔案實際名稱 |
| R2 雜湊不符 | 同上；`reason=post_rename_hash_mismatch` |
| R3 寫最終 STATUS 失敗 | 同上回復（含 manifest）；再嘗試寫 PUBLISH_FAILED／INCONSISTENT；仍失敗 → STATUS 停在 PUBLISHING（無效） |
| 程序中斷 | 不自動復原；STATUS 停在 PUBLISHING → 無效 |
- **U-1**：`PUBLISH_INCONSISTENT` 不提供自動修復；保留 STATUS、manifest、`publish_journal.jsonl` 與全部相關檔案；不重試、不放行、不覆蓋或刪除；人工調查。
- 不宣稱多檔改名具有原子性；以「預寫 PUBLISHING＋只追加日誌＋反向回復＋STATUS 為唯一判定」補償。

**正式發布判定（唯一依據；三項全部成立）**
1. STATUS：`publish.state = PUBLISHED`、`stage = DONE`、`overall` ∈ {`COMPLETED`, `COMPLETED_WITH_UNVERIFIED`}。
2. `merge_manifest.json` 存在且 SHA-256 ＝ STATUS 的 `manifest_sha256`（也可與使用者另外記錄的值比對）。
3. manifest 列出的每個正式檔案都存在且 SHA-256 相符。
- 檔名呈現正式格式本身不代表已發布。

**不可發布結果的收尾**
- `VERIFY_FAILED`、`SOURCE_CHANGED`、`SNAPSHOT_CHANGED`、`FAILED`（F5 之後）：manifest 直接寫 `merge_manifest.json`（`publish.state=NOT_ATTEMPTED`），STATUS 寫最終結果，印出 M；報告檔維持 `.partial`。
- 快照類失敗（F3）：沒有報告檔；manifest 只含已完成的雜湊與失敗原因。
- manifest 或 STATUS 寫入失敗：結束碼非 0、印出原因；STATUS 停在先前階段 → 無效。
- 重跑一律建立新的 MERGE 資料夾；第一版不提供在既有資料夾續跑、修復或重新發布。

## 12. 處理流程與完整性限制

### 12.1 流程（M-1）
| 步驟 | STATUS `stage` | 內容 | 失敗時 |
|---|---|---|---|
| F0 | — | 網路封鎖 | — |
| F1 | — | V1～V9；空間；最長輸出路徑 | rc=2；不建立 MERGE |
| F2 | SNAPSHOT | 不存在才建立 `MERGE_<ts>\`；寫 STATUS | 已存在 → rc=2 |
| F3 | SNAPSHOT | H1 → 複製 → H2 → H3 | 快照類結果；不分析 |
| F4 | SNAPSHOT | 快照設唯讀屬性 | 只記錄 |
| F5 | ANALYZE | 每 Session 分析、配對、確認、I1～I8（只讀快照） | `FAILED`（`count_invariant`）；不產生報告檔 |
| F6 | WRITE_DETAILS | 重產明細 `*.partial.xlsx`（只讀快照）；寫入標題（X5） | `FAILED`（`write_failed`） |
| F7 | VERIFY | 資料值驗證與原始參數可追溯性 | 記錄；不停止 |
| F8 | WRITE_SUMMARY | 決定暫定整體結果與預定檔名；寫整合活頁簿 `*.partial.xlsx`（第一屏 §9.5） | `FAILED`（`write_failed`） |
| F9 | WRITE_SUMMARY | 計算所有 `*.partial.xlsx` 的 SHA-256 | — |
| F10 | INTEGRITY | H5 | `SNAPSHOT_CHANGED` |
| F11 | INTEGRITY | H4（記錄實際時間） | `SOURCE_CHANGED` |
| F12 | PUBLISHING 或 DONE | 可發布 → §11.4；不可發布 → 收尾 | §11.4 |
| F13 | DONE | 最終 STATUS；印出 manifest SHA-256 | §11.4 |
- `VERIFY_FAILED` 時 F8 仍寫整合活頁簿供調查；第一屏預定檔名列寫「不發布（VERIFY_FAILED）」。
- H4／H5 失敗時不重新修改已產生的整合報告；由 `.partial` 檔名、第一屏固定判定規則、STATUS、manifest 共同標示（M-3）。

### 12.2 完整性限制 IC-1～IC-6
| # | 限制 | 落實 |
|---|---|---|
| IC-1 | 報告檔在 F9 計算 SHA-256 後內容不得再修改 | F9 之後只做雜湊、manifest／STATUS 寫入、改名；R2 重算必須相符 |
| IC-2 | H4（F11）之後不得重新讀取原始 Session | F11 之後的程式路徑不呼叫任何來源讀取 |
| IC-3 | H5（F10）之後不得修改快照 | 快照只在 F3 寫入；F4 之後不寫快照 |
| IC-4 | 發布階段不得重新產生或修改報告內容 | 發布只做改名與 STATUS／manifest 寫入 |
| IC-5 | 發布後是否有效一律依 STATUS、manifest、各正式檔案 SHA-256 判定 | §11.4 判定規則；第一屏與 manifest 聲明 |
| IC-6 | 不得只因正式檔名存在就宣稱發布成功 | 「發布完成」只在 R3 讀回驗證成功後印出，並附判定依據 |
- **U-2**：manifest 記錄 H4 實際時間與來源雜湊；來源完整性只驗證到 H4 時點；H4 之後不讀原始 Session；整合報告與重產明細全部來自已驗證快照；不宣稱發布完成時原始 Session 一定未改變。

## 13. C1 實際案例（離線驗證依據；使用者已接受）
- 方法：只讀 scratchpad C1 副本；開發 clone `cert_stage_engine.analyze`；`cert_state.params` 與程式預設相同；副本前後雜湊一致；未修改 C1、未執行 Menu 23。
| 階段 ID | 方向 | 開始 | 最後觀測運行 | end_by | 邊界 | 標記 | SOC | 不確定停頓 |
|---|---|---|---|---|---|---|---|---|
| CERT_20261112_111816#ST1 | 放電 | 11:19:20 | 13:20:24 | idle_confirmed | 確定／確定 | 未標記 | 100→0 | 4 |
| CERT_20261112_111816#ST2 | 充電 | 13:24:56 | 15:24:52 | idle_confirmed | 確定／確定 | 未標記 | 0→100 | 4 |
- DC：CY1＝(ST1, ST2)；配對確定（無相鄰同向）；確認狀態**待確認**（兩階段未標記；8 個未決停頓）。
- 時間量：ST1 跨度 2:01:04；ST2 1:59:56；休息（rest_between）271 秒；觀測間隔 272 秒。
- 8 個不確定停頓（A→R 11～12 秒）：U-20261112113257-20261112113309、U-20261112120326-20261112120337、U-20261112123354-20261112123406、U-20261112130421-20261112130432（ST1）；U-20261112133445-20261112133457、U-20261112140515-20261112140527、U-20261112143536-20261112143548、U-20261112150603-20261112150614（ST2）；系統判定「暫時保留同一階段（階段是否連續不確定）」；決策「待確認」；不推論原因。
- 預期計數列：`Session 1｜原始階段：放電 1、充電 1｜已配對循環 1｜配對確定性：確定 1、循環配對待確認 0｜確認狀態：確認完成 0、未滿 0、中止 0、待確認 1、未完成 0｜未配對階段 0`。
- U-3：C1 實際資料值驗證結果（T23-1）列為實作後離線測試項目。

## 14. 整合一致性檢查結果（Phase 1）

### 14.1 編輯性衝突（Phase 1 發現；使用者已於 2026-11-16 決定）
| # | 衝突 | 來源 | 影響 |
|---|---|---|---|
| O-1 | 測試編號重複：r5 §5.5 的 `TP-1～TP-3` 與 r6 §8 的 `TP-1～TP-8` 編號相同、內容不同 | r5 §5.5、r6 §8 | **已決定**：r5 三項改為 `TN-1`（網路封鎖）、`TN-2`（靜態檢查）、`TN-3`（寫入守門），內容與預期不變；`TP-1～TP-8` 保留為發布協定測試（§15.7、§15.9） |
| O-2 | 第 5 頁名稱不一致：r4 §5.1 稱 `不確定停頓`；r5 §1.2、§2.2 稱 `不確定停頓／標記紀錄` | r4 §5.1、r5 §1.2／§2.2 | **已決定**：統一為 `不確定停頓與標記紀錄`；內容不變（不確定停頓、標記歷史、未匹配的標記及相關紀錄）（§9.4） |

### 14.2 實作注意（不是衝突，凍結規格可實作）
| # | 項目 | 說明 |
|---|---|---|
| N-1（已決定） | 重產明細標題（X5） | `save_report` 存檔時把標題固定設為「充／放電認證測試報告」，無法經由參數改寫；凍結規格要求標題為「重產明細（非原始認證紀錄）」。可在 F6、F9 計算雜湊之前，以 zip 層級只改寫 `docProps/core.xml` 的標題達成（與既有 `_inject_shapes` 相同的作法，不重新以 openpyxl 開啟另存），不修改既有模組。使用者決定：只能修改新產生的重產明細、不得修改原始報告；必須確認只有標題改變（改寫前後除 `docProps/core.xml` 以外的壓縮內容逐項相同），差異依 P-23（X5）處理 |
| N-2（已決定） | 描述 3,000 字元截斷 | `save_report` 截斷描述為 3,000 字元；若原始或重產描述達上限，尾端行可能不同而依 V-D 判 FAILED（保守方向，不會誤判通過）。C1 原始描述 834 字元，不受影響。使用者決定：維持保守驗證，未解釋差異不得判為 VERIFIED |
| N-3（已決定） | `build_sampling_slots`、`params_note`、`hmi_reader_note`、`record_to_obs`、meta 組裝位於 `cert_runtime.py` | 依 §1.6 在新模組重建；與正式流程的差異由資料值驗證偵測（K2）。使用者決定：不得 import `cert_runtime`；重建後須經離線測試與資料值驗證確認 |
| N-4（已決定） | V7 讀取正式版 `active.json` | 執行時唯讀讀取；使用者決定：離線測試只能使用隔離的 `active.json` 測試資料，**不得存取正式版 `active.json`** |
| N-5（已決定） | T23-9 原以 `CERT_20261029_082805` 為例 | 測試授權只允許 C1 唯讀快照；使用者同意：使用合成 Session，不額外讀取其他正式 Session |

### 14.3 風險（沿用）
| # | 風險 | 處理 |
|---|---|---|
| K1 | 重產明細與原始報告不同 | 標示重產；只允許 X1～X9 |
| K2 | 重建 meta 組裝可能與正式流程脫節 | manifest 記錄模組雜湊；資料值驗證 |
| K3 | 依賴 `cert_report_xlsx` 內部函式 | 只在開發 clone；版本變動時重跑驗證 |
| K4 | none 模式下待機分段的長放電／充電產生配對待確認 | 不會被當成確認完成；人工判讀 |
| K5 | C1 的 8 個停頓未確認 | 依 Q6 |
| K6 | 唯讀屬性可被取消 | 只依雜湊；manifest 雜湊另外記錄 |
| K7 | 快照空間累積 | 不自動刪除；使用者管理 |
| K8 | 產報時服務寫入來源 | 只接受 ended；H3／H4 偵測 |
| K9 | 數值不設容許誤差 | 程式小幅變更也會 FAILED（保守取捨） |
| K10 | 圖表、截圖、注入圖形不在驗證範圍 | 報告明示；人工檢視 |

### 14.4 實作偏離紀錄（使用者 2026-11-16 有條件接受）
**DEV-1：不 import `cert_env.py`，改在 `cert_merge_xlsx.py` 逐字重建必要邏輯**
| 項目 | 內容 |
|---|---|
| 1. 原因與間接依賴 | §1.6 原列 `cert_env` 為只讀引用模組；但 `cert_env.py` 第 18 行 `from env_data_scraper import ENDPOINTS, …`，而 `env_data_scraper.py` 第 38 行 `from api_client import ApiClient`。import `cert_env` 會間接載入規格禁止的 `api_client`（`api_client` 本身 import 時不連線，但違反 §1.6 禁止規則），故不 import |
| 2. 重建的函式與對應來源 | `cert_merge_xlsx.ENV._temp` ← `cert_env._temp`（第 229 行）；`ENV.summary_lines` ← `cert_env.summary_lines`（第 237 行）；`ENV.failure_note` ← `cert_env.failure_note`（第 255 行）；常數 `ENV.SNAPSHOT_FILE`（第 20 行）、`ENV.NORMAL／ABNORMAL／READ_FAIL`（第 22 行）。函式本體逐字複製，只把模組層名稱改為 `ENV.` 前綴並加上 `@staticmethod` |
| 3. 原始檔 SHA-256 | `cert_env.py` `5c7a6a67b1c63f68ef05de9fe5f11fdb8de4361b26ed24ac45aa1935fe2a5917`（2026-11-16，開發 clone）；manifest `tool.reference_only` 每次產報記錄當時雜湊 |
| 4. 等價性驗證方式 | 離線測試 T-ENV：以**讀取原始碼文字**（不 import `cert_env`）取出三個函式，正規化（移除 `ENV.` 前綴、`@staticmethod`、縮排）後與重建版逐字比較，必須完全相同；常數值逐一比較；另以多組環控快照輸入（有快照、無快照、有異常設備、有端點失敗）比較重建版輸出與預期文字。同法 T-N3 驗證 `build_sampling_slots`（對照 `cert_runtime.py`，只允許 `REQ.read_json`→`_read_json` 一處差異）、`params_note`、`hmi_reader_note`、`record_to_obs` |
| 5. 邏輯漂移風險 | 日後 `cert_env.py` 或 `cert_runtime.py` 修改時，重建版不會自動跟進，重產明細的冷氣／硬體文字或描述可能與正式報告不同。偵測：(a) manifest 記錄對照來源雜湊，與本表雜湊不同即需重新審查；(b) T-ENV／T-N3 失敗；(c) 資料值驗證 V-S（Summary 條件列）／V-D（描述）會判 FAILED。處理：重新逐字同步並重跑測試，不放寬驗證 |
- 本偏離**不放寬任何功能要求**：輸出內容、驗證範圍、禁止事項均不變。

### 14.5 規格修訂紀錄
**REV-1：V-R／X1 改為可追溯的逐欄嚴格條件（使用者 2026-11-16 核准）**
| 項目 | 內容 |
|---|---|
| 修訂日期 | 2026-11-16 |
| 核准範圍 | 只修改 §9.3 X1／X2 與 §10.6 V-R；其他凍結條文不變；r1～r7 原始文件不變 |
| 原 §9.3 | X1：「Raw Data 每秒頁：PCS 旗標延遲期間的電流與狀態」；X2：「Raw Data 說明欄『PCS 狀態：…』」 |
| 原 §10.6 V-R | 「不同時必須全部符合 X1：① 基準列數值欄＝該列之前最近一筆 LIVE 列（沿用）；② 重產列對應秒數的來源觀測 `raw_measurement_valid` 為真、`is_report_valid` 為假；③ 重產列狀態欄為 N/A；說明欄差異只允許附加『PCS 狀態：…』（X2）」 |
| 新條文 | §9.3 X1：PCS 旗標延遲期間的整列差異，內容逐欄取自該秒來源觀測，只有符合 V-R (a)～(f) 者屬於 X1；X2 保留歷史編號，說明欄差異改由 X1 條件處理。§10.6 V-R：條件 (a)～(f)（見 §10.6），任一不成立或無法驗證 → FAILED（`RAW_ROW_NOT_X1`） |
| C1 唯讀證據摘要 | 只讀 scratchpad C1 副本與其原始 r2b 報告，以 r2c 規則在記憶體計算每秒列（不產生檔案；副本雜湊前後相同）。原始與重產皆 14,878 列 × 13 欄；**差異 26 列**，分兩段：放電結束 2026-11-12 13:20:24～13:20:35（12 列）、充電結束 15:24:52～15:25:05（14 列）。26 列全部：原始列（含說明欄）＝前一列；重產列來源觀測電流 0.0、PCS 放電／充電旗標 True、分析狀態「休息」、`raw_measurement_valid` 真、`is_report_valid` 假；重產狀態 N/A；重產數值欄＝來源觀測讀值；重產說明＝重新計算結果。沒有其他類型的差異 |
| 26 列欄位分布 | 狀態 26、電流 26、總電壓 26、Cell 最大電壓 25、Cell 最小電壓 25、說明 26；SOC、Rack 溫度、傳感器、PCS 模組溫度、功率 kW 為 0（功率因 PCS 功率讀值延遲，兩邊皆為 −113.7） |
| 其他「等於前一列」的原因（證據） | 原始報告中等於前一列者共 5,381 列：重產為 LIVE（讀值本就相同）4,557、MISSED 742、CARRIED 82；這些列在重產中與原始相同，不構成差異。故**不得只憑「等於前一列」判定 FAIL-2** |
| 修改原因 | (1) 原條文字面（X1 只含電流與狀態、說明欄只允許附加「PCS 狀態」）與實際 FAIL-2 列不符（電壓與說明欄也會不同），照字面會把已知原因的差異判為 FAILED；(2) 原實作未檢查說明欄與逐欄來源，比原條文寬鬆。修訂後每個豁免欄位都可逐欄追溯到該秒來源觀測，未知差異一律 FAILED |
| 對 T23-1 的影響 | C1 的 V-R 依新條文預期 VERIFIED（26 列 X1）；依舊條文字面會 FAILED。其餘驗證項目結果需實際執行（仍暫停，待使用者確認） |
| 對 T23-2 的影響 | 合成 X1 型差異列預期 VERIFIED；另補反例（各條件單獨不成立 → FAILED）（仍暫停，待確認）。V-R 條件的單元測試以 T-VR-* 另列 |
| 對 T23-3 的影響 | 不受影響：非 X1 型差異仍 FAILED（仍暫停，待確認） |
| 證據檔 | scratchpad `r2c\merge_design\vr_evidence.py`、`vr_evidence_out.json` |

## 15. 離線測試規格（P-24、TA、TP、TB、TN）

**共同停止條件**：任何測試對 scratchpad／隔離測試資料夾以外的路徑寫入、發生網路連線嘗試、或來源副本雜湊改變 → 立即停止整個測試執行並回報。單一案例失敗 → 記錄並繼續，最後整體 FAILED；不得修改門檻或預期值使其通過。測試資料只用 scratchpad 複本或隔離測試資料夾；不修改真實 Session。

### 15.1 前置驗證、快照、計數（r4／r5）
| ID | 情境 | 預期 |
|---|---|---|
| T-V | V1～V9 拒絕案例；事前空間不足 | rc=2；不建立輸出 |
| TH-1～TH-5 | H1～H5 各自不一致（替身） | 對應 §11.1 狀態；只有全部一致才可能發布 |
| TH-6 | 事前空間不足（替身） | rc=2；不建立 MERGE |
| TH-7 | 複製中磁碟滿（替身） | `FAILED`（`disk_full`）；無正式檔名；來源不變 |
| TH-8 | manifest 已寫出後失敗 | 仍輸出 manifest SHA-256 |
| T-K1～K19 | §6.7 判定案例 | 與 §6.7 一致 |
| T-K20 | 計數一致性 | I2～I5 成立 |
| T-K21 | 原始階段不消失 | `階段清單` 列數 ＝ `analyze` 階段數 |
| T-A | A1、A2 觸發 | 配對待確認；確認狀態不會是確認完成 |
| T-C1 | C1 副本 | 與 §13 完全一致 |

### 15.2 P-7（T7）
| ID | 情境 | 預期 |
|---|---|---|
| T7-1 | 四邊界確定、DC 標記正確 | 確認完成 |
| T7-2 | E1 不確定 | 未完成（E1） |
| T7-3 | E2 不確定 | 未完成（E2） |
| T7-4 | E3 不確定 | 未完成（E3） |
| T7-5 | E4 不確定（data_end） | 未完成 |
| T7-6 | 放電標滿充（唯一一筆） | 無有效標記；待確認；兩種標記並列 |
| T7-7 | 未知標記值 | 同上 |
| T7-8 | 滿放→未滿→滿放 | 有效＝滿放；歷史 3 筆 |
| T7-9 | 滿放 → 滿充（方向不符，最後一筆） | 無有效標記（不回退）；待確認 |
| T7-10 | 行序與 `at` 顛倒 | 照行序；待確認（時間順序異常） |
| T7-11 | A1 且標記正確、邊界確定 | 待確認；配對待確認（兩欄分開） |
| T7-12 | 中止＋未完成條件 | 中止；理由同時列出 |
| T7-13 | 無法對應的 continuity 決策落在循環時間內 | 待確認；列出 |

### 15.3 P-8、P-9、P-11（T8、T9、T11）
| ID | 情境 | 預期 |
|---|---|---|
| T8-1 | 規格相同 | 標題含規格 |
| T8-2 | 規格不同 | 「電櫃規格不一致」；相關比較欄不可比 |
| T8-3 | 全部未設定 | 「電櫃規格未設定」 |
| T8-4 | I1～I8 正常資料 | 全部成立 |
| T8-5 | 同一階段出現在兩個循環（替身） | I6 失敗 → `FAILED`（`count_invariant`） |
| T9-1 | `stage_start` 對不到 | 無法對應；不套用 |
| T9-2 | 未知 `type` | 無法辨識 |
| T11-1 | 目的地路徑 241 字元 | `path_too_long`；不分析；來源不變 |
| T11-2 | `MERGE_<ts>` 已存在 | rc=2；既有內容不變 |

### 15.4 P-22（T22）
| ID | 情境 | 預期 |
|---|---|---|
| T22-1 | 邊界確定 | 跨度 ＝ A − start |
| T22-2 | 開始不確定 | N/A＋下限欄 |
| T22-3 | 休息期間狀態未知 | 休息 N/A；觀測間隔照列並標「不是休息時間」 |
| T22-4 | 未觀測到待機 | 休息 N/A |
| T22-5 | params 不同 | 不可比；無差值欄 |
| T22-6 | read_cert vs 未記錄 | 端點失敗類不可比 |
| T22-7 | 整合活頁簿 | 無圖表物件；無差值、合計時長欄 |
| T22-8 | C1 | 2:01:04、1:59:56、271 秒、272 秒 |
| T22-9 | 極值算法 | 與重產明細階段頁相同 |

### 15.5 P-23（T23）
| ID | 情境 | 預期 |
|---|---|---|
| T23-1 | C1 副本重產 vs 原始 `_01` | 預期 VERIFIED；若有未解釋差異 → FAILED，照實回報（U-3） |
| T23-2 | 只有 X1 型差異列 | V-R VERIFIED |
| T23-3 | 一列非 X1 型差異 | V-R FAILED |
| T23-4 | 階段頁一格不同、無新決策 | V-S FAILED |
| T23-5 | 階段頁不同、基準後有新 stage_label | V-S UNVERIFIABLE（X7） |
| T23-6 | 描述多一行未知文字 | V-D FAILED |
| T23-7 | 基準為 `_中途` | UNVERIFIABLE |
| T23-8 | 沒有原始報告 | UNVERIFIABLE |
| T23-9 | 沒有 slots.jsonl（舊版 Session 型態，可用合成資料） | UNVERIFIABLE（格式不支援） |
| T23-10 | 工作表清單不同 | FAILED（F3） |
| T23-11 | 多個原始報告＋一筆記錄無檔案 | 基準＝最大編號；清單全部列出並標示 |
| T23-12 | 驗證全程 | 來源與快照雜湊不變；原始報告未被寫入 |

### 15.6 Q-A／Q-B／D-1／D-2（TA，r6；TA-5 依 r7 改寫）
| ID | 情境 | 預期 |
|---|---|---|
| TA-1 | S2 FAILED、S1 VERIFIED | `VERIFY_FAILED`；全部 `.partial`；manifest 有差異與 M |
| TA-2 | S1 UNVERIFIABLE、S2 VERIFIED | `COMPLETED_WITH_UNVERIFIED`；整合報告 `_含未驗證明細`；S1 明細 `_資料值未驗證`；S2 無後綴；第一屏列出 S1 |
| TA-3 | 全部 VERIFIED | `COMPLETED`；第一屏仍顯示原始參數 UNVERIFIABLE；無後綴 |
| TA-4 | I6 失敗（替身） | `FAILED`（`count_invariant`）；沒有報告檔 |
| TA-5 | H5 不一致（替身） | `SNAPSHOT_CHANGED`；全部 `.partial`；第一屏無「H4／H5 通過」「已發布」措辭；STATUS、manifest 記錄不一致檔案與「快照不可信，只供調查」 |
| TA-6 | 有數值差異 | `FAILED`（`UNEXPLAINED_VALUE_DIFF`）＋X6 附註；不是 UNVERIFIABLE |
| TA-7 | 禁用措辭靜態檢查 | 0 命中 |
| TA-8 | 公式、表格範圍、已使用範圍不同 | 各自 FAILED（`FORMULA_DIFF`／`TABLE_MISMATCH`／`STRUCTURE`） |
| TA-9 | 放電最後一筆標滿充 | 整合報告無有效標記；重產明細照 Menu 22；P-23 不因此 FAILED |
| TA-10 | 最長輸出路徑（含後綴）> 240 | F1 rc=2；不建立資料夾 |

### 15.7 發布協定（TP，r6）
| ID | 情境 | 預期 |
|---|---|---|
| TP-1 | R1 第 2 個檔案改名失敗（替身） | 第 1 個改回；`PUBLISH_FAILED`／`ROLLED_BACK`；日誌完整 |
| TP-2 | R1 失敗且回復也失敗（替身） | `PUBLISH_INCONSISTENT`；STATUS 列出實際檔名；無效 |
| TP-3 | R3 寫 STATUS 失敗（替身） | 回復改名；STATUS 為 PUBLISH_FAILED 或停在 PUBLISHING；無效 |
| TP-4 | 預寫 PUBLISHING 失敗（替身） | 沒有任何改名；無效 |
| TP-5 | 預定正式檔名已存在 | 不發布；不覆蓋 |
| TP-6 | manifest 讀回不一致（替身） | 不發布；`FAILED`（`manifest_write_failed`） |
| TP-7 | R1 中途終止（替身） | STATUS＝PUBLISHING；無效；下次建立新資料夾 |
| TP-8 | 判定規則檢查器 | 只有三條全部成立才判為正式發布 |

### 15.8 完整性與第一屏（TB，r7）
| ID | 情境 | 預期 |
|---|---|---|
| TB-1 | H4 不一致（F10 之後修改來源） | `SOURCE_CHANGED`；全部 `.partial`；manifest 含 H4 時間與清單 |
| TB-2 | 檔案開啟紀錄 | F11 之後開啟來源 0 次；F4 之後寫入快照 0 次 |
| TB-3 | 第一屏靜態檢查 | 只有「產生本檔時的狀態」格式；不含「H5 通過」「H4 通過」「已發布」「發布成功」「完全一致」「全部驗證通過」 |
| TB-4 | F9 之後改動 `.partial`（替身） | R2 不符 → 回復 → `PUBLISH_FAILED` |
| TB-5 | manifest 欄位 | 含 `integrity.h4`、`integrity.h5`、`integrity.statement` |
| TB-6 | STATUS stage 序列 | 依 §11.2；中斷 → 無效 |

### 15.9 網路封鎖、靜態檢查、寫入守門（TN；原 r5 §5.5 的 TP-1～TP-3，依 O-1 改編號，內容不變）及其他
| ID | 情境 | 預期 |
|---|---|---|
| TN-1 | 網路封鎖 | 0 次連線 |
| TN-2 | 靜態檢查 | 新模組不 import `cert_runtime`／`cert_service`／`api_client`／`report_monitor`／`auto_control`／`bess_request`；不呼叫 `generate_report`、`write_quality_metrics`、`write_offgrid_record` |
| TN-3 | 寫入守門 | 寫到 MERGE 之外 → 例外中止 |
| T-X | 重產明細 vs 原始報告 | 只允許 X1～X9；檔名與屬性聲明正確；工作表名稱 ≤31、不重名 |
| T-R | 認證相關小集合回歸 | 全過 |
