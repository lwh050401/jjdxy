"""分批补实物图：给出下一批要画什么，再把画好的图收进素材目录。

用法：
    python -X utf8 .run/name_image_batch.py --count 20        # 打印下一批提示词
    python -X utf8 .run/name_image_batch.py --adopt <目录>     # 把 <目标名>.png 收进 static/img/products

一张图只挂它真的画着的那个品名，所以这里宁可慢慢补，也不退回「整类共用一张代表照」。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.seed_catalog import IMAGE_DIR  # noqa: E402

TODO = ROOT / ".run" / "name_image_todo.json"
GEN_SUFFIX = re.compile(r"_\d+_[0-9a-fA-F]+$")


def pending() -> list[dict]:
    return [t for t in json.loads(TODO.read_text(encoding="utf-8")) if not (IMAGE_DIR / t["file"]).exists()]


def show(count: int) -> int:
    items = pending()[:count]
    for t in items:
        stem = t["file"].rsplit(".", 1)[0]
        print(json.dumps({"name": stem, "品名": t["name"], "prompt": t["prompt"]}, ensure_ascii=False))
    print(f"# 还差 {len(items)}/{len(pending())} 张，收图后用 --adopt")
    return 0


def put(path: pathlib.Path, target: pathlib.Path) -> None:
    """收图时压到 800x1000：卡片位只有 300px 宽，原图 1MB 一张会让列表页动辄几十 MB。"""
    from PIL import Image

    with Image.open(path) as im:
        im.thumbnail((800, 1000), Image.LANCZOS)
        im.convert("RGB").save(target, "PNG", optimize=True)


def adopt(source: pathlib.Path) -> int:
    # ImageGen 存成 <目标名>_<时间戳>_<hash>.png；目标名自己可能带下划线（travel_gear-001），只能砍掉尾部两段
    files: dict[str, pathlib.Path] = {}
    if source.is_dir():
        for p in sorted(source.glob("*.png")):
            if p.is_file():
                files[GEN_SUFFIX.sub("", p.stem).lower()] = p
    if not files:
        print(f"{source} 里没有 png 可收")
        return 1
    by_stem = {t["file"].rsplit(".", 1)[0].lower(): t for t in pending()}
    moved = skipped = 0
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    for stem, path in files.items():
        target = by_stem.get(stem)
        if target is None:
            skipped += 1
            continue
        try:
            put(path, IMAGE_DIR / target["file"])
        except Exception as err:  # 目录里可能正有图在写，跳过，下一批再收
            print(f"跳过 {path.name}：{err}")
            continue
        path.unlink()  # 收一张删一张，避免下次重复扫到
        moved += 1
        print(f"收图 {target['name']} -> {target['file']}")
    print(f"收进 {moved} 张，忽略无名可对的 {skipped} 张，还差 {len(pending()) - moved} 张")
    if moved:
        print("接着跑：python -X utf8 .run/build_name_images.py && python -X utf8 -m app.seed_catalog --rebind-images")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--adopt", type=pathlib.Path)
    args = ap.parse_args()
    raise SystemExit(adopt(args.adopt) if args.adopt else show(args.count))
