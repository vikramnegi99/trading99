"""Strategy profiles: named parameter sets for the pipeline.

A profile ONLY adjusts decision thresholds (edge / confidence / sizing
fraction). It can NEVER relax the hard risk caps (max position %, max
exposure %, daily loss, drawdown, max open positions) - those stay
exactly as configured in config.py, which remains the single source of
truth. This satisfies "no hard-coded duplicate thresholds": profiles are
an explicit, declarative override layer on top of Config, not hidden
values inside AI code.

States (see strategy/promotion.json):
  CANDIDATE -> BACKTEST -> SHADOW -> ACTIVE -> RETIRED
Promotion to ACTIVE requires a human approval record; the agent never
deploys an unproven strategy by itself.
"""
from typing import Dict

# Decision-threshold overrides per profile. Risk caps deliberately absent.
PROFILES: Dict[str, Dict[str, float]] = {
    "conservative": {
        "MIN_EDGE": 0.06,
        "MIN_CONFIDENCE": 0.65,
        "KELLY_FRACTION": 0.20,
    },
    "balanced": {
        "MIN_EDGE": 0.05,
        "MIN_CONFIDENCE": 0.60,
        "KELLY_FRACTION": 0.25,
    },
    "experimental": {
        "MIN_EDGE": 0.04,
        "MIN_CONFIDENCE": 0.55,
        "KELLY_FRACTION": 0.30,
    },
}


def effective_env(env: Dict[str, str], strategy: str) -> Dict[str, str]:
    """Overlay a profile's thresholds onto an env dict (strings)."""
    out = dict(env)
    if strategy in PROFILES:
        for k, v in PROFILES[strategy].items():
            out[k] = str(v)
    return out


def profile_summary(strategy: str) -> Dict[str, float]:
    return dict(PROFILES.get(strategy, {}))
