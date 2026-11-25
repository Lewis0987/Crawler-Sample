# 跨 Session 整合認證報告工具 — 設計規格 r8（G5-B：H2 stage_count／G6 參數相容）

## 0. 文件資訊
- 版本：r8。r7 正文（`docs/Cert_Merge_Report_Design.md`，SHA-256 `3766d6f951f9acd5b4e1a03de400dbce8d1243893b58b88fbd97e57ea15df591`）**不修改**；本文件記錄 r7 → r8 的全部修訂條文，未列出的 r7 條文全部沿用。
- 設計依據：G5-B 設計草案 r1（`G5B_r7_compatibility_revision_DRAFT.md`，SHA-256 `0ed48f63…`）、r2（`G5B_design_DRAFT_r2.md`，SHA-256 `2b4fe3f86268b19a…`），以及使用者對保護②、T23-5、Phase 3S、舊報告請求時間、STOP3、STOP4 的裁定；原文保留於 `D:\Crawler WT\g5g6_evidence_20261124\g5b\` 與 `g5b_impl\`。
- 程式基準：dev `feature/g3-ab @ 6b738c9` 加 19 個變更檔。G5-B 修改了 `test/cert_merge_analysis.py`、`cert_merge_verify.py`、`cert_merge_report.py`、`cert_merge_xlsx.py`、`test_cert_merge_report.py`；H2 引擎、`cert_report_xlsx.py`、`cert_runtime.py`、`cert_menu.py`、`cert_requests.py`、`cert_merge_source.py` 都沒有修改。

## 1. r7 → r8 章節對照
| r7 章節 | r8 修訂 | 本文件 |
|---|---|---|
| §4.2 前置驗證 V1～V9 | 新增 V10：Session 原始參數不合法 → rc=2，不建立任何輸出 | §7 |
| §6.1 每 Session 獨立分析 | 參數只取 cert_state 原值的已知鍵，不以 CFG.params() 補值；stage_count 只採用 H2 結果 | §3、§7 |
| §6.2 配對掃描 | 選項甲：有效不計次階段不參與配對 | §4 |
| §6.3 配對確定性 | 保護①②③；保護②窗口定義 | §4 |
| §6.4 確認狀態（P-7） | 不變；保護③明定不放寬 | §4 |
| §6.5 對應不到的紀錄（P-9） | 新增種類「無法對應的計次決策」 | §3 |
| §6.6 計數不變式 I1～I8 | 不變 | — |
| §10.2 各 Session 的欄位 | 新增三種保證：決策快照、報告重現性、可追溯性 | §2 |
| §10.3 比較基準 | 基準決策集合：新版 dec_v1（前 N 筆），舊版時間路徑 | §5 |
| §10.4、§10.6 F3 與逐項驗證 | 第一階段改比對「原始報告 vs R_base」，規則不變；第二階段「R_base vs R_all」差異歸因 | §6 |
| X 表（X1～X9） | 新增 X10（計次描述）、FMT（H2 標題底色欄數）只用於第二階段；X7 只保留給基準不可確認的舊報告 | §6 |
| §11 STATUS／manifest | 新增三種保證欄、不計次階段、計次決策、決策異常、變更清單 | §2、§8 |
| §15 離線測試規格 | 新增 Phase 3S；T23-5 預期依核准變更 | §10 |

## 2. 三種保證（分開陳述，不互相推論）
| 保證 | 新版報告（有 dec_v1 keywords） | 舊版報告（沒有 keywords） |
|---|---|---|
| ① 決策快照一致性（`decision_snapshot`） | dec_n 與 dec_sha256 相符 → CONFIRMED；不符、資料不足或格式錯誤 → UNVERIFIABLE | 時間路徑全部條件成立 → PRESUMED；任一條件不成立 → UNVERIFIABLE |
| ② 報告內容重現性（`reproducibility`） | 原始報告 vs R_base 嚴格比對：VERIFIED 或 FAILED | 推定成立時可判 VERIFIED；有差異 → UNVERIFIABLE（基準可能配錯，不判 FAILED） |
| ③ 原始決策集合可追溯性（`traceability`） | 雜湊相符 → 已確認 | 一律「推定（未確認）」，即使 ② 是 VERIFIED |
- request_id 重複等決策異常 → 可追溯性 UNVERIFIABLE。
- STATUS 與 manifest 各自分欄記錄，不合併成單一「通過」。GU-03 證明 CONFIRMED 與 FAILED 可同時成立。

## 3. stage_count 辨識（只採用 H2 結果）
- 有效性只由 H2 引擎判定（`Stage.counted／count_reason／count_note／count_history`、`Result.count_orphans`），C 不重新實作。
- P-9 新增「無法對應的計次決策」（來源 `count_orphans`）。其他未知類型仍列為「無法辨識的紀錄」。
- manifest 記錄每筆計次決策：行號、stage_start、counted、原因、at、by、request_id；另記錄決策異常（request_id 重複）。

## 4. 選項甲配對與保護①～④
- 套用條件：有效標記是「中止」，而且 H2 判定 `counted is False`。不計次階段標為「不計次（不參與配對）」；原始階段、量測資料、階段頁、Raw Data、統計全部保留。
- 保護①：只移除不計次階段本身，剩下的序列重新判斷 A1／A2（PA-02）。
- 保護②（裁定）：A＝前一階段最後運轉時間，B＝下一階段開始時間，W＝該 Session 原始 `W_PAUSE_SEC`。窗口 [A−W, B+W] 含端點；任何未決停頓區間與窗口重疊就觸發。交界為 `operator_split` 或邊界不確定也觸發。觸發時相鄰循環仍套用 A1／A2，並加註。不寫死 60 秒，也不使用 CFG.params()（PA-B01～B10、PA-B12）。
- 保護③：P-7 確認狀態與其他待確認理由不受影響（PA-B09）。
- 保護④（裁定：083020 實際資料驗收項目）：5 個階段、1 個不計次、4 個參與配對、2 個循環；保護②不觸發；第 2 階段 7 個、全部 20 個未決停頓保留；循環 1 確認狀態為「待確認」（PA-B11、§11 實際驗收）。
- 沒有 stage_count 的 Session：配對與 r7 完全相同（PA-B13、既有 T-K）。

## 5. 基準決策集合
### 5.1 新版報告（dec_v1）
- keywords 格式為 `dec_v=1;dec_n=N;dec_sha256=…`。取目前 decisions.jsonl 前 N 筆解析成功的紀錄，以 H2 `decisions_digest` 重算。
- 以下任一情況 → UNVERIFIABLE：雜湊不符、紀錄少於 N 筆、前 N 筆範圍內有無法解析的行、版本不是 1。keywords 格式錯誤時仍走新版路徑，不降級為舊版時間路徑（NK-04b，案例 10）。
- 黃金值：空清單 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`；`[{"b":1,"a":"中止"},{"type":"x","v":true}]` → `3771fb150709bab134744b9711584b767822760d127715fb4d3567a22d3ec5a5`（NK-07）。
### 5.2 舊版報告（時間路徑）
- 產生時間取自 cert_state.reports 與 supplements 紀錄，兩者必須一致。時間帶時區 → UNVERIFIABLE。
- 基準報告必須是結束認證報告（由 events.jsonl 對應）。Menu 25 重產、沒有可靠請求時間的舊報告 → UNVERIFIABLE（裁定：不擴充快照，不讀正式版 Request Queue）。
- 模糊窗口為 [認證結束−120 秒, 產生時間+120 秒]，含端點；窗口內有任何決策 → UNVERIFIABLE。
- 有新增決策時，以下條件必須全部成立：
  - 決策時間可解析，且依行序不遞減；
  - 新增決策都晚於認證結束；
  - 輔助檢查通過：報告檔修改時間與產生時間相差 ≤120 秒，decisions.jsonl 修改時間不早於最後一筆決策。
- 超過 120 秒本身不構成 VERIFIED 的理由，仍要通過嚴格逐格比對（LG-03／LG-06）。
- 快照無法判定 → 回到 r7 比對（含 X7），D-S 項目判 UNVERIFIABLE（LG-08，案例 7）。

## 6. 資料值驗證兩階段
### 6.1 第一階段：原始報告 vs R_base（規則不變）
- R_base 以基準決策集合重產。F3、V-S、V-O、V-R、V-D、V-T 全部沿用 r7，沒有任何新增豁免。X10 與 FMT 都**不**適用此階段（X10-N5、FMT-10）。
- T23-5（核准選項 A）：新版報告的階段頁被竄改，之後才加 stage_label → FAILED、`UNEXPLAINED_VALUE_DIFF`、decision_snapshot CONFIRMED。
### 6.2 第二階段：R_base vs R_all 差異歸因（D-A）
- 新增決策只有 stage_label 或 stage_count 時，可接受的差異只有以下幾種：
  - 由 H2 `stage_names`／`_stage_title`／`_sheet_name` 推導的名稱、標題、頁籤差異；
  - X10 計次描述；
  - FMT 標題底色格（§6.4）。
- 任何數值、列數、結構、表格、合併儲存格差異 → FAILED `DECISION_DIFF_OUT_OF_SCOPE`。
- 名稱對照可以一對多：不計次階段的標題與名稱都是「中止充電（不計次）」，恢復計次後分成「第N次充電（標記）」與「第N次充電」，所以一個舊字串可能有兩個候選。限制如下：
  - 每個舊字串只能來自同一個原始 Stage.no，否則 FAILED；
  - 每處以單次替換取一個候選；
  - 其他階段的名稱不被接受（FMT-20b）；
  - 合法的同類名稱（附時間區分）不會被誤拒（FMT-20a）。
- 新增決策含 continuity 等結構性決策時：差異完整列出，Raw Data 時間與數值欄仍必須不變。
### 6.3 X10 計次描述
- 描述在最後的 stage_count_notes 處拆開，非計次行（包括 params_note）必須逐字相同。原有決策歷史必須以前綴形式保留，k/n 允許合法重新編號。描述達 3,000 字元 → UNVERIFIABLE（X10-P1～P3、X10-N1～N6）。
### 6.4 FMT：H2 標題底色欄數（STOP3、STOP4 裁定 A2）
- H2 規則（`cert_report_xlsx.py`，不修改）：
  - `_title_cols(titles)`：px＝所有區塊標題中最大的 Σ(全形 19／半形 10)，tcols＝⌈(px＋30)／58⌉；
  - `_title_row` 在 (r, c0…c0+tcols−1) 上設底色 `TITLE_FILL`（solid F4B183），只有 c0 有文字（14pt 粗體）；
  - 呼叫者只有 `_write_overview`（數據概覽，各區塊標題在第 1 欄）與 `_write_heat`（即時熱度，第 1 列，區塊間距 pitch）；
  - 同一頁所有標題列共用同一個 tcols，所以文字沒變的標題列也會同步延伸或縮減。
- 只適用第二階段，只適用這兩張頁，並且新增決策必須是 stage_label 或 stage_count。
- 推導步驟：
  1. 依 `screenshot_slots`、`_slot_title`、`_title_cols` 推導 R_base 與 R_all 的標題集合與 tcols；
  2. 每個標題在頁上恰好出現一次、依序，且是 H2 標題格；
  3. 實際底色欄數必須等於推導值，兩邊都要符合；
  4. 預期集合＝每個標題列 c0+min(tcols)…c0+max(tcols)−1。
- 逐格條件：
  - 底色較多的一邊：沒有值、沒有公式、沒有超連結、沒有註解，格式屬性等於 H2 只設底色的格；
  - 另一邊：不存在或是空白的預設格式。
  - 格式以屬性比對，不依 style_id。
- 預期集合與實際差異集合必須**完全相等**：多一格或少一格都判 FAILED（FMT-19）。
- 兩張頁的其他格嚴格比對值、型別、格式、超連結、註解；合併儲存格、表格、欄寬、列高、圖片／圖表也比對。
- 不使用固定白名單或固定格數。格數依階段數和標題寬度不同（FMT-18：6 個階段共 12 格）。
- 每格記錄：Session、工作表、格位、增加或減少、前後狀態、標題格與前後標題、px、tcols、決策行號、理由（manifest `D-A.detail.format_cells`）；規則推導資訊記錄在 `format_rule`。
- 即時熱度各區塊相距至少 12 欄，底色延伸不會改變整張頁的已使用範圍，但仍逐格驗證（FMT-13）。
- 083020 實際驗收：10 格。數據概覽 E1／E26／E51／E76／E101，即時熱度 E1／Q1／AC1／AO1／BA1；tcols 4→5（200→209 px），決策第 4 行。這是驗收結果，不是程式白名單。

## 7. G6 參數規則（V10）
- C 端逐字重建 G6 `session_params_check`（不 import cert_runtime）。
  - 必要參數：W_PAUSE_SEC、TCONF_SEC、GAP_G_SEC、STOP_WARN_SEC、SAMPLE_INTERVAL_SEC 須 >0；IDLE_CURRENT_A、IDLE_POWER_KW、CMD_STAGE_MATCH_SEC 須 ≥0；SENSOR_VALID_RANGE 為 null，或 [lo,hi] 有限且 lo<hi。
  - 相容參數：MINUTE_TOL_SEC。
  - bool、NaN、±Inf 一律拒絕；未知鍵保留並記錄。
- 不合法 → V10，rc=2，不建立輸出（PRM-C2）。
- 一致性以輸入矩陣在獨立子行程對照 cert_runtime 驗證，107 個輸入完全一致（PRM-C1／C1b）；原始碼 AST 比對只作輔助（PRM-C4）。

## 8. 呈現
- 「Session總覽」加三種保證表（B 欄不放 cert_id）。
- 「循環判定」加不計次階段表與循環加註。
- 「不確定停頓與標記紀錄」加計次決策歷史、決策異常、人工決策變更清單（只呈現，不參與判定）。
- manifest 增加：count 欄位、uncounted_notes、stage_count_records、stage_count_orphans、decision_anomalies、data_value_verification.decision_verification／change_list，以及 D-A 的 rename_map、x10、format_rule、format_cells。

## 9. UNVERIFIABLE 規則彙整
- r7 原有情況：沒有原始報告；基準為 `_中途`；沒有 slots；Raw Data 不是 13 欄；X7、X8。
- 新增：
  - NK-03、NK-04；
  - 舊版時間路徑任一條件不成立：窗口內有決策、時間無法解析、時區、行序不一致、輔助檢查不符、產生時間缺失、不是結束報告、request 與報告版本不符；
  - 舊版重放有差異；
  - request_id 重複（可追溯性）；
  - 描述截斷（X10）。

## 10. 不放寬對照表（2026-11-20 表）與測試證據
| 情況 | r7 | r8 | 證據 |
|---|---|---|---|
| 沒有新增決策，原始報告＝重產 | VERIFIED | VERIFIED | NK-01、LG-01、3A～3N、R1 |
| 沒有新增決策，有差異 | FAILED | FAILED | NK-05、3B 既有竄改案例 |
| 有新增決策（舊報告），無法可靠重建 | UNVERIFIABLE | UNVERIFIABLE | LG-04a、c～g、LG-07～LG-10、RC-03、LG-05 |
| 有新增決策（舊報告），可靠重建且嚴格相同 | UNVERIFIABLE | VERIFIED（唯一提升） | LG-02、LG-04b、LG-04h、R2 |
| 有新增決策（舊報告），可靠重建但有差異 | UNVERIFIABLE | UNVERIFIABLE | LG-03 |
| 新版報告，雜湊相符且嚴格相同 | — | VERIFIED | NK-01、NK-02、RC-01、MR-04、R3 |
| 新版報告，雜湊相符但有差異 | — | FAILED | NK-05、NK-05b～d、T23-5、FMT-10 |
| 新版報告，雜湊不符、資料不足或無法解析 | — | UNVERIFIABLE | NK-03、NK-04、NK-04b |
| 變更清單出現範圍外的數值或結構差異 | — | FAILED | MR-11、F3-02、X10-N1～N3、FMT-03～07、09、11、15、17、19、20b |

## 11. Phase 3S 完整測試矩陣（113 項，全部在 --work 內的合成 Session 或獨立副本）
- PRM：C1、C1b、C2、C3、C4
- SCR：01～07
- PA：01、02、04、B01～B13（B05a／b、B07a／b）、B11 系列（083020 副本，保護④）
- NK：01～07（含 05b～05d、04b）
- GU：01～03
- F3：01～03
- X10：P1～P3、P2b（控制組）、N1～N6（含 N2a／b）
- MR：04、05、09a／b、11；MR-01＝3A～3N，MR-02＝NK-02，MR-03＝NK-05c，MR-06＝NK／LG，MR-07＝LG-04，MR-08＝NK-03，MR-10＝RC-01／03，MR-12＝LG-05，MR-13＝PA-01／PA-B11
- RC：01～03
- LG：01～05、04a～h、07～10、RUN2
- FMT：01、01b、02～20（20a／b），RUN1～4
- 案例 1～10：案例 1 NK-01；案例 2 NK-05；案例 3～5 NK-05b～d；案例 6 NK-02；案例 7 LG-08；案例 8 LG-02；案例 9 LG-07；案例 10 NK-04b。

## 12. 已知限制
- 舊報告的原始決策集合只能推定；資料夾內沒有單調時鐘證據，時鐘調整只能部分偵測（LG-05）。
- Menu 25 重產、沒有可靠請求時間的舊報告 → UNVERIFIABLE（不擴充快照）。
- 第一階段沿用 r7，只比對值、範圍與表格，不比對格式（與 r7 相同，未放寬也未加嚴）。
- FMT 規則只涵蓋 H2 目前的 `_title_row` 呼叫者。H2 如果新增其他依標題寬度變化的版面規則，C 會判 FAILED（保守），需要另行裁定。

## 13. G-8 條件
- G5-B PASS、3S 全部通過、r8 經確認後，可考慮解除 dev「含 stage_count 的 Session 不得正式整合」的限制。
- 正式版使用要等 G7／G8 完成並另行授權。FIELD 維持 DEFERRED／HOLD。

## 14. STOP 與裁定紀錄
- STOP1（設計）：保護②定義 → 裁定（§4）。
- STOP2：T23-5 預期 → 核准選項 A。
- STOP3：083020 舊報告加不計次後，數據概覽標題底色 4→5 欄 → 判 BUG，核准方案 A（精確限制）。
- STOP4：H2 規則推導出即時熱度也有 5 格 → 核准 A2（兩張頁同一規則；083020 預期 10 格；不使用固定格數）。
- 原始紀錄：`g5b_impl\G5B_STEP0_and_P4_STOP_record.json`、`G5B_STOP2_*`、`G5B_STOP3_real_session_record.json`、`G5B_STOP4_title_fill_record.json`、`G5B_resume*_ruling_record.json`。

## 15. 正式驗收結果（2026-11-24）
- 程式版本：G5-B 正式驗收時的產品程式 SHA 記錄於 `g5b_impl\formal\code_SHA256_at_formal_start.txt`，其中 `cert_merge_verify.py` 為 `932f468a…`，`test_cert_merge_report.py` 為 `458bba5a…`；其餘三個 G5-B 檔案自 STOP2 起未再變動。
- Phase 3S：**113/0**（原 87 項全部保留，新增 FMT 26 項）。
- 3A／3B／3C／3R／3N：**21/0、262/0、18/0、27/0、102/0，合計 430/0**。測試編號集合與 G5-A 相同；T23-5 依核准選項 A 通過。
- 實際 Session 副本（`g5b_impl\real_formal\formal_acceptance.json`，ALL_PASS）：
  - R1（143431、111816、083020 原樣）：全部 VERIFIED。143431 為 CONFIRMED／已確認，另兩者為 PRESUMED／推定。
  - R2（083020 舊報告＋Menu 23 不計次）：VERIFIED、PRESUMED／推定。合法底色格 10 格，與獨立推導的集合完全相等，全部對應決策第 4 行。
  - R3（另經 Menu 25 重產）：VERIFIED、CONFIRMED。
  - 083020 配對：5／1／4／2；保護②未觸發；循環 1 為「待確認」；R2 與 R3 的配對結果一致。
- 不放寬對照表：每列都有正式 PASS 證據（`g5b_impl\formal\non_loosening_evidence.json`）。
- 完整 Regression：
  - 第一次（tag g5b）在第 30 個套件 `test_phase4_observer` 停止，結果 62/2（STOP5，原始紀錄保留）。
  - 根因：同一 ROOT 內兩個進行中 Session 的資料夾 mtime 打平，由路徑排序決勝，選中了 E 段 Session（隔離診斷重現：getmtime 均為 1795513334.6906407，st_mtime_ns 均為 1795513334690640800）。分類為 CONFIRMED MTIME TIE／TEST ISOLATION ISSUE；原始 STOP5 維持 D。
  - 經核准 T1：只修改 `test_phase4_observer.py`，讓 E 段使用獨立的 E_ROOT；64 項 assertion 不變。專項執行 64/0。
  - 新 tag g5b_t1 完整 Regression：**9339 PASS／12 FAIL**，106 個套件（執行 102、SKIP 2、SKIP-CLI 1、NOT RUN 1），與 G6 逐套件相同，網路嘗試為 0。
- 獨立追蹤事項 **F-W**（非 Owner 在 Writer Gate 之前寫入 Session，位於 `report_monitor.report_start` 路徑）：與 G5-B 無影響路徑，狀態依使用者裁定（不得標示 PASS／CLOSED／WAIVED）；見 `g5b_impl\fw_readonly_analysis\FW_readonly_analysis.json`。
- 部署：本文件不構成正式部署就緒。G7／G8／G9 與 FIELD 維持 HOLD。
