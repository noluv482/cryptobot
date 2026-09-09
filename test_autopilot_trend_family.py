#!/usr/bin/env python3
"""The trend family had never traded — and the sd_SR refusal was mislabelled.

TWO measured problems, one file:

1. ROOT CAUSE (fixed here). autopilot._fetch_hourly_candles read ONLY
   interval_m=60. The 60m archive is a rolling ~45-day window (measured on the
   live DB 2026-09-07: XBTUSD/ETHUSD 1068 hourly bars back to 2026-07-24), so a
   20-week SMA or a 50-day Donchian channel could never be ASSEMBLED —
   tsmom_btc_20w, tsmom_eth_20w and donchian_btc sat at trades_n=0 forever.
   Not "the rule said flat": the rule was never evaluated. The candles table
   already holds the history at coarser intervals (XBTUSD: 32 bars at
   interval_m=10080 back to 2026-01-29, 50 at 1440 back to 2026-07-12), and
   _fetch_trend_bars now seeds the long lookback from those while keeping the
   60m read for the recent tail.

   NOTHING IS LOOSENED. The pre-registration rule (nothing at or before born_ts
   is graded), one decision per ISO week, the forward-week pairing and the spot
   round-trip cost are all unchanged — the tests below pin each of them on the
   NEW path. Where coarse bars are absent the behaviour degrades to exactly
   what it was, and says so.

2. sd_SR IS MEASURED, NOT CHANGED. sd_sr_diagnostic() decomposes the
   cross-entrant SR variance (who supplies it, on how many decisions) and
   prices out two labelled alternatives, and budget_status() attaches the
   binding constraint to a budget of 0 instead of the misleading word
   "exhausted". Both are REPORTS: the last two sections here pin that
   _current_var_sr, SR0, N_eff, the DSR gate and budget_remaining() all still
   return exactly what they returned before.
"""
import io
import math
import os
import statistics
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs
import autopilot as ap
import research_loop as rl

bs.log = lambda *a, **k: None
ap.log = lambda *a, **k: None
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


HERE = os.path.dirname(os.path.abspath(__file__))
SRC_AP = io.open(os.path.join(HERE, "autopilot.py"), encoding="utf-8").read()

WEEK = 7 * 86400
DAY = 86400
# Anchor everything AFTER AP_CF_EPOCH so the pre-registration rule lets the
# fixture decisions be graded at all (born_ts = max(AP_CF_EPOCH, cfg born)).
EPOCH_DAY = (int(ap.AP_CF_EPOCH) // DAY) * DAY


# ── fixture bar builders ─────────────────────────────────────────────────────
def price(i):
    """Deterministic rising series with real weekly variance (sd > 0)."""
    return 100.0 * (1.006 ** i) * (1 + 0.004 * math.sin(i * 1.3))


def hourly_bars(first_day, n_days):
    """interval_m=60 rows (ts, high, low, close) — the recent tail."""
    rows = []
    for d in range(n_days):
        for h in range(24):
            c = price((first_day - EPOCH_DAY) // DAY + d + h / 24.0)
            rows.append((first_day + d * DAY + h * 3600, c * 1.001, c * 0.999, c))
    return rows


def daily_bars(first_day, n_days):
    """interval_m=1440 rows — ts is the venue's day bucket START."""
    rows = []
    for d in range(n_days):
        c = price((first_day - EPOCH_DAY) // DAY + d)
        rows.append((first_day + d * DAY, c * 1.004, c * 0.996, c))
    return rows


def weekly_bars(first_bucket, n_weeks):
    """interval_m=10080 rows — ts is the epoch-aligned bucket START (Thursday),
    so the bar's close lands on the bucket's LAST day (the Wednesday)."""
    rows = []
    for w in range(n_weeks):
        ts = first_bucket + w * WEEK
        c = price((ts - EPOCH_DAY) // DAY + 6)
        rows.append((ts, c * 1.01, c * 0.99, c))
    return rows


class TableCursor:
    """Fake cursor that honours interval_m — the whole point of the fix is that
    the reader asks for more than one interval, so the fixture must be able to
    answer differently per interval."""
    def __init__(self, tables):
        self.tables = tables
        self._rows = []
        self.seen = tables.setdefault("_seen", [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql, params=None):
        if "FROM candles" in sql:
            pair, iv = params[0], int(params[1])
            self.seen.append(iv)
            self._rows = list(self.tables.get(("candles", pair, iv), []))
        elif "FROM funding_rates" in sql:
            self._rows = list(self.tables.get(("funding", params[0]), []))
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


class TableConn:
    def __init__(self, tables):
        self.tables = tables

    def cursor(self):
        return TableCursor(self.tables)


def with_db(tables, fn):
    saved = bs.db.conn
    bs.db.conn = TableConn(tables)
    try:
        return fn()
    finally:
        bs.db.conn = saved


AP_BARE = ap.Autopilot.__new__(ap.Autopilot)     # no __init__: pure scorer surface


# ═════════════════════════ 1. the reader asks for the coarse intervals ═══════
check("the 60m-only query is gone from the source",
      "interval_m=60 ORDER BY ts" not in SRC_AP)
check("_fetch_bars parameterises interval_m",
      "WHERE pair=%s AND interval_m=%s ORDER BY ts" in SRC_AP)
check("the coarse intervals are the real table's 10080 / 1440",
      (ap.WEEKLY_INTERVAL_M, ap.DAILY_INTERVAL_M) == (10080, 1440))

tables = {("candles", "XBTUSD", 60): hourly_bars(EPOCH_DAY - 30 * DAY, 40),
          ("candles", "XBTUSD", 1440): daily_bars(EPOCH_DAY - 60 * DAY, 60),
          ("candles", "XBTUSD", 10080): weekly_bars(EPOCH_DAY - 210 * DAY, 30)}
daily, weekly, src = with_db(tables, lambda: AP_BARE._fetch_trend_bars("XBTUSD"))
check("_fetch_trend_bars reads all three intervals, not just 60m",
      sorted(set(tables["_seen"])) == [60, 1440, 10080], sorted(set(tables["_seen"])))
check("weekly bars seed a lookback the 60m window cannot reach",
      len(weekly) >= 30 and src["weeks_from_native"] > 0,
      (len(weekly), src["weeks_from_native"]))
check("every weekly row is a distinct ISO week (one decision per week)",
      len({ap._iso_week_key(w[1]) for w in weekly}) == len(weekly))
check("weekly rows come back sorted by decision_ts",
      [w[1] for w in weekly] == sorted(w[1] for w in weekly))
check("the daily spine merges native 1440 bars with the 60m tail",
      len(daily) >= 60 + 10, len(daily))
check("bars_source discloses the seam between the two week sources",
      src["seam_ts"] is not None and src["weeks_from_daily"] > 0,
      (src["seam_ts"], src["weeks_from_daily"]))
check("native-only weeks carry daily_index None (no invented channel)",
      any(w[3] is None for w in weekly) and any(w[3] is not None for w in weekly))


# ═════════════════════════ 2. the honest degrade ladder ══════════════════════
# (a) only daily bars stored
t_daily = {("candles", "XBTUSD", 60): hourly_bars(EPOCH_DAY - 30 * DAY, 40),
           ("candles", "XBTUSD", 1440): daily_bars(EPOCH_DAY - 120 * DAY, 120),
           ("candles", "XBTUSD", 10080): []}
d2, w2, s2 = with_db(t_daily, lambda: AP_BARE._fetch_trend_bars("XBTUSD"))
check("only-daily: every week is derived from the daily spine",
      s2["weeks_from_native"] == 0 and s2["weeks_from_daily"] == len(w2) > 15,
      (s2["weeks_from_native"], len(w2)))
check("only-daily: the note says where the weeks came from",
      "daily spine" in s2["note"], s2["note"])

# (b) only 60m bars stored -> EXACTLY the pre-fix result, bar for bar
t_hourly = {("candles", "XBTUSD", 60): hourly_bars(EPOCH_DAY - 30 * DAY, 40),
            ("candles", "XBTUSD", 1440): [],
            ("candles", "XBTUSD", 10080): []}
d3, w3, s3 = with_db(t_hourly, lambda: AP_BARE._fetch_trend_bars("XBTUSD"))
legacy_daily = ap.daily_bars_from_hourly(t_hourly[("candles", "XBTUSD", 60)])
legacy_weekly = ap.weekly_closes_from_daily(legacy_daily)
check("only-60m: the daily spine is IDENTICAL to the pre-fix resampling",
      d3 == legacy_daily)
check("only-60m: the weekly series is IDENTICAL to the pre-fix one",
      w3 == legacy_weekly)
check("only-60m: the note says so instead of pretending",
      "60m only" in s3["note"], s3["note"])

# (c) nothing stored at any interval
d4, w4, s4 = with_db({}, lambda: AP_BARE._fetch_trend_bars("NOPE"))
check("no bars at all: empty series and an honest note",
      d4 == [] and w4 == [] and "no stored bars" in s4["note"], s4["note"])
r4 = with_db({}, lambda: AP_BARE._score_cf_trend(
    {"id": "x", "kind": "trend", "cf": {"rule": "tsmom", "pair": "NOPE", "weeks": 20}}))
check("no bars at all: the scorer still says 'waiting on price history'",
      r4["n_oos"] == 0 and "waiting on price history" in str(r4.get("status")))
check("a starved scorer reports WHICH intervals it found",
      isinstance(r4.get("bars_source"), dict)
      and r4["bars_source"]["native_weekly_rows"] == 0)

# (d) a read that FAILS is not the same as an interval that is empty
class BoomCursor(TableCursor):
    def execute(self, sql, params=None):
        raise RuntimeError("db exploded")


class BoomConn:
    def cursor(self):
        return BoomCursor({})


saved = bs.db.conn
bs.db.conn = BoomConn()
try:
    check("a failed candle read returns None (not a silent empty history)",
          AP_BARE._fetch_bars("XBTUSD", 10080) is None)
finally:
    bs.db.conn = saved


# ═════════════════════════ 3. a weekly entrant actually decides ══════════════
# 30 weekly bars of warm-up + 12 weeks of daily/60m tail, all post-epoch so the
# pre-registration rule admits them.
FEED = {("candles", "XBTUSD", 60): hourly_bars(EPOCH_DAY + 0 * DAY, 84),
        ("candles", "XBTUSD", 1440): daily_bars(EPOCH_DAY - 30 * DAY, 114),
        ("candles", "XBTUSD", 10080): weekly_bars(EPOCH_DAY - 224 * DAY, 28)}
TSMOM_CFG = {"id": "tsmom_btc_20w", "kind": "trend", "cf_only": True,
             "born_ts": ap.AP_CF_EPOCH,
             "cf": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 20, "horizon": "fwd168"}}
rt = with_db(FEED, lambda: AP_BARE._score_cf_trend(TSMOM_CFG))
check("a weekly entrant fed weekly bars produces > 0 GRADED decisions",
      rt["n_oos"] > 0, rt)
check("...and enough of them to be a real record, not a rounding artifact",
      rt["n_oos"] >= 8, rt["n_oos"])
check("graded decisions land on ISO-week boundaries (one per week, no repeats)",
      len({ap._iso_week_key(t) for t in rt["net_ts"]}) == len(rt["net_ts"]),
      rt["net_ts"])
gaps = sorted({int(b - a) for a, b in zip(rt["net_ts"], rt["net_ts"][1:])})
check("consecutive graded decisions are exactly one week apart",
      gaps in ([], [WEEK]), gaps)
check("the whole record is post-born (pre-registration rule intact)",
      all(t > max(ap.AP_CF_EPOCH, TSMOM_CFG["born_ts"]) for t in rt["net_ts"]))
check("the scored stream has real dispersion (sd > 0, so it can be scored)",
      len(rt["nets"]) >= 2 and statistics.pstdev(rt["nets"]) > 1e-12)
check("the entrant reports where its lookback came from",
      rt["bars_source"]["native_weekly_rows"] == 28
      and rt["bars_source"]["weeks_from_native"] > 0)

# the SAME entrant on 60m alone still cannot warm up — that IS the bug, pinned
rt_60 = with_db({("candles", "XBTUSD", 60): hourly_bars(EPOCH_DAY, 45)},
                lambda: AP_BARE._score_cf_trend(TSMOM_CFG))
check("REGRESSION PIN: 60m alone cannot reach a 20-week warm-up",
      rt_60["n_oos"] == 0 and "warm-up 20 weeks not met" in str(rt_60.get("status")),
      rt_60.get("status"))
check("...and the starved status now says how many weeks it actually has",
      "have " in str(rt_60.get("status")), rt_60.get("status"))

# costs are still charged on the new path. gross - net over the graded window
# must be an EXACT whole number of half round trips: one per entry, one per
# exit, nothing else. In this monotonically rising fixture the entry happens
# during the (ungraded) warm-up and the rule never exits, so the honest answer
# is zero charges in the graded window — not a cost quietly skipped.
def half_rts(res):
    return (res["gross_edge"] - res["oos_edge"]) * res["n_oos"] / (ap.CF_SPOT_RT / 2.0)


check("cost accounting is an exact whole number of half round trips",
      abs(half_rts(rt) - round(half_rts(rt))) < 1e-9 and round(half_rts(rt)) >= 0,
      half_rts(rt))
check("the cost charged is the honest spot round trip, unchanged",
      abs(ap.CF_SPOT_RT - 2.0 * (bs.KRAKEN_FEE + bs.SLIPPAGE)) < 1e-12)

# a fixture that actually flips post-born: rise through the weekly warm-up,
# then collapse through the graded tail so tsmom crosses below its 20w SMA.
def falling_tail(first_day, n_days, per_day=0.97):
    rows = []
    peak = price((first_day - EPOCH_DAY) // DAY)
    for d in range(n_days):
        c = peak * (per_day ** d) * (1 + 0.004 * math.sin(d * 1.3))
        rows.append((first_day + d * DAY, c * 1.004, c * 0.996, c))
    return rows


FALL = {("candles", "XBTUSD", 60): [],
        ("candles", "XBTUSD", 1440): falling_tail(EPOCH_DAY, 120),
        ("candles", "XBTUSD", 10080): weekly_bars(EPOCH_DAY - 224 * DAY, 32)}
rf = with_db(FALL, lambda: AP_BARE._score_cf_trend(TSMOM_CFG))
check("a post-born regime flip is graded and CHARGED the round trip",
      rf["n_oos"] > 0 and round(half_rts(rf)) >= 1, (rf["n_oos"], half_rts(rf)))
check("...and the charge is exactly CF_SPOT_RT/2 per transition, not a fudge",
      abs(half_rts(rf) - round(half_rts(rf))) < 1e-9, half_rts(rf))
check("the falling fixture goes flat (the rule is evaluated, not stuck long)",
      rf["oos_edge"] is not None and rf["oos_edge"] > -1.0)

# donchian: needs the DAILY channel, and must skip weeks with no daily behind
dch_cfg = {"id": "donchian_btc", "kind": "trend", "cf_only": True,
           "born_ts": ap.AP_CF_EPOCH,
           "cf": {"rule": "donchian", "pair": "XBTUSD", "enter_days": 50,
                  "exit_days": 25, "horizon": "fwd168"}}
rd = with_db(FEED, lambda: AP_BARE._score_cf_trend(dch_cfg))
check("donchian produces graded decisions once the daily spine is deep enough",
      rd["n_oos"] > 0, rd.get("status"))
check("donchian decisions are one per ISO week too",
      len({ap._iso_week_key(t) for t in rd["net_ts"]}) == len(rd["net_ts"]))
mixed = ap.weekly_series(ap.daily_bars_from_hourly(hourly_bars(EPOCH_DAY, 84)),
                         weekly_bars(EPOCH_DAY - 224 * DAY, 28))
check("donchian SKIPS native-only weeks instead of inventing a channel",
      all(dts >= EPOCH_DAY for dts, _ in
          ap.donchian_decisions(ap.daily_bars_from_hourly(hourly_bars(EPOCH_DAY, 84)),
                                mixed, 50, 25)))

# native weekly bars ALONE: tsmom can decide, donchian honestly cannot
ONLY_W = {("candles", "XBTUSD", 10080): weekly_bars(EPOCH_DAY - 210 * DAY, 40)}
rw_only = with_db(ONLY_W, lambda: AP_BARE._score_cf_trend(TSMOM_CFG))
check("weekly bars alone are enough for a 20-week SMA entrant",
      rw_only["n_oos"] > 0, rw_only.get("status"))
rd_only = with_db(ONLY_W, lambda: AP_BARE._score_cf_trend(dch_cfg))
check("weekly bars alone are NOT enough for a 50-DAY channel — and it says so",
      rd_only["n_oos"] == 0 and "warm-up 50 days not met" in str(rd_only.get("status")),
      rd_only.get("status"))
check("...naming the interval it actually found, so the gap is actionable",
      rd_only["bars_source"]["native_weekly_rows"] == 40
      and rd_only["bars_source"]["days"] == 0)

# the switch entrant reads the same spine
rs = with_db({**FEED, ("funding", "PF_XBTUSD"):
              [(EPOCH_DAY + d * DAY + h * 3600, 1e-5)
               for d in range(-10, 90) for h in range(24)]},
             lambda: AP_BARE._score_cf_switch(
                 {"id": "carry_or_trend", "kind": "switch", "born_ts": ap.AP_CF_EPOCH,
                  "cf": {"pair": "XBTUSD", "symbol": "PF_XBTUSD", "weeks": 20}}))
check("the switch entrant reads the coarse spine too",
      rs["n_oos"] > 0 and rs["bars_source"]["weeks_from_native"] > 0, rs.get("status"))

# a weekly bar's close belongs to the LAST day of its bucket, not the first
wb = ap.weekly_rows_from_native([(EPOCH_DAY, 1.0, 1.0, 42.0)])
check("a native weekly bar's decision_ts is its bucket's LAST day",
      wb[0][1] == EPOCH_DAY + 6 * DAY and wb[0][2] == 42.0, wb)
check("...and it carries no daily_index", wb[0][3] is None)
check("native weekly bars collapse to one row per ISO week",
      len(ap.weekly_rows_from_native(weekly_bars(EPOCH_DAY, 30))) == 30)

# merge precedence: a day covered by both keeps the venue's native bar
merged = ap.merge_daily_bars([(EPOCH_DAY, 9.0, 1.0, 5.0)],
                             [(EPOCH_DAY + 3600, 2.0, 2.0, 2.0)])
check("a day in both sources keeps the NATIVE bar", merged == [(EPOCH_DAY, 9.0, 1.0, 5.0)])
check("merge with no native bars == plain hourly resampling",
      ap.merge_daily_bars([], hourly_bars(EPOCH_DAY, 5))
      == ap.daily_bars_from_hourly(hourly_bars(EPOCH_DAY, 5)))


# ═════════════════════════ 4. sd_SR diagnostic arithmetic ════════════════════
def fixture_ap(scores, configs=None, killed=None, trials=21, n_eff=21):
    a = ap.Autopilot.__new__(ap.Autopilot)
    a.scores = scores
    a.configs = configs if configs is not None else {
        cid: {"id": cid, "kind": "price"} for cid in scores}
    a.killed = killed or {}
    a.trials_count = trials
    a.n_eff = n_eff
    a.n_clusters = None
    a.sd_sr = a.sr0 = None
    return a


# Deliberately lopsided, like the real board: one 4-decision entrant carrying
# most of the variance, three long-record entrants clustered near each other.
SCORES = {
    "spike":  {"sr": 1.40, "trades_n": 4},
    "slow_a": {"sr": 0.02, "trades_n": 40},
    "slow_b": {"sr": -0.03, "trades_n": 55},
    "slow_c": {"sr": 0.05, "trades_n": 31},
    "nosr":   {"sr": None, "trades_n": 9},          # must be ignored entirely
}
CFGS = {"spike": {"id": "spike", "kind": "carry"},
        "slow_a": {"id": "slow_a", "kind": "price"},
        "slow_b": {"id": "slow_b", "kind": "price"},
        "slow_c": {"id": "slow_c", "kind": "trend"},
        "nosr": {"id": "nosr", "kind": "price"}}
A = fixture_ap(SCORES, CFGS, killed={"spike": {"reason": "cost"}})
D = A.sd_sr_diagnostic()

# "spike" is the pathology this fixture was built to show: sr 1.40 on FOUR
# decisions, carrying most of the variance. Since 2026-09-09 the
# MIN_SR_CONTRIB_DECISIONS floor is APPLIED, so it no longer counts -- under the
# null Var[sr] = 1/(n-3), which has no finite value anywhere near n=4, so that
# 1.40 is sampling noise being read as strategy dispersion. Dropping it takes the
# variance from 0.36135 to 0.00109, a 332x reduction, and that is the whole fix.
srs_admissible = [SCORES[k]["sr"] for k in ("slow_a", "slow_b", "slow_c")]
exp_var = statistics.pvariance(srs_admissible)
check("the diagnostic decomposes EXACTLY the variance _current_var_sr computes",
      abs(D["measured"]["var_sr"] - A._current_var_sr()) < 1e-15
      and abs(D["measured"]["var_sr"] - exp_var) < 1e-15,
      (D["measured"]["var_sr"], A._current_var_sr(), exp_var))
check("sd_sr is the square root of that variance",
      abs(D["measured"]["sd_sr"] - math.sqrt(exp_var)) < 1e-15)
check("the 4-decision entrant is EXCLUDED from the variance, not merely flagged",
      abs(D["measured"]["var_sr"] - statistics.pvariance(
          srs_admissible + [SCORES["spike"]["sr"]])) > 0.3,
      D["measured"]["var_sr"])
check("...but it is still LISTED, with a stated reason",
      any(c["id"] == "spike" and c["admissible"] is False
          and "floor" in (c.get("excluded_reason") or "")
          for c in D["contributors"]),
      [(c["id"], c.get("admissible"), c.get("excluded_reason")) for c in D["contributors"]])
check("an excluded entrant carries no variance share",
      all(c["variance_share"] is None
          for c in D["contributors"] if not c["admissible"]))
check("the diagnostic reports the floor as APPLIED",
      D["applied"] is True, D["applied"])
check("an entrant with no SR contributes nothing and is not listed",
      "nosr" not in {c["id"] for c in D["contributors"]})
check("every contributor reports id / sr / decisions_n / cadence",
      all({"id", "sr", "decisions_n", "decisions_per_year"} <= set(c)
          and c["decisions_per_year"] in (52, 365) for c in D["contributors"]))
# Shares are defined over the ADMISSIBLE set, so excluded rows carry None and
# the sum is taken over the rows that actually make up the variance.
shares = [c["variance_share"] for c in D["contributors"] if c["admissible"]]
check("variance shares sum to exactly 1 over the admissible set",
      abs(sum(shares) - 1.0) < 1e-12, sum(shares))
mean_sr = statistics.fmean(srs_admissible)
tot = sum((v - mean_sr) ** 2 for v in srs_admissible)
spike = next(c for c in D["contributors"] if c["id"] == "spike")
# The exemplar moved off "spike" on purpose: it is excluded now, so it has no
# share to check. slow_b is the largest admissible deviation.
exemplar = next(c for c in D["contributors"] if c["id"] == "slow_b")
check("each share is that entrant's squared deviation over the total",
      abs(exemplar["variance_share"] - (SCORES["slow_b"]["sr"] - mean_sr) ** 2 / tot) < 1e-12)
check("the top contributor is now the largest ADMISSIBLE deviation, not the 4-decision spike",
      D["top_contributor"]["id"] == "slow_b" and D["top_contributor"]["variance_share"] > 0.5,
      D["top_contributor"])
check("the excluded spike can no longer be the top contributor",
      D["top_contributor"]["id"] != "spike")
check("the report still says how many ADMISSIBLE contributors are KILLED",
      D["measured"]["n_killed_contributors"] == 0, D["measured"]["n_killed_contributors"])
check("...and the killed short-record entrant is still visible in the list",
      spike["killed"] is True and spike["admissible"] is False)
check("deviation is reported next to the share",
      abs(exemplar["deviation"] - (SCORES["slow_b"]["sr"] - mean_sr)) < 1e-12)

# alternative (a): require n >= MIN_SR_CONTRIB_DECISIONS
alt_a = D["alternatives"]["a_min_decisions"]
kept = [k for k in ("slow_a", "slow_b", "slow_c") if SCORES[k]["trades_n"] >= ap.MIN_SR_CONTRIB_DECISIONS]
check("(a) drops the short-record entrant and keeps the rest",
      sorted(alt_a["contributors"]) == sorted(kept) and "spike" not in alt_a["contributors"],
      alt_a["contributors"])
check("(a) reports the variance over exactly the survivors",
      abs(alt_a["var_sr"] - statistics.pvariance([SCORES[k]["sr"] for k in kept])) < 1e-15)
# (a) WAS the proposal to apply this floor. It is applied now, so (a) and
# "measured" describe the same set and must agree exactly. Keeping the key and
# asserting the identity is how a future silent divergence gets caught.
check("(a) now EQUALS measured — it is the applied rule, no longer a proposal",
      abs(alt_a["sd_sr"] - D["measured"]["sd_sr"]) < 1e-15,
      (alt_a["sd_sr"], D["measured"]["sd_sr"]))
check("(a) shares sum to 1 over its own contributor set",
      abs(sum(alt_a["variance_shares"].values()) - 1.0) < 1e-12)
check("(a) reports the SR0 that variance implies at the SAME N_eff",
      abs(alt_a["sr0"] - ap.expected_max_sr(alt_a["var_sr"], alt_a["n_eff_used"])) < 1e-15)
check("(a) prices out what budget_remaining WOULD be",
      alt_a["budget_remaining"] == rl.budget_remaining(
          alt_a["var_sr"], A.trials_count, D["measured"]["decisions_per_year_used"]))
check("the payload reports the floor as APPLIED",
      D["applied"] is True, (D["applied"], alt_a["note"]))
# TRIPWIRE, re-pointed 2026-09-09. It used to assert the floor was compared in
# exactly ONE place and that the place was the diagnostic, "not a gate" -- it
# existed to stop the floor being applied silently. The floor is now applied
# DELIBERATELY, so the tripwire guards the new invariant instead: the two paths
# that feed a hurdle must BOTH apply it, because one filtering and the other not
# is how the live budget and the persisted budget silently disagree.
check("the floor is applied in the tournament scorer",
      "len(nets) >= MIN_SR_CONTRIB_DECISIONS" in SRC_AP)
check("the floor is applied in the persisted _current_var_sr path too",
      '(s.get("trades_n") or 0) >= MIN_SR_CONTRIB_DECISIONS' in SRC_AP)
check("both hurdle paths apply it — neither may drift from the other",
      SRC_AP.count(">= MIN_SR_CONTRIB_DECISIONS") >= 3)
check("the old n>=2 admission is gone from the scorer",
      "if len(nets) >= 2:" not in SRC_AP)

# alternative (b): rescale by 1/sqrt(decisions/year), exactly as written
alt_b = D["alternatives"]["b_rescaled"]
resc = [SCORES[c]["sr"] / math.sqrt(ap.decisions_per_year_of(CFGS[c]))
        for c in sorted(SCORES) if SCORES[c]["sr"] is not None]
check("(b) states the formula it actually used",
      alt_b["formula"] == "sr / sqrt(decisions_per_year)")
check("(b) computes the variance of the rescaled SRs",
      abs(alt_b["var_sr"] - statistics.pvariance(resc)) < 1e-15,
      (alt_b["var_sr"], statistics.pvariance(resc)))
check("(b) keeps every contributor (it rescales, it does not filter)",
      alt_b["n_contributors"] == D["measured"]["n_contributors"] == 4)
check("(b) shares sum to 1", abs(sum(alt_b["variance_shares"].values()) - 1.0) < 1e-12)
check("(b) discloses that the conventional annualization MULTIPLIES instead",
      "sr * sqrt(decisions_per_year)" in alt_b["note"] and "UNAPPLIED" in alt_b["note"])
check("(b) prices out its own budget too",
      alt_b["budget_remaining"] == rl.budget_remaining(
          alt_b["var_sr"], A.trials_count, D["measured"]["decisions_per_year_used"]))

# too few entrants to measure anything -> None, never a fabricated 0
D1 = fixture_ap({"only": {"sr": 0.3, "trades_n": 30}}).sd_sr_diagnostic()
check("one entrant: variance is None (unknown), never 0",
      D1["measured"]["var_sr"] is None and D1["measured"]["sd_sr"] is None)
check("one entrant: its share is None, not 1.0",
      D1["contributors"][0]["variance_share"] is None)
D0 = fixture_ap({}).sd_sr_diagnostic()
check("no entrants at all: the report is empty, not crashed",
      D0["contributors"] == [] and D0["top_contributor"] is None)


# ═════════════════════════ 5. the refusal says WHY ═══════════════════════════
# sd_SR big enough that SR0 out-climbs what 24 months can resolve.
# Records are ADMISSIBLE (>= MIN_SR_CONTRIB_DECISIONS) on purpose. This block
# tests that a refused budget names sd_sr as the binding lever; at trades_n=4
# the entrants would now be excluded and the variance unmeasurable, which is a
# different refusal ("cannot measure") and not the one under test. The SRs stay
# far apart so the dispersion, not the record length, drives SR0.
BIG = fixture_ap({"a": {"sr": 1.2, "trades_n": 40}, "b": {"sr": -1.2, "trades_n": 40}},
                 trials=21)
bstat = BIG.budget_status()
check("a refused budget is 0 and agrees with budget_remaining()",
      bstat["budget_remaining"] == 0 == BIG.budget_remaining(), bstat["budget_remaining"])
check("the refusal names sd_sr as the binding constraint, not 'exhausted'",
      bstat["binding_constraint"] == "sd_sr" and "exhausted" not in bstat["reason"], bstat)
check("binding_constraint is decided by driving each lever to its FLOOR",
      bstat["would_be_budget_if_sd_sr_zero"] > 0
      and bstat["would_be_budget_if_trials_at_floor"] == 0, bstat)
check("the refusal reports the sd_SR at which ONE more registration is honest",
      isinstance(bstat["sd_sr_for_budget_1"], float)
      and 0.0 < bstat["sd_sr_for_budget_1"] < bstat["sd_sr"], bstat["sd_sr_for_budget_1"])
check("...and that number really does reopen the budget (bisection is sound)",
      rl.budget_remaining(bstat["sd_sr_for_budget_1"] ** 2, BIG.trials_count,
                          bstat["decisions_per_year"]) >= 1
      and rl.budget_remaining((bstat["sd_sr_for_budget_1"] * 1.5) ** 2,
                              BIG.trials_count, bstat["decisions_per_year"]) == 0)
check("sr0's basis is named (the budget's N is trials_count, not N_eff)",
      "trials_count" in bstat["sr0_basis"] and "N_eff" in bstat["sr0_basis"])
check("the refusal carries sd_sr, sr0 and the plausible SR it was measured against",
      all(isinstance(bstat[k], float) for k in ("sd_sr", "sr0", "plausible_sr", "threshold"))
      and abs(bstat["plausible_sr"] - rl.PLAUSIBLE_SR) < 1e-12)
check("the refusal reports what the budget WOULD be if sd_sr halved",
      bstat["would_be_budget_if_sd_sr_halved"] == rl.budget_remaining(
          BIG._current_var_sr() / 4.0, BIG.trials_count, bstat["decisions_per_year"]))
check("...and what it would be at the trials seed, so breadth can be ruled out",
      bstat["would_be_budget_if_trials_at_seed"] == rl.budget_remaining(
          BIG._current_var_sr(), ap.TRIALS_SEED, bstat["decisions_per_year"]))
check("MEASURED CLAIM: with this sd_SR, resetting trials does NOT reopen the budget",
      bstat["would_be_budget_if_trials_at_seed"] == 0
      and bstat["would_be_budget_if_trials_at_floor"] == 0
      and bstat["binding_constraint"] != "trials_count", bstat)
# A cadence so slow that 24 months cannot resolve a plausible edge AT ALL: then
# the horizon really is the wall, and the report must say so instead of blaming
# sd_SR. resolution_threshold(1/yr) = 1.645/sqrt(2) = 1.163 > PLAUSIBLE_SR.
slow = BIG.budget_status(decisions_per_year=1)
check("when no sd_SR could reopen it, the horizon is named, not sd_sr",
      slow["binding_constraint"] == "resolution_horizon"
      and slow["sd_sr_for_budget_1"] is None
      and "resolution horizon is the wall" in slow["reason"], slow["binding_constraint"])
check("the reason sentence is readable English with the numbers in it",
      "binding constraint" in bstat["reason"] and "sd_SR" in bstat["reason"]
      and "SR0(" in bstat["reason"], bstat["reason"])

SMALL = fixture_ap({"a": {"sr": 0.01, "trades_n": 40}, "b": {"sr": -0.01, "trades_n": 40}},
                   trials=21)
sstat = SMALL.budget_status()
check("an OPEN budget reports binding_constraint 'none'",
      sstat["budget_remaining"] > 0 and sstat["binding_constraint"] == "none", sstat)
UNK = fixture_ap({"a": {"sr": 0.2, "trades_n": 40}}, trials=21)
ustat = UNK.budget_status()
check("an unmeasurable budget says 'unknown', never 0",
      ustat["budget_remaining"] is None
      and "no cross-entrant SR variance" in ustat["binding_constraint"], ustat)
check("budget_status defaults to the SAME cadence budget_remaining() uses",
      BIG.budget_status()["decisions_per_year"] == BIG._budget_dpy())


# ═════════════════════ 6. GUARD: the floor is applied, in BOTH paths ═════════
# This section used to pin _current_var_sr byte-for-byte and assert it did NOT
# apply MIN_SR_CONTRIB_DECISIONS -- a tripwire against changing the science
# silently. The floor was applied deliberately on 2026-09-09 (see the constant),
# so the guard now pins the OPPOSITE invariant. What it protects is unchanged in
# spirit: the live hurdle and the persisted hurdle must be computed the same way,
# because one filtering while the other does not is how the budget silently
# changes across a restart with no new evidence.
check("_current_var_sr applies the record-length floor",
      '(s.get("trades_n") or 0) >= MIN_SR_CONTRIB_DECISIONS' in SRC_AP)
check("_current_var_sr agrees EXACTLY with the diagnostic's measured variance",
      abs(A._current_var_sr() - D["measured"]["var_sr"]) < 1e-15,
      (A._current_var_sr(), D["measured"]["var_sr"]))
check("a board of only short records yields None, not a fabricated variance",
      fixture_ap({"x": {"sr": 1.1, "trades_n": 4},
                  "y": {"sr": -1.1, "trades_n": 5}})._current_var_sr() is None)
check("_current_var_sr still returns None below 2 admissible entrants",
      fixture_ap({"only": {"sr": 0.3, "trades_n": 30}})._current_var_sr() is None
      and fixture_ap({})._current_var_sr() is None)
check("an entrant with no SR is still ignored entirely",
      abs(fixture_ap({"a": {"sr": 0.02, "trades_n": 40},
                      "b": {"sr": -0.03, "trades_n": 55},
                      "z": {"sr": None, "trades_n": 90}})._current_var_sr()
          - statistics.pvariance([0.02, -0.03])) < 1e-15)
check("the docstring still says the diagnostic itself only reports",
      "THIS FUNCTION STILL CHANGES NOTHING" in SRC_AP)
check("...and states plainly that the floor it reports on IS applied",
      "is now APPLIED in" in SRC_AP)
check("...and records that applying it does not unpark the book",
      "does NOT unpark the book" in SRC_AP)

before = (A._current_var_sr(), A.budget_remaining(), A.trials_count,
          A.sd_sr, A.sr0, A.n_eff, dict(A.scores), dict(A.killed))
for _ in range(3):
    A.sd_sr_diagnostic()
    A.budget_status()
after = (A._current_var_sr(), A.budget_remaining(), A.trials_count,
         A.sd_sr, A.sr0, A.n_eff, dict(A.scores), dict(A.killed))
check("running the diagnostic changes NO state and NO gate output", before == after)
check("budget_remaining() still returns a bare int/None (contract [G] unbroken)",
      isinstance(BIG.budget_remaining(), int) and UNK.budget_remaining() is None)
check("goal() keeps budget_remaining AND adds the reason alongside it",
      '"budget_remaining": self.budget_remaining(),' in SRC_AP
      and '"budget_status": self.budget_status(),' in SRC_AP)
check("status() exposes the diagnostic under 'sd_sr_diagnostic'",
      '"sd_sr_diagnostic": self.sd_sr_diagnostic(),' in SRC_AP)
check("the diagnostic is logged at boot, and can never break one",
      "self._log_sd_sr_diagnostic()" in SRC_AP
      and "sd_SR diagnostic unavailable" in SRC_AP)
check("the boot log labels every alternative NOT APPLIED",
      "NOT APPLIED" in SRC_AP)

# the untouched gates
check("MIN_OOS_TRADES / T_MARGIN / KILL_PSR / PROVEN_DSR are untouched",
      (ap.MIN_OOS_TRADES, ap.T_MARGIN, ap.KILL_PSR, ap.PROVEN_DSR) == (20, 2.0, 0.20, 0.95))
check("CARRY_HURDLE_ANN is untouched (not this agent's to lower)",
      abs(ap.CARRY_HURDLE_ANN - (ap.CARRY_RT_4LEG * 4.0 + 0.10)) < 1e-15)
check("the n_eff / clustering path is untouched",
      "rl.n_eff_from_clusters(self.trials_count, n_scored, n_clusters)" in SRC_AP)
check("every trend entrant still carries its hardcoded born_ts",
      all(float(c.get("born_ts") or 0) > ap.AP_CF_EPOCH
          for c in ap.CHALLENGER_CONFIGS if c.get("kind") in ("trend", "switch", "carry")))
check("the graveyard and family tags survive",
      "def graveyard" in SRC_AP and "def family_of" in SRC_AP)


print()
if FAILS:
    print(f"{len(FAILS)} CHECK(S) FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all trend-family + sd_SR-diagnostic checks pass")
