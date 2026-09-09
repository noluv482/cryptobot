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

# ── 5. HYPOTHESIS INTAKE, GOAL BLOCK, SSE CONTRACT ────────────────────────────
# Contracts [H] (hypotheses file), [G] (goal block), [S] (SSE types), [E]
# (graveyard). Every check below RUNS the real autopilot code on a temp DATA_DIR
# with a captured SSE sink; nothing here is asserted in prose.
import shutil
import time as _t
try:
    import research_loop as _rl
except Exception:                                   # pragma: no cover
    _rl = None

INTAKE = 1_800_000_000.0          # fixed intake clock so born_ts math is exact


def hyp(**over):
    """A minimal VALID hypothesis entry ([H]); `over` breaks one thing at a time."""
    e = {
        "kind": "price",
        "family": "regime_gate",
        "cf": {"conf": 0.60, "adx": 25.0, "horizon": "fwd48"},
        "born_ts": INTAKE - 30 * 86400,             # backdated on purpose
        "origin": "llm_prereg",
        "note": "HYPOTHESIS: entries taken only when confidence and trend strength "
                "are both high should survive the round-trip cost the middle "
                "band cannot pay.",
        "prereg": {
            "mechanism": "cost per decision is fixed, so only the high-conviction "
                         "subset can clear it",
            "expected_decisions_per_month": 10,
            "mintrl_estimate_months": 9.0,
            "kill_bar": "deflated PSR < 0.20 once n >= MinTRL",
            "cost_model": "ROUND_TRIP_COST_PCT charged on every counterfactual fill",
        },
    }
    e.update(over)
    return e


def san(d, ts=INTAKE):
    return ap.sanitize_hypotheses(d, ts)


# 5a. sanitizer: the good case ------------------------------------------------
ok = san({"hyp_conf60_adx25": hyp()})
check("sanitize accepts a well-formed hypothesis", len(ok) == 1, ok)
if ok:
    o = ok[0]
    check("sanitized hypothesis is cf_only (never a live book)", o["cf_only"] is True)
    check("sanitized hypothesis keeps its id/kind/family/origin",
          (o["id"], o["kind"], o["family"], o["origin"])
          == ("hyp_conf60_adx25", "price", "regime_gate", "llm_prereg"))
    check("backdated born_ts is re-stamped to intake time (never earlier)",
          o["born_ts"] == INTAKE, o["born_ts"])
    check("cf is the scorer's whitelist only",
          set(o["cf"]) <= set(ap.HYP_CF_KEYS["price"]), o["cf"])
    check("prereg block survives with all five keys",
          all(k in o["prereg"] for k in ap.HYP_PREREG_KEYS), sorted(o["prereg"]))
    check("note keeps the HYPOTHESIS: mechanism", o["note"].startswith("HYPOTHESIS:"))

fut = san({"hyp_future": hyp(born_ts=INTAKE + 5000)})
check("a FUTURE born_ts is re-stamped too (no unkillable slot squatter)",
      len(fut) == 1 and fut[0]["born_ts"] == INTAKE, fut)
check("a missing born_ts becomes intake time, not 0",
      san({"hyp_nb": hyp(born_ts=None)})[0]["born_ts"] == INTAKE)
check("a garbage born_ts becomes intake time, not 0 or a crash",
      san({"hyp_gb": hyp(born_ts="last tuesday")})[0]["born_ts"] == INTAKE)

# 5b. sanitizer: every rejection path ------------------------------------------
BAD = {
    "bad id (no hyp_ prefix)":        {"conf60": hyp()},
    "bad id (uppercase)":             {"hyp_Conf60": hyp()},
    "bad id (too long)":              {"hyp_" + "a" * 25: hyp()},
    "id shadowing a built-in":        {"base": hyp()},
    "bad kind":                       {"hyp_k": hyp(kind="oracle")},
    "bad family":                     {"hyp_f": hyp(family="vibes")},
    "bad origin":                     {"hyp_o": hyp(origin="anonymous")},
    "cf key outside the scorer whitelist":
        {"hyp_cf": hyp(cf={"conf": 0.6, "secret_knob": 3, "horizon": "fwd48"})},
    "cf value out of range":          {"hyp_r": hyp(cf={"conf": 4.2, "horizon": "fwd48"})},
    "cf horizon not a recorded column":
        {"hyp_h": hyp(cf={"conf": 0.6, "horizon": "fwd999"})},
    "note missing the HYPOTHESIS: prefix":
        {"hyp_n": hyp(note="conf 0.6 and adx 25 should work nicely")},
    "note with no mechanism after the prefix":
        {"hyp_n2": hyp(note="HYPOTHESIS: x")},
    "prereg block missing":           {"hyp_p": hyp(prereg=None)},
    "entry is not an object":         {"hyp_x": 5},
    "cf is not an object":            {"hyp_c": hyp(cf="conf>0.6")},
}
for _name, _raw in BAD.items():
    check("sanitize drops: " + _name, san(_raw) == [], san(_raw))

for miss in ap.HYP_PREREG_KEYS:
    pre = dict(hyp()["prereg"])
    pre.pop(miss)
    check("sanitize drops a prereg missing '" + miss + "'",
          san({"hyp_pm": hyp(prereg=pre)}) == [])

# per-kind cf rules that exist so a hypothesis can never LOOSEN a bar
check("trend hypothesis must be graded on the next week (fwd168 only)",
      san({"hyp_t": hyp(kind="trend", family="trend",
                        cf={"rule": "tsmom", "pair": "XBTUSD", "weeks": 26,
                            "horizon": "fwd48"})}) == [])
check("carry hypothesis may only RAISE the hurdle (mult < 1 dropped)",
      san({"hyp_cy": hyp(kind="carry", family="carry",
                         cf={"symbol": "PF_XBTUSD", "hurdle_mult": 0.5})}) == [])
check("carry hypothesis with a 2x hurdle is accepted",
      len(san({"hyp_cy2": hyp(kind="carry", family="carry",
                              cf={"symbol": "PF_XBTUSD", "hurdle_mult": 2.0})})) == 1)

# 5c. slot cap + one bad entry never poisons the good ones ---------------------
many = {("hyp_slot%d" % i): hyp() for i in range(ap.HYP_MAX_SLOTS + 3)}
capped = san(many)
check("slot cap holds at HYP_MAX_SLOTS (%d)" % ap.HYP_MAX_SLOTS,
      len(capped) == ap.HYP_MAX_SLOTS, len(capped))
mixed = san({"hyp_good1": hyp(), "nope": hyp(), "hyp_good2": hyp(kind="oracle"),
             "hyp_good3": hyp()})
check("a bad sibling never drops the good entries",
      sorted(e["id"] for e in mixed) == ["hyp_good1", "hyp_good3"], mixed)

# 5d. garbage never raises -----------------------------------------------------
for junk in (None, [], "hyp_x", 7, {"hyp_x": None}, {"hyp_x": []},
             {"hyp_x": {"kind": "price"}},
             {"hyp_x": {"cf": {"conf": float("nan")}}},
             {"hyp_x": hyp(cf={"conf": None, "horizon": None})}):
    try:
        r = san(junk)
        check("garbage input %r returns a list, never raises" % (junk,),
              isinstance(r, list))
    except Exception as ex:
        check("garbage input %r returns a list, never raises" % (junk,), False, ex)

# 5e. kill reason codes ([E] graveyard) ---------------------------------------
check("reason_code 'cost': gross edge positive, net eaten",
      ap.kill_reason_code({"gross_edge": 0.004, "oos_edge": -0.001, "sr": 0.1}) == "cost")
check("reason_code 'regime_flip': first half up, second half down",
      ap.kill_reason_code({"gross_edge": 0.004, "oos_edge": 0.001, "sr": 0.1},
                          nets=[0.01] * 5 + [-0.01] * 5) == "regime_flip")
check("reason_code 'no_signal': nothing there before costs",
      ap.kill_reason_code({"gross_edge": -0.002, "oos_edge": -0.003, "sr": -0.1}) == "no_signal")
check("reason_code 'overlap_artifact': edge exists but the entrant is a near-copy",
      ap.kill_reason_code({"gross_edge": 0.004, "oos_edge": 0.002, "sr": 0.3},
                          nets=None, cluster_size=3) == "overlap_artifact")
check("reason_code 'unknown' when nothing can be established",
      ap.kill_reason_code({}) == "unknown")
check("reason_code never invents a code outside the contract set",
      all(ap.kill_reason_code(s) in ap.KILL_REASON_CODES
          for s in ({}, {"sr": None}, {"gross_edge": None, "oos_edge": None})))
check("a short nets stream cannot trigger regime_flip (needs >= 8)",
      ap.kill_reason_code({"gross_edge": 0.004, "oos_edge": 0.002, "sr": 0.3},
                          nets=[0.01, 0.01, -0.01, -0.01]) != "regime_flip")

# 5f. instance-level: SSE contract, budget refusal, goal block, graveyard ------
PUSHED = []
_orig_push_sse = bs._push_sse
_orig_state_path = ap._state_path
_orig_data_dir = bs._DATA_DIR
_tmp5 = tempfile.mkdtemp()
try:
    bs._push_sse = lambda t, d=None: PUSHED.append((t, d))
    bs._DATA_DIR = _tmp5
    ap._state_path = lambda: os.path.join(_tmp5, "autopilot_state.json")

    # two hypotheses on disk before boot: intake must register them exactly once
    with io.open(os.path.join(_tmp5, "hypotheses.json"), "w", encoding="utf-8") as f:
        json.dump({
            "hyp_conf60_adx25": hyp(),
            "hyp_tsmom_btc_52w": hyp(
                kind="trend", family="trend",
                cf={"rule": "tsmom", "pair": "XBTUSD", "weeks": 52, "horizon": "fwd168"},
                note="HYPOTHESIS: a 52-week lookback holds trends the 20-week rule "
                     "exits early; one decision per ISO week."),
        }, f)

    inst = ap.Autopilot()

    regs = [d for t, d in PUSHED if t == "autopilot_register"]
    reg_ids = [d["entrant"] for d in regs]
    check("[S] autopilot_register fires for a newly registered hypothesis",
          "hyp_conf60_adx25" in reg_ids and "hyp_tsmom_btc_52w" in reg_ids, reg_ids)
    check("[S] autopilot_register payload carries entrant/origin/family/born_ts/"
          "trials_count/n_eff",
          all(set(d) >= {"entrant", "origin", "family", "born_ts",
                         "trials_count", "n_eff"} for d in regs),
          [sorted(d) for d in regs[:1]])
    r0 = next(d for d in regs if d["entrant"] == "hyp_conf60_adx25")
    check("[S] register payload reports the hypothesis's real origin/family",
          (r0["origin"], r0["family"]) == ("llm_prereg", "regime_gate"), r0)
    check("[S] register payload's born_ts is the intake stamp, not the file's value",
          abs(float(r0["born_ts"]) - _t.time()) < 120
          and float(r0["born_ts"]) != INTAKE - 30 * 86400, r0["born_ts"])
    check("built-in entrants report origin 'builtin'",
          all(d["origin"] == "builtin" for d in regs
              if not d["entrant"].startswith(("hyp_", "lab_"))))

    n_before = len(regs)
    tc_before = inst.trials_count
    inst._register_trials()
    inst._register_trials()
    regs2 = [d for t, d in PUSHED if t == "autopilot_register"]
    check("[S] autopilot_register fires ONCE per id (re-registering is a no-op)",
          len(regs2) == n_before and inst.trials_count == tc_before,
          (len(regs2), n_before, inst.trials_count, tc_before))
    check("trials_count counted both hypotheses into the monotone ledger",
          {"hyp_conf60_adx25", "hyp_tsmom_btc_52w"} <= set(inst.trials_ids))
    check("registered hypotheses are cf_only entrants in the pool",
          all(inst.configs[c].get("cf_only") is True
              for c in ("hyp_conf60_adx25", "hyp_tsmom_btc_52w")))
    check("a hypothesis never gets a live sandbox trader",
          not any(c.startswith("hyp_") for c in inst.traders))

    # --- budget refusal ([G] budget_remaining 0 -> [S] autopilot_budget_exhausted)
    PUSHED.clear()
    inst._current_var_sr = lambda: 0.09        # sd_SR 0.30 -> SR0 far above 0.17
    check("budget_remaining is 0 when the hurdle out-climbs what 24m can resolve",
          inst.budget_remaining(52) == 0, inst.budget_remaining(52))
    new_cfg = san({"hyp_extra_gate": hyp(cf={"conf": 0.7, "horizon": "fwd24"})})
    inst._hyp_apply(new_cfg, mtime=_t.time())
    exh = [d for t, d in PUSHED if t == "autopilot_budget_exhausted"]
    check("[S] a refused registration pushes autopilot_budget_exhausted",
          len(exh) == 1 and set(exh[0]) >= {"trials_count", "sr0"}, exh)
    check("a budget-refused hypothesis never enters the pool",
          "hyp_extra_gate" not in inst.configs)
    inst._hyp_apply(new_cfg, mtime=_t.time())
    check("a budget refusal is announced once, not on every refresh",
          len([d for t, d in PUSHED if t == "autopilot_budget_exhausted"]) == 1)
    check("a refused hypothesis never bumps the trials ledger",
          "hyp_extra_gate" not in inst.trials_ids)

    inst._current_var_sr = lambda: None
    check("budget_remaining is None (unknown) when cross-entrant variance is unknown",
          inst.budget_remaining(52) is None)
    inst._current_var_sr = lambda: 1e-6        # tiny variance -> room again
    b_small = inst.budget_remaining(52)
    check("a small cross-entrant variance leaves budget", bool(b_small) and b_small > 0,
          b_small)
    check("budget is monotone non-increasing in trials_count",
          _rl is None or
          _rl.budget_remaining(1e-6, 5, 52) >= _rl.budget_remaining(1e-6, 500, 52))
    check("budget is non-decreasing in decisions per year",
          _rl is None or
          _rl.budget_remaining(1e-6, 20, 365) >= _rl.budget_remaining(1e-6, 20, 52))

    # --- proven rule ([G] proven + [S] autopilot_proven) ---------------------
    PUSHED.clear()
    inst.scores = {
        "p_yes":   {"id": "p_yes", "min_trl": 10, "trades_n": 22, "dsr": 0.97,
                    "family": "trend", "clears_cost": True},
        "p_thin":  {"id": "p_thin", "min_trl": 40, "trades_n": 22, "dsr": 0.99,
                    "family": "trend", "clears_cost": True},
        "p_weak":  {"id": "p_weak", "min_trl": 10, "trades_n": 22, "dsr": 0.90,
                    "family": "carry", "clears_cost": True},
        "p_dead":  {"id": "p_dead", "min_trl": 10, "trades_n": 22, "dsr": 0.99,
                    "family": "carry", "clears_cost": True},
        "p_blank": {"id": "p_blank", "min_trl": None, "trades_n": None, "dsr": None,
                    "family": "trend", "clears_cost": False},
    }
    inst.killed["p_dead"] = {"ts": _t.time(), "reason": "killed earlier"}
    inst._apply_proven_rule()
    prov = [d for t, d in PUSHED if t == "autopilot_proven"]
    check("PROVEN requires trades_n >= MinTRL AND DSR >= PROVEN_DSR",
          inst.proven == ["p_yes"], inst.proven)
    check("an entrant short of MinTRL is not proven", "p_thin" not in inst.proven)
    check("an entrant below DSR %.2f is not proven" % ap.PROVEN_DSR,
          "p_weak" not in inst.proven)
    check("a KILLED entrant can never be proven", "p_dead" not in inst.proven)
    check("an unmeasured entrant is not proven (None is not a pass)",
          "p_blank" not in inst.proven)
    check("[S] autopilot_proven payload = {entrant, dsr, n, min_trl}",
          len(prov) == 1 and set(prov[0]) >= {"entrant", "dsr", "n", "min_trl"}, prov)
    inst._apply_proven_rule()
    check("[S] autopilot_proven fires ONCE per entrant",
          len([d for t, d in PUSHED if t == "autopilot_proven"]) == 1)

    # --- clears ([S] autopilot_clears on the FIRST cost-gate flip) -----------
    PUSHED.clear()
    inst.scores = {
        "c_a":    {"id": "c_a", "clears_cost": True, "n_oos": 31, "sr": 0.42, "sr0": 0.30},
        "c_no":   {"id": "c_no", "clears_cost": False, "n_oos": 25, "sr": 0.05, "sr0": 0.30},
        "p_dead": {"id": "p_dead", "clears_cost": True, "n_oos": 25, "sr": 0.4, "sr0": 0.3},
    }
    inst._announce_clears()
    clr = [d for t, d in PUSHED if t == "autopilot_clears"]
    check("[S] autopilot_clears fires on the first clears_cost flip",
          [d["entrant"] for d in clr] == ["c_a"], clr)
    check("[S] autopilot_clears payload = {entrant, n, sr, sr0}",
          bool(clr) and set(clr[0]) >= {"entrant", "n", "sr", "sr0"}, clr)
    check("a KILLED entrant is never announced as clearing",
          "p_dead" not in inst.cleared_ids)
    inst._announce_clears()
    check("[S] autopilot_clears fires ONCE per entrant",
          len([d for t, d in PUSHED if t == "autopilot_clears"]) == 1)

    # --- switch ([S] autopilot_switch on a champion change) -------------------
    PUSHED.clear()
    inst.killed.pop("p_dead", None)
    fake_scores = {
        "sw_a": {"id": "sw_a", "clears_cost": True, "oos_edge": 0.004, "t": 3.1,
                 "n_oos": 60, "min_trl": None, "trades_n": None, "dsr": None},
        "sw_b": {"id": "sw_b", "clears_cost": True, "oos_edge": 0.001, "t": 2.2,
                 "n_oos": 60, "min_trl": None, "trades_n": None, "dsr": None},
    }

    def _fake_score():
        inst.scores = {k: dict(v) for k, v in fake_scores.items()}
        return inst.scores

    inst.score = _fake_score
    inst.refresh_lab_configs = lambda: None
    inst.refresh_hypotheses = lambda: None
    inst.champion_id = None
    d1 = inst.decide()
    sw = [d for t, d in PUSHED if t == "autopilot_switch"]
    check("FLAT -> champion pushes [S] autopilot_switch {from,to,why}",
          len(sw) == 1 and sw[0]["from"] is None and sw[0]["to"] == "sw_a"
          and isinstance(sw[0].get("why"), str) and bool(sw[0]["why"]), sw)
    check("decide() crowned the higher-edge entrant", d1["champion"] == "sw_a")
    inst.decide()
    check("holding the same champion pushes NO switch event",
          len([d for t, d in PUSHED if t == "autopilot_switch"]) == 1)
    fake_scores["sw_a"]["clears_cost"] = False
    fake_scores["sw_b"]["clears_cost"] = False
    d2 = inst.decide()
    sw2 = [d for t, d in PUSHED if t == "autopilot_switch"]
    check("losing every eligible entrant switches back to FLAT and says so",
          d2["allocation"] == "FLAT" and len(sw2) == 2
          and sw2[-1]["from"] == "sw_a" and sw2[-1]["to"] is None, sw2[-1:])

    # --- graveyard ([E]) ------------------------------------------------------
    inst.scores = {
        "g_kill": {"id": "g_kill", "min_trl": 10, "trades_n": 30, "dsr": 0.05,
                   "sr": 0.02, "sr0": 0.31, "gross_edge": 0.003, "oos_edge": -0.0005,
                   "n_oos": 30, "t": 0.3, "psr": 0.1, "clears_cost": True},
    }
    inst.configs["g_kill"] = {"id": "g_kill", "cf_only": True, "kind": "price",
                              "family": "exit_rule", "cf": {"horizon": "fwd24"},
                              "born_ts": ap.AP_CF_EPOCH + 1}
    inst._last_nets = {"g_kill": [0.001] * 15}
    inst._apply_kill_rule()
    check("the kill rule fired on a past-MinTRL, deflated-PSR entrant",
          "g_kill" in inst.killed)
    k = inst.killed["g_kill"]
    check("the kill record PRESERVES the original shape",
          all(kk in k for kk in ("ts", "reason", "config", "final_score")), sorted(k))
    check("[E] the kill record ADDS family/horizon/cost_model/reason_code/killed_ts",
          all(kk in k for kk in ("id", "family", "horizon", "cost_model",
                                 "reason_code", "sr", "sr0", "dsr", "n", "killed_ts")),
          sorted(k))
    check("[E] the reason_code is measured ('cost': gross positive, net negative)",
          k["reason_code"] == "cost", k["reason_code"])

    # an OLD-shape kill (pre-graveyard) normalizes without inventing anything
    inst.killed["g_legacy"] = {"ts": 1_700_000_000.0,
                               "reason": "killed before the graveyard existed",
                               "config": {"id": "g_legacy", "kind": "carry"},
                               "final_score": {"sr": 0.01, "dsr": 0.02, "trades_n": 44}}
    inst.killed["g_junk"] = "not a dict"
    gy = inst.graveyard()
    by_id = {g["id"]: g for g in gy}
    check("[E] graveyard returns one structured record per kill (junk skipped)",
          set(by_id) == {"g_kill", "g_legacy"}, sorted(by_id))
    lg = by_id["g_legacy"]
    check("[E] a legacy kill gets reason_code 'unknown', never a guessed one",
          lg["reason_code"] == "unknown", lg["reason_code"])
    check("[E] a legacy kill keeps its measured numbers from final_score",
          (lg["sr"], lg["dsr"], lg["n"]) == (0.01, 0.02, 44), lg)
    check("[E] a legacy kill's sr0 is None (never back-filled with today's hurdle)",
          lg["sr0"] is None, lg["sr0"])
    check("[E] a legacy kill's family comes from its config kind, not invention",
          lg["family"] == "carry", lg["family"])
    check("[E] every graveyard reason_code is inside the contract set",
          all(g["reason_code"] in ap.KILL_REASON_CODES for g in gy))
    check("[E] every graveyard record carries the full key set",
          all(set(g) >= {"id", "family", "horizon", "cost_model", "reason_code",
                         "sr", "sr0", "dsr", "n", "killed_ts"} for g in gy))

    # --- goal block ([G]) -----------------------------------------------------
    g = inst.goal()
    REQUIRED_GOAL = ("proven", "alive", "killed", "trials_count", "n_eff", "sd_sr",
                     "sr0", "nearest_verdict", "families", "budget_remaining",
                     "book_state")
    check("[G] goal block carries every contract key",
          all(kk in g for kk in REQUIRED_GOAL),
          [kk for kk in REQUIRED_GOAL if kk not in g])
    check("[G] goal.proven is the persisted proven list", g["proven"] == inst.proven)
    check("[G] goal.killed counts the graveyard, goal.alive the survivors",
          g["killed"] == len(inst.killed) and isinstance(g["alive"], int) and g["alive"] > 0,
          (g["killed"], g["alive"]))
    check("[G] goal.trials_count is the monotone ledger",
          g["trials_count"] == inst.trials_count)
    check("[G] nearest_verdict = {id, months}",
          set(g["nearest_verdict"]) == {"id", "months"}, g["nearest_verdict"])
    check("[G] families entries carry decisions_per_year/alive/killed/posterior{s,f}",
          all(set(v) >= {"decisions_per_year", "alive", "killed", "posterior"}
              and set(v["posterior"]) >= {"s", "f"} for v in g["families"].values()),
          {k2: sorted(v) for k2, v in list(g["families"].items())[:1]})
    check("[G] every contract family is present in the goal block",
          (set(ap.HYP_FAMILIES) | {"switch"}) <= set(g["families"]),
          sorted((set(ap.HYP_FAMILIES) | {"switch"}) - set(g["families"])))
    check("[G] weekly families report 52 decisions/year, carry 365",
          g["families"]["trend"]["decisions_per_year"] == 52
          and g["families"]["carry"]["decisions_per_year"] == 365,
          (g["families"]["trend"]["decisions_per_year"],
           g["families"]["carry"]["decisions_per_year"]))
    check("[G] the killed entrant shows up as a failure in its family's posterior",
          g["families"]["exit_rule"]["posterior"]["f"] >= 1
          and "g_kill" in g["families"]["exit_rule"]["killed"],
          g["families"]["exit_rule"])
    inst.champion_id = "sw_a"
    check("[G] book_state names the champion when one is crowned",
          inst.goal()["book_state"] == "champion:sw_a", inst.goal()["book_state"])
    inst.champion_id = None
    check("[G] book_state is 'flat' when nothing is crowned",
          inst.goal()["book_state"] == "flat")
    check("[G] budget_remaining is an int or an honest None, never a guess",
          g["budget_remaining"] is None or isinstance(g["budget_remaining"], int),
          g["budget_remaining"])
    check("[G] goal block is JSON-serializable (it ships over /api/goal)",
          isinstance(json.dumps(inst.goal()), str))

    # nearest_verdict picks the SOONEST pending verdict, and only a pending one
    inst.configs["nv_soon"] = {"id": "nv_soon", "cf_only": True, "kind": "price",
                               "family": "regime_gate", "cf": {"horizon": "fwd48"},
                               "born_ts": ap.AP_CF_EPOCH + 1}
    inst.configs["nv_late"] = {"id": "nv_late", "cf_only": True, "kind": "price",
                               "family": "regime_gate", "cf": {"horizon": "fwd48"},
                               "born_ts": ap.AP_CF_EPOCH + 1}
    inst.order.extend(["nv_soon", "nv_late"])
    inst.scores = {"nv_soon": {"id": "nv_soon", "months_to_verdict": 7.25},
                   "nv_late": {"id": "nv_late", "months_to_verdict": 31.0}}
    check("[G] nearest_verdict names the soonest pending verdict, with months",
          inst.goal()["nearest_verdict"] == {"id": "nv_soon", "months": 7.25},
          inst.goal()["nearest_verdict"])
    inst.proven.append("nv_soon")
    check("[G] an already-proven entrant is not the 'nearest verdict'",
          inst.goal()["nearest_verdict"]["id"] == "nv_late",
          inst.goal()["nearest_verdict"])
    inst.scores = {"nv_late": {"id": "nv_late", "months_to_verdict": None}}
    check("[G] nearest_verdict is {None, None} when nothing has a projection",
          inst.goal()["nearest_verdict"] == {"id": None, "months": None},
          inst.goal()["nearest_verdict"])
    inst.proven.remove("nv_soon")

    st5 = inst.status()          # self.killed still holds the 'g_junk' string
    check("a malformed kill record renders as 'unknown' instead of crashing status()",
          st5["killed"]["g_junk"]["reason_code"] == "unknown", st5["killed"].get("g_junk"))
    check("[G] the goal block is served inside status()",
          isinstance(st5.get("goal"), dict) and "trials_count" in st5["goal"])
    check("status() surfaces sd_sr / sr0 / n_eff next to the ledger",
          all(kk in st5 for kk in ("sd_sr", "sr0", "n_eff", "trials_count")),
          sorted(k2 for k2 in ("sd_sr", "sr0", "n_eff", "trials_count") if k2 not in st5))
    check("status() ships the structured graveyard",
          isinstance(st5.get("graveyard"), list)
          and all("reason_code" in r for r in st5["graveyard"]), st5.get("graveyard"))

    sd5 = inst._state_dict()
    check("state persists proven / cleared_ids / hyp_seen",
          all(kk in sd5 for kk in ("proven", "cleared_ids", "hyp_seen")), sorted(sd5))
    check("state is JSON-serializable with the graveyard records",
          isinstance(json.dumps(sd5), str))
except Exception as _e5:
    import traceback; traceback.print_exc()
    check("hypothesis/goal/SSE instance flow", False, _e5)
finally:
    bs._push_sse = _orig_push_sse
    bs._DATA_DIR = _orig_data_dir
    ap._state_path = _orig_state_path
    shutil.rmtree(_tmp5, ignore_errors=True)

# 5f2. N_eff / sd_SR / SR0 reach every verdict (requirement 1, end to end) ----
_a7 = ap.Autopilot.__new__(ap.Autopilot)
_a7.trials_count = 20
_a7.killed = {}
_a7.configs = {}
_a7.n_eff = _a7.n_clusters = _a7.sd_sr = _a7.sr0 = None
# two near-copies (same decision clock, rho > 0.7) + one independent stream
# 24 decisions each, not 12: an SR from a record shorter than
# MIN_SR_CONTRIB_DECISIONS no longer counts toward sd_SR (sr = t_{n-1}/sqrt(n-1),
# so a short record contributes its own sampling noise, not strategy dispersion).
# This block tests N_eff clustering and the SR0 wiring, so the streams are simply
# made long enough to be admissible; the correlation structure is unchanged.
_base = [0.004, -0.002, 0.006, -0.001, 0.003, -0.004, 0.005, 0.001,
         -0.003, 0.002, 0.004, -0.005, 0.003, -0.003, 0.007, -0.002,
         0.002, -0.006, 0.004, 0.002, -0.001, 0.005, -0.004, 0.003]
_twin = [x + (0.0002 if i % 2 else -0.0002) for i, x in enumerate(_base)]
_indep = [-0.003, 0.005, -0.006, 0.002, -0.001, 0.004, -0.005, 0.003,
          0.006, -0.002, -0.004, 0.001, -0.005, 0.002, 0.006, -0.003,
          0.001, 0.004, -0.002, -0.006, 0.005, -0.001, 0.003, -0.004]
_clock = [1_780_000_000.0 + i * 86400 for i in range(24)]
_nets = {"n_a": _base, "n_b": _twin, "n_c": _indep}
_tss = {"n_a": list(_clock), "n_b": list(_clock), "n_c": list(_clock)}
_out7 = {cid: {"id": cid, "n_oos": 24, "oos_edge": 0.001, "t": 1.0,
               "clears_cost": False, "via": "cf_price", "family": "regime_gate"}
         for cid in _nets}
_a7._attach_survival_stats(_out7, _nets, _tss)
check("N_eff collapses two near-copy streams into one cluster (3 scored -> 2)",
      _a7.n_clusters == 2, (_a7.n_clusters, _rl and _rl.cluster_streams(
          {k: dict(zip(_tss[k], v)) for k, v in _nets.items()})))
check("N_eff = trials_count minus the DEMONSTRATED redundancy (20 - 1 = 19)",
      _a7.n_eff == 19, _a7.n_eff)
check("trials_count itself never moves when entrants cluster",
      _a7.trials_count == 20)
check("sd_SR is measured across the entrants' per-decision SRs",
      _a7.sd_sr is not None and _a7.sd_sr > 0, _a7.sd_sr)
check("SR0 is the expected max SR of N_eff unskilled tries",
      _a7.sr0 is not None
      and abs(_a7.sr0 - ap.expected_max_sr(_a7.sd_sr ** 2, _a7.n_eff)) < 1e-12,
      (_a7.sr0, _a7.n_eff))
# The record-length floor. sr = mean/pstdev = t_{n-1}/sqrt(n-1) exactly, so under
# the null Var[sr] = 1/(n-3): infinite at n=3, no finite value at n=2. Admitting
# those made sd_SR a measure of record length -- 24 zero-skill entrants scored
# this way reproduce the live sd_SR 0.7012 to three decimals, which is how the
# hurdle reached an annualised Sharpe of 10+ and killed everything.
_a8 = ap.Autopilot.__new__(ap.Autopilot)
_a8.trials_count = 20
_a8.killed = {}
_a8.configs = {}
_a8.n_eff = _a8.n_clusters = _a8.sd_sr = _a8.sr0 = None
_short = {"s_a": _base[:4], "s_b": _indep[:4]}
_short_ts = {k: _clock[:4] for k in _short}
_out8 = {cid: {"id": cid, "n_oos": 4, "oos_edge": 0.001, "t": 1.0,
               "clears_cost": False, "via": "cf_price", "family": "regime_gate"}
         for cid in _short}
_a8._attach_survival_stats(_out8, _short, _short_ts)
check("an SR from a record under MIN_SR_CONTRIB_DECISIONS never enters sd_SR",
      _a8.sd_sr is None, (_a8.sd_sr, ap.MIN_SR_CONTRIB_DECISIONS))
check("and with no admissible SRs the hurdle is unknown, NOT zero",
      _a8.sr0 is None, _a8.sr0)

check("requirement 1: sd_SR, SR0 and N_eff are printed next to EVERY verdict",
      all(("sd_SR" in r["verdict"] and "SR0" in r["verdict"]
           and "N_eff" in r["verdict"]) for r in _out7.values()),
      [r["verdict"] for r in _out7.values()][:1])
check("every score row exposes sd_sr / sr0 / n_eff as numbers, not just prose",
      all(all(kk in r for kk in ("sd_sr", "sr0", "n_eff")) for r in _out7.values()),
      sorted(list(_out7.values())[0]))
check("a clustered entrant records its cluster size (feeds 'overlap_artifact')",
      _out7["n_a"].get("cluster_size") == 2 and _out7["n_c"].get("cluster_size") == 1,
      {k: v.get("cluster_size") for k, v in _out7.items()})
# an unmeasurable pool degrades to 'unknown', never to a flattering hurdle
_a8 = ap.Autopilot.__new__(ap.Autopilot)
_a8.trials_count = 20
_a8.killed = {}
_a8.configs = {}
_a8.n_eff = _a8.n_clusters = _a8.sd_sr = _a8.sr0 = None
_out8 = {"solo": {"id": "solo", "n_oos": 0, "oos_edge": None, "t": None,
                  "clears_cost": False, "via": "cf_price", "family": "trend"}}
_a8._attach_survival_stats(_out8, {"solo": []}, {"solo": None})
check("with no measurable variance sd_SR/SR0 say 'unknown' (never 0)",
      _a8.sd_sr is None and _a8.sr0 is None, (_a8.sd_sr, _a8.sr0))
check("an unknown hurdle never lets an entrant clear the gate",
      _out8["solo"]["clears_cost"] is False)
check("N_eff falls back to the full monotone ledger when nothing can cluster",
      _a8.n_eff == 20, _a8.n_eff)

# 5g. the templates the research pass may write all pass THIS sanitizer --------
if _rl is not None:
    for fam in _rl.EXPRESSIBLE_FAMILIES:
        tpl = _rl.family_templates(fam)
        check("every '%s' template passes the real sanitizer" % fam,
              len(tpl) > 0 and len(san(tpl)) == min(len(tpl), ap.HYP_MAX_SLOTS),
              (fam, len(tpl), len(san(tpl))))
    check("templates are cf_only and stamped at intake, like any hypothesis",
          all(e["cf_only"] is True and e["born_ts"] == INTAKE
              for e in san(_rl.family_templates("trend"))))
    check("the sanitizer and research_loop agree on the slot cap",
          ap.HYP_MAX_SLOTS == _rl.HYP_MAX_SLOTS)
    check("the sanitizer and research_loop agree on the family list",
          tuple(ap.HYP_FAMILIES) == tuple(_rl.FAMILIES))
    check("the sanitizer and research_loop agree on the origin allowlist",
          tuple(ap.HYP_ORIGINS) == tuple(_rl.ORIGINS))

SRC_BS = bs._DASHBOARD_HTML
check("standings render in the dashboard", 'ap_standings' in SRC_BS)
check("standings colour only on CLEARS, not on a nice middle number",
      "r.clears_cost?'var(--g)'" in SRC_BS)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all tournament checks pass")
