#!/usr/bin/env python3
"""What does the net-of-cost R:R gate actually cost in trade frequency?

The gate rejects 5 of the 6 positions that were open on 2026-08-05. That is
either the fix working or the bot being switched off, and the difference
matters, so this measures it instead of arguing about it.

Method: run backtest.py's own loop over the same candles twice on a real
universe -- once with the gate forced off, once on -- and compare trades, win
rate and P&L. Nothing is reimplemented; the gate is toggled by monkeypatching
the constant the live code reads, which is the only difference between runs.

A gate that removes trades AND removes money is a bad gate. A gate that removes
trades and improves P&L per trade is doing its job. Both outcomes are reported
plainly.

Usage:
    python measure_cost_gate.py                       # default 8 pairs, 1h
    python measure_cost_gate.py --pairs SOLUSD,LINKUSD --interval 60
"""
import argparse
import sys

import bot_server as bs
import backtest as bt

# The per-signal GATE lines bury the result under thousands of rows.
bs.log = lambda *a, **k: None
bt.bs.log = bs.log


def run_universe(pairs, interval, gate_on, polls):
    """One pass over every pair. Returns aggregate stats."""
    # The ONLY difference between the two runs. Setting the multiplier to 0
    # makes the cost term vanish, which reduces the net gate back to the gross
    # one the bot used before -- i.e. the old behaviour, exactly.
    bs.ROUND_TRIP_COST_PCT = _REAL_COST if gate_on else 0.0

    tot = {"trades": 0, "wins": 0, "pnl": 0.0, "pairs": 0}
    per_pair = []
    for pair in pairs:
        try:
            candles = bt.fetch_kraken(pair, interval)
        except Exception as e:
            print(f"  {pair}: fetch failed ({e})", file=sys.stderr)
            continue
        if len(candles) < 60:
            continue
        name = next((c["name"] for c in bs.SCAN_UNIVERSE if c["pair"] == pair), pair)
        # Fresh trader per pair so one pair's balance cannot mask another's.
        try:
            res = bt.run(candles, pair, name, verbose=False, intrabar_polls=polls)
        except Exception as e:
            print(f"  {pair}: run failed ({e})", file=sys.stderr)
            continue
        # summarize() reports return_pct / win_rate, not raw counts.
        n = res.get("trades", 0)
        w = round(res.get("win_rate", 0.0) / 100 * n)
        pnl = bs.PAPER_START * res.get("return_pct", 0.0) / 100
        per_pair.append((pair, n, w, pnl))
        tot["trades"] += n
        tot["wins"] += w
        tot["pnl"] += pnl
        tot["pairs"] += 1
    return tot, per_pair


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="SOLUSD,LINKUSD,ETHUSD,XBTUSD,ARBUSD,INJUSD,ATOMUSD,NEARUSD")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--polls", type=int, default=3)
    args = ap.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    global _REAL_COST
    _REAL_COST = bs.ROUND_TRIP_COST_PCT

    print(f"round-trip cost the bot pays: {_REAL_COST*100:.3f}%")
    print(f"MIN_RR_RATIO: {bs.MIN_RR_RATIO}")
    print(f"{len(pairs)} pairs, {args.interval}m candles, {args.polls} intrabar polls\n")

    print("running WITHOUT the cost gate (old behaviour)...")
    off, off_pp = run_universe(pairs, args.interval, False, args.polls)
    print("running WITH the cost gate (new behaviour)...")
    on, on_pp = run_universe(pairs, args.interval, True, args.polls)

    print()
    print("=" * 70)
    print(f"  {'pair':10s} {'trades off':>11s} {'trades on':>10s} {'pnl off':>10s} {'pnl on':>10s}")
    print("  " + "-" * 62)
    on_map = {p: (n, w, pnl) for p, n, w, pnl in on_pp}
    for pair, n, w, pnl in off_pp:
        n2, w2, pnl2 = on_map.get(pair, (0, 0, 0.0))
        print(f"  {pair:10s} {n:>11d} {n2:>10d} {pnl:>+9.2f}$ {pnl2:>+9.2f}$")

    print()
    print("=" * 70)
    print("  TOTALS")
    print("=" * 70)
    for label, t in (("gate OFF (old)", off), ("gate ON  (new)", on)):
        wr = t["wins"] / t["trades"] * 100 if t["trades"] else 0.0
        per = t["pnl"] / t["trades"] if t["trades"] else 0.0
        print(f"  {label}:  {t['trades']:4d} trades  {wr:5.1f}% win  "
              f"{t['pnl']:+9.2f}$ total  {per:+7.4f}$ per trade")

    print()
    if off["trades"]:
        cut = (1 - on["trades"] / off["trades"]) * 100
        print(f"  trades removed: {cut:.0f}%")
    if on["trades"] and off["trades"]:
        p_off = off["pnl"] / off["trades"]
        p_on = on["pnl"] / on["trades"]
        print(f"  P&L per trade:  {p_off:+.4f}$ -> {p_on:+.4f}$")
        if p_on > p_off:
            print("  -> the trades it removed were worse than the ones it kept.")
        else:
            print("  -> WARNING: the gate removed trades WITHOUT improving quality.")
    elif on["trades"] == 0:
        print("  -> the gate blocks EVERY trade on this sample. Too strict, or the")
        print("     strategy genuinely has no edge that survives its own costs.")
    print()
    print("  Caveat: Kraken's free OHLC is ~720 candles, macro gates read NEUTRAL")
    print("  in backtest, and exits fire on candle close. Direction of the effect")
    print("  is the signal here, not the exact P&L.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
