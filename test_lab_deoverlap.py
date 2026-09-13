#!/usr/bin/env python3
"""research_lab de-overlap contract — one decision per forward window.

WHY THIS FILE EXISTS
--------------------
The sweep used to count EVERY bar as an independent decision. A rule firing at
bar i with an hz-hour forward window cannot begin another independent decision
until bar i+hz, but `for i in range(a2, b)` accepted a fire on every qualifying
bar, so a 24h test folded ~24 near-identical copies of each real decision into
its own t-statistic. MEASURED at the time of the fix:

  * family_results: 62 of 128 tests (48.4%) passed |t_oos| > 2.5, where a
    correctly calibrated sweep at that bar passes ~1.2%.
  * one test's own record: n_is 543,323 / n_oos 513,611 "decisions" from a
    1.4M-bar corpus.
  * the empirical sd of t under a null that preserves each pair's
    autocorrelation was 4.44, where 1.00 is correct.

The fix was structural, not a patch: evaluators YIELD (bar, side) events and a
single _fold_events applies the spacing. There were 20 separate `fwd[i]` read
sites across 11 evaluators, and a guard added to 19 of them is still the bug.

So these checks are deliberately written to fail if someone reintroduces the
old shape — [2] derives the evaluator list FROM _EVALS rather than from a list
written here, because a hand-maintained list is exactly what misses the
twelfth evaluator.

Plain script: exit 0 == pass.
"""
import io
import os
import re
import sys
import inspect

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_lab as R

FAILS = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ── [1] the fold enforces one decision per window ───────────────────────────
print("[1] _fold_events spacing")

fwd = [0.01] * 100                       # every bar a +1% forward return
every_bar = [(i, R.LONG) for i in range(100)]

nb, sb, qb, ns_, ss_, qs_ = R._fold_events(every_bar, fwd, 1)
check("spacing 1 keeps every event (the pre-fix behaviour, as a control)",
      nb == 100 and ns_ == 0)

nb, sb, qb, ns_, ss_, qs_ = R._fold_events(every_bar, fwd, 24)
check("spacing 24 over 100 bars keeps ceil(100/24) = 5 decisions, not 100",
      nb == 5 and ns_ == 0)

nb, _, _, _, _, _ = R._fold_events(every_bar, fwd, 6)
check("spacing 6 over 100 bars keeps 17", nb == 17)

# the first event always survives; spacing is measured from the last KEPT bar,
# not from the last SEEN bar (otherwise a dense stream starves itself).
kept = R._fold_events([(0, R.LONG), (1, R.LONG), (2, R.LONG), (30, R.LONG)],
                      fwd, 24)
check("spacing measured from last KEPT bar (0 and 30 survive, 1 and 2 do not)",
      kept[0] == 2)

# ── [2] every evaluator is an event generator — derived FROM SOURCE ─────────
print()
print("[2] no evaluator can accumulate a return itself")

for fam, fn in sorted(R._EVALS.items()):
    check("%-18s is a generator function" % fam,
          inspect.isgeneratorfunction(fn))

for fam, fn in sorted(R._EVALS.items()):
    params = list(inspect.signature(fn).parameters)
    check("%-18s takes no forward-return array" % fam, "fwd" not in params)

# The real regression guard: no evaluator body may index a forward array. This
# reads the SOURCE, so an evaluator added later is covered without editing this
# file.
src = io.open(R.__file__, encoding="utf-8").read()
ev_block = src[src.index("def _move_rule"):src.index("_EVALS = {")]
check("no 'fwd[' anywhere in the evaluator block (only _fold_events reads it)",
      "fwd[" not in ev_block)
check("_fold_events is the sole reader of a forward return",
      src.count("fwd[i]") == 1
      and "fwd[i]" in src[src.index("def _fold_events"):
                          src.index("def _baseline_events")])

# ── [3] the baseline is spaced identically to the rules ────────────────────
print()
print("[3] baseline and rule sit on the same effective-n scale")

bn, bs_, _, _, _, _ = R._fold_events(R._baseline_events(0, 100), fwd, 24)
rn = R._fold_events(every_bar, fwd, 24)[0]
check("baseline over [0,100) at hz=24 keeps the same count as a fire-every-bar "
      "rule", bn == rn == 5)

sweep = src[src.index("def _sweep_pair"):src.index("def _finalize_sweep")]
check("_sweep_pair folds the baseline through _fold_events, not math.fsum",
      "_fold_events(_baseline_events" in sweep
      and "math.fsum(fwd[a:b])" not in sweep)
check("_sweep_pair passes hz (not a literal) as the spacing to both folds",
      sweep.count("fwd, hz)") >= 2)

# ── [4] sign convention survived the refactor ──────────────────────────────
print()
print("[4] SHORT still flips the forward return")

up = [0.02] * 50
nb, sb, _, ns_, ss_, _ = R._fold_events([(0, R.LONG)], up, 1)
check("a LONG on a +2% bar books +2%", nb == 1 and abs(sb - 0.02) < 1e-12)
nb, sb, _, ns_, ss_, _ = R._fold_events([(0, R.SHORT)], up, 1)
check("a SHORT on a +2% bar books -2%", ns_ == 1 and abs(ss_ + 0.02) < 1e-12)
check("LONG and SHORT are distinct and signed", R.LONG == 1 and R.SHORT == -1)

# ── [5] degenerate inputs degrade, never crash or fabricate ────────────────
print()
print("[5] honest degradation")

gappy = [0.01] * 10 + [None] * 10 + [0.01] * 10
nb, _, _, _, _, _ = R._fold_events([(i, R.LONG) for i in range(30)], gappy, 1)
check("bars with no hz-return are skipped, not counted as zero", nb == 20)

check("empty event stream returns a zero sextuple, not an error",
      R._fold_events([], fwd, 24) == (0, 0.0, 0.0, 0, 0.0, 0.0))

# A stream that arrives out of order must UNDER-count (drop), never
# double-count: the guard compares against the last kept bar.
out_of_order = [(0, R.LONG), (50, R.LONG), (1, R.LONG), (2, R.LONG)]
nb, _, _, _, _, _ = R._fold_events(out_of_order, fwd, 24)
check("unsorted events never inflate the count", nb <= 2)

# ── [6] the scorer's own assumption is now met ─────────────────────────────
print()
print("[6] _score's independence assumption")

# _score divides by sqrt(n). That is only a t-statistic if the n returns are
# independent draws. Pin the arithmetic that made the old numbers wrong: 24
# overlapping copies inflate a t by ~sqrt(24) = 4.9, which is the gap between
# the measured null sd of 4.44 and the correct 1.0.
n_over = R._fold_events(every_bar, fwd, 1)[0]
n_sp = R._fold_events(every_bar, fwd, 24)[0]
check("overlapping n is ~24x the honest n at hz=24 (the t inflation factor)",
      19 <= n_over / max(1, n_sp) <= 25)

print()
if FAILS:
    print("%d FAILURES" % len(FAILS))
    sys.exit(1)
print("all research_lab de-overlap checks pass")
