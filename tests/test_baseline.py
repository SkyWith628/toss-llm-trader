"""증분4 보호 기준선 초기화 테스트 — 사용자 명시 확인·덮어쓰기 금지·live 무기준선 차단.

필수테스트 12(수동거래로 대사 불일치 시 기준선 자동변경 금지)와 §최초 초기화의 안전장치를
네트워크·실주문 0으로 검증. 실계좌 조회는 holdings_provider로 주입.

실행: python3 tests/test_baseline.py
"""
from __future__ import annotations

import builtins
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import forward_main
from src.forward import baseline
from src.forward.experiment_portfolio import ExperimentPortfolio, ExperimentPosition
from src.forward.reconcile import reconcile_or_halt
from src.forward.runner import ForwardConfig, run_once


def _cfg(tmp, mode="live"):
    return ForwardConfig(symbols=["AAPL", "MSFT"], mode=mode, source="synthetic",
                         strategy="momentum", state_dir=tmp)


# ---------- build_baseline: 실계좌 보유 → 보호 기준선, 실험분 0 ----------
def test_build_baseline_freezes_holdings_as_protected():
    exp = baseline.build_baseline(100.0, {"AAPL": 10.0, "NVDA": 1.5})
    assert exp.protected_positions == {"AAPL": 10.0, "NVDA": 1.5}
    assert exp.experiment_positions == {}          # 실험 최초 0주(필수테스트 1)
    assert exp.cash == 100.0 and exp.initial_capital == 100.0
    assert exp.max_sellable("AAPL") == 0.0         # 보호분은 자동매도 대상 아님


def test_build_baseline_drops_zero_holdings():
    exp = baseline.build_baseline(100.0, {"AAPL": 10.0, "TSLA": 0.0})
    assert exp.protected_positions == {"AAPL": 10.0}   # 0주는 기준선에서 제외


def test_empty_account_baseline_is_empty():
    exp = baseline.build_baseline(100.0, {})
    assert exp.protected_positions == {} and exp.cash == 100.0   # 전용/빈 계좌


# ---------- 필수테스트 12: 대사 실패해도 기준선을 자동 변경하지 않음 ----------
def test_reconcile_failure_does_not_change_baseline():
    exp = baseline.build_baseline(100.0, {"AAPL": 10.0})
    exp.experiment_positions["AAPL"] = ExperimentPosition("AAPL", 0.3, 9.0)
    before = dict(exp.protected_positions)
    # 사용자가 앱에서 직접 매도 → 실계좌 9.0 < 보호 10.0 + 실험 0.3 → 대사 실패.
    ok, _ = reconcile_or_halt(exp, {"AAPL": 9.0})
    assert not ok and not exp.can_trade                # reconciliation_required로 전환
    assert exp.protected_positions == before           # 기준선은 그대로(자동 재계산 금지)


# ---------- 초과 보유(미귀속)는 자동 편입하지 않고 '보고'만 ----------
def test_unattributed_reports_extra_without_absorbing():
    exp = baseline.build_baseline(100.0, {"AAPL": 10.0})
    exp.experiment_positions["AAPL"] = ExperimentPosition("AAPL", 0.3, 9.0)
    # 실계좌 11.0 = 보호 10.0 + 실험 0.3 + 미귀속 0.7(사용자 직접 추가매수).
    extra = baseline.unattributed(exp, {"AAPL": 11.0})
    assert abs(extra["AAPL"] - 0.7) < 1e-9
    assert exp.protected_positions == {"AAPL": 10.0}   # 기준선 불변
    assert exp.experiment_positions["AAPL"].quantity == 0.3   # 실험분 불변


# ---------- live는 기준선 없이 돌지 않음(fail-closed) ----------
def test_run_once_live_without_baseline_fails_closed():
    tmp = tempfile.mkdtemp(prefix="base_")   # state.json 없음
    try:
        run_once(_cfg(tmp), force=True)
        assert False, "기준선 없는 live는 예외여야 한다"
    except ValueError as e:
        assert "기준선" in str(e)


# ---------- init_baseline: 덮어쓰기 금지 ----------
def test_init_baseline_refuses_overwrite():
    tmp = tempfile.mkdtemp(prefix="base_")
    cfg = _cfg(tmp)
    ok1 = forward_main.init_baseline(cfg, confirm=True,
                                     holdings_provider=lambda: {"AAPL": 10.0})
    assert ok1 and os.path.exists(cfg.state_path)
    # 두 번째 호출은 기존 상태를 절대 덮어쓰지 않음.
    ok2 = forward_main.init_baseline(cfg, confirm=True,
                                     holdings_provider=lambda: {"AAPL": 999.0})
    assert ok2 is False
    from src.forward.state import ForwardState
    st = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=["AAPL", "MSFT"],
                                   source="synthetic", strategy="momentum", model="claude-opus-5")
    assert st.experiment.protected_positions == {"AAPL": 10.0}   # 원본 유지


# ---------- init_baseline: 확인 거부 시 아무것도 저장 안 함 ----------
def test_init_baseline_cancel_writes_nothing():
    tmp = tempfile.mkdtemp(prefix="base_")
    cfg = _cfg(tmp)
    orig_input = builtins.input
    builtins.input = lambda *_: "no"          # 사용자가 'no' 입력
    try:
        ok = forward_main.init_baseline(cfg, confirm=False,
                                        holdings_provider=lambda: {"AAPL": 10.0})
    finally:
        builtins.input = orig_input
    assert ok is False and not os.path.exists(cfg.state_path)


def test_init_baseline_confirm_writes_state():
    tmp = tempfile.mkdtemp(prefix="base_")
    cfg = _cfg(tmp)
    ok = forward_main.init_baseline(cfg, confirm=True,
                                    holdings_provider=lambda: {"AAPL": 10.0, "MSFT": 2.0})
    assert ok and os.path.exists(cfg.state_path)
    from src.forward.state import ForwardState
    st = ForwardState.load_or_init(cfg.state_path, budget=100.0, symbols=["AAPL", "MSFT"],
                                   source="synthetic", strategy="momentum", model="claude-opus-5")
    assert st.experiment.protected_positions == {"AAPL": 10.0, "MSFT": 2.0}
    assert st.experiment.cash == 100.0 and st.experiment.experiment_positions == {}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("모든 기준선 테스트 통과")
