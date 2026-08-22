"""PairEntryDecider — 인버스 페어 진입 어댑터(기댓값 게이트·상호배타·동적 임계).

[What] 전략(snapshot→signals)을 감싸, 진입 전에 세 규칙을 적용한다:
       ① 기댓값 E=p·g−(1−p)·loss 이 문턱 미만이면 대기(빈 신호)
       ② 페어 중 한 방향만 남김(상호배타)
       ③ 통과하면 그 날 익절/손절 임계를 clamp해 risk에 확정 주입(동적 임계)
[Why]  '진입 결정은 기댓값에 의거, 청산은 여전히 결정론'을 지키는 이음매. 임계를 진입 시
       한 번 확정하면 이후 서킷브레이커는 고정값으로만 판정 → LLM은 청산에 개입하지 않는다.
[How]  risk(DayRiskManager) 참조를 들고 set_thresholds를 직접 호출 → 오케스트레이터 무변경.
       strategy는 generate_signal(snap)과 last_decision(원 결정 dict)을 노출하면 무엇이든 OK
       (LLMAgentStrategy 또는 테스트용 스텁). 설계: docs/design-leveraged-pair.md
"""
from __future__ import annotations

from dataclasses import replace

from src.core.types import Action, MarketSnapshot, Signal
from src.risk.day_risk import DayRiskManager
from src.strategy.llm_agent.decision_tool import decision_thresholds
from src.strategy.pair_gate import clamp_thresholds, mutual_exclusive, passes_gate


class PairEntryDecider:
    def __init__(
        self,
        strategy,
        risk: DayRiskManager,
        *,
        e_min: float = 0.0,
        default_target: float = 0.04,   # LLM이 g를 안 주면 기본 목표(+4%)
        default_stop: float = 0.02,     # LLM이 loss를 안 주면 기본 손절폭(2%)
        tp_bounds: tuple[float, float] = (0.02, 0.08),
        sl_bounds: tuple[float, float] = (0.01, 0.04),
    ) -> None:
        self.strategy = strategy
        self.risk = risk
        self.e_min = e_min
        self.default_target = default_target
        self.default_stop = default_stop
        self.tp_bounds = tp_bounds
        self.sl_bounds = sl_bounds

    def __call__(self, snapshot: MarketSnapshot) -> list[Signal]:
        signals = self.strategy.generate_signal(snapshot)
        decision = getattr(self.strategy, "last_decision", None) or {}

        p, g, loss = decision_thresholds(decision, signals)
        g = g if g is not None else self.default_target
        loss = loss if loss is not None else self.default_stop

        # ① 기댓값 게이트 — 미달이면 대기
        if not passes_gate(p, g, loss, self.e_min):
            return []

        # ② 상호배타 — 한 방향만
        excl = mutual_exclusive({s.symbol: s.target_weight for s in signals})
        if not any(w > 0 for w in excl.values()):
            return []                       # 방향 모호 → 현금 대기

        # ③ 동적 임계 확정(진입 시 1회) → 이후 서킷브레이커가 결정론 판정
        tp, sl = clamp_thresholds(g, loss, tp_bounds=self.tp_bounds, sl_bounds=self.sl_bounds)
        self.risk.set_thresholds(tp, sl)

        return [
            replace(s, target_weight=excl[s.symbol],
                    action=Action.BUY if excl[s.symbol] > 0 else Action.SELL)
            for s in signals
        ]
