"""공통 비용 모델 — 리밸런서와 브로커가 '같은' 슬리피지·수수료 가정을 쓰게 한다.

[What] 매수 소요 현금·최대 매수 수량·매도 순대금을 슬리피지+비례수수료+고정수수료로 계산.
[Why]  리밸런서가 슬리피지만 보고 사이징하면, 수수료가 있는 브로커에서 실제 비용이 현금을
       초과해 주문이 거부될 수 있다(Codex 지적: 테스트7). 두 계층이 동일 모델을 공유해야 한다.
[How]  순수 함수. 표준 라이브러리만.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    slippage: float = 0.0005        # 체결가 밀림(매수 불리↑, 매도 불리↓)
    commission_rate: float = 0.0    # 거래대금 비례 수수료
    fixed_fee: float = 0.0          # 건당 고정 수수료

    def buy_cost(self, qty: float, price: float) -> float:
        """매수 시 실제로 나가는 현금(슬리피지·수수료 포함)."""
        notional = qty * price * (1 + self.slippage)
        return notional * (1 + self.commission_rate) + self.fixed_fee

    def affordable_qty(self, cash: float, price: float) -> float:
        """주어진 현금으로 살 수 있는 최대 수량(수수료·슬리피지 여유 반영). 역산."""
        if price <= 0:
            return 0.0
        avail = max(0.0, cash - self.fixed_fee)
        notional = avail / (1 + self.commission_rate)   # 비례수수료 제외
        return (notional / (1 + self.slippage)) / price  # 슬리피지 제외 → 수량

    def sell_proceeds(self, qty: float, price: float) -> float:
        """매도 시 실험 현금에 들어오는 순대금(슬리피지·수수료 차감). 추정용."""
        notional = qty * price * (1 - self.slippage)
        return notional * (1 - self.commission_rate) - self.fixed_fee
