#!/usr/bin/env python3
"""kalshi_fav_recorder — the pre-registered forward test of one Kalshi cell.

WHY THIS EXISTS, AND WHY IT RECORDS INSTEAD OF TRADING
------------------------------------------------------
A venue-wide audit of Kalshi (September 2026) measured ten classes of trade at
real quotes, net of fees and spread. Nine came back negative, undetermined, or
a census. Exactly one cell survived: NON-CRYPTO FAVORITES priced 95c-99.5c in
TIGHT (spread <= 5c), FRESH (traded in the last 24h) books, observed ~24h
before their SCHEDULED close, went 65/65 for about +2.0c net per contract.

That is not an edge. It is a hypothesis with a hole in it: zero losses in 55
events cannot bound the tail. The whole return is carry on already-decided
paper, and it breaks even at a 2.0% event loss rate — so the only question
that matters is whether the true loss rate is under 2%, and 55 events cannot
answer it (the honest interval runs to -2.4c). So the test was PRE-REGISTERED
before any data existed, and this file is that test. It records candidates
every hour, resolves their settlements, and prints the pre-registered decision
rules. It NEVER places an order and NEVER authenticates — the stopping rules
below decide whether a 1-10 contract live pilot is ever justified, and nothing
in this file can pre-empt them.

THE PROTOCOL (fixed; loosening it is the failure mode this design prevents)
---------------------------------------------------------------------------
Every hour, on the hour, GET the public market list for markets whose
scheduled close_time is 23h-25h after the poll. Candidate = open market with
ALL of: series not KXMVE* (parlays); series category != Crypto; two-sided
book (yes_bid > 0, yes_ask < 1, yes_ask > yes_bid); spread <= 5c; a favorite
side — YES if 0.95 <= yes_ask <= 0.995 (price = yes_ask, size = ask size),
else NO if 0.005 <= yes_bid <= 0.05 (price = 1 - yes_bid, size = bid size).
Freshness (volume_24h > 0) is NOT a recording filter: the raw volume fields are
stored and freshness is applied at analysis time, so the study can be re-cut
without look-ahead. The PRIMARY CELL that --status reports is the first fresh
observation per ticker.

Settlement: once a recorded market's scheduled close is more than an hour in
the past, GET /markets/{ticker} until it is settled/finalized with a result.

Decision (--status), exactly these rules: REFUTED if the event loss rate
exceeds 2.0% with >= 100 settled events; otherwise NOT YET until >= 149
events with a Clopper-Pearson 95% upper bound under 2.0% => BOUNDED; >= 987
events => POWERED (report the CI). Below 30 settled events it prints COUNTS
ONLY — no rates, no EV — because a percentage of 12 events would carry the
authority of a measurement it does not have.

FEES ARE REPORTED TWICE
-----------------------
Kalshi's taker fee is roundup(0.07 * P * (1-P)) per contract. Whether that
rounds up to the centicent (the published schedule) or to the CENT per order
(what two referees believed) is UNRESOLVED and at 98c it is the difference
between +2.0c and +1.0c. Every P&L number is therefore printed in both
variants, labelled fee_centicent and fee_cent, and nothing here picks one.

WHAT IT WILL NOT DO
-------------------
It never imports bot_server, never authenticates, never places or simulates
an order, and writes only its own four tables. Public data in, four tables
out. A bug here cannot reach the trading path because it has no code path
to it.

GAPS ARE DATA
-------------
Every cycle writes a kalshi_fav_polls row whether it succeeded or not. A
missing hour of candidates that looks like "no favorites were on offer" is
the exact failure this project has spent a month unlearning.

Usage
    python kalshi_fav_recorder.py --dry-run      # one cycle, LIVE public API, no database
    python kalshi_fav_recorder.py --once         # one cycle, stored
    python kalshi_fav_recorder.py --create       # create the tables, exit
    python kalshi_fav_recorder.py                # the loop
    python kalshi_fav_recorder.py --status       # the pre-registered analysis, exit
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
INTERVAL = 3600                    # one cycle per hour, on the hour
HTTP_TIMEOUT = 20
USER_AGENT = "kalshi_fav_recorder/1.0 (personal research; read-only)"

# Kalshi's public guidance is about one request per second for unauthenticated
# reads. The recorder never exceeds it: every call waits out MIN_CALL_GAP, a
# 429 backs off 5s * attempt, and a whole hourly cycle is capped at
# MAX_CALLS_PER_CYCLE so a runaway pagination cannot turn into a ban.
MIN_CALL_GAP = 1.0
MAX_CALLS_PER_CYCLE = 300
MAX_SCAN_CALLS = 80                # pages of 1000; the window has never needed 10
MAX_SETTLE_PER_CYCLE = 200
PAGE_LIMIT = 1000
MAX_CONSECUTIVE_FAIL = 10

# The pre-registered cell. Constants, not arguments: the point of a
# pre-registered test is that nobody can tune them after seeing the data.
WINDOW_LO = 23 * 3600
WINDOW_HI = 25 * 3600
PRICE_LO = 0.95
PRICE_HI = 0.995
MAX_SPREAD = 0.05
EPS = 1e-9                         # 0.95 <= "0.9500" must not fail on float noise
FEE_RATE = 0.07
BREAK_EVEN_LOSS = 0.02
N_REFUTE = 100
N_BOUND = 149
N_POWER = 987
N_MIN_REPORT = 30

# Kalshi's terminal states. The listing's status filter says "open" but rows
# come back as "active"; a finished market is "finalized" (sometimes
# "settled"), and "determined" means the result is known but not yet paid.
# Only the terminal states with a non-empty result count as settled.
SETTLED_STATUSES = ("settled", "finalized")


def _log(msg: str) -> None:
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def parse_ts(s) -> int | None:
    """ISO-8601 Z timestamp -> epoch seconds. updated_time carries microseconds
    and close_time does not; both must parse or the window filter silently
    rejects everything."""
    if not s or not isinstance(s, str):
        return None
    try:
        core = s.rstrip("Z").split(".")[0].split("+")[0]
        return calendar.timegm(time.strptime(core, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ── the one HTTP client: GET, public, counted, rate-limited ─────────────────
class Http:
    """Every request the recorder makes goes through here, so the per-cycle
    call budget and the 1 req/s floor are enforced in one place. Returns
    (obj, err, raw_body) and never raises: a dead network is a poll row with
    ok=False, not a crash loop."""

    def __init__(self, budget: int = MAX_CALLS_PER_CYCLE):
        self.budget = budget
        self.calls = 0
        self.urls = []
        self._last = 0.0

    def get(self, path: str, params: dict | None = None) -> tuple:
        if self.calls >= self.budget:
            return None, "call budget exhausted (%d/cycle)" % self.budget, b""
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        self.urls.append(url)
        for attempt in range(1, 4):
            gap = MIN_CALL_GAP - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()
            self.calls += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                    body = r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(5.0 * attempt)
                    continue
                return None, "HTTP %d on %s" % (e.code, path[:60]), b""
            except Exception as e:                                    # noqa: BLE001
                return None, "%s: %s" % (type(e).__name__, str(e)[:120]), b""
            try:
                return json.loads(body.decode("utf-8")), "", body
            except ValueError:
                return None, "non-JSON body from %s" % path[:60], body
        return None, "HTTP 429 three times on %s" % path[:60], b""


# ── the filter, as a pure function so the tests can hit every edge ─────────
def classify(m: dict, poll_ts: int, category=None) -> tuple:
    """(row, reason). row is None when the market is rejected and reason says
    why; otherwise reason is 'candidate'. `category` is the series category
    when known, None when unknown — an unknown category is RECORDED (with
    NULL) so that a missing series lookup can never silently shrink the
    sample; only a known 'Crypto' rejects."""
    ticker = str(m.get("ticker") or "")
    series = ticker.split("-")[0]
    if not ticker:
        return None, "no ticker"
    if series.startswith("KXMVE"):
        return None, "parlay"
    if category is not None and str(category).strip().lower() == "crypto":
        return None, "crypto"
    close_ts = parse_ts(m.get("close_time"))
    if close_ts is None:
        return None, "no close_time"
    ahead = close_ts - poll_ts
    if ahead < WINDOW_LO or ahead > WINDOW_HI:
        return None, "outside 23-25h window"
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    if not (bid > 0 and ask < 1 and ask > bid):
        return None, "one-sided book"
    spread = ask - bid
    if spread > MAX_SPREAD + EPS:
        return None, "spread > 5c"
    bid_sz, ask_sz = _f(m.get("yes_bid_size_fp")), _f(m.get("yes_ask_size_fp"))
    if PRICE_LO - EPS <= ask <= PRICE_HI + EPS:
        side, price, size = "YES", ask, ask_sz
    elif (1 - PRICE_HI) - EPS <= bid <= (1 - PRICE_LO) + EPS:
        side, price, size = "NO", 1.0 - bid, bid_sz
    else:
        return None, "not a 95-99.5c favorite"
    row = {
        "ticker": ticker, "ts": int(poll_ts),
        "event_ticker": str(m.get("event_ticker") or ""), "series": series,
        "category": category, "side": side, "price": round(price, 4), "size_fp": size,
        "yes_bid": bid, "yes_ask": ask, "yes_bid_size": bid_sz, "yes_ask_size": ask_sz,
        "mid": round((bid + ask) / 2.0, 4), "spread": round(spread, 4),
        "close_time": close_ts,
        "expected_expiration_time": parse_ts(m.get("expected_expiration_time")),
        "volume_24h": _f(m.get("volume_24h_fp")), "volume": _f(m.get("volume_fp")),
        "open_interest": _f(m.get("open_interest_fp")),
        "updated_time": parse_ts(m.get("updated_time")),
    }
    return row, "candidate"


OBS_COLS = ("ticker", "ts", "event_ticker", "series", "category", "side", "price", "size_fp",
            "yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size", "mid", "spread", "close_time",
            "expected_expiration_time", "volume_24h", "volume", "open_interest", "updated_time")


# ── fees, both ways ─────────────────────────────────────────────────────────
def fee_raw(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def fee_centicent(p: float) -> float:
    """Kalshi's published schedule: round UP to the centicent ($0.0001)."""
    return math.ceil(round(fee_raw(p) * 10000.0, 6)) / 10000.0


def fee_cent(p: float) -> float:
    """What two referees believed: round UP to the cent, per order. Reported
    per contract for a one-contract order, which is the conservative case."""
    return math.ceil(round(fee_raw(p) * 100.0, 6)) / 100.0


# ── the pre-registered stopping rules ───────────────────────────────────────
def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    tot = 0.0
    for i in range(k + 1):
        tot += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                        + i * math.log(p) + (n - i) * math.log(1.0 - p))
    return min(1.0, tot)


def cp_upper(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper-Pearson 95% upper bound on a binomial rate. At 0/149
    it is 1.99%, which is where the 149 in the protocol comes from (the rule
    of three at 2%). Bisection on the exact CDF: no scipy in the container."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def decide(losses: int, events: int) -> str:
    if events < N_MIN_REPORT:
        return "COUNTS ONLY (fewer than %d settled events: no rate, no EV)" % N_MIN_REPORT
    rate = losses / float(events)
    if events >= N_REFUTE and rate > BREAK_EVEN_LOSS:
        return "REFUTED (event loss rate above the 2.0c break-even with >= %d events)" % N_REFUTE
    if events >= N_POWER:
        return "POWERED (>= %d events; report the CI)" % N_POWER
    if events >= N_BOUND and cp_upper(losses, events) < BREAK_EVEN_LOSS:
        return "BOUNDED (CP95 upper bound under 2.0 pct with >= %d events)" % N_BOUND
    return "NOT YET (need >= %d zero-loss events, or a CP95 bound under 2.0 pct)" % N_BOUND


# ── schema ──────────────────────────────────────────────────────────────────
DDL = [
    """CREATE TABLE IF NOT EXISTS kalshi_fav_obs (
           ticker                   TEXT             NOT NULL,
           ts                       BIGINT           NOT NULL,   -- hour slot, epoch seconds
           event_ticker             TEXT,
           series                   TEXT,
           category                 TEXT,                        -- NULL = series lookup unknown
           side                     TEXT             NOT NULL,   -- YES | NO
           price                    DOUBLE PRECISION NOT NULL,   -- what a taker of the favorite pays
           size_fp                  DOUBLE PRECISION,            -- top-of-book size on that side
           yes_bid                  DOUBLE PRECISION,
           yes_ask                  DOUBLE PRECISION,
           yes_bid_size             DOUBLE PRECISION,
           yes_ask_size             DOUBLE PRECISION,
           mid                      DOUBLE PRECISION,
           spread                   DOUBLE PRECISION,
           close_time               BIGINT,                      -- SCHEDULED close as observed
           expected_expiration_time BIGINT,
           volume_24h               DOUBLE PRECISION,            -- freshness, applied at analysis
           volume                   DOUBLE PRECISION,
           open_interest            DOUBLE PRECISION,
           updated_time             BIGINT,
           PRIMARY KEY (ticker, ts)
       )""",
    "CREATE INDEX IF NOT EXISTS kalshi_fav_obs_close ON kalshi_fav_obs(close_time)",
    # Gap accounting. One row per cycle ATTEMPT, so an outage is a fact in the
    # data rather than an absence a reader has to notice.
    """CREATE TABLE IF NOT EXISTS kalshi_fav_polls (
           ts               BIGINT PRIMARY KEY,
           ok               BOOLEAN NOT NULL,
           n_scanned        INTEGER NOT NULL,
           n_candidates     INTEGER NOT NULL,
           n_settle_checked INTEGER NOT NULL,
           latency_ms       INTEGER,
           err              TEXT
       )""",
    """CREATE TABLE IF NOT EXISTS kalshi_fav_settle (
           ticker           TEXT PRIMARY KEY,
           result           TEXT   NOT NULL,   -- yes | no
           status           TEXT   NOT NULL,   -- settled | finalized
           close_time_final BIGINT,            -- < observed close_time means it closed early
           settlement_ts    BIGINT,
           resolved_at      BIGINT NOT NULL
       )""",
    # One /series call per NEW series, ever. Without this cache a poll that
    # sees 400 gas-price markets would spend 400 calls learning "Economics".
    """CREATE TABLE IF NOT EXISTS kalshi_series (
           series     TEXT PRIMARY KEY,
           category   TEXT,
           fetched_at BIGINT NOT NULL
       )""",
]


def create_tables(conn) -> None:
    with conn.cursor() as cur:
        for stmt in DDL:
            cur.execute(stmt)
    conn.commit()
    _log("tables ready: kalshi_fav_obs, kalshi_fav_polls, kalshi_fav_settle, kalshi_series")


def load_series_cache(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT series, category FROM kalshi_series")
        return {r[0]: r[1] for r in cur.fetchall()}


def series_category(http: Http, series: str, cache: dict, new_rows: list, now: int):
    """Category from the cache, else one public call. A lookup that fails or
    comes back without a category is NOT cached: it returns None (recorded as
    NULL, excluded from the primary cell) and is retried next cycle, so a
    transient 5xx cannot permanently mislabel a series."""
    if series in cache:
        return cache[series]
    d, err, _ = http.get("/series/%s" % urllib.parse.quote(series))
    if err or not isinstance(d, dict):
        return None
    cat = (d.get("series") or {}).get("category")
    if not cat or not isinstance(cat, str):
        return None
    cache[series] = cat
    new_rows.append((series, cat, int(now)))
    return cat


# ── the hourly scan ─────────────────────────────────────────────────────────
def scan_window(http: Http, poll_ts: int) -> tuple:
    """(markets, err, last_raw_body). Server-side close-time window, so one
    cycle is a handful of pages instead of the ~150+ it takes to walk the
    whole listing (which is >95% KXMVE parlays). mve_filter=exclude drops the
    parlays before they reach us; KXMVE is still rejected client-side in case
    the parameter is ever ignored."""
    params = {"status": "open", "limit": PAGE_LIMIT, "mve_filter": "exclude",
              "min_close_ts": int(poll_ts + WINDOW_LO), "max_close_ts": int(poll_ts + WINDOW_HI)}
    out, cursor, pages, raw = [], "", 0, b""
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        d, err, raw = http.get("/markets", p)
        if err:
            return out, err, raw
        page = (d or {}).get("markets") or []
        out.extend(page)
        pages += 1
        cursor = (d or {}).get("cursor") or ""
        if not cursor or not page:
            return out, "", raw
        if pages >= MAX_SCAN_CALLS:
            # A truncated scan is a partial observation, and partial must be
            # visible: the poll row carries this text and ok=False.
            return out, "scan truncated at %d pages" % pages, raw


def settle_row_from(m: dict, now: int):
    """A settle tuple when the market is terminal WITH a result, else None.
    'determined', 'closed' and 'active' all return None and are retried."""
    status = str(m.get("status") or "")
    result = str(m.get("result") or "")
    ticker = str(m.get("ticker") or "")
    if not ticker or status not in SETTLED_STATUSES or not result:
        return None
    return (ticker, result, status, parse_ts(m.get("close_time")),
            parse_ts(m.get("settlement_ts")), int(now))


def pending_settlements(conn, now: int, limit: int) -> list:
    """Recorded tickers past scheduled close by more than an hour with no
    settle row yet, OLDEST FIRST so a backlog drains in order and a market
    can never be starved by newer ones."""
    with conn.cursor() as cur:
        cur.execute("""SELECT o.ticker, min(o.close_time) AS c
                       FROM kalshi_fav_obs o
                       WHERE o.close_time < %s
                         AND NOT EXISTS (SELECT 1 FROM kalshi_fav_settle s
                                         WHERE s.ticker = o.ticker)
                       GROUP BY o.ticker ORDER BY c LIMIT %s""",
                    (int(now) - 3600, int(limit)))
        return [r[0] for r in cur.fetchall()]


def resolve_settlements(http: Http, tickers: list, now: int) -> tuple:
    """(settle_rows, n_checked, err). One GET per ticker; stops at the call
    budget and leaves the rest for next cycle."""
    rows, checked, errs = [], 0, []
    for t in tickers:
        d, err, _ = http.get("/markets/%s" % urllib.parse.quote(t))
        if err:
            errs.append(err)
            if "budget" in err:
                break
            continue
        checked += 1
        row = settle_row_from((d or {}).get("market") or {}, now)
        if row:
            rows.append(row)
    return rows, checked, ("; ".join(errs[:3]) if errs else "")


def write_cycle(conn, slot: int, ok: bool, n_scanned: int, n_candidates: int,
                n_settle_checked: int, ms: int, err: str,
                obs_rows: list, settle_rows: list, series_rows: list) -> None:
    """One transaction: the poll record ALWAYS, the rest when there is any."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO kalshi_fav_polls
                           (ts, ok, n_scanned, n_candidates, n_settle_checked, latency_ms, err)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (ts) DO NOTHING""",
                    (int(slot), bool(ok), int(n_scanned), int(n_candidates),
                     int(n_settle_checked), int(ms), (err or "")[:300]))
        for s, cat, at in series_rows:
            cur.execute("""INSERT INTO kalshi_series (series, category, fetched_at)
                           VALUES (%s,%s,%s) ON CONFLICT (series) DO NOTHING""", (s, cat, at))
        for row in obs_rows:
            cur.execute("""INSERT INTO kalshi_fav_obs (%s) VALUES (%s)
                           ON CONFLICT (ticker, ts) DO NOTHING"""
                        % (", ".join(OBS_COLS), ", ".join(["%s"] * len(OBS_COLS))),
                        tuple(row[c] for c in OBS_COLS))
        for row in settle_rows:
            cur.execute("""INSERT INTO kalshi_fav_settle
                               (ticker, result, status, close_time_final, settlement_ts, resolved_at)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (ticker) DO NOTHING""", row)
    conn.commit()


def poll_once(conn, series_cache: dict | None = None, verbose: bool = False) -> bool:
    """One full cycle. conn=None is the dry run: live public API, nothing
    stored, everything printed."""
    slot = int(time.time() // INTERVAL) * INTERVAL
    now = int(time.time())
    http = Http()
    t0 = time.time()
    cache = series_cache if series_cache is not None else {}
    new_series = []

    markets, scan_err, raw = scan_window(http, slot)
    reasons, cands = {}, []
    for m in markets:
        row, why = classify(m, slot)
        if row is None:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        cat = series_category(http, row["series"], cache, new_series, now)
        row, why = classify(m, slot, cat)
        if row is None:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        cands.append(row)

    settle_rows, n_checked, settle_err = [], 0, ""
    if conn is not None:
        remaining = max(0, http.budget - http.calls)
        pending = pending_settlements(conn, now, min(MAX_SETTLE_PER_CYCLE, remaining))
        settle_rows, n_checked, settle_err = resolve_settlements(http, pending, now)

    ms = int((time.time() - t0) * 1000)
    ok = not scan_err
    err = "; ".join(e for e in (scan_err, settle_err) if e)
    if conn is not None:
        write_cycle(conn, slot, ok, len(markets), len(cands), n_checked, ms, err,
                    cands, settle_rows, new_series)

    if verbose:
        for u in http.urls[:6]:
            print("   GET %s" % u)
        if len(http.urls) > 6:
            print("   ... %d requests in total" % len(http.urls))
        if not markets:
            # Zero scanned is a request to debug, never a result. Show what
            # actually came back so a renamed parameter is visible at once.
            print("   ZERO MARKETS SCANNED — first 300 bytes of the last response:")
            print("   %r" % (raw[:300],))
        print("   scanned %d  candidates %d  rejected %s"
              % (len(markets), len(cands), json.dumps(reasons, sort_keys=True)))
        for r in sorted(cands, key=lambda x: (x["category"] or "~", x["ticker"]))[:60]:
            print("   %-38s %-19s %s %.3f x%-8.0f bid %.3f ask %.3f v24h %-8.0f close +%.1fh"
                  % (r["ticker"][:38], (r["category"] or "NULL")[:19], r["side"], r["price"],
                     r["size_fp"], r["yes_bid"], r["yes_ask"], r["volume_24h"],
                     (r["close_time"] - slot) / 3600.0))
        if len(cands) > 60:
            print("   ... %d more candidates" % (len(cands) - 60))
        print("   settlement: %s" % ("%d checked, %d resolved" % (n_checked, len(settle_rows))
                                     if conn is not None else "skipped (no database)"))
        print("   %d calls, %d ms%s" % (http.calls, ms, ("  ERR " + err) if err else ""))
    else:
        _log("cycle %s: scanned %d, candidates %d, settled %d/%d checked, %d calls, %d ms%s"
             % (time.strftime("%Y-%m-%d %H:00", time.gmtime(slot)), len(markets), len(cands),
                len(settle_rows), n_checked, http.calls, ms, ("  ERR " + err) if err else ""))
    return ok


# ── --status: the pre-registered analysis ───────────────────────────────────
def primary_cell(obs_rows: list) -> dict:
    """{ticker: obs} — the FIRST FRESH observation per ticker (volume_24h > 0)
    with a KNOWN non-crypto category. Rows are dicts with the OBS_COLS keys."""
    out = {}
    for r in sorted(obs_rows, key=lambda x: (x["ticker"], x["ts"])):
        if r["ticker"] in out:
            continue
        if r.get("category") is None or str(r["category"]).strip().lower() == "crypto":
            continue
        if not (_f(r.get("volume_24h")) > 0):
            continue
        out[r["ticker"]] = r
    return out


def _mean_se(xs: list) -> tuple:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mu = sum(xs) / n
    if n < 2:
        return mu, 0.0
    var = sum((x - mu) ** 2 for x in xs) / (n - 1)
    return mu, math.sqrt(var / n)


def _event_stats(cell: dict, settle: dict) -> dict:
    """Cluster by event_ticker over tickers that have a settle row. An event
    counts as settled only when EVERY primary-cell market in it has settled,
    so a half-resolved event cannot be scored on its winners first."""
    by_event = {}
    for t, r in cell.items():
        by_event.setdefault(r["event_ticker"] or t, []).append(r)
    events = []
    for ev, rows in by_event.items():
        if any(r["ticker"] not in settle for r in rows):
            continue
        pnl_cc, pnl_c, lost, early = [], [], False, False
        for r in rows:
            s = settle[r["ticker"]]
            won = (r["side"] == "YES" and s["result"] == "yes") or \
                  (r["side"] == "NO" and s["result"] == "no")
            gross = (1.0 - r["price"]) if won else (-r["price"])
            pnl_cc.append(gross - fee_centicent(r["price"]))
            pnl_c.append(gross - fee_cent(r["price"]))
            lost = lost or not won
            if s.get("close_time_final") and r.get("close_time") and \
                    s["close_time_final"] < r["close_time"]:
                early = True
        events.append({"event": ev, "n": len(rows), "lost": lost, "early": early,
                       "pnl_cc": sum(pnl_cc) / len(pnl_cc), "pnl_c": sum(pnl_c) / len(pnl_c)})
    return {"events": events, "n_events_all": len(by_event)}


def render_status(obs_rows: list, settle_rows: list, poll_rows: list) -> str:
    """The whole report as text so the tests can assert on it. obs_rows: dicts
    keyed like OBS_COLS; settle_rows: dicts (ticker, result, status,
    close_time_final, settlement_ts); poll_rows: (ts, ok) tuples."""
    L = []
    npolls = len(poll_rows)
    nbad = sum(1 for p in poll_rows if not p[1])
    if npolls:
        lo, hi = min(p[0] for p in poll_rows), max(p[0] for p in poll_rows)
        expected = int((hi - lo) // 3600) + 1
        missing = max(0, expected - npolls)
        L.append("  polls         %d recorded, %d failed  (expected ~%d)" % (npolls, nbad, expected))
        L.append("  MISSING       %d hours with no poll row at all%s"
                 % (missing, "  <- the recorder was down" if missing else ""))
    else:
        L.append("  polls         none recorded yet")
    tickers = {r["ticker"] for r in obs_rows}
    unknown = {r["ticker"] for r in obs_rows if r.get("category") is None}
    L.append("  candidates    %d observations, %d distinct tickers, %d with category NULL"
             " (excluded from the primary cell, not dropped)" % (len(obs_rows), len(tickers), len(unknown)))
    cell = primary_cell(obs_rows)
    settle = {s["ticker"]: s for s in settle_rows}
    st = _event_stats(cell, settle)
    events = st["events"]
    n_settled_t = sum(1 for t in cell if t in settle)
    losses = sum(1 for e in events if e["lost"])
    L.append("  primary cell  %d tickers (first FRESH obs per ticker), %d events"
             % (len(cell), st["n_events_all"]))
    L.append("  settled       %d tickers, %d events, %d losing events"
             % (n_settled_t, len(events), losses))
    # capacity: sum(size * price) per calendar day of observation
    per_day = {}
    for r in cell.values():
        day = time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))
        per_day[day] = per_day.get(day, 0.0) + _f(r.get("size_fp")) * _f(r.get("price"))
    if per_day:
        L.append("  capacity      $%.0f/day mean of sum(size*price) over %d observation days"
                 % (sum(per_day.values()) / len(per_day), len(per_day)))

    n = len(events)
    if n < N_MIN_REPORT:
        L.append("  decision      %s" % decide(losses, n))
        L.append("  (no rate, no EV and no interval are printed below %d settled events:"
                 " an under-powered number rendered with authority is the failure this"
                 " project exists to avoid)" % N_MIN_REPORT)
        return "\n".join(L)

    rate = losses / float(n)
    ub = cp_upper(losses, n)
    L.append("  event loss    %.2f%%  (Clopper-Pearson 95%% upper bound %.2f%%; break-even 2.00%%)"
             % (100.0 * rate, 100.0 * ub))
    mu_cc, se_cc = _mean_se([e["pnl_cc"] for e in events])
    mu_c, se_c = _mean_se([e["pnl_c"] for e in events])
    L.append("  mean P&L      fee_centicent %+.2fc (event-clustered SE %.2fc)   fee_cent %+.2fc (SE %.2fc)"
             % (100 * mu_cc, 100 * se_cc, 100 * mu_c, 100 * se_c))
    if n >= N_POWER:
        L.append("  95%% CI        fee_centicent [%+.2fc, %+.2fc]   fee_cent [%+.2fc, %+.2fc]"
                 % (100 * (mu_cc - 1.96 * se_cc), 100 * (mu_cc + 1.96 * se_cc),
                    100 * (mu_c - 1.96 * se_c), 100 * (mu_c + 1.96 * se_c)))
    for label, sub in (("on-schedule", [e for e in events if not e["early"]]),
                       ("early-closed", [e for e in events if e["early"]])):
        if len(sub) >= N_MIN_REPORT:
            m1, s1 = _mean_se([e["pnl_cc"] for e in sub])
            m2, s2 = _mean_se([e["pnl_c"] for e in sub])
            L.append("  %-13s %d events, %d losing, fee_centicent %+.2fc (SE %.2fc), fee_cent %+.2fc (SE %.2fc)"
                     % (label, len(sub), sum(1 for e in sub if e["lost"]), 100 * m1, 100 * s1, 100 * m2, 100 * s2))
        else:
            L.append("  %-13s %d events, %d losing (counts only below %d)"
                     % (label, len(sub), sum(1 for e in sub if e["lost"]), N_MIN_REPORT))
    L.append("  decision      %s" % decide(losses, n))
    return "\n".join(L)


def status(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT %s FROM kalshi_fav_obs" % ", ".join(OBS_COLS))
        obs = [dict(zip(OBS_COLS, r)) for r in cur.fetchall()]
        cur.execute("""SELECT ticker, result, status, close_time_final, settlement_ts
                       FROM kalshi_fav_settle""")
        settle = [dict(zip(("ticker", "result", "status", "close_time_final", "settlement_ts"), r))
                  for r in cur.fetchall()]
        cur.execute("SELECT ts, ok FROM kalshi_fav_polls")
        polls = cur.fetchall()
    print("kalshi_fav_recorder --status  (pre-registered analysis; prints, never trades)")
    print(render_status(obs, settle, polls))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="one cycle, store it, exit")
    ap.add_argument("--create", action="store_true", help="create tables and exit")
    ap.add_argument("--status", action="store_true", help="the pre-registered analysis, exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="one cycle against the live public API, no database, print everything")
    a = ap.parse_args()

    if a.dry_run:
        _log("dry run: live public API, nothing stored")
        poll_once(None, verbose=True)
        return 0

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set — use --dry-run to poll without storing")
        return 2
    import psycopg2
    conn = psycopg2.connect(url)
    create_tables(conn)

    if a.create:
        return 0
    if a.status:
        status(conn)
        return 0

    cache = load_series_cache(conn)
    _log("recording the 23-25h favorites cell, one cycle per hour (%d series cached)" % len(cache))
    if a.once:
        poll_once(conn, cache, verbose=True)
        return 0

    fails = 0
    while True:
        # Align to the top of the hour so slots are stable across restarts and
        # a resumed recorder lands on the same grid as the one before it.
        time.sleep(max(0.0, INTERVAL - (time.time() % INTERVAL)))
        try:
            ok = poll_once(conn, cache)
            fails = 0 if ok else fails + 1
            if not ok:
                _log("cycle failed (%d consecutive)" % fails)
            if fails >= MAX_CONSECUTIVE_FAIL:
                # Stop rather than write an unbroken run of failures nobody
                # reads. kalshi_fav_polls already records every one of them,
                # so the gap is explained in the data.
                _log("giving up after %d consecutive failures" % fails)
                return 1
        except KeyboardInterrupt:
            _log("stopped")
            return 0
        except Exception as e:                                    # noqa: BLE001
            fails += 1
            _log("loop error (%d): %s" % (fails, str(e)[:140]))
            try:
                conn.rollback()
            except Exception:                                     # noqa: BLE001
                pass
            # A cycle that RAISED still has to leave a row, or the hour is
            # indistinguishable from an hour with no candidates. poll_once
            # writes its own row for every error it handles; this covers the
            # ones it cannot — a dead database, a bug in here — and is itself
            # best-effort, because the alternative to a failed write is an
            # unlogged hour either way.
            try:
                slot = int(time.time() // INTERVAL) * INTERVAL
                write_cycle(conn, slot, False, 0, 0, 0, 0,
                            "cycle raised: %s" % str(e)[:200], [], [], [])
            except Exception:                                     # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
