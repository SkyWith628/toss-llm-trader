"""Forward 실험의 영속 상태 — '매일 별도 프로세스'를 이어붙이는 접착제.

[What] 실험 장부(ExperimentPortfolio: 실험/보호 포지션·현금·손익·상태) · 회고 메모리 ·
       Buy&Hold 앵커 · 직전 결정(pending) · 실험 메타를 JSON 1파일로 저장/복원한다.
[Why]  forward는 하루 1회 실행이라 프로세스 메모리가 매번 사라진다. 연속성(직전 결정
       회고·누적 손익·동일 앵커의 B&H)을 파일로 실어 날라야 공정 측정이 된다.
[How]  실험 장부를 단일 모델(ExperimentPortfolio)로 통일 — 페이퍼는 protected_positions={}
       인 특수경우, 실계좌는 기존 보유를 보호분으로. 이후 CarveOutBroker가 같은 장부를 쓴다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from src.core.types import Portfolio
from src.forward.experiment_portfolio import ExperimentPortfolio

SCHEMA_VERSION = 4   # state.json 형식 버전. 구버전(필드 상이)은 로드 시 fail-closed.
                     # v3: open_orders(열린주문 장부) 추가 — live 멱등키/부분체결 재시작 복구용.
                     # v4: last_account_snapshot 추가 — 외부 계좌 사건 감지(증분5)의 비교 기준선.


def _config_diff(a: dict, b: dict) -> str:
    """두 canonical 설정의 차이를 사람이 읽을 문자열로. 같으면 빈 문자열."""
    keys = sorted(set(a) | set(b))
    diffs = [f"{k}: {a.get(k)!r}→{b.get(k)!r}" for k in keys if a.get(k) != b.get(k)]
    return ", ".join(diffs)


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
    experiment: ExperimentPortfolio | None = None   # 실험 장부(단일 모델)
    bh_shares: dict[str, float] = field(default_factory=dict)  # B&H 주식수(앵커, 불변)
    bh_anchor: dict[str, float] = field(default_factory=dict)  # 앵커 시점 가격(기록용)
    memory: list[dict] = field(default_factory=list)           # DecisionMemory 덤프
    # 직전 거래일에 내린 결정 — '오늘' 체결한다(시점누수 방지: 당일종가 신호를 당일 체결하지 않음).
    # 각 항목: {"symbol","action","target_weight","rationale"}
    pending_signals: list[dict] = field(default_factory=list)
    # live 열린주문 장부 — intent_id -> 열린주문 레코드(order_book.py). 전송 '이전'에 여기에
    # 기록·영속하고(write-ahead), 재시작 시 미결 주문을 이어받아 중복주문을 막는다. 페이퍼는 {}.
    open_orders: dict[str, dict] = field(default_factory=dict)
    # 외부 계좌 사건 감지(증분5)의 비교 기준선 — 지난 실행 종료 시 관측한 실계좌 보유. 이번 실행
    # 시작 보유와 비교해 '실험이 설명 못하는 변화'(수동매매·기업행동)를 잡아 halt. 페이퍼는 {}.
    last_account_snapshot: dict[str, float] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    config: dict = field(default_factory=dict)   # 결과에 영향 주는 설정(재실행 시 대조)

    # ---------- 로드/초기화/저장 ----------
    @classmethod
    def load_or_init(
        cls, path: str, *, budget: float, symbols: list[str],
        source: str, strategy: str, model: str, config: dict | None = None,
    ) -> "ForwardState":
        """기존 state.json이 있으면 로드, 없으면 예산 전액 현금으로 신규 실험 시작.

        기존 파일이 있으면 스키마 버전·예산·종목·결과영향 설정(config)이 바뀌었는지 검사해
        실수로 다른 실험을 같은 파일에 섞지 않도록 막는다(측정 오염 방지, fail-closed).
        """
        check_config = config is not None   # None이면 '검사 안 함'(상태 조회 목적)
        config = dict(config or {})
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            ver = data.get("schema_version")
            if ver is None:
                raise ValueError(
                    "구버전 상태 형식입니다(schema_version 없음). 리셋하거나 새 --state-dir로 시작하세요.")
            if ver != SCHEMA_VERSION:
                # 마이그레이션 함수가 생기기 전까지는 '정확히 일치'만 허용(변환 없는 통과 금지).
                raise ValueError(
                    f"지원하지 않는 상태 버전 {ver}(현재 {SCHEMA_VERSION}). 명시적 마이그레이션 없이는 "
                    f"열지 않습니다 — 새 --state-dir을 쓰세요.")
            exp = ExperimentPortfolio.from_dict(data["experiment"])
            st = cls(
                budget=data["budget"], symbols=list(data["symbols"]),
                source=data["source"], strategy=data["strategy"], model=data["model"],
                started_at=data.get("started_at", ""),
                last_run_date=data.get("last_run_date", ""),
                day_count=data.get("day_count", 0), experiment=exp,
                bh_shares=dict(data.get("bh_shares") or {}),
                bh_anchor=dict(data.get("bh_anchor") or {}),
                memory=list(data.get("memory") or []),
                pending_signals=list(data.get("pending_signals") or []),
                open_orders=dict(data.get("open_orders") or {}),
                last_account_snapshot=dict(data.get("last_account_snapshot") or {}),
                schema_version=ver, config=dict(data.get("config") or {}))
            if abs(st.budget - budget) > 1e-9 or sorted(st.symbols) != sorted(symbols):
                raise ValueError(
                    f"기존 실험과 구성이 다릅니다(budget/symbols). "
                    f"새 실험은 다른 --state-dir을 쓰세요. 기존={st.symbols}@${st.budget}")
            if check_config:
                diff = _config_diff(st.config, config)
                if diff:
                    raise ValueError(
                        f"기존 실험과 설정이 다릅니다({diff}). 다른 실험이 한 성과곡선에 "
                        f"섞이지 않게 새 --state-dir을 쓰세요.")
            return st
        # 신규: 페이퍼는 보호분 없음(protected_positions={}), 예산 전액이 실험 현금.
        exp = ExperimentPortfolio(initial_capital=budget, cash=budget)
        return cls(budget=budget, symbols=list(symbols), source=source,
                   strategy=strategy, model=model, experiment=exp, config=config)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "budget": self.budget, "symbols": self.symbols, "source": self.source,
            "strategy": self.strategy, "model": self.model, "config": self.config,
            "started_at": self.started_at, "last_run_date": self.last_run_date,
            "day_count": self.day_count,
            "experiment": self.experiment.to_dict() if self.experiment else None,
            "bh_shares": self.bh_shares, "bh_anchor": self.bh_anchor,
            "memory": self.memory, "pending_signals": self.pending_signals,
            "open_orders": self.open_orders,
            "last_account_snapshot": self.last_account_snapshot,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 원자적 교체 — 쓰다 죽어도 상태 파일이 깨지지 않게

    # ---------- 편의 ----------
    def to_portfolio(self) -> Portfolio:
        """실험 슬라이스를 표준 Portfolio로(보호분 제외) — 리밸런서·가드레일 재사용용."""
        return self.experiment.to_portfolio()

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
