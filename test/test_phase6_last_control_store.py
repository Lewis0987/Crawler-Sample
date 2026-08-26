# -*- coding: utf-8 -*-
"""
Phase 6.5-E LastControl Store 驗證 —— 完全離線
======================================================================
用途
    驗證「最後一次經 read-back 驗證成功的 PCS 控制」之持久化、載入、
    以及跨 restart / reboot 的信任判定。
    最重要的斷言：**只有 VERIFY_SUCCESS 才更新；API control_success 不算。**

是否需要設備
    **不需要**。完全離線：不控制 PCS/BMS、不登入 HMI、不連 6160、
    不呼叫 device_control_operator。所有檔案 I/O 一律在 tempfile 暫存目錄，
    **絕不寫入正式 output/**。

涵蓋範圍
    A. Model         action 值域、power 型別、非有限值、schema_version
    B. Eligibility   只有 VERIFY_SUCCESS 可寫
    C. Persistence   write→load 一致、精度、stop power=None、UTF-8、atomic
    D. Bad file      缺檔／空檔／壞 JSON／缺欄位／型別錯／未知 action／NaN／schema 不支援
    E. Time / trust  同 process、restart、reboot、boot_id 未知、wall clock 倒退
    F. Isolation     無控制路徑、無網路、不碰正式 output

用法
    python test_phase6_last_control_store.py          # exit 0 = PASS
"""
import os
import sys
import ast
import json
import math
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import last_control_store as LC                        # noqa: E402

_MODULES_AFTER_LC = set(sys.modules)

import pcs_control_integration as PCI                  # noqa: E402

RESULTS = []


def check(label, ok):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


class FakeVerified:
    """VerifiedControlResult 替身。"""

    def __init__(self, action="charge", power=30.0, eligible=True,
                 outcome="VERIFY_SUCCESS", observed="CHARGING", obs_power=-29.9,
                 reason="TARGET_STATE_REACHED"):
        self.control_action = action
        self.target_power_kw = power
        self.lastcontrol_eligible = eligible
        self.outcome = outcome
        self.reason = reason
        self.readback_result = type("RB", (), {
            "observed_state": observed, "actual_active_power_kw": obs_power})()


class Env:
    """暫存目錄 + 可控 boot/clock/wall 的測試環境。"""

    def __init__(self, boot="boot-A", t=1000.0, wall="2026-08-20 12:00:00"):
        self.td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.td.name, "phase6_last_control.json")
        self.boot = boot
        self.t = t
        self.wall = wall

    def store(self, boot=None):
        b = self.boot if boot is None else boot
        return LC.LastControlStore(self.path, boot_id_provider=lambda: b,
                                   clock=lambda: self.t, wall_clock=lambda: self.wall)

    def store_no_boot(self):
        """boot_id 取不到（provider 回 None）的情境 —— 需顯式注入，因為預設已是 production 來源。"""
        return LC.LastControlStore(self.path, boot_id_provider=lambda: None,
                                   clock=lambda: self.t, wall_clock=lambda: self.wall)

    def store_default_boot(self):
        """使用 production 預設 boot_id_provider。"""
        return LC.LastControlStore(self.path, clock=lambda: self.t,
                                   wall_clock=lambda: self.wall)

    def write_raw(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def close(self):
        self.td.cleanup()


def main():
    print("== Phase 6.5-E LastControl Store 驗證（完全離線）==\n")

    # ---------------- A. Model ----------------
    print("A. Model")
    check("action 值域為 charge/discharge/stop（不含 none）",
          LC.VALID_ACTIONS == {"charge", "discharge", "stop"} and "none" not in LC.VALID_ACTIONS)
    check("★ 與 6.5-A 的控制動作值域一致（drift 防護）",
          LC.VALID_ACTIONS == PCI.CONTROL_ACTIONS)
    check("schema_version = 1", LC.SCHEMA_VERSION == 1)

    e = Env()
    st = e.store()
    for act, pw in (("charge", 30.0), ("discharge", 40.0), ("stop", None)):
        u = st.update_from_verified_result(
            FakeVerified(act, pw, observed={"charge": "CHARGING", "discharge": "DISCHARGING",
                                            "stop": "STANDBY"}[act]))
        check(f"{act} 可寫入（power={pw}）", u.updated and u.record.action == act)
    check("★ stop 的 target_power_kw 強制為 None",
          st.update_from_verified_result(
              FakeVerified("stop", 99.0, observed="STANDBY")).record.target_power_kw is None)
    check("none 不可記錄",
          not st.update_from_verified_result(FakeVerified("none", 30.0)).updated)
    for bad, label in ((None, "None"), (0, "0"), (-5, "負值"),
                       (float("nan"), "NaN"), (float("inf"), "inf"), (True, "bool")):
        check(f"charge 的 power={label} → 不寫入",
              not st.update_from_verified_result(FakeVerified("charge", bad)).updated)
    e.close()

    # ---------------- B. Eligibility ----------------
    print("\nB. 更新資格（只有 VERIFY_SUCCESS）")
    e = Env()
    st = e.store()
    u = st.update_from_verified_result(FakeVerified("charge", 30.0, eligible=True))
    check("★ VERIFY_SUCCESS（eligible=True）→ UPDATED", u.updated)
    for oc in ("COMMAND_NOT_SENT", "COMMAND_BLOCKED", "COMMAND_SEND_FAILED",
               "COMMAND_ACCEPTED", "VERIFY_PENDING", "VERIFY_FAILED",
               "VERIFY_TIMEOUT", "CONFIG_NOT_READY"):
        r = st.update_from_verified_result(
            FakeVerified("discharge", 40.0, eligible=False, outcome=oc))
        check(f"{oc} → NOT_UPDATED / NOT_ELIGIBLE",
              r.outcome == LC.UPD_NOT_UPDATED and r.reason == LC.R_NOT_ELIGIBLE)
    check("★ 8 種非成功結果寫入後，檔案仍是最初那筆 charge（未被覆寫）",
          st.load().record.action == "charge")
    check("result=None → NOT_UPDATED",
          not st.update_from_verified_result(None).updated)
    check("★ API control_success=True 不足以更新（eligible 才是唯一依據）",
          not st.update_from_verified_result(
              FakeVerified("charge", 30.0, eligible=False,
                           outcome="COMMAND_ACCEPTED")).updated)
    e.close()

    # ---------------- C. Persistence ----------------
    print("\nC. Persistence")
    e = Env()
    st = e.store()
    st.update_from_verified_result(FakeVerified("charge", 30.123456789, obs_power=-29.87654321))
    lr = LC.LastControlStore(e.path, boot_id_provider=lambda: e.boot).load()
    check("write → load 成功", lr.ok and lr.record.action == "charge")
    check("★ charge power 完整精度保留", lr.record.target_power_kw == 30.123456789)
    check("★ 觀測功率完整精度保留", lr.record.actual_active_power_kw == -29.87654321)
    check("wall / monotonic / pcs_actual_state / boot_id 皆保留",
          lr.record.verified_at_wall == e.wall
          and lr.record.verified_at_monotonic == 1000.0
          and lr.record.pcs_actual_state == "CHARGING"
          and lr.record.boot_id == "boot-A")
    st.update_from_verified_result(FakeVerified("stop", None, observed="STANDBY",
                                                obs_power=0.0))
    lr = LC.LastControlStore(e.path, boot_id_provider=lambda: e.boot).load()
    check("stop 寫入後 power 為 None、狀態為 STANDBY",
          lr.record.target_power_kw is None and lr.record.pcs_actual_state == "STANDBY")
    check("★ STOP 也會寫入 LastControl（它也是一次成功控制）", lr.record.action == "stop")

    st.update_from_verified_result(FakeVerified("discharge", 40.0, observed="DISCHARGING",
                                                reason="目標狀態已達成"))
    raw = open(e.path, encoding="utf-8").read()
    check("UTF-8 中文原樣寫入（未被 escape）", "目標狀態已達成" in raw)
    check("JSON 可被標準 parser 解析且無 NaN/Infinity",
          json.loads(raw)["action"] == "discharge"
          and "NaN" not in raw and "Infinity" not in raw)
    check("★ atomic：寫入後不留 .tmp 殘檔", not os.path.exists(e.path + ".tmp"))
    check("  暫存檔名為 <target>.tmp，最終以 os.replace 就位",
          "os.replace" in open(LC.__file__, encoding="utf-8").read())

    # 寫入失敗不得破壞既有檔案
    good_before = LC.LastControlStore(e.path, boot_id_provider=lambda: e.boot).load()
    blocked_path = os.path.join(e.td.name, "as_dir")
    os.makedirs(blocked_path, exist_ok=True)
    st_bad = LC.LastControlStore(blocked_path, clock=lambda: e.t, wall_clock=lambda: e.wall)
    ub = st_bad.update_from_verified_result(FakeVerified("charge", 30.0))
    check("目標為目錄 → WRITE_FAILED（不 crash）", ub.outcome == LC.UPD_WRITE_FAILED)
    check("  原檔案未受影響",
          LC.LastControlStore(e.path, boot_id_provider=lambda: e.boot).load().record.action
          == good_before.record.action)
    e.close()

    # ---------------- D. Bad file ----------------
    print("\nD. 壞檔案（全部 fail closed，不 crash）")
    e = Env()
    check("檔案不存在 → NOT_FOUND",
          LC.LastControlStore(e.path).load().outcome == LC.LOAD_NOT_FOUND)
    for text, want, label in (
            ("", LC.LOAD_INVALID_JSON, "空檔"),
            ("   \n ", LC.LOAD_INVALID_JSON, "只有空白"),
            ("{not json", LC.LOAD_INVALID_JSON, "壞 JSON"),
            ("[1,2,3]", LC.LOAD_INVALID_SCHEMA, "頂層非 dict"),
            ('{"action":"charge"}', LC.LOAD_INVALID_SCHEMA, "缺 schema_version"),
            ('{"schema_version":"1"}', LC.LOAD_INVALID_SCHEMA, "schema_version 型別錯"),
            ('{"schema_version":99}', LC.LOAD_UNSUPPORTED_SCHEMA, "schema 不支援"),
            ('{"schema_version":1,"action":"charge"}', LC.LOAD_INVALID_FIELD, "缺欄位")):
        e.write_raw(text)
        check(f"{label} → {want}", LC.LastControlStore(e.path).load().outcome == want)

    base = {"schema_version": 1, "action": "charge", "target_power_kw": 30.0,
            "verified_at_wall": "2026-08-20 12:00:00", "verified_at_monotonic": 1000.0,
            "pcs_actual_state": "CHARGING", "actual_active_power_kw": -29.9,
            "reason": "OK", "boot_id": "boot-A"}

    def bad(**over):
        d = dict(base)
        d.update(over)
        e.write_raw(json.dumps(d, ensure_ascii=False))
        return LC.LastControlStore(e.path).load()

    check("未知 action → INVALID_FIELD", bad(action="teleport").outcome == LC.LOAD_INVALID_FIELD)
    check("action=none → INVALID_FIELD", bad(action="none").outcome == LC.LOAD_INVALID_FIELD)
    check("charge 但 power=None → INVALID_FIELD",
          bad(target_power_kw=None).outcome == LC.LOAD_INVALID_FIELD)
    check("stop 但帶 power → INVALID_FIELD",
          bad(action="stop", target_power_kw=5.0).outcome == LC.LOAD_INVALID_FIELD)
    check("monotonic 型別錯 → INVALID_FIELD",
          bad(verified_at_monotonic="x").outcome == LC.LOAD_INVALID_FIELD)
    check("wall 格式錯 → INVALID_FIELD",
          bad(verified_at_wall="2026/08/20").outcome == LC.LOAD_INVALID_FIELD)
    check("pcs_actual_state 空字串 → INVALID_FIELD",
          bad(pcs_actual_state="").outcome == LC.LOAD_INVALID_FIELD)
    check("boot_id 型別錯 → INVALID_FIELD", bad(boot_id=123).outcome == LC.LOAD_INVALID_FIELD)
    check("boot_id=None 合法（production 來源未確認）", bad(boot_id=None).ok)

    for lit, label in (("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "-Infinity")):
        e.write_raw('{"schema_version":1,"action":"charge","target_power_kw":%s,'
                    '"verified_at_wall":"2026-08-20 12:00:00","verified_at_monotonic":1.0,'
                    '"pcs_actual_state":"CHARGING","actual_active_power_kw":null,'
                    '"reason":"OK"}' % lit)
        r = LC.LastControlStore(e.path).load()
        check(f"★ JSON 含 {label} → INVALID_FIELD（不接受非標準 JSON 常數）",
              r.outcome == LC.LOAD_INVALID_FIELD)

    e.write_raw("{not json")
    check("★ 壞檔案時 current() → INVALID，不 crash、不編造歷史",
          LC.LastControlStore(e.path).current().trust == LC.TRUST_INVALID)
    check("★ 壞檔案不得偽造 last_action / elapsed",
          LC.LastControlStore(e.path).current().record is None)
    e.close()

    # ---------------- E. Time / trust ----------------
    print("\nE. 時間與信任（monotonic vs wall clock）")
    e = Env()
    st = e.store()
    st.update_from_verified_result(FakeVerified("charge", 30.0))
    t = st.current()
    check("★ 同 process → TRUSTED_FOR_INTERVAL（source=memory）",
          t.trust == LC.TRUST_FOR_INTERVAL and t.source == "memory"
          and t.reason == LC.R_SAME_PROCESS)

    st2 = e.store()                                   # 新實例＝模擬 service restart
    t2 = st2.current()
    check("★ service restart 且同 boot → TRUSTED_FOR_INTERVAL（source=file）",
          t2.trust == LC.TRUST_FOR_INTERVAL and t2.source == "file"
          and t2.reason == LC.R_SAME_BOOT)

    st3 = e.store(boot="boot-B")                      # 模擬 Windows reboot
    t3 = st3.current()
    check("★ reboot（boot_id 改變）→ HISTORY_ONLY，舊 monotonic 不可信",
          t3.trust == LC.TRUST_HISTORY_ONLY and t3.reason == LC.R_BOOT_ID_MISMATCH)
    check("  HISTORY_ONLY 仍保留紀錄供稽核", t3.record is not None and t3.record.action == "charge")

    st4 = e.store_no_boot()                           # provider 回 None
    t4 = st4.current()
    check("★ boot_id 取不到 → HISTORY_ONLY（不得當可信歷史）",
          t4.trust == LC.TRUST_HISTORY_ONLY and t4.reason == LC.R_BOOT_ID_UNKNOWN)

    # ---- production boot_id_provider（Phase 6.5-G 核可：ntdll BootTime）----
    bid = LC.windows_boot_id()
    check(f"★ windows_boot_id() 可取得且格式為 winboot-<BootTime>：{bid}",
          isinstance(bid, str) and bid.startswith("winboot-")
          and bid[len("winboot-"):].isdigit())
    check("同 process 連讀 3 次一致",
          len({LC.windows_boot_id() for _ in range(3)}) == 1)
    bt = LC.windows_boot_time_100ns()
    check(f"BootTime 為正整數（自 1601 起算的 100ns）：{bt}",
          isinstance(bt, int) and bt > 0)
    import subprocess as _sp
    _out = _sp.run([sys.executable, "-c",
                    "import sys;sys.path.insert(0,r'%s');"
                    "import last_control_store as L;print(L.windows_boot_id())"
                    % os.path.dirname(os.path.abspath(__file__))],
                   capture_output=True, text=True).stdout.strip()
    check(f"★ 跨獨立 Python process 一致（子行程={_out}）", _out == bid)
    check("★ LastControlStore 預設已採用 production boot_id_provider",
          e.store_default_boot()._boot_id_provider is LC.windows_boot_id)

    e2 = Env()
    st_p = e2.store_default_boot()
    st_p.update_from_verified_result(FakeVerified("charge", 30.0))
    check("  以 production provider 寫入的紀錄帶有真實 boot_id",
          st_p.load().record.boot_id == bid)
    check("★ Service restart 模擬（新實例、真實 boot_id）→ TRUSTED_FOR_INTERVAL",
          e2.store_default_boot().current().trust == LC.TRUST_FOR_INTERVAL)
    check("★ reboot 模擬（boot_id 不同）→ HISTORY_ONLY",
          e2.store(boot="winboot-999999999999999999").current().trust
          == LC.TRUST_HISTORY_ONLY)
    check("  未取整到秒（保留原始 100ns 精度）", str(bt).endswith(str(bt % 10000000)))
    e2.close()

    check("★ min_switch_interval=None → CONFIG_NOT_READY（不 fallback 15/60）",
          st.check_interval(None).outcome == LC.IV_CONFIG_NOT_READY)
    e.t = 1020.0
    check("同 boot、elapsed 20s < 30s → WITHIN_INTERVAL",
          st.check_interval(30.0).outcome == LC.IV_WITHIN_INTERVAL)
    e.t = 1030.0
    check("elapsed 30s（=門檻）→ INTERVAL_SATISFIED",
          st.check_interval(30.0).outcome == LC.IV_INTERVAL_SATISFIED)
    check("★ HISTORY_ONLY 不得用於 interval → NO_TRUSTED_HISTORY",
          st3.check_interval(30.0).outcome == LC.IV_NO_TRUSTED_HISTORY)
    check("★ boot_id 未知時亦為 NO_TRUSTED_HISTORY",
          st4.check_interval(30.0).outcome == LC.IV_NO_TRUSTED_HISTORY)
    check("無任何紀錄時 → NO_TRUSTED_HISTORY（不編造 elapsed=inf）",
          (lambda x: x.outcome == LC.IV_NO_TRUSTED_HISTORY and x.elapsed_sec is None)(
              LC.LastControlStore(os.path.join(e.td.name, "nope.json")).check_interval(30.0)))
    check("門檻不合法（負值 / NaN）→ CONFIG_NOT_READY",
          st.check_interval(-1).outcome == LC.IV_CONFIG_NOT_READY
          and st.check_interval(float("nan")).outcome == LC.IV_CONFIG_NOT_READY)

    # wall clock 倒退不得影響 interval（interval 只用 monotonic）
    e.wall = "2020-01-01 00:00:00"                    # 牆鐘倒退數年
    e.t = 2000.0                                      # 寫入時的 monotonic
    st5 = e.store()
    st5.update_from_verified_result(FakeVerified("discharge", 40.0, observed="DISCHARGING"))
    e.t = 2000.0 + 45.0                               # 之後推進 45 秒
    iv = st5.check_interval(30.0)
    check("★ wall clock 倒退不影響 interval（只用 monotonic）",
          iv.outcome == LC.IV_INTERVAL_SATISFIED and abs(iv.elapsed_sec - 45.0) < 1e-9)
    check("  wall 值原樣保存供稽核", st5.current().record.verified_at_wall == "2020-01-01 00:00:00")
    src = open(LC.__file__, encoding="utf-8").read()
    _t = ast.parse(src)
    _fn = [n for n in ast.walk(_t) if isinstance(n, ast.FunctionDef)
           and n.name == "check_interval"][0]
    _names = {n.attr for n in ast.walk(_fn) if isinstance(n, ast.Attribute)}
    check("★ check_interval 未使用 verified_at_wall",
          "verified_at_wall" not in _names and "verified_at_monotonic" in _names)
    e.close()

    # ---------------- F. Isolation ----------------
    print("\nF. Isolation")
    for m in ("device_control_operator", "device_control_menu", "api_client",
              "charge_discharge_report", "report_monitor", "meter_client",
              "requests", "socketio", "urllib", "socket"):
        check(f"runtime 未載入 {m}", m not in _MODULES_AFTER_LC)
    imported = set()
    for n in ast.walk(_t):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    check(f"imports 僅標準庫：{sorted(imported)}",
          imported <= {"os", "sys", "json", "math", "time", "argparse",
                       "dataclasses", "datetime", "tempfile", "ctypes"})
    consts = {n.value for n in ast.walk(_t)
              if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
              and not isinstance(n.value, bool)}
    check(f"未出現 15 / 60 / 90 / 150 / 249 / 229（命中={sorted(consts & {15, 60, 90, 150, 249, 229})}）",
          not (consts & {15, 60, 90, 150, 249, 229}))
    idents = {n.id for n in ast.walk(_t) if isinstance(n, ast.Name)}
    idents |= {n.attr for n in ast.walk(_t) if isinstance(n, ast.Attribute)}
    check("未引用 min_hold_sec / held_since（與 Decision 的抑制無關）",
          not (idents & {"min_hold_sec", "held_since"}))
    check("★ 正式 output 目錄未被本測試寫入",
          not os.path.exists(LC.DEFAULT_STORE_PATH)
          or os.path.getmtime(LC.DEFAULT_STORE_PATH) < _START)
    check("production 預設路徑指向 output/phase6_last_control.json（獨立於既有 action result）",
          LC.DEFAULT_STORE_PATH.endswith(os.path.join("output", "phase6_last_control.json"))
          and "device_control_action_result" not in LC.DEFAULT_STORE_PATH)

    ok_all = all(RESULTS)
    print(f"\n== Phase 6.5-E LastControl Store 驗證 {'PASS' if ok_all else 'FAIL'}"
          f"（{sum(RESULTS)}/{len(RESULTS)} 檢查通過）==")
    return 0 if ok_all else 1


_START = __import__("time").time()

if __name__ == "__main__":
    sys.exit(main())
