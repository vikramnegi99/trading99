"""Dashboard regression tests (placeholders must be substituted)."""
import re

from agent.runner import Agent
from dashboard.generate import generate_dashboard
import config as cfgmod

from tests.test_integration_runs import _env


def _generate(tmp_path):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    env = _env(run / "trading.db", run / "logs")
    cfg = cfgmod.Config(env=env)
    a = Agent(cfg)
    a.run_cycle()  # some state so tables are non-empty
    out = run / "index.html"
    page = generate_dashboard(cfg, a.db, str(out))
    html = open(page, encoding="utf-8").read()
    a.db.close()
    return html


def test_dashboard_has_no_unsubstituted_placeholders(tmp_path):
    html = _generate(tmp_path)
    # the old bug: literal {starting} / {balance:.2f} style placeholders
    assert "{starting" not in html
    assert "{balance" not in html
    assert not re.search(r"\{(pnl|ret|wr|avg|lw|ll|mdd|unr|trades|pf|n_open)[}:]", html)


def test_dashboard_shows_formatted_wallet_numbers(tmp_path):
    html = _generate(tmp_path)
    assert "$100.00" in html, "starting balance must be formatted, not templated"
    assert "PAPER TRADING ONLY" in html          # mandatory banner
    assert "<table>" in html
