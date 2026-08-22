"""전 계층이 공유하는 데이터 계약(contract).

[What] 전략·안전판·브로커·원장이 주고받는 값 객체(Signal/Order/...)를 한곳에 정의.
[Why]  상위 로직이 '토스 API'나 '특정 모델'이 아니라 이 계약에만 의존하도록(DIP) 해서,
       브로커/전략을 교체해도 회귀가 없게 한다.
[How]  퀀트 실무에서 alpha 모델·집행·리스크를 느슨하게 결합할 때 쓰는 방식.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Action(str, Enum):
    """전략이 내리는 방향 판단."""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Side(str, Enum):
    """실제 주문의 방향."""
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class MarketSnapshot:
    """어떤 시점(as-of)의 시장 관측치. 미래 데이터는 절대 담기지 않는다.

    prices  : 종목 -> 현재가
    history : 종목 -> 과거 종가 리스트(오래된→최신), '현재 이전'만 포함(누수 방지).
    """
    timestamp: datetime
    prices: dict[str, float]
    history: dict[str, list[float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    """전략의 출력. LLM/규칙/RL이 공통으로 뱉는 매매 의사."""
    symbol: str
    action: Action
    target_weight: float   # 0.0~1.0, 포트폴리오 내 목표 비중(연속 행동)
    confidence: float = 0.5  # 0~1, 안전판이 사이징 참고
    timestamp: datetime | None = None
    rationale: str = ""    # LLM 근거 등 — 실험 분석의 핵심 데이터


@dataclass(frozen=True)
class Order:
    """집행 계층으로 넘어가는 구체 주문."""
    symbol: str
    side: Side
    quantity: float        # 소수점 주식 허용(소액 예산 실험에 필수)
    reason: str = ""


@dataclass(frozen=True)
class OrderResult:
    """체결 결과."""
    order: Order
    filled_qty: float
    fill_price: float
    commission: float
    status: str            # "filled" | "rejected"
    timestamp: datetime
    note: str = ""


@dataclass
class Position:
    """보유 종목."""
    symbol: str
    quantity: float
    avg_price: float

    def market_value(self, price: float) -> float:
        return self.quantity * price


@dataclass
class Portfolio:
    """현금 + 보유. 가치 평가·비중 계산의 단일 출처."""
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def total_value(self, prices: dict[str, float]) -> float:
        held = sum(p.market_value(prices.get(sym, p.avg_price))
                   for sym, p in self.positions.items())
        return self.cash + held

    def weight_of(self, symbol: str, prices: dict[str, float]) -> float:
        total = self.total_value(prices)
        if total <= 0 or symbol not in self.positions:
            return 0.0
        return self.positions[symbol].market_value(prices[symbol]) / total
