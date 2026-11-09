"""小马的会话落库：线程、消息与工具调用都写 MySQL，运营要能复盘。"""

from __future__ import annotations

from .. import db
from ..security import new_id


def ensure_thread(thread_id: str | None, *, user: dict | None = None, guest_token: str | None = None, channel: str = "web") -> dict:
    now = db.now_iso()
    if thread_id:
        row = db.get("SELECT * FROM agent_threads WHERE id = :tid", {"tid": thread_id})
        if row:
            if user and not row.get("user_id"):
                db.run("UPDATE agent_threads SET user_id = :uid, updated_at = :now WHERE id = :tid", {"uid": user["id"], "now": now, "tid": thread_id})
            return db.get("SELECT * FROM agent_threads WHERE id = :tid", {"tid": thread_id})
    tid = thread_id or new_id("thr")
    db.insert(
        "agent_threads",
        {
            "id": tid,
            "user_id": (user or {}).get("id"),
            "guest_token": guest_token,
            "channel": channel,
            "status": "open",
            "turns": 0,
            "created_at": now,
            "updated_at": now,
        },
    )
    return db.get("SELECT * FROM agent_threads WHERE id = :tid", {"tid": tid})


def append_message(
    thread_id: str,
    role: str,
    content: str,
    *,
    intent: str | None = None,
    tool_calls: list[dict] | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
) -> int:
    message_id = db.insert(
        "agent_messages",
        {
            "thread_id": thread_id,
            "role": role,
            "content": content[:8000],
            "intent": intent,
            "tool_calls": tool_calls or None,
            "model": model,
            "latency_ms": latency_ms,
            "created_at": db.now_iso(),
        },
    )
    now = db.now_iso()
    db.run(
        "UPDATE agent_threads SET updated_at = :now, turns = turns + :inc, last_intent = COALESCE(:intent, last_intent) WHERE id = :tid",
        {"now": now, "inc": 1 if role == "user" else 0, "intent": intent, "tid": thread_id},
    )
    return int(message_id or 0)


def record_tool_call(*, thread_id: str, tool: str, args: dict, result_count: int, status: str, duration_ms: int) -> None:
    db.insert(
        "agent_tool_calls",
        {
            "thread_id": thread_id,
            "tool": tool[:48],
            "args": {k: v for k, v in (args or {}).items() if v not in (None, "")},
            "result_count": int(result_count),
            "status": status,
            "duration_ms": int(duration_ms),
            "created_at": db.now_iso(),
        },
    )


def history(thread_id: str, *, limit: int = 40) -> list[dict]:
    rows = db.all_(
        "SELECT * FROM agent_messages WHERE thread_id = :tid ORDER BY id DESC LIMIT :limit",
        {"tid": thread_id, "limit": int(limit)},
    )
    return list(reversed(rows))


def messages_for_model(thread_id: str, *, turns: int = 8) -> list[dict]:
    """只带最近 N 轮进模型，太长会让回答变飘。"""
    rows = db.all_(
        "SELECT role, content FROM agent_messages WHERE thread_id = :tid AND role IN ('user','assistant') ORDER BY id DESC LIMIT :limit",
        {"tid": thread_id, "limit": turns * 2},
    )
    return list(reversed(rows))


def as_calls(value) -> list[dict]:
    calls = value or []
    if isinstance(calls, str):
        import json

        calls = json.loads(calls)
    return [c for c in calls if isinstance(c, dict)]


def cards_from_calls(calls, *, limit: int = 6) -> list[dict]:
    """从一条回复的 tool_calls 里还原商品卡，前台刷新后靠它把橱窗补回来。

    preview 只认带 url 的卡片对象；库里更早的那批行存的是标题字符串，
    那种还原不出能点的卡，直接跳过而不是拼一个点不开的 /product/undefined。
    """
    cards: list[dict] = []
    for call in as_calls(calls):
        for item in (call.get("preview") or []):
            if isinstance(item, dict) and item.get("url") and item not in cards:
                cards.append(item)
    return cards[:limit]


def cards(thread_id: str, *, limit: int = 12) -> list[dict]:
    """从最近一次带商品卡的回复里取卡片，前端据此渲染商品橱窗。"""
    row = db.get(
        "SELECT tool_calls FROM agent_messages WHERE thread_id = :tid AND role = 'assistant' AND tool_calls IS NOT NULL ORDER BY id DESC LIMIT 1",
        {"tid": thread_id},
    )
    if not row:
        return []
    return cards_from_calls(row.get("tool_calls"), limit=limit)


INTENT_TEXT = {
    "greeting": "问候",
    "product_search": "商品检索",
    "product_price": "价格咨询",
    "stock": "库存查询",
    "order_lookup": "订单查询",
    "my_orders": "历史订单",
    "refund": "退换货",
    "shipping": "配送运费",
    "promo": "优惠",
    "payment": "支付",
    "account": "账号",
    "invoice": "发票",
    "human": "转人工",
    "recommend": "推荐",
    "outfit": "场合穿搭",
    "visual_search": "按图找相似",
    "llm": "大模型作答",
    "identity": "自我介绍",
    "fallback": "未命中",
    "product_not_found": "没找到商品",
}

STATUS_TEXT = {"open": "待处理", "resolved": "已解决", "unanswered": "未回答", "escalated": "转人工"}

TOOL_TEXT = {
    "search_products": "商品检索",
    "product_detail": "商品详情",
    "query_order": "订单查询",
    "check_inventory": "库存查询",
    "recommend_products": "商品推荐",
    "outfit_recommend": "场合穿搭",
    "find_similar_by_image": "按图找相似",
    "shipping_options": "配送方案",
    "coupons_available": "可用优惠码",
    "store_faq": "常见问题",
}


def tool_text(name: str) -> str:
    return TOOL_TEXT.get(name, name or "工具")


def list_threads(*, q: str = "", status: str = "", limit: int = 30, offset: int = 0) -> dict:
    where, params = [], {}
    if q:
        where.append("(t.id LIKE :q OR u.email LIKE :q OR EXISTS (SELECT 1 FROM agent_messages m WHERE m.thread_id = t.id AND m.content LIKE :q))")
        params["q"] = f"%{q}%"
    if status:
        where.append("t.status = :status")
        params["status"] = status
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = int(
        db.value(
            f"SELECT COUNT(*) FROM agent_threads t LEFT JOIN users u ON u.id = t.user_id {clause}",
            params,
        )
        or 0
    )
    rows = db.all_(
        f"""SELECT t.*, u.email, u.name,
                   (SELECT COUNT(*) FROM agent_messages m WHERE m.thread_id = t.id AND m.role = 'user') AS user_turns,
                   (SELECT content FROM agent_messages m WHERE m.thread_id = t.id AND m.role = 'user' ORDER BY m.id DESC LIMIT 1) AS last_question
            FROM agent_threads t LEFT JOIN users u ON u.id = t.user_id {clause}
            ORDER BY t.updated_at DESC LIMIT :limit OFFSET :offset""",
        {**params, "limit": int(limit), "offset": int(offset)},
    )
    for r in rows:
        r["intent_text"] = INTENT_TEXT.get(r.get("last_intent"), r.get("last_intent") or "会话")
        r["status_text"] = STATUS_TEXT.get(r.get("status"), r.get("status"))
    return {"items": rows, "total": total}


def tool_stats(*, limit: int = 10) -> list[dict]:
    rows = db.all_(
        "SELECT tool, COUNT(*) AS calls, IFNULL(SUM(result_count), 0) AS hits, ROUND(AVG(duration_ms)) AS avg_ms "
        "FROM agent_tool_calls GROUP BY tool ORDER BY calls DESC LIMIT :limit",
        {"limit": int(limit)},
    )
    return [
        {
            "tool": r["tool"],
            "label": tool_text(r["tool"]),
            "calls": int(r["calls"]),
            "hits": int(r["hits"]),
            "avg_ms": int(r["avg_ms"] or 0),
        }
        for r in rows
    ]


def stats() -> dict:
    return {
        "threads": int(db.value("SELECT COUNT(*) FROM agent_threads") or 0),
        "messages": int(db.value("SELECT COUNT(*) FROM agent_messages") or 0),
        "tool_calls": int(db.value("SELECT COUNT(*) FROM agent_tool_calls") or 0),
        "unanswered": int(db.value("SELECT COUNT(*) FROM agent_messages WHERE intent = 'fallback'") or 0),
    }
