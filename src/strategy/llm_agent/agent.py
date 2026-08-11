"""LLM 자율매매 전략 — Claude가 tool-calling으로 매일 목표 비중을 결정한다.

[What] MarketSnapshot -> Claude 호출 -> submit_decision 결과 -> list[Signal].
[Why]  '모멘텀 규칙' 자리에 LLM을 꽂아, 동일 백테스트 환경에서 규칙 대비 자율매매를 측정한다.
[How]  Strategy 인터페이스만 구현하므로 드라이버 수정 없이 교체 가능(main --strategy llm).
       비결정성(opus-5는 temperature 조절 불가)을 감안해 프롬프트·응답·usage를 원장과
       별도 JSONL로 남겨 사후 재현·분석에 쓴다.
"""
from __future__ import annotations

import json
import os
from typing import Any

from src.core.types import MarketSnapshot, Signal
from src.strategy.base import Strategy
from src.strategy.llm_agent.decision_tool import (
    DECISION_TOOL,
    TOOL_NAME,
    decision_to_signals,
)
from src.strategy.llm_agent.prompts import SYSTEM_PROMPT, render_snapshot


class LLMAgentStrategy(Strategy):
    """Claude에게 매매 전권을 준 전략. 안전판·예산 한도는 상위 계층이 강제한다."""

    def __init__(
        self,
        model: str = "claude-opus-5",
        max_tokens: int = 2048,
        client: Any | None = None,
        log_path: str | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.log_path = log_path
        self.name = f"llm_agent({model})"
        self._client = client  # 테스트에서 목(mock) 주입 가능

    @property
    def client(self) -> Any:
        """지연 초기화 — anthropic은 선택 의존성(단계 0은 표준 라이브러리만으로 동작)."""
        if self._client is None:
            import anthropic  # noqa: PLC0415

            self._client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
        return self._client

    def generate_signal(self, snapshot: MarketSnapshot) -> list[Signal]:
        universe = sorted(snapshot.prices)
        date = snapshot.timestamp.date().isoformat()

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": render_snapshot(snapshot)}],
                tools=[DECISION_TOOL],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                thinking={"type": "disabled"},  # 강제 툴 호출 + 비용/지연 절감
            )
        except Exception as exc:  # API 실패 → 그날은 무변경(안전 폴백)
            self._log({"date": date, "error": repr(exc)})
            return []

        decision = self._extract_decision(resp)
        if decision is None:
            self._log({"date": date, "error": "submit_decision 툴 호출 없음"})
            return []

        signals = decision_to_signals(decision, universe, snapshot.timestamp)
        self._log({
            "date": date,
            "model": self.model,
            "market_view": decision.get("market_view", ""),
            "decision": decision,
            "usage": self._usage(resp),
        })
        return signals

    @staticmethod
    def _extract_decision(resp: Any) -> dict | None:
        """응답 블록에서 submit_decision tool_use 입력을 꺼낸다."""
        for block in getattr(resp, "content", []):
            if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
                return dict(block.input)
        return None

    @staticmethod
    def _usage(resp: Any) -> dict:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return {}
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }

    def _log(self, record: dict) -> None:
        """결정 로그를 원장과 분리해 append(재현·비용 분석용). log_path 없으면 무동작."""
        if not self.log_path:
            return
        directory = os.path.dirname(self.log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
