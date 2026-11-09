"""需求说法层：用户不说品类、只说「我是谁、要干什么」时也要查得到货。

「露营装备」「猫咪用品」「退休礼物」这类说法里，「装备/用品/礼物」只是口语的框，
站内没有一件商品把自己写成「装备」。检索要先剥框再走同义词，同时不能把「办公 椅」
这种真条件一起丢掉。
"""

from __future__ import annotations

from app import db
from app.services import catalog, synonyms


def flat(text: str) -> str:
    return text.replace(" ", "").lower()


def test_generic_frame_is_stripped_before_matching():
    assert catalog.needles("露营装备") == ["露营装备", "露营"]
    assert "退休" in catalog.needles("退休礼物")
    assert "猫咪" in catalog.needles("猫咪用品")


def test_stripping_stops_before_the_word_becomes_too_short():
    """「用品」本身没有可查的东西，剥成空串不如留着原话。"""
    assert catalog.without_generic("用品") == ""
    assert catalog.needles("用品") == ["用品"]


def test_a_real_condition_between_two_words_is_not_thrown_away():
    """办公 椅 的两段都是真条件：丢了「办公」就等于把全站 98 件椅子端给用户。"""
    both = catalog.search(q="办公 椅", per_page=1)["total"]
    assert both < catalog.search(q="椅", per_page=1)["total"]
    assert catalog.search(q="办公 椅", per_page=60)["items"][0]["title"].count("办公") == 1
    assert "椅" in catalog.search(q="办公 椅", per_page=1)["items"][0]["title"]


def test_contentless_segment_drops_out_of_a_multi_word_query():
    """「露营 装备」里的「装备」什么都不指向，留着它 AND 下去整句就 0 件。"""
    assert catalog.search(q="露营 装备", per_page=1)["total"] > 0
    assert catalog.search(q="露营 装备", per_page=1)["total"] == catalog.search(q="露营", per_page=1)["total"]


def test_scene_phrases_reach_real_stock():
    for phrase in ["露营装备", "出差装备", "猫咪用品", "狗狗外出", "退休礼物", "乔迁礼物", "开学文具",
                   "阳台种菜", "搬家打包", "待产包", "产后修复", "孕期营养", "新生儿用品", "婴儿辅食"]:
        got = catalog.search(q=phrase, per_page=1)
        assert got["total"] > 0, f"搜「{phrase}」一件都没有"
        top = got["items"][0]
        haystack = flat(top["title"]) + flat(str(top.get("tags") or "")) + flat(str(top.get("attributes") or ""))
        assert any(flat(n) in haystack for n in catalog.needles(phrase)), f"搜「{phrase}」第一件不对题：{top['title']}"


def test_new_need_words_point_at_stock_not_at_an_empty_shelf():
    """词表右半边必须真有货：把「坐月子」绕到一件没有的「产妇」上，等于答非所问。"""
    for spoken in ["坐月子", "产假", "月经期", "租房必备", "宠物生病"]:
        target = synonyms.SYNONYMS[spoken]
        assert catalog.search(q=target, per_page=1)["total"] > 0, f"「{spoken}」指向「{target}」，站内却是空的"


def test_maternal_category_is_on_the_shelf_and_in_the_nav():
    row = db.get("SELECT id, name FROM categories WHERE slug = 'maternal_care'")
    assert row and row["name"] == "孕产护理"
    count = db.value("SELECT COUNT(*) FROM products p JOIN categories c ON c.id = p.category_id "
                     "WHERE c.slug = 'maternal_care' AND p.status = 'active'")
    assert int(count) > 0


def test_recommend_by_occasion_does_not_fall_back_to_best_sellers():
    """小马只拿到「露营」这个场合时，不能回一句全站热卖：卡片会全是毛巾与电工胶带。"""
    from app.agent import shop_api

    got = shop_api.recommend_products(occasion="露营", limit=4)
    assert got["strategy"] == "条件筛选", got["strategy"]
    for item in got["items"]:
        assert "露营" in item["title"], item["title"]


def test_shop_page_answers_a_need_phrase(client):
    res = client.get("/shop", params={"q": "露营装备"})
    assert res.status_code == 200
    assert "露营" in res.text
