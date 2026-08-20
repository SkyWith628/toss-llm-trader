"""RealtimeFeed 단위 테스트 — 신선도·불량틱 방어를 mock 소스로 검증(네트워크 없음).

실행: python -m pytest -q  (pytest 없으면 python tests/test_realtime_feed.py)

검증 포인트(실시간 시세 오염 방어):
- 정상 시세 반환 · 누락/0/음수/NaN 거부 · 조회 예외 → StaleFeedError
- 급변(불량틱) 거부 · 연속 실패 카운트 · 성공 시 리셋 · 양호가는 성공에만 갱신
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market.feed import RealtimeFeed, StaleFeedError

_SYMBOLS = ["AAPL", "MSFT"]


class _FakeSource:
    """get_prices()가 미리 정한 값을 주거나(리스트로 순차), 예외를 던지는 mock."""
    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def get_prices(self):
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


def _feed(script, **kw):
    return RealtimeFeed(_FakeSource(script), _SYMBOLS, **kw)


def test_valid_quote_returned():
    f = _feed([{"AAPL": 100.0, "MSFT": 200.0}])
    assert f.quotes() == {"AAPL": 100.0, "MSFT": 200.0}
    assert f.consecutive_failures == 0


def test_missing_symbol_raises():
    f = _feed([{"AAPL": 100.0}])  # MSFT 누락
    try:
        f.quotes(); assert False
    except StaleFeedError:
        pass
    assert f.consecutive_failures == 1


def test_zero_and_negative_and_nan_rejected():
    for bad in (0.0, -5.0, float("nan")):
        f = _feed([{"AAPL": bad, "MSFT": 200.0}])
        try:
            f.quotes(); assert False, f"{bad} 거부돼야 함"
        except StaleFeedError:
            pass


def test_source_exception_raises_stale():
    f = _feed([RuntimeError("API down")])
    try:
        f.quotes(); assert False
    except StaleFeedError:
        pass
    assert f.consecutive_failures == 1


def test_sudden_jump_rejected():
    """직전 100 → 200(+100%)은 max_jump=0.5 초과 → 불량틱."""
    f = _feed([{"AAPL": 100.0, "MSFT": 200.0},
               {"AAPL": 200.0, "MSFT": 200.0}], max_jump=0.5)
    assert f.quotes()["AAPL"] == 100.0            # 1틱 정상
    try:
        f.quotes(); assert False, "급변은 거부돼야 함"
    except StaleFeedError:
        pass
    assert f.last_good["AAPL"] == 100.0           # 양호가는 불량틱에 갱신 안 됨


def test_normal_move_within_jump_ok():
    """+10%는 정상 범위."""
    f = _feed([{"AAPL": 100.0, "MSFT": 200.0},
               {"AAPL": 110.0, "MSFT": 200.0}], max_jump=0.5)
    f.quotes()
    assert f.quotes()["AAPL"] == 110.0
    assert f.last_good["AAPL"] == 110.0


def test_consecutive_failures_and_reset():
    f = _feed([{"AAPL": 0.0, "MSFT": 200.0},          # 실패1
               RuntimeError("x"),                      # 실패2
               {"AAPL": 100.0, "MSFT": 200.0},         # 성공→리셋
               {"AAPL": 0.0, "MSFT": 200.0}])          # 실패1
    for _ in range(2):
        try: f.quotes()
        except StaleFeedError: pass
    assert f.consecutive_failures == 2
    f.quotes()  # 성공
    assert f.consecutive_failures == 0
    try: f.quotes()
    except StaleFeedError: pass
    assert f.consecutive_failures == 1


def test_invalid_max_jump_rejected():
    for bad in (0.0, -0.1):
        try:
            RealtimeFeed(_FakeSource([{}]), _SYMBOLS, max_jump=bad); assert False
        except ValueError:
            pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 RealtimeFeed 테스트 통과")
