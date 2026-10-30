# -*- coding: utf-8 -*-
"""
充／放電認證測試報告 —— P3 選單操作（device_control_menu 呼叫）
==============================================================
選單**只建立請求、讀取狀態與結果**，不寫任何認證資料，也不需要（不會搶）Monitor Ownership。
實際記錄由背景服務（Monitor Owner，角色 service）處理。

  21 開始認證測試          輸入電櫃規格（不固定 340KW）、循環數（選填，不自動推算）、冷氣溫度、硬體概述、測試條件
  22 結束認證測試          只結束記錄，不送停止指令；設備仍運行 → 服務端提示並標記未完成
  23 階段標記／不確定項目確認
  24 立即截圖／手動補拍
  25 重新產生認證報告      每次產生新檔，不覆蓋舊報告
  26 查看目前認證狀態
  27 查詢請求結果          等待逾時（結果待確認）時用請求 ID 查詢；**不要重複送出**

充電、放電、停止仍使用既有 Menu 4～7，本模組不新增任何控制。
"""

import os
from datetime import datetime

import cert_report_config as CFG
import cert_requests as REQ

CHOICES = {
    "21": "開始認證測試",
    "22": "結束認證測試",
    "23": "階段標記／不確定項目確認",
    "24": "立即截圖／手動補拍",
    "25": "重新產生認證報告",
    "26": "查看目前認證狀態",
    "27": "查詢請求結果",
}
SERVICE_STALE_SEC = 15


def enabled():
    return bool(CFG.CERT_MODE_ENABLED)


def menu_section():
    return ("認證測試", [(k, v) for k, v in CHOICES.items()])


def stage_post_stop_soc(st):
    """
    停止後 SOC：階段結束後首筆觀測待機（st.end_B）的實際 SOC；沒有 end_B 或該筆 SOC 無效 → None。
    只供操作人員參考（不往後找其他筆、不依方向推測 0／100），不改變階段範圍，也不參與滿充／滿放判定。
    階段運行範圍的 SOC 仍是 st.start → st.A（最後觀測運行）。
    """
    from cert_stage_engine import _num
    return _num(st.end_B.values.get("soc")) if st.end_B is not None else None


class UI:
    """input／print 可注入（模擬測試）。"""

    def __init__(self, root=None, inp=input, out=print, now=datetime.now, wait_sec=None, is_owner=None):
        self.root = root or REQ.cert_root()
        self.inp = inp
        self.out = out
        self.now = now
        self.wait_sec = CFG.CERT_MENU_WAIT_SEC if wait_sec is None else wait_sec
        self.is_owner = is_owner or (lambda: False)

    # ---- 共用 ----------------------------------------------------------------
    def ask(self, prompt, default=""):
        try:
            v = self.inp(prompt)
        except (EOFError, KeyboardInterrupt):
            return default
        v = (v or "").strip()
        return v if v else default

    def service_alive(self):
        st = REQ.read_status(self.root)
        if not isinstance(st, dict):
            return False, "背景服務尚未回報認證狀態（服務未執行、或認證功能未在服務端啟用）"
        try:
            age = (self.now() - datetime.strptime(st["updated_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        except (KeyError, ValueError):
            return False, "狀態檔格式異常"
        if age > SERVICE_STALE_SEC:
            return False, f"背景服務狀態已 {age:.0f} 秒未更新（服務可能未執行）"
        return True, f"背景服務運作中（pid {st.get('pid')}）"

    def submit(self, rtype, payload=None, cert_id=None):
        """送出請求並等待結果；逾時顯示「結果待確認」，**不自動重送**。回傳結果 dict 或 None。"""
        rid = REQ.new_request(self.root, rtype, payload, cert_id=cert_id, now=self.now())
        self.out(f"已送出請求 {rid}（{REQ.TYPES[rtype]}），等待背景服務處理（最長 {self.wait_sec:g} 秒）…")
        res = REQ.wait_result(self.root, rid, self.wait_sec)
        if res is None:
            self.out(f"⚠ 結果待確認：請求 {rid} 尚未有處理結果（不代表失敗）。")
            self.out("  請勿重複送出；稍後用「27 查詢請求結果」查詢。"
                     f"若服務未執行，請求建立 {CFG.CERT_REQUEST_EXPIRE_SEC} 秒後會標示逾期、不執行。")
            return None
        self.show_result(res)
        return res

    def show_result(self, res):
        self.out(f"【{res.get('type_text')}】{res.get('status_text')}｜{res.get('message')}")
        if res.get("cert_id"):
            self.out(f"  所屬認證：{res['cert_id']}｜請求 {res['request_id']}｜處理時間 {res.get('processed_at')}")

    def pick_cert(self, allow_ended=True):
        cid = REQ.active_on_disk(self.root)
        if cid and not allow_ended:
            return cid
        certs = sorted((d for d in os.listdir(self.root) if d.startswith("CERT_")), reverse=True) \
            if os.path.isdir(self.root) else []
        if not certs:
            self.out("尚無任何認證測試。")
            return None
        if cid and allow_ended:
            self.out(f"目前進行中：{cid}")
        for i, c in enumerate(certs[:9], 1):
            st = REQ.read_json(os.path.join(self.root, c, "cert_state.json")) or {}
            self.out(f"  {i}. {c}（{'記錄中' if st.get('status') == 'recording' else '已結束'}）")
        v = self.ask("選擇認證（Enter＝第 1 項）：", "1")
        try:
            return certs[int(v) - 1]
        except (ValueError, IndexError):
            return v if v in certs else None

    # ---- 21 開始 --------------------------------------------------------------
    def start(self):
        if self.is_owner():
            self.out("⚠ 本選單視窗目前持有 Monitor Ownership → 背景服務無法接手記錄。")
            self.out("  認證必須由背景服務記錄（關閉選單也不中斷）：請關閉本視窗、確認服務執行後再開始。")
            return None
        cid = REQ.active_on_disk(self.root)
        if cid:
            self.out(f"已有進行中的認證 {cid}，不能重複開始（請先結束，或查看狀態）。")
            return None
        alive, why = self.service_alive()
        if not alive:
            self.out(f"⚠ {why}")
            if self.ask("仍要送出請求嗎？（y/N）：", "n").lower() != "y":
                self.out("已取消。")
                return None
        self.out("請輸入本次測試資訊（電櫃規格與循環數依本次測試輸入，不會自動帶入或推算）：")
        spec = self.ask("電櫃規格（例 340KW、250KW；Enter＝不填，報告顯示「電櫃規格未設定」）：")
        cyc = self.ask("循環數（整數，Enter＝不填；不會自動推算）：")
        if cyc:
            try:
                assert int(cyc) > 0
            except (ValueError, AssertionError):
                self.out("循環數必須是正整數，已取消。")
                return None
        payload = {"cabinet_spec": spec, "cycles": int(cyc) if cyc else None,
                   "aircon": self.ask("冷氣溫度（選填）："), "hardware": self.ask("硬體概述（選填）："),
                   "conditions": self.ask("測試條件（選填，例：定功率 170kW）：")}
        self.out(f"確認：電櫃規格 {spec or '（未設定）'}｜循環數 {cyc or '（不填）'}｜"
                 f"冷氣 {payload['aircon'] or '-'}｜硬體 {payload['hardware'] or '-'}｜條件 {payload['conditions'] or '-'}")
        self.out("開始後背景服務持續記錄到「結束認證測試」為止（含休息）；充放電仍用 Menu 4～7 操作。")
        if self.ask("輸入 YES 開始：") != "YES":
            self.out("已取消。")
            return None
        return self.submit("start_cert", payload)

    # ---- 22 結束 --------------------------------------------------------------
    def end(self):
        cid = REQ.active_on_disk(self.root)
        if not cid:
            self.out("目前沒有進行中的認證。")
            return None
        st = REQ.read_status(self.root) or {}
        self.out(f"即將結束認證 {cid} 的記錄（最後方向：{st.get('last_direction', '未知')}）。")
        self.out("⚠ 結束認證只結束記錄，**不會**停止設備；設備若仍在充放電，報告會標記未完成。")
        if self.ask("輸入 YES 結束：") != "YES":
            self.out("已取消。")
            return None
        return self.submit("end_cert", cert_id=cid)

    # ---- 23 標記／確認 ---------------------------------------------------------
    def stage_rows(self, cid):
        """
        唯讀分析該認證的階段（observations／decisions／commands → 階段判定），記錄中與已結束的認證都適用。
        與服務端 _h_stage_label 比對階段用的是同一條分析路徑（CertRuntime._offline().analysis()），
        所以送出的 stage_start 一定是服務端認得的階段開始時間。不寫任何認證資料。
        """
        from cert_runtime import CertRuntime, _iso
        from cert_stage_engine import _num, stage_names
        folder = os.path.join(self.root, cid)
        res = CertRuntime(self.root, reader=None, enable_shots=False, log=lambda *a: None)._offline(folder).analysis()
        recording = (REQ.read_json(os.path.join(folder, "cert_state.json")) or {}).get("status") == "recording"
        names = stage_names(res.stages)

        def soc(v):
            return "N/A" if v is None else f"{v:g}%"
        rows = []
        for st in res.stages:
            # 記錄中且最後一個階段尚未結束（資料結束時仍運行）→ 進行中：尚未停止，不顯示停止後 SOC
            active = recording and st is res.stages[-1] and st.end_by == "data_end"
            rows.append({"no": st.no, "dir": st.dir, "name": names[st.no], "start": _iso(st.start.t),
                         "t0": st.start.t, "t1": st.A.t, "label": st.label, "active": active,
                         "soc0": soc(_num(st.start.values.get("soc"))), "soc1": soc(_num(st.A.values.get("soc"))),
                         "soc_post": None if active else soc(stage_post_stop_soc(st))})
        return rows

    def mark(self):
        cid = self.pick_cert()
        if not cid:
            return None
        kind = self.ask("1. 階段標記（滿充／滿放／未滿／中止）　2. 不確定項目確認：", "1")
        if kind == "1":
            return self.mark_stage(cid)
        st = REQ.read_status(self.root) or {}
        if st.get("cert_id") != cid:
            self.out("（此認證已結束；階段清單以最新報告摘要為準，確認後請「25 重新產生認證報告」）")
        items = st.get("uncertain") if st.get("cert_id") == cid else None
        if items:
            for u in items:
                self.out(f"  {u['item_id']}｜第 {u['stage_no']} 階段｜{u['A']} → {u['R']}｜已確認 {u.get('decided') or '-'}")
        item = self.ask("不確定項目 ID：")
        ch = self.ask("1. 同一階段（merge）　2. 拆成兩個階段（split）：")
        choice = {"1": "merge", "2": "split"}.get(ch)
        if not item or not choice:
            self.out("輸入不完整，已取消。")
            return None
        return self.submit("confirm_item", {"item_id": item, "choice": choice,
                                            "note": self.ask("備註（選填）：")}, cert_id=cid)

    def mark_stage(self, cid):
        """列出分析出的階段 → 選編號 → 依方向列出可用標記 → 送出（stage_start 由程式帶入，不需手動輸入）。"""
        try:
            rows = self.stage_rows(cid)
        except Exception as e:                          # noqa: BLE001
            self.out(f"無法分析 {cid} 的階段（{type(e).__name__}: {e}），未送出請求。")
            return None
        if not rows:
            self.out(f"{cid} 目前沒有已判定的充／放電階段，未送出請求。")
            return None
        self.out("【請選擇階段】")
        for r in rows:
            end = r["t1"].strftime("%H:%M:%S" if r["t1"].date() == r["t0"].date() else "%Y-%m-%d %H:%M:%S")
            state = "進行中" if r["active"] else (f"目前：{r['label']}" if r["label"] else "未標記")
            self.out(f"  {r['no']}. {r['name']}" + (f"（目前：{r['label']}）" if r["label"] else ""))
            post = "" if r["soc_post"] is None else f"｜停止後 SOC {r['soc_post']}"
            self.out(f"     {r['t0']:%Y-%m-%d %H:%M:%S} ～ {end}｜SOC {r['soc0']} → {r['soc1']}{post}｜狀態：{state}")
        self.out("  0. 返回")
        v = self.ask("階段編號（0＝返回）：", "0")
        if v == "0":
            self.out("已返回，未送出請求。")
            return None
        row = next((r for r in rows if str(r["no"]) == v), None)
        if row is None:
            self.out(f"沒有階段編號「{v}」，未送出請求。")
            return None
        if row["active"]:
            # 階段尚未結束：還不能確認是否滿充／滿放或未滿，只提供「中止」
            labels = ["中止"]
            self.out("此階段仍在進行中：只能標記「中止」；滿充／滿放／未滿請待階段結束後再標記。")
        else:
            labels = ["滿充" if row["dir"] == "C" else "滿放", "未滿", "中止"]
        for i, w in enumerate(labels, 1):
            self.out(f"  {i}. {w}")
        self.out("  0. 返回")
        v = self.ask(f"{row['name']} 標記（0＝返回）：", "0")
        if v not in [str(i) for i in range(1, len(labels) + 1)]:
            self.out("已返回，未送出請求。" if v == "0" else f"沒有選項「{v}」，未送出請求。")
            return None
        res = self.submit("stage_label", {"stage_start": row["start"], "label": labels[int(v) - 1]}, cert_id=cid)
        self.out("（標記只影響報告標題與頁籤名稱；已產生的報告不會自動更新，請用「25 重新產生認證報告」）")
        return res

    # ---- 24 補拍 --------------------------------------------------------------
    def shot(self):
        cid = REQ.active_on_disk(self.root)
        if not cid:
            self.out("目前沒有進行中的認證（手動補拍只在記錄中可用）。")
            return None
        return self.submit("manual_shot", {"reason": self.ask("原因（Enter＝操作人員要求立即截圖）：")},
                           cert_id=cid)

    # ---- 25 重產 --------------------------------------------------------------
    def regenerate(self):
        cid = self.pick_cert()
        return self.submit("regenerate", cert_id=cid) if cid else None

    # ---- 26 狀態 --------------------------------------------------------------
    def status(self):
        alive, why = self.service_alive()
        self.out(f"背景服務：{why}")
        st = REQ.read_status(self.root) or {}
        cid = REQ.active_on_disk(self.root)
        if not cid:
            self.out("目前沒有進行中的認證。")
        else:
            ti = st.get("test_info") or {}
            self.out(f"認證 {cid}｜開始 {st.get('started_at')}｜取樣 {st.get('sample_count')} 筆｜"
                     f"最後取樣 {st.get('last_sample_time')}（{'成功' if st.get('last_sample_ok') else '讀取失敗或無值'}）｜"
                     f"目前 {st.get('last_direction', '未知')}")
            self.out(f"  電櫃規格 {ti.get('cabinet_spec') or '（未設定）'}｜循環數 {ti.get('cycles') or '（不填）'}｜"
                     f"服務中斷 {st.get('interruptions', 0)} 次")
            for s in st.get("stages") or []:
                self.out(f"  階段 {s['no']}. {s['name']}｜開始 {s['start']}｜{s['end_by']}｜標記 {s.get('label') or '-'}")
            for u in st.get("uncertain") or []:
                self.out(f"  不確定 {u['item_id']}｜已確認 {u.get('decided') or '尚未'}")
        return st

    # ---- 27 查詢 --------------------------------------------------------------
    def query(self):
        rows = REQ.recent_requests(self.root, 10)
        for req, stt, res in rows:
            self.out(f"  {req['request_id']}｜{REQ.TYPES.get(req['type'], req['type'])}｜"
                     f"{REQ.STATUS_TEXT.get(stt, stt)}" + (f"｜{res.get('message')}" if res else ""))
        rid = self.ask("輸入請求 ID 查看完整結果（Enter 略過）：")
        if rid:
            stt, res = REQ.request_state(self.root, rid)
            if res:
                self.show_result(res)
            else:
                self.out(f"請求 {rid}：{REQ.STATUS_TEXT.get(stt, stt)}")
        return rows

    def handle(self, choice):
        return {"21": self.start, "22": self.end, "23": self.mark, "24": self.shot,
                "25": self.regenerate, "26": self.status, "27": self.query}[choice]()


def status_line(root=None):
    """主選單顯示用的一行摘要（唯讀）。"""
    if not enabled():
        return None
    root = root or REQ.cert_root()
    cid = REQ.active_on_disk(root)
    if not cid:
        return None
    st = REQ.read_status(root) or {}
    alive, why = UI(root).service_alive()
    return (f"【認證測試｜記錄中】{cid}｜取樣 {st.get('sample_count', '-')} 筆｜"
            f"最後 {st.get('last_sample_time', '-')}｜目前 {st.get('last_direction', '未知')}"
            + ("" if alive else f"｜⚠ {why}"))
