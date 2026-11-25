# C／r8 相容性補充：中止階段排除報告格式（stopped_stages_excluded=v1）

本文件是 r8（`Cert_Merge_Report_Design_r8.md`）的獨立補充；r7、r8 原文不修改。
適用：單一認證報告新增「有效 Flag 為中止的階段全面排除」後，整合工具（cert_merge_*）對新舊兩種來源報告的驗證規則。

## 1. 單一報告的新規則（cert_report_xlsx）
- 判斷只有一處：`split_stopped(res, enabled)`；只有 `meta["exclude_stopped_stages"]` 為真時，`Stage.label == "中止"`（該階段 stage_label 最後一筆決策）的階段被排除。未標記、未滿、進行中但未標記的階段一律保留。
- 旗標只由 `cert_runtime.generate_report` 設定（Menu 25 重產、結束認證產報）；直接呼叫 `save_report` 預設不排除（舊格式）。
- 所有工作表共用同一份排除結果：階段頁、Summary（圖表、箭頭）、數據概覽、即時熱度、整體測試、Raw Data。
- 第 N 次以 `stage_names(有效階段)` 重新計算；原始 `Stage.no`、觀測、Session 檔案不變。
- 整體測試保留完整原始時間軸；中止區間分鐘 N/A，備註「已排除中止階段」，圖表空白（曲線斷開）。
- Raw Data：舊 Session 依 `obs.stage_no` 排除；1 秒格點：LIVE 依來源觀測歸屬、區間內非 LIVE 排除、區間外沿用中止讀值的列保留時間但數值 N/A（說明「沿用來源屬已排除的中止階段，不顯示數值」）；來源缺失只依格點秒判斷。
- 描述：排除說明（方向、開始、結束、觀測筆數、Raw Data 排除／改 N/A 列數；全部中止時註明沒有有效充放電階段）放在 stage_count 計次描述**之前**（X10 要求計次描述在最後）。
- 格式識別：活頁簿屬性 `category = stopped_stages_excluded=v1`（旗標開啟時寫入）；dec_v1 keywords 不變。

## 2. 整合工具的格式選擇
| 原始報告 category | 視為 | 重產規則（R_base／R_all） |
|---|---|---|
| 空白 | 舊格式（完整階段） | 不排除（與 r8 相同） |
| `stopped_stages_excluded=v1` | 新格式 | 排除（與 generate_report 相同） |
| 其他任何值 | 不支援 | 不猜測 → 該 Session **UNVERIFIABLE**（原始報告格式識別不支援） |
- 沒有原始報告的 Session：重產採舊規則（本來就 UNVERIFIABLE，無比較基準）。
- 重產規則由 `cert_merge_xlsx.detail_meta` 依該 Session 比較基準報告的 category 決定；驗證端 `cert_merge_verify.verify_session` 讀取同一欄位判斷未知格式。

## 3. 新格式下 r8 規則的套用
- 第一階段（原始報告 vs R_base）：F3、V-S、V-O、V-R、V-D、V-T 規則不變（嚴格比對）；Raw Data 比對的重產每秒列使用與 build_workbook 相同的 `exclude_stopped_raw_rows`。
- 第二階段（R_base vs R_all）：名稱、頁籤與 FMT 標題底色推導改用排除後的有效階段；X10（計次描述、歷史、孤兒）仍依完整 Result（與 save_report 相同）。
- 新增決策使中止階段集合改變（某階段變成或不再是中止）→ 結構差異無法依 r8 名稱規則歸因 → 第二階段 **UNVERIFIABLE（STOPPED_STAGE_SET_CHANGED）**，不放寬、不判 VERIFIED。

## 4. 可偵測範圍（限制）
- 格式識別被移除或改寫，且報告實際內容與宣稱格式不同（有中止階段）→ 依錯誤規則重產必然與原始報告不符 → FAILED（不會通過）。
- 沒有中止階段時新舊格式內容完全相同；此時格式識別被移除或加上無法、也不需要區分（重產內容一致 → VERIFIED）。格式識別不是防竄改機制。

## 5. 不變範圍
- 跨 Session 整合報告的呈現、Cycle 配對及保護規則、stage_count（Menu 23-3）功能與歷史決策、dec_v1、r7／r8 原文。

## 6. 驗收對應
- 單一報告：`test_cert_exclude_stopped.py`（EX-01～EX-30）。
- 新舊格式：`test_cert_merge_report.py` 3S FX-01～FX-08。
