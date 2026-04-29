# Trade Signals Scanner — Windows Quick Start

## One-time install

1. **Download** the project ZIP and extract it somewhere (e.g. `C:\trade-signals\`).
2. **Double-click `install.bat`**. It will:
   - Check that Python 3.10+ is installed (and install it via `winget` if missing)
   - Create a virtual environment in `.venv\`
   - Install all dependencies from `requirements.txt`

The first run takes **3–5 minutes** depending on your connection. Subsequent runs are instant.

## Daily use

**Double-click `run.bat`.**

That's it. The scanner starts on `http://localhost:8000` and your default browser opens automatically.

To stop the scanner, **close the black command-prompt window**.

## What runs automatically while it's open

| Loop | Frequency | What it does |
|---|---|---|
| Market scan | every 5 min | Re-detects FVG/OB zones, scores signals |
| Live price ticker | every 10 sec | Updates current prices |
| Outcome tracker | every 10 sec | Marks TP1/TP2/TP3/SL on each tracked signal |
| Auto health agent | every 24h | Runs `auto_agent.py` — checks data pipeline, scoring drift, factor edges |
| Auto-learn weights | every 24h | Runs `auto_learn.py` — nudges scoring weights based on live outcomes |

**No external schedulers, no cron, no cloud, no LLM.** Everything runs in-process on your local machine.

## Pages

| URL | What |
|---|---|
| `http://localhost:8000`            | **Top Picks** — best signals right now, plain English |
| `http://localhost:8000/calculator` | BitMart trade calculator (entry/SL/TP/funding) |
| `http://localhost:8000/chart`      | Live FVG/OB chart |
| `http://localhost:8000/journal`    | Trade journal |
| `http://localhost:8000/performance`| Live signal outcomes vs backtest |
| `http://localhost:8000/agent`      | Auto-agent health + self-learning status |

## Files the system writes (everything stays local)

| File | What |
|---|---|
| `outcomes.json`           | Every signal tracked + its TP/SL outcome |
| `learned_weights.json`    | Current auto-learned scoring weights |
| `learn_history.json`      | Last 60 weight-update events |
| `agent_reports/`          | Daily health reports (JSON + Markdown) |
| `backtest_history/`       | Archived backtest runs |
| `.learn_paused`           | Touch this file to pause auto-learn |

## Running a fresh backtest

To validate or recalibrate the scoring weights:

```
.venv\Scripts\python.exe run_backtest.py --archive
```

This walks ~1500 historical bars across BTC/ETH/SOL/AAPL/NVDA at 1H+4H, and writes the result to `/tmp/backtest_results.json` plus `backtest_history/`.

## Pausing self-learning

If you don't want the system tuning weights:

- Click "Pause" on the `/agent` page, OR
- Create an empty file named `.learn_paused` in the project folder

To resume: click Resume on `/agent`, or delete `.learn_paused`.

## Troubleshooting

**"Python is not recognized"** — run `install.bat` again, it will install Python via winget.

**Browser doesn't open** — manually open `http://localhost:8000`.

**Port 8000 in use** — close other apps using that port, or edit `app.py` line 359 to use a different port.

**Scanner says "Loading…" forever** — check the terminal for errors. yfinance / Binance can be temporarily flaky; usually resolves on the next 5-min scan.
