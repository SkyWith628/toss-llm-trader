"""MarketClock 단위 테스트 — 고정 UTC 시각으로 결정론적 검증(실시간 'now' 의존 없음).

실행: python -m pytest -q  (pytest 없으면 python tests/test_market_clock.py)

검증 포인트(실시간계의 시간 함정):
- 정규장 개폐(09:30–16:00 ET)  · 개장 전/후 판정
- 서머타임: 여름(EDT, 09:30=13:30 UTC) vs 겨울(EST, 09:30=14:30 UTC)
- 주말·휴장(평일이라도 닫힘)·조기폐장(13:00 ET)
- seconds_to_open/close: 다음 개장(주말·연휴 건너뜀)·폐장까지 잔여
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market.clock import MarketClock

_UTC = ZoneInfo("UTC")


def _utc(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_UTC)


def test_open_during_regular_summer_hours():
    """2026-07-06(월) 10:00 EDT = 14:00 UTC → 개장."""
    c = MarketClock()
    assert c.is_open(_utc(2026, 7, 6, 14, 0)) is True


def test_closed_before_open_summer():
    """09:00 EDT = 13:00 UTC → 개장 전(09:30=13:30 UTC까지 30분)."""
    c = MarketClock()
    now = _utc(2026, 7, 6, 13, 0)
    assert c.is_open(now) is False
    assert c.seconds_to_open(now) == 30 * 60


def test_closed_after_close_summer():
    """17:00 EDT = 21:00 UTC → 폐장 후."""
    c = MarketClock()
    assert c.is_open(_utc(2026, 7, 6, 21, 0)) is False


def test_dst_winter_open_uses_est():
    """겨울엔 09:30 ET = 14:30 UTC. 15:00 UTC(=10:00 EST) → 개장."""
    c = MarketClock()
    assert c.is_open(_utc(2026, 12, 7, 15, 0)) is True
    # 14:00 UTC = 09:00 EST → 아직 개장 전(서머타임 오프셋이 여름과 다름을 확인)
    assert c.is_open(_utc(2026, 12, 7, 14, 0)) is False


def test_weekend_closed_and_next_open_is_monday():
    """토요일은 닫힘. 다음 개장은 월요일 09:30 EDT = 13:30 UTC."""
    c = MarketClock()
    sat = _utc(2026, 7, 11, 16, 0)  # 토 16:00 UTC
    assert c.is_open(sat) is False
    # 다음 월요일(07-13) 13:30 UTC까지의 초
    expected = (_utc(2026, 7, 13, 13, 30) - sat).total_seconds()
    assert c.seconds_to_open(sat) == expected


def test_holiday_closed_even_on_weekday():
    """2026-01-01(목)은 평일이지만 신정 휴장 → 닫힘."""
    c = MarketClock()
    assert c.is_open(_utc(2026, 1, 1, 15, 0)) is False


def test_early_close_day():
    """2026-11-27(금) 조기폐장 13:00 EST = 18:00 UTC."""
    c = MarketClock()
    assert c.is_open(_utc(2026, 11, 27, 17, 0)) is True   # 12:00 EST → 개장
    assert c.is_open(_utc(2026, 11, 27, 18, 30)) is False  # 13:30 EST → 조기폐장 후


def test_seconds_to_close_when_open():
    """10:00 EDT(14:00 UTC)면 폐장(16:00 EDT=20:00 UTC)까지 6시간."""
    c = MarketClock()
    assert c.seconds_to_close(_utc(2026, 7, 6, 14, 0)) == 6 * 3600


def test_seconds_to_open_zero_when_open():
    c = MarketClock()
    assert c.seconds_to_open(_utc(2026, 7, 6, 14, 0)) == 0.0


def test_seconds_to_close_zero_when_closed():
    c = MarketClock()
    assert c.seconds_to_close(_utc(2026, 7, 6, 21, 0)) == 0.0


def test_naive_datetime_treated_as_utc():
    """tz 없는 datetime은 UTC로 간주(방어적)."""
    c = MarketClock()
    assert c.is_open(datetime(2026, 7, 6, 14, 0)) is True


def test_local_date_is_et():
    """UTC를 ET 날짜로. 07-06 02:00 UTC = 07-05 22:00 EDT → 전날."""
    c = MarketClock()
    assert c.local_date(_utc(2026, 7, 6, 14, 0)) == "2026-07-06"
    assert c.local_date(_utc(2026, 7, 6, 2, 0)) == "2026-07-05"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 MarketClock 테스트 통과")
