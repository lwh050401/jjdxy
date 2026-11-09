"""钱包余额：加钱、扣钱都留一条流水，扣成负数直接拒绝，口径与旧 Node 版一致。"""

from __future__ import annotations

from .. import db
from ..security import new_id
from .checkout import CheckoutError

TOPUP_MIN_CENTS = 1000
TOPUP_MAX_CENTS = 500_000


def balance_of(user_id: str) -> int:
    return int(db.value("SELECT balance_cents FROM users WHERE id = :uid", {"uid": user_id}) or 0)


def adjust(user_id: str, delta_cents: int, *, kind: str, reference: str | None = None, note: str = "") -> dict:
    amount = int(delta_cents or 0)
    if amount == 0:
        raise CheckoutError("金额不能为 0")
    after = balance_of(user_id) + amount
    if after < 0:
        raise CheckoutError("账户余额不足，请更换支付方式")
    now = db.now_iso()
    entry_id = new_id("bal")
    db.update("users", {"balance_cents": after, "updated_at": now}, "id = :uid", {"uid": user_id})
    db.insert(
        "balance_entries",
        {
            "id": entry_id,
            "user_id": user_id,
            "amount_cents": amount,
            "balance_after_cents": after,
            "kind": kind,
            "reference": reference,
            "note": note[:255],
            "created_at": now,
        },
    )
    return db.get("SELECT * FROM balance_entries WHERE id = :eid", {"eid": entry_id})


def top_up(user_id: str, amount_cents: int) -> dict:
    amount = int(amount_cents or 0)
    if amount < TOPUP_MIN_CENTS or amount > TOPUP_MAX_CENTS:
        raise CheckoutError(f"单次充值 {db.cents_to_yuan(TOPUP_MIN_CENTS)} 元到 {db.cents_to_yuan(TOPUP_MAX_CENTS)} 元之间")
    return adjust(user_id, amount, kind="topup", note="模拟网关充值")


def spend(user_id: str, amount_cents: int, *, reference: str | None = None) -> dict:
    return adjust(user_id, -int(amount_cents), kind="payment", reference=reference, note="订单支付")


def refund(user_id: str, amount_cents: int, *, reference: str | None = None) -> dict:
    return adjust(user_id, int(amount_cents), kind="refund", reference=reference, note="退款回账")


def overview(user_id: str, *, limit: int = 50) -> dict:
    rows = db.all_(
        """SELECT b.*, o.order_no AS reference_number FROM balance_entries b
           LEFT JOIN orders o ON o.order_no = b.reference
           WHERE b.user_id = :uid ORDER BY b.created_at DESC, b.id DESC LIMIT :limit""",
        {"uid": user_id, "limit": max(1, min(int(limit), 200))},
    )
    return {
        "balance_cents": balance_of(user_id),
        "topup_min_cents": TOPUP_MIN_CENTS,
        "topup_max_cents": TOPUP_MAX_CENTS,
        "entries": [
            {
                "id": r["id"],
                "amount_cents": int(r["amount_cents"]),
                "balance_after_cents": int(r["balance_after_cents"]),
                "kind": r["kind"],
                "reference": r.get("reference"),
                "reference_number": r.get("reference_number"),
                "note": r.get("note"),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }
