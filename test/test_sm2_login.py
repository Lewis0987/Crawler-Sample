# -*- coding: utf-8 -*-
"""
雙模式登入驗證（test_sm2_login.py）
===================================
驗證 api_client.login_hmi 的 password 解析雙模式：
  1. env-payload：LOGIN_PASSWORD_PAYLOAD（舊模式，可重放密文）
  2. sm2        ：HMI_PASSWORD + SM2_PUBLIC_KEY（新模式，即時 SM2 加密）
  3. arg        ：呼叫端傳入（fallback）

離線案例（S1~S3）：用系統暫存 env 檔 + `API_ENV_FILE` 指定，
連線目標指向死埠（127.0.0.1:9），只驗證「來源判斷 / SM2 加密輸出」，不打真伺服器。

真實登入（S4，需 --with-login）：用實際 env（含真正的 SM2_PUBLIC_KEY 或 LOGIN_PASSWORD_PAYLOAD）
向真伺服器登入一次。

用法：
    python test_sm2_login.py                # 只跑 S1~S3 離線
    python test_sm2_login.py --with-login   # 追加 S4 真實登入
"""

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import api_client as ac
import sm2_util

_ENV_KEYS = ["API_ENV_FILE", "LOGIN_PASSWORD_PAYLOAD", "HMI_PASSWORD",
             "SM2_PUBLIC_KEY", "HMI_USERNAME", "LOGIN_USERNAME"]


def make_test_pubkey():
    """產生一把測試用 SM2 公鑰（僅供離線驗證加密輸出格式；非真站公鑰）。"""
    d = 0x2A3B4C5D6E7F8091A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D5E6F70819
    x, y = sm2_util._pt_mul(d, sm2_util._G)
    return "04" + format(x, "064x") + format(y, "064x")


def _reset_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    ac._dotenv_loaded = False  # 讓 _load_dotenv 重新掃描


def run_offline(name, env_lines, expect_source):
    print("=" * 60)
    print(f"{name}（預期 password_source = {expect_source}）")
    _reset_env()
    with tempfile.TemporaryDirectory() as d:
        envp = os.path.join(d, "scenario.env")
        with open(envp, "w", encoding="utf-8") as f:
            f.write("\n".join(env_lines) + "\n")
        os.environ["API_ENV_FILE"] = envp
        client = ac.ApiClient(base_url="http://127.0.0.1:9", timeout=1)
        tok, dbg = client.login_hmi(return_debug=True)
    print(f"  password_source = {dbg.get('password_source')}")
    print(f"  password_masked = {dbg.get('password_masked')}")
    if dbg.get("error"):
        print(f"  note = {dbg.get('error')[:70]}...（死埠或缺設定，屬預期）")
    ok = dbg.get("password_source") == expect_source
    print(f"  結果：{'PASS' if ok else 'FAIL'}")
    return ok


def s4_real_login():
    print("=" * 60)
    print("S4. 真實登入（讀實際 env，連真伺服器）")
    _reset_env()  # 清掉離線案例殘留，改用真實 discover
    client = ac.ApiClient()
    tok, dbg = client.login_hmi(return_debug=True)
    print(f"  password_source = {dbg.get('password_source')}")
    print(f"  login = {'success' if tok else 'failed'}"
          f"（code={dbg.get('code')} msg={dbg.get('msg')}）")
    if tok:
        print(f"  accessToken = {ac.mask_secret(tok)}")
    elif dbg.get("error"):
        print(f"  error = {dbg.get('error')}")


def main():
    pub = make_test_pubkey()
    results = []
    results.append(run_offline(
        "S1. payload 模式（LOGIN_PASSWORD_PAYLOAD）",
        ["LOGIN_PASSWORD_PAYLOAD=0400112233445566778899aabbccddeeff"],
        "env-payload"))
    results.append(run_offline(
        "S2. SM2 模式（HMI_PASSWORD + SM2_PUBLIC_KEY，即時加密）",
        ["HMI_USERNAME=hmiUser", "HMI_PASSWORD=hmiUser123", f"SM2_PUBLIC_KEY={pub}"],
        "sm2"))
    results.append(run_offline(
        "S3. 兩者皆無（應報缺設定，無 source）",
        ["HMI_USERNAME=hmiUser"],
        None))

    print("=" * 60)
    print(f"離線結果：{sum(1 for r in results if r)}/{len(results)} PASS")

    if "--with-login" in sys.argv:
        s4_real_login()
    else:
        print("（略過 S4 真實登入；如需執行請加 --with-login）")
    print("=" * 60)
    print("完成。")


if __name__ == "__main__":
    main()
