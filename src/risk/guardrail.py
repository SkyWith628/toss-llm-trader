"""얇은 안전판 — LLM의 '판단'은 건드리지 않고 '형식 오류·물리적 불가능'만 막는다.

[What] 주문의 불변식(invariant)을 검사: 수량>0, 시세 존재, 현금/보유 초과 금지, $100 예산 초과 금지.
[Why]  전권 LLM 실험이라도 잔고 초과 주문 하나로 API 에러 루프에 빠지면 자동 운영이 깨진다.
       이 계층은 알파(무엇을·언제·얼마)를 판단하지 않는다 — 오직 안전 하한선.
[How]  place_order 직전에 통과. 실패한 주문은 사유와 함께 원장에 남겨 실험 분석에 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.types import Order, Portfolio, Side


@dataclass
class GuardrailDecision:
    order: Order | None       # None이면 차단
    ok: bool
    reason: str = ""


class Guardrail:
    def __init__(self, budget: float, valid_symbols: set[str]) -> None:
        self.budget = budget
        self.valid_symbols = valid_symbols

    def check(self, order: Order, portfolio: Portfolio,
              prices: dict[str, float]) -> GuardrailDecision:
        if order.quantity <= 0:
            return GuardrailDecision(None, False, "수량<=0")
        if order.symbol not in self.valid_symbols:
            return GuardrailDecision(None, False, f"미허용 종목 {order.symbol}")
        price = prices.get(order.symbol)
        if price is None or price <= 0:
            return GuardrailDecision(None, False, "시세 없음")

        if order.side is Side.BUY:
            cost = order.quantity * price
            if cost > portfolio.cash + 1e-9:
                return GuardrailDecision(None, False, "현금 초과 매수")
            # $100 예산(총 노출) 초과 방지
            total = portfolio.total_value(prices)
            if total > self.budget * 1.001:
                return GuardrailDecision(None, False, "예산 초과 노출")
        else:  # SELL
            pos = portfolio.positions.get(order.symbol)
            held = pos.quantity if pos else 0.0
            if order.quantity > held + 1e-9:
                return GuardrailDecision(None, False, "보유 초과 매도")

        return GuardrailDecision(order, True, "ok")
