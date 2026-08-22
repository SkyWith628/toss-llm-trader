"""MetaPortfolioStrategy — 여러 전략을 자본 예산으로 합성하는 Composite 전략.

[What] 각 슬리브의 generate_signal을 호출해 목표비중 w_ij를 받고, Allocator의 자본비중 a_i로
       스케일해 종목별로 합산(merged[sym] += a_i·w_ij) → 통합 Signal 리스트를 반환한다.
[Why]  메타-포트폴리오 자신이 Strategy 계약을 만족하면 백테스트 드라이버(main.py)·오케스트레이터
       어디든 '단일 전략'처럼 무수정으로 꽂힌다(LSP). 배분을 더해도 측정 코드는 안 바뀐다.
[How]  Σw_ij ≤ 1, Σa_i ≤ 1 이므로 합성도 Σ ≤ 1 (예산 초과 원천 불가). 미언급 종목은 비중 0으로
       채워 리밸런서가 매도하게 한다. 기여도(attribution)는 last_decision에 노출.
       설계: docs/design-meta-portfolio.md
"""
from __future__ import annotations

from src.core.types import Action, MarketSnapshot, Signal
from src.portfolio.allocator import Allocator
from src.portfolio.sleeve import Sleeve
from src.strategy.base import Strategy


class MetaPortfolioStrategy(Strategy):
    name = "meta"

    def __init__(self, sleeves: list[Sleeve], allocator: Allocator) -> None:
        if not sleeves:
            raise ValueError("슬리브가 최소 1개는 필요합니다.")
        names = [s.name for s in sleeves]
        if len(names) != len(set(names)):
            raise ValueError(f"슬리브 이름은 유일해야 합니다: {names}")
        self.sleeves = sleeves
        self.allocator = allocator
        self.last_decision: dict = {}      # 기여도·배분 로깅(원장 태깅용)

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        alloc = self.allocator.weights(self.sleeves, snapshot)

        merged: dict[str, float] = {}
        contrib: dict[str, dict[str, float]] = {}     # 슬리브별 스케일된 기여 비중
        for sleeve in self.sleeves:
            a_i = alloc.get(sleeve.name, 0.0)
            if a_i <= 0:
                continue
            # 슬리브는 '자기 유니버스'만 본다(격리) — 코어(주식)와 페어(3x)가 섞이지 않게.
            sub = sleeve.strategy.generate_signal(_restrict(snapshot, sleeve.symbols))
            scaled: dict[str, float] = {}
            for s in sub:
                if s.target_weight > 0:
                    w = a_i * s.target_weight
                    merged[s.symbol] = merged.get(s.symbol, 0.0) + w
                    scaled[s.symbol] = scaled.get(s.symbol, 0.0) + w
            if scaled:
                contrib[sleeve.name] = scaled

        self.last_decision = {
            "allocation": alloc,           # {슬리브: a_i}
            "contribution": contrib,       # {슬리브: {종목: a_i·w}}
            "merged": dict(merged),        # {종목: 합산 비중}
        }

        # snapshot.prices 전체를 Signal로 — 미언급 종목은 비중 0(현금화)이라야 매도가 실행된다.
        signals: list[Signal] = []
        for sym in snapshot.prices:
            w = merged.get(sym, 0.0)
            signals.append(Signal(
                symbol=sym,
                action=Action.BUY if w > 0 else Action.SELL,
                target_weight=w,
                timestamp=snapshot.timestamp,
                rationale=_rationale(sym, contrib) if w > 0 else "배분 밖 → 현금화",
            ))
        return signals


def _restrict(snapshot: MarketSnapshot, symbols: list[str]) -> MarketSnapshot:
    """스냅샷을 슬리브 유니버스로 좁힌 사본. 슬리브 전략은 자기 종목만 관측(격리).

    [Why] 이질 슬리브(주식 코어 + 3x 페어)를 섞을 때, 각 전략이 전체 유니버스를 보면 코어가
          페어까지 담거나 모멘텀이 페어를 랭킹에 넣는 오염이 생긴다. Sleeve.symbols를 강제한다.
    """
    keep = set(symbols)
    return MarketSnapshot(
        timestamp=snapshot.timestamp,
        prices={s: p for s, p in snapshot.prices.items() if s in keep},
        history={s: h for s, h in snapshot.history.items() if s in keep},
    )


def _rationale(sym: str, contrib: dict[str, dict[str, float]]) -> str:
    """이 종목에 기여한 슬리브들을 근거 문자열로(실험 분석용)."""
    parts = [f"{name} {sc[sym]:.3f}" for name, sc in contrib.items() if sym in sc]
    return "메타 배분: " + " + ".join(parts)
