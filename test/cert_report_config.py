# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— P1 離線核心參數
========================================
本檔只供認證報告（cert_*）使用，**與單次報告 charge_discharge_report_config.py 完全獨立**：
修改這裡不會影響單次報告的取樣週期、門檻或格式。

⚠️ 參數狀態（PARAM_STATUS）：
  - "confirmed"  ：使用者已確認
  - "provisional"：僅供離線測試的暫用值，**尚未定案，不可視為現場正式設定**
報告的 Summary 會逐項列出這些參數與狀態。
"""

# ---- 方案 B 停頓判斷 --------------------------------------------------------
W_PAUSE_SEC = 60            # 停頓判斷時間：B 起 ≤ 60 秒恢復同向 → 合併（剛好 60 秒算在內）
TCONF_SEC = 15              # 階段開始／恢復確認門檻：同方向連續觀測達此秒數才算成立
GAP_G_SEC = 30              # G：相鄰兩筆有效取樣（中間無失敗紀錄）允許的最大間隔
SAMPLE_INTERVAL_SEC = 10    # 認證取樣週期（P1 離線回放沿用來源資料的實際間隔，此值僅供文件與合成測試）

STOP_WARN_SEC = 30          # 停止指令被接受後仍未觀測到待機的提示時間（只提示，不重送指令）
CMD_STAGE_MATCH_SEC = 120   # 控制紀錄對應階段：充／放電指令最多早於階段首筆觀測幾秒；停止指令最多晚於階段結束幾秒

# ---- 每分鐘時點值 -----------------------------------------------------------
MINUTE_TOL_SEC = 20         # 整分 T 前後容許範圍：取 |t − T| 最小的一筆（同距取較早者）

# ---- 方向判定（觀測值 → 充電／放電／待機）----------------------------------
# 優先用電池電流（充正放負）；沒有電流才用 PCS 功率。
# 依據（2026-10-27 samples.csv 唯讀檢查）：停止後 battery_current_a 立即為 0.0，
# 但 actual_active_power_kw 會延遲約 15～20 秒仍顯示運轉值，待機時又顯示約 −1.2 kW（待機耗電），
# 以功率判斷會把待機誤判成放電。
IDLE_CURRENT_A = 1.0
IDLE_POWER_KW = 0.1

# ---- 欄位有效範圍 -----------------------------------------------------------
SENSOR_VALID_RANGE = None   # 多功能傳感器合理溫度範圍：未定案 → None = 不套用範圍檢查

# ---- P2 截圖（全部為暫用測試值，未定案；可在呼叫時 override）--------------
SHOT_PAGE_TIMEOUT_SEC = 30      # 單次嘗試：開頁到區塊就緒的最長等待
SHOT_POLL_SEC = 1.0             # 等待區塊就緒的輪詢間隔
SHOT_SETTLE_SEC = 2.0           # 區塊就緒後再等一下（等數值／動畫穩定）才截圖
SHOT_ATTEMPTS = 3               # 每個目標最多嘗試次數（含第一次）
SHOT_RETRY_DELAY_SEC = 5        # 失敗後到下一次嘗試的間隔
SHOT_ATTEMPT_HARD_TIMEOUT_SEC = 60  # 單次嘗試的外部看門狗（超過即判逾時、強制結束 Chrome）
SHOT_STALE_SEC = 60             # 頁面「更新時間」落後 PC 擷取時間超過此秒數 → 資料未更新
                                # （即時熱度頁的更新時間是瀏覽器時間，重新開頁時幾乎等於擷取時間，
                                #   無法代表設備資料新舊；資料有效性改由區塊內容驗證）
SHOT_VIEWPORT = (1920, 1080)    # 截圖視窗（沿用已確認成品）
SHOT_PAD_PX = 8                 # 區塊四周留白（沿用已確認成品）

# ---- P2 即時熱度區塊驗證（6161 頁面；暫用值，可依現場設定調整）-------------
# 2026-10-27 有資料時確認：獨立櫃 110 為 Pack 1～14、每 Pack 20 格 Cell 溫度。
# 頁面原始碼（2026-10-28 唯讀讀取）：API 回傳空資料時，頁面以 19 個 Pack、全部「-」的預設版面顯示，
# 「更新時間」為瀏覽器本機時間（formatDate(new Date())），不是設備資料時間。
HEAT_CABINET_NO = "110"                         # 目標櫃號（區塊內只能有這一櫃）
HEAT_PACK_IDS = tuple(range(1, 15))             # 該櫃完整 Pack 編號集合（缺漏、重複、多出都判範圍不符）
HEAT_CELLS_PER_PACK = 20                        # 每個 Pack 的 Cell 溫度格數
HEAT_MAX_MISSING_CELLS = 0                      # Cell 溫度允許缺值（「-」）格數上限；超過即判資料無效
HEAT_REQUIRED_FIELDS = ("最大溫度", "最小溫度", "溫差")         # 必要欄位（Cell 極值）：必須有數值
HEAT_OPTIONAL_FIELDS = ("Sensor", "環境溫度", "櫃內溫度", "模組溫度", "當前SOC", "當前功率")
# ↑ 允許缺值欄位：顯示「-」或「?」不拒絕截圖，只記錄於說明列。
#   已知：Sensor 因多功能傳感器關閉而顯示「-」。

# ---- P3 服務整合（全部為暫用測試值，未定案；不是現場正式設定）---------------
# 功能總開關：False → 服務不啟動認證執行緒、選單只顯示「未啟用」，單次報告行為與 P3 前完全相同。
# 🔴 預設關閉：程式已在磁碟上，但部署（含服務重啟）與實機驗收另行確認後才開啟。
CERT_MODE_ENABLED = True
CERT_SUBDIR = "cert_reports"            # output/<此資料夾>：認證資料、請求與結果
CERT_LOOP_SEC = 1.0                     # 服務端認證執行緒的輪詢間隔（處理請求、檢查是否該取樣）
CERT_REQUEST_EXPIRE_SEC = 300           # 請求建立後超過此秒數仍未處理 → 標示逾期、不執行（避免延遲執行開始/結束）
CERT_MENU_WAIT_SEC = 20                 # 選單送出請求後等待結果的時間；逾時顯示「結果待確認」，可再查詢
CERT_HEAT_API_URL = "http://192.168.70.201:6161/api/temperature?no=1"   # 傳感器／PCS 模組溫度來源（唯讀 GET）
CERT_HEAT_API_TIMEOUT_SEC = 3
CERT_SCREENSHOT_ENABLED = True          # 階段截圖（P2）；False → 只記錄資料，不截圖

# ---- 報告外觀 ---------------------------------------------------------------
# 電櫃規格（例 340KW、250KW）與循環數屬「測試資訊」，每次測試輸入（報告 meta 的 cabinet_spec／cycles），
# 不在此寫死。電櫃規格 ≠ 測試設定功率（例：定功率 170 kW）；循環數的判定方式未定案，不自動推算。
NA = "N/A"

PARAM_STATUS = {
    "W_PAUSE_SEC": "confirmed",
    "TCONF_SEC": "provisional",
    "GAP_G_SEC": "provisional",
    "SAMPLE_INTERVAL_SEC": "provisional",
    "STOP_WARN_SEC": "provisional",
    "CMD_STAGE_MATCH_SEC": "provisional",
    "MINUTE_TOL_SEC": "provisional",
    "IDLE_CURRENT_A": "provisional",
    "IDLE_POWER_KW": "provisional",
    "SENSOR_VALID_RANGE": "provisional",
}

PARAM_LABELS = {
    "W_PAUSE_SEC": "停頓判斷時間 W（秒；剛好 60 秒算在內）",
    "TCONF_SEC": "階段開始／恢復確認門檻（秒）",
    "GAP_G_SEC": "G：無失敗紀錄時允許的最大取樣間隔（秒）",
    "SAMPLE_INTERVAL_SEC": "認證取樣週期（秒）",
    "STOP_WARN_SEC": "停止指令後未觀測到待機的提示時間（秒）",
    "CMD_STAGE_MATCH_SEC": "控制紀錄對應階段的時間容許（秒）",
    "MINUTE_TOL_SEC": "每分鐘時點值容許範圍（±秒）",
    "IDLE_CURRENT_A": "待機判定：|電池電流| ≤ (A)",
    "IDLE_POWER_KW": "待機判定（無電流時）：|PCS 功率| ≤ (kW)",
    "SENSOR_VALID_RANGE": "多功能傳感器合理溫度範圍",
}


def params(**override):
    """回傳目前參數 dict；override 只影響這次呼叫（測試／回放用），不改模組值。"""
    p = {k: globals()[k] for k in PARAM_STATUS}
    p.update(override)
    return p
