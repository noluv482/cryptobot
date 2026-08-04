#!/usr/bin/env python3
"""Does CVD divergence actually predict anything?

Order flow is genuinely different information from price — but "different" is
not "predictive", and every previous idea in this project looked plausible and
turned out to be noise. So this measures it before it is allowed anywhere near
the bot's entry gates.

Method
------
1. Page Kraken's public /Trades backwards to reconstruct the historical tape
   (~124 min per 1000-trade request, so ~12 requests per day per pair).
2. Bucket trades into bars and build cumulative volume delta. Volume-weighted
   throughout — trade COUNT gives the opposite answer on real data.
3. At each bar, detect divergence using ONLY bars up to that point.
4. Measure the forward return after each signal and compare it against the
   unconditional forward return over the same period.

The comparison against baseline is the whole test. In a rising market every
long signal looks good; the question is whether the signal beats simply being
in the market at a random time.

Usage
    python test_orderflow.py                       # default pairs, 14 days
    python test_orderflow.py --days 30 --pairs SOLUSD,ETHUSD
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
CACHE = os.path.join(os.environ.get("DATA_DIR", "."), "tape_cache")


def fetch_page(pair, since=None):
    q = {"pair": pair}
    if since:
        q["since"] = str(since)
    url = f"{BASE}/Trades?" + urllib.parse.urlencode(q)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
            if d.get("error"):
                time.sleep(2 + attempt * 2)
                continue
            key = next(k for k in d["result"] if k != "last")
            return d["result"][key], d["result"]["last"]
        except Exception:
            time.sleep(2 + attempt * 2)
    return [], None


def load_tape(pair, days):
    """Page backwards until `days` of history is covered. Cached to disk."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{pair}_{days}d.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        with open(path) as f:
            return json.load(f)

    want_from = time.time() - days * 86400
    rows, cursor, guard = [], None, 0
    # Walk back from now: ask for trades since (oldest_seen - window).
    page, _ = fetch_page(pair)
    if not page:
        return []
    rows.extend(page)
    while rows and rows[0][2] > want_from and guard < days * 20:
        guard += 1
        oldest = rows[0][2]
        since = int((oldest - 7200) * 1e9)      # step back ~2h, the page span
        page, _ = fetch_page(pair, since)
        if not page:
            break
        new = [r for r in page if r[2] < oldest]
        if not new:
            break
        rows = new + rows
        sys.stderr.write(f"\r  {pair}: {len(rows)} trades, back to "
                         f"{time.strftime('%m-%d %H:%M', time.gmtime(rows[0][2]))}   ")
        sys.stderr.flush()
        time.sleep(1.1)                          # respect the public rate limit
    sys.stderr.write("\n")
    out = [[float(r[0]), float(r[1]), float(r[2]), r[3]] for r in rows]
    with open(path, "w") as f:
        json.dump(out, f)
    return out


def build_bars(tape, bar_secs):
    """Bucket the tape into bars carrying close price and signed volume delta."""
    if not tape:
        return []
    bars, cur = [], None
    for price, vol, ts, side in tape:
        b = int(ts // bar_secs) * bar_secs
        if cur is None or b != cur["t"]:
            if cur:
                bars.append(cur)
            cur = {"t": b, "close": price, "delta": 0.0, "vol": 0.0}
        cur["close"] = price
        cur["vol"] += vol
        cur["delta"] += vol if side == "b" else -vol
    if cur:
        bars.append(cur)
    run = 0.0
    for b in bars:
        run += b["delta"]
        b["cvd"] = run
    return bars


def divergences(bars, look):
    """Signals using only bars[:i+1]. Returns [(index, direction)]."""
    out = []
    for i in range(look * 2, len(bars)):
        a = bars[i - look * 2:i - look]
        b = bars[i - look:i + 1]
        pa, pb = max(x["close"] for x in a), max(x["close"] for x in b)
        ca, cb = max(x["cvd"] for x in a), max(x["cvd"] for x in b)
        la, lb = min(x["close"] for x in a), min(x["close"] for x in b)
        da, db = min(x["cvd"] for x in a), min(x["cvd"] for x in b)
        if pb > pa and cb < ca:
            out.append((i, "BEARISH"))
        elif lb < la and db > da:
            out.append((i, "BULLISH"))
    return out


def fwd(bars, i, horizon):
    j = min(i + horizon, len(bars) - 1)
    if j <= i:
        return None
    return (bars[j]["close"] - bars[i]["close"]) / bars[i]["close"] * 100


def measure(pairs, days, bar):
    """One (pairs, days, bar) run. Returns {signal: (n, edge, win)}."""
    sig = {"BEARISH": [], "BULLISH": []}
    base = []
    for pair in pairs:
        tape = load_tape(pair, days)
        if len(tape) < 5000:
            continue
        bars = build_bars(tape, bar)
        if len(bars) < 40:
            continue
        b = [fwd(bars, i, 6) for i in range(len(bars) - 6)]
        base += [x for x in b if x is not None]
        for i, d in divergences(bars, 6):
            f = fwd(bars, i, 6)
            if f is not None:
                sig[d].append(f)
    if not base:
        return {}
    bm = statistics.fmean(base)
    out = {}
    for d, v in sig.items():
        if len(v) < 5:
            continue
        m = statistics.fmean(v)
        edge = (bm - m) if d == "BEARISH" else (m - bm)
        win = (sum(1 for x in v if x < 0) if d == "BEARISH"
               else sum(1 for x in v if x > 0)) / len(v) * 100
        out[d] = (len(v), edge, win)
    return out


def sweep(pairs, days):
    """Judge the signal across bar sizes.

    A real effect degrades smoothly as the bar size changes. One setting working
    while its neighbours fail is the signature of noise, and picking that
    setting is how a backtest lies to you. This project already made that exact
    mistake once with a 3-pair Donchian smoke test that reversed on the full
    universe -- so consistency, not the best row, is the verdict here.
    """
    sizes = [900, 1800, 3600, 7200, 14400]
    print("=" * 72)
    print("  CONSISTENCY SWEEP — a real signal should survive a change of bar size")
    print("=" * 72)
    print(f"  {'bar':>7s} {'BEAR n':>7s} {'BEAR edge':>10s} {'BEAR win':>9s} "
          f"{'BULL n':>7s} {'BULL edge':>10s} {'BULL win':>9s}")
    print("  " + "-" * 68)
    rows = []
    for b in sizes:
        r = measure(pairs, days, b)
        if not r:
            print(f"  {b:>7d}   (insufficient data)")
            continue
        be = r.get("BEARISH"); bu = r.get("BULLISH")
        rows.append((b, be, bu))
        f = lambda t: (f"{t[0]:>7d} {t[1]:>+9.3f}% {t[2]:>8.1f}%" if t else
                       f"{'-':>7s} {'-':>10s} {'-':>9s}")
        lbl = f"{b//60}m" if b < 3600 else f"{b//3600}h"
        print(f"  {lbl:>7s} {f(be)} {f(bu)}")

    print()
    for name, idx in (("BEARISH", 1), ("BULLISH", 2)):
        vals = [r[idx][1] for r in rows if r[idx]]
        if len(vals) < 3:
            print(f"  {name}: too few settings to judge")
            continue
        pos = sum(1 for v in vals if v > 0)
        spread = statistics.pstdev(vals)
        mean = statistics.fmean(vals)
        print(f"  {name}: positive at {pos}/{len(vals)} settings | "
              f"mean edge {mean:+.3f}% | spread {spread:.3f}pp")
        if pos <= len(vals) / 2 or spread > abs(mean) * 1.5:
            print(f"           -> NOISE. Settings disagree more than the average "
                  f"result; the good ones are luck, not signal.")
        else:
            print(f"           -> holds up across settings. Worth a second period.")
    print()
    print("  Reminder: picking the single best bar size here would be curve-fitting.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--pairs", default="SOLUSD,ETHUSD,XBTUSD,AVAXUSD")
    ap.add_argument("--bar", type=int, default=3600, help="bar size in seconds")
    ap.add_argument("--sweep", action="store_true",
                    help="run several bar sizes and judge CONSISTENCY across them")
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    if args.sweep:
        return sweep(pairs, args.days)

    print(f"reconstructing {args.days}d of trade tape for {len(pairs)} pairs")
    print("(cached 6h on disk — reruns are instant)\n")

    all_sig, all_base = {"BEARISH": [], "BULLISH": []}, []
    per_pair = []

    for pair in pairs:
        tape = load_tape(pair, args.days)
        if len(tape) < 5000:
            print(f"  {pair}: only {len(tape)} trades — skipping")
            continue
        bars = build_bars(tape, args.bar)
        if len(bars) < 40:
            print(f"  {pair}: only {len(bars)} bars — skipping")
            continue
        for look in (6,):
            sigs = divergences(bars, look)
            base = [fwd(bars, i, 6) for i in range(len(bars) - 6)]
            base = [x for x in base if x is not None]
            all_base += base
            got = {"BEARISH": [], "BULLISH": []}
            for i, d in sigs:
                f = fwd(bars, i, 6)
                if f is not None:
                    got[d].append(f)
                    all_sig[d].append(f)
            per_pair.append((pair, len(bars), len(sigs), got, base))
            print(f"  {pair}: {len(tape)} trades, {len(bars)} bars, {len(sigs)} signals")

    if not per_pair:
        print("\nnot enough data")
        return 1

    print()
    print("=" * 72)
    print("  Forward return 6 bars after a divergence, vs the unconditional")
    print("  average over the same bars. A signal has to beat the baseline.")
    print("=" * 72)
    print(f"  {'signal':10s} {'n':>5s} {'mean fwd':>10s} {'baseline':>10s} "
          f"{'edge':>8s} {'win%':>7s}")
    print("  " + "-" * 60)

    b_mean = statistics.fmean(all_base) if all_base else 0.0
    verdicts = []
    for d in ("BEARISH", "BULLISH"):
        v = all_sig[d]
        if len(v) < 5:
            print(f"  {d:10s} {len(v):>5d}      too few signals to judge")
            continue
        m = statistics.fmean(v)
        # A bearish signal is right when price FALLS, so its edge is inverted.
        edge = (b_mean - m) if d == "BEARISH" else (m - b_mean)
        win = (sum(1 for x in v if x < 0) if d == "BEARISH"
               else sum(1 for x in v if x > 0)) / len(v) * 100
        verdicts.append((d, len(v), edge))
        print(f"  {d:10s} {len(v):>5d} {m:>+9.3f}% {b_mean:>+9.3f}% "
              f"{edge:>+7.3f}% {win:>6.1f}%")

    print()
    print(f"  baseline sample: {len(all_base)} bars, mean {b_mean:+.3f}%")
    print()
    if not verdicts:
        print("  VERDICT: not enough signals — inconclusive, not a negative result.")
    else:
        best = max(abs(e) for _, _, e in verdicts)
        thin = any(n < 30 for _, n, _ in verdicts)
        pos = [d for d, _, e in verdicts if e > 0.05]
        if not pos:
            print("  VERDICT: no edge. Divergence does not beat simply being in the")
            print("  market. Do NOT wire this into the entry gates.")
        elif thin or best < 0.15:
            print(f"  VERDICT: marginal ({', '.join(pos)} positive but small or thin).")
            print("  Not enough to act on — collect more days and re-run.")
        else:
            print(f"  VERDICT: {', '.join(pos)} beats baseline on this sample.")
            print("  Still ONE period and one exchange — re-run on different days")
            print("  before letting it influence a live trade.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
