#!/usr/bin/env python3
"""Is rsi_zone (40 <= RSI <= 60) a good thing to reward, or a bad one?

The live engine scores rsi_zone as a POSITIVE pillar and gives middle RSI the
full 1.0 in rsi_pts. Two things argue it is backwards:

  - the pillar table: 12.8% win rate when active (n=117) vs 29.2% when not
    (n=48), a -16.3pp lift, z = -2.25
  - the signal search found no support for rewarding middle RSI at 1h or 4h

Both are suggestive and neither is proof. 11 pillars were tested, so one hit at
p = 0.025 is roughly what chance produces, and the sample is 165 trades of a
system with no measurable entry edge. This tests the hypothesis directly.

The test matches how the pillar is actually USED: it is not a direction, it is
a filter on the engine's own signals. So take every BUY/SELL the live engine
produces, split them by whether rsi_zone was true, and compare what each group
went on to do. Each group is measured against its OWN direction-matched
baseline, because the two groups do not have the same long/short mix and the
market's drift would otherwise be scored as an effect.

Same guards that killed the last promising finding:
  full universe · in-sample/out-of-sample time split · non-overlapping forward
  windows · direction-matched baselines · two timeframes

Usage:
    python test_rsi_zone.py                 # 1h and 4h
    python test_rsi_zone.py --interval 60
"""
import argparse
import math
import statistics
import sys
import time

import bot_server as bs

bs.log = lambda *a, **k: None


def fwd(c, i, n, side):
    j = i + n
    if j >= len(c):
        return None
    r = (c[j] - c[i]) / c[i]
    return -r if side == "SELL" else r


def collect(series, interval, horizon):
    """Replay the live engine. Returns rows of (bar_index, pair_idx, side,
    rsi_at_signal, rsi_zone_bool, forward_return) plus a baseline per pair."""
    rows, base = [], []
    for pi, (c, h, l, v) in enumerate(series):
        eng = bs.SignalEngine()
        lb = 80
        b = len(c) - horizon - 1
        while lb < b:                              # non-overlapping baseline
            f = fwd(c, lb, horizon, "BUY")
            if f is not None:
                base.append(f)
            lb += horizon
        for i in range(80, b):
            try:
                sig, plan, ema, rsi, conf = eng.evaluate(
                    c[:i+1], h[:i+1], l[:i+1], v[:i+1], c[i], [], pair=None)
            except Exception:
                continue
            if sig not in ("BUY", "SELL"):
                continue
            pil = plan.get("pillars") or {}
            if "rsi_zone" not in pil or rsi is None:
                continue
            f = fwd(c, i, horizon, sig)
            if f is None:
                continue
            rows.append((i, pi, sig, rsi, bool(pil["rsi_zone"]), f))
    return rows, base


def deoverlap(rows, horizon):
    """Keep only signals whose forward windows do not overlap, per pair."""
    out, last = [], {}
    for r in sorted(rows, key=lambda x: (x[1], x[0])):
        i, pi = r[0], r[1]
        if pi in last and i < last[pi] + horizon:
            continue
        last[pi] = i
        out.append(r)
    return out


def edge(group, base_mean):
    """Mean forward return less a direction-matched baseline, and its t."""
    if len(group) < 12:
        return len(group), None, None
    adj = [f - base_mean if s == "BUY" else f + base_mean
           for (_, _, s, _, _, f) in group]
    m = statistics.fmean(adj)
    sd = statistics.pstdev(adj)
    if sd < 1e-12:
        return len(adj), None, None
    return len(adj), m, m / (sd / math.sqrt(len(adj)))


def run(interval, horizons):
    pairs = [c["pair"] for c in bs.SCAN_UNIVERSE]
    print(f"fetching {len(pairs)} pairs at {interval}m ...")
    series = []
    for p in pairs:
        try:
            c, h, l, v, o = bs.get_klines(p, interval=interval, limit=720)
        except Exception:
            continue
        if len(c) >= 300:
            series.append((c, h, l, v))
        time.sleep(1.1)
    if not series:
        print("  no data")
        return []
    n = min(len(c) for c, _, _, _ in series)
    mid = n // 2
    print(f"  {len(series)} pairs x {n} bars   IS: 80..{mid}   OOS: {mid}..{n}\n")

    findings = []
    for hz in horizons:
        rows, base = collect(series, interval, hz)
        rows = deoverlap(rows, hz)
        bl = statistics.fmean(base) if base else 0.0
        print("=" * 76)
        print(f"  {interval}m candles, {hz}-bar horizon "
              f"({hz*interval/60:.0f}h hold) — {len(rows)} de-overlapped signals")
        print("=" * 76)
        print(f"  {'window':10s} {'group':14s} {'n':>5s} {'edge':>9s} {'t':>7s}")
        print("  " + "-" * 52)
        for label, lo, hi in (("in-sample", 80, mid), ("out-sample", mid, n)):
            part = [r for r in rows if lo <= r[0] < hi]
            inz = [r for r in part if r[4]]
            outz = [r for r in part if not r[4]]
            res = {}
            for gname, g in (("rsi_zone TRUE", inz), ("rsi_zone FALSE", outz)):
                cnt, e, t = edge(g, bl)
                res[gname] = (cnt, e, t)
                es = f"{e*100:>+8.3f}%" if e is not None else f"{'—':>9s}"
                ts = f"{t:>+7.2f}" if t is not None else f"{'—':>7s}"
                print(f"  {label:10s} {gname:14s} {cnt:>5d} {es} {ts}")
            a = res["rsi_zone TRUE"][1]
            b = res["rsi_zone FALSE"][1]
            if a is not None and b is not None:
                print(f"  {'':10s} {'difference':14s} {'':>5s} "
                      f"{(a-b)*100:>+8.3f}%   (TRUE minus FALSE)")
                findings.append((interval, hz, label, a, b))
        print()
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=0, help="0 = test both 60 and 240")
    args = ap.parse_args()

    all_f = []
    if args.interval:
        all_f += run(args.interval, [24, 48])
    else:
        all_f += run(60, [24, 48])
        all_f += run(240, [6, 12])

    print("=" * 76)
    print("  VERDICT")
    print("=" * 76)
    # The claim under test: rewarding rsi_zone is harmful, i.e. the TRUE group
    # should do WORSE than the FALSE group, consistently.
    pairs_ = {}
    for iv, hz, win, a, b in all_f:
        pairs_.setdefault((iv, hz), {})[win] = a - b
    consistent_neg = consistent_pos = 0
    total = 0
    for k, v in sorted(pairs_.items()):
        if "in-sample" in v and "out-sample" in v:
            total += 1
            i_, o_ = v["in-sample"], v["out-sample"]
            tag = ("both negative" if i_ < 0 and o_ < 0 else
                   "both positive" if i_ > 0 and o_ > 0 else "SIGN FLIP")
            if i_ < 0 and o_ < 0: consistent_neg += 1
            if i_ > 0 and o_ > 0: consistent_pos += 1
            print(f"  {k[0]}m / {k[1]} bars:  IS {i_*100:+.3f}%   "
                  f"OOS {o_*100:+.3f}%   -> {tag}")
    print()
    if total == 0:
        print("  not enough signals to judge")
    elif consistent_neg == total:
        print("  rsi_zone TRUE underperforms in EVERY window and timeframe.")
        print("  The pillar rewards a condition that predicts worse outcomes.")
        print("  Removing the reward is justified; INVERTING it is not — that")
        print("  needs its own test, since 'not middle' is a wide, mixed bucket.")
    elif consistent_pos == total:
        print("  rsi_zone TRUE outperforms everywhere — the pillar is correct")
        print("  as written and the -16.3pp lift in the trade table was noise.")
    else:
        print(f"  Inconsistent: {consistent_neg}/{total} windows negative, "
              f"{consistent_pos}/{total} positive.")
        print("  The -16.3pp lift does NOT replicate under a proper split. On")
        print("  this evidence rsi_zone is not clearly harmful OR helpful — it")
        print("  is noise, and the honest action is to leave it alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
