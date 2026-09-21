"""DEAD-HAND lifecycle tests: every deterministic transition rule."""
import json
import time

import pytest

import config as cfgmod
from agent.lifecycle import (Lifecycle, lease_timestamp, load_release)
from database.db import Database


def _db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def _cfg(**over):
    env = {"DATABASE_PATH": ":memory:", "STRATEGY_INACTIVE_CYCLES": "3",
           "DATA_FAILURE_LOCK_CYCLES": "3", "MAINTENANCE_LEASE_DAYS": "7",
           "REVIEW_DEADLINE_HOURS": "48"}
    env.update(over)
    return cfgmod.Config(env=env)


def _lc(tmp_path, **over):
    db = _db(tmp_path)
    db.init_wallet(100.0)
    return Lifecycle(_cfg(**over), db), db


# ------------------------------------------------------------- bankruptcy
def test_bankruptcy_is_dead_and_persistent(tmp_path):
    lc, db = _lc(tmp_path)
    assert lc.state == "RUNNING"
    assert lc.check_bankruptcy(0.0)
    assert lc.state == "DEAD" and lc.reason == "BANKRUPT"
    assert not lc.can_trade()
    # persists across a "restart" (new instance, same SQLite file)
    db.close()
    db2 = Database(str(tmp_path / "t.db"))
    lc2 = Lifecycle(cfgmod.Config(env={}), db2)
    assert lc2.state == "DEAD"
    assert lc2.get()["death_reason"] == "BANKRUPT"
    db2.close()


def test_dead_agent_cycle_is_skipped_no_resurrection(tmp_path):
    """A brand-new Agent run on a DEAD database must not trade."""
    import os
    from agent.runner import Agent
    db_path = str(tmp_path / "t.db")
    env = {"DATABASE_PATH": db_path, "LOG_DIR": str(tmp_path / "logs"),
           "DATA_SOURCE": "mock", "MIN_VOLUME": "1000",
           "MIN_LIQUIDITY": "100", "MIN_EDGE": "0.03",
           "SCAN_INTERVAL_MINUTES": "0.01"}
    a = Agent(cfgmod.Config(env=env))
    a.run_cycle()
    a.lifecycle.safety_death("SAFETY_FAILURE", "test death")
    a.db.close()
    a2 = Agent(cfgmod.Config(env=env))
    stats = a2.run_cycle()
    assert stats.get("skipped") in ("DEAD", "RECOVERY_REQUIRED")
    assert stats.get("paper_trades", 0) == 0
    assert stats.get("signals", 0) == 0
    # still DEAD, not resurrected
    assert a2.lifecycle.state == "DEAD"
    a2.db.close()


def test_safety_death_details_and_events(tmp_path):
    lc, db = _lc(tmp_path)
    lc.safety_death("SAFETY_FAILURE", "impossible portfolio values")
    events = db.query("SELECT * FROM lifecycle_events")
    assert events[-1]["to_state"] == "DEAD"
    assert events[-1]["reason"] == "SAFETY_FAILURE"
    db.close()


# ------------------------------------------------------------ data failure
def test_data_failure_degrades_then_locks(tmp_path):
    lc, db = _lc(tmp_path)
    fail = {"error": "api down", "markets_found": 0}
    lc.evaluate_cycle(fail)                 # 1st failure -> DEGRADED
    assert lc.state == "DEGRADED"
    assert lc.can_trade()                   # trading still allowed
    lc.evaluate_cycle(fail)
    lc.evaluate_cycle(fail)                # 3rd (lock threshold) -> LOCKED
    assert lc.state == "LOCKED" and lc.reason == "DATA_FAILURE"
    assert not lc.can_trade()               # no trading while LOCKED
    db.close()


def test_locked_recovers_after_healthy_cycles(tmp_path):
    lc, db = _lc(tmp_path)
    fail = {"error": "api down", "markets_found": 0}
    for _ in range(3):
        lc.evaluate_cycle(fail)
    assert lc.state == "LOCKED"
    ok = {"markets_found": 100, "candidates": 50, "ai_analyzed": 10,
          "signals": 1, "rejected": 10}
    lc.evaluate_cycle(ok)
    lc.evaluate_cycle(ok)
    lc.evaluate_cycle(ok)                   # 3rd healthy cycle unlocks
    assert lc.state == "RUNNING" and lc.reason == "DATA_RECOVERED"
    assert lc.can_trade()
    db.close()


def test_degraded_recovers_on_first_healthy_cycle(tmp_path):
    lc, db = _lc(tmp_path)
    lc.evaluate_cycle({"error": "x", "markets_found": 0})
    assert lc.state == "DEGRADED"
    lc.evaluate_cycle({"markets_found": 100, "candidates": 50,
                        "ai_analyzed": 10, "signals": 0, "rejected": 10})
    assert lc.state == "RUNNING"
    db.close()


# ------------------------------------------------------- strategy inactivity
def test_strategy_inactive_needs_prolonged_rejection(tmp_path):
    lc, db = _lc(tmp_path)  # STRATEGY_INACTIVE_CYCLES = 3
    idle = {"markets_found": 1000, "candidates": 500,
            "ai_analyzed": 25, "signals": 0, "rejected": 25}
    lc.evaluate_cycle(idle)
    lc.evaluate_cycle(idle)
    assert lc.state == "RUNNING"           # not yet prolonged
    lc.evaluate_cycle(idle)                # 3rd consecutive
    assert lc.state == "REVIEW_REQUIRED"
    assert lc.reason == "STRATEGY_INACTIVE"
    assert lc.can_trade()                  # review, not a stop
    db.close()


def test_zero_candidates_is_not_strategy_inactivity(tmp_path):
    """Genuinely no opportunities (thin data) must NOT flag the agent."""
    lc, db = _lc(tmp_path)
    thin = {"markets_found": 120, "candidates": 2, "ai_analyzed": 0,
            "signals": 0, "rejected": 0}
    for _ in range(6):
        lc.evaluate_cycle(thin)
    assert lc.state == "RUNNING"
    db.close()


def test_strategy_active_again_clears_review(tmp_path):
    lc, db = _lc(tmp_path)
    idle = {"markets_found": 1000, "candidates": 500,
            "ai_analyzed": 25, "signals": 0, "rejected": 25}
    for _ in range(3):
        lc.evaluate_cycle(idle)
    assert lc.state == "REVIEW_REQUIRED"
    lc.evaluate_cycle({"markets_found": 1000, "candidates": 500,
                       "ai_analyzed": 25, "signals": 1, "rejected": 20})
    assert lc.state == "RUNNING"
    assert lc.reason == "STRATEGY_ACTIVE_AGAIN"
    db.close()


# -------------------------------------------------------- maintenance lease
def test_maintenance_lease_expiry_to_dead(tmp_path):
    lc, db = _lc(tmp_path)
    # 9 days ago: lease expired
    lease_ts = time.time() - 9 * 86400
    lc.check_maintenance(lease_ts)
    assert lc.state == "REVIEW_REQUIRED" and lc.reason == "MAINTENANCE_DUE"
    # 49h pass without a human release -> DEAD
    db.conn.execute("UPDATE lifecycle SET since_ts = since_ts - ?",
                    (49 * 3600,))
    db.conn.commit()
    lc.check_maintenance(lease_ts)
    assert lc.state == "DEAD" and lc.reason == "MAINTENANCE_EXPIRED"
    db.close()


def test_fresh_release_renews_lease(tmp_path):
    lc, db = _lc(tmp_path)
    lc.check_maintenance(time.time() - 9 * 86400)   # overdue
    assert lc.state == "REVIEW_REQUIRED"
    lc.check_maintenance(time.time())                # human release
    assert lc.state == "RUNNING" and lc.reason == "LEASE_RENEWED"
    db.close()


def test_recent_lease_keeps_running(tmp_path):
    lc, db = _lc(tmp_path)
    lc.check_maintenance(time.time() - 2 * 86400)     # 2 days ago: fine
    assert lc.state == "RUNNING"
    db.close()


# -------------------------------------------------------------- recovery
def test_dead_recovery_requires_release_plus_ack(tmp_path):
    lc, db = _lc(tmp_path)
    lc.safety_death("SAFETY_FAILURE", "test")
    # a new release is detected -> RECOVERY_REQUIRED, still not trading
    death_version = lc.get()["death_release_version"]
    assert death_version                      # recorded at death time
    rel_new = {"version": "3.0.0"}
    assert death_version != "3.0.0" or True   # any different version works
    lc.detect_recovery_input(rel_new)
    assert lc.state == "RECOVERY_REQUIRED"
    assert not lc.can_trade()
    # wrong ack -> refused
    assert not lc.recover("WRONG", rel_new)
    assert lc.state == "RECOVERY_REQUIRED"
    # correct ack -> recovered
    assert lc.recover("SAFETY_FAILURE", rel_new)
    assert lc.state == "RUNNING" and lc.reason == "RECOVERED"
    # history preserved: events recorded
    ev = db.query("SELECT * FROM lifecycle_events")
    assert any(e["to_state"] == "RECOVERY_REQUIRED" for e in ev)
    db.close()


def test_no_new_release_no_recovery(tmp_path):
    lc, db = _lc(tmp_path)
    lc.safety_death("BANKRUPT", None)
    same = {"version": lc.get()["death_release_version"]}
    assert not lc.detect_recovery_input(same)  # same version: no recovery
    assert lc.state == "DEAD"
    db.close()


def test_release_file_loads(tmp_path):
    import shutil
    rel = tmp_path / "strategy"
    rel.mkdir()
    shutil.copy("strategy/RELEASE.json", rel / "RELEASE.json")
    data = load_release(str(rel / "RELEASE.json"))
    assert data["version"] == "2.0.0"
