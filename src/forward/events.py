"""외부 계좌 사건 감지 — '실험이 설명하지 못하는 계좌 변화'를 잡아 halt한다(증분5).

[What] 지난 실행 종료 시점의 계좌 보유 스냅샷과 이번 실행 시작 시점의 보유를 비교해, 그 변화
       중 '실험 자신의 체결'로 설명되지 않는 잔여(residual)를 찾는다. 잔여가 있으면 수동매매·
       기업행동(분할·병합·합병)·배당 재투자 등 외부 사건으로 보고 자동매매를 중단한다.
[Why]  실행과 실행 사이에는 실험이 아무 주문도 내지 않는다(하루 1회). 그 사이 계좌 보유가
       바뀌었다면 원인은 (a)직전 실행이 남긴 열린주문의 지연 체결 — 이건 실험이 설명 가능 —
       아니면 (b)외부 사건뿐이다. 외부 사건은 귀속을 안전하게 계산할 수 없으므로, 추측해서
       매매를 이어가면 보호분을 침범하거나 성과를 오염시킨다. 그래서 fail-closed로 멈춘다.
[How]  residual[sym] = (current - prev) - experiment_delta. |residual|>tol이면 사건. 부호로 종류
       구분(감소=수동매도/출금, 증가=수동매수/기업행동). 이 모듈은 순수 판정만 — halt·영속은
       호출자(runner)가 한다. (EXPERIMENT_PORTFOLIO_SPEC.md §추가로 처리해야 할 사건.)

주의: 현금성 사건(배당금 입금·환전)은 계좌 현금을 별도 조회해야 잡히므로 이 감지 범위 밖이다
      (브로커 보유 조회만 사용). 수량으로 드러나는 사건(수동매매·분할·배당 '재투자')만 다룬다.
"""
from __future__ import annotations

MANUAL_SELL = "manual_sell_or_withdrawal"          # 보유가 실험 매도보다 더 줄어듦
MANUAL_BUY = "manual_buy_or_corporate_action"      # 보유가 실험 매수보다 더 늘어남(수동매수/분할/합병)


def detect_events(
    prev_snapshot: dict[str, float],
    current_holdings: dict[str, float],
    experiment_delta: dict[str, float],
    *, tol: float = 1e-6,
) -> list[dict]:
    """실험이 설명하지 못하는 계좌 보유 변화(외부 사건) 목록을 돌려준다. 없으면 빈 리스트.

    prev_snapshot:    지난 실행 종료 시 관측한 계좌 보유(비교 기준선).
    current_holdings: 이번 실행 시작 시 관측한 계좌 보유.
    experiment_delta: 그 사이 실험 자신이 만든 순 수량 변화(매수 +, 매도 −). 보통 직전 실행이
                      남긴 열린주문의 지연 체결분(재시작 복구에서 정산된 값).
    """
    events: list[dict] = []
    symbols = set(prev_snapshot) | set(current_holdings) | set(experiment_delta)
    for sym in sorted(symbols):
        prev = prev_snapshot.get(sym, 0.0)
        curr = current_holdings.get(sym, 0.0)
        exp_d = experiment_delta.get(sym, 0.0)
        residual = (curr - prev) - exp_d          # 실험으로 설명 안 되는 잔여 변화
        if abs(residual) <= tol:
            continue
        events.append({
            "symbol": sym,
            "kind": MANUAL_SELL if residual < 0 else MANUAL_BUY,
            "prev": round(prev, 6), "current": round(curr, 6),
            "experiment_delta": round(exp_d, 6), "residual": round(residual, 6),
        })
    return events


def summarize(events: list[dict]) -> str:
    """halt 사유로 쓸 한 줄 요약(사람이 원인을 바로 알아보게)."""
    parts = [
        f"{e['symbol']} {e['kind']}(관측 {e['current']} vs 기대 "
        f"{round(e['prev'] + e['experiment_delta'], 6)}, 잔여 {e['residual']:+})"
        for e in events
    ]
    return "; ".join(parts)
