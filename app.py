"""
app.py — FastAPI server for FVG & OB Signal Scanner
Run:  python app.py
Open: http://localhost:8000
"""

from __future__ import annotations

import asyncio
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
import yfinance as yf
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from config import (
    ASSETS, TIMEFRAMES, MIN_FVG_PCT, OB_LOOKFORWARD, OB_MOVE_PCT,
    ATR_MIN_FVG_MULT,
)
from fetcher import fetch_ohlcv, resample_ohlcv
from indicators import detect_fvgs, detect_obs, atr
from signals import Signal, scan_asset, refresh_btc_momentum
import outcomes
import econ_calendar

app = FastAPI(title="FVG & OB Scanner", docs_url=None, redoc_url=None)

# ── Signal cache ───────────────────────────────────────────────────────────────
_state: dict = {
    "signals":    [],
    "last_scan":  None,
    "scanning":   False,
    "scan_count": 0,
}
_lock = threading.Lock()

# ── Price cache (refreshed every 10 seconds) ───────────────────────────────────
_prices: dict = {"data": {}, "updated": None}
_price_lock = threading.Lock()

SCAN_INTERVAL  = 5 * 60   # full scan every 5 minutes
PRICE_INTERVAL = 10       # live price update every 10 seconds

# Stop-loss buffer.
#
# Backtest 2026-04-24: volatility-scaled SL (0.5×ATR) is strictly better on
# 4H/1D setups (BTC 4H: -0.22R → +0.43R, SOL 4H: +0.55R → +0.92R) but
# hurts 1H / intraday setups (1H BTC: -0.05R → -0.18R) because the wider
# stop pushes TP1 farther than the 50-bar forward window can usually reach.
#
# Solution: gate the ATR expansion by timeframe. Higher TFs give the trade
# time to breathe; intraday stays on a tight fixed buffer.
SL_BUFFER_FLOOR     = 0.002
SL_BUFFER_ATR_MULT  = 0.5
_ATR_SL_TFS         = {"4H", "1D", "1W"}

def _sl_buffer_for(atr_pct: float, timeframe: str = "") -> float:
    if timeframe not in _ATR_SL_TFS:
        return SL_BUFFER_FLOOR
    return max(SL_BUFFER_FLOOR, SL_BUFFER_ATR_MULT * max(0.0, atr_pct))

# Timeframe config for chart data endpoint (includes 1W for multi-year analysis)
_TF_CONFIG = {tf["name"]: tf for tf in TIMEFRAMES}
_TF_CONFIG["1W"] = {"name": "1W", "interval": "1d", "period": "5y", "resample": "W"}


# ── Trade level calculator ─────────────────────────────────────────────────────

def _add_levels(d: dict) -> dict:
    """
    Add Entry, SL, TP1 (1.5R), TP2 (3.0R), TP3 (5.0R) to signal dict.

    Entry   = current price (enter at market when signal fires)
    SL      = 0.2% beyond the zone boundary that invalidates the setup
    TP1/2/3 = multiples of the risk distance from entry to SL
    """
    entry = d["current_price"]
    buf   = _sl_buffer_for(d.get("atr_pct", 0.0), d.get("timeframe", ""))

    if d["direction"] == "LONG":
        sl   = d["zone_bottom"] * (1 - buf)
        risk = max(entry - sl, entry * 0.001)   # floor at 0.1%
        d["sl"]  = round(sl, 8)
        d["tp1"] = round(entry + 1.5 * risk, 8)
        d["tp2"] = round(entry + 3.0 * risk, 8)
        d["tp3"] = round(entry + 5.0 * risk, 8)
    else:                                        # SHORT
        sl   = d["zone_top"] * (1 + buf)
        risk = max(sl - entry, entry * 0.001)
        d["sl"]  = round(sl, 8)
        d["tp1"] = round(entry - 1.5 * risk, 8)
        d["tp2"] = round(entry - 3.0 * risk, 8)
        d["tp3"] = round(entry - 5.0 * risk, 8)

    d["entry"]    = round(entry, 8)
    d["risk_pct"] = round(abs(entry - d["sl"]) / entry * 100, 3)
    d["in_zone"]  = d["zone_bottom"] <= entry <= d["zone_top"]
    return d


def _sig_to_dict(s: Signal) -> dict:
    d = {
        "category":      s.category,
        "ticker":        s.ticker,
        "name":          s.name,
        "direction":     s.direction,
        "timeframe":     s.timeframe,
        "zone_bottom":   round(float(s.zone_bottom), 8),
        "zone_top":      round(float(s.zone_top), 8),
        "current_price": round(float(s.current_price), 8),
        "reason":        s.reason,
        "strength":      int(s.strength),
        "dist_pct":      round(float(s.dist_pct), 3),
        "touch_count":   int(s.touch_count),
        "htf_bias":      s.htf_bias,
        "sweep":         bool(s.sweep),
        "confirmation":  s.confirmation,
        "score":         int(s.score),
        "zone_age":      int(s.zone_age),
        "vol_strong":    bool(s.vol_strong),
        "bos":           bool(s.bos),
        "wick_tagged":   bool(s.wick_tagged),
        "raw_score":     int(s.raw_score),
        # ── Stage B: ICT context layer ───────────────────────────────────
        "pd_zone":        s.pd_zone or "",
        "pd_aligned":     bool(s.pd_aligned),
        "liq_sweep":      bool(s.liq_sweep),
        "htf_zone_align": bool(s.htf_zone_align),
        "regime":         s.regime or "",
        "atr_pct":        round(float(s.atr_pct), 5),
    }
    # News blackout flag — added at API layer since it's time-relative
    # (blackout status evolves even between scans). `news_event` is the
    # upcoming event name if we're in blackout window.
    try:
        in_bl, evt = econ_calendar.in_blackout()
        d["news_blackout"] = bool(in_bl)
        d["news_event"]    = evt.get("title") if (in_bl and evt) else ""
    except Exception:
        d["news_blackout"] = False
        d["news_event"]    = ""
    return _add_levels(d)


# ── Full scan (every 5 min) ────────────────────────────────────────────────────

def _do_scan() -> None:
    with _lock:
        if _state["scanning"]:
            return
        _state["scanning"] = True

    all_sigs: list[Signal] = []
    try:
        # BTC correlation context — refreshed once per scan so every alt
        # signal gets the same BTC momentum snapshot.
        try:
            refresh_btc_momentum()
        except Exception:
            pass
        flat = [(t, n, cat) for cat, items in ASSETS.items() for t, n in items]
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=14) as ex:
            futures = {ex.submit(scan_asset, t, n, c): t for t, n, c in flat}
            for fut in as_completed(futures):
                try:
                    all_sigs.extend(fut.result())
                except Exception:
                    pass

        all_sigs.sort(key=lambda s: (-s.score, s.dist_pct))

        sig_dicts = [_sig_to_dict(s) for s in all_sigs]

        with _lock:
            _state["signals"]    = sig_dicts
            _state["last_scan"]  = datetime.now(timezone.utc).isoformat()
            _state["scan_count"] += 1

        # Track new signals for live outcome monitoring.
        try:
            outcomes.track_signals(sig_dicts)
        except Exception:
            pass

    finally:
        with _lock:
            _state["scanning"] = False


# ── Fast price update (every 10 s) ────────────────────────────────────────────

def _update_prices() -> None:
    """Fetch latest prices for all tickers that have active signals."""
    with _lock:
        tickers = list(set(s["ticker"] for s in _state["signals"]))

    if not tickers:
        return

    def fetch(ticker: str) -> tuple[str, float | None]:
        try:
            p = yf.Ticker(ticker).fast_info.last_price
            if p and not math.isnan(float(p)) and float(p) > 0:
                return ticker, float(p)
        except Exception:
            pass
        return ticker, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(14, len(tickers))) as ex:
        results = dict(ex.map(fetch, tickers))

    with _price_lock:
        for k, v in results.items():
            if v is not None:
                _prices["data"][k] = v
        _prices["updated"] = datetime.now(timezone.utc).isoformat()

    # Check pending tracked signals against the latest prices — this is
    # the "agent backtesting" loop that keeps calibration honest.
    try:
        with _price_lock:
            snapshot = dict(_prices["data"])
        outcomes.check_outcomes(snapshot)
    except Exception:
        pass


# ── Async background loops ─────────────────────────────────────────────────────

async def _scan_loop() -> None:
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(None, _do_scan)
        await asyncio.sleep(SCAN_INTERVAL)


async def _price_loop() -> None:
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(None, _update_prices)
        await asyncio.sleep(PRICE_INTERVAL)


# ── Auto-agent health check loop ──────────────────────────────────────────────
# Runs the auto_agent.py check once at startup (after a delay to let the first
# scan finish), then every 24 hours while the server stays up. Emits reports
# to agent_reports/ — viewable at /agent.
AGENT_STARTUP_DELAY  = 10 * 60       # 10 min — let first scan + some outcomes accrue
AGENT_INTERVAL       = 24 * 60 * 60  # 24h

def _run_auto_agent_once() -> None:
    try:
        import auto_agent as aa
        aa.run_agent()
    except Exception:
        pass

def _run_auto_learn_once() -> None:
    """Daily self-learning step. Reads outcomes.json, nudges scoring weights."""
    try:
        import auto_learn as al
        al.learn()
    except Exception:
        pass

async def _agent_loop() -> None:
    """Daily: run health agent, then run weight learner."""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(AGENT_STARTUP_DELAY)
    while True:
        await loop.run_in_executor(None, _run_auto_agent_once)
        await loop.run_in_executor(None, _run_auto_learn_once)
        await asyncio.sleep(AGENT_INTERVAL)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_scan_loop())
    asyncio.create_task(_price_loop())
    asyncio.create_task(_agent_loop())


# ── REST API ───────────────────────────────────────────────────────────────────

@app.get("/api/signals")
def api_signals() -> JSONResponse:
    with _lock:
        return JSONResponse({
            "signals":      list(_state["signals"]),
            "last_scan":    _state["last_scan"],
            "scanning":     _state["scanning"],
            "scan_count":   _state["scan_count"],
            "next_scan_in": SCAN_INTERVAL,
        })


@app.get("/api/prices")
def api_prices() -> JSONResponse:
    with _price_lock:
        return JSONResponse({
            "prices":  dict(_prices["data"]),
            "updated": _prices["updated"],
        })


@app.post("/api/scan")
async def api_trigger_scan() -> JSONResponse:
    with _lock:
        if _state["scanning"]:
            return JSONResponse({"status": "already_scanning"})
    loop = asyncio.get_event_loop()
    asyncio.create_task(loop.run_in_executor(None, _do_scan))
    return JSONResponse({"status": "started"})


@app.get("/api/status")
def api_status() -> JSONResponse:
    with _lock:
        sigs = _state["signals"]
    longs  = sum(1 for s in sigs if s["direction"] == "LONG")
    shorts = sum(1 for s in sigs if s["direction"] == "SHORT")
    strong = sum(1 for s in sigs if s["strength"] == 2)
    with _lock:
        return JSONResponse({
            "total":      len(sigs),
            "longs":      longs,
            "shorts":     shorts,
            "strong":     strong,
            "medium":     len(sigs) - strong,
            "last_scan":  _state["last_scan"],
            "scanning":   _state["scanning"],
        })


@app.get("/api/agent_report")
def api_agent_report() -> JSONResponse:
    """Return the latest auto-agent health report."""
    import json as _json
    latest = Path("agent_reports") / "latest.json"
    if not latest.exists():
        return JSONResponse({"overall": None, "message": "No report yet — click 'Run agent now'"})
    try:
        return JSONResponse(_json.loads(latest.read_text()))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/agent_run")
def api_agent_run() -> JSONResponse:
    """Kick off the auto agent on demand. Blocks ~5-15s."""
    try:
        import auto_agent as aa
        report = aa.run_agent()
        return JSONResponse({"ok": True, "overall": report["overall"]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/learn_status")
def api_learn_status() -> JSONResponse:
    """Current learned weights, defaults, last update timestamp, history."""
    try:
        import auto_learn as al
        return JSONResponse({
            **al.current_weights(),
            "history": al.history(limit=20),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/learn_run")
def api_learn_run() -> JSONResponse:
    """Run one learning cycle now (force=True bypasses the once-per-day gate)."""
    try:
        import auto_learn as al
        r = al.learn(force=True)
        return JSONResponse({"ok": True, "applied": r.get("applied", False), "report": r})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/learn_pause")
def api_learn_pause(pause: bool = True) -> JSONResponse:
    """Pause / resume auto-learn."""
    try:
        import auto_learn as al
        return JSONResponse({"ok": True, "paused": al.pause(pause)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/agent")
def agent_page() -> FileResponse:
    return FileResponse("static/agent.html")


@app.get("/api/econ_calendar")
def api_econ_calendar() -> JSONResponse:
    """Upcoming high-impact events + current blackout status."""
    try:
        in_bl, evt = econ_calendar.in_blackout()
        return JSONResponse({
            "in_blackout":   bool(in_bl),
            "current_event": evt,
            "next_events":   econ_calendar.next_events(limit=6),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/performance")
def api_performance(lookback_days: int = 30) -> JSONResponse:
    """Live signal outcome stats — updated continuously as prices tick."""
    try:
        agg = outcomes.aggregate(lookback_days=lookback_days)
        return JSONResponse(agg)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/performance")
def performance_page() -> FileResponse:
    return FileResponse("static/performance.html")


@app.get("/api/assets")
def api_assets() -> JSONResponse:
    """All assets with current prices, grouped by category."""
    with _price_lock:
        prices = dict(_prices["data"])
    result = {}
    for cat, items in ASSETS.items():
        result[cat] = [
            {"ticker": t, "name": n, "price": prices.get(t)}
            for t, n in items
        ]
    return JSONResponse(result)


@app.get("/api/asset-zones")
def api_asset_zones(ticker: str) -> JSONResponse:
    """All active FVG/OB signals for one ticker."""
    with _lock:
        sigs = [s for s in _state["signals"] if s["ticker"] == ticker]
    with _price_lock:
        price = _prices["data"].get(ticker)
    return JSONResponse({
        "ticker":        ticker,
        "current_price": price,
        "signals":       sigs,
    })


# Calculator page
@app.get("/calculator")
def calculator_page() -> FileResponse:
    return FileResponse("static/calculator.html")

# Trade journal page
@app.get("/journal")
def journal_page() -> FileResponse:
    return FileResponse("static/journal.html")


# Chart page
@app.get("/chart")
def chart_page() -> FileResponse:
    return FileResponse("static/chart.html")


@app.get("/api/chart-data")
def api_chart_data(ticker: str, timeframe: str = "1H") -> JSONResponse:
    """OHLCV candles + EMA21/50 + active FVG/OB zones for one ticker/timeframe."""
    cfg = _TF_CONFIG.get(timeframe, _TF_CONFIG["1H"])
    df  = fetch_ohlcv(ticker, cfg["interval"], cfg["period"])
    if df is None:
        return JSONResponse({"error": "no_data"}, status_code=404)
    if cfg.get("resample"):
        df = resample_ohlcv(df, cfg["resample"])
    if df is None:
        return JSONResponse({"error": "resample_failed"}, status_code=404)

    def row_ts(dt) -> int:
        try:
            return int(dt.timestamp())
        except Exception:
            return int(dt.value // 1_000_000_000)

    candles = []
    for i in range(len(df)):
        row = df.iloc[i]
        candles.append({
            "time":   row_ts(df.index[i]),
            "open":   round(float(row["open"]),  8),
            "high":   round(float(row["high"]),  8),
            "low":    round(float(row["low"]),   8),
            "close":  round(float(row["close"]), 8),
            "volume": round(float(row["volume"]), 2) if "volume" in df.columns else 0,
        })

    close   = df["close"]
    ema21   = close.ewm(span=21, adjust=False).mean()
    ema50   = close.ewm(span=50, adjust=False).mean()
    ema21_d = [{"time": row_ts(df.index[i]), "value": round(float(ema21.iat[i]), 8)} for i in range(len(df))]
    ema50_d = [{"time": row_ts(df.index[i]), "value": round(float(ema50.iat[i]), 8)} for i in range(len(df))]

    # Use the same ATR-aware min_gap_pct the scanner uses, so chart zones
    # match the signal zones (no UI/scanner divergence).
    a_val   = atr(df)
    price_v = float(df["close"].iat[-1]) if len(df) else 0.0
    atr_pct = (a_val / price_v) if (a_val > 0 and price_v > 0) else 0.0
    min_fvg = max(MIN_FVG_PCT, atr_pct * ATR_MIN_FVG_MULT)

    fvgs = detect_fvgs(df, min_gap_pct=min_fvg)
    obs  = detect_obs(
        df,
        lookforward=OB_LOOKFORWARD,
        move_pct=OB_MOVE_PCT,
        min_gap_pct=min_fvg,
        require_displacement=True,
    )

    def to_zone(z) -> dict | None:
        try:
            return {
                "kind":       z["kind"],
                "bottom":     round(float(z["bottom"]), 8),
                "top":        round(float(z["top"]),    8),
                "mid":        round(float(z["mid"]),    8),
                "start_time": row_ts(df.index[z["idx"]]),
                "zone_age":   int(z.get("zone_age",    0)),
                "vol_strong": bool(z.get("vol_strong", False)),
            }
        except Exception:
            return None

    return JSONResponse({
        "ticker":    ticker,
        "timeframe": timeframe,
        "candles":   candles,
        "ema21":     ema21_d,
        "ema50":     ema50_d,
        "fvgs":      [z for z in (to_zone(z) for z in fvgs) if z],
        "obs":       [z for z in (to_zone(z) for z in obs)  if z],
    })


# ── Serve static files ─────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False, log_level="warning")
