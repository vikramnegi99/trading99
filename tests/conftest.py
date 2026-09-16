"""Shared pytest fixtures."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from database.db import Database  # noqa: E402
from models.entities import MarketSnapshot  # noqa: E402


@pytest.fixture
def cfg():
    return Config(env={
        "STARTING_BALANCE": "100",
        "MIN_EDGE": "0.08",
        "MIN_CONFIDENCE": "0.60",
        "MAX_POSITION_PERCENT": "0.06",
        "SLIPPAGE_PERCENT": "0.01",
        "FEE_PERCENT": "0.002",
    })


@pytest.fixture
def db(cfg, tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.init_wallet(cfg.STARTING_BALANCE)
    yield database
    database.close()


def make_market(**overrides) -> MarketSnapshot:
    import time
    base = dict(
        market_id="mkt-1",
        question="Will X happen?",
        outcomes=["Yes", "No"],
        outcome_prices=[0.42, 0.58],
        volume=250_000.0,
        liquidity=50_000.0,
        spread=0.02,
        best_bid=0.41,
        best_ask=0.43,
        status="open",
        created_at=time.time() - 86400 * 10,
        end_date=time.time() + 86400 * 30,
        price_history=[0.40 + 0.01 * (i % 5) for i in range(30)],
    )
    base.update(overrides)
    return MarketSnapshot(**base)
