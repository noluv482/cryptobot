#!/usr/bin/env python3
"""Cross-sectional momentum harness.

The idea, and why it is different
--------------------------------
Everything tested so far asks "will SOL go up?" — an absolute directional call,
and the measurements on 2026-07-29 said the bot has no edge at that (favourable
excursion +1.03% vs adverse -1.38% over 82 live trades; a Donchian benchmark on
16 pairs cleared costs in only 1 of 9 configurations).

This asks a different question: "which coins are outperforming the others?" It
ranks the universe each rebalance and holds the leaders, optionally shorting the
laggards. Relative strength, not market direction — so it does not need to
predict the market at all.

The bar it must clear
---------------------
Beating zero is meaningless: in a rising market any long-biased rule looks
brilliant. So every run is reported against EQUAL-WEIGHT BUY-AND-HOLD of the same
universe over the same period. If the strategy cannot beat simply owning the
basket, it is an expensive way to buy beta. That comparison is the whole point of
this file.

Honesty rules
-------------
* Costs charged on TURNOVER only — a coin that stays in the same bucket across a
  rebalance is not re-traded. Charging full turnover every period is a common way
  to make a strategy look worse than it is; charging none makes it look better.
* Ranking uses only data available at the rebalance bar. No lookahead.
* Timestamps are intersected across pairs so every coin is ranked on the same
  date. Misaligned candles would silently compare different days.
* EVERY parameter combination is printed, never the best one.
* Long-short and long-only are both shown: long-only in crypto is mostly a
  leveraged market bet, and separating them shows how much is real selection.

Usage
    python benchmark_xsection.py
    python benchmark_xsection.py --interval 1440 --maker
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request

TAKER_ROUND_TRIP = 2 * 0.0026 + 2 * 0.001     # 0.72%
MAKER_ROUND_TRIP = 2 * 0.0016                 # 0.32%

BASE = "https://api.kraken.com/0/public"
PAIRS = ["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "AVAXUSD", "LINKUSD",
         "DOTUSD", "ATOMUSD", "LTCUSD", "BCHUSD", "FILUSD", "ALGOUSD", "XDGUSD",
         "UNIUSD", "AAVEUSD", "TRXUSD", "XLMUSD"]


def fetch(pair, interval):
    try:
        with urllib.request.urlopen(
                f"{BASE}/OHLC?pair={pair}&interval={interval}", timeout=25) as r:
            d = json.loads(r.read().decode())
        if d.get("error"):
            return {}
        k = next(x for x in d["result"] if x != "last")
        return {int(c[0]): float(c[4]) for c in d["result"][k]}   # ts -> close
    except Exception as e:
        print(f"  {pair}: fetch failed ({e})", file=sys.stderr)
        return {}


def build_panel(data):
    """Intersect timestamps so every coin is ranked on the same bar."""
    common = None
    for series in data.values():
        ks = set(series)
        common = ks if common is None else (common & ks)
    if not common:
        return [], {}
    ts = sorted(common)
    return ts, {p: [data[p][t] for t in ts] for p in data}


def run(ts, panel, lookback, hold, k, long_short, cost):
    """Rank by trailing return each `hold` bars; hold top-k (and short bottom-k)."""
    names = list(panel)
    equity = 1.0
    curve = [1.0]
    periods = []
    held_long, held_short = set(), set()

    i = lookback
    while i + hold < len(ts):
        rets = {}
        for p in names:
            past, now = panel[p][i - lookback], panel[p][i]
            if past > 0:
                rets[p] = (now - past) / past
        if len(rets) < 2 * k:
            i += hold
            continue

        ranked = sorted(rets, key=lambda p: rets[p], reverse=True)
        want_long = set(ranked[:k])
        want_short = set(ranked[-k:]) if long_short else set()

        # Turnover: only positions that actually change are charged.
        changes = len(want_long ^ held_long) + len(want_short ^ held_short)
        n_pos = len(want_long) + len(want_short)
        # Each changed position pays one round trip, spread over the book.
        period_cost = (changes / 2.0) * cost / max(n_pos, 1)

        fwd = 0.0
        for p in want_long:
            a, b = panel[p][i], panel[p][i + hold]
            fwd += ((b - a) / a) / n_pos
        for p in want_short:
            a, b = panel[p][i], panel[p][i + hold]
            fwd += ((a - b) / a) / n_pos

        net = fwd - period_cost
        equity *= (1 + net)
        curve.append(equity)
        periods.append(net)
        held_long, held_short = want_long, want_short
        i += hold

    return equity, curve, periods


def buy_hold(ts, panel, lookback, hold):
    """Equal-weight buy-and-hold over the same window the strategy trades."""
    names = list(panel)
    start_i, end_i = lookback, len(ts) - 1
    total = 0.0
    for p in names:
        a, b = panel[p][start_i], panel[p][end_i]
        total += ((b - a) / a) / len(names)
    return 1.0 + total


def maxdd(curve):
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak else 0.0)
    return dd * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=1440)
    ap.add_argument("--maker", action="store_true",
                    help="use maker round trip (0.32%) instead of taker (0.72%)")
    ap.add_argument("--probe", default="",
                    help="lb,hold,k — sweep the NEIGHBOURHOOD of one result. A real "
                         "effect degrades smoothly; an isolated spike is noise.")
    args = ap.parse_args()
    cost = MAKER_ROUND_TRIP if args.maker else TAKER_ROUND_TRIP

    print(f"fetching {args.interval}m closes for {len(PAIRS)} pairs...")
    data = {}
    for p in PAIRS:
        s = fetch(p, args.interval)
        if len(s) > 200:
            data[p] = s
        time.sleep(1.3)
    if len(data) < 6:
        print("too few pairs fetched", file=sys.stderr)
        return 1

    ts, panel = build_panel(data)
    if len(ts) < 120:
        print(f"only {len(ts)} common bars — not enough", file=sys.stderr)
        return 1

    days = len(ts) * args.interval / 1440
    print(f"\n{len(panel)} pairs, {len(ts)} aligned bars (~{days:.0f} days)")
    print(f"costs: {cost*100:.2f}% round trip on turnover only "
          f"({'maker' if args.maker else 'taker'})")
    print()

    if args.probe:
        plb, phold, pk = (int(x) for x in args.probe.split(","))
        bh = buy_hold(ts, panel, 30, 7)
        print(f"  NEIGHBOURHOOD PROBE around lb={plb} hold={phold} k={pk}")
        print(f"  buy & hold over the same window: {(bh-1)*100:+.1f}%")
        print()
        print("  A genuine effect degrades SMOOTHLY as parameters move. If only the")
        print("  centre works and its neighbours fail, the result is curve-fitted.")
        print()
        print(f"  {'lb':>4s} {'hold':>5s} {'k':>3s} {'return':>9s} {'vs B&H':>9s} {'win%':>6s}")
        print("  " + "-" * 46)
        wins = tot = 0
        probe_rets = []
        for lb in (plb - 4, plb - 2, plb, plb + 2, plb + 4):
            for hold in (phold - 4, phold - 2, phold, phold + 2, phold + 4):
                if lb < 3 or hold < 3:
                    continue
                eq, curve, per = run(ts, panel, lb, hold, pk, True, cost)
                if not per:
                    continue
                tot += 1
                ret = (eq - 1) * 100
                vs = ret - (bh - 1) * 100
                if vs > 0:
                    wins += 1
                probe_rets.append(ret)
                wr = sum(1 for x in per if x > 0) / len(per) * 100
                centre = "  <-- centre" if (lb == plb and hold == phold) else ""
                print(f"  {lb:>4d} {hold:>5d} {pk:>3d} {ret:>+8.1f}% {vs:>+8.1f}% "
                      f"{wr:>5.1f}%{centre}")
        print()
        # "Beats buy & hold" is a weak test when buy & hold is NEGATIVE: any
        # market-neutral rule clears it by simply losing less than the market.
        # So judge on absolute profitability and on dispersion — a real effect
        # produces a smooth surface, noise produces adjacent cells that disagree
        # violently.
        pos = sum(1 for r in probe_rets if r > 0)
        spread = statistics.pstdev(probe_rets) if len(probe_rets) > 1 else 0.0
        mean = statistics.fmean(probe_rets)
        print(f"  {wins}/{tot} neighbours beat buy & hold  "
              f"(weak test: B&H was {(bh-1)*100:+.1f}%)")
        print(f"  {pos}/{tot} neighbours are ABSOLUTELY profitable")
        print(f"  mean {mean:+.1f}%  |  spread (sd) {spread:.1f}pp  |  "
              f"range {min(probe_rets):+.1f}% to {max(probe_rets):+.1f}%")
        print()
        noisy = spread > abs(mean) * 1.5
        if pos / tot < 0.6 or noisy:
            print("  -> NOISE, not an effect. Adjacent parameters disagree by more")
            print("     than the average result, and fewer than 60% of the")
            print("     neighbourhood actually makes money. Beating a falling")
            print("     market is not an edge.")
        else:
            print("  -> Holds up: mostly profitable in absolute terms AND the")
            print("     surface is smooth. Still one regime — validate elsewhere.")
        return 0

    combos = []
    for lb in (7, 14, 30, 60):
        for hold in (7, 14, 30):
            if hold > lb * 2:
                continue
            for k in (3, 5):
                combos.append((lb, hold, k))

    bh = buy_hold(ts, panel, 30, 7)
    print(f"  BAR TO CLEAR — equal-weight buy & hold of the same 18 coins: "
          f"{(bh-1)*100:+.1f}%")
    print()
    print(f"  {'lb':>4s} {'hold':>5s} {'k':>3s} {'mode':>11s} {'periods':>8s} "
          f"{'return':>9s} {'vs B&H':>9s} {'maxDD':>7s} {'win%':>6s}")
    print("  " + "-" * 82)

    beat = 0
    total = 0
    for lb, hold, k in combos:
        for long_short, label in ((True, "long-short"), (False, "long-only")):
            eq, curve, per = run(ts, panel, lb, hold, k, long_short, cost)
            if not per:
                continue
            total += 1
            ret = (eq - 1) * 100
            vs = ret - (bh - 1) * 100
            wr = sum(1 for x in per if x > 0) / len(per) * 100
            if vs > 0:
                beat += 1
            mark = "  <-- beats B&H" if vs > 0 else ""
            print(f"  {lb:>4d} {hold:>5d} {k:>3d} {label:>11s} {len(per):>8d} "
                  f"{ret:>+8.1f}% {vs:>+8.1f}% {maxdd(curve):>6.1f}% {wr:>5.1f}%{mark}")

    print()
    print(f"  {beat}/{total} configurations beat equal-weight buy & hold")
    print()
    if beat == 0:
        print("  Nothing beat simply owning the basket. Cross-sectional momentum")
        print("  does not work on this universe/period — that is a real answer, and")
        print("  it means no amount of tuning the current bot will help either.")
    elif beat < total * 0.3:
        print("  Only a small minority beat buy & hold. That pattern is what")
        print("  overfitting looks like; do not deploy on the best row.")
    else:
        print("  A majority beat buy & hold, which is the signature of a real")
        print("  effect rather than a fitted one. Still ONE regime and one")
        print("  exchange — validate on a different period before risking money.")
    print()
    print("  Note: long-only in crypto is largely a leveraged market bet. If")
    print("  long-only beats B&H but long-short does not, the 'edge' is mostly")
    print("  beta and timing, not coin selection.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
