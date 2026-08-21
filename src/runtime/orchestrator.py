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

# 장중 진입 타이밍 결정: point-in-time 스냅샷(현재가+현재 이전 종가)을 받아, '지금 들어갈'
# 목표 Signal을 반환(빈 리스트면 아직 대기 → 다음 주기에 재판단). LLMAgentStrategy.generate_signal이
# 이 계약을 그대로 만족하므로 어댑터 없이 진입 decider로 꽂힌다.
EntryDecider = Callable[[MarketSnapshot], list[Signal]]


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
        open_decider: OpenDecider | None = None,
        entry_decider: EntryDecider | None = None,
        entry_interval: int = 1,
        max_entry_calls: int = 12,
        state_path: str,
        poll_interval: float = 10.0,
        now_fn: Callable[[], datetime] = _utcnow,
        sleep_fn: Callable[[float], None] = None,
    ) -> None:
        if open_decider is None and entry_decider is None:
            raise ValueError("open_decider(개장 진입) 또는 entry_decider(장중 타이밍) 중 하나는 필요")
        self.symbols = list(symbols)
        self.clock = clock
        self.feed = feed
        self.broker = broker
        self.guardrail = guardrail
        self.risk = risk
        self.state = state
        self.open_decider = open_decider
        self.entry_decider = entry_decider      # None이면 개장 진입 모드, 있으면 장중 타이밍 모드
        self.entry_interval = max(1, entry_interval)
        self.max_entry_calls = max_entry_calls
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
        """오늘 하루를 실행하고 요약을 반환. 재시작이면 앵커·halted를 복원하고 이어간다.

        두 진입 모드:
        - 개장 진입(entry_decider 없음): 개장 시세로 open_decider 1회 진입.
        - 장중 타이밍(entry_decider 있음): 현금으로 시작, 감시 루프가 주기적으로 진입을 판단.
        어느 쪽이든 출구(익절/손절 서킷브레이커)는 동일하게 결정론적으로 동작한다.
        """
        open_px = self.feed.quotes()          # 개장 시세(첫 틱) — 실패 시 예외 전파
        self.broker.set_prices(open_px)
        today = self.clock.local_date(self._now())
        timing_mode = self.entry_decider is not None

        if self.state.is_new_day(today):
            start_equity = self._equity(open_px)          # 진입 전 = 당일 시작 자산
            self.state.start_new_day(today, start_equity)
            self.risk.anchor(start_equity)
            if not timing_mode:
                self._execute(self.open_decider(open_px))  # 개장 1회 진입
            # timing_mode면 현금 대기 — 진입은 감시 루프가 판단
            self.state.set_from_portfolio(self.broker.get_portfolio())
            self.state.save(self.state_path)
        else:  # 같은 날 재시작 — 앵커 복원(재앵커·재진입 금지)
            self.risk.anchor(self.state.day_start_equity)

        # 장중 종가 버퍼(진입 판단은 '현재 이전' 종가만 본다 — 누수 방지). 개장 틱으로 시드.
        intraday: dict[str, list[float]] = {s: [] for s in self.symbols}
        for s in self.symbols:
            if open_px.get(s):
                intraday[s].append(float(open_px[s]))

        peak, trough, ticks, entry_calls = 0.0, 0.0, 0, 0
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

            # 1) 서킷브레이커 — 항상 먼저 평가(진입 여부와 무관하게 하방 보호)
            action = self.risk.evaluate(self._equity(px))
            if action is not RiskAction.NORMAL:
                self._liquidate_all()
                self.state.mark_halt(action.value)
                self.state.set_from_portfolio(self.broker.get_portfolio())

            # 2) 진입 판단 — 타이밍 모드 · 미진입 · 미halt · 호출상한 내 · 주기 도달
            entered = bool(self.broker.get_portfolio().positions)
            if (timing_mode and not entered and not self.state.halted
                    and entry_calls < self.max_entry_calls
                    and ticks % self.entry_interval == 0):
                snap = MarketSnapshot(
                    timestamp=self._now(),
                    prices=dict(px),
                    history={s: list(intraday[s]) for s in self.symbols},  # 현재 이전만
                )
                entry_calls += 1
                signals = self.entry_decider(snap)
                if signals:                    # 빈 결정이면 대기 → 다음 주기 재판단
                    self._execute(signals)
                    self.state.set_from_portfolio(self.broker.get_portfolio())

            # 현재 틱 종가를 버퍼에 추가(다음 판단부터 '과거'로 보이게)
            for s in self.symbols:
                if px.get(s):
                    intraday[s].append(float(px[s]))

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
            "entered": bool(self.broker.get_portfolio().positions) or self.state.halted,
            "entry_calls": entry_calls,
        }

    # --- 상시 루프(라이브) ---
    def run_forever(self) -> None:  # pragma: no cover (실시간 대기 — 오프라인 테스트 불가)
        while True:
            wait = self.clock.seconds_to_open(self._now())
            if wait > 0:
                self._sleep(wait)
            self.run_trading_day()
