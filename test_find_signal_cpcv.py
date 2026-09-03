#!/usr/bin/env python3
"""CPCV / PBO / venue-cost contract for find_signal.py.

The battery's single IS/OOS split can be gamed by one lucky regime boundary.
CPCV (combinatorial purged cross-validation) re-judges every candidate on
many train/test partitions with the leakage seams cut out, and PBO turns the
path distribution into one number: how often the in-sample winner is a loser
out-of-sample. Three things must hold:

  1. NO LEAKAGE — no training bar's forward label may touch a test block,
     and an embargo >= the horizon must clear the serially-correlated wake
     after each block. Verified as a PROPERTY over the mask, not by trusting
     the arithmetic that built it.
  2. PBO DISCRIMINATES — a battery of pure noise candidates on a random walk
     must score high PBO; a battery with one genuinely predictive candidate
     on a mean-reverting series must score low. Both synthetic, both seeded.
  3. HONEST COSTS — the perp venue charges fees plus funding drag scaled by
     the hold, --cost keeps its old meaning, and the funding fallback says
     it is an ESTIMATE instead of dressing up as data.

All synthetic, no DB, no network (the funding table path runs against a fake
psycopg2 injected into sys.modules).
"""
import math
import os
import random
import sys
import types

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs

bs.log = lambda *a, **k: None

import find_signal as fs

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── 1. combinatorial splits ──────────────────────────────────────────────────
combos = fs.cpcv_combinations(6, 2)
check("C(6,2) = 15 splits", len(combos) == 15, str(len(combos)))
check("splits are unique", len(set(combos)) == 15)
appearances = [sum(1 for c in combos if g in c) for g in range(6)]
check("every group tests equally often (5x each)",
      appearances == [5] * 6, str(appearances))

b = fs.group_bounds(1000, 5)
check("group bounds tile [0,n) contiguously",
      b[0][0] == 0 and b[-1][1] == 1000
      and all(b[i][1] == b[i + 1][0] for i in range(4)), str(b))


# ── 2. purge + embargo: no train label touches a test block ─────────────────
n, hz, emb = 1000, 24, 50
bounds = fs.group_bounds(n, 5)          # blocks of 200
tg = (1, 3)                             # test blocks [200,400) and [600,800)
mask = fs.purged_train_mask(n, bounds, tg, hz, emb)
tmask = fs.test_mask(n, bounds, tg)

check("test mask covers exactly the chosen blocks",
      sum(tmask) == 400 and tmask[200] and tmask[399]
      and tmask[600] and tmask[799] and not tmask[199] and not tmask[400])

# the property that matters: for every usable train bar i and every test bar
# j, the label intervals [i, i+hz] and [j, j+hz] must be disjoint
test_blocks = [bounds[g] for g in tg]
leaks = []
for i in range(n):
    if not mask[i]:
        continue
    for (a, bb) in test_blocks:
        # overlap with any j in [a, bb): intervals meet iff i <= j+hz and
        # j <= i+hz for some j -> iff i <= bb-1+hz and a <= i+hz
        if i <= bb - 1 + hz and a <= i + hz:
            leaks.append(i)
            break
check("PURGE: no train sample's label overlaps any test label",
      not leaks, f"first leaks {leaks[:5]}")

emb_viol = [i for (a, bb) in test_blocks
            for i in range(bb, min(n, bb + emb)) if mask[i]]
check("EMBARGO: no train sample within embargo bars after a test block",
      not emb_viol, str(emb_viol[:5]))

expect_false = set(range(200 - hz, 400 + emb)) | set(range(600 - hz, 800 + emb))
got_false = {i for i in range(n) if not mask[i]}
check("mask excludes exactly [a-hz, b+emb) around each block, keeps the rest",
      got_false == expect_false,
      f"extra={sorted(got_false - expect_false)[:5]} "
      f"missing={sorted(expect_false - got_false)[:5]}")

m0 = fs.purged_train_mask(n, bounds, (1,), hz, 0)   # embargo=0 given
check("embargo is clamped to >= horizon inside the mask",
      all(not m0[i] for i in range(400, 400 + hz)) and m0[400 + hz])

e2 = fs.cpcv_pbo([], {}, horizon=24, embargo=6)      # embargo < horizon
check("cpcv_pbo with embargo < horizon returns (empty, None, 0) not a crash",
      e2 == ({}, None, 0), str(e2))


# ── 3. non-overlap inside masked_edge ────────────────────────────────────────
def mk_series(rets, start=100.0):
    c = [start]
    for r in rets:
        c.append(c[-1] * (1 + r))
    h = [x * 1.001 for x in c]
    l = [x * 0.999 for x in c]
    v = [1.0] * len(c)
    return (c, h, l, v)


rng = random.Random(11)
flat = mk_series([rng.gauss(0, 0.004) for _ in range(1200)])
always_buy = lambda c, h, l, v, i: "BUY"
sigs = fs.precompute_signals(always_buy, [flat])
hz3 = 6
full = [[True] * len(flat[0])]
edge, ntr = fs.masked_edge([flat], sigs, hz3, full, min_n=5)
n_bars = len(flat[0])
max_possible = math.ceil((n_bars - hz3 - 1 - fs.WARMUP) / hz3)
check("masked_edge is NON-overlapping: an always-on signal takes ~n/hz "
      "trades, not n",
      ntr <= max_possible + 1 and ntr >= max_possible - 2,
      f"ntr={ntr} max={max_possible}")
check("always-BUY vs its own baseline is ~zero edge (direction-matched)",
      edge is not None and abs(edge) < 0.004, str(edge))


# ── 4. PBO: overfit battery scores high, real edge scores low ────────────────
def noise_cand(k):
    def fn(c, h, l, v, i):
        x = (i * 2654435761 + k * 40503) & 0xffffffff
        x = (x ^ (x >> 13)) % 7
        return "BUY" if x == 0 else ("SELL" if x == 1 else None)
    return fn


def rev_cand(c, h, l, v, i):
    if i < 1:
        return None
    r = (c[i] - c[i - 1]) / c[i - 1]
    if r > 0.002:
        return "SELL"
    if r < -0.002:
        return "BUY"
    return None


# overfit case: iid random walk, 8 noise candidates. Any IS winner is luck,
# so its OOS rank is uniform -> PBO should sit near 0.5.
rw = mk_series([random.Random(3).gauss(0, 0.005) for _ in range(2400)])
cands_noise = {f"noise_{k}": fs.precompute_signals(noise_cand(k), [rw])
               for k in range(8)}
stats_o, pbo_over, used_o = fs.cpcv_pbo([rw], cands_noise, horizon=2,
                                        n_groups=6, n_test=2, embargo=2,
                                        min_n=5)
check("overfit synthetic: all 15 splits scorable", used_o == 15, str(used_o))
check(f"overfit synthetic: PBO > house line {fs.PBO_DEAD_LINE}",
      pbo_over is not None and pbo_over > fs.PBO_DEAD_LINE, str(pbo_over))

# robust case: strong mean reversion + the same noise candidates. The
# reversal rule wins in-sample AND out-of-sample everywhere -> PBO ~ 0.
r_rng = random.Random(7)
rets, prev = [], 0.003
for _ in range(2400):
    r = -0.6 * prev + r_rng.gauss(0, 0.003)
    rets.append(r)
    prev = r
mr = mk_series(rets)
cands_mix = {f"noise_{k}": fs.precompute_signals(noise_cand(k), [mr])
             for k in range(7)}
cands_mix["reversal"] = fs.precompute_signals(rev_cand, [mr])
stats_r, pbo_rob, used_r = fs.cpcv_pbo([mr], cands_mix, horizon=1,
                                       n_groups=6, n_test=2, embargo=1,
                                       min_n=5)
check("robust synthetic: all 15 splits scorable", used_r == 15, str(used_r))
check(f"robust synthetic: PBO <= house line {fs.PBO_DEAD_LINE}",
      pbo_rob is not None and pbo_rob <= fs.PBO_DEAD_LINE, str(pbo_rob))
check("robust synthetic: the real candidate's CPCV mean is positive",
      "reversal" in stats_r and stats_r["reversal"]["mean"] > 0,
      str(stats_r.get("reversal")))
check("CPCV stats carry mean/p5/p95/n_splits and p5 <= mean <= p95",
      all(stats_r["reversal"][k] is not None
          for k in ("mean", "p5", "p95", "n_splits"))
      and stats_r["reversal"]["p5"] <= stats_r["reversal"]["mean"]
          <= stats_r["reversal"]["p95"])

check("percentile helper: interpolates and bounds",
      fs._pctl([1, 2, 3, 4], 0.0) == 1 and fs._pctl([1, 2, 3, 4], 1.0) == 4
      and abs(fs._pctl([1, 2, 3, 4], 0.5) - 2.5) < 1e-12
      and fs._pctl([], 0.5) is None)


# ── 5. venue cost math ───────────────────────────────────────────────────────
check("spot default = the live spot round trip (unchanged behavior)",
      fs.venue_cost("spot", 24, 60) == bs.ROUND_TRIP_COST_PCT)
check("spot --cost override is exact (backward compatible)",
      fs.venue_cost("spot", 24, 60, base_rt=0.0005) == 0.0005)
check("spot cost ignores horizon (no funding on spot)",
      fs.venue_cost("spot", 6, 60, base_rt=0.0005)
      == fs.venue_cost("spot", 96, 60, base_rt=0.0005))

f = 2e-5
got = fs.venue_cost("perp", 48, 60, funding_hourly=f)
check("perp taker: 0.10% RT + funding x hold hours",
      abs(got - (0.0010 + f * 48)) < 1e-12, str(got))
got = fs.venue_cost("perp", 48, 60, maker=True, funding_hourly=f)
check("perp maker: 0.04% RT + funding x hold hours",
      abs(got - (0.0004 + f * 48)) < 1e-12, str(got))
got = fs.venue_cost("perp", 12, 240, funding_hourly=f)
check("perp hold scales with the INTERVAL (12 x 4h bars = 48h of funding)",
      abs(got - (0.0010 + f * 48)) < 1e-12, str(got))
got = fs.venue_cost("perp", 24, 60, base_rt=0.0006, funding_hourly=f)
check("--cost with --venue perp overrides the fee leg, drag still added",
      abs(got - (0.0006 + f * 24)) < 1e-12, str(got))
check("longer holds cost strictly more on perp (the multi-day floor)",
      fs.venue_cost("perp", 96, 60, funding_hourly=f)
      > fs.venue_cost("perp", 6, 60, funding_hourly=f))


# ── 6. funding lookup: DB when reachable, labeled ESTIMATE when not ─────────
_env_saved = os.environ.pop("DATABASE_URL", None)
_mod_saved = sys.modules.get("psycopg2")
try:
    rate, src = fs.perp_funding_hourly()
    check("no DATABASE_URL -> flat fallback rate",
          rate == fs.FUNDING_HOURLY_FALLBACK, str(rate))
    check("fallback is labeled an ESTIMATE (data honesty)",
          "ESTIMATE" in src, src)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, q, params):
            assert "funding_rates" in q and "ABS(rate)" in q
            assert params == ("kraken",)

        def fetchone(self):
            return (3e-5, 4321)

    class _Conn:
        closed = False

        def cursor(self):
            return _Cur()

        def close(self):
            _Conn.closed = True

    fake = types.ModuleType("psycopg2")
    fake.connect = lambda url, connect_timeout=5: _Conn()
    sys.modules["psycopg2"] = fake
    os.environ["DATABASE_URL"] = "postgresql://mocked/nowhere"
    rate, src = fs.perp_funding_hourly()
    check("funding_rates table reachable -> its mean |rate| is used",
          rate == 3e-5, str(rate))
    check("table source is labeled as the table, with row count",
          "funding_rates" in src and "4321" in src, src)
    check("DB connection is closed after the read", _Conn.closed)

    class _BoomModule(types.ModuleType):
        @staticmethod
        def connect(url, connect_timeout=5):
            raise RuntimeError("db down")

    sys.modules["psycopg2"] = _BoomModule("psycopg2")
    rate, src = fs.perp_funding_hourly()
    check("DB error degrades silently to the labeled fallback",
          rate == fs.FUNDING_HOURLY_FALLBACK and "ESTIMATE" in src)
finally:
    if _env_saved is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _env_saved
    if _mod_saved is None:
        sys.modules.pop("psycopg2", None)
    else:
        sys.modules["psycopg2"] = _mod_saved


# ── 7. CLI flags ─────────────────────────────────────────────────────────────
ap = fs.build_parser()
d = ap.parse_args([])
check("defaults: spot venue, CPCV on, 6/2 groups, embargo=None(-> horizon)",
      d.venue == "spot" and not d.no_cpcv and d.cpcv_groups == 6
      and d.cpcv_test == 2 and d.embargo is None and not d.maker
      and d.cost is None)

a = ap.parse_args(["--venue", "perp", "--maker", "--embargo", "48",
                   "--cpcv-groups", "8", "--cpcv-test", "3", "--no-cpcv"])
check("all new flags parse",
      a.venue == "perp" and a.maker and a.embargo == 48
      and a.cpcv_groups == 8 and a.cpcv_test == 3 and a.no_cpcv)

old = ap.parse_args(["--cost", "0.0005", "--history", "--horizons", "6,24"])
check("pre-venue invocations still parse identically (--cost, --history)",
      old.cost == 0.0005 and old.history and old.venue == "spot"
      and old.horizons == "6,24")

bad_ok = True
try:
    ap.parse_args(["--venue", "margin"])
    bad_ok = False
except SystemExit:
    pass
check("--venue rejects anything but spot/perp", bad_ok)


print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all CPCV/PBO/venue-cost contract checks pass")
