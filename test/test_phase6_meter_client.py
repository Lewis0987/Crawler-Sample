# -*- coding: utf-8 -*-
"""
Phase 6.1 Meter Client 驗證 —— 完全離線
======================================================================
用途
    驗證 meter_client.py 的契約驗證、Grid Meter 方向語意、Fail Closed 與
    staleness 判定。重點不是「能不能連上 6160」，而是「**連不上、或收到壞資料時
    會不會正確判定為無效**」—— 這是 Phase 6.4 Safety Gate 的地基。

是否需要設備
    **不需要**。完全離線：不連 6160、不連 HMI、不連電表、不開任何 socket。
    Socket.IO 事件以直接呼叫 _handle_update() 模擬，時間以 get_snapshot(now=...) 注入。

涵蓋範圍
    A. 契約驗證      缺欄位 / 型別錯 / bool 偽裝數值 / NaN / 未知狀態字彙 / 非 dict
    B. 方向語意      IMPORT / EXPORT / NEUTRAL / UNKNOWN，且永不產生 CHARGE / DISCHARGE
    C. Fail Closed   meter_state != ok、demand_state == fault、stale、NO_DATA
    D. 邊界          age 恰為 3.0s 不算 stale；3.01s 算
    E. 語意隔離      snapshot 不得洩漏 bess_* / soc_* / wharf
    F. 客戶端行為    壞資料不得覆蓋上一筆好資料；斷線後 age 自然增長

用法
    python test_phase6_meter_client.py          # exit 0 = PASS
"""
import os
import sys
import json
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import meter_client as MC                              # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


# ---------- 樣本 payload（取自 Phase 6.0 實測，未做任何修改）----------
def prod_payload(**over):
    """production 18 欄位 payload（2026-08-19 15:22:26 實測樣本）。"""
    p = {
        "bess_0": 0.0, "bess_0_state": "offline",
        "bess_1": 0.0, "bess_1_state": "ok",
        "bess_sum": 0.0, "bess_sum_state": "ok",
        "datetime": "2026-08-19 15:22:26", "timestamp": "15:22:26",
        "demand": 92.0761875, "demand_state": "degraded",
        "meter": 92.0761875, "meter_state": "ok",
        "soc_0": None, "soc_1": 2, "soc_avg": 2.0,
        "status": "BESS_0_OFFLINE",
        "wharf": 0.0, "wharf_state": "ok",
    }
    p.update(over)
    return p


def shadow_payload():
    """本機稽核實例 8 欄位 payload（缺 meter_state / demand_state）。"""
    return {
        "timestamp": "15:22:28", "meter": 92.0761875,
        "bess_0": 0.0, "bess_1": 0.0, "bess_sum": 0.0,
        "soc_0": 0, "soc_1": 2, "demand": 92.0761875,
    }


def main():
    print("== Phase 6.1 Meter Client 驗證（完全離線）==\n")

    # ---------------- A. 契約驗證 ----------------
    print("A. 契約驗證")
    ok, reason, _ = MC.validate_payload(prod_payload())
    check("production 實測 payload 通過契約", ok and reason == MC.R_OK)

    ok, reason, detail = MC.validate_payload(shadow_payload())
    check("shadow 8 欄位被拒（缺 meter_state/demand_state）",
          (not ok) and reason == MC.R_MISSING
          and "meter_state" in detail and "demand_state" in detail)

    ok, reason, _ = MC.validate_payload({"meter": 1.0, "meter_state": "ok",
                                         "demand": 1.0})
    check("缺 demand_state → INVALID_SCHEMA_MISSING",
          (not ok) and reason == MC.R_MISSING)

    ok, reason, _ = MC.validate_payload(prod_payload(meter="92.07"))
    check("meter 為字串 → INVALID_SCHEMA_TYPE", (not ok) and reason == MC.R_TYPE)

    ok, reason, _ = MC.validate_payload(prod_payload(meter=True))
    check("meter 為 bool → INVALID_SCHEMA_TYPE（bool 不得被當成 1 kW）",
          (not ok) and reason == MC.R_TYPE)

    ok, reason, _ = MC.validate_payload(prod_payload(meter_state=None))
    check("meter_state 為 None → INVALID_SCHEMA_TYPE",
          (not ok) and reason == MC.R_TYPE)

    ok, reason, _ = MC.validate_payload(prod_payload(meter=float("nan")))
    check("meter 為 NaN → INVALID_VALUE_NOT_FINITE",
          (not ok) and reason == MC.R_NOT_FINITE)

    ok, reason, _ = MC.validate_payload(prod_payload(demand=float("inf")))
    check("demand 為 inf → INVALID_VALUE_NOT_FINITE",
          (not ok) and reason == MC.R_NOT_FINITE)

    ok, reason, _ = MC.validate_payload(prod_payload(meter_state="weird"))
    check("meter_state 未知字彙 → INVALID_STATE_VALUE（production 改版即察覺）",
          (not ok) and reason == MC.R_STATE_VOCAB)

    ok, reason, _ = MC.validate_payload(prod_payload(demand_state="offline"))
    check("demand_state='offline' 不在字彙內 → INVALID_STATE_VALUE",
          (not ok) and reason == MC.R_STATE_VOCAB)

    ok, reason, _ = MC.validate_payload(["not", "a", "dict"])
    check("payload 非 dict → INVALID_PAYLOAD_NOT_MAPPING",
          (not ok) and reason == MC.R_NOT_MAPPING)

    ok, _, _ = MC.validate_payload(prod_payload(future_field_v2=123))
    check("多出未知欄位仍通過（向前相容）", ok)

    # ---------------- B. 方向語意 ----------------
    print("\nB. Grid Meter 方向語意")
    check("meter=+92.08 → IMPORT", MC.classify_direction(92.0761875) == MC.DIR_IMPORT)
    check("meter=-243.41 → EXPORT", MC.classify_direction(-243.41119384765625) == MC.DIR_EXPORT)
    check("meter=0.0 → NEUTRAL", MC.classify_direction(0.0) == MC.DIR_NEUTRAL)
    check("meter=None → UNKNOWN", MC.classify_direction(None) == MC.DIR_UNKNOWN)
    check("meter=NaN → UNKNOWN", MC.classify_direction(float("nan")) == MC.DIR_UNKNOWN)
    check("meter=True(bool) → UNKNOWN", MC.classify_direction(True) == MC.DIR_UNKNOWN)
    dirs = {MC.classify_direction(v) for v in (92.0, -92.0, 0.0, None, float("nan"))}
    check("方向值域不含 CHARGE / DISCHARGE（三種 kW 語意隔離）",
          dirs.isdisjoint({"CHARGE", "DISCHARGE"})
          and dirs <= {MC.DIR_IMPORT, MC.DIR_EXPORT, MC.DIR_NEUTRAL, MC.DIR_UNKNOWN})

    # ---------------- C. Fail Closed ----------------
    print("\nC. Fail Closed")
    s = MC.evaluate(prod_payload(), received_at=100.0, now=100.5)
    check("正常且新鮮 → valid=True", s.valid and s.reason == MC.R_OK)
    check("正常樣本 direction=IMPORT 且 power_kw 正確",
          s.direction == MC.DIR_IMPORT and abs(s.power_kw - 92.0761875) < 1e-9)

    s = MC.evaluate(prod_payload(meter_state="offline"), 100.0, 100.5)
    check("meter_state=offline → invalid INVALID_METER_STATE",
          (not s.valid) and s.reason == MC.R_METER_STATE)

    s = MC.evaluate(prod_payload(meter_state="fault"), 100.0, 100.5)
    check("meter_state=fault → invalid INVALID_METER_STATE",
          (not s.valid) and s.reason == MC.R_METER_STATE)

    s = MC.evaluate(prod_payload(demand_state="fault"), 100.0, 100.5)
    check("demand_state=fault → invalid INVALID_DEMAND_STATE_FAULT",
          (not s.valid) and s.reason == MC.R_DEMAND_FAULT)

    s = MC.evaluate(prod_payload(demand_state="degraded"), 100.0, 100.5)
    check("demand_state=degraded → 仍 valid（production 官方語意：結果仍可信）", s.valid)

    s = MC.evaluate(shadow_payload(), 100.0, 100.5)
    check("shadow payload → invalid 且不外洩數值（power_kw is None）",
          (not s.valid) and s.reason == MC.R_MISSING and s.power_kw is None
          and s.direction == MC.DIR_UNKNOWN)

    s = MC.no_data_snapshot()
    check("從未收到資料 → NO_DATA / valid=False / age=inf / stale=True",
          (not s.valid) and s.reason == MC.R_NO_DATA
          and math.isinf(s.age_sec) and s.stale)

    # ---------------- D. Staleness 邊界 ----------------
    print("\nD. Staleness 邊界（門檻 3.0s，以本機 monotonic 計算）")
    s = MC.evaluate(prod_payload(), 100.0, 102.9)
    check("age=2.90s → 未過期、valid", s.valid and (not s.stale))
    s = MC.evaluate(prod_payload(), 100.0, 103.0)
    check("age=3.00s（等於門檻，不算超過）→ valid", s.valid and (not s.stale))
    s = MC.evaluate(prod_payload(), 100.0, 103.01)
    check("age=3.01s → stale 且 invalid（STALE）",
          s.stale and (not s.valid) and s.reason == MC.R_STALE)
    s = MC.evaluate(prod_payload(), 100.0, 130.0)
    check("age=30s（超過 Socket.IO pingTimeout 20s）→ 早已 invalid，不必等 disconnect",
          s.stale and (not s.valid))
    s = MC.evaluate(prod_payload(), 100.0, 102.0)
    check("stale 判定不使用 production timestamp（server_timestamp 僅記錄）",
          s.server_timestamp == "15:22:26" and s.valid)

    # ---------------- E. 語意隔離 ----------------
    print("\nE. 語意隔離（禁止洩漏非控制欄位）")
    d = MC.evaluate(prod_payload(), 100.0, 100.5).as_dict()
    leaked = sorted(set(d.keys()) & MC.NON_CONTROL_FIELDS)
    check(f"snapshot 不含 bess_*/soc_*/wharf（洩漏={leaked}）", not leaked)
    check("snapshot 欄位皆帶語意（有 direction 才有 power_kw）",
          "direction" in d and "power_kw" in d)
    check("snapshot 保留本機接收時間欄位 received_at / age_sec / stale / valid",
          all(k in d for k in ("received_at", "age_sec", "stale", "valid")))

    # ---------------- E2. CLI 顯示與 6160 網頁 UI 對照 ----------------
    print("\nE2. CLI 顯示標籤對照 6160 網頁 UI（僅顯示層）")
    line = str(MC.evaluate(prod_payload(), 100.0, 100.5))
    # 樣本 meter=demand=92.0761875 → 網頁 toFixed(2) 顯示 92.08（node 實測）
    check(f"顯示使用 UI 欄位名「{MC.UI_LABEL_METER}」（= payload.meter）且為 2 位",
          f"{MC.UI_LABEL_METER}=+92.08 kW" in line)
    check(f"顯示使用 UI 欄位名「{MC.UI_LABEL_DEMAND}」（= payload.demand）且為 2 位",
          f"{MC.UI_LABEL_DEMAND}=+92.08 kW(degraded)" in line)
    check("顯示保留 IMPORT / meter_state / VALID",
          "IMPORT" in line and "meter_state=ok" in line and line.endswith("VALID"))
    check("不再出現舊標籤 power= / demand=",
          "power=" not in line and "demand=" not in line)
    check("內部欄位名未被改動（power_kw / demand_kw 仍在，且無中文 key）",
          "power_kw" in d and "demand_kw" in d
          and all(k.isascii() for k in d))
    check("JSON 模式欄位名不受顯示標籤影響",
          MC.UI_LABEL_METER not in str(d) and MC.UI_LABEL_DEMAND not in str(d))

    # ---------------- E3. 2 位小數捨入規則對齊 toFixed(2) ----------------
    # 期望值來源：node v24.14.1 實際執行 Number(x).toFixed(2)，非規格推論。
    # 前 6 筆為一般值；後 5 筆是精確二進位中點，正是 Python 預設 .2f（ties-to-even）
    # 與 JS toFixed（ties-away-from-zero）會分歧之處。
    print("\nE3. CLI 兩位小數捨入規則（對齊 6160 網頁 toFixed(2)）")
    TOFIXED2 = [
        (109.554, "+109.55"), (109.555, "+109.56"), (109.556, "+109.56"),
        (-109.554, "-109.55"), (-109.555, "-109.56"), (-109.556, "-109.56"),
        (109.125, "+109.13"), (109.625, "+109.63"), (-109.125, "-109.13"),
        (0.125, "+0.13"), (-0.125, "-0.13"),
    ]
    bad = [(v, exp, MC._fmt2(v)) for v, exp in TOFIXED2 if MC._fmt2(v) != exp]
    for v, exp, got in bad:
        print(f"       {v!r}: 期望 {exp}，實得 {got}")
    check(f"11 個 toFixed(2) 對照值全數相符（不符={len(bad)}）", not bad)

    ties = [(109.125, "+109.13"), (109.625, "+109.63"), (-109.125, "-109.13"),
            (0.125, "+0.13"), (-0.125, "-0.13")]
    check("精確二進位中點採 ties-away-from-zero（非 Python 預設的 ties-to-even）",
          all(MC._fmt2(v) == exp for v, exp in ties)
          and all(MC._fmt2(v) != f"{v:+.2f}" for v, _ in ties))
    check("_fmt2 對非有限值回 n/a（不拋例外）",
          MC._fmt2(float("inf")) == "n/a" and MC._fmt2(float("nan")) == "n/a"
          and MC._fmt2(None) == "n/a")

    # 顯示層不得改變內部精度
    s3 = MC.evaluate(prod_payload(meter=100.306469, demand=100.306469), 100.0, 100.5)
    check("CLI 顯示為 2 位（+100.31）",
          f"{MC.UI_LABEL_METER}=+100.31 kW" in str(s3)
          and f"{MC.UI_LABEL_DEMAND}=+100.31 kW" in str(s3))
    check("內部 power_kw / demand_kw 仍為完整原始精度（100.306469）",
          s3.power_kw == 100.306469 and s3.demand_kw == 100.306469)

    # ---------------- E4. JSON 序列化：不得出現 Infinity / NaN ----------------
    print("\nE4. JSON 序列化防護（標準 JSON 不得有 Infinity / NaN）")
    nd = MC.no_data_snapshot()
    raw = nd.as_dict()
    check("as_dict() 保留原樣（received_at=-inf、age_sec=inf）",
          math.isinf(raw["received_at"]) and math.isinf(raw["age_sec"]))
    jd = nd.as_json_dict()
    check("as_json_dict() 將 received_at / age_sec 轉為 None",
          jd["received_at"] is None and jd["age_sec"] is None)
    txt = json.dumps(jd, ensure_ascii=False, allow_nan=False)
    check("NO_DATA 可用 allow_nan=False 序列化（不拋 ValueError）", isinstance(txt, str))
    check("輸出不含 Infinity / -Infinity / NaN 字樣",
          "Infinity" not in txt and "NaN" not in txt)
    check("輸出可被嚴格 JSON parser 解析回來",
          json.loads(txt)["reason"] == MC.R_NO_DATA)
    check("valid / stale / reason 原邏輯不受影響",
          jd["valid"] is False and jd["stale"] is True and jd["reason"] == MC.R_NO_DATA)
    check("human-readable CLI 仍顯示 age=n/a", "age=n/a" in str(nd))

    vj = MC.evaluate(prod_payload(meter=100.306469, demand=100.306469),
                     100.0, 100.5).as_json_dict()
    vtxt = json.dumps(vj, ensure_ascii=False, allow_nan=False)
    check("valid 資料的 JSON 仍保留原始 float 精度（未被截成 2 位）",
          json.loads(vtxt)["power_kw"] == 100.306469
          and json.loads(vtxt)["demand_kw"] == 100.306469)
    check("valid 資料 JSON 亦無 Infinity / NaN",
          "Infinity" not in vtxt and "NaN" not in vtxt)

    # ---------------- F. 客戶端行為（不開 socket）----------------
    print("\nF. MeterClient 行為（不建立任何連線）")
    c = MC.MeterClient(url="http://127.0.0.1:1/never-connected")
    check("未連線時 get_snapshot() → NO_DATA", c.get_snapshot().reason == MC.R_NO_DATA)

    c._handle_update(prod_payload())
    snap = c.get_snapshot()
    check("餵入有效 payload → accepted 且 snapshot valid",
          snap.valid and c.get_stats()["accepted"] == 1)

    good_at = c._received_at
    c._handle_update(prod_payload(meter=999.0, meter_state="weird"))
    snap = c.get_snapshot()
    st = c.get_stats()
    check("壞 payload 被拒且**不覆蓋**上一筆好資料",
          st["rejected"] == 1 and c._received_at == good_at
          and abs(snap.power_kw - 92.0761875) < 1e-9)
    check("拒收原因有計數", st["reject_reasons"].get(MC.R_STATE_VOCAB) == 1)

    stale_snap = c.get_snapshot(now=good_at + 5.0)
    check("模擬斷線 5 秒後（無新資料）→ 自動轉 STALE 並 invalid",
          stale_snap.stale and (not stale_snap.valid)
          and stale_snap.reason == MC.R_STALE)

    check("斷線後仍保留最後數值供診斷（但 valid=False）",
          abs(stale_snap.power_kw - 92.0761875) < 1e-9 and not stale_snap.valid)

    c._handle_update(shadow_payload())
    check("shadow schema 進來也被拒（rejected 累計 2）",
          c.get_stats()["rejected"] == 2)

    # 「完全收不到」與「收得到但契約破壞」是兩種嚴重度，不可都報成 NO_DATA
    c2 = MC.MeterClient(url="http://127.0.0.1:1/never-connected")
    check("全新 client 尚未收到任何東西 → NO_DATA",
          c2.get_snapshot().reason == MC.R_NO_DATA)
    for _ in range(3):
        c2._handle_update(shadow_payload())
    s2 = c2.get_snapshot()
    check("只收到契約不符的資料 → 回報 INVALID_SCHEMA_MISSING 而非 NO_DATA",
          s2.reason == MC.R_MISSING and (not s2.valid))
    check("拒收詳情含缺少欄位與筆數（可辨識 production 改版）",
          "meter_state" in s2.detail and "3" in s2.detail)
    check("契約破壞時仍不外洩數值", s2.power_kw is None and s2.direction == MC.DIR_UNKNOWN)
    check("age 為 inf 時字串顯示為 n/a（不出現 'infs'）",
          "age=n/a" in str(s2) and "infs" not in str(s2))

    # ---------------- 總結 ----------------
    ok_all = all(RESULTS)
    print(f"\n== Phase 6.1 Meter Client 驗證 {'PASS' if ok_all else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
