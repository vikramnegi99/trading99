"""Integration tests simulating SEPARATE GitHub Actions runs.

The workflow only carries `data/trading.db` between runs (committed at the
end of each run after a WAL checkpoint). These tests simulate exactly that:
run a cycle in one "run" directory, copy ONLY trading.db to a second "run"
directory (as a git checkout would), and verify the agent in run 2 sees
identical wallet / positions / history. Also verifies resolved markets
settle positions with correct P&L.
"""
import os
import shutil

import pytest

from agent.runner import Agent
from models.entities import Signal
import config as cfgmod


def _env(db_path, log_dir, extra=None):
    env = {
        "DATABASE_PATH": str(db_path),
        "DATA_SOURCE": "mock",
        "LOG_DIR": str(log_dir),
        "MIN_VOLUME": "1000",
        "MIN_LIQUIDITY": "100",
        "MIN_EDGE": "0.03",
        "SCAN_INTERVAL_MINUTES": "10",
    }
    if extra:
        env.update(extra)
    return env


def _open_test_position(agent, dollar_size=5.0):
    """Open a deterministic YES position on the first mock market."""
    markets = agent.source.fetch_markets()
    m = markets[0]
    sig = Signal(
        market_id=m.market_id,
        market_probability=m.mid_price,
        estimated_probability=min(0.95, (m.mid_price or 0.5) + 0.3),
        edge=0.3,
        confidence=0.9,
        decision="BUY_YES",
        reason="integration test",
    )
    order = agent.engine.open_position(m, sig, dollar_size)
    assert order is not None, "test position failed to open"
    return m, order


# --------------------------------------------------------------- persistence
def test_state_persists_across_simulated_actions_runs(tmp_path):
    """Run 1 writes state -> only trading.db is 'committed' -> Run 2 must
    inherit wallet, open positions and full history."""
    run1 = tmp_path / "run1"
    run2 = tmp_path / "run2"
    run1.mkdir()
    run2.mkdir()

    # ---- "Actions run 1"
    a1 = Agent(cfgmod.Config(env=_env(run1 / "trading.db", run1 / "logs")))
    m1, order1 = _open_test_position(a1)
    a1.run_cycle()
    bal1 = a1.wallet.balance
    n_pos = len(a1.db.open_positions())
    n_trades = a1.db.query("SELECT count(*) c FROM trades")[0]["c"]
    n_scans = len(a1.db.recent("scans", 100))
    a1.db.close()
    assert n_pos >= 1 and n_trades >= 1 and n_scans == 1

    # The -wal file is NOT committed by the workflow; it must be flushed
    wal = run1 / "trading.db-wal"
    assert not wal.exists() or os.path.getsize(wal) == 0

    # ---- simulate git checkout: only trading.db carries over
    shutil.copy(run1 / "trading.db", run2 / "trading.db")

    # ---- "Actions run 2" (fresh process, fresh source, same state file)
    a2 = Agent(cfgmod.Config(env=_env(run2 / "trading.db", run2 / "logs")))
    assert a2.wallet.balance == pytest.approx(bal1), "wallet must survive runs"
    assert len(a2.db.open_positions()) == n_pos, "positions must survive runs"
    assert a2.db.query("SELECT count(*) c FROM trades")[0]["c"] == n_trades
    assert len(a2.db.recent("scans", 100)) == n_scans

    # run 2 must continue the history, not reset it
    s2 = a2.run_cycle()
    assert 0 < s2["balance"] < 100          # sane simulated balance
    assert s2["balance"] == pytest.approx(a2.wallet.balance, abs=0.01)
    assert len(a2.db.recent("scans", 100)) == n_scans + 1
    assert a2.db.query("SELECT count(*) c FROM trades")[0]["c"] >= n_trades
    a2.db.close()


# ------------------------------------------------------------------- dedup
def test_cross_run_ai_dedup(tmp_path):
    """Markets analyzed in run 1 must NOT be re-analyzed in run 2 within
    the cooldown window (persisted via the ai_decisions table)."""
    run1 = tmp_path / "run1"
    run2 = tmp_path / "run2"
    run1.mkdir()
    run2.mkdir()

    a1 = Agent(cfgmod.Config(env=_env(run1 / "trading.db", run1 / "logs")))
    s1 = a1.run_cycle()
    assert s1["ai_analyzed"] > 0
    n_decisions = a1.db.query("SELECT count(*) c FROM ai_decisions")[0]["c"]
    assert n_decisions == s1["ai_analyzed"]
    a1.db.close()

    shutil.copy(run1 / "trading.db", run2 / "trading.db")

    a2 = Agent(cfgmod.Config(env=_env(run2 / "trading.db", run2 / "logs")))
    s2 = a2.run_cycle()
    # dedup must hold: NO market analyzed in run 1 may be re-analyzed now.
    # (Run 2 still spends its budget on NEW markets - 60 candidates, cap 25 -
    # so the correct check is "no market appears twice in ai_decisions".)
    dup = a2.db.query(
        "SELECT market_id, count(*) c FROM ai_decisions "
        "GROUP BY market_id HAVING c > 1")
    assert not dup, f"markets re-analyzed across runs: {dup[:3]}"
    total = a2.db.query("SELECT count(*) c FROM ai_decisions")[0]["c"]
    assert total == s1["ai_analyzed"] + s2["ai_analyzed"]
    assert s2["ai_analyzed"] > 0   # budget spent on new markets, not repeats
    a2.db.close()


# --------------------------------------------------------------- resolution
def test_resolved_markets_settle_with_pnl(tmp_path):
    """A resolved market must settle the position, record realized P&L and
    pay the winning side - detected via by-id polling, because resolved
    markets no longer appear in the active-market feed.

    KILL_SWITCH=true so no NEW trades open - the only balance change must
    come from the resolution settlement.
    """
    run = tmp_path / "run"
    run.mkdir()
    a = Agent(cfgmod.Config(env=_env(run / "trading.db", run / "logs",
                                    {"KILL_SWITCH": "true"})))

    m, order = _open_test_position(a)
    qty = order.quantity
    entry = order.executed_price
    bal_before = a.wallet.balance

    # market resolves YES (prices snap to 1/0 like the real API)
    outcome = a.source.resolve_market(m.market_id, yes_wins=True)
    assert outcome == "Yes"

    stats = a.run_cycle()
    assert stats["resolved"] == 1, "resolution must be detected and settled"

    # position is closed
    assert a.db.find_open_position(m.market_id) is None

    # RESOLVE trade recorded with correct realized P&L
    res = a.db.query("SELECT * FROM trades WHERE action='RESOLVE'")
    assert len(res) == 1
    assert res[0]["pnl"] == pytest.approx(qty * 1.0 - order.notional)
    assert res[0]["side"] == "YES"

    # wallet credited the winning payout
    assert a.wallet.balance == pytest.approx(bal_before + qty)
    a.db.close()


def test_resolved_market_wrong_side_loses_stake(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    a = Agent(cfgmod.Config(env=_env(run / "trading.db", run / "logs",
                                    {"KILL_SWITCH": "true"})))

    m, order = _open_test_position(a)
    qty = order.quantity
    bal_before = a.wallet.balance

    a.source.resolve_market(m.market_id, yes_wins=False)
    stats = a.run_cycle()
    assert stats["resolved"] == 1

    res = a.db.query("SELECT * FROM trades WHERE action='RESOLVE'")
    assert res[0]["pnl"] == pytest.approx(-order.notional)
    assert a.wallet.balance == pytest.approx(bal_before)  # nothing paid back
    a.db.close()


def test_unresolved_market_does_not_settle(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    a = Agent(cfgmod.Config(env=_env(run / "trading.db", run / "logs")))
    m, order = _open_test_position(a)
    a.run_cycle()  # market still open
    assert a.db.find_open_position(m.market_id) is not None
    assert a.db.query("SELECT count(*) c FROM trades WHERE action='RESOLVE'")[0]["c"] == 0
    a.db.close()
