"""Forward 페이퍼 테스트 단위 검증 — 네트워크·API 없이 합성 store/목 클라이언트로.

실행: python -m pytest -q  (pytest 없으면 python tests/test_forward.py)

검증 포인트(포워드 특유의 난제들):
- 상태 왕복(round-trip): 저장→로드로 포트폴리오·메모리·B&H앵커가 보존
- 첫날: Buy&Hold 기준점 고정 + 매매 + 원장 1줄
- 멱등성: 같은 거래일 재실행은 스킵(이중매매 금지)
- 이어달리기: 다음 거래일엔 자본곡선이 누적(1→2)
- 메모리 지속: LLM 회고가 프로세스 재시작을 넘어 다음 날 프롬프트에 주입
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Portfolio, Position
from src.data.synthetic import make_synthetic_store
from src.forward.runner import run_once
from src.forward.state import ForwardState
from src.strategy.llm_agent.decision_tool import TOOL_NAME

_SYMBOLS = ["AAA", "BBB", "CCC"]


def _paths():
    """임시 상태·원장·LLM로그 경로(실제 logs/ 오염 방지)."""
    d = tempfile.mkdtemp(prefix="fwd_test_")
    return (os.path.join(d, "state.json"),
            os.path.join(d, "ledger.jsonl"),
            os.path.join(d, "llm.jsonl"))


def _store(days: int):
    """seed 고정 → days만 늘리면 앞부분 동일 + 뒤에 하루 추가(다음 거래일 시뮬)."""
    return make_synthetic_store(_SYMBOLS, days=days, seed=7)


# --- 목 LLM 클라이언트 ---

class _FakeMessages:
    def __init__(self, decision: dict):
        self._decision = decision
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=TOOL_NAME,
                                     input=self._decision)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def _fake_client(decision: dict):
    return SimpleNamespace(messages=_FakeMessages(decision))


# --- 상태 왕복 ---

def test_state_round_trip():
    sp, _, _ = _paths()
    st = ForwardState.new(budget=1000.0, symbols=_SYMBOLS)
    pf = Portfolio(cash=400.0, positions={"AAA": Position("AAA", 12.5, 48.0)})
    st.set_from_portfolio(pf)
    st.memory = [{"date": "2025-01-02", "market_view": "상승",
                  "weights": {"AAA": 0.6}, "prices": {"AAA": 50.0}}]
    st.bh_shares = {"AAA": 3.3}
    st.processed_dates = ["2025-01-02"]
    st.save(sp)

    loaded = ForwardState.load(sp)
    assert loaded is not None
    rp = loaded.to_portfolio()
    assert abs(rp.cash - 400.0) < 1e-9
    assert abs(rp.positions["AAA"].quantity - 12.5) < 1e-9
    assert loaded.memory[0]["weights"]["AAA"] == 0.6
    assert loaded.bh_shares["AAA"] == 3.3
    assert loaded.processed_dates == ["2025-01-02"]


def test_load_missing_returns_none():
    sp, _, _ = _paths()
    assert ForwardState.load(sp) is None  # 첫 실행 신호


# --- 러너: 첫날/멱등/이어달리기 ---

def test_first_run_trades_and_anchors_buy_and_hold():
    sp, lp, llog = _paths()
    r = run_once(symbols=_SYMBOLS, budget=1000.0, strategy_name="momentum",
                 lookback=5, state_path=sp, ledger_path=lp, llm_log_path=llog,
                 store=_store(10))
    assert r["status"] == "traded" and r["day_index"] == 1

    st = ForwardState.load(sp)
    assert st.bh_shares and set(st.bh_shares) == set(_SYMBOLS)  # B&H 기준점 고정
    assert st.start_date == r["date"]
    assert len(st.strat_equity) == 1 and len(st.bh_equity) == 1
    # 원장에 하루치 1줄
    with open(lp, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["day_index"] == 1


def test_idempotent_same_trading_day():
    sp, lp, llog = _paths()
    store = _store(10)
    run_once(symbols=_SYMBOLS, budget=1000.0, strategy_name="momentum", lookback=5,
             state_path=sp, ledger_path=lp, llm_log_path=llog, store=store)
    r2 = run_once(symbols=_SYMBOLS, budget=1000.0, strategy_name="momentum", lookback=5,
                  state_path=sp, ledger_path=lp, llm_log_path=llog, store=store)
    assert r2["status"] == "skipped"

    st = ForwardState.load(sp)
    assert len(st.processed_dates) == 1  # 이중 처리 없음
    with open(lp, encoding="utf-8") as f:
        assert len([ln for ln in f if ln.strip()]) == 1  # 원장도 그대로


def test_continues_on_next_trading_day():
    sp, lp, llog = _paths()
    run_once(symbols=_SYMBOLS, budget=1000.0, strategy_name="momentum", lookback=5,
             state_path=sp, ledger_path=lp, llm_log_path=llog, store=_store(10))
    r2 = run_once(symbols=_SYMBOLS, budget=1000.0, strategy_name="momentum", lookback=5,
                  state_path=sp, ledger_path=lp, llm_log_path=llog, store=_store(11))
    assert r2["status"] == "traded" and r2["day_index"] == 2

    st = ForwardState.load(sp)
    assert len(st.strat_equity) == 2 and len(st.processed_dates) == 2
    with open(lp, encoding="utf-8") as f:
        assert len([ln for ln in f if ln.strip()]) == 2  # 누적(초기화 아님)


# --- 메모리 지속(프로세스 재시작을 넘는 회고 연속성) ---

def test_llm_memory_persists_across_runs():
    sp, lp, llog = _paths()
    decision = {"market_view": "1일차 상승 베팅", "positions": [
        {"symbol": "AAA", "target_weight": 0.5, "confidence": 0.7, "rationale": "모멘텀"},
    ]}

    # 1일차: 결정 기록 → 상태에 메모리 저장
    run_once(symbols=_SYMBOLS, budget=1000.0, strategy_name="llm", lookback=5,
             state_path=sp, ledger_path=lp, llm_log_path=llog,
             store=_store(10), client=_fake_client(decision))
    st1 = ForwardState.load(sp)
    assert len(st1.memory) == 1 and st1.memory[0]["market_view"] == "1일차 상승 베팅"

    # 2일차: 새 프로세스처럼 새 클라이언트 주입 → 프롬프트에 1일차 회고가 복원돼야 함
    fake2 = _fake_client(decision)
    run_once(symbols=_SYMBOLS, budget=1000.0, strategy_name="llm", lookback=5,
             state_path=sp, ledger_path=lp, llm_log_path=llog,
             store=_store(11), client=fake2)
    day2_user_msg = fake2.messages.calls[0]["messages"][0]["content"]
    assert "직전 결정 회고" in day2_user_msg and "1일차 상승 베팅" in day2_user_msg

    st2 = ForwardState.load(sp)
    assert len(st2.processed_dates) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 Forward 테스트 통과")
