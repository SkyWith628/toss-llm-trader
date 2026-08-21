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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, MarketSnapshot, Signal
from src.market.feed import ReplayFeed
from src.portfolio import FixedAllocator, InverseVolAllocator, Sleeve
from src.runtime.dryrun import _AlwaysOpenClock
from src.runtime.meta_orchestrator import MetaOrchestrator, slice_capital
from src.runtime.sleeve_account import make_sleeve_account


def _sleeve(name: str, sym: str, capital: float, stop_loss: float):
    """개장 90% 매수 + 지정 손절선을 가진 독립 슬리브(피드는 메타가 소유하므로 None)."""
    return make_sleeve_account(
        name=name, symbols=[sym], capital=capital, clock=_AlwaysOpenClock("meta-day"),
        open_decider=lambda px, s=sym: [Signal(s, Action.BUY, 0.9)], stop_loss=stop_loss)


class _Stub:
    """배분 계산용 더미 전략(allocator는 symbols·snapshot만 보고 strategy는 안 부름)."""
    def generate_signal(self, snapshot):
        return []


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


def test_slice_capital_fixed():
    # budget 1000, 고정 0.7/0.3 → {core:700, pair:300}
    sleeves = [Sleeve("core", _Stub(), ["SPY"]), Sleeve("pair", _Stub(), ["SOXL"])]
    caps = slice_capital(1000.0, sleeves, FixedAllocator({"core": 0.7, "pair": 0.3}))
    assert caps == {"core": 700.0, "pair": 300.0}


def test_slice_capital_inverse_vol_gives_less_to_high_vol():
    # 일별 히스토리 스냅샷 주입 — 고변동 슬리브가 자본을 적게 받는다.
    _ts = datetime(2026, 8, 20, tzinfo=timezone.utc)
    calm = [100, 100.5, 100, 100.5, 100, 100.5, 100, 100.5, 100, 100.5, 100]
    wild = [100, 105, 100, 105, 100, 105, 100, 105, 100, 105, 100]
    snap = MarketSnapshot(timestamp=_ts, prices={"CALM": 100, "WILD": 100},
                          history={"CALM": calm, "WILD": wild})
    sleeves = [Sleeve("calm", _Stub(), ["CALM"]), Sleeve("wild", _Stub(), ["WILD"])]
    caps = slice_capital(1000.0, sleeves, InverseVolAllocator(lookback=10), snap)
    assert abs(sum(caps.values()) - 1000.0) < 1e-6
    assert caps["calm"] > caps["wild"]          # 저변동에 더 큰 자본 슬라이스


def test_sliced_capital_flows_into_sleeve_brokers():
    # slice_capital → make_sleeve_account 로 만든 슬리브 broker에 실제 반영되는지.
    sleeves = [Sleeve("core", _Stub(), ["CORE"]), Sleeve("sat", _Stub(), ["SAT"])]
    caps = slice_capital(1000.0, sleeves, FixedAllocator({"core": 0.6, "sat": 0.4}))
    core = _sleeve("core", "CORE", caps["core"], -0.10)
    sat = _sleeve("sat", "SAT", caps["sat"], -0.02)
    assert core.capital() == 600.0 and sat.capital() == 400.0        # 슬라이스 반영
    meta = MetaOrchestrator(sleeves=[core, sat], clock=_AlwaysOpenClock("d"),
                            feed=ReplayFeed([{"CORE": 100.0, "SAT": 100.0}]), poll_interval=0.0)
    out = meta.run_trading_day()
    assert abs(out["sleeves"]["core"]["day_start_equity"] - 600.0) < 1e-6
    assert abs(out["sleeves"]["sat"]["day_start_equity"] - 400.0) < 1e-6


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
