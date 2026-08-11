"""Forward 실험의 영속 상태 — '매일 별도 프로세스'를 이어붙이는 접착제.

[What] 페이퍼 계좌(현금·보유) · 회고 메모리 · Buy&Hold 앵커 · 실험 메타를 JSON 1파일로
       저장/복원한다. 이 파일이 곧 페이퍼 계좌다(Toss 라우팅 없이 로컬 시뮬레이션).
[Why]  forward는 하루 1회 실행이라 프로세스 메모리가 매번 사라진다. 연속성(직전 결정
       회고·누적 손익·동일 앵커의 B&H)을 파일로 실어 날라야 공정 측정이 된다.
[How]  로드-또는-초기화. 첫 실행에서 예산 전액 현금 + 그날 가격으로 B&H 주식수를 '한 번'
       고정(이후 불변) → 전략과 동일 구간·동일 시작점 비교.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from src.core.types import Portfolio, Position


@dataclass
class ForwardState:
    """forward 실험의 전체 영속 상태(state.json). 작게 유지 — equity 시계열은 별도 파일."""

    budget: float
    symbols: list[str]
    source: str
    strategy: str
    model: str
    started_at: str = ""            # 첫 실행 거래일(ISO date)
    last_run_date: str = ""         # 마지막으로 매매한 거래일(중복 실행 방지)
    day_count: int = 0              # 매매한 거래일 수
    cash: float = 0.0
    positions: dict[str, dict] = field(default_factory=dict)   # sym -> {quantity, avg_price}
    bh_shares: dict[str, float] = field(default_factory=dict)  # B&H 주식수(앵커, 불변)
    bh_anchor: dict[str, float] = field(default_factory=dict)  # 앵커 시점 가격(기록용)
    memory: list[dict] = field(default_factory=list)           # DecisionMemory 덤프
    # 직전 거래일에 내린 결정 — '오늘' 체결한다(시점누수 방지: 당일종가 신호를 당일 체결하지 않음).
    # 각 항목: {"symbol","action","target_weight","rationale"}
    pending_signals: list[dict] = field(default_factory=list)

    # ---------- 로드/초기화/저장 ----------
    @classmethod
    def load_or_init(
        cls, path: str, *, budget: float, symbols: list[str],
        source: str, strategy: str, model: str,
    ) -> "ForwardState":
        """기존 state.json이 있으면 로드, 없으면 예산 전액 현금으로 신규 실험 시작.

        기존 파일이 있으면 예산·종목 구성이 바뀌었는지 검사해 실수로 다른 실험을
        같은 파일에 섞지 않도록 막는다(측정 오염 방지).
        """
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            st = cls(**data)
            if abs(st.budget - budget) > 1e-9 or sorted(st.symbols) != sorted(symbols):
                raise ValueError(
                    f"기존 실험과 구성이 다릅니다(budget/symbols). "
                    f"새 실험은 다른 --state-dir을 쓰세요. 기존={st.symbols}@${st.budget}")
            return st
        return cls(budget=budget, symbols=list(symbols), source=source,
                   strategy=strategy, model=model, cash=budget)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 원자적 교체 — 쓰다 죽어도 상태 파일이 깨지지 않게

    # ---------- Portfolio <-> state 브리지 ----------
    def to_portfolio(self) -> Portfolio:
        pf = Portfolio(cash=self.cash)
        for sym, p in self.positions.items():
            pf.positions[sym] = Position(sym, p["quantity"], p["avg_price"])
        return pf

    def update_from_portfolio(self, pf: Portfolio) -> None:
        self.cash = round(pf.cash, 6)
        self.positions = {
            sym: {"quantity": round(pos.quantity, 8), "avg_price": round(pos.avg_price, 6)}
            for sym, pos in pf.positions.items()
        }

    # ---------- Buy&Hold 앵커(첫 실행에 한 번 고정) ----------
    def ensure_bh_anchor(self, prices: dict[str, float]) -> None:
        """B&H를 아직 안 심었으면 지금 가격으로 동일비중 매수(슬리피지 1회) 후 고정."""
        if self.bh_shares:
            return
        slippage = 0.0005
        per_symbol = self.budget / len(self.symbols)
        self.bh_anchor = {s: prices[s] for s in self.symbols}
        self.bh_shares = {
            s: per_symbol / (prices[s] * (1 + slippage)) for s in self.symbols
        }

    def bh_value(self, prices: dict[str, float]) -> float:
        return sum(self.bh_shares.get(s, 0.0) * prices.get(s, 0.0) for s in self.symbols)
