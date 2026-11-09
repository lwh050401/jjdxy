"""小马的对话接口：前台聊天窗与独立聊天页都调这里。"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import db
from ..agent import graph, store, tools
from ..deps import current_user, err, guest_token, ok
from ..security import new_id
from ..services import visual
from ..settings import get_settings

router = APIRouter()

# 轻量频控：单个来源每分钟最多 20 条，防止脚本刷屏
_WINDOW = 60.0
_LIMIT = 20
_UPLOAD_LIMIT = 6  # 图片比对每张都要算描述子，上传单独收紧
_hits: dict[str, deque] = defaultdict(deque)

# 只带图不带文字时，替用户把这句话补全，好让离线路由和模型都知道要干什么
IMAGE_ONLY_QUESTION = "这张图在站内有相似的吗"


def _limited(key: str, limit: int = _LIMIT) -> bool:
    now = time.time()
    queue = _hits[key]
    while queue and now - queue[0] > _WINDOW:
        queue.popleft()
    if len(queue) >= limit:
        return True
    queue.append(now)
    return False


@router.post("/images")
def upload_image(request: Request, file: UploadFile = File(...), thread_id: str = Form("")):
    """收一张图：算出描述子落库，返回编号给聊天窗接着发消息。"""
    ip = request.client.host if request.client else "anon"
    if _limited(f"upload:{ip}:{(current_user(request) or {}).get('id', 'guest')}", _UPLOAD_LIMIT):
        return JSONResponse(status_code=429, content=err("传图太快了，歇一下再传", "rate_limited"))
    mime = str(file.content_type or "").lower()
    if mime not in visual.ACCEPTED_KINDS:
        return JSONResponse(status_code=400, content=err("只支持 JPG、PNG、WebP 图片", "bad_image_type"))
    data = file.file.read(get_settings().chat_upload_max_bytes + 1)
    if not data:
        return JSONResponse(status_code=400, content=err("这张图是空的"))
    if len(data) > get_settings().chat_upload_max_bytes:
        return JSONResponse(status_code=413, content=err("图片超过 5 MB，请压缩后再传", "too_large"))
    try:
        sig = visual.descriptor(data)
    except ValueError as problem:
        return JSONResponse(status_code=400, content=err(str(problem), "bad_image"))
    known = db.value("SELECT id FROM agent_threads WHERE id = :t", {"t": thread_id}) if thread_id else None
    token = new_id("img")
    row = visual.record(token, data, mime=mime, filename=file.filename or "", thread_id=thread_id if known else None, sig=sig)
    return ok(
        {
            "token": token,
            "url": row["url"],
            "color": sig["color"],
            "width": sig["width"],
            "height": sig["height"],
            "bytes": len(data),
            "note": f"已按 {sig['color']}、明暗和轮廓记下这张图，发一句话或直接发送就能找相似。",
        }
    )


@router.post("/messages")
def messages(request: Request, payload: dict):
    ip = request.client.host if request.client else "anon"
    if _limited(f"{ip}:{(current_user(request) or {}).get('id', 'guest')}"):
        return JSONResponse(status_code=429, content=err("提问太快了，歇一下再发", "rate_limited"))
    text = str(payload.get("message") or "").strip()[:500]
    image_token = str(payload.get("image_token") or "").strip()[:40]
    if not text and image_token:
        text = IMAGE_ONLY_QUESTION
    if not text:
        return JSONResponse(status_code=400, content=err("请输入内容"))
    who = current_user(request)
    token = guest_token(request)
    fresh_guest = not who and not token
    if fresh_guest:
        token = new_id("gst")
    result = graph.ask(text, thread_id=payload.get("thread_id") or None, user=who, guest_token=token, image_token=image_token)
    response = JSONResponse(content=ok(result))
    if fresh_guest:
        response.set_cookie("lumen_guest", token, httponly=True, samesite="lax", max_age=30 * 24 * 3600, path="/")
    return response


@router.get("/history")
def history(request: Request, thread_id: str = ""):
    if not thread_id:
        return ok({"thread_id": None, "messages": [], "cards": []})
    rows = store.history(thread_id, limit=60)
    messages = []
    for r in rows:
        calls = store.as_calls(r.get("tool_calls"))
        messages.append(
            {
                "role": r["role"],
                "content": r["content"],
                "intent": r.get("intent"),
                "created_at": r["created_at"],
                # 气泡要连工具标签和卡片一起回放，否则刷新后用户只剩一段没有商品卡的文字
                "tool_calls": [{**c, "label": c.get("label") or store.tool_text(c.get("tool", ""))} for c in calls],
                "cards": store.cards_from_calls(calls),
            }
        )
    return ok(
        {
            "thread_id": thread_id,
            "messages": messages,
            "cards": store.cards(thread_id),
        }
    )


@router.get("/state")
def state():
    s = get_settings()
    return ok(
        {
            "online": s.agent_online,
            "model": graph.model_name(),
            "tools": [t.name for t in tools.ALL_TOOLS],
            "upload": {"accept": sorted(visual.ACCEPTED_KINDS), "max_bytes": s.chat_upload_max_bytes},
        }
    )


@router.get("/quick")
def quick():
    """首屏快捷提问：优先取后台知识库里被问得最多的，不足则给通用能力提示。"""
    rows = db.all_("SELECT question FROM support_faqs WHERE active = 1 ORDER BY hits DESC, sort ASC LIMIT 4")
    items = [r["question"] for r in rows]
    for fallback in ("最近有什么优惠？", "帮我推荐 200 元以内的礼物", "朋友结婚我该穿什么？", "露营桌还有货吗", "LM 开头的订单号帮我查一下进度"):
        if len(items) >= 5:
            break
        if fallback not in items:
            items.append(fallback)
    return ok(items[:5])
