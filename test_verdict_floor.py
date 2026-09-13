"""test_verdict_floor.py — no verdict on a record that cannot carry one.

Plain script, no pytest: exit 0 == pass.

What happened. On 2026-09-09 a 20-decision floor was put on the cross-entrant
Sharpe VARIANCE, which took SR0 from 1.39 to 0.37 per decision. That was right.
But MinTRL is derived from SR0, so it fell too — and MinTRL was the ONLY
record-length test the kill and proven rules had. One day later tsmom_btc_20w
and carry_or_trend were killed with the reason "past MinTRL (2 decisions >= 2)
with deflated PSR 0.045". A Sharpe ratio on two decisions is a Cauchy variable:
it has no finite variance, so "PSR 0.045" on that record is not a measurement.
Kills are permanent by design. The floor now applies to VERDICTS, both signs.

Also pinned: a weekly bar whose week has not ended is not a week. The newest
ZECUSD 10080m bar started 2026-09-10 and was being graded as the week ending
the 17th, on the 13th. The daily-derived path emitted the in-progress ISO week
the same way. Two weekly entrants were graded on such rows.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import autopilot as ap                                       # noqa: E402

_failed: list = []


def check(label, cond, detail=""):
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        _failed.append(label)


def _bare(scores):
    """An Autopilot with only what the verdict rules touch."""
    a = ap.Autopilot.__new__(ap.Autopilot)
    a.scores = scores
    a.killed = {}
    a.proven = []
    a.configs = {cid: {"id": cid} for cid in scores}
    a._last_nets = {}
    a.champion_id = None            # the kill rule demotes a killed champion
    a._push = lambda *args, **kw: None
    return a


def _score(n, dsr, min_trl=2.0, sr=-0.3):
    return {"trades_n": n, "min_trl": min_trl, "dsr": dsr, "sr": sr, "sr0": 0.371,
            "psr": dsr, "family": "trend", "cluster_size": 1}


print("[1] the kill rule refuses a record under the floor")
check("VERDICT_MIN_DECISIONS mirrors the variance floor",
      ap.VERDICT_MIN_DECISIONS == ap.MIN_SR_CONTRIB_DECISIONS == 20,
      (ap.VERDICT_MIN_DECISIONS, ap.MIN_SR_CONTRIB_DECISIONS))

# the exact record that killed tsmom_btc_20w: n=2, MinTRL 2, DSR 0.045
a = _bare({"tsmom_btc_20w": _score(2, 0.045, min_trl=2.0)})
a._apply_kill_rule()
check("n=2 past a MinTRL of 2 with DSR 0.045 is NOT killed", "tsmom_btc_20w" not in a.killed,
      a.killed)

a = _bare({"x": _score(19, 0.045, min_trl=2.0)})
a._apply_kill_rule()
check("n=19 is still under the floor: not killed", "x" not in a.killed)

a = _bare({"x": _score(20, 0.045, min_trl=2.0)})
a._apply_kill_rule()
check("n=20 at the floor with DSR 0.045: killed", "x" in a.killed, a.killed.keys())
check("...and the reason still names MinTRL and the PSR",
      "past MinTRL" in a.killed["x"]["reason"] and "0.045" in a.killed["x"]["reason"])

a = _bare({"x": _score(40, 0.30, min_trl=2.0)})
a._apply_kill_rule()
check("a long record ABOVE KILL_PSR is not killed (rule otherwise unchanged)",
      "x" not in a.killed)

a = _bare({"x": _score(40, 0.045, min_trl=60.0)})
a._apply_kill_rule()
check("MinTRL still applies on its own: n=40 under a MinTRL of 60 is not killed",
      "x" not in a.killed)

print("\n[2] the proven rule refuses the same record")
a = _bare({"lucky": _score(2, 0.99, min_trl=2.0, sr=2.5)})
a._apply_proven_rule()
check("n=2 with DSR 0.99 is NOT proven", "lucky" not in a.proven, a.proven)
a = _bare({"lucky": _score(20, 0.99, min_trl=2.0, sr=2.5)})
a._apply_proven_rule()
check("n=20 with DSR 0.99 IS proven", "lucky" in a.proven, a.proven)
a = _bare({"lucky": _score(20, 0.90, min_trl=2.0, sr=2.5)})
a._apply_proven_rule()
check("n=20 with DSR 0.90 is not proven (PROVEN_DSR unchanged at 0.95)",
      "lucky" not in a.proven)

print("\n[3] a native weekly bar whose bucket has not ended is not a week")
WEEK = 7 * 86400
now = 1_789_300_000.0                      # some Sunday-ish moment
closed_start = now - 2 * WEEK              # ended a week ago
open_start = now - 3 * 86400               # started three days ago, ends in four
rows = [(closed_start, 1, 1, 100.0), (open_start, 1, 1, 200.0)]
out = ap.weekly_rows_from_native(rows, now=now)
check("the closed bucket is emitted", any(abs(r[2] - 100.0) < 1e-9 for r in out), out)
check("the OPEN bucket is dropped", not any(abs(r[2] - 200.0) < 1e-9 for r in out), out)
check("a bucket ending exactly now counts as closed",
      len(ap.weekly_rows_from_native([(now - WEEK, 1, 1, 5.0)], now=now)) == 1)
check("...and one ending one second later does not",
      len(ap.weekly_rows_from_native([(now - WEEK + 1, 1, 1, 5.0)], now=now)) == 0)
check("without `now` it defaults to the wall clock (a bar from 2030 is dropped)",
      len(ap.weekly_rows_from_native([(1_900_000_000, 1, 1, 5.0)])) == 0)

print("\n[4] the daily-derived path drops the in-progress ISO week")
def _day(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()
# Wed 2026-09-16 is `now`; ISO week 38 is open. Week 37 (7th-13th) has closed.
now = _day(2026, 9, 16) + 3600
daily = [(_day(2026, 9, dd), 1, 1, float(dd)) for dd in range(7, 17)]   # 7th..16th
out = ap.weekly_closes_from_daily(daily, now=now)
weeks = {k for k, *_ in out}
check("week 37 (closed) is emitted", (2026, 37) in weeks, weeks)
check("week 38 (open, contains now) is NOT emitted", (2026, 38) not in weeks, weeks)
check("the closed week's decision is its LAST day, Sunday the 13th",
      any(k == (2026, 37) and abs(r[1] - 13.0) < 1e-9 for k, *r in out), out)

print("\n" + "=" * 60)
if _failed:
    print("FAILED (%d): %s" % (len(_failed), ", ".join(_failed)))
    sys.exit(1)
print("all verdict-floor checks passed")
