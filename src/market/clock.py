"""MarketClock — 미국 증시의 '지금 열렸나 / 언제 열리나 / 언제 닫히나'의 단일 출처.

[What] UTC 시각을 받아 미국 동부시장(ET) 기준 개장/폐장/휴장을 판정한다.
[Why]  실시간 오케스트레이터가 '개장 대기 → 감시 → 폐장 정리'를 돌리려면 시장 시각이 필요하다.
       서머타임(EDT↔EST)·주말·휴장·조기폐장을 한곳에서 처리해 상위 루프를 단순하게 유지.
[How]  표준 라이브러리 zoneinfo("America/New_York")로 DST를 자동 처리(외부 의존성 0).
       휴장·조기폐장은 연 단위 상수 집합 — 해가 바뀌면 갱신하거나 캘린더 라이브러리로 교체.

주의: '정규장(09:30–16:00 ET)'만 다룬다. 프리/애프터마켓은 범위 밖(실험은 정규장 대상).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

MARKET_TZ = "America/New_York"
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)  # 조기폐장일의 폐장 시각

# 미국 증시 전일(全日) 휴장 — 매년 갱신 필요. 추후 pandas-market-calendars 등으로 교체 가능.
US_MARKET_HOLIDAYS_2026 = frozenset({
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (7/4 토 → 7/3 관측)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
})

# 조기폐장(13:00 ET)
US_MARKET_EARLY_CLOSES_2026 = frozenset({
    "2026-11-27",  # 추수감사절 다음 날
    "2026-12-24",  # 크리스마스 이브
})

_UTC = ZoneInfo("UTC")


class MarketClock:
    def __init__(
        self,
        tz: str = MARKET_TZ,
        regular_open: time = REGULAR_OPEN,
        regular_close: time = REGULAR_CLOSE,
        early_close: time = EARLY_CLOSE,
        holidays: frozenset[str] = US_MARKET_HOLIDAYS_2026,
        early_closes: frozenset[str] = US_MARKET_EARLY_CLOSES_2026,
    ) -> None:
        self._tz = ZoneInfo(tz)
        self._open = regular_open
        self._regular_close = regular_close
        self._early_close = early_close
        self._holidays = frozenset(holidays)
        self._early_closes = frozenset(early_closes)

    # --- 내부 헬퍼 ---
    def _to_local(self, now_utc: datetime) -> datetime:
        """aware UTC(또는 naive=UTC로 간주)를 ET로 변환."""
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=_UTC)
        return now_utc.astimezone(self._tz)

    def _is_trading_day(self, local_dt: datetime) -> bool:
        return (local_dt.weekday() < 5
                and local_dt.date().isoformat() not in self._holidays)

    def _session(self, local_dt: datetime) -> tuple[datetime, datetime] | None:
        """해당 로컬 날짜의 (개장, 폐장) aware datetime. 거래일 아니면 None."""
        if not self._is_trading_day(local_dt):
            return None
        day = local_dt.date()
        close = (self._early_close
                 if day.isoformat() in self._early_closes else self._regular_close)
        open_dt = datetime.combine(day, self._open, tzinfo=self._tz)
        close_dt = datetime.combine(day, close, tzinfo=self._tz)
        return open_dt, close_dt

    # --- 공개 API ---
    def is_open(self, now_utc: datetime) -> bool:
        local = self._to_local(now_utc)
        session = self._session(local)
        return session is not None and session[0] <= local < session[1]

    def seconds_to_open(self, now_utc: datetime) -> float:
        """개장까지 남은 초. 이미 열려 있으면 0.0."""
        local = self._to_local(now_utc)
        if self.is_open(now_utc):
            return 0.0
        session = self._session(local)
        if session is not None and local < session[0]:  # 오늘 개장 전
            return (session[0] - local).total_seconds()
        # 다음 거래일 개장 탐색(최대 15일 — 연휴 대비)
        probe = local.replace(hour=0, minute=0, second=0, microsecond=0)
        for _ in range(15):
            probe += timedelta(days=1)
            nxt = self._session(probe)
            if nxt is not None:
                return (nxt[0] - local).total_seconds()
        raise RuntimeError("15일 내 개장일을 찾지 못함 — 휴장일 설정 오류?")

    def seconds_to_close(self, now_utc: datetime) -> float:
        """폐장까지 남은 초. 닫혀 있으면 0.0."""
        if not self.is_open(now_utc):
            return 0.0
        local = self._to_local(now_utc)
        session = self._session(local)
        assert session is not None  # is_open이 True면 세션 존재
        return (session[1] - local).total_seconds()
