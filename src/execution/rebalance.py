"""목표 비중 -> 구체 주문 변환(리밸런서).

[What] 전략이 낸 target_weight와 현재 포트폴리오의 차이를 매수/매도 주문으로.
[Why]  전략은 '얼마나 갖고 싶은지'(비중)만 말하고, '얼마 사고팔지'(주문)는 여기서 계산 →
       전략과 집행의 관심사 분리.
[How]  현재가치 대비 목표가치 차액을 주식수로 환산. 먼지 거래는 min_trade로 억제.
"""
from __future__ import annotations

from src.core.types import Order, Portfolio, Side, Signal


def signals_to_orders(
    signals: list[Signal],
    portfolio: Portfolio,
    prices: dict[str, float],
    min_trade: float = 1.0,
) -> list[Order]:
    total = portfolio.total_value(prices)
    targets = {s.symbol: max(0.0, s.target_weight) for s in signals}
    reasons = {s.symbol: s.rationale for s in signals}
    orders: list[Order] = []

    for symbol, weight in targets.items():
        price = prices.get(symbol)
        if not price or price <= 0:
            continue
        target_value = weight * total
        pos = portfolio.positions.get(symbol)
        current_value = (pos.quantity * price) if pos else 0.0
        diff_value = target_value - current_value

        if abs(diff_value) < min_trade:
            continue
        qty = abs(diff_value) / price
        side = Side.BUY if diff_value > 0 else Side.SELL
        orders.append(Order(symbol, side, qty, reasons.get(symbol, "")))

    # 매도를 먼저 실행해 현금을 확보한 뒤 매수(현금 부족 방지)
    orders.sort(key=lambda o: 0 if o.side is Side.SELL else 1)
    return orders
