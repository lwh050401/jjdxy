"""接口测试的公共夹具。

跑的是本机 MySQL（`lumen_store`），每个用例用一次性注册账号，结束按子表到主表删干净，
不碰任何预置账号 —— 管理员也由夹具现场注册再提 role。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)  # 静态资源用相对路径挂载

from app import db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.settings import get_settings  # noqa: E402

# 在线大脑要花真 Key、答案每次都不一样，接口测试统一走离线路由；
# 在线分支由 `.run/check_online.py` 对着真 DeepSeek 人工核对。
os.environ["DEEPSEEK_API_KEY"] = ""
get_settings.cache_clear()


class ASGIWebClient:
    """同步风格的 ASGI 客户端。

    本机 starlette 0.27 的 TestClient 与 httpx 0.28 不兼容（Client 不再收 app 参数），
    所以直接走 ASGITransport，cookie 由同一个实例持有，登录态能跨请求保留。
    """

    def __init__(self, app):
        self._loop = asyncio.new_event_loop()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            follow_redirects=True,
            timeout=30,
        )

    def request(self, method: str, url: str, **kwargs):
        return self._loop.run_until_complete(self._client.request(method, url, **kwargs))

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    @property
    def cookies(self):
        return self._client.cookies

    def copy_cookies_from(self, other: "ASGIWebClient") -> None:
        """把另一个客户端的会话搬过来。

        前台与后台是两个进程，但浏览器里它们同域（127.0.0.1 的 cookie 不分端口），
        所以测试要模拟「在前台登录、直接开后台地址」这条真实路径。
        """
        for name, value in other.cookies.items():
            self._client.cookies.set(name, value)

    def close(self):
        self._loop.run_until_complete(self._client.aclose())
        self._loop.close()


@pytest.fixture(scope="session")
def client():
    c = ASGIWebClient(create_app("shop"))
    yield c
    c.close()


@pytest.fixture
def admin_client():
    """后台进程：只有 /admin 页面与 /api/admin/*。

    每个用例新建一个，保证默认是匿名会话，不会把上一个用例登录的管理员 cookie 带过去。
    """
    c = ASGIWebClient(create_app("admin"))
    yield c
    c.close()


@pytest.fixture(scope="session")
def variant():
    """挑一个便宜且有现货的规格，给购物车与下单用例复用。"""
    row = db.get(
        """SELECT v.id, v.sku, v.price_cents, v.stock_qty - v.reserved_qty AS available, p.slug, p.title
           FROM variants v JOIN products p ON p.id = v.product_id
           WHERE p.status = 'active' AND v.status = 'active' AND v.stock_qty - v.reserved_qty >= 5
           ORDER BY v.price_cents ASC LIMIT 1"""
    )
    assert row, "库里没有可用于测试的现货规格"
    return row


def _purge(user_id: str) -> None:
    for oid in [r["id"] for r in db.all_("SELECT id FROM orders WHERE user_id = :uid", {"uid": user_id})]:
        for table in ("order_items", "order_events", "payments", "refund_requests", "reservations"):
            db.run(f"DELETE FROM {table} WHERE order_id = :oid", {"oid": oid})
    db.run("DELETE FROM orders WHERE user_id = :uid", {"uid": user_id})
    for cid in [r["id"] for r in db.all_("SELECT id FROM carts WHERE user_id = :uid", {"uid": user_id})]:
        db.run("DELETE FROM cart_items WHERE cart_id = :cid", {"cid": cid})
    db.run("DELETE FROM carts WHERE user_id = :uid", {"uid": user_id})
    db.run("DELETE FROM refund_requests WHERE user_id = :uid", {"uid": user_id})
    db.run("DELETE FROM agent_messages WHERE thread_id IN (SELECT id FROM agent_threads WHERE user_id = :uid)", {"uid": user_id})
    db.run("DELETE FROM agent_tool_calls WHERE thread_id IN (SELECT id FROM agent_threads WHERE user_id = :uid)", {"uid": user_id})
    for table in ("sessions", "addresses", "balance_entries", "agent_threads"):
        db.run(f"DELETE FROM {table} WHERE user_id = :uid", {"uid": user_id})
    db.run("DELETE FROM users WHERE id = :uid", {"uid": user_id})


@pytest.fixture
def buyer(client):
    email = f"pytest-{uuid.uuid4().hex[:12]}@example.com"
    res = client.post("/api/register", json={"email": email, "password": "Pytest@12345", "name": "接口测试账号"})
    assert res.status_code == 200, res.text
    user = res.json()["data"]
    yield user
    _purge(user["id"])


@pytest.fixture(scope="session")
def admin_account(client):
    """仓库里没有预置账号，管理员就用「注册 + 改 role」现场造一个，用完删掉。"""
    email = f"pytest-admin-{uuid.uuid4().hex[:12]}@example.com"
    password = "Adm1n@pytest"
    res = client.post("/api/register", json={"email": email, "password": password, "name": "接口测试管理员"})
    assert res.status_code == 200, res.text
    user = res.json()["data"]
    db.run("UPDATE users SET role = 'admin' WHERE id = :uid", {"uid": user["id"]})
    yield {"email": email, "password": password, "id": user["id"]}
    _purge(user["id"])


@pytest.fixture(scope="session", autouse=True)
def _no_empty_guest_carts():
    """游客浏览页面会建空车，跑完统一清掉，不给演示库留垃圾。"""
    yield
    db.run("DELETE FROM carts WHERE user_id IS NULL AND id NOT IN (SELECT cart_id FROM cart_items)", {})
