# fetcher.py — OHLCV data via yfinance

from __future__ import annotations

import warnings
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from crypto_fetcher import fetch_binance_ohlcv, yf_ticker_to_binance

warnings.filterwarnings("ignore")


# Approximate seconds per yfinance interval / pandas resample rule.
# Used to detect whether the last bar in the returned dataframe has actually
# closed yet. Both raw yfinance intervals (1m, 1h, 1d) and pandas resample
# rules (4h, w) are handled.
_INTERVAL_SECS = {
    "1m":  60,
    "2m":  120,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1h":  3600,
    "90m": 5400,
    "4h":  14400,
    "1d":  86400,
    "d":   86400,
    "5d":  5 * 86400,
    "1wk": 7 * 86400,
    "w":   7 * 86400,
    "1mo": 30 * 86400,
}


def _drop_unclosed_last_bar(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """
    yfinance returns the currently-forming candle as the last row, and pandas
    resample produces partial buckets at the right edge. Pattern logic
    (sweep, confirmation, BOS) on a half-formed bar is unstable — a wick or
    close can flip mid-bar and the score will flicker.

    Drop the last row if its bar-end timestamp is still in the future.
    Iterate so a chain of unclosed bars all gets removed (rare but possible
    for resample buckets that span an in-progress lower-TF bar).
    """
    if df is None or df.empty:
        return df

    key = (interval or "").lower().rstrip("0123456789").lstrip("0123456789")  # unused
    secs = _INTERVAL_SECS.get((interval or "").lower())
    if not secs:
        return df

    try:
        # Trim repeatedly in case more than one tail row is unclosed.
        while not df.empty:
            last_ts = df.index[-1]
            if last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            bar_end_epoch = int(last_ts.timestamp()) + secs
            now_epoch     = int(datetime.now(timezone.utc).timestamp())
            if bar_end_epoch > now_epoch + 2:
                df = df.iloc[:-1]
            else:
                break
    except Exception:
        return df

    return df


def fetch_ohlcv(ticker: str, interval: str, period: str) -> pd.DataFrame | None:
    """
    Download OHLCV data for one ticker.

    Crypto tickers (anything ending in '-USD' that Binance Futures trades)
    go through crypto_fetcher.fetch_binance_ohlcv — genuine exchange data,
    no weekend gaps, no yfinance scraping lag. Anything else (stocks,
    commodities, forex) falls back to yfinance.

    Uses yf.Ticker().history() — NOT yf.download() — because yf.download()
    shares a global HTTP session across threads, causing responses to bleed
    into each other when called concurrently. Ticker.history() is thread-safe.

    The currently-forming bar is dropped before returning, so all downstream
    pattern logic operates only on closed candles.
    """
    # ── Fast path: crypto via Binance ────────────────────────────────────────
    if yf_ticker_to_binance(ticker) is not None:
        bdf = fetch_binance_ohlcv(ticker, interval, period)
        if bdf is not None:
            bdf = _drop_unclosed_last_bar(bdf, interval)
            if bdf is not None and len(bdf) >= 30:
                return bdf
        # If Binance fails, fall through to yfinance for resilience.

    # ── Fallback: yfinance ───────────────────────────────────────────────────
    try:
        t = yf.Ticker(ticker)
        df: pd.DataFrame = t.history(
            interval=interval,
            period=period,
            auto_adjust=True,
        )
        if df is None or df.empty:
            return None

        # Ticker.history() returns flat columns: Open, High, Low, Close, Volume …
        df.columns = [str(c).lower() for c in df.columns]

        required = ["open", "high", "low", "close"]
        if not all(c in df.columns for c in required):
            return None

        cols = required + (["volume"] if "volume" in df.columns else [])
        df = df[cols].dropna(subset=required)

        df = _drop_unclosed_last_bar(df, interval)

        if df is None or len(df) < 30:
            return None

        return df

    except Exception:
        return None


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame | None:
    """
    Resample a 1H dataframe to a higher timeframe (e.g. '4h').

    Anchored to the UNIX epoch so 4H bars close on standard UTC boundaries
    (00/04/08/12/16/20). Without this, pandas anchors to the first bar in
    the dataset and "4H" doesn't match TradingView/exchange 4H candles.
    """
    try:
        agg: dict[str, str] = {
            "open":  "first",
            "high":  "max",
            "low":   "min",
            "close": "last",
        }
        if "volume" in df.columns:
            agg["volume"] = "sum"

        # origin='epoch' anchors buckets to UTC boundaries derived from
        # 1970-01-01, which lines up 4H/12H/1D bars with the rest of the world.
        resampled = (
            df.resample(rule, origin="epoch", label="left", closed="left")
              .agg(agg)
              .dropna()
        )
        # The last resampled bucket may be built from incomplete lower-TF
        # bars (e.g. a 4H bucket containing only 1–2 closed 1H constituents).
        # Drop it the same way we drop unclosed raw bars.
        resampled = _drop_unclosed_last_bar(resampled, rule)
        if resampled is None or len(resampled) < 20:
            return None
        return resampled
    except Exception:
        return None
