"""DayRiskManager 단위 테스트 — 서킷브레이커 판정의 결정론적 검증.

실행: python -m pytest -q  (pytest 없으면 python tests/test_day_risk.py)

검증 포인트:
- 앵커 대비 +2%/-1% 도달(경계 포함) 판정 · 그 사이는 NORMAL
- 커스텀 임계 · daily_return 정확도
- 방어: 앵커 전 evaluate 금지 · 잘못된 앵커/임계값 거부
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.day_risk import DayRiskManager, RiskAction

_BASE = 1000.0


def _mgr(tp=0.02, sl=-0.01) -> DayRiskManager:
    m = DayRiskManager(take_profit=tp, stop_loss=sl)
    m.anchor(_BASE)
    return m


def test_normal_within_band():
    m = _mgr()
    assert m.evaluate(1000.0) is RiskAction.NORMAL     # 0%
    assert m.evaluate(1010.0) is RiskAction.NORMAL     # +1%
    assert m.evaluate(995.0) is RiskAction.NORMAL      # -0.5%


def test_take_profit_at_and_above_threshold():
    m = _mgr()
    assert m.evaluate(1020.0) is RiskAction.TAKE_PROFIT  # 정확히 +2%(경계 포함)
    assert m.evaluate(1050.0) is RiskAction.TAKE_PROFIT  # +5%


def test_stop_loss_at_and_below_threshold():
    m = _mgr()
    assert m.evaluate(990.0) is RiskAction.STOP_LOSS   # 정확히 -1%(경계 포함)
    assert m.evaluate(950.0) is RiskAction.STOP_LOSS   # -5%


def test_just_inside_band_is_normal():
    m = _mgr()
    assert m.evaluate(1019.99) is RiskAction.NORMAL    # +1.999%
    assert m.evaluate(990.01) is RiskAction.NORMAL     # -0.999%


def test_daily_return_value():
    m = _mgr()
    assert abs(m.daily_return(1015.0) - 0.015) < 1e-12
    assert abs(m.daily_return(985.0) - (-0.015)) < 1e-12


def test_custom_thresholds():
    m = _mgr(tp=0.05, sl=-0.03)
    assert m.evaluate(1020.0) is RiskAction.NORMAL     # +2% < +5%
    assert m.evaluate(1050.0) is RiskAction.TAKE_PROFIT
    assert m.evaluate(970.0) is RiskAction.STOP_LOSS   # -3%


def test_evaluate_before_anchor_raises():
    m = DayRiskManager()
    assert m.anchored is False
    try:
        m.evaluate(1000.0)
        assert False, "앵커 전 evaluate는 에러여야 함"
    except RuntimeError:
        pass


def test_invalid_anchor_rejected():
    m = DayRiskManager()
    for bad in (0.0, -100.0):
        try:
            m.anchor(bad)
            assert False, "0/음수 앵커는 거부돼야 함"
        except ValueError:
            pass


def test_invalid_thresholds_rejected():
    for tp, sl in [(-0.01, -0.01), (0.0, -0.01), (0.02, 0.0), (0.02, 0.01)]:
        try:
            DayRiskManager(take_profit=tp, stop_loss=sl)
            assert False, f"잘못된 임계({tp},{sl})는 거부돼야 함"
        except ValueError:
            pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 DayRiskManager 테스트 통과")
