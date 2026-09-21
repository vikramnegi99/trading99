"""V2 dashboard tests: professional terminal sections, lifecycle panels,
canary indicator, rejection analytics, no fake data, no placeholders."""
import json
import re

from agent.runner import Agent
from dashboard.generate import generate_dashboard
import config as cfgmod

from tests.test_integration_runs import _env


def _generate(tmp_path, **extra):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    env = _env(run / "trading.db", run / "logs", extra)
    cfg = cfgmod.Config(env=env)
    a = Agent(cfg)
    a.run_cycle()  # some state so tables are non-empty
    # record a canary result so the indicator renders
    a.db.save_canary(True, {"steps": ["market: OK"], "error": None,
                            "pnl": 1.0})
    a.db.save_provider_health("mock", 2, 0, 12.5, 300, 300, 5, 5)
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


def test_dashboard_v2_sections_present(tmp_path):
    html = _generate(tmp_path)
    for section in ("AI PAPER TRADING TERMINAL", "RUNNING",
                   "Signal Funnel", "24h Challenge",
                   "Risk Panel", "System Health", "Rejection Analytics",
                   "Strategy Performance", "Episode History",
                   "Lifecycle Events", "Achievements",
                   "CANARY: PASS", "NO REAL ORDERS"):
        assert section in html, f"missing V2 section: {section}"
    # inline SVG equity chart, no external CDN
    assert "<svg" in html and "polyline" in html
    assert "http://" not in html and "https://" not in html.split("<footer>")[0]
    # mobile-first meta
    assert 'name="viewport"' in html


def test_dashboard_deadhand_panel_when_not_running(tmp_path):
    html = _generate(tmp_path)
    assert "DEAD-HAND" not in html or "REVIEW REQUIRED" not in html  # running: no banner
    run = tmp_path / "run"
    from agent.lifecycle import Lifecycle
    from database.db import Database
    import config as cfgmod
    db = Database(str(run / "trading.db"))
    lc = Lifecycle(cfgmod.Config(env={}), db)
    lc.safety_death("SAFETY_FAILURE", "test banner")
    cfg = cfgmod.Config(env=_env(run / "trading.db", run / "logs"))
    generate_dashboard(cfg, db, str(run / "dead.html"))
    html2 = open(run / "dead.html", encoding="utf-8").read()
    assert "DEAD" in html2 and "SAFETY_FAILURE" in html2
    assert "--recover --ack SAFETY_FAILURE" in html2   # recovery steps shown
    db.close()


def test_dashboard_rejection_analytics_categories(tmp_path):
    html = _generate(tmp_path, extra={"MIN_EDGE": "0.30"})
    # with a huge MIN_EDGE every signal is rejected with exact reasons
    assert "EDGE TOO SMALL" in html or "AI HOLD" in html
