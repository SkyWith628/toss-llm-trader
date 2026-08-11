"""Point-in-time 시계 — 정보 누수(Profit Mirage) 방지의 핵심.

[What] 백테스트에선 '가상 현재시각'을 전진시키고, 실거래에선 실제 now()를 준다.
[Why]  모든 데이터 조회가 clock.now() '이전'만 보게 강제하면, 미래 데이터를 훔쳐보는
       백테스트 편향을 원천 차단한다(특히 LLM은 학습 컷오프 이후 사건을 '기억'함).
[How]  실무 퀀트의 point-in-time database 개념을 시계 하나로 단순화.
"""
from __future__ import annotations

from datetime import datetime


class Clock:
    def now(self) -> datetime:  # pragma: no cover - 인터페이스
        raise NotImplementedError


class BacktestClock(Clock):
    """백테스트용: set_time으로 가상 시각을 전진시킨다."""

    def __init__(self) -> None:
        self._t: datetime | None = None

    def set_time(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        if self._t is None:
            raise RuntimeError("BacktestClock.now() 호출 전 set_time 필요")
        return self._t


class LiveClock(Clock):
    """실거래용: 실제 현재시각."""

    def now(self) -> datetime:
        return datetime.now()
