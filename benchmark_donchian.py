#!/usr/bin/env python3
"""Donchian breakout benchmark — a number for the real strategy to beat.

Why a benchmark
---------------
On 2026-07-29 the live strategy was measured over 82 trades: average favourable
excursion +1.03% against average adverse excursion -1.38%, a 19.5% win rate, and
targets reached only 6% of the time. Before adding anything else it is worth
knowing what a deliberately dumb rule does on the same data with the same costs.
If nine pillars of gates cannot beat a 20-line breakout rule, that is the finding.

This is intentionally NOT wired into bot_server.py. A measuring stick you can
tune is not a measuring stick.

Honesty rules baked in
----------------------
* Full round-trip costs applied to every trade (fees + slippage, same constants
  as the bot). A backtest without costs is fiction.
* EVERY parameter combination is printed, not the best one. Cherry-picking the
  top row of a sweep is how backtests lie.
* Breakeven win rate is shown per row: the win rate that combination's own
  payoff ratio requires. Below it, the rule loses no matter the win rate story.
* Entries are decided on CLOSED bars; stops/targets are checked against
  subsequent bars' highs/lows, and when a single bar spans both the stop is
  assumed to hit first. No peeking.
* Daily candles are the default because Kraken returns 720 bars per request —
  ~2 years daily versus ~30 days hourly. Sample size matters more than
  resolution for a trend rule.

Usage
    python benchmark_donchian.py                      # daily, all pairs, sweep
    python benchmark_donchian.py --interval 240       # 4h bars
    python benchmark_donchian.py --pairs XBTUSD,ETHUSD
"""
import argparse
import json
import sys
import time
import urllib.request

TAKER_FEE = 0.0026
MAKER_FEE = 0.0016
SLIPPAGE = 0.001
ROUND_TRIP = 2 * TAKER_FEE + 2 * SLIPPAGE       # 0.72%, matches bot_server
# Posting passively earns the maker fee AND avoids slippage (you name the price),
# but only fills if price comes back to you. For a breakout that is precisely the
# adverse-selection case: the trades that never pull back are the ones that ran.
ROUND_TRIP_MAKER = 2 * MAKER_FEE                # 0.32%
KRAKEN_FEE = TAKER_FEE                          # back-compat for existing prints

BASE = "https://api.kraken.com/0/public"
DEFAULT_PAIRS = ["XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "AVAXUSD",
                 "LINKUSD", "DOTUSD", "ATOMUSD", "LTCUSD", "BCHUSD", "FILUSD",
                 "ALGOUSD", "XDGUSD", "UNIUSD", "AAVEUSD"]


def fetch(pair, interval):
    try:
        with urllib.request.urlopen(
                f"{BASE}/OHLC?pair={pair}&interval={interval}", timeout=25) as r:
            d = json.loads(r.read().decode())
        if d.get("error"):
            return []
        k = next(x for x in d["result"] if x != "last")
        return [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
                 "l": float(c[3]), "c": float(c[4])} for c in d["result"][k]]
    except Exception as e:
        print(f"  {pair}: fetch failed ({e})", file=sys.stderr)
        return []


def atr(bars, i, period=14):
    if i < period:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        trs.append(max(bars[j]["h"] - bars[j]["l"],
                       abs(bars[j]["h"] - bars[j - 1]["c"]),
                       abs(bars[j]["l"] - bars[j - 1]["c"])))
    return sum(trs) / period


def run_pair(bars, lookback, stop_atr, target_atr, max_hold, allow_short=True,
             fill="taker", maker_wait=2):
    """Donchian: close above the prior N-bar high goes long, below the low goes short.

    fill="taker": cross the spread, always fills at the signal close plus slippage.
    fill="maker":  post a limit AT the signal close and wait up to `maker_wait`
                   bars for price to come back. Cheaper (maker fee, no slippage)
                   but a runaway breakout never fills — which is the whole point
                   of testing it rather than assuming the saving is free.

    Missed maker fills are recorded with what they WOULD have made as a taker, so
    adverse selection can be measured instead of hand-waved.
    """
    trades = []
    missed = []
    i = max(lookback, 15)
    while i < len(bars) - 1:
        win = bars[i - lookback:i]                 # prior N bars, excludes bar i
        hh = max(b["h"] for b in win)
        ll = min(b["l"] for b in win)
        price = bars[i]["c"]
        a = atr(bars, i)
        if not a:
            i += 1
            continue

        side = None
        if price > hh:
            side = "LONG"
        elif allow_short and price < ll:
            side = "SHORT"
        if not side:
            i += 1
            continue

        taker_entry = price * (1 + SLIPPAGE) if side == "LONG" else price * (1 - SLIPPAGE)

        if fill == "maker":
            # Limit rests at the signal close; needs price to trade back to it.
            start = None
            for w in range(i + 1, min(i + 1 + maker_wait, len(bars))):
                if side == "LONG" and bars[w]["l"] <= price:
                    start = w; break
                if side == "SHORT" and bars[w]["h"] >= price:
                    start = w; break
            if start is None:
                # No fill. Record what a taker entry WOULD have earned, so the
                # cost of missing this trade is measured rather than assumed.
                missed.append(_exit_sim(bars, i, side, taker_entry, stop_atr,
                                        target_atr, a, max_hold, ROUND_TRIP))
                i += 1
                continue
            trades.append(_exit_sim(bars, start, side, price, stop_atr,
                                    target_atr, a, max_hold, ROUND_TRIP_MAKER))
        else:
            start = i
            trades.append(_exit_sim(bars, i, side, taker_entry, stop_atr,
                                    target_atr, a, max_hold, ROUND_TRIP))

        i = start + trades[-1]["bars"] + 1          # no overlapping positions
    return trades, missed


def _exit_sim(bars, start, side, entry, stop_atr, target_atr, a, max_hold, cost):
    """Walk forward from `start` and resolve the position. Shared by both fill models."""
    if side == "LONG":
        stop, target = entry - stop_atr * a, entry + target_atr * a
    else:
        stop, target = entry + stop_atr * a, entry - target_atr * a

    exit_px = reason = None
    held = 0
    for j in range(start + 1, min(start + 1 + max_hold, len(bars))):
        held = j - start
        hi, lo = bars[j]["h"], bars[j]["l"]
        if side == "LONG":
            if lo <= stop:                          # stop first on an ambiguous bar
                exit_px, reason = stop, "stop"; break
            if hi >= target:
                exit_px, reason = target, "target"; break
        else:
            if hi >= stop:
                exit_px, reason = stop, "stop"; break
            if lo <= target:
                exit_px, reason = target, "target"; break
    if exit_px is None:
        j = min(start + max_hold, len(bars) - 1)
        exit_px, reason, held = bars[j]["c"], "timeout", max(1, j - start)

    gross = ((exit_px - entry) / entry) if side == "LONG" else ((entry - exit_px) / entry)
    return {"net": (gross - cost) * 100, "reason": reason, "bars": held, "side": side}


def stats(trades):
    n = len(trades)
    if not n:
        return None
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [t["net"] for t in trades if t["net"] <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = abs(sum(losses) / len(losses)) if losses else 0.0
    be = (al / (aw + al) * 100) if (aw + al) else 0.0
    net = sum(t["net"] for t in trades)
    return {"n": n, "wr": len(wins) / n * 100, "net": net, "avg": net / n,
            "avg_win": aw, "avg_loss": -al, "breakeven_wr": be,
            "targets": sum(1 for t in trades if t["reason"] == "target") / n * 100}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=1440,
                    help="candle minutes; 1440 (daily) gives ~2y of history")
    ap.add_argument("--pairs", default="")
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--fill", choices=("taker", "maker"), default="taker")
    ap.add_argument("--compare-fills", action="store_true",
                    help="taker vs maker on identical signals, incl. missed fills")
    args = ap.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()] or DEFAULT_PAIRS
    print(f"fetching {args.interval}m candles for {len(pairs)} pairs...")
    data = {}
    for p in pairs:
        c = fetch(p, args.interval)
        if len(c) > 80:
            data[p] = c
        time.sleep(1.3)                             # Kraken rate-limits hard
    if not data:
        print("no data fetched", file=sys.stderr)
        return 1

    bars_each = sum(len(v) for v in data.values()) / len(data)
    days = bars_each * args.interval / 1440
    print(f"\n{len(data)} pairs, ~{bars_each:.0f} bars each (~{days:.0f} days per pair)")
    print(f"costs: {ROUND_TRIP*100:.2f}% round trip applied to every trade")
    print(f"direction: {'long only' if args.long_only else 'long + short'}")
    print()

    # A small, deliberately coarse sweep. Every row is printed.
    sweep = []
    for lb in (10, 20, 40):
        for st, tg in ((1.5, 3.0), (2.0, 4.0), (2.0, 6.0)):
            sweep.append((lb, st, tg))

    hold = max(10, int(20 * (1440 / args.interval)) if args.interval < 1440 else 20)

    if args.compare_fills:
        print(f"TAKER ({ROUND_TRIP*100:.2f}% round trip, always fills) vs "
              f"MAKER ({ROUND_TRIP_MAKER*100:.2f}%, fills only on a pullback)")
        print()
        print(f"  {'combo':>14s} {'taker n':>8s} {'taker avg':>10s} "
              f"{'maker n':>8s} {'maker avg':>10s} {'missed':>7s} {'missed avg':>11s} {'verdict':>9s}")
        print("  " + "-" * 88)
        for lb, st, tg in sweep:
            tk, mk, ms = [], [], []
            for pair, bars in data.items():
                t1, _ = run_pair(bars, lb, st, tg, hold,
                                 allow_short=not args.long_only, fill="taker")
                t2, m2 = run_pair(bars, lb, st, tg, hold,
                                  allow_short=not args.long_only, fill="maker")
                tk += t1; mk += t2; ms += m2
            s1, s2 = stats(tk), stats(mk)
            sm = stats(ms)
            if not (s1 and s2):
                continue
            verdict = "maker" if s2["avg"] > s1["avg"] else "taker"
            print(f"  {lb:>3d}/{st:.1f}/{tg:.1f}".rjust(16)
                  + f" {s1['n']:>8d} {s1['avg']:>+9.2f}% {s2['n']:>8d} {s2['avg']:>+9.2f}%"
                    f" {len(ms):>7d} {(sm['avg'] if sm else 0):>+10.2f}% {verdict:>9s}")
        print()
        print("  'missed avg' is what the UNFILLED signals would have returned as a")
        print("  taker. If that number is strongly positive, posting passively is")
        print("  skipping the winners and the fee saving is an illusion.")
        return 0

    print(f"max hold {hold} bars | showing ALL {len(sweep)} parameter combinations")
    print()
    print(f"  {'lookback':>8s} {'stop':>5s} {'targ':>5s} {'trades':>7s} {'WR':>7s} "
          f"{'need':>6s} {'NET%':>9s} {'avg%':>7s} {'hit tgt':>8s}")
    print("  " + "-" * 78)

    rows = []
    for lb, st, tg in sweep:
        allt = []
        for pair, bars in data.items():
            t, _m = run_pair(bars, lb, st, tg, hold,
                             allow_short=not args.long_only, fill=args.fill)
            allt += t
        s = stats(allt)
        if not s:
            print(f"  {lb:>8d} {st:>5.1f} {tg:>5.1f} {'0':>7s}  (no trades)")
            continue
        flag = "  <-- beats breakeven" if s["wr"] > s["breakeven_wr"] else ""
        print(f"  {lb:>8d} {st:>5.1f} {tg:>5.1f} {s['n']:>7d} {s['wr']:>6.1f}% "
              f"{s['breakeven_wr']:>5.1f}% {s['net']:>+8.1f}% {s['avg']:>+6.2f}% "
              f"{s['targets']:>7.0f}%{flag}")
        rows.append(((lb, st, tg), s))

    print()
    if rows:
        best = max(rows, key=lambda r: r[1]["net"])
        worst = min(rows, key=lambda r: r[1]["net"])
        pos = sum(1 for _, s in rows if s["net"] > 0)
        print(f"  {pos}/{len(rows)} combinations profitable after costs")
        print(f"  best  {best[0]}: NET {best[1]['net']:+.1f}%  avg {best[1]['avg']:+.2f}%/trade")
        print(f"  worst {worst[0]}: NET {worst[1]['net']:+.1f}%  avg {worst[1]['avg']:+.2f}%/trade")
        print()
        print("  BENCHMARK TO BEAT (live strategy, 82 trades, 2026-07-24..29):")
        print("    win rate 19.5% | avg -0.13%/trade | targets hit 6% | net -10.7%")
        print()
        if pos == 0:
            print("  Donchian also loses after costs on this data. That is informative:")
            print("  the cost floor may simply be too high for any trend rule at this")
            print("  size, which points at fees/holding period rather than the signal.")
        elif pos < len(rows) / 2:
            print("  Only a minority of settings work — weak or regime-dependent edge.")
            print("  Do not deploy on the strength of the best row; that is curve-fitting.")
        else:
            print("  Most settings profitable after costs, which is the signature of a")
            print("  real (if modest) effect rather than a fitted one. Still only")
            print("  one market regime — treat as a floor to beat, not a strategy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
