"""MySQL 数据访问层：一个连接池 + 四个薄封装（all/get/run/value），保持与旧 Node 代码一致的取数手感。"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .settings import get_settings

_engine: Engine | None = None

# 旧库用 TEXT 存 JSON 与 ISO 时间，迁到 MySQL 后这两类列要在读取时还原
JSON_COLUMN_NAMES = {
    "tags",
    "attributes",
    "options",
    "images",
    "shipping_address",
    "billing_address",
    "item_ids",
    "raw",
    "meta",
    "tool_calls",
    "args",
    "sig",
}

DATETIME_SUFFIXES = ("_at", "_time")


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().dsn, pool_size=8, max_overflow=8, pool_pre_ping=True, future=True)
    return _engine


def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


@contextmanager
def session() -> Iterator[Any]:
    with engine().begin() as conn:
        yield conn


class _Tx:
    """短事务：下单这种多表写入必须一起成功或一起回滚。"""

    def __init__(self, conn: Any):
        self._conn = conn

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        return self._conn.execute(text(sql), bind(params)).rowcount

    def rows(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [_decode(dict(r)) for r in self._conn.execute(text(sql), params or {}).mappings().all()]

    def insert(self, table: str, row: dict[str, Any]) -> int | None:
        cols = list(row)
        sql = f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in cols)}) VALUES ({', '.join(f':{c}' for c in cols)})"
        result = self._conn.execute(text(sql), bind(row))
        return int(result.lastrowid) if result.lastrowid else None

    def scalar(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return self._conn.execute(text(sql), params or {}).scalar()


@contextmanager
def tx() -> Iterator[_Tx]:
    conn = engine().connect()
    trans = conn.begin()
    try:
        yield _Tx(conn)
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if isinstance(value, dt.datetime):
            out[key] = (value.replace(microsecond=0).isoformat() + "Z") if value.tzinfo is None else value.isoformat()
        elif key in JSON_COLUMN_NAMES and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                out[key] = value
        else:
            out[key] = value
    return out


def bind(params: dict[str, Any] | None) -> dict[str, Any]:
    """写操作的入参统一过一遍 coerce，ISO 时间要变成 MySQL 认的 datetime。"""
    return {k: coerce(v) for k, v in (params or {}).items()}


def coerce(value: Any) -> Any:
    """写库前的值转换：bool→0/1，dict/list→JSON，ISO 字符串→datetime。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and len(value) >= 19 and value[4:5] == "-" and (value[10:11] in ("T", " ")):
        iso = value.replace("Z", "").replace("T", " ")
        try:
            return dt.datetime.fromisoformat(iso)
        except ValueError:
            return value
    return value


def all_(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with engine().connect() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().all()
    return [_decode(dict(r)) for r in rows]


def get(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = all_(sql, params)
    return rows[0] if rows else None


def value(sql: str, params: dict[str, Any] | None = None) -> Any:
    row = get(sql, params)
    return next(iter(row.values())) if row else None


def run(sql: str, params: dict[str, Any] | None = None) -> int:
    return _execute_in_session(sql, params)


def _execute_in_session(sql: str, params: dict[str, Any] | None = None) -> int:
    with session() as conn:
        return conn.execute(text(sql), bind(params)).rowcount


def insert(table: str, row: dict[str, Any]) -> int | None:
    cols = list(row)
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in cols)}) VALUES ({placeholders})"
    with session() as conn:
        result = conn.execute(text(sql), bind(row))
        if result.lastrowid:
            return int(result.lastrowid)
    return None


def update(table: str, row: dict[str, Any], where: str, params: dict[str, Any] | None = None) -> int:
    assignments = ", ".join(f"`{c}` = :set_{c}" for c in row)
    merged = {f"set_{c}": v for c, v in row.items()}
    merged.update(params or {})
    return run(f"UPDATE `{table}` SET {assignments} WHERE {where}", merged)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def money(cents: Any) -> str:
    """站内统一的金额写法：千分位 + 两位小数，前台和小马都用它。"""
    return f"¥{int(cents or 0) / 100:,.2f}"


def cents_to_yuan(cents: Any) -> float:
    return round((int(cents or 0)) / 100, 2)
