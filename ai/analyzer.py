"""AI analysis layer.

Two providers:
  - HeuristicAnalyzer: free, offline statistical baseline (default). No LLM
    key required - the system is fully functional without any paid API.
  - OpenAIAnalyzer: optional LLM via any OpenAI-compatible endpoint.
    Configured with env vars; automatically falls back to heuristic.

Both return a validated Signal. The agent never trusts raw AI output -
parse_signal() rejects anything malformed.
"""
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Dict, Optional

from config import Config
from models.entities import MarketSnapshot, Signal, parse_signal
from utils.logger import log_event

logger = logging.getLogger("ai")

SYSTEM_PROMPT = """You are a careful prediction-market analyst for a PAPER
TRADING system (simulated money only). Given a market and its features,
estimate the true probability of the first outcome.

Respond ONLY with a JSON object with exactly these fields:
{
  "market_id": "<the market id>",
  "market_probability": <current implied probability of outcome 1, 0..1>,
  "estimated_probability": <your best estimate, 0..1>,
  "edge": <estimated - market>,
  "confidence": <your confidence in the estimate, 0..1>,
  "decision": "BUY_YES" | "BUY_NO" | "HOLD",
  "reason": "<short evidence-based reason>",
  "risk_flags": ["<list of concerns, e.g. ambiguous wording, low liquidity>"]
}
Never inflate confidence. If evidence is weak, return HOLD with low confidence."""


class Analyzer(ABC):
    @abstractmethod
    def analyze(self, market: MarketSnapshot,
                features: Dict[str, float]) -> Optional[Signal]:
        ...

    @abstractmethod
    def name(self) -> str:
        ...


class HeuristicAnalyzer(Analyzer):
    """Statistical baseline: mean reversion + momentum on price history.

    This is deliberately transparent and conservative. It exists so the
    pipeline runs for free; it is NOT a claim of a real edge. Swap in the
    LLM provider or write a custom analyzer for real research.
    """

    def name(self):
        return "heuristic"

    def analyze(self, market: MarketSnapshot, features: Dict) -> Optional[Signal]:
        p = market.mid_price
        if p is None or len(market.price_history or []) < 10:
            # Not enough data for a statistical estimate - never guess.
            return None
        momentum = features.get("momentum", 0.0)
        mean_dev = features.get("mean_dev", 0.0)
        volatility = features.get("volatility", 0.0)

        # Estimate: current price pulled toward recent mean, tilted by momentum.
        estimated = p - 0.5 * mean_dev + 0.25 * momentum
        estimated = min(0.95, max(0.05, estimated))

        # Confidence from liquidity, data depth and stability.
        liq_score = min(1.0, features.get("liquidity", 0) / 100_000.0)
        depth_score = min(1.0, features.get("history_len", 0) / 30.0)
        stab_score = max(0.0, 1.0 - volatility / 0.25)
        confidence = 0.35 + 0.25 * liq_score + 0.2 * depth_score + 0.2 * stab_score
        confidence = min(0.95, confidence)

        edge = estimated - p
        flags = []
        if features.get("spread", 1) > 0.03:
            flags.append("wide_spread")
        if features.get("liquidity", 0) < 10_000:
            flags.append("thin_liquidity")

        if abs(edge) < 0.08 or confidence < 0.60:
            decision = "HOLD"
        elif edge > 0:
            decision = "BUY_YES"
        else:
            decision = "BUY_NO"

        return Signal(
            market_id=market.market_id,
            market_probability=round(p, 4),
            estimated_probability=round(estimated, 4),
            edge=round(edge, 4),
            confidence=round(confidence, 3),
            decision=decision,
            reason=(f"heuristic: p={p:.3f} mean_rev={-0.5*mean_dev:+.3f} "
                    f"momentum={0.25*momentum:+.3f} vol={volatility:.3f}"),
            risk_flags=flags,
        )


class OpenAIAnalyzer(Analyzer):
    """Optional LLM analyzer against an OpenAI-compatible endpoint."""

    def __init__(self, cfg: Config):
        from utils.http import HttpClient
        if not cfg.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY not set")
        self.cfg = cfg
        self.http = HttpClient(timeout=60, retries=2, rate_limit=1.0)
        self.fallback = HeuristicAnalyzer()

    def name(self):
        return "openai"

    def _prompt(self, market: MarketSnapshot, features: Dict) -> str:
        return (
            f"Market question: {market.question}\n"
            f"Outcomes: {market.outcomes} (estimate probability of "
            f"'{market.outcomes[0]}')\n"
            f"Current prices: {market.outcome_prices}\n"
            f"Volume: {market.volume:.0f}, Liquidity: {market.liquidity:.0f}, "
            f"Spread: {market.spread:.3f}\n"
            f"Days to resolution: {features.get('days_to_resolution', 0):.1f}\n"
            f"Recent price history (oldest first): "
            f"{market.price_history[-20:]}\n"
            f"Features: momentum={features.get('momentum', 0):.4f}, "
            f"volatility={features.get('volatility', 0):.4f}\n"
        )

    def analyze(self, market: MarketSnapshot, features: Dict) -> Optional[Signal]:
        try:
            data = self.http.post(
                f"{self.cfg.OPENAI_BASE_URL}/chat/completions",
                json_body={
                    "model": self.cfg.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": self._prompt(market, features)},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 400,
                },
                headers={"Authorization": f"Bearer {self.cfg.OPENAI_API_KEY}"},
            )
            text = data["choices"][0]["message"]["content"].strip()
            # extract first {...} block - models sometimes wrap in prose
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError("no JSON object in response")
            parsed = json.loads(match.group(0))
            parsed.setdefault("market_id", market.market_id)
            sig = parse_signal(parsed)          # strict validation
            # recompute edge from our own numbers, never trust theirs
            sig.edge = round(sig.estimated_probability - sig.market_probability, 4)
            return sig
        except Exception as exc:  # noqa: BLE001 - never let AI errors crash a cycle
            log_event(logger, "llm_analysis_failed",
                      market_id=market.market_id, error=str(exc)[:200])
            return self.fallback.analyze(market, features)


class DuplicateGuard:
    """Prevents re-analyzing the same market within a cooldown window."""

    def __init__(self, cooldown_seconds: int = 3600):
        self.cooldown = cooldown_seconds
        self._seen = {}

    def should_analyze(self, market_id: str, now: float) -> bool:
        last = self._seen.get(market_id)
        if last is not None and now - last < self.cooldown:
            return False
        return True

    def mark(self, market_id: str, now: float):
        self._seen[market_id] = now


def build_analyzer(cfg: Config) -> Analyzer:
    if cfg.AI_PROVIDER == "openai" and cfg.OPENAI_API_KEY:
        try:
            return OpenAIAnalyzer(cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM unavailable (%s), using heuristic", exc)
    return HeuristicAnalyzer()
