"""모의(페이퍼) 브로커 — 돈 안 잃고 파이프라인·전략을 검증.

[What] 메모리상의 현금·보유로 시장가 체결을 시뮬레이션(수수료·슬리피지·소수점 반영).
[Why]  $100 실험의 최대 적은 '비용'이므로, 비용을 처음부터 현실적으로 넣어야
       '종이수익'이 아닌 '실수익'을 측정한다.
[How]  실거래 TossBroker와 동일한 BrokerClient 계약 → 검증된 코드가 그대로 실거래로.
"""
from __future__ import annotations

from datetime import datetime

from src.core.types import (Order, OrderResult, Portfolio, Position, Side)
from src.execution.broker_base import BrokerClient


class PaperBroker(BrokerClient):
    def __init__(
        self,
        starting_cash: float = 100.0,
        commission_per_trade: float = 0.0,   # 미국 주식은 대개 건당 0, 스프레드가 실비용
        commission_rate: float = 0.0,        # 거래대금 비례 수수료(예: 0.0005)
        slippage: float = 0.0005,            # 체결가 밀림(0.05%)
        min_notional: float = 1.0,           # 이보다 작은 주문은 무시(먼지 거래 방지)
    ) -> None:
        self.portfolio = Portfolio(cash=starting_cash)
        self.commission_per_trade = commission_per_trade
        self.commission_rate = commission_rate
        self.slippage = slippage
        self.min_notional = min_notional
        self._prices: dict[str, float] = {}
        self.total_commission_paid = 0.0

    # --- 시장 데이터 주입(실거래에선 API가 대신함) ---
    def set_prices(self, prices: dict[str, float]) -> None:
        self._prices = dict(prices)

    def get_prices(self) -> dict[str, float]:
        return dict(self._prices)

    def get_portfolio(self) -> Portfolio:
        return self.portfolio

    def place_order(self, order: Order, when: datetime | None = None) -> OrderResult:
        when = when or datetime.now()
        ref = self._prices.get(order.symbol)
        if ref is None:
            return self._reject(order, when, "시세 없음")

        # 시장가 슬리피지: 매수는 불리하게↑, 매도는 불리하게↓
        fill_price = ref * (1 + self.slippage) if order.side is Side.BUY \
            else ref * (1 - self.slippage)
        notional = order.quantity * fill_price
        if notional < self.min_notional:
            return self._reject(order, when, f"주문 금액 {notional:.2f} < 최소 {self.min_notional}")

        commission = self.commission_per_trade + notional * self.commission_rate

        if order.side is Side.BUY:
            cost = notional + commission
            if cost > self.portfolio.cash + 1e-9:
                return self._reject(order, when, "현금 부족")
            self.portfolio.cash -= cost
            self._add_position(order.symbol, order.quantity, fill_price)
        else:  # SELL
            pos = self.portfolio.positions.get(order.symbol)
            if pos is None or order.quantity > pos.quantity + 1e-9:
                return self._reject(order, when, "보유 수량 부족")
            self.portfolio.cash += notional - commission
            pos.quantity -= order.quantity
            if pos.quantity <= 1e-9:
                del self.portfolio.positions[order.symbol]

        self.total_commission_paid += commission
        return OrderResult(order, order.quantity, round(fill_price, 4),
                           round(commission, 6), "filled", when)

    # --- 내부 헬퍼 ---
    def _add_position(self, symbol: str, qty: float, price: float) -> None:
        pos = self.portfolio.positions.get(symbol)
        if pos is None:
            self.portfolio.positions[symbol] = Position(symbol, qty, price)
        else:
            total_qty = pos.quantity + qty
            pos.avg_price = (pos.avg_price * pos.quantity + price * qty) / total_qty
            pos.quantity = total_qty

    def _reject(self, order: Order, when: datetime, note: str) -> OrderResult:
        return OrderResult(order, 0.0, 0.0, 0.0, "rejected", when, note)
