"""中文前台：服务端渲染，数据口径与 /api 完全一致，不复制第二份查询。"""

from __future__ import annotations

import os

import jinja2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import db
from .deps import current_user, guest_token
from .services import auth
from .services import cart as cart_service
from .services import catalog, checkout, orders, outfit, recommend, wallet
from .settings import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.filters["money"] = db.money
templates.env.filters["date"] = lambda v: str(v or "")[:10]
templates.env.filters["datetime"] = lambda v: str(v or "").replace("T", " ")[:16]

_BALANCE_KINDS = {"topup": "模拟充值", "payment": "订单支付", "refund": "退款到账", "admin_adjust": "后台调整"}
templates.env.filters["balance_kind"] = lambda k: _BALANCE_KINDS.get(k, k or "流水")
templates.env.filters["user_status"] = lambda v: {"active": "正常", "disabled": "已停用"}.get(v, v or "—")
templates.env.filters["signed_money"] = lambda c: ("+" if int(c or 0) > 0 else "−") + db.money(abs(int(c or 0)))


_ASSETS = ("static/css/site.css", "static/js/site.js", "static/template/maison.css", "static/js/maison.js")


def _asset_v() -> str:
    """静态资源改过就换 URL，免得浏览器拿旧样式。"""
    try:
        return str(int(max(os.path.getmtime(p) for p in _ASSETS)))
    except OSError:
        return "0"


def _link(context, **overrides) -> str:
    """在当前查询条件上改几个参数，筛选项之间可以叠加。"""
    from urllib.parse import urlencode

    request = context["request"]
    params = dict(request.query_params)
    for key, value in overrides.items():
        if value in (None, "", 0, "0"):
            params.pop(key, None)
        else:
            params[key] = str(value)
    return "/shop" + ("?" + urlencode(params) if params else "")


templates.env.globals["link"] = jinja2.pass_context(_link)


def _cart(request: Request) -> dict | None:
    who = current_user(request)
    found = cart_service.find(who["id"] if who else None, None if who else guest_token(request))
    return cart_service.view(found["id"]) if found else None


def render(request: Request, name: str, *, status_code: int = 200, **ctx) -> HTMLResponse:
    settings = get_settings()
    user = current_user(request)
    cart = _cart(request)
    surface = getattr(request.app.state, "surface", "shop")
    ctx.setdefault("active", "")
    ctx.setdefault("query", "")
    return templates.TemplateResponse(
        name,
        {
            "request": request,
            "asset_v": _asset_v(),
            "settings": settings,
            "surface": surface,
            # 后台端口上的页面要把商城链接指回前台，反之后台入口要指到后台端口
            "shop_url": settings.shop_url if surface == "admin" else "",
            "admin_url": settings.admin_url if surface == "shop" else "",
            "user": user,
            "cart": cart,
            "cart_count": cart["count"] if cart else 0,
            "nav_categories": catalog.categories(),
            "shipping_plans": checkout.plans(),
            "store_stats": _store_stats(),
            **ctx,
        },
        status_code=status_code,
    )


def _store_stats() -> dict:
    row = db.get(
        """SELECT (SELECT COUNT(*) FROM products WHERE status = 'active') AS products,
                  (SELECT COUNT(*) FROM categories WHERE active = 1) AS categories,
                  (SELECT COUNT(*) FROM reviews WHERE status = 'published') AS reviews,
                  (SELECT IFNULL(SUM(sold_count), 0) FROM products) AS sold"""
    )
    return {k: int(v or 0) for k, v in (row or {}).items()}


def _category_covers() -> dict[str, str]:
    rows = db.all_(
        """SELECT c.slug, MIN(img.url) AS url
           FROM categories c
           JOIN products p ON p.category_id = c.id AND p.status = 'active'
           JOIN product_images img ON img.product_id = p.id
           GROUP BY c.slug"""
    )
    return {r["slug"]: r["url"] for r in rows}


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    # 首屏大图位只选确实有实物图的商品，否则会拿灰色占位图当门面
    slides = [p for p in catalog.search(featured=True, in_stock=True, per_page=12)["items"] if p["image"]][:3]
    if len(slides) < 3:
        slides = [p for p in catalog.search(sort="popular", in_stock=True, per_page=12)["items"] if p["image"]][:3]
    picks = catalog.search(sort="rating", in_stock=True, per_page=4)["items"]
    fresh = [p for p in catalog.search(sort="newest", per_page=12)["items"] if p["image"]][:4]
    voices = db.all_(
        """SELECT r.rating, r.title, r.body, r.created_at, u.name AS author, p.slug, p.title AS product_title, p.rating_avg
           FROM reviews r JOIN users u ON u.id = r.user_id JOIN products p ON p.id = r.product_id
           WHERE r.status = 'published' AND r.body <> '' ORDER BY r.helpful_count DESC, r.created_at DESC LIMIT 3"""
    )
    return render(
        request,
        "index.html",
        active="home",
        slides=slides,
        hot=recommend.hot(limit=8),
        picks=picks,
        fresh=fresh,
        voices=voices,
        covers=_category_covers(),
    )


@router.get("/shop", response_class=HTMLResponse)
def shop(
    request: Request,
    q: str = "",
    category: str = "",
    brand: str = "",
    tag: str = "",
    sort: str = "relevant",
    min_price: int = 0,
    max_price: int = 0,
    in_stock: int = 0,
    page: int = 1,
):
    result = catalog.search(
        q=q,
        category=category,
        brand=brand,
        tag=tag,
        sort=sort,
        min_price=min_price or None,
        max_price=max_price or None,
        in_stock=bool(in_stock),
        page=page,
        per_page=12,
    )
    facets = catalog.facets(category=category)
    active_cat = db.get("SELECT * FROM categories WHERE slug = :s", {"s": category}) if category else None
    return render(
        request,
        "shop.html",
        active="shop",
        query=q,
        result=result,
        facets=facets,
        filters={"q": q, "category": category, "brand": brand, "tag": tag, "sort": sort, "min_price": min_price, "max_price": max_price, "in_stock": in_stock},
        active_cat=active_cat,
        sort_options=[("relevant", "综合"), ("popular", "销量"), ("newest", "最新"), ("price_asc", "价格从低到高"), ("price_desc", "价格从高到低"), ("rating", "评分优先")],
    )


@router.get("/outfit", response_class=HTMLResponse)
def outfit_page(
    request: Request,
    occasion: str = "",
    height: str = "",
    body: str = "",
    style: str = "",
    personality: str = "",
    line: str = "",
    budget: str = "",
):
    """场合穿搭：条件全部来自商品自己标注的字段，凑不出来的位子直接在页面上说明。

    height、budget 收的是字符串：表单里数字输入框留空会提交空值，直接声明成 int 会被 FastAPI 判成 422。
    """
    height_cm = int(height) if height.isdigit() else 0
    budget_yuan = int(budget) if budget.isdigit() else 0
    result = None
    if occasion:
        result = outfit.recommend(
            occasion=occasion,
            height_cm=height_cm or None,
            body_type=body,
            style=style,
            personality=personality,
            line=line,
            budget_yuan=budget_yuan or None,
            limit=2,
        )
    return render(
        request,
        "outfit.html",
        active="outfit",
        vocab=outfit.vocabulary(),
        personality_words=sorted(outfit.PERSONALITY_STYLES),
        result=result,
        form={"occasion": occasion, "height": height, "body": body, "style": style,
              "personality": personality, "line": line, "budget": budget},
    )


@router.get("/product/{key}", response_class=HTMLResponse)
def product(request: Request, key: str):
    item = catalog.get(key, count_view=True)
    if not item:
        return render(request, "not_found.html", active="", status_code=404, back="/shop")
    reviews = catalog.reviews(item["id"], page=1, per_page=6)
    together = recommend.bought_together(product_ids=[item["id"]], limit=4)
    return render(request, "product.html", active="shop", item=item, reviews=reviews, together=together)


@router.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    cart = _cart(request)
    priced = None
    if cart and cart["items"]:
        try:
            priced = checkout.quote(cart=cart, plan_code=cart["shipping_plan_code"], coupon_code=cart["coupon_code"] or "")
        except checkout.CheckoutError:
            priced = None
    return render(request, "cart.html", active="cart", items=(cart or {"items": []})["items"], subtotal=(cart or {"subtotal_cents": 0})["subtotal_cents"], quote=priced)


@router.get("/support", response_class=HTMLResponse)
def support(request: Request):
    faqs = db.all_("SELECT id, question, kind FROM support_faqs WHERE active = 1 ORDER BY sort ASC LIMIT 8")
    return render(request, "support.html", active="support", faqs=faqs, model=_agent_model())


@router.get("/checkout", response_class=HTMLResponse)
def checkout_page(request: Request):
    who = current_user(request)
    cart = _cart(request)
    priced = None
    if cart and cart["items"]:
        try:
            priced = checkout.quote(
                cart=cart,
                plan_code=cart["shipping_plan_code"],
                coupon_code=cart["coupon_code"] or "",
                user_id=who["id"] if who else None,
            )
        except checkout.CheckoutError:
            priced = None
    return render(
        request,
        "checkout.html",
        active="checkout",
        quote=priced,
        addresses=auth.addresses(who["id"]) if who else [],
        providers=checkout.providers(),
        store=checkout.settings(),
    )


@router.get("/account", response_class=HTMLResponse)
def account(request: Request):
    who = current_user(request)
    if not who:
        return render(request, "account.html", active="account")
    return render(
        request,
        "account.html",
        active="account",
        orders=orders.customer_orders(who["id"], limit=10),
        addresses=auth.addresses(who["id"]),
        wallet=wallet.overview(who["id"], limit=12),
        order_count=int(db.value("SELECT COUNT(*) FROM orders WHERE user_id = :uid", {"uid": who["id"]}) or 0),
    )


@router.get("/order/{order_no}", response_class=HTMLResponse)
def order_page(request: Request, order_no: str):
    who = current_user(request)
    order = orders.find(order_no, user_id=(who or {}).get("id"), is_admin=bool(who and who.get("role") == "admin"))
    if not order:
        return render(request, "not_found.html", active="", status_code=404, title="订单查不到", back="/account", hint="订单号不对，或它属于别的账号。游客下单请用订单号问小马。")
    return render(request, "order.html", active="account", order=order, can_refund=bool(who and order["payment_status"] == "paid" and order["refundable_cents"] > 0))


@router.get("/{path:path}", response_class=HTMLResponse)
def not_found(request: Request, path: str):
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    return render(request, "not_found.html", active="", status_code=404, title="页面不存在", back="/shop", hint="这个地址在站内没有对应页面，换个入口逛逛。")


def _agent_model() -> str:
    from .agent import graph

    return graph.model_name()
