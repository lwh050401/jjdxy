"""钱包接口：余额、流水与模拟充值。"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..deps import current_user, err, ok
from ..services import checkout, wallet

router = APIRouter()

_WINDOW = 60.0
_LIMIT = 20
_hits: dict[str, deque] = defaultdict(deque)


def _limited(user_id: str) -> bool:
    now = time.time()
    queue = _hits[user_id]
    while queue and now - queue[0] > _WINDOW:
        queue.popleft()
    if len(queue) >= _LIMIT:
        return True
    queue.append(now)
    return False


def _require(request: Request):
    who = current_user(request)
    if not who:
        return None, JSONResponse(status_code=401, content=err("请先登录", "unauthorized"))
    return who, None


@router.get("/wallet")
def get_wallet(request: Request, limit: int = 50):
    who, denied = _require(request)
    if denied:
        return denied
    return ok(wallet.overview(who["id"], limit=limit))


@router.post("/wallet/topup")
def topup(request: Request, payload: dict):
    who, denied = _require(request)
    if denied:
        return denied
    if _limited(who["id"]):
        return JSONResponse(status_code=429, content=err("充值请求太频繁，请稍后再试", "rate_limited"))
    try:
        entry = wallet.top_up(who["id"], int(payload.get("amount_cents") or 0))
    except (checkout.CheckoutError, TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content=err(getattr(exc, "message", "充值金额格式不正确")))
    return JSONResponse(status_code=201, content=ok({"entry": entry, "balance_cents": wallet.balance_of(who["id"])}))
