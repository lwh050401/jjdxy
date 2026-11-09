"""推荐：只从真实成交、真实库存与真实评分里算，不编造销量话术。"""

from __future__ import annotations

from .. import db
from . import catalog

CARD_FIELDS = ("id", "slug", "title", "image", "price_cents", "available", "rating_avg", "rating_count", "sold_count", "brand", "category")


def _trim(products: list[dict]) -> list[dict]:
    return [{k: p.get(k) for k in CARD_FIELDS} for p in products]


def hot(*, limit: int = 6, category: str = "") -> list[dict]:
    result = catalog.search(sort="popular", in_stock=True, category=category, per_page=limit)
    return _trim(result["items"])


def top_rated(*, limit: int = 6) -> list[dict]:
    result = catalog.search(sort="rating", in_stock=True, per_page=limit)
    return _trim(result["items"])


def by_budget(*, max_cents: int | None = None, min_cents: int | None = None, category: str = "", q: str = "", limit: int = 6) -> list[dict]:
    result = catalog.search(
        q=q,
        category=category,
        min_price=min_cents,
        max_price=max_cents,
        in_stock=True,
        # 带说法时先按标题对题排再按销量：搜「露营」要出露营灯，不是出个评分高的电饭煲
        sort="relevant" if q else "rating",
        per_page=limit,
    )
    return _trim(result["items"])


def search_first(*, q: str, limit: int = 6) -> list[dict]:
    result = catalog.search(q=q, per_page=limit, sort="relevant")
    return _trim(result["items"])


def for_user(*, user_id: str, limit: int = 6) -> dict:
    """复购与加购角度：买过的分类里挑没买过的商品，不够再用热卖补。"""
    bought = db.all_(
        """SELECT DISTINCT oi.product_id FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.user_id = :uid AND o.status NOT IN ('cancelled', 'expired')""",
        {"uid": user_id},
    )
    bought_ids = [int(r["product_id"]) for r in bought if r.get("product_id")]
    cats = db.all_(
        """SELECT DISTINCT p.category_id FROM order_items oi
           JOIN orders o ON o.id = oi.order_id JOIN products p ON p.id = oi.product_id
           WHERE o.user_id = :uid AND p.category_id IS NOT NULL""",
        {"uid": user_id},
    )
    cat_ids = [int(r["category_id"]) for r in cats]
    picked: list[dict] = []
    if cat_ids:
        marks = ", ".join(f":c{i}" for i in range(len(cat_ids)))
        rows = db.all_(
            f"""{catalog.BASE_SELECT} WHERE p.status = 'active' AND p.category_id IN ({marks})
                AND p.id NOT IN ({', '.join(str(i) for i in bought_ids) if bought_ids else '0'})
                AND (SELECT SUM(v.stock_qty - v.reserved_qty) FROM variants v WHERE v.product_id = p.id AND v.status='active') > 0
                ORDER BY p.rating_avg DESC, p.sold_count DESC LIMIT :limit""",
            {**{f"c{i}": c for i, c in enumerate(cat_ids)}, "limit": limit},
        )
        picked = [catalog.decorate(r) for r in rows]
    reason = "按你买过的分类推荐" if picked else "还没有购买记录，先看热卖"
    if len(picked) < limit:
        picked += [dict(p, _from="hot") for p in catalog.search(sort="popular", in_stock=True, per_page=limit - len(picked))["items"]]
    seen, unique = set(), []
    for p in picked:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)
    return {"reason": reason, "items": _trim(unique[:limit])}


def bought_together(*, product_ids: list[int], limit: int = 4) -> list[dict]:
    """同单共现：真实订单里和这些商品一起结算过的商品。"""
    ids = [int(i) for i in product_ids if i]
    if not ids:
        return []
    marks = ", ".join(f":p{i}" for i in range(len(ids)))
    rows = db.all_(
        f"""SELECT oi2.product_id AS pid, COUNT(*) AS n FROM order_items oi1
            JOIN orders o ON o.id = oi1.order_id
            JOIN order_items oi2 ON oi2.order_id = o.id
            WHERE oi1.product_id IN ({marks}) AND oi2.product_id NOT IN ({marks})
            GROUP BY oi2.product_id ORDER BY n DESC, oi2.product_id ASC LIMIT :limit""",
        {**{f"p{i}": pid for i, pid in enumerate(ids)}, "limit": int(limit)},
    )
    out = []
    for r in rows:
        product = catalog.get(r["pid"])
        if product and product["status"] == "active":
            out.append(dict(_trim([product])[0], co_orders=int(r["n"])))
    return out


def similar(*, product_id: int, limit: int = 6) -> list[dict]:
    product = catalog.get(product_id)
    if not product:
        return []
    return _trim(catalog.related(product, limit=limit))
