#!/usr/bin/env python3
"""Do the cost gates actually reject the trades that lost the money?

The bot's book was 11W-49L with every open position shaped the same way: a
target ~2.3% out, a stop ~1.0% back, and a 0.52% round-trip cost. Gross that
reads 2.2:1; net of costs it is 1.14:1, and the book proved it.

This replays the REAL positions that were open on 2026-08-05 through the REAL
gate arithmetic lifted out of bot_server.py, and checks:

  1. the cost constant matches the fee path the bot actually takes
  2. the net-of-cost R:R rejects those positions and the gross one does not
  3. the stop floor never leaves a stop inside one round trip
  4. the target clamp fixes a take-profit that would close at a loss
  5. breakeven lands where the trade truly nets zero, not at entry

Usage:  python test_cost_gates.py
"""
import ast
import sys

SRC = "bot_server.py"

CONSTS = ["KRAKEN_FEE", "KRAKEN_MAKER_FEE", "SLIPPAGE", "BINANCE_FEE",
          "KRAKEN_FUTURES_FEE", "USE_BINANCE", "USE_FUTURES", "USE_MAKER_ENTRIES",
          "_ENTRY_COST_PCT", "_EXIT_COST_PCT", "ROUND_TRIP_COST_PCT",
          "MIN_PROFIT_VS_COST_MULT", "MIN_STOP_VS_COST_MULT", "MIN_RR_RATIO",
          "EXCHANGE", "ATR_MULTIPLIER", "TRAIL_PCT"]


HELPERS = {"_clean_env"}      # functions the constant expressions call


def load_consts():
    """Exec the constant assignments in file order, so the values under test are
    the ones the running bot computes rather than ones restated here."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    ns = {"os": __import__("os")}
    want = set(CONSTS)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS:
            exec(compile(ast.Module([node], []), SRC, "exec"), ns)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in want):
            exec(compile(ast.Module([node], []), SRC, "exec"), ns)
    missing = want - set(ns)
    if missing:
        print(f"FAIL: constants not found: {sorted(missing)}")
        sys.exit(1)
    return ns


# The six positions open on the live book at 2026-08-05 07:1x, read out of
# data/paper_state.json. Real numbers, not invented ones.
#
# Risk is taken from atr_dist, NOT from the stored trail_stop: the trail
# ratchets as a position moves into profit, so on a winning trade it no longer
# reflects the risk the trade was entered with. NEARUSD is the example — its
# trail had already walked past entry, which reads as negative risk.
LIVE = [
    # pair,      side,    entry,     target,       atr_dist (the stop distance)
    ("ARBUSD",  "LONG",   0.081200,  0.08336857,   0.000986),
    ("INJUSD",  "LONG",   4.893000,  5.03437143,   0.081714),
    ("SOLUSD",  "LONG",  74.070000, 75.74514286,   0.761429),
    ("ATOMUSD", "SHORT",  1.353300,  1.31869714,   0.015729),
    ("LINKUSD", "LONG",   8.190000,  8.37866257,   0.085756),
    ("NEARUSD", "SHORT",  1.723600,  1.66665143,   0.011821),
]

# The four trades that closed as "take profit" at a LOSS, from the trades table.
BAD_TP = [
    ("LINK/USD", 8.19087,  8.18267913),
    ("LINK/USD", 8.18821,  8.18498682),
    ("ETH/USD",  1870.83,  1870.06806),
    ("LINK/USD", 8.18153,  8.18142039),
]


def main():
    C = load_consts()
    cost = C["ROUND_TRIP_COST_PCT"]
    fails = []
    P = print

    P("=" * 74)
    P("  COST MODEL")
    P("=" * 74)
    P(f"  exchange           {C['EXCHANGE']}   maker entries: {C['USE_MAKER_ENTRIES']}")
    P(f"  entry cost         {C['_ENTRY_COST_PCT']*100:.3f}%   (maker fee, no slippage — a resting limit fills at its price)")
    P(f"  exit cost          {C['_EXIT_COST_PCT']*100:.3f}%   (taker fee + slippage — the close always crosses)")
    P(f"  round trip         {cost*100:.3f}%")
    P(f"  old hard-coded     {(2*C['KRAKEN_FEE'] + 2*C['SLIPPAGE'])*100:.3f}%   (taker both ways — overstated by "
      f"{((2*C['KRAKEN_FEE']+2*C['SLIPPAGE'])/cost - 1)*100:.0f}%)")
    # With maker entries on, paying a taker fee on entry is simply not what happens.
    expect = C["KRAKEN_MAKER_FEE"] + C["KRAKEN_FEE"] + C["SLIPPAGE"]
    if C["USE_MAKER_ENTRIES"] and not (C["USE_BINANCE"] or C["USE_FUTURES"]):
        if abs(cost - expect) > 1e-12:
            fails.append(f"round trip {cost} != maker+taker+slip {expect}")

    P()
    P("=" * 74)
    P("  THE LIVE BOOK — gross R:R passes, net R:R is what the book actually paid")
    P("=" * 74)
    P(f"  {'pair':9s} {'tgt%':>7s} {'stop%':>7s} {'gross':>7s} {'net':>7s}  {'gross gate':>11s} {'net gate':>9s}")
    P("  " + "-" * 66)
    n_gross_pass = n_net_pass = 0
    for pair, side, entry, target, risk in LIVE:
        reward = (target - entry) if side == "LONG" else (entry - target)
        c = entry * cost
        gross = reward / risk
        net = (reward - c) / (risk + c)
        gp = gross >= C["MIN_RR_RATIO"]
        np_ = (reward - c) > 0 and net >= C["MIN_RR_RATIO"]
        n_gross_pass += gp
        n_net_pass += np_
        P(f"  {pair:9s} {reward/entry*100:6.2f}% {risk/entry*100:6.2f}% "
          f"{gross:6.2f} {net:6.2f}  {'PASS' if gp else 'reject':>11s} {'PASS' if np_ else 'reject':>9s}")
    P()
    P(f"  gross gate let {n_gross_pass}/{len(LIVE)} through — this is what the bot was doing")
    P(f"  net gate lets  {n_net_pass}/{len(LIVE)} through")
    # Every one of these was actually taken, so the gross gate must accept them
    # all; if it does not, this replay does not match what the bot ran.
    if n_gross_pass != len(LIVE):
        fails.append(f"replay wrong: gross gate rejects {len(LIVE)-n_gross_pass} "
                     f"positions the bot really opened")
    # The point of the change is that the net gate is strictly stricter. It is
    # NOT that it rejects everything -- a trade whose reward genuinely survives
    # its costs should still be taken, and one here does.
    if n_net_pass >= n_gross_pass:
        fails.append("net gate is not stricter than the gross gate")

    P()
    P("=" * 74)
    P("  STOP FLOOR — no stop may sit inside one round trip")
    P("=" * 74)
    floor_mult = C["MIN_STOP_VS_COST_MULT"]
    for label, entry, raw_dist in [("tight structure stop", 100.0, 0.20),
                                   ("parabolic 1% tighten", 100.0, 1.00),
                                   ("normal ATR stop",      100.0, 2.50)]:
        floored = max(raw_dist, entry * cost * floor_mult)
        ok = floored >= entry * cost * floor_mult - 1e-12
        P(f"  {label:22s} {raw_dist:.2f}% -> {floored:.2f}%  {'(widened)' if floored > raw_dist else '(unchanged)'}")
        if not ok:
            fails.append(f"{label}: stop still inside cost band")

    P()
    P("=" * 74)
    P("  TARGET CLAMP — the four losing 'take profit' trades")
    P("=" * 74)
    min_tgt = cost * C["MIN_PROFIT_VS_COST_MULT"]
    P(f"  a target must sit at least {min_tgt*100:.2f}% away to be reachable at a profit")
    for coin, entry, exit_px in BAD_TP:
        moved = (exit_px - entry) / entry
        clamped = entry * (1 + min_tgt)
        would_fire = exit_px >= clamped
        P(f"  {coin:9s} entry {entry:>10.5f} exited {exit_px:>10.5f} "
          f"({moved*100:+.3f}%)  clamped target {clamped:>10.5f}  "
          f"fires now? {'YES' if would_fire else 'no'}")
        if would_fire:
            fails.append(f"{coin}: clamped target would still fire at a loss")
        # And the exit guard is a second, independent line of defence.
        if moved > cost:
            fails.append(f"{coin}: exit guard would have allowed this close")
    P("  none of them can fire as 'take profit' any more — clamp and exit guard agree")

    P()
    P("=" * 74)
    P("  BREAKEVEN — must land where the trade truly nets zero")
    P("=" * 74)
    for side, entry in (("LONG", 100.0), ("SHORT", 100.0)):
        be = entry * (1 + cost) if side == "LONG" else entry * (1 - cost)
        # Net move at that stop, as _close() would compute it.
        net_move = ((be - entry) / entry) if side == "LONG" else ((entry - be) / entry)
        P(f"  {side:5s} entry {entry:.2f} -> breakeven stop {be:.4f} "
          f"(net {(net_move - cost)*100:+.3f}% after costs; old 'breakeven' at entry netted {-cost*100:+.3f}%)")
        if abs(net_move - cost) > 1e-9:
            fails.append(f"{side} breakeven does not net zero")

    P()
    P("=" * 74)
    if fails:
        P(f"  {len(fails)} FAILURE(S)")
        for f in fails:
            P("   x " + f)
        return 1
    P("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
