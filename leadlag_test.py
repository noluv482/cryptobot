#!/usr/bin/env python3
"""Judge the cross-exchange lead-lag signal with the find_signal.py discipline.

THE QUESTION. Every price-based signal failed the cost hurdle (1.4M candles,
find_signal.py), so this tests the last retail-feasible door: when Coinbase's
price is above Kraken's at the end of a minute, does Kraken's NEXT price move
close the gap by more than it costs to trade it?

Signal at end of minute t (both venues' candles closed, no lookahead):
    d = (cb_close - kr_close) / kr_close
    d > +thr  ->  BUY on Kraken (it should catch up)
    d < -thr  ->  SELL on Kraken
Entry is reported as TWO BOUNDS, because adversarial review found the naive
version flattered the strategy: the "open of minute t+1" is the FIRST trade of
that minute, which on a liquid pair prints ~0.0x seconds in — before any real
order could be computed and routed. So:
    FAST entry = kr open of t+1  (zero-latency, optimistic bound)
    SLOW entry = kr open of t+2  (a full minute of latency, pessimistic bound)
The collectable truth lives between the bounds. If even FAST fails the cost
hurdle, the idea is dead with no further questions.
Exit = Kraken open of the first minute >= t+1+h (15-min grace, else dropped).

THE RULES (the discipline that killed everything else, plus one new guard):
  1. FULL SET, no cherry-picking: all pairs fetched, per-pair AND pooled.
  2. SPLIT BY TIME: configs (threshold x horizon) are ranked on the first half
     only; the verdict is the OOS number of the single in-sample winner.
  3. NON-OVERLAPPING trades: after entry for horizon h, no new signal until
     the previous trade has exited. Overlap inflates t ~3x (measured before).
  4. DIRECTION-MATCHED BASELINE: each side is judged against the unconditional
     forward return of the same period, not against zero.
  5. COST HURDLE: mean edge vs 0.52% spot maker round trip — but this strategy
     enters immediately, which is TAKER-shaped, so 0.72% is the realistic spot
     hurdle; 0.16% futures-tier shown for reference. Spread is NOT in trade
     data, so every hurdle here is a LOWER bound on the real cost.
  6. STALENESS GUARD (the lead-lag-specific trap): if Kraken's last print in
     minute t is old, "Kraken converges to Coinbase" is partly just a stale
     price catching up — predictable on paper, untradeable. Every result is
     stratified fresh (last trade <=10s before minute end) vs stale. An edge
     that lives only in the stale bucket is a mirage and is reported as one.
  7. POOLED t IS OPTIMISTIC: crypto pairs move together, so simultaneous
     trades across pairs are not independent observations. The per-pair table
     is the honest unit; the pooled t is a ceiling, not a fact.

--selftest calibrates the instrument on synthetic markets (Kraken lagging
Coinbase by 1 minute; two independent random walks). Candles carry distinct
opens and closes (an intraminute step) so an entry-at-close lookahead bug
would CHANGE the numbers — review found the earlier synth had open==close,
which made the calibration blind to exactly that bug class.

Usage:
    python3 leadlag_test.py                # judge data/leadlag/
    python3 leadlag_test.py --selftest     # calibrate the instrument first
"""
import argparse
import csv
import math
import os
import statistics
import sys

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "leadlag")
THRESHOLDS = [0.0005, 0.001, 0.002, 0.003]      # 0.05% .. 0.3%
HORIZONS = [1, 5, 15, 60]                        # minutes held
GRACE = 15                                       # max minutes to find the exit
FRESH_S = 10                                     # staleness cut, seconds
COST_MAKER = 0.0052                              # Kraken spot maker round trip
COST_TAKER = 0.0072                              # realistic for immediate entry
COST_FUT = 0.0016                                # futures-tier reference
MIN_IS_TRADES = 100                              # rank a config only with >= this


def load(path, has_meta):
    """{minute_ts: (open, close[, staleness_s])}"""
    out = {}
    with open(path, encoding="utf-8") as f:
        next(f, None)
        for line in f:
            b = line.rstrip("\n").split(",")
            if len(b) < 6 or not b[0].isdigit():
                continue
            try:
                ts = int(b[0])
                if has_meta:
                    out[ts] = (float(b[1]), float(b[4]),
                               max(0.0, ts + 60 - float(b[7])))
                else:
                    out[ts] = (float(b[1]), float(b[4]))
            except (ValueError, IndexError):
                continue
    return out


def t_stat(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / (sd / math.sqrt(n)) if sd > 1e-15 else 0.0


def fmean(xs):
    return statistics.fmean(xs) if xs else 0.0


def baselines(kr, lo, hi):
    """Unconditional non-overlapping forward mean per horizon over [lo, hi),
    same convention as trades: enter open of t+1, exit open of first minute
    >= t+1+h within grace."""
    base = {}
    for h in HORIZONS:
        rets, t = [], lo
        while t < hi:
            e = kr.get(t + 60)
            x = next((kr.get(t + 60 * (1 + h) + 60 * g)
                      for g in range(GRACE + 1)
                      if kr.get(t + 60 * (1 + h) + 60 * g)), None)
            if e and x:
                rets.append((x[0] - e[0]) / e[0])
            t += 60 * (1 + h)
        base[h] = fmean(rets)
    return base


def run_config(kr, cb, minutes, lo, hi, thr, h, base, pair):
    """Non-overlapping trades for one config over [lo, hi). Each trade:
    (edge_fast, edge_slow_or_None, side, staleness_s, gap_closed_frac, pair)."""
    trades = []
    nxt = lo
    for t in minutes:
        if t < nxt or t >= hi:
            continue
        k, c = kr.get(t), cb.get(t)
        if not k or not c:
            continue
        d = (c[1] - k[1]) / k[1]
        if abs(d) < thr:
            continue
        side = 1 if d > 0 else -1
        ent = kr.get(t + 60)
        if not ent:
            continue                      # no real Kraken trade to enter on
        ex = exit_t = None
        for g in range(GRACE + 1):
            ex = kr.get(t + 60 * (1 + h) + 60 * g)
            if ex:
                exit_t = t + 60 * (1 + h) + 60 * g
                break
        if not ex:
            continue
        e_fast = side * ((ex[0] - ent[0]) / ent[0]) - side * base[h]
        # SLOW bound: the same trade shifted one minute later — enter t+2,
        # hold the same h minutes. Measures what's left after real latency.
        e_slow = None
        ent2 = kr.get(t + 120)
        if ent2:
            for g in range(GRACE + 1):
                ex2 = kr.get(t + 60 * (2 + h) + 60 * g)
                if ex2:
                    e_slow = (side * ((ex2[0] - ent2[0]) / ent2[0])
                              - side * base[h])
                    exit_t = max(exit_t, t + 60 * (2 + h) + 60 * g)
                    break
        closed = ((ent[0] - k[1]) / k[1]) / d if abs(d) > 1e-12 else 0.0
        trades.append((e_fast, e_slow, side,
                       k[2] if len(k) > 2 else 0.0, closed, pair))
        nxt = exit_t + 60
    return trades


def judge(pair_data, label):
    """pair_data: {pair: (kr, cb)}. Full discipline; prints a report."""
    spans = {}
    for pair, (kr, cb) in pair_data.items():
        common = sorted(set(kr) & set(cb))
        if len(common) > 2000:
            spans[pair] = common
    if not spans:
        print("not enough aligned data yet")
        return None
    print(f"\n=== {label} ===")
    for pair, mins in spans.items():
        kr = pair_data[pair][0]
        stale = [kr[t][2] for t in mins if len(kr[t]) > 2]
        med = statistics.median(stale) if stale else 0
        print(f"  {pair:8s} {len(mins):>7,} aligned minutes  "
              f"med staleness {med:5.1f}s")

    lo = min(m[0] for m in spans.values())
    hi = max(m[-1] for m in spans.values())
    mid = (lo + hi) // 2

    def collect(a, b):
        per = {}
        for pair, mins in spans.items():
            kr, cb = pair_data[pair]
            base = baselines(kr, a, b)
            for thr in THRESHOLDS:
                for h in HORIZONS:
                    per.setdefault((thr, h), []).extend(
                        run_config(kr, cb, mins, a, b, thr, h, base, pair))
        return per

    is_res = collect(lo, mid)
    ranked = sorted(((t_stat([tr[0] for tr in trs]), thr, h)
                     for (thr, h), trs in is_res.items()
                     if len(trs) >= MIN_IS_TRADES),
                    key=lambda x: -abs(x[0]))
    if not ranked:
        print("  no config reached MIN_IS_TRADES in-sample — no verdict")
        return None
    print(f"\n  in-sample (first half), top 3 of {len(is_res)} configs "
          f"(the best of {len(is_res)} looks good by luck alone):")
    for t, thr, h in ranked[:3]:
        trs = is_res[(thr, h)]
        print(f"    thr {thr*100:.2f}%  hold {h:>2}m  "
              f"fast edge {fmean([x[0] for x in trs])*100:+.4f}%  "
              f"t {t:+6.2f}  n {len(trs):,}")

    t_is, thr, h = ranked[0]
    oos = collect(mid, hi).get((thr, h), [])
    if len(oos) < 30:
        print("  winner has <30 OOS trades — no verdict")
        return None
    fast = [x[0] for x in oos]
    slow = [x[1] for x in oos if x[1] is not None]
    m_fast, t_fast = fmean(fast), t_stat(fast)
    m_slow, t_slow = fmean(slow), t_stat(slow)
    print(f"\n  OUT-OF-SAMPLE VERDICT (winner only: thr {thr*100:.2f}%, "
          f"hold {h}m; in-sample t was {t_is:+.2f}):")
    print(f"    FAST entry (t+1 open, zero-latency, optimistic): "
          f"edge {m_fast*100:+.4f}%/trade  t {t_fast:+6.2f}  n {len(fast):,}")
    print(f"    SLOW entry (t+2 open, 1-min latency, pessimistic): "
          f"edge {m_slow*100:+.4f}%/trade  t {t_slow:+6.2f}  n {len(slow):,}")
    buys = [x[0] for x in oos if x[2] > 0]
    sells = [x[0] for x in oos if x[2] < 0]
    if buys and sells:
        print(f"    sides (fast): BUY {fmean(buys)*100:+.4f}% (n {len(buys):,})"
              f"   SELL {fmean(sells)*100:+.4f}% (n {len(sells):,})")
    fresh = [x[0] for x in oos if x[3] <= FRESH_S]
    stale = [x[0] for x in oos if x[3] > FRESH_S]
    if fresh and stale:
        print(f"    staleness (fast): FRESH {fmean(fresh)*100:+.4f}% "
              f"(n {len(fresh):,})   STALE {fmean(stale)*100:+.4f}% "
              f"(n {len(stale):,})   <- edge only-in-stale = mirage")
    print(f"    median gap already closed at fast entry: "
          f"{statistics.median([x[4] for x in oos])*100:.0f}%")
    print(f"    per pair (fast, OOS) — the honest unit; pooled t is a ceiling:")
    for pair in sorted(spans):
        pf = [x[0] for x in oos if x[5] == pair]
        if len(pf) >= 10:
            print(f"      {pair:8s} edge {fmean(pf)*100:+.4f}%  "
                  f"t {t_stat(pf):+6.2f}  n {len(pf):,}")
    print(f"    cost hurdles: taker {COST_TAKER*100:.2f}% (realistic for this) "
          f"| maker {COST_MAKER*100:.2f}% | futures-ref {COST_FUT*100:.2f}% "
          f"— spread NOT included, real cost is higher")
    coll = m_fast > COST_TAKER and t_fast > 2
    print("    COLLECTABLE at spot taker cost?  "
          + ("FAST-BOUND YES — but SLOW bound and live paper must confirm"
             if coll else "NO"))
    return m_fast, t_fast, len(fast)


# ── selftest: calibrate the instrument before trusting it ────────────────────

def _synth(lag_minutes, n=20000, seed=7):
    """Coinbase random walk; Kraken follows it `lag_minutes` behind (0 = an
    independent walk: the null). Prices step TWICE per minute so open != close
    within every candle — a harness that peeks (enters at close instead of the
    next open) produces different numbers and fails calibration."""
    import random
    rng = random.Random(seed)
    t0 = 1_700_000_000
    steps = 2 * n
    cb_path = [100.0]
    for _ in range(steps):
        cb_path.append(cb_path[-1] * (1 + rng.gauss(0, 0.0011)))
    kr_path = [100.0]
    for i in range(1, steps + 1):
        if lag_minutes:
            target = cb_path[max(0, i - 2 * lag_minutes)]
            kr_path.append(kr_path[-1] + 0.45 * (target - kr_path[-1])
                           + kr_path[-1] * rng.gauss(0, 0.0003))
        else:
            kr_path.append(kr_path[-1] * (1 + rng.gauss(0, 0.0011)))
    kr, cb = {}, {}
    for m in range(1, n):
        ts = t0 + m * 60
        a, b = 2 * m, 2 * m + 1          # the minute's two intraminute steps
        cb[ts] = (cb_path[a], cb_path[b])
        kr[ts] = (kr_path[a], kr_path[b], 1.0)   # always fresh
    return kr, cb


def selftest():
    print("SELFTEST 1: Kraken lags Coinbase by 1 minute -> must find a big edge")
    r1 = judge({"SYNTH": _synth(1)}, "planted lag")
    print("\nSELFTEST 2: independent random walks -> must find nothing")
    r2 = judge({"SYNTH": _synth(0)}, "null")
    ok1 = r1 is not None and r1[0] > 0 and r1[1] > 3
    ok2 = r2 is None or abs(r2[1]) < 3
    print(f"\nplanted-edge found: {'PASS' if ok1 else 'FAIL'}   "
          f"null stayed null: {'PASS' if ok2 else 'FAIL'}")
    return 0 if ok1 and ok2 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    pair_data = {}
    for fn in sorted(os.listdir(DIR) if os.path.isdir(DIR) else []):
        if fn.endswith("_kr_1.csv"):
            pair = fn[:-9]
            cbp = os.path.join(DIR, f"{pair}_cb_1.csv")
            if os.path.exists(cbp):
                pair_data[pair] = (load(os.path.join(DIR, fn), True),
                                   load(cbp, False))
    if not pair_data:
        print("no data in data/leadlag/ — run leadlag_fetch.py first")
        return 1
    judge(pair_data, "cross-exchange lead-lag, real data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
