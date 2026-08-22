"""변동성 스케일링 사이징 — 역변동성(risk-parity) 리웨이팅.

[What] 목표 비중을 각 종목의 최근 실현변동성에 반비례하게 재분배한다(총 투입비중은 보존).
[Why]  Moskowitz·Ooi·Pedersen(2012) 시계열 모멘텀 연구에서, 초과수익의 상당 부분이
       '무엇을 사느냐'가 아니라 '변동성으로 정규화한 사이징'에서 나온다(스케일링 제거 시
       월 알파 1.27%→0.41%). 조용한 종목은 크게, 요동치는 종목은 작게 잡아 리스크를 고르게.
[How]  순수 함수 — 외부 상태·의존성 없음. 전략(LLM/규칙)이 낸 base 비중에 후처리로 얹거나,
       진입 전 비중 산출에 쓴다. 표준 라이브러리만(통계는 직접 계산).
"""
from __future__ import annotations

from math import sqrt


def realized_vol(closes: list[float]) -> float:
    """최근 종가 시퀀스의 단순수익률 표준편차(실현변동성). 표본 부족 시 0.0.

    [How] r_i = c_i/c_{i-1} - 1 의 표본표준편차(모표준편차 n분모 아님, n-1). 데이터가 2개
          미만이거나 유효 수익률이 1개 이하면 0.0(사이징에서 '변동성 미상'으로 처리).
    """
    rets: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev:  # 0/음수 방어
            rets.append(cur / prev - 1.0)
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return sqrt(var)


def vol_scaled_weights(
    base: dict[str, float],
    vol: dict[str, float],
    *,
    floor: float = 1e-4,
) -> dict[str, float]:
    """base 비중을 역변동성으로 재분배(총합 보존). vol이 없거나 0인 종목은 floor로 대체.

    [What] w_i ∝ base_i / max(vol_i, floor) 로 재분배 후, 합이 Σbase_i가 되도록 정규화.
    [Why]  총 투입비중(=현금 완충)은 그대로 두고 '종목 간 배분'만 리스크 균형으로 바꾼다.
    [How]  base가 모두 0이거나 역가중 합이 0이면 base를 그대로 반환(무동작 안전).
    """
    total_base = sum(base.values())
    inv = {s: base[s] / max(vol.get(s, floor), floor) for s in base}
    total_inv = sum(inv.values())
    if total_base <= 0 or total_inv <= 0:
        return dict(base)
    return {s: total_base * inv[s] / total_inv for s in base}
