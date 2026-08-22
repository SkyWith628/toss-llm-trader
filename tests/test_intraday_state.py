"""IntradayState 단위 테스트 — 지속·크래시 복원·하루 경계 로직 검증.

실행: python -m pytest -q  (pytest 없으면 python tests/test_intraday_state.py)

검증 포인트:
- 저장→로드 왕복(포트폴리오·앵커·halted 보존) · 없는 파일 → None
- 하루 경계: 새 날엔 앵커/리셋, 같은 날 재시작이면 앵커·halted 유지(재앵커 금지)
- mark_halt · 일별 종가자산 멱등 기록
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Portfolio, Position
from src.runtime.state import IntradayState

_SYMBOLS = ["AAPL", "MSFT"]


def _path():
    return os.path.join(tempfile.mkdtemp(prefix="intraday_"), "state.json")


def test_new_defaults():
    st = IntradayState.new(1000.0, _SYMBOLS)
    assert st.cash == 1000.0 and st.halted is False and st.trading_date == ""


def test_load_missing_returns_none():
    assert IntradayState.load(_path()) is None


def test_round_trip_preserves_state():
    p = _path()
    st = IntradayState.new(1000.0, _SYMBOLS)
    st.set_from_portfolio(Portfolio(cash=300.0,
                                    positions={"AAPL": Position("AAPL", 5.0, 140.0)}))
    st.start_new_day("2026-07-06", 1000.0)
    st.mark_halt("stop_loss")
    st.touch("2026-07-06T18:00:00+00:00")
    st.save(p)

    loaded = IntradayState.load(p)
    assert loaded is not None
    assert loaded.day_start_equity == 1000.0
    assert loaded.halted is True and loaded.halt_reason == "stop_loss"
    assert loaded.last_tick_ts == "2026-07-06T18:00:00+00:00"
    rp = loaded.to_portfolio()
    assert rp.cash == 300.0 and rp.positions["AAPL"].quantity == 5.0


def test_is_new_day_and_start_new_day():
    st = IntradayState.new(1000.0, _SYMBOLS)
    assert st.is_new_day("2026-07-06") is True        # 아직 아무 날도 시작 안 함
    st.start_new_day("2026-07-06", 1000.0)
    assert st.is_new_day("2026-07-06") is False        # 같은 날 → 재앵커 금지 신호
    assert st.is_new_day("2026-07-07") is True         # 다음 날 → 새 앵커 필요
    assert st.start_date == "2026-07-06"               # 첫 거래일 고정


def test_restart_same_day_keeps_anchor_and_halt():
    """같은 날 재시작 시나리오: 앵커·halted가 그대로 복원되고 재앵커하지 않는다."""
    p = _path()
    st = IntradayState.new(1000.0, _SYMBOLS)
    st.start_new_day("2026-07-06", 1000.0)
    st.mark_halt("take_profit")
    st.save(p)

    restored = IntradayState.load(p)
    assert restored.is_new_day("2026-07-06") is False  # 재앵커 안 함
    assert restored.day_start_equity == 1000.0
    assert restored.halted is True and restored.halt_reason == "take_profit"


def test_start_new_day_resets_halt():
    st = IntradayState.new(1000.0, _SYMBOLS)
    st.start_new_day("2026-07-06", 1000.0)
    st.mark_halt("stop_loss")
    st.start_new_day("2026-07-07", 1010.0)             # 새 날
    assert st.halted is False and st.halt_reason == ""
    assert st.day_start_equity == 1010.0


def test_record_daily_close_idempotent():
    st = IntradayState.new(1000.0, _SYMBOLS)
    st.record_daily_close("2026-07-06", 1005.1234)
    st.record_daily_close("2026-07-06", 998.0)          # 같은 날 재기록 → 덮어씀
    st.record_daily_close("2026-07-07", 1010.0)
    assert st.equity_curve() == [998.0, 1010.0]
    assert len(st.daily_equity) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 IntradayState 테스트 통과")
