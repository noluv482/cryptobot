#!/usr/bin/env python3
"""Tests for the evidence pack's BREADTH section and the vault's Breadth /
Blockers rendering.

Nothing here touches the network, the live database, the bot container or the
real vault: every fixture is a fake cursor or a temp directory. The point of
the suite is that the breadth section is honest under bad data — an empty
database, a partly-populated one, a cursor that returns garbage shapes and one
that raises on every statement all have to produce a document with nulls and a
recorded reason, never a fabricated number and never an exception.

    python test_evidence_breadth.py
"""
import json
import math
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = FAIL = SKIP = 0
FAILURES = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def skip(name, why):
    global SKIP
    SKIP += 1
    print(f"  SKIP  {name}  ({why})")


def section(title):
    print(f"\n== {title} ==")


# --------------------------------------------------------------------------- #
# Fake database — routes by the SQL text so each section gets its own shape
# --------------------------------------------------------------------------- #
HOUR = 3600
DAY = 86400
T0 = 1756886400          # 2025-09-03 08:00 UTC — the real archive's first hour


def _funding_rows(symbols, n_hours, rate_fn):
    rows = []
    for sym in symbols:
        for i in range(n_hours):
            rows.append(("kraken", sym, T0 + i * HOUR, rate_fn(sym, i)))
    return rows


class FakeCur:
    def __init__(self, owner):
        self.o = owner
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.o.executed.append(s)
        if self.o.mode == "boom":
            raise RuntimeError("relation does not exist")
        if self.o.mode == "garbage":
            self._rows = [("only-one",), (), (object(), float("nan"), None, "x")]
            return
        self._rows = self.o.rows_for(s)

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, mode="full", funding=None, candles=None, state=None):
        self.mode = mode
        self.executed = []
        self.funding = funding or []
        self.candles = candles or []          # (pair, interval_m, ts)
        self.state = state
        self.closed = False

    def cursor(self):
        return FakeCur(self)

    def close(self):
        self.closed = True

    def rows_for(self, s):
        if "FROM funding_rates" in s and "venue, symbol, ts, rate" in s:
            return list(self.funding)
        if "FROM candles" in s and "GROUP BY interval_m" in s:
            agg = {}
            for pair, iv, ts in self.candles:
                a = agg.setdefault(iv, {"pairs": set(), "n": 0, "lo": ts, "hi": ts})
                a["pairs"].add(pair)
                a["n"] += 1
                a["lo"] = min(a["lo"], ts)
                a["hi"] = max(a["hi"], ts)
            return [(iv, len(a["pairs"]), a["n"], a["lo"], a["hi"]) for iv, a in agg.items()]
        if "FROM candles" in s and "GROUP BY pair, interval_m" in s:
            agg = {}
            for pair, iv, ts in self.candles:
                if iv not in (60, 1440, 10080):
                    continue
                a = agg.setdefault((pair, iv), {"n": 0, "lo": ts, "hi": ts})
                a["n"] += 1
                a["lo"] = min(a["lo"], ts)
                a["hi"] = max(a["hi"], ts)
            return [(p, iv, a["n"], a["lo"], a["hi"]) for (p, iv), a in agg.items()]
        if "FROM bot_state" in s:
            return [(self.state,)] if self.state is not None else []
        return []


# --------------------------------------------------------------------------- #
try:
    import research_evidence as rev
except Exception as e:                                    # noqa: BLE001
    print(f"FATAL: cannot import research_evidence: {type(e).__name__}: {e}")
    sys.exit(1)

# keep the state fallback hermetic: the pack reads autopilot_state.json from
# DATA_DIR when bot_state is unreadable, and a real file there would smuggle
# live numbers into these fixtures.
_TMP_DATA = tempfile.mkdtemp(prefix="evidence_breadth_")
rev.DATA_DIR = _TMP_DATA


SCORES_REAL_SHAPE = {
    # zero-decision carry entrant that records its own hurdle
    "carry_harvest": {"id": "carry_harvest", "family": "carry", "via": "cf_carry",
                      "trades_n": 0, "hurdle_ann": 0.184, "worst_neg_funding_days": 0},
    # zero-decision trend entrants that record a warm-up status
    "tsmom_btc_20w": {"id": "tsmom_btc_20w", "family": "trend", "via": "cf_trend",
                      "trades_n": 0,
                      "status": "waiting on price history (warm-up 20 weeks not met)"},
    "donchian_btc": {"id": "donchian_btc", "family": "trend", "via": "cf_trend",
                     "trades_n": 0,
                     "status": "waiting on price history (warm-up 50 days not met)"},
    # a live entrant with no status and no signal
    "reversion": {"id": "reversion", "family": "reversion_pattern", "via": "live",
                  "trades_n": 0},
    # a family that HAS decisions must never appear as a blocker
    "base": {"id": "base", "family": "regime_gate", "via": "live", "trades_n": 92},
}


def full_conn(**kw):
    """A conn whose funding never clears 0.184 and whose hourly candles are far
    short of a 20-week warm-up — i.e. today's measured situation in shape."""
    fund = _funding_rows(("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"), 24 * 40,
                         lambda sym, i: 3e-6 + (1e-6 if sym == "PF_SOLUSD" else 0)
                         + 5e-7 * math.sin(i / 7.0))
    candles = []
    for pair in ("XBTUSD", "ETHUSD"):
        for i in range(24 * 44):                       # 44 days of hourly
            candles.append((pair, 60, T0 + i * HOUR))
        for i in range(32):                            # 32 weekly bars, never read
            candles.append((pair, 10080, T0 + i * 7 * DAY))
    kw.setdefault("state", json.dumps({"scores": SCORES_REAL_SHAPE, "trials_count": 24}))
    return FakeConn("full", funding=fund, candles=candles, **kw)


# --------------------------------------------------------------------------- #
section("breadth never raises, whatever the cursor does")
MODES = {
    "empty": FakeConn("empty"),
    "boom": FakeConn("boom"),
    "garbage": FakeConn("garbage"),
    "partial": FakeConn("full",
                        funding=_funding_rows(("PF_XBTUSD",), 24 * 40, lambda s, i: 4e-6),
                        candles=[], state=None),
    "full": full_conn(),
}
docs = {}
for mode, conn in MODES.items():
    raised = None
    try:
        docs[mode] = rev.sec_breadth(conn)
    except Exception as e:                                 # noqa: BLE001
        raised = f"{type(e).__name__}: {e}"
        docs[mode] = None
    check(f"[{mode}] sec_breadth returns instead of raising", raised is None, raised)
    if docs[mode] is None:
        continue
    check(f"[{mode}] the section is JSON-serialisable",
          isinstance(json.dumps(docs[mode], default=rev._json_default), str))
    check(f"[{mode}] it carries its own errors list",
          isinstance(docs[mode].get("errors"), list))
    check(f"[{mode}] every statement it executed was a SELECT",
          all(s.strip().upper().startswith("SELECT") for s in conn.executed),
          [s[:50] for s in conn.executed if not s.strip().upper().startswith("SELECT")])

check("an empty database reports ZERO symbols archived, not a guess",
      docs["empty"]["funding"]["symbols_archived"] == 0,
      docs["empty"]["funding"])
check("an empty database leaves correlation null WITH a stated reason",
      docs["empty"]["funding_correlation"]["mean_offdiag"] is None
      and docs["empty"]["funding_correlation"]["n_eff"] is None
      and bool(docs["empty"]["funding_correlation"]["reason"]),
      docs["empty"]["funding_correlation"])
check("with unreadable scores the blockers list is null, NOT an empty list",
      docs["empty"]["blockers"] is None and docs["empty"]["blockers_measured"] is False,
      docs["empty"]["blockers"])
check("a cursor that raises on every statement records a reason per sub-step",
      len(docs["boom"]["errors"]) >= 2
      and any("RuntimeError" in e for e in docs["boom"]["errors"]),
      docs["boom"]["errors"])
check("a boom cursor still yields the contracted keys",
      all(k in docs["boom"] for k in ("funding", "candles", "blockers", "hurdle_check")),
      sorted(docs["boom"]))
# the anti-fooling rule: a FAILED read is null, never a measured-looking zero
check("a failed funding read reports symbols_archived null, not 0",
      docs["boom"]["funding"]["symbols_archived"] is None,
      docs["boom"]["funding"]["symbols_archived"])
check("a failed coverage read reports null coverage and measured=False",
      docs["boom"]["candles"]["by_interval"] is None
      and docs["boom"]["candles"]["measured"] is False, docs["boom"]["candles"])
check("a SUCCESSFUL read of an empty table does report a measured 0",
      docs["empty"]["funding"]["symbols_archived"] == 0
      and docs["empty"]["candles"]["measured"] is True,
      (docs["empty"]["funding"], docs["empty"]["candles"].get("measured")))
check("garbage rows are skipped, never coerced into a number",
      docs["garbage"]["funding"]["symbols_archived"] in (0, 1)
      and all(v is None or isinstance(v, (int, float))
              for v in (docs["garbage"]["funding_correlation"] or {}).get("pairs", {}).values()),
      docs["garbage"]["funding"])

section("partial data — one symbol cannot produce a correlation")
p = docs["partial"]
check("one archived symbol is counted", p["funding"]["symbols_archived"] == 1,
      p["funding"]["symbols_archived"])
check("with one symbol n_eff is null and the reason says why",
      p["funding_correlation"]["n_eff"] is None
      and "fewer than 2 symbols" in (p["funding_correlation"]["reason"] or ""),
      p["funding_correlation"])
check("no candles -> empty coverage, not a fabricated span",
      p["candles"]["by_interval"] == {} and p["candles"]["by_pair"] == {},
      p["candles"])

section("full data — the numbers are measured, and n rides along")
f = docs["full"]
per = f["funding"]["per_symbol"]
check("every archived symbol reports n_hours and earliest/latest",
      len(per) == 3 and all(v["n_hours"] > 0 and v["earliest_iso"] and v["latest_iso"]
                            for v in per.values()), sorted(per))
check("trailing-7d stats carry their own n_days and window floor",
      all(v["trailing7d_ann"]["n_days"] > 0
          and v["trailing7d_ann"]["min_window_hours"] == 120 for v in per.values()),
      {k: v["trailing7d_ann"]["n_days"] for k, v in per.items()})
fc = f["funding_correlation"]
check("correlation is computed over ALIGNED hours only, and n is reported",
      fc["n_symbols"] == 3 and fc["n_aligned_hours"] == 24 * 40
      and fc["mean_offdiag"] is not None, fc.get("n_aligned_hours"))
check("n_eff = k / (1 + (k-1)*rbar) to within 1e-9",
      abs(fc["n_eff"] - 3 / (1 + 2 * fc["mean_offdiag"])) < 1e-9,
      (fc["n_eff"], fc["mean_offdiag"]))
check("n_eff can never exceed the number of symbols measured",
      fc["n_eff"] <= fc["n_symbols"] + 1e-9, fc["n_eff"])
cov = f["candles"]["by_interval"]
check("candle coverage is reported per interval_m with pairs AND rows",
      cov["60"]["pairs"] == 2 and cov["60"]["rows"] == 2 * 24 * 44
      and cov["10080"]["pairs"] == 2, cov)
check("per-pair coverage is reported for 60 / 1440 / 10080",
      f["candles"]["intervals_reported_per_pair"] == ["60", "1440", "10080"],
      f["candles"]["intervals_reported_per_pair"])

section("blockers — a family with zero decisions, and the DETECTED cause")
by_fam = {b["family"]: b for b in f["blockers"]}
check("a family that HAS decisions is not listed as blocked",
      "regime_gate" not in by_fam, sorted(by_fam))
check("every idle family in the scores is listed",
      set(by_fam) == {"carry", "trend", "reversion_pattern"}, sorted(by_fam))
check("carry's cause is its OWN recorded hurdle sitting above realized funding",
      by_fam["carry"]["cause_code"] == "hurdle_above_realized_funding"
      and by_fam["carry"]["evidence"]["hurdle_ann"] == 0.184,
      by_fam["carry"])
check("the carry blocker cites the per-symbol max it compared against",
      all("max_trailing7d_ann" in v
          for v in by_fam["carry"]["evidence"]["symbols"].values()),
      by_fam["carry"]["evidence"]["symbols"])
check("the carry unblock line asks for BETTER EVIDENCE, never a lower hurdle",
      "not to be lowered" in (by_fam["carry"]["would_unblock"] or ""),
      by_fam["carry"]["would_unblock"])
check("trend's cause is the warm-up the scorer itself reported",
      by_fam["trend"]["cause_code"] == "price_history_warm_up_not_met"
      and "20 weeks" in (by_fam["trend"]["reported_status"] or ""),
      by_fam["trend"])
check("the trend blocker cites hourly span AND the coarser bars already stored",
      by_fam["trend"]["evidence"]["hourly_60_max_span_days"] is not None
      and by_fam["trend"]["evidence"]["weekly_10080_pairs"] == 2,
      by_fam["trend"]["evidence"])
check("a live entrant with no status is named as never-fired, not as unknown",
      by_fam["reversion_pattern"]["cause_code"] == "live_entrant_never_fired",
      by_fam["reversion_pattern"])
check("every blocker row carries decisions=0 and its entrant ids",
      all(b["decisions"] == 0 and b["entrants"] for b in f["blockers"]),
      f["blockers"])

section("idle entrants are never hidden behind a sibling that DID run")
# the real shape this catches: a family whose only decisions came from ONE
# member while every counterfactual member has never produced a decision.
hidden = full_conn()
hidden.state = json.dumps({"scores": dict(
    SCORES_REAL_SHAPE,
    high_conviction={"id": "high_conviction", "family": "trend", "via": "live",
                     "trades_n": 13})})
d_hidden = rev.sec_breadth(hidden)
check("the family is no longer 'blocked' once a member has decisions",
      "trend" not in {b["family"] for b in d_hidden["blockers"]},
      [b["family"] for b in d_hidden["blockers"]])
idle_ids = {r["id"] for r in d_hidden["idle_entrants"]}
check("...but its zero-decision members are still each named",
      {"tsmom_btc_20w", "donchian_btc"} <= idle_ids, sorted(idle_ids))
check("an entrant WITH decisions is never listed as idle",
      "high_conviction" not in idle_ids and "base" not in idle_ids, sorted(idle_ids))
check("every idle row carries its own family, cause code and decisions=0",
      all(r["family"] and r["cause_code"] and r["decisions"] == 0
          for r in d_hidden["idle_entrants"]), d_hidden["idle_entrants"][:2])

section("hurdle check counts DAYS above the hurdle, not just the max")
hc = f["hurdle_check"]["0.184"]
check("each symbol reports days_above_hurdle with its n_days",
      all(v["days_above_hurdle"] is not None and v["n_days"] > 0
          for v in hc["symbols"].values()), hc["symbols"])
check("share_days_above_hurdle equals days_above / n_days",
      all(abs(v["share_days_above_hurdle"] - v["days_above_hurdle"] / v["n_days"]) < 1e-12
          for v in hc["symbols"].values()), hc["symbols"])
# a symbol that clears on a couple of days but whose MEDIAN is far below
rare = full_conn()
rare.funding = _funding_rows(
    ("PF_XBTUSD",), 24 * 40,
    # one sustained spike, long enough that a couple of trailing-7d windows
    # sit entirely inside it, in an otherwise low-funding series
    lambda sym, i: (3e-4 if 300 <= i < 520 else 2e-6))
rare.state = json.dumps({"scores": {"carry_harvest": SCORES_REAL_SHAPE["carry_harvest"]}})
d_rare = rev.sec_breadth(rare)
b_rare = d_rare["blockers"][0]
check("a hurdle cleared on a handful of days reads 'rarely cleared', not 'never'",
      b_rare["cause_code"] == "hurdle_rarely_cleared", b_rare["cause_code"])
check("the rarely-cleared cause quotes the day count it measured",
      "day(s) above it" in (b_rare["cause"] or ""), b_rare["cause"])
check("its unblock line still refuses to lower the hurdle",
      "not to be lowered" in (b_rare["would_unblock"] or ""), b_rare["would_unblock"])
check("the comparison states that the entrant's own symbol is not recorded",
      "record which symbol" in (b_rare["evidence"].get("note") or ""),
      b_rare["evidence"].get("note"))

section("a hurdle that IS cleared is never reported as 'above realized'")
easy = full_conn()
easy.state = json.dumps({"scores": dict(
    SCORES_REAL_SHAPE, carry_harvest=dict(SCORES_REAL_SHAPE["carry_harvest"],
                                          hurdle_ann=1e-9))})
d_easy = rev.sec_breadth(easy)
b_easy = {b["family"]: b for b in d_easy["blockers"]}
check("a cleared hurdle yields the 'cause not determined' code, not a fake cause",
      b_easy["carry"]["cause_code"] == "hurdle_cleared_but_no_decision_recorded",
      b_easy["carry"]["cause_code"])

section("an unknown cause stays 'unknown' — never invented")
mystery = full_conn()
mystery.state = json.dumps({"scores": {
    "mystery": {"id": "mystery", "family": "cross_section", "trades_n": 0}}})
d_m = rev.sec_breadth(mystery)
check("no status, no via, no hurdle -> cause_code 'unknown' and cause null",
      d_m["blockers"][0]["cause_code"] == "unknown"
      and d_m["blockers"][0]["cause"] is None, d_m["blockers"])

section("build_pack still ships ONE document with breadth in it")
saved_urlopen = rev.urllib.request.urlopen


def _no_net(url, timeout=None):
    raise OSError("connection refused")


try:
    rev.urllib.request.urlopen = _no_net
    for mode in ("empty", "boom", "garbage"):
        conn = FakeConn(mode)
        raised = None
        try:
            doc = rev.build_pack(conn)
        except Exception as e:                             # noqa: BLE001
            raised, doc = f"{type(e).__name__}: {e}", None
        check(f"build_pack({mode}) never raises with breadth wired in", raised is None, raised)
        if doc is not None:
            check(f"build_pack({mode}) carries a breadth key", "breadth" in doc, sorted(doc))
    doc_full = rev.build_pack(full_conn())
    check("breadth rides in the same single JSON document",
          isinstance(json.dumps(doc_full, default=rev._json_default), str)
          and doc_full["breadth"]["funding"]["symbols_archived"] == 3,
          doc_full.get("breadth", {}).get("funding"))
finally:
    rev.urllib.request.urlopen = saved_urlopen

section("read-only: not one statement outside SELECT, over every mode")
all_exec = []
for conn in list(MODES.values()) + [easy, mystery]:
    all_exec += conn.executed
check("every statement the breadth path issued was a SELECT",
      all_exec and all(s.strip().upper().startswith("SELECT") for s in all_exec),
      [s[:60] for s in all_exec if not s.strip().upper().startswith("SELECT")])
check("nothing in the breadth code writes a table",
      not any(w in open(os.path.join(HERE, "research_evidence.py"), encoding="utf-8").read().upper()
              for w in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " ALTER ")),
      "found a write keyword in research_evidence.py")


# --------------------------------------------------------------------------- #
section("vault: Breadth + Blockers rendering")
GEN = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                   "OneDrive", "noluv", "Claude Daily")
sys.path.insert(0, GEN)
try:
    import _generate_bot_lab as lab
except Exception as e:                                     # noqa: BLE001
    lab = None
    skip("vault Breadth/Blockers sections", f"generator not importable: {type(e).__name__}: {e}")

if lab is not None:
    goal = {"data": None, "ts": None, "error": "no goal block cached"}

    def _index(ev):
        return lab.build_index([], goal, now=T0, evidence=ev)

    txt_none = _index(None)
    check("with no evidence pack the Breadth section still exists",
          "## Breadth" in txt_none, txt_none[:0])
    check("with no evidence pack Breadth says unknown and names the reason",
          "unknown" in txt_none.split("## Breadth", 1)[1].split("##", 1)[0],
          txt_none.split("## Breadth", 1)[1][:200])
    check("with no evidence pack Blockers says unknown too",
          "## Blockers" in txt_none
          and "unknown" in txt_none.split("## Blockers", 1)[1].split("##", 1)[0],
          txt_none.split("## Blockers", 1)[1][:200] if "## Blockers" in txt_none else "missing")

    pack = {"generated": T0, "breadth": docs["full"], "errors": []}
    txt = _index(pack)
    breadth_part = txt.split("## Breadth", 1)[1].split("\n## ", 1)[0]
    block_part = txt.split("## Blockers", 1)[1].split("\n## ", 1)[0]
    check("the vault reports the number of symbols ARCHIVED",
          "3" in breadth_part and "archived" in breadth_part.lower(), breadth_part[:300])
    check("every archived symbol appears with its n (hours)",
          all(s in breadth_part for s in ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"))
          and str(24 * 40) in breadth_part, breadth_part[:400])
    check("the frozen-universe note is stated, not implied",
          "frozen" in breadth_part.lower(), breadth_part[:400])
    check("scored-vs-archived is distinguished",
          "scored" in breadth_part.lower(), breadth_part[:400])
    check("n_eff and the mean off-diagonal correlation are both shown with n",
          "n_eff" in breadth_part and "aligned" in breadth_part.lower(), breadth_part[:600])
    check("candle coverage names interval_m and the pair counts",
          "interval_m" in breadth_part and "10080" in breadth_part, breadth_part[:800])
    check("the blockers table lists each idle family",
          all(fam in block_part for fam in ("carry", "trend", "reversion_pattern")),
          block_part[:400])
    check("each blocker row shows decisions = 0",
          block_part.count("| 0 |") >= 3, block_part[:400])
    check("each blocker row names its detected cause code",
          "hurdle_above_realized_funding" in block_part
          and "price_history_warm_up_not_met" in block_part, block_part[:600])
    check("each blocker row says what would unblock it",
          "unblock" in block_part.lower(), block_part[:200])
    check("the Idle entrants table lists every zero-decision entrant by id",
          all(f"`{e}`" in block_part for e in ("carry_harvest", "tsmom_btc_20w",
                                               "donchian_btc", "reversion")),
          block_part[-800:])
    txt_hidden = _index({"breadth": d_hidden})
    bp_hidden = txt_hidden.split("## Blockers", 1)[1].split("\n## ", 1)[0]
    fam_table, idle_table = bp_hidden.split("**Idle entrants**", 1)
    check("the family that HAS decisions is absent from the family table",
          "trend" not in fam_table, fam_table[-400:])
    check("...yet its idle members are still listed under Idle entrants",
          "`tsmom_btc_20w`" in idle_table and "`donchian_btc`" in idle_table,
          idle_table[:600])
    txt_noidle = _index({"breadth": dict(d_hidden, idle_entrants=[])})
    check("no idle entrant renders as 'none', not as a blank table",
          "none — every registered entrant" in txt_noidle, "missing")
    txt_unk = _index({"breadth": dict(d_hidden, idle_entrants=None)})
    check("an unreadable idle list renders 'unknown', never 'none'",
          "unknown — the entrant scores could not be read" in txt_unk, "missing")
    check("the vault never suggests lowering the hurdle",
          "lower the hurdle" not in txt.lower().replace("not to be lowered", ""),
          "found a loosening suggestion")

    # a breadth section that is present but EMPTY must not render fake numbers
    txt_empty = _index({"breadth": docs["empty"]})
    bp = txt_empty.split("## Breadth", 1)[1].split("\n## ", 1)[0]
    check("zero archived symbols renders as 0 with an explicit note, not as blank",
          "0" in bp and "unknown" in bp.lower(), bp[:300])
    check("unreadable scores render Blockers as unknown, never as 'none blocked'",
          "unknown" in txt_empty.split("## Blockers", 1)[1].split("\n## ", 1)[0].lower(),
          txt_empty.split("## Blockers", 1)[1][:200])
    txt_noblock = _index({"breadth": dict(docs["full"], blockers=[])})
    check("a measured empty blockers list says 'none' plainly",
          "none" in txt_noblock.split("## Blockers", 1)[1].split("\n## ", 1)[0].lower(),
          txt_noblock.split("## Blockers", 1)[1][:200])
    txt_boom = _index({"breadth": docs["boom"]})
    bpb = txt_boom.split("## Breadth", 1)[1].split("\n## ", 1)[0]
    check("a FAILED breadth read renders 'unknown', never a measured-looking 0",
          "unknown" in bpb.lower()
          and "NOT 'no candles exist'" in bpb, bpb[:500])

    # garbage in the pack must not crash the generator
    for bad in ("not-a-dict", 12, [], {"funding": "nope"}, {"blockers": "nope"}):
        raised = None
        try:
            _index({"breadth": bad})
        except Exception as e:                             # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
        check(f"a garbage breadth pack ({type(bad).__name__}) renders instead of raising",
              raised is None, raised)

    # read_evidence must never raise and must explain its nulls
    with tempfile.TemporaryDirectory() as td:
        got = lab.read_evidence(td)
        check("an empty evidence dir -> data None with a stated reason",
              got["data"] is None and got["error"], got)
        with open(os.path.join(td, "evidence_2026-09-07.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        got = lab.read_evidence(td)
        check("an unreadable evidence file -> data None with a stated reason",
              got["data"] is None and "unreadable" in got["error"].lower(), got)
        with open(os.path.join(td, "evidence_2026-09-08.json"), "w", encoding="utf-8") as fh:
            json.dump({"generated": T0, "breadth": docs["full"]}, fh)
        got = lab.read_evidence(td)
        check("the NEWEST evidence file wins and its ts rides along",
              got["data"] is not None and got["ts"] == T0, got.get("error"))
        check("read_evidence never raises on a missing directory",
              lab.read_evidence(os.path.join(td, "nope"))["data"] is None)


print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
for f_ in FAILURES:
    print(f"  - {f_}")
sys.exit(1 if FAIL else 0)
