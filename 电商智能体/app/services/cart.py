"""购物车：登录用户与游客共用一张表，登录后游客车并入用户车。"""

from __future__ import annotations

from .. import db
from ..security import new_id
from ..settings import get_settings
from . import inventory


class CartError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def find(user_id: str | None, guest_token: str | None) -> dict | None:
    if user_id:
        return db.get("SELECT * FROM carts WHERE user_id = :uid", {"uid": user_id})
    if guest_token:
        return db.get("SELECT * FROM carts WHERE guest_token = :guest", {"guest": guest_token})
    return None


def ensure(user_id: str | None, guest_token: str | None) -> tuple[dict, str | None]:
    """返回 (购物车, 新的游客令牌)。游客第一次加购时分配令牌。"""
    cart = find(user_id, guest_token)
    if cart:
        return cart, None
    now = db.now_iso()
    cart_id = new_id("crt")
    payload = {"id": cart_id, "currency": get_settings().currency, "created_at": now, "updated_at": now}
    if user_id:
        payload["user_id"] = user_id
    else:
        guest_token = guest_token or new_id("gst")
        payload.update({"guest_token": guest_token})
    db.insert("carts", payload)
    return db.get("SELECT * FROM carts WHERE id = :id", {"id": cart_id}), guest_token


def merge_guest(user_id: str, guest_token: str) -> None:
    guest_cart = db.get("SELECT * FROM carts WHERE guest_token = :guest", {"guest": guest_token})
    user_cart = db.get("SELECT * FROM carts WHERE user_id = :uid", {"uid": user_id})
    if not guest_cart:
        return
    if not user_cart:
        db.run("UPDATE carts SET user_id = :uid, guest_token = NULL WHERE id = :cid", {"uid": user_id, "cid": guest_cart["id"]})
        return
    # 用户已经有车：同规格并到一行、按可售数量封顶，不能直接改 cart_id（会撞 uq_cart_variant）
    limit = get_settings().max_qty_per_item
    for item in db.all_("SELECT * FROM cart_items WHERE cart_id = :cid", {"cid": guest_cart["id"]}):
        free = inventory.available(int(item["variant_id"]))
        target = db.get(
            "SELECT * FROM cart_items WHERE cart_id = :tcid AND variant_id = :vid",
            {"tcid": user_cart["id"], "vid": int(item["variant_id"])},
        )
        if target:
            qty = max(1, min(int(target["qty"]) + int(item["qty"]), free, limit))
            db.run("UPDATE cart_items SET qty = :qty WHERE id = :iid", {"qty": qty, "iid": int(target["id"])})
            db.run("DELETE FROM cart_items WHERE id = :iid", {"iid": int(item["id"])})
        else:
            db.run("UPDATE cart_items SET cart_id = :target WHERE id = :iid", {"target": user_cart["id"], "iid": int(item["id"])})
    db.run("DELETE FROM carts WHERE id = :cid", {"cid": guest_cart["id"]})
    db.run("UPDATE carts SET updated_at = :now WHERE id = :cid", {"now": db.now_iso(), "cid": user_cart["id"]})


def add(cart_id: str, variant_id: int, qty: int = 1) -> dict:
    variant = db.get(
        """SELECT v.*, p.title AS product_title, p.status AS product_status, p.id AS product_pid
           FROM variants v JOIN products p ON p.id = v.product_id WHERE v.id = :vid""",
        {"vid": int(variant_id)},
    )
    if not variant or variant["product_status"] != "active" or variant["status"] != "active":
        raise CartError("这个规格已经下架，换一款试试", 404)
    qty = max(1, int(qty or 1))
    existing = db.get("SELECT * FROM cart_items WHERE cart_id = :cid AND variant_id = :vid", {"cid": cart_id, "vid": int(variant_id)})
    want = qty + int(existing["qty"] if existing else 0)
    free = inventory.available(int(variant_id))
    if free <= 0:
        raise CartError("这件商品暂时售罄，可以先收藏或让小马帮你找替代")
    capped = min(want, free, get_settings().max_qty_per_item)
    if existing:
        db.run("UPDATE cart_items SET qty = :qty WHERE id = :iid", {"qty": capped, "iid": int(existing["id"])})
    else:
        db.insert(
            "cart_items",
            {
                "cart_id": cart_id,
                "product_id": int(variant["product_pid"]),
                "variant_id": int(variant_id),
                "qty": capped,
                "unit_price_cents": int(variant["price_cents"]),
                "added_at": db.now_iso(),
            },
        )
    db.run("UPDATE carts SET updated_at = :now WHERE id = :cid", {"now": db.now_iso(), "cid": cart_id})
    return view(cart_id)


def set_qty(cart_id: str, item_id: int, qty: int) -> dict:
    item = db.get("SELECT * FROM cart_items WHERE id = :iid AND cart_id = :cid", {"iid": int(item_id), "cid": cart_id})
    if not item:
        raise CartError("购物车里没有这件商品", 404)
    if qty <= 0:
        db.run("DELETE FROM cart_items WHERE id = :iid", {"iid": int(item_id)})
    else:
        capped = min(int(qty), inventory.available(int(item["variant_id"])), get_settings().max_qty_per_item)
        db.run("UPDATE cart_items SET qty = :qty WHERE id = :iid", {"qty": max(1, capped), "iid": int(item_id)})
    db.run("UPDATE carts SET updated_at = :now WHERE id = :cid", {"now": db.now_iso(), "cid": cart_id})
    return view(cart_id)


def remove(cart_id: str, item_id: int) -> dict:
    db.run("DELETE FROM cart_items WHERE id = :iid AND cart_id = :cid", {"iid": int(item_id), "cid": cart_id})
    db.run("UPDATE carts SET updated_at = :now WHERE id = :cid", {"now": db.now_iso(), "cid": cart_id})
    return view(cart_id)


def clear(cart_id: str) -> dict:
    db.run("DELETE FROM cart_items WHERE cart_id = :cid", {"cid": cart_id})
    db.run("UPDATE carts SET coupon_code = NULL, shipping_plan_code = NULL, updated_at = :now WHERE id = :cid", {"now": db.now_iso(), "cid": cart_id})
    return view(cart_id)


def set_coupon(cart_id: str, code: str | None) -> dict:
    db.run("UPDATE carts SET coupon_code = :code, updated_at = :now WHERE id = :cid", {"code": (code or None), "now": db.now_iso(), "cid": cart_id})
    return view(cart_id)


def set_plan(cart_id: str, plan_code: str | None) -> dict:
    db.run("UPDATE carts SET shipping_plan_code = :code, updated_at = :now WHERE id = :cid", {"code": (plan_code or None), "now": db.now_iso(), "cid": cart_id})
    return view(cart_id)


def view(cart_id: str) -> dict:
    rows = db.all_(
        """SELECT ci.*, v.title AS variant_title, v.options AS variant_options, v.status AS variant_status,
                  p.slug, p.title AS product_title, p.status AS product_status, p.compare_at_cents,
                  img.url AS image, (v.stock_qty - v.reserved_qty) AS available
           FROM cart_items ci
           JOIN variants v ON v.id = ci.variant_id
           JOIN products p ON p.id = ci.product_id
           LEFT JOIN (SELECT product_id, MIN(url) AS url FROM product_images GROUP BY product_id) img ON img.product_id = p.id
           WHERE ci.cart_id = :cid ORDER BY ci.id ASC""",
        {"cid": cart_id},
    )
    items = []
    subtotal = 0
    for r in rows:
        line = int(r["unit_price_cents"]) * int(r["qty"])
        subtotal += line
        options = r.get("variant_options")
        if isinstance(options, str):
            import json

            try:
                options = json.loads(options)
            except json.JSONDecodeError:
                options = {}
        items.append(
            {
                "id": int(r["id"]),
                "variant_id": int(r["variant_id"]),
                "product_id": int(r["product_id"]),
                "slug": r["slug"],
                "title": r["product_title"],
                "variant_title": r["variant_title"],
                "options": options or {},
                "image": r.get("image") or "/static/img/placeholder.svg",
                "unit_price_cents": int(r["unit_price_cents"]),
                "qty": int(r["qty"]),
                "line_total_cents": line,
                "available": int(r["available"] or 0),
                "on_sale": bool(r.get("compare_at_cents") and int(r["compare_at_cents"]) > int(r["unit_price_cents"])),
            }
        )
    cart = db.get("SELECT * FROM carts WHERE id = :cid", {"cid": cart_id}) or {"id": cart_id}
    return {
        "cart_id": cart_id,
        "items": items,
        "count": sum(i["qty"] for i in items),
        "subtotal_cents": subtotal,
        "coupon_code": cart.get("coupon_code"),
        "shipping_plan_code": cart.get("shipping_plan_code") or "STANDARD",
        "currency": cart.get("currency") or "CNY",
    }
