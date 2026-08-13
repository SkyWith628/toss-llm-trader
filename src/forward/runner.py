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

from src.core.types import Action, Side, Signal
from src.data.storage import PriceStore
from src.execution.costs import CostModel
from src.execution.paper_broker import PaperBroker
from src.execution.rebalance import signals_to_orders
from src.experiment.metrics import compute_metrics
from src.risk.guardrail import Guardrail
from src.strategy.base import Strategy
from src.forward import order_book as ob
from src.forward.carveout_broker import CarveOutBroker
from src.forward.events import detect_events, summarize
from src.forward.experiment_portfolio import ExperimentLedgerError
from src.forward.reconcile import reconcile_or_halt
from src.forward.state import ForwardState


@dataclass
class ForwardConfig:
    """forward 실행 설정 — CLI가 채운다."""
    symbols: list[str]
    budget: float = 100.0
    source: str = "yfinance"
    strategy: str = "llm"
    mode: str = "paper"          # "paper"=모의체결(PaperBroker) | "live"=실계좌(CarveOutBroker)
    model: str = "claude-opus-5"
    lookback: int = 20
    min_trade: float = 1.0
    slippage: float = 0.0005
    commission_rate: float = 0.0    # 거래대금 비례 수수료(리밸런서·브로커 공유)
    fixed_fee: float = 0.0          # 건당 고정 수수료
    days: int = 250              # synthetic/yfinance history 길이
    seed: int = 42
    state_dir: str = ""          # logs/forward (CLI가 절대경로로 채움)

    def canonical(self) -> dict:
        """결과에 영향 주는 설정 — state에 고정해 재실행 시 대조(다른 실험 혼입 방지)."""
        return {
            "source": self.source, "strategy": self.strategy, "model": self.model,
            "lookback": self.lookback, "min_trade": self.min_trade,
            "slippage": self.slippage, "commission_rate": self.commission_rate,
            "fixed_fee": self.fixed_fee, "days": self.days, "seed": self.seed,
        }

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
    broker: object | None = None,
    force: bool = False,
) -> dict:
    """거래일 1일을 실행하고 요약 dict를 반환.

    store/today/strategy는 테스트가 주입(네트워크·비결정성 제거). 미주입 시 실데이터로 동작.
    force=True면 같은 거래일 재실행 skip을 무시(수동 재시도용).
    """
    # live는 보호 기준선이 이미 확정된 상태에서만 돈다. 상태 파일이 없으면 자동 생성하지 않고
    # 멈춘다 — 계좌 보유를 자동으로 기준선에 편입하는 건 금지(스펙 §최초 초기화, 사용자 명시 확인).
    if cfg.mode == "live" and not os.path.exists(cfg.state_path):
        raise ValueError(
            "live 모드인데 보호 기준선이 없습니다. 먼저 `--init-baseline`로 실계좌를 조회·확인해 "
            "기준선을 만드세요(자동 생성 금지).")

    state = ForwardState.load_or_init(
        cfg.state_path, budget=cfg.budget, symbols=cfg.symbols,
        source=cfg.source, strategy=cfg.strategy, model=cfg.model,
        config=cfg.canonical())

    store = store or _build_store(cfg)
    date = today or store.dates[-1]
    date_iso = date.date().isoformat()

    # 같은 거래일 재실행 방지 — 하루 두 번 돌면 이중 매매·이중 카운트가 된다.
    if state.last_run_date == date_iso and not force:
        return {"skipped": True, "reason": f"{date_iso} 이미 실행됨(--force로 재실행)",
                "date": date_iso, "day": state.day_count}

    prices = store.prices_on(date)
    snapshot = store.snapshot(date, lookback=cfg.lookback + 5)

    exp = state.experiment   # 실험 장부(단일 모델). 페이퍼는 protected_positions={}.
    # 리밸런서·브로커가 공유하는 비용 모델(슬리피지+비례+고정수수료).
    cost = CostModel(slippage=cfg.slippage, commission_rate=cfg.commission_rate,
                     fixed_fee=cfg.fixed_fee)
    guardrail = Guardrail(budget=cfg.budget, valid_symbols=set(cfg.symbols))
    strategy = strategy or _build_strategy(cfg, state)

    # --- 1) 직전 거래일의 결정(pending)을 '오늘' 체결 (시점누수 방지) ---
    # 당일 종가로 결정하고 당일 종가에 체결하는 건 실현 불가능한 가정이다. 그래서 어제 내린
    # 결정을 오늘 가격에 집행한다. 첫 실행엔 pending이 없어 매매가 없다(전량 현금).
    # 실험이 중단 상태(대사 필요/halted)면 어떤 신규 주문도 내지 않는다(fail-closed).
    pending = _signals_from_dicts(state.pending_signals, date)
    orders = (signals_to_orders(pending, exp.to_portfolio(), prices, cfg.min_trade, cost=cost)
              if exp.can_trade else [])

    # 집행: 같은 장부·가드레일·주문을 공유하고 '어디로 체결하나'만 다르다.
    # - paper: PaperBroker 모의체결. - live: CarveOutBroker로 실계좌(전후 대사·멱등·부분체결 복구).
    if cfg.mode == "live":
        if broker is None:
            broker = _build_live_broker(cfg, exp, cost)
        fills, commission_total = _execute_live(
            cfg, state, exp, orders, guardrail, prices, date, date_iso, cost, broker)
    else:
        fills, commission_total = _execute_paper(
            cfg, state, exp, orders, guardrail, prices, date, cost)

    # --- 2) 측정: 오늘 종가로 실험 NAV vs Buy&Hold(첫 실행에 앵커 고정) ---
    state.ensure_bh_anchor(prices)
    strat_value = exp.nav(prices)
    bh_value = state.bh_value(prices)

    # --- 3) 오늘 결정 생성 → '다음' 실행에 체결하도록 영속(오늘 정보만으로 결정) ---
    decided = strategy.generate_signal(snapshot)

    # --- 4) 상태 갱신·영속화 ---
    if hasattr(strategy, "memory"):
        state.memory = strategy.memory.to_list()  # 회고 연속성 저장
    state.pending_signals = _signals_to_dicts(decided)   # 다음 실행이 이걸 체결
    if not state.started_at:
        state.started_at = date_iso
    state.last_run_date = date_iso
    state.day_count += 1
    state.save(cfg.state_path)

    row = {
        "date": date_iso, "day": state.day_count,
        "strategy_value": round(strat_value, 4), "bh_value": round(bh_value, 4),
        "cash": round(exp.cash, 4), "realized_pnl": round(exp.realized_pnl, 4),
        "fees_paid": round(exp.fees_paid, 4), "status": exp.status,
        # 귀속 구분: 실험 보유 vs 보호 기준선(페이퍼는 protected={}; 실계좌 연결 시 채워짐).
        "experiment_positions": {s: round(ep.quantity, 6)
                                 for s, ep in exp.experiment_positions.items()},
        "protected_positions": exp.protected_positions,
        "executed_fills": fills,   # 직전 결정의 '오늘' 체결
        "decided_signals": [{"symbol": s.symbol, "action": s.action.value,
                             "weight": round(s.target_weight, 4), "why": s.rationale}
                            for s in decided],  # 오늘 결정(다음 실행에 체결)
    }
    _append_jsonl(cfg.equity_path, row)

    return {
        "skipped": False, "date": date_iso, "day": state.day_count,
        "strategy_value": round(strat_value, 4), "bh_value": round(bh_value, 4),
        "strategy_return": round(strat_value / cfg.budget - 1.0, 4),
        "bh_return": round(bh_value / cfg.budget - 1.0, 4),
        "cash": round(exp.cash, 4), "realized_pnl": round(exp.realized_pnl, 4),
        "commission": round(commission_total, 6), "fills": fills,
    }


def _apply_fill(exp, side: Side, symbol: str, qty: float,
                price: float, commission: float) -> None:
    """실체결(방향별)을 실험 장부에 반영 — 불변식 위반 시 ExperimentLedgerError(fail-closed)."""
    if side is Side.BUY:
        exp.apply_buy(symbol, qty, price, commission)
    else:
        exp.apply_sell(symbol, qty, price, commission)


def _execute_paper(cfg, state, exp, orders, guardrail, prices, date, cost):
    """모의체결: PaperBroker가 '오늘 가격'으로 체결을 시뮬 → 실험 장부를 실체결값으로 갱신.

    실계좌가 아니므로 대사·열린주문이 없다(가장 단순한 경로). 반환: (fills, 누적 수수료).
    """
    broker = PaperBroker(starting_cash=exp.cash, slippage=cfg.slippage,
                         commission_rate=cfg.commission_rate,
                         commission_per_trade=cfg.fixed_fee, min_notional=cfg.min_trade)
    broker.portfolio = exp.to_portfolio()
    broker.set_prices(prices)

    fills = []
    try:
        for order in orders:
            decision = guardrail.check(order, broker.get_portfolio(), prices)
            if not decision.ok:
                fills.append({"symbol": order.symbol, "side": order.side.value,
                              "status": "blocked", "reason": decision.reason})
                continue
            result = broker.place_order(decision.order, when=date)
            if result.status == "filled":   # 실체결 기반 장부 갱신(손익·수수료 추적)
                _apply_fill(exp, order.side, order.symbol,
                            result.filled_qty, result.fill_price, result.commission)
            fills.append({"symbol": order.symbol, "side": order.side.value,
                          "qty": round(order.quantity, 6), "status": result.status,
                          "fill": result.fill_price, "commission": result.commission,
                          "reason": order.reason})
    except ExperimentLedgerError:
        # 불변식 위반으로 장부가 중단 상태가 됨 → 그 상태·부분 체결분을 우선 영속화한 뒤 재발생.
        state.save(cfg.state_path)
        raise
    return fills, broker.total_commission_paid


def _execute_live(cfg, state, exp, orders, guardrail, prices, date, date_iso, cost, broker):
    """실계좌 집행 — 전후 대사 + 열린주문 복구 + 외부 사건 감지 + 부분체결 델타 정산.

    순서: ①시작 대사 → ②미결 열린주문 복구(재시작) → ③외부 사건 감지(증분5) → ④신규 주문(전송 前
    영속·정산) → ⑤종료 대사·스냅샷 갱신. 어느 단계든 대사 실패/사건/장부 중단이면 신규 주문을
    멈춘다(fail-closed). 반환: (fills, 누적 수수료).
    """
    fills: list[dict] = []
    commission_total = 0.0
    start_holdings = broker.account_holdings()   # 이번 실행 시작 시점 실계좌 보유(대사·사건감지 공용)

    # ① 시작 대사: 실계좌 보유가 보호분+실험분 미만이면(수동매매/출금 의심) 즉시 중단.
    ok, reason = reconcile_or_halt(exp, start_holdings)
    if not ok:
        fills.append({"status": "halted", "reason": f"시작 대사 실패: {reason}"})
        state.last_account_snapshot = start_holdings
        state.save(cfg.state_path)
        return fills, commission_total

    # ② 재시작 복구: 지난 실행에서 미결로 남은 열린주문을 먼저 정산(중복 전송 없이 실체결만 회수).
    #    experiment_delta = 그 사이 실험이 만든 순 보유 변화 → ③ 사건감지의 '설명 가능분'으로 뺀다.
    try:
        rec_commission, experiment_delta = _recover_open_orders(cfg, state, exp, broker, fills)
        commission_total += rec_commission
    except ExperimentLedgerError:
        state.save(cfg.state_path)
        raise

    # ③ 외부 사건 감지(증분5): 지난 실행 종료 스냅샷 대비, 실험이 설명 못하는 보유 변화(수동매매·
    #    기업행동·배당재투자)면 귀속을 추측하지 않고 halt한다(fail-closed). 기준선은 baseline 초기화가
    #    항상 채우므로(빈 전용계좌면 {}) live에선 매 실행 비교한다 — 빈 dict라고 건너뛰지 않는다
    #    (그러면 빈 계좌에 낀 수동매수를 놓친다). 설명 가능분(experiment_delta)은 잔여에서 뺀다.
    detected = detect_events(state.last_account_snapshot, start_holdings, experiment_delta)
    if detected:
        exp.halt("외부 계좌 사건 감지: " + summarize(detected))
        fills.append({"status": "halted", "reason": exp.status_reason, "events": detected})
        state.last_account_snapshot = start_holdings
        state.save(cfg.state_path)
        return fills, commission_total

    # ④ 신규 주문: 전송 '이전'에 의도·멱등키를 영속화(write-ahead) → 전송·정산 → 델타 반영.
    state_id = _state_id(cfg)
    try:
        for order in orders:
            if not exp.can_trade:   # 복구 중 장부가 중단됐으면 신규 주문 없음(fail-closed)
                fills.append({"symbol": order.symbol, "side": order.side.value,
                              "status": "blocked", "reason": f"중단({exp.status})"})
                continue
            decision = guardrail.check(order, exp.to_portfolio(), prices)
            if not decision.ok:
                fills.append({"symbol": order.symbol, "side": order.side.value,
                              "status": "blocked", "reason": decision.reason})
                continue
            order = decision.order
            coid = ob.stable_client_order_id(state_id, date_iso, order.symbol, order.side)
            price = prices.get(order.symbol, 0.0)
            reserved_cash = cost.buy_cost(order.quantity, price) if order.side is Side.BUY else 0.0
            reserved_sell = order.quantity if order.side is Side.SELL else 0.0
            record = ob.make_intent(coid, order, reserved_cash=reserved_cash,
                                    reserved_sell_qty=reserved_sell)
            state.open_orders[coid] = record
            state.save(cfg.state_path)   # ← 전송 前 저장: 여기서 죽어도 재시작이 같은 키로 이어받음

            result = broker.place_and_settle(order, client_order_id=coid, when=date)
            delta_commission, _ = _apply_and_record(exp, record, result, fills)
            commission_total += delta_commission
            state.open_orders = ob.prune_terminal(state.open_orders)
            state.save(cfg.state_path)
    except ExperimentLedgerError:
        state.save(cfg.state_path)
        raise

    # ⑤ 종료 대사 + 스냅샷 갱신: 체결 후 실계좌를 다시 확인(불일치면 다음 실행부터 fail-closed).
    #    종료 시점 보유를 다음 실행의 사건감지 비교 기준선으로 영속(증분5).
    end_holdings = broker.account_holdings()
    reconcile_or_halt(exp, end_holdings)
    state.last_account_snapshot = end_holdings
    state.save(cfg.state_path)
    return fills, commission_total


def _recover_open_orders(cfg, state, exp, broker, fills) -> tuple[float, dict[str, float]]:
    """재시작 시 미결 열린주문을 이어받아 실체결 델타만 장부에 반영(중복 전송 없이).

    orderId를 이미 안다면 재조회(settle_existing)만, 모른다면(전송 후 저장 전 사망) 같은 멱등키로
    재전송한다 — 브로커의 clientOrderId dedup이 '같은 주문'을 하나로 묶어 중복주문을 막는다.
    반환: (누적 수수료, 종목별 실험 순 수량 변화) — 뒤 값은 증분5 사건감지가 '설명 가능한 변화'로 뺀다.
    """
    commission_total = 0.0
    experiment_delta: dict[str, float] = {}   # 실험이 만든 순 보유 변화(매수 +, 매도 −)
    pending = ob.prune_terminal(state.open_orders)   # terminal(완전체결/거부/취소)은 복구 불필요
    for coid, record in list(pending.items()):
        order = ob.order_from_record(record)
        broker_order_id = record.get("broker_order_id")
        if broker_order_id:
            result = broker.settle_existing(broker_order_id, order)         # 조회만(전송 없음)
        else:
            result = broker.place_and_settle(order, client_order_id=coid)   # 멱등 재전송
        delta_commission, signed_qty = _apply_and_record(exp, record, result, fills,
                                                         recovered=True)
        commission_total += delta_commission
        # 사건감지용 delta에는 '조회 복구'(이미 전송된 주문 = 그 체결이 시작 스냅샷에 반영됨)만
        # 넣는다. '재전송'은 체결이 지금(스냅샷 이후) 일어나므로 시작 스냅샷과 비교할 delta가 아니다.
        if signed_qty and broker_order_id:
            experiment_delta[record["symbol"]] = experiment_delta.get(record["symbol"], 0.0) + signed_qty
        if not exp.can_trade:       # 복구 중 불변식 위반이면 남은 주문 복구 중단
            break
    state.open_orders = ob.prune_terminal(state.open_orders)
    return commission_total, experiment_delta


def _apply_and_record(exp, record, result, fills, *, recovered: bool = False) -> tuple[float, float]:
    """브로커 응답을 델타 정산해 장부에 반영하고 fills 행을 남긴다.

    브로커의 filled_qty는 '누적'이라, 이미 반영한 만큼을 뺀 델타만 장부에 흘린다(이중 카운트 방지).
    반환: (델타 수수료, 실험 순 수량 변화 — 매수 +, 매도 −). 뒤 값은 사건감지가 '설명 가능분'으로 쓴다.
    """
    delta_qty, delta_commission, fill_price = ob.settle_delta(record, result)
    signed_qty = 0.0
    if delta_qty > 0:
        side = Side(record["side"])
        _apply_fill(exp, side, record["symbol"], delta_qty, fill_price, delta_commission)
        signed_qty = delta_qty if side is Side.BUY else -delta_qty
    fills.append({
        "symbol": record["symbol"], "side": record["side"],
        "qty": round(record["requested_qty"], 6),
        "filled_qty": round(record["accounted_filled_qty"], 6),
        "status": result.status, "fill": fill_price,
        "commission": round(delta_commission, 6),
        "broker_order_id": record.get("broker_order_id", ""),
        "recovered": recovered, "reason": record.get("reason", ""),
    })
    return delta_commission, signed_qty


def _build_live_broker(cfg: ForwardConfig, exp, cost: CostModel) -> CarveOutBroker:
    """실계좌 CarveOutBroker 조립 — 내부 TossBroker는 .env(TRADING_MODE)로 live 여부 결정.

    내부 TossBroker의 계좌전체 예산 방어는 끄고(enforce_budget=False), 방어는 카브아웃 계층이
    experiment_nav/cash 기준으로 수행한다(스펙 §다층 방어).
    """
    from src.execution.toss_broker import build_toss_broker_from_env
    inner = build_toss_broker_from_env(cfg.symbols, budget=cfg.budget)
    inner.enforce_budget = False
    return CarveOutBroker(inner, exp, cost=cost)


def _state_id(cfg: ForwardConfig) -> str:
    """멱등키 접두사 — state_dir 이름으로 실험을 식별(재시작 후에도 안정적)."""
    return os.path.basename(os.path.normpath(cfg.state_dir)) or "forward"


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


def _signals_to_dicts(signals: list[Signal]) -> list[dict]:
    """결정을 state.json에 실어 나르기 위한 직렬화(다음 거래일 체결용)."""
    return [{"symbol": s.symbol, "action": s.action.value,
             "target_weight": s.target_weight, "rationale": s.rationale}
            for s in signals]


def _signals_from_dicts(items: list[dict], timestamp) -> list[Signal]:
    """직전 결정 복원 — 오늘 가격에 체결한다. 리밸런서가 쓰는 필드만 채우면 충분."""
    out = []
    for it in items or []:
        out.append(Signal(
            symbol=it["symbol"],
            action=Action(it.get("action", "hold")),
            target_weight=float(it.get("target_weight", 0.0)),
            timestamp=timestamp,
            rationale=it.get("rationale", "")))
    return out


def _append_jsonl(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)
