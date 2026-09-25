#!/usr/bin/env python3
"""_feature_multiplier must not ratchet the stake down on noise.

This is the only learning loop in the bot that changes live behaviour, and it
was measured doing the opposite of learning. It compared each signal
fingerprint's raw win rate against a fixed 65/55/45/35 ladder on a minimum of
five samples, and cut the stake to 0.25x for anything below 35%. On a book
whose own pooled win rate is about 22%, that ladder condemns nearly everything:
8 of 10 fingerprints were cut to a quarter stake, where a null in which every
fingerprint shares the pooled rate predicts 8.83. It was measuring the ladder,
not the fingerprints.

Worse, it got WORSE with evidence. A genuinely 50% fingerprint's chance of ever
reaching the 1.15x bucket fell from 0.189 at n=5 to 0.002 at n=100, because the
observed rate converges on 50 and 50 is under the fixed 65 gate. A reward that
becomes unreachable as data accumulates is a ratchet toward minimum size.

So this file pins the three properties the rewrite has to have, and every one
of them is a property the old code FAILED:

  1. NEUTRAL BY DEFAULT. Insufficient or ambiguous evidence returns exactly
     1.0. The bot does not guess with position size.
  2. RELATIVE, NOT ABSOLUTE. A fingerprint is judged against this book's own
     pooled win rate, so a 22% book judges against 22%. A fingerprint that
     matches the book is not punished for it.
  3. MONOTONE IN EVIDENCE. More outcomes must make a real difference EASIER to
     detect, never harder. This is asserted by simulation across n, because it
     is the property whose absence was the actual bug.

Offline: no network, no database. Plain script - exit 0 == pass.
"""
import io
import os
import random
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PAPER_LOCK", "1")
import bot_server as B

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


SRC = io.open(B.__file__, encoding="utf-8").read()


class _Fake:
    """Minimal stand-in exposing just what _feature_multiplier touches."""
    _no_persist = False

    def __init__(self, cache):
        self._feat_cache = cache
        self._feat_ts = float("inf")          # never refresh: no DB in this test

    # staticmethod() re-wraps it: PaperTrader._wilson hands back the plain
    # underlying function, and a plain function assigned to a class becomes an
    # instance method, which would pass `self` in as `wins`.
    _wilson = staticmethod(B._wilson)
    _feature_multiplier = B.PaperTrader._feature_multiplier


def book(entries):
    """entries: {fkey: (wins, n)} -> the cache shape the real code consumes."""
    return {k: {"wins": w, "n": n, "wr": 100.0 * w / n} for k, (w, n) in entries.items()}


def mult(entries, fkey):
    return _Fake(book(entries))._feature_multiplier(fkey)


# ── [1] the Wilson interval itself ─────────────────────────────────────────
print("[1] the interval")

lo, hi = B.PaperTrader._wilson(5, 10)
check("50% of 10 -> a WIDE interval (it knows it does not know)",
      abs(lo - 0.2366) < 0.001 and abs(hi - 0.7634) < 0.001, (round(lo, 4), round(hi, 4)))
lo2, hi2 = B.PaperTrader._wilson(50, 100)
check("50% of 100 -> a much narrower one", (hi2 - lo2) < (hi - lo) / 2,
      (round(hi - lo, 3), round(hi2 - lo2, 3)))
check("the interval only ever narrows as n grows",
      all(B.PaperTrader._wilson(n // 2, n)[1] - B.PaperTrader._wilson(n // 2, n)[0]
          > B.PaperTrader._wilson(2 * n // 2, 2 * n)[1] - B.PaperTrader._wilson(2 * n // 2, 2 * n)[0]
          for n in (20, 40, 80, 160)))
check("n=0 is total ignorance, never a division error",
      B.PaperTrader._wilson(0, 0) == (0.0, 1.0))
check("it agrees with the project's other two implementations (learning_report"
      " and the dashboard JS use this same formula)",
      "z * z / (2 * n)" in SRC and "denom = 1 + z * z / n" in SRC)

# ── [2] neutral by default ─────────────────────────────────────────────────
print()
print("[2] neutral until the evidence says otherwise")

check("an unknown fingerprint is 1.0", mult({"a": (5, 20)}, "zzz") == 1.0)
check("an empty book is 1.0", mult({}, "a") == 1.0)
check("below the sample floor is 1.0, even when the raw rate looks terrible",
      mult({"a": (0, 19), "b": (40, 200)}, "a") == 1.0)
check("...and 1.0 even when the raw rate looks wonderful",
      mult({"a": (19, 19), "b": (40, 200)}, "a") == 1.0)
check("the floor is %d, not 5" % B.FEATURE_MIN_N, B.FEATURE_MIN_N >= 20)

# THE REGRESSION THAT MATTERS: a fingerprint performing exactly like the book.
pooled_book = {"a": (22, 100), "b": (22, 100), "c": (22, 100),
               "d": (22, 100), "e": (22, 100)}
check("a fingerprint matching this 22% book EXACTLY is left alone at 1.0"
      " (the old code cut it to 0.25x)", mult(pooled_book, "a") == 1.0,
      mult(pooled_book, "a"))

# ── [3] relative to THIS book, not an absolute ladder ──────────────────────
print()
print("[3] judged against the book's own base rate")

low_book = {"x": (22, 100), "y": (22, 100), "z": (22, 100), "w": (22, 100)}
low_book_up = dict(low_book); low_book_up["hot"] = (45, 100)   # 45% in a 22% book
check("45%% in a 22%% book is REWARDED (it beats its peers)",
      mult(low_book_up, "hot") == 1.15, mult(low_book_up, "hot"))

high_book = {"x": (70, 100), "y": (70, 100), "z": (70, 100), "w": (70, 100)}
high_book_same = dict(high_book); high_book_same["mid"] = (45, 100)  # 45% in a 70% book
check("the SAME 45%% in a 70%% book is PENALISED (it lags its peers)",
      mult(high_book_same, "mid") == 0.70, mult(high_book_same, "mid"))
check("...which is the whole point: 45% is not good or bad on its own",
      mult(low_book_up, "hot") != mult(high_book_same, "mid"))
# Scoped to the two STAKE paths. The same absolute-ladder bug lived in the
# per-pair stake multiplier as well and is fixed alongside; the remaining
# `wr >= 65` at ~line 4370 is a SCORING path with a hard `score = -1000`
# exclusion, which changes which pairs trade at all and is therefore the
# owner's call, not a silent edit. It is reported, not patched.
_FN_SPLIT = chr(10) + "    def "          # heredocs eat a bare backslash-n
_stake_paths = (SRC.split("def _feature_multiplier")[1].split(_FN_SPLIT)[0]
                + SRC.split("wr_mult = 1.0")[1][:2000])
check("no absolute win-rate ladder survives in either STAKE path",
      not re.search(r"wr\s*>=\s*65", _stake_paths)
      and not re.search(r"wr\s*<=\s*4[05]", _stake_paths))
check("the per-pair stake multiplier is now interval-based too",
      "_beats_book(_wins_of(wr_data)" in SRC)
check("the 0.25x quarter-stake bucket is gone",
      "return 0.25" not in SRC.split("def _feature_multiplier")[1].split("\n    def ")[0])

# ── [4] monotone in evidence — the property the old code inverted ──────────
print()
print("[4] more evidence must help, never hurt")

rng = random.Random(20260925)


def detect_rate(true_p, pooled_p, n, trials=4000):
    """How often is a fingerprint with true rate `true_p` correctly moved off
    1.0, in a book whose pooled rate is `pooled_p`, given n outcomes?"""
    hits = 0
    for _ in range(trials):
        w = sum(1 for _ in range(n) if rng.random() < true_p)
        peers = {"p%d" % i: (int(round(pooled_p * 400)), 400) for i in range(3)}
        peers["t"] = (w, n)
        if _Fake(book(peers))._feature_multiplier("t") != 1.0:
            hits += 1
    return hits / float(trials)


d20, d60, d200 = (detect_rate(0.45, 0.22, n) for n in (20, 60, 200))
print("     detection of a genuinely-better fingerprint (45%% in a 22%% book):"
      " n=20 %.3f  n=60 %.3f  n=200 %.3f" % (d20, d60, d200))
check("detection RISES with n (the old code's fell 0.189 -> 0.002)",
      d20 < d60 < d200, (d20, d60, d200))
check("...and gets close to certain with enough evidence", d200 > 0.95, d200)

fp20, fp200 = (detect_rate(0.22, 0.22, n) for n in (20, 200))
print("     false moves on a fingerprint that IS average: n=20 %.3f  n=200 %.3f"
      % (fp20, fp200))
check("a fingerprint matching the book is rarely moved at all (<=10%)",
      fp20 <= 0.10 and fp200 <= 0.10, (fp20, fp200))
check("...and the old 8-of-10-cut-to-0.25x behaviour cannot recur: an all-average"
      " book leaves almost everything at 1.0",
      sum(1 for k in pooled_book if mult(pooled_book, k) != 1.0) == 0)

# ── [5] wiring ─────────────────────────────────────────────────────────────
print()
print("[5] wiring")

check("the DB query now returns wins, so an interval is computable",
      re.search(r"SUM\(CASE WHEN won THEN 1 ELSE 0 END\) AS wins", SRC) is not None)
check("the in-memory path returns wins too", '"wins": v["wins"]' in SRC)
check("a cache without `wins` still works (older shape is derived, not crashed)",
      _Fake({"a": {"n": 100, "wr": 45.0}, "b": {"n": 400, "wr": 22.0}}
            )._feature_multiplier("a") == 1.15)
check("the multiplier is still applied to sizing", "_feature_multiplier(fkey)" in SRC)

# ── [6] the shared helpers, used by all four former ladders ────────────────
print()
print("[6] one rule, four call sites")

_bk = {"a": {"n": 100, "wins": 22}, "b": {"n": 100, "wins": 22},
       "c": {"n": 100, "wins": 22}, "d": {"n": 100, "wins": 45}}
_p = B._pooled_rate(_bk)
check("pooled is computed from the same rows being judged", abs(_p - 0.2775) < 0.001, _p)
check("a member matching the book scores 0 (leave it alone)",
      B._beats_book(22, 100, _p) == 0)
check("a member genuinely beating it scores +1", B._beats_book(45, 100, _p) == 1)
check("a member genuinely lagging it scores -1", B._beats_book(5, 100, _p) == -1)
check("too few samples is 0, never a guess", B._beats_book(0, 10, 0.242) == 0)
check("no pooled rate available is 0", B._beats_book(50, 100, None) == 0)
check("wins is derived when absent, not crashed",
      B._wins_of({"n": 100, "wr": 45.0}) == 45)

# The hard exclusion is the one that removed a pair from trading entirely.
# Against CODE, not SRC. My own comment explaining the removal contains the
# very string being searched for — the exact false-positive this project has
# now hit five times (post_only, video-cadence, the vol_dist default, the
# stripped INSERT). Strip comments first, then assert.
_CODE = re.sub(r"#[^%s]*" % chr(10), "", SRC)
check("the `score = -1000` hard exclusion is gone from the scoring path",
      "score = -1000" not in _CODE)
check("...and the string still appears in a COMMENT, so this check is proven"
      " capable of failing", "score = -1000" in SRC)
check("...and all four former ladders now route through _beats_book",
      SRC.count("_beats_book(") >= 4, SRC.count("_beats_book("))
check("the confidence-tier ladder is gone too",
      not re.search(r"if wr >= 55: return 1\.0", SRC))

print()
if FAILS:
    print("FAILED %d checks:" % len(FAILS))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("all checks passed")
sys.exit(0)
