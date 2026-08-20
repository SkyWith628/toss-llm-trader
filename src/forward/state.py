"""ForwardState — 프로세스 재시작 사이에 지속되는 실험 상태.

[What] 포트폴리오(현금·보유)·에이전트 메모리·Buy&Hold 기준점·처리한 거래일·자본곡선을
       하나의 JSON 파일로 저장/복원한다.
[Why]  포워드 테스트는 하루 1회 프로세스라 메모리상 상태가 매번 사라진다. 지속 계층이 없으면
       '어제까지의 포지션'을 잃어 실험이 성립하지 않는다.
[How]  stdlib json. 저장은 tmp에 쓰고 os.replace로 교체(원자적) — 저장 도중 크래시해도
       파일이 반쪽으로 깨지지 않는다. 알 수 없는 필드는 무시해 스키마 진화에 견딘다.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

from src.core.types import Portfolio, Position


@dataclass
class ForwardState:
    budget: float
    symbols: list[str]
    start_date: str = ""                                # 첫 처리 거래일(ISO)
    cash: float = 0.0
    positions: dict[str, dict] = field(default_factory=dict)   # sym -> {quantity, avg_price}
    commission: float = 0.0                             # 누적 지불 수수료
    memory: list[dict] = field(default_factory=list)    # DecisionMemory.to_list()
    bh_shares: dict[str, float] = field(default_factory=dict)  # Buy&Hold 1일차 매수 주식수(고정)
    processed_dates: list[str] = field(default_factory=list)   # 이미 매매한 거래일(멱등성)
    strat_equity: list[float] = field(default_factory=list)    # 전략 자본곡선
    bh_equity: list[float] = field(default_factory=list)       # Buy&Hold 자본곡선

    # --- 생성/입출력 ---
    @classmethod
    def new(cls, budget: float, symbols: list[str]) -> "ForwardState":
        return cls(budget=budget, symbols=list(symbols), cash=budget)

    @classmethod
    def load(cls, path: str) -> "ForwardState | None":
        """파일이 없으면 None(=첫 실행). 알 수 없는 키는 버려 하위호환 유지."""
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 원자적 교체(크래시 내성)

    # --- 포트폴리오 왕복 ---
    def to_portfolio(self) -> Portfolio:
        return Portfolio(
            cash=self.cash,
            positions={s: Position(s, p["quantity"], p["avg_price"])
                       for s, p in self.positions.items()},
        )

    def set_from_portfolio(self, pf: Portfolio) -> None:
        self.cash = pf.cash
        self.positions = {s: {"quantity": p.quantity, "avg_price": p.avg_price}
                          for s, p in pf.positions.items()}

    # --- Buy&Hold 기준점(1일차에 고정) ---
    def set_bh_anchor(self, prices: dict[str, float], slippage: float) -> None:
        """예산을 동일비중으로 나눠 1일차 진입가(슬리피지 반영)로 매수한 주식수를 고정한다."""
        per_symbol = self.budget / len(self.symbols)
        self.bh_shares = {
            s: per_symbol / (prices[s] * (1 + slippage))
            for s in self.symbols if prices.get(s)
        }

    def bh_value(self, prices: dict[str, float]) -> float:
        return sum(sh * prices.get(s, 0.0) for s, sh in self.bh_shares.items())
