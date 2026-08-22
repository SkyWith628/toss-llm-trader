"""장중 진입 타이밍 단위 테스트 — 결정론적 decider로 오프라인 검증.

실행: python -m pytest -q  (pytest 없으면 python tests/test_entry_timing.py)

검증 포인트:
- 타이밍 모드는 개장에 진입하지 않고 현금으로 시작 → decider가 신호를 낼 때 진입(딥 매수 확인)
- enter-once: 진입 후에는 재판단하지 않음
- max_entry_calls: 계속 대기하면 호출 상한에서 멈추고 미진입(현금)으로 종료
- point-in-time: snapshot.history는 '현재 이전' 종가만(현재 틱 미포함, 누수 방지)
- 진입 후에도 서킷브레이커가 그대로 하방을 보호(손절 청산)
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, Signal
from src.execution.paper_broker import PaperBroker
from src.market.feed import ReplayFeed
from src.risk.day_risk import DayRiskManager
from src.risk.guardrail import Guardrail
from src.runtime.orchestrator import Orchestrator
from src.runtime.state import IntradayState

_SYMS = ["AAA"]
_NOW = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)


class _Clock:
    def is_open(self, now): return True
    def local_date(self, now): return "2026-07-06"
    def seconds_to_open(self, now): return 0.0


def _orch(ticks, decider, *, interval=1, cap=12, budget=1000.0, stop=-0.5, take=0.5):
    broker = PaperBroker(starting_cash=budget)
    return Orchestrator(
        symbols=_SYMS, clock=_Clock(), feed=ReplayFeed(ticks), broker=broker,
        guardrail=Guardrail(budget=budget, valid_symbols=set(_SYMS)),
        risk=DayRiskManager(take_profit=take, stop_loss=stop),
        state=IntradayState.new(budget, _SYMS),
        entry_decider=decider, entry_interval=interval, max_entry_calls=cap,
        state_path=os.path.join(tempfile.mkdtemp(), "s.json"),
        poll_interval=0.0, now_fn=lambda: _NOW,
    ), broker


def _dip_decider(threshold=0.02):
    """개장가 대비 threshold 이상 하락하면 동일비중 90% 진입, 아니면 대기."""
    def decide(snap):
        sym = _SYMS[0]
        open_px = snap.history[sym][0] if snap.history.get(sym) else None
        if open_px and snap.prices[sym] <= open_px * (1 - threshold):
            return [Signal(sym, Action.BUY, 0.9)]
        return []
    return decide


def test_no_open_entry_then_dip_entry():
    # 개장 100 → 100 → 99 → 98(딥 2%) → 98
    ticks = [{"AAA": 100.0}, {"AAA": 100.0}, {"AAA": 99.0}, {"AAA": 98.0}, {"AAA": 98.0}]
    orch, broker = _orch(ticks, _dip_decider(0.02))
    r = orch.run_trading_day()

    assert r["entered"] is True
    pos = broker.get_portfolio().positions.get("AAA")
    assert pos is not None
    # 개장(100)이 아니라 딥(98)에서 진입 → 평단이 100이 아닌 98 근처
    assert abs(pos.avg_price - 98.0) < 0.2, pos.avg_price


def test_enter_once_only():
    ticks = [{"AAA": 100.0}, {"AAA": 90.0}, {"AAA": 90.0}, {"AAA": 90.0}]
    orch, broker = _orch(ticks, _dip_decider(0.02))
    r = orch.run_trading_day()
    # 첫 딥에서 진입 후 재판단 없음 → 딱 1회 소비
    assert r["entry_calls"] == 1
    assert r["entered"] is True


def test_cap_stops_and_stays_cash():
    never = lambda snap: []  # 항상 대기
    ticks = [{"AAA": 100.0}] + [{"AAA": 100.0}] * 8
    orch, broker = _orch(ticks, never, cap=3)
    r = orch.run_trading_day()
    assert r["entry_calls"] == 3            # 상한에서 멈춤
    assert r["entered"] is False            # 끝까지 미진입
    assert not broker.get_portfolio().positions
    assert abs(r["close_equity"] - 1000.0) < 1e-6  # 현금 유지 → 손실 0


def test_point_in_time_history_excludes_current():
    seen = []
    def spy(snap):
        sym = _SYMS[0]
        seen.append((len(snap.history[sym]), snap.prices[sym], list(snap.history[sym])))
        return []  # 대기(진입 안 함 → 매 틱 호출되어 history 성장 관찰)
    ticks = [{"AAA": 100.0}, {"AAA": 101.0}, {"AAA": 102.0}, {"AAA": 103.0}]
    orch, _ = _orch(ticks, spy)
    orch.run_trading_day()
    # 개장(100)이 시드 → tick1: history=[100], 현재가=101 (현재 미포함)
    assert seen[0] == (1, 101.0, [100.0])
    assert seen[1] == (2, 102.0, [100.0, 101.0])
    # 현재가는 절대 history에 없음(누수 방지)
    for hist_len, cur, hist in seen:
        assert cur not in hist


def test_circuit_breaker_guards_after_timed_entry():
    # 진입 직후 급락 → 손절 -5% 발동
    ticks = [{"AAA": 100.0}, {"AAA": 97.0}, {"AAA": 90.0}, {"AAA": 90.0}]
    orch, broker = _orch(ticks, _dip_decider(0.02), stop=-0.05)
    r = orch.run_trading_day()
    assert r["entered"] is True
    assert r["halted"] is True
    assert r["halt_reason"] == "stop_loss"
    assert not broker.get_portfolio().positions  # 전량 청산


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 진입 타이밍 테스트 통과")
