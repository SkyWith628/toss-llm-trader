"""규칙 기반 전략 — LLM 붙이기 전의 베이스라인(대조 실력).

[What] MomentumStrategy(모멘텀 상위 매수) + BuyAndHoldStrategy(동일비중 상시보유).
[Why]  '거래비용 감안해도 Buy&Hold를 이기는가?'의 하한선. LLM이 이걸 넘어야 의미.
       메타-포트폴리오의 '코어' 슬리브로도 B&H가 쓰인다(안정 성분).
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


class BuyAndHoldStrategy(Strategy):
    """동일비중 상시보유 — 메타-포트폴리오의 '코어(안정)' 슬리브.

    [What] 유니버스 전 종목을 동일비중(1/N)으로 목표. 매일 같은 목표라 리밸런서가 드리프트만
           소폭 교정(사실상 보유).
    [Why]  단독 B&H 벤치마크(benchmark.buy_and_hold_equity)는 '측정용 곡선'일 뿐 Strategy가
           아니다. 메타 배분의 코어 성분으로 쓰려면 Strategy 계약을 만족하는 구현이 필요.
    [How]  history가 없어도 동작(현재가만으로 비중 결정) — 워밍업 의존 0.
    """
    name = "buy_and_hold"

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        syms = list(snapshot.prices)
        if not syms:
            return []
        weight = 1.0 / len(syms)
        return [Signal(sym, Action.BUY, weight, confidence=0.5,
                       timestamp=snapshot.timestamp, rationale="동일비중 보유")
                for sym in syms]
