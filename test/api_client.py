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
import pathlib

import requests

import sm2_util  # 新模式：Python 端即時 SM2 加密（sm-crypto doEncrypt cipherMode=1 對應）


# ============ 共用設定 ============
BASE_URL = "http://192.168.128.110:8080/admin-api"   # 如 8853 有代理可改成 :8853
DEFAULT_TIMEOUT = 8
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
}

DEFAULT_USERNAME = "hmiUser"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# 登入密碼設定檔（與本檔同目錄）。實際密文請放這裡或環境變數，勿寫進程式碼。
LOGIN_CONFIG_FILE = os.path.join(_HERE, "login_config.json")

_dotenv_loaded = False


def _env_files_in(d):
    """
    掃描單一目錄下的 env 檔（用 pathlib，不寫死檔名）。
    回傳順序：.env 優先，其餘 *.env 依「檔名」排序。
    （Windows 上 .env 未必被 *.env 掃到，故單獨處理並去重。）
    """
    p = pathlib.Path(d)
    files = []
    dotenv = p / ".env"
    if dotenv.is_file():
        files.append(dotenv)
    try:
        globbed = sorted(p.glob("*.env"), key=lambda x: x.name)
    except OSError:
        globbed = []
    for f in globbed:
        if f.is_file() and f.name != ".env" and f not in files:
            files.append(f)
    return files


def discover_env_candidates():
    """
    依優先序回傳 env 候選檔（去重、保序）：
      1. API_ENV_FILE 指定檔（若存在）
      2. api_client.py 同層 .env
      3. 同層其他 *.env（依檔名排序）
      4. 專案根目錄 .env
      5. 專案根目錄其他 *.env（依檔名排序）
    """
    candidates = []
    forced = os.environ.get("API_ENV_FILE")
    if forced:
        fp = pathlib.Path(forced)
        if fp.is_file():
            candidates.append(fp)
    for d in (_HERE, _ROOT):
        for f in _env_files_in(d):
            if f not in candidates:
                candidates.append(f)
    return candidates


def _apply_env_file(path):
    """把 KEY=VALUE 載入 os.environ（僅在尚未設定時；# 註解、去引號、不展開）。"""
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
    except OSError as e:
        print(f"[ENV] 讀取失敗 {path}: {e}")


def _load_dotenv():
    """
    動態搜尋並載入單一 env 檔（掃 api_client 同層與專案根的 .env / *.env）。
    只載入優先序最高的一個；多個候選會列出。不改變登入讀取邏輯。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    candidates = discover_env_candidates()
    if not candidates:
        print("[ENV] no env file found")
        return
    if len(candidates) > 1:
        print("[ENV] candidates:")
        for c in candidates:
            print(f"  - {c}")
    chosen = candidates[0]
    print(f"[ENV] loaded: {chosen}")
    _apply_env_file(chosen)


def mask_secret(s):
    """遮罩密文：只顯示前 5 + ... + 後 5 與長度；短字串只顯示長度。"""
    if not s:
        return "(empty)"
    if len(s) <= 12:
        return f"(len={len(s)})"
    return f"{s[:5]}...{s[-5:]} (len={len(s)})"


def _redact_login_body(body):
    """回傳 login 回應的遮罩副本：data.accessToken / refreshToken 只留前後幾字元，其餘不動。"""
    if not isinstance(body, dict):
        return body
    safe = dict(body)
    data = safe.get("data")
    if isinstance(data, dict):
        data = dict(data)
        for k in ("accessToken", "refreshToken"):
            if data.get(k):
                data[k] = mask_secret(data[k])
        safe["data"] = data
    return safe


def _extract_error_message(body):
    """從回應 body 兼容多種錯誤欄位擷取訊息：msg / message / errorMessage / error。"""
    if not isinstance(body, dict):
        return None
    for k in ("msg", "message", "errorMessage", "error", "error_description"):
        v = body.get(k)
        if v:
            return str(v)
    return None


def _clean(v):
    """去除佔位字串（例如 '<...>'）與空白；空值回 None。"""
    if v is None:
        return None
    v = str(v).strip()
    if not v or v.startswith("<"):
        return None
    return v


def load_login_config():
    """
    讀取登入設定（環境變數/.env 優先，其次 login_config.json）。

    回傳 dict：
      - username       : 帳號（預設 hmiUser；HMI_USERNAME / LOGIN_USERNAME）
      - hmi_password   : 密碼原文（HMI_PASSWORD）
      - sm2_public_key : SM2 公鑰 hex（SM2_PUBLIC_KEY；支援 128/130/ASN.1-DER）

    登入採 SM2 即時加密：password = sm2_util.sm2_encrypt(hmi_password, sm2_public_key)
    （對齊前端 sm-crypto doEncrypt cipherMode=1，04 前綴 C1C3C2 hex）。
    """
    _load_dotenv()

    username = _clean(os.environ.get("HMI_USERNAME")) or _clean(os.environ.get("LOGIN_USERNAME"))
    hmi_password = _clean(os.environ.get("HMI_PASSWORD"))
    sm2_public_key = _clean(os.environ.get("SM2_PUBLIC_KEY"))

    if os.path.exists(LOGIN_CONFIG_FILE):
        try:
            with open(LOGIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                username = username or _clean(data.get("username"))
                hmi_password = hmi_password or _clean(data.get("hmi_password"))
                sm2_public_key = sm2_public_key or _clean(data.get("sm2_public_key"))
        except (ValueError, OSError):
            pass

    return {
        "username": username or DEFAULT_USERNAME,
        "hmi_password": hmi_password,
        "sm2_public_key": sm2_public_key,
    }


def encrypt_password_sm2(password, public_key_hex):
    """
    SM2 加密登入密碼（獨立 helper）：驗證/正規化公鑰後，回傳前端相同格式的密文
    （04 + C1C3C2 hex，對齊 sm-crypto doEncrypt cipherMode=1）。公鑰格式錯誤會 raise。
    """
    sm2_util.normalize_public_key(public_key_hex)   # 驗證 128/130/ASN.1-DER，錯則 raise
    return sm2_util.sm2_encrypt(password, public_key_hex)


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
    def get(self, path, params=None, unwrap_envelope=True, headers=None):
        """
        GET 一支 API。成功回傳（預設解封包後的）資料；失敗回傳 None 並印警告。
        path 可為 "/hmiGuest/..." 或 "/system/..."（會接在 base_url 後）。
        headers：本次請求額外標頭（如 {"Accept-Language": "zh-TW"} 取中文化 value），不影響其他請求。
        """
        url = self.base_url + path
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout, headers=headers)
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
        HMI 登入（SM2 即時加密）— 單一封裝函式。
        - POST /system/auth/login，body={username, password(SM2 密文), captchaVerification:""}
        - password 來源優先序：
            1. env/config 的 HMI_PASSWORD + SM2_PUBLIC_KEY → encrypt_password_sm2() 即時加密
            2. 呼叫端傳入的 password（相容 / 測試用）
        - 成功設定 Authorization: Bearer；回傳 token（失敗回傳 None）。
          return_debug=True 時回傳 (token, debug_dict)。
        - log 不含機密：不印 password / token / 公鑰 / 密文。
        """
        url = self.base_url + self.LOGIN_PATH

        def _log(msg):
            if log:
                print(msg)

        provisional_user = username or DEFAULT_USERNAME
        _log(f"嘗試登入（{provisional_user}）…")
        cfg = load_login_config()   # 觸發 [ENV] loaded: <path>
        user = username or cfg["username"]

        # 決定 password：SM2 動態加密優先，其次呼叫端傳入值
        enc_password, source, error = None, None, None
        if cfg["hmi_password"] and cfg["sm2_public_key"]:
            try:
                enc_password = encrypt_password_sm2(cfg["hmi_password"], cfg["sm2_public_key"])
                source = "sm2"
            except Exception as e:
                error, source = str(e), "sm2-error"
        elif password:
            enc_password, source = password, "arg"

        debug = {"url": url, "method": "POST", "username": user, "password_source": source}
        token = None
        self.token_info = {}

        if not enc_password:
            reason = error or "未設定 HMI_PASSWORD + SM2_PUBLIC_KEY（或未傳入 password）"
            debug["error"] = reason
            _log("HMI login failed")
            _log(f"msg: {reason}")
            return (None, debug) if return_debug else None

        _log(f"HMI login: user={user}")
        payload = {"username": user, "password": enc_password, "captchaVerification": ""}
        try:
            resp = self.session.post(url, json=payload, timeout=self.timeout)
            debug["status"] = resp.status_code
            debug["content_type"] = resp.headers.get("Content-Type")
            try:
                body = resp.json()
            except ValueError:
                body = {"_non_json": (resp.text or "")[:500]}
            debug["response"] = _redact_login_body(body)   # 遮罩 accessToken/refreshToken
            debug["code"] = body.get("code") if isinstance(body, dict) else None
            debug["msg"] = (_extract_error_message(body)
                            or (body.get("_non_json") if isinstance(body, dict) else None))

            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, dict):
                token = data.get("accessToken")
                # token_info 保留完整（供 Authorization 使用，僅存記憶體、不輸出）
                self.token_info = {"accessToken": token,
                                   "refreshToken": data.get("refreshToken"),
                                   "expiresTime": data.get("expiresTime")}
                debug["refreshToken"] = mask_secret(data.get("refreshToken"))  # 遮罩
                debug["expiresTime"] = data.get("expiresTime")
            if token:
                self.session.headers["Authorization"] = f"Bearer {token}"
                self.logged_in = True
                _log("HMI login success")
            else:
                # 失敗診斷（皆為非機密）：狀態 / code / msg / Content-Type / 回應前 300 字
                # response preview 取自「已遮罩」的 body，確保不外洩 token/密文。
                preview = json.dumps(debug.get("response"), ensure_ascii=False)[:300]
                _log("HMI login failed")
                _log(f"HTTP: {resp.status_code}")
                _log(f"code: {debug.get('code')}")
                _log(f"msg: {debug.get('msg')}")
                _log(f"Content-Type: {debug.get('content_type')}")
                _log(f"response preview: {preview}")
        except requests.exceptions.RequestException as e:
            debug["error"] = f"{type(e).__name__}: {e}"
            _log("HMI login failed")
            _log("HTTP: -")
            _log("code: -")
            _log(f"msg: {e}")
            _log(f"Content-Type: -")
            _log(f"exception: {type(e).__name__}")

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

    # password 由 config/env 的 HMI_PASSWORD + SM2_PUBLIC_KEY 即時 SM2 加密；不硬寫密文
    c = ApiClient()
    tok, dbg = c.login_hmi(return_debug=True)

    print("== login_hmi 最小驗證 ==")
    print("request URL    :", dbg.get("url"))
    print("username       :", dbg.get("username"))
    print("password 來源  :", dbg.get("password_source"))
    print("HTTP status    :", dbg.get("status"))
    print("code           :", dbg.get("code"), "| msg:", dbg.get("msg"))
    if dbg.get("error"):
        print("error          :", dbg.get("error"))
    print("accessToken    :", "取得成功" if tok else "未取得")
