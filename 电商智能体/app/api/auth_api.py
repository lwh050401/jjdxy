"""登录注册与收货地址。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..deps import clear_session_cookie, client_ip, current_user, err, guest_token, ok, set_session_cookie, user_agent
from ..services import auth

router = APIRouter()


def fail(message: str, status: int = 400):
    return JSONResponse(status_code=status, content=err(message))


@router.post("/register")
def register(request: Request, response: Response, payload: dict):
    try:
        user, token = auth.register(
            email=payload.get("email", ""),
            password=payload.get("password", ""),
            name=payload.get("name", ""),
            agent=user_agent(request),
            ip=client_ip(request),
        )
    except auth.AuthError as exc:
        return fail(exc.message, exc.status)
    set_session_cookie(response, token)
    return ok(auth.public(user))


@router.post("/login")
def login(request: Request, response: Response, payload: dict):
    try:
        user, token = auth.login(
            email=payload.get("email", ""),
            password=payload.get("password", ""),
            guest_token=guest_token(request),
            agent=user_agent(request),
            ip=client_ip(request),
        )
    except auth.AuthError as exc:
        return fail(exc.message, exc.status)
    set_session_cookie(response, token)
    return ok(auth.public(user))


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("lumen_sid")
    if token:
        auth.logout(token)
    clear_session_cookie(response)
    return ok({"logged_out": True})


@router.get("/me")
def me(request: Request):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("未登录", "unauthorized"))
    data = auth.public(who)
    data["addresses"] = auth.addresses(who["id"])
    data["orders"] = _order_count(who["id"])
    return ok(data)


def _order_count(user_id: str) -> int:
    from .. import db

    return int(db.value("SELECT COUNT(*) FROM orders WHERE user_id = :uid", {"uid": user_id}) or 0)


@router.get("/addresses")
def list_addresses(request: Request):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("未登录", "unauthorized"))
    return ok(auth.addresses(who["id"]))


@router.post("/addresses")
def create_address(request: Request, payload: dict):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("未登录", "unauthorized"))
    try:
        return ok(auth.save_address(who["id"], payload))
    except auth.AuthError as exc:
        return fail(exc.message, exc.status)


@router.patch("/addresses/{address_id}")
def update_address(request: Request, address_id: str, payload: dict):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("未登录", "unauthorized"))
    try:
        return ok(auth.save_address(who["id"], payload, address_id))
    except auth.AuthError as exc:
        return fail(exc.message, exc.status)


@router.delete("/addresses/{address_id}")
def delete_address(request: Request, address_id: str):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("未登录", "unauthorized"))
    auth.delete_address(who["id"], address_id)
    return ok({"deleted": address_id})
