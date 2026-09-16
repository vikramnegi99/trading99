"""Edge / mispricing detection: turn an AI Signal into a trade decision.

A large estimated edge alone is NOT enough - every configurable condition
must pass before a signal becomes a trade.
"""
import logging

from config import Config
from models.entities import MarketSnapshot, Signal

logger = logging.getLogger("strategy.edge")


def evaluate_signal(sig: Signal, m: MarketSnapshot, cfg: Config) -> tuple:
    """Return (trade: bool, reasons: list[str]).

    All thresholds from Config must pass; risk_flags from the AI are treated
    as hard blockers.
    """
    reasons = []
    if sig.decision == "HOLD":
        reasons.append("ai_hold")
        return False, reasons
    if sig.risk_flags:
        reasons.append(f"risk_flags:{sig.risk_flags}")
        return False, reasons
    if abs(sig.edge) < cfg.MIN_EDGE:
        reasons.append(f"edge_too_small:{sig.edge:.3f}")
        return False, reasons
    if sig.confidence < cfg.MIN_CONFIDENCE:
        reasons.append(f"confidence_too_low:{sig.confidence:.2f}")
        return False, reasons
    if m.spread > cfg.MAX_SPREAD:
        reasons.append(f"spread_too_wide:{m.spread:.3f}")
        return False, reasons
    if m.liquidity < cfg.MIN_LIQUIDITY:
        reasons.append(f"liquidity_too_low:{m.liquidity:.0f}")
        return False, reasons

    # Sanity: decision direction must agree with the sign of the edge.
    if sig.decision == "BUY_YES" and sig.edge <= 0:
        reasons.append("edge_disagrees_with_direction")
        return False, reasons
    if sig.decision == "BUY_NO" and sig.edge >= 0:
        reasons.append("edge_disagrees_with_direction")
        return False, reasons

    # Edge must survive estimated transaction costs.
    cost = m.spread / 2 + cfg.SLIPPAGE_PERCENT + (cfg.FEE_PERCENT if cfg.SIMULATED_FEES else 0)
    if abs(sig.edge) <= cost:
        reasons.append(f"edge_below_costs:{abs(sig.edge):.3f}<={cost:.3f}")
        return False, reasons

    return True, reasons
