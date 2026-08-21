"""SleeveAccount — 한 슬리브의 장중 실행 단위(독립 broker·risk·state를 든 Orchestrator).

[What] 이름 + 그 슬리브 전용 Orchestrator를 묶는다. Orchestrator가 이미 broker·risk·state·
       decider·symbols를 들고 begin/process_tick/end를 제공하므로, 슬리브는 그걸 이름과 함께
       감싸기만 하면 '독립 자본·독립 서킷을 가진 미니 트레이딩 시스템'이 된다.
[Why]  v2의 핵심 — 슬리브마다 서킷 앵커·halt가 독립. 한 슬리브가 청산돼도 다른 슬리브는 계속.
[How]  MetaOrchestrator가 피드를 단일 소유하고, 개장 시세·매 틱을 각 SleeveAccount.orch에
       펌프한다. 설계: docs/design-sleeve-accounts.md
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from src.execution.paper_broker import PaperBroker
from src.risk.day_risk import DayRiskManager
from src.risk.guardrail import Guardrail
from src.runtime.orchestrator import Orchestrator
from src.runtime.state import IntradayState


@dataclass
class SleeveAccount:
    name: str                # 배분·기여도·원장 태깅 키(유일)
    orch: Orchestrator       # 이 슬리브 전용 실행기(독립 broker·risk·state)

    @property
    def halted(self) -> bool:
        return self.orch.state.halted

    def start_equity(self) -> float:
        return self.orch.state.day_start_equity

    def capital(self) -> float:
        return self.orch.broker.get_portfolio().cash


def make_sleeve_account(
    *,
    name: str,
    symbols: list[str],
    capital: float,
    clock,
    open_decider=None,
    entry_decider=None,
    stop_loss: float,
    take_profit: float = 10.0,
    trailing: float | None = None,
    trailing_arm: float = 0.0,
    poll_interval: float = 0.0,
    state_path: str | None = None,
) -> SleeveAccount:
    """슬리브 하나를 자본 슬라이스와 함께 조립 — 독립 broker·risk·state를 든 Orchestrator.

    피드는 MetaOrchestrator가 단일 소유하므로 None(begin_trading_day에 개장 시세를 넘겨받는다).
    open_decider(개장 진입) 또는 entry_decider(장중 타이밍) 중 하나는 필요.
    """
    if state_path is None:
        state_path = os.path.join(tempfile.mkdtemp(), f"{name}.json")
    orch = Orchestrator(
        symbols=list(symbols), clock=clock, feed=None,
        broker=PaperBroker(starting_cash=capital),
        guardrail=Guardrail(budget=capital, valid_symbols=set(symbols)),
        risk=DayRiskManager(take_profit=take_profit, stop_loss=stop_loss,
                            trailing=trailing, trailing_arm=trailing_arm),
        state=IntradayState.new(capital, list(symbols)),
        open_decider=open_decider, entry_decider=entry_decider,
        state_path=state_path, poll_interval=poll_interval,
    )
    return SleeveAccount(name, orch)
