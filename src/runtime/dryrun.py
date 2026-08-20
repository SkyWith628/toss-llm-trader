"""실데이터 dry-run — 실제 1분봉을 리플레이해 서킷브레이커를 페이퍼로 검증(실주문 0).

[What] yfinance에서 최근 정규장 1분봉을 받아 틱으로 리플레이 → Orchestrator를 PaperBroker로
       하루 실행 → 당일 시작자산 대비 서킷브레이커(±임계) 동작을 관찰.
[Why]  실시간 라이브를 장중까지 기다리지 않고, '실제 가격'으로 파이프라인 전체를 안전하게
       확인한다. 진입은 동일비중(90%), 실주문은 전송되지 않는다.
[How]  python -m src.runtime.dryrun --symbols AAPL,MSFT,NVDA [--stop-loss -0.003 --take-profit 0.005]
       임계를 좁히면 잔잔한 날에도 발동 경로를 눈으로 볼 수 있다(메커니즘 시연용).
"""
from __future__ import annotations

import argparse
import os
import tempfile

from src.config import DEFAULT_BUDGET_USD, DEFAULT_STOP_LOSS, DEFAULT_TAKE_PROFIT
from src.core.types import Action, Signal
from src.execution.paper_broker import PaperBroker
from src.market.feed import ReplayFeed
from src.risk.day_risk import DayRiskManager
from src.risk.guardrail import Guardrail
from src.runtime.orchestrator import Orchestrator
from src.runtime.state import IntradayState


class _AlwaysOpenClock:
    """리플레이 전용 — 항상 개장. 하루 종료는 리플레이 소진(FeedExhausted)이 담당."""
    def __init__(self, date: str):
        self._date = date

    def is_open(self, now):
        return True

    def local_date(self, now):
        return self._date

    def seconds_to_open(self, now):
        return 0.0


def _invest_90pct(prices: dict) -> list[Signal]:
    """개장 진입: 동일비중 90%(현금 10% 버퍼). LLM으로 교체 가능(open_decider)."""
    n = len(prices)
    return [Signal(s, Action.BUY, 0.9 / n) for s in prices]


def load_intraday_ticks(symbols: list[str], period: str, interval: str):
    """실제 1분봉을 (거래일, [ {sym: close}, ... ]) 로 반환. 모든 종목 존재하는 바만."""
    import yfinance as yf  # 선택 의존성

    data = yf.download(symbols, period=period, interval=interval,
                       auto_adjust=True, progress=False, threads=False)
    closes = data["Close"]
    ticks: list[dict] = []
    date = None
    for ts, row in closes.iterrows():
        prices = {}
        for s in symbols:
            val = row[s] if len(symbols) > 1 else row
            if val == val:  # NaN 아님
                prices[s] = float(round(val, 4))
        if len(prices) == len(symbols):
            ticks.append(prices)
            date = ts.to_pydatetime().date().isoformat()
    return date, ticks


def run_dryrun(symbols, budget, take_profit, stop_loss, period, interval) -> dict:
    date, ticks = load_intraday_ticks(symbols, period, interval)
    if not ticks:
        return {"status": "error", "reason": "1분봉 데이터 없음(장중/직전 세션 확인)"}

    state_path = os.path.join(tempfile.mkdtemp(prefix="dryrun_"), "state.json")
    orch = Orchestrator(
        symbols=symbols,
        clock=_AlwaysOpenClock(date),
        feed=ReplayFeed(ticks),
        broker=PaperBroker(starting_cash=budget),
        guardrail=Guardrail(budget=budget, valid_symbols=set(symbols)),
        risk=DayRiskManager(take_profit=take_profit, stop_loss=stop_loss),
        state=IntradayState.new(budget, symbols),
        open_decider=_invest_90pct,
        state_path=state_path,
        poll_interval=0.0,
    )
    result = orch.run_trading_day()
    result["status"] = "ok"
    result["total_ticks"] = len(ticks)
    result["thresholds"] = (take_profit, stop_loss)
    return result


def _print(r: dict, symbols: list[str]) -> None:
    if r.get("status") != "ok":
        print(f"[오류] {r.get('reason')}")
        return
    tp, sl = r["thresholds"]
    print("\n" + "=" * 56)
    print(f"  실데이터 dry-run(페이퍼·실주문 0) | {r['date']} | {','.join(symbols)}")
    print("=" * 56)
    print(f"  임계          : 익절 {tp:+.2%} / 손절 {sl:+.2%}")
    print(f"  1분봉 틱       : {r['total_ticks']}개(개장 제외 {r['ticks']}개 감시)")
    print(f"  당일 시작자산  : ${r['day_start_equity']:,.2f}")
    print(f"  장중 최고/최저 : {r['peak_return']:+.2%} / {r['trough_return']:+.2%}")
    if r["halted"]:
        label = "익절(TAKE_PROFIT)" if r["halt_reason"] == "take_profit" else "손절(STOP_LOSS)"
        print(f"  서킷브레이커   : ⚡ 발동 → {label} → 전량 청산")
    else:
        print(f"  서킷브레이커   : 미발동(밴드 내 유지) → 폐장까지 보유")
    print(f"  종가 자산      : ${r['close_equity']:,.2f}")
    print("=" * 56 + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description="실데이터 dry-run — 서킷브레이커 페이퍼 검증")
    p.add_argument("--symbols", default="AAPL,MSFT,NVDA")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    p.add_argument("--take-profit", type=float, default=DEFAULT_TAKE_PROFIT)
    p.add_argument("--stop-loss", type=float, default=DEFAULT_STOP_LOSS)
    p.add_argument("--period", default="1d", help="yfinance 기간(예: 1d, 5d)")
    p.add_argument("--interval", default="1m", help="봉 간격(예: 1m, 2m, 5m)")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    r = run_dryrun(symbols, args.budget, args.take_profit, args.stop_loss,
                   args.period, args.interval)
    _print(r, symbols)


if __name__ == "__main__":
    main()
