"""OpenDecider 어댑터 — Strategy(MarketSnapshot→Signal)를 개장 결정 콜백으로 변환.

[What] 오케스트레이터의 개장 시세 dict({sym: price})를 MarketSnapshot으로 조립해
       LLMAgentStrategy(또는 어떤 Strategy든)에 넘기고, 그 목표 비중을 그대로 반환한다.
[Why]  서킷브레이커 감시 루프는 '개장에 한 번' 결정한 뒤 하루 종일 시세로만 청산을 판단한다
       (전략 무관). 그 '개장 1회 결정'만 전략에 위임하려면, 시세 dict밖에 못 받는
       OpenDecider 계약과 MarketSnapshot을 요구하는 Strategy 계약 사이에 얇은 어댑터가 필요하다.
[How]  빠진 조각은 history 하나뿐 — 개장가 dict엔 과거 종가가 없다. history_provider를 주입해
       (DIP) yfinance·PriceStore·mock 무엇으로든 채운다. 전략이 []를 뱉으면(API 실패 폴백)
       그대로 [] → 오케스트레이터는 전량 현금으로 안전하게 시작한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from src.core.types import MarketSnapshot, Signal
from src.strategy.base import Strategy

# 과거 종가만 반환: {symbol: [오래된→최신 종가]}. 개장 dict가 못 주는 유일한 조각.
HistoryProvider = Callable[[], dict[str, list[float]]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LLMOpenDecider:
    """Strategy를 오케스트레이터의 open_decider(Callable[[dict], list[Signal]])로 적응."""

    def __init__(
        self,
        strategy: Strategy,
        history_provider: HistoryProvider,
        *,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.strategy = strategy
        self._history_provider = history_provider
        self._now = now_fn

    def __call__(self, open_px: dict[str, float]) -> list[Signal]:
        """개장 시세로 MarketSnapshot을 조립해 전략에 위임한다."""
        snapshot = MarketSnapshot(
            timestamp=self._now(),
            prices=dict(open_px),
            history=self._history_provider(),
        )
        return self.strategy.generate_signal(snapshot)
