"""小马的离线大脑：没有模型 Key 时，用意图路由 + 同一批工具把话答完。

这里刻意不复制旧客服的静态文案，所有数字都通过 tools 现取，避免前台与库内口径不一致。
"""

from __future__ import annotations

import re
import time

from .. import db
from ..services import catalog, outfit
from ..settings import get_settings
from . import shop_api, tools

ORDER_NO = re.compile(r"\bLM\d{6}[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}\b", re.I)

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[，。！？、,.!?；;：:()（）【】\"'’”]", " ", str(text or "").lower())).strip()


def _support_email() -> str:
    return db.value("SELECT `value` FROM settings WHERE `key` = 'support_email'") or get_settings().support_email


def _category_hint() -> str:
    """兜底文案只报站内真有商品的分类，别承诺没有的品类。"""
    rows = db.all_(
        """SELECT c.name, COUNT(p.id) AS n FROM categories c
           LEFT JOIN products p ON p.category_id = c.id AND p.status = 'active'
           WHERE c.active = 1 GROUP BY c.id HAVING n > 0 ORDER BY n DESC LIMIT 6"""
    )
    return "、".join(r["name"] for r in rows) or "商品名"


def has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def _yuan(amount) -> str:
    return f"¥{float(amount or 0):,.2f}"


def _money(cents) -> str:
    return db.money(cents)


def _plan_answer(subtotal_yuan: float = 0) -> str:
    data = shop_api.shipping_options(subtotal_yuan or 0)
    lines = [
        f"· {p['name']}：{('免费' if p['fee_yuan'] == 0 else _yuan(p['fee_yuan']))}"
        + (f"，满 {_yuan(p['free_over_yuan'])} 包邮" if p["free_over_yuan"] else "")
        + (f"，{p['eta']}" if p["eta"] else "")
        for p in data["plans"]
    ]
    return "\n".join(
        [
            f"全场满 {_yuan(data['free_shipping_threshold_yuan'])} 免标准快递运费。",
            *lines,
            "结算页可以选择配送方案，出库后在「我的订单」查看承运商与运单号。",
        ]
    )


def _promo_answer() -> str:
    coupons = shop_api.coupons_available()["coupons"]
    if not coupons:
        return "当前没有可领取的优惠码，活动会不定期上线，可以先看首页公告。"
    return "现在可用的优惠：\n" + "\n".join(f"· 优惠码 {c['code']}：{c['label']}" for c in coupons) + "\n结算页输入优惠码即可抵扣。"


def _faq_answer(text: str) -> tuple[str, str] | tuple[None, None]:
    for row in db.all_("SELECT * FROM support_faqs WHERE active = 1 ORDER BY sort ASC, hits DESC"):
        words = [w.strip().lower() for w in re.split(r"[,，;；\s]+", str(row.get("keywords") or "")) if w.strip()]
        if words and any(w in text for w in words):
            db.run("UPDATE support_faqs SET hits = hits + 1 WHERE id = :id", {"id": int(row["id"])})
            return f"faq:{row['id']}", row["answer"]
    return None, None


# 问句里跟「找哪件商品」无关的词，先剥掉再拿去检索
_FILLER = r"请问|想问|帮我|我|一下|有没有|有吗|有货|现货|库存|还有|卖完|缺货|补货|货|多少钱|什么价|价格|贵|便宜|推荐|介绍|看看|哪些|哪款|这个|那个|适合|值得|买|预算|以内|以下|左右|上下|元|块|的|了|吗|呢|嘛|要|能|可以"


def clean_needle(text: str) -> str:
    s = re.sub(r"\d+(\.\d+)?", " ", text)
    s = re.sub(_FILLER, " ", s)
    # 「推荐个双肩包」「这件衬衫什么价」这类说法，去掉指示词和量词后才能命中标题
    s = re.sub(r"(^|\s)[这那一]?[个件款只台](?=[\u4e00-\u9fff])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _category_slug(token: str) -> str:
    if len(token) < 2:
        return ""
    for row in db.all_("SELECT slug, name FROM categories"):
        name = str(row["name"] or "")
        if token == name or (len(token) >= 2 and (token in name or name in token)):
            return row["slug"]
    return ""


def _has_hits(token: str) -> bool:
    return bool(token) and bool(catalog.search(q=token, in_stock=True, per_page=1)["items"])


def _title_hit(token: str) -> bool:
    return bool(token) and bool(catalog.suggest(token, limit=1))


def pick_keyword(needle: str) -> str:
    """分词后挑最能定位商品的词：整串优先，其次中文品类词，避免 lumen 这类品牌词抢位。"""
    tokens = needle.split()
    joined = "".join(tokens)
    if len(tokens) > 1 and _has_hits(joined):
        return joined
    cjk = "[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]"

    def rank(t: str) -> tuple:
        return (1 if re.search(cjk, t) else 0, len(t))

    ordered = sorted(tokens, key=rank, reverse=True)
    # 场合词（婚礼、年会）写在几乎每件商品的介绍里，先只认标题命中，
    # 否则「适合婚礼的衬衫」会被「婚礼」带走，搜出一堆鞋
    for token in ordered:
        if _title_hit(token):
            return token
    for token in ordered:
        if _has_hits(token):
            return token
    return ""


def budget_of(text: str) -> float | None:
    m = re.search(r"(?:预算|不超过|最多|控制在)\D{0,3}(\d+(?:\.\d+)?)", text) or re.search(
        r"(\d+(?:\.\d+)?)\s*(?:元|块|以内|以下|左右|上下)", text
    )
    return float(m.group(1)) if m else None


def _format_order(order: dict) -> str:
    logistics = (
        f"{order['carrier'] or '快递'} {order['tracking_no']}"
        if order["tracking_no"]
        else ("尚未出库，出库后会在订单详情写入运单号" if order["status"] == "paid" else "—")
    )
    lines = [
        f"订单 {order['order_no']}：{order['status_text']}。",
        f"下单时间 {str(order['placed_at'])[:16].replace('T', ' ')}，实付 {_yuan(order['paid_yuan'] or order['total_yuan'])}"
        + (f"，已退款 {_yuan(order['refunded_yuan'])}" if order["refunded_yuan"] else "")
        + "。",
        f"物流：{logistics}",
    ]
    if order["items"]:
        lines.append("\n".join(f"· {i['title']}{' / ' + i['variant'] if i['variant'] else ''} ×{i['qty']}" for i in order["items"]))
    if order["refunds"]:
        newest = order["refunds"][0]
        lines.append(f"退款申请：{newest.get('status_text') or newest['status']}，金额 {_yuan(newest['amount_yuan'])}，原因 {newest['reason']}")
    lines.append("点上面的订单号可直接打开订单详情，物流与退款进度都在那一页。")
    return "\n".join(lines)


def _search_lines(items: list[dict]) -> str:
    first = items[0]
    lines = [f"{first['title']} 目前售价 {_yuan(first['price_yuan'])}，{str(first['available']) + ' 件现货' if first['available'] else '暂时售罄'}。"]
    lines += [f"· {i['title']} {_yuan(i['price_yuan'])}（{i['available']} 件可售）" for i in items[1:]]
    return "\n".join(lines)


# 场合词要配「穿」相关的话才算穿搭咨询，否则「面试衬衫多少钱」这种该走商品检索。
# 别用「装」「服」这类单字：露营装备、客服都会误判成穿搭。
DRESSING_WORDS = ("穿", "搭", "衣", "裙", "鞋", "外套", "衬衫", "造型",
                  "服装", "套装", "着装", "装扮", "打扮", "穿搭")


def _outfit_question(q: str) -> dict | None:
    """认出穿搭咨询并解析条件；场合词只认商品自己标的适用场合，「相亲」「露营」这类说法由词表映射到最接近的场合并在回复里说明。"""
    if not any(w in q for w in DRESSING_WORDS):
        return None
    parsed = outfit.parse_question(q)
    return parsed if parsed.get("occasion") else None


def _outfit_reply(result: dict) -> str:
    if not result.get("found"):
        return result.get("message", "没能按这些条件凑出一套。")
    lines = [result.get("reason", ""), *result.get("reply_lines", [])]
    lines += [f"· {n}" for n in result.get("notes", [])]
    lines.append("点卡片可以看单品详情与实时库存；场合、身高、体型、预算任一项都能改，直接告诉我就行。")
    return "\n".join(line for line in lines if line)


def _visual_reply(result: dict) -> str:
    if not result.get("found"):
        return result.get("message", "这次没能按图片找到相似的商品。")
    lines = [f"{result['reason']}："]
    for item in result["items"]:
        lines.append(
            f"· {item['title']} {_yuan(item['price_yuan'])}，相似度 {item['similarity_pct']}%，"
            f"{item['available']} 件现货"
        )
    lines += [f"· {note}" for note in result.get("notes", [])]
    lines.append("要更准可以再说一句品类或预算，比如「衬衫」「500 以内」。")
    return "\n".join(lines)


def run(text: str, *, image_token: str = "") -> dict:
    """返回 {intent, reply, tool_calls, cards}，与在线图的输出结构一致。"""
    q = normalize(text)
    if not q and not image_token:
        return {"intent": "fallback", "reply": "请输入想咨询的问题", "tool_calls": [], "cards": []}

    calls: list[dict] = []
    cards: list[dict] = []

    def call(name: str, **kwargs) -> dict:
        started = time.time()
        result = tools.OFFLINE_HANDLERS[name](**kwargs)
        tools.record_metric(name, kwargs, result, started)
        new_cards = tools.card_rows(name, result)
        calls.append(
            {
                "tool": name,
                "args": {k: v for k, v in kwargs.items() if v not in (None, "")},
                "count": tools.result_count(result),
                # 与在线 _log 同一份 preview 口径：卡片对象，刷新后能还原成可点的商品卡
                "preview": [tools.brief(i) for i in tools.preview_rows(name, result)],
            }
        )
        for item in new_cards:
            if item not in cards:
                cards.append(item)
        return result

    ref = ORDER_NO.search(text or "")
    if image_token:
        # 传了图就先按图比，文字只用来收窄品类和价格
        hint = clean_needle(q).replace(" ", "")
        result = call(
            "find_similar_by_image",
            image_token=image_token,
            category=_category_slug(hint) if len(hint) >= 2 else "",
            max_price_yuan=budget_of(q),
            limit=5,
        )
        return {"intent": "visual_search", "reply": _visual_reply(result), "tool_calls": calls, "cards": cards}

    if ref:
        result = call("query_order", order_no=ref.group(0).upper())
        if not result.get("found"):
            return {"intent": "order_lookup", "reply": result.get("message", "没有查到这笔订单"), "tool_calls": calls, "cards": []}
        order = result["orders"][0]
        return {"intent": "order_lookup", "reply": _format_order(order), "tool_calls": calls, "cards": cards}

    if has(q, "我的订单", "买过", "购买记录", "订单记录", "最近买", "历史订单"):
        result = call("query_order", limit=3)
        if not result.get("found"):
            return {"intent": "my_orders", "reply": result.get("message", "这个账号还没有订单。要推荐商品可以告诉我预算和用途。"), "tool_calls": calls, "cards": []}
        return {
            "intent": "my_orders",
            "reply": "最近 %d 笔订单：\n%s\n%s" % (
                result["count"],
                "\n".join(f"· {o['order_no']} {o['status_text']}，{_yuan(o['total_yuan'])}，{str(o['placed_at'])[:10]}" for o in result["orders"]),
                "点任一订单号可打开详情看物流与售后。",
            ),
            "tool_calls": calls,
            "cards": cards,
        }

    faq_intent, faq_answer = _faq_answer(q)
    if faq_answer:
        return {"intent": faq_intent, "reply": faq_answer, "tool_calls": calls, "cards": []}

    if has(q, "你是谁", "机器人", "智能客服", "什么模型") or re.search(r"\bai\b", q):
        return {
            "intent": "identity",
            "reply": "我是小马，这个商城的智能助理。价格、库存、订单进度都从数据库实时查，配送与售后口径来自后台知识库。需要人工请把订单号和诉求发到 " + _support_email() + "。",
            "tool_calls": calls,
            "cards": [],
        }

    if re.match(r"^(你好|您好|嗨|在吗|在么|哈喽|hi|hello)", q):
        return {"intent": "greeting", "reply": "你好，我是小马。商品价格、库存、发货时效、订单进度、优惠怎么用都能问我，也可以让我按预算推荐。", "tool_calls": calls, "cards": []}

    if has(q, "谢谢", "感谢", "辛苦", "好的收到"):
        return {"intent": "thanks", "reply": "不客气，还有其他要查的吗？", "tool_calls": calls, "cards": []}

    if has(q, "物流", "快递", "配送", "运费", "包邮", "邮费", "几天", "多久", "能到", "送达", "发货"):
        result = call("shipping_options")
        return {"intent": "shipping", "reply": _plan_answer(result["subtotal_yuan"]), "tool_calls": calls, "cards": cards}

    if has(q, "退款", "退货", "退换", "售后", "取消订单", "不想要"):
        row = db.get("SELECT answer FROM support_faqs WHERE kind = 'refund' AND active = 1 ORDER BY sort LIMIT 1")
        return {
            "intent": "refund",
            "reply": row["answer"]
            if row
            else "售后走订单页的退款申请：未支付订单可直接取消并释放库存与优惠码；已支付订单在订单详情发起仅退款或退货退款，可申请部分商品，金额不超过实付减已退。",
            "tool_calls": calls,
            "cards": [],
        }

    if has(q, "优惠", "券", "满减", "活动", "打折", "促销", "折扣"):
        call("coupons_available")
        return {"intent": "promo", "reply": _promo_answer(), "tool_calls": calls, "cards": cards}

    if has(q, "支付", "付款", "余额", "钱包", "充值", "扣款", "支付宝", "微信", "银行卡"):
        return {
            "intent": "payment",
            "reply": "支持支付宝、微信支付、银行卡与钱包余额。演示站点不会真实扣款；订单创建后 30 分钟内未支付会自动关闭并释放库存；余额在「账户中心 - 钱包余额」里模拟充值，单次 10 到 5000 元。",
            "tool_calls": calls,
            "cards": [],
        }

    if has(q, "发票", "开票", "报销"):
        return {"intent": "invoice", "reply": "站内还没有自助开票入口。把订单号和开票信息发到 " + _support_email() + " 由人工处理，演示站点不真实开票。", "tool_calls": calls, "cards": []}

    if has(q, "人工", "投诉", "建议", "电话", "邮箱", "转人工"):
        return {"intent": "human", "reply": "把订单号和诉求发到 " + _support_email() + " 就能转人工，我这边先帮你查过的内容会留在会话记录里。", "tool_calls": calls, "cards": []}

    if has(q, "注册", "登录", "密码", "账号", "邮箱验证", "忘记密码"):
        return {"intent": "account", "reply": "右上角「登录」注册或登录，邮箱 + 至少 8 位含字母与数字的密码。账户中心里有我的订单、收货地址与钱包余额流水，待付款订单可自助取消，已付款可提交退款申请。游客下单不用登录，结算后请保存订单号。", "tool_calls": calls, "cards": []}

    if dress := _outfit_question(q):
        args = {**dress, "limit": 2}
        if budget := budget_of(q):
            args["budget_yuan"] = budget
        result = call("outfit_recommend", **args)
        return {"intent": "outfit", "reply": _outfit_reply(result), "tool_calls": calls, "cards": cards}

    if has(q, "推荐", "热卖", "爆款", "买什么", "值得买", "好物", "排行", "预算"):
        budget = budget_of(q)
        needle = clean_needle(q)
        keyword = pick_keyword(needle)
        slug = "" if keyword else _category_slug(needle.replace(" ", ""))
        args: dict = {"budget_yuan": budget} if budget else {}
        if keyword:
            args["keywords"] = keyword
        elif slug:
            args["category"] = slug
        result = call("recommend_products", **args)
        titles = "、".join(i["title"] for i in result["items"][:3]) or "暂时没有符合条件的商品"
        hint = "没查到合适的，可以换个品类词或放宽预算。" if not result["items"] else "要看某件的库存或详情，把名字告诉我就行。"
        return {"intent": "recommend", "reply": f"{result.get('reason', '')}：{titles}。{hint}", "tool_calls": calls, "cards": cards}

    if has(q, "库存", "有货", "现货", "还有吗", "卖完", "缺货", "补货"):
        name = clean_needle(q)
        result = call("check_inventory", product=name) if len(name) >= 2 else call("check_inventory")
        if not result.get("found"):
            return {"intent": "stock", "reply": result.get("message", "没找到这个商品"), "tool_calls": calls, "cards": []}
        if result.get("mode") == "low_stock":
            lines = "\n".join(f"· {i['product_title']} {i['title']}：剩 {i['available']} 件" for i in result["items"])
            return {"intent": "stock", "reply": "库存告急的规格：\n" + (lines or "暂时没有低库存商品"), "tool_calls": calls, "cards": []}
        variants = "\n".join(f"· {v['title']}（{v['sku']}）：可售 {v['available']} 件，{_yuan(v['price_yuan'])}" for v in result["variants"])
        return {"intent": "stock", "reply": f"{result['product_title']}：{result['verdict']}，共可售 {result['total_available']} 件。\n{variants}", "tool_calls": calls, "cards": cards}

    if has(q, "多少钱", "价格", "贵", "便宜", "怎么样", "介绍", "有没有", "有吗", "买") or catalog.suggest(q, limit=1):
        needle = clean_needle(q)
        keyword = pick_keyword(needle)
        slug = "" if keyword else _category_slug(needle.replace(" ", ""))
        max_yuan = budget_of(q)
        if len(keyword) >= 2:
            result = call("search_products", query=keyword, max_price_yuan=max_yuan, limit=3)
        elif slug:
            result = call("search_products", category=slug, max_price_yuan=max_yuan, limit=3)
        else:
            result = {"items": []}
        if not result.get("items"):
            detail = None
            guess = catalog.search(q=keyword or needle, per_page=1, in_stock=False) if len(keyword or needle) >= 2 else None
            if guess and guess["items"]:
                detail = call("product_detail", slug_or_id=guess["items"][0]["slug"])
            if detail and detail.get("found"):
                stock = sum(v["available"] for v in detail["variants"])
                return {
                    "intent": "product_price",
                    "reply": f"{detail['title']} 目前售价 {_yuan(detail['price_yuan'])}，{('共 ' + str(stock) + ' 件现货') if stock else '暂时售罄'}。{detail.get('description') or ''}",
                    "tool_calls": calls,
                    "cards": cards,
                }
            return {
                "intent": "product_not_found",
                "reply": f"没找到这个商品。可以说站内真有的品类词（{_category_hint()}），或者告诉我预算和用途，我来推荐。",
                "tool_calls": calls,
                "cards": [],
            }
        return {
            "intent": "product_price" if has(q, "多少钱", "价格", "贵", "便宜") else "product_search",
            "reply": _search_lines(result["items"]),
            "tool_calls": calls,
            "cards": cards,
        }

    needle = clean_needle(q)
    keyword = pick_keyword(needle) or (q if 2 <= len(q) <= 12 else "")
    if keyword:
        result = call("search_products", query=keyword, limit=3)
        if not result.get("items") and (slug := _category_slug(needle.replace(" ", ""))):
            result = call("search_products", category=slug, limit=3)
        if result.get("items"):
            return {"intent": "product_search", "reply": _search_lines(result["items"]), "tool_calls": calls, "cards": cards}

    return {
        "intent": "fallback",
        "reply": f"这个问题我暂时答不上来，站内也没搜到相关商品。可以说品类词（{_category_hint()}）、预算或用途，也可以把 LM 开头的订单号发给我查进度。",
        "tool_calls": calls,
        "cards": [],
    }
