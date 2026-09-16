"""Backtesting engine: replay historical/simulated price paths through the
strategy WITHOUT any live execution. Uses an isolated in-memory database.

Modes are clearly separated everywhere:
    BACKTEST | PAPER LIVE | REAL MONEY (permanently DISABLED)
"""
import json
import logging
import os
import tempfile
from typing import Dict, List

from ai.analyzer import build_analyzer
from config import Config
from data.sources import MockMarketSource
from database.db import Database
from execution.engine import PaperTradingEngine
from risk.manager import RiskManager
from strategy.edge import evaluate_signal
from strategy.filter import extract_features, filter_markets

logger = logging.getLogger("backtest")


class Backtester:
    def __init__(self, cfg: Config):
        # isolated state - backtest never touches live paper-trading state
        self.cfg = cfg
        self.tmp = tempfile.mkdtemp(prefix="backtest_")
        self.db = Database(os.path.join(self.tmp, "backtest.db"))
        self.wallet = self.db.init_wallet(cfg.STARTING_BALANCE)
        self.analyzer = build_analyzer(cfg)
        self.risk = RiskManager(cfg, self.db)
        self.engine = PaperTradingEngine(cfg, self.db, self.wallet)

    def run(self, source: MockMarketSource, steps: int = 60,
            verbose: bool = True) -> dict:
        cfg = self.cfg
        # step 0: open markets
        markets = source.fetch_markets(limit=cfg.MAX_MARKETS_PER_SCAN)
        for step in range(steps):
            mkt_map = {m.market_id: m for m in markets}
            candidates = filter_markets(markets, cfg)
            trades_this_step = 0
            for m in candidates:
                feats = extract_features(m)
                sig = self.analyzer.analyze(m, feats)
                if sig is None:
                    continue
                self.db.save_ai_decision(sig, self.analyzer.name())
                ok, _ = evaluate_signal(sig, m, cfg)
                if not ok:
                    continue
                price = m.mid_price if sig.decision == "BUY_YES" \
                    else 1 - (m.mid_price or 0.5)
                decision = self.risk.check(self.wallet, sig, m.market_id, price)
                if decision.approved:
                    if self.engine.open_position(m, sig, decision.size_dollars):
                        trades_this_step += 1

            self.engine.update_positions(mkt_map)

            # resolve markets whose time has passed
            import time
            now = time.time()
            for m in markets:
                if m.status == "open" and m.end_date and m.end_date <= now:
                    import random
                    p = m.price_history[-1] if m.price_history else 0.5
                    m.status = "resolved"
                    m.resolution_outcome = m.outcomes[0] if random.random() < p \
                        else m.outcomes[1]
            for row in self.db.open_positions():
                m = mkt_map.get(row["market_id"])
                if m and m.status == "resolved" and m.resolution_outcome:
                    winning = "YES" if m.resolution_outcome == m.outcomes[0] else "NO"
                    self.engine.settle_position(row["id"], row, winning)

            source.advance_prices()
            unrealized = sum((r["mark_price"] - r["avg_entry_price"]) * r["quantity"]
                            for r in self.db.open_positions())
            self.db.save_equity(self.wallet.balance,
                                self.wallet.balance + unrealized)
            if verbose:
                logger.info("backtest step %s/%s balance=%.2f trades=%s open=%s",
                            step + 1, steps, self.wallet.balance,
                            trades_this_step, len(self.db.open_positions()))

        # final: force-resolve everything to value the book
        results = source.resolve_all()
        for row in self.db.open_positions():
            outcome = results.get(row["market_id"])
            m = mkt_map.get(row["market_id"])
            if outcome and m:
                winning = "YES" if outcome == m.outcomes[0] else "NO"
                self.engine.settle_position(row["id"], row, winning)

        return self.report()

    def report(self) -> dict:
        trades = [dict(r) for r in
                  self.db.query("SELECT * FROM trades WHERE pnl IS NOT NULL")]
        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        eq = [r["equity"] for r in
              self.db.query("SELECT equity FROM equity_curve ORDER BY id")]
        max_dd, peak = 0.0, 0.0
        for e in eq:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak)
        start = self.wallet.starting_balance
        end = self.wallet.balance
        return {
            "mode": "BACKTEST",
            "starting_balance": start,
            "final_balance": round(end, 2),
            "total_return_pct": round((end - start) / start * 100, 2)
            if start else 0.0,
            "n_trades": len(pnls),
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
            "avg_return_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
            if losses and sum(losses) != 0 else None,
        }

    def close(self):
        self.db.close()


def run_backtest(cfg: Config, steps: int = 60, seed: int = 7) -> dict:
    cfg.assert_safe()
    source = MockMarketSource(n_markets=60, seed=seed)
    bt = Backtester(cfg)
    try:
        report = bt.run(source, steps=steps)
        print(json.dumps(report, indent=2))
        return report
    finally:
        bt.close()
