"""生成「品名 → 实物图」清单与缺口表。

规则只有一条：一张图只能挂在它真的画着的那个品名上。
- 已有品名专属图（场合装的 dress-1、boot-1，旧商城的 apparel-17 等）→ 写进 name_images.json；
- 还没有图的品名 → 写进 name_image_todo.json，带上确定性的目标文件名与 ImageGen 提示词。

文件名用「分类 slug + 品名在该分类里的序号」，序号只增不改，所以清单一旦落定就不会串位。
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.seed_catalog import IMAGE_DIR, NAME_FILES, CATALOG  # noqa: E402

RUN = pathlib.Path(__file__).resolve().parent
MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "app" / "seed_data" / "name_images.json"
TODO = RUN / "name_image_todo.json"

FILES = {p.stem for p in IMAGE_DIR.glob("*.png")}


def keyword_hit(name: str) -> str | None:
    """按 NAME_FILES 的关键词规则找一张「画的就是这个东西」的图。

    命中多个关键词时取最长的那个，避免「连衣裙」抢走「亮片派对连衣裙」的图。
    """
    tokens = [token for token in NAME_FILES if token in name]
    if not tokens:
        return None
    for token in sorted(tokens, key=len, reverse=True):
        for key in NAME_FILES[token]:
            if key in FILES:
                return f"{key}.png"
            variants = sorted(f for f in FILES if f.startswith(f"{key}-"))
            if variants:
                return f"{variants[0]}.png"
    return None


def prompt_for(slug: str, name: str, facts: list[str]) -> str:
    detail = "，".join(facts[:2]) if facts else ""
    return (
        f"电商主图：{name}的实物照片，白底，居中，柔和投影，细节清晰。{detail}。"
        "画面里不出现任何文字、字母、数字、品牌标志或水印，只有这件商品本身。"
    )


def main() -> int:
    manifest: dict[str, str] = {}
    todo: list[dict] = []
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    for slug, spec in CATALOG.items():
        for idx, (name, facts) in enumerate(spec["facts"].items(), start=1):
            hit = manifest.get(name) or keyword_hit(name)
            if hit and hit[:-4] in FILES:
                manifest[name] = hit
                continue
            target = f"{slug}-{idx:03d}.png"
            if target[:-4] in FILES:
                manifest[name] = target
                continue
            todo.append({
                "name": name,
                "category": slug,
                "file": target,
                "prompt": prompt_for(slug, name, list(facts or [])),
            })

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    TODO.write_text(json.dumps(todo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"品名总数 {len(manifest) + len(todo)}：已有同物图 {len(manifest)} 个，待补图 {len(todo)} 个")
    print(f"清单：{MANIFEST.relative_to(MANIFEST.parents[2])}")
    print(f"缺口表：{TODO.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
