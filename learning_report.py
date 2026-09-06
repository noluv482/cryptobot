#!/usr/bin/env python3
"""REGIME STEP-1 + honesty dashboard over the shadow book. Read-only.

Three honest tables from shadow_signals, nothing invented, every row carries
its n and small samples say so out loud:

  1. REGIME x SIDE — for each (regime, sig): n, mean fwd24 NET of the
     recorded live spread and honest round-trip fees, win rate with its
     Wilson 95% interval, and a verdict. The verdict vocabulary is fixed:
        'n too small'      below MIN_N_REGIME — a hypothesis, not a finding
        'no edge shown'    interval straddles or sits under 50% / mean <= 0
        'candidate (rig)'  Wilson LOWER bound > 50% AND mean net > 0 — still
                           only a candidate: promote nothing without the rig
                           (time split, second timeframe, de-overlap).
  2. GATES — same math grouped by TAKEN vs rejected_by: a rejecting gate is
     vindicated by NEGATIVE net numbers on its row.
  3. SPREAD MAP — per (pair, hour-of-day) median and p75 of the live bid-ask
     spread recorded at signal time, floors enforced (MIN_N_SPREAD=100 per
     cell). Written to spread_hours.json for the bot to read as a gate input.

spread_hours.json contract (the bot_server agent codes against this EXACTLY):
    {pair: {hour: {"median_pct": float, "p75_pct": float, "n": int}}}
    - hour is the string "0".."23" (JSON object keys are strings)
    - median_pct / p75_pct are FRACTIONS of price (0.003 = 0.30%), the same
      unit as shadow_signals.spread and bot_server._spread_pct
    - only cells with n >= 100 are present; absence means "not enough data",
      never "spread is fine"
    - top-level "_meta" key carries generated_ts, min_n, units — pairs never
      collide with it because no Kraken pair is named "_meta"
    - top-level "buckets6h" key (added 2026-09-05, BACKWARD COMPATIBLE — an
      older reader that skips only "_meta" would see a fake pair named
      "buckets6h", so bot_server's loader reserves both names):
          {pair: {"0"|"6"|"12"|"18": {"median_pct", "p75_pct", "n"}}}
      4 cells/pair keyed by bucket START hour (00-05, 06-11, 12-17, 18-23),
      same n >= 100 floor, same units. The bot consults a bucket ONLY when
      the exact (pair, hour) cell is absent — a coarser tier, never an
      override of a finer one.

Costs: net = signed(fwd24) - recorded spread - HONEST_FEES_RT (0.012 =
maker 0.4% entry + taker 0.8% exit, base tier; override env LR_FEES_RT).
Rows with no recorded spread use 0.0 and the table discloses how many.

CLI (dsn from --dsn or DATABASE_URL env — never hardcoded):
    python learning_report.py [--dsn postgres://...] [--out spread_hours.json]

Writes NOTHING to any table. Its only output is stdout + spread_hours.json.
"""
import json
import math
import os
import statistics
import sys
import time

HONEST_FEES_RT = float(os.environ.get("LR_FEES_RT", "0.012"))
MIN_N_REGIME   = 30      # below this a (regime, sig) row is 'n too small'
MIN_N_SPREAD   = 100     # below this a (pair, hour) spread cell is omitted
Z95            = 1.959963984540054

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "spread_hours.json")


# ── shared math ──────────────────────────────────────────────────────────────
def wilson(wins, n, z=Z95):
    """Wilson 95% interval for a binomial proportion -> (lo, hi)."""
    if n <= 0:
        return None, None
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def percentile(vals, q):
    """Linear-interpolated percentile of a non-empty list. q in [0,1]."""
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    return s[lo] if lo + 1 >= len(s) else s[lo] * (1 - frac) + s[lo + 1] * frac


def net_fwd24(sig, fwd24, spread, fees_rt=HONEST_FEES_RT):
    """Signed fwd24 minus recorded spread minus honest fees. None if
    unresolved. Missing spread counts 0.0 (callers disclose the count)."""
    if fwd24 is None:
        return None
    signed = -fwd24 if sig == "SELL" else fwd24
    return signed - (spread or 0.0) - fees_rt


# ── table builders (pure: rows in, structures out) ───────────────────────────
def regime_table(rows, fees_rt=HONEST_FEES_RT, min_n=MIN_N_REGIME):
    """rows: dicts with regime, sig, fwd24, spread.
    -> list of {regime, sig, n, n_spread_missing, mean_net, win_rate,
                wilson_lo, wilson_hi, verdict}, biggest n first."""
    groups = {}
    for r in rows:
        net = net_fwd24(r.get("sig"), r.get("fwd24"), r.get("spread"), fees_rt)
        if net is None:
            continue
        key = (r.get("regime") or "?", r.get("sig") or "?")
        g = groups.setdefault(key, {"nets": [], "miss": 0})
        g["nets"].append(net)
        g["miss"] += 1 if r.get("spread") is None else 0
    out = []
    for (regime, sig), g in groups.items():
        nets = g["nets"]
        n = len(nets)
        wins = sum(1 for v in nets if v > 0)
        lo, hi = wilson(wins, n)
        mean_net = statistics.fmean(nets)
        if n < min_n:
            verdict = "n too small"
        elif lo is not None and lo > 0.5 and mean_net > 0:
            verdict = "candidate (rig)"
        else:
            verdict = "no edge shown"
        out.append({"regime": regime, "sig": sig, "n": n,
                    "n_spread_missing": g["miss"], "mean_net": mean_net,
                    "win_rate": wins / n, "wilson_lo": lo, "wilson_hi": hi,
                    "verdict": verdict})
    out.sort(key=lambda r: -r["n"])
    return out


def gate_table(rows, fees_rt=HONEST_FEES_RT, min_n=MIN_N_REGIME):
    """Same math grouped by TAKEN / rejected_by. A rejecting gate earns its
    keep only when its row's mean net is NEGATIVE."""
    groups = {}
    for r in rows:
        net = net_fwd24(r.get("sig"), r.get("fwd24"), r.get("spread"), fees_rt)
        if net is None:
            continue
        key = "TAKEN" if r.get("taken") else (r.get("rejected_by") or "other")
        groups.setdefault(key, []).append(net)
    out = []
    for key, nets in groups.items():
        n = len(nets)
        wins = sum(1 for v in nets if v > 0)
        lo, hi = wilson(wins, n)
        out.append({"group": key, "n": n,
                    "mean_net": statistics.fmean(nets),
                    "win_rate": wins / n, "wilson_lo": lo, "wilson_hi": hi,
                    "verdict": "n too small" if n < min_n else ""})
    out.sort(key=lambda r: -r["n"])
    return out


def _spread_cells(rows, key_fn, min_n):
    """Shared builder: group spreads by (pair, key_fn(hour)), emit
    {median_pct, p75_pct, n} per cell, OMIT cells under min_n."""
    cells = {}
    for r in rows:
        pair, hour, sp = r.get("pair"), r.get("hour"), r.get("spread")
        if pair is None or hour is None or sp is None:
            continue
        cells.setdefault(pair, {}).setdefault(key_fn(int(hour) % 24), []).append(float(sp))
    out = {}
    for pair, keys in cells.items():
        for key, vals in keys.items():
            if len(vals) < min_n:
                continue
            out.setdefault(pair, {})[str(key)] = {
                "median_pct": percentile(vals, 0.50),
                "p75_pct": percentile(vals, 0.75),
                "n": len(vals)}
    return out


def spread_map(rows, min_n=MIN_N_SPREAD):
    """rows: dicts with pair, hour, spread (fraction of price).
    -> {pair: {"0".."23": {median_pct, p75_pct, n}}} — cells under min_n are
    OMITTED, not zero-filled: absence means 'not enough data'."""
    return _spread_cells(rows, lambda h: h, min_n)


BUCKET6H_STARTS = ("0", "6", "12", "18")


def spread_map_6h(rows, min_n=MIN_N_SPREAD):
    """Coarser tier: -> {pair: {"0"|"6"|"12"|"18": {median_pct, p75_pct, n}}},
    keyed by 6h-bucket START hour, same floor, same omission rule. Four
    cells per pair reach n >= 100 six times sooner than 24 hour cells do —
    the bot uses one only when the exact (pair, hour) cell is absent."""
    return _spread_cells(rows, lambda h: (h // 6) * 6, min_n)


# ── DB glue (read-only) ──────────────────────────────────────────────────────
RESOLVED_SQL = """
    SELECT regime, sig, fwd24, spread, taken, rejected_by
    FROM shadow_signals
    WHERE fwd_done=1 AND fwd24 IS NOT NULL AND sig IN ('BUY','SELL')
"""
RESOLVED_COLS = ("regime", "sig", "fwd24", "spread", "taken", "rejected_by")

SPREAD_SQL = """
    SELECT pair, hour, spread FROM shadow_signals WHERE spread IS NOT NULL
"""
SPREAD_COLS = ("pair", "hour", "spread")


def fetch(conn, sql, cols):
    with conn.cursor() as cur:
        cur.execute(sql)
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def write_spread_json(smap, path, buckets6h=None):
    """Contract file. Pair keys at top level (unchanged shape); "_meta" and
    "buckets6h" are the only reserved top-level names."""
    doc = {"_meta": {"generated_ts": time.time(), "min_n": MIN_N_SPREAD,
                     "units": "fraction of price (0.003 = 0.30%)",
                     "buckets6h": "per-pair 6h buckets keyed by start hour; "
                                  "consulted only when the exact hour cell is absent"},
           "buckets6h": dict(buckets6h or {})}
    doc.update({k: v for k, v in smap.items() if k not in ("_meta", "buckets6h")})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ── report ───────────────────────────────────────────────────────────────────
def _pct(v):
    return "      —" if v is None else f"{v*100:+7.3f}%"


def run_report(conn, out_path=DEFAULT_OUT):
    resolved = fetch(conn, RESOLVED_SQL, RESOLVED_COLS)
    spreads = fetch(conn, SPREAD_SQL, SPREAD_COLS)

    print("=" * 78)
    print(f"  REGIME x SIDE — {len(resolved)} resolved BUY/SELL shadow rows, "
          f"net = fwd24 - spread - {HONEST_FEES_RT*100:.2f}% fees")
    print("=" * 78)
    rt = regime_table(resolved)
    if rt:
        miss = sum(r["n_spread_missing"] for r in rt)
        print(f"  {'regime':10s} {'sig':4s} {'n':>5s} {'mean net':>9s} "
              f"{'win%':>6s} {'wilson95':>15s}  verdict")
        print("  " + "-" * 68)
        for r in rt:
            wl = (f"[{r['wilson_lo']*100:4.1f},{r['wilson_hi']*100:5.1f}]%"
                  if r["wilson_lo"] is not None else "      —")
            print(f"  {r['regime']:10s} {r['sig']:4s} {r['n']:>5d} "
                  f"{_pct(r['mean_net']):>9s} {r['win_rate']*100:>5.1f}% "
                  f"{wl:>15s}  {r['verdict']}")
        if miss:
            print(f"  ({miss} rows had no recorded spread — costed at 0.0, "
                  "so their nets are OPTIMISTIC)")
        print("  'candidate (rig)' promotes nothing: it earns the rig "
              "(time split, 2nd timeframe, de-overlap), not a settings change.")
    else:
        print("  no resolved rows yet — rows gain forward returns 49h after "
              "logging")

    print()
    print("=" * 78)
    print("  GATES — a rejecting gate is vindicated by NEGATIVE net on its row")
    print("=" * 78)
    gt = gate_table(resolved)
    if gt:
        print(f"  {'group':16s} {'n':>5s} {'mean net':>9s} {'win%':>6s}  note")
        print("  " + "-" * 52)
        for r in gt:
            print(f"  {r['group']:16s} {r['n']:>5d} {_pct(r['mean_net']):>9s} "
                  f"{r['win_rate']*100:>5.1f}%  {r['verdict']}")
    else:
        print("  nothing resolved yet")

    print()
    print("=" * 78)
    print(f"  SPREAD MAP — median/p75 live spread per (pair, hour), "
          f"cells need n >= {MIN_N_SPREAD}")
    print("=" * 78)
    smap = spread_map(spreads)
    kept = sum(len(h) for h in smap.values())
    total_cells = len({(r['pair'], int(r['hour']) % 24) for r in spreads
                       if r.get('pair') is not None and r.get('hour') is not None})
    print(f"  {len(spreads)} rows with spread -> {kept} cells kept of "
          f"{total_cells} seen (rest under the n={MIN_N_SPREAD} floor)")
    for pair in sorted(smap):
        hours = smap[pair]
        worst = max(hours.items(), key=lambda kv: kv[1]["p75_pct"])
        best = min(hours.items(), key=lambda kv: kv[1]["median_pct"])
        print(f"  {pair:12s} {len(hours):>2d} hours mapped   "
              f"widest p75 {worst[1]['p75_pct']*100:.3f}% @ {worst[0]:>2s}h   "
              f"tightest median {best[1]['median_pct']*100:.3f}% @ {best[0]:>2s}h")
    b6 = spread_map_6h(spreads)
    kept6 = sum(len(h) for h in b6.values())
    print(f"  6h-bucket tier: {kept6} cells kept across {len(b6)} pairs "
          f"(4 cells/pair max, same n >= {MIN_N_SPREAD} floor; used only "
          "where the exact hour cell is absent)")
    write_spread_json(smap, out_path, buckets6h=b6)
    print(f"  -> {out_path}")
    print()
    print("  Small n = hypothesis, not finding. Promote nothing without the rig.")
    return 0


def _connect(dsn):
    if not dsn:
        print("no dsn: pass --dsn or set DATABASE_URL "
              "(never hardcoded here on purpose)")
        return None
    import psycopg2
    return psycopg2.connect(dsn)


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = list(argv)
    dsn = os.environ.get("DATABASE_URL")
    out = DEFAULT_OUT
    if "--dsn" in args:
        i = args.index("--dsn")
        dsn = args[i + 1]
        del args[i:i + 2]
    if "--out" in args:
        i = args.index("--out")
        out = args[i + 1]
        del args[i:i + 2]
    conn = _connect(dsn)
    if conn is None:
        return 2
    try:
        return run_report(conn, out)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
