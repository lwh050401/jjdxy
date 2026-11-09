"""购物车、结算与订单接口。"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..deps import current_user, ensure_guest_token, err, guest_token, ok, set_session_cookie
from ..services import cart as cart_service
from ..services import checkout, orders, refunds

router = APIRouter()

# 退款申请频控：同一账号 10 分钟内最多 10 次，防止刷单式提交
_REFUND_WINDOW = 600.0
_REFUND_LIMIT = 10
_refund_hits: dict[str, deque] = defaultdict(deque)


def _refund_limited(user_id: str) -> bool:
    now = time.time()
    queue = _refund_hits[user_id]
    while queue and now - queue[0] > _REFUND_WINDOW:
        queue.popleft()
    if len(queue) >= _REFUND_LIMIT:
        return True
    queue.append(now)
    return False


def fail(exc: Exception):
    status = getattr(exc, "status", 400)
    return JSONResponse(status_code=status, content=err(getattr(exc, "message", str(exc))))


def resolve_cart(request: Request, response: Response) -> dict:
    who = current_user(request)
    cart, new_guest = cart_service.ensure(who["id"] if who else None, guest_token(request))
    if new_guest:
        response.set_cookie("lumen_guest", new_guest, httponly=True, samesite="lax", max_age=30 * 24 * 3600, path="/")
    return cart


@router.get("/cart")
def get_cart(request: Request, response: Response):
    cart = resolve_cart(request, response)
    priced = cart_service.view(cart["id"])
    priced["totals"] = checkout.quote(cart=priced) if priced["items"] else None
    return ok(priced)


@router.post("/cart/items")
def add_item(request: Request, response: Response, payload: dict):
    cart = resolve_cart(request, response)
    try:
        return ok(cart_service.add(cart["id"], int(payload.get("variant_id", 0)), int(payload.get("qty", 1))))
    except cart_service.CartError as exc:
        return fail(exc)


@router.patch("/cart/items/{item_id}")
def update_item(request: Request, response: Response, item_id: int, payload: dict):
    cart = resolve_cart(request, response)
    try:
        return ok(cart_service.set_qty(cart["id"], item_id, int(payload.get("qty", 1))))
    except cart_service.CartError as exc:
        return fail(exc)


@router.delete("/cart/items/{item_id}")
def delete_item(request: Request, response: Response, item_id: int):
    cart = resolve_cart(request, response)
    return ok(cart_service.remove(cart["id"], item_id))


@router.delete("/cart")
def clear_cart(request: Request, response: Response):
    cart = resolve_cart(request, response)
    return ok(cart_service.clear(cart["id"]))


@router.post("/cart/coupon")
def apply_coupon(request: Request, response: Response, payload: dict):
    cart = resolve_cart(request, response)
    code = (payload.get("code") or "").strip()
    view = cart_service.view(cart["id"])
    if code:
        try:
            who = current_user(request)
            checkout.coupon_quote(code, view["subtotal_cents"], view["items"], user_id=(who or {}).get("id"))
        except checkout.CheckoutError as exc:
            return fail(exc)
    return ok(cart_service.set_coupon(cart["id"], code or None))


@router.post("/cart/plan")
def choose_plan(request: Request, response: Response, payload: dict):
    cart = resolve_cart(request, response)
    return ok(cart_service.set_plan(cart["id"], payload.get("plan_code")))


@router.post("/checkout/quote")
def quote(request: Request, response: Response, payload: dict):
    cart = resolve_cart(request, response)
    view = cart_service.view(cart["id"])
    who = current_user(request)
    try:
        return ok(
            checkout.quote(
                cart=view,
                plan_code=payload.get("plan_code") or view["shipping_plan_code"],
                coupon_code=payload.get("coupon_code") if payload.get("coupon_code") is not None else (view["coupon_code"] or ""),
                user_id=(who or {}).get("id"),
            )
        )
    except checkout.CheckoutError as exc:
        return fail(exc)


@router.post("/checkout/place")
def place(request: Request, response: Response, payload: dict):
    cart = resolve_cart(request, response)
    view = cart_service.view(cart["id"])
    who = current_user(request)
    try:
        order = checkout.place(
            cart=view,
            contact=payload.get("contact") or {},
            address=payload.get("address") or {},
            plan_code=payload.get("plan_code") or view["shipping_plan_code"],
            coupon_code=payload.get("coupon_code") if payload.get("coupon_code") is not None else (view["coupon_code"] or ""),
            note=payload.get("note") or "",
            user=who,
        )
        return ok(order)
    except checkout.CheckoutError as exc:
        return fail(exc)


@router.post("/checkout/pay")
def pay(request: Request, payload: dict):
    who = current_user(request)
    try:
        return ok(checkout.pay(order_ref=payload.get("order_no") or payload.get("order_id"), provider=payload.get("provider", "alipay"), user=who))
    except checkout.CheckoutError as exc:
        return fail(exc)


@router.post("/orders/{order_no}/cancel")
def cancel(request: Request, order_no: str, payload: dict = None):
    who = current_user(request)
    try:
        return ok(checkout.cancel(order_ref=order_no, user=who, reason=(payload or {}).get("reason") or "用户取消"))
    except checkout.CheckoutError as exc:
        return fail(exc)


@router.get("/orders")
def my_orders(request: Request, status: str = "", limit: int = 20):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("请先登录", "unauthorized"))
    return ok(orders.customer_orders(who["id"], limit=limit, status=status))


@router.get("/orders/{order_no}")
def order_detail(request: Request, order_no: str):
    who = current_user(request)
    order = orders.find(order_no, user_id=(who or {}).get("id"), is_admin=bool(who and who.get("role") == "admin"))
    if not order:
        return JSONResponse(status_code=404, content=err("订单不存在或不属于当前账号", "not_found"))
    return ok(order)


@router.get("/orders/{order_no}/refund-requests")
def refund_requests(request: Request, order_no: str):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("请先登录", "unauthorized"))
    order = orders.find(order_no, user_id=who["id"])
    if not order:
        return JSONResponse(status_code=404, content=err("订单不存在或不属于当前账号", "not_found"))
    return ok(refunds.for_order(order["id"]))


@router.post("/orders/{order_no}/refund-requests")
def create_refund(request: Request, order_no: str, payload: dict):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("请先登录", "unauthorized"))
    if _refund_limited(who["id"]):
        return JSONResponse(status_code=429, content=err("提交太频繁，请稍后再试", "rate_limited"))
    try:
        row = refunds.request(
            order_no,
            user_id=who["id"],
            kind=payload.get("kind") or "refund",
            item_ids=payload.get("item_ids"),
            amount_cents=payload.get("amount_cents"),
            reason=payload.get("reason") or "",
            detail=payload.get("detail") or "",
        )
        return JSONResponse(status_code=201, content=ok(row))
    except checkout.CheckoutError as exc:
        return fail(exc)


@router.delete("/orders/{order_no}/refund-requests/{request_id}")
def cancel_refund(request: Request, order_no: str, request_id: str):
    who = current_user(request)
    if not who:
        return JSONResponse(status_code=401, content=err("请先登录", "unauthorized"))
    try:
        return ok(refunds.cancel(request_id, who["id"]))
    except checkout.CheckoutError as exc:
        return fail(exc)
