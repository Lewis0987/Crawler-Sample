# -*- coding: utf-8 -*-
"""
test_phase6_610b17_network_identity.py — Phase 6.10-B1.7 Network Identity 三態
======================================================================
核心命題
    「『目前可部署』與『DHCP Reservation 已 field verified』是兩件事，
      而且必須能同時成立：
          Phase 6.10-B1.7           = ACCEPTED FOR CURRENT DEPLOYMENT
          NETWORK_IDENTITY_STABILITY = ACCEPTED / USER CONFIRMED
          DHCP Reservation           = NOT VERIFIED / NON-BLOCKING
      ACCEPTED 可以放行 live gate，但**永遠不得**被印成、對映成或
      推論成 PASS / FIELD_VERIFIED。」

是否需要設備
    **不需要**。本檔不連線、不送任何遠端動詞、不碰 guard。

用法
    python test_phase6_610b17_network_identity.py        # exit 0 = PASS
"""
import io
import os
import re
import sys
import ast

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_handoff_orchestrator as HO         # noqa: E402
import phase6_remote_adapter as RA               # noqa: E402
import phase6_unattended_service as SVC          # noqa: E402

RESULTS = []


def check(label, ok):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return ok


def probe(**over):
    p = {"screen_alive": True, "process_alive": True,
         "process_identity_ok": True, "controller_running": True}
    p.update(over)
    return p


def gate(state):
    """以指定的 network identity 狀態評估 live gate。"""
    orig = SVC.NETWORK_IDENTITY_STABILITY
    try:
        SVC.NETWORK_IDENTITY_STABILITY = state
        return SVC.live_handoff_allowed(HO.MODE_ARMED, HO.R_CLEAN, True,
                                        probe(), health=SVC.HEALTH_OK)[1]
    finally:
        SVC.NETWORK_IDENTITY_STABILITY = orig


# ======================================================================
# 1/2/3. 三態 → gate 放行與否
# ======================================================================
def test_1_2_3_gate():
    print("\n[1/2/3] 三態 → live gate")
    check("★★ NET_PASS → gate allowed",
          gate(SVC.NET_PASS)["network_identity_acceptable"] is True)
    check("★★ NET_ACCEPTED → gate allowed",
          gate(SVC.NET_ACCEPTED)["network_identity_acceptable"] is True)
    check("★★ NET_NOT_VERIFIED → gate blocked",
          gate(SVC.NET_NOT_VERIFIED)["network_identity_acceptable"] is False)
    for bogus in ("", None, "pass", "Accepted", "TRUE", "OK"):
        check(f"  未知值 {bogus!r} → gate blocked（Fail Closed）",
              SVC.network_identity_acceptable(bogus) is False)
    check("  三態詞彙恰好三個且互不相同",
          len(SVC.NETWORK_IDENTITY_STATES) == 3
          and len(set(SVC.NETWORK_IDENTITY_STATES)) == 3)
    check("  可放行集合恰為 {PASS, ACCEPTED}",
          SVC.NETWORK_IDENTITY_ACCEPTABLE == {SVC.NET_PASS, SVC.NET_ACCEPTED})


# ======================================================================
# 4/5/10. ACCEPTED 不得被當成 PASS / FIELD_VERIFIED
# ======================================================================
def test_4_5_10_no_alias():
    print("\n[4/5/10] ACCEPTED != PASS / FIELD_VERIFIED")
    check("★★ NET_ACCEPTED 與 NET_PASS 是不同字串（不得 alias）",
          SVC.NET_ACCEPTED != SVC.NET_PASS
          and SVC.NET_ACCEPTED == "ACCEPTED" and SVC.NET_PASS == "PASS")
    check("★★ 目前狀態是 ACCEPTED，**不是** PASS",
          SVC.NETWORK_IDENTITY_STABILITY == SVC.NET_ACCEPTED
          and SVC.NETWORK_IDENTITY_STABILITY != SVC.NET_PASS)
    check("★★ 佐證為 USER_CONFIRMED，**不是** FIELD_VERIFIED",
          SVC.NETWORK_IDENTITY_EVIDENCE == SVC.NET_EVIDENCE_USER_CONFIRMED
          and SVC.NETWORK_IDENTITY_EVIDENCE
          != SVC.NET_EVIDENCE_FIELD_VERIFIED)

    v, rows, blocked = SVC.live_readiness_report()
    got = {r["item"]: r["value"] for r in rows}
    check(f"★★ readiness 印出 NETWORK_IDENTITY_STABILITY = "
          f"{got['NETWORK_IDENTITY_STABILITY']}（不是 PASS）",
          got["NETWORK_IDENTITY_STABILITY"] == "ACCEPTED")
    check(f"★★ readiness 印出 NETWORK_IDENTITY_EVIDENCE = "
          f"{got['NETWORK_IDENTITY_EVIDENCE']}",
          got["NETWORK_IDENTITY_EVIDENCE"] == "USER_CONFIRMED")

    txt = SVC.format_readiness(v, rows, blocked)
    line = [l for l in txt.splitlines()
            if "NETWORK_IDENTITY_STABILITY" in l][0]
    # 只看「值」欄 —— 註解欄說明「field verified 需要什麼」是合理的
    value_col = line.split("NETWORK_IDENTITY_STABILITY")[1].split("#")[0]
    check(f"★★ 值欄為 ACCEPTED 而非 PASS：{value_col.strip()}",
          value_col.strip() == "ACCEPTED" and "PASS" not in value_col)
    check("★★ 也不含 FIELD_VERIFIED",
          "FIELD_VERIFIED" not in
          [l for l in txt.splitlines()
           if "NETWORK_IDENTITY_EVIDENCE" in l][0])
    # 該列以 INFO 標示，不會被誤讀成 OK
    check("★★ 未 field verified 的列標為 INFO，不是 OK",
          line.strip().startswith("INFO"))

    # 原始碼層級：不得出現把常數設為 NET_PASS 的寫法
    src = io.open(os.path.join(HERE, "phase6_unattended_service.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    assigned = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name)
                        and t.id == "NETWORK_IDENTITY_STABILITY"
                        for t in n.targets)):
            assigned = n.value
    check("★★ AST：NETWORK_IDENTITY_STABILITY 指定為具名的 NET_ACCEPTED",
          isinstance(assigned, ast.Name) and assigned.id == "NET_ACCEPTED")


# ======================================================================
# 6. DHCP Reservation 仍為 NOT_VERIFIED
# ======================================================================
def test_6_dhcp():
    print("\n[6] DHCP Reservation 仍未驗證")
    check("★★ DHCP_RESERVATION_VERIFIED is False",
          SVC.DHCP_RESERVATION_VERIFIED is False)
    check("★★ 狀態字串為 NOT_VERIFIED",
          SVC.DHCP_RESERVATION_STATUS == "NOT_VERIFIED")
    check("  註記為 NON_BLOCKING_BY_USER_DECISION（如實記錄成因）",
          SVC.DHCP_RESERVATION_NOTE == "NON_BLOCKING_BY_USER_DECISION")
    _, rows, _ = SVC.live_readiness_report()
    got = {r["item"]: r for r in rows}
    check("★★ readiness 的 DHCP_RESERVATION 列印 NOT_VERIFIED",
          got["DHCP_RESERVATION"]["value"] == "NOT_VERIFIED")
    check("★★ 該列的 ok 為 False（沒有假裝已驗證）",
          got["DHCP_RESERVATION"]["ok"] is False)
    check("  但不參與阻塞（使用者裁示為 non-blocking）",
          got["DHCP_RESERVATION"]["gating"] is False)
    check("★★ 三句話同時成立：ACCEPTED / USER_CONFIRMED / DHCP NOT_VERIFIED",
          SVC.NETWORK_IDENTITY_STABILITY == SVC.NET_ACCEPTED
          and SVC.NETWORK_IDENTITY_EVIDENCE
          == SVC.NET_EVIDENCE_USER_CONFIRMED
          and SVC.DHCP_RESERVATION_VERIFIED is False)


# ======================================================================
# 7/8. network identity 不再單項阻塞；其他 blocker 照常
# ======================================================================
def test_7_8_blockers():
    print("\n[7/8] blocker 清單")
    v, rows, blocked = SVC.live_readiness_report()
    check("  整體仍為 BLOCKED", v == SVC.BLOCKED)
    check("★★ network identity 三列已不在 blocked",
          not ({"NETWORK_IDENTITY_STABILITY", "NETWORK_IDENTITY_EVIDENCE",
                "DHCP_RESERVATION"} & set(blocked)))
    check("★★ NETWORK_IDENTITY_GATE 為 ALLOWED 且不阻塞",
          [r for r in rows if r["item"] == "NETWORK_IDENTITY_GATE"][0]["ok"]
          is True)
    # 2026-09-07 B2 部署後 REMOTE_GUARD_VARIANT 已通過，不再列於 blocked
    for k in ("REMOTE_CAPABILITY_REPORT",
              "PAUSE_CAPABILITY", "RESTORE_CAPABILITY",
              "PAUSE_TIMEOUT_VERIFIED", "RESTORE_TIMEOUT_VERIFIED",
              "RECOVERY_CLEAN", "DISPATCH_ENABLED", "MODE",
              "FIRST_LIVE_PREREQUISITE"):
        check(f"  其他 blocker 照常存在：{k}", k in blocked)
    check("  REMOTE_GUARD_VARIANT 已通過（B2 已部署）",
          "REMOTE_GUARD_VARIANT" not in blocked)
    check(f"★★ blocked 共 {len(blocked)} 項", len(blocked) == 9)
    check("★★ 未驗證項仍被如實揭露，沒有被隱藏",
          SVC.readiness_unverified(rows)
          == ["NETWORK_IDENTITY_STABILITY", "NETWORK_IDENTITY_EVIDENCE",
              "DHCP_RESERVATION"])
    txt = SVC.format_readiness(v, rows, blocked)
    check("  格式化輸出同時列出 not-field-verified 與 blocked",
          "not field verified (non-blocking, 3)" in txt
          and "blocked reasons (9)" in txt)


# ======================================================================
# 9. Regression trigger
# ======================================================================
def test_9_triggers():
    print("\n[9] 重新驗證觸發條件")
    want = {"ipv4_changed", "nic_changed", "route_changed",
            "dhcp_policy_changed", "wifi_source_changed", "ssh_from_mismatch"}
    check(f"  觸發條件共 6 項：{sorted(SVC.NETWORK_IDENTITY_REGRESSION_TRIGGERS)}",
          set(SVC.NETWORK_IDENTITY_REGRESSION_TRIGGERS) == want)
    check("  無觸發 → 維持 ACCEPTED",
          SVC.network_identity_after_triggers([])
          == (SVC.NET_ACCEPTED, []))
    for t in sorted(want):
        st, fired = SVC.network_identity_after_triggers([t])
        check(f"★★ {t} → 退回 NOT_VERIFIED（不得維持 ACCEPTED）",
              st == SVC.NET_NOT_VERIFIED and st != SVC.NET_ACCEPTED
              and fired == [t])
    st, fired = SVC.network_identity_after_triggers(
        ["ipv4_changed", "ssh_from_mismatch"])
    check("  多項同時觸發 → 仍 NOT_VERIFIED 且完整列出",
          st == SVC.NET_NOT_VERIFIED and len(fired) == 2)
    try:
        SVC.network_identity_after_triggers(["typo_trigger"])
        ok = False
    except ValueError:
        ok = True
    check("★★ 未知觸發名稱必須拒絕（打錯字不得變成沒檢查）", ok)
    check("★★ 退回後 gate 立刻擋下",
          SVC.network_identity_acceptable(
              SVC.network_identity_after_triggers(["route_changed"])[0])
          is False)

    # baseline 供未來比對
    b = SVC.NETWORK_IDENTITY_BASELINE
    check(f"  baseline 已記錄：{b['ipv4']} / {b['dhcp_server']} / {b['mac']}",
          b["ipv4"] == "192.168.128.234"
          and b["dhcp_server"] == "192.168.128.16"
          and b["mac"] == "04-EC-D8-6A-46-4A"
          and b["ssh_from"] == "192.168.128.234")


# ======================================================================
# 11/12. guard 行為未變、dispatch 未啟用
# ======================================================================
def test_11_12_untouched():
    print("\n[11/12] guard 與 dispatch 未受影響")
    check("★★ DEPLOYED_GUARD_VARIANT = B2（2026-09-07 已部署並唯讀驗證）",
          RA.DEPLOYED_GUARD_VARIANT == "B2")
    check("  B1 allowlist 常數未變（三個唯讀動詞，供回滾比對）",
          RA.GUARD_B1_ALLOWED_VERBS == ("probe", "loopcheck", "status"))
    check("  B1 定義仍不接受 pause / restore（回滾後的預期行為）",
          not RA.guard_b1_would_accept("pause")
          and not RA.guard_b1_would_accept("restore"))
    check("  B2 allowlist 未變（五個動詞）",
          RA.GUARD_B2_ALLOWED_VERBS
          == ("probe", "loopcheck", "status", "pause", "restore"))
    check("★★ DISPATCH_ENABLED 仍為 False", HO.DISPATCH_ENABLED is False)
    check("★★ RemoteSenders 預設 armed = False",
          RA.RemoteSenders().armed is False)
    check("  live gate 仍 11 項", len(SVC.LIVE_GATE_ITEMS) == 11)
    check("  CRITICAL_CONDITIONS 仍 4 項",
          len(SVC.CRITICAL_CONDITIONS) == 4)
    check("★★ 即使 network identity 已放行，live handoff 仍 REFUSED",
          SVC.live_handoff_allowed(HO.MODE_ARMED, HO.R_CLEAN, True, probe(),
                                   health=SVC.HEALTH_OK)[0] is False)
    check("  pause / restore SSH timeout 仍為 None（未受本輪影響）",
          RA.SshTimeouts().command_timeout_for("pause") is None
          and RA.SshTimeouts().command_timeout_for("restore") is None)


# ======================================================================
def main():
    for fn in (test_1_2_3_gate, test_4_5_10_no_alias, test_6_dhcp,
               test_7_8_blockers, test_9_triggers, test_11_12_untouched):
        fn()
    n, tot = sum(RESULTS), len(RESULTS)
    print("\n" + "=" * 72)
    print(f"  結果：{n}/{tot} {'PASS' if n == tot else 'FAIL'}")
    print("=" * 72)
    return 0 if n == tot else 1


if __name__ == "__main__":
    sys.exit(main())
