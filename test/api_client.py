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
      - username         : 帳號（預設 hmiUser）
      - password_payload : 舊模式，DevTools 抓到的可重放 SM2 密文（LOGIN_PASSWORD_PAYLOAD）
      - hmi_password     : 新模式，密碼原文（HMI_PASSWORD）
      - sm2_public_key   : 新模式，SM2 公鑰 hex（SM2_PUBLIC_KEY）
      - source           : 密文來源 "env"|"config"|None（僅指 password_payload）

    【兩種登入模式】
      舊模式：直接送 password_payload（可重放密文）。
      新模式：以 sm2_util.sm2_encrypt(hmi_password, sm2_public_key) 每次即時加密
              （對齊前端 sm-crypto doEncrypt cipherMode=1，04 前綴 C1C3C2 hex）。
    """
    _load_dotenv()

    username = _clean(os.environ.get("HMI_USERNAME")) or _clean(os.environ.get("LOGIN_USERNAME"))
    payload = _clean(os.environ.get("LOGIN_PASSWORD_PAYLOAD"))
    hmi_password = _clean(os.environ.get("HMI_PASSWORD"))
    sm2_public_key = _clean(os.environ.get("SM2_PUBLIC_KEY"))
    source = "env" if payload else None

    if os.path.exists(LOGIN_CONFIG_FILE):
        try:
            with open(LOGIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                username = username or _clean(data.get("username"))
                if payload is None:
                    fv = _clean(data.get("password_payload"))
                    if fv:
                        payload = fv
                        source = "config"
                hmi_password = hmi_password or _clean(data.get("hmi_password"))
                sm2_public_key = sm2_public_key or _clean(data.get("sm2_public_key"))
        except (ValueError, OSError):
            pass

    return {
        "username": username or DEFAULT_USERNAME,
        "password_payload": payload,
        "hmi_password": hmi_password,
        "sm2_public_key": sm2_public_key,
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

        【password 來源（雙模式，優先序高→低）】
          1. 舊模式 payload：env/config 的 LOGIN_PASSWORD_PAYLOAD（DevTools 抓到的可重放 SM2 密文）
             → 直接送出。
          2. 新模式 SM2：env/config 同時有 HMI_PASSWORD（密碼原文）+ SM2_PUBLIC_KEY（公鑰）
             → 以 sm2_util.sm2_encrypt() 每次即時 SM2 加密（對齊前端 sm-crypto doEncrypt cipherMode=1）。
          3. fallback：呼叫端傳入的 password（相容 / 測試用）。
          → device_control_scraper.py 的 client.login(USERNAME, PASSWORD) 不需修改，
             只要設定好 config/env，就會自動採用對應模式。

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

        # password 解析（雙模式，優先序：payload → sm2 → arg）
        resolved_pw = None
        pw_source = None
        sm2_error = None
        if cfg["password_payload"]:
            resolved_pw = cfg["password_payload"]
            pw_source = "env-payload" if cfg["source"] == "env" else "config-payload"
        elif cfg["hmi_password"] and cfg["sm2_public_key"]:
            try:
                resolved_pw = sm2_util.sm2_encrypt(cfg["hmi_password"], cfg["sm2_public_key"])
                pw_source = "sm2"
            except Exception as e:
                sm2_error = str(e)
                pw_source = "sm2-error"
        elif password:
            resolved_pw = password
            pw_source = "arg"

        _SOURCE_MSG = {
            "env-payload": "Using LOGIN_PASSWORD_PAYLOAD from env (payload mode)",
            "config-payload": "Using login_config password_payload (payload mode)",
            "sm2": "Using SM2 dynamic encryption (HMI_PASSWORD + SM2_PUBLIC_KEY)",
            "sm2-error": "SM2 encryption failed",
            "arg": "Using plain password fallback",
        }
        _log(_SOURCE_MSG.get(pw_source, "No password source found"))

        debug = {"url": url, "method": "POST", "content_type": "application/json",
                 "username": resolved_user, "password_source": pw_source,
                 "password_masked": mask_secret(resolved_pw),
                 "password_len": len(resolved_pw) if resolved_pw else 0}
        token = None
        self.token_info = {}

        if not resolved_pw:
            if pw_source == "sm2-error":
                debug["error"] = f"SM2 加密失敗：{sm2_error}（請確認 SM2_PUBLIC_KEY 格式為 128/130 hex）"
                _log(f"HMI login failed: SM2 加密失敗：{sm2_error}")
            else:
                debug["error"] = ("未取得 password：請設定 (A) LOGIN_PASSWORD_PAYLOAD（可重放密文），"
                                  "或 (B) HMI_PASSWORD + SM2_PUBLIC_KEY（即時 SM2 加密）。")
                _log("HMI login failed: 未設定 LOGIN_PASSWORD_PAYLOAD，也沒有 HMI_PASSWORD+SM2_PUBLIC_KEY")
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
