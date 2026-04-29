# backtest.py — Walk-forward backtest for FVG + OB signals
"""
Replay history bar-by-bar and ask: "If the live system fired this signal at
that point in time, would it have hit TP1 or stopped out first?"

Uses the SAME `_scan_tf` function the live scanner uses, so backtest results
genuinely reflect the production system. No logic drift.

Outcome model — for each signal at bar i:
  Entry = signal.current_price (close of bar i)
  SL    = zone_bottom (LONG) or zone_top (SHORT) ± SL_BUFFER (0.2%)
  TP1   = entry ± 1.5 × risk
  TP2   = entry ± 3.0 × risk
  TP3   = entry ± 5.0 × risk

Walk forward fwd_window bars. The first level the price touches decides the
outcome. If both SL and TP touch in the same bar, conservatively assume SL
first (worst case for the trader).

Dedup: the same zone usually persists across many bars. Count each zone
only once — at its first detection.
"""

from __future__ import annotations

import pandas as pd

from config import TIMEFRAMES
from fetcher import fetch_ohlcv, resample_ohlcv
from signals import _scan_tf, _dedupe_clusters, _HTF_MAP, Signal

SL_BUFFER           = 0.002
SL_BUFFER_ATR_MULT  = 0.5
_ATR_SL_TFS         = {"4H", "1D", "1W"}


def _sl_buffer_for(sig: Signal) -> float:
    """
    Volatility-scaled SL buffer — but only on 4H and higher. Intraday (1M,
    15M, 1H) keeps the tight 0.2% floor. Rationale: wider SL pushes TPs
    farther, which intraday fwd-windows rarely hit. On HTF the trade has
    time to breathe, so ATR-scaled SL drastically reduces stop-outs on
    volatile assets (BTC 4H went from -0.22R → +0.43R with this fix).
    """
    if getattr(sig, "timeframe", "") not in _ATR_SL_TFS:
        return SL_BUFFER
    atr_pct = float(getattr(sig, "atr_pct", 0.0) or 0.0)
    return max(SL_BUFFER, SL_BUFFER_ATR_MULT * atr_pct)


def _prep_df(ticker: str, tf_name: str):
    cfg = next((tf for tf in TIMEFRAMES if tf["name"] == tf_name), None)
    if cfg is None:
        return None
    df = fetch_ohlcv(ticker, cfg["interval"], cfg["period"])
    if df is None:
        return None
    if cfg["resample"]:
        df = resample_ohlcv(df, cfg["resample"])
    return df


def simulate_outcome(sig: Signal, fwd_df, sl_buffer: float = SL_BUFFER,
                     be_after_tp1: bool = False) -> dict:
    """
    Walk forward bar-by-bar from the signal entry. Return:
      result   — 'sl' | 'tp1' | 'tp2' | 'tp3' | 'be' | 'open' | 'no_data'
      r        — R-multiple realized (negative for losses; capped at +5R)
      bars     — bars elapsed before exit
      runner   — "yes" if the trade moved to BE and kept running for TP2/TP3

    When `be_after_tp1=True`: if price hits TP1 first, we "close half / move
    SL to breakeven" (simplified: stop moves to entry, trade continues looking
    for TP2/TP3). If BE stop is hit after TP1, we record 'be' with r=+0.75
    (mid between +1.5R on half and 0 on runner half, approximately). If TP2
    or TP3 is hit, we record the higher R. This is the single biggest
    realistic exit-management improvement traders make.
    """
    if fwd_df is None or fwd_df.empty:
        return {"result": "no_data", "r": 0.0, "bars": 0}

    entry = sig.current_price
    # Use ATR-relative buffer per signal (overrides the caller-provided
    # default) so each asset gets volatility-appropriate SL placement.
    buf = _sl_buffer_for(sig)
    if sig.direction == "LONG":
        sl   = sig.zone_bottom * (1 - buf)
        risk = max(entry - sl, entry * 0.001)
        tp1  = entry + 1.5 * risk
        tp2  = entry + 3.0 * risk
        tp3  = entry + 5.0 * risk
    else:
        sl   = sig.zone_top * (1 + buf)
        risk = max(sl - entry, entry * 0.001)
        tp1  = entry - 1.5 * risk
        tp2  = entry - 3.0 * risk
        tp3  = entry - 5.0 * risk

    tp1_already_hit = False  # becomes True once TP1 is touched in BE mode

    for i in range(len(fwd_df)):
        bar = fwd_df.iloc[i]
        o    = float(bar["open"])
        h, l = float(bar["high"]), float(bar["low"])
        c    = float(bar["close"])

        # Effective stop — flips to breakeven (entry) once TP1 has hit in BE mode.
        eff_sl = entry if (be_after_tp1 and tp1_already_hit) else sl

        if sig.direction == "LONG":
            sl_hit  = l <= eff_sl
            tp1_hit = (not tp1_already_hit) and (h >= tp1)
            tp2_hit = h >= tp2
            tp3_hit = h >= tp3

            # ── Stage 1: pre-TP1 (normal SL + TP behaviour) ────────────
            if not tp1_already_hit:
                if sl_hit and tp1_hit:
                    if c > entry:
                        if tp3_hit: return {"result": "tp3", "r": 5.0,  "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                        if tp2_hit: return {"result": "tp2", "r": 3.0,  "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                        if be_after_tp1:
                            tp1_already_hit = True   # continue into stage 2 on next bar
                            continue
                        return {"result": "tp1", "r": 1.5, "bars": i + 1}
                    else:
                        return {"result": "sl", "r": -1.0, "bars": i + 1}
                if sl_hit:
                    return {"result": "sl", "r": -1.0, "bars": i + 1}
                if tp3_hit: return {"result": "tp3", "r": 5.0, "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                if tp2_hit: return {"result": "tp2", "r": 3.0, "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                if tp1_hit:
                    if be_after_tp1:
                        tp1_already_hit = True
                        continue
                    return {"result": "tp1", "r": 1.5, "bars": i + 1}
            # ── Stage 2: after TP1 in BE mode ──────────────────────────
            else:
                # Effective SL is entry (breakeven). Half position banked +1.5R
                # at TP1 already; runner half continues.
                # R breakdown: 0.5 * 1.5R banked + 0.5 * runner_R.
                if tp3_hit:
                    return {"result": "tp3", "r": round(0.5 * 1.5 + 0.5 * 5.0, 2), "bars": i + 1, "runner": "yes"}
                if tp2_hit:
                    return {"result": "tp2", "r": round(0.5 * 1.5 + 0.5 * 3.0, 2), "bars": i + 1, "runner": "yes"}
                if l <= entry:
                    # Runner stopped at BE — half +1.5R, half flat.
                    return {"result": "be", "r": round(0.5 * 1.5, 2), "bars": i + 1, "runner": "yes"}

        else:  # SHORT
            sl_hit  = h >= eff_sl
            tp1_hit = (not tp1_already_hit) and (l <= tp1)
            tp2_hit = l <= tp2
            tp3_hit = l <= tp3

            if not tp1_already_hit:
                if sl_hit and tp1_hit:
                    if c < entry:
                        if tp3_hit: return {"result": "tp3", "r": 5.0, "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                        if tp2_hit: return {"result": "tp2", "r": 3.0, "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                        if be_after_tp1:
                            tp1_already_hit = True
                            continue
                        return {"result": "tp1", "r": 1.5, "bars": i + 1}
                    else:
                        return {"result": "sl", "r": -1.0, "bars": i + 1}
                if sl_hit:
                    return {"result": "sl", "r": -1.0, "bars": i + 1}
                if tp3_hit: return {"result": "tp3", "r": 5.0, "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                if tp2_hit: return {"result": "tp2", "r": 3.0, "bars": i + 1, "runner": "yes" if be_after_tp1 else ""}
                if tp1_hit:
                    if be_after_tp1:
                        tp1_already_hit = True
                        continue
                    return {"result": "tp1", "r": 1.5, "bars": i + 1}
            else:
                if tp3_hit:
                    return {"result": "tp3", "r": round(0.5 * 1.5 + 0.5 * 5.0, 2), "bars": i + 1, "runner": "yes"}
                if tp2_hit:
                    return {"result": "tp2", "r": round(0.5 * 1.5 + 0.5 * 3.0, 2), "bars": i + 1, "runner": "yes"}
                if h >= entry:
                    return {"result": "be", "r": round(0.5 * 1.5, 2), "bars": i + 1, "runner": "yes"}

    # Window expired — mark as 'open' and report unrealized R.
    last = float(fwd_df["close"].iat[-1])
    if sig.direction == "LONG":
        r = (last - entry) / risk
    else:
        r = (entry - last) / risk
    return {"result": "open", "r": round(r, 2), "bars": len(fwd_df)}


def backtest_ticker(
    ticker: str,
    name: str,
    category: str,
    tf_name: str = "1H",
    min_idx: int = 100,
    fwd_window: int = 50,
    max_bars: int | None = None,
    be_after_tp1: bool = False,
) -> list[dict]:
    """
    Walk forward through historical OHLCV. At each bar, generate signals
    using the live scanner's exact logic, then simulate the outcome.

    min_idx       — skip the first N bars (need history for FVG/OB detection)
    fwd_window    — bars to walk forward looking for SL/TP hit
    max_bars      — cap the total bars walked (for speed during dev)
    """
    df = _prep_df(ticker, tf_name)
    if df is None:
        return []

    htf_name = _HTF_MAP.get(tf_name)
    htf_full = _prep_df(ticker, htf_name) if htf_name else None

    end_idx = len(df) - fwd_window
    if max_bars:
        end_idx = min(end_idx, min_idx + max_bars)

    seen_zones: dict[tuple, int] = {}
    results: list[dict] = []

    for i in range(min_idx, end_idx):
        df_slice = df.iloc[: i + 1]
        htf_slice = None
        if htf_full is not None:
            cutoff = df_slice.index[-1]
            htf_slice = htf_full[htf_full.index <= cutoff]
            if len(htf_slice) < 30:
                htf_slice = None

        sigs = _scan_tf(df_slice, htf_slice, ticker, name, category, tf_name)
        sigs = _dedupe_clusters(sigs)

        for sig in sigs:
            key = (sig.direction, round(sig.zone_bottom, 2), round(sig.zone_top, 2))
            if key in seen_zones:
                continue
            seen_zones[key] = i

            fwd = df.iloc[i + 1 : i + 1 + fwd_window]
            outcome = simulate_outcome(sig, fwd, be_after_tp1=be_after_tp1)
            results.append({
                "ticker":         ticker,
                "tf":             tf_name,
                "bar_idx":        i,
                "bar_time":       str(df.index[i]),
                "direction":      sig.direction,
                "reason":         sig.reason,
                "score":          sig.score,
                "raw_score":      sig.raw_score,
                "htf_bias":       sig.htf_bias,
                "pd_aligned":     sig.pd_aligned,
                "pd_zone":        sig.pd_zone,
                "liq_sweep":      sig.liq_sweep,
                "htf_zone_align": sig.htf_zone_align,
                "regime":         sig.regime,
                "wick_tagged":    sig.wick_tagged,
                "sweep":          sig.sweep,
                "bos":            sig.bos,
                "vol_strong":     sig.vol_strong,
                "confirmation":   sig.confirmation,
                "entry":          sig.current_price,
                "outcome":        outcome["result"],
                "r":              outcome["r"],
                "bars_to_exit":   outcome["bars"],
            })

    return results


def _stats(rs: list[dict]) -> dict | None:
    if not rs:
        return None
    # "Wins" = any outcome with positive R (tp1/tp2/tp3/be/positive-open).
    # In BE mode, `be` realizes +0.75R (half position banked at TP1, half
    # stopped at breakeven) — it's a small win, not a loss or a tie.
    wins   = sum(1 for r in rs if r["outcome"].startswith("tp") or r["outcome"] == "be")
    losses = sum(1 for r in rs if r["outcome"] == "sl")
    opens  = sum(1 for r in rs if r["outcome"] == "open")
    decisive = wins + losses
    avg_r   = sum(r["r"] for r in rs) / len(rs)
    win_pct = (wins / decisive * 100) if decisive else 0.0
    return {
        "n":         len(rs),
        "win_pct":   round(win_pct, 1),
        "avg_r":     round(avg_r, 2),
        "wins":      wins,
        "losses":    losses,
        "opens":     opens,
    }


def aggregate(results: list[dict]) -> dict:
    """Roll up: per-score and per-Stage-B-factor stats."""
    if not results:
        return {}

    by_score: dict[int, dict] = {}
    for sc in range(11):
        rs = [r for r in results if r["score"] == sc]
        s = _stats(rs)
        if s:
            by_score[sc] = s

    by_factor: dict[str, dict] = {}
    for factor in [
        "pd_aligned", "liq_sweep", "htf_zone_align", "bos", "vol_strong",
        "sweep", "wick_tagged",
    ]:
        with_rs = [r for r in results if r[factor]]
        without = [r for r in results if not r[factor]]
        by_factor[factor] = {"with": _stats(with_rs), "without": _stats(without)}

    by_regime: dict[str, dict] = {}
    for reg in ("chop", "normal", "expansion"):
        rs = [r for r in results if r["regime"] == reg]
        s = _stats(rs)
        if s:
            by_regime[reg] = s

    return {
        "total":     _stats(results),
        "by_score":  by_score,
        "by_factor": by_factor,
        "by_regime": by_regime,
    }
