"""Buy&Hold 대조군 — '전략 실력'을 '그냥 시장 상승'과 분리한다.

[What] 첫날 예산으로 대상 종목을 동일비중 매수 후 끝까지 보유한 자본 곡선.
[Why]  전략이 이걸 못 이기면 '그냥 사서 들고 있느니만 못한' 것. 실험의 기준선.
[How]  동일한 수수료·슬리피지를 진입 시 1회 적용해 공정 비교.
"""
from __future__ import annotations

from src.data.storage import PriceStore


def buy_and_hold_equity(
    store: PriceStore,
    symbols: list[str],
    budget: float = 100.0,
    slippage: float = 0.0005,
) -> list[float]:
    first = store.dates[0]
    p0 = store.prices_on(first)
    per_symbol_cash = budget / len(symbols)
    shares = {s: per_symbol_cash / (p0[s] * (1 + slippage)) for s in symbols}

    equity = []
    for date in store.dates:
        prices = store.prices_on(date)
        equity.append(sum(shares[s] * prices[s] for s in symbols))
    return equity
