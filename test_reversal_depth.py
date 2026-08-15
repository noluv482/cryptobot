#!/usr/bin/env python3
"""The reversal signals survived the full-history battery out-of-sample. This
asks the only question left: does their real edge ever clear the cost of
trading it?

The full 15-candidate battery over 1.4M candles is too slow to finish in one
run, but the verdict already named the survivors — short-term reversal at
1/6/24-bar lookbacks all beat a direction-matched random entry out-of-sample
(t = +3.3 / +2.6 / +4.6 at horizon 6). So test ONLY those, across a ladder of
hold horizons, with non-overlapping windows (honest t) and an explicit cost
column. A statistically real edge that is smaller than the round trip is a
true fact about the market and a worthless trade.

    docker exec ... none needed — reads data/history CSVs locally
    python test_reversal_depth.py
"""
import math
import os
import statistics
import sys

import bot_server as bs
from find_signal import (load_history, sig_reversal_1, sig_reversal_6,
                         sig_reversal_24, sig_zscore_fade_48, fwd)

bs.log = lambda *a, **k: None

CANDS = {
    "reversal_1bar":  sig_reversal_1,
    "reversal_6bar":  sig_reversal_6,
    "reversal_24bar": sig_reversal_24,
    "zscore_fade_48": sig_zscore_fade_48,
}
HORIZONS = [6, 24, 48, 96]


def run(series, fn, hz, lo, hi):
    """NON-overlapping windows over each pair's [lo,hi) fraction. Returns
    (adjusted-return list, n) where adjusted = signal minus direction-matched
    baseline."""
    adj, nb = [], 0
    for c, h, l, v in series:
        n = len(c)
        a, b = int(n * lo), min(int(n * hi), n - hz - 1)
        # baseline: non-overlapping unconditional forward returns
        base = []
        t = max(a, 200)
        while t < b:
            f = fwd(c, t, hz, "BUY")
            if f is not None:
                base.append(f)
            t += hz
        if not base:
            continue
        bl = statistics.fmean(base)
        i = max(a, 200)
        while i < b:
            s = fn(c, h, l, v, i)
            if s in ("BUY", "SELL"):
                g = fwd(c, i, hz, s)
                if g is not None:
                    adj.append(g - bl if s == "BUY" else g + bl)
                    i += hz
                    continue
            i += 1
    return adj, len(adj)


def stats(adj):
    n = len(adj)
    if n < 30:
        return n, None, None
    m = statistics.fmean(adj)
    sd = statistics.pstdev(adj)
    if sd < 1e-12:
        return n, None, None
    return n, m, m / (sd / math.sqrt(n))


def main():
    pairs = [c["pair"] for c in bs.SCAN_UNIVERSE]
    series = [g for g in (load_history(p) for p in pairs) if g]
    cost = bs.ROUND_TRIP_COST_PCT
    print(f"{len(series)} pairs loaded · round-trip cost {cost*100:.2f}%")
    print("non-overlapping windows · first half ranks / second half judges\n")

    for name, fn in CANDS.items():
        print("=" * 74)
        print(f"  {name}")
        print("=" * 74)
        print(f"  {'hz':>4s} {'IS n':>6s} {'IS edge':>9s} {'IS t':>6s} "
              f"{'OOS n':>6s} {'OOS edge':>9s} {'OOS t':>6s} {'OOS xcost':>9s}")
        print("  " + "-" * 62)
        for hz in HORIZONS:
            ai, _ = run(series, fn, hz, 0.0, 0.5)
            ni, ei, ti = stats(ai)
            ao, _ = run(series, fn, hz, 0.5, 1.0)
            no, eo, to = stats(ao)
            if ei is None or eo is None:
                print(f"  {hz:>4d} {ni:>6d}  (too few)")
                continue
            xc = abs(eo) / cost
            flag = "" if (ei > 0) == (eo > 0) else "  SIGNFLIP"
            print(f"  {hz:>4d} {ni:>6d} {ei*100:>+8.3f}% {ti:>+6.2f} "
                  f"{no:>6d} {eo*100:>+8.3f}% {to:>+6.2f} {xc:>8.2f}x{flag}")
        print()

    print("=" * 74)
    print("  READING IT")
    print("=" * 74)
    print("  OOS t > 2.5 with the same sign as IS  = a real, repeatable effect.")
    print("  OOS xcost > 1.0                        = the edge exceeds one round")
    print("                                           trip — i.e. actually tradeable.")
    print("  A signal can be real (t high) and still worthless (xcost << 1).")
    print(f"  Costs are fixed at {cost*100:.2f}%; only the edge grows with horizon,")
    print("  so the xcost column is where a tradeable signal would finally appear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
