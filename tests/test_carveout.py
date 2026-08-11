"""카브아웃 실험 장부 테스트 — EXPERIMENT_PORTFOLIO_SPEC 필수 테스트(순수 로직 부분).

보호분 격리 · NAV 복리 · 실험분 한정 매도 · 대사 fail-closed 를 네트워크·live 없이 검증.
실행: python3 tests/test_carveout.py  (또는 python3 -m pytest -q)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, Side, Signal
from src.execution.rebalance import signals_to_orders
from src.risk.guardrail import Guardrail
from src.forward.experiment_portfolio import (
    ExperimentLedgerError, ExperimentPortfolio, ExperimentPosition,
    RECONCILIATION_REQUIRED)
from src.forward.reconcile import reconcile, reconcile_or_halt


def _exp(cash=100.0, protected=None, experiment=None):
    return ExperimentPortfolio(
        initial_capital=100.0, cash=cash,
        protected_positions=protected or {},
        experiment_positions=experiment or {})


# ── 필수테스트 1: 기존 AAPL 10주가 있어도 실험 최초 포지션은 0주 ──
def test_experiment_starts_empty_despite_protected():
    exp = _exp(protected={"AAPL": 10.0})
    assert exp.experiment_positions == {}
    assert exp.max_sellable("AAPL") == 0.0
    # 실험 슬라이스 투영엔 보호분이 없어야(리밸런서가 손댈 수 없음)
    assert "AAPL" not in exp.to_portfolio().positions


# ── 필수테스트 2 + 8: 실험 0.3주 매수 후 목표0 → 정확히 0.3주만 매도, 초과매도 차단 ──
def test_sell_only_experiment_quantity():
    exp = _exp(protected={"AAPL": 10.0})
    exp.apply_buy("AAPL", 0.3, fill_price=200.0)
    assert exp.max_sellable("AAPL") == 0.3

    prices = {"AAPL": 210.0}
    # 목표비중 0 → 리밸런서(실험 슬라이스)가 보유 0.3 전량 매도 주문
    orders = signals_to_orders([Signal("AAPL", Action.SELL, 0.0)],
                               exp.to_portfolio(), prices)
    assert len(orders) == 1 and orders[0].side is Side.SELL
    assert abs(orders[0].quantity - 0.3) < 1e-9

    g = Guardrail(budget=100.0, valid_symbols={"AAPL"})
    # 0.3 매도는 허용, 0.4(실험 초과)는 차단 — 보호분 침범 방지
    assert g.check(orders[0], exp.to_portfolio(), prices).ok
    from src.core.types import Order
    assert not g.check(Order("AAPL", Side.SELL, 0.4), exp.to_portfolio(), prices).ok


# ── 필수테스트 3: 보호분 10주는 자동매매 후에도 장부상 그대로 ──
def test_protected_untouched_after_sell():
    exp = _exp(protected={"AAPL": 10.0})
    exp.apply_buy("AAPL", 0.3, fill_price=200.0)
    exp.apply_sell("AAPL", 0.3, fill_price=210.0)
    assert "AAPL" not in exp.experiment_positions      # 실험분만 청산
    assert exp.protected_positions["AAPL"] == 10.0     # 보호분 불변
    assert exp.realized_pnl > 0                         # (210-200)*0.3 이익 기록


# ── 필수테스트 4·5: 리밸런싱은 초기원금이 아니라 현재 NAV 기준(복리/축소) ──
def test_rebalance_uses_current_nav_not_initial():
    # 이익: 현금10 + AAPL 0.5주@180(현재가) = 100 → NAV가 초기 90에서 100으로
    exp = ExperimentPortfolio(initial_capital=90.0, cash=10.0,
                              experiment_positions={"AAPL": ExperimentPosition("AAPL", 0.5, 100.0)})
    prices = {"AAPL": 180.0}
    assert abs(exp.nav(prices) - 100.0) < 1e-9         # 10 + 0.5*180
    # 목표 AAPL 60% → 목표평가액 = 0.6 * NAV(100) = 60, 현재 90 → 30 매도
    orders = signals_to_orders([Signal("AAPL", Action.SELL, 0.6)], exp.to_portfolio(), prices)
    assert len(orders) == 1 and orders[0].side is Side.SELL
    assert abs(orders[0].quantity * 180.0 - 30.0) < 1e-6   # ≈$30어치 매도(초기 90 아님)


# ── 필수테스트 9: 실계좌 수량 < 보호+실험 이면 대사 실패 ──
def test_reconcile_fails_when_account_short():
    exp = _exp(protected={"AAPL": 10.0})
    exp.apply_buy("AAPL", 0.3, fill_price=200.0)
    # 정상: 계좌 10.3 >= 10 + 0.3
    assert reconcile(exp, {"AAPL": 10.3})[0] is True
    # 위반: 사용자가 앱에서 0.5 팔아 9.8 → 10.3 필요 > 9.8
    ok, reason = reconcile(exp, {"AAPL": 9.8})
    assert ok is False and "AAPL" in reason


# ── 필수테스트 12: 대사 불일치 시 자동으로 기준선 바꾸지 말고 status 전환(신규주문 중단) ──
def test_reconcile_or_halt_sets_status_without_changing_baseline():
    exp = _exp(protected={"AAPL": 10.0})
    exp.apply_buy("AAPL", 0.3, fill_price=200.0)
    ok, _ = reconcile_or_halt(exp, {"AAPL": 9.0})   # 불일치
    assert ok is False
    assert exp.status == RECONCILIATION_REQUIRED
    assert not exp.can_trade                          # 신규 주문 차단
    assert exp.protected_positions["AAPL"] == 10.0    # 기준선 자동변경 금지


def test_apply_sell_overfill_fails_closed():
    """실체결이 실험 귀속 수량을 초과하면 조용히 축소하지 말고 fail-closed(보호분 침범 은폐 방지)."""
    exp = _exp(protected={"AAPL": 10.0})
    exp.apply_buy("AAPL", 0.3, fill_price=200.0)
    try:
        exp.apply_sell("AAPL", 0.4, fill_price=210.0)   # 실험분 0.3 초과 체결
        raised = False
    except ExperimentLedgerError:
        raised = True
    assert raised
    assert exp.status == RECONCILIATION_REQUIRED and not exp.can_trade
    assert exp.status_reason and "초과 체결" in exp.status_reason
    # 장부는 손대지 않아야(조용한 축소 금지) — 실험 0.3 그대로, 보호분 불변
    assert exp.experiment_positions["AAPL"].quantity == 0.3
    assert exp.protected_positions["AAPL"] == 10.0


def test_apply_buy_sell_cash_and_fees():
    exp = _exp(cash=100.0)
    exp.apply_buy("AAA", 1.0, fill_price=40.0, commission=0.1)
    assert abs(exp.cash - (100.0 - 40.0 - 0.1)) < 1e-9
    assert abs(exp.fees_paid - 0.1) < 1e-9
    exp.apply_sell("AAA", 1.0, fill_price=45.0, commission=0.1)
    assert abs(exp.cash - (59.9 + 45.0 - 0.1)) < 1e-9
    assert abs(exp.realized_pnl - ((45.0 - 40.0) * 1.0 - 0.1)) < 1e-9


def test_serialization_roundtrip():
    exp = _exp(protected={"AAPL": 10.0})
    exp.apply_buy("AAA", 0.5, fill_price=20.0, commission=0.05)
    exp.realized_pnl = 1.23
    exp.require_reconciliation("수동매매 의심")   # 중단 사유·시각이 영속되는지도 확인
    exp2 = ExperimentPortfolio.from_dict(exp.to_dict())
    assert exp2.protected_positions == {"AAPL": 10.0}
    assert exp2.experiment_positions["AAA"].quantity == 0.5
    assert abs(exp2.realized_pnl - 1.23) < 1e-9 and exp2.status == exp.status
    # 재시작 후에도 원인 확인 가능해야(P2 지적 반영)
    assert exp2.status_reason == "수동매매 의심" and exp2.status_changed_at


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 카브아웃 테스트 통과")
