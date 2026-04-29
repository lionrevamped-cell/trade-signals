#!/usr/bin/env python3
"""
Trade Signal Scanner — FVG & Order Block
─────────────────────────────────────────
Usage:
  python main.py              # scan all assets
  python main.py crypto       # scan one category
  python main.py india        # India stocks only
  python main.py usa          # USA stocks only
  python main.py commodities  # Gold / Silver / PAX Gold
  python main.py --explain    # explain FVG and OB

Signals:
  LONG  → price is at a bullish FVG / OB (demand zone) → buy / go long
  SHORT → price is at a bearish FVG / OB (supply zone)  → sell / go short

Strength:
  FVG+OB  ★★  Both a FVG and an OB overlap at the same zone — strongest signal
  FVG     ★   Fair Value Gap only
  OB      ★   Order Block only
"""

from __future__ import annotations
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

from config import ASSETS
from signals import scan_all, Signal

console = Console()

EXPLAIN = """
[bold yellow]FVG — Fair Value Gap[/]
A FVG forms when price moves so fast that it skips a price range entirely,
leaving a 3-candle pattern where candle 1 and candle 3 don't overlap.

  [green]Bullish FVG[/]  →  candle[i-2].high < candle[i].low
                  The gap is a DEMAND zone. Price tends to return and bounce UP.
                  → [green]LONG[/] signal when price retraces into this gap.

  [red]Bearish FVG[/]  →  candle[i-2].low > candle[i].high
                  The gap is a SUPPLY zone. Price tends to return and reject DOWN.
                  → [red]SHORT[/] signal when price retraces into this gap.

[bold yellow]OB — Order Block[/]
An Order Block is the last opposing candle before a strong impulse move.
Large players placed orders there, so price often returns to that zone.

  [green]Bullish OB[/]  →  last BEARISH (red) candle before a strong UP-move
                  That candle's body is a DEMAND zone. → [green]LONG[/] signal.

  [red]Bearish OB[/]  →  last BULLISH (green) candle before a strong DOWN-move
                  That candle's body is a SUPPLY zone. → [red]SHORT[/] signal.

[bold yellow]FVG + OB Confluence[/]
When both a FVG and an OB overlap at the same price zone, the signal is
[bold]STRONGER[/] because two different reasons point to the same demand/supply.
"""


def fmt_price(p: float) -> str:
    if p >= 10_000:
        return f"{p:,.2f}"
    elif p >= 100:
        return f"{p:,.3f}"
    elif p >= 1:
        return f"{p:.4f}"
    else:
        return f"{p:.6f}"


def fmt_zone(bottom: float, top: float) -> str:
    return f"{fmt_price(bottom)} – {fmt_price(top)}"


def build_table(signals: list[Signal], title: str, border: str) -> Table:
    t = Table(
        title=title,
        box=box.ROUNDED,
        header_style=f"bold {border}",
        border_style=border,
        show_lines=True,
        expand=False,
    )
    t.add_column("Asset",     min_width=15, no_wrap=True)
    t.add_column("Category",  min_width=12, style="dim")
    t.add_column("TF",        min_width=4,  justify="center")
    t.add_column("Signal",    min_width=6,  justify="center")
    t.add_column("Zone (bottom – top)",  min_width=24, justify="right")
    t.add_column("Price Now",            min_width=12, justify="right")
    t.add_column("Dist%",                min_width=7,  justify="right")
    t.add_column("Reason",               min_width=8,  justify="center")

    for s in signals:
        sig_color = "green" if s.direction == "LONG" else "red"
        sig_text  = Text(f"{'▲' if s.direction == 'LONG' else '▼'} {s.direction}",
                         style=f"bold {sig_color}")

        reason_color = "yellow" if "+" in s.reason else "cyan"
        reason_text  = Text(s.reason, style=f"bold {reason_color}")

        dist_color = "green" if s.dist_pct < 0.3 else ("yellow" if s.dist_pct < 1.0 else "dim")

        t.add_row(
            f"[bold]{s.name}[/]\n[dim]{s.ticker}[/dim]",
            s.category,
            s.timeframe,
            sig_text,
            fmt_zone(s.zone_bottom, s.zone_top),
            fmt_price(s.current_price),
            Text(f"{s.dist_pct:.2f}%", style=dist_color),
            reason_text,
        )
    return t


def main() -> None:
    args = sys.argv[1:]

    # ── --explain flag ────────────────────────────────────────────────────────
    if "--explain" in args:
        console.print(Panel(EXPLAIN, title="[bold]What are FVG and OB?[/]",
                            border_style="cyan", expand=False))
        return

    # ── Category filter ───────────────────────────────────────────────────────
    category_filter = args[0].lower() if args else None

    if category_filter:
        assets = {k: v for k, v in ASSETS.items()
                  if category_filter in k.lower()}
        if not assets:
            valid = ", ".join(k.lower() for k in ASSETS)
            console.print(f"[red]No category matching '{category_filter}'. "
                          f"Available: {valid}[/]")
            return
    else:
        assets = ASSETS

    total = sum(len(v) for v in assets.values())
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    console.print(Panel(
        f"[bold cyan]FVG & Order Block Signal Scanner[/]\n"
        f"[dim]{now}  ·  {total} assets  ·  3 timeframes (1H / 4H / 1D)[/]",
        border_style="cyan",
    ))

    # ── Scan ──────────────────────────────────────────────────────────────────
    all_signals: list[Signal] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(
            f"Scanning {total} assets…", total=total
        )

        from signals import scan_asset
        from concurrent.futures import ThreadPoolExecutor, as_completed

        flat = [(t, n, c) for c, items in assets.items() for t, n in items]

        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(scan_asset, t, n, c): t for t, n, c in flat}
            for fut in as_completed(futures):
                try:
                    all_signals.extend(fut.result())
                except Exception:
                    pass
                progress.advance(task)

    all_signals.sort(key=lambda s: (-s.strength, s.dist_pct))

    # ── Display ───────────────────────────────────────────────────────────────
    if not all_signals:
        console.print(
            "\n[yellow]No signals right now.[/] "
            "Price is not near any active FVG or OB zone.\n"
            "[dim]Try again when markets are trending or after a pullback.[/]"
        )
        return

    strong = [s for s in all_signals if s.strength == 2]
    medium = [s for s in all_signals if s.strength == 1]

    if strong:
        console.print()
        console.print(build_table(
            strong,
            f"[bold yellow]★★  STRONG — FVG + OB Confluence  ({len(strong)} signals)[/]",
            "yellow",
        ))

    if medium:
        console.print()
        console.print(build_table(
            medium,
            f"[bold cyan]★   MEDIUM — FVG or OB  ({len(medium)} signals)[/]",
            "cyan",
        ))

    # ── Summary line ──────────────────────────────────────────────────────────
    longs  = sum(1 for s in all_signals if s.direction == "LONG")
    shorts = sum(1 for s in all_signals if s.direction == "SHORT")

    console.print()
    console.print(Rule(style="dim"))
    console.print(
        f"  [bold]Total signals:[/] {len(all_signals)}   "
        f"[green]▲ LONG {longs}[/]   [red]▼ SHORT {shorts}[/]   "
        f"[yellow]★★ Strong {len(strong)}[/]   [cyan]★ Medium {len(medium)}[/]\n"
        f"  [dim]Dist% = how far price is from zone midpoint. "
        f"Lower = price is deeper inside the zone.[/]"
    )
    console.print()


if __name__ == "__main__":
    main()
