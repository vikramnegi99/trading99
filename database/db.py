"""SQLite persistence layer. Everything the agent learns survives restarts."""
import json
import os
import sqlite3
import threading
import time
from typing import List, Optional

from models.entities import (MarketSnapshot, PaperOrder, PaperPosition,
                             PaperWallet, Signal, Trade)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_balance REAL NOT NULL,
    balance REAL NOT NULL,
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    avg_entry_price REAL NOT NULL DEFAULT 0,
    mark_price REAL NOT NULL DEFAULT 0,
    cost_basis REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    opened_ts REAL NOT NULL,
    closed_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL NOT NULL,
    requested_price REAL NOT NULL,
    executed_price REAL NOT NULL,
    fees REAL NOT NULL,
    slippage REAL NOT NULL,
    notional REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL,
    pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_market ON snapshots(market_id);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_ts ON ai_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_ai_market ON ai_decisions(market_id);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    balance REAL NOT NULL,
    equity REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_curve(ts);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts REAL NOT NULL,
    ended_ts REAL,
    starting_balance REAL NOT NULL,
    ending_balance REAL,
    status TEXT NOT NULL DEFAULT 'active',
    report TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);

CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    episode_id INTEGER,
    stage TEXT NOT NULL,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    market_probability REAL,
    estimated_probability REAL,
    edge REAL,
    confidence REAL,
    reasons TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_ep ON rejections(episode_id, ts);

CREATE TABLE IF NOT EXISTS lifecycle (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'RUNNING',
    reason TEXT,
    since_ts REAL NOT NULL,
    details TEXT,
    death_reason TEXT,
    death_release_version TEXT
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_ts ON lifecycle_events(ts);

CREATE TABLE IF NOT EXISTS provider_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    markets_returned INTEGER NOT NULL DEFAULT 0,
    valid_markets INTEGER NOT NULL DEFAULT 0,
    history_attempts INTEGER NOT NULL DEFAULT 0,
    history_success INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS score_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    scope TEXT NOT NULL,
    ref TEXT,
    score REAL NOT NULL,
    breakdown TEXT
);

CREATE TABLE IF NOT EXISTS canary_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    passed INTEGER NOT NULL,
    details TEXT
);

CREATE TABLE IF NOT EXISTS system_health (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str = "data/trading.db"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.commit()
            self.conn.close()

    # ------------------------------------------------------------- wallet
    def init_wallet(self, starting_balance: float) -> PaperWallet:
        row = self.conn.execute("SELECT * FROM wallet WHERE id=1").fetchone()
        if row:
            return PaperWallet(row["starting_balance"], row["balance"])
        w = PaperWallet(starting_balance, starting_balance)
        self.save_wallet(w)
        return w

    def save_wallet(self, w: PaperWallet):
        with self._lock:
            self.conn.execute(
                "INSERT INTO wallet(id, starting_balance, balance, updated_ts) "
                "VALUES(1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "starting_balance=excluded.starting_balance, "
                "balance=excluded.balance, updated_ts=excluded.updated_ts",
                (w.starting_balance, w.balance, time.time()))
            self.conn.commit()

    # ---------------------------------------------------------- positions
    def save_position(self, p: PaperPosition) -> int:
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO positions(market_id, question, side, quantity,
                     avg_entry_price, mark_price, cost_basis, realized_pnl,
                     status, opened_ts, closed_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (p.market_id, p.question, p.side, p.quantity, p.avg_entry_price,
                 p.mark_price, p.cost_basis, p.realized_pnl, p.status,
                 p.opened_ts, p.closed_ts))
            self.conn.commit()
            return cur.lastrowid

    def update_position(self, pos_id: int, p: PaperPosition):
        with self._lock:
            self.conn.execute(
                """UPDATE positions SET quantity=?, avg_entry_price=?, mark_price=?,
                     cost_basis=?, realized_pnl=?, status=?, closed_ts=? WHERE id=?""",
                (p.quantity, p.avg_entry_price, p.mark_price, p.cost_basis,
                 p.realized_pnl, p.status, p.closed_ts, pos_id))
            self.conn.commit()

    def open_positions(self) -> List[tuple]:
        return self.conn.execute(
            "SELECT id, * FROM positions WHERE status='open'").fetchall()

    def find_open_position(self, market_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, * FROM positions WHERE market_id=? AND status='open'",
            (market_id,)).fetchone()

    # -------------------------------------------------- orders/trades/logs
    def save_order(self, o: PaperOrder):
        with self._lock:
            self.conn.execute(
                "INSERT INTO orders(ts, market_id, side, action, quantity, "
                "requested_price, executed_price, fees, slippage, notional, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (o.ts, o.market_id, o.side, o.action, o.quantity,
                 o.requested_price, o.executed_price, o.fees, o.slippage,
                 o.notional, o.status))
            self.conn.commit()

    def save_trade(self, t: Trade):
        with self._lock:
            self.conn.execute(
                "INSERT INTO trades(ts, market_id, side, action, quantity, "
                "price, fees, pnl) VALUES(?,?,?,?,?,?,?,?)",
                (t.ts, t.market_id, t.side, t.action, t.quantity, t.price,
                 t.fees, t.pnl))
            self.conn.commit()

    def save_snapshot(self, m: MarketSnapshot):
        with self._lock:
            self.conn.execute(
                "INSERT INTO snapshots(ts, market_id, question, payload) "
                "VALUES(?,?,?,?)",
                (m.snapshot_ts, m.market_id, m.question,
                 json.dumps(m.to_dict())))
            self.conn.commit()

    def save_ai_decision(self, s: Signal, provider: str):
        with self._lock:
            self.conn.execute(
                "INSERT INTO ai_decisions(ts, market_id, provider, payload) "
                "VALUES(?,?,?,?)",
                (s.created_ts, s.market_id, provider,
                 json.dumps(s.__dict__ if hasattr(s, "__dict__") else str(s))))
            self.conn.commit()

    def save_scan(self, payload: dict):
        with self._lock:
            self.conn.execute("INSERT INTO scans(ts, payload) VALUES(?,?)",
                              (time.time(), json.dumps(payload)))
            self.conn.commit()

    def save_equity(self, balance: float, equity: float):
        with self._lock:
            self.conn.execute(
                "INSERT INTO equity_curve(ts, balance, equity) VALUES(?,?,?)",
                (time.time(), balance, equity))
            self.conn.commit()

    # ---------------------------------------------------------- episodes
    def active_episode(self) -> Optional[sqlite3.Row]:
        """The currently running challenge episode, if any."""
        return self.conn.execute(
            "SELECT * FROM episodes WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def start_episode(self, starting_balance: float,
                      started_ts: float = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO episodes(started_ts, starting_balance, status) "
                "VALUES(?,?,'active')",
                (started_ts if started_ts is not None else time.time(),
                 starting_balance))
            self.conn.commit()
            return cur.lastrowid

    def complete_episode(self, ep_id: int, ended_ts: float,
                         ending_balance: float, report: dict):
        with self._lock:
            self.conn.execute(
                "UPDATE episodes SET ended_ts=?, ending_balance=?, "
                "status='completed', report=? WHERE id=?",
                (ended_ts, ending_balance, json.dumps(report), ep_id))
            self.conn.commit()

    def log_rejection(self, episode_id, stage: str,
                      market_id: str, question: str,
                      market_probability, estimated_probability,
                      edge, confidence, reasons) -> None:
        """Record a rejected opportunity with its EXACT rejection reason."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO rejections(ts, episode_id, stage, market_id, "
                "question, market_probability, estimated_probability, edge, "
                "confidence, reasons) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (time.time(), episode_id, stage, market_id,
                 (question or "")[:500], market_probability,
                 estimated_probability, edge, confidence,
                 "; ".join(str(r) for r in reasons)))
            self.conn.commit()

    def top_rejections(self, episode_id: int, limit: int = 20):
        """Best rejected opportunities of an episode (highest edge first)."""
        return self.conn.execute(
            "SELECT * FROM rejections WHERE episode_id=? "
            "ORDER BY COALESCE(edge, -1) DESC, ts DESC LIMIT ?",
            (episode_id, int(limit))).fetchall()

    # -------------------------------------------------- health / scores
    def save_provider_health(self, provider: str, requests: int,
                              errors: int, latency_ms: float,
                              markets_returned: int, valid_markets: int,
                              history_attempts: int, history_success: int):
        with self._lock:
            self.conn.execute(
                "INSERT INTO provider_health(ts, provider, requests, "
                "errors, latency_ms, markets_returned, valid_markets, "
                "history_attempts, history_success) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), provider, requests, errors, latency_ms,
                 markets_returned, valid_markets, history_attempts,
                 history_success))
            self.conn.commit()

    def latest_provider_health(self):
        return self.conn.execute(
            "SELECT * FROM provider_health ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def save_score(self, scope: str, ref, score: float, breakdown: dict):
        with self._lock:
            self.conn.execute(
                "INSERT INTO score_history(ts, scope, ref, score, "
                "breakdown) VALUES(?,?,?,?,?)",
                (time.time(), scope, str(ref) if ref is not None else None,
                 score, json.dumps(breakdown)))
            self.conn.commit()

    def save_canary(self, passed: bool, details: dict):
        with self._lock:
            self.conn.execute(
                "INSERT INTO canary_results(ts, passed, details) "
                "VALUES(?,?,?)",
                (time.time(), 1 if passed else 0, json.dumps(details)))
            self.conn.commit()

    def latest_canary(self):
        return self.conn.execute(
            "SELECT * FROM canary_results ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def set_sys(self, key: str, value):
        with self._lock:
            self.conn.execute(
                "INSERT INTO system_health(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))
            self.conn.commit()

    def get_sys(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM system_health WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------ queries
    def query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def last_ai_decision_ts(self, market_id: str) -> Optional[float]:
        """Most recent analysis timestamp for a market (cross-run dedup)."""
        row = self.conn.execute(
            "SELECT MAX(ts) AS t FROM ai_decisions WHERE market_id=?",
            (market_id,)).fetchone()
        return row["t"] if row else None

    def checkpoint(self):
        """Flush the WAL into the main .db file.

        Called at the end of every cycle so that committing/copying the
        .db file alone (as the GitHub Actions workflow does) never loses
        recent transactions.

        """
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.commit()

    def prune(self, snapshots_per_market: int = 5, scans: int = 2000,
              equity: int = 5000, ai_decisions: int = 5000):
        """Bound table growth so the committed state file stays small.

        Keeps the newest N snapshots per market and the newest N rows of
        the scan/equity/ai_decision logs. Positions, trades and orders are
        NEVER pruned - they are the trading history.

        """
        with self._lock:
            self.conn.execute(
                """DELETE FROM snapshots WHERE id IN (
                     SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (
                         PARTITION BY market_id ORDER BY id DESC) AS rn
                       FROM snapshots)
                     WHERE rn > ?)""", (snapshots_per_market,))
            self.conn.execute(
                "DELETE FROM scans WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM scans)", (scans,))
            self.conn.execute(
                "DELETE FROM equity_curve WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM equity_curve)", (equity,))
            self.conn.execute(
                "DELETE FROM ai_decisions WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM ai_decisions)", (ai_decisions,))
            self.conn.execute(
                "DELETE FROM rejections WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM rejections)",
                (5000,))
            self.conn.execute(
                "DELETE FROM provider_health WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM provider_health)",
                (2000,))
            self.conn.execute(
                "DELETE FROM lifecycle_events WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM lifecycle_events)",
                (5000,))
            self.conn.execute(
                "DELETE FROM score_history WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM score_history)",
                (5000,))
            self.conn.execute(
                "DELETE FROM canary_results WHERE id <= "
                "(SELECT COALESCE(MAX(id), 0) - ? FROM canary_results)",
                (500,))
            self.conn.commit()

    def recent(self, table: str, limit: int = 20,
               where: str = "", params: tuple = ()) -> List[sqlite3.Row]:
        sql = f"SELECT * FROM {table} {where} ORDER BY id DESC LIMIT {int(limit)}"
        return self.query(sql, params)
