# -*- coding: utf-8 -*-
"""
phase6_hmi_auth_renewal.py — HMI READ 認證續期（**單一 auth policy 來源**）
======================================================================
本模組**抽出既有 Phase 6.9 `observe_window()` 的 bounded re-auth 行為**，
供 6.9 與 Scheduled FIRST LIVE provider 共用。
🔴 **不建立第二套 auth policy** —— bound 與偵測條件都沿用既有實作。

既有 6.9 契約（`phase6_first_live_runner.observe_window`）
    偵測 : `read_all()` 的 `_fail` 內出現 `_error401`
    上限 : `len(relogins) < 2`  ->  **最多 2 次**
    動作 : 重新登入後**重讀同一筆**；只取唯讀權限，不改變任何控制判定

🔴 **SCOPE：只處理 READ operation 的認證恢復。**
    不重啟 Attempt、不重跑 GATE A、不重 pause、不重 acquire authority、
    不重 dispatch、不重送 CHARGE、不重 restore。
    本模組**結構上沒有**任何 command sender 的引用（由回歸 AST 鎖住）。

🔴 **ERROR CLASSIFICATION**：只對明確的 401 / session expired 續期。
    timeout / connection refused / 500 / bad payload / schema error /
    device fault **一律不觸發重新登入**，維持既有 Fail Closed。

🔴 **CLIENT COHERENCE**：續期採**原地** re-login（`client.login_hmi()` 會更新
    同一個 client 物件的 token），因此所有下游 reader / chain / verifier
    立即共用更新後的憑證 —— 結構上不可能出現
    「new client + old reader」或「old token + new reader」的混合狀態，
    也不會重建 chain 而丟失 PowerClassifier debounce 與 SocBandLatch 狀態。

🔴 **CREDENTIAL SAFETY**：本模組不記錄 password / token / session secret。
    evidence 只有 `auth_renewal_attempted` / `auth_renewal_count` /
    `auth_renewal_result` / `read_recovered`。

用法
    python phase6_hmi_auth_renewal.py      # 離線自述；不連線
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 🔴 沿用既有 6.9 的 bound，**不得**自行加大
AUTH_RENEWAL_MAX_ATTEMPTS = 2

# 🔴 既有 `read_all()` 把 application 失敗記成 "<path>:_error<code>"，
#    只有 401 這個 code 代表 session / token 失效。
AUTH_EXPIRED_MARKER = "_error401"

AUTH_OK = "AUTH_OK"                            # 未發生 401
AUTH_RENEWED = "AUTH_RENEWED"                  # 續期後讀取成功
AUTH_RENEWAL_FAILED = "AUTH_RENEWAL_FAILED"    # 續期本身失敗
AUTH_RENEWAL_EXHAUSTED = "AUTH_RENEWAL_EXHAUSTED"   # 超過 bound
AUTH_RESULTS = (AUTH_OK, AUTH_RENEWED, AUTH_RENEWAL_FAILED,
                AUTH_RENEWAL_EXHAUSTED)

EVIDENCE_FIELDS = ("auth_renewal_attempted", "auth_renewal_count",
                   "auth_renewal_result", "read_recovered")


def is_auth_expired(reading):
    """
    是否為**明確的**認證失效。

    🔴 只認 `_error401`。timeout / refused / 500 / payload / schema /
       device fault 一律 False —— 那些維持既有 Fail Closed，不重新登入。
    """
    if not isinstance(reading, dict):
        return False
    for f in (reading.get("_fail") or ()):
        if isinstance(f, str) and AUTH_EXPIRED_MARKER in f:
            return True
    return False


def build_inplace_relogin(client, username=None):
    """
    回傳零參數 relogin callable —— **原地**更新同一個 client 的 token。

    🔴 不建立新 client、不重建 reader / chain / bundle，
       因此不可能產生混合憑證狀態，也不會重置 classifier / SOC latch。
    """
    if client is None:
        raise ValueError("client 必填")

    def _relogin():
        import charge_discharge_report as CDR
        name = username if username is not None else CDR.USERNAME
        token = client.login_hmi(name)
        return token is not None

    return _relogin


class AuthRenewingReader(object):
    """
    包住既有 reader：讀到明確 401 就**有上限地**續期並重讀同一筆。

    reader()  -> reading dict（既有 `CDR.read_all(client)` 之類）
    relogin() -> bool（True = 續期成功）

    🔴 `max_attempts` 為**本實例累計**上限（對應 6.9 的 per-window 語意），
       不是每次讀取重設。超過即 `AUTH_RENEWAL_EXHAUSTED`，
       回傳最後一次的 reading 讓上層照既有規則 Fail Closed。
    🔴 本類別**只會呼叫 reader 與 relogin**，沒有任何 command 能力。
    """

    def __init__(self, reader, relogin, max_attempts=None, log=None):
        if reader is None or relogin is None:
            raise ValueError("reader / relogin 必填")
        self._reader = reader
        self._relogin = relogin
        self.max_attempts = (AUTH_RENEWAL_MAX_ATTEMPTS if max_attempts is None
                             else int(max_attempts))
        self._log = log or (lambda m: None)
        self.renewal_count = 0
        self.last_result = AUTH_OK
        self.last_read_recovered = None
        self.history = []

    # ------------------------------------------------------------------
    @property
    def exhausted(self):
        return self.renewal_count >= self.max_attempts

    def evidence(self):
        """**非敏感** evidence；不含任何 credential。"""
        return {"auth_renewal_attempted": self.renewal_count > 0,
                "auth_renewal_count": self.renewal_count,
                "auth_renewal_result": self.last_result,
                "read_recovered": self.last_read_recovered,
                "auth_renewal_max": self.max_attempts}

    # ------------------------------------------------------------------
    def __call__(self):
        reading = self._reader()
        if not is_auth_expired(reading):
            self.last_result = AUTH_OK
            self.last_read_recovered = None
            return reading

        if self.exhausted:
            # 🔴 超過既有 bound -> READ UNKNOWN / Fail Closed，不再嘗試
            self.last_result = AUTH_RENEWAL_EXHAUSTED
            self.last_read_recovered = False
            self.history.append({"result": AUTH_RENEWAL_EXHAUSTED,
                                 "count": self.renewal_count})
            self._log("[auth] 續期額度已用盡（%d/%d）-> READ UNKNOWN"
                      % (self.renewal_count, self.max_attempts))
            return reading

        self.renewal_count += 1
        self._log("[auth] 偵測到明確 401 -> 第 %d 次續期（上限 %d）"
                  % (self.renewal_count, self.max_attempts))
        try:
            ok = bool(self._relogin())
        except Exception as e:                                 # noqa: BLE001
            self.last_result = AUTH_RENEWAL_FAILED
            self.last_read_recovered = False
            self.history.append({"result": AUTH_RENEWAL_FAILED,
                                 "count": self.renewal_count,
                                 "error": type(e).__name__})
            return reading
        if not ok:
            self.last_result = AUTH_RENEWAL_FAILED
            self.last_read_recovered = False
            self.history.append({"result": AUTH_RENEWAL_FAILED,
                                 "count": self.renewal_count})
            return reading

        # 🔴 只重讀**這一筆**；不重啟任何流程、不重送任何指令
        reading2 = self._reader()
        recovered = not is_auth_expired(reading2)
        self.last_result = AUTH_RENEWED if recovered else AUTH_RENEWAL_FAILED
        self.last_read_recovered = recovered
        self.history.append({"result": self.last_result,
                             "count": self.renewal_count})
        return reading2


def main(argv=None):
    print("=" * 72)
    print("  Phase 6 — HMI READ 認證續期（單一 auth policy 來源）")
    print("=" * 72)
    print("  bound（沿用 6.9 observe_window）: %d" % AUTH_RENEWAL_MAX_ATTEMPTS)
    print("  偵測條件                        : _fail 內含 %r"
          % AUTH_EXPIRED_MARKER)
    print("  結果分類                        : %s" % (list(AUTH_RESULTS),))
    print("  evidence 欄位                   : %s" % (list(EVIDENCE_FIELDS),))
    print()
    print("  [!] 只恢復 READ；不重啟 Attempt / GATE A，不重送任何 control command。")
    print("  [!] timeout / refused / 500 / payload / schema / fault 一律不續期。")
    print("  [!] 原地 re-login，不重建 client / reader / chain -> 無混合憑證狀態。")
    print("  [!] 不記錄 password / token / session secret。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
