"""얇은 안전판 — LLM의 '판단'은 건드리지 않고 '형식 오류·물리적 불가능'만 막는다.

[What] 주문의 불변식(invariant)을 검사: 수량>0, 시세 존재, 현금 초과 매수 금지(무레버리지),
       보유 초과 매도 금지.
[Why]  전권 LLM 실험이라도 잔고 초과 주문 하나로 API 에러 루프에 빠지면 자동 운영이 깨진다.
       이 계층은 알파(무엇을·언제·얼마)를 판단하지 않는다 — 오직 안전 하한선.
       주의: '최초 예산 초과 노출'을 막던 고정 상한은 제거했다. 그 상한은 평가이익으로
       NAV가 초기자본을 넘으면 재매수를 차단해 '복리 재투자'를 막았다(측정을 아래로 편향).
       외부자금·레버리지 방지는 '가진 현금으로만 매수'(현금 한도)로 이미 보장된다.
       (실계좌 공유 시의 보호분 격리·experiment_nav 상한은 후속 단계에서 별도 계층으로.)
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
        # budget: 실험 시작 자본(참고용). 고정 상한으로 쓰지 않는다 — NAV 복리 재투자 허용.
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
            # 가진 현금으로만 매수 → 무레버리지·무외부자금 + 주문 후 노출 <= NAV 자동 성립.
            # (평가이익으로 NAV가 초기자본을 넘어도 재투자 허용: 복리)
            if cost > portfolio.cash + 1e-9:
                return GuardrailDecision(None, False, "현금 초과 매수")
        else:  # SELL
            pos = portfolio.positions.get(order.symbol)
            held = pos.quantity if pos else 0.0
            if order.quantity > held + 1e-9:
                return GuardrailDecision(None, False, "보유 초과 매도")

        return GuardrailDecision(order, True, "ok")
