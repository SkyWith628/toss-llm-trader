"""DayRiskManager — 당일 손익 서킷브레이커(결정론적 안전장치).

[What] 개장 시 '당일 시작 자산'을 앵커로 고정하고, 장중 현재 자산이 앵커 대비
       +take_profit(기본 +2%) 이상이면 TAKE_PROFIT, -stop_loss(기본 -1%) 이하면
       STOP_LOSS를 판정한다. 도달 시 상위(오케스트레이터)가 전량 강제청산한다.
[Why]  손실 한도는 '판단'이 아니라 '안전장치'다. 비결정적 LLM 프롬프트에 맡기면 안 지킬 수
       있으므로 결정론적 레이어에서 하드 강제한다. 주문 단위 Guardrail과는 직교(관심사 분리):
       Guardrail은 주문 물리검증(무상태), 여기는 포트폴리오 일손익(상태) 감시.
[How]  순수 로직 — 상태는 앵커 하나뿐. '발동 후 당일 잠금(halt)'은 여기가 아니라
       오케스트레이터/State의 책임(SRP). 여기는 매 틱 '지금 어떤 액션인가'만 결정론적으로 답한다.
"""
from __future__ import annotations

from enum import Enum

from src.config import DEFAULT_STOP_LOSS, DEFAULT_TAKE_PROFIT


class RiskAction(str, Enum):
    """서킷브레이커 판정 결과."""
    NORMAL = "normal"                # 임계 미도달 — 계속 감시
    TAKE_PROFIT = "take_profit"      # +임계 도달 — 익절 전량청산
    STOP_LOSS = "stop_loss"          # -임계 도달 — 손절 전량청산
    TRAILING_STOP = "trailing_stop"  # 장중 고점 대비 되돌림 — 트레일링 청산(이익 보호)


class DayRiskManager:
    def __init__(
        self,
        take_profit: float = DEFAULT_TAKE_PROFIT,
        stop_loss: float = DEFAULT_STOP_LOSS,
        trailing: float | None = None,   # 고점 수익률 대비 되돌림 폭 δ(>0). None이면 트레일링 끔.
        trailing_arm: float = 0.0,       # 고점이 이 값(a) 이상일 때만 트레일링 무장(초기 노이즈 방지).
    ) -> None:
        if take_profit <= 0:
            raise ValueError(f"take_profit은 양수여야 함: {take_profit}")
        if stop_loss >= 0:
            raise ValueError(f"stop_loss는 음수여야 함: {stop_loss}")
        if trailing is not None and trailing <= 0:
            raise ValueError(f"trailing(δ)은 양수여야 함: {trailing}")
        if trailing_arm < 0:
            raise ValueError(f"trailing_arm(a)은 음수 불가: {trailing_arm}")
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.trailing = trailing
        self.trailing_arm = trailing_arm
        self._anchor: float | None = None
        self._peak: float = 0.0          # 당일 고점 수익률 M_t = max_{s<=t} r_s (앵커=0에서 시작)

    def anchor(self, day_start_equity: float) -> None:
        """당일 시작 자산을 기준점으로 고정(개장 시 1회). 재시작 복원 시에도 같은 값으로.

        주의: 장중 고점 _peak은 프로세스 내에서만 유지된다(재시작 시 0으로 리셋). 트레일링을
        재시작 넘어 이어가려면 IntradayState에 peak를 지속시켜야 함(후속 과제).
        """
        if day_start_equity <= 0:
            raise ValueError(f"당일 시작 자산은 양수여야 함: {day_start_equity}")
        self._anchor = day_start_equity
        self._peak = 0.0

    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    def daily_return(self, current_equity: float) -> float:
        """앵커 대비 현재 수익률. 앵커 미설정 시 에러(개장 앵커가 선행돼야 함)."""
        if self._anchor is None:
            raise RuntimeError("anchor()로 당일 시작 자산을 먼저 고정해야 합니다.")
        return current_equity / self._anchor - 1.0

    @property
    def peak_return(self) -> float:
        """지금까지의 당일 고점 수익률(트레일링 기준·진단용)."""
        return self._peak

    def evaluate(self, current_equity: float) -> RiskAction:
        """현재 자산으로 서킷브레이커 액션 판정. 우선순위: 익절 → 손절 → 트레일링.

        통합 트리거:  exit if  r_t≥θ⁺  ∨  r_t≤θ⁻  ∨  (M_t≥a ∧ r_t≤M_t−δ)
        - θ⁺ 익절·θ⁻ 손절은 하드 경계(경계값 포함).
        - 트레일링: 고점 M_t가 arm(a) 이상으로 오른 뒤, 고점 대비 δ 이상 되돌리면 청산(이익 보호).
          trailing=None이면 이 항은 꺼짐 → 기존 동작과 완전 동일(하위호환).
        """
        ret = self.daily_return(current_equity)
        self._peak = max(self._peak, ret)          # M_t 갱신(먼저)
        if ret >= self.take_profit:
            return RiskAction.TAKE_PROFIT
        if ret <= self.stop_loss:
            return RiskAction.STOP_LOSS
        if (self.trailing is not None
                and self._peak >= self.trailing_arm
                and ret <= self._peak - self.trailing):
            return RiskAction.TRAILING_STOP
        return RiskAction.NORMAL
