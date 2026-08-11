# toss-llm-trader — $100 LLM 자율매매 실험

LLM에게 **매매 전권(종목·방향·비중·타이밍)** 을 주고, $100 예산 안에서 실제로 어느 정도 수익이 나는지
**재현 가능하게 측정**하는 실험 프로젝트. 미국 주식 · 페이퍼(모의) 우선.

> ⚠️ 실험 목표는 "돈 버는 봇"이 아니라 **"LLM 자율매매가 Buy&Hold 대비 어떤지 측정하는 실험"** 입니다.
> $100 상한이 곧 리스크 통제입니다. 실계좌 전환은 페이퍼 검증 통과 후 본인이 명시적으로.

## 지금 상태: 단계 0 완료 (뼈대 + 원장)
전략을 뺀 파이프라인 전체가 돕니다. **다음 단계에서 `Strategy` 자리에 LLM 에이전트만 꽂으면** 자율매매 실험이 됩니다.

```
관찰(point-in-time) → 전략(현재: 모멘텀 규칙) → 얇은 안전판 → 모의체결(수수료·슬리피지·소수점) → 원장 → 성과지표(vs Buy&Hold)
```

## 실행 (외부 의존성 0 — 표준 라이브러리만)
```bash
python3 -m src.main                      # 합성데이터로 스모크
python3 -m src.main --days 250 --seed 7  # 다른 기간/시드
python3 -m src.main --symbols AAPL,MSFT,NVDA --source yfinance   # 실데이터(pip install yfinance)
python3 tests/test_smoke.py              # 테스트
```

## 구조
| 폴더 | 역할 |
|---|---|
| `core/` | 데이터 계약(Signal/Order/...) + point-in-time 시계(누수 방지) |
| `data/` | 가격 저장소(as-of 조회) + 합성/yfinance 로더 |
| `strategy/` | 전략 인터페이스 + 모멘텀 베이스라인 ← **다음 단계에 LLM 에이전트 추가** |
| `risk/` | 얇은 안전판(잔고·유효성·$100 한도만, 판단은 LLM 전권) |
| `execution/` | 브로커 추상 + 모의 브로커 + 리밸런서 (← TossBroker 추가 예정) |
| `experiment/` | 원장(JSONL) · 성과지표 · Buy&Hold 대조군 |

## 다음 단계 (로드맵)
1. **LLM 에이전트 전략** (`strategy/llm_agent/`) — tool-calling으로 매일 1회 결정, 메모리·회고
2. **TossBroker** (`execution/toss_broker.py`) — 토스 Open API REST 연동
3. **Forward 페이퍼 테스트** — 실시간 모의 2~4주, Buy&Hold 대비 측정
4. (선택) 실계좌 $100 — 검증 통과 후
