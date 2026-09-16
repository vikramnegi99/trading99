"""Market data sources.

Each source yields MarketSnapshot objects. Public/official APIs are used
wherever they exist - no scraping. The mock source exists for tests,
backtesting and offline demos.
"""
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from typing import List

from models.entities import MarketSnapshot
from utils.http import HttpClient, TTLCache

logger = logging.getLogger("data.sources")


class MarketSource(ABC):
    @abstractmethod
    def fetch_markets(self, limit: int = 500) -> List[MarketSnapshot]:
        ...

    @abstractmethod
    def name(self) -> str:
        ...


# --------------------------------------------------------------------- polymarket
class PolymarketSource(MarketSource):
    """Public Gamma API (no auth, read-only market data).

    Docs: https://gamma-api.polymarket.com/markets
    """
    BASE = "https://gamma-api.polymarket.com"

    def __init__(self, cache_ttl: int = 300):
        self.http = HttpClient(timeout=15, retries=3, rate_limit=0.3,
                               cache=TTLCache(ttl_seconds=cache_ttl))

    def name(self):
        return "polymarket"

    @staticmethod
    def _parse_json_field(raw):
        # Polymarket returns JSON-encoded strings for list fields.
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        try:
            import json
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _to_snapshot(self, m: dict) -> MarketSnapshot:
        outcomes = [str(o) for o in self._parse_json_field(m.get("outcomes"))]
        prices = []
        for p in self._parse_json_field(m.get("outcomePrices")):
            try:
                prices.append(float(p))
            except (TypeError, ValueError):
                prices.append(0.0)
        best_bid = m.get("bestBid")
        best_ask = m.get("bestAsk")
        history = []
        for h in self._parse_json_field(m.get("priceHistory")):
            try:
                history.append(float(h.get("p", h.get("price"))))
            except (TypeError, ValueError):
                continue
        spread = 0.0
        try:
            spread = float(m.get("spread") or 0.0)
        except (TypeError, ValueError):
            pass
        return MarketSnapshot(
            market_id=str(m.get("id", "")),
            question=str(m.get("question", ""))[:500],
            outcomes=outcomes,
            outcome_prices=prices,
            volume=float(m.get("volumeNum") or m.get("volume") or 0.0),
            liquidity=float(m.get("liquidityNum") or m.get("liquidity") or 0.0),
            spread=spread,
            best_bid=float(best_bid) if best_bid is not None else None,
            best_ask=float(best_ask) if best_ask is not None else None,
            status="resolved" if (m.get("umaResolved") or m.get("resolved")) else "open",
            resolution_outcome=m.get("resolvedOutcome") or None,
            created_at=self._ts(m.get("createdAt")),
            end_date=self._ts(m.get("endDate")),
            url=str(m.get("slug", "")).join(("https://polymarket.com/market/", "")) if m.get("slug") else "",
            price_history=history,
        )

    @staticmethod
    def _ts(iso):
        if not iso:
            return None
        try:
            from datetime import datetime, timezone
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")) \
                .astimezone(timezone.utc).timestamp()
        except Exception:  # noqa: BLE001
            return None

    def fetch_markets(self, limit: int = 500) -> List[MarketSnapshot]:
        """Paginated fetch of open markets."""
        snapshots: List[MarketSnapshot] = []
        offset = 0
        page_size = min(100, max(10, limit))
        while len(snapshots) < limit:
            data = self.http.get(
                f"{self.BASE}/markets",
                params={
                    "active": "true", "closed": "false", "archived": "false",
                    "order": "volumeNum", "ascending": "false",
                    "limit": page_size, "offset": offset,
                },
            )
            if not isinstance(data, list) or not data:
                break
            for m in data:
                try:
                    s = self._to_snapshot(m)
                    if s.is_valid():
                        snapshots.append(s)
                except Exception as exc:  # noqa: BLE001 - skip broken rows
                    logger.debug("skip malformed market %s: %s", m.get("id"), exc)
            if len(data) < page_size:
                break
            offset += page_size
        logger.info("polymarket: fetched %s valid markets", len(snapshots))
        return snapshots


# --------------------------------------------------------------------- mock
class MockMarketSource(MarketSource):
    """Deterministic fake markets with price history - tests/backtest/demo.

    Random seed is fixed by default so results are reproducible.
    """

    QUESTIONS = [
        "Will it rain in Mumbai this weekend?",
        "Will BTC close above $100k by Friday?",
        "Will India win the next cricket match?",
        "Will the central bank cut rates this month?",
        "Will the new phone launch happen in March?",
        "Will the government pass the bill before July?",
        "Will the startup announce a funding round?",
        "Will the festival break attendance record?",
        "Will the satellite launch succeed this quarter?",
        "Will the stock index hit a new high this year?",
    ]

    def __init__(self, n_markets: int = 60, seed: int = 42, history_len: int = 30):
        self.rng = random.Random(seed)
        self.n_markets = n_markets
        self.history_len = history_len
        self._markets = None

    def name(self):
        return "mock"

    def _gen_market(self, i: int) -> MarketSnapshot:
        rng = self.rng
        p_true = rng.uniform(0.1, 0.9)
        # random-walk price history around the "true" probability
        history, p = [], p_true
        for _ in range(self.history_len):
            p = min(0.97, max(0.03, p + rng.gauss(0, 0.05)))
            history.append(round(p, 4))
        current = history[-1]
        spread = round(rng.uniform(0.005, 0.04), 4)
        bid = max(0.01, current - spread / 2)
        ask = min(0.99, current + spread / 2)
        return MarketSnapshot(
            market_id=f"mock-{i:04d}",
            question=self.QUESTIONS[i % len(self.QUESTIONS)],
            outcomes=["Yes", "No"],
            outcome_prices=[round(current, 4), round(1 - current, 4)],
            volume=round(rng.uniform(1e4, 5e6), 2),
            liquidity=round(rng.uniform(2e3, 8e5), 2),
            spread=spread,
            best_bid=round(bid, 4),
            best_ask=round(ask, 4),
            status="open",
            created_at=time.time() - 86400 * 30,
            end_date=time.time() + 86400 * rng.uniform(2, 120),
            url=f"https://example.com/mock/{i}",
            price_history=history,
        )

    def fetch_markets(self, limit: int = 500) -> List[MarketSnapshot]:
        if self._markets is None:
            self._markets = [self._gen_market(i) for i in range(self.n_markets)]
        return self._markets[:limit]

    # helpers used by tests/backtest -------------------------------
    def set_seed(self, seed: int):
        self.rng = random.Random(seed)
        self._markets = None

    def advance_prices(self):
        """Move every mock market one step along a fresh random walk."""
        for m in self._markets or []:
            p = m.price_history[-1] if m.price_history else 0.5
            p = min(0.97, max(0.03, p + self.rng.gauss(0, 0.05)))
            m.price_history.append(round(p, 4))
            m.outcome_prices = [p, 1 - p]
            m.best_bid = max(0.01, p - m.spread / 2)
            m.best_ask = min(0.99, p + m.spread / 2)

    def resolve_all(self) -> dict:
        """Resolve every open mock market. Returns {market_id: outcome}."""
        results = {}
        for m in self._markets or []:
            p = m.price_history[-1] if m.price_history else 0.5
            win = self.rng.random() < p
            m.status = "resolved"
            m.resolution_outcome = m.outcomes[0] if win else m.outcomes[1]
            results[m.market_id] = m.resolution_outcome
        return results


class FailingSource(MarketSource):
    """Always raises - used to test API-failure handling."""

    def name(self):
        return "failing"

    def fetch_markets(self, limit: int = 500) -> List[MarketSnapshot]:
        raise ConnectionError("simulated API outage")
