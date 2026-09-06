#!/usr/bin/env python3
"""Data-hygiene contracts (2026-09-05): fwd168 backfill, candle resample,
config-ledger boot stamp, incremental funding history, spread-map 6h tier,
meta_lab shadow wiring.

All mocked — a fake context-manager cursor that records every (sql, params)
and answers SELECTs from SQL-substring fixtures. No Postgres, no Kraken.

What must hold:
  1. fwd168 BACKFILL touches ONLY fwd168 / max_up_48 / max_dn_48: the UPDATE
     never names fwd6/fwd24/fwd48/fwd_done; candidates are windowed on the
     1h ARCHIVE; a pass on fixture bars writes the same numbers
     _shadow_forward_calc gives the live filler; the filler loop calls it.
  2. RESAMPLE: 1m fixture rows -> 15m/5m buckets with first-open/last-close,
     max-high/min-low, summed volume, Kraken-aligned bucket ts; the forming
     bucket is dropped; the DB glue upserts on (pair, interval_m, ts); a
     thread runs it every 15 min under the 1m-archive env gate.
  3. BOOT STAMP: idempotent (ON CONFLICT DO NOTHING, same hash twice),
     survives no DB, main() calls it after runtime settings are restored.
  4. FUNDING: empty table -> full pull; newest > 7d old -> full; otherwise
     only rows from MAX(ts) - 48h are upserted.
  5. SPREAD 6h TIER: cells under n=100 stay omitted; buckets6h lands in the
     contract file; the bot's loader never mistakes it for a pair; the gate
     uses a bucket ONLY when the exact hour cell is absent.
  6. META_LAB WIRING: log_shadow stores meta_p; NULL without a trained
     model; equals meta_lab.score_signal with one; a scorer failure costs
     the score, never the row; and NOTHING outside the Database class reads
     meta_p (never a gate input — checked from the source).
"""
import ast
import json
import math
import os
import random
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bot_server as bs
import learning_report as lr
import meta_lab

bs.log = lambda *a, **k: None
SRC = open(os.path.join(HERE, "bot_server.py"), encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── fake DB ──────────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self._rows = []
        for key, rows in self.conn.results.items():
            if key in sql:
                self._rows = rows(sql, params) if callable(rows) else rows
                break

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.results = results or {}
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def sqls(self, needle):
        return [(s, p) for s, p in self.executed if needle in s]


_saved_conn = bs.db.conn
H = 3600

# ═════════════════════════════════════════════════════════════════════════════
print("\n-- 1. fwd168 backfill")
ts0 = 1_700_000_000.0
# 1h bars (ts, close, high, low): close climbs 100 -> 100 + h*0.05, a spike
# high of 111 at h=10 and a low of 93 at h=20 inside the 48h window.
bars = []
for h in range(0, 200):
    c = 100.0 + h * 0.05
    hi, lo = c + 0.5, c - 0.5
    if h == 10: hi = 111.0
    if h == 20: lo = 93.0
    bars.append((int(ts0) + h * H, c, hi, lo))
expected = bs._shadow_forward_calc(bars, ts0, 100.0)
check("fixture resolves through the live filler's math", expected is not None)

fc = FakeConn({
    "FROM shadow_signals s": [(1, ts0, "XBTUSD", 100.0), (2, ts0 + H, "XBTUSD", 100.0)],
    "SELECT ts, close, high, low FROM candles": bars,
})
try:
    bs.db.conn = fc
    n = bs._fwd168_backfill_pass(limit=500)
finally:
    bs.db.conn = _saved_conn
ups = fc.sqls("UPDATE shadow_signals")
check("pass fills both candidate rows", n == 2 and len(ups) == 2, f"n={n} updates={len(ups)}")
check("UPDATE names ONLY fwd168 / max_up_48 / max_dn_48",
      all("fwd168" in s and "max_up_48" in s and "max_dn_48" in s for s, _ in ups)
      and all(not any(k in s for k in ("fwd6", "fwd24", "fwd48", "fwd_done")) for s, _ in ups),
      ups[0][0] if ups else "no update")
check("no statement in the pass touches fwd6/fwd24/fwd48",
      all(not any(k in s.replace("fwd168", "") for k in ("fwd6", "fwd24", "fwd48"))
          for s, _ in fc.executed if "UPDATE" in s))
if ups:
    f168, mu, md, sid = ups[0][1]
    check("fwd168 value = the 168h close vs recorded price",
          abs(f168 - expected[3]) < 1e-12, f"{f168} vs {expected[3]}")
    check("max_up_48 = +11% spike, max_dn_48 = -7% dip",
          abs(mu - 0.11) < 1e-9 and abs(md - (-0.07)) < 1e-9, f"{mu} {md}")
    check("row id carried through", sid == 1)
sel = fc.sqls("FROM shadow_signals s")
check("candidates: fwd_done=1 AND fwd168 IS NULL, windowed on the 1h ARCHIVE",
      len(sel) == 1 and "fwd_done=1" in sel[0][0] and "fwd168 IS NULL" in sel[0][0]
      and "interval_m=60" in sel[0][0] and "169*3600" in sel[0][0]
      and sel[0][1] == (500,), sel[0][0] if sel else "no select")
check("bars come from the archive once per pair, not from Kraken OHLC",
      len(fc.sqls("SELECT ts, close, high, low FROM candles")) == 1
      and "requests.get" not in SRC[SRC.find("def _fwd168_backfill_pass"):SRC.find("def _spread_map_loop")])
# archive gap: not enough bars for 168h -> row stays pending, no UPDATE
fc2 = FakeConn({"FROM shadow_signals s": [(3, ts0, "XBTUSD", 100.0)],
                "SELECT ts, close, high, low FROM candles": bars[:100]})
try:
    bs.db.conn = fc2
    n2 = bs._fwd168_backfill_pass()
finally:
    bs.db.conn = _saved_conn
check("archive gap -> row left NULL (no UPDATE), never a guess",
      n2 == 0 and not fc2.sqls("UPDATE"))
# no DB -> quiet no-op
try:
    bs.db.conn = None
    check("no DB -> 0 without raising", bs._fwd168_backfill_pass() == 0)
finally:
    bs.db.conn = _saved_conn
filler = SRC[SRC.find("def _learning_filler_loop"):SRC.find("# ── Entry point")]
check("filler loop runs one bounded batch every pass",
      "_fwd168_backfill_pass()" in filler and "FWD168_BACKFILL_BATCH = 500" in SRC)
check("fill_shadow (live path) is untouched: still writes fwd6/24/48 + fwd_done=1",
      "fwd6=%s, fwd24=%s, fwd48=%s" in " ".join(SRC.split()) and "fwd_done=1" in SRC)

# ═════════════════════════════════════════════════════════════════════════════
print("\n-- 2. candle resample")
B = 1_700_000_000 - (1_700_000_000 % 900)           # aligned 15m bucket start
m1 = []
for i in range(30):                                  # two full 15m buckets
    ts = B + i * 60
    m1.append((ts, 100 + i, 100 + i + 0.7, 100 + i - 0.3, 100 + i + 0.5, 1.0 + i))
m1.append((B + 1800 + 60, 500, 510, 490, 505, 9.0))  # 3rd bucket: 1 bar, forming
random.Random(7).shuffle(m1)
out = bs.resample_bars(m1, 900, complete_before=B + 1800)
check("two complete 15m buckets, forming third dropped", [o[0] for o in out] == [B, B + 900],
      str([o[0] for o in out]))
if len(out) == 2:
    b0, b1 = out
    check("open = earliest bar's open (order-independent)", b0[1] == 100 and b1[1] == 115)
    check("close = latest bar's close", b0[4] == 114.5 and b1[4] == 129.5)
    check("high = max, low = min", b0[2] == 114.7 and b0[3] == 99.7 and b1[2] == 129.7)
    check("volume = sum", abs(b0[5] - sum(1.0 + i for i in range(15))) < 1e-9)
out_all = bs.resample_bars(m1, 900)
check("no complete_before -> forming bucket included (3 buckets)", len(out_all) == 3)
out5 = bs.resample_bars(m1, 300, complete_before=B + 1800)
check("5m buckets: 6 per 30 minutes, aligned to 300", len(out5) == 6
      and all(o[0] % 300 == 0 for o in out5))
check("bucket ts alignment = Kraken's (ts // bucket * bucket)",
      all(o[0] % 900 == 0 for o in out))
check("degenerate bucket size -> []", bs.resample_bars(m1, 0) == [])

fc = FakeConn({"FROM candles": [("AUSD",) + r for r in m1]
                               + [("BUSD",) + r for r in sorted(m1)[:15]]})   # BUSD: bucket B only
try:
    bs.db.conn = fc
    n = bs.db.resample_candles(15, B - 60, now=B + 1800 + 5)
finally:
    bs.db.conn = _saved_conn
ins = fc.sqls("INSERT INTO candles")
check("DB glue upserts every complete bucket for every pair (2 + 1)", n == 3 and len(ins) == 3,
      f"n={n} inserts={len(ins)}")
check("upsert is ON CONFLICT (pair,interval_m,ts) DO UPDATE",
      all("ON CONFLICT (pair,interval_m,ts) DO UPDATE" in s for s, _ in ins))
check("rows carry interval_m=15 and aligned ts",
      all(p[1] == 15 and p[2] % 900 == 0 for _, p in ins))
check("source query reads interval_m=1 since the lookback",
      any("interval_m=%s" in s and p == (1, B - 60) for s, p in fc.sqls("SELECT pair, ts, open")))
try:
    bs.db.conn = None
    check("no DB -> 0", bs.db.resample_candles(15, 0) == 0)
finally:
    bs.db.conn = _saved_conn
check("resample thread registered next to the 1m archive",
      '("Candle resample",   _resample_loop,       ()),' in SRC)
loop = SRC[SRC.find("def _resample_loop"):SRC.find("def _iso_to_epoch")]
check("loop: 15m and 5m, every 15 min, gated with the 1m archive env, never raises",
      bs.RESAMPLE_INTERVALS_M == (15, 5) and bs.RESAMPLE_PERIOD_S == 900
      and 'os.environ.get("M1_ARCHIVE", "1")' in loop and "except Exception as e:" in loop)
check("liquidation check still reads interval_m=15 (what the resample feeds)",
      "WHERE pair=%s AND interval_m=15" in SRC)

# ═════════════════════════════════════════════════════════════════════════════
print("\n-- 3. config_ledger boot stamp")
fc = FakeConn()
try:
    bs.db.conn = fc
    h1 = bs._stamp_config_boot()
    h2 = bs._stamp_config_boot()
finally:
    bs.db.conn = _saved_conn
ins = fc.sqls("INSERT INTO config_ledger")
check("stamp returns the live fingerprint", h1 == bs._cfg_fingerprint() and h1 == h2)
check("idempotent: ON CONFLICT (cfg_hash) DO NOTHING, same hash both times",
      len(ins) == 2 and all("ON CONFLICT (cfg_hash) DO NOTHING" in s for s, _ in ins)
      and ins[0][1][0] == ins[1][1][0] == h1)
check("snapshot stored is the secret-free JSON snapshot",
      ins and json.loads(ins[0][1][2]) == bs._cfg_snapshot()
      and not any(k in ins[0][1][2].upper() for k in ("PAPER_LOCK", "API_KEY", "TOKEN")))
try:
    bs.db.conn = None
    check("no DB -> None, nothing executed", bs._stamp_config_boot() is None)
finally:
    bs.db.conn = _saved_conn
main_src = SRC[SRC.find("def main():"):]
check("main() stamps AFTER runtime settings + autopilot restore, BEFORE threads",
      0 < main_src.find("_load_runtime_settings()") < main_src.find("_stamp_config_boot()")
      < main_src.find("threads = ["))

# ═════════════════════════════════════════════════════════════════════════════
print("\n-- 4. funding history: incremental vs full")
NOW = 1_800_000_000
fetched = [(NOW - i * H, 1e-5 * i) for i in range(0, 24 * 30)]   # 30 days hourly


def fetch(sym):
    return list(fetched)


def run_pass(newest):
    fc = FakeConn({"SELECT MAX(ts) FROM funding_rates": [(newest,)]})
    try:
        bs.db.conn = fc
        res = bs._funding_history_pass(("PF_XBTUSD",), fetch, now=NOW, pause=0)
    finally:
        bs.db.conn = _saved_conn
    return res["PF_XBTUSD"], fc.sqls("INSERT INTO funding_rates")


(mode, n), ins = run_pass(None)
check("empty table -> FULL pull, every fetched row upserted",
      mode == "full" and n == len(fetched) == len(ins), f"{mode} {n}")
(mode, n), ins = run_pass(NOW - 10 * 86400)
check("newest > 7d old -> FULL pull", mode == "full" and n == len(fetched), f"{mode} {n}")
newest = NOW - 3 * H
(mode, n), ins = run_pass(newest)
want = [r for r in fetched if r[0] >= newest - bs.FUNDING_INCR_WINDOW_S]
check("newest 3h old -> INCREMENTAL: only rows from MAX(ts)-48h (52 of 720: i=0..51)",
      mode == "incremental" and n == len(want) == 52 and len(ins) == 52, f"{mode} {n} {len(ins)}")
check("incremental rows all >= newest-48h and ts sent as int",
      all(p[2] >= newest - bs.FUNDING_INCR_WINDOW_S and isinstance(p[2], int) for _, p in ins))
(mode, n), ins = run_pass(NOW - 6 * 86400)
check("newest 6d old -> still incremental (under the 7d line)", mode == "incremental")
check("window constants: 48h incremental, 7d full",
      bs.FUNDING_INCR_WINDOW_S == 48 * 3600 and bs.FUNDING_FULL_STALE_S == 7 * 86400)


def boom(sym):
    raise RuntimeError("endpoint down")


fc = FakeConn({"SELECT MAX(ts) FROM funding_rates": [(None,)]})
try:
    bs.db.conn = fc
    res = bs._funding_history_pass(("PF_XBTUSD", "PF_ETHUSD"), boom, now=NOW, pause=0)
finally:
    bs.db.conn = _saved_conn
check("a failing fetch is contained per symbol", res == {"PF_XBTUSD": ("error", 0), "PF_ETHUSD": ("error", 0)})
try:
    bs.db.conn = FakeConn({"SELECT MAX(ts) FROM funding_rates": [(12345,)]})
    check("funding_newest_ts -> int", bs.db.funding_newest_ts("kraken", "PF_XBTUSD") == 12345)
    bs.db.conn = FakeConn({"SELECT MAX(ts) FROM funding_rates": [(None,)]})
    check("funding_newest_ts empty -> None (honest unknown)",
          bs.db.funding_newest_ts("kraken", "PF_XBTUSD") is None)
finally:
    bs.db.conn = _saved_conn
floop = SRC[SRC.find("def _funding_history_loop"):SRC.find("FUNDING_INCR_WINDOW_S =")]
check("loop delegates to the tested pass with the real fetch",
      "_funding_history_pass(syms, _funding_history_fetch)" in floop)

# ═════════════════════════════════════════════════════════════════════════════
print("\n-- 5. spread map 6h tier")
rows = []
for h in (1, 2, 3):                                  # 40 per hour: no hour cell, one bucket
    rows += [{"pair": "AUSD", "hour": h, "spread": 0.001 * h} for _ in range(40)]
rows += [{"pair": "BUSD", "hour": 14, "spread": 0.002} for _ in range(120)]   # exact cell
rows += [{"pair": "CUSD", "hour": 20, "spread": 0.002} for _ in range(50)]    # under floor everywhere
hm, b6 = lr.spread_map(rows), lr.spread_map_6h(rows)
check("hour map: AUSD has no cell (40 < 100), BUSD h14 does",
      "AUSD" not in hm and hm.get("BUSD", {}).get("14", {}).get("n") == 120)
check("6h tier: AUSD bucket '0' n=120 median 0.002, CUSD omitted (50 < 100)",
      b6.get("AUSD", {}).get("0", {}).get("n") == 120
      and abs(b6["AUSD"]["0"]["median_pct"] - 0.002) < 1e-12 and "CUSD" not in b6, str(b6))
check("bucket keys are start hours 0/6/12/18", set(b6["BUSD"]) == {"12"} and lr.BUCKET6H_STARTS == ("0", "6", "12", "18"))
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "spread_hours.json")
lr.write_spread_json(hm, path, buckets6h=b6)
doc = json.load(open(path, encoding="utf-8"))
check("contract file: pairs at top level (old shape), plus _meta and buckets6h",
      doc["BUSD"]["14"]["n"] == 120 and "_meta" in doc and doc["buckets6h"] == b6)
check("older-shape call (no buckets) still writes a valid file",
      (lr.write_spread_json(hm, path) or True) and "buckets6h" in json.load(open(path)))
lr.write_spread_json(hm, path, buckets6h=b6)

# bot loader must not mistake "buckets6h" for a pair
_s_file, _s_chk, _s_mt = bs.SPREAD_HOURS_FILE, bs._spread_hours_checked, bs._spread_hours_mtime
_s_map, _s_b6 = bs._spread_hours_map, bs._spread_buckets6h_map
try:
    bs.SPREAD_HOURS_FILE = path
    bs._spread_hours_checked, bs._spread_hours_mtime = 0.0, -1.0
    loaded = bs._load_spread_hours()
    check("loader: pairs only — 'buckets6h' and '_meta' are reserved, not pairs",
          set(loaded) == {"BUSD"}, str(set(loaded)))
    check("loader fills the 6h tier from the same file", bs._spread_buckets6h_map == b6)
    bs._spread_hours_checked = time.time() + 1e9          # pin: no reload mid-test
    # exact cell present -> exact wins (hour 14 on BUSD: 2x0.002 = 0.004 > cap 0.0025 -> cap)
    blk, thr, why = bs._spread_gate_check("BUSD", 0.0030, hour=14)
    check("exact cell present: used (2x median >= cap here, so cap) — bucket not consulted",
          "6h-bucket" not in why, why)
    # exact cell absent -> bucket: AUSD hour 4 has no cell; bucket '0' median 0.002 -> 2x = 0.004 > cap
    bs._spread_buckets6h_map = {"AUSD": {"0": {"median_pct": 0.0005, "p75_pct": 0.0009, "n": 120}}}
    blk, thr, why = bs._spread_gate_check("AUSD", 0.0012, hour=4)
    check("exact cell ABSENT -> 6h bucket stands in (2x0.05% = 0.10% < cap; 0.12% blocks)",
          blk is True and abs(thr - 0.001) < 1e-12 and "6h-bucket 00-05h" in why, f"{blk} {thr} {why}")
    blk, thr, why = bs._spread_gate_check("AUSD", 0.0008, hour=5)
    check("under the bucket threshold passes", blk is False and abs(thr - 0.001) < 1e-12)
    blk, thr, why = bs._spread_gate_check("AUSD", 0.0012, hour=7)
    check("hour 7 = bucket '6' — not mapped -> hard cap alone", blk is False and why == "hard cap", why)
    bs._spread_hours_map = {"AUSD": {"4": {"median_pct": 0.0010, "p75_pct": 0.0015, "n": 150}}}
    blk, thr, why = bs._spread_gate_check("AUSD", 0.0015, hour=4)
    check("when BOTH exist the exact hour cell wins (0.20% thr from h4, not 0.10% from bucket)",
          abs(thr - 0.002) < 1e-12 and "h4" in why and "6h-bucket" not in why, f"{thr} {why}")
    bs._spread_buckets6h_map = {"AUSD": {"0": {"median_pct": 0.0005, "p75_pct": 0.0009, "n": 60}}}
    blk, thr, why = bs._spread_gate_check("AUSD", 0.0012, hour=3)
    check("bucket under the n=100 floor is not trusted -> hard cap", why == "hard cap", why)
    check("unknown spread never blocks, bucket or not", bs._spread_gate_check("AUSD", None, hour=3)[0] is False)
finally:
    bs.SPREAD_HOURS_FILE, bs._spread_hours_checked, bs._spread_hours_mtime = _s_file, _s_chk, _s_mt
    bs._spread_hours_map, bs._spread_buckets6h_map = _s_map, _s_b6
check("run_report writes the 6h tier into the same file",
      "write_spread_json(smap, out_path, buckets6h=b6)" in open(os.path.join(HERE, "learning_report.py"), encoding="utf-8").read())

# ═════════════════════════════════════════════════════════════════════════════
print("\n-- 6. meta_lab shadow wiring")
check("schema: meta_p column, ALTER IF NOT EXISTS, nullable FLOAT",
      "ALTER TABLE shadow_signals ADD COLUMN IF NOT EXISTS meta_p FLOAT" in SRC)
feat = {"ts": 1.0, "pair": "XBTUSD", "sig": "BUY", "price": 100.0, "conf": 0.6,
        "rsi": 55.0, "atr_pct": 0.01, "adx": 20.0, "er": 0.2, "spread": 0.0005,
        "regime": "TRENDING", "hour": 13, "dow": 2, "pillars": "{}", "fkey": "k"}
nf = len(meta_lab.FEATURE_NAMES)
model = {"w": [0.1 * (i + 1) for i in range(nf)] + [-0.3],
         "mu": [0.0] * nf, "sd": [1.0] * nf, "fees_rt": 0.012}
p_ref = meta_lab.score_signal(feat, model)
check("reference score is a probability", p_ref is not None and 0.0 < p_ref < 1.0)


def shadow_insert(results):
    fc = FakeConn({"INSERT INTO shadow_signals": [(77,)], **results})
    try:
        bs.db.conn = fc
        bs.db._meta_loaded_ts, bs.db._meta_model_cache = 0.0, None
        sid = bs.db.log_shadow(dict(feat))
    finally:
        bs.db.conn = _saved_conn
        bs.db._meta_loaded_ts, bs.db._meta_model_cache = 0.0, None
    ins = fc.sqls("INSERT INTO shadow_signals")
    return sid, ins, fc


sid, ins, fc = shadow_insert({"to_regclass": [(None,)]})
check("no meta_lab table -> row inserted with meta_p NULL",
      sid == 77 and len(ins) == 1 and "meta_p" in ins[0][0] and ins[0][1][-1] is None)
check("model lookup probes the table first (to_regclass), never a failing SELECT",
      fc.sqls("to_regclass") and not fc.sqls("SELECT model FROM meta_lab"))
sid, ins, fc = shadow_insert({"to_regclass": [("meta_lab",)],
                              "SELECT model FROM meta_lab": [(json.dumps(model),)]})
check("trained model present -> meta_p = meta_lab.score_signal(features)",
      sid == 77 and ins[0][1][-1] is not None and abs(ins[0][1][-1] - p_ref) < 1e-12,
      str(ins[0][1][-1] if ins else None))
check("column list gained meta_p at the end, positional binding intact",
      "adx,er,spread,meta_p)" in ins[0][0].replace(" ", "") and ins[0][0].count("%s") == len(ins[0][1]))
sid, ins, fc = shadow_insert({"to_regclass": [("meta_lab",)],
                              "SELECT model FROM meta_lab": [(json.dumps({"w": [1.0]}),)]})
check("broken model (scorer raises) -> row still inserted, meta_p NULL",
      sid == 77 and len(ins) == 1 and ins[0][1][-1] is None)
sid, ins, fc = shadow_insert({"to_regclass": [("meta_lab",)],
                              "SELECT model FROM meta_lab": []})
check("table present but no trained row -> NULL", sid == 77 and ins[0][1][-1] is None)
# cache: second insert within the hour does not re-query the table
fc = FakeConn({"INSERT INTO shadow_signals": [(1,)], "to_regclass": [("meta_lab",)],
               "SELECT model FROM meta_lab": [(json.dumps(model),)]})
try:
    bs.db.conn = fc
    bs.db._meta_loaded_ts, bs.db._meta_model_cache = 0.0, None
    bs.db.log_shadow(dict(feat)); bs.db.log_shadow(dict(feat))
finally:
    bs.db.conn = _saved_conn
    bs.db._meta_loaded_ts, bs.db._meta_model_cache = 0.0, None
check("model cached (hourly reload): one table probe for two inserts",
      len(fc.sqls("to_regclass")) == 1 and len(fc.sqls("INSERT INTO shadow_signals")) == 2)
try:
    bs.db.conn = None
    check("meta_score with no DB -> None", bs.db.meta_score(feat) is None)
finally:
    bs.db.conn = _saved_conn
    bs.db._meta_loaded_ts, bs.db._meta_model_cache = 0.0, None

# NEVER a gate input: every code line mentioning meta_p lives inside class Database
tree = ast.parse(SRC)
db_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Database")
lo, hi = db_cls.lineno, db_cls.end_lineno
outside = []
for i, line in enumerate(SRC.splitlines(), 1):
    code = line.split("#", 1)[0]
    if "meta_p" in code and not (lo <= i <= hi):
        outside.append((i, line.strip()))
check("meta_p is read/written ONLY inside class Database (never a gate input)",
      not outside, str(outside[:3]))
engine = SRC[SRC.find("class SignalEngine"):SRC.find("class Database")]
check("SignalEngine / gates never mention meta_p or meta_score",
      "meta_p" not in engine and "meta_score" not in engine)
scan = SRC[SRC.find("def trading_loop"):SRC.find("def _m1_archive_loop")]
check("scan/entry path never calls meta_score", "meta_score" not in scan)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all data-hygiene contract checks pass")
