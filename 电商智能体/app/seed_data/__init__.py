"""新品类数据包：一个模块管几类商品，`seed_catalog` 导入时合并进 CATALOG。

为什么要拆包：主文件 `app/seed_catalog.py` 只放生成逻辑，商品词表越加越多，
混在一个文件里改一次要滚动几千行，多个品类并行补货也会撞在同一处。

字段名是契约，不要改（合并与验收脚本按这些名字读）：

- `CATEGORY_META[slug]`：分类怎么在站里露脸
    {"name": 中文名, "description": 一句话, "icon": 一个 ASCII 词, "sort": 排序, "parent": 父分类 slug 或 None}
- `CATALOG_EXTRA[slug]`：这一类的商品词表，结构与 `seed_catalog.CATALOG[slug]` 完全一致
    {"price": (下限元, 上限元), "options": [(组名, [值...]), ...], "scenes": [...], "tags": [...],
     "facts": {品名: [三条卖点]}, "by_name": {品名: [(组名, [值...]), ...]}}
  `by_name` 可选：同一类里鞋要鞋码、锅要口径时用，优先级高于 `options`。
- `NAMES_EXTRA[已有分类 slug]`：往站内已有的分类补品名，值就是 {品名: [三条卖点]}。
- `OUTFIT_EXTRA[品名]`：可穿戴的品名补穿搭属性，字段照 `seed_catalog.OUTFIT` 的写法。
- `OPTION_RULES_EXTRA[已有分类 slug]`：往 `seed_catalog.OPTION_RULES` 追加关键词规格规则，
  值就是 `[((品名子串, ...), [(规格组名, [取值...])), ...])]`。往 `fashion` 补「登山鞋」这类
  新品名时用，否则它会落到分类默认的 S/M/L 尺码上；追加的规则排在已有规则之后，不抢已有品名。

合并时撞车直接报错而不是静默覆盖：同一个 slug 或同一个品名出现在两个模块，
多半是两边都在补同一批需求，静默赢的那个会把另一边的商品吞掉。
"""

from __future__ import annotations

import importlib
import pkgutil

CATEGORY_META: dict[str, dict] = {}
CATALOG_EXTRA: dict[str, dict] = {}
NAMES_EXTRA: dict[str, dict[str, list[str]]] = {}
OUTFIT_EXTRA: dict[str, dict] = {}
OPTION_RULES_EXTRA: dict[str, list] = {}
# 某个模块自己写坏了不要连累别的模块：错误记在这里，由 lint 与 seed_catalog 各自决定要不要拦
LOAD_ERRORS: dict[str, str] = {}

_REQUIRED = ("name", "description", "icon", "sort")


def _merge(slug: str, value: dict, source: str, into: dict, what: str) -> None:
    if slug in into and into[slug] != value:
        raise ValueError(f"{what}「{slug}」在 {source} 里重复定义且内容不一致")
    into[slug] = value


def _read(module) -> None:
    for slug, meta in getattr(module, "CATEGORY_META", {}).items():
        missing = [k for k in _REQUIRED if k not in meta]
        if missing:
            raise ValueError(f"{module.__name__} 分类「{slug}」缺字段 {missing}")
        _merge(slug, meta, module.__name__, CATEGORY_META, "CATEGORY_META")
    for slug, spec in getattr(module, "CATALOG_EXTRA", {}).items():
        missing = [k for k in ("price", "options", "scenes", "tags", "facts") if k not in spec]
        if missing:
            raise ValueError(f"{module.__name__} 分类「{slug}」缺字段 {missing}")
        _merge(slug, spec, module.__name__, CATALOG_EXTRA, "CATALOG_EXTRA")
    for slug, names in getattr(module, "NAMES_EXTRA", {}).items():
        NAMES_EXTRA.setdefault(slug, {}).update(names)
    for slug, rules in getattr(module, "OPTION_RULES_EXTRA", {}).items():
        OPTION_RULES_EXTRA.setdefault(slug, []).extend(rules)
    OUTFIT_EXTRA.update(getattr(module, "OUTFIT_EXTRA", {}))


def _load() -> None:
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            _read(importlib.import_module(f"{__name__}.{info.name}"))
        except Exception as exc:  # noqa: BLE001 —— 单个模块的语法/字段错误不该让整站起不来
            LOAD_ERRORS[info.name] = f"{type(exc).__name__}: {exc}"


_load()


def all_names() -> dict[str, str]:
    """品名 → 分类 slug，用于跨模块查重。"""
    out: dict[str, str] = {}
    for slug, spec in CATALOG_EXTRA.items():
        for name in spec["facts"]:
            out[name] = slug
    for slug, names in NAMES_EXTRA.items():
        for name in names:
            out[name] = slug
    return out
