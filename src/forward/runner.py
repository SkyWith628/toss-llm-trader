"""Forward 러너 — 거래일 1일을 처리하고 상태를 다음 날로 넘긴다.

[What] state 로드 → 데이터/스냅샷 → 전략 → 리밸런서 → 가드레일 → 페이퍼 체결 →
       equity 기록 → state 저장. 한 번의 호출 = 한 거래일.
[Why]  백테스트 드라이버(src/main.py)와 '같은 부품'을 재사용하되, 루프 대신 '1일 실행 +
       영속화'로 재구성해 실시간 forward 측정을 가능케 한다.
[How]  같은 거래일 재실행은 skip(중복 매매·이중 카운트 방지). 전략·데이터는 주입 가능
       (테스트가 네트워크·비결정성 없이 여러 날을 몰아 검증).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

from src.data.storage import PriceStore
from src.execution.paper_broker import PaperBroker
from src.execution.rebalance import signals_to_orders
from src.experiment.metrics import compute_metrics
from src.risk.guardrail import Guardrail
from src.strategy.base import Strategy
from src.forward.state import ForwardState


@dataclass
class ForwardConfig:
    """forward 실행 설정 — CLI가 채운다."""
    symbols: list[str]
    budget: float = 100.0
    source: str = "yfinance"
    strategy: str = "llm"
    model: str = "claude-opus-5"
    lookback: int = 20
    min_trade: float = 1.0
    slippage: float = 0.0005
    days: int = 250              # synthetic/yfinance history 길이
    seed: int = 42
    state_dir: str = ""          # logs/forward (CLI가 절대경로로 채움)

    @property
    def state_path(self) -> str:
        return os.path.join(self.state_dir, "state.json")

    @property
    def equity_path(self) -> str:
        return os.path.join(self.state_dir, "equity.jsonl")

    @property
    def decisions_path(self) -> str:
        return os.path.join(self.state_dir, "decisions.jsonl")


def _build_store(cfg: ForwardConfig) -> PriceStore:
    """실행 시점 기준 데이터 소스 조립(마지막 거래일 = '오늘')."""
    from src.data.synthetic import make_synthetic_store, make_yfinance_store
    if cfg.source == "yfinance":
        return make_yfinance_store(cfg.symbols)
    return make_synthetic_store(cfg.symbols, days=cfg.days, seed=cfg.seed)


def _build_strategy(cfg: ForwardConfig, state: ForwardState) -> Strategy:
    """전략 조립 + (LLM이면) 영속 회고 메모리를 되살려 연속성 확보."""
    if cfg.strategy == "llm":
        from src.strategy.llm_agent import LLMAgentStrategy
        strat = LLMAgentStrategy(model=cfg.model, log_path=cfg.decisions_path)
        if state.memory:
            strat.memory.load(state.memory)  # 직전 결정 회고를 프로세스 밖에서 복원
        return strat
    from src.strategy.rule_based import MomentumStrategy
    return MomentumStrategy(lookback=cfg.lookback)


def run_once(
    cfg: ForwardConfig,
    *,
    store: PriceStore | None = None,
    today: datetime | None = None,
    strategy: Strategy | None = None,
    force: bool = False,
) -> dict:
    """거래일 1일을 실행하고 요약 dict를 반환.

    store/today/strategy는 테스트가 주입(네트워크·비결정성 제거). 미주입 시 실데이터로 동작.
    force=True면 같은 거래일 재실행 skip을 무시(수동 재시도용).
    """
    state = ForwardState.load_or_init(
        cfg.state_path, budget=cfg.budget, symbols=cfg.symbols,
        source=cfg.source, strategy=cfg.strategy, model=cfg.model)

    store = store or _build_store(cfg)
    date = today or store.dates[-1]
    date_iso = date.date().isoformat()

    # 같은 거래일 재실행 방지 — 하루 두 번 돌면 이중 매매·이중 카운트가 된다.
    if state.last_run_date == date_iso and not force:
        return {"skipped": True, "reason": f"{date_iso} 이미 실행됨(--force로 재실행)",
                "date": date_iso, "day": state.day_count}

    prices = store.prices_on(date)
    snapshot = store.snapshot(date, lookback=cfg.lookback + 5)

    # 페이퍼 계좌를 상태에서 복원(이 파일이 곧 계좌)
    broker = PaperBroker(starting_cash=state.cash, slippage=cfg.slippage,
                         min_notional=cfg.min_trade)
    broker.portfolio = state.to_portfolio()
    broker.set_prices(prices)

    guardrail = Guardrail(budget=cfg.budget, valid_symbols=set(cfg.symbols))
    strategy = strategy or _build_strategy(cfg, state)

    # --- 하루 결정·집행 ---
    signals = strategy.generate_signal(snapshot)
    orders = signals_to_orders(signals, broker.get_portfolio(), prices, cfg.min_trade)

    fills = []
    for order in orders:
        decision = guardrail.check(order, broker.get_portfolio(), prices)
        if not decision.ok:
            fills.append({"symbol": order.symbol, "side": order.side.value,
                          "status": "blocked", "reason": decision.reason})
            continue
        result = broker.place_order(decision.order, when=date)
        fills.append({"symbol": order.symbol, "side": order.side.value,
                      "qty": round(order.quantity, 6), "status": result.status,
                      "fill": result.fill_price, "commission": result.commission,
                      "reason": order.reason})

    # --- 측정: 전략 vs Buy&Hold(첫 실행에 앵커 고정) ---
    state.ensure_bh_anchor(prices)
    pf = broker.get_portfolio()
    strat_value = pf.total_value(prices)
    bh_value = state.bh_value(prices)

    # --- 상태 갱신·영속화 ---
    state.update_from_portfolio(pf)
    if hasattr(strategy, "memory"):
        state.memory = strategy.memory.to_list()  # 회고 연속성 저장
    if not state.started_at:
        state.started_at = date_iso
    state.last_run_date = date_iso
    state.day_count += 1
    state.save(cfg.state_path)

    row = {
        "date": date_iso, "day": state.day_count,
        "strategy_value": round(strat_value, 4), "bh_value": round(bh_value, 4),
        "cash": round(pf.cash, 4),
        "positions": {s: round(p.quantity, 6) for s, p in pf.positions.items()},
        "signals": [{"symbol": s.symbol, "action": s.action.value,
                     "weight": round(s.target_weight, 4), "why": s.rationale}
                    for s in signals],
        "fills": fills,
    }
    _append_jsonl(cfg.equity_path, row)

    return {
        "skipped": False, "date": date_iso, "day": state.day_count,
        "strategy_value": round(strat_value, 4), "bh_value": round(bh_value, 4),
        "strategy_return": round(strat_value / cfg.budget - 1.0, 4),
        "bh_return": round(bh_value / cfg.budget - 1.0, 4),
        "cash": round(pf.cash, 4), "commission": round(broker.total_commission_paid, 6),
        "fills": fills,
    }


def build_report(cfg: ForwardConfig) -> dict:
    """equity.jsonl 누적을 읽어 전략·B&H의 위험조정 지표를 계산(실험 총평)."""
    strat_eq, bh_eq, days = [], [], 0
    if os.path.exists(cfg.equity_path):
        with open(cfg.equity_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                strat_eq.append(r["strategy_value"])
                bh_eq.append(r["bh_value"])
                days += 1
    return {
        "symbols": cfg.symbols, "budget": cfg.budget, "days": days,
        "strategy": {"name": cfg.strategy, **compute_metrics(strat_eq)},
        "buy_and_hold": {"name": "buy_and_hold", **compute_metrics(bh_eq)},
    }


def _append_jsonl(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)
