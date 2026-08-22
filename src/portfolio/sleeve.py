"""Sleeve — 배분 단위 하나(한 전략 + 그 전략이 다루는 유니버스).

[What] "이 전략에게 이 종목들을 맡긴다"를 한 값 객체로. Allocator가 슬리브 목록을 받아
       각 슬리브의 자본 비중 a_i를 정하고, MetaPortfolioStrategy가 신호를 합성한다.
[Why]  전략과 '그 전략의 유니버스·이름'을 묶어야 기여도(attribution)·역변동성 추정이 슬리브
       단위로 가능하다. 이름은 배분/원장 태깅의 키.
[How]  strategy는 generate_signal(snapshot)->list[Signal]만 만족하면 무엇이든 OK(덕 타이핑) —
       MomentumStrategy·BuyAndHoldStrategy·LLMAgentStrategy 등. v2에서 risk·capital 필드 추가.
       설계: docs/design-meta-portfolio.md
"""
from __future__ import annotations

from dataclasses import dataclass

from src.strategy.base import Strategy


@dataclass
class Sleeve:
    name: str                # 배분·기여도·원장 태깅의 키(유일해야 함)
    strategy: Strategy       # generate_signal(snapshot) -> list[Signal] (덕 타이핑)
    symbols: list[str]       # 이 슬리브의 유니버스(역변동성 추정·진단용)
