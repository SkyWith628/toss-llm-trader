"""드라이버 — 전체 파이프라인을 하루씩 굴려 전략 vs Buy&Hold를 비교한다.

실행:
    python -m src.main                         # 합성데이터로 모멘텀 전략 스모크
    python -m src.main --symbols AAPL,MSFT,NVDA --source yfinance
    python -m src.main --days 250 --seed 7

이 드라이버는 단계 0의 '증명'이다: 여기서 Strategy 자리에 다음 단계의
LLMAgentStrategy만 바꿔 끼우면 자율매매 실험이 된다.
"""
from __future__ import annotations

import argparse
import os

from src.config import DEFAULT_BUDGET_USD
from src.core.clock import BacktestClock
from src.data.synthetic import make_synthetic_store, make_yfinance_store
from src.execution.paper_broker import PaperBroker
from src.execution.rebalance import signals_to_orders
from src.experiment.benchmark import buy_and_hold_equity
from src.experiment.ledger import Ledger
from src.experiment.metrics import compute_metrics
from src.risk.guardrail import Guardrail
from src.strategy.rule_based import MomentumStrategy

LEDGER_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "run.jsonl")
LLM_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "llm_decisions.jsonl")
ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")


def load_env_file(path: str = ENV_PATH) -> None:
    """.env를 표준 라이브러리만으로 로드(python-dotenv 의존성 없이).

    [What] KEY=VALUE 줄을 읽어 os.environ에 주입 — ANTHROPIC_API_KEY 등.
    [Why]  단계 0의 '표준 라이브러리만' 기조 유지 + 키를 코드/깃에 두지 않게(.env는 .gitignore).
    [How]  주석(#)·빈 줄 무시, 따옴표 제거, '이미 설정된 환경변수는 덮어쓰지 않음'(export 우선).
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # 인라인 주석 제거(예: KEY=val   # 설명) 후 따옴표 정리
            value = value.split("#", 1)[0].strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LLM 자율매매 실험 — 단계 0 파이프라인")
    p.add_argument("--symbols", default="AAA,BBB,CCC,DDD",
                   help="쉼표구분. yfinance면 실티커(예: AAPL,MSFT,NVDA)")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--source", choices=["synthetic", "yfinance"], default="synthetic")
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--top-k", type=int, default=2)
    p.add_argument("--strategy", choices=["momentum", "llm"], default="momentum",
                   help="momentum(규칙 베이스라인) | llm(Claude 자율매매)")
    p.add_argument("--model", default="claude-opus-5",
                   help="--strategy llm 일 때 사용할 Claude 모델")
    return p.parse_args()


def build_strategy(args: argparse.Namespace):
    """전략 조립 — 여기만 갈아끼우면 동일 백테스트 환경에서 공정 비교가 된다."""
    if args.strategy == "llm":
        from src.strategy.llm_agent import LLMAgentStrategy  # 선택 의존성(anthropic)
        return LLMAgentStrategy(model=args.model, budget=args.budget,
                                log_path=os.path.abspath(LLM_LOG_PATH))
    return MomentumStrategy(lookback=args.lookback, top_k=args.top_k)


def run(args: argparse.Namespace) -> dict:
    load_env_file()  # LLM 전략용 API 키 등을 .env에서 읽어 환경변수로
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 1) 데이터
    if args.source == "yfinance":
        store = make_yfinance_store(symbols)
    else:
        store = make_synthetic_store(symbols, days=args.days, seed=args.seed)

    # 2) 구성요소 조립(계약 기반 — 여기 전략만 갈아끼우면 LLM 실험)
    clock = BacktestClock()
    broker = PaperBroker(starting_cash=args.budget)
    guardrail = Guardrail(budget=args.budget, valid_symbols=set(symbols))
    strategy = build_strategy(args)
    ledger = Ledger(os.path.abspath(LEDGER_PATH))

    equity_curve: list[float] = []

    # 결정·측정 창(window): 마지막 N일만 매매하고, 그 이전은 워밍업 history로만 쓴다.
    # (yfinance는 항상 1년치를 받으므로 --days로 실제 거래일 수=LLM 콜 수=비용을 통제.)
    dates = store.dates
    if args.days and args.days < len(dates):
        dates = dates[-args.days:]

    # 3) 하루씩 전진(점진적 관찰 — 미래 미포함)
    for date in dates:
        clock.set_time(date)
        prices = store.prices_on(date)
        broker.set_prices(prices)
        snapshot = store.snapshot(date, lookback=args.lookback + 5)

        signals = strategy.generate_signal(snapshot)
        orders = signals_to_orders(signals, broker.get_portfolio(), prices)

        fills = []
        for order in orders:
            decision = guardrail.check(order, broker.get_portfolio(), prices)
            if not decision.ok:
                fills.append({"symbol": order.symbol, "side": order.side.value,
                              "status": "blocked", "reason": decision.reason})
                continue
            result = broker.place_order(decision.order, when=date)
            fills.append({"symbol": order.symbol, "side": order.side.value,
                          "qty": round(order.quantity, 6),
                          "status": result.status, "fill": result.fill_price,
                          "commission": result.commission, "reason": order.reason})

        pf = broker.get_portfolio()
        total = pf.total_value(prices)
        equity_curve.append(total)

        ledger.record_day({
            "date": date.date().isoformat(),
            "total_value": round(total, 4),
            "cash": round(pf.cash, 4),
            "positions": {s: round(p.quantity, 6) for s, p in pf.positions.items()},
            "signals": [{"symbol": s.symbol, "action": s.action.value,
                         "weight": round(s.target_weight, 4), "why": s.rationale}
                        for s in signals],
            "fills": fills,
        })

    # 4) 대조군 + 지표 (전략과 '같은 창'에서 측정해야 공정 비교)
    bh_equity = buy_and_hold_equity(store, symbols, budget=args.budget, dates=dates)
    strat_metrics = compute_metrics(equity_curve)
    bh_metrics = compute_metrics(bh_equity)

    return {
        "symbols": symbols, "source": args.source, "budget": args.budget,
        "commission_paid": round(broker.total_commission_paid, 4),
        "strategy": {"name": strategy.name, **strat_metrics},
        "buy_and_hold": {"name": "buy_and_hold", **bh_metrics},
    }


def print_report(r: dict) -> None:
    s, b = r["strategy"], r["buy_and_hold"]
    print("\n" + "=" * 58)
    print(f"  실험 결과  |  종목 {r['symbols']}  |  예산 ${r['budget']:.0f}  ({r['source']})")
    print("=" * 58)
    print(f"{'지표':<14}{'전략('+s['name']+')':>22}{'Buy&Hold':>18}")
    print("-" * 58)
    for key, label in [("total_return", "누적수익률"), ("cagr", "CAGR"),
                       ("sharpe", "샤프"), ("sortino", "소르티노"),
                       ("mdd", "MDD"), ("calmar", "Calmar"),
                       ("final_value", "최종자산($)")]:
        sv, bv = s.get(key, 0), b.get(key, 0)
        fmt = (lambda x: f"{x:.2%}") if key in ("total_return", "cagr", "mdd") else \
              (lambda x: f"{x:,.2f}")
        print(f"{label:<14}{fmt(sv):>22}{fmt(bv):>18}")
    print("-" * 58)
    print(f"지불한 수수료/비용: ${r['commission_paid']:.4f}")
    verdict = "✅ 전략이 Buy&Hold를 이김" if s["total_return"] > b["total_return"] \
        else "❌ Buy&Hold에 못 미침(정상 — 대다수 전략이 그렇다)"
    print(f"판정: {verdict}")
    print("원장(일별 결정 로그): logs/run.jsonl")
    print("=" * 58 + "\n")


if __name__ == "__main__":
    args = parse_args()
    report = run(args)
    print_report(report)
