"""HTTP 层公共设施：响应包裹、Cookie 会话与依赖注入。"""

from __future__ import annotations

from fastapi import Request

from .security import new_id
from .services import auth
from .settings import get_settings


def ok(data):
    return {"ok": True, "data": data}


def err(message: str, code: str = "bad_request"):
    return {"ok": False, "error": {"code": code, "message": message}}


def set_session_cookie(response, token: str) -> None:
    s = get_settings()
    response.set_cookie(s.cookie_name, token, httponly=True, samesite="lax", max_age=s.session_ttl_seconds, path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(get_settings().cookie_name, path="/")


def guest_token(request: Request) -> str | None:
    return request.cookies.get("lumen_guest") or None


def ensure_guest_token(request: Request, response) -> str:
    token = guest_token(request)
    if not token:
        token = new_id("gst")
        response.set_cookie("lumen_guest", token, httponly=True, samesite="lax", max_age=get_settings().session_ttl_seconds, path="/")
    return token


def current_user(request: Request) -> dict | None:
    return auth.user_by_token(request.cookies.get(get_settings().cookie_name))


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:255]
