"""증분5 외부 계좌 사건 감지 테스트 — 실험이 설명 못하는 보유 변화 시 halt(fail-closed).

순수 감지(detect_events)와 _execute_live 통합(수동매수/기업행동 halt vs 실험이 설명하는
지연체결은 통과)을 네트워크·실주문 0으로 검증. 내부 브로커는 test_live_wiring의 FakeLiveBroker.

실행: python3 tests/test_events.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Order, Side
from src.execution.costs import CostModel
from src.risk.guardrail import Guardrail
from src.forward import events, order_book as ob, runner
from src.forward.experiment_portfolio import ExperimentPortfolio, ExperimentPosition
from src.forward.state import ForwardState
from tests.test_live_wiring import FakeLiveBroker

_DATE = datetime(2026, 8, 13)
_DATE_ISO = "2026-08-13"
_PRICES = {"AAPL": 10.0}


def _ctx(exp):
    tmp = tempfile.mkdtemp(prefix="events_")
    cfg = runner.ForwardConfig(symbols=["AAPL"], mode="live", state_dir=tmp)
    state = ForwardState(budget=100.0, symbols=["AAPL"], source="synthetic",
                         strategy="momentum", model="-", experiment=exp)
    guardrail = Guardrail(budget=100.0, valid_symbols={"AAPL"})
    return cfg, state, guardrail, CostModel(slippage=0.0005)


def _run(cfg, state, exp, orders, guardrail, cost, broker):
    return runner._execute_live(cfg, state, exp, orders, guardrail,
                                _PRICES, _DATE, _DATE_ISO, cost, broker)


# ---------- 순수 감지 ----------
def test_no_change_no_events():
    assert events.detect_events({"AAPL": 1.0}, {"AAPL": 1.0}, {}) == []


def test_manual_sell_flagged():
    ev = events.detect_events({"AAPL": 1.0}, {"AAPL": 0.7}, {})
    assert len(ev) == 1 and ev[0]["kind"] == events.MANUAL_SELL
    assert abs(ev[0]["residual"] + 0.3) < 1e-9      # 잔여 −0.3


def test_manual_buy_flagged():
    ev = events.detect_events({"AAPL": 1.0}, {"AAPL": 1.6}, {})
    assert len(ev) == 1 and ev[0]["kind"] == events.MANUAL_BUY
    assert abs(ev[0]["residual"] - 0.6) < 1e-9


def test_experiment_delta_explains_change():
    # 실험이 0.5 매수(지연체결)한 만큼의 변화는 사건이 아니다(잔여 0).
    assert events.detect_events({"AAPL": 0.0}, {"AAPL": 0.5}, {"AAPL": 0.5}) == []


def test_split_like_increase_flagged_as_corporate_action():
    # 2:1 분할로 1.0 → 2.0. 실험이 만든 변화가 아니므로 기업행동으로 잡아 halt 대상.
    ev = events.detect_events({"AAPL": 1.0}, {"AAPL": 2.0}, {})
    assert ev and ev[0]["kind"] == events.MANUAL_BUY


# ---------- _execute_live 통합 ----------
def test_manual_buy_detected_halts_live():
    """대사(≥)는 통과하지만 실험이 설명 못하는 증가 → halt, 신규 주문 0(fail-closed)."""
    exp = ExperimentPortfolio(initial_capital=100.0, cash=100.0,
                              protected_positions={"AAPL": 1.0})
    cfg, state, guardrail, cost = _ctx(exp)
    state.last_account_snapshot = {"AAPL": 1.0}
    broker = FakeLiveBroker(price=10.0, holdings={"AAPL": 2.0})   # 사용자 추가매수/분할
    fills, _ = _run(cfg, state, exp, [Order("AAPL", Side.BUY, 0.5)], guardrail, cost, broker)

    assert not exp.can_trade and exp.status == "halted"
    assert broker.place_calls == []                              # 신규 주문 전송 0
    halted = [f for f in fills if f.get("status") == "halted"]
    assert halted and halted[0]["events"][0]["kind"] == events.MANUAL_BUY
    assert state.last_account_snapshot == {"AAPL": 2.0}          # 스냅샷 갱신(다음 비교용)


def test_experiment_explained_change_not_halted_live():
    """직전 실행이 남긴 열린주문의 지연 체결로 보유가 는 건 정상 — halt하지 않는다."""
    exp = ExperimentPortfolio(initial_capital=100.0, cash=100.0)
    cfg, state, guardrail, cost = _ctx(exp)
    state.last_account_snapshot = {"AAPL": 0.0}
    coid = ob.stable_client_order_id("events", _DATE_ISO, "AAPL", Side.BUY)
    rec = ob.make_intent(coid, Order("AAPL", Side.BUY, 0.5), reserved_cash=5.0)
    rec["broker_order_id"] = "ord-1"
    rec["status"] = "submitted"
    state.open_orders[coid] = rec
    broker = FakeLiveBroker(price=10.0, holdings={"AAPL": 0.5})   # 열린주문이 이미 체결된 계좌

    fills, _ = _run(cfg, state, exp, [], guardrail, cost, broker)

    assert exp.can_trade                                         # 설명 가능 → halt 없음
    ep = exp.experiment_positions.get("AAPL")
    assert ep and abs(ep.quantity - 0.5) < 1e-9                  # 지연체결분 장부 반영
    assert not any(f.get("status") == "halted" for f in fills)


def test_first_run_snapshot_persisted_for_next_comparison():
    """정상 실행이면 종료 시점 보유가 다음 실행 비교 기준선으로 영속된다."""
    exp = ExperimentPortfolio(initial_capital=100.0, cash=100.0)
    cfg, state, guardrail, cost = _ctx(exp)
    state.last_account_snapshot = {}                            # 첫 실행(비교 기준선 없음)
    broker = FakeLiveBroker(price=10.0, holdings={"AAPL": 0.0})
    _run(cfg, state, exp, [], guardrail, cost, broker)
    assert state.last_account_snapshot == {"AAPL": 0.0}         # 종료 스냅샷 저장
    assert exp.can_trade


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 사건 감지 테스트 통과")
