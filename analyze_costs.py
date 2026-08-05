#!/usr/bin/env python3
"""Measure the cost problem against the REAL trade book, not a backtest.

backtest.py cannot answer this: on ~720 free candles it produces about one
closed trade per pair, and a conclusion drawn from eight trades is how this
project previously "discovered" a Donchian edge that reversed on the full
universe. The live paper book has 130 real closed trades with real fills, so
that is what gets measured.

The question is simple and does not need a simulation: for each closed trade,
how big was the price move compared to what the round trip cost? A strategy
whose typical move is smaller than its own fees cannot be rescued by a better
win rate.

Usage:
    python analyze_costs.py                      # reads data/paper_state.json
    python analyze_costs.py --state path.json
"""
import argparse
import json
import os
import statistics
import sys

import bot_server as bs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.path.join(
        os.environ.get("DATA_DIR", "."), "paper_state.json"))
    args = ap.parse_args()

    try:
        state = json.load(open(args.state, encoding="utf-8"))
    except OSError:
        print(f"no state file at {args.state}\n"
              f"pull one:  scp noluv@10.0.0.88:~/cryptobot/data/paper_state.json .")
        return 2

    trades = [t for t in state.get("trades", []) if t.get("entry") and t.get("exit")]
    if not trades:
        print("no closed trades in the state file")
        return 2

    cost = bs.ROUND_TRIP_COST_PCT
    P = print

    # Gross price move, signed so that positive always means "the call was right".
    for t in trades:
        m = (t["exit"] - t["entry"]) / t["entry"]
        t["_move"] = -m if t.get("side") == "SHORT" else m

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    P("=" * 74)
    P(f"  {len(trades)} closed trades from the live paper book")
    P("=" * 74)
    P(f"  round-trip cost           {cost*100:.3f}% of price")
    P(f"  record                    {len(wins)}W-{len(losses)}L "
      f"({len(wins)/len(trades)*100:.0f}% win)")
    P(f"  net P&L                   {sum(t['pnl'] for t in trades):+.2f}$")

    P()
    P("  PRICE MOVE PER TRADE (before costs)")
    P("  " + "-" * 68)
    for label, grp in (("winners", wins), ("losers", losses)):
        if not grp:
            continue
        mv = [t["_move"] for t in grp]
        P(f"  {label:8s} n={len(grp):3d}  median {statistics.median(mv)*100:+6.3f}%  "
          f"mean {statistics.fmean(mv)*100:+6.3f}%  "
          f"best {max(mv)*100:+6.2f}%  worst {min(mv)*100:+6.2f}%")

    # The decisive number: a winning trade has to out-run the round trip, and
    # a losing one is made worse by it. If the typical move on either side is
    # smaller than the cost, the cost IS the strategy's result.
    P()
    P("  MOVE vs COST — how many trades even cleared the fee?")
    P("  " + "-" * 68)
    small = [t for t in trades if abs(t["_move"]) < cost]
    win_small = [t for t in wins if t["_move"] < cost]
    P(f"  trades whose move was smaller than the round trip : "
      f"{len(small):3d}/{len(trades)} ({len(small)/len(trades)*100:.0f}%)")
    P(f"  WINNERS that did not clear the round trip         : "
      f"{len(win_small):3d}/{max(len(wins),1)}")
    if wins:
        P(f"  median winner {statistics.median([t['_move'] for t in wins])*100:+.3f}% "
          f"vs {cost*100:.3f}% cost")
    if losses:
        P(f"  median loser  {statistics.median([t['_move'] for t in losses])*100:+.3f}% "
          f"vs {cost*100:.3f}% cost")

    P()
    P("  WHAT WOULD BREAK EVEN")
    P("  " + "-" * 68)
    if wins and losses:
        aw = statistics.fmean([t["_move"] for t in wins]) - cost
        al = abs(statistics.fmean([t["_move"] for t in losses])) + cost
        P(f"  average win after costs   {aw*100:+.3f}%")
        P(f"  average loss after costs  {-al*100:+.3f}%")
        if aw > 0:
            need = al / (aw + al) * 100
            P(f"  win rate needed to break even: {need:.0f}%  (actual "
              f"{len(wins)/len(trades)*100:.0f}%)")
            if need > len(wins) / len(trades) * 100:
                P(f"  -> the book loses because the payoff is upside down, not")
                P(f"     because the signal picks the wrong direction.")
        else:
            P("  -> the average WINNER does not clear costs. No win rate saves this.")

    P()
    P("  EXIT REASONS")
    P("  " + "-" * 68)
    by = {}
    for t in trades:
        r = t.get("reason", "?")
        d = by.setdefault(r, {"n": 0, "w": 0, "pnl": 0.0, "moves": []})
        d["n"] += 1
        d["pnl"] += t["pnl"]
        d["moves"].append(t["_move"])
        if t["pnl"] > 0:
            d["w"] += 1
    P(f"  {'reason':16s} {'n':>4s} {'wins':>5s} {'median move':>12s} {'pnl':>9s}")
    for r, d in sorted(by.items(), key=lambda x: -x[1]["n"]):
        P(f"  {r:16s} {d['n']:>4d} {d['w']:>5d} "
          f"{statistics.median(d['moves'])*100:>11.3f}% {d['pnl']:>+8.2f}$")

    P()
    P("=" * 74)
    P("  This measures the trades that were TAKEN. It cannot say how many the")
    P("  new gate would have refused -- targets and stops are not stored per")
    P("  trade -- so read it as evidence about the payoff shape, not as a")
    P("  backtest of the fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
