"""전략 인터페이스 — 규칙·RL·LLM이 모두 이 하나를 구현한다.

[What] MarketSnapshot -> 목표 비중 신호 리스트.
[Why]  전략을 플러그인처럼 교체해 '동일 백테스트 환경'에서 공정 비교(규칙 vs LLM).
[How]  다음 단계의 LLMAgentStrategy도 이 시그니처만 지키면 드라이버 수정 없이 붙는다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.types import MarketSnapshot, Signal


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        """이 시점의 관측치로 목표 비중을 결정한다.

        반환한 target_weight의 합은 <= 1.0 (나머지는 현금).
        """
        raise NotImplementedError
