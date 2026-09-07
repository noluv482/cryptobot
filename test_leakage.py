#!/usr/bin/env python3
"""POINT-IN-TIME LEAKAGE CHECK for the shadow_signals feature columns.

Every shadow row stores the raw features the bot saw when the signal fired
(rsi, atr_pct, adx, er). A learner trained on those columns is only honest if
each value was computable from bars that existed BEFORE row.ts. This test
rebuilds the four features from archived bars strictly before each row's ts
with the bot's OWN indicator functions (bot_server.calc_rsi / calc_atr /
calc_adx / calc_efficiency_ratio, over the same CANDLE_LIMIT-bar window the
scanner uses) and compares them with the stored values.

Three rebuild variants are scored per row, in this order:
    open_lt         bars whose OPEN ts < row.ts — what the scanner had in
                    hand, INCLUDING the still-forming bar (get_klines returns
                    it; the archive later overwrites it with its final values)
    closed_only     bars that had CLOSED by row.ts (ts + interval <= row.ts)
    future1         open_lt PLUS the first bar that opened at/after row.ts —
                    a value that matches THIS and neither of the above was
                    computed with a bar from the future: a LEAK.

Verdict per row: ok | ok_closed_only | mismatch | LEAK | no_bars | no_features.
'mismatch' is reported, not hidden: the forming bar's final archived values
differ from its partial values at signal time, and get_klines caches a pair's
bars for up to _KLINES_TTL seconds, so an exact rebuild is not always
possible. What the test forbids is LEAK. Rows that match closed_only are
consistent with the scanner having run just after a bar closed.

RUN
    python test_leakage.py                      # fixture rows, DB-less (CI)
    docker exec cryptobot-bot-1 python test_leakage.py --live 50
        # in-container: the last 50 real shadow rows against the `candles`
        # archive (read-only SELECTs over DATABASE_URL; the DSN is never
        # printed). Exit 1 only when a LEAK is found. The container name is
        # <compose project>-bot-1 — cryptobot-bot-1 on the home server.

No writes, no network, no order path. Never raises on garbage input.
"""
import argparse
import io
import os
import random
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bot_server as bs          # noqa: E402

bs.log = lambda *a, **k: None

FEATURES = ("rsi", "atr_pct", "adx", "er")
# Stored values are rounded by the producer: rsi/adx to 0.1, er to 0.001;
# atr_pct is stored raw (atr / price), compared relatively.
TOL_ABS = {"rsi": 0.11, "adx": 0.11, "er": 0.0011}
TOL_REL_ATR = 1e-6
VERDICTS = ("ok", "ok_closed_only", "mismatch", "LEAK", "no_bars", "no_features")


# ── pure helpers ─────────────────────────────────────────────────────────────
def clean_bars(bars):
    """[(ts, open, high, low, close, volume)] ascending; garbage rows dropped."""
    out = []
    for b in bars or ():
        try:
            t, o, h, l, c = (float(b[0]), float(b[1]), float(b[2]),
                             float(b[3]), float(b[4]))
            v = float(b[5]) if len(b) > 5 and b[5] is not None else 0.0
        except Exception:
            continue
        if not all(x == x for x in (t, o, h, l, c)):      # NaN guard
            continue
        out.append((t, o, h, l, c, v))
    out.sort(key=lambda b: b[0])
    return out


def select_bars(bars, row_ts, interval_s, mode="open_lt", limit=None):
    """The window a rebuild may see. STRICT inequalities are the contract."""
    limit = limit or bs.CANDLE_LIMIT
    if mode == "open_lt":
        sel = [b for b in bars if b[0] < row_ts]
    elif mode == "closed_only":
        sel = [b for b in bars if b[0] + interval_s <= row_ts]
    elif mode == "future1":
        sel = [b for b in bars if b[0] < row_ts + interval_s]
    else:
        raise ValueError(mode)
    return sel[-limit:]


def rebuild(bars, price):
    """{rsi, atr_pct, adx, er} from a bar window with the bot's own functions,
    or None when the window is too short to compute rsi."""
    if len(bars) < bs.RSI_PERIOD + 2:
        return None
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    c = [b[4] for b in bars]
    out = {"rsi": None, "atr_pct": None, "adx": None, "er": None}
    try:
        out["rsi"] = bs.calc_rsi(c)
    except Exception:
        pass
    try:
        atr = bs.calc_atr(h, l, c)
        out["atr_pct"] = (atr / float(price)) if price else None
    except Exception:
        pass
    try:
        out["adx"] = bs.calc_adx(h, l, c)
    except Exception:
        pass
    try:
        out["er"] = bs.calc_efficiency_ratio(c)
    except Exception:
        pass
    return out


def feature_match(name, stored, rebuilt):
    """True/False, or None when either side is missing (not comparable)."""
    if stored is None or rebuilt is None:
        return None
    try:
        s, r = float(stored), float(rebuilt)
    except Exception:
        return None
    if name == "atr_pct":
        return abs(s - r) <= TOL_REL_ATR * max(abs(s), abs(r), 1e-12) + 1e-12
    return abs(s - r) <= TOL_ABS[name]


def compare(row, rebuilt):
    if rebuilt is None:
        return {f: None for f in FEATURES}
    return {f: feature_match(f, row.get(f), rebuilt.get(f)) for f in FEATURES}


def _all_ok(res):
    vals = [v for v in res.values() if v is not None]
    return bool(vals) and all(vals)


def check_row(row, bars, interval_s):
    """One shadow row against its bar archive. Never raises."""
    out = {"id": row.get("id"), "pair": row.get("pair"), "ts": row.get("ts"),
           "verdict": "no_features", "compared": [], "detail": {},
           "stored": {f: row.get(f) for f in FEATURES}, "rebuilt": None}
    try:
        compared = [f for f in FEATURES if row.get(f) is not None]
        out["compared"] = compared
        if not compared:
            return out
        row_ts = float(row.get("ts"))
        price = row.get("price")
        cb = clean_bars(bars)
        variants = {m: rebuild(select_bars(cb, row_ts, interval_s, m), price)
                    for m in ("open_lt", "closed_only", "future1")}
        res = {m: compare(row, v) for m, v in variants.items()}
        out["detail"] = res
        out["rebuilt"] = variants["open_lt"]
        if variants["open_lt"] is None and variants["closed_only"] is None:
            out["verdict"] = "no_bars"
        elif _all_ok(res["open_lt"]):
            out["verdict"] = "ok"
        elif _all_ok(res["closed_only"]):
            out["verdict"] = "ok_closed_only"
        elif _all_ok(res["future1"]):
            out["verdict"] = "LEAK"
        else:
            out["verdict"] = "mismatch"
    except Exception as e:
        out["verdict"] = "no_bars"
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def summarize(results):
    counts = {v: 0 for v in VERDICTS}
    for r in results:
        counts[r.get("verdict", "no_bars")] = counts.get(r.get("verdict", "no_bars"), 0) + 1
    return counts


# ── fixtures (DB-less) ───────────────────────────────────────────────────────
def synth_bars(n=300, ts0=1_700_000_000, interval_s=3600, seed=5):
    rng = random.Random(seed)
    px, out = 100.0, []
    for i in range(n):
        o = px
        c = o * (1 + rng.gauss(0, 0.006))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.002)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
        out.append((ts0 + i * interval_s, o, h, l, c, 10 + rng.random()))
        px = c
    return out


def _stored_from(bars, row_ts, interval_s, mode, price):
    f = rebuild(select_bars(clean_bars(bars), row_ts, interval_s, mode), price)
    # the producer rounds rsi/adx to 0.1 and er to 0.001 already (calc_* do);
    # atr_pct is stored raw
    return dict(f)


FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def run_fixtures():
    iv = 3600
    bars = synth_bars(300, interval_s=iv)
    mid_ts = bars[250][0] + 1800          # signal fired mid-bar 250
    price = bars[250][4] * 1.001          # live ticker, not a close

    # 1. strictness of the windows
    w = select_bars(bars, bars[250][0], iv, "open_lt")
    check("open_lt excludes a bar whose open ts == row.ts", w[-1][0] == bars[249][0])
    w = select_bars(bars, bars[250][0], iv, "closed_only")
    check("closed_only includes a bar closing exactly at row.ts", w[-1][0] == bars[249][0])
    w = select_bars(bars, mid_ts, iv, "closed_only")
    check("closed_only excludes the forming bar", w[-1][0] == bars[249][0])
    w = select_bars(bars, mid_ts, iv, "open_lt")
    check("open_lt includes the forming bar", w[-1][0] == bars[250][0])
    check("window is capped to CANDLE_LIMIT bars (rsi depends on series length)",
          len(w) == bs.CANDLE_LIMIT, len(w))
    w = select_bars(bars, mid_ts, iv, "future1")
    check("future1 adds exactly the first bar opened after row.ts", w[-1][0] == bars[251][0])

    # 2. a row stored exactly as the scanner computes it -> ok
    row = {"id": 1, "pair": "XBTUSD", "ts": mid_ts, "price": price}
    row.update(_stored_from(bars, mid_ts, iv, "open_lt", price))
    r = check_row(row, bars, iv)
    check("honest row (features from bars strictly < ts) -> ok", r["verdict"] == "ok", r["verdict"])
    check("the future probe DISCRIMINATES (future1 does not also match an honest row)",
          not _all_ok(r["detail"]["future1"]), r["detail"]["future1"])
    check("all four features were compared", r["compared"] == list(FEATURES), r["compared"])

    # 3. a row computed with one future bar -> LEAK
    leak = {"id": 2, "pair": "XBTUSD", "ts": mid_ts, "price": price}
    leak.update(_stored_from(bars, mid_ts, iv, "future1", price))
    r = check_row(leak, bars, iv)
    check("row computed with a bar from the future -> LEAK", r["verdict"] == "LEAK", r["verdict"])

    # 4. computed from closed bars only (scanner ran right after a close) -> ok_closed_only
    cl = {"id": 3, "pair": "XBTUSD", "ts": mid_ts, "price": price}
    cl.update(_stored_from(bars, mid_ts, iv, "closed_only", price))
    r = check_row(cl, bars, iv)
    check("row computed from closed bars only -> ok_closed_only",
          r["verdict"] == "ok_closed_only", r["verdict"])

    # 5. a stored value nobody can rebuild -> mismatch (reported, never hidden)
    mm = dict(row)
    mm["id"] = 4
    mm["rsi"] = (row["rsi"] or 50.0) + 7.0
    r = check_row(mm, bars, iv)
    check("stored rsi off by 7 points -> mismatch", r["verdict"] == "mismatch", r["verdict"])
    check("mismatch names the failing feature",
          r["detail"]["open_lt"]["rsi"] is False and r["detail"]["open_lt"]["adx"] is True)

    # 6. tolerance respects the producer's rounding, not more
    near = dict(row)
    near["id"] = 5
    near["rsi"] = row["rsi"] + 0.1          # one rounding step
    near["er"] = row["er"] + 0.001
    check("one rounding step of rsi/er is still a match", check_row(near, bars, iv)["verdict"] == "ok")
    far = dict(row)
    far["atr_pct"] = row["atr_pct"] * 1.001
    check("atr_pct 0.1% off is NOT a match (relative 1e-6)",
          check_row(far, bars, iv)["verdict"] == "mismatch")

    # 7. garbage never raises
    check("row without features -> no_features",
          check_row({"id": 6, "ts": mid_ts, "price": price}, bars, iv)["verdict"] == "no_features")
    check("no bars -> no_bars", check_row(row, [], iv)["verdict"] == "no_bars")
    junk = [("x", None, 1), None, (mid_ts - 10, "a", "b", "c", "d"), 5]
    check("garbage bars -> no_bars, no exception", check_row(row, junk, iv)["verdict"] == "no_bars")
    check("garbage row -> no exception",
          check_row({"id": None, "ts": "nope", "price": None, "rsi": "?"}, bars, iv)["verdict"]
          in VERDICTS)
    check("too-short archive -> no_bars", check_row(row, bars[240:251], iv)["verdict"] == "no_bars")

    # 8. summary shape
    s = summarize([r, {"verdict": "ok"}, {"verdict": "LEAK"}])
    check("summarize counts every verdict", s["ok"] == 1 and s["LEAK"] == 1 and sum(s.values()) == 3)


# ── live mode (in-container) ─────────────────────────────────────────────────
LIVE_ROWS_SQL = ("SELECT id, ts, pair, price, rsi, atr_pct, adx, er FROM shadow_signals "
                 "WHERE ts IS NOT NULL ORDER BY ts DESC LIMIT %s")
LIVE_BARS_SQL = ("SELECT ts, open, high, low, close, volume FROM candles "
                 "WHERE pair=%s AND interval_m=%s AND ts >= %s AND ts < %s ORDER BY ts")


def run_live(n):
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("--live needs DATABASE_URL (run inside the bot container)")
        return 2
    try:
        import psycopg2
    except ImportError:
        print("--live needs psycopg2 (present in the bot image)")
        return 2
    conn = psycopg2.connect(dsn, connect_timeout=10)
    conn.autocommit = True
    iv_m = int(bs.INTERVAL)
    iv_s = iv_m * 60
    limit = int(bs.CANDLE_LIMIT)
    results = []
    try:
        with conn.cursor() as cur:
            cur.execute(LIVE_ROWS_SQL, (int(n),))
            rows = [dict(zip(("id", "ts", "pair", "price", "rsi", "atr_pct", "adx", "er"), r))
                    for r in cur.fetchall()]
        for row in rows:
            try:
                with conn.cursor() as cur:
                    cur.execute(LIVE_BARS_SQL, (row["pair"], iv_m,
                                                float(row["ts"]) - (limit + 3) * iv_s,
                                                float(row["ts"]) + 2 * iv_s))
                    bars = cur.fetchall()
            except Exception as e:
                bars = []
                print(f"  row {row['id']}: bars query failed: {type(e).__name__}")
            results.append(check_row(row, bars, iv_s))
    finally:
        conn.close()
    print(f"point-in-time check: {len(results)} most recent shadow rows, "
          f"{iv_m}m bars, window {limit} bars, tol rsi/adx ±0.11 er ±0.0011 atr_pct rel 1e-6")
    print(f"  {'id':>7s} {'pair':10s} {'verdict':15s} rsi(stored/open_lt)   adx   er")
    for r in results:
        st, rb = r["stored"], r["rebuilt"] or {}
        f = lambda v, d: ("—" if v is None else f"{float(v):.{d}f}")
        print(f"  {str(r['id']):>7s} {str(r['pair']):10s} {r['verdict']:15s} "
              f"{f(st['rsi'],1)}/{f(rb.get('rsi'),1)}   "
              f"{f(st['adx'],1)}/{f(rb.get('adx'),1)}   "
              f"{f(st['er'],3)}/{f(rb.get('er'),3)}")
    counts = summarize(results)
    print("  counts: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print("  ok = rebuilt from bars strictly before ts; ok_closed_only = matches the closed-bar\n"
          "  window; mismatch = neither (forming-bar overwrite / klines cache staleness are the\n"
          "  known causes — reported, not excused); LEAK = matches ONLY with a future bar.")
    return 1 if counts.get("LEAK") else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, default=None, metavar="N",
                    help="check the N most recent real shadow rows (needs DATABASE_URL)")
    args = ap.parse_args(argv)
    if args.live is not None:
        return run_live(args.live)
    run_fixtures()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        return 1
    print("all point-in-time leakage fixture checks pass "
          "(run --live N in-container for real rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
