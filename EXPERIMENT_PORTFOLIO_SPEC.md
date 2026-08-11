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

`tests/test_carveout.py` — 1(실험 최초 0주), 2·8(실험분만 매도/초과 차단), 3(보호분 불변), 4·5(NAV 기준 리밸런싱), 9·12(대사 실패 시 fail-closed·기준선 유지), 직렬화 왕복.
`tests/test_smoke.py` — 6·7(슬리피지 포함 주문가능액, NAV>초기자본 재투자 허용).
전체(`test_smoke/llm_agent/toss_broker/forward/carveout`) 통과.

### 미완료 (다음 증분)

- **증분2**: `forward/runner.py`·`state.py`를 `ExperimentPortfolio`로 통합(페이퍼=`protected={}` 특수경우 → 페이퍼/실거래 단일 모델).
- **증분3(live 배선)**: `CarveOutBroker` — TossBroker 감싸 ①실주문 ②실험 슬라이스만 보고 ③`get_order` 폴링 실체결 정산(부분체결) ④**멱등키 영속**(재시도 중복주문 방지). §실체결 기반 장부 갱신 / 필수테스트 10·11.
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

