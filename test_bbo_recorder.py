#!/usr/bin/env python3
"""bbo_recorder contract — public data in, two tables out, gaps recorded.

This module runs unattended for weeks next to a process that trades. Two
properties have to hold no matter what anyone adds later:

  1. IT CANNOT REACH THE TRADING PATH. Not "does not today" — cannot. No
     bot_server import, no authenticated endpoint, no order verb. Asserted
     against the source, because behaviour tests only cover the paths someone
     thought to write.

  2. GAPS ARE RECORDED, NOT INFERRED. Every poll writes a bbo_polls row whether
     it worked or not. The server lost power for ten hours this month; a study
     that reads an outage as "the spread was normal" is the exact failure this
     project has spent a month unlearning.

Plus the one detail that would silently destroy the data's usefulness: Kraken
returns XXBTZUSD when you ask for XBTUSD, and a table keyed on the response
name never joins to candles.

Offline by default — no network, no database. Pass --live for one real public
poll.

Plain script: exit 0 == pass.
"""
import io
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbo_recorder as B

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("   " + str(detail)) if detail else ""))
    if not cond:
        FAILS.append(name)


SRC = io.open(B.__file__, encoding="utf-8").read()
CODE = re.sub(r'"""[\s\S]*?"""', "", SRC)
CODE = re.sub(r"#[^\n]*", "", CODE)


# ── [1] it cannot reach the trading path ───────────────────────────────────
print("[1] isolation")

check("does not import bot_server",
      not re.search(r"^\s*(import|from)\s+bot_server\b", CODE, re.M))
for mod in ("autopilot", "sizing", "clipper"):
    check("does not import %-10s" % mod,
          not re.search(r"^\s*(import|from)\s+%s\b" % mod, CODE, re.M))

check("touches only PUBLIC Kraken endpoints",
      "/0/public" in CODE and "/0/private" not in CODE)
for verb in ("AddOrder", "CancelOrder", "Balance", "API-Sign", "API-Key"):
    check("no reference to %-12s" % verb, verb not in CODE)

# Against SRC, not CODE. The SQL lives inside triple-quoted strings, and the
# prose-stripper above removes those wholesale — so CODE contains no INSERT at
# all and this check silently passed on an empty set. Verified: the module
# docstring contains no "INSERT INTO", so SRC carries no false-pass risk.
check("writes only its own two tables",
      set(re.findall(r"INSERT INTO (\w+)", SRC)) == {"bbo_1m", "bbo_polls"},
      sorted(set(re.findall(r"INSERT INTO (\w+)", SRC))))
check("never UPDATEs or DELETEs anything",
      "UPDATE " not in CODE.upper().replace("UPDATED", "")
      and "DELETE" not in CODE.upper())

# ── [2] the key remapping — the silent-data-loss bug ───────────────────────
print()
print("[2] Kraken's response keys are not the names you asked for")

B.fetch_bbo._amap = {"XXBTZUSD": "XBTUSD", "XETHZUSD": "ETHUSD", "SOLUSD": "SOLUSD"}
check("the module keeps a response-key -> requested-name map",
      "altname" in CODE and "_amap" in CODE)
check("...and it is built from AssetPairs at boot",
      "AssetPairs" in CODE)
check("an UNMAPPED key is reported, never silently dropped",
      "unmapped response keys" in SRC)

# an unmapped key must surface as an error, not vanish
_saved = B.fetch_bbo._amap
B.fetch_bbo._amap = {"SOLUSD": "SOLUSD"}
_orig = B.urllib.request.urlopen


class _Fake:
    def __init__(self, payload):
        self._p = payload.encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_ok(req, timeout=None):
    return _Fake('{"error":[],"result":{'
                 '"SOLUSD":{"b":["100.0","1","1.0"],"a":["100.2","2","2.0"]},'
                 '"XXBTZUSD":{"b":["9.0","1","1.0"],"a":["9.1","1","1.0"]}}}')


B.urllib.request.urlopen = _fake_ok
rows, err = B.fetch_bbo(["SOLUSD"])
B.urllib.request.urlopen = _orig
B.fetch_bbo._amap = _saved
check("a response key with no mapping produces an ERROR", bool(err), err)
check("...and the mapped rows still come back", len(rows) == 1 and rows[0][0] == "SOLUSD")

# ── [3] the spread the gate actually consumes ──────────────────────────────
print()
print("[3] spread convention")

# bot_server._spread_pct is (ask - bid) / bid — a fraction of the BID, not the
# mid. Storing bid and ask raw means the consumer derives it its own way and
# the two can never drift; storing a mid-based spread would be ~half a bp off
# from what the gate compares against, forever.
check("bid and ask are stored RAW, not a precomputed spread",
      "bid" in CODE and "ask" in CODE and "spread" not in
      " ".join(re.findall(r"CREATE TABLE IF NOT EXISTS bbo_1m[\s\S]*?\)", CODE)))
bid, ask = 100.0, 100.2
check("the printed spread uses the bid as denominator, matching the gate",
      abs((ask - bid) / bid - 0.002) < 1e-12
      and "(ask - bid) / max(bid, 1e-9)" in CODE)

# ── [4] gaps are data ──────────────────────────────────────────────────────
print()
print("[4] gap accounting")

writes = []


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, q, args=None):
        writes.append((q.split()[0].upper() + " " + (q.split()[2] if len(q.split()) > 2 else ""), args))

    def fetchone(self):
        return (0, 0, 0, 0)


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass


B.write(_Conn(), 1700000000, [], False, 42, "boom")
polls = [w for w in writes if "bbo_polls" in w[0]]
samples = [w for w in writes if "bbo_1m" in w[0]]
check("a FAILED poll still writes a bbo_polls row", len(polls) == 1)
check("...and writes no samples", len(samples) == 0)
check("...carrying the error text", any("boom" in str(w[1]) for w in polls))

writes.clear()
B.write(_Conn(), 1700000060, [("SOLUSD", 1.0, 1.1, 5.0, 6.0)], True, 30, "")
check("a good poll writes the poll row AND the sample",
      len([w for w in writes if "bbo_polls" in w[0]]) == 1
      and len([w for w in writes if "bbo_1m" in w[0]]) == 1)

check("re-running a minute cannot duplicate it",
      SRC.count("ON CONFLICT") >= 2)      # SQL is in strings — see above
check("status() reports MISSING minutes, not just what is present",
      "MISSING" in SRC and "the recorder was down" in SRC)

# ── [5] honest degradation ─────────────────────────────────────────────────
print()
print("[5] degradation")


def _boom(req, timeout=None):
    raise OSError("network is down")


B.urllib.request.urlopen = _boom
rows, err = B.fetch_bbo(["SOLUSD"])
B.urllib.request.urlopen = _orig
check("a dead network returns an error, never raises", rows == [] and bool(err))

B.urllib.request.urlopen = lambda req, timeout=None: _Fake('{"error":["EGeneral:Bad"],"result":{}}')
rows, err = B.fetch_bbo(["SOLUSD"])
B.urllib.request.urlopen = _orig
check("a Kraken-side error is reported as one", "kraken" in err.lower(), err)

check("the loop gives up rather than logging failures forever",
      "MAX_CONSECUTIVE_FAIL" in CODE and "giving up" in SRC)
check("one sample per minute, aligned to the minute",
      B.INTERVAL == 60 and "time.time() % INTERVAL" in CODE)

# ── [6] optional live check ────────────────────────────────────────────────
if "--live" in sys.argv:
    print()
    print("[6] live public poll")
    B.fetch_bbo._amap = B.altname_map(["XBTUSD", "SOLUSD"])
    rows, err = B.fetch_bbo(["XBTUSD", "SOLUSD"])
    check("a real poll returns rows", bool(rows) and not err, err)
    check("...named as REQUESTED, not as Kraken's key",
          {r[0] for r in rows} <= {"XBTUSD", "SOLUSD"}, {r[0] for r in rows})
    check("...with a sane spread", all(0 <= (r[2] - r[1]) / r[1] < 0.05 for r in rows))

print()
if FAILS:
    print("%d FAILURES" % len(FAILS))
    sys.exit(1)
print("all bbo_recorder checks pass")
