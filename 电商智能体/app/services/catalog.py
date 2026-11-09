"""商品目录：检索、筛选、详情与联想，全部读 MySQL。"""

from __future__ import annotations

import json
import re

from .. import db
from . import synonyms

SORTS = {
    "relevant": "p.sold_count DESC, p.rating_avg DESC, p.id ASC",
    "popular": "p.sold_count DESC, p.rating_count DESC, p.id ASC",
    "newest": "p.published_at DESC, p.id DESC",
    "price_asc": "price.min_cents ASC, p.id ASC",
    "price_desc": "price.min_cents DESC, p.id ASC",
    "rating": "p.rating_avg DESC, p.rating_count DESC, p.id ASC",
}

STOP = re.compile(r"[^\w一-鿿]+")

# 「露营装备」「户外野餐」这类说法里，前后那截只是口语的框，站内没有哪件商品标题写着「装备」。
# 真正带货的是中间那个场景词（露营、野餐），tags 与 attributes 里写的就是它。
GENERIC_TAILS = ("装备", "用品", "器材", "神器", "好物", "大全", "清单", "东西", "礼物", "工具")
GENERIC_HEADS = ("户外", "居家", "家用", "办公", "旅行", "日常", "便携", "室内", "车载")


def without_generic(token: str) -> str:
    """剥掉口语框：「露营装备」→「露营」、「户外野餐」→「野餐」。剥完太短的不剥，免得剩个单字捞出整面货架。"""
    for tail in GENERIC_TAILS:
        if token.endswith(tail) and len(token) - len(tail) >= 2:
            return token[: -len(tail)]
    for head in GENERIC_HEADS:
        if token.startswith(head) and len(token) - len(head) >= 2:
            return token[len(head):]
    return ""


def needles(q: str) -> list[str]:
    """一次检索要挨个试的比对串：用户原话排第一，同义词与去框的说法跟在后面。

    去框之后再展开一次同义词：「猫咪用品」剥掉「用品」剩「猫咪」，而站内把猫的东西写成「猫砂盆」「猫爬架」，
    只有再走一层词表才接得住。
    """
    out: list[str] = []

    def add(value: str) -> None:
        core = STOP.sub("", value).lower()
        if core and core not in out:
            out.append(core)

    for value in synonyms.expand(q):
        add(value)
        core = STOP.sub("", value).lower()
        head = without_generic(core)
        if head:
            for extra in synonyms.expand(head):
                add(extra)
    return out


def tokens(q: str) -> list[str]:
    """用户问法按空格切段：「防水 手表」是两条独立条件，不是一个连续串。"""
    return [t for t in re.split(r"\s+", (q or "").strip()) if t]


def _segment_terms(q: str, params: dict, prefix: str, *, title_only: bool = False) -> list[list[tuple[str, int]]]:
    """每个空格段一组 `(检索式, 权重)`：段内各项互相 OR，参数名 {prefix}{段号}_{序号}。

    比的不只是标题/描述/品牌：`tags` 里存的是分类器给的场景词（户外、通勤、送礼），
    `attributes` 里存的是参数与亮点（适用人群、口味、功效），用户就说「户外 帐篷」「无糖 可乐」时，
    这两个字段才是站内唯一写得上的地方。只比标题与描述，这类问法会集体 0 件。

    权重只被 `title_rank()` 用：整串命中 2 分，插字命中 1 分。筛选必须宽松（别让用户搜「折叠桌」
    看不到「折叠露营桌」），排序必须势利（搜「台式机」不能把「台式饮水机」抬到第一屏）。
    """
    segments = []
    parts = tokens(q)
    for gi, token in enumerate(parts):
        per: list[tuple[str, int]] = []
        for ni, needle in enumerate(needles(token)):
            if len(parts) > 1 and needle in GENERIC_TAILS:
                # 「露营 装备」里的「装备」不指向具体的货，两段问法留着它 AND 下去只会把结果清零；
                # 单搜「装备」时不丢，商品标签里确实写着它。
                # 「办公」「户外」这类前缀段不丢：用户单独打出它们时是真条件（办公 椅 ≠ 椅）。
                continue
            key = f"{prefix}{gi}_{ni}"
            params[key] = f"%{needle}%"
            expr = f"LOWER(REPLACE(p.title,' ','')) LIKE :{key}"
            if title_only:
                per.append((expr, 2))
            else:
                per.append(("(" + " OR ".join([
                    expr,
                    f"LOWER(IFNULL(p.description,'')) LIKE :{key}",
                    f"LOWER(IFNULL(p.brand,'')) LIKE :{key}",
                    f"LOWER(IFNULL(CAST(p.tags AS CHAR),'')) LIKE :{key}",
                    f"LOWER(IFNULL(CAST(p.attributes AS CHAR),'')) LIKE :{key}",
                ]) + ")", 2))
            gap = synonyms.gap_pattern(needle)
            if gap:
                # 插字兜底只比标题：描述与亮点里凑齐这几个字太容易，「毛衣」会捞出「婴儿毛巾」这种不相干的货
                gkey = f"{prefix}{gi}_{ni}_gap"
                params[gkey] = gap
                per.append((f"LOWER(REPLACE(p.title,' ','')) LIKE :{gkey}", 1))
        if per:
            segments.append(per)
    return segments


def q_clauses(q: str, params: dict, *, title_only: bool = False) -> str:
    """段与段之间 AND、段内 OR 的检索式。

    老写法把整句空格抹掉当一个连续串比，站内 123 件手表加一批防水商品会一起被判成搜不到
    ——40 句常见组合问法挂了 32 句。没空格的说法（「防水手表」）仍按连续串比，行为与改前一致。

    返回的串调用方要自己用括号包住 —— `search()` 的分页计数复用的是同一个 clause，
    两边检索式必须一模一样，否则「共 N 件」和翻页结果会对不上。
    """
    return " AND ".join(
        "(" + " OR ".join(expr for expr, _ in terms) + ")" for terms in _segment_terms(q, params, "q", title_only=title_only)
    )


def title_rank(q: str, params: dict) -> str:
    """标题里命中了几段、命中得多准，用来把真正对题的商品顶到前面。

    「静音 风扇」单靠 AND 会把描述里同时出现这两个词的交换机捞进来，相关性排序（销量、评分）
    救不了它；标题命中的件数比描述命中的更可信，所以先按这个数排。
    整串命中比插字命中可信，所以一份算两分。
    """
    terms = _segment_terms(q, params, "r", title_only=True)
    if not terms:
        return ""
    return " + ".join(
        f"(CASE WHEN {expr} THEN {weight} ELSE 0 END)" for segment in terms for expr, weight in segment
    )


def _json(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _price(row: dict) -> int:
    return int(row.get("min_cents") or row.get("price_cents") or 0)


def decorate(row: dict, stock: int | None = None) -> dict:
    price = int(row["price_cents"])
    max_price = int(row.get("max_cents") or price)
    out = {
        "id": int(row["id"]),
        "slug": row["slug"],
        "title": row["title"],
        "subtitle": row.get("subtitle"),
        "brand": row.get("brand"),
        "description": row.get("description"),
        "status": row["status"],
        "currency": row.get("currency") or "CNY",
        "price_cents": price,
        "compare_at_cents": int(row["compare_at_cents"]) if row.get("compare_at_cents") else None,
        "price_range": {"min": price, "max": max(max_price, price)},
        "tags": _json(row.get("tags"), []),
        "attributes": _json(row.get("attributes"), {}),
        "rating_avg": float(row.get("rating_avg") or 0),
        "rating_count": int(row.get("rating_count") or 0),
        "sold_count": int(row.get("sold_count") or 0),
        "featured": bool(row.get("featured")),
        "category": {"id": int(row["category_id"]), "slug": row.get("category_slug"), "name": row.get("category_name")}
        if row.get("category_id")
        else None,
        "image": row.get("image_url"),
        "published_at": row.get("published_at"),
    }
    out["available"] = int(stock if stock is not None else row.get("available") or 0)
    return out


BASE_SELECT = """
SELECT p.*, price.min_cents, price.max_cents, price.available,
       c.slug AS category_slug, c.name AS category_name, img.url AS image_url
FROM products p
LEFT JOIN categories c ON c.id = p.category_id
LEFT JOIN (
  SELECT product_id, MIN(price_cents) AS min_cents, MAX(price_cents) AS max_cents,
         SUM(stock_qty - reserved_qty) AS available
  FROM variants WHERE status = 'active' GROUP BY product_id
) price ON price.product_id = p.id
LEFT JOIN (
  SELECT product_id, MIN(url) AS url FROM product_images GROUP BY product_id
) img ON img.product_id = p.id
"""


def search(
    *,
    q: str = "",
    category: str = "",
    brand: str = "",
    tag: str = "",
    min_price: int | None = None,
    max_price: int | None = None,
    min_rating: float | None = None,
    in_stock: bool = False,
    featured: bool = False,
    status: str = "active",
    sort: str = "relevant",
    page: int = 1,
    per_page: int = 12,
) -> dict:
    where, params = [], {}
    if status:
        where.append("p.status = :status")
        params["status"] = status
    if q:
        text = q_clauses(q, params)
        if text:
            where.append(f"({text})")
    if category:
        where.append("(c.slug = :category OR c.id = :category_id)")
        params["category"] = category
        params["category_id"] = int(category) if category.isdigit() else 0
    if brand:
        where.append("p.brand = :brand")
        params["brand"] = brand
    if tag:
        where.append("JSON_SEARCH(p.tags, 'one', :tag, NULL, '$[*]') IS NOT NULL")
        params["tag"] = tag
    if min_price is not None:
        where.append("price.min_cents >= :min_price")
        params["min_price"] = int(min_price)
    if max_price is not None:
        where.append("price.min_cents <= :max_price")
        params["max_price"] = int(max_price)
    if min_rating:
        where.append("p.rating_avg >= :min_rating")
        params["min_rating"] = float(min_rating)
    if in_stock:
        where.append("price.available > 0")
    if featured:
        where.append("p.featured = 1")

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = SORTS.get(sort, SORTS["relevant"])
    # 相关性排序才加「标题命中段数」这一级；用户明确选了价格/销量排序时不能插队。
    # 绑定参数单独放一个字典：计数那条 SQL 不带 ORDER BY，多喂几个没用到的参数会被 SQLAlchemy 拒掉。
    rank_params: dict = {}
    if q and sort in ("", "relevant"):
        rank = title_rank(q, rank_params)
        if rank:
            order = f"{rank} DESC, {order}"
    page = max(1, int(page or 1))
    per_page = min(60, max(1, int(per_page or 12)))
    total = int(
        db.value(
            f"SELECT COUNT(*) FROM products p LEFT JOIN categories c ON c.id = p.category_id "
            f"LEFT JOIN (SELECT product_id, MIN(price_cents) AS min_cents, SUM(stock_qty - reserved_qty) AS available FROM variants WHERE status='active' GROUP BY product_id) price ON price.product_id = p.id {clause}",
            params,
        )
        or 0
    )
    rows = db.all_(f"{BASE_SELECT} {clause} ORDER BY {order} LIMIT :limit OFFSET :offset",
                   {**params, **rank_params, "limit": per_page, "offset": (page - 1) * per_page})
    return {
        "items": [decorate(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


def get(slug_or_id: str | int, *, count_view: bool = False) -> dict | None:
    key = str(slug_or_id)
    params = {"k": key}
    predicate = "p.id = :k" if key.isdigit() else "p.slug = :k"
    row = db.get(f"{BASE_SELECT} WHERE {predicate} LIMIT 1", params)
    if not row:
        return None
    product = decorate(row)
    variants = db.all_(
        "SELECT v.*, (v.stock_qty - v.reserved_qty) AS available FROM variants v WHERE v.product_id = :pid AND v.status = 'active' ORDER BY v.price_cents ASC, v.id ASC",
        {"pid": product["id"]},
    )
    product["variants"] = [
        {
            "id": int(v["id"]),
            "sku": v["sku"],
            "title": v["title"],
            "options": _json(v.get("options"), {}),
            "price_cents": int(v["price_cents"]),
            "stock_qty": int(v["stock_qty"]),
            "reserved_qty": int(v["reserved_qty"]),
            "available": int(v["available"] or 0),
            "weight_gram": int(v["weight_gram"] or 0),
            "image_url": v.get("image_url"),
        }
        for v in variants
    ]
    product["images"] = [
        {"id": int(r["id"]), "url": r["url"], "alt": r.get("alt"), "sort": int(r["sort"])}
        for r in db.all_("SELECT * FROM product_images WHERE product_id = :pid ORDER BY sort ASC, id ASC", {"pid": product["id"]})
    ]
    product["option_groups"] = _option_groups(product["variants"])
    product["reviews"] = review_summary(product["id"])
    product["related"] = related(product, limit=6)
    if count_view:
        db.insert(
            "analytics_events",
            {"kind": "product_view", "product_id": product["id"], "user_id": None, "meta": {"slug": product["slug"]}, "created_at": db.now_iso()},
        )
    return product


def _option_groups(variants: list[dict]) -> list[dict]:
    """把规格拆成「颜色 / 容量」这类可选组，供详情页做切换。"""
    groups: dict[str, list] = {}
    for variant in variants:
        for name, value in (variant["options"] or {}).items():
            groups.setdefault(name, [])
            if value not in groups[name]:
                groups[name].append(value)
    return [{"name": name, "values": values} for name, values in groups.items() if len(values) > 1]


def review_summary(product_id: int) -> dict:
    row = db.get(
        "SELECT COUNT(*) AS n, AVG(rating) AS avg, SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END) AS five FROM reviews WHERE product_id = :pid AND status = 'published'",
        {"pid": product_id},
    )
    return {
        "count": int(row["n"] or 0),
        "avg": round(float(row["avg"] or 0), 2),
        "five_star": int(row["five"] or 0),
    }


def reviews(product_id: int, *, rating: int | None = None, page: int = 1, per_page: int = 10) -> dict:
    where = "r.product_id = :pid AND r.status = 'published'"
    params: dict = {"pid": product_id}
    if rating:
        where += " AND r.rating = :rating"
        params["rating"] = int(rating)
    page, per_page = max(1, page), per_page
    total = int(db.value(f"SELECT COUNT(*) FROM reviews r WHERE {where}", params) or 0)
    rows = db.all_(
        f"SELECT r.*, u.name AS author FROM reviews r JOIN users u ON u.id = r.user_id WHERE {where} ORDER BY r.created_at DESC LIMIT :limit OFFSET :offset",
        {**params, "limit": per_page, "offset": (page - 1) * per_page},
    )
    return {
        "total": total,
        "items": [
            {
                "id": r["id"],
                "rating": int(r["rating"]),
                "title": r.get("title"),
                "body": r["body"],
                "author": r["author"],
                "variant_label": r.get("variant_label"),
                "helpful_count": int(r["helpful_count"]),
                "reply": r.get("reply"),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
        "page": page,
        "pages": max(1, -(-total // per_page)),
    }


def related(product: dict, *, limit: int = 6) -> list[dict]:
    """相关推荐：同分类优先，再用标签交集补齐。"""
    rows = db.all_(
        f"""{BASE_SELECT} WHERE p.status = 'active' AND p.id <> :pid
           ORDER BY (CASE WHEN p.category_id = :cat THEN 0 ELSE 1 END), p.sold_count DESC LIMIT :limit""",
        {"pid": product["id"], "cat": (product["category"] or {}).get("id") or 0, "limit": limit},
    )
    return [decorate(r) for r in rows]


def categories(*, active_only: bool = True) -> list[dict]:
    rows = db.all_(
        f"""SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id AND p.status = 'active') AS product_count
            FROM categories c {'WHERE c.active = 1' if active_only else ''} ORDER BY c.sort ASC, c.id ASC"""
    )
    return [
        {
            "id": int(r["id"]),
            "slug": r["slug"],
            "name": r["name"],
            "description": r.get("description"),
            "icon": r.get("icon"),
            "sort": int(r["sort"]),
            "active": bool(r["active"]),
            "product_count": int(r["product_count"]),
        }
        for r in rows
    ]


def facets(*, category: str = "") -> dict:
    scope = " AND c.slug = :category" if category else ""
    params = {"category": category} if category else {}
    brands = db.all_(
        f"SELECT p.brand AS name, COUNT(*) AS count FROM products p LEFT JOIN categories c ON c.id = p.category_id "
        f"WHERE p.status = 'active' AND p.brand IS NOT NULL AND p.brand <> ''{scope} GROUP BY p.brand ORDER BY count DESC",
        params,
    )
    price = db.get(
        f"SELECT MIN(price_cents) AS min, MAX(price_cents) AS max FROM products p LEFT JOIN categories c ON c.id = p.category_id "
        f"WHERE p.status = 'active'{scope}",
        params,
    )
    tags: dict[str, int] = {}
    for row in db.all_(
        f"SELECT p.tags FROM products p LEFT JOIN categories c ON c.id = p.category_id WHERE p.status = 'active'{scope}",
        params,
    ):
        for tag in _json(row["tags"], []):
            tags[tag] = tags.get(tag, 0) + 1
    rating = db.all_(
        f"SELECT FLOOR(rating_avg) AS bucket, COUNT(*) AS count FROM products p LEFT JOIN categories c ON c.id = p.category_id "
        f"WHERE p.status = 'active'{scope} GROUP BY bucket",
        params,
    )
    return {
        "brands": [{"name": b["name"], "count": int(b["count"])} for b in brands],
        "price": {"min": int((price or {}).get("min") or 0), "max": int((price or {}).get("max") or 0)},
        "tags": [{"name": name, "count": count} for name, count in sorted(tags.items(), key=lambda kv: -kv[1])],
        "rating": [{"min": bucket, "count": int(count)} for bucket, count in sorted(((r["bucket"], r["count"]) for r in rating), reverse=True)],
    }


def suggest(q: str, *, limit: int = 6) -> list[dict]:
    params: dict = {"limit": limit}
    text = q_clauses(q, params, title_only=True)
    if not text:
        return []
    rows = db.all_(
        "SELECT p.id, p.slug, p.title, p.price_cents, img.url AS image_url FROM products p "
        "LEFT JOIN (SELECT product_id, MIN(url) AS url FROM product_images GROUP BY product_id) img ON img.product_id = p.id "
        f"WHERE p.status = 'active' AND ({text}) ORDER BY p.sold_count DESC LIMIT :limit",
        params,
    )
    return [{"id": int(r["id"]), "slug": r["slug"], "title": r["title"], "price_cents": int(r["price_cents"]), "image": r.get("image_url")} for r in rows]
