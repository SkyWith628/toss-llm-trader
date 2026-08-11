"""LLM 에이전트의 단기 메모리 — '매일 백지' 한계를 없앤다.

[What] 직전 결정(목표비중·판단요약·당시 관측가)을 기억하고, 오늘 시점에서
       그 결정 이후 가격이 어떻게 움직였는지 '회고' 텍스트로 만든다.
[Why]  Strategy 인터페이스는 스냅샷만 주므로, 연속성은 전략 객체 내부 상태로 확보한다.
       (인터페이스를 안 건드려 규칙 전략과의 공정 비교·교체성을 유지 — DIP.)
[How]  전략 인스턴스가 백테스트 전체 수명 동안 살아있음을 이용해 self에 누적.
       실보유는 인터페이스로 안 오므로 '목표비중 + 당시 관측가'만으로 근사 회고.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class MemoryEntry:
    date: str
    market_view: str
    weights: dict[str, float]   # 그날 결정한 목표 비중(>0인 것만)
    prices: dict[str, float]    # 당시 관측 현재가(회고 기준점)


class DecisionMemory:
    """직전 결정들을 담는 경량 링버퍼. 최근 결정 대비 결과를 회고 텍스트로 낸다."""

    def __init__(self, max_entries: int = 5) -> None:
        self._entries: deque[MemoryEntry] = deque(maxlen=max_entries)

    def record(
        self,
        date: str,
        market_view: str,
        weights: dict[str, float],
        prices: dict[str, float],
    ) -> None:
        self._entries.append(
            MemoryEntry(date, market_view, dict(weights), dict(prices))
        )

    def __len__(self) -> int:
        return len(self._entries)

    def retrospective(self, current_prices: dict[str, float]) -> str:
        """가장 최근 결정 대비 지금까지의 결과를 회고 텍스트로. 없으면 빈 문자열."""
        if not self._entries:
            return ""
        last = self._entries[-1]
        moves = []
        for sym, w in sorted(last.weights.items(), key=lambda kv: -kv[1]):
            p0 = last.prices.get(sym)
            p1 = current_prices.get(sym)
            chg = f"{(p1 - p0) / p0:+.2%}" if p0 and p1 else "n/a"
            moves.append(f"{sym}(목표 {w:.0%}) → {chg}")
        moves_str = ", ".join(moves) if moves else "보유 없음(전량 현금)"
        return (
            f"[직전 결정 회고 | {last.date}]\n"
            f"- 당시 판단: {last.market_view}\n"
            f"- 그 이후 가격 변화(당시→현재): {moves_str}\n"
            f"- 이 결과를 되짚어 오늘 판단을 유지하거나 조정하라 "
            f"(근거 없는 잦은 매매는 비용이니 관성도 고려)."
        )
