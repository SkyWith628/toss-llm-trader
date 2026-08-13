"""Forward 페이퍼 테스트 CLI — 거래일마다 1회 실행(cron/작업 스케줄러로 자동화).

실행:
    # 하루치 실행(오늘 종가 기준으로 결정·모의체결·상태 갱신)
    python -m src.forward_main --symbols AAPL,MSFT,NVDA --strategy llm
    python -m src.forward_main --symbols AAPL,MSFT,NVDA --strategy momentum --source yfinance

    # 지금까지 누적된 실험 총평(전략 vs Buy&Hold 위험조정 지표)
    python -m src.forward_main --report

상태는 --state-dir(기본 logs/forward)에 영속화되어 다음 실행이 이어받는다.
같은 거래일 재실행은 자동 skip(--force로 강제).
"""
from __future__ import annotations

import argparse
import os

from src.main import load_env_file
from src.forward.baseline import build_baseline, preview_lines
from src.forward.runner import ForwardConfig, build_report, run_once
from src.forward.state import ForwardState

DEFAULT_STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "forward")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Forward 페이퍼 테스트 — 하루 1회 실행")
    p.add_argument("--symbols", default="AAPL,MSFT,NVDA",
                   help="쉼표구분 실티커. yfinance 소스면 실데이터로 조회")
    p.add_argument("--budget", type=float, default=100.0)
    p.add_argument("--source", choices=["synthetic", "yfinance"], default="yfinance")
    p.add_argument("--strategy", choices=["momentum", "llm"], default="llm")
    p.add_argument("--mode", choices=["paper", "live"], default="paper",
                   help="paper=모의체결 | live=실계좌 CarveOutBroker(.env TRADING_MODE로 실주문 여부)")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--min-trade", type=float, default=1.0)
    p.add_argument("--days", type=int, default=250, help="히스토리 길이(워밍업)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--state-dir", default=os.path.abspath(DEFAULT_STATE_DIR))
    p.add_argument("--force", action="store_true", help="같은 거래일 재실행 강제")
    p.add_argument("--report", action="store_true",
                   help="매매 대신 누적 실험 총평만 출력")
    p.add_argument("--init-baseline", action="store_true",
                   help="live 실험 시작: 실계좌 보유를 조회·확인해 보호 기준선으로 1회 고정")
    p.add_argument("--yes", action="store_true",
                   help="--init-baseline 확인 프롬프트를 건너뜀(비대화식 확정)")
    return p.parse_args()


def _cfg(args: argparse.Namespace) -> ForwardConfig:
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return ForwardConfig(
        symbols=symbols, budget=args.budget, source=args.source,
        strategy=args.strategy, mode=args.mode, model=args.model, lookback=args.lookback,
        min_trade=args.min_trade, days=args.days, seed=args.seed,
        state_dir=os.path.abspath(args.state_dir))


def print_run(r: dict) -> None:
    if r.get("skipped"):
        print(f"⏭  {r['reason']}  (누적 {r['day']}일)")
        return
    arrow = "✅ 우세" if r["strategy_return"] > r["bh_return"] else "❌ 열세"
    print("\n" + "=" * 52)
    print(f"  Forward Day {r['day']}  |  {r['date']}")
    print("=" * 52)
    print(f"  전략     : ${r['strategy_value']:.2f}  ({r['strategy_return']:+.2%})")
    print(f"  Buy&Hold : ${r['bh_value']:.2f}  ({r['bh_return']:+.2%})   [{arrow}]")
    print(f"  현금 ${r['cash']:.2f}  |  수수료/비용 누계 ${r['commission']:.4f}")
    if r["fills"]:
        print("  체결:")
        for f in r["fills"]:
            if f["status"] == "blocked":
                print(f"    - {f['symbol']} {f['side']} 차단({f['reason']})")
            else:
                print(f"    - {f['symbol']} {f['side']} {f.get('qty')} @ {f.get('fill')}")
    print("=" * 52 + "\n")


def _query_live_holdings(cfg: ForwardConfig) -> dict[str, float]:
    """실계좌의 종목별 총 보유 수량을 '읽기 전용'으로 조회(주문 전송 없음).

    시세가 아니라 잔고만 필요하므로 TossBroker.get_portfolio()만 쓴다. .env 자격증명 로드는
    main()에서 선행. IP allowlist 미등록 등으로 실패하면 예외가 그대로 올라온다(fail-closed).
    """
    from src.execution.toss_broker import build_toss_broker_from_env
    inner = build_toss_broker_from_env(cfg.symbols, budget=cfg.budget)
    pf = inner.get_portfolio()
    return {sym: pos.quantity for sym, pos in pf.positions.items()}


def init_baseline(cfg: ForwardConfig, *, confirm: bool,
                  holdings_provider=None) -> bool:
    """live 실험 시작: 실계좌 보유를 조회·확인해 보호 기준선으로 1회 고정. 성공 시 True.

    안전장치:
    - 이미 상태 파일이 있으면 '덮어쓰지 않는다'(기준선 재계산 금지 — 새 --state-dir을 쓰라).
    - 사용자가 명시적으로 확인(yes)하기 전엔 저장하지 않는다(--yes로 비대화식 확정 가능).
    holdings_provider: 테스트가 계좌조회를 주입(네트워크 제거). 미주입 시 실계좌를 읽는다.
    """
    if os.path.exists(cfg.state_path):
        print(f"❌ 이미 기준선/상태가 있습니다: {cfg.state_path}\n"
              "   기준선은 자동으로 다시 계산·덮어쓰지 않습니다. 새 실험은 다른 --state-dir을 쓰세요.")
        return False

    provider = holdings_provider or (lambda: _query_live_holdings(cfg))
    holdings = provider()

    for line in preview_lines(holdings, cfg.budget):
        print(line)

    if not confirm:
        ans = input("\n이 보유를 '보호 기준선'으로 고정하고 실험을 시작합니까? (yes 입력): ")
        if ans.strip().lower() != "yes":
            print("취소됨 — 기준선을 만들지 않았습니다.")
            return False

    exp = build_baseline(cfg.budget, holdings)
    state = ForwardState(
        budget=cfg.budget, symbols=cfg.symbols, source=cfg.source,
        strategy=cfg.strategy, model=cfg.model, experiment=exp, config=cfg.canonical())
    state.save(cfg.state_path)
    print(f"✅ 보호 기준선 저장 완료: {cfg.state_path}\n"
          f"   보호 {len(exp.protected_positions)}종목 · 실험 현금 ${cfg.budget:.2f} · 실험분 0.\n"
          "   이제 매 거래일 `--mode live`로 실행하세요.")
    return True


def print_report(r: dict) -> None:
    s, b = r["strategy"], r["buy_and_hold"]
    print("\n" + "=" * 58)
    print(f"  Forward 누적 총평  |  종목 {r['symbols']}  |  {r['days']}거래일")
    print("=" * 58)
    print(f"{'지표':<14}{'전략('+s['name']+')':>22}{'Buy&Hold':>18}")
    print("-" * 58)
    for key, label in [("total_return", "누적수익률"), ("cagr", "CAGR"),
                       ("sharpe", "샤프"), ("sortino", "소르티노"),
                       ("mdd", "MDD"), ("calmar", "Calmar"),
                       ("final_value", "최종자산($)")]:
        sv, bv = s.get(key, 0), b.get(key, 0)
        fmt = (lambda x: f"{x:.2%}") if key in ("total_return", "cagr", "mdd") else \
              (lambda x: f"{x:,.2f}")
        print(f"{label:<14}{fmt(sv):>22}{fmt(bv):>18}")
    print("=" * 58 + "\n")


def main() -> None:
    args = parse_args()
    load_env_file()  # LLM 전략용 ANTHROPIC_API_KEY 등을 .env에서 로드
    cfg = _cfg(args)
    if args.init_baseline:
        if cfg.mode != "live":
            raise SystemExit("--init-baseline은 --mode live에서만 씁니다(실계좌 기준선 고정).")
        init_baseline(cfg, confirm=args.yes)
        return
    if args.report:
        print_report(build_report(cfg))
        return
    print_run(run_once(cfg, force=args.force))


if __name__ == "__main__":
    main()
