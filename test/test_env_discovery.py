# -*- coding: utf-8 -*-
"""
[DIAGNOSTIC 一次性診斷工具] env 檔動態搜尋測試（test_env_discovery.py）
==========================================
⚠️ 一次性診斷 / 開發輔助用；不屬於正式流程，run_all.py 不會呼叫本檔。
驗證 api_client.py 的 .env / *.env 動態搜尋與載入邏輯。

測試案例：
  A.  真實環境：目前 test/.env（或專案根）候選 + 載入 + 登入檢查
  B1. 同層多檔排序：.env、1.env、login.env、abc.env、test.env（+ 非 env 檔）
  B2. 只有單一任意名稱 env 檔（例如 abc.env）
  B3. API_ENV_FILE 指定檔最優先

用法：
    cd "D:\\Crawler Sample\\test"
    python test_env_discovery.py

注意：B1~B3 使用系統暫存目錄（tempfile），測完自動清除，不會在專案內留檔。
"""

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import api_client as ac
from api_client import ApiClient, discover_env_candidates, mask_secret


def _make_env(dir_path, name, content="K=V\n"):
    with open(os.path.join(dir_path, name), "w", encoding="utf-8") as f:
        f.write(content)


def test_A_real_and_login():
    print("=" * 60)
    print("A. 真實環境：候選 + 載入 + 登入檢查")
    cands = discover_env_candidates()
    print("  discover_env_candidates():")
    if cands:
        for c in cands:
            print(f"    - {c}")
    else:
        print("    (無候選)")

    # 實際登入（login_hmi 內部會觸發 _load_dotenv，印出 [ENV] loaded）
    client = ApiClient()
    tok, dbg = client.login_hmi(return_debug=True)
    print(f"  password_source = {dbg.get('password_source')}")
    print(f"  login = {'success' if tok else 'failed'}"
          f"（code={dbg.get('code')} msg={dbg.get('msg')}）")
    if tok:
        print(f"  accessToken = {mask_secret(tok)}")


def test_B1_ordering():
    print("=" * 60)
    print("B1. 同層多檔排序（.env 優先，其餘 *.env 依檔名）")
    with tempfile.TemporaryDirectory() as d:
        for n in [".env", "1.env", "login.env", "abc.env", "test.env", "notenv.txt"]:
            _make_env(d, n)
        order = [f.name for f in ac._env_files_in(d)]
        print(f"  掃描目錄：{d}")
        print(f"  順序：{order}")
        print("  預期：['.env', '1.env', 'abc.env', 'login.env', 'test.env']（notenv.txt 不列入）")


def test_B2_single_arbitrary():
    print("=" * 60)
    print("B2. 只有單一任意名稱 env 檔（abc.env）")
    with tempfile.TemporaryDirectory() as d:
        _make_env(d, "abc.env")
        found = [f.name for f in ac._env_files_in(d)]
        print(f"  找到：{found}（預期 ['abc.env']）")


def test_B3_api_env_file_priority():
    print("=" * 60)
    print("B3. API_ENV_FILE 指定檔最優先")
    with tempfile.TemporaryDirectory() as d:
        forced = os.path.join(d, "login.env")
        _make_env(d, "login.env")
        _make_env(d, "1.env")
        old = os.environ.get("API_ENV_FILE")
        os.environ["API_ENV_FILE"] = forced
        try:
            first = discover_env_candidates()[0]
        finally:
            if old is None:
                os.environ.pop("API_ENV_FILE", None)
            else:
                os.environ["API_ENV_FILE"] = old
        print(f"  API_ENV_FILE = {forced}")
        print(f"  候選首位 = {first}")
        print(f"  結果：{'PASS' if str(first) == forced else 'FAIL'}（應為指定的 login.env）")


def main():
    # 預設只跑 B1~B3 本地離線測試；A（真實登入）需明確加 --with-login
    test_B1_ordering()
    test_B2_single_arbitrary()
    test_B3_api_env_file_priority()
    if "--with-login" in sys.argv:
        test_A_real_and_login()  # 會真的登入一次
    else:
        print("=" * 60)
        print("（略過 A 真實登入測試；如需執行請加 --with-login）")
    print("=" * 60)
    print("完成。")


if __name__ == "__main__":
    main()
