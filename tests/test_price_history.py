"""Price-history enrichment tests (live feeds don't include history)."""
from agent.runner import Agent
from data.sources import PolymarketSource
from models.entities import MarketSnapshot
import config as cfgmod

from tests.conftest import make_market
from tests.test_integration_runs import _env


def _agent(tmp_path, extra=None):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    env = _env(run / "trading.db", run / "logs", extra)
    return Agent(cfgmod.Config(env=env))


def test_clob_price_history_parsing():
    src = PolymarketSource()

    class FakeHttp:
        @staticmethod
        def get(url, params=None, use_cache=True):
            assert "prices-history" in url
            assert params["market"] == "tok-1"
            return {"history": [{"t": 1, "p": "0.40"}, {"t": 2, "p": 0.42},
                                {"t": 3, "p": "junk"}, {"t": 4, "p": 1.0},
                                {"t": 5, "p": "0.44"}]}
    src.http = FakeHttp()
    m = make_market(clob_token_id="tok-1")
    hist = src.fetch_price_history(m)
    assert hist == [0.40, 0.42, 0.44]  # junk and out-of-range filtered


def test_clob_price_history_requires_token():
    src = PolymarketSource()
    m = make_market(clob_token_id=None)
    assert src.fetch_price_history(m) is None


def test_runner_enriches_missing_price_history(tmp_path):
    """Markets without history must get it fetched before analysis -
    this is what makes the live Polymarket pipeline functional."""
    a = _agent(tmp_path)
    for m in a.source.fetch_markets():
        m.price_history = []          # simulate the live list feed
    a.source.fetch_price_history = lambda market: [0.40 + 0.005 * i
                                                   for i in range(20)]
    stats = a.run_cycle()
    assert stats["ai_analyzed"] > 0, "enrichment must enable analysis"
    a.db.close()


def test_runner_survives_price_history_failure(tmp_path):
    """A failing history endpoint must not crash the cycle."""
    a = _agent(tmp_path)
    for m in a.source.fetch_markets():
        m.price_history = []
    a.source.fetch_price_history = lambda market: (_ for _ in ()).throw(
        ConnectionError("clob down"))
    stats = a.run_cycle()
    assert stats["markets_found"] == 60   # cycle completed
    assert stats["ai_analyzed"] == 0      # nothing analyzable without history
    a.db.close()
