"""MetaOrchestrator — N개 슬리브를 하나의 피드로 lockstep 구동(슬리브별 독립 서킷).

[What] 피드를 단일 소유하고, 개장 시세와 매 틱을 모든 SleeveAccount에 펌프한다. 각 슬리브는
       자기 broker·risk·state로 서킷을 '독립' 판정 — 한 슬리브가 halt돼도 나머지는 계속.
[Why]  v1의 공유 서킷 한계 해소: 3x 페어 슬리브의 손절이 코어 B&H까지 끌고 내려가지 않는다.
[How]  Orchestrator를 begin/process_tick/end로 분해해 뒀으므로(증분1), 루프 소유권만 여기로
       올려 N개에 같은 px를 흘려보낸다. 모든 슬리브가 halt되면 조기 종료. 피드 소진=폐장.
       설계: docs/design-sleeve-accounts.md
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from src.core.types import MarketSnapshot
from src.market.feed import FeedExhausted, StaleFeedError
from src.portfolio.allocator import Allocator
from src.portfolio.sleeve import Sleeve
from src.runtime.sleeve_account import SleeveAccount


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slice_capital(budget: float, sleeves: list[Sleeve], allocator: Allocator,
                  snapshot: MarketSnapshot | None = None) -> dict[str, float]:
    """개장 시 자본 슬라이스 — Allocator의 a_i로 {슬리브: budget·a_i} 산출.

    [Why] v2에서 각 슬리브 broker의 시작 자본을 배분 정책이 정한다. vol/corr 배분은 '일별
          히스토리 스냅샷'이 필요(장중 런타임엔 없으므로 개장 전 store에서 만들어 주입).
          FixedAllocator는 snapshot을 무시하므로 히스토리 없이도 동작.
    [How] Σa_i ≤ 1 이라 배정 합 ≤ budget(나머지는 미투입 현금). 슬리브 유니버스는 v1 Sleeve로
          기술(name·strategy·symbols) — 여기선 배분 계산에만 쓰이고, 실행기는 별도 조립한다.
    """
    alloc = allocator.weights(sleeves, snapshot)
    return {s.name: round(budget * alloc.get(s.name, 0.0), 4) for s in sleeves}


class MetaOrchestrator:
    def __init__(
        self,
        *,
        sleeves: list[SleeveAccount],
        clock,
        feed,
        poll_interval: float = 10.0,
        now_fn: Callable[[], datetime] = _utcnow,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if not sleeves:
            raise ValueError("슬리브가 최소 1개는 필요합니다.")
        names = [s.name for s in sleeves]
        if len(names) != len(set(names)):
            raise ValueError(f"슬리브 이름은 유일해야 합니다: {names}")
        self.sleeves = sleeves
        self.clock = clock
        self.feed = feed
        self.poll_interval = poll_interval
        self._now = now_fn
        self._sleep = sleep_fn or (lambda _s: None)

    def _all_halted(self) -> bool:
        return all(s.halted for s in self.sleeves)

    def run_trading_day(self) -> dict:
        """하루 실행 — 개장 시세 1회 fetch → 모든 슬리브 begin → 감시 루프(틱 펌프) → 결산.

        피드 소진(FeedExhausted)=폐장, StaleFeedError=이 틱 건너뜀. 모든 슬리브가 청산되면 조기
        종료(할 일 없음)하되, 결산(end)은 항상 실행해 종가자산을 기록한다.
        """
        open_px = self.feed.quotes()          # 개장 시세(첫 틱) — 피드 단일 소유
        for s in self.sleeves:
            s.orch.begin_trading_day(open_px)

        while not self._all_halted():
            if not self.clock.is_open(self._now()):
                break
            try:
                px = self.feed.quotes()
            except FeedExhausted:
                break                          # 리플레이 소진 = 폐장
            except StaleFeedError:
                self._sleep(self.poll_interval)
                continue                       # stale → 이 틱 건너뜀(모든 슬리브 공통)
            for s in self.sleeves:
                s.orch.process_tick(px)        # halted 슬리브는 내부에서 무동작
            self._sleep(self.poll_interval)

        summaries = {s.name: s.orch.end_trading_day() for s in self.sleeves}
        start_total = sum(v["day_start_equity"] for v in summaries.values())
        close_total = sum(v["close_equity"] for v in summaries.values())
        return {
            "date": summaries[self.sleeves[0].name]["date"],
            "sleeves": summaries,                                  # 슬리브별 상세(halt·기여도)
            "day_start_equity": round(start_total, 4),
            "close_equity": round(close_total, 4),
            "return": round(close_total / start_total - 1.0, 6) if start_total > 0 else 0.0,
            "halted_sleeves": [s.name for s in self.sleeves if s.halted],
            "all_halted": self._all_halted(),
        }
