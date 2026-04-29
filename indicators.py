# indicators.py — FVG and Order Block detection
"""
FVG (Fair Value Gap)
────────────────────
A 3-candle pattern where price moved so fast it skipped a price range.

  Bullish FVG  →  candle[i-2].high < candle[i].low
                  Gap zone: bottom = candle[i-2].high, top = candle[i].low
                  Acts as DEMAND / support when price returns.
                  → LONG signal

  Bearish FVG  →  candle[i-2].low > candle[i].high
                  Gap zone: bottom = candle[i].high,   top = candle[i-2].low
                  Acts as SUPPLY / resistance when price returns.
                  → SHORT signal

A FVG is *mitigated* (invalid) only when a subsequent candle's BODY closes
fully through the gap. A wick poke is tracked separately as wick_tagged.

Order Block (OB) — ICT definition
─────────────────────────────────
The last opposing candle before a strong DISPLACEMENT move that:
  1. breaks recent market structure (BOS), AND
  2. leaves a Fair Value Gap behind (institutional imbalance)

  Bullish OB  →  last BEARISH candle before an up-displacement that breaks
                 the prior swing high AND prints a bullish FVG within the
                 next few candles. Zone = body (open–close).
                 Acts as DEMAND. → LONG signal

  Bearish OB  →  last BULLISH candle before a down-displacement that breaks
                 the prior swing low AND prints a bearish FVG within the
                 next few candles. Zone = body (open–close).
                 Acts as SUPPLY. → SHORT signal

An OB is *mitigated* only when a subsequent candle's BODY closes fully
through its body. A wick poke is tracked separately as wick_tagged.
"""

from __future__ import annotations
import pandas as pd


# ── ATR (Average True Range, 14-period) ───────────────────────────────────────

def atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    14-period ATR on the most recent candles.
    Returns 0.0 if not enough data — callers should fall back to fixed thresholds.
    """
    if df is None or len(df) < period + 1:
        return 0.0
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    val = tr.rolling(period, min_periods=period).mean().iat[-1]
    return float(val) if pd.notna(val) else 0.0


# ── Premium / Discount (PD array) ─────────────────────────────────────────────

def dealing_range(df: pd.DataFrame, lookback: int = 50) -> tuple[float, float, float] | None:
    """
    The recent dealing range = swing high to swing low over the last `lookback`
    closed candles. Equilibrium is the 50% midpoint.

    Returns (low, high, mid) or None if not enough data.
    """
    if df is None or len(df) < 5:
        return None
    sub = df.iloc[-lookback:]
    lo = float(sub["low"].min())
    hi = float(sub["high"].max())
    if hi <= lo:
        return None
    return lo, hi, (lo + hi) / 2


def pd_zone(price: float, df: pd.DataFrame, lookback: int = 50, eq_band: float = 0.05) -> str:
    """
    Premium / Discount classification — the cornerstone ICT filter.

      discount     → price is in the lower half of the dealing range (good for LONG)
      equilibrium  → price near the 50% line (within ±5% of range height)
      premium      → price is in the upper half of the dealing range (good for SHORT)
      ""            → not enough data

    A LONG taken in premium fights the range; a SHORT taken in discount does too.
    """
    rng = dealing_range(df, lookback)
    if rng is None:
        return ""
    lo, hi, mid = rng
    height = hi - lo
    band = eq_band * height
    if mid - band <= price <= mid + band:
        return "equilibrium"
    return "discount" if price < mid else "premium"


# ── Real liquidity sweeps (equal highs / lows) ────────────────────────────────

def _pivot_highs(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[tuple[int, float]]:
    """Local highs where high[i] > all neighbors within ±left/right bars."""
    n = len(df)
    out: list[tuple[int, float]] = []
    if n < left + right + 1:
        return out
    highs = df["high"].values
    for i in range(left, n - right):
        h = highs[i]
        if all(h >= highs[i - k] for k in range(1, left + 1)) and \
           all(h >  highs[i + k] for k in range(1, right + 1)):
            out.append((i, float(h)))
    return out


def _pivot_lows(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[tuple[int, float]]:
    """Local lows where low[i] < all neighbors within ±left/right bars."""
    n = len(df)
    out: list[tuple[int, float]] = []
    if n < left + right + 1:
        return out
    lows = df["low"].values
    for i in range(left, n - right):
        l = lows[i]
        if all(l <= lows[i - k] for k in range(1, left + 1)) and \
           all(l <  lows[i + k] for k in range(1, right + 1)):
            out.append((i, float(l)))
    return out


def liquidity_pools(df: pd.DataFrame, lookback: int = 60, tol: float = 0.001) -> dict:
    """
    Find clusters of equal pivot highs (BSL — Buy-Side Liquidity above)
    and equal pivot lows (SSL — Sell-Side Liquidity below) in the recent
    `lookback` bars. Two pivots cluster if their prices are within `tol`
    (default 0.1%) of each other.

    Returns {"bsl": [price, ...], "ssl": [price, ...]} — each price is the
    cluster average. Empty lists if no equal H/L found.

    These are where retail stops cluster — exactly what institutions hunt
    before reversing. A wick that pierces a BSL/SSL cluster then closes
    back is the cleanest sweep signal.
    """
    if df is None or len(df) < 10:
        return {"bsl": [], "ssl": []}

    sub = df.iloc[-lookback:]
    pivot_h = _pivot_highs(sub)
    pivot_l = _pivot_lows(sub)

    def cluster(pivots: list[tuple[int, float]]) -> list[float]:
        if len(pivots) < 2:
            return []
        prices = [p for _, p in pivots]
        used = [False] * len(prices)
        clusters: list[float] = []
        for i in range(len(prices)):
            if used[i]:
                continue
            members = [prices[i]]
            used[i] = True
            for j in range(i + 1, len(prices)):
                if used[j]:
                    continue
                ref = sum(members) / len(members)
                if abs(prices[j] - ref) / ref < tol:
                    members.append(prices[j])
                    used[j] = True
            if len(members) >= 2:
                clusters.append(sum(members) / len(members))
        return clusters

    return {"bsl": cluster(pivot_h), "ssl": cluster(pivot_l)}


def detect_liquidity_sweep(df: pd.DataFrame, pools: dict, direction: str, lookback: int = 4) -> bool:
    """
    Did any of the last `lookback` candles wick through a liquidity pool
    of the right kind, then close back inside? This is the real ICT sweep —
    not the zone-edge wick (which is `detect_sweep`).

    For a LONG signal we want a SSL sweep below (stops grabbed below the lows
    before the up-move). For a SHORT we want a BSL sweep above the highs.
    """
    if df is None or len(df) < lookback:
        return False
    sub = df.iloc[-lookback:]
    if direction == "LONG":
        pool_prices = pools.get("ssl", [])
        for _, r in sub.iterrows():
            for pp in pool_prices:
                if float(r["low"]) < pp and float(r["close"]) > pp:
                    return True
    else:  # SHORT
        pool_prices = pools.get("bsl", [])
        for _, r in sub.iterrows():
            for pp in pool_prices:
                if float(r["high"]) > pp and float(r["close"]) < pp:
                    return True
    return False


# ── Volatility regime (chop / normal / expansion) ─────────────────────────────

def regime(df: pd.DataFrame, atr_period: int = 14, lookback: int = 100) -> str:
    """
    Classify current volatility regime by ATR percentile rank within the last
    `lookback` ATR readings.

      chop       → ATR percentile < 25  (consolidation, FVG/OB strategies bleed)
      normal     → 25 ≤ ATR percentile ≤ 75
      expansion  → ATR percentile > 75  (breakout / impulsive moves)
      ""          → insufficient data

    The trader uses this as context: a 7/10 signal in expansion is far more
    reliable than the same 7/10 in chop.
    """
    if df is None or len(df) < atr_period + lookback:
        # Try with a smaller window if we just don't have enough history
        if df is None or len(df) < atr_period + 20:
            return ""
        lookback = max(20, len(df) - atr_period - 1)

    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(atr_period, min_periods=atr_period).mean().dropna()
    if len(atr_series) < 5:
        return ""

    recent = atr_series.iloc[-lookback:] if len(atr_series) >= lookback else atr_series
    cur = float(recent.iloc[-1])
    rank = (recent < cur).sum() / len(recent) * 100

    if rank < 25:  return "chop"
    if rank > 75:  return "expansion"
    return "normal"


# ── HTF trend strength (5-level EMA21+50 stack) ───────────────────────────────

def htf_trend_strength(df) -> str:
    """
    5-level Higher Timeframe trend strength using EMA21 + EMA50 stack.

    Returns one of:
      'strong_bullish'  — price > EMA21 > EMA50  (all aligned up = highest quality LONG)
      'bullish'         — price > EMA21 but EMA50 not confirmed yet
      'neutral'         — price near or between EMAs
      'bearish'         — price < EMA21 but EMA50 not confirmed yet
      'strong_bearish'  — price < EMA21 < EMA50  (all aligned down = highest quality SHORT)

    EMA50 requires at least 52 rows. If data is too short, returns 'neutral'.
    """
    if df is None or len(df) < 52:
        return "neutral"

    close = df["close"]
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    price = float(close.iat[-1])
    e21   = float(ema21.iat[-1])
    e50   = float(ema50.iat[-1])

    if price > e21 and e21 > e50:
        return "strong_bullish"
    elif price > e21:
        return "bullish"
    elif price < e21 and e21 < e50:
        return "strong_bearish"
    elif price < e21:
        return "bearish"
    return "neutral"


# ── Break of Structure (BOS) ──────────────────────────────────────────────────

def detect_bos(df: pd.DataFrame, direction: str) -> bool:
    """
    Break of Structure: confirms the market is trending in the trade direction.

    Compares the last 10 candles against the prior 10 candles:
      Bullish BOS → recent high > previous 10-candle high (upside structure break)
      Bearish BOS → recent low  < previous 10-candle low  (downside structure break)
    """
    if len(df) < 22:
        return False
    recent   = df.iloc[-10:]
    previous = df.iloc[-20:-10]

    if direction == "bullish":
        return float(recent["high"].max()) > float(previous["high"].max())
    else:
        return float(recent["low"].min()) < float(previous["low"].min())


# ── Internal helper: did an impulse window leave a same-direction FVG? ────────

def _has_displacement_fvg(
    df: pd.DataFrame,
    start_idx: int,
    window: int,
    kind: str,
    min_gap_pct: float,
) -> bool:
    """
    Scan candles [start_idx+1 … start_idx+window] for at least one
    same-direction FVG. This is the ICT 'displacement' criterion —
    impulsive moves leave gaps behind. A pause does not.
    """
    n = len(df)
    end = min(start_idx + window + 1, n)
    if end - start_idx < 3:
        return False

    for i in range(start_idx + 2, end):
        high_a = df["high"].iat[i - 2]
        low_a  = df["low"].iat[i - 2]
        mid_b  = df["close"].iat[i - 1]
        high_c = df["high"].iat[i]
        low_c  = df["low"].iat[i]

        # Guard: a corrupt close=0 row would make gap_pct collapse to a raw
        # price difference (always passes any threshold) — skip the row.
        if not mid_b or pd.isna(mid_b):
            continue

        if kind == "bullish" and high_a < low_c:
            if (low_c - high_a) / mid_b >= min_gap_pct:
                return True
        elif kind == "bearish" and low_a > high_c:
            if (low_a - high_c) / mid_b >= min_gap_pct:
                return True
    return False


def _broke_swing(df: pd.DataFrame, start_idx: int, window: int, kind: str, lookback: int = 10) -> bool:
    """
    Did price break the prior swing high (bullish) / low (bearish) within the
    impulse window? Prior swing = the high/low of the `lookback` bars BEFORE
    the OB candle. Falls back to whatever prior bars exist if start_idx
    < lookback (so newly-listed assets don't lose all early OBs).
    """
    n = len(df)
    prior_start = max(0, start_idx - lookback)
    prior       = df.iloc[prior_start : start_idx]
    end         = min(start_idx + window + 1, n)
    impulse     = df.iloc[start_idx + 1 : end]
    if prior.empty or impulse.empty:
        return False

    if kind == "bullish":
        return float(impulse["high"].max()) > float(prior["high"].max())
    else:
        return float(impulse["low"].min())  < float(prior["low"].min())


# ── FVG ───────────────────────────────────────────────────────────────────────

def detect_fvgs(df: pd.DataFrame, min_gap_pct: float = 0.0008) -> list[dict]:
    """
    Scan df for unmitigated Fair Value Gaps. Returns newest first.

    Mitigation rule: a candle's BODY (open–close, not the wick) must close
    fully through the gap. A wick poke is recorded as wick_tagged so callers
    can downgrade freshness.

    Each zone dict includes:
      zone_age    — candles since the gap formed (lower = fresher)
      vol_strong  — middle candle had 1.5× above-average volume
      wick_tagged — at least one later candle's wick entered the gap
    """
    raw: list[dict] = []
    n = len(df)
    has_vol = "volume" in df.columns

    if has_vol:
        vol_avg = df["volume"].rolling(20, min_periods=5).mean()

    for i in range(2, n):
        high_a = df["high"].iat[i - 2]
        low_a  = df["low"].iat[i - 2]
        mid_b  = df["close"].iat[i - 1]
        high_c = df["high"].iat[i]
        low_c  = df["low"].iat[i]

        age = n - 1 - i

        vol_strong = False
        if has_vol:
            try:
                mid_vol = float(df["volume"].iat[i - 1])
                avg_v   = float(vol_avg.iat[i - 1])
                if avg_v > 0 and not pd.isna(avg_v):
                    vol_strong = mid_vol > avg_v * 1.5
            except Exception:
                pass

        if high_a < low_c:
            gap_pct = (low_c - high_a) / mid_b
            if gap_pct >= min_gap_pct:
                raw.append({
                    "kind":       "bullish",
                    "bottom":     high_a,
                    "top":        low_c,
                    "mid":        (high_a + low_c) / 2,
                    "idx":        i,
                    "time":       df.index[i],
                    "gap_pct":    gap_pct,
                    "zone_age":   age,
                    "vol_strong": vol_strong,
                })

        elif low_a > high_c:
            gap_pct = (low_a - high_c) / mid_b
            if gap_pct >= min_gap_pct:
                raw.append({
                    "kind":       "bearish",
                    "bottom":     high_c,
                    "top":        low_a,
                    "mid":        (high_c + low_a) / 2,
                    "idx":        i,
                    "time":       df.index[i],
                    "gap_pct":    gap_pct,
                    "zone_age":   age,
                    "vol_strong": vol_strong,
                })

    # Body-close mitigation. Wick taps tracked separately.
    active: list[dict] = []
    for fvg in raw:
        sub = df.iloc[fvg["idx"] + 1:]
        if sub.empty:
            fvg["wick_tagged"] = False
            active.append(fvg)
            continue

        body_min = sub[["open", "close"]].min(axis=1)
        body_max = sub[["open", "close"]].max(axis=1)

        if fvg["kind"] == "bullish":
            mitigated = (body_max < fvg["bottom"]).any()  # body closed BELOW gap
            wick      = (sub["low"] < fvg["top"]).any()   # any wick inside gap
        else:
            mitigated = (body_min > fvg["top"]).any()     # body closed ABOVE gap
            wick      = (sub["high"] > fvg["bottom"]).any()

        if not mitigated:
            fvg["wick_tagged"] = bool(wick)
            active.append(fvg)

    return list(reversed(active))


# ── OB ────────────────────────────────────────────────────────────────────────

def detect_obs(
    df: pd.DataFrame,
    lookforward: int = 6,
    move_pct: float = 0.003,
    min_gap_pct: float = 0.0008,
    require_displacement: bool = True,
) -> list[dict]:
    """
    Scan df for unmitigated ICT Order Blocks. Returns newest first.

    A candle qualifies as an OB only if the impulse that follows:
      1. moves ≥ move_pct in the OB direction within `lookforward` candles
      2. breaks the prior swing high (bullish) or low (bearish)  ← BOS
      3. leaves at least one same-direction FVG behind            ← displacement

    Body-close mitigation; wick taps tracked as wick_tagged.
    """
    raw: list[dict] = []
    n = len(df)
    has_vol = "volume" in df.columns

    if has_vol:
        vol_avg = df["volume"].rolling(20, min_periods=5).mean()

    for i in range(n - 1):
        o = df["open"].iat[i]
        c = df["close"].iat[i]
        h = df["high"].iat[i]
        l = df["low"].iat[i]

        end = min(i + lookforward + 1, n)
        fut = df.iloc[i + 1: end]
        if fut.empty:
            continue

        body_top    = max(o, c)
        body_bottom = min(o, c)
        age         = n - 1 - i

        vol_strong = False
        if has_vol:
            try:
                v     = float(df["volume"].iat[i])
                avg_v = float(vol_avg.iat[i])
                if avg_v > 0 and not pd.isna(avg_v):
                    vol_strong = v > avg_v * 1.5
            except Exception:
                pass

        # ── Bullish OB: bearish candle before up-impulse ─────────────────────
        if c < o:
            future_high = fut["high"].max()
            if (future_high - h) / c < move_pct:
                continue

            if require_displacement:
                if not _broke_swing(df, i, lookforward, "bullish"):
                    continue
                if not _has_displacement_fvg(df, i, lookforward, "bullish", min_gap_pct):
                    continue

            raw.append({
                "kind":         "bullish",
                "bottom":       body_bottom,
                "top":          body_top,
                "mid":          (body_bottom + body_top) / 2,
                "candle_low":   l,
                "candle_high":  h,
                "idx":          i,
                "time":         df.index[i],
                "zone_age":     age,
                "vol_strong":   vol_strong,
            })

        # ── Bearish OB: bullish candle before down-impulse ───────────────────
        elif c > o:
            future_low = fut["low"].min()
            if (l - future_low) / c < move_pct:
                continue

            if require_displacement:
                if not _broke_swing(df, i, lookforward, "bearish"):
                    continue
                if not _has_displacement_fvg(df, i, lookforward, "bearish", min_gap_pct):
                    continue

            raw.append({
                "kind":         "bearish",
                "bottom":       body_bottom,
                "top":          body_top,
                "mid":          (body_bottom + body_top) / 2,
                "candle_low":   l,
                "candle_high":  h,
                "idx":          i,
                "time":         df.index[i],
                "zone_age":     age,
                "vol_strong":   vol_strong,
            })

    # Body-close mitigation. Wick taps tracked separately.
    active: list[dict] = []
    for ob in raw:
        sub = df.iloc[ob["idx"] + 1:]
        if sub.empty:
            ob["wick_tagged"] = False
            active.append(ob)
            continue

        body_min = sub[["open", "close"]].min(axis=1)
        body_max = sub[["open", "close"]].max(axis=1)

        if ob["kind"] == "bullish":
            mitigated = (body_max < ob["bottom"]).any()
            wick      = (sub["low"] < ob["top"]).any()
        else:
            mitigated = (body_min > ob["top"]).any()
            wick      = (sub["high"] > ob["bottom"]).any()

        if not mitigated:
            ob["wick_tagged"] = bool(wick)
            active.append(ob)

    return list(reversed(active))


# ── Zone touch counter ────────────────────────────────────────────────────────

def zone_touch_count(df: pd.DataFrame, zone: dict) -> int:
    """
    Count how many candles touched the zone after it was formed.
    0 = fresh / first visit.  Higher = zone has been tested before.
    """
    sub = df.iloc[zone["idx"] + 1:]
    if sub.empty:
        return 0
    touched = (sub["low"] <= zone["top"]) & (sub["high"] >= zone["bottom"])
    return int(touched.sum())


# ── Zone proximity checks ──────────────────────────────────────────────────────

def detect_sweep(df: pd.DataFrame, zone: dict) -> bool:
    """
    Liquidity sweep: price briefly wicked THROUGH the zone edge, then
    closed back inside. The #1 institutional entry trigger in ICT.

    Bullish sweep  → wick below zone_bottom, close above zone_bottom
    Bearish sweep  → wick above zone_top,    close below zone_top

    Checks the last 4 candles to catch very recent sweeps.
    """
    sub = df.iloc[-4:]
    bottom = zone["bottom"]
    top    = zone["top"]

    if zone["kind"] == "bullish":
        for _, r in sub.iterrows():
            if float(r["low"]) < bottom and float(r["close"]) >= bottom:
                return True
    else:
        for _, r in sub.iterrows():
            if float(r["high"]) > top and float(r["close"]) <= top:
                return True
    return False


def detect_confirmation(df: pd.DataFrame, zone_kind: str) -> str:
    """
    Detect a reversal confirmation candle pattern at the zone edge.
    Only looks at the last completed candle vs the one before it.

    Returns one of:
      "engulfing"  — candle fully engulfs the previous (strong reversal)
      "pin_bar"    — long wick, small body (rejection at level)
      "doji"       — near-equal open/close (indecision / turning point)
      ""           — no pattern
    """
    if len(df) < 2:
        return ""

    c = df.iloc[-1]
    p = df.iloc[-2]

    co, cc = float(c["open"]), float(c["close"])
    ch, cl = float(c["high"]), float(c["low"])
    po, pc = float(p["open"]), float(p["close"])

    c_range = ch - cl
    if c_range < 1e-10:
        return ""
    c_body  = abs(cc - co)
    body_pct = c_body / c_range

    if zone_kind == "bullish":
        if cc > co and pc < po and cc >= po and co <= pc:
            return "engulfing"
        lower_wick = min(co, cc) - cl
        if body_pct < 0.35 and lower_wick > 0 and lower_wick >= 2.0 * c_body:
            return "pin_bar"
        if body_pct < 0.10:
            return "doji"

    else:
        if cc < co and pc > po and cc <= po and co >= pc:
            return "engulfing"
        upper_wick = ch - max(co, cc)
        if body_pct < 0.35 and upper_wick > 0 and upper_wick >= 2.0 * c_body:
            return "pin_bar"
        if body_pct < 0.10:
            return "doji"

    return ""


def zones_at_price(
    zones: list[dict],
    price: float,
    proximity: float = 0.006,
) -> list[dict]:
    """
    Return zones where the current price is at or near the zone.
    Expands each zone by ±proximity% to catch price touching the edge.
    """
    result = []
    for z in zones:
        lo = z["bottom"] * (1 - proximity)
        hi = z["top"]    * (1 + proximity)
        if lo <= price <= hi:
            result.append(z)
    return result
