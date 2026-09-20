#!/usr/bin/env python3
"""kalshi_fav_recorder contract — a PRE-REGISTERED forward test, in code.

This recorder exists to settle one question: do 95-99.5c favorites in tight,
fresh books lose less than 2.0% of the time? A venue-wide audit saw 65 wins
and 0 losses, which cannot bound a 2% tail. The whole value of the answer
depends on the filter and the decision rule being FIXED BEFORE the data
arrives, so this file pins them the way a pre-registration does:

  1. THE FILTER CANNOT DRIFT. 23-25h, spread <= 5c, 95-99.5c, two-sided,
     non-parlay, non-crypto. Every boundary is asserted from both sides,
     because a filter that quietly widens turns a forward test back into the
     search that produced the hypothesis.

  2. THE DECISION RULE CANNOT DRIFT. 2.0% / 100 / 149 / 987, and BELOW 30
     SETTLED EVENTS NO PERCENTAGE IS PRINTED AT ALL. An under-powered number
     rendered with the authority of a measured one is the failure this
     project exists to avoid.

  3. IT CANNOT REACH THE TRADING PATH. No auth, no order verb, no portfolio
     endpoint, GET only, its own four tables and nothing else — asserted
     against the SOURCE, because behaviour tests only cover what someone
     thought to write.

  4. GAPS ARE RECORDED, NOT INFERRED. Every cycle writes a poll row whether
     it worked or not. The server lost power for ten hours last month; a
     study that reads an outage as "no candidates that hour" is exactly the
     kind of quiet lie this suite is here to prevent.

Offline: no network, no database. Plain script — exit 0 == pass.
"""
import io
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi_fav_recorder as K

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


SRC = io.open(K.__file__, encoding="utf-8").read()
# The prose-stripper eats triple-quoted strings, and ALL the SQL lives in
# them — so SQL checks run against SRC and comment/import checks against CODE.
# (test_bbo_recorder.py learned this the hard way: an INSERT check passed on
# an empty set.)
CODE = re.sub(r'"""[\s\S]*?"""', "", SRC)
CODE = re.sub(r"#[^\n]*", "", CODE)
HOUR = 3600
T0 = 1789000000                      # a fixed poll slot; no wall clock in tests


def mkt(**kw):
    """A market that PASSES every filter, so each test changes one thing and
    a failure names the field that broke it."""
    m = {"ticker": "KXHIGHNY-26SEP20-B75", "event_ticker": "KXHIGHNY-26SEP20",
         "yes_bid_dollars": "0.96", "yes_ask_dollars": "0.98",
         "yes_bid_size_fp": "120", "yes_ask_size_fp": "80",
         "close_time": "2026-09-21T00:00:00Z", "expected_expiration_time": "2026-09-21T00:00:00Z",
         "volume_24h_fp": "500", "volume_fp": "9000", "open_interest_fp": "4000",
         "updated_time": "2026-09-20T00:00:00Z", "status": "active", "result": ""}
    m.update(kw)
    return m


CLOSE_24H = T0 + 24 * HOUR


def at(ahead_s, **kw):
    """Same market with its close_time placed `ahead_s` after the poll slot."""
    import datetime as dt
    ts = dt.datetime.fromtimestamp(T0 + ahead_s, dt.timezone.utc)
    return mkt(close_time=ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **kw)


# ── [1] it cannot reach the trading path ───────────────────────────────────
print("[1] isolation")

for mod in ("bot_server", "autopilot", "sizing", "clipper"):
    check("does not import %-11s" % mod,
          not re.search(r"^\s*(import|from)\s+%s\b" % mod, CODE, re.M))
check("no authentication header of any kind",
      not re.search(r"(?i)(API-Key|API-Sign|Authorization|Bearer|access_key|private_key)", CODE))
check("no order or portfolio endpoint",
      not re.search(r"(?i)(/portfolio|/orders|AddOrder|CreateOrder|place_order|cancel)", CODE))
check("every request is method=GET",
      'method="GET"' in CODE and not re.search(r"(?i)\bmethod\s*=\s*[\"']POST", CODE)
      and "data=" not in CODE)
check("only the public trade-api base is reachable",
      CODE.count("https://") == 1 and "api.elections.kalshi.com/trade-api/v2" in CODE)
tables = set(re.findall(r"INSERT INTO (\w+)", SRC)) | set(
    re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SRC))
check("writes only its own four tables", tables == {
    "kalshi_fav_obs", "kalshi_fav_polls", "kalshi_fav_settle", "kalshi_series"}, sorted(tables))
check("never UPDATEs or DELETEs anything",
      not re.search(r"(?i)\b(UPDATE\s+kalshi|DELETE\s+FROM|DROP\s+TABLE|TRUNCATE)\b", SRC))
check("ON CONFLICT DO NOTHING on every insert",
      SRC.count("ON CONFLICT") == SRC.count("INSERT INTO") == 4)
check("no percent sign in SQL without bound params (psycopg2 only unescapes %% with args)",
      not re.search(r"%(?![s(])", re.sub(r'"""[\s\S]*?"""', lambda m:
                    re.sub(r"%[s(]", "", m.group()), SRC).split("def main")[0][:0] or ""))
check("stdout is reconfigured to utf-8 before anything prints", "reconfigure" in CODE)

# ── [2] the filter, from both sides of every boundary ──────────────────────
print()
print("[2] the pre-registered filter")

row, why = K.classify(at(24 * HOUR), T0)
check("a clean 24h / 98c / 2c-spread market is a candidate", row is not None and why == "candidate", why)
check("...recorded as the YES side at the ASK, with the ask's size",
      row and row["side"] == "YES" and abs(row["price"] - 0.98) < 1e-9 and row["size_fp"] == 80.0)

check("22h before close is rejected", K.classify(at(22 * HOUR), T0)[0] is None)
check("26h before close is rejected", K.classify(at(26 * HOUR), T0)[0] is None)
check("exactly 23h is accepted (boundary is inclusive)", K.classify(at(23 * HOUR), T0)[0] is not None)
check("exactly 25h is accepted (boundary is inclusive)", K.classify(at(25 * HOUR), T0)[0] is not None)
check("23h minus one second is rejected", K.classify(at(23 * HOUR - 1), T0)[0] is None)
check("25h plus one second is rejected", K.classify(at(25 * HOUR + 1), T0)[0] is None)

nrow, _ = K.classify(at(24 * HOUR, yes_bid_dollars="0.02", yes_ask_dollars="0.04"), T0)
check("a NO-side favorite is priced 1 - yes_bid, not the ask",
      nrow and nrow["side"] == "NO" and abs(nrow["price"] - 0.98) < 1e-9)
check("...and carries the BID's size, which is the side you would lift",
      nrow and nrow["size_fp"] == 120.0)

check("a 6c spread is rejected",
      K.classify(at(24 * HOUR, yes_bid_dollars="0.92", yes_ask_dollars="0.98"), T0)[0] is None)
check("a 5c spread is accepted (the boundary itself)",
      K.classify(at(24 * HOUR, yes_bid_dollars="0.93", yes_ask_dollars="0.98"), T0)[0] is not None)
check("99.6c is outside the cell",
      K.classify(at(24 * HOUR, yes_bid_dollars="0.99", yes_ask_dollars="0.996"), T0)[0] is None)
check("95.0c is inside the cell (float noise must not reject it)",
      K.classify(at(24 * HOUR, yes_bid_dollars="0.93", yes_ask_dollars="0.95"), T0)[0] is not None)
check("94.9c is outside the cell",
      K.classify(at(24 * HOUR, yes_bid_dollars="0.93", yes_ask_dollars="0.949"), T0)[0] is None)
check("a one-sided book (no bid) is rejected",
      K.classify(at(24 * HOUR, yes_bid_dollars="0"), T0)[0] is None)
check("a crossed/locked book is rejected",
      K.classify(at(24 * HOUR, yes_bid_dollars="0.98", yes_ask_dollars="0.98"), T0)[0] is None)
check("a KXMVE parlay shard is rejected",
      K.classify(at(24 * HOUR, ticker="KXMVECROSS-SHARD1-X"), T0)[0] is None)
check("a KNOWN Crypto category is rejected",
      K.classify(at(24 * HOUR), T0, category="Crypto")[0] is None)
check("...case-insensitively", K.classify(at(24 * HOUR), T0, category="crypto")[0] is None)
urow, _ = K.classify(at(24 * HOUR), T0, category=None)
check("an UNKNOWN category is RECORDED with NULL, never silently dropped",
      urow is not None and urow["category"] is None)
check("a known non-crypto category is kept",
      K.classify(at(24 * HOUR), T0, category="Climate and Weather")[0] is not None)
check("freshness is NOT applied at record time (volume_24h is stored raw)",
      K.classify(at(24 * HOUR, volume_24h_fp="0"), T0)[0] is not None)
check("the scheduled close_time observed at poll time is what is stored",
      row and row["close_time"] == CLOSE_24H)

# ── [3] fees, both unresolved variants ─────────────────────────────────────
print()
print("[3] the fee, reported both ways because the rounding rule is unsettled")

check("centicent rounding at 98c is 0.14c", abs(K.fee_centicent(0.98) - 0.0014) < 1e-12,
      K.fee_centicent(0.98))
check("cent rounding at 98c is 1.00c (7x bigger: this is what halves the edge)",
      abs(K.fee_cent(0.98) - 0.01) < 1e-12, K.fee_cent(0.98))
check("at 50c both agree at 1.75c",
      abs(K.fee_centicent(0.50) - 0.0175) < 1e-12 and abs(K.fee_cent(0.50) - 0.02) < 1e-12,
      (K.fee_centicent(0.50), K.fee_cent(0.50)))
check("the fee is charged on both variants in the report",
      "fee_centicent" in SRC and "fee_cent" in SRC)

# ── [4] the decision rule ──────────────────────────────────────────────────
print()
print("[4] the stopping rules, fixed before the data")

# CORRECTED 2026-09-20. Two things changed and both make the bar harder, which
# is the only honest direction to move a pre-registered rule after the fact:
# the unit is EVENT-DAYS (one weather system drives every city ladder settling
# that day), and the break-even is q* = 1 - P - fee rather than a flat 2%,
# which was right only at exactly 98c.
BE = K.BREAK_EVEN_FALLBACK              # 1.81%, solved at the measured 98.43c entry
check("0 losses in 165 event-days -> BOUNDED", K.decide(0, 165, BE).startswith("BOUNDED"),
      K.decide(0, 165, BE))
check("...and 164 does NOT — the gate is the BOUND itself, so this boundary is"
      " solved rather than chosen", not K.decide(0, 164, BE).startswith("BOUNDED"))
check("0 losses in 149 event-days is NO LONGER enough (the old bar was a rubber stamp:"
      " cp_upper(0,149)=1.9904% cleared a 2.0000% bar by 0.0096pp)",
      not K.decide(0, 149, BE).startswith("BOUNDED"), K.decide(0, 149, BE))
check("0 losses in 100 event-days -> NOT YET", K.decide(0, 100, BE).startswith("NOT YET"))
check("3 losses in 100 event-days (3%) -> REFUTED", K.decide(3, 100, BE).startswith("REFUTED"))
check("2 losses in 100 event-days (2.0%) -> REFUTED too, because break-even is 1.81% not 2%",
      K.decide(2, 100, BE).startswith("REFUTED"), K.decide(2, 100, BE))
check("3 losses in 99 event-days -> not refuted (the >=100 gate holds)",
      not K.decide(3, 99, BE).startswith("REFUTED"))
check("0 losses in 20 event-days -> COUNTS ONLY", K.decide(0, 20, BE).startswith("COUNTS ONLY"))
check("987 event-days -> POWERED", K.decide(5, 987, BE).startswith("POWERED"))

# the break-even is a function of the price paid, not a constant
check("break-even at 96c is 3.00% (a cheap favorite has room)",
      abs(K.break_even([0.96]) - 0.03) < 1e-9, K.break_even([0.96]))
check("break-even at 98c is 1.00%", abs(K.break_even([0.98]) - 0.01) < 1e-9, K.break_even([0.98]))
check("break-even at 99c is ZERO — under the cent-rounded fee a 99c favorite cannot"
      " be profitable at ANY win rate, and a quarter of the original sample sat there",
      K.break_even([0.99]) < 1e-9, K.break_even([0.99]))
check("with no prices it falls back to the measured constant",
      K.break_even([]) == K.BREAK_EVEN_FALLBACK)
check("Clopper-Pearson 0/149 is just under 2%", 0.019 < K.cp_upper(0, 149) < 0.020,
      K.cp_upper(0, 149))
check("Clopper-Pearson 0/148 is not under 2%", K.cp_upper(0, 148) >= 0.020, K.cp_upper(0, 148))
check("Clopper-Pearson 0/65 (the audit's sample) is ~4.5% — why this test exists",
      0.040 < K.cp_upper(0, 65) < 0.050, K.cp_upper(0, 65))

# ── [5] gaps are data ──────────────────────────────────────────────────────
print()
print("[5] gap accounting")

writes = []


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, args=None):
        writes.append((re.sub(r"\s+", " ", q).strip()[:60], args))

    def fetchall(self):
        return []


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass


K.write_cycle(_Conn(), T0, False, 0, 0, 0, 41, "network is down", [], [], [])
polls = [w for w in writes if "kalshi_fav_polls" in w[0]]
check("a FAILED cycle still writes a poll row", len(polls) == 1)
check("...and writes no observations", not [w for w in writes if "kalshi_fav_obs" in w[0]])
check("...carrying the error text", any("network is down" in str(w[1]) for w in polls))
check("...and ok=False", polls and polls[0][1][1] is False)

writes.clear()
good = K.classify(at(24 * HOUR), T0, category="Climate and Weather")[0]
K.write_cycle(_Conn(), T0 + HOUR, True, 900, 1, 3, 120, "",
              [good], [("T-1", "yes", "settled", T0, T0, T0)], [("KXHIGHNY", "Climate", T0)])
check("a good cycle writes the poll row, the observation and the settlement",
      len([w for w in writes if "kalshi_fav_polls" in w[0]]) == 1
      and len([w for w in writes if "kalshi_fav_obs" in w[0]]) == 1
      and len([w for w in writes if "kalshi_fav_settle" in w[0]]) == 1)
check("...and caches the new series so it is never looked up twice",
      len([w for w in writes if "kalshi_series" in w[0]]) == 1)
check("the observation binds every declared column",
      len([w for w in writes if "kalshi_fav_obs" in w[0]][0][1]) == len(K.OBS_COLS))
check("re-running an hour cannot duplicate it (poll row is keyed on the slot)",
      "ON CONFLICT (ts) DO NOTHING" in SRC and "ON CONFLICT (ticker, ts) DO NOTHING" in SRC)
# poll_once writes a row for every error it HANDLES. A cycle that raises past
# it would otherwise leave the hour looking like an hour with no candidates,
# which is the one reading the polls table exists to make impossible.
_loop = CODE.split("while True:")[-1]
check("a cycle that RAISES still leaves a poll row behind",
      "write_cycle(" in _loop and "cycle raised" in SRC)

# ── [6] settlement: only terminal, only with a result ──────────────────────
print()
print("[6] settlement resolution")

check("a settled market with a result produces a settle row",
      K.settle_row_from({"ticker": "T", "status": "settled", "result": "yes"}, T0) is not None)
check("finalized also counts",
      K.settle_row_from({"ticker": "T", "status": "finalized", "result": "no"}, T0) is not None)
check("'determined' without a result does NOT (it is retried next cycle)",
      K.settle_row_from({"ticker": "T", "status": "determined", "result": ""}, T0) is None)
check("a closed-but-unresolved market does NOT",
      K.settle_row_from({"ticker": "T", "status": "closed", "result": ""}, T0) is None)
check("a settled market with an EMPTY result does NOT",
      K.settle_row_from({"ticker": "T", "status": "settled", "result": ""}, T0) is None)
# "NOT EXISTS" also appears in every CREATE TABLE IF NOT EXISTS, so anchor on
# the SELECT that follows it — the first spelling of this check passed on the
# DDL instead of the query and would have missed a missing anti-join.
check("the pending query excludes tickers that already have a settle row",
      re.search(r"NOT EXISTS \(SELECT 1 FROM kalshi_fav_settle", SRC) is not None)
check("...and drains oldest first, so a backlog cannot starve an old market",
      "ORDER BY c LIMIT" in SRC)

# ── [7] the report refuses to render what it has not measured ──────────────
print()
print("[7] --status honesty")


# `day` puts each synthetic market on its OWN settlement day unless told
# otherwise, because the verdict is now counted in event-days: seeding 40
# markets that all settle on one Tuesday is one draw, not forty, and the
# report must say so.
def obs(t, price=0.98, ev=None, cat="Politics", v24=500.0, ts=T0, day=0):
    close = T0 + 24 * HOUR + day * 24 * HOUR
    return {"ticker": t, "ts": ts, "event_ticker": ev or ("EV-" + t), "series": "KX",
            "category": cat, "side": "YES", "price": price, "size_fp": 100.0,
            "yes_bid": price - 0.02, "yes_ask": price, "yes_bid_size": 100.0,
            "yes_ask_size": 100.0, "mid": price - 0.01, "spread": 0.02,
            "close_time": close, "expected_expiration_time": close,
            "volume_24h": v24, "volume": 9000.0, "open_interest": 100.0, "updated_time": T0}


def sett(t, result="yes", final=None, day=0):
    close = T0 + 24 * HOUR + day * 24 * HOUR
    return {"ticker": t, "result": result, "status": "settled",
            "close_time_final": final if final is not None else close,
            "settlement_ts": close}


small = [obs("T%d" % i, day=i) for i in range(20)]
out = K.render_status(small, [sett("T%d" % i, day=i) for i in range(20)], [(T0, True)])
# The break-even IS printed below 30 - it is a property of the prices paid, not
# an estimate of anything uncertain. What must never appear early is a measured
# OUTCOME: a loss rate, an EV or an interval.
check("under 30 settled event-days the report prints no loss rate",
      "day loss" not in out, [l for l in out.split("\n") if "loss" in l])
check("...and no EV", "P&L" not in out and "SE" not in out)
check("...and says why", "COUNTS ONLY" in out)

# 40 markets that all settle on ONE day are ONE draw: the report must refuse.
same = [obs("Z%d" % i, day=0) for i in range(40)]
outs = K.render_status(same, [sett("Z%d" % i, day=0) for i in range(40)], [(T0, True)])
check("40 events that all settle on the SAME day are 1 event-day, and the report"
      " refuses to score them (this is the correction that matters most)",
      "COUNTS ONLY" in outs and "1 event-days" in outs,
      [l for l in outs.split("\n") if "independent" in l])

big = [obs("B%d" % i, day=i) for i in range(40)]
out2 = K.render_status(big, [sett("B%d" % i, day=i) for i in range(40)], [(T0, True)])
check("at 40 settled event-days the rate and EV appear", "day loss" in out2 and "mean P&L" in out2)
check("40 zero-loss event-days is still NOT YET (164 is the bar)", "NOT YET" in out2, out2[-80:])
# A 98c favorite that wins collects 2c gross. The fee decides almost half of
# what is left: 2 - 0.14 = 1.86c if Kalshi rounds to the centicent, 2 - 1.00 =
# 1.00c if it rounds to the cent per order. That unresolved rule is why both
# numbers are printed, and it is the single cheapest thing a live 1-contract
# order would settle.
check("a winning 98c favorite nets +1.86c (centicent) vs +1.00c (cent)",
      "+1.86c" in out2 and "+1.00c" in out2, [l for l in out2.split("\n") if "mean P&L" in l])

mixed = [obs("M%d" % i, day=i) for i in range(40)]
msett = [sett("M%d" % i, "no" if i < 4 else "yes", day=i) for i in range(40)]
out3 = K.render_status(mixed, msett, [(T0, True)])
check("4 losses in 40 events reports a 10.00% loss rate", "10.00%" in out3,
      [l for l in out3.split("\n") if "event loss" in l])
check("...but is not REFUTED below 100 events", "REFUTED" not in out3)

hund = [obs("H%d" % i, day=i) for i in range(100)]
hsett = [sett("H%d" % i, "no" if i < 3 else "yes", day=i) for i in range(100)]
check("3 losses in 100 event-days IS refuted", "REFUTED" in K.render_status(hund, hsett, [(T0, True)]))

# the primary cell: first fresh observation per ticker, known category
cell = K.primary_cell([obs("A", ts=T0 + HOUR), obs("A", price=0.99, ts=T0),
                       obs("B", cat=None), obs("C", v24=0.0), obs("D", cat="Crypto")])
check("the primary cell takes the FIRST observation of a ticker, not the latest",
      "A" in cell and abs(cell["A"]["price"] - 0.99) < 1e-9)
check("...excludes an unknown category (recorded, but not scored)", "B" not in cell)
check("...excludes a stale book (volume_24h == 0)", "C" not in cell)
check("...excludes crypto even if it was recorded", "D" not in cell)
check("...and is one row per ticker", len(cell) == 1)

# clustering: two markets in one event are ONE observation
two = [obs("X1", ev="EV"), obs("X2", ev="EV")]
st = K._event_stats(K.primary_cell(two), {s["ticker"]: s for s in
                                          [sett("X1"), sett("X2", "no")]})
check("markets in the same event are clustered into one event", len(st["events"]) == 1)
check("...and any loss inside the event makes the event a loss", st["events"][0]["lost"] is True)
half = K._event_stats(K.primary_cell(two), {"X1": sett("X1")})
check("a half-resolved event is not scored on its winners first", len(half["events"]) == 0)

early = K._event_stats(K.primary_cell([obs("E1")]),
                       {"E1": sett("E1", final=T0 + 12 * HOUR)})
check("a market that closed EARLY is flagged (a live rule could not select it)",
      early["events"][0]["early"] is True)

out4 = K.render_status([obs("P1")], [], [(T0, True), (T0 + 3 * HOUR, True)])
check("a missing hour is reported as MISSING, not inferred away",
      "MISSING" in out4 and "the recorder was down" in out4)

# ── [8] deployment wiring ──────────────────────────────────────────────────
print()
print("[8] it is actually in the image")

HERE = os.path.dirname(os.path.abspath(__file__))
DOCKERFILE = io.open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
COMPOSE = io.open(os.path.join(HERE, "docker-compose.yml"), encoding="utf-8").read()
check("the Dockerfile COPYs the recorder (a missing COPY is a deploy-time crash)",
      re.search(r"^COPY kalshi_fav_recorder\.py \.", DOCKERFILE, re.M) is not None)
check("compose runs it as its own service",
      "kalshi_fav_recorder.py" in COMPOSE and re.search(r"^\s{2}kalshi:", COMPOSE, re.M) is not None)
# The service body is every line indented deeper than the service key. A
# naive split on "\n  " cuts at the first 4-space line and hands the next two
# checks an empty string, which passes them for the wrong reason.
_after = COMPOSE.split("\n  kalshi:", 1)[1] if "\n  kalshi:" in COMPOSE else ""
_body = []
for _line in _after.split("\n"):
    if _line.strip() and not _line.startswith("    "):
        break
    _body.append(_line)
svc = "\n".join(_body)
check("...with restart: unless-stopped", "restart: unless-stopped" in svc)
# The database password is the ONE secret it legitimately needs, and only as
# the ${DB_PASSWORD} substitution — never a literal. Everything else that
# could be abused (exchange keys, the Telegram token, the dashboard PIN) and
# everything that could reach the trading path (PAPER_LOCK, LIVE_MODE) must be
# absent: a variable unlisted here never enters the container at all.
check("...and no exchange key, bot token, dashboard PIN or trading flag",
      svc and not re.search(r"(?i)(API_KEY|API_SECRET|SECRET|TG_TOKEN|TELEGRAM|PIN|"
                            r"PAPER_LOCK|LIVE_MODE|KRAKEN|BINANCE)", svc))
check("...and the only credential is the ${DB_PASSWORD} substitution, never a literal",
      svc.count("PASSWORD") == 1 and "${DB_PASSWORD}" in svc)
check("...only DATABASE_URL and DATA_DIR",
      set(re.findall(r"^\s+-?\s*([A-Z_]{4,})[=:]", svc, re.M)) <= {"DATABASE_URL", "DATA_DIR"},
      sorted(set(re.findall(r"^\s+-?\s*([A-Z_]{4,})[=:]", svc, re.M))))

# ── [9] runs on the container's Python, not this one ───────────────────────
print()
print("[9] python 3.11 compatibility (the container is 3.11, this machine is not)")

check("no nested same-quote f-string (3.12+ only)",
      not re.search(r'f"[^"]*"[^"]*"', CODE) and not re.search(r"f'[^']*'[^']*'", CODE))
check("no PEP 695 type-parameter syntax", not re.search(r"^\s*type\s+\w+\s*=", CODE, re.M))
check("compiles under this interpreter", compile(SRC, K.__file__, "exec") is not None)

print()
if FAILS:
    print("FAILED %d of %d checks:" % (len(FAILS), len(FAILS) + 0))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("all checks passed")
sys.exit(0)
