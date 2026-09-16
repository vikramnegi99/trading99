"""Edge, probability and signal-validation tests."""
import pytest

from models.entities import Signal, parse_signal
from strategy.edge import evaluate_signal

from tests.conftest import make_market


def _sig(**kw):
    base = dict(
        market_id="mkt-1",
        market_probability=0.42,
        estimated_probability=0.55,
        edge=0.13,
        confidence=0.72,
        decision="BUY_YES",
        reason="test",
        risk_flags=[],
    )
    base.update(kw)
    return Signal(**base)


def test_edge_computed():
    s = _sig()
    assert s.edge == pytest.approx(0.13)


def test_good_signal_passes(cfg):
    ok, _ = evaluate_signal(_sig(), make_market(), cfg)
    assert ok


def test_small_edge_rejected(cfg):
    ok, reasons = evaluate_signal(_sig(edge=0.02), make_market(), cfg)
    assert not ok and any("edge_too_small" in r for r in reasons)


def test_low_confidence_rejected(cfg):
    ok, reasons = evaluate_signal(_sig(confidence=0.40), make_market(), cfg)
    assert not ok and any("confidence" in r for r in reasons)


def test_risk_flags_block(cfg):
    ok, reasons = evaluate_signal(_sig(risk_flags=["ambiguous"]), make_market(), cfg)
    assert not ok and any("risk_flags" in r for r in reasons)


def test_direction_mismatch_rejected(cfg):
    ok, reasons = evaluate_signal(
        _sig(decision="BUY_NO", edge=0.13, estimated_probability=0.55),
        make_market(), cfg)
    assert not ok and "edge_disagrees_with_direction" in reasons


def test_hold_not_traded(cfg):
    ok, _ = evaluate_signal(_sig(decision="HOLD"), make_market(), cfg)
    assert not ok


# ------------------------------------------------- AI response validation
def test_parse_valid_signal():
    d = {"market_id": "abc", "market_probability": 0.4,
         "estimated_probability": 0.6, "edge": 0.2, "confidence": 0.8,
         "decision": "BUY_YES", "reason": "r", "risk_flags": []}
    s = parse_signal(d)
    assert s.decision == "BUY_YES"


def test_parse_rejects_missing_fields():
    with pytest.raises(ValueError):
        parse_signal({"market_id": "abc"})


def test_parse_rejects_out_of_range():
    with pytest.raises(ValueError):
        parse_signal({"market_id": "abc", "market_probability": 1.5,
                      "estimated_probability": 0.5, "confidence": 0.5})


def test_parse_rejects_non_dict():
    with pytest.raises(ValueError):
        parse_signal("garbage")


def test_parse_unknown_decision_becomes_hold():
    s = parse_signal({"market_id": "abc", "market_probability": 0.4,
                      "estimated_probability": 0.5, "confidence": 0.5,
                      "decision": "YOLO"})
    assert s.decision == "HOLD"


def test_llm_json_extraction():
    from ai.analyzer import OpenAIAnalyzer
    class FakeResp:
        @staticmethod
        def post(url, json_body=None, headers=None):
            class D(dict):
                pass
            d = D()
            d["choices"] = [{"message": {"content":
                "Here is my analysis: {\"market_id\": \"x\", "
                "\"market_probability\": 0.4, \"estimated_probability\": 0.7, "
                "\"confidence\": 0.8, \"decision\": \"BUY_YES\"} hope it helps"}}]
            return d
    # patch an analyzer instance's http
    import config as cfgmod
    c = cfgmod.Config(env={"OPENAI_API_KEY": "test-key"})
    from ai.analyzer import OpenAIAnalyzer
    an = OpenAIAnalyzer(c)
    an.http = FakeResp()
    from tests.conftest import make_market
    from strategy.filter import extract_features
    m = make_market(market_id="x")
    sig = an.analyze(m, extract_features(m))
    assert sig is not None and sig.market_id == "x"
    assert sig.decision == "BUY_YES"
