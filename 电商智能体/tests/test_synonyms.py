"""搜索同义词：口语说法要能搜到站内用词，且词表右半边必须真有货。

补货解决「站内没这东西」，同义词只解决「这东西我们换了个叫法」，
所以这里的用例盯两件事：说法能命中、命中的确实是那件货。
"""

from __future__ import annotations

import pytest

from app import db
from app.services import catalog, synonyms


def norm(text: str) -> str:
    return catalog.STOP.sub("", text).lower()


def test_original_word_stays_first_and_variants_are_unique():
    got = synonyms.expand("充电宝")
    assert got[0] == "充电宝", "用户原话必须排第一，命中不耗时也排在联想前面"
    assert "移动电源" in got
    assert len(got) == len(set(got))


def test_two_spoken_words_make_two_lookups_not_one_mangled_one():
    got = synonyms.expand("充电宝和手机膜")
    assert "充电宝和手机膜" in got
    assert "移动电源和手机膜" in got
    assert "充电宝和钢化膜" in got


@pytest.mark.parametrize("q", ["", "   ", "登山鞋"])
def test_queries_without_spoken_words_pass_through(q):
    assert synonyms.expand(q) == ([q.strip()] if q.strip() else [])


def test_needles_match_how_titles_are_stored():
    assert catalog.needles(" 充电宝 ") == ["充电宝", "移动电源"]
    assert catalog.needles("手机膜") == ["手机膜", "钢化膜"]
    assert catalog.needles("。。。") == []


def test_every_catalog_word_in_the_table_has_stock():
    """词表右半边要是缺货，同义词就是把人领到空货架前，得先补货。"""
    for catalog_word in sorted(set(synonyms.SYNONYMS.values())):
        assert catalog.search(q=catalog_word, per_page=1)["total"] > 0, f"同义词指向「{catalog_word}」，站内却没有现货"


def test_spoken_query_reaches_the_stocked_product():
    for spoken, catalog_word in synonyms.SYNONYMS.items():
        result = catalog.search(q=spoken, per_page=12)
        assert result["total"] > 0, f"搜「{spoken}」一件都没有，应该命中「{catalog_word}」"
        assert any(norm(catalog_word) in norm(i["title"]) for i in result["items"]), f"搜「{spoken}」没把「{catalog_word}」排出来"


def test_count_and_page_use_the_same_or_clause():
    """分页计数与列表各自拼一次 SQL，检索式必须一致，否则「共 N 件」和翻页对不上。"""
    total = catalog.search(q="充电宝")["total"]
    assert total == catalog.search(q="充电宝", per_page=1)["total"]
    assert total == catalog.search(q="充电宝", per_page=60)["total"]


def test_punctuation_only_query_is_treated_as_no_filter():
    """原话被标点吃光时不拼空条件：SQL 里留个 `AND ()` 会直接报错。"""
    assert catalog.search(q="。。")["total"] == catalog.search(q="")["total"]
    assert catalog.suggest("。。") == []


def test_suggest_and_api_agree_on_a_spoken_word(client):
    got = catalog.suggest("充电宝", limit=6)
    assert got, "联想里也该有移动电源"
    assert all("移动电源" in norm(g["title"]) or "充电宝" in norm(g["title"]) for g in got)
    res = client.get("/api/suggest", params={"q": "刮胡刀"})
    assert res.status_code == 200
    assert any("剃须刀" in norm(i["title"]) for i in res.json()["data"]), res.text
    assert catalog.suggest("。") == []


def test_shop_search_page_answers_a_spoken_word(client):
    res = client.get("/shop", params={"q": "尿不湿"})
    assert res.status_code == 200
    assert "纸尿裤" in res.text, "前台搜「尿不湿」的页面上一个纸尿裤商品都没有"


# —— 标题插字的说法：用户说「折叠桌」，站内那件叫「折叠露营桌」 ——

def test_gap_pattern_keeps_the_letter_order_and_inserts_wildcards():
    assert synonyms.gap_pattern("折叠桌") == "%折%叠%桌%"
    assert synonyms.gap_pattern("露营折叠桌") == "%露%营%折%叠%桌%"


@pytest.mark.parametrize("word", ["", "  ", "雨伞", "毛巾", "typec", "4g", "U盘"])
def test_short_pure_latin_and_empty_needles_are_never_split(word):
    """两个字不拆：「雨伞」拆成「雨」+「伞」会把雨具和伞具乱并；带字母数字的截出来是半截词。"""
    assert synonyms.gap_pattern(word) == ""


def test_a_word_broken_up_in_the_title_is_still_found():
    """这条就是用户截图那件事：库里明明有「折叠露营桌」，按连续串搜「折叠桌」却看不到它。"""
    items = catalog.search(q="折叠桌", per_page=20)["items"]
    assert any("折叠桌" not in i["title"] and "露营桌" in i["title"] for i in items), "插字那件没被捞出来"

    combo = catalog.search(q="露营 折叠桌")
    assert combo["total"] > 0, "「露营 折叠桌」两个条件都住在同一件货上，不该搜空"
    assert all("露营" in i["title"] and "折" in i["title"] for i in combo["items"][:5])


def test_gap_hits_never_outrank_exact_ones(monkeypatch):
    """宽松只能用来兜底，不能用来排座次：搜「台式机」时「台式饮水机」得在后面。"""
    real = synonyms.gap_pattern
    monkeypatch.setattr(synonyms, "gap_pattern", lambda needle: "")
    strict = catalog.search(q="台式机", per_page=6)
    monkeypatch.setattr(synonyms, "gap_pattern", real)
    loose = catalog.search(q="台式机", per_page=6)
    assert strict["total"] > 0 and loose["total"] > strict["total"], "这条用例要靠插字真的多捞出货才有意义"
    assert all("台式机" in i["title"] for i in loose["items"][: strict["total"]]), "整串命中的那几件必须排在前面"
    assert any("台式机" not in i["title"] for i in loose["items"]), "插字捞出来的货排在后面，不是挤掉正主"


@pytest.mark.parametrize("word", [
    "折叠桌", "折叠椅", "电脑包", "双肩包", "不粘锅", "仰卧板", "速干裤",
    "针织衫", "落地灯", "感应灯", "儿童座椅", "婴儿车", "沙滩玩具", "婴儿辅食",
])
def test_gap_words_land_on_a_product_that_really_is_that_thing(word):
    """每个插字说法都得真到货，且第一屏那件货的标题确实住得下这几个字（顺序不变、允许插字）。"""
    result = catalog.search(q=word, per_page=6)
    assert result["total"] >= 3, f"搜「{word}」只剩 {result['total']} 件，兜底等于没接住"
    joined = catalog.STOP.sub("", result["items"][0]["title"]).lower()
    assert synonyms.gap_pattern(word) == "" or all(ch in joined for ch in word), f"「{word}」的头名对不上：{joined}"


def test_gap_query_counts_and_pages_agree():
    """计数与列表各拼一次 SQL：插字条件只进一边，「共 N 件」就会与翻页对不上。"""
    total = catalog.search(q="露营 折叠桌")["total"]
    assert total == catalog.search(q="露营 折叠桌", per_page=1)["total"]
    assert total == catalog.search(q="露营 折叠桌", per_page=60)["total"]


def test_front_page_search_box_shows_the_broken_up_word(client):
    res = client.get("/shop", params={"q": "露营 折叠桌"})
    assert res.status_code == 200
    assert "折叠露营桌" in res.text, "前台搜索框搜「露营 折叠桌」，页面上那件「折叠露营桌」得露出来"
