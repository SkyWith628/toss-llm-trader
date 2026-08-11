"""성과·리스크 지표 — '종이수익'이 아닌 검증 가능한 성과 측정(FinTSB 논문 기준).

[What] 자본 곡선(equity curve)에서 CAGR·샤프·소르티노·MDD·Calmar를 계산.
[Why]  수익만 보면 위험을 숨긴다. 위험조정 성과로 전략을 공정 비교.
[How]  표준 라이브러리만으로 구현(외부 의존성 0).
"""
from __future__ import annotations

import math

TRADING_DAYS = 252


def daily_returns(equity: list[float]) -> list[float]:
    out = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev > 0:
            out.append(equity[i] / prev - 1.0)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def max_drawdown(equity: list[float]) -> float:
    """최대낙폭(MDD): 고점 대비 최대 하락률(음수)."""
    peak = equity[0] if equity else 0.0
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def compute_metrics(equity: list[float]) -> dict[str, float]:
    if len(equity) < 2:
        return {"error": 1.0}
    rets = daily_returns(equity)
    mu, sd = _mean(rets), _std(rets)
    downside = _std([r for r in rets if r < 0])
    total_return = equity[-1] / equity[0] - 1.0
    years = len(equity) / TRADING_DAYS
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1.0 if years > 0 else 0.0
    mdd = max_drawdown(equity)

    sharpe = (mu / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else 0.0
    sortino = (mu / downside * math.sqrt(TRADING_DAYS)) if downside > 0 else 0.0
    calmar = (cagr / abs(mdd)) if mdd < 0 else 0.0

    return {
        "total_return": round(total_return, 4),
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "mdd": round(mdd, 4),
        "calmar": round(calmar, 3),
        "final_value": round(equity[-1], 2),
        "days": len(equity),
    }
