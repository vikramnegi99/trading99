"""DEAD-HAND / survival lifecycle for the paper-trading agent.

A persistent, deterministic state machine. The current state lives in
SQLite (table `lifecycle`) so it survives GitHub Actions restarts, and a
DEAD agent can never be silently resurrected by the next workflow run.

States
------
RUNNING            normal operation
DEGRADED            data issues detected (API errors / stale feeds);
                   trading still allowed, cycles still run
REVIEW_REQUIRED    prolonged strategy inactivity OR maintenance lease
                   overdue; trading continues but a human review is due
LOCKED              12 consecutive failed/stale data cycles; NO trading
RECOVERY_REQUIRED  agent was DEAD, a new human strategy release was
                   detected, but the explicit acknowledgement is missing
DEAD                terminal for this run. No trades, no strategy
                   execution, history preserved.

Transition rules (deterministic, all logged in lifecycle_events)
-----------------------------------------------------------------
BANKRUPT             equity/balance <= 0                    -> DEAD
SAFETY_FAILURE       REAL_TRADING requested, unsafe execution
                     path, impossible portfolio values, corrupted DB
                                                            -> DEAD
DATA_FAILURE         12 consecutive failed/stale cycles      -> LOCKED
                     (3 consecutive healthy cycles unlock it)
STRATEGY_INACTIVE    prolonged "analyzed but rejected everything"
                                                            -> REVIEW_REQUIRED
MAINTENANCE_DUE      7 days without a human strategy release  -> REVIEW_REQUIRED
MAINTENANCE_EXPIRED  48h in REVIEW_REQUIRED (maintenance)    -> DEAD
LEASE_RENEWED        human strategy release while overdue    -> RUNNING
DATA_RECOVERED       healthy cycles after DEGRADED/LOCKED    -> RUNNING

DEAD recovery (no automatic resurrection):
  1. a new human strategy release is detected (strategy/RELEASE.json
     version differs from the one recorded at death) -> RECOVERY_REQUIRED
  2. explicit acknowledgement: `python main.py --recover --ack <REASON>`
  3. the recovery commit itself must pass the test suite (CI gate)

The maintenance lease is ONLY renewed by commits that touch strategy /
AI / risk code (or strategy/RELEASE.json) by a human author. Bot state
commits never renew it.
"""
import json
import logging
import subprocess
import time
from typing import Optional

from config import Config
from database.db import Database
from utils.logger import log_event

logger = logging.getLogger("agent.lifecycle")

STATES = ("RUNNING", "DEGRADED", "LOCKED", "REVIEW_REQUIRED",
          "DEAD", "RECOVERY_REQUIRED")

STRATEGY_PATHS = ("strategy/", "ai/", "risk/", "config.py",
                  "strategy/RELEASE.json")
BOT_AUTHORS = ("paper-trading-bot", "github-actions[bot]", "actions-user")


def load_release(path: str = "strategy/RELEASE.json") -> Optional[dict]:
    """The human strategy release marker (version, author, timestamp)."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("version"):
            return d
    except OSError:
        pass
    return None


def human_strategy_commit_ts(repo_dir: str = ".") -> Optional[float]:
    """Timestamp of the last HUMAN commit touching strategy/AI/risk code.

    Bot state commits (paper-trading-bot) only touch data/, dashboard.html
    and index.html, so they never renew the lease. Returns None when git
    history is unavailable (e.g. plain tarball) - the caller then falls
    back to strategy/RELEASE.json's committed_at.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-n", "10", "--format=%ct|%an",
             "--", *STRATEGY_PATHS],
            cwd=repo_dir, capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            parts = line.strip().split("|", 1)
            if len(parts) != 2:
                continue
            ts, author = float(parts[0]), parts[1].strip().lower()
            if not any(b in author for b in BOT_AUTHORS):
                return ts
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def lease_timestamp(repo_dir: str = ".") -> Optional[float]:
    """The current maintenance lease: the newest human release signal."""
    ts = human_strategy_commit_ts(repo_dir)
    rel = load_release()
    if rel and rel.get("committed_at"):
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(
                rel["committed_at"].replace("Z", "+00:00"))
            file_ts = t.timestamp() if t.tzinfo else \
                t.replace(tzinfo=timezone.utc).timestamp()
            ts = file_ts if ts is None else max(ts, file_ts)
        except (ValueError, TypeError):
            pass
    return ts


class Lifecycle:
    """Owns the persistent state machine. Paper-only survival system."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        with self.db._lock:
            self.db.conn.execute(
                "INSERT OR IGNORE INTO lifecycle(id, state, reason, "
                "since_ts, details) VALUES(1, 'RUNNING', NULL, ?, NULL)",
                (time.time(),))
            self.db.conn.commit()

    # ------------------------------------------------------------- access
    def get(self) -> dict:
        row = self.db.conn.execute(
            "SELECT * FROM lifecycle WHERE id=1").fetchone()
        return dict(row) if row else {"state": "RUNNING", "reason": None,
                                      "since_ts": time.time()}

    @property
    def state(self) -> str:
        return self.get()["state"]

    @property
    def reason(self) -> Optional[str]:
        return self.get()["reason"]

    def can_trade(self) -> bool:
        """Trading is allowed in RUNNING / DEGRADED / REVIEW_REQUIRED only."""
        return self.state in ("RUNNING", "DEGRADED", "REVIEW_REQUIRED")

    def is_dead(self) -> bool:
        return self.state in ("DEAD", "RECOVERY_REQUIRED")

    # ------------------------------------------------------- transitions
    def _set(self, state: str, reason: Optional[str],
             details: Optional[str] = None) -> bool:
        """Apply a transition (only if the state actually changes)."""
        cur = self.get()
        if cur["state"] == state and cur.get("reason") == reason:
            return False
        now = time.time()
        with self.db._lock:
            if state == "DEAD":
                # freeze the death reason + release version for recovery
                self.db.conn.execute(
                    "UPDATE lifecycle SET state=?, reason=?, since_ts=?, "
                    "details=?, death_reason=?, death_release_version=? "
                    "WHERE id=1",
                    (state, reason, now, details, reason,
                     _release_version()))
            else:
                self.db.conn.execute(
                    "UPDATE lifecycle SET state=?, reason=?, since_ts=?, "
                    "details=? WHERE id=1", (state, reason, now, details))
            self.db.conn.execute(
                "INSERT INTO lifecycle_events(ts, from_state, to_state, "
                "reason, details) VALUES(?,?,?,?,?)",
                (now, cur["state"], state, reason, details))
            self.db.conn.commit()
        log_event(logger, "LIFECYCLE", from_state=cur["state"],
                  to_state=state, reason=reason)
        return True

    # ------------------------------------------------------------ rules
    def check_bankruptcy(self, equity: float) -> bool:
        if equity <= 0 and self.state != "DEAD":
            return self._set("DEAD", "BANKRUPT", f"equity={equity:.2f}")
        return False

    def safety_death(self, reason: str = "SAFETY_FAILURE",
                     details: Optional[str] = None) -> bool:
        if self.state == "DEAD":
            return False
        return self._set("DEAD", reason, details)

    def db_integrity_ok(self) -> bool:
        try:
            row = self.db.conn.execute("PRAGMA quick_check").fetchone()
            return bool(row) and str(row[0]).lower() == "ok"
        except Exception:  # noqa: BLE001
            return False

    def evaluate_cycle(self, stats: dict) -> list:
        """Deterministic post-cycle transitions. Returns applied changes."""
        applied = []
        if self.is_dead():
            return applied

        failed = bool(stats.get("error")) or stats.get("markets_found", 0) < 50
        if failed:
            self._bump("consecutive_failed")
            self._reset("consecutive_ok")
            n = self._counter("consecutive_failed")
            if self.state in ("RUNNING", "DEGRADED"):
                if n == 1:
                    self._set("DEGRADED", "DATA_ISSUE")
                    applied.append("DEGRADED")
                if n >= self.cfg.DATA_FAILURE_LOCK_CYCLES:
                    self._set("LOCKED", "DATA_FAILURE",
                              f"{n} consecutive failed/stale cycles")
                    applied.append("LOCKED")
        else:
            self._bump("consecutive_ok")
            self._reset("consecutive_failed")
            n = self._counter("consecutive_ok")
            if self.state == "DEGRADED":
                self._set("RUNNING", "DATA_RECOVERED")
                applied.append("RUNNING")
            elif self.state == "LOCKED" and n >= 3:
                self._set("RUNNING", "DATA_RECOVERED",
                          f"{n} consecutive healthy cycles")
                applied.append("RUNNING")

        # strategy inactivity: plenty of data, plenty analyzed, zero
        # valid signals -> the strategy rejected everything
        rejected_all = (
            stats.get("markets_found", 0) >= 100
            and stats.get("candidates", 0) >= 10
            and stats.get("ai_analyzed", 0) >= 5
            and stats.get("signals", 0) == 0
            and stats.get("rejected", 0) > 0)
        if rejected_all:
            self._bump("consecutive_reject_all")
            n = self._counter("consecutive_reject_all")
            if n >= self.cfg.STRATEGY_INACTIVE_CYCLES and \
                    self.state in ("RUNNING", "DEGRADED"):
                self._set("REVIEW_REQUIRED", "STRATEGY_INACTIVE",
                          f"{n} cycles with analyzed>="
                          f"{stats.get('ai_analyzed', 0)} all rejected")
                applied.append("REVIEW_REQUIRED")
        elif stats.get("signals", 0) > 0:
            self._reset("consecutive_reject_all")
            if self.state == "REVIEW_REQUIRED" and \
                    self.reason == "STRATEGY_INACTIVE":
                self._set("RUNNING", "STRATEGY_ACTIVE_AGAIN")
                applied.append("RUNNING")
        return applied

    def check_maintenance(self, lease_ts: Optional[float]) -> list:
        """Maintenance lease: bot commits never renew it. 7 days overdue
        -> REVIEW_REQUIRED; 48h after that -> DEAD."""
        applied = []
        if self.is_dead():
            return applied
        now = time.time()
        cur = self.get()
        if cur["state"] == "REVIEW_REQUIRED" and \
                cur.get("reason") == "MAINTENANCE_DUE" and \
                lease_ts is not None and \
                lease_ts > (cur.get("since_ts") or 0):
            # a fresh human release arrived during the review window
            self._set("RUNNING", "LEASE_RENEWED")
            applied.append("RUNNING")
            return applied
        if lease_ts is None:
            return applied
        overdue_s = now - lease_ts
        if overdue_s > self.cfg.MAINTENANCE_LEASE_DAYS * 86400 and \
                cur["state"] in ("RUNNING", "DEGRADED"):
            self._set("REVIEW_REQUIRED", "MAINTENANCE_DUE",
                      f"last human strategy release "
                      f"{overdue_s / 86400:.1f} days ago")
            applied.append("REVIEW_REQUIRED")
        elif cur["state"] == "REVIEW_REQUIRED" and \
                cur.get("reason") == "MAINTENANCE_DUE" and \
                now - (cur.get("since_ts") or now) > \
                self.cfg.REVIEW_DEADLINE_HOURS * 3600:
            self._set("DEAD", "MAINTENANCE_EXPIRED",
                      "no human strategy release within the deadline")
            applied.append("DEAD")
        return applied

    # ---------------------------------------------------------- recovery
    def detect_recovery_input(self, release: Optional[dict]) -> bool:
        """DEAD + a NEW human strategy release (version differs from the
        one recorded at death) moves to RECOVERY_REQUIRED. Still no
        trading until the explicit acknowledgement."""
        if self.state != "DEAD" or not release or not release.get("version"):
            return False
        death_version = self.get().get("death_release_version")
        if release["version"] != death_version:
            return self._set("RECOVERY_REQUIRED", "AWAITING_ACK",
                             f"new release {release['version']} detected; "
                             f"acknowledgement required "
                             f"(python main.py --recover --ack <REASON>)")
        return False

    def recover(self, ack: str, release: Optional[dict]) -> bool:
        """Explicit human recovery. Requires RECOVERY_REQUIRED state, a
        valid release, and the acknowledgement naming the death reason.
        History is never deleted."""
        if self.state != "RECOVERY_REQUIRED":
            return False
        if not release or not release.get("version"):
            return False
        death_reason = self.get().get("death_reason") or ""
        if (ack or "").strip().upper() != death_reason.strip().upper():
            return False
        return self._set("RUNNING", "RECOVERED",
                         f"ack={ack}, release={release['version']}")

    # ---------------------------------------------------------- counters
    def _counter(self, key: str) -> int:
        row = self.db.conn.execute(
            "SELECT value FROM system_health WHERE key=?",
            (f"lifecycle_{key}",)).fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _bump(self, key: str):
        with self.db._lock:
            self.db.conn.execute(
                "INSERT INTO system_health(key, value) VALUES(?, '1') "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
                (f"lifecycle_{key}",))
            self.db.conn.commit()

    def _reset(self, key: str, value: int = 0):
        with self.db._lock:
            self.db.conn.execute(
                "INSERT INTO system_health(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"lifecycle_{key}", str(value)))
            self.db.conn.commit()


def _release_version() -> Optional[str]:
    rel = load_release()
    return rel["version"] if rel else None
