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
python main.py --status       # performance summary
python main.py --loop          # run continuously every SCAN_INTERVAL_MINUTES
python main.py --backtest 60   # 60-step backtest on simulated markets
python main.py --canary        # isolated full-pipeline proof test
python main.py --lab           # strategy threshold replay sweep
python main.py --recover --ack REASON   # recover a DEAD agent (human)
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
4. To pause the agent: set repository variable `KILL_SWITCH` to `true`
   (Settings → Secrets and variables → Actions → **Variables** tab) -
   no workflow edit needed. To fully stop, disable the workflow. To
   reset: delete `data/trading.db` and re-run `--init`.

### How state survives between runs

- `data/trading.db` is **committed on purpose** - it is the wallet/
  positions/trades history. Each Actions run checks it out, runs one
  cycle, then commits it back.
- Every cycle ends with a SQLite `wal_checkpoint(TRUNCATE)` so the `.db`
  file alone carries the full state (the `-wal`/`-shm` sidecars are
  gitignored and never needed).
- High-churn tables (`snapshots`, `scans`, `equity_curve`,
  `ai_decisions`) are pruned every cycle so the committed file stays
  small; trades/orders/positions are never pruned.
- If GitHub delays a scheduled run (happens under load), nothing is lost -
  the next run simply continues from the committed state.
- Note: on public repositories GitHub auto-disables schedules after 60
  days with no repo activity. The bot's own commits normally prevent
  this; if the schedule ever stops, push any commit or re-enable via the
  Actions tab.

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
| MIN_EDGE | 0.05 | min est_prob − mkt_prob to trade (repo variable `MIN_EDGE` overrides, mobile-configurable) |
| STRATEGY | conservative | active strategy profile: conservative / balanced / experimental (repo variable) |
| SHADOW_STRATEGY | (unset) | optional shadow strategy, logged but never executed (repo variable) |
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
- `KILL_SWITCH=true` (repo variable) halts new trades while still
  allowing held positions to settle; daily-loss and drawdown stops are
  enforced even without it.
- Fail-safe: an API outage skips the cycle and keeps prior state intact.

## Project structure

```
agent/       scan cycle + loop + lifecycle (DEAD-HAND) + 24h windows
ai/          heuristic + optional LLM analyzers, signal validation
backtest/    replay engine (clearly labelled BACKTEST)
canary/      isolated full-pipeline proof test (never touches the live wallet)
data/        market sources (Polymarket public API, mock) + health wrapper
database/    SQLite persistence (wallet, positions, orders, trades,
             snapshots, AI decisions, equity curve, episodes, rejections,
             lifecycle, provider health, scores, canary, system health)
dashboard/   professional terminal dashboard generator (mobile-first)
execution/   paper trading engine (100% simulated, impossible-value guards)
lab/         strategy lab: threshold replay sweep + promotion bookkeeping
models/      dataclasses: MarketSnapshot, Signal, PaperOrder, ...
risk/        Kelly sizing + hard risk limits
strategy/   filtering, features, edge evaluation, profiles, RELEASE.json,
             promotion.json (human approval gate)
tests/       pytest suite (119 tests: lifecycle, survival, canary, probability matrix)
.github/     Actions: scheduled runs + CI
```

Market resolution is detected by polling each held market by id
(resolved markets disappear from the active feed; the Gamma API reports
them with `umaResolutionStatus: "resolved"` and prices pinned to 1/0).

## 24h Challenge Episodes (V2: evaluation windows)

On top of the CONTINUOUS paper wallet (one-time $100, never reset), the
agent rolls **24-hour evaluation windows**: at the end of each 24 hours
the window is closed out with a full report (the bankroll and open
positions carry over untouched), stored in the `episodes` table and
shown on the dashboard.

The episode layer is strictly **observational** - it never loosens the
trading thresholds and never pressures the agent to trade. An episode
with zero trades is a valid, unpenalized outcome.

**Performance score (0-100, from a neutral 50):**

| Component | Effect |
|---|---|
| Return | +2 pts per +1% episode return (cap +30 up / -20 down) |
| Drawdown | -1 pt per 1% max drawdown (cap -15) |
| Trade quality | +/- per $0.50 avg realized P&L per trade (cap +/-10; exactly 0 with no trades) |
| Probability calibration | Brier score of entry estimates vs outcomes (cap +/-10; neutral under 3 resolved samples) |
| Risk discipline | -5 pts per risk-limit violation (cap -15) |
| Churn penalty | -1 pt per trade above 24 in an episode (cap -10) - punishes overtrading, never NOT trading |

The end-of-episode report contains: starting/ending equity, return,
realized/unrealized P&L, scans, markets, candidates, AI analyzed, valid
and rejected signals, trades, wins/losses, win rate, max drawdown,
average edge, average confidence, total fees, slippage, risk
violations, data failures, strategy-inactivity cycles, final score
(with full breakdown) and the **top 20 rejected opportunities with
their exact rejection reason** (e.g. `edge_too_small:0.041`), logged
at both the signal stage and the risk stage.

## V2: Survival System (one-time $100, DEAD-HAND, canary)

**Bankroll is CONTINUOUS.** The account starts at $100 exactly once and is
never reset. 24h "episodes" are evaluation windows only - they record a
report and a 0-100 score, but the wallet and open positions carry over:
`$100 -> $103 -> $98 -> $110 -> ...`.

**Lifecycle states** (persistent in SQLite, survives Actions restarts):
`RUNNING -> DEGRADED` (data issues) `-> LOCKED` (12 consecutive failed
cycles; auto-unlocks after 3 healthy cycles) - no trading while locked.
`REVIEW_REQUIRED` (prolonged strategy inactivity, or maintenance lease
overdue) - trading continues but review is due.
`DEAD` (terminal: BANKRUPT if equity <= 0, SAFETY_FAILURE on unsafe
paths/impossible values, MAINTENANCE_EXPIRED after 48h overdue review).

**DEAD cannot auto-resurrect.** Recovery requires: a new human strategy
release (`strategy/RELEASE.json` with a new version) + explicit
acknowledgement: `python main.py --recover --ack <REASON>`. The database
and all trading history are always preserved.

**Maintenance lease.** Bot state commits never renew it. 7 days without a
human commit touching strategy/AI/risk code -> REVIEW_REQUIRED; +48h
without a release -> DEAD (MAINTENANCE_EXPIRED).

**Paper engine canary.** Every workflow run executes an ISOLATED full
pipeline proof (market -> features -> signal -> edge -> risk ->
execution -> position -> resolution -> P&L) on a throwaway database -
`python main.py --canary`. The dashboard shows `CANARY: PASS / FAIL`, so
a broken code path can never silently masquerade as "no opportunities".

**Strategy profiles** (`STRATEGY` repo variable): `conservative`,
`balanced`, `experimental`. Profiles adjust decision thresholds only -
hard risk caps always come from config defaults and can never be relaxed
by a profile. Optional `SHADOW_STRATEGY` repo variable runs a second
strategy in shadow mode (signals logged, never executed). Promotion to
ACTIVE requires human approval in `strategy/promotion.json` - the agent
never deploys a strategy by itself.

**Strategy Lab:** `python main.py --lab` runs a threshold replay sweep
over the agent's own logged decisions (no claimed counterfactual P&L).

**Provider health, rejection analytics, risk panels, system health and
a professional mobile-first terminal dashboard** are regenerated from
the committed SQLite state every cycle.

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
