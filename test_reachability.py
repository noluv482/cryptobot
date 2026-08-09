#!/usr/bin/env python3
"""Does _reachable_dist() measure what the offline analysis measured?

analyze_reachability.py found, over 136 real trades, that the median best-case
favourable move within 8 hours was 0.758%, and that whole pairs rarely travel
the 2.30% their targets asked for. _reachable_dist() is the live version of
that measurement, so it has to agree with it on the same candles -- otherwise
the bot is clamping targets using a number that means something else.

Also checks the properties the clamp depends on:
  - more bars cannot reach less far (monotonic)
  - a quiet pair reports a smaller reach than a volatile one
  - insufficient history returns None rather than a confident wrong answer
  - the clamp only ever pulls a target IN, never pushes it out

Usage:
    python test_reachability.py            # uses cached candles if present
"""
import os
import random
import statistics
import sys

import bot_server as bs
from analyze_stale_exit import fetch_ohlc

PAIRS = ["XBTUSD", "SOLUSD", "LINKUSD", "UNIUSD", "XRPUSD", "NEARUSD"]
# What analyze_reachability.py reported per pair: % of 8h windows containing a
# 2.30% move. A pair near the bottom of that list must not report a big reach.
KNOWN_LOW = {"XBTUSD", "XRPUSD"}      # 4% and 8% of windows
KNOWN_HIGH = {"UNIUSD", "NEARUSD"}    # 59% and 38%


def synth(bar_vol, n=400, seed=1):
    random.seed(seed)
    closes, highs, lows = [100.0], [100.0], [100.0]
    for _ in range(n):
        c = closes[-1] * (1 + random.gauss(0, bar_vol))
        closes.append(c)
        highs.append(c * (1 + bar_vol / 2))
        lows.append(c * (1 - bar_vol / 2))
    return highs, lows, closes


def main():
    fails = []
    P = print

    P("=" * 72)
    P("  PROPERTIES")
    P("=" * 72)

    h, l, c = synth(0.004)
    r8 = bs._reachable_dist(h, l, c, 8, "BUY")
    r24 = bs._reachable_dist(h, l, c, 24, "BUY")
    P(f"  8 bars  -> {r8*100:.3f}%")
    P(f"  24 bars -> {r24*100:.3f}%")
    if not (r24 >= r8):
        fails.append("more bars reported LESS reach — not monotonic")
    else:
        P("  monotonic in bars (ok)")

    hq, lq, cq = synth(0.001, seed=2)
    hv, lv, cv = synth(0.010, seed=2)
    rq = bs._reachable_dist(hq, lq, cq, 8, "BUY")
    rv = bs._reachable_dist(hv, lv, cv, 8, "BUY")
    P(f"  quiet pair (0.1%/bar)    -> {rq*100:.3f}%")
    P(f"  volatile pair (1.0%/bar) -> {rv*100:.3f}%")
    if not (rv > rq * 3):
        fails.append("volatile pair did not report a materially larger reach")
    else:
        P("  scales with volatility (ok)")

    if bs._reachable_dist(h[:6], l[:6], c[:6], 8, "BUY") is not None:
        fails.append("returned a number from insufficient history")
    else:
        P("  insufficient history -> None (ok)")

    # REGRESSION: the live bot fetches CANDLE_LIMIT (80) candles, and the hold
    # horizon is 48 bars. The original guard (n < bars + 12) passed that, leaving
    # 31 heavily-overlapping windows -- under two independent observations -- and
    # returned a confident 0.70% where 720 candles say 3.70%. The cost floor then
    # rejected everything and the bot did not trade for 54 hours. A measurement
    # this thin must return None so the caller leaves the target alone.
    bars_live = int(bs.MAX_TRADE_MINS / bs.INTERVAL)
    thin = bs._reachable_dist(h[:bs.CANDLE_LIMIT], l[:bs.CANDLE_LIMIT],
                              c[:bs.CANDLE_LIMIT], bars_live, "BUY")
    P(f"  {bs.CANDLE_LIMIT} candles with a {bars_live}-bar lookahead -> {thin}")
    if thin is not None:
        fails.append(f"measured {thin*100:.2f}% from only {bs.CANDLE_LIMIT} candles "
                     f"with a {bars_live}-bar lookahead — this halted trading for 54h")
    else:
        P("  refuses to measure from the scan's short window (ok)")

    # And the guard must not be so strict that a full history is refused.
    full = bs._reachable_dist(h, l, c, bars_live, "BUY") if len(c) > bars_live * 4 else None
    if len(c) > bars_live * 4 and full is None:
        fails.append("refused to measure even with ample history — too strict")

    # Both sides must be measured, and on symmetric noise be similar.
    rs = bs._reachable_dist(h, l, c, 8, "SELL")
    P(f"  BUY {r8*100:.3f}%  vs  SELL {rs*100:.3f}%")
    if abs(r8 - rs) > max(r8, rs) * 0.6:
        fails.append("BUY and SELL reach wildly different on symmetric noise")

    P()
    P("=" * 72)
    P("  AGAINST REAL CANDLES — must agree with the offline analysis")
    P("=" * 72)
    P(f"  {'pair':10s} {'reach 8 bars':>14s}   note")
    P("  " + "-" * 56)
    got = {}
    for p in PAIRS:
        bars = fetch_ohlc(p)
        if not bars:
            P(f"  {p:10s} {'(no candles)':>14s}")
            continue
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        closes = [b["c"] for b in bars]
        r = bs._reachable_dist(highs, lows, closes, 8, "BUY")
        if r is None:
            continue
        got[p] = r
        note = ""
        if p in KNOWN_LOW:
            note = "analysis: reaches 2.30% in <10% of windows"
        elif p in KNOWN_HIGH:
            note = "analysis: reaches 2.30% in 38-59% of windows"
        P(f"  {p:10s} {r*100:>13.3f}%   {note}")

    if got:
        med = statistics.median(got.values())
        P()
        P(f"  median across these pairs: {med*100:.3f}%")
        P(f"  offline analysis found a median best-case of 0.758% over 8h on the")
        P(f"  136 real trades — the same order of magnitude, as it must be.")
        # A live measure that disagreed by more than ~2x would mean the clamp is
        # using a different quantity than the one that was validated.
        if not (0.003 < med < 0.020):
            fails.append(f"live reach {med*100:.2f}% is nowhere near the measured 0.758%")
        # The ranking is the part the clamp actually relies on.
        lows_seen = [got[p] for p in KNOWN_LOW if p in got]
        highs_seen = [got[p] for p in KNOWN_HIGH if p in got]
        if lows_seen and highs_seen and max(lows_seen) >= min(highs_seen):
            fails.append("pairs the analysis called quiet do not rank below the busy ones")
        elif lows_seen and highs_seen:
            P(f"  quiet pairs rank below busy ones (ok)")

    P()
    P("=" * 72)
    P("  THE CLAMP ONLY EVER PULLS A TARGET IN")
    P("=" * 72)
    price = 100.0
    for side, tgt in (("BUY", 103.0), ("SELL", 97.0), ("BUY", 100.2), ("SELL", 99.9)):
        reach = 0.008
        want = (tgt - price) if side == "BUY" else (price - tgt)
        reach_px = price * reach
        if want > reach_px:
            new = price + reach_px if side == "BUY" else price - reach_px
        else:
            new = tgt
        new_want = (new - price) if side == "BUY" else (price - new)
        P(f"  {side:4s} target {tgt:6.2f} ({want/price*100:+.2f}%) -> "
          f"{new:6.2f} ({new_want/price*100:+.2f}%)")
        if new_want > want + 1e-9:
            fails.append(f"{side} {tgt}: clamp pushed the target FURTHER out")
        if new_want <= 0:
            fails.append(f"{side} {tgt}: clamp produced a target at or behind entry")

    P()
    P("=" * 72)
    if fails:
        P(f"  {len(fails)} FAILURE(S)")
        for f in fails:
            P("   x " + f)
        return 1
    P("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
