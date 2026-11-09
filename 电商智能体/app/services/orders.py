"""订单查询：前台「我的订单」、后台列表与小马的订单工具共用同一份口径。"""

from __future__ import annotations

from .. import db

STATUS_TEXT = {
    "pending_payment": "待付款",
    "paid": "待发货",
    "shipped": "已发货",
    "delivered": "已送达",
    "completed": "交易完成",
    "cancelled": "已取消",
    "expired": "已过期",
    "refunded": "已退款",
}

PAYMENT_TEXT = {"unpaid": "未支付", "paid": "已支付", "partially_refunded": "部分退款", "refunded": "已退款"}
FULFIL_TEXT = {"unfulfilled": "未出库", "reserved": "已锁库存", "picked": "拣货中", "shipped": "已出库", "delivered": "已送达", "cancelled": "已取消"}


def _items(order_id: str) -> list[dict]:
    rows = db.all_("SELECT * FROM order_items WHERE order_id = :oid ORDER BY id ASC", {"oid": order_id})
    return [
        {
            "id": int(r["id"]),
            "product_id": int(r["product_id"]) if r.get("product_id") else None,
            "variant_id": int(r["variant_id"]) if r.get("variant_id") else None,
            "title": r["title"],
            "variant_title": r.get("variant_title"),
            "sku": r.get("sku"),
            "image": r.get("image_url"),
            "unit_price_cents": int(r["unit_price_cents"]),
            "qty": int(r["qty"]),
            "refunded_qty": int(r["refunded_qty"]),
        }
        for r in rows
    ]


def detail(order: dict) -> dict:
    oid = order["id"]
    events = db.all_("SELECT * FROM order_events WHERE order_id = :oid ORDER BY id ASC", {"oid": oid})
    payments = db.all_("SELECT * FROM payments WHERE order_id = :oid ORDER BY created_at ASC", {"oid": oid})
    refunds = db.all_("SELECT * FROM refund_requests WHERE order_id = :oid ORDER BY created_at DESC", {"oid": oid})
    out = dict(order)
    out["items"] = _items(oid)
    out["events"] = [{"kind": e["kind"], "message": e["message"], "actor": e.get("actor"), "created_at": e["created_at"]} for e in events]
    out["payments"] = [
        {
            "id": p["id"],
            "provider": p["provider"],
            "kind": p["kind"],
            "amount_cents": int(p["amount_cents"]),
            "status": p["status"],
            "created_at": p["created_at"],
            "confirmed_at": p.get("confirmed_at"),
        }
        for p in payments
    ]
    out["refunds"] = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "amount_cents": int(r["amount_cents"]),
            "reason": r["reason"],
            "status": r["status"],
            "created_at": r["created_at"],
            "handled_at": r.get("handled_at"),
        }
        for r in refunds
    ]
    out["status_text"] = STATUS_TEXT.get(out["status"], out["status"])
    out["payment_status_text"] = PAYMENT_TEXT.get(out.get("payment_status"), out.get("payment_status"))
    out["fulfillment_status_text"] = FULFIL_TEXT.get(out.get("fulfillment_status"), out.get("fulfillment_status"))
    out["refundable_cents"] = int(out.get("paid_cents") or 0) - int(out.get("refunded_cents") or 0)
    return out


_SELECT = "SELECT * FROM orders"


def find(ref: str, *, user_id: str | None = None, is_admin: bool = False) -> dict | None:
    """支持订单号与内部 id 两种写法；非管理员只能看到自己账号的单，游客只能看游客单。"""
    row = db.get(f"{_SELECT} WHERE order_no = :ref OR id = :ref", {"ref": str(ref)})
    if not row:
        return None
    if not is_admin and (row.get("user_id") or "") != (user_id or ""):
        return None
    return detail(row)


def customer_orders(user_id: str, *, limit: int = 20, status: str = "") -> list[dict]:
    clause = "AND status = :status" if status else ""
    rows = db.all_(
        f"{_SELECT} WHERE user_id = :uid {clause} ORDER BY placed_at DESC LIMIT :limit",
        {"uid": user_id, "status": status, "limit": int(limit)},
    )
    out = []
    for row in rows:
        card = summarize(row)
        out.append(card)
    return out


def summarize(order: dict) -> dict:
    return {
        "id": order["id"],
        "order_no": order["order_no"],
        "status": order["status"],
        "status_text": STATUS_TEXT.get(order["status"], order["status"]),
        "payment_status": order.get("payment_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "total_cents": int(order["total_cents"]),
        "paid_cents": int(order.get("paid_cents") or 0),
        "refunded_cents": int(order.get("refunded_cents") or 0),
        "discount_cents": int(order.get("discount_cents") or 0),
        "shipping_cents": int(order.get("shipping_cents") or 0),
        "tax_cents": int(order.get("tax_cents") or 0),
        "placed_at": order["placed_at"],
        "tracking_no": order.get("tracking_no"),
        "carrier": order.get("carrier"),
        "coupon_code": order.get("coupon_code"),
        "items": _items(order["id"]),
    }


def list_admin(*, status: str = "", q: str = "", limit: int = 30, offset: int = 0) -> dict:
    where, params = [], {}
    if status:
        where.append("status = :status")
        params["status"] = status
    if q:
        where.append("(order_no LIKE :q OR email LIKE :q OR phone LIKE :q)")
        params["q"] = f"%{q}%"
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = int(db.value(f"SELECT COUNT(*) FROM orders {clause}", params) or 0)
    rows = db.all_(f"{_SELECT} {clause} ORDER BY placed_at DESC LIMIT :limit OFFSET :offset", {**params, "limit": int(limit), "offset": int(offset)})
    return {"items": [summarize(r) for r in rows], "total": total}


def stats() -> dict:
    return {
        "orders": int(db.value("SELECT COUNT(*) FROM orders") or 0),
        "paid": int(db.value("SELECT COUNT(*) FROM orders WHERE payment_status = 'paid'") or 0),
        "gmv_cents": int(db.value("SELECT IFNULL(SUM(paid_cents), 0) FROM orders WHERE payment_status = 'paid'") or 0),
        "pending_payment": int(db.value("SELECT COUNT(*) FROM orders WHERE status = 'pending_payment'") or 0),
        "open_refunds": int(db.value("SELECT COUNT(*) FROM refund_requests WHERE status = 'pending'") or 0),
    }
