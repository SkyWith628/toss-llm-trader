"""메타-포트폴리오(전략 배분 레이어) — 여러 전략을 자본 예산으로 합성한다.

설계: docs/design-meta-portfolio.md
- Sleeve                : 한 전략 + 그 유니버스(배분 단위).
- Allocator            : 슬리브별 자본 비중 a_i 산출(고정·역변동성 …).
- MetaPortfolioStrategy : Composite — 서브전략 신호를 a_i로 스케일·합산해 통합 Signal 반환.
"""
from __future__ import annotations

from src.portfolio.allocator import (
    Allocator,
    FixedAllocator,
    InverseVolAllocator,
    RiskParityAllocator,
)
from src.portfolio.meta import MetaPortfolioStrategy
from src.portfolio.sleeve import Sleeve

__all__ = [
    "Sleeve",
    "Allocator",
    "FixedAllocator",
    "InverseVolAllocator",
    "RiskParityAllocator",
    "MetaPortfolioStrategy",
]
