"""商品与商城公开接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..deps import ok
from ..services import catalog, checkout, inventory, recommend
from ..settings import get_settings

router = APIRouter()


@router.get("/health")
def health():
    from .. import db
    from ..agent import graph

    return ok(
        {
            "status": "healthy",
            "store": get_settings().store_name,
            "database": "mysql",
            "products": int(db.value("SELECT COUNT(*) FROM products WHERE status = 'active'") or 0),
            "orders": int(db.value("SELECT COUNT(*) FROM orders") or 0),
            "agent_model": graph.model_name(),
        }
    )


@router.get("/products")
def products(
    q: str = "",
    category: str = "",
    brand: str = "",
    tag: str = "",
    min_price: int | None = None,
    max_price: int | None = None,
    rating: float | None = None,
    in_stock: bool = False,
    featured: bool = False,
    sort: str = Query("relevant"),
    page: int = Query(1, ge=1),
    per_page: int = Query(12, ge=1, le=60),
):
    return ok(catalog.search(q=q, category=category, brand=brand, tag=tag, min_price=min_price, max_price=max_price, min_rating=rating, in_stock=in_stock, featured=featured, sort=sort, page=page, per_page=per_page))


@router.get("/products/{key}")
def product(key: str, request: Request):
    product = catalog.get(key, count_view=not request.query_params.get("no_view"))
    if not product:
        return _not_found(key)
    return ok(product)


@router.get("/products/{key}/reviews")
def product_reviews(key: str, rating: int | None = None, page: int = 1, per_page: int = 10):
    product = catalog.get(key)
    if not product:
        return _not_found(key)
    return ok(catalog.reviews(int(product["id"]), rating=rating, page=page, per_page=per_page))


def _not_found(key: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=404, content={"ok": False, "error": {"code": "not_found", "message": f"没有找到商品 {key}"}})


@router.get("/categories")
def categories():
    return ok(catalog.categories())


@router.get("/facets")
def facets(category: str = ""):
    return ok(catalog.facets(category=category))


@router.get("/suggest")
def suggest(q: str = ""):
    return ok(catalog.suggest(q))


@router.get("/shipping-plans")
def shipping_plans():
    return ok(checkout.plans())


@router.get("/payment-providers")
def payment_providers():
    return ok(checkout.providers())


@router.get("/store-settings")
def store_settings():
    return ok(checkout.settings())


@router.get("/recommendations")
def recommendations(request: Request, budget: int | None = None, category: str = "", q: str = "", limit: int = 6):
    from ..deps import current_user

    who = current_user(request)
    if who:
        return ok(recommend.for_user(user_id=who["id"], limit=limit))
    if budget or category or q:
        return ok({"items": recommend.by_budget(max_cents=budget, category=category, q=q, limit=limit), "reason": "按预算与分类筛选"})
    return ok({"items": recommend.hot(limit=limit), "reason": "全站热卖"})


@router.get("/inventory/low")
def low_stock(threshold: int = 5, limit: int = 20):
    return ok(inventory.low_stock(threshold=threshold, limit=limit))
