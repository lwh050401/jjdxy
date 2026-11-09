"""页面渲染与状态码：SSR 层是用户「能不能打开」的第一道关口。"""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx

from app import db
from app.main import create_app
from app.settings import get_settings

PAGES = ["/", "/shop", "/cart", "/checkout", "/support", "/account"]

# 仓库里没有预置账号：管理员用 admin_account 夹具现场注册并提 role，用完删掉
BUYER_PASSWORD = "Pytest@12345"  # 与 conftest 里 buyer 夹具注册的密码保持一致

CONTACT = {"email": "page@example.com", "phone": "13800000000"}
ADDRESS = {
    "receiver": "接口测试",
    "phone": "13800000000",
    "province": "浙江省",
    "city": "杭州市",
    "district": "西湖区",
    "detail": "文三路 1 号",
}


def test_catalog_scaled_to_two_thousand(client):
    """演示库按要求补到 2000+ 在售：分类侧栏计数与深翻页都要正常。"""
    assert client.get("/api/health").json()["data"]["products"] >= 2000
    cats = client.get("/api/categories").json()["data"]
    assert sum(c["product_count"] for c in cats) >= 2000
    assert all(c["product_count"] > 0 for c in cats), "每个分类都该有在售商品"
    last = client.get("/api/products?per_page=60").json()["data"]
    deep = client.get(f"/shop?category=fashion&page={max(1, last['pages'])}")
    assert deep.status_code == 200


def test_public_pages_render(client):
    for path in PAGES:
        res = client.get(path)
        assert res.status_code == 200, (path, res.status_code)
        assert "Lumen" in res.text


def test_pages_are_chinese(client):
    home = client.get("/").text
    for word in ("全部商品", "小马客服", "购物车"):
        assert word in home


def test_product_page(client, variant):
    res = client.get(f"/product/{variant['slug']}")
    assert res.status_code == 200
    assert variant["title"] in res.text


def test_unknown_paths_use_status_codes(client):
    assert client.get("/this-page-does-not-exist").status_code == 404
    assert client.get("/product/no-such-slug").status_code == 404
    assert client.get("/order/LM000000AAAAAA").status_code == 404
    assert client.get("/admin").status_code == 404  # 后台在另一个端口，前台进程里没有这条路由


def test_api_404_is_json(client):
    res = client.get("/api/nope")
    assert res.status_code == 404
    assert res.json()["ok"] is False


def test_health_reports_mysql(client):
    data = client.get("/api/health").json()["data"]
    assert data["status"] == "healthy"
    assert data["database"] == "mysql"
    assert data["products"] > 0


def test_surfaces_do_not_overlap(client, admin_client):
    """前台进程里查不到后台路由，后台进程里也渲染不出商城页面。

    分端口不只是换个地址：拿到买家会话的人也不该在商城地址上打到后台接口。
    """
    assert client.get("/admin").status_code == 404
    assert client.get("/api/admin/orders").status_code == 404
    assert admin_client.get("/shop").status_code == 404
    assert admin_client.get("/api/products").status_code == 404


def test_admin_surface_needs_admin_session(admin_client):
    """没会话打开后台地址会被领到后台自己的登录页，接口照旧 403。"""
    res = admin_client.get("/admin")
    assert res.url.path == "/admin/login", f"匿名访问 /admin 应该跳到登录页，实际停在 {res.url}"
    assert "管理员登录" in res.text
    assert 'id="adminLoginForm"' in res.text
    assert admin_client.get("/api/admin/orders").status_code in (401, 403)


def test_admin_page_opens_with_session_from_shop(client, admin_client, admin_account):
    """在前台登录管理员，浏览器带着同一个 cookie 打开后台地址应当直接进得去。"""
    res = client.post(
        "/api/login",
        json={"email": admin_account["email"], "password": admin_account["password"]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["role"] == "admin"
    admin_client.copy_cookies_from(client)
    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert "运营后台" in page.text
    overview = admin_client.get("/api/admin/overview")
    assert overview.status_code == 200
    assert set(overview.json()["data"]) >= {"orders", "agent", "low_stock"}
    products = admin_client.get("/api/admin/products", params={"per_page": 1})
    assert products.status_code == 200
    assert products.json()["data"]["total"] > 0
    # 后台页面里指向商城的链接必须带上前台地址，否则点一下就掉回 3101 的 404
    assert get_settings().shop_url in page.text
    client.post("/api/logout")  # 用完就注销，不给管理员账号留活会话


def test_admin_api_requires_admin(client, admin_client, buyer):
    """买家会话即使拿到后台地址，也打不开任何管理接口。"""
    assert admin_client.get("/api/admin/orders").status_code == 403  # 匿名
    admin_client.copy_cookies_from(client)  # buyer 夹具刚在前台注册，client 此刻持有买家会话
    assert admin_client.get("/api/admin/orders").status_code == 403
    res = admin_client.get("/admin")
    assert res.url.path == "/admin/login"
    assert res.url.params.get("reason") == "not_admin", "买家账号该被领去登录页并说清楚是身份不对"


def test_admin_logs_in_on_its_own_port(admin_client, admin_account):
    """后台不必绕前台：在 3101 上直接输管理员账号就能进面板。"""
    assert admin_client.get("/api/admin/overview").status_code == 403
    res = admin_client.post(
        "/api/admin/login",
        json={"email": admin_account["email"], "password": admin_account["password"]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["role"] == "admin"
    assert admin_client.get("/api/admin/overview").status_code == 200
    page = admin_client.get("/admin")
    assert page.status_code == 200 and page.url.path == "/admin"
    assert "运营后台" in page.text
    # 已经登进来再打开登录页，不该再看见登录表单
    assert 'id="adminLoginForm"' not in admin_client.get("/admin/login").text
    assert admin_client.post("/api/admin/logout").status_code == 200  # 用完注销，不给管理员账号留活会话


def test_admin_login_rejects_a_buyer_account(admin_client, buyer):
    """买家账号在后台表单里登不进来，也不给留一条可用会话。"""
    res = admin_client.post("/api/admin/login", json={"email": buyer["email"], "password": BUYER_PASSWORD})
    assert res.status_code == 403, res.text
    assert "不是管理员" in res.json()["error"]["message"]
    assert admin_client.get("/api/admin/overview").status_code == 403
    assert admin_client.get("/admin").url.path == "/admin/login"


def test_admin_logout_closes_the_panel(admin_client, admin_account):
    assert admin_client.post(
        "/api/admin/login",
        json={"email": admin_account["email"], "password": admin_account["password"]},
    ).status_code == 200
    assert admin_client.get("/admin").status_code == 200
    assert admin_client.post("/api/admin/logout").status_code == 200
    assert admin_client.get("/api/admin/overview").status_code == 403
    assert admin_client.get("/admin").url.path == "/admin/login"


def test_product_page_shows_real_intro(client):
    """商品介绍不能是空壳：导语、亮点、配送与售后都要来自数据库里的真实字段。"""
    row = db.get(
        "SELECT slug, description, attributes FROM products WHERE slug LIKE :p AND description <> '' LIMIT 1",
        {"p": "seed-%"},
    )
    assert row, "演示商品应带真实介绍"
    points = row["attributes"]
    if isinstance(points, str):
        points = json.loads(points)
    points = points.get("亮点") or []
    assert len(points) >= 3, "每件商品至少三条亮点"

    page = client.get(f"/product/{row['slug']}")
    assert page.status_code == 200
    for block in ("商品介绍", "这件好在哪", "参数速览", "配送与售后"):
        assert block in page.text, block
    assert row["description"][:12] in page.text
    for point in points:
        assert point in page.text, point


def test_xiaoma_panel_can_move_and_resize(client):
    """悬浮客服窗要能拖、能拉伸、能放大，位置尺寸存本地，且不参与进场动画。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "templates" / "partials" / "xiaoma.html").read_text(encoding="utf-8")
    js = (root / "static" / "js" / "site.js").read_text(encoding="utf-8")
    reveal = (root / "static" / "js" / "maison.js").read_text(encoding="utf-8")
    for marker in ("xiaomaHead", "xiaomaMax", "xiaomaGrip"):
        assert f'id="{marker}"' in html, marker
    assert "lumen_xiaoma_box" in js and "pointerdown" in js and "is-max" in js
    assert "not(.xiaoma-panel)" in reveal, "固定层一旦被进场动画位移，存档几何就不准"
    page = client.get("/shop")
    assert page.status_code == 200 and "xiaomaGrip" in page.text


def test_every_active_product_has_intro_copy(client):
    """「每个商品都有商品介绍」是硬口径：导语与亮点都不能缺，亮点最多三条。"""
    missing = db.value(
        """SELECT COUNT(*) FROM products
           WHERE status = 'active' AND (COALESCE(description, '') = ''
             OR attributes IS NULL OR NOT JSON_CONTAINS_PATH(attributes, 'one', '$."亮点"'))""",
        {},
    )
    assert missing == 0, f"{missing} 件在售商品没有介绍文案"


def test_no_product_borrows_another_products_photo(client):
    """一张图只挂它真画着的那个品名：配不到就留空给前台显示「暂无实物图」，宁可无图不可错图。"""
    from app.seed_catalog import name_images

    manifest = name_images()
    rows = db.all_(
        """SELECT p.title, p.attributes AS a, i.url FROM products p
           JOIN product_images i ON i.product_id = p.id AND i.sort = 0 WHERE p.slug LIKE 'seed-%'"""
    )
    wrong = [
        r["title"]
        for r in rows
        if manifest.get(str((r["a"] if isinstance(r["a"], dict) else json.loads(r["a"] or "{}")).get("品类") or ""))
        != pathlib.PurePosixPath(r["url"]).name
    ]
    assert not wrong, f"{len(wrong)} 件商品挂着别的商品的图：{wrong[:5]}"
    assert len(rows) > 1000, "有图商品太少，说明 --rebind-images 没跑"


def test_intro_points_come_from_their_own_copy(client):
    """亮点的出处只有两个：数据包写死的三条，或旧商品自家导语里拆出来的句子。

    导语只拆得出两句的就留两句（Glow 面霜、Pulse 跳绳没有第三句可写），硬凑第三条就是编造承诺。
    """
    from app.seed_catalog import fact_clauses

    assert fact_clauses("17.5 微米美利奴羊毛，可机洗，四季通勤。") == [
        "17.5 微米美利奴羊毛",
        "可机洗",
        "四季通勤",
    ], "三个字的真卖点不能被长度过滤丢掉"

    rows = db.all_(
        """SELECT slug, description, JSON_LENGTH(JSON_EXTRACT(attributes, '$."亮点"')) AS n
           FROM products WHERE status = 'active'""",
        {},
    )
    thin = [r["slug"] for r in rows if r["n"] is None or r["n"] < 2]
    assert not thin, f"亮点不足两条的商品：{thin[:5]}"
    over = [r["slug"] for r in rows if r["n"] > 3]
    assert not over, f"亮点超过三条的商品：{over[:5]}"

    seeds = [r for r in rows if r["slug"].startswith("seed-")]
    assert all(r["n"] == 3 for r in seeds), "数据包灌出来的商品每件都要有写死的三条亮点"
    for row in rows:
        if row["n"] == 2:
            assert len(fact_clauses(row["description"])) == 2, f"{row['slug']} 只有两条亮点，导语也该只拆得出两句"


def test_guest_cannot_open_others_order(client, buyer, variant):
    """订单详情只认本人：小马给的链接也只能自己的会话打开。"""
    client.post("/api/cart/items", json={"variant_id": variant["id"], "qty": 1})
    placed = client.post(
        "/api/checkout/place", json={"contact": CONTACT, "address": ADDRESS, "coupon_code": ""}
    ).json()["data"]
    order_no = placed["order_no"]
    assert client.get(f"/order/{order_no}").status_code == 200

    async def probe():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30) as anon:
            res = await anon.get(f"/order/{order_no}", follow_redirects=True)
            api = await anon.get(f"/api/orders/{order_no}", follow_redirects=True)
            return res.status_code, api.status_code

    page_status, api_status = asyncio.new_event_loop().run_until_complete(probe())
    assert page_status == 404
    assert api_status == 404
