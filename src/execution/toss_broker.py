"""토스증권 Open API 실거래 브로커 — 검증된 PaperBroker와 '같은 계약'으로.

[What] OAuth2(client_credentials)로 토큰을 받아 REST로 시세·잔고·주문을 처리하는
       BrokerClient 구현. 기본은 dry-run(실주문 차단), live=True일 때만 POST /orders.
[Why]  단계 0~1에서 PaperBroker로 검증한 파이프라인을 '코드 수정 없이' 실계좌로 옮기기
       위해 BrokerClient 계약만 만족한다. 소액 실험이라도 실계좌는 되돌릴 수 없으므로
       안전을 '기본값'으로: read(시세/잔고)는 허용, write(주문)는 명시적 opt-in.
[How]  퀀트 실무에서 집행(execution)을 브로커 뒤로 숨겨 알파/리스크와 분리하는 방식.

스펙: https://openapi.tossinvest.com  (openapi-docs/latest/openapi.json, 2026-08 기준 정합)
  - 인증:  POST /oauth2/token            (form: grant_type/client_id/client_secret)
  - 시세:  GET  /api/v1/prices?symbols=  (콤마 배치, lastPrice=문자열)
  - 잔고:  GET  /api/v1/holdings         (X-Tossinvest-Account 헤더; 현금은 여기 없음)
  - 현금:  GET  /api/v1/buying-power?currency=USD
  - 주문:  POST /api/v1/orders           (side 대문자, 소수점 매수=orderAmount, 매도=quantity)
  - 체결:  GET  /api/v1/orders/{orderId} (생성은 orderId만; 체결은 비동기라 별도 조회)
  * 모든 금액/수량은 '문자열'로 오간다. US 종목 심볼 = 영문 티커(코드 매핑 불필요).

의존성: 표준 라이브러리만(urllib) — 프로젝트의 'stdlib 우선' 기조 유지.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Callable

from src.config import DEFAULT_BUDGET_USD
from src.core.types import Order, OrderResult, Portfolio, Position, Side
from src.execution.broker_base import BrokerClient

# transport: (method, url, headers, body_bytes) -> (status_code, parsed_json)
#   실HTTP는 _urllib_transport가 담당. 테스트는 mock을 주입해 네트워크 없이 검증.
Transport = Callable[[str, str, dict[str, str], bytes | None], "tuple[int, dict]"]

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"

# 주문 상태(OrderStatus) -> 내부 OrderResult.status 로 축약
_FILLED = {"FILLED", "PARTIAL_FILLED"}
_REJECTED = {"REJECTED", "CANCELED", "CANCEL_REJECTED", "REPLACE_REJECTED"}


class TossApiError(RuntimeError):
    """토스 API가 2xx 이외를 반환했을 때. status/메시지를 담는다(시크릿 없음)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict]:
    """표준 라이브러리 HTTP 전송. 4xx/5xx도 예외 대신 (status, json)으로 정규화."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:  # 4xx/5xx
        raw = e.read().decode("utf-8", "replace") or "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"message": raw[:200]}


def _f(x, default: float = 0.0) -> float:
    """토스 응답의 '문자열 숫자'(또는 null)를 float로. None/빈값은 default."""
    if x is None or x == "":
        return default
    return float(x)


def _num(x: float) -> str:
    """수량을 API용 문자열로(소수점 6자리, 불필요한 0 제거). US 소수점은 6자리까지 허용."""
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s or "0"


class TossBroker(BrokerClient):
    """토스증권 Open API 브로커.

    안전 모델:
      - live=False(기본): place_order가 POST를 보내지 않고 'dry-run' 결과만 반환.
      - live=True: 실주문 전송. 단, 예산(budget) 초과 노출은 브로커에서도 재차단(다층 방어).
      - clientOrderId(멱등성 키)로 재요청 시 중복 주문 방지.
      - 시크릿은 로그·에러에 절대 노출하지 않는다.
    """

    name = "toss_broker"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        account_no: str,
        symbols: list[str],
        *,
        budget: float = DEFAULT_BUDGET_USD,
        live: bool = False,
        currency: str = "USD",
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("client_id/client_secret이 비어 있습니다(.env 확인)")
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_no = str(account_no)
        self.symbols = list(symbols)
        self.budget = budget
        self.live = live
        self.currency = currency
        self.base_url = base_url.rstrip("/")
        self._transport: Transport = transport or _urllib_transport

        self._token: str | None = None
        self._token_expiry: float = 0.0  # time.monotonic() 기준 만료 시각

    # ================= 인증 =================
    def _access_token(self) -> str:
        """캐시된 토큰을 반환하되, 만료 60초 전이면 재발급."""
        if self._token and time.monotonic() < self._token_expiry:
            return self._token

        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }).encode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        status, data = self._transport(
            "POST", f"{self.base_url}/oauth2/token", headers, body)
        if status != 200 or "access_token" not in data:
            raise TossApiError(status, data.get("message", "토큰 발급 실패"))

        self._token = data["access_token"]
        # 만료 60초 전에 갱신(네트워크 지연 여유). expires_in 없으면 보수적으로 5분.
        self._token_expiry = time.monotonic() + _f(data.get("expires_in"), 300) - 60
        return self._token

    def _headers(self, with_account: bool = False, json_body: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"}
        if with_account:
            h["X-Tossinvest-Account"] = self._account_no
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _get(self, path: str, *, account: bool = False) -> dict:
        status, data = self._transport("GET", f"{self.base_url}{path}",
                                       self._headers(with_account=account), None)
        if status != 200:
            raise TossApiError(status, data.get("message", f"GET {path} 실패"))
        return data.get("result", data)  # ApiResponse 봉투에서 result 추출

    # ================= BrokerClient 계약 =================
    def get_prices(self) -> dict[str, float]:
        """구성 종목의 현재가를 배치(콤마)로 한 번에 조회. lastPrice(문자열)->float."""
        if not self.symbols:
            return {}
        query = urllib.parse.urlencode({"symbols": ",".join(self.symbols)})
        result = self._get(f"/api/v1/prices?{query}")
        rows = result if isinstance(result, list) else result.get("result", [])
        return {r["symbol"]: _f(r.get("lastPrice")) for r in rows}

    def get_buying_power(self) -> float:
        """현금 기반 매수 가능 금액(현금). Portfolio.cash의 출처."""
        query = urllib.parse.urlencode({"currency": self.currency})
        result = self._get(f"/api/v1/buying-power?{query}", account=True)
        return _f(result.get("cashBuyingPower"))

    def get_portfolio(self) -> Portfolio:
        """보유 종목(holdings) + 현금(buying-power)을 합쳐 Portfolio로 반환."""
        result = self._get("/api/v1/holdings", account=True)
        pf = Portfolio(cash=self.get_buying_power())
        for item in result.get("items", []) or []:
            qty = _f(item.get("quantity"))
            if qty > 0:
                pf.positions[item["symbol"]] = Position(
                    item["symbol"], qty, _f(item.get("averagePurchasePrice")))
        return pf

    def place_order(self, order: Order, when: datetime | None = None) -> OrderResult:
        """시장가 주문 전송. live=False면 실제로 보내지 않고 dry-run 결과만 반환(안전 기본값).

        US 소수점 규칙:
          - 매수(BUY): 금액 기반 orderAmount(달러) 사용 — 소수점 수량은 매수 quantity 불가.
          - 매도(SELL): 수량 기반 quantity 사용 — US MARKET SELL은 소수점 허용(정규장 시간 한정).
        """
        when = when or datetime.now()

        # 현재가(주문 금액 환산 + 예산 방어에 사용)
        try:
            prices = self.get_prices()
        except TossApiError as e:
            return self._reject(order, when, f"시세 조회 실패({e.status})")
        price = prices.get(order.symbol)
        if not price or price <= 0:
            return self._reject(order, when, "시세 없음")

        # --- 다층 방어: 상위 Guardrail을 우회하더라도 예산 초과 노출은 브로커가 재차단 ---
        if order.side is Side.BUY:
            try:
                total = self.get_portfolio().total_value(prices)
                if total > self.budget * 1.001:
                    return self._reject(order, when,
                                        f"예산 초과 노출(브로커 방어): {total:.2f} > {self.budget}")
            except TossApiError:
                if self.live:  # 눈먼 매수 방지
                    return self._reject(order, when, "예산 확인 실패로 매수 보류")

        payload = {
            "symbol": order.symbol,
            "side": order.side.value.upper(),   # BUY | SELL
            "orderType": "MARKET",
            "clientOrderId": uuid.uuid4().hex,  # 멱등성 키(재요청 중복주문 방지)
        }
        if order.side is Side.BUY:
            payload["orderAmount"] = f"{order.quantity * price:.2f}"  # 달러 금액 기반
        else:
            payload["quantity"] = _num(order.quantity)               # 수량 기반(소수점 OK)

        if not self.live:
            return OrderResult(order, 0.0, 0.0, 0.0, "dry-run", when,
                               f"live=False: 미전송 payload={payload}")

        status, data = self._transport(
            "POST", f"{self.base_url}/api/v1/orders",
            self._headers(with_account=True, json_body=True), json.dumps(payload).encode())
        if status not in (200, 201):
            return self._reject(order, when, str(data.get("message", f"주문 거부(status={status})")))
        order_id = str((data.get("result") or {}).get("orderId", ""))
        # 생성 응답엔 체결가가 없다(비동기). orderId만 담아 'submitted'로 반환.
        return OrderResult(order, 0.0, 0.0, 0.0, "submitted", when, order_id)

    # ================= 체결 조회(비동기 주문 정산) =================
    def get_order(self, order_id: str) -> OrderResult:
        """주문 상세를 조회해 체결 결과(OrderResult)로 변환 — 원장 정산에 사용."""
        result = self._get(f"/api/v1/orders/{urllib.parse.quote(order_id)}")
        side = Side.BUY if str(result.get("side", "")).upper() == "BUY" else Side.SELL
        order = Order(result.get("symbol", ""), side, _f(result.get("quantity")))
        raw = str(result.get("status", "")).upper()
        status = "filled" if raw in _FILLED else "rejected" if raw in _REJECTED else "submitted"
        ex = result.get("execution") or {}
        return OrderResult(order, _f(ex.get("filledQuantity")),
                           round(_f(ex.get("averageFilledPrice")), 4),
                           round(_f(ex.get("commission")), 6), status, datetime.now(), order_id)

    # ================= 내부 =================
    @staticmethod
    def _reject(order: Order, when: datetime, note: str) -> OrderResult:
        return OrderResult(order, 0.0, 0.0, 0.0, "rejected", when, note)


def build_toss_broker_from_env(
    symbols: list[str], *, budget: float = DEFAULT_BUDGET_USD, currency: str = "USD",
    transport: Transport | None = None,
) -> TossBroker:
    """.env(환경변수)에서 자격증명을 읽어 TossBroker를 조립.

    필요 변수: TOSS_APP_KEY(=client_id), TOSS_SECRET_KEY(=client_secret),
              TOSS_ACCOUNT_NO(계좌번호), TRADING_MODE(paper|live, 기본 paper).
    실주문(live)은 TRADING_MODE=live 일 때만. main.py의 load_env_file()로 미리 로드해 둘 것.
    """
    import os

    live = os.environ.get("TRADING_MODE", "paper").strip().lower() == "live"
    return TossBroker(
        client_id=os.environ.get("TOSS_APP_KEY", ""),
        client_secret=os.environ.get("TOSS_SECRET_KEY", ""),
        account_no=os.environ.get("TOSS_ACCOUNT_NO", ""),
        symbols=symbols, budget=budget, live=live, currency=currency, transport=transport,
    )
