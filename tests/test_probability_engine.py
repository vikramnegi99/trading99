"""Probability-engine validation matrix (spec section 8).

Covers the price regimes that historically produced artifacts:
0.0005, 0.01, 0.5, 0.99 markets, insufficient history, noisy history,
high spread, low liquidity. A probability clamp must never create a
fake high-confidence edge; weak evidence must lower confidence.
"""
import pytest

import config as cfgmod
from ai.analyzer import HeuristicAnalyzer
from models.entities import MarketSnapshot


def _market(p, hist=None, liquidity=150_000, spread=0.01, volume=400_000):
    return MarketSnapshot(
        market_id="m", question="q?", outcomes=["Yes", "No"],
        outcome_prices=[p, 1 - p], volume=volume, liquidity=liquidity,
        spread=spread, price_history=hist if hist is not None else [p] * 30)


def _feats(m, **over):
    from strategy.filter import extract_features
    f = extract_features(m)
    f.setdefault("liquidity", 150_000)
    f.update(over)
    return f


CFG = cfgmod.Config(env={})
AN = HeuristicAnalyzer(CFG)


# -------------------------------------------------------- price matrix
def test_price_0005_clamped_low_confidence_hold():
    m = _market(0.0005)
    sig = AN.analyze(m, _feats(m))
    assert sig.estimated_probability == 0.05        # floor applied
    assert sig.edge == pytest.approx(0.0495, abs=1e-6)
    assert "clamped_estimate" in sig.risk_flags
    assert sig.confidence < CFG.MIN_CONFIDENCE
    assert sig.decision == "HOLD"


def test_price_01_clamped_market():
    m = _market(0.01)
    sig = AN.analyze(m, _feats(m))
    # raw estimate ~0.01 -> clamped to the 0.05 floor -> fake +0.04 edge
    if sig.estimated_probability == 0.05:
        assert sig.confidence < CFG.MIN_CONFIDENCE
        assert sig.decision == "HOLD"
        assert "clamped_estimate" in sig.risk_flags
    else:  # genuinely estimated above the floor: must be unclamped
        assert "clamped_estimate" not in sig.risk_flags


def test_price_5_normal_market():
    m = _market(0.5)
    sig = AN.analyze(m, _feats(m))
    assert sig is not None
    assert "clamped_estimate" not in sig.risk_flags
    # flat history: edge ~0 -> HOLD, no fake edge from a flat price
    assert sig.decision == "HOLD"


def test_price_99_clamped_ceiling():
    m = _market(0.99)
    sig = AN.analyze(m, _feats(m))
    assert sig.estimated_probability == 0.95
    assert "clamped_estimate" in sig.risk_flags
    assert sig.confidence < CFG.MIN_CONFIDENCE
    assert sig.decision == "HOLD"


# ------------------------------------------------------ data quality
def test_insufficient_history_returns_none():
    m = _market(0.4, hist=[0.4] * 5)
    assert AN.analyze(m, _feats(m)) is None


def test_noisy_history_lowers_confidence():
    flat = _market(0.4, hist=[0.4] * 30)
    noisy = _market(0.4, hist=[0.4] * 20 + [0.2, 0.6, 0.1, 0.7,
                                             0.25, 0.55, 0.15, 0.65,
                                             0.2, 0.6])
    s_flat = AN.analyze(flat, _feats(flat))
    s_noisy = AN.analyze(noisy, _feats(noisy))
    assert s_noisy.confidence < s_flat.confidence


def test_high_spread_flagged():
    m = _market(0.4, spread=0.15)
    sig = AN.analyze(m, _feats(m))
    assert "wide_spread" in sig.risk_flags


def test_low_liquidity_flagged():
    m = _market(0.4, liquidity=5_000)
    sig = AN.analyze(m, _feats(m))
    assert "thin_liquidity" in sig.risk_flags
    assert sig.confidence < 0.95   # weak evidence -> lower confidence


def test_low_price_genuine_estimate_not_blocked():
    """A low-price market whose raw estimate sits INSIDE the bounds must
    not be blanket-blocked - only the clamp artifact is distrusted."""
    # history mean ~0.28, current price 0.25 -> est ~0.265, unclamped
    m = _market(0.25, hist=[0.28] * 29 + [0.25])
    sig = AN.analyze(m, _feats(m))
    assert "clamped_estimate" not in sig.risk_flags
    assert 0.05 < sig.estimated_probability < 0.95
    assert sig.confidence >= CFG.MIN_CONFIDENCE
