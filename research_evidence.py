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
    breadth              what the rig can SEE: funding symbols archived (n +
                         earliest/latest per symbol), the funding correlation
                         summary measured from our OWN funding_rates table
                         (mean off-diagonal + n_eff), candle coverage by
                         interval_m, and a 'blockers' list naming every family
                         with zero decisions and its DETECTED cause
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


# ── breadth: what the rig can SEE (archived symbols, candle coverage, blockers) ─
# Every number below is measured from our OWN contract tables (funding_rates,
# candles) and from the entrants' own recorded scores. Nothing is pulled from a
# venue here, nothing is re-derived from prose, and no threshold is invented:
# the carry hurdle used in hurdle_check is the value the ENTRANT recorded
# (score["hurdle_ann"]), never a constant retyped in this file.
CARRY_MIN_7D_HOURS = 120       # window-completeness floor the scorer records
SECS_PER_WEEK = 7 * 86400
COVERAGE_INTERVALS = (60, 1440, 10080)   # hourly / daily / weekly


def _q(vals, p):
    """Linear-interpolated quantile of a non-empty list, else None."""
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    k = (len(s) - 1) * float(p)
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def _funding_rows(conn):
    """{symbol: {venue, hours: {ts: rate}}} — deduped by ts, first venue wins,
    exactly like the scorer's own _fetch_funding. Garbage rows are skipped,
    never guessed at."""
    rows = fetch(conn, "SELECT venue, symbol, ts, rate FROM funding_rates "
                       "WHERE rate IS NOT NULL ORDER BY symbol, ts",
                 ("venue", "symbol", "ts", "rate"))
    out = {}
    for r in rows:
        try:
            sym = str(r["symbol"])
            ts = int(float(r["ts"]))
            rate = float(r["rate"])
            if not math.isfinite(rate):
                continue
        except Exception:
            continue
        e = out.setdefault(sym, {"venue": r.get("venue"), "hours": {}})
        e["hours"].setdefault(ts, rate)
    return out


def _trailing7d_ann(hours):
    """[(day_ts, annualized trailing-7d mean)] replaying the scorer's own loop:
    one UTC day, window [day-7d, day), >= CARRY_MIN_7D_HOURS of 168 rates."""
    ts_sorted = sorted(hours)
    if len(ts_sorted) < 2:
        return []
    first_day = (ts_sorted[0] // 86400) * 86400
    last_day = (ts_sorted[-1] // 86400) * 86400
    out = []
    day = first_day + 86400
    while day < last_day:
        w = [hours[t] for t in ts_sorted if day - SECS_PER_WEEK <= t < day]
        if len(w) >= CARRY_MIN_7D_HOURS:
            out.append((day, statistics.fmean(w) * 24.0 * 365.0))
        day += 86400
    return out


def _funding_breadth(funding):
    """(per-symbol summary, {symbol: [trailing-7d annualized, ...]}) — the raw
    series rides along so hurdle_check counts DAYS above a hurdle instead of
    re-deriving the window a second time."""
    per, series = {}, {}
    for sym, e in sorted(funding.items()):
        hours = e["hours"]
        if not hours:
            continue
        lo, hi = min(hours), max(hours)
        span_h = (hi - lo) / 3600.0
        expected = int(span_h) + 1
        vals = list(hours.values())
        t7 = [a for _, a in _trailing7d_ann(hours)]
        series[sym] = t7
        mean_h = statistics.fmean(vals)
        per[sym] = {
            "venue": e["venue"],
            "n_hours": len(hours),
            "earliest_ts": float(lo), "latest_ts": float(hi),
            "earliest_iso": datetime.fromtimestamp(lo, tz=timezone.utc).isoformat(),
            "latest_iso": datetime.fromtimestamp(hi, tz=timezone.utc).isoformat(),
            "span_days": span_h / 24.0,
            "expected_hours": expected,
            "gap_share": (1.0 - len(hours) / expected) if expected > 0 else None,
            "mean_hourly": _fnum(mean_h),
            "mean_annualized": _fnum(mean_h * 24 * 365),
            "share_positive_hours": sum(1 for v in vals if v > 0) / len(vals),
            "trailing7d_ann": {
                "n_days": len(t7),
                "min": _fnum(min(t7)) if t7 else None,
                "p25": _fnum(_q(t7, .25)), "median": _fnum(_q(t7, .50)),
                "p75": _fnum(_q(t7, .75)), "p95": _fnum(_q(t7, .95)),
                "max": _fnum(max(t7)) if t7 else None,
                "min_window_hours": CARRY_MIN_7D_HOURS,
            },
        }
    return per, series


def _funding_correlation(funding):
    """Mean off-diagonal Pearson correlation of HOURLY funding over the
    timestamps every archived symbol shares, and the effective number of
    independent streams n_eff = k / (1 + (k-1)*rbar). Fewer than 2 symbols or
    fewer than 2 aligned hours -> nulls with a reason, never a made-up 1.0."""
    syms = sorted(funding)
    k = len(syms)
    base = {"n_symbols": k, "n_aligned_hours": 0, "mean_offdiag": None,
            "n_eff": None, "pairs": {}, "reason": None}
    if k < 2:
        base["reason"] = "fewer than 2 symbols archived"
        return base
    common = set(funding[syms[0]]["hours"])
    for s in syms[1:]:
        common &= set(funding[s]["hours"])
    common = sorted(common)
    base["n_aligned_hours"] = len(common)
    if len(common) < 2:
        base["reason"] = "fewer than 2 timestamps shared by every symbol"
        return base
    off = []
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            xa = [funding[a]["hours"][t] for t in common]
            xb = [funding[b]["hours"][t] for t in common]
            try:
                c = _fnum(statistics.correlation(xa, xb))
            except Exception:
                c = None
            base["pairs"][f"{a}|{b}"] = c
            if c is not None:
                off.append(c)
    if not off:
        base["reason"] = "no pair had a defined correlation (constant series?)"
        return base
    rbar = statistics.fmean(off)
    base["mean_offdiag"] = _fnum(rbar)
    denom = 1.0 + (k - 1) * rbar
    base["n_eff"] = _fnum(k / denom) if denom > 0 else None
    if base["n_eff"] is None:
        base["reason"] = "1 + (k-1)*rbar <= 0 — n_eff undefined"
    return base


def _candle_coverage(conn):
    rows = fetch(conn, "SELECT interval_m, COUNT(DISTINCT pair), COUNT(*), "
                       "MIN(ts), MAX(ts) FROM candles GROUP BY interval_m",
                 ("interval_m", "pairs", "rows", "min_ts", "max_ts"))
    by_interval = {}
    for r in rows:
        try:
            iv = str(int(r["interval_m"]))
        except Exception:
            continue
        lo, hi = _fnum(r.get("min_ts")), _fnum(r.get("max_ts"))
        by_interval[iv] = {
            "pairs": int(r["pairs"] or 0), "rows": int(r["rows"] or 0),
            "earliest_ts": lo, "latest_ts": hi,
            "span_days": ((hi - lo) / 86400.0) if (lo is not None and hi is not None) else None,
        }
    per_pair = {}
    prows = fetch(conn, "SELECT pair, interval_m, COUNT(*), MIN(ts), MAX(ts) "
                        "FROM candles WHERE interval_m IN (60, 1440, 10080) "
                        "GROUP BY pair, interval_m",
                  ("pair", "interval_m", "n", "min_ts", "max_ts"))
    for r in prows:
        try:
            iv = str(int(r["interval_m"]))
            pair = str(r["pair"])
        except Exception:
            continue
        lo, hi = _fnum(r.get("min_ts")), _fnum(r.get("max_ts"))
        per_pair.setdefault(iv, {})[pair] = {
            "n": int(r["n"] or 0), "earliest_ts": lo, "latest_ts": hi,
            "span_days": ((hi - lo) / 86400.0) if (lo is not None and hi is not None) else None,
        }
    return {"by_interval": by_interval,
            "intervals_reported_per_pair": [str(i) for i in COVERAGE_INTERVALS],
            "by_pair": per_pair}


def _hurdle_check(funding_per_symbol, series, hurdles):
    """For every DISTINCT hurdle the entrants recorded, how the realized
    trailing-7d annualized funding of each archived symbol sits against it —
    including HOW MANY of its decision days actually cleared it. This
    DESCRIBES a hurdle against measured history; it never proposes changing
    one, and a hurdle is never re-derived here from a constant."""
    out = {}
    for h in sorted(set(hurdles)):
        per = {}
        for sym, f in funding_per_symbol.items():
            t7 = f.get("trailing7d_ann") or {}
            mx, med = t7.get("max"), t7.get("median")
            vals = series.get(sym) or []
            above = sum(1 for v in vals if v > h)
            per[sym] = {
                "n_days": int(t7.get("n_days") or 0),
                "max_trailing7d_ann": mx,
                "median_trailing7d_ann": med,
                "days_above_hurdle": above if vals else None,
                "share_days_above_hurdle": (above / len(vals)) if vals else None,
                "max_clears_hurdle": (mx > h) if (mx is not None) else None,
                "median_clears_hurdle": (med > h) if (med is not None) else None,
            }
        mx_c = [v["max_clears_hurdle"] for v in per.values() if v["max_clears_hurdle"] is not None]
        md_c = [v["median_clears_hurdle"] for v in per.values() if v["median_clears_hurdle"] is not None]
        out["%.6g" % h] = {"hurdle_ann": h, "symbols": per,
                           "any_symbol_max_clears": (any(mx_c) if mx_c else None),
                           "any_symbol_median_clears": (any(md_c) if md_c else None)}
    return out


def _detect_cause(status, vias, hurdle, funding_per_symbol, coverage, hurdle_check):
    """(cause_code, cause, evidence, would_unblock) from MEASURED inputs only.

    The order is: what the scorer itself said, then its own recorded hurdle
    against realized funding, then the shape of the entrant. Nothing here
    proposes loosening a gate — 'would_unblock' always asks for more evidence.
    """
    low = str(status or "").lower()
    if "funding history" in low:
        return ("no_funding_history",
                "the scorer reports it is waiting on funding history",
                {"symbols_archived": len(funding_per_symbol)},
                "funding_rates rows for the symbol the entrant reads")
    if "price history" in low:
        hourly = (coverage.get("by_pair") or {}).get("60") or {}
        spans = [v.get("span_days") for v in hourly.values()
                 if isinstance(v, dict) and v.get("span_days") is not None]
        wk = (coverage.get("by_pair") or {}).get("10080") or {}
        dl = (coverage.get("by_pair") or {}).get("1440") or {}
        ev = {
            "hourly_60_pairs": len(hourly),
            "hourly_60_max_span_days": max(spans) if spans else None,
            "daily_1440_pairs": ((coverage.get("by_interval") or {}).get("1440") or {}).get("pairs"),
            "daily_1440_max_rows_per_pair":
                max([v.get("n") or 0 for v in dl.values() if isinstance(v, dict)] or [0]),
            "weekly_10080_pairs": ((coverage.get("by_interval") or {}).get("10080") or {}).get("pairs"),
            "weekly_10080_max_rows_per_pair":
                max([v.get("n") or 0 for v in wk.values() if isinstance(v, dict)] or [0]),
        }
        return ("price_history_warm_up_not_met",
                "the scorer reports its warm-up is not met from the candle history it reads",
                ev,
                "longer stored history at the interval the scorer reads, or a scorer that "
                "reads the coarser intervals already archived")
    if hurdle is not None:
        hc = hurdle_check.get("%.6g" % hurdle) or {}
        syms = hc.get("symbols") or {}
        mx_c, md_c = hc.get("any_symbol_max_clears"), hc.get("any_symbol_median_clears")
        ev = {"hurdle_ann": hurdle, "symbols": syms,
              "note": "the scores do not record which symbol this entrant reads, so the "
                      "comparison is against EVERY archived symbol"}
        if mx_c is None:
            return ("hurdle_vs_funding_unmeasurable",
                    "no archived symbol had enough history to compare against the hurdle",
                    ev, "funding history long enough to form a trailing-7d window")
        if mx_c is False:
            return ("hurdle_above_realized_funding",
                    f"the entrant's own recorded hurdle ({hurdle:.4g} annualized) is above the "
                    f"highest trailing-7d funding ever realized by any archived symbol, so the "
                    f"entry condition can never fire on this universe",
                    ev,
                    "archiving symbols whose realized funding actually reaches the existing "
                    "hurdle — the hurdle itself is not to be lowered")
        best = max(((v.get("days_above_hurdle") or 0), s) for s, v in syms.items()) \
            if syms else (0, None)
        if md_c is not True:
            return ("hurdle_rarely_cleared",
                    f"the recorded hurdle ({hurdle:.4g} annualized) is above the MEDIAN "
                    f"trailing-7d funding of every archived symbol; the best any symbol managed "
                    f"is {best[0]} day(s) above it ({best[1]})",
                    ev,
                    "archiving symbols that clear the existing hurdle on more than a handful "
                    "of days — the hurdle itself is not to be lowered")
        return ("hurdle_cleared_but_no_decision_recorded",
                "the recorded hurdle is cleared on at least one archived symbol, yet no "
                "decision was recorded — this evidence does not name the cause",
                ev, None)
    if vias and set(vias) <= {"live"}:
        return ("live_entrant_never_fired",
                "a live-book entrant with no recorded status: its gates produced no signal "
                "in the window measured", {}, None)
    return ("unknown", None, {}, None)


def _blockers(scores, funding_per_symbol, coverage, hurdle_check):
    """(family rows, idle-entrant rows).

    A FAMILY row is emitted only when the family's entrants have produced zero
    decisions between them. That alone would hide the real situation whenever
    one member has decisions and the rest have never run, so every entrant with
    zero decisions ALSO gets its own row with its own detected cause. 'unknown'
    when nothing in the evidence names one — never a guess.
    """
    idle, fams = [], {}
    for cid, s in sorted((scores or {}).items()):
        if not isinstance(s, dict):
            continue
        fam = str(s.get("family") or "unknown")
        try:
            n = int(s.get("trades_n") or 0)
        except Exception:
            n = 0
        via = str(s.get("via")) if s.get("via") else None
        hv = _fnum(s.get("hurdle_ann"))
        st = str(s.get("status"))[:200] if s.get("status") else None
        e = fams.setdefault(fam, {"entrants": [], "decisions": 0, "idle": []})
        e["entrants"].append(str(cid))
        e["decisions"] += n
        if n > 0:
            continue
        code, cause, ev, unblock = _detect_cause(st, [via] if via else [], hv,
                                                 funding_per_symbol, coverage, hurdle_check)
        row = {"id": str(cid), "family": fam, "decisions": 0, "via": via,
               "cause_code": code, "cause": cause, "reported_status": st,
               "would_unblock": unblock, "evidence": ev}
        idle.append(row)
        e["idle"].append(row)

    out = []
    for fam, e in sorted(fams.items()):
        if e["decisions"] > 0:
            continue
        codes = sorted({r["cause_code"] for r in e["idle"]})
        if len(codes) == 1:
            r0 = e["idle"][0]
            code, cause = r0["cause_code"], r0["cause"]
            evidence, unblock = r0["evidence"], r0["would_unblock"]
        elif codes:
            code, cause = "multiple_causes", "its idle entrants are blocked for different reasons"
            evidence = {"per_entrant": {r["id"]: r["cause_code"] for r in e["idle"]}}
            unblock = None
        else:
            code, cause, evidence, unblock = "unknown", None, {}, None
        status = " ; ".join(f"{r['id']}: {r['reported_status']}"
                            for r in e["idle"] if r["reported_status"])
        out.append({"family": fam, "entrants": sorted(e["entrants"]),
                    "idle_entrants": sorted(r["id"] for r in e["idle"]),
                    "decisions": 0, "cause_code": code, "cause": cause,
                    "reported_status": status or None,
                    "via": sorted({r["via"] for r in e["idle"] if r["via"]}),
                    "would_unblock": unblock, "evidence": evidence})
    return out, idle


def sec_breadth(conn):
    """What the rig can SEE, and what is stopping each idle family.

    Sub-steps are guarded one by one: a failure inside becomes null/empty with a
    line in breadth["errors"], so partial breadth still ships. Nothing here
    proposes loosening a gate — a blocker row states the measured cause and what
    NEW EVIDENCE would clear it.
    """
    out = {"errors": []}

    failed = set()

    def _step(name, fn, default=None):
        try:
            return fn()
        except Exception as e:                      # noqa: BLE001
            failed.add(name)
            out["errors"].append(f"{name}: {type(e).__name__}: {_scrub(e)}")
            return default

    funding = _step("funding_rows", lambda: _funding_rows(conn), {}) or {}
    per_sym, t7_series = _step("funding_symbols", lambda: _funding_breadth(funding),
                               ({}, {})) or ({}, {})
    # A count is only a MEASUREMENT when the read behind it succeeded: if the
    # query failed, "0 symbols archived" would read as a measured zero. It is
    # null instead, and the reason is already in errors.
    out["funding"] = {
        "symbols_archived": (None if ("funding_rows" in failed or "funding_symbols" in failed)
                             else len(per_sym)),
        "per_symbol": per_sym,
    }
    out["funding_correlation"] = _step("funding_correlation",
                                       lambda: _funding_correlation(funding))
    empty_cov = {"by_interval": {}, "by_pair": {}}
    coverage = _step("candle_coverage", lambda: _candle_coverage(conn), empty_cov) or empty_cov
    # same rule: a coverage read that failed is null, not "no candles exist".
    out["candles"] = (dict(coverage, by_interval=None, by_pair=None,
                           measured=False)
                      if "candle_coverage" in failed
                      else dict(coverage, measured=True))

    def _scores():
        state, src = _read_autopilot_state(conn)
        out["scores_source"] = src
        s = state.get("scores") if isinstance(state, dict) else None
        return s if isinstance(s, dict) else {}

    scores = _step("scores", _scores, {}) or {}
    hurdles = [h for h in (_fnum(s.get("hurdle_ann")) for s in scores.values()
                           if isinstance(s, dict)) if h is not None]
    out["hurdle_check"] = _step("hurdle_check",
                                lambda: _hurdle_check(per_sym, t7_series, hurdles), {}) or {}
    fam_rows, idle_rows = _step("blockers",
                                lambda: _blockers(scores, per_sym, coverage,
                                                  out["hurdle_check"]), ([], [])) or ([], [])
    out["blockers"] = fam_rows
    out["idle_entrants"] = idle_rows
    out["families_scored"] = (None if "scores" in failed else
                              sorted({str(s.get("family") or "unknown")
                                      for s in scores.values() if isinstance(s, dict)}))
    out["blockers_measured"] = "scores" not in failed
    if "scores" in failed:
        # not "no blockers": the scores were unreadable, so nothing is known
        out["blockers"] = None
        out["idle_entrants"] = None
    return out


SECTIONS = (
    ("regime_table", sec_regime_table),
    ("gate_table", sec_gate_table),
    ("spread_map_summary", sec_spread_map_summary),
    ("rejects_by_gate_week", sec_rejects_by_gate_week),
    ("shadow_counts", sec_shadow_counts),
    ("graveyard", sec_graveyard),
    ("funding_summary", sec_funding_summary),
    ("breadth", sec_breadth),
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
