"""RealtimeFeed — 실시간 시세의 '신선도·건전성' 방어 레이어.

[What] 원천 시세 소스(TossBroker.get_prices 등)를 폴링하되, 검증을 통과한 '양호한' 시세만
       반환하고 문제가 있으면 StaleFeedError를 던진다.
[Why]  서킷브레이커는 시세로 청산을 판단한다. 누락·0·불량틱(급변)·조회실패 같은 오염된
       시세로 판단하면 멀쩡한 포지션을 잘못 청산할 수 있다. 그래서 '의심스러우면 반환하지 않음'
       — 오케스트레이터는 예외를 받으면 그 틱의 판단을 건너뛴다(보수적 안전).
[How]  원천 소스를 주입(DI)해 Toss 세부와 분리 → mock으로 오프라인 테스트. 연속 실패 횟수를
       노출해, '몇 회 이상 실패하면 신규 중단' 같은 상위 정책은 오케스트레이터가 결정한다(SRP).
"""
from __future__ import annotations

from typing import Protocol


class PriceSource(Protocol):
    """get_prices() -> {symbol: price} 를 제공하는 무엇이든(TossBroker·mock)."""
    def get_prices(self) -> dict[str, float]: ...


class StaleFeedError(RuntimeError):
    """양호한(신선·유효) 시세를 얻지 못함 — 이 틱의 판단을 건너뛰라는 신호."""


class FeedExhausted(RuntimeError):
    """리플레이 시세가 소진됨 — 폐장과 동치(오케스트레이터가 그날 루프를 끝낸다)."""


class ReplayFeed:
    """미리 로드한 틱 시퀀스를 순차 반환(백테스트·드라이런). 소진 시 FeedExhausted.

    실시간 방어(RealtimeFeed)와 달리 데이터가 이미 정제됐다고 보고 검증은 생략한다.
    quotes() 인터페이스가 같아 오케스트레이터에 그대로 주입된다.
    """
    def __init__(self, ticks: list[dict[str, float]]) -> None:
        self._ticks = [dict(t) for t in ticks]
        self._i = 0

    @property
    def remaining(self) -> int:
        return len(self._ticks) - self._i

    def quotes(self) -> dict[str, float]:
        if self._i >= len(self._ticks):
            raise FeedExhausted("리플레이 틱 소진")
        tick = self._ticks[self._i]
        self._i += 1
        return dict(tick)


class RealtimeFeed:
    def __init__(
        self,
        price_source: PriceSource,
        symbols: list[str],
        *,
        max_jump: float = 0.5,   # 직전 양호가 대비 이 비율 초과 급변은 불량틱으로 간주
    ) -> None:
        if max_jump <= 0:
            raise ValueError(f"max_jump는 양수여야 함: {max_jump}")
        self._source = price_source
        self.symbols = list(symbols)
        self.max_jump = max_jump
        self._last_good: dict[str, float] = {}
        self._failures = 0

    @property
    def consecutive_failures(self) -> int:
        """직전 성공 이후 연속 실패 횟수(상위 정책이 '신규 중단' 판단에 사용)."""
        return self._failures

    @property
    def last_good(self) -> dict[str, float]:
        return dict(self._last_good)

    def quotes(self) -> dict[str, float]:
        """양호한 시세를 반환하거나 StaleFeedError. 절대 오래된/불량 시세를 반환하지 않는다."""
        try:
            raw = self._source.get_prices()
        except Exception as exc:  # 네트워크·API 오류
            self._failures += 1
            raise StaleFeedError(
                f"시세 조회 실패(연속 {self._failures}회): {exc!r}") from exc

        prices = self._validate(raw)
        if prices is None:
            self._failures += 1
            raise StaleFeedError(f"불량 시세(연속 {self._failures}회): 누락·0·NaN·급변")

        self._failures = 0
        self._last_good = prices
        return dict(prices)

    def _validate(self, raw: dict[str, float]) -> dict[str, float] | None:
        """모든 종목이 존재하고 양수·유한하며 직전 대비 급변이 아니면 dict, 아니면 None."""
        out: dict[str, float] = {}
        for sym in self.symbols:
            p = raw.get(sym)
            # None·비수치·NaN(p!=p)·0·음수 거부
            if not isinstance(p, (int, float)) or p != p or p <= 0:
                return None
            prev = self._last_good.get(sym)
            if prev and abs(p / prev - 1.0) > self.max_jump:  # 급변 불량틱
                return None
            out[sym] = float(p)
        return out
