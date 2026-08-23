#!/usr/bin/env python3
"""Did loosening the entry gates bury a real edge?

THE QUESTION. Three gates were each loosened repeatedly, always in the same
direction, never against held-out data (the trail is in the source comments
and git history):

    MIN_CONFIDENCE  0.50 -> 0.35     ADX_MIN  20 -> 18 -> 14 -> 11 -> 8
    ER_MIN          0.15 -> 0.08 -> 0.05 -> 0.03

Every signal measurement since — including t=+0.04 over 1,421 signals — was
made UNDER the loosened gates. So "the signal is a coin flip" might mean the
signal was always a coin flip, or it might mean the coin flip is what the
loosening let in. Different diagnoses, different fixes.

THE TEST. shadow_signals records every signal with its 48h forward return,
but not ADX or ER — so both are RECOMPUTED here at each signal's timestamp
from the stored 1h candles, using the bot's own calc_adx / calc_efficiency_
ratio on the same 80-bar window the live scan uses. No new data, no fitting.

PRE-REGISTERED PRIMARY HYPOTHESIS (declared before looking): signals passing
the ORIGINAL gates (conf>=0.50, ADX>=18, ER>=0.15) have a higher direction-
matched 48h return than the signals the loosening added. Secondary, reported
but not the verdict: each gate alone at its original value.

DISCIPLINE:
  - direction-matched: ret = +fwd48 for BUY, -fwd48 for SELL
  - NON-OVERLAPPING per pair: signals closer than 48h to the previous kept
    one are dropped before any t is computed (overlap inflated t ~3x
    elsewhere in this project); raw-n figures shown for reference only
  - the verdict is the primary hypothesis; the per-gate grid is context
  - too-small subsets refuse: no mean printed under n_indep = 30

Run inside the container (needs DB + candles):
    docker exec -w /app cryptobot-bot-1 python3 analyze_gate_loosening.py
"""
import math
import statistics
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs

bs.log = lambda *a, **k: None

WINDOW = 80                 # live CANDLE_LIMIT: judge what the LIVE gate saw
HORIZON_S = 48 * 3600
MIN_INDEP = 30
ORIG = {"conf": 0.50, "adx": 18.0, "er": 0.15}
CUR  = {"conf": 0.35, "adx": 8.0,  "er": 0.03}


def t_stat(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.fmean(xs) / (sd / math.sqrt(n)) if sd > 1e-12 else 0.0


def load_signals():
    with bs.db.conn.cursor() as cur:
        cur.execute("""SELECT ts, pair, sig, conf, fwd48 FROM shadow_signals
                       WHERE fwd_done=1 AND fwd48 IS NOT NULL
                         AND sig IN ('BUY','SELL')
                       ORDER BY pair, ts""")
        return cur.fetchall()


def load_candles(pair):
    with bs.db.conn.cursor() as cur:
        cur.execute("""SELECT ts, high, low, close FROM candles
                       WHERE pair=%s AND interval_m=60 ORDER BY ts""", (pair,))
        return cur.fetchall()


def main():
    if not bs.db.connected:
        print("no DB")
        return 1
    rows = load_signals()
    print(f"shadow signals with a 48h outcome: {len(rows):,}")

    # recompute ADX and ER at each signal from the candles the bot stores
    candles = {}
    enriched = []
    skipped = 0
    for ts, pair, sig, conf, fwd48 in rows:
        if pair not in candles:
            candles[pair] = load_candles(pair)
        cs = candles[pair]
        # candles strictly before the signal, mirroring the live no-lookahead rule
        idx = 0
        lo_i, hi_i = 0, len(cs)
        while lo_i < hi_i:                     # bisect on ts
            mid = (lo_i + hi_i) // 2
            if cs[mid][0] <= ts:
                lo_i = mid + 1
            else:
                hi_i = mid
        idx = lo_i
        win = cs[max(0, idx - WINDOW):idx]
        if len(win) < WINDOW:
            skipped += 1
            continue
        highs  = [float(r[1]) for r in win]
        lows   = [float(r[2]) for r in win]
        closes = [float(r[3]) for r in win]
        adx = bs.calc_adx(highs, lows, closes)
        er  = bs.calc_efficiency_ratio(closes)
        ret = float(fwd48) if sig == "BUY" else -float(fwd48)
        enriched.append({"ts": float(ts), "pair": pair, "conf": float(conf or 0),
                         "adx": float(adx or 0), "er": float(er or 0), "ret": ret})
    print(f"enriched with recomputed ADX/ER: {len(enriched):,} "
          f"(skipped {skipped} without {WINDOW} prior candles)")

    # non-overlapping selection per pair — 48h forward windows must not overlap
    indep = []
    last_kept = {}
    for e in sorted(enriched, key=lambda x: (x["pair"], x["ts"])):
        lk = last_kept.get(e["pair"])
        if lk is None or e["ts"] - lk >= HORIZON_S:
            indep.append(e)
            last_kept[e["pair"]] = e["ts"]
    print(f"non-overlapping (one per pair per 48h): {len(indep):,}\n")

    def describe(name, xs, raw_n=None):
        n = len(xs)
        extra = f"  (raw n={raw_n:,})" if raw_n is not None else ""
        if n < MIN_INDEP:
            print(f"  {name:34s} n_indep={n:<5,} — too few, refusing to average{extra}")
            return None
        m, t = statistics.fmean(xs), t_stat(xs)
        print(f"  {name:34s} n_indep={n:<5,} mean {m*100:+7.3f}%  t {t:+6.2f}{extra}")
        return m, t, n

    def strict(e):
        return (e["conf"] >= ORIG["conf"] and e["adx"] >= ORIG["adx"]
                and e["er"] >= ORIG["er"])

    print("=" * 74)
    print("PRIMARY (pre-registered): ORIGINAL gates vs the signals loosening ADDED")
    print("=" * 74)
    s_ret  = [e["ret"] for e in indep if strict(e)]
    a_ret  = [e["ret"] for e in indep if not strict(e)]
    raw_s  = sum(1 for e in enriched if strict(e))
    r1 = describe("ORIGINAL gates (all three)", s_ret, raw_s)
    r2 = describe("added by loosening", a_ret, len(enriched) - raw_s)
    print(f"\n  the original gates would have passed {raw_s:,} of "
          f"{len(enriched):,} signals ({100*raw_s/max(len(enriched),1):.1f}%) — "
          f"the loosening multiplied signal volume "
          f"{(len(enriched)/max(raw_s,1)):.1f}x")

    print()
    print("=" * 74)
    print("SECONDARY (context, one gate at a time at its original value)")
    print("=" * 74)
    for key, label in (("conf", "conf >= 0.50 (now 0.35)"),
                       ("adx",  "ADX  >= 18   (now 8)"),
                       ("er",   "ER   >= 0.15 (now 0.03)")):
        hi = [e["ret"] for e in indep if e[key] >= ORIG[key]]
        lo = [e["ret"] for e in indep if e[key] < ORIG[key]]
        describe(f"passes {label}", hi)
        describe(f"fails  {label}", lo)
        print()

    print("=" * 74)
    print("VERDICT")
    print("=" * 74)
    if r1 is None:
        print(f"""
  The original gates pass too few independent signals ({len(s_ret)}) to judge.
  That is itself a finding: at strict thresholds this bot would barely trade,
  and nothing measured so far says the strict signals are different.
  The loosening cannot be evaluated from this sample — keep recording
  (adx/er are now stored on every new shadow signal) and re-run.""")
    else:
        m1, t1, n1 = r1
        diff = (m1 - r2[0]) * 100 if r2 else None
        if t1 > 2 and m1 > 0:
            print(f"""
  The strict subset is POSITIVE (t={t1:+.2f}, n={n1}). The loosening may have
  buried a real edge under noise trades. This justifies re-testing the
  original thresholds properly: out-of-sample, non-overlapping, cost hurdle —
  find_signal.py discipline — before changing the live gates.""")
        else:
            print(f"""
  The strict subset shows no edge either (t={t1:+.2f}, n={n1}"""
                  + (f", vs added-by-loosening diff {diff:+.3f}pp" if diff is not None else "") + f""").
  The coin flip is not an artifact of the loosening — the signal measures
  the same at the original thresholds. The gates were loosened for nothing,
  but they also did not bury anything.""")
    print(f"""
  Multiple-comparisons note: 1 pre-registered primary + 6 secondary views.
  Only the primary carries verdict weight; a lone secondary at |t|~2 is
  expected noise.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
