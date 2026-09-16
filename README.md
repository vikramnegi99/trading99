# AI-Powered Paper Trading Agent

An autonomous agent that scans prediction markets, estimates fair
probabilities, detects mispricing, and trades — **with simulated money
only**. Virtual bankroll: **$100**. Designed to run on GitHub Actions and
be managed entirely from the GitHub mobile app.

> **PAPER TRADING ONLY.** Real-money execution is disabled at the code
> level. There is no wallet connection, no private keys, and no
> order-routing code path anywhere in this project. Setting
> `REAL_TRADING=true` makes the program refuse to start.

## How it works

```
Market Data (Polymarket public API)
  → Market Filtering        (cheap, programmatic)
  → Feature Extraction      (momentum, volatility, liquidity, ...)
  → AI Probability Estimate (heuristic or LLM)
  → Fair Value + Edge       (edge = est_prob − market_prob)
  → Risk Management         (fractional Kelly, hard caps, kill switch)
  → Paper Trade Execution   (simulated fills, slippage, fees)
  → Position Tracking
  → Resolution Detection    → P&L
  → Performance Analytics   → Dashboard + Logs
```

Funnel by design: ~1000 markets → 100–200 candidates → AI analysis →
5–20 potential opportunities. Thresholds are all configurable.

## Quick start (local)

```bash
pip install -r requirements.txt
cp .env.example .env          # optional: tweak settings

python main.py --init         # create wallet ($100 simulated)
python main.py --once         # one scan cycle + dashboard
python main.py --status        # performance summary
python main.py --loop          # run continuously every SCAN_INTERVAL_MINUTES
python main.py --backtest 60   # 60-step backtest on simulated markets
```

Output: `dashboard.html` (open in any browser) and state in
`data/trading.db` (SQLite).

## Run it from your phone (GitHub Actions)

1. Push this repo to GitHub.
2. The workflow `.github/workflows/paper-trading.yml` runs every ~10
   minutes on a schedule, runs one scan cycle, and commits the updated
   state + `dashboard.html` back to the repo.
3. On mobile: open the repo in the GitHub app → **Actions** → tap a run
   for logs; open `dashboard.html` for stats; use **Run workflow** for a
   manual run.
4. To pause the agent: disable the workflow (or set repo variable /
   secret `KILL_SWITCH`). To reset: delete `data/` and re-run `--init`.

No API keys are required for the default setup — the built-in
**heuristic analyzer** is free and offline. To plug in an LLM (any
OpenAI-compatible endpoint), add a repository secret `OPENAI_API_KEY`
and set `AI_PROVIDER=openai` in the workflow env. Invalid LLM output is
rejected and falls back safely; the agent never trusts unvalidated JSON.

## Configuration

All settings live in `config.py` with env-var overrides (see
`.env.example`). Highlights:

| Setting | Default | Meaning |
|---|---|---|
| STARTING_BALANCE | 100 | simulated bankroll ($) |
| SCAN_INTERVAL_MINUTES | 10 | loop interval |
| MIN_EDGE | 0.08 | min est_prob − mkt_prob to trade |
| MIN_CONFIDENCE | 0.60 | min AI confidence |
| MAX_POSITION_PERCENT | 0.06 | max 6% of bankroll per trade |
| MAX_OPEN_EXPOSURE_PERCENT | 0.25 | max total open exposure |
| MAX_DAILY_LOSS_PERCENT | 0.10 | daily stop |
| MAX_DRAWDOWN_PERCENT | 0.30 | drawdown stop |
| SLIPPAGE_PERCENT | 0.01 | simulated slippage |
| SIMULATED_FEES | true | simulated taker fees |
| KILL_SWITCH | false | stops all new trades |

## Safety

- `REAL_TRADING` is a hard-coded `False`; env attempts to enable it exit
  with a `SafetyError`.
- No wallet connection, no private keys, no seed phrases — providing them
  in env is rejected.
- All keys come from environment variables / repo secrets, never code.
- `KILL_SWITCH=true` halts trading; daily-loss and drawdown stops are
  enforced even without it.
- Fail-safe: an API outage skips the cycle and keeps prior state intact.

## Project structure

```
agent/       scan cycle + continuous loop
ai/          heuristic + optional LLM analyzers, signal validation
backtest/    replay engine (clearly labelled BACKTEST)
data/        market sources (Polymarket public API, mock)
database/    SQLite persistence (wallet, positions, orders, trades,
             snapshots, AI decisions, equity curve)
dashboard/   mobile-friendly HTML dashboard generator
execution/   paper trading engine (100% simulated)
models/      dataclasses: MarketSnapshot, Signal, PaperOrder, ...
risk/        Kelly sizing + hard risk limits
strategy/   filtering, features, edge evaluation
tests/       pytest suite (48 tests)
.github/     Actions: scheduled runs + CI
```

## Modes

| Mode | Command | Notes |
|---|---|---|
| BACKTEST | `python main.py --backtest` | simulated replay, isolated DB |
| PAPER LIVE | `python main.py --once/--loop` | simulated fills |
| REAL MONEY | — | **permanently disabled** |

## Honest disclaimer

This project measures whether a strategy *might* have a repeatable
statistical edge. It does not claim profitability without substantial
paper/backtest evidence, it is not investment advice, and the default
heuristic analyzer is a transparent statistical baseline — not a magic
money machine. Expect slippage, fees, losing streaks, and calibration
error. Track drawdown, exposure, and confidence calibration before
believing any result.
