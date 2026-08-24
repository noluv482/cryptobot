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

SRC_BS = bs._DASHBOARD_HTML
check("standings render in the dashboard", 'ap_standings' in SRC_BS)
check("standings colour only on CLEARS, not on a nice middle number",
      "r.clears_cost?'var(--g)'" in SRC_BS)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all tournament checks pass")
