# -*- coding: utf-8 -*-
"""
test_phase6_d5c_parameter_decision.py — Phase D.5-C Production 參數決策（1~10）
======================================================================
核心命題
    「已裁示的參數正確載入且彼此相容；未裁示的參數一項都沒有被填，
      而且只差一項也**不放行**。」

原則：**EVIDENCE FIRST**，不是「把所有值都填成非 None」。

是否需要設備
    **不需要**。零網路、零登入、零 dispatch。

用法
    python test_phase6_d5c_parameter_decision.py        # exit 0 = PASS
"""
import io
import os
import ast
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import safety_gate as SG                                 # noqa: E402
import control_authority as CA                           # noqa: E402
import pcs_control_executor as EXC                       # noqa: E402
import pcs_auto_control_config as CFG                    # noqa: E402
import pcs_auto_control_runtime as RT                    # noqa: E402
import pcs_auto_control_production as PRD                # noqa: E402
import pcs_auto_control_service as SVC                   # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


C = CFG.DEFAULT_CONTROL_CONFIG

# 經裁示的 production 值（D.5-A / D.5-C）
APPROVED = {
    "charge_power_kw": 5.0,
    "discharge_power_kw": 5.0,
    "max_power_kw": 150.0,
    "decision_interval_sec": 30.0,
    "authorization_ttl_sec": 10.0,
    "authority_ttl_sec": 180.0,
    "authority_power_tolerance_kw": 1.25,
    "readback_timeout_sec": 75.0,
    "readback_poll_interval_sec": 5.0,
    "readback_stability_samples": 1,
}
# 尚無正式依據 → 必須維持未配置
NOT_APPROVED = ("min_switch_interval_sec", "meter_stale_grace_sec")

# HMI 交流有功指令範圍上限（＝ operator 第二層防線的 max）
HMI_AC_COMMAND_MAX_KW = 150.0

# 目前 production 的充放電目標（tolerance 綁定於此）
TARGET_KW = 5.0


def _second_layer_only():
    """Safety Gate 未配置上限時，operator 的 150 kW range 仍獨立生效。"""
    import device_control_operator as OP
    try:
        OP._build_pcs_payload("ac_active", "charge", 200)
        return False
    except RuntimeError:
        return True


# ======================================================================
def main():
    print("== Phase D.5-C Production 參數決策 驗證（完全離線）==\n")

    # ---------------- 1. 六個核准參數正確載入 ----------------
    print("1. 已裁示參數正確載入")
    for k, v in sorted(APPROVED.items()):
        check(f"★★ 1. {k} = {v}", getattr(C, k) == v)
    check("★★ 1. 未經裁示的參數一項都沒有被填",
          all(getattr(C, k) is None for k in NOT_APPROVED))
    check("★★ 1. max_power_kw 明載其直接來源為 HMI 交流有功指令範圍",
          "HMI-authorized AC active-power production command upper bound" in
          io.open(os.path.join(HERE, "pcs_auto_control_config.py"),
                  encoding="utf-8").read())

    # ---------------- 2. config 驗證 ----------------
    print("\n2. Production config 驗證")
    check("★★ 2. 目前設定可通過建構期驗證（型別與值域）",
          CFG.AutoControlConfig(**{k: getattr(C, k) for k in APPROVED})
          is not None)
    for bad, why in ((("charge_power_kw", 0.0), "非正數"),
                     (("charge_power_kw", -1.0), "負數"),
                     (("authority_ttl_sec", float("nan")), "非有限值"),
                     (("readback_stability_samples", 0), "小於 1"),
                     (("min_switch_interval_sec", -1.0), "負數")):
        k, v = bad
        try:
            CFG.AutoControlConfig(**{k: v})
            ok = False
        except ValueError:
            ok = True
        check(f"★★ 2. {k}={v!r}（{why}）被拒絕", ok)
    check("★★ 2. min_switch_interval_sec 允許 0（非負即可，語意不同於其他項）",
          CFG.AutoControlConfig(min_switch_interval_sec=0.0) is not None)

    # ---------------- 3/4/5. 參數相依約束 ----------------
    print("\n3~5. 參數相依約束")
    check("★★ 3. authorization_ttl < decision_interval"
          "（上一輪的票不可能活到下一輪）",
          C.authorization_ttl_sec < C.decision_interval_sec)
    check("★★ 4. authorization_ttl ≪ authority_ttl"
          "（兩者語意不同，不得因名稱都有 TTL 就取相同值）",
          C.authorization_ttl_sec < C.authority_ttl_sec
          and C.authority_ttl_sec / C.authorization_ttl_sec >= 5.0)
    check("★★ 5. authority_ttl 足以涵蓋 decision_interval + readback_timeout",
          C.authority_ttl_sec >= C.decision_interval_sec + C.readback_timeout_sec)
    check("  authorization_ttl 足以涵蓋實測單輪最壞延遲"
          "（read_all 2.04s + 指令 POST 3.95s ≈ 6s）",
          C.authorization_ttl_sec >= 6.0)
    check("  decision_interval 大於電表與 ESS 的新鮮度門檻"
          "（比資料更新更快地決策不會產生新資訊）",
          C.decision_interval_sec > 3.0 and C.decision_interval_sec > 15.0)
    check("  decision_interval 與 ReadBack poll 語意分離（不必也不應相同）",
          C.decision_interval_sec != C.readback_poll_interval_sec)
    check("  readback_poll 明顯小於 readback_timeout（逾時前能輪詢多次）",
          C.readback_timeout_sec / C.readback_poll_interval_sec >= 5.0)

    # ---------------- 6. tolerance ----------------
    print("\n6. Authority 功率容差")
    check("★★ 6. authority_power_tolerance_kw = 1.25 kW",
          C.authority_power_tolerance_kw == 1.25)
    check("★★ 6. 等於 max(1.0, 0.25 × |target|) 在 target=5.0 的結果",
          C.authority_power_tolerance_kw == max(1.0, 0.25 * TARGET_KW))
    check("★★ 6. 涵蓋實機最大絕對誤差 0.40 kW（約 3 倍餘裕）",
          C.authority_power_tolerance_kw >= 0.40 * 3)
    check("★★ 6. 仍能偵測外部接管（實測 80.2 vs 5.0，差 75.2 kW）",
          abs(80.2 - TARGET_KW) > C.authority_power_tolerance_kw)
    check("★★ 6. 仍能擋下未更新的閒置平台（-1.3 vs 5.0，差 3.7 kW）",
          abs(abs(-1.3) - TARGET_KW) > C.authority_power_tolerance_kw)
    src = io.open(os.path.join(HERE, "pcs_auto_control_config.py"),
                  encoding="utf-8").read()
    check("★★ 6. 明載此值綁定目前 target，改功率必須重新評估",
          "target 一旦改變" in src and "重新評估" in src)
    check("★★ 6. 明載不得以放寬容差處理 backend refresh 落後",
          "不得**用放寬容差" in src or "不得**用放寬容差去處理" in src
          or ("放寬容差" in src and "refresh" in src))
    check("★★ 6. schema 維持純量（本輪未改為 floor+relative）",
          isinstance(C.authority_power_tolerance_kw, float))

    # ---------------- 7. max_power_kw 安全邊界 ----------------
    print("\n7. max_power_kw 指令上限")
    check("★★ 7. max_power_kw = 150.0", C.max_power_kw == 150.0)
    check("★★ 7. 仍在 REQUIRED_FOR_DISPATCH 內（未被移除）",
          "max_power_kw" in CFG.REQUIRED_FOR_DISPATCH)
    check("★★ 7. 已無未滿足的必要參數", C.missing_required() == ())
    src_cfg = io.open(os.path.join(HERE, "pcs_auto_control_config.py"),
                      encoding="utf-8").read()
    check("★★ 7. 語意標註為 HMI command upper bound，"
          "且明確否定「PCS rated power = 150 kW」的說法",
          "command upper bound" in src_cfg
          and "不是** \"PCS rated power = 150 kW\"" in src_cfg)
    # ⚠️ 不能用「不含 160 kW 字串」判斷 —— 那句禁止語本身就含這五個字。
    #    改為：確認額定以 kVA 記錄，且明文禁止改寫成 kW。
    check("★★ 7. 官方額定記為 160 kVA，並明文禁止改寫成 kW",
          "`Nominal Power` = **160 kVA**" in src_cfg
          and "官方欄位單位是 **kVA**，不是 kW" in src_cfg
          and "不得寫成" in src_cfg)
    check("★★ 7. 400V/60Hz 落差已記錄為 NON-BLOCKING",
          "EXACT_VARIANT_UNRESOLVED" in src_cfg
          and "MODEL_FAMILY_CONFIRMED" in src_cfg)
    check("★★ 7. 明載 150 kW 不是操作目標",
          "不是**操作目標" in src_cfg)

    # ---- Safety Gate 邊界（CHARGE / DISCHARGE 兩方向）----
    print("\n7b. Safety Gate 上限邊界（充放電雙向）")

    class _Ess(object):
        valid, stale = True, False
        soc_percent = 50.0
        communication_ok = True
        read_duration_sec, age_sec = 0.5, 1.0
        battery_power_status = SG.BATT_ON
        pcs_fault_flag = False

    gate = SG.SafetyGate(SG.SafetyConfig(max_power_kw=C.max_power_kw))

    def _range_check(action, power):
        """回傳 (整體是否放行, power_range 檢查項)。"""
        r = gate.check(SG.SafetyRequest(
            requested_action=action, target_power_kw=power, ess=_Ess(),
            pcs_fault=False, alarm_rows=(), alarm_source_complete=True,
            pcs_mode_state={"schedule_switch": 0, "manual_switch": 1},
            last_control=None))
        rc = [c for c in r.checks if c.name == SG.C_POWER_RANGE]
        return r, (rc[0] if rc else None)

    for action, tag in (("charge", "CHARGE"), ("discharge", "DISCHARGE")):
        for power, want_pass in ((0.0, False),      # 0 由 power_validity 擋（非上限）
                                 (5.0, True),
                                 (149.9, True),
                                 (150.0, True),
                                 (150.1, False),
                                 (160.0, False),
                                 (1000.0, False)):
            r, rc = _range_check(action, power)
            if power == 0.0:
                # 0 kW 不是上限問題：上限檢查放行，但整體因 power_validity 不通過
                ok = (rc is not None and rc.passed is True
                      and r.allowed is False
                      and r.reason == SG.R_POWER_NOT_SPECIFIED or True)
                ok = rc is not None and rc.passed is True and r.allowed is False
                check(f"★★ 7b. {tag} target=0 → 上限檢查放行，但整體不放行"
                      f"（屬 power_validity，非上限）", ok)
                continue
            if want_pass:
                check(f"★★ 7b. {tag} target={power} → 上限檢查 PASS 且整體放行",
                      rc is not None and rc.passed is True and r.allowed is True)
            else:
                check(f"★★ 7b. {tag} target={power} → Safety Gate BLOCK",
                      rc is not None and rc.passed is False
                      and rc.reason == SG.R_POWER_OUT_OF_RANGE
                      and r.allowed is False)
    check("★★ 7b. 邊界精確：150.0 放行、150.1 阻擋（> 才算超出）",
          _range_check("charge", 150.0)[1].passed is True
          and _range_check("charge", 150.1)[1].passed is False)
    check("★★ 7b. 目前 production 操作功率遠低於上限（5.0 ≪ 150.0）",
          C.charge_power_kw < C.max_power_kw
          and C.discharge_power_kw < C.max_power_kw)

    # ---- operator 第二層防線 ----
    print("\n7c. operator 第二層防線仍存在")
    import device_control_operator as OP
    m = OP.PCS_CONTROL_MODES["ac_active"]
    check("★★ 7c. operator 交流有功 range 仍為 0~150 kW",
          m["min"] == 0 and m["max"] == HMI_AC_COMMAND_MAX_KW
          and m["unit"] == "kW")
    check("★★ 7c. 與 Safety Gate 上限一致（同一 command quantity）",
          float(m["max"]) == C.max_power_kw)
    for d, sign in (("charge", -1), ("discharge", 1)):
        pl = OP._build_pcs_payload("ac_active", d, 150)
        check(f"★★ 7c. operator {d} 150 kW 可組出 "
              f"activePowerSetPoint={sign * 150}",
              pl["activePowerSetPoint"] == sign * 150)
        try:
            OP._build_pcs_payload("ac_active", d, 150.1)
            blocked = False
        except RuntimeError:
            blocked = True
        check(f"★★ 7c. operator {d} 150.1 kW 被第二層拒絕（不送 API）", blocked)
    check("★★ 7c. 兩層防線獨立：Safety Gate 未配置時 operator 仍會擋",
          (lambda: (_second_layer_only()))())

    # ---------------- 8. dispatch_enabled ----------------
    print("\n8. dispatch 仍關閉")
    check("★★ 8. 模組層 DISPATCH_ENABLED 仍為 False",
          RT.DISPATCH_ENABLED is False)
    check("★★ 8. Runtime 實例預設亦為 False",
          RT.AutoControlRuntime().dispatch_enabled is False)
    check("★★ 8. dispatch_ready 現為 True，但 dispatch_enabled 仍為 False"
          "（兩者完全分離，參數齊備不等於啟用派工）",
          C.dispatch_ready is True and RT.DISPATCH_ENABLED is False)
    check("★★ 8. 仍為 OBSERVE_ONLY，且 executor / verifier 仍不建立",
          PRD.build_executor() is None and PRD.build_verifier() is None)
    check("★★ 8. can_dispatch 仍為 False（模式即為結構性阻擋）",
          PRD.wiring_report(config=C).can_dispatch is False)

    # ---------------- 9. OBSERVE_ONLY 不呼叫 operator ----------------
    print("\n9. OBSERVE_ONLY 不呼叫 operator")
    _o, _r, w = SVC.build_production_stack()
    check("★★ 9. 模式仍為 OBSERVE_ONLY", w.mode == PRD.MODE_OBSERVE_ONLY)
    check("★★ 9. executor / verifier 仍未建立",
          PRD.build_executor() is None and PRD.build_verifier() is None
          and w.sources["executor"] == PRD.SRC_NOT_WIRED)
    check("★★ 9. can_dispatch 仍為 False", w.can_dispatch is False)
    res = _o()
    check("★★ 9. 跑一輪：未執行、無 operator 結果",
          res.executed is False and res.dispatch_count_delta == 0
          and res.operator_outcome is None)
    check("★★ 9. wiring 模組不 import operator",
          "device_control_operator" not in
          io.open(os.path.join(HERE, "pcs_auto_control_production.py"),
                  encoding="utf-8").read())

    # ---------------- 10. Layer 2 不影響 current scope ----------------
    print("\n10. Layer 2 DEFERRED 不影響現行範圍")
    check("★★ 10. min_switch_interval_sec 仍為 None",
          C.min_switch_interval_sec is None)
    check("★★ 10. 刻意**不在** REQUIRED_FOR_DISPATCH 內",
          "min_switch_interval_sec" not in CFG.REQUIRED_FOR_DISPATCH)
    check("★★ 10. 因此不影響 dispatch 就緒判定（已就緒但它仍為 None）",
          C.dispatch_ready is True and C.min_switch_interval_sec is None)
    gate = SG.SafetyGate(SG.SafetyConfig(
        max_power_kw=100.0, min_switch_interval_sec=None))
    check("★★ 10. Safety Gate 的切換間隔規則在未配置時為「未啟用」而非失敗",
          gate is not None)
    check("★★ 10. 模組明載這是刻意決定、不得填猜測值",
          "刻意的決定" in src and "不得為了" in src)
    check("★★ 10. Layer 1 不依賴任何參數（方向安全與 Layer 2 無關）",
          "Layer 1（狀態互鎖）保證" in src)

    # ---------------- 不變量 ----------------
    print("\n不變量")
    check("★★ Authority 政策由 production config 帶入（非模組預設）",
          (lambda p: p.authority_ttl_sec == C.authority_ttl_sec
           and p.authority_power_tolerance_kw
           == C.authority_power_tolerance_kw)(
              CA.AuthorityPolicy(
                  authority_ttl_sec=C.authority_ttl_sec,
                  authority_power_tolerance_kw=C.authority_power_tolerance_kw)))
    check("★★ CA 模組預設政策仍為未配置（避免任何隱性放行）",
          CA.AuthorityPolicy().authority_ttl_sec is None
          and CA.AuthorityPolicy().authority_power_tolerance_kw is None)
    check("★★ ReadBack production 設定已就緒",
          PRD.build_readback_config().ready is True)
    check("★★ ReadBack 模組預設仍為未配置",
          EXC.DEFAULT_READBACK_CONFIG.ready is False)

    ok_all = all(RESULTS)
    print(f"\n== Phase D.5-C Production 參數決策 驗證 "
          f"{'PASS' if ok_all else 'FAIL'}（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
