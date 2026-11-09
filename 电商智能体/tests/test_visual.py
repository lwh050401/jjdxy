"""按图找相似：描述子、签名库、上传接口与小马的第 10 个工具。

用例只断言「小马说出来的相似度与商品在库里查得到」，不锁死具体哪一件，
免得补图或换实拍图就把测试搞红。
"""

from __future__ import annotations

import io
import pathlib
import re

import pytest
from PIL import Image

from app import db
from app.agent import offline, shop_api, store, tools
from app.services import visual

PRODUCT_DIR = pathlib.Path("static/img/products")

_cleanup_tokens: list[str] = []
_cleanup_threads: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _signature_library():
    """整个模块共用一次建库：每张商品图只算一遍。"""
    report = visual.build()
    assert report["built"] + report["skipped"] > 0, "一张商品图都没算出来，比对功能不可能工作"
    assert report["unreadable"] == 0, f"有 {report['unreadable']} 张商品图找不到本地文件"
    return report


@pytest.fixture(scope="session", autouse=True)
def _remove_test_uploads():
    yield
    for thread in _cleanup_threads:
        db.run("DELETE FROM agent_messages WHERE thread_id = :t", {"t": thread})
        db.run("DELETE FROM agent_tool_calls WHERE thread_id = :t", {"t": thread})
        db.run("DELETE FROM agent_images WHERE thread_id = :t", {"t": thread})
        db.run("DELETE FROM agent_threads WHERE id = :t", {"t": thread})
    for token in _cleanup_tokens:
        db.run("DELETE FROM agent_images WHERE token = :t", {"t": token})
        found = visual.token_path(token)
        if found:
            found.unlink()


@pytest.fixture(autouse=True)
def _clear_chat_rate_limit():
    """传图频控是每分钟 6 次：用例各测各的场景，不该被前面那条扣满额度。"""
    from app.api import agent_api

    agent_api._hits.clear()
    yield
    agent_api._hits.clear()


def _png(width: int, height: int, color, bg=None) -> bytes:
    img = Image.new("RGB", (width, height), bg or color)
    if bg:
        box = int(min(width, height) * 0.18)
        for y in range(box, height - box):
            for x in range(box, width - box):
                img.putpixel((x, y), color)
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def _url_for(filename: str) -> str:
    return f"/img/products/{filename}"


def _products_using(filename: str) -> list[dict]:
    return db.all_(
        "SELECT p.id FROM products p JOIN product_images img ON img.product_id = p.id "
        "WHERE img.url = :u AND p.status = 'active' GROUP BY p.id",
        {"u": _url_for(filename)},
    )


def _max_bytes() -> int:
    from app.settings import get_settings

    return get_settings().chat_upload_max_bytes


def test_descriptor_reports_color_light_and_shape():
    """黑白灰的图没有彩色像素可归一化，主色直方图整条是 0，这是设计而不是漏算。"""
    path = sorted(PRODUCT_DIR.glob("*.png"))[0]
    sig = visual.descriptor(path.read_bytes())
    assert len(sig["hue"]) == visual.HUE_BINS
    if sig["chroma_share"] > 0:
        assert abs(sum(sig["hue"]) - 1.0) < 0.01, "主色直方图必须归一化，否则距离没法比"
    else:
        assert sig["hue"] == [0.0] * visual.HUE_BINS, sig["hue"]
    assert abs(sum(sig["val"]) - 1.0) < 0.01
    assert 0 <= sig["bright"] <= 1 and 0 <= sig["sat"] <= 1
    assert len(sig["dhash"]) == 16
    assert sig["color"], "描述子要能说出主色，回复里得写给用户看"
    assert sig["width"] >= 16 and sig["height"] >= 16


def test_chromatic_picture_has_a_normalized_hue_histogram():
    sig = visual.descriptor(_png(200, 200, (200, 40, 40), bg=(245, 245, 246)))
    assert sig["chroma_share"] > 0
    assert abs(sum(sig["hue"]) - 1.0) < 0.01
    assert sig["color"] == "红色系"


def test_similarity_is_one_for_the_same_picture():
    path = sorted(PRODUCT_DIR.glob("*.png"))[0]
    sig = visual.descriptor(path.read_bytes())
    assert visual.similarity(sig, sig) == pytest.approx(1.0)


def test_same_color_pictures_score_above_different_colors():
    red = visual.descriptor(_png(200, 200, (188, 40, 52), bg=(245, 245, 246)))
    red_again = visual.descriptor(_png(240, 240, (196, 44, 60), bg=(245, 245, 246)))
    teal = visual.descriptor(_png(200, 200, (26, 120, 118), bg=(245, 245, 246)))
    assert visual.similarity(red, red_again) > visual.similarity(red, teal)
    assert red["color"] != teal["color"]


def test_wine_red_is_not_called_green():
    """色相 330-360 度是玫红/酒红，标错成绿用户一眼就能看出来。"""
    sig = visual.descriptor(_png(200, 200, (122, 26, 44), bg=(246, 246, 246)))
    assert sig["color"] in {"玫红色系", "红色系", "棕色系"}


def test_background_is_cut_out_before_counting_colors():
    """棚拍图基本白底：不比本体只比背景，一百张白底图互相都挺像。"""
    plain = visual.descriptor(_png(320, 320, (246, 246, 247)))
    with_subject = visual.descriptor(_png(320, 320, (40, 44, 60), bg=(246, 246, 247)))
    assert with_subject["fg_share"] <= plain["fg_share"]


@pytest.mark.parametrize("size", [(8, 8), (12, 400)])
def test_descriptor_rejects_unusable_pictures(size):
    with pytest.raises(ValueError):
        visual.descriptor(_png(size[0], size[1], (200, 60, 60)))


def test_descriptor_rejects_non_images():
    with pytest.raises(ValueError):
        visual.descriptor("这不是图片，只是一段文字".encode("utf-8"))


def test_every_product_image_has_a_signature():
    urls = {
        r["url"]
        for r in db.all_(
            "SELECT DISTINCT img.url FROM product_images img JOIN products p ON p.id = img.product_id "
            "WHERE p.status = 'active'"
        )
    }
    cached = {r["url"] for r in db.all_("SELECT url FROM product_image_signatures")}
    missing = sorted(urls - cached)
    assert not missing, f"这些商品图没有描述子：{missing[:3]}"
    assert len(cached) >= 30, "比对库太小，相似结果没法看"


def test_build_skips_cached_pictures():
    report = visual.build()
    assert report["built"] == 0, "第二次建库不该重算，只补新图"
    assert report["skipped"] >= 30


def test_search_returns_buyable_products():
    path = next(p for p in sorted(PRODUCT_DIR.glob("*.png")) if visual.image_path(_url_for(p.name)))
    result = visual.search(visual.descriptor(path.read_bytes()), limit=5)
    assert result["found"], result.get("message")
    assert 1 <= len(result["items"]) <= 5
    for item in result["items"]:
        assert item["slug"], "服务层给的是商品行，链接由工具层拼"
        assert item["image"], "卡片不能没有实物图"
        assert item["price_cents"] > 0
        assert item["available"] > 0, "找相似不能推售罄的"
        assert 0 < item["similarity"] <= 1
    assert result["best_score"] > 0
    assert any("没有款式、品牌或材质的图像识别" in note for note in result["notes"])


def test_tool_cards_link_to_the_product_page(uploaded):
    """工具层要拼出能点的详情页链接，并带上前台角标用的整数相似度。"""
    result = shop_api.find_similar_by_image(image_token=uploaded, limit=5)
    assert result["found"], result.get("message")
    for card in result["items"]:
        assert card["url"] == f"/product/{card['slug']}"
        assert 0 < card["similarity_pct"] <= 100


def test_one_picture_does_not_flood_the_results():
    """一张实拍图挂着好几件商品，同图最多出两件，否则五条结果全是同一个款。"""
    path = next(p for p in sorted(PRODUCT_DIR.glob("*.png")) if len(_products_using(p.name)) > 3)
    result = visual.search(visual.descriptor(path.read_bytes()), limit=6)
    per_image: dict[str, int] = {}
    for item in result["items"]:
        per_image[item["image"]] = per_image.get(item["image"], 0) + 1
    assert max(per_image.values()) <= 2, f"{path.name} 一次出了 {max(per_image.values())} 件"


def test_search_can_narrow_by_price():
    path = sorted(PRODUCT_DIR.glob("*.png"))[0]
    cheap = visual.search(visual.descriptor(path.read_bytes()), limit=5, max_price_yuan=300)
    if cheap["found"]:
        assert all(i["price_cents"] <= 30000 for i in cheap["items"])


def test_similar_by_token_needs_a_real_upload():
    missing = visual.similar_by_token("img_0000000000000000")
    assert not missing["found"]
    assert "请重新上传" in missing["message"]
    assert not visual.similar_by_token("")["found"]


def test_path_look_alikes_are_rejected():
    assert visual.token_path("../../app/settings") is None
    assert visual.token_path("img_%2e%2e%2f") is None
    assert visual.token_path("") is None


@pytest.fixture
def uploaded(client):
    path = sorted(PRODUCT_DIR.glob("*.png"))[0]
    res = client.post("/api/agent/images", files={"file": (path.name, path.read_bytes(), "image/png")})
    assert res.status_code == 200, res.text
    token = res.json()["data"]["token"]
    _cleanup_tokens.append(token)
    return token


def test_upload_accepts_a_real_picture(client, uploaded):
    res = client.post(
        "/api/agent/images",
        files={"file": ("again.png", sorted(PRODUCT_DIR.glob("*.png"))[1].read_bytes(), "image/png")},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["token"].startswith("img_")
    assert data["color"], "要能告诉用户这张图被判成了什么色系"
    preview = client.get(data["url"])
    assert preview.status_code == 200, f"{data['url']} 预览打不开"
    assert len(preview.content) > 1000
    _cleanup_tokens.append(data["token"])


def test_upload_rejects_non_images(client):
    res = client.post("/api/agent/images", files={"file": ("说明.txt", b"hello world", "text/plain")})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_image_type"


def test_upload_rejects_oversize(client):
    big = b"\x00" * (_max_bytes() + 10)
    res = client.post("/api/agent/images", files={"file": ("big.png", big, "image/png")})
    assert res.status_code == 413
    assert "MB" in res.json()["error"]["message"]


def test_upload_rejects_broken_picture(client):
    res = client.post("/api/agent/images", files={"file": ("坏图.png", b"not an image at all", "image/png")})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "bad_image"


def _claims_same(text: str) -> str | None:
    """只抓肯定说法，否定词在整句里找：「不代表是同一款或同款商品」的否定离「同款」六个字开外，
    只看紧邻窗口会把站内想让用户看到的老实话判成违规。"""
    for sentence in re.split(r"[。！？；\n]", text or ""):
        for match in re.finditer(r"同款|一模一样|同一件|识别出", sentence):
            head = sentence[: match.start()]
            if not any(word in head for word in ("不", "没", "非", "别", "未", "难", "无法")):
                return match.group(0)
    return None


def test_honest_wording_rule_tells_apart_yes_and_no():
    """这条规则本身要能被测：漏判等于放任假承诺，误判等于把老实回答打成违规。"""
    assert _claims_same("这两件就是同款，直接下单吧") == "同款"
    assert _claims_same("找到了同款商品") == "同款"
    for honest in (
        "这个相似度只算「接近」，不算同款",
        "站内只比主色、明暗、轮廓和边缘，不代表是同一款或同款商品。",
        "认不出款式、品牌、材质，不能说这就是同一件",
    ):
        assert _claims_same(honest) is None, honest


def test_picture_only_question_matches_the_backend():
    """只发图时补的那句话前后端各写了一份：对不上，用户气泡里看到的和后台复盘记的就不是同一句。"""
    from app.api import agent_api

    js = pathlib.Path("static/js/site.js").read_text(encoding="utf-8")
    assert f"const PICTURE_ONLY_QUESTION = '{agent_api.IMAGE_ONLY_QUESTION}'" in js


def test_offline_router_uses_the_picture(uploaded):
    plan = offline.run("", image_token=uploaded)
    assert plan["intent"] == "visual_search"
    assert plan["tool_calls"][0]["tool"] == "find_similar_by_image"
    assert plan["cards"], "按图找相似必须给出可点的商品卡片"
    assert "相似度" in plan["reply"]
    assert not _claims_same(plan["reply"]), f"文案断言了同款，站内没这个能力：{plan['reply'][:80]}"
    assert plan["cards"][0]["url"].startswith("/product/")


def test_offline_text_only_narrows_the_picture_search(uploaded):
    """传了图又说「300 以内的衬衫」，图定颜色轮廓，文字定品类和价位。"""
    plan = offline.run("有没有 300 以内的衬衫", image_token=uploaded)
    args = plan["tool_calls"][0]["args"]
    assert args["image_token"] == uploaded
    assert plan["tool_calls"][0]["count"] > 0


def test_messages_endpoint_accepts_an_image_token(client, uploaded):
    res = client.post("/api/agent/messages", json={"message": "", "image_token": uploaded})
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["intent"] == "visual_search"
    assert data["cards"]
    assert any(c["tool"] == "find_similar_by_image" for c in data["tool_calls"])
    assert data["tool_calls"][0]["label"] == "按图找相似"
    _cleanup_threads.append(data["thread_id"])
    row = db.get("SELECT thread_id FROM agent_images WHERE token = :t", {"t": uploaded})
    assert row["thread_id"] == data["thread_id"], "上传的图要挂到会话上，后台复盘才看得到"
    # 后台会话详情用的就是这条查询：按会话能把用户传过的图取回来
    listed = db.all_(
        "SELECT token, url, filename, mime, bytes, width, height, created_at FROM agent_images "
        "WHERE thread_id = :tid ORDER BY id",
        {"tid": data["thread_id"]},
    )
    assert [r["token"] for r in listed] == [uploaded]
    assert listed[0]["url"].startswith("/img/chat/") and listed[0]["width"] >= 16


def test_tool_is_registered_and_labelled():
    names = [t.name for t in tools.ALL_TOOLS]
    assert "find_similar_by_image" in names
    assert "find_similar_by_image" in [t.name for t in tools.CORE_TOOLS]
    assert store.tool_text("find_similar_by_image") == "按图找相似"
    assert store.INTENT_TEXT["visual_search"] == "按图找相似"
    assert tools.OFFLINE_HANDLERS["find_similar_by_image"] is shop_api.find_similar_by_image


def test_tool_without_a_picture_says_so():
    data = shop_api.find_similar_by_image(image_token="", limit=5)
    assert tools.result_count(data) == 0
    assert "请先在聊天窗" in data["message"]


def test_state_advertises_the_real_upload_limits(client):
    upload = client.get("/api/agent/state").json()["data"]["upload"]
    assert "image/png" in upload["accept"]
    assert upload["max_bytes"] == _max_bytes()
    assert "find_similar_by_image" in client.get("/api/agent/state").json()["data"]["tools"]
