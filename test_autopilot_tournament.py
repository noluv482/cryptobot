#!/usr/bin/env python3
"""Autopilot tournament contract — persistent, fed, honest.

The tournament was structurally unable to conclude: challenger books were
memory-only (wiped by every deploy-per-push) and starved (they only trade
when the live pipeline fires). This pins the three fixes:

  1. PERSISTENT — live challengers get their own state_path; retirement
     deletes the file.
  2. FED — counterfactual scoring reads the recorded signal stream. Its math
     is tested on a synthetic stream through the REAL scorer: filters apply,
     overlapping windows are dropped, costs are charged, and rows at or
     before the config's epoch never count (the pre-registration rule: a
     config suggested by an audit over past rows is graded only on rows it
     has never seen).
  3. HONEST MERGE — real fills outrank simulated: cf drives a record only
     while the live book is short of MIN_OOS_TRADES; the promotion bars
     (n>=20, t>=T_MARGIN, cost gate) are identical for both.

Plus: the strict-gates and exit-family entrants exist, cf_only entrants get
no live trader (nothing to leak), and the standings ship in status().
"""
import io
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs
import autopilot as ap

bs.log = lambda *a, **k: None
ap.log = lambda *a, **k: None
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


SRC_AP = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "autopilot.py"), encoding="utf-8").read()

# 1. persistence
check("live challengers carry a state_path",
      'state_path=os.path.join(bs._DATA_DIR, f"ap_{cid}.json")' in SRC_AP)
check("retirement deletes the challenger's book file",
      'os.remove(_f)' in SRC_AP)

# configs present
ids = {c["id"] for c in ap.CHALLENGER_CONFIGS}
check("strict_gates entrant exists", "strict_gates" in ids)
check("exit-family entrants exist", {"exit_6h", "exit_24h"} <= ids)
sg = next(c for c in ap.CHALLENGER_CONFIGS if c["id"] == "strict_gates")
check("strict_gates uses the ORIGINAL thresholds",
      sg["cf"] == {"conf": 0.50, "adx": 18.0, "er": 0.15, "horizon": "fwd48"})
check("strict_gates is cf_only (live path cannot filter adx/er)",
      sg.get("cf_only") is True)

# trend_rr: the owner's loosening hypothesis, admitted as an entrant instead
# of a gate change. Its born_ts is the load-bearing detail — the retrospective
# audit that suggested it ran on post-epoch rows, so falling back to
# AP_CF_EPOCH would grade the hypothesis on the data that generated it.
check("trend_rr entrant exists", "trend_rr" in ids)
tr = next(c for c in ap.CHALLENGER_CONFIGS if c["id"] == "trend_rr")
check("trend_rr is the pure loosening: ADX floor only, no conf/er/rr",
      tr["cf"] == {"adx": 30.0, "horizon": "fwd48"} and tr.get("cf_only") is True)
check("trend_rr is pre-registered AFTER the audit rows (hardcoded born_ts)",
      float(tr.get("born_ts") or 0) > ap.AP_CF_EPOCH)
check("cf scorer honors a config's own birth over the epoch",
      "max(AP_CF_EPOCH, float(cfg.get(\"born_ts\") or 0))" in SRC_AP)

# 2. the cf scorer, driven with a synthetic recorded stream through the REAL code
class FakeCursor:
    def __init__(self, rows): self.rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=None):
        self.born = params[0]
    def fetchall(self):
        return [r for r in self.rows if r[0] > self.born]

class FakeConn:
    def __init__(self, rows): self.rows = rows
    def cursor(self): return FakeCursor(self.rows)

EPOCH = ap.AP_CF_EPOCH
H = 3600
# (ts, pair, sig, conf, adx, er, rr_net, fwd48)
ROWS = []
# 40 non-overlapping qualifying BUYs on pair A with a clearly positive mean
# and REAL variance — a constant series has sd=0, no computable t, and the
# scorer must refuse it (checked below).
for i in range(40):
    fwd = 0.02 + (0.012 if i % 3 == 0 else -0.004) + (0.002 * (i % 5))
    ROWS.append((EPOCH + 1 + i * 49 * H, "AAA", "BUY", 0.60, 20.0, 0.20, 2.0, fwd))
# overlapping cluster (same pair, minutes apart) — only the first may count
for i in range(5):
    ROWS.append((EPOCH + 2 + i * 60, "BBB", "BUY", 0.60, 20.0, 0.20, 2.0, 0.02))
# fails the conf filter
ROWS.append((EPOCH + 3, "CCC", "BUY", 0.40, 20.0, 0.20, 2.0, 0.5))
# pre-epoch row that must NEVER count
ROWS.append((EPOCH - 100, "DDD", "BUY", 0.99, 99.0, 0.99, 9.0, 5.0))

saved = bs.db.conn
try:
    # db.connected is a property derived from conn — swapping conn is enough
    bs.db.conn = FakeConn(ROWS)
    a = ap.Autopilot.__new__(ap.Autopilot)   # no full init: scorer is self-contained
    cfg = {"id": "sgtest", "cf": {"conf": 0.50, "adx": 18.0, "er": 0.15,
                                  "horizon": "fwd48"}}
    r = ap.Autopilot.score_counterfactual(a, cfg)
    check("cf scorer returns a result", r is not None)
    if r:
        # 40 on AAA (spaced 49h) + 1 from the BBB cluster = 41
        check("overlapping windows collapse to one", r["n_oos"] == 41, r["n_oos"])
        import statistics as _st
        kept = [x[7] for x in ROWS if x[1] == "AAA"] + [0.02]  # AAA + first BBB
        expect = _st.fmean(kept) - bs.ROUND_TRIP_COST_PCT
        check("costs charged on every counterfactual fill",
              abs(r["oos_edge"] - expect) < 1e-9, (r["oos_edge"], expect))
        check("pre-epoch rows never count (pre-registration rule)",
              r["oos_edge"] < 0.03)   # the 5.0 pre-epoch row would explode the mean
        check("a real +2% stream clears the bars", r["clears_cost"] is True)
    # a constant-return stream must be refused for want of a t, not crowned
    sat = [(EPOCH + 1 + i * 49 * H, "EEE", "BUY", 0.9, 30.0, 0.5, 3.0, 0.05)
           for i in range(25)]
    bs.db.conn = FakeConn(sat)
    r3 = ap.Autopilot.score_counterfactual(a, {"id": "c", "cf": {"conf": None,
                                                "horizon": "fwd48"}})
    check("constant returns (sd=0) cannot clear — no t, no crown",
          r3 is not None and not r3["clears_cost"])
    bs.db.conn = FakeConn(ROWS)
    cfg_hi = {"id": "x", "cf": {"conf": 0.95, "horizon": "fwd48"}}
    r2 = ap.Autopilot.score_counterfactual(a, cfg_hi)
    check("a filter nothing passes refuses to clear",
          r2 is not None and r2["n_oos"] == 0 and not r2["clears_cost"])
finally:
    bs.db.conn = saved

# 3. honest merge + standings, from source
check("cf drives a record only below MIN_OOS_TRADES (real fills outrank)",
      'if res["n_oos"] < MIN_OOS_TRADES:' in SRC_AP)
check("standings ship in status()", '"standings":' in SRC_AP)
check("cf_only entrants get no live trader",
      'if cfg.get("cf_only"):' in SRC_AP)

# status() must survive cf_only entrants — the KeyError('strict_gates') that
# took down /autopilot live got here because the test asserted the pieces but
# never CALLED the real status() with a mixed pool. Build a real instance
# (DB-less: lab file read and _load tolerate absence) and call it.
try:
    inst = ap.Autopilot()
    st = inst.status()
    ok = isinstance(st, dict) and "standings" in st and len(st["challengers"]) >= 9
    cf_rows = [c for c in st["challengers"] if c["id"] == "strict_gates"]
    check("real status() succeeds with cf_only entrants in the pool", ok,
          list(st.keys())[:6] if isinstance(st, dict) else st)
    check("cf_only entrant appears in challengers with null balance",
          bool(cf_rows) and cf_rows[0]["balance"] is None)
except Exception as e:
    check("real status() succeeds with cf_only entrants in the pool", False, e)

# ══════════════════════════════════════════════════════════════════════════════
# WORKSTREAM AP (2026-09-03): DSR/PSR/MinTRL survival stats, kill rule,
# fwd168 + kind dispatch, weekly/carry/switch entrants, dead-weight conversion.
# ══════════════════════════════════════════════════════════════════════════════
import json
import math
import statistics as st_

BASE_DAY = (int(EPOCH) // 86400 + 1) * 86400   # first UTC day boundary after the epoch

# 4a. DSR / PSR / MinTRL math on synthetic returns ----------------------------
check("norm ppf/cdf round-trip", abs(ap._norm_cdf(ap._norm_ppf(0.975)) - 0.975) < 1e-6)

# the N-trials hurdle RISES with trials_count (more hypotheses tried => a
# luckier best-of-N => a higher bar for everyone, forever)
sr0_16 = ap.expected_max_sr(0.04, 16)
sr0_100 = ap.expected_max_sr(0.04, 100)
sr0_500 = ap.expected_max_sr(0.04, 500)
check("SR0 hurdle rises with trials_count",
      sr0_16 is not None and sr0_16 < sr0_100 < sr0_500, (sr0_16, sr0_100, sr0_500))
check("SR0 hurdle refuses to exist without cross-entrant variance",
      ap.expected_max_sr(None, 50) is None and ap.expected_max_sr(0.04, 1) is None)

NOW = 1790000000.0
GOOD = [0.001 + 0.004 * math.sin(i * 1.7) for i in range(60)]  # real variance, sr ~0.35
d0 = ap.dsr_stats(GOOD, sr0=None, born_ts=NOW - 60 * 7 * 86400, now=NOW)
sr_hand = st_.fmean(GOOD) / st_.pstdev(GOOD)
check("dsr_stats sr matches hand computation", abs(d0["sr"] - sr_hand) < 1e-12)
check("psr computable vs 0 even without a hurdle",
      d0["psr"] is not None and 0.99 < d0["psr"] <= 1.0, d0["psr"])
check("no hurdle -> honest verdict, no dsr", d0["dsr"] is None and "hurdle" in d0["verdict"])

d1 = ap.dsr_stats(GOOD, sr0=0.05, born_ts=NOW - 60 * 7 * 86400, now=NOW)
d2 = ap.dsr_stats(GOOD, sr0=-0.50, born_ts=NOW - 60 * 7 * 86400, now=NOW)
check("dsr falls as the hurdle rises", d2["dsr"] > d1["dsr"], (d1["dsr"], d2["dsr"]))
check("MinTRL shrinks as |sr-sr0| grows (resolvable both sides)",
      d2["min_trl"] < d1["min_trl"], (d1["min_trl"], d2["min_trl"]))
check("dsr_stats refuses n<2", ap.dsr_stats([0.01], 0.0)["verdict"] == "insufficient data")
check("dsr_stats refuses sd=0", "sd=0" in ap.dsr_stats([0.01] * 30, 0.0)["verdict"])

# a young weekly entrant: n far below MinTRL -> months-to-verdict, said honestly
young = [0.002 * math.sin(i * 2.1) + 0.001 for i in range(10)]      # 10 weekly decisions
dy = ap.dsr_stats(young, sr0=st_.fmean(young) / st_.pstdev(young) - 0.20,
                  born_ts=NOW - 10 * 7 * 86400, now=NOW)
check("weekly entrant surfaces 'verdict in ~N months at current signal rate'",
      "months at current signal rate" in str(dy["verdict"]), dy["verdict"])

# 4b. survival stats through the instance path + hurdle uses trials_count -----
stub = ap.Autopilot.__new__(ap.Autopilot)
stub.killed = {}
stub.configs = {"a": {}, "b": {}}
stub.trials_count = 16
BAD = [-0.001 + 0.005 * math.sin(i * 1.3) for i in range(60)]
out16 = {"a": {"clears_cost": False}, "b": {"clears_cost": False}}
stub._attach_survival_stats(out16, {"a": list(GOOD), "b": list(BAD)})
stub.trials_count = 500
out500 = {"a": {"clears_cost": False}, "b": {"clears_cost": False}}
stub._attach_survival_stats(out500, {"a": list(GOOD), "b": list(BAD)})
check("instance stats expose sr/dsr/psr/min_trl/trades_n/verdict",
      all(k in out16["a"] for k in ("sr", "dsr", "psr", "min_trl", "trades_n", "verdict")))
check("hurdle in instance path rises with trials_count",
      out500["a"]["sr0"] > out16["a"]["sr0"], (out16["a"]["sr0"], out500["a"]["sr0"]))
check("higher hurdle can only lower an entrant's dsr",
      out500["a"]["dsr"] <= out16["a"]["dsr"])

# 4c. kill rule fires exactly once, freezes config, heals champion ------------
k = ap.Autopilot.__new__(ap.Autopilot)
k.killed = {}
k.configs = {"bad": {"id": "bad", "cf_only": True,
                     "allowed_strategies": {"MULTI_SIGNAL"}},
             "ok": {"id": "ok"}}
k.champion_id, k.allocation = "bad", "bad"
k.last_switch = {}
k.scores = {"bad": {"min_trl": 10.0, "trades_n": 50, "dsr": 0.01, "psr": 0.4,
                    "sr0": 0.5, "sr": -0.1, "clears_cost": True},
            "ok": {"min_trl": 10.0, "trades_n": 50, "dsr": 0.90, "psr": 0.95,
                   "sr0": 0.5, "sr": 0.9, "clears_cost": True}}
k._apply_kill_rule()
check("kill rule fires on past-MinTRL + DSR<0.20", "bad" in k.killed)
check("healthy entrant past MinTRL with DSR>=0.20 is NOT killed", "ok" not in k.killed)
check("killed entrant loses eligibility", k.scores["bad"]["clears_cost"] is False)
check("killed entrant verdict is KILLED", k.scores["bad"]["verdict"] == "KILLED")
check("kill freezes config with reason",
      k.killed["bad"]["reason"] and k.killed["bad"]["config"]["id"] == "bad"
      and k.killed["bad"]["config"]["allowed_strategies"] == ["MULTI_SIGNAL"])
check("killed champion falls to FLAT", k.champion_id is None and k.allocation == "FLAT")
ts_first = k.killed["bad"]["ts"]
k.scores["bad"]["clears_cost"] = True          # try to sneak it back in
k._apply_kill_rule()
check("kill rule fires exactly once (graveyard is idempotent)",
      len(k.killed) == 1 and k.killed["bad"]["ts"] == ts_first)
# never auto-revived: a rescore of a killed id keeps the KILLED verdict and
# forces clears_cost back to False, no matter how good its new numbers look
k.trials_count = 16
o2 = {"bad": {"clears_cost": True}}
k._attach_survival_stats(o2, {"bad": list(GOOD)})
check("KILLED verdict + clears_cost=False survive a rescore",
      o2["bad"]["verdict"] == "KILLED" and o2["bad"]["clears_cost"] is False)

# 4d. trials ledger: monotone, seeded honestly --------------------------------
check("TRIALS_SEED documents 13 entrants + 3 gap_fade deaths", ap.TRIALS_SEED == 16)
tstub = ap.Autopilot.__new__(ap.Autopilot)
tstub.configs = {"base": {}, "tsmom_btc_20w": {}, "lab_x": {}}
tstub.trials_count, tstub.trials_ids = ap.TRIALS_SEED, []
tstub._register_trials()
check("new ids increment trials_count once each",
      tstub.trials_count == ap.TRIALS_SEED + 2
      and set(tstub.trials_ids) == {"tsmom_btc_20w", "lab_x"})
tstub._register_trials()
check("re-registering never re-counts", tstub.trials_count == ap.TRIALS_SEED + 2)
check("pre-seed built-ins never re-count", "base" not in tstub.trials_ids)

# 4e. horizons + kind dispatch ------------------------------------------------
R168 = [(EPOCH + 1,            "AAA", "BUY", 0.6, 20.0, 0.2, 2.0, 0.03),
        (EPOCH + 1 + 100 * H,  "AAA", "BUY", 0.6, 20.0, 0.2, 2.0, 0.03),  # <168h: overlap
        (EPOCH + 1 + 400 * H,  "AAA", "BUY", 0.6, 20.0, 0.2, 2.0, 0.03)]
saved = bs.db.conn
try:
    bs.db.conn = FakeConn(R168)
    a2 = ap.Autopilot.__new__(ap.Autopilot)
    r168 = a2._score_cf_price({"id": "h", "cf": {"conf": None, "horizon": "fwd168"}})
    check("fwd168 accepted in the horizon whitelist", r168 is not None)
    check("fwd168 non-overlap window is 168h", r168 and r168["n_oos"] == 2, r168 and r168["n_oos"])
    # weekly flag: several qualifying rows in one ISO week -> ONE scored decision.
    # Anchored to BASE_DAY (Tue 2026-08-25 UTC): +0/+2h/+3d are the same ISO
    # week, +8d lands in the next one. Distinct pairs dodge the per-pair
    # overlap rule so ONLY the weekly guard can be doing the collapsing.
    WK = [(BASE_DAY + 3600,               "AAA", "BUY", 0.6, 20.0, 0.2, 2.0, 0.02),
          (BASE_DAY + 3600 + 7200,        "BBB", "BUY", 0.6, 20.0, 0.2, 2.0, 0.02),
          (BASE_DAY + 3600 + 3 * 86400,   "CCC", "BUY", 0.6, 20.0, 0.2, 2.0, 0.02),
          (BASE_DAY + 3600 + 8 * 86400,   "DDD", "BUY", 0.6, 20.0, 0.2, 2.0, 0.02)]
    bs.db.conn = FakeConn(WK)
    rw = a2._score_cf_price({"id": "w", "cf": {"conf": None, "horizon": "fwd48",
                                               "weekly": True}})
    rnw = a2._score_cf_price({"id": "nw", "cf": {"conf": None, "horizon": "fwd48"}})
    check("weekly=True: max ONE scored decision per ISO week",
          rw["n_oos"] == 2, rw["n_oos"])
    check("without weekly flag the same rows all score", rnw["n_oos"] == 4, rnw["n_oos"])
finally:
    bs.db.conn = saved

# kind dispatch routes by via (each path stamps its own)
class TableCursor:
    def __init__(self, tables): self.tables = tables; self._rows = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=None):
        if "FROM candles" in sql:
            self._rows = self.tables.get(("candles", params[0]), [])
        elif "FROM funding_rates" in sql:
            self._rows = self.tables.get(("funding", params[0]), [])
        elif "FROM shadow_signals" in sql:
            self._rows = [r for r in self.tables.get("shadow", []) if r[0] > params[0]]
    def fetchall(self): return self._rows

class TableConn:
    def __init__(self, tables): self.tables = tables
    def cursor(self): return TableCursor(self.tables)

def mk_candles(n_pre=200, n_post=160, drift=0.01, start=100.0):
    rows = []
    for d in range(-n_pre, n_post):
        # deterministic jitter so weekly returns have real variance (sd>0)
        c = start * ((1 + drift) ** d) * (1 + 0.004 * math.sin(d * 1.3))
        ts = BASE_DAY + d * 86400 + 43200
        rows.append((ts, c * 1.001, c * 0.999, c))
    return rows

saved = bs.db.conn
try:
    up = mk_candles()
    fund_hi = [(BASE_DAY + d * 86400 + h * 3600, 1e-4)
               for d in range(-7, 57) for h in range(24)]
    bs.db.conn = TableConn({("candles", "XBTUSD"): up, ("funding", "PF_XBTUSD"): fund_hi})
    a3 = ap.Autopilot.__new__(ap.Autopilot)
    vias = {
        "trend": a3.score_counterfactual({"id": "t", "kind": "trend",
                                          "cf": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 20}}),
        "carry": a3.score_counterfactual({"id": "c", "kind": "carry",
                                          "cf": {"symbol": "PF_XBTUSD"}}),
        "switch": a3.score_counterfactual({"id": "s", "kind": "switch",
                                           "cf": {"pair": "XBTUSD", "symbol": "PF_XBTUSD"}}),
        "price": a3.score_counterfactual({"id": "p", "cf": {"conf": None, "horizon": "fwd48"}}),
    }
    check("kind dispatch: trend/carry/switch/price each hit their own scorer",
          vias["trend"]["via"] == "cf_trend" and vias["carry"]["via"] == "cf_carry"
          and vias["switch"]["via"] == "cf_switch" and vias["price"]["via"] == "cf",
          {k: (v or {}).get("via") for k, v in vias.items()})
finally:
    bs.db.conn = saved

# 4f. one-decision-per-ISO-week enforcement in the weekly machinery -----------
daily = ap.daily_bars_from_hourly(mk_candles())
weekly = ap.weekly_closes_from_daily(daily)
check("weekly aggregation: exactly one row per ISO week",
      len({ap._iso_week_key(w[1]) for w in weekly}) == len(weekly))
decisions = ap.tsmom_decisions(weekly, 20)
check("tsmom: one decision per ISO week, none inside the warm-up",
      len(decisions) == len(weekly) - 19
      and len({ap._iso_week_key(d[0]) for d in decisions}) == len(decisions),
      (len(weekly), len(decisions)))
check("tsmom goes long in a rising series", all(p == 1 for _, p in decisions[5:]))
dch = ap.donchian_decisions(daily, weekly, 50, 25)
check("donchian: one decision per ISO week",
      len({ap._iso_week_key(d[0]) for d in dch}) == len(dch) and len(dch) > 0)
check("donchian enters on a 50-day-high breakout in a rising series",
      any(p == 1 for _, p in dch))

# trend scorer end-to-end: post-epoch weeks only, costs charged, real variance
saved = bs.db.conn
try:
    bs.db.conn = TableConn({("candles", "XBTUSD"): mk_candles()})
    a4 = ap.Autopilot.__new__(ap.Autopilot)
    rt = a4._score_cf_trend({"id": "tt", "kind": "trend",
                             "cf": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 20}})
    check("trend scorer produces weekly decisions post-epoch",
          rt["n_oos"] >= 15, rt["n_oos"])
    check("trend scorer: rising series scores positive net of costs",
          rt["oos_edge"] is not None and rt["oos_edge"] > 0)
    check("trend nets are per-week (one per ISO week max)",
          rt["n_oos"] <= len(weekly))
    bs.db.conn = TableConn({})           # no candles at all
    rt0 = a4._score_cf_trend({"id": "tt", "kind": "trend",
                              "cf": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 20}})
    check("trend scorer with no price history says so",
          rt0["n_oos"] == 0 and "waiting on price history" in str(rt0.get("status")))
finally:
    bs.db.conn = saved

# 4g. carry entrant: hurdle logic + empty-table honesty -----------------------
def mk_funding():
    rows = []
    for d in range(-7, 57):
        for h in range(24):
            if d < 37:
                rate = -1e-5 if d in (20, 21, 22) else 1e-4   # rich carry, 3 bad days
            else:
                rate = 0.0                                     # carry dries up
            rows.append((BASE_DAY + d * 86400 + h * 3600, rate))
    return rows

saved = bs.db.conn
try:
    bs.db.conn = TableConn({("funding", "PF_XBTUSD"): mk_funding()})
    a5 = ap.Autopilot.__new__(ap.Autopilot)
    rc = a5._score_cf_carry({"id": "ch", "kind": "carry", "cf": {"symbol": "PF_XBTUSD"}})
    check("carry enters when 7d annualized funding clears the hurdle",
          rc["n_oos"] > 0, rc["n_oos"])
    check("carry exits when funding dries up (marks stop)",
          40 <= rc["n_oos"] <= 50, rc["n_oos"])
    check("carry accrual beats the 4-leg costs in a rich-funding stream",
          rc["oos_edge"] is not None and rc["oos_edge"] > 0)
    check("worst negative-funding streak tracked",
          rc.get("worst_neg_funding_days") == 3, rc.get("worst_neg_funding_days"))
    # funding below hurdle forever -> never enters, zero marks, no invention
    weak = [(BASE_DAY + d * 86400 + h * 3600, 1e-6)
            for d in range(-7, 30) for h in range(24)]
    bs.db.conn = TableConn({("funding", "PF_XBTUSD"): weak})
    rw2 = a5._score_cf_carry({"id": "ch", "kind": "carry", "cf": {"symbol": "PF_XBTUSD"}})
    check("carry below hurdle never enters", rw2["n_oos"] == 0, rw2["n_oos"])
    bs.db.conn = TableConn({})           # contract table absent/empty
    re_ = a5._score_cf_carry({"id": "ch", "kind": "carry", "cf": {"symbol": "PF_XBTUSD"}})
    check("empty funding table -> status 'waiting on funding history'",
          re_["n_oos"] == 0 and re_.get("status") == "waiting on funding history")
    check("empty funding table never clears", re_["clears_cost"] is False)
finally:
    bs.db.conn = saved

# 4h. switch entrant: allocates carry > trend > flat, scores the switch -------
saved = bs.db.conn
try:
    down = mk_candles(drift=-0.005)      # falling price: trend leg is NOT live
    rich = [(BASE_DAY + d * 86400 + h * 3600, 1e-4)
            for d in range(-200, 150) for h in range(24)]
    bs.db.conn = TableConn({("candles", "XBTUSD"): down, ("funding", "PF_XBTUSD"): rich})
    a6 = ap.Autopilot.__new__(ap.Autopilot)
    rs = a6._score_cf_switch({"id": "sw", "kind": "switch",
                              "cf": {"pair": "XBTUSD", "symbol": "PF_XBTUSD", "weeks": 20}})
    check("switch takes the carry sleeve when carry is live (falling price, +edge)",
          rs["n_oos"] > 0 and rs["oos_edge"] is not None and rs["oos_edge"] > 0,
          (rs["n_oos"], rs["oos_edge"]))
    # rising price, NO funding history -> trend sleeve, honest note about carry
    bs.db.conn = TableConn({("candles", "XBTUSD"): mk_candles()})
    rs2 = a6._score_cf_switch({"id": "sw", "kind": "switch",
                               "cf": {"pair": "XBTUSD", "symbol": "PF_XBTUSD", "weeks": 20}})
    check("switch degrades honestly when funding history is missing",
          "carry leg treated as not live" in str(rs2.get("status")))
    check("switch rides the trend sleeve when only trend is live",
          rs2["oos_edge"] is not None and rs2["oos_edge"] > 0)
    # falling price AND no funding -> flat every week, edge exactly 0
    bs.db.conn = TableConn({("candles", "XBTUSD"): down})
    rs3 = a6._score_cf_switch({"id": "sw", "kind": "switch",
                               "cf": {"pair": "XBTUSD", "symbol": "PF_XBTUSD", "weeks": 20}})
    check("switch stays flat when neither sleeve is live (edge 0, no crown)",
          rs3["n_oos"] >= 2 and rs3["oos_edge"] == 0 and rs3["clears_cost"] is False)
finally:
    bs.db.conn = saved

# 4i. new entrants registered honestly ----------------------------------------
NEW_IDS = {"tsmom_btc_20w", "donchian_btc", "tsmom_eth_20w",
           "carry_harvest", "carry_or_trend"}
ids2 = {c["id"] for c in ap.CHALLENGER_CONFIGS}
check("all five weekly/carry entrants exist", NEW_IDS <= ids2, sorted(NEW_IDS - ids2))
for c in ap.CHALLENGER_CONFIGS:
    if c["id"] in NEW_IDS:
        check(f"{c['id']} is cf_only", c.get("cf_only") is True)
        check(f"{c['id']} born_ts hardcoded AFTER the cf epoch",
              float(c.get("born_ts") or 0) > ap.AP_CF_EPOCH, c.get("born_ts"))
check("tsmom_btc_20w spec: 20-week SMA, fwd168",
      next(c for c in ap.CHALLENGER_CONFIGS if c["id"] == "tsmom_btc_20w")["cf"]
      == {"rule": "tsmom", "pair": "XBTUSD", "weeks": 20, "horizon": "fwd168"})
check("donchian_btc spec: enter 50d / exit 25d",
      next(c for c in ap.CHALLENGER_CONFIGS if c["id"] == "donchian_btc")["cf"]
      == {"rule": "donchian", "pair": "XBTUSD", "enter_days": 50, "exit_days": 25,
          "horizon": "fwd168"})

# 4j. dead weight: starved live books converted to cf_only --------------------
for cid in ("selective", "momentum", "reversion", "loose"):
    cc = next(c for c in ap.CHALLENGER_CONFIGS if c["id"] == cid)
    check(f"{cid} converted to cf_only (live path never fills it)",
          cc.get("cf_only") is True)
check("only base + high_conviction still run live sandbox books",
      {c["id"] for c in ap.CHALLENGER_CONFIGS if not c.get("cf_only")}
      == {"base", "high_conviction"})
check("no-breeding rule documented in code",
      "NO AUTO-MUTATION, NO BREEDING" in SRC_AP and "gap_fade" in SRC_AP)

# 4k. full instance: trials ledger, DB-less honesty, state round-trip ---------
import tempfile
_tmp = tempfile.mkdtemp()
_orig_sp = ap._state_path
ap._state_path = lambda: os.path.join(_tmp, "autopilot_state.json")
try:
    inst2 = ap.Autopilot()
    check("full instance counts the 5 new entrants into trials_count",
          inst2.trials_count >= ap.TRIALS_SEED + 5
          and NEW_IDS <= set(inst2.trials_ids),
          (inst2.trials_count, inst2.trials_ids))
    sc = inst2.score()          # DB-less: every cf path must degrade honestly
    check("DB-less carry entrant says 'waiting on funding history'",
          sc["carry_harvest"].get("status") == "waiting on funding history",
          sc["carry_harvest"].get("status"))
    check("DB-less trend entrants say 'waiting on price history'",
          "waiting on price history" in str(sc["tsmom_btc_20w"].get("status")))
    check("every entrant carries the survival fields",
          all(all(kk in s for kk in ("sr", "dsr", "psr", "min_trl", "trades_n", "verdict"))
              for s in sc.values()))
    check("no entrant is crowned on missing data",
          not any(s["clears_cost"] for s in sc.values()))
    check("nets stream never persisted in scores",
          not any("nets" in s for s in sc.values()))
    d = inst2.decide()
    check("decide() runs the full loop and stays FLAT with nothing proven",
          d["allocation"] == "FLAT")
    sdict = inst2._state_dict()
    check("state dict persists trials_count/trials_ids/killed",
          sdict["trials_count"] == inst2.trials_count
          and set(sdict["trials_ids"]) >= NEW_IDS and "killed" in sdict)
    json.dumps(sdict)            # must be JSON-serializable (raises on failure)
    check("state dict is JSON-serializable", True)
    st2 = inst2.status()
    check("status() ships trials_count + kill bar + killed map",
          st2["trials_count"] == inst2.trials_count and st2["kill_psr"] == ap.KILL_PSR
          and isinstance(st2["killed"], dict))
    check("status() challengers carry verdict + status_note",
          all("verdict" in c and "status_note" in c for c in st2["challengers"]))
    check("standings carry the survival columns",
          all(k in st2["standings"][0] for k in ("sr", "dsr", "psr", "min_trl", "verdict")))
    row = next(c for c in st2["challengers"] if c["id"] == "carry_harvest")
    check("carry entrant's waiting status reaches the dashboard payload",
          row["status_note"] == "waiting on funding history", row["status_note"])
except Exception as e:
    import traceback; traceback.print_exc()
    check("full instance DB-less survival flow", False, e)
finally:
    ap._state_path = _orig_sp

SRC_BS = bs._DASHBOARD_HTML
check("standings render in the dashboard", 'ap_standings' in SRC_BS)
check("standings colour only on CLEARS, not on a nice middle number",
      "r.clears_cost?'var(--g)'" in SRC_BS)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all tournament checks pass")
