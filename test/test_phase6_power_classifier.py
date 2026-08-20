# -*- coding: utf-8 -*-
"""
Phase 6.2 Power Classification 驗證 —— 完全離線
======================================================================
用途
    驗證 MeterSnapshot → GridPowerState 的遲滯分類、防抖、以及 UNKNOWN 立即生效。
    重點在「門檻附近不亂跳」「短暫反向不切換」「資料不可信時立刻退回 UNKNOWN」。

是否需要設備
    **不需要**。完全離線：不連 6160、不連 HMI、不開任何 socket。
    MeterSnapshot 一律由 Phase 6.1 的 meter_client.evaluate() 產生（composition，
    不重寫 6.1 邏輯），時間由 update(now=...) 注入，不依賴真實時鐘。

涵蓋範圍
    A. 基本分類     正/負/零/正near-zero/負near-zero
    B. Fail Closed  valid=False / stale / power=None / 非 finite → UNKNOWN
    C. Hysteresis   IMPORT、EXPORT 門檻附近來回；與 NEAR_ZERO 互轉
    D. Debounce     單筆反向、短暫反向、持續成立、候選改變重計時、候選取消
    E. UNKNOWN      立即生效；恢復需經 debounce；遲滯記憶重置
    F. 語意隔離     不得出現 CHARGE / DISCHARGE / PEAK / OFF_PEAK

用法
    python test_phase6_power_classifier.py          # exit 0 = PASS
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dataclasses                                     # noqa: E402
import meter_client as MC                              # noqa: E402
import power_classifier as PC                          # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def mk(power, rec=100.0, now=100.0, demand=None, **over):
    """用 Phase 6.1 的正式路徑產生 MeterSnapshot（不繞過契約驗證）。"""
    p = {"meter": power, "meter_state": "ok", "timestamp": "12:00:00",
         "demand": power if demand is None else demand, "demand_state": "degraded"}
    p.update(over)
    return MC.evaluate(p, rec, now)


def drive(clf, power, t, **kw):
    """在時刻 t 餵一筆新鮮 snapshot。"""
    return clf.update(mk(power, rec=t, now=t, **kw), now=t)


def settle(clf, power, t0, cfg=PC.DEFAULT_CONFIG):
    """把某狀態餵到 debounce 完成，回傳 (state, 完成時刻)。"""
    t = t0
    for _ in range(200):
        st = drive(clf, power, t)
        if st.reason != PC.R_DEBOUNCING:
            return st, t
        t += 0.5
    raise AssertionError("settle 未收斂")


H = PC.classify_with_hysteresis
CFG = PC.DEFAULT_CONFIG


def main():
    print("== Phase 6.2 Power Classification 驗證（完全離線）==\n")
    print(f"門檻：IMPORT enter>={CFG.import_enter_kw} exit<={CFG.import_exit_kw}；"
          f"EXPORT enter<={CFG.export_enter_kw} exit>={CFG.export_exit_kw}；"
          f"debounce={CFG.debounce_sec}s（皆為暫定值）\n")

    # ---------------- A. 基本分類 ----------------
    print("A. 基本分類（無先前狀態，須跨過 ENTER 門檻）")
    check("正常正功率 +80 → IMPORT", H(80.0, None) == PC.STATE_IMPORT)
    check("正常負功率 -80 → EXPORT", H(-80.0, None) == PC.STATE_EXPORT)
    check("0 kW → NEAR_ZERO", H(0.0, None) == PC.STATE_NEAR_ZERO)
    check("正 near-zero +3（<enter 5）→ NEAR_ZERO", H(3.0, None) == PC.STATE_NEAR_ZERO)
    check("負 near-zero -3（>enter -5）→ NEAR_ZERO", H(-3.0, None) == PC.STATE_NEAR_ZERO)
    check("恰好 +5.0（=enter）→ IMPORT", H(5.0, None) == PC.STATE_IMPORT)
    check("恰好 -5.0（=enter）→ EXPORT", H(-5.0, None) == PC.STATE_EXPORT)
    check("非有限值 → UNKNOWN",
          H(float("nan"), None) == PC.STATE_UNKNOWN
          and H(float("inf"), None) == PC.STATE_UNKNOWN
          and H(None, None) == PC.STATE_UNKNOWN)

    # ---------------- B. Fail Closed ----------------
    print("\nB. Validity Gate → UNKNOWN（不得繞過 Phase 6.1 Fail Closed）")
    clf = PC.PowerClassifier()
    check("初始（未餵資料）→ UNKNOWN / NO_DATA",
          clf.current().state == PC.STATE_UNKNOWN
          and clf.current().reason == PC.R_NO_DATA and not clf.current().valid)

    s = drive(PC.PowerClassifier(), 80.0, 0.0, meter_state="fault")
    check("meter_state=fault（valid=False）→ UNKNOWN / METER_INVALID",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_INVALID)

    s = drive(PC.PowerClassifier(), 80.0, 0.0, demand_state="fault")
    check("demand_state=fault → UNKNOWN / METER_INVALID",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_INVALID)

    c = PC.PowerClassifier()
    s = c.update(mk(80.0, rec=0.0, now=10.0), now=10.0)     # age=10s > 3s
    check("stale=True → UNKNOWN / METER_STALE",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_STALE)

    good = mk(80.0, rec=0.0, now=0.0)
    s = PC.PowerClassifier().update(dataclasses.replace(good, power_kw=None), now=0.0)
    check("power_kw=None（即使 valid=True）→ UNKNOWN / POWER_NOT_FINITE",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_POWER_NOT_FINITE)

    s = PC.PowerClassifier().update(
        dataclasses.replace(good, power_kw=float("nan")), now=0.0)
    check("power_kw=NaN（即使 valid=True）→ UNKNOWN / POWER_NOT_FINITE",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_POWER_NOT_FINITE)

    s = PC.PowerClassifier().update(MC.no_data_snapshot(), now=0.0)
    check("NO_DATA snapshot → UNKNOWN / NO_DATA",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_NO_DATA)

    # 契約破壞（shadow 實例的 8 欄位 payload，缺 meter_state / demand_state）
    # —— 本機 shadow 服務已停止，改由離線覆蓋此路徑
    shadow = {"timestamp": "15:22:28", "meter": 92.07, "demand": 92.07,
              "bess_0": 0.0, "bess_1": 0.0, "bess_sum": 0.0, "soc_0": 0, "soc_1": 2}
    ss = MC.evaluate(shadow, 0.0, 0.0)
    s = PC.PowerClassifier().update(ss, now=0.0)
    check("shadow 契約破壞（缺 state 欄位）→ UNKNOWN / METER_INVALID",
          (not ss.valid) and ss.reason == MC.R_MISSING
          and s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_INVALID)

    cshadow = PC.PowerClassifier()
    settle(cshadow, 80.0, 0.0)
    s = cshadow.update(MC.evaluate(shadow, 30.0, 30.0), now=30.0)
    check("已 IMPORT 時突然收到契約破壞資料 → 立即 UNKNOWN（不保留舊狀態）",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_INVALID
          and s.source_reason == MC.R_MISSING)

    # ---------------- C. Hysteresis ----------------
    print("\nC. Hysteresis（進入與離開用不同門檻）")
    check("已 IMPORT，+3.5 仍維持 IMPORT（>exit 2）", H(3.5, PC.STATE_IMPORT) == PC.STATE_IMPORT)
    check("未 IMPORT，+3.5 維持 NEAR_ZERO（<enter 5）",
          H(3.5, PC.STATE_NEAR_ZERO) == PC.STATE_NEAR_ZERO)
    check("★ 同一數值 +3.5 因歷史不同而結果不同 → 遲滯確實生效",
          H(3.5, PC.STATE_IMPORT) != H(3.5, PC.STATE_NEAR_ZERO))
    check("IMPORT → NEAR_ZERO：+1.5（<=exit 2）", H(1.5, PC.STATE_IMPORT) == PC.STATE_NEAR_ZERO)
    check("恰好 +2.0（=exit，不算高於）→ 離開 IMPORT", H(2.0, PC.STATE_IMPORT) == PC.STATE_NEAR_ZERO)
    check("NEAR_ZERO → IMPORT 需 >=5：+4.9 不切換", H(4.9, PC.STATE_NEAR_ZERO) == PC.STATE_NEAR_ZERO)

    check("已 EXPORT，-3.5 仍維持 EXPORT（<exit -2）", H(-3.5, PC.STATE_EXPORT) == PC.STATE_EXPORT)
    check("EXPORT → NEAR_ZERO：-1.5（>=exit -2）", H(-1.5, PC.STATE_EXPORT) == PC.STATE_NEAR_ZERO)
    check("恰好 -2.0（=exit）→ 離開 EXPORT", H(-2.0, PC.STATE_EXPORT) == PC.STATE_NEAR_ZERO)
    check("NEAR_ZERO → EXPORT 需 <=-5：-4.9 不切換",
          H(-4.9, PC.STATE_NEAR_ZERO) == PC.STATE_NEAR_ZERO)
    check("IMPORT 直接跌到 -80 → EXPORT（不卡在 NEAR_ZERO）",
          H(-80.0, PC.STATE_IMPORT) == PC.STATE_EXPORT)

    # 門檻附近連續來回：raw 不應改變
    prev = PC.STATE_IMPORT
    seq = [3.0, 4.0, 2.5, 4.5, 3.2, 4.8]
    outs = []
    for v in seq:
        prev = H(v, prev)
        outs.append(prev)
    check(f"IMPORT 門檻附近來回 {seq} → 全程維持 IMPORT",
          all(o == PC.STATE_IMPORT for o in outs))

    prev = PC.STATE_EXPORT
    outs = []
    for v in [-3.0, -4.0, -2.5, -4.5, -3.2]:
        prev = H(v, prev)
        outs.append(prev)
    check("EXPORT 門檻附近來回 → 全程維持 EXPORT",
          all(o == PC.STATE_EXPORT for o in outs))

    # ---------------- D. Debounce ----------------
    print("\nD. Debounce（候選需連續維持 3.0s 才切換）")
    clf = PC.PowerClassifier()
    s0 = drive(clf, 80.0, 0.0)
    check("首筆 IMPORT：stable 仍 UNKNOWN，候選 IMPORT，reason=DEBOUNCING",
          s0.state == PC.STATE_UNKNOWN and s0.candidate_state == PC.STATE_IMPORT
          and s0.reason == PC.R_DEBOUNCING)
    s = drive(clf, 80.0, 2.9)
    check("2.9s 未達門檻 → 仍 UNKNOWN", s.state == PC.STATE_UNKNOWN)
    s = drive(clf, 80.0, 3.0)
    check("3.0s 達門檻 → 切換 IMPORT，候選清空",
          s.state == PC.STATE_IMPORT and s.candidate_state is None
          and s.reason == PC.R_OK and s.valid)

    s = drive(clf, -80.0, 3.5)
    check("單筆反向 EXPORT → stable 仍 IMPORT（候選 EXPORT 計時中）",
          s.state == PC.STATE_IMPORT and s.candidate_state == PC.STATE_EXPORT
          and s.reason == PC.R_DEBOUNCING)
    s = drive(clf, 80.0, 4.0)
    check("反向後恢復 → 候選取消，stable 維持 IMPORT",
          s.state == PC.STATE_IMPORT and s.candidate_state is None and s.reason == PC.R_OK)

    for t in (5.0, 5.5, 6.0, 6.5, 7.0, 7.5):
        s = drive(clf, -80.0, t)
    check("EXPORT 持續 2.5s 仍未切換", s.state == PC.STATE_IMPORT)
    s = drive(clf, -80.0, 8.0)
    check("EXPORT 持續滿 3.0s → 切換 EXPORT", s.state == PC.STATE_EXPORT)

    # 候選中途改變 → 重新計時
    clf2 = PC.PowerClassifier()
    settle(clf2, 80.0, 0.0)                       # stable = IMPORT
    s = drive(clf2, -80.0, 20.0)                  # 候選 EXPORT since 20.0
    check("候選 EXPORT 起算", s.candidate_state == PC.STATE_EXPORT
          and abs(s.candidate_since - 20.0) < 1e-9)
    s = drive(clf2, 0.0, 21.0)                    # raw 變 NEAR_ZERO → 候選改變
    check("候選中途改變（EXPORT→NEAR_ZERO）→ 重新計時",
          s.candidate_state == PC.STATE_NEAR_ZERO
          and abs(s.candidate_since - 21.0) < 1e-9 and s.candidate_count == 1)
    s = drive(clf2, 0.0, 23.5)
    check("重新計時後 2.5s 仍未切換（未沿用舊候選時間）", s.state == PC.STATE_IMPORT)
    s = drive(clf2, 0.0, 24.0)
    check("重新計時滿 3.0s → 切換 NEAR_ZERO", s.state == PC.STATE_NEAR_ZERO)

    check("candidate_count 有累計", s0.candidate_count == 1)

    # ---------------- E. UNKNOWN ----------------
    print("\nE. UNKNOWN 立即生效（Fail Closed 優先於狀態穩定性）")
    clf3 = PC.PowerClassifier()
    settle(clf3, 80.0, 0.0)
    check("前置：stable = IMPORT", clf3.current().state == PC.STATE_IMPORT)
    s = drive(clf3, 80.0, 30.0, meter_state="fault")
    check("IMPORT → invalid → **同一筆立即** UNKNOWN（不等 debounce）",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_INVALID
          and s.candidate_state is None)

    clf4 = PC.PowerClassifier()
    settle(clf4, -80.0, 0.0)
    check("前置：stable = EXPORT", clf4.current().state == PC.STATE_EXPORT)
    s = clf4.update(mk(-80.0, rec=30.0, now=40.0), now=40.0)   # age 10s
    check("EXPORT → stale → **同一筆立即** UNKNOWN",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_STALE)

    # 恢復必須重走 debounce
    clf5 = PC.PowerClassifier()
    settle(clf5, 80.0, 0.0)
    drive(clf5, 80.0, 30.0, meter_state="fault")               # → UNKNOWN
    s = drive(clf5, 80.0, 31.0)
    check("UNKNOWN → valid IMPORT：第一筆不得立即恢復（需 debounce）",
          s.state == PC.STATE_UNKNOWN and s.candidate_state == PC.STATE_IMPORT)
    s = drive(clf5, 80.0, 34.0)
    check("UNKNOWN → valid IMPORT：滿 3.0s 後才 IMPORT", s.state == PC.STATE_IMPORT)

    clf6 = PC.PowerClassifier()
    drive(clf6, 80.0, 0.0, meter_state="fault")
    s, _ = settle(clf6, -80.0, 1.0)
    check("UNKNOWN → valid EXPORT：經 debounce 後 EXPORT", s.state == PC.STATE_EXPORT)

    # 中斷後遲滯記憶必須重置
    clf7 = PC.PowerClassifier()
    settle(clf7, 80.0, 0.0)                        # IMPORT，_last_raw=IMPORT
    drive(clf7, 80.0, 30.0, meter_state="fault")   # UNKNOWN，記憶重置
    s = drive(clf7, 3.5, 31.0)
    check("中斷後遲滯記憶重置：+3.5 以 ENTER 門檻判定為 NEAR_ZERO（非沿用 IMPORT）",
          s.raw_state == PC.STATE_NEAR_ZERO)

    # 只在資料到達時更新會漏掉斷線 —— 驗證週期性 tick 能轉 UNKNOWN
    clf8 = PC.PowerClassifier()
    settle(clf8, 80.0, 0.0)
    last = mk(80.0, rec=10.0, now=10.0)
    s = clf8.update(MC.evaluate({"meter": 80.0, "meter_state": "ok", "demand": 80.0,
                                 "demand_state": "degraded", "timestamp": "12:00:00"},
                                10.0, 14.0), now=14.0)
    check("同一筆資料放到 age=4s 再 tick → UNKNOWN（斷線可被偵測）",
          s.state == PC.STATE_UNKNOWN and s.reason == PC.R_METER_STALE and last.valid)

    # ---------------- F. 語意隔離 ----------------
    print("\nF. 語意隔離")
    clf9 = PC.PowerClassifier()
    states, texts = set(), []
    t = 0.0
    for v in (80.0, 80.0, 80.0, 80.0, 80.0, 80.0, 80.0,
              3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0,
              -80.0, -80.0, -80.0, -80.0, -80.0, -80.0, -80.0, 0.0):
        st = drive(clf9, v, t)
        states.add(st.state)
        states.add(st.raw_state)
        if st.candidate_state:
            states.add(st.candidate_state)
        texts.append(str(st))
        t += 0.5
    check(f"整段序列出現的狀態皆在合法值域內：{sorted(states)}",
          states <= PC.VALID_STATES)
    blob = " ".join(texts)
    hit = sorted(w for w in PC.FORBIDDEN_STATES if w in blob)
    check(f"輸出不含 CHARGE / DISCHARGE / PEAK / OFF_PEAK 等字彙（命中={hit}）", not hit)
    check("值域與禁用字彙無交集", PC.VALID_STATES.isdisjoint(PC.FORBIDDEN_STATES))

    d = clf9.current().as_dict()
    leaked = sorted(set(d.keys()) & MC.NON_CONTROL_FIELDS)
    check(f"GridPowerState 不含 bess_*/soc_*/wharf（洩漏={leaked}）", not leaked)
    check("GridPowerState 具備診斷欄位（state/raw/candidate/since/reason）",
          all(k in d for k in ("state", "raw_state", "candidate_state",
                               "stable_since", "candidate_since", "reason", "valid")))

    # demand 不得影響方向分類
    a = PC.PowerClassifier()
    b = PC.PowerClassifier()
    sa, _ = settle(a, 80.0, 0.0)
    tb = 0.0
    for _ in range(20):
        sb = b.update(mk(80.0, rec=tb, now=tb, demand=-9999.0), now=tb)
        if sb.reason != PC.R_DEBOUNCING:
            break
        tb += 0.5
    check("demand_kw 天差地遠也不影響 IMPORT/EXPORT/NEAR_ZERO 判定",
          sa.state == sb.state == PC.STATE_IMPORT)
    check("demand_kw 仍保留於輸出供診斷", sb.demand_kw == -9999.0)

    # ---------------- G. 設定防呆與 JSON ----------------
    print("\nG. 門檻設定防呆與序列化")
    for kw, label in (({"import_enter_kw": 1.0, "import_exit_kw": 2.0}, "import enter<=exit"),
                      ({"export_enter_kw": -1.0, "export_exit_kw": -2.0}, "export enter>=exit"),
                      ({"import_exit_kw": -1.0}, "import_exit<=0"),
                      ({"debounce_sec": -1.0}, "debounce 為負")):
        try:
            PC.ClassifierConfig(**kw)
            ok = False
        except ValueError:
            ok = True
        check(f"錯誤門檻設定被擋下（{label}）", ok)

    import json as _json
    txt = _json.dumps(clf9.current().as_json_dict(), ensure_ascii=False, allow_nan=False)
    check("GridPowerState 可用 allow_nan=False 序列化且無 Infinity/NaN",
          "Infinity" not in txt and "NaN" not in txt
          and _json.loads(txt)["state"] in PC.VALID_STATES)

    # ---------------- 總結 ----------------
    ok_all = all(RESULTS)
    print(f"\n== Phase 6.2 Power Classification 驗證 {'PASS' if ok_all else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
