#!/usr/bin/env python3
"""Was the stale exit premature, or was it right?

84 of 136 closed trades ended as "stale exit" -- the bot closing a position
that had not moved one ATR in STALE_EXIT_MINS. The obvious reading is that it
is killing trades before they can work. The book says otherwise:

    stale exit     84 trades   -0.0698$ per trade
    trailing stop  41 trades   -0.1486$ per trade   (median hold: 9 minutes)

The stale exit is the CHEAPEST way these trades end, so simply loosening it
pushes trades into an exit that costs twice as much. That makes "let them run
longer" a change that has to be justified with evidence, not assumed.

This asks the market directly: after each stale exit actually taken, what did
the price do next, in the direction the trade was betting on? Real Kraken
candles, real exit timestamps.

  - forward return at +1/+3/+6/+12/+24 hours, in the trade's direction
  - and a hold-to-resolution simulation: walking the bars after the exit with
    the bot's own stop and target distances, would holding have paid?

Usage:
    python analyze_stale_exit.py                      # needs paper_state.json
    python analyze_stale_exit.py --reason "trailing stop"
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.kraken.com/0/public"
CACHE = "ohlc_cache"


def fetch_ohlc(pair, interval=60):
    """~720 candles of OHLC. Cached, because this is called once per pair."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{pair}_{interval}.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 3600:
        with open(path) as f:
            return json.load(f)
    url = f"{BASE}/OHLC?" + urllib.parse.urlencode({"pair": pair, "interval": interval})
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        print(f"  {pair}: fetch failed ({e})", file=sys.stderr)
        return []
    if d.get("error"):
        return []
    key = next(k for k in d["result"] if k != "last")
    rows = [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
             "l": float(c[3]), "c": float(c[4])} for c in d["result"][key]]
    with open(path, "w") as f:
        json.dump(rows, f)
    time.sleep(1.1)
    return rows


def fwd_return(bars, i, n, side):
    """Return over n bars from bar i, signed so positive = the trade was right."""
    j = i + n
    if j >= len(bars):
        return None
    r = (bars[j]["c"] - bars[i]["c"]) / bars[i]["c"]
    return -r if side == "SHORT" else r


def hold_to_resolution(bars, i, side, stop_pct, target_pct, max_bars=24):
    """Walk forward from bar i. Returns the % move at whichever level hit first,
    or the close at max_bars. Uses H/L so an intrabar touch counts, and checks
    the STOP first on every bar -- assuming the target won on a bar that spans
    both is the classic way a simulation flatters itself."""
    entry = bars[i]["c"]
    for k in range(i + 1, min(i + 1 + max_bars, len(bars))):
        b = bars[k]
        if side == "SHORT":
            if b["h"] >= entry * (1 + stop_pct):
                return -stop_pct
            if b["l"] <= entry * (1 - target_pct):
                return target_pct
        else:
            if b["l"] <= entry * (1 - stop_pct):
                return -stop_pct
            if b["h"] >= entry * (1 + target_pct):
                return target_pct
    j = min(i + max_bars, len(bars) - 1)
    if j <= i:
        return None
    r = (bars[j]["c"] - entry) / entry
    return -r if side == "SHORT" else r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="paper_state.json")
    ap.add_argument("--reason", default="stale exit")
    ap.add_argument("--cost", type=float, default=0.0052)
    ap.add_argument("--stop", type=float, default=0.0105, help="stop as a fraction")
    ap.add_argument("--target", type=float, default=0.0230)
    args = ap.parse_args()

    try:
        state = json.load(open(args.state, encoding="utf-8"))
    except OSError:
        print(f"no {args.state} — pull one:\n"
              f"  scp noluv@10.0.0.88:~/cryptobot/data/paper_state.json .")
        return 2

    rows = [t for t in state.get("trades", [])
            if t.get("reason") == args.reason and t.get("pair") and t.get("ts")]
    if not rows:
        print(f"no trades with reason {args.reason!r}")
        return 2

    pairs = sorted({t["pair"] for t in rows})
    print(f"{len(rows)} '{args.reason}' trades across {len(pairs)} pairs")
    print("fetching candles...")
    ohlc = {p: fetch_ohlc(p) for p in pairs}

    horizons = [1, 3, 6, 12, 24]
    fwd = {h: [] for h in horizons}
    held = []
    matched = 0
    for t in rows:
        bars = ohlc.get(t["pair"]) or []
        if not bars:
            continue
        # The bar the exit landed in.
        i = None
        for k, b in enumerate(bars):
            if b["t"] <= t["ts"] < b["t"] + 3600:
                i = k
                break
        if i is None:
            continue
        matched += 1
        side = t.get("side", "LONG")
        for h in horizons:
            v = fwd_return(bars, i, h, side)
            if v is not None:
                fwd[h].append(v)
        v = hold_to_resolution(bars, i, side, args.stop, args.target)
        if v is not None:
            held.append(v)

    print(f"matched {matched}/{len(rows)} to candle data\n")
    if not matched:
        print("no overlap with available history — Kraken only serves ~720 candles")
        return 1

    print("=" * 72)
    print("  AFTER THE EXIT — price move in the direction the trade was betting")
    print("=" * 72)
    print(f"  {'horizon':>8s} {'n':>5s} {'median':>9s} {'mean':>9s} {'% positive':>11s}")
    print("  " + "-" * 48)
    for h in horizons:
        v = fwd[h]
        if not v:
            continue
        pos = sum(1 for x in v if x > 0) / len(v) * 100
        print(f"  {h:>6d}h {len(v):>5d} {statistics.median(v)*100:>+8.3f}% "
              f"{statistics.fmean(v)*100:>+8.3f}% {pos:>10.0f}%")

    print()
    print("=" * 72)
    print(f"  HOLDING INSTEAD — stop {args.stop*100:.2f}%, target {args.target*100:.2f}%, 24h cap")
    print("=" * 72)
    if held:
        m = statistics.fmean(held)
        wins = sum(1 for x in held if x > 0)
        net_hold = m - args.cost
        print(f"  n={len(held)}  mean outcome {m*100:+.3f}%  "
              f"({wins}/{len(held)} = {wins/len(held)*100:.0f}% positive)")
        print(f"  net of the {args.cost*100:.2f}% round trip: {net_hold*100:+.3f}% per trade")
        print()
        # What the stale exit actually collected, from the book itself.
        taken = [t["pnl"] for t in rows]
        print(f"  what the exit ACTUALLY paid: {statistics.fmean(taken):+.4f}$ per trade "
              f"({sum(taken):+.2f}$ over {len(taken)})")
        print()
        if net_hold > 0:
            print("  -> holding beats exiting on this sample. The stale exit is")
            print("     cutting trades that go on to work; it should be loosened.")
        else:
            print("  -> holding is WORSE than exiting. The stale exit is doing its")
            print("     job; loosening it would move money from the cheapest exit")
            print("     the bot has into a more expensive one.")
    # The average hides the question that matters. The exit tests abs(move), so
    # it fires on a trade drifting the RIGHT way just as readily as one drifting
    # the wrong way. Those are not the same trade and should not share a rule.
    print()
    print("=" * 72)
    print("  SPLIT BY WHETHER THE TRADE WAS WORKING WHEN IT WAS KILLED")
    print("=" * 72)
    up, down = [], []
    for t in rows:
        bars = ohlc.get(t["pair"]) or []
        if not bars:
            continue
        i = next((k for k, b in enumerate(bars)
                  if b["t"] <= t["ts"] < b["t"] + 3600), None)
        if i is None:
            continue
        mv = (t["exit"] - t["entry"]) / t["entry"]
        if t.get("side") == "SHORT":
            mv = -mv
        f6 = fwd_return(bars, i, 6, t.get("side", "LONG"))
        if f6 is None:
            continue
        (up if mv > 0 else down).append((mv, f6, t["pnl"]))

    print(f"  {'at exit':22s} {'n':>4s} {'move at exit':>13s} {'next 6h':>10s} {'% kept going':>13s}")
    print("  " + "-" * 66)
    for label, grp in (("trade was UP", up), ("trade was DOWN", down)):
        if not grp:
            continue
        mvs = [g[0] for g in grp]
        f6s = [g[1] for g in grp]
        kept = sum(1 for x in f6s if x > 0) / len(f6s) * 100
        print(f"  {label:22s} {len(grp):>4d} {statistics.median(mvs)*100:>+12.3f}% "
              f"{statistics.median(f6s)*100:>+9.3f}% {kept:>12.0f}%")
    if up and down:
        ku = sum(1 for _, f, _ in up if f > 0) / len(up) * 100
        kd = sum(1 for _, f, _ in down if f > 0) / len(down) * 100
        print()
        if ku > kd + 10:
            print(f"  -> a trade that was UP kept going {ku:.0f}% of the time vs {kd:.0f}% for one")
            print(f"     that was DOWN. Closing both under one abs(move) rule throws away")
            print(f"     the half that was working.")
        else:
            print(f"  -> up {ku:.0f}% vs down {kd:.0f}% — no meaningful difference. The move at")
            print(f"     exit does not predict what comes next, so one rule for both is fine.")

    print()
    print("  Caveats: forward returns are measured from the exit bar's close, and")
    print("  the hold simulation uses one stop/target pair for every trade because")
    print("  per-trade levels are not stored. Direction of the effect is the")
    print("  signal here, not the third decimal place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
