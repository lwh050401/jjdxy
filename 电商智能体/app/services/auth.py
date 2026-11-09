"""账号与会话：会话记录落 MySQL，Cookie 只放随机令牌，库里存 HMAC。"""

from __future__ import annotations

import datetime as dt
import re

from .. import db
from . import cart as cart_service
from ..security import hash_password, hash_token, new_id, new_token, verify_password
from ..settings import get_settings

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def public(user: dict) -> dict:
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "phone": user.get("phone"),
        "role": user["role"],
        "status": user["status"],
        "balance_cents": int(user.get("balance_cents") or 0),
        "marketing_opt_in": bool(user.get("marketing_opt_in")),
        "created_at": user["created_at"],
    }


def by_email(email: str) -> dict | None:
    return db.get("SELECT * FROM users WHERE email = :email", {"email": (email or "").strip().lower()})


def by_id(user_id: str) -> dict | None:
    return db.get("SELECT * FROM users WHERE id = :uid", {"uid": user_id})


def _open_session(user_id: str, *, agent: str = "", ip: str = "", guest_token: str | None = None) -> str:
    token = new_token()
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=get_settings().session_ttl_seconds)
    db.insert(
        "sessions",
        {
            "id": new_id("ses"),
            "user_id": user_id,
            "token_hash": hash_token(token),
            "guest_token": guest_token,
            "user_agent": agent[:255],
            "ip": ip[:64],
            "expires_at": expires.replace(tzinfo=None).isoformat(timespec="seconds"),
            "created_at": db.now_iso(),
        },
    )
    return token


def register(*, email: str, password: str, name: str = "", **ctx) -> tuple[dict, str]:
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        raise AuthError("邮箱格式不正确")
    if len(password or "") < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise AuthError("密码至少 8 位，需同时包含字母与数字")
    if by_email(email):
        raise AuthError("这个邮箱已经注册过了", 409)
    now = db.now_iso()
    user_id = new_id("usr")
    db.insert(
        "users",
        {
            "id": user_id,
            "email": email,
            "name": (name or email.split("@")[0])[:80],
            "password_hash": hash_password(password),
            "role": "customer",
            "status": "active",
            "balance_cents": 0,
            "email_verified_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    token = _open_session(user_id, agent=ctx.get("agent", ""), ip=ctx.get("ip", ""))
    return by_id(user_id), token


def login(*, email: str, password: str, guest_token: str | None = None, **ctx) -> tuple[dict, str]:
    user = by_email(email)
    if not user or not verify_password(password or "", user["password_hash"]):
        raise AuthError("邮箱或密码不正确", 401)
    if user["status"] != "active":
        raise AuthError("账号已被停用，请联系客服", 403)
    now = db.now_iso()
    db.run("UPDATE users SET last_login_at = :now, updated_at = :now WHERE id = :uid", {"now": now, "uid": user["id"]})
    token = _open_session(user["id"], agent=ctx.get("agent", ""), ip=ctx.get("ip", ""), guest_token=guest_token)
    if guest_token:
        cart_service.merge_guest(user["id"], guest_token)
    return by_id(user["id"]), token


def logout(token: str) -> None:
    db.run("UPDATE sessions SET revoked_at = :now WHERE token_hash = :hash AND revoked_at IS NULL", {"now": db.now_iso(), "hash": hash_token(token)})


def user_by_token(token: str | None) -> dict | None:
    if not token:
        return None
    row = db.get(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = :hash AND s.revoked_at IS NULL AND s.expires_at > UTC_TIMESTAMP()""",
        {"hash": hash_token(token)},
    )
    return row


def addresses(user_id: str) -> list[dict]:
    return db.all_("SELECT * FROM addresses WHERE user_id = :uid ORDER BY is_default_shipping DESC, created_at ASC", {"uid": user_id})


def save_address(user_id: str, form: dict, address_id: str | None = None) -> dict:
    required = ("receiver", "phone", "province", "city", "district", "detail")
    missing = [k for k in required if not str(form.get(k, "")).strip()]
    if missing:
        raise AuthError("请填写完整的收货信息：" + "、".join(missing))
    payload = {k: str(form.get(k, ""))[:120] for k in required}
    payload.update({"postal": str(form.get("postal", ""))[:16], "tag": str(form.get("tag", ""))[:24]})
    if address_id:
        db.update("addresses", payload, "id = :aid AND user_id = :uid", {"aid": address_id, "uid": user_id})
        row = db.get("SELECT * FROM addresses WHERE id = :aid", {"aid": address_id})
        if not row:
            raise AuthError("地址不存在", 404)
        return row
    payload.update({"user_id": user_id, "is_default_shipping": 0, "created_at": db.now_iso()})
    if form.get("is_default"):
        db.run("UPDATE addresses SET is_default_shipping = 0 WHERE user_id = :uid", {"uid": user_id})
        payload["is_default_shipping"] = 1
    db.insert("addresses", payload)
    return db.get("SELECT * FROM addresses WHERE user_id = :uid ORDER BY created_at DESC, id DESC LIMIT 1", {"uid": user_id})


def delete_address(user_id: str, address_id: str) -> None:
    db.run("DELETE FROM addresses WHERE id = :aid AND user_id = :uid", {"aid": address_id, "uid": user_id})
