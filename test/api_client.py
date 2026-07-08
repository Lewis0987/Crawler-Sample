# -*- coding: utf-8 -*-
"""
共用 HTTP 工具 — 各頁籤 scraper 共用的 baseURL / session / GET 包裝 / 登入
=========================================================================
把重複的連線邏輯集中在這裡，各 scraper 只 import 這支，不再各自複製。

【已驗證】
  - BASE_URL = http://192.168.128.110:8080/admin-api （API 在同主機 8080）
  - 資料概覽等公開資料在 /hmiGuest/unauthorizedAccess/... （免登入 GET）
  - 芋道封包格式 {code, data, msg}；code 0/200 為成功。

【安全原則】
  - 本工具預設只提供 GET（只讀）。
  - login() 為「設備控制頁只讀分析」而提供，只做登入取得 token，
    不封裝任何控制類（POST/PUT/DELETE）請求。
  - 若日後要呼叫寫入類 API，請另外明確實作並加審核，勿放進共用 GET 流程。
"""

import os
import json

import requests


# ============ 共用設定 ============
BASE_URL = "http://192.168.128.110:8080/admin-api"   # 如 8853 有代理可改成 :8853
DEFAULT_TIMEOUT = 8
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
}

DEFAULT_USERNAME = "hmiUser"

_HERE = os.path.dirname(os.path.abspath(__file__))
# 登入密碼設定檔（與本檔同目錄）。實際密文請放這裡或環境變數，勿寫進程式碼。
LOGIN_CONFIG_FILE = os.path.join(_HERE, "login_config.json")
# .env 搜尋位置：test/ 與專案根目錄
_ENV_FILES = [os.path.join(_HERE, ".env"), os.path.join(os.path.dirname(_HERE), ".env")]

_dotenv_loaded = False


def _load_dotenv():
    """
    極簡 .env 讀取器（無第三方相依）：把 KEY=VALUE 載入 os.environ。
    - 只在尚未設定該環境變數時填入（真正的環境變數優先）。
    - 支援 # 註解、去除前後引號；不做變數展開。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    for path in _ENV_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key, val = key.strip(), val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except OSError:
            pass


def mask_secret(s):
    """遮罩密文：只顯示前 5 + ... + 後 5 與長度；短字串只顯示長度。"""
    if not s:
        return "(empty)"
    if len(s) <= 12:
        return f"(len={len(s)})"
    return f"{s[:5]}...{s[-5:]} (len={len(s)})"


def load_login_config():
    """
    讀取登入設定，優先序（高→低）：
      1. 環境變數（含 .env）：LOGIN_PASSWORD_PAYLOAD、HMI_USERNAME / LOGIN_USERNAME
      2. login_config.json（key: password_payload / username）
    回傳 dict：{"username": ..., "password_payload": ..., "source": "env"|"config"|None}
    找不到密文時 password_payload 為 None（呼叫端需自行處理）。

    【背景】前端 LoginForm 會把 password 用 SM2（sm-crypto，hex/04 前綴密文）加密後才送出，
    每次登入密文不同；但實測「同一段密文可重放多次登入」。因此短期方案是把 DevTools 抓到的
    可重放密文放進 .env / 環境變數 / login_config.json，login_hmi() 直接送出，
    不在 Python 端重現加密流程。
    """
    _load_dotenv()

    username = os.environ.get("HMI_USERNAME") or os.environ.get("LOGIN_USERNAME")
    payload = os.environ.get("LOGIN_PASSWORD_PAYLOAD")
    source = "env" if payload else None

    if payload is None and os.path.exists(LOGIN_CONFIG_FILE):
        try:
            with open(LOGIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                username = username or data.get("username")
                fv = data.get("password_payload")
                # 忽略仍是佔位字串的情況（例如 "<...>"）
                if fv and not str(fv).startswith("<"):
                    payload = fv
                    source = "config"
        except (ValueError, OSError):
            pass

    return {
        "username": username or DEFAULT_USERNAME,
        "password_payload": payload,
        "source": source,
    }


def unwrap(payload):
    """
    解開芋道封包：
    - code == 0/200 視為成功，優先回傳 data；無 data 則回整包。
    - 失敗保留 code/msg 方便除錯。
    - 非封包格式（例如純 list/dict）原樣回傳。
    """
    if isinstance(payload, dict) and "code" in payload:
        code = payload.get("code")
        if code in (0, 200):
            return payload.get("data", payload)
        return {"_error": code, "msg": payload.get("msg"), "_raw": payload}
    return payload


class ApiClient:
    """
    薄封裝的 API client：一個 session 重複使用，統一 timeout / headers / 錯誤處理。
    只提供 GET（只讀）與 login（設備控制頁只讀分析用）。
    """

    def __init__(self, base_url=BASE_URL, timeout=DEFAULT_TIMEOUT):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.logged_in = False

    # ---------- 只讀 GET ----------
    def get(self, path, params=None, unwrap_envelope=True):
        """
        GET 一支 API。成功回傳（預設解封包後的）資料；失敗回傳 None 並印警告。
        path 可為 "/hmiGuest/..." 或 "/system/..."（會接在 base_url 後）。
        """
        url = self.base_url + path
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  [警告] GET {path} 失敗：{e}")
            return None
        except ValueError:
            print(f"  [警告] GET {path} 回傳非 JSON")
            return None
        return unwrap(data) if unwrap_envelope else data

    def get_many(self, endpoints):
        """
        批次 GET。endpoints 為 {名稱: path} 或 {名稱: (path, params)}。
        回傳 {名稱: 資料或 None}。單支失敗不中斷。
        """
        result = {}
        for name, spec in endpoints.items():
            path, params = spec if isinstance(spec, tuple) else (spec, None)
            result[name] = self.get(path, params=params)
        return result

    # ---------- 登入（僅供設備控制頁只讀分析）----------
    LOGIN_PATH = "/system/auth/login"

    def login_hmi(self, username=None, password=None, return_debug=False, log=True):
        """
        設備控制頁登入：依前端實際格式送出。
        - POST /system/auth/login（JSON body，Content-Type application/json）
        - body：{"username": ..., "password": ...(密文), "captchaVerification": ""}

        【password 來源（短期可重放方案）】
          前端 LoginForm 會把 password 用 SM2 加密（每次密文不同、hex/04 前綴）才送出，
          我們不在 Python 端重現加密；改為使用 DevTools 抓到的「可重放密文」。
          密碼取值優先序（config 覆蓋傳入參數）：
            1. 環境變數 LOGIN_PASSWORD_PAYLOAD
            2. login_config.json 的 password_payload
            3. 呼叫端傳入的 password（相容 / 測試用）
          → device_control_scraper.py 的 client.login(USERNAME, PASSWORD) 不需修改，
             只要設定好 config/env，就會自動改送可重放密文。

        - 成功時 accessToken 取自 resp["data"]["accessToken"]，並設定 Authorization: Bearer。
        - tenant-id 不需要（VITE_GLOB_APP_TENANT_ENABLE=false）。

        成功回傳 token；失敗回傳 None。return_debug=True 時回傳 (token, debug_dict)。
        """
        url = self.base_url + self.LOGIN_PATH

        def _log(msg):
            if log:
                print(msg)

        cfg = load_login_config()
        resolved_user = username or cfg["username"]
        # config/env 的密文優先；沒設定時才退回傳入的 password
        resolved_pw = cfg["password_payload"] if cfg["password_payload"] else password
        pw_source = cfg["source"] if cfg["password_payload"] else ("arg" if password else None)

        _SOURCE_MSG = {
            "env": "Using LOGIN_PASSWORD_PAYLOAD from env",
            "config": "Using login config password payload",
            "arg": "Using plain password fallback",
        }
        _log(_SOURCE_MSG.get(pw_source, "No password payload found"))

        debug = {"url": url, "method": "POST", "content_type": "application/json",
                 "username": resolved_user, "password_source": pw_source,
                 "password_masked": mask_secret(resolved_pw),
                 "password_len": len(resolved_pw) if resolved_pw else 0}
        token = None
        self.token_info = {}

        if not resolved_pw:
            debug["error"] = ("未取得 password 密文：請設定環境變數 LOGIN_PASSWORD_PAYLOAD，"
                              "或在 login_config.json 填入 password_payload（前端 DevTools 抓到的可重放密文）。")
            _log("HMI login failed: 未設定 LOGIN_PASSWORD_PAYLOAD（env / .env / login_config.json 皆無密文）")
            return (None, debug) if return_debug else None

        _log(f"HMI login: user={resolved_user} password={mask_secret(resolved_pw)}")
        payload = {"username": resolved_user, "password": resolved_pw, "captchaVerification": ""}
        debug["payload_keys"] = list(payload.keys())
        try:
            resp = self.session.post(url, json=payload, timeout=self.timeout)
            debug["status"] = resp.status_code
            try:
                body = resp.json()
            except ValueError:
                body = {"_non_json": resp.text[:300]}
            debug["response"] = body
            debug["code"] = body.get("code") if isinstance(body, dict) else None
            debug["msg"] = body.get("msg") if isinstance(body, dict) else None

            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, dict):
                token = data.get("accessToken")
                # 一併保留 refreshToken / expiresTime
                self.token_info = {
                    "accessToken": token,
                    "refreshToken": data.get("refreshToken"),
                    "expiresTime": data.get("expiresTime"),
                }
                debug["refreshToken"] = data.get("refreshToken")
                debug["expiresTime"] = data.get("expiresTime")
            debug["accessToken_field"] = "data.accessToken" if token else None
            if token:
                self.session.headers["Authorization"] = f"Bearer {token}"
                self.logged_in = True
                _log(f"HMI login success (accessToken={mask_secret(token)})")
            else:
                _log(f"HMI login failed: code={debug.get('code')} msg={debug.get('msg')}")
        except requests.exceptions.RequestException as e:
            debug["error"] = str(e)
            _log(f"HMI login failed: {e}")

        return (token, debug) if return_debug else token

    # 舊名相容：device_control 舊呼叫 login(USERNAME, PASSWORD) 仍可用
    def login(self, username=None, password=None):
        return self.login_hmi(username, password)


if __name__ == "__main__":
    # 最小登入驗證：只驗證 login_hmi() 能否取得 accessToken；不呼叫任何設備控制 endpoint。
    import io
    import sys
    import json

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    # password 由 config/env 決定（LOGIN_PASSWORD_PAYLOAD / login_config.json）；不硬寫密文
    c = ApiClient()
    tok, dbg = c.login_hmi(return_debug=True)

    print("== login_hmi 最小驗證 ==")
    print("request URL   :", dbg.get("url"))
    print("method        :", dbg.get("method"), "| Content-Type:", dbg.get("content_type"))
    print("password 來源  :", dbg.get("password_source"))
    print("request body  :", {"username": dbg.get("username"),
                              "password": dbg.get("password_masked"),
                              "captchaVerification": ""})
    print("response status:", dbg.get("status"))
    print("response code  :", dbg.get("code"), "| msg:", dbg.get("msg"))
    print("response(raw)  :", json.dumps(dbg.get("response"), ensure_ascii=False))
    if dbg.get("error"):
        print("request error  :", dbg.get("error"))
    if tok:
        print("accessToken    : 取得成功（欄位 =", dbg.get("accessToken_field"), "）")
        print("refreshToken   :", dbg.get("refreshToken"))
        print("expiresTime    :", dbg.get("expiresTime"))
    else:
        print("accessToken    : 未取得")
