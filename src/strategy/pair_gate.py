"""인버스 페어 전략의 순수 로직 — 기댓값 게이트·상호배타·임계 클램프.

[What] 3x 인버스 페어(SOXL/SOXS 등) 진입 결정의 안전 규칙을 순수 함수로 모은다.
[Why]  LLM(비결정적)이 낸 방향·확률을 그대로 신뢰하면 위험 — 기댓값이 양(+)일 때만 진입,
       페어는 한 방향만(둘 다 들면 상쇄·decay), 임계는 3x용으로 상·하한에 가둔다.
[How]  외부 상태·의존성 0. 어댑터(PairEntryDecider)가 이 함수들로 신호·임계를 다듬는다.
       설계 근거: docs/design-leveraged-pair.md
"""
from __future__ import annotations


def expectancy(p: float, g: float, loss: float) -> float:
    """기댓값 E = p·g − (1−p)·loss.  p=상승확신(0~1), g=목표수익(>0), loss=손절폭(>0)."""
    return p * g - (1.0 - p) * loss


def passes_gate(p: float, g: float, loss: float, e_min: float = 0.0) -> bool:
    """기댓값이 진입 문턱(e_min) 이상인가. 아니면 대기(진입 보류)."""
    return expectancy(p, g, loss) >= e_min


def mutual_exclusive(weights: dict[str, float]) -> dict[str, float]:
    """페어 중 '한 방향만' 남긴다 — 최대 비중 하나만 유지, 나머지 0.

    동률(방향 모호)이거나 양수 비중이 없으면 전부 0(→ 현금이 중립). 둘 다 들면 상쇄+
    수수료·변동성 붕괴만 발생하므로 코드로 강제 차단한다.
    """
    positive = {s: w for s, w in weights.items() if w > 0}
    if not positive:
        return {s: 0.0 for s in weights}
    top = max(positive.values())
    winners = [s for s, w in positive.items() if w == top]
    if len(winners) != 1:                       # 동률 → 방향 모호 → 현금
        return {s: 0.0 for s in weights}
    keep = winners[0]
    return {s: (w if s == keep else 0.0) for s, w in weights.items()}


def clamp_thresholds(
    g: float,
    loss: float,
    *,
    tp_bounds: tuple[float, float] = (0.02, 0.08),
    sl_bounds: tuple[float, float] = (0.01, 0.04),
) -> tuple[float, float]:
    """LLM이 낸 목표 g·손절 loss를 3x용 상·하한에 가둬 (take_profit, stop_loss) 반환.

    take_profit은 양수, stop_loss는 음수. 기본 범위는 SOXL/SOXS 1분봉 관찰 기반
    (익절 +2~8%, 손절 −1~4%). LLM 미보정 값이 과대·과소여도 결정론적으로 캡된다.
    """
    tp_lo, tp_hi = tp_bounds
    sl_lo, sl_hi = sl_bounds
    take_profit = min(max(g, tp_lo), tp_hi)
    stop_mag = min(max(loss, sl_lo), sl_hi)
    return take_profit, -stop_mag
