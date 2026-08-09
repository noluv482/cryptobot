#!/usr/bin/env python3
"""Does the entry signal predict direction AT ALL?

This is the only question that matters right now, and nothing downstream can
answer it. Replaying the live book showed the trades lose 0.211% each BEFORE
any fee (t = -3.54 over 139 trades) -- so cheaper fees, a longer timeframe and
looser gates are all downstream of a signal that may simply have no edge.

Everything that could hide the answer is stripped out:

  no exits      -- forward return at a FIXED horizon, so trailing stops and
                   stale exits cannot manufacture or destroy the result
  no costs      -- gross price move only
  no gates      -- the R:R, cost-floor and reachability gates are skipped; they
                   decide WHETHER to trade, not whether the signal is right
  no sizing     -- every signal counts once, so one big position cannot carry
                   the average

What remains is: when the engine says BUY, does price go up more than it does
at a random moment in the same market?

Method
------
Walk each pair bar by bar. At bar i the engine sees ONLY closes[:i+1] -- the
same information it would have had live. When the signal CHANGES to BUY or SELL
(mirroring the live loop, which acts on changes), record the forward return in
the signal's direction at several horizons. The baseline is the same forward
return measured at EVERY bar, which is what "being in the market at a random
time" pays.

Edge = signal mean - baseline mean. A signal with no skill scores 0.

Usage:
    python test_signal_edge.py                       # default universe
    python test_signal_edge.py --pairs SOLUSD,ADAUSD --horizons 6,24
"""
import argparse
import math
import statistics
import sys
import time

import bot_server as bs

bs.log = lambda *a, **k: None          # the engine logs a line per gate


def fwd(closes, i, n, direction):
    """Return over n bars from bar i, signed so positive = the call was right."""
    j = i + n
    if j >= len(closes):
        return None
    r = (closes[j] - closes[i]) / closes[i]
    return -r if direction == "SELL" else r


def walk(pair, candles, horizons, warmup=60):
    """Replay one pair. Returns (signals, baseline) keyed by horizon.

    `signals` holds (horizon -> [forward returns]) for bars where the signal
    changed to BUY/SELL. `baseline` holds the same horizons measured at every
    bar, as the unconditional comparison.
    """
    closes, highs, lows, vols = candles
    engine = bs.SignalEngine()
    sigs = {h: [] for h in horizons}
    sides = {"BUY": 0, "SELL": 0}
    base = {h: [] for h in horizons}
    last = None

    for i in range(warmup, len(closes) - 1):
        # Strictly no lookahead: the engine sees history up to and including i.
        c, h_, l_, v_ = closes[:i+1], highs[:i+1], lows[:i+1], vols[:i+1]
        # Baseline is stored LONG-signed. It gets compared per direction later:
        # a SELL is judged against the short baseline (-long). Comparing a
        # ~50/50 long/short signal against buy-and-hold would score every short
        # as a failure purely because the market rose over the sample, which
        # measures the trend and not the signal.
        for hz in horizons:
            b = fwd(closes, i, hz, "BUY")
            if b is not None:
                base[hz].append(b)
        try:
            sig, plan, ema, rsi, conf = engine.evaluate(
                c, h_, l_, v_, c[-1], [], pair=pair)
        except Exception:
            continue
        if sig in ("BUY", "SELL") and sig != last:
            sides[sig] += 1
            for hz in horizons:
                f = fwd(closes, i, hz, sig)
                if f is not None:
                    sigs[hz].append((sig, f))
        last = sig
    return sigs, base, sides


def tstat(sample, baseline_mean):
    """t for (sample mean - baseline mean). Baseline is treated as a known
    constant: it is measured on far more observations than the signal set."""
    n = len(sample)
    if n < 8:
        return None
    sd = statistics.pstdev(sample)
    if sd < 1e-12:
        return None
    return (statistics.fmean(sample) - baseline_mean) / (sd / math.sqrt(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="SOLUSD,ADAUSD,DOTUSD,UNIUSD,ATOMUSD,"
                                       "LINKUSD,NEARUSD,XBTUSD,ETHUSD,XRPUSD")
    ap.add_argument("--horizons", default="1,3,6,12,24,48")
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    horizons = [int(x) for x in args.horizons.split(",")]

    print(f"replaying {len(pairs)} pairs on {bs.INTERVAL}m candles")
    print("engine sees history up to each bar only — no lookahead\n")

    all_sig = {h: [] for h in horizons}
    all_base = {h: [] for h in horizons}
    total_sides = {"BUY": 0, "SELL": 0}

    for p in pairs:
        try:
            closes, highs, lows, vols, opens = bs.get_klines(p, limit=720)
        except Exception as e:
            print(f"  {p}: fetch failed ({e})", file=sys.stderr)
            continue
        if len(closes) < 150:
            print(f"  {p}: only {len(closes)} candles — skipped", file=sys.stderr)
            continue
        s, b, sides = walk(p, (closes, highs, lows, vols), horizons)
        n = sum(sides.values())
        print(f"  {p:9s} {len(closes):4d} candles  {n:4d} signals "
              f"({sides['BUY']} BUY / {sides['SELL']} SELL)")
        for h in horizons:
            all_sig[h] += s[h]
            all_base[h] += b[h]
        total_sides["BUY"] += sides["BUY"]
        total_sides["SELL"] += sides["SELL"]
        time.sleep(1.1)                      # public API rate limit

    if not any(all_sig.values()):
        print("\nno signals produced — nothing to measure")
        return 1

    print()
    print("=" * 74)
    print("  DOES THE SIGNAL BEAT A RANDOM ENTRY OF THE SAME DIRECTION?")
    print("  (gross move, no costs, no exits — each side vs its own baseline)")
    print("=" * 74)
    verdicts = []
    for label, want in (("BUY", "BUY"), ("SELL", "SELL"), ("ALL", None)):
        print(f"\n  --- {label} ---")
        print(f"  {'horizon':>8s} {'n':>6s} {'signal':>9s} {'baseline':>9s} "
              f"{'edge':>9s} {'t':>7s}  verdict")
        print("  " + "-" * 66)
        for h in horizons:
            rows = [f for s, f in all_sig[h] if want is None or s == want]
            if not all_base[h] or len(rows) < 8:
                print(f"  {h:>7d}h {len(rows):>6d}   too few signals")
                continue
            blong = statistics.fmean(all_base[h])
            if want == "SELL":
                bmean = -blong                      # short's fair comparison
            elif want == "BUY":
                bmean = blong
            else:
                # Mixed set: weight each side's baseline by how often it fired,
                # so the trend cannot flatter or punish the aggregate.
                nb = sum(1 for s, _ in all_sig[h] if s == "BUY")
                ns = sum(1 for s, _ in all_sig[h] if s == "SELL")
                bmean = (nb * blong + ns * -blong) / max(nb + ns, 1)
            m = statistics.fmean(rows)
            t = tstat(rows, bmean)
            edge = m - bmean
            if t is None:      verdict = "—"
            elif t > 2:        verdict = "POSITIVE EDGE"
            elif t < -2:       verdict = "NEGATIVE (worse than random)"
            else:              verdict = "no edge (indistinguishable)"
            if label == "ALL":
                verdicts.append((h, edge, t))
            print(f"  {h:>7d}h {len(rows):>6d} {m*100:>+8.3f}% {bmean*100:>+8.3f}% "
                  f"{edge*100:>+8.3f}% {t:>+7.2f}  {verdict}")

    print()
    print(f"  signal mix: {total_sides['BUY']} BUY / {total_sides['SELL']} SELL")
    print(f"  baseline sample: {len(all_base[horizons[0]])} bars")
    print()
    pos = [h for h, e, t in verdicts if t is not None and t > 2]
    neg = [h for h, e, t in verdicts if t is not None and t < -2]
    print("=" * 74)
    if pos:
        print(f"  The signal beats a random entry at {sorted(pos)} bars.")
        print("  That is a real edge to build on — costs, gates and timeframe")
        print("  now matter, because there is something for them to preserve.")
    elif neg:
        print(f"  The signal is WORSE than a random entry at {sorted(neg)} bars.")
        print("  Fees, gates and timeframe are all downstream of this. Tuning")
        print("  them cannot turn a negative edge positive — only lose slower.")
        print("  The entry logic needs replacing, not adjusting.")
    else:
        print("  No edge at any horizon: the signal is indistinguishable from")
        print("  entering at random. It is not actively harmful, but there is")
        print("  nothing here for better fees or wider stops to preserve.")
    print()
    print("  Caveats: ~30 days of Kraken history, one timeframe, macro feeds")
    print("  (news/NASDAQ/funding) read NEUTRAL in replay, and forward returns")
    print("  overlap so the t-values are optimistic. A weak positive here would")
    print("  need a second period before being believed; a clear negative is")
    print("  already enough to stop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
