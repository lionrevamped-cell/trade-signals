#!/usr/bin/env python3
"""
auto_learn.py — Daily weight tuner. Pure statistics, no LLM.

Reads outcomes.json (every signal the live scanner has tracked, plus its
realized R-multiple), computes the actual edge each scoring factor is
contributing right now, and nudges the scoring weights one step toward
what the data says.

Why this is safe:
  • Bounded: weights clamped to [-2, +2]
  • Smoothed: at most ±1 step per run, so a fluky day can't crash weights
  • Gated: requires ≥30 resolved trades on each side of every factor
            and ≥100 total resolved trades before any change
  • Reversible: writes to learned_weights.json. Delete that file and the
                 system falls back to the recalibrated defaults
  • Logged: every update is appended to learn_history.json
  • Never modifies code — signals.py reads from learned_weights.json at
    runtime; the scoring formula stays under human control

Tunable factors (binary on/off in scoring):
  pd_aligned, wick_tagged, bos, liq_sweep, htf_zone_align,
  vol_strong, sweep, regime_expansion, regime_chop_intraday

NOT tunable (structural / interaction effects):
  HTF bias buckets, score caps, strength weight, freshness, proximity
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_DIR     = Path(__file__).parent
OUTCOMES_FILE   = PROJECT_DIR / "outcomes.json"
WEIGHTS_FILE    = PROJECT_DIR / "learned_weights.json"
HISTORY_FILE    = PROJECT_DIR / "learn_history.json"
PAUSE_FLAG_FILE = PROJECT_DIR / ".learn_paused"   # touch this to disable updates

# ── Tunable factors ───────────────────────────────────────────────────────────
# Each factor is a boolean column in outcomes.json. The default weight is
# what we landed on after the manual recalibration in v5.
DEFAULT_WEIGHTS: dict[str, int] = {
    "pd_aligned":     +1,
    "wick_tagged":    +1,
    "bos":            +1,
    "liq_sweep":      -1,
    "htf_zone_align": -1,
    "vol_strong":      0,
    "sweep":           0,
}

WEIGHT_MIN          = -2
WEIGHT_MAX          = +2
MIN_SAMPLE_PER_SIDE = 30      # need at least this many WITH and WITHOUT
MIN_TOTAL_TRADES    = 100     # gate for any update at all
MAX_STEP_PER_RUN    = 1       # never move more than one unit per day
LOOKBACK_DAYS       = 30      # rolling window
MIN_DELTA_R         = 0.20    # Δ avg R below this is treated as noise — don't move

HISTORY_MAX_ENTRIES = 60


# ── helpers ───────────────────────────────────────────────────────────────────

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


def _load_current_weights() -> dict[str, int]:
    """Read learned_weights.json, fall back to defaults."""
    if WEIGHTS_FILE.exists():
        try:
            with open(WEIGHTS_FILE) as f:
                data = json.load(f)
            w = data.get("weights", {})
            # Sanity-check every factor is in range
            for k, v in w.items():
                if not isinstance(v, int) or not (WEIGHT_MIN <= v <= WEIGHT_MAX):
                    return dict(DEFAULT_WEIGHTS)
            # Fill any missing factors with default
            return {k: w.get(k, dv) for k, dv in DEFAULT_WEIGHTS.items()}
        except Exception:
            pass
    return dict(DEFAULT_WEIGHTS)


def _load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _append_history(entry: dict) -> None:
    history = _load_history()
    history.append(entry)
    history = history[-HISTORY_MAX_ENTRIES:]
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def _save_weights(weights: dict[str, int], meta: dict) -> None:
    out = {
        "weights":    weights,
        "updated_at": _now_iso(),
        **meta,
    }
    WEIGHTS_FILE.write_text(json.dumps(out, indent=2))


# ── core: per-factor Δ avg R ──────────────────────────────────────────────────

def _delta_avg_r(trades: list[dict], factor: str) -> tuple[int, int, float, float]:
    """Return (n_with, n_without, avg_r_with, avg_r_without) for a factor."""
    with_rs    = [t for t in trades if t.get(factor)]
    without_rs = [t for t in trades if not t.get(factor)]
    if not with_rs or not without_rs:
        return len(with_rs), len(without_rs), 0.0, 0.0
    avg_w  = sum(t.get("r") or 0 for t in with_rs)    / len(with_rs)
    avg_wo = sum(t.get("r") or 0 for t in without_rs) / len(without_rs)
    return len(with_rs), len(without_rs), avg_w, avg_wo


def _last_run_was_today() -> bool:
    """True if the most recent learn run was within the last 20 hours.
    Prevents duplicate runs when the user opens/closes the scanner multiple
    times per day."""
    history = _load_history()
    if not history:
        return False
    try:
        last = datetime.fromisoformat(history[-1]["generated_at"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last) < timedelta(hours=20)
    except Exception:
        return False


def learn(lookback_days: int = LOOKBACK_DAYS, force: bool = False) -> dict:
    """
    Run one learning cycle. Returns a structured report describing what
    changed (or what blocked us from changing).

    Skips silently if a run has already happened in the last 20h, unless
    force=True (called from the manual UI button).
    """
    started = _now_iso()

    if not force and _last_run_was_today():
        return {
            "ok":          True,
            "applied":     False,
            "reason":      "Already ran within the last 20h — skipped (one auto-run per day)",
            "generated_at": started,
        }

    if PAUSE_FLAG_FILE.exists():
        return {
            "ok":          True,
            "applied":     False,
            "reason":      "Auto-learn is PAUSED (delete .learn_paused to resume)",
            "generated_at": started,
        }

    outcomes_all = _load_outcomes()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    resolved = [
        r for r in outcomes_all
        if r.get("first_seen", "") >= cutoff
        and r.get("outcome") in ("tp1", "tp2", "tp3", "sl", "be", "expired")
    ]

    if len(resolved) < MIN_TOTAL_TRADES:
        return {
            "ok":          True,
            "applied":     False,
            "reason":      f"Need ≥{MIN_TOTAL_TRADES} resolved trades; have {len(resolved)}",
            "trade_count": len(resolved),
            "generated_at": started,
        }

    current = _load_current_weights()
    new_weights = dict(current)
    factor_log: list[dict] = []

    for factor, _default in DEFAULT_WEIGHTS.items():
        n_with, n_without, avg_w, avg_wo = _delta_avg_r(resolved, factor)
        if n_with < MIN_SAMPLE_PER_SIDE or n_without < MIN_SAMPLE_PER_SIDE:
            factor_log.append({
                "factor":   factor,
                "skipped":  True,
                "reason":   f"sample too small (with={n_with}, without={n_without})",
                "current":  current[factor],
                "applied":  current[factor],
            })
            continue

        delta = avg_w - avg_wo
        cur_w = current[factor]

        # Noise gate: tiny Δ R → don't move. Prevents normal noise from
        # drifting weights toward 0 during stable periods.
        if abs(delta) < MIN_DELTA_R:
            new_w = cur_w
            recommended = cur_w
            noise_skipped = True
        else:
            # Map empirical Δ avg R → weight magnitude (in score points).
            # 0.20–0.50 R/trade  → ±1  (small but real edge)
            # 0.50–1.00 R/trade  → ±2  (clear edge)
            # ≥1.00              → ±2  (cap)
            sign = 1 if delta > 0 else -1
            mag = abs(delta)
            recommended = sign * (1 if mag < 0.5 else 2)
            recommended = max(WEIGHT_MIN, min(WEIGHT_MAX, recommended))

            # Smooth: move at most MAX_STEP_PER_RUN per day.
            if recommended > cur_w:
                new_w = min(cur_w + MAX_STEP_PER_RUN, recommended)
            elif recommended < cur_w:
                new_w = max(cur_w - MAX_STEP_PER_RUN, recommended)
            else:
                new_w = cur_w
            noise_skipped = False

        new_weights[factor] = new_w
        factor_log.append({
            "factor":      factor,
            "n_with":      n_with,
            "n_without":   n_without,
            "avg_r_with":  round(avg_w, 3),
            "avg_r_wo":    round(avg_wo, 3),
            "delta_avg_r": round(delta, 3),
            "noise":       noise_skipped,
            "recommended": recommended,
            "current":     cur_w,
            "applied":     new_w,
            "changed":     new_w != cur_w,
        })

    changed_factors = [f for f in factor_log if f.get("changed")]
    applied = bool(changed_factors)

    if applied:
        _save_weights(new_weights, {
            "trade_count":    len(resolved),
            "lookback_days":  lookback_days,
            "factors_changed": [f["factor"] for f in changed_factors],
        })

    report = {
        "ok":            True,
        "applied":       applied,
        "trade_count":   len(resolved),
        "lookback_days": lookback_days,
        "weights_after": new_weights,
        "factors":       factor_log,
        "generated_at":  started,
    }
    _append_history(report)
    return report


def current_weights() -> dict:
    """For UI: return current learned weights + when they were last updated."""
    cur = _load_current_weights()
    meta = {}
    if WEIGHTS_FILE.exists():
        try:
            with open(WEIGHTS_FILE) as f:
                meta = json.load(f)
        except Exception:
            pass
    return {
        "weights":     cur,
        "defaults":    DEFAULT_WEIGHTS,
        "updated_at":  meta.get("updated_at"),
        "trade_count": meta.get("trade_count"),
        "paused":      PAUSE_FLAG_FILE.exists(),
    }


def history(limit: int = 30) -> list[dict]:
    return _load_history()[-limit:]


def pause(paused: bool) -> bool:
    """Pause / unpause the learner via filesystem flag."""
    if paused:
        PAUSE_FLAG_FILE.write_text(_now_iso())
    else:
        try:
            PAUSE_FLAG_FILE.unlink()
        except FileNotFoundError:
            pass
    return PAUSE_FLAG_FILE.exists()


if __name__ == "__main__":
    r = learn()
    if r.get("applied"):
        print(f"✓ Updated weights: {[f['factor'] for f in r['factors'] if f.get('changed')]}")
    else:
        print(f"– No update: {r.get('reason', '(no factor changed)')} "
              f"(trades available: {r.get('trade_count', 0)})")
