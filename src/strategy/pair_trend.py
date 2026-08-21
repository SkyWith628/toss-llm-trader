"""PairTrendStrategy — 인버스 페어의 결정론적 일별 위성 전략(추세추종).

[What] 두 레그(예 SOXL 3x롱 / SOXS 3x숏) 중 최근 모멘텀이 강한 쪽 '하나만' 목표비중 1.0.
       둘 다 음(-)이면 현금. 방향은 한 번에 하나(상호배타) — 페어의 핵심 안전규칙.
[Why]  메타-포트폴리오에 '고변동 위성' 슬리브를 붙여 역변동성 배분(InverseVolAllocator)이
       실제로 3x 슬리브에 자본을 적게 주는지 시험한다. LLM 없이(비용 0) 결정론으로.
[How]  최근 lookback일 수익률로 레그를 고르고, pair_gate.mutual_exclusive로 한 방향만 남긴다.
       LLM 페어 결정(방향=알파)의 결정론적 대리 — 진짜 방향판단은 LLMAgentStrategy 몫.
       설계: docs/design-meta-portfolio.md · docs/design-leveraged-pair.md
"""
from __future__ import annotations

from src.core.types import Action, MarketSnapshot, Signal
from src.strategy.base import Strategy
from src.strategy.pair_gate import mutual_exclusive


class PairTrendStrategy(Strategy):
    name = "pair_trend"

    def __init__(self, legs: list[str], lookback: int = 20) -> None:
        if len(legs) != 2:
            raise ValueError(f"페어는 정확히 2개 레그: {legs}")
        self.legs = list(legs)
        self.lookback = max(1, lookback)

    def _momentum(self, closes: list[float]) -> float | None:
        """최근 lookback일 수익률. 전체 lookback 미확보면 None(노이즈 진입 방지)."""
        if len(closes) < self.lookback + 1:
            return None
        past = closes[-self.lookback - 1]
        if past <= 0:
            return None
        return closes[-1] / past - 1.0

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        # 각 레그의 모멘텀 → 양(+)만 후보 비중으로(음수·결측은 0)
        raw: dict[str, float] = {}
        for leg in self.legs:
            m = self._momentum(snapshot.history.get(leg, []))
            raw[leg] = m if (m is not None and m > 0) else 0.0

        keep = mutual_exclusive(raw)                 # 한 방향만(동률/무양수 → 전부 0)
        winner = next((s for s, w in keep.items() if w > 0), None)

        signals: list[Signal] = []
        for leg in self.legs:
            if leg == winner:
                signals.append(Signal(leg, Action.BUY, 1.0, confidence=0.55,
                                      timestamp=snapshot.timestamp,
                                      rationale=f"페어 추세 {raw[leg]:+.2%}"))
            else:
                signals.append(Signal(leg, Action.SELL, 0.0,
                                      timestamp=snapshot.timestamp,
                                      rationale="반대 레그 → 현금"))
        return signals
