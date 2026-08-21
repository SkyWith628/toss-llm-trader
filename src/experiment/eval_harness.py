"""평가 하네스 — 단일 ETF들 × 여러 거래일을 시행으로, 진입/청산 변형을 공정 비교.

[What] yfinance 1분봉을 '일자별'로 쪼개 각 날을 독립 시행(trial)으로 삼는다. 변형(B&H·개장·
       딥타이밍 × 트레일 on/off)을 같은 데이터에 돌려 기대값·승률·Sharpe·낙폭을 나란히 낸다.
[Why]  연구(Kaminski-Lo·MOP·ORB)의 효과는 레짐·데이터 의존이라 '측정 없이 구현'은 과최적화로
       흐른다. 변형을 공정 비교하는 틀이 있어야 트레일링·타이밍이 진짜 이득인지 판별한다.
       '승률'이 아니라 기대값(expectancy=E[r])을 1차 지표로 둔다(고승률≠고수익).
[How]  결정론적 변형만 → API 비용 0·재현 가능. LLM 진입은 비용 때문에 여기서 제외(별도 dry-run).
       실행: python -m src.experiment.eval_harness --symbols SPY,QQQ,DIA,IWM --period 5d
"""
from __future__ import annotations

import argparse
import os
import tempfile

from src.config import DEFAULT_STOP_LOSS, DEFAULT_TAKE_PROFIT
from src.core.types import Action, Signal
from src.execution.paper_broker import PaperBroker
from src.market.feed import ReplayFeed
from src.risk.day_risk import DayRiskManager
from src.risk.guardrail import Guardrail
from src.runtime.dryrun import _AlwaysOpenClock
from src.runtime.orchestrator import Orchestrator
from src.runtime.state import IntradayState

BUDGET = 1000.0
_BIG = 10.0  # '사실상 무한' 임계(B&H: 서킷 안 걸리게)


# --- 데이터: 1분봉을 일자별로 그룹 ---
def load_by_day(symbol: str, period: str, interval: str) -> dict[str, list[dict]]:
    """{거래일ISO: [ {sym: close}, ... ]}. 단일 종목 기준(단일 ETF 평가)."""
    import yfinance as yf

    data = yf.download(symbol, period=period, interval=interval,
                       auto_adjust=True, progress=False, threads=False)
    closes = data["Close"]
    by_day: dict[str, list[dict]] = {}
    for ts, row in closes.iterrows():
        val = row[symbol] if symbol in row.index else float("nan")
        if val == val:  # NaN 아님
            day = ts.to_pydatetime().date().isoformat()
            by_day.setdefault(day, []).append({symbol: float(round(val, 4))})
    return by_day


# --- 진입 decider 팩토리(결정론적) ---
def _open_equal(sym: str):
    return lambda px: [Signal(sym, Action.BUY, 0.9)]        # 개장 즉시 90% 진입


def _dip_entry(sym: str, dip: float):
    """개장가 대비 dip 이상 하락하면 90% 진입, 아니면 대기(빈 리스트)."""
    def decide(snap):
        hist = snap.history.get(sym, [])
        open_px = hist[0] if hist else None
        if open_px and snap.prices[sym] <= open_px * (1 - dip):
            return [Signal(sym, Action.BUY, 0.9)]
        return []
    return decide


# --- 변형 정의 ---
def _variants(sym: str, tp: float, sl: float, trail: float, arm: float, dip: float):
    """(이름, kwargs) 리스트. kwargs는 Orchestrator·DayRiskManager 구성에 쓰인다."""
    return [
        ("B&H(개장보유)",        dict(open=_open_equal(sym), tp=_BIG, sl=-_BIG, trail=None)),
        ("개장+서킷",            dict(open=_open_equal(sym), tp=tp, sl=sl, trail=None)),
        ("개장+서킷+트레일",     dict(open=_open_equal(sym), tp=tp, sl=sl, trail=trail, arm=arm)),
        ("딥타이밍+서킷",        dict(entry=_dip_entry(sym, dip), tp=tp, sl=sl, trail=None)),
        ("딥타이밍+서킷+트레일", dict(entry=_dip_entry(sym, dip), tp=tp, sl=sl, trail=trail, arm=arm)),
    ]


def _run_day(sym: str, ticks: list[dict], v: dict) -> dict:
    """한 종목·하루·한 변형 실행 → {ret, halt_reason, entered}."""
    risk = DayRiskManager(take_profit=v["tp"], stop_loss=v["sl"],
                          trailing=v.get("trail"), trailing_arm=v.get("arm", 0.0))
    orch = Orchestrator(
        symbols=[sym], clock=_AlwaysOpenClock("eval-day"), feed=ReplayFeed(ticks),
        broker=PaperBroker(starting_cash=BUDGET),
        guardrail=Guardrail(budget=BUDGET, valid_symbols={sym}),
        risk=risk, state=IntradayState.new(BUDGET, [sym]),
        open_decider=v.get("open"), entry_decider=v.get("entry"),
        entry_interval=1, max_entry_calls=10**9,
        state_path=os.path.join(tempfile.mkdtemp(), "s.json"), poll_interval=0.0,
    )
    r = orch.run_trading_day()
    return {
        "ret": r["close_equity"] / r["day_start_equity"] - 1.0,
        "halt_reason": r["halt_reason"] if r["halted"] else "",
        "entered": r["entered"],
    }


# --- 집계 지표 ---
def _aggregate(rets: list[float], halts: list[str], entered: list[bool]) -> dict:
    """시행별 일수익률 리스트 → 기대값·승률·손익비·Sharpe·복리·낙폭 등."""
    import math

    n = len(rets)
    if n == 0:
        return {"n": 0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n >= 2 else 0.0
    sd = math.sqrt(var)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss else 0.0
    # 복리 자본곡선 → 순차 낙폭
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "expectancy": mean,                                  # 1차 지표: E[일수익률]
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "sharpe": (mean / sd * math.sqrt(252)) if sd > 0 else 0.0,  # 일→연 환산
        "total": eq - 1.0,                                   # 시행 순차 복리 총수익
        "mdd": mdd,
        "worst": min(rets),
        "entry_rate": sum(entered) / n,
        "halt_stop": halts.count("stop_loss"),
        "halt_trail": halts.count("trailing_stop"),
        "halt_take": halts.count("take_profit"),
    }


def run_eval(symbols: list[str], period: str, interval: str,
             tp: float, sl: float, trail: float, arm: float, dip: float) -> dict:
    # 변형별 시행 누적
    acc: dict[str, dict] = {}
    trials = 0
    for sym in symbols:
        by_day = load_by_day(sym, period, interval)
        for _day, ticks in by_day.items():
            if len(ticks) < 5:      # 너무 짧은 날(반휴장 등) 제외
                continue
            trials += 1
            for name, v in _variants(sym, tp, sl, trail, arm, dip):
                a = acc.setdefault(name, {"rets": [], "halts": [], "entered": []})
                r = _run_day(sym, ticks, v)
                a["rets"].append(r["ret"])
                a["halts"].append(r["halt_reason"])
                a["entered"].append(r["entered"])
    results = {name: _aggregate(d["rets"], d["halts"], d["entered"])
               for name, d in acc.items()}
    return {"trials": trials, "symbols": symbols, "period": period,
            "thresholds": (tp, sl, trail, arm, dip), "results": results}


def _print(out: dict) -> None:
    tp, sl, trail, arm, dip = out["thresholds"]
    if out["trials"] == 0:
        print("[오류] 시행 데이터 없음 — 1분봉 가용 기간/종목 확인(yfinance는 최근 며칠만).")
        return
    print("\n" + "=" * 92)
    print(f"  평가 하네스 | {','.join(out['symbols'])} | {out['trials']}시행(종목×거래일) "
          f"| 익절 {tp:+.1%} 손절 {sl:+.1%} 트레일 δ{trail}/arm{arm} 딥{dip}")
    print("=" * 92)
    hdr = (f"  {'변형':<20} {'승률':>6} {'기대값':>9} {'손익비':>6} "
           f"{'Sharpe':>7} {'복리수익':>9} {'MDD':>8} {'진입율':>6} {'손절/트레일/익절':>14}")
    print(hdr)
    print("  " + "-" * 88)
    # 기대값 내림차순
    for name, m in sorted(out["results"].items(), key=lambda kv: -kv[1].get("expectancy", 0)):
        if m.get("n", 0) == 0:
            continue
        print(f"  {name:<20} {m['win_rate']:>6.0%} {m['expectancy']:>+9.3%} "
              f"{m['payoff']:>6.2f} {m['sharpe']:>7.2f} {m['total']:>+9.2%} "
              f"{m['mdd']:>8.2%} {m['entry_rate']:>6.0%} "
              f"{m['halt_stop']:>4}/{m['halt_trail']:>3}/{m['halt_take']:>3}")
    print("=" * 92)
    print("  ※ 기대값=E[일수익률](1차 지표). 승률↑와 수익↑는 대개 상충 — 손익비·Sharpe 함께 볼 것.")
    print(f"  ※ n={out['trials']}는 작음(1분봉은 최근 며칠만) — 결론 아닌 '방향 감지'용.\n")


def main() -> None:
    p = argparse.ArgumentParser(description="평가 하네스 — 단일 ETF 변형 비교")
    p.add_argument("--symbols", default="SPY,QQQ,DIA,IWM", help="단일 ETF 목록")
    p.add_argument("--period", default="5d", help="yfinance 기간(1분봉은 보통 최근 5~7일)")
    p.add_argument("--interval", default="1m")
    p.add_argument("--take-profit", type=float, default=DEFAULT_TAKE_PROFIT)
    p.add_argument("--stop-loss", type=float, default=DEFAULT_STOP_LOSS)
    p.add_argument("--trailing", type=float, default=0.005, help="트레일링 δ(고점 대비 되돌림)")
    p.add_argument("--trailing-arm", type=float, default=0.008, help="트레일링 무장 임계 a")
    p.add_argument("--dip", type=float, default=0.003, help="딥타이밍 진입 트리거(개장가 대비 하락)")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    out = run_eval(symbols, args.period, args.interval, args.take_profit,
                   args.stop_loss, args.trailing, args.trailing_arm, args.dip)
    _print(out)


if __name__ == "__main__":
    main()
