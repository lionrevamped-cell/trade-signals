# signals.py — Combine FVG + OB hits into actionable trade signals

from __future__ import annotations
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from config import (
    TIMEFRAMES, PROXIMITY_PCT, MIN_FVG_PCT, OB_LOOKFORWARD, OB_MOVE_PCT,
    ATR_PROXIMITY_MULT, ATR_MIN_FVG_MULT,
)
from fetcher import fetch_ohlcv, resample_ohlcv
from indicators import (
    detect_fvgs, detect_obs, zones_at_price,
    zone_touch_count, detect_sweep, detect_confirmation,
    htf_trend_strength, detect_bos, atr,
    pd_zone, liquidity_pools, detect_liquidity_sweep, regime,
)

# ── Learned scoring weights ───────────────────────────────────────────────────
# auto_learn.py rewrites learned_weights.json daily. We read it every time
# we score (cheap — small dict from JSON). Falls back to recalibrated
# defaults if missing/corrupt. This is how the system "self-tunes" without
# touching any code.
import json as _json_for_weights
from pathlib import Path as _Path_for_weights

_DEFAULT_TUNABLE_WEIGHTS = {
    "pd_aligned":     +1,
    "wick_tagged":    +1,
    "bos":            +1,
    "liq_sweep":      -1,
    "htf_zone_align": -1,
    "vol_strong":      0,
    "sweep":           0,
}

def _load_learned_weights() -> dict:
    p = _Path_for_weights(__file__).parent / "learned_weights.json"
    if not p.exists():
        return dict(_DEFAULT_TUNABLE_WEIGHTS)
    try:
        with open(p) as f:
            data = _json_for_weights.load(f)
        w = data.get("weights", {})
        # Validate: integers in [-2, 2]; missing factors take the default.
        out = dict(_DEFAULT_TUNABLE_WEIGHTS)
        for k, default in _DEFAULT_TUNABLE_WEIGHTS.items():
            v = w.get(k, default)
            if isinstance(v, int) and -2 <= v <= 2:
                out[k] = v
        return out
    except Exception:
        return dict(_DEFAULT_TUNABLE_WEIGHTS)

# Loaded fresh on every scan (called from _scan_tf), so a weight update
# during the day takes effect immediately on the next scan.

# ── BTC momentum cache — used to down-score alt signals that fight BTC ───────
# Recomputed once per scan by refresh_btc_momentum(). 'up'/'down'/'neutral'.
_BTC_MOMENTUM = {"ret_4h_pct": 0.0, "state": "neutral"}
BTC_STRONG_MOVE_PCT = 2.0  # a ≥2% 4h BTC move counts as "strong"
_CRYPTO_CATEGORIES = {"Crypto"}


def refresh_btc_momentum() -> dict:
    """
    Fetch BTC-USD 1H data and compute the trailing 4-hour return.
    Called once at the start of each full scan so all altcoin signals
    see a consistent BTC context.
    """
    df = fetch_ohlcv("BTC-USD", "1h", "1d")
    if df is None or len(df) < 5:
        _BTC_MOMENTUM.update(ret_4h_pct=0.0, state="neutral")
        return dict(_BTC_MOMENTUM)
    try:
        cur  = float(df["close"].iat[-1])
        past = float(df["close"].iat[-5])   # 4 bars ago = 4 hours on 1H data
        ret  = (cur - past) / past * 100 if past else 0.0
    except Exception:
        ret = 0.0
    if ret >= BTC_STRONG_MOVE_PCT:       state = "up"
    elif ret <= -BTC_STRONG_MOVE_PCT:    state = "down"
    else:                                state = "neutral"
    _BTC_MOMENTUM.update(ret_4h_pct=round(ret, 2), state=state)
    return dict(_BTC_MOMENTUM)


def btc_alt_penalty(direction: str, category: str, ticker: str) -> int:
    """
    −1 point when an altcoin LONG is fighting a strong BTC dump,
    or an altcoin SHORT is fighting a strong BTC rip. Returns 0 otherwise
    (including for BTC itself and non-crypto assets).
    """
    if category not in _CRYPTO_CATEGORIES or ticker == "BTC-USD":
        return 0
    st = _BTC_MOMENTUM["state"]
    if st == "down" and direction == "LONG":
        return -1
    if st == "up" and direction == "SHORT":
        return -1
    return 0


@dataclass
class Signal:
    category:      str
    ticker:        str
    name:          str
    direction:     Literal["LONG", "SHORT"]
    timeframe:     str
    zone_bottom:   float
    zone_top:      float
    current_price: float
    reason:        str          # "FVG", "OB", "FVG+OB"
    strength:      int          # 1 = single zone, 2 = FVG+OB overlap
    dist_pct:      float        # % distance price → zone midpoint
    touch_count:   int          # 0 = fresh first visit
    htf_bias:      str          # 5-level: strong_aligned|aligned|neutral|against|strong_against
    sweep:         bool         # liquidity sweep detected at zone
    confirmation:  str          # "engulfing"|"pin_bar"|"doji"|""
    score:         int          # 0–10 overall trade quality
    zone_age:      int          # candles since zone formation (lower = fresher)
    vol_strong:    bool         # zone formed on above-average volume (institutional)
    bos:           bool         # Break of Structure confirmed in trade direction
    wick_tagged:   bool = False # zone wick-tested already (less fresh than zone_age implies)
    raw_score:     int = 0      # pre-cap additive score; final `score` may be HTF-capped lower
    # ── Stage B: ICT context layer ───────────────────────────────────────────
    pd_zone:        str  = ""    # "discount" | "equilibrium" | "premium" | ""
    pd_aligned:     bool = False # LONG in discount or SHORT in premium
    liq_sweep:      bool = False # real liquidity-pool sweep (equal H/L), distinct from zone-edge sweep
    htf_zone_align: bool = False # current zone falls inside a same-direction HTF zone (A+ setup)
    regime:         str  = ""    # "chop" | "normal" | "expansion" | ""
    atr_pct:        float = 0.0  # ATR / price — used for volatility-relative SL buffer


_HTF_MAP = {"1M": "1H", "15M": "1H", "1H": "4H", "4H": "1D", "1D": None}


def _htf_alignment(direction: str, trend_strength: str) -> str:
    """
    Map 5-level HTF trend to alignment label for this trade direction.

    strong_aligned  = EMA21 + EMA50 both lined up with our trade (strongest)
    aligned         = EMA21 lines up but EMA50 not fully confirmed
    neutral         = price hovering around EMAs
    against         = trading against EMA21
    strong_against  = fighting both EMA21 and EMA50 (highest risk)
    """
    if trend_strength == "neutral":
        return "neutral"

    if direction == "LONG":
        if trend_strength == "strong_bullish": return "strong_aligned"
        if trend_strength == "bullish":        return "aligned"
        if trend_strength == "bearish":        return "against"
        if trend_strength == "strong_bearish": return "strong_against"
    else:
        if trend_strength == "strong_bearish": return "strong_aligned"
        if trend_strength == "bearish":        return "aligned"
        if trend_strength == "bullish":        return "against"
        if trend_strength == "strong_bullish": return "strong_against"

    return "neutral"


def _compute_score(
    strength: int,
    htf: str,
    tc: int,
    dist_pct: float,
    sweep: bool,
    confirmation: str,
    bos: bool,
    vol_strong: bool,
    wick_tagged: bool,
    pd_aligned: bool,
    pd_zone_v: str,
    direction: str,
    liq_sweep: bool,
    htf_zone_align: bool,
    regime_v: str,
    timeframe: str,
    btc_penalty: int = 0,
) -> tuple[int, int]:
    """
    0–10 trade quality score — recalibrated from backtest findings.

    Backtest run 2026-04-24 on 260 signals across BTC/ETH/SOL/AAPL/NVDA
    (1H + 4H) revealed the ORIGINAL scoring was inverted on several factors.
    Weights below flipped to match observed edge (Δ win%):

      Factor              Δ win%    Old weight  →  New weight
      PD aligned          +6.3      +1             +1  (kept — works)
      BOS                 +3.0      +1             +1  (kept — works)
      Wick tagged         +5.6      -1             +1  (FLIPPED — now reward)
      Liq sweep           -6.4      +2             -1  (FLIPPED — penalize)
      HTF zone align      -7.1      +1             -1  (FLIPPED — penalize)
      Vol strong          -1.3      +1              0  (dropped — no edge)
      Zone-edge sweep     -5.4      +1              0  (dropped — no edge)

    HTF bias was also inverted. Observed win rates:
      aligned         44.7%  +0.30R  → BEST  (was +2 pts, now +3)
      strong_against  39.2%  +0.12R           (was 0 pts, now +2)
      against         38.7%  +0.09R           (was 0 pts, now +2)
      neutral                                   (+1)
      strong_aligned  27.7%  -0.12R  → WORST (was +3 pts, now 0, and capped at 6)

    Max achievable: FVG+OB (4) + aligned (3) + fresh (2) + wick (1) + proximity (1)
                  + confirmation (1) + BOS (1) + PD aligned (1) + expansion (1) = 15 → clamp to 10
    """
    pts = 4 if strength == 2 else 2

    # HTF bias — calibrated, NOT auto-tuned (structural caps depend on it).
    if htf == "aligned":              pts += 3   # BEST bucket
    elif htf == "against":            pts += 2
    elif htf == "strong_against":     pts += 2
    elif htf == "neutral":            pts += 1
    # strong_aligned gets 0 (was +3), AND is capped below at 6.

    pts += 2 if tc == 0 else (1 if tc == 1 else 0)
    pts += 1 if dist_pct < 0.5 else 0
    pts += 1 if confirmation else 0

    # ── Tunable factors (auto-learned daily from outcomes.json) ──────────────
    # The W dict is reloaded from learned_weights.json on every score, so
    # auto_learn.py's nightly updates take effect on the next scan without
    # any code change. Falls back to recalibrated defaults if file missing.
    W = _load_learned_weights()

    if pd_aligned:
        pts += W["pd_aligned"]
    else:
        if pd_zone_v in ("discount", "premium"):
            wrong_side = (
                (direction == "LONG"  and pd_zone_v == "premium") or
                (direction == "SHORT" and pd_zone_v == "discount")
            )
            if wrong_side:
                pts -= W["pd_aligned"] if W["pd_aligned"] > 0 else 1

    if wick_tagged:    pts += W["wick_tagged"]
    if bos:            pts += W["bos"]
    if liq_sweep:      pts += W["liq_sweep"]
    if htf_zone_align: pts += W["htf_zone_align"]
    if vol_strong:     pts += W["vol_strong"]
    if sweep:          pts += W["sweep"]

    if regime_v == "expansion":
        pts += 1
    elif regime_v == "chop" and timeframe in ("1M", "15M"):
        pts -= 1

    # BTC correlation: an alt LONG during a BTC dump (or alt SHORT during a
    # BTC pump) is likely about to catch the correlated move against it.
    # Penalize by -1. No effect for BTC itself or non-crypto.
    pts += btc_penalty

    raw = max(0, min(10, pts))
    capped = raw

    # Cap the BAD bucket (strong_aligned 27.7% win, −0.12R).
    if htf == "strong_aligned":
        capped = min(capped, 6)

    return capped, raw


def _zones_overlap(a: dict, b: dict) -> bool:
    return a["bottom"] < b["top"] and a["top"] > b["bottom"]


# ── Cluster suppression ───────────────────────────────────────────────────────

_TF_RANK = {"1M": 1, "15M": 2, "1H": 3, "4H": 4, "1D": 5}


def _dedupe_clusters(signals: list[Signal]) -> list[Signal]:
    """
    Same zone visible on 1H and 4H is one trade, not two.
    Group same-direction signals whose price zones overlap. The cluster is
    transitive: if A overlaps B and B overlaps C, all three group together
    (even if A doesn't directly overlap C).

    Keep the highest-score representative, with TF as tiebreaker, then
    closest distance. Score wins because for a leverage trader an A+ entry
    on 4H is more useful than a mediocre re-statement of the same zone on 1D.
    """
    if len(signals) <= 1:
        return signals

    def overlap_ratio(a: Signal, b: Signal) -> float:
        lo = max(a.zone_bottom, b.zone_bottom)
        hi = min(a.zone_top,    b.zone_top)
        if hi <= lo:
            return 0.0
        a_h = a.zone_top - a.zone_bottom
        b_h = b.zone_top - b.zone_bottom
        denom = min(a_h, b_h) or 1e-12
        return (hi - lo) / denom

    used   = [False] * len(signals)
    kept: list[Signal] = []

    # Score is primary so a high-quality entry is never dropped just because
    # a higher TF has a mediocre overlapping zone.
    def rank(s: Signal) -> tuple:
        return (s.score, _TF_RANK.get(s.timeframe, 0), -s.dist_pct)

    for i, s in enumerate(signals):
        if used[i]:
            continue
        cluster = [s]
        used[i] = True
        # Transitive expansion: re-scan for new overlaps as the cluster grows.
        changed = True
        while changed:
            changed = False
            for j in range(len(signals)):
                if used[j]:
                    continue
                t = signals[j]
                if t.direction != s.direction:
                    continue
                if any(overlap_ratio(t, c) >= 0.25 for c in cluster):
                    cluster.append(t)
                    used[j] = True
                    changed = True
        cluster.sort(key=rank, reverse=True)
        kept.append(cluster[0])

    return kept


# ── Per-TF scan (reusable: live + backtest call this exact function) ──────────

def _scan_tf(
    df,
    htf_df,
    ticker: str,
    name: str,
    category: str,
    tf_name: str,
) -> list[Signal]:
    """
    Generate signals for a single timeframe at the latest bar of `df`.
    Used by both the live scanner and the walk-forward backtest — same logic
    in both, so backtest results genuinely reflect what the live system did.

    `df` and `htf_df` should already be resampled to their target TF.
    """
    signals: list[Signal] = []
    if df is None or len(df) < 30:
        return signals

    price = float(df["close"].iat[-1])
    trend_strength = htf_trend_strength(htf_df)

    # ── ATR-relative thresholds (per-asset, per-TF) ──────────────────────────
    a = atr(df)
    if a > 0 and price > 0:
        atr_pct       = a / price
        proximity_pct = max(PROXIMITY_PCT, atr_pct * ATR_PROXIMITY_MULT)
        min_fvg_pct   = max(MIN_FVG_PCT,   atr_pct * ATR_MIN_FVG_MULT)
    else:
        atr_pct       = 0.0
        proximity_pct = PROXIMITY_PCT
        min_fvg_pct   = MIN_FVG_PCT

    fvgs = detect_fvgs(df, min_gap_pct=min_fvg_pct)
    obs  = detect_obs(
        df,
        lookforward=OB_LOOKFORWARD,
        move_pct=OB_MOVE_PCT,
        min_gap_pct=min_fvg_pct,
        require_displacement=True,
    )

    fvgs_hit = zones_at_price(fvgs, price, proximity=proximity_pct)
    obs_hit  = zones_at_price(obs,  price, proximity=proximity_pct)

    # ── Stage B context (computed once per TF, reused per signal) ────────────
    pd_v       = pd_zone(price, df)
    pools_v    = liquidity_pools(df)
    regime_v   = regime(df)

    htf_fvgs: list[dict] = []
    htf_obs:  list[dict] = []
    if htf_df is not None:
        htf_a    = atr(htf_df)
        htf_p    = float(htf_df["close"].iat[-1]) if len(htf_df) else 0.0
        htf_atr_pct = (htf_a / htf_p) if (htf_a > 0 and htf_p > 0) else 0
        htf_min  = max(MIN_FVG_PCT, htf_atr_pct * ATR_MIN_FVG_MULT)
        htf_fvgs = detect_fvgs(htf_df, min_gap_pct=htf_min)
        htf_obs  = detect_obs(
            htf_df, lookforward=OB_LOOKFORWARD, move_pct=OB_MOVE_PCT,
            min_gap_pct=htf_min, require_displacement=True,
        )

    def _htf_zone_aligned(zone: dict, kind: str) -> bool:
        mid = (zone["bottom"] + zone["top"]) / 2
        for hz in htf_fvgs + htf_obs:
            if hz["kind"] == kind and hz["bottom"] <= mid <= hz["top"]:
                return True
        return False

    added_obs: set[int] = set()

    for fvg in fvgs_hit:
        direction = "LONG" if fvg["kind"] == "bullish" else "SHORT"
        mid  = fvg["mid"]
        dist = abs(price - mid) / mid * 100

        overlap_obs = [
            ob for ob in obs_hit
            if ob["kind"] == fvg["kind"] and _zones_overlap(fvg, ob)
        ]
        reason   = "FVG+OB" if overlap_obs else "FVG"
        strength = 2 if overlap_obs else 1

        for ob in overlap_obs:
            added_obs.add(ob["idx"])

        tc        = zone_touch_count(df, fvg)
        htf       = _htf_alignment(direction, trend_strength)
        sweep     = detect_sweep(df, fvg)
        confirm   = detect_confirmation(df, fvg["kind"])
        bos       = detect_bos(df, fvg["kind"])
        vol_str   = fvg.get("vol_strong", False)
        age       = int(fvg.get("zone_age", 0))
        wick_tag  = bool(fvg.get("wick_tagged", False))
        pd_align  = (
            (direction == "LONG"  and pd_v == "discount") or
            (direction == "SHORT" and pd_v == "premium")
        )
        liq_sw    = detect_liquidity_sweep(df, pools_v, direction)
        htf_align = _htf_zone_aligned(fvg, fvg["kind"])
        btc_pen   = btc_alt_penalty(direction, category, ticker)
        score, raw_score = _compute_score(
            strength, htf, tc, dist, sweep, confirm, bos, vol_str, wick_tag,
            pd_align, pd_v, direction, liq_sw, htf_align, regime_v, tf_name,
            btc_penalty=btc_pen,
        )

        signals.append(Signal(
            category=category, ticker=ticker, name=name,
            direction=direction, timeframe=tf_name,
            zone_bottom=round(fvg["bottom"], 6),
            zone_top=round(fvg["top"], 6),
            current_price=round(price, 6),
            reason=reason, strength=strength,
            dist_pct=round(dist, 2),
            touch_count=tc, htf_bias=htf,
            sweep=sweep, confirmation=confirm,
            score=score,
            zone_age=age,
            vol_strong=vol_str,
            bos=bos,
            wick_tagged=wick_tag,
            raw_score=raw_score,
            pd_zone=pd_v,
            pd_aligned=pd_align,
            liq_sweep=liq_sw,
            htf_zone_align=htf_align,
            regime=regime_v,
            atr_pct=atr_pct,
        ))

    for ob in obs_hit:
        if ob["idx"] in added_obs:
            continue
        direction = "LONG" if ob["kind"] == "bullish" else "SHORT"
        mid  = (ob["bottom"] + ob["top"]) / 2
        dist = abs(price - mid) / mid * 100

        tc        = zone_touch_count(df, ob)
        htf       = _htf_alignment(direction, trend_strength)
        sweep     = detect_sweep(df, ob)
        confirm   = detect_confirmation(df, ob["kind"])
        bos       = detect_bos(df, ob["kind"])
        vol_str   = ob.get("vol_strong", False)
        age       = int(ob.get("zone_age", 0))
        wick_tag  = bool(ob.get("wick_tagged", False))
        pd_align  = (
            (direction == "LONG"  and pd_v == "discount") or
            (direction == "SHORT" and pd_v == "premium")
        )
        liq_sw    = detect_liquidity_sweep(df, pools_v, direction)
        htf_align = _htf_zone_aligned(ob, ob["kind"])
        btc_pen   = btc_alt_penalty(direction, category, ticker)
        score, raw_score = _compute_score(
            1, htf, tc, dist, sweep, confirm, bos, vol_str, wick_tag,
            pd_align, pd_v, direction, liq_sw, htf_align, regime_v, tf_name,
            btc_penalty=btc_pen,
        )

        signals.append(Signal(
            category=category, ticker=ticker, name=name,
            direction=direction, timeframe=tf_name,
            zone_bottom=round(ob["bottom"], 6),
            zone_top=round(ob["top"], 6),
            current_price=round(price, 6),
            reason="OB", strength=1,
            dist_pct=round(dist, 2),
            touch_count=tc, htf_bias=htf,
            sweep=sweep, confirmation=confirm,
            score=score,
            zone_age=age,
            vol_strong=vol_str,
            bos=bos,
            wick_tagged=wick_tag,
            raw_score=raw_score,
            pd_zone=pd_v,
            pd_aligned=pd_align,
            liq_sweep=liq_sw,
            htf_zone_align=htf_align,
            regime=regime_v,
            atr_pct=atr_pct,
        ))

    return signals


# ── Per-asset scan ────────────────────────────────────────────────────────────

def scan_asset(ticker: str, name: str, category: str) -> list[Signal]:
    """
    Live scanner: fetch all TFs, generate signals via _scan_tf for each TF,
    then dedupe across TFs.
    """
    tf_data: dict[str, object] = {}
    for tf in TIMEFRAMES:
        df = fetch_ohlcv(ticker, tf["interval"], tf["period"])
        if df is None:
            tf_data[tf["name"]] = None
            continue
        if tf["resample"]:
            df = resample_ohlcv(df, tf["resample"])
        tf_data[tf["name"]] = df

    signals: list[Signal] = []
    for tf in TIMEFRAMES:
        df       = tf_data.get(tf["name"])
        htf_df   = tf_data.get(_HTF_MAP.get(tf["name"])) if _HTF_MAP.get(tf["name"]) else None
        signals.extend(_scan_tf(df, htf_df, ticker, name, category, tf["name"]))

    return _dedupe_clusters(signals)


def scan_all(assets: dict, max_workers: int = 12) -> list[Signal]:
    # Compute BTC's short-term momentum ONCE before scanning so every alt
    # signal sees the same BTC context for correlation penalties.
    refresh_btc_momentum()

    flat = [
        (ticker, name, cat)
        for cat, items in assets.items()
        for ticker, name in items
    ]
    all_signals: list[Signal] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(scan_asset, t, n, c): t for t, n, c in flat}
        for fut in as_completed(futures):
            try:
                all_signals.extend(fut.result())
            except Exception:
                pass
    all_signals.sort(key=lambda s: (-s.score, s.dist_pct))
    return all_signals
