"""Orchestrator 단위 테스트 — 서킷브레이커 하루 루프를 리플레이 피드로 오프라인 검증.

실행: python -m pytest -q  (pytest 없으면 python tests/test_orchestrator.py)

검증 포인트:
- 손절/익절 임계 도달 시 전량 청산 + 당일 halt + 루프 종료(남은 틱 미소비)
- 밴드 내면 폐장까지 완주 · stale 틱은 건너뛰고 계속
- 같은 날 재시작(halted) 시 재진입 없음
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, Signal
from src.execution.paper_broker import PaperBroker
from src.market.feed import ReplayFeed, StaleFeedError
from src.risk.day_risk import DayRiskManager
from src.risk.guardrail import Guardrail
from src.runtime.orchestrator import Orchestrator
from src.runtime.state import IntradayState

_SYMBOLS = ["AAA"]
_NOW = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)


class _AlwaysOpenClock:
    """리플레이용 — 항상 개장, 고정 날짜. 루프 종료는 FeedExhausted가 담당."""
    def __init__(self, date="2026-07-06"):
        self._date = date

    def is_open(self, now):
        return True

    def local_date(self, now):
        return self._date

    def seconds_to_open(self, now):
        return 0.0


def _invest_90pct(prices: dict) -> list[Signal]:
    """총 90% 투자(슬리피지·먼지 여유로 현금 10% 버퍼)."""
    n = len(prices)
    return [Signal(s, Action.BUY, 0.9 / n) for s in prices]


def _orch(ticks, decider=_invest_90pct, state=None, budget=1000.0):
    path = os.path.join(tempfile.mkdtemp(prefix="orch_"), "state.json")
    broker = PaperBroker(starting_cash=budget,
                         portfolio=state.to_portfolio() if state else None)
    return Orchestrator(
        symbols=_SYMBOLS,
        clock=_AlwaysOpenClock(),
        feed=ReplayFeed(ticks),
        broker=broker,
        guardrail=Guardrail(budget=budget, valid_symbols=set(_SYMBOLS)),
        risk=DayRiskManager(),
        state=state or IntradayState.new(budget, _SYMBOLS),
        open_decider=decider,
        state_path=path,
        poll_interval=0.0,
        now_fn=lambda: _NOW,
    )


def test_stop_loss_fires_liquidates_and_halts():
    # 개장 100 매수 → 98(-2%) 도달 시 손절. 마지막 97 틱은 소비 안 됨.
    ticks = [{"AAA": 100.0}, {"AAA": 100.5}, {"AAA": 98.0}, {"AAA": 97.0}]
    orch = _orch(ticks)
    r = orch.run_trading_day()
    assert r["halted"] is True and r["halt_reason"] == "stop_loss"
    assert orch.broker.get_portfolio().positions == {}      # 전량 청산
    assert r["ticks"] == 2                                   # 100.5, 98.0만 처리
    assert orch.feed.remaining == 1                          # 97.0 미소비
    assert r["trough_return"] <= -0.01


def test_take_profit_fires():
    ticks = [{"AAA": 100.0}, {"AAA": 101.0}, {"AAA": 102.5}, {"AAA": 103.0}]
    orch = _orch(ticks)
    r = orch.run_trading_day()
    assert r["halted"] is True and r["halt_reason"] == "take_profit"
    assert orch.broker.get_portfolio().positions == {}
    assert r["peak_return"] >= 0.02


def test_no_breach_runs_to_close():
    # 밴드(-1%~+2%) 내에서만 움직임 → 폐장까지 완주, halt 없음.
    ticks = [{"AAA": 100.0}, {"AAA": 100.5}, {"AAA": 101.0}, {"AAA": 100.8}]
    orch = _orch(ticks)
    r = orch.run_trading_day()
    assert r["halted"] is False
    assert r["ticks"] == 3                                   # 개장 제외 3틱 완주
    assert orch.broker.get_portfolio().positions != {}       # 보유 유지
    assert orch.state.equity_curve()                         # 종가자산 기록됨


def test_stale_tick_is_skipped():
    """중간에 stale이 나도 크래시/오작동 없이 다음 틱으로 계속."""
    class _FeedWithStale:
        def __init__(self):
            self._seq = [{"AAA": 100.0}, "STALE", {"AAA": 100.5}, {"AAA": 100.8}]
            self._i = 0

        @property
        def remaining(self):
            return len(self._seq) - self._i

        def quotes(self):
            if self._i >= len(self._seq):
                from src.market.feed import FeedExhausted
                raise FeedExhausted("end")
            item = self._seq[self._i]
            self._i += 1
            if item == "STALE":
                raise StaleFeedError("일시적 불량")
            return dict(item)

    path = os.path.join(tempfile.mkdtemp(prefix="orch_"), "s.json")
    orch = Orchestrator(
        symbols=_SYMBOLS, clock=_AlwaysOpenClock(), feed=_FeedWithStale(),
        broker=PaperBroker(starting_cash=1000.0),
        guardrail=Guardrail(budget=1000.0, valid_symbols=set(_SYMBOLS)),
        risk=DayRiskManager(), state=IntradayState.new(1000.0, _SYMBOLS),
        open_decider=_invest_90pct, state_path=path, poll_interval=0.0,
        now_fn=lambda: _NOW,
    )
    r = orch.run_trading_day()
    assert r["halted"] is False           # 밴드 내 + stale 건너뜀 → 정상 완주


def test_restart_same_day_no_reentry():
    """이미 오늘 시작·halted 상태로 재시작하면 재진입하지 않는다."""
    st = IntradayState.new(1000.0, _SYMBOLS)
    st.start_new_day("2026-07-06", 1000.0)
    st.mark_halt("stop_loss")             # 이미 오늘 청산됨

    calls = {"n": 0}
    def _decider(prices):
        calls["n"] += 1
        return _invest_90pct(prices)

    orch = _orch([{"AAA": 100.0}, {"AAA": 100.5}], decider=_decider, state=st)
    r = orch.run_trading_day()
    assert calls["n"] == 0                 # 개장 결정 호출 안 됨(재진입 없음)
    assert r["halted"] is True
    assert orch.broker.get_portfolio().positions == {}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 Orchestrator 테스트 통과")
