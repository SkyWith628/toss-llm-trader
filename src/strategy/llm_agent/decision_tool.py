"""submit_decision 툴 — LLM의 매매 결정을 '구조화된 JSON'으로 강제 추출한다.

[What] Claude가 자유서술 대신 이 툴을 호출하게 해 종목별 목표 비중을 받아낸다.
[Why]  파싱 안정성 — 스키마로 필드를 고정해 후단(리밸런서) 회귀를 막는다.
       추후 지표조회·메모리 같은 '진짜 툴'을 추가할 때의 tool-calling 뼈대이기도 하다.
[How]  tool_choice로 이 툴을 강제 호출 → tool_use.input을 Signal 리스트로 변환.
       (숫자 범위 검증은 스키마가 아니라 decision_to_signals에서 클램프·정규화한다.)
"""
from __future__ import annotations

from datetime import datetime

from src.core.types import Action, Signal

TOOL_NAME = "submit_decision"

# strict 스키마는 min/max 같은 숫자 제약을 지원하지 않으므로 코드에서 검증한다.
# (더 강한 보장을 원하면 최신 SDK에서 이 dict에 "strict": True를 추가할 수 있다.)
DECISION_TOOL: dict = {
    "name": TOOL_NAME,
    "description": (
        "이번 거래일의 목표 포트폴리오를 제출한다. positions에는 '보유하고 싶은' 종목만 "
        "목표 비중(target_weight)과 함께 넣는다. 비중의 합은 1.0 이하여야 하며 나머지는 "
        "현금으로 남는다. 언급하지 않은 종목은 목표 비중 0(전량 매도)으로 처리된다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "market_view": {
                "type": "string",
                "description": "오늘 시장·종목에 대한 한두 문장 판단 요약(실험 분석용 근거).",
            },
            "positions": {
                "type": "array",
                "description": "보유 목표 종목 목록. 비우면 전량 현금.",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "종목 티커"},
                        "target_weight": {
                            "type": "number",
                            "description": "포트폴리오 내 목표 비중(0.0~1.0).",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "이 판단의 확신도(0.0~1.0).",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "이 종목을 이 비중으로 담는 이유.",
                        },
                    },
                    "required": ["symbol", "target_weight", "confidence", "rationale"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["market_view", "positions"],
        "additionalProperties": False,
    },
}


def _as_float(value, default: float) -> float:
    """LLM이 문자열·None을 넣어도 죽지 않게 안전 변환."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def decision_to_signals(
    decision: dict,
    universe: list[str],
    timestamp: datetime,
) -> list[Signal]:
    """submit_decision 입력을 Signal 리스트로 변환(안전 검증 포함).

    - 음수 비중은 0으로(롱온리), universe 밖 종목은 무시.
    - 비중 합이 1.0을 넘으면 비례 축소(정규화) — 예산 초과 노출 방지.
    - universe 중 언급되지 않은 종목은 target_weight 0(현금화)으로 채운다.
      (리밸런서는 신호에 들어온 종목만 처리하므로, 전 종목을 반드시 포함해야 매도가 실행된다.)
    """
    positions = {
        p["symbol"]: p
        for p in decision.get("positions", [])
        if isinstance(p, dict) and p.get("symbol") in universe
    }

    # 1) 비중 클램프(0~1) + 합산
    weights: dict[str, float] = {}
    for sym, p in positions.items():
        weights[sym] = max(0.0, min(1.0, _as_float(p.get("target_weight"), 0.0)))
    total = sum(weights.values())

    # 2) 합 > 1.0 이면 비례 정규화
    if total > 1.0:
        weights = {sym: w / total for sym, w in weights.items()}

    # 3) universe 전체를 Signal로(미언급 종목은 비중 0 → 현금화)
    signals: list[Signal] = []
    for sym in universe:
        weight = weights.get(sym, 0.0)
        p = positions.get(sym, {})
        rationale = str(p.get("rationale", "")).strip() or (
            "미언급 → 현금화" if sym not in positions else "비중 0"
        )
        signals.append(Signal(
            symbol=sym,
            action=Action.BUY if weight > 0 else Action.SELL,
            target_weight=weight,
            confidence=max(0.0, min(1.0, _as_float(p.get("confidence"), 0.5))),
            timestamp=timestamp,
            rationale=rationale,
        ))
    return signals
