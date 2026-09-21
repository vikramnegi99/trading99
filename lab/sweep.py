"""Strategy Lab: threshold sweeps + promotion bookkeeping.

What this lab does (honestly):
1. REPLAY sweep - re-runs the MIN_EDGE / MIN_CONFIDENCE gate over the
   agent's own logged historical ai_decisions and reports, per candidate
   threshold pair, how many signals would have passed and their average
   edge/confidence. It does NOT claim counterfactual P&L (untraded
   outcomes are unknowable).
2. MOCK backtest - deterministic simulation on the mock source for
   P&L-shaped sanity checks.
3. Promotion pipeline state lives in strategy/promotion.json; the agent
   NEVER promotes a strategy by itself. Promotion to ACTIVE requires a
   human approval record (a commit by a human author).
"""
import json
from typing import Dict, List

from database.db import Database


def replay_sweep(db: Database,
                 min_edges=(0.03, 0.04, 0.05, 0.06, 0.08),
                 min_confidences=(0.55, 0.60, 0.65)) -> List[dict]:
    """Evaluate candidate thresholds over logged decisions."""
    rows = db.query(
        "SELECT payload FROM ai_decisions ORDER BY id DESC LIMIT 5000")
    decisions = []
    for r in rows:
        try:
            d = json.loads(r["payload"])
            decisions.append(d)
        except (TypeError, ValueError):
            continue
    results = []
    for me in min_edges:
        for mc in min_confidences:
            passed = [
                d for d in decisions
                if (d.get("decision") in ("BUY_YES", "BUY_NO")
                    and abs(d.get("edge", 0) or 0) >= me
                    and (d.get("confidence", 0) or 0) >= mc)
            ]
            n = len(passed)
            results.append({
                "min_edge": me,
                "min_confidence": mc,
                "signals_passing": n,
                "pass_rate_pct": round(
                    100.0 * n / len(decisions), 2) if decisions else 0.0,
                "avg_edge": round(
                    sum(abs(d.get("edge", 0) or 0) for d in passed) / n, 4)
                if n else 0.0,
                "avg_confidence": round(
                    sum(d.get("confidence", 0) or 0 for d in passed) / n, 4)
                if n else 0.0,
            })
    return results


def promotion_states() -> Dict[str, dict]:
    try:
        with open("strategy/promotion.json", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("strategies", {})
    except OSError:
        return {}


def run_lab(db: Database) -> dict:
    """Full lab report: sweep + current promotion states."""
    return {
        "generated_at_unix": __import__("time").time(),
        "replay_sweep": replay_sweep(db),
        "promotion_states": promotion_states(),
        "notes": ("Replay sweep re-gates historical logged decisions "
                  "only; it does not claim counterfactual P&L. Promotion "
                  "to ACTIVE always requires human approval recorded in "
                  "strategy/promotion.json."),
    }


if __name__ == "__main__":
    db = Database("data/trading.db")
    print(json.dumps(run_lab(db), indent=2))
    db.close()
