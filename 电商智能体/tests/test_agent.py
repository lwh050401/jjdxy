"""小马：意图路由、工具口径与中文标签。

真 DeepSeek 要 Key、答案每次不一样，接口测试仍走站内离线路由与十个工具本身；
在线图用假 LLM 按脚本回答，只核对消息序列、系统提示体积和重复工具调用这些确定性行为。
"""

from __future__ import annotations

import asyncio
import contextvars
import pathlib

import httpx
import pytest

from app import db
from app.agent import offline, shop_api, store, tools
from app.main import create_app

CONTACT = {"email": "agent@example.com", "phone": "13800000000"}
ADDRESS = {
    "receiver": "接口测试",
    "phone": "13800000000",
    "province": "浙江省",
    "city": "杭州市",
    "district": "西湖区",
    "detail": "文三路 1 号",
}


@pytest.fixture(autouse=True)
def _no_guest_thread_leftovers():
    """游客提问会建会话，跑完只删本次新增的，不动演示库已有的记录。"""
    before = {r["id"] for r in db.all_("SELECT id FROM agent_threads WHERE user_id IS NULL", {})}
    yield
    fresh = [r["id"] for r in db.all_("SELECT id FROM agent_threads WHERE user_id IS NULL", {}) if r["id"] not in before]
    for tid in fresh:
        db.run("DELETE FROM agent_threads WHERE id = :tid", {"tid": tid})


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("你好", "greeting"),
        ("你是谁，是机器人吗", "identity"),
        ("最近有什么优惠", "promo"),
        ("几天能到货", "shipping"),
        ("谢谢啦", "thanks"),
        ("帮我推荐 500 以内的露营装备", "recommend"),
        ("露营桌还有货吗", "stock"),
        ("Lumen Air 降噪耳机 Pro 怎么样", "product_search"),
        ("Lumen Air 降噪耳机 Pro 多少钱", "product_price"),
        ("我要转人工", "human"),
        ("LM202610ABCDEF 到哪了", "order_lookup"),
        ("zxcvbnm 这是什么意思", "fallback"),
    ],
)
def test_offline_routes_intent(text, intent):
    assert offline.run(text)["intent"] == intent


def test_offline_empty_input():
    result = offline.run("   ")
    assert result["intent"] == "fallback"
    assert result["tool_calls"] == [] and result["cards"] == []


def test_budget_and_keyword_extraction():
    assert offline.budget_of("预算 300 以内") == 300
    assert offline.budget_of("不超过 1299 元") == 1299
    assert offline.budget_of("随便看看") is None
    assert "耳机" in offline.clean_needle("我想看看降噪耳机有没有货")
    assert "耳机" in offline.pick_keyword("降噪耳机")


def test_tool_labels_are_chinese():
    for tool in tools.ALL_TOOLS:
        assert store.tool_text(tool.name) in store.TOOL_TEXT.values()
    assert store.tool_text("recommend_products") == "商品推荐"
    assert store.tool_text("unknown_tool") == "unknown_tool"


def test_offline_records_tool_args_and_preview():
    result = offline.run("露营桌还有货吗")
    assert result["tool_calls"], "库存问题必须真实调用工具"
    for call in result["tool_calls"]:
        assert call["tool"] in store.TOOL_TEXT
        assert call["args"], "工具调用要留下参数"
        assert isinstance(call["preview"], list)


def test_recommend_cards_use_cents():
    result = offline.run("帮我推荐 500 以内的露营装备")
    assert result["cards"], "有货的情况下推荐要给卡片"
    for card in result["cards"]:
        assert int(card["price_cents"]) > 0
        assert card["slug"]
        assert card["image"] is None or card["image"].startswith("/img/products/"), "卡片只挂实物图，配不到就留空让前台显示暂无实物图"
        assert card["url"] == f"/product/{card['slug']}", "recommend.* 的行只有 slug，卡片要补成能点的链接"


def test_search_products_tool():
    data = shop_api.search_products(query="耳机", in_stock_only=True, limit=3)
    assert data["count"] <= 3
    for item in data["items"]:
        assert item["price_yuan"] > 0 and item["url"].startswith("/product/")


def test_inventory_tool_matches_service():
    from app.services import inventory

    found = shop_api.search_products(query="桌", limit=1)
    assert found["items"], "库里应有桌类商品"
    data = shop_api.check_inventory(product=found["items"][0]["title"])
    assert data["found"] and data["mode"] == "product"
    pid = int(db.value("SELECT id FROM products WHERE slug = :s", {"s": data["slug"]}))
    assert data["total_available"] == inventory.stock_of(pid)["available"]


def test_shipping_tool_lists_plans():
    data = shop_api.shipping_options(subtotal_yuan=0)
    assert data["plans"] and all(p["name"] for p in data["plans"])
    assert data["free_shipping_threshold_yuan"] >= 0


def test_faq_tool_only_returns_station_facts():
    for item in shop_api.faq_hits()["faqs"]:
        assert item["question"] and item["answer"]


def test_fallback_lists_real_categories():
    result = offline.run("zxcvbnm 这是什么意思")
    assert result["intent"] == "fallback"
    assert offline._category_hint() in result["reply"]


def test_bare_category_word_searches_catalog():
    row = db.get(
        """SELECT c.name FROM categories c JOIN products p ON p.category_id = c.id
           WHERE p.status = 'active' GROUP BY c.id HAVING COUNT(*) > 3 ORDER BY COUNT(*) DESC LIMIT 1"""
    )
    result = offline.run(row["name"])
    assert result["intent"] == "product_search"
    assert result["cards"] and result["tool_calls"][0]["tool"] == "search_products"


def test_bare_category_word_from_tags():
    tags = db.value("SELECT tags FROM products WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    word = tags[0]
    result = offline.run(word)
    assert result["intent"] in ("product_search", "product_price")
    assert result["cards"], f"品类词「{word}」应给出商品卡"


def _place_order(client, variant) -> str:
    """给买家下一单，返回订单号，供订单类问答复用。"""
    assert client.post("/api/cart/items", json={"variant_id": variant["id"], "qty": 1}).status_code == 200
    placed = client.post("/api/checkout/place", json={"contact": CONTACT, "address": ADDRESS, "coupon_code": ""})
    assert placed.status_code == 200, placed.text
    return placed.json()["data"]["order_no"]


def _anonymous():
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30)


def test_order_lookup_reply_points_to_detail_page(client, buyer, variant):
    order_no = _place_order(client, variant)
    data = client.post("/api/agent/messages", json={"message": f"{order_no} 到哪了"}).json()["data"]
    assert data["intent"] == "order_lookup"
    assert order_no in data["reply"]
    assert "订单详情" in data["reply"], "要告诉用户能点进详情页"
    assert data["tool_calls"][0]["count"] == 1, "工具标签不能说查到了却报 0"
    assert client.get(f"/order/{order_no}").status_code == 200


def test_my_orders_lists_real_order_numbers(client, buyer, variant):
    order_no = _place_order(client, variant)
    data = client.post("/api/agent/messages", json={"message": "我的订单"}).json()["data"]
    assert data["intent"] == "my_orders"
    assert order_no in data["reply"]
    assert client.get(f"/order/{order_no}").status_code == 200


def test_agent_hides_others_order_from_guest(client, buyer, variant):
    order_no = _place_order(client, variant)

    async def probe():
        async with _anonymous() as anon:
            res = await anon.post("/api/agent/messages", json={"message": f"{order_no} 到哪了"})
            return res.json()["data"]

    data = asyncio.new_event_loop().run_until_complete(probe())
    assert data["intent"] == "order_lookup"
    assert data["reply"].startswith("没有查到订单号"), data["reply"]
    assert "别人的订单" in data["reply"]
    assert data["cards"] == []


def test_chat_frontend_turns_order_numbers_into_links():
    js = (pathlib.Path(__file__).resolve().parents[1] / "static" / "js" / "site.js").read_text(encoding="utf-8")
    assert "linkifyOrders" in js
    assert "/order/" in js


def test_chat_frontend_restores_history_and_outfit_slots():
    root = pathlib.Path(__file__).resolve().parents[1]
    js = (root / "static" / "js" / "site.js").read_text(encoding="utf-8")
    css = (root / "static" / "css" / "site.css").read_text(encoding="utf-8")
    assert "/api/agent/history" in js, "刷新后要拿历史把上一轮的商品卡补回聊天窗"
    assert "chat-role" in js and ".chat-role" in css, "穿搭卡上的穿搭位要有样式，不能只写在文字清单里"
    assert "<b>$1</b>" in js, "模型吐的 markdown **强调** 要变成粗体，不能把星号留在气泡里"


def test_agent_chat_endpoint_roundtrip(client, admin_client, buyer):
    from app.settings import get_settings

    first = client.post("/api/agent/messages", json={"message": "露营桌还有货吗"})
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["thread_id"]
    assert data["intent"] == "stock"
    assert data["model"] == ("offline-router" if not get_settings().agent_online else data["model"])
    for call in data["tool_calls"]:
        assert call["label"] == store.tool_text(call["tool"])

    again = client.post("/api/agent/messages", json={"message": "那帮我推荐 500 以内的露营装备", "thread_id": data["thread_id"]})
    second = again.json()["data"]
    assert second["thread_id"] == data["thread_id"]

    history = client.get("/api/agent/history", params={"thread_id": data["thread_id"]}).json()["data"]
    roles = [m["role"] for m in history["messages"]]
    assert roles.count("user") == 2 and roles.count("assistant") == 2
    assert history["cards"]

    rows = db.all_("SELECT tool FROM agent_tool_calls WHERE thread_id = :t", {"t": data["thread_id"]})
    assert rows and all(r["tool"] in store.TOOL_TEXT for r in rows)

    # 会话记录只在后台进程里，前台买家即使猜到地址也打不开
    anon_admin = admin_client.get(f"/api/admin/agent/threads/{data['thread_id']}")
    assert anon_admin.status_code == 403


def test_history_replays_clickable_cards(client):
    """刷新页面时聊天窗靠 /api/agent/history 把上一轮的卡片补回来，
    卡片必须是带 url 的对象 —— 存成标题字符串的话前端只会渲染出点不开的空卡。"""
    data = client.post("/api/agent/messages", json={"message": "露营桌还有货吗"}).json()["data"]
    history = client.get("/api/agent/history", params={"thread_id": data["thread_id"]}).json()["data"]
    assistant = [m for m in history["messages"] if m["role"] == "assistant"][-1]

    assert assistant["cards"], "回放要能重新给出上一轮的商品卡"
    for card in assistant["cards"]:
        assert isinstance(card, dict) and card["url"].startswith("/product/")
        assert card["title"] and card["image"], f"卡片少了图和标题：{card}"
    assert assistant["tool_calls"][0]["label"] == store.tool_text(assistant["tool_calls"][0]["tool"])
    assert history["cards"] == assistant["cards"], "整会话的卡片口径要和最近那条回复一致"
    assert client.get(assistant["cards"][0]["url"]).status_code == 200, "卡片链接要真能打开"


def test_agent_state_and_rejections(client):
    state = client.get("/api/agent/state").json()["data"]
    assert sorted(state["tools"]) == sorted(t.name for t in tools.ALL_TOOLS) == sorted(store.TOOL_TEXT)
    assert len(state["tools"]) == 10, "小马的工具数量变了要同步改后台展示与文档"
    assert "outfit_recommend" in state["tools"]
    assert "find_similar_by_image" in state["tools"]
    assert state["upload"]["max_bytes"] > 0 and "image/png" in state["upload"]["accept"]

    empty = client.post("/api/agent/messages", json={"message": "   "})
    assert empty.status_code == 400

    long = client.post("/api/agent/messages", json={"message": "字" * 900})
    assert long.status_code == 200
    assert long.json()["data"]["reply"]

    history = client.get("/api/agent/history")
    assert history.json()["data"]["messages"] == []


def test_rate_limiter_blocks_burst():
    from app.api import agent_api

    key = "pytest-burst"
    agent_api._hits.pop(key, None)
    assert [agent_api._limited(key) for _ in range(21)].count(True) == 1
    agent_api._hits.pop(key, None)


class _FakeBound:
    """替 DeepSeek 记下每次 think 收到的消息，用来核对提示词的实际体积。"""

    def __init__(self, sink: list):
        self.sink = sink

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        self.sink.append(list(messages))
        return AIMessage(content="帮你查到了。")


class _FakeLLM:
    def __init__(self):
        self.seen: list = []

    def bind_tools(self, _tools):
        return _FakeBound(self.seen)


def test_online_think_sends_one_system_message_and_no_tool_list():
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.agent import graph

    fake = _FakeLLM()
    chain = graph.build_online_graph(fake)
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在的，有什么可以帮你？"}]
    state = chain.invoke(
        {
            "question": "露营桌还有货吗",
            "thread_id": "pytest-no-thread",
            "history": history,
            "image_token": "",
            "messages": graph._messages({"history": history, "question": "露营桌还有货吗", "image_token": ""}),
            "hops": 0,
        }
    )

    assert len(fake.seen) == 1, "没让调工具就该一跳结束，别多跑一轮"
    msgs = fake.seen[0]
    systems = [m for m in msgs if isinstance(m, SystemMessage)]
    assert len(systems) == 1 and msgs[0] is systems[0], "bind_tools 已带工具 schema，只留开头一条 system"
    assert not any("可用工具" in str(m.content) for m in msgs), "不能再往尾部拼一条工具清单白烧 token"
    assert isinstance(msgs[-1], HumanMessage) and "露营桌" in msgs[-1].content
    assert history[0]["content"] in [m.content for m in msgs if isinstance(m, HumanMessage)]
    assert state["reply"] == "帮你查到了。" and state["intent"] == "llm"


def test_identical_tool_call_in_one_turn_is_answered_from_cache():
    """本轮缓存挂在 ContextVar 上。真实请求各跑在自己的 asyncio context 里互不干扰，
    测试直接调工具会把这个线程指针漏给后面的用例，所以放进独立 context 跑完就丢。"""

    def scenario():
        tid = store.ensure_thread(None)["id"]
        cards, calls = tools.reset_turn()
        tools.current_thread.set(tid)
        args = {"query": "耳机", "limit": 2}

        first = tools.search_products.invoke(args)
        again = tools.search_products.invoke(args)
        other = tools.search_products.invoke({**args, "limit": 3})
        rows = db.all_("SELECT tool FROM agent_tool_calls WHERE thread_id = :t", {"t": tid})
        return rows, calls, cards, first, again, other

    rows, calls, cards, first, again, other = contextvars.Context().run(scenario)

    assert len(rows) == 2 and len(calls) == 2, "完全相同条件的第二次调用不该再记一笔"
    assert "同一份结果" in again.get("note", ""), first
    assert first.get("note") is None and again["items"] == first["items"]
    assert other.get("note") is None, "换个 limit 就是另一次查询，不能被缓存挡住"
    assert len(cards) == len({c["url"] for c in cards}), "复用结果不该重复收同一张商品卡"


def test_scripted_online_loop_reuses_the_second_identical_tool_call():
    """模型爱在多跳里原样再问一次工具。第二跳要复用结果，同时两条 tool 消息都得留在
    消息序列里 —— 少一条，DeepSeek 就以「tool 消息没有对应的 tool_calls」400 拒收。"""
    from langchain_core.messages import AIMessage, ToolMessage

    from app.agent import graph

    def call(tool_call_id: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": "search_products", "args": {"query": "耳机", "limit": 2}, "id": tool_call_id, "type": "tool_call"}],
        )

    scripted = [call("call_1"), call("call_2"), AIMessage(content="给你两件现货耳机。")]

    class _Scripted:
        def __init__(self):
            self.step = 0

        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            msg = scripted[self.step]
            self.step += 1
            return msg

    def scenario():
        tid = store.ensure_thread(None)["id"]
        cards, calls = tools.reset_turn()
        tools.current_thread.set(tid)
        chain = graph.build_online_graph(_Scripted())
        state = chain.invoke(
            {
                "question": "耳机有货吗",
                "thread_id": tid,
                "history": [],
                "image_token": "",
                "messages": graph._messages({"history": [], "question": "耳机有货吗", "image_token": ""}),
                "hops": 0,
            }
        )
        rows = db.all_("SELECT tool FROM agent_tool_calls WHERE thread_id = :t", {"t": tid})
        return state, calls, rows

    state, calls, rows = contextvars.Context().run(scenario)

    tools_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tools_msgs] == ["call_1", "call_2"], "两个 tool_call_id 都要有对应回复"
    assert "同一份结果" in str(tools_msgs[1].content), tools_msgs[1].content
    assert "同一份结果" not in str(tools_msgs[0].content)
    assert len(calls) == 1 and len(rows) == 1, "重复那一跳不该再记一次调用"
    assert state["reply"] == "给你两件现货耳机。"


def test_login_state_is_told_to_the_model():
    """模型自己看不出对方登没登录，不问清楚它就一律跟人要订单号。"""
    from langchain_core.messages import SystemMessage

    from app.agent import graph

    def prompt_for(logged_in: bool) -> str:
        msgs = graph._messages({"history": [], "question": "我的订单", "image_token": "", "logged_in": logged_in})
        assert len(msgs) == 2 and isinstance(msgs[0], SystemMessage), "登录说明要并进那条 system，不能再开一条"
        return msgs[0].content

    mine = prompt_for(True)
    assert "order_no 留空" in mine and "已经登录" in mine
    assert "这一轮是游客" not in mine, "登录账号不该同时收到游客那条说法"
    guest = prompt_for(False)
    assert "这一轮是游客" in guest and "order_no 留空" not in guest
    assert len(graph.SYSTEM_PROMPT) < 900, "这两条说明挂在主提示之外，别把省下来的字数又吃回去"


def test_tool_param_rules_stay_in_the_tool_docstring():
    from app.agent import graph

    assert len(graph.SYSTEM_PROMPT) < 900, "系统提示每轮都要重发，膨胀的部分全是重复付费"
    assert not hasattr(graph, "_tool_context")
    # 这三条口径从系统提示挪走了，必须确认工具说明里还在，否则模型两头都读不到
    assert "体重" in tools.outfit_recommend.description
    assert "穿搭位" in tools.outfit_recommend.description
    assert "同款" in tools.find_similar_by_image.description


def _scripted_llm(reply: str, name: str, args: dict):
    """让假 DeepSeek 先调一次指定工具、再回一句话，复现在线那一轮的真实消息序列。"""
    from langchain_core.messages import AIMessage

    calls = [
        AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call_1", "type": "tool_call"}]),
        AIMessage(content=reply),
    ]

    class _Scripted:
        step = 0

        def bind_tools(self, _tools):
            return self

        def invoke(self, _messages):
            msg = calls[self.step]
            self.step += 1
            return msg

    return _Scripted()


@pytest.mark.parametrize(
    ("name", "args", "question"),
    [
        ("search_products", {"query": "耳机", "limit": 2}, "耳机有货吗"),
        # recommend.* 的行只有 slug：以前在线那一轮把推荐结果全当成「不是卡」丢了，
        # 用户收到的就是一段没有卡片的清单，离线却出得来卡
        ("recommend_products", {"budget_yuan": 300, "keywords": "耳机", "limit": 3}, "推荐 300 以内的耳机"),
    ],
)
def test_online_turn_logs_cards_the_history_replay_can_reuse(name, args, question):
    """在线那一轮以前只落标题字符串，刷新后卡片全渲染不出来；现在两条大脑落同一份卡片对象。"""
    from app.agent import graph

    def scenario():
        tid = store.ensure_thread(None)["id"]
        cards, calls = tools.reset_turn()
        tools.current_thread.set(tid)
        state = graph.build_online_graph(_scripted_llm("给你找到现货耳机。", name, args)).invoke(
            {"question": question, "thread_id": tid, "history": [], "image_token": "", "logged_in": False, "hops": 0}
        )
        store.append_message(tid, "assistant", state["reply"], intent="llm", tool_calls=calls)
        return tid, cards, calls

    tid, cards, calls = contextvars.Context().run(scenario)

    assert calls and calls[0]["tool"] == name, f"{name} 那一轮必须真的调过工具"
    assert cards, f"{name} 的结果一张卡都没出，前台就只剩一段文字清单"
    assert all(c["url"].startswith("/product/") for c in cards)
    replayed = store.cards(tid)
    assert [c["url"] for c in replayed] == [c["url"] for c in cards][: len(replayed)], "回放要和当轮给出同一批商品"
    assert all(set(c) <= set(tools.CARD_KEYS) for c in replayed)
    assert all(c.get("title") for c in replayed)
    assert all(c.get("image") is None or str(c["image"]).startswith("/img/products/") for c in replayed), "卡片只挂实物图"


def test_legacy_string_preview_never_becomes_a_broken_card():
    """库里早期那批 preview 是标题字符串，还原不出能点的卡；宁可没有卡，也别拼出 /product/undefined。"""

    def scenario():
        tid = store.ensure_thread(None)["id"]
        store.append_message(tid, "assistant", "上一轮的回答", tool_calls=[{"tool": "search_products", "count": 3, "preview": ["只有标题的旧格式"]}])
        return tid

    tid = contextvars.Context().run(scenario)
    assert store.cards(tid) == []
    assert store.cards_from_calls([{"preview": [{"title": "没有链接的行"}]}]) == []
