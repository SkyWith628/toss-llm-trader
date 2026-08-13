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

전체 9개 테스트 파일 통과(python3.12).

## 5-2. 남은 항목 처리 (2차)

원 §6의 6개 중 **깔끔하고 안전한 3개는 구현**, **대형·스펙연기 3개는 근거를 붙여 의도적 연기**.

**구현 완료**
- ✅ **스키마 마이그레이션**(`state.py::_migrate`) — v2·v3 **페이퍼** 상태를 누락 필드만 채워 v4로
  단계적 승격. **보호분이 있는 상태(live 기원)는 자동 변환 금지**(보호 기준선 자동추정 금지, 스펙
  §최초 초기화) → 새 `--state-dir`/사용자 확인 요구. v1 이하·무버전·미래버전은 fail-closed.
  테스트 `test_migrate_v3_paper_state_to_current`, `test_migrate_refuses_live_origin_baseline`.
- ✅ **guardrail 예산 인자 검증** — 코드 확인 결과 `Guardrail.check`는 `budget`을 **저장만 하고
  검사에 쓰지 않는다**(한도는 현금·보유뿐). NAV>budget을 오차단하지 않음이 확인됐고, 기존
  `test_guardrail_allows_reinvestment_above_initial_budget`가 이미 커버. 코드 변경 불필요.
- ✅ **paper↔live 혼입 차단** — `canonical()`에 `mode` 추가 → 같은 `--state-dir`에서 체결 성격이
  다른 곡선이 섞이면 config 대조에서 fail-closed. 구버전 config는 로드 시 `mode=paper` 백필.
  테스트 `test_paper_and_live_mode_not_mixed_in_one_state_dir`.

**의도적 연기 (근거)**
- ⏸ **P2 저장 원자성** — `state.save` 후 `equity.jsonl` append 사이 사망 시 성과 행 유실 가능.
  올바른 해법(SQLite 트랜잭션 또는 write-ahead 저널+완료 마커)은 기존 파일영속 구조의 재작성이라
  **스펙도 명시적으로 다음 증분으로 미룸**. 반쪽짜리 재정렬은 오히려 재실행 이중매매 위험을 만들 수
  있어, 별도 증분에서 설계째 다룬다. → 별도 증분 권장.
- ⏸ **현금성 사건 감지(배당·환전)** — 신뢰할 수 있는 '실험 귀속 현금' 산출이 필요한데, 브로커
  `buying_power`는 계좌 전체 현금이라 공유계좌에선 실험분과 분리 안 됨. 부정확한 현금 드리프트
  감지기는 **정상 운용을 오탐 halt**해 없느니만 못하다. 계좌 거래내역(입출금·배당 원장) 연동이
  선행돼야 안전 → 그 API 연동을 전제로 별도 증분.
- ⏸ **Decimal 전환** — `core/types.py`가 float를 광범위 사용. 전면 전환은 직렬화·산술·브로커 계약을
  모두 건드리는 고위험 변경인데, $100 페이퍼 실험에서의 실익은 한계적(스펙도 "가능하면 검토"). 실키
  실거래 직전에 통화 정밀도가 필요한 지점만 국소 적용을 권장. → 실거래 전 국소 도입.

전체 **9개 테스트 파일 통과**(신규 마이그레이션·mode 3건 포함).

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

리뷰 반영 완료(§5-1·§5-2): 지연 SELL 복구 순서 · 종료 대사 실패 로깅 · 멱등키 충돌 방지 ·
스키마 마이그레이션 · guardrail 검증 · paper↔live 혼입 차단.

남음(별도 증분/실거래 전): **P2 저장 원자성** · **현금성 사건 대응(거래내역 연동)** ·
**Decimal 국소 도입** · **실키 연동 검증(IP allowlist)**.
→ 위가 정리되기 전까지 실주문은 `.env TRADING_MODE=live` + 전용 $100 계좌에서만.
