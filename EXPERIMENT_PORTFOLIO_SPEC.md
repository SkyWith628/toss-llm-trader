# $100 독립 실험 포트폴리오 및 수익 재투자 명세

## 목적

토스증권 실계좌에 이미 보유 중인 주식과 기존 현금은 건드리지 않고, 새로 투입한 **$100만으로 자동매매 실험**을 운영한다.

실험에서 발생한 실현·미실현 수익은 다음 리밸런싱에 재투자한다. 반대로 손실이 발생하면 투자 가능 금액도 함께 감소해야 하며, 계좌의 기존 자금으로 손실을 보충해서는 안 된다.

이 문서는 Claude가 현재 프로젝트를 수정할 때 따라야 할 구현 명세다.

## 핵심 원칙

1. 기존 보유 포지션은 절대 자동 매도하지 않는다.
2. 실험 시작 후 매수한 수량만 자동매도할 수 있다.
3. 실험이 사용할 수 있는 최초 현금은 정확히 $100이다.
4. 수익은 실험 자산으로 귀속하고 자동으로 재투자한다.
5. 손실이 나면 실험 투자 가능 금액도 감소한다.
6. 계좌 전체 현금이나 전체 포지션을 전략 자산으로 사용하지 않는다.
7. 실험 장부와 실제 계좌가 불일치하면 신규 주문을 중단한다.
8. 공매도, 마진, 레버리지 및 실험 NAV를 초과하는 주문은 허용하지 않는다.

## 권장 운영 방식

가능하면 별도의 토스증권 계좌를 만들고 해당 계좌의 `accountSeq`만 사용하는 것이 가장 안전하다.

별도 계좌를 사용할 수 없다면 같은 실계좌 안에서 **가상 서브포트폴리오 장부(Experiment Ledger)**를 유지해야 한다. 증권사 계좌에서는 기존 보유분과 실험 보유분이 물리적으로 분리되지 않으므로, 애플리케이션이 귀속 수량과 현금을 별도로 추적해야 한다.

## 최초 초기화

실험 시작 전에 실제 계좌의 포지션을 조회하고 이를 보호 기준선으로 저장한다.

예시:

```json
{
  "initial_capital": 100.0,
  "experiment_cash": 100.0,
  "protected_positions": {
    "AAPL": 10.0,
    "NVDA": 1.5
  },
  "experiment_positions": {},
  "realized_pnl": 0.0,
  "fees_paid": 0.0
}
```

- `protected_positions`: 실험 시작 전에 계좌가 보유하던 수량이다. 자동매매가 매도할 수 없다.
- `experiment_positions`: 실험 시작 후 매수하여 실험에 귀속된 수량이다.
- `experiment_cash`: 실험에 귀속된 사용 가능 현금이다.
- 사용자가 명시적으로 확인하기 전에는 보호 기준선을 다시 계산하거나 덮어쓰면 안 된다.

## 동일 종목을 기존 계좌와 실험이 함께 보유하는 경우

기존 AAPL 10주가 있고 실험이 AAPL 0.3주를 매수했다면 다음처럼 처리한다.

```text
실제 계좌 AAPL 수량: 10.3주
보호 기준선:          10.0주
실험 귀속 수량:        0.3주
자동매도 가능 수량:     최대 0.3주
```

전략이 AAPL 목표 비중을 0으로 결정해도 0.3주만 매도해야 한다. 기존 10주는 어떤 경우에도 주문 수량에 포함하지 않는다.

다음 불변식을 주문 직전에 검사한다.

```text
실제 계좌 수량 >= 보호 수량 + 실험 장부 수량
매도 주문 수량 <= 실험 장부 수량
```

불변식이 깨지면 주문을 보내지 말고 상태를 `reconciliation_required`로 전환한다.

## 실험 순자산과 수익 재투자

매 거래 시점의 실험 순자산(NAV)은 다음과 같다.

```python
experiment_nav = experiment_cash + sum(
    experiment_quantity[symbol] * current_price[symbol]
    for symbol in experiment_positions
)
```

리밸런싱 목표 금액은 최초 원금 `$100`이 아니라 현재 `experiment_nav`를 기준으로 계산한다.

```python
target_value = target_weight * experiment_nav
current_value = experiment_quantity * current_price
order_value = target_value - current_value
```

예시:

```text
최초 원금:             $100
현재 누적 손익:          +$5
현재 실험 NAV:          $105
AAPL 목표 비중:           60%
AAPL 목표 평가액:         $63
현금 목표액:              $42
```

따라서 실현이익과 미실현이익 모두 다음 리밸런싱에 복리로 반영된다. NAV가 $90으로 감소했다면 다음 리밸런싱도 $90만을 기준으로 해야 한다.

## 매도 후 현금 처리

실험 포지션을 매도하면 다음 금액만 실험 현금으로 반영한다.

```python
net_proceeds = filled_quantity * fill_price - commission - taxes - other_costs
experiment_cash += net_proceeds
```

매도 후 발생한 현금과 이익은 다음 리밸런싱에서 재사용할 수 있다. 계좌 전체 매수 가능 금액을 `experiment_cash`로 간주하면 안 된다.

## 매수 가능 금액 계산

매수 수량은 슬리피지와 수수료를 포함하여 계산해야 한다. 현재가만으로 수량을 계산하면 목표 비중 합이 100%일 때 실제 체결 비용이 현금을 초과할 수 있다.

개념식:

```python
estimated_fill_price = market_price * (1 + estimated_slippage)
available_notional = max(0, experiment_cash - estimated_fixed_fee)
max_quantity = available_notional / (
    estimated_fill_price * (1 + estimated_commission_rate)
)
order_quantity = min(target_quantity_delta, max_quantity)
```

실제 주문 전에는 최소한 다음을 보장해야 한다.

```text
예상 주문비용 <= experiment_cash
주문 후 예상 총 주식 노출 <= experiment_nav
목표 비중 합계 <= 1.0
```

## 현재 Guardrail에서 변경할 사항

현재 프로젝트는 총자산이 최초 예산 `$100`을 넘으면 매수를 막는 구조를 가지고 있다. 이 검사는 수익 재투자를 차단하므로 제거하거나 의미를 변경해야 한다.

잘못된 기준:

```text
현재 총자산 > 최초 예산 $100이면 매수 차단
```

필요한 기준:

```text
매수 비용 > experiment_cash이면 차단
주문 후 실험 노출 > experiment_nav이면 차단
매도 수량 > experiment_position.quantity이면 차단
실제 계좌와 보호/실험 장부가 일치하지 않으면 차단
```

평가이익으로 NAV가 $100을 초과하는 것은 정상이다. 외부 자금을 추가로 사용하지 않는 한 해당 수익은 재투자할 수 있어야 한다.

## 실제 체결 기반 장부 갱신

장부는 주문 요청값이 아니라 실제 체결 결과를 기준으로 갱신한다.

- `submitted` 상태에서는 잔고를 확정 변경하지 않는다.
- 부분 체결이면 `filledQuantity`만 반영한다.
- 체결 수수료와 실제 평균 체결가를 반영한다.
- 미체결 잔량은 열린 주문으로 관리한다.
- 타임아웃 후 주문을 재시도할 때 동일한 `clientOrderId`를 재사용한다.
- 주문 상태를 알 수 없으면 새 주문을 보내기 전에 기존 주문부터 조회한다.

## 계좌 대사(Reconciliation)

매 실행 시작 전과 주문 체결 후 실제 계좌를 다시 조회한다.

검사 항목:

```text
실제 보유량 >= protected_quantity + experiment_quantity
실험 현금 장부가 음수가 아님
열린 주문과 장부의 예약 현금/수량이 일치함
실험에서 기록한 체결이 실제 주문 내역에 존재함
```

사용자가 토스 앱에서 동일 종목을 직접 매매하거나 환전·출금하면 자동으로 귀속을 추측하지 않는다. 불일치 로그를 남기고 주문을 중단한 뒤 사용자 확인을 요구한다.

## 추가로 처리해야 할 사건

- 매수·매도 수수료
- 환전 비용과 환율 손익
- 부분 체결과 미체결
- 배당금 중 실험 수량에 귀속되는 부분
- 주식분할, 병합, 합병, 종목 변경
- 세금과 기타 제비용
- 사용자의 수동 매매
- API 응답 실패 및 네트워크 타임아웃

기업행동이나 사용자 수동 거래가 감지됐는데 안전하게 귀속을 계산할 수 없다면 자동 주문을 중단한다.

## 권장 데이터 모델

```python
@dataclass
class ExperimentPosition:
    symbol: str
    quantity: float
    avg_price: float


@dataclass
class ProtectedPortfolio:
    initial_capital: float
    cash: float
    protected_positions: dict[str, float]
    experiment_positions: dict[str, ExperimentPosition]
    realized_pnl: float
    fees_paid: float
    reserved_cash: float
    reserved_sell_qty: dict[str, float]
    status: str  # active | reconciliation_required | halted
```

금액 계산에는 가능하면 이진 부동소수점 `float` 대신 통화·수량 정밀도에 맞춘 `Decimal` 사용을 검토한다.

## 현재 프로젝트에서 예상되는 수정 위치

- `src/forward/state.py`
  - 보호 포지션, 실험 현금, 실험 포지션, 열린 주문을 영속화한다.
- `src/execution/rebalance.py`
  - 계좌 전체가 아닌 실험 NAV를 기준으로 주문을 계산한다.
  - 슬리피지와 수수료를 반영해 매수 수량을 제한한다.
- `src/risk/guardrail.py`
  - 최초 `$100` 고정 상한을 제거한다.
  - 실험 현금, 실험 NAV, 실험 귀속 매도 수량을 검사한다.
- `src/execution/toss_broker.py`
  - 주문 체결 조회와 부분 체결 정산을 강화한다.
  - 멱등 키를 주문 재시도 사이에 영속적으로 재사용한다.
- `src/forward/runner.py`
  - 주문 전후 계좌 대사를 수행한다.
  - 상태와 체결 로그를 복구 가능한 방식으로 저장한다.

## 필수 테스트

1. 기존 AAPL 10주가 있어도 실험의 최초 포지션은 0주여야 한다.
2. 실험이 AAPL 0.3주를 산 뒤 목표 비중을 0으로 바꾸면 정확히 0.3주만 매도해야 한다.
3. 기존 AAPL 10주는 모든 자동매매 과정 후에도 최소 10주가 남아야 한다.
4. $100이 $105가 되면 다음 리밸런싱은 $105를 기준으로 해야 한다.
5. $100이 $90이 되면 계좌의 다른 현금을 사용하지 않고 $90만 운용해야 한다.
6. 목표 비중 합계가 100%여도 슬리피지 때문에 정상 주문이 거부되지 않아야 한다.
7. 예상 수수료까지 포함한 주문 비용이 실험 현금을 넘으면 수량을 축소하거나 차단해야 한다.
8. 실험 보유량보다 많은 매도는 반드시 차단해야 한다.
9. 실제 계좌 수량이 보호 수량과 실험 수량의 합보다 작으면 모든 신규 주문을 중단해야 한다.
10. 부분 체결 시 실제 체결 수량만 장부에 반영해야 한다.
11. 네트워크 타임아웃 후 재시도해도 중복 주문이 발생하지 않아야 한다.
12. 사용자의 수동 거래로 대사 불일치가 발생하면 자동으로 기준선을 변경하지 않아야 한다.

## 완료 조건

다음 조건을 모두 만족해야 실거래 모드를 허용한다.

- 보호 포지션이 주문 생성 과정에서 완전히 제외된다.
- 실험 포지션 이상으로 매도할 수 없다.
- 실험 NAV 기준 복리 재투자가 작동한다.
- 외부 현금이 실험에 유입되지 않는다.
- 슬리피지와 수수료를 포함한 주문 가능 금액 검사가 있다.
- 실제 체결 기반으로 장부가 갱신된다.
- 주문 전후 계좌 대사와 fail-closed 동작이 테스트된다.
- 재시도 멱등성과 부분 체결이 테스트된다.
- 위 필수 테스트가 모두 자동화되어 통과한다.

## Claude에게 요청할 작업

현재 코드를 먼저 읽고 위 명세와 충돌하는 지점을 목록화한다. 이후 작은 단위로 구현하며 각 단계마다 테스트를 추가한다. 기존 실거래 포지션을 보호하는 기능이 완성되고 대사 테스트가 통과하기 전까지 `TRADING_MODE=live`를 활성화하지 않는다.

---

## 구현 현황 (Claude 작업 결과 — Codex 재검토용)

작은 단위로 진행 중. **실주문·live 배선은 아직 없음**(TossBroker 미변경, `TRADING_MODE=paper` 기본). 아래는 명세 대비 현재 상태.

### 완료 (증분1 + 선행 수정)

| 명세 항목 | 구현 | 위치 |
|---|---|---|
| 슬리피지 반영 매수 사이징(§매수가능금액) | 매수를 가용현금에 클립(슬리피지 여유) | `src/execution/rebalance.py` |
| 고정 $100 상한 제거 → NAV 복리(§가드레일 변경) | `total>budget` 차단 제거, 현금 한도(무레버리지)만 유지 | `src/risk/guardrail.py` |
| 보호분/실험분 분리 장부 · NAV · 실현손익 | `ExperimentPortfolio`(protected/experiment/cash/realized_pnl/fees/status), `to_portfolio()`로 실험 슬라이스만 투영해 리밸런서·가드레일 재사용 | `src/forward/experiment_portfolio.py` |
| 계좌 대사 + fail-closed(§계좌 대사) | `reconcile()`: 실계좌≥보호+실험·현금≥0, 위반 시 `reconciliation_required`로 전환·주문 중단(기준선 자동변경 금지) | `src/forward/reconcile.py` |

**핵심 보증**: `to_portfolio()`가 보호분을 담지 않으므로, 리밸런서·가드레일이 **보호분을 주문 대상에 넣는 것이 구조적으로 불가능**하다. 매도 한도 = 실험 귀속 수량뿐.

### 테스트 커버(명세 §필수 테스트)

`tests/test_carveout.py` — 1(실험 최초 0주), 2·8(실험분만 매도/초과 차단), 3(보호분 불변), 4·5(NAV 기준 리밸런싱), 9·12(대사 실패 시 fail-closed·기준선 유지), 초과체결 fail-closed, 직렬화 왕복.
`tests/test_smoke.py` — 6(슬리피지 포함 주문가능액), NAV>초기자본 재투자 허용.
**테스트 7 완성(증분3a)**: 공통 `CostModel`(슬리피지+비례수수료+고정수수료)을 리밸런서·브로커가 공유.
전체(`test_smoke/llm_agent/toss_broker/forward/carveout`) 통과 — Windows/PowerShell 포함(임시경로 수정 후).

### 미완료 (다음 증분)

- ✅ **증분2 완료**: `state.py`가 `ExperimentPortfolio`를 임베드(페이퍼=`protected={}`),
  `runner.py`가 실체결(apply_buy/sell)로 장부 갱신 + NAV·realized_pnl·fees·status 추적.
  원장에 실험/보호 포지션 구분 노출(미귀속분은 실계좌 연결 시 추가). 페이퍼/실거래 단일 모델.
- ✅ **증분3a 완료**: `CarveOutBroker`(`forward/carveout_broker.py`) — 실계좌 시세·보유 조회,
  **실험 슬라이스만 보고**, experiment_cash/보유 기준 다층 방어, 멱등키로 전송, `get_order`
  폴링으로 **실체결 정산(부분체결=filledQuantity만)**. 공통 `CostModel`(슬리피지+수수료)을
  리밸런서·브로커가 공유(테스트7 완성). TossBroker에 `client_order_id`·`enforce_budget=False`
  추가(계좌전체 방어는 카브아웃 계층으로 이전). mock 단위테스트 7개(부분체결·멱등·방어·dry-run).
- **증분3b(live 배선)**: runner의 live 경로 + 주문 전후 `reconcile` + 멱등키/열린주문 **state 영속**
  + 상태미상 시 기존주문 조회. 필수테스트 10·11(부분체결·타임아웃 재시도)의 통합 검증.
- **증분4**: 실계좌 보호분 **기준선 초기화**(사용자 명시 확인) + runner 주문 전후 대사.
- **증분5**: 기업행동·배당·수동매매 감지 시 halt(§추가 처리 사건).
- **미도입**: `reserved_cash`/`reserved_sell_qty`/open orders, `Decimal` 금액(현재 float).

### Codex에게 검토 요청 포인트

1. `to_portfolio()` 투영으로 기존 리밸런서·가드레일을 실험 한정 재사용하는 접근이 명세 불변식을 충분히 만족하는가(특히 §동일 종목 보유 케이스).
2. 동일종목 대사의 취약성 — **실험 유니버스를 개인 보유와 disjoint하게** 강제하는 옵션을 1급으로 둘지(대사 복잡성 대폭 감소).
3. 멱등키 영속 방식·체결 정산 타이밍(증분3) 설계 제안.
4. `Decimal` 전환의 비용/효익(현재 `core/types.py`가 float 광범위 사용).
5. 대사 실패 후 복구 절차(사용자 확인→기준선 재설정)의 UX/안전 프로토콜.

> live 활성화 조건(명세 §완료 조건)은 아직 미충족 — 증분3~5와 대사·멱등·부분체결 테스트 통과가 선행되어야 한다.

---

## Codex 재검토 피드백 (2026-08-12)

### 총평

현재 변경은 **증분 1로서는 합격**이다. 보호분을 `to_portfolio()`에서 제외하고 실험 슬라이스만 기존 리밸런서·가드레일에 투영하는 구조, NAV 기반 복리 재투자, 계좌 대사 실패 시 기준선을 자동 변경하지 않고 중단하는 방향은 적절하다.

그러나 아직 Forward runner 및 실계좌 주문 경로에 통합되지 않았으므로, 현재 상태에서 기존 실계좌 포지션 보호를 실제로 보장하거나 `TRADING_MODE=live`를 활성화할 수는 없다.

### [P1] 초과 매도 체결을 조용히 축소 기록하면 안 됨

현재 `ExperimentPortfolio.apply_sell()`은 다음과 같이 실제 체결 수량을 실험 보유량으로 잘라낸다.

```python
qty = min(qty, ep.quantity)
```

예를 들어 실험 장부에는 AAPL 0.3주가 있는데 실제 체결 결과가 0.4주라면, 장부에는 0.3주만 매도된 것으로 기록된다. 실제로는 보호 포지션 0.1주가 침범됐지만 그 사실이 숨겨진다.

실제 체결량이 실험 귀속 수량을 초과하면 수량을 조정하지 말고 즉시 fail-closed 해야 한다.

```python
if qty > ep.quantity + tolerance:
    self.require_reconciliation("실험 수량 초과 체결")
    raise ExperimentLedgerError(...)
```

장부 반영 함수는 주문을 보정하는 계층이 아니라 실제 체결 결과가 불변식을 지키는지 검증하는 계층이어야 한다.

### [P1] TossBroker의 고정 `$100` 방어 제거 또는 책임 이동 필요

`TossBroker.place_order()`에는 계좌 전체 포트폴리오 가치가 `budget`보다 크면 매수를 거부하는 로직이 남아 있다.

기존 자산이 있는 공유 계좌는 처음부터 총자산이 `$100`보다 크므로, 향후 `CarveOutBroker`를 연결하더라도 정상적인 실험 매수가 모두 차단될 수 있다.

실계좌 공유 환경의 한도 검사는 다음 값으로 상위 카브아웃 계층에서 수행해야 한다.

- `ExperimentPortfolio.cash`
- 실험 NAV
- 예약 현금
- 열린 매도 주문의 예약 수량

TossBroker는 계좌 전체 자산을 실험 예산과 비교해서는 안 된다.

### [P1] Forward의 사용자 지정 슬리피지를 리밸런서에도 전달해야 함

`forward/runner.py`는 `cfg.slippage`를 `PaperBroker`에 전달하지만 `signals_to_orders()`에는 전달하지 않는다. 설정값이 기본값과 다르면 리밸런서의 예상 비용과 브로커의 실제 체결 비용이 달라져 매수가 다시 거부될 수 있다.

```python
orders = signals_to_orders(
    signals,
    broker.get_portfolio(),
    prices,
    cfg.min_trade,
    slippage=cfg.slippage,
)
```

리밸런서와 브로커는 반드시 동일한 비용 설정을 사용해야 한다.

### [P1] 리밸런서의 수수료 반영 및 테스트 필요

현재 리밸런서는 슬리피지만 고려하고 고정 수수료·비례 수수료를 고려하지 않는다. 따라서 필수 테스트 7인 “예상 수수료까지 포함한 주문 비용 검사”는 아직 완료되지 않았다.

문서의 “테스트 6·7 완료” 표기는 “슬리피지 테스트 완료, 수수료 테스트 미완료”로 수정해야 한다.

권장 방식:

1. 수수료·슬리피지 계산을 공통 비용 모델로 분리한다.
2. 리밸런서와 브로커가 동일한 모델을 사용한다.
3. 고정 수수료와 비례 수수료가 모두 있는 경우에도 두 건 이상의 매수가 정상 체결되는지 테스트한다.
4. 예상 비용이 실험 현금을 넘으면 주문 수량을 축소하거나 차단한다.

### [P2] 중단 사유를 상태에 영속화해야 함

현재 `_halt_reason`은 dataclass 필드가 아니므로 `to_dict()`와 `from_dict()` 왕복에서 사라진다. 재시작 후에는 `reconciliation_required` 상태만 남고 원인을 확인할 수 없다.

다음과 같은 영속 필드를 권장한다.

```python
status: str = ACTIVE
status_reason: str = ""
status_changed_at: str = ""
```

직렬화 왕복 테스트에서 중단 상태와 사유가 모두 보존되는지 확인해야 한다.

### [P2] 초과 보유량의 귀속을 운영 화면과 로그에 표시

대사 로직이 다음 조건을 허용하는 것은 안전 측면에서 합리적이다.

```text
실제 계좌 수량 > 보호 수량 + 실험 수량
```

이는 사용자가 같은 종목을 직접 추가 매수한 경우에도 추가 수량을 자동매도 대상으로 편입하지 않게 한다. 다만 운영 로그에는 다음 세 값을 구분해 표시해야 한다.

```text
보호 기준선
실험 귀속 수량
귀속되지 않은 추가 수량
```

귀속되지 않은 수량을 실험 장부나 보호 기준선에 자동 편입해서는 안 된다.

### [P2] Windows 테스트 실패 수정 필요

`tests/test_llm_agent.py`가 고정 경로 `/tmp/_llm_test.jsonl`을 사용하여 Windows에서 다음 오류로 실패한다.

```text
PermissionError: /tmp/_llm_test.jsonl
```

`tempfile.TemporaryDirectory()` 또는 pytest의 `tmp_path` fixture로 변경해야 한다. 현재 확인 결과:

- `test_carveout.py`: 통과
- `test_forward.py`: 통과
- `test_smoke.py`: 통과
- `test_toss_broker.py`: 통과
- `test_llm_agent.py`: Windows 임시 경로 문제로 실패

따라서 현시점에서 “전체 테스트 통과”라고 표시하면 안 된다.

### 이전 리뷰에서 아직 남아 있는 별도 문제

백테스트와 Forward 러너는 당일 종가를 포함한 스냅샷으로 신호를 만든 직후 같은 당일 종가를 기준으로 체결한다. 미래 날짜 행을 보지 않는 것만으로는 충분하지 않다. 종가 확정 후 같은 종가 체결은 실현 불가능한 시점 가정이다.

다음 중 하나로 수정해야 한다.

- `t-1`까지의 데이터로 신호를 만들고 `t` 시가에 체결
- `t` 종가로 신호를 만들고 `t+1` 가격에 체결

이 문제는 계좌 보호 기능과 별개지만 전략 성과를 신뢰하기 위한 필수 수정이다.

### 권장 다음 구현 순서

1. `apply_sell()`의 초과 체결을 fail-closed 처리한다.
2. 슬리피지와 수수료를 공통 비용 모델로 통합한다.
3. `cfg.slippage` 및 수수료 설정을 리밸런서와 브로커에 동일하게 전달한다.
4. `ExperimentPortfolio`를 `ForwardState`와 `forward/runner.py`에 연결한다.
5. 중단 사유와 변경 시각을 영속화한다.
6. Windows 테스트를 수정하고 전체 테스트를 한 명령으로 검증한다.
7. `CarveOutBroker`, 예약 현금, 예약 매도 수량, 열린 주문 장부를 구현한다.
8. 부분 체결과 초과 체결 테스트를 추가한다.
9. 멱등 키를 영속화하고 타임아웃 재시도 중복 주문 테스트를 추가한다.
10. 보호 기준선 초기화·사용자 확인·주문 전후 대사를 연결한다.
11. 당일 종가 시점 누수를 제거한다.
12. 모든 완료 조건이 충족된 뒤에만 live 경로를 활성화한다.

### 현재 완료도

```text
순수 실험 장부 모델       완료
NAV 복리 재투자           완료
보호분 제외 리밸런싱       단위 로직 완료
계좌 대사 순수 함수        완료
Forward 상태 통합          미완료
실제 계좌 기준선 초기화     미완료
실주문 전후 대사           미완료
부분 체결 안전 정산        미완료
멱등 주문                  미완료
예약 현금·예약 수량         미완료
수수료 포함 사이징          미완료
시점 누수 없는 성과 측정     미완료
live 안전 운용             불가
```

---

## Codex 2차 재검토 피드백 (2026-08-12)

### 총평

이전 재검토에서 지적한 항목 대부분이 정상적으로 반영됐다. 전체 테스트 파일과 `compileall`도 현재 환경에서 모두 통과했다.

특히 다음 변경은 적절하다.

- 초과 매도 체결 시 fail-closed
- 상태 중단 사유와 시각 영속화
- 공통 `CostModel`을 이용한 슬리피지·수수료 반영
- Forward runner에 `cfg.slippage` 전달
- 고정 `$100` 상한 제거 및 실험 NAV 복리 재투자
- `ExperimentPortfolio`와 `ForwardState` 통합
- 직전 거래일 결정의 next-bar 집행으로 당일 종가 시점 누수 제거
- Windows 비호환 임시 테스트 경로 수정
- TossBroker에 외부 `client_order_id` 전달 지원
- 공유 계좌에서 계좌 전체 예산 검사를 끌 수 있는 `enforce_budget=False` 추가
- 보호 포지션과 실험 포지션의 구조적 분리

그러나 **부분 체결, 열린 주문 영속성, 실주문 조회, 프로세스 재시작 복구**가 아직 완성되지 않았으므로 live 활성화 조건은 여전히 충족되지 않는다.

### [P1] 부분 체결을 최종 체결로 종료하면 안 됨

현재 Toss 주문 상태 매핑에서 `PARTIAL_FILLED`가 내부 `filled`로 정규화되고, `CarveOutBroker._settle()`은 `filled`를 종결 상태로 판단해 폴링을 끝낸다.

예시:

```text
주문 수량:             1.0주
현재 체결 수량:         0.4주
실제 열린 주문 잔량:     0.6주
현재 내부 반환 상태:     filled
```

이 경우 0.4주만 장부에 반영된 뒤 주문이 끝난 것으로 처리되지만, 실제 계좌에서는 0.6주가 계속 체결될 수 있다. 예약 현금·예약 매도 수량을 해제하거나 새 주문을 보내면 실험 한도를 초과할 수 있다.

내부 주문 상태를 최소한 다음처럼 분리해야 한다.

```text
submitted
partially_filled
filled
rejected
canceled
```

부분 체결 처리 요구사항:

1. `filledQuantity`는 누적값인지 증분값인지 API 계약을 기준으로 명확히 처리한다.
2. 이미 장부에 반영한 수량을 `accounted_filled_qty`로 영속화한다.
3. 새로 증가한 체결분만 장부에 반영한다.
4. 잔량에 해당하는 현금 또는 매도 수량을 계속 예약한다.
5. 완전체결·취소·거부 전까지 열린 주문으로 유지한다.
6. 프로세스 재시작 후에도 동일 주문 ID를 다시 조회해 정산을 이어간다.

현재 `test_partial_fill_reports_filled_quantity_only`는 절반만 체결됐는데도 `status == "filled"`를 정상으로 기대하므로 수정이 필요하다.

### [P1] 주문 상세 조회에 계좌 헤더 필요

현재 `TossBroker.get_order()`는 다음과 같이 주문 상세를 조회한다.

```python
result = self._get(f"/api/v1/orders/{urllib.parse.quote(order_id)}")
```

토스 주문·주문 조회 API는 `X-Tossinvest-Account` 계좌 헤더가 필요한 범주이므로 다음과 같이 호출해야 한다.

```python
result = self._get(
    f"/api/v1/orders/{urllib.parse.quote(order_id)}",
    account=True,
)
```

테스트 Fake transport도 주문 상세 조회 시 계좌 헤더 존재 여부를 검증해야 한다. 현재 테스트는 URL과 응답만 검사하므로 이 누락을 잡지 못한다.

### [P1] 실제 매수 체결 비용이 실험 현금을 넘을 때 fail-closed 필요

`ExperimentPortfolio.apply_buy()`는 실제 체결가와 수수료를 장부에 반영하지만, 차감 후 실험 현금이 음수가 되어도 상태가 `active`로 유지된다.

주문 전 예상 가격과 실제 체결 가격은 다를 수 있다.

```text
주문 전 예상 비용 <= 실험 현금
실제 체결 비용     > 실험 현금
```

장부는 실제 체결 비용을 줄여 기록해서는 안 된다. 실제 값을 모두 기록한 뒤 즉시 대사 필요 상태로 전환해야 한다.

```python
actual_cost = qty * fill_price + commission
self.cash -= actual_cost

if self.cash < -tolerance:
    self.require_reconciliation("실제 매수 체결 비용이 실험 현금 초과")
    raise ExperimentLedgerError(...)
```

필수 테스트:

- 주문 전 가격 기준으로는 허용되지만 실제 체결가 상승으로 현금이 음수가 되는 경우
- 실제 수수료가 예상 수수료보다 커 현금이 음수가 되는 경우
- 오류 후 `status == reconciliation_required`
- 오류 사유와 시각의 직렬화 왕복

### [P1] 멱등 키·주문 ID·누적 체결량을 영속화해야 함

`CarveOutBroker`가 외부 `client_order_id`를 받고 TossBroker가 이를 그대로 전송하도록 변경한 것은 올바른 기반이다. 그러나 현재 키와 주문 상태가 `ForwardState`에 저장되지 않는다.

위험 시나리오:

```text
1. 주문을 토스에 전송
2. 토스에서 주문 접수 또는 체결
3. state.json 저장 전에 프로세스 종료
4. 다음 실행은 기존 clientOrderId와 orderId를 알지 못함
5. 새 주문을 전송하여 중복 주문 가능
```

동일 프로세스에서 같은 문자열을 두 번 전달하는 테스트만으로는 충분하지 않다. 프로세스를 재시작하고 상태 파일에서 동일 주문을 복원하는 테스트가 필요하다.

권장 열린 주문 모델:

```json
{
  "open_orders": {
    "intent-id": {
      "client_order_id": "stable-key",
      "broker_order_id": "order-id",
      "symbol": "AAPL",
      "side": "buy",
      "requested_qty": 1.0,
      "accounted_filled_qty": 0.4,
      "reserved_cash": 6.1,
      "reserved_sell_qty": 0.0,
      "status": "partially_filled",
      "created_at": "...",
      "updated_at": "..."
    }
  }
}
```

주문 의도와 멱등 키를 주문 전송 **이전**에 영속화해야 한다. 전송 후 저장하는 구조는 여전히 충돌 창이 있다.

### [P1] 기존 Forward 상태 형식에 대한 버전·마이그레이션 필요

현재 `ForwardState.load_or_init()`은 기존 상태 파일에 `experiment` 필드가 있다고 가정한다. 이전 버전의 상태는 `cash`와 `positions` 형식이므로 `KeyError`가 발생할 수 있다.

권장 방식:

```json
{
  "schema_version": 2,
  "experiment": {},
  "open_orders": {}
}
```

로드 정책:

1. `schema_version`이 없으면 구버전으로 판정한다.
2. 페이퍼 상태는 명시적인 변환 함수를 통해 마이그레이션할 수 있다.
3. 실계좌 보호 기준선은 자동 추정하지 않는다.
4. 실계좌 상태 마이그레이션은 사용자 확인 또는 새로운 `state_dir`을 요구한다.
5. 지원하지 않는 미래 버전은 친절한 오류와 함께 fail-closed 한다.

### [P2] Forward 상태와 equity 로그의 원자성 문제

현재 runner는 `state.json`을 먼저 저장한 후 `equity.jsonl`을 append한다. 두 작업 사이에 프로세스가 종료되면 상태에는 해당 날짜가 실행 완료로 기록되지만 성과 로그에는 행이 없다. 재실행은 같은 날짜 중복 방지에 걸려 skip된다.

실주문과 열린 주문까지 연결되면 단순 JSON 파일 두 개의 쓰기 순서만으로는 복구를 보장하기 어렵다.

권장 방식:

- SQLite 트랜잭션으로 state·order·fill·daily equity를 함께 저장
- 또는 일별 write-ahead journal을 먼저 기록하고 완료 마커를 남기는 복구 프로토콜

### [P2] 실험 구성 변경 검사를 확대해야 함

현재 기존 상태와 새 실행 설정 비교는 예산과 종목만 검사한다. 같은 상태 디렉터리에서 다음 설정이 바뀌면 서로 다른 실험이 하나의 성과 곡선에 섞일 수 있다.

- 데이터 소스
- 전략
- LLM 모델
- lookback
- slippage
- commission rate
- fixed fee
- min trade
- 체결 모델 버전

실험 결과에 영향을 주는 설정을 canonical config 또는 config hash로 저장하고 재실행 시 비교해야 한다.

### 2차 검증 결과

현재 환경에서 직접 실행한 결과:

```text
test_carveout.py          통과
test_carveout_broker.py   통과
test_forward.py           통과
test_llm_agent.py         통과
test_smoke.py             통과
test_toss_broker.py       통과
compileall                통과
```

테스트 통과는 현재 구현된 동작의 회귀가 없다는 의미다. 부분 체결 상태 분리, 프로세스 재시작 복구, 실제 계좌 헤더 검증 등 아직 구현되지 않은 시나리오의 안전성을 보증하지는 않는다.

### 갱신된 구현 우선순위

1. `PARTIAL_FILLED`를 `partially_filled`로 분리한다.
2. 열린 주문, 예약 현금, 예약 매도 수량, 누적 장부 반영량을 모델링한다.
3. 주문 의도와 안정적인 `client_order_id`를 전송 전에 영속화한다.
4. `TossBroker.get_order()`에 계좌 헤더를 추가하고 테스트한다.
5. 실제 매수 비용 초과 시 `ExperimentPortfolio`를 fail-closed 한다.
6. 상태 스키마 버전과 안전한 마이그레이션을 추가한다.
7. 프로세스 재시작 후 부분 체결·미체결 주문 복구 테스트를 추가한다.
8. 주문 전후 실계좌 대사를 runner에 연결한다.
9. state·order·fill·equity 저장의 트랜잭션 또는 복구 프로토콜을 구현한다.
10. 실험 구성 전체를 상태에 고정하고 변경 시 새 실험 디렉터리를 요구한다.
11. 보호 기준선 초기화와 사용자 명시 확인 절차를 구현한다.
12. 모든 live 완료 조건이 충족된 뒤에만 실주문 경로를 활성화한다.

### 2차 검토 시점 완료도

```text
페이퍼 백테스트·Forward    상당히 개선됨
카브아웃 순수 장부          양호
NAV 복리 재투자             완료
보호분 제외 리밸런싱         단위 로직 완료
next-bar 시점 누수 제거      완료
공통 비용 모델              완료
실주문 래퍼 단위 구조        양호
부분 체결 상태·잔량 추적      미완료
열린 주문·예약 자산 영속      미완료
프로세스 재시작 멱등성        미완료
실주문 조회 계좌 헤더         수정 필요
실제 비용 초과 fail-closed    미완료
상태 스키마 마이그레이션      미완료
실계좌 주문 전후 대사         미완료
보호 기준선 사용자 확인       미완료
live 안전 운용               불가
```

---

## Codex 3차 재검토 피드백 (2026-08-12)

### 총평

2차 재검토의 빠른 정정 항목은 대체로 정확하게 반영됐으며, 현재 전체 테스트와 `compileall`은 통과한다.

다음 항목은 해결된 것으로 확인했다.

- `PARTIAL_FILLED`를 `partially_filled`로 분리
- 부분 체결에서 첫 응답만 보고 폴링을 종료하지 않음
- 주문 상세 조회에 `X-Tossinvest-Account` 계좌 헤더 추가
- 실제 매수 체결 비용이 실험 현금을 초과하면 fail-closed
- 상태 스키마 버전 도입
- 결과에 영향을 주는 canonical 설정 저장 및 재실행 비교
- 설정 변경 시 동일 성과 곡선 재사용 차단
- 구버전 상태에 대한 `KeyError` 대신 설명 가능한 오류

그러나 중단 상태에서도 `CarveOutBroker`가 주문을 전송할 수 있는 P0 결함이 새로 확인됐다. 또한 fail-closed 상태의 영속성, 고정 수수료 전달, 열린 주문의 재시작 복구가 아직 미완료이므로 live 활성화는 불가하다.

### [P0] `reconciliation_required` 또는 `halted` 상태에서도 주문 전송 가능

현재 `CarveOutBroker.place_and_settle()`은 `ExperimentPortfolio.can_trade`를 확인하지 않는다.

따라서 다음과 같은 상태에서도 실험 현금과 수량 조건만 맞으면 내부 TossBroker로 주문이 전달될 수 있다.

```python
exp.require_reconciliation("계좌 수량 불일치")
assert not exp.can_trade

carveout.place_and_settle(order, client_order_id="...")
# 현재 구현에서는 주문 전송 가능
```

이는 “대사 실패 또는 안전 불변식 위반 시 모든 신규 주문을 중단한다”는 핵심 요구사항을 위반한다.

`place_and_settle()` 진입 직후 다음 검사를 수행해야 한다.

```python
if not self.exp.can_trade:
    return self._reject(
        order,
        when,
        f"실험 거래 중단 상태: {self.exp.status} — {self.exp.status_reason}",
    )
```

필수 테스트:

1. `status == reconciliation_required`이면 주문이 거부된다.
2. `status == halted`이면 주문이 거부된다.
3. 두 경우 모두 내부 브로커의 `place_order()` 호출 횟수는 0이다.
4. 거부 결과에 상태와 중단 사유가 포함된다.
5. 상태를 명시적으로 복구하기 전까지 재시도해도 계속 거부된다.

### [P1] fail-closed 예외 발생 후 상태가 저장되지 않을 수 있음

`ExperimentPortfolio.apply_buy()`와 `apply_sell()`은 불변식 위반 시 다음 순서로 동작한다.

```text
1. 메모리의 상태를 reconciliation_required로 변경
2. status_reason/status_changed_at 설정
3. ExperimentLedgerError 발생
```

그러나 현재 Forward runner는 이 예외를 처리하지 않으며 `state.save()`는 이후 단계에서 실행된다.

위험 시나리오:

```text
실제 체결 발생
→ 장부 반영 중 안전 불변식 위반
→ 메모리에서 reconciliation_required
→ 예외로 run_once 종료
→ state.json 저장 안 됨
→ 다음 프로세스는 이전 active 상태를 로드
```

Runner 또는 live 실행 계층에서 `ExperimentLedgerError`를 잡아 중단 상태와 체결 사실을 우선 영속화한 뒤 종료해야 한다.

최소 구현:

```python
try:
    apply_fill(...)
except ExperimentLedgerError:
    state.save(cfg.state_path)
    raise
```

다만 실주문 환경에서는 state 파일 저장만으로 주문·체결·장부의 원자성을 완전히 보장하지 못하므로, 열린 주문 저널 또는 SQLite 트랜잭션 도입이 권장된다.

필수 테스트:

- 실제 비용 초과로 `apply_buy()`가 예외를 던진 뒤 상태 파일을 다시 로드해도 `reconciliation_required`가 유지된다.
- 초과 매도 체결 예외 후에도 상태와 사유가 유지된다.
- 예외 후 다음 실행은 어떤 주문도 전송하지 않는다.

### [P1] `fixed_fee` 설정이 실제 비용에 전달되지 않음

`ForwardConfig`와 canonical 설정에는 `fixed_fee`가 존재하지만 현재 Forward runner는 이를 다음 경로에 전달하지 않는다.

- `PaperBroker.commission_per_trade`
- 리밸런서가 사용하는 `CostModel.fixed_fee`

따라서 사용자는 고정 수수료를 설정했다고 생각하지만 실제 주문 사이징·체결·성과 계산에는 반영되지 않는다.

권장 구현:

```python
cost = CostModel(
    slippage=cfg.slippage,
    commission_rate=cfg.commission_rate,
    fixed_fee=cfg.fixed_fee,
)

broker = PaperBroker(
    starting_cash=exp.cash,
    slippage=cfg.slippage,
    commission_rate=cfg.commission_rate,
    commission_per_trade=cfg.fixed_fee,
    min_notional=cfg.min_trade,
)

orders = signals_to_orders(
    pending,
    broker.get_portfolio(),
    prices,
    cfg.min_trade,
    cost=cost,
)
```

필수 테스트:

- 고정 수수료가 두 건 이상의 매수 사이징에 반영된다.
- 리밸런서 예상 비용과 PaperBroker 실제 차감액이 일치한다.
- 고정 수수료 때문에 마지막 주문이 현금을 초과하지 않는다.
- `fees_paid`와 성과 곡선에 고정 수수료가 반영된다.

### [P1] 부분 체결의 상태 분리는 해결됐지만 재시작 복구는 미완료

부분 체결을 최종 `filled`로 오인하던 문제는 해결됐다. `CarveOutBroker`도 부분 체결 시 폴링을 계속한다.

그러나 폴링 횟수가 끝난 뒤 다음 정보가 상태에 저장되지 않는다.

- `client_order_id`
- broker `order_id`
- 요청 수량
- 누적 체결 수량
- 이미 실험 장부에 반영한 수량
- 미체결 잔량
- 예약 현금
- 예약 매도 수량
- 마지막 확인 상태와 시각

따라서 프로세스 재시작 후 잔여 주문의 추가 체결분을 증분 정산할 수 없다. 2차 검토에서 제안한 `open_orders` 영속 모델이 여전히 필요하다.

### [P2] 스키마 버전 1을 변환 없이 통과시킬 수 있음

현재 상태 로더는 다음 경우만 거부한다.

```python
if ver > SCHEMA_VERSION:
    raise ValueError(...)
```

이 구조에서는 `schema_version=1` 파일이 통과한 뒤 `experiment` 등 새로운 필드를 읽는 과정에서 실패할 수 있다.

마이그레이션 함수가 아직 없다면 현재 버전과 정확히 일치하는 경우만 허용해야 한다.

```python
if ver != SCHEMA_VERSION:
    raise ValueError(
        f"지원하지 않는 상태 버전 {ver}. 새 --state-dir을 사용하거나 명시적으로 마이그레이션하세요."
    )
```

향후 마이그레이션을 지원할 경우 다음처럼 버전별 변환 함수를 명시적으로 둔다.

```python
if ver == 1:
    data = migrate_v1_to_v2(data)
elif ver != SCHEMA_VERSION:
    fail_closed()
```

실계좌의 보호 기준선은 마이그레이션 과정에서 자동 추정하지 않는다.

### [P2] state와 equity 로그의 원자성 문제는 남아 있음

Runner는 `state.json`을 먼저 저장한 후 `equity.jsonl`을 append한다. 두 작업 사이에서 종료되면 state에는 해당 날짜가 실행 완료로 남지만 equity 행은 누락된다. 다음 실행은 같은 날짜 중복 방지에 걸려 누락을 복구하지 못한다.

live 주문·체결 상태까지 통합하기 전에 다음 중 하나가 필요하다.

- SQLite 트랜잭션
- 일별 write-ahead journal 및 완료 마커
- 상태에서 누락된 equity 행을 재구성할 수 있는 결정론적 복구 절차

### 3차 검증 결과

현재 환경에서 직접 실행한 결과:

```text
test_carveout.py          통과
test_carveout_broker.py   통과
test_forward.py           통과
test_llm_agent.py         통과
test_smoke.py             통과
test_toss_broker.py       통과
compileall                통과
```

현재 테스트에는 `exp.can_trade == False`인 상태에서 `CarveOutBroker`가 실제 주문 전송을 차단하는 시나리오가 없다. 따라서 전체 테스트 통과가 P0 불변식을 보증하지는 않는다.

### 갱신된 구현 우선순위

1. `CarveOutBroker.place_and_settle()`에서 `exp.can_trade`를 최우선 검사한다.
2. `reconciliation_required`와 `halted` 상태의 주문 차단 테스트를 추가한다.
3. `ExperimentLedgerError` 발생 시 상태를 즉시 영속화하고 다음 실행도 fail-closed 되는지 테스트한다.
4. `fixed_fee`를 공통 `CostModel`과 `PaperBroker`에 전달한다.
5. 고정 수수료가 주문 사이징·체결·성과에 반영되는 통합 테스트를 추가한다.
6. `ForwardState.open_orders`와 예약 현금·예약 매도 수량을 구현한다.
7. 주문 전송 전에 주문 의도와 안정적인 멱등 키를 영속화한다.
8. 부분 체결의 누적값과 이미 반영한 체결량의 차이만 장부에 반영한다.
9. 프로세스 재시작 후 열린 주문을 복원하고 정산하는 테스트를 추가한다.
10. 스키마 버전이 정확히 일치하지 않으면 명시적 마이그레이션 없이 fail-closed 한다.
11. 주문·체결·state·equity 기록의 복구 프로토콜을 구현한다.
12. 주문 전후 실계좌 대사와 보호 기준선 사용자 확인 절차를 연결한다.
13. 모든 완료 조건이 충족된 뒤에만 live 경로를 활성화한다.

### 3차 검토 시점 완료도

```text
페이퍼 운용                    가능
NAV 복리 재투자                가능
백테스트 시점 누수             해결
공통 비례수수료·슬리피지 모델   완료
고정 수수료 실행 반영           미완료
카브아웃 순수 장부             양호
부분 체결 상태 분리             완료
부분 체결 잔량·재시작 복구       미완료
중단 상태의 실주문 차단         P0 결함
fail-closed 상태 영속           불완전
열린 주문·예약 자산             미완료
실계좌 대사 runner 연결         미완료
보호 기준선 초기화              미완료
state/equity 원자성             미완료
live 안전 운용                  불가
```

---

## Codex 피드백 반영 (2026-08-12, Claude)

재검토 8개 항목을 코드와 대조 검증 후 조치. 1건은 이미 반영돼 있었음.

### 이번에 처리 (즉시 정정)

- **[P1] `apply_sell` 초과체결 fail-closed** — 실체결이 실험 귀속 수량을 초과하면 `min()`으로
  조용히 축소하던 것을 제거. `ExperimentLedgerError`를 던지고 `reconciliation_required`로
  전환(보호분 침범 은폐 방지). 테스트 `test_apply_sell_overfill_fails_closed` 추가.
- **[P2] 중단 사유·시각 영속화** — `_halt_reason`(비필드) → `status_reason`/`status_changed_at`
  필드로. `to_dict/from_dict` 왕복 보존, 직렬화 테스트 강화.
- **[P2] Windows/PowerShell 테스트 실패** — `test_llm_agent.py`의 고정 `/tmp` 경로를
  `tempfile.TemporaryDirectory()`로. PowerShell 네이티브 파이썬 통과 확인.
- **문서 정정** — "테스트7 완료" → "슬리피지만, 수수료 미반영". "전체 통과"에 Windows 포함 명시.
- **[별개] 당일 종가 시점 누수 수정** — `t` 종가로 결정하고 `t` 종가에 체결하던 실현불가 가정 제거.
  **next-bar 집행**으로 변경(직전 거래일 결정을 오늘 체결). 백테스트(`main.py`)·forward(`runner.py`)
  모두. LLM 비결정성 때문에 재계산이 아니라 **직전 결정을 state에 영속(`pending_signals`)해 그대로
  체결**. 첫날은 무체결(전량 현금), 둘째날부터 어제 결정 체결. `test_two_day_continuity_next_bar` 추가.

### 스테일 (이미 반영됨 — 조치 불요)

- **[P1] Forward slippage 미전달** — `forward/runner.py`는 이미 `signals_to_orders(..., slippage=cfg.slippage)`
  로 전달 중(클립 수정 커밋에 포함). 재검토가 이전 상태를 본 것으로 보임.

### 다음 증분에서 (타당, 설계 필요)

- ✅ **[P1] TossBroker 고정 `$100` 방어 이전 — 처리(증분3a)**: `enforce_budget=False` 플래그로
  계좌전체 방어를 끄고, `CarveOutBroker`가 experiment_cash/보유 기준으로 재방어.
- ✅ **[P1] 수수료 공통 비용모델 — 처리(증분3a)**: `execution/costs.py`의 `CostModel`을
  리밸런서·브로커가 공유. 수수료 포함 사이징 테스트(테스트7) 추가.
- **[P2] 귀속 안 된 초과분 로그 3값 구분** — 증분2에서 원장에 보호/실험 포지션을 분리 노출.
  '미귀속(actual − 보호 − 실험)'분은 실계좌 holdings가 있어야 계산되므로 증분3(reconcile+live)에서 완성.

live 활성화는 위 증분3~5 + 완료 조건 충족 전까지 금지(변동 없음).

---

## Codex 2차 피드백 반영 (2026-08-12, Claude)

3차(2차 재검토) 8개 지적을 코드로 전부 확인 — 이번엔 **스테일 없이 모두 타당**. 빠른 정정 4건 처리, 나머지는 증분3b.

### 이번에 처리 (빠른 정정)

- ✅ **[P1] 부분체결을 최종체결로 종료 금지** — `PARTIAL_FILLED`를 `filled`로 정규화하던 것을
  분리(`partially_filled`). `CarveOutBroker._settle`은 `filled/rejected`만 종결로 보므로 부분체결
  시 폴링을 계속한다. 잘못된 기대를 박제한 테스트를 `test_partial_fill_not_finalized`로 수정
  (첫 partial에서 멈추지 않고 끝까지 폴링·filled로 종결 안 함 검증).
- ✅ **[P1] `get_order` 계좌 헤더** — `_get(..., account=True)` 추가. Fake transport가 주문조회 시
  `X-Tossinvest-Account` 존재를 검증(`test_get_order_sends_account_header`).
- ✅ **[P1] `apply_buy` 실비용 초과 fail-closed** — 실제 체결가·수수료 차감 후 현금이 음수면
  값을 줄이지 않고 실제로 기록한 뒤 `reconciliation_required` 전환+예외. 체결가 상승·수수료
  초과 2케이스 + 직렬화 보존 테스트.
- ✅ **[P2] 구성변경 검사 확대 + 스키마 버전** — `state.json`에 `schema_version=2`와 canonical
  `config`(source/strategy/model/lookback/min_trade/slippage/commission/fixed_fee/days/seed) 저장.
  재실행 시 불일치하면 차이를 적시해 fail-closed. 구버전(스키마 없음)은 KeyError 대신 친절한 중단.

### 증분3b (핵심 — 재시작 안전·대사 배선)

- **[P1] 멱등키·주문ID·누적체결 영속** — `ForwardState.open_orders`(client_order_id·broker_order_id·
  `accounted_filled_qty`·reserved cash/qty·status)를 **전송 前** 저장. 증분 체결만 반영, 완전체결·
  취소·거부 전까지 열린 주문 유지. **프로세스 재시작 복구 테스트**(기존 주문 재조회→정산 이어감→
  중복주문 없음). 동일프로세스 2회 호출 테스트로는 불충분하다는 지적 반영.
- **[P2] state·order·fill·equity 저장 원자성** — 완료마커/WAL 또는 SQLite 트랜잭션.
- runner의 live 경로 + 주문 전후 `reconcile` 배선(증분3b), 보호분 기준선 초기화(증분4).

전체 테스트(6파일)+`compileall` 통과. live는 3b~4 완료 전까지 계속 금지.

---

## Codex 3차 피드백 반영 (2026-08-12, Claude)

6개 지적을 grep으로 전부 검증 — **사실관계는 6개 다 정확(스테일 없음)**. 다만 심각도는 현재 상태
기준으로 과대: **`CarveOutBroker`가 어떤 실행 경로에도 배선돼 있지 않고(runner는 PaperBroker만,
live 분기 없음), paper 흐름은 fail-closed 예외를 구조적으로 못 일으키며(PaperBroker가 현금·보유
초과를 선차단), `fixed_fee`는 CLI 노출이 없어 항상 0**이다. 즉 셋 다 "오늘 돌아가는 실험엔 무영향,
아직 없는 live 경로의 선결 조건". 그럼에도 값싸고 옳으므로 선반영:

### 이번에 처리 (live 전 방어, 빠른 정정)

- ✅ **[P0] 중단 상태 주문 차단** — `CarveOutBroker.place_and_settle` 진입 즉시 `exp.can_trade`
  검사(대사필요/halted면 전송 前 거부). **Codex가 놓친 대칭 결함까지**: runner 실행 루프도
  `can_trade`가 아니면 신규 주문을 내지 않게 게이트. 테스트: 브로커(대사필요·halted·재시도 지속
  거부·전송 0), runner(중단 상태 day에서 어제 결정 무체결).
- ✅ **[P1] fail-closed 상태 영속** — runner의 체결 루프를 `try/except ExperimentLedgerError`로
  감싸 예외 시 `state.save` 후 재발생(중단 상태·부분 체결분 우선 영속). '다음 실행 무주문'은 위
  can_trade 게이트가 보장.
- ✅ **[P1] `fixed_fee` 배선** — runner가 `CostModel(...,fixed_fee)`를 리밸런서에, `commission_
  per_trade=fixed_fee`를 PaperBroker에 전달(`commission_rate`와 대칭). 고정비 반영 사이징 테스트.
- ✅ **[P2] 스키마 정확일치** — `ver != SCHEMA_VERSION`이면 fail-closed(마이그레이션 함수 생기기
  전 `=1` 등 중간 버전 무변환 통과 금지). 테스트 추가.

### 증분3b로 (남음)

- **[P1] open_orders 영속 + 재시작 복구** — client_order_id·broker_order_id·requested/누적/반영
  수량·reserved cash/qty·last status를 **전송 前** 저장, 재시작 후 재조회·증분 정산·중복주문 없음
  테스트. (2차·3차에서 재확인)
- **[P2] state·order·fill·equity 저장 원자성** — 완료마커/WAL/SQLite.
- runner live 경로 + 주문 전후 `reconcile` 배선, 보호분 기준선 초기화(증분4).

전체 테스트(6파일)+`compileall` 통과. live 계속 금지.
