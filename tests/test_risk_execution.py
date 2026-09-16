"""Risk management, sizing, execution and P&L tests."""
import pytest

from execution.engine import PaperTradingEngine
from models.entities import PaperWallet, Signal, Trade
from risk.manager import RiskManager

from tests.conftest import make_market


def _sig(**kw):
    base = dict(
        market_id="mkt-1",
        market_probability=0.42,
        estimated_probability=0.60,
        edge=0.18,
        confidence=0.80,
        decision="BUY_YES",
        reason="test",
        risk_flags=[],
    )
    base.update(kw)
    return Signal(**base)


def test_kelly_positive(cfg, db):
    rm = RiskManager(cfg, db)
    assert rm.kelly_fraction(0.6, 0.42) > 0


def test_kelly_zero_when_no_edge(cfg, db):
    rm = RiskManager(cfg, db)
    assert rm.kelly_fraction(0.4, 0.42) == 0


def test_position_size_capped(cfg, db):
    rm = RiskManager(cfg, db)
    w = db.init_wallet(100.0)
    size = rm.position_size(w, _sig(), 0.42)
    assert size <= 100 * cfg.MAX_POSITION_PERCENT  # $6 cap


def test_risk_approves_good_trade(cfg, db):
    rm = RiskManager(cfg, db)
    w = db.init_wallet(100.0)
    d = rm.check(w, _sig(), "mkt-1", 0.42)
    assert d.approved and 0 < d.size_dollars <= 6.0


def test_duplicate_position_blocked(cfg, db):
    from execution.engine import PaperTradingEngine
    engine = PaperTradingEngine(cfg, db, db.init_wallet(100.0))
    engine.open_position(make_market(), _sig(), 5.0)
    rm = RiskManager(cfg, db)
    d = rm.check(db.init_wallet(100.0), _sig(), "mkt-1", 0.42)
    assert not d.approved and any("duplicate" in r for r in d.reasons)


def test_max_open_positions(cfg, db):
    cfg.MAX_OPEN_POSITIONS = 2
    engine = PaperTradingEngine(cfg, db, db.init_wallet(100.0))
    for i in range(3):
        engine.open_position(make_market(market_id=f"m{i}"),
                             _sig(market_id=f"m{i}"), 5.0)
    rm = RiskManager(cfg, db)
    d = rm.check(db.init_wallet(100.0), _sig(market_id="m9"), "m9", 0.42)
    assert not d.approved and any("max_open_positions" in r for r in d.reasons)


def test_kill_switch_blocks(cfg, db):
    cfg.KILL_SWITCH = True
    rm = RiskManager(cfg, db)
    d = rm.check(db.init_wallet(100.0), _sig(), "mkt-1", 0.42)
    assert not d.approved and d.reasons == ["kill_switch_active"]
    cfg.KILL_SWITCH = False


def test_daily_loss_limit(cfg, db):
    import time
    for i in range(5):
        db.save_trade(Trade(market_id="x", side="YES", action="SELL",
                            quantity=1, price=0.5, fees=0, pnl=-2.5,
                            ts=time.time()))
    rm = RiskManager(cfg, db)
    d = rm.check(db.init_wallet(100.0), _sig(), "mkt-1", 0.42)
    assert not d.approved and any("daily_loss" in r for r in d.reasons)


# --------------------------------------------------------------- execution
def test_open_position_charges_wallet(cfg, db):
    w = db.init_wallet(100.0)
    engine = PaperTradingEngine(cfg, db, w)
    order = engine.open_position(make_market(), _sig(), 6.0)
    assert order is not None
    assert order.action == "BUY"
    assert w.balance < 100.0
    assert w.balance > 93.0  # ~$6 + small fees/slippage
    assert order.executed_price > order.requested_price  # slippage on buy


def test_close_position_realizes_pnl(cfg, db):
    w = db.init_wallet(100.0)
    engine = PaperTradingEngine(cfg, db, w)
    engine.open_position(make_market(), _sig(), 6.0)
    row = db.find_open_position("mkt-1")
    assert row is not None
    pnl = engine.close_position(row["id"], "mkt-1", row["side"],
                                row["quantity"], row["avg_entry_price"],
                                0.60, reason="take_profit")
    assert pnl > 0
    assert db.find_open_position("mkt-1") is None


def test_resolution_pnl(cfg, db):
    w = db.init_wallet(100.0)
    engine = PaperTradingEngine(cfg, db, w)
    engine.open_position(make_market(), _sig(), 6.0)
    row = db.find_open_position("mkt-1")
    bal_before = w.balance
    engine.settle_position(row["id"], row, "YES")
    # bought ~14 shares around 0.42 -> payout ~= quantity
    assert w.balance > bal_before
    trades = db.query("SELECT * FROM trades WHERE action='RESOLVE'")
    assert len(trades) == 1


def test_resolution_loses_when_wrong_side(cfg, db):
    w = db.init_wallet(100.0)
    engine = PaperTradingEngine(cfg, db, w)
    engine.open_position(make_market(), _sig(), 6.0)
    row = db.find_open_position("mkt-1")
    bal_before = w.balance
    engine.settle_position(row["id"], row, "NO")
    assert w.balance == pytest.approx(bal_before)  # nothing paid back


def test_insufficient_balance_rejected(cfg, db):
    w = db.init_wallet(100.0)
    w.balance = 1.0
    db.save_wallet(w)
    engine = PaperTradingEngine(cfg, db, w)
    assert engine.open_position(make_market(), _sig(), 6.0) is None


def test_slippage_and_fees_applied(cfg, db):
    w = db.init_wallet(100.0)
    engine = PaperTradingEngine(cfg, db, w)
    order = engine.open_position(make_market(), _sig(), 6.0)
    assert order.fees > 0
    assert order.executed_price > order.requested_price


def test_marking_updates_positions(cfg, db):
    w = db.init_wallet(100.0)
    engine = PaperTradingEngine(cfg, db, w)
    engine.open_position(make_market(), _sig(), 6.0)
    m = make_market(outcome_prices=[0.6, 0.4], best_bid=0.59, best_ask=0.61)
    engine.update_positions({"mkt-1": m})
    row = db.find_open_position("mkt-1")
    assert row["mark_price"] == pytest.approx(0.6, abs=0.01)
