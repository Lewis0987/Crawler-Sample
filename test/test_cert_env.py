# -*- coding: utf-8 -*-
"""
認證開始環控快照（Summary 冷氣溫度／硬體概述）測試（離線）
==========================================================
不連設備：假 client 回傳與實機相同結構的環控資料（結構取自 output/env_data.json、device_control_readonly.json）。
驗證：冷氣設定解析、10 個設備正常／異常、消防／冷卻循環／UPS／空調聚合規則、讀取失敗 ≠ 設備異常、
     請求成功但無裝置資料＝通訊異常、Summary 文字、快照只寫一次且可離線重算、Menu 25 不連 HMI、舊認證人工值相容。
執行：python test_cert_env.py
"""

import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from openpyxl import load_workbook

import cert_env as ENV
import cert_requests as REQ
from cert_runtime import CertRuntime, _read_jsonl
from env_data_scraper import ENDPOINTS

_results = []


def check(label, ok):
    _results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


T0 = datetime(2026, 11, 3, 9, 0, 0)


def m(mark, old, value=None, status=None, unit=None):
    return {"mark": mark, "oldValue": str(old), "value": value if value is not None else str(old), "unit": unit,
            "statusValue": status}


def tty_dev(mark, metrics, alert=False):
    return {"mark": mark, "alertFlag": alert, "metricsDataVoList": metrics}


TRIG = "0=type.attr.trigger;1=type.attr.notTriggered"
NOR = "0=type.attr.normal;1=type.attr.alarm;"


def good_data():
    """10 個設備全部正常的環控資料（data 部分；結構同實機）。"""
    return {
        "ttyS0": [tty_dev("waterPump", [m("waterPumpOn", 0, status=TRIG)]),            # 水泵關閉（不是異常）
                  tty_dev("liquid", [m("liquidHigh", 1, status=TRIG)]),
                  tty_dev("emergencyButton", [m("emergencyButton", 1, status=TRIG)]),
                  tty_dev("accessControl", [m("accessControl", 1, status=TRIG)]),
                  tty_dev("liquid", [m("liquidLow", 1, status=TRIG)]),
                  tty_dev("fireFighting", [m("fireAlarm", 1, status=TRIG), m("fireMalfunction", 1, status=TRIG)]),
                  tty_dev("SPD", [m("SPD", 1, status=TRIG)])],
        "water": [tty_dev("water", [m("alarm", 0, "正常", "0=type.attr.normal;1=type.attr.waterAlarm")])],
        "air": [tty_dev("air", [m("coolingSetTemperature", 15, unit="℃"), m("heatingSetTemperature", 14, unit="℃"),
                                m("workingMode", 4, "製冷", "0=type.attr.standby;4=type.attr.refrigeration;"),
                                m("frostProbeMalfunction", 0, "正常", NOR),
                                m("condensationTemperatureProbeFault", 0, "正常", NOR),
                                m("cabinetWithinTemperatureProbeFault", 0, "正常", NOR),
                                m("humidityProbeFault", 0, "正常", NOR),
                                m("cabinetTemperature", 45, unit="℃"),                  # 數值：不參與判定
                                m("cabinetWithinTemperatureHigh", 1, "告警", NOR)])],    # 環控頁未顯示 → 不參與
        "ups": [tty_dev("ups", [m("batteryCapacity", 3, unit="%"),                      # 數值：不參與判定
                                m("emergencyStopStatus", 0, "未處於緊急停止",
                                  "0=type.attr.notInEmergencyStop;1=type.attr.emergencyStop;")])],
        "voltameter": [tty_dev("voltameter", [m("phaseVoltageAB", "-")], alert=False)],
        "multifunction": [tty_dev("multifunction", [m("temperature", "-"), m("h2", 999)])],
        "pumpState": False,
        "pumpSwitch": False,                                                             # 系統開關關閉（不是異常）
    }


class FakeClient:
    """回傳 {code, data}；fail＝請求失敗（None）的端點名；errcode＝API 錯誤碼的端點名；calls 計數。"""

    def __init__(self, data, fail=(), errcode=()):
        self.data, self.fail, self.errcode, self.calls = data, set(fail), set(errcode), []

    def get(self, path, params=None, unwrap_envelope=True, headers=None):
        self.calls.append(path)
        name = next(n for n, p in ENDPOINTS.items() if p == path)
        assert unwrap_envelope is False and headers == {"Accept-Language": "zh-TW"}
        if name in self.fail:
            return None
        if name in self.errcode:
            return {"code": 401, "msg": "unauthorized", "data": None}
        return {"code": 200, "msg": "", "data": copy.deepcopy(self.data.get(name))}


def snap_of(data, **kw):
    return ENV.build_snapshot(ENV.read_env(FakeClient(data, **kw)), T0, "CERT_T")


def status(snap):
    return {d["name"]: d["status"] for d in snap["devices"]}


def edit(data, ep, mark, **kv):
    for dev in data[ep]:
        for mm in dev["metricsDataVoList"]:
            if mm["mark"] == mark:
                mm.update({k: str(v) if k == "oldValue" else v for k, v in kv.items()})
    return data


TMP = tempfile.mkdtemp(prefix="cert_env_")
try:
    print("\n== 冷氣設定與 10 個設備 ==")
    s = snap_of(good_data())
    ac, hw = ENV.summary_lines(s)
    check(f"1 冷氣：製冷／製熱解析（{ac}）；原始值與單位保存", ac == "製冷 15°C／製熱 14°C"
          and s["aircon"]["cooling"] == {"mark": "coolingSetTemperature", "oldValue": "15", "value": "15", "unit": "℃"}
          and s["aircon"]["heating"]["oldValue"] == "14")
    check("2 全部正常：10 個固定設備（固定順序）皆正常", [d["name"] for d in s["devices"]] == list(ENV.DEVICES)
          and set(status(s).values()) == {"正常"})
    check(f"16 全部正常 → 硬體概述 N/A（{hw}）；不列任何正常設備", hw == "N/A")
    check("8／9 水泵關閉、冷卻系統開關關閉、空調製冷模式、數值欄（溫度／電池容量／H2）都不算異常",
          status(s)["冷卻循環系統"] == "正常" and status(s)["空調"] == "正常" and status(s)["UPS"] == "正常")

    bad = good_data()
    for mk in ("emergencyButton", "accessControl", "SPD", "fireMalfunction", "liquidLow"):
        edit(bad, "ttyS0", mk, oldValue=0)
    edit(bad, "water", "alarm", oldValue=1)
    bad.update(pumpState=True, multifunction=None, ups=None, air=None)
    bad["voltameter"][0]["alertFlag"] = True
    sb = snap_of(bad)
    check("3 10 個設備異常：每個都判為異常", set(status(sb).values()) == {"異常"})
    check(f"17 多個異常 → 只列異常設備、固定順序、只有「名稱：異常」：{ENV.summary_lines(sb)[1][:40]}…",
          ENV.summary_lines(sb)[1] == "；".join(f"{n}：異常" for n in ENV.DEVICES))

    print("\n== 聚合規則 ==")
    for mk in ("fireAlarm", "fireMalfunction"):
        d = edit(good_data(), "ttyS0", mk, oldValue=0)
        st = status(snap_of(d))
        check(f"4 消防：只有 {mk} 異常 → 消防：異常；Summary「消防：異常」不列子狀態",
              st["消防"] == "異常" and ENV.summary_lines(snap_of(d))[1] == "消防：異常")
    check("5 冷卻循環：系統狀態異常（waterPumpStateRead 為真）→ 異常",
          status(snap_of(dict(good_data(), pumpState=True)))["冷卻循環系統"] == "異常")
    check("6 液位低觸發 → 冷卻循環系統異常", status(snap_of(edit(good_data(), "ttyS0", "liquidLow", oldValue=0)))["冷卻循環系統"] == "異常")
    check("7 液位高觸發 → 冷卻循環系統異常", status(snap_of(edit(good_data(), "ttyS0", "liquidHigh", oldValue=0)))["冷卻循環系統"] == "異常")
    d = edit(good_data(), "ttyS0", "waterPumpOn", oldValue=1)
    check("8b 水泵開啟／關閉都不影響（開啟亦正常）", status(snap_of(d))["冷卻循環系統"] == "正常"
          and status(snap_of(dict(good_data(), pumpSwitch=True)))["冷卻循環系統"] == "正常")
    check("10 UPS 通訊異常（請求成功但沒有裝置資料）→ 異常", status(snap_of(dict(good_data(), ups=None)))["UPS"] == "異常")
    s_es = snap_of(edit(good_data(), "ups", "emergencyStopStatus", oldValue=1, value="緊急停止"))
    check("10b UPS 故障資訊依 HMI statusValue 明確為緊急停止 → 異常",
          status(s_es)["UPS"] == "異常" and s_es["devices"][7]["basis"]["故障資訊"]["判定"] == "緊急停止（故障）")
    s_unk = snap_of(edit(good_data(), "ups", "emergencyStopStatus", oldValue=7, value="?"))
    check("10c UPS 故障資訊為 HMI 未定義的值 → 不自行判異常（保存原始值、標示未判定）",
          status(s_unk)["UPS"] == "正常" and "未判定" in s_unk["devices"][7]["basis"]["故障資訊"]["判定"]
          and s_unk["devices"][7]["basis"]["故障資訊"]["oldValue"] == "7")
    check("11 空調通訊異常 → 異常（冷氣溫度同時 N/A）", status(snap_of(dict(good_data(), air=None)))["空調"] == "異常"
          and ENV.summary_lines(snap_of(dict(good_data(), air=None)))[0] == "N/A")
    s_af = snap_of(edit(good_data(), "air", "humidityProbeFault", oldValue=1, value="告警"))
    check("12 空調環控頁故障欄位（溼度探頭）依 HMI 定義為告警 → 空調異常；依據保存",
          status(s_af)["空調"] == "異常" and s_af["devices"][9]["basis"]["故障欄位"]["humidityProbeFault"]["判定"] == "故障")
    d = good_data()
    d["air"][0]["metricsDataVoList"] += [m("envicoolEdition", 103, "103"), m("internalFanFault", 1, "告警", NOR)]
    check("12b envicoolEdition 103：環控頁不顯示的內風機故障不參與（仍正常）", status(snap_of(d))["空調"] == "正常")
    d = edit(good_data(), "air", "workingMode", oldValue=0, value="待機")
    check("13 空調待機／製冷（運轉模式）不算異常；環控頁未顯示的 cabinetWithinTemperatureHigh 告警也不參與",
          status(snap_of(d))["空調"] == "正常" and status(snap_of(good_data()))["空調"] == "正常")
    check("AC 380：alertFlag=True → 異常；false → 正常（電壓 '-' 不參與）",
          status(snap_of(bad))["AC 380 電量儀"] == "異常" and status(s)["AC 380 電量儀"] == "正常")

    print("\n== 讀取失敗 ≠ 設備異常 ==")
    s_tf = snap_of(good_data(), fail={"ttyS0"})
    st = status(s_tf)
    check("14 ttyS0 請求失敗 → 急停／閉門／消防／SPD／冷卻循環為「讀取失敗」（不是異常），Summary 不列；快照記錄失敗端點",
          all(st[n] == "讀取失敗" for n in ("急停按鈕", "閉門器", "消防", "SPD防雷電涌保護", "冷卻循環系統"))
          and ENV.summary_lines(s_tf)[1] == "N/A" and s_tf["endpoints"]["ttyS0"]["ok"] is False
          and s_tf["endpoints"]["ttyS0"]["error"])
    s_ec = snap_of(good_data(), errcode={"multifunction"})
    check("14b API 錯誤碼（401）也是讀取失敗，不是通訊異常", status(s_ec)["多功能傳感器"] == "讀取失敗"
          and "401" in s_ec["endpoints"]["multifunction"]["error"])
    s_all = snap_of(good_data(), fail=set(ENDPOINTS))
    check("14c 全部端點失敗 → 冷氣溫度 N/A、硬體概述 N/A；10 個皆讀取失敗；報告註記失敗端點",
          ENV.summary_lines(s_all) == ("N/A", "N/A") and set(status(s_all).values()) == {"讀取失敗"}
          and "讀取失敗" in ENV.failure_note(s_all))
    s_none = ENV.build_snapshot(None, T0, "CERT_T", "沒有已登入的 client（未讀取）")
    check("14d 沒有 client → 快照照樣保存（全部讀取失敗＋原因），Summary N/A", ENV.summary_lines(s_none) == ("N/A", "N/A")
          and all(not e["ok"] for e in s_none["endpoints"].values()) and s_none["error"])
    s_mix = snap_of(dict(good_data(), pumpState=True), fail={"ttyS0"})
    check("14e 部分失敗但已讀到的部分明確異常 → 仍為異常（冷卻循環系統狀態異常、ttyS0 讀不到）",
          status(s_mix)["冷卻循環系統"] == "異常")
    s_null = snap_of(dict(good_data(), multifunction=None, voltameter=None))
    check("15 請求成功但 HMI 無裝置資料（data=null）→ 多功能傳感器、AC 380 依環控頁判通訊異常",
          status(s_null)["多功能傳感器"] == "異常" and status(s_null)["AC 380 電量儀"] == "異常"
          and s_null["endpoints"]["multifunction"]["ok"] is True)

    print("\n== 快照與報告（Menu 21／22／25）==")
    js = json.loads(json.dumps(sb, ensure_ascii=False))
    check("18 快照 JSON 可序列化；只用快照原始資料離線重算 ＝ 保存的判定（含依據）",
          ENV.evaluate(js)["devices"] == js["devices"] and ENV.evaluate(js)["aircon"] == js["aircon"]
          and js["schema"] == 1 and js["trigger"] == "cert_start" and js["captured_at"] == "2026-11-03T09:00:00")

    class Clock:
        def __init__(self):
            self.t = T0

        def __call__(self):
            return self.t

    def reading():
        return {"soc_percent": 50, "battery_current_a": 0.0, "actual_active_power_kw": 0.0, "rack_max_temperature_c": 30,
                "rack_min_temperature_c": 25, "battery_voltage_v": 1250.0, "rack_cell_max_voltage_v": 3.4,
                "rack_cell_min_voltage_v": 3.38, "_fail": [], "battery_power_status": "已上電", "pcs_fault_flag": False,
                "pcs_charging_flag": False, "pcs_discharging_flag": False}

    root = os.path.join(TMP, "rt")
    clk = Clock()
    client = FakeClient(bad)
    rt = CertRuntime(root, reader=reading, heat_reader=lambda: {"st": 22.0, "pt": {"moduleTemperature": 40}},
                     enable_shots=False, clock=clk, log=lambda *a: None,
                     env_reader=lambda: ENV.read_env(client))
    REQ.new_request(root, "start_cert", {"cabinet_spec": "250KW", "conditions": "Idle"}, now=clk())
    rt.tick()
    rt._env_thread.join(10)
    cid = rt.active_id()
    path = os.path.join(root, cid, ENV.SNAPSHOT_FILE)
    n_calls = len(client.calls)
    check(f"Menu 21：開始後讀一次環控（{n_calls} 個 GET＝{len(ENDPOINTS)} 端點）並寫 env_start_snapshot.json；事件有紀錄",
          os.path.exists(path) and n_calls == len(ENDPOINTS)
          and any(e["type"] == "env_start" for e in _read_jsonl(os.path.join(root, cid, "events.jsonl"))))
    for _ in range(30):
        clk.t += timedelta(seconds=5)
        rt.tick()
    check("取樣期間不再讀環控（不週期監控）", len(client.calls) == n_calls)
    with open(path, encoding="utf-8") as f:
        snap_disk = f.read()
    rq = REQ.new_request(root, "end_cert", cert_id=cid, now=clk())
    rt.tick()
    res = REQ.read_result(root, rq)
    check("Menu 22 不再讀環控、快照只寫一次（內容不變）", len(client.calls) == n_calls
          and open(path, encoding="utf-8").read() == snap_disk and res["status"] == "done")

    def a123(xlsx):
        ws = load_workbook(xlsx)["Summary"]
        return [str(ws.cell(i, 1).value) for i in (1, 2, 3)]
    rows = a123(res["data"]["report"])
    check(f"Summary：{rows[0]}｜{rows[1][:30]}…｜{rows[2]}",
          rows[0] == "冷氣溫度：N/A" and rows[1] == "硬體概述：" + ENV.summary_lines(sb)[1] and rows[2] == "測試條件：Idle")

    def boom():
        raise AssertionError("Menu 25 不得連 HMI")
    rt.env_reader = boom
    client.data = good_data()                                       # 「現在」設備已改變：不得被讀到
    rq = REQ.new_request(root, "regenerate", cert_id=cid, now=clk())
    rt.tick()
    rg = REQ.read_result(root, rq)
    check("19 Menu 25 重產：不連 HMI（讀取器不會被呼叫）、內容與當時快照相同（不受現在設備狀態影響）",
          rg["status"] == "done" and len(client.calls) == n_calls and a123(rg["data"]["report"]) == rows)
    rt2 = CertRuntime(root, reader=None, enable_shots=False, log=lambda *a: None, env_reader=boom)
    ok2, _m, p2 = rt2.generate_report(cid)
    check("19b 服務重啟後（新 Runtime、HMI 無法連線）照樣由快照重建", ok2 and a123(p2) == rows)

    # 20 舊認證（沒有快照）：人工值＋標示；沒有人工值 → N/A；不連 HMI
    old = os.path.join(TMP, "old")
    shutil.copytree(os.path.join(root, cid), os.path.join(old, cid), ignore=shutil.ignore_patterns("reports"))
    os.remove(os.path.join(old, cid, ENV.SNAPSHOT_FILE))
    stf = os.path.join(old, cid, "cert_state.json")
    st_ = REQ.read_json(stf)
    st_["test_info"].update(aircon="21", hardware="test")
    REQ.atomic_write_json(stf, st_)
    ro = CertRuntime(old, reader=None, enable_shots=False, log=lambda *a: None, env_reader=boom)
    ok3, _m, p3 = ro.generate_report(cid)
    st_["test_info"].update(aircon="", hardware="")
    REQ.atomic_write_json(stf, st_)
    ok4, _m, p4 = ro.generate_report(cid)
    check(f"20 舊認證沒有快照：人工值標示（{a123(p3)[0]}｜{a123(p3)[1]}）；沒有人工值 → N/A；不連 HMI",
          ok3 and a123(p3)[:2] == ["冷氣溫度：21（人工輸入）", "硬體概述：test（人工輸入）"]
          and ok4 and a123(p4)[:2] == ["冷氣溫度：N/A", "硬體概述：N/A"])

    # 讀取例外不阻擋開始認證
    root_e = os.path.join(TMP, "err")
    rte = CertRuntime(root_e, reader=reading, enable_shots=False, clock=clk, log=lambda *a: None,
                      env_reader=lambda: (_ for _ in ()).throw(OSError("模擬：HMI 無回應")))
    rq = REQ.new_request(root_e, "start_cert", {}, now=clk())
    rte.tick()
    rte._env_thread.join(10)
    cid_e = rte.active_id()
    se = REQ.read_json(os.path.join(root_e, cid_e, ENV.SNAPSHOT_FILE))
    check("環控讀取例外不阻擋開始認證；快照照樣保存失敗原因，Summary 為 N/A",
          REQ.read_result(root_e, rq)["status"] == "done" and rte.is_active() and "OSError" in se["error"]
          and ENV.summary_lines(se) == ("N/A", "N/A"))
    check("21～25 取樣與分析不受影響：observation 數＝取樣次數、未新增欄位（Raw Data／整體／階段／Cache 由完整 Regression 驗證）",
          len(_read_jsonl(os.path.join(root, cid, "observations.jsonl"))) == 32
          and set(_read_jsonl(os.path.join(root, cid, "observations.jsonl"))[0]) ==
          {"t", "ok", "hmi_ok", "values", "missing", "fail_reason", "notes", "flags"})
finally:
    shutil.rmtree(TMP, ignore_errors=True)

ok = all(_results)
print(f"\n== 認證開始環控快照 {'PASS' if ok else 'FAIL'}（{sum(_results)}/{len(_results)} 檢查通過）==")
sys.exit(0 if ok else 1)
