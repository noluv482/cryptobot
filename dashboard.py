"""
dashboard.py — Read-only performance dashboard for CryptoBot.

Reads the SAME Postgres `trades` table the bot already writes to and serves an
honest, public track record: real equity curve, real win rate, and — given
equal billing — real max drawdown and every losing trade. It is the credible
opposite of "12 WINS / 0 LOSS" signal-selling screenshots.

Read-only by design: it never writes, never trades, never takes input. Safe to
expose publicly.

Run locally:
    DATABASE_URL=postgres://... python3 dashboard.py
    # open http://localhost:8080

Deploy on Railway as a SECOND service from the same repo:
    Start command:  python3 dashboard.py
    Variables:      DATABASE_URL  (same Postgres as the bot)
    Railway assigns a public URL — that URL is your shareable track record.
"""

import os
import json
from datetime import datetime, timezone

from flask import Flask, jsonify, Response

app = Flask(__name__)

PAPER_START = 100.0   # must match the bot's starting balance


# ── Data layer ─────────────────────────────────────────────────────────────────
def fetch_trades():
    """Pull all closed trades from Postgres, oldest first. Returns list of dicts."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None   # signals "not configured" to the caller
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url, connect_timeout=5)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ts, coin, pair, side, entry, exit_price, pnl,
                       held_mins, reason, confidence, balance_after
                FROM trades
                ORDER BY ts ASC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        app.logger.error(f"DB error: {e}")
        return []


# ── Stats (pure function — unit-testable without a DB) ──────────────────────────
def compute_stats(rows):
    start = PAPER_START
    if not rows:
        return {"configured": True, "has_data": False, "start": start}

    pnls   = [float(r["pnl"]) for r in rows]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(rows)

    # Equity curve from the balance the bot logged after each trade.
    eq_vals   = [start] + [float(r["balance_after"]) for r in rows]
    eq_labels = ["start"] + [
        datetime.fromtimestamp(float(r["ts"]), tz=timezone.utc).strftime("%m-%d %H:%M")
        for r in rows
    ]
    current = eq_vals[-1]

    # Max drawdown across the realized equity curve.
    peak, max_dd = start, 0.0
    for v in eq_vals:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)

    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    # Per-coin performance
    by_coin = {}
    for r in rows:
        c = r.get("coin") or r.get("pair") or "?"
        d = by_coin.setdefault(c, {"n": 0, "w": 0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += float(r["pnl"])
        if float(r["pnl"]) > 0:
            d["w"] += 1
    coins = sorted(
        [{"coin": c, "n": d["n"], "win_rate": round(d["w"] / d["n"] * 100, 1),
          "pnl": round(d["pnl"], 2)} for c, d in by_coin.items()],
        key=lambda x: x["n"], reverse=True)

    # Confidence calibration — does "high confidence" actually win more?
    def tier_stats(pred):
        sub = [r for r in rows if pred(float(r.get("confidence") or 0))]
        if not sub:
            return {"n": 0, "win_rate": 0.0}
        w = sum(1 for r in sub if float(r["pnl"]) > 0)
        return {"n": len(sub), "win_rate": round(w / len(sub) * 100, 1)}
    calibration = {"high": tier_stats(lambda c: c >= 0.6),
                   "low":  tier_stats(lambda c: c < 0.6)}

    # Exit-reason breakdown
    by_reason = {}
    for r in rows:
        rs = r.get("reason") or "?"
        d = by_reason.setdefault(rs, {"n": 0, "pnl": 0.0})
        d["n"] += 1
        d["pnl"] += float(r["pnl"])
    reasons = sorted(
        [{"reason": k, "n": v["n"], "avg_pnl": round(v["pnl"] / v["n"], 3)}
         for k, v in by_reason.items()],
        key=lambda x: x["avg_pnl"], reverse=True)

    # Recent trades (newest first) — losses shown, not hidden
    recent = []
    for r in reversed(rows[-25:]):
        recent.append({
            "time": datetime.fromtimestamp(float(r["ts"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "coin": r.get("coin") or r.get("pair"),
            "side": r.get("side"),
            "pnl": round(float(r["pnl"]), 2),
            "reason": r.get("reason"),
            "conf": round(float(r.get("confidence") or 0) * 100),
        })

    span_days = (float(rows[-1]["ts"]) - float(rows[0]["ts"])) / 86400 if n > 1 else 0

    return {
        "configured": True, "has_data": True, "start": start,
        "current": round(current, 2),
        "return_pct": round((current / start - 1) * 100, 2),
        "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_factor": (round(pf, 2) if pf != float("inf") else None),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(pnls) / n, 3),
        "max_drawdown": round(max_dd * 100, 1),
        "best": round(max(pnls), 2), "worst": round(min(pnls), 2),
        "span_days": round(span_days, 1),
        "equity": {"labels": eq_labels, "values": [round(v, 2) for v in eq_vals]},
        "coins": coins, "calibration": calibration,
        "reasons": reasons, "recent": recent,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


@app.route("/api/stats")
def api_stats():
    rows = fetch_trades()
    if rows is None:
        return jsonify({"configured": False, "has_data": False})
    return jsonify(compute_stats(rows))


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


# ── Front-end (single self-contained page) ──────────────────────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CryptoBot — Live Track Record</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --ink:#0E1621; --panel:#17222F; --panel2:#1E2B3A; --line:#293a4d;
    --text:#E7ECF3; --muted:#8698ad; --teal:#47C7BD;
    --win:#46B98A; --loss:#E06B57; --amber:#E0A458;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ink);color:var(--text);
    font-family:Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased;line-height:1.5}
  a{color:var(--teal)}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 64px}
  .mono{font-family:"IBM Plex Mono",monospace}

  header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
    border-bottom:1px solid var(--line);padding-bottom:16px}
  .logo{font-family:"Space Grotesk",sans-serif;font-weight:700;font-size:20px;letter-spacing:-.01em}
  .logo b{color:var(--teal)}
  .status{margin-left:auto;font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted)}
  .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--win);
    margin-right:6px;vertical-align:middle;box-shadow:0 0 0 0 rgba(70,185,138,.6);animation:pulse 2.4s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(70,185,138,.5)}70%{box-shadow:0 0 0 7px rgba(70,185,138,0)}100%{box-shadow:0 0 0 0 rgba(70,185,138,0)}}

  .banner{margin:18px 0 26px;padding:12px 16px;border:1px solid var(--line);
    border-left:3px solid var(--amber);border-radius:8px;background:var(--panel);
    font-size:13.5px;color:#cdd7e3}
  .banner b{color:var(--text)}

  h2{font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:13px;letter-spacing:.14em;
    text-transform:uppercase;color:var(--muted);margin:34px 0 14px}

  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 16px 14px}
  .card .k{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
  .card .v{font-family:"Space Grotesk",sans-serif;font-weight:700;font-size:30px;margin-top:8px;
    letter-spacing:-.02em;line-height:1}
  .card .s{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted);margin-top:7px}
  .pos{color:var(--win)} .neg{color:var(--loss)} .warn{color:var(--amber)} .tealv{color:var(--teal)}

  .chartbox{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 14px 8px;margin-top:6px}
  .chartbox .cap{display:flex;justify-content:space-between;align-items:baseline;padding:0 4px 6px}
  .chartbox .cap .now{font-family:"Space Grotesk",sans-serif;font-weight:700;font-size:22px}
  .chartbox .cap small{color:var(--muted);font-size:12px;font-family:"IBM Plex Mono",monospace}
  canvas{width:100%!important}

  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
  th{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.05em;text-transform:uppercase;
    color:var(--muted);font-weight:500}
  td.num,th.num{text-align:right;font-family:"IBM Plex Mono",monospace}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .panel .ph{padding:12px 14px;border-bottom:1px solid var(--line);
    font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:13px;letter-spacing:.02em}
  .tag{font-family:"IBM Plex Mono",monospace;font-size:11px;padding:2px 7px;border-radius:5px;border:1px solid var(--line)}
  .buy{color:var(--win)} .sell{color:var(--loss)}

  .empty{background:var(--panel);border:1px dashed var(--line);border-radius:14px;padding:44px 24px;
    text-align:center;color:var(--muted);margin-top:20px}
  .empty h3{font-family:"Space Grotesk",sans-serif;color:var(--text);margin:0 0 8px}
  footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
    font-size:12px;color:var(--muted);font-family:"IBM Plex Mono",monospace}

  @media(max-width:760px){
    .cards{grid-template-columns:repeat(2,1fr)}
    .grid2{grid-template-columns:1fr}
    .card .v{font-size:26px}
  }
  @media(prefers-reduced-motion:reduce){.dot{animation:none}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">CryptoBot <b>·</b> Track Record</div>
    <div class="status"><span class="dot"></span><span id="updated">loading…</span></div>
  </header>

  <div class="banner">
    <b>Paper trading.</b> Real Kraken market prices, simulated $100 of capital — no real money is at risk.
    Every trade is recorded straight from the bot's database, <b>including losing ones</b>. Drawdown and worst
    trade are shown next to the gains, because a track record that only shows wins isn't one.
  </div>

  <div id="content"></div>

  <footer>
    Auto-refreshes every 60s · read-only · figures computed from the live trade log.
  </footer>
</div>

<script>
const fmt = (n,d=2)=> (n==null?"—":Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d}));
const signClass = n => n>0?"pos":(n<0?"neg":"");
let chart;

function render(s){
  const c = document.getElementById("content");
  document.getElementById("updated").textContent = s.updated || "";

  if(!s.configured){
    c.innerHTML = `<div class="empty"><h3>Database not connected</h3>
      Set <span class="mono">DATABASE_URL</span> to the same Postgres the bot uses, then reload.</div>`;
    return;
  }
  if(!s.has_data){
    c.innerHTML = `<div class="empty"><h3>No trades logged yet</h3>
      The record begins the moment the bot closes its first position. Leave it running and check back.</div>`;
    return;
  }

  const ret = s.return_pct;
  c.innerHTML = `
    <div class="cards">
      <div class="card"><div class="k">Balance</div>
        <div class="v">$${fmt(s.current)}</div>
        <div class="s">from $${fmt(s.start,0)} start</div></div>
      <div class="card"><div class="k">Total return</div>
        <div class="v ${signClass(ret)}">${ret>0?"+":""}${fmt(ret)}%</div>
        <div class="s">${s.span_days} days · ${s.trades} trades</div></div>
      <div class="card"><div class="k">Win rate</div>
        <div class="v">${fmt(s.win_rate,1)}%</div>
        <div class="s">${s.wins}W · ${s.losses}L</div></div>
      <div class="card"><div class="k">Max drawdown</div>
        <div class="v warn">-${fmt(s.max_drawdown,1)}%</div>
        <div class="s">worst trade $${fmt(s.worst)}</div></div>
    </div>

    <h2>Equity curve</h2>
    <div class="chartbox">
      <div class="cap"><span class="now">$${fmt(s.current)}</span>
        <small>profit factor ${s.profit_factor==null?"∞":fmt(s.profit_factor)} ·
        expectancy $${fmt(s.expectancy,3)}/trade · avg win $${fmt(s.avg_win)} / avg loss $${fmt(s.avg_loss)}</small></div>
      <canvas id="eq" height="220"></canvas>
    </div>

    <div class="grid2" style="margin-top:26px">
      <div class="panel"><div class="ph">Performance by coin</div>
        <table><thead><tr><th>Coin</th><th class="num">Trades</th><th class="num">Win %</th><th class="num">PnL</th></tr></thead>
        <tbody>${s.coins.map(x=>`<tr><td>${x.coin}</td><td class="num">${x.n}</td>
          <td class="num">${fmt(x.win_rate,1)}</td>
          <td class="num ${signClass(x.pnl)}">${x.pnl>0?"+":""}${fmt(x.pnl)}</td></tr>`).join("")||
          `<tr><td colspan="4" style="color:var(--muted)">—</td></tr>`}</tbody></table></div>

      <div class="panel"><div class="ph">Confidence check</div>
        <table><thead><tr><th>Signal tier</th><th class="num">Trades</th><th class="num">Win %</th></tr></thead>
        <tbody>
          <tr><td>High conf (≥60%)</td><td class="num">${s.calibration.high.n}</td><td class="num">${fmt(s.calibration.high.win_rate,1)}</td></tr>
          <tr><td>Low conf (&lt;60%)</td><td class="num">${s.calibration.low.n}</td><td class="num">${fmt(s.calibration.low.win_rate,1)}</td></tr>
        </tbody></table>
        <div class="ph" style="border-top:1px solid var(--line)">Exit reasons</div>
        <table><tbody>${s.reasons.map(r=>`<tr><td>${r.reason}</td><td class="num">${r.n}</td>
          <td class="num ${signClass(r.avg_pnl)}">${r.avg_pnl>0?"+":""}${fmt(r.avg_pnl,3)}</td></tr>`).join("")}</tbody></table>
      </div>
    </div>

    <h2>Recent trades</h2>
    <div class="panel"><table>
      <thead><tr><th>Time (UTC)</th><th>Coin</th><th>Side</th><th class="num">Conf</th><th class="num">PnL</th><th>Exit</th></tr></thead>
      <tbody>${s.recent.map(t=>`<tr>
        <td class="mono" style="color:var(--muted)">${t.time}</td>
        <td>${t.coin}</td>
        <td><span class="tag ${t.side==='LONG'?'buy':'sell'}">${t.side}</span></td>
        <td class="num">${t.conf}%</td>
        <td class="num ${signClass(t.pnl)}">${t.pnl>0?"+":""}${fmt(t.pnl)}</td>
        <td style="color:var(--muted)">${t.reason}</td></tr>`).join("")}</tbody>
    </table></div>`;

  drawChart(s.equity);
}

function drawChart(eq){
  const ctx = document.getElementById("eq").getContext("2d");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const g = ctx.createLinearGradient(0,0,0,220);
  g.addColorStop(0,"rgba(71,199,189,.28)"); g.addColorStop(1,"rgba(71,199,189,0)");
  if(chart) chart.destroy();
  chart = new Chart(ctx,{type:"line",
    data:{labels:eq.labels,datasets:[{data:eq.values,borderColor:"#47C7BD",
      backgroundColor:g,borderWidth:2,fill:true,tension:.25,pointRadius:0,
      pointHoverRadius:4,pointHoverBackgroundColor:"#47C7BD"}]},
    options:{responsive:true,maintainAspectRatio:false,animation:reduce?false:{duration:900},
      plugins:{legend:{display:false},
        tooltip:{callbacks:{title:i=>i[0].label,label:i=>" $"+Number(i.raw).toFixed(2)},
          backgroundColor:"#0E1621",borderColor:"#293a4d",borderWidth:1,padding:10,
          titleColor:"#8698ad",bodyColor:"#E7ECF3",bodyFont:{family:"IBM Plex Mono"}}},
      scales:{
        x:{grid:{display:false},ticks:{color:"#5f7186",maxTicksLimit:8,font:{family:"IBM Plex Mono",size:10}}},
        y:{grid:{color:"#1f2c3a"},ticks:{color:"#5f7186",font:{family:"IBM Plex Mono",size:10},
          callback:v=>"$"+v}}}}});
}

async function load(){
  try{ const r = await fetch("/api/stats"); render(await r.json()); }
  catch(e){ document.getElementById("content").innerHTML =
    `<div class="empty"><h3>Couldn't load stats</h3>${e}</div>`; }
}
load(); setInterval(load, 60000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
