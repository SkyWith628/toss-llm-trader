"""메타-포트폴리오(전략 배분 레이어) 단위 테스트 — 순수 로직, 네트워크 0.

실행: python -m pytest -q  (pytest 없으면 python tests/test_meta_portfolio.py)

검증 포인트:
- FixedAllocator: 지정 비중 그대로 · 합>1 정규화 · 미지정 슬리브 0
- InverseVolAllocator: 고변동 슬리브에 적게 · 히스토리 부족 시 동일가중 폴백
- MetaPortfolioStrategy: 슬리브 신호를 a_i로 스케일·합산 · Σ≤1 · 미언급 종목 현금화(매도) ·
  이름 중복/빈 슬리브 방어 · 기여도(last_decision) 노출
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Action, MarketSnapshot, Signal
from src.portfolio import (
    FixedAllocator, InverseVolAllocator, MetaPortfolioStrategy, Sleeve,
)
from src.strategy.pair_trend import PairTrendStrategy
from src.strategy.rule_based import BuyAndHoldStrategy

_TS = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)


# --- 테스트용 고정 신호 전략(덕 타이핑) ---
class _FixedSignals:
    """항상 같은 {sym: weight}를 뱉는 스텁 전략."""
    def __init__(self, weights: dict[str, float]):
        self._w = weights

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        return [Signal(s, Action.BUY if w > 0 else Action.SELL, w, timestamp=_TS)
                for s, w in self._w.items()]


def _snap(prices: dict[str, float], history: dict | None = None) -> MarketSnapshot:
    return MarketSnapshot(timestamp=_TS, prices=prices, history=history or {})


# --- FixedAllocator ---

def test_fixed_allocator_passthrough_and_normalize():
    sleeves = [Sleeve("core", _FixedSignals({}), ["SPY"]),
               Sleeve("pair", _FixedSignals({}), ["SOXL"])]
    # 합 ≤ 1 → 그대로(나머지 현금)
    a = FixedAllocator({"core": 0.5, "pair": 0.2}).weights(sleeves)
    assert a == {"core": 0.5, "pair": 0.2}
    # 합 > 1 → 비례 정규화
    a2 = FixedAllocator({"core": 0.8, "pair": 0.4}).weights(sleeves)
    assert abs(sum(a2.values()) - 1.0) < 1e-9
    assert abs(a2["core"] - 0.8 / 1.2) < 1e-9
    # 미지정 슬리브 → 0
    a3 = FixedAllocator({"core": 0.5}).weights(sleeves)
    assert a3["pair"] == 0.0


# --- InverseVolAllocator ---

def test_inverse_vol_gives_less_to_high_vol():
    # calm: ±0.5% 진동, wild: ±5% 진동 → wild이 σ 큼 → a 작아야
    calm = [100, 100.5, 100, 100.5, 100, 100.5, 100, 100.5, 100, 100.5, 100]
    wild = [100, 105, 100, 105, 100, 105, 100, 105, 100, 105, 100]
    snap = _snap({"CALM": 100.5, "WILD": 105},
                 history={"CALM": calm, "WILD": wild})
    sleeves = [Sleeve("calm", _FixedSignals({}), ["CALM"]),
               Sleeve("wild", _FixedSignals({}), ["WILD"])]
    a = InverseVolAllocator(lookback=10).weights(sleeves, snap)
    assert abs(sum(a.values()) - 1.0) < 1e-9
    assert a["calm"] > a["wild"]              # 저변동에 더 배분


def test_inverse_vol_falls_back_to_equal_when_no_history():
    sleeves = [Sleeve("a", _FixedSignals({}), ["X"]),
               Sleeve("b", _FixedSignals({}), ["Y"])]
    a = InverseVolAllocator().weights(sleeves, _snap({"X": 1, "Y": 1}))  # history 없음
    assert a == {"a": 0.5, "b": 0.5}


# --- MetaPortfolioStrategy ---

def test_meta_scales_and_merges():
    # core: SPY 1.0, sat: SOXL 1.0. 배분 core 0.6 / sat 0.4 → SPY 0.6, SOXL 0.4
    sleeves = [Sleeve("core", _FixedSignals({"SPY": 1.0}), ["SPY"]),
               Sleeve("sat", _FixedSignals({"SOXL": 1.0}), ["SOXL"])]
    meta = MetaPortfolioStrategy(sleeves, FixedAllocator({"core": 0.6, "sat": 0.4}))
    out = {s.symbol: s.target_weight for s in meta.generate_signal(_snap({"SPY": 10, "SOXL": 30}))}
    assert abs(out["SPY"] - 0.6) < 1e-9 and abs(out["SOXL"] - 0.4) < 1e-9
    assert sum(out.values()) <= 1.0 + 1e-9


def test_meta_merges_overlapping_symbols():
    # 두 슬리브가 같은 SPY를 잡으면 스케일 후 합산: 0.5·1.0 + 0.5·0.4 = 0.7
    sleeves = [Sleeve("a", _FixedSignals({"SPY": 1.0}), ["SPY"]),
               Sleeve("b", _FixedSignals({"SPY": 0.4}), ["SPY"])]
    meta = MetaPortfolioStrategy(sleeves, FixedAllocator({"a": 0.5, "b": 0.5}))
    out = {s.symbol: s.target_weight for s in meta.generate_signal(_snap({"SPY": 10}))}
    assert abs(out["SPY"] - 0.7) < 1e-9


def test_meta_liquidates_unmentioned_symbol():
    # 배분 밖 종목(QQQ)은 비중 0·SELL로 나와야 리밸런서가 매도한다
    sleeves = [Sleeve("core", _FixedSignals({"SPY": 1.0}), ["SPY"])]
    meta = MetaPortfolioStrategy(sleeves, FixedAllocator({"core": 1.0}))
    sigs = meta.generate_signal(_snap({"SPY": 10, "QQQ": 20}))
    by = {s.symbol: s for s in sigs}
    assert by["QQQ"].target_weight == 0.0 and by["QQQ"].action is Action.SELL
    assert by["SPY"].action is Action.BUY


def test_meta_attribution_recorded():
    sleeves = [Sleeve("core", _FixedSignals({"SPY": 1.0}), ["SPY"]),
               Sleeve("sat", _FixedSignals({"SOXL": 1.0}), ["SOXL"])]
    meta = MetaPortfolioStrategy(sleeves, FixedAllocator({"core": 0.7, "sat": 0.3}))
    meta.generate_signal(_snap({"SPY": 10, "SOXL": 30}))
    d = meta.last_decision
    assert d["allocation"] == {"core": 0.7, "sat": 0.3}
    assert abs(d["contribution"]["sat"]["SOXL"] - 0.3) < 1e-9
    assert abs(d["merged"]["SPY"] - 0.7) < 1e-9


def test_meta_validates_construction():
    bh = BuyAndHoldStrategy()
    for bad in ([], [Sleeve("x", bh, ["A"]), Sleeve("x", bh, ["B"])]):   # 빈·이름중복
        try:
            MetaPortfolioStrategy(bad, FixedAllocator({}))
            assert False
        except ValueError:
            pass


def test_meta_with_buy_and_hold_sleeve():
    # 실제 전략(B&H) 슬리브 하나 — 동일비중 × 배분
    sleeves = [Sleeve("core", BuyAndHoldStrategy(), ["SPY", "QQQ"])]
    meta = MetaPortfolioStrategy(sleeves, FixedAllocator({"core": 0.8}))
    out = {s.symbol: s.target_weight for s in meta.generate_signal(_snap({"SPY": 10, "QQQ": 20}))}
    assert abs(out["SPY"] - 0.4) < 1e-9 and abs(out["QQQ"] - 0.4) < 1e-9   # 0.8 × 0.5


def test_meta_isolates_sleeve_universe():
    # 코어(B&H over SPY,QQQ) + 위성(B&H over SOXL). 격리 없으면 코어가 SOXL까지 담음.
    sleeves = [Sleeve("core", BuyAndHoldStrategy(), ["SPY", "QQQ"]),
               Sleeve("sat", BuyAndHoldStrategy(), ["SOXL"])]
    meta = MetaPortfolioStrategy(sleeves, FixedAllocator({"core": 0.6, "sat": 0.4}))
    out = {s.symbol: s.target_weight
           for s in meta.generate_signal(_snap({"SPY": 10, "QQQ": 20, "SOXL": 30}))}
    assert abs(out["SPY"] - 0.3) < 1e-9 and abs(out["QQQ"] - 0.3) < 1e-9   # 0.6 × 0.5
    assert abs(out["SOXL"] - 0.4) < 1e-9                                    # 0.4 × 1.0 (코어 오염 없음)


# --- PairTrendStrategy ---

def _pair_snap(soxl: list[float], soxs: list[float]) -> MarketSnapshot:
    return _snap({"SOXL": soxl[-1], "SOXS": soxs[-1]},
                 history={"SOXL": soxl, "SOXS": soxs})


def test_pair_trend_picks_stronger_leg_one_direction():
    # SOXL 상승 추세 / SOXS 하락 → SOXL 하나만 1.0
    up = [100, 102, 104, 106, 108]
    down = [100, 98, 96, 94, 92]
    out = {s.symbol: s.target_weight
           for s in PairTrendStrategy(["SOXL", "SOXS"], lookback=3).generate_signal(_pair_snap(up, down))}
    assert out["SOXL"] == 1.0 and out["SOXS"] == 0.0


def test_pair_trend_cash_when_both_negative():
    down1 = [100, 99, 98, 97, 96]
    down2 = [100, 98, 96, 94, 92]
    out = {s.symbol: s.target_weight
           for s in PairTrendStrategy(["SOXL", "SOXS"], lookback=3).generate_signal(_pair_snap(down1, down2))}
    assert out["SOXL"] == 0.0 and out["SOXS"] == 0.0        # 둘 다 음(-) → 현금


def test_pair_trend_cash_on_insufficient_history():
    out = {s.symbol: s.target_weight
           for s in PairTrendStrategy(["SOXL", "SOXS"], lookback=20).generate_signal(
               _pair_snap([100, 101], [100, 99]))}                # 히스토리 짧음
    assert out["SOXL"] == 0.0 and out["SOXS"] == 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 메타-포트폴리오 테스트 통과")
