"""CarveOutBroker 단위 테스트 — 실험 슬라이스 보고·NAV 방어·멱등·부분체결 정산.

내부 브로커를 목(FakeInner)으로 주입 → 네트워크·실주문 0. TossBroker HTTP는 test_toss_broker가 검증.
실행: python3 tests/test_carveout_broker.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Order, OrderResult, Portfolio, Position, Side
from src.forward.carveout_broker import CarveOutBroker
from src.forward.experiment_portfolio import ExperimentPortfolio, ExperimentPosition


class FakeInner:
    """CarveOutBroker가 기대하는 내부 브로커 계약을 흉내(get_prices/get_portfolio/place_order/get_order)."""

    def __init__(self, price=10.0, holdings=None, live=True, fill_ratio=1.0, commission=0.0):
        self.price = price
        self.holdings = holdings or {}          # sym -> qty (실계좌 전체)
        self.live = live
        self.fill_ratio = fill_ratio            # 1.0=완전체결, 0.5=부분체결
        self.commission = commission
        self.seq = 0
        self.key_to_oid: dict[str, str] = {}    # clientOrderId -> orderId (멱등 dedup)
        self._orders: dict[str, Order] = {}
        self.placed: list = []
        self.get_order_calls = 0

    def get_prices(self):
        return {"AAPL": self.price}

    def get_portfolio(self):
        pf = Portfolio(cash=0.0)
        for s, q in self.holdings.items():
            pf.positions[s] = Position(s, q, self.price)
        return pf

    def place_order(self, order, when=None, client_order_id=None):
        self.placed.append((client_order_id, order.side.value, order.quantity))
        when = when or datetime.now()
        if not self.live:
            return OrderResult(order, 0.0, 0.0, 0.0, "dry-run", when, "live=False")
        oid = self.key_to_oid.get(client_order_id)
        if oid is None:                          # 새 키 → 새 orderId(멱등: 같은 키면 재사용)
            self.seq += 1
            oid = f"ord-{self.seq}"
            self.key_to_oid[client_order_id] = oid
            self._orders[oid] = order
        return OrderResult(order, 0.0, 0.0, 0.0, "submitted", when, oid)

    def get_order(self, order_id):
        self.get_order_calls += 1
        order = self._orders[order_id]
        filled = order.quantity * self.fill_ratio
        # 부분체결은 종결이 아님(partially_filled) — 완전체결(fill_ratio>=1)만 filled.
        status = "filled" if self.fill_ratio >= 1.0 else "partially_filled"
        return OrderResult(order, filled, self.price, self.commission,
                           status, datetime.now(), order_id)


def _exp(cash=100.0, experiment=None):
    return ExperimentPortfolio(initial_capital=100.0, cash=cash,
                               experiment_positions=experiment or {})


def _carve(inner, exp):
    return CarveOutBroker(inner, exp, poll_attempts=3, poll_wait=0.0, sleep=lambda _: None)


def test_account_holdings_from_inner():
    inner = FakeInner(holdings={"AAPL": 10.3})
    c = _carve(inner, _exp())
    assert c.account_holdings() == {"AAPL": 10.3}


def test_buy_full_fill_settles():
    inner = FakeInner(price=10.0)
    c = _carve(inner, _exp(cash=100.0))
    r = c.place_and_settle(Order("AAPL", Side.BUY, 0.5), client_order_id="k1")
    assert r.status == "filled" and abs(r.filled_qty - 0.5) < 1e-9 and r.fill_price == 10.0


def test_partial_fill_not_finalized():
    """부분체결을 최종체결로 종료하면 안 됨(Codex P1) — 종결로 안 보고 폴링을 계속해야."""
    inner = FakeInner(price=10.0, fill_ratio=0.5)   # 요청 1.0 중 0.5만 체결(계속 partial)
    c = _carve(inner, _exp(cash=100.0))             # poll_attempts=3
    r = c.place_and_settle(Order("AAPL", Side.BUY, 1.0), client_order_id="k2")
    assert r.status == "partially_filled"           # filled로 종결시키지 않음
    assert abs(r.filled_qty - 0.5) < 1e-9           # 반영은 체결분만
    assert inner.get_order_calls == 3               # 첫 partial에서 멈추지 않고 끝까지 폴링


def test_buy_blocked_over_experiment_cash():
    inner = FakeInner(price=10.0)
    c = _carve(inner, _exp(cash=3.0))               # 현금 3불
    r = c.place_and_settle(Order("AAPL", Side.BUY, 1.0), client_order_id="k3")  # ~10불 필요
    assert r.status == "rejected" and "실험 현금 초과" in r.note
    assert inner.placed == []                        # 실제 전송조차 안 됨


def test_sell_blocked_over_experiment_holding():
    inner = FakeInner(price=10.0, holdings={"AAPL": 10.3})  # 실계좌엔 10.3(보호분 포함)
    c = _carve(inner, _exp(experiment={"AAPL": ExperimentPosition("AAPL", 0.3, 9.0)}))
    r = c.place_and_settle(Order("AAPL", Side.SELL, 0.4), client_order_id="k4")  # 실험분 0.3 초과
    assert r.status == "rejected" and "실험 보유 초과" in r.note   # 보호분 침범 차단


def test_idempotent_retry_reuses_order():
    inner = FakeInner(price=10.0)
    c = _carve(inner, _exp(cash=100.0))
    c.place_and_settle(Order("AAPL", Side.BUY, 0.5), client_order_id="same")
    c.place_and_settle(Order("AAPL", Side.BUY, 0.5), client_order_id="same")
    assert inner.seq == 1                            # 같은 멱등키 → orderId 1개만(중복주문 방지)


def test_dry_run_inner_not_settled():
    inner = FakeInner(price=10.0, live=False)        # 내부가 paper(dry-run)
    c = _carve(inner, _exp(cash=100.0))
    r = c.place_and_settle(Order("AAPL", Side.BUY, 0.5), client_order_id="k5")
    assert r.status == "dry-run"                     # 미전송 — 실주문 안 나감


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 CarveOutBroker 테스트 통과")
