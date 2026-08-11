"""계좌 대사(reconciliation) — 실험 장부와 실제 계좌가 어긋나면 '주문 중단'(fail-closed).

[What] 실제 계좌 보유량이 '보호분 + 실험분' 이상인지, 실험 현금이 음수가 아닌지 검사.
[Why]  사용자가 앱에서 같은 종목을 직접 매매/출금하면 장부의 귀속 추정이 깨진다. 그 상태로
       자동매도를 계속하면 보호분(기존 자산)을 침범할 수 있다. 안전하게 추측하지 말고 멈춘다.
[How]  매 실행 시작 전·주문 체결 후 호출. 위반 시 status를 바꾸고 신규 주문을 막는다.
       (EXPERIMENT_PORTFOLIO_SPEC.md §계좌 대사 — 단계 (b) 증분1.)
"""
from __future__ import annotations

from src.forward.experiment_portfolio import ExperimentPortfolio

_TOL = 1e-6


def reconcile(
    exp: ExperimentPortfolio, account_holdings: dict[str, float]
) -> tuple[bool, str]:
    """실험 장부 vs 실제 계좌 대사. (ok, 사유) 반환. 위반 시 exp.status는 바꾸지 않는다(호출자 책임).

    account_holdings: 실제 계좌의 종목별 총 보유 수량(protected+experiment+그 외 전부 합).
    불변식: 실제 보유량 >= 보호 수량 + 실험 장부 수량  (모든 관련 종목)
            실험 현금 >= 0
    """
    if exp.cash < -_TOL:
        return False, f"실험 현금 음수: {exp.cash:.4f}"

    symbols = set(exp.protected_positions) | set(exp.experiment_positions)
    for sym in symbols:
        protected = exp.protected_positions.get(sym, 0.0)
        ep = exp.experiment_positions.get(sym)
        experiment = ep.quantity if ep else 0.0
        need = protected + experiment
        actual = account_holdings.get(sym, 0.0)
        if actual + _TOL < need:
            return False, (
                f"{sym}: 실계좌 {actual:.6f} < 보호 {protected:.6f} + 실험 {experiment:.6f} "
                f"(= {need:.6f}) — 수동매매/출금/기업행동 의심")
    return True, "ok"


def reconcile_or_halt(
    exp: ExperimentPortfolio, account_holdings: dict[str, float]
) -> tuple[bool, str]:
    """대사 실패 시 exp를 reconciliation_required로 전환하고 (False, 사유) 반환(신규 주문 차단용)."""
    ok, reason = reconcile(exp, account_holdings)
    if not ok:
        exp.require_reconciliation(reason)
    return ok, reason
