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

import requests


# ============ 共用設定 ============
BASE_URL = "http://192.168.128.110:8080/admin-api"   # 如 8853 有代理可改成 :8853
DEFAULT_TIMEOUT = 8
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
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

    def login(self, username, password):
        """
        登入取得 accessToken，並設定 session 的 Authorization: Bearer <token>。
        ⚠️ 登入是必要且唯一的 POST；登入成功後本 client 仍只提供 GET，不送任何控制命令。
        成功回傳 token 字串；失敗回傳 None 並印出原因。

        依前端盤點：POST /system/auth/login，帶 {username, password}，回傳 data.accessToken。
        前端客戶端寫法為 params，故先試 JSON body，失敗再退回 query params。
        （tenant-id 不需要，因 VITE_GLOB_APP_TENANT_ENABLE=false）
        """
        url = self.base_url + self.LOGIN_PATH
        cred = {"username": username, "password": password}
        body = None
        for attempt in ("json", "params"):
            try:
                if attempt == "json":
                    resp = self.session.post(url, json=cred, timeout=self.timeout)
                else:
                    resp = self.session.post(url, params=cred, timeout=self.timeout)
                resp.raise_for_status()
                body = resp.json()
            except requests.exceptions.RequestException as e:
                print(f"  [警告] 登入請求失敗（{attempt}）：{e}")
                continue
            except ValueError:
                print(f"  [警告] 登入回傳非 JSON（{attempt}）")
                continue

            data = unwrap(body)
            token = data.get("accessToken") if isinstance(data, dict) else None
            if token:
                self.session.headers["Authorization"] = f"Bearer {token}"
                self.logged_in = True
                print(f"  [登入成功] 已取得 accessToken（{attempt}）")
                return token
            print(f"  [警告] 登入未取得 accessToken（{attempt}）：{body}")

        return None
