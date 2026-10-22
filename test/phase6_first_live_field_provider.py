# -*- coding: utf-8 -*-
"""
phase6_first_live_field_provider.py — FIELD DEPENDENCY WIRING ONLY
======================================================================
定位：把**既有元件**接起來，供 `phase6_scheduled_first_live` 使用。

    HMI / device observation      既有 PRD.build_ess_reader + EssSnapshotAdapter
    Meter client                  既有 PRD.build_meter_client
    B2 SSH transport / guard      既有 RA.SshTransport + phase6_guard_ssh_runner
    Fresh Safety Preflight        既有 FL.first_live_safety_preflight
    GATE A observation            既有 live chain（SVC.build_live_chain_bundle）
    GATE A 判定                   既有 FL.pre_handoff_policy_eligibility
    Handoff readiness             既有 FL.handoff_readiness
    GATE B                        既有 FL.post_handoff_revalidation
    Phase6AuthorityLease          既有 FL.Phase6AuthorityLease
    FIRST LIVE Dispatcher         既有 FL.FirstLiveDispatcher
    6.9 live leg                  既有 SVC.run_live_leg_cli
    Handoff                       既有 HO.HandoffRunner
    artifact / ledger             既有 FL.collect_handoff_artifact / SC ledger

🔴 **只 construct / wire / return** —— 本模組**不**判斷 CHARGE / DISCHARGE、
   不判斷 OFF_PEAK / PEAK、不判斷 SOC policy、不取得 control authority、
   不送 PCS command、不送 pause / restore、不做 readback policy、
   不新增 retry / fallback / bypass。

🔴 **模組層只 import stdlib** —— 所有專案模組一律 **lazy import**，
   因此 `import phase6_first_live_field_provider` 本身
   **零 field I/O、零 side effect**（由回歸測試鎖住）。

🔴 **validate ≠ build**
       validate_provider_definition(config)  純離線；不連 HMI / Meter / SSH
       build_field_dependencies(config)      只有正式 scheduled run 才呼叫

🔴 credential：只用既有 dedicated key ＋ B2 forced-command guard contract；
   缺任何必要條件即 Fail Closed，**不 fallback** 到一般 SSH / 其他 key /
   password auth / bypass guard。**不硬編、不複製、不寫入 artifact 任何 secret。**

用法
    python phase6_first_live_field_provider.py    # 離線自述；不連線
"""
import dataclasses
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PROVIDER_ID = "phase6_first_live_field"
EXPECTED_GUARD_VARIANT = "B2"

# 🔴 本模組的 `__main__` 只做**離線自述 ＋ validate**，
#    結構上到不了 live leg / 任何控制路徑（由回歸 AST 證明）。
CLI_ENTRY_IS_PASSIVE = True

# 🔴 這些鍵就是 `ScheduledFirstLiveRunner` 的相依名稱；不另立第二套字彙。
REQUIRED_DEPENDENCIES = ("now_fn", "source_state_check", "safety_preflight",
                         "gate_a", "handoff_readiness", "arm_and_run_handoff")
OPTIONAL_DEPENDENCIES = ("register_pause_intent_hook",
                         "collect_artifact", "safety_reset", "host_evidence",
                         "waiter")

VALIDATION_ITEMS = ("provider_id_match", "core_modules_importable",
                    "guard_variant_b2", "credentials_present",
                    "safe_defaults_intact", "interface_complete",
                    "no_field_io_on_import")


class ProviderPreconditionFailed(RuntimeError):
    """必要前提不成立 —— Fail Closed，不 fallback。"""


class ProviderWiringError(RuntimeError):
    """canonical decision path 取不到 —— **結構性 wiring 缺陷**，不是 runtime 未知。"""


# ======================================================================
# Canonical SOC band accessor（BUG FIX #1，2026-09-22）
# ======================================================================
SOC_BAND_CANONICAL_PATH = "bundle.factory(None, None).arbiter.observer.policy.soc_band"


def canonical_soc_band(bundle):
    """
    取得 **canonical** SOC band —— `TouArbitragePolicy.soc_band`
    -> `SocBandLatch.band`，即既有 production decision path 的輸出。

    🔴 provider 只做 wiring：**不得**在此另寫第二套 SOC policy，
       **不得**硬編 `SOC < 90`、`20.0` / `90.0` 等門檻。

    🔴 兩種 None 必須分開：
       * 路徑斷掉（`LiveChainBundle` 沒有 `factory`、chain 沒有 `arbiter`…）
         = **結構性缺陷** -> raise `ProviderWiringError`（大聲失敗）。
       * 路徑健在但 latch 尚未定狀態 = 合法 runtime 未知 -> 回傳 `None`
         （交由呼叫端 Fail Closed 拒絕）。
       2026-09-22 Attempt #2 之所以能靜默中止，正是因為舊寫法把前者
       降級成後者。
    """
    factory = getattr(bundle, "factory", None)
    if not callable(factory):
        raise ProviderWiringError(
            "live bundle 沒有可呼叫的 factory；canonical path = %s"
            % SOC_BAND_CANONICAL_PATH)
    chain = factory(None, None)
    arbiter = getattr(chain, "arbiter", None)
    if arbiter is None:
        raise ProviderWiringError(
            "execution chain 沒有 arbiter；canonical path = %s"
            % SOC_BAND_CANONICAL_PATH)
    observer = getattr(arbiter, "observer", None)
    if observer is None:
        raise ProviderWiringError(
            "arbiter 沒有 observer；canonical path = %s"
            % SOC_BAND_CANONICAL_PATH)
    policy = getattr(observer, "policy", None)
    if policy is None:
        raise ProviderWiringError(
            "observer 沒有 policy；canonical path = %s"
            % SOC_BAND_CANONICAL_PATH)
    if not hasattr(policy, "soc_band"):
        raise ProviderWiringError(
            "policy 沒有 soc_band；canonical path = %s"
            % SOC_BAND_CANONICAL_PATH)
    return policy.soc_band


# ======================================================================
# Handoff observation adapter（Attempt #3 postmortem，BUG #3）
# ======================================================================
# 🔴 `HandoffOrchestrator` / `HandoffRunner` 的 Step 1 `pre_pause_gates`
#    使用一套 **handoff 詞彙**（`soc_fresh` / `ess_fresh` / `tou` /
#    `decision` / `external_behaviour_known` …）。
#    Attempt #3 之前 provider 直接把原始 `sample()` 餵進去，
#    結構上永遠對不上 -> GATE A PASS 但 pre-pause preflight 必 FAIL。
#
# 🔴 本層**只做 mapping**：
#      不重新實作 policy、不重算 SOC 門檻、不重判 TOU、不重判 decision、
#      不發明 freshness rule。每個欄位都指向既有 canonical 產出。
#
# 🔴 未能對應到 canonical 來源的欄位一律留 `None` -> `pre_pause_gates`
#    的 `is True` 判定直接 Fail Closed。**不得**為了讓 gate 通過而填 True。
SOC_MAX_PCT_CANONICAL_PATH = (
    "bundle.factory(None, None).arbiter.observer.policy.cfg.soc_charge_stop_pct")

# 🔴 `external_behaviour_known` 正式定義（2026-09-23 裁示）
#      「目前對 external controller 的 loop / runtime behaviour，已有足夠的
#        既有 read-only evidence，可確定分類為 RUNNING 或 NOT_RUNNING，
#        而不是 UNKNOWN。」
#    🔴 KNOWN != ACTIVE。**不得**定義成 external 正在充放電 / 正在控制 PCS /
#       PCS 非零功率 / Authority EXTERNAL / Trial #2 Active Gate PASS。
#    🔴 canonical source = 既有 `phase6_remote_adapter.external_liveness()`
#       的三態裁決（probe facts ＋ loop evidence）。**不新增第二套 classifier。**
#    🔴 本 provider 只做 mapping：`verdict != EXT_UNKNOWN`。
#       不自行判 mark age / mark count / advancing threshold / process semantics。
EXTERNAL_BEHAVIOUR_KNOWN_CANONICAL_SOURCE = (
    "phase6_remote_adapter.external_liveness() -> "
    "(EXT_RUNNING | EXT_NOT_RUNNING | EXT_UNKNOWN)")
EXTERNAL_BEHAVIOUR_KNOWN_STATUS = "CONFIGURED"
# loop evidence 觀測數沿用既有 `HandoffConfig.restore_loop_min_observations`
# （與 `handoff_readiness()` / RestoreVerifier 同一份契約），不新增常數。
EXTERNAL_LIVENESS_MIN_OBS_SOURCE = (
    "phase6_handoff_orchestrator.HandoffConfig.restore_loop_min_observations")

# 本 adapter 負責供給的欄位（其餘由 `_decision_sample()` 既有輸出提供）
HANDOFF_ADAPTED_FIELDS = ("soc_fresh", "ess_fresh", "tou",
                          "external_behaviour_known")

# 每個 `pre_pause_gates` 欄位的 canonical 來源（供文件與回歸直接引用）
HANDOFF_OBSERVATION_CANONICAL_SOURCES = {
    "soc_fresh": "EssSnapshotAdapter.observe().valid/.stale（同一筆 ESS 觀測）",
    "meter_fresh": "GridMeterClient.get_snapshot().valid/.stale",
    "ess_fresh": "EssSnapshotAdapter.observe().valid/.stale",
    "tou_off_peak": "arbitration.tou_state（production TariffProvider）",
    "soc_in_charge_band": SOC_MAX_PCT_CANONICAL_PATH + " + ess.soc_percent",
    "decision_is_charge": "arbitration.fresh_decision_action",
    "comm_ok": "ControlObservation.valid",
    "fault_clear": "ControlObservation.pcs_fault",
    "alarm_clear": "ControlObservation.alarm_rows（level 0 且 alarmStatus）",
    "external_runtime_healthy": "B2 guard probe（screen/process/identity）",
    "external_behaviour_known": EXTERNAL_BEHAVIOUR_KNOWN_CANONICAL_SOURCE,
}


def canonical_soc_max_pct(bundle):
    """
    取得 **canonical** 充電閂上限 —— 與 `canonical_soc_band()` **同一條**
    production path 上的 `PolicyConfig.soc_charge_stop_pct`。

    🔴 OLD / NEW / WHY
      OLD : `soc_max_pct = ctx.get("soc_max_pct")` -> 永遠 None
            -> `pre_pause_gates.soc_in_charge_band` 結構上不可能通過。
      NEW : 從 live chain 上**實際在用**的 policy 物件取值。
      WHY : 不得硬編 90.0；也不得另建第二份 PolicyConfig ——
            必須是 `soc_band` latch 正在使用的同一份設定。

    路徑斷掉 = 結構性缺陷 -> raise `ProviderWiringError`（大聲失敗）。
    """
    factory = getattr(bundle, "factory", None)
    if not callable(factory):
        raise ProviderWiringError(
            "live bundle 沒有可呼叫的 factory；canonical path = %s"
            % SOC_MAX_PCT_CANONICAL_PATH)
    chain = factory(None, None)
    arbiter = getattr(chain, "arbiter", None)
    observer = getattr(arbiter, "observer", None) if arbiter is not None else None
    policy = getattr(observer, "policy", None) if observer is not None else None
    if policy is None:
        raise ProviderWiringError(
            "canonical policy 路徑斷裂；canonical path = %s"
            % SOC_MAX_PCT_CANONICAL_PATH)
    cfg = getattr(policy, "cfg", None)
    if cfg is None or not hasattr(cfg, "soc_charge_stop_pct"):
        raise ProviderWiringError(
            "policy 沒有 cfg.soc_charge_stop_pct；canonical path = %s"
            % SOC_MAX_PCT_CANONICAL_PATH)
    v = cfg.soc_charge_stop_pct
    if not isinstance(v, (int, float)):
        raise ProviderWiringError(
            "soc_charge_stop_pct 非數值：%r" % (v,))
    return float(v)


def external_behaviour_known_from(verdict):
    """
    canonical 三態 -> `external_behaviour_known`。**唯一的 mapping 規則。**

        EXT_RUNNING      -> True
        EXT_NOT_RUNNING  -> True
        EXT_UNKNOWN      -> False
        其他 / None      -> False（Fail Closed）

    🔴 本質就是 `resolved_external_running is not UNKNOWN`，不另外推導。
    🔴 **不得**在此加入 PCS state / power / meter / Authority / SOC 判斷 ——
       那些最多是其他 evidence，不是本欄位的 canonical source。
    """
    import phase6_remote_adapter as _RA
    return verdict in (_RA.EXT_RUNNING, _RA.EXT_NOT_RUNNING)


def build_handoff_observation(decision_sample,
                              external_liveness_verdict=None):
    """
    把既有 canonical evidence 轉成 `pre_pause_gates` 所需詞彙。

    🔴 **純 mapping**，無 I/O、無 policy、無門檻、無 fallback 預設值。
    🔴 輸入必須是 `_decision_sample()` 的輸出（已含 canonical
       `tou_state` / `decision` / `soc` / `comm_ok` / `meter_fresh` /
       `fault` / `critical_alarms` / `ess_valid` / `ess_stale`）。
    🔴 `external_liveness_verdict` 必須是
       `phase6_remote_adapter.external_liveness()` 的裁決字串。
    🔴 輸入不是 dict -> 回傳全 None 的骨架，讓 gate Fail Closed。
    """
    if not isinstance(decision_sample, dict):
        # 🔴 骨架同樣走 canonical mapping —— `external_behaviour_known`
        #    一律是 bool（此處必為 False），不得回 None 讓語意變模糊。
        skel = dict((k, None) for k in HANDOFF_ADAPTED_FIELDS)
        skel["external_behaviour_known"] = external_behaviour_known_from(
            external_liveness_verdict)
        skel["external_liveness_verdict"] = external_liveness_verdict
        return skel
    out = dict(decision_sample)
    # ESS / SOC 新鮮度：設備未提供獨立 SOC 時戳（SOURCE_COHERENCE =
    # UNRESOLVED），因此 canonical 新鮮度就是這一筆 ESS 觀測的判定；
    # `EssSnapshot` 有效性本身已涵蓋 soc 缺失／非有限／讀太慢／stale。
    ess_ok = (out.get("ess_valid") is True and out.get("ess_stale") is not True)
    out["ess_fresh"] = ess_ok
    out["soc_fresh"] = bool(ess_ok
                            and isinstance(out.get("soc"), (int, float)))
    # canonical TOU state -> orchestrator 詞彙（只改名，不改值）
    out["tou"] = out.get("tou_state")
    # canonical 三態 -> bool（UNKNOWN / 取不到 -> False -> Fail Closed）
    out["external_behaviour_known"] = external_behaviour_known_from(
        external_liveness_verdict)
    out["external_liveness_verdict"] = external_liveness_verdict
    return out


# ======================================================================
# Typed dependency container
# ======================================================================
@dataclasses.dataclass
class FieldDependencies(object):
    """`ScheduledFirstLiveRunner` 實際需要的相依。**typed，不回大型 untyped dict。**"""

    now_fn: object
    source_state_check: object
    safety_preflight: object
    gate_a: object
    handoff_readiness: object
    arm_and_run_handoff: object
    collect_artifact: object = None
    safety_reset: object = None
    host_evidence: object = None
    waiter: object = None
    register_pause_intent_hook: object = None

    def missing(self):
        return [k for k in REQUIRED_DEPENDENCIES if getattr(self, k) is None]

    def as_runner_kwargs(self):
        d = dataclasses.asdict(self)
        # dataclasses.asdict 會深拷貝，callable 需改用 getattr 取原物件
        return dict((k, getattr(self, k)) for k in d)


# ======================================================================
# validate_provider_definition —— PURE / OFFLINE
# ======================================================================
def validate_provider_definition(config=None):
    """
    回傳 (ok, checks)。

    🔴 **純離線**：只做 module importability、常數比對、本機檔案存在性、
       interface shape。**不連 HMI / Meter / SSH，不建立任何 client /
       transport，不 arm、不 probe。**
    """
    checks = dict((k, False) for k in VALIDATION_ITEMS)
    cfg = config or {}
    want = cfg.get("dependency_provider", PROVIDER_ID)
    checks["provider_id_match"] = (want in (PROVIDER_ID, None, "")
                                   or want == PROVIDER_ID)

    try:
        import importlib
        mods = {}
        for name in ("phase6_remote_adapter", "phase6_handoff_orchestrator",
                     "phase6_first_live_handoff", "phase6_scheduled_first_live",
                     "phase6_execution_deadline",
                     "phase6_guard_ssh_runner", "phase6_hmi_auth_renewal",
                     "pcs_auto_control_service",
                     "pcs_auto_control_production", "pcs_auto_control_config",
                     "ess_snapshot_adapter", "pcs_control_integration",
                     "control_authority", "decision_policy"):
            mods[name] = importlib.import_module(name)
        checks["core_modules_importable"] = True
    except Exception:                                          # noqa: BLE001
        return False, checks

    RA = mods["phase6_remote_adapter"]
    HO = mods["phase6_handoff_orchestrator"]
    RT = importlib.import_module("pcs_auto_control_runtime")
    GR = mods["phase6_guard_ssh_runner"]

    checks["guard_variant_b2"] = (RA.DEPLOYED_GUARD_VARIANT
                                  == EXPECTED_GUARD_VARIANT)
    cred_ok, _cred = GR.check_credentials()
    checks["credentials_present"] = bool(cred_ok)
    checks["safe_defaults_intact"] = bool(
        HO.DISPATCH_ENABLED is False and RT.DISPATCH_ENABLED is False
        and RA.RemoteSenders().armed is False
        and RA.SshTransport().armed is False
        and RA.SshTimeouts().command_timeouts.get("pause") is None
        and RA.SshTimeouts().command_timeouts.get("restore") is None)
    checks["interface_complete"] = all(
        callable(globals().get(n))
        for n in ("build_field_dependencies", "validate_provider_definition",
                  "build"))
    # 🔴 本模組層只 import stdlib -> import 本身不可能產生 field I/O
    checks["no_field_io_on_import"] = True
    return all(checks.values()), checks


# ======================================================================
# build_field_dependencies —— 只有正式 scheduled run 才呼叫
# ======================================================================
def build_field_dependencies(config, runtime_context=None):
    """
    建立真實 field 相依。**會連 HMI / Meter / SSH，只在 execution path 呼叫。**

    🔴 呼叫前一律先跑 `validate_provider_definition()`；不通過即
       `ProviderPreconditionFailed`（Fail Closed）。
    """
    ok, checks = validate_provider_definition(config)
    if not ok:
        raise ProviderPreconditionFailed(
            "provider 前提不成立：%s" % [k for k, v in checks.items() if not v])

    ctx = dict(runtime_context or {})
    import importlib
    RA = importlib.import_module("phase6_remote_adapter")
    HO = importlib.import_module("phase6_handoff_orchestrator")
    RT = importlib.import_module("pcs_auto_control_runtime")
    FL = importlib.import_module("phase6_first_live_handoff")
    DL = importlib.import_module("phase6_execution_deadline")
    GR = importlib.import_module("phase6_guard_ssh_runner")
    SVC = importlib.import_module("pcs_auto_control_service")
    PRD = importlib.import_module("pcs_auto_control_production")
    ADP = importlib.import_module("ess_snapshot_adapter")
    PCI = importlib.import_module("pcs_control_integration")
    CA = importlib.import_module("control_authority")
    DP = importlib.import_module("decision_policy")
    US = importlib.import_module("phase6_unattended_service")
    AUTH = importlib.import_module("phase6_hmi_auth_renewal")
    CFG = HO.HandoffConfig

    # ---- 1. 遠端：唯讀 transport（armed=False）----
    runner = ctx.get("ssh_runner") or GR.GuardSshRunner()
    ro_timeouts = RA.SshTimeouts(
        connect_timeout_sec=RA.APPROVED_SSH_CONNECT_TIMEOUT_SEC,
        command_timeouts=dict(RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC))
    ro_tr = RA.SshTransport(timeouts=ro_timeouts, runner=runner, armed=False,
                            clock=time.monotonic)

    state = {"probe_raw": "", "probe_fields": {}, "verb_counts": {},
             "post_handoff": False}

    def _run(verb):
        r = ro_tr.run(verb)
        state["verb_counts"][verb] = state["verb_counts"].get(verb, 0) + 1
        return r

    def _kv(text):
        d = {}
        for ln in (text or "").splitlines():
            if "=" in ln and not ln.startswith(("Transferred", "Bytes")):
                k, _, v = ln.partition("=")
                d[k.strip()] = v.strip()
        return d

    def probe():
        r = _run("probe")
        f = _kv(r.stdout)
        state["probe_raw"] = r.stdout or ""
        state["probe_fields"] = f
        cap_ok, _g = RA.capability_gate(RA.parse_capability(r.stdout))
        return {"ssh_ok": r.outcome == RA.SSH_OK,
                "screen_alive": f.get("screen_count") == "1",
                "process_alive": bool(f.get("pid")),
                "process_identity_ok": (f.get("cwd") == RA.EXPECTED_CWD
                                        and f.get("cmd") == RA.EXPECTED_CMD),
                "guard_variant": f.get("guard_variant"),
                "capability_ok": bool(cap_ok), "pid": f.get("pid")}

    def loop_mark():
        return _kv(_run("loopcheck").stdout).get("mark2")

    # ---- 2. 本機：HMI / Meter（既有唯讀路徑，不新增第二套 scraper）----
    client = ctx.get("api_client")
    if client is None:
        client, token = PRD.build_api_client(login=True)
        if token is None:
            raise ProviderPreconditionFailed("HMI 登入失敗 -> Fail Closed")
    # 🔴 PART A：HMI READ 認證續期。
    #    沿用既有 6.9 bounded re-auth（bound = 2、只認 _error401），
    #    但採**原地** re-login —— `client.login_hmi()` 更新的是同一個 client
    #    物件，因此 provider adapter / live chain / live-leg verifier
    #    **全部同時**拿到新憑證，不可能出現混合狀態，
    #    也不會重建 chain 而重置 PowerClassifier debounce 與 SocBandLatch。
    #    🔴 只恢復 READ；本層沒有任何 command 能力。
    _raw_reader = ctx.get("ess_reader") or PRD.build_ess_reader(client)
    _relogin = ctx.get("relogin") or AUTH.build_inplace_relogin(client)
    reader = AUTH.AuthRenewingReader(_raw_reader, _relogin)
    adapter = ADP.EssSnapshotAdapter(reader=reader)
    meter = ctx.get("meter_client")
    if meter is None:
        meter = PRD.build_meter_client(connect=True)
        time.sleep(float(ctx.get("meter_warmup_sec", 2.5)))
    # 🔴 additive 注入點：offline construction smoke 可注入 fake store，
    #    避免碰到正式 LastControl 檔案。未注入即維持既有 production 行為。
    store = ctx.get("last_control_store") or PRD.build_last_control_store()
    bundle = ctx.get("live_bundle") or SVC.build_live_chain_bundle(
        client=client, meter_client=meter)

    def sample():
        obs = adapter.observe()
        cur = store.current()
        auth = CA.evaluate(
            PCI.build_authority_request(PCI.PipelineInputs(
                decision=None, ess=obs.ess, pcs_fault=obs.pcs_fault,
                alarm_rows=obs.alarm_rows,
                alarm_source_complete=obs.alarm_source_complete,
                pcs_mode_state=obs.pcs_mode_state, last_control=None,
                last_control_record=cur.record,
                last_control_trust=cur.trust)),
            policy=CA.AuthorityPolicy(authority_ttl_sec=180.0,
                                      authority_power_tolerance_kw=1.25),
            now=time.monotonic())
        crit = [x for x in (obs.alarm_rows or ())
                if x.get("level") == 0 and x.get("alarmStatus")]
        snap = meter.get_snapshot()
        return {"fresh": obs.valid is True, "comm_ok": obs.valid is True,
                # 🔴 handoff adapter 需要的 canonical ESS 新鮮度原料
                "ess_valid": obs.valid is True, "ess_stale": obs.stale,
                "ess_age_sec": obs.age_sec, "ess_reason": obs.reason,
                "meter_fresh": bool(snap.valid is True and snap.stale is False),
                "meter_kw": snap.power_kw, "pcs_state": obs.pcs_state,
                "authority": auth.state,
                "ac_kw": getattr(obs.ess, "actual_active_power_kw", None),
                "fault": obs.pcs_fault, "critical_alarms": len(crit),
                "ownership": HO.classify_ownership(
                    external_running=(True if auth.state
                                      == "EXTERNAL_OR_UNKNOWN" else None),
                    phase6_holding=False),
                "soc": getattr(obs.ess, "soc_percent", None)}

    # ---- 3. GATE A / GATE B 的輸入（判定仍由既有 FL 函式做）----
    def _tou_allows_charge(tou):
        return bool(tou is not None and any(
            k[0] == tou and v == DP.CHARGE
            for k, v in DP.DECISION_MATRIX.items()))

    def _soc_allows_charge(band):
        return bool(band is not None and any(
            k[2] == band and v == DP.CHARGE
            for k, v in DP.DECISION_MATRIX.items()))

    # ------------------------------------------------------------------
    # 🔴 BUG FIX #1（2026-09-22）—— canonical SOC band 來源
    #   OLD : getattr(getattr(getattr(bundle, "arbiter", None),
    #                          "observer", None), "soc_band", None)
    #         兩處都錯：① `LiveChainBundle` 只有 factory / observe / reader /
    #         store，**沒有 arbiter**；② `soc_band` 掛在 policy 上，不在
    #         observer 上。結果 band 恆為 None -> soc_allows_charge 恆為 False，
    #         GATE A 在任何真實 SOC 下都不可能通過（2026-09-22 Attempt #2 成因）。
    #   NEW : chain.arbiter.observer.policy.soc_band
    #         —— `TouArbitragePolicy.soc_band` -> `SocBandLatch.band`，
    #         是既有 production decision path 的 canonical output。
    #   WHY : provider 只能 wiring canonical 結果，不得自建第二套 SOC policy。
    #   EVIDENCE: build_live_chain_bundle 內 `factory` 以 closure 捕捉**同一個**
    #         arbiter，故 `factory(None, None).arbiter` 與決策鏈用的是同一實例，
    #         latch 狀態不會流失。
    def _canonical_soc_band():
        return canonical_soc_band(bundle)

    # 🔴 BUG FIX #2（2026-09-22）—— classifier stabilization
    #   既有 `prime_classifier()` 的 docstring 明載：PowerClassifier 的 stable
    #   採 time-based debounce，**單次 update() 結構上不可能脫離 UNKNOWN**。
    #   原本 `_decision_sample()` 只 observe() 一次 -> grid 恆 UNKNOWN ->
    #   自然決策恆 no_action。
    #   🔴 **沿用既有正式 helper 與既有值**，不自訂次數 / 秒數：
    #      `phase6_first_live_runner.prime_classifier`
    #      `PRIME_INTERVAL_SEC = PC.DEFAULT_CONFIG.debounce_sec`
    #      預算沿用 6.9 relative-window 分支的 `PRIME_INTERVAL_SEC * 4`。
    #   🔴 有界：預算用盡即停，不無限 polling；仍不穩定 -> 交由 GATE A
    #      Fail Closed，**不得**重跑整個 Attempt。
    _primed = {"done": False}

    def _prime_classifier_once():
        if _primed["done"]:
            return None
        _primed["done"] = True
        fn = ctx.get("prime_classifier")
        if fn is not None:
            return fn(bundle.observe)
        R = importlib.import_module("phase6_first_live_runner")
        until = datetime.datetime.now() + datetime.timedelta(
            seconds=R.PRIME_INTERVAL_SEC * 4)
        return R.prime_classifier(bundle.observe, until, R.PRIME_INTERVAL_SEC)

    def _decision_sample(post=False):
        if not post:
            # GATE A 之前先暖機；GATE B 時分類器已隨同一 arbiter 保持溫熱
            _prime_classifier_once()
        # 🔴 PART A 順序要求：先走**會續期**的 reader（`sample()`），
        #    才呼叫 live chain 的 `observe()`。
        #    chain 內部的 reader 未被包裝，但與本 reader 共用**同一個** client
        #    物件；只要續期先發生，chain 這一讀就已拿到新 token。
        #    反過來（先 observe 再 sample）會讓 chain 讀落在續期之前，
        #    401 只能 Fail Closed 而無法恢復。
        base = sample()
        res = bundle.observe()
        arb = getattr(res, "arbitration", None) or res
        band = _canonical_soc_band()
        d = {"decision": getattr(arb, "fresh_decision_action", None),
             "grid_state": getattr(arb, "grid_state", None),
             "classifier_primed": _primed["done"],
             "tou_allows_charge": _tou_allows_charge(
                 getattr(arb, "tou_state", None)),
             "soc_allows_charge": _soc_allows_charge(band),
             "safety_gate_ok": getattr(arb, "safety_allowed", None),
             "tou_state": getattr(arb, "tou_state", None),
             "soc_band": band}
        base.update(d)
        base["authority"] = getattr(arb, "authority_state", base["authority"])
        base["pcs_state"] = getattr(res, "pcs_state", base["pcs_state"])
        if post:
            base["post_handoff_fresh"] = True
        return base

    # ---- 4. Scheduled runner 相依 ----
    def now_fn():
        return datetime.datetime.now()

    def source_state_check():
        R = importlib.import_module("phase6_first_live_runner")
        return {"integration_module_ok": True,
                "runner_direct_blocked": R.DIRECT_FIELD_EXECUTION_BLOCKED,
                "service_cli_blocked": SVC.LIVE_LEG_CLI_BLOCKED,
                "guard_variant_ok": RA.DEPLOYED_GUARD_VARIANT == "B2",
                "safe_defaults_ok": (HO.DISPATCH_ENABLED is False
                                     and RT.DISPATCH_ENABLED is False
                                     and RA.RemoteSenders().armed is False),
                "auth_renewal_bound": AUTH.AUTH_RENEWAL_MAX_ATTEMPTS,
                "auth_renewal_count": reader.renewal_count}

    def safety_flags():
        return {"dispatch_enabled": HO.DISPATCH_ENABLED, "mode": "DRY_RUN",
                "senders_armed": RA.RemoteSenders().armed}

    def _mark(key, ok):
        state[key] = bool(ok)
        return ok

    def safety_preflight():
        r = FL.first_live_safety_preflight(sample(), probe(), safety_flags())
        _mark("preflight_ok", r[0])
        return r

    def gate_a():
        r = FL.pre_handoff_policy_eligibility(_decision_sample())
        _mark("gate_a_ok", r[0])
        return r

    def handoff_readiness():
        prb = probe()
        marks = [loop_mark() for _ in range(CFG.restore_loop_min_observations)]
        r = FL.handoff_readiness(
            prb, loop_marks=marks,
            min_loop_observations=CFG.restore_loop_min_observations,
            capability=RA.parse_capability(state["probe_raw"]),
            pause_capable=US.remote_pause_capable,
            restore_capable=US.remote_restore_capable)
        _mark("readiness_ok", r[0])
        return r

    holder = {}

    def _external_liveness():
        """🔴 取得 canonical 三態。**完全委派**既有 resolver，不自行判定。"""
        prb = probe()
        marks = [loop_mark()
                 for _ in range(CFG.restore_loop_min_observations)]
        verdict, why = RA.external_liveness(
            prb, loop_marks=marks,
            min_loop_observations=CFG.restore_loop_min_observations)
        rec = {"verdict": verdict, "detail": why,
               "at": datetime.datetime.now().isoformat(timespec="seconds"),
               "min_loop_observations": CFG.restore_loop_min_observations,
               "loop_marks": list(marks)}
        # 🔴 additive evidence：保留 pre-pause / post-pause / post-restore
        #    三個階段，**不得**覆蓋成單一最後值。
        #    階段由 handoff 生命週期決定，不由本層重新判斷。
        state.setdefault("liveness_history", []).append(rec)
        phase = state.get("liveness_phase") or "pre_pause"
        state.setdefault("liveness_by_phase", {})
        state["liveness_by_phase"].setdefault(phase, rec)
        state["liveness_by_phase"][phase] = rec
        state["external_liveness"] = rec          # backward compatible（最後值）
        return verdict

    def _handoff_sample():
        """🔴 餵給 6.10 orchestrator 的**唯一**取樣來源（經 adapter 轉詞彙）。"""
        return build_handoff_observation(
            _decision_sample(),
            external_liveness_verdict=_external_liveness())

    def register_pause_intent_hook(fn):
        """由 scheduled runner 註冊；在 pause sender 呼叫**前一刻**回呼。"""
        state["pause_intent_hook"] = fn

    def _build_deadline():
        """
        建立 canonical `ExecutionDeadline`。

        🔴 **唯一**建立點 —— 其他 module 不得自行解析 target_date /
           window_end（2026-10-02 裁示）。
        🔴 只接受**原型別** `datetime.date` / `datetime.time`
           （launcher 以 `target_date_obj` / `window_end_obj` 傳入）——
           本層**不**解析字串，避免成為第二套解析實作。
        🔴 ctx 可直接注入 `deadline`（離線測試用，與 soc_max_pct 同慣例）。
        🔴 建不出來時回 None（等同未注入），並把原因記進 state ->
           artifact 可見。**不得**靜默地假裝有 deadline。
        """
        if ctx.get("deadline") is not None:
            state["deadline_source"] = "INJECTED"
            return ctx["deadline"]
        td = ctx.get("target_date_obj")
        we = ctx.get("window_end_obj")
        if not isinstance(td, datetime.date) or not isinstance(
                we, datetime.time):
            state["deadline_source"] = "UNAVAILABLE"
            state["deadline_unavailable_reason"] = (
                "runtime context 缺少 target_date_obj / window_end_obj "
                "原型別（取得 %r / %r）" % (type(td).__name__,
                                            type(we).__name__))
            return None
        try:
            d = DL.ExecutionDeadline.from_window(
                td, we, tzinfo=ctx.get("tzinfo"),
                label=ctx.get("attempt_id"))
            state["deadline_source"] = "FROM_WINDOW"
            return d
        except Exception as e:                                 # noqa: BLE001
            state["deadline_source"] = "UNAVAILABLE"
            state["deadline_unavailable_reason"] = (
                "建立失敗：%s" % type(e).__name__)
            return None

    def _permit_evidence():
        """
        FIRST LIVE permit 的 16 項前置條件。**只讀既有執行事實。**

        🔴 全部來自本次 attempt 已產生的 canonical evidence：
           authorization（來自正式 config）、各 gate 的實際結果、
           `HandoffRunner` 的 step 記錄、`Phase6AuthorityLease`、
           `CommandCountGuard`。任何一項取不到 -> False -> Fail Closed。
        """
        auth = ctx.get("authorization") or {}
        hr = holder.get("handoff_runner")
        lease = holder.get("lease")
        guard = holder.get("guard")
        steps = {s.get("step"): s for s in (getattr(hr, "steps", None) or ())}

        def step_ok(name):
            return bool(steps.get(name, {}).get("ok"))

        counts = (guard.as_dict().get("counts") if guard is not None else {})
        limits = (guard.as_dict().get("limits") if guard is not None else {})
        return {
            "attempt_field_authorized": ctx.get("field_authorized") is True,
            "attempt_id_matches": (auth.get("attempt_id")
                                   == ctx.get("attempt_id")
                                   and bool(ctx.get("attempt_id"))),
            "one_shot_not_consumed": ctx.get("one_shot_consumed") is False,
            "inside_authorized_window": ctx.get("inside_window") is True,
            "fresh_safety_preflight_ok": state.get("preflight_ok") is True,
            "gate_a_ok": state.get("gate_a_ok") is True,
            "handoff_readiness_ok": state.get("readiness_ok") is True,
            "pre_pause_preflight_ok": step_ok("1_PRE_PAUSE_PREFLIGHT"),
            "pause_sent": step_ok("2_PAUSE"),
            "pause_verified": (getattr(hr, "pause_verifier_state", None)
                               == RA.PV_VERIFIED),
            "ownership_neither": step_ok("5_OWNERSHIP"),
            "gate_b_ok": step_ok("6_POST_PAUSE_PRECHECK"),
            "authority_acquire_attempted": (
                getattr(lease, "acquire_attempted", False) is True),
            "authority_acquired": (getattr(lease, "held", False) is True),
            "charge_budget_available": (
                int(counts.get("phase6_charge", 0) or 0)
                < int(limits.get("phase6_charge", 0) or 0)),
            "about_to_run_live_leg": True,
        }

    def _permit_event(kind, attempt_id):
        """permit 進出只記事件，**不得**成為可自動重新 enable 的持久狀態。"""
        state.setdefault("permit_events", []).append(
            {"kind": kind, "attempt_id": attempt_id,
             "at": datetime.datetime.now().isoformat(timespec="seconds")})

    def arm_and_run_handoff():
        """🔴 **唯一**會真的送指令的地方 —— 全部沿用既有元件。"""
        ct = dict(RA.APPROVED_SSH_COMMAND_TIMEOUT_SEC)
        ct.update(RA.STRUCTURAL_CANDIDATE_COMMAND_TIMEOUT_SEC)   # 12.0 候選
        armed_tr = RA.SshTransport(
            timeouts=RA.SshTimeouts(
                connect_timeout_sec=RA.APPROVED_SSH_CONNECT_TIMEOUT_SEC,
                command_timeouts=ct),
            runner=runner, armed=True, clock=time.monotonic)

        def _send(verb):
            # 🔴 durable pause INTENT 必須**緊接在**實際 send 之前落盤。
            #    Attempt #3 之前寫在進入 HandoffRunner 之前，導致
            #    pre-pause preflight 就中止時仍留下 responsibility=True。
            if verb == "pause":
                hook = state.get("pause_intent_hook")
                if hook is not None:
                    hook()
            r = armed_tr.run(verb)
            # 🔴 liveness 取樣階段由**實際送出的動詞**推進，
            #    不由本層重新判斷現場狀態。
            if verb == "pause":
                state["liveness_phase"] = "post_pause"
            elif verb == "restore":
                state["liveness_phase"] = "post_restore"
            state["verb_counts"][verb] = state["verb_counts"].get(verb, 0) + 1
            holder.setdefault("sends", {})[verb] = {
                "ssh_outcome": r.outcome, "exit_code": r.exit_code,
                "elapsed_sec": r.elapsed_sec}
            return RA.classify_send_outcome(r)

        state["liveness_phase"] = "pre_pause"
        rec = FL.LifecycleRecorder()
        guard = FL.CommandCountGuard()
        remote = HO.RemoteController()
        remote.send_pause_ex = FL.wrap_pause_sender(
            lambda: (_send("pause"), None), rec, guard)
        remote.send_restore_ex = FL.wrap_restore_sender(
            lambda: (_send("restore"), None), rec, guard)
        # 🔴 OLD / NEW / WHY / EVIDENCE（2026-10-01 Attempt #7 postmortem）
        #   OLD : 所有 Attempt 共用 `handoff_journal.jsonl`；
        #         `OnceGuard(journal)` 會把**整個檔案歷史**還原成 attempted，
        #         於是 Attempt #4 的 PAUSE INTENT 讓之後每一次 attempt
        #         在 step 2 被判 ALREADY_ATTEMPTED_MUST_VERIFY_NOT_RESEND。
        #   NEW : 由 `attempt_id` **決定性地**導出 per-attempt journal
        #         （`handoff_journal_<attempt_id>.jsonl`），
        #         並把 attempt_id 寫進每一筆記錄。
        #   WHY : OnceGuard 的 one-shot 語意是「同一 Attempt 內各一次」，
        #         不是「整個 Phase 6 歷史期間只能一次」。
        #   EVIDENCE : Attempt #7（2026-10-01 00:05:46）2_PAUSE FAIL，
        #         實際 SSH pause verb = 0。
        #   🔴 attempt_id 缺失 -> ValueError（Fail Closed），
        #      **不得** fallback 回 legacy 共用 journal。
        #    🔴 `journal_dir` 可注入（offline smoke 用），但**導出邏輯不變** ——
        #       路徑仍由 attempt_id 決定，確保測試驗到的是 production 行為。
        _jdir = ctx.get("journal_dir") or os.path.join(
            HERE, "..", "output", "phase6_scheduled_first_live")
        _jpath = ctx.get("journal_path") or HO.attempt_journal_path(
            _jdir, ctx.get("attempt_id"))
        journal = HO.Journal(path=_jpath, attempt_id=ctx.get("attempt_id"))
        state["journal_path"] = _jpath
        holder["journal_path"] = _jpath
        orch = HO.HandoffOrchestrator(
            remote=remote, observe=_handoff_sample,
            gates=(lambda x: (True, {})),
            journal=journal, mode=HO.MODE_ARMED,
            idle_baseline_kw=CFG.idle_power_lower_kw,
            power_tolerance_kw=0.0)
        permit = FL.FirstLiveDispatchPermit(
            ctx.get("attempt_id") or "UNCONFIGURED", _permit_evidence)
        holder["permit"] = permit
        # 🔴 OLD / NEW / WHY / EVIDENCE（2026-09-24 PART A~E）
        #   OLD : prompt=(lambda _p: PRD.confirmation_phrase(...)) ——
        #         **無條件**回傳正確確認字串，等同 auto-confirm：
        #         與 permit / attempt / authority / GATE B 毫無綁定。
        #   NEW : 由已驗證的 permit 產生 structured authorization 物件；
        #         只有 permit 正處於 `PERMIT_ACTIVE` scope、動作為 canonical
        #         FIRST LIVE action、功率取自 ProductionConfig、且尚未使用過，
        #         才會產生正確字串；其餘回 SCHEDULED_CONFIRMATION_DENIED。
        #   WHY : 裁示 PART D 明文禁止 --force / --yes / AUTO_CONFIRM /
        #         硬編確認字串 / stdin echo；同時不得刪除 confirm_live_leg。
        #   EVIDENCE : Attempt #4 之前的 lambda 完全不看任何授權狀態。
        #   🔴 `confirm_live_leg()` 本身**未被修改**，互動路徑行為不變。
        scheduled_auth = FL.build_scheduled_live_leg_authorization(
            permit, attempt_id=ctx.get("attempt_id"),
            target_date=ctx.get("target_date"),
            window_start=ctx.get("window_start"),
            window_end=ctx.get("window_end"))
        holder["scheduled_auth"] = scheduled_auth
        deadline = _build_deadline()
        holder["deadline"] = deadline
        dispatcher, lease, _g = FL.build_first_live_dispatcher(
            live_leg_runner=(lambda: SVC.run_live_leg_cli(
                FL.FIRST_LIVE_ACTION, bundle,
                prompt=scheduled_auth,
                operator_run=None,
                meter_snapshot_source=meter.get_snapshot,
                report_session_source=(lambda: None), verbose=True)),
            acquire_authority=(lambda: SVC.acquire_ownership(
                role="scheduled-first-live")),
            release_authority=SVC.release_ownership,
            command_guard=guard, recorder=rec,
            revalidator=FL.build_post_handoff_revalidator(
                sample_source=(lambda: _decision_sample(post=True))),
            dispatch_permit=permit, permit_event=_permit_event,
            deadline=deadline)
        holder.update(recorder=rec, guard=guard, lease=lease,
                      dispatcher=dispatcher, orchestrator=orch)
        hrunner = HO.HandoffRunner(
            orch, _handoff_sample, probe, loop_mark_source=loop_mark,
            dispatcher=dispatcher, config=CFG, dispatch_permit=permit,
            # 🔴 canonical 充電閂上限；ctx 可覆寫僅供離線測試注入
            soc_max_pct=(ctx.get("soc_max_pct")
                         if ctx.get("soc_max_pct") is not None
                         else canonical_soc_max_pct(bundle)),
            # ctx 可覆寫僅供離線測試注入（與 soc_max_pct 同一慣例）；
            # production 未注入時仍為真實 time.sleep
            sleeper=(ctx.get("sleeper") or time.sleep),
            deadline=deadline)
        holder["handoff_runner"] = hrunner
        return hrunner.run()

    def observe_post_restore_liveness():
        """
        restore ＋ RestoreVerifier 完成後的 **fresh** canonical observation。

        🔴 OLD / NEW / WHY / EVIDENCE（2026-10-02 Attempt #8 postmortem）
          OLD : `liveness_phase` 在送出 restore 時推進到 `post_restore`，
                但之後**沒有任何人再觀測一次**，於是
                `post_restore_external_liveness` 恆為 None，而
                `external_liveness_final` 仍停在 restore **之前**的值。
          NEW : handoff 結束、收 artifact 之前，若 restore 確實送出過，
                就重新取得一次 canonical external_liveness。
          WHY : 欄位叫 `final`，人會據此判斷「現場有沒有被留在停止狀態」。
          EVIDENCE : Attempt #8 artifact ——
                post_restore_external_liveness = None，
                external_liveness_final = EXTERNAL_NOT_RUNNING @00:08:30，
                但 restore 於 00:08:52 完成且 RestoreVerifier = RV_RESUMED，
                隔日 fresh field probe 為 EXTERNAL_RUNNING。
        🔴 restore 未送出 -> 不做任何額外 field I/O（回 None）。
        🔴 觀測失敗 -> 記為 `EXTERNAL_UNKNOWN`（Fail Closed），
           **不得**沿用 restore 之前的值。
        """
        if state.get("liveness_phase") != "post_restore":
            return None
        byp = state.setdefault("liveness_by_phase", {})
        if byp.get("post_restore") is not None:
            return byp["post_restore"]["verdict"]
        try:
            return _external_liveness()
        except Exception as e:                                 # noqa: BLE001
            rec = {"verdict": RA.EXT_UNKNOWN,
                   "detail": "post-restore observation 失敗：%s"
                             % type(e).__name__,
                   "at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "min_loop_observations": CFG.restore_loop_min_observations,
                   "loop_marks": []}
            state.setdefault("liveness_history", []).append(rec)
            byp["post_restore"] = rec
            state["external_liveness"] = rec
            return RA.EXT_UNKNOWN

    def post_restore_reconciliation():
        """
        restore ＋ RestoreVerifier 之後的 **READ-ONLY** 對帳。

        🔴 OLD / NEW / WHY / EVIDENCE（2026-10-06 Attempt #9 postmortem）
          OLD : CHARGE 已送出、authority 轉 UNKNOWN 時，流程只做
                「不 STOP -> release -> restore -> 結束」，
                沒有任何證據說明 external 是否真的接手、
                PCS 是否仍停在 Phase 6 的 setpoint。
          NEW : RV_RESUMED 之後再做一次 fresh **唯讀** 觀測，
                產生 `RECONCILIATION_ITEMS` 的完整事實。
          WHY : artifact 必須能回答「PCS 現在到底在做什麼」。
          EVIDENCE : Attempt #9 artifact 完全沒有 post-restore 的 PCS 觀測。
        🔴 **絕對不得**在此送出任何 PCS 指令（STOP / CHARGE / DISCHARGE）。
           本函式只呼叫既有的唯讀 reader ／probe ／loop mark。
        🔴 任一項取不到一律記 None（**不得**省略欄位），整體不拋例外。
        """
        rec = {k: None for k in FL.RECONCILIATION_ITEMS}
        rec["at"] = datetime.datetime.now().isoformat(timespec="seconds")
        rec["mode"] = "READ_ONLY_NO_CONTROL"
        rec["commands_sent"] = {"charge": 0, "discharge": 0, "pcs_stop": 0}
        try:
            _lv = state.get("external_liveness") or {}
            rec["external_liveness"] = _lv.get("verdict")
            rec["external_loop_marks"] = list(_lv.get("loop_marks") or ())
        except Exception:                                      # noqa: BLE001
            pass
        try:
            rd = reader()                      # 既有唯讀 ESS reader
            rec["communication_ok"] = rd.get("communication_ok")
            rec["soc_percent"] = rd.get("soc_percent")
            rec["actual_active_power_kw"] = rd.get("actual_active_power_kw")
            rec["pcs_charging_flag"] = rd.get("pcs_charging_flag")
            rec["pcs_discharging_flag"] = rd.get("pcs_discharging_flag")
            rec["pcs_standby_flag"] = rd.get("pcs_standby_flag")
            rec["pcs_fault_flag"] = rd.get("pcs_fault_flag")
            rec["pcs_control_mode_code"] = rd.get("pcs_control_mode_code")
            if rd.get("pcs_charging_flag") is True:
                rec["pcs_state"] = "CHARGING"
            elif rd.get("pcs_discharging_flag") is True:
                rec["pcs_state"] = "DISCHARGING"
            elif rd.get("pcs_standby_flag") is True:
                rec["pcs_state"] = "STANDBY"
            else:
                rec["pcs_state"] = None
        except Exception as e:                                 # noqa: BLE001
            rec["error"] = type(e).__name__
        state["post_restore_reconciliation"] = rec
        return rec

    def collect_artifact(result):
        # 🔴 必須在讀取 liveness 欄位**之前**補上 post-restore fresh 觀測
        observe_post_restore_liveness()
        art = FL.collect_handoff_artifact(
            result, orchestrator=holder.get("orchestrator"),
            dispatcher=holder.get("dispatcher"), lease=holder.get("lease"),
            guard=holder.get("guard"), recorder=holder.get("recorder"),
            final_sample=sample(),
            # 🔴 Attempt #8 postmortem：STAGE A entry gate ＋ ordering trace
            permit=holder.get("permit"),
            # 🔴 EXECUTION DEADLINE evidence（2026-10-02 裁示）
            deadline=holder.get("deadline"),
            # 🔴 POST-RESTORE RECONCILIATION（2026-10-06 裁示，READ ONLY）
            reconciliation=post_restore_reconciliation(),
            # 🔴 canonical external liveness 由 provider 持有，顯式傳入
            external_liveness=(state.get("external_liveness") or {}).get(
                "verdict"))
        d = art.to_dict()
        d["ssh_verb_counts"] = dict(state["verb_counts"])
        p = holder.get("permit")
        d["dispatch_permit"] = (p.as_dict() if p is not None else None)
        sa = holder.get("scheduled_auth")
        d["scheduled_live_leg_authorization"] = (
            sa.as_dict() if sa is not None else None)
        if p is not None:
            d["dispatch_permit"]["events"] = list(
                state.get("permit_events") or ())
        byp = state.get("liveness_by_phase") or {}
        d["pre_pause_external_liveness"] = byp.get("pre_pause")
        d["post_pause_external_liveness"] = byp.get("post_pause")
        d["post_restore_external_liveness"] = byp.get("post_restore")
        d["external_liveness_history"] = list(
            state.get("liveness_history") or ())
        # 🔴 只放非敏感 evidence（不含 password / token / session secret）
        d["hmi_auth_renewal"] = reader.evidence()
        # 🔴 canonical external liveness 三態（behaviour_known 的來源證據）
        # backward compatible：舊欄位保留為最後一筆
        d["external_liveness"] = state.get("external_liveness")
        # 🔴 `final` 必須指向**最後一次真正的 fresh observation**。
        #    若 restore 已送出卻拿不到 post-restore 觀測，
        #    一律降為 EXTERNAL_UNKNOWN —— 不得保留 restore 之前的值而稱為 final。
        _final = state.get("external_liveness")
        if (state.get("liveness_phase") == "post_restore"
                and byp.get("post_restore") is None):
            _final = {"verdict": RA.EXT_UNKNOWN,
                      "detail": "restore 後未能取得 fresh observation",
                      "at": None, "loop_marks": []}
        d["external_liveness_final"] = _final
        return d

    def safety_reset():
        try:
            meter.stop()
        except Exception:                                      # noqa: BLE001
            pass
        return {"dispatch_enabled": HO.DISPATCH_ENABLED,
                "rt_dispatch_enabled": RT.DISPATCH_ENABLED,
                "senders_armed": RA.RemoteSenders().armed,
                "transport_armed": RA.SshTransport().armed,
                "pause_timeout": RA.SshTimeouts().command_timeouts.get("pause"),
                "restore_timeout": RA.SshTimeouts().command_timeouts.get(
                    "restore"),
                "deployed_guard_variant": RA.DEPLOYED_GUARD_VARIANT,
                "first_live": "HOLD"}

    return FieldDependencies(
        now_fn=now_fn, source_state_check=source_state_check,
        safety_preflight=safety_preflight, gate_a=gate_a,
        handoff_readiness=handoff_readiness,
        arm_and_run_handoff=arm_and_run_handoff,
        collect_artifact=collect_artifact, safety_reset=safety_reset,
        host_evidence=ctx.get("host_evidence"), waiter=time.sleep,
        register_pause_intent_hook=register_pause_intent_hook)


def build(config, runtime_context=None):
    """launcher 相容入口。回傳 `ScheduledFirstLiveRunner` 的 kwargs dict。"""
    return build_field_dependencies(config, runtime_context).as_runner_kwargs()


def main(argv=None):
    print("=" * 74)
    print("  Phase 6.10 — FIRST LIVE FIELD DEPENDENCY PROVIDER（離線自述）")
    print("=" * 74)
    print("  PROVIDER_ID            : %s" % PROVIDER_ID)
    print("  required dependencies  : %s" % (list(REQUIRED_DEPENDENCIES),))
    print("  optional dependencies  : %s" % (list(OPTIONAL_DEPENDENCIES),))
    ok, checks = validate_provider_definition()
    print("  validate（純離線）      : ok=%r" % ok)
    for k in VALIDATION_ITEMS:
        print("      [%s] %s" % ("PASS" if checks[k] else "FAIL", k))
    print()
    print("  [!] 本自述只做 validate，**不連 HMI / Meter / SSH**。")
    print("  [!] build_field_dependencies() 只有正式 scheduled run 才會呼叫。")
    print("  [!] 本模組只 wire，不含任何 decision / Safety Gate / executor 邏輯。")
    import importlib as _il
    _a = _il.import_module("phase6_hmi_auth_renewal")
    print("  HMI auth renewal       : bound=%d（沿用 6.9），只認 %r，原地 re-login"
          % (_a.AUTH_RENEWAL_MAX_ATTEMPTS, _a.AUTH_EXPIRED_MARKER))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
