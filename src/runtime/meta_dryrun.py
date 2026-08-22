"""메타 dry-run — 실 1분봉으로 슬리브별 자본 슬라이스 + 독립 서킷브레이커를 시연(실주문 0).

[What] 코어(주식 동일비중)와 페어(3x 한 레그) 두 슬리브에 Allocator로 자본을 슬라이스하고,
       MetaOrchestrator로 하루를 리플레이 → 슬리브별 서킷 발동·생존·기여도를 관찰.
[Why]  v2의 핵심(독립 서킷)을 '실제 가격'으로 눈으로 확인. 한 슬리브가 손절 청산돼도 다른
       슬리브가 계속 굴러가는지 실측.
[How]  python -m src.runtime.meta_dryrun --equities SPY,QQQ --legs SOXL,SOXS \
              --budget 1000 --core-frac 0.6 --pair-frac 0.4 --pair-stop -0.02 --period 5d
       배분은 고정(장중 런타임엔 일별 히스토리가 없어 vol/corr 슬라이스는 일별 평면 몫).
       설계: docs/design-sleeve-accounts.md
"""
from __future__ import annotations

import argparse

from src.config import DEFAULT_BUDGET_USD
from src.core.types import Action, Signal
from src.market.feed import ReplayFeed
from src.portfolio import FixedAllocator, Sleeve
from src.runtime.dryrun import _AlwaysOpenClock, load_intraday_ticks
from src.runtime.meta_orchestrator import MetaOrchestrator, slice_capital
from src.runtime.sleeve_account import make_sleeve_account
from src.strategy.rule_based import BuyAndHoldStrategy


def _equal_weight_90(symbols: list[str]):
    """개장 진입: 종목 동일비중 90%(현금 10% 버퍼)."""
    def decide(px):
        present = [s for s in symbols if px.get(s)]
        w = 0.9 / len(present) if present else 0.0
        return [Signal(s, Action.BUY, w) for s in present]
    return decide


def _one_leg_90(leg: str):
    """개장 진입: 페어의 한 레그에 90%(방향성 3x 베팅 — 데모용)."""
    return lambda px: [Signal(leg, Action.BUY, 0.9)] if px.get(leg) else []


def run_meta_dryrun(equities, legs, budget, core_frac, pair_frac,
                    core_stop, pair_stop, period, interval) -> dict:
    symbols = equities + legs
    date, ticks = load_intraday_ticks(symbols, period, interval)
    if not ticks:
        return {"status": "error", "reason": "1분봉 데이터 없음(장중/직전 세션 확인)"}

    # 배분 계산용 v1 Sleeve(고정 배분은 snapshot 불필요) → 자본 슬라이스
    defs = [Sleeve("core", BuyAndHoldStrategy(), equities),
            Sleeve("pair", BuyAndHoldStrategy(), legs)]
    caps = slice_capital(budget, defs, FixedAllocator({"core": core_frac, "pair": pair_frac}))

    clock = _AlwaysOpenClock(date)
    core = make_sleeve_account(name="core", symbols=equities, capital=caps["core"],
                               clock=clock, open_decider=_equal_weight_90(equities),
                               stop_loss=core_stop)
    pair = make_sleeve_account(name="pair", symbols=legs, capital=caps["pair"],
                               clock=clock, open_decider=_one_leg_90(legs[0]),
                               stop_loss=pair_stop)
    meta = MetaOrchestrator(sleeves=[core, pair], clock=clock,
                            feed=ReplayFeed(ticks), poll_interval=0.0)
    out = meta.run_trading_day()
    out["status"] = "ok"
    out["capital"] = caps
    return out


def _print(out: dict) -> None:
    if out.get("status") != "ok":
        print(f"[오류] {out.get('reason')}")
        return
    print("\n" + "=" * 68)
    print(f"  메타 dry-run | {out['date']} | 시작 ${out['day_start_equity']:.2f} "
          f"→ 종가 ${out['close_equity']:.2f} ({out['return']:+.2%})")
    print("=" * 68)
    print(f"  {'슬리브':<8}{'자본$':>10}{'시작$':>10}{'종가$':>10}{'수익':>9}{'서킷':>12}")
    print("  " + "-" * 64)
    for name, s in out["sleeves"].items():
        ret = s["close_equity"] / s["day_start_equity"] - 1.0 if s["day_start_equity"] else 0.0
        halt = s["halt_reason"] or "—"
        print(f"  {name:<8}{out['capital'][name]:>10.2f}{s['day_start_equity']:>10.2f}"
              f"{s['close_equity']:>10.2f}{ret:>+9.2%}{halt:>12}")
    print("  " + "-" * 64)
    survived = [n for n in out["sleeves"] if n not in out["halted_sleeves"]]
    print(f"  청산 슬리브: {out['halted_sleeves'] or '없음'} | 생존: {survived or '없음'}")
    print("  ※ 한 슬리브가 서킷 발동해도 다른 슬리브는 독립적으로 계속(v2 핵심).")
    print("=" * 68 + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description="메타 dry-run — 슬리브별 독립 서킷 시연")
    p.add_argument("--equities", default="SPY,QQQ", help="코어 슬리브 주식(쉼표)")
    p.add_argument("--legs", default="SOXL,SOXS", help="페어 슬리브 3x 레그 2개(쉼표)")
    p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD)
    p.add_argument("--core-frac", type=float, default=0.6, help="코어 자본 비중")
    p.add_argument("--pair-frac", type=float, default=0.4, help="페어 자본 비중")
    p.add_argument("--core-stop", type=float, default=-0.10, help="코어 손절선")
    p.add_argument("--pair-stop", type=float, default=-0.02, help="페어 손절선(3x라 좁게)")
    p.add_argument("--period", default="5d")
    p.add_argument("--interval", default="1m")
    args = p.parse_args()

    equities = [s.strip() for s in args.equities.split(",") if s.strip()]
    legs = [s.strip() for s in args.legs.split(",") if s.strip()]
    if len(legs) != 2:
        raise SystemExit("--legs 는 정확히 2개(예: SOXL,SOXS)")
    out = run_meta_dryrun(equities, legs, args.budget, args.core_frac, args.pair_frac,
                          args.core_stop, args.pair_stop, args.period, args.interval)
    _print(out)


if __name__ == "__main__":
    main()
