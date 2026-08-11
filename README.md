# toss-llm-trader — $100 LLM 자율매매 실험

LLM에게 **매매 전권(종목·방향·비중·타이밍)** 을 주고, $100 예산 안에서 실제로 어느 정도 수익이 나는지
**재현 가능하게 측정**하는 실험 프로젝트. 미국 주식 · 페이퍼(모의) 우선.

> ⚠️ 실험 목표는 "돈 버는 봇"이 아니라 **"LLM 자율매매가 Buy&Hold 대비 어떤지 측정하는 실험"** 입니다.
> $100 상한이 곧 리스크 통제입니다. 실계좌 전환은 페이퍼 검증 통과 후 본인이 명시적으로.

## 지금 상태: 단계 1 완료 (LLM 에이전트)
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

## 구조
| 폴더 | 역할 |
|---|---|
| `core/` | 데이터 계약(Signal/Order/...) + point-in-time 시계(누수 방지) |
| `data/` | 가격 저장소(as-of 조회) + 합성/yfinance 로더 |
| `strategy/` | 전략 인터페이스 + 모멘텀 베이스라인 + **`llm_agent/`** (Claude 에이전트·메모리·회고·캐싱) |
| `risk/` | 얇은 안전판(잔고·유효성·$100 한도만, 판단은 LLM 전권) |
| `execution/` | 브로커 추상 + 모의 브로커 + 리밸런서 + **`toss_broker.py`**(토스 REST 실거래) |
| `experiment/` | 원장(JSONL) · 성과지표 · Buy&Hold 대조군 |

## 로드맵
1. ✅ **LLM 에이전트 전략** (`strategy/llm_agent/`) — tool-calling 결정 · 메모리·회고 · 캐싱
2. ✅ **TossBroker** (`execution/toss_broker.py`) — 토스 Open API REST 연동(OAuth2·시세·잔고·주문). 실스펙 정합·단위테스트 완료, **실키 연동 검증 대기**. 기본 dry-run(실주문은 `TRADING_MODE=live`).
3. **Forward 페이퍼 테스트** — 실시간 모의 2~4주, Buy&Hold 대비 측정
4. (선택) 실계좌 $100 — 검증 통과 후
