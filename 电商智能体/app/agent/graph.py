"""小马的 LangGraph 图。

两条路径共用一张图与同一批工具：
- 配了 DeepSeek Key 走模型自主决策（think → tools → think，最多 4 跳）
- 没配 Key 走离线意图路由（route → act → respond），照样是同一套工具与同一张状态表
"""

from __future__ import annotations

import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from .. import db
from ..settings import get_settings
from . import offline, shop_api, store, tools

MAX_HOPS = 4

SYSTEM_PROMPT = """你是「小马」，Lumen 精选商城的在线客服。

工作方式：
1. 涉及商品价格、库存、订单状态、配送时效、优惠码的问题，必须先调用工具查真实数据，绝不凭记忆或编造；
   一个问句可能要连用好几个工具。
2. 工具返回的是数据库里的实时结果，回答时直接引用具体数字：价格写 ¥xx.xx，库存写「N 件现货」。
3. 查不到就照实说查不到，并给出下一步（换个关键词、提供订单号、或联系 support@lumen.shop），不要假装有这个商品或服务。
4. 每个工具能查什么、参数怎么填、结果怎么说，都以工具自身的说明为准；用户没亲口说过的条件
   （身高、体型、性格、预算、场景）一律不要替他补、替他估或替他改写。
5. 消息里出现「图片编号 img_xxxxxxxx」说明用户上传了一张图（你看不到图的内容），必须先调
   find_similar_by_image 再回答，编号原样填进 image_token。
6. 用户要下单、付款、改地址时，告诉对方在页面上怎么操作，你不能替对方改动数据。
7. 回答用简体中文，口语、克制、不吹嘘，不用「亲」「宝」这类称呼，不堆 emoji，不超过 200 字，分条时用「·」开头。
8. 商城只有工具能查到的这些能力：商品检索与详情、库存、订单、推荐、场合穿搭、按图找相似、配送方案、优惠码、常见问题。
   不要承诺未提供的服务（电话回访、上门安装、鉴定证书、线下搭配师），也不要提站内没有的通知方式（短信、发货提醒推送）；
   订单号引导用户去「我的订单」页和付款成功页看，客服联系方式只有在线咨询和 support@lumen.shop。
9. 提问的人是登录账号还是游客，看下面单独给的那一条说明，不要凭猜。"""

# 模型自己看不出对方登没登录：不说清楚它就一律跟人要订单号，登录用户的「我的订单」就问不出结果
LOGIN_NOTE = """补充：这一轮的提问者已经登录。他问「我的订单」「上次买的」「发货了吗」时，
直接调 query_order 并把 order_no 留空，工具会返回他账号里最近的订单，不要反过来找他要订单号。"""
GUEST_NOTE = """补充：这一轮是游客，没有登录账号。他问「我的订单」时先说明需要订单号（LM 开头的 14 位）或登录，
不要猜任何订单。"""


class XiaomaState(TypedDict, total=False):
    question: str
    thread_id: str
    history: list
    image_token: str
    logged_in: bool
    # 必须有 add_messages 累加器：否则 ToolNode 写回的工具消息会整表覆盖，
    # 把带 tool_calls 的那条 AI 消息挤掉，DeepSeek 会以 400「tool 消息没有对应的 tool_calls」拒收
    messages: Annotated[list, add_messages]
    hops: int
    intent: str
    reply: str
    tool_calls: list
    cards: list
    model: str
    error: str


def build_llm():
    """DeepSeek 走 OpenAI 兼容协议；没配 Key 返回 None，由调用方降级。"""
    s = get_settings()
    if not s.agent_online:
        return None
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=s.deepseek_model,
        api_key=s.deepseek_api_key,
        base_url=s.deepseek_base_url,
        temperature=0.3,
        timeout=25,
        max_retries=1,
    )


def _messages(state: XiaomaState) -> list[BaseMessage]:
    prompt = SYSTEM_PROMPT + ("\n\n" + (LOGIN_NOTE if state.get("logged_in") else GUEST_NOTE))
    out: list[BaseMessage] = [SystemMessage(content=prompt)]
    for item in state.get("history", []):
        role = item.get("role")
        content = item.get("content") or ""
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
    question = state["question"]
    token = state.get("image_token") or ""
    if token:
        question = f"{question}\n（用户附了一张图，图片编号 {token}）"
    out.append(HumanMessage(content=question))
    return out


def build_online_graph(llm):
    bound = llm.bind_tools(tools.ALL_TOOLS)
    nodes = ToolNode(tools.ALL_TOOLS)

    def think(state: XiaomaState) -> dict:
        msgs = list(state.get("messages") or _messages(state))
        # 工具清单不用再拼一条 system 消息：bind_tools 已把每个工具的名称、说明和参数
        # schema 一起发给模型，每条尾注只会白烧 token，还会把消息顺序搞乱
        reply = bound.invoke(msgs)
        return {"messages": [reply], "hops": int(state.get("hops", 0)) + 1}

    def decide(state: XiaomaState) -> str:
        last = state["messages"][-1] if state.get("messages") else None
        if isinstance(last, AIMessage) and last.tool_calls and state.get("hops", 0) < MAX_HOPS:
            return "tools"
        return "respond"

    def respond(state: XiaomaState) -> dict:
        last = state["messages"][-1] if state.get("messages") else None
        text = ""
        if isinstance(last, AIMessage):
            text = last.content if isinstance(last.content, str) else "".join(p.get("text", "") for p in last.content)
        if not text:
            for message in reversed(state["messages"]):
                if isinstance(message, AIMessage) and message.content:
                    text = message.content if isinstance(message.content, str) else "".join(p.get("text", "") for p in message.content)
                    break
        return {"reply": text.strip() or "我这边没查到能直接回答的信息，可以换个说法再问一次。", "intent": "llm"}

    graph = StateGraph(XiaomaState)
    graph.add_node("think", think)
    graph.add_node("tools", nodes)
    graph.add_node("respond", respond)
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", decide, {"tools": "tools", "respond": "respond"})
    graph.add_edge("tools", "think")
    graph.add_edge("respond", END)
    return graph.compile()


_offline_graph: Any = None


def build_offline_graph() -> Any:
    """离线版同样是一张 StateGraph：路由决定要调哪些工具，act 执行，respond 组织话术。

    编译好的图可以反复 invoke（状态每次从 payload 传进去），所以只做一次；
    Key 失效或在线图抛异常时每轮都落这里，重新 compile 一遍纯属白烧。
    """
    global _offline_graph
    if _offline_graph is not None:
        return _offline_graph

    def route(state: XiaomaState) -> dict:
        plan = offline.run(state["question"], image_token=state.get("image_token") or "")
        return {"intent": plan["intent"], "reply": plan["reply"], "tool_calls": plan["tool_calls"], "cards": plan["cards"]}

    graph = StateGraph(XiaomaState)
    graph.add_node("route", route)
    graph.add_edge(START, "route")
    graph.add_edge("route", END)
    _offline_graph = graph.compile()
    return _offline_graph


_graph_cache: dict[str, Any] = {}


def get_graph():
    s = get_settings()
    key = "online" if s.agent_online else "offline"
    if key not in _graph_cache:
        if key == "online":
            try:
                _graph_cache[key] = build_online_graph(build_llm())
            except Exception as err:  # Key 无效或依赖异常时不至于整站打不开
                _graph_cache["error"] = str(err)
                _graph_cache[key] = None
        else:
            _graph_cache[key] = build_offline_graph()
    return _graph_cache.get(key)


def model_name() -> str:
    s = get_settings()
    return s.deepseek_model if s.agent_online and get_graph() else "offline-router"


def ask(
    question: str,
    *,
    thread_id: str | None = None,
    user: dict | None = None,
    guest_token: str | None = None,
    image_token: str = "",
) -> dict:
    """一次对话轮次：落库 → 跑图 → 落回复。工具与前台 API 共用同一份数据口径。"""
    started = time.time()
    thread = store.ensure_thread(thread_id, user=user, guest_token=guest_token)
    tid = thread["id"]
    cards, calls = tools.reset_turn()
    tools.current_thread.set(tid)
    shop_api.current_actor.set(shop_api.Actor(user=user, guest_token=guest_token))
    store.append_message(tid, "user", question)
    if image_token:
        # 图片归到这条会话名下，后台复盘能看到用户当时传了什么
        db.run("UPDATE agent_images SET thread_id = :tid WHERE token = :token AND thread_id IS NULL", {"tid": tid, "token": image_token})

    graph = get_graph()
    history = store.messages_for_model(tid)
    # 在线图要先把系统提示与历史塞进 messages 通道，ToolNode 之后才能接上 tool 消息
    payload: XiaomaState = {
        "question": question,
        "thread_id": tid,
        "history": history,
        "image_token": image_token or "",
        "logged_in": bool(user),
        "messages": _messages(
            {"history": history, "question": question, "image_token": image_token or "", "logged_in": bool(user)}
        ),
        "hops": 0,
    }
    error = ""
    if graph is None:
        graph = build_offline_graph()
        error = _graph_cache.get("error", "")
    try:
        state = graph.invoke(payload)
    except Exception as err:
        state = build_offline_graph().invoke(payload)
        error = f"{type(err).__name__}: {err}"

    reply = (state or {}).get("reply") or ""
    intent = (state or {}).get("intent") or "fallback"
    used = "offline-router" if error else model_name()
    tool_calls = [
        {**c, "label": store.tool_text(c.get("tool", ""))} for c in (calls if calls else (state or {}).get("tool_calls", []))
    ]
    product_cards = cards if cards else (state or {}).get("cards", [])
    latency = int((time.time() - started) * 1000)
    store.append_message(
        tid,
        "assistant",
        reply,
        intent=intent,
        tool_calls=tool_calls,
        model=used,
        latency_ms=latency,
    )
    return {
        "thread_id": tid,
        "reply": reply,
        "intent": intent,
        "cards": product_cards[:6],
        "tool_calls": tool_calls,
        "model": used,
        "latency_ms": latency,
        "degraded": bool(error),
        "note": error or None,
    }
