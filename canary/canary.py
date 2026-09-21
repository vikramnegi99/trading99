"""PAPER ENGINE CANARY.

A deterministic, ISOLATED proof that the full trade pipeline still
works: market -> feature extraction -> AI signal -> edge test -> risk
test -> simulated execution -> position -> marking -> resolution ->
P&L. It runs against a throwaway database in a temp directory, so it
can NEVER affect the live paper bankroll.

Result is stored in the LIVE database (canary_results table) as one row,
and shown on the dashboard as PASS / FAIL. This prevents the system
from silently looking "dead" for days because some code path broke
while the agent keeps reporting zero trades as "no opportunities".
"""
import json
import tempfile
import time
from typing import Optional

from config import Config
from database.db import Database
from execution.engine import PaperTradingEngine
from models.entities import MarketSnapshot
from risk.manager import RiskManager
from strategy.edge import evaluate_signal
from strategy.filter import extract_features
from ai.analyzer import HeuristicAnalyzer


def _canary_market() -> MarketSnapshot:
    """A deterministic market with a KNOWN valid opportunity.

    Price is 0.30 while the recent history sits around 0.46 (mean_dev
    -0.16, low volatility, deep history, high liquidity) so a correct
    heuristic must estimate ~0.376 -> edge ~+0.076 > MIN_EDGE, and the
    confidence components are all strong.
    """
    hist = [0.46] * 29 + [0.30]   # mean far above the current price
    return MarketSnapshot(
        market_id="canary-1",
        question="CANARY: will the paper engine prove itself?",
        outcomes=["Yes", "No"],
        outcome_prices=[0.30, 0.70],
        volume=500_000, liquidity=150_000,
        spread=0.01,
        best_bid=0.295, best_ask=0.305,
        price_history=hist,
    )


def run_canary(live_db_path: Optional[str] = None) -> dict:
    """Execute the canary. Returns {'passed': bool, 'steps': [...]}.

    When live_db_path is given, the RESULT ROW ONLY is appended to the
    live database (canary_results) - the canary's own trading happens in
    a temp DB that is deleted afterwards.
    """
    steps = []
    details = {"steps": steps, "pnl": None, "error": None}
    try:
        with tempfile.TemporaryDirectory(prefix="canary-") as tmp:
            env = {"DATABASE_PATH": f"{tmp}/canary.db",
                   "REALISTIC_EXECUTION": "true",
                   "SIMULATED_FEES": "true"}
            cfg = Config(env=env)
            cfg.assert_safe()

            db = Database(cfg.DATABASE_PATH)
            wallet = db.init_wallet(100.0)
            engine = PaperTradingEngine(cfg, db, wallet)
            risk = RiskManager(cfg, db)
            analyzer = HeuristicAnalyzer(cfg)

            market = _canary_market()
            steps.append("market: OK")

            feats = extract_features(market)
            assert feats["history_len"] >= 10, "insufficient features"
            steps.append("features: OK")

            sig = analyzer.analyze(market, feats)
            assert sig is not None, "no signal produced"
            assert sig.decision == "BUY_YES", \
                f"analyzer said {sig.decision} (edge={sig.edge})"
            assert sig.edge >= cfg.MIN_EDGE, \
                f"edge {sig.edge} below MIN_EDGE {cfg.MIN_EDGE}"
            steps.append(f"ai_signal: OK (edge={sig.edge}, "
                         f"conf={sig.confidence})")

            ok, reasons = evaluate_signal(sig, market, cfg)
            assert ok, f"edge test rejected: {reasons}"
            steps.append("edge_test: OK")

            size = risk.position_size(wallet, sig, market.mid_price or 0.5)
            assert size and size > 0, "risk sizing returned nothing"
            decision = risk.check(wallet, sig, market.market_id,
                                 market.mid_price or 0.5)
            assert decision.approved and decision.size_dollars > 0, \
                f"risk test rejected: {decision.reasons}"
            steps.append(f"risk_test: OK (size=${size:.2f})")

            order = engine.open_position(market, sig, size)
            assert order is not None, "execution produced no order"
            assert order.quantity > 0 and 0 < order.executed_price < 1
            assert order.fees >= 0
            steps.append(f"execution: OK (qty={order.quantity:.2f}, "
                         f"fees={order.fees:.4f}, "
                         f"slip={order.slippage:.4f})")

            engine.update_positions({market.market_id: market})
            rows = db.open_positions()
            assert any(r["market_id"] == market.market_id for r in rows), \
                "position not recorded"
            steps.append("position: OK")

            # resolve in the market's favour and settle
            market.status = "resolved"
            market.resolution_outcome = "Yes"
            engine.settle_position(rows[-1]["id"], rows[-1], "YES")
            settled = db.query(
                "SELECT * FROM trades WHERE market_id=? AND "
                "action='RESOLVE'", (market.market_id,))
            assert settled, "settlement produced no RESOLVE trade"
            pnl = settled[-1]["pnl"]
            assert pnl > 0, f"canary trade lost money: {pnl}"
            details["pnl"] = round(pnl, 4)
            steps.append(f"resolution: OK (pnl=+{pnl:.4f})")

            final = db.init_wallet(100.0).balance
            assert final > 100.0, f"wallet did not grow: {final}"
            steps.append(f"pnl: OK (wallet 100.00 -> {final:.2f})")
            db.close()

        details["passed"] = True
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        details["passed"] = False
        details["error"] = f"{type(exc).__name__}: {exc}"

    if live_db_path:
        try:
            live = Database(live_db_path)
            live.save_canary(details["passed"],
                             {"steps": steps, "error": details["error"],
                              "pnl": details["pnl"]})
            live.set_sys("canary_last", {"ts": time.time(),
                                         "passed": details["passed"]})
            live.close()
        except Exception:  # noqa: BLE001
            pass
    return details


if __name__ == "__main__":
    print(json.dumps(run_canary(), indent=2))
