"""多词问法：空格切开的每个词都要各自成立，标题里同时住下的那件要排在前面。

用户不会按我们的品类名说话：他会打「防水 手表」「静音 风扇」。改之前整句被当成一个连续串去比，
「防水 手表」只能命中「防水手表」四个连着的字，命中不了；更糟的是靠描述凑出来的
交换机也会被端上来 —— 这就是用户截图里那种「连查几次都是不对的东西」。
这里盯四件事：切段、段间 AND、段内同义词 OR、标题命中数排序。
"""

from __future__ import annotations

from app.services import catalog


def flat(text: str) -> str:
    return catalog.STOP.sub("", text).lower()


def title_hits(title: str, q: str) -> int:
    """标题里住下了几个词（同义词说法算同一个词）。"""
    joined = flat(title)
    return sum(
        1 for token in catalog.tokens(q) if any(flat(n) in joined for n in catalog.needles(token))
    )


def test_space_splits_the_query_into_separate_conditions():
    assert catalog.tokens("防水 手表") == ["防水", "手表"]
    assert catalog.tokens("  静音   风扇 ") == ["静音", "风扇"]
    assert catalog.tokens("") == []


def test_two_word_query_never_wider_than_either_word_alone():
    """AND 的语义：两词问法的结果数不能超过任何一个单词，也不能等于「整句连写」。"""
    both = catalog.search(q="户外 帐篷", per_page=1)["total"]
    assert both <= catalog.search(q="帐篷", per_page=1)["total"]
    assert both <= catalog.search(q="户外", per_page=1)["total"]


def test_every_hit_holds_both_words():
    """凑数不行：返回的每件商品，五个可比字段里必须真的同时有这两个词。"""
    got = catalog.search(q="防水 背包", per_page=12)
    assert got["items"], "防水背包这条问法一件都查不出来，就是还没补货"
    for item in got["items"]:
        haystack = flat(
            " ".join(
                str(item.get(key) or "")
                for key in ("title", "brand", "description", "tags", "attributes")
            )
        )
        for token in catalog.tokens("防水 背包"):
            assert any(flat(n) in haystack for n in catalog.needles(token)), (item["title"], token)


def test_each_token_gets_its_own_synonym_expansion():
    """段内 OR、段间 AND：「拖布」这段要能靠「拖把」成立，同时不许放过「手表」那段。"""
    spoken = catalog.search(q="手表 拖布", per_page=1)["total"]
    stocked = catalog.search(q="手表 拖把", per_page=1)["total"]
    assert spoken == stocked, "同义词只在一个词上生效，另一个词的条件不能丢"


def test_title_holding_more_words_comes_first():
    """排序口径：标题里词住得越多的越该在前面，哪怕它卖得没别人多。"""
    got = catalog.search(q="手机 支架", per_page=12)
    counts = [title_hits(i["title"], "手机 支架") for i in got["items"]]
    assert counts == sorted(counts, reverse=True), counts
    assert counts and counts[0] == len(catalog.tokens("手机 支架"))


def test_other_sorts_still_rank_by_title_within_their_own_order():
    """只有 relevant 才插标题权重；按价格排就不能被标题打乱。"""
    got = catalog.search(q="书包", sort="price_asc", per_page=12)
    prices = [i["price_cents"] for i in got["items"]]
    assert prices == sorted(prices)


def test_junk_segment_between_words_is_dropped_not_turned_into_empty_clause():
    """只由标点组成的那段不拼条件：SQL 里留个 `AND ()` 会直接报错。"""
    clean = catalog.search(q="儿童 牙膏", per_page=1)["total"]
    assert catalog.search(q="儿童 。， 牙膏", per_page=1)["total"] == clean


def test_count_and_pages_share_the_same_two_word_filter():
    total = catalog.search(q="旅行 收纳")["total"]
    assert total == catalog.search(q="旅行 收纳", per_page=1)["total"]
    assert total == catalog.search(q="旅行 收纳", per_page=60)["total"]
    first = catalog.search(q="旅行 收纳", page=1, per_page=5)["items"]
    second = catalog.search(q="旅行 收纳", page=2, per_page=5)["items"]
    assert {i["id"] for i in first}.isdisjoint({i["id"] for i in second})


def test_ranking_params_do_not_leak_into_the_count_query():
    """排序用的绑定参数要和筛选参数分开：混在一起时 COUNT 会收到没使用的参数直接报错。"""
    got = catalog.search(q="办公 椅", min_price=1, max_price=10**9, in_stock=True, sort="price_asc", per_page=6)
    assert got["total"] >= len(got["items"])
    assert got["total"] == catalog.search(q="办公 椅", min_price=1, max_price=10**9, in_stock=True)["total"]
