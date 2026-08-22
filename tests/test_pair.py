"""인버스 페어 전략 단위 테스트 — 기댓값 게이트·상호배타·동적 임계.

실행: python -m pytest -q  (pytest 없으면 python tests/test_pair.py)

검증 포인트:
- pair_gate: expectancy·passes_gate·mutual_exclusive·clamp_thresholds
- DayRiskManager.set_thresholds: 진입 시 임계 동적 확정 + 검증
- PairEntryDecider: E<0이면 대기 · 통과 시 한 방향만 + risk 임계 주입 · 동률이면 현금
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, MarketSnapshot, Signal
from src.experiment.eval_harness import BASE_DEFAULTS, PRESETS, resolve_config
from src.risk.day_risk import DayRiskManager, RiskAction
from src.runtime.pair_decider import PairEntryDecider
from src.strategy.pair_gate import (
    clamp_thresholds, expectancy, mutual_exclusive, passes_gate,
)

_TS = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)


# --- pair_gate 순수 함수 ---

def test_expectancy_and_gate():
    assert abs(expectancy(0.7, 0.05, 0.02) - 0.029) < 1e-9
    assert passes_gate(0.7, 0.05, 0.02)          # E>0
    assert not passes_gate(0.1, 0.02, 0.05)      # E<0 → 대기
    assert not passes_gate(0.5, 0.02, 0.02, e_min=0.01)  # 문턱 미달


def test_mutual_exclusive_keeps_one():
    assert mutual_exclusive({"L": 0.4, "S": 0.2}) == {"L": 0.4, "S": 0.0}
    assert mutual_exclusive({"L": 0.1, "S": 0.6}) == {"L": 0.0, "S": 0.6}
    assert mutual_exclusive({"L": 0.3, "S": 0.3}) == {"L": 0.0, "S": 0.0}  # 동률 → 현금
    assert mutual_exclusive({"L": 0.0, "S": 0.0}) == {"L": 0.0, "S": 0.0}


def test_clamp_thresholds():
    assert clamp_thresholds(0.05, 0.02) == (0.05, -0.02)      # 범위 내 그대로
    assert clamp_thresholds(0.20, 0.10) == (0.08, -0.04)      # 상한 캡
    assert clamp_thresholds(0.005, 0.001) == (0.02, -0.01)    # 하한 바닥


# --- 하네스 구성 우선순위(resolve_config) — 순수, 네트워크 0 ---

def test_resolve_config_priority():
    # 프리셋 없음 → BASE_DEFAULTS 그대로
    assert resolve_config(None, {"symbols": None}) == BASE_DEFAULTS
    # 3x 프리셋 → 재보정 임계·슬리피지로 교체
    c3 = resolve_config("3x", {k: None for k in BASE_DEFAULTS})
    assert c3["symbols"] == "SOXL,SOXS" and c3["stop_loss"] == -0.02
    assert c3["slippage"] == 0.001 and c3["take_profit"] == 0.04
    # 명시 인자(None 아님)는 프리셋보다 우선, None은 무시
    over = resolve_config("3x", {"stop_loss": -0.03, "take_profit": None})
    assert over["stop_loss"] == -0.03            # 사용자 값 우선
    assert over["take_profit"] == PRESETS["3x"]["take_profit"]  # None → 프리셋 유지
    # 전역 1x 벤치마크는 프리셋 슬리피지 상향에 영향 없음
    assert resolve_config(None, {})["slippage"] == 0.0005


# --- DayRiskManager.set_thresholds ---

def test_set_thresholds_updates_and_validates():
    m = DayRiskManager(take_profit=0.02, stop_loss=-0.01)
    m.anchor(1000.0)
    m.set_thresholds(0.05, -0.02)
    assert m.take_profit == 0.05 and m.stop_loss == -0.02
    assert m.evaluate(1050.0) is RiskAction.TAKE_PROFIT   # +5% = 새 익절
    assert m.evaluate(1030.0) is RiskAction.NORMAL        # +3% < +5%
    for bad in [(0.0, -0.01), (0.05, 0.0)]:
        try:
            m.set_thresholds(*bad)
            assert False
        except ValueError:
            pass


# --- PairEntryDecider ---

class _Stub:
    def __init__(self, signals, decision):
        self._signals = signals
        self.last_decision = decision
    def generate_signal(self, snap):
        return list(self._signals)


def _snap():
    return MarketSnapshot(timestamp=_TS, prices={"SOXL": 30.0, "SOXS": 10.0}, history={})


def _sig(sym, w, conf):
    return Signal(sym, Action.BUY if w > 0 else Action.SELL, w, conf, _TS, "")


def test_pair_enters_one_side_and_sets_thresholds():
    strat = _Stub([_sig("SOXL", 0.4, 0.7), _sig("SOXS", 0.2, 0.3)],
                  {"target_return": 0.05, "stop_loss": 0.02, "positions": []})
    risk = DayRiskManager(take_profit=0.02, stop_loss=-0.01)
    risk.anchor(1000.0)
    out = PairEntryDecider(strat, risk)(_snap())
    by = {s.symbol: s for s in out}
    assert by["SOXL"].target_weight == 0.4 and by["SOXS"].target_weight == 0.0  # 한 방향
    assert risk.take_profit == 0.05 and risk.stop_loss == -0.02                  # 동적 임계 주입


def test_pair_waits_on_negative_expectancy():
    strat = _Stub([_sig("SOXL", 0.4, 0.1), _sig("SOXS", 0.0, 0.1)],
                  {"target_return": 0.02, "stop_loss": 0.05, "positions": []})
    risk = DayRiskManager(take_profit=0.03, stop_loss=-0.02)
    risk.anchor(1000.0)
    assert PairEntryDecider(strat, risk)(_snap()) == []      # E<0 → 대기
    assert risk.take_profit == 0.03 and risk.stop_loss == -0.02  # 임계 미변경


def test_pair_cash_on_tie():
    strat = _Stub([_sig("SOXL", 0.3, 0.8), _sig("SOXS", 0.3, 0.8)],
                  {"target_return": 0.05, "stop_loss": 0.02, "positions": []})
    risk = DayRiskManager(take_profit=0.02, stop_loss=-0.01)
    risk.anchor(1000.0)
    assert PairEntryDecider(strat, risk)(_snap()) == []      # 동률 → 방향 모호 → 현금


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 페어 테스트 통과")
