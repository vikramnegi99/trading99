"""PAPER-ONLY 24-hour challenge episodes.

Each episode is a self-contained paper-trading challenge:
- starts with a fresh $100 virtual wallet
- runs for 24 hours (checked at the start of every scan cycle)
- positions still open at the end are marked-to-market into the report
- a report + performance score is stored in the `episodes` table

The episode system is strictly OBSERVATIONAL: it never loosens strategy
thresholds and never pressures the agent to trade. Zero trades is a
valid, unpenalized outcome - the trade-quality and calibration
components simply stay neutral.

Scoring (0-100, starting from a neutral 50):
- return:            +2 pts per +1% episode return          (cap +30 up / -20 down:
                     a 15%+ day can max the component; losses can't subtract
                     more than the capital that was risked)
- drawdown:         -1 pt  per 1% max drawdown              (cap -15)
- trade quality:    +/- per $0.50 avg realized P&L per trade (cap +/-10,
                    exactly 0 when no trades were taken)
- calibration:      Brier score of entry estimates vs actual outcomes
                    (cap +/-10, neutral until >= 3 resolved samples)
- risk discipline: -5 pts per risk-limit violation           (cap -15)
- churn penalty:    -1 pt per trade above 24 in the episode    (cap -10)
                    (24 = ~1/hour; punishes EXCESSIVE trading only - this
                    is a penalty for overtrading, never for NOT trading)
"""
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

from config import Config
from database.db import Database
from models.entities import PaperWallet, Trade
from utils.logger import log_event

logger = logging.getLogger("agent.episode")

EPISODE_HOURS = 24.0
EPISODE_CAPITAL = 100.0
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
    # Calibration is neutral until there are enough resolved outcomes.
    breakdown["calibration"] = (
        round(_clip((0.25 - brier) * 40.0, -10.0, 10.0), 2)
        if brier is not None else 0.0)
    breakdown["risk_discipline"] = round(-min(5.0 * risk_violations, 15.0), 2)
    breakdown["churn_penalty"] = round(
        -min(max(0, n_trades - CHURN_FREE_TRADES), 10), 2)
    score = _clip(50.0 + sum(breakdown.values()), 0.0, 100.0)
    return round(score, 2), breakdown


class EpisodeManager:
    """Owns the 24h episode lifecycle. Paper-only, purely observational."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    # ------------------------------------------------------------ lifecycle
    def ensure(self, wallet: PaperWallet):
        """Close the active episode if >24h old, then guarantee one is active.

        Mutates `wallet` in place (same object the engine references) when
        a new episode starts, so every $100 reset propagates everywhere.
        """
        ep = self.db.active_episode()
        now = time.time()
        if ep is None:
            return self._start(wallet, now)
        if now - ep["started_ts"] >= EPISODE_HOURS * 3600:
            report = self.close_episode(ep, wallet)
            log_event(logger, "EPISODE COMPLETE",
                      episode=ep["id"], score=report["final_score"],
                      return_pct=report["return_pct"],
                      trades=report["trades"])
            return self._start(wallet, now)
        return self.db.active_episode()

    def _start(self, wallet: PaperWallet, now: float):
        # The FIRST episode ever adopts the current wallet state as its
        # challenge capital (in production that is exactly the $100 initial
        # bankroll; positions opened before the first cycle keep settling
        # normally). Every later episode is a rollover and starts from a
        # fresh $100 - the previous one was just closed mark-to-market.
        first = self.db.query(
            "SELECT COUNT(*) AS c FROM episodes")[0]["c"] == 0
        if first:
            starting = round(wallet.balance, 2)
        else:
            starting = EPISODE_CAPITAL
            # reset the virtual wallet to exactly $100 in place (the
            # engine holds a reference to this object)
            wallet.starting_balance = EPISODE_CAPITAL
            wallet.balance = EPISODE_CAPITAL
            self.db.save_wallet(wallet)
        ep_id = self.db.start_episode(starting, started_ts=now)
        # The equity curve belongs to the challenge: a fresh episode means
        # a fresh drawdown baseline (also unblocks the drawdown stop).
        self.db.conn.execute("DELETE FROM equity_curve")
        self.db.conn.commit()
        log_event(logger, "EPISODE START", episode=ep_id,
                  capital=starting)
        return self.db.active_episode()

    def close_episode(self, ep, wallet: PaperWallet) -> dict:
        """Mark-to-market any open positions and finalize the report."""
        now = time.time()
        # 1. mark-to-market close every open position into the episode
        for row in self.db.open_positions():
            mark = row["mark_price"] if row["mark_price"] > 0 \
                else row["avg_entry_price"]
            payout = row["quantity"] * mark
            pnl = payout - row["cost_basis"]
            wallet.balance += payout
            self.db.conn.execute(
                "UPDATE positions SET quantity=0, status='closed', "
                "realized_pnl=?, mark_price=?, closed_ts=? WHERE id=?",
                (pnl, mark, now, row["id"]))
            self.db.save_trade(Trade(
                market_id=row["market_id"], side=row["side"],
                action="EPISODE_CLOSE", quantity=row["quantity"],
                price=mark, fees=0.0, pnl=round(pnl, 6)))
        self.db.conn.commit()
        self.db.save_wallet(wallet)
        self.db.save_equity(wallet.balance, wallet.balance)

        # 2. build + persist the report
        report = self.build_report(ep, wallet.balance, now)
        self.db.complete_episode(ep["id"], now, wallet.balance, report)
        return report

    # --------------------------------------------------------------- report
    def build_report(self, ep, ending_balance: float,
                     ended_ts: float) -> dict:
        db = self.db
        start, end = ep["started_ts"], ended_ts

        trades = [dict(r) for r in db.query(
            "SELECT * FROM trades WHERE ts >= ? AND ts <= ? "
            "ORDER BY ts", (start, end))]
        closed = [t for t in trades if t["pnl"] is not None]
        wins = [t for t in closed if t["pnl"] > 0]
        opens = [t for t in trades if t["action"] == "BUY"]

        starting_balance = ep["starting_balance"]
        return_pct = round(
            (ending_balance - starting_balance) / starting_balance * 100, 2
        ) if starting_balance else 0.0

        # max drawdown within the episode window (equity is reset per
        # episode, so this is the challenge's own drawdown)
        eq = [r["equity"] for r in db.query(
            "SELECT equity FROM equity_curve WHERE ts >= ? ORDER BY id",
            (start,))]
        max_dd = 0.0
        peak = 0.0
        for e in eq:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak)
        max_dd_pct = round(max_dd * 100, 2)

        # what the AI estimated during the episode
        dec = [json.loads(r["payload"]) for r in db.query(
            "SELECT payload FROM ai_decisions WHERE ts >= ? AND ts <= ?",
            (start, end))]
        avg_edge = round(sum(abs(d.get("edge", 0) or 0) for d in dec) / len(dec), 4) \
            if dec else 0.0
        avg_confidence = round(
            sum(d.get("confidence", 0) or 0 for d in dec) / len(dec), 4) \
            if dec else 0.0

        # calibration: entry-time estimates vs actual outcomes (traded only)
        brier = self._brier_score(trades, start, end)

        # risk-limit violations: risk-stage vetoes during the episode
        # (kill-switch vetoes are user pauses, not discipline failures)
        risk_violations = sum(
            1 for r in db.query(
                "SELECT reasons FROM rejections WHERE episode_id=? AND "
                "stage='risk'", (ep["id"],))
            if "kill_switch" not in (r["reasons"] or ""))

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
            "starting_balance": round(starting_balance, 2),
            "ending_balance": round(ending_balance, 2),
            "return_pct": return_pct,
            "trades": n_trades,
            "entries": len(opens),
            "win_rate": round(len(wins) / n_trades * 100, 1) if n_trades else 0.0,
            "max_drawdown_pct": max_dd_pct,
            "avg_edge": avg_edge,
            "avg_confidence": avg_confidence,
            "brier": round(brier, 4) if brier is not None else None,
            "calibration_samples": self._calib_n,
            "risk_violations": risk_violations,
            "final_score": score,
            "score_breakdown": breakdown,
            "no_forced_trades": True,   # zero-trade episodes are never penalized
            "top_20_rejections": self._top_rejections(ep["id"]),
        }

    def _brier_score(self, trades: List[dict], start: float,
                     end: float) -> Optional[float]:
        """Brier score of entry estimates vs outcomes for resolved trades.

        Matches each RESOLVE trade to the market's entry (BUY) trade, then
        to the AI decision closest before that entry. Returns None when
        there are fewer than CALIBRATION_MIN_SAMPLES usable pairs.
        """
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
            # est is the probability of YES; convert the side's outcome
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
            "reasons": r["reasons"],   # exact rejection reason, as logged
        } for r in rows]
