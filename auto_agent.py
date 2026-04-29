#!/usr/bin/env python3
"""
auto_agent.py — Daily health check + recommendations for the scanner

Runs a battery of checks and emits a structured report. Designed to be
cron'd (daily at 2am) but also callable ad-hoc from the /agent UI page.

Checks performed:
  1. Data pipeline     — is each asset category fetching successfully?
  2. Scanner uptime    — did scans run recently? any stuck state?
  3. Live vs backtest  — is the scoring still predictive, or has it drifted?
  4. Factor edges      — do pd_aligned / wick_tagged / liq_sweep / etc.
                         still have the edge the backtest found?
  5. Archive freshness — is the backtest history recent? recommend rerun.

Outputs:
  agent_reports/YYYY-MM-DD_HHMMSS.json  — structured report
  agent_reports/latest.json             — symlink/copy of the latest
  agent_reports/latest.md               — human-readable markdown version

The agent NEVER modifies scoring code autonomously. It only EMITS
recommendations. The human applies them via explicit PR-style change.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROJECT_DIR    = Path(__file__).parent
REPORT_DIR     = PROJECT_DIR / "agent_reports"
OUTCOMES_FILE  = PROJECT_DIR / "outcomes.json"
ARCHIVE_DIR    = PROJECT_DIR / "backtest_history"

# Backtest-expected per-score expectancy (from v5 TF-gated run)
BACKTEST_EXPECTED = {
    "overall_avg_r":        0.18,
    "overall_win_pct":      42.0,
    "by_score": {
        7:  {"win_pct": 54.1, "avg_r": +0.57},
        8:  {"win_pct": 48.6, "avg_r": +0.39},
        9:  {"win_pct": 30.8, "avg_r": -0.06},
        10: {"win_pct": 22.2, "avg_r": -0.44},
    },
    "by_factor": {
        "pd_aligned":     +6.1,
        "wick_tagged":    +3.4,
        "bos":            +1.3,
        "liq_sweep":      -6.9,
        "htf_zone_align": -1.6,
        "vol_strong":     -7.6,
        "sweep":          -0.5,
    },
}

# Drift thresholds — how far live can diverge from backtest before we alarm.
DRIFT_AVG_R_ABS    = 0.25     # |Δ avg R| on a bucket or factor
DRIFT_WIN_PCT_ABS  = 15.0     # |Δ win%|
DRIFT_FACTOR_FLIP  = True     # whether sign-flip alone triggers alarm
ARCHIVE_STALE_DAYS = 8        # rebacktest if nothing newer than this


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_outcomes() -> list[dict]:
    if not OUTCOMES_FILE.exists():
        return []
    try:
        with open(OUTCOMES_FILE) as f:
            data = json.load(f)
        return list(data.values())
    except Exception:
        return []


def _stats(rs: list[dict]) -> dict | None:
    if not rs:
        return None
    wins   = sum(1 for r in rs if r["outcome"].startswith("tp") or r["outcome"] == "be")
    losses = sum(1 for r in rs if r["outcome"] == "sl")
    decisive = wins + losses
    resolved = [r for r in rs if r["outcome"] in ("tp1", "tp2", "tp3", "sl", "be", "expired")]
    avg_r = (sum(r["r"] or 0 for r in resolved) / len(resolved)) if resolved else 0.0
    win_pct = (wins / decisive * 100) if decisive else 0.0
    return {
        "n":         len(rs),
        "resolved":  len(resolved),
        "wins":      wins,
        "losses":    losses,
        "win_pct":   round(win_pct, 1),
        "avg_r":     round(avg_r, 2),
    }


# ── Check 1: data pipeline health ──────────────────────────────────────────────

def check_data_pipeline() -> dict:
    """
    Probe up to 3 assets per category. A category is healthy if ANY probe
    succeeds — we tolerate individual tickers flaking (e.g. yfinance has
    known issues with XAUUSD=X) as long as the category works overall.
    """
    from fetcher import fetch_ohlcv

    from config import ASSETS
    results: dict[str, dict] = {}
    for cat, items in ASSETS.items():
        if not items:
            continue
        probes: list[dict] = []
        for ticker, _name in items[:3]:
            t0 = time.time()
            df = fetch_ohlcv(ticker, "1h", "7d")
            dt = time.time() - t0
            probes.append({
                "ticker":    ticker,
                "ok":        df is not None and len(df) > 10,
                "bars":      len(df) if df is not None else 0,
                "fetch_sec": round(dt, 2),
            })
        any_ok = any(p["ok"] for p in probes)
        results[cat] = {
            "ok":     any_ok,
            "probes": probes,
        }

    failed = [c for c, r in results.items() if not r["ok"]]
    return {
        "ok":         not failed,
        "categories": results,
        "summary":    "All categories fetching" if not failed
                      else f"FETCH FAILURES: {', '.join(failed)}",
    }


# ── Check 2: scanner uptime ────────────────────────────────────────────────────

def check_scanner_uptime() -> dict:
    """Inspect outcomes.json for recent signal activity."""
    outs = _load_outcomes()
    if not outs:
        return {
            "ok":      False,
            "summary": "No outcomes file found — scanner has not written anything yet",
        }
    last_seen_iso = max((r.get("last_seen", "") for r in outs), default="")
    try:
        last_seen = datetime.fromisoformat(last_seen_iso.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - last_seen).total_seconds() / 3600
    except Exception:
        age_hours = 999
    ok = age_hours < 2  # expect signals updated within 2h
    return {
        "ok":            ok,
        "last_seen_iso": last_seen_iso,
        "age_hours":     round(age_hours, 1),
        "n_tracked":     len(outs),
        "summary":       "Scanner active" if ok
                         else f"SCANNER STALE — last signal update {age_hours:.1f}h ago",
    }


# ── Check 3: live vs backtest drift ────────────────────────────────────────────

def check_score_drift(lookback_days: int = 30) -> dict:
    """Per-score live win% / avg R vs backtest-expected."""
    outs = _load_outcomes()
    if not outs:
        return {"ok": False, "summary": "No outcomes tracked yet", "by_score": {}}

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    recent = [r for r in outs if r.get("first_seen", "") >= cutoff_iso]

    findings: list[str] = []
    buckets: dict[int, dict] = {}
    MIN_N = 10   # require this many RESOLVED signals before calling drift real
    for sc in (7, 8, 9, 10):
        rs = [r for r in recent if r.get("score") == sc]
        s = _stats(rs)
        if not s:
            buckets[sc] = {"verdict": "no data"}
            continue
        if s["resolved"] < MIN_N:
            buckets[sc] = {**s, "verdict": f"insufficient sample (need ≥{MIN_N})"}
            continue
        exp = BACKTEST_EXPECTED["by_score"].get(sc, {})
        d_avg = s["avg_r"]   - exp.get("avg_r", 0)
        d_win = s["win_pct"] - exp.get("win_pct", 0)
        drift = abs(d_avg) > DRIFT_AVG_R_ABS or abs(d_win) > DRIFT_WIN_PCT_ABS
        buckets[sc] = {
            **s,
            "expected_avg_r":   exp.get("avg_r"),
            "expected_win_pct": exp.get("win_pct"),
            "d_avg_r":          round(d_avg, 2),
            "d_win_pct":        round(d_win, 1),
            "verdict":          "DRIFT" if drift else "on-track",
        }
        if drift:
            findings.append(
                f"score={sc}: live avg_r={s['avg_r']} (expected {exp.get('avg_r')}); "
                f"live win%={s['win_pct']} (expected {exp.get('win_pct')})"
            )

    return {
        "ok":        not findings,
        "by_score":  buckets,
        "findings":  findings,
        "summary":   "Score buckets tracking backtest" if not findings
                     else f"{len(findings)} score buckets drifted",
    }


# ── Check 4: factor edge drift ────────────────────────────────────────────────

def check_factor_drift(lookback_days: int = 30) -> dict:
    """Does each factor still have its backtest-confirmed edge?"""
    outs = _load_outcomes()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    recent = [r for r in outs if r.get("first_seen", "") >= cutoff_iso
              and r.get("outcome") in ("tp1", "tp2", "tp3", "sl", "be", "expired")]

    findings: list[str] = []
    factors: dict[str, dict] = {}
    MIN_N = 15
    for factor, expected_d_win in BACKTEST_EXPECTED["by_factor"].items():
        with_rs = [r for r in recent if r.get(factor)]
        without = [r for r in recent if not r.get(factor)]
        s_w  = _stats(with_rs)
        s_wo = _stats(without)
        if not s_w or not s_wo or s_w["n"] < MIN_N or s_wo["n"] < MIN_N:
            factors[factor] = {"verdict": f"insufficient sample (need ≥{MIN_N} each side)"}
            continue
        live_d = round(s_w["win_pct"] - s_wo["win_pct"], 1)
        flipped = (live_d > 0) != (expected_d_win > 0)
        big = abs(live_d - expected_d_win) > DRIFT_WIN_PCT_ABS
        factors[factor] = {
            "with":            s_w,
            "without":         s_wo,
            "live_d_win":      live_d,
            "expected_d_win":  expected_d_win,
            "flipped":         flipped,
            "verdict":         "FLIPPED" if flipped else ("DRIFT" if big else "on-track"),
        }
        if flipped or big:
            findings.append(
                f"{factor}: expected Δ={expected_d_win:+.1f}%, live Δ={live_d:+.1f}%"
                + (" (SIGN FLIPPED)" if flipped else "")
            )

    return {
        "ok":       not findings,
        "factors":  factors,
        "findings": findings,
        "summary":  "All factors tracking" if not findings
                    else f"{len(findings)} factors drifted — review scoring weights",
    }


# ── Check 5: backtest archive freshness ────────────────────────────────────────

def check_archive_freshness() -> dict:
    if not ARCHIVE_DIR.exists():
        return {"ok": False, "summary": "No backtest archive — run `python run_backtest.py --archive`"}
    files = sorted(ARCHIVE_DIR.glob("*.json"))
    if not files:
        return {"ok": False, "summary": "Archive empty — run `python run_backtest.py --archive`"}
    latest = files[-1]
    age_days = (time.time() - latest.stat().st_mtime) / 86400
    ok = age_days <= ARCHIVE_STALE_DAYS
    return {
        "ok":          ok,
        "latest_file": latest.name,
        "age_days":    round(age_days, 1),
        "summary":     f"Last backtest {age_days:.1f} days old" +
                       (" — run a fresh `python run_backtest.py --archive`" if not ok else ""),
    }


# ── Recommendation engine ──────────────────────────────────────────────────────

def build_recommendations(checks: dict) -> list[dict]:
    recs: list[dict] = []

    if not checks["data_pipeline"]["ok"]:
        recs.append({
            "severity": "HIGH",
            "title":    "Data pipeline has failing categories",
            "body":     checks["data_pipeline"]["summary"],
            "action":   "Check yfinance/Binance connectivity. If persistent, inspect fetch logs.",
        })

    if not checks["scanner_uptime"]["ok"]:
        recs.append({
            "severity": "HIGH",
            "title":    "Scanner appears stale",
            "body":     checks["scanner_uptime"]["summary"],
            "action":   "Verify the FastAPI server is running (`lsof -i :8000`). Restart if needed.",
        })

    if checks["factor_drift"]["findings"]:
        recs.append({
            "severity": "MEDIUM",
            "title":    "Factor edges have drifted from backtest",
            "body":     "\n".join(checks["factor_drift"]["findings"]),
            "action":   "Run `python run_backtest.py --archive` to see if drift is real. "
                        "If confirmed over two runs, consider adjusting scoring weights in "
                        "signals.py:_compute_score.",
        })

    if checks["score_drift"]["findings"]:
        recs.append({
            "severity": "MEDIUM",
            "title":    "Per-score expectancy has drifted",
            "body":     "\n".join(checks["score_drift"]["findings"]),
            "action":   "The score buckets are predicting less well than backtest said. "
                        "Likely needs fresh backtest + possible recalibration.",
        })

    if not checks["archive_freshness"]["ok"]:
        recs.append({
            "severity": "LOW",
            "title":    "Backtest archive is stale",
            "body":     checks["archive_freshness"]["summary"],
            "action":   "Run `python run_backtest.py --archive` to refresh.",
        })

    if not recs:
        recs.append({
            "severity": "OK",
            "title":    "All checks green — no action needed",
            "body":     "System is healthy and tracking the backtest.",
            "action":   "",
        })

    return recs


# ── Main runner ────────────────────────────────────────────────────────────────

def run_agent() -> dict:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started = _now_iso()
    t0 = time.time()

    checks = {
        "data_pipeline":     check_data_pipeline(),
        "scanner_uptime":    check_scanner_uptime(),
        "score_drift":       check_score_drift(),
        "factor_drift":      check_factor_drift(),
        "archive_freshness": check_archive_freshness(),
    }

    recommendations = build_recommendations(checks)

    # Overall status = worst of any check
    severities = [r["severity"] for r in recommendations]
    if "HIGH" in severities:    overall = "red"
    elif "MEDIUM" in severities: overall = "amber"
    else:                        overall = "green"

    report = {
        "generated_at":     started,
        "runtime_sec":      round(time.time() - t0, 1),
        "overall":          overall,
        "checks":           checks,
        "recommendations":  recommendations,
    }

    # Write timestamped + latest
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    (REPORT_DIR / f"{stamp}.json").write_text(json.dumps(report, indent=2))
    (REPORT_DIR / "latest.json").write_text(json.dumps(report, indent=2))
    (REPORT_DIR / "latest.md").write_text(render_markdown(report))

    return report


def render_markdown(report: dict) -> str:
    o = report["overall"].upper()
    emoji = {"GREEN": "🟢", "AMBER": "🟠", "RED": "🔴"}.get(o, "⚪")
    lines = [
        f"# Agent Health Report — {report['generated_at']}",
        f"**Overall: {emoji} {o}** · runtime {report['runtime_sec']}s",
        "",
        "## Recommendations",
    ]
    for r in report["recommendations"]:
        lines += [f"### [{r['severity']}] {r['title']}", r["body"], ""]
        if r["action"]:
            lines += [f"**Action:** {r['action']}", ""]

    lines += ["", "## Checks"]
    for k, v in report["checks"].items():
        lines += [f"### {k}", f"- ok: {v.get('ok')}", f"- summary: {v.get('summary', '')}"]
    return "\n".join(lines)


if __name__ == "__main__":
    r = run_agent()
    print(f"Overall: {r['overall'].upper()}   runtime: {r['runtime_sec']}s")
    for rec in r["recommendations"]:
        print(f"  [{rec['severity']}] {rec['title']}")
