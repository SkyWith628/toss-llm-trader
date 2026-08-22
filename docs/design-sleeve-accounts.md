# v2: 슬리브 서브계좌 + 독립 서킷브레이커 설계

> 메타-포트폴리오 v2 — 각 슬리브에 **독립 자본 슬라이스 + 독립 서킷브레이커**를 줘, 한 슬리브가
> 청산(halt)돼도 나머지는 계속 굴러가게 한다. v1의 "공유 서킷" 한계를 해소.
> 선행: [[design-meta-portfolio]](v1 배분 레이어).

---

## 0. 한 문장
슬리브를 **독립 미니 트레이딩 시스템**(각자 broker·risk·state)으로 두고, `MetaOrchestrator`가 하나의
피드로 lockstep 구동하며 슬리브별 서킷을 독립 판정한다. 슬리브별 자본은 Allocator가 개장 시 슬라이스.

## 1. 왜 v2인가 (v1 한계)
v1은 단일 계좌·비중 혼합이라 **서킷브레이커가 전체 NAV에 하나**뿐. 3x 페어 슬리브의 −2% 서킷이
발동하면 **코어 B&H까지 전량 청산**된다(공유 앵커). 슬리브마다 리스크 성격이 다른데(코어=완만,
페어=고변동) 하나의 손절선을 강제하는 건 부정확. v2는 슬리브별 독립 서킷으로 이를 푼다.

## 2. 어느 평면인가 (핵심)
- **독립 서킷브레이커 = 장중 실행 평면**(`orchestrator.py` + `DayRiskManager`)의 것. v1 메타가 사는
  **일별 측정 평면**(`main.py`)엔 서킷이 없다.
- 따라서 v2 = **메타-포트폴리오를 오케스트레이터로 태우는** 작업. 슬리브별 `DayRiskManager` N개.

## 3. 핵심 설계 결정

### 결정 A — 슬리브 = 독립 미니 트레이딩 시스템 (N × broker·risk·state)
각 슬리브가 자기 `PaperBroker`(자본 슬라이스)·`DayRiskManager`·`IntradayState`·decider를 갖는다.
- **이유**: 독립 앵커·독립 halt가 **공짜로** 나온다(각 슬리브가 완결된 하루 시스템). 기존 서킷 로직
  재사용.
- **대안**: 하나의 broker에 sleeve-aware 서브계좌 → 서킷 앵커를 서브NAV로 쪼개는 커스텀·결합↑.
- **Trade-off**: N 인스턴스 오버헤드(작음) ↔ 격리·재사용 명확.
- **면접 설명**: "슬리브를 서브계좌가 아니라 독립 트레이딩 시스템으로 모델링해, 이미 검증된 서킷·상태
  로직을 그대로 N배 재사용했습니다. 격리가 설계에서 자동으로 따라옵니다."

### 결정 B — 루프 제어 역전 (MetaOrchestrator가 틱 루프 소유)
현재 `Orchestrator.run_trading_day`는 **자기 루프에서 `feed.quotes()`를 직접 소비**한다. N 슬리브가
같은 피드를 공유하면 소비가 충돌 → **제어 역전**: `Orchestrator`에서 '틱 1개 처리'를 `process_tick(px)`
로 추출하고, `MetaOrchestrator`가 피드 루프를 소유해 매 틱을 N 슬리브에 펌프한다.
- **이유**: 하나의 시장 시계열을 N 슬리브가 **동일 시점**으로 봐야 공정(같은 틱). 피드 단일 소비.
- **Trade-off**: 검증된 `Orchestrator`(65 테스트) 리팩터 위험 ↔ 틱 처리/루프 분리는 원래 SRP상 옳음.
  하위호환: 단일 슬리브용 `run_trading_day`는 `process_tick`을 호출하는 얇은 래퍼로 유지(기존 테스트 불변).
- **면접 설명**: "루프 소유권을 상위로 올리는 Inversion of Control 리팩터입니다. 틱 처리는 슬리브가, 시계열
  진행은 메타가 — 관심사를 분리하니 다중 슬리브가 자연스럽게 붙습니다."

### 결정 C — 자본 배분 = Allocator(v1) 재사용, 카브아웃 회계는 fresh
개장 시 `MetaOrchestrator`가 `Allocator.weights`로 aᵢ 산출 → 각 슬리브 broker에 `budget·aᵢ` 배정.
- main 브랜치 `CarveOutBroker`/`ExperimentPortfolio` **이식은 하지 않는다**(cross-branch). 이 브랜치에
  fresh `SleeveAccount`로 구현하고, 회계 정합·복리 재투자는 **브랜치 병합 세션**에서 통합.
- **Trade-off**: 일시적 중복(양 브랜치에 유사 개념) ↔ 지금 병합 리스크를 지지 않음. 통합 방향은
  [[project-branch-divergence]] 미결정과 함께.

### 결정 D — 슬리브 halt 시 자본 처리
한 슬리브가 서킷 발동하면 그 슬리브만 현금화·당일 잠금. **자본은 재분배하지 않는다**(당일은 현금 유지,
익일 개장에 Allocator가 재슬라이스). 결정론·단순·정직.

## 4. 파일 구조
```
src/runtime/
  sleeve_account.py     # SleeveAccount — 한 슬리브의 장중 실행 단위(broker·risk·state·decider·capital)
  meta_orchestrator.py  # MetaOrchestrator — 피드 루프 소유, N 슬리브에 틱 펌프, NAV 합산·halt 추적
  orchestrator.py       # (리팩터) process_tick(px) 추출 + run_trading_day는 얇은 래퍼
docs/design-sleeve-accounts.md
tests/test_meta_orchestrator.py
```

## 5. 데이터 흐름
```
개장: budget → Allocator(aᵢ) → 슬리브별 PaperBroker(budget·aᵢ) + DayRiskManager(앵커=슬라이스)
루프(MetaOrchestrator 소유):
  매 틱 px ← feed.quotes()
    for 슬리브 in 슬리브들:  슬리브.process_tick(px)   # 서킷 평가 → 발동 시 그 슬리브만 청산·halt
  전체 NAV = Σ 슬리브 broker.total_value
폐장: 슬리브별 종가자산 기록 + 합산 → 성과지표(vs B&H, 슬리브별 기여도·halt 사유)
```

## 6. 구현 증분 (안전 우선)
- **증분 1**: `Orchestrator.process_tick(px)` 추출 + `run_trading_day` 래퍼화. **기존 65 테스트 그대로 그린**이 성공 기준(리팩터 무회귀).
- **증분 2**: `SleeveAccount`(값+process_tick 위임) + `MetaOrchestrator`(단일 슬리브부터 → run_trading_day 동치 확인).
- **증분 3**: 다중 슬리브 — ReplayFeed로 2슬리브(코어 완만 + 페어 고변동) 오프라인 시나리오, **한 슬리브만 halt·다른 슬리브 생존** 검증.
- **증분 4**: 자본 슬라이스에 Allocator 배선 + 슬리브별 기여도·halt 리포트.

## 7. 미결정 / 리스크
- **리팩터 회귀**: `process_tick` 추출이 상태·앵커·halt 타이밍을 바꾸면 안 됨 → 증분 1에서 65 테스트가 게이트.
- **틱 정렬**: 슬리브마다 종목이 다름(코어 주식 vs 페어 3x) → 한 피드에 전 종목을 실어야. ReplayFeed 다종목 확인.
- **일별 자본 리밸런싱**: 증분 4는 개장 재슬라이스만. 장중 재분배·복리 정합은 v2.5.
- **브랜치 병합**: fresh SleeveAccount ↔ main CarveOutBroker 정합은 별도 세션([[project-branch-divergence]]).

## 부록: v2 실측 (2026-08-22, meta_dryrun)

증분 1~4 완료. `python -m src.runtime.meta_dryrun --core-frac 0.6 --pair-frac 0.4 --pair-stop -0.015`
(2026-08-21 1분봉):

| 슬리브 | 자본$ | 종가$ | 수익 | 서킷 |
|---|---:|---:|---:|---|
| core (SPY·QQQ) | 600 | 588.88 | −1.85% | — (생존) |
| pair (SOXL) | 400 | 362.83 | −9.29% | **stop_loss** |

- **독립 서킷 실증**: 페어(3x)가 손절 청산돼도 **코어는 독립적으로 계속** — v1 공유 서킷이었다면 코어까지
  함께 청산됐을 것.
- **관찰**: 페어 청산이 −1.5% 손절선이 아닌 −9.29%에서 — 3x가 **1분봉 하나에서 손절선을 훌쩍 초과**(이산
  감시 + 레버리지 갭). 버그 아닌 실제 한계(페어 설계 §부록의 "3x 갭 위험"과 일관). 후속: 틱 내 보호는
  주문형 stop이 필요(페이퍼 한계).
- **자본 슬라이스**: `slice_capital(budget, sleeves, Allocator)` 로 개장 시 배정. 장중 런타임엔 일별
  히스토리가 없어 dry-run은 고정 배분 — vol/corr 슬라이스는 일별 평면(main.py --strategy meta)에서 실증됨.

---
*권장: 증분 1(process_tick 추출, 무회귀)부터 — 리팩터가 안전해야 나머지가 선다. **v2 증분 1~4 완료.***
