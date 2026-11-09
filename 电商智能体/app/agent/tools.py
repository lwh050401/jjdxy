"""小马的工具箱：把 shop_api 的能力包成 LangGraph @tool，并记录每次调用。"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar

from langchain_core.tools import tool

from . import shop_api
from . import store

# 工具内部要知道「现在在和谁说话」「该把调用记到哪条会话」
current_thread: ContextVar[str | None] = ContextVar("current_thread", default=None)
# 本轮工具查出的商品卡，前端据此渲染橱窗（在线/离线两条路径都用它）
collected_cards: ContextVar[list | None] = ContextVar("collected_cards", default=None)
collected_calls: ContextVar[list | None] = ContextVar("collected_calls", default=None)
# 本轮用完全相同条件查过的结果。模型爱在同一次提问里原样再调一次（放宽价格又改回来），
# 重复查库既拖慢回复，又会让后台统计出两笔调用，所以命中缓存直接把上一份结果给它。
turn_cache: ContextVar[dict | None] = ContextVar("turn_cache", default=None)


def reset_turn() -> tuple[list, list]:
    cards: list = []
    calls: list = []
    collected_cards.set(cards)
    collected_calls.set(calls)
    turn_cache.set({})
    return cards, calls


def _rows(result: dict, *keys: str) -> list[dict]:
    for key in keys:
        rows = result.get(key)
        if isinstance(rows, list) and rows:
            return [r for r in rows if isinstance(r, dict)]
    return []


CARD_LIMIT = 6


def with_link(row: dict) -> dict:
    """推荐这类服务只给 slug 的商品行补上跳转地址。

    卡片、落库 preview 和刷新后的回放要用同一个字段，缺 url 的话在线那条路径
    会把推荐结果整批当成「不是卡」丢掉，离线却有卡 —— 换个大脑少几种商品是不成立的。
    """
    return row if row.get("url") else {**row, "url": f"/product/{row['slug']}"}


def card_rows(name: str, result: dict) -> list[dict]:
    """一条工具结果里能当商品卡的行：要能跳到详情页。

    在线（_collect/_log）与离线（offline.run）共用这里，两条路径的卡片口径必须一致，
    否则同一句话换个大脑，前台橱窗和刷新后的回放就会少几种商品。
    """
    if not isinstance(result, dict) or not result.get("found", True):
        return []
    if name == "product_detail":
        return [with_link(result)] if result.get("slug") or result.get("url") else []
    rows = [with_link(r) for r in _rows(result, "items") if r.get("url") or r.get("slug")]
    if name == "check_inventory":
        # 库存问答只给那一件商品的卡；低库存清单里的行没有 slug，本来就不是卡
        return rows[:1]
    return rows[:CARD_LIMIT]


# 落库与前台卡片真正要读的字段；把 description、variants 整包塞进 JSON 列既没人看又占空间
CARD_KEYS = ("slug", "title", "url", "image", "price_yuan", "price_cents", "available", "role", "similarity_pct", "order_no", "status_text")


def brief(item: dict) -> dict:
    return {k: item[k] for k in CARD_KEYS if item.get(k) is not None}


def preview_rows(name: str, result: dict) -> list[dict]:
    """工具标签与落库 preview 用的行：先是商品卡，其次订单、规格这些非卡结果。"""
    cards = card_rows(name, result)
    if cards:
        return cards
    return _rows(result, "orders", "variants", "plans", "coupons", "faqs")[:4]


def _collect(name: str, result: dict) -> None:
    if not isinstance(result, dict):
        return
    buffer = collected_cards.get()
    if buffer is None:
        return
    for item in card_rows(name, result):
        if item not in buffer:
            buffer.append(item)


_KEYS = ("items", "orders", "variants", "plans", "coupons", "faqs")


def result_count(result: dict) -> int:
    """工具返回条数的统一口径，前台的工具标签与落库的 result_count 都用它。"""
    payload = result if isinstance(result, dict) else {}
    rows = payload.get("count")
    if isinstance(rows, int):
        return rows
    for key in _KEYS:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return len(value)
    return 1 if payload.get("found") else 0


def record_metric(name: str, args: dict, result: dict, started: float) -> None:
    """把一次工具调用落到 agent_tool_calls，在线与离线路径共用。"""
    thread_id = current_thread.get()
    if not thread_id or not isinstance(result, dict):
        return
    store.record_tool_call(
        thread_id=thread_id,
        tool=name,
        args=args,
        result_count=result_count(result),
        status="ok" if result.get("found", True) else "empty",
        duration_ms=int((time.time() - started) * 1000),
    )


def _log(name: str, args: dict, result: dict, started: float) -> None:
    _collect(name, result)
    calls = collected_calls.get()
    if calls is not None:
        payload = result if isinstance(result, dict) else {}
        calls.append(
            {
                "tool": name,
                "args": {k: v for k, v in (args or {}).items() if v not in (None, "", False)},
                "count": result_count(payload),
                # 存卡片对象而不是标题字符串：刷新页面后 store.cards() 要靠它还原成能点的商品卡
                "preview": [brief(i) for i in preview_rows(name, payload)],
            }
        )
    record_metric(name, args, result, started)


def _cache_key(name: str, kwargs: dict) -> str:
    return f"{name}|" + json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)


def _invoke(name: str, fn, **kwargs) -> dict:
    cache = turn_cache.get()
    key = _cache_key(name, kwargs) if cache is not None else None
    if key and cache.get(key) is not None:
        # 命中原样返回上一份结果（浅拷贝，别把 note 写回缓存），
        # 且不走 _log：既不重复记 agent_tool_calls，也不重复收商品卡
        hit = dict(cache[key])
        hit["note"] = "本轮已经用完全相同的条件查过一次，这是同一份结果，不必再查。"
        return hit
    started = time.time()
    result = fn(**kwargs)
    _log(name, kwargs, result, started)
    if key and isinstance(result, dict):
        cache[key] = result
    return result


@tool
def search_products(
    query: str = "",
    category: str = "",
    tag: str = "",
    brand: str = "",
    max_price_yuan: float | None = None,
    min_rating: float | None = None,
    in_stock_only: bool = True,
    limit: int = 5,
) -> dict:
    """按关键词、分类、品牌、标签、价格上限与评分检索在售商品。

    用户问「有没有 XX」「XX 多少钱」「便宜的 XX」「降噪耳机推荐几款」时先用它。
    query 用中文品类或型号词即可（如「保温杯」「降噪耳机」）；max_price_yuan 是元；
    找不到时把 in_stock_only 设为 False 或放宽价格再试一次。
    """
    return _invoke(
        "search_products",
        shop_api.search_products,
        query=query,
        category=category,
        tag=tag,
        brand=brand,
        max_price_yuan=max_price_yuan,
        min_rating=min_rating,
        in_stock_only=in_stock_only,
        limit=limit,
    )


@tool
def product_detail(slug_or_id: str) -> dict:
    """查看单个商品的详情：描述、参数、规格与库存、评价摘要、关联商品。

    输入商品 slug（如 lumen-air-pro-1）或数字 id，回答具体商品问题时用它补细节。
    """
    return _invoke("product_detail", shop_api.product_detail, slug_or_id=slug_or_id)


@tool
def query_order(order_no: str = "", scope: str = "recent", limit: int = 5) -> dict:
    """查询订单状态、金额、物流与售后进度。

    order_no 传 LM 开头的 14 位订单号可精确查询；不传则查当前登录账号最近的订单。
    用户问「我的订单到哪了」「什么时候发货」「退款到哪一步了」时用它。
    游客问「我的订单」时不要瞎猜，先请对方提供订单号。
    """
    return _invoke("query_order", shop_api.query_order, order_no=order_no, scope=scope, limit=limit)


@tool
def check_inventory(product: str = "", sku: str = "", variant_id: int | None = None) -> dict:
    """查实时库存：某个商品/SKU/规格还剩几件，或列出库存告急的规格。

    product 传商品名（如「露营桌」），sku 传精确编码，variant_id 传规格 id，三者任选其一；
    都不传则返回全站低库存清单。可售数量已扣除下单未支付的占用，可以直接答复用户。
    """
    return _invoke("check_inventory", shop_api.check_inventory, product=product, sku=sku, variant_id=variant_id)


@tool
def recommend_products(
    occasion: str = "",
    budget_yuan: float | None = None,
    category: str = "",
    keywords: str = "",
    based_on_order: str = "",
    product_id: int | None = None,
    limit: int = 5,
) -> dict:
    """给出购物推荐清单，返回带理由的商品卡片。

    用法：budget_yuan 是预算（元）；category 是分类 slug；occasion 描述用途（如「送同事」「露营」）；
    product_id 用于「和这件商品搭配/相似」；based_on_order 传订单号可推荐同单常一起买的商品；
    什么都不传则返回当前登录账号的复购向推荐或全站热卖。
    """
    return _invoke(
        "recommend_products",
        shop_api.recommend_products,
        occasion=occasion,
        budget_yuan=budget_yuan,
        category=category,
        keywords=keywords,
        based_on_order=based_on_order,
        product_id=product_id,
        limit=limit,
    )


@tool
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
    """按场合凑一套穿得出门的搭配：婚礼、年会、面试、约会、商务宴请、毕业典礼、见家长、派对、通勤、度假、日常。

    用户说「参加婚礼穿什么」「年会怎么穿」「面试穿搭」这类问题用它，不要用 search_products 硬搜。
    用户说的场景站内没标过（相亲、露营、出差、当伴娘…）就把原话填进 occasion，
    站内映射到最接近的场合并在 reason 里说明按哪个场合配的，不要自己替用户改写场景。
    height_cm 传用户说的身高（厘米）；体重、斤数不是身高，「身高185体重160」只填 185。
    body_type 只能是 偏瘦/标准/微胖/小个子/高挑；用户只报体重没报体型就别填 body_type，不替人估身材；
    style 只能是 正式/简约/温柔/甜美/英气/个性/休闲；用户说性格（内向、外向、干练…）就原样填 personality，
    站内按他自己说的偏好映射风格，不替用户判断；line 填 男装 或 女装；budget_yuan 是单件价格上限（元），
    站内按每件单品筛，不是整套总价上限，回复里要把合计价与这个口径一起说清。
    返回的 looks 里每件商品都带「穿搭位」（主件/外套/内搭/下装/鞋履/包袋/配饰），回答时按这个顺序说。
    """
    return _invoke(
        "outfit_recommend",
        shop_api.outfit_recommend,
        occasion=occasion,
        height_cm=height_cm,
        body_type=body_type,
        style=style,
        personality=personality,
        line=line,
        budget_yuan=budget_yuan,
        limit=limit,
    )


@tool
def find_similar_by_image(image_token: str = "", category: str = "", max_price_yuan: float | None = None, limit: int = 5) -> dict:
    """按用户上传的图找站内相似商品。

    用户在聊天窗传了图（消息里会给出图片编号 img_xxxxxxxx）就用它，image_token 原样填那个编号。
    比的是图片的主色、明暗分布、轮廓和边缘密度，站内没有识别款式、品牌或材质的能力，
    所以回答里只说「颜色轮廓接近」，别说「找到了同款/同一件」。
    category 只在用户另外说了品类时填（如「衬衫」），max_price_yuan 是单件价格上限（元）。
    返回的 items 每件带 similarity_pct（相似度百分比）与现货数，best_score 低于 70% 时要照实说只是接近。
    """
    return _invoke(
        "find_similar_by_image",
        shop_api.find_similar_by_image,
        image_token=image_token,
        category=category,
        max_price_yuan=max_price_yuan,
        limit=limit,
    )


@tool
def shipping_options(subtotal_yuan: float = 0) -> dict:
    """查配送方案：各方案运费、包邮门槛与时效，回答「几天到」「包邮吗」。"""
    return _invoke("shipping_options", shop_api.shipping_options, subtotal_yuan=subtotal_yuan)


@tool
def coupons_available() -> dict:
    """查当前可用优惠码与规则，回答「有优惠吗」「怎么用券」。"""
    return _invoke("coupons_available", shop_api.coupons_available)


@tool
def store_faq() -> dict:
    """查后台维护的常见问题口径（退换货、支付、账号、发票等），答政策类问题先对齐这里的说法。"""
    return _invoke("store_faq", shop_api.faq_hits)


CORE_TOOLS = [search_products, query_order, check_inventory, recommend_products, outfit_recommend, find_similar_by_image]
EXTRA_TOOLS = [product_detail, shipping_options, coupons_available, store_faq]
ALL_TOOLS = CORE_TOOLS + EXTRA_TOOLS

# 离线兜底直接调函数，不经过 LangChain 的工具包装
OFFLINE_HANDLERS = {
    "search_products": shop_api.search_products,
    "product_detail": shop_api.product_detail,
    "query_order": shop_api.query_order,
    "check_inventory": shop_api.check_inventory,
    "recommend_products": shop_api.recommend_products,
    "outfit_recommend": shop_api.outfit_recommend,
    "find_similar_by_image": shop_api.find_similar_by_image,
    "shipping_options": shop_api.shipping_options,
    "coupons_available": shop_api.coupons_available,
    "store_faq": shop_api.faq_hits,
}
