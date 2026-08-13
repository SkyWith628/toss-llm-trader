"""단계 3 forward 페이퍼 하네스 테스트 — 영속성·연속성·중복방지·앵커 불변.

네트워크·LLM 없이 검증: 합성 store + 고정(mock) 전략을 주입해 '여러 날'을 몰아 돌린다.
실행: python3 tests/test_forward.py  (또는 python3 -m pytest -q)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, MarketSnapshot, Signal
from src.data.synthetic import make_synthetic_store
from src.forward.runner import ForwardConfig, build_report, run_once
from src.forward.state import ForwardState, SCHEMA_VERSION
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


class ScriptedStrategy(Strategy):
    """날짜별로 정해진 목표비중을 순서대로 내는 mock 전략(체결→장부 갱신 검증용)."""
    name = "scripted"

    def __init__(self, weights_by_day: list[dict[str, float]]) -> None:
        self.weights_by_day = weights_by_day
        self.i = 0

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        w = self.weights_by_day[min(self.i, len(self.weights_by_day) - 1)]
        self.i += 1
        return [Signal(s, Action.BUY if v > 0 else Action.SELL, v,
                       timestamp=snapshot.timestamp) for s, v in w.items()]


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
        assert abs(st2.experiment.cash - 100.0) < 1e-9   # 초기 전액 현금(실험 장부)
        assert st2.experiment.protected_positions == {}  # 페이퍼는 보호분 없음
        # 앵커 시점 B&H 총가치 ≈ 예산(슬리피지만큼만 손실)
        assert st2.bh_value({"AAA": 100.0, "BBB": 50.0}) < 100.0


def test_two_day_continuity_next_bar():
    """시점누수 방지: 첫날은 체결 없음(직전 결정 없음), 둘째날에 '어제 결정'이 체결돼야."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=3)
        strat = FixedStrategy({"AAA": 0.5})

        r1 = run_once(cfg, store=store, today=store.dates[10], strategy=strat)
        assert not r1["skipped"] and r1["day"] == 1 and r1["fills"] == []   # 첫날 무체결
        st1 = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                        source="synthetic", strategy="momentum", model="-")
        assert "AAA" not in st1.experiment.experiment_positions  # 당일 종가 신호를 당일 체결 안 함
        assert st1.pending_signals               # 첫날 결정은 pending으로 저장(다음날 체결)

        r2 = run_once(cfg, store=store, today=store.dates[11], strategy=strat)
        assert not r2["skipped"] and r2["day"] == 2 and r2["fills"]         # 둘째날 체결 발생
        st2 = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                        source="synthetic", strategy="momentum", model="-")
        assert "AAA" in st2.experiment.experiment_positions   # 어제 결정이 오늘 체결됨
        # 체결가는 '둘째날' 종가여야(첫날 종가 아님) — next-bar 집행 확인
        assert abs(r2["fills"][0]["fill"] - store.prices_on(store.dates[11])["AAA"]) \
            < store.prices_on(store.dates[11])["AAA"] * 0.01
        with open(cfg.equity_path, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        assert len(lines) == 2


def test_runner_tracks_realized_pnl_via_ledger():
    """실체결로 실험 장부가 갱신되는지 — 매수 후 전량 매도 시 realized_pnl이 잡혀야."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=13)
        # day1 결정: AAA 100% / day2 결정: 청산 / day3: 청산 유지
        strat = ScriptedStrategy([{"AAA": 1.0}, {"AAA": 0.0}, {"AAA": 0.0}])
        for i in (10, 11, 12):   # day1 결정→day2 매수→day3 매도
            run_once(cfg, store=store, today=store.dates[i], strategy=strat)
        st = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                       source="synthetic", strategy="momentum", model="-")
        assert "AAA" not in st.experiment.experiment_positions   # 전량 매도됨
        assert st.experiment.realized_pnl != 0.0                 # 손익이 장부에 기록됨
        assert st.experiment.status == "active"                  # 대사 위반 없음


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


def test_config_change_detected():
    """예산·종목이 같아도 결과영향 설정(전략 등)이 바뀌면 같은 state 재사용을 막아야."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=9)
        run_once(cfg, store=store, today=store.dates[10], strategy=FixedStrategy({"AAA": 0.5}))
        try:
            ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                      source="synthetic", strategy="llm", model="-",
                                      config={**cfg.canonical(), "strategy": "llm"})
            raised = False
        except ValueError as e:
            raised = "strategy" in str(e)
        assert raised


def test_runner_skips_orders_when_not_tradeable():
    """실험이 중단 상태면 어제 결정이 있어도 runner가 신규 주문을 내지 않아야(fail-closed)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        store = make_synthetic_store(cfg.symbols, days=30, seed=21)
        strat = FixedStrategy({"AAA": 0.5})
        run_once(cfg, store=store, today=store.dates[10], strategy=strat)  # day1: pending 저장
        # 실험을 중단 상태로 만들고 저장
        st = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                       source="synthetic", strategy="momentum", model="-")
        st.experiment.require_reconciliation("테스트 중단")
        st.save(cfg.state_path)
        # day2: 중단 상태 → 어제 결정 체결 안 됨
        r2 = run_once(cfg, store=store, today=store.dates[11], strategy=strat)
        assert r2["fills"] == []
        st2 = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=cfg.symbols,
                                        source="synthetic", strategy="momentum", model="-")
        assert "AAA" not in st2.experiment.experiment_positions   # 매수 안 됨


def test_schema_version_mismatch_fails_closed():
    """현재와 다른 스키마 버전은 변환 없이 통과시키지 않아야(schema_version=1도 거부)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"schema_version":1,"budget":100.0,"symbols":["AAA"]}')
        try:
            ForwardState.load_or_init(path, budget=100.0, symbols=["AAA"],
                                      source="synthetic", strategy="momentum", model="-")
            raised = False
        except ValueError as e:
            raised = "버전" in str(e)
        assert raised


def test_old_schema_fails_closed():
    """schema_version 없는 구버전 상태 파일은 친절히 fail-closed(KeyError 대신)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"budget":100.0,"symbols":["AAA"],"cash":100.0,"positions":{}}')  # 구형식
        try:
            ForwardState.load_or_init(path, budget=100.0, symbols=["AAA"],
                                      source="synthetic", strategy="momentum", model="-")
            raised = False
        except ValueError as e:
            raised = "구버전" in str(e)
        assert raised


def _write_v3_paper_state(path, protected=None):
    """v3(last_account_snapshot 이전) 페이퍼 상태 파일을 만든다(마이그레이션 테스트용)."""
    data = {
        "schema_version": 3, "budget": 100.0, "symbols": ["AAA"],
        "source": "synthetic", "strategy": "momentum", "model": "-",
        "experiment": {"initial_capital": 100.0, "cash": 100.0,
                       "protected_positions": protected or {}, "experiment_positions": {}},
        "open_orders": {},   # v3엔 있음. last_account_snapshot은 없음(v4에서 추가).
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_migrate_v3_paper_state_to_current():
    """v3 페이퍼 상태는 누락 필드를 채워 현재 스키마로 승격되어 로드된다(#1)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        _write_v3_paper_state(path)
        st = ForwardState.load_or_init(path, budget=100.0, symbols=["AAA"],
                                       source="synthetic", strategy="momentum", model="-")
        assert st.schema_version == SCHEMA_VERSION
        assert st.last_account_snapshot == {}          # v4 필드가 기본값으로 채워짐
        st.save(path)                                   # 저장 시 v4로 영속
        assert json.load(open(path, encoding="utf-8"))["schema_version"] == SCHEMA_VERSION


def test_migrate_refuses_live_origin_baseline():
    """보호 기준선이 있는 상태(live 기원)는 자동 마이그레이션하지 않는다(#1, 보호분 자동추정 금지)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "state.json")
        _write_v3_paper_state(path, protected={"AAA": 10.0})
        try:
            ForwardState.load_or_init(path, budget=100.0, symbols=["AAA"],
                                      source="synthetic", strategy="momentum", model="-")
            raised = False
        except ValueError as e:
            raised = "자동 마이그레이션" in str(e)
        assert raised


def test_paper_and_live_mode_not_mixed_in_one_state_dir():
    """paper로 만든 상태를 live 설정으로 다시 열면 config 대조에서 fail-closed(#5)."""
    with tempfile.TemporaryDirectory() as d:
        paper = ForwardConfig(symbols=["AAA"], mode="paper", source="synthetic",
                              strategy="momentum", model="-", state_dir=d)
        st = ForwardState.load_or_init(paper.state_path, budget=100.0, symbols=["AAA"],
                                       source="synthetic", strategy="momentum", model="-",
                                       config=paper.canonical())
        st.save(paper.state_path)
        live = ForwardConfig(symbols=["AAA"], mode="live", source="synthetic",
                             strategy="momentum", model="-", state_dir=d)
        try:
            ForwardState.load_or_init(live.state_path, budget=100.0, symbols=["AAA"],
                                      source="synthetic", strategy="momentum", model="-",
                                      config=live.canonical())
            raised = False
        except ValueError as e:
            raised = "mode" in str(e)
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
