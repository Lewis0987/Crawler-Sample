# -*- coding: utf-8 -*-
"""
零依賴 SM2 加密（配 SM3），對齊 sm-crypto 0.3.x doEncrypt(cipherMode=1) 的輸出。
輸出格式：hex 字串 = "04" + C1(x||y, 64 bytes) + C3(32 bytes) + C2(len(msg))  → 即 C1C3C2。
只需要「加密」（登入用），不含解密。
"""

import secrets

# ---- SM2 推薦曲線參數 (sm2p256v1 / GB-T 32918) ----
_P = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
_A = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFC
_B = 0x28E9FA9E9D9F5E344D5A9E4BCF6509A7F39789F515AB8F92DDBCBD414D940E93
_N = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF409  # 補齊見下
_N = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
_GX = 0x32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7
_GY = 0xBC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0
_G = (_GX, _GY)


# ---------------- 橢圓曲線運算（Jacobian 不用，直接仿射 + 模逆）----------------
def _inv(a, m):
    return pow(a, -1, m)


def _pt_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if x1 == x2 and y1 == y2:
        lam = (3 * x1 * x1 + _A) * _inv(2 * y1 % _P, _P) % _P
    else:
        lam = (y2 - y1) * _inv((x2 - x1) % _P, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _pt_mul(k, pt):
    r = None
    while k:
        if k & 1:
            r = _pt_add(r, pt)
        pt = _pt_add(pt, pt)
        k >>= 1
    return r


# ---------------- SM3 雜湊 ----------------
_IV = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
       0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]


def _rl(x, n):
    n &= 31
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _sm3(msg: bytes) -> bytes:
    length = len(msg) * 8
    msg += b"\x80"
    while len(msg) % 64 != 56:
        msg += b"\x00"
    msg += length.to_bytes(8, "big")

    v = list(_IV)
    for i in range(0, len(msg), 64):
        b = msg[i:i + 64]
        w = [int.from_bytes(b[j:j + 4], "big") for j in range(0, 64, 4)]
        for j in range(16, 68):
            x = w[j - 16] ^ w[j - 9] ^ _rl(w[j - 3], 15)
            x = x ^ _rl(x, 15) ^ _rl(x, 23)
            w.append(x ^ _rl(w[j - 13], 7) ^ w[j - 6])
        w1 = [w[j] ^ w[j + 4] for j in range(64)]

        a, bb, c, d, e, f, g, h = v
        for j in range(64):
            tj = 0x79CC4519 if j < 16 else 0x7A879D8A
            ss1 = _rl((_rl(a, 12) + e + _rl(tj, j)) & 0xFFFFFFFF, 7)
            ss2 = ss1 ^ _rl(a, 12)
            if j < 16:
                ff = a ^ bb ^ c
                gg = e ^ f ^ g
            else:
                ff = (a & bb) | (a & c) | (bb & c)
                gg = (e & f) | (~e & g)
            tt1 = (ff + d + ss2 + w1[j]) & 0xFFFFFFFF
            tt2 = (gg + h + ss1 + w[j]) & 0xFFFFFFFF
            d = c
            c = _rl(bb, 9)
            bb = a
            a = tt1
            h = g
            g = _rl(f, 19)
            f = e
            e = (tt2 ^ _rl(tt2, 9) ^ _rl(tt2, 17)) & 0xFFFFFFFF
        v = [(x ^ y) & 0xFFFFFFFF for x, y in zip(v, [a, bb, c, d, e, f, g, h])]

    return b"".join(x.to_bytes(4, "big") for x in v)


# ---------------- KDF（以 SM3 為底）----------------
def _kdf(z: bytes, klen: int) -> bytes:
    out = b""
    ct = 1
    while len(out) < klen:
        out += _sm3(z + ct.to_bytes(4, "big"))
        ct += 1
    return out[:klen]


# ASN.1/DER 內用來辨識 SM2 SubjectPublicKeyInfo 的 OID
_OID_EC_PUBLIC_KEY = "2a8648ce3d0201"   # 1.2.840.10045.2.1 ecPublicKey
_OID_SM2 = "2a811ccf5501822d"           # 1.2.156.10197.1.301 sm2p256v1


def normalize_public_key(pub_hex: str) -> str:
    """
    把各種輸入正規化成 130 hex 的 SM2 EC point（'04' + X + Y）。
    支援：
      - 128 hex（x||y）        → 補 '04'
      - 130 hex（'04' 開頭）    → 原樣
      - ASN.1/DER SubjectPublicKeyInfo（'30' 開頭）→ 解析出 EC point
    無法解析時 raise ValueError（訊息含 length 與格式判斷）。
    """
    h = (pub_hex or "").strip().lower().replace("0x", "").replace(" ", "").replace("\n", "")
    if not h:
        raise ValueError("SM2 public key 為空")
    if any(c not in "0123456789abcdef" for c in h):
        raise ValueError(f"SM2 public key 非十六進位（可能是 PEM/Base64），length={len(h)}")

    if len(h) == 128:
        return "04" + h
    if len(h) == 130 and h.startswith("04"):
        return h

    # ASN.1 / DER（SubjectPublicKeyInfo）
    if h.startswith("30"):
        idx = h.find("034200")   # BIT STRING(0x42=66B) + 未使用位元 00 + 04(未壓縮)
        if idx >= 0:
            pt = h[idx + 6: idx + 6 + 130]
            if len(pt) == 130 and pt.startswith("04"):
                return pt
        idx2 = h.rfind("04")     # 後備：最後一段 04+128hex
        if idx2 >= 0 and len(h) - idx2 >= 130:
            pt = h[idx2: idx2 + 130]
            if len(pt) == 130:
                return pt
        raise ValueError(f"SM2 public key 疑似 ASN.1/DER 但無法取出 EC point，length={len(h)}")

    # 疑似 SM2 密文（04 + C1 + C3 + C2），常見誤把加密後 password 貼成公鑰
    if h.startswith("04") and len(h) > 130:
        raise ValueError(f"SM2 public key length={len(h)}：符合 SM2『密文』特徵(04+C1+C3+C2)，"
                         f"疑似誤填『加密後 password 密文』；請改填真正的公鑰（128 或 130 hex）")

    raise ValueError(f"SM2 public key 長度/格式不正確：length={len(h)}（應為 128 或 130 hex，或 ASN.1 DER）")


def _parse_pubkey(pub_hex: str):
    """正規化為 130 hex 後回傳 (x, y)。"""
    h = normalize_public_key(pub_hex)[2:]  # 去掉 '04'
    return int(h[:64], 16), int(h[64:], 16)


def sm2_encrypt(plaintext, public_key_hex, prefix04=True):
    """
    以 SM2 加密 plaintext（str 或 bytes），回傳 hex 字串（C1C3C2）。
    - prefix04：是否在最前面加上 '04'（本站抓到的密文有 04 前綴）。
    - 每次呼叫隨機 k，密文都不同（正常，伺服器解密後得同一明碼）。
    """
    if isinstance(plaintext, str):
        m = plaintext.encode("utf-8")
    else:
        m = plaintext
    px, py = _parse_pubkey(public_key_hex)
    pub = (px, py)

    while True:
        k = secrets.randbelow(_N - 1) + 1
        x1, y1 = _pt_mul(k, _G)                 # C1 = k*G
        x2, y2 = _pt_mul(k, pub)                # k*Pub
        z = x2.to_bytes(32, "big") + y2.to_bytes(32, "big")
        t = _kdf(z, len(m))
        if any(t):                              # t 不可全為 0
            break

    c2 = bytes(a ^ b for a, b in zip(m, t))
    c3 = _sm3(x2.to_bytes(32, "big") + m + y2.to_bytes(32, "big"))
    c1 = x1.to_bytes(32, "big") + y1.to_bytes(32, "big")

    out = (b"\x04" if prefix04 else b"") + c1 + c3 + c2  # C1 C3 C2
    return out.hex()


if __name__ == "__main__":
    # 自我測試：SM3 標準測資 "abc" → 66c7f0f4...
    got = _sm3(b"abc").hex()
    exp = "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    print("SM3('abc'):", got)
    print("SM3 自測:", "PASS" if got == exp else "FAIL（實作有誤）")
