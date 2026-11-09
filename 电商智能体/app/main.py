"""应用装配：同一份代码装出两个进程——商城前台与运营后台，各监听一个端口。"""

from __future__ import annotations

import logging
import pathlib
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from . import admin_views, db, views
from .api import admin, agent_api, auth_api, cart, shop, wallet as wallet_api
from .deps import err
from .settings import get_settings

log = logging.getLogger("lumen")


def _configure_logging() -> None:
    """uvicorn 只配它自己的两个 logger，"lumen" 这条没人接手就会全静默：
    后台在关超时单、启动时打的商品与订单数，日志里一个字都看不到。"""
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


_configure_logging()


def create_app(surface: str = "shop") -> FastAPI:
    """按「面」装应用：`shop` 是买家侧页面与接口，`admin` 是运营后台。

    两个面各自监听一个端口。分开之后，买家侧进程里根本没有 `/api/admin` 这条路由，
    就算有人拿到会话也打不到后台接口，反之后台进程也渲染不出商城页面。
    """
    if surface not in ("shop", "admin"):
        raise ValueError(f"surface 只能是 shop 或 admin，收到 {surface!r}")
    settings = get_settings()
    title = settings.store_name if surface == "shop" else f"{settings.store_name} 运营后台"
    app = FastAPI(title=title, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.surface = surface

    uploads = pathlib.Path(settings.chat_upload_dir)
    uploads.mkdir(parents=True, exist_ok=True)  # StaticFiles 挂载时目录必须已经存在
    app.mount("/img/products", StaticFiles(directory="static/img/products"), name="product-images")
    app.mount(f"/{settings.chat_upload_prefix.strip('/')}", StaticFiles(directory=str(uploads)), name="chat-uploads")
    app.mount("/static", StaticFiles(directory="static"), name="static")

    if surface == "admin":
        app.include_router(admin.public_router, prefix="/api/admin")
        app.include_router(admin.router, prefix="/api/admin")
        app.include_router(admin_views.router)  # 兜底路由要最后注册
    else:
        app.include_router(shop.router, prefix="/api")
        app.include_router(auth_api.router, prefix="/api")
        app.include_router(cart.router, prefix="/api")
        app.include_router(wallet_api.router, prefix="/api")
        app.include_router(agent_api.router, prefix="/api/agent")
        app.include_router(views.router)  # 前台页面最后注册：兜底路由要接住所有未命中的 HTML 请求

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        code = "forbidden" if exc.status_code == 403 else "error"
        return JSONResponse(status_code=exc.status_code, content=err(str(exc.detail), code))

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception):
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content=err("服务器开小差了，请稍后再试", "server_error"))

    stop_sweep = threading.Event()

    @app.on_event("startup")
    def startup():
        counts = {
            "products": db.value("SELECT COUNT(*) FROM products"),
            "orders": db.value("SELECT COUNT(*) FROM orders"),
            "users": db.value("SELECT COUNT(*) FROM users"),
        }
        log.info("%s（%s 面）已连上 MySQL，商品 %s / 订单 %s / 会员 %s", title, surface, counts["products"], counts["orders"], counts["users"])
        log.info("小马大脑：%s", f"DeepSeek {settings.deepseek_model}" if settings.agent_online else "离线意图路由（未配置 DEEPSEEK_API_KEY）")
        if surface != "shop":
            return  # 超时单只让前台进程扫，两个进程都扫会互相抢同一批单
        stop_sweep.clear()
        threading.Thread(
            target=_sweep_expired, args=(stop_sweep, settings.order_sweep_seconds), name="order-expiry", daemon=True
        ).start()

    @app.on_event("shutdown")
    def shutdown():
        stop_sweep.set()

    return app


def _sweep_expired(stop: threading.Event, seconds: int) -> None:
    """先清一遍，之后每 `ORDER_SWEEP_SECONDS` 秒一遍，直到进程退出。

    光在启动时跑一次等于没有：服务连续开着的时候，超时单会一直挂着占库存。
    """
    from .services import checkout

    while not stop.is_set():
        try:
            released = checkout.release_expired()
            if released:
                log.info("释放超时未付订单 %s 笔", released)
        except Exception:  # 清欠失败不该拖垮循环，下一轮再来
            log.exception("释放超时订单失败")
        stop.wait(max(5, seconds))


app = create_app("shop")
