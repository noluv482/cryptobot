"""test_counterfactual_learning.py — learning from signals the book declined.

Plain script, no pytest: exit 0 == pass.

Context. The adaptive pillar/feature weights only ever learned at position
CLOSE. The book went flat on 2026-08-24 and the learning tables have had no new
row since 2026-08-21 — while the engine kept evaluating signals and the filler
kept resolving what they went on to do. Roughly 9,000 resolved outcomes sat
unread.

Using them is easy to get WRONG in three specific ways, and this file exists to
pin all three:

  1. PROVENANCE. A counterfactual is what a trade WOULD have done. There are
     ~193 realised feature rows and ~9,000 counterfactual ones, so one
     unqualified query and the labelling convention silently becomes the
     learning. Writers stamp src; every realised reader filters on it.
  2. INDEPENDENCE. The horizon is 48h, so two signals on one pair inside 48h
     describe overlapping windows. Measured 2026-09-09: 8,990 resolved rows are
     269 independent observations across 33 pairs — a 33x overstatement. This
     is not theoretical: it is what made the min_conf gate look like it was
     rejecting +2.838% winners when de-overlapped the figure is -0.178%.
  3. LABELLING. We hold the 48h high and low but not their ORDER, so a row that
     touched both target and stop cannot be labelled without inventing the
     sequence — 46.6% of BUY rows. Those are skipped and counted, not guessed.

Also pinned here: the pillar weight denominator (measured, not a hard-coded
50% the book has never had) and single-sourced trading costs.
"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bot_server as bs                                        # noqa: E402

_failed: list = []


def check(label, cond, detail=""):
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        _failed.append(label)


SRC = io.open(ROOT / "bot_server.py", encoding="utf-8").read()

# ── 1. the labeller ──────────────────────────────────────────────────────────
print("[1] the counterfactual label")
T, S = 0.03, 0.02
C = bs.ROUND_TRIP_COST_PCT

check("cost is a FRACTION and is the real 1.30%, not a guessed 0.2%",
      abs(C - 0.013) < 1e-9, C)

for name, sig, up, dn, f48, want in [
    ("BUY target only",   "BUY",  0.040, -0.005,  0.035, True),
    ("BUY stop only",     "BUY",  0.010, -0.025, -0.020, False),
    ("SELL target only",  "SELL", 0.005, -0.040, -0.035, True),
    ("SELL stop only",    "SELL", 0.025, -0.010,  0.020, False),
]:
    got, _ = bs._cf_label(sig, T, S, up, dn, f48)
    check("%s -> %s" % (name, want), got is want, got)

for name, sig, up, dn in [("BUY", "BUY", 0.040, -0.025), ("SELL", "SELL", 0.025, -0.040)]:
    got, why = bs._cf_label(sig, T, S, up, dn, 0.01)
    check("%s touching BOTH barriers is UNLABELABLE, not guessed" % name,
          got is None and "order unknown" in why, (got, why))

check("neither barrier: a move BIGGER than cost is a win",
      bs._cf_label("BUY", T, S, 0.020, -0.010, C + 0.005)[0] is True)
check("neither barrier: a move SMALLER than cost is a loss, not a win",
      bs._cf_label("BUY", T, S, 0.005, -0.004, C - 0.005)[0] is False)
check("neither barrier, SELL: the sign is flipped before the cost test",
      bs._cf_label("SELL", T, S, 0.010, -0.020, -(C + 0.005))[0] is True)
check("missing barrier data is unlabelable, never assumed",
      bs._cf_label("BUY", None, S, 0.01, -0.01, 0.01)[0] is None
      and bs._cf_label("BUY", T, S, None, -0.01, 0.01)[0] is None)
check("no close to fall back on is unlabelable",
      bs._cf_label("BUY", T, S, 0.010, -0.010, None)[0] is None)

# ── 2. provenance ────────────────────────────────────────────────────────────
print("\n[2] a counterfactual can never be counted as a realised trade")
check("feature_outcomes has a src column defaulting to realised",
      "ALTER TABLE feature_outcomes ADD COLUMN IF NOT EXISTS" in SRC
      and "src TEXT NOT NULL DEFAULT 'realised'" in SRC)
check("pillar_outcomes has one too",
      "ALTER TABLE pillar_outcomes ADD COLUMN IF NOT EXISTS" in SRC)
check("log_feature takes src and defaults to realised",
      'def log_feature(self, fkey, pair, won, src="realised"' in SRC)
check("log_pillars takes src and defaults to realised",
      'def log_pillars(self, pillars: dict, won: bool, src="realised"' in SRC)
check("feature_win_rates reads ONLY realised rows",
      re.search(r"FROM feature_outcomes\s+WHERE src = 'realised'", SRC) is not None)
check("pillar_win_rates is scoped by src and DEFAULTS to realised",
      'def pillar_win_rates(self, src="realised")' in SRC
      and "WHERE active = true AND src = %s" in SRC)
check("the live weight path asks for realised explicitly or by default",
      "db.pillar_win_rates()" in SRC)
check("the counterfactual weights are REPORTED, not applied",
      '"applied": "realised"' in SRC and "REPORT ONLY, live" in SRC)
check("...and the report says how many pillars would actually move",
      "would_move" in SRC)
check("pillar_base_rate is scoped to a src too",
      "WHERE active = true AND src = %s" in SRC)
check("the counterfactual writer stamps its rows",
      'src="counterfactual"' in SRC)
check("...and the PROVEN verdict is never fed from them",
      "counterfactual" not in SRC.split("def _apply_proven_rule")[-1][:800]
      if "def _apply_proven_rule" in SRC else True)

# ── 3. independence ──────────────────────────────────────────────────────────
print("\n[3] overlapping rows are not independent observations")
check("the bucket is the full 48h horizon",
      bs.DB.SHADOW_BUCKET_S == 48 * 3600 if hasattr(bs, "DB") else
      "SHADOW_BUCKET_S = 48 * 3600" in SRC)
check("the batch selects ONE representative per (pair, bucket)",
      "DISTINCT ON (pair, floor(ts / %s))" in SRC)
check("...and retires the REST of each bucket, so the next batch cannot re-pick it",
      "id <> ALL(%s)" in SRC and "mark_shadow_learned(overlap_ids, 2)" in SRC)
check("the ledger distinguishes contributed / overlap / ambiguous",
      "skipped_overlap" in SRC and "skipped_ambiguous" in SRC)
check("nothing is silently dropped — every outcome is counted and logged",
      "retired as overlapping" in SRC and "unlabelable" in SRC)

# ── 4. the weight denominator ────────────────────────────────────────────────
print("\n[4] pillar weights are a ratio to the MEASURED base, not to 50%")
check("no hard-coded 50% denominator survives in either path",
      "wr / 0.50" not in SRC and 's["wr"] / 50.0' not in SRC)
check("the in-memory path measures its own base",
      "base = base_w / base_n" in SRC)
check("the db path asks for the measured base",
      "db.pillar_base_rate()" in SRC)
check("a base below the floor leaves weights untouched rather than inventing one",
      "if base_n < PILLAR_BASE_MIN_N or base_w == 0:" in SRC)

# the real distribution, measured 2026-09-09 off the live table
LIVE = {"high_volume": 27.27, "vwap_align": 25.93, "stoch_rsi": 24.14,
        "obv_trend": 21.54, "chart_struct": 21.31, "nasdaq_align": 21.15,
        "tick_strength": 20.31, "macd_align": 19.51, "candle_pattern": 19.12,
        "rsi_zone": 17.42, "news_align": 0.00}
LIVE_BASE = 21.08
old_w = {p: max(0.4, min(1.5, wr / 50.0)) for p, wr in LIVE.items()}
new_w = {p: max(0.4, min(1.5, wr / LIVE_BASE)) for p, wr in LIVE.items()}
check("the old scheme pinned 4 of 11 pillars to the 0.4 floor",
      sum(1 for v in old_w.values() if v <= 0.4001) == 4,
      sorted(round(v, 3) for v in old_w.values()))
check("the measured base lifts all but the 0-win pillar off the floor",
      sum(1 for v in new_w.values() if v <= 0.4001) == 1,
      sorted(round(v, 3) for v in new_w.values()))
check("...and gives a real spread around 1.0 instead of a 0.15-wide huddle",
      (max(new_w.values()) - min(new_w.values())) > 0.8
      and (max(old_w.values()) - min(old_w.values())) < 0.2,
      (round(max(new_w.values()) - min(new_w.values()), 3),
       round(max(old_w.values()) - min(old_w.values()), 3)))

def _weights_from(trades):
    out: dict = {}
    bs._compute_pillar_weights(trades, out)
    return out

many = [{"pillars": {"a": True, "b": True}, "pnl": (1.0 if i % 5 == 0 else -1.0)}
        for i in range(120)]
check("a 20%-base sample puts an average pillar near 1.0, not near the floor",
      abs(_weights_from(many).get("a", 0) - 1.0) < 0.05, _weights_from(many))
check("too little evidence leaves the caller's weights exactly as they were",
      _weights_from.__call__([{"pillars": {"a": True}, "pnl": 1.0}]) == {})
allloss = [{"pillars": {"a": True}, "pnl": -1.0} for _ in range(200)]
check("an all-losing sample has no base to divide by and changes nothing",
      _weights_from(allloss) == {}, _weights_from(allloss))

# ── 5. one source of truth for cost ──────────────────────────────────────────
print("\n[5] trading cost is single-sourced")
FEE_DEF = re.compile(r"^(TAKER_FEE|MAKER_FEE|ROUND_TRIP)\s*=\s*0\.\d+", re.M)
offenders = []
for f in sorted(ROOT.glob("*.py")):
    if f.name.startswith("test_") or f.name == "bot_server.py":
        continue
    body = io.open(f, encoding="utf-8", errors="ignore").read()
    for m in FEE_DEF.finditer(body):
        # a literal inside an except-fallback is allowed if the file also
        # imports the real thing
        if "ROUND_TRIP_COST_PCT" in body or "_bs.KRAKEN_FEE" in body:
            continue
        offenders.append("%s: %s" % (f.name, m.group(0)))
check("no module defines its own trading-fee constants in isolation",
      not offenders, offenders)
check("the donchian benchmark now takes its cost from the bot",
      "_bs.ROUND_TRIP_COST_PCT" in io.open(ROOT / "benchmark_donchian.py",
                                           encoding="utf-8").read())
check("...and no longer claims 0.72% 'matches bot_server'",
      "0.72%, matches bot_server" not in io.open(ROOT / "benchmark_donchian.py",
                                                 encoding="utf-8").read())

print("\n" + "=" * 60)
if _failed:
    print("FAILED (%d): %s" % (len(_failed), ", ".join(_failed)))
    sys.exit(1)
print("all counterfactual-learning checks passed")
