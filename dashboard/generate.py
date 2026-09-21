"""Professional paper-trading terminal dashboard.

Self-contained dark HTML (mobile-first, no external CDN, inline SVG
only, no fake data - every number comes from the SQLite state). Shows
the full V2 survival picture: lifecycle state, dead-hand panel, canary,
equity curve, 24h challenge window, signal funnel, risk usage, system
health, provider health, rejection analytics and strategy performance.

Regenerated after every scan cycle and committed to the repo, so it is
viewable from the GitHub mobile app and GitHub Pages.
"""
import html
import json
import logging
import time
from typing import Dict, List

from config import Config
from database.db import Database
from utils.logger import utcnow_iso

logger = logging.getLogger("dashboard")

# ----------------------------------------------------------------- helpers
def _e(s) -> str:
    return html.escape(str(s))


def _cls(v: float) -> str:
    return "pos" if v >= 0 else "neg"


def _sign(v: float) -> str:
    return "+" if v >= 0 else "-"


def _money(v: float) -> str:
    return f"{'+' if v >= 0 else ''}${v:.2f}"


def _fmt_ts(ts) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return "-"


def _age(ts) -> str:
    try:
        d = time.time() - float(ts)
    except (TypeError, ValueError):
        return "-"
    if d < 90:
        return f"{int(d)}s ago"
    if d < 5400:
        return f"{int(d // 60)}m ago"
    if d < 172800:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


# ----------------------------------------------------------------- stats
def compute_stats(db: Database, wallet_balance: float,
                  starting_balance: float) -> Dict:
    trades = [dict(r) for r in db.query(
        "SELECT * FROM trades WHERE pnl IS NOT NULL ORDER BY id")]
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    eq = [r["equity"] for r in db.query(
        "SELECT equity FROM equity_curve ORDER BY id")]

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
        "equity": round(wallet_balance + unrealized, 2),
        "total_pnl": round(wallet_balance + unrealized - starting_balance, 2),
        "return_pct": round((wallet_balance + unrealized - starting_balance)
                            / starting_balance * 100, 2) if starting_balance else 0,
        "peak_equity": round(max(eq) if eq else starting_balance, 2),
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
        "recent_scans": [json.loads(r["payload"]) for r in db.recent(
            "scans", 10)],
        "equity_curve": eq,
    }
    return stats


# ------------------------------------------------------------ CSS + shell
CSS = """
:root{--bg:#0b0f14;--panel:#11161d;--card:#151c24;--border:#263041;
--text:#dce3ea;--muted:#7d8a99;--green:#2ecc71;--red:#e74c3c;
--amber:#f39c12;--blue:#3498db;--purple:#9b59b6}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
background:var(--bg);color:var(--text);padding:0 0 40px;font-size:13px}
.wrap{max-width:900px;margin:0 auto;padding:12px}
header{position:sticky;top:0;z-index:5;background:linear-gradient(
180deg,#0d1420,#0b0f14ee);border-bottom:1px solid var(--border);
padding:10px 12px;backdrop-filter:blur(6px)}
.hrow{max-width:900px;margin:0 auto;display:flex;flex-wrap:wrap;gap:8px;
align-items:center;justify-content:space-between}
h1{font-size:15px;letter-spacing:.08em}
.sub{color:var(--muted);font-size:11px}
.pill{display:inline-block;border-radius:12px;padding:2px 10px;
font-size:10.5px;font-weight:700;letter-spacing:.05em}
.p-run{background:#0e2b1a;color:var(--green);border:1px solid #1d5b36}
.p-deg{background:#2b220e;color:var(--amber);border:1px solid #5b4a1d}
.p-rev{background:#241b33;color:#b388ff;border:1px solid #4a3a6b}
.p-lock{background:#33240e;color:#ffce6b;border:1px solid #5b4a1d}
.p-dead{background:#331111;color:#ff8f8f;border:1px solid #5b1d1d}
.p-paper{background:var(--panel);color:var(--muted);border:1px solid var(--border)}
.p-pass{background:#0e2b1a;color:var(--green);border:1px solid #1d5b36}
.p-fail{background:#331111;color:#ff8f8f;border:1px solid #5b1d1d}
h2{font-size:12px;letter-spacing:.1em;color:var(--muted);margin:22px 0 8px;
text-transform:uppercase;border-bottom:1px solid var(--border);padding-bottom:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
gap:8px;margin-top:10px}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;
padding:10px}
.card .k{color:var(--muted);font-size:9.5px;text-transform:uppercase;
letter-spacing:.06em}
.card .v{font-size:17px;font-weight:700;margin-top:3px}
.card .s{color:var(--muted);font-size:10px;margin-top:2px}
.pos{color:var(--green)}.neg{color:var(--red)}.mut{color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:11.5px;margin:6px 0 4px}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--border);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
th{color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase}
.tag{display:inline-block;background:var(--panel);border:1px solid var(--border);
border-radius:10px;padding:0 7px;font-size:10px;margin:1px}
.deadhand{background:#2a1114;border:1px solid #5b1d1d;border-radius:10px;
padding:14px;margin:12px 0}
.deadhand h3{color:#ff8f8f;font-size:14px;letter-spacing:.1em;margin-bottom:6px}
.deadhand p{font-size:12px;color:#f0c8c8;margin:3px 0}
.banner{background:#3a2c0a;color:#ffd57a;text-align:center;font-weight:700;
padding:6px;font-size:11px;border-bottom:1px solid #5b4a1d}
.bar{height:8px;background:var(--panel);border-radius:4px;overflow:hidden;
border:1px solid var(--border)}
.bar>i{display:block;height:100%}
.funnel{display:flex;flex-direction:column;gap:5px;margin:8px 0}
.fstep{display:grid;grid-template-columns:120px 1fr 70px;gap:8px;
align-items:center;font-size:11.5px}
.fstep .fl{color:var(--muted)}
.kv{display:grid;grid-template-columns:170px 1fr;gap:4px 10px;
font-size:12px;margin:6px 0}
.kv .k{color:var(--muted)}
details{border:1px solid var(--border);border-radius:8px;background:var(--panel);
margin:8px 0;padding:0}
details summary{cursor:pointer;padding:9px 12px;font-size:12px;
letter-spacing:.05em;color:var(--text);list-style:none}
details summary::-webkit-details-marker{display:none}
details[open] summary{border-bottom:1px solid var(--border)}
details .body{padding:10px 12px}
footer{color:var(--muted);font-size:10.5px;text-align:center;margin-top:26px;
line-height:1.6}
.chart{background:var(--card);border:1px solid var(--border);border-radius:8px;
padding:10px;margin-top:10px}
.leg{display:flex;gap:14px;color:var(--muted);font-size:10px;margin-top:6px}
.leg i{display:inline-block;width:10px;height:3px;border-radius:2px;
margin-right:4px;vertical-align:middle}
@media(max-width:640px){
 .hrow h1{font-size:13px}
 th,td{max-width:130px}
 .kv{grid-template-columns:140px 1fr}
}
"""

STATE_PILL = {
    "RUNNING": ("p-run", "● RUNNING"),
    "DEGRADED": ("p-deg", "⚠ DEGRADED"),
    "REVIEW_REQUIRED": ("p-rev", "🔍 REVIEW REQUIRED"),
    "LOCKED": ("p-lock", "🔒 LOCKED"),
    "RECOVERY_REQUIRED": ("p-dead", "♻ RECOVERY REQUIRED"),
    "DEAD": ("p-dead", "☠ DEAD"),
}

REASON_LABELS = [
    ("ai_hold", "AI HOLD"),
    ("edge_too_small", "EDGE TOO SMALL"),
    ("low_confidence", "CONFIDENCE TOO LOW"),
    ("kill_switch", "KILL SWITCH"),
    ("max_open_positions", "RISK LIMIT"),
    ("max_exposure", "RISK LIMIT"),
    ("daily_loss", "RISK LIMIT"),
    ("drawdown", "RISK LIMIT"),
    ("spread", "SPREAD TOO WIDE"),
    ("liquidity", "LIQUIDITY TOO LOW"),
    ("cost", "EDGE BELOW COSTS"),
    ("risk_flag", "RISK FLAG"),
]


def _categorize(reasons: str) -> str:
    r = (reasons or "").lower()
    for key, label in REASON_LABELS:
        if key in r:
            return label
    return "OTHER"


# ------------------------------------------------------- section builders
def _lifecycle(db: Database) -> dict:
    row = db.conn.execute("SELECT * FROM lifecycle WHERE id=1").fetchone()
    return dict(row) if row else {"state": "RUNNING", "reason": None}


def _equity_chart(s: Dict, db: Database) -> str:
    rows = [(r["ts"], r["equity"]) for r in db.query(
        "SELECT ts, equity FROM equity_curve ORDER BY id DESC LIMIT 240")]
    rows.reverse()
    if len(rows) < 2:
        rows = [(time.time() - 1, s["starting_balance"]),
                (time.time(), s["equity"])]
    w, h, pad = 600.0, 150.0, 8.0
    vals = [v for _, v in rows]
    lo = min(min(vals), s["starting_balance"])
    hi = max(max(vals), s["starting_balance"])
    if hi - lo < 1e-9:
        hi = lo + 1.0
    t0, t1 = rows[0][0], rows[-1][0]
    span = max(1.0, t1 - t0)

    def x(ts):
        return pad + (w - 2 * pad) * (ts - t0) / span

    def y(v):
        return h - pad - (h - 2 * pad) * (v - lo) / (hi - lo)

    pts = " ".join(f"{x(ts):.1f},{y(v):.1f}" for ts, v in rows)
    area = f"{pad:.1f},{h - pad:.1f} " + pts + f" {w - pad:.1f},{h - pad:.1f}"
    base_y = y(s["starting_balance"])
    # episode boundary markers inside the visible window
    marks = ""
    for e in db.query(
            "SELECT started_ts FROM episodes WHERE started_ts >= ? "
            "AND started_ts <= ? ORDER BY id DESC LIMIT 8", (t0, t1)):
        mx = x(e["started_ts"])
        marks += (f'<line x1="{mx:.1f}" y1="{pad}" x2="{mx:.1f}" '
                  f'y2="{h - pad}" stroke="#3a4a61" stroke-width="1" '
                  f'stroke-dasharray="2 3" opacity="0.6"/>')
    return (f'<div class="chart"><svg viewBox="0 0 {w:.0f} {h:.0f}" '
            f'role="img" style="width:100%;height:auto" '
            f'preserveAspectRatio="none">'
            f'<polygon points="{area}" fill="#1d3a29" opacity="0.35"/>'
            f'{marks}'
            f'<line x1="{pad}" y1="{base_y:.1f}" x2="{w - pad}" '
            f'y2="{base_y:.1f}" stroke="#7d8a99" stroke-width="1" '
            f'stroke-dasharray="4 4" opacity="0.7"/>'
            f'<polyline points="{pts}" fill="none" stroke="#2ecc71" '
            f'stroke-width="1.8"/>'
            f'<circle cx="{x(rows[-1][0]):.1f}" cy="{y(rows[-1][1]):.1f}" '
            f'r="3" fill="#2ecc71"/></svg>'
            f'<div class="leg"><span><i style="background:#2ecc71"></i>'
            f'equity (now ${s["equity"]:.2f})</span>'
            f'<span><i style="background:#7d8a99"></i>'
            f'start ${s["starting_balance"]:.2f}</span>'
            f'<span><i style="background:#3a4a61"></i>episode boundary</span>'
            f'<span>peak ${s["peak_equity"]:.2f}</span></div></div>')


def _funnel(scans: List[dict], trades_total_new: int) -> str:
    def agg(key):
        return sum(s.get(key, 0) or 0 for s in scans) or 0
    m, c, a = agg("markets_found"), agg("candidates"), agg("ai_analyzed")
    v, rj, t = agg("signals"), agg("rejected"), trades_total_new
    steps = [("Markets scanned", m), ("Candidates", c),
             ("AI analyzed", a), ("Valid signals", v),
             ("Risk approved", v - max(0, rj)) if v >= rj else ("Risk approved", v),
             ("Paper trades", t)]
    out = ['<div class="funnel">']
    top = max(1, steps[0][1])
    colors = ["#3498db", "#5dade2", "#85c1e9", "#f39c12", "#d4ac0d", "#2ecc71"]
    for i, (label, val) in enumerate(steps):
        pct = 100.0 * val / top
        conv = (f"{100.0 * val / steps[i-1][1]:.0f}% conv."
                if i and steps[i-1][1] else "")
        out.append(
            f'<div class="fstep"><span class="fl">{_e(label)}</span>'
            f'<span class="bar"><i style="width:{min(100, pct):.0f}%;'
            f'background:{colors[i]}"></i></span>'
            f'<span><b>{val}</b> <span class="mut">{conv}</span></span></div>')
    out.append('</div>')
    out.append('<div class="sub">Aggregated over the last 10 scans. '
               '"Valid signals" passed the edge test; the remainder were '
               'rejected with exact reasons (see rejection analytics).</div>')
    return "".join(out)


def _rejection_analytics(db: Database) -> str:
    rows = db.query("SELECT reasons, stage FROM rejections ORDER BY id DESC LIMIT 2000")
    if not rows:
        return "<p class='sub'>No rejections logged yet.</p>"
    counts = {}
    for r in rows:
        cat = _categorize(r["reasons"]) + (f" ({r['stage']})"
                                           if r["stage"] == "risk" else "")
        counts[cat] = counts.get(cat, 0) + 1
    total = sum(counts.values())
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    out = ['<div class="funnel">']
    for label, n in items:
        pct = 100.0 * n / total
        out.append(
            f'<div class="fstep"><span class="fl">{_e(label)}</span>'
            f'<span class="bar"><i style="width:{pct:.0f}%;'
            f'background:#e74c3c"></i></span>'
            f'<span><b>{n}</b> <span class="mut">{pct:.1f}%</span></span></div>')
    out.append("</div>")
    return "".join(out)


def _risk_panel(db: Database, s: Dict, cfg: Config) -> str:
    open_rows = s["open_positions"]
    exposure = sum(r["quantity"] * r["avg_entry_price"] for r in open_rows)
    exp_pct = 100 * exposure / max(1, s["starting_balance"])
    dd_pct = s["max_drawdown_pct"]
    # daily realized loss (trailing 24h)
    day_ago = time.time() - 86400
    day_pnl = sum(t["pnl"] for t in db.query(
        "SELECT pnl FROM trades WHERE pnl IS NOT NULL AND ts >= ?",
        (day_ago,)))
    dl_used = max(0.0, -day_pnl) / max(1, s["starting_balance"]) * 100
    def bar(pct, limit, color):
        frac = min(100.0, 100.0 * pct / limit if limit else 0)
        return (f'<span class="bar"><i style="width:{frac:.0f}%;'
                f'background:{color}"></i></span>')
    rows = [
        ("Daily loss used", bar(dl_used, cfg.MAX_DAILY_LOSS_PERCENT * 100, "#e74c3c"),
         f"{dl_used:.1f}% of {cfg.MAX_DAILY_LOSS_PERCENT * 100:.0f}% limit"),
        ("Drawdown used", bar(dd_pct, cfg.MAX_DRAWDOWN_PERCENT * 100, "#f39c12"),
         f"{dd_pct:.2f}% of {cfg.MAX_DRAWDOWN_PERCENT * 100:.0f}% limit"),
        ("Exposure used", bar(exp_pct, cfg.MAX_OPEN_EXPOSURE_PERCENT * 100, "#3498db"),
         f"{exp_pct:.1f}% of {cfg.MAX_OPEN_EXPOSURE_PERCENT * 100:.0f}% limit"),
        ("Open positions", bar(len(open_rows), cfg.MAX_OPEN_POSITIONS, "#9b59b6"),
         f"{len(open_rows)} of {cfg.MAX_OPEN_POSITIONS}"),
        ("Max position", "", f"{cfg.MAX_POSITION_PERCENT * 100:.0f}% of bankroll per trade"),
        ("Kill switch", "", "ACTIVE - all trades blocked"
         if cfg.KILL_SWITCH else "inactive"),
    ]
    out = ['<div class="kv">']
    for k, b, v in rows:
        out.append(f'<span class="k">{_e(k)}</span><span>{b} '
                   f'<span class="mut">{_e(v)}</span></span>')
    out.append("</div>")
    return "".join(out)


def _system_health(db: Database, cfg: Config) -> str:
    ph = db.latest_provider_health()
    canary = db.latest_canary()
    lc = _lifecycle(db)
    scans = db.query("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
    scan = json.loads(scans[0]["payload"]) if scans else {}
    ai = db.query("SELECT ts FROM ai_decisions ORDER BY id DESC LIMIT 1")
    try:
        from agent.lifecycle import lease_timestamp, load_release
        lease_ts = lease_timestamp()
        release = load_release() or {}
    except Exception:  # noqa: BLE001
        lease_ts, release = None, {}
    lease_left = (f"{(cfg.MAINTENANCE_LEASE_DAYS - (time.time() - lease_ts) / 86400):.1f} days"
                  if lease_ts else "unknown")
    integrity = db.conn.execute("PRAGMA quick_check").fetchone()[0]
    phd = dict(ph) if ph else {}
    rows = [
        ("Latest successful scan", _fmt_ts(scans[0]["ts"]) if scans else "-"),
        ("Scan age", _age(scans[0]["ts"]) if scans else "-"),
        ("Data provider", f"{phd.get('provider', '-')} - "
         f"{phd.get('requests', 0)} req, {phd.get('errors', 0)} errors, "
         f"{phd.get('latency_ms', 0)}ms avg"),
        ("Markets / valid", f"{phd.get('markets_returned', 0)} / "
         f"{phd.get('valid_markets', 0)}"),
        ("Price-history success", f"{phd.get('history_success', 0)}/"
         f"{phd.get('history_attempts', 0)}"),
        ("Last successful AI analysis", _age(ai[0]["ts"]) if ai else "-"),
        ("Database", f"SQLite quick_check: {_e(integrity)}"),
        ("Last human strategy release",
         f"{release.get('version', '-')} ({_age(lease_ts)})" if lease_ts else "-"),
        ("Maintenance lease remaining", lease_left),
        ("Software / strategy", f"v{release.get('version', '-')} / {cfg.STRATEGY}"),
        ("Lifecycle", f"{lc.get('state')} ({lc.get('reason') or 'nominal'})"),
    ]
    out = ['<div class="kv">']
    for k, v in rows:
        out.append(f'<span class="k">{_e(k)}</span><span>{v}</span>')
    out.append("</div>")
    return "".join(out)


def _episode_panel(db: Database, s: Dict) -> str:
    active = db.active_episode()
    if active is None:
        return "<p class='sub'>No active episode window.</p>"
    ret = (s["equity"] - active["starting_balance"]) / active["starting_balance"] * 100 \
        if active["starting_balance"] else 0
    elapsed = time.time() - active["started_ts"]
    remaining = max(0.0, 24 * 3600 - elapsed)
    n_buy = db.query(
        "SELECT COUNT(*) AS c FROM trades WHERE action='BUY' AND ts >= ?",
        (active["started_ts"],))[0]["c"]
    score_row = db.query(
        "SELECT score FROM score_history WHERE scope='episode' ORDER BY id DESC LIMIT 1")
    streak = 0
    for r in db.query("SELECT report FROM episodes WHERE status='completed' "
                      "ORDER BY id DESC LIMIT 20"):
        try:
            rep = json.loads(r["report"])
        except (TypeError, ValueError):
            continue
        if (rep.get("return_pct") or 0) > 0:
            streak += 1
        else:
            break
    out = [f'<div class="grid">']
    cells = [
        ("Window remaining", f"{int(remaining // 3600)}h {int((remaining % 3600) // 60)}m"),
        ("Starting equity", f"${active['starting_balance']:.2f}"),
        ("Current equity", f"${s['equity']:.2f}"),
        ("Window return", f"{ret:+.2f}%"),
        ("Trades (entries)", str(n_buy)),
        ("Latest episode score", f"{score_row[0]['score']:.1f}"
         if score_row else "-"),
        ("Profitable-window streak", str(streak)),
    ]
    for k, v in cells:
        cls = _cls(ret) if "return" in k.lower() else ""
        out.append(f'<div class="card"><div class="k">{_e(k)}</div>'
                   f'<div class="v {cls}">{_e(v)}</div></div>')
    out.append("</div>")
    out.append("<div class='sub'>A 24h window is an EVALUATION ONLY: the "
               "$100 bankroll is one-time and never resets.</div>")
    return "".join(out)


def _badges(db: Database, s: Dict) -> str:
    """Informational, gamified only - never affects risk limits."""
    badges = []
    if s["total_trades"] >= 1:
        badges.append("First Trade")
    if s["winning_trades"] >= 3:
        badges.append("3 Profitable Trades")
    if s["max_drawdown_pct"] < 2 and s["total_trades"] >= 1:
        badges.append("Low Drawdown")
    eps = db.query("SELECT report FROM episodes WHERE status='completed'")
    wins24 = 0
    for r in eps:
        try:
            rep = json.loads(r["report"])
        except (TypeError, ValueError):
            continue
        if (rep.get("return_pct") or 0) > 0:
            wins24 += 1
    if wins24 >= 1:
        badges.append("24h Survival")
    if len(eps) >= 7:
        badges.append("7-Day Survival")
    if not badges:
        return "<span class='mut'>none yet</span>"
    return "".join(f'<span class="tag">{_e(b)}</span>' for b in badges)


# ------------------------------------------------------------ main render
def generate_dashboard(cfg: Config, db: Database, out_path: str = None) -> str:
    wallet = db.init_wallet(cfg.STARTING_BALANCE)
    s = compute_stats(db, wallet.balance, wallet.starting_balance)
    lc = _lifecycle(db)

    pill_cls, pill_txt = STATE_PILL.get(
        lc.get("state"), ("p-run", "RUNNING"))

    # dead-hand banner
    deadhand = ""
    if lc.get("state") in ("DEAD", "RECOVERY_REQUIRED", "LOCKED",
                           "REVIEW_REQUIRED"):
        reason = lc.get("reason") or "-"
        need = ("a new human strategy release (strategy/RELEASE.json with a "
                "new version) + explicit acknowledgement "
                "(`python main.py --recover --ack " + _e(str(reason)) + "`)")
        if lc["state"] == "LOCKED":
            need = "automatic unlock after 3 consecutive healthy data cycles"
        elif lc["state"] == "REVIEW_REQUIRED":
            need = ("a human strategy release within "
                    f"{cfg.REVIEW_DEADLINE_HOURS:.0f}h, or the agent dies "
                    "(MAINTENANCE_EXPIRED)")
        deadhand = (f'<div class="deadhand"><h3>{pill_txt} '
                    f'&mdash; reason: {_e(reason)}</h3>'
                    f'<p>Since: {_fmt_ts(lc.get("since_ts"))} '
                    f'({_age(lc.get("since_ts"))})</p>'
                    f'<p>Details: {_e(lc.get("details") or "n/a")}</p>'
                    f'<p>Recovery requires: {need}</p>'
                    f'<p>Human code release required: '
                    f'{"YES" if lc["state"] in ("DEAD", "RECOVERY_REQUIRED") else "no"}'
                    f' &middot; history is always preserved</p></div>')

    # canary
    canary = db.latest_canary()
    canary_html = "<span class='pill p-fail'>CANARY: NO DATA</span>"
    if canary:
        ok = bool(canary["passed"])
        canary_html = (f'<span class="pill {"p-pass" if ok else "p-fail"}">'
                       f'CANARY: {"PASS" if ok else "FAIL"} '
                       f'({_age(canary["ts"])})</span>')

    # main cards
    exposure = sum(r["quantity"] * r["avg_entry_price"] for r in s["open_positions"])
    last_ep_score = db.query(
        "SELECT score FROM score_history WHERE scope='episode' "
        "ORDER BY id DESC LIMIT 1")
    score_v = f"{last_ep_score[0]['score']:.0f}/100" if last_ep_score else "-"
    health = 100 if lc["state"] == "RUNNING" else {
        "DEGRADED": 60, "REVIEW_REQUIRED": 40, "LOCKED": 20,
        "RECOVERY_REQUIRED": 5, "DEAD": 0}.get(lc["state"], 50)
    cards = [
        ("Current equity", f"${s['equity']:.2f}", ""),
        ("Lifetime P&L", _money(s["total_pnl"]), _cls(s["total_pnl"])),
        ("Return", f"{s['return_pct']:+.2f}%", _cls(s["return_pct"])),
        ("Drawdown", f"{s['max_drawdown_pct']:.2f}%", ""),
        ("Open exposure", f"${exposure:.2f}", ""),
        ("Trades", str(s["total_trades"]), ""),
        ("Win rate", f"{s['win_rate']:.1f}%", ""),
        ("Current score", score_v, ""),
        ("Health", f"{health}/100", "pos" if health >= 60 else "neg"),
        ("Unrealized", _money(s["unrealized_pnl"]), _cls(s["unrealized_pnl"])),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{_e(k)}</div>'
        f'<div class="v {c}">{_e(v)}</div></div>' for k, v, c in cards)

    # tables
    open_rows = "".join(
        f"<tr><td title='{_e(r['question'])}'>{_e(r['question'][:48])}</td>"
        f"<td>{_e(r['side'])}</td><td>{r['quantity']:.2f}</td>"
        f"<td>{r['avg_entry_price']:.3f}</td><td>{r['mark_price']:.3f}</td>"
        f"<td class='{_cls((r['mark_price']-r['avg_entry_price'])*r['quantity'])}'>"
        f"{_money((r['mark_price']-r['avg_entry_price'])*r['quantity'])}</td></tr>"
        for r in s["open_positions"]) or "<tr><td colspan=6>None</td></tr>"

    signal_rows = "".join(
        f"<tr><td title='{_e(d.get('reason', ''))}'>{_e(d.get('market_id', ''))}"
        f"<div class='mut' style='font-size:10px;max-width:180px;overflow:hidden;"
        f"text-overflow:ellipsis'>{_e(str(d.get('reason', ''))[:70])}</div></td>"
        f"<td>{d.get('market_probability', '-')}</td>"
        f"<td>{d.get('estimated_probability', '-')}</td>"
        f"<td class='{_cls(d.get('edge', 0) or 0)}'>{d.get('edge', '-')}</td>"
        f"<td>{d.get('confidence', '-')}</td>"
        f"<td class='{'mut' if d.get('decision') == 'HOLD' else 'pos'}'>"
        f"{_e(d.get('decision', ''))}</td>"
        f"<td>{''.join(f'<span class=tag>{_e(f)}</span>' for f in (d.get('risk_flags') or [])[:3])}</td>"
        f"<td class='mut' style='font-size:10px'>{_e(d.get('strategy', ''))}</td></tr>"
        for d in s["recent_signals"]) or "<tr><td colspan=8>None</td></tr>"

    trade_rows = "".join(
        f"<tr><td>{_fmt_ts(t['ts'])}</td><td>{_e(t['market_id'])}</td>"
        f"<td>{_e(t['action'])}</td><td>{t['quantity']:.2f}</td>"
        f"<td>{t['price']:.3f}</td>"
        f"<td class='{_cls(t['pnl'] or 0)}'>{_money(t['pnl'] or 0)}</td></tr>"
        for t in s["recent_trades"]) or "<tr><td colspan=6>None</td></tr>"

    # strategy performance
    try:
        from lab.sweep import promotion_states
        promo = promotion_states()
    except Exception:  # noqa: BLE001
        promo = {}
    strat_rows = ""
    for name, st in promo.items():
        n = db.query("SELECT COUNT(*) AS c FROM ai_decisions")[0]["c"]
        strat_rows += (f"<tr><td>{_e(name)}</td><td>{_e(st.get('state', ''))}</td>"
                       f"<td>{st.get('since', '-')}</td></tr>")
    strat_rows += (f"<tr><td colspan=3 class='mut'>Active: "
                   f"{_e(cfg.STRATEGY)}"
                   f"{' + shadow: ' + _e(cfg.SHADOW_STRATEGY) if cfg.SHADOW_STRATEGY else ''}"
                   f" &middot; promotion to ACTIVE always needs human approval"
                   f" (strategy/promotion.json)</td></tr>")

    # episode history
    ep_rows = ""
    for ep in db.query("SELECT * FROM episodes ORDER BY id DESC LIMIT 8"):
        if ep["status"] == "completed":
            try:
                r = json.loads(ep["report"])
                ep_rows += (f"<tr><td>#{ep['id']}</td>"
                            f"<td>{_fmt_ts(ep['started_ts'])} &rarr; "
                            f"{_fmt_ts(ep['ended_ts'])}</td>"
                            f"<td>${r.get('starting_balance', 0):.2f} &rarr; "
                            f"${r.get('ending_balance', 0):.2f}</td>"
                            f"<td class='{_cls(r.get('return_pct', 0))}'>"
                            f"{r.get('return_pct', 0):+.2f}%</td>"
                            f"<td>{r.get('trades', 0)}</td>"
                            f"<td>{r.get('win_rate', 0):.1f}%</td>"
                            f"<td>{r.get('max_drawdown_pct', 0):.2f}%</td>"
                            f"<td><b>{r.get('final_score', '-')}</b></td></tr>")
            except (TypeError, ValueError):
                continue
        else:
            ep_rows += (f"<tr><td>#{ep['id']} live</td>"
                        f"<td>{_fmt_ts(ep['started_ts'])} &rarr; now</td>"
                        f"<td>${ep['starting_balance']:.2f} &rarr; "
                        f"${s['equity']:.2f}</td>"
                        f"<td class='{_cls(s['equity'] - ep['starting_balance'])}'>"
                        f"{(s['equity'] - ep['starting_balance']) / max(0.01, ep['starting_balance']) * 100:+.2f}%</td>"
                        f"<td colspan=4 class='mut'>in progress</td></tr>")
    if not ep_rows:
        ep_rows = "<tr><td colspan=8>None</td></tr>"

    # recent lifecycle events
    ev_rows = "".join(
        f"<tr><td>{_fmt_ts(r['ts'])}</td><td>{_e(r['from_state'] or '')}"
        f" &rarr; <b>{_e(r['to_state'])}</b></td>"
        f"<td>{_e(r['reason'] or '')}</td></tr>"
        for r in db.query(
            "SELECT * FROM lifecycle_events ORDER BY id DESC LIMIT 10")
    ) or "<tr><td colspan=3>No transitions yet</td></tr>"

    # scan rows
    scan_rows = "".join(
        f"<tr><td>{sc.get('markets_found', 0)}</td>"
        f"<td>{sc.get('candidates', 0)}</td>"
        f"<td>{sc.get('ai_analyzed', 0)}</td>"
        f"<td>{sc.get('signals', 0)}</td>"
        f"<td>{sc.get('paper_trades', 0)}</td>"
        f"<td>${sc.get('balance', 0):.2f}</td>"
        f"<td class='mut'>{_e(str(sc.get('lifecycle', '')) or '')}</td></tr>"
        for sc in s["recent_scans"]) or "<tr><td colspan=7>None</td></tr>"

    n_trades_new = sum(sc.get("paper_trades", 0) for sc in s["recent_scans"])

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>AI Paper Trading Terminal</title>
<style>{CSS}</style></head><body>
<div class="banner">PAPER TRADING ONLY &mdash; SIMULATED MONEY &mdash;
REAL-MONEY EXECUTION PERMANENTLY DISABLED IN CODE</div>
<header><div class="hrow">
<h1>AI PAPER TRADING TERMINAL</h1>
<div>{canary_html} <span class="pill {pill_cls}">{pill_txt}</span></div>
</div>
<div class="hrow" style="margin-top:6px">
<span class="sub">Updated {utcnow_iso()} &middot; strategy: {_e(cfg.STRATEGY)}
&middot; <a href="javascript:location.reload()" style="color:var(--blue)">
reload</a></span>
<span><span class="pill p-paper">PAPER ONLY</span>
<span class="pill p-paper">NO REAL ORDERS</span></span>
</div></header>
<div class="wrap">
{deadhand}
<div class="grid">{cards_html}</div>
{_equity_chart(s, db)}

<h2>24h Challenge Window</h2>
{_episode_panel(db, s)}

<h2>Signal Funnel (why is / isn't the agent trading)</h2>
{_funnel(s['recent_scans'], n_trades_new)}

<details open><summary>AI Signals (last 20)</summary><div class="body">
<table><tr><th>Market</th><th>Mkt p</th><th>Est p</th><th>Edge</th>
<th>Conf</th><th>Decision</th><th>Flags</th><th>Strategy</th></tr>
{signal_rows}</table></div></details>

<details><summary>Rejection Analytics (categorized, with %)</summary>
<div class="body">{_rejection_analytics(db)}</div></details>

<details><summary>Open Positions ({len(s['open_positions'])})</summary>
<div class="body"><table><tr><th>Market</th><th>Side</th><th>Qty</th>
<th>Entry</th><th>Mark</th><th>P&L</th></tr>{open_rows}</table></div></details>

<details><summary>Recent Trades (last 20)</summary><div class="body">
<table><tr><th>Time</th><th>Market</th><th>Action</th><th>Qty</th>
<th>Price</th><th>P&L</th></tr>{trade_rows}</table></div></details>

<details><summary>Risk Panel</summary><div class="body">
{_risk_panel(db, s, cfg)}</div></details>

<details><summary>System Health</summary><div class="body">
{_system_health(db, cfg)}</div></details>

<details><summary>Strategy Performance</summary><div class="body">
<table><tr><th>Strategy</th><th>State</th><th>Since</th></tr>
{strat_rows}</table></div></details>

<details><summary>Episode History</summary><div class="body">
<table><tr><th>Ep</th><th>Period</th><th>Equity</th><th>Return</th>
<th>Trades</th><th>Win rate</th><th>Max DD</th><th>Score</th></tr>
{ep_rows}</table></div></details>

<details><summary>Lifecycle Events (dead-hand audit trail)</summary>
<div class="body"><table><tr><th>When</th><th>Transition</th>
<th>Reason</th></tr>{ev_rows}</table></div></details>

<details><summary>Achievements (informational only)</summary>
<div class="body"><p>{_badges(db, s)}</p>
<p class="sub">Badges never affect risk limits - they are gamified
feedback only.</p></div></details>

<h2>Last Scans</h2>
<table><tr><th>Markets</th><th>Candidates</th><th>Analyzed</th>
<th>Signals</th><th>Trades</th><th>Balance</th><th>State</th></tr>
{scan_rows}</table>

<footer>Simulation &mdash; slippage/fees applied. Not investment advice.
No claim of profitability without statistically meaningful
paper/backtest evidence. Every number on this page comes from the
committed SQLite state (no fake data).</footer>
</div></body></html>"""

    out = out_path or cfg.DASHBOARD_PATH
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    logger.info("dashboard written to %s", out)
    return out
