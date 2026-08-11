"""LLM 에이전트 단위 테스트 — API 호출 없이 목(mock) 클라이언트로 검증.

실행: python3 -m pytest -q   (pytest 없으면 python3 tests/test_llm_agent.py)

검증 포인트:
- 결정 → Signal 변환의 불변식(비중 클램프·정규화·전 종목 커버리지)
- 강제 tool_choice 파싱, 사후 로깅
- API 실패/툴 미호출 시 '무변경' 안전 폴백
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, MarketSnapshot
from src.strategy.llm_agent import LLMAgentStrategy
from src.strategy.llm_agent.decision_tool import TOOL_NAME, decision_to_signals

_UNIVERSE = ["AAA", "BBB", "CCC"]
_TS = datetime(2025, 6, 2)


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        timestamp=_TS,
        prices={s: 10.0 for s in _UNIVERSE},
        history={s: [10.0] * 25 for s in _UNIVERSE},
    )


class _FakeMessages:
    """anthropic client.messages.create 를 흉내내는 목."""

    def __init__(self, decision: dict | None, raise_exc: bool = False):
        self._decision = decision
        self._raise = raise_exc

    def create(self, **kwargs):
        if self._raise:
            raise RuntimeError("API down")
        content = []
        if self._decision is not None:
            content.append(SimpleNamespace(
                type="tool_use", name=TOOL_NAME, input=self._decision))
        return SimpleNamespace(
            content=content,
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )


def _fake_client(decision=None, raise_exc=False):
    return SimpleNamespace(messages=_FakeMessages(decision, raise_exc))


# --- decision_to_signals 불변식 ---

def test_covers_full_universe_and_cashes_out_unmentioned():
    """언급 안 한 종목은 비중 0(현금화)로 반드시 신호에 포함돼야 매도가 실행된다."""
    decision = {"market_view": "", "positions": [
        {"symbol": "AAA", "target_weight": 0.5, "confidence": 0.8, "rationale": "상승"},
    ]}
    signals = decision_to_signals(decision, _UNIVERSE, _TS)
    by = {s.symbol: s for s in signals}
    assert set(by) == set(_UNIVERSE)
    assert by["AAA"].target_weight == 0.5 and by["AAA"].action is Action.BUY
    assert by["BBB"].target_weight == 0.0 and by["BBB"].action is Action.SELL


def test_weights_normalized_when_over_one():
    """합>1.0이면 비례 축소돼 예산 초과 노출을 막는다."""
    decision = {"market_view": "", "positions": [
        {"symbol": "AAA", "target_weight": 0.8, "confidence": 0.5, "rationale": "x"},
        {"symbol": "BBB", "target_weight": 0.8, "confidence": 0.5, "rationale": "y"},
    ]}
    signals = decision_to_signals(decision, _UNIVERSE, _TS)
    total = sum(s.target_weight for s in signals)
    assert abs(total - 1.0) < 1e-9


def test_clamps_negative_and_garbage_values():
    """음수 비중·잘못된 타입도 죽지 않고 안전하게 처리한다(롱온리)."""
    decision = {"market_view": "", "positions": [
        {"symbol": "AAA", "target_weight": -0.3, "confidence": "bad", "rationale": ""},
        {"symbol": "ZZZ", "target_weight": 0.9, "confidence": 0.9, "rationale": "밖"},
    ]}
    signals = decision_to_signals(decision, _UNIVERSE, _TS)
    by = {s.symbol: s for s in signals}
    assert by["AAA"].target_weight == 0.0
    assert "ZZZ" not in by  # universe 밖 종목 무시
    assert 0.0 <= by["AAA"].confidence <= 1.0


# --- 전략 통합(목 클라이언트) ---

def test_generate_signal_parses_tool_call(tmp_log="/tmp/_llm_test.jsonl"):
    if os.path.exists(tmp_log):
        os.remove(tmp_log)
    decision = {"market_view": "테스트", "positions": [
        {"symbol": "AAA", "target_weight": 0.6, "confidence": 0.7, "rationale": "모멘텀"},
    ]}
    strat = LLMAgentStrategy(client=_fake_client(decision), log_path=tmp_log)
    signals = strat.generate_signal(_snapshot())
    assert len(signals) == len(_UNIVERSE)
    assert any(s.target_weight == 0.6 for s in signals)
    # 결정 로그가 원장과 별도로 남았는지
    with open(tmp_log, encoding="utf-8") as f:
        rec = json.loads(f.readline())
    assert rec["market_view"] == "테스트" and rec["usage"]["input_tokens"] == 100
    os.remove(tmp_log)


def test_api_failure_falls_back_to_no_change():
    strat = LLMAgentStrategy(client=_fake_client(raise_exc=True))
    assert strat.generate_signal(_snapshot()) == []


def test_missing_tool_call_falls_back():
    strat = LLMAgentStrategy(client=_fake_client(decision=None))
    assert strat.generate_signal(_snapshot()) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 LLM 에이전트 테스트 통과")
