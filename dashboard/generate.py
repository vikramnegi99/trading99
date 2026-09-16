"""Performance statistics + mobile-friendly self-contained HTML dashboard.

The dashboard is regenerated after every scan and committed to the repo, so
you can view it from the GitHub mobile app. It is clearly labelled
PAPER TRADING.
"""
import html
import json
import logging
from typing import Dict, List

from config import Config
from database.db import Database
from utils.logger import utcnow_iso

logger = logging.getLogger("dashboard")


def compute_stats(db: Database, wallet_balance: float,
                  starting_balance: float) -> Dict:
    trades = [dict(r) for r in db.query(
        "SELECT * FROM trades WHERE pnl IS NOT NULL ORDER BY id")]
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    eq = [r["equity"] for r in db.query("SELECT equity FROM equity_curve ORDER BY id")]

    max_dd = 0.0
    peak = 0.0
    for e in eq:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    open_rows = [dict(r) for r in db.open_positions()]
    unrealized = sum((r["mark_price"] - r["avg_entry_price"]) * r["quantity"]
                     for r in open_rows)

    stats = {
        "starting_balance": starting_balance,
        "current_balance": round(wallet_balance, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(wallet_balance - starting_balance, 2),
        "return_pct": round((wallet_balance - starting_balance)
                            / starting_balance * 100, 2) if starting_balance else 0,
        "total_trades": len(pnls),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "avg_trade_pnl": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses and sum(losses) != 0 else None),
        "open_positions": open_rows,
        "closed_positions": [dict(r) for r in db.recent(
            "positions", 20, "WHERE status='closed'")],
        "recent_trades": [dict(r) for r in db.recent("trades", 20)],
        "recent_signals": [json.loads(r["payload"]) for r in db.recent(
            "ai_decisions", 20)],
        "recent_scans": [json.loads(r["payload"]) for r in db.recent("scans", 10)],
    }
    return stats


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trading Dashboard</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;
--muted:#8b949e;--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);
color:var(--text);padding:14px;max-width:760px;margin:0 auto}
.banner{background:var(--amber);color:#000;text-align:center;font-weight:700;
padding:8px;border-radius:8px;margin-bottom:12px;font-size:14px}
h1{font-size:1.3rem;margin-bottom:4px}
.sub{color:var(--muted);font-size:.8rem;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:12px}
.card .k{color:var(--muted);font-size:.72rem;text-transform:uppercase;
letter-spacing:.04em}
.card .v{font-size:1.35rem;font-weight:700;margin-top:4px}
.pos{color:var(--green)}.neg{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:.78rem;margin:8px 0 18px}
th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--border);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
th{color:var(--muted);font-weight:600}
.tag{display:inline-block;background:var(--border);border-radius:10px;
padding:1px 8px;font-size:.7rem}
.hold{color:var(--muted)}
footer{color:var(--muted);font-size:.72rem;text-align:center;margin-top:10px}
</style></head><body>
<div class="banner">PAPER TRADING ONLY - SIMULATED MONEY - NO REAL ORDERS</div>
<h1>AI Paper Trading Agent</h1>
<div class="sub">Mode: PAPER LIVE (real-money execution is disabled in code) -
Updated {updated}</div>
<div class="grid">
<div class="card"><div class="k">Starting balance</div>
<div class="v">${starting}</div></div>
<div class="card"><div class="k">Current balance</div>
<div class="v">${balance}</div></div>
<div class="card"><div class="k">Total P&L</div>
<div class="v {pnl_cls}">{pnl_sign}${pnl}</div></div>
<div class="card"><div class="k">Return</div>
<div class="v {pnl_cls}">{pnl_sign}{ret}%</div></div>
<div class="card"><div class="k">Trades</div><div class="v">{trades}</div></div>
<div class="card"><div class="k">Win rate</div>
<div class="v">{wr}%</div></div>
<div class="card"><div class="k">Avg trade P&L</div>
<div class="v">${avg}</div></div>
<div class="card"><div class="k">Largest win</div>
<div class="v pos">${lw}</div></div>
<div class="card"><div class="k">Largest loss</div>
<div class="v neg">${ll}</div></div>
<div class="card"><div class="k">Max drawdown</div>
<div class="v">{mdd}%</div></div>
<div class="card"><div class="k">Unrealized</div>
<div class="v {unr_cls}">{unr_sign}${unr}</div></div>
<div class="card"><div class="k">Profit factor</div>
<div class="v">{pf}</div></div>
</div>

<h2 style="font-size:1rem;margin-bottom:6px">Open positions ({n_open})</h2>
<table><tr><th>Market</th><th>Side</th><th>Qty</th><th>Entry</th>
<th>Mark</th><th>P&L</th></tr>{open_rows}</table>

<h2 style="font-size:1rem;margin-bottom:6px">Recent AI signals</h2>
<table><tr><th>Market</th><th>Mkt p</th><th>Est p</th><th>Edge</th>
<th>Conf</th><th>Decision</th></tr>{signal_rows}</table>

<h2 style="font-size:1rem;margin-bottom:6px">Recent trades</h2>
<table><tr><th>Time</th><th>Market</th><th>Action</th><th>Qty</th>
<th>Price</th><th>P&L</th></tr>{trade_rows}</table>

<h2 style="font-size:1rem;margin-bottom:6px">Last scans</h2>
<table><tr><th>Markets</th><th>Candidates</th><th>Analyzed</th>
<th>Signals</th><th>Trades</th><th>Balance</th></tr>{scan_rows}</table>

<footer>Simulation - slippage/fees applied. Not investment advice.
No claim of profitability without sufficient paper/backtest evidence.</footer>
</body></html>"""


def _e(s) -> str:
    return html.escape(str(s))


def _cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def _sign(v: float) -> str:
    return "+" if v >= 0 else ""


def generate_dashboard(cfg: Config, db: Database, out_path: str = None) -> str:
    wallet = db.init_wallet(cfg.STARTING_BALANCE)
    s = compute_stats(db, wallet.balance, wallet.starting_balance)

    open_rows = "".join(
        f"<tr><td title='{_e(r['question'])}'>{_e(r['question'][:60])}</td>"
        f"<td>{_e(r['side'])}</td><td>{r['quantity']:.2f}</td>"
        f"<td>{r['avg_entry_price']:.3f}</td><td>{r['mark_price']:.3f}</td>"
        f"<td class='{_cls((r['mark_price']-r['avg_entry_price'])*r['quantity'])}'>"
        f"{_sign((r['mark_price']-r['avg_entry_price'])*r['quantity'])}"
        f"${(r['mark_price']-r['avg_entry_price'])*r['quantity']:.2f}</td></tr>"
        for r in s["open_positions"]) or "<tr><td colspan=6>None</td></tr>"

    signal_rows = "".join(
        f"<tr><td>{_e(d.get('market_id',''))}</td>"
        f"<td>{d.get('market_probability','-')}</td>"
        f"<td>{d.get('estimated_probability','-')}</td>"
        f"<td>{d.get('edge','-')}</td><td>{d.get('confidence','-')}</td>"
        f"<td class='{'hold' if d.get('decision')=='HOLD' else ''}'>"
        f"{_e(d.get('decision',''))}</td></tr>"
        for d in s["recent_signals"]) or "<tr><td colspan=6>None</td></tr>"

    import datetime
    trade_rows = "".join(
        f"<tr><td>{datetime.datetime.fromtimestamp(t['ts']).strftime('%d %b %H:%M')}</td>"
        f"<td>{_e(t['market_id'])}</td><td>{_e(t['action'])}</td>"
        f"<td>{t['quantity']:.2f}</td><td>{t['price']:.3f}</td>"
        f"<td class='{_cls(t['pnl'] or 0)}'>{_sign(t['pnl'] or 0)}"
        f"${(t['pnl'] or 0):.2f}</td></tr>"
        for t in s["recent_trades"]) or "<tr><td colspan=6>None</td></tr>"

    scan_rows = "".join(
        f"<tr><td>{sc.get('markets_found',0)}</td>"
        f"<td>{sc.get('candidates',0)}</td><td>{sc.get('ai_analyzed',0)}</td>"
        f"<td>{sc.get('signals',0)}</td><td>{sc.get('paper_trades',0)}</td>"
        f"<td>${sc.get('balance',0):.2f}</td></tr>"
        for sc in s["recent_scans"]) or "<tr><td colspan=6>None</td></tr>"

    # NOTE: str.replace, not str.format - the template contains CSS braces.
    values = {
        "updated": utcnow_iso(),
        "starting": f"{s['starting_balance']:.2f}",
        "balance": f"{s['current_balance']:.2f}",
        "pnl": f"{s['total_pnl']:.2f}",
        "pnl_sign": _sign(s["total_pnl"]),
        "pnl_cls": _cls(s["total_pnl"]),
        "ret": f"{s['return_pct']:.2f}",
        "trades": s["total_trades"],
        "wr": f"{s['win_rate']:.1f}",
        "avg": f"{s['avg_trade_pnl']:.3f}",
        "lw": f"{s['largest_win']:.2f}",
        "ll": f"{s['largest_loss']:.2f}",
        "mdd": f"{s['max_drawdown_pct']:.2f}",
        "unr": f"{s['unrealized_pnl']:.2f}",
        "unr_sign": _sign(s["unrealized_pnl"]),
        "unr_cls": _cls(s["unrealized_pnl"]),
        "pf": f"{s['profit_factor']:.2f}" if s["profit_factor"] else "-",
        "n_open": len(s["open_positions"]),
        "open_rows": open_rows,
        "signal_rows": signal_rows,
        "trade_rows": trade_rows,
        "scan_rows": scan_rows,
    }
    page = TEMPLATE
    for key, val in values.items():
        page = page.replace("{" + key + "}", str(val))

    out = out_path or cfg.DASHBOARD_PATH
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    logger.info("dashboard written to %s", out)
    return out
