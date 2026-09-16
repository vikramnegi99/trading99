"""Paper trading engine - 100% simulated execution.

No network calls are made here. No real order can ever be placed: the engine
has no code path to any exchange. Orders are filled against the market's
current price plus simulated slippage and fees.
"""
import logging
import time
from typing import Dict, Optional, Tuple

from config import Config
from database.db import Database
from models.entities import (MarketSnapshot, PaperOrder, PaperPosition,
                             PaperWallet, Signal, Trade)
from utils.logger import log_event

logger = logging.getLogger("execution.engine")


class PaperTradingEngine:
    def __init__(self, cfg: Config, db: Database, wallet: PaperWallet):
        cfg.assert_safe()  # hard guard - refuses to construct if unsafe
        self.cfg = cfg
        self.db = db
        self.wallet = wallet

    # ------------------------------------------------------------ helpers
    def _fill_price(self, price: float, action: str) -> Tuple[float, float]:
        """Apply slippage: buy at a worse (higher) price, sell lower."""
        if not self.cfg.REALISTIC_EXECUTION:
            return price, 0.0
        slip = self.cfg.SLIPPAGE_PERCENT * (1 if action == "BUY" else -1)
        return min(0.99, max(0.01, price * (1 + slip))), abs(slip)

    def _fees(self, notional: float) -> float:
        return notional * self.cfg.FEE_PERCENT if self.cfg.SIMULATED_FEES else 0.0

    # ------------------------------------------------------------ opening
    def open_position(self, market: MarketSnapshot, sig: Signal,
                      size_dollars: float) -> Optional[PaperOrder]:
        """Buy YES or NO shares with `size_dollars` of simulated money."""
        assert not self.cfg.REAL_TRADING
        if self.cfg.EXECUTION_DELAY_SECONDS > 0:
            time.sleep(min(self.cfg.EXECUTION_DELAY_SECONDS, 2))

        side = "YES" if sig.decision == "BUY_YES" else "NO"
        base_price = market.mid_price if sig.decision == "BUY_YES" \
            else 1 - (market.mid_price or 0.5)
        base_price = min(0.99, max(0.01, base_price))

        fill, slip = self._fill_price(base_price, "BUY")
        quantity = size_dollars / fill
        notional = quantity * fill
        fees = self._fees(notional)
        total_cost = notional + fees

        if total_cost > self.wallet.balance:
            logger.info("insufficient balance for %s (%.2f > %.2f)",
                        market.market_id, total_cost, self.wallet.balance)
            return None

        order = PaperOrder(
            market_id=market.market_id, side=side, action="BUY",
            quantity=round(quantity, 6), requested_price=round(base_price, 4),
            executed_price=round(fill, 4), fees=round(fees, 6),
            slippage=round(slip, 6), notional=round(notional, 6))
        self.db.save_order(order)
        self.db.save_trade(Trade(market_id=market.market_id, side=side,
                                 action="BUY", quantity=order.quantity,
                                 price=order.executed_price, fees=order.fees))

        pos = PaperPosition(
            market_id=market.market_id, question=market.question, side=side,
            quantity=order.quantity, avg_entry_price=order.executed_price,
            mark_price=order.executed_price, cost_basis=notional)
        self.db.save_position(pos)

        self.wallet.balance -= total_cost
        self.db.save_wallet(self.wallet)
        log_event(logger, "paper_buy", market_id=market.market_id, side=side,
                  qty=order.quantity, price=order.executed_price,
                  fees=order.fees, balance=self.wallet.balance)
        return order

    # ------------------------------------------------- marking / updating
    def update_positions(self, markets: Dict[str, MarketSnapshot]):
        """Mark open positions to current market prices."""
        for row in self.db.open_positions():
            pos_id = row["id"]
            m = markets.get(row["market_id"])
            if not m or m.mid_price is None:
                continue
            mark = m.mid_price if row["side"] == "YES" else 1 - m.mid_price
            self.db.conn.execute("UPDATE positions SET mark_price=? WHERE id=?",
                                 (mark, pos_id))
            self.db.conn.commit()

    # ------------------------------------------------------------ closing
    def close_position(self, pos_id: int, market_id: str, side: str,
                       quantity: float, avg_entry: float,
                       price: float, reason: str = "manual"):
        """Sell shares back at `price` (probability units)."""
        fill, slip = self._fill_price(price, "SELL")
        notional = quantity * fill
        fees = self._fees(notional)
        pnl = (fill - avg_entry) * quantity - fees

        order = PaperOrder(
            market_id=market_id, side=side, action="SELL",
            quantity=round(quantity, 6), requested_price=round(price, 4),
            executed_price=round(fill, 4), fees=round(fees, 6),
            slippage=round(slip, 6), notional=round(notional, 6))
        self.db.save_order(order)
        self.db.save_trade(Trade(market_id=market_id, side=side, action="SELL",
                                 quantity=order.quantity,
                                 price=order.executed_price, fees=order.fees,
                                 pnl=round(pnl, 6)))

        self.wallet.balance += notional - fees
        self.db.conn.execute(
            "UPDATE positions SET quantity=0, status='closed', "
            "realized_pnl=realized_pnl+?, closed_ts=? WHERE id=?",
            (pnl, time.time(), pos_id))
        self.db.conn.commit()
        self.db.save_wallet(self.wallet)
        log_event(logger, "paper_sell", market_id=market_id, reason=reason,
                  qty=quantity, price=fill, pnl=round(pnl, 4),
                  balance=self.wallet.balance)
        return pnl

    def settle_position(self, pos_id: int, row, winning_side: str):
        """Resolve a position at market resolution (pays 1.0 or 0.0/share)."""
        qty = row["quantity"]
        won = (row["side"] == winning_side)
        payout = qty * 1.0 if won else 0.0
        pnl = payout - row["cost_basis"]

        self.wallet.balance += payout
        self.db.conn.execute(
            "UPDATE positions SET quantity=0, status='closed', "
            "realized_pnl=?, mark_price=?, closed_ts=? WHERE id=?",
            (pnl, 1.0 if won else 0.0, time.time(), pos_id))
        self.db.conn.commit()
        self.db.save_trade(Trade(market_id=row["market_id"], side=row["side"],
                                 action="RESOLVE", quantity=qty,
                                 price=1.0 if won else 0.0, fees=0.0,
                                 pnl=round(pnl, 6)))
        self.db.save_wallet(self.wallet)
        log_event(logger, "paper_resolve", market_id=row["market_id"],
                  side=row["side"], won=won, payout=round(payout, 4),
                  pnl=round(pnl, 4), balance=self.wallet.balance)
        return pnl
