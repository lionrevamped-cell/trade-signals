# outcomes.py — Live signal outcome tracker ("agent backtesting")
"""
Continuously watches every signal fired by the live scanner and records
whether the price eventually hit TP1 / TP2 / TP3 or SL. Produces a rolling
record of actual live performance that can be compared against the
backtest predictions — so we notice immediately if calibration drifts
out of sync with real markets.

Storage: a single JSON file `outcomes.json` keyed by signal fingerprint.
For our scale (a few thousand signals/month) this is trivially fast to
load/save whole. No database needed.

Resolution rules:
  - PENDING while price hasn't hit TP1/TP2/TP3 or SL
  - SL if price touched SL first
  - TP1/TP2/TP3 when the corresponding level is touched (trade closes at
    the highest level price has reached so far; upgrades from tp1 → tp2
    → tp3 are permitted)
  - EXPIRED if not resolved within EXPIRY_BARS of first detection (we use
    wall-clock hours × TF-dependent factor)

Dedup: same (ticker, tf, direction, zone_bottom, zone_top) is ONE trade.
Subsequent scans of the same zone update "last_seen" but never start a
new tracker.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

OUTCOMES_FILE = Path(__file__).parent / "outcomes.json"
_lock = threading.Lock()

# Maximum age (in hours) for a PENDING signal before we mark it EXPIRED.
# Matches the backtest fwd_window roughly.
TF_EXPIRY_HOURS = {
    "1M":  4,      # ~240 1-min bars
    "15M": 24,     # ~96 15-min bars
    "1H":  96,     # ~96 1h bars (4 days)
    "4H":  240,    # ~60 4h bars (10 days)
    "1D":  720,    # ~30 daily bars
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(s: dict) -> str:
    """Unique key for a signal across scans. Rounded so small price
    fluctuations in the zone re-detection don't create phantom re-entries."""
    return (
        f"{s['ticker']}|{s['timeframe']}|{s['direction']}|"
        f"{round(float(s['zone_bottom']), 4)}|{round(float(s['zone_top']), 4)}"
    )


def load() -> dict[str, dict]:
    """Load the full tracked-signals map. Returns empty dict if no file."""
    with _lock:
        if not OUTCOMES_FILE.exists():
            return {}
        try:
            with open(OUTCOMES_FILE) as f:
                return json.load(f)
        except Exception:
            return {}


def save(tracked: dict[str, dict]) -> None:
    """Atomically rewrite the whole tracking file."""
    tmp = OUTCOMES_FILE.with_suffix(".json.tmp")
    with _lock:
        with open(tmp, "w") as f:
            json.dump(tracked, f, indent=2, default=str)
        os.replace(tmp, OUTCOMES_FILE)


def track_signals(signals: list[dict]) -> tuple[int, int]:
    """
    Called after each scan. Records any signal we haven't seen before.
    Returns (new_count, total_count).
    """
    if not signals:
        return 0, 0
    tracked = load()
    new = 0
    now = _now_iso()
    for s in signals:
        fp = fingerprint(s)
        if fp in tracked:
            # Update last_seen, leave outcome alone
            tracked[fp]["last_seen"] = now
            continue
        tracked[fp] = {
            "fp":           fp,
            "first_seen":   now,
            "last_seen":    now,
            "ticker":       s.get("ticker"),
            "name":         s.get("name"),
            "tf":           s.get("timeframe"),
            "direction":    s.get("direction"),
            "zone_bottom":  float(s.get("zone_bottom", 0)),
            "zone_top":     float(s.get("zone_top",    0)),
            "entry":        float(s.get("entry",      0)),
            "sl":           float(s.get("sl",         0)),
            "tp1":          float(s.get("tp1",        0)),
            "tp2":          float(s.get("tp2",        0)),
            "tp3":          float(s.get("tp3",        0)),
            # Scoring snapshot for later analysis
            "score":          int(s.get("score", 0)),
            "raw_score":      int(s.get("raw_score", 0)),
            "reason":         s.get("reason"),
            "strength":       int(s.get("strength", 1)),
            "htf_bias":       s.get("htf_bias"),
            "pd_zone":        s.get("pd_zone"),
            "pd_aligned":     bool(s.get("pd_aligned", False)),
            "liq_sweep":      bool(s.get("liq_sweep", False)),
            "htf_zone_align": bool(s.get("htf_zone_align", False)),
            "wick_tagged":    bool(s.get("wick_tagged", False)),
            "bos":            bool(s.get("bos", False)),
            "sweep":          bool(s.get("sweep", False)),
            "confirmation":   s.get("confirmation"),
            "vol_strong":     bool(s.get("vol_strong", False)),
            "regime":         s.get("regime"),
            # Live tracking
            "outcome":     "pending",
            "r":           None,
            "resolved_at": None,
            "max_favor":   0.0,   # tracks how close to TP it got
            "max_adverse": 0.0,   # tracks how close to SL it got
        }
        new += 1
    save(tracked)
    return new, len(tracked)


def check_outcomes(price_map: dict[str, float]) -> int:
    """
    Called on each live price update. For each PENDING signal, checks if
    the current price has hit TP or SL and updates outcome. Returns the
    number of resolutions this pass.

    Note: we can only see the latest *tick* price, not full bar high/low.
    Between price updates (10s apart) the price may have briefly touched
    TP or SL and come back — we'll miss those. For a conservative read,
    outcomes here are a lower bound on actual resolutions.
    """
    if not price_map:
        return 0

    tracked = load()
    resolutions = 0
    now = _now_iso()

    for fp, rec in tracked.items():
        if rec["outcome"] != "pending":
            continue
        price = price_map.get(rec["ticker"])
        if price is None:
            continue

        entry = rec["entry"]
        sl    = rec["sl"]
        risk  = abs(entry - sl) or max(abs(entry) * 0.001, 1e-9)
        # Track favorable/adverse excursion for analytics
        if rec["direction"] == "LONG":
            favor   = (price - entry) / risk
            adverse = (entry - price) / risk
        else:
            favor   = (entry - price) / risk
            adverse = (price - entry) / risk
        rec["max_favor"]   = max(rec["max_favor"],   favor)
        rec["max_adverse"] = max(rec["max_adverse"], adverse)

        # Resolution check (uses last-tick price; may miss intra-interval
        # touches but is directionally conservative)
        if rec["direction"] == "LONG":
            if price <= sl:
                rec.update(outcome="sl",  r=-1.0, resolved_at=now); resolutions += 1
            elif price >= rec["tp3"]:
                rec.update(outcome="tp3", r=5.0,  resolved_at=now); resolutions += 1
            elif price >= rec["tp2"]:
                rec.update(outcome="tp2", r=3.0,  resolved_at=now); resolutions += 1
            elif price >= rec["tp1"]:
                rec.update(outcome="tp1", r=1.5,  resolved_at=now); resolutions += 1
        else:
            if price >= sl:
                rec.update(outcome="sl",  r=-1.0, resolved_at=now); resolutions += 1
            elif price <= rec["tp3"]:
                rec.update(outcome="tp3", r=5.0,  resolved_at=now); resolutions += 1
            elif price <= rec["tp2"]:
                rec.update(outcome="tp2", r=3.0,  resolved_at=now); resolutions += 1
            elif price <= rec["tp1"]:
                rec.update(outcome="tp1", r=1.5,  resolved_at=now); resolutions += 1

    # Mark expired signals
    cutoff_by_tf = {
        tf: (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()
        for tf, h in TF_EXPIRY_HOURS.items()
    }
    for fp, rec in tracked.items():
        if rec["outcome"] != "pending":
            continue
        tf = rec.get("tf", "")
        cutoff = cutoff_by_tf.get(tf)
        if cutoff and rec["first_seen"] < cutoff:
            # Unrealized R at current price if we have it
            price = price_map.get(rec["ticker"])
            if price is not None:
                entry = rec["entry"]; sl = rec["sl"]
                risk = abs(entry - sl) or 1e-9
                r = ((price - entry) if rec["direction"] == "LONG" else (entry - price)) / risk
            else:
                r = 0.0
            rec.update(outcome="expired", r=round(r, 2), resolved_at=now)
            resolutions += 1

    if resolutions:
        save(tracked)
    return resolutions


def aggregate(lookback_days: int = 30) -> dict:
    """
    Roll up stats on resolved signals in the last N days. Returns the
    same shape as the backtest aggregate so the UI can show live vs
    backtest side-by-side.
    """
    data = load()
    if not data:
        return {"total": None, "by_score": {}, "by_factor": {}, "lookback_days": lookback_days}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    recent = [
        r for r in data.values()
        if r.get("first_seen", "") >= cutoff
    ]

    def _stats(rs):
        if not rs: return None
        # `be` (breakeven after TP1 in BE-mode) also counts as a (small) win.
        wins   = sum(1 for r in rs if r["outcome"].startswith("tp") or r["outcome"] == "be")
        losses = sum(1 for r in rs if r["outcome"] == "sl")
        pending = sum(1 for r in rs if r["outcome"] == "pending")
        expired = sum(1 for r in rs if r["outcome"] == "expired")
        decisive = wins + losses
        resolved_rs = [r for r in rs if r["outcome"] in ("sl", "tp1", "tp2", "tp3", "be", "expired")]
        avg_r = (sum(r["r"] for r in resolved_rs) / len(resolved_rs)) if resolved_rs else 0.0
        return {
            "n": len(rs),
            "resolved": len(resolved_rs),
            "pending":  pending,
            "expired":  expired,
            "wins":     wins,
            "losses":   losses,
            "win_pct":  round((wins / decisive * 100), 1) if decisive else 0.0,
            "avg_r":    round(avg_r, 2),
        }

    by_score = {}
    for sc in range(11):
        rs = [r for r in recent if r["score"] == sc]
        s = _stats(rs)
        if s: by_score[sc] = s

    by_factor = {}
    for factor in ("pd_aligned", "liq_sweep", "htf_zone_align", "bos",
                   "wick_tagged", "sweep", "vol_strong"):
        with_rs = [r for r in recent if r.get(factor)]
        wout    = [r for r in recent if not r.get(factor)]
        by_factor[factor] = {"with": _stats(with_rs), "without": _stats(wout)}

    return {
        "total":         _stats(recent),
        "by_score":      by_score,
        "by_factor":     by_factor,
        "lookback_days": lookback_days,
        "generated_at":  _now_iso(),
    }
