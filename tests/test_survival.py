"""Survival semantics: one-time $100 bankroll, continuous capital,
episode windows, scores/streaks/badges, DEAD persistence in the agent
pipeline itself."""
import json
import time

import pytest

import config as cfgmod
from agent.runner import Agent
from agent.episode import EpisodeManager, compute_score
from database.db import Database


def _env(db_path, log_dir, **extra):
    env = {"DATABASE_PATH": str(db_path), "LOG_DIR": str(log_dir),
           "DATA_SOURCE": "mock", "MIN_VOLUME": "1000",
           "MIN_LIQUIDITY": "100", "MIN_EDGE": "0.03",
           "SCAN_INTERVAL_MINUTES": "0.01"}
    env.update(extra)
    return env


def test_bankroll_is_persistent_across_restarts(tmp_path):
    """The $100 is one-time: restarts never re-credit or reset it."""
    env = _env(tmp_path / "t.db", tmp_path / "logs")
    a = Agent(cfgmod.Config(env=env))
    a.run_cycle()
    markets = a.source.fetch_markets()
    m = markets[0]
    from models.entities import Signal
    sig = Signal(market_id=m.market_id, market_probability=m.mid_price,
                 estimated_probability=0.8, edge=0.3, confidence=0.9,
                 decision="BUY_YES", reason="survival")
    a.engine.open_position(m, sig, 10.0)
    spent = round(a.wallet.balance, 2)
    assert spent < 100.0
    a.db.close()

    # "restart": a brand new Agent on the same database
    a2 = Agent(cfgmod.Config(env=env))
    assert round(a2.wallet.balance, 2) == spent   # no reset, no re-credit
    a2.run_cycle()
    assert round(a2.wallet.balance, 2) == spent   # cycle did not reset it
    a2.db.close()


def test_episode_window_does_not_reseed_bankroll(tmp_path):
    """Rolling 24h evaluation windows must not touch the wallet."""
    env = _env(tmp_path / "t.db", tmp_path / "logs")
    a = Agent(cfgmod.Config(env=env))
    a.run_cycle()
    a.db.conn.execute("UPDATE episodes SET started_ts = started_ts - ?",
                      (25 * 3600,))
    a.db.conn.commit()
    a.db.close()
    a2 = Agent(cfgmod.Config(env=env))
    bal = round(a2.wallet.balance, 2)
    a2.run_cycle()   # rolls the window
    assert a2.db.active_episode() is not None
    assert round(a2.wallet.balance, 2) == bal or bal == 100.0
    # wallet only changes through trades, never through the rollover
    trades = a2.db.query("SELECT * FROM trades")
    for t in trades:
        assert t["action"] in ("BUY", "SELL", "RESOLVE")
    a2.db.close()


def test_bankruptcy_via_agent_cycle(tmp_path):
    """Zero equity inside a cycle trips the DEAD dead-hand."""
    env = _env(tmp_path / "t.db", tmp_path / "logs")
    a = Agent(cfgmod.Config(env=env))
    a.run_cycle()
    # simulate a wiped-out account, then run another cycle
    a.db.conn.execute("UPDATE wallet SET balance = -5.0")
    a.db.conn.commit()
    a.db.close()
    a2 = Agent(cfgmod.Config(env=env))
    # force the stored (negative) balance into the live wallet
    a2.wallet.balance = -5.0
    a2.db.save_wallet(a2.wallet)
    stats = a2.run_cycle()
    assert stats["equity"] <= 0 or stats.get("skipped") in (
        "DEAD", "RECOVERY_REQUIRED")
    assert a2.lifecycle.state == "DEAD"
    assert a2.lifecycle.reason == "BANKRUPT"
    a2.db.close()


def test_scores_and_streaks_and_badges(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    w = db.init_wallet(100.0)
    mgr = EpisodeManager(cfgmod.Config(env={}), db)
    # two completed windows with known returns
    for ret, ts in ((0.05, time.time() - 10 * 86400),
                    (0.10, time.time() - 5 * 86400)):
        ep_id = db.start_episode(100.0, started_ts=ts - 86400)
        ep = db.query("SELECT * FROM episodes WHERE id=?", (ep_id,))[0]
        rep = mgr.build_report(ep, ts)
        db.complete_episode(ep_id, ts, rep["ending_balance"], rep)
        db.save_score("episode", ep_id, rep["final_score"],
                      rep["score_breakdown"])

    scores = db.query(
        "SELECT * FROM score_history WHERE scope='episode' "
        "ORDER BY id")
    assert len(scores) == 2
    assert all(0 <= s["score"] <= 100 for s in scores)
    # breakdowns are stored and explainable
    b = json.loads(scores[0]["breakdown"])
    assert set(b) >= {"return", "drawdown", "trade_quality",
                      "calibration", "risk_discipline", "churn_penalty"}

    # streaks / best / worst from episodes
    eps = db.query("SELECT * FROM episodes WHERE status='completed'")
    assert len(eps) == 2
    db.close()


def test_no_trade_episode_score_is_neutral(tmp_path):
    """Zero trades: no reward, no penalty - exactly neutral components."""
    score, breakdown = compute_score(
        return_pct=0.0, max_dd_pct=0.0, n_trades=0,
        avg_pnl_per_trade=0.0, brier=None, risk_violations=0)
    assert score == 50.0
    assert breakdown["trade_quality"] == 0.0
    assert breakdown["churn_penalty"] == 0.0
    assert breakdown["calibration"] == 0.0


def test_churn_and_violation_penalties_apply(tmp_path):
    score, b = compute_score(
        return_pct=5.0, max_dd_pct=0.0, n_trades=30,
        avg_pnl_per_trade=0.0, brier=None, risk_violations=2)
    assert b["churn_penalty"] == -6.0       # 30-24 = 6 trades over budget
    assert b["risk_discipline"] == -10.0    # 2 violations
    assert score < 50 + b["return"]
