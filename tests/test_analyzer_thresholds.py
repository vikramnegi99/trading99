"""Analyzer threshold-consistency and clamp-artifact tests.

Proves:
1. HeuristicAnalyzer uses the CONFIGURED MIN_EDGE / MIN_CONFIDENCE, not
   hard-coded values (the old 0.08 hard-code silently overrode MIN_EDGE).
2. Low-price clamping (the 0.05 probability floor) can never manufacture a
   high-confidence trade signal: clamped estimates are flagged and their
   confidence is capped strictly below MIN_CONFIDENCE, so they HOLD.
3. Unclamped low/mid-price markets are NOT blanket-blocked - only the
   clamp artifact is.
"""
import pytest

from ai.analyzer import HeuristicAnalyzer
from config import Config
from models.entities import MarketSnapshot
from strategy.edge import evaluate_signal


def _market(p=0.5):
    return MarketSnapshot(
        market_id="m1", question="test?", outcomes=["Yes", "No"],
        outcome_prices=[p, 1 - p], volume=50_000, liquidity=50_000,
        spread=0.01, price_history=[p] * 15,
    )


def _features(**over):
    f = {"liquidity": 50_000.0, "volume": 50_000.0, "spread": 0.01,
         "history_len": 15.0, "momentum": 0.0, "volatility": 0.05,
         "mean_dev": 0.0, "days_to_resolution": 5.0, "mid_price": 0.5}
    f.update(over)
    return f


# ------------------------------------------------ configured MIN_EDGE is used
def test_analyzer_uses_configured_min_edge_not_hardcoded():
    """edge = +0.06: ABOVE MIN_EDGE=0.05 but BELOW the old hard-coded 0.08.

    Under the old code this signal was forced to HOLD by the analyzer even
    though the configured threshold said it was tradeable.
    """
    cfg = Config(env={})
    a = HeuristicAnalyzer(cfg)
    # estimated = 0.5 - 0.5 * (-0.12) = 0.56 -> edge +0.06, not clamped
    sig = a.analyze(_market(p=0.5), _features(mean_dev=-0.12))
    assert sig is not None
    assert sig.edge == pytest.approx(0.06, abs=1e-6)
    assert sig.confidence >= cfg.MIN_CONFIDENCE
    assert "clamped_estimate" not in sig.risk_flags
    assert sig.decision == "BUY_YES"

    # same market, but with MIN_EDGE ABOVE the edge -> analyzer holds
    cfg2 = Config(env={"MIN_EDGE": "0.07"})
    a2 = HeuristicAnalyzer(cfg2)
    sig2 = a2.analyze(_market(p=0.5), _features(mean_dev=-0.12))
    assert sig2.decision == "HOLD"


def test_analyzer_uses_configured_min_confidence():
    cfg = Config(env={"MIN_CONFIDENCE": "0.90"})
    a = HeuristicAnalyzer(cfg)
    sig = a.analyze(_market(p=0.5), _features(mean_dev=-0.12))
    # raw confidence ~0.735 < configured 0.90 -> must HOLD
    assert sig.confidence < 0.90
    assert sig.decision == "HOLD"


def test_repo_variable_min_edge_flows_into_analyzer():
    """The mobile-configurable path: env MIN_EDGE (repo variable) must
    reach the analyzer, lowering OR raising the effective threshold."""
    a = HeuristicAnalyzer(Config(env={"MIN_EDGE": "0.03"}))
    # edge 0.03 < 0.05 default but >= 0.03 configured -> directional
    sig = a.analyze(_market(p=0.5), _features(mean_dev=-0.06))
    assert sig.edge == pytest.approx(0.03, abs=1e-6)
    assert sig.decision == "BUY_YES"


# ------------------------------------------------------ clamp artifacts
def test_low_price_clamp_cannot_create_high_confidence_signal():
    """The exact live bug: a market priced 0.0005 gets est clamped to the
    0.05 floor, manufacturing a 0.0495 'edge' with high confidence."""
    cfg = Config(env={"MIN_EDGE": "0.04"})  # edge 0.0495 WOULD pass this
    a = HeuristicAnalyzer(cfg)
    sig = a.analyze(_market(p=0.0005), _features(mean_dev=0.0, momentum=0.0))
    assert sig is not None
    assert sig.estimated_probability == 0.05          # the floor...
    assert sig.edge == pytest.approx(0.0495, abs=1e-6)  # ...the artifact...
    assert "clamped_estimate" in sig.risk_flags
    assert sig.confidence < cfg.MIN_CONFIDENCE           # ...but untrusted
    assert sig.decision == "HOLD"
    # and the strategy layer rejects it independently (defense in depth)
    ok, reasons = evaluate_signal(sig, _market(p=0.0005), cfg)
    assert not ok and reasons


def test_high_price_clamp_cannot_create_high_confidence_signal():
    """Mirror image: market priced 0.9995 -> est clamped to 0.95 ceiling,
    manufacturing a -0.0495 'BUY_NO edge'."""
    cfg = Config(env={"MIN_EDGE": "0.04"})
    a = HeuristicAnalyzer(cfg)
    sig = a.analyze(_market(p=0.9995), _features(mean_dev=0.0, momentum=0.0))
    assert sig.estimated_probability == 0.95
    assert sig.edge == pytest.approx(-0.0495, abs=1e-6)
    assert "clamped_estimate" in sig.risk_flags
    assert sig.confidence < cfg.MIN_CONFIDENCE
    assert sig.decision == "HOLD"


def test_unclamped_low_price_market_not_blanket_blocked():
    """Only the CLAMP is distrusted: a low-priced market whose raw estimate
    stays inside the bounds still produces a normal signal."""
    cfg = Config(env={})
    a = HeuristicAnalyzer(cfg)
    # p=0.30, raw estimate 0.38 (inside bounds) -> edge +0.08
    sig = a.analyze(_market(p=0.30), _features(mean_dev=-0.16))
    assert sig.estimated_probability == pytest.approx(0.38, abs=1e-6)
    assert "clamped_estimate" not in sig.risk_flags
    assert sig.confidence >= cfg.MIN_CONFIDENCE
    assert sig.decision == "BUY_YES"


def test_clamp_confidence_cap_is_below_any_min_confidence():
    """Even if MIN_CONFIDENCE is lowered by config, a clamped estimate is
    always capped strictly below it - the cap tracks the setting."""
    for mc in ("0.45", "0.60", "0.80"):
        cfg = Config(env={"MIN_CONFIDENCE": mc})
        a = HeuristicAnalyzer(cfg)
        sig = a.analyze(_market(p=0.0005), _features())
        assert sig.confidence < cfg.MIN_CONFIDENCE
        assert sig.decision == "HOLD"
