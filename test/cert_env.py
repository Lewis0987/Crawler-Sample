# -*- coding: utf-8 -*-
"""
認證測試報告 —— 認證開始（Menu 21）環控快照與 Summary「冷氣溫度」「硬體概述」
==========================================================================
只在開始認證時讀一次（不週期監控、Menu 22 不再讀），結果寫入 <cert>/env_start_snapshot.json（只寫一次）；
報告（含 Menu 25 重產）只讀這份快照，不連 HMI。

來源：環控數據頁（environmental-control）同一批 guest 端點（env_data_scraper.ENDPOINTS，GET＋zh-TW）。
設備狀態沿用環控數據頁規則（env_data_scraper 已逐行對齊前端），只另加以下已確認的設備層級聚合：
  消防           消防報警、消防故障任一異常 → 異常
  冷卻循環系統    系統狀態異常、液位低觸發、液位高觸發任一成立 → 異常（系統開關／水泵開啟關閉不算異常）
  UPS            通訊異常，或故障資訊（emergencyStopStatus）依 HMI statusValue 明確為緊急停止 → 異常
  空調            通訊異常，或環控頁顯示的探頭／內風機故障欄位依 HMI statusValue 明確為告警 → 異常
端點讀取失敗（連線失敗／逾時／HTTP 錯誤／API 錯誤碼）＝「讀取失敗」，不是設備異常；
請求成功但 HMI 回傳沒有裝置資料（data 為空）＝依環控頁規則判定（通訊狀態異常）。
"""

from env_data_scraper import ENDPOINTS, LANG_HEADER, _devlist, _flatten_bool, _water_bool

SNAPSHOT_FILE = "env_start_snapshot.json"
SCHEMA = 1
NORMAL, ABNORMAL, READ_FAIL = "正常", "異常", "讀取失敗"
DEVICES = ("急停按鈕", "閉門器", "消防", "多功能傳感器", "冷卻循環系統", "水浸", "SPD防雷電涌保護", "UPS",
           "AC 380 電量儀", "空調")
AIR_FAULT_MARKS = ("frostProbeMalfunction", "condensationTemperatureProbeFault", "cabinetWithinTemperatureProbeFault",
                   "airOutTemperatureProbefault", "humidityProbeFault", "internalFanFault")   # 環控頁空調區塊顯示的故障欄位
AIR_FAULT_MARKS_103 = ("airOutTemperatureProbefault", "internalFanFault")                    # envicoolEdition 103 不顯示
NORMAL_KEY = "type.attr.normal"
FAULT_KEYS = {"frostProbeMalfunction": "type.attr.alarm", "condensationTemperatureProbeFault": "type.attr.alarm",
              "cabinetWithinTemperatureProbeFault": "type.attr.alarm", "airOutTemperatureProbefault": "type.attr.alarm",
              "humidityProbeFault": "type.attr.alarm", "internalFanFault": "type.attr.alarm",
              "emergencyStopStatus": "type.attr.emergencyStop"}
UPS_FAULT_OK_KEY = "type.attr.notInEmergencyStop"


# ================= 讀取（只在 Menu 21 開始認證時呼叫一次）=================
def read_env(client):
    """逐一 GET（zh-TW，保留封包以區分「請求失敗」與「請求成功但 data 為空」）。回傳快照的原始部分。"""
    endpoints, raw = {}, {}
    for name, path in ENDPOINTS.items():
        rec = {"path": path, "ok": False, "error": None, "code": None}
        try:
            body = client.get(path, headers=LANG_HEADER, unwrap_envelope=False)
        except Exception as e:                          # noqa: BLE001
            body, rec["error"] = None, f"{type(e).__name__}: {e}"
        if body is None:
            rec["error"] = rec["error"] or "請求失敗（連線失敗／逾時／HTTP 錯誤／非 JSON）"
        elif isinstance(body, dict) and "code" in body and body.get("code") not in (0, 200):
            rec.update(code=body.get("code"), error=f"API 錯誤碼 {body.get('code')}：{body.get('msg')}")
        else:
            rec.update(ok=True, code=body.get("code") if isinstance(body, dict) else None)
            raw[name] = body.get("data") if isinstance(body, dict) and "code" in body else body
        endpoints[name] = rec
    return {"endpoints": endpoints, "raw": raw}


def build_snapshot(captured, now, cert_id, error=None):
    """read_env 結果（或讀取例外）→ 完整快照 dict（含原始資料、判定結果與依據）。"""
    captured = captured or {"endpoints": {n: {"path": p, "ok": False, "error": error or "未讀取", "code": None}
                                          for n, p in ENDPOINTS.items()}, "raw": {}}
    snap = {"schema": SCHEMA, "trigger": "cert_start", "cert_id": cert_id,
            "captured_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "error": error,
            "endpoints": captured["endpoints"], "raw": captured["raw"]}
    snap.update(evaluate(snap))
    return snap


# ================= 判定（純函式：只用快照內的原始資料，可離線重算）=================
def _ok(snap, name):
    return bool((snap.get("endpoints") or {}).get(name, {}).get("ok"))


def _metric(devs, mark):
    """(metric dict, 裝置 alertFlag)；找不到回 (None, None)。"""
    for dev in devs:
        if isinstance(dev, dict):
            for m in dev.get("metricsDataVoList") or []:
                if isinstance(m, dict) and m.get("mark") == mark:
                    return m, dev.get("alertFlag")
    return None, None


def _status_key(m):
    """依 HMI statusValue（如 '0=type.attr.normal;1=type.attr.alarm;'）把 oldValue 對到語意 key；無法對應回 None。"""
    if not m or not m.get("statusValue"):
        return None
    table = dict(x.split("=", 1) for x in str(m["statusValue"]).split(";") if "=" in x)
    return table.get(str(m.get("oldValue")))


def _tty(snap):
    """ttyS0 → (tf 函式, 依據)；端點讀取失敗回 (None, 依據)。tf 規則同環控頁：整包無裝置 → False；缺該 mark → True。"""
    if not _ok(snap, "ttyS0"):
        return None, {"ttyS0": "讀取失敗"}
    payload = snap["raw"].get("ttyS0")
    flags, has = _flatten_bool({"data": payload})
    devs = _devlist({"data": payload})

    def tf(mark):
        return flags.get(mark, True) if has else False

    def basis(*marks):
        out = {}
        for mk in marks:
            m, alert = _metric(devs, mk)
            out[mk] = {"oldValue": m.get("oldValue"), "alertFlag": alert} if m else ("ttyS0 無裝置資料" if not has else "缺此欄位")
        return out
    return tf, basis


def _comm(snap, name, alert_flag=False):
    """通訊狀態（環控頁）：air／ups／multifunction＝有裝置資料；voltameter＝alertFlag === False。"""
    devs = _devlist({"data": snap["raw"].get(name)})
    if not devs:
        return False, {"通訊": "HMI 回傳沒有裝置資料"}
    if alert_flag:
        a = devs[0].get("alertFlag") if isinstance(devs[0], dict) else None
        return a is False, {"通訊": f"alertFlag={a}"}
    return True, {"通訊": "有裝置資料"}


def evaluate(snap):
    """快照原始資料 → {"aircon": …, "devices": [{name, status, basis}]}（10 個固定設備，順序固定）。"""
    raw = snap.get("raw") or {}
    dev = {}

    tf, tb = _tty(snap)
    for name, mk in (("急停按鈕", "emergencyButton"), ("閉門器", "accessControl"), ("SPD防雷電涌保護", "SPD")):
        dev[name] = (READ_FAIL, tb) if tf is None else (NORMAL if tf(mk) else ABNORMAL, tb(mk))
    if tf is None:
        dev["消防"] = (READ_FAIL, tb)
    else:
        sub = {"消防報警": tf("fireAlarm"), "消防故障": tf("fireMalfunction")}
        dev["消防"] = (NORMAL if all(sub.values()) else ABNORMAL,
                      dict(tb("fireAlarm", "fireMalfunction"), 子狀態={k: NORMAL if v else ABNORMAL for k, v in sub.items()}))

    # 冷卻循環系統：系統狀態（waterPumpStateRead：真值 → 異常）＋液位低／高（觸發 → 異常）
    parts, basis = {}, {}
    if _ok(snap, "pumpState"):
        ps = raw.get("pumpState")
        parts["系統狀態"] = not bool(ps)
        basis["waterPumpStateRead"] = ps
    else:
        basis["waterPumpStateRead"] = "讀取失敗"
    if tf is not None:
        parts["液位低"] = tf("liquidLow")
        parts["液位高"] = tf("liquidHigh")
        basis.update(tb("liquidLow", "liquidHigh"))
    else:
        basis["ttyS0"] = "讀取失敗"
    basis["子狀態"] = {k: (NORMAL if v else ABNORMAL) if k == "系統狀態" else ("未觸發" if v else "觸發")
                    for k, v in parts.items()}
    if not all(parts.values()):
        dev["冷卻循環系統"] = (ABNORMAL, basis)        # 已讀到的部分明確異常
    elif len(parts) < 3:
        dev["冷卻循環系統"] = (READ_FAIL, basis)       # 有端點讀不到 → 無法確認正常
    else:
        dev["冷卻循環系統"] = (NORMAL, basis)

    if _ok(snap, "water"):
        w = _water_bool({"data": raw.get("water")})
        m, _a = _metric(_devlist({"data": raw.get("water")}), "alarm")
        dev["水浸"] = (NORMAL if w.get("alarm", False) else ABNORMAL, {"alarm": m.get("oldValue") if m else "缺此欄位"})
    else:
        dev["水浸"] = (READ_FAIL, {"water": "讀取失敗"})

    if _ok(snap, "multifunction"):
        c, b = _comm(snap, "multifunction")
        dev["多功能傳感器"] = (NORMAL if c else ABNORMAL, b)
    else:
        dev["多功能傳感器"] = (READ_FAIL, {"multifunction": "讀取失敗"})

    if _ok(snap, "voltameter"):
        c, b = _comm(snap, "voltameter", alert_flag=True)
        dev["AC 380 電量儀"] = (NORMAL if c else ABNORMAL, b)
    else:
        dev["AC 380 電量儀"] = (READ_FAIL, {"voltameter": "讀取失敗"})

    if _ok(snap, "ups"):
        c, b = _comm(snap, "ups")
        m, _a = _metric(_devlist({"data": raw.get("ups")}), "emergencyStopStatus")
        key = _status_key(m)
        fault = key == FAULT_KEYS["emergencyStopStatus"]
        b["故障資訊"] = {"oldValue": m.get("oldValue"), "value": m.get("value"), "statusValue": m.get("statusValue"),
                       "判定": "緊急停止（故障）" if fault else "正常" if key == UPS_FAULT_OK_KEY else "未判定（無法依 HMI 定義對應）"} \
            if m else "缺此欄位"
        dev["UPS"] = (NORMAL if c and not fault else ABNORMAL, b)
    else:
        dev["UPS"] = (READ_FAIL, {"ups": "讀取失敗"})

    if _ok(snap, "air"):
        c, b = _comm(snap, "air")
        devs = _devlist({"data": raw.get("air")})
        ed, _a = _metric(devs, "envicoolEdition")
        ed103 = str((ed or {}).get("value") or "").split(" ")[0] == "103"
        faults, fb = [], {}
        for mk in AIR_FAULT_MARKS:
            if ed103 and mk in AIR_FAULT_MARKS_103:
                continue
            m, _a = _metric(devs, mk)
            if not m:
                continue
            key = _status_key(m)
            fb[mk] = {"oldValue": m.get("oldValue"), "value": m.get("value"),
                      "判定": "故障" if key == FAULT_KEYS[mk] else "正常" if key == NORMAL_KEY else "未判定（無法依 HMI 定義對應）"}
            if key == FAULT_KEYS[mk]:
                faults.append(mk)
        b["故障欄位"] = fb
        dev["空調"] = (NORMAL if c and not faults else ABNORMAL, b)
    else:
        dev["空調"] = (READ_FAIL, {"air": "讀取失敗"})

    return {"aircon": _aircon(snap), "devices": [{"name": n, "status": dev[n][0], "basis": dev[n][1]} for n in DEVICES]}


def _aircon(snap):
    out = {}
    if _ok(snap, "air"):
        devs = _devlist({"data": (snap.get("raw") or {}).get("air")})
        for key, mk in (("cooling", "coolingSetTemperature"), ("heating", "heatingSetTemperature")):
            m, _a = _metric(devs, mk)
            out[key] = {"mark": mk, "oldValue": m.get("oldValue"), "value": m.get("value"), "unit": m.get("unit")} if m else None
    else:
        out = {"cooling": None, "heating": None}
    return out


# ================= Summary 文字 =================
def _temp(x):
    try:
        v = float(str((x or {}).get("value")).strip())
    except (TypeError, ValueError):
        return None
    return f"{int(v) if v.is_integer() else v}°C"


def summary_lines(snap, test_info=None):
    """
    回傳 (冷氣溫度文字, 硬體概述文字)（不含「冷氣溫度：」標籤）。
    有快照 → 依快照（認證開始時）；沒有快照（舊認證）→ 沿用人工輸入並標示（人工輸入），沒有則 N/A。不連 HMI。
    """
    if isinstance(snap, dict) and snap.get("schema"):
        ac = snap.get("aircon") or {}
        c, h = _temp(ac.get("cooling")), _temp(ac.get("heating"))
        aircon = "N/A" if c is None and h is None else f"製冷 {c or 'N/A'}／製熱 {h or 'N/A'}"
        bad = [d["name"] for d in snap.get("devices") or [] if d.get("status") == ABNORMAL]
        hardware = "；".join(f"{n}：{ABNORMAL}" for n in bad) if bad else "N/A"
        return aircon, hardware
    ti = test_info or {}
    aircon = f"{ti['aircon']}（人工輸入）" if str(ti.get("aircon") or "").strip() else "N/A"
    hardware = f"{ti['hardware']}（人工輸入）" if str(ti.get("hardware") or "").strip() else "N/A"
    return aircon, hardware


def failure_note(snap):
    """報告來源說明：起始環控快照有端點讀取失敗時如實註明（讀取失敗不列為設備異常）。"""
    if not isinstance(snap, dict) or not snap.get("schema"):
        return None
    failed = [n for n, e in (snap.get("endpoints") or {}).items() if not e.get("ok")]
    if not failed:
        return None
    return (f"認證開始環控快照（{snap.get('captured_at')}）：{ '、'.join(failed)} 讀取失敗"
            "（相關設備狀態無法取得，不列為異常）")
