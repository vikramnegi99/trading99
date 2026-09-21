"""Canary tests: the paper-engine proof pipeline."""
import json

from canary.canary import run_canary, _canary_market
import config as cfgmod
from database.db import Database


def test_canary_full_pipeline_passes(tmp_path):
    result = run_canary()
    assert result["passed"] is True, result["error"]
    steps = " | ".join(result["steps"])
    for stage in ("market", "features", "ai_signal", "edge_test",
                  "risk_test", "execution", "position", "resolution", "pnl"):
        assert stage in steps, f"canary stage missing: {stage}"
    assert result["pnl"] > 0


def test_canary_is_isolated_from_live_bankroll(tmp_path):
    """The canary must NEVER touch the live database's wallet."""
    live = Database(str(tmp_path / "live.db"))
    w = live.init_wallet(100.0)
    live.save_canary(True, {"steps": []})  # pre-existing row
    before = live.query("SELECT * FROM trades")
    result = run_canary(live_db_path=str(tmp_path / "live.db"))
    assert result["passed"] is True
    # wallet untouched
    w2 = live.init_wallet(100.0)
    assert w2.balance == 100.0 and w2.total_pnl == 0.0
    # no trades leaked into the live db
    assert live.query("SELECT * FROM trades") == before
    # only the canary RESULT row was appended
    rows = live.query("SELECT * FROM canary_results")
    assert len(rows) == 2
    assert rows[-1]["passed"] == 1
    assert live.latest_canary()["passed"] == 1
    live.close()


def test_canary_market_has_known_opportunity():
    m = _canary_market()
    from strategy.filter import extract_features
    from ai.analyzer import HeuristicAnalyzer
    cfg = cfgmod.Config(env={})
    sig = HeuristicAnalyzer(cfg).analyze(m, extract_features(m))
    assert sig is not None
    assert sig.decision == "BUY_YES"
    assert sig.edge >= cfg.MIN_EDGE
