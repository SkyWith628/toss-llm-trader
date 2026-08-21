"""MetaOrchestrator 단위 테스트 — 슬리브별 독립 서킷브레이커(v2 핵심).

실행: python -m pytest -q  (pytest 없으면 python tests/test_meta_orchestrator.py)

검증 포인트:
- 한 피드를 두 슬리브가 공유할 때, 폭락한 슬리브만 halt·청산되고 잔잔한 슬리브는 생존.
- 메타 요약: halted_sleeves·all_halted·합산 NAV가 슬리브 상태를 정확히 반영.
- 구성 방어: 빈 슬리브·이름 중복.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, Signal
from src.execution.paper_broker import PaperBroker
from src.market.feed import ReplayFeed
from src.risk.day_risk import DayRiskManager
from src.risk.guardrail import Guardrail
from src.runtime.dryrun import _AlwaysOpenClock
from src.runtime.meta_orchestrator import MetaOrchestrator
from src.runtime.orchestrator import Orchestrator
from src.runtime.sleeve_account import SleeveAccount
from src.runtime.state import IntradayState


def _sleeve(name: str, sym: str, capital: float, stop_loss: float) -> SleeveAccount:
    """개장 90% 매수 + 지정 손절선을 가진 독립 슬리브(피드는 메타가 소유하므로 None)."""
    orch = Orchestrator(
        symbols=[sym], clock=_AlwaysOpenClock("meta-day"), feed=None,
        broker=PaperBroker(starting_cash=capital),
        guardrail=Guardrail(budget=capital, valid_symbols={sym}),
        risk=DayRiskManager(take_profit=10.0, stop_loss=stop_loss),
        state=IntradayState.new(capital, [sym]),
        open_decider=lambda px, s=sym: [Signal(s, Action.BUY, 0.9)],
        state_path=os.path.join(tempfile.mkdtemp(), f"{name}.json"), poll_interval=0.0,
    )
    return SleeveAccount(name, orch)


def test_one_sleeve_halts_other_survives():
    # CORE 잔잔(손절 -10%) · SAT 폭락(손절 -2%). SAT만 halt, CORE 생존.
    ticks = [
        {"CORE": 100.0, "SAT": 100.0},   # 개장(앵커=현금 500씩)
        {"CORE": 100.5, "SAT": 96.0},    # SAT -3.6% → 손절 발동 · CORE +
        {"CORE": 101.0, "SAT": 96.0},    # CORE 계속 · SAT는 halt(무동작)
    ]
    core = _sleeve("core", "CORE", 500.0, stop_loss=-0.10)
    sat = _sleeve("sat", "SAT", 500.0, stop_loss=-0.02)
    meta = MetaOrchestrator(sleeves=[core, sat], clock=_AlwaysOpenClock("meta-day"),
                            feed=ReplayFeed(ticks), poll_interval=0.0)
    out = meta.run_trading_day()

    assert sat.halted and sat.orch.state.halt_reason == "stop_loss"   # 폭락 슬리브만 청산
    assert not core.halted                                            # 잔잔 슬리브 생존
    assert out["halted_sleeves"] == ["sat"] and not out["all_halted"]
    # SAT는 현금화(포지션 0), CORE는 보유 유지
    assert not sat.orch.broker.get_portfolio().positions
    assert core.orch.broker.get_portfolio().positions
    # 합산 NAV = 두 슬리브 종가자산 합
    assert abs(out["close_equity"]
               - (out["sleeves"]["core"]["close_equity"]
                  + out["sleeves"]["sat"]["close_equity"])) < 1e-6


def test_meta_orchestrator_validates_construction():
    s = _sleeve("x", "AAA", 100.0, -0.02)
    for bad in ([], [s, _sleeve("x", "BBB", 100.0, -0.02)]):   # 빈 · 이름중복
        try:
            MetaOrchestrator(sleeves=bad, clock=_AlwaysOpenClock("d"),
                             feed=ReplayFeed([{"AAA": 1.0}]))
            assert False
        except ValueError:
            pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 메타-오케스트레이터 테스트 통과")
