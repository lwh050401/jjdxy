"""运营后台的页面层：只渲染后台一个页面，商城页面不在这个进程里。

前台与后台分端口跑（见 `app.main.create_app` 的 `surface`），所以这里的返回链接一律
指回前台地址，否则在后台端口上点「返回」会撞到自己的 404。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db
from .agent import store as agent_store
from .deps import current_user
from .services import inventory, orders
from .settings import get_settings
from .views import render

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    who = current_user(request)
    if not who:
        return RedirectResponse("/admin/login", status_code=302)
    if who.get("role") != "admin":
        return RedirectResponse("/admin/login?reason=not_admin", status_code=302)
    return render(
        request,
        "admin.html",
        active="admin",
        overview={"orders": orders.stats(), "agent": agent_store.stats(), "tool_usage": agent_store.tool_stats(), "low_stock": inventory.low_stock(limit=8)},
        faqs=db.all_("SELECT * FROM support_faqs ORDER BY sort ASC, id ASC"),
    )


@router.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, reason: str = ""):
    who = current_user(request)
    if who and who.get("role") == "admin":
        return RedirectResponse("/admin", status_code=302)
    return render(
        request,
        "admin_login.html",
        active="",
        not_admin=bool(reason == "not_admin") and bool(who),
        visitor_name=(who or {}).get("name", ""),
    )


@router.get("/{path:path}", response_class=HTMLResponse)
def not_found(request: Request, path: str):
    shop = get_settings().shop_url
    if path.startswith("api/"):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="接口不存在")
    return render(
        request,
        "not_found.html",
        active="",
        status_code=404,
        title="页面不存在",
        back=f"{shop}/shop",
        hint="这里是运营后台地址，商城页面在前台。",
    )
