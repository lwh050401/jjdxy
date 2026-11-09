"""校验 app/seed_data/ 下的品类数据模块：字段齐不齐、词表接没接住分配的词、文案有没有踩线。

跑法：
    PYTHONIOENCODING=utf-8 python -X utf8 .run/lint_seed_data.py appliance_big          # 只查一个模块
    PYTHONIOENCODING=utf-8 python -X utf8 .run/lint_seed_data.py                        # 查全部

写数据的同学可以跑完再交：品名漏了、卖点写成空话、分类元信息缺字段，这里都会挡住，
比等 pytest 或灌库时报错快得多。
"""

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import seed_data  # noqa: E402
from app.services import catalog  # noqa: E402

# 广告法里的绝对化说法 + 站内政策明确没有的服务（不支持价保/货到付款/专票，见 support_faqs）
BANNED = [
    "最便宜", "最好", "最快", "第一", "顶级", "极致", "独家", "唯一", "国家级", "世界领先", "销量冠军",
    "治疗", "治愈", "疗效", "根治", "包治", "防癌", "抗癌", "永久", "终身", "100%", "纯天然无添加",
    "上门安装", "免费安装", "上门维修", "价保", "价格保护", "货到付款", "增值税专用发票", "顺丰包邮",
    "无理由退换", "假一赔十", "终身保修",
]
SPEC = json.loads((ROOT / ".run/seed_spec.json").read_text(encoding="utf-8"))
EXISTING = set(json.loads((ROOT / ".run/existing_names.json").read_text(encoding="utf-8")))
NORMALIZE = re.compile(r"[\s/、，,（）()]+")


def norm(text: str) -> str:
    return NORMALIZE.sub("", str(text)).lower()


def check_group(name: str, groups: list, problems: list) -> None:
    if not isinstance(groups, list) or not groups:
        problems.append(f"{name}：规格组要是非空列表")
        return
    for group in groups:
        if not (isinstance(group, (list, tuple)) and len(group) == 2):
            problems.append(f"{name}：规格组必须是 (组名, [值...])")
            continue
        label, values = group
        if not isinstance(label, str) or not isinstance(values, list):
            problems.append(f"{name}：规格组「{label}」的值必须是列表")
            continue
        if not 2 <= len(values) <= 9:
            problems.append(f"{name}：「{label}」有 {len(values)} 个值，规格组取值应在 2~9 个")
        if len({norm(v) for v in values}) != len(values):
            problems.append(f"{name}：「{label}」有重复取值")
        for v in values:
            if not isinstance(v, str) or not v.strip():
                problems.append(f"{name}：「{label}」里有空值")


def check_facts(name: str, facts: list, problems: list) -> None:
    if not isinstance(facts, list) or len(facts) != 3:
        problems.append(f"{name}：卖点必须正好三条，实际 {len(facts) if isinstance(facts, list) else '非列表'}")
        return
    seen = set()
    for f in facts:
        if not isinstance(f, str) or not f.strip():
            problems.append(f"{name}：卖点里有空句")
            continue
        if not 6 <= len(f) <= 46:
            problems.append(f"{name}：卖点长度 {len(f)} 不在 6~46 字（「{f}」）")
        hit = [b for b in BANNED if b in f]
        if hit:
            problems.append(f"{name}：卖点踩线 {hit}（「{f}」）")
        if norm(f) in seen:
            problems.append(f"{name}：三条卖点里有重复句「{f}」")
        seen.add(norm(f))


def check_module(module: str) -> list[str]:
    problems: list[str] = []
    if module in seed_data.LOAD_ERRORS:
        problems.append(f"{module}：模块导入/合并失败 → {seed_data.LOAD_ERRORS[module]}")
    data = seed_data.CATALOG_EXTRA
    metas = seed_data.CATEGORY_META
    names_extra = seed_data.NAMES_EXTRA

    spec = SPEC["batches"].get(f"{module}.py")
    if not spec:
        return [f"{module}：分配表里没有这个模块，先确认文件名是不是 {module}.py"]

    from app import seed_catalog

    owned_slugs = [s for s in data if s in spec]
    for slug, cat_spec in spec.items():
        if cat_spec.get("__extra__"):
            if slug not in seed_catalog.CATALOG:
                problems.append(f"{slug}：说要往已有分类补品名，但站内没这个分类")
            got = {n for n in names_extra.get(slug, {})}
            if not got:
                problems.append(f"{slug}：NAMES_EXTRA 里没有这个分类的品名")
            pool = got
            for n, facts in names_extra.get(slug, {}).items():
                check_facts(f"{slug}/{n}", facts, problems)
        else:
            if slug not in owned_slugs:
                problems.append(f"{slug}：CATALOG_EXTRA 里没有这个分类")
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]*", slug):
                problems.append(f"{slug}：分类 slug 要写成小写 ASCII（会直接进 URL）")
            entry = data[slug]
            meta = metas.get(slug)
            if not meta:
                problems.append(f"{slug}：缺 CATEGORY_META")
            else:
                if not isinstance(meta.get("sort"), int):
                    problems.append(f"{slug}：meta.sort 必须是整数")
                if not (isinstance(meta.get("icon"), str) and re.fullmatch(r"[a-z][a-z0-9_-]*", meta["icon"])):
                    problems.append(f"{slug}：meta.icon 要写成小写 ASCII 词")
                if not 8 <= len(str(meta.get("description", ""))) <= 60:
                    problems.append(f"{slug}：meta.description 长度要在 8~60 字")
            lo, hi = entry["price"]
            if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and 0 < lo < hi):
                problems.append(f"{slug}：price 区间失真 {entry['price']}")
            check_group(f"{slug}/options", entry["options"], problems)
            if "by_name" in entry:
                for n, groups in entry["by_name"].items():
                    if n not in entry["facts"]:
                        problems.append(f"{slug}/by_name：「{n}」在 facts 里没有对应品名")
                    check_group(f"{slug}/by_name/{n}", groups, problems)
            if not 3 <= len(entry["scenes"]) <= 8:
                problems.append(f"{slug}：scenes {len(entry['scenes'])} 个，建议 3~8 个")
            if len(entry["tags"]) != 3:
                problems.append(f"{slug}：tags 要正好 3 个（分类词表前两枚会变成商品标签）")
            pool = set(entry["facts"])
            for n, facts in entry["facts"].items():
                check_facts(f"{slug}/{n}", facts, problems)
                if not 2 <= len(n) <= 12:
                    problems.append(f"{slug}：品名「{n}」长度失真")

        for w in cat_spec["words"]:
            # 词与品名不必字字对上：检索层会先展开同义词、再剥掉「装备/礼物」这类口语框，
            # 所以「退休礼物」只要品名里写着「退休」就算接住了。按检索真正会比的形状判。
            forms = [norm(f) for f in (catalog.needles(w) or [w])]
            if not any(f and f in norm(n) for f in forms for n in pool):
                problems.append(f"{slug}：分配的词「{w}」没能从品名里查到（要做成品名或其子串）")

    for slug in owned_slugs:
        for n in data[slug]["facts"]:
            if n in EXISTING:
                problems.append(f"{slug}：品名「{n}」和站内已有品名重复")
    for slug, names in names_extra.items():
        if slug in spec and spec[slug].get("__extra__"):
            for n in names:
                if n in EXISTING:
                    problems.append(f"{slug}：品名「{n}」站内已经有了，不用再补")
    return problems


def cross_module_problems() -> list[str]:
    """两个模块抢同一个品名要报出来：各自看自己都合规，合起来就撞车。"""
    from collections import defaultdict

    buckets: dict[str, set] = defaultdict(set)
    for name, slug in seed_data.all_names().items():
        buckets[name].add(slug)
    dup = {n: sorted(s) for n, s in buckets.items() if len(s) > 1}
    return [f"品名「{n}」同时挂在 {s}" for n, s in sorted(dup.items())]


def main() -> int:
    modules = sys.argv[1:] or sorted(p.stem for p in (ROOT / "app/seed_data").glob("*.py") if not p.stem.startswith("_"))
    if not modules:
        print("app/seed_data 里还没有数据模块")
        return 1
    total = 0
    for module in modules:
        problems = check_module(module)
        names = sum(len(v["facts"]) for k, v in seed_data.CATALOG_EXTRA.items()) + sum(len(v) for v in seed_data.NAMES_EXTRA.values())
        print(f"\n[{module}] 问题 {len(problems)} 条（全站数据包共 {names} 个品名）")
        for p in problems:
            print("  - " + p)
        total += len(problems)
    cross = cross_module_problems()
    others = {k: v for k, v in seed_data.LOAD_ERRORS.items() if k not in modules}
    if others:
        print(f"\n[旁路提醒] 其他模块正在写，暂时读不进去（不算你的问题）：{sorted(others)}")
    if cross:
        print(f"\n[跨模块] 问题 {len(cross)} 条")
        for p in cross:
            print("  - " + p)
        total += len(cross)
    if total:
        print(f"\n未通过：{total} 条问题")
        return 1
    print("\n全部通过：字段齐、分配的词都接住了、文案没踩线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
