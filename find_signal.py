#!/usr/bin/env python3
"""Search for an entry signal that actually predicts direction.

The bot's current signal measures a coin flip (t = +0.04 over 1,421 signals),
so this tests a battery of candidates the same way and reports whether ANY of
them beat a random entry of the same direction.

The discipline matters more than the candidates. This project has already
produced one false finding -- a 3-pair Donchian smoke test showing +37.1% that
became -58.0% on the full 16-pair universe -- so the rules here are:

  1. FULL UNIVERSE. Never a hand-picked subset. A signal that only works on the
     pairs you chose is a story about the pairs.
  2. SPLIT BY TIME. Candidates are ranked on the FIRST half of history and then
     judged on the SECOND half, which is never consulted during selection. An
     in-sample number is a hypothesis; only the out-of-sample number is evidence.
  3. DIRECTION-MATCHED BASELINE. A long/short signal compared against
     buy-and-hold scores the market's trend, not the signal. Each side is
     compared against its own baseline. (This exact mistake produced a spurious
     t = -4.19 earlier today.)
  4. MULTIPLE COMPARISONS. Testing N candidates means the best one looks good by
     luck alone. With ~20 candidates, an in-sample |t| > 2 is expected NOISE.
     The bar is out-of-sample significance, in the same direction.
  5. NO LOOKAHEAD. Every candidate sees closes[:i+1] only.

Usage:
    python find_signal.py                 # full universe, 1h candles
    python find_signal.py --interval 240  # 4h candles (more calendar time)
"""
import argparse
import math
import statistics
import sys
import time

import bot_server as bs

bs.log = lambda *a, **k: None


# ── candidate signals ────────────────────────────────────────────────────────
# Each takes the series and an index, and returns "BUY" / "SELL" / None using
# ONLY data up to and including i. Kept deliberately simple: a signal that needs
# heavy fitting to show an edge is fitting, not predicting.

def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _z(xs, n):
    if len(xs) < n:
        return None
    w = xs[-n:]
    m = statistics.fmean(w)
    sd = statistics.pstdev(w)
    return (w[-1] - m) / sd if sd > 1e-12 else None


def sig_reversal_1(c, h, l, v, i, n=1):
    """Short-term reversal: fade the last bar's move. The single best-documented
    short-horizon effect in crypto — overreaction that partially unwinds."""
    if i < n + 1:
        return None
    r = (c[i] - c[i - n]) / c[i - n]
    if r > 0.004:  return "SELL"
    if r < -0.004: return "BUY"
    return None


def sig_reversal_6(c, h, l, v, i):
    return sig_reversal_1(c, h, l, v, i, n=6)


def sig_reversal_24(c, h, l, v, i):
    return sig_reversal_1(c, h, l, v, i, n=24)


def sig_zscore_fade(c, h, l, v, i, n=24, k=1.5):
    """Fade a stretched price relative to its own recent mean."""
    z = _z(c[:i + 1], n)
    if z is None:
        return None
    if z > k:  return "SELL"
    if z < -k: return "BUY"
    return None


def sig_zscore_fade_48(c, h, l, v, i):
    return sig_zscore_fade(c, h, l, v, i, n=48, k=1.5)


def sig_momentum_24(c, h, l, v, i, n=24):
    """Trend continuation: go with the last n bars' direction."""
    if i < n + 1:
        return None
    r = (c[i] - c[i - n]) / c[i - n]
    if r > 0.01:  return "BUY"
    if r < -0.01: return "SELL"
    return None


def sig_momentum_72(c, h, l, v, i):
    return sig_momentum_24(c, h, l, v, i, n=72)


def sig_ma_cross(c, h, l, v, i, fast=12, slow=48):
    """Classic MA crossover state."""
    if i < slow + 1:
        return None
    f, s = _sma(c[:i + 1], fast), _sma(c[:i + 1], slow)
    pf, ps = _sma(c[:i], fast), _sma(c[:i], slow)
    if None in (f, s, pf, ps):
        return None
    if pf <= ps and f > s:  return "BUY"
    if pf >= ps and f < s:  return "SELL"
    return None


def sig_breakout_24(c, h, l, v, i, n=24):
    """Donchian breakout — already rejected once at 1/9 settings; included so
    the battery is not quietly stacked toward reversal."""
    if i < n + 1:
        return None
    hi, lo = max(h[i - n:i]), min(l[i - n:i])
    if c[i] > hi:  return "BUY"
    if c[i] < lo:  return "SELL"
    return None


def sig_vol_expansion(c, h, l, v, i, n=24):
    """Direction of the bar that breaks out of a quiet range."""
    if i < n + 2:
        return None
    rng = [(h[j] - l[j]) / c[j] for j in range(i - n, i)]
    cur = (h[i] - l[i]) / c[i]
    if cur < 2 * statistics.fmean(rng):
        return None
    return "BUY" if c[i] > (h[i] + l[i]) / 2 else "SELL"


def sig_volume_spike_fade(c, h, l, v, i, n=24):
    """Fade the move on an abnormal volume bar (capitulation/blowoff)."""
    if i < n + 2 or not v[i]:
        return None
    med = statistics.median(v[i - n:i])
    if med <= 0 or v[i] < 3 * med:
        return None
    r = (c[i] - c[i - 1]) / c[i - 1]
    if r > 0.003:  return "SELL"
    if r < -0.003: return "BUY"
    return None


def sig_rsi_extreme(c, h, l, v, i):
    """RSI mean reversion at extremes."""
    if i < 30:
        return None
    try:
        r = bs.calc_rsi(c[:i + 1])
    except Exception:
        return None
    if r is None:
        return None
    if r > 75: return "SELL"
    if r < 25: return "BUY"
    return None


def sig_inside_bar(c, h, l, v, i):
    """Range contraction then resolution."""
    if i < 3:
        return None
    if h[i - 1] < h[i - 2] and l[i - 1] > l[i - 2]:
        if c[i] > h[i - 1]: return "BUY"
        if c[i] < l[i - 1]: return "SELL"
    return None


def sig_gap_fade(c, h, l, v, i):
    """Fade a bar that opens far from the previous close."""
    if i < 2:
        return None
    r = (c[i] - c[i - 1]) / c[i - 1]
    if abs(r) < 0.015:
        return None
    return "SELL" if r > 0 else "BUY"


def sig_three_down(c, h, l, v, i):
    """Buy after three consecutive down bars (and mirror)."""
    if i < 4:
        return None
    d = [c[j] < c[j - 1] for j in (i, i - 1, i - 2)]
    u = [c[j] > c[j - 1] for j in (i, i - 1, i - 2)]
    if all(d): return "BUY"
    if all(u): return "SELL"
    return None


CANDIDATES = {
    "reversal_1bar":      sig_reversal_1,
    "reversal_6bar":      sig_reversal_6,
    "reversal_24bar":     sig_reversal_24,
    "zscore_fade_24":     sig_zscore_fade,
    "zscore_fade_48":     sig_zscore_fade_48,
    "momentum_24":        sig_momentum_24,
    "momentum_72":        sig_momentum_72,
    "ma_cross_12_48":     sig_ma_cross,
    "breakout_24":        sig_breakout_24,
    "vol_expansion":      sig_vol_expansion,
    "volume_spike_fade":  sig_volume_spike_fade,
    "rsi_extreme":        sig_rsi_extreme,
    "inside_bar":         sig_inside_bar,
    "gap_fade":           sig_gap_fade,
    "three_in_a_row":     sig_three_down,
}


# ── evaluation ───────────────────────────────────────────────────────────────
def fwd(c, i, n, side):
    j = i + n
    if j >= len(c):
        return None
    r = (c[j] - c[i]) / c[i]
    return -r if side == "SELL" else r


def evaluate(fn, series, horizon, lo, hi, warmup=80):
    """Run one candidate over [lo, hi). Returns (buys, sells, baseline)."""
    buys, sells, base = [], [], []
    for c, h, l, v in series:
        a = max(warmup, lo)
        b = min(hi, len(c) - horizon - 1)
        for i in range(a, b):
            f = fwd(c, i, horizon, "BUY")
            if f is not None:
                base.append(f)
            s = fn(c, h, l, v, i)
            if s in ("BUY", "SELL"):
                g = fwd(c, i, horizon, s)
                if g is not None:
                    (buys if s == "BUY" else sells).append(g)
    return buys, sells, base


def score(buys, sells, base):
    """Edge vs a direction-matched baseline, and its t. Returns (n, edge, t)."""
    n = len(buys) + len(sells)
    if n < 30 or not base:
        return n, None, None
    bl = statistics.fmean(base)
    # Each side against its own baseline; a short's fair comparison is -long.
    adj = [x - bl for x in buys] + [x + bl for x in sells]
    m = statistics.fmean(adj)
    sd = statistics.pstdev(adj)
    if sd < 1e-12:
        return n, None, None
    return n, m, m / (sd / math.sqrt(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--horizons", default="6,24")
    args = ap.parse_args()

    pairs = ([p.strip() for p in args.pairs.split(",") if p.strip()]
             or [c["pair"] for c in bs.SCAN_UNIVERSE])
    horizons = [int(x) for x in args.horizons.split(",")]

    print(f"fetching {len(pairs)} pairs at {args.interval}m ...")
    series = []
    for p in pairs:
        try:
            c, h, l, v, o = bs.get_klines(p, interval=args.interval, limit=720)
        except Exception:
            continue
        if len(c) >= 300:
            series.append((c, h, l, v))
        time.sleep(1.1)
    if not series:
        print("no data")
        return 1
    n_bars = min(len(c) for c, _, _, _ in series)
    mid = n_bars // 2
    print(f"{len(series)} pairs, {n_bars} bars each")
    print(f"IN-SAMPLE  bars 80..{mid}   OUT-OF-SAMPLE bars {mid}..{n_bars}")
    print("candidates are RANKED in-sample and JUDGED out-of-sample\n")

    results = []
    for hz in horizons:
        print("=" * 78)
        print(f"  HORIZON {hz} bars")
        print("=" * 78)
        print(f"  {'candidate':20s} {'IS n':>6s} {'IS edge':>9s} {'IS t':>7s} "
              f"{'OOS n':>6s} {'OOS edge':>9s} {'OOS t':>7s}")
        print("  " + "-" * 72)
        for name, fn in CANDIDATES.items():
            bi, si, base_i = evaluate(fn, series, hz, 80, mid)
            n_i, e_i, t_i = score(bi, si, base_i)
            bo, so, base_o = evaluate(fn, series, hz, mid, n_bars)
            n_o, e_o, t_o = score(bo, so, base_o)
            f = lambda e, t: (f"{e*100:>+8.3f}% {t:>+7.2f}" if e is not None
                              else f"{'—':>9s} {'—':>7s}")
            print(f"  {name:20s} {n_i:>6d} {f(e_i, t_i)} {n_o:>6d} {f(e_o, t_o)}")
            if None not in (t_i, t_o):
                results.append((name, hz, n_i, e_i, t_i, n_o, e_o, t_o))
        print()

    # ── verdict ──────────────────────────────────────────────────────────────
    print("=" * 78)
    print("  VERDICT")
    print("=" * 78)
    n_tests = len(results)
    # Bonferroni-style bar: with this many candidates, ~1 will clear |t|>2 by luck.
    bar = 2.5
    survivors = [r for r in results
                 if abs(r[4]) > 2 and abs(r[7]) > bar and (r[4] > 0) == (r[7] > 0)]
    print(f"  {n_tests} candidate/horizon combinations tested.")
    print(f"  With that many tests, an in-sample |t| > 2 is expected by chance alone,")
    print(f"  so a survivor must ALSO clear |t| > {bar} out-of-sample in the SAME direction.")
    print()
    if survivors:
        for name, hz, n_i, e_i, t_i, n_o, e_o, t_o in survivors:
            d = "as-is" if e_o > 0 else "INVERTED (the rule predicts the opposite)"
            print(f"  SURVIVOR: {name} @ {hz} bars")
            print(f"     in-sample  edge {e_i*100:+.3f}%  t {t_i:+.2f}  (n={n_i})")
            print(f"     out-sample edge {e_o*100:+.3f}%  t {t_o:+.2f}  (n={n_o})")
            print(f"     -> trade it {d}")
        print()
        print("  Still only ONE market regime and one exchange. Before this goes")
        print("  anywhere near money it needs a different period and a cost check:")
        print(f"  the edge must exceed the {bs.ROUND_TRIP_COST_PCT*100:.2f}% round trip.")
    else:
        best = max(results, key=lambda r: abs(r[7])) if results else None
        print("  NOTHING SURVIVED. No candidate beat a direction-matched random")
        print("  entry both in-sample and out-of-sample.")
        if best:
            print(f"  Best out-of-sample was {best[0]} @ {best[1]}b: "
                  f"edge {best[6]*100:+.3f}%, t {best[7]:+.2f} — inside noise.")
        print()
        print("  This is the expected result. Hourly crypto is close to efficient,")
        print("  and simple price-derived rules are the most-mined ideas there are.")
        print("  An edge, if one exists here, will come from information the price")
        print("  series does not contain — not from another arrangement of it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
