"""Agent runner: the full scan cycle, plus the continuous loop.

Cycle: lifecycle gates -> episode window -> fetch -> validate ->
settle resolutions -> filter -> analyze (strategy + optional shadow)
-> signal -> risk -> execute -> mark positions -> wallet/equity ->
persist -> lifecycle evaluation.

V2 survival rules baked in:
- DEAD / RECOVERY_REQUIRED: the cycle is skipped entirely (no silent
  resurrection from GitHub Actions restarts)
- LOCKED: the cycle runs but no new trades
- bankruptcy (equity <= 0) -> DEAD
- impossible execution values -> DEAD (SAFETY_FAILURE)
- provider health and rejection reasons recorded every cycle
"""
import logging
import time
from typing import Dict, List, Optional

from ai.analyzer import DuplicateGuard, build_analyzer
from agent.episode import EpisodeManager
from agent.lifecycle import Lifecycle, lease_timestamp, load_release
from config import Config
from data.sources import (FailingSource, HealthTrackingSource,
                          MarketSource, MockMarketSource, PolymarketSource)
from database.db import Database
from execution.engine import PaperTradingEngine, UnsafeExecution
from models.entities import MarketSnapshot
from risk.manager import RiskManager
from strategy.edge import evaluate_signal
from strategy.filter import extract_features, filter_markets
from strategy.profiles import PROFILES
from utils.logger import log_event

logger = logging.getLogger("agent.runner")


def build_source(cfg: Config) -> MarketSource:
    if cfg.DATA_SOURCE == "mock":
        return MockMarketSource()
    if cfg.DATA_SOURCE == "failing":
        return FailingSource()
    return PolymarketSource(cache_ttl=cfg.CACHE_TTL_SECONDS)


class Agent:
    def __init__(self, cfg: Config, db: Database = None):
        self.cfg = cfg
        self.db = db or Database(cfg.DATABASE_PATH)
        self.wallet = self.db.init_wallet(cfg.STARTING_BALANCE)
        # ONE-TIME $100 bankroll: never reset - capital is continuous.
        self.source = HealthTrackingSource(build_source(cfg))
        # active strategy profile: thresholds only, risk caps untouchable
        self.strategy = cfg.STRATEGY
        self.acfg = cfg.with_overrides(PROFILES.get(cfg.STRATEGY, {}))
        self.analyzer = build_analyzer(self.acfg)
        self.risk = RiskManager(self.acfg, self.db)
        self.engine = PaperTradingEngine(cfg, self.db, self.wallet)
        self.guard = DuplicateGuard(cooldown_seconds=int(cfg.SCAN_INTERVAL_MINUTES * 60))
        self.episodes = EpisodeManager(cfg, self.db)
        self.lifecycle = Lifecycle(cfg, self.db)
        # optional shadow strategy: evaluated + logged, NEVER executed
        self.shadow_cfg = (cfg.with_overrides(PROFILES[cfg.SHADOW_STRATEGY])
                           if cfg.SHADOW_STRATEGY else None)
        self.shadow_analyzer = (build_analyzer(self.shadow_cfg)
                                if self.shadow_cfg else None)
        # Cross-run dedup window: markets analyzed within this many seconds
        # (persisted in ai_decisions, so it survives restarts/Actions runs)
        self.dedup_cooldown = int(cfg.SCAN_INTERVAL_MINUTES * 60)

    # ------------------------------------------------------------- helpers
    def _current_equity(self) -> float:
        unrealized = sum(
            (r["mark_price"] - r["avg_entry_price"]) * r["quantity"]
            for r in self.db.open_positions())
        return self.wallet.balance + unrealized

    # ------------------------------------------------------------- cycle
    def run_cycle(self) -> dict:
        stats = {
            "markets_found": 0, "candidates": 0, "ai_analyzed": 0,
            "signals": 0, "paper_trades": 0, "rejected": 0,
            "resolved": 0, "open_positions": 0,
            "balance": round(self.wallet.balance, 2),
        }
        log_event(logger, "SCAN START", strategy=self.strategy)
        cfg = self.cfg
        lc = self.lifecycle

        # ---- lifecycle gate 0: DB integrity (safety) ----
        if not lc.db_integrity_ok():
            lc.safety_death("SAFETY_FAILURE", "db integrity check failed")
        release = load_release()

        # ---- lifecycle gate 1: maintenance lease (bot commits never
        #      renew it; only human strategy releases do) ----
        lc.check_maintenance(lease_timestamp())

        # ---- lifecycle gate 2: DEAD handling (no silent resurrection) ----
        if lc.state == "DEAD":
            lc.detect_recovery_input(release)
        if lc.is_dead():
            cur = lc.get()
            stats["skipped"] = cur["state"]
            stats["lifecycle"] = cur["state"]
            stats["lifecycle_reason"] = cur.get("reason")
            log_event(logger, "SCAN SKIPPED", **{
                k: v for k, v in stats.items() if isinstance(v, (str, int, float))})
            self.db.save_scan(stats)
            self.db.checkpoint()
            return stats

        trading_allowed = lc.can_trade()  # False only while LOCKED

        # 0. episode window: close the 24h evaluation window if due.
        #    Purely observational - the bankroll is NEVER reset.
        ep = self.episodes.ensure(self._current_equity())
        episode_id = ep["id"] if ep is not None else None
        stats["episode_id"] = episode_id

        # 1-2. fetch + validate
        try:
            markets: List[MarketSnapshot] = self.source.fetch_markets(
                limit=cfg.MAX_MARKETS_PER_SCAN)
        except Exception as exc:  # noqa: BLE001 - fail safe, keep old state
            log_event(logger, "SCAN_FAILED", error=str(exc)[:300])
            stats["error"] = str(exc)[:300]
            self._finish_cycle(stats, None)
            return stats
        stats["markets_found"] = len(markets)
        markets = [m for m in markets if m.is_valid()]
        market_map: Dict[str, MarketSnapshot] = {m.market_id: m for m in markets}

        # 9 (early). settle any resolved markets we hold positions in
        for row in self.db.open_positions():
            m = market_map.get(row["market_id"])
            if m is None or m.status != "resolved":
                # Resolved markets drop out of the active-market feed,
                # so poll them individually by id.
                try:
                    m = self.source.fetch_market_by_id(row["market_id"]) or m
                except Exception as exc:  # noqa: BLE001 - fail safe
                    log_event(logger, "resolution_fetch_failed",
                              market_id=row["market_id"], error=str(exc)[:120])
            if m is None or m.status != "resolved":
                continue
            winner = m.resolved_winner()
            if winner is None:
                log_event(logger, "resolution_outcome_unknown",
                          market_id=row["market_id"])
                continue
            winning_side = "YES" if winner == m.outcomes[0] else "NO"
            self.engine.settle_position(row["id"], row, winning_side)
            stats["resolved"] += 1

        # 3. filter
        candidates = filter_markets(markets, cfg)
        stats["candidates"] = len(candidates)

        # 4. AI analysis (capped calls, duplicate guard)
        analyzed = 0
        signals = []
        for m in candidates:
            if analyzed >= cfg.MAX_AI_CALLS_PER_CYCLE:
                break
            now = time.time()
            if not self.guard.should_analyze(m.market_id, now):
                continue
            # Cross-run dedup: skip markets analyzed within the cooldown
            # window. ai_decisions persists, so this also works across
            # separate GitHub Actions runs (saves AI cost).
            last = self.db.last_ai_decision_ts(m.market_id)
            if last is not None and now - last < self.dedup_cooldown:
                continue
            self.guard.mark(m.market_id, now)
            # Live list feeds do not include price history; enrich the
            # shortlisted candidate before analysis (fail-safe on errors).
            if len(m.price_history or []) < 10:
                try:
                    hist = self.source.fetch_price_history(m)
                    if hist:
                        m.price_history = hist
                except Exception as exc:  # noqa: BLE001
                    logger.debug("price history fetch failed for %s: %s",
                                 m.market_id, exc)
            sig = self.analyzer.analyze(m, extract_features(m))
            if sig is None:
                continue
            analyzed += 1
            sig.strategy = self.strategy
            self.db.save_ai_decision(sig, self.analyzer.name())
            ok, reasons = evaluate_signal(sig, m, self.acfg)
            if ok:
                signals.append((sig, m))
            else:
                stats["rejected"] += 1
                # rejection analytics: rejected opportunity + exact reason
                if episode_id is not None:
                    self.db.log_rejection(
                        episode_id, "signal", m.market_id, m.question,
                        sig.market_probability, sig.estimated_probability,
                        sig.edge, sig.confidence, reasons)
            # shadow strategy: evaluated + logged, NEVER executed
            if self.shadow_analyzer is not None:
                ssig = self.shadow_analyzer.analyze(m, extract_features(m))
                if ssig is not None:
                    ssig.strategy = f"shadow:{cfg.SHADOW_STRATEGY}"
                    self.db.save_ai_decision(
                        ssig, f"{self.shadow_analyzer.name()}|shadow")
        stats["ai_analyzed"] = analyzed
        stats["signals"] = len(signals)

        # 5-7. risk + simulated execution (skipped while LOCKED)
        if trading_allowed:
            for sig, m in signals:
                price = m.mid_price if sig.decision == "BUY_YES" \
                    else 1 - (m.mid_price or 0.5)
                decision = self.risk.check(self.wallet, sig, m.market_id, price)
                if decision.approved and decision.size_dollars > 0:
                    try:
                        order = self.engine.open_position(
                            m, sig, decision.size_dollars)
                    except UnsafeExecution as exc:
                        log_event(logger, "UNSAFE_EXECUTION",
                                  market_id=m.market_id, error=str(exc))
                        lc.safety_death("SAFETY_FAILURE", str(exc)[:300])
                        break
                    if order:
                        stats["paper_trades"] += 1
                else:
                    stats["rejected"] += 1
                    log_event(logger, "trade_vetoed", market_id=m.market_id,
                              reasons=decision.reasons)
                    # rejection analytics: risk-stage veto + exact reason
                    if episode_id is not None:
                        self.db.log_rejection(
                            episode_id, "risk", m.market_id, m.question,
                            sig.market_probability, sig.estimated_probability,
                            sig.edge, sig.confidence, decision.reasons)
        else:
            stats["skipped"] = lc.state  # LOCKED: no new trades

        # 8. mark positions
        self.engine.update_positions(market_map)
        self._finish_cycle(stats, markets)
        return stats

    def _finish_cycle(self, stats: dict,
                      markets: Optional[List[MarketSnapshot]]):
        """Common cycle tail: equity, bankruptcy check, provider health,
        lifecycle evaluation, persistence."""
        # 10-11. wallet + equity + logs
        unrealized = sum(
            (r["mark_price"] - r["avg_entry_price"]) * r["quantity"]
            for r in self.db.open_positions())
        stats["open_positions"] = len(self.db.open_positions())
        stats["balance"] = round(self.wallet.balance, 2)
        stats["total_pnl"] = round(self.wallet.total_pnl, 2)
        stats["equity"] = round(self.wallet.balance + unrealized, 2)
        self.db.save_equity(self.wallet.balance, stats["equity"])
        self.db.save_scan(stats)
        if markets:
            for m in markets[:40]:  # snapshot the busiest markets
                self.db.save_snapshot(m)

        # bankruptcy: the continuous bankroll hit zero -> DEAD
        self.lifecycle.check_bankruptcy(stats["equity"])

        # deterministic lifecycle evaluation from this cycle's outcome
        self.lifecycle.evaluate_cycle(stats)

        # provider health for this cycle
        try:
            ph = self.source.metrics()
            self.db.save_provider_health(**ph)
            self.db.set_sys("provider_last", ph)
        except Exception as exc:  # noqa: BLE001
            logger.debug("provider health not recorded: %s", exc)

        # Keep the state file small and safe to commit:
        self.db.prune()          # bound table growth
        self.db.checkpoint()     # flush WAL so the .db alone carries state

        log_event(logger, "SCAN SUMMARY", **stats)

    # -------------------------------------------------------------- loop
    def run_forever(self, max_cycles: int = 0):
        interval = self.cfg.SCAN_INTERVAL_MINUTES * 60
        log_event(logger, "LOOP_START", interval_minutes=self.cfg.SCAN_INTERVAL_MINUTES,
                  **{k: v for k, v in self.cfg.summary().items()
                     if k != "SCAN_INTERVAL_MINUTES"})
        n = 0
        while True:
            if self.cfg.KILL_SWITCH:
                log_event(logger, "KILL_SWITCH_ACTIVE", cycle=n)
                break
            try:
                self.run_cycle()
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log_event(logger, "CYCLE_ERROR", error=str(exc)[:300])
                logger.exception("cycle failed")
            n += 1
            if max_cycles and n >= max_cycles:
                break
            time.sleep(interval)
