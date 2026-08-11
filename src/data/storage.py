"""가격 저장소 — as-of 조회로 point-in-time을 보장한다.

[What] (날짜 x 종목) 종가 패널을 담고, '어떤 날짜 이전'만 반환하는 조회를 제공.
[Why]  전략이 미래 값을 못 보게 저장소 수준에서 막는다.
[How]  실데이터(yfinance)든 합성데이터든 같은 저장소로 흘려보내 상위 코드를 통일.
"""
from __future__ import annotations

from datetime import datetime

from src.core.types import MarketSnapshot


class PriceStore:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)
        self.dates: list[datetime] = []
        # dates[i]에 대응하는 {symbol: close}
        self._rows: list[dict[str, float]] = []

    def add_row(self, date: datetime, prices: dict[str, float]) -> None:
        self.dates.append(date)
        self._rows.append(dict(prices))

    def prices_on(self, date: datetime) -> dict[str, float]:
        idx = self.dates.index(date)
        return dict(self._rows[idx])

    def history_until(self, date: datetime, lookback: int) -> dict[str, list[float]]:
        """date '이전(당일 포함)'의 최근 lookback개 종가. 미래 미포함."""
        cutoff = self.dates.index(date)
        start = max(0, cutoff - lookback + 1)
        hist: dict[str, list[float]] = {s: [] for s in self.symbols}
        for i in range(start, cutoff + 1):
            for s in self.symbols:
                if s in self._rows[i]:
                    hist[s].append(self._rows[i][s])
        return hist

    def snapshot(self, date: datetime, lookback: int = 60) -> MarketSnapshot:
        return MarketSnapshot(
            timestamp=date,
            prices=self.prices_on(date),
            history=self.history_until(date, lookback),
        )
