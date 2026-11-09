"""场合穿搭：引擎取值、小马第 9 个工具与 /outfit 页。

用例只断言「页面上写的数在数据库里查得到」，不锁死具体商品，避免补数据就红。
"""

from __future__ import annotations

import pytest

from app import db
from app.agent import offline, shop_api, store, tools
from app.services import outfit


def test_vocabulary_lists_only_values_with_stock():
    vocab = outfit.vocabulary()
    assert vocab["场合"], "场合词表不能空，否则筛选页没得选"
    for name, count in vocab["场合"]:
        assert name in outfit.OCCASIONS
        assert count > 0
        real = db.value(
            """SELECT COUNT(*) FROM products p
               JOIN (SELECT product_id, SUM(stock_qty - reserved_qty) AS available
                     FROM variants WHERE status = 'active' GROUP BY product_id) price ON price.product_id = p.id
               WHERE p.status = 'active' AND price.available > 0
                 AND JSON_CONTAINS(IFNULL(JSON_EXTRACT(p.attributes, '$."适用场合"'), '[]'), JSON_QUOTE(:o))""",
            {"o": name},
        )
        assert real >= count, f"「{name}」标了 {count} 款现货，实际查不到这么多"


def test_every_advertised_occasion_has_inventory():
    """词表里写给用户看的场合，必须真能凑出东西，不能只在提示里挂着。"""
    listed = {name for name, _ in outfit.vocabulary()["场合"]}
    assert listed == set(outfit.OCCASIONS), f"这些场合在小马提示里可选却没现货：{set(outfit.OCCASIONS) - listed}"


def test_occasion_synonyms_map_to_stocked_words():
    assert outfit.normalize_occasion("婚礼") == ("婚礼", "婚礼")
    assert outfit.normalize_occasion("第一次相亲")[0] == "约会"
    assert outfit.normalize_occasion("周末去露营")[0] == "度假"
    assert outfit.normalize_occasion("当伴娘穿什么")[0] == "婚礼"
    assert outfit.normalize_occasion("火星移民")[0] == ""


def test_personality_maps_to_styles_without_guessing():
    assert outfit.styles_of(personality="内向") == ["简约", "温柔"]
    assert outfit.styles_of(personality="我说不清") == []
    assert outfit.styles_of(style="浮夸") == []


@pytest.mark.parametrize("occasion", ["婚礼", "面试", "年会", "度假"])
def test_recommend_returns_buyable_pieces(occasion):
    result = outfit.recommend(occasion=occasion, limit=2)
    assert result["found"], result.get("message")
    assert result["looks"], "found 为真时必须给出成套单品"
    for look in result["looks"]:
        assert look["items"]
        for piece in look["items"]:
            assert piece["price_cents"] > 0
            assert piece["url"].startswith("/product/")
            assert piece["image"] is None or piece["image"].startswith("/img/products/"), "只挂实物图，配不到就留空"
            assert piece["role"] in ("主件", "外套", "内搭", "下装", "鞋履", "包袋", "配饰")
            tagged = db.value(
                "SELECT JSON_EXTRACT(attributes, '$.\"适用场合\"') FROM products WHERE slug = :s",
                {"s": piece["slug"]},
            ) or ""
            assert occasion in tagged, f"{piece['title']} 没标「{occasion}」却被推荐了"


def test_recommend_respects_single_piece_budget():
    result = outfit.recommend(occasion="婚礼", budget_yuan=800, limit=2)
    assert result["found"], result.get("message")
    assert "单件 800 元以内" in result["reason"]
    for piece in result["items"]:
        assert piece["price_cents"] <= 80000


def test_recommend_never_gives_a_one_piece_look():
    result = outfit.recommend(occasion="派对", body_type="高挑", height_cm=150, style="甜美", limit=2)
    if not result["found"]:
        assert "没能凑出一套" in result["message"] or "没有" in result["message"]
    else:
        assert result["count"] >= 2


def test_recommend_rejects_unknown_occasion():
    result = outfit.recommend(occasion="火星移民")
    assert not result["found"]
    assert "没认出具体场合" in result["message"]


def test_outfit_tool_is_registered_and_labelled():
    assert "outfit_recommend" in [t.name for t in tools.ALL_TOOLS]
    assert "outfit_recommend" in [t.name for t in tools.CORE_TOOLS]
    assert store.tool_text("outfit_recommend") == "场合穿搭"
    assert store.INTENT_TEXT["outfit"] == "场合穿搭"


def test_outfit_tool_counts_match_result_count():
    data = shop_api.outfit_recommend(occasion="婚礼", limit=2)
    assert data["found"]
    assert data["count"] == len(data["items"]) == tools.result_count(data)
    assert data["reply_lines"], "工具要给小马逐套的可读文本"


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("我要参加朋友的婚礼有什么服装推荐", "婚礼"),
        ("年会穿搭，我 160cm 偏瘦", "年会"),
        ("面试穿什么比较好", "面试"),
    ],
)
def test_offline_routes_dress_questions_to_outfit(text, needle):
    result = offline.run(text)
    assert result["intent"] == "outfit"
    assert needle in result["reply"]
    assert result["tool_calls"][0]["tool"] == "outfit_recommend"
    assert result["cards"], "穿搭问答要带单品卡"


def test_outfit_cards_carry_their_slot_for_the_frontend():
    """卡片上要标得出「主件/鞋履」，还要能跳详情页；落库的 preview 用同一批对象，刷新才回放得出来。"""
    result = offline.run("我要参加朋友的婚礼有什么服装推荐")
    cards = result["cards"]
    roles = [c.get("role") for c in cards]
    assert all(roles), f"每件穿搭卡都要带穿搭位：{roles}"
    assert {"主件", "鞋履"} <= set(roles), f"一套里主件和鞋要认得出来：{roles}"
    assert all(c["url"].startswith("/product/") and c["image"] for c in cards)

    preview = result["tool_calls"][0]["preview"]
    assert [p["url"] for p in preview] == [c["url"] for c in cards][: len(preview)], "preview 与卡片要同源，别一个有卡一个没卡"
    assert all(set(p) <= set(tools.CARD_KEYS) for p in preview), "落库只留卡片字段，别把整包结果写进 JSON 列"
    assert all(p.get("role") for p in preview), "穿搭位要跟着落库，回放时角标才不会丢"


def test_offline_outfit_explains_synonym():
    result = offline.run("相亲穿什么")
    assert result["intent"] == "outfit"
    assert "站内按" in result["reply"], "映射到别的场合时要照实说明"


def test_outfit_page_renders_form_and_results(client):
    page = client.get("/outfit")
    assert page.status_code == 200
    assert "凑一套" in page.text
    assert page.text.count('class="outfit-field"') >= 7

    hit = client.get("/outfit", params={"occasion": "婚礼", "height": "162", "body": "小个子"})
    assert hit.status_code == 200
    assert "look-piece" in hit.text


def test_outfit_page_tolerates_blank_number_fields(client):
    """表单一定带 height= 与 budget= 的空值，FastAPI 直接声明成 int 会 422。"""
    res = client.get("/outfit", params={"occasion": "婚礼", "height": "", "budget": "", "line": ""})
    assert res.status_code == 200
    assert "凑一套" in res.text


def test_outfit_page_reports_no_match_honestly(client):
    res = client.get("/outfit", params={"occasion": "火星移民"})
    assert res.status_code == 200
    assert "outfit-empty" in res.text


def test_product_page_links_into_outfit(client):
    slug = db.value(
        """SELECT p.slug FROM products p
           WHERE p.status = 'active' AND JSON_EXTRACT(p.attributes, '$."适用场合"') IS NOT NULL LIMIT 1"""
    )
    text = client.get(f"/product/{slug}").text
    assert "intro-scene-link" in text
    assert "/outfit?occasion=" in text


def test_outfit_nav_entry_exists(client):
    text = client.get("/").text
    assert text.count('href="/outfit"') >= 2, "桌面与移动导航都要有穿搭入口"


@pytest.mark.parametrize(
    ("text", "height"),
    [
        ("婚礼穿搭 身高185体重160预算1000", 185),
        ("我 160 斤，年会穿什么", None),
        ("体重62kg 面试穿搭", None),
        ("预算1500，婚礼穿什么", None),
        ("175cm 相亲穿什么", 175),
    ],
)
def test_height_parser_skips_weight_and_budget(text, height):
    """体重与预算里的数字很容易被当成身高：身高按 160 筛，185 的人就永远配不到合身的衣服。"""
    assert outfit._height_of(text) == height


def test_offline_outfit_does_not_read_weight_as_height():
    result = offline.run("婚礼男装穿搭，身高185体重160，预算1000")
    assert result["intent"] == "outfit"
    assert result["tool_calls"][0]["args"]["height_cm"] == 185
    assert "160cm" not in result["reply"]


@pytest.mark.parametrize("line", ["男装", "女装"])
def test_wedding_fills_every_role_the_main_piece_advertises(line):
    """主件的「搭配位」列了几项，站内就得每项都有能配的现货，否则小马只能说这一位没有货。"""
    pool = outfit._outfit_products("婚礼", None, line)
    assert pool
    roles = {role for p in pool if outfit._attrs(p).get("穿搭位") == "主件"
             for role in (outfit._attrs(p).get("搭配位") or [])}
    assert roles, f"{line}的婚礼主件都没写搭配位"
    criteria = {"formality": outfit.OCCASION_FORMALITY["婚礼"], "styles": [], "body_type": "",
                "height": None, "season": "", "line": line}
    for role in sorted(roles):
        assert outfit._pick(pool, role, used=set(), criteria=criteria), f"婚礼{line}缺「{role}」的现货"


def test_tall_menswear_wedding_look_is_complete():
    result = outfit.recommend(occasion="婚礼", line="男装", height_cm=185, body_type="标准", budget_yuan=1000)
    assert result["found"], result.get("message")
    look = result["looks"][0]
    assert not look["missing"], f"这套还缺 {look['missing']}"
    assert {i["role"] for i in look["items"]} >= {"主件", "内搭", "鞋履"}
    assert all(i["price_yuan"] <= 1000 for i in look["items"]), "预算按每件单品筛，单件不该顶破"
    assert "每件单品筛" in "".join(look["notes"]), "合计超过预算时要照实说明口径"


def test_budget_pulls_the_look_down():
    cheap = outfit.recommend(occasion="婚礼", line="男装", height_cm=185, body_type="标准", budget_yuan=1000)
    loose = outfit.recommend(occasion="婚礼", line="男装", height_cm=185, body_type="标准")
    assert cheap["looks"][0]["total_yuan"] < loose["looks"][0]["total_yuan"], "给了预算就该在合格品里偏向便宜的"


def test_accessory_only_needs_the_occasion_not_the_style():
    """手拿包写的是「正式、个性」，用户说温柔风也得能配上包，否则一套永远缺一个包。"""
    pool = outfit._outfit_products("婚礼", None, "女装")
    criteria = {"formality": outfit.OCCASION_FORMALITY["婚礼"], "styles": ["温柔"], "body_type": "",
                "height": None, "season": "", "line": "女装"}
    picked = outfit._pick(pool, "包袋", used=set(), criteria=criteria)
    assert picked, "温柔风配不出包袋"
    bottom = outfit._pick(pool, "下装", used=set(), criteria=criteria)
    if bottom:
        assert "温柔" in (outfit._attrs(bottom[0]).get("风格") or []), "穿在身上的单品仍要按风格淘汰"


@pytest.mark.parametrize("name", [
    "男士正装衬衫", "三粒扣西装", "西装马甲", "男士西裤", "便士乐福鞋", "商务公文包", "真丝口袋巾",
    "粗花呢短外套", "收腰小礼服裙", "缎面内搭吊带", "链条小包", "玛丽珍中跟鞋", "羊毛大衣",
])
def test_new_occasion_names_have_sellable_stock(name):
    """seed_catalog 加了新品名就要真灌进库：品名只在词表里、库里没货，推荐照样配不出这一件。"""
    count = db.value(
        """SELECT COUNT(*) FROM products p
           JOIN (SELECT product_id, SUM(stock_qty - reserved_qty) AS available
                 FROM variants WHERE status = 'active' GROUP BY product_id) price ON price.product_id = p.id
           JOIN categories c ON c.id = p.category_id
           WHERE p.status = 'active' AND price.available > 0 AND c.slug = 'occasion'
             AND JSON_UNQUOTE(JSON_EXTRACT(p.attributes, '$."品类"')) = :n""",
        {"n": name},
    )
    assert count >= 5, f"「{name}」只有 {count} 件现货，凑两套都会撞款"
