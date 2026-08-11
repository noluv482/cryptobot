#!/usr/bin/env python3
"""Does the perp FUNDING RATE predict spot price? The first non-price signal.

Every price-derived candidate failed (15 tested, none survived validation), and
the bot's own signal measures a coin flip. Funding is different INFORMATION,
not a different arrangement of the same series: it is what leveraged traders
are paying to hold their positions. Extreme positive funding = crowded longs
paying through the nose; the classic hypothesis is that crowded trades unwind.

Candidates (two only, to keep the fishing expedition small):
  fund_z_fade : z-score of the funding rate over the trailing 7 days.
                z > +2 -> SELL (crowded longs), z < -2 -> BUY.
  fund_extreme: funding above its trailing 90th percentile -> SELL,
                below the 10th -> BUY.

Data: Kraken Futures public historical funding rates (PF_ perps), aligned to
spot 1h candles by forward-fill — a bar sees only funding entries whose
timestamp is at or before the bar. No lookahead.

Discipline (same rules that killed rsi_extreme and rsi_zone):
  full universe · IS/OOS time split · NON-OVERLAPPING forward windows ·
  direction-matched baselines · explicit cost hurdle (0.52% round trip)

Usage:
    python test_funding_signal.py
"""
import json
import math
import statistics
import sys
import time
import urllib.request

import bot_server as bs
from analyze_stale_exit import fetch_ohlc          # cached spot 1h OHLC with timestamps
from find_signal import score                       # direction-matched edge + t

bs.log = lambda *a, **k: None

FUT = "https://futures.kraken.com/derivatives/api/v4/historicalfundingrates"

# Spot pair -> Kraken Futures perp symbol. XBT keeps its name; spot XDG is DOGE
# on futures. Everything else follows PF_{COIN}USD, and pairs whose perp does
# not exist just drop out with a note rather than silently vanishing.
def perp_symbol(pair):
    coin = pair[:-3]
    if coin == "XDG":
        return ["PF_DOGEUSD", "PF_XDGUSD"]
    return [f"PF_{coin}USD"]


def fetch_funding(pair):
    """[(epoch_seconds, relative_rate)] ascending, or [] if no perp exists."""
    for sym in perp_symbol(pair):
        url = f"{FUT}?symbol={sym}"
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                d = json.loads(r.read().decode())
        except Exception:
            continue
        rows = d.get("rates") or []
        out = []
        for x in rows:
            try:
                ts = x["timestamp"]
                # ISO8601 like 2026-08-01T12:00:00.000Z
                t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
                out.append((t, float(x["relativeFundingRate"])))
            except Exception:
                continue
        if out:
            out.sort()
            return out
    return []


def align(bars, funding):
    """Per-bar funding value, forward-filled. bars[i] sees only entries with
    timestamp <= bars[i].t — funding published later never leaks backward."""
    vals, j, cur = [], 0, None
    for b in bars:
        while j < len(funding) and funding[j][0] <= b["t"]:
            cur = funding[j][1]
            j += 1
        vals.append(cur)
    return vals


def sig_z(fv, i, window=168, k=2.0):
    w = [x for x in fv[max(0, i - window):i + 1] if x is not None]
    if len(w) < 48 or fv[i] is None:
        return None
    m = statistics.fmean(w)
    sd = statistics.pstdev(w)
    if sd < 1e-12:
        return None
    z = (fv[i] - m) / sd
    if z > k:  return "SELL"
    if z < -k: return "BUY"
    return None


def sig_pct(fv, i, window=168):
    w = sorted(x for x in fv[max(0, i - window):i + 1] if x is not None)
    if len(w) < 48 or fv[i] is None:
        return None
    lo = w[int(len(w) * 0.10)]
    hi = w[int(len(w) * 0.90)]
    if fv[i] >= hi and fv[i] > 0: return "SELL"
    if fv[i] <= lo and fv[i] < 0: return "BUY"
    return None


CANDS = {"fund_z_fade": sig_z, "fund_extreme": sig_pct}


def fwd(closes, i, n, side):
    j = i + n
    if j >= len(closes):
        return None
    r = (closes[j] - closes[i]) / closes[i]
    return -r if side == "SELL" else r


def run_window(data, fn, hz, lo_frac, hi_frac):
    """Non-overlapping signals and baseline over a fraction of each pair's bars."""
    buys, sells, base = [], [], []
    for closes, fv in data:
        n = len(closes)
        lo, hi = int(n * lo_frac), int(n * hi_frac)
        b = min(hi, n - hz - 1)
        t = max(lo, 200)                       # z needs a 168-bar warmup
        while t < b:                           # baseline, non-overlapping
            f = fwd(closes, t, hz, "BUY")
            if f is not None:
                base.append(f)
            t += hz
        i = max(lo, 200)
        while i < b:
            s = fn(fv, i)
            if s:
                g = fwd(closes, i, hz, s)
                if g is not None:
                    (buys if s == "BUY" else sells).append(g)
                    i += hz
                    continue
            i += 1
    return buys, sells, base


def main():
    pairs = [c["pair"] for c in bs.SCAN_UNIVERSE]
    cost = bs.ROUND_TRIP_COST_PCT
    print(f"fetching funding + spot candles for {len(pairs)} pairs ...")
    data, nofut = [], []
    for p in pairs:
        fund = fetch_funding(p)
        time.sleep(0.4)
        if not fund:
            nofut.append(p)
            continue
        bars = fetch_ohlc(p)
        if len(bars) < 400:
            continue
        closes = [b["c"] for b in bars]
        fv = align(bars, fund)
        cov = sum(1 for x in fv if x is not None) / len(fv)
        if cov < 0.6:
            nofut.append(p + "(sparse)")
            continue
        data.append((closes, fv))
    print(f"  {len(data)} pairs with funding data; skipped: {', '.join(nofut) or 'none'}\n")
    if len(data) < 8:
        print("too few pairs with funding history to judge")
        return 1

    print("=" * 76)
    print("  FUNDING vs FORWARD SPOT RETURN — IS ranked, OOS judged")
    print(f"  non-overlapping windows · direction-matched baseline · cost {cost*100:.2f}%")
    print("=" * 76)
    results = []
    for name, fn in CANDS.items():
        print(f"\n  --- {name} ---")
        print(f"  {'hz':>4s} {'IS n':>5s} {'IS edge':>9s} {'IS t':>6s} "
              f"{'OOS n':>6s} {'OOS edge':>9s} {'OOS t':>6s} {'xcost':>6s}")
        print("  " + "-" * 58)
        for hz in (6, 24, 48):
            bi, si, basei = run_window(data, fn, hz, 0.0, 0.5)
            ni, ei, ti = score(bi, si, basei)
            bo, so, baseo = run_window(data, fn, hz, 0.5, 1.0)
            no, eo, to = score(bo, so, baseo)
            if ei is None or eo is None:
                print(f"  {hz:>4d} {ni:>5d}   too few signals ({ni} IS / {no} OOS)")
                continue
            xc = abs(eo) / cost
            results.append((name, hz, ni, ei, ti, no, eo, to))
            flag = "" if (ei > 0) == (eo > 0) else "  SIGN FLIP"
            print(f"  {hz:>4d} {ni:>5d} {ei*100:>+8.3f}% {ti:>+6.2f} "
                  f"{no:>6d} {eo*100:>+8.3f}% {to:>+6.2f} {xc:>5.2f}x{flag}")

    print()
    print("=" * 76)
    print("  VERDICT")
    print("=" * 76)
    surv = [r for r in results
            if abs(r[4]) > 2 and abs(r[7]) > 2 and (r[3] > 0) == (r[6] > 0)
            and abs(r[6]) > cost]
    if surv:
        for name, hz, ni, ei, ti, no, eo, to in surv:
            print(f"  SURVIVOR: {name} @ {hz}h — IS {ei*100:+.3f}% (t{ti:+.2f}), "
                  f"OOS {eo*100:+.3f}% (t{to:+.2f}), {abs(eo)/cost:.1f}x cost")
        print()
        print("  One regime, one exchange. Before money: re-run in a month on")
        print("  fresh data, and validate on 4h bars the way rsi_extreme was —")
        print("  that check is what killed the last 'survivor'.")
    else:
        best = max(results, key=lambda r: abs(r[7])) if results else None
        print("  NOTHING SURVIVED both halves at a size that clears costs.")
        if best:
            print(f"  best OOS: {best[0]} @ {best[1]}h, edge {best[6]*100:+.3f}%, "
                  f"t {best[7]:+.2f}")
        print("  Funding on this window does not pay for its own round trip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
