"""목표 비중 -> 구체 주문 변환(리밸런서).

[What] 전략이 낸 target_weight와 현재 포트폴리오의 차이를 매수/매도 주문으로.
[Why]  전략은 '얼마나 갖고 싶은지'(비중)만 말하고, '얼마 사고팔지'(주문)는 여기서 계산 →
       전략과 집행의 관심사 분리.
[How]  현재가치 대비 목표가치 차액을 주식수로 환산. 먼지 거래는 min_trade로 억제.
       매수는 '가용 현금에 맞춰 클립' — 슬리피지·수수료 탓에 마지막 매수가 통째로 차단돼
       예산 일부가 놀던 문제를 없앤다(차단 대신 살 수 있는 만큼). 비용 가정은 브로커와
       공유하는 CostModel로 통일(리밸런서 추정 == 브로커 실제).
"""
from __future__ import annotations

from src.core.types import Order, Portfolio, Side, Signal
from src.execution.costs import CostModel


def signals_to_orders(
    signals: list[Signal],
    portfolio: Portfolio,
    prices: dict[str, float],
    min_trade: float = 1.0,
    slippage: float = 0.0005,
    commission_rate: float = 0.0,
    cost: CostModel | None = None,
) -> list[Order]:
    # 브로커와 동일한 비용 모델(슬리피지+수수료)로 사이징 — 없으면 인자로 구성.
    cost = cost or CostModel(slippage=slippage, commission_rate=commission_rate)
    total = portfolio.total_value(prices)
    targets = {s.symbol: max(0.0, s.target_weight) for s in signals}
    reasons = {s.symbol: s.rationale for s in signals}

    sells: list[Order] = []
    buys: list[tuple[str, float, float]] = []   # (symbol, qty, price) — 삽입 순서 유지

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
        if diff_value > 0:
            buys.append((symbol, qty, price))
        else:
            sells.append(Order(symbol, Side.SELL, qty, reasons.get(symbol, "")))

    # 매도를 먼저 실행해 현금을 확보한 뒤 매수(현금 부족 방지)
    orders: list[Order] = list(sells)

    # 가용 현금 = 현재 현금 + 매도 예상 순대금(슬리피지·수수료 반영). 매수는 이 한도 안에서만.
    avail = portfolio.cash + sum(
        cost.sell_proceeds(o.quantity, prices[o.symbol]) for o in sells)

    for symbol, qty, price in buys:
        # 이 매수에 실제로 드는 현금이 남은 현금을 넘으면 살 수 있는 만큼으로 축소.
        if cost.buy_cost(qty, price) > avail:
            qty = cost.affordable_qty(avail, price)
        if qty * price < min_trade:   # 클립 후 먼지 거래면 건너뜀
            continue
        orders.append(Order(symbol, Side.BUY, qty, reasons.get(symbol, "")))
        avail -= cost.buy_cost(qty, price)
        if avail < min_trade:         # 남은 현금이 먼지면 이후 매수 중단
            break

    return orders
