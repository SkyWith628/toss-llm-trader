"""브로커 추상 인터페이스 — 토스/모의/타 증권사를 같은 계약으로.

[What] 잔고·보유·시세·주문을 표준 메서드로 노출.
[Why]  토스 API 스펙이 유동적이어도 상위 로직은 이 인터페이스에만 의존(변경 격리).
[How]  다음 단계에서 TossBroker(REST)만 새로 구현하면 드라이버는 그대로.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.types import Order, OrderResult, Portfolio


class BrokerClient(ABC):
    @abstractmethod
    def get_portfolio(self) -> Portfolio: ...

    @abstractmethod
    def get_prices(self) -> dict[str, float]:
        """현재 체결 기준가."""

    @abstractmethod
    def place_order(self, order: Order) -> OrderResult: ...
