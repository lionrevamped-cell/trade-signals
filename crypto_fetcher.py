# crypto_fetcher.py — OHLCV for crypto via Binance futures API
"""
yfinance scrapes Yahoo, which for crypto means:
  • aggregated / averaged tick data with occasional gaps
  • higher latency than an exchange feed
  • rate limits that can silently drop responses when running many tickers

Binance Futures has a free public klines API with:
  • genuine exchange tick data, no weekend gaps (crypto is 24/7 anyway)
  • very generous rate limits (2400 weight/minute for klines)
  • clean UTC-aligned bars
  • covers every asset currently in config.ASSETS["Crypto"] + PAXG

We use this by default for any ticker ending in "-USD" and fall back to
yfinance only if Binance is unavailable. The user's BitMart prices track
Binance to within ~0.2%, so using Binance for signal detection doesn't
affect execution — the calculator still shows BitMart-specific symbols.
"""

from __future__ import annotations

import pandas as pd
import requests

BINANCE_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"

# Map yfinance intervals → Binance klines intervals.
_INTERVAL_MAP = {
    "1m":  "1m",
    "2m":  "3m",     # Binance has no 2m — 3m is the closest
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "1h",
    "1h":  "1h",
    "90m": "2h",     # rounded
    "4h":  "4h",
    "1d":  "1d",
    "5d":  "1w",     # rounded
    "1wk": "1w",
    "1mo": "1M",
}

# Minutes per bar, used for period→limit math.
_INTERVAL_MINUTES = {
    "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60,
    "90m": 90, "4h": 240, "1d": 1440, "5d": 7200, "1wk": 10080,
}


def yf_ticker_to_binance(ticker: str) -> str | None:
    """
    BTC-USD → BTCUSDT
    PAXG-USD → PAXGUSDT
    AAPL → None   (not a Binance-tradeable symbol)
    """
    if not ticker or not ticker.endswith("-USD"):
        return None
    base = ticker[: -len("-USD")]
    if not base:
        return None
    return f"{base}USDT"


def _period_to_limit(period: str, interval: str) -> int:
    """
    Translate yfinance-style periods ('1d', '8d', '30d', '60d', '365d', '5y')
    into a Binance `limit` parameter — max 1500 bars per request.
    """
    try:
        if period.endswith("d"):
            days = int(period[:-1])
        elif period.endswith("y"):
            days = int(period[:-1]) * 365
        elif period.endswith("mo"):
            days = int(period[:-2]) * 30
        else:
            days = 30
    except Exception:
        days = 30

    minutes_per_bar = _INTERVAL_MINUTES.get(interval, 60)
    n = (days * 24 * 60) // max(minutes_per_bar, 1)
    return max(100, min(n, 1500))


def fetch_binance_ohlcv(ticker: str, interval: str, period: str,
                        timeout: float = 8.0) -> pd.DataFrame | None:
    """
    Fetch OHLCV from Binance futures. Returns a DataFrame with the same
    shape as fetcher.fetch_ohlcv (index=UTC timestamps, columns=open/high/
    low/close/volume) or None on failure.
    """
    symbol = yf_ticker_to_binance(ticker)
    if symbol is None:
        return None

    binance_interval = _INTERVAL_MAP.get(interval)
    if binance_interval is None:
        return None

    limit = _period_to_limit(period, interval)

    try:
        resp = requests.get(
            BINANCE_KLINE_URL,
            params={
                "symbol":   symbol,
                "interval": binance_interval,
                "limit":    limit,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None

    # Binance kline columns:
    #   [open_time_ms, open, high, low, close, volume, close_time_ms,
    #    quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]
    try:
        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)

        # Index = bar-open time in UTC (so labels match `origin="epoch"`
        # resampling convention in fetcher.py).
        df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )

        if len(df) < 10:
            return None
        return df
    except Exception:
        return None
