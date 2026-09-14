#!/usr/bin/env python3
"""Directional-evidence gate + basket scoring (B1/B3).

WHY THIS FILE EXISTS
--------------------
Every candidate was scored on RAW forward return, which in a rising market is
mostly the market. MEASURED on the bot's own rows: 2,000 no-skill placebo
strategies scored that way have mean t = +1.878 (should be 0) and a 36.4%
false-positive rate against a nominal 5%.

Scoring against a time-matched equal-weight basket fixes the benchmark, but it
does NOT by itself establish that a rule knows which WAY a pair will move. The
live book scores +0.700% on its BUY side and +0.499% on its SELL side - both
positive, when directional skill requires the SELL side to be negative. What it
has is an ATTENTION effect: pairs it fires on in either direction beat the
basket by +0.532%, pairs it ignores by -0.101%.

So the gate contrasts a rule against its own opposite side - the only placebo
with the same generator, pairs, hours and conditioning.

The checks below are written so that the attention artifact FAILS them. A gate
that cannot fail is not a gate; [2] is the part that matters.

Plain script: exit 0 == pass.
"""
import io
import os
import re
import sys
import math
import random

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot as A

D = A.Autopilot._direction_stats
FAILS = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILS.append(name)


def side(mu, sd, n, seed):
    r = random.Random(seed)
    return [r.gauss(mu, sd) for _ in range(n)]


# ── [1] the state machine ───────────────────────────────────────────────────
print("[1] direction states")

check("real skill (BUY beats, SELL trails) confirms",
      D(side(+0.010, 0.06, 300, 1), side(-0.010, 0.06, 300, 2))["state"]
      == "confirmed")
check("anti-directional (SELL beats BUY) is refuted",
      D(side(-0.006, 0.06, 300, 3), side(+0.015, 0.06, 300, 4))["state"]
      == "refuted")
check("one side only -> 'one-sided', never a pass",
      D(side(0.01, 0.06, 300, 5), [])["state"] == "one-sided"
      and D([], side(0.01, 0.06, 300, 6))["state"] == "one-sided")
check("no data at all -> 'not measured'", D([], [])["state"] == "not measured")
check("below the per-side floor -> 'undetermined'",
      D(side(+0.05, 0.01, 5, 7), side(-0.05, 0.01, 5, 8))["state"]
      == "undetermined")
check("zero variance degrades, never divides by zero",
      D([0.01] * 50, [0.01] * 50)["state"] == "undetermined")

# ── [2] THE ARTIFACT THIS GATE EXISTS FOR ──────────────────────────────────
print()
print("[2] the attention effect must not pass")

# Both sides beat the basket by the same amount: the rule found MOVERS, not
# DIRECTION. This is the live book's actual shape, and a benchmark that only
# asked "does it beat the basket" scores it +0.6% and calls it edge.
att = D(side(+0.007, 0.06, 400, 11), side(+0.007, 0.06, 400, 12))
check("both sides equally positive does NOT confirm",
      att["state"] != "confirmed")
check("...and its xs_dir is ~0, not ~+0.7%", abs(att["xs_dir"]) < 0.002)

# The same stream scored the OLD way (signed excess pooled, no side contrast)
# looks strongly positive - that is the false positive being prevented.
pooled = [x for x in side(+0.007, 0.06, 400, 11)] + \
         [-x for x in side(+0.007, 0.06, 400, 12)]
check("a pooled signed score on that same stream is near zero too "
      "(the SELL side is flipped, so attention cancels there as well)",
      abs(sum(pooled) / len(pooled)) < 0.002)

# An attention effect that is STRONGER on the buy side still must not confirm
# unless the gap itself is significant.
att2 = D(side(+0.009, 0.06, 300, 13), side(+0.007, 0.06, 300, 14))
check("a small buy-side tilt inside the noise stays undetermined",
      att2["state"] == "undetermined")

# ── [3] sign and arithmetic ────────────────────────────────────────────────
print()
print("[3] xs_dir arithmetic")

r = D([0.02] * 100, [-0.02] * 100)
check("xs_dir = (mean_buy - mean_sell)/2", abs(r["xs_dir"] - 0.02) < 1e-12)
check("counts are reported per side", r["n_buy"] == 100 and r["n_sell"] == 100)
r2 = D([0.01] * 100, [0.01] * 100)
check("equal sides give exactly zero", abs(r2["xs_dir"]) < 1e-15)

# ── [4] thresholds are reused, not invented ────────────────────────────────
print()
print("[4] no threshold fitted to this dataset")

check("per-side floor IS the project's existing decisions floor",
      A.DIRECTION_MIN_PER_SIDE == A.MIN_SR_CONTRIB_DECISIONS)
check("t bar IS the project's existing multiple-comparison margin",
      A.DIRECTION_T_BAR == A.T_MARGIN)

src = io.open(A.__file__, encoding="utf-8").read()
blk = src[src.index("DIRECTION_MIN_PER_SIDE"):src.index("DIRECTION_T_BAR") + 200]
check("both are defined from existing constants, not numeric literals",
      "= MIN_SR_CONTRIB_DECISIONS" in blk and "= T_MARGIN" in blk)

# ── [5] the verdict must not travel onto a stream it did not measure ───────
print()
print("[5] provenance")

# The cf->res whitelist at the top of that block copies keys UNCONDITIONALLY —
# it sits outside the `n_oos < MIN_OOS_TRADES` guard. A direction verdict listed
# there would carry the shadow counterfactual's answer onto a record the LIVE
# book is driving. `base` is exactly that shape.
wl = src[src.index('for k in ("status", "worst_neg_funding_days"'):]
wl = wl[:wl.index("\n")]
for k in ("direction_state", "xs_dir", "xs_edge"):
    check("%-15s is NOT in the unconditional cf->res whitelist" % k,
          k not in wl)
check("direction keys are copied only inside the cf-drives-the-record branch",
      'for k in ("direction_state", "xs_dir", "xs_dir_t",' in src)
check("a live-driven record says so explicitly",
      '"not measured (live book)"' in src)
check("an entrant with no counterfactual defaults to 'not measured'",
      '"direction_state": "not measured",' in src)

# ── [6] what the gate is allowed to do ─────────────────────────────────────
print()
print("[6] gate semantics: block promotion, never kill on low power")

elig = src[src.index("eligible = {cid: s for cid, s in scores.items()"):]
elig = elig[:elig.index("prev = self.champion_id")]
check("only 'refuted' is excluded from eligibility",
      'direction_state") != "refuted"' in elig)
for s in ("undetermined", "one-sided", "not measured"):
    check("'%s' is NOT excluded from eligibility" % s, s not in elig)

prov = src[src.index("def _apply_proven_rule"):]
prov = prov[:prov.index("def _announce_clears")]
check("PROVEN requires 'confirmed'", 'dstate != "confirmed"' in prov)

# The invariant is that this rule never MUTATES kill state — not that the word
# is absent, which it never will be (the rule reads `cid in self.killed`, and
# the comment explaining the gate says "Nothing is KILLED here"). Strip
# docstrings and comments, then look for writes.
_body = re.sub(r'""".*?"""', "", prov, flags=re.S)
_body = re.sub(r"#[^\n]*", "", _body)
check("the proven rule never mutates kill state",
      not any(m in _body for m in ('self.killed[', 'self.killed.pop',
                                   'del self.killed', '= "KILLED"')))
check("...and the graveyard is only ever READ there",
      _body.count("self.killed") == 1 and "cid in self.killed" in _body)
check("a blocked promotion explains itself in the verdict",
      "not PROVEN without directional evidence" in prov)

# ── [7] the basket is leave-one-out ────────────────────────────────────────
print()
print("[7] leave-one-out basket")

# With k pairs, the plain mean subtracts 1/k of the signal's own return from
# itself, shrinking the estimate toward zero. MEASURED on the live book:
# 0.7000% plain vs 0.7191% LOO across 34 pairs — a 2.7% shrink, matching 1/34.
sc = src[src.index("def _score_cf_price"):]
sc = sc[:sc.index("def _cf_result")] if "def _cf_result" in sc else sc[:20000]
check("the own pair is removed from its own benchmark",
      "(bk[0] - float(fwd)) / (bk[1] - 1)" in sc)
check("a one-pair hour cannot produce a benchmark", "bk[1] > 1" in sc)

k, own, others = 34, 0.05, 0.01
total = own + others * (k - 1)
loo = (total - own) / (k - 1)
check("LOO arithmetic recovers the other pairs' mean exactly",
      abs(loo - others) < 1e-12)

# ── [8] additive, not a redefinition ───────────────────────────────────────
print()
print("[8] stored history stays comparable")

# Every oos_edge ever persisted was RAW net-of-cost vs cash. If that field
# silently became basket excess, old and new records would sit side by side in
# the same table meaning different things.
check("oos_edge is still built from the raw net stream",
      "nets.append((gross - bs.ROUND_TRIP_COST_PCT, gross, float(ts)))" in sc)
check("the basket number arrives as a SEPARATE field",
      '"xs_edge"' in sc)
check("clears_cost still compares gross to the round-trip cost",
      "gross_bar=bs.ROUND_TRIP_COST_PCT" in sc)

print()
if FAILS:
    print("%d FAILURES" % len(FAILS))
    sys.exit(1)
print("all direction-gate checks pass")
