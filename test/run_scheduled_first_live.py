# -*- coding: utf-8 -*-
"""
run_scheduled_first_live.py — Scheduled FIRST LIVE（ONE-SHOT）CLI launcher
======================================================================
**非常薄的 entry point。** 責任只有六件事：

    1. parse CLI / config
    2. validate required one-shot inputs（Fail Closed）
    3. 建立 scheduled runner 所需 dependencies（由 config 指名的 provider 提供）
    4. 呼叫既有 `phase6_scheduled_first_live`
    5. 將結果寫 artifact / exit code
    6. safe exit

🔴 **不得**複製 decision logic / GATE A / GATE B / handoff / dispatcher /
   executor，也**不得**自行控制 PCS。
   本檔在模組層**只** import stdlib 與 `phase6_scheduled_first_live`；
   任何會碰到設備的相依，一律由 config 指名的 **dependency provider**
   在執行時動態載入 —— launcher 自己不 import 任何 control / executor 模組。

🔴 **無互動**：不呼叫 `input()`、不需按 Enter、不讀 stdin。
   任何必要輸入缺少 -> Fail Closed + 非 0 exit。

🔴 **Task Scheduler 獨立性**：可由
       "<python.exe>" "<absolute launcher path>" --config "<absolute config>"
   啟動，不依賴 VS Code / 互動式 terminal / 目前 cwd。
   config 內的相對路徑一律以 **config 檔所在目錄**解析（不看 cwd）。

🔴 **config 存在 != field authorization**。
   一次性授權契約仍由 `phase6_scheduled_first_live` 的
   `OneShotAuthorization` ＋ durable ledger 負責；本檔不放寬任何條件。

用法
    python run_scheduled_first_live.py --config <path-to-one-shot.json>
    python run_scheduled_first_live.py --validate-only --config <path>
"""
import argparse
import datetime
import importlib
import inspect
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import phase6_scheduled_first_live as SC              # noqa: E402

# ======================================================================
# Exit codes（穩定契約）
# ======================================================================
# 🔴 **不得**「只有 PASS 才 exit 0」——
#    安全的 ABORT / EXPIRED 是 runner 正常收斂的結果，
#    若回非 0 會讓 Task Scheduler 誤判成 launcher crash。
EXIT_HANDLED = 0        # runner 走完並產生可辨識終態（不代表 PASS）
EXIT_CONFIG_INVALID = 2  # config / invocation 無效
EXIT_REFUSED = 3        # already consumed / not enabled -> 拒絕執行
EXIT_MANUAL = 4         # recovery 需人工處理
EXIT_LAUNCHER_FAILURE = 5   # launcher 自身非預期錯誤

EXIT_NAMES = {EXIT_HANDLED: "HANDLED_TERMINAL_RESULT",
              EXIT_CONFIG_INVALID: "CONFIG_OR_INVOCATION_INVALID",
              EXIT_REFUSED: "REFUSED_ALREADY_CONSUMED_OR_NOT_ENABLED",
              EXIT_MANUAL: "RECOVERY_REQUIRES_MANUAL_ATTENTION",
              EXIT_LAUNCHER_FAILURE: "UNEXPECTED_LAUNCHER_FAILURE"}

CONFIG_OK = "CONFIG_OK"
CONFIG_INVALID = "CONFIG_INVALID"
CONFIG_NOT_ENABLED = "NOT_ENABLED"

REQUIRED_FIELDS = ("enabled", "attempt_id", "target_date", "window_start",
                   "window_end", "timezone", "dependency_provider")

# ======================================================================
# Provider allowlist
# ======================================================================
# 🔴 unattended field runner **不得**允許 config 任意指定 module path。
#    config 只能給 **symbolic provider ID**，由本表對映到正式模組。
#    未列於本表 -> CONFIG_INVALID（exit 2）。
#    **刻意不提供** --provider-module / --force-provider /
#    dynamic filesystem import / 任意 path import。
PROVIDER_ALLOWLIST = {
    "phase6_first_live_field": "phase6_first_live_field_provider",
}


# ======================================================================
# Config
# ======================================================================
class ConfigError(Exception):
    """config 無效。**一律 Fail Closed，不猜、不補預設。**"""


def _parse_date(v):
    if not isinstance(v, str):
        raise ConfigError("target_date 必須是 YYYY-MM-DD 字串")
    try:
        return datetime.date.fromisoformat(v)
    except ValueError:
        raise ConfigError("target_date 格式錯誤（需 YYYY-MM-DD）：%r" % (v,))


def _parse_time(name, v):
    if not isinstance(v, str):
        raise ConfigError("%s 必須是 HH:MM 或 HH:MM:SS 字串" % name)
    try:
        return datetime.time.fromisoformat(v)
    except ValueError:
        raise ConfigError("%s 格式錯誤（需 HH:MM[:SS]）：%r" % (name, v))


def _check_timezone(name):
    """🔴 用 stdlib zoneinfo 驗證，**不新造 timezone helper、不猜預設時區**。"""
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("timezone 必填且必須是字串")
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:                                        # pragma: no cover
        raise ConfigError("此環境缺少 zoneinfo，無法驗證 timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ConfigError("timezone 無效：%r" % (name,))
    except Exception:                                          # noqa: BLE001
        raise ConfigError("timezone 無效：%r" % (name,))


def resolve_path(base_dir, value):
    """
    🔴 相對路徑一律以 **config 檔所在目錄**解析 —— **不看 cwd**，
       這樣 Task Scheduler 以任何工作目錄啟動結果都相同。
    """
    if not value:
        return None
    p = os.path.expanduser(str(value))
    if not os.path.isabs(p):
        p = os.path.join(base_dir, p)
    return os.path.abspath(p)


def load_config(path):
    """回傳 (status, cfg, problems)。**任何問題一律 CONFIG_INVALID。**"""
    problems = []
    if not path:
        return CONFIG_INVALID, None, ["未提供 --config"]
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(p):
        return CONFIG_INVALID, None, ["config 檔不存在：%s" % p]
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except ValueError as e:
        return CONFIG_INVALID, None, ["config 非合法 JSON：%s" % e]
    except OSError as e:
        return CONFIG_INVALID, None, ["config 無法讀取：%s" % e]
    if not isinstance(raw, dict):
        return CONFIG_INVALID, None, ["config 最外層必須是 JSON object"]

    for k in REQUIRED_FIELDS:
        if k not in raw:
            problems.append("缺少必要欄位：%s" % k)
    if problems:
        return CONFIG_INVALID, None, problems

    base = os.path.dirname(p)
    cfg = {"config_path": p, "config_dir": base, "raw": dict(raw)}
    try:
        cfg["target_date"] = _parse_date(raw["target_date"])
        cfg["window_start"] = _parse_time("window_start", raw["window_start"])
        cfg["window_end"] = _parse_time("window_end", raw["window_end"])
        if cfg["window_end"] <= cfg["window_start"]:
            raise ConfigError("window_end 必須晚於 window_start（不跨日）")
        cfg["tzinfo"] = _check_timezone(raw["timezone"])
        cfg["timezone"] = raw["timezone"]
        aid = raw["attempt_id"]
        if not isinstance(aid, str) or not aid.strip():
            raise ConfigError("attempt_id 必填且必須是非空字串")
        cfg["attempt_id"] = aid.strip()
        prov = raw["dependency_provider"]
        if not isinstance(prov, str) or not prov.strip():
            raise ConfigError("dependency_provider 必填（symbolic provider ID）")
        prov = prov.strip()
        if prov not in PROVIDER_ALLOWLIST:
            # 🔴 任意 dotted module / 檔案路徑 / os / subprocess 一律拒絕
            raise ConfigError(
                "dependency_provider 不在 allowlist：%r（允許：%s）"
                % (prov, sorted(PROVIDER_ALLOWLIST)))
        cfg["dependency_provider"] = prov
        cfg["provider_module"] = PROVIDER_ALLOWLIST[prov]
    except ConfigError as e:
        return CONFIG_INVALID, None, [str(e)]

    # 🔴 enabled 必須是**明確的 True**；不自動補、不接受字串 "true"
    if raw["enabled"] is not True:
        cfg["enabled"] = False
        return CONFIG_NOT_ENABLED, cfg, ["enabled 不是 true -> 未授權"]
    cfg["enabled"] = True

    cfg["ledger_path"] = resolve_path(
        base, raw.get("ledger_path")
        or os.path.join("..", "output", "phase6_scheduled_first_live",
                        "%s.jsonl" % cfg["attempt_id"]))
    cfg["artifact_dir"] = resolve_path(
        base, raw.get("artifact_dir")
        or os.path.join("..", "output", "phase6_scheduled_first_live"))
    cfg["max_wait_sec"] = raw.get("max_wait_sec")
    cfg["created_at"] = raw.get("created_at")
    return CONFIG_OK, cfg, []


def build_authorization(cfg):
    """把已驗證的 config 轉成既有 `OneShotAuthorization`。"""
    created = cfg.get("created_at")
    if isinstance(created, str):
        try:
            created = datetime.datetime.fromisoformat(created)
        except ValueError:
            pass
    return SC.OneShotAuthorization(
        attempt_id=cfg["attempt_id"], enabled=cfg["enabled"],
        target_date=cfg["target_date"], window_start=cfg["window_start"],
        window_end=cfg["window_end"], timezone_name=cfg["timezone"],
        created_at=created)


def resolve_provider_module(provider_id):
    """symbolic ID -> 正式模組名稱。不在 allowlist 一律拒絕。"""
    if provider_id not in PROVIDER_ALLOWLIST:
        raise ConfigError("dependency_provider 不在 allowlist：%r"
                          % (provider_id,))
    return PROVIDER_ALLOWLIST[provider_id]


def load_provider(provider_id):
    """
    依 **allowlist** 載入 provider 模組。

    🔴 只接受 symbolic ID；**不接受** dotted module path、檔案路徑、
       或任何 config 自由指定的模組。
    🔴 launcher **自己不 import** 任何 control / executor 模組 ——
       設備相依一律由 provider 負責，載入失敗即 Fail Closed。
    """
    name = resolve_provider_module(provider_id)
    try:
        return importlib.import_module(name)
    except Exception as e:                                     # noqa: BLE001
        raise ConfigError("provider 模組無法載入：%s（%s: %s）"
                          % (name, type(e).__name__, e))


PROVIDER_REQUIRED = ("now_fn", "source_state_check", "safety_preflight",
                     "gate_a", "handoff_readiness", "arm_and_run_handoff")


def validate_provider(provider, cfg):
    """
    🔴 **PASSIVE ONLY** —— 只檢查 interface shape ＋ 呼叫 provider 自己的
       純離線 `validate_provider_definition()`。
       **不連 HMI / Meter / SSH、不 probe、不建立 control transport、
         不 arm、不 pause / restore、不取得 authority、不送任何 command。**
    """
    problems = []
    for fn in ("build", "validate_provider_definition"):
        if not callable(getattr(provider, fn, None)):
            problems.append("provider 缺少 %s()" % fn)
    if problems:
        raise ConfigError("；".join(problems))
    try:
        ok, checks = provider.validate_provider_definition(cfg)
    except Exception as e:                                     # noqa: BLE001
        raise ConfigError("provider validate 失敗：%s: %s"
                          % (type(e).__name__, e))
    if not ok:
        raise ConfigError("provider 前提不成立：%s"
                          % [k for k, v in (checks or {}).items() if not v])
    return checks


def build_dependencies(provider, cfg, runtime_context=None):
    """🔴 只有正式 execution path 才呼叫 —— 這裡才會建立真實 field 相依。"""
    if not hasattr(provider, "build"):
        raise ConfigError("provider 缺少 build(config)")
    try:
        # 🔴 只在 provider 的 `build()` **確實宣告**第二個參數時才傳入
        #    runtime_context —— 以 signature 判斷，不用 try/except TypeError，
        #    否則 build() 內部真正的 TypeError 會被誤判成「不支援第二個參數」。
        pass_ctx = False
        if runtime_context is not None:
            try:
                sig = inspect.signature(provider.build)
                pass_ctx = len([p for p in sig.parameters.values()
                                if p.kind in (p.POSITIONAL_ONLY,
                                              p.POSITIONAL_OR_KEYWORD)]) >= 2
            except (TypeError, ValueError):
                pass_ctx = False
        deps = (provider.build(cfg, runtime_context) if pass_ctx
                else provider.build(cfg))
    except Exception as e:                                     # noqa: BLE001
        # 🔴 provider 組相依失敗 = invocation 無效 -> Fail Closed（exit 2），
        #    **不得**讓它變成「launcher 自身崩潰」而掩蓋原因。
        raise ConfigError("dependency_provider.build() 失敗：%s: %s"
                          % (type(e).__name__, e))
    if not isinstance(deps, dict):
        raise ConfigError("provider.build() 必須回傳 dict")
    missing = [k for k in PROVIDER_REQUIRED if deps.get(k) is None]
    if missing:
        raise ConfigError("provider 未提供必要相依：%s" % missing)
    return deps


# ======================================================================
# Exit 分類
# ======================================================================
REFUSED_RESULTS = frozenset({SC.R_ALREADY_CONSUMED, SC.R_NOT_ENABLED})
MANUAL_RESULTS = frozenset({SC.R_EXCEPTION, SC.R_RESTORE_UNKNOWN,
                            SC.R_RESTORE_FAILED, SC.R_PAUSE_UNKNOWN,
                            SC.R_DISPATCH_UNKNOWN,
                    # 🔴 未提供 artifact 時保守視為需人工處理；
                    #    artifact 提供 manual_attention 後才可降級。
                    SC.R_AUTHORITY_UNKNOWN_AFTER_DISPATCH})


def classify_exit(result, artifact=None):
    """
    🔴 安全的 ABORT / EXPIRED 屬於 **handled terminal result** -> exit 0。
       只有「拒絕執行」「需人工處理」「launcher 自身失敗」才回非 0。

    🔴 OLD / NEW / WHY / EVIDENCE（2026-10-06 Attempt #9 postmortem）
      OLD : 只看 `result` 字串；`DISPATCH_UNKNOWN` 在 MANUAL_RESULTS 內，
            因此一律 exit 4，**不論** restore 與 external 接手是否已驗證。
      NEW : 另接受 `artifact`。當 artifact 已提供
            `manual_attention` 判定時，以它為準 ——
              MANUAL_ATTENTION_REQUIRED      -> EXIT_MANUAL(4)
              OPERATOR_REVIEW_RECOMMENDED    -> EXIT_HANDLED(0)
              MANUAL_ATTENTION_NOT_REQUIRED  -> EXIT_HANDLED(0)
      WHY : 「authority UNKNOWN after dispatch ＋ restore VERIFIED ＋
            EXTERNAL_RUNNING ＋ reconciliation PASS」與
            「restore UNKNOWN ＋ ownership 未解決」是兩種處置完全不同的
            情況，不應共用同一個 exit classification。
      EVIDENCE : Attempt #9 —— restore SUCCESS ／RV_RESUMED ／
            external_liveness_final EXTERNAL_RUNNING，卻得到 exit 4。
      🔴 **不放寬**：artifact 未提供 manual_attention 時，
         一律沿用舊的 result-based 判定（Fail Closed）。
         REFUSED 永遠優先於 artifact。
    """
    if result in REFUSED_RESULTS:
        return EXIT_REFUSED
    ma = (artifact or {}).get("manual_attention")
    if ma == "MANUAL_ATTENTION_REQUIRED":
        return EXIT_MANUAL
    if ma in ("OPERATOR_REVIEW_RECOMMENDED", "MANUAL_ATTENTION_NOT_REQUIRED"):
        return EXIT_HANDLED if result in SC.RESULT_STATES \
            else EXIT_LAUNCHER_FAILURE
    if result in MANUAL_RESULTS:
        return EXIT_MANUAL
    if result in SC.RESULT_STATES:
        return EXIT_HANDLED
    return EXIT_LAUNCHER_FAILURE


# ======================================================================
def build_parser():
    ap = argparse.ArgumentParser(
        prog="run_scheduled_first_live.py",
        description="Scheduled FIRST LIVE（ONE-SHOT）launcher。"
                    "不含任何控制邏輯；一切判定沿用既有模組。")
    ap.add_argument("--config", required=False, default=None,
                    help="one-shot config JSON 的路徑（必填）")
    ap.add_argument("--validate-only", action="store_true",
                    help="只驗證 config 與相依，不執行任何 attempt")
    # 🔴 刻意**不提供** --force / --bypass / --now / --window 覆寫參數
    return ap


def _emit(summary):
    try:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    except Exception:                                          # noqa: BLE001
        pass


def main(argv=None):
    started = datetime.datetime.now()
    summary = {"launcher": "run_scheduled_first_live",
               "launcher_start_time": started.isoformat(timespec="seconds"),
               "config_path": None, "config_validation": None,
               "attempt_id": None, "provider_id": None,
               "provider_module": None, "provider_validation": None,
               "scheduled_result": None,
               "artifact_path": None, "exit_code": None,
               "exit_classification": None, "problems": []}
    try:
        args = build_parser().parse_args(argv)
        summary["config_path"] = args.config
        status, cfg, problems = load_config(args.config)
        summary["config_validation"] = status
        summary["problems"] = list(problems)
        if status == CONFIG_INVALID:
            return _finish(summary, EXIT_CONFIG_INVALID)
        summary["attempt_id"] = cfg.get("attempt_id")
        if status == CONFIG_NOT_ENABLED:
            summary["scheduled_result"] = SC.R_NOT_ENABLED
            return _finish(summary, EXIT_REFUSED)

        try:
            auth = build_authorization(cfg)
            provider = load_provider(cfg["dependency_provider"])
            # 🔴 **先做 passive validate**；validate-only 到此為止，
            #    絕不進入 build_dependencies()（那才會碰設備）。
            summary["provider_id"] = cfg["dependency_provider"]
            summary["provider_module"] = cfg["provider_module"]
            summary["provider_validation"] = validate_provider(provider, cfg)
        except (ConfigError, ValueError) as e:
            summary["config_validation"] = CONFIG_INVALID
            summary["problems"].append(str(e))
            return _finish(summary, EXIT_CONFIG_INVALID)

        summary["window_start"] = auth.start_dt().isoformat(timespec="seconds")
        summary["window_end"] = auth.end_dt().isoformat(timespec="seconds")
        summary["timezone"] = cfg["timezone"]
        summary["ledger_path"] = cfg["ledger_path"]
        if args.validate_only:
            # 🔴 passive：未建立任何 client / transport，未產生 field side effect
            summary["scheduled_result"] = "VALIDATE_ONLY_NOT_EXECUTED"
            summary["field_side_effects"] = 0
            return _finish(summary, EXIT_HANDLED)

        try:
            # 🔴 FIRST LIVE permit 的授權事實一律來自**正式 config**，
            #    不是 provider 自行假定，也沒有任何 force / bypass 入口。
            rtctx = {
                "attempt_id": cfg["attempt_id"],
                "authorization": {"attempt_id": auth.attempt_id},
                "field_authorized": True,
                "one_shot_consumed": False,
                "inside_window": True,
                "target_date": cfg["target_date"].isoformat(),
                "window_start": cfg["window_start"].isoformat(),
                "window_end": cfg["window_end"].isoformat(),
                # 🔴 EXECUTION DEADLINE 需要 date / time 原型別 ＋ tzinfo。
                #    canonical 單一解析：config 已解析過，下游**不得**再
                #    從字串解析一次（2026-10-02 裁示）。
                "target_date_obj": cfg["target_date"],
                "window_end_obj": cfg["window_end"],
                "tzinfo": cfg.get("tzinfo"),
            }
            deps = build_dependencies(provider, cfg, rtctx)
        except (ConfigError, ValueError) as e:
            summary["config_validation"] = CONFIG_INVALID
            summary["problems"].append(str(e))
            return _finish(summary, EXIT_CONFIG_INVALID)

        ledger = SC.OneShotLedger(cfg["ledger_path"], now_fn=deps["now_fn"])
        runner = SC.ScheduledFirstLiveRunner(
            auth=auth, ledger=ledger, now_fn=deps["now_fn"],
            source_state_check=deps["source_state_check"],
            safety_preflight=deps["safety_preflight"],
            gate_a=deps["gate_a"],
            handoff_readiness=deps["handoff_readiness"],
            arm_and_run_handoff=deps["arm_and_run_handoff"],
            # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-24 Attempt #4 postmortem）
            #   OLD : 這份顯式 kwarg 清單漏傳 `register_pause_intent_hook`，
            #         hook 從未註冊 -> provider `_send("pause")` 跳過 ->
            #         scheduled ledger 只有 OUTCOME、沒有 INTENT。
            #   NEW : 明確傳入。intent-before-action 在 scheduled ledger
            #         與 6.10 journal **兩層**都成立。
            #   WHY : `SC.recover()` 讀的正是 scheduled ledger；缺 INTENT 會
            #         讓「pause 後崩潰」被誤判成 CLEAN，external 可能被留在
            #         暫停狀態而無人負責。
            #   EVIDENCE : Attempt #4 ledger seq2 = OUTCOME pause SENT，
            #         且 ledger.pause_attempted = False；
            #         對比 handoff_journal 00:05:47 INTENT -> 00:05:48 OUTCOME。
            register_pause_intent_hook=deps.get("register_pause_intent_hook"),
            collect_artifact=deps.get("collect_artifact"),
            safety_reset=deps.get("safety_reset"),
            host_evidence=deps.get("host_evidence"),
            waiter=deps.get("waiter"),
            max_wait_sec=cfg.get("max_wait_sec"))
        evidence = runner.run()
        summary["scheduled_result"] = evidence.get("result")
        summary["artifact_path"] = _write_artifact(cfg, evidence)
        # 🔴 exit classification 依 artifact 的 manual_attention 判定
        #    （未提供時沿用舊的 result-based 判定，Fail Closed）。
        return _finish(summary, classify_exit(
            evidence.get("result"), evidence.get("handoff_artifact")))
    except SystemExit:
        raise
    except Exception as e:                                     # noqa: BLE001
        summary["problems"].append("%s: %s" % (type(e).__name__, e))
        return _finish(summary, EXIT_LAUNCHER_FAILURE)


def _write_artifact(cfg, evidence):
    d = cfg.get("artifact_dir")
    if not d:
        return None
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
        name = "scheduled_first_live_%s_%s.json" % (
            cfg["attempt_id"],
            datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(evidence, fh, ensure_ascii=False, indent=2, default=str)
        return path
    except OSError:
        # 🔴 artifact 寫檔失敗**不得**改變控制結果；durable evidence 仍在 ledger
        return None


def _finish(summary, code):
    summary["exit_code"] = code
    summary["exit_classification"] = EXIT_NAMES.get(code, "UNKNOWN")
    summary["launcher_end_time"] = datetime.datetime.now().isoformat(
        timespec="seconds")
    _emit(summary)
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        pass
    raise SystemExit(main())
