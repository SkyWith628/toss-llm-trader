"""열린주문 장부 — live 주문의 '전송 이전 영속 + 부분체결 델타 정산'을 담당하는 순수 로직.

[What] 실계좌로 나가는 각 주문을 intent 단위로 추적한다: 안정적 멱등키 생성, 전송 前
       기록(pending), 브로커 응답으로 상태·누적체결 갱신, 이미 반영한 양을 뺀 '델타'만 장부에
       흘려보낼 값으로 계산. I/O·네트워크 0 — runner가 이 결과를 실체결 장부(apply_buy/sell)에 반영.
[Why]  주문을 보낸 직후 프로세스가 죽어도 재시작 때 '같은 멱등키 → 같은 orderId'로 복원돼야
       중복주문이 안 난다. 그래서 키·의도를 전송 '이전'에 영속화하고(write-ahead), 브로커가
       돌려주는 누적 filledQuantity에서 '이미 장부에 반영한 만큼'을 빼 이중 카운트를 막는다.
[How]  intent_id를 dict 키로 하는 열린주문 레코드를 ForwardState.open_orders에 얹는다. 완전체결/
       거부/취소면 terminal — 재시작 복구 대상에서 제외. (EXPERIMENT_PORTFOLIO_SPEC.md §열린 주문.)

주의: 이 모듈은 순수 장부다 — 주문을 보내지도, 파일을 쓰지도 않는다(영속은 state, 전송은 broker).
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.core.types import Order, OrderResult, Side

_TOL = 1e-9

# 열린주문 상태(브로커 상태를 그대로 반영). terminal이면 복구 폴링 대상에서 빠진다.
PENDING = "pending"                    # 전송 이전(write-ahead 기록만 됨)
SUBMITTED = "submitted"                # 전송됐으나 체결 미확정
PARTIALLY_FILLED = "partially_filled"  # 일부 체결(잔량은 계속 열린주문)
FILLED = "filled"
REJECTED = "rejected"
CANCELLED = "cancelled"

_TERMINAL = frozenset({FILLED, REJECTED, CANCELLED})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_terminal(status: str) -> bool:
    """더 이상 체결이 남지 않은 상태 — 재시작 복구 폴링에서 제외."""
    return status in _TERMINAL


def stable_client_order_id(state_id: str, date_iso: str, symbol: str,
                           side: Side, seq: int = 0) -> str:
    """결정론적 멱등키 — 같은 (실험·거래일·종목·방향·순번)이면 재시작 후에도 동일 문자열.

    랜덤 UUID면 재시작 시 새 키가 생겨 브로커가 중복주문을 새 주문으로 접수한다. 결정론적
    키여야 브로커의 clientOrderId dedup이 '같은 의도'를 하나로 묶는다(중복주문 방지의 근간).
    seq: 같은 거래일에 같은 종목·방향 주문이 둘 이상일 때 의도를 구분(주문분할·--force 대비).
    주문 목록은 영속된 pending_signals에서 결정론적으로 재생성되므로 순번도 재시작에 안정적.
    """
    side_v = side.value if isinstance(side, Side) else str(side)
    return f"{state_id}-{date_iso}-{symbol}-{side_v}-{seq}"


def make_intent(
    client_order_id: str, order: Order, *,
    reserved_cash: float = 0.0, reserved_sell_qty: float = 0.0,
) -> dict:
    """전송 '이전'에 영속화할 열린주문 레코드를 만든다(status=pending, 누적체결 0).

    reserved_cash/reserved_sell_qty: 대사(reconcile) 가시성을 위해 기록만 한다. 실제 현금·수량
    이동은 실체결(apply_buy/sell) 시점에만 일어난다 — 여기서 미리 차감하지 않는다(이중 반영 방지).
    """
    now = _now_iso()
    return {
        "intent_id": client_order_id,
        "client_order_id": client_order_id,
        "broker_order_id": "",
        "symbol": order.symbol,
        "side": order.side.value,
        "requested_qty": order.quantity,
        "reason": order.reason,
        "accounted_filled_qty": 0.0,     # 이미 장부에 반영한 누적 체결량
        "accounted_commission": 0.0,     # 이미 장부에 반영한 누적 수수료
        "reserved_cash": reserved_cash,
        "reserved_sell_qty": reserved_sell_qty,
        "status": PENDING,
        "created_at": now,
        "updated_at": now,
    }


def settle_delta(record: dict, result: OrderResult) -> tuple[float, float, float]:
    """브로커 응답으로 레코드를 갱신하고, 아직 장부에 반영 안 된 '델타'만 돌려준다.

    브로커의 filled_qty/commission는 그 주문의 '누적'값이다. 이미 반영한 양(accounted_*)을 빼야
    부분체결이 여러 실행에 걸쳐도 이중 카운트가 안 난다.
    반환: (델타 체결수량, 델타 수수료, 체결가) — runner가 apply_buy/apply_sell에 그대로 넘긴다.
    """
    if result.note:
        record["broker_order_id"] = record["broker_order_id"] or result.note
    record["status"] = result.status
    record["updated_at"] = _now_iso()

    delta_qty = result.filled_qty - record["accounted_filled_qty"]
    delta_commission = result.commission - record["accounted_commission"]
    if delta_qty <= _TOL:           # 새로 체결된 게 없으면 장부 변화 없음
        return 0.0, 0.0, result.fill_price
    record["accounted_filled_qty"] = result.filled_qty
    record["accounted_commission"] = result.commission
    return delta_qty, max(delta_commission, 0.0), result.fill_price


def order_from_record(record: dict) -> Order:
    """열린주문 레코드에서 재조회·재전송에 쓸 Order를 복원(재시작 복구용)."""
    return Order(
        symbol=record["symbol"],
        side=Side(record["side"]),
        quantity=float(record["requested_qty"]),
        reason=record.get("reason", "복구: 재시작 후 미결 주문 재정산"),
    )


def prune_terminal(open_orders: dict[str, dict]) -> dict[str, dict]:
    """완전체결·거부·취소된 레코드를 걷어낸 새 dict(열린주문만 남긴다)."""
    return {k: v for k, v in open_orders.items() if not is_terminal(v.get("status", ""))}
