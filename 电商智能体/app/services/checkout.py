"""结算：报价 → 下单锁库存 → 支付确认，全程一个事务内完成。"""

from __future__ import annotations

import datetime as dt
import json

from .. import db
from ..security import new_id, order_no
from ..settings import get_settings
from . import cart as cart_service
from . import inventory


class CheckoutError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def plans() -> list[dict]:
    rows = db.all_("SELECT * FROM shipping_plans WHERE active = 1 ORDER BY sort ASC")
    return [
        {
            "code": r["code"],
            "name": r["name"],
            "description": r.get("description"),
            "eta": r.get("eta"),
            "flat_cents": int(r["flat_cents"]),
            "free_over_cents": int(r["free_over_cents"]) if r.get("free_over_cents") is not None else None,
            "weight_limit_gram": int(r["weight_limit_gram"]) if r.get("weight_limit_gram") else None,
        }
        for r in rows
    ]


PROVIDERS = [
    {"code": "alipay", "name": "支付宝", "note": "演示环境不真实扣款"},
    {"code": "wechat", "name": "微信支付", "note": "演示环境不真实扣款"},
    {"code": "card", "name": "银行卡", "note": "尾号 0002 模拟风控失败"},
    {"code": "balance", "name": "钱包余额", "note": "在账户中心充值后可用"},
]


def providers() -> list[dict]:
    return PROVIDERS


def provider_name(code: str) -> str:
    return next((p["name"] for p in PROVIDERS if p["code"] == code), code or "线下")


def settings() -> dict:
    s = get_settings()
    rows = {r["key"]: r["value"] for r in db.all_("SELECT `key`, `value` FROM settings")}

    def num(key: str, fallback):
        try:
            return type(fallback)(rows.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return {
        "store_name": rows.get("store_name", s.store_name),
        "support_email": rows.get("support_email", s.support_email),
        "announcement": rows.get("announcement", ""),
        "tax_rate": num("tax_rate", s.tax_rate),
        "free_shipping_threshold_cents": num("free_shipping_threshold_cents", s.free_shipping_threshold_cents),
        "low_stock_threshold": num("low_stock_threshold", 5),
        "currency": s.currency,
        "order_hold_minutes": s.order_hold_minutes,
    }


def _coupon_row(code: str) -> dict | None:
    return db.get("SELECT * FROM coupons WHERE code = :code AND status = 'active'", {"code": (code or "").strip().upper()})


def coupon_quote(code: str, subtotal_cents: int, items: list[dict], *, user_id: str | None = None) -> dict:
    coupon = _coupon_row(code)
    if not coupon:
        raise CheckoutError("优惠码不可用或已过期")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    starts = _as_naive(coupon["starts_at"])
    ends = _as_naive(coupon["ends_at"])
    if starts and now < starts:
        raise CheckoutError("优惠码还没到生效时间")
    if ends and now > ends:
        raise CheckoutError("优惠码已过期")
    if coupon["usage_limit"] is not None and int(coupon["used_count"]) >= int(coupon["usage_limit"]):
        raise CheckoutError("优惠码已被领完")
    if subtotal_cents < int(coupon["min_subtotal_cents"]):
        raise CheckoutError(f"满 {db.cents_to_yuan(coupon['min_subtotal_cents'])} 元才能用这个优惠码")
    if user_id and int(coupon["per_user_limit"]) > 0:
        used = int(
            db.value(
                "SELECT COUNT(*) FROM coupon_redemptions r JOIN orders o ON o.id = r.order_id WHERE r.coupon_id = :cid AND o.user_id = :uid AND o.status <> 'cancelled'",
                {"cid": coupon["id"], "uid": user_id},
            )
            or 0
        )
        if used >= int(coupon["per_user_limit"]):
            raise CheckoutError("这个优惠码你的账号已经用过了")

    scope = _parse(coupon.get("applies_to"))
    eligible = _eligible_cents(items, scope)
    kind = coupon["kind"]
    if kind == "percent":
        discount = min(int(eligible * int(coupon["value"]) / 100_00), int(coupon["max_discount_cents"] or eligible))
    elif kind == "fixed":
        discount = min(int(coupon["value"]), eligible)
    else:  # shipping：只免运费，不打进商品折扣
        discount = 0
    return {
        "code": coupon["code"],
        "coupon_id": coupon["id"],
        "kind": kind,
        "discount_cents": max(0, discount),
        "free_shipping": bool(coupon["free_shipping"]) or kind == "shipping",
        "label": _coupon_label(coupon),
    }


def _coupon_label(coupon: dict) -> str:
    threshold = f"满 {db.money(coupon['min_subtotal_cents'])} " if int(coupon["min_subtotal_cents"]) else "无门槛 "
    if coupon["kind"] == "percent":
        return f"{threshold}立减 {round(int(coupon['value']) / 100)}%"
    if coupon["kind"] == "fixed":
        return f"{threshold}立减 {db.money(coupon['value'])}"
    return f"{threshold}免运费"


def active_coupons() -> list[dict]:
    rows = db.all_(
        "SELECT * FROM coupons WHERE status = 'active' AND (ends_at IS NULL OR ends_at > UTC_TIMESTAMP()) ORDER BY created_at DESC"
    )
    return [{"code": r["code"], "label": _coupon_label(r), "kind": r["kind"], "min_subtotal_cents": int(r["min_subtotal_cents"])} for r in rows]


def _parse(value):
    if value is None or isinstance(value, (dict, list)):
        return value or {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _as_naive(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "").replace("T", " "))
    return value.replace(tzinfo=None) if value.tzinfo else value


def _eligible_cents(items: list[dict], scope: dict) -> int:
    category_ids = {int(c) for c in (scope or {}).get("category_ids", [])}
    product_ids = {int(p) for p in (scope or {}).get("product_ids", [])}
    if not category_ids and not product_ids:
        return sum(int(i["line_total_cents"]) for i in items)
    total = 0
    for item in items:
        pid = int(item["product_id"])
        cid = int(db.value("SELECT category_id FROM products WHERE id = :pid", {"pid": pid}) or 0)
        if pid in product_ids or cid in category_ids:
            total += int(item["line_total_cents"])
    return total


def quote(*, cart: dict, plan_code: str = "STANDARD", coupon_code: str = "", user_id: str | None = None) -> dict:
    """报价单：小结算页与结算页共用，保证两处数字一致。"""
    if not cart["items"]:
        raise CheckoutError("购物车是空的")
    subtotal = int(cart["subtotal_cents"])
    plan = next((p for p in plans() if p["code"] == (plan_code or "STANDARD")), None)
    if not plan:
        raise CheckoutError("配送方案不存在")
    threshold = settings()["free_shipping_threshold_cents"]
    shipping = int(plan["flat_cents"])
    if plan["free_over_cents"] is not None and subtotal >= plan["free_over_cents"]:
        shipping = 0
    elif plan_code == "STANDARD" and subtotal >= threshold:
        shipping = 0

    coupon = coupon_quote(coupon_code, subtotal, cart["items"], user_id=user_id) if coupon_code else None
    discount = int(coupon["discount_cents"]) if coupon else 0
    if coupon and coupon["free_shipping"]:
        shipping = 0
    tax_base = max(0, subtotal - discount)
    tax = round(tax_base * settings()["tax_rate"])
    total = tax_base + shipping + tax
    return {
        "items": cart["items"],
        "count": cart["count"],
        "subtotal_cents": subtotal,
        "discount_cents": discount,
        "shipping_cents": shipping,
        "tax_cents": tax,
        "total_cents": total,
        "plan": plan,
        "coupon": coupon,
        "currency": cart.get("currency", "CNY"),
    }


def place(*, cart: dict, contact: dict, address: dict, plan_code: str = "STANDARD", coupon_code: str = "", note: str = "", user: dict | None = None) -> dict:
    priced = quote(cart=cart, plan_code=plan_code, coupon_code=coupon_code, user_id=(user or {}).get("id"))
    for item in cart["items"]:
        if inventory.available(int(item["variant_id"])) < int(item["qty"]):
            raise CheckoutError(f"「{item['title']}」库存不足，请减少数量")

    now = dt.datetime.now(dt.timezone.utc)
    iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    oid = new_id("ord")
    no = order_no()
    expires = (now + dt.timedelta(minutes=settings()["order_hold_minutes"])).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    shipping_snapshot = {k: address.get(k) for k in ("receiver", "phone", "province", "city", "district", "detail", "postal", "tag")}

    with db.tx() as tx:
        tx.insert(
            "orders",
            {
                "id": oid,
                "order_no": no,
                "user_id": (user or {}).get("id"),
                "status": "pending_payment",
                "payment_status": "unpaid",
                "fulfillment_status": "reserved",
                "email": contact.get("email") or (user or {}).get("email"),
                "phone": contact.get("phone") or shipping_snapshot.get("phone"),
                "shipping_address": shipping_snapshot,
                "shipping_plan_code": plan_code,
                "subtotal_cents": priced["subtotal_cents"],
                "discount_cents": priced["discount_cents"],
                "shipping_cents": priced["shipping_cents"],
                "tax_cents": priced["tax_cents"],
                "total_cents": priced["total_cents"],
                "coupon_code": (priced["coupon"] or {}).get("code"),
                "buyer_note": note[:500] or None,
                "expires_at": expires,
                "placed_at": iso,
                "updated_at": iso,
            },
        )
        for item in cart["items"]:
            tx.insert(
                "order_items",
                {
                    "order_id": oid,
                    "product_id": int(item["product_id"]),
                    "variant_id": int(item["variant_id"]),
                    "title": item["title"],
                    "variant_title": item["variant_title"],
                    "sku": tx.scalar("SELECT sku FROM variants WHERE id = :vid", {"vid": int(item["variant_id"])}),
                    "image_url": item["image"],
                    "unit_price_cents": int(item["unit_price_cents"]),
                    "qty": int(item["qty"]),
                },
            )
            tx.execute(
                "UPDATE variants SET reserved_qty = reserved_qty + :qty WHERE id = :vid AND stock_qty - reserved_qty >= :qty",
                {"qty": int(item["qty"]), "vid": int(item["variant_id"])},
            )
            if tx.scalar("SELECT ROW_COUNT()") == 0:
                raise CheckoutError(f"「{item['title']}」刚被抢完了，请调整数量")
            tx.insert(
                "reservations",
                {"order_id": oid, "variant_id": int(item["variant_id"]), "qty": int(item["qty"]), "status": "active", "created_at": iso},
            )
        tx.insert("order_events", {"order_id": oid, "kind": "created", "message": "订单已创建，等待支付", "actor": (user or {}).get("id"), "created_at": iso})
        if priced["coupon"]:
            tx.insert(
                "coupon_redemptions",
                {"coupon_id": priced["coupon"]["coupon_id"], "order_id": oid, "user_id": (user or {}).get("id"), "discount_cents": priced["discount_cents"], "created_at": iso},
            )
            tx.execute("UPDATE coupons SET used_count = used_count + 1 WHERE id = :cid", {"cid": priced["coupon"]["coupon_id"]})

    cart_service.clear(cart["cart_id"])
    from . import orders

    return orders.find(no, user_id=(user or {}).get("id"), is_admin=True)


def pay(*, order_ref: str, provider: str, user: dict | None = None) -> dict:
    from . import orders

    order = orders.find(order_ref, user_id=(user or {}).get("id"), is_admin=user is None or user.get("role") == "admin")
    if not order:
        raise CheckoutError("订单不存在", 404)
    if order["payment_status"] == "paid":
        raise CheckoutError("这笔订单已经支付过了")
    if order["status"] in ("cancelled", "expired"):
        raise CheckoutError("订单已关闭，请重新下单")

    iso = db.now_iso()
    pid = new_id("pay")
    if provider == "balance":
        balance = int((user or {}).get("balance_cents") or 0)
        if balance < int(order["total_cents"]):
            raise CheckoutError("钱包余额不足，请先充值或换一种支付方式")

    with db.tx() as tx:
        tx.insert(
            "payments",
            {
                "id": pid,
                "order_id": order["id"],
                "provider": provider,
                "kind": "sale",
                "amount_cents": int(order["total_cents"]),
                "currency": "CNY",
                "status": "confirmed",
                "gateway_ref": f"DEMO{pid[-8:].upper()}",
                "created_at": iso,
                "confirmed_at": iso,
            },
        )
        for item in order["items"]:
            tx.execute(
                "UPDATE variants SET stock_qty = GREATEST(0, stock_qty - :qty), reserved_qty = GREATEST(0, reserved_qty - :qty), updated_at = :now WHERE id = :vid",
                {"qty": int(item["qty"]), "vid": int(item["variant_id"]), "now": iso},
            )
            product_id = tx.scalar("SELECT product_id FROM variants WHERE id = :vid", {"vid": int(item["variant_id"])})
            tx.execute("UPDATE products SET sold_count = sold_count + :qty, updated_at = :now WHERE id = :pid", {"qty": int(item["qty"]), "now": iso, "pid": int(product_id)})
            tx.insert(
                "inventory_movements",
                {"variant_id": int(item["variant_id"]), "delta": -int(item["qty"]), "reason": "sale", "reference_type": "order", "reference_id": order["id"], "created_at": iso},
            )
        tx.execute("UPDATE reservations SET status = 'consumed', resolved_at = :now WHERE order_id = :oid AND status = 'active'", {"now": iso, "oid": order["id"]})
        tx.execute(
            "UPDATE orders SET status = 'paid', payment_status = 'paid', fulfillment_status = 'picked', paid_cents = :amount, paid_at = :now, updated_at = :now WHERE id = :oid",
            {"amount": int(order["total_cents"]), "now": iso, "oid": order["id"]},
        )
        tx.insert("order_events", {"order_id": order["id"], "kind": "paid", "message": f"{provider_name(provider)} 支付成功", "actor": (user or {}).get("id"), "created_at": iso})
        if provider == "balance":
            amount = int(order["total_cents"])
            tx.execute("UPDATE users SET balance_cents = balance_cents - :amount WHERE id = :uid", {"amount": amount, "uid": user["id"]})
            tx.insert(
                "balance_entries",
                {
                    "id": new_id("bal"),
                    "user_id": user["id"],
                    "amount_cents": -amount,
                    "balance_after_cents": tx.scalar("SELECT balance_cents FROM users WHERE id = :uid", {"uid": user["id"]}),
                    "kind": "payment",
                    "reference": order["order_no"],
                    "note": "订单支付",
                    "created_at": iso,
                },
            )
    return orders.detail(db.get("SELECT * FROM orders WHERE id = :oid", {"oid": order["id"]}))


def cancel(*, order_ref: str, user: dict | None = None, reason: str = "用户取消") -> dict:
    from . import orders

    order = orders.find(order_ref, user_id=(user or {}).get("id"), is_admin=False)
    if not order:
        raise CheckoutError("订单不存在或不属于当前账号", 404)
    if order["status"] != "pending_payment":
        raise CheckoutError("只有待付款订单可以直接取消，已支付的请发起退款申请")
    iso = db.now_iso()
    with db.tx() as tx:
        for item in order["items"]:
            tx.execute("UPDATE variants SET reserved_qty = GREATEST(0, reserved_qty - :qty) WHERE id = :vid", {"qty": int(item["qty"]), "vid": int(item["variant_id"])})
        tx.execute("UPDATE reservations SET status = 'released', resolved_at = :now WHERE order_id = :oid AND status = 'active'", {"now": iso, "oid": order["id"]})
        tx.execute(
            "UPDATE orders SET status = 'cancelled', fulfillment_status = 'cancelled', cancel_reason = :reason, cancelled_at = :now, updated_at = :now WHERE id = :oid",
            {"reason": reason[:255], "now": iso, "oid": order["id"]},
        )
        if order.get("coupon_code"):
            tx.execute("UPDATE coupons SET used_count = GREATEST(0, used_count - 1) WHERE code = :code", {"code": order["coupon_code"]})
        tx.insert("order_events", {"order_id": order["id"], "kind": "cancelled", "message": reason[:200], "actor": (user or {}).get("id"), "created_at": iso})
    return orders.detail(db.get("SELECT * FROM orders WHERE id = :oid", {"oid": order["id"]}))


def release_expired() -> int:
    """超时未付的订单自动关闭并释放库存，由 `app.main` 里的扫描循环按 `ORDER_SWEEP_SECONDS` 调用。"""
    iso = db.now_iso()
    rows = db.all_("SELECT id, order_no, coupon_code FROM orders WHERE status = 'pending_payment' AND expires_at < UTC_TIMESTAMP()")
    for row in rows:
        with db.tx() as tx:
            for item in tx.rows("SELECT variant_id, qty FROM order_items WHERE order_id = :oid", {"oid": row["id"]}):
                tx.execute("UPDATE variants SET reserved_qty = GREATEST(0, reserved_qty - :qty) WHERE id = :vid", {"qty": int(item["qty"]), "vid": int(item["variant_id"])})
            tx.execute("UPDATE reservations SET status = 'released', resolved_at = :now WHERE order_id = :oid AND status = 'active'", {"now": iso, "oid": row["id"]})
            tx.execute("UPDATE orders SET status = 'expired', fulfillment_status = 'cancelled', updated_at = :now WHERE id = :oid", {"now": iso, "oid": row["id"]})
            if row.get("coupon_code"):
                tx.execute("UPDATE coupons SET used_count = GREATEST(0, used_count - 1) WHERE code = :code", {"code": row["coupon_code"]})
            tx.insert("order_events", {"order_id": row["id"], "kind": "expired", "message": "超时未支付，订单自动关闭", "created_at": iso})
    return len(rows)
