"""IntradayState — 실시간 상시 루프의 지속 상태(크래시·재시작 내성).

[What] 당일 앵커(day_start_equity)·서킷 발동 여부(halted)·포트폴리오·일별 종가자산을
       JSON으로 저장/복원한다.
[Why]  장중 상주 프로세스는 언제든 죽을 수 있다. 상태가 없으면 재시작 시 '오늘 앵커'를
       다시 잡아(이중 기준) 서킷을 잘못 판정하거나, 이미 청산(halted)한 날 재진입할 수 있다.
[How]  저장은 tmp→os.replace(원자적)로 반쪽 파일 방지. 하루 경계는 trading_date로 감지 —
       같은 날 재시작이면 앵커·halted를 '그대로 복원'하고, 새 날일 때만 새 앵커를 잡는다.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

from src.core.types import Portfolio, Position


@dataclass
class IntradayState:
    # --- 실험 단위(여러 날 지속) ---
    budget: float
    symbols: list[str]
    start_date: str = ""                                # 첫 거래일(ET)
    cash: float = 0.0
    positions: dict[str, dict] = field(default_factory=dict)   # sym -> {quantity, avg_price}
    commission: float = 0.0
    daily_equity: list[dict] = field(default_factory=list)     # [{date, equity}] 일별 종가자산

    # --- 당일 단위(매 거래일 리셋) ---
    trading_date: str = ""            # 오늘 ET 날짜(이 값으로 '새 날' 판정)
    day_start_equity: float = 0.0     # 당일 시작 자산(서킷 앵커)
    halted: bool = False              # 서킷 발동 후 True(당일 신규·재진입 차단)
    halt_reason: str = ""             # "take_profit" | "stop_loss" | ""
    last_tick_ts: str = ""            # 마지막 감시 시각(ISO UTC) — 복원·진단용

    # --- 생성/입출력 ---
    @classmethod
    def new(cls, budget: float, symbols: list[str]) -> "IntradayState":
        return cls(budget=budget, symbols=list(symbols), cash=budget)

    @classmethod
    def load(cls, path: str) -> "IntradayState | None":
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

    # --- 하루 경계 ---
    def is_new_day(self, trading_date: str) -> bool:
        """오늘이 아직 앵커를 안 잡은 새 거래일인가(재시작이면 False → 앵커 유지)."""
        return trading_date != self.trading_date

    def start_new_day(self, trading_date: str, day_start_equity: float) -> None:
        """개장 시 1회: 당일 앵커 고정 + 서킷 상태 리셋."""
        self.trading_date = trading_date
        self.day_start_equity = day_start_equity
        self.halted = False
        self.halt_reason = ""
        if not self.start_date:
            self.start_date = trading_date

    def mark_halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def touch(self, ts: str) -> None:
        self.last_tick_ts = ts

    # --- 일별 종가자산(성과지표용) ---
    def record_daily_close(self, date: str, equity: float) -> None:
        """당일 종가자산 기록. 같은 날 재호출이면 덮어씀(멱등)."""
        rounded = round(equity, 4)
        if self.daily_equity and self.daily_equity[-1]["date"] == date:
            self.daily_equity[-1]["equity"] = rounded
        else:
            self.daily_equity.append({"date": date, "equity": rounded})

    def equity_curve(self) -> list[float]:
        return [row["equity"] for row in self.daily_equity]
