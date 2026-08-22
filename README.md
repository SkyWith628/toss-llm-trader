# toss-llm-trader — $1000 LLM 자율매매 실험

LLM에게 **매매 전권(종목·방향·비중·타이밍)** 을 주고, $1000 예산 안에서 실제로 어느 정도 수익이 나는지
**재현 가능하게 측정**하는 실험 프로젝트. 미국 주식 · 페이퍼(모의) 우선.

> ⚠️ 실험 목표는 "돈 버는 봇"이 아니라 **"LLM 자율매매가 Buy&Hold 대비 어떤지 측정하는 실험"** 입니다.
> $1000 상한이 곧 리스크 통제입니다. 실계좌 전환은 페이퍼 검증 통과 후 본인이 명시적으로.

## 지금 상태: 단계 3 완료 (Forward 페이퍼 하네스) — 실시간 측정 대기
`Strategy` 자리에 **Claude 기반 `LLMAgentStrategy`** 를 꽂아 자율매매가 돕니다. 규칙(모멘텀)과 **동일한 백테스트 환경에서 공정 비교**됩니다.

```
관찰(point-in-time) → 전략(모멘텀 규칙 | LLM 에이전트) → 얇은 안전판 → 모의체결(수수료·슬리피지·소수점) → 원장 → 성과지표(vs Buy&Hold)
```

**LLM 에이전트**: 매 거래일 강제 tool-calling(`submit_decision`)으로 종목별 목표 비중 결정 · 직전 결정 회고(단기 메모리) · 프롬프트 캐싱으로 비용 절감. 결정 근거는 `logs/llm_decisions.jsonl`에 기록.

## 실행
```bash
# 규칙 전략(모멘텀) — 외부 의존성 0, 표준 라이브러리만
python3 -m src.main --days 250 --seed 7
python3 -m src.main --symbols AAPL,MSFT,NVDA --source yfinance   # 실데이터(pip install yfinance)

# LLM 에이전트 — .env에 ANTHROPIC_API_KEY 필요(자동 로드)
python3 -m src.main --strategy llm --days 20                                          # 합성데이터
python3 -m src.main --strategy llm --source yfinance --symbols AAPL,MSFT,NVDA --days 20

python3 tests/test_smoke.py && python3 tests/test_llm_agent.py   # 테스트
```
> `--days N`: 마지막 N 거래일만 매매·측정(그 이전은 워밍업 history). LLM은 opus-5 콜당 ~$0.018.

## Forward 페이퍼 테스트 (단계 3)
백테스트와 달리 **거래일마다 1회 실행**하고 상태(포지션·현금·회고·B&H 앵커)를 `logs/forward/`에
영속화해 다음 날 이어받는다. 실시간 모의로 Buy&Hold 대비 성과를 누적 측정.
```bash
# 하루치 실행(오늘 종가로 결정·모의체결·상태 갱신). cron/작업 스케줄러로 매 거래일 1회.
python3 -m src.forward_main --symbols AAPL,MSFT,NVDA --strategy llm
python3 -m src.forward_main --symbols AAPL,MSFT,NVDA --strategy momentum --source yfinance

python3 -m src.forward_main --report   # 지금까지 누적 총평(전략 vs B&H 위험조정 지표)
python3 tests/test_forward.py          # 테스트(네트워크·LLM 없이 영속성·연속성 검증)
```
> 같은 거래일 재실행은 자동 skip(`--force`로 강제). 상태 파일이 곧 페이퍼 계좌(로컬 시뮬레이션 —
> `TossBroker` 라우팅은 단계 4용). 자동화: Windows는 작업 스케줄러, Linux/mac은 cron으로 평일 장마감 후 1회.

## 구조
| 폴더 | 역할 |
|---|---|
| `core/` | 데이터 계약(Signal/Order/...) + point-in-time 시계(누수 방지) |
| `data/` | 가격 저장소(as-of 조회) + 합성/yfinance 로더 |
| `strategy/` | 전략 인터페이스 + 모멘텀 베이스라인 + **`llm_agent/`** (Claude 에이전트·메모리·회고·캐싱) |
| `risk/` | 얇은 안전판(잔고·유효성·$1000 한도만, 판단은 LLM 전권) |
| `execution/` | 브로커 추상 + 모의 브로커 + 리밸런서 + **`toss_broker.py`**(토스 REST 실거래) |
| `forward/` | **Forward 페이퍼 하네스** — 상태 영속화(`state.py`) + 하루 1회 러너(`runner.py`) |
| `experiment/` | 원장(JSONL) · 성과지표 · Buy&Hold 대조군 |

## 로드맵
1. ✅ **LLM 에이전트 전략** (`strategy/llm_agent/`) — tool-calling 결정 · 메모리·회고 · 캐싱
2. ✅ **TossBroker** (`execution/toss_broker.py`) — 토스 Open API REST 연동(OAuth2·시세·잔고·주문). 실스펙 정합·단위테스트·**실키 읽기연동 검증 완료**. 기본 dry-run(실주문은 `TRADING_MODE=live`).
   - 실키 연결 검증(읽기 전용, 주문 미전송): `cp .env.example .env`로 앱키/시크릿 채운 뒤 `python3 scripts/verify_toss_connection.py` (계좌번호는 자동 탐색; IP allowlist 등록 필요)
   - ⚠️ **실계좌 주의**: `live`는 연결된 계좌를 전략 목표비중으로 리밸런싱한다. 기존 보유분이 있는 실제 계좌에 걸면 청산될 수 있으므로, 실거래는 **전용 계좌**에서만.
3. ✅ **Forward 페이퍼 테스트 + 카브아웃 회계** (`forward/`) — 하루 1회 실행·상태 영속화·중복방지·B&H 앵커 고정. `ExperimentPortfolio`·`CarveOutBroker`로 기존 보유분을 건드리지 않고 실험분만 격리 추적(next-bar 집행·NAV 복리 재투자).
4. ✅ **실시간 일중 서킷브레이커** (`market/`·`risk/day_risk.py`·`runtime/`) — 장중 상시 감시로 당일 시작자산 대비 +임계/-임계 도달 시 전량 강제청산. 개장 1회 LLM 결정 + 결정론적 서킷브레이커. 크래시·재시작 내성(`IntradayState`).
5. ✅ **메타-포트폴리오(전략 배분)** (`portfolio/`·`runtime/meta_orchestrator.py`) — 여러 전략을 자본·리스크 예산으로 배분(Fixed·역변동성·리스크패리티) + 슬리브별 독립 서킷브레이커. 설계: `docs/design-meta-portfolio.md`·`docs/design-sleeve-accounts.md`.
6. (선택) 실계좌 $1000 — 페이퍼·dry-run 검증 통과 후
