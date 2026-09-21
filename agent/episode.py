"""PAPER-ONLY 24-hour evaluation windows ("episodes").

V2 semantics: the $100 virtual bankroll is a ONE-TIME starting capital
that is NEVER reset. An episode is an EVALUATION WINDOW ONLY - it rolls
every 24 hours, records statistics and a score, but:

- the wallet is untouched (continuous capital: $100 -> $103 -> $98 ...)
- open positions are NOT closed at episode boundaries; they stay open
  until the market resolves (or risk rules close them)
- the equity curve is continuous (it is the bankroll's history)

Scoring (0-100, from a neutral 50) is unchanged in spirit:
- return:            +2 pts per +1% window return     (cap +30 / -20)
- drawdown:         -1 pt  per 1% max drawdown         (cap -15)
- trade quality:    +/- per $0.50 avg realized P&L per trade
- calibration:      Brier score of entry estimates vs outcomes
- risk discipline: -5 pts per risk-limit violation     (cap -15)
- churn penalty:    -1 pt per trade above 24            (cap -10)

Zero trades is a valid, unpenalized outcome. Nothing here ever forces a
trade.
"""
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

from config import Config
from database.db import Database
from utils.logger import log_event

logger = logging.getLogger("agent.episode")

EPISODE_HOURS = 24.0
CHURN_FREE_TRADES = 24       # ~1 trade/hour before the churn penalty starts
CALIBRATION_MIN_SAMPLES = 3  # neutral below this many resolved outcomes


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_score(*, return_pct: float, max_dd_pct: float, n_trades: int,
                  avg_pnl_per_trade: float, brier: Optional[float] = None,
                  risk_violations: int = 0) -> Tuple[float, Dict[str, float]]:
    """Deterministic, explainable score. Returns (score, breakdown)."""
    breakdown: Dict[str, float] = {}
    breakdown["return"] = round(_clip(return_pct * 2.0, -20.0, 30.0), 2)
    breakdown["drawdown"] = round(-_clip(max_dd_pct, 0.0, 15.0), 2)
    # No trades -> exactly 0: no reward, no penalty, no pressure to trade.
    breakdown["trade_quality"] = (
        round(_clip(avg_pnl_per_trade * 2.0, -10.0, 10.0), 2)
        if n_trades else 0.0)
    breakdown["calibration"] = (
        round(_clip((0.25 - brier) * 40.0, -10.0, 10.0), 2)
        if brier is not None else 0.0)
    breakdown["risk_discipline"] = round(-min(5.0 * risk_violations, 15.0), 2)
    breakdown["churn_penalty"] = round(
        -min(max(0, n_trades - CHURN_FREE_TRADES), 10), 2)
    score = _clip(50.0 + sum(breakdown.values()), 0.0, 100.0)
    return round(score, 2), breakdown


class EpisodeManager:
    """Owns the 24h evaluation windows. Purely observational: the
    bankroll, positions and equity curve are continuous and untouched."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    # ------------------------------------------------------------ lifecycle
    def ensure(self, current_equity: float) -> Optional[dict]:
        """Close the active window if >24h old, then guarantee one active.

        `current_equity` is the continuous bankroll's equity snapshot that
        the new evaluation window starts from. The bankroll itself is
        NEVER reset."""
        ep = self.db.active_episode()
        now = time.time()
        if ep is None:
            return self._start(now, current_equity)
        if now - ep["started_ts"] >= EPISODE_HOURS * 3600:
            report = self.close_episode(ep)
            log_event(logger, "EPISODE COMPLETE",
                      episode=ep["id"], score=report["final_score"],
                      return_pct=report["return_pct"],
                      trades=report["trades"])
            # persist the episode score for history/streaks
            self.db.save_score("episode", ep["id"],
                               report["final_score"],
                               report["score_breakdown"])
            return self._start(now, report["ending_balance"])
        return dict(ep)

    def _start(self, now: float, starting_equity: float) -> dict:
        # The starting_balance column records the EQUITY SNAPSHOT at the
        # window start (the continuous bankroll is never reset).
        ep_id = self.db.start_episode(round(starting_equity, 2),
                                      started_ts=now)
        log_event(logger, "EPISODE START", episode=ep_id,
                  starting_equity=round(starting_equity, 2))
        return dict(self.db.active_episode())

    def close_episode(self, ep) -> dict:
        """Finalize the window report. Positions are NOT closed: they are
        part of the continuous bankroll and settle at resolution."""
        now = time.time()
        report = self.build_report(ep, now)
        self.db.complete_episode(ep["id"], now, report["ending_balance"],
                                 report)
        return report

    # --------------------------------------------------------------- report
    def build_report(self, ep, ended_ts: float) -> dict:
        db = self.db
        start, end = ep["started_ts"], ended_ts

        trades = [dict(r) for r in db.query(
            "SELECT * FROM trades WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start, end))]
        closed = [t for t in trades if t["pnl"] is not None]
        wins = [t for t in closed if t["pnl"] > 0]
        entries = [t for t in trades if t["action"] == "BUY"]

        scans = [json.loads(r["payload"]) for r in db.query(
            "SELECT payload FROM scans WHERE ts >= ? AND ts <= ?",
            (start, end))]
        orders = [dict(r) for r in db.query(
            "SELECT * FROM orders WHERE ts >= ? AND ts <= ?", (start, end))]

        # equity at window boundaries (continuous bankroll)
        wallet = db.init_wallet(self.cfg.STARTING_BALANCE)
        open_rows = [dict(r) for r in db.open_positions()]
        unrealized = sum((r["mark_price"] - r["avg_entry_price"])
                         * r["quantity"] for r in open_rows)
        ending_equity = wallet.balance + unrealized
        starting_equity = ep["starting_balance"]

        return_pct = round(
            (ending_equity - starting_equity) / starting_equity * 100, 2
        ) if starting_equity else 0.0
        realized = round(sum(t["pnl"] for t in closed), 4)

        # max drawdown within the window (equity curve is continuous, so
        # clamp the peak baseline to the window start)
        eq = [starting_equity] + [r["equity"] for r in db.query(
            "SELECT equity FROM equity_curve WHERE ts >= ? AND ts <= ? "
            "ORDER BY id", (start, end))]
        max_dd = 0.0
        peak = eq[0]
        for e in eq:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak)
        max_dd_pct = round(max_dd * 100, 2)

        dec = [json.loads(r["payload"]) for r in db.query(
            "SELECT payload FROM ai_decisions WHERE ts >= ? AND ts <= ?",
            (start, end))]
        avg_edge = round(sum(abs(d.get("edge", 0) or 0) for d in dec)
                         / len(dec), 4) if dec else 0.0
        avg_confidence = round(
            sum(d.get("confidence", 0) or 0 for d in dec) / len(dec), 4) \
            if dec else 0.0

        brier = self._brier_score(trades)
        risk_violations = sum(
            1 for r in db.query(
                "SELECT reasons FROM rejections WHERE episode_id=? AND "
                "stage='risk'", (ep["id"],))
            if "kill_switch" not in (r["reasons"] or ""))
        data_failures = sum(1 for s in scans if s.get("error"))
        inactive_cycles = self._inactive_time(start, end, scans)

        n_trades = len(closed)
        avg_pnl_per_trade = round(
            sum(t["pnl"] for t in closed) / n_trades, 4) if n_trades else 0.0

        score, breakdown = compute_score(
            return_pct=return_pct, max_dd_pct=max_dd_pct,
            n_trades=n_trades, avg_pnl_per_trade=avg_pnl_per_trade,
            brier=brier, risk_violations=risk_violations)

        return {
            "episode": ep["id"],
            "started_ts": start,
            "ended_ts": end,
            "starting_balance": round(starting_equity, 2),
            "ending_balance": round(ending_equity, 2),
            "return_pct": return_pct,
            "realized_pnl": realized,
            "unrealized_pnl": round(unrealized, 2),
            "scans": len(scans),
            "markets": sum(s.get("markets_found", 0) for s in scans),
            "candidates": sum(s.get("candidates", 0) for s in scans),
            "ai_analyzed": sum(s.get("ai_analyzed", 0) for s in scans),
            "valid_signals": sum(s.get("signals", 0) for s in scans),
            "rejected_signals": sum(s.get("rejected", 0) for s in scans),
            "trades": n_trades,
            "entries": len(entries),
            "wins": len(wins),
            "losses": n_trades - len(wins),
            "win_rate": round(len(wins) / n_trades * 100, 1) if n_trades else 0.0,
            "avg_edge": avg_edge,
            "avg_confidence": avg_confidence,
            "max_drawdown_pct": max_dd_pct,
            "total_fees": round(sum(o.get("fees", 0) or 0 for o in orders), 4),
            "total_slippage": round(
                sum(o.get("slippage", 0) or 0 for o in orders), 6),
            "risk_violations": risk_violations,
            "data_failures": data_failures,
            "strategy_inactive_cycles": inactive_cycles,
            "brier": round(brier, 4) if brier is not None else None,
            "calibration_samples": self._calib_n,
            "final_score": score,
            "score_breakdown": breakdown,
            "no_forced_trades": True,
            "top_20_rejections": self._top_rejections(ep["id"]),
        }

    def _inactive_time(self, start: float, end: float,
                      scans: List[dict]) -> int:
        """Cycles inside the window where data was healthy and analyzed
        markets existed, yet zero valid signals were produced."""
        return sum(
            1 for s in scans
            if s.get("markets_found", 0) >= 100
            and s.get("ai_analyzed", 0) >= 5
            and s.get("signals", 0) == 0
            and s.get("rejected", 0) > 0)

    def _brier_score(self, trades: List[dict]) -> Optional[float]:
        """Brier score of entry estimates vs outcomes for resolved trades."""
        db = self.db
        self._calib_n = 0
        entries = {t["market_id"]: t for t in trades if t["action"] == "BUY"}
        pairs = []
        for t in trades:
            if t["action"] != "RESOLVE":
                continue
            entry = entries.get(t["market_id"])
            if entry is None:
                continue
            row = db.query(
                "SELECT payload FROM ai_decisions WHERE market_id=? "
                "AND ts <= ? ORDER BY ts DESC LIMIT 1",
                (t["market_id"], entry["ts"] + 120))
            if not row:
                continue
            row = row[0]
            try:
                sig = json.loads(row["payload"])
                est = float(sig.get("estimated_probability"))
            except (TypeError, ValueError):
                continue
            won = t["pnl"] > 0
            side = entry["side"]
            outcome = (1.0 if won else 0.0) if side == "YES" \
                else (0.0 if won else 1.0)
            pairs.append((est - outcome) ** 2)
        self._calib_n = len(pairs)
        if len(pairs) < CALIBRATION_MIN_SAMPLES:
            return None
        return sum(pairs) / len(pairs)

    def _top_rejections(self, episode_id: int) -> List[dict]:
        rows = self.db.top_rejections(episode_id, limit=20)
        return [{
            "market_id": r["market_id"],
            "question": r["question"],
            "market_probability": r["market_probability"],
            "estimated_probability": r["estimated_probability"],
            "edge": r["edge"],
            "confidence": r["confidence"],
            "stage": r["stage"],
            "reasons": r["reasons"],
        } for r in rows]
