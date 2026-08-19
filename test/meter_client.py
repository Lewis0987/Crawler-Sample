# -*- coding: utf-8 -*-
"""
Phase 6.1 Grid Meter Client — 唯讀
======================================================================
訂閱 6160 的 Socket.IO `update` 事件，將 payload 驗證為契約後轉成 MeterSnapshot，
只輸出「併網點（Grid Meter）」狀態。

⚠️ 唯讀：不控制 PCS / BMS、不切換模式、不建立 Session、不送任何寫入請求。
⚠️ 本模組**不做決策**。分類（PEAK/OFF_PEAK/…）、debounce、充放電判斷屬 Phase 6.2/6.3。

資料來源（Phase 6.0 結案決議）
    正式  http://192.168.1.47:6160        （SOURCE_PRODUCTION）
    稽核  http://192.168.128.234:6160     （SOURCE_SHADOW，不得作為控制來源）

三種 kW 語意嚴格隔離 —— 本模組只處理第一種
    ① Grid Meter（本模組）  + = IMPORT 台電取電 / - = EXPORT 逆送台電
    ② Battery Measurement   + = CHARGE / - = DISCHARGE
    ③ PCS Command           CHARGE → 負 setpoint / DISCHARGE → 正 setpoint
    因此 direction 一律是 IMPORT / EXPORT / NEUTRAL，**永遠不會**是 CHARGE / DISCHARGE。
    嚴禁把本模組的 power_kw 正負號直接當成 PCS setpoint。

只消費 4 個欄位
    meter, meter_state, demand, demand_state

    與 6160 網頁 UI 的對照（僅 CLI 顯示層採用中文標籤，資料欄位名一律不變）
        UI「台電電錶」   = payload.meter  = MeterSnapshot.power_kw
        UI「原始需求量」 = payload.demand = MeterSnapshot.demand_kw
        CLI 兩者固定 2 位小數，並以 _fmt2() 複製網頁的 toFixed(2) 捨入規則，
        顯示結果與畫面逐位相同；MeterSnapshot 內部仍保留完整原始精度。
        JSON 模式（--json）維持英文欄位名與原始 float 精度，不受顯示層影響。
    以下欄位**刻意不取用**（設備身分／物理來源未確認，見 NON_CONTROL_FIELDS）：
    bess_0, bess_1, bess_sum, soc_0, soc_1, soc_avg, wharf
    SOC / PCS / BMS / Fault / Alarm 一律走既有 ESS HMI 192.168.128.110。

Fail Closed 規則（任一不成立即 valid=False）
    meter_state != "ok"                  → 電表不可信
    demand_state == "fault"              → 資料不可信（production 官方旗標）
    本機接收 age > STALE_AFTER_SEC (3s)   → stale
    schema / 型別 / 狀態字彙不符           → 契約破壞
    從未收到資料                           → NO_DATA

新鮮度只用本機時間
    age 由 time.monotonic() 的接收時刻計算。production 的 timestamp / datetime
    是「伺服器讀取時間」，只記錄不參與判定。
    Socket.IO 自身 pingTimeout=20s 太慢，**不以 disconnect 事件作為唯一斷線判據**；
    斷線後 snapshot 不清除，任其 age 增長而自然轉為 stale。

用法（皆唯讀）
    python meter_client.py                    # 連正式來源，持續印 Grid Meter 狀態
    python meter_client.py --duration 30      # 觀察 30 秒後結束
    python meter_client.py --once             # 取得第一筆有效資料後結束
    python meter_client.py --shadow           # 連本機稽核實例
    python meter_client.py --json             # 每筆輸出一行 JSON
    註：--shadow 的本機實例只有 8 個欄位、缺 meter_state / demand_state，
        依契約會被判定為 INVALID_SCHEMA_MISSING —— 這是預期行為，不是 bug。
"""

import sys
import json
import math
import time
import argparse
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import socketio


# ======================================================================
# 設定區
# ======================================================================
SOURCE_PRODUCTION = "GRID_METER_6160"
SOURCE_SHADOW = "GRID_METER_6160_SHADOW"

URL_PRODUCTION = "http://192.168.1.47:6160"
URL_SHADOW = "http://192.168.128.234:6160"

EVENT_NAME = "update"
STALE_AFTER_SEC = 3.0          # 對齊 production 前端 STALE_SECONDS
CONNECT_TIMEOUT_SEC = 8.0
NEUTRAL_BAND_KW = 0.0          # |kW| <= band 視為 NEUTRAL；預設 0，不夾帶任何未確認策略

# 6160 網頁 UI 欄位名稱對照 —— 只用於人類可讀輸出，
# 不影響 Socket.IO payload 欄位（meter / demand）與 MeterSnapshot 欄位（power_kw / demand_kw）。
UI_LABEL_METER = "台電電錶"        # UI「台電電錶」  = payload.meter  = MeterSnapshot.power_kw
UI_LABEL_DEMAND = "原始需求量"      # UI「原始需求量」= payload.demand = MeterSnapshot.demand_kw

# Grid Meter 方向（僅併網點語意）
DIR_IMPORT = "IMPORT"          # meter > 0，台電取電
DIR_EXPORT = "EXPORT"          # meter < 0，逆送台電
DIR_NEUTRAL = "NEUTRAL"        # 併網點平衡
DIR_UNKNOWN = "UNKNOWN"        # 無有效數值

# production 狀態字彙（來源：production scripts.js 常數，與後端 meter_reader 同源）
S_OK = "ok"
S_OFFLINE = "offline"
S_FAULT = "fault"
S_DEGRADED = "degraded"

METER_STATE_VOCAB = frozenset({S_OK, S_OFFLINE, S_FAULT})
DEMAND_STATE_VOCAB = frozenset({S_OK, S_DEGRADED, S_FAULT})

# 契約必要欄位與型別（bool 不算數值，需另外排除）
REQUIRED_NUMERIC = ("meter", "demand")
REQUIRED_STRING = ("meter_state", "demand_state")

# 刻意不取用的欄位 —— 僅供文件與自我檢查，永不進入 MeterSnapshot
NON_CONTROL_FIELDS = frozenset({
    "bess_0", "bess_1", "bess_sum", "soc_0", "soc_1", "soc_avg", "wharf",
    "bess_0_state", "bess_1_state", "bess_sum_state", "wharf_state", "status",
})

# 無效原因
R_OK = "OK"
R_NO_DATA = "NO_DATA"
R_NOT_MAPPING = "INVALID_PAYLOAD_NOT_MAPPING"
R_MISSING = "INVALID_SCHEMA_MISSING"
R_TYPE = "INVALID_SCHEMA_TYPE"
R_NOT_FINITE = "INVALID_VALUE_NOT_FINITE"
R_STATE_VOCAB = "INVALID_STATE_VALUE"
R_METER_STATE = "INVALID_METER_STATE"
R_DEMAND_FAULT = "INVALID_DEMAND_STATE_FAULT"
R_STALE = "STALE"


# ======================================================================
# 資料模型
# ======================================================================
@dataclass(frozen=True)
class MeterSnapshot:
    """
    併網點量測快照。power_kw 一定伴隨 direction，不存在「沒有語意的 kW」。

    received_at                        本機 time.monotonic()，唯一的新鮮度依據
    server_timestamp / server_datetime production 伺服器讀取時間，僅記錄不參與判定
    valid                              False 時 power_kw / direction 不可用於任何決策
    """
    source: str
    power_kw: float
    direction: str
    meter_state: str
    demand_kw: float
    demand_state: str
    received_at: float
    received_wall: str
    server_timestamp: str
    server_datetime: str
    age_sec: float
    stale: bool
    valid: bool
    reason: str
    detail: str = ""

    def as_dict(self):
        """原樣輸出（含 ±inf），供程式內部使用。要序列化請用 as_json_dict()。"""
        return asdict(self)

    def as_json_dict(self):
        """
        JSON 序列化用。標準 JSON 沒有 Infinity / -Infinity / NaN，
        NO_DATA 時無法表示的 received_at(-inf) / age_sec(inf) 一律轉為 None（null）。
        其餘數值**保留原始 float 精度**，不套用 _fmt2 的 2 位格式化。
        """
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = None
        return d

    def __str__(self):
        age = "n/a" if not math.isfinite(self.age_sec) else f"{self.age_sec:.2f}s"
        if not self.valid:
            extra = f" {self.detail}" if self.detail else ""
            return (f"[{self.source}] INVALID reason={self.reason}{extra} "
                    f"age={age}")
        # 欄位標籤與小數位數刻意對齊 6160 網頁 UI，方便與畫面逐項對照
        # （僅顯示層：資料欄位名與原始精度皆不變）
        return (f"[{self.source}] "
                f"{UI_LABEL_METER}={_fmt2(self.power_kw)} kW {self.direction}  "
                f"{UI_LABEL_DEMAND}={_fmt2(self.demand_kw)} kW({self.demand_state})  "
                f"meter_state={self.meter_state}  "
                f"age={age}  VALID")


def _fmt2(v):
    """
    CLI 顯示專用的 2 位小數格式化 —— 與 6160 網頁 UI 的 Number.prototype.toFixed(2) 等價。

    為什麼不用 f"{v:.2f}"
        Python 的 .2f 在「精確二進位中點」採 ties-to-even，JS toFixed 採 ties-away-from-zero，
        兩者在 109.125 / 109.625 / ±0.125 這類值上會差 0.01（已用 node v24 實測確認）。
        Decimal(float) 取的是該 double 的**精確二進位值**，再以 ROUND_HALF_UP
        （即 ties away from zero）量化，結果與 toFixed(2) 逐位相同。

    ⚠️ 僅供人類可讀輸出。格式化後的字串**不得**回流到 valid / stale / direction /
       Phase 6.2 分類 / Decision Engine / PCS Control 任何判斷 ——
       那些一律使用 MeterSnapshot 的原始 float。
    """
    if not _is_number(v) or not math.isfinite(v):
        return "n/a"
    return f"{Decimal(v).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):+}"


def classify_direction(power_kw, neutral_band=NEUTRAL_BAND_KW):
    """Grid Meter 方向：只回 IMPORT / EXPORT / NEUTRAL / UNKNOWN，不回 CHARGE / DISCHARGE。"""
    if not _is_number(power_kw) or not math.isfinite(power_kw):
        return DIR_UNKNOWN
    if power_kw > neutral_band:
        return DIR_IMPORT
    if power_kw < -neutral_band:
        return DIR_EXPORT
    return DIR_NEUTRAL


def _is_number(v):
    """bool 是 int 的子類，必須排除，否則 True 會被當成 1 kW。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ======================================================================
# 契約驗證
# ======================================================================
def validate_payload(payload):
    """
    驗證 production Socket.IO payload 是否符合契約。
    回傳 (ok, reason, detail)。允許有額外欄位（向前相容），但必要欄位缺一不可。
    """
    if not isinstance(payload, dict):
        return False, R_NOT_MAPPING, type(payload).__name__

    missing = [k for k in REQUIRED_NUMERIC + REQUIRED_STRING if k not in payload]
    if missing:
        return False, R_MISSING, ",".join(sorted(missing))

    bad_type = [k for k in REQUIRED_NUMERIC if not _is_number(payload[k])]
    bad_type += [k for k in REQUIRED_STRING if not isinstance(payload[k], str)]
    if bad_type:
        return False, R_TYPE, ",".join(sorted(bad_type))

    not_finite = [k for k in REQUIRED_NUMERIC if not math.isfinite(float(payload[k]))]
    if not_finite:
        return False, R_NOT_FINITE, ",".join(sorted(not_finite))

    if payload["meter_state"] not in METER_STATE_VOCAB:
        return False, R_STATE_VOCAB, f"meter_state={payload['meter_state']!r}"
    if payload["demand_state"] not in DEMAND_STATE_VOCAB:
        return False, R_STATE_VOCAB, f"demand_state={payload['demand_state']!r}"

    return True, R_OK, ""


def evaluate(payload, received_at, now, source=SOURCE_PRODUCTION,
             stale_after=STALE_AFTER_SEC, neutral_band=NEUTRAL_BAND_KW,
             received_wall=None):
    """
    把「已接收的 payload + 接收時刻」在時間點 now 評估成 MeterSnapshot。
    純函式、可離線測試；age 一律由本機 monotonic 時間算出。
    """
    age = max(0.0, now - received_at)
    stale = age > stale_after

    ok, reason, detail = validate_payload(payload)
    if not ok:
        return MeterSnapshot(
            source=source, power_kw=None, direction=DIR_UNKNOWN,
            meter_state=None, demand_kw=None, demand_state=None,
            received_at=received_at, received_wall=received_wall,
            server_timestamp=None, server_datetime=None,
            age_sec=age, stale=stale, valid=False, reason=reason, detail=detail,
        )

    power = float(payload["meter"])
    demand = float(payload["demand"])
    m_state = payload["meter_state"]
    d_state = payload["demand_state"]

    # Fail Closed 判定順序：資料本身不可信 → 再看新鮮度
    if m_state != S_OK:
        valid, reason, detail = False, R_METER_STATE, f"meter_state={m_state}"
    elif d_state == S_FAULT:
        valid, reason, detail = False, R_DEMAND_FAULT, "demand_state=fault"
    elif stale:
        valid, reason, detail = False, R_STALE, f"age={age:.2f}s > {stale_after}s"
    else:
        valid, reason, detail = True, R_OK, ""

    return MeterSnapshot(
        source=source,
        power_kw=power,
        direction=classify_direction(power, neutral_band),
        meter_state=m_state,
        demand_kw=demand,
        demand_state=d_state,
        received_at=received_at,
        received_wall=received_wall,
        server_timestamp=payload.get("timestamp"),
        server_datetime=payload.get("datetime"),
        age_sec=age, stale=stale, valid=valid, reason=reason, detail=detail,
    )


def no_data_snapshot(source=SOURCE_PRODUCTION, reason=R_NO_DATA, detail=None):
    """
    尚未取得任何「有效」資料時的快照 —— 一律 invalid。

    reason 可帶入最近一次的拒收原因：完全收不到（NO_DATA）與「收得到但契約破壞」
    （例如 production 改版導致 INVALID_SCHEMA_MISSING）是兩種嚴重度不同的故障，
    只回 NO_DATA 會讓維運誤判成網路問題。
    """
    if detail is None:
        detail = "尚未收到任何 update 事件"
    return MeterSnapshot(
        source=source, power_kw=None, direction=DIR_UNKNOWN,
        meter_state=None, demand_kw=None, demand_state=None,
        received_at=float("-inf"), received_wall=None,
        server_timestamp=None, server_datetime=None,
        age_sec=float("inf"), stale=True, valid=False,
        reason=reason, detail=detail,
    )


# ======================================================================
# Socket.IO 客戶端
# ======================================================================
class MeterClient:
    """
    唯讀訂閱 6160 的 `update` 事件並維護最新一筆 payload。

    設計要點
      - 只保留最後一筆；不緩衝歷史（歷史屬 6.2 之後的責任）
      - 收到即記錄 monotonic 接收時刻；staleness 於 get_snapshot() 當下計算
      - 斷線不清除既有 payload，讓 age 自然增長成 stale（fail closed）
      - 契約不符的 payload 不覆蓋上一筆有效資料，只累計拒收統計
    """

    def __init__(self, url=URL_PRODUCTION, source=SOURCE_PRODUCTION,
                 stale_after=STALE_AFTER_SEC, neutral_band=NEUTRAL_BAND_KW,
                 on_snapshot=None):
        self.url = url
        self.source = source
        self.stale_after = stale_after
        self.neutral_band = neutral_band
        self._on_snapshot = on_snapshot

        self._lock = threading.Lock()
        self._payload = None
        self._received_at = None
        self._received_wall = None
        self._last_reject = None          # (reason, detail) 最近一次契約拒收

        self.stats = {
            "received": 0, "accepted": 0, "rejected": 0,
            "connects": 0, "disconnects": 0, "connect_errors": 0,
            "reject_reasons": {},
        }

        self._sio = socketio.Client(logger=False, engineio_logger=False)
        self._sio.on(EVENT_NAME, self._handle_update)
        self._sio.on("connect", self._handle_connect)
        self._sio.on("disconnect", self._handle_disconnect)
        self._sio.on("connect_error", self._handle_connect_error)

    # ---- Socket.IO callbacks（於背景執行緒觸發）----
    def _handle_connect(self):
        with self._lock:
            self.stats["connects"] += 1

    def _handle_disconnect(self):
        # 只做統計。斷線不代表資料立刻無效，也不代表資料還新鮮 —— 一律交給 age 判定。
        with self._lock:
            self.stats["disconnects"] += 1

    def _handle_connect_error(self, _data=None):
        with self._lock:
            self.stats["connect_errors"] += 1

    def _handle_update(self, payload):
        now = time.monotonic()
        wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        ok, reason, detail = validate_payload(payload)
        with self._lock:
            self.stats["received"] += 1
            if ok:
                self.stats["accepted"] += 1
                self._payload = payload
                self._received_at = now
                self._received_wall = wall
            else:
                self.stats["rejected"] += 1
                self._last_reject = (reason, detail)
                self.stats["reject_reasons"][reason] = \
                    self.stats["reject_reasons"].get(reason, 0) + 1
        if self._on_snapshot is not None:
            try:
                self._on_snapshot(self.get_snapshot())
            except Exception:                    # callback 不得影響接收迴圈
                pass

    # ---- 對外 ----
    def get_snapshot(self, now=None):
        """回傳「此刻」的 MeterSnapshot；age / stale / valid 皆於呼叫當下重算。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            payload, rec, wall = self._payload, self._received_at, self._received_wall
            rejected, last_reject = self.stats["rejected"], self._last_reject
        if payload is None or rec is None:
            # 從未有過有效資料。若期間有收到但被契約擋下，回報真正的拒收原因，
            # 否則維運會把「production 改版」誤判成「網路不通」。
            if last_reject is not None:
                reason, detail = last_reject
                return no_data_snapshot(
                    self.source, reason=reason,
                    detail=f"{detail}（已拒收 {rejected} 筆，未曾取得有效資料）")
            return no_data_snapshot(self.source)
        return evaluate(payload, rec, now, source=self.source,
                        stale_after=self.stale_after, neutral_band=self.neutral_band,
                        received_wall=wall)

    def get_stats(self):
        with self._lock:
            s = dict(self.stats)
            s["reject_reasons"] = dict(self.stats["reject_reasons"])
        s["connected"] = bool(self._sio.connected)
        return s

    def start(self):
        self._sio.connect(self.url, transports=["websocket", "polling"],
                          wait_timeout=CONNECT_TIMEOUT_SEC)
        return self

    def stop(self):
        try:
            self._sio.disconnect()
        except Exception:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()
        return False


# ======================================================================
# CLI（唯讀觀察）
# ======================================================================
def main():
    p = argparse.ArgumentParser(description="Phase 6.1 Grid Meter Client（唯讀）")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--shadow", action="store_true",
                     help="連本機稽核實例（8 欄位，缺 state → 預期 INVALID_SCHEMA_MISSING）")
    src.add_argument("--url", help="自訂 Socket.IO base URL")
    p.add_argument("--duration", type=float, default=0.0, help="觀察秒數（0=持續到 Ctrl+C）")
    p.add_argument("--once", action="store_true", help="取得第一筆有效快照後結束")
    p.add_argument("--interval", type=float, default=1.0, help="列印間隔秒數（預設 1.0）")
    p.add_argument("--json", action="store_true", help="每筆輸出一行 JSON")
    args = p.parse_args()

    if args.url:
        url, source = args.url, SOURCE_PRODUCTION
    elif args.shadow:
        url, source = URL_SHADOW, SOURCE_SHADOW
    else:
        url, source = URL_PRODUCTION, SOURCE_PRODUCTION

    print("== Phase 6.1 Grid Meter Client（唯讀）==")
    print(f"  來源 : {source}  {url}")
    print(f"  規則 : stale>{STALE_AFTER_SEC}s / meter_state!=ok / demand_state==fault → Fail Closed")
    print("  說明 : direction 為併網點語意（IMPORT/EXPORT），不可作為 PCS setpoint")
    print(f"  對照 : {UI_LABEL_METER}=payload.meter、{UI_LABEL_DEMAND}=payload.demand"
          f"（同 6160 網頁 UI 欄位名；--json 仍輸出英文欄位）\n")

    client = MeterClient(url=url, source=source)
    try:
        client.start()
    except Exception as e:
        print(f"  [FAIL] 連線失敗：{e!r}")
        return 1

    t0 = time.monotonic()
    rc = 0
    st = client.get_stats()
    try:
        while True:
            snap = client.get_snapshot()
            if args.json:
                # allow_nan=False：萬一仍有非有限值殘留，寧可明確報錯也不輸出非法 JSON
                print(json.dumps(snap.as_json_dict(), ensure_ascii=False,
                                 allow_nan=False, default=str))
            else:
                print(f"  {datetime.now().strftime('%H:%M:%S')}  {snap}")
            if args.once and snap.valid:
                break
            if args.duration and (time.monotonic() - t0) >= args.duration:
                break
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\n  （已中止）")
    finally:
        st = client.get_stats()
        client.stop()

    print(f"\n  統計：received={st['received']} accepted={st['accepted']} "
          f"rejected={st['rejected']} connects={st['connects']} "
          f"disconnects={st['disconnects']} connect_errors={st['connect_errors']}")
    if st["reject_reasons"]:
        print(f"  拒收原因：{st['reject_reasons']}")
    if args.once and st["accepted"] == 0:
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
