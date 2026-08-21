"""트레일링 스톱 + 변동성 스케일링 사이징 단위 테스트.

실행: python -m pytest -q  (pytest 없으면 python tests/test_trailing_and_sizing.py)

검증 포인트:
- 트레일링 off(기본)면 기존 판정과 동일(하위호환)
- 고점 무장 후 δ 되돌림 → TRAILING_STOP · 무장 전이면 미발동
- 하드 손절/익절이 트레일링보다 우선
- realized_vol 정확도 · 역변동성 리웨이팅(총합 보존·저변동성 가중↑)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.risk.day_risk import DayRiskManager, RiskAction
from src.strategy.sizing import realized_vol, vol_scaled_weights

_BASE = 1000.0


# --- 트레일링 스톱 ---

def test_trailing_off_matches_legacy():
    m = DayRiskManager(take_profit=0.02, stop_loss=-0.01)  # trailing 기본 None
    m.anchor(_BASE)
    assert m.evaluate(1015.0) is RiskAction.NORMAL   # +1.5%
    assert m.evaluate(1008.0) is RiskAction.NORMAL   # 되돌렸어도 트레일링 꺼져 있으니 NORMAL


def test_trailing_fires_after_peak_drawdown():
    # δ=0.5% 되돌림, arm=+0.8%
    m = DayRiskManager(take_profit=0.02, stop_loss=-0.01, trailing=0.005, trailing_arm=0.008)
    m.anchor(_BASE)
    assert m.evaluate(1018.0) is RiskAction.NORMAL       # +1.8% 고점(무장됨)
    assert m.evaluate(1014.0) is RiskAction.NORMAL       # +1.4%: 고점-0.4% < δ → 아직
    assert m.evaluate(1012.0) is RiskAction.TRAILING_STOP  # +1.2%: 고점 1.8%-0.6% ≤ -δ → 청산


def test_trailing_not_armed_below_threshold():
    m = DayRiskManager(take_profit=0.02, stop_loss=-0.01, trailing=0.003, trailing_arm=0.01)
    m.anchor(_BASE)
    assert m.evaluate(1005.0) is RiskAction.NORMAL   # +0.5% 고점(arm 1% 미달 → 무장 안 됨)
    assert m.evaluate(1000.0) is RiskAction.NORMAL   # 0.5% 되돌려도 무장 전이라 미발동


def test_hard_stops_take_precedence_over_trailing():
    m = DayRiskManager(take_profit=0.02, stop_loss=-0.01, trailing=0.003, trailing_arm=0.005)
    m.anchor(_BASE)
    m.evaluate(1010.0)                                    # +1% 고점 무장
    assert m.evaluate(990.0) is RiskAction.STOP_LOSS      # -1%: 손절이 트레일링보다 우선
    m2 = DayRiskManager(take_profit=0.02, stop_loss=-0.01, trailing=0.003, trailing_arm=0.005)
    m2.anchor(_BASE)
    assert m2.evaluate(1020.0) is RiskAction.TAKE_PROFIT  # +2%: 익절 우선


def test_trailing_validation():
    for bad in (0.0, -0.01):
        try:
            DayRiskManager(trailing=bad)
            assert False, "trailing 음수/0 거부 실패"
        except ValueError:
            pass


# --- 변동성 스케일링 사이징 ---

def test_realized_vol_basic():
    assert realized_vol([100.0]) == 0.0            # 표본 부족
    assert realized_vol([100.0, 100.0, 100.0]) == 0.0  # 무변동 → 0
    v = realized_vol([100.0, 101.0, 100.0, 101.0])     # 진동 → 양수
    assert v > 0


def test_vol_scaled_preserves_total_and_favors_low_vol():
    base = {"A": 0.3, "B": 0.3}          # 동일 비중, 총 0.6
    vol = {"A": 0.01, "B": 0.03}         # A가 3배 조용함
    out = vol_scaled_weights(base, vol)
    assert abs(sum(out.values()) - 0.6) < 1e-9    # 총 투입비중 보존
    assert out["A"] > out["B"]                     # 저변동성에 더 큰 비중


def test_vol_scaled_safe_when_no_vol():
    base = {"A": 0.5, "B": 0.5}
    assert vol_scaled_weights(base, {}) == base    # vol 미상 → floor로 균등(=base 유지)
    assert vol_scaled_weights({"A": 0.0}, {"A": 0.02}) == {"A": 0.0}  # base 0 → 무동작


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 트레일링·사이징 테스트 통과")
