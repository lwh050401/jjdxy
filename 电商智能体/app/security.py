"""口令与会话令牌：沿用旧 Node 商城的 scrypt 参数，迁移过来的账号无需改密码。"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from .settings import get_settings

SCRYPT_N = 16384  # 与 node crypto.scryptSync 的默认 cost 对齐
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 64


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = _scrypt(password, salt)
    return f"scrypt${salt}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    candidate = _scrypt(str(password or ""), parts[1])
    expected = bytes.fromhex(parts[2])
    return hmac.compare_digest(candidate, expected)


def _scrypt(password: str, salt: str) -> bytes:
    return hashlib.scrypt(
        str(password or "").encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_LEN,
        maxmem=64 * 1024 * 1024,
    )


def hash_token(token: str) -> str:
    return hmac.new(get_settings().session_secret.encode("utf-8"), str(token).encode("utf-8"), hashlib.sha256).hexdigest()


def new_token(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_hex(16)}"


def new_id(prefix: str) -> str:
    """业务主键风格与旧库一致：usr_xxx / ord_xxx。"""
    return f"{prefix}_{secrets.token_hex(8)}"


def order_no() -> str:
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    import datetime as dt

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%y%m%d")
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"LM{stamp}{suffix}"
