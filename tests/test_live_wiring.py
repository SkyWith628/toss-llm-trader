"""증분3b live 배선 통합 테스트 — 전후 대사·부분체결 델타·재시작 멱등성.

필수테스트 10(부분 체결 시 실제 체결 수량만 장부 반영) · 11(타임아웃/재시작 후 재시도해도
중복 주문 없음)을 네트워크·실주문 0으로 검증한다. 내부 브로커는 FakeLiveBroker로 주입.

실행: python3 tests/test_live_wiring.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Order, OrderResult, Side
from src.execution.costs import CostModel
from src.risk.guardrail import Guardrail
from src.forward import order_book as ob
from src.forward import runner
from src.forward.experiment_portfolio import ExperimentPortfolio, ExperimentPosition
from src.forward.state import ForwardState

_DATE = datetime(2026, 8, 13)
_DATE_ISO = "2026-08-13"
_PRICES = {"AAPL": 10.0}


class FakeLiveBroker:
    """CarveOutBroker 계약을 흉내(account_holdings/place_and_settle/settle_existing).

    fill_ratio=1.0=완전체결, 0.5=부분체결(계속 partially_filled). 같은 client_order_id는 같은
    orderId로 dedup(멱등). 완전체결은 실계좌 보유(holdings)에 반영(대사 통과되도록).
    """

    def __init__(self, price=10.0, holdings=None, fill_ratio=1.0, commission=0.0):
        self.price = price
        self.holdings = dict(holdings or {})
        self.fill_ratio = fill_ratio
        self.commission = commission
        self.seq = 0
        self.key_to_oid: dict[str, str] = {}
        self.orders: dict[str, Order] = {}
        self.place_calls: list[str] = []          # place_and_settle에 넘어온 client_order_id
        self.settle_existing_calls: list[str] = []

    def account_holdings(self):
        return dict(self.holdings)

    def _result(self, order, oid, *, reflect_holding):
        filled = order.quantity * self.fill_ratio
        status = "filled" if self.fill_ratio >= 1.0 else "partially_filled"
        if reflect_holding and order.side is Side.BUY:
            self.holdings[order.symbol] = self.holdings.get(order.symbol, 0.0) + filled
        return OrderResult(order, filled, self.price, self.commission, status,
                           datetime.now(), oid)

    def place_and_settle(self, order, client_order_id, when=None):
        self.place_calls.append(client_order_id)
        oid = self.key_to_oid.get(client_order_id)
        if oid is None:                            # 새 키 → 새 orderId(같은 키면 재사용=멱등)
            self.seq += 1
            oid = f"ord-{self.seq}"
            self.key_to_oid[client_order_id] = oid
            self.orders[oid] = order
        return self._result(order, oid, reflect_holding=True)

    def settle_existing(self, broker_order_id, order, when=None):
        self.settle_existing_calls.append(broker_order_id)
        return self._result(order, broker_order_id, reflect_holding=False)  # 이미 반영됨


def _ctx(exp):
    """_execute_live 호출에 필요한 cfg/state/guardrail/cost 묶음(임시 state_dir)."""
    tmp = tempfile.mkdtemp(prefix="livewire_")
    cfg = runner.ForwardConfig(symbols=["AAPL"], mode="live", state_dir=tmp)
    state = ForwardState(budget=100.0, symbols=["AAPL"], source="synthetic",
                         strategy="momentum", model="-", experiment=exp)
    guardrail = Guardrail(budget=100.0, valid_symbols={"AAPL"})
    cost = CostModel(slippage=0.0005)
    return cfg, state, guardrail, cost


def _run(cfg, state, exp, orders, guardrail, cost, broker):
    return runner._execute_live(cfg, state, exp, orders, guardrail,
                                _PRICES, _DATE, _DATE_ISO, cost, broker)


# ---------- 필수테스트 10: 부분 체결은 체결분만 장부에 반영 ----------
def test_partial_fill_accounts_only_filled():
    exp = ExperimentPortfolio(initial_capital=100.0, cash=100.0)
    cfg, state, guardrail, cost = _ctx(exp)
    broker = FakeLiveBroker(price=10.0, fill_ratio=0.5)   # 요청 1.0 중 0.5만 체결
    fills, _ = _run(cfg, state, exp, [Order("AAPL", Side.BUY, 1.0)], guardrail, cost, broker)

    ep = exp.experiment_positions.get("AAPL")
    assert ep and abs(ep.quantity - 0.5) < 1e-9          # 체결분(0.5)만 반영
    assert abs(exp.cash - (100.0 - 0.5 * 10.0)) < 1e-9   # 현금도 체결분만 차감
    # 잔량은 열린주문으로 남아 다음 실행이 이어받는다(부분체결은 종결 아님).
    open_rec = list(state.open_orders.values())
    assert len(open_rec) == 1 and open_rec[0]["status"] == "partially_filled"
    assert abs(open_rec[0]["accounted_filled_qty"] - 0.5) < 1e-9


# ---------- 필수테스트 11: 재시작 후 재시도해도 중복 주문 없음 ----------
def test_restart_recovers_via_query_not_resend():
    """orderId를 아는 미결 주문은 '조회'로만 복구한다(재전송=중복주문 금지)."""
    exp = ExperimentPortfolio(initial_capital=100.0, cash=100.0)
    cfg, state, guardrail, cost = _ctx(exp)
    # 지난 실행: 전송+orderId 기록됐으나 체결 반영 전 프로세스 사망(열린주문으로 잔존).
    coid = ob.stable_client_order_id("livewire", _DATE_ISO, "AAPL", Side.BUY)
    rec = ob.make_intent(coid, Order("AAPL", Side.BUY, 1.0), reserved_cash=10.0)
    rec["broker_order_id"] = "ord-1"
    rec["status"] = "submitted"
    state.open_orders[coid] = rec
    broker = FakeLiveBroker(price=10.0, fill_ratio=1.0, holdings={"AAPL": 1.0})

    fills, _ = _run(cfg, state, exp, [], guardrail, cost, broker)

    assert broker.settle_existing_calls == ["ord-1"]     # 조회로 복구
    assert broker.place_calls == []                       # 재전송 안 함(중복주문 0)
    ep = exp.experiment_positions.get("AAPL")
    assert ep and abs(ep.quantity - 1.0) < 1e-9           # 체결분 장부 반영
    assert state.open_orders == {}                        # 완전체결 → 열린주문 정리


def test_restart_resends_with_same_key_when_orderid_unknown():
    """전송 후 orderId 저장 전 사망 → 같은 멱등키로 재전송, 브로커가 dedup(단일 주문)."""
    exp = ExperimentPortfolio(initial_capital=100.0, cash=100.0)
    cfg, state, guardrail, cost = _ctx(exp)
    coid = ob.stable_client_order_id("livewire", _DATE_ISO, "AAPL", Side.BUY)
    rec = ob.make_intent(coid, Order("AAPL", Side.BUY, 1.0), reserved_cash=10.0)
    # broker_order_id="" (아직 모름) → 재전송 경로
    state.open_orders[coid] = rec
    broker = FakeLiveBroker(price=10.0, fill_ratio=1.0)

    _run(cfg, state, exp, [], guardrail, cost, broker)

    assert broker.place_calls == [coid]                   # 같은 멱등키로 1회만
    assert broker.seq == 1                                 # 단일 orderId(중복주문 없음)


def test_two_sequential_runs_no_duplicate_send():
    """프로세스 재시작 시뮬(상태 파일 왕복) — 부분체결이 완결돼도 중복 전송이 없어야."""
    exp = ExperimentPortfolio(initial_capital=100.0, cash=100.0)
    cfg, state, guardrail, cost = _ctx(exp)
    broker = FakeLiveBroker(price=10.0, fill_ratio=0.5)   # 1차: 절반만 체결
    _run(cfg, state, exp, [Order("AAPL", Side.BUY, 1.0)], guardrail, cost, broker)
    state.save(cfg.state_path)

    # --- '재시작': 상태 파일에서 복원(같은 open_orders) ---
    reloaded = ForwardState.load_or_init(
        cfg.state_path, budget=100.0, symbols=["AAPL"],
        source="synthetic", strategy="momentum", model="-")
    exp2 = reloaded.experiment
    broker.fill_ratio = 1.0                                # 2차: 나머지까지 체결
    _run(cfg, reloaded, exp2, [], guardrail, cost, broker)   # 신규 주문 없음, 복구만

    coid = broker.place_calls[0]
    assert broker.place_calls == [coid]                    # 1차 전송 1회뿐(2차는 재전송 없음)
    assert broker.settle_existing_calls == ["ord-1"]       # 2차는 orderId 조회로만 복구
    assert broker.seq == 1                                 # 끝까지 단일 orderId(중복주문 0)
    ep = exp2.experiment_positions.get("AAPL")
    assert ep and abs(ep.quantity - 1.0) < 1e-9            # 최종 누적 체결 = 1.0(이중 카운트 없음)
    assert reloaded.open_orders == {}                      # 완결 → 열린주문 정리


# ---------- 대사 fail-closed: 시작 대사 실패면 신규 주문 0 ----------
def test_start_reconcile_failure_blocks_orders():
    exp = ExperimentPortfolio(
        initial_capital=100.0, cash=50.0,
        experiment_positions={"AAPL": ExperimentPosition("AAPL", 1.0, 10.0)})
    cfg, state, guardrail, cost = _ctx(exp)
    # 실계좌 보유(0.2)가 실험 장부(1.0)보다 적음 → 수동매도/출금 의심 → 즉시 중단.
    broker = FakeLiveBroker(price=10.0, holdings={"AAPL": 0.2})
    fills, _ = _run(cfg, state, exp, [Order("AAPL", Side.BUY, 1.0)], guardrail, cost, broker)

    assert not exp.can_trade                               # reconciliation_required로 전환
    assert broker.place_calls == []                        # 신규 주문 전송 0
    assert any(f.get("status") == "halted" for f in fills)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 live 배선 테스트 통과")
