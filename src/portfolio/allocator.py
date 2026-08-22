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


class RiskParityAllocator(Allocator):
    """리스크패리티(등리스크기여, ERC) — 각 슬리브가 포트폴리오 위험에 '똑같이' 기여하게.

    InverseVol과의 차이: 슬리브 간 '상관'을 반영한다. 상관 높은 슬리브 묶음(같은 리스크를
    중복 부담)은 자본을 덜 받는다. 2슬리브에선 상관과 무관하게 역변동성과 동일 — 상관이
    의미를 갖는 건 3슬리브 이상(그때 invvol과 갈라진다).

    구현: 슬리브별 대리 수익률(종목 동일가중)로 공분산 Σ를 추정하고, ERC 조건
    (w_i·(Σw)_i 가 모두 동일)을 곱셈 갱신 반복해로 푼다. numpy 없이 stdlib만.
    추정 불가(히스토리 부족·무변동·수렴 실패)면 동일가중 폴백.
    """
    name = "risk_parity"

    def __init__(self, lookback: int = 20, iters: int = 300,
                 tol: float = 1e-8, floor: float = 1e-12) -> None:
        self.lookback = max(2, lookback)
        self.iters = iters
        self.tol = tol
        self.floor = floor

    def weights(self, sleeves: list[Sleeve],
                snapshot: MarketSnapshot | None = None) -> dict[str, float]:
        if not sleeves:
            return {}
        names = [s.name for s in sleeves]
        sigma = {n: self._sleeve_sigma(s, snapshot) for n, s in zip(names, sleeves)}
        proxy = {n: self._sleeve_proxy(s, snapshot) for n, s in zip(names, sleeves)}
        if any(sigma[n] is None or proxy[n] is None for n in names):
            return _equal_weight(sleeves)

        length = min(len(proxy[n]) for n in names)        # 시점 정렬(공통 최소 길이)
        if length < 2:
            return _equal_weight(sleeves)
        mats = [proxy[n][-length:] for n in names]
        corr = _corr_matrix(mats)                          # 상관만 대리수익률에서
        sig = [sigma[n] for n in names]
        # Σ_ab = ρ_ab·σ_a·σ_b — 대각은 invvol 일관 σ(종목 σ 평균), 상쇄 함정 회피.
        cov = [[corr[a][b] * sig[a] * sig[b] for b in range(len(names))]
               for a in range(len(names))]
        w = _erc_weights(cov, self.floor, self.iters, self.tol)
        if w is None:
            return _equal_weight(sleeves)
        return {names[i]: w[i] for i in range(len(names))}

    def _sleeve_sigma(self, sleeve: Sleeve,
                      snapshot: MarketSnapshot | None) -> float | None:
        """슬리브 위험 크기 = 종목 일수익률 표준편차의 평균(InverseVol과 일관).

        인버스 페어처럼 동일가중이 상쇄돼도, 개별 종목 σ의 평균이라 '한 레그 보유'의
        실제 고변동을 반영한다(리스크패리티 과배분 방지).
        """
        if snapshot is None:
            return None
        stdevs: list[float] = []
        for sym in sleeve.symbols:
            window = snapshot.history.get(sym, [])[-(self.lookback + 1):]
            rets = [window[i] / window[i - 1] - 1.0
                    for i in range(1, len(window)) if window[i - 1] > 0]
            if len(rets) >= 2:
                stdevs.append(_stdev(rets))
        if not stdevs:
            return None
        return sum(stdevs) / len(stdevs)

    def _sleeve_proxy(self, sleeve: Sleeve,
                      snapshot: MarketSnapshot | None) -> list[float] | None:
        """상관 추정용 대리 수익률 = 종목 동일가중 평균. 크기(σ)는 _sleeve_sigma가 따로 준다."""
        if snapshot is None:
            return None
        per_sym: list[list[float]] = []
        for sym in sleeve.symbols:
            window = snapshot.history.get(sym, [])[-(self.lookback + 1):]
            rets = [window[i] / window[i - 1] - 1.0
                    for i in range(1, len(window)) if window[i - 1] > 0]
            if len(rets) >= 2:
                per_sym.append(rets)
        if not per_sym:
            return None
        length = min(len(r) for r in per_sym)
        if length < 2:
            return None
        return [sum(r[-length:][t] for r in per_sym) / len(per_sym)
                for t in range(length)]


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


def _corr_matrix(series: list[list[float]]) -> list[list[float]]:
    """S개 수익률 계열(각 길이 L, 시점 정렬됨) → S×S 상관 행렬.

    무변동(표준편차~0, 예: 인버스 페어 상쇄) 계열은 상관을 정의할 수 없으므로 그 행/열을
    비대각 0(무상관)으로 둔다 — 대각은 항상 1.
    """
    s = len(series)
    length = len(series[0])
    means = [sum(row) / length for row in series]
    sds = [_stdev(row) for row in series]
    corr = [[1.0 if a == b else 0.0 for b in range(s)] for a in range(s)]
    for a in range(s):
        for b in range(a + 1, s):
            if sds[a] <= 0 or sds[b] <= 0:
                continue                                # 무상관 처리(대각 1 유지)
            cov = sum((series[a][t] - means[a]) * (series[b][t] - means[b])
                      for t in range(length)) / (length - 1)
            corr[a][b] = corr[b][a] = cov / (sds[a] * sds[b])
    return corr


def _matvec(mat: list[list[float]], vec: list[float]) -> list[float]:
    """행렬×벡터."""
    return [sum(mat[i][j] * vec[j] for j in range(len(vec))) for i in range(len(mat))]


def _normalize(w: list[float]) -> None:
    """제자리 정규화(합=1). 합≤0이면 균등."""
    total = sum(w)
    if total <= 0:
        n = len(w)
        for i in range(n):
            w[i] = 1.0 / n
        return
    for i in range(len(w)):
        w[i] /= total


def _erc_weights(cov: list[list[float]], floor: float,
                 iters: int, tol: float) -> list[float] | None:
    """등리스크기여(ERC) 가중 — w_i·(Σw)_i 가 모두 같아지는 w(합=1, 양수). 실패 시 None.

    곱셈 갱신 반복해: w_i ← w_i·√(b_i / rc_i)  (b_i=1/S 목표기여, rc_i=상대 리스크기여).
    √ 감쇠로 진동을 억제. 대각(분산)이 양수여야 하며, 수렴 못 하면 마지막 근사를 반환.
    """
    s = len(cov)
    vol = [math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0.0 for i in range(s)]
    if any(v <= 0 for v in vol):
        return None
    w = [1.0 / v for v in vol]           # 역변동성으로 시드
    _normalize(w)
    budget = 1.0 / s
    for _ in range(iters):
        sw = _matvec(cov, w)
        port_var = sum(w[i] * sw[i] for i in range(s))
        if port_var <= 0:
            return None
        rc = [w[i] * sw[i] / port_var for i in range(s)]   # 상대 리스크기여(합=1)
        if max(abs(rc[i] - budget) for i in range(s)) < tol:
            break
        w = [max(w[i] * math.sqrt(budget / max(rc[i], floor)), floor) for i in range(s)]
        _normalize(w)
    return w
