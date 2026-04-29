#!/usr/bin/env python3
"""
Walk-forward backtest runner. Runs multiple tickers in parallel, aggregates,
and writes JSON + progress notes.

Flags:
  --be           Enable "move SL to breakeven after TP1" in the simulator
                 (more realistic for how traders actually exit).
  --archive      Also write a timestamped copy to backtest_history/ so you
                 can cron this script weekly and see calibration drift over
                 time.
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backtest import backtest_ticker, aggregate, _stats  # noqa: E402

BE_AFTER_TP1  = "--be" in sys.argv
DO_ARCHIVE    = "--archive" in sys.argv

PROGRESS_FILE = "/tmp/backtest_progress.txt"
RESULTS_FILE  = "/tmp/backtest_results.json"
RAW_FILE      = "/tmp/backtest_raw_signals.json"
ARCHIVE_DIR   = Path(__file__).parent / "backtest_history"

JOBS = [
    # (ticker, name, category, tf_name, min_idx, fwd_window, max_bars)
    ("BTC-USD", "Bitcoin",  "Crypto",     "1H", 100, 50, 150),
    ("ETH-USD", "Ethereum", "Crypto",     "1H", 100, 50, 150),
    ("SOL-USD", "Solana",   "Crypto",     "1H", 100, 50, 150),
    ("AAPL",    "Apple",    "USA Stocks", "1H", 100, 50, 150),
    ("NVDA",    "NVIDIA",   "USA Stocks", "1H", 100, 50, 150),
    ("BTC-USD", "Bitcoin",  "Crypto",     "4H", 50,  30, 80),
    ("ETH-USD", "Ethereum", "Crypto",     "4H", 50,  30, 80),
    ("SOL-USD", "Solana",   "Crypto",     "4H", 50,  30, 80),
    ("AAPL",    "Apple",    "USA Stocks", "4H", 50,  30, 80),
    ("NVDA",    "NVIDIA",   "USA Stocks", "4H", 50,  30, 80),
]


def progress(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line + "\n")


def run_one(job):
    ticker, name, cat, tf, min_idx, fwd, mx = job
    t0 = time.time()
    progress(f"START {ticker} {tf}")
    try:
        rs = backtest_ticker(ticker, name, cat, tf, min_idx, fwd, mx,
                             be_after_tp1=BE_AFTER_TP1)
        progress(f"DONE  {ticker} {tf} -> {len(rs)} signals in {time.time()-t0:.1f}s")
        return rs
    except Exception as e:
        progress(f"FAIL  {ticker} {tf}: {e}")
        traceback.print_exc()
        return []


def main():
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
    progress(f"Starting backtest with {len(JOBS)} jobs")

    all_results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(run_one, j): j for j in JOBS}
        for fut in as_completed(futures):
            rs = fut.result()
            all_results.extend(rs)
            progress(f"  cumulative signals so far: {len(all_results)}")

    progress(f"All jobs complete. Total signals: {len(all_results)}")

    agg = aggregate(all_results)

    extras = {}
    by_bias = {}
    for bias in set(r["htf_bias"] for r in all_results):
        rs = [r for r in all_results if r["htf_bias"] == bias]
        by_bias[bias] = _stats(rs)
    extras["by_htf_bias"] = by_bias

    losers = sorted(all_results, key=lambda r: r["r"])[:15]
    extras["worst_15"] = losers

    extras["score_dist"] = {
        sc: sum(1 for r in all_results if r["score"] == sc) for sc in range(11)
    }

    per_ticker = {}
    for j in JOBS:
        key = f"{j[0]}_{j[3]}"
        rs = [r for r in all_results if r["ticker"] == j[0] and r["tf"] == j[3]]
        per_ticker[key] = _stats(rs)
    extras["per_ticker"] = per_ticker

    threshold_sweep = {}
    for t in range(4, 11):
        rs = [r for r in all_results if r["score"] >= t]
        threshold_sweep[t] = _stats(rs)
    extras["threshold_sweep"] = threshold_sweep

    out = {
        "aggregate": agg,
        "extras":    extras,
        "meta": {
            "run_at":       datetime.now(timezone.utc).isoformat(),
            "be_after_tp1": BE_AFTER_TP1,
            "jobs":         [list(j) for j in JOBS],
            "total_n":      len(all_results),
        },
    }

    with open(RESULTS_FILE, "w") as f:
        json.dump(out, f, indent=2, default=str)
    progress(f"Wrote {RESULTS_FILE}")

    with open(RAW_FILE, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    progress(f"Wrote {RAW_FILE}")

    if DO_ARCHIVE:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        suffix = "_be" if BE_AFTER_TP1 else ""
        archive_path = ARCHIVE_DIR / f"{stamp}{suffix}.json"
        with open(archive_path, "w") as f:
            json.dump(out, f, indent=2, default=str)
        progress(f"Archived to {archive_path}")


if __name__ == "__main__":
    main()
