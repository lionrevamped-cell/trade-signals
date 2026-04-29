# config.py — Asset lists and scanner settings

ASSETS = {
    "Crypto": [
        ("BTC-USD",  "Bitcoin"),
        ("ETH-USD",  "Ethereum"),
        ("BNB-USD",  "BNB"),
        ("SOL-USD",  "Solana"),
        ("XRP-USD",  "XRP"),
        ("ADA-USD",  "Cardano"),
        ("AVAX-USD", "Avalanche"),
        ("DOGE-USD", "Dogecoin"),
        ("DOT-USD",  "Polkadot"),
        ("LINK-USD", "Chainlink"),
    ],
    "Commodities": [
        # Yahoo dropped XAUUSD=X from the free forex feed (returns 404).
        # GLD (SPDR Gold Trust) tracks spot gold within ~0.05% and is the
        # cleanest free proxy for spot XAU/USD.
        ("GLD",      "Gold (Spot Proxy)"),  # SPDR Gold ETF — yfinance, tracks XAUUSD
        ("PAXG-USD", "PAX Gold"),           # crypto-backed gold (Binance)
        ("GC=F",     "Gold Futures"),       # CME front-month (yfinance)
        ("SI=F",     "Silver Futures"),     # CME front-month (yfinance)
        ("SLV",      "Silver (Spot Proxy)"),# iShares Silver Trust ETF
    ],
    "India Stocks": [
        ("RELIANCE.NS",   "Reliance"),
        ("TCS.NS",        "TCS"),
        ("HDFCBANK.NS",   "HDFC Bank"),
        ("INFY.NS",       "Infosys"),
        ("ICICIBANK.NS",  "ICICI Bank"),
        ("WIPRO.NS",      "Wipro"),
        ("AXISBANK.NS",   "Axis Bank"),
        ("KOTAKBANK.NS",  "Kotak Bank"),
        ("BAJFINANCE.NS", "Bajaj Finance"),
        ("SBIN.NS",       "SBI"),
    ],
    "USA Stocks": [
        ("AAPL",  "Apple"),
        ("MSFT",  "Microsoft"),
        ("GOOGL", "Google"),
        ("AMZN",  "Amazon"),
        ("TSLA",  "Tesla"),
        ("NVDA",  "NVIDIA"),
        ("META",  "Meta"),
        ("JPM",   "JPMorgan"),
        ("SPY",   "S&P 500 ETF"),
        ("QQQ",   "Nasdaq ETF"),
    ],
}

# Timeframes to scan.
# 4H is built by resampling 1H data from yfinance.
# 1M uses only last 1d (yfinance limit: 7d for 1m data).
# 15M uses last 8d (yfinance limit: 60d for 15m data).
TIMEFRAMES = [
    {"name": "1M",  "interval": "1m",  "period": "1d",   "resample": None},
    {"name": "15M", "interval": "15m", "period": "8d",   "resample": None},
    {"name": "1H",  "interval": "1h",  "period": "30d",  "resample": None},
    {"name": "4H",  "interval": "1h",  "period": "60d",  "resample": "4h"},
    {"name": "1D",  "interval": "1d",  "period": "365d", "resample": None},
]

# ── Signal tuning ──────────────────────────────────────────────────────────────
# These are FLOOR values. Per-asset thresholds are derived from ATR (see
# ATR_*_MULT below) and the larger of (floor, ATR-derived) is used. This
# stops DOGE from sharing AAPL's static thresholds.
PROXIMITY_PCT  = 0.006   # min price proximity (0.6%) to trigger a zone
MIN_FVG_PCT    = 0.0008  # min FVG size (0.08% of price)
OB_LOOKFORWARD = 6       # candles ahead to check for impulse move
OB_MOVE_PCT    = 0.003   # 0.3% impulse move needed to qualify an OB

# ATR-relative multipliers. ATR is computed per-asset per-TF; the resulting
# atr_pct = ATR / price is multiplied below to get a volatility-aware threshold.
#   proximity used = max(PROXIMITY_PCT, atr_pct * ATR_PROXIMITY_MULT)
#   min_fvg used   = max(MIN_FVG_PCT,   atr_pct * ATR_MIN_FVG_MULT)
ATR_PROXIMITY_MULT = 0.5   # zone proximity ≈ half an ATR
ATR_MIN_FVG_MULT   = 0.15  # FVG must be at least 15% of an ATR to count
