"""库存：可售 = 库存 - 已占用。variants.reserved_qty 是下单时的占用计数，
reservations 只是每笔订单的占用明细，不能再减一次。"""

from __future__ import annotations

from .. import db


def available(variant_id: int) -> int:
    row = db.get(
        """SELECT v.stock_qty - v.reserved_qty AS free
           FROM variants v
           WHERE v.id = :vid""",
        {"vid": int(variant_id)},
    )
    return max(0, int((row or {}).get("free") or 0))


def stock_of(product_id: int) -> dict:
    """按商品汇总：在售规格数、总可售、最热卖规格是否缺货。"""
    rows = db.all_(
        """SELECT v.id, v.sku, v.title, v.price_cents, v.stock_qty, v.reserved_qty,
                  v.stock_qty - v.reserved_qty AS available, v.status
           FROM variants v WHERE v.product_id = :pid AND v.status = 'active' ORDER BY v.price_cents ASC""",
        {"pid": int(product_id)},
    )
    total = sum(max(0, int(r["available"])) for r in rows)
    return {
        "product_id": int(product_id),
        "available": total,
        "variant_count": len(rows),
        "variants": [
            {
                "variant_id": int(r["id"]),
                "sku": r["sku"],
                "title": r["title"],
                "price_cents": int(r["price_cents"]),
                "stock_qty": int(r["stock_qty"]),
                "reserved_qty": int(r["reserved_qty"]),
                "available": max(0, int(r["available"])),
            }
            for r in rows
        ],
    }


def low_stock(*, threshold: int = 5, limit: int = 20) -> list[dict]:
    rows = db.all_(
        """SELECT v.id, v.sku, v.title, v.stock_qty, v.reserved_qty, p.title AS product_title, p.slug
           FROM variants v JOIN products p ON p.id = v.product_id
           WHERE v.status = 'active' AND (v.stock_qty - v.reserved_qty) <= :threshold
           ORDER BY (v.stock_qty - v.reserved_qty) ASC LIMIT :limit""",
        {"threshold": int(threshold), "limit": int(limit)},
    )
    return [
        {
            "variant_id": int(r["id"]),
            "sku": r["sku"],
            "title": r["title"],
            "product_title": r["product_title"],
            "slug": r["slug"],
            "available": int(r["stock_qty"]) - int(r["reserved_qty"]),
        }
        for r in rows
    ]


def movements(*, variant_id: int | None = None, limit: int = 30) -> list[dict]:
    where = "WHERE m.variant_id = :vid" if variant_id else ""
    params = {"vid": int(variant_id)} if variant_id else {}
    rows = db.all_(
        f"""SELECT m.*, v.sku, p.title AS product_title FROM inventory_movements m
            JOIN variants v ON v.id = m.variant_id JOIN products p ON p.id = v.product_id
            {where} ORDER BY m.id DESC LIMIT :limit""",
        {**params, "limit": int(limit)},
    )
    return [
        {
            "id": int(r["id"]),
            "sku": r["sku"],
            "product_title": r["product_title"],
            "delta": int(r["delta"]),
            "reason": r["reason"],
            "reference_id": r.get("reference_id"),
            "note": r.get("note"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def adjust(variant_id: int, delta: int, *, reason: str = "manual", actor: str | None = None, note: str = "") -> dict:
    """改库存必须留流水，运营口径要能追溯。"""
    now = db.now_iso()
    db.run("UPDATE variants SET stock_qty = GREATEST(0, stock_qty + :delta), updated_at = :now WHERE id = :vid", {"delta": int(delta), "now": now, "vid": int(variant_id)})
    db.insert(
        "inventory_movements",
        {"variant_id": int(variant_id), "delta": int(delta), "reason": reason, "reference_type": "admin", "reference_id": actor, "actor_user_id": actor, "note": note, "created_at": now},
    )
    product_id = db.value("SELECT product_id FROM variants WHERE id = :vid", {"vid": int(variant_id)})
    return stock_of(int(product_id))

def search_variants(*, q: str = "", product_id: int = 0, limit: int = 40) -> list[dict]:
    where, params = ["v.status <> 'archived'"], {"limit": int(limit)}
    if q:
        where.append("(v.sku LIKE :q OR v.title LIKE :q OR p.title LIKE :q)")
        params["q"] = f"%{q}%"
    if product_id:
        where.append("v.product_id = :pid")
        params["pid"] = int(product_id)
    rows = db.all_(
        f"""SELECT v.*, p.title AS product_title, p.slug, p.price_cents AS card_price_cents
            FROM variants v JOIN products p ON p.id = v.product_id
            WHERE {' AND '.join(where)} ORDER BY v.product_id DESC, v.price_cents ASC LIMIT :limit""",
        params,
    )
    return [
        {
            "variant_id": int(r["id"]),
            "product_id": int(r["product_id"]),
            "product_title": r["product_title"],
            "slug": r["slug"],
            "sku": r["sku"],
            "title": r["title"],
            "price_cents": int(r["price_cents"]),
            "card_price_cents": int(r["card_price_cents"]),
            "stock_qty": int(r["stock_qty"]),
            "reserved_qty": int(r["reserved_qty"]),
            "available": int(r["stock_qty"]) - int(r["reserved_qty"]),
            "status": r["status"],
            "weight_gram": int(r["weight_gram"] or 0),
        }
        for r in rows
    ]


def update_variant(variant_id: int, form: dict, *, actor: str | None = None) -> dict:
    """改价与改库存都写规格：商品卡价取最低规格价，购物车取的就是这一行。"""
    from .checkout import CheckoutError

    row = db.get("SELECT * FROM variants WHERE id = :vid", {"vid": int(variant_id)})
    if not row:
        raise CheckoutError("规格不存在", 404)
    now = db.now_iso()
    changes, old_delta = {}, 0
    if form.get("price_cents") is not None:
        price = int(form["price_cents"])
        if price <= 0:
            raise CheckoutError("价格要大于 0")
        changes["price_cents"] = price
    if form.get("stock_qty") is not None:
        stock = int(form["stock_qty"])
        if stock < int(row["reserved_qty"]):
            raise CheckoutError(f"库存不能低于已占用的 {int(row['reserved_qty'])} 件")
        old_delta = stock - int(row["stock_qty"])
        changes["stock_qty"] = stock
    if form.get("status") in ("active", "draft", "archived"):
        changes["status"] = form["status"]
    if not changes:
        raise CheckoutError("没有可更新的字段")
    changes["updated_at"] = now
    db.update("variants", changes, "id = :vid", {"vid": int(variant_id)})
    if old_delta:
        db.insert(
            "inventory_movements",
            {"variant_id": int(variant_id), "delta": old_delta, "reason": "manual", "reference_type": "admin", "reference_id": actor, "actor_user_id": actor, "note": form.get("note") or "后台改库存", "created_at": now},
        )
    db.run(
        """UPDATE products SET price_cents = (SELECT MIN(price_cents) FROM variants WHERE product_id = :pid AND status = 'active'),
                                  updated_at = :now
           WHERE id = :pid""",
        {"pid": int(row["product_id"]), "now": now},
    )
    return search_variants(product_id=int(row["product_id"]), limit=50)
