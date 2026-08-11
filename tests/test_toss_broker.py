"""TossBroker 단위 테스트 — transport mock으로 네트워크·실주문 0.

실행: python3 -m pytest -q  (pytest 없으면 python3 tests/test_toss_broker.py)

토스 Open API 실스펙(2026-08) 기준 mock:
  - POST /oauth2/token            토큰 발급(+캐싱 검증)
  - GET  /api/v1/prices?symbols=  배치 시세(lastPrice=문자열)
  - GET  /api/v1/holdings         보유(items[]), 현금은 여기 없음
  - GET  /api/v1/buying-power     현금(cashBuyingPower)
  - POST /api/v1/orders           주문 생성(result.orderId)
  - GET  /api/v1/orders/{id}      체결 조회(execution)

핵심 불변식: dry-run(live=False)은 절대 POST /api/v1/orders 를 치지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Order, Side
from src.execution.toss_broker import TossBroker


class FakeToss:
    """토스 API 실스펙을 흉내내는 in-memory transport. 호출 기록으로 불변식 검증."""

    def __init__(self, price=50.0, cash=100.0, holdings=None):
        self.price = price
        self.cash = cash
        self.holdings = holdings or []
        self.calls: list[tuple[str, str]] = []   # (method, path)
        self.last_order_body = None
        self.token_issued = 0

    def __call__(self, method, url, headers, body):
        path = url.split("?", 1)[0].replace("https://openapi.tossinvest.com", "")
        self.calls.append((method, path))
        if path == "/oauth2/token":
            self.token_issued += 1
            return 200, {"access_token": f"tok-{self.token_issued}",
                         "token_type": "Bearer", "expires_in": 3600}
        if path == "/api/v1/prices":
            return 200, {"result": [{"symbol": "AAPL", "lastPrice": str(self.price),
                                     "currency": "USD"}]}
        if path == "/api/v1/buying-power":
            return 200, {"result": {"currency": "USD", "cashBuyingPower": str(self.cash)}}
        if path == "/api/v1/holdings":
            return 200, {"result": {"items": self.holdings}}
        if path == "/api/v1/orders":
            self.last_order_body = json.loads(body.decode())
            return 201, {"result": {"orderId": "ord-1", "clientOrderId": None}}
        if path.startswith("/api/v1/orders/"):
            return 200, {"result": {"orderId": "ord-1", "symbol": "AAPL", "side": "BUY",
                                    "quantity": "1", "status": "FILLED",
                                    "execution": {"filledQuantity": "1",
                                                  "averageFilledPrice": str(self.price),
                                                  "commission": "0.01"}}}
        return 404, {"message": f"unknown {path}"}

    def order_calls(self) -> int:
        return sum(1 for m, p in self.calls if p == "/api/v1/orders")


def _broker(fake: FakeToss, *, live: bool) -> TossBroker:
    return TossBroker(client_id="cid", client_secret="secret", account_no="12345",
                      symbols=["AAPL"], budget=100.0, live=live, transport=fake)


def test_token_is_cached():
    fake = FakeToss()
    b = _broker(fake, live=False)
    b.get_prices(); b.get_prices()
    assert fake.token_issued == 1  # 두 번 조회해도 토큰은 1회 발급(캐싱)


def test_prices_batched_in_one_call():
    fake = FakeToss(price=42.0)
    b = _broker(fake, live=False)
    assert b.get_prices()["AAPL"] == 42.0
    # 종목 시세는 배치 1콜(콤마)로 — /api/v1/prices 호출이 정확히 1회
    assert sum(1 for m, p in fake.calls if p == "/api/v1/prices") == 1


def test_portfolio_merges_holdings_and_cash():
    fake = FakeToss(price=42.0, cash=80.0,
                    holdings=[{"symbol": "AAPL", "quantity": "2", "averagePurchasePrice": "30"}])
    b = _broker(fake, live=False)
    pf = b.get_portfolio()
    assert abs(pf.cash - 80.0) < 1e-9          # 현금은 buying-power에서
    assert pf.positions["AAPL"].quantity == 2.0  # 보유는 holdings.items에서


def test_dry_run_never_sends_order():
    """live=False면 어떤 경우에도 POST /api/v1/orders 가 발생하면 안 된다(안전 불변식)."""
    fake = FakeToss()
    b = _broker(fake, live=False)
    r = b.place_order(Order("AAPL", Side.BUY, 1.0), when=datetime(2026, 1, 2))
    assert r.status == "dry-run"
    assert fake.order_calls() == 0


def test_live_buy_uses_order_amount():
    """US 소수점 매수는 quantity가 아니라 orderAmount(달러)로 나가야 한다."""
    fake = FakeToss(price=10.0, cash=100.0)
    b = _broker(fake, live=True)
    r = b.place_order(Order("AAPL", Side.BUY, 0.5), when=datetime(2026, 1, 2))
    assert r.status == "submitted" and r.note == "ord-1"
    assert fake.order_calls() == 1
    body = fake.last_order_body
    assert body["side"] == "BUY" and body["orderType"] == "MARKET"
    assert body["orderAmount"] == "5.00"   # 0.5주 * $10
    assert "quantity" not in body
    assert "clientOrderId" in body         # 멱등성 키 포함


def test_live_sell_uses_quantity():
    fake = FakeToss(price=10.0, cash=100.0,
                    holdings=[{"symbol": "AAPL", "quantity": "2", "averagePurchasePrice": "9"}])
    b = _broker(fake, live=True)
    r = b.place_order(Order("AAPL", Side.SELL, 1.5), when=datetime(2026, 1, 2))
    assert r.status == "submitted"
    body = fake.last_order_body
    assert body["side"] == "SELL" and body["quantity"] == "1.5"
    assert "orderAmount" not in body


def test_live_buy_blocked_when_over_budget():
    """보유 평가액이 예산을 넘으면 매수는 브로커가 자체 차단(상위 Guardrail 우회 대비)."""
    fake = FakeToss(price=50.0, cash=100.0,
                    holdings=[{"symbol": "AAPL", "quantity": "3", "averagePurchasePrice": "50"}])
    # 총자산 = 100 + 3*50 = 250 > 100 → 매수 차단
    b = _broker(fake, live=True)
    r = b.place_order(Order("AAPL", Side.BUY, 1.0), when=datetime(2026, 1, 2))
    assert r.status == "rejected"
    assert fake.order_calls() == 0


def test_get_order_parses_execution():
    fake = FakeToss(price=20.0)
    b = _broker(fake, live=True)
    r = b.get_order("ord-1")
    assert r.status == "filled"
    assert r.filled_qty == 1.0 and r.fill_price == 20.0 and r.commission == 0.01


def test_missing_credentials_raises():
    try:
        TossBroker(client_id="", client_secret="", account_no="x",
                   symbols=["AAPL"], transport=FakeToss())
        assert False, "빈 자격증명인데 예외가 안 남"
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 TossBroker 테스트 통과")
