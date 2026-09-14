#!/usr/bin/env python3
"""bbo_recorder — one top-of-book sample per pair per minute. Nothing else.

WHY THIS EXISTS, AND WHY IT IS THIS SMALL
-----------------------------------------
The obvious build was a full order-book DEPTH recorder, to hunt for a
directional signal in resting liquidity. That was designed, attacked, and
abandoned, for a reason worth keeping written down:

  * PERFECT foreknowledge does not pay at short horizons. Ranking all 33 pairs
    by their ACTUAL realised forward return and buying the top 20% earns 0.777%
    at 1h and 1.094% at 2h over seven years, against a 1.80% round trip. An
    oracle loses money there. Book imbalance is a 30-minute-to-2-hour signal, so
    no feature found in the book could have cleared the fee even if it were
    perfect. (Measured 2026-09-14; the same computation clears easily at 6h+,
    so this is a statement about SHORT horizons, not about direction.)
  * This account's paper balance is ~$100. A trade of tens of dollars does not
    walk the book past level 1. Depth beyond the top of book is liquidity this
    account will never consume.

So what is left is level 1 — and level 1 is worth recording, for two questions
that have nothing to do with predicting direction and known, large payoffs:

  1. MAKER ENTRIES, worth 0.50% per round trip. bot_server sets
     LIVE_MAKER_ENTRIES_WIRED = False because nobody can show a resting order
     would have filled; entries therefore cross the spread and pay taker
     (1.80% instead of 1.30%). That 0.50% is larger than every directional
     effect this project has ever measured. A per-minute bid/ask series is the
     instrument that settles whether a limit at the signal price would fill.
  2. THE SPREAD GATE IS STARVED. _spread_map_loop already regenerates a
     per-(pair, hour) median spread daily, but it reads from shadow_signals —
     it needs ~100 signals per pair-hour cell, i.e. tens of thousands of
     signals, and the bot takes none. One sample a minute fills every cell in
     about two days.

WHAT IT WILL NOT DO
-------------------
It never imports bot_server, never authenticates, never places or simulates an
order, and writes only its own two tables. Public market data in, two tables
out. A bug here cannot reach the trading path because it has no code path to it.

GAPS ARE DATA
-------------
Every poll writes a row to bbo_polls whether it succeeded or not. A study that
cannot tell "the spread was normal" from "we were not recording" will read an
outage as a measurement — which is the failure this whole project has spent a
month unlearning. The server lost power for ten hours this month; that gap must
be visible in the data, not inferred from its absence.

Usage
    python bbo_recorder.py --once          # one poll, print, exit
    python bbo_recorder.py --create        # create the tables, exit
    python bbo_recorder.py                 # the loop
    python bbo_recorder.py --status        # coverage + gaps, exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

PUBLIC = "https://api.kraken.com/0/public"
INTERVAL = 60                      # one sample per pair per minute
HTTP_TIMEOUT = 10
USER_AGENT = "bbo_recorder/1.0 (personal research)"

# One request carries every pair: ~288 bytes/pair measured, so 34 pairs is
# ~9.5 KB in a single call. At one call per minute that is 0.0167 req/s against
# Kraken's documented ~1 req/s public guidance — about 1/60th of it. The rate
# limit is not a constraint at this cadence, which is most of why the design
# collapsed to something this small.
MAX_CONSECUTIVE_FAIL = 10


def _log(msg: str) -> None:
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ── the pair list, without importing bot_server ─────────────────────────────
def pairs_from_db(conn) -> list:
    """Whatever the bot already tracks. Read from candles rather than imported
    from bot_server: this module must stay unable to touch the trading path,
    and an import is a path."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT pair FROM candles ORDER BY pair")
        return [r[0] for r in cur.fetchall()]


# ── Kraken's response keys are NOT the names you asked for ──────────────────
def altname_map(pairs: list) -> dict:
    """{response_key: requested_name}.

    Asking for XBTUSD returns a key of XXBTZUSD, ETHUSD returns XETHZUSD, while
    SOLUSD returns SOLUSD (verified against the live endpoint). Storing the
    RESPONSE key would produce a table that never joins to candles, which keys
    on the requested string — a silent, total loss of usefulness discovered
    weeks later. Built once at boot from AssetPairs, which carries altname.
    """
    url = "%s/AssetPairs?%s" % (PUBLIC, urllib.parse.urlencode({"pair": ",".join(pairs)}))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        d = json.loads(r.read().decode())
    if d.get("error"):
        raise RuntimeError("AssetPairs: %s" % d["error"])
    out = {}
    for key, info in (d.get("result") or {}).items():
        alt = str(info.get("altname") or "")
        if alt in pairs:
            out[key] = alt
        else:
            out[key] = alt or key
    return out


def fetch_bbo(pairs: list) -> tuple:
    """(rows, err). rows = [(pair, bid, ask, bid_sz, ask_sz)] using REQUESTED names.

    Prices are b[0]/a[0]. Sizes are b[2]/a[2] — the lot-volume field. Sizes are
    stored but nothing is built on them: they come back whole-lot quantised
    (a bid size of '3.000' on BTC), so they describe the tick's granularity more
    than real resting size. Price is the load-bearing field here.
    """
    url = "%s/Ticker?%s" % (PUBLIC, urllib.parse.urlencode({"pair": ",".join(pairs)}))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            body = r.read()
        d = json.loads(body.decode())
    except Exception as e:                                        # noqa: BLE001
        return [], "%s: %s" % (type(e).__name__, str(e)[:120])
    if d.get("error"):
        return [], "kraken: %s" % (d["error"],)

    amap = fetch_bbo._amap or {}
    rows, unknown = [], []
    for key, v in (d.get("result") or {}).items():
        name = amap.get(key)
        if not name:
            # NEVER silently drop. A response key with no mapping means the
            # pair list and the map have diverged, and a quietly missing pair
            # is exactly the invisible failure this file exists to avoid.
            unknown.append(key)
            continue
        try:
            rows.append((name, float(v["b"][0]), float(v["a"][0]),
                         float(v["b"][2]), float(v["a"][2])))
        except (KeyError, IndexError, TypeError, ValueError):
            unknown.append(key)
    if unknown:
        return rows, "unmapped response keys: %s" % ",".join(sorted(unknown)[:6])
    return rows, ""


fetch_bbo._amap = {}


# ── schema ──────────────────────────────────────────────────────────────────
DDL = [
    """CREATE TABLE IF NOT EXISTS bbo_1m (
           pair      TEXT             NOT NULL,
           ts        BIGINT           NOT NULL,   -- minute slot, epoch seconds
           bid       DOUBLE PRECISION NOT NULL,
           ask       DOUBLE PRECISION NOT NULL,
           bid_size  DOUBLE PRECISION,
           ask_size  DOUBLE PRECISION,
           PRIMARY KEY (pair, ts)
       )""",
    "CREATE INDEX IF NOT EXISTS bbo_1m_ts ON bbo_1m(ts)",
    # Gap accounting. One row per poll ATTEMPT, so an outage is a fact in the
    # data rather than an absence a reader has to notice.
    """CREATE TABLE IF NOT EXISTS bbo_polls (
           ts        BIGINT PRIMARY KEY,
           ok        BOOLEAN NOT NULL,
           n_pairs   INTEGER NOT NULL,
           latency_ms INTEGER,
           err       TEXT
       )""",
]


def create_tables(conn) -> None:
    with conn.cursor() as cur:
        for stmt in DDL:
            cur.execute(stmt)
    conn.commit()
    _log("tables ready: bbo_1m, bbo_polls")


def write(conn, slot: int, rows: list, ok: bool, ms: int, err: str) -> None:
    """One transaction: the poll record ALWAYS, the samples when there are any."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO bbo_polls (ts, ok, n_pairs, latency_ms, err)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (ts) DO NOTHING""",
                    (slot, bool(ok), len(rows), int(ms), (err or "")[:300]))
        for name, bid, ask, bsz, asz in rows:
            cur.execute("""INSERT INTO bbo_1m (pair, ts, bid, ask, bid_size, ask_size)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (pair, ts) DO NOTHING""",
                        (name, slot, bid, ask, bsz, asz))
    conn.commit()


def status(conn) -> None:
    """Coverage and gaps — the two questions a reader must be able to answer
    before trusting anything computed from this table."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT pair), min(ts), max(ts) FROM bbo_1m")
        n, npairs, lo, hi = cur.fetchone()
        if not n:
            print("  bbo_1m is empty")
            return
        span_h = (hi - lo) / 3600.0
        print("  samples      %d across %d pairs" % (n, npairs))
        print("  span         %s -> %s  (%.1f h)"
              % (time.strftime("%Y-%m-%d %H:%M", time.localtime(lo)),
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(hi)), span_h))
        cur.execute("SELECT count(*), count(*) FILTER (WHERE NOT ok) FROM bbo_polls")
        polls, bad = cur.fetchone()
        expected = int(span_h * 60) + 1
        print("  polls        %d recorded, %d failed  (expected ~%d)"
              % (polls, bad, expected))
        missing = max(0, expected - polls)
        print("  MISSING      %d minutes with no poll row at all%s"
              % (missing, "  <- the recorder was down" if missing else ""))
        # the spread-gate cells this is meant to fill
        cur.execute("""SELECT count(*) FROM (
                           SELECT pair, (ts/3600)%%24 AS hr
                           FROM bbo_1m GROUP BY pair, hr HAVING count(*) >= 100
                       ) q""")
        print("  gate cells   %d (pair,hour) cells at n>=100 — the spread map's bar"
              % cur.fetchone()[0])


def poll_once(conn, pairs: list, verbose: bool = False) -> bool:
    slot = int(time.time() // INTERVAL) * INTERVAL
    t0 = time.time()
    rows, err = fetch_bbo(pairs)
    ms = int((time.time() - t0) * 1000)
    ok = bool(rows) and not err
    if conn is not None:
        write(conn, slot, rows, ok, ms, err)
    if verbose:
        for name, bid, ask, bsz, asz in sorted(rows)[:6]:
            print("   %-10s bid %-12.6g ask %-12.6g spread %.4f%%"
                  % (name, bid, ask, 100.0 * (ask - bid) / max(bid, 1e-9)))
        print("   %d pairs, %d ms%s" % (len(rows), ms, ("  ERR " + err) if err else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="one poll, print it, exit")
    ap.add_argument("--create", action="store_true", help="create tables and exit")
    ap.add_argument("--status", action="store_true", help="coverage and gaps, exit")
    ap.add_argument("--pairs", default="", help="comma list (default: every pair in candles)")
    ap.add_argument("--dry-run", action="store_true", help="never touch the database")
    a = ap.parse_args()

    conn = None
    if not a.dry_run:
        url = os.environ.get("DATABASE_URL", "")
        if not url:
            print("DATABASE_URL is not set — use --dry-run to poll without storing")
            return 2
        import psycopg2
        conn = psycopg2.connect(url)

    if a.create:
        create_tables(conn)
        return 0
    if a.status:
        status(conn)
        return 0

    pairs = ([p.strip() for p in a.pairs.split(",") if p.strip()]
             or (pairs_from_db(conn) if conn is not None else ["XBTUSD", "ETHUSD", "SOLUSD"]))
    if not pairs:
        print("no pairs to record")
        return 2
    fetch_bbo._amap = altname_map(pairs)
    _log("recording %d pairs, one sample per %ds" % (len(pairs), INTERVAL))

    if a.once:
        poll_once(conn, pairs, verbose=True)
        return 0

    if conn is not None:
        create_tables(conn)
    fails = 0
    while True:
        # Align to the top of the minute so slots are stable across restarts
        # and a resumed recorder lands on the same grid as the one before it.
        time.sleep(max(0.0, INTERVAL - (time.time() % INTERVAL)))
        try:
            ok = poll_once(conn, pairs)
            fails = 0 if ok else fails + 1
            if not ok:
                _log("poll failed (%d consecutive)" % fails)
            if fails >= MAX_CONSECUTIVE_FAIL:
                # Stop rather than write an unbroken run of failures nobody
                # reads. bbo_polls already records every one of them, so the
                # gap is explained in the data.
                _log("giving up after %d consecutive failures" % fails)
                return 1
        except KeyboardInterrupt:
            _log("stopped")
            return 0
        except Exception as e:                                    # noqa: BLE001
            fails += 1
            _log("loop error (%d): %s" % (fails, str(e)[:140]))
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:                                 # noqa: BLE001
                    pass


if __name__ == "__main__":
    sys.exit(main())
