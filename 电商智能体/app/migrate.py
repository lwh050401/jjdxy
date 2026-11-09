"""把 SQLite 导出的 JSON 灌进 MySQL：建库 → 建表 → 全量导入。

用法：python -m app.migrate          # 已有数据时拒绝覆盖
      python -m app.migrate --force  # 清空业务表后重灌
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import pymysql
from sqlalchemy import create_engine, text

from .settings import get_settings

HERE = pathlib.Path(__file__).resolve().parent
EXPORT = HERE.parent / "data" / "sqlite-export.json"

# 先父表后子表，删数据时反序，避免外键报错
LOAD_ORDER = [
    "users",
    "categories",
    "products",
    "product_images",
    "variants",
    "inventory_movements",
    "addresses",
    "balance_entries",
    "sessions",
    "verification_tokens",
    "wishlist",
    "reviews",
    "review_votes",
    "coupons",
    "shipping_plans",
    "carts",
    "cart_items",
    "orders",
    "order_items",
    "order_events",
    "payments",
    "coupon_redemptions",
    "refund_requests",
    "reservations",
    "settings",
    "support_faqs",
    "support_messages",
    "analytics_events",
    "audit_logs",
    "outbox_messages",
]

# 旧库用 TEXT 存 JSON，空串会让 MySQL 的 JSON 列报错，这里给每列补一个合法默认值
JSON_DEFAULTS = {
    "products.tags": "[]",
    "products.attributes": "{}",
    "variants.options": "{}",
    "orders.shipping_address": "{}",
    "reviews.images": "[]",
}


def ensure_database() -> None:
    s = get_settings()
    conn = pymysql.connect(host=s.mysql_host, port=s.mysql_port, user=s.mysql_user, password=s.mysql_password, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{s.mysql_database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    conn.close()


def apply_schema() -> None:
    sql = (HERE / "schema.sql").read_text(encoding="utf-8")
    # 整行注释先去掉再按分号切，否则注释会粘在下一条 CREATE 语句头上被整段丢掉
    sql = re.sub(r"(?m)^\s*--.*$", "", sql)
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    engine = create_engine(get_settings().dsn)
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    engine.dispose()


def table_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        text("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t ORDER BY ORDINAL_POSITION"),
        {"db": get_settings().mysql_database, "t": table},
    ).all()
    return [r[0] for r in rows]


def normalize(table: str, col: str, raw):
    if raw is None:
        return JSON_DEFAULTS.get(f"{table}.{col}")
    if isinstance(raw, str):
        key = f"{table}.{col}"
        is_json_col = key in JSON_DEFAULTS or col in {"tags", "attributes", "options", "images", "shipping_address", "billing_address", "item_ids", "meta", "raw", "tool_calls", "args"}
        if is_json_col:
            if raw.strip() in ("", "null"):
                return JSON_DEFAULTS.get(key)
            json.loads(raw)  # 校验，坏 JSON 直接抛出，避免脏数据进库
            return raw
        if col.endswith(("_at", "_time")) and raw and "T" in raw:
            return raw.replace("T", " ").replace("Z", "")[:23]
    return raw


def load(data: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    engine = create_engine(get_settings().dsn)
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in reversed(LOAD_ORDER):
            conn.execute(text(f"TRUNCATE TABLE `{table}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

        for table in LOAD_ORDER:
            rows = data.get(table) or []
            if not rows:
                counts[table] = 0
                continue
            cols = table_columns(conn, table)
            for row in rows:
                payload = {c: normalize(table, c, row.get(c)) for c in cols if c in row}
                names = ", ".join(f"`{c}`" for c in payload)
                holders = ", ".join(f":{c}" for c in payload)
                conn.execute(text(f"INSERT INTO `{table}` ({names}) VALUES ({holders})"), payload)
            counts[table] = len(rows)

        # 自增序列接到已有最大 id 之后，否则新商品会撞主键
        for table in ("products", "variants", "categories", "product_images", "order_items"):
            current = conn.execute(text(f"SELECT COALESCE(MAX(`id`), 0) FROM `{table}`")).scalar()
            conn.execute(text(f"ALTER TABLE `{table}` AUTO_INCREMENT = {int(current) + 1}"))
    engine.dispose()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite → MySQL 迁移")
    parser.add_argument("--force", action="store_true", help="清空业务表后重新导入")
    parser.add_argument("--source", default=str(EXPORT), help="SQLite 导出的 JSON 路径")
    args = parser.parse_args()

    if not pathlib.Path(args.source).exists():
        print(f"找不到导出数据：{args.source}", file=sys.stderr)
        return 2

    ensure_database()
    apply_schema()

    engine = create_engine(get_settings().dsn)
    with engine.connect() as conn:
        existing = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
    engine.dispose()
    if existing and not args.force:
        print(f"库里已有 {existing} 个用户，跳过导入（需要重灌请加 --force）")
        return 0

    data = json.loads(pathlib.Path(args.source).read_text(encoding="utf-8"))
    counts = load(data)
    total = sum(counts.values())
    print("迁移完成：")
    for table, n in counts.items():
        print(f"  {table:<22}{n}")
    print(f"共 {total} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
