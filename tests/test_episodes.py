"""Tests for the PAPER-ONLY 24h Challenge/Episode system.

Covers: episode lifecycle ($100 start, 24h rollover, mark-to-market close),
the performance score (return/drawdown/quality/calibration/risk + churn and
violation penalties), no-trade no-pressure semantics, rejection logging
with exact reasons, and unchanged safety limits.
"""
import json
import time

import pytest

from agent.episode import (CHURN_FREE_TRADES, EpisodeManager, compute_score)
from agent.runner import Agent
from models.entities import Signal, Trade
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


# ------------------------------------------------------------ score (pure)
def test_score_neutral_baseline():
    score, breakdown = compute_score(
        return_pct=0, max_dd_pct=0, n_trades=0,
        avg_pnl_per_trade=0.0, brier=None, risk_violations=0)
    assert score == 50.0
    assert all(v == 0 for k, v in breakdown.items()
               if k in ("trade_quality", "calibration"))


def test_score_zero_trades_never_penalized():
    """No forced trades: an all-HOLD episode scores a clean neutral 50."""
    score, breakdown = compute_score(
        return_pct=0, max_dd_pct=0, n_trades=0,
        avg_pnl_per_trade=0.0, brier=None, risk_violations=0)
    assert score == 50.0
    # even with drawdown from marks, zero trades cannot go below neutral
    # on quality/calibration components
    score2, b2 = compute_score(
        return_pct=-2, max_dd_pct=4, n_trades=0,
        avg_pnl_per_trade=0.0, brier=None, risk_violations=0)
    assert b2["trade_quality"] == 0.0 and b2["calibration"] == 0.0
    assert score2 < score  # only return/drawdown can drag it down


def test_score_return_and_drawdown():
    score, b = compute_score(return_pct=5, max_dd_pct=3, n_trades=1,
                            avg_pnl_per_trade=0.0, brier=None)
    assert b["return"] == 10.0          # +2 per +1%
    assert b["drawdown"] == -3.0        # -1 per 1%


def test_score_trade_quality():
    score, b = compute_score(return_pct=0, max_dd_pct=0, n_trades=10,
                            avg_pnl_per_trade=0.5, brier=None)
    assert b["trade_quality"] == 1.0    # $0.50 avg profit -> +1
    _, b2 = compute_score(return_pct=0, max_dd_pct=0, n_trades=10,
                          avg_pnl_per_trade=-1.0, brier=None)
    assert b2["trade_quality"] == -2.0


def test_score_calibration_brier():
    _, b = compute_score(return_pct=0, max_dd_pct=0, n_trades=4,
                         avg_pnl_per_trade=0, brier=0.0)
    assert b["calibration"] == 10.0    # perfect calibration
    _, b2 = compute_score(return_pct=0, max_dd_pct=0, n_trades=4,
                          avg_pnl_per_trade=0, brier=0.5)
    assert b2["calibration"] == -10.0  # terrible calibration
    # None (not enough samples) stays neutral
    _, b3 = compute_score(return_pct=0, max_dd_pct=0, n_trades=4,
                          avg_pnl_per_trade=0, brier=None)
    assert b3["calibration"] == 0.0


def test_score_excessive_trading_penalty():
    """Churn: trades above the free allowance cost points; below costs none."""
    _, b_ok = compute_score(return_pct=0, max_dd_pct=0,
                            n_trades=CHURN_FREE_TRADES,
                            avg_pnl_per_trade=0, brier=None)
    assert b_ok["churn_penalty"] == 0
    _, b_churn = compute_score(return_pct=0, max_dd_pct=0,
                               n_trades=CHURN_FREE_TRADES + 6,
                               avg_pnl_per_trade=0, brier=None)
    assert b_churn["churn_penalty"] == -6
    # capped at -10
    _, b_max = compute_score(return_pct=0, max_dd_pct=0, n_trades=80,
                             avg_pnl_per_trade=0, brier=None)
    assert b_max["churn_penalty"] == -10


def test_score_risk_violation_penalty():
    _, b1 = compute_score(return_pct=0, max_dd_pct=0, n_trades=0,
                          avg_pnl_per_trade=0, brier=None, risk_violations=2)
    assert b1["risk_discipline"] == -10
    _, b2 = compute_score(return_pct=0, max_dd_pct=0, n_trades=0,
                          avg_pnl_per_trade=0, brier=None, risk_violations=9)
    assert b2["risk_discipline"] == -15  # capped


def test_score_clamped_0_100():
    s_lo, _ = compute_score(return_pct=-50, max_dd_pct=90, n_trades=30,
                            avg_pnl_per_trade=-5, brier=0.5,
                            risk_violations=10)
    s_hi, _ = compute_score(return_pct=+50, max_dd_pct=0, n_trades=5,
                            avg_pnl_per_trade=10, brier=0.0)
    assert s_lo == 0.0
    assert s_hi == 100.0  # 50 + 30 (return) + 10 (quality) + 10 (calib)


# ----------------------------------------------------------- lifecycle
def test_episode_starts_on_first_cycle(tmp_path):
    a = Agent(cfgmod.Config(env=_env(tmp_path / "t.db", tmp_path / "logs")))
    stats = a.run_cycle()
    ep = a.db.active_episode()
    assert ep is not None
    assert stats["episode_id"] == ep["id"]
    assert ep["starting_balance"] == 100.0
    assert a.wallet.balance == 100.0 or stats["paper_trades"] > 0
    a.db.close()


def test_24h_rollover_generates_report_and_resets_wallet(tmp_path):
    env = _env(tmp_path / "t.db", tmp_path / "logs")
    a = Agent(cfgmod.Config(env=env))
    a.run_cycle()
    ep1 = a.db.active_episode()
    assert ep1 is not None

    # open a real position so mark-to-market close is exercised
    markets = a.source.fetch_markets()
    m = markets[0]
    sig = Signal(market_id=m.market_id, market_probability=m.mid_price,
                 estimated_probability=min(0.95, (m.mid_price or 0.5) + 0.3),
                 edge=0.3, confidence=0.9, decision="BUY_YES",
                 reason="episode test")
    a.engine.open_position(m, sig, 5.0)

    # age the episode past 24h (frozen clock, directly in the DB), then run
    # the next cycle with KILL_SWITCH so the ONLY money change is rollover
    a.db.conn.execute("UPDATE episodes SET started_ts = started_ts - ?",
                      (25 * 3600,))
    a.db.conn.commit()
    a.db.close()
    env2 = dict(env)
    env2["KILL_SWITCH"] = "true"
    a2 = Agent(cfgmod.Config(env=env2))
    stats2 = a2.run_cycle()

    ep2 = a2.db.active_episode()
    assert ep2 is not None and ep2["id"] != ep1["id"]

    done = a2.db.query("SELECT * FROM episodes WHERE id=?", (ep1["id"],))
    assert done[0]["status"] == "completed"
    report = json.loads(done[0]["report"])

    # every required report field present
    for key in ("starting_balance", "ending_balance", "return_pct",
                "trades", "win_rate", "max_drawdown_pct", "avg_edge",
                "avg_confidence", "final_score"):
        assert key in report, f"missing report field: {key}"
    assert report["starting_balance"] == 100.0
    assert 0.0 <= report["final_score"] <= 100.0
    assert isinstance(report["top_20_rejections"], list)
    assert report["no_forced_trades"] is True

    # the open positions were marked-to-market closed into episode 1
    closes = a2.db.query(
        "SELECT * FROM trades WHERE action='EPISODE_CLOSE'")
    assert len(closes) >= 1
    assert all(c["pnl"] is not None for c in closes)

    # fresh $100 for the new episode (nothing else could spend it)
    assert a2.wallet.starting_balance == 100.0
    assert a2.wallet.balance == 100.0
    assert stats2["episode_id"] == ep2["id"]
    a2.db.close()


def test_rejections_logged_with_exact_reasons(tmp_path):
    # MIN_EDGE high -> signals get rejected with an exact reason
    env = _env(tmp_path / "t.db", tmp_path / "logs",
               extra={"MIN_EDGE": "0.30"})
    a = Agent(cfgmod.Config(env=env))
    a.run_cycle()
    ep = a.db.active_episode()
    rejections = a.db.query(
        "SELECT * FROM rejections WHERE episode_id=?", (ep["id"],))
    assert len(rejections) > 0
    for r in rejections:
        assert r["reasons"]                      # exact, non-empty reason
        assert r["stage"] in ("signal", "risk")
        assert r["edge"] is not None
    top = a.db.top_rejections(ep["id"], limit=20)
    assert len(top) <= 20
    edges = [t["edge"] for t in top]
    assert edges == sorted(edges, reverse=True)  # best first
    a.db.close()


def test_calibration_brier_from_resolved_trades(tmp_path):
    from database.db import Database
    db = Database(str(tmp_path / "t.db"))
    db.init_wallet(100.0)
    mgr = EpisodeManager(cfgmod.Config(env={}), db)
    now = time.time()
    ep_id = db.start_episode(100.0, started_ts=now - 3600)
    ep = db.query("SELECT * FROM episodes WHERE id=?", (ep_id,))[0]

    # 3 markets: est 0.8/0.2/0.6, actual YES outcomes 1/0/1 -> correct twice
    specs = [("mA", 0.8, 1), ("mB", 0.2, 0), ("mC", 0.6, 1)]
    for mid, est, outcome in specs:
        sig = Signal(market_id=mid, market_probability=0.5,
                     estimated_probability=est, edge=est - 0.5,
                     confidence=0.9, decision="BUY_YES", reason="calib",
                     created_ts=now - 600)
        db.save_ai_decision(sig, "test")
        db.save_trade(Trade(market_id=mid, side="YES", action="BUY",
                           quantity=10, price=0.5, fees=0.0,
                           ts=now - 500))
        won = (outcome == 1)
        db.save_trade(Trade(market_id=mid, side="YES", action="RESOLVE",
                            quantity=10, price=1.0 if won else 0.0, fees=0.0,
                            pnl=5.0 if won else -5.0, ts=now - 400))
    report = mgr.build_report(ep, 100.0, now)
    # brier = mean((0.8-1)^2, (0.2-0)^2, (0.6-1)^2) = (0.04+0.04+0.16)/3
    assert report["brier"] == pytest.approx(0.08, abs=1e-6)
    assert report["calibration_samples"] == 3
    db.close()


def test_safety_limits_unchanged_with_episodes(tmp_path):
    a = Agent(cfgmod.Config(env=_env(tmp_path / "t.db", tmp_path / "logs")))
    a.run_cycle()
    cfg = a.cfg
    assert cfg.REAL_TRADING is False
    assert cfg.WALLET_CONNECTION == "disabled"
    assert cfg.ORDER_EXECUTION == "simulation_only"
    assert cfg.MIN_EDGE == 0.03        # the env value, untouched by episodes
    assert cfg.MIN_CONFIDENCE == 0.60
    assert cfg.MAX_POSITION_PERCENT == 0.06
    assert cfg.MAX_OPEN_EXPOSURE_PERCENT == 0.25
    assert cfg.MAX_DAILY_LOSS_PERCENT == 0.10
    a.db.close()
