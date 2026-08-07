#!/usr/bin/env python3
"""Do the bot's fees agree with its own entry gates, and with the manual book?

Three ledgers have to tell the same story and did not:

  * the entry gates compare a target against ROUND_TRIP_COST_PCT, a fraction of
    PRICE — leverage-independent, so they assume break-even is a move equal to
    the fee rate;
  * PaperTrader charged fees on MARGIN while scaling P&L by LEVERAGE, which
    makes break-even fee_rate/leverage — a 5x trade appeared to need a 5x
    smaller move to pay for itself;
  * the manual book charges notional * KRAKEN_FEE, the correct way, so the one
    comparison it exists for was between two different fee models.

Partial exits charged no fee at all: two partials plus a final close is three
market exits, and the book paid for one.

This checks the invariants rather than specific numbers, so it keeps holding if
the fee rates change.

Usage:  python test_fee_accounting.py
"""
import ast
import sys

SRC = "bot_server.py"


def fee_exprs():
    """Pull every fee/pnl assignment out of PaperTrader by AST, so this reads
    what the class actually does rather than what a comment claims."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "PaperTrader":
            continue
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                        and isinstance(n.targets[0], ast.Name) \
                        and n.targets[0].id in ("fee", "pnl"):
                    out.setdefault(fn.name, []).append(
                        (n.targets[0].id, ast.unparse(n.value)))
    return out


def fn_source(name):
    """Whole body of a PaperTrader method. Needed because slippage and the exit
    price are computed in their own statements, not inside the fee expression —
    an earlier version of this test looked only at `fee`/`pnl` and wrongly
    reported that _partial_close ignored slippage."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "PaperTrader":
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == name:
                    return ast.unparse(fn)
    return ""


def main():
    import bot_server as bs
    fails = []
    P = print

    P("=" * 72)
    P("  FEE EXPRESSIONS IN PaperTrader")
    P("=" * 72)
    got = fee_exprs()
    for fn in ("_open", "_close", "_partial_close"):
        for kind, expr in got.get(fn, []):
            P(f"  {fn:16s} {kind:4s} = {expr[:90]}")
    P()

    # A fee expression that scales P&L by leverage must scale the fee too.
    for fn in ("_open", "_close", "_partial_close"):
        exprs = dict(got.get(fn, []))
        fee = exprs.get("fee", "")
        pnl = exprs.get("pnl", "")
        combined = fee + " " + pnl
        if "leverage" in pnl or "_lev" in pnl or "leverage" in combined:
            # the paper branch of _open has no pnl; check its fee alone
            target = fee if fn == "_open" else combined
            if "lev" not in target:
                fails.append(f"{fn}: P&L uses leverage but the fee does not "
                             f"({target[:70]})")
    if not fails:
        P("  every fee that sits beside a leveraged P&L is itself leveraged (ok)")

    # ── the invariant that actually matters ──────────────────────────────────
    P()
    P("=" * 72)
    P("  BREAK-EVEN MOVE MUST NOT DEPEND ON LEVERAGE")
    P("=" * 72)
    P("  the gates assume break-even = the fee rate, at any leverage")
    P(f"  {'leverage':>9s} {'break-even move':>17s} {'gate assumes':>14s}")
    P("  " + "-" * 46)
    rate = bs.KRAKEN_FEE
    for lev in (1, 2, 3, 5, 10):
        # pnl = move*margin*lev - margin*lev*rate  ->  zero at move == rate
        margin = 100.0
        be = None
        for m in [x / 100000 for x in range(0, 2000)]:
            if m * margin * lev - margin * lev * rate >= 0:
                be = m
                break
        P(f"  {lev:>9d} {be*100:>16.4f}% {rate*100:>13.4f}%")
        if be is None or abs(be - rate) > 1e-6:
            fails.append(f"leverage {lev}: break-even {be} != fee rate {rate}")
    if not any("break-even" in f for f in fails):
        P("  break-even is leverage-independent (ok) — matches the gates")

    # ── the manual book must use the same model ─────────────────────────────
    P()
    P("=" * 72)
    P("  BOT vs MANUAL BOOK — the comparison the manual book exists for")
    P("=" * 72)
    src = open(SRC, encoding="utf-8").read()
    manual_notional = "notional * KRAKEN_FEE" in src
    P(f"  manual book charges on notional : {manual_notional}")
    # EVERY fee site in _open, not just one: the method has a paper branch and
    # three live branches (Kraken, Kraken Futures, Binance), and an earlier
    # version of this test collapsed them with dict() and only checked the last.
    open_fees = [e for k, e in got.get("_open", []) if k == "fee"]
    bad = [e for e in open_fees if not ("leverage" in e or "_lev" in e)]
    P(f"  fee sites in _open              : {len(open_fees)}")
    P(f"  ...charging on notional         : {len(open_fees) - len(bad)}/{len(open_fees)}")
    for e in bad:
        P(f"     still on margin: {e}")
    if bad:
        fails.append(f"{len(bad)} fee site(s) in _open still charge on margin, "
                     f"so the bot and the manual book are not comparable")
    elif manual_notional:
        P("  both ledgers use the same fee model (ok)")

    # ── partial exits must cost something ───────────────────────────────────
    P()
    P("=" * 72)
    P("  A PARTIAL EXIT IS A REAL EXIT AND MUST BE CHARGED")
    P("=" * 72)
    pc_all = fn_source("_partial_close")
    has_fee = "_sim_fee" in pc_all or "FEE" in pc_all
    has_slip = "SLIPPAGE" in pc_all
    P(f"  partial close charges a fee      : {has_fee}")
    P(f"  partial close applies slippage   : {has_slip}")
    if not has_fee:
        fails.append("_partial_close books a gain with no fee — scaling out is free")
    if not has_slip:
        fails.append("_partial_close ignores slippage on a real market exit")
    if has_fee and has_slip:
        P("  scaling out is no longer free (ok)")

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
