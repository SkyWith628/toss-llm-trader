"""단계 0 스모크·단위 테스트 — 파이프라인과 핵심 불변식 검증.

실행: python3 -m pytest -q   (pytest 없으면 python3 tests/test_smoke.py)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

# 프로젝트 루트를 경로에 추가 → `python3 tests/test_smoke.py` 직접 실행 가능
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Order, Portfolio, Position, Side
from src.execution.paper_broker import PaperBroker
from src.risk.guardrail import Guardrail


def test_paper_broker_buy_then_sell():
    b = PaperBroker(starting_cash=100.0, slippage=0.0)
    b.set_prices({"AAA": 50.0})
    r = b.place_order(Order("AAA", Side.BUY, 1.0), when=datetime(2025, 1, 2))
    assert r.status == "filled"
    assert abs(b.portfolio.cash - 50.0) < 1e-6
    assert b.portfolio.positions["AAA"].quantity == 1.0

    r2 = b.place_order(Order("AAA", Side.SELL, 1.0), when=datetime(2025, 1, 3))
    assert r2.status == "filled"
    assert abs(b.portfolio.cash - 100.0) < 1e-6
    assert "AAA" not in b.portfolio.positions


def test_guardrail_blocks_overspend():
    g = Guardrail(budget=100.0, valid_symbols={"AAA"})
    pf = Portfolio(cash=10.0)
    prices = {"AAA": 50.0}
    # 현금 10인데 2주(=100어치) 매수 시도 → 차단
    d = g.check(Order("AAA", Side.BUY, 2.0), pf, prices)
    assert not d.ok and d.order is None


def test_guardrail_blocks_oversell():
    g = Guardrail(budget=100.0, valid_symbols={"AAA"})
    pf = Portfolio(cash=0.0, positions={"AAA": Position("AAA", 1.0, 50.0)})
    d = g.check(Order("AAA", Side.SELL, 2.0), pf, {"AAA": 50.0})
    assert not d.ok


def test_rebalance_clips_buys_to_cash():
    """두 매수를 5:5로 낼 때, 슬리피지로 둘째가 차단되지 않고 '가용 현금에 맞춰' 둘 다 나가야."""
    from src.core.types import Action, Signal
    from src.execution.rebalance import signals_to_orders
    pf = Portfolio(cash=100.0)
    prices = {"AAA": 40.0, "BBB": 25.0}
    sigs = [Signal("AAA", Action.BUY, 0.5), Signal("BBB", Action.BUY, 0.5)]
    orders = signals_to_orders(sigs, pf, prices, slippage=0.0005)
    assert len(orders) == 2 and all(o.side is Side.BUY for o in orders)
    # 두 매수의 소요 현금(슬리피지 포함) 합이 보유 현금을 넘지 않아야(둘 다 체결 가능)
    cost = sum(o.quantity * prices[o.symbol] * 1.0005 for o in orders)
    assert cost <= 100.0 + 1e-6


def test_rebalance_clipped_buys_pass_guardrail_and_fill():
    """클립된 주문이 가드레일·페이퍼브로커를 순차 통과해 실제로 둘 다 체결되는지(회귀)."""
    from src.core.types import Action, Signal
    from src.execution.rebalance import signals_to_orders
    broker = PaperBroker(starting_cash=100.0, slippage=0.0005)
    prices = {"AAA": 40.0, "BBB": 25.0}
    broker.set_prices(prices)
    g = Guardrail(budget=100.0, valid_symbols={"AAA", "BBB"})
    sigs = [Signal("AAA", Action.BUY, 0.5), Signal("BBB", Action.BUY, 0.5)]
    filled = 0
    for o in signals_to_orders(sigs, broker.get_portfolio(), prices, slippage=0.0005):
        d = g.check(o, broker.get_portfolio(), prices)
        assert d.ok, f"가드레일 차단됨: {d.reason}"
        assert broker.place_order(d.order).status == "filled"
        filled += 1
    assert filled == 2                       # 둘 다 체결(과거엔 하나가 차단됐음)
    assert broker.portfolio.cash < 0.2       # 현금이 거의 소진(≈100% 배치)


def test_no_lookahead_in_history():
    """history_until은 미래 데이터를 절대 포함하지 않아야 한다."""
    from src.data.synthetic import make_synthetic_store
    store = make_synthetic_store(["AAA"], days=30, seed=1)
    mid = store.dates[10]
    hist = store.history_until(mid, lookback=100)["AAA"]
    # 11번째 날까지만(=index 0~10), 그 이후 미포함
    assert len(hist) == 11


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 스모크 테스트 통과")
