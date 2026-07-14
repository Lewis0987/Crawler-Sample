# -*- coding: utf-8 -*-
"""
唯讀：分析目前 SM2_PUBLIC_KEY 的結構（長度 / ASN.1 / EC point / 其他），
判斷 214 hex 是什麼格式，並嘗試解析出真正的 EC point（04+X+Y=130 hex）。
不印私密資料（公鑰非機密，但仍遮罩中段）。

用法：python inspect_sm2_key.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from api_client import load_login_config

# SM2 曲線 OID / ecPublicKey OID（DER 內用來辨識）
_OID_EC_PUBLIC_KEY = "2a8648ce3d0201"      # 1.2.840.10045.2.1
_OID_SM2 = "2a811ccf5501822d"              # 1.2.156.10197.1.301 (sm2p256v1)


def _clean_hex(s):
    return (s or "").strip().lower().replace("0x", "").replace(" ", "").replace("\n", "")


def analyze(hex_str):
    s = _clean_hex(hex_str)
    n = len(s)
    print(f"length(hex chars) = {n}")
    print(f"bytes             = {n // 2 if n % 2 == 0 else '奇數，非整數 byte'}")
    is_hex = all(c in "0123456789abcdef" for c in s)
    print(f"is_hex            = {is_hex}")
    print(f"leading 8 bytes   = {s[:16]}…（遮罩中段）")
    print(f"trailing 4 bytes  = …{s[-8:]}")

    if not is_hex:
        print("判斷：非十六進位 → 可能是 PEM/Base64 或含非 hex 字元")
        return None

    if n in (128, 130):
        print("判斷：已是原始 SM2 EC 座標（128=x||y / 130=04+x+y）→ 可直接用")
        return ("04" + s) if n == 128 else s

    if s.startswith("30"):
        print("判斷：0x30 開頭 → ASN.1 DER SEQUENCE（SubjectPublicKeyInfo 可能）")
        has_ec = _OID_EC_PUBLIC_KEY in s
        has_sm2 = _OID_SM2 in s
        print(f"  含 ecPublicKey OID = {has_ec} | 含 sm2 OID = {has_sm2}")
        # BIT STRING 內的 EC point：找 '034200 04'（03=BITSTRING,42=66bytes,00=unused,04=uncompressed）
        idx = s.find("034200")
        if idx >= 0:
            pt = s[idx + 6:idx + 6 + 130]
            print(f"  解析出 EC point（034200 之後 130 hex）：{pt[:10]}…{pt[-6:]}  len={len(pt)}")
            if len(pt) == 130 and pt.startswith("04"):
                return pt
        # 泛用：找最後一段 04+128hex
        idx2 = s.rfind("04")
        if idx2 >= 0 and len(s) - idx2 >= 130:
            pt = s[idx2:idx2 + 130]
            if len(pt) == 130:
                print(f"  後備解析（rfind 04 後 130 hex）：{pt[:10]}…{pt[-6:]}")
                return pt
        print("  無法從 DER 取出 130 hex EC point")
        return None

    if s.startswith("04") and n == 214:
        print("判斷：04 開頭且 214 hex → 這是 SM2『密文』長度特徵（04+C1(128)+C3(64)+C2(?)），")
        print("      很可能誤把『加密後 password 密文 / LOGIN_PASSWORD_PAYLOAD』貼成公鑰！")
        return None

    if s.startswith("04"):
        print(f"判斷：04 開頭但長度 {n}（非 130）→ 非標準未壓縮 EC point")
        return None

    print("判斷：未知格式")
    return None


def main():
    cfg = load_login_config()
    k = cfg.get("sm2_public_key")
    print("=" * 56)
    print("SM2_PUBLIC_KEY 結構分析")
    print("=" * 56)
    if not k:
        print("SM2_PUBLIC_KEY 未設定（env / .env / login_config.json 皆無）")
        return
    point = analyze(k)
    print("-" * 56)
    if point:
        print(f"→ 可用的 SM2 EC point（130 hex）：{point[:10]}…{point[-6:]}")
    else:
        print("→ 目前無法得到 130 hex EC point，需確認公鑰來源（見上方判斷）")


if __name__ == "__main__":
    main()
