"""Forward 페이퍼 테스트 러너 — 하루 1회 실행 오케스트레이션.

[What] 상태 로드 → 실데이터(as-of today) → (오늘 이미 처리면 멱등 종료) → 스냅샷 →
       전략(메모리 복원) → 안전판 → 모의체결 → 상태/원장 저장 → Buy&Hold 대비 요약.
[Why]  백테스트와 동일한 계약(Strategy·Guardrail·PaperBroker·리밸런서)을 재사용하되,
       프로세스 재시작 사이의 연속성은 ForwardState가 책임진다.
[How]  OS 스케줄러가 매 거래일 장 마감 후 `python -m src.forward.runner`를 1회 호출.
       run_once()는 store/client 주입을 받아 네트워크·API 없이 테스트 가능.
"""
from __future__ import annotations

import argparse
import os
from typing import Any

from src.config import DEFAULT_BUDGET_USD
from src.data.storage import PriceStore
from src.data.synthetic import make_yfinance_store
from src.execution.paper_broker import PaperBroker
from src.execution.rebalance import signals_to_orders
from src.experiment.metrics import compute_metrics
from src.forward.append_ledger import AppendLedger
from src.forward.state import ForwardState
from src.risk.guardrail import Guardrail
from src.strategy.rule_based import MomentumStrategy

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
STATE_PATH = os.path.join(_ROOT, "logs", "forward_state.json")
LEDGER_PATH = os.path.join(_ROOT, "logs", "forward_ledger.jsonl")
LLM_LOG_PATH = os.path.join(_ROOT, "logs", "forward_llm_decisions.jsonl")
ENV_PATH = os.path.join(_ROOT, ".env")

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA"]
SLIPPAGE = 0.0005  # Buy&Hold 진입가와 브로커 체결의 슬리피지를 동일하게(공정 비교)


def _build_strategy(strategy_name: str, *, model: str, budget: float,
                    lookback: int, log_path: str | None, client: Any | None):
    """전략 조립 — 백테스트와 같은 인터페이스라 그대로 갈아끼운다."""
    if strategy_name == "llm":
        from src.strategy.llm_agent import LLMAgentStrategy  # 선택 의존성(anthropic)
        return LLMAgentStrategy(model=model, budget=budget,
                                log_path=log_path, client=client)
    return MomentumStrategy(lookback=lookback, top_k=2)


def run_once(
    *,
    symbols: list[str],
    budget: float = DEFAULT_BUDGET_USD,
    strategy_name: str = "llm",
    model: str = "claude-opus-5",
    lookback: int = 20,
    state_path: str = STATE_PATH,
    ledger_path: str = LEDGER_PATH,
    llm_log_path: str = LLM_LOG_PATH,
    store: PriceStore | None = None,
    client: Any | None = None,
) -> dict:
    """하루치 실행. store/client를 주입하면 네트워크·API 없이 테스트 가능.

    반환: {"status": "traded"|"skipped"|"error", "date", "day_index", ...}
    """
    # 1) 상태 로드(없으면 첫 실행)
    state = ForwardState.load(state_path) or ForwardState.new(budget, symbols)

    # 2) 실데이터 — 최신 거래일이 '오늘'(yfinance는 거래일만 반환 → 주말/휴장은 자연히 최신=직전 영업일)
    store = store if store is not None else make_yfinance_store(symbols)
    if not store.dates:
        return {"status": "error", "reason": "가격 데이터 없음"}
    today = store.dates[-1]
    today_iso = today.date().isoformat()

    # 3) 멱등성 — 오늘분을 이미 처리했으면 아무것도 하지 않고 종료
    if today_iso in state.processed_dates:
        return {"status": "skipped", "date": today_iso,
                "day_index": len(state.processed_dates),
                "reason": "오늘 거래일은 이미 처리됨"}

    prices_today = store.prices_on(today)

    # 4) 런타임 컴포넌트 복원(어제까지의 포트폴리오·수수료·메모리를 이어받음)
    broker = PaperBroker(slippage=SLIPPAGE, portfolio=state.to_portfolio())
    broker.total_commission_paid = state.commission
    broker.set_prices(prices_today)
    guardrail = Guardrail(budget=state.budget, valid_symbols=set(symbols))
    strategy = _build_strategy(strategy_name, model=model, budget=state.budget,
                               lookback=lookback, log_path=llm_log_path, client=client)
    if hasattr(strategy, "memory"):  # LLM 전략만 메모리 보유(회고 연속성)
        from src.strategy.llm_agent.memory import DecisionMemory
        strategy.memory = DecisionMemory.from_list(state.memory)

    # 5) 첫날이면 Buy&Hold 기준점을 이 시점 가격으로 고정
    if not state.bh_shares:
        state.start_date = today_iso
        state.set_bh_anchor(prices_today, SLIPPAGE)

    # 6) 결정 → 주문 → 안전판 → 체결
    snapshot = store.snapshot(today, lookback=lookback + 5)
    signals = strategy.generate_signal(snapshot)
    orders = signals_to_orders(signals, broker.get_portfolio(), prices_today)

    fills = []
    for order in orders:
        decision = guardrail.check(order, broker.get_portfolio(), prices_today)
        if not decision.ok:
            fills.append({"symbol": order.symbol, "side": order.side.value,
                          "status": "blocked", "reason": decision.reason})
            continue
        result = broker.place_order(decision.order, when=today)
        fills.append({"symbol": order.symbol, "side": order.side.value,
                      "qty": round(order.quantity, 6), "status": result.status,
                      "fill": result.fill_price, "commission": result.commission,
                      "reason": order.reason})

    # 7) 상태 갱신 + 자본곡선 누적
    pf = broker.get_portfolio()
    total = pf.total_value(prices_today)
    bh_total = state.bh_value(prices_today)

    state.set_from_portfolio(pf)
    state.commission = broker.total_commission_paid
    if hasattr(strategy, "memory"):
        state.memory = strategy.memory.to_list()
    state.processed_dates.append(today_iso)
    state.strat_equity.append(round(total, 4))
    state.bh_equity.append(round(bh_total, 4))
    state.save(state_path)

    # 8) 원장에 하루 append(누적)
    AppendLedger(ledger_path).append({
        "date": today_iso,
        "day_index": len(state.processed_dates),
        "total_value": round(total, 4),
        "buy_and_hold_value": round(bh_total, 4),
        "cash": round(pf.cash, 4),
        "positions": {s: round(p.quantity, 6) for s, p in pf.positions.items()},
        "signals": [{"symbol": s.symbol, "action": s.action.value,
                     "weight": round(s.target_weight, 4), "why": s.rationale}
                    for s in signals],
        "fills": fills,
    })

    return {
        "status": "traded",
        "date": today_iso,
        "day_index": len(state.processed_dates),
        "total_value": round(total, 4),
        "buy_and_hold_value": round(bh_total, 4),
        "commission_paid": round(state.commission, 4),
        "strategy": compute_metrics(state.strat_equity),
        "buy_and_hold": compute_metrics(state.bh_equity),
    }


def _load_env_file(path: str = ENV_PATH) -> None:
    """.env를 stdlib만으로 로드(ANTHROPIC_API_KEY 등). 이미 설정된 값은 덮지 않음."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.split("#", 1)[0].strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def _print_summary(r: dict) -> None:
    if r["status"] == "skipped":
        print(f"[{r['date']}] 이미 처리된 거래일 — 스킵(day {r['day_index']}).")
        return
    if r["status"] == "error":
        print(f"[오류] {r.get('reason')}")
        return
    s, b = r["strategy"], r["buy_and_hold"]
    print("\n" + "=" * 52)
    print(f"  Forward 페이퍼 | {r['date']} | day {r['day_index']}")
    print("=" * 52)
    print(f"  전략 자산     : ${r['total_value']:,.2f}")
    print(f"  Buy&Hold 자산 : ${r['buy_and_hold_value']:,.2f}")
    print(f"  누적 수수료   : ${r['commission_paid']:,.4f}")
    print(f"  전략 누적수익 : {s.get('total_return', 0):+.2%}   "
          f"(B&H {b.get('total_return', 0):+.2%})")
    lead = r["total_value"] - r["buy_and_hold_value"]
    print(f"  판정          : {'✅ B&H 앞섬' if lead >= 0 else '❌ B&H 뒤짐'} "
          f"({lead:+,.2f})")
    print(f"  원장          : {os.path.relpath(LEDGER_PATH, _ROOT)}")
    print("=" * 52 + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description="Forward 페이퍼 테스트 — 하루 1회 실행")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                   help="쉼표구분 실티커(예: AAPL,MSFT,NVDA)")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    p.add_argument("--strategy", choices=["momentum", "llm"], default="llm")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--reset", action="store_true",
                   help="기존 상태·원장을 지우고 새 실험 시작")
    args = p.parse_args()

    if args.reset:
        for path in (STATE_PATH, LEDGER_PATH):
            if os.path.exists(path):
                os.remove(path)
        print("기존 상태·원장 초기화 완료. 새 실험을 시작합니다.")

    _load_env_file()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    result = run_once(symbols=symbols, budget=args.budget,
                      strategy_name=args.strategy, model=args.model,
                      lookback=args.lookback)
    _print_summary(result)


if __name__ == "__main__":
    main()
