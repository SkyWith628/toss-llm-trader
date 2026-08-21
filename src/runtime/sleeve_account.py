"""SleeveAccount — 한 슬리브의 장중 실행 단위(독립 broker·risk·state를 든 Orchestrator).

[What] 이름 + 그 슬리브 전용 Orchestrator를 묶는다. Orchestrator가 이미 broker·risk·state·
       decider·symbols를 들고 begin/process_tick/end를 제공하므로, 슬리브는 그걸 이름과 함께
       감싸기만 하면 '독립 자본·독립 서킷을 가진 미니 트레이딩 시스템'이 된다.
[Why]  v2의 핵심 — 슬리브마다 서킷 앵커·halt가 독립. 한 슬리브가 청산돼도 다른 슬리브는 계속.
[How]  MetaOrchestrator가 피드를 단일 소유하고, 개장 시세·매 틱을 각 SleeveAccount.orch에
       펌프한다. 설계: docs/design-sleeve-accounts.md
"""
from __future__ import annotations

from dataclasses import dataclass

from src.runtime.orchestrator import Orchestrator


@dataclass
class SleeveAccount:
    name: str                # 배분·기여도·원장 태깅 키(유일)
    orch: Orchestrator       # 이 슬리브 전용 실행기(독립 broker·risk·state)

    @property
    def halted(self) -> bool:
        return self.orch.state.halted

    def start_equity(self) -> float:
        return self.orch.state.day_start_equity
