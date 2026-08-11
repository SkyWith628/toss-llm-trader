"""오프라인 데이터 소스 2종: 합성(기본) + yfinance(선택).

[What] 외부 의존성 없이 바로 돌릴 합성 가격 + 실데이터 로더.
[Why]  토스 API 키가 없어도 파이프라인 전체를 검증하기 위함(단계 0의 목적).
[How]  재현성을 위해 seed 고정(ai-ml 규칙: 실험은 항상 재현 가능해야).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from src.data.storage import PriceStore

TRADING_DAYS_PER_YEAR = 252


def make_synthetic_store(
    symbols: list[str],
    days: int = 180,
    seed: int = 42,
    start_price: float = 100.0,
    annual_drift: float = 0.08,
    annual_vol: float = 0.25,
) -> PriceStore:
    """기하 브라운 운동(GBM) 근사로 일별 종가 생성.

    각 종목에 서로 다른 drift/vol를 줘 '고를 만한' 시장을 만든다.
    """
    rng = random.Random(seed)
    store = PriceStore(symbols)

    # 종목별로 개성 부여(어떤 건 상승추세, 어떤 건 변동성 큼)
    params = {}
    for s in symbols:
        params[s] = {
            "price": start_price * rng.uniform(0.5, 2.0),
            "mu": annual_drift * rng.uniform(-0.5, 1.5),
            "sigma": annual_vol * rng.uniform(0.6, 1.6),
        }

    dt = 1.0 / TRADING_DAYS_PER_YEAR
    date = datetime(2025, 1, 2)
    added = 0
    while added < days:
        # 주말 건너뛰기(월~금만)
        if date.weekday() < 5:
            row = {}
            for s in symbols:
                p = params[s]
                shock = rng.gauss(0, 1)
                # GBM: S_{t+1} = S_t * exp((mu - 0.5 sigma^2) dt + sigma sqrt(dt) Z)
                p["price"] *= pow(
                    2.718281828,
                    (p["mu"] - 0.5 * p["sigma"] ** 2) * dt
                    + p["sigma"] * (dt ** 0.5) * shock,
                )
                row[s] = round(p["price"], 4)
            store.add_row(date, row)
            added += 1
        date += timedelta(days=1)
    return store


def make_yfinance_store(
    symbols: list[str], period: str = "1y", interval: str = "1d"
) -> PriceStore:
    """실데이터 로더(선택). yfinance 미설치 시 친절한 에러.

    pip install yfinance 후 --source yfinance 로 사용.
    """
    try:
        import yfinance as yf  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "yfinance가 필요합니다. `pip install yfinance` 후 다시 시도하세요."
        ) from exc

    store = PriceStore(symbols)
    data = yf.download(symbols, period=period, interval=interval,
                       auto_adjust=True, progress=False)
    closes = data["Close"]
    for ts, row in closes.iterrows():
        date = ts.to_pydatetime()
        prices = {}
        for s in symbols:
            val = row[s] if len(symbols) > 1 else row
            if val == val:  # NaN 체크
                prices[s] = float(round(val, 4))
        if len(prices) == len(symbols):
            store.add_row(date, prices)
    return store
