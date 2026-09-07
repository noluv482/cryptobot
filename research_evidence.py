#!/usr/bin/env python3
"""EVIDENCE PACK [E] — one JSON document on stdout, read-only, in-container.

    docker exec <bot-container> python research_evidence.py > evidence.json

WHAT IT IS
----------
The PC-side research pass (crew_brain chain) must reason over MEASURED
numbers, never over prose about them. This script gathers every measured
artifact the bot already produces and prints them as ONE JSON document:

    generated            epoch + iso of this run
    regime_table         learning_report.regime_table over resolved shadow rows
    gate_table           learning_report.gate_table over the same rows
    spread_map_summary   per-pair digest of learning_report.spread_map
    rejects_by_gate_week ISO-week x gate counts (engine_rejects + shadow
                         rejected_by), last 8 weeks
    shadow_counts        shadow_signals readiness per forward horizon
    graveyard            KILLED entrants from the autopilot's persisted state
    funding_summary      per-symbol 30d mean hourly funding + sign
    tca_summary          fills_tca 30d, paper and live NEVER averaged together
    goal                 the [G] block from the running bot's GET /api/goal
    errors               one line per section that could not be measured

HONESTY RULES (all enforced by construction)
--------------------------------------------
* Read-only: SELECT only, over DATABASE_URL from the environment. No table is
  written, no file is written, nothing is POSTed. The DSN VALUE is never
  printed (a failure prints the exception class and message only after the
  DSN is scrubbed out of it).
* Never raises: every section runs under its own guard; a section that fails
  becomes null and its reason lands in "errors". Exit code is 0 either way —
  the document IS the report, and a partial document beats none.
* No guesses: a value that could not be measured is null / "unknown", never
  0. Sample sizes ride along with every aggregate.
* learning_report's table builders are IMPORTED, not re-implemented, so this
  pack can never disagree with the report the owner reads.
* The owner's hand-trade table is never read here (research side).
* No order or private-endpoint symbol exists in this file (test_hypothesis_safety).
"""
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

import learning_report as lr

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)
WEEKS_BACK = 8
DAYS_30 = 30 * 86400
HORIZONS = ("fwd6", "fwd24", "fwd48", "fwd168")

# Cost-model NAMES per entrant kind (the constants live in autopilot.py; the
# names are code facts, the numbers are not re-derived here).
COST_MODEL_BY_KIND = {
    None: "ROUND_TRIP_COST_PCT (intraday spot round trip)",
    "price": "ROUND_TRIP_COST_PCT (intraday spot round trip)",
    "trend": "CF_SPOT_RT (2 x (KRAKEN_FEE + SLIPPAGE), taker both legs)",
    "carry": "CARRY_RT_4LEG (2 spot legs + 2 perp legs, taker + slippage)",
    "switch": "CF_SPOT_RT for the trend leg, CARRY_RT_4LEG for the carry leg",
}


def _scrub(msg):
    """Strip anything that looks like a DSN or credential from an error text."""
    s = str(msg)
    s = re.sub(r"postgres(ql)?://\S+", "postgres://<scrubbed>", s)
    s = re.sub(r"password=\S+", "password=<scrubbed>", s)
    return s[:300]


def _iso_week(ts):
    try:
        d = datetime.fromtimestamp(float(ts), tz=timezone.utc).isocalendar()
        return f"{d[0]}-W{d[1]:02d}"
    except Exception:
        return "unknown"


def _fnum(v):
    """JSON-safe float or None (NaN/inf become null, Decimal -> float)."""
    if v is None:
        return None
    try:
        f = float(v)
    except Exception:
        return None
    return f if math.isfinite(f) else None


def fetch(conn, sql, cols, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── sections (each: conn -> value; raising is fine, the runner guards) ──────
def sec_regime_table(conn):
    return lr.regime_table(fetch(conn, lr.RESOLVED_SQL, lr.RESOLVED_COLS))


def sec_gate_table(conn):
    return lr.gate_table(fetch(conn, lr.RESOLVED_SQL, lr.RESOLVED_COLS))


def sec_spread_map_summary(conn):
    spreads = fetch(conn, lr.SPREAD_SQL, lr.SPREAD_COLS)
    smap = lr.spread_map(spreads)
    b6 = lr.spread_map_6h(spreads)
    seen = {(r["pair"], int(r["hour"]) % 24) for r in spreads
            if r.get("pair") is not None and r.get("hour") is not None}
    pairs = {}
    for pair, hours in smap.items():
        worst = max(hours.items(), key=lambda kv: kv[1]["p75_pct"])
        best = min(hours.items(), key=lambda kv: kv[1]["median_pct"])
        pairs[pair] = {
            "hours_mapped": len(hours),
            "widest_p75": {"hour": worst[0], "p75_pct": worst[1]["p75_pct"], "n": worst[1]["n"]},
            "tightest_median": {"hour": best[0], "median_pct": best[1]["median_pct"], "n": best[1]["n"]},
        }
    return {"rows_with_spread": len(spreads),
            "cells_kept": sum(len(h) for h in smap.values()),
            "cells_seen": len(seen),
            "min_n_per_cell": lr.MIN_N_SPREAD,
            "buckets6h_cells": sum(len(h) for h in b6.values()),
            "pairs": pairs}


def sec_rejects_by_gate_week(conn):
    since = time.time() - WEEKS_BACK * 7 * 86400
    eng = fetch(conn, "SELECT gate, ts FROM engine_rejects WHERE ts >= %s",
                ("gate", "ts"), (since,))
    sha = fetch(conn, "SELECT rejected_by, ts FROM shadow_signals "
                      "WHERE ts >= %s AND rejected_by IS NOT NULL AND rejected_by <> ''",
                ("gate", "ts"), (since,))
    out = {"weeks_back": WEEKS_BACK, "engine": {}, "shadow": {}}
    for key, rows in (("engine", eng), ("shadow", sha)):
        for r in rows:
            wk = _iso_week(r["ts"])
            g = str(r.get("gate") or "unknown")
            out[key].setdefault(wk, {})
            out[key][wk][g] = out[key][wk].get(g, 0) + 1
    out["engine_rows"] = len(eng)
    out["shadow_rows"] = len(sha)
    return out


def sec_shadow_counts(conn):
    cols = ("total", "taken", "pending", "oldest_ts", "newest_ts", "last_7d") + HORIZONS
    sql = ("SELECT COUNT(*), "
           "SUM(CASE WHEN taken=1 THEN 1 ELSE 0 END), "
           "SUM(CASE WHEN fwd_done=0 THEN 1 ELSE 0 END), "
           "MIN(ts), MAX(ts), "
           "SUM(CASE WHEN ts >= %s THEN 1 ELSE 0 END), "
           + ", ".join(f"SUM(CASE WHEN {h} IS NOT NULL THEN 1 ELSE 0 END)" for h in HORIZONS)
           + " FROM shadow_signals")
    row = fetch(conn, sql, cols, (time.time() - 7 * 86400,))
    if not row:
        return None
    r = row[0]
    out = {"total": int(r["total"] or 0), "taken": int(r["taken"] or 0),
           "pending_forward": int(r["pending"] or 0),
           "last_7d": int(r["last_7d"] or 0),
           "oldest_ts": _fnum(r["oldest_ts"]), "newest_ts": _fnum(r["newest_ts"]),
           "resolved_by_horizon": {h: int(r[h] or 0) for h in HORIZONS}}
    # readiness: a horizon is "ready" for a scorer only where its forward
    # return exists; rows younger than the horizon can never be resolved yet.
    out["readiness"] = {h: (out["resolved_by_horizon"][h] / out["total"]
                            if out["total"] else None) for h in HORIZONS}
    return out


def _read_autopilot_state(conn):
    """bot_state id=2 (the autopilot's mirror) first, the data-volume json second."""
    src = None
    data = None
    try:
        rows = fetch(conn, "SELECT data FROM bot_state WHERE id = 2", ("data",))
        if rows and rows[0]["data"]:
            d = rows[0]["data"]
            data = d if isinstance(d, dict) else json.loads(d)
            src = "bot_state id=2"
    except Exception:
        data = None
    if data is None:
        p = os.path.join(DATA_DIR, "autopilot_state.json")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        src = "autopilot_state.json"
    return data, src


def _reason_code(reason):
    r = str(reason or "")
    if "deflated" in r and "PSR" in r:
        return "psr_below_kill_bar"
    if "MinTRL" in r:
        return "past_mintrl"
    return "unknown"


def _sr0_from_reason(reason):
    m = re.search(r"SR0 hurdle ([-+]?[0-9.]+)", str(reason or ""))
    return _fnum(m.group(1)) if m else None


def sec_graveyard(conn):
    state, src = _read_autopilot_state(conn)
    killed = state.get("killed") if isinstance(state, dict) else None
    out = []
    for cid, k in (killed or {}).items():
        k = k if isinstance(k, dict) else {}
        cfg = k.get("config") if isinstance(k.get("config"), dict) else {}
        cf = cfg.get("cf") if isinstance(cfg.get("cf"), dict) else {}
        fs = k.get("final_score") if isinstance(k.get("final_score"), dict) else {}
        kind = cfg.get("kind")
        family = cfg.get("family") or (kind if kind in ("trend", "carry") else "unknown")
        prereg = cfg.get("prereg") if isinstance(cfg.get("prereg"), dict) else {}
        out.append({
            "id": str(cid),
            "family": family,
            "horizon": cf.get("horizon") or "unknown",
            "cost_model": cfg.get("cost_model") or prereg.get("cost_model")
                          or COST_MODEL_BY_KIND.get(kind, "unknown"),
            "reason_code": _reason_code(k.get("reason")),
            "reason": str(k.get("reason") or "")[:300],
            "sr": _fnum(fs.get("sr")),
            "sr0": _sr0_from_reason(k.get("reason")),
            "dsr": _fnum(fs.get("dsr")),
            "n": fs.get("trades_n"),
            "killed_ts": _fnum(k.get("ts")),
        })
    out.sort(key=lambda e: e["killed_ts"] or 0)
    return {"source": src, "trials_count": (state or {}).get("trials_count"),
            "entries": out}


def sec_funding_summary(conn):
    since = time.time() - DAYS_30
    rows = fetch(conn, "SELECT venue, symbol, AVG(rate), COUNT(*), MIN(ts), MAX(ts), "
                       "SUM(CASE WHEN rate > 0 THEN 1 ELSE 0 END) "
                       "FROM funding_rates WHERE ts >= %s AND rate IS NOT NULL "
                       "GROUP BY venue, symbol",
                 ("venue", "symbol", "mean", "n", "from_ts", "to_ts", "n_pos"), (since,))
    out = {}
    for r in rows:
        mean = _fnum(r["mean"])
        out[str(r["symbol"])] = {
            "venue": r["venue"],
            "window_days": 30,
            "n_hours": int(r["n"] or 0),
            "mean_hourly": mean,
            "mean_annualized": (mean * 24 * 365) if mean is not None else None,
            "sign": ("positive" if mean > 0 else "negative" if mean < 0 else "zero")
                    if mean is not None else "unknown",
            "share_positive": (int(r["n_pos"] or 0) / int(r["n"])) if r["n"] else None,
            "from_ts": _fnum(r["from_ts"]), "to_ts": _fnum(r["to_ts"]),
        }
    return out


def sec_tca_summary(conn):
    since = time.time() - DAYS_30
    rows = fetch(conn, "SELECT is_paper, shortfall_bps, maker, pair FROM fills_tca "
                       "WHERE ts >= %s AND shortfall_bps IS NOT NULL",
                 ("is_paper", "shortfall_bps", "maker", "pair"), (since,))
    out = {"window_days": 30, "paper": None, "live": None,
           "note": "paper rows are MODELED fills; live rows are market measurements — never averaged together"}
    for label, flag in (("paper", True), ("live", False)):
        vals = [float(r["shortfall_bps"]) for r in rows if bool(r["is_paper"]) is flag]
        if not vals:
            out[label] = {"n": 0}
            continue
        out[label] = {"n": len(vals),
                      "mean_bps": statistics.fmean(vals),
                      "median_bps": statistics.median(vals),
                      "p75_bps": lr.percentile(vals, 0.75),
                      "maker_share": (sum(1 for r in rows if bool(r["is_paper"]) is flag and r["maker"])
                                      / len(vals))}
    return out


def sec_goal(_conn=None):
    port = int(os.environ.get("PORT", 8080))
    url = f"http://127.0.0.1:{port}/api/goal"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:      # 404-safe body from /api/goal
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"goal": None, "reason": f"HTTP {e.code}"}
    goal = body.get("goal") if isinstance(body, dict) else None
    if not isinstance(goal, dict):
        raise RuntimeError(f"/api/goal: {body.get('reason') if isinstance(body, dict) else 'bad body'}")
    return goal


SECTIONS = (
    ("regime_table", sec_regime_table),
    ("gate_table", sec_gate_table),
    ("spread_map_summary", sec_spread_map_summary),
    ("rejects_by_gate_week", sec_rejects_by_gate_week),
    ("shadow_counts", sec_shadow_counts),
    ("graveyard", sec_graveyard),
    ("funding_summary", sec_funding_summary),
    ("tca_summary", sec_tca_summary),
    ("goal", sec_goal),
)


def _connect():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None, "no DATABASE_URL in the environment (run inside the bot container)"
    try:
        import psycopg2
    except ImportError:
        return None, "psycopg2 not installed"
    try:
        conn = psycopg2.connect(dsn, connect_timeout=10)
        conn.autocommit = True      # no transaction to leave open on a read-only run
        return conn, None
    except Exception as e:
        return None, f"connect failed: {type(e).__name__}: {_scrub(e)}"


def build_pack(conn, sections=SECTIONS, conn_error=None):
    """Assemble the document. Every section is guarded; NOTHING here raises."""
    now = time.time()
    doc = {"generated": now,
           "generated_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
           "errors": []}
    if conn_error:
        doc["errors"].append(f"db: {conn_error}")
    for name, fn in sections:
        try:
            if conn is None and name != "goal":
                raise RuntimeError("no database connection")
            doc[name] = fn(conn)
        except Exception as e:
            doc[name] = None
            doc["errors"].append(f"{name}: {type(e).__name__}: {_scrub(e)}")
    return doc


def _json_default(o):
    try:
        return float(o)          # Decimal from psycopg2 aggregates
    except Exception:
        return str(o)


def main(argv=None):
    conn, err = _connect()
    try:
        doc = build_pack(conn, conn_error=err)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    try:
        sys.stdout.write(json.dumps(doc, indent=1, sort_keys=True, default=_json_default))
        sys.stdout.write("\n")
    except Exception as e:       # last-resort: still ONE valid JSON document
        sys.stdout.write(json.dumps({"generated": time.time(),
                                     "errors": [f"serialize: {type(e).__name__}: {_scrub(e)}"]}))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
