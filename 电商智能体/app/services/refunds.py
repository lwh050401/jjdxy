"""退款申请：买家发起、商家处理，钱货两失的回补都在一个事务里。"""

from __future__ import annotations

from .. import db
from ..security import new_id
from .checkout import CheckoutError

KINDS = ("refund", "return")
KIND_TEXT = {"refund": "仅退款", "return": "退货退款"}
STATUS_TEXT = {"pending": "待处理", "approved": "已退款", "rejected": "已驳回", "cancelled": "已撤销"}


def _decorate(row: dict | None) -> dict | None:
    if not row:
        return None
    out = dict(row)
    out["amount_cents"] = int(out["amount_cents"])
    out["kind_text"] = KIND_TEXT.get(out["kind"], out["kind"])
    out["status_text"] = STATUS_TEXT.get(out["status"], out["status"])
    return out


def for_order(order_id: str) -> list[dict]:
    rows = db.all_("SELECT * FROM refund_requests WHERE order_id = :oid ORDER BY created_at DESC", {"oid": order_id})
    return [_decorate(r) for r in rows]


def pending(order_id: str) -> dict | None:
    return db.get("SELECT * FROM refund_requests WHERE order_id = :oid AND status = 'pending' ORDER BY created_at DESC LIMIT 1", {"oid": order_id})


def _owned_order(order_ref: str, user_id: str | None):
    order = db.get("SELECT * FROM orders WHERE order_no = :ref OR id = :ref", {"ref": str(order_ref)})
    if not order:
        raise CheckoutError("订单不存在", 404)
    if order.get("user_id") != user_id:
        raise CheckoutError("无权操作该订单", 403)
    return order


def _resolve_items(order: dict, raw_ids) -> list[dict] | None:
    """明细必须属于本单且还有可退数量，否则退款金额与回补库存会对不上。"""
    if not raw_ids:
        return None
    wanted = [str(i.get("id") if isinstance(i, dict) else i) for i in raw_ids]
    rows = db.all_("SELECT * FROM order_items WHERE order_id = :oid ORDER BY id ASC", {"oid": order["id"]})
    picked = [r for r in rows if str(r["id"]) in wanted and int(r["qty"]) > int(r["refunded_qty"])]
    if len(picked) != len(wanted):
        raise CheckoutError("退款明细不属于该订单或已全额退款")
    return picked


def request(order_ref: str, *, user_id: str | None, kind: str = "refund", item_ids=None, amount_cents: int | None = None, reason: str = "", detail: str = "") -> dict:
    order = _owned_order(order_ref, user_id)
    if order["payment_status"] != "paid":
        raise CheckoutError("订单尚未支付，不需要退款，直接取消即可")
    if order["status"] in ("cancelled", "refunded"):
        raise CheckoutError("订单已关闭，无法再次申请退款")
    if pending(order["id"]):
        raise CheckoutError("该订单已有待处理的退款申请，请等待商家处理或先撤销")
    if kind not in KINDS:
        raise CheckoutError("退款类型只能是仅退款或退货退款")
    why = (reason or "").strip()
    if not why or len(why) > 200:
        raise CheckoutError("请填写退款原因（不超过 200 字）")
    refundable = int(order["paid_cents"] or 0) - int(order["refunded_cents"] or 0)
    if refundable <= 0:
        raise CheckoutError("该订单已无可退金额")
    items = _resolve_items(order, item_ids)
    if items is None and kind == "return":
        # 退货退款不挑明细就是整单退回，必须记下所有还可退的行，否则同意时不会回补库存
        items = _resolve_items(order, [r["id"] for r in db.all_(
            "SELECT id FROM order_items WHERE order_id = :oid AND qty > refunded_qty ORDER BY id", {"oid": order["id"]})])
    amount = int(amount_cents if amount_cents is not None else refundable)
    if amount <= 0:
        raise CheckoutError("退款金额必须大于 0")
    if amount > refundable:
        raise CheckoutError(f"退款金额超出可退额度（可退 {db.money(refundable)}）")

    rid, now = new_id("rfv"), db.now_iso()
    db.insert(
        "refund_requests",
        {
            "id": rid,
            "order_id": order["id"],
            "user_id": user_id,
            "kind": kind,
            "item_ids": [int(i["id"]) for i in items] if items else None,
            "amount_cents": amount,
            "reason": why,
            "detail": (detail or "").strip()[:500] or None,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        },
    )
    _add_event(order["id"], "refund_request", f"买家发起{KIND_TEXT[kind]}申请 {db.money(amount)}：{why}", "customer")
    return _decorate(db.get("SELECT * FROM refund_requests WHERE id = :rid", {"rid": rid}))


def cancel(request_id: str, user_id: str | None) -> dict:
    row = db.get("SELECT * FROM refund_requests WHERE id = :rid", {"rid": request_id})
    if not row:
        raise CheckoutError("退款申请不存在", 404)
    if row["user_id"] != user_id:
        raise CheckoutError("无权操作该申请", 403)
    if row["status"] != "pending":
        raise CheckoutError("该申请已处理，无法撤销")
    now = db.now_iso()
    db.update("refund_requests", {"status": "cancelled", "updated_at": now}, "id = :rid", {"rid": request_id})
    _add_event(row["order_id"], "refund_cancel", "买家撤回了退款申请", "customer")
    return _decorate(db.get("SELECT * FROM refund_requests WHERE id = :rid", {"rid": request_id}))


def list_requests(*, status: str = "", q: str = "", limit: int = 20, offset: int = 0) -> dict:
    where, params = [], {"limit": int(limit), "offset": int(offset)}
    if status:
        where.append("r.status = :status")
        params["status"] = status
    if q:
        where.append("(o.order_no LIKE :q OR o.email LIKE :q)")
        params["q"] = f"%{q}%"
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.all_(
        f"""SELECT r.*, o.order_no, o.email, o.status AS order_status, o.paid_cents, o.refunded_cents, o.total_cents
            FROM refund_requests r JOIN orders o ON o.id = r.order_id {clause}
            ORDER BY (r.status = 'pending') DESC, r.created_at DESC LIMIT :limit OFFSET :offset""",
        params,
    )
    total = int(
        db.value(
            f"SELECT COUNT(*) FROM refund_requests r JOIN orders o ON o.id = r.order_id {clause}",
            {k: v for k, v in params.items() if k in ("status", "q")},
        )
        or 0
    )
    items = []
    for r in rows:
        card = _decorate(r)
        card["refundable_cents"] = int(r["paid_cents"] or 0) - int(r["refunded_cents"] or 0)
        card["lines"] = db.all_(
            "SELECT id, title, variant_title, qty, refunded_qty, unit_price_cents FROM order_items WHERE order_id = :oid ORDER BY id ASC",
            {"oid": r["order_id"]},
        )
        items.append(card)
    return {"items": items, "total": total}


def pending_count() -> int:
    return int(db.value("SELECT COUNT(*) FROM refund_requests WHERE status = 'pending'") or 0)


def _add_event(order_id: str, kind: str, message: str, actor: str) -> None:
    db.insert("order_events", {"order_id": order_id, "kind": kind, "message": message[:255], "actor": actor, "created_at": db.now_iso()})


def refund_order(order_id: str, *, amount_cents: int, reason: str = "退款", actor: str = "admin", item_ids=None) -> dict:
    """商家退款：记账、按需回补库存、归还优惠券额度。"""
    order = db.get("SELECT * FROM orders WHERE id = :oid", {"oid": order_id})
    if not order:
        raise CheckoutError("订单不存在", 404)
    if order["payment_status"] != "paid":
        raise CheckoutError("该订单尚未支付，无需退款")
    amount = int(amount_cents)
    already = int(order["refunded_cents"] or 0)
    if amount <= 0:
        raise CheckoutError("退款金额必须大于 0")
    if amount + already > int(order["paid_cents"] or 0):
        raise CheckoutError(f"退款金额超出可退额度（可退 {db.money(int(order['paid_cents'] or 0) - already)}）")

    sale = db.get(
        "SELECT * FROM payments WHERE order_id = :oid AND kind = 'sale' AND status = 'confirmed' ORDER BY created_at DESC LIMIT 1",
        {"oid": order_id},
    )
    provider = sale["provider"] if sale else "mock"
    now = db.now_iso()
    restock = []
    for raw in (item_ids or []):
        iid = raw.get("id") if isinstance(raw, dict) else raw
        item = db.get("SELECT * FROM order_items WHERE id = :iid AND order_id = :oid", {"iid": iid, "oid": order_id})
        if not item:
            continue
        remaining = int(item["qty"]) - int(item["refunded_qty"])
        if remaining <= 0:
            continue
        restock.append((item, remaining))

    with db.tx() as tx:
        refund_id = new_id("pay")
        tx.insert(
            "payments",
            {
                "id": refund_id,
                "order_id": order_id,
                "provider": provider,
                "kind": "refund",
                "amount_cents": amount,
                "currency": "CNY",
                "status": "confirmed",
                "gateway_ref": f"DEMO-R{refund_id[-8:].upper()}",
                "created_at": now,
                "confirmed_at": now,
            },
        )
        if sale:
            fully_now = already + amount >= int(order["paid_cents"] or 0)
            tx.execute(
                "UPDATE payments SET status = :status WHERE id = :pid",
                {"status": "refunded" if fully_now else "partially_refunded", "pid": sale["id"]},
            )
        for item, remaining in restock:
            tx.execute("UPDATE order_items SET refunded_qty = qty WHERE id = :iid", {"iid": item["id"]})
            tx.execute(
                "UPDATE variants SET stock_qty = stock_qty + :qty, updated_at = :now WHERE id = :vid",
                {"qty": remaining, "now": now, "vid": int(item["variant_id"])},
            )
            tx.insert(
                "inventory_movements",
                {
                    "variant_id": int(item["variant_id"]),
                    "delta": remaining,
                    "reason": "refund_restock",
                    "reference_type": "order",
                    "reference_id": order_id,
                    "created_at": now,
                },
            )
        tx.execute("UPDATE orders SET refunded_cents = refunded_cents + :amount, updated_at = :now WHERE id = :oid", {"amount": amount, "now": now, "oid": order_id})
        paid = int(order["paid_cents"] or 0)
        total_refunded = already + amount
        fully = total_refunded >= paid
        tx.execute(
            "UPDATE orders SET payment_status = :pay_status, status = :status, updated_at = :now WHERE id = :oid",
            {"pay_status": "refunded" if fully else "paid", "status": "refunded" if fully else order["status"], "now": now, "oid": order_id},
        )
        if fully and order.get("coupon_code"):
            tx.execute("UPDATE coupons SET used_count = GREATEST(0, used_count - 1) WHERE code = :code", {"code": order["coupon_code"]})
            tx.execute("DELETE FROM coupon_redemptions WHERE order_id = :oid", {"oid": order_id})
    if provider == "balance" and order.get("user_id"):
        from . import wallet

        wallet.refund(order["user_id"], amount, reference=order["order_no"])

    _add_event(order_id, "refund", f"{'全额' if total_refunded >= paid else '部分'}退款 {db.money(amount)}：{reason}", actor)
    from . import orders

    return orders.detail(db.get("SELECT * FROM orders WHERE id = :oid", {"oid": order_id}))


def approve(request_id: str, *, admin_id: str | None, amount_cents: int | None = None, note: str = "") -> dict:
    row = db.get("SELECT * FROM refund_requests WHERE id = :rid", {"rid": request_id})
    if not row:
        raise CheckoutError("退款申请不存在", 404)
    if row["status"] != "pending":
        raise CheckoutError("该申请已处理")
    amount = int(amount_cents if amount_cents is not None else row["amount_cents"])
    restock_items = row.get("item_ids") if row["kind"] == "return" else None
    reason = f"{KIND_TEXT.get(row['kind'], row['kind'])}（申请 {request_id}）：{row['reason']}" + (f" · 商家备注：{note.strip()}" if note else "")
    order = refund_order(row["order_id"], amount_cents=amount, reason=reason, actor="admin", item_ids=restock_items)
    now = db.now_iso()
    db.update(
        "refund_requests",
        {"status": "approved", "admin_note": (note or "").strip()[:500] or None, "handled_by": admin_id, "handled_at": now, "updated_at": now},
        "id = :rid",
        {"rid": request_id},
    )
    _add_event(row["order_id"], "refund_approved", f"商家同意{KIND_TEXT.get(row['kind'])}，退款 {db.money(amount)}", "admin")
    return {"request": _decorate(db.get("SELECT * FROM refund_requests WHERE id = :rid", {"rid": request_id})), "order": order}


def reject(request_id: str, *, admin_id: str | None, note: str = "") -> dict:
    row = db.get("SELECT * FROM refund_requests WHERE id = :rid", {"rid": request_id})
    if not row:
        raise CheckoutError("退款申请不存在", 404)
    if row["status"] != "pending":
        raise CheckoutError("该申请已处理")
    why = (note or "").strip()
    if not why or len(why) > 300:
        raise CheckoutError("驳回申请必须填写说明（不超过 300 字）")
    now = db.now_iso()
    db.update(
        "refund_requests",
        {"status": "rejected", "admin_note": why, "handled_by": admin_id, "handled_at": now, "updated_at": now},
        "id = :rid",
        {"rid": request_id},
    )
    _add_event(row["order_id"], "refund_rejected", f"商家驳回{KIND_TEXT.get(row['kind'])}申请：{why}", "admin")
    return _decorate(db.get("SELECT * FROM refund_requests WHERE id = :rid", {"rid": request_id}))
