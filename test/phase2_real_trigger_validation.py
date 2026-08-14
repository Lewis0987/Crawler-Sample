# -*- coding: utf-8 -*-
"""
Phase 2 實機驗證 —— B. 真實排程觸發階段（黑箱驗證）
======================================================================
驗證「智慧排程自動建立充放電報告」在**實機**上的行為。走 device_control_menu 的
**正式程式路徑**（_monitor_client → _read_device_state → auto_schedule_check →
_report_start_session），不 monkeypatch 任何正式函式、不建立第二套 report state。

⚠️ 本腳本**只讀 + 觀察**，絕不控制設備。全檔不含任何控制 API：
   不切換智慧/手動模式、不開關排程主開關、不新增/修改/刪除排程、
   不送充電/放電/停止、不控制電池上下電、不修改任何設備參數。
⚠️ 不會自行把 AUTO_SCHEDULE_REPORT_ENABLED 由 False 改為 True —— 必須由使用者
   先在 charge_discharge_report_config.py 確認後才能執行（否則腳本安全退出）。
⚠️ 測試結束不對 PCS 發送停止命令。報告如何結束由現場人員與既有 Menu 決定。

⚠️執行前需由現場完成測試環境（本腳本不會修改設備）
    本腳本僅驗證「智慧排程是否自動建立報告」。
    因此執行前請確認：
    1. PCS 已切換為智慧模式
    2. 排程主開關已開啟
    3. 已建立一筆可於近期觸發的充電排程
    4. AUTO_SCHEDULE_REPORT_ENABLED=True
    以上均由使用者或 HMI 完成，
    本腳本不會修改任何設備設定，
    若條件未成立，僅會退出或等待，不會自動建立環境。

使用方式：
    cd "D:\\Crawler Sample\\test"
    python phase2_real_trigger_validation.py                # 實機驗證（需先完成上述前置條件）
    python phase2_real_trigger_validation.py --selftest     # 離線自我測試（不連設備、純 fixture）
    python phase2_real_trigger_validation.py --wait-min 30  # 最長等待充電開始的分鐘數（預設 20）

輸出：
    output/phase2_real_trigger_validation_YYYYMMDD_HHMMSS.log
    （已遮蔽帳號/密碼/Token/Authorization；不寫入任何機密）
"""

import argparse
import csv
import io
import os
import re
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
OUTPUT_DIR = os.path.join(os.path.dirname(HERE), "output")

# ---- 驗證節奏（與正式 Dashboard 一致，不另訂一套）----
AUTO_CYCLES_AFTER_START = 4       # 建立後連續「自動刷新」輪數（驗證不建立第二份）
MANUAL_CYCLES_AFTER_START = 1     # 建立後「手動刷新」輪數（驗證不建立第二份）
LEAVE_LOOP_QUIET_SEC = 20         # 離開監看迴圈後的靜默觀察秒數（需 > AUTO_REFRESH_SEC）
REENTER_CYCLES = 2                # 再次進入監看的輪數（驗證只接續、不建立第二份）

# ---- 人工確認關卡（僅本驗證腳本內部；不影響正式 Menu 的輸入函式）----
CONFIRM_TIMEOUT_SEC = 120         # 人工確認等待秒數；逾時＝取消（腳本不會永久卡住）
CONFIRM_TOKEN = "YES"             # 必須**精確**輸入（大寫）才繼續；其他一律取消
CANCEL_MSG = "3-B 驗證已取消，未進入觸發監看。"
# 排程項目方向 enum 與型別正規化：**引用 config 的唯一定義**，不再各自維護副本
# （Phase 3.6 / D-1 整併；原本 menu 與本檔各有一份，新增方向時需改多處）。
from charge_discharge_report_config import SCHED_CD_CODE as SCHED_CD  # noqa: E402
from charge_discharge_report_config import as_int as _as_int          # noqa: E402

# ---- 驗證判定三態 ----
# PASS：確認通過　FAIL：確認失敗（真的有問題）
# SKIP：本次驗證條件下**外部不可觀察**（Not Observable），非功能失敗，不併入 FAIL 計數
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# ======================================================================
# 輸出：同時寫 stdout 與 log，並遮蔽機密
# ======================================================================
_SECRET_PAT = re.compile(
    r"(?i)(authorization|bearer\s|token|password|passwd|\bpwd\b|secret|"
    r"publickey|public_key|sm2|encrypt|cipher)")


def scrub(line, username=None):
    """遮蔽含機密關鍵字的輸出行；帳號一律以 *** 取代。回傳可安全寫入 log 的字串。"""
    text = str(line)
    if username:
        text = text.replace(str(username), "***")
    if _SECRET_PAT.search(text):
        return "[已遮蔽：該行含機密關鍵字，未寫入 log]"
    return text


class Log:
    """Tee：同時輸出到畫面與 log 檔（log 內容經 scrub）。"""

    def __init__(self, path, username=None):
        self.path = path
        self.username = username
        self.lines = []

    def __call__(self, text=""):
        print(text)
        self.lines.append(scrub(text, self.username))

    def section(self, title):
        self("")
        self("=" * 78)
        self(title)
        self("=" * 78)

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with io.open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")
        return self.path


# ======================================================================
# 純函式核心（無 I/O、無網路）—— --selftest 以 fixture 直接驗證這一層
# ======================================================================
BLOCK_REASONS = {
    "auto_report_disabled": "AUTO_SCHEDULE_REPORT_ENABLED=False（請先由使用者在 config 確認後改為 True）",
    "login_failed": "登入失敗，無法取得共用 client",
    "no_reading": "reading 取樣失敗（API 無回應或格式非預期）",
    "pcs_fault": "PCS 故障中（pcs_fault_flag=True）",
    "flag_conflict": "charging 與 discharging 旗標同時為 True（設備狀態異常）",
    "unknown_session": "已有來源不明的 active ReportSession（請先以 Menu 停止並產生報告）",
    "battery_off": "電池未上電，不適合本次充電驗證",
    "discharge_only": "偵測到 discharge：本次驗證僅允許 charge，不建立／不接續本次測試",
}
WAIT_REASONS = {
    "mode_not_smart": "PCS 控制模式尚非智慧模式 → 唯讀等待（本腳本不會自行切換）",
    "schedule_off": "排程主開關尚未開啟 → 唯讀等待（本腳本不會自行開啟）",
    "idle": "尚未開始充電（方向 idle/unknown）→ 唯讀等待排程觸發",
}


def preflight(reading, *, auto_enabled, active_session, battery_power_status,
              session_is_known):
    """
    啟動前安全判定（純函式）。回傳 (verdict, reason)：
      verdict ∈ "block"（安全退出）/ "wait"（唯讀等待，不修改設備）/ "ready"（可進入驗證）
    """
    if not auto_enabled:
        return "block", "auto_report_disabled"
    if not isinstance(reading, dict):
        return "block", "no_reading"
    if active_session is not None and not session_is_known:
        return "block", "unknown_session"
    chg, dis = reading.get("pcs_charging_flag"), reading.get("pcs_discharging_flag")
    if chg is True and dis is True:
        return "block", "flag_conflict"
    if reading.get("pcs_fault_flag") is True:
        return "block", "pcs_fault"
    if battery_power_status == "已下電":
        return "block", "battery_off"
    if dis is True:                       # 本次僅允許充電
        return "block", "discharge_only"
    if reading.get("pcs_control_mode_code") != "smart":
        return "wait", "mode_not_smart"
    if reading.get("pcs_schedule_enabled") is not True:
        return "wait", "schedule_off"
    if chg is not True:
        return "wait", "idle"
    return "ready", "charging"


def schedule_plan_direction(client, tpl_path, item_path_fmt):
    """
    唯讀取得「**已啟用**排程」的配置方向：charge / discharge / none / unknown。
    只呼叫既有唯讀 GET（/schedule/template/list、/schedule/list/{tempId}）；
    **不新增、不修改、不啟用、不停用任何排程**，也不以排程時間推論設備已開始充電。
    僅供 preflight 顯示與「明確為 discharge 時攔阻」；真正建立報告仍只依
    systemChargingStatus.oldValue（由正式 auto_schedule_check 判斷）。
    回傳 (direction, detail)。
    """
    try:
        tpls = client.get(tpl_path)
    except Exception as e:
        return "unknown", f"排程模板清單讀取失敗：{e}"
    if not isinstance(tpls, list):
        return "unknown", "排程模板清單格式非預期"
    enabled = [t for t in tpls if isinstance(t, dict) and t.get("enableFlag") in (1, "1")]
    if not enabled:
        return "none", "無任何已啟用排程模板"
    dirs, detail = set(), []
    for t in enabled:
        tid = t.get("tempId")
        try:
            items = client.get(item_path_fmt.format(tid=tid))
        except Exception as e:
            return "unknown", f"排程項目讀取失敗（tempId={tid}）：{e}"
        if not isinstance(items, list):
            return "unknown", f"排程項目格式非預期（tempId={tid}）"
        for it in items:
            if not isinstance(it, dict):
                continue
            d = SCHED_CD.get(_as_int(it.get("chargeOrDischarge")), "unknown")
            dirs.add(d)
            detail.append(f"{t.get('tempName')}: {it.get('startTime')}~{it.get('endTime')} "
                          f"{d} {it.get('instantaneousPowerLimit')}kW SOC{it.get('soc')}%")
    if not dirs:
        return "none", "已啟用排程模板底下沒有任何排程項目"
    if "discharge" in dirs:                      # 只要有放電項目就攔阻（本次僅驗證充電）
        return "discharge", "；".join(detail)
    if "charge" in dirs:
        return "charge", "；".join(detail)
    if dirs == {"none"}:
        return "none", "；".join(detail)
    return "unknown", "；".join(detail)


def plan_direction_verdict(direction):
    """
    排程配置方向 → 是否放行（純函式）。
      discharge          → ("block", "plan_discharge")  明確配置放電，直接安全退出
      charge/none/unknown→ ("ok", direction)            交由人工確認，不因此修改設備
    """
    if direction == "discharge":
        return "block", "plan_discharge"
    return "ok", direction


def confirm_gate(log, reader, soc, plan_direction, plan_detail="",
                 timeout=CONFIRM_TIMEOUT_SEC):
    """
    人工確認關卡（**純流程**）：只印提示、讀一行輸入、回傳是否繼續。
      - 不接受 client 參數 → 等待期間不可能呼叫任何 API
      - 不持有 _CLIENT_LOCK / _SESSION_LOCK（本函式完全不涉及鎖）
      - 不建立 ReportSession、不更新任何正式狀態（_report / _auto / config 皆不觸碰）
    reader(timeout) -> str；可拋 TimeoutError / EOFError / KeyboardInterrupt。
    回傳 (proceed, reason)：只有**精確**輸入 CONFIRM_TOKEN 才 proceed=True。
    ⚠️ 不回顯使用者輸入內容（避免誤打的密碼等機密進入畫面或 log）。
    """
    log("")
    log("已完成唯讀 Preflight。")
    log("請人工比對 HMI 的 SOC、模式、排程開關與排程方向。")
    log("")
    log(f"  API rackSoc = {soc} %")
    log("  請人工確認 HMI 顯示是否一致。")
    log("  若不一致，請勿輸入 YES。")
    log(f"  排程配置方向（唯讀查得）：{plan_direction}")
    if plan_detail:
        log(f"    明細：{plan_detail}")
    if plan_direction != "charge":
        log("    ⚠️ 排程配置方向非明確 charge → 請人工於 HMI 確認本次排程確實為「充電」，"
            "否則請勿輸入 YES。")
    log("")
    log(f"確認本次為低功率短時間「充電」排程，且允許開始 3-B 驗證，請輸入：")
    log(f"{CONFIRM_TOKEN}")
    log(f"（必須精確輸入大寫 {CONFIRM_TOKEN}；{timeout} 秒未輸入、或輸入其他任何內容 → 取消。"
        f"取消不會建立報告、不會修改設備、不會刪除任何資料）")
    try:
        ans = reader(timeout)
    except TimeoutError:
        return False, "timeout"
    except EOFError:
        return False, "eof"
    except KeyboardInterrupt:
        return False, "interrupt"
    except Exception as e:                       # 讀取異常一律視為取消（fail-safe）
        return False, f"reader_error:{type(e).__name__}"
    if ans is None:
        return False, "timeout"
    if ans != CONFIRM_TOKEN:                     # 精確比對：yes / Yes / Y / 空白 / 其他 → 取消
        return False, "input_mismatch"           # 刻意不記錄輸入內容
    return True, "confirmed"


def recheck_after_confirm(before, after, *, auto_enabled, active_session,
                         battery_power_status):
    """
    YES 之後、正式進入等待觸發前的快速唯讀複查（**不再要求 YES**）。
    沿用同一套 preflight 守門，並列出人工確認期間變化的欄位。
    回傳 (ok, reason, changes)；ok=False → 安全退出、不建立 Session。
    """
    verdict, reason = preflight(after, auto_enabled=auto_enabled,
                                active_session=active_session,
                                battery_power_status=battery_power_status,
                                session_is_known=False)
    keys = ("pcs_control_mode_code", "pcs_schedule_enabled", "pcs_charging_flag",
            "pcs_discharging_flag", "pcs_fault_flag", "soc_percent")
    changes = []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = [f"{k}: {before.get(k)} → {after.get(k)}"
                   for k in keys if before.get(k) != after.get(k)]
    return verdict != "block", reason, changes


def evaluate_checks(obs, facts):
    """
    以觀測紀錄（obs）與資料夾事實（facts）產出 10 項主要 + 補充檢查（純函式）。
    obs  : dict —— 由 live 迴圈或 fixture 提供，欄位見 --selftest 的 fixture。
    facts: dict —— 由 inspect_session_folder() 提供（或 fixture）。
    回傳 [(編號, 標題, status, 說明), ...]；status ∈ PASS / FAIL / SKIP。
      編號 1~10 為主要項，"補" 為補充項。
    SKIP 專用於「本次驗證條件下外部本來就觀察不到」的項目（Not Observable），
    **不代表功能失敗**，避免使用者把時機造成的不可觀察誤讀成缺陷。
    """
    out = []

    def add(no, label, ok, detail="", skip=False, skip_reason=""):
        if skip:
            out.append((no, label, SKIP, skip_reason or detail))
        else:
            out.append((no, label, PASS if ok else FAIL, detail))

    idle_cycles = [c for c in obs["cycles"] if c["phase"] == "idle"]
    start_cycle = obs.get("start_cycle")
    after = [c for c in obs["cycles"] if c["phase"] in ("after_auto", "after_manual")]
    reenter = [c for c in obs["cycles"] if c["phase"] == "reenter"]

    # 1) idle 階段沒有建立 Session
    #    啟動時設備若已在 charging，就沒有 idle→charge 可觀察 → SKIP（Not Observable），非 FAIL。
    add(1, "idle 階段沒有建立 ReportSession",
        all(c["session_id"] is None and c["action"] != "started" for c in idle_cycles),
        f"idle 觀察 {len(idle_cycles)} 輪，"
        f"reason={sorted({c['reason'] for c in idle_cycles})}",
        skip=not idle_cycles,
        skip_reason="Not Observable：啟動時已處於 charging，未觀察到 idle→charge。")

    # 2) 真正出現 charging flag 後才建立
    add(2, "出現 charging flag 後才建立 Session",
        start_cycle is not None and start_cycle["chg"] is True
        and start_cycle["action"] == "started",
        f"觸發輪 chg={start_cycle['chg'] if start_cycle else None} "
        f"dis={start_cycle['dis'] if start_cycle else None} "
        f"action={start_cycle['action'] if start_cycle else None}")

    # 3) origin == scheduler（優先讀 events detail 的 origin=scheduler；舊版 log 退回事件型別推論）
    add(3, "建立來源 origin == scheduler",
        facts.get("origin") == "scheduler",
        f"events.csv 取得 origin={facts.get('origin')}"
        f"（判定來源：{facts.get('origin_source')}"
        f"{'＝detail 明確標註' if facts.get('origin_source') == 'detail' else ''}"
        f"{'＝舊版 log 由 auto_charge_start 推論' if facts.get('origin_source') == 'inferred' else ''}）")

    # 4) 僅建立一個新資料夾／一個 Session
    add(4, "僅建立 1 個新資料夾／1 個 Session",
        len(obs["new_folders"]) == 1 and len(obs["session_ids"]) == 1,
        f"新資料夾={obs['new_folders']}｜session_ids={sorted(obs['session_ids'])}")

    # 5) 連續自動刷新不建立第二份
    auto_after = [c for c in after if c["phase"] == "after_auto"]
    add(5, "連續自動刷新不建立第二份",
        bool(auto_after) and all(c["action"] == "skipped"
                                 and c["reason"] == "session_exists" for c in auto_after),
        f"自動刷新 {len(auto_after)} 輪，"
        f"reason={sorted({c['reason'] for c in auto_after})}")

    # 6) 手動刷新不建立第二份
    man_after = [c for c in after if c["phase"] == "after_manual"]
    add(6, "手動刷新不建立第二份",
        bool(man_after) and all(c["action"] == "skipped"
                                and c["reason"] == "session_exists" for c in man_after),
        f"手動刷新 {len(man_after)} 輪，"
        f"reason={sorted({c['reason'] for c in man_after})}")

    # 7) auto_charge_start 只寫一次
    add(7, "auto_charge_start 事件只寫入一次",
        facts.get("auto_charge_start_count") == 1,
        f"count={facts.get('auto_charge_start_count')}｜"
        f"time={facts.get('auto_charge_start_time')}")

    # 8) 背景取樣持續增加
    add(8, "報告背景取樣持續增加（samples.csv 有新增）",
        (facts.get("samples_after") or 0) > (facts.get("samples_at_start") or 0),
        f"samples.csv：建立時 {facts.get('samples_at_start')} 筆 → "
        f"結束時 {facts.get('samples_after')} 筆")

    # 9) 離開監看迴圈後不再自動刷新
    q = obs.get("quiet", {})
    add(9, "離開監看迴圈後不再執行 Dashboard 自動刷新",
        q.get("monitor_cycles_delta") == 0 and q.get("unexpected_threads") == [],
        f"靜默 {q.get('seconds')}s：監看輪數增加 {q.get('monitor_cycles_delta')}；"
        f"非預期執行緒={q.get('unexpected_threads')}；"
        f"（背景取樣執行緒屬報告模組、應持續：samples +{q.get('samples_delta')} 筆）")

    # 10) 再次進入監看只接續
    add(10, "再次進入監看只接續，不建立第二份",
        bool(reenter)
        and all(c["action"] == "skipped" and c["reason"] == "session_exists"
                for c in reenter)
        and len(obs["new_folders"]) == 1,
        f"再進入 {len(reenter)} 輪，session_id 全程={sorted(obs['session_ids'])}")

    # ---------------- 補充檢查 ----------------
    add("補", "Session ID 前後一致", len(obs["session_ids"]) == 1,
        f"{sorted(obs['session_ids'])}")
    add("補", "共用 client id 前後一致（未額外建立登入 client）",
        len(obs["client_ids"]) == 1, f"{sorted(obs['client_ids'])}")
    # auto state：只驗證「外部可觀察」的結果。START_PENDING 是 auto_schedule_check() 內部的
    # 瞬時狀態（鎖內設定、finally 立刻改為 RUNNING），黑箱觀察者在呼叫前後取樣本來就看不到，
    # 因此不列為 FAIL 條件；其實際保護作用由離線測試（情境 6c）負責驗證。
    states = obs["state_transitions"]
    idle_to_running = sum(1 for i in range(len(states) - 1)
                          if states[i] == "IDLE" and states[i + 1] == "RUNNING")
    add("補", "auto state 最終為 RUNNING，且只發生一次 IDLE→RUNNING（外部可觀察）",
        bool(states) and states[-1] == "RUNNING" and idle_to_running == 1,
        f"轉換={states}｜IDLE→RUNNING × {idle_to_running}")
    add("補", "auto state 經過 START_PENDING（函式內瞬時狀態）", False,
        skip=True,
        skip_reason="SKIP（Externally Not Observable）：START_PENDING 僅存在於 "
                    "auto_schedule_check() 呼叫期間，黑箱前後取樣無法觀察；"
                    "由離線測試情境 6c 驗證其阻擋作用。")
    add("補", "沒有出現第二次 START_PENDING", False,
        skip=True,
        skip_reason="SKIP（Externally Not Observable）：同上；"
                    "實際保護已由「只建立 1 份 Session／auto_charge_start ×1／"
                    "刷新不重建」等項目證明。")
    add("補", "events.csv 中 auto_charge_start 數量精確為 1",
        facts.get("auto_charge_start_count") == 1,
        f"count={facts.get('auto_charge_start_count')}")
    add("補", "charge_start／direction_change 符合既有邏輯"
             "（charge_start ≥ 1；排程自動建立不應有 direction_change）",
        (facts.get("charge_start_count") or 0) >= 1
        and (facts.get("direction_change_count") or 0) == 0,
        f"charge_start={facts.get('charge_start_count')}｜"
        f"direction_change={facts.get('direction_change_count')}")
    add("補", "報告資料夾必要檔案齊備",
        not facts.get("missing_files"),
        f"缺少={facts.get('missing_files')}｜實際={facts.get('present_files')}")
    add("補", "samples.csv 欄位與既有 schema 一致（未修改 Raw Data schema）",
        facts.get("samples_header_matches") is True,
        f"欄位數={facts.get('samples_header_len')}（預期 {facts.get('samples_header_expected')}）"
        f"｜差異={facts.get('samples_header_diff')}")
    add("補", "events.csv 欄位與既有 schema 一致",
        facts.get("events_header_matches") is True,
        f"欄位數={facts.get('events_header_len')}（預期 {facts.get('events_header_expected')}）")
    add("補", "alarms.csv 欄位與既有 schema 一致（若已產出）",
        facts.get("alarms_header_matches") in (True, None),
        f"matches={facts.get('alarms_header_matches')}")
    return out


def summarize(checks):
    """
    三態統計。回傳 (main, extra, ok)：
      main / extra 各為 {"pass": n, "fail": n, "skip": n, "total": n}
      ok = 沒有任何 FAIL（SKIP 不算失敗；Not Observable 不代表功能異常）
    """
    def _tally(rows):
        return {"pass": sum(1 for c in rows if c[2] == PASS),
                "fail": sum(1 for c in rows if c[2] == FAIL),
                "skip": sum(1 for c in rows if c[2] == SKIP),
                "total": len(rows)}

    main = _tally([c for c in checks if c[0] != "補"])
    extra = _tally([c for c in checks if c[0] == "補"])
    return main, extra, (main["fail"] == 0 and extra["fail"] == 0)


# ======================================================================
# 唯讀 I/O：檢查 session 資料夾（不修改、不刪除任何檔案）
# ======================================================================
def read_line_with_deadline(timeout):
    """
    **僅供本驗證腳本使用**的逾時輸入（不改、不呼叫正式 Menu 的 _prompt_with_timeout）。
    與 Menu 版本的差異：這裡是**硬性截止**——即使使用者已開始打字，逾時仍會取消，
    確保安全關卡不會永久卡住。
      - Windows：msvcrt 逐鍵讀取
      - POSIX  ：select 監看 stdin
      - 非互動 stdin（管線）：讀一行（視為使用者明確以管線給定答案）
    逾時 → 拋 TimeoutError；Ctrl+C → KeyboardInterrupt；Ctrl+D/EOF → EOFError。
    等待期間不呼叫任何 API、不持任何鎖。
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    deadline = time.monotonic() + timeout
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

    if msvcrt is not None:
        buf = ""
        while True:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print()
                    return buf
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch == "\x04":
                    raise EOFError
                if ch in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                if ch == "\b":
                    if buf:
                        buf = buf[:-1]
                        print("\b \b", end="", flush=True)
                    continue
                buf += ch
                print(ch, end="", flush=True)
            if time.monotonic() >= deadline:      # 硬性截止（即使已輸入部分字元）
                print()
                raise TimeoutError
            time.sleep(0.05)

    import select
    remain = deadline - time.monotonic()
    rlist, _, _ = select.select([sys.stdin], [], [], max(0.0, remain))
    if not rlist:
        raise TimeoutError
    line = sys.stdin.readline()
    if line == "":
        raise EOFError
    return line.rstrip("\r\n")


def _read_csv_rows(path):
    if not os.path.exists(path):
        return None, None
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        rdr = csv.reader(f)
        try:
            header = next(rdr)
        except StopIteration:
            return [], []
        return header, [r for r in rdr if any(x.strip() for x in r)]


def inspect_session_folder(folder, CFG, samples_at_start=None):
    """唯讀檢查 session 資料夾，回傳 facts dict（不寫入、不刪除任何檔案）。"""
    f = {"folder": folder, "samples_at_start": samples_at_start}
    need = [CFG.FILE_SESSION_STATE, CFG.FILE_SAMPLES, CFG.FILE_EVENTS]
    optional = [CFG.FILE_SUMMARY, CFG.FILE_STATISTICS, CFG.FILE_ALARMS,
                CFG.FILE_XLSX, CFG.FILE_CELL_SNAPSHOTS]
    present = [n for n in need + optional
               if os.path.exists(os.path.join(folder, n))]
    f["present_files"] = present
    f["missing_files"] = [n for n in need if n not in present]

    s_head, s_rows = _read_csv_rows(os.path.join(folder, CFG.FILE_SAMPLES))
    f["samples_after"] = len(s_rows) if s_rows is not None else None
    f["samples_header_len"] = len(s_head) if s_head else None
    f["samples_header_expected"] = len(CFG.SAMPLE_FIELDS)
    f["samples_header_matches"] = (s_head == CFG.SAMPLE_FIELDS) if s_head else False
    f["samples_header_diff"] = ([] if s_head == CFG.SAMPLE_FIELDS else
                                {"多": [c for c in (s_head or []) if c not in CFG.SAMPLE_FIELDS],
                                 "缺": [c for c in CFG.SAMPLE_FIELDS if c not in (s_head or [])]})

    e_head, e_rows = _read_csv_rows(os.path.join(folder, CFG.FILE_EVENTS))
    f["events_after"] = len(e_rows) if e_rows is not None else None
    f["events_header_len"] = len(e_head) if e_head else None
    f["events_header_expected"] = len(CFG.EVENT_FIELDS)
    f["events_header_matches"] = (e_head == CFG.EVENT_FIELDS) if e_head else False

    a_head, _a_rows = _read_csv_rows(os.path.join(folder, CFG.FILE_ALARMS))
    f["alarms_header_matches"] = (None if a_head is None else a_head == CFG.ALARM_FIELDS)

    # 事件統計（依 EVENT_FIELDS 位置解析）
    # origin 判定（兩段式，向下相容）：
    #   ① 明確標註：detail 內含 "origin=scheduler"（正式程式已寫入）→ 直接採用，來源 "detail"
    #   ② 舊版 log（detail 未含 origin）→ 以事件型別推論：auto_charge_start /
    #      auto_discharge_start **只由 auto_schedule_check() 發出**，其存在即代表排程自動建立，
    #      來源 "inferred"。兩者都視為 origin=scheduler。
    counts, first_time = {}, {}
    origin = origin_source = None
    if e_head and e_rows:
        i_type = e_head.index("event_type") if "event_type" in e_head else 2
        i_ts = e_head.index("timestamp") if "timestamp" in e_head else 0
        i_detail = e_head.index("detail") if "detail" in e_head else 4
        for row in e_rows:
            if len(row) <= i_type:
                continue
            et = row[i_type]
            counts[et] = counts.get(et, 0) + 1
            first_time.setdefault(et, row[i_ts] if len(row) > i_ts else None)
            if et in ("auto_charge_start", "auto_discharge_start"):
                detail = row[i_detail] if len(row) > i_detail else ""
                m = re.search(r"origin=([A-Za-z_]+)", detail)
                if m:                                    # ① 明確標註優先
                    origin, origin_source = m.group(1), "detail"
                elif origin is None:                     # ② 舊版退回推論
                    origin, origin_source = "scheduler", "inferred"
    f["event_counts"] = counts
    f["auto_charge_start_count"] = counts.get("auto_charge_start", 0)
    f["auto_charge_start_time"] = first_time.get("auto_charge_start")
    f["charge_start_count"] = counts.get("charge_start", 0)
    f["direction_change_count"] = counts.get("direction_change", 0)
    f["origin"] = origin
    f["origin_source"] = origin_source                   # detail（明確） / inferred（舊版推論）
    return f


def _list_session_folders(root):
    if not os.path.isdir(root):
        return set()
    return {n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))}


def _samples_count(folder, CFG):
    _h, rows = _read_csv_rows(os.path.join(folder, CFG.FILE_SAMPLES))
    return len(rows) if rows is not None else 0


# ======================================================================
# 實機驗證主流程（走正式路徑；不 monkeypatch）
# ======================================================================
def run_live(wait_min, log):
    import device_control_menu as M
    import report_monitor as RM    # noqa: E402  Phase 4.2：Monitor Core 已抽離
    import charge_discharge_report_config as CFG

    started_at = datetime.now()
    obs = {"cycles": [], "session_ids": set(), "client_ids": set(),
           "state_transitions": [], "new_folders": [], "start_cycle": None,
           "quiet": {}}
    monitor_cycles = {"n": 0}
    root = os.path.join(OUTPUT_DIR, CFG.OUTPUT_SUBDIR)
    folders_before = _list_session_folders(root)

    def note_state():
        st = RM._auto["state"]
        if not obs["state_transitions"] or obs["state_transitions"][-1] != st:
            obs["state_transitions"].append(st)

    def cycle(phase, client, source):
        """一輪監看：完全走正式路徑（read_all → auto_schedule_check）。"""
        monitor_cycles["n"] += 1
        r = RM._read_device_state(client)
        note_state()
        action, reason = RM.auto_schedule_check(r, client=client, source=source)
        note_state()
        sess = RM._report["session"]
        sid = sess.session_id if sess is not None else None
        if sid:
            obs["session_ids"].add(sid)
        if RM._report["client"] is not None:
            obs["client_ids"].add(id(RM._report["client"]))
        # 觸發輪單獨標為 "trigger"：第 1 項只檢查真正的 idle 輪，第 2 項只檢查觸發輪
        phase = "trigger" if action == "started" else phase
        rec = {"phase": phase, "t": datetime.now().strftime("%H:%M:%S"),
               "src": source, "action": action, "reason": reason,
               "session_id": sid, "state": RM._auto["state"],
               "mode": (r or {}).get("pcs_control_mode_code"),
               "sched": (r or {}).get("pcs_schedule_enabled"),
               "chg": (r or {}).get("pcs_charging_flag"),
               "dis": (r or {}).get("pcs_discharging_flag"),
               "fault": (r or {}).get("pcs_fault_flag"),
               "dir": RM._actual_direction(r)[0] if r else None,
               "soc": (r or {}).get("soc_percent"),
               "power": (r or {}).get("actual_active_power_kw"),
               "held": round(RM._auto["held_sec"], 1)}
        obs["cycles"].append(rec)
        log(f"  [{rec['t']}] {phase:<12} src={source:<6} mode={rec['mode']} "
            f"sched={rec['sched']} chg={rec['chg']} dis={rec['dis']} "
            f"fault={rec['fault']} dir={rec['dir']} held={rec['held']}s "
            f"state={rec['state']} session={sid or 'None'}")
        log(f"               → action={action} reason={reason}"
            f"｜SOC {rec['soc']}% P {rec['power']}kW")
        return rec, r

    # ---------------- 啟動前安全檢查 ----------------
    log.section("Phase 2 實機驗證 B —— 真實排程觸發（黑箱、唯讀觀察、不送任何控制）")
    log(f"開始時間：{started_at:%Y-%m-%d %H:%M:%S}")
    log(f"AUTO_SCHEDULE_REPORT_ENABLED = {CFG.AUTO_SCHEDULE_REPORT_ENABLED}")
    log(f"AUTO_MONITOR_DEBUG           = {CFG.AUTO_MONITOR_DEBUG}")
    log(f"AUTO_REFRESH_SEC             = {RM.AUTO_REFRESH_SEC}s"
        f"（本腳本沿用正式節奏，不另訂）")
    log(f"最長等待充電開始             = {wait_min} 分鐘")
    log(f"報告輸出根目錄               = {root}")
    log(f"目前既有 session 資料夾數     = {len(folders_before)}"
        f"（最新：{sorted(folders_before)[-1] if folders_before else '無'}）")

    if not CFG.AUTO_SCHEDULE_REPORT_ENABLED:
        log("")
        log(f"[安全退出] {BLOCK_REASONS['auto_report_disabled']}")
        log("           本腳本**不會**自行修改此開關。請確認現場已完成智慧模式／排程設定，")
        log("           再由使用者手動改為 True 後重新執行。")
        return False, obs, None

    client, _p = RM._monitor_client(force=True)
    if client is None:
        log(f"\n[安全退出] {BLOCK_REASONS['login_failed']}")
        return False, obs, None
    obs["client_ids"].add(id(client))
    log(f"共用 client id                = {id(client)}（整個測試只登入一次）")

    r0 = RM._read_device_state(client)
    batt = "未知"
    if r0 is not None:
        batt = r0.get("battery_power_status", "未知")
    active = RM._report["session"]
    plan_dir, plan_detail = schedule_plan_direction(
        client, RM._SCHED_TPL_LIST, RM._SCHED_ITEM_LIST)     # 唯讀 GET，不修改任何排程

    verdict, reason = preflight(
        r0, auto_enabled=CFG.AUTO_SCHEDULE_REPORT_ENABLED, active_session=active,
        battery_power_status=batt, session_is_known=False)

    log("")
    log("啟動前狀態（唯讀 Preflight）：")
    for k, v in (("AUTO_SCHEDULE_REPORT_ENABLED", CFG.AUTO_SCHEDULE_REPORT_ENABLED),
                 ("pcs_control_mode_code", (r0 or {}).get("pcs_control_mode_code")),
                 ("pcs_schedule_enabled", (r0 or {}).get("pcs_schedule_enabled")),
                 ("pcs_charging_flag", (r0 or {}).get("pcs_charging_flag")),
                 ("pcs_discharging_flag", (r0 or {}).get("pcs_discharging_flag")),
                 ("pcs_fault_flag", (r0 or {}).get("pcs_fault_flag")),
                 ("actual_direction", RM._actual_direction(r0)[0] if r0 else None),
                 ("rackSoc (%)", (r0 or {}).get("soc_percent")),
                 ("電池上下電狀態", batt),
                 ("active Session", active.session_id if active is not None else "None"),
                 ("最新 output 資料夾", sorted(folders_before)[-1] if folders_before else "無"),
                 ("共用 client id", id(client)),
                 ("排程配置方向", f"{plan_dir}（{plan_detail}）"),
                 ("preflight verdict", f"{verdict} / {reason}")):
        log(f"  {k:<28}= {v}")

    # ---- 自動守門：block → 直接安全退出（不顯示 YES 提示、不進入等待觸發）----
    if verdict == "block":
        log("")
        log(f"[安全退出] {BLOCK_REASONS.get(reason, reason)}")
        if reason == "unknown_session":
            log("           腳本不會刪除或重設任何既有 Session/資料；"
                "請先以 Menu 19 停止並產生報告後重試。")
        return False, obs, None
    pv, _ = plan_direction_verdict(plan_dir)         # reason 已由下方訊息明確表達，不另取用
    if pv == "block":
        log("")
        log("[安全退出] 排程配置方向明確為 discharge —— "
            f"{BLOCK_REASONS['discharge_only']}")
        log(f"           排程明細：{plan_detail}")
        return False, obs, None

    # ---- 人工確認關卡（等待期間不呼叫 API、不持任何鎖、不建立 Session）----
    proceed, creason = confirm_gate(log, read_line_with_deadline,
                                    (r0 or {}).get("soc_percent"), plan_dir, plan_detail)
    if not proceed:
        log("")
        log(CANCEL_MSG)
        log(f"（取消原因：{creason}）")
        return False, obs, None

    # ---- YES 之後的快速唯讀複查（不再要求 YES）----
    log("")
    log("[複查] 人工確認期間狀態是否改變（唯讀）…")
    r1 = RM._read_device_state(client)
    batt1 = r1.get("battery_power_status", "未知") if isinstance(r1, dict) else "未知"
    ok2, reason2, changes = recheck_after_confirm(
        r0, r1, auto_enabled=CFG.AUTO_SCHEDULE_REPORT_ENABLED,
        active_session=RM._report["session"], battery_power_status=batt1)
    log(f"  期間變化：{changes if changes else '無'}")
    if not ok2:
        log("")
        log(f"[安全退出] 人工確認期間狀態改變 → {BLOCK_REASONS.get(reason2, reason2)}")
        log("           未建立任何 Session、未送任何控制命令。")
        return False, obs, None
    log(f"  複查結果：通過（{reason2}）→ 進入等待觸發")
    r0 = r1

    # ---------------- 階段 1：唯讀等待 → idle 觀察 ----------------
    log.section("階段 1：唯讀等待排程觸發（idle 期間不得建立報告）")
    if verdict == "wait":
        log(f"  目前：{WAIT_REASONS.get(reason, reason)}")
    deadline = time.monotonic() + wait_min * 60
    start_rec = None
    while True:
        rec, r = cycle("idle", client, "auto")
        if rec["dis"] is True:
            log("")
            log(f"[停止驗證] {BLOCK_REASONS['discharge_only']}")
            if RM._report["session"] is not None:
                log(f"           ⚠️ 正式程式已建立 Session："
                    f"{RM._report['session'].session_id}")
                log("           腳本不會刪除任何資料，請由現場人員以 Menu 19 處理。")
            return False, obs, None
        if rec["fault"] is True:
            log(f"\n[停止驗證] {BLOCK_REASONS['pcs_fault']}")
            return False, obs, None
        if rec["action"] == "started":
            start_rec = rec
            break
        if time.monotonic() >= deadline:
            log("")
            log(f"[逾時結束] 等待 {wait_min} 分鐘仍未偵測到實際充電 → 未建立任何報告。")
            log("           請確認現場排程是否已啟動，再重新執行。")
            return False, obs, None
        time.sleep(RM.AUTO_REFRESH_SEC)

    # ---------------- 階段 2：建立確認 ----------------
    obs["start_cycle"] = start_rec
    folders_after = _list_session_folders(root)
    obs["new_folders"] = sorted(folders_after - folders_before)
    sess = RM._report["session"]
    folder = sess.folder if sess is not None else None
    log.section("階段 2：已偵測到實際充電並建立 Session")
    log(f"  Session ID      ：{sess.session_id if sess else None}")
    log(f"  資料夾完整路徑  ：{folder}")
    log(f"  新增資料夾      ：{obs['new_folders']}")
    samples_at_start = _samples_count(folder, CFG) if folder else 0
    events_at_start = len((_read_csv_rows(os.path.join(folder, CFG.FILE_EVENTS))[1] or [])
                          ) if folder else 0
    log(f"  建立時 samples.csv 筆數：{samples_at_start}")
    log(f"  建立時 events.csv  筆數：{events_at_start}")

    # ---------------- 階段 3：唯一性（自動 + 手動刷新）----------------
    log.section("階段 3：連續刷新（驗證不建立第二份）")
    for _i in range(AUTO_CYCLES_AFTER_START):
        time.sleep(RM.AUTO_REFRESH_SEC)
        cycle("after_auto", client, "auto")
    for _i in range(MANUAL_CYCLES_AFTER_START):
        cycle("after_manual", client, "manual")       # 等同使用者按 r

    # ---------------- 階段 4：離開監看迴圈（靜默觀察）----------------
    log.section(f"階段 4：離開監看迴圈，靜默觀察 {LEAVE_LOOP_QUIET_SEC}s")
    log("  （模擬使用者離開 Dashboard：腳本不再呼叫任何監看流程）")
    n_before = monitor_cycles["n"]
    s_before = _samples_count(folder, CFG) if folder else 0
    expected = {"MainThread"}
    time.sleep(LEAVE_LOOP_QUIET_SEC)
    alive = [t.name for t in threading.enumerate() if t.name not in expected]
    # 報告背景取樣執行緒屬報告模組（應持續運作）；監看層不得有任何執行緒
    bg = RM._report["thread"]
    unexpected = [n for n in alive if bg is None or n != bg.name]
    s_after = _samples_count(folder, CFG) if folder else 0
    obs["quiet"] = {"seconds": LEAVE_LOOP_QUIET_SEC,
                    "monitor_cycles_delta": monitor_cycles["n"] - n_before,
                    "unexpected_threads": unexpected,
                    "samples_delta": s_after - s_before,
                    "threads_alive": alive,
                    "bg_thread": bg.name if bg is not None else None}
    log(f"  監看輪數增加        ：{obs['quiet']['monitor_cycles_delta']}（應為 0）")
    log(f"  存活執行緒（非主）  ：{alive}")
    log(f"  報告背景取樣執行緒  ：{obs['quiet']['bg_thread']}（應持續運作）")
    log(f"  非預期執行緒        ：{unexpected}（應為空）")
    log(f"  期間 samples.csv 增加：{obs['quiet']['samples_delta']} 筆（背景取樣應持續）")

    # ---------------- 階段 5：再次進入監看 ----------------
    log.section("階段 5：再次進入監看（既有 Session 只接續、不建立第二份）")
    for _i in range(REENTER_CYCLES):
        cycle("reenter", client, "manual")
        time.sleep(RM.AUTO_REFRESH_SEC)

    # ---------------- 事實蒐集 ----------------
    folders_final = _list_session_folders(root)
    obs["new_folders"] = sorted(folders_final - folders_before)
    facts = inspect_session_folder(folder, CFG, samples_at_start=samples_at_start) \
        if folder else {}
    facts["events_at_start"] = events_at_start

    log.section("報告狀態（本腳本不送任何停止命令）")
    cur = RM._report["session"]
    last = obs["cycles"][-1]
    if cur is not None and last["chg"] is not True:
        log("  充電已停止，但 ReportSession 仍存在，等待 Phase 3 Stop Debounce")
    elif cur is not None:
        log(f"  充電仍在進行，ReportSession 持續記錄中：{cur.session_id}")
    else:
        log("  ReportSession 已由正式邏輯結束（control_stop / fault_stop / "
            "communication_error）")
    log("  ⚠️ 如需結束報告，請由使用者以既有 Menu 19（停止並產生報告）操作；"
        "本腳本不會呼叫任何控制或停止 API。")
    return True, obs, facts


def report(log, obs, facts, started_at):
    """輸出每輪摘要、10 項逐項結果與總結。"""
    log.section("每輪 reading 摘要與判斷")
    log(f"{'phase':<12} {'time':<9} {'src':<7} {'dir':<10} {'chg':<6} {'action':<8} reason")
    for c in obs["cycles"]:
        log(f"{c['phase']:<12} {c['t']:<9} {c['src']:<7} {str(c['dir']):<10} "
            f"{str(c['chg']):<6} {c['action']:<8} {c['reason']}")

    log.section("證據彙整")
    log(f"  Session ID        ：{sorted(obs['session_ids'])}")
    log(f"  新建資料夾        ：{obs['new_folders']}")
    log(f"  資料夾完整路徑    ：{facts.get('folder')}")
    log(f"  共用 client id    ：{sorted(obs['client_ids'])}")
    log(f"  auto state 轉換   ：{' → '.join(obs['state_transitions'])}")
    log(f"  auto_charge_start ：{facts.get('auto_charge_start_count')} 次"
        f"（時間 {facts.get('auto_charge_start_time')}）")
    log(f"  samples.csv       ：建立時 {facts.get('samples_at_start')} 筆 → "
        f"結束時 {facts.get('samples_after')} 筆")
    log(f"  events.csv        ：建立時 {facts.get('events_at_start')} 筆 → "
        f"結束時 {facts.get('events_after')} 筆")
    log(f"  事件統計          ：{facts.get('event_counts')}")
    log(f"  資料夾檔案        ：{facts.get('present_files')}")

    checks = evaluate_checks(obs, facts)
    log.section("驗證結果（主要 10 項 + 補充檢查；SKIP=外部不可觀察，非失敗）")
    for no, label, status, detail in checks:
        log(f"  [{status}] {no:>2}. {label}")
        if detail:
            log(f"          {detail}")
    main, extra, all_ok = summarize(checks)
    log.section("總結")
    log(f"  主要 {main['total']} 項")
    log(f"    PASS：{main['pass']}")
    log(f"    FAIL：{main['fail']}")
    log(f"    SKIP：{main['skip']}（Not Observable）")
    log(f"  補充檢查 {extra['total']} 項")
    log(f"    PASS：{extra['pass']}")
    log(f"    FAIL：{extra['fail']}")
    log(f"    SKIP：{extra['skip']}（Not Observable）")
    log(f"  結束時間  ：{datetime.now():%Y-%m-%d %H:%M:%S}"
        f"（歷時 {(datetime.now() - started_at).total_seconds() / 60:.1f} 分）")
    log(f"  == Phase 2 實機觸發驗證 {'PASS' if all_ok else 'FAIL'} ==")
    if main["skip"] or extra["skip"]:
        log("  （SKIP 項目為本次驗證條件下外部無法觀察，不代表功能異常；原因見上方各項說明）")
    return all_ok


# ======================================================================
# 離線自我測試：以 fixture 驗證純函式核心與輸出格式（不連設備、不 monkeypatch）
# ======================================================================
def _fx_cycle(phase, action="skipped", reason="direction=idle", chg=False, dis=False,
              session_id=None, state="IDLE", src="auto"):
    return {"phase": phase, "t": "12:00:00", "src": src, "action": action,
            "reason": reason, "session_id": session_id, "state": state,
            "mode": "smart", "sched": True, "chg": chg, "dis": dis, "fault": False,
            "dir": "charge" if chg else ("discharge" if dis else "idle"),
            "soc": 12.0, "power": 20.0 if chg else 0.0, "held": 30.0}


def _fx_obs(**over):
    sid = "20260731_101500_auto"
    start = _fx_cycle("trigger", action="started", reason="charge", chg=True,
                      session_id=sid, state="RUNNING")
    obs = {
        "cycles": ([_fx_cycle("idle") for _ in range(3)] + [start]
                   + [_fx_cycle("after_auto", reason="session_exists", chg=True,
                                session_id=sid, state="RUNNING") for _ in range(4)]
                   + [_fx_cycle("after_manual", reason="session_exists", chg=True,
                                session_id=sid, state="RUNNING", src="manual")]
                   + [_fx_cycle("reenter", reason="session_exists", chg=True,
                                session_id=sid, state="RUNNING", src="manual")
                      for _ in range(2)]),
        "session_ids": {sid}, "client_ids": {140000}, "start_cycle": start,
        # 真實觀測到的轉換：START_PENDING 是 auto_schedule_check() 內部瞬時狀態，
        # 黑箱在呼叫前後取樣只會看到 IDLE → RUNNING（故該項改為 SKIP）
        "state_transitions": ["IDLE", "RUNNING"],
        "new_folders": [sid],
        "quiet": {"seconds": 20, "monitor_cycles_delta": 0, "unexpected_threads": [],
                  "samples_delta": 4, "threads_alive": ["Thread-1"], "bg_thread": "Thread-1"},
    }
    obs.update(over)
    return obs


def _fx_facts(CFG, **over):
    f = {"folder": r"D:\out\20260731_101500_auto", "origin": "scheduler",
         "auto_charge_start_count": 1, "auto_charge_start_time": "2026-07-31 10:15:03",
         "charge_start_count": 1, "direction_change_count": 0,
         "samples_at_start": 1, "samples_after": 20, "events_at_start": 2,
         "events_after": 5, "event_counts": {"session_start": 1, "auto_charge_start": 1,
                                             "charge_start": 1},
         "present_files": [CFG.FILE_SESSION_STATE, CFG.FILE_SAMPLES, CFG.FILE_EVENTS],
         "missing_files": [],
         "samples_header_matches": True, "samples_header_len": len(CFG.SAMPLE_FIELDS),
         "samples_header_expected": len(CFG.SAMPLE_FIELDS), "samples_header_diff": [],
         "events_header_matches": True, "events_header_len": len(CFG.EVENT_FIELDS),
         "events_header_expected": len(CFG.EVENT_FIELDS),
         "alarms_header_matches": True}
    f.update(over)
    return f


def selftest():
    """離線自我測試：驗證安全守門、人工確認關卡、10 項判斷、輸出格式、「不含控制 API」。"""
    import inspect
    import charge_discharge_report_config as CFG
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()

    def _src_of(fn):
        return inspect.getsource(fn)

    res = []

    def check(label, ok):
        res.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    print("== phase2_real_trigger_validation 離線自我測試（不連設備）==")

    # ---------- 1) 安全守門 ----------
    print("\n[守門] preflight 各種情境")
    base = {"pcs_control_mode_code": "smart", "pcs_schedule_enabled": True,
            "pcs_charging_flag": True, "pcs_discharging_flag": False,
            "pcs_fault_flag": False}
    ok_kw = dict(auto_enabled=True, active_session=None,
                 battery_power_status="已上電", session_is_known=False)
    check("開關 False → block/auto_report_disabled",
          preflight(base, **dict(ok_kw, auto_enabled=False))
          == ("block", "auto_report_disabled"))
    check("reading=None → block/no_reading",
          preflight(None, **ok_kw) == ("block", "no_reading"))
    check("PCS 故障 → block/pcs_fault",
          preflight(dict(base, pcs_fault_flag=True), **ok_kw) == ("block", "pcs_fault"))
    check("chg 與 dis 同時 True → block/flag_conflict",
          preflight(dict(base, pcs_discharging_flag=True), **ok_kw)
          == ("block", "flag_conflict"))
    check("已有來源不明 Session → block/unknown_session",
          preflight(base, **dict(ok_kw, active_session=object()))
          == ("block", "unknown_session"))
    check("電池已下電 → block/battery_off",
          preflight(base, **dict(ok_kw, battery_power_status="已下電"))
          == ("block", "battery_off"))
    check("偵測到 discharge → block/discharge_only",
          preflight(dict(base, pcs_charging_flag=False, pcs_discharging_flag=True), **ok_kw)
          == ("block", "discharge_only"))
    check("非智慧模式 → wait/mode_not_smart（唯讀等待，不修改設備）",
          preflight(dict(base, pcs_control_mode_code="manual"), **ok_kw)
          == ("wait", "mode_not_smart"))
    check("排程未開 → wait/schedule_off",
          preflight(dict(base, pcs_schedule_enabled=False), **ok_kw)
          == ("wait", "schedule_off"))
    check("尚未充電 → wait/idle",
          preflight(dict(base, pcs_charging_flag=False), **ok_kw) == ("wait", "idle"))
    check("條件就緒 → ready/charging", preflight(base, **ok_kw) == ("ready", "charging"))
    check("discharge 判定優先於 wait 條件（放電一律不測）",
          preflight({"pcs_control_mode_code": "manual", "pcs_schedule_enabled": False,
                     "pcs_charging_flag": False, "pcs_discharging_flag": True,
                     "pcs_fault_flag": False}, **ok_kw) == ("block", "discharge_only"))

    # ---------- 2) 10 項判斷：全部正常 ----------
    print("\n[判斷] 理想情境 → 無任何 FAIL；START_PENDING 兩項為 SKIP（外部不可觀察）")
    checks = evaluate_checks(_fx_obs(), _fx_facts(CFG))
    main, extra, all_ok = summarize(checks)
    check(f"無任何 FAIL（主要 {main['pass']}P/{main['fail']}F/{main['skip']}S、"
          f"補充 {extra['pass']}P/{extra['fail']}F/{extra['skip']}S）",
          all_ok and main["total"] == 10 and main["fail"] == 0 and extra["fail"] == 0)
    check("編號 1~10 齊全且不重複",
          sorted(c[0] for c in checks if c[0] != "補") == list(range(1, 11)))
    check(f"idle 有觀察到時第 1 項為 PASS（非 SKIP）",
          next(c[2] for c in checks if c[0] == 1) == PASS)
    check(f"補充項固定 2 項 SKIP（START_PENDING 相關，外部不可觀察）",
          extra["skip"] == 2)
    check("SKIP 項目都附有 Not Observable 原因",
          all("Not Observable" in c[3] for c in checks if c[2] == SKIP))

    # ---------- 3) 10 項判斷：每種失敗都要被抓到 ----------
    print("\n[判斷] 各種異常情境 → 對應項目必須 FAIL")

    def fails(obs, facts):
        return {c[0] for c in evaluate_checks(obs, facts) if c[2] == FAIL}

    def skips(obs, facts):
        return {c[0] for c in evaluate_checks(obs, facts) if c[2] == SKIP}

    o = _fx_obs()
    o["cycles"] = [_fx_cycle("idle", action="started", session_id="X")] + o["cycles"][1:]
    check("idle 階段就建立 → 第 1 項 FAIL", 1 in fails(o, _fx_facts(CFG)))
    check("origin 非 scheduler → 第 3 項 FAIL",
          3 in fails(_fx_obs(), _fx_facts(CFG, origin="manual")))
    o = _fx_obs(new_folders=["a", "b"], session_ids={"a", "b"})
    check("建立 2 個資料夾 → 第 4 項 FAIL", 4 in fails(o, _fx_facts(CFG)))
    o = _fx_obs()
    o["cycles"] = [c if c["phase"] != "after_auto" else dict(c, action="started")
                   for c in o["cycles"]]
    check("自動刷新又建立 → 第 5 項 FAIL", 5 in fails(o, _fx_facts(CFG)))
    o = _fx_obs()
    o["cycles"] = [c if c["phase"] != "after_manual" else dict(c, action="started")
                   for c in o["cycles"]]
    check("手動刷新又建立 → 第 6 項 FAIL", 6 in fails(o, _fx_facts(CFG)))
    check("auto_charge_start 寫 2 次 → 第 7 項 FAIL",
          7 in fails(_fx_obs(), _fx_facts(CFG, auto_charge_start_count=2)))
    check("samples 未增加 → 第 8 項 FAIL",
          8 in fails(_fx_obs(), _fx_facts(CFG, samples_after=1, samples_at_start=1)))
    o = _fx_obs()
    o["quiet"] = dict(o["quiet"], monitor_cycles_delta=2)
    check("離開後仍在刷新 → 第 9 項 FAIL", 9 in fails(o, _fx_facts(CFG)))
    o = _fx_obs()
    o["quiet"] = dict(o["quiet"], unexpected_threads=["MonitorThread"])
    check("出現非預期執行緒 → 第 9 項 FAIL", 9 in fails(o, _fx_facts(CFG)))
    o = _fx_obs()
    o["cycles"] = [c if c["phase"] != "reenter" else dict(c, action="started")
                   for c in o["cycles"]]
    check("再進入時又建立 → 第 10 項 FAIL", 10 in fails(o, _fx_facts(CFG)))
    # auto state：改為驗證外部可觀察的結果 —— 出現第二次 IDLE→RUNNING 代表建立了第二份
    o = _fx_obs(state_transitions=["IDLE", "RUNNING", "IDLE", "RUNNING"])
    check("出現第二次 IDLE→RUNNING（等同重建）→ 補充項 FAIL",
          "補" in fails(o, _fx_facts(CFG)))
    o = _fx_obs(state_transitions=["IDLE", "RUNNING", "IDLE"])
    check("最終狀態非 RUNNING → 補充項 FAIL", "補" in fails(o, _fx_facts(CFG)))
    check("samples.csv 欄位不符 → 補充項 FAIL",
          "補" in fails(_fx_obs(), _fx_facts(CFG, samples_header_matches=False)))
    check("缺少必要檔案 → 補充項 FAIL",
          "補" in fails(_fx_obs(), _fx_facts(CFG, missing_files=["events.csv"])))
    check("direction_change 出現在排程自動建立 → 補充項 FAIL",
          "補" in fails(_fx_obs(), _fx_facts(CFG, direction_change_count=1)))

    # ---------- 3.2) SKIP（Not Observable）判定 ----------
    print("\n[判斷] 啟動時已在 charging → 第 1 項應為 SKIP 而非 FAIL")
    o = _fx_obs()
    o["cycles"] = [c for c in o["cycles"] if c["phase"] != "idle"]   # 沒有任何 idle 輪
    ch_noidle = evaluate_checks(o, _fx_facts(CFG))
    st1 = next(c[2] for c in ch_noidle if c[0] == 1)
    check(f"第 1 項 = SKIP（實際 {st1}）", st1 == SKIP)
    check("第 1 項不列為 FAIL", 1 not in fails(o, _fx_facts(CFG)))
    check("第 1 項 SKIP 說明含「啟動時已處於 charging」",
          "啟動時已處於 charging" in next(c[3] for c in ch_noidle if c[0] == 1))
    m_ni, e_ni, ok_ni = summarize(ch_noidle)
    check(f"整體仍判 PASS（無 FAIL）：主要 {m_ni['pass']}P/{m_ni['fail']}F/{m_ni['skip']}S",
          ok_ni is True and m_ni["fail"] == 0 and m_ni["skip"] == 1)
    check("三態統計加總正確",
          m_ni["pass"] + m_ni["fail"] + m_ni["skip"] == m_ni["total"]
          and e_ni["pass"] + e_ni["fail"] + e_ni["skip"] == e_ni["total"])
    # 有 FAIL 時整體必須判 FAIL（SKIP 不可掩蓋真實失敗）
    _m, _e, ok_bad = summarize(evaluate_checks(o, _fx_facts(CFG, origin="manual")))
    check("SKIP 不會掩蓋真實 FAIL（origin 錯 → 整體 FAIL）", ok_bad is False)

    print("\n[判斷] origin 兩段式判定（新版 detail 明確標註／舊版推論，向下相容）")
    import tempfile

    def _mk_events(detail):
        """建立臨時 session 資料夾（僅供解析測試；寫在系統暫存區，不碰 output/）。"""
        d = tempfile.mkdtemp(prefix="p2_origin_")
        with io.open(os.path.join(d, CFG.FILE_EVENTS), "w", encoding="utf-8", newline="") as fh:
            fh.write(",".join(CFG.EVENT_FIELDS) + "\n")
            fh.write(f"2026-08-04 15:01:07,0.0,session_start,info,action=auto\n")
            fh.write(f"2026-08-04 15:01:08,1.0,auto_charge_start,info,{detail}\n")
            fh.write(f"2026-08-04 15:01:09,2.4,charge_start,info,P=2.5kW\n")
        with io.open(os.path.join(d, CFG.FILE_SAMPLES), "w", encoding="utf-8", newline="") as fh:
            fh.write(",".join(CFG.SAMPLE_FIELDS) + "\n")
        with io.open(os.path.join(d, CFG.FILE_SESSION_STATE), "w", encoding="utf-8") as fh:
            fh.write("{}")
        return d

    _d_new = _mk_events("origin=scheduler｜智慧排程自動開始充電｜方向持續 0s｜"
                        "排程配置 enabled｜刷新來源 auto")
    fa_new = inspect_session_folder(_d_new, CFG)
    check(f"新版 detail 含 origin=scheduler → origin={fa_new['origin']}"
          f" source={fa_new['origin_source']}",
          fa_new["origin"] == "scheduler" and fa_new["origin_source"] == "detail")
    _d_old = _mk_events("智慧排程自動開始充電｜方向持續 0s｜排程配置 enabled｜刷新來源 auto")
    fa_old = inspect_session_folder(_d_old, CFG)
    check(f"舊版 detail 無 origin → 由 auto_charge_start 推論 origin={fa_old['origin']}"
          f" source={fa_old['origin_source']}",
          fa_old["origin"] == "scheduler" and fa_old["origin_source"] == "inferred")
    check("新版與舊版第 3 項皆 PASS（向下相容）",
          next(c[2] for c in evaluate_checks(_fx_obs(), fa_new) if c[0] == 3) == PASS
          and next(c[2] for c in evaluate_checks(_fx_obs(), fa_old) if c[0] == 3) == PASS)
    _d_manual = _mk_events("origin=manual｜人工建立")
    fa_man = inspect_session_folder(_d_manual, CFG)
    check(f"detail 明確標註 origin=manual → 第 3 項 FAIL（不可被推論覆蓋，實際 {fa_man['origin']}）",
          fa_man["origin"] == "manual"
          and next(c[2] for c in evaluate_checks(_fx_obs(), fa_man) if c[0] == 3) == FAIL)

    # ---------- 3.5) 人工確認關卡 ----------
    print("\n[關卡] preflight=block 時不得顯示確認、不得進入等待")
    blocked = preflight(dict(base, pcs_fault_flag=True), **ok_kw)
    check("block 情境的 verdict 確實為 block（run_live 於此 return，不會呼叫 confirm_gate）",
          blocked[0] == "block")
    _gate_src = src[src.index("    # ---- 自動守門"):src.index("    # ---- YES 之後")]
    check("原始碼順序：block 的 return 在 confirm_gate 之前",
          _gate_src.index("return False, obs, None") < _gate_src.index("confirm_gate("))
    check("confirm_gate 簽章不含 client（等待期間不可能呼叫 API）",
          "client" not in str(inspect.signature(confirm_gate).parameters.keys()))

    print("\n[關卡] 輸入判定：只有精確 YES 才繼續")
    quiet = Log(os.devnull)

    def gate(reader):
        return confirm_gate(quiet, reader, 12.0, "charge", "t1: 01:00~02:00 charge 10kW")

    check("輸入 YES → 繼續", gate(lambda _t: "YES") == (True, "confirmed"))
    for bad, label in ((lambda _t: "yes", "yes"), (lambda _t: "Yes", "Yes"),
                       (lambda _t: "Y", "Y"), (lambda _t: "", "空白"),
                       (lambda _t: "   ", "空格"), (lambda _t: "YES ", "YES＋尾隨空格"),
                       (lambda _t: " YES", "前置空格＋YES"),
                       (lambda _t: "YESS", "YESS"), (lambda _t: "確認", "其他文字"),
                       (lambda _t: None, "reader 回 None")):
        r_ = gate(bad)
        check(f"輸入「{label}」→ 取消", r_[0] is False)

    def _raise(exc):
        def _r(_t):
            raise exc
        return _r

    check("EOF → 取消（reason=eof）", gate(_raise(EOFError())) == (False, "eof"))
    check("KeyboardInterrupt → 取消（reason=interrupt）",
          gate(_raise(KeyboardInterrupt())) == (False, "interrupt"))
    check("TimeoutError → 取消（reason=timeout）",
          gate(_raise(TimeoutError())) == (False, "timeout"))
    check("reader 其他例外 → 取消（fail-safe）",
          gate(_raise(RuntimeError("boom")))[0] is False)
    check(f"逾時常數為 120 秒（實際 {CONFIRM_TIMEOUT_SEC}）", CONFIRM_TIMEOUT_SEC == 120)
    check("逾時預設值由 confirm_gate 簽章帶入",
          inspect.signature(confirm_gate)
          .parameters["timeout"].default == CONFIRM_TIMEOUT_SEC)

    print("\n[關卡] 等待期間不得呼叫 API、不得修改正式狀態或 config")
    import device_control_menu as M_
    import report_monitor as RM    # noqa: E402  Phase 4.2：Monitor Core 已抽離

    class _CountingClient:
        def __init__(self):
            self.n = 0

        def get(self, *_a, **_k):
            self.n += 1
            return []

    spy = _CountingClient()
    rep_before = dict(RM._report)
    auto_before = dict(RM._auto)
    cfg_before = (CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG)
    calls_during = {"n": None}

    def _reader_probe(_t):
        calls_during["n"] = spy.n          # 等待期間的 API 呼叫次數
        return "yes"                       # 取消

    res_cancel = gate(_reader_probe)
    check("取消（輸入 yes）", res_cancel[0] is False)
    check(f"等待期間 API 呼叫次數 = 0（實際 {calls_during['n']}）",
          calls_during["n"] == 0 and spy.n == 0)
    check("取消時未修改 _report", dict(RM._report) == rep_before)
    check("取消時未修改 _auto", dict(RM._auto) == auto_before)
    check("取消時未修改 config 開關",
          (CFG.AUTO_SCHEDULE_REPORT_ENABLED, CFG.AUTO_MONITOR_DEBUG) == cfg_before)
    check("取消時未建立 Session", RM._report["session"] is None)
    # 判定「是否持鎖」只看實際取得鎖的語法（with <lock>: / .acquire()），docstring 提及不算
    _gsrc = _src_of(confirm_gate) + _src_of(read_line_with_deadline)
    check("confirm_gate / read_line_with_deadline 未取得任何鎖（無 with LOCK / .acquire()）",
          not re.search(r"with\s+_(?:CLIENT|SESSION)_LOCK", _gsrc)
          and ".acquire()" not in _gsrc)

    print("\n[關卡] YES 後二次複查")
    rk = dict(auto_enabled=True, active_session=None, battery_power_status="已上電")
    ok2, rs2, ch2 = recheck_after_confirm(base, base, **rk)
    check("狀態未變 → 通過", ok2 is True and ch2 == [])
    ok2, rs2, ch2 = recheck_after_confirm(base, dict(base, pcs_fault_flag=True), **rk)
    check(f"期間出現故障 → 安全退出（{rs2}）", ok2 is False and rs2 == "pcs_fault")
    ok2, rs2, _ = recheck_after_confirm(
        base, dict(base, pcs_charging_flag=False, pcs_discharging_flag=True), **rk)
    check(f"期間變成放電 → 安全退出（{rs2}）", ok2 is False and rs2 == "discharge_only")
    ok2, rs2, _ = recheck_after_confirm(base, base,
                                        **dict(rk, battery_power_status="已下電"))
    check(f"期間電池下電 → 安全退出（{rs2}）", ok2 is False and rs2 == "battery_off")
    ok2, rs2, _ = recheck_after_confirm(base, base, **dict(rk, auto_enabled=False))
    check(f"期間開關被關掉 → 安全退出（{rs2}）",
          ok2 is False and rs2 == "auto_report_disabled")
    ok2, rs2, _ = recheck_after_confirm(base, base,
                                        **dict(rk, active_session=object()))
    check(f"期間出現不明 Session → 安全退出（{rs2}）",
          ok2 is False and rs2 == "unknown_session")
    ok2, rs2, _ = recheck_after_confirm(base, dict(base, pcs_discharging_flag=True), **rk)
    check(f"期間雙旗標衝突 → 安全退出（{rs2}）", ok2 is False and rs2 == "flag_conflict")
    ok2, _rs, ch2 = recheck_after_confirm(
        dict(base, pcs_charging_flag=False), base, **rk)
    check(f"期間排程剛啟動（idle→charging）→ 通過並列出變化（{ch2}）",
          ok2 is True and any("pcs_charging_flag" in c for c in ch2))
    check("複查不要求再次輸入 YES（recheck_after_confirm 簽章無 reader）",
          "reader" not in str(inspect.signature(recheck_after_confirm).parameters.keys()))

    print("\n[關卡] 排程配置方向（唯讀 GET，不修改任何排程）")

    class _SchedClient:
        def __init__(self, tpls, items):
            self.tpls, self.items = tpls, items
            self.paths = []

        def get(self, path, **_k):
            self.paths.append(path)
            if path == RM._SCHED_TPL_LIST:
                return self.tpls
            for tid, v in self.items.items():
                if path == RM._SCHED_ITEM_LIST.format(tid=tid):
                    return v
            return []

    T, I = RM._SCHED_TPL_LIST, RM._SCHED_ITEM_LIST
    en = [{"tempId": "1", "tempName": "t1", "enableFlag": 1}]
    check("啟用模板含充電項目 → charge",
          schedule_plan_direction(_SchedClient(en, {"1": [{"chargeOrDischarge": 1}]}), T, I)[0]
          == "charge")
    check("啟用模板含放電項目 → discharge",
          schedule_plan_direction(_SchedClient(en, {"1": [{"chargeOrDischarge": 2}]}), T, I)[0]
          == "discharge")
    check("充電＋放電並存 → discharge（保守攔阻）",
          schedule_plan_direction(
              _SchedClient(en, {"1": [{"chargeOrDischarge": 1},
                                      {"chargeOrDischarge": 2}]}), T, I)[0] == "discharge")
    check("僅「不充不放」→ none",
          schedule_plan_direction(_SchedClient(en, {"1": [{"chargeOrDischarge": 3}]}), T, I)[0]
          == "none")
    check("停用模板不列入（enableFlag=2）→ none",
          schedule_plan_direction(
              _SchedClient([{"tempId": "1", "enableFlag": 2}],
                           {"1": [{"chargeOrDischarge": 2}]}), T, I)[0] == "none")
    check("模板清單格式非預期 → unknown",
          schedule_plan_direction(_SchedClient({"bad": 1}, {}), T, I)[0] == "unknown")
    # enum 型別防禦：後端若回字串，放電仍必須被攔阻（fail-safe，不可放行）
    check("chargeOrDischarge 為字串 \"2\" → 仍判 discharge（攔阻）",
          schedule_plan_direction(
              _SchedClient(en, {"1": [{"chargeOrDischarge": "2"}]}), T, I)[0] == "discharge")
    check("chargeOrDischarge 為字串 \"1\" → charge",
          schedule_plan_direction(
              _SchedClient(en, {"1": [{"chargeOrDischarge": "1"}]}), T, I)[0] == "charge")
    check("chargeOrDischarge 缺失/非數值 → unknown（不誤判為充電）",
          schedule_plan_direction(_SchedClient(en, {"1": [{}]}), T, I)[0] == "unknown"
          and schedule_plan_direction(
              _SchedClient(en, {"1": [{"chargeOrDischarge": "x"}]}), T, I)[0] == "unknown")
    check("_as_int：bool 不被當成數值（True 不可變成 1=充電）",
          _as_int(True) is None and _as_int(False) is None)
    check("_as_int：int/字串數字正常轉型、其他回 None",
          (_as_int(2), _as_int("2"), _as_int(None), _as_int("x")) == (2, 2, None, None))

    class _FailClient:
        def get(self, *_a, **_k):
            raise RuntimeError("API down")

    check("API 失敗 → unknown（不阻擋、不修改設備）",
          schedule_plan_direction(_FailClient(), T, I)[0] == "unknown")
    check("discharge → plan_direction_verdict 判為 block",
          plan_direction_verdict("discharge") == ("block", "plan_discharge"))
    for d in ("charge", "none", "unknown"):
        check(f"{d} → 交由人工確認（不 block）",
              plan_direction_verdict(d)[0] == "ok")
    sc = _SchedClient(en, {"1": [{"chargeOrDischarge": 1}]})
    schedule_plan_direction(sc, T, I)
    check(f"只呼叫唯讀清單端點（{sc.paths}）",
          all(p == T or p.startswith("/schedule/list/") for p in sc.paths))

    # ---------- 4) 遮蔽機密 ----------
    print("\n[安全] log 遮蔽")
    check("帳號被遮蔽", "hmiUser" not in scrub("HMI login: user=hmiUser", "hmiUser"))
    for s in ("Authorization: Bearer abc.def", "password=1234", "token=xyz",
              "SM2_PUBLIC_KEY=04ab", "Cipher: 3f9a"):
        check(f"含機密關鍵字整行遮蔽：{s[:22]}…", scrub(s).startswith("[已遮蔽"))
    check("一般狀態行不受影響",
          scrub("mode=smart sched=True chg=True") == "mode=smart sched=True chg=True")

    # ---------- 5) 靜態守門：不得含任何控制 API / 不得改開關 ----------
    print("\n[安全] 原始碼靜態檢查（本檔不得含控制能力）")
    # 只掃描「實際執行路徑」：檔頭說明文字與 selftest 自己的關鍵字清單都不算。
    # 以行首 "\ndef <name>(" 作為切點（避免被註解或字串中的同名文字誤切）。
    _b1 = src.index("import argparse")
    _b2 = src.index("\ndef self" + "test(")
    _b3 = src.index("\ndef ma" + "in(")
    body = src[_b1:_b2] + src[_b3:]
    # 關鍵字以片段組合，避免本行自己被掃到（掃描範圍已排除本區塊，這是第二層保險）
    forbidden = ["_execute" + "_live", "_run" + "_script", "OPERA" + "TOR", "--exe" + "cute",
                 "manual" + "Control", "edit" + "ScheduleSwitch", "edit" + "ManualSwitch",
                 "manually" + "PowerOn", "manually" + "PowerOff", "fault" + "Recovery",
                 "pcs_" + "charge", "pcs_" + "discharge", "pcs_stop" + "_power",
                 "handle" + "_choice", "report" + "_stop", "report_on" + "_stop_control",
                 "_stop_and" + "_finalize", "report" + "_pause", "sub" + "process",
                 "requests." + "post", "." + "post(", "." + "put(", "." + "delete("]
    # 註：本腳本只做唯讀 GET 與觀察；上列任一出現即代表可能具備控制/停止能力。
    hits = [k for k in forbidden if k in body]
    check(f"執行路徑不含任何控制/停止 API 關鍵字（命中={hits}）", not hits)
    # 只禁止「指派給 config 屬性」與 setattr；log 訊息或 kwargs 讀取（f(x=CFG.XXX,)）不算
    _assign = re.search(r"(?:CFG|CDR_CFG|config)\.AUTO_SCHEDULE_REPORT_ENABLED\s*=[^=]", body)
    _setattr = re.search(r"setattr\s*\([^)]*AUTO_SCHEDULE_REPORT_ENABLED", body)
    check("不會寫入 AUTO_SCHEDULE_REPORT_ENABLED（僅讀取）",
          _assign is None and _setattr is None)
    check("不 monkeypatch 正式函式（無 M.<name> = 指派）",
          not re.search(r"\bM\.[A-Za-z_]+\s*=(?!=)", body))
    check("不重設正式 Session（無 _report[...] = 指派）",
          not re.search(r"_report\[[^\]]+\]\s*=(?!=)", body)
          and not re.search(r"_report\.update\(", body))
    check("不刪除任何檔案/資料夾",
          not any(k in body for k in ("rmtree", "os.remove", "os.unlink", "shutil.move")))
    # 唯一的「寫入」是 log；其餘 open 皆為讀取
    writes = re.findall(r"io\.open\([^)]*['\"]w['\"]", body)
    check(f"唯一寫入檔案為 log（寫入模式 open 共 {len(writes)} 處）", len(writes) == 1)

    # ---------- 6) 輸出格式 ----------
    print("\n[格式] report() 輸出可產生且含必要欄位")
    tmp = os.path.join(OUTPUT_DIR, "test_output",
                       "phase2_validation_selftest_sample.log")
    lg = Log(tmp, username="hmiUser")
    all_ok = report(lg, _fx_obs(), _fx_facts(CFG), datetime.now())
    text = "\n".join(lg.lines)
    check("fixture 情境判定為 PASS", all_ok)
    for kw in ("每輪 reading 摘要與判斷", "證據彙整", "Session ID", "新建資料夾",
               "共用 client id", "auto state 轉換", "auto_charge_start",
               "samples.csv", "events.csv", "驗證結果", "總結"):
        check(f"log 含區段/欄位「{kw}」", kw in text)
    check("log 不含帳號", "hmiUser" not in text)
    p = lg.save()
    check(f"log 可寫出（{os.path.basename(p)}）", os.path.exists(p))

    ok = all(res)
    print(f"\n== 腳本自我測試 {'PASS' if ok else 'FAIL'}"
          f"（{sum(res)}/{len(res)} 檢查通過）==")
    return ok


def main():
    ap = argparse.ArgumentParser(
        description="Phase 2 實機驗證 B —— 真實排程觸發（唯讀觀察，不送任何控制命令）")
    ap.add_argument("--selftest", action="store_true",
                    help="離線自我測試（不連設備、不建立報告）")
    ap.add_argument("--wait-min", type=int, default=20,
                    help="最長等待實際充電開始的分鐘數（預設 20）")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    started_at = datetime.now()
    log_path = os.path.join(
        OUTPUT_DIR, f"phase2_real_trigger_validation_{started_at:%Y%m%d_%H%M%S}.log")
    import charge_discharge_report as CDR
    log = Log(log_path, username=getattr(CDR, "USERNAME", None))
    ok = False
    try:
        proceeded, obs, facts = run_live(args.wait_min, log)
        if proceeded:
            ok = report(log, obs, facts, started_at)
        else:
            log.section("總結")
            log("  驗證未進行（見上方安全退出原因）；未建立任何報告、未送任何控制命令。")
    except KeyboardInterrupt:
        log("")
        log("[中斷] 使用者按 Ctrl+C。腳本未送任何控制命令；"
            "若正式程式已建立 Session，資料完整保留於報告資料夾。")
    finally:
        p = log.save()
        print(f"\n完整輸出已寫入：{p}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
