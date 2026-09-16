"""Core data models. Pure dataclasses, no I/O - easily testable."""
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
import json
import time


def _now() -> float:
    return time.time()


@dataclass
class MarketSnapshot:
    """A snapshot of one prediction market at scan time."""
    market_id: str
    question: str
    outcomes: List[str]
    outcome_prices: List[float]        # current implied probabilities per outcome
    volume: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0                # bid/ask spread (probability units)
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    status: str = "open"               # open | resolved | closed
    resolution_outcome: Optional[str] = None
    created_at: Optional[float] = None
    end_date: Optional[float] = None
    url: str = ""
    price_history: List[float] = field(default_factory=list)  # recent YES prices, oldest first
    snapshot_ts: float = field(default_factory=_now)

    # ------------------------------------------------------------ helpers
    @property
    def yes_price(self) -> Optional[float]:
        """Probability of the FIRST outcome (conventionally YES)."""
        if not self.outcome_prices:
            return None
        return self.outcome_prices[0]

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.yes_price

    def days_to_resolution(self) -> Optional[float]:
        if not self.end_date:
            return None
        return (self.end_date - _now()) / 86400.0

    def resolved_winner(self) -> Optional[str]:
        """Name of the winning outcome, if this market has resolved.

        Prefers an explicit resolution outcome; falls back to the outcome
        whose price pinned to ~1.0 (how the Polymarket API reports resolved
        markets, e.g. outcomePrices ["1", "0"]).
        """
        if not self.outcomes:
            return None
        if self.resolution_outcome and self.resolution_outcome in self.outcomes:
            return self.resolution_outcome
        for name, price in zip(self.outcomes, self.outcome_prices):
            if price >= 0.995:
                return name
        return None

    def is_valid(self) -> bool:
        if not self.market_id or not self.question:
            return False
        if not self.outcomes or not self.outcome_prices:
            return False
        if len(self.outcomes) != len(self.outcome_prices):
            return False
        for p in self.outcome_prices:
            if not (0.0 < p < 1.0):
                return False
        return True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketSnapshot":
        return cls(**d)


@dataclass
class Signal:
    """Structured output of the AI analysis stage (validated)."""
    market_id: str
    market_probability: float
    estimated_probability: float
    edge: float
    confidence: float
    decision: str                    # BUY_YES | BUY_NO | HOLD
    reason: str = ""
    risk_flags: List[str] = field(default_factory=list)
    created_ts: float = field(default_factory=_now)


@dataclass
class PaperOrder:
    market_id: str
    side: str                         # YES | NO
    action: str                       # BUY | SELL
    quantity: float                   # shares
    requested_price: float
    executed_price: float
    fees: float
    slippage: float
    notional: float
    status: str = "filled"            # filled | rejected
    ts: float = field(default_factory=_now)


@dataclass
class PaperPosition:
    market_id: str
    question: str
    side: str                         # YES | NO
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    mark_price: float = 0.0
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    status: str = "open"              # open | closed
    opened_ts: float = field(default_factory=_now)
    closed_ts: Optional[float] = None

    def unrealized_pnl(self) -> float:
        if self.status != "open" or self.quantity <= 0:
            return 0.0
        return (self.mark_price - self.avg_entry_price) * self.quantity


@dataclass
class Trade:
    market_id: str
    side: str
    action: str
    quantity: float
    price: float
    fees: float
    pnl: Optional[float] = None       # set on closes/resolutions
    ts: float = field(default_factory=_now)


@dataclass
class PaperWallet:
    starting_balance: float
    balance: float

    @property
    def total_pnl(self) -> float:
        return self.balance - self.starting_balance

    @property
    def return_pct(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return self.total_pnl / self.starting_balance


def parse_signal(d: dict) -> Signal:
    """Validate and convert an (untrusted) analysis dict into a Signal.

    Raises ValueError on anything malformed. The AI layer NEVER gets the
    benefit of the doubt - invalid output becomes a HOLD or an exception.
    """
    if not isinstance(d, dict):
        raise ValueError("signal must be a dict")

    market_id = d.get("market_id")
    if not isinstance(market_id, str) or not market_id.strip():
        raise ValueError("market_id missing/invalid")

    def _prob(key):
        v = d.get(key)
        if v is None:
            raise ValueError(f"{key} missing")
        v = float(v)
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"{key} out of range: {v}")
        return v

    mp = _prob("market_probability")
    ep = _prob("estimated_probability")
    conf = _prob("confidence")

    decision = str(d.get("decision", "HOLD")).upper()
    if decision not in ("BUY_YES", "BUY_NO", "HOLD"):
        decision = "HOLD"

    edge = ep - mp
    reason = str(d.get("reason", ""))[:1000]
    flags = d.get("risk_flags", [])
    if not isinstance(flags, list):
        flags = [str(flags)]
    flags = [str(f) for f in flags][:20]

    return Signal(
        market_id=market_id.strip(),
        market_probability=mp,
        estimated_probability=ep,
        edge=edge,
        confidence=conf,
        decision=decision,
        reason=reason,
        risk_flags=flags,
    )
