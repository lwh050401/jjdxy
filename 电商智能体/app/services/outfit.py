"""场合穿搭引擎：按场合、体型、身高与风格偏好，从在售商品里凑出一套真买得到的搭配。

只认商品 attributes 里的穿搭字段（由 `app/seed_catalog.py` 的 OUTFIT 词表写入）：
适用场合、穿搭位、风格、适合体型、适合身高、正式度、季节、颜色系、着装线。
匹配不到就照实说没有，不做「谁穿都好看」这类承诺，也不替用户判断长相气质。
"""

from __future__ import annotations

import json
import re

from .. import db
from . import catalog

OCCASIONS = ["婚礼", "年会", "面试", "约会", "商务宴请", "毕业典礼", "见家长", "派对", "通勤", "度假", "日常"]
STYLES = ["正式", "简约", "温柔", "甜美", "英气", "个性", "休闲"]
BODY_TYPES = ["偏瘦", "标准", "微胖", "小个子", "高挑"]
LINES = ["女装", "男装"]
# 用户嘴里的场景比商品标签杂。这里只做「说法 → 站内真有货的场合」映射，
# 命中后把原话一起写进回复，说明按哪个场合配的，不假装站内有那个场合的专属货
OCCASION_SYNONYMS = {
    "相亲": "约会", "表白": "约会", "初次见面": "约会", "看电影": "约会",
    "毕业": "毕业典礼", "答辩": "毕业典礼", "学位授予": "毕业典礼",
    "露营": "度假", "野餐": "度假", "旅行": "度假", "旅游": "度假", "徒步": "度假", "爬山": "度假",
    "上班": "通勤", "办公": "通勤", "入职": "通勤", "出差": "通勤",
    "见客户": "商务宴请", "晚宴": "商务宴请", "酒会": "商务宴请", "应酬": "商务宴请",
    "聚会": "派对", "蹦迪": "派对",
    "家长": "见家长", "订婚": "婚礼", "喜酒": "婚礼", "伴娘": "婚礼", "伴郎": "婚礼", "结婚": "婚礼",
}

# 场合对应的正式度（1 最松、5 最紧），搭配时允许上下浮动一档
OCCASION_FORMALITY = {
    "婚礼": 4, "年会": 4, "派对": 4, "商务宴请": 4, "面试": 4,
    "毕业典礼": 3, "见家长": 3, "通勤": 3, "约会": 2, "度假": 1, "日常": 1,
}
# 性格或状态描述 → 风格词表里的取值。只按用户自己说的偏好映射，不替用户判断性格
PERSONALITY_STYLES = {
    "内向": ["简约", "温柔"], "安静": ["简约", "温柔"], "低调": ["简约"], "害羞": ["温柔", "简约"],
    "慢热": ["简约", "温柔"], "随和": ["休闲", "简约"],
    "外向": ["个性", "甜美"], "活泼": ["甜美", "个性"], "开朗": ["甜美", "休闲"], "张扬": ["个性"],
    "爱热闹": ["个性", "甜美"],
    "严谨": ["正式", "简约"], "稳重": ["正式", "简约"], "成熟": ["正式"], "干练": ["英气", "简约"],
    "利落": ["英气", "简约"], "文艺": ["温柔", "简约"],
}
# 没有合适主件时的替补组合：外套 + 内搭 + 下装 也能成套
FALLBACK_PLAN = ["外套", "内搭", "下装", "鞋履"]
# 只有穿在身上才有合不合体型的单品才按体型、身高淘汰；包袋、配饰、鞋码另说
WEAR_ROLES = {"主件", "外套", "内搭", "下装"}
SEASON_BY_MONTH = {"03": "春", "04": "春", "05": "春", "06": "夏", "07": "夏", "08": "夏",
                   "09": "秋", "10": "秋", "11": "秋", "12": "冬", "01": "冬", "02": "冬"}
NEIGHBOUR_SEASONS = {"春": ["春", "秋"], "夏": ["夏", "春"], "秋": ["秋", "春"], "冬": ["冬", "秋"]}

HEIGHT_RE = re.compile(r"(\d{2,3})\s*[-~至]\s*(\d{2,3})")
# MySQL 的 JSON path 遇到中文键名必须用双引号包起来，否则报 3143
P_OCCASION_SQL = """'$."适用场合"'"""
P_ROLE_SQL = """'$."穿搭位"'"""
P_LINE_SQL = """'$."着装线"'"""


def parse_height(text: str) -> tuple[int, int] | None:
    """把「155-175cm」读成区间；「不限身高」这类返回 None。"""
    m = HEIGHT_RE.search(str(text or ""))
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    return (lo, hi) if lo <= hi else None


def current_season() -> str:
    return SEASON_BY_MONTH.get(str(db.now_iso())[5:7], "")


def season_words(season: str) -> list[str]:
    return NEIGHBOUR_SEASONS.get(season, [])


def styles_of(style: str = "", personality: str = "") -> list[str]:
    """把用户说的风格词与性格词都翻成词表里的取值。"""
    out: list[str] = []
    for token in re.split(r"[\s、,，/]+", str(style or "")):
        if token.strip() and token.strip() not in out:
            out.append(token.strip())
    text = str(personality or "")
    for word, mapped in PERSONALITY_STYLES.items():
        if word in text:
            for item in mapped:
                if item not in out:
                    out.append(item)
    return [s for s in out if s in STYLES]


def match_occasion(text: str) -> str:
    return normalize_occasion(text)[0]


def normalize_occasion(text: str) -> tuple[str, str]:
    """返回 (站内场合, 用户说法)。说法与场合不同时，调用方要照实说明是照着哪个场合配的。

    长词优先，避免「商务宴请」被「商务」或「日常」这类词抢走。
    """
    haystack = str(text or "")
    for occasion in sorted(OCCASIONS, key=len, reverse=True):
        if occasion in haystack:
            return occasion, occasion
    for word in sorted(OCCASION_SYNONYMS, key=len, reverse=True):
        if word in haystack:
            return OCCASION_SYNONYMS[word], word
    return "", ""


def match_body_type(text: str) -> str:
    haystack = str(text or "")
    aliases = {"太瘦": "偏瘦", "偏瘦": "偏瘦", "瘦": "偏瘦", "标准": "标准", "正常": "标准",
               "微胖": "微胖", "偏胖": "微胖", "小个子": "小个子", "娇小": "小个子", "不高": "小个子",
               "矮": "小个子", "高挑": "高挑", "高个": "高挑", "胖": "微胖", "壮": "微胖"}
    for word in sorted(aliases, key=len, reverse=True):
        if word in haystack:
            return aliases[word]
    return ""


# 「身高185体重160预算1000」这种一句话报三个数的说法很常见，先把体重与预算抹掉再找身高，
# 否则 160 会被当成身高，推荐出来的衣服全按 160cm 筛
WEIGHT_NUM_RE = re.compile(r"(?:体重|重量)\D{0,3}?\d{2,3}\s*(?:斤|kg|公斤|千克)?|\d{2,3}\s*(?:斤|公斤|千克|kg)", re.I)
BUDGET_NUM_RE = re.compile(r"(?:预算|价位|价格)\D{0,3}?\d{2,4}|\d{2,4}\s*(?:元|块钱|块)", re.I)


def _height_of(text: str) -> int | None:
    s = BUDGET_NUM_RE.sub(" ", WEIGHT_NUM_RE.sub(" ", str(text or "")))
    m = re.search(r"(?<!\d)(1[3-9]\d)(?!\d)(?:\s*(?:cm|厘米|公分))?", s, re.I)
    if not m:
        return None
    value = int(m.group(1))
    return value if 130 <= value <= 200 else None


def parse_question(text: str) -> dict:
    """给小马的离线路由用：从一句话里抽出场合、身高、体型与风格偏好。"""
    haystack = str(text or "")
    out: dict = {}
    occasion, spoken = normalize_occasion(haystack)
    if occasion:
        # 传用户原话：recommend 自己映射成站内场合，回复里才能写清「相亲按约会配」
        out["occasion"] = spoken
    if height := _height_of(haystack):
        out["height_cm"] = height
    if body := match_body_type(haystack):
        out["body_type"] = body
    if styles := styles_of(personality=haystack):
        out["style"] = "、".join(styles[:3])
    if any(w in haystack for w in ("男士", "先生", "男装", "男生", "男的")):
        out["line"] = "男装"
    elif any(w in haystack for w in ("女士", "女生", "女装", "女孩", "女的")):
        out["line"] = "女装"
    elif "男" in haystack and "女" not in haystack:
        out["line"] = "男装"
    elif "女" in haystack and "男" not in haystack:
        out["line"] = "女装"
    return out


def _attrs(product: dict) -> dict:
    return product.get("attributes") or {}


def vocabulary() -> dict:
    """筛选表单只列站内真有现货的取值，别让「毕业典礼」这种选了永远查不到的词挂在页面上。

    数的是款数（一个商品链接算一款），不是库存件数：下拉框写「4.5 万件现货」会把人吓一跳，
    而且跟推荐结果里能拿出的单品数量对不上。
    """
    rows = db.all_(
        f"""SELECT p.attributes
            FROM products p
            JOIN (SELECT product_id, SUM(stock_qty - reserved_qty) AS available
                  FROM variants WHERE status = 'active' GROUP BY product_id) price ON price.product_id = p.id
            WHERE p.status = 'active' AND price.available > 0 AND JSON_EXTRACT(p.attributes, {P_ROLE_SQL}) IS NOT NULL""",
        {},
    )
    counts = {key: {} for key in ("场合", "风格", "体型", "着装线")}
    for row in rows:
        attrs = _json_attrs(row["attributes"])
        for occasion in attrs.get("适用场合") or []:
            if occasion in OCCASIONS:
                counts["场合"][occasion] = counts["场合"].get(occasion, 0) + 1
        for style in attrs.get("风格") or []:
            if style in STYLES:
                counts["风格"][style] = counts["风格"].get(style, 0) + 1
        for body in attrs.get("适合体型") or []:
            if body in BODY_TYPES:
                counts["体型"][body] = counts["体型"].get(body, 0) + 1
        line = attrs.get("着装线")
        if line in LINES:
            counts["着装线"][line] = counts["着装线"].get(line, 0) + 1
    return {key: sorted(((name, n) for name, n in value.items()), key=lambda kv: (-kv[1], kv[0]))
            for key, value in counts.items()}


def _json_attrs(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _outfit_products(occasion: str, budget_cents: int | None, line: str = "") -> list[dict]:
    params: dict = {"occasion": occasion}
    budget_clause = ""
    if budget_cents:
        budget_clause = "AND price.min_cents <= :budget"
        params["budget"] = int(budget_cents)
    # 男女装一起查会把池子占满，用户点了线就在 SQL 里先按着装线过滤（中性两边都算）
    line_clause = ""
    if line in LINES:
        line_clause = f"""AND (JSON_UNQUOTE(JSON_EXTRACT(p.attributes, {P_LINE_SQL})) IN (:line, '中性'))"""
        params["line"] = line
    rows = db.all_(
        f"""{catalog.BASE_SELECT}
            WHERE p.status = 'active' AND price.available > 0
              AND JSON_CONTAINS(IFNULL(JSON_EXTRACT(p.attributes, {P_OCCASION_SQL}), '[]'), JSON_QUOTE(:occasion))
              AND JSON_EXTRACT(p.attributes, {P_ROLE_SQL}) IS NOT NULL
              {budget_clause}
              {line_clause}
            ORDER BY p.rating_avg DESC, p.sold_count DESC LIMIT 400""",
        params,
    )
    return [catalog.decorate(r) for r in rows]


def _fits(product: dict, role: str, *, body_type: str, height: int | None, season: str, line: str) -> tuple[bool, int]:
    """体型与身高只按商品自己写的版型建议判断，不达标就淘汰。

    包袋、配饰、鞋履没有「合不合体型」这回事，只加分不淘汰，否则一套里永远配不出包。
    """
    attrs = _attrs(product)
    score = 0
    if line and attrs.get("着装线") not in (line, "中性"):
        return False, 0
    body = attrs.get("适合体型") or []
    worn = role in WEAR_ROLES
    if body_type and body:
        if body_type in body:
            score += 15
        elif worn:
            return False, 0
    if height and worn:
        span = parse_height(attrs.get("适合身高") or "")
        if span and not (span[0] <= height <= span[1]):
            return False, 0
        if span:
            score += 10
    seasons = attrs.get("季节") or []
    if season and seasons:
        if set(season_words(season)) & set(seasons):
            score += 8
        elif "春" not in seasons and "秋" not in seasons:
            score += 2
    return True, score


def _score(product: dict, *, formality: int, styles: list[str], strict_style: bool = True) -> tuple[bool, int]:
    attrs = _attrs(product)
    try:
        level = int(attrs.get("正式度") or 0)
    except (TypeError, ValueError):
        level = 0
    if not level or abs(level - formality) > 1:
        return False, 0
    score = 20 - abs(level - formality) * 8
    if styles:
        hit = [s for s in styles if s in (attrs.get("风格") or [])]
        if hit:
            score += len(hit) * 12
        elif strict_style:
            return False, 0
    return True, score


def _rank(product: dict, role: str, *, formality: int, styles: list[str], body_type: str, height: int | None,
          season: str, line: str) -> float | None:
    # 风格只强制约束穿在身上的那几件；包、鞋、配饰按场合与正式度配就行，
    # 否则「温柔风」会把所有手拿包筛掉，一套里永远缺一个包
    ok_form, score_form = _score(product, formality=formality, styles=styles, strict_style=role in WEAR_ROLES)
    ok_fit, score_fit = _fits(product, role, body_type=body_type, height=height, season=season, line=line)
    if not (ok_form and ok_fit):
        return None
    return score_form + score_fit + min(8, float(product.get("rating_avg") or 0))


def _budget_bonus(price_cents: int, budget_cents: int) -> float:
    """给了预算就在合格品里偏向便宜的：最多 8 分，与评分加分同量级，不会盖过版型与正式度。

    不加这一项，预算 1000 会顶格配出 970 的西装加 981 的鞋，等于没听见用户说的预算。
    """
    if not budget_cents or price_cents <= 0:
        return 0.0
    return 8.0 * max(0.0, 1 - float(price_cents) / budget_cents)


def _pick(pool: list[dict], role: str, *, used: set[int], criteria: dict, budget_cents: int = 0) -> tuple[dict, float] | None:
    ranked = []
    for product in pool:
        if product["id"] in used or _attrs(product).get("穿搭位") != role:
            continue
        score = _rank(product, role, **criteria)
        if score is not None:
            ranked.append((score + _budget_bonus(int(product["price_cents"]), budget_cents), product))
    if not ranked:
        return None
    # 给了预算就在同分里先挑便宜的，否则凑出来的一套总是顶格价，预算等于没起作用
    ranked.sort(key=lambda item: (-item[0], item[1]["price_cents"] if budget_cents else 0,
                                  -float(item[1].get("rating_avg") or 0)))
    return ranked[0][1], ranked[0][0]


def _why(product: dict, *, occasion: str, body_type: str, height: int | None, styles: list[str]) -> str:
    attrs = _attrs(product)
    bits = [f"适合{occasion}", f"正式度 {attrs.get('正式度')}"]
    if hit := [s for s in styles if s in (attrs.get("风格") or [])]:
        bits.append("、".join(hit) + "风")
    if body_type and body_type in (attrs.get("适合体型") or []):
        bits.append(f"{body_type}可穿")
    if height and (span := parse_height(attrs.get("适合身高") or "")):
        bits.append(f"建议身高 {span[0]}-{span[1]}cm")
    facts = attrs.get("亮点") or []
    if facts:
        bits.append(str(facts[0]))
    return "，".join(bits)


def recommend(*, occasion: str = "", height_cm=None, body_type: str = "", style: str = "",
              personality: str = "", line: str = "", budget_yuan=None, season: str = "",
              limit: int = 2) -> dict:
    """凑一套：先定主件，再按主件自带的搭配位补齐；凑不齐的位子在 missing 里照实说。"""
    occasion, spoken = normalize_occasion(occasion)
    if not occasion:
        return {
            "found": False,
            "message": "没认出具体场合。可以说：" + "、".join(OCCASIONS) + "。",
            "items": [],
        }
    # 用户说的是「相亲」这类站内没打过的标签，回复里要讲清按哪个场合配的
    spoken_note = f"「{spoken}」站内按「{occasion}」来配，" if spoken != occasion else ""
    try:
        height = int(height_cm) if height_cm else None
    except (TypeError, ValueError):
        height = None
    body_type = body_type if body_type in BODY_TYPES else ""
    line = line if line in LINES else ""
    budget_cents = int(round(float(budget_yuan) * 100)) if budget_yuan else None
    season = season if season in NEIGHBOUR_SEASONS else current_season()
    styles = styles_of(style=style, personality=personality)
    formality = OCCASION_FORMALITY[occasion]
    criteria = {"formality": formality, "styles": styles, "body_type": body_type,
                "height": height, "season": season, "line": line}

    pool = _outfit_products(occasion, budget_cents, line)
    if not pool:
        empty = f"站内没有标为「{occasion}」的在售现货商品"
        if line:
            empty = f"站内标为「{occasion}」的现货里没有{line}的商品"
        return {
            "found": False,
            "occasion": occasion,
            "message": spoken_note + empty
            + (f"，{float(budget_yuan):,.0f} 元预算内也没有" if budget_yuan else "")
            + "。可以放宽预算或换个场合词再试。",
            "items": [],
        }

    # 同一品名只有品牌不同，凑两套会像复读；按「品类」去重，每个品名留下最合适的那一件
    mains, best = [], {}
    for p in pool:
        if _attrs(p).get("穿搭位") != "主件":
            continue
        score = _rank(p, "主件", **criteria)
        if score is None:
            continue
        score += _budget_bonus(int(p["price_cents"]), budget_cents or 0)
        name = _attrs(p).get("品类") or p["title"]
        if name not in best or score > best[name][0]:
            best[name] = (score, p, name)
    mains = list(best.values())
    mains.sort(key=lambda item: (-item[0], -float(item[1].get("rating_avg") or 0)))

    looks: list[dict] = []
    used_across: set[int] = set()
    for main_score, main, _name in mains[: max(1, min(3, int(limit)))]:
        attrs = _attrs(main)
        # 上一套用过的鞋和包先避开，两套才有区别；避开后配不齐就退回允许复用
        used = {main["id"]} | used_across
        chosen = [(main, "主件", main_score)]
        look_line = attrs.get("着装线") if attrs.get("着装线") != "中性" else line
        roles = list(attrs.get("搭配位") or ["鞋履", "包袋"])
        for role in roles:
            picked = _pick(pool, role, used=used, criteria={**criteria, "line": look_line}, budget_cents=budget_cents or 0)
            if not picked and used_across:
                picked = _pick(pool, role, used={main["id"]}, criteria={**criteria, "line": look_line},
                               budget_cents=budget_cents or 0)
            if not picked:
                chosen.append((None, role, 0))
                continue
            piece, piece_score = picked
            used.add(piece["id"])
            used_across.add(piece["id"])
            chosen.append((piece, role, piece_score))
        looks.append(_build_look(chosen, occasion=occasion, styles=styles, body_type=body_type,
                                 height=height, season=season, line=look_line, index=len(looks) + 1,
                                 budget_yuan=budget_yuan))

    if not looks:
        filled = []
        used: set[int] = set()
        for role in FALLBACK_PLAN:
            picked = _pick(pool, role, used=used, criteria=criteria)
            if picked:
                used.add(picked[0]["id"])
                filled.append((picked[0], role, picked[1]))
        # 只剩一件包或一只鞋不算一套，宁可不给
        if len(filled) >= 2 and any(role in ("外套", "内搭") for _piece, role, _s in filled):
            looks.append(_build_look(filled, occasion=occasion, styles=styles, body_type=body_type,
                                     height=height, season=season, line=line, index=1,
                                     budget_yuan=budget_yuan, fallback=True))

    if not looks:
        return {
            "found": False,
            "occasion": occasion,
            "message": spoken_note + f"标为「{occasion}」的现货里，按你给的身高、体型和风格没能凑出一套。"
            "可以放宽条件，或者让我按单品再查一次。",
            "items": [],
        }

    flat = [item for look in looks for item in look["items"]]
    return {
        "found": True,
        "occasion": occasion,
        "season": season,
        "formality": formality,
        "styles": styles,
        "body_type": body_type,
        "height_cm": height,
        "line": line or "不限",
        "strategy": "场合穿搭",
        "reason": spoken_note + f"从站内标为「{occasion}」的在售现货里凑的搭配"
        + (f"，按{body_type}体型" if body_type else "")
        + (f"、身高 {height}cm" if height else "")
        + (f"、{'、'.join(styles)}风格" if styles else "")
        + (f"，单件 {float(budget_yuan):,.0f} 元以内" if budget_yuan else ""),
        "looks": looks,
        "items": [dict(i) for i in flat],
        "count": len(flat),
        "notes": looks[0]["notes"],
    }


def _build_look(chosen: list[tuple[dict | None, str, float]], *, occasion: str, styles: list[str],
                body_type: str, height: int | None, season: str, line: str, index: int,
                budget_yuan=None, fallback: bool = False) -> dict:
    items, missing, colors = [], [], []
    for piece, role, _score in chosen:
        if piece is None:
            missing.append(role)
            continue
        colors.append(_attrs(piece).get("颜色系") or "")
        items.append({
            "slug": piece["slug"],
            "title": piece["title"],
            "role": role,
            "price_cents": int(piece["price_cents"]),
            "price_yuan": round(int(piece["price_cents"]) / 100, 2),
            "available": piece.get("available"),
            "image": piece.get("image"),
            "url": f"/product/{piece['slug']}",
            "why": _why(piece, occasion=occasion, body_type=body_type, height=height, styles=styles),
        })
    total = round(sum(i["price_yuan"] for i in items), 2)
    if fallback:
        head = " + ".join(i["role"] for i in items)
    else:
        head = items[0]["title"] if items else "待定"
    return {
        "name": f"第 {index} 套：{head}",
        "line": line or "中性为主",
        "items": items,
        "total_yuan": total,
        "missing": missing,
        "notes": _notes(occasion=occasion, total=total, season=season, height=height, body_type=body_type,
                        missing=missing, colors=[c for c in colors if c], budget_yuan=budget_yuan),
    }


def _notes(*, occasion: str, total: float, season: str, height: int | None, body_type: str,
           missing: list[str], colors: list[str], budget_yuan=None) -> list[str]:
    """搭配说明只写这批字段读得出的话，凑不齐的位子照实说。"""
    notes = []
    if len(colors) >= 2 and len(set(colors)) == 1:
        notes.append(f"整套颜色同属{colors[0]}，互相不会抢。")
    if season:
        notes.append(f"季节只参与排序：站内标了{season}或春秋通用的款排在前面，不当季的没有直接排除。")
    if height:
        notes.append(f"身高按 {height}cm 过滤：只保留建议区间含这个数的衣服，鞋、包、配饰不看身高。")
    if body_type:
        notes.append(f"版型按{body_type}挑：主件、外套、内搭、下装只选写了适合{body_type}的款，松紧仍以详情页尺码表为准。")
    notes.append(f"这一套合计 ¥{total:,.2f}。")
    if budget_yuan and total > float(budget_yuan):
        notes.append(f"整套超过了 {float(budget_yuan):,.0f} 元：这个数是按每件单品筛的，不是整套上限，想压总价就减掉包袋或配饰。")
    if missing:
        notes.append("这几个位子站内没有同时满足条件的现货，先空着：" + "、".join(missing) + "。")
    return notes
