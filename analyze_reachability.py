#!/usr/bin/env python3
"""Was the target ever reachable in the time the trade was allowed?

The stale exit measures clean: after 3 hours of going nowhere the position has
no edge left in either direction (forward returns 41-57% positive at every
horizon, up-trades and down-trades indistinguishable). So it is not the bug --
it is the bot noticing, correctly and cheaply, that 61% of its trades never go
anywhere.

Which raises the real question. A trade is given STALE_EXIT_MINS to move one
ATR and MAX_TRADE_MINS overall to reach a target ~2.3% away. If a pair simply
does not travel 2.3% in that window, the target is decoration: the trade can
only ever end at the stop, the stale exit, or the time limit.

This measures, per pair, how far price actually travels in the time allowed,
and compares it to the target the bot sets.

Usage:
    python analyze_reachability.py
    python analyze_reachability.py --target 0.023 --hours 8
"""
import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_stale_exit import fetch_ohlc      # same cache, same fetcher


def best_excursion(bars, i, n, side):
    """Furthest FAVOURABLE move within n bars of bar i, as a fraction.
    Uses highs/lows: what the trade could have captured at its best moment."""
    entry = bars[i]["c"]
    best = 0.0
    for k in range(i + 1, min(i + 1 + n, len(bars))):
        if side == "SHORT":
            best = max(best, (entry - bars[k]["l"]) / entry)
        else:
            best = max(best, (bars[k]["h"] - entry) / entry)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="paper_state.json")
    ap.add_argument("--target", type=float, default=0.0230, help="median target distance")
    ap.add_argument("--hours", type=int, default=8, help="MAX_TRADE_MINS in hours")
    ap.add_argument("--stale-hours", type=int, default=3)
    args = ap.parse_args()

    try:
        state = json.load(open(args.state, encoding="utf-8"))
    except OSError:
        print(f"no {args.state} — scp it from the server first")
        return 2

    trades = [t for t in state.get("trades", []) if t.get("pair") and t.get("ts")]
    pairs = sorted({t["pair"] for t in trades})
    print(f"{len(trades)} trades across {len(pairs)} pairs")
    print("fetching candles...")
    ohlc = {p: fetch_ohlc(p) for p in pairs}

    # How often does each pair travel `target` in the window at all? Measured
    # over ALL its history, not just around the trades, so it is a property of
    # the pair rather than of the entries.
    print()
    print("=" * 76)
    print(f"  CAN THE PAIR EVEN TRAVEL {args.target*100:.2f}% IN {args.hours}h?")
    print("=" * 76)
    print(f"  {'pair':10s} {'bars':>6s} {'median {}h range'.format(args.hours):>16s} "
          f"{'p90':>8s} {'% of windows reaching target':>29s}")
    print("  " + "-" * 72)
    reach_by_pair = {}
    for p in pairs:
        bars = ohlc.get(p) or []
        if len(bars) < args.hours + 10:
            continue
        ranges, hits = [], 0
        n = 0
        for i in range(len(bars) - args.hours - 1):
            hi = max(b["h"] for b in bars[i + 1:i + 1 + args.hours])
            lo = min(b["l"] for b in bars[i + 1:i + 1 + args.hours])
            c = bars[i]["c"]
            rng = (hi - lo) / c
            ranges.append(rng)
            # A long needs the HIGH to clear the target; a short needs the low.
            if (hi - c) / c >= args.target or (c - lo) / c >= args.target:
                hits += 1
            n += 1
        if not n:
            continue
        pct = hits / n * 100
        reach_by_pair[p] = pct
        srt = sorted(ranges)
        print(f"  {p:10s} {n:>6d} {statistics.median(ranges)*100:>15.2f}% "
              f"{srt[int(len(srt)*0.9)]*100:>7.2f}% {pct:>28.0f}%")

    if reach_by_pair:
        vals = sorted(reach_by_pair.values())
        print()
        print(f"  across {len(reach_by_pair)} pairs: median {statistics.median(vals):.0f}% of "
              f"{args.hours}h windows contain a {args.target*100:.2f}% move")
        print(f"  worst pair {min(vals):.0f}%   best pair {max(vals):.0f}%")

    # And what the trades themselves actually had available to them.
    print()
    print("=" * 76)
    print("  WHAT EACH TRADE COULD HAVE CAPTURED AT BEST (from its entry bar)")
    print("=" * 76)
    groups = {}
    for t in trades:
        bars = ohlc.get(t["pair"]) or []
        if not bars:
            continue
        ts = t["ts"] - t.get("held_mins", 0) * 60          # entry time
        i = next((k for k, b in enumerate(bars)
                  if b["t"] <= ts < b["t"] + 3600), None)
        if i is None:
            continue
        mfe = best_excursion(bars, i, args.hours, t.get("side", "LONG"))
        groups.setdefault(t.get("reason", "?"), []).append(mfe)

    print(f"  {'exit reason':16s} {'n':>4s} {'median best move':>18s} "
          f"{'% that reached target':>22s}")
    print("  " + "-" * 64)
    for r, v in sorted(groups.items(), key=lambda x: -len(x[1])):
        reached = sum(1 for x in v if x >= args.target) / len(v) * 100
        print(f"  {r:16s} {len(v):>4d} {statistics.median(v)*100:>17.3f}% {reached:>21.0f}%")

    allv = [x for v in groups.values() for x in v]
    if allv:
        reached = sum(1 for x in allv if x >= args.target) / len(allv) * 100
        print()
        print(f"  Over ALL {len(allv)} trades, only {reached:.0f}% ever saw a "
              f"{args.target*100:.2f}% favourable move")
        print(f"  within {args.hours}h. Median best-case was "
              f"{statistics.median(allv)*100:.3f}%.")
        print()
        if reached < 35:
            print("  -> the target is out of reach for most entries. The trade cannot")
            print("     end at the target, so it ends at the stop, the stale exit or")
            print("     the clock -- which is exactly the book's exit mix. The fix")
            print("     belongs at ENTRY: do not take a trade whose target the pair")
            print("     does not reach in the time the trade is allowed.")
        else:
            print("  -> targets are broadly reachable; the losses are not explained")
            print("     by unreachable targets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
