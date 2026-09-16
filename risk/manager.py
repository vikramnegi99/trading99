"""Risk management: Kelly-style sizing with strict hard caps.

Every rule here can VETO a trade. Sizing is fractional Kelly capped by
MAX_POSITION_PERCENT; exposure / daily-loss / drawdown / count limits are
absolute - they are never relaxed by a big edge.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from config import Config
from database.db import Database
from models.entities import PaperWallet, Signal

logger = logging.getLogger("risk.manager")


@dataclass
class RiskDecision:
    approved: bool
    size_dollars: float
    reasons: List[str]


class RiskManager:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    # ------------------------------------------------------------ sizing
    def kelly_fraction(self, p: float, price: float) -> float:
        """Fractional Kelly for a binary contract bought at `price`.

        f* = (p - price) / (1 - price), scaled by KELLY_FRACTION.
        """
        if price <= 0 or price >= 1:
            return 0.0
        full = (p - price) / (1.0 - price)
        if full <= 0:
            return 0.0
        return full * self.cfg.KELLY_FRACTION

    def position_size(self, wallet: PaperWallet, sig: Signal,
                      price: float) -> float:
        """Dollar size for a new position, after all caps."""
        kelly = self.kelly_fraction(sig.estimated_probability, price)
        cap = wallet.balance * self.cfg.MAX_POSITION_PERCENT
        size = min(kelly * wallet.balance, cap)
        # never risk more than the balance itself
        return max(0.0, min(size, wallet.balance))

    # ---------------------------------------------------------- vetting
    def check(self, wallet: PaperWallet, sig: Signal,
              market_id: str, price: float) -> RiskDecision:
        reasons = []
        cfg = self.cfg

        if cfg.KILL_SWITCH:
            return RiskDecision(False, 0.0, ["kill_switch_active"])
        if cfg.REAL_TRADING:
            return RiskDecision(False, 0.0, ["unsafe_config"])  # unreachable

        open_rows = self.db.open_positions()
        if len(open_rows) >= cfg.MAX_OPEN_POSITIONS:
            reasons.append(f"max_open_positions:{len(open_rows)}")

        if any(r["market_id"] == market_id for r in open_rows):
            reasons.append("duplicate_open_position")

        exposure = sum(r["quantity"] * r["avg_entry_price"] for r in open_rows)
        max_exposure = wallet.starting_balance * cfg.MAX_OPEN_EXPOSURE_PERCENT
        proposed = self.position_size(wallet, sig, price)
        if exposure + proposed > max_exposure:
            room = max(0.0, max_exposure - exposure)
            proposed = min(proposed, room)
            if proposed <= 0.5:  # below half a dollar - not worth a position
                reasons.append("max_open_exposure_reached")

        # daily loss protection
        import time
        day_ago = time.time() - 86400
        rows = self.db.query(
            "SELECT pnl FROM trades WHERE pnl IS NOT NULL AND ts > ?",
            (day_ago,))
        realized_today = sum(r["pnl"] for r in rows)
        if realized_today < 0 and abs(realized_today) >= \
                wallet.starting_balance * cfg.MAX_DAILY_LOSS_PERCENT:
            reasons.append(f"daily_loss_limit:{realized_today:.2f}")

        # drawdown protection from the equity curve
        eq = [r["equity"] for r in
              self.db.query("SELECT equity FROM equity_curve ORDER BY id")]
        if eq:
            peak = max(eq)
            dd = (peak - eq[-1]) / peak if peak > 0 else 0.0
            if dd >= cfg.MAX_DRAWDOWN_PERCENT:
                reasons.append(f"max_drawdown:{dd:.2f}")

        if proposed < 0.5 and "max_open_exposure_reached" not in reasons:
            reasons.append("position_too_small")

        approved = not reasons
        return RiskDecision(approved, proposed if approved else 0.0, reasons)
