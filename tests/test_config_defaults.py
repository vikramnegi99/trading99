"""Config default + override tests for MIN_EDGE (mobile-configurable)."""
import pytest

import config as cfgmod


def test_min_edge_default_is_005():
    # Unset variable -> code default 0.05 (was 0.08 before this change).
    cfg = cfgmod.Config(env={})
    assert cfg.MIN_EDGE == 0.05


def test_min_edge_env_override():
    # Repo variable MIN_EDGE (wired into the workflow as env) wins over the
    # default - this is the mobile-configurable path from GitHub Actions.
    cfg = cfgmod.Config(env={"MIN_EDGE": "0.03"})
    assert cfg.MIN_EDGE == 0.03


def test_min_edge_override_rejects_garbage():
    # A malformed variable must fail loudly, not silently trade on junk.
    with pytest.raises(ValueError):
        cfgmod.Config(env={"MIN_EDGE": "not-a-number"})


def test_safety_switches_unchanged():
    cfg = cfgmod.Config(env={})
    assert cfg.REAL_TRADING is False
    assert cfg.WALLET_CONNECTION == "disabled"
    assert cfg.ORDER_EXECUTION == "simulation_only"
    assert cfg.MIN_CONFIDENCE == 0.60          # untouched by the MIN_EDGE change
    assert cfg.AI_PROVIDER == "heuristic"      # untouched by the MIN_EDGE change
    assert cfg.MAX_POSITION_PERCENT == 0.06    # risk limits untouched
    assert cfg.MAX_OPEN_POSITIONS == 15
    assert cfg.MAX_OPEN_EXPOSURE_PERCENT == 0.25
    assert cfg.MAX_DAILY_LOSS_PERCENT == 0.10
    assert cfg.MAX_DRAWDOWN_PERCENT == 0.30
