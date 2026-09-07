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
  6. CPCV + PBO. On top of the single IS/OOS split, every horizon runs
     combinatorial purged cross-validation (contiguous time groups, test pairs
     drawn combinatorially, training bars PURGED where their forward label
     overlaps a test block and EMBARGOED for >= one horizon after it) and the
     Probability of Backtest Overfitting from the resulting path distribution.
     HOUSE RULE: PBO > 0.20 = dead candidate, regardless of headline multiple.
     VENDORING NOTE: the pip package 'purged-cross-validation' does not resolve
     on this box (no distribution found on py3.13), so purge/embargo/
     combinatorial splits are implemented directly below (~120 lines, pure
     stdlib — no numpy needed, which also sidesteps any numpy-version pin).
  7. VENUE COSTS. --venue perp charges the real perp floor: 0.10% taker round
     trip (0.04% maker with --maker) PLUS funding drag for the hold, pulled
     from the funding_rates table when a DB is reachable, else a flat
     historical-mean fallback that is labeled an ESTIMATE. --cost keeps its
     old meaning (overrides the fee leg; spot behavior is unchanged).

Usage:
    python find_signal.py                 # full universe, 1h candles
    python find_signal.py --interval 240  # 4h candles (more calendar time)
    python find_signal.py --history --venue perp   # scored at the perp floor
"""
import argparse
import importlib
import inspect
import json
import math
import os
import statistics
import sys
import time
from itertools import combinations

import bot_server as bs

bs.log = lambda *a, **k: None

HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "history")


def load_history(pair, since_year=None):
    """(closes, highs, lows, vols) from data/history/{pair}_60.csv, or None.

    Multi-year Coinbase data fetched by fetch_history.py. Kraken's API only
    serves 720 candles; these files are what make a REAL out-of-sample split
    possible — 2022's crash and 2024's chop instead of two halves of one
    quiet month.
    """
    path = os.path.join(HISTORY_DIR, f"{pair}_60.csv")
    if not os.path.exists(path):
        return None
    cutoff = 0
    if since_year:
        import calendar
        cutoff = calendar.timegm((since_year, 1, 1, 0, 0, 0))
    c, h, l, v = [], [], [], []
    with open(path, encoding="utf-8") as f:
        next(f, None)                              # header
        for line in f:
            bits = line.rstrip("\n").split(",")
            if len(bits) < 6 or not bits[0].isdigit():
                continue
            if int(bits[0]) < cutoff:
                continue
            try:
                c.append(float(bits[4])); h.append(float(bits[2]))
                l.append(float(bits[3])); v.append(float(bits[5]))
            except ValueError:
                continue
    return (c, h, l, v) if len(c) > 500 else None


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
    """RSI mean reversion at extremes.

    RSI is computed on a bounded 40-bar window, NOT the full history-to-date:
    calc_rsi over an ever-growing slice made this one candidate O(n²) — it
    ground for 20+ minutes across 33 multi-year pairs and died where every
    other candidate finished in seconds (two identical silent deaths at this
    exact point in the battery). Wilder smoothing technically carries longer
    memory, so the windowed RSI is an approximation — accepted, documented,
    and the same for every bar, in exchange for being computable at all.
    """
    if i < 30:
        return None
    try:
        r = bs.calc_rsi(c[max(0, i - 40):i + 1])
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


def load_candidates(spec):
    """--candidate mod:fn[,mod:fn...] -> {name: fn} for THIS run only.

    Each fn is importlib-loaded and must take the same (c, h, l, v, i)
    positional signature as the built-ins (checked by inspect, so a wrong
    closure fails at the CLI, not 20 minutes into a sweep). The module-level
    CANDIDATES dict is never mutated: main() merges the result into a local
    copy, so a research pass can score one hypothesis against the battery
    without re-registering it as a permanent candidate. Names: the function
    name, or mod.fn when that would shadow a built-in.
    """
    out = {}
    for item in [s.strip() for s in str(spec or "").split(",") if s.strip()]:
        if ":" not in item:
            raise ValueError(f"--candidate {item!r}: expected mod:fn")
        mod, _, fn_name = item.rpartition(":")
        if not mod or not fn_name:
            raise ValueError(f"--candidate {item!r}: expected mod:fn")
        try:
            module = importlib.import_module(mod)
        except Exception as e:
            raise ValueError(f"--candidate {item!r}: import failed: {e}")
        fn = getattr(module, fn_name, None)
        if not callable(fn):
            raise ValueError(f"--candidate {item!r}: {fn_name} is not a callable in {mod}")
        try:
            params = [p for p in inspect.signature(fn).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            required = [p for p in params if p.default is p.empty]
        except (TypeError, ValueError):
            params, required = [None] * 5, [None] * 5   # builtins/C callables: trust the call
        if len(params) < 5 or len(required) > 5:
            raise ValueError(f"--candidate {item!r}: {fn_name} must accept (c, h, l, v, i)")
        name = fn_name if (fn_name not in CANDIDATES and fn_name not in out) else f"{mod}.{fn_name}"
        out[name] = fn
    return out


# ── evaluation ───────────────────────────────────────────────────────────────
def fwd(c, i, n, side):
    j = i + n
    if j >= len(c):
        return None
    r = (c[j] - c[i]) / c[i]
    return -r if side == "SELL" else r


def evaluate(fn, series, horizon, lo, hi, warmup=80):
    """Run one candidate over a window. Returns (buys, sells, baseline).

    lo/hi are FRACTIONS of each pair's own length (0.0–1.0). Pairs list on
    different dates, and an absolute split at the shortest pair's midpoint
    would throw away most of a 9-year BTC file to match a 3-year SUI file.
    """
    buys, sells, base = [], [], []
    for c, h, l, v in series:
        a = max(warmup, int(len(c) * lo))
        b = min(int(len(c) * hi), len(c) - horizon - 1)
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


# ── CPCV: combinatorial purged cross-validation ──────────────────────────────
# Vendored, not pip'd: 'purged-cross-validation' has no installable
# distribution here (checked on py3.13 — "no matching distribution"), and the
# sklearn-flavored alternatives drag in pandas for what is, in this codebase,
# a hundred lines of index arithmetic. Pure stdlib, same as the rest of the
# file. References: Bailey, Borwein, Lopez de Prado & Zhu (2017) for PBO;
# Lopez de Prado, "Advances in Financial Machine Learning" ch.7/12 for
# purging, embargo and combinatorial splits.

WARMUP = 80                 # same warmup evaluate() uses
PBO_DEAD_LINE = 0.20        # HOUSE RULE: PBO above this = dead candidate,
                            # regardless of the headline cost multiple.


def cpcv_combinations(n_groups, n_test):
    """All C(n_groups, n_test) ways to pick the test groups."""
    return list(combinations(range(n_groups), n_test))


def group_bounds(n, n_groups):
    """[a, b) bar bounds of each contiguous time group for a pair of n bars.
    Fractional, like evaluate()'s lo/hi — pairs list on different dates."""
    return [(int(n * g / n_groups), int(n * (g + 1) / n_groups))
            for g in range(n_groups)]


def purged_train_mask(n, bounds, test_groups, horizon, embargo):
    """Boolean list: True where a bar may be used for TRAINING.

    A train bar i carries the forward label [i, i+horizon]. It is excluded
    when that label could touch a test block (purge: the `horizon` bars just
    before each test block) and for `embargo` bars after each block (embargo
    is clamped to >= horizon, which also covers test labels that extend past
    the block's right edge). Everything inside a test block is excluded too.
    """
    emb = max(embargo, horizon)
    ok = [True] * n
    for g in test_groups:
        a, b = bounds[g]
        for i in range(max(0, a - horizon), min(n, b + emb)):
            ok[i] = False
    return ok


def test_mask(n, bounds, test_groups):
    """Boolean list: True inside the chosen test blocks."""
    ok = [False] * n
    for g in test_groups:
        a, b = bounds[g]
        for i in range(a, b):
            ok[i] = True
    return ok


def precompute_signals(fn, series):
    """One pass per pair: sigs[k][i] = 'BUY'/'SELL'/None.

    A candidate's signal at bar i depends only on data up to i — never on the
    horizon — so ONE pass serves every horizon and every CPCV split. This is
    the rsi_extreme O(n^2) lesson applied: the splits below only index into
    these arrays; they never re-run the signal functions.
    """
    out = []
    for c, h, l, v in series:
        sig = [None] * len(c)
        for i in range(WARMUP, len(c)):
            sig[i] = fn(c, h, l, v, i)
        out.append(sig)
    return out


def masked_edge(series, sigs, horizon, masks, min_n=10):
    """Mean direction-matched edge over the bars where the mask is True,
    NON-overlapping (a fire consumes `horizon` bars, same as
    test_reversal_depth.run). Returns (mean_edge, n) or (None, n)."""
    adj = []
    for (c, h, l, v), sg, ok in zip(series, sigs, masks):
        n = len(c)
        stop = n - horizon - 1
        base = []
        t = WARMUP
        while t < stop:
            if ok[t]:
                f = fwd(c, t, horizon, "BUY")
                if f is not None:
                    base.append(f)
                t += horizon
            else:
                t += 1
        if not base:
            continue
        bl = statistics.fmean(base)
        i = WARMUP
        while i < stop:
            if ok[i]:
                s = sg[i]
                if s in ("BUY", "SELL"):
                    g = fwd(c, i, horizon, s)
                    if g is not None:
                        adj.append(g - bl if s == "BUY" else g + bl)
                        i += horizon
                        continue
            i += 1
    if len(adj) < min_n:
        return None, len(adj)
    return statistics.fmean(adj), len(adj)


def _pctl(xs, q):
    """Percentile with linear interpolation; None on empty."""
    if not xs:
        return None
    ys = sorted(xs)
    k = (len(ys) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return ys[int(k)]
    return ys[f] * (c - k) + ys[c] * (k - f)


def cpcv_pbo(series, cand_sigs, horizon, n_groups=6, n_test=2, embargo=None,
             min_n=10):
    """CPCV path distribution per candidate + battery PBO for one horizon.

    For each of the C(n_groups, n_test) combinatorial splits:
      - every candidate is scored on the purged/embargoed TRAIN bars and on
        the TEST bars (non-overlapping, direction-matched, per masked_edge);
      - the in-sample (train) winner is found and its RELATIVE RANK among all
        candidates' out-of-sample scores recorded.
    PBO = the share of splits whose IS winner lands in the bottom half OOS
    (Bailey et al. 2017: omega = rank/(M+1), overfit split when omega <= 0.5).

    Returns (per_candidate, pbo, n_splits_used) where per_candidate maps
    name -> {"mean", "p5", "p95", "n_splits"} over that candidate's OOS path
    scores, pbo is a float or None (fewer than 2 scorable candidates
    everywhere), and embargo defaults to the horizon and is clamped to it.
    """
    embargo = horizon if embargo is None else max(embargo, horizon)
    combos = cpcv_combinations(n_groups, n_test)
    lengths = [len(c) for c, _, _, _ in series]
    per = {name: [] for name in cand_sigs}
    overfit, used = 0, 0
    for tg in combos:
        tr_masks, te_masks = [], []
        for n in lengths:
            b = group_bounds(n, n_groups)
            tr_masks.append(purged_train_mask(n, b, tg, horizon, embargo))
            te_masks.append(test_mask(n, b, tg))
        is_s, oos_s = {}, {}
        for name, sigs in cand_sigs.items():
            e_tr, _ = masked_edge(series, sigs, horizon, tr_masks, min_n)
            e_te, _ = masked_edge(series, sigs, horizon, te_masks, min_n)
            if e_tr is not None and e_te is not None:
                is_s[name], oos_s[name] = e_tr, e_te
                per[name].append(e_te)
        if len(is_s) < 2:
            continue
        used += 1
        winner = max(is_s, key=is_s.get)
        rank = 1 + sum(1 for v in oos_s.values() if v < oos_s[winner])
        omega = rank / (len(oos_s) + 1)
        if omega <= 0.5:
            overfit += 1
    stats = {}
    for name, xs in per.items():
        if xs:
            stats[name] = {"mean": statistics.fmean(xs), "p5": _pctl(xs, 0.05),
                           "p95": _pctl(xs, 0.95), "n_splits": len(xs)}
    pbo = (overfit / used) if used else None
    return stats, pbo, used


# ── venue cost model ─────────────────────────────────────────────────────────
# Perp fees: Kraken Futures standard tier, both legs. Funding drag: perps
# charge/pay funding every hour a position is open; over many trades on both
# sides the honest COST floor is the mean |hourly rate| times the hold — it
# overcharges the side that happens to collect, which is the conservative
# direction for a go/no-go gate.
PERP_TAKER_RT = 0.0010      # 0.05% x 2 legs
PERP_MAKER_RT = 0.0004      # 0.02% x 2 legs
# Flat fallback when no funding_rates data is reachable: the canonical
# 0.01%-per-8h perp baseline = 0.00125%/hour (~11%/yr). An ESTIMATE, and
# every line that uses it says so.
FUNDING_HOURLY_FALLBACK = 1.25e-5


def perp_funding_hourly(venue="kraken"):
    """(mean |hourly funding rate|, source label).

    Reads the shared funding_rates table (venue TEXT, symbol TEXT, ts BIGINT,
    rate FLOAT — hourly Kraken Futures funding) when DATABASE_URL + psycopg2
    are available; degrades silently to the flat fallback otherwise. DATA
    HONESTY: the label always states which one you got.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        try:
            import psycopg2
            conn = psycopg2.connect(url, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT AVG(ABS(rate)), COUNT(*) "
                                "FROM funding_rates WHERE venue = %s", (venue,))
                    row = cur.fetchone()
                if row and row[0] is not None:
                    return float(row[0]), (f"funding_rates table mean "
                                           f"({int(row[1])} hourly rows)")
            finally:
                conn.close()
        except Exception:
            pass
    return FUNDING_HOURLY_FALLBACK, ("flat historical-mean ESTIMATE "
                                     "(no funding_rates data reachable)")


def venue_cost(venue, horizon_bars, interval_min, base_rt=None, maker=False,
               funding_hourly=0.0):
    """Round-trip cost of ONE trade held `horizon_bars` bars.

    spot: base_rt (the --cost value) or the live spot round trip — exactly
          the pre-venue behavior, so --cost semantics are unchanged.
    perp: fee round trip (taker unless maker=True; --cost overrides the fee
          leg) + funding drag = mean |hourly rate| x hold hours. Multi-day
          candidates get scored at the real perp floor instead of a flat fee.
    """
    if venue == "perp":
        fee = base_rt if base_rt is not None else (
            PERP_MAKER_RT if maker else PERP_TAKER_RT)
        hold_hours = horizon_bars * interval_min / 60.0
        return fee + funding_hourly * hold_hours
    return base_rt if base_rt is not None else bs.ROUND_TRIP_COST_PCT


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--horizons", default="6,24")
    ap.add_argument("--history", action="store_true",
                    help="use data/history CSVs (multi-year) instead of the live API")
    ap.add_argument("--since", type=int, default=None,
                    help="with --history: trim to candles from this year on")
    ap.add_argument("--cost", type=float, default=None,
                    help="round-trip cost as a decimal (e.g. 0.0005 for the "
                         "0.05%% futures venue); default: the live spot cost. "
                         "With --venue perp this overrides the FEE leg only — "
                         "funding drag is still added.")
    ap.add_argument("--venue", choices=("spot", "perp"), default="spot",
                    help="cost model: spot (default, unchanged) or perp "
                         "(0.10%% taker RT, 0.04%% with --maker, + funding drag)")
    ap.add_argument("--maker", action="store_true",
                    help="with --venue perp: assume maker fills (0.04%% RT)")
    ap.add_argument("--no-cpcv", action="store_true",
                    help="skip the CPCV/PBO pass (quick look only — survivors "
                         "still need it before belief)")
    ap.add_argument("--cpcv-groups", type=int, default=6,
                    help="CPCV: number of contiguous time groups (default 6)")
    ap.add_argument("--cpcv-test", type=int, default=2,
                    help="CPCV: groups held out per split (default 2 -> 15 splits)")
    ap.add_argument("--json", default=None, metavar="PATH",
                    help="also dump results/cpcv_stats/pbo/survivors/cost_for/args "
                         "(the same numbers printed) as one JSON document")
    ap.add_argument("--candidate", default="", metavar="MOD:FN[,MOD:FN]",
                    help="extra candidate closures with the (c,h,l,v,i) signature, "
                         "loaded by importlib for this run only")
    ap.add_argument("--embargo", type=int, default=None,
                    help="CPCV embargo in bars after each test block; default "
                         "= the horizon, and it is clamped to >= the horizon")
    return ap


def _write_json(path, doc):
    """Atomic dump of the run document. Never raises into main(): a bad path
    costs the JSON, not the printed verdict."""
    if not path:
        return False
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True, default=str)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"  (could not write --json {path}: {e})")
        return False


def _result_dict(r):
    name, hz, n_i, e_i, t_i, n_o, e_o, t_o = r
    return {"candidate": name, "horizon": hz, "is_n": n_i, "is_edge": e_i,
            "is_t": t_i, "oos_n": n_o, "oos_edge": e_o, "oos_t": t_o}


def main():
    args = build_parser().parse_args()
    try:
        cands = dict(CANDIDATES)
        cands.update(load_candidates(args.candidate))
    except ValueError as e:
        print(str(e))
        return 2
    if args.venue == "perp":
        funding_h, funding_src = perp_funding_hourly()
    else:
        funding_h, funding_src = 0.0, ""

    def cost_for(hz):
        return venue_cost(args.venue, hz, args.interval, base_rt=args.cost,
                          maker=args.maker, funding_hourly=funding_h)

    pairs = ([p.strip() for p in args.pairs.split(",") if p.strip()]
             or [c["pair"] for c in bs.SCAN_UNIVERSE])
    horizons = [int(x) for x in args.horizons.split(",")]

    series = []
    if args.history:
        print(f"loading {len(pairs)} pairs from {HISTORY_DIR}"
              + (f" (since {args.since})" if args.since else "") + " ...")
        for p in pairs:
            got = load_history(p, args.since)
            if got:
                series.append(got)
        if not series:
            print("no history files — run fetch_history.py first")
            _write_json(args.json, {"error": "no history files", "args": vars(args)})
            return 1
        spans = [len(c) for c, _, _, _ in series]
        print(f"{len(series)} pairs, {min(spans):,}–{max(spans):,} bars "
              f"({max(spans)/8760:.1f} years at the longest)")
    else:
        print(f"fetching {len(pairs)} pairs at {args.interval}m ...")
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
            _write_json(args.json, {"error": "no data", "args": vars(args)})
            return 1
    print("split: first half of EACH pair ranks, second half judges")
    print("candidates are RANKED in-sample and JUDGED out-of-sample")
    if args.venue == "perp":
        fee = args.cost if args.cost is not None else (
            PERP_MAKER_RT if args.maker else PERP_TAKER_RT)
        print(f"venue model: perp — {fee*100:.2f}% "
              f"{'maker' if args.maker and args.cost is None else 'fee'} RT "
              f"+ funding drag {funding_h*100:.5f}%/h [{funding_src}]")
    print()

    sig_cache = None
    if not args.no_cpcv:
        # One signal pass per candidate serves every horizon AND all CPCV
        # splits (signals never depend on the horizon) — no O(n^2) re-runs.
        print("precomputing candidate signals for CPCV ...")
        sig_cache = {name: precompute_signals(fn, series)
                     for name, fn in cands.items()}

    results = []
    cpcv_stats, pbo_by_hz = {}, {}
    for hz in horizons:
        print("=" * 78)
        print(f"  HORIZON {hz} bars")
        print("=" * 78)
        print(f"  {'candidate':20s} {'IS n':>6s} {'IS edge':>9s} {'IS t':>7s} "
              f"{'OOS n':>6s} {'OOS edge':>9s} {'OOS t':>7s}")
        print("  " + "-" * 72)
        for name, fn in cands.items():
            bi, si, base_i = evaluate(fn, series, hz, 0.0, 0.5)
            n_i, e_i, t_i = score(bi, si, base_i)
            bo, so, base_o = evaluate(fn, series, hz, 0.5, 1.0)
            n_o, e_o, t_o = score(bo, so, base_o)
            f = lambda e, t: (f"{e*100:>+8.3f}% {t:>+7.2f}" if e is not None
                              else f"{'—':>9s} {'—':>7s}")
            print(f"  {name:20s} {n_i:>6d} {f(e_i, t_i)} {n_o:>6d} {f(e_o, t_o)}")
            if None not in (t_i, t_o):
                results.append((name, hz, n_i, e_i, t_i, n_o, e_o, t_o))
        print()

        if sig_cache is not None:
            emb = hz if args.embargo is None else max(args.embargo, hz)
            n_spl = len(cpcv_combinations(args.cpcv_groups, args.cpcv_test))
            print(f"  CPCV @ {hz} bars: {args.cpcv_groups} groups, "
                  f"{args.cpcv_test} test -> {n_spl} splits, embargo {emb} bars"
                  + (" (clamped to >= horizon)"
                     if args.embargo is not None and args.embargo < hz else ""))
            stats_h, pbo, used = cpcv_pbo(series, sig_cache, hz,
                                          args.cpcv_groups, args.cpcv_test, emb)
            cpcv_stats[hz], pbo_by_hz[hz] = stats_h, pbo
            print(f"  {'candidate':20s} {'splits':>6s} {'mean':>9s} "
                  f"{'p5':>9s} {'p95':>9s}   (OOS edge/trade across splits)")
            print("  " + "-" * 62)
            for name in cands:
                st = stats_h.get(name)
                if st:
                    print(f"  {name:20s} {st['n_splits']:>6d} "
                          f"{st['mean']*100:>+8.3f}% {st['p5']*100:>+8.3f}% "
                          f"{st['p95']*100:>+8.3f}%")
                else:
                    print(f"  {name:20s} {'—':>6s}   (too few trades per split)")
            if pbo is None:
                print("  PBO: — (fewer than 2 scorable candidates per split)")
            else:
                print(f"  PBO: {pbo:.2f} over {used} splits — the IS winner "
                      f"landed in the bottom half OOS that often."
                      f"  [> {PBO_DEAD_LINE:.2f} = overfit battery]")
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
        hz0 = survivors[0][1]
        print(f"  the edge must exceed the {cost_for(hz0)*100:.3f}% round trip"
              + (f" at the {args.venue} venue"
                 f"{' (incl. funding drag for the hold)' if args.venue == 'perp' else ''}."
                 if args.venue == "perp" or args.cost is not None else "."))
        for name, hz, n_i, e_i, t_i, n_o, e_o, t_o in survivors:
            cst = cost_for(hz)
            mult = e_o / cst if cst > 0 else float("inf")
            print(f"     {name} @ {hz}: OOS edge is {mult:+.2f}x the "
                  f"{cst*100:.3f}% {args.venue} round trip"
                  + ("" if mult > 1 else " — does NOT clear it"))
            st = cpcv_stats.get(hz, {}).get(name)
            pbo = pbo_by_hz.get(hz)
            if st:
                print(f"        CPCV: mean {st['mean']*100:+.3f}% "
                      f"[p5 {st['p5']*100:+.3f}%, p95 {st['p95']*100:+.3f}%] "
                      f"over {st['n_splits']} splits")
            elif not args.no_cpcv:
                print("        CPCV: too few trades per split to score — "
                      "treat the headline as UNCONFIRMED")
            if pbo is not None:
                if pbo > PBO_DEAD_LINE:
                    print(f"        PBO {pbo:.2f} > {PBO_DEAD_LINE:.2f} — "
                          f"HOUSE RULE: DEAD candidate, regardless of the "
                          f"headline multiple.")
                else:
                    print(f"        PBO {pbo:.2f} (<= {PBO_DEAD_LINE:.2f} "
                          f"house line — selection not obviously overfit)")
            elif args.no_cpcv:
                print("        PBO not computed (--no-cpcv) — required "
                      "before belief.")
        print("  NOTE: these windows OVERLAP — any survivor must still pass the")
        print("  non-overlapping re-test (see test_reversal_depth.py) before belief.")
        print("  The CPCV columns above ARE non-overlapping, but they are an")
        print("  additional gate, not a replacement for that re-test.")
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
    # --json: the SAME numbers printed above, as one document (results,
    # cpcv_stats, pbo, survivors, cost_for, args). Read by the research pass;
    # nothing here is recomputed, so the file can never disagree with stdout.
    if args.json:
        doc = {
            "generated": time.time(),
            "args": vars(args),
            "candidates": list(cands),
            "n_pairs": len(series),
            "bars_per_pair": [len(c) for c, _, _, _ in series],
            "funding": {"hourly": funding_h, "source": funding_src},
            "results": [_result_dict(r) for r in results],
            "cpcv_stats": {str(hz): st for hz, st in cpcv_stats.items()},
            "pbo": {str(hz): p for hz, p in pbo_by_hz.items()},
            "pbo_dead_line": PBO_DEAD_LINE,
            "survivors": [_result_dict(r) for r in survivors],
            "survivor_bar_oos_t": bar,
            "cost_for": {str(hz): cost_for(hz) for hz in horizons},
        }
        if _write_json(args.json, doc):
            print(f"  -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
