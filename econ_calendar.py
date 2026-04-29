# econ_calendar.py — High-impact economic event blackout
"""
CPI / FOMC / NFP spikes create fake FVGs and OBs that don't mean-revert.
Entering a signal at one is a coin-flip at best.

Free data source: Forex Factory publishes a weekly JSON calendar at
  https://nfs.faireconomy.media/ff_calendar_thisweek.json

Data format (per event):
  {
    "title": "Core CPI m/m",
    "country": "USD",
    "date": "2026-04-10T12:30:00-04:00",
    "impact": "High",
    "forecast": "0.3%",
    "previous": "0.2%"
  }

We only care about "High" impact events from USD (most-watched) and the
user's trading regions (EUR, GBP, JPY for forex impact; INR for India).

Cache: refreshed once per hour. Staleness is fine — macro releases don't
move much inside an hour and the feed publishes mid-week updates.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta

import requests

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Currencies we care about. USD dominates volatility for crypto/gold/US stocks;
# INR for India stocks; EUR/GBP for cross-market spikes.
IMPACT_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "INR", "CNY"}

# Minutes before and after an event during which signals are "in blackout".
BLACKOUT_BEFORE_MIN = 30
BLACKOUT_AFTER_MIN  = 30

_cache: dict = {"events": [], "fetched_at": 0.0}
_lock = threading.Lock()
_TTL  = 3600   # 1 hour


def _parse_iso(s: str) -> datetime | None:
    """ForexFactory uses '2026-04-10T12:30:00-04:00' style."""
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def refresh_events(force: bool = False) -> list[dict]:
    """
    Pull the weekly calendar. Cached for TTL. Returns the filtered list
    of high-impact USD/major-currency events for this week.
    """
    with _lock:
        if not force and (time.time() - _cache["fetched_at"]) < _TTL and _cache["events"]:
            return list(_cache["events"])

    try:
        resp = requests.get(FEED_URL, timeout=8.0, headers={"User-Agent": "trade-signals-scanner"})
        if resp.status_code != 200:
            return list(_cache["events"])   # return stale cache on failure
        raw = resp.json()
    except Exception:
        return list(_cache["events"])

    events: list[dict] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        if e.get("impact", "").lower() != "high":
            continue
        cur = e.get("country", "").upper()
        if cur not in IMPACT_CURRENCIES:
            continue
        dt = _parse_iso(e.get("date", ""))
        if dt is None:
            continue
        # Store as UTC for easy comparison
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        events.append({
            "title":   e.get("title", "?"),
            "country": cur,
            "dt_utc":  dt.astimezone(timezone.utc).isoformat(),
            "epoch":   int(dt.timestamp()),
            "forecast": e.get("forecast", "") or "",
            "previous": e.get("previous", "") or "",
        })

    events.sort(key=lambda x: x["epoch"])

    with _lock:
        _cache["events"]     = events
        _cache["fetched_at"] = time.time()
    return list(events)


def in_blackout(at_epoch: int | None = None) -> tuple[bool, dict | None]:
    """
    Is the given time (default: now) within ±30min of a high-impact event?
    Returns (True, event_dict) or (False, None).
    """
    if at_epoch is None:
        at_epoch = int(datetime.now(timezone.utc).timestamp())

    events = refresh_events()
    if not events:
        return False, None

    before = BLACKOUT_BEFORE_MIN * 60
    after  = BLACKOUT_AFTER_MIN  * 60
    for e in events:
        delta = at_epoch - e["epoch"]
        if -before <= delta <= after:
            return True, e
    return False, None


def next_events(limit: int = 8) -> list[dict]:
    """Next N upcoming high-impact events from now."""
    events = refresh_events()
    now = int(datetime.now(timezone.utc).timestamp())
    return [e for e in events if e["epoch"] >= now - BLACKOUT_AFTER_MIN * 60][:limit]
