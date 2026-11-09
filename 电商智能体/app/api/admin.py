"""后台：商品、库存、订单、知识库与小马会话记录。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import db
from ..agent import store as agent_store
from ..deps import clear_session_cookie, client_ip, current_user, err, ok, set_session_cookie, user_agent
from ..services import auth, catalog, checkout, inventory, orders, refunds, wallet
from ..settings import get_settings


def require_admin(request: Request) -> dict:
    who = current_user(request)
    if not who or who.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return who


# 整组接口默认过权限，之后新增路由不会漏掉鉴权
router = APIRouter(dependencies=[Depends(require_admin)])

# 登录与登出另起一张路由：同一前缀下不能过 require_admin，否则谁都登不进来
public_router = APIRouter()


@public_router.post("/login")
def admin_login(request: Request, response: Response, payload: dict):
    """在后台地址上直接用管理员账号登录，不用先绕一圈前台。"""
    try:
        user, token = auth.login(
            email=payload.get("email", ""),
            password=payload.get("password", ""),
            guest_token=None,
            agent=user_agent(request),
            ip=client_ip(request),
        )
    except auth.AuthError as exc:
        return JSONResponse(status_code=exc.status, content=err(exc.message))
    if user.get("role") != "admin":
        # 校验密码时已经开了一条会话；买家账号不该在后台端口上留下能用的会话
        auth.logout(token)
        clear_session_cookie(response)
        return JSONResponse(status_code=403, content=err("这个账号不是管理员"))
    set_session_cookie(response, token)
    return ok(auth.public(user))


@public_router.post("/logout")
def admin_logout(request: Request, response: Response):
    token = request.cookies.get(get_settings().cookie_name)
    if token:
        auth.logout(token)
    clear_session_cookie(response)
    return ok({"logged_out": True})


@router.get("/overview")
def overview():
    return ok({"orders": orders.stats(), "agent": agent_store.stats(), "tool_usage": agent_store.tool_stats(), "low_stock": inventory.low_stock(limit=6)})


@router.get("/products")
def list_products(q: str = "", status: str = "", page: int = 1, per_page: int = 20):
    return ok(catalog.search(q=q, status=status or "", page=page, per_page=per_page))


@router.put("/products/{product_id}")
def update_product(who: dict = Depends(require_admin), product_id: int = 0, payload: dict = None):
    allowed = {"title", "subtitle", "brand", "description", "status", "featured"}
    changes = {k: v for k, v in (payload or {}).items() if k in allowed}
    if not changes:
        return JSONResponse(status_code=400, content=err("没有可更新的字段"))
    changes["updated_at"] = db.now_iso()
    db.update("products", changes, "id = :pid", {"pid": int(product_id)})
    db.insert(
        "audit_logs",
        {
            "user_id": who["id"],
            "actor_type": "admin",
            "action": "product.update",
            "entity": "product",
            "entity_id": str(product_id),
            "meta": {"fields": list(changes)},
            "created_at": db.now_iso(),
        },
    )
    return ok(db.get("SELECT * FROM products WHERE id = :pid", {"pid": int(product_id)}))


@router.post("/inventory/adjust")
def adjust_inventory(who: dict = Depends(require_admin), payload: dict = None):
    payload = payload or {}
    return ok(
        inventory.adjust(
            int(payload.get("variant_id", 0)),
            int(payload.get("delta", 0)),
            reason=payload.get("reason") or "manual",
            actor=who["id"],
            note=payload.get("note") or "",
        )
    )


@router.get("/inventory/movements")
def inventory_movements(limit: int = 30):
    return ok(inventory.movements(limit=limit))


@router.get("/variants")
def list_variants(q: str = "", product_id: int = 0, limit: int = 40):
    return ok(inventory.search_variants(q=q, product_id=product_id, limit=limit))


@router.put("/variants/{variant_id}")
def update_variant(who: dict = Depends(require_admin), variant_id: int = 0, payload: dict = None):
    try:
        row = inventory.update_variant(int(variant_id), payload or {}, actor=who["id"])
    except checkout.CheckoutError as exc:
        return JSONResponse(status_code=exc.status, content=err(exc.message))
    _audit(who, "admin.variant_update", "variant", str(variant_id), {"fields": list((payload or {}).keys())})
    return ok(row)


@router.get("/orders")
def admin_orders(status: str = "", q: str = "", limit: int = 30, offset: int = 0):
    return ok(orders.list_admin(status=status, q=q, limit=limit, offset=offset))


@router.get("/faqs")
def faqs():
    return ok(db.all_("SELECT * FROM support_faqs ORDER BY sort ASC, id ASC"))


@router.post("/faqs")
def save_faq(who: dict = Depends(require_admin), payload: dict = None):
    payload = payload or {}
    now = db.now_iso()
    fields = {
        "question": str(payload.get("question", ""))[:200],
        "keywords": str(payload.get("keywords", ""))[:255],
        "answer": str(payload.get("answer", ""))[:2000],
        "kind": payload.get("kind") if payload.get("kind") in ("shipping", "refund", "payment", "account", "promo", "other") else "other",
        "sort": int(payload.get("sort") or 100),
        "active": 1 if payload.get("active", True) else 0,
        "updated_by": who["id"],
        "updated_at": now,
    }
    if not fields["question"] or not fields["answer"]:
        return JSONResponse(status_code=400, content=err("问题与答复都要填"))
    if payload.get("id"):
        db.update("support_faqs", fields, "id = :id", {"id": int(payload["id"])})
    else:
        fields.update({"created_at": now, "hits": 0})
        db.insert("support_faqs", fields)
    return ok(db.all_("SELECT * FROM support_faqs ORDER BY sort ASC, id ASC"))


@router.delete("/faqs/{faq_id}")
def delete_faq(faq_id: int):
    db.run("DELETE FROM support_faqs WHERE id = :id", {"id": int(faq_id)})
    return ok({"deleted": faq_id})


@router.get("/agent/threads")
def agent_threads(q: str = "", status: str = "", limit: int = 25, offset: int = 0):
    return ok(agent_store.list_threads(q=q, status=status, limit=limit, offset=offset))


@router.get("/agent/threads/{thread_id}")
def agent_thread(thread_id: str):
    return ok(
        {
            "thread": db.get("SELECT * FROM agent_threads WHERE id = :tid", {"tid": thread_id}),
            "messages": agent_store.history(thread_id, limit=100),
            "tool_calls": [
                {**c, "label": agent_store.tool_text(c["tool"])}
                for c in db.all_("SELECT * FROM agent_tool_calls WHERE thread_id = :tid ORDER BY id DESC LIMIT 40", {"tid": thread_id})
            ],
            # 用户在这一段会话里传过的图：按图找相似的复盘看的就是这个
            "images": db.all_(
                "SELECT token, url, filename, mime, bytes, width, height, created_at FROM agent_images "
                "WHERE thread_id = :tid ORDER BY id",
                {"tid": thread_id},
            ),
        }
    )


@router.get("/settings")
def store_settings():
    return ok(checkout.settings())


def _audit(who: dict, action: str, entity: str, entity_id: str, meta: dict) -> None:
    db.insert(
        "audit_logs",
        {"user_id": who["id"], "actor_type": "admin", "action": action, "entity": entity, "entity_id": entity_id, "meta": meta, "created_at": db.now_iso()},
    )


@router.get("/refunds")
def refund_queue(status: str = "", q: str = "", limit: int = 30, offset: int = 0):
    return ok(refunds.list_requests(status=status, q=q, limit=limit, offset=offset))


@router.post("/refunds/{request_id}/approve")
def refund_approve(who: dict = Depends(require_admin), request_id: str = "", payload: dict = None):
    payload = payload or {}
    try:
        result = refunds.approve(request_id, admin_id=who["id"], amount_cents=payload.get("amount_cents"), note=payload.get("note") or "")
    except checkout.CheckoutError as exc:
        return JSONResponse(status_code=exc.status, content=err(exc.message))
    _audit(who, "admin.refund_approved", "refund_request", request_id, {"amount_cents": result["request"]["amount_cents"]})
    return ok(result)


@router.post("/refunds/{request_id}/reject")
def refund_reject(who: dict = Depends(require_admin), request_id: str = "", payload: dict = None):
    try:
        row = refunds.reject(request_id, admin_id=who["id"], note=(payload or {}).get("note") or "")
    except checkout.CheckoutError as exc:
        return JSONResponse(status_code=exc.status, content=err(exc.message))
    _audit(who, "admin.refund_rejected", "refund_request", request_id, {"note": row.get("admin_note")})
    return ok(row)


@router.post("/orders/{order_no}/fulfill")
def fulfill(who: dict = Depends(require_admin), order_no: str = "", payload: dict = None):
    payload = payload or {}
    status = payload.get("status")
    if status not in ("shipped", "delivered", "completed"):
        return JSONResponse(status_code=400, content=err("只能流转到已发货、已送达或交易完成"))
    order = orders.find(order_no, is_admin=True)
    if not order:
        return JSONResponse(status_code=404, content=err("订单不存在", "not_found"))
    if order["payment_status"] != "paid":
        return JSONResponse(status_code=400, content=err("订单尚未支付，不能出库"))
    if status == "shipped" and not (payload.get("tracking_no") or "").strip():
        return JSONResponse(status_code=400, content=err("发货要填运单号"))
    now = db.now_iso()
    changes = {"status": status, "updated_at": now}
    if status == "shipped":
        changes.update({"carrier": (payload.get("carrier") or "顺丰速运")[:40], "tracking_no": payload["tracking_no"].strip()[:64], "fulfillment_status": "shipped"})
    if status == "delivered":
        changes["fulfillment_status"] = "delivered"
    db.update("orders", changes, "id = :oid", {"oid": order["id"]})
    db.insert("order_events", {"order_id": order["id"], "kind": status, "message": payload.get("note") or f"商家标记为{orders.STATUS_TEXT[status]}", "actor": who["id"], "created_at": now})
    _audit(who, "admin.order_fulfilled", "order", order["id"], {"status": status, "tracking_no": changes.get("tracking_no")})
    return ok(orders.detail(db.get("SELECT * FROM orders WHERE id = :oid", {"oid": order["id"]})))


@router.get("/members")
def members(q: str = "", limit: int = 30, offset: int = 0):
    where, params = [], {"limit": int(limit), "offset": int(offset)}
    if q:
        where.append("(email LIKE :q OR name LIKE :q OR phone LIKE :q)")
        params["q"] = f"%{q}%"
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = int(db.value(f"SELECT COUNT(*) FROM users {clause}", {k: v for k, v in params.items() if k == "q"}) or 0)
    rows = db.all_(
        f"""SELECT u.id, u.email, u.name, u.phone, u.role, u.status, u.balance_cents, u.created_at, u.last_login_at,
                   (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id) AS orders,
                   (SELECT IFNULL(SUM(o.paid_cents), 0) FROM orders o WHERE o.user_id = u.id AND o.payment_status = 'paid') AS paid_cents
            FROM users u {clause} ORDER BY u.created_at DESC LIMIT :limit OFFSET :offset""",
        params,
    )
    return ok({"items": rows, "total": total})


@router.post("/members/{user_id}/balance")
def adjust_balance(who: dict = Depends(require_admin), user_id: str = "", payload: dict = None):
    payload = payload or {}
    if not db.get("SELECT id FROM users WHERE id = :uid", {"uid": user_id}):
        return JSONResponse(status_code=404, content=err("会员不存在", "not_found"))
    try:
        entry = wallet.adjust(
            user_id,
            int(payload.get("amount_cents") or 0),
            kind=payload.get("kind") or "admin_adjust",
            note=payload.get("note") or "后台调整",
        )
    except (checkout.CheckoutError, TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content=err(getattr(exc, "message", "金额格式不正确")))
    _audit(who, "admin.balance_adjusted", "user", user_id, {"amount_cents": entry["amount_cents"], "note": entry["note"]})
    return ok({"entry": entry, "balance_cents": wallet.balance_of(user_id)})
