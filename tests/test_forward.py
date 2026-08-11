"""단계 3 forward 페이퍼 하네스 테스트 — 영속성·연속성·중복방지·앵커 불변.

네트워크·LLM 없이 검증: 합성 store + 고정(mock) 전략을 주입해 '여러 날'을 몰아 돌린다.
실행: python3 tests/test_forward.py  (또는 python3 -m pytest -q)
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, MarketSnapshot, Signal
from src.data.synthetic import make_synthetic_store
from src.forward.runner import ForwardConfig, build_report, run_once
from src.forward.state import ForwardState
from src.strategy.base import Strategy
from src.strategy.llm_agent.memory import DecisionMemory


class FixedStrategy(Strategy):
    """항상 같은 목표 비중을 내는 mock 전략(비결정성 제거)."""
    name = "fixed"

    def __init__(self, weights: dict[str, float]) -> None:
        self.weights = weights

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        return [Signal(s, Action.BUY, w, timestamp=snapshot.timestamp)
                for s, w in self.weights.items()]


def _cfg(state_dir: str) -> ForwardConfig:
    return ForwardConfig(symbols=["AAA", "BBB"], budget=100.0, source="synthetic",
                         strategy="momentum", state_dir=state_dir, min_trade=1.0)


def test_memory_serialization_roundtrip():
    """DecisionMemory to_list/load가 회고를 프로세스 밖에서 복원해야."""
    m = DecisionMemory(max_entries=5)
    m.record("2026-01-02", "테크 강세", {"AAA": 0.6}, {"AAA": 100.0, "BBB": 50.0})
    dumped = m.to_list()

    m2 = DecisionMemory(max_entries=5)
    m2.load(dumped)
    assert len(m2) == 1
    # 복원된 메모리로도 회고 텍스트가 동일 기준점(당시 가격)으로 생성돼야
    retro = m2.retrospective({"AAA": 110.0, "BBB": 50.0})
    assert "테크 강세" in retro and "+10.00%" in retro


def test_state_roundtrip_and_bh_anchor():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        st = ForwardState.load_or_init(path, budget=100.0, symbols=["AAA", "BBB"],
                                       source="synthetic", strategy="momentum", model="-")
        st.ensure_bh_anchor({"AAA": 100.0, "BBB": 50.0})
        anchor = dict(st.bh_shares)
        st.save(path)

        st2 = ForwardState.load_or_init(path, budget=100.0, symbols=["AAA", "BBB"],
                                        source="synthetic", strategy="momentum", model="-")
        assert st2.bh_shares == anchor          # 앵커가 파일 왕복 후에도 동일
        assert abs(st2.cash - 100.0) < 1e-9     # 초기 전액 현금
        # 앵커 시점 B&H 총가치 ≈ 예산(슬리피지만큼만 손실)
        assert st2.bh_value({"AAA": 100.0, "BBB": 50.0}) < 100.0


def test_two_day_continuity():
    """이틀 연속 실행 시 포지션·회차가 상태로 이어져야(각 날은 별도 run_once 호출)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=3)
        strat = FixedStrategy({"AAA": 0.5})

        r1 = run_once(cfg, store=store, today=store.dates[10], strategy=strat)
        assert not r1["skipped"] and r1["day"] == 1
        st1 = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                        source="synthetic", strategy="momentum", model="-")
        assert "AAA" in st1.positions            # 첫날 매수분이 파일에 저장됨

        r2 = run_once(cfg, store=store, today=store.dates[11], strategy=strat)
        assert not r2["skipped"] and r2["day"] == 2
        # equity.jsonl에 이틀치 누적
        with open(cfg.equity_path, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        assert len(lines) == 2


def test_same_day_rerun_skipped():
    """같은 거래일 재실행은 skip(중복 매매·이중 카운트 방지)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=5)
        strat = FixedStrategy({"AAA": 0.5})
        run_once(cfg, store=store, today=store.dates[10], strategy=strat)
        again = run_once(cfg, store=store, today=store.dates[10], strategy=strat)
        assert again["skipped"] and again["day"] == 1


def test_bh_anchor_invariant_across_days():
    """B&H 주식수는 앵커 이후 절대 변하지 않아야(전략과 동일 시작점 유지)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=7)
        strat = FixedStrategy({"AAA": 0.5})
        run_once(cfg, store=store, today=store.dates[10], strategy=strat)
        st1 = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                        source="synthetic", strategy="momentum", model="-")
        anchor = dict(st1.bh_shares)
        run_once(cfg, store=store, today=store.dates[12], strategy=strat)
        st2 = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                        source="synthetic", strategy="momentum", model="-")
        assert st2.bh_shares == anchor


def test_config_mismatch_guard():
    """다른 구성(예산/종목)으로 같은 state를 재사용하면 막아야(측정 오염 방지)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=9)
        run_once(cfg, store=store, today=store.dates[10], strategy=FixedStrategy({"AAA": 0.5}))
        try:
            ForwardState.load_or_init(cfg.state_path, budget=200.0, symbols=cfg.symbols,
                                      source="synthetic", strategy="momentum", model="-")
            raised = False
        except ValueError:
            raised = True
        assert raised


def test_report_reads_equity():
    """--report 경로: equity.jsonl 누적에서 전략·B&H 지표를 계산."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=11)
        strat = FixedStrategy({"AAA": 0.5})
        for i in range(10, 15):
            run_once(cfg, store=store, today=store.dates[i], strategy=strat)
        rep = build_report(cfg)
        assert rep["days"] == 5
        assert "total_return" in rep["strategy"] and "total_return" in rep["buy_and_hold"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 forward 테스트 통과")
