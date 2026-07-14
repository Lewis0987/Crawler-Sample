# -*- coding: utf-8 -*-
"""
[DEPRECATED / DIAGNOSTIC 一次性工具] 密文重放登入驗證。
⚠️ 正式流程已改為 SM2 即時加密（見 api_client.login_hmi）；本檔僅供舊版相容 / 緊急測試，
   run_all.py 不會呼叫。password 由命令列原樣帶入（不加密），僅遮罩顯示、不打印完整值。
用法：
    python login_probe.py "<前端實際送出的完整 password 長字串>"
"""
import io
import sys
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from api_client import ApiClient

USERNAME = "hmiUser"

if len(sys.argv) < 2 or not sys.argv[1]:
    print("請以參數提供 password 長字串：python login_probe.py \"<password>\"")
    sys.exit(2)

password = sys.argv[1]   # 原樣，不轉換


def _mask(s):
    if len(s) <= 10:
        return s[:2] + "…" + s[-2:]
    return f"{s[:6]}…{s[-6:]}（len={len(s)}）"


client = ApiClient()
tok, dbg = client.login_hmi(USERNAME, password, return_debug=True)

print("== 1. Request 摘要 ==")
print("URL          :", dbg.get("url"))
print("method       :", dbg.get("method"))
print("Content-Type :", dbg.get("content_type"))
print("body keys    :", dbg.get("payload_keys"))
print("password     :", _mask(password), "（原樣帶入，未轉換）")

print("\n== 2. Response 摘要 ==")
print("HTTP status  :", dbg.get("status"))
print("code         :", dbg.get("code"))
print("msg          :", dbg.get("msg"))
resp = dbg.get("response") or {}
print("data is null :", (isinstance(resp, dict) and resp.get("data") is None))
print("accessToken  :", ("取得成功（data.accessToken）" if tok else "未取得"))
if tok:
    print("refreshToken :", dbg.get("refreshToken"))
    print("expiresTime  :", dbg.get("expiresTime"))

print("\n== 3. 結論 ==")
if tok:
    print("成功：前端 payload 重放可登入，已取得 accessToken")
else:
    print("失敗：即使使用提供的 password 長字串重放，仍無法登入")
