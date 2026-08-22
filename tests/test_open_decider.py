"""LLMOpenDecider 단위 테스트 — 개장 시세 dict를 스냅샷으로 조립해 전략에 위임.

실행: python -m pytest -q  (pytest 없으면 python tests/test_open_decider.py)

검증 포인트:
- 개장가 dict + 주입 history → MarketSnapshot 정확 조립(prices·history·timestamp)
- 전략의 반환 Signal이 그대로 통과
- 전략이 []를 뱉으면(API 실패 폴백) decider도 [] → 오케스트레이터가 전량 현금 시작
- history_provider는 개장 결정 시점에 호출(지연 평가)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, MarketSnapshot, Signal
from src.runtime.open_decider import LLMOpenDecider

_NOW = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)


class _SpyStrategy:
    """generate_signal 인자를 포착하고 미리 정한 Signal을 반환하는 mock 전략."""

    def __init__(self, to_return):
        self._to_return = to_return
        self.seen: MarketSnapshot | None = None
        self.calls = 0

    def generate_signal(self, snapshot):
        self.calls += 1
        self.seen = snapshot
        return list(self._to_return)


def _decider(strategy, history):
    """호출 횟수 셀 수 있는 history_provider로 감싼 decider."""
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return history

    dec = LLMOpenDecider(strategy, provider, now_fn=lambda: _NOW)
    return dec, calls


def test_assembles_snapshot_and_passes_signals_through():
    strat = _SpyStrategy([Signal("AAPL", Action.BUY, 0.6)])
    history = {"AAPL": [100.0, 101.0, 102.0]}
    dec, _ = _decider(strat, history)

    out = dec({"AAPL": 103.5})

    # 반환 통과
    assert len(out) == 1 and out[0].symbol == "AAPL" and out[0].target_weight == 0.6
    # 스냅샷 정확 조립
    snap = strat.seen
    assert snap.prices == {"AAPL": 103.5}
    assert snap.history == history
    assert snap.timestamp == _NOW


def test_empty_strategy_result_falls_back_to_cash():
    strat = _SpyStrategy([])  # API 실패 폴백 등
    dec, _ = _decider(strat, {"AAPL": [100.0]})

    assert dec({"AAPL": 100.0}) == []  # 전량 현금(오케스트레이터가 아무것도 안 삼)


def test_history_provider_is_called_at_decision_time():
    strat = _SpyStrategy([])
    dec, calls = _decider(strat, {"AAPL": [1.0]})

    assert calls["n"] == 0  # 생성 시점엔 미호출(지연 평가)
    dec({"AAPL": 1.0})
    assert calls["n"] == 1  # 결정 시점에 1회 호출


def test_prices_are_copied_not_aliased():
    strat = _SpyStrategy([])
    dec, _ = _decider(strat, {})
    px = {"AAPL": 100.0}

    dec(px)
    px["AAPL"] = 999.0  # 이후 원본 변경이 스냅샷에 새지 않아야 함

    assert strat.seen.prices == {"AAPL": 100.0}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 LLMOpenDecider 테스트 통과")
