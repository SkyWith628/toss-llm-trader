"""토스 Open API 읽기 전용 연결 검증 — 실키 연동의 첫 관문(단계 2 마감).

[What] .env의 자격증명으로 OAuth2 토큰 발급 → 시세·매수가능금액·잔고를 '읽기만' 한다.
[Why]  실주문 전에 '인증·엔드포인트·계좌 헤더·응답 파싱'이 실제로 맞는지 확인해야 한다.
       페이퍼 검증 통과 전까지 실주문은 금지 — 이 스크립트는 GET만 호출하고 주문은 절대 안 보낸다.
[How]  live=False로 브로커를 조립(이중 안전)하고 read 3종만 순서대로 호출, 실패는 status로 진단.
       시크릿은 어떤 경우에도 출력하지 않는다(있음/없음, 길이만).

실행(어느 폴더에서든 — 스크립트가 프로젝트 경로를 자동으로 잡는다):
    python scripts/verify_toss_connection.py
    python scripts/verify_toss_connection.py --symbols AAPL,MSFT,NVDA
    # (-m 형태는 반드시 프로젝트 루트에서: python -m scripts.verify_toss_connection)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.main import load_env_file
from src.execution.toss_broker import TossApiError, TossBroker

# accountSeq는 API가 알려주므로(아래 계좌 탐색), 처음엔 앱키/시크릿만 있으면 된다.
REQUIRED = ["TOSS_APP_KEY", "TOSS_SECRET_KEY"]


def _check_env() -> bool:
    """필수 키가 채워졌는지(값은 안 보고 존재만) 확인. 하나라도 없으면 안내 후 False."""
    missing = [k for k in REQUIRED if not os.environ.get(k, "").strip()]
    if missing:
        print("❌ .env에 다음 값이 비어 있습니다:", ", ".join(missing))
        print("   1) cp .env.example .env")
        print("   2) 토스 Open API 콘솔에서 발급한 앱키/시크릿을 .env에 채우기")
        print("   (TOSS_ACCOUNT_NO는 이 스크립트가 API로 자동 탐색해줍니다)")
        print("   (키는 채팅에 붙여넣지 말고 .env 파일에만 두세요)")
        return False
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="토스 Open API 읽기 전용 연결 검증")
    p.add_argument("--symbols", default="AAPL,MSFT,NVDA")
    args = p.parse_args()

    load_env_file()
    if not _check_env():
        return 2

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    account_no = os.environ.get("TOSS_ACCOUNT_NO", "").strip()
    # live=False 고정 — 검증은 GET만. TRADING_MODE와 무관하게 실주문 경로를 원천 차단.
    broker = TossBroker(
        client_id=os.environ["TOSS_APP_KEY"],
        client_secret=os.environ["TOSS_SECRET_KEY"],
        account_no=account_no or "PENDING",  # 계좌 탐색엔 불필요(진입점 엔드포인트)
        symbols=symbols, live=False,
    )

    print("=" * 52)
    print("  토스 Open API 읽기 전용 연결 검증 (주문 미전송)")
    print("=" * 52)

    # 1) 인증 + 계좌 탐색 — accountSeq(=TOSS_ACCOUNT_NO)의 출처
    try:
        accounts = broker.list_accounts()
        token_len = len(broker._token or "")  # 값이 아니라 '발급됨' 신호로 길이만
        print(f"✅ [1/4] OAuth2 토큰 발급 OK (token 길이 {token_len})")
    except TossApiError as e:
        print(f"❌ [1/4] 인증/계좌조회 실패: {e}")  # status·메시지만(시크릿 없음)
        return 1
    except Exception as e:  # 네트워크·DNS 등
        print(f"❌ [1/4] 연결 실패(네트워크/URL 확인): {type(e).__name__}: {e}")
        return 1

    if not accounts:
        print("⚠  [1/4] 계좌 목록이 비어 있습니다(정상 상태 계좌 없음). 토스 앱에서 계좌 상태 확인.")
        return 1
    print(f"✅ [1/4] 계좌 탐색 OK — {len(accounts)}개:")
    for a in accounts:
        seq = a.get("accountSeq", a.get("accountNo", "?"))
        label = a.get("accountName") or a.get("accountType") or a.get("status") or ""
        print(f"        · accountSeq={seq}   {label}")

    # 계좌번호 미설정이면 여기서 안내하고 종료 — 위 accountSeq를 .env에 넣으면 끝.
    if not account_no:
        seq0 = accounts[0].get("accountSeq", "")
        print("-" * 52)
        print("👉 위 accountSeq 중 쓸 계좌를 골라 .env에 넣으세요:")
        print(f"   TOSS_ACCOUNT_NO={seq0}")
        print("   그런 다음 이 스크립트를 다시 실행하면 시세·현금·잔고까지 검증합니다.")
        print("=" * 52)
        return 0

    # (설정돼 있으면) 탐색된 계좌와 일치하는지 교차 확인
    seqs = [str(a.get("accountSeq", "")) for a in accounts]
    if account_no not in seqs:
        print(f"⚠  .env의 TOSS_ACCOUNT_NO={account_no} 가 탐색된 계좌({seqs})에 없습니다. 확인 필요.")

    # 2) 시세
    try:
        print(f"✅ [2/4] 시세 조회 OK: {broker.get_prices()}")
    except TossApiError as e:
        print(f"❌ [2/4] 시세 실패: {e}")
        return 1

    # 3) 매수가능금액(현금)
    try:
        print(f"✅ [3/4] 매수가능금액 조회 OK: ${broker.get_buying_power():,.2f}")
    except TossApiError as e:
        print(f"❌ [3/4] 매수가능금액 실패: {e}")
        return 1

    # 4) 잔고(보유 종목)
    try:
        pf = broker.get_portfolio()
        held = {s: round(p.quantity, 6) for s, p in pf.positions.items()}
        print(f"✅ [4/4] 잔고 조회 OK: 현금 ${pf.cash:,.2f}, 보유 {held or '없음'}")
    except TossApiError as e:
        print(f"❌ [4/4] 잔고 실패: {e}")
        return 1

    print("-" * 52)
    print("🎉 읽기 전용 연결 검증 통과. 실주문(TRADING_MODE=live)은 페이퍼 검증 후 본인이 명시적으로.")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
