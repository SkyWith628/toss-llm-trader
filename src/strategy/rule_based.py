"""모멘텀 규칙 전략 — LLM 붙이기 전의 베이스라인(대조 실력).

[What] 최근 lookback일 수익률 상위 top_k 종목을 동일비중 매수.
[Why]  '거래비용 감안해도 Buy&Hold를 이기는가?'의 하한선. LLM이 이걸 넘어야 의미.
[How]  단순 크로스섹션 모멘텀 — 실무 팩터의 가장 기본형.
"""
from __future__ import annotations

from src.core.types import Action, MarketSnapshot, Signal
from src.strategy.base import Strategy


class MomentumStrategy(Strategy):
    name = "momentum"

    def __init__(self, lookback: int = 20, top_k: int = 2) -> None:
        self.lookback = lookback
        self.top_k = top_k

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        scores: dict[str, float] = {}
        for sym, closes in snapshot.history.items():
            if len(closes) < self.lookback + 1:
                continue
            past = closes[-self.lookback - 1]
            now = closes[-1]
            if past > 0:
                scores[sym] = (now - past) / past

        # 양(+)의 모멘텀만, 상위 top_k
        ranked = sorted(
            (s for s in scores.items() if s[1] > 0),
            key=lambda kv: kv[1],
            reverse=True,
        )[: self.top_k]

        if not ranked:
            # 살 게 없으면 전량 현금(HOLD)
            return [Signal(sym, Action.HOLD, 0.0, rationale="양의 모멘텀 없음")
                    for sym in snapshot.prices]

        weight = 1.0 / len(ranked)
        chosen = {sym for sym, _ in ranked}
        signals = []
        for sym in snapshot.prices:
            if sym in chosen:
                signals.append(Signal(
                    sym, Action.BUY, weight, confidence=0.6,
                    timestamp=snapshot.timestamp,
                    rationale=f"모멘텀 {scores[sym]:+.2%}",
                ))
            else:
                signals.append(Signal(sym, Action.SELL, 0.0,
                                      timestamp=snapshot.timestamp,
                                      rationale="랭크 밖"))
        return signals
