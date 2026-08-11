"""CarveOutBroker — 공유 실계좌에서 '$100 실험 슬라이스'만 실제로 집행한다.

[What] TossBroker(실계좌)를 감싸, ①실계좌 시세·보유 조회 ②실험 장부(experiment_nav/cash/
       보유) 기준 다층 방어 ③멱등키로 실주문 전송 ④체결 폴링으로 실체결 정산(부분체결 포함).
[Why]  실계좌 get_portfolio()는 보호분+실험분을 합쳐서 준다. 파이프라인엔 실험 슬라이스만
       보여야 하고, 예산 방어도 계좌 전체가 아니라 experiment_nav 기준이어야 한다.
[How]  결정론 리밸런서·가드레일은 실험 장부(exp.to_portfolio)로 이미 처리되므로, 이 브로커는
       '실계좌로의 안전한 집행 + 실체결 회수'만 담당한다. 내부 TossBroker는 enforce_budget=False로
       두어(계좌전체 방어 끔) 여기서 experiment 기준으로 재방어한다.

주의: live 전송 여부는 내부 TossBroker의 live 플래그가 결정한다. 기본(paper)에서는 내부가
      dry-run을 반환하므로 실주문이 나가지 않는다. 대사(reconcile)·상태 영속·runner 배선은
      증분3b에서.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from src.core.types import Order, OrderResult, Side
from src.execution.costs import CostModel
from src.execution.toss_broker import TossApiError, TossBroker
from src.forward.experiment_portfolio import ExperimentPortfolio

_TOL = 1e-6


class CarveOutBroker:
    def __init__(
        self,
        inner: TossBroker,
        experiment: ExperimentPortfolio,
        *,
        cost: CostModel | None = None,
        poll_attempts: int = 5,
        poll_wait: float = 1.0,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.inner = inner
        self.exp = experiment
        self.cost = cost or CostModel()
        self.poll_attempts = poll_attempts
        self.poll_wait = poll_wait
        self._sleep = sleep or time.sleep

    # ---------- 조회 ----------
    def get_prices(self) -> dict[str, float]:
        return self.inner.get_prices()

    def account_holdings(self) -> dict[str, float]:
        """실계좌의 종목별 총 보유 수량(보호분+실험분+그 외). 대사(reconcile)의 입력."""
        pf = self.inner.get_portfolio()
        return {sym: pos.quantity for sym, pos in pf.positions.items()}

    # ---------- 집행 + 실체결 정산 ----------
    def place_and_settle(
        self, order: Order, client_order_id: str, when: datetime | None = None
    ) -> OrderResult:
        """실험 슬라이스 주문을 실계좌에 멱등 전송하고, 체결을 폴링해 실체결 결과를 돌려준다.

        client_order_id: 이 '의도한 주문'의 멱등키. 재시도 시 같은 값을 넘기면 중복주문 방지.
        """
        when = when or datetime.now()
        # 중단 상태(대사 필요/halted)면 어떤 신규 주문도 전송하지 않는다(fail-closed, 최우선).
        if not self.exp.can_trade:
            return self._reject(
                order, when, f"실험 거래 중단({self.exp.status}): {self.exp.status_reason}")
        try:
            prices = self.get_prices()
        except TossApiError as e:
            return self._reject(order, when, f"시세 조회 실패({e.status})")
        price = prices.get(order.symbol)
        if not price or price <= 0:
            return self._reject(order, when, "시세 없음")

        # --- 실험 장부 기준 다층 방어(계좌 전체가 아니라 experiment_cash/보유 기준) ---
        if order.side is Side.BUY:
            need = self.cost.buy_cost(order.quantity, price)
            if need > self.exp.cash + _TOL:
                return self._reject(order, when,
                                    f"실험 현금 초과 매수: 소요 {need:.2f} > 현금 {self.exp.cash:.2f}")
        else:
            sellable = self.exp.max_sellable(order.symbol)
            if order.quantity > sellable + _TOL:
                return self._reject(order, when,
                                    f"실험 보유 초과 매도: {order.quantity:.6f} > 실험 {sellable:.6f}")

        # --- 멱등 전송 ---
        submitted = self.inner.place_order(order, when=when, client_order_id=client_order_id)
        if submitted.status in ("dry-run", "rejected"):
            return submitted   # live=False면 미전송(dry-run), 또는 브로커 거부
        order_id = submitted.note
        if not order_id:
            return self._reject(order, when, "orderId 없음(전송 실패)")

        # --- 비동기 체결 정산 폴링(부분체결이면 filledQuantity만 반영됨) ---
        return self._settle(order_id, order, when)

    def _settle(self, order_id: str, order: Order, when: datetime) -> OrderResult:
        last: OrderResult | None = None
        for i in range(self.poll_attempts):
            try:
                res = self.inner.get_order(order_id)
            except TossApiError:
                res = None
            if res is not None:
                last = res
                if res.status in ("filled", "rejected"):
                    return res
            if i < self.poll_attempts - 1:
                self._sleep(self.poll_wait)
        # 타임아웃: 마지막으로 관측한 상태 반환(미확정=submitted). 미체결/부분은 증분3b의
        # 열린주문 장부로 관리 — 여기선 확정분(filled_qty)만 상위가 반영한다.
        return last or OrderResult(order, 0.0, 0.0, 0.0, "submitted", when, order_id)

    @staticmethod
    def _reject(order: Order, when: datetime, note: str) -> OrderResult:
        return OrderResult(order, 0.0, 0.0, 0.0, "rejected", when, note)
