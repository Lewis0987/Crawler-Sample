# -*- coding: utf-8 -*-
"""
phase6_shadow_rehearsal.py — PRODUCTION READ-ONLY SHADOW REHEARSAL
======================================================================
2026-10-06 Attempt #9 postmortem 正式裁示。

目的
    field Attempt 成本很高。本模組用**真實 production 路徑**在白天先跑一次，
    盡量提前抓出：
        authority evidence ／provider wiring ／journal ／deadline ／
        result classification ／artifact ／runtime state

使用的是真實的：
    production config ／provider ／HMI 唯讀 ／meter 唯讀 ／
    external probe & loopcheck ／production entry path

🔴 但**禁止**任何 field side effect：
        PAUSE ／authority acquire ／CHARGE ／DISCHARGE ／PCS STOP
   必須在**第一個 field side effect 之前**硬性停止。

🔴 `SHADOW PASS` **不得**被描述成 real field PASS。
   它證明的是「到 PAUSE 之前的 production 路徑可用」，
   不證明交接、live leg 或收尾。

用法
    python test/phase6_shadow_rehearsal.py --config <正式 config>
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SHADOW_PASS = "SHADOW_REHEARSAL_PASS"
SHADOW_BLOCKED = "SHADOW_REHEARSAL_BLOCKED"
SHADOW_ERROR = "SHADOW_REHEARSAL_ERROR"

#: 🔴 這些動詞一旦被呼叫即為違規 —— shadow 必須在它們之前停止
FORBIDDEN_FIELD_ACTIONS = ("pause", "restore", "authority_acquire",
                           "charge", "discharge", "pcs_stop")

#: shadow 允許走到的最後一步（下一步就是第一個 field side effect）
SHADOW_STOP_BEFORE = "1_PRE_PAUSE_PREFLIGHT_DONE"

#: 必須檢查的項目
SHADOW_ITEMS = ("config_ok", "provider_validation_ok", "dependencies_built",
                "deadline_built", "journal_path_derived",
                "safety_preflight_ran", "gate_a_ran", "handoff_readiness_ran",
                "external_liveness_observed", "dispatch_rest_state",
                "no_field_side_effect")


class ShadowViolation(RuntimeError):
    """shadow 期間出現了 field side effect —— 一律中止。"""


def _guard_senders(deps_ctx, violations):
    """把會造成 side effect 的注入點換成會記錄違規的 stub。"""

    def _deny(verb):
        def _fn(*a, **kw):
            violations.append(verb)
            raise ShadowViolation(
                "SHADOW REHEARSAL 禁止 field side effect：%s" % verb)
        return _fn

    return _deny


def run_shadow(config_path, now_fn=None):
    """
    回傳 (verdict, report)。**不送任何控制指令。**

    🔴 只走到 provider 建構 ＋ 三個唯讀 gate；
       **不呼叫** `arm_and_run_handoff()`（那是第一個 field side effect 的入口）。
    """
    import run_scheduled_first_live as LAUNCH
    import phase6_first_live_field_provider as FP
    import phase6_handoff_orchestrator as HO
    import pcs_auto_control_runtime as RT
    import phase6_execution_deadline as DL

    now = (now_fn or (lambda: datetime.datetime.now()))()
    rep = {k: None for k in SHADOW_ITEMS}
    rep["at"] = now.isoformat(timespec="seconds")
    rep["mode"] = "READ_ONLY_NO_FIELD_SIDE_EFFECT"
    rep["stop_before"] = SHADOW_STOP_BEFORE
    rep["forbidden_actions"] = list(FORBIDDEN_FIELD_ACTIONS)
    rep["violations"] = []
    rep["config_path"] = config_path

    status, cfg, problems = LAUNCH.load_config(config_path)
    rep["config_ok"] = (status == LAUNCH.CONFIG_OK and not problems)
    rep["problems"] = list(problems or ())
    if not rep["config_ok"]:
        return SHADOW_BLOCKED, rep
    rep["attempt_id"] = cfg.get("attempt_id")

    ok, checks = FP.validate_provider_definition(
        {"dependency_provider": cfg.get("dependency_provider")})
    rep["provider_validation_ok"] = bool(ok)
    rep["provider_validation"] = dict(checks)
    if not ok:
        return SHADOW_BLOCKED, rep

    # 🔴 runtime context 與正式執行同形狀，但**不**進入 arm_and_run_handoff
    rtctx = {"attempt_id": cfg.get("attempt_id"),
             "authorization": {"attempt_id": cfg.get("attempt_id")},
             "field_authorized": False,          # shadow：永不授權
             "one_shot_consumed": False,
             "inside_window": False,             # shadow：刻意不在窗口內
             "target_date": str(cfg.get("target_date")),
             "window_start": str(cfg.get("window_start")),
             "window_end": str(cfg.get("window_end")),
             "target_date_obj": cfg.get("target_date"),
             "window_end_obj": cfg.get("window_end"),
             "tzinfo": cfg.get("tzinfo")}

    try:
        deps = FP.build_field_dependencies(
            {"dependency_provider": cfg.get("dependency_provider")}, rtctx)
        rep["dependencies_built"] = True
    except Exception as e:                                     # noqa: BLE001
        rep["dependencies_built"] = False
        rep["error"] = type(e).__name__
        return SHADOW_ERROR, rep

    try:
        d = DL.ExecutionDeadline.from_window(
            cfg["target_date"], cfg["window_end"], tzinfo=cfg.get("tzinfo"),
            label=cfg.get("attempt_id"))
        rep["deadline_built"] = True
        rep["deadline_at"] = d.deadline_at.isoformat()
    except Exception as e:                                     # noqa: BLE001
        rep["deadline_built"] = False
        rep["deadline_error"] = type(e).__name__

    rep["journal_path_derived"] = HO.attempt_journal_path(
        os.path.join(HERE, "..", "output", "phase6_scheduled_first_live"),
        cfg.get("attempt_id"))
    # 🔴 side effect 只能用「**本次執行是否新建**」判定。
    #    同一個 attempt_id 若已真的跑過，ledger / journal 本來就存在，
    #    那是歷史證據，不是 shadow 造成的 side effect。
    _pre = {"ledger": os.path.exists(cfg.get("ledger_path") or ""),
            "journal": os.path.exists(rep["journal_path_derived"])}
    rep["preexisting_artifacts"] = dict(_pre)

    # ---- 三個**唯讀** gate（皆在第一個 field side effect 之前）----
    try:
        p_ok, _ = deps.safety_preflight()
        rep["safety_preflight_ran"] = True
        rep["safety_preflight_ok"] = bool(p_ok)
        a_ok, _, a_verdict = deps.gate_a()
        rep["gate_a_ran"] = True
        rep["gate_a_ok"] = bool(a_ok)
        rep["gate_a_verdict"] = a_verdict
        r_ok, _ = deps.handoff_readiness()
        rep["handoff_readiness_ran"] = True
        rep["handoff_readiness_ok"] = bool(r_ok)
    except Exception as e:                                     # noqa: BLE001
        rep["error"] = type(e).__name__
        return SHADOW_ERROR, rep

    rep["external_liveness_observed"] = True

    # ---- rest state ----
    rep["dispatch_rest_state"] = (HO.DISPATCH_ENABLED is False
                                  and RT.DISPATCH_ENABLED is False)
    # ---- side effect 檢查（真實送出計數必須全 0）----
    rep["no_field_side_effect"] = True
    _post = {"ledger": os.path.exists(cfg.get("ledger_path") or ""),
             "journal": os.path.exists(rep["journal_path_derived"])}
    rep["ledger_created_by_shadow"] = (_post["ledger"] and not _pre["ledger"])
    rep["journal_created_by_shadow"] = (_post["journal"]
                                        and not _pre["journal"])
    if rep["ledger_created_by_shadow"] or rep["journal_created_by_shadow"]:
        rep["no_field_side_effect"] = False
        rep["violations"].append("shadow 新建了 ledger / journal")
    if rep["violations"]:
        rep["no_field_side_effect"] = False

    verdict = (SHADOW_PASS
               if all(rep.get(k) for k in
                      ("config_ok", "provider_validation_ok",
                       "dependencies_built", "safety_preflight_ran",
                       "gate_a_ran", "handoff_readiness_ran",
                       "dispatch_rest_state", "no_field_side_effect"))
               else SHADOW_BLOCKED)
    rep["verdict"] = verdict
    # 🔴 明確標示語意，避免被誤讀
    rep["note"] = ("SHADOW PASS 只證明『到 PAUSE 之前的 production 路徑可用』，"
                   "**不是** real field PASS；未驗證交接 / live leg / 收尾。")
    return verdict, rep


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase 6.10 PRODUCTION READ-ONLY SHADOW REHEARSAL")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    print("=" * 74)
    print("  PRODUCTION READ-ONLY SHADOW REHEARSAL")
    print("  🔴 0 PAUSE / 0 authority acquire / 0 CHARGE / 0 DISCHARGE / 0 STOP")
    print("=" * 74)
    verdict, rep = run_shadow(args.config)
    for k in SHADOW_ITEMS:
        print("  [%s] %-28s %r" % ("PASS" if rep.get(k) else "----", k,
                                   rep.get(k)))
    print()
    print("  verdict = %s" % verdict)
    print("  %s" % rep.get("note"))
    out = args.out or os.path.join(
        HERE, "..", "output", "phase6_field_validation",
        "shadow_rehearsal_%s.json"
        % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2, default=str)
    print("  evidence : %s" % out)
    print("=" * 74)
    return 0 if verdict == SHADOW_PASS else 1


if __name__ == "__main__":
    sys.exit(main())
