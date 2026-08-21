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
    """anthropic client.messages.create 를 흉내내는 목. 호출 kwargs를 기록한다."""

    def __init__(self, decision: dict | None, raise_exc: bool = False):
        self._decision = decision
        self._raise = raise_exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
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


# --- 메모리·회고 ---

def test_memory_retrospective_format():
    from src.strategy.llm_agent.memory import DecisionMemory
    mem = DecisionMemory()
    assert mem.retrospective({"AAA": 11.0}) == ""  # 기록 전엔 빈 문자열
    mem.record("2025-06-02", "상승 베팅", {"AAA": 0.5}, {"AAA": 10.0})
    retro = mem.retrospective({"AAA": 11.0})  # 10 → 11 = +10%
    assert "직전 결정 회고" in retro and "+10.00%" in retro and "상승 베팅" in retro


def test_memory_injects_retrospective_on_second_day():
    """2일차 호출의 프롬프트에는 1일차 회고가 붙어야 한다(연속적 의사결정)."""
    decision = {"market_view": "1일차 판단", "positions": [
        {"symbol": "AAA", "target_weight": 0.5, "confidence": 0.6, "rationale": "x"},
    ]}
    fake = _FakeMessages(decision)
    strat = LLMAgentStrategy(client=SimpleNamespace(messages=fake))

    # 1일차: 회고 없음
    strat.generate_signal(_snapshot())
    day1_prompt = fake.calls[0]["messages"][0]["content"]
    assert "직전 결정 회고" not in day1_prompt
    assert len(strat.memory) == 1

    # 2일차: 프롬프트에 1일차 회고 주입
    strat.generate_signal(_snapshot())
    day2_prompt = fake.calls[1]["messages"][0]["content"]
    assert "직전 결정 회고" in day2_prompt and "1일차 판단" in day2_prompt


def test_fallback_does_not_pollute_memory():
    """API 실패한 날은 메모리에 기록하지 않는다(회고 오염 방지)."""
    strat = LLMAgentStrategy(client=_fake_client(raise_exc=True))
    strat.generate_signal(_snapshot())
    assert len(strat.memory) == 0


# --- 프롬프트 캐싱 ---

def test_system_prompt_has_cache_breakpoint():
    """system 블록에 cache_control이 붙어 고정부(tools+system)가 캐시되어야 한다."""
    decision = {"market_view": "", "positions": []}
    fake = _FakeMessages(decision)
    LLMAgentStrategy(client=SimpleNamespace(messages=fake)).generate_signal(_snapshot())
    system = fake.calls[0]["system"]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}


# --- intraday(장중 진입 타이밍) 모드 ---

def test_intraday_mode_uses_timing_prompt_and_render():
    """intraday 모드는 타이밍 전용 시스템 프롬프트 + 장중 관점 렌더를 쓴다."""
    client = _fake_client({"market_view": "대기", "positions": []})
    strat = LLMAgentStrategy(client=client, mode="intraday")
    strat.generate_signal(_snapshot())
    kw = client.messages.calls[0]
    sys_text = kw["system"][0]["text"]
    assert "기다릴까" in sys_text and "대기" in sys_text      # 타이밍 프롬프트
    user_text = kw["messages"][0]["content"]
    assert "개장가" in user_text and "장중" in user_text       # 장중 렌더


def test_intraday_wait_produces_no_entry_weights():
    """빈 positions(=대기) → 전 종목 목표비중 0 → 오케스트레이터가 진입하지 않는다."""
    strat = LLMAgentStrategy(
        client=_fake_client({"market_view": "", "positions": []}), mode="intraday")
    signals = strat.generate_signal(_snapshot())
    assert signals and all(s.target_weight == 0 for s in signals)


def test_daily_mode_unchanged_by_default():
    """기본(daily) 모드는 기존 프롬프트를 유지(하위호환)."""
    client = _fake_client({"market_view": "", "positions": []})
    LLMAgentStrategy(client=client).generate_signal(_snapshot())
    assert "포트폴리오 매니저" in client.messages.calls[0]["system"][0]["text"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 LLM 에이전트 테스트 통과")
