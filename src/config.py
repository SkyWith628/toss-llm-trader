"""실험 전역 상수 — 여러 모듈이 공유하는 1급(first-class) 파라미터의 단일 출처.

[What] 예산처럼 파이프라인 곳곳(브로커·가드레일·벤치마크·프롬프트)이 함께 참조하는 값을
       한 곳에 모은다.
[Why]  같은 기본값이 여러 파일에 흩어지면 예산 변경 시 한 곳을 빠뜨려 '가드레일은 $1000,
       LLM은 $100'처럼 계층 간 불일치가 생긴다. 단일 출처로 그 부류의 버그를 원천 차단.
[How]  실행 시 --budget으로 덮어쓸 수 있고, 미지정 시 이 기본값을 쓴다.
"""
from __future__ import annotations

# 실험 기본 예산(USD). 새 계좌 기준 $1000. `python -m src.main --budget N`으로 재정의.
DEFAULT_BUDGET_USD: float = 1000.0

# 실시간 일중 서킷브레이커 임계 — 당일 시작 자산(day_start_equity) 대비 수익률.
# 도달 시 전량 강제청산 후 당일 거래 종료. take_profit>0, stop_loss<0.
DEFAULT_TAKE_PROFIT: float = 0.02   # +2% 익절
DEFAULT_STOP_LOSS: float = -0.01    # -1% 손절
