"""Allocator — 슬리브별 자본 비중 a_i 산출(배분 정책). 전략과 직교(SRP).

[What] 슬리브 목록(+시점 관측치)을 받아 {슬리브이름: a_i}를 반환. Σa_i ≤ 1(나머지 현금).
[Why]  "무엇을 사나(Strategy)"와 "각 전략에 얼마를 주나(Allocator)"는 다른 축. 정책만 갈아끼워
       고정 → 역변동성 → 리스크패리티를 데이터로 A/B 하는 게 이 실험의 핵심.
[How]  순수 계열(외부 상태 0). 역변동성은 시점 스냅샷의 과거 종가로 σ를 추정 — 실현 손익
       피드백 없이 Strategy 계약 안에서 매일 재산출된다. 설계: docs/design-meta-portfolio.md
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod

from src.core.types import MarketSnapshot
from src.portfolio.sleeve import Sleeve


class Allocator(ABC):
    name: str = "base"

    @abstractmethod
    def weights(self, sleeves: list[Sleeve],
                snapshot: MarketSnapshot | None = None) -> dict[str, float]:
        """슬리브별 자본 비중 {name: a_i} (a_i ≥ 0, Σ ≤ 1). 빈 슬리브면 빈 dict."""
        raise NotImplementedError


class FixedAllocator(Allocator):
    """고정 비중 — 사용자가 지정한 슬리브별 a_i를 그대로(합 > 1이면 비례 정규화).

    예) FixedAllocator({"core": 0.5, "pair": 0.2, "universe": 0.3}). 미지정 슬리브는 0.
    """
    name = "fixed"

    def __init__(self, allocations: dict[str, float]) -> None:
        if any(v < 0 for v in allocations.values()):
            raise ValueError(f"음수 배분 불가: {allocations}")
        self._alloc = dict(allocations)

    def weights(self, sleeves: list[Sleeve],
                snapshot: MarketSnapshot | None = None) -> dict[str, float]:
        raw = {s.name: self._alloc.get(s.name, 0.0) for s in sleeves}
        total = sum(raw.values())
        if total > 1.0:                     # 과배분 → 비례 축소(현금 0)
            return {n: w / total for n, w in raw.items()}
        return raw                          # 합 ≤ 1 → 그대로(나머지 현금)


class InverseVolAllocator(Allocator):
    """역변동성 — 최근 변동성이 큰 슬리브에 자본을 적게(a_i ∝ 1/σ_i), 합=1로 정규화.

    σ_i는 슬리브 종목들의 최근 lookback일 일수익률 표준편차의 평균(동일가중 프록시).
    데이터 부족(추정 불가) 슬리브가 하나라도 있으면 안전하게 동일가중으로 폴백.
    """
    name = "inverse_vol"

    def __init__(self, lookback: int = 20, floor: float = 1e-6) -> None:
        self.lookback = max(2, lookback)
        self.floor = floor              # σ=0(무변동) 시 0분모 방지 하한

    def weights(self, sleeves: list[Sleeve],
                snapshot: MarketSnapshot | None = None) -> dict[str, float]:
        if not sleeves:
            return {}
        vols = {s.name: self._sleeve_vol(s, snapshot) for s in sleeves}
        if any(v is None for v in vols.values()):       # 추정 불가 → 동일가중 폴백
            return _equal_weight(sleeves)
        inv = {n: 1.0 / max(v, self.floor) for n, v in vols.items()}
        z = sum(inv.values())
        if z <= 0:
            return _equal_weight(sleeves)
        return {n: w / z for n, w in inv.items()}

    def _sleeve_vol(self, sleeve: Sleeve,
                    snapshot: MarketSnapshot | None) -> float | None:
        """슬리브 종목들의 일수익률 표준편차 평균. 히스토리 부족이면 None."""
        if snapshot is None:
            return None
        stdevs: list[float] = []
        for sym in sleeve.symbols:
            closes = snapshot.history.get(sym, [])
            window = closes[-(self.lookback + 1):]
            rets = [window[i] / window[i - 1] - 1.0
                    for i in range(1, len(window)) if window[i - 1] > 0]
            if len(rets) >= 2:
                stdevs.append(_stdev(rets))
        if not stdevs:
            return None
        return sum(stdevs) / len(stdevs)


# --- 내부 헬퍼 ---
def _equal_weight(sleeves: list[Sleeve]) -> dict[str, float]:
    w = 1.0 / len(sleeves)
    return {s.name: w for s in sleeves}


def _stdev(xs: list[float]) -> float:
    """표본 표준편차(n-1). n<2면 0."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)
