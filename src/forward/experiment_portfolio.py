"""실험 장부 — 공유 실계좌 안에서 '$100 실험 자산'만 격리 추적한다.

[What] 계좌의 기존 보유(protected)는 건드리지 않고, 실험이 매수한 수량(experiment)과
       실험 현금만 별도 장부로 관리. 실험 순자산(NAV)·실현손익·수수료를 추적한다.
[Why]  증권사 계좌에선 기존분과 실험분이 물리적으로 안 나뉜다(같은 AAPL 풀). 애플리케이션이
       귀속을 분리해야 '보호분은 절대 안 팔고, 실험분만 자동매매'가 성립한다.
       (EXPERIMENT_PORTFOLIO_SPEC.md 구현 — 단계 (b) 증분1: 순수 장부 + NAV.)
[How]  실험 슬라이스를 표준 Portfolio로 투영(to_portfolio)해 기존 리밸런서·가드레일을
       '실험 자산 한정'으로 그대로 재사용. 리밸런싱 목표는 초기원금이 아니라 현재 NAV 기준
       (복리 재투자). 장부는 '실제 체결값'으로만 갱신한다.

주의: 이 모듈은 순수 장부다 — 주문을 보내지 않는다. 실주문·대사 배선은 이후 증분.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from src.core.types import Portfolio, Position


class ExperimentLedgerError(RuntimeError):
    """실제 체결이 실험 장부 불변식을 위반했다. 장부는 '보정' 계층이 아니라 '검증' 계층이므로,
    조용히 수량을 줄이지 않고 fail-closed 한다(보호분 침범을 숨기지 않기 위함)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExperimentPosition:
    symbol: str
    quantity: float
    avg_price: float


# 실험 장부 상태. status 로 신규 주문 허용 여부를 통제(fail-closed).
ACTIVE = "active"
RECONCILIATION_REQUIRED = "reconciliation_required"
HALTED = "halted"


@dataclass
class ExperimentPortfolio:
    """실험에 귀속된 자산만 담는 장부. 보호분(protected)은 '기준선'으로만 들고 절대 매도 대상 아님."""

    initial_capital: float
    cash: float                                              # 실험 귀속 현금
    protected_positions: dict[str, float] = field(default_factory=dict)   # sym -> 보호 수량
    experiment_positions: dict[str, ExperimentPosition] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    status: str = ACTIVE
    status_reason: str = ""        # 중단/대사 사유 — 재시작 후에도 원인 확인 가능하게 영속
    status_changed_at: str = ""    # 상태 전이 시각(ISO/UTC)

    # ---------- 조회 ----------
    @property
    def can_trade(self) -> bool:
        return self.status == ACTIVE

    def nav(self, prices: dict[str, float]) -> float:
        """실험 순자산 = 실험 현금 + 실험 보유 평가액. 리밸런싱·노출 판단의 기준."""
        held = sum(ep.quantity * prices.get(sym, ep.avg_price)
                   for sym, ep in self.experiment_positions.items())
        return self.cash + held

    def max_sellable(self, symbol: str) -> float:
        """자동매도 가능 최대 수량 = 실험 귀속 수량(보호분은 포함 안 됨)."""
        ep = self.experiment_positions.get(symbol)
        return ep.quantity if ep else 0.0

    def to_portfolio(self) -> Portfolio:
        """실험 슬라이스를 표준 Portfolio로 투영 — 기존 리밸런서·가드레일을 '실험 한정'으로 재사용.

        보호분은 담지 않는다 → 리밸런서가 애초에 보호분을 주문 대상에 넣지 못한다(구조적 보호).
        """
        pf = Portfolio(cash=self.cash)
        for sym, ep in self.experiment_positions.items():
            pf.positions[sym] = Position(sym, ep.quantity, ep.avg_price)
        return pf

    # ---------- 실제 체결 반영(장부는 요청값이 아니라 체결값으로만 갱신) ----------
    def apply_buy(self, symbol: str, qty: float, fill_price: float,
                  commission: float = 0.0) -> None:
        """실험 매수 체결 반영 — 실험 현금 차감, 실험 포지션 가중평균 갱신."""
        if qty <= 0:
            return
        self.cash -= qty * fill_price + commission
        self.fees_paid += commission
        ep = self.experiment_positions.get(symbol)
        if ep is None:
            self.experiment_positions[symbol] = ExperimentPosition(symbol, qty, fill_price)
        else:
            total = ep.quantity + qty
            ep.avg_price = (ep.avg_price * ep.quantity + fill_price * qty) / total
            ep.quantity = total

    def apply_sell(self, symbol: str, qty: float, fill_price: float,
                   commission: float = 0.0, tolerance: float = 1e-6) -> None:
        """실험 매도 체결 반영 — 순대금만 실험 현금에 가산, 실현손익 기록.

        실제 체결 수량이 실험 귀속 수량을 초과하면 '조용히 축소'하지 않는다. 그건 보호분(기존
        보유)이 침범됐다는 신호이므로 즉시 대사 필요 상태로 전환하고 예외를 던진다(fail-closed).
        """
        if qty <= 0:
            return
        ep = self.experiment_positions.get(symbol)
        held = ep.quantity if ep else 0.0
        if qty > held + tolerance:
            reason = f"{symbol} 실험 수량 초과 체결: 체결 {qty:.6f} > 실험 {held:.6f}"
            self.require_reconciliation(reason)      # 보호분 침범 가능 → 신규 주문 중단
            raise ExperimentLedgerError(reason)
        qty = min(qty, held)                          # 허용오차 내 미세 초과분만 보정(음수 방지)
        net_proceeds = qty * fill_price - commission
        self.cash += net_proceeds
        self.fees_paid += commission
        self.realized_pnl += (fill_price - ep.avg_price) * qty - commission
        ep.quantity -= qty
        if ep.quantity <= 1e-9:
            del self.experiment_positions[symbol]

    # ---------- 상태 전이(fail-closed) ----------
    def halt(self, reason: str) -> None:
        self.status = HALTED
        self.status_reason = reason
        self.status_changed_at = _now_iso()

    def require_reconciliation(self, reason: str) -> None:
        if self.status == ACTIVE:
            self.status = RECONCILIATION_REQUIRED
        self.status_reason = reason
        self.status_changed_at = _now_iso()

    # ---------- 직렬화(state.json 통합용) ----------
    def to_dict(self) -> dict:
        d = asdict(self)
        # experiment_positions: ExperimentPosition -> dict (asdict가 이미 처리)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentPortfolio":
        eps = {
            sym: ExperimentPosition(**ep) if isinstance(ep, dict) else ep
            for sym, ep in (data.get("experiment_positions") or {}).items()
        }
        return cls(
            initial_capital=data["initial_capital"],
            cash=data["cash"],
            protected_positions=dict(data.get("protected_positions") or {}),
            experiment_positions=eps,
            realized_pnl=data.get("realized_pnl", 0.0),
            fees_paid=data.get("fees_paid", 0.0),
            status=data.get("status", ACTIVE),
            status_reason=data.get("status_reason", ""),
            status_changed_at=data.get("status_changed_at", ""),
        )
