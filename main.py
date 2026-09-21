#!/usr/bin/env python3
"""AI-Powered Paper Trading Agent - entry point.

USAGE
  python main.py --init            create the database + wallet
  python main.py --once            run one full scan cycle, save state, dashboard
  python main.py --loop            run continuously every SCAN_INTERVAL_MINUTES
  python main.py --dashboard       regenerate dashboard.html only
  python main.py --backtest [N]    run N-step backtest on simulated data
  python main.py --status          print wallet + position summary
  python main.py --canary          run the isolated paper-engine canary test
  python main.py --lab             run the strategy lab threshold sweep
  python main.py --recover --ack R  explicitly recover a DEAD agent

PAPER TRADING ONLY. Real-money execution is disabled at the code level:
there is no wallet connection and no order-routing code path anywhere.
"""
import argparse
import json
import logging
import os
import sys

from config import Config, SafetyError
from utils.logger import setup_logging, log_event

logger = logging.getLogger("main")


def build_config() -> Config:
    try:
        return Config()
    except SafetyError as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description="AI Paper Trading Agent")
    parser.add_argument("--init", action="store_true",
                        help="initialize database and wallet")
    parser.add_argument("--once", action="store_true",
                        help="run one scan cycle")
    parser.add_argument("--loop", action="store_true",
                        help="run continuously")
    parser.add_argument("--cycles", type=int, default=0,
                        help="max cycles in --loop mode")
    parser.add_argument("--dashboard", action="store_true",
                        help="regenerate dashboard only")
    parser.add_argument("--backtest", type=int, nargs="?", const=60,
                        help="run backtest for N steps")
    parser.add_argument("--status", action="store_true",
                        help="print current status")
    parser.add_argument("--canary", action="store_true",
                        help="run the isolated paper-engine canary test")
    parser.add_argument("--lab", action="store_true",
                        help="run the strategy lab threshold sweep")
    parser.add_argument("--recover", action="store_true",
                        help="explicitly acknowledge and recover a DEAD agent")
    parser.add_argument("--ack", metavar="REASON",
                        help="death reason acknowledgement for --recover")
    parser.add_argument("--source", choices=["polymarket", "mock"],
                        help="override DATA_SOURCE")
    args = parser.parse_args()

    cfg = build_config()
    if args.source:
        os.environ["DATA_SOURCE"] = args.source
        cfg.DATA_SOURCE = args.source
    setup_logging(cfg.LOG_DIR)
    log_event(logger, "CONFIG", **cfg.summary())

    if args.canary:
        from canary.canary import run_canary
        result = run_canary(live_db_path=cfg.DATABASE_PATH)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1

    if args.lab:
        from database.db import Database
        from lab.sweep import run_lab
        db = Database(cfg.DATABASE_PATH)
        print(json.dumps(run_lab(db), indent=2))
        db.close()
        return 0

    if args.recover:
        from agent.lifecycle import Lifecycle, load_release
        from database.db import Database
        db = Database(cfg.DATABASE_PATH)
        lc = Lifecycle(cfg, db)
        release = load_release()
        if not args.ack:
            cur = lc.get()
            print(f"DEAD recovery requires an explicit acknowledgement.")
            print(f"Death reason: {cur.get('death_reason') or 'n/a'}")
            print(f"Usage: python main.py --recover --ack <REASON>")
            db.close()
            return 2
        if lc.recover(args.ack, release):
            print(f"Recovered: state={lc.state} reason={lc.reason}. "
                  f"All history preserved.")
            db.close()
            return 0
        print(f"Recovery refused: state={lc.state} "
              f"(needs RECOVERY_REQUIRED, a new strategy release, and "
              f"the correct death reason acknowledgement).")
        db.close()
        return 2

    if args.init:
        from database.db import Database
        db = Database(cfg.DATABASE_PATH)
        w = db.init_wallet(cfg.STARTING_BALANCE)
        print(f"Initialized wallet: ${w.starting_balance:.2f} simulated "
              f"(database: {cfg.DATABASE_PATH})")
        db.close()
        return

    if args.status:
        from database.db import Database
        from dashboard.generate import compute_stats
        db = Database(cfg.DATABASE_PATH)
        w = db.init_wallet(cfg.STARTING_BALANCE)
        stats = compute_stats(db, w.balance, w.starting_balance)
        stats.pop("open_positions", None)
        stats.pop("closed_positions", None)
        stats.pop("recent_trades", None)
        stats.pop("recent_signals", None)
        stats.pop("recent_scans", None)
        print(json.dumps(stats, indent=2, default=str))
        db.close()
        return

    if args.backtest is not None:
        from backtest.engine import run_backtest
        run_backtest(cfg, steps=args.backtest)
        return

    if args.dashboard:
        from database.db import Database
        from dashboard.generate import generate_dashboard
        db = Database(cfg.DATABASE_PATH)
        path = generate_dashboard(cfg, db)
        print(f"Dashboard written: {path}")
        db.close()
        return

    if args.once or args.loop:
        from agent.runner import Agent
        agent = Agent(cfg)
        if args.once:
            stats = agent.run_cycle()
            from database.db import Database  # noqa: F401
            from dashboard.generate import generate_dashboard
            generate_dashboard(cfg, agent.db)
            print(json.dumps(stats, indent=2, default=str))
        else:
            agent.run_forever(max_cycles=args.cycles)
        agent.db.close()
        return

    parser.print_help()


if __name__ == "__main__":
    sys.exit(main() or 0)
