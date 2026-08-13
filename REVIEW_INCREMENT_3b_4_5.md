# 증분 3b·4·5 재검토 자료 (Claude 작업 결과 — Codex 재검토용)

브랜치 `feat/increment-3b-4-live-wiring` / 기준 커밋 `b43cfbc` 이후 3개 커밋.
`EXPERIMENT_PORTFOLIO_SPEC.md`의 "미완료 (다음 증분)"에 있던 **증분3b·4·5**를 구현했다.
이 문서는 무엇을·왜 그렇게 했는지, 알려진 한계, 재검토 요청 포인트를 정리한다.

> 검증 환경: **Python 3.10+ 전제**(`core/types.py`가 `X | None`을 런타임 평가). 시스템 기본
> `python3`가 3.9면 `python3.12`로 실행. 전체 9개 테스트 파일 + `compileall` 통과.

---

## 1. 커밋 개요

| 커밋 | 증분 | 한 줄 |
|---|---|---|
| `bfd0518` | 3b | live 배선 — 전후 대사 + 멱등키/열린주문 write-ahead 영속 + 재시작 복구 |
| `14a42f0` | 4 | 보호 기준선 초기화 + 사용자 명시 확인(자동 생성·덮어쓰기 금지) |
| `e11d7a3` | 5 | 외부 계좌 사건 감지(수동매매·기업행동) 시 halt(fail-closed) |

변경: **+988 / −35 줄**, 신규 모듈 5(순수 로직 3 + 테스트 3).

---

## 2. 파일 맵 · 핵심 로직 위치

| 파일 | 역할 | 핵심 |
|---|---|---|
| `src/forward/order_book.py` (신규) | 열린주문 장부(순수) | `stable_client_order_id`(결정론적 멱등키), `settle_delta`(누적−이미반영 델타), `prune_terminal` |
| `src/forward/baseline.py` (신규) | 보호 기준선(순수) | `build_baseline`, `unattributed`, `preview_lines` |
| `src/forward/events.py` (신규) | 외부 사건 감지(순수) | `detect_events`(residual=(current−prev)−experiment_delta), `summarize` |
| `src/forward/state.py` | 상태 영속 | `open_orders`·`last_account_snapshot` 필드, **schema v2→v4** |
| `src/forward/carveout_broker.py` | 실계좌 집행 | `settle_existing`(재시작 조회 복구) 공개 |
| `src/forward/runner.py` | 러너 배선 | `_execute_paper`/`_execute_live` 분기, `_recover_open_orders` |
| `src/forward_main.py` | CLI | `--mode`, `--init-baseline`/`--yes`, `init_baseline` |

---

## 3. live 실행 순서 (안전 계층)

`runner._execute_live` (paper는 대사·열린주문 없는 단순 경로 `_execute_paper`):

```
start_holdings = broker.account_holdings()          # 시작 스냅샷(대사·감지 공용)
① 시작 대사   reconcile_or_halt(exp, start_holdings)  # 실계좌 < 보호+실험 → halt, 신규주문 0
② 복구        _recover_open_orders(...)               # 미결 열린주문 정산(중복 전송 없이)
                - broker_order_id 있음 → settle_existing(조회만)
                - 없음(전송 후 저장 전 사망) → place_and_settle(같은 멱등키 재전송, 브로커 dedup)
③ 사건 감지   detect_events(last_snapshot, start_holdings, experiment_delta)  # 잔여≠0 → halt
④ 신규 주문   make_intent → state.save(전송 前) → place_and_settle → 델타만 장부 반영
⑤ 종료 대사   reconcile_or_halt(exp, end_holdings); last_account_snapshot = end_holdings
```

어느 단계든 실패 시 **fail-closed** — 추측해서 매매를 이어가지 않고 상태를 영속화한 뒤 멈춘다.
`CarveOutBroker.place_and_settle`도 `exp.can_trade` 아니면 전송 자체를 거부(증분3a P0 게이트, 이중 방어).

---

## 4. 설계 결정 · Trade-off

**멱등키 = 결정론적** `f"{state_id}-{date}-{symbol}-{side}"` (`order_book.stable_client_order_id`)
- 재시작 후 같은 입력이면 같은 키 → 브로커 `clientOrderId` dedup이 '같은 의도'를 하나로 묶음.
- *대안* 랜덤 UUID: 재시작 시 새 키 → 중복주문. 그래서 결정론적 키 채택.

**열린주문 write-ahead JSON 영속** (`state.open_orders`, `runner._execute_live` ④)
- 의도·키를 **전송 이전**에 `state.save` → 전송 직후 사망해도 재시작이 이어받음.
- *대안* SQLite 트랜잭션(P2): 원자성 강하나 증분 범위 초과·기존 파일영속 재작성 → **연기**.

**장부는 실체결 델타로만 갱신** (`order_book.settle_delta`)
- 브로커 `filled_qty`는 누적 → 이미 반영한 양을 빼 이중 카운트 방지. 부분체결이 여러 실행에 걸쳐도 정확.

**보호 기준선은 사용자 명시 확인 하에서만 1회 생성** (`forward_main.init_baseline`)
- `run_once`(무인 cron)가 아니라 사람이 `--init-baseline`을 직접 실행할 때만 생성.
- 상태 파일이 이미 있으면 덮어쓰기 거부(재계산 금지). live인데 기준선 없으면 `run_once`가 fail-closed.

**외부 사건 = 실험이 설명 못하는 보유 변화** (`events.detect_events`)
- 실행 사이 계좌 변화의 정당한 원인은 '직전 실행이 남긴 열린주문의 지연 체결'뿐 → 그 델타를 빼고
  남는 잔여가 있으면 수동매매·기업행동으로 보고 halt.
- 감지용 델타는 **조회 복구(settle_existing)** 분만 포함. **재전송(place_and_settle)** 분은 시작
  스냅샷 *이후* 체결이므로 제외(§6-3 한계 참조).

---

## 5. 스펙 §필수 테스트(1–12) 커버 매핑

| # | 요구 | 커버 테스트 |
|---|---|---|
| 1 | 기존 보유 있어도 실험 최초 0주 | `test_carveout`, `test_baseline::build_baseline` |
| 2 | 0비중 → 실험분만 정확 매도 | `test_carveout` |
| 3 | 보호분 자동매매 후에도 불변 | `test_carveout`, `test_baseline` |
| 4 | NAV(예: $105) 기준 복리 리밸런싱 | `test_carveout` |
| 5 | 손실 시 외부 현금 미사용 | `test_carveout` |
| 6 | 슬리피지로 정상 주문 거부 안 됨 | `test_smoke` |
| 7 | 수수료 포함 초과면 축소/차단 | `test_smoke`(cost model) |
| 8 | 실험 보유 초과 매도 차단 | `test_carveout`, `test_carveout_broker` |
| 9 | 실계좌 < 보호+실험 → 신규주문 중단 | `test_baseline`, `test_live_wiring::start_reconcile_failure` |
| 10 | 부분체결 시 실제 체결분만 반영 | `test_live_wiring`, `test_carveout_broker` |
| 11 | 타임아웃/재시작 후 재시도 중복주문 없음 | `test_live_wiring`(조회복구·멱등재전송·2회실행) |
| 12 | 수동거래 대사 불일치 시 기준선 자동변경 금지 | `test_baseline`, `test_events` |

증분5 추가 커버(12 확장): 수동매수·기업행동(분할) 감지 → `test_events`.

---

## 5-1. 1차 리뷰 반영 (해결 완료)

리뷰에서 지적된 아래 3건을 이 브랜치에서 수정하고 회귀 테스트를 추가했다(`test_live_wiring`).

- ✅ **#4 지연 SELL → 시작 대사 오탐 halt (Critical)**: live 순서를 재정렬해 **orderId를 아는 열린
  주문의 조회 복구(전송 없음)를 시작 대사보다 먼저** 수행(`_recover_known_orders`) → 지연 SELL을
  장부에 반영한 뒤 대사. 재전송(orderId 미상)은 대사·감지 통과 후(`_recover_resend_orders`, ④).
  테스트 `test_delayed_sell_fill_recovered_before_reconcile`. (원 §6-3의 재전송 감지 오탐도 함께 해소.)
- ✅ **#5 종료 대사 실패 미노출 (Warning)**: 종료 대사 반환을 확인해 실패 시 `fills`에 `halted` 행을
  남기고 `run_once` 반환에 `status`를 추가. 테스트 `test_end_reconcile_failure_surfaced`.
- ✅ **#6 멱등키 충돌 (Warning)**: `stable_client_order_id`에 순번(`seq`) 추가, 신규 주문 루프가
  (종목,방향)별로 순번을 매겨 같은 날 같은 방향 다중 주문이 서로 다른 키를 갖게 함. 테스트
  `test_duplicate_same_side_orders_get_distinct_keys`.

전체 9개 테스트 파일 통과(python3.12). 아래 §6은 남은 판단/한계.

## 6. Codex 재검토 요청 포인트 · 알려진 한계 (남은 판단)

> 1차 리뷰의 #4·#5·#6과 원 §6-3(재전송 감지 오탐)은 해결되어 §5-1로 이동. 아래는 미해결 항목.

1. **스키마 마이그레이션 부재** — `SCHEMA_VERSION`을 2→4로 올렸고, 정확 일치 아니면 fail-closed
   (`state.py`). 기존 페이퍼 상태(v2)는 새 `--state-dir`을 써야 한다. 마이그레이션 함수는 미도입.
   → 페이퍼 상태 자동 변환을 넣을지, fail-closed 유지가 맞는지 판단 요청.

2. **P2 원자성 미해결(선행 이슈 잔존)** — `runner`는 여전히 `state.save` 후 `equity.jsonl` append.
   그 사이 사망 시 상태엔 실행됨/중복방지가 남지만 성과 로그 행이 빠질 수 있음. 실주문·열린주문까지
   얽히면 파일 2개 쓰기 순서만으로 복구 보장 어려움 → SQLite/저널은 다음 증분으로 연기.

3. **현금성 사건 미감지** — 배당금 입금·환전 손익은 계좌 **현금** 조회가 필요해 이번 감지 범위 밖
   (브로커 보유 수량만 사용). 배당 '재투자'로 수량이 늘면 잡히지만, 현금 배당은 안 잡힘.

4. **guardrail 예산 인자** — `Guardrail(budget=...)`은 "참고용"이고 NAV 복리 재투자를 막지 않는다고
   주석돼 있으나, NAV>budget 상황에서 정상 주문을 오차단하지 않는지 재확인 요청(리밸런서는
   `exp.to_portfolio()`의 NAV로 사이징).

5. **mode는 canonical config에서 제외** — 같은 `--state-dir`에서 paper↔live 전환을 config 대조로
   막지 않는다(체결 성격이 다른 곡선 혼입 가능). 의도적 라우팅 선택이나, 막아야 할지 판단 요청.

6. **Decimal 미도입** — 금액·수량 여전히 float. 통화 정밀도 관점 비용/효익 판단 요청.

---

## 7. 검증 방법

```bash
# 전체 테스트(파이썬 3.10+)
for t in smoke llm_agent forward carveout carveout_broker toss_broker live_wiring baseline events; do
  python3.12 tests/test_$t.py; done
python3.12 -m compileall -q src tests

# live 배선 end-to-end(네트워크 0, 주입 브로커) — 수동매매 감지→halt 확인 예시는 PR 설명 참조
python3.12 tests/test_live_wiring.py   # 필수테스트 10·11
python3.12 tests/test_baseline.py      # 기준선·필수테스트 12
python3.12 tests/test_events.py        # 증분5 감지
```

## 8. live 활성화 잔여 조건 (§완료 조건 대비)

충족: 보호분 주문 제외 · 실험분 초과매도 차단 · NAV 복리 · 외부현금 차단 · 수수료 포함 사이징 ·
실체결 기반 장부 · 주문 전후 대사·fail-closed 테스트 · 재시도 멱등성·부분체결 테스트.

1차 리뷰 반영(§5-1): 지연 SELL 복구 순서 · 종료 대사 실패 로깅 · 멱등키 충돌 방지 완료.

남음: **스키마 마이그레이션** · **P2 저장 원자성** · **Decimal** · **현금성 사건 대응** ·
**실키 연동 검증(IP allowlist)**.
→ 위가 정리되기 전까지 실주문은 `.env TRADING_MODE=live` + 전용 $100 계좌에서만.
