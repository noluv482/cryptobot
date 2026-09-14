#!/usr/bin/env python3
"""Does the sweep report an edge where there is provably none?

THE FAILURE THIS GUARDS
-----------------------
For months the nightly lab reported that roughly half its tests cleared
|t_oos| > 2.5, a bar a correctly calibrated sweep clears about 1.2% of the
time. Nothing caught it, because every existing test asks "is this function
correct?" and none asks "does the whole machine agree that noise is noise?"

The bug was that every bar counted as an independent decision, so an hz-hour
forward window folded ~hz near-identical copies of each real decision into its
own t-statistic. But that is only the instance. The CLASS is "the instrument
reports signal on data that contains none", and overlap is one of several ways
to land there — a leaked forward price, a mis-specified baseline, a variance
estimate on correlated rows, or a future change nobody has thought of yet would
all present identically and all pass the other 41 test files.

So this test does not check any function. It runs the REAL grid through the
REAL scorer over a corpus built from independent random walks, where the true
edge of every rule is exactly zero by construction, and asserts the machine
says so.

WHY SYNTHETIC DATA
------------------
Real history might genuinely contain an effect, so a high t there is ambiguous.
Here it cannot be: the generator has no memory, no cross-pair coupling and no
drift, so ANY rule's expected edge is zero and every t past the bar is a false
positive by definition. The corpus is seeded, so the result is deterministic
and nothing needs committing.

Plain script: exit 0 == pass. Runtime a few seconds.
"""
import io
import os
import sys
import math
import random
import statistics

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_lab as R

FAILS = []

# Headroom, not a fitted value. With 128 tests at a nominal ~1.24% rate the
# expected number of false positives is about 1.6, so a handful is consistent
# with correct behaviour and only a large excess is evidence of a broken
# instrument. The seed makes the run deterministic regardless.
MAX_PASS_RATE = 0.05        # 5% vs ~1.2% nominal; the bug produced 20-50%
MAX_ABS_T     = 4.5         # the bug reached 9-10 on real data

N_PAIRS  = 10
N_BARS   = 2500
SEED     = 20260913


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("   " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def synth(seed, n_pairs=N_PAIRS, n_bars=N_BARS):
    """Independent driftless random walks. No memory, no cross-pair coupling,
    no trend — so the true edge of EVERY rule in _GRID is exactly zero."""
    rnd = random.Random(seed)
    out = {}
    for p in range(n_pairs):
        c = 100.0
        closes, highs, lows, vols = [], [], [], []
        for _ in range(n_bars):
            c *= math.exp(rnd.gauss(0.0, 0.004))
            hi = c * (1 + abs(rnd.gauss(0, 0.0015)))
            lo = c * (1 - abs(rnd.gauss(0, 0.0015)))
            closes.append(c)
            highs.append(max(hi, c))
            lows.append(min(lo, c))
            vols.append(abs(rnd.gauss(1000, 250)) + 1.0)
        out["SYN%02d" % p] = (closes, highs, lows, vols)
    return out


def sweep(corpus, spacing_mode):
    """Run the whole grid. spacing_mode 'correct' spaces by hz; 'overlapping'
    is the pre-fix behaviour, kept so this test can prove it has teeth."""
    acc = {"%s_h%d" % (s, hz): {"is": [0] * 6, "oos": [0] * 6}
           for s, _, _ in R._GRID for hz in R.HORIZONS}
    base = {"h%d_%s" % (hz, w): [0, 0.0] for hz in R.HORIZONS for w in ("is", "oos")}
    for pair, (c, h, l, v) in corpus.items():
        pre = R._precompute(c, h, l, v)
        n = len(c)
        for hz in R.HORIZONS:
            fwd = pre["fwd"][hz]
            sp = hz if spacing_mode == "correct" else 1
            for wname, lo, hi in (("is", 0.0, 0.5), ("oos", 0.5, 1.0)):
                a = max(R.WARMUP, int(n * lo))
                b = min(int(n * hi), n - hz - 1)
                if hi < 1.0:
                    b = min(b, int(n * hi) - hz)
                if b <= a:
                    continue
                bn, bs_ = R._fold_events(R._baseline_events(a, b), fwd, sp)[:2]
                base["h%d_%s" % (hz, wname)][0] += bn
                base["h%d_%s" % (hz, wname)][1] += bs_
                for slug, fam, params in R._GRID:
                    r = R._fold_events(
                        R._EVALS[fam](pre, c, h, l, v, params, a, b), fwd, sp)
                    slot = acc["%s_h%d" % (slug, hz)][wname]
                    for j in range(6):
                        slot[j] += r[j]
    ts = []
    for slug, _f, _p in R._GRID:
        for hz in R.HORIZONS:
            e = acc["%s_h%d" % (slug, hz)]
            _ni, _ei, t_i = R._score(e["is"], *base["h%d_is" % hz])
            _no, _eo, t_o = R._score(e["oos"], *base["h%d_oos" % hz])
            if t_i is not None and t_o is not None:
                ts.append(t_o)
    return ts


print("[1] the machine on data with provably zero edge")
print("    %d independent random walks x %d bars, seed %d" % (N_PAIRS, N_BARS, SEED))

corpus = synth(SEED)
ts = sweep(corpus, "correct")
n = len(ts)
hits = [t for t in ts if abs(t) > R.T_OOS_BAR]
rate = len(hits) / max(1, n)
mx = max((abs(t) for t in ts), default=0.0)

print("    scored %d tests, %d past |t|>%.1f (%.1f%%), max |t| %.2f"
      % (n, len(hits), R.T_OOS_BAR, 100 * rate, mx))

check("the grid actually produced scorable tests", n >= 100, "n=%d" % n)
check("pass rate is near nominal, not the ~50%% the overlap bug produced",
      rate <= MAX_PASS_RATE, "%.1f%% (bar %.0f%%)" % (100 * rate, 100 * MAX_PASS_RATE))
check("no test reaches an implausible |t| on pure noise",
      mx < MAX_ABS_T, "max %.2f (bar %.1f)" % (mx, MAX_ABS_T))

# ── [2] does this test have teeth? ────────────────────────────────────────
# A calibration test that cannot fail is worth nothing. Re-run the identical
# corpus with the pre-fix spacing and require the numbers to get materially
# worse — if they do not, this file is not measuring what it claims to.
print()
print("[2] the same corpus through the PRE-FIX scorer, to prove this can fail")
ts_bug = sweep(corpus, "overlapping")
hits_bug = [t for t in ts_bug if abs(t) > R.T_OOS_BAR]
rate_bug = len(hits_bug) / max(1, len(ts_bug))
mx_bug = max((abs(t) for t in ts_bug), default=0.0)
print("    overlapping: %d past the bar (%.1f%%), max |t| %.2f"
      % (len(hits_bug), 100 * rate_bug, mx_bug))

check("the pre-fix scorer inflates |t| on the same noise",
      mx_bug > mx * 1.3,
      "%.2f vs %.2f" % (mx_bug, mx))
check("...and this test would have FAILED against it",
      rate_bug > MAX_PASS_RATE or mx_bug >= MAX_ABS_T,
      "rate %.1f%%, max |t| %.2f" % (100 * rate_bug, mx_bug))

# ── [3] the guard is wired to the real constants ──────────────────────────
print()
print("[3] wiring")
check("the sweep under test is the shipped grid, not a copy",
      len(R._GRID) >= 32 and len(R.HORIZONS) >= 2,
      "%d rules x %d horizons" % (len(R._GRID), len(R.HORIZONS)))
check("the bar under test is the shipped bar", R.T_OOS_BAR == 2.5)
src = io.open(R.__file__, encoding="utf-8").read()
check("_GRID_SIG names the fold, so a semantics change invalidates a resume",
      "-spaced" in src and "_GRID_SIG" in src)

print()
if FAILS:
    print("%d FAILURES" % len(FAILS))
    sys.exit(1)
print("all calibration checks pass")
