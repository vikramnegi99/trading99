"""Stage 1: cheap programmatic market filtering + feature extraction.

Goal: 1000 raw markets -> 100-200 candidates -> (AI stage) 5-20 opportunities.
Every threshold is configurable via Config.
"""
import logging
from typing import Dict, List

from config import Config
from models.entities import MarketSnapshot

logger = logging.getLogger("strategy.filter")


def passes_filter(m: MarketSnapshot, cfg: Config) -> tuple:
    """Return (ok: bool, reason: str). Cheap checks only, no AI calls."""
    if not m.is_valid():
        return False, "invalid_data"
    if m.status != "open":
        return False, "not_open"
    if m.liquidity < cfg.MIN_LIQUIDITY:
        return False, "low_liquidity"
    if m.volume < cfg.MIN_VOLUME:
        return False, "low_volume"
    if m.spread > cfg.MAX_SPREAD:
        return False, "wide_spread"
    d = m.days_to_resolution()
    if d is None:
        return False, "no_resolution_date"
    if d < cfg.MIN_DAYS_TO_RESOLUTION:
        return False, "resolving_too_soon"
    if d > cfg.MAX_DAYS_TO_RESOLUTION:
        return False, "too_far_out"
    if len(m.outcomes) != 2:
        return False, "not_binary"   # keep the engine simple: YES/NO only
    return True, "ok"


def filter_markets(markets: List[MarketSnapshot],
                   cfg: Config) -> List[MarketSnapshot]:
    out, skip = [], {}
    seen = set()
    for m in markets:
        if m.market_id in seen:               # duplicate protection
            continue
        seen.add(m.market_id)
        ok, reason = passes_filter(m, cfg)
        if ok:
            out.append(m)
        else:
            skip[reason] = skip.get(reason, 0) + 1
    logger.info("filter: %s -> %s candidates (skipped: %s)",
                len(markets), len(out), skip)
    return out


def extract_features(m: MarketSnapshot) -> Dict[str, float]:
    """Programmatic features fed to the AI / heuristic analyzers."""
    h = [p for p in (m.price_history or []) if 0 < p < 1]
    feats = {
        "liquidity": m.liquidity,
        "volume": m.volume,
        "spread": m.spread,
        "days_to_resolution": m.days_to_resolution() or 0.0,
        "mid_price": m.mid_price or 0.5,
        "history_len": float(len(h)),
        "momentum": 0.0,
        "volatility": 0.0,
        "mean_dev": 0.0,
    }
    if len(h) >= 5:
        n = min(10, len(h))
        recent = h[-n:]
        feats["momentum"] = (h[-1] - h[-n]) / n
        mean = sum(recent) / len(recent)
        feats["mean_dev"] = h[-1] - mean
        feats["volatility"] = (sum((x - mean) ** 2 for x in recent)
                               / len(recent)) ** 0.5
    return feats
