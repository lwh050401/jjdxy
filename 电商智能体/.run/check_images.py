"""货不对版门禁：商品挂的图必须是它自己品名的那张实物图。

以前这里出过事——毛巾套装挂着空气净化器的照片，因为分配逻辑会退到「整类共用一张代表照」。
现在只认 name_images.json 的品名级映射，配不到就留空给前台显示占位卡。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.seed_catalog import IMAGE_DIR, name_images  # noqa: E402


def main() -> int:
    manifest = name_images()
    missing_file = [n for n, f in manifest.items() if not (IMAGE_DIR / f).exists()]
    if missing_file:
        print(f"清单里 {len(missing_file)} 个品名指向不存在的文件：{missing_file[:5]}")
        return 1

    rows = db.all_("""SELECT p.id, p.slug, p.title, p.attributes AS a, c.slug AS cat,
                             i.url AS url, (SELECT COUNT(*) FROM product_images x WHERE x.product_id = p.id) AS shots
                      FROM products p JOIN categories c ON c.id = p.category_id
                      LEFT JOIN product_images i ON i.product_id = p.id AND i.sort = 0""")
    dup = wrong = orphans = nophoto = 0
    for r in rows:
        attrs = r["a"] if isinstance(r["a"], dict) else (json.loads(r["a"] or "{}"))
        shots = int(r["shots"] or 0)
        if r["url"] and not (IMAGE_DIR / pathlib.PurePosixPath(r["url"]).name).exists():
            orphans += 1
        if not r["slug"].startswith("seed-"):
            continue
        if shots > 1:
            dup += 1
        if not r["url"]:
            nophoto += 1
            continue
        want = manifest.get(str(attrs.get("品类") or ""))
        if want and r["url"] != f"/img/products/{want}":
            wrong += 1
            if wrong <= 5:
                print(f"错版：{r['title']}（品类 {attrs.get('品类')}）挂的是 {r['url']}，应为 /img/products/{want}")

    print(f"商品 {len(rows)} 件：多图 {dup}、图文件缺失 {orphans}、挂错品名 {wrong}、演示商品无图（走占位卡）{nophoto}")
    return 1 if (dup or orphans or wrong) else 0


if __name__ == "__main__":
    raise SystemExit(main())
