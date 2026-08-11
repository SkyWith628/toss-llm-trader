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
