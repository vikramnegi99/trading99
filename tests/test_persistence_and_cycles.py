"""Persistence, API-failure and end-to-end cycle tests."""
import pytest

from agent.runner import Agent
from data.sources import FailingSource, MockMarketSource
from models.entities import PaperWallet


def test_database_persists_wallet(db):
    w = db.init_wallet(100.0)
    w.balance = 123.45
    db.save_wallet(w)
    db.conn.commit()
    # simulate restart: new connection to same file
    from database.db import Database
    db2 = Database(db.path)
    w2 = db2.init_wallet(0)
    assert w2.balance == 123.45
    assert w2.starting_balance == 100.0
    db2.close()


def test_positions_persist(db):
    from models.entities import PaperPosition
    db.save_position(PaperPosition(market_id="m1", question="q", side="YES",
                                   quantity=10, avg_entry_price=0.4,
                                   mark_price=0.5, cost_basis=4.0))
    assert db.find_open_position("m1") is not None


def test_failing_source_handled_by_cycle(tmp_path):
    """API outage must not crash the cycle - state is kept, scan logged."""
    import config as cfgmod
    cfg = cfgmod.Config(env={"DATABASE_PATH": str(tmp_path / "t.db"),
                             "DATA_SOURCE": "failing",
                             "LOG_DIR": str(tmp_path / "logs")})
    agent = Agent(cfg)
    stats = agent.run_cycle()
    assert "error" in stats
    assert stats["balance"] == 100.0  # wallet untouched
    scans = agent.db.recent("scans", 5)
    assert len(scans) == 1
    agent.db.close()


def test_full_cycle_with_mock_source(tmp_path):
    import config as cfgmod
    cfg = cfgmod.Config(env={"DATABASE_PATH": str(tmp_path / "t.db"),
                             "DATA_SOURCE": "mock",
                             "LOG_DIR": str(tmp_path / "logs"),
                             "MIN_VOLUME": "1000",
                             "MIN_LIQUIDITY": "100",
                             "MIN_EDGE": "0.03"})
    agent = Agent(cfg)
    stats = agent.run_cycle()
    assert stats["markets_found"] == 60
    assert stats["candidates"] > 0
    assert stats["ai_analyzed"] > 0
    assert stats["balance"] + stats.get("equity", 0) >= 100 - 40  # sane
    assert stats["open_positions"] >= 0
    agent.db.close()


def test_mock_source_runs_multiple_cycles(tmp_path):
    import config as cfgmod
    cfg = cfgmod.Config(env={"DATABASE_PATH": str(tmp_path / "t.db"),
                             "DATA_SOURCE": "mock",
                             "LOG_DIR": str(tmp_path / "logs"),
                             "MIN_VOLUME": "1000",
                             "MIN_LIQUIDITY": "100",
                             "MIN_EDGE": "0.03",
                             "SCAN_INTERVAL_MINUTES": "0.01"})
    agent = Agent(cfg)
    for _ in range(3):
        agent.run_cycle()
        agent.source.advance_prices()
    assert len(agent.db.recent("scans", 10)) == 3
    agent.db.close()


def test_state_survives_restart(tmp_path):
    import config as cfgmod
    env = {"DATABASE_PATH": str(tmp_path / "t.db"), "DATA_SOURCE": "mock",
           "LOG_DIR": str(tmp_path / "logs"), "MIN_VOLUME": "1000",
           "MIN_LIQUIDITY": "100", "MIN_EDGE": "0.03"}
    a1 = Agent(cfgmod.Config(env=dict(env)))
    a1.run_cycle()
    bal = a1.wallet.balance
    n_positions = len(a1.db.open_positions())
    a1.db.close()

    a2 = Agent(cfgmod.Config(env=dict(env)))
    assert a2.wallet.balance == bal
    assert len(a2.db.open_positions()) == n_positions
    a2.db.close()


def test_backtest_runs(tmp_path):
    import config as cfgmod
    cfg = cfgmod.Config(env={"DATABASE_PATH": str(tmp_path / "unused.db"),
                             "LOG_DIR": str(tmp_path / "logs")})
    from backtest.engine import run_backtest
    report = run_backtest(cfg, steps=10)
    assert report["mode"] == "BACKTEST"
    assert report["n_trades"] >= 0
    assert "total_return_pct" in report


def test_real_trading_env_rejected():
    import config as cfgmod
    with pytest.raises(cfgmod.SafetyError):
        cfgmod.Config(env={"REAL_TRADING": "true"})


def test_private_key_env_rejected():
    import config as cfgmod
    with pytest.raises(cfgmod.SafetyError):
        cfgmod.Config(env={"PRIVATE_KEY": "abc"})
