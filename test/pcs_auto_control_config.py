# -*- coding: utf-8 -*-
"""
pcs_auto_control_config.py — Phase 6 自動控制的 Production 參數（D.3-A）
======================================================================
本檔是 **Production Automatic Control 的唯一正式參數來源**。

🔴 全部 production 控制參數預設為 None ＝ **尚未取得正式依據**。
   只要任何一個 required 參數仍為 None，Runtime 一律停在 OBSERVE_ONLY，
   結構上不可能進入 command dispatch。

🔴 **不得**從 phase6_field_measure.py 取值。
   該檔的 FIELD_* 常數是 FIELD TEST ONLY 的量測參數，
   phase6_field_measure.py 永遠只是 reference / validation harness，
   Production Runtime 不得依賴它（連 import 都不行）。

🔴 **不得**從既有監看層常數推導。
   AUTO_NEXT_PLAN_GAP_SEC / AUTO_HOLD_* / AUTO_COOLDOWN_SEC / SAMPLE_INTERVAL_SEC
   全部是「報告監看」語意，與設備控制無關，不得挪用。

🔴 readback_timeout_sec / readback_poll_interval_sec 雖已完成實機驗證並裁定 FINAL，
   但**在 D.3-E Executor Wiring 之前不得填入**。本檔維持 None。

參數語意（刻意逐一標註，避免日後混用）
    charge_power_kw / discharge_power_kw
        Decision Policy 的固定功率。與 Safety Gate 的 max_power_kw 是不同東西。
    max_power_kw
        Safety Gate 的設備額定上限（硬限制）。
    authority_ttl_sec / authority_power_tolerance_kw
        Control Authority 判定「這台設備是不是 Phase 6 自己在控」的門檻。
    min_switch_interval_sec
        Layer 2 **時間**互鎖。與 Layer 1 的 Direction-Reversal **State** Interlock
        是兩件事，不得合併判定。目前無正式依據 → None（規則未啟用）。
    decision_interval_sec
        決策迴圈週期。**不是** readback_poll_interval_sec，兩者語意不同。
    meter_stale_grace_sec
        放電中失去 Grid Meter 後、進入 controlled STOP 流程前的寬限。
        政策方向已裁示（分方向處理），但門檻數值 UNDECIDED / UNVALIDATED。

用法（完全離線）
    python pcs_auto_control_config.py --show
"""

import math
import argparse
from dataclasses import dataclass, asdict, fields

# ======================================================================
# Mutex 名稱前綴
# ======================================================================
# 🔴 必須與 charge_discharge_report_config.MONITOR_MUTEX_PREFIX **不同**。
#    Report Monitor Ownership（誰可以寫報告檔）
#      ≠ PCS Control Ownership（哪個行程可以驅動自動控制）
#    兩者共用同一把鎖會讓「報告服務 crash」等同「控制服務失去所有權」，
#    也會讓報告的檔案鎖被誤當成設備的控制權 —— 語意完全不同，不得共用。
CONTROL_MUTEX_PREFIX = "Global\\ESS_PcsAutoControl_Owner_v1_"

# 稽核輸出子目錄（相對於 output root）。D.3-A 不寫任何 LastControl。
CONTROL_OUTPUT_SUBDIR = "pcs_auto_control"

SERVICE_NAME = "PcsAutoControlService"


def _is_number(v):
    """bool 是 int 的子類，必須排除。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class AutoControlConfig:
    """
    Production 自動控制參數。**全部預設 None＝未取得正式依據**。

    ⚠️ 任何欄位都不得以「暫時先填一個合理值」的方式設定。
       未經實機驗證或廠商依據的數值，一律維持 None，讓 Runtime fail closed。
    """

    # ---- Decision Policy ----
    # 🟢 **FINAL（Phase D.5-C 裁示）** —— Production 第一版營運功率。
    #    依據：5 kW 已完成實機 CHARGE ×2、DISCHARGE ×2，方向、穩態功率、
    #    ReadBack、STOP 流程皆有實證。目標是先建立**保守**的 Go-Live baseline。
    #    🔴 現場曾觀察到的約 80 kW **不是**依據 —— 那是無 LastControl 的
    #       OBSERVED EXTERNAL / FIELD OPERATION，代表「別人在動這台設備」。
    #    ⚠️ 要提高功率必須另行變更並重新驗證（含 tolerance 重新評估）。
    charge_power_kw: float = 5.0
    discharge_power_kw: float = 5.0

    # ---- Safety Gate ----
    # 🟢 **FINAL** —— Safety Gate 對「即將送出的交流有功指令」的上限。
    #
    #    🔴 語意：**HMI-authorized AC active-power production command upper bound**
    #       **不是** "PCS rated power = 150 kW"。這兩者是不同的東西，不得互相改寫。
    #
    #    直接來源：本機 HMI「設備控制 → 手動模式 → 併網 → 交流有功」
    #             所示的有功功率設定範圍 **0 ~ 150 kW**，
    #             並由 device_control_operator.PCS_CONTROL_MODES["ac_active"]["max"]
    #             在送出前強制 abs(activePowerSetPoint) <= 150（第二層防線）。
    #
    #    設備能力級別的交叉佐證（**僅作上界佐證，不是本值的來源**）：
    #      Sinexcel PWS1-160M-H-EX/NA User Manual V1.3_7.0
    #      欄位 `Nominal Power` = **160 kVA**（視在功率）
    #      ⚠️ 官方欄位單位是 **kVA**，不是 kW；不得寫成 "Rated Active Power 160 kW"。
    #      實測 DC 匯流排 886.6~930.5 V 落在該系列範圍內，排除舊 50K~250K 系列。
    #
    #    ⚠️ 已知且已記錄的落差（DOCUMENTED / NON-BLOCKING）：
    #      本機銘牌 400 V / 60 Hz；公開手冊 EX=400 V/50 Hz、NA=480 V/60 Hz。
    #      → MODEL_FAMILY_CONFIRMED / EXACT_VARIANT_UNRESOLVED。
    #      本值不由 EX/NA 額定推導，而由本機 HMI command range 決定，故不受此影響。
    #
    #    🔴 150 kW **不是**操作目標。Production 正常指令仍為 ±5 kW；
    #       這只是一道上限閘，不得據以提高 charge/discharge 功率或安排實機測試。
    max_power_kw: float = 150.0
    # 🔵 min_switch_interval_sec：**Layer 2 DEFERRED**（D.5-A 裁示，方案 B）。
    #    目前**沒有** CHARGE→DISCHARGE / DISCHARGE→CHARGE 反轉最小安全間隔的
    #    實機依據，因此明確維持未配置 —— 這是**刻意的決定**，不是遺漏。
    #    🔴 不得為了「啟用 Layer 2」而填入 75 s 或任何看起來合理的數值。
    #    方向安全完全由 Layer 1（狀態互鎖）保證：運轉中不得直接反向，
    #    必須先送 STOP 並確認進入閒置，才由新的 decision cycle 決定方向。
    min_switch_interval_sec: float = None

    # ---- Control Authority ----
    # 🟢 **FINAL（D.5-C）** —— LastControl 這份擁有權證據可被相信多久。
    #    自 `LastControl.verified_at_monotonic` 起算；到期 → AUTH_UNKNOWN /
    #    CA_EXPIRED → Fail Closed。
    #    推導：需涵蓋「一次決策週期 + 最壞回讀時間 + 餘裕」
    #          = decision_interval(30) + readback_timeout(75) 後取約 1.7× 餘裕。
    #    實機佐證：重評時 age 為 24.7 / 28.8 s，遠在範圍內。
    #    🔴 到期**不得**因 PCS 狀態看起來吻合而自動續期 —— 狀態吻合從來
    #       就不是充分條件。真正 reboot（boot_id 改變）後亦不得沿用舊 ownership。
    authority_ttl_sec: float = 180.0
    # 🟢 **FINAL（D.5-C）**，但**綁定目前的 production target = 5 kW**。
    #    公式 max(1.0 kW, 0.25 × |target|)，在 target=5.0 時得 1.25 kW。
    #    依據：4 段穩態實測誤差 0.30/0.40/0.40/0.40 kW（相對 6~8%），無 outlier；
    #          相對比例 0.25 與既有 STEADY_REACH_REL 同源（約 3 倍餘裕）。
    #    🔴 **charge/discharge target 一旦改變，本值必須重新評估** ——
    #       固定 1.25 kW 在 5 kW 是 25%，在 80 kW 只剩 1.6%，會過緊。
    #    ⚠️ 本值只處理「**新鮮**觀測與預期功率的合理誤差」。
    #       「觀測是不是指令後的新資料」由 Blocker 13 的時機守則負責，
    #       **不得**用放寬容差去處理 backend refresh 落後。
    authority_power_tolerance_kw: float = 1.25

    # ---- Production Leg Authorization（D.3-D）----
    # 🔴 授權票的有效期。與 authority_ttl_sec 是**不同**的東西：
    #    authority_ttl_sec    判定「LastControl 還能不能證明這台設備是我們在控」
    #    authorization_ttl_sec 判定「這張一次性授權票還能不能被消費」
    #    None ＝ 尚未取得正式依據，**不得**解讀成「永不過期」→ 一律 NOT READY。
    # 🟢 **FINAL（D.5-C）** —— one-shot、dispatch 後立即消費、不可重用、
    #    逾時 Fail Closed。推導自實測單輪最壞延遲：
    #    read_all 2.04s + 指令 POST 3.95s ≈ 6s → 取 10s（約 1.7× 餘裕）。
    #    約束：必須 ≪ authority_ttl_sec(180)，且 < decision_interval_sec(30)，
    #    後者確保上一輪的票不可能活到下一輪。
    authorization_ttl_sec: float = 10.0

    # ---- Runtime ----
    # 🟢 **FINAL（D.5-C）** —— 每多久做一次新的 production 決策。
    #    依據：backend refresh 約 15 s，30 s ≈ 2 個 cycle，確保每次決策都
    #    拿到至少一次新的後端刷新；遠大於電表 3 s 與 ESS 15 s 門檻；
    #    時段邊界為小時級，30 s 解析度足夠。
    #    🔴 與 ReadBack poll(5 s) 語意分離：poll 是**一次指令內**的驗證節奏，
    #       本值是**兩次決策之間**的節奏，兩者不必也不應相同。
    #    🔴 **不是** backend refresh 的 workaround —— refresh 落後由 Blocker 13
    #       的時機守則處理，不靠拉長決策間隔掩蓋。
    decision_interval_sec: float = 30.0
    meter_stale_grace_sec: float = None

    # ---- ReadBack ----
    # 🟢 readback_timeout_sec：**已裁示 FINAL**，Phase D.5-A 依既有裁示寫入。
    #    實機依據（Phase 6.5-G，4 筆完整 leg）：狀態確認耗時
    #    10.9 / 16.0 / 16.3 / 16.6 秒，全部遠低於此上限。
    #    ⚠️ 這是**逾時上限**，不是預期耗時；放大它不會加快任何事，
    #       縮小它會把正常的 backend refresh 誤判成失敗。
    readback_timeout_sec: float = 75.0
    # 🟢 readback_poll_interval_sec：**已裁示 FINAL**（D.5-A）。
    #    實機依據：4/4 次 ReadBack 皆以 5.0 秒輪詢成功。
    readback_poll_interval_sec: float = 5.0
    # 🟢 readback_stability_samples：**FINAL**。單次命中即視為到位；
    #    狀態旗標本身不抖動，抖動的是 AC 功率（見 Blocker 13，另行處理）。
    readback_stability_samples: int = 1

    def __post_init__(self):
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "readback_stability_samples":
                if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                    raise ValueError(f"{f.name} 需為 >=1 的整數：{v!r}")
                continue
            if v is None:
                continue
            if not _is_number(v) or not math.isfinite(v):
                raise ValueError(f"{f.name} 需為 None 或有限數值：{v!r}")
            if f.name == "min_switch_interval_sec":
                if v < 0:
                    raise ValueError(f"{f.name} 需為 None 或非負數：{v!r}")
            elif v <= 0:
                raise ValueError(f"{f.name} 需為 None 或正數：{v!r}")

    # ------------------------------------------------------------------
    # Fail Closed 判定
    # ------------------------------------------------------------------
    def missing_required(self):
        """
        回傳仍為 None 的 required 參數名稱（已排序 tuple）。

        🔴 只要非空 → Runtime 必須停在 OBSERVE_ONLY。
        🔴 「readback 兩項已 FINAL」**不構成**放行理由 —— required 是 all-or-nothing。
        """
        return tuple(sorted(n for n in REQUIRED_FOR_DISPATCH
                            if getattr(self, n, None) is None))

    @property
    def dispatch_ready(self):
        """全部 required 參數皆已設定才為 True。"""
        return not self.missing_required()

    def as_dict(self):
        return asdict(self)


# 🔴 進入 command dispatch 之前，這些參數**全部**必須有正式數值。
#    少一個都不行 —— 不允許「部分就緒就先跑起來」。
REQUIRED_FOR_DISPATCH = (
    "charge_power_kw",
    "discharge_power_kw",
    "max_power_kw",
    "authority_ttl_sec",
    "authority_power_tolerance_kw",
    "decision_interval_sec",
    "authorization_ttl_sec",
    "readback_timeout_sec",
    "readback_poll_interval_sec",
)

# ⚠️ min_switch_interval_sec 刻意**不在** REQUIRED 之內：
#    Layer 2 時間互鎖在取得廠商依據前維持「未配置＝規則未啟用」，
#    這與 Safety Gate 既有語意一致，不得因此擋住整個 runtime。
#    Layer 1 的 State Interlock 已實作且無條件生效，方向安全不依賴 Layer 2。
#
# ⚠️ meter_stale_grace_sec 亦不在 REQUIRED：政策方向已裁示（分方向處理），
#    門檻未定時採最保守解釋（不等待），不需要因此停掉整個 runtime。

DEFAULT_CONTROL_CONFIG = AutoControlConfig()


def main(argv=None):
    ap = argparse.ArgumentParser(description="PCS 自動控制 Production 參數（唯讀）")
    ap.add_argument("--show", action="store_true", help="顯示目前參數與就緒狀態")
    ap.parse_args(argv)

    c = DEFAULT_CONTROL_CONFIG
    print("== PCS Auto Control — Production 參數 ==")
    for k, v in c.as_dict().items():
        mark = "  (required)" if k in REQUIRED_FOR_DISPATCH else ""
        print(f"  {k:32s} = {v}{mark}")
    print()
    print(f"  Mutex 前綴 : {CONTROL_MUTEX_PREFIX}")
    print(f"  dispatch_ready : {c.dispatch_ready}")
    print(f"  缺少的 required 參數 : {list(c.missing_required())}")
    print("\n  ⚠ 任一 required 參數為 None → Runtime 一律 OBSERVE_ONLY，不可能送出控制。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
