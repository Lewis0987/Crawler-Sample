# -*- coding: utf-8 -*-
"""
PCS 排程主開關（schedulePlanSwitch）控制 —— 離線驗證
======================================================================
用途
    驗證 Item 8「PCS 排程主開關」的開/關控制接線正確：
      · ACTIONS 的端點／payload／expect 與已確認來源一致
      · 選單依目前狀態決定送出或略過（已是目標狀態不重複送）
      · 成功判定＝API 送出成功 **且** 回讀狀態一致（read-back）
      · API 未成功 / 回讀未翻轉 / 狀態讀取失敗 → 一律不得顯示成功
      · 不影響既有手動模式控制、智慧模式判定與報告邏輯

是否需要設備
    **不需要**。全程離線：_execute_live / _sched_switch_state / refresh_status_silent
    全部以假物件注入；不啟動任何 subprocess、不連任何設備、不送任何控制 API。

安全性
    不含帳號密碼；不建立/修改 Session；不修改 config；不執行 Git。
    _execute_live 一律被替換成記錄用假物件 —— 即使邏輯有誤也不可能真的送出控制。

用法
    python test_schedule_switch.py         # exit 0 = PASS
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import device_control_menu as M              # noqa: E402
import device_control_operator as OP         # noqa: E402
import device_control_scraper as SC          # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# ======================================================================
# 1. ACTIONS 定義：端點／payload／expect
# ======================================================================
print("\n[排程主開關] ACTIONS 定義與既有已確認來源一致")
_ON, _OFF = OP.ACTIONS["pcs_schedule_on"], OP.ACTIONS["pcs_schedule_off"]
_EP_SCHED = "/schedule/config/editScheduleSwitch"
_EP_MANUAL = "/schedule/config/editManualSwitch"
# 2026-08-11 DevTools：排程主開關有專屬端點，payload 與手動模式完全相同 →
# **語意由端點決定**，不可依 payload 判斷該送哪一支。
check("pcs_schedule_on 走專屬端點 editScheduleSwitch",
      _ON["endpoint"] == _EP_SCHED and _ON["method"] == "PUT")
check("pcs_schedule_off 同上", _OFF["endpoint"] == _EP_SCHED and _OFF["method"] == "PUT")
check("排程主開關不再誤用 editManualSwitch（2026-08-10 ON 失效的根因）",
      _ON["endpoint"] != _EP_MANUAL and _OFF["endpoint"] != _EP_MANUAL)
check("手動模式 action 仍走 editManualSwitch（兩者未混用）",
      OP.ACTIONS["pcs_manual_on"]["endpoint"] == _EP_MANUAL
      and OP.ACTIONS["pcs_manual_off"]["endpoint"] == _EP_MANUAL)
check("editScheduleSwitch 已加入控制白名單（否則 _assert_control_allowed 會攔下）",
      _EP_SCHED in OP._ALLOWED_CONTROL_PATHS)
check("editScheduleSwitch 不在禁止清單", _EP_SCHED not in OP._FORBIDDEN_EXACT)
check("_assert_control_allowed 放行 editScheduleSwitch（不拋例外）",
      OP._assert_control_allowed(_EP_SCHED) is None)
check("editScheduleSwitch 仍登記於 scraper 的 CONTROL_APIS（唯讀掃描一律不呼叫）",
      _EP_SCHED in {c["path"] for c in SC.CONTROL_APIS})
# payload 與 pcs_manual_* 相同是 DevTools 實抓結果，非巧合 —— 鎖住以防日後有人「修正」它
check("pcs_schedule_on.payload == pcs_manual_off.payload（DevTools 實抓：兩端點同 payload）",
      _ON["payload"] == OP.ACTIONS["pcs_manual_off"]["payload"])
check("pcs_schedule_off.payload == pcs_manual_on.payload（同上）",
      _OFF["payload"] == OP.ACTIONS["pcs_manual_on"]["payload"])
check("payload 欄位未超出已確認的三個 key",
      set(_ON["payload"]) == {"schedulePlanSwitchId", "schedulePlanSwitch", "manualModeSwitch"})
check("開啟送 schedulePlanSwitch=1、關閉送 0",
      _ON["payload"]["schedulePlanSwitch"] == 1 and _OFF["payload"]["schedulePlanSwitch"] == 0)
check("expect 驗證 schedulePlanSwitch（不是 manualModeSwitch）",
      _ON["expect"] == {"schedulePlanSwitch": 1} and _OFF["expect"] == {"schedulePlanSwitch": 0})
check("kind=pcs_manual → 沿用既有 getScheduleSwitch 回讀驗證",
      _ON["kind"] == "pcs_manual" and _OFF["kind"] == "pcs_manual")
check("未標記 high_risk（不需 YES 二次確認；非電池類操作）",
      not _ON.get("high_risk") and not _OFF.get("high_risk"))
check("既有 pcs_manual_on/off 定義未被更動",
      OP.ACTIONS["pcs_manual_on"]["expect"] == {"manualModeSwitch": 1}
      and OP.ACTIONS["pcs_manual_off"]["expect"] == {"manualModeSwitch": 0})

print("\n[排程主開關] 回讀來源與 timeout")
check("回讀沿用狀態頁同一欄位 PCS排程開關狀態（未另建第二套來源）",
      M.VERIFY_EXPECT["pcs_schedule_on"] == ("PCS", "PCS排程開關狀態", "開")
      and M.VERIFY_EXPECT["pcs_schedule_off"] == ("PCS", "PCS排程開關狀態", "關"))
check("該欄位確實由 scraper 以 schedulePlanSwitch 產生",
      SC._SCHEDULE_DISPLAY[True] == "開" and SC._SCHEDULE_DISPLAY[False] == "關")
check("「開」/「關」互不為子字串（substring 比對不會誤判）",
      "開" not in "關" and "關" not in "開")
check("暫無資料（None）兩者皆不符 → 只會逾時，不會誤判成功",
      "開" not in SC._SCHEDULE_DISPLAY[None] and "關" not in SC._SCHEDULE_DISPLAY[None])
# 2026-08-10 實機：editManualSwitch 回 200/operation successful、control_success=True，
# 但 getScheduleSwitch 回讀超過 15s 才反映 → 曾把實際成功誤判為 FAIL。
check(f"排程主開關 verify timeout 沿用 AUTO_POLL_TIMEOUT_SEC=90"
      f"（實際 {M._verify_timeout('pcs_schedule_on')}）",
      M._verify_timeout("pcs_schedule_on") == M.AUTO_POLL_TIMEOUT_SEC
      and M._verify_timeout("pcs_schedule_off") == M.AUTO_POLL_TIMEOUT_SEC)
check(f"AUTO_POLL_TIMEOUT_SEC 仍為 90（實際 {M.AUTO_POLL_TIMEOUT_SEC}）",
      M.AUTO_POLL_TIMEOUT_SEC == 90)
check("未再為排程主開關另立短 timeout（VERIFY_TIMEOUT 不含 pcs_schedule）",
      "pcs_schedule" not in M.VERIFY_TIMEOUT)
check("兩個方向使用同一套 read-back timeout",
      M._verify_timeout("pcs_schedule_on") == M._verify_timeout("pcs_schedule_off"))
check("timeout 於函式內即時取值（改 AUTO_POLL_TIMEOUT_SEC 立即生效，不會各走各的）",
      (lambda: [M.__setattr__("AUTO_POLL_TIMEOUT_SEC", 123),
                M._verify_timeout("pcs_schedule_on") == 123,
                M.__setattr__("AUTO_POLL_TIMEOUT_SEC", 90)][1])())
check(f"既有 pcs 類 timeout 未受影響（pcs_manual_on = {M._verify_timeout('pcs_manual_on')}s）",
      M._verify_timeout("pcs_manual_on") == 60 and M._verify_timeout("pcs_charge") == 60)
check("既有其他設備 timeout 未受影響",
      M._verify_timeout("ac_on") == 180 and M._verify_timeout("battery_power_on") == 60)


# ======================================================================
# 2. 選單流程（完全離線：_execute_live 被替換為記錄器）
# ======================================================================
def run_menu(switch_state, answers):
    """
    以指定的 (schedulePlanSwitch, manualModeSwitch) 跑一次選單，回傳送出的 action 清單。
    switch_state=None 代表狀態讀取失敗。
    """
    sent = []
    _bak = (M._sched_readonly_client, M._sched_switch_state, M._execute_live, M._menu_input)
    it = iter(list(answers) + ["0"] * 5)          # 耗盡後一律返回，任何情況都不會卡住
    ps, ms = switch_state if switch_state is not None else (None, None)
    M._sched_readonly_client = lambda *a, **k: object()
    M._sched_switch_state = lambda *a, **k: (ps, ms, {"_error": "boom"} if ps is None else {})
    M._execute_live = lambda args, action, label: sent.append(action)
    M._menu_input = lambda *a, **k: next(it, "0")
    try:
        M._pcs_schedule_switch_menu()
    finally:
        (M._sched_readonly_client, M._sched_switch_state,
         M._execute_live, M._menu_input) = _bak
    return sent


print("\n[排程主開關] 選單：依目前狀態決定送出或略過")
check("① 初始 OFF → 選 1 → 送出 pcs_schedule_on（改用專屬端點後已解除封鎖）",
      run_menu((0, 1), ["1"]) == ["pcs_schedule_on"])
check("② 初始 ON → 選 2 → 送出 pcs_schedule_off",
      run_menu((1, 0), ["2"]) == ["pcs_schedule_off"])
check("③ 初始 ON → 選 1（已是此狀態）→ 不重複送命令",
      run_menu((1, 0), ["1"]) == [])
check("④ 初始 OFF → 選 2（已是此狀態）→ 不重複送命令",
      run_menu((0, 1), ["2"]) == [])
check("⑧ 狀態讀取失敗（None）→ 兩個方向都仍可送出，且不誤判「已是此狀態」",
      run_menu(None, ["1"]) == ["pcs_schedule_on"]
      and run_menu(None, ["2"]) == ["pcs_schedule_off"])
check("⑨ 選 0 → 直接返回，未送出任何控制", run_menu((0, 1), ["0"]) == [])
check("無效選項 → 不送出控制，可繼續操作",
      run_menu((0, 1), ["9", "x", "0"]) == [])
check("連續操作：每次都送（狀態由設備決定，不自行快取）",
      run_menu((1, 0), ["2", "2"]) == ["pcs_schedule_off", "pcs_schedule_off"])
# unsup 機制本身保留供日後使用 —— 以合成項目驗證，不再綁在 pcs_schedule_on 上
_bak_tbl = dict(M.SCHEDULE_SWITCH_ACTIONS)
try:
    M.SCHEDULE_SWITCH_ACTIONS["1"] = ("pcs_schedule_on", "L", "開啟排程主開關", 1, "測試用原因")
    check("unsup 機制仍有效（標記後即不送出，供日後其他 action 使用）",
          run_menu((0, 1), ["1"]) == [])
finally:
    M.SCHEDULE_SWITCH_ACTIONS.clear()
    M.SCHEDULE_SWITCH_ACTIONS.update(_bak_tbl)
check("還原後開啟方向恢復可送出", run_menu((0, 1), ["1"]) == ["pcs_schedule_on"])
_bak_cli = M._sched_readonly_client
M._sched_readonly_client = lambda *a, **k: None
try:
    _sent = []
    M._execute_live = lambda *a, **k: _sent.append(1)
    M._pcs_schedule_switch_menu()
    check("登入/連線失敗（client=None）→ 直接返回，未送出任何控制", _sent == [])
finally:
    M._sched_readonly_client = _bak_cli

print("\n[排程主開關] 選單狀態：可用但明示為暫定")
_src = open(os.path.join(HERE, "device_control_menu.py"), encoding="utf-8").read()
_fn = _src[_src.index("def _pcs_schedule_switch_menu("):
           _src.index("def handle_choice(")]
# 註：「尚未開放」現在**刻意**存在，但只針對開啟方向（見下方 unsup 區塊）；
# 關閉方向必須維持實機可用 —— 以實際送出行為驗證，而非字串比對。
check("關閉方向仍實際送出控制（未整支退回唯讀）",
      run_menu((1, 0), ["2"]) == ["pcs_schedule_off"])
check("已實際呼叫 _execute_live 送出控制", "_execute_live(" in _fn)
# 2026-08-10 檢討：本項與手動模式共用 payload → 必須對使用者明示，不得宣稱為獨立控制
check("送出前告知將同時切換手動模式（payload 實際同時帶兩個欄位）",
      "手動模式" in _fn and "同時" in _fn)
check("docstring 記錄端點來源與沿革（誤用 editManualSwitch 的教訓）",
      "editScheduleSwitch" in _fn and "editManualSwitch" in _fn)
check("已移除「暫定」標示（功能正式開放）", "暫定" not in _fn)
check("選單標題不再帶任何保留字樣", 'print("\\nPCS 排程主開關")' in _fn)
# 「尚未開放」字樣只保留在通用的 unsup 分支（供日後其他 action 使用），不得綁定排程主開關
check("「尚未開放」僅出現在通用 unsup 分支",
      _fn.count("尚未開放") == 1 and "此功能尚未開放：{unsup}" in _fn)
_opsrc = open(os.path.join(HERE, "device_control_operator.py"), encoding="utf-8").read()
check("ACTIONS 不再標記 UNSUPPORTED / PENDING",
      "UNSUPPORTED" not in _opsrc and "PENDING API CONFIRMATION" not in _opsrc)
check("註解記錄「兩端點同 payload、語意由端點決定」這個關鍵事實",
      "端點本身才是語意所在" in _opsrc)

print("\n[排程主開關] HOLD 已解除，但機制保留")
_ON_ENTRY, _OFF_ENTRY = M.SCHEDULE_SWITCH_ACTIONS["1"], M.SCHEDULE_SWITCH_ACTIONS["2"]
check("兩個方向皆未標記未支援原因", _ON_ENTRY[4] is None and _OFF_ENTRY[4] is None)
check("operator 層兩個方向皆非 confirmed=False",
      _ON.get("confirmed") is not False and _OFF.get("confirmed") is not False)
check("目前沒有任何 action 停在 HOLD",
      [a for a, s in OP.ACTIONS.items() if s.get("confirmed") is False] == [])
check("action 級 HOLD 機制仍保留於 run()（供日後未確認 action 使用）",
      'spec.get("confirmed") is False' in _opsrc and "[HOLD]" in _opsrc)
check("未調高 timeout（維持 AUTO_POLL_TIMEOUT_SEC=90）",
      M.AUTO_POLL_TIMEOUT_SEC == 90 and "120" not in _fn and "180" not in _fn)
check("個別排程模板（Item 9）仍維持唯讀「尚未開放」（本次不擴大範圍）",
      "尚未開放" in _src[_src.index("def _pcs_schedule_template_submenu("):
                       _src.index("def _pcs_schedule_switch_menu(")])


# ======================================================================
# 3. read-back：API 成功仍必須回讀一致才算 PASS
# ======================================================================
_POLLS = {"n": 0}          # 每次 run_verify 記錄回讀被實際輪詢幾次
_REAL_SLEEP = time.sleep   # run_verify 會暫時覆寫；結束後須還原成這一個


def run_verify(action, status_seq, timeout=None, control_success=True):
    """
    離線跑 wait_and_verify：以固定/序列狀態值餵回讀，回傳其結果（True/False/None）。
    timeout：暫時覆寫 AUTO_POLL_TIMEOUT_SEC（排程主開關的 timeout 來源）。
      逾時案例一律傳 0，否則測試會真的空轉 90 秒。

    ⚠️ wait_and_verify 有**兩個**等待點，兩個都必須中和，否則測試會依賴機器負載：
        主迴圈   time.sleep(1)                ← M.time.sleep 已覆寫
        輪詢執行緒 stop.wait(VERIFY_INTERVAL)  ← threading.Event.wait，覆寫 sleep **無效**
      漏掉第二個時，「延遲 25 輪才翻轉」的案例會真的耗掉 25×2=50 秒實際時間，
      而 deadline 是 90 秒（同樣走實際時間）——餘裕僅 40 秒，且主迴圈因 sleep 被
      取消而變成 busy spin。單獨執行時通過，放進 Regression Runner（前後有多行程
      套件在跑）就可能超過 90 秒而逾時 → 間歇性 FAIL。
      （2026-08-12 Regression 首跑即為此，PCS schedule switch 79/80。）
      改為覆寫 VERIFY_INTERVAL：延遲仍是「25 輪之後才翻轉」（語意不變、輪數不變），
      但不再綁定實際秒數，套件執行時間也從約 100 秒降到 1 秒以內。
    """
    seq = list(status_seq)
    _POLLS["n"] = 0
    _bak = (M.refresh_status_silent, M.RM._load_json, M.time.sleep,
            M.AUTO_POLL_TIMEOUT_SEC, M.VERIFY_INTERVAL)
    if timeout is not None:
        M.AUTO_POLL_TIMEOUT_SEC = timeout
    M.time.sleep = lambda *_a: None                    # 主迴圈：不真的等待
    M.VERIFY_INTERVAL = 0.01                           # 輪詢執行緒：不真的等待

    def _status(*_a, **_k):
        _POLLS["n"] += 1
        if len(seq) > 1:
            return {"PCS": {"PCS排程開關狀態": seq.pop(0)}}
        return {"PCS": {"PCS排程開關狀態": seq[0]}} if seq else {}

    M.refresh_status_silent = _status
    M.RM._load_json = lambda *a, **k: {"control_success": control_success}
    try:
        return M.wait_and_verify(action)
    finally:
        (M.refresh_status_silent, M.RM._load_json, M.time.sleep,
         M.AUTO_POLL_TIMEOUT_SEC, M.VERIFY_INTERVAL) = _bak


print("\n[排程主開關] read-back 判定")
print("  --- 以下為被測程式的正常輸出 ---")
check("① 開啟後回讀＝開 → 驗證成功",
      run_verify("pcs_schedule_on", ["開"]) is True)
check("② 關閉後回讀＝關 → 驗證成功",
      run_verify("pcs_schedule_off", ["關"]) is True)
# 這是 2026-08-10 實機回歸的核心案例：狀態延遲數十秒才翻轉，在 90s 內仍必須 PASS。
_t0 = time.monotonic()
_delay_on = run_verify("pcs_schedule_on", ["關"] * 25 + ["開"])
_delay_polls, _delay_sec = _POLLS["n"], time.monotonic() - _t0
check("① 回讀延遲（連續多輪仍為關，之後才翻成開）→ 仍在 timeout 內 → 驗證成功",
      _delay_on is True)
check(f"① 延遲路徑確實被走到：回讀被輪詢 {_delay_polls} 次（≥26 才可能翻轉）",
      _delay_polls >= 26)
# 確定性守門：本案例不得依賴實際秒數，否則機器負載會決定成敗（間歇性 FAIL 的來源）。
check(f"① 不依賴實際時間：{_delay_sec:.2f}s 完成，遠低於 {M.AUTO_POLL_TIMEOUT_SEC}s deadline",
      _delay_sec < 10)
check("② 關閉方向的延遲回讀同樣可成功",
      run_verify("pcs_schedule_off", ["開"] * 25 + ["關"]) is True)
check(f"② 延遲路徑同樣確實被走到（輪詢 {_POLLS['n']} 次）", _POLLS["n"] >= 26)
check("⑦ API 成功但回讀未翻轉（仍為關）→ 逾時，不得顯示成功",
      run_verify("pcs_schedule_on", ["關"], timeout=0) is False)
check("⑦ 關閉方向同理（仍為開）→ 逾時",
      run_verify("pcs_schedule_off", ["開"], timeout=0) is False)
check("⑧ 回讀為「暫無資料」→ 逾時，不得顯示成功",
      run_verify("pcs_schedule_on", ["暫無資料"], timeout=0) is False)
check("⑧ 回讀完全讀不到欄位（None）→ 逾時且不拋例外",
      run_verify("pcs_schedule_on", [None], timeout=0) is False)
check("⑥ 控制未成功送出（control_success=False）→ 略過等待，不宣告成功",
      run_verify("pcs_schedule_on", ["開"], control_success=False) is None)
check("⑤ 逾時上限確實生效（timeout=0 立即結束，不卡住畫面）",
      run_verify("pcs_schedule_on", ["關"], timeout=0) is False)
print("  --- 被測程式輸出結束 ---")
check("read-back 測試已還原兩個等待點與 timeout（未留下副作用）",
      M.VERIFY_INTERVAL == 2 and M.AUTO_POLL_TIMEOUT_SEC == 90
      and M.time.sleep is _REAL_SLEEP)


# ======================================================================
# 4. 不影響既有功能
# ======================================================================
print("\n[排程主開關] 未影響既有功能")
check("⑩ 既有查詢欄位清單未改動（PCS排程開關狀態 仍在狀態頁）",
      "PCS排程開關狀態" in SC._UI_FIELDS["PCS"])
check("⑩ 手動模式開關欄位亦仍在（兩者並存顯示，未互相取代）",
      "PCS手動模式開關" in SC._UI_FIELDS["PCS"])
check("⑪ 智慧/手動模式判定仍由 getScheduleSwitch 兩欄位組合決定（未改動）",
      SC._SCHEDULE_DISPLAY[True] == "開" and hasattr(SC, "_RUNMODE_SMART"))
check("⑪ Menu 2/3（手動模式開關）仍指向原 action",
      M.MENU_ACTIONS["2"][0] == "pcs_manual_on" and M.MENU_ACTIONS["3"][0] == "pcs_manual_off")
check("⑪ 排程主開關未被加進 MENU_ACTIONS（走專屬子選單，不影響既有 dispatch）",
      all(a not in [v[0] for v in M.MENU_ACTIONS.values()]
          for a in ("pcs_schedule_on", "pcs_schedule_off")))
check("⑫ 子選單不觸碰報告模組（不呼叫任何 RM.report_* / should_auto_end）",
      "report_" not in _fn and "should_auto_end" not in _fn)
check("⑫ 未改動 continuity 相關常數",
      "AUTO_SCHEDULE_CONTINUITY_GRACE_SEC" not in _fn
      and "AUTO_NEXT_PLAN_GAP_SEC" not in _fn)
check("人工充放電安全閘未被改動（智慧模式仍阻擋人工控制）",
      "no_mode_enabled" in open(os.path.join(HERE, "device_control_operator.py"),
                                encoding="utf-8").read())


# ======================================================================
# 5. operator 層 HOLD：CLI 直呼也不得送出未確認的 action
# ======================================================================
print("\n[排程主開關] operator 層：兩個方向皆可 execute，HOLD 機制仍可用")


_url_on, _body_on = [None], [None]


def run_cli(action):
    """
    離線跑 operator.run()：攔截登入與 HTTP，回傳 (是否送出請求, 輸出文字)。
    不連任何設備 —— session.put/post 一旦被呼叫即記錄其 URL/payload 後立即中斷，
    據此同時驗證「HOLD 有沒有擋住」與「真的要送出時送的是哪一支、帶什麼」。
    """
    import io as _io
    import contextlib as _ctx
    sent = []
    _url_on[0], _body_on[0] = None, None

    class _Sess:
        def _rec(self, method, a, k):
            sent.append((method, a, k))
            _url_on[0] = a[0] if a else k.get("url")
            _body_on[0] = k.get("json")
            raise ConnectionError("offline: 測試不實際送出")

        def put(self, *a, **k):
            self._rec("PUT", a, k)

        def post(self, *a, **k):
            self._rec("POST", a, k)

    class _Cli:
        base_url = "http://offline.invalid"

        def __init__(self, *a, **k):
            self.session = _Sess()

        def login_hmi(self, *a, **k):
            return "offline-token"        # 假裝登入成功 → 才會走到 HOLD 判斷

        def get(self, *a, **k):
            return {}

    _bak = OP.ApiClient
    OP.ApiClient = _Cli
    buf = _io.StringIO()
    try:
        with _ctx.redirect_stdout(buf):
            OP.run(action, execute=True, assume_yes=True)
    except Exception as e:                              # noqa: BLE001
        buf.write(f"\n[EXC] {type(e).__name__}: {e}")
    finally:
        OP.ApiClient = _bak
    return bool(sent), buf.getvalue()


_sent_on, _out_on = run_cli("pcs_schedule_on")
check("CLI pcs_schedule_on --execute → 未被 HOLD（實際嘗試送出請求）",
      _sent_on is True and "[HOLD]" not in _out_on)
check("送出的 URL 為 editScheduleSwitch（不是 editManualSwitch）",
      _EP_SCHED in _url_on[0] and _EP_MANUAL not in _url_on[0])
check("送出的 payload 為 DevTools 實抓那組（ON）",
      _body_on[0] == {"schedulePlanSwitchId": 1, "schedulePlanSwitch": 1, "manualModeSwitch": 0})
_sent_off, _out_off = run_cli("pcs_schedule_off")
check("CLI pcs_schedule_off --execute → 未被 HOLD（實際嘗試送出請求）",
      _sent_off is True and "[HOLD]" not in _out_off)
check("送出的 URL 為 editScheduleSwitch（OFF 方向）", _EP_SCHED in _url_on[0])
check("送出的 payload 為 DevTools 實抓那組（OFF）",
      _body_on[0] == {"schedulePlanSwitchId": 1, "schedulePlanSwitch": 0, "manualModeSwitch": 1})
# HOLD 機制本身：以合成 action 驗證仍會阻擋（不再綁在排程主開關上）
OP.ACTIONS["_test_hold_only"] = dict(_OFF, confirmed=False, hold_reason="測試用：未確認")
try:
    _sent_h, _out_h = run_cli("_test_hold_only")
    check("HOLD 機制仍有效：confirmed=False 的 action 不送出任何請求", _sent_h is False)
    check("HOLD 輸出含 [HOLD]、原因與「不進行 read-back 等待」",
          "[HOLD]" in _out_h and "測試用：未確認" in _out_h
          and "不進行 read-back 等待" in _out_h)
finally:
    OP.ACTIONS.pop("_test_hold_only", None)
check("測試用 action 已移除（未污染正式 ACTIONS）", "_test_hold_only" not in OP.ACTIONS)

ok = all(RESULTS)
print(f"\n== PCS 排程主開關 {'PASS' if ok else 'FAIL'}"
      f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
sys.exit(0 if ok else 1)
