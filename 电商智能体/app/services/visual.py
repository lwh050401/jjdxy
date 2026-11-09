"""按图找相似：把图片压成一份「颜色 + 明暗 + 轮廓」描述子，再和站内商品实物图逐张比。

小马接的是 DeepSeek，只看文字看不到图，所以像不像不能靠大模型说，得在这里现算。
返回给用户看的依据也只有主色、明暗、轮廓密度这几项真实算出来的东西，
不写「识别出这是某品牌某款式」——站内没有这种能力。
"""

from __future__ import annotations

import io
import json
import math
import pathlib
import re

from PIL import Image, ImageFilter, ImageOps

from .. import db
from ..settings import get_settings

SIGNATURE_VERSION = 1
SIDE = 32  # 描述子统一缩到 32x32 再统计，原图尺寸不该影响颜色结果
HASH_BOX = (9, 8)  # 差感受 hash：相邻像素谁更亮，得 64 位
HUE_BINS = 12
VAL_BINS = 5
SAT_CUTOFF = 0.16  # 饱和度低于这个值算无彩色，只进明暗直方图
BG_CUTOFF = 42  # 与背景底色三通道差值和超过这条线才算商品本体
MAX_PIXELS = 40_000_000  # 比这大的图直接拒，防止解压炸弹
ACCEPTED_KINDS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
# 相似度低于这条线就不给结果了：实测一张不像商品的图对站内实拍图最高也只有 0.6 上下
WEAK_MATCH = 0.70  # 低于这条线时回复里要照实说「只是接近，不算同款」
LOW_MATCH = 0.58
# PIL 的色相 0-255 对应 0-360 度，12 个 bins 就是每格 30 度：
# 330-360 度回到玫红/红，别再往下标成绿，否则酒红色会报成「绿色系」
HUE_NAMES = ["红", "橙", "黄", "绿", "绿", "青", "青", "蓝", "蓝", "紫", "紫", "玫红"]


def upload_root() -> pathlib.Path:
    return pathlib.Path(get_settings().chat_upload_dir)


def upload_url_prefix() -> str:
    return str(get_settings().chat_upload_prefix).strip("/")


def _quantize(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _hue_word(sig: dict) -> str:
    """把主色说成一个人话词：黑、白、灰、棕或某个色系名。"""
    if sig["chroma_share"] < 0.22:
        if sig["bright"] < 0.24:
            return "黑色系"
        if sig["bright"] > 0.82:
            return "白色系"
        return "灰色系"
    top = max(range(HUE_BINS), key=lambda i: sig["hue"][i])
    # 皮件、皮鞋这种暗处的红橙色说成「棕色系」才对得上人的说法
    if top in (0, 1, 11) and sig["bright"] < 0.52 and sig["sat"] < 0.62:
        return "棕色系"
    return HUE_NAMES[top] + "色系"


def _background_color(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    """拿四边框像素的中位色当背景色：站内商品图几乎都是纯色底。"""
    ring = [p for i, p in enumerate(pixels) if i % SIDE < 2 or i // SIDE < 2 or i % SIDE >= SIDE - 2 or i // SIDE >= SIDE - 2]
    ring = ring or pixels
    return tuple(sorted(channel)[len(channel) // 2] for channel in zip(*ring))


def _distance(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def descriptor(data: bytes) -> dict:
    """从图片字节算描述子；不是能认的图或太大就抛 ValueError。"""
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as err:
        raise ValueError(f"读不出这张图：{type(err).__name__}") from err
    if img.width * img.height > MAX_PIXELS:
        raise ValueError("图片像素太大，请压缩后再上传")
    if img.width < 16 or img.height < 16:
        raise ValueError("图片太小，看不出颜色和轮廓")

    rgb = ImageOps.exif_transpose(img).convert("RGB")
    width, height = img.width, img.height
    small = rgb.resize((SIDE, SIDE), Image.BOX)
    pixels = list(small.getdata())
    total = len(pixels)
    hsv_data = list(small.convert("HSV").getdata())

    # 棚拍图基本都是白底：先把和底色一致的像素剔掉再统计颜色，
    # 否则比的是背景，一百张白底图互相都挺像
    bg = _background_color(pixels)
    keep = [i for i, p in enumerate(pixels) if _distance(p, bg) > BG_CUTOFF]
    if len(keep) < total * 0.06:
        keep = list(range(total))  # 商品铺满整帧，没有背景可剔

    hue = [0.0] * HUE_BINS
    val = [0.0] * VAL_BINS
    chroma = 0
    red_sum = green_sum = blue_sum = 0
    sat_sum = 0.0
    val_sum = 0.0
    for i in keep:
        r, g, b = pixels[i]
        h, s, v = hsv_data[i]
        red_sum += r
        green_sum += g
        blue_sum += b
        sat_sum += s
        val_sum += v
        if s > SAT_CUTOFF * 255:
            chroma += 1
            hue[min(HUE_BINS - 1, h * HUE_BINS // 256)] += 1.0
        val[min(VAL_BINS - 1, v * VAL_BINS // 256)] += 1.0

    seen = len(keep)
    hue = [_quantize(x / chroma) if chroma else 0.0 for x in hue]
    val = [_quantize(x / seen) for x in val]
    mean_rgb = [red_sum // seen, green_sum // seen, blue_sum // seen]
    bright = val_sum / seen / 255

    gray = rgb.convert("L")
    box = gray.resize(HASH_BOX, Image.BOX)
    bits = list(box.getdata())
    hash_value = 0
    for row in range(HASH_BOX[1]):
        for col in range(HASH_BOX[0] - 1):
            left = bits[row * HASH_BOX[0] + col]
            right = bits[row * HASH_BOX[0] + col + 1]
            hash_value = (hash_value << 1) | (1 if left > right else 0)

    edges = gray.filter(ImageFilter.FIND_EDGES).resize((SIDE, SIDE), Image.BOX)
    edge_mean = sum(edges.getdata()) / (255.0 * total)
    sig = {
        "v": SIGNATURE_VERSION,
        "width": width,
        "height": height,
        "aspect": _quantize(width / height, 3),
        "hue": hue,
        "val": val,
        "rgb": mean_rgb,
        "sat": _quantize(sat_sum / seen / 255),
        "bright": _quantize(bright),
        "chroma_share": _quantize(chroma / seen),
        "fg_share": _quantize(seen / total),
        "edge": _quantize(min(1.0, edge_mean * 3)),
        "dhash": f"{hash_value:016x}",
        "color": _hue_word({"hue": hue, "chroma_share": chroma / seen, "bright": bright, "sat": sat_sum / seen / 255}),
    }
    return sig


def _loads(sig) -> dict:
    if isinstance(sig, dict):
        return sig
    try:
        parsed = json.loads(sig or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) and parsed.get("v") == SIGNATURE_VERSION else {}


def _l1(a: list[float], b: list[float]) -> float:
    """两个归一化直方图的距离：0 完全重合，1 完全不沾边。"""
    return min(1.0, sum(abs(x - y) for x, y in zip(a, b)) / 2.0)


def _hamming(a: str, b: str) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1") / 64.0
    except ValueError:
        return 1.0


def similarity(a: dict, b: dict) -> float:
    """0 到 1 的相似度：主色占大头，其次是明暗分布、平均色、轮廓形状、边缘密度。"""
    if not a or not b:
        return 0.0
    chroma = 1.0 - _l1(a["hue"], b["hue"])
    tone = 1.0 - _l1(a["val"], b["val"])
    distance = math.sqrt(sum((x - y) ** 2 for x, y in zip(a["rgb"], b["rgb"]))) / (255 * math.sqrt(3))
    shape = 1.0 - _hamming(a["dhash"], b["dhash"])
    detail = 1.0 - min(1.0, abs(a["edge"] - b["edge"]) * 2)
    return _quantize(0.32 * chroma + 0.20 * tone + 0.16 * (1.0 - distance) + 0.20 * shape + 0.12 * detail)


def image_path(url: str) -> pathlib.Path | None:
    """把站内图片 URL 换成本地文件，只认我们自己挂载的这两个前缀。"""
    text = str(url or "")
    static = pathlib.Path(get_settings().static_dir)
    for prefix, root in (("/img/products/", static / "img" / "products"), ("/static/img/", static / "img")):
        if text.startswith(prefix):
            candidate = root / text[len(prefix) :]
            if candidate.is_file() and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                return candidate
    return None


def build(force: bool = False) -> dict:
    """给全站商品实物图算描述子并缓存；每张图只算一次，重复调用只补新图。"""
    rows = db.all_(
        """SELECT img.url, COUNT(DISTINCT img.product_id) AS products FROM product_images img
           JOIN products p ON p.id = img.product_id AND p.status = 'active'
           GROUP BY img.url"""
    )
    built = skipped = unreadable = 0
    for row in rows:
        url = str(row["url"] or "")
        if not force and db.value("SELECT url FROM product_image_signatures WHERE url = :u", {"u": url}) == url:
            skipped += 1
            continue
        path = image_path(url)
        if path is None:
            unreadable += 1
            continue
        sig = descriptor(path.read_bytes())
        db.run(
            """INSERT INTO product_image_signatures (url, sig, pixels, products, built_at)
               VALUES (:u, :s, :px, :n, :at)
               ON DUPLICATE KEY UPDATE sig = :s, pixels = :px, products = :n, built_at = :at""",
            {
                "u": url,
                "s": json.dumps(sig, ensure_ascii=False),
                "px": sig["width"] * sig["height"],
                "n": int(row["products"]),
                "at": db.now_iso(),
            },
        )
        built += 1
    return {"images": len(rows), "built": built, "skipped": skipped, "unreadable": unreadable}


def library(*, refresh: bool = False) -> list[dict]:
    """读缓存好的商品图描述子；空表时先补一次，避免功能依赖「有人记得跑脚本」。"""
    rows = db.all_("SELECT url, sig, products FROM product_image_signatures")
    if not rows or refresh:
        build()
        rows = db.all_("SELECT url, sig, products FROM product_image_signatures")
    out = []
    for row in rows:
        sig = _loads(row.get("sig"))
        if sig:
            out.append({"url": row["url"], "sig": sig, "products": int(row.get("products") or 0)})
    return out


def compare(sig: dict) -> list[dict]:
    """按相似度排站内全部商品图，返回 [{url, score, products}]。"""
    ranked = []
    for entry in library():
        score = similarity(sig, entry["sig"])
        if score > 0:
            ranked.append({"url": entry["url"], "score": score, "products": entry["products"]})
    ranked.sort(key=lambda r: -r["score"])
    return ranked


def _products_for(ranked: list[dict], *, category: str, max_cents: int | None, limit: int) -> list[dict]:
    """把命中的图片落成商品：同一张图最多取 2 件，避免一张图占满结果。"""
    from . import catalog

    urls = [r["url"] for r in ranked]
    scores = {r["url"]: r["score"] for r in ranked}
    if not urls:
        return []
    placeholders = ", ".join(f":u{i}" for i in range(len(urls)))
    params: dict = {f"u{i}": url for i, url in enumerate(urls)}
    where = f"p.status = 'active' AND img.url IN ({placeholders}) AND price.available > 0"
    if category:
        where += " AND (c.slug = :category OR c.name = :category)"
        params["category"] = category
    if max_cents:
        where += " AND price.min_cents <= :max_cents"
        params["max_cents"] = int(max_cents)
    # FIELD() 保住相似度先后：MySQL 自己只会说「同一张图里谁卖得好」
    rows = db.all_(
        f"""{catalog.BASE_SELECT} WHERE {where}
            ORDER BY FIELD(img.url, {placeholders}), p.rating_avg DESC, p.sold_count DESC, p.id ASC
            LIMIT :cap""",
        {**params, "cap": min(60, max(6, limit * 4))},
    )
    per_url: dict[str, int] = {}
    picked: list[dict] = []
    for row in rows:
        url = row.get("image_url") or ""
        if per_url.get(url, 0) >= 2:
            continue  # 一张实物图最多带出两件，否则结果会被同一个款刷满
        per_url[url] = per_url.get(url, 0) + 1
        product = catalog.decorate(row)
        product["similarity"] = scores.get(url, 0.0)
        picked.append(product)
        if len(picked) >= limit:
            break
    return picked


def search(
    sig: dict,
    *,
    limit: int = 6,
    category: str = "",
    max_price_yuan: float | None = None,
) -> dict:
    """拿一张图的描述子在站内找最接近的实物图，再落成可点的商品卡片。"""
    if not sig:
        return {"found": False, "message": "没有拿到图片的描述子，请重新上传一张。"}
    ranked = compare(sig)
    if not ranked:
        return {"found": False, "message": "站内商品图的比对库还没建好，稍等一下再问。"}
    best = ranked[0]
    usable = [r for r in ranked if r["score"] >= LOW_MATCH][: max(8, limit * 3)]
    if not usable:
        return {
            "found": False,
            "best_score": best["score"],
            "message": f"站内 {len(ranked)} 张商品实物图里，最接近的也只有 {int(best['score'] * 100)}%，"
            "颜色和轮廓都对不上。可以说个品类词（比如衬衫、乐福鞋）让我按文字找。",
        }
    items = _products_for(usable, category=category, max_cents=int(round(float(max_price_yuan) * 100)) if max_price_yuan else None, limit=limit)
    if not items:
        return {
            "found": False,
            "best_score": best["score"],
            "message": "找到颜色轮廓接近的实物图，但那几张图上的商品目前都没现货。"
            + (f"去掉「{category}」这个限定再试一次。" if category else ""),
        }
    notes = [
        f"你这张图偏{sig['color']}，整体明度 {int(sig['bright'] * 100)}%，宽高比 {sig['aspect']}:1。",
        f"站内 {len(ranked)} 张商品实物图全部比过，最像的一张相似度 {int(best['score'] * 100)}%。",
    ]
    if best["score"] < WEAK_MATCH:
        notes.append("这个相似度只算「接近」，不算同款；下面的结果按颜色与轮廓排，买之前看图和参数。")
    notes.append("相似只比主色、明暗、轮廓和边缘，站内没有款式、品牌或材质的图像识别。")
    return {
        "found": True,
        "count": len(items),
        "items": items,
        "reference": {"color": sig["color"], "bright": sig["bright"], "aspect": sig["aspect"], "size": [sig["width"], sig["height"]]},
        "best_score": best["score"],
        "matched_images": [{"url": r["url"], "score": r["score"], "products": r["products"]} for r in usable[:3]],
        "reason": f"按主色（{sig['color']}）、明暗与轮廓比过站内商品实物图，最接近的现货 {len(items)} 件",
        "notes": notes,
    }


def token_path(token: str) -> pathlib.Path | None:
    """按编号找回上传的文件：编号是我们自己生成的，只允许这种形态，避免拼路径读别处。"""
    name = str(token or "")
    if not re.fullmatch(r"[a-z]{3}_[0-9a-f]{16}", name):
        return None
    for file in upload_root().glob(f"{name}.*"):
        return file
    return None


def record(token: str, data: bytes, *, mime: str, filename: str, thread_id: str | None, sig: dict) -> dict:
    """上传的图落盘 + 落库：文件给聊天窗做预览，描述子给比对用。"""
    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    stored = root / f"{token}.{ACCEPTED_KINDS.get(mime, 'png')}"
    stored.write_bytes(data)
    db.insert(
        "agent_images",
        {
            "token": token,
            "thread_id": thread_id,
            "filename": str(filename or "")[:200],
            "mime": mime,
            "bytes": len(data),
            "width": sig["width"],
            "height": sig["height"],
            "sig": sig,
            "url": f"/{upload_url_prefix()}/{stored.name}",
            "created_at": db.now_iso(),
        },
    )
    row = db.get("SELECT * FROM agent_images WHERE token = :t", {"t": token})
    return {**(row or {}), "sig": sig}


def by_token(token: str) -> dict | None:
    row = db.get("SELECT token, url, mime, bytes, width, height, sig, created_at FROM agent_images WHERE token = :t", {"t": str(token or "")})
    if not row:
        return None
    row["descriptor"] = _loads(row.get("sig"))
    return row


def similar_by_token(
    token: str,
    *,
    limit: int = 6,
    category: str = "",
    max_price_yuan: float | None = None,
) -> dict:
    """小马工具走这里：按会话里上传过的图片编号找相似。"""
    row = by_token(token)
    if not row:
        return {"found": False, "message": f"没有编号 {token} 的图片，请重新上传一张再问。"}
    if not row["descriptor"]:
        return {"found": False, "message": "这张图的描述子读不出来，请重新上传一张。"}
    result = search(row["descriptor"], limit=limit, category=category, max_price_yuan=max_price_yuan)
    return {**result, "image_token": token, "image_url": row["url"]}


def main() -> int:
    """python -m app.services.visual --build：给全站商品实物图建描述子缓存。"""
    import argparse

    parser = argparse.ArgumentParser(description="按图找相似的描述子工具")
    parser.add_argument("--build", action="store_true", help="算一遍全站商品图描述子")
    parser.add_argument("--force", action="store_true", help="已有缓存也重算")
    parser.add_argument("--image", default="", help="给本地图片或站内 URL 算描述子并列出最像的三张")
    args = parser.parse_args()

    if args.build or args.force:
        report = build(force=args.force)
        print(f"商品图 {report['images']} 张：新算 {report['built']}，跳过 {report['skipped']}，读不到文件 {report['unreadable']}")
    if args.image:
        raw = str(args.image).replace("\\", "/")
        data = None
        for candidate in (pathlib.Path(raw), pathlib.Path(f"static/{raw}"), image_path(raw), image_path(f"/{raw}")):
            if candidate is not None and candidate.is_file():
                data = candidate.read_bytes()
                print("读的是", candidate)
                break
        if data is None:
            print(f"读不到「{args.image}」：给本地图片路径，或站内图片 URL（/img/products/xxx.png）")
            return 1
        sig = descriptor(data)
        print(json.dumps(sig, ensure_ascii=False, indent=2))
        for hit in compare(sig)[:3]:
            print(f"  {int(hit['score'] * 100)}%  {hit['url']}（{hit['products']} 件商品用这张图）")
    if not (args.build or args.force or args.image):
        print("用法：python -m app.services.visual --build [--force] | --image 图片路径")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
