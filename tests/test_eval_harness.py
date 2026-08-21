"""평가 하네스 단위 테스트 — 네트워크 없이 합성 틱으로 로직 검증.

실행: python -m pytest -q  (pytest 없으면 python tests/test_eval_harness.py)

검증 포인트:
- _aggregate: 승률·기대값·손익비·복리·낙폭 계산 정확
- _run_day: B&H는 종가수익 그대로 · 서킷은 손절 발동 · 트레일은 되돌림 청산
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.experiment.eval_harness import _aggregate, _dip_entry, _open_equal, _run_day

_S = "SPY"


def test_aggregate_math():
    m = _aggregate([0.02, -0.01, 0.03, -0.02],
                   ["", "stop_loss", "", ""], [True, True, True, True])
    assert m["n"] == 4
    assert abs(m["win_rate"] - 0.5) < 1e-9              # 2/4 양수
    assert abs(m["expectancy"] - 0.005) < 1e-9          # (0.02-0.01+0.03-0.02)/4
    assert m["payoff"] > 0 and m["avg_loss"] < 0
    assert m["halt_stop"] == 1
    assert m["entry_rate"] == 1.0


def test_run_day_bh_is_close_return():
    # 개장 100 → 110 상승, B&H(무한 임계)면 서킷 미발동 → +투입비중 수익
    ticks = [{_S: 100.0}] + [{_S: 110.0}] * 3
    v = dict(open=_open_equal(_S), tp=10.0, sl=-10.0, trail=None)
    r = _run_day(_S, ticks, v)
    assert r["halt_reason"] == ""            # 미발동
    assert r["ret"] > 0.05                    # 90% 투입 × +10% ≈ +9%(슬리피지 반영)


def test_run_day_stop_loss_fires():
    ticks = [{_S: 100.0}, {_S: 98.0}, {_S: 98.0}]   # 개장 후 -2% 급락
    v = dict(open=_open_equal(_S), tp=0.02, sl=-0.01, trail=None)
    r = _run_day(_S, ticks, v)
    assert r["halt_reason"] == "stop_loss"
    assert r["ret"] < 0


def test_run_day_dip_timing_enters_late():
    # 딥 1% 트리거: 개장 100 → 100 → 98(딥) 진입 → 이후 보합
    ticks = [{_S: 100.0}, {_S: 100.0}, {_S: 98.0}, {_S: 98.0}]
    v = dict(entry=_dip_entry(_S, 0.01), tp=0.05, sl=-0.10, trail=None)
    r = _run_day(_S, ticks, v)
    assert r["entered"] is True               # 딥에서 진입함


def test_run_day_trailing_beats_stop():
    # 개장 100 → 101(고점) → 100.5 되돌림. 트레일 δ0.3%/arm0.2%면 손절(-1%) 전에 트레일 청산
    ticks = [{_S: 100.0}, {_S: 101.0}, {_S: 100.5}, {_S: 100.5}]
    v = dict(open=_open_equal(_S), tp=0.02, sl=-0.01, trail=0.003, arm=0.002)
    r = _run_day(_S, ticks, v)
    assert r["halt_reason"] == "trailing_stop"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 평가 하네스 테스트 통과")
