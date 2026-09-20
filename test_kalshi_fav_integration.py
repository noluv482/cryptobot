#!/usr/bin/env python3
"""kalshi_fav_recorder against a REAL Postgres — the half the unit tests can't reach.

test_kalshi_fav_recorder.py proves the filter, the fees and the decision rule
with fake payloads and a fake cursor. None of that executes a single line of
SQL. The DDL, the dynamic column list in the observation INSERT, the
ON CONFLICT clauses, the settlement anti-join and every query behind --status
have never touched a database at the time this file was written, and each of
them is the kind of thing that fails on its first real row:

  - a column named in OBS_COLS but missing from the CREATE TABLE
  - psycopg2 refusing a bare %% in SQL when no params are passed
  - ON CONFLICT naming a constraint that does not exist
  - a second cycle in the same hour silently duplicating observations
  - --status crashing on an empty table, which is exactly the state it is in
    on day one

So this runs the real thing against a real server.

SAFETY. This script CREATES and DROPS tables. It refuses to run unless the
database name says it is a sandbox, and it refuses if the database contains
any table that is not one of the recorder's own four. The live bot's database
is called `cryptobot`; that name is rejected outright. Run it as:

    DATABASE_URL=postgresql://u:p@host:5432/kalshi_sandbox python test_kalshi_fav_integration.py

Add --live to include one real (read-only, unauthenticated) poll of the public
Kalshi API. Without it, no network is touched and the API is faked.

Plain script: exit 0 == pass.
"""
import io
import os
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kalshi_fav_recorder as K

FAILS = []
HOUR = 3600
OWN_TABLES = {"kalshi_fav_obs", "kalshi_fav_polls", "kalshi_fav_settle", "kalshi_series"}


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


# ── the guard ──────────────────────────────────────────────────────────────
URL = os.environ.get("DATABASE_URL", "")
if not URL:
    print("DATABASE_URL is not set. This test needs a THROWAWAY database.")
    sys.exit(2)

dbname = URL.rsplit("/", 1)[-1].split("?")[0]
if "sandbox" not in dbname.lower():
    print("REFUSING to run: the database is %r. This script creates and drops "
          "tables, so it only runs against a name containing 'sandbox'." % dbname)
    sys.exit(2)

import psycopg2

conn = psycopg2.connect(URL)
conn.autocommit = False
with conn.cursor() as cur:
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    existing = {r[0] for r in cur.fetchall()}
conn.rollback()
if existing - OWN_TABLES:
    print("REFUSING to run: %r holds tables that are not the recorder's: %s"
          % (dbname, sorted(existing - OWN_TABLES)))
    sys.exit(2)

print("sandbox database %r — %d pre-existing tables, all the recorder's own"
      % (dbname, len(existing)))
print()


def q(sql, args=None, fetch=True):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall() if fetch else None
    conn.commit()
    return rows


def wipe():
    with conn.cursor() as cur:
        for t in sorted(OWN_TABLES):
            cur.execute("DROP TABLE IF EXISTS %s" % t)     # table names are our own literals
    conn.commit()


wipe()

# ── [1] the DDL actually executes ──────────────────────────────────────────
print("[1] schema")

K.create_tables(conn)
have = {r[0] for r in q("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
check("all four tables are created", OWN_TABLES <= have, sorted(have))
cols = {r[0] for r in q("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='kalshi_fav_obs'")}
missing = set(K.OBS_COLS) - cols
check("every column the INSERT binds exists in the table", not missing, sorted(missing))
check("...and the table has no column the INSERT forgets",
      not (cols - set(K.OBS_COLS)), sorted(cols - set(K.OBS_COLS)))
K.create_tables(conn)
check("create_tables is idempotent (it runs on every boot)", True)

# ── [2] a failed cycle leaves a row, a good cycle leaves three ─────────────
print()
print("[2] writes, for real")

T0 = (int(time.time()) // HOUR) * HOUR - 100 * HOUR      # a slot safely in the past

K.write_cycle(conn, T0, False, 0, 0, 0, 55, "network is down", [], [], [])
r = q("SELECT ok, err, n_scanned FROM kalshi_fav_polls WHERE ts=%s", (T0,))
check("a FAILED cycle writes its poll row", len(r) == 1 and r[0][0] is False, r)
check("...with the error text preserved", r and "network is down" in (r[0][1] or ""))
check("...and no observations", q("SELECT count(*) FROM kalshi_fav_obs")[0][0] == 0)


def fake_market(tick, ahead=24 * HOUR, bid="0.96", ask="0.98", ev=None, slot=None):
    base = slot if slot is not None else T0
    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + ahead))
    return {"ticker": tick, "event_ticker": ev or tick.rsplit("-", 1)[0],
            "yes_bid_dollars": bid, "yes_ask_dollars": ask,
            "yes_bid_size_fp": "120", "yes_ask_size_fp": "80", "close_time": t,
            "expected_expiration_time": t, "volume_24h_fp": "500", "volume_fp": "9000",
            "open_interest_fp": "4000", "updated_time": t, "status": "active", "result": ""}


row1 = K.classify(fake_market("KXTEST-A-T1"), T0, category="Politics")[0]
row2 = K.classify(fake_market("KXTEST-A-T2"), T0, category="Politics")[0]
K.write_cycle(conn, T0 + HOUR, True, 600, 2, 0, 90, "", [row1, row2], [],
              [("KXTEST", "Politics", T0)])
check("a good cycle writes both observations",
      q("SELECT count(*) FROM kalshi_fav_obs")[0][0] == 2)
check("...and caches the series", q("SELECT category FROM kalshi_series "
                                    "WHERE series='KXTEST'")[0][0] == "Politics")
stored = q("SELECT ticker, side, price, size_fp, close_time, category, volume_24h "
           "FROM kalshi_fav_obs WHERE ticker='KXTEST-A-T1'")[0]
check("...with the values the classifier produced, unrounded by the round trip",
      stored[1] == "YES" and abs(float(stored[2]) - 0.98) < 1e-9
      and abs(float(stored[3]) - 80.0) < 1e-9 and stored[5] == "Politics", stored)

K.write_cycle(conn, T0 + HOUR, True, 600, 2, 0, 90, "", [row1, row2], [],
              [("KXTEST", "Politics", T0)])
check("re-running the SAME hour duplicates nothing",
      q("SELECT count(*) FROM kalshi_fav_obs")[0][0] == 2
      and q("SELECT count(*) FROM kalshi_fav_polls")[0][0] == 2)

row1b = dict(row1)
row1b["ts"] = T0 + 2 * HOUR
K.write_cycle(conn, T0 + 2 * HOUR, True, 600, 1, 0, 90, "", [row1b], [], [])
check("the SAME ticker in a LATER hour is a new row (the window is 2h wide)",
      q("SELECT count(*) FROM kalshi_fav_obs WHERE ticker='KXTEST-A-T1'")[0][0] == 2)

# ── [3] the settlement queue ───────────────────────────────────────────────
print()
print("[3] settlement backlog")

now = T0 + 30 * HOUR
pend = K.pending_settlements(conn, now, 50)
check("markets past close with no settlement are queued",
      set(pend) == {"KXTEST-A-T1", "KXTEST-A-T2"}, pend)
check("...each ticker once, however many times it was observed", len(pend) == 2)

K.write_cycle(conn, T0 + 3 * HOUR, True, 0, 0, 2, 10, "",
              [], [("KXTEST-A-T1", "yes", "settled", T0 + 24 * HOUR, T0 + 24 * HOUR, now)], [])
check("a settled ticker leaves the queue",
      K.pending_settlements(conn, now, 50) == ["KXTEST-A-T2"])
check("a market NOT yet an hour past close is not queued",
      K.pending_settlements(conn, T0 + 24 * HOUR + 60, 50) == [])
check("re-writing the same settlement changes nothing",
      (K.write_cycle(conn, T0 + 4 * HOUR, True, 0, 0, 0, 10, "", [],
                     [("KXTEST-A-T1", "no", "settled", T0, T0, now)], []) or
       q("SELECT result FROM kalshi_fav_settle WHERE ticker='KXTEST-A-T1'")[0][0]) == "yes")

# ── [4] --status against a real table, at every sample size ────────────────
print()
print("[4] the report, read back out of Postgres")


def status_text():
    """Exactly what --status prints, via the real queries."""
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        K.status(conn)
    finally:
        sys.stdout = old
    return buf.getvalue()


wipe()
K.create_tables(conn)
out0 = status_text()
check("--status on an EMPTY database does not crash", "polls" in out0)
check("...and prints no percentage", "%" not in out0, [l for l in out0.split("\n") if "%" in l])


def seed(n_events, losers=0, fresh=True, early=0, base=None, prefix="S"):
    """n_events markets, one per event, written through the REAL writer."""
    b = base if base is not None else T0
    obs, setl = [], []
    for i in range(n_events):
        tick = "KX%s-%04d-T" % (prefix, i)
        m = fake_market(tick, ev="KX%s-%04d" % (prefix, i), slot=b)
        r = K.classify(m, b, category="Politics")[0]
        if not fresh:
            r["volume_24h"] = 0.0
        obs.append(r)
        final = b + (12 * HOUR if i < early else 24 * HOUR)
        setl.append((tick, "no" if i < losers else "yes", "settled", final, final, b + 30 * HOUR))
    K.write_cycle(conn, b, True, n_events, n_events, n_events, 10, "", obs, setl, [])


seed(20)
o20 = status_text()
check("20 settled events: counts only, no rate, no EV",
      "%" not in o20 and "COUNTS ONLY" in o20, [l for l in o20.split("\n") if "%" in l])

wipe(); K.create_tables(conn); seed(40)
o40 = status_text()
check("40 zero-loss events: the rate appears", "event loss    0.00%" in o40,
      [l for l in o40.split("\n") if "event loss" in l])
check("...both fee variants are reported",
      "fee_centicent +1.86c" in o40 and "fee_cent +1.00c" in o40,
      [l for l in o40.split("\n") if "P&L" in l])
check("...and the verdict is still NOT YET at 40", "NOT YET" in o40)

wipe(); K.create_tables(conn); seed(149)
o149 = status_text()
check("149 zero-loss events: BOUNDED", "BOUNDED" in o149,
      [l for l in o149.split("\n") if "decision" in l])

wipe(); K.create_tables(conn); seed(100, losers=3)
o100 = status_text()
check("3 losses in 100 events: REFUTED", "REFUTED" in o100,
      [l for l in o100.split("\n") if "decision" in l])
check("...and the loss rate is 3.00%", "3.00%" in o100)

wipe(); K.create_tables(conn); seed(40, fresh=False)
ostale = status_text()
check("40 STALE books are recorded but score nothing (freshness at analysis time)",
      "%" not in ostale and "40 observations" in ostale,
      [l for l in ostale.split("\n") if "candidates" in l or "%" in l])

wipe(); K.create_tables(conn); seed(40, early=40)
oearly = status_text()
check("markets that closed EARLY are split out, not silently pooled",
      "early-closed  40 events" in oearly,
      [l for l in oearly.split("\n") if "early" in l])

# ── [5] it touches nothing else in the database ────────────────────────────
print()
print("[5] blast radius")

after = {r[0] for r in q("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
check("no table outside its own four was created", after <= OWN_TABLES, sorted(after))

# ── [6] one real poll, end to end ──────────────────────────────────────────
if "--live" in sys.argv:
    print()
    print("[6] a real cycle against the public API")
    wipe()
    K.create_tables(conn)
    slot = int(time.time() // HOUR) * HOUR
    ok = K.poll_once(conn, {}, verbose=False)
    pr = q("SELECT ok, n_scanned, n_candidates FROM kalshi_fav_polls WHERE ts=%s", (slot,))
    check("the cycle reports success", ok is True)
    check("it wrote its poll row", len(pr) == 1, pr)
    check("it scanned a non-zero number of markets (zero means a broken request)",
          pr and pr[0][1] > 0, pr)
    n_obs = q("SELECT count(*) FROM kalshi_fav_obs")[0][0]
    check("observations written == candidates counted", pr and n_obs == pr[0][2], (n_obs, pr))
    cats = q("SELECT DISTINCT category FROM kalshi_fav_obs")
    check("no crypto row was recorded",
          not any((c[0] or "").lower() == "crypto" for c in cats), cats)
    bad = q("SELECT count(*) FROM kalshi_fav_obs WHERE price < 0.95 OR price > 0.995 "
            "OR spread > 0.05 OR close_time - ts NOT BETWEEN %s AND %s",
            (23 * HOUR, 25 * HOUR))[0][0]
    check("every stored row satisfies the pre-registered filter", bad == 0, bad)
    print("     (%d markets scanned, %d candidates stored)" % (pr[0][1], n_obs) if pr else "")
    out = status_text()
    check("--status runs on live data", "candidates" in out)
    print("\n".join("     " + l for l in out.strip().split("\n")[:8]))

    # ── [7] resolving a REAL settlement ────────────────────────────────────
    # The only path a first cycle cannot exercise: a market of ours reaching
    # settlement. It is also the one whose failure would be invisible for
    # weeks — observations would pile up and the report would stay at "0
    # settled" while looking perfectly healthy. So: take a market Kalshi has
    # already settled, pretend we recorded it yesterday, and make the real
    # resolver go and find its result.
    print()
    print("[7] a settlement the exchange has really made")
    http = K.Http()
    # mve_filter=exclude, for the same reason the scanner uses it: the census
    # measured 98.77% of settled markets as KXMVE parlay shards, so a plain
    # listing of 200 came back with nothing this test could use.
    d, err, _ = http.get("/markets", {"status": "settled", "limit": 200,
                                      "mve_filter": "exclude"})
    picked = None
    for m in ((d or {}).get("markets") or []):
        tk = str(m.get("ticker") or "")
        if tk and not tk.startswith("KXMVE") and str(m.get("result") or "") in ("yes", "no"):
            picked = m
            break
    check("the public listing returns a settled market to test against",
          picked is not None, "" if picked else (err or "none found in 200 rows"))
    if picked:
        tk = picked["ticker"]
        past = int(time.time()) - 26 * HOUR
        fake = fake_market(tk, ev=str(picked.get("event_ticker") or tk), slot=past)
        seeded = K.classify(fake, past, category="Politics")[0]
        K.write_cycle(conn, past, True, 1, 1, 0, 5, "", [seeded], [], [])
        check("...and our recorder now has it in the settlement queue",
              tk in K.pending_settlements(conn, int(time.time()), 10))
        rows, checked_n, serr = K.resolve_settlements(K.Http(), [tk], int(time.time()))
        check("the resolver fetches it", checked_n == 1, serr)
        check("...and reads a real result off it",
              len(rows) == 1 and rows[0][1] == picked["result"],
              (rows[0][1:3] if rows else None, picked.get("result"), picked.get("status")))
        K.write_cycle(conn, past + HOUR, True, 0, 0, 1, 5, "", [], rows, [])
        check("...which then leaves the queue for good",
              tk not in K.pending_settlements(conn, int(time.time()), 10))
        got = q("SELECT result, status FROM kalshi_fav_settle WHERE ticker=%s", (tk,))
        check("...and is stored with its exchange status",
              got and got[0][1] in K.SETTLED_STATUSES, got)
        print("     (%s settled %r, recorded from the live API)" % (tk[:44], picked["result"]))

wipe()
conn.close()

print()
if FAILS:
    print("FAILED %d checks:" % len(FAILS))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("all integration checks passed")
sys.exit(0)
