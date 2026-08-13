"""보호 기준선 초기화 — live 실험을 시작할 때 '실계좌의 기존 보유'를 한 번 고정한다.

[What] 실험 시작 전 실계좌 보유를 protected_positions(보호 기준선)로 박제한 신규 실험 장부를
       만든다. 실험분은 0, 실험 현금은 예산 전액. 초과 미귀속 수량 계산(운영 로그용)도 제공.
[Why]  증권사 계좌는 기존분과 실험분이 물리적으로 안 나뉜다. '실험 시작 시점의 보유'를 기준선
       으로 고정해야 그 이후 매수분만 실험에 귀속되고, 기존 보유는 절대 자동매도되지 않는다.
       기준선은 사용자가 명시적으로 확인하기 전엔 다시 계산·덮어쓰지 않는다(스펙 §최초 초기화).
[How]  이 모듈은 순수 계산이다 — 계좌 조회·사용자 확인·파일 저장은 호출자(forward_main)가 한다.
       여기서 '기준선을 만든다'와 '초과분을 보고한다'만 담당(자동 편입은 하지 않음).

주의: 기준선 생성은 '사용자 명시 확인 + 기존 상태 없음'을 전제로만 호출돼야 한다(호출자 책임).
"""
from __future__ import annotations

from src.forward.experiment_portfolio import ExperimentPortfolio

_TOL = 1e-9


def build_baseline(budget: float, account_holdings: dict[str, float]) -> ExperimentPortfolio:
    """실계좌 현 보유를 보호 기준선으로 고정한 '신규' 실험 장부를 만든다.

    account_holdings: 실계좌의 종목별 총 보유 수량(실험 시작 시점 스냅샷).
    실험분은 0으로 시작(스펙 필수테스트 1), 현금은 예산 전액. 0 이하 수량은 기준선에서 제외.
    """
    protected = {sym: qty for sym, qty in account_holdings.items() if qty > _TOL}
    return ExperimentPortfolio(
        initial_capital=budget, cash=budget,
        protected_positions=protected, experiment_positions={})


def unattributed(exp: ExperimentPortfolio, account_holdings: dict[str, float]) -> dict[str, float]:
    """실계좌 보유 중 '보호분+실험분'을 초과하는 미귀속 수량(운영 로그·화면용).

    사용자가 같은 종목을 직접 추가 매수하면 초과분이 생긴다. 이 수량은 자동매도 대상이 아니고
    장부에 자동 편입해서도 안 된다(스펙 P2) — 여기선 '보이게'만 계산해 돌려준다.
    """
    out: dict[str, float] = {}
    symbols = set(account_holdings) | set(exp.protected_positions) | set(exp.experiment_positions)
    for sym in symbols:
        protected = exp.protected_positions.get(sym, 0.0)
        ep = exp.experiment_positions.get(sym)
        experiment = ep.quantity if ep else 0.0
        extra = account_holdings.get(sym, 0.0) - protected - experiment
        if extra > _TOL:
            out[sym] = extra
    return out


def preview_lines(account_holdings: dict[str, float], budget: float) -> list[str]:
    """기준선 확인 프롬프트에 보여줄 사람이 읽는 요약(호출자가 print). 결정은 사용자가 한다."""
    lines = [
        "── 보호 기준선 미리보기 ──",
        f"  실험 예산(현금)   : ${budget:.2f}",
        "  보호 기준선(자동매도 대상 아님):",
    ]
    held = {s: q for s, q in account_holdings.items() if q > _TOL}
    if held:
        for sym in sorted(held):
            lines.append(f"    - {sym}: {held[sym]:.6f}주")
    else:
        lines.append("    (없음 — 빈 계좌/전용 계좌)")
    return lines
