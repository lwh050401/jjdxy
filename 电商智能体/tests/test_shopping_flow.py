"""一条完整的购物链路：加购 → 报价 → 下单 → 余额支付 → 退款申请 → 商家同意。

金额全部以「分」为单位断言，避免前台展示层把问题藏起来。
"""

from __future__ import annotations

from app import db
from app.services import checkout, inventory, refunds, wallet
from app.settings import get_settings

CONTACT = {"email": "flow@example.com", "phone": "13800000000"}
ADDRESS = {
    "receiver": "接口测试",
    "phone": "13800000000",
    "province": "浙江省",
    "city": "杭州市",
    "district": "西湖区",
    "detail": "文三路 1 号",
}


def _add(client, variant_id, qty):
    res = client.post("/api/cart/items", json={"variant_id": variant_id, "qty": qty})
    assert res.status_code == 200, res.text
    return res.json()["data"]


def test_cart_caps_quantity(client, buyer, variant):
    view = _add(client, variant["id"], 2)
    assert view["count"] == 2
    assert view["subtotal_cents"] == 2 * int(variant["price_cents"])

    item = view["items"][0]
    capped = client.patch(f"/api/cart/items/{item['id']}", json={"qty": 99999}).json()["data"]
    expected = min(inventory.available(int(variant["id"])), get_settings().max_qty_per_item)
    assert capped["items"][0]["qty"] == expected


def test_checkout_pay_and_refund(client, buyer, variant):
    before = inventory.available(int(variant["id"]))
    view = _add(client, variant["id"], 1)
    subtotal = view["subtotal_cents"]

    code = db.get(
        "SELECT code FROM coupons WHERE status = 'active' AND kind = 'percent' AND min_subtotal_cents <= :sub ORDER BY min_subtotal_cents ASC LIMIT 1",
        {"sub": subtotal},
    )
    coupon = (code or {}).get("code") or ""
    quote = client.post("/api/checkout/quote", json={"coupon_code": coupon}).json()["data"]
    assert quote["subtotal_cents"] == subtotal
    assert quote["total_cents"] == quote["subtotal_cents"] - quote["discount_cents"] + quote["shipping_cents"] + quote["tax_cents"]
    if coupon:
        assert quote["discount_cents"] > 0

    placed = client.post(
        "/api/checkout/place", json={"contact": CONTACT, "address": ADDRESS, "coupon_code": coupon}
    ).json()["data"]
    assert placed["status"] == "pending_payment"
    assert inventory.available(int(variant["id"])) == before - 1

    paid = client.post("/api/checkout/pay", json={"order_no": placed["order_no"], "provider": "balance"})
    assert paid.status_code == 400  # 新账号余额为 0，先拒付再充值

    topup = client.post("/api/wallet/topup", json={"amount_cents": placed["total_cents"]})
    assert topup.status_code == 201, topup.text
    assert topup.json()["data"]["balance_cents"] == placed["total_cents"]

    paid = client.post("/api/checkout/pay", json={"order_no": placed["order_no"], "provider": "balance"}).json()["data"]
    assert paid["payment_status"] == "paid"
    assert wallet.balance_of(buyer["id"]) == 0

    events = db.all_("SELECT message FROM order_events WHERE order_id = :oid ORDER BY id", {"oid": paid["id"]})
    assert any(e["message"] == "钱包余额 支付成功" for e in events)
    assert not [e for e in events if e["message"].split(" ")[0] in ("alipay", "balance", "wechat", "card")]

    refund = client.post(
        f"/api/orders/{placed['order_no']}/refund-requests",
        json={"kind": "return", "reason": "尺码不合适", "detail": "接口测试"},
    )
    assert refund.status_code == 201, refund.text
    row = refund.json()["data"]
    assert row["amount_cents"] == placed["total_cents"]
    assert row["status_text"] == "待处理"

    refunds.approve(row["id"], admin_id=None, note="接口测试同意")
    after = db.get("SELECT * FROM orders WHERE id = :id", {"id": paid["id"]})
    assert after["payment_status"] == "refunded"
    assert int(after["refunded_cents"]) == placed["total_cents"]
    assert inventory.available(int(variant["id"])) == before
    assert wallet.balance_of(buyer["id"]) == placed["total_cents"]
    assert db.value("SELECT COUNT(*) FROM balance_entries WHERE user_id = :uid AND kind = 'refund'", {"uid": buyer["id"]}) == 1


def test_wallet_rejects_amounts_out_of_range(client, buyer):
    assert client.post("/api/wallet/topup", json={"amount_cents": 99}).status_code == 400
    assert client.post("/api/wallet/topup", json={"amount_cents": 500_001}).status_code == 400


def test_cancel_unpaid_order_releases_stock(client, buyer, variant):
    before = inventory.available(int(variant["id"]))
    _add(client, variant["id"], 1)
    placed = client.post(
        "/api/checkout/place", json={"contact": CONTACT, "address": ADDRESS, "coupon_code": ""}
    ).json()["data"]
    assert inventory.available(int(variant["id"])) == before - 1
    client.post(f"/api/orders/{placed['order_no']}/cancel", json={"reason": "接口测试取消"})
    assert inventory.available(int(variant["id"])) == before


def test_expired_order_closes_itself(client, buyer, variant):
    """超时未付必须真的关单放库存：`release_expired()` 过去只在服务启动时跑一次，
    运行期从来没人调用，这条链等于没有。"""
    before = inventory.available(int(variant["id"]))
    _add(client, variant["id"], 1)
    placed = client.post(
        "/api/checkout/place", json={"contact": CONTACT, "address": ADDRESS, "coupon_code": ""}
    ).json()["data"]
    assert inventory.available(int(variant["id"])) == before - 1

    # 把到期时间挪到一小时前，模拟「服务一直开着，30 分钟早过了」
    db.run("UPDATE orders SET expires_at = DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR) WHERE id = :oid", {"oid": placed["id"]})
    assert checkout.release_expired() >= 1
    closed = db.get("SELECT status, fulfillment_status FROM orders WHERE id = :oid", {"oid": placed["id"]})
    assert closed["status"] == "expired"
    assert closed["fulfillment_status"] == "cancelled"
    assert db.value("SELECT COUNT(*) FROM reservations WHERE order_id = :oid AND status = 'active'", {"oid": placed["id"]}) == 0
    assert db.value("SELECT COUNT(*) FROM reservations WHERE order_id = :oid AND status = 'released'", {"oid": placed["id"]}) >= 1
    assert db.value("SELECT COUNT(*) FROM order_events WHERE order_id = :oid AND kind = 'expired'", {"oid": placed["id"]}) == 1
    assert inventory.available(int(variant["id"])) == before

    # 再扫一遍不该把已经关掉的单再处理一次，也不该把库存多放回去
    assert checkout.release_expired() == 0
    assert inventory.available(int(variant["id"])) == before
