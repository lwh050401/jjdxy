"""电商后端能力的 Agent 视角封装。

小马的工具不直连数据库，全部经过这一层，保证和前台 API 走同一份业务口径；
金额在这一层从「分」翻成「元」，让模型说人话时不用做算术。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from .. import db
from ..services import catalog, checkout, inventory, orders, outfit, recommend, visual

# 当前会话的用户与游客令牌：由 /api/agent 入口写入，工具在同一次请求内读取
current_actor: ContextVar["Actor | None"] = ContextVar("current_actor", default=None)


@dataclass
class Actor:
    user: dict | None = None
    guest_token: str | None = None

    @property
    def user_id(self) -> str | None:
        return self.user.get("id") if self.user else None

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.get("role") == "admin")


def actor() -> Actor:
    return current_actor.get() or Actor()


def yuan(cents) -> float:
    return round(int(cents or 0) / 100, 2)


def _inventory_card(product_id: int) -> list[dict]:
    product = catalog.get(product_id)
    return [_card(product)] if product else []


def _trim_number(value) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _card(product: dict) -> dict:
    return {
        "id": product["id"],
        "slug": product["slug"],
        "title": product["title"],
        "brand": product.get("brand"),
        "category": (product.get("category") or {}).get("name"),
        "price_yuan": yuan(product["price_cents"]),
        "compare_at_yuan": yuan(product["compare_at_cents"]) if product.get("compare_at_cents") else None,
        "available": product.get("available", 0),
        "rating_avg": product.get("rating_avg"),
        "rating_count": product.get("rating_count"),
        "sold_count": product.get("sold_count"),
        "image": product.get("image"),
        "url": f"/product/{product['slug']}",
    }


def _order_card(order: dict) -> dict:
    return {
        "order_no": order["order_no"],
        "status": order["status"],
        "status_text": order.get("status_text") or orders.STATUS_TEXT.get(order["status"], order["status"]),
        "payment_status": order.get("payment_status"),
        "placed_at": order["placed_at"],
        "total_yuan": yuan(order["total_cents"]),
        "paid_yuan": yuan(order.get("paid_cents")),
        "refunded_yuan": yuan(order.get("refunded_cents")),
        "discount_yuan": yuan(order.get("discount_cents")),
        "shipping_yuan": yuan(order.get("shipping_cents")),
        "tax_yuan": yuan(order.get("tax_cents")),
        "carrier": order.get("carrier"),
        "tracking_no": order.get("tracking_no"),
        "items": [
            {
                "title": i["title"],
                "variant": i.get("variant_title"),
                "qty": i["qty"],
                "unit_yuan": yuan(i["unit_price_cents"]),
                "product_id": i.get("product_id"),
            }
            for i in order.get("items", [])
        ],
        "timeline": [{"message": e["message"], "at": e["created_at"]} for e in order.get("events", [])][-4:],
        "refunds": [{"status": r["status"], "amount_yuan": yuan(r["amount_cents"]), "reason": r["reason"]} for r in order.get("refunds", [])],
    }


def search_products(
    query: str = "",
    category: str = "",
    tag: str = "",
    brand: str = "",
    max_price_yuan: float | None = None,
    min_price_yuan: float | None = None,
    min_rating: float | None = None,
    in_stock_only: bool = True,
    sort: str = "relevant",
    limit: int = 5,
) -> dict:
    """商品检索：关键词 + 分类 + 价格区间 + 评分，返回卡片列表。"""
    result = catalog.search(
        q=query,
        category=category,
        tag=tag,
        brand=brand,
        max_price=int(round(float(max_price_yuan) * 100)) if max_price_yuan else None,
        min_price=int(round(float(min_price_yuan) * 100)) if min_price_yuan else None,
        min_rating=min_rating,
        in_stock=in_stock_only,
        sort=sort if sort in catalog.SORTS else "relevant",
        per_page=min(20, max(1, int(limit))),
    )
    return {
        "total": result["total"],
        "count": len(result["items"]),
        "items": [_card(p) for p in result["items"]],
        "hint": "没有结果时可以放宽价格区间或去掉 in_stock_only 再查一次" if not result["items"] else "",
    }


def product_detail(slug_or_id: str) -> dict:
    product = catalog.get(slug_or_id)
    if not product:
        return {"found": False, "message": f"没有找到商品 {slug_or_id}"}
    detail = _card(product)
    detail.update(
        {
            "found": True,
            "description": product.get("description"),
            "attributes": product.get("attributes"),
            "tags": product.get("tags"),
            "variants": [
                {
                    "variant_id": v["id"],
                    "sku": v["sku"],
                    "title": v["title"],
                    "options": v["options"],
                    "price_yuan": yuan(v["price_cents"]),
                    "available": v["available"],
                    "stock_qty": v["stock_qty"],
                }
                for v in product["variants"]
            ],
            "review_summary": product["reviews"],
            "top_reviews": [r["body"] for r in catalog.reviews(product["id"], per_page=3)["items"]],
            "related": [_card(p) for p in product["related"][:4]],
        }
    )
    return detail


def query_order(order_no: str = "", scope: str = "recent", limit: int = 5) -> dict:
    """订单查询：给了订单号就精确查，否则查当前账号最近的单。"""
    who = actor()
    if order_no:
        order = orders.find(order_no.strip().upper(), user_id=who.user_id, is_admin=who.is_admin)
        if not order:
            return {"found": False, "message": f"没有查到订单号 {order_no}；如果这是别人的订单，需要用下单账号登录后再问。"}
        return {"found": True, "count": 1, "orders": [_order_card(order)]}
    if not who.user_id:
        return {"found": False, "message": "游客没有账号订单。请让对方提供订单号（LM 开头的 14 位），或先登录。"}
    rows = orders.customer_orders(who.user_id, limit=min(10, max(1, int(limit))), status=scope if scope in orders.STATUS_TEXT else "")
    return {"found": bool(rows), "count": len(rows), "orders": [_order_card(o) for o in rows]}


def check_inventory(product: str = "", sku: str = "", variant_id: int | None = None) -> dict:
    """库存查询：可售 = 库存 - 已下单未释放的占用。"""
    if variant_id:
        row = db.get("SELECT product_id FROM variants WHERE id = :vid", {"vid": int(variant_id)})
        if not row:
            return {"found": False, "message": f"规格 {variant_id} 不存在"}
        summary = inventory.stock_of(int(row["product_id"]))
        product_row = db.get("SELECT title, slug FROM products WHERE id = :pid", {"pid": summary["product_id"]})
    elif sku:
        row = db.get("SELECT v.product_id, p.title, p.slug FROM variants v JOIN products p ON p.id = v.product_id WHERE v.sku = :sku", {"sku": sku.strip().upper()})
        if not row:
            return {"found": False, "message": f"SKU {sku} 不存在"}
        summary = inventory.stock_of(int(row["product_id"]))
        product_row = {"title": row["title"], "slug": row["slug"]}
    elif product:
        found = catalog.search(q=product, per_page=1)
        if not found["items"]:
            return {"found": False, "message": f"没有匹配到商品「{product}」，可以换成品类词再查"}
        target = found["items"][0]
        summary = inventory.stock_of(int(target["id"]))
        product_row = {"title": target["title"], "slug": target["slug"]}
    else:
        lows = inventory.low_stock(threshold=int(db.value("SELECT `value` FROM settings WHERE `key` = 'low_stock_threshold'") or 5), limit=8)
        return {
            "found": True,
            "mode": "low_stock",
            "message": "以下是库存低于阈值的规格，适合提醒补货",
            "items": [{**item, "price_note": None} for item in lows],
        }

    variants = summary["variants"]
    return {
        "found": True,
        "mode": "product",
        "product_title": product_row["title"],
        "slug": product_row["slug"],
        "url": f"/product/{product_row['slug']}",
        "total_available": summary["available"],
        "items": _inventory_card(int(summary["product_id"])),
        "variants": [
            {
                "variant_id": v["variant_id"],
                "sku": v["sku"],
                "title": v["title"],
                "price_yuan": yuan(v["price_cents"]),
                "stock_qty": v["stock_qty"],
                "reserved_qty": v["reserved_qty"],
                "available": v["available"],
            }
            for v in variants
        ],
        "verdict": "有现货" if summary["available"] > 0 else "已售罄",
    }


def recommend_products(
    occasion: str = "",
    budget_yuan: float | None = None,
    min_price_yuan: float | None = None,
    category: str = "",
    keywords: str = "",
    based_on_order: str = "",
    product_id: int | None = None,
    limit: int = 5,
) -> dict:
    """推荐：优先按当前账号的购买记录，其次按预算/品类，再次按热卖与共现。"""
    who = actor()
    limit = min(10, max(1, int(limit)))

    if based_on_order:
        order = orders.find(based_on_order.strip().upper(), user_id=who.user_id, is_admin=who.is_admin)
        if order:
            ids = [i["product_id"] for i in order["items"] if i.get("product_id")]
            pairs = recommend.bought_together(product_ids=ids, limit=limit)
            if pairs:
                return {
                    "strategy": "同单共现",
                    "reason": f"买过订单 {order['order_no']} 里这些商品的人，还经常一起下单",
                    "items": pairs,
                }
            return {"strategy": "同类目", "reason": "这单商品的同类目里挑几款", "items": recommend.similar(product_id=int(ids[0]), limit=limit) if ids else []}

    if product_id:
        return {"strategy": "关联推荐", "reason": "和这件商品同分类、卖得更好的", "items": recommend.similar(product_id=int(product_id), limit=limit)}

    if budget_yuan or min_price_yuan or category or keywords or occasion:
        items = recommend.by_budget(
            max_cents=int(round(float(budget_yuan) * 100)) if budget_yuan else None,
            min_cents=int(round(float(min_price_yuan) * 100)) if min_price_yuan else None,
            category=category,
            q=keywords or occasion,
            limit=limit,
        )
        parts = []
        if budget_yuan:
            parts.append(f"{_trim_number(budget_yuan)} 元以内")
        if category:
            parts.append(f"{category} 分类")
        if keywords or occasion:
            parts.append(occasion or keywords)
        return {"strategy": "条件筛选", "reason": "、".join(parts) or "全站评分最高的现货", "items": items}

    if who.user_id:
        return {**recommend.for_user(user_id=who.user_id, limit=limit), "strategy": "老客复购"}

    return {"strategy": "全站热卖", "reason": "按真实销量排的热卖榜", "items": recommend.hot(limit=limit)}


def outfit_recommend(
    occasion: str = "",
    height_cm: int | None = None,
    body_type: str = "",
    style: str = "",
    personality: str = "",
    line: str = "",
    budget_yuan: float | None = None,
    limit: int = 2,
) -> dict:
    """场合穿搭：从商品自己标注的场合、版型、身高区间里凑一套买得到的搭配。"""
    result = outfit.recommend(
        occasion=occasion,
        height_cm=height_cm,
        body_type=body_type,
        style=style,
        personality=personality,
        line=line,
        budget_yuan=budget_yuan,
        limit=limit,
    )
    if not result.get("found"):
        return result
    lines = []
    for look in result["looks"]:
        pieces = "、".join(f"{i['role']}：{i['title']}（¥{i['price_yuan']:,.0f}）" for i in look["items"])
        tail = f"；还差 {('、'.join(look['missing']))}，站内没有同时满足条件的现货" if look["missing"] else ""
        lines.append(f"{look['name']}，合计 ¥{look['total_yuan']:,.2f}：{pieces}{tail}")
    return {**result, "reply_lines": lines}


def find_similar_by_image(
    image_token: str = "",
    category: str = "",
    max_price_yuan: float | None = None,
    limit: int = 5,
) -> dict:
    """按图找相似：拿会话里上传过的图片编号，比主色、明暗与轮廓，不靠大模型看图。"""
    if not image_token:
        return {"found": False, "message": "这次没有收到图片。请先在聊天窗点「上传图片」选一张商品图，再把编号发给我。"}
    result = visual.similar_by_token(
        image_token,
        limit=min(10, max(1, int(limit))),
        category=category,
        max_price_yuan=max_price_yuan,
    )
    if not result.get("found"):
        return result
    result["items"] = [
        {**_card(item), "similarity_pct": round(float(item.get("similarity") or 0) * 100)}
        for item in result["items"]
    ]
    return result


def shipping_options(subtotal_yuan: float = 0) -> dict:
    rows = checkout.plans()
    threshold = checkout.settings()["free_shipping_threshold_cents"]
    return {
        "free_shipping_threshold_yuan": yuan(threshold),
        "plans": [
            {
                "code": p["code"],
                "name": p["name"],
                "fee_yuan": yuan(p["flat_cents"]),
                "free_over_yuan": yuan(p["free_over_cents"]) if p["free_over_cents"] is not None else None,
                "eta": p["eta"],
                "description": p["description"],
            }
            for p in rows
        ],
        "subtotal_yuan": subtotal_yuan,
    }


def coupons_available() -> dict:
    return {"coupons": checkout.active_coupons()}


def faq_hits() -> dict:
    return {
        "faqs": [
            {"question": r["question"], "answer": r["answer"], "kind": r["kind"]}
            for r in db.all_("SELECT * FROM support_faqs WHERE active = 1 ORDER BY sort ASC, hits DESC LIMIT 8")
        ]
    }
