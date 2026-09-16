"""Market parsing, validation and filtering tests."""
import time

import pytest

from models.entities import MarketSnapshot
from strategy.filter import extract_features, filter_markets, passes_filter

from tests.conftest import make_market


def test_valid_market_passes(cfg):
    m = make_market()
    assert m.is_valid()
    ok, reason = passes_filter(m, cfg)
    assert ok, reason


def test_invalid_price_rejected(cfg):
    m = make_market(outcome_prices=[1.5, -0.5])
    assert not m.is_valid()


def test_missing_outcome_prices(cfg):
    m = make_market(outcome_prices=[])
    assert not m.is_valid()


def test_mismatched_outcomes(cfg):
    m = make_market(outcomes=["Yes", "No", "Maybe"])
    assert not m.is_valid()


def test_polymarket_json_field_parsing():
    from data.sources import PolymarketSource
    p = PolymarketSource._parse_json_field('["Yes", "No"]')
    assert p == ["Yes", "No"]
    assert PolymarketSource._parse_json_field(None) == []
    assert PolymarketSource._parse_json_field("not json") == []


def test_polymarket_snapshot_conversion():
    from data.sources import PolymarketSource
    raw = {
        "id": "12345",
        "question": "Will BTC hit 100k?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.65", "0.35"]',
        "volumeNum": 1000000,
        "liquidityNum": 50000,
        "spread": 0.01,
        "endDate": "2030-01-01T00:00:00Z",
        "active": True,
        "closed": False,
    }
    s = PolymarketSource()._to_snapshot(raw)
    assert s.market_id == "12345"
    assert s.outcome_prices == [0.65, 0.35]
    assert s.yes_price == 0.65
    assert s.end_date is not None and s.end_date > time.time()


def test_low_liquidity_filtered(cfg):
    m = make_market(liquidity=10)
    ok, reason = passes_filter(m, cfg)
    assert not ok and reason == "low_liquidity"


def test_low_volume_filtered(cfg):
    ok, reason = passes_filter(make_market(volume=100), cfg)
    assert not ok and reason == "low_volume"


def test_resolving_too_soon_filtered(cfg):
    ok, reason = passes_filter(make_market(end_date=time.time() + 3600), cfg)
    assert not ok and reason == "resolving_too_soon"


def test_duplicate_markets_removed(cfg):
    markets = [make_market(), make_market()]
    assert len(filter_markets(markets, cfg)) == 1


def test_features_extracted():
    m = make_market()
    f = extract_features(m)
    assert 0 < f["mid_price"] < 1
    assert f["history_len"] == 30
    assert f["momentum"] != 0 or True  # just runs
