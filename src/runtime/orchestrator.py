"""Orchestrator — 실시간 일중 시스템의 심장(상시 루프).

[What] 하루 생애주기를 돈다: 개장 앵커 → 개장 1회 진입 결정 → 감시 루프(폴링·서킷 평가·
       발동 시 전량 청산) → 폐장 결산. run_forever는 개장 대기까지 감싼다.
[Why]  MarketClock·RealtimeFeed·DayRiskManager·IntradayState·Broker를 한 흐름으로 엮되,
       각 부품은 계약으로만 참조해 교체·테스트가 쉽도록(DIP) 한다.
[How]  clock/feed/broker/risk/state와 now_fn·sleep_fn을 주입 → 실제 시계·네트워크 없이
       리플레이 피드로 오프라인 테스트/드라이런. 진입 결정은 open_decider 콜백으로 분리
       (LLM이든 동일비중이든 교체 가능 — 서킷브레이커 루프는 전략에 무관).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from src.core.types import MarketSnapshot, Order, Side, Signal
from src.execution.rebalance import signals_to_orders
from src.market.feed import FeedExhausted, StaleFeedError
from src.risk.day_risk import DayRiskManager, RiskAction

# 개장 시 진입 결정: 개장 시세를 받아 목표 Signal 리스트를 반환(빈 리스트면 전량 현금).
OpenDecider = Callable[[dict], list[Signal]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Orchestrator:
    def __init__(
        self,
        *,
        symbols: list[str],
        clock,
        feed,
        broker,
        guardrail,
        risk: DayRiskManager,
        state,
        open_decider: OpenDecider,
        state_path: str,
        poll_interval: float = 10.0,
        now_fn: Callable[[], datetime] = _utcnow,
        sleep_fn: Callable[[float], None] = None,
    ) -> None:
        self.symbols = list(symbols)
        self.clock = clock
        self.feed = feed
        self.broker = broker
        self.guardrail = guardrail
        self.risk = risk
        self.state = state
        self.open_decider = open_decider
        self.state_path = state_path
        self.poll_interval = poll_interval
        self._now = now_fn
        self._sleep = sleep_fn or (lambda _s: None)

    # --- 집행 헬퍼 ---
    def _execute(self, signals: list[Signal]) -> None:
        prices = self.broker.get_prices()
        orders = signals_to_orders(signals, self.broker.get_portfolio(), prices)
        for order in orders:
            decision = self.guardrail.check(order, self.broker.get_portfolio(), prices)
            if decision.ok:
                self.broker.place_order(decision.order, when=self._now())

    def _liquidate_all(self) -> None:
        """서킷 발동 시 보유 전량 시장가 매도(현금화)."""
        prices = self.broker.get_prices()
        for sym, pos in list(self.broker.get_portfolio().positions.items()):
            if prices.get(sym):
                self.broker.place_order(
                    Order(sym, Side.SELL, pos.quantity, "circuit_breaker"),
                    when=self._now())

    def _equity(self, prices: dict) -> float:
        return self.broker.get_portfolio().total_value(prices)

    # --- 하루 실행 ---
    def run_trading_day(self) -> dict:
        """오늘 하루를 실행하고 요약을 반환. 재시작이면 앵커·halted를 복원하고 이어간다."""
        open_px = self.feed.quotes()          # 개장 시세(첫 틱) — 실패 시 예외 전파
        self.broker.set_prices(open_px)
        today = self.clock.local_date(self._now())

        if self.state.is_new_day(today):
            start_equity = self._equity(open_px)          # 진입 전 = 당일 시작 자산
            self.state.start_new_day(today, start_equity)
            self.risk.anchor(start_equity)
            self._execute(self.open_decider(open_px))     # 개장 1회 진입
            self.state.set_from_portfolio(self.broker.get_portfolio())
            self.state.save(self.state_path)
        else:  # 같은 날 재시작 — 앵커 복원(재앵커·재진입 금지)
            self.risk.anchor(self.state.day_start_equity)

        peak, trough, ticks = 0.0, 0.0, 0
        last_px = open_px

        # 감시 루프
        while not self.state.halted:
            if not self.clock.is_open(self._now()):
                break
            try:
                px = self.feed.quotes()
            except FeedExhausted:
                break                          # 리플레이 소진 = 폐장
            except StaleFeedError:
                self._sleep(self.poll_interval)
                continue                       # stale → 이 틱 판단 건너뜀(보수적)

            self.broker.set_prices(px)
            last_px = px
            ticks += 1
            ret = self.risk.daily_return(self._equity(px))
            peak, trough = max(peak, ret), min(trough, ret)

            action = self.risk.evaluate(self._equity(px))
            if action is not RiskAction.NORMAL:
                self._liquidate_all()
                self.state.mark_halt(action.value)
                self.state.set_from_portfolio(self.broker.get_portfolio())

            self.state.touch(self._now().isoformat())
            self.state.save(self.state_path)
            self._sleep(self.poll_interval)

        # 폐장 결산
        close_equity = self._equity(last_px)
        self.state.record_daily_close(today, close_equity)
        self.state.set_from_portfolio(self.broker.get_portfolio())
        self.state.save(self.state_path)

        return {
            "date": today,
            "ticks": ticks,
            "day_start_equity": round(self.state.day_start_equity, 4),
            "close_equity": round(close_equity, 4),
            "peak_return": round(peak, 4),
            "trough_return": round(trough, 4),
            "halted": self.state.halted,
            "halt_reason": self.state.halt_reason,
        }

    # --- 상시 루프(라이브) ---
    def run_forever(self) -> None:  # pragma: no cover (실시간 대기 — 오프라인 테스트 불가)
        while True:
            wait = self.clock.seconds_to_open(self._now())
            if wait > 0:
                self._sleep(wait)
            self.run_trading_day()
